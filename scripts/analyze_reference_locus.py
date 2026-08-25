#!/usr/bin/env python3
"""Infer the RX1 signed range-difference locus from paired TX transfer artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from math import atan2, isfinite, pi
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.reference_locus import (
    ReferenceLocusAnalysis,
    ReferenceLocusError,
    ReferenceTransferCapture,
    analyze_reference_locus,
)

REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_ANALYSIS_KIND = "fast20_dual_rx_ota_reference_transfer"
DEFAULT_GEOMETRY = REPOSITORY / "profiles/phase20-v1/array_geometry.json"
DEFAULT_BOUNDS_MM = (-750.0, 750.0, -750.0, 750.0)


class DocumentError(ReferenceLocusError):
    """An input JSON document violates the reference-locus contract."""


@dataclass(frozen=True, slots=True)
class LoadedCapture:
    capture: ReferenceTransferCapture
    created_at: datetime
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class PairingSummary:
    method: str
    maximum_pair_gap_s: float | None
    pair_count: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help=(
            "reference-transfer JSON or directory; repeat as needed (directories are "
            "searched recursively for fast20-reference-transfer.json)"
        ),
    )
    parser.add_argument(
        "--pairing-manifest",
        type=Path,
        help="optional Fast20 run manifest whose attempt round supplies the pair identity",
    )
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--tx1-x-mm", type=float, required=True)
    parser.add_argument("--tx1-y-mm", type=float, required=True)
    parser.add_argument("--tx2-x-mm", type=float, required=True)
    parser.add_argument("--tx2-y-mm", type=float, required=True)
    parser.add_argument("--x-min-mm", type=float, default=DEFAULT_BOUNDS_MM[0])
    parser.add_argument("--x-max-mm", type=float, default=DEFAULT_BOUNDS_MM[1])
    parser.add_argument("--y-min-mm", type=float, default=DEFAULT_BOUNDS_MM[2])
    parser.add_argument("--y-max-mm", type=float, default=DEFAULT_BOUNDS_MM[3])
    parser.add_argument("--minimum-pair-repeats", type=int, default=2)
    parser.add_argument("--minimum-pair-coherence", type=float, default=0.70)
    parser.add_argument("--minimum-valid-states", type=int, default=4)
    parser.add_argument("--minimum-state-coherence", type=float, default=0.50)
    parser.add_argument("--grid-step-mm", type=float, default=0.1)
    parser.add_argument("--systematic-phase-std-deg", type=float, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DocumentError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DocumentError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DocumentError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise DocumentError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DocumentError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DocumentError(f"{label} must be a non-empty string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DocumentError(f"{label} must be boolean")
    return value


def _complex(value: object, label: str) -> complex:
    document = _mapping(value, label)
    return complex(
        _number(document.get("real"), f"{label}.real"),
        _number(document.get("imag"), f"{label}.imag"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_datetime(value: object, label: str) -> datetime:
    text = _string(value, label)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise DocumentError(f"{label} is not an ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise DocumentError(f"{label} must include a timezone")
    return result.astimezone(UTC)


def _load_capture(path: Path) -> LoadedCapture:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentError(f"cannot load {path}: {error}") from error
    root = _mapping(document, str(path))
    if root.get("schema") != 1:
        raise DocumentError(f"{path}: analysis schema must be 1")
    if root.get("analysis_kind") != EXPECTED_ANALYSIS_KIND:
        raise DocumentError(f"{path}: input is not a dual-RX OTA reference transfer")
    artifact = _mapping(root.get("artifact"), f"{path}: artifact")
    artifact_id = _string(artifact.get("artifact_id"), f"{path}: artifact.artifact_id")
    created_at = _parse_datetime(artifact.get("created_at"), f"{path}: artifact.created_at")
    key = _mapping(root.get("aggregation_key"), f"{path}: aggregation_key")
    if _string(key.get("artifact_id"), f"{path}: aggregation_key.artifact_id") != artifact_id:
        raise DocumentError(f"{path}: aggregation artifact identity disagrees")
    tx_channel = _integer(key.get("tx_channel"), f"{path}: aggregation_key.tx_channel")
    carrier_frequency_hz = _number(
        key.get("carrier_frequency_hz"),
        f"{path}: aggregation_key.carrier_frequency_hz",
    )
    quality = _mapping(root.get("quality_gate"), f"{path}: quality_gate")
    global_quality = _boolean(quality.get("passed"), f"{path}: quality_gate.passed")
    transfer = _mapping(root.get("transfer"), f"{path}: transfer")
    raw_states = _sequence(transfer.get("states"), f"{path}: transfer.states")
    expected_names = tuple(f"ANT{index}" for index in range(1, 9))
    if len(raw_states) != len(expected_names):
        raise DocumentError(f"{path}: transfer must contain ANT1 through ANT8")
    phasors: list[complex] = []
    uncertainties: list[float] = []
    valid: list[bool] = []
    names: list[str] = []
    for index, raw_state in enumerate(raw_states):
        state = _mapping(raw_state, f"{path}: transfer.states[{index}]")
        name = _string(state.get("name"), f"{path}: transfer.states[{index}].name")
        names.append(name)
        reported = _mapping(
            state.get("all_off_subtracted_rx2_over_rx1"),
            f"{path}: {name}.all_off_subtracted_rx2_over_rx1",
        )
        phasors.append(_complex(reported.get("phasor"), f"{path}: {name}.phasor"))
        uncertainties.append(
            _number(
                state.get("transfer_approximate_phase_standard_error_deg"),
                f"{path}: {name}.transfer_approximate_phase_standard_error_deg",
            )
        )
        state_quality = _boolean(state.get("quality_passed"), f"{path}: {name}.quality")
        repeat_quality = _boolean(
            reported.get("repeat_quality_passed"),
            f"{path}: {name}.repeat_quality_passed",
        )
        valid.append(global_quality and state_quality and repeat_quality)
    if tuple(names) != expected_names:
        raise DocumentError(f"{path}: states must be ordered ANT1 through ANT8")
    embedded_pair_id = key.get("pair_id")
    if embedded_pair_id is None:
        pair_id = artifact_id
    else:
        pair_id = _string(embedded_pair_id, f"{path}: aggregation_key.pair_id")
    try:
        capture = ReferenceTransferCapture(
            artifact_id=artifact_id,
            pair_id=pair_id,
            tx_channel=tx_channel,
            carrier_frequency_hz=carrier_frequency_hz,
            state_names=tuple(names),
            transfer_phasor=np.asarray(phasors),
            phase_standard_error_deg=np.asarray(uncertainties),
            valid_mask=np.asarray(valid),
            global_quality_passed=global_quality,
        )
    except ValueError as error:
        raise DocumentError(f"{path}: invalid transfer capture: {error}") from error
    return LoadedCapture(capture=capture, created_at=created_at, path=path, sha256=_sha256(path))


def _input_paths(inputs: Sequence[Path]) -> tuple[Path, ...]:
    found: dict[Path, None] = {}
    for raw_path in inputs:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            found[path] = None
        elif path.is_dir():
            for candidate in sorted(path.rglob("fast20-reference-transfer.json")):
                found[candidate.resolve()] = None
        else:
            raise DocumentError(f"input path does not exist: {path}")
    if not found:
        raise DocumentError("no fast20-reference-transfer.json inputs were found")
    return tuple(found)


def _manifest_pair_ids(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentError(f"cannot load pairing manifest {path}: {error}") from error
    root = _mapping(document, str(path))
    if root.get("schema") != 1:
        raise DocumentError("pairing manifest schema must be 1")
    attempts = _sequence(root.get("attempts"), "pairing manifest attempts")
    result: dict[str, str] = {}
    for index, raw_attempt in enumerate(attempts):
        attempt = _mapping(raw_attempt, f"pairing manifest attempts[{index}]")
        artifact_id = attempt.get("artifact_id")
        round_index = attempt.get("round")
        if artifact_id is None or round_index is None or attempt.get("status") != "complete":
            continue
        artifact = _string(artifact_id, f"pairing manifest attempts[{index}].artifact_id")
        round_number = _integer(round_index, f"pairing manifest attempts[{index}].round")
        if artifact in result:
            raise DocumentError(f"artifact {artifact} appears twice in pairing manifest")
        result[artifact] = f"round-{round_number}"
    return result


def _assign_pair_ids(
    loaded: tuple[LoadedCapture, ...],
    *,
    manifest: Path | None,
) -> tuple[tuple[LoadedCapture, ...], PairingSummary]:
    if manifest is not None:
        identifiers = _manifest_pair_ids(manifest.expanduser().resolve())
        loaded_by_id = {item.capture.artifact_id: item for item in loaded}
        missing = sorted(set(identifiers) - set(loaded_by_id))
        if missing:
            raise DocumentError(
                f"{len(missing)} completed manifest artifacts have no reference-transfer JSON; "
                f"first missing artifact is {missing[0]}"
            )
        assigned: list[LoadedCapture] = []
        for artifact_id, pair_id in identifiers.items():
            item = loaded_by_id[artifact_id]
            assigned.append(replace(item, capture=replace(item.capture, pair_id=pair_id)))
        unique_pairs = {
            (item.capture.carrier_frequency_hz, item.capture.pair_id) for item in assigned
        }
        return tuple(assigned), PairingSummary("run_manifest_round", None, len(unique_pairs))

    embedded = [item.capture.pair_id != item.capture.artifact_id for item in loaded]
    if any(embedded):
        if not all(embedded):
            raise DocumentError("only some transfer artifacts contain an explicit pair_id")
        unique_pairs = {
            (item.capture.carrier_frequency_hz, item.capture.pair_id) for item in loaded
        }
        return loaded, PairingSummary("embedded_pair_id", None, len(unique_pairs))

    grouped: dict[tuple[float, int], list[LoadedCapture]] = {}
    for item in loaded:
        grouped.setdefault(
            (item.capture.carrier_frequency_hz, item.capture.tx_channel), []
        ).append(item)
    frequencies = sorted({frequency for frequency, _tx in grouped})
    assigned = []
    gaps: list[float] = []
    for frequency in frequencies:
        tx1_items = sorted(grouped.get((frequency, 0), []), key=lambda item: item.created_at)
        tx2_items = sorted(grouped.get((frequency, 1), []), key=lambda item: item.created_at)
        if len(tx1_items) != len(tx2_items) or not tx1_items:
            raise DocumentError(
                f"{frequency:.3f} Hz has {len(tx1_items)} TX1 and {len(tx2_items)} TX2 captures"
            )
        for repeat_index, (tx1_item, tx2_item) in enumerate(
            zip(tx1_items, tx2_items, strict=True), start=1
        ):
            pair_id = f"chronological-{repeat_index}"
            assigned.extend(
                (
                    replace(tx1_item, capture=replace(tx1_item.capture, pair_id=pair_id)),
                    replace(tx2_item, capture=replace(tx2_item.capture, pair_id=pair_id)),
                )
            )
            gaps.append(abs((tx2_item.created_at - tx1_item.created_at).total_seconds()))
    return tuple(assigned), PairingSummary(
        "chronological_rank_within_frequency",
        max(gaps) if gaps else None,
        len(gaps),
    )


def _load_geometry(path: Path) -> tuple[tuple[str, ...], npt.NDArray[np.float64], dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentError(f"cannot load array geometry {path}: {error}") from error
    root = _mapping(document, str(path))
    if root.get("schema") != 1:
        raise DocumentError("array geometry schema must be 1")
    raw_antennas = _sequence(root.get("antennas"), "geometry.antennas")
    names: list[str] = []
    positions: list[tuple[float, float]] = []
    for index, raw_antenna in enumerate(raw_antennas):
        antenna = _mapping(raw_antenna, f"geometry.antennas[{index}]")
        names.append(_string(antenna.get("name"), f"geometry.antennas[{index}].name"))
        raw_position = _sequence(
            antenna.get("vertical_axis_mm"),
            f"geometry.antennas[{index}].vertical_axis_mm",
        )
        if len(raw_position) != 2:
            raise DocumentError("each antenna vertical axis must contain x and y")
        positions.append(
            (
                _number(raw_position[0], f"geometry.antennas[{index}].x"),
                _number(raw_position[1], f"geometry.antennas[{index}].y"),
            )
        )
    expected = tuple(f"ANT{index}" for index in range(1, 9))
    if tuple(names) != expected:
        raise DocumentError("array geometry must be ordered ANT1 through ANT8")
    outline = _mapping(root.get("board_outline_mm"), "geometry.board_outline_mm")
    center = np.asarray(
        (
            0.5
            * (
                _number(outline.get("x0"), "board_outline_mm.x0")
                + _number(outline.get("x1"), "board_outline_mm.x1")
            ),
            0.5
            * (
                _number(outline.get("y0"), "board_outline_mm.y0")
                + _number(outline.get("y1"), "board_outline_mm.y1")
            ),
        )
    )
    centered = np.asarray(positions, dtype=np.float64) - center
    provenance = {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "input_coordinate_origin": "geometry file / KiCad origin",
        "analysis_coordinate_origin": "board outline center",
        "board_center_in_input_coordinates_mm": center.tolist(),
    }
    return tuple(names), centered, provenance


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _phase_deg(value: complex) -> float:
    return atan2(value.imag, value.real) * 180.0 / pi


def _finite_or_none(value: float) -> float | None:
    return float(value) if isfinite(value) else None


def _analysis_document(
    analysis: ReferenceLocusAnalysis,
    *,
    loaded: tuple[LoadedCapture, ...],
    pairing: PairingSummary,
    geometry_provenance: Mapping[str, Any],
    antenna_positions_mm: npt.NDArray[np.float64],
    thresholds: Mapping[str, float | int],
    source_commit: str,
) -> dict[str, Any]:
    measurements = analysis.measurements
    profile_by_source = {
        int(source_index): profile_index
        for profile_index, source_index in enumerate(analysis.profiles.source_frequency_index)
    }
    frequency_rows = []
    for frequency_index, frequency_hz in enumerate(measurements.carrier_frequency_hz):
        profile_index = profile_by_source.get(frequency_index)
        states = []
        for state_index, state_name in enumerate(measurements.state_names):
            ratio = measurements.ratio_of_ratios[frequency_index, state_index]
            corrected = measurements.geometry_corrected_phasor[frequency_index, state_index]
            state_valid = bool(measurements.valid_mask[frequency_index, state_index])
            states.append(
                {
                    "name": state_name,
                    "quality_passed": state_valid,
                    "paired_repeat_count": int(
                        measurements.pair_count[frequency_index, state_index]
                    ),
                    "paired_repeat_coherence": float(
                        measurements.pair_coherence[frequency_index, state_index]
                    ),
                    "ratio_of_ratios": (
                        _complex_document(complex(ratio)) if state_valid else None
                    ),
                    "ratio_of_ratios_phase_deg": (
                        _phase_deg(complex(ratio)) if state_valid else None
                    ),
                    "array_path_difference_mm": float(
                        measurements.array_path_difference_mm[state_index]
                    ),
                    "geometry_corrected_phasor": (
                        _complex_document(complex(corrected)) if state_valid else None
                    ),
                    "geometry_corrected_phase_deg": (
                        _phase_deg(complex(corrected)) if state_valid else None
                    ),
                    "phase_standard_error_deg": _finite_or_none(
                        float(
                            measurements.phase_standard_error_deg[
                                frequency_index, state_index
                            ]
                        )
                    ),
                    "ideal_free_space_corrected_amplitude_ratio": _finite_or_none(
                        float(
                            measurements.corrected_amplitude_ratio[
                                frequency_index, state_index
                            ]
                        )
                    ),
                    "corrected_log_amplitude_repeat_std": _finite_or_none(
                        float(
                            measurements.corrected_log_amplitude_std[
                                frequency_index, state_index
                            ]
                        )
                    ),
                }
            )
        profile = None
        if profile_index is not None:
            profile = {
                "phasor": _complex_document(complex(analysis.profiles.phasor[profile_index])),
                "phase_deg": _phase_deg(complex(analysis.profiles.phasor[profile_index])),
                "phase_standard_error_deg": float(
                    analysis.profiles.phase_standard_error_deg[profile_index]
                ),
                "state_coherence": float(analysis.profiles.state_coherence[profile_index]),
                "state_phase_rms_deg": float(
                    analysis.profiles.state_phase_rms_deg[profile_index]
                ),
                "valid_state_count": int(
                    analysis.profiles.valid_state_count[profile_index]
                ),
            }
        frequency_rows.append(
            {
                "carrier_frequency_hz": float(frequency_hz),
                "accepted_as_one_likelihood_row": profile is not None,
                "frequency_profile": profile,
                "states": states,
            }
        )

    fit = analysis.fit
    maximum_weight = float(np.max(fit.normalized_weight))
    profile_stride = max(1, fit.grid_mm.size // 5000)
    profile_indices = np.arange(0, fit.grid_mm.size, profile_stride)
    if profile_indices[-1] != fit.grid_mm.size - 1:
        profile_indices = np.append(profile_indices, fit.grid_mm.size - 1)
    source_documents = [
        {
            "path": str(item.path.resolve()),
            "sha256": item.sha256,
            "artifact_id": item.capture.artifact_id,
            "pair_id": item.capture.pair_id,
            "tx_channel": item.capture.tx_channel,
            "carrier_frequency_hz": item.capture.carrier_frequency_hz,
            "quality_passed": item.capture.global_quality_passed,
            "created_at": item.created_at.isoformat(),
        }
        for item in sorted(loaded, key=lambda item: (item.created_at, item.capture.artifact_id))
    ]
    return {
        "schema": 1,
        "analysis_kind": "paired_tx_rx1_range_difference_locus",
        "status": "passed",
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": source_commit,
        "inputs": {
            "source_documents": source_documents,
            "pairing": asdict(pairing),
            "geometry": dict(geometry_provenance),
        },
        "geometry": {
            "coordinate_system": (
                "millimetres in board plane; origin at PCB outline center; +x right/east, "
                "+y down/south in top view"
            ),
            "antenna_positions_mm": {
                name: antenna_positions_mm[index].tolist()
                for index, name in enumerate(measurements.state_names)
            },
            "tx1_position_mm": list(analysis.tx1_position_mm),
            "tx2_position_mm": list(analysis.tx2_position_mm),
            "anchor_separation_mm": analysis.anchor_separation_mm,
        },
        "quality_gate": {
            "passed": True,
            "thresholds": dict(thresholds),
            "input_document_count": len(loaded),
            "input_quality_failed_count": sum(
                not item.capture.global_quality_passed for item in loaded
            ),
            "unique_frequency_count": measurements.frequency_count,
            "accepted_frequency_count": analysis.profiles.frequency_count,
            "pairing_assumption": (
                "TX1 and TX2 members of a pair share stable RX2/RX1 channel phase. Pair-repeat "
                "coherence tests this assumption but cannot prove absence of a common bias."
            ),
        },
        "observable": {
            "definition": "Q[f,i] = (RX2/RX1)[TX2,f,i] / (RX2/RX1)[TX1,f,i]",
            "geometry_correction": (
                "Q[f,i] * exp(+j k[f] (d(ANT[i],TX2)-d(ANT[i],TX1)))"
            ),
            "predicted_corrected_phase": (
                "+k[f] * (d(RX1,TX2)-d(RX1,TX1))"
            ),
            "pcb_path_handling": (
                "No released PCB delay is injected: the same-state, same-frequency TX2/TX1 "
                "ratio cancels each fixed selector/PCB path algebraically."
            ),
            "aggregation_policy": (
                "Cycles form one phasor per capture; paired capture repeats form one Q per "
                "frequency/state; selector states form one frequency row. State scatter is not "
                "divided by sqrt(state count)."
            ),
        },
        "measurements": {"frequency_rows": frequency_rows},
        "range_difference_fit": {
            "definition": "d(RX1,TX2)-d(RX1,TX1)",
            "map_mm": fit.map_range_difference_mm,
            "median_mm": fit.median_range_difference_mm,
            "interval_50_mm": list(fit.interval_50_mm),
            "interval_90_mm": list(fit.interval_90_mm),
            "map_wrapped_rms_deg": fit.map_wrapped_rms_deg,
            "effective_grid_sample_count": fit.effective_grid_sample_count,
            "competing_modes": [
                {"range_difference_mm": delta, "relative_log_likelihood": relative}
                for delta, relative in fit.competing_modes
            ],
            "profile": {
                "decimation_stride": profile_stride,
                "range_difference_mm": fit.grid_mm[profile_indices].tolist(),
                "relative_log_likelihood": fit.relative_log_likelihood[
                    profile_indices
                ].tolist(),
                "relative_weight": (
                    fit.normalized_weight[profile_indices] / maximum_weight
                ).tolist(),
            },
        },
        "identifiability": {
            "geometric_rank": analysis.identifiability_rank,
            "unique_planar_position_identified": False,
            "primary_result_type": "signed range-difference hyperbola",
            "equation": (
                "d((x,y),TX2)-d((x,y),TX1) = range_difference_fit.map_mm"
            ),
            "reason": (
                "Eight switch states repeatedly observe the same RX1 phase center. With only "
                "two known transmitter points, the data contain one independent geometric "
                "scalar, so every point on the signed hyperbola is phase-equivalent."
            ),
            "additional_information_needed_for_point": (
                "a third non-collinear surveyed transmitter position (four preferred), or a "
                "calibrated RX1/RX2 differential delay plus a side/half-plane constraint"
            ),
            "hyperbola_points_mm": analysis.hyperbola_points_mm.tolist(),
        },
        "sensitivity": {
            "interpretation": (
                "Each row is a robustness diagnostic, not an additional independent result. "
                "A two-frequency leave-one-out fit may be multimodal and is not standalone "
                "localization evidence."
            ),
            "leave_one_frequency_out": [
                asdict(item) for item in analysis.leave_one_frequency_out
            ],
            "leave_one_state_out": [asdict(item) for item in analysis.leave_one_state_out],
        },
        "weak_amplitude_diagnostic": {
            **asdict(analysis.weak_amplitude),
            "primary_fit_uses_amplitude": False,
            "unique_planar_position_identified": False,
            "warning": (
                "This diagnostic assumes inverse-distance free-space magnitude and stable "
                "antenna patterns. Indoor multipath and orientation commonly violate it."
            ),
        },
    }


def _failure_document(error: BaseException, *, source_commit: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "analysis_kind": "paired_tx_rx1_range_difference_locus",
        "status": "failed",
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": source_commit,
        "quality_gate": {"passed": False, "failure": f"{type(error).__name__}: {error}"},
        "identifiability": {
            "geometric_rank": 1,
            "unique_planar_position_identified": False,
        },
    }


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _source_commit() -> str:
    try:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def main() -> int:
    args = _parser().parse_args()
    source_commit = _source_commit()
    try:
        paths = _input_paths(args.input)
        loaded = tuple(_load_capture(path) for path in paths)
        loaded, pairing = _assign_pair_ids(loaded, manifest=args.pairing_manifest)
        state_names, antenna_positions, geometry_provenance = _load_geometry(
            args.geometry.expanduser().resolve()
        )
        if any(item.capture.state_names != state_names for item in loaded):
            raise DocumentError("capture state names disagree with the geometry")
        thresholds: dict[str, float | int] = {
            "minimum_pair_repeats": args.minimum_pair_repeats,
            "minimum_pair_coherence": args.minimum_pair_coherence,
            "minimum_valid_states": args.minimum_valid_states,
            "minimum_state_coherence": args.minimum_state_coherence,
            "grid_step_mm": args.grid_step_mm,
            "systematic_phase_standard_error_deg": args.systematic_phase_std_deg,
        }
        analysis = analyze_reference_locus(
            tuple(item.capture for item in loaded),
            antenna_positions_mm=antenna_positions,
            tx1_position_mm=(args.tx1_x_mm, args.tx1_y_mm),
            tx2_position_mm=(args.tx2_x_mm, args.tx2_y_mm),
            bounds_mm=(args.x_min_mm, args.x_max_mm, args.y_min_mm, args.y_max_mm),
            minimum_pair_repeats=args.minimum_pair_repeats,
            minimum_pair_coherence=args.minimum_pair_coherence,
            minimum_valid_states=args.minimum_valid_states,
            minimum_state_coherence=args.minimum_state_coherence,
            grid_step_mm=args.grid_step_mm,
            systematic_phase_standard_error_deg=args.systematic_phase_std_deg,
        )
        document = _analysis_document(
            analysis,
            loaded=loaded,
            pairing=pairing,
            geometry_provenance=geometry_provenance,
            antenna_positions_mm=antenna_positions,
            thresholds=thresholds,
            source_commit=source_commit,
        )
    except (DocumentError, ReferenceLocusError, ValueError, OSError) as error:
        document = _failure_document(error, source_commit=source_commit)
        _write_json_atomic(args.output, document)
        print(json.dumps({"status": "failed", "output": str(args.output), "error": str(error)}))
        return 2
    _write_json_atomic(args.output, document)
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(args.output),
                "range_difference_mm": analysis.fit.map_range_difference_mm,
                "result_type": "signed_range_difference_hyperbola",
                "unique_planar_position_identified": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
