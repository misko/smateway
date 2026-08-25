#!/usr/bin/env python3
"""Render the retained multi-frequency localization report from immutable JSON inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg", force=True)

import matplotlib.pyplot as plt
import numpy as np
import numpy.typing as npt
from matplotlib import colors as mcolors
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Circle, Rectangle, Wedge

from smateway.frequency_slope_localization import (
    AnchoredArrayGeometry,
    predict_double_relative_phase_deg,
    wrap_phase_deg,
)

REPOSITORY = Path(__file__).resolve().parents[1]
BOARD_ID = "stm32c011-4c0055000950313950363920"
RUN_ID = "multifrequency-phase-20260825-b"
DEFAULT_RUN_DIRECTORY = (
    Path.home()
    / ".local/state/smateway/boards"
    / BOARD_ID
    / "phase-distributions"
    / RUN_ID
)
DEFAULT_GEOMETRY = REPOSITORY / "profiles/phase20-v1/array_geometry.json"
DEFAULT_OUTPUT_ROOT = REPOSITORY / "docs/localization"
DEFAULT_RF_RELEASE_REPORT = Path(
    "/home/pi/gits/circuits/projects/pluto-rx2-8way-v5/07_releases/"
    "v0.2.1-2026-08-14/verification/rf/realized/report.json"
)

DIRECT_NAME = "analysis-multifrequency-direct-nominal-2m.json"
PRIMARY_NAME = "analysis-anchored-slope-sys10-exclude2458-2m.json"
ALL_FREQUENCY_NAME = "analysis-anchored-slope-sys10-2m-v2.json"
CONSERVATIVE_NAME = "analysis-anchored-slope-sys40-2m.json"
OUTLIER_FREQUENCY_HZ = 2_458_000_000
EXPECTED_FREQUENCIES_HZ = (
    2_400_000_000,
    2_409_000_000,
    2_423_000_000,
    2_440_000_000,
    OUTLIER_FREQUENCY_HZ,
    2_472_000_000,
    2_483_000_000,
)
STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 9))

RELEASED_RF_COMMON_LENGTH_MM = 14.503822
RELEASED_RF_LENGTH_MM: Mapping[str, float] = {
    "ANT1": 22.194973,
    "ANT2": 34.930782,
    "ANT3": 31.500992,
    "ANT4": 36.557345,
    "ANT5": 36.557345,
    "ANT6": 31.500992,
    "ANT7": 34.930819,
    "ANT8": 22.194973,
}
RF_RELEASE_REPORT_SHA256 = "d1e4d45bc780cd765bf80cb13e02d459a09ad23ae6c677d1c2e09bf5b738a053"

FIGURE_NAMES = (
    "fig01_setup_coordinate_system.png",
    "fig02_capture_plan_and_continuity.png",
    "fig03_phase_profiles_by_frequency.png",
    "fig04_repeatability_and_quality.png",
    "fig05_direct_model_residuals.png",
    "fig06_anchored_phase_slope_fit.png",
    "fig07_tx2_posterior_map.png",
    "fig08_sensitivity_lofo.png",
)
SNAPSHOT_NAME = "multifrequency-phase-20260825-b-report-snapshot.json"
FIGURE_MANIFEST_NAME = "figures-manifest.json"

COLORS = {
    "ink": "#17202a",
    "muted": "#5f6b76",
    "grid": "#d7dde3",
    "tx1": "#2f6fbb",
    "tx2": "#d47a19",
    "primary": "#8b2f68",
    "outlier": "#c63737",
    "good": "#26836a",
    "board": "#e8ecef",
    "prior": "#7a6bb7",
}

STYLE: Mapping[str, object] = {
    "font.family": "DejaVu Sans",
    "font.size": 9.0,
    "axes.titlesize": 11.0,
    "axes.labelsize": 9.0,
    "axes.edgecolor": COLORS["muted"],
    "axes.labelcolor": COLORS["ink"],
    "axes.titlecolor": COLORS["ink"],
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": COLORS["grid"],
    "grid.linewidth": 0.6,
    "grid.alpha": 0.75,
    "xtick.color": COLORS["ink"],
    "ytick.color": COLORS["ink"],
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "legend.frameon": False,
    "lines.linewidth": 1.5,
}


class ReportError(RuntimeError):
    """A retained input or generated report violates a reproducibility invariant."""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One immutable JSON source and its byte identity."""

    role: str
    path: Path
    sha256: str
    byte_size: int
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReportSources:
    """Validated documents used by every generated report asset."""

    manifest: SourceDocument
    direct: SourceDocument
    primary: SourceDocument
    all_frequency: SourceDocument
    conservative: SourceDocument
    lofo: tuple[SourceDocument, ...]
    geometry: SourceDocument
    rf_release: SourceDocument


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, default=DEFAULT_RUN_DIRECTORY)
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--rf-release-report", type=Path, default=DEFAULT_RF_RELEASE_REPORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--refresh-snapshot",
        action="store_true",
        help="validate retained full analyses, replace the compact snapshot, and render figures",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="render into a temporary directory and byte-compare with committed outputs",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReportError(f"{label} must be an array")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportError(message)


def _load_document(path: Path, role: str) -> SourceDocument:
    resolved = path.expanduser().resolve(strict=True)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReportError(f"{role} is not valid JSON: {error}") from error
    document = _mapping(raw, role)
    return SourceDocument(
        role=role,
        path=resolved,
        sha256=_sha256(resolved),
        byte_size=resolved.stat().st_size,
        document=document,
    )


def _configuration(source: SourceDocument) -> Mapping[str, Any]:
    return _mapping(source.document.get("analysis_configuration"), f"{source.role}.configuration")


def _posterior(source: SourceDocument) -> Mapping[str, Any]:
    localization = _mapping(source.document.get("localization"), f"{source.role}.localization")
    return _mapping(localization.get("posterior"), f"{source.role}.posterior")


def _direct_rows(sources: ReportSources) -> Sequence[Any]:
    localization = _mapping(sources.direct.document.get("localization"), "direct.localization")
    return _sequence(localization.get("frequency_profile_rows"), "direct.frequency_profile_rows")


def _rf_lengths_from_release(source: SourceDocument) -> tuple[float, dict[str, float]]:
    routes = _sequence(source.document.get("routes"), "rf release routes")
    realized: dict[str, float] = {}
    for index, raw_route in enumerate(routes):
        route = _mapping(raw_route, f"rf release routes[{index}]")
        net = route.get("net")
        length = route.get("length_mm")
        if (
            isinstance(net, str)
            and isinstance(length, (int, float))
            and not isinstance(length, bool)
        ):
            realized[net] = float(length)
    _require("RF_COMMON" in realized, "RF release report does not contain RF_COMMON")
    antennas = {}
    for name in STATE_NAMES:
        net = f"RF_{name}"
        _require(net in realized, f"RF release report does not contain {net}")
        antennas[name] = realized[net]
    return realized["RF_COMMON"], antennas


def _load_sources(run_directory: Path, geometry: Path, rf_release: Path) -> ReportSources:
    run = run_directory.expanduser().resolve(strict=True)
    lofo_paths = sorted(run.glob("analysis-anchored-slope-lofo-exclude2458-*-300k.json"))
    sources = ReportSources(
        manifest=_load_document(run / "manifest.json", "manifest"),
        direct=_load_document(run / DIRECT_NAME, "direct analysis"),
        primary=_load_document(run / PRIMARY_NAME, "primary anchored analysis"),
        all_frequency=_load_document(run / ALL_FREQUENCY_NAME, "all-frequency analysis"),
        conservative=_load_document(run / CONSERVATIVE_NAME, "conservative analysis"),
        lofo=tuple(
            _load_document(path, f"LOFO analysis {path.stem}") for path in lofo_paths
        ),
        geometry=_load_document(geometry, "array geometry"),
        rf_release=_load_document(rf_release, "released v0.2.1 RF report"),
    )
    _validate_sources(sources)
    return sources


def _require_anchored_source_hash(source: SourceDocument, direct: SourceDocument) -> None:
    provenance = _mapping(source.document.get("source"), f"{source.role}.source")
    _require(
        provenance.get("analysis_sha256") == direct.sha256,
        f"{source.role} does not hash the selected direct analysis at {direct.path}",
    )


def _validate_sources(sources: ReportSources) -> None:
    manifest = sources.manifest.document
    _require(manifest.get("schema") == 1, "manifest schema must be 1")
    _require(manifest.get("run_id") == RUN_ID, f"manifest run ID must be {RUN_ID}")
    _require(manifest.get("status") == "complete", "manifest must be complete")
    summary = _mapping(manifest.get("summary"), "manifest.summary")
    expected_summary = {
        "planned_conditions": 42,
        "completed_conditions": 42,
        "execution_attempts": 42,
        "failed_attempts": 0,
        "quality_passed": 42,
        "quality_rejected": 0,
    }
    _require(
        all(summary.get(key) == value for key, value in expected_summary.items()),
        "manifest does not prove 42/42 successful retained conditions",
    )
    plan = _sequence(manifest.get("plan"), "manifest.plan")
    attempts = _sequence(manifest.get("attempts"), "manifest.attempts")
    _require(len(plan) == 42 and len(attempts) == 42, "manifest must contain 42 plan rows")
    for index, raw_attempt in enumerate(attempts):
        attempt = _mapping(raw_attempt, f"manifest.attempts[{index}]")
        post_mute = _mapping(attempt.get("post_mute"), f"manifest.attempts[{index}].post_mute")
        _require(
            attempt.get("status") == "complete" and post_mute.get("status") == "passed",
            f"attempt {index} lacks complete status and post-mute proof",
        )

    direct = sources.direct.document
    _require(direct.get("schema") == 1, "direct analysis schema must be 1")
    _require(
        direct.get("analysis_kind")
        == "fast20_dualband_phase_distribution_and_joint_localization",
        "direct analysis kind is unexpected",
    )
    direct_source = _mapping(direct.get("source"), "direct.source")
    _require(direct_source.get("run_id") == RUN_ID, "direct analysis run ID differs")
    _require(
        direct_source.get("manifest_sha256") == sources.manifest.sha256,
        "direct analysis manifest hash differs from retained manifest",
    )
    _require(
        direct_source.get("geometry_sha256") == sources.geometry.sha256,
        "direct analysis geometry hash differs from selected geometry",
    )
    experiment = _mapping(direct.get("experiment"), "direct.experiment")
    continuity = _mapping(experiment.get("continuity"), "direct.experiment.continuity")
    _require(
        experiment.get("completed_capture_count") == 42
        and experiment.get("paired_capture_count") == 21
        and experiment.get("rounds") == 3,
        "direct analysis capture counts differ from the retained plan",
    )
    _require(
        continuity.get("all_artifacts_validated") is True
        and continuity.get("blocks_per_artifact") == 100
        and continuity.get("samples_per_artifact") == 10_000_000
        and continuity.get("missing_samples_total") == 0
        and continuity.get("distinct_stream_id_count") == 42,
        "direct analysis does not prove continuous unique streams",
    )
    rows = _direct_rows(sources)
    _require(len(rows) == 7, "direct analysis must contain seven frequency profiles")
    centers = []
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, f"direct.frequency_profile_rows[{index}]")
        centers.append(row.get("center_frequency_hz"))
        _require(row.get("state_names") == list(STATE_NAMES), "frequency state order changed")
        accepted = _mapping(
            row.get("accepted_replicate_count"),
            f"direct.frequency_profile_rows[{index}].accepted_replicate_count",
        )
        _require(
            all(accepted.get(name) == 3 for name in STATE_NAMES),
            "frequency lacks three accepted replicates per antenna",
        )
        _require(row.get("valid_mask") == [True] * 8, "frequency contains an invalid antenna")
    _require(tuple(centers) == EXPECTED_FREQUENCIES_HZ, "retained frequency grid changed")

    for source in (sources.primary, sources.all_frequency, sources.conservative, *sources.lofo):
        _require(source.document.get("schema") == 1, f"{source.role} schema must be 1")
        _require(
            source.document.get("analysis_kind")
            == "anchored_multifrequency_tx2_phase_slope_localization",
            f"{source.role} kind is unexpected",
        )
        _require_anchored_source_hash(source, sources.direct)

    primary_configuration = _configuration(sources.primary)
    _require(
        primary_configuration.get("excluded_center_frequencies_hz") == [OUTLIER_FREQUENCY_HZ]
        and primary_configuration.get("systematic_phase_standard_deviation_deg") == 10.0,
        "primary analysis must be sys10 with the post-hoc 2.458 GHz exclusion",
    )
    _require(
        _configuration(sources.all_frequency).get("excluded_center_frequencies_hz") == [],
        "all-frequency analysis unexpectedly excludes a profile",
    )
    _require(
        _configuration(sources.conservative).get("excluded_center_frequencies_hz") == []
        and _configuration(sources.conservative).get("systematic_phase_standard_deviation_deg")
        == 40.0,
        "conservative analysis must use all frequencies and a 40 degree floor",
    )
    _require(len(sources.lofo) == 6, "exactly six leave-one-frequency-out analyses are required")
    observed_omissions = set()
    for source in sources.lofo:
        excluded = _configuration(source).get("excluded_center_frequencies_hz")
        _require(
            isinstance(excluded, list)
            and len(excluded) == 2
            and excluded[0] == OUTLIER_FREQUENCY_HZ,
            f"{source.role} must first exclude the post-hoc 2.458 GHz profile",
        )
        observed_omissions.add(excluded[1])
    _require(
        observed_omissions == set(EXPECTED_FREQUENCIES_HZ) - {OUTLIER_FREQUENCY_HZ},
        "LOFO analyses do not cover every retained primary frequency",
    )

    _require(
        sources.rf_release.sha256 == RF_RELEASE_REPORT_SHA256,
        "released RF report hash differs from the reviewed v0.2.1 report",
    )
    common, antennas = _rf_lengths_from_release(sources.rf_release)
    _require(
        round(common, 6) == RELEASED_RF_COMMON_LENGTH_MM,
        "released RF common-path length changed",
    )
    _require(
        all(round(antennas[name], 6) == RELEASED_RF_LENGTH_MM[name] for name in STATE_NAMES),
        "released RF antenna-path lengths changed",
    )


def _source_record(source: SourceDocument) -> dict[str, Any]:
    return {
        "role": source.role,
        "path": str(source.path),
        "file_name": source.path.name,
        "sha256": source.sha256,
        "byte_size": source.byte_size,
        "schema": source.document.get("schema"),
        "kind": source.document.get("analysis_kind", source.document.get("experiment_kind")),
    }


def _model_summary(source: SourceDocument) -> dict[str, Any]:
    posterior = _posterior(source)
    map_result = _mapping(posterior.get("map"), f"{source.role}.posterior.map")
    tx2 = _mapping(posterior.get("tx2"), f"{source.role}.posterior.tx2")
    residuals = _mapping(
        posterior.get("map_residuals"), f"{source.role}.posterior.map_residuals"
    )
    return {
        "excluded_center_frequencies_hz": _configuration(source).get(
            "excluded_center_frequencies_hz", []
        ),
        "systematic_phase_standard_deviation_deg": _configuration(source).get(
            "systematic_phase_standard_deviation_deg"
        ),
        "tx2_map_position_mm": map_result.get("tx2_position_mm"),
        "tx2_map_radius_mm": map_result.get("tx2_radius_mm"),
        "tx2_map_direction_deg": map_result.get("tx2_direction_deg"),
        "tx2_mean_position_mm": tx2.get("mean_position_mm"),
        "tx2_direction_resultant_length": tx2.get("direction_resultant_length"),
        "tx2_radius_interval_95_mm": tx2.get("radius_interval_95_mm"),
        "effective_sample_size": posterior.get("effective_sample_size"),
        "overall_weighted_rms_deg": residuals.get("overall_weighted_rms_deg"),
        "maximum_absolute_residual_deg": residuals.get("maximum_absolute_residual_deg"),
    }


def _build_snapshot(sources: ReportSources) -> dict[str, Any]:
    direct_posterior = _posterior(sources.direct)
    direct_residuals = _mapping(direct_posterior.get("map_residuals"), "direct residuals")
    direct_map = _mapping(direct_posterior.get("map"), "direct map")
    primary_posterior = _posterior(sources.primary)
    primary_residuals = _mapping(
        primary_posterior.get("map_residuals"), "primary residuals"
    )
    primary_output = _mapping(
        primary_posterior.get("output_particles"), "primary output particles"
    )
    direct_rows = []
    for raw_row in _direct_rows(sources):
        row = _mapping(raw_row, "frequency profile")
        diagnostics = _mapping(row.get("map_residual_diagnostics"), "direct diagnostics")
        direct_rows.append(
            {
                "center_frequency_hz": row.get("center_frequency_hz"),
                "carrier_frequency_hz": row.get("carrier_frequency_hz"),
                "accepted_replicate_count": row.get("accepted_replicate_count"),
                "circular_mean_double_relative_phase_deg": row.get(
                    "circular_mean_double_relative_phase_deg"
                ),
                "circular_repeat_standard_deviation_deg": row.get(
                    "circular_repeat_standard_deviation_deg"
                ),
                "aggregate_analyzer_standard_error_deg": row.get(
                    "aggregate_analyzer_standard_error_deg"
                ),
                "direct_map_weighted_rms_deg": diagnostics.get("weighted_rms_deg"),
            }
        )
    continuity = _mapping(
        _mapping(sources.direct.document.get("experiment"), "direct experiment").get(
            "continuity"
        ),
        "direct continuity",
    )
    manifest_configuration = _mapping(
        sources.manifest.document.get("configuration"), "manifest configuration"
    )
    _positions, centered_outline = _source_centered_geometry(sources)
    source_records = [
        _source_record(sources.manifest),
        _source_record(sources.direct),
        _source_record(sources.primary),
        _source_record(sources.all_frequency),
        _source_record(sources.conservative),
        *(_source_record(source) for source in sources.lofo),
        _source_record(sources.geometry),
        _source_record(sources.rf_release),
    ]
    lofo_rows = []
    for source in sources.lofo:
        summary = _model_summary(source)
        excluded = summary["excluded_center_frequencies_hz"]
        lofo_rows.append({"omitted_center_frequency_hz": excluded[1], **summary})
    lofo_rows.sort(key=lambda row: int(row["omitted_center_frequency_hz"]))
    return {
        "schema": 1,
        "report_kind": "retained_multifrequency_tx_localization_snapshot",
        "run_id": RUN_ID,
        "board_id": BOARD_ID,
        "source_files": source_records,
        "software": {
            "renderer": "scripts/render_localization_report.py",
            "renderer_sha256": _sha256(Path(__file__).resolve()),
            "numpy_version": np.__version__,
            "matplotlib_version": matplotlib.__version__,
        },
        "capture": {
            "planned_conditions": 42,
            "completed_conditions": 42,
            "rounds": 3,
            "frequencies_hz": list(EXPECTED_FREQUENCIES_HZ),
            "transmitters": ["TX1", "TX2"],
            "blocks_per_artifact": continuity.get("blocks_per_artifact"),
            "samples_per_artifact": continuity.get("samples_per_artifact"),
            "missing_samples_total": continuity.get("missing_samples_total"),
            "distinct_stream_id_count": continuity.get("distinct_stream_id_count"),
            "all_post_capture_mutes_passed": True,
            "round_order_policy": manifest_configuration.get("round_order_policy"),
        },
        "geometry": {
            "coordinate_system": "board-centered, +x right, +y down, millimetres",
            "state_names": list(STATE_NAMES),
            "selected_antenna_positions_mm": _mapping(
                _mapping(sources.direct.document.get("localization"), "direct localization").get(
                    "geometry"
                ),
                "direct geometry",
            ).get("selected_antenna_positions_mm"),
            "board_outline_centered_mm": list(centered_outline),
            "released_v0_2_1_rf_common_length_mm": RELEASED_RF_COMMON_LENGTH_MM,
            "released_v0_2_1_rf_antenna_length_mm": dict(RELEASED_RF_LENGTH_MM),
            "rf_length_treatment": (
                "documented physical routes; fixed phase is absorbed by the anchored model's "
                "per-antenna nuisance intercepts, not subtracted as calibrated delay"
            ),
        },
        "phase_profiles": direct_rows,
        "direct_diagnostic": {
            "tx1_map_position_mm": direct_map.get("tx1_position_mm"),
            "tx2_map_position_mm": direct_map.get("tx2_position_mm"),
            "overall_weighted_rms_deg": direct_residuals.get("overall_weighted_rms_deg"),
            "maximum_absolute_residual_deg": direct_residuals.get(
                "maximum_absolute_residual_deg"
            ),
            "per_frequency_weighted_rms_deg": direct_residuals.get(
                "capture_pair_rms_deg"
            ),
            "residual_phase_deg": direct_residuals.get("residual_phase_deg"),
            "verdict": "rejected as a calibrated position fit",
        },
        "primary_anchored_slope": {
            **_model_summary(sources.primary),
            "tx1_anchor_position_mm": _configuration(sources.primary).get(
                "tx1_anchor_position_mm"
            ),
            "radial_prior": _configuration(sources.primary).get("radial_prior"),
            "map_residuals": {
                "state_names": primary_residuals.get("state_names"),
                "state_indices": primary_residuals.get("state_indices"),
                "nuisance_intercept_deg": primary_residuals.get("nuisance_intercept_deg"),
                "antenna_weighted_rms_deg": primary_residuals.get(
                    "antenna_weighted_rms_deg"
                ),
                "overall_weighted_rms_deg": primary_residuals.get(
                    "overall_weighted_rms_deg"
                ),
            },
            "posterior_display_particles": {
                "method": primary_output.get("method"),
                "source_particle_count": primary_output.get("source_particle_count"),
                "output_particle_count": primary_output.get("output_particle_count"),
                "tx2_position_mm": [
                    _mapping(item, "primary output particle").get("tx2_position_mm")
                    for item in _sequence(primary_output.get("particles"), "primary particles")
                ],
            },
            "exclusion_classification": (
                "2.458 GHz was identified and excluded post hoc from its all-frequency "
                "77 degree residual; this is a transparent diagnostic sensitivity result"
            ),
            "interpretation": (
                "robust lower-right angular sector; radial range is dominated by the "
                "304.8 +/- 50 mm prior, not independently measured by this phase sweep"
            ),
        },
        "all_frequency_sys10": _model_summary(sources.all_frequency),
        "all_frequency_sys40": _model_summary(sources.conservative),
        "leave_one_frequency_out": lofo_rows,
        "figure_files": list(FIGURE_NAMES),
    }


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
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


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _float_matrix(value: object, label: str, shape: tuple[int, int]) -> npt.NDArray[np.float64]:
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ReportError(f"{label} must be numeric") from error
    if result.shape != shape or not np.all(np.isfinite(result)):
        raise ReportError(f"{label} must be a finite {shape[0]}x{shape[1]} matrix")
    return result


def _direct_arrays(
    snapshot: Mapping[str, Any],
) -> tuple[
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
]:
    rows = _sequence(snapshot.get("phase_profiles"), "snapshot.phase_profiles")
    frequencies = np.asarray(
        [_mapping(row, "frequency row")["carrier_frequency_hz"] for row in rows],
        dtype=np.float64,
    )
    phase = _float_matrix(
        [
            _mapping(row, "frequency row")["circular_mean_double_relative_phase_deg"]
            for row in rows
        ],
        "phase means",
        (7, 8),
    )
    repeat = _float_matrix(
        [
            _mapping(row, "frequency row")["circular_repeat_standard_deviation_deg"]
            for row in rows
        ],
        "repeat standard deviations",
        (7, 8),
    )
    analyzer = _float_matrix(
        [
            _mapping(row, "frequency row")["aggregate_analyzer_standard_error_deg"]
            for row in rows
        ],
        "analyzer standard errors",
        (7, 8),
    )
    return frequencies, phase, repeat, analyzer


def _source_centered_geometry(
    sources: ReportSources,
) -> tuple[npt.NDArray[np.float64], tuple[float, ...]]:
    localization = _mapping(sources.direct.document.get("localization"), "direct localization")
    geometry = _mapping(localization.get("geometry"), "direct geometry")
    positions = _mapping(
        geometry.get("selected_antenna_positions_mm"), "selected antenna positions"
    )
    result = np.asarray([positions[name] for name in STATE_NAMES], dtype=np.float64)
    if result.shape != (8, 2) or not np.all(np.isfinite(result)):
        raise ReportError("selected antenna geometry must be finite and 8x2")
    raw_geometry = sources.geometry.document
    outline = _mapping(raw_geometry.get("board_outline_mm"), "geometry board outline")
    x0 = float(outline["x0"])
    y0 = float(outline["y0"])
    x1 = float(outline["x1"])
    y1 = float(outline["y1"])
    center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return result, (x0 - center[0], y0 - center[1], x1 - center[0], y1 - center[1])


def _snapshot_geometry(
    snapshot: Mapping[str, Any],
) -> tuple[npt.NDArray[np.float64], tuple[float, ...]]:
    geometry = _mapping(snapshot.get("geometry"), "snapshot.geometry")
    positions = _mapping(
        geometry.get("selected_antenna_positions_mm"),
        "snapshot.geometry.selected_antenna_positions_mm",
    )
    position_array = np.asarray([positions[name] for name in STATE_NAMES], dtype=np.float64)
    outline = np.asarray(geometry.get("board_outline_centered_mm"), dtype=np.float64)
    if position_array.shape != (8, 2) or not np.all(np.isfinite(position_array)):
        raise ReportError("snapshot antenna geometry must be finite and 8x2")
    if outline.shape != (4,) or not np.all(np.isfinite(outline)):
        raise ReportError("snapshot board outline must contain four finite coordinates")
    return position_array, tuple(float(value) for value in outline)


def _new_figure(
    *, figsize: tuple[float, float], layout: str | None = "constrained"
) -> Figure:
    return plt.figure(figsize=figsize, dpi=160, layout=layout)


def _save_figure(fig: Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        fig.savefig(
            temporary,
            format="png",
            dpi=160,
            metadata={
                "Software": "smateway deterministic localization report renderer",
                "Creation Time": None,
            },
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        plt.close(fig)


def _draw_board(
    ax: Axes,
    positions_mm: npt.NDArray[np.float64],
    outline_mm: tuple[float, ...],
    *,
    label_antennas: bool = True,
) -> None:
    x0, y0, x1, y1 = outline_mm
    ax.add_patch(
        Rectangle(
            (x0, y0),
            x1 - x0,
            y1 - y0,
            facecolor=COLORS["board"],
            edgecolor=COLORS["ink"],
            linewidth=1.2,
            zorder=2,
        )
    )
    ax.scatter(
        positions_mm[:, 0],
        positions_mm[:, 1],
        s=35,
        color=COLORS["good"],
        edgecolors="white",
        linewidths=0.7,
        zorder=5,
    )
    if label_antennas:
        label_positions = {
            "ANT1": (-20.0, -96.0),
            "ANT2": (-66.0, -78.0),
            "ANT3": (-112.0, -12.0),
            "ANT4": (-112.0, 22.0),
            "ANT5": (112.0, 22.0),
            "ANT6": (112.0, -12.0),
            "ANT7": (66.0, -78.0),
            "ANT8": (20.0, -96.0),
        }
        for name, (x, y) in zip(STATE_NAMES, positions_mm, strict=True):
            ax.annotate(
                name,
                (x, y),
                xytext=label_positions[name],
                textcoords="data",
                ha="center",
                va="center",
                fontsize=7.5,
                color=COLORS["ink"],
                zorder=6,
                arrowprops={
                    "arrowstyle": "-",
                    "color": COLORS["muted"],
                    "linewidth": 0.55,
                    "shrinkA": 2.0,
                    "shrinkB": 3.0,
                },
            )
    ax.scatter([0.0], [0.0], marker="+", s=65, color=COLORS["ink"], zorder=6)


def _style_top_view(ax: Axes, *, limit_mm: float) -> None:
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(-limit_mm, limit_mm)
    ax.set_ylim(limit_mm, -limit_mm)
    ax.set_xlabel("board-centered x (mm)  —  +x right")
    ax.set_ylabel("board-centered y (mm)  —  +y down")
    ax.axhline(0.0, color=COLORS["grid"], linewidth=0.7, zorder=0)
    ax.axvline(0.0, color=COLORS["grid"], linewidth=0.7, zorder=0)


def _plot_setup_geometry(snapshot: Mapping[str, Any], output: Path) -> None:
    positions, outline = _snapshot_geometry(snapshot)
    primary = _mapping(snapshot.get("primary_anchored_slope"), "snapshot primary result")
    anchor = np.asarray(primary["tx1_anchor_position_mm"], dtype=np.float64)
    map_position = np.asarray(primary["tx2_map_position_mm"], dtype=np.float64)
    mean_position = np.asarray(primary["tx2_mean_position_mm"], dtype=np.float64)
    prior = _mapping(primary.get("radial_prior"), "snapshot primary radial prior")
    prior_mean = float(prior["mean_mm"])
    prior_std = float(prior["standard_deviation_mm"])

    fig = _new_figure(figsize=(13.2, 8.0))
    grid = fig.add_gridspec(1, 2, width_ratios=(1.65, 1.0))
    ax = fig.add_subplot(grid[0, 0])
    table_ax = fig.add_subplot(grid[0, 1])
    _draw_board(ax, positions, outline)
    for radius, linestyle, alpha in (
        (prior_mean, "--", 0.9),
        (prior_mean - prior_std, ":", 0.55),
        (prior_mean + prior_std, ":", 0.55),
    ):
        ax.add_patch(
            Circle(
                (0.0, 0.0),
                radius,
                fill=False,
                color=COLORS["prior"],
                linewidth=1.2,
                linestyle=linestyle,
                alpha=alpha,
                zorder=1,
            )
        )
    ax.scatter(
        [anchor[0]],
        [anchor[1]],
        marker="^",
        s=95,
        color=COLORS["tx1"],
        edgecolors="white",
        linewidths=0.8,
        label="fixed TX1 anchor",
        zorder=8,
    )
    ax.scatter(
        [map_position[0]],
        [map_position[1]],
        marker="*",
        s=170,
        color=COLORS["primary"],
        edgecolors="white",
        linewidths=0.8,
        label="primary TX2 MAP",
        zorder=9,
    )
    ax.scatter(
        [mean_position[0]],
        [mean_position[1]],
        marker="o",
        s=48,
        facecolors="white",
        edgecolors=COLORS["primary"],
        linewidths=1.2,
        label="TX2 posterior mean",
        zorder=9,
    )
    _style_top_view(ax, limit_mm=430.0)
    ax.set_title("Top-view geometry used by the anchored phase-slope model", loc="left")
    ax.legend(loc="upper left")
    ax.text(
        0.02,
        0.02,
        "Dashed ring: 304.8 mm radial prior mean\nDotted rings: prior mean ± 50 mm",
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=8,
        color=COLORS["muted"],
        bbox={"facecolor": "white", "edgecolor": COLORS["grid"], "alpha": 0.92},
    )

    table_ax.axis("off")
    table_ax.set_title("Released v0.2.1 RF route lengths", loc="left", pad=12)
    cell_text = [
        ["COMMON", f"{RELEASED_RF_COMMON_LENGTH_MM:.6f}"],
        *[[name, f"{RELEASED_RF_LENGTH_MM[name]:.6f}"] for name in STATE_NAMES],
    ]
    table = table_ax.table(
        cellText=cell_text,
        colLabels=["route", "realized length (mm)"],
        cellLoc="right",
        colLoc="right",
        loc="upper center",
        bbox=(0.02, 0.30, 0.96, 0.64),
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor(COLORS["grid"])
        if row == 0:
            cell.set_facecolor("#edf3f8")
            cell.set_text_props(weight="bold", color=COLORS["ink"])
    table_ax.text(
        0.02,
        0.23,
        "These are physical PCB route lengths, not complete calibrated electrical delays.",
        transform=table_ax.transAxes,
        ha="left",
        va="top",
        wrap=True,
        color=COLORS["ink"],
        fontsize=9,
        weight="bold",
    )
    table_ax.text(
        0.02,
        0.14,
        "The anchored model absorbs each fixed RF/antenna phase into one nuisance "
        "intercept. Range remains dominated by the independent 304.8 ± 50 mm prior.",
        transform=table_ax.transAxes,
        ha="left",
        va="top",
        wrap=True,
        color=COLORS["muted"],
        fontsize=8.5,
    )
    fig.suptitle(
        "Pluto RX2 eight-way array — geometry and conditional transmitter result",
        fontsize=15,
        weight="bold",
        x=0.03,
        ha="left",
    )
    _save_figure(fig, output)


def _plot_capture_plan(snapshot: Mapping[str, Any], output: Path) -> None:
    capture = _mapping(snapshot.get("capture"), "snapshot.capture")
    policies = _sequence(capture.get("round_order_policy"), "snapshot round order policy")
    policy_labels = (
        "forward: TX1 → TX2",
        "reverse: TX2 → TX1",
        "rotated; alternating TX order",
    )
    fig = _new_figure(figsize=(15.5, 7.4), layout=None)
    fig.subplots_adjust(left=0.06, right=0.87, top=0.83, bottom=0.06, hspace=0.25)
    grid = fig.add_gridspec(4, 1, height_ratios=(1.0, 1.0, 1.0, 0.92))
    for round_index, raw_policy in enumerate(policies, start=1):
        policy = _mapping(raw_policy, f"round policy {round_index}")
        conditions = _sequence(policy.get("conditions"), f"round {round_index} conditions")
        ax = fig.add_subplot(grid[round_index - 1, 0])
        ax.set_xlim(0.0, float(len(conditions)))
        ax.set_ylim(0.0, 1.0)
        ax.axis("off")
        ax.text(
            -0.15,
            0.5,
            f"Round {round_index}",
            ha="right",
            va="center",
            fontsize=10,
            weight="bold",
            color=COLORS["ink"],
        )
        for index, raw_condition in enumerate(conditions):
            condition = _mapping(raw_condition, f"round {round_index} condition {index}")
            tx_channel = int(condition["tx_channel"])
            center_ghz = float(condition["center_frequency_hz"]) / 1e9
            color = COLORS["tx1"] if tx_channel == 0 else COLORS["tx2"]
            ax.add_patch(
                Rectangle(
                    (index + 0.03, 0.08),
                    0.94,
                    0.84,
                    facecolor=color,
                    edgecolor="white",
                    linewidth=0.8,
                )
            )
            ax.text(
                index + 0.5,
                0.50,
                f"{center_ghz:.3f}\nTX{tx_channel + 1}",
                ha="center",
                va="center",
                color="white",
                fontsize=7.7,
                weight="bold",
            )
        ax.text(
            14.12,
            0.5,
            policy_labels[round_index - 1],
            ha="left",
            va="center",
            fontsize=8,
            color=COLORS["muted"],
        )

    summary_ax = fig.add_subplot(grid[3, 0])
    summary_ax.axis("off")
    badges = (
        ("42 / 42", "planned conditions complete"),
        ("100", "ABI-2 buffers per artifact"),
        ("10,000,000", "samples per artifact"),
        ("0", "missing samples total"),
        ("42", "unique continuous streams"),
        ("42 / 42", "strict post-capture mutes"),
    )
    for index, (value, label) in enumerate(badges):
        x0 = index / len(badges) + 0.008
        width = 1.0 / len(badges) - 0.016
        summary_ax.add_patch(
            Rectangle(
                (x0, 0.15),
                width,
                0.72,
                transform=summary_ax.transAxes,
                facecolor="#f5f7f9",
                edgecolor=COLORS["grid"],
                linewidth=0.8,
            )
        )
        summary_ax.text(
            x0 + width / 2.0,
            0.59,
            value,
            transform=summary_ax.transAxes,
            ha="center",
            va="center",
            fontsize=13,
            weight="bold",
            color=COLORS["good"],
        )
        summary_ax.text(
            x0 + width / 2.0,
            0.30,
            label,
            transform=summary_ax.transAxes,
            ha="center",
            va="center",
            fontsize=7.5,
            color=COLORS["muted"],
            wrap=True,
        )
    fig.suptitle(
        "Retained acquisition plan and continuity proof",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.925,
        "Each condition is a separate 10 s artifact; frequency and TX ordering is reversed/rotated "
        "across rounds to expose time drift. Firmware repeats ALL_OFF, ANT1, …, ANT8.",
        ha="left",
        va="top",
        color=COLORS["muted"],
        fontsize=9,
    )
    _save_figure(fig, output)


def _plot_phase_profiles(snapshot: Mapping[str, Any], output: Path) -> None:
    frequencies, phase, repeat, _analyzer = _direct_arrays(snapshot)
    x = frequencies / 1e9
    fig, axes = plt.subplots(
        2,
        4,
        figsize=(15.0, 8.2),
        dpi=160,
        sharex=True,
        sharey=True,
        layout=None,
    )
    fig.subplots_adjust(left=0.06, right=0.985, top=0.84, bottom=0.11, wspace=0.09, hspace=0.14)
    for antenna_index, (name, ax) in enumerate(zip(STATE_NAMES, axes.flat, strict=True)):
        ax.errorbar(
            x,
            phase[:, antenna_index],
            yerr=repeat[:, antenna_index],
            fmt="o-",
            markersize=4.3,
            capsize=2.4,
            color=COLORS["primary"],
            ecolor="#9f89a7",
        )
        ax.axvline(OUTLIER_FREQUENCY_HZ / 1e9, color=COLORS["outlier"], linestyle="--")
        ax.scatter(
            [OUTLIER_FREQUENCY_HZ / 1e9],
            [phase[4, antenna_index]],
            marker="x",
            s=50,
            linewidths=1.5,
            color=COLORS["outlier"],
            zorder=5,
        )
        ax.set_title(f"{name}  (ANT1 reference)" if antenna_index else "ANT1 reference = 0°")
        ax.set_ylim(-190.0, 190.0)
        ax.set_yticks((-180, -90, 0, 90, 180))
        ax.set_xticks(x)
        ax.set_xticklabels([f"{value:.3f}" for value in x], rotation=35, ha="right")
        if antenna_index >= 4:
            ax.set_xlabel("carrier frequency (GHz)")
        if antenna_index % 4 == 0:
            ax.set_ylabel("wrapped phase (degrees)")
    fig.suptitle(
        "Circularly aggregated TX2 − TX1 phase profiles",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.94,
        "Points are three paired-capture circular means; error bars are circular repeat SD only. "
        "Red × marks the 2.458 GHz profile excluded post hoc from the primary anchored fit.",
        ha="left",
        va="top",
        color=COLORS["muted"],
        fontsize=9,
    )
    _save_figure(fig, output)


def _annotated_heatmap(
    ax: Axes,
    values: npt.NDArray[np.float64],
    *,
    title: str,
    vmax: float,
    cmap: str,
) -> None:
    image = ax.imshow(
        values,
        aspect="auto",
        interpolation="nearest",
        vmin=0.0,
        vmax=vmax,
        cmap=cmap,
    )
    ax.set_title(title, loc="left")
    ax.set_xticks(np.arange(8), labels=STATE_NAMES)
    ax.set_yticks(
        np.arange(7),
        labels=[f"{frequency / 1e9:.3f}" for frequency in EXPECTED_FREQUENCIES_HZ],
    )
    ax.set_xlabel("receive state")
    ax.set_ylabel("center frequency (GHz)")
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            normalized = values[row, column] / max(vmax, 1e-9)
            ax.text(
                column,
                row,
                f"{values[row, column]:.1f}",
                ha="center",
                va="center",
                fontsize=7.2,
                color="white" if normalized > 0.55 else COLORS["ink"],
            )
    colorbar = ax.figure.colorbar(image, ax=ax, shrink=0.86, pad=0.02)
    colorbar.set_label("degrees")


def _plot_repeatability(snapshot: Mapping[str, Any], output: Path) -> None:
    _frequencies, _phase, repeat, analyzer = _direct_arrays(snapshot)
    fig, axes = plt.subplots(1, 2, figsize=(15.0, 6.8), dpi=160, layout=None)
    fig.subplots_adjust(left=0.06, right=0.95, top=0.83, bottom=0.12, wspace=0.23)
    _annotated_heatmap(
        axes[0],
        repeat,
        title="Between-round circular repeat SD",
        vmax=max(4.0, float(np.ceil(np.max(repeat)))),
        cmap="YlGnBu",
    )
    _annotated_heatmap(
        axes[1],
        analyzer,
        title="Aggregate analyzer standard error",
        vmax=max(4.0, float(np.ceil(np.max(analyzer)))),
        cmap="YlOrBr",
    )
    for ax in axes:
        ax.add_patch(
            Rectangle(
                (-0.5, 3.5),
                8.0,
                1.0,
                fill=False,
                edgecolor=COLORS["outlier"],
                linewidth=1.5,
                linestyle="--",
            )
        )
    fig.suptitle(
        "Measurement repeatability and phase-estimator quality",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.925,
        "All 56 frequency/state cells are valid and each frequency has three accepted TX pairs. "
        "The red dashed 2.458 GHz row remains statistically repeatable despite its "
        "geometric-model mismatch.",
        ha="left",
        va="top",
        color=COLORS["muted"],
        fontsize=9,
    )
    _save_figure(fig, output)


def _plot_direct_residuals(snapshot: Mapping[str, Any], output: Path) -> None:
    diagnostic = _mapping(snapshot.get("direct_diagnostic"), "snapshot direct diagnostic")
    matrix = _float_matrix(
        diagnostic.get("residual_phase_deg"), "direct residual phase", (7, 8)
    )
    per_frequency = np.asarray(
        diagnostic.get("per_frequency_weighted_rms_deg"), dtype=np.float64
    )
    if per_frequency.shape != (7,) or not np.all(np.isfinite(per_frequency)):
        raise ReportError("direct per-frequency RMS must contain seven finite values")
    overall = float(diagnostic["overall_weighted_rms_deg"])

    fig = _new_figure(figsize=(14.8, 7.2), layout=None)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.83, bottom=0.17, wspace=0.28)
    grid = fig.add_gridspec(1, 2, width_ratios=(1.45, 1.0))
    heat_ax = fig.add_subplot(grid[0, 0])
    bar_ax = fig.add_subplot(grid[0, 1])
    normalizer = mcolors.TwoSlopeNorm(vmin=-180.0, vcenter=0.0, vmax=180.0)
    image = heat_ax.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="RdBu_r",
        norm=normalizer,
    )
    heat_ax.set_xticks(np.arange(8), labels=STATE_NAMES)
    heat_ax.set_yticks(
        np.arange(7),
        labels=[f"{frequency / 1e9:.3f}" for frequency in EXPECTED_FREQUENCIES_HZ],
    )
    heat_ax.set_xlabel("receive state")
    heat_ax.set_ylabel("center frequency (GHz)")
    heat_ax.set_title("Wrapped residual at direct-model MAP", loc="left")
    for row in range(7):
        for column in range(8):
            value = matrix[row, column]
            heat_ax.text(
                column,
                row,
                f"{value:+.0f}",
                ha="center",
                va="center",
                fontsize=7.2,
                color="white" if abs(value) > 90.0 else COLORS["ink"],
            )
    heat_ax.add_patch(
        Rectangle(
            (-0.5, 3.5),
            8.0,
            1.0,
            fill=False,
            edgecolor=COLORS["outlier"],
            linewidth=1.8,
            linestyle="--",
        )
    )
    colorbar = fig.colorbar(image, ax=heat_ax, shrink=0.86, pad=0.02)
    colorbar.set_label("wrapped residual (degrees)")

    indices = np.arange(7)
    colors = [COLORS["outlier"] if index == 4 else COLORS["tx1"] for index in indices]
    bars = bar_ax.barh(indices, per_frequency, color=colors, alpha=0.9)
    bar_ax.set_yticks(
        indices,
        labels=[f"{frequency / 1e9:.3f} GHz" for frequency in EXPECTED_FREQUENCIES_HZ],
    )
    bar_ax.invert_yaxis()
    bar_ax.set_xlabel("weighted RMS residual (degrees)")
    bar_ax.set_title("Per-frequency direct-model mismatch", loc="left")
    bar_ax.axvline(overall, color=COLORS["ink"], linestyle="--", linewidth=1.2)
    for bar, value in zip(bars, per_frequency, strict=True):
        bar_ax.text(
            value + 1.2,
            bar.get_y() + bar.get_height() / 2.0,
            f"{value:.1f}°",
            ha="left",
            va="center",
            fontsize=8,
            color=COLORS["ink"],
        )
    bar_ax.set_xlim(0.0, max(90.0, float(np.max(per_frequency)) + 12.0))
    bar_ax.text(
        overall + 1.2,
        6.7,
        f"overall {overall:.1f}°",
        ha="left",
        va="bottom",
        color=COLORS["ink"],
        fontsize=8,
    )
    fig.text(
        0.61,
        0.045,
        "Diagnostic verdict: rejected as a calibrated position fit.\n"
        "Low repeat scatter cannot remove antenna phase, coupling, or multipath.",
        ha="left",
        va="bottom",
        fontsize=8.5,
        color=COLORS["outlier"],
        weight="bold",
    )
    fig.suptitle(
        "Seven-frequency direct-path model diagnostic",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.925,
        "The uncalibrated joint TX1/TX2 fit leaves 53.9° weighted RMS; it is retained to expose "
        "model mismatch, not used as the primary TX2 result.",
        ha="left",
        va="top",
        color=COLORS["muted"],
        fontsize=9,
    )
    _save_figure(fig, output)


def _masked_wrapped_curve(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Insert NaNs across circular wrap jumps so line plots do not imply vertical phase motion."""

    result = np.asarray(values, dtype=np.float64).copy()
    if result.ndim != 1 or not np.all(np.isfinite(result)):
        raise ReportError("wrapped curve must be a finite vector")
    jump_indices = np.flatnonzero(np.abs(np.diff(result)) > 180.0) + 1
    result[jump_indices] = np.nan
    return result


def _plot_anchored_slope(snapshot: Mapping[str, Any], output: Path) -> None:
    frequencies, observed, repeat, analyzer = _direct_arrays(snapshot)
    primary = _mapping(snapshot.get("primary_anchored_slope"), "snapshot primary result")
    anchor = np.asarray(primary["tx1_anchor_position_mm"], dtype=np.float64)
    tx2_map = np.asarray(primary["tx2_map_position_mm"], dtype=np.float64)
    residuals = _mapping(primary.get("map_residuals"), "snapshot primary residuals")
    nuisance = np.asarray(residuals["nuisance_intercept_deg"], dtype=np.float64)
    rms_by_antenna = np.asarray(residuals["antenna_weighted_rms_deg"], dtype=np.float64)
    state_indices = tuple(int(value) for value in residuals["state_indices"])
    _require(state_indices == tuple(range(1, 8)), "primary residual antenna order changed")
    if nuisance.shape != (7,) or rms_by_antenna.shape != (7,):
        raise ReportError("primary residual diagnostics must contain ANT2 through ANT8")
    positions, _outline = _snapshot_geometry(snapshot)
    geometry = AnchoredArrayGeometry(positions, np.asarray((0.0, 0.0)))
    dense_frequency = np.linspace(float(frequencies[0]), float(frequencies[-1]), 1_500)
    dense_prediction = predict_double_relative_phase_deg(
        geometry,
        dense_frequency,
        fixed_tx1_position_mm=anchor,
        tx2_position_mm=tx2_map,
    )
    used = np.asarray(
        [frequency != OUTLIER_FREQUENCY_HZ for frequency in EXPECTED_FREQUENCIES_HZ],
        dtype=np.bool_,
    )
    statistical = np.sqrt(repeat**2 + analyzer**2)

    fig = _new_figure(figsize=(15.0, 9.0), layout=None)
    fig.subplots_adjust(left=0.05, right=0.98, top=0.84, bottom=0.09, wspace=0.18, hspace=0.24)
    grid = fig.add_gridspec(2, 4)
    axes: list[Axes] = []
    for row in range(2):
        for column in range(4):
            axes.append(fig.add_subplot(grid[row, column]))
    x = frequencies / 1e9
    for panel_index, antenna_index in enumerate(range(1, 8)):
        ax = axes[panel_index]
        predicted = wrap_phase_deg(dense_prediction[:, antenna_index] + nuisance[panel_index])
        ax.plot(
            dense_frequency / 1e9,
            _masked_wrapped_curve(predicted),
            color=COLORS["ink"],
            linewidth=1.4,
            label="MAP phase-slope model",
        )
        ax.errorbar(
            x[used],
            observed[used, antenna_index],
            yerr=statistical[used, antenna_index],
            fmt="o",
            color=COLORS["primary"],
            ecolor="#9f89a7",
            capsize=2.2,
            markersize=4.5,
            label="used aggregate profile",
            zorder=4,
        )
        ax.scatter(
            x[~used],
            observed[~used, antenna_index],
            marker="x",
            s=65,
            linewidths=1.8,
            color=COLORS["outlier"],
            label="2.458 GHz post-hoc exclusion",
            zorder=5,
        )
        ax.axvline(OUTLIER_FREQUENCY_HZ / 1e9, color=COLORS["outlier"], linestyle=":")
        ax.set_title(f"{STATE_NAMES[antenna_index]}   RMS {rms_by_antenna[panel_index]:.1f}°")
        ax.set_ylim(-190.0, 190.0)
        ax.set_yticks((-180, -90, 0, 90, 180))
        ax.set_xticks(x)
        ax.set_xticklabels([f"{value:.3f}" for value in x], rotation=35, ha="right")
        if panel_index >= 4:
            ax.set_xlabel("carrier frequency (GHz)")
        if panel_index in (0, 4):
            ax.set_ylabel("wrapped phase (degrees)")
    legend_ax = axes[-1]
    legend_ax.axis("off")
    handles, labels = axes[0].get_legend_handles_labels()
    legend_ax.legend(handles, labels, loc="upper left", fontsize=8.5)
    legend_ax.text(
        0.02,
        0.61,
        "Primary conditional model",
        transform=legend_ax.transAxes,
        ha="left",
        va="top",
        fontsize=10,
        weight="bold",
        color=COLORS["ink"],
    )
    legend_ax.text(
        0.02,
        0.54,
        f"TX1 fixed: ({anchor[0]:+.1f}, {anchor[1]:+.1f}) mm\n"
        f"TX2 MAP: ({tx2_map[0]:+.1f}, {tx2_map[1]:+.1f}) mm\n"
        f"Overall residual: {float(residuals['overall_weighted_rms_deg']):.1f}° RMS\n\n"
        "One fixed circular intercept is profiled per antenna. This absorbs PCB, connector, "
        "antenna and receiver-path phase; the remaining frequency slope supplies the "
        "angular evidence.",
        transform=legend_ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.5,
        color=COLORS["muted"],
        wrap=True,
    )
    fig.suptitle(
        "Anchored TX2 phase-slope fit after transparent 2.458 GHz exclusion",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.945,
        "The exclusion is post hoc: the all-frequency fit assigned 2.458 GHz a 77.0° "
        "profile residual, more than twice every other frequency. Error bars combine repeat "
        "SD and analyzer SE; the 10° systematic floor enters once in the likelihood.",
        ha="left",
        va="top",
        color=COLORS["muted"],
        fontsize=9,
    )
    _save_figure(fig, output)


def _plot_posterior(snapshot: Mapping[str, Any], output: Path) -> None:
    positions, outline = _snapshot_geometry(snapshot)
    primary = _mapping(snapshot.get("primary_anchored_slope"), "snapshot primary result")
    anchor = np.asarray(primary["tx1_anchor_position_mm"], dtype=np.float64)
    prior = _mapping(primary.get("radial_prior"), "snapshot primary radial prior")
    output_particles = _mapping(
        primary.get("posterior_display_particles"), "snapshot primary output particles"
    )
    particle_positions = np.asarray(output_particles.get("tx2_position_mm"), dtype=np.float64)
    if particle_positions.ndim != 2 or particle_positions.shape[1] != 2:
        raise ReportError("primary posterior particles must contain planar positions")
    map_position = np.asarray(primary["tx2_map_position_mm"], dtype=np.float64)
    mean_position = np.asarray(primary["tx2_mean_position_mm"], dtype=np.float64)
    radius_95 = tuple(float(value) for value in primary["tx2_radius_interval_95_mm"])

    lofo_rows = _sequence(snapshot.get("leave_one_frequency_out"), "snapshot LOFO rows")
    lofo_angles = [
        float(_mapping(row, "snapshot LOFO row")["tx2_map_direction_deg"])
        for row in lofo_rows
    ]
    angle_low, angle_high = min(lofo_angles), max(lofo_angles)
    fig = _new_figure(figsize=(10.8, 9.2))
    ax = fig.add_subplot(1, 1, 1)
    ax.add_patch(
        Wedge(
            (0.0, 0.0),
            radius_95[1],
            angle_low,
            angle_high,
            width=radius_95[1] - radius_95[0],
            facecolor=COLORS["primary"],
            alpha=0.08,
            edgecolor="none",
            zorder=0,
        )
    )
    ax.scatter(
        particle_positions[:, 0],
        particle_positions[:, 1],
        s=5,
        color=COLORS["primary"],
        alpha=0.11,
        linewidths=0.0,
        rasterized=False,
        zorder=1,
    )
    _draw_board(ax, positions, outline)
    for radius, linestyle, linewidth in (
        (float(prior["mean_mm"]), "--", 1.3),
        (radius_95[0], ":", 1.0),
        (radius_95[1], ":", 1.0),
    ):
        ax.add_patch(
            Circle(
                (0.0, 0.0),
                radius,
                fill=False,
                color=COLORS["prior"],
                linestyle=linestyle,
                linewidth=linewidth,
                zorder=3,
            )
        )
    ax.scatter(
        [anchor[0]],
        [anchor[1]],
        marker="^",
        s=100,
        color=COLORS["tx1"],
        edgecolors="white",
        linewidths=0.8,
        label="fixed TX1 anchor",
        zorder=8,
    )
    ax.scatter(
        [map_position[0]],
        [map_position[1]],
        marker="*",
        s=190,
        color=COLORS["primary"],
        edgecolors="white",
        linewidths=0.8,
        label="TX2 MAP",
        zorder=9,
    )
    ax.scatter(
        [mean_position[0]],
        [mean_position[1]],
        marker="o",
        s=55,
        facecolors="white",
        edgecolors=COLORS["primary"],
        linewidths=1.3,
        label="TX2 posterior mean",
        zorder=9,
    )
    _style_top_view(ax, limit_mm=440.0)
    ax.legend(loc="upper left")
    ax.set_title("Seeded downsample of the 2,000,000-particle posterior", loc="left")
    ax.text(
        0.02,
        0.02,
        f"MAP direction {float(primary['tx2_map_direction_deg']):.1f}°; "
        f"LOFO sector {angle_low:.1f}–{angle_high:.1f}°\n"
        f"95% radial interval {radius_95[0]:.1f}–{radius_95[1]:.1f} mm\n"
        "Radial interval closely follows the 304.8 ± 50 mm prior; "
        "interpret this as a sector estimate.",
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=8.5,
        color=COLORS["muted"],
        bbox={"facecolor": "white", "edgecolor": COLORS["grid"], "alpha": 0.94},
    )
    fig.suptitle(
        "Primary anchored TX2 posterior — lower-right sector, prior-dominated range",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    _save_figure(fig, output)


def _plot_lofo_sensitivity(snapshot: Mapping[str, Any], output: Path) -> None:
    primary = _mapping(snapshot.get("primary_anchored_slope"), "snapshot primary result")
    all_frequency = _mapping(snapshot.get("all_frequency_sys10"), "snapshot all sys10")
    conservative = _mapping(snapshot.get("all_frequency_sys40"), "snapshot all sys40")
    lofo = []
    for raw_summary in _sequence(
        snapshot.get("leave_one_frequency_out"), "snapshot LOFO rows"
    ):
        summary = _mapping(raw_summary, "snapshot LOFO row")
        lofo.append((int(summary["omitted_center_frequency_hz"]), summary))
    lofo.sort(key=lambda item: item[0])

    fig = _new_figure(figsize=(14.0, 7.8), layout=None)
    fig.subplots_adjust(left=0.04, right=0.98, top=0.86, bottom=0.15, wspace=0.28)
    grid = fig.add_gridspec(1, 2, width_ratios=(1.0, 1.45))
    polar_ax = fig.add_subplot(grid[0, 0], projection="polar")
    comparison_ax = fig.add_subplot(grid[0, 1])
    polar_ax.set_theta_zero_location("E")
    polar_ax.set_theta_direction(-1)
    polar_ax.set_thetamin(0.0)
    polar_ax.set_thetamax(55.0)
    polar_ax.set_ylim(190.0, 420.0)
    polar_ax.set_rlabel_position(50.0)
    polar_ax.plot(
        np.linspace(0.0, np.deg2rad(55.0), 200),
        np.full(200, 304.8),
        color=COLORS["prior"],
        linestyle="--",
        linewidth=1.2,
        label="304.8 mm prior mean",
    )
    lofo_angles = np.asarray([float(summary["tx2_map_direction_deg"]) for _, summary in lofo])
    lofo_radii = np.asarray([float(summary["tx2_map_radius_mm"]) for _, summary in lofo])
    polar_ax.scatter(
        np.deg2rad(lofo_angles),
        lofo_radii,
        s=48,
        color=COLORS["good"],
        label="LOFO MAP",
        zorder=5,
    )
    polar_ax.scatter(
        [np.deg2rad(float(primary["tx2_map_direction_deg"]))],
        [float(primary["tx2_map_radius_mm"])],
        marker="*",
        s=150,
        color=COLORS["primary"],
        edgecolors="white",
        linewidths=0.8,
        label="primary MAP",
        zorder=7,
    )
    polar_ax.scatter(
        [np.deg2rad(float(all_frequency["tx2_map_direction_deg"]))],
        [float(all_frequency["tx2_map_radius_mm"])],
        marker="x",
        s=70,
        linewidths=1.8,
        color=COLORS["outlier"],
        label="all 7 frequencies",
        zorder=7,
    )
    polar_ax.set_title("MAP direction/radius sensitivity", loc="left", pad=18)
    polar_ax.legend(loc="lower left", bbox_to_anchor=(-0.02, -0.11), fontsize=8)

    labels = [
        "primary: exclude 2.458 post hoc",
        "all 7: sys 10°",
        "all 7: sys 40°",
        *[f"also omit {frequency / 1e9:.3f}" for frequency, _summary in lofo],
    ]
    summaries = [primary, all_frequency, conservative, *(summary for _frequency, summary in lofo)]
    y = np.arange(len(labels))
    directions = np.asarray([float(summary["tx2_map_direction_deg"]) for summary in summaries])
    residual_rms = np.asarray([float(summary["overall_weighted_rms_deg"]) for summary in summaries])
    point_colors = [
        COLORS["primary"],
        COLORS["outlier"],
        COLORS["prior"],
        *([COLORS["good"]] * len(lofo)),
    ]
    comparison_ax.hlines(
        y,
        np.full_like(directions, 0.0),
        directions,
        color=COLORS["grid"],
        linewidth=2.0,
    )
    comparison_ax.scatter(directions, y, s=52, color=point_colors, zorder=4)
    comparison_ax.axvline(
        float(primary["tx2_map_direction_deg"]),
        color=COLORS["primary"],
        linestyle="--",
    )
    comparison_ax.set_yticks(y, labels=labels)
    comparison_ax.invert_yaxis()
    comparison_ax.set_xlim(0.0, 45.0)
    comparison_ax.set_xlabel("TX2 MAP direction (degrees clockwise from +x / right)")
    comparison_ax.set_title("Outlier, systematic-floor and LOFO checks", loc="left")
    for index, (direction, rms) in enumerate(zip(directions, residual_rms, strict=True)):
        comparison_ax.text(
            direction + 0.8,
            index,
            f"{direction:.1f}°   RMS {rms:.1f}°",
            ha="left",
            va="center",
            fontsize=8,
            color=COLORS["ink"],
        )
    fig.text(
        0.56,
        0.035,
        f"LOFO direction span: {float(np.min(lofo_angles)):.1f}–{float(np.max(lofo_angles)):.1f}°\n"
        "The ~305 mm MAP radii are not an independent range validation;\n"
        "every fit shares the same radial prior.",
        ha="left",
        va="bottom",
        fontsize=8.5,
        color=COLORS["muted"],
    )
    fig.suptitle(
        "TX2 localization sensitivity — stable lower-right sector, unresolved absolute range",
        fontsize=15,
        weight="bold",
        x=0.02,
        ha="left",
    )
    _save_figure(fig, output)


FIGURE_RENDERERS: tuple[Callable[[Mapping[str, Any], Path], None], ...] = (
    _plot_setup_geometry,
    _plot_capture_plan,
    _plot_phase_profiles,
    _plot_repeatability,
    _plot_direct_residuals,
    _plot_anchored_slope,
    _plot_posterior,
    _plot_lofo_sensitivity,
)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ReportError(f"generated figure is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _figure_manifest(snapshot_path: Path, figure_paths: Sequence[Path]) -> dict[str, Any]:
    figures = []
    for path in figure_paths:
        width, height = _png_dimensions(path)
        figures.append(
            {
                "path": f"png/{path.name}",
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
                "width_px": width,
                "height_px": height,
            }
        )
    return {
        "schema": 1,
        "manifest_kind": "deterministic_localization_report_figures",
        "snapshot_path": f"data/{snapshot_path.name}",
        "snapshot_sha256": _sha256(snapshot_path),
        "renderer_path": "scripts/render_localization_report.py",
        "renderer_sha256": _sha256(Path(__file__).resolve()),
        "matplotlib_version": matplotlib.__version__,
        "figures": figures,
    }


def _validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    _require(snapshot.get("schema") == 1, "report snapshot schema must be 1")
    _require(snapshot.get("run_id") == RUN_ID, f"report snapshot run ID must be {RUN_ID}")
    _require(
        snapshot.get("figure_files") == list(FIGURE_NAMES),
        "report snapshot figure inventory changed",
    )
    capture = _mapping(snapshot.get("capture"), "snapshot.capture")
    _require(
        capture.get("completed_conditions") == 42
        and capture.get("missing_samples_total") == 0
        and capture.get("all_post_capture_mutes_passed") is True,
        "report snapshot does not retain capture/continuity proof",
    )
    profiles = _sequence(snapshot.get("phase_profiles"), "snapshot.phase_profiles")
    _require(len(profiles) == 7, "report snapshot must retain seven phase profiles")
    centers = tuple(
        _mapping(row, "snapshot phase row").get("center_frequency_hz") for row in profiles
    )
    _require(centers == EXPECTED_FREQUENCIES_HZ, "report snapshot frequency grid changed")
    primary = _mapping(snapshot.get("primary_anchored_slope"), "snapshot primary result")
    _require(
        primary.get("excluded_center_frequencies_hz") == [OUTLIER_FREQUENCY_HZ],
        "report snapshot must label the post-hoc 2.458 GHz exclusion",
    )
    particles = _mapping(
        primary.get("posterior_display_particles"), "snapshot posterior particles"
    )
    particle_positions = _sequence(
        particles.get("tx2_position_mm"), "snapshot posterior particle positions"
    )
    _require(
        len(particle_positions) == particles.get("output_particle_count")
        and 1 <= len(particle_positions) <= 5_000,
        "report snapshot posterior particle count is inconsistent",
    )


def _render_snapshot(snapshot_source: SourceDocument, output_root: Path) -> dict[str, Any]:
    snapshot = snapshot_source.document
    _validate_snapshot(snapshot)
    root = output_root.expanduser().resolve()
    data_directory = root / "data"
    png_directory = root / "png"
    snapshot_path = data_directory / SNAPSHOT_NAME
    if snapshot_source.path != snapshot_path:
        _atomic_bytes(snapshot_path, snapshot_source.path.read_bytes())
    figure_paths = [png_directory / name for name in FIGURE_NAMES]
    with plt.rc_context(STYLE):
        for renderer, path in zip(FIGURE_RENDERERS, figure_paths, strict=True):
            renderer(snapshot, path)
    figure_manifest = _figure_manifest(snapshot_path, figure_paths)
    manifest_path = data_directory / FIGURE_MANIFEST_NAME
    _atomic_json(manifest_path, figure_manifest)
    return {
        "output_root": str(root),
        "snapshot": str(snapshot_path),
        "figure_manifest": str(manifest_path),
        "figure_count": len(figure_paths),
        "figure_sha256": {
            entry["path"]: entry["sha256"] for entry in figure_manifest["figures"]
        },
    }


def _refresh_report(sources: ReportSources, output_root: Path) -> dict[str, Any]:
    root = output_root.expanduser().resolve()
    snapshot_path = root / "data" / SNAPSHOT_NAME
    _atomic_json(snapshot_path, _build_snapshot(sources))
    snapshot_source = _load_document(snapshot_path, "generated report snapshot")
    return _render_snapshot(snapshot_source, root)


def _expected_report_paths(root: Path) -> tuple[Path, ...]:
    return (
        root / "data" / SNAPSHOT_NAME,
        root / "data" / FIGURE_MANIFEST_NAME,
        *(root / "png" / name for name in FIGURE_NAMES),
    )


def _check_report(snapshot_source: SourceDocument, output_root: Path) -> dict[str, Any]:
    expected_root = output_root.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="smateway-localization-report-check-") as temporary:
        generated_root = Path(temporary) / "localization"
        _render_snapshot(snapshot_source, generated_root)
        mismatches = []
        for expected in _expected_report_paths(expected_root):
            generated = generated_root / expected.relative_to(expected_root)
            if not expected.is_file():
                mismatches.append(f"missing: {expected}")
            elif expected.read_bytes() != generated.read_bytes():
                mismatches.append(f"byte mismatch: {expected}")
    if mismatches:
        raise ReportError("deterministic report check failed: " + "; ".join(mismatches))
    return {
        "output_root": str(expected_root),
        "checked_file_count": len(_expected_report_paths(expected_root)),
        "deterministic_regeneration": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check and args.refresh_snapshot:
            raise ReportError("--check and --refresh-snapshot are mutually exclusive")
        output_root = args.output_root.expanduser().resolve()
        if args.refresh_snapshot:
            sources = _load_sources(args.run_directory, args.geometry, args.rf_release_report)
            result = _refresh_report(sources, output_root)
        else:
            snapshot_path = output_root / "data" / SNAPSHOT_NAME
            snapshot_source = _load_document(snapshot_path, "committed report snapshot")
            result = (
                _check_report(snapshot_source, output_root)
                if args.check
                else _render_snapshot(snapshot_source, output_root)
            )
    except (FileNotFoundError, OSError, ReportError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
