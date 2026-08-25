#!/usr/bin/env python3
"""Calibrate phase offsets and localize one emitter from accepted analyses."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.localization import (
    calibrate_channel_phases,
    estimate_phase_position,
    load_antenna_positions,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, nargs="+", required=True)
    parser.add_argument("--target", type=Path, nargs="+", required=True)
    parser.add_argument("--target-name", required=True)
    parser.add_argument("--calibration-x-mm", type=float, default=65.0)
    parser.add_argument("--calibration-y-mm", type=float, default=385.0)
    parser.add_argument(
        "--geometry",
        type=Path,
        default=Path("profiles/phase20-v1/array_geometry.json"),
    )
    parser.add_argument(
        "--bounds-mm",
        type=float,
        nargs=4,
        metavar=("X0", "X1", "Y0", "Y1"),
        default=(-500.0, 600.0, -500.0, 600.0),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _load(path: Path) -> tuple[dict[str, Any], npt.NDArray[np.float64]]:
    document = _mapping(json.loads(path.read_text(encoding="utf-8")), "analysis")
    if document.get("schema") != 2:
        raise ValueError(f"{path} is not an RX2 phase analysis schema 2 document")
    fft = _mapping(document.get("fft"), "fft")
    if fft.get("continuity_verified") is not True:
        raise ValueError(f"{path} does not prove capture continuity")
    if float(fft.get("alignment_confidence", 0.0)) < 0.9:
        raise ValueError(f"{path} selector alignment confidence is below 0.9")
    states = fft.get("states")
    if not isinstance(states, list) or len(states) != 8:
        raise ValueError(f"{path} does not contain eight states")
    names = [state.get("name") for state in states if isinstance(state, dict)]
    if names != [f"ANT{index}" for index in range(1, 9)]:
        raise ValueError(f"{path} states are not ordered ANT1 through ANT8")
    phases = np.asarray([float(state["phase_deg"]) for state in states], dtype=np.float64)
    return document, phases


def _circular_summary(
    paths: list[Path],
) -> tuple[list[dict[str, Any]], npt.NDArray[np.float64], npt.NDArray[np.float64], float]:
    documents = []
    phase_rows = []
    frequencies = []
    for path in paths:
        document, phases = _load(path)
        documents.append(document)
        phase_rows.append(phases)
        frequencies.append(
            float(document["center_frequency_hz"]) + float(document["tone_offset_hz"])
        )
    if max(frequencies) - min(frequencies) > 1.0:
        raise ValueError("all calibration and target analyses must use one RF frequency")
    radians = np.deg2rad(np.asarray(phase_rows, dtype=np.float64))
    unit_mean = np.mean(np.exp(1j * radians), axis=0)
    mean_deg = np.rad2deg(np.angle(unit_mean))
    circular_std_deg = np.rad2deg(
        np.sqrt(np.maximum(0.0, -2.0 * np.log(np.maximum(np.abs(unit_mean), 1e-15))))
    )
    return documents, mean_deg, circular_std_deg, float(np.mean(frequencies))


def main() -> int:
    args = _parser().parse_args()
    calibration_docs, calibration_phase, calibration_std, calibration_frequency = (
        _circular_summary(args.calibration)
    )
    target_docs, target_phase, target_std, target_frequency = _circular_summary(args.target)
    if abs(calibration_frequency - target_frequency) > 1.0:
        raise ValueError("calibration and target RF frequencies differ")
    antennas = load_antenna_positions(args.geometry)
    calibration = calibrate_channel_phases(
        calibration_phase,
        frequency_hz=calibration_frequency,
        antenna_positions_mm=antennas,
        calibration_position_mm=(args.calibration_x_mm, args.calibration_y_mm),
    )
    candidates = estimate_phase_position(
        target_phase,
        calibration,
        bounds_mm=tuple(args.bounds_mm),
        grid_step_mm=5.0,
        maximum_candidates=8,
        candidate_separation_mm=20.0,
    )
    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document = {
        "schema": 1,
        "source_commit": source_commit,
        "target_name": args.target_name,
        "rf_frequency_hz": calibration_frequency,
        "geometry": str(args.geometry.resolve()),
        "calibration": {
            "position_mm": [args.calibration_x_mm, args.calibration_y_mm],
            "artifact_ids": [item["artifact_id"] for item in calibration_docs],
            "mean_phase_deg": calibration_phase.tolist(),
            "phase_circular_std_deg": calibration_std.tolist(),
            "channel_offset_deg": calibration.channel_offset_deg.tolist(),
        },
        "target": {
            "artifact_ids": [item["artifact_id"] for item in target_docs],
            "mean_phase_deg": target_phase.tolist(),
            "phase_circular_std_deg": target_std.tolist(),
        },
        "search_bounds_mm": list(args.bounds_mm),
        "candidates": [
            {
                "x_mm": candidate.x_mm,
                "y_mm": candidate.y_mm,
                "rms_phase_error_deg": candidate.rms_phase_error_deg,
            }
            for candidate in candidates
        ],
    }
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"target": args.target_name, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
