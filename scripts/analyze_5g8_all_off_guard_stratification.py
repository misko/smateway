#!/usr/bin/env python3
"""Reproduce the exact-5.8-GHz selector-synchronous ALL_OFF stratification.

This is a read-only offline diagnostic.  It streams the exact twenty raw
rotation-0 captures named by the committed 20-pass repeatability result,
verifies every inventory, SigMF, continuity, and retained-analysis binding,
then compares the clean center of nominally identical ALL_OFF intervals.

A positive result can identify a selector/control-state-dependent contribution.
A null result cannot distinguish Pluto/direct-field leakage from state-independent
cable or selector-common-launch leakage, so it cannot replace physical Stage A.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

import smateway.capture_continuity as capture_continuity_library
import smateway.guard_stratification as guard_stratification_library
import smateway.hexcal as hexcal_library
import smateway.profile as profile_library
import smateway.schedule_alignment as schedule_alignment_library
from smateway.capture_continuity import validate_sigmf_continuity
from smateway.guard_stratification import (
    CaptureStratification,
    DetectionThresholds,
    aggregate_capture_centers,
    phase_coherence,
    robust_complex_center,
    stratify_all_off_transfer,
)
from smateway.hexcal import sha256_path, write_json_atomic
from smateway.profile import ControlProfile, load_profile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_BOARD_STATE_ROOT = Path.home() / ".local/state/smateway/boards"
DEFAULT_INVENTORY = REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/data/evidence-inventory.json"
DEFAULT_REPEATABILITY = (
    REPOSITORY_ROOT
    / "docs/closed_loop_frequency_sweep_repeatability/data"
    / "rotation0-repeatability-20pass-results.json"
)
DEFAULT_PROFILE = REPOSITORY_ROOT / "profiles/fast20-v1/control_profile.json"
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "docs/5g8_root_cause_analysis/data"
    / "selector-synchronous-all-off-guard-stratification.json"
)
DEFAULT_FIGURE = (
    REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/png/fig07_all_off_guard_stratification.png"
)

EXPECTED_INVENTORY_SHA256 = "486468af1b85e1d3c4897584d9b3316639eb0ec78bab1f2496e833475ed3d319"
EXPECTED_REPEATABILITY_SHA256 = "359f22070647ac8201b7d62845a175931c1eabfa18f075620282b69c01adf64b"
EXPECTED_PROFILE_SHA256 = "c8f3de200573d90d1b87b623770cd9253649c634a22cea1a9ca980042ae40f11"
EXPECTED_PROFILE_HEADER_SHA256 = "839a1eced9eaf168bb8128301a8509709bb42a09c89ce5d6fbf26b216e79bfd3"
EXPECTED_PROFILE_CONTRACT_SHA256 = (
    "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
)
EXPECTED_PYPROJECT_SHA256 = "40c4d784810e2502c670e3ac0a7af238aad2743a9a4f048a08d2b4540fbf628f"
EXPECTED_UV_LOCK_SHA256 = "1a79ca57c67e8d51f1d340d99654237a40ce183cc3b97151866ef5b2328c3030"
EXPECTED_PYTHON_IMPLEMENTATION = "CPython"
EXPECTED_PYTHON_VERSION = "3.11.2"
EXPECTED_PYTHON_CACHE_TAG = "cpython-311"
EXPECTED_NUMPY_VERSION = "2.4.6"
EXPECTED_MATPLOTLIB_VERSION = "3.11.1"
EXPECTED_MATPLOTLIB_BACKEND = "Agg"
EXPECTED_MATPLOTLIB_CANVAS = "matplotlib.backends.backend_agg.FigureCanvasAgg"
EXPECTED_MATPLOTLIB_RENDERER = "matplotlib.backends.backend_agg.RendererAgg"
EXPECTED_FONT_FILES = (
    {
        "path": "matplotlib-data/fonts/ttf/DejaVuSans-Bold.ttf",
        "sha256": "b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167",
        "byte_size": 704_128,
    },
    {
        "path": "matplotlib-data/fonts/ttf/DejaVuSans.ttf",
        "sha256": "3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22",
        "byte_size": 756_072,
    },
)
EXPECTED_CENTER_FREQUENCY_HZ = 5_800_000_000
EXPECTED_SAMPLE_RATE_HZ = 1_000_000
EXPECTED_SAMPLE_COUNT = 10_000_000
EXPECTED_RECEIVER_COUNT = 2
EXPECTED_CAPTURE_COUNT = 20
EXPECTED_COMPLETE_CYCLES = 25
EXPECTED_ANALYZED_CYCLES = 23
EXPECTED_RAW_BYTES = 80_000_000
COHERENT_BIN_SAMPLES = 100
MARKER_ANCHOR_WINDOW_MS = (10.0, 70.0)
CENTER_WINDOW_AFTER_ENTRY_MS = (2.0, 3.0)
MINIMUM_REFERENCE_VALID_FRACTION = 0.95
MINIMUM_RETAINED_ALIGNMENT_SCORE = 0.99
MINIMUM_RETAINED_ALIGNMENT_EVEN_ODD_AGREEMENT = 0.99
MINIMUM_PILOT_CONFIDENCE = 0.99
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.999
MAXIMUM_PILOT_PHASE_RESIDUAL_RMS_RAD = 0.01
MAXIMUM_MARKER_VERSUS_RETAINED_H_OFF_RELATIVE_ERROR = 0.05
MAXIMUM_MARKER_VERSUS_RETAINED_H_OFF_PHASE_ERROR_DEG = 2.0
DETECTION_THRESHOLDS = DetectionThresholds(
    minimum_amplitude_fraction_of_h_off=0.005,
    minimum_cross_capture_phase_coherence=0.75,
)


class GuardArtifactError(RuntimeError):
    """Persisted evidence is absent, inconsistent, or outside the fixed cohort."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--board-state-root", type=Path, default=DEFAULT_BOARD_STATE_ROOT)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--repeatability", type=Path, default=DEFAULT_REPEATABILITY)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--figure", type=Path, default=DEFAULT_FIGURE)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GuardArtifactError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise GuardArtifactError(f"{label} must be an array")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuardArtifactError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardArtifactError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GuardArtifactError(f"{label} must be finite")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GuardArtifactError(f"{label} must be a nonempty string")
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardArtifactError(f"cannot read {label}: {path}") from error
    return dict(_mapping(value, label))


def _sha256_stream(path: Path, *, chunk_bytes: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(chunk_bytes), b""):
                digest.update(block)
    except OSError as error:
        raise GuardArtifactError(f"cannot hash {path}") from error
    return digest.hexdigest()


def _require_sha256(path: Path, expected: str, label: str) -> None:
    observed = _sha256_stream(path)
    if observed != expected:
        raise GuardArtifactError(
            f"{label} SHA-256 mismatch: expected {expected}, observed {observed}"
        )


def _reported_repository_path(path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise GuardArtifactError(f"repository source escapes the repository: {path}") from error


def _source_binding(path: Path) -> dict[str, str]:
    return {"path": _reported_repository_path(path), "sha256": sha256_path(path)}


def _canonical_figure_path(path: Path) -> Path:
    """Resolve only the one reviewed repository figure target, without writing."""

    candidate = path.expanduser().resolve(strict=False)
    expected = DEFAULT_FIGURE.resolve(strict=False)
    repository = REPOSITORY_ROOT.resolve(strict=True)
    try:
        candidate.relative_to(repository)
    except ValueError as error:
        raise GuardArtifactError("--figure escapes the repository") from error
    if candidate != expected:
        raise GuardArtifactError(
            "--figure must be the canonical repository guard-stratification figure"
        )
    return candidate


def _font_file_binding(path: Path, matplotlib_data_root: Path) -> dict[str, str | int]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(matplotlib_data_root.resolve(strict=True))
    except ValueError as error:
        raise GuardArtifactError(
            "Matplotlib selected a font outside its bound data tree"
        ) from error
    if not resolved.is_file():
        raise GuardArtifactError("Matplotlib font binding is not a regular file")
    return {
        "path": f"matplotlib-data/{relative.as_posix()}",
        "sha256": _sha256_stream(resolved),
        "byte_size": resolved.stat().st_size,
    }


def _expected_generation_environment() -> dict[str, Any]:
    return {
        "project_inputs": {
            "pyproject": {
                "path": "pyproject.toml",
                "sha256": EXPECTED_PYPROJECT_SHA256,
            },
            "uv_lock": {"path": "uv.lock", "sha256": EXPECTED_UV_LOCK_SHA256},
        },
        "python": {
            "implementation": EXPECTED_PYTHON_IMPLEMENTATION,
            "version": EXPECTED_PYTHON_VERSION,
            "cache_tag": EXPECTED_PYTHON_CACHE_TAG,
        },
        "packages": {
            "numpy_version": EXPECTED_NUMPY_VERSION,
            "matplotlib_version": EXPECTED_MATPLOTLIB_VERSION,
        },
        "rendering": {
            "backend": EXPECTED_MATPLOTLIB_BACKEND,
            "figure_canvas": EXPECTED_MATPLOTLIB_CANVAS,
            "renderer": EXPECTED_MATPLOTLIB_RENDERER,
            "font_files": [dict(item) for item in EXPECTED_FONT_FILES],
        },
    }


def _generation_environment() -> dict[str, Any]:
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        from matplotlib import font_manager
        from matplotlib.backends.backend_agg import FigureCanvasAgg, RendererAgg
    except ImportError as error:
        raise GuardArtifactError(
            "generation environment requires the report dependency group"
        ) from error
    matplotlib_data_root = Path(matplotlib.get_data_path())
    font_paths = {
        Path(
            font_manager.findfont(
                font_manager.FontProperties(
                    family="DejaVu Sans",
                    style="normal",
                    weight=weight,
                ),
                fallback_to_default=False,
            )
        )
        for weight in ("normal", "bold")
    }
    fonts = sorted(
        (_font_file_binding(font_path, matplotlib_data_root) for font_path in font_paths),
        key=lambda item: str(item["path"]),
    )
    cache_tag = sys.implementation.cache_tag
    if cache_tag is None:
        raise GuardArtifactError("Python runtime does not expose a cache tag")
    return {
        "project_inputs": {
            "pyproject": _source_binding(REPOSITORY_ROOT / "pyproject.toml"),
            "uv_lock": _source_binding(REPOSITORY_ROOT / "uv.lock"),
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "cache_tag": cache_tag,
        },
        "packages": {
            "numpy_version": np.__version__,
            "matplotlib_version": matplotlib.__version__,
        },
        "rendering": {
            "backend": matplotlib.get_backend(),
            "figure_canvas": f"{FigureCanvasAgg.__module__}.{FigureCanvasAgg.__qualname__}",
            "renderer": f"{RendererAgg.__module__}.{RendererAgg.__qualname__}",
            "font_files": fonts,
        },
    }


def _require_generation_environment() -> dict[str, Any]:
    observed = _generation_environment()
    if observed != _expected_generation_environment():
        raise GuardArtifactError(
            "generation environment drifted from the frozen byte-exact replay contract"
        )
    return observed


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _complex_from_document(value: object, label: str) -> complex:
    item = _mapping(value, label)
    return complex(
        _number(item.get("real"), f"{label}.real"),
        _number(item.get("imag"), f"{label}.imag"),
    )


def _phase_delta_deg(first: complex, second: complex) -> float:
    return abs(math.degrees(np.angle(first * np.conj(second))))


def _select_repeatability_cohort(document: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Select repeat-1 through repeat-20, excluding the separate baseline."""

    if document.get("schema") != 1 or document.get("analysis_kind") != (
        "rotation0_broadband_repeatability"
    ):
        raise GuardArtifactError("repeatability document identity is not canonical")
    identity = _mapping(document.get("identity"), "repeatability.identity")
    if (
        identity.get("board_id") != DEFAULT_BOARD_ID
        or identity.get("profile_id") != "fast20-v1"
        or identity.get("profile_contract_sha256") != EXPECTED_PROFILE_CONTRACT_SHA256
    ):
        raise GuardArtifactError("repeatability identity disagrees with the fixed cohort")
    scope = _mapping(document.get("scope"), "repeatability.scope")
    expected_labels = [f"repeat-{index}" for index in range(1, EXPECTED_CAPTURE_COUNT + 1)]
    if (
        scope.get("baseline_run_label") != "baseline"
        or scope.get("repeat_run_labels") != expected_labels
        or scope.get("rotation") != 0
        or scope.get("frequency_max_hz") != EXPECTED_CENTER_FREQUENCY_HZ
    ):
        raise GuardArtifactError("repeatability scope is not the fixed twenty-repeat design")
    source_runs = _sequence(document.get("source_runs"), "repeatability.source_runs")
    if len(source_runs) != EXPECTED_CAPTURE_COUNT + 1:
        raise GuardArtifactError("repeatability source-run count is not baseline plus twenty")
    baseline = _mapping(source_runs[0], "repeatability baseline")
    if baseline.get("label") != "baseline":
        raise GuardArtifactError("repeatability first source is not the excluded baseline")
    selected: list[dict[str, str]] = []
    for index, raw_run in enumerate(source_runs[1:], start=1):
        run = _mapping(raw_run, f"repeatability repeat-{index}")
        label = f"repeat-{index}"
        run_id = _string(run.get("run_id"), f"{label}.run_id")
        if run.get("label") != label:
            raise GuardArtifactError(f"{label} identity is malformed")
        _sequence(run.get("failed_attempts"), f"{label}.failed_attempts")
        rows = [
            _mapping(row, f"{label}.source_analysis")
            for row in _sequence(run.get("source_analyses"), f"{label}.source_analyses")
            if _mapping(row, f"{label}.source_analysis").get("center_frequency_hz")
            == EXPECTED_CENTER_FREQUENCY_HZ
        ]
        if len(rows) != 1:
            raise GuardArtifactError(f"{label} does not name exactly one 5.8 GHz artifact")
        row = rows[0]
        selected.append(
            {
                "label": label,
                "run_id": run_id,
                "artifact_id": _string(row.get("artifact_id"), f"{label}.artifact_id"),
                "raw_data_sha256": _string(row.get("artifact_sha256"), f"{label}.artifact_sha256"),
                "reference_sidecar_sha256": _string(
                    row.get("analysis_sha256"), f"{label}.analysis_sha256"
                ),
            }
        )
    if len({item["artifact_id"] for item in selected}) != EXPECTED_CAPTURE_COUNT:
        raise GuardArtifactError("repeatability cohort reuses an artifact")
    return tuple(selected)


def _inventory_by_artifact(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if document.get("schema") != 1 or document.get("evidence_kind") != (
        "exact_5g8_local_raw_evidence_inventory"
    ):
        raise GuardArtifactError("evidence inventory identity is not canonical")
    scope = _mapping(document.get("scope"), "inventory.scope")
    contract = _mapping(document.get("validation_contract"), "inventory.validation_contract")
    if (
        scope.get("board_id") != DEFAULT_BOARD_ID
        or scope.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
        or contract.get("datatype") != "ci16_le"
        or contract.get("channels") != [0, 1]
        or contract.get("receiver_count") != EXPECTED_RECEIVER_COUNT
        or contract.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
    ):
        raise GuardArtifactError("evidence inventory scope/contract is not canonical")
    captures = _sequence(document.get("captures"), "inventory.captures")
    result: dict[str, Mapping[str, Any]] = {}
    for raw in captures:
        capture = _mapping(raw, "inventory capture")
        artifact_id = _string(capture.get("artifact_id"), "inventory artifact ID")
        if artifact_id in result:
            raise GuardArtifactError("evidence inventory contains a duplicate artifact ID")
        result[artifact_id] = capture
    return result


def _bind_cohort_to_inventory(
    cohort: Sequence[Mapping[str, str]],
    inventory: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    bound: list[dict[str, Any]] = []
    for source in cohort:
        artifact_id = source["artifact_id"]
        capture = inventory.get(artifact_id)
        if capture is None:
            raise GuardArtifactError(f"cohort artifact absent from inventory: {artifact_id}")
        companions = {
            _string(item.get("analysis_kind"), "companion kind"): _mapping(
                item, "companion analysis"
            )
            for raw_item in _sequence(capture.get("companion_analyses"), "companions")
            for item in (_mapping(raw_item, "companion analysis"),)
        }
        reference = companions.get("fast20_dual_rx_ota_reference_transfer")
        if reference is None:
            raise GuardArtifactError(f"cohort artifact lacks reference sidecar: {artifact_id}")
        continuity = _mapping(capture.get("continuity"), "inventory continuity")
        if (
            capture.get("family") != "rotation0_broadband_sweep"
            or not str(capture.get("role", "")).startswith(source["run_id"] + ":")
            or capture.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
            or capture.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
            or capture.get("sample_count") != EXPECTED_SAMPLE_COUNT
            or capture.get("receiver_count") != EXPECTED_RECEIVER_COUNT
            or capture.get("datatype") != "ci16_le"
            or capture.get("tx_channel") != 0
            or capture.get("tx_hardware_gain_db") != -20.0
            or capture.get("raw_data_size_bytes") != EXPECTED_RAW_BYTES
            or capture.get("raw_data_sha256") != source["raw_data_sha256"]
            or reference.get("sha256") != source["reference_sidecar_sha256"]
            or continuity.get("present") is not True
            or continuity.get("validated") is not True
        ):
            raise GuardArtifactError(f"cohort/inventory binding failed for {artifact_id}")
        bound.append({"source": dict(source), "inventory": capture, "reference": reference})
    return tuple(bound)


def _resolve_under(root: Path, relative: str, label: str) -> Path:
    path = (root / relative).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise GuardArtifactError(f"{label} escapes the board-state root") from error
    if not path.is_file():
        raise GuardArtifactError(f"{label} is not a file")
    return path


def _validate_metadata(
    metadata: Mapping[str, Any],
    *,
    artifact_id: str,
    raw_sha256: str,
) -> dict[str, int | None]:
    global_metadata = _mapping(metadata.get("global"), "SigMF global")
    capture_metadata = _mapping(metadata.get("pluto:capture"), "SigMF capture")
    captures = _sequence(metadata.get("captures"), "SigMF captures")
    if len(captures) != 1:
        raise GuardArtifactError("SigMF metadata must contain exactly one capture record")
    settings = _mapping(_mapping(captures[0], "SigMF capture record").get("settings"), "settings")
    if (
        global_metadata.get("pluto:artifact_id") != artifact_id
        or global_metadata.get("pluto:sha256") != raw_sha256
        or global_metadata.get("core:datatype") != "ci16_le"
        or global_metadata.get("core:num_channels") != EXPECTED_RECEIVER_COUNT
        or settings.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
        or settings.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or settings.get("channels") != [0, 1]
        or capture_metadata.get("sample_count") != EXPECTED_SAMPLE_COUNT
        or capture_metadata.get("receiver_count") != EXPECTED_RECEIVER_COUNT
    ):
        raise GuardArtifactError("SigMF identity/settings disagree with the fixed capture")
    try:
        return validate_sigmf_continuity(
            metadata, expected_total_samples=EXPECTED_SAMPLE_COUNT
        ).as_dict()
    except ValueError as error:
        raise GuardArtifactError(f"strict ABI-2 continuity validation failed: {error}") from error


def _validate_reference_sidecar(
    document: Mapping[str, Any],
    *,
    artifact_id: str,
    raw_sha256: str,
    continuity: Mapping[str, int | None],
    profile: ControlProfile,
) -> dict[str, Any]:
    if (
        document.get("schema") != 1
        or document.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer"
    ):
        raise GuardArtifactError("reference sidecar identity is not canonical")
    artifact = _mapping(document.get("artifact"), "sidecar.artifact")
    capture = _mapping(document.get("capture"), "sidecar.capture")
    aggregation = _mapping(document.get("aggregation_key"), "sidecar.aggregation_key")
    pilot = _mapping(document.get("pilot"), "sidecar.pilot")
    transfer = _mapping(document.get("transfer"), "sidecar.transfer")
    quality = _mapping(document.get("quality_gate"), "sidecar.quality_gate")
    if (
        artifact.get("artifact_id") != artifact_id
        or artifact.get("sha256") != raw_sha256
        or artifact.get("sample_count") != EXPECTED_SAMPLE_COUNT
        or artifact.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or artifact.get("receiver_count") != EXPECTED_RECEIVER_COUNT
        or artifact.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
        or capture.get("sample_count") != EXPECTED_SAMPLE_COUNT
        or capture.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or capture.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
        or capture.get("metadata_abi") != 2
        or capture.get("profile_contract_sha256") != profile.contract_sha256
        or capture.get("stream_id") != continuity["stream_id"]
        or capture.get("first_sample_sequence") != continuity["first_sample_sequence"]
        or capture.get("last_sample_sequence_exclusive")
        != continuity["last_sample_sequence_exclusive"]
        or capture.get("tx_channel") != 0
        or capture.get("tx_gain_readback_db") != -20.0
        or capture.get("stimulus") != "qualification"
        or capture.get("fully_conducted_user_confirmation") is not True
        or capture.get("conducted_fixture_id") != "tx1-2way-rx1-and-8way-board-rx2-v1"
        or aggregation.get("artifact_id") != artifact_id
        or aggregation.get("center_frequency_hz") != EXPECTED_CENTER_FREQUENCY_HZ
        or aggregation.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or aggregation.get("stream_id") != continuity["stream_id"]
        or aggregation.get("tx_channel") != 0
        or quality.get("passed") is not True
        or quality.get("global_rejection_reasons") != []
        or transfer.get("continuity_verified") is not True
        or transfer.get("continuity_block_count") != continuity["block_count"]
        or transfer.get("complete_cycle_count") != EXPECTED_COMPLETE_CYCLES
        or _number(transfer.get("alignment_score"), "alignment score")
        < MINIMUM_RETAINED_ALIGNMENT_SCORE
        or _number(transfer.get("alignment_even_odd_agreement"), "alignment agreement")
        < MINIMUM_RETAINED_ALIGNMENT_EVEN_ODD_AGREEMENT
        or _number(transfer.get("reference_valid_bin_fraction"), "reference fraction")
        < MINIMUM_REFERENCE_VALID_FRACTION
    ):
        raise GuardArtifactError("reference sidecar binding/quality contract failed")
    if (
        _number(pilot.get("confidence"), "pilot confidence") < MINIMUM_PILOT_CONFIDENCE
        or _number(pilot.get("phase_step_coherence"), "pilot phase coherence")
        < MINIMUM_PILOT_PHASE_STEP_COHERENCE
        or _number(pilot.get("phase_residual_rms_rad"), "pilot phase residual")
        > MAXIMUM_PILOT_PHASE_RESIDUAL_RMS_RAD
    ):
        raise GuardArtifactError("reference sidecar pilot fails the fixed gate")
    return {
        "created_at": _string(artifact.get("created_at"), "artifact creation time"),
        "pilot_offset_hz": _number(pilot.get("estimated_offset_hz"), "pilot offset"),
        "pilot_confidence": _number(pilot.get("confidence"), "pilot confidence"),
        "pilot_phase_step_coherence": _number(
            pilot.get("phase_step_coherence"), "pilot phase coherence"
        ),
        "pilot_phase_residual_rms_rad": _number(
            pilot.get("phase_residual_rms_rad"), "pilot phase residual"
        ),
        "cycle_ms": _number(transfer.get("cycle_ms"), "cycle_ms"),
        "marker_phase_ms": _number(transfer.get("marker_phase_ms"), "marker_phase_ms"),
        "alignment_score": _number(transfer.get("alignment_score"), "alignment score"),
        "alignment_even_odd_agreement": _number(
            transfer.get("alignment_even_odd_agreement"), "alignment agreement"
        ),
        "retained_h_off": _complex_from_document(
            _mapping(
                _mapping(transfer.get("all_off"), "sidecar all_off").get("raw_rx2_over_rx1"),
                "sidecar raw all_off",
            ).get("phasor"),
            "retained H_off",
        ),
    }


def _coherent_transfer_bins(
    data_path: Path,
    *,
    sample_count: int,
    sample_rate_hz: float,
    tone_offset_hz: float,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.bool_], float]:
    """Stream canonical CI16 into 0.1 ms RX2/RX1 coherent-transfer bins."""

    if sample_count % COHERENT_BIN_SAMPLES:
        raise GuardArtifactError("sample count is not divisible by coherent bin size")
    raw = np.memmap(data_path, dtype="<i2", mode="r")
    expected_components = sample_count * EXPECTED_RECEIVER_COUNT * 2
    if raw.size != expected_components:
        raise GuardArtifactError("raw CI16 component count disagrees with metadata")
    components = raw.reshape(sample_count, EXPECTED_RECEIVER_COUNT, 2)
    bin_count = sample_count // COHERENT_BIN_SAMPLES
    transfer = np.empty(bin_count, dtype=np.complex128)
    reference_amplitude = np.empty(bin_count, dtype=np.float64)
    oscillator = np.exp(
        -2j
        * np.pi
        * tone_offset_hz
        * np.arange(COHERENT_BIN_SAMPLES, dtype=np.float64)
        / sample_rate_hz
    )
    bins_per_chunk = 2_500
    for bin_start in range(0, bin_count, bins_per_chunk):
        bin_stop = min(bin_count, bin_start + bins_per_chunk)
        sample_start = bin_start * COHERENT_BIN_SAMPLES
        sample_stop = bin_stop * COHERENT_BIN_SAMPLES
        shaped = (
            components[sample_start:sample_stop]
            .astype(np.float64)
            .reshape(
                bin_stop - bin_start,
                COHERENT_BIN_SAMPLES,
                EXPECTED_RECEIVER_COUNT,
                2,
            )
        )
        reference = shaped[:, :, 0, 0] + 1j * shaped[:, :, 0, 1]
        measurement = shaped[:, :, 1, 0] + 1j * shaped[:, :, 1, 1]
        reference_phasor = np.mean(reference * oscillator, axis=1)
        measurement_phasor = np.mean(measurement * oscillator, axis=1)
        reference_amplitude[bin_start:bin_stop] = np.abs(reference_phasor)
        transfer[bin_start:bin_stop] = measurement_phasor / reference_phasor
    threshold = 0.2 * float(np.median(reference_amplitude))
    reference_valid = reference_amplitude >= threshold
    fraction = float(np.mean(reference_valid))
    if fraction < MINIMUM_REFERENCE_VALID_FRACTION:
        raise GuardArtifactError("RX1 reference is not continuously usable")
    return transfer, reference_valid, fraction


def _stratum_summary(
    analysis: CaptureStratification,
    *,
    name: str,
) -> dict[str, Any]:
    raw_values = analysis.raw_cycle_residuals[name]
    raw_center = robust_complex_center(raw_values)
    result: dict[str, Any] = {
        "raw_residual_center": _complex_document(raw_center),
        "raw_residual_amplitude_fraction_of_h_off": abs(raw_center),
        "raw_cycle_phase_coherence": phase_coherence(raw_values),
        "raw_cycle_median_amplitude_fraction_of_h_off": float(np.median(np.abs(raw_values))),
    }
    adjusted_values = analysis.adjusted_cycle_residuals.get(name)
    if adjusted_values is not None:
        adjusted_center = robust_complex_center(adjusted_values)
        result.update(
            {
                "control_adjusted_center": _complex_document(adjusted_center),
                "control_adjusted_amplitude_fraction_of_h_off": abs(adjusted_center),
                "control_adjusted_cycle_phase_coherence": phase_coherence(adjusted_values),
                "control_adjusted_cycle_median_amplitude_fraction_of_h_off": float(
                    np.median(np.abs(adjusted_values))
                ),
                "control_adjusted_cycle_p90_amplitude_fraction_of_h_off": float(
                    np.quantile(np.abs(adjusted_values), 0.9)
                ),
            }
        )
    return result


def _analyze_bound_capture(
    item: Mapping[str, Any],
    *,
    board_root: Path,
    profile: ControlProfile,
) -> dict[str, Any]:
    source = _mapping(item.get("source"), "bound source")
    inventory = _mapping(item.get("inventory"), "bound inventory")
    reference_record = _mapping(item.get("reference"), "bound reference record")
    artifact_id = _string(source.get("artifact_id"), "artifact ID")
    data_path = _resolve_under(
        board_root,
        _string(inventory.get("data_path"), "raw data path"),
        "raw data",
    )
    metadata_path = _resolve_under(
        board_root,
        _string(inventory.get("metadata_path"), "metadata path"),
        "SigMF metadata",
    )
    sidecar_path = _resolve_under(
        board_root,
        _string(reference_record.get("path"), "reference sidecar path"),
        "reference sidecar",
    )
    if data_path.parent.name != artifact_id or metadata_path.parent != data_path.parent:
        raise GuardArtifactError("artifact paths do not match the cohort artifact ID")
    if data_path.stat().st_size != EXPECTED_RAW_BYTES:
        raise GuardArtifactError("raw data byte size is not canonical")
    raw_sha256 = _sha256_stream(data_path)
    metadata_sha256 = _sha256_stream(metadata_path)
    sidecar_sha256 = _sha256_stream(sidecar_path)
    if (
        raw_sha256 != source.get("raw_data_sha256")
        or raw_sha256 != inventory.get("raw_data_sha256")
        or metadata_sha256 != inventory.get("metadata_sha256")
        or sidecar_sha256 != source.get("reference_sidecar_sha256")
        or sidecar_sha256 != reference_record.get("sha256")
    ):
        raise GuardArtifactError(f"artifact file hash binding failed for {artifact_id}")
    metadata = _read_json(metadata_path, "SigMF metadata")
    continuity = _validate_metadata(metadata, artifact_id=artifact_id, raw_sha256=raw_sha256)
    inventory_continuity = _mapping(inventory.get("continuity"), "inventory continuity")
    if continuity != _mapping(inventory_continuity.get("summary"), "continuity summary"):
        raise GuardArtifactError("recomputed continuity disagrees with the evidence inventory")
    sidecar = _read_json(sidecar_path, "reference sidecar")
    retained = _validate_reference_sidecar(
        sidecar,
        artifact_id=artifact_id,
        raw_sha256=raw_sha256,
        continuity=continuity,
        profile=profile,
    )
    transfer, reference_valid, reference_valid_fraction = _coherent_transfer_bins(
        data_path,
        sample_count=EXPECTED_SAMPLE_COUNT,
        sample_rate_hz=EXPECTED_SAMPLE_RATE_HZ,
        tone_offset_hz=float(retained["pilot_offset_hz"]),
    )
    bin_duration_ms = COHERENT_BIN_SAMPLES * 1_000.0 / EXPECTED_SAMPLE_RATE_HZ
    times_ms = (np.arange(transfer.size, dtype=np.float64) + 0.5) * bin_duration_ms
    analysis = stratify_all_off_transfer(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=EXPECTED_SAMPLE_COUNT * 1_000.0 / EXPECTED_SAMPLE_RATE_HZ,
        cycle_ms=float(retained["cycle_ms"]),
        marker_phase_ms=float(retained["marker_phase_ms"]),
        profile=profile,
        marker_anchor_window_ms=MARKER_ANCHOR_WINDOW_MS,
        center_window_after_entry_ms=CENTER_WINDOW_AFTER_ENTRY_MS,
        minimum_complete_cycles=20,
    )
    if (
        analysis.complete_cycle_count != EXPECTED_COMPLETE_CYCLES
        or analysis.analyzed_cycle_count != EXPECTED_ANALYZED_CYCLES
    ):
        raise GuardArtifactError("recomputed complete/analyzed cycle counts are not canonical")
    retained_h_off = complex(retained["retained_h_off"])
    relative_error = abs(analysis.h_off - retained_h_off) / abs(retained_h_off)
    phase_error_deg = _phase_delta_deg(analysis.h_off, retained_h_off)
    if (
        relative_error > MAXIMUM_MARKER_VERSUS_RETAINED_H_OFF_RELATIVE_ERROR
        or phase_error_deg > MAXIMUM_MARKER_VERSUS_RETAINED_H_OFF_PHASE_ERROR_DEG
    ):
        raise GuardArtifactError("marker-only and retained pooled H_off estimates disagree")
    stratum_names = [*analysis.adjusted_cycle_residuals, analysis.control_name]
    return {
        "repeat_label": source["label"],
        "run_id": source["run_id"],
        "artifact_id": artifact_id,
        "created_at": retained["created_at"],
        "raw_data_path": inventory["data_path"],
        "raw_data_sha256": raw_sha256,
        "raw_data_bytes": EXPECTED_RAW_BYTES,
        "metadata_path": inventory["metadata_path"],
        "metadata_sha256": metadata_sha256,
        "reference_sidecar_path": reference_record["path"],
        "reference_sidecar_sha256": sidecar_sha256,
        "continuity": continuity,
        "pilot": {
            "estimated_offset_hz": retained["pilot_offset_hz"],
            "confidence": retained["pilot_confidence"],
            "phase_step_coherence": retained["pilot_phase_step_coherence"],
            "phase_residual_rms_rad": retained["pilot_phase_residual_rms_rad"],
        },
        "alignment": {
            "cycle_ms": retained["cycle_ms"],
            "marker_phase_ms": retained["marker_phase_ms"],
            "score": retained["alignment_score"],
            "even_odd_agreement": retained["alignment_even_odd_agreement"],
            "complete_cycle_count": analysis.complete_cycle_count,
            "analyzed_cycle_count": analysis.analyzed_cycle_count,
        },
        "reference_valid_bin_fraction": reference_valid_fraction,
        "marker_h_off": _complex_document(analysis.h_off),
        "retained_pooled_h_off": _complex_document(retained_h_off),
        "marker_versus_retained_h_off": {
            "complex_relative_error": relative_error,
            "phase_error_deg": phase_error_deg,
        },
        "negative_control_name": analysis.control_name,
        "strata": {name: _stratum_summary(analysis, name=name) for name in stratum_names},
    }


def aggregate_from_capture_summaries(
    captures: Sequence[Mapping[str, Any]],
    *,
    thresholds: DetectionThresholds = DETECTION_THRESHOLDS,
) -> dict[str, Any]:
    """Recompute the durable cross-capture decision from compact capture centers."""

    if len(captures) != EXPECTED_CAPTURE_COUNT:
        raise GuardArtifactError("aggregate requires exactly twenty capture summaries")
    control_names = {
        _string(capture.get("negative_control_name"), "negative control name")
        for capture in captures
    }
    if len(control_names) != 1:
        raise GuardArtifactError("capture summaries disagree on the negative control")
    control_name = next(iter(control_names))
    first_strata = _mapping(captures[0].get("strata"), "first capture strata")
    differential_names = [name for name in first_strata if name != control_name]
    if len(differential_names) != 8:
        raise GuardArtifactError("capture summaries must contain eight differential strata")
    centers: dict[str, list[complex]] = {name: [] for name in differential_names}
    control_centers: list[complex] = []
    for capture in captures:
        strata = _mapping(capture.get("strata"), "capture strata")
        if set(strata) != set(first_strata):
            raise GuardArtifactError("capture summaries have inconsistent strata")
        control = _mapping(strata[control_name], "negative control")
        control_centers.append(
            _complex_from_document(control.get("raw_residual_center"), "control center")
        )
        for name in differential_names:
            stratum = _mapping(strata[name], f"stratum {name}")
            centers[name].append(
                _complex_from_document(
                    stratum.get("control_adjusted_center"), f"{name} adjusted center"
                )
            )
    aggregate = dict(aggregate_capture_centers(centers, thresholds=thresholds))
    control_center = robust_complex_center(control_centers)
    aggregate["negative_control"] = {
        "name": control_name,
        "robust_center": _complex_document(control_center),
        "robust_amplitude_fraction_of_h_off": abs(control_center),
        "robust_amplitude_percent_of_h_off": 100.0 * abs(control_center),
        "median_capture_amplitude_fraction_of_h_off": float(np.median(np.abs(control_centers))),
        "cross_capture_phase_coherence": phase_coherence(control_centers),
    }
    return aggregate


def _figure_font_bindings(figure: Any, matplotlib_data_root: Path) -> list[dict[str, str | int]]:
    try:
        from matplotlib import font_manager
        from matplotlib.text import Text
    except ImportError as error:  # pragma: no cover - guarded by the caller.
        raise GuardArtifactError("font attestation requires Matplotlib") from error
    figure.canvas.draw()
    font_paths = {
        Path(font_manager.findfont(text.get_fontproperties(), fallback_to_default=False))
        for text in figure.findobj(match=Text)
        if text.get_text()
    }
    return sorted(
        (_font_file_binding(font_path, matplotlib_data_root) for font_path in font_paths),
        key=lambda item: str(item["path"]),
    )


def _render_figure(
    aggregate: Mapping[str, Any],
    path: Path,
    *,
    generation_environment: Mapping[str, Any],
) -> dict[str, str]:
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise GuardArtifactError("figure rendering requires the report dependency group") from error
    with plt.rc_context():
        plt.rcdefaults()
        plt.rcParams.update(
            {
                "font.family": "DejaVu Sans",
                "font.sans-serif": ["DejaVu Sans"],
                "font.style": "normal",
                "font.weight": "normal",
            }
        )
        rows = [
            _mapping(row, "aggregate stratum")
            for row in _sequence(aggregate.get("strata"), "aggregate strata")
        ]
        labels = [
            str(row["name"])
            .replace("marker_entry_after_", "marker after ")
            .replace("after_", "after ")
            for row in rows
        ]
        amplitudes = np.asarray(
            [_number(row.get("robust_amplitude_percent_of_h_off"), "amplitude") for row in rows]
        )
        coherence = np.asarray(
            [_number(row.get("cross_capture_phase_coherence"), "coherence") for row in rows]
        )
        thresholds = _mapping(aggregate.get("thresholds"), "aggregate thresholds")
        amplitude_threshold = _number(
            thresholds.get("minimum_amplitude_percent_of_h_off"), "amplitude threshold"
        )
        coherence_threshold = _number(
            thresholds.get("minimum_cross_capture_phase_coherence"), "coherence threshold"
        )
        colors = [
            "#d62728" if row.get("persistent_signature_detected") else "#4c78a8" for row in rows
        ]
        x = np.arange(len(rows))
        fig, axes = plt.subplots(2, 1, figsize=(10.2, 6.8), sharex=True)
        axes[0].bar(x, amplitudes, color=colors)
        axes[0].axhline(
            amplitude_threshold,
            color="#d62728",
            linestyle="--",
            linewidth=1.2,
            label=f"chosen resolution gate ({amplitude_threshold:.1f}% of H_off)",
        )
        axes[0].set_ylabel("Control-adjusted amplitude\n(% of H_off)")
        axes[0].legend(loc="upper right")
        axes[1].bar(x, coherence, color=colors)
        axes[1].axhline(
            coherence_threshold,
            color="#d62728",
            linestyle="--",
            linewidth=1.2,
            label=f"phase-coherence gate ({coherence_threshold:.2f})",
        )
        axes[1].set(ylabel="Cross-capture phase coherence", ylim=(0.0, 1.02))
        axes[1].legend(loc="upper right")
        axes[1].set_xticks(x, labels, rotation=28, ha="right")
        for axis in axes:
            axis.grid(axis="y", alpha=0.25)
        decision = bool(aggregate.get("persistent_selector_synchronous_signature_detected"))
        fig.suptitle(
            (
                "Persistent selector-synchronous ALL_OFF signature detected"
                if decision
                else "No persistent selector-synchronous ALL_OFF signature resolved at 5.8 GHz"
            ),
            fontweight="bold",
        )
        fig.text(
            0.5,
            0.01,
            "20 raw repeats · 23 analyzed cycles each · central 2–3 ms · pre-ANT1 control",
            ha="center",
            fontsize=9,
        )
        fig.tight_layout(rect=(0.0, 0.05, 1.0, 0.95))
        actual_fonts = _figure_font_bindings(fig, Path(matplotlib.get_data_path()))
        rendering = _mapping(generation_environment.get("rendering"), "generation rendering")
        expected_fonts = list(
            _sequence(rendering.get("font_files"), "generation rendering font files")
        )
        if actual_fonts != expected_fonts:
            plt.close(fig)
            raise GuardArtifactError("rendered figure font use drifted from the bound font files")
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=180, bbox_inches="tight", metadata={"Software": "smateway"})
        plt.close(fig)
    return {"path": _reported_repository_path(path), "sha256": sha256_path(path)}


def build_document(
    *,
    board_root: Path,
    inventory_path: Path,
    repeatability_path: Path,
    profile_path: Path,
    figure_path: Path,
) -> dict[str, Any]:
    figure_path = _canonical_figure_path(figure_path)
    generation_environment = _require_generation_environment()
    _require_sha256(inventory_path, EXPECTED_INVENTORY_SHA256, "evidence inventory")
    _require_sha256(repeatability_path, EXPECTED_REPEATABILITY_SHA256, "repeatability result")
    _require_sha256(profile_path, EXPECTED_PROFILE_SHA256, "control profile")
    profile_header = profile_path.with_name("control_profile.h")
    _require_sha256(profile_header, EXPECTED_PROFILE_HEADER_SHA256, "control profile header")
    profile = load_profile(profile_path)
    if profile.contract_sha256 != EXPECTED_PROFILE_CONTRACT_SHA256:
        raise GuardArtifactError("control profile contract SHA-256 is not canonical")
    repeatability = _read_json(repeatability_path, "repeatability result")
    inventory_document = _read_json(inventory_path, "evidence inventory")
    cohort = _select_repeatability_cohort(repeatability)
    inventory = _inventory_by_artifact(inventory_document)
    bound = _bind_cohort_to_inventory(cohort, inventory)
    captures = [
        _analyze_bound_capture(item, board_root=board_root, profile=profile) for item in bound
    ]
    aggregate = aggregate_from_capture_summaries(captures)
    figure = _render_figure(
        aggregate,
        figure_path,
        generation_environment=generation_environment,
    )
    implementation_sources = [
        _source_binding(Path(__file__)),
        _source_binding(Path(guard_stratification_library.__file__)),
        _source_binding(Path(capture_continuity_library.__file__)),
        _source_binding(Path(profile_library.__file__)),
        _source_binding(Path(schedule_alignment_library.__file__)),
        _source_binding(Path(hexcal_library.__file__)),
    ]
    return {
        "schema": 1,
        "analysis_kind": "selector_synchronous_5g8_all_off_guard_stratification",
        "status": "offline_diagnostic_not_physical_attribution",
        "generation_environment": generation_environment,
        "source": {
            "board_id": board_root.name,
            "raw_storage_root_embedded": False,
            "evidence_inventory": _source_binding(inventory_path),
            "repeatability_result": _source_binding(repeatability_path),
            "control_profile": _source_binding(profile_path),
            "control_profile_header": _source_binding(profile_header),
            "implementation_sources": implementation_sources,
            "cohort_selection": (
                "repeat-1 through repeat-20 source_runs from the committed 20-pass result; "
                "the separate baseline run is excluded; exact 5.8 GHz only"
            ),
        },
        "method": {
            "center_frequency_hz": EXPECTED_CENTER_FREQUENCY_HZ,
            "capture_count": EXPECTED_CAPTURE_COUNT,
            "sample_rate_hz": EXPECTED_SAMPLE_RATE_HZ,
            "sample_count_per_capture": EXPECTED_SAMPLE_COUNT,
            "samples_per_coherent_bin": COHERENT_BIN_SAMPLES,
            "coherent_bin_duration_ms": (COHERENT_BIN_SAMPLES * 1_000.0 / EXPECTED_SAMPLE_RATE_HZ),
            "marker_anchor_window_ms": list(MARKER_ANCHOR_WINDOW_MS),
            "center_window_after_all_off_entry_ms": list(CENTER_WINDOW_AFTER_ENTRY_MS),
            "complete_cycles_per_capture": EXPECTED_COMPLETE_CYCLES,
            "analyzed_cycles_per_capture": EXPECTED_ANALYZED_CYCLES,
            "marker_baseline": (
                "component-wise median in marker window; complex linear interpolation between "
                "bracketing marker anchors"
            ),
            "normalization": "each capture residual divided by its marker-only complex H_off",
            "negative_control": (
                "central 2-3 ms of the contiguous pre-ANT1 ALL_OFF guard; its nominal start "
                "has no RF state transition"
            ),
            "differential_strata": (
                "seven ordinary guards after ANT1-ANT7 plus the first marker interval after "
                "ANT8; marker_entry_after_ANT8 is not labeled a protocol post-ANT8 guard"
            ),
            "capture_center": "component-wise median across analyzed cycles",
            "cross_capture_center": "component-wise median across exactly twenty captures",
            "decision_contract": (
                "chosen resolution threshold and phase-coherence threshold must both pass; "
                "this is a bounded detection contract, not proof that smaller effects are absent"
            ),
        },
        "captures": captures,
        "aggregate": aggregate,
        "figure": figure,
        "interpretation": {
            "persistent_selector_synchronous_signature_detected": aggregate[
                "persistent_selector_synchronous_signature_detected"
            ],
            "proven": (
                "No central ALL_OFF stratum in the fixed twenty-repeat corpus satisfies both "
                "the chosen 0.5%-of-H_off amplitude resolution gate and 0.75 cross-capture "
                "phase-coherence gate."
                if not aggregate["persistent_selector_synchronous_signature_detected"]
                else "At least one central ALL_OFF stratum satisfies both fixed detection gates."
            ),
            "positive_result_scope": (
                "A positive differential can identify only a selector/control-state-dependent "
                "or fixture-interaction contribution; it does not by itself locate that term."
            ),
            "null_result_limit": (
                "A null cannot distinguish Pluto-internal/direct-field leakage from "
                "state-independent RX2-cable or selector-common-launch leakage."
            ),
            "physical_root_cause_identified": False,
            "required_next_discriminant": (
                "Stage A direct-RX2 termination followed by Stage B cable and Stage C powered "
                "selector-common boundary captures"
            ),
            "calibration_admissible": False,
        },
    }


def main() -> int:
    args = _parser().parse_args()
    figure_path = _canonical_figure_path(args.figure)
    if Path(args.board_id).name != args.board_id or args.board_id in {"", ".", ".."}:
        raise GuardArtifactError("board ID must be one nonempty path component")
    board_state_root = args.board_state_root.expanduser().resolve(strict=True)
    board_root = (board_state_root / args.board_id).resolve(strict=True)
    if board_root.parent != board_state_root or not board_root.is_dir():
        raise GuardArtifactError("board ID does not identify one board-state directory")
    document = build_document(
        board_root=board_root,
        inventory_path=args.inventory.expanduser().resolve(strict=True),
        repeatability_path=args.repeatability.expanduser().resolve(strict=True),
        profile_path=args.profile.expanduser().resolve(strict=True),
        figure_path=figure_path,
    )
    output = args.output.expanduser().resolve()
    write_json_atomic(output, document)
    aggregate = _mapping(document["aggregate"], "aggregate")
    maximum_amplitude = max(
        _number(row.get("robust_amplitude_percent_of_h_off"), "aggregate amplitude")
        for row in (
            _mapping(item, "aggregate stratum")
            for item in _sequence(aggregate.get("strata"), "aggregate strata")
        )
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "output_sha256": sha256_path(output),
                "figure": str(figure_path),
                "figure_sha256": _mapping(document["figure"], "figure")["sha256"],
                "capture_count": len(document["captures"]),
                "maximum_control_adjusted_amplitude_percent_of_h_off": maximum_amplitude,
                "persistent_signature_detected": aggregate[
                    "persistent_selector_synchronous_signature_detected"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
