#!/usr/bin/env python3
"""Analyze the conducted 2.1–5.8 GHz permutation sweep into board corrections."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import analyze_closed_loop_permutation as base  # type: ignore[import-not-found]
import numpy as np
import numpy.typing as npt

FREQUENCIES_HZ = tuple(range(2_100_000_000, 5_800_000_001, 100_000_000))
CLOSURE_FREQUENCIES_HZ = (
    2_100_000_000,
    2_400_000_000,
    3_000_000_000,
    4_000_000_000,
    5_000_000_000,
    5_800_000_000,
)
ROTATION_STAGES = ("rotation0", "rotation1", "rotation2")
ALL_STAGES = (*ROTATION_STAGES, "closure0")
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
SPEED_OF_LIGHT_MM_PER_PS = 0.299792458


class SweepAnalysisError(RuntimeError):
    """The persisted sweep cannot support a traceable broadband calibration."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--correction-csv", type=Path)
    parser.add_argument("--figure-directory", type=Path)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SweepAnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise SweepAnalysisError(f"{label} must be an array")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SweepAnalysisError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SweepAnalysisError(f"{label} must be a nonempty string")
    return value


def _wrap_phase_deg(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), "sweep manifest")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SweepAnalysisError(f"cannot load sweep manifest {path}: {error}") from error


def _completed_attempts_by_plan_index(
    manifest: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]]:
    completed: dict[int, Mapping[str, Any]] = {}
    for raw in _sequence(manifest.get("attempts"), "attempts"):
        attempt = _mapping(raw, "attempt")
        if attempt.get("status") != "complete":
            continue
        plan_index = _integer(attempt.get("plan_index"), "attempt plan index")
        if plan_index in completed:
            raise SweepAnalysisError(
                f"multiple complete attempts exist for plan index {plan_index}"
            )
        completed[plan_index] = attempt
    return completed


def _canonical_manifest(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if manifest.get("schema") != 1:
        raise SweepAnalysisError("sweep manifest schema must be 1")
    if manifest.get("experiment_kind") != "fast20_fully_conducted_broadband_board_calibration":
        raise SweepAnalysisError("manifest is not a conducted broadband board calibration")
    configuration = _mapping(manifest.get("configuration"), "configuration")
    if tuple(_sequence(configuration.get("frequencies_hz"), "frequency grid")) != FREQUENCIES_HZ:
        raise SweepAnalysisError("frequency grid is not exactly 2.1–5.8 GHz in 100 MHz steps")
    if tuple(_sequence(configuration.get("closure_frequencies_hz"), "closure grid")) != (
        CLOSURE_FREQUENCIES_HZ
    ):
        raise SweepAnalysisError("closure grid differs from the predeclared sentinels")
    if configuration.get("storage_medium") != "raspberry_pi_local_filesystem":
        raise SweepAnalysisError("sweep does not attest Raspberry Pi local storage")
    if configuration.get("pluto_onboard_storage_used") is not False:
        raise SweepAnalysisError("sweep does not exclude Pluto onboard storage")

    plan_rows: dict[tuple[str, int], Mapping[str, Any]] = {}
    plan_by_index: dict[int, Mapping[str, Any]] = {}
    for raw in _sequence(manifest.get("plan"), "plan"):
        condition = _mapping(raw, "plan condition")
        plan_index = _integer(condition.get("plan_index"), "plan index")
        stage = _string(condition.get("stage"), "plan stage")
        frequency_hz = _integer(condition.get("center_frequency_hz"), "plan frequency")
        key = (stage, frequency_hz)
        if stage not in ALL_STAGES or key in plan_rows or plan_index in plan_by_index:
            raise SweepAnalysisError("plan contains an unsupported or duplicate condition")
        plan_rows[key] = condition
        plan_by_index[plan_index] = condition

    expected_keys = {
        *((stage, frequency_hz) for stage in ROTATION_STAGES for frequency_hz in FREQUENCIES_HZ),
        *(("closure0", frequency_hz) for frequency_hz in CLOSURE_FREQUENCIES_HZ),
    }
    if set(plan_rows) != expected_keys or set(plan_by_index) != set(range(len(expected_keys))):
        raise SweepAnalysisError("plan is not the exact predeclared 120-condition sweep")

    completed = _completed_attempts_by_plan_index(manifest)
    artifacts: dict[tuple[str, int], str] = {}
    rejected: dict[tuple[str, int], str] = {}
    for key, condition in plan_rows.items():
        plan_index = _integer(condition.get("plan_index"), "plan index")
        attempt = completed.get(plan_index)
        if attempt is None:
            rejected[key] = "capture_not_complete"
            continue
        if any(attempt.get(name) != value for name, value in condition.items()):
            raise SweepAnalysisError(f"attempt {plan_index} differs from its frozen condition")
        quality = _mapping(attempt.get("quality_result"), "quality result")
        if quality.get("quality_passed") is not True or attempt.get("outcome") != "quality_passed":
            rejected[key] = "capture_quality_rejected"
            continue
        artifact_id = _string(attempt.get("artifact_id"), "artifact ID")
        if ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise SweepAnalysisError(f"attempt {plan_index} has a malformed artifact ID")
        artifacts[key] = artifact_id

    usable_frequencies = tuple(
        frequency_hz
        for frequency_hz in FREQUENCIES_HZ
        if all((stage, frequency_hz) in artifacts for stage in ROTATION_STAGES)
    )
    if not usable_frequencies:
        raise SweepAnalysisError("no frequency has three accepted permutation captures")

    excluded = []
    for frequency_hz in FREQUENCIES_HZ:
        reasons = {
            stage: rejected.get((stage, frequency_hz), "accepted") for stage in ROTATION_STAGES
        }
        if any(value != "accepted" for value in reasons.values()):
            excluded.append({"center_frequency_hz": frequency_hz, "stages": reasons})

    rotations = []
    for rotation, stage in enumerate(ROTATION_STAGES):
        rotations.append(
            {
                "rotation": rotation,
                "mapping": plan_rows[(stage, FREQUENCIES_HZ[0])]["mapping"],
                "artifacts_by_frequency_hz": {
                    str(frequency_hz): artifacts[(stage, frequency_hz)]
                    for frequency_hz in usable_frequencies
                },
            }
        )
    closure_artifacts = {
        str(frequency_hz): artifacts[("closure0", frequency_hz)]
        for frequency_hz in CLOSURE_FREQUENCIES_HZ
        if frequency_hz in usable_frequencies and ("closure0", frequency_hz) in artifacts
    }
    canonical = {
        "schema": 1,
        "run_id": _string(manifest.get("run_id"), "run ID"),
        "board_id": _string(configuration.get("board_id"), "board ID"),
        "pluto_serial": _string(configuration.get("serial"), "Pluto serial"),
        "profile_id": _string(configuration.get("profile_id"), "profile ID"),
        "profile_contract_sha256": _string(
            configuration.get("profile_contract_sha256"), "profile contract SHA-256"
        ),
        "firmware_binary_sha256": _string(
            configuration.get("firmware_binary_sha256"), "firmware SHA-256"
        ),
        "frequencies_hz": list(usable_frequencies),
        "rounds": rotations,
        "closure": {
            "rotation": 0,
            "mapping": plan_rows[("closure0", CLOSURE_FREQUENCIES_HZ[0])]["mapping"],
            "artifacts_by_frequency_hz": closure_artifacts,
        },
    }
    coverage = {
        "requested_frequency_count": len(FREQUENCIES_HZ),
        "usable_frequency_count": len(usable_frequencies),
        "usable_frequencies_hz": list(usable_frequencies),
        "excluded_frequencies": excluded,
        "closure_sentinel_count": len(CLOSURE_FREQUENCIES_HZ),
        "accepted_closure_frequencies_hz": [int(value) for value in closure_artifacts],
        "all_requested_frequencies_usable": len(usable_frequencies) == len(FREQUENCIES_HZ),
    }
    return canonical, coverage


def _continuity_branches(
    phases_deg: npt.NDArray[np.float64], frequencies_hz: Sequence[int]
) -> tuple[npt.NDArray[np.float64], list[int]]:
    """Resolve frequency-to-frequency cyclic branch changes while anchoring 2.4 GHz."""

    if phases_deg.ndim != 2 or phases_deg.shape[1] != 8:
        raise SweepAnalysisError("phase correction matrix must have eight antenna columns")
    if phases_deg.shape[0] != len(frequencies_hz):
        raise SweepAnalysisError("phase correction rows and frequencies differ")
    options = np.arange(8, dtype=np.float64)[:, None] * 45.0 * np.arange(8)[None, :]
    candidates = np.asarray(
        [
            [[_wrap_phase_deg(value) for value in phases_deg[row] + option] for option in options]
            for row in range(phases_deg.shape[0])
        ],
        dtype=np.float64,
    )
    anchor = min(range(len(frequencies_hz)), key=lambda index: abs(frequencies_hz[index] - 2.4e9))
    selected = [0] * len(frequencies_hz)
    for direction in (1, -1):
        index = anchor + direction
        previous = anchor
        while 0 <= index < len(frequencies_hz):
            costs = []
            for branch in range(8):
                delta = np.asarray(
                    [
                        _wrap_phase_deg(value)
                        for value in candidates[index, branch]
                        - candidates[previous, selected[previous]]
                    ]
                )
                costs.append(float(np.dot(delta, delta)))
            selected[index] = min(range(8), key=costs.__getitem__)
            previous = index
            index += direction
    adjusted = np.asarray(
        [candidates[index, branch] for index, branch in enumerate(selected)], dtype=np.float64
    )
    return adjusted, selected


def _fit_single_delay(
    frequencies_hz: npt.NDArray[np.float64], phases_deg: npt.NDArray[np.float64]
) -> dict[str, float]:
    unwrapped = np.unwrap(np.deg2rad(phases_deg))
    centered = frequencies_hz - float(np.mean(frequencies_hz))
    design = np.column_stack((np.ones(frequencies_hz.size), centered))
    intercept_rad, slope_rad_per_hz = np.linalg.lstsq(design, unwrapped, rcond=None)[0]
    prediction = design @ np.asarray((intercept_rad, slope_rad_per_hz))
    residual_deg = np.rad2deg(unwrapped - prediction)
    delay_ps = float(slope_rad_per_hz / (2.0 * math.pi) * 1e12)
    return {
        "correction_equivalent_delay_ps": delay_ps,
        "correction_equivalent_free_space_path_mm": delay_ps * SPEED_OF_LIGHT_MM_PER_PS,
        "phase_intercept_at_mean_frequency_deg": _wrap_phase_deg(math.degrees(intercept_rad)),
        "phase_residual_rms_deg": float(math.sqrt(float(np.mean(np.square(residual_deg))))),
        "phase_residual_max_abs_deg": float(np.max(np.abs(residual_deg))),
    }


def _add_broadband_products(document: dict[str, Any]) -> None:
    rows = tuple(
        _mapping(item, "frequency result")
        for item in _sequence(document.get("frequency_results"), "frequency results")
    )
    frequencies = np.asarray(
        [_integer(row.get("center_frequency_hz"), "frequency") for row in rows],
        dtype=np.float64,
    )
    raw_phases = []
    gains = []
    for row in rows:
        model = _mapping(row.get("separable_model"), "separable model")
        paths = tuple(
            _mapping(item, "board path")
            for item in _sequence(model.get("board_path_terms"), "board paths")
        )
        raw_phases.append([float(item["correction_phase_deg"]) for item in paths])
        gains.append([float(item["correction_gain_db"]) for item in paths])
    adjusted, branches = _continuity_branches(np.asarray(raw_phases), frequencies.astype(int))
    correction_rows = []
    for index, frequency_hz in enumerate(frequencies.astype(int)):
        correction_rows.append(
            {
                "center_frequency_hz": int(frequency_hz),
                "cyclic_branch_delta": branches[index],
                "antennas": [
                    {
                        "name": f"ANT{antenna + 1}",
                        "correction_gain_db": gains[index][antenna],
                        "raw_correction_phase_deg": raw_phases[index][antenna],
                        "continuity_adjusted_correction_phase_deg": float(adjusted[index, antenna]),
                    }
                    for antenna in range(8)
                ],
            }
        )
    delay_models = [
        {
            "name": f"ANT{antenna + 1}",
            **_fit_single_delay(frequencies, adjusted[:, antenna]),
        }
        for antenna in range(8)
    ]
    document["broadband_correction_table"] = correction_rows
    document["frequency_continuity_branch_resolution"] = {
        "anchor_frequency_hz": 2_400_000_000,
        "anchor_branch_delta": 0,
        "branch_delta_by_frequency": branches,
        "remaining_absolute_ambiguity": (
            "the constant eight-way 45-degree spatial-ramp ambiguity remains anchored to the "
            "existing 2.4 GHz minimum-reconnect-common-phase prior"
        ),
    }
    document["single_delay_diagnostic"] = {
        "model": "correction_phase = intercept + 360 * frequency * relative_delay",
        "sign_convention": (
            "positive correction-equivalent delay means correction phase increases with frequency; "
            "the corresponding measured board response delay has the same physical magnitude"
        ),
        "antenna_models": delay_models,
    }


def build_analysis(manifest_path: Path, artifact_root: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    canonical, coverage = _canonical_manifest(manifest)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as stream:
        json.dump(canonical, stream)
        stream.flush()
        document = base.build_analysis(Path(stream.name), artifact_root)
    document["analysis_kind"] = "fast20_conducted_broadband_board_calibration"
    document["source_manifest"] = {
        "path": str(manifest_path.resolve()),
        "sha256": base._sha256(manifest_path),
        "runner_source_commit": manifest.get("runner_source_commit"),
    }
    document["sweep_coverage"] = coverage
    _add_broadband_products(document)
    return document


def _write_csv(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            (
                "center_frequency_hz",
                "antenna",
                "correction_gain_db",
                "correction_phase_deg",
                "cyclic_branch_delta",
            )
        )
        for raw in _sequence(document.get("broadband_correction_table"), "correction table"):
            row = _mapping(raw, "correction row")
            for antenna in _sequence(row.get("antennas"), "antenna corrections"):
                item = _mapping(antenna, "antenna correction")
                writer.writerow(
                    (
                        row["center_frequency_hz"],
                        item["name"],
                        item["correction_gain_db"],
                        item["continuity_adjusted_correction_phase_deg"],
                        row["cyclic_branch_delta"],
                    )
                )


def main() -> int:
    args = _parser().parse_args()
    manifest = _load_manifest(args.manifest)
    configuration = _mapping(manifest.get("configuration"), "configuration")
    artifact_root = (
        Path(_string(configuration.get("artifact_storage_root"), "artifact storage root"))
        if args.artifact_root is None
        else args.artifact_root
    )
    document = build_analysis(args.manifest, artifact_root)
    base._write_json_atomic(args.output, document)
    if args.correction_csv is not None:
        _write_csv(args.correction_csv, document)
    figures: Sequence[Path] = ()
    if args.figure_directory is not None:
        figures = base._render_figures(document, args.figure_directory)
    coverage = _mapping(document.get("sweep_coverage"), "sweep coverage")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "correction_csv": (
                    None if args.correction_csv is None else str(args.correction_csv)
                ),
                "figures": [str(path) for path in figures],
                "usable_frequency_count": coverage["usable_frequency_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
