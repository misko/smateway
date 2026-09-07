#!/usr/bin/env python3
"""Render the measured 50 MHz antenna comparison from the two five-sweep cohorts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

FREQUENCIES_100_HZ = np.arange(2_100_000_000, 5_800_000_001, 100_000_000)
FREQUENCIES_MID_HZ = np.arange(2_150_000_000, 5_750_000_001, 100_000_000)
FREQUENCIES_50_HZ = np.arange(2_100_000_000, 5_800_000_001, 50_000_000)
STATES = tuple(f"ANT{index}" for index in range(1, 8))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort-json",
        type=Path,
        default=Path("docs/broadband_future_sweep_comparison/data/comparison.json"),
    )
    parser.add_argument(
        "--midpoint-json",
        type=Path,
        default=Path("docs/broadband_midpoint_campaign/data/campaign-results.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/broadband_midpoint_campaign/png/"
            "fig07_combined_50mhz_antenna_comparison.png"
        ),
    )
    return parser


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _phasor_runs(row: dict[str, Any], real_key: str, imag_key: str) -> np.ndarray:
    real = np.asarray(row[real_key], dtype=np.float64)
    imag = np.asarray(row[imag_key], dtype=np.float64)
    values = real + 1j * imag
    if values.ndim != 2 or values.shape[0] != 5:
        raise ValueError("each frequency cohort must contain exactly five sweeps")
    return values


def _midpoint_runs(row: dict[str, Any]) -> np.ndarray:
    values = np.asarray(
        [
            np.asarray(run["real"], dtype=np.float64)
            + 1j * np.asarray(run["imag"], dtype=np.float64)
            for run in row["runs"]
        ]
    )
    if values.shape != (5, len(FREQUENCIES_MID_HZ)):
        raise ValueError("midpoint cohort does not have the exact five-by-37 geometry")
    return values


def _combined_runs(
    cohort_row: dict[str, Any], midpoint_row: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    knots = _phasor_runs(cohort_row, "future_real", "future_imag")
    if knots.shape[1] != len(FREQUENCIES_100_HZ):
        raise ValueError("100 MHz cohort does not have the exact five-by-38 geometry")
    midpoints = _midpoint_runs(midpoint_row)
    combined = np.empty((5, len(FREQUENCIES_50_HZ)), dtype=np.complex128)
    combined[:, 0::2] = knots
    combined[:, 1::2] = midpoints
    return combined, np.mean(knots, axis=0)


def _phase_summary(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    circular_mean = np.mean(values / np.abs(values), axis=0)
    mean_deg = np.degrees(np.unwrap(np.angle(circular_mean)))
    deviations = np.degrees(np.angle(values / circular_mean))
    return mean_deg, mean_deg + np.min(deviations, axis=0), mean_deg + np.max(
        deviations, axis=0
    )


def _gain_summary(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    gain_db = 20.0 * np.log10(np.abs(values))
    return np.mean(gain_db, axis=0), np.min(gain_db, axis=0), np.max(gain_db, axis=0)


def render(cohort: dict[str, Any], midpoint: dict[str, Any], output: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if cohort.get("evidence_kind") != "smateway.broadband-cohort-comparison/v1":
        raise ValueError("unexpected 100 MHz cohort evidence kind")
    if midpoint.get("evidence_kind") != "smateway.broadband-midpoint-campaign/v1":
        raise ValueError("unexpected midpoint evidence kind")
    cohort_by_state = {row["state"]: row for row in cohort["curve_data"]}
    midpoint_by_state = {row["state"]: row for row in midpoint["measured_midpoints"]}
    if set(cohort_by_state) != set(STATES) or set(midpoint_by_state) != set(STATES):
        raise ValueError("antenna state set is not ANT1 through ANT7")

    frequency_ghz = FREQUENCIES_50_HZ / 1e9
    plt.rcParams.update({"font.size": 9, "figure.dpi": 120, "savefig.dpi": 180})
    figure, axes = plt.subplots(
        len(STATES),
        2,
        figsize=(15, 20),
        sharex=True,
    )
    for row_index, state in enumerate(STATES):
        combined, _ = _combined_runs(cohort_by_state[state], midpoint_by_state[state])
        phase_mean, phase_low, phase_high = _phase_summary(combined)
        gain_mean, gain_low, gain_high = _gain_summary(combined)
        for axis, mean, low, high, ylabel in (
            (axes[row_index, 0], phase_mean, phase_low, phase_high, "Phase (deg)"),
            (axes[row_index, 1], gain_mean, gain_low, gain_high, "Gain (dB)"),
        ):
            axis.fill_between(
                frequency_ghz,
                low,
                high,
                color="#9CA3AF",
                alpha=0.24,
                linewidth=0,
                label="five-sweep range",
            )
            axis.plot(
                frequency_ghz,
                mean,
                color="#1F2937",
                linewidth=1.15,
                label="combined 50 MHz mean",
            )
            axis.scatter(
                frequency_ghz[0::2],
                mean[0::2],
                color="#4C78A8",
                s=12,
                zorder=3,
                label="100 MHz cohort",
            )
            axis.scatter(
                frequency_ghz[1::2],
                mean[1::2],
                color="#F58518",
                marker="D",
                s=11,
                zorder=3,
                label="50 MHz midpoints",
            )
            axis.set_ylabel(ylabel)
            axis.grid(alpha=0.22)
        axes[row_index, 0].set_title(f"{state} vs ANT8 — unwrapped relative phase")
        axes[row_index, 1].set_title(f"{state} vs ANT8 — relative path gain")

    for axis in axes[-1, :]:
        axis.set_xlabel("RF frequency (GHz)")
        axis.set_xlim(frequency_ghz[0] - 0.02, frequency_ghz[-1] + 0.02)
        axis.set_xticks(np.arange(2.1, 5.81, 0.5))
    handles, labels = axes[0, 0].get_legend_handles_labels()
    figure.subplots_adjust(
        top=0.94,
        bottom=0.045,
        left=0.065,
        right=0.985,
        hspace=0.34,
        wspace=0.17,
    )
    figure.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.963),
        ncol=4,
        frameon=False,
    )
    figure.suptitle(
        "Measured antenna-path comparison on the combined 50 MHz grid\n"
        "ANT1–ANT7 relative to ANT8; 75 frequencies; mean and full range of five sweeps",
        fontsize=15,
        y=0.995,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output)
    plt.close(figure)


def main() -> int:
    args = _parser().parse_args()
    render(_load(args.cohort_json), _load(args.midpoint_json), args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
