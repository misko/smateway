#!/usr/bin/env python3
"""Evaluate direct-injection LUT interpolation on measured midpoint holdouts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-run", type=Path, action="append", required=True)
    parser.add_argument("--holdout-run", type=Path, required=True)
    parser.add_argument("--state", required=True, choices=tuple(f"ANT{i}" for i in range(1, 9)))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    return parser


def _load(paths: list[Path], state: str) -> tuple[dict[int, complex], list[dict[str, Any]]]:
    transfers: dict[int, complex] = {}
    documents: list[dict[str, Any]] = []
    for path in paths:
        document = json.loads(path.read_text())
        documents.append(document)
        for observation in document.get("observations", []):
            if observation.get("state") != state or observation.get("analysis_error") is not None:
                continue
            frequency_hz = int(observation["frequency_hz"])
            if frequency_hz in transfers:
                raise ValueError(f"duplicate training frequency: {frequency_hz}")
            transfer = observation["analysis"]["transfer_rx2_over_rx1"]
            transfers[frequency_hz] = complex(float(transfer["real"]), float(transfer["imag"]))
    return transfers, documents


def _pchip_midpoint(values: np.ndarray, spacing: float) -> np.ndarray:
    delta = np.diff(values) / spacing
    derivatives = np.zeros_like(values)
    same_sign = delta[:-1] * delta[1:] > 0.0
    derivatives[1:-1][same_sign] = 2.0 / (
        1.0 / delta[:-1][same_sign] + 1.0 / delta[1:][same_sign]
    )

    first = (3.0 * delta[0] - delta[1]) / 2.0
    if first * delta[0] <= 0.0:
        first = 0.0
    elif delta[0] * delta[1] < 0.0 and abs(first) > 3.0 * abs(delta[0]):
        first = 3.0 * delta[0]
    last = (3.0 * delta[-1] - delta[-2]) / 2.0
    if last * delta[-1] <= 0.0:
        last = 0.0
    elif delta[-1] * delta[-2] < 0.0 and abs(last) > 3.0 * abs(delta[-1]):
        last = 3.0 * delta[-1]
    derivatives[0] = first
    derivatives[-1] = last

    return (
        0.5 * values[:-1]
        + 0.125 * spacing * derivatives[:-1]
        + 0.5 * values[1:]
        - 0.125 * spacing * derivatives[1:]
    )


def _phase_error_and_affine_residual(
    prediction: np.ndarray,
    actual: np.ndarray,
    frequency_hz: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    phase_error_deg = np.rad2deg(np.unwrap(np.angle(prediction / actual)))
    centered_frequency_ghz = (frequency_hz - np.mean(frequency_hz)) / 1e9
    design = np.column_stack((np.ones_like(centered_frequency_ghz), centered_frequency_ghz))
    offset_deg, slope_deg_per_ghz = np.linalg.lstsq(
        design, phase_error_deg, rcond=None
    )[0]
    residual_deg = phase_error_deg - design @ np.asarray(
        [offset_deg, slope_deg_per_ghz]
    )
    return (
        phase_error_deg,
        residual_deg,
        float(offset_deg),
        float(slope_deg_per_ghz),
    )


def _metrics(
    prediction: np.ndarray,
    actual: np.ndarray,
    frequency_hz: np.ndarray,
) -> dict[str, float]:
    phase_error_deg, detrended_phase_error_deg, offset_deg, slope_deg_per_ghz = (
        _phase_error_and_affine_residual(prediction, actual, frequency_hz)
    )
    magnitude_error_db = 20.0 * np.log10(np.abs(prediction) / np.abs(actual))
    return {
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(phase_error_deg)))),
        "phase_p95_abs_deg": float(np.percentile(np.abs(phase_error_deg), 95.0)),
        "phase_max_abs_deg": float(np.max(np.abs(phase_error_deg))),
        "phase_affine_fit_offset_at_band_center_deg": offset_deg,
        "phase_affine_fit_slope_deg_per_ghz": slope_deg_per_ghz,
        "phase_affine_fit_delay_delta_ns": -slope_deg_per_ghz / 360.0,
        "phase_affine_detrended_rms_deg": float(
            np.sqrt(np.mean(np.square(detrended_phase_error_deg)))
        ),
        "phase_affine_detrended_p95_abs_deg": float(
            np.percentile(np.abs(detrended_phase_error_deg), 95.0)
        ),
        "phase_affine_detrended_max_abs_deg": float(
            np.max(np.abs(detrended_phase_error_deg))
        ),
        "magnitude_rms_db": float(np.sqrt(np.mean(np.square(magnitude_error_db)))),
        "magnitude_p95_abs_db": float(np.percentile(np.abs(magnitude_error_db), 95.0)),
        "magnitude_max_abs_db": float(np.max(np.abs(magnitude_error_db))),
    }


def _safe(document: dict[str, Any]) -> bool:
    return bool(
        document.get("error") is None
        and document.get("final_radio_mute", {}).get("passed") is True
        and document.get("final_source_radio_mute", {}).get("passed") is True
        and document.get("final_selector", {}).get("applied_code") == 8
        and document.get("final_selector", {}).get("lease_active") is False
    )


def main() -> int:
    args = _parser().parse_args()
    training, training_documents = _load(args.training_run, args.state)
    holdout, holdout_documents = _load([args.holdout_run], args.state)
    training_frequency_hz = np.asarray(sorted(training), dtype=np.float64)
    holdout_frequency_hz = np.asarray(sorted(holdout), dtype=np.float64)
    training_transfer = np.asarray(
        [training[int(frequency)] for frequency in training_frequency_hz], dtype=np.complex128
    )
    holdout_transfer = np.asarray(
        [holdout[int(frequency)] for frequency in holdout_frequency_hz], dtype=np.complex128
    )
    spacing = np.diff(training_frequency_hz)
    if training_frequency_hz.size < 4 or not np.allclose(spacing, spacing[0]):
        raise ValueError("training grid must contain at least four uniformly spaced frequencies")
    expected_midpoints = (training_frequency_hz[:-1] + training_frequency_hz[1:]) / 2.0
    within_holdout_band = (expected_midpoints >= holdout_frequency_hz[0]) & (
        expected_midpoints <= holdout_frequency_hz[-1]
    )
    indices = np.flatnonzero(within_holdout_band)
    expected_midpoints = expected_midpoints[indices]
    if not np.array_equal(expected_midpoints, holdout_frequency_hz):
        raise ValueError("holdout frequencies must be exact training-grid midpoints")

    magnitude_db = 20.0 * np.log10(np.abs(training_transfer))
    phase_rad = np.unwrap(np.angle(training_transfer))
    linear_complex = (training_transfer[:-1] + training_transfer[1:]) / 2.0
    linear_logphase = 10.0 ** (
        ((magnitude_db[:-1] + magnitude_db[1:]) / 2.0) / 20.0
    ) * np.exp(1j * (phase_rad[:-1] + phase_rad[1:]) / 2.0)
    pchip_logphase = 10.0 ** (
        _pchip_midpoint(magnitude_db, spacing[0]) / 20.0
    ) * np.exp(1j * _pchip_midpoint(phase_rad, spacing[0]))
    predictions = {
        "complex_linear": linear_complex[indices],
        "logphase_linear": linear_logphase[indices],
        "logphase_pchip": pchip_logphase[indices],
    }
    metrics = {
        name: _metrics(value, holdout_transfer, holdout_frequency_hz)
        for name, value in predictions.items()
    }

    summary = {
        "schema": 1,
        "selected_state": args.state,
        "training_frequency_count": int(training_frequency_hz.size),
        "holdout_frequency_count": int(holdout_frequency_hz.size),
        "training_step_hz": int(spacing[0]),
        "holdout_first_hz": int(holdout_frequency_hz[0]),
        "holdout_last_hz": int(holdout_frequency_hz[-1]),
        "methods": metrics,
        "best_phase_rms_method": min(metrics, key=lambda name: metrics[name]["phase_rms_deg"]),
        "final_safety_passed": all(
            _safe(document) for document in [*training_documents, *holdout_documents]
        ),
    }

    output = args.output or args.holdout_run.with_name("interpolation-holdout.png")
    summary_path = args.summary or args.holdout_run.with_name("interpolation-holdout.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    frequency_ghz = holdout_frequency_hz / 1e9
    figure, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True, constrained_layout=True)
    axes[0].plot(training_frequency_hz / 1e9, magnitude_db, ".-", label="training")
    axes[0].plot(frequency_ghz, 20.0 * np.log10(np.abs(holdout_transfer)), ".", label="holdout")
    axes[0].set_ylabel("RX2/RX1 magnitude (dB)")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    for name, prediction in predictions.items():
        axes[1].plot(
            frequency_ghz,
            np.rad2deg(np.angle(prediction / holdout_transfer)),
            ".-",
            label=name,
        )
        axes[2].plot(
            frequency_ghz,
            20.0 * np.log10(np.abs(prediction) / np.abs(holdout_transfer)),
            ".-",
            label=name,
        )
    _, pchip_detrended_deg, _, _ = _phase_error_and_affine_residual(
        predictions["logphase_pchip"], holdout_transfer, holdout_frequency_hz
    )
    axes[1].plot(
        frequency_ghz,
        pchip_detrended_deg,
        ".--",
        linewidth=1,
        label="logphase_pchip (affine drift removed)",
    )
    axes[1].axhline(0.0, color="black", linewidth=1)
    axes[1].set_ylabel("Phase prediction error (degrees)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()
    axes[2].axhline(0.0, color="black", linewidth=1)
    axes[2].set_ylabel("Magnitude prediction error (dB)")
    axes[2].set_xlabel("RF frequency (GHz)")
    axes[2].grid(True, alpha=0.3)
    figure.suptitle(
        f"{args.state} midpoint holdout: {spacing[0] / 1e6:g} MHz training grid"
    )
    figure.savefig(output, dpi=180)
    plt.close(figure)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"plot={output}")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
