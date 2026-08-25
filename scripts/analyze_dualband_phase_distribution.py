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
from math import atan2, cos, isfinite, log, pi, sin, sqrt
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
ISM_2G4_MIN_HZ = 2_400_000_000
ISM_2G4_MAX_HZ = 2_483_500_000
EXACT_5G8_HZ = 5_800_000_000
ROUND_ORDER_POLICY = (
    "supplied_frequency_order_tx1_then_tx2",
    "reverse_frequency_order_tx2_then_tx1",
    "rotate_frequency_order_alternate_tx_order",
)
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
MINIMUM_LOCALIZATION_ANTENNAS = 4
MINIMUM_ACCEPTED_REPLICATES = 2
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


def _systematic_floor(
    center_frequency_hz: int,
    floor_2g4: float,
    floor_5g8: float,
) -> float:
    if ISM_2G4_MIN_HZ <= center_frequency_hz <= ISM_2G4_MAX_HZ:
        return floor_2g4
    if center_frequency_hz == EXACT_5G8_HZ:
        return floor_5g8
    raise AnalysisError(
        f"localization profile uses unsupported center frequency {center_frequency_hz} Hz; "
        f"supported frequencies are {ISM_2G4_MIN_HZ}..{ISM_2G4_MAX_HZ} Hz and "
        f"exactly {EXACT_5G8_HZ} Hz"
    )


def _validated_condition_order(
    configuration: Mapping[str, Any],
) -> tuple[tuple[int, int], ...]:
    raw_order = _sequence(configuration.get("condition_order"), "condition_order")
    if not raw_order:
        raise AnalysisError("manifest condition order must contain at least one TX pair")
    conditions: list[tuple[int, int]] = []
    for index, raw_item in enumerate(raw_order):
        item = _mapping(raw_item, f"condition_order[{index}]")
        if set(item) != {"center_frequency_hz", "tx_channel"}:
            raise AnalysisError(
                "each manifest condition-order item must contain only "
                "center_frequency_hz and tx_channel"
            )
        frequency_hz = _integer(
            item.get("center_frequency_hz"),
            f"condition_order[{index}].center_frequency_hz",
        )
        tx_channel = _integer(item.get("tx_channel"), f"condition_order[{index}].tx_channel")
        if frequency_hz <= 0:
            raise AnalysisError("manifest condition frequencies must be positive")
        if tx_channel not in (0, 1):
            raise AnalysisError("manifest condition TX channel must be 0 or 1")
        conditions.append((frequency_hz, tx_channel))

    if len(conditions) % 2:
        raise AnalysisError("manifest condition order must contain complete TX1/TX2 pairs")
    frequencies: list[int] = []
    for index in range(0, len(conditions), 2):
        tx1, tx2 = conditions[index : index + 2]
        if tx1[1] != 0 or tx2[1] != 1 or tx1[0] != tx2[0]:
            raise AnalysisError(
                "manifest condition order must contain adjacent same-frequency TX1/TX2 pairs"
            )
        if tx1[0] in frequencies:
            raise AnalysisError("manifest condition order contains a duplicate frequency pair")
        _systematic_floor(tx1[0], 1.0, 1.0)
        frequencies.append(tx1[0])
    return tuple(conditions)


def _round_condition_order(
    center_frequencies_hz: Sequence[int], round_index: int
) -> tuple[tuple[int, int], ...]:
    """Reconstruct the runner's deterministic drift-detection order."""

    pattern_index = (round_index - 1) % len(ROUND_ORDER_POLICY)
    frequencies = tuple(center_frequencies_hz)
    if pattern_index == 0:
        ordered_frequencies = frequencies
        tx_orders = ((0, 1),) * len(frequencies)
    elif pattern_index == 1:
        ordered_frequencies = tuple(reversed(frequencies))
        tx_orders = ((1, 0),) * len(frequencies)
    else:
        ordered_frequencies = frequencies[1:] + frequencies[:1]
        tx_orders = tuple((0, 1) if index % 2 == 0 else (1, 0) for index in range(len(frequencies)))
    return tuple(
        (frequency_hz, tx_channel)
        for frequency_hz, tx_order in zip(ordered_frequencies, tx_orders, strict=True)
        for tx_channel in tx_order
    )


def _validated_multifrequency_configuration(
    configuration: Mapping[str, Any],
) -> tuple[int, ...] | None:
    """Return new-run frequencies, or None for a legacy fixed-order manifest."""

    raw_frequencies = configuration.get("center_frequencies_hz")
    if raw_frequencies is None:
        return None
    frequencies = tuple(
        _integer(value, f"configuration.center_frequencies_hz[{index}]")
        for index, value in enumerate(
            _sequence(raw_frequencies, "configuration.center_frequencies_hz")
        )
    )
    if not frequencies:
        raise AnalysisError("configuration must contain at least one center frequency")
    if len(set(frequencies)) != len(frequencies):
        raise AnalysisError("configuration center frequencies must be unique")
    for frequency_hz in frequencies:
        _systematic_floor(frequency_hz, 1.0, 1.0)

    condition_order = _validated_condition_order(configuration)
    if condition_order != _round_condition_order(frequencies, 1):
        raise AnalysisError("configuration condition order differs from its center-frequency list")
    expected_policy = [
        {
            "pattern": pattern_index,
            "name": pattern_name,
            "conditions": [
                {"center_frequency_hz": frequency_hz, "tx_channel": tx_channel}
                for frequency_hz, tx_channel in _round_condition_order(frequencies, pattern_index)
            ],
        }
        for pattern_index, pattern_name in enumerate(ROUND_ORDER_POLICY, start=1)
    ]
    if configuration.get("round_order_policy") != expected_policy:
        raise AnalysisError("configuration round-order policy is not the reviewed policy")
    return frequencies


def _expected_plan(
    rounds: int,
    condition_order: Sequence[tuple[int, int]],
    *,
    alternating_round_order: bool = False,
) -> list[dict[str, int | str]]:
    plan: list[dict[str, int | str]] = []
    frequencies = tuple(condition_order[index][0] for index in range(0, len(condition_order), 2))
    for round_index in range(1, rounds + 1):
        round_order = (
            _round_condition_order(frequencies, round_index)
            if alternating_round_order
            else tuple(condition_order)
        )
        for condition_index, (frequency_hz, tx_channel) in enumerate(round_order, start=1):
            condition: dict[str, int | str] = {
                "plan_index": len(plan),
                "round": round_index,
                "condition_index": condition_index,
                "center_frequency_hz": frequency_hz,
                "tx_channel": tx_channel,
                "tx_name": f"TX{tx_channel + 1}",
            }
            if alternating_round_order:
                condition["round_order_pattern"] = ((round_index - 1) % len(ROUND_ORDER_POLICY)) + 1
            plan.append(condition)
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
    condition_order = _validated_condition_order(configuration)
    multifrequency_frequencies = _validated_multifrequency_configuration(configuration)
    expected_plan = _expected_plan(
        rounds,
        condition_order,
        alternating_round_order=multifrequency_frequencies is not None,
    )
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
    if not captures:
        raise AnalysisError("no completed captures exist to pair")
    indexed = {
        (capture.round_index, capture.center_frequency_hz, capture.tx_channel): capture
        for capture in captures
    }
    if len(indexed) != len(captures):
        raise AnalysisError("capture condition keys are not unique")
    rounds = sorted({capture.round_index for capture in captures})
    center_frequencies_hz = tuple(
        dict.fromkeys(capture.center_frequency_hz for capture in captures)
    )
    pairs = []
    for round_index in rounds:
        for center_frequency_hz in center_frequencies_hz:
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
    if len(pairs) * 2 != len(captures):
        raise AnalysisError("capture set contains a condition outside the paired round plan")
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


def _localization_inputs(
    pairs: Sequence[CapturePair],
    centered_positions: np.ndarray,
    *,
    floor_2g4: float,
    floor_5g8: float,
) -> tuple[
    tuple[str, ...],
    dict[str, dict[str, int]],
    PairedPhaseMeasurements,
    PlanarArrayGeometry,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    def pair_state_valid(pair: CapturePair, name: str) -> bool:
        return bool(
            pair.tx1.artifact.capture_quality_passed
            and pair.tx2.artifact.capture_quality_passed
            and pair.tx1.artifact.state(name).quality_passed
            and pair.tx2.artifact.state(name).quality_passed
        )

    def analyzer_error(capture: CompletedCapture, name: str) -> float:
        value = capture.analyzer_standard_error_deg[STATE_NAMES.index(name)]
        return 0.0 if value is None else value

    def circular_summary(values_deg: Sequence[float]) -> tuple[float, float, float]:
        radians = [value * pi / 180.0 for value in values_deg]
        mean_cos = sum(cos(value) for value in radians) / len(radians)
        mean_sin = sum(sin(value) for value in radians) / len(radians)
        resultant = min(1.0, max(0.0, sqrt(mean_cos**2 + mean_sin**2)))
        mean_deg = wrap_phase_deg(atan2(mean_sin, mean_cos) * 180.0 / pi)
        repeat_std_deg = sqrt(max(0.0, -2.0 * log(max(resultant, 1e-15)))) * 180.0 / pi
        return mean_deg, repeat_std_deg, resultant

    raw_pair_rows = []
    for pair in pairs:
        raw_phase = []
        raw_pair_error = []
        double_relative_phase = []
        double_relative_pair_error = []
        raw_valid = [pair_state_valid(pair, name) for name in STATE_NAMES]
        ant1_valid = raw_valid[0]
        ant1_raw = wrap_phase_deg(
            pair.tx2.artifact.state("ANT1").raw_phase_deg
            - pair.tx1.artifact.state("ANT1").raw_phase_deg
        )
        ant1_tx1_error = analyzer_error(pair.tx1, "ANT1")
        ant1_tx2_error = analyzer_error(pair.tx2, "ANT1")
        for name in STATE_NAMES:
            value = wrap_phase_deg(
                pair.tx2.artifact.state(name).raw_phase_deg
                - pair.tx1.artifact.state(name).raw_phase_deg
            )
            raw_phase.append(value)
            tx1_error = analyzer_error(pair.tx1, name)
            tx2_error = analyzer_error(pair.tx2, name)
            raw_pair_error.append(sqrt(tx1_error**2 + tx2_error**2))
            double_relative_phase.append(wrap_phase_deg(value - ant1_raw))
            double_relative_pair_error.append(
                0.0
                if name == "ANT1"
                else sqrt(tx1_error**2 + tx2_error**2 + ant1_tx1_error**2 + ant1_tx2_error**2)
            )
        double_valid = [ant1_valid and valid for valid in raw_valid]
        raw_pair_rows.append(
            {
                "round": pair.round_index,
                "center_frequency_hz": pair.center_frequency_hz,
                "carrier_frequency_hz": pair.carrier_frequency_hz,
                "tx1_rf_frequency_hz": pair.tx1.artifact.rf_frequency_hz,
                "tx2_rf_frequency_hz": pair.tx2.artifact.rf_frequency_hz,
                "tx1_artifact_id": pair.tx1.artifact.artifact_id,
                "tx2_artifact_id": pair.tx2.artifact.artifact_id,
                "state_names": list(STATE_NAMES),
                "raw_valid_mask": raw_valid,
                "double_relative_valid_mask": double_valid,
                "raw_tx2_minus_tx1_phase_deg": raw_phase,
                "raw_pair_analyzer_standard_error_deg": raw_pair_error,
                "double_relative_to_ant1_phase_deg": double_relative_phase,
                "double_relative_pair_analyzer_standard_error_deg": (double_relative_pair_error),
                "systematic_floor_deg": _systematic_floor(
                    pair.center_frequency_hz, floor_2g4, floor_5g8
                ),
                "used_as_independent_posterior_row": False,
            }
        )

    frequency_rows: list[dict[str, Any]] = []
    accepted_counts: dict[str, dict[str, int]] = {}
    center_frequencies_hz = tuple(dict.fromkeys(pair.center_frequency_hz for pair in pairs))
    for center_frequency_hz in center_frequencies_hz:
        frequency_pairs = [
            pair for pair in pairs if pair.center_frequency_hz == center_frequency_hz
        ]
        if not frequency_pairs:
            raise AnalysisError(f"no capture pairs exist at {center_frequency_hz} Hz")
        floor = _systematic_floor(center_frequency_hz, floor_2g4, floor_5g8)
        cell_mean: list[float] = []
        cell_repeat_std: list[float] = []
        cell_resultant: list[float] = []
        cell_aggregate_se: list[float] = []
        cell_sigma: list[float] = []
        cell_valid: list[bool] = []
        cell_counts: dict[str, int] = {}
        accepted_rounds: dict[str, list[int]] = {}
        for name in STATE_NAMES:
            values = []
            pair_errors = []
            rounds = []
            for pair in frequency_pairs:
                if not pair_state_valid(pair, "ANT1") or not pair_state_valid(pair, name):
                    continue
                raw_state = wrap_phase_deg(
                    pair.tx2.artifact.state(name).raw_phase_deg
                    - pair.tx1.artifact.state(name).raw_phase_deg
                )
                raw_ant1 = wrap_phase_deg(
                    pair.tx2.artifact.state("ANT1").raw_phase_deg
                    - pair.tx1.artifact.state("ANT1").raw_phase_deg
                )
                values.append(wrap_phase_deg(raw_state - raw_ant1))
                if name == "ANT1":
                    pair_errors.append(0.0)
                else:
                    pair_errors.append(
                        sqrt(
                            analyzer_error(pair.tx1, name) ** 2
                            + analyzer_error(pair.tx2, name) ** 2
                            + analyzer_error(pair.tx1, "ANT1") ** 2
                            + analyzer_error(pair.tx2, "ANT1") ** 2
                        )
                    )
                rounds.append(pair.round_index)
            count = len(values)
            valid = count >= MINIMUM_ACCEPTED_REPLICATES
            cell_counts[name] = count
            accepted_rounds[name] = rounds
            cell_valid.append(valid)
            if valid:
                mean, repeat_std, resultant = circular_summary(values)
                aggregate_se = sqrt(sum(value**2 for value in pair_errors)) / count
                sigma = sqrt(floor**2 + repeat_std**2 + aggregate_se**2)
            else:
                mean = 0.0
                repeat_std = 0.0
                resultant = 0.0
                aggregate_se = 0.0
                sigma = floor
            cell_mean.append(mean)
            cell_repeat_std.append(repeat_std)
            cell_resultant.append(resultant)
            cell_aggregate_se.append(aggregate_se)
            cell_sigma.append(sigma)
        valid_count = sum(cell_valid)
        if valid_count < MINIMUM_LOCALIZATION_ANTENNAS:
            raise AnalysisError(
                f"frequency profile {center_frequency_hz} Hz has only {valid_count} "
                f"states with at least {MINIMUM_ACCEPTED_REPLICATES} accepted replicates; "
                f"at least {MINIMUM_LOCALIZATION_ANTENNAS} are required"
            )
        accepted_counts[str(center_frequency_hz)] = cell_counts
        carrier_values = [pair.carrier_frequency_hz for pair in frequency_pairs]
        frequency_rows.append(
            {
                "center_frequency_hz": center_frequency_hz,
                "carrier_frequency_hz": sum(carrier_values) / len(carrier_values),
                "carrier_frequency_min_hz": min(carrier_values),
                "carrier_frequency_max_hz": max(carrier_values),
                "replicate_pair_count": len(frequency_pairs),
                "state_names": list(STATE_NAMES),
                "valid_mask": cell_valid,
                "valid_state_count": valid_count,
                "accepted_replicate_count": cell_counts,
                "accepted_rounds": accepted_rounds,
                "circular_mean_double_relative_phase_deg": cell_mean,
                "circular_repeat_standard_deviation_deg": cell_repeat_std,
                "circular_resultant_length": cell_resultant,
                "aggregate_analyzer_standard_error_deg": cell_aggregate_se,
                "systematic_floor_deg": floor,
                "combined_phase_standard_deviation_deg": cell_sigma,
            }
        )

    selected_indices = tuple(
        index
        for index, _name in enumerate(STATE_NAMES)
        if any(row["valid_mask"][index] for row in frequency_rows)
    )
    selected_names = tuple(STATE_NAMES[index] for index in selected_indices)
    phases = np.asarray(
        [
            [row["circular_mean_double_relative_phase_deg"][index] for index in selected_indices]
            for row in frequency_rows
        ]
    )
    uncertainties = np.asarray(
        [
            [row["combined_phase_standard_deviation_deg"][index] for index in selected_indices]
            for row in frequency_rows
        ]
    )
    valid_mask = np.asarray(
        [[row["valid_mask"][index] for index in selected_indices] for row in frequency_rows]
    )
    carrier_array = np.asarray([row["carrier_frequency_hz"] for row in frequency_rows])
    geometry = PlanarArrayGeometry(
        antenna_positions_mm=centered_positions[np.asarray(selected_indices)],
        center_mm=np.asarray((0.0, 0.0)),
    )
    measurements = PairedPhaseMeasurements(
        carrier_frequency_hz=carrier_array,
        tx2_minus_tx1_phase_deg=phases,
        phase_standard_deviation_deg=uncertainties,
        valid_mask=valid_mask,
    )
    selected_frequency_rows = []
    for row in frequency_rows:
        selected_frequency_rows.append(
            {
                **row,
                "state_names": list(selected_names),
                "valid_mask": [row["valid_mask"][index] for index in selected_indices],
                "accepted_replicate_count": {
                    name: row["accepted_replicate_count"][name] for name in selected_names
                },
                "accepted_rounds": {name: row["accepted_rounds"][name] for name in selected_names},
                "circular_mean_double_relative_phase_deg": [
                    row["circular_mean_double_relative_phase_deg"][index]
                    for index in selected_indices
                ],
                "circular_repeat_standard_deviation_deg": [
                    row["circular_repeat_standard_deviation_deg"][index]
                    for index in selected_indices
                ],
                "circular_resultant_length": [
                    row["circular_resultant_length"][index] for index in selected_indices
                ],
                "aggregate_analyzer_standard_error_deg": [
                    row["aggregate_analyzer_standard_error_deg"][index]
                    for index in selected_indices
                ],
                "combined_phase_standard_deviation_deg": [
                    row["combined_phase_standard_deviation_deg"][index]
                    for index in selected_indices
                ],
            }
        )
    return (
        selected_names,
        accepted_counts,
        measurements,
        geometry,
        raw_pair_rows,
        selected_frequency_rows,
    )


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


def _frequency_rows_with_map_residuals(
    frequency_rows: Sequence[Mapping[str, Any]],
    posterior: DualTxPosterior,
) -> list[dict[str, Any]]:
    diagnostics = posterior.map_residuals
    row_count = len(frequency_rows)
    if (
        diagnostics.residual_phase_deg.shape[0] != row_count
        or diagnostics.capture_pair_rms_deg.shape != (row_count,)
        or diagnostics.nuisance_offset_deg.shape != (row_count,)
    ):
        raise AnalysisError("posterior residual rows do not match the frequency profiles")
    result = []
    for index, row in enumerate(frequency_rows):
        valid = np.asarray(row["valid_mask"], dtype=np.bool_)
        residual = diagnostics.residual_phase_deg[index]
        if valid.shape != residual.shape:
            raise AnalysisError("posterior residual columns do not match a frequency profile")
        valid_residual = residual[valid]
        result.append(
            {
                **row,
                "map_residual_diagnostics": {
                    "nuisance_offset_deg": float(diagnostics.nuisance_offset_deg[index]),
                    "weighted_rms_deg": float(diagnostics.capture_pair_rms_deg[index]),
                    "maximum_absolute_residual_deg": float(np.max(np.abs(valid_residual))),
                    "valid_state_count": int(np.count_nonzero(valid)),
                },
            }
        )
    return result


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
    (
        selected_names,
        accepted_replicate_counts,
        measurements,
        geometry,
        raw_pair_rows,
        frequency_profile_rows,
    ) = _localization_inputs(
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
    frequency_profile_rows = _frequency_rows_with_map_residuals(
        frequency_profile_rows,
        posterior,
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
            "center_frequencies_hz": [row["center_frequency_hz"] for row in frequency_profile_rows],
            "carrier_frequencies_hz": [
                row["carrier_frequency_hz"] for row in frequency_profile_rows
            ],
            "systematic_phase_floor_deg": {
                str(row["center_frequency_hz"]): row["systematic_floor_deg"]
                for row in frequency_profile_rows
            },
            "radial_prior": asdict(prior),
            "plane_z_mm": 0.0,
            "minimum_valid_antennas_per_frequency_profile": (MINIMUM_LOCALIZATION_ANTENNAS),
            "minimum_accepted_replicates_per_frequency_state": (MINIMUM_ACCEPTED_REPLICATES),
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
                "calibration-free planar direct-path TX2-minus-TX1 phase from "
                f"{len(frequency_profile_rows)} circularly aggregated frequency profiles, "
                "with one marginalized "
                "circular offset per frequency profile"
            ),
            "repeat_handling": (
                f"{configuration.get('rounds')} raw TX pairs estimate each "
                "frequency/state circular distribution. "
                "The shared frequency-specific systematic floor enters once in each of "
                f"the {len(frequency_profile_rows)} aggregated profile rows; repeats are "
                "not independent posterior "
                "rows and therefore do not pseudoreplicate the systematic uncertainty."
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
                (
                    "Receive-path phase cancels between explicitly paired TX1/TX2 "
                    "captures; each replicate profile is additionally referenced to "
                    "its ANT1 TX2-minus-TX1 phase before circular aggregation."
                ),
                (
                    "One remaining common phase nuisance in each aggregated frequency "
                    "profile is marginalized by the likelihood."
                ),
                (
                    "Frequency-specific phase floors cover antenna, multipath and "
                    "unmodeled systematic error; analyzer standard errors are added "
                    "in quadrature when present."
                ),
                (
                    "Failed replicate states are excluded from their frequency/state "
                    "circular aggregate; cells with fewer than two accepted replicates "
                    "contribute zero likelihood."
                ),
            ],
            "selected_state_names": list(selected_names),
            "selected_state_count": len(selected_names),
            "frequency_state_accepted_replicate_counts": accepted_replicate_counts,
            "capture_pair_count": len(pairs),
            "localization_profile_count": len(frequency_profile_rows),
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
            "raw_pair_rows": raw_pair_rows,
            "frequency_profile_rows": frequency_profile_rows,
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
                "localization_profiles": len(frequency_profile_rows),
                "selected_antennas": list(selected_names),
                "accepted_replicate_counts": accepted_replicate_counts,
                "effective_sample_size": posterior.effective_sample_size,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
