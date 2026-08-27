#!/usr/bin/env python3
"""Fit board and splitter-feed path terms from a conducted cyclic-permutation run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from smateway.permutation_calibration import (
    PermutationCalibrationError,
    PermutationObservation,
    coherent_leakage_phase_bound_deg,
    compare_rotation_zero_closure,
    fit_separable_paths,
)

ANTENNAS = tuple(f"ANT{index}" for index in range(1, 9))
FEEDS = tuple(f"F{index}" for index in range(1, 9))
MAXIMUM_MODEL_AMPLITUDE_RMS_DB = 0.2
MAXIMUM_MODEL_PHASE_RMS_DEG = 2.0
MAXIMUM_CLOSURE_SHAPE_AMPLITUDE_RMS_DB = 0.1
MAXIMUM_CLOSURE_SHAPE_PHASE_RMS_DEG = 2.0
MINIMUM_OPERATIONAL_RAW_CONTRAST_DB = 20.0
PRECISION_PHASE_TARGET_DEG = 1.0
MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB = 20.0 * math.log10(
    1.0 / math.sin(math.radians(PRECISION_PHASE_TARGET_DEG))
)


class AnalysisInputError(RuntimeError):
    """A manifest or immutable capture analysis violates an experiment invariant."""


@dataclass(frozen=True, slots=True)
class LoadedCapture:
    artifact_id: str
    document_path: Path
    document_sha256: str
    artifact_sha256: str
    center_frequency_hz: int
    source_commit: str
    receiver_gain_db: int
    stream_id: int
    transfers: tuple[complex, ...]
    raw_contrast_db: tuple[float, ...]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="directory containing one subdirectory per artifact ID",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figure-directory", type=Path)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisInputError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise AnalysisInputError(f"{label} must be an array")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisInputError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisInputError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise AnalysisInputError(f"{label} must be finite")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisInputError(f"{label} must be a nonempty string")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AnalysisInputError(f"cannot load {label} {path}: {error}") from error


def _complex(value: object, label: str) -> complex:
    document = _mapping(value, label)
    result = complex(
        _number(document.get("real"), f"{label}.real"),
        _number(document.get("imag"), f"{label}.imag"),
    )
    if abs(result) <= 0.0:
        raise AnalysisInputError(f"{label} must be nonzero")
    return result


def _load_capture(artifact_root: Path, artifact_id: str, frequency_hz: int) -> LoadedCapture:
    path = artifact_root / artifact_id / "fast20-reference-transfer.json"
    document = _read_json(path, f"artifact {artifact_id}")
    if document.get("schema") != 1:
        raise AnalysisInputError(f"artifact {artifact_id} analysis schema is not 1")
    if document.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer":
        raise AnalysisInputError(f"artifact {artifact_id} has the wrong analysis kind")
    quality = _mapping(document.get("quality_gate"), f"artifact {artifact_id} quality gate")
    if quality.get("passed") is not True:
        raise AnalysisInputError(f"artifact {artifact_id} did not pass reference-transfer gates")
    artifact = _mapping(document.get("artifact"), f"artifact {artifact_id} identity")
    if artifact.get("artifact_id") != artifact_id:
        raise AnalysisInputError(f"artifact {artifact_id} identity does not match its directory")
    if round(_number(artifact.get("center_frequency_hz"), "artifact center frequency")) != (
        frequency_hz
    ):
        raise AnalysisInputError(f"artifact {artifact_id} center frequency does not match")
    aggregation = _mapping(document.get("aggregation_key"), "aggregation key")
    if _integer(aggregation.get("center_frequency_hz"), "aggregation center frequency") != (
        frequency_hz
    ):
        raise AnalysisInputError(f"artifact {artifact_id} aggregation frequency does not match")
    if _integer(aggregation.get("tx_channel"), "TX channel") != 0:
        raise AnalysisInputError(f"artifact {artifact_id} did not use TX1")
    capture = _mapping(document.get("capture"), "capture")
    if capture.get("profile_contract_sha256") is None:
        raise AnalysisInputError(f"artifact {artifact_id} lacks the profile contract hash")
    transfer = _mapping(document.get("transfer"), "transfer")
    all_off = _mapping(transfer.get("all_off"), "ALL_OFF")
    all_off_raw = _mapping(all_off.get("raw_rx2_over_rx1"), "ALL_OFF raw transfer")
    all_off_amplitude = _number(all_off_raw.get("amplitude"), "ALL_OFF amplitude")
    if all_off_amplitude <= 0.0:
        raise AnalysisInputError(f"artifact {artifact_id} ALL_OFF amplitude must be positive")

    state_rows = tuple(
        _mapping(item, "transfer state") for item in _sequence(transfer.get("states"), "states")
    )
    if tuple(row.get("name") for row in state_rows) != ANTENNAS:
        raise AnalysisInputError(f"artifact {artifact_id} states differ from ANT1..ANT8")
    transfers = []
    contrasts = []
    for row in state_rows:
        name = _string(row.get("name"), "state name")
        if row.get("quality_passed") is not True:
            raise AnalysisInputError(f"artifact {artifact_id} {name} quality gate failed")
        subtracted = _mapping(
            row.get("all_off_subtracted_rx2_over_rx1"), f"{name} subtracted transfer"
        )
        transfers.append(_complex(subtracted.get("phasor"), f"{name} subtracted phasor"))
        raw = _mapping(row.get("raw_rx2_over_rx1"), f"{name} raw transfer")
        raw_amplitude = _number(raw.get("amplitude"), f"{name} raw amplitude")
        if raw_amplitude <= 0.0:
            raise AnalysisInputError(f"artifact {artifact_id} {name} raw amplitude is nonpositive")
        contrasts.append(20.0 * math.log10(raw_amplitude / all_off_amplitude))

    return LoadedCapture(
        artifact_id=artifact_id,
        document_path=path,
        document_sha256=_sha256(path),
        artifact_sha256=_string(artifact.get("sha256"), "raw artifact SHA-256"),
        center_frequency_hz=frequency_hz,
        source_commit=_string(document.get("source_commit"), "source commit"),
        receiver_gain_db=_integer(aggregation.get("receiver_gain_db"), "receiver gain"),
        stream_id=_integer(aggregation.get("stream_id"), "stream ID"),
        transfers=tuple(transfers),
        raw_contrast_db=tuple(contrasts),
    )


def _round_by_rotation(manifest: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    rounds = {
        _integer(row.get("rotation"), "round rotation"): row
        for row in (
            _mapping(item, "round") for item in _sequence(manifest.get("rounds"), "rounds")
        )
    }
    if not {0, 1, 2}.issubset(rounds):
        raise AnalysisInputError("manifest must contain rotations 0, 1 and 2")
    return rounds


def _mapping_for_round(round_document: Mapping[str, Any]) -> Mapping[str, Any]:
    mapping = _mapping(round_document.get("mapping"), "round mapping")
    if set(mapping) != set(FEEDS) or set(mapping.values()) != set(ANTENNAS):
        raise AnalysisInputError("each round mapping must be a bijection from F1..F8 to ANT1..ANT8")
    return mapping


def _artifact_id(round_document: Mapping[str, Any], frequency_hz: int) -> str:
    artifacts = _mapping(round_document.get("artifacts_by_frequency_hz"), "round artifacts")
    return _string(artifacts.get(str(frequency_hz)), f"artifact ID at {frequency_hz} Hz")


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_analysis(manifest_path: Path, artifact_root: Path) -> dict[str, Any]:
    """Build the deterministic final analysis from one run manifest and accepted captures."""

    manifest = _read_json(manifest_path, "closed-loop manifest")
    if manifest.get("schema") != 1:
        raise AnalysisInputError("closed-loop manifest schema must be 1")
    frequencies = tuple(
        _integer(value, "frequency")
        for value in _sequence(manifest.get("frequencies_hz"), "frequencies")
    )
    if not frequencies:
        raise AnalysisInputError("manifest frequency list is empty")
    rounds = _round_by_rotation(manifest)
    closure = _mapping(manifest.get("closure"), "rotation-0 closure")
    if _integer(closure.get("rotation"), "closure rotation") != 0:
        raise AnalysisInputError("closure must restore rotation 0")
    if _mapping_for_round(closure) != _mapping_for_round(rounds[0]):
        raise AnalysisInputError("closure mapping differs from the initial rotation-0 mapping")

    loaded: dict[tuple[str, int], LoadedCapture] = {}

    def capture(round_document: Mapping[str, Any], frequency_hz: int) -> LoadedCapture:
        identity = _artifact_id(round_document, frequency_hz)
        key = (identity, frequency_hz)
        if key not in loaded:
            loaded[key] = _load_capture(artifact_root, identity, frequency_hz)
        return loaded[key]

    frequency_results = []
    closure_results = []
    for frequency_hz in frequencies:
        fitting_rounds = (closure, rounds[1], rounds[2])
        observations = []
        fit_captures = []
        for round_document in fitting_rounds:
            rotation = _integer(round_document.get("rotation"), "rotation")
            selected = capture(round_document, frequency_hz)
            fit_captures.append(selected)
            mapping = _mapping_for_round(round_document)
            for feed in FEEDS:
                antenna = _string(mapping[feed], f"mapping for {feed}")
                observations.append(
                    PermutationObservation(
                        rotation=rotation,
                        feed=feed,
                        antenna=antenna,
                        transfer=selected.transfers[ANTENNAS.index(antenna)],
                        artifact_id=selected.artifact_id,
                    )
                )
        try:
            fitted = fit_separable_paths(observations)
        except PermutationCalibrationError as error:
            raise AnalysisInputError(f"cannot fit {frequency_hz} Hz: {error}") from error

        all_contrasts = tuple(value for item in fit_captures for value in item.raw_contrast_db)
        minimum_contrast = min(all_contrasts)
        phase_bound = coherent_leakage_phase_bound_deg(minimum_contrast)
        fit_quality = _mapping(fitted.get("fit_quality"), "fit quality")
        model_passed = (
            _number(fit_quality.get("amplitude_residual_rms_db"), "amplitude residual")
            <= MAXIMUM_MODEL_AMPLITUDE_RMS_DB
            and _number(fit_quality.get("phase_residual_rms_deg"), "phase residual")
            <= MAXIMUM_MODEL_PHASE_RMS_DEG
        )

        closure_result: dict[str, Any] | None = None
        initial_artifacts = _mapping(
            rounds[0].get("artifacts_by_frequency_hz"), "initial rotation-0 artifacts"
        )
        if str(frequency_hz) in initial_artifacts:
            initial = capture(rounds[0], frequency_hz)
            current = capture(closure, frequency_hz)
            closure_result = {
                "frequency_hz": frequency_hz,
                "initial_artifact_id": initial.artifact_id,
                "closure_artifact_id": current.artifact_id,
                **compare_rotation_zero_closure(initial.transfers, current.transfers),
            }
            closure_results.append(closure_result)
        closure_passed = closure_result is None or (
            _number(
                closure_result.get("relative_shape_gain_rms_db"), "closure amplitude RMS"
            )
            <= MAXIMUM_CLOSURE_SHAPE_AMPLITUDE_RMS_DB
            and _number(
                closure_result.get("relative_shape_phase_rms_deg"), "closure phase RMS"
            )
            <= MAXIMUM_CLOSURE_SHAPE_PHASE_RMS_DEG
        )
        operational_isolation_passed = minimum_contrast >= MINIMUM_OPERATIONAL_RAW_CONTRAST_DB
        precision_isolation_passed = minimum_contrast >= MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB
        if model_passed and closure_passed and operational_isolation_passed:
            status = (
                "qualified_conducted_path_calibration"
                if precision_isolation_passed
                else "qualified_with_measured_isolation_phase_floor"
            )
        elif model_passed and closure_passed:
            status = "experimental_leakage_limited"
        else:
            status = "rejected"

        frequency_results.append(
            {
                "center_frequency_hz": frequency_hz,
                "carrier_frequency_hz": frequency_hz + 99_992,
                "fit_artifact_ids_by_rotation": {
                    str(rotation): item.artifact_id
                    for rotation, item in (
                        (0, fit_captures[0]),
                        (1, fit_captures[1]),
                        (2, fit_captures[2]),
                    )
                },
                "receiver_gain_db": fit_captures[0].receiver_gain_db,
                "separable_model": fitted,
                "raw_isolation": {
                    "minimum_selected_to_all_off_contrast_db": minimum_contrast,
                    "maximum_selected_to_all_off_contrast_db": max(all_contrasts),
                    "worst_case_coherent_leakage_phase_bound_deg": phase_bound,
                    "phase_bound_is_finite": phase_bound is not None,
                },
                "closure": closure_result,
                "quality_gate": {
                    "status": status,
                    "separable_model_passed": model_passed,
                    "rotation_zero_closure_passed": closure_passed,
                    "operational_raw_isolation_passed": operational_isolation_passed,
                    "one_degree_raw_isolation_passed": precision_isolation_passed,
                    "may_be_used_as_board_calibration": status.startswith("qualified_"),
                },
            }
        )

    source_documents = [
        {
            "artifact_id": item.artifact_id,
            "center_frequency_hz": item.center_frequency_hz,
            "analysis_path": _relative(item.document_path, manifest_path.parent),
            "analysis_sha256": item.document_sha256,
            "raw_artifact_sha256": item.artifact_sha256,
            "source_commit": item.source_commit,
            "receiver_gain_db": item.receiver_gain_db,
            "stream_id": item.stream_id,
        }
        for item in sorted(
            loaded.values(), key=lambda row: (row.center_frequency_hz, row.artifact_id)
        )
    ]
    qualified = [
        row["center_frequency_hz"]
        for row in frequency_results
        if _mapping(row["quality_gate"], "quality").get("may_be_used_as_board_calibration")
    ]
    experimental = [
        row["center_frequency_hz"]
        for row in frequency_results
        if _mapping(row["quality_gate"], "quality").get("status")
        == "experimental_leakage_limited"
    ]
    return {
        "schema": 1,
        "analysis_kind": "fast20_closed_loop_permutation_calibration",
        "run_id": _string(manifest.get("run_id"), "run ID"),
        "board_id": _string(manifest.get("board_id"), "board ID"),
        "pluto_serial": _string(manifest.get("pluto_serial"), "Pluto serial"),
        "profile_id": _string(manifest.get("profile_id"), "profile ID"),
        "profile_contract_sha256": _string(
            manifest.get("profile_contract_sha256"), "profile contract SHA-256"
        ),
        "firmware_binary_sha256": _string(
            manifest.get("firmware_binary_sha256"), "firmware SHA-256"
        ),
        "source_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": _sha256(manifest_path),
        },
        "source_commit": subprocess.run(
            ("git", "rev-parse", "HEAD"), check=True, capture_output=True, text=True
        ).stdout.strip(),
        "fit_input_policy": (
            "use the final rotation-0 closure plus accepted rotations 1 and 2; retain the "
            "initial rotation-0 captures only for reconnect closure validation"
        ),
        "thresholds": {
            "maximum_model_amplitude_residual_rms_db": MAXIMUM_MODEL_AMPLITUDE_RMS_DB,
            "maximum_model_phase_residual_rms_deg": MAXIMUM_MODEL_PHASE_RMS_DEG,
            "maximum_closure_shape_amplitude_rms_db": (
                MAXIMUM_CLOSURE_SHAPE_AMPLITUDE_RMS_DB
            ),
            "maximum_closure_shape_phase_rms_deg": MAXIMUM_CLOSURE_SHAPE_PHASE_RMS_DEG,
            "minimum_operational_raw_contrast_db": MINIMUM_OPERATIONAL_RAW_CONTRAST_DB,
            "precision_phase_target_deg": PRECISION_PHASE_TARGET_DEG,
            "minimum_raw_contrast_for_precision_target_db": (
                MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB
            ),
        },
        "frequency_results": frequency_results,
        "rotation_zero_closure_results": closure_results,
        "source_documents": source_documents,
        "conclusions": {
            "captures_complete": True,
            "additional_cyclic_rotations_required": False,
            "qualified_board_calibration_frequencies_hz": qualified,
            "experimental_leakage_limited_frequencies_hz": experimental,
            "normal_wiring_restored": True,
            "normal_wiring": "F1->ANT1, F2->ANT2, ..., F8->ANT8",
            "phase_branch_caveat": (
                "Cyclic mappings retain an eight-way 45-degree spatial-ramp ambiguity. "
                "This analysis selects the minimum reconnect-common-phase branch using the "
                "continuous RX1 reference and closure evidence. A future noncyclic swap can "
                "remove that prior if absolute phase metrology is required."
            ),
            "5g8_caveat": (
                "The ALL_OFF-subtracted 5.8 GHz model is separable, but raw leakage can equal "
                "or exceed selected-path signal. Its coefficients are diagnostic only until "
                "RF isolation is improved or a held-out noncyclic validation passes."
            ),
        },
    }


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _render_figures(document: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    rows = tuple(
        _mapping(item, "frequency result")
        for item in _sequence(document.get("frequency_results"), "frequency results")
    )
    colors = plt.cm.viridis([index / max(1, len(rows) - 1) for index in range(len(rows))])
    antenna_index = list(range(1, 9))

    figure, axes = plt.subplots(2, 1, figsize=(10.5, 8.0), sharex=True, constrained_layout=True)
    for color, row in zip(colors, rows, strict=True):
        frequency_ghz = _integer(row.get("center_frequency_hz"), "frequency") / 1e9
        model = _mapping(row.get("separable_model"), "model")
        paths = tuple(
            _mapping(item, "board path")
            for item in _sequence(model.get("board_path_terms"), "board paths")
        )
        axes[0].plot(
            antenna_index,
            [_number(item.get("correction_gain_db"), "correction gain") for item in paths],
            marker="o",
            color=color,
            label=f"{frequency_ghz:.3f} GHz",
        )
        axes[1].plot(
            antenna_index,
            [_number(item.get("correction_phase_deg"), "correction phase") for item in paths],
            marker="o",
            color=color,
        )
    axes[0].axhline(0.0, color="black", linewidth=0.8)
    axes[1].axhline(0.0, color="black", linewidth=0.8)
    axes[0].set_ylabel("Board correction gain (dB)\nrelative to ANT1")
    axes[1].set_ylabel("Board correction phase (degrees)\nrelative to ANT1")
    axes[1].set_xlabel("Selector input")
    axes[1].set_xticks(antenna_index, ANTENNAS)
    axes[0].grid(alpha=0.25)
    axes[1].grid(alpha=0.25)
    axes[0].legend(ncol=3, fontsize=8)
    figure.suptitle("Closed-loop board-path correction coefficients")
    coefficient_path = directory / "fig01_board_path_corrections.png"
    figure.savefig(coefficient_path, dpi=180)
    plt.close(figure)

    frequencies_ghz = [_integer(row.get("center_frequency_hz"), "frequency") / 1e9 for row in rows]
    phase_residual = []
    amplitude_residual = []
    contrast = []
    closure_phase = []
    for row in rows:
        model = _mapping(row.get("separable_model"), "model")
        quality = _mapping(model.get("fit_quality"), "fit quality")
        isolation = _mapping(row.get("raw_isolation"), "raw isolation")
        closure = row.get("closure")
        phase_residual.append(_number(quality.get("phase_residual_rms_deg"), "phase RMS"))
        amplitude_residual.append(
            _number(quality.get("amplitude_residual_rms_db"), "amplitude RMS")
        )
        contrast.append(
            _number(isolation.get("minimum_selected_to_all_off_contrast_db"), "contrast")
        )
        closure_phase.append(
            math.nan
            if closure is None
            else _number(
                _mapping(closure, "closure").get("relative_shape_phase_rms_deg"),
                "closure phase RMS",
            )
        )

    figure, axes = plt.subplots(3, 1, figsize=(10.5, 9.0), sharex=True, constrained_layout=True)
    axes[0].plot(frequencies_ghz, phase_residual, "o-", label="separable fit")
    axes[0].plot(frequencies_ghz, closure_phase, "s--", label="rotation-0 closure")
    axes[0].axhline(MAXIMUM_MODEL_PHASE_RMS_DEG, color="tab:red", linestyle=":")
    axes[0].set_ylabel("Phase RMS (degrees)")
    axes[0].legend()
    axes[1].plot(frequencies_ghz, amplitude_residual, "o-", color="tab:green")
    axes[1].axhline(MAXIMUM_MODEL_AMPLITUDE_RMS_DB, color="tab:red", linestyle=":")
    axes[1].set_ylabel("Model amplitude RMS (dB)")
    axes[2].plot(frequencies_ghz, contrast, "o-", color="tab:purple")
    axes[2].axhline(MINIMUM_OPERATIONAL_RAW_CONTRAST_DB, color="tab:orange", linestyle="--")
    axes[2].axhline(MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB, color="tab:red", linestyle=":")
    axes[2].set_ylabel("Worst raw selected/ALL_OFF (dB)")
    axes[2].set_xlabel("Centre frequency (GHz)")
    for axis in axes:
        axis.grid(alpha=0.25)
    figure.suptitle("Permutation fit, reconnect closure, and RF-isolation gates")
    quality_path = directory / "fig02_quality_and_isolation.png"
    figure.savefig(quality_path, dpi=180)
    plt.close(figure)
    return coefficient_path, quality_path


def main() -> int:
    args = _parser().parse_args()
    manifest = _read_json(args.manifest, "closed-loop manifest")
    if args.artifact_root is None:
        board_id = _string(manifest.get("board_id"), "board ID")
        artifact_root = (
            Path.home() / ".local/state/smateway/boards" / board_id / "pluto-usb-captures"
        )
    else:
        artifact_root = args.artifact_root
    document = build_analysis(args.manifest, artifact_root)
    _write_json_atomic(args.output, document)
    figures: Sequence[Path] = ()
    if args.figure_directory is not None:
        figures = _render_figures(document, args.figure_directory)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "figures": [str(path) for path in figures],
                "qualified_frequencies_hz": document["conclusions"][
                    "qualified_board_calibration_frequencies_hz"
                ],
                "experimental_frequencies_hz": document["conclusions"][
                    "experimental_leakage_limited_frequencies_hz"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
