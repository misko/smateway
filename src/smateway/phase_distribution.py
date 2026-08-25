"""Circular summaries for independent Fast20 phase artifacts.

This module is deliberately hardware-free.  It accepts only the persisted
``fast20-relative-phase.json`` schema and keeps every artifact identifier and
quality decision in the returned summaries.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import atan2, cos, isfinite, log, pi, sin, sqrt
from pathlib import Path
from typing import Any

STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 9))
PAIR_RF_TOLERANCE_HZ = 1.0


@dataclass(frozen=True, slots=True)
class Fast20PhaseState:
    """One state estimate and its analyzer-owned quality decision."""

    name: str
    raw_phase_deg: float
    phase_relative_to_ant1_deg: float
    quality_passed: bool


@dataclass(frozen=True, slots=True)
class Fast20PhaseArtifact:
    """Validated fields needed to aggregate one independent capture."""

    artifact_id: str
    artifact_sha256: str
    tx_channel: int
    center_frequency_hz: int
    rf_frequency_hz: float
    stream_id: int
    capture_quality_passed: bool
    overall_quality_passed: bool
    states: tuple[Fast20PhaseState, ...]

    def state(self, name: str) -> Fast20PhaseState:
        """Return one ANT1..ANT8 state."""

        for state in self.states:
            if state.name == name:
                return state
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class PhaseDistribution:
    """ANT1-referenced circular distribution for one TX/frequency/state."""

    tx_channel: int
    center_frequency_hz: int
    state_name: str
    attempted_count: int
    accepted_count: int
    circular_mean_deg: float | None
    circular_std_deg: float | None
    resultant_length: float | None
    samples_deg: tuple[float, ...]
    attempted_artifact_ids: tuple[str, ...]
    accepted_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairedTxPhaseDistribution:
    """Circular distribution of raw TX2-minus-TX1 state phase."""

    center_frequency_hz: int
    state_name: str
    attempted_count: int
    accepted_count: int
    circular_mean_deg: float | None
    circular_std_deg: float | None
    resultant_length: float | None
    samples_deg: tuple[float, ...]
    attempted_artifact_pairs: tuple[tuple[str, str], ...]
    accepted_artifact_pairs: tuple[tuple[str, str], ...]


def wrap_phase_deg(value: float) -> float:
    """Wrap one angle into [-180, 180)."""

    return float((value + 180.0) % 360.0 - 180.0)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _hex_digest(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def parse_fast20_phase_document(document: Mapping[str, Any]) -> Fast20PhaseArtifact:
    """Validate and extract one Fast20 relative-phase schema-1 document.

    A failed all-state gate remains loadable: individual state masks determine
    which observations enter a distribution.  Capture-wide continuity, cycle,
    and confidence gates are applied to every state.
    """

    if document.get("schema") != 1:
        raise ValueError("fast20 phase document schema must be 1")
    if document.get("analysis_kind") != "fast20_rx1_referenced_relative_phase":
        raise ValueError("document is not a Fast20 RX1-referenced phase analysis")

    artifact = _mapping(document.get("artifact"), "artifact")
    artifact_id = _string(artifact.get("artifact_id"), "artifact.artifact_id")
    artifact_sha256 = _hex_digest(artifact.get("sha256"), "artifact.sha256")
    capture = _mapping(document.get("capture"), "capture")
    tx_channel = _integer(capture.get("tx_channel"), "capture.tx_channel")
    if tx_channel not in (0, 1):
        raise ValueError("capture.tx_channel must be 0 or 1")
    center_frequency_hz = _integer(
        capture.get("center_frequency_hz"), "capture.center_frequency_hz"
    )
    if center_frequency_hz <= 0:
        raise ValueError("capture.center_frequency_hz must be positive")
    stream_id = _integer(capture.get("stream_id"), "capture.stream_id")
    if stream_id < 0:
        raise ValueError("capture.stream_id must not be negative")

    artifact_center_hz = _number(
        artifact.get("center_frequency_hz"), "artifact.center_frequency_hz"
    )
    if artifact_center_hz != center_frequency_hz:
        raise ValueError("artifact and capture center frequencies differ")

    pilot = _mapping(document.get("pilot"), "pilot")
    pilot_offset_hz = _number(pilot.get("estimated_offset_hz"), "pilot.estimated_offset_hz")
    rf_frequency_hz = center_frequency_hz + pilot_offset_hz
    if rf_frequency_hz <= 0:
        raise ValueError("derived RF frequency must be positive")

    phase = _mapping(document.get("phase"), "phase")
    if phase.get("ant1_reference_state") != "ANT1":
        raise ValueError("phase.ant1_reference_state must be ANT1")
    if _boolean(phase.get("continuity_verified"), "phase.continuity_verified") is not True:
        raise ValueError("phase analysis does not prove capture continuity")
    complete_cycles = _integer(phase.get("complete_cycle_count"), "phase.complete_cycle_count")
    if complete_cycles < 0:
        raise ValueError("phase.complete_cycle_count must not be negative")
    phase_confidence = _number(phase.get("confidence"), "phase.confidence")
    if not 0.0 <= phase_confidence <= 1.0:
        raise ValueError("phase.confidence must be within 0..1")

    quality_gate = _mapping(document.get("quality_gate"), "quality_gate")
    overall_quality_passed = _boolean(quality_gate.get("passed"), "quality_gate.passed")
    minimum_cycles = _integer(
        quality_gate.get("minimum_complete_cycles"),
        "quality_gate.minimum_complete_cycles",
    )
    if minimum_cycles < 1:
        raise ValueError("quality_gate.minimum_complete_cycles must be positive")
    minimum_overall_confidence = _number(
        quality_gate.get("minimum_overall_confidence"),
        "quality_gate.minimum_overall_confidence",
    )
    if not 0.0 <= minimum_overall_confidence <= 1.0:
        raise ValueError("quality_gate.minimum_overall_confidence must be within 0..1")
    capture_quality_passed = (
        complete_cycles >= minimum_cycles and phase_confidence >= minimum_overall_confidence
    )

    raw_states = _sequence(phase.get("states"), "phase.states")
    if len(raw_states) != len(STATE_NAMES):
        raise ValueError("phase.states must contain ANT1 through ANT8")
    states = []
    for index, expected_name in enumerate(STATE_NAMES):
        raw_state = _mapping(raw_states[index], f"phase.states[{index}]")
        if raw_state.get("name") != expected_name:
            raise ValueError("phase.states must be ordered ANT1 through ANT8")
        states.append(
            Fast20PhaseState(
                name=expected_name,
                raw_phase_deg=wrap_phase_deg(
                    _number(raw_state.get("phase_deg"), f"{expected_name}.phase_deg")
                ),
                phase_relative_to_ant1_deg=wrap_phase_deg(
                    _number(
                        raw_state.get("phase_relative_to_ant1_deg"),
                        f"{expected_name}.phase_relative_to_ant1_deg",
                    )
                ),
                quality_passed=_boolean(
                    raw_state.get("quality_passed"), f"{expected_name}.quality_passed"
                ),
            )
        )
    if abs(states[0].phase_relative_to_ant1_deg) > 1e-6:
        raise ValueError("ANT1 relative phase must be zero")
    for state in states:
        expected_relative = wrap_phase_deg(state.raw_phase_deg - states[0].raw_phase_deg)
        error = abs(wrap_phase_deg(state.phase_relative_to_ant1_deg - expected_relative))
        if error > 1e-6:
            raise ValueError(f"{state.name} relative phase is inconsistent with raw phase")
    expected_overall = capture_quality_passed and all(state.quality_passed for state in states)
    if overall_quality_passed != expected_overall:
        raise ValueError("quality_gate.passed is inconsistent with capture and state gates")

    return Fast20PhaseArtifact(
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        tx_channel=tx_channel,
        center_frequency_hz=center_frequency_hz,
        rf_frequency_hz=rf_frequency_hz,
        stream_id=stream_id,
        capture_quality_passed=capture_quality_passed,
        overall_quality_passed=overall_quality_passed,
        states=tuple(states),
    )


def load_fast20_phase_document(path: Path) -> Fast20PhaseArtifact:
    """Load and validate one persisted ``fast20-relative-phase.json`` file."""

    document = json.loads(path.read_text(encoding="utf-8"))
    return parse_fast20_phase_document(_mapping(document, str(path)))


def _circular_summary(values_deg: Sequence[float]) -> tuple[float, float, float] | None:
    if not values_deg:
        return None
    radians = [value * pi / 180.0 for value in values_deg]
    mean_cos = sum(cos(value) for value in radians) / len(radians)
    mean_sin = sum(sin(value) for value in radians) / len(radians)
    resultant = min(1.0, max(0.0, sqrt(mean_cos**2 + mean_sin**2)))
    mean_deg = wrap_phase_deg(atan2(mean_sin, mean_cos) * 180.0 / pi)
    circular_std_deg = sqrt(max(0.0, -2.0 * log(max(resultant, 1e-15)))) * 180.0 / pi
    return mean_deg, circular_std_deg, resultant


def _assert_independent(artifacts: Sequence[Fast20PhaseArtifact]) -> None:
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("phase replicates must use distinct artifact IDs")
    stream_ids = [artifact.stream_id for artifact in artifacts]
    if len(set(stream_ids)) != len(stream_ids):
        raise ValueError("phase replicates must use distinct stream IDs")


def summarize_phase_replicates(
    artifacts: Iterable[Fast20PhaseArtifact],
) -> tuple[PhaseDistribution, ...]:
    """Summarize ANT1-relative replicates by TX, center frequency, and state."""

    captures = tuple(artifacts)
    _assert_independent(captures)
    groups: dict[tuple[int, int], list[Fast20PhaseArtifact]] = {}
    for artifact in captures:
        groups.setdefault((artifact.tx_channel, artifact.center_frequency_hz), []).append(artifact)

    summaries = []
    for (tx_channel, center_frequency_hz), group in sorted(groups.items()):
        ordered = tuple(sorted(group, key=lambda artifact: artifact.artifact_id))
        for state_name in STATE_NAMES:
            attempted_ids = tuple(artifact.artifact_id for artifact in ordered)
            accepted = tuple(
                artifact
                for artifact in ordered
                if artifact.capture_quality_passed and artifact.state(state_name).quality_passed
            )
            samples = tuple(
                artifact.state(state_name).phase_relative_to_ant1_deg for artifact in accepted
            )
            circular = _circular_summary(samples)
            summaries.append(
                PhaseDistribution(
                    tx_channel=tx_channel,
                    center_frequency_hz=center_frequency_hz,
                    state_name=state_name,
                    attempted_count=len(ordered),
                    accepted_count=len(accepted),
                    circular_mean_deg=None if circular is None else circular[0],
                    circular_std_deg=None if circular is None else circular[1],
                    resultant_length=None if circular is None else circular[2],
                    samples_deg=samples,
                    attempted_artifact_ids=attempted_ids,
                    accepted_artifact_ids=tuple(artifact.artifact_id for artifact in accepted),
                )
            )
    return tuple(summaries)


def summarize_paired_tx_phase_differences(
    pairs: Iterable[tuple[Fast20PhaseArtifact, Fast20PhaseArtifact]],
) -> tuple[PairedTxPhaseDistribution, ...]:
    """Summarize explicitly paired raw-state ``TX2 - TX1`` phase.

    Pairing is supplied by the caller because acquisition order is experiment
    metadata and must not be guessed from artifact IDs or timestamps.  No ANT1
    subtraction is applied to either raw state phase.
    """

    captures = tuple(pairs)
    flattened = tuple(artifact for pair in captures for artifact in pair)
    _assert_independent(flattened)
    groups: dict[int, list[tuple[Fast20PhaseArtifact, Fast20PhaseArtifact]]] = {}
    for tx1, tx2 in captures:
        if tx1.tx_channel != 0 or tx2.tx_channel != 1:
            raise ValueError("each phase pair must be ordered (TX1, TX2)")
        if tx1.center_frequency_hz != tx2.center_frequency_hz:
            raise ValueError("paired TX captures must use one center frequency")
        if abs(tx1.rf_frequency_hz - tx2.rf_frequency_hz) > PAIR_RF_TOLERANCE_HZ:
            raise ValueError("paired TX captures have incompatible RF frequencies")
        groups.setdefault(tx1.center_frequency_hz, []).append((tx1, tx2))

    summaries = []
    for center_frequency_hz, group in sorted(groups.items()):
        ordered = tuple(sorted(group, key=lambda pair: (pair[0].artifact_id, pair[1].artifact_id)))
        for state_name in STATE_NAMES:
            attempted_pairs = tuple((tx1.artifact_id, tx2.artifact_id) for tx1, tx2 in ordered)
            accepted = tuple(
                (tx1, tx2)
                for tx1, tx2 in ordered
                if tx1.capture_quality_passed
                and tx2.capture_quality_passed
                and tx1.state(state_name).quality_passed
                and tx2.state(state_name).quality_passed
            )
            samples = tuple(
                wrap_phase_deg(
                    tx2.state(state_name).raw_phase_deg - tx1.state(state_name).raw_phase_deg
                )
                for tx1, tx2 in accepted
            )
            circular = _circular_summary(samples)
            summaries.append(
                PairedTxPhaseDistribution(
                    center_frequency_hz=center_frequency_hz,
                    state_name=state_name,
                    attempted_count=len(ordered),
                    accepted_count=len(accepted),
                    circular_mean_deg=None if circular is None else circular[0],
                    circular_std_deg=None if circular is None else circular[1],
                    resultant_length=None if circular is None else circular[2],
                    samples_deg=samples,
                    attempted_artifact_pairs=attempted_pairs,
                    accepted_artifact_pairs=tuple(
                        (tx1.artifact_id, tx2.artifact_id) for tx1, tx2 in accepted
                    ),
                )
            )
    return tuple(summaries)
