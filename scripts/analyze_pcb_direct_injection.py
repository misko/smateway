#!/usr/bin/env python3
"""Summarize and plot a direct-injection PCB selector sweep."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run.json from run_pinned_static_screen.py")
    parser.add_argument("--state", required=True, choices=tuple(f"ANT{i}" for i in range(1, 9)))
    parser.add_argument(
        "--all-off-run",
        type=Path,
        help="optional run.json containing a separate, possibly sparse ALL_OFF sweep",
    )
    parser.add_argument("--output", type=Path, help="PNG output path")
    parser.add_argument("--summary", type=Path, help="JSON summary output path")
    return parser


def _transfer(observation: dict[str, Any]) -> complex:
    analysis = observation.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("observation is missing analysis")
    transfer = analysis.get("transfer_rx2_over_rx1")
    if not isinstance(transfer, dict):
        raise ValueError("observation is missing transfer_rx2_over_rx1")
    return complex(float(transfer["real"]), float(transfer["imag"]))


def main() -> int:
    args = _parser().parse_args()
    run = json.loads(args.run.read_text())
    observations = run.get("observations")
    if not isinstance(observations, list):
        raise ValueError("run is missing observations")
    all_off_run = json.loads(args.all_off_run.read_text()) if args.all_off_run else run
    all_off_observations = all_off_run.get("observations")
    if not isinstance(all_off_observations, list):
        raise ValueError("ALL_OFF run is missing observations")

    selected: dict[int, dict[str, Any]] = {}
    all_off: dict[int, dict[str, Any]] = {}
    for observation in observations:
        if observation.get("analysis_error") is not None:
            continue
        frequency_hz = int(observation["frequency_hz"])
        state = observation.get("state")
        if state != args.state:
            continue
        if frequency_hz in selected:
            raise ValueError(f"duplicate {state} observation at {frequency_hz} Hz")
        selected[frequency_hz] = observation
    for observation in all_off_observations:
        if observation.get("analysis_error") is not None or observation.get("state") != "ALL_OFF":
            continue
        frequency_hz = int(observation["frequency_hz"])
        if frequency_hz in all_off:
            raise ValueError(f"duplicate ALL_OFF observation at {frequency_hz} Hz")
        all_off[frequency_hz] = observation

    frequencies_hz = np.asarray(sorted(selected), dtype=np.float64)
    if frequencies_hz.size < 3:
        raise ValueError("at least three selected frequencies are required")
    if not all_off:
        raise ValueError("at least one ALL_OFF frequency is required")
    all_off_frequencies_hz = np.asarray(sorted(all_off), dtype=np.float64)

    selected_transfer = np.asarray(
        [_transfer(selected[int(frequency)]) for frequency in frequencies_hz],
        dtype=np.complex128,
    )
    all_off_transfer = np.asarray(
        [_transfer(all_off[int(frequency)]) for frequency in all_off_frequencies_hz],
        dtype=np.complex128,
    )
    selected_db = 20.0 * np.log10(np.abs(selected_transfer))
    all_off_db = 20.0 * np.log10(np.abs(all_off_transfer))
    paired_frequencies = sorted(set(selected) & set(all_off))
    paired_contrast_db = np.asarray(
        [
            20.0
            * math.log10(abs(_transfer(selected[frequency])) / abs(_transfer(all_off[frequency])))
            for frequency in paired_frequencies
        ],
        dtype=np.float64,
    )
    phase_rad = np.unwrap(np.angle(selected_transfer))

    centered_hz = frequencies_hz - float(np.mean(frequencies_hz))
    slope_rad_per_hz, intercept_rad = np.polyfit(centered_hz, phase_rad, 1)
    fitted_phase_rad = intercept_rad + slope_rad_per_hz * centered_hz
    residual_deg = np.rad2deg(phase_rad - fitted_phase_rad)
    delay_ns = -slope_rad_per_hz / (2.0 * math.pi) * 1e9

    configuration = run.get("configuration", {})
    step_hz = int(np.min(np.diff(frequencies_hz)))
    def safe(document: dict[str, Any]) -> bool:
        return bool(
            document.get("error") is None
            and document.get("final_radio_mute", {}).get("passed") is True
            and document.get("final_source_radio_mute", {}).get("passed") is True
            and document.get("final_selector", {}).get("applied_code") == 8
            and document.get("final_selector", {}).get("lease_active") is False
        )

    safety_passed = safe(run) and safe(all_off_run)
    quality_observations = [*observations, *([] if all_off_run is run else all_off_observations)]
    summary = {
        "schema": 1,
        "run_id": run.get("run_id"),
        "selected_state": args.state,
        "frequency_count": int(frequencies_hz.size),
        "all_off_frequency_count": int(all_off_frequencies_hz.size),
        "paired_contrast_frequency_count": len(paired_frequencies),
        "first_hz": int(frequencies_hz[0]),
        "last_hz": int(frequencies_hz[-1]),
        "step_hz": step_hz,
        "fitted_delay_ns": float(delay_ns),
        "linear_phase_residual_rms_deg": float(np.sqrt(np.mean(np.square(residual_deg)))),
        "linear_phase_residual_peak_deg": float(np.max(np.abs(residual_deg))),
        "selected_magnitude_mean_db": float(np.mean(selected_db)),
        "selected_magnitude_min_db": float(np.min(selected_db)),
        "selected_magnitude_max_db": float(np.max(selected_db)),
        "selected_magnitude_peak_to_peak_db": float(np.ptp(selected_db)),
        "selected_vs_all_off_mean_db": float(np.mean(paired_contrast_db)),
        "selected_vs_all_off_min_db": float(np.min(paired_contrast_db)),
        "minimum_pilot_coherence": float(
            min(
                observation["analysis"]["pilot"]["phase_step_coherence"]
                for observation in quality_observations
                if observation.get("analysis") is not None
            )
        ),
        "maximum_rx1_peak_counts": float(
            max(
                observation["analysis"]["peak_component_counts"][0]
                for observation in quality_observations
                if observation.get("analysis") is not None
            )
        ),
        "maximum_rx2_peak_counts": float(
            max(
                observation["analysis"]["peak_component_counts"][1]
                for observation in quality_observations
                if observation.get("analysis") is not None
            )
        ),
        "analysis_error_count": sum(
            observation.get("analysis_error") is not None for observation in quality_observations
        ),
        "final_safety_passed": safety_passed,
        "tx_gain_db": configuration.get("tx_gain_db"),
    }

    output_path = args.output or args.run.with_name(f"{args.state.lower()}-direct-transfer.png")
    summary_path = args.summary or args.run.with_name("analysis-summary.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    frequency_ghz = frequencies_hz / 1e9
    all_off_frequency_ghz = all_off_frequencies_hz / 1e9
    figure, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True, constrained_layout=True)
    axes[0].plot(frequency_ghz, selected_db, "o-", label=args.state)
    axes[0].plot(all_off_frequency_ghz, all_off_db, "o-", alpha=0.75, label="ALL_OFF")
    axes[0].set_ylabel("RX2/RX1 magnitude (dB)")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    axes[1].plot(frequency_ghz, np.rad2deg(phase_rad), "o", label="measured")
    axes[1].plot(
        frequency_ghz,
        np.rad2deg(fitted_phase_rad),
        "-",
        label=f"delay fit: {delay_ns:+.3f} ns",
    )
    axes[1].set_ylabel("Unwrapped phase (degrees)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()

    axes[2].axhline(0.0, color="black", linewidth=1)
    axes[2].plot(frequency_ghz, residual_deg, "o-")
    axes[2].set_xlabel("RF frequency (GHz)")
    axes[2].set_ylabel("Delay-fit residual (degrees)")
    axes[2].grid(True, alpha=0.3)
    axes[2].set_title(
        f"RMS {summary['linear_phase_residual_rms_deg']:.1f}°, "
        f"peak {summary['linear_phase_residual_peak_deg']:.1f}°"
    )

    figure.suptitle(f"Direct PCB injection: {args.state}, {step_hz / 1e6:g} MHz grid")
    figure.savefig(output_path, dpi=180)
    plt.close(figure)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"plot={output_path}")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
