#!/usr/bin/env python3
"""Aggregate a completed Fast20 sweep and infer planar dual-TX positions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite, sqrt
from pathlib import Path
from typing import Any

import numpy as np

from smateway.capture_continuity import (
    CaptureContinuitySummary,
    validate_sigmf_continuity,
)
from smateway.dual_tx_localization import (
    CircularLikelihood,
    DualTxPosterior,
    PairedPhaseMeasurements,
    PlanarArrayGeometry,
    RadialPositionPrior,
    infer_dual_tx_importance,
)
from smateway.localization import load_antenna_positions
from smateway.phase_distribution import (
    STATE_NAMES,
    Fast20PhaseArtifact,
    load_fast20_phase_document,
    summarize_paired_tx_phase_differences,
    summarize_phase_replicates,
    wrap_phase_deg,
)

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_GEOMETRY = REPOSITORY / "profiles/phase20-v1/array_geometry.json"
EXPECTED_CONDITIONS = (
    (2_400_000_000, 0),
    (2_400_000_000, 1),
    (5_800_000_000, 0),
    (5_800_000_000, 1),
)
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
MINIMUM_LOCALIZATION_ANTENNAS = 4
DEFAULT_SAMPLE_COUNT = 100_000
DEFAULT_SEED = 20260825
DEFAULT_VISUALIZATION_PARTICLES = 2_000


class AnalysisError(RuntimeError):
    """The persisted experiment does not satisfy an analysis invariant."""


@dataclass(frozen=True, slots=True)
class CompletedCapture:
    """One exact plan condition joined to its validated phase artifact."""

    plan_index: int
    round_index: int
    center_frequency_hz: int
    tx_channel: int
    attempt_id: int
    started_at: str
    completed_at: str
    analysis_path: Path
    analysis_sha256: str
    metadata_path: Path
    metadata_sha256: str
    continuity: CaptureContinuitySummary
    artifact: Fast20PhaseArtifact
    analyzer_standard_error_deg: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class CapturePair:
    """Explicit same-round, same-frequency TX1/TX2 capture pair."""

    round_index: int
    center_frequency_hz: int
    tx1: CompletedCapture
    tx2: CompletedCapture

    @property
    def carrier_frequency_hz(self) -> float:
        return (self.tx1.artifact.rf_frequency_hz + self.tx2.artifact.rf_frequency_hz) / 2.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--sample-count", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--systematic-floor-2g4-deg", type=float, default=25.0)
    parser.add_argument("--systematic-floor-5g8-deg", type=float, default=40.0)
    parser.add_argument(
        "--visualization-particles",
        type=int,
        default=DEFAULT_VISUALIZATION_PARTICLES,
    )
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AnalysisError(f"{label} must be an array")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisError(f"{label} must be a non-empty string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AnalysisError(f"{label} must be a boolean")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_plan(rounds: int) -> list[dict[str, int | str]]:
    plan: list[dict[str, int | str]] = []
    for round_index in range(1, rounds + 1):
        for condition_index, (frequency_hz, tx_channel) in enumerate(EXPECTED_CONDITIONS, start=1):
            plan.append(
                {
                    "plan_index": len(plan),
                    "round": round_index,
                    "condition_index": condition_index,
                    "center_frequency_hz": frequency_hz,
                    "tx_channel": tx_channel,
                    "tx_name": f"TX{tx_channel + 1}",
                }
            )
    return plan


def _analyzer_standard_errors(path: Path) -> tuple[float | None, ...]:
    raw = _mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    phase = _mapping(raw.get("phase"), "phase")
    raw_states = _sequence(phase.get("states"), "phase.states")
    if len(raw_states) != len(STATE_NAMES):
        raise AnalysisError("phase.states does not contain eight analyzer estimates")
    result: list[float | None] = []
    for index, name in enumerate(STATE_NAMES):
        state = _mapping(raw_states[index], f"phase.states[{index}]")
        if state.get("name") != name:
            raise AnalysisError("analyzer states are not ordered ANT1 through ANT8")
        value = state.get("approximate_phase_standard_error_deg")
        if value is None:
            result.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AnalysisError(f"{name} analyzer standard error must be numeric")
        standard_error = float(value)
        if not isfinite(standard_error) or standard_error < 0.0:
            raise AnalysisError(f"{name} analyzer standard error must be finite and non-negative")
        result.append(standard_error)
    return tuple(result)


def _validate_attempt(
    raw_attempt: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    capture_root: Path,
) -> CompletedCapture:
    for field in (
        "plan_index",
        "round",
        "condition_index",
        "center_frequency_hz",
        "tx_channel",
        "tx_name",
    ):
        if raw_attempt.get(field) != expected.get(field):
            raise AnalysisError(f"completed attempt does not match plan field {field}")
    if raw_attempt.get("status") != "complete" or raw_attempt.get("error") is not None:
        raise AnalysisError("completed plan attempt is not cleanly complete")
    post_mute = _mapping(raw_attempt.get("post_mute"), "attempt.post_mute")
    if post_mute.get("status") != "passed":
        raise AnalysisError("completed attempt does not prove post-capture TX mute")
    capture = _mapping(raw_attempt.get("capture"), "attempt.capture")
    reanalysis = _mapping(raw_attempt.get("reanalysis"), "attempt.reanalysis")
    if _boolean(capture.get("accepted"), "capture.accepted") is not True:
        raise AnalysisError("completed attempt capture was not accepted")
    if _boolean(reanalysis.get("accepted"), "reanalysis.accepted") is not True:
        raise AnalysisError("completed attempt phase reanalysis was not accepted")

    artifact_id = _string(raw_attempt.get("artifact_id"), "attempt.artifact_id")
    if ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise AnalysisError("attempt artifact ID is malformed")
    parsed = _mapping(reanalysis.get("parsed_output"), "reanalysis.parsed_output")
    if parsed.get("artifact_id") != artifact_id:
        raise AnalysisError("reanalysis artifact ID differs from its attempt")
    analysis_path = capture_root / artifact_id / "fast20-relative-phase.json"
    parsed_analysis = Path(_string(parsed.get("analysis"), "reanalysis analysis path"))
    if parsed_analysis.resolve(strict=True) != analysis_path.resolve(strict=True):
        raise AnalysisError("reanalysis path differs from the canonical artifact path")
    artifact = load_fast20_phase_document(analysis_path)
    if artifact.artifact_id != artifact_id:
        raise AnalysisError("phase document artifact ID differs from its attempt")
    if artifact.tx_channel != expected["tx_channel"]:
        raise AnalysisError("phase document TX channel differs from the plan")
    if artifact.center_frequency_hz != expected["center_frequency_hz"]:
        raise AnalysisError("phase document center frequency differs from the plan")
    metadata_path = capture_root / artifact_id / f"{artifact_id}.sigmf-meta"
    metadata = _mapping(
        json.loads(metadata_path.read_text(encoding="utf-8")),
        str(metadata_path),
    )
    continuity = validate_sigmf_continuity(
        metadata,
        expected_total_samples=10_000_000,
        expected_samples_per_block=100_000,
    )
    if continuity.stream_id != artifact.stream_id:
        raise AnalysisError("SigMF and phase document stream IDs differ")
    return CompletedCapture(
        plan_index=_integer(expected["plan_index"], "plan.plan_index"),
        round_index=_integer(expected["round"], "plan.round"),
        center_frequency_hz=_integer(expected["center_frequency_hz"], "plan.center_frequency_hz"),
        tx_channel=_integer(expected["tx_channel"], "plan.tx_channel"),
        attempt_id=_integer(raw_attempt.get("attempt_id"), "attempt.attempt_id"),
        started_at=_string(raw_attempt.get("started_at"), "attempt.started_at"),
        completed_at=_string(raw_attempt.get("completed_at"), "attempt.completed_at"),
        analysis_path=analysis_path.resolve(strict=True),
        analysis_sha256=_sha256(analysis_path),
        metadata_path=metadata_path.resolve(strict=True),
        metadata_sha256=_sha256(metadata_path),
        continuity=continuity,
        artifact=artifact,
        analyzer_standard_error_deg=_analyzer_standard_errors(analysis_path),
    )


def _load_completed_experiment(
    manifest_path: Path,
) -> tuple[Mapping[str, Any], tuple[CompletedCapture, ...], str]:
    manifest_sha256 = _sha256(manifest_path)
    manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), str(manifest_path))
    if manifest.get("schema") != 1:
        raise AnalysisError("manifest schema must be 1")
    if manifest.get("experiment_kind") != "fast20_phase_distribution":
        raise AnalysisError("manifest is not a Fast20 phase-distribution experiment")
    if manifest.get("status") != "complete":
        raise AnalysisError("manifest must be complete before analysis")

    configuration = _mapping(manifest.get("configuration"), "configuration")
    rounds = _integer(configuration.get("rounds"), "configuration.rounds")
    if not 1 <= rounds <= 20:
        raise AnalysisError("configuration rounds must lie within 1..20")
    expected_order = [
        {"center_frequency_hz": frequency_hz, "tx_channel": tx_channel}
        for frequency_hz, tx_channel in EXPECTED_CONDITIONS
    ]
    order = [
        dict(_mapping(item, "configuration.condition_order item"))
        for item in _sequence(configuration.get("condition_order"), "condition_order")
    ]
    if order != expected_order:
        raise AnalysisError(
            "manifest condition order is not exact 2.4/TX1, 2.4/TX2, 5.8/TX1, 5.8/TX2"
        )

    expected_plan = _expected_plan(rounds)
    actual_plan = [
        dict(_mapping(item, "plan item")) for item in _sequence(manifest.get("plan"), "plan")
    ]
    if actual_plan != expected_plan:
        raise AnalysisError("manifest plan is not the exact interleaved round plan")

    board_id = _string(configuration.get("board_id"), "configuration.board_id")
    capture_root = Path.home() / ".local/state/smateway/boards" / board_id / "pluto-usb-captures"
    attempts = [
        _mapping(item, "attempt") for item in _sequence(manifest.get("attempts"), "attempts")
    ]
    if any(attempt.get("status") not in {"complete", "failed"} for attempt in attempts):
        raise AnalysisError("complete manifest contains a non-terminal attempt")
    completed: dict[int, Mapping[str, Any]] = {}
    for attempt in attempts:
        if attempt.get("status") != "complete":
            continue
        plan_index = _integer(attempt.get("plan_index"), "attempt.plan_index")
        if not 0 <= plan_index < len(expected_plan):
            raise AnalysisError("completed attempt has an out-of-plan index")
        if plan_index in completed:
            raise AnalysisError("plan index has more than one completed attempt")
        completed[plan_index] = attempt
    if set(completed) != set(range(len(expected_plan))):
        raise AnalysisError("manifest does not contain one completed attempt per plan condition")

    summary = _mapping(manifest.get("summary"), "summary")
    if summary.get("planned_conditions") != len(expected_plan):
        raise AnalysisError("manifest summary planned count is inconsistent")
    if summary.get("completed_conditions") != len(expected_plan):
        raise AnalysisError("manifest summary completed count is inconsistent")

    captures = tuple(
        _validate_attempt(completed[index], expected_plan[index], capture_root=capture_root)
        for index in range(len(expected_plan))
    )
    summarize_phase_replicates(capture.artifact for capture in captures)
    return manifest, captures, manifest_sha256


def _pair_captures(captures: Sequence[CompletedCapture]) -> tuple[CapturePair, ...]:
    indexed = {
        (capture.round_index, capture.center_frequency_hz, capture.tx_channel): capture
        for capture in captures
    }
    if len(indexed) != len(captures):
        raise AnalysisError("capture condition keys are not unique")
    rounds = sorted({capture.round_index for capture in captures})
    pairs = []
    for round_index in rounds:
        for center_frequency_hz in (2_400_000_000, 5_800_000_000):
            try:
                tx1 = indexed[(round_index, center_frequency_hz, 0)]
                tx2 = indexed[(round_index, center_frequency_hz, 1)]
            except KeyError as error:
                raise AnalysisError(
                    "a round is missing an explicit same-frequency TX pair"
                ) from error
            pairs.append(
                CapturePair(
                    round_index=round_index,
                    center_frequency_hz=center_frequency_hz,
                    tx1=tx1,
                    tx2=tx2,
                )
            )
    summarize_paired_tx_phase_differences((pair.tx1.artifact, pair.tx2.artifact) for pair in pairs)
    return tuple(pairs)


def _board_geometry(path: Path) -> tuple[np.ndarray, np.ndarray, Mapping[str, Any], str]:
    antenna_positions = load_antenna_positions(path)
    document = _mapping(json.loads(path.read_text(encoding="utf-8")), "geometry")
    outline = _mapping(document.get("board_outline_mm"), "geometry.board_outline_mm")
    coordinates = np.asarray(
        (
            float(outline["x0"]),
            float(outline["x1"]),
            float(outline["y0"]),
            float(outline["y1"]),
        ),
        dtype=np.float64,
    )
    if (
        not np.all(np.isfinite(coordinates))
        or coordinates[1] <= coordinates[0]
        or coordinates[3] <= coordinates[2]
    ):
        raise AnalysisError("geometry board outline is invalid")
    board_center = np.asarray(
        ((coordinates[0] + coordinates[1]) / 2.0, (coordinates[2] + coordinates[3]) / 2.0)
    )
    return antenna_positions - board_center, board_center, document, _sha256(path)


def _systematic_floor(center_frequency_hz: int, floor_2g4: float, floor_5g8: float) -> float:
    if center_frequency_hz == 2_400_000_000:
        return floor_2g4
    if center_frequency_hz == 5_800_000_000:
        return floor_5g8
    raise AnalysisError("localization pair uses an unsupported center frequency")


def _localization_inputs(
    pairs: Sequence[CapturePair],
    centered_positions: np.ndarray,
    *,
    floor_2g4: float,
    floor_5g8: float,
) -> tuple[
    tuple[str, ...],
    dict[str, int],
    PairedPhaseMeasurements,
    PlanarArrayGeometry,
    list[dict[str, Any]],
]:
    full_valid_mask = tuple(
        tuple(
            pair.tx1.artifact.capture_quality_passed
            and pair.tx2.artifact.capture_quality_passed
            and pair.tx1.artifact.state(name).quality_passed
            and pair.tx2.artifact.state(name).quality_passed
            for name in STATE_NAMES
        )
        for pair in pairs
    )
    accepted_pair_counts = {
        name: sum(row[index] for row in full_valid_mask) for index, name in enumerate(STATE_NAMES)
    }
    selected_names = tuple(name for name in STATE_NAMES if accepted_pair_counts[name] > 0)
    if len(selected_names) < MINIMUM_LOCALIZATION_ANTENNAS:
        raise AnalysisError(
            f"only {len(selected_names)} antennas pass at least one pair; at least "
            f"{MINIMUM_LOCALIZATION_ANTENNAS} are required"
        )
    indices = tuple(STATE_NAMES.index(name) for name in selected_names)
    phases = []
    uncertainties = []
    valid_masks = []
    carriers = []
    row_documents = []
    for pair, full_row_mask in zip(pairs, full_valid_mask, strict=True):
        floor = _systematic_floor(pair.center_frequency_hz, floor_2g4, floor_5g8)
        row_phase = []
        row_uncertainty = []
        tx1_errors = []
        tx2_errors = []
        row_valid = [full_row_mask[index] for index in indices]
        valid_count = sum(row_valid)
        if valid_count < MINIMUM_LOCALIZATION_ANTENNAS:
            raise AnalysisError(
                f"round {pair.round_index} at {pair.center_frequency_hz} Hz has "
                f"only {valid_count} valid antennas; at least "
                f"{MINIMUM_LOCALIZATION_ANTENNAS} are required per pair"
            )
        for index, name in zip(indices, selected_names, strict=True):
            tx1_state = pair.tx1.artifact.state(name)
            tx2_state = pair.tx2.artifact.state(name)
            row_phase.append(wrap_phase_deg(tx2_state.raw_phase_deg - tx1_state.raw_phase_deg))
            tx1_error = pair.tx1.analyzer_standard_error_deg[index]
            tx2_error = pair.tx2.analyzer_standard_error_deg[index]
            tx1_errors.append(tx1_error)
            tx2_errors.append(tx2_error)
            row_uncertainty.append(
                sqrt(floor**2 + (tx1_error or 0.0) ** 2 + (tx2_error or 0.0) ** 2)
            )
        carrier = pair.carrier_frequency_hz
        phases.append(row_phase)
        uncertainties.append(row_uncertainty)
        valid_masks.append(row_valid)
        carriers.append(carrier)
        row_documents.append(
            {
                "round": pair.round_index,
                "center_frequency_hz": pair.center_frequency_hz,
                "carrier_frequency_hz": carrier,
                "tx1_rf_frequency_hz": pair.tx1.artifact.rf_frequency_hz,
                "tx2_rf_frequency_hz": pair.tx2.artifact.rf_frequency_hz,
                "tx1_artifact_id": pair.tx1.artifact.artifact_id,
                "tx2_artifact_id": pair.tx2.artifact.artifact_id,
                "state_names": list(selected_names),
                "valid_mask": row_valid,
                "valid_state_names": [
                    name for name, valid in zip(selected_names, row_valid, strict=True) if valid
                ],
                "valid_state_count": valid_count,
                "raw_tx2_minus_tx1_phase_deg": row_phase,
                "tx1_analyzer_standard_error_deg": tx1_errors,
                "tx2_analyzer_standard_error_deg": tx2_errors,
                "systematic_floor_deg": floor,
                "combined_phase_standard_deviation_deg": row_uncertainty,
            }
        )
    geometry = PlanarArrayGeometry(
        antenna_positions_mm=centered_positions[np.asarray(indices)],
        center_mm=np.asarray((0.0, 0.0)),
    )
    measurements = PairedPhaseMeasurements(
        carrier_frequency_hz=np.asarray(carriers),
        tx2_minus_tx1_phase_deg=np.asarray(phases),
        phase_standard_deviation_deg=np.asarray(uncertainties),
        valid_mask=np.asarray(valid_masks),
    )
    return selected_names, accepted_pair_counts, measurements, geometry, row_documents


def _source_commit() -> str | None:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _particle_downsample(
    posterior: DualTxPosterior, maximum_count: int, seed: int
) -> dict[str, Any]:
    samples = posterior.samples
    source_count = samples.sample_count
    if source_count <= maximum_count:
        indices = np.arange(source_count)
        display_weights = samples.weight.copy()
        method = "all-weighted-particles"
    else:
        rng = np.random.default_rng(seed)
        targets = (np.arange(maximum_count, dtype=np.float64) + rng.random()) / maximum_count
        selected = np.searchsorted(np.cumsum(samples.weight), targets, side="left")
        indices, counts = np.unique(np.minimum(selected, source_count - 1), return_counts=True)
        display_weights = counts.astype(np.float64) / maximum_count
        method = "seeded-systematic-resample"
    particles = []
    for index, display_weight in zip(indices, display_weights, strict=True):
        particles.append(
            {
                "source_index": int(index),
                "display_weight": float(display_weight),
                "source_weight": float(samples.weight[index]),
                "tx1_radius_mm": float(samples.tx1_radius_mm[index]),
                "tx1_angle_deg": float(samples.tx1_angle_deg[index]),
                "tx1_position_mm": samples.tx1_position_mm[index].tolist(),
                "tx2_radius_mm": float(samples.tx2_radius_mm[index]),
                "tx2_angle_deg": float(samples.tx2_angle_deg[index]),
                "tx2_position_mm": samples.tx2_position_mm[index].tolist(),
                "log_likelihood": float(samples.log_likelihood[index]),
                "log_posterior_density": float(samples.log_posterior_density[index]),
            }
        )
    return {
        "method": method,
        "source_particle_count": source_count,
        "maximum_output_count": maximum_count,
        "output_particle_count": len(particles),
        "display_weight_sum": float(np.sum(display_weights)),
        "particles": particles,
    }


def _posterior_document(
    posterior: DualTxPosterior, visualization_particles: int, seed: int
) -> dict[str, Any]:
    samples = posterior.samples
    map_index = int(np.argmax(samples.log_posterior_density))
    effective_fraction = posterior.effective_sample_size / samples.sample_count
    low_effective_sample_size = posterior.effective_sample_size < max(
        100.0, samples.sample_count * 0.001
    )
    return {
        "method": posterior.method,
        "sample_count": samples.sample_count,
        "effective_sample_size": posterior.effective_sample_size,
        "effective_sample_fraction": effective_fraction,
        "low_effective_sample_size_warning": low_effective_sample_size,
        "map": {
            "tx1_position_mm": samples.tx1_position_mm[map_index].tolist(),
            "tx1_radius_mm": float(samples.tx1_radius_mm[map_index]),
            "tx1_angle_deg": float(samples.tx1_angle_deg[map_index]),
            "tx2_position_mm": samples.tx2_position_mm[map_index].tolist(),
            "tx2_radius_mm": float(samples.tx2_radius_mm[map_index]),
            "tx2_angle_deg": float(samples.tx2_angle_deg[map_index]),
            "log_likelihood": float(samples.log_likelihood[map_index]),
            "log_posterior_density": float(samples.log_posterior_density[map_index]),
            "posterior_weight": float(samples.weight[map_index]),
        },
        "tx1": asdict(posterior.tx1),
        "tx2": asdict(posterior.tx2),
        "modes": [asdict(mode) for mode in posterior.modes],
        "credible_regions": [asdict(region) for region in posterior.credible_regions],
        "map_residuals": {
            "nuisance_offset_deg": posterior.map_residuals.nuisance_offset_deg.tolist(),
            "residual_phase_deg": posterior.map_residuals.residual_phase_deg.tolist(),
            "capture_pair_rms_deg": posterior.map_residuals.capture_pair_rms_deg.tolist(),
            "overall_weighted_rms_deg": posterior.map_residuals.overall_weighted_rms_deg,
            "maximum_absolute_residual_deg": posterior.map_residuals.maximum_absolute_residual_deg,
            "valid_mask": (
                None
                if posterior.map_residuals.valid_mask is None
                else posterior.map_residuals.valid_mask.tolist()
            ),
        },
        "visualization_particles": _particle_downsample(
            posterior, visualization_particles, seed ^ 0x5EED5EED
        ),
    }


def _artifact_provenance(capture: CompletedCapture) -> dict[str, Any]:
    return {
        "plan_index": capture.plan_index,
        "round": capture.round_index,
        "center_frequency_hz": capture.center_frequency_hz,
        "tx_channel": capture.tx_channel,
        "attempt_id": capture.attempt_id,
        "started_at": capture.started_at,
        "completed_at": capture.completed_at,
        "analysis_path": str(capture.analysis_path),
        "analysis_sha256": capture.analysis_sha256,
        "metadata_path": str(capture.metadata_path),
        "metadata_sha256": capture.metadata_sha256,
        "continuity": capture.continuity.as_dict(),
        "artifact_id": capture.artifact.artifact_id,
        "artifact_data_sha256": capture.artifact.artifact_sha256,
        "stream_id": capture.artifact.stream_id,
        "rf_frequency_hz": capture.artifact.rf_frequency_hz,
        "capture_quality_passed": capture.artifact.capture_quality_passed,
        "overall_quality_passed": capture.artifact.overall_quality_passed,
        "state_quality": {state.name: state.quality_passed for state in capture.artifact.states},
        "analyzer_standard_error_deg": {
            name: value
            for name, value in zip(STATE_NAMES, capture.analyzer_standard_error_deg, strict=True)
        },
    }


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
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


def main() -> int:
    args = _parser().parse_args()
    if not 1_000 <= args.sample_count <= 2_000_000:
        raise SystemExit("sample count must lie within 1000..2000000")
    if not 100 <= args.visualization_particles <= 10_000:
        raise SystemExit("visualization particles must lie within 100..10000")
    for value, label in (
        (args.systematic_floor_2g4_deg, "2.4 GHz systematic floor"),
        (args.systematic_floor_5g8_deg, "5.8 GHz systematic floor"),
    ):
        if not isfinite(value) or value <= 0.0 or value > 180.0:
            raise SystemExit(f"{label} must lie within (0, 180]")

    manifest_path = args.manifest.expanduser().resolve(strict=True)
    geometry_path = args.geometry.expanduser().resolve(strict=True)
    output_path = args.output.expanduser().resolve()
    if output_path in {manifest_path, geometry_path}:
        raise SystemExit("output must not overwrite an analysis input")
    manifest, captures, manifest_sha256 = _load_completed_experiment(manifest_path)
    if output_path in {capture.analysis_path for capture in captures}:
        raise SystemExit("output must not overwrite a phase artifact")
    pairs = _pair_captures(captures)
    centered_positions, board_center, geometry_document, geometry_sha256 = _board_geometry(
        geometry_path
    )
    selected_names, accepted_pair_counts, measurements, geometry, rows = _localization_inputs(
        pairs,
        centered_positions,
        floor_2g4=args.systematic_floor_2g4_deg,
        floor_5g8=args.systematic_floor_5g8_deg,
    )
    prior = RadialPositionPrior(mean_mm=304.8, standard_deviation_mm=50.0)
    likelihood = CircularLikelihood(systematic_phase_std_deg=0.0, minimum_phase_std_deg=0.1)
    posterior = infer_dual_tx_importance(
        measurements,
        geometry,
        sample_count=args.sample_count,
        seed=args.seed,
        prior=prior,
        likelihood=likelihood,
    )
    tx_distributions = summarize_phase_replicates(capture.artifact for capture in captures)
    paired_distributions = summarize_paired_tx_phase_differences(
        (pair.tx1.artifact, pair.tx2.artifact) for pair in pairs
    )
    configuration = _mapping(manifest.get("configuration"), "configuration")
    document = {
        "schema": 1,
        "analysis_kind": "fast20_dualband_phase_distribution_and_joint_localization",
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "repository": str(REPOSITORY),
            "git_commit": _source_commit(),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "run_id": manifest.get("run_id"),
            "board_id": configuration.get("board_id"),
            "radio_serial": configuration.get("serial"),
            "geometry_path": str(geometry_path),
            "geometry_sha256": geometry_sha256,
        },
        "analysis_configuration": {
            "sample_count": args.sample_count,
            "seed": args.seed,
            "visualization_particle_limit": args.visualization_particles,
            "systematic_phase_floor_deg": {
                "2400000000": args.systematic_floor_2g4_deg,
                "5800000000": args.systematic_floor_5g8_deg,
            },
            "radial_prior": asdict(prior),
            "plane_z_mm": 0.0,
            "minimum_valid_antennas_per_pair": MINIMUM_LOCALIZATION_ANTENNAS,
        },
        "experiment": {
            "status": manifest.get("status"),
            "rounds": configuration.get("rounds"),
            "condition_order": configuration.get("condition_order"),
            "completed_capture_count": len(captures),
            "paired_capture_count": len(pairs),
            "continuity": {
                "all_artifacts_validated": True,
                "metadata_abi": 2,
                "blocks_per_artifact": 100,
                "samples_per_block": 100_000,
                "samples_per_artifact": 10_000_000,
                "distinct_stream_id_count": len(
                    {capture.continuity.stream_id for capture in captures}
                ),
                "missing_samples_total": 0,
            },
            "artifacts": [_artifact_provenance(capture) for capture in captures],
        },
        "distributions": {
            "phase_definition": (
                "per-TX values are ANT1-referenced; paired values are raw-state TX2 minus TX1"
            ),
            "per_tx_center_frequency_state": [asdict(summary) for summary in tx_distributions],
            "paired_raw_tx2_minus_tx1": [asdict(summary) for summary in paired_distributions],
        },
        "localization": {
            "model": (
                "calibration-free planar direct-path TX2-minus-TX1 phase with "
                "one marginalized circular offset per capture pair"
            ),
            "assumptions": [
                (
                    "TX1, TX2 and all receive-antenna phase centers lie at z=0 "
                    "in the centered board plane."
                ),
                (
                    "Each TX radius has an independent truncated Gaussian prior "
                    "centered at 304.8 mm with 50 mm standard deviation."
                ),
                "Receive-path phase cancels between explicitly paired TX1/TX2 captures.",
                (
                    "An arbitrary common phase for each independently started TX pair "
                    "is marginalized by the likelihood."
                ),
                (
                    "Frequency-specific phase floors cover antenna, multipath and "
                    "unmodeled systematic error; analyzer standard errors are added "
                    "in quadrature when present."
                ),
                (
                    "A failed state contributes zero likelihood only in its capture "
                    "pair; other valid observations from that antenna remain in use."
                ),
            ],
            "selected_state_names": list(selected_names),
            "selected_state_count": len(selected_names),
            "state_accepted_pair_counts": accepted_pair_counts,
            "capture_pair_count": len(pairs),
            "geometry": {
                "original_board_center_mm": board_center.tolist(),
                "inference_center_mm": [0.0, 0.0],
                "plane_z_mm": 0.0,
                "selected_antenna_positions_mm": {
                    name: geometry.antenna_positions_mm[index].tolist()
                    for index, name in enumerate(selected_names)
                },
                "source_coordinate_system": geometry_document.get("coordinate_system"),
            },
            "measurement_rows": rows,
            "posterior": _posterior_document(posterior, args.visualization_particles, args.seed),
        },
    }
    _atomic_write(output_path, document)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "run_id": manifest.get("run_id"),
                "capture_pairs": len(pairs),
                "selected_antennas": list(selected_names),
                "accepted_pair_counts": accepted_pair_counts,
                "effective_sample_size": posterior.effective_sample_size,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
