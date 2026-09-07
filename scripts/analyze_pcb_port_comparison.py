#!/usr/bin/env python3
"""Compare two direct-injection PCB paths on their common frequency grid."""

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
    parser.add_argument("--reference-run", type=Path, action="append", required=True)
    parser.add_argument("--reference-state", required=True)
    parser.add_argument("--target-run", type=Path, action="append", required=True)
    parser.add_argument("--target-state", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
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
                raise ValueError(f"duplicate {state} frequency: {frequency_hz}")
            transfer = observation["analysis"]["transfer_rx2_over_rx1"]
            transfers[frequency_hz] = complex(float(transfer["real"]), float(transfer["imag"]))
    if not transfers:
        raise ValueError(f"no valid {state} observations")
    return transfers, documents


def _safe(document: dict[str, Any]) -> bool:
    return bool(
        document.get("error") is None
        and document.get("final_radio_mute", {}).get("passed") is True
        and document.get("final_source_radio_mute", {}).get("passed") is True
        and document.get("final_selector", {}).get("applied_code") == 8
        and document.get("final_selector", {}).get("lease_active") is False
    )


def _fit(frequency_hz: np.ndarray, transfer: np.ndarray) -> tuple[dict[str, float], np.ndarray]:
    phase_rad = np.unwrap(np.angle(transfer))
    centered_frequency_hz = frequency_hz - np.mean(frequency_hz)
    design = np.column_stack((np.ones_like(centered_frequency_hz), centered_frequency_hz))
    intercept_rad, slope_rad_per_hz = np.linalg.lstsq(design, phase_rad, rcond=None)[0]
    fit_rad = design @ np.asarray([intercept_rad, slope_rad_per_hz])
    residual_deg = np.rad2deg(phase_rad - fit_rad)
    magnitude_db = 20.0 * np.log10(np.abs(transfer))
    metrics = {
        "delay_ns": float(-slope_rad_per_hz / (2.0 * math.pi) * 1e9),
        "phase_at_band_center_deg": float(np.rad2deg(intercept_rad)),
        "delay_residual_rms_deg": float(np.sqrt(np.mean(np.square(residual_deg)))),
        "delay_residual_peak_deg": float(np.max(np.abs(residual_deg))),
        "magnitude_mean_db": float(np.mean(magnitude_db)),
        "magnitude_min_db": float(np.min(magnitude_db)),
        "magnitude_max_db": float(np.max(magnitude_db)),
        "magnitude_peak_to_peak_db": float(np.ptp(magnitude_db)),
    }
    return metrics, fit_rad


def _band_metrics(
    frequency_hz: np.ndarray,
    transfer: np.ndarray,
    first_hz: float,
    last_hz: float,
) -> dict[str, float | int] | None:
    selected = (frequency_hz >= first_hz) & (frequency_hz <= last_hz)
    if int(np.sum(selected)) < 4:
        return None
    metrics, _ = _fit(frequency_hz[selected], transfer[selected])
    return {"frequency_count": int(np.sum(selected)), **metrics}


def main() -> int:
    args = _parser().parse_args()
    reference, reference_documents = _load(args.reference_run, args.reference_state)
    target, target_documents = _load(args.target_run, args.target_state)
    common = sorted(set(reference) & set(target))
    if len(common) < 4:
        raise ValueError("paths have fewer than four common frequencies")
    frequency_hz = np.asarray(common, dtype=np.float64)
    steps_hz = np.diff(frequency_hz)
    if not np.allclose(steps_hz, steps_hz[0]):
        raise ValueError("common frequency grid is not uniform")
    reference_transfer = np.asarray([reference[value] for value in common])
    target_transfer = np.asarray([target[value] for value in common])
    relative_transfer = target_transfer / reference_transfer
    reference_metrics, _ = _fit(frequency_hz, reference_transfer)
    target_metrics, _ = _fit(frequency_hz, target_transfer)
    relative_metrics, relative_fit_rad = _fit(frequency_hz, relative_transfer)
    relative_phase_rad = np.unwrap(np.angle(relative_transfer))
    relative_residual_deg = np.rad2deg(relative_phase_rad - relative_fit_rad)

    bands = {
        "0p5_to_5_ghz": _band_metrics(frequency_hz, relative_transfer, 0.5e9, 5.0e9),
        "5_to_6_ghz": _band_metrics(frequency_hz, relative_transfer, 5.0e9, 6.0e9),
        "5p7_to_5p9_ghz": _band_metrics(frequency_hz, relative_transfer, 5.7e9, 5.9e9),
    }
    summary = {
        "schema": 1,
        "reference_state": args.reference_state,
        "target_state": args.target_state,
        "frequency_count": len(common),
        "first_frequency_hz": common[0],
        "last_frequency_hz": common[-1],
        "frequency_step_hz": int(steps_hz[0]),
        "reference": reference_metrics,
        "target": target_metrics,
        "relative_target_over_reference": relative_metrics,
        "relative_bands": bands,
        "final_safety_passed": all(
            _safe(document)
            for document in [*reference_documents, *target_documents]
        ),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    frequency_ghz = frequency_hz / 1e9
    reference_db = 20.0 * np.log10(np.abs(reference_transfer))
    target_db = 20.0 * np.log10(np.abs(target_transfer))
    relative_db = 20.0 * np.log10(np.abs(relative_transfer))
    figure, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True, constrained_layout=True)
    axes[0].plot(frequency_ghz, reference_db, label=args.reference_state)
    axes[0].plot(frequency_ghz, target_db, label=args.target_state)
    axes[0].set_ylabel("RX2/RX1 magnitude (dB)")
    axes[0].legend()
    axes[1].plot(frequency_ghz, relative_db)
    axes[1].axhline(0.0, color="black", linewidth=1)
    axes[1].set_ylabel(f"{args.target_state}/{args.reference_state} (dB)")
    axes[2].plot(frequency_ghz, np.rad2deg(relative_phase_rad), label="measured")
    axes[2].plot(frequency_ghz, np.rad2deg(relative_fit_rad), color="black", label="delay fit")
    axes[2].set_ylabel("Relative unwrapped phase (deg)")
    axes[2].legend()
    axes[3].plot(frequency_ghz, relative_residual_deg)
    axes[3].axhline(0.0, color="black", linewidth=1)
    axes[3].set_ylabel("Relative delay residual (deg)")
    axes[3].set_xlabel("RF frequency (GHz)")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    figure.suptitle(
        f"PCB direct-injection comparison: {args.target_state} / {args.reference_state}"
    )
    figure.savefig(args.output, dpi=180)
    plt.close(figure)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"plot={args.output}")
    print(f"summary={args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
