import copy
import json
from pathlib import Path

import pytest

from smateway.phase_distribution import (
    Fast20PhaseArtifact,
    load_fast20_phase_document,
    parse_fast20_phase_document,
    summarize_paired_tx_phase_differences,
    summarize_phase_replicates,
)


def _document(
    artifact_id: str,
    *,
    tx_channel: int,
    center_frequency_hz: int = 2_400_000_000,
    stream_id: int,
    raw_phases: tuple[float, ...] = (10, 20, 30, 40, 50, 60, 70, 80),
    relative_phases: tuple[float, ...] = (0, 10, 20, 30, 40, 50, 60, 70),
    failed_states: tuple[str, ...] = (),
    complete_cycles: int = 25,
    confidence: float = 0.98,
) -> dict[str, object]:
    state_quality = [f"ANT{index}" not in failed_states for index in range(1, 9)]
    capture_quality = complete_cycles >= 20 and confidence >= 0.9
    return {
        "schema": 1,
        "analysis_kind": "fast20_rx1_referenced_relative_phase",
        "artifact": {
            "artifact_id": artifact_id,
            "sha256": "a" * 64,
            "center_frequency_hz": center_frequency_hz,
        },
        "capture": {
            "tx_channel": tx_channel,
            "center_frequency_hz": center_frequency_hz,
            "stream_id": stream_id,
        },
        "pilot": {"estimated_offset_hz": 99_990.8447},
        "quality_gate": {
            "passed": capture_quality and all(state_quality),
            "minimum_complete_cycles": 20,
            "minimum_overall_confidence": 0.9,
        },
        "phase": {
            "ant1_reference_state": "ANT1",
            "continuity_verified": True,
            "complete_cycle_count": complete_cycles,
            "confidence": confidence,
            "states": [
                {
                    "name": f"ANT{index}",
                    "phase_deg": raw_phases[index - 1],
                    "phase_relative_to_ant1_deg": relative_phases[index - 1],
                    "quality_passed": state_quality[index - 1],
                }
                for index in range(1, 9)
            ],
        },
    }


def _artifact(
    artifact_id: str,
    *,
    tx_channel: int,
    stream_id: int,
    center_frequency_hz: int = 2_400_000_000,
    raw_phases: tuple[float, ...] | None = None,
    relative_phases: tuple[float, ...] = (0, 10, 20, 30, 40, 50, 60, 70),
    failed_states: tuple[str, ...] = (),
    complete_cycles: int = 25,
) -> Fast20PhaseArtifact:
    if raw_phases is None:
        raw_phases = tuple(10.0 + phase for phase in relative_phases)
    return parse_fast20_phase_document(
        _document(
            artifact_id,
            tx_channel=tx_channel,
            stream_id=stream_id,
            center_frequency_hz=center_frequency_hz,
            raw_phases=raw_phases,
            relative_phases=relative_phases,
            failed_states=failed_states,
            complete_cycles=complete_cycles,
        )
    )


def _summary(summaries: tuple[object, ...], state_name: str) -> object:
    return next(summary for summary in summaries if summary.state_name == state_name)


def test_loads_fresh_schema_and_retains_per_state_quality(tmp_path: Path) -> None:
    document = _document(
        "artifact-a",
        tx_channel=0,
        stream_id=101,
        failed_states=("ANT7",),
    )
    path = tmp_path / "fast20-relative-phase.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    artifact = load_fast20_phase_document(path)

    assert artifact.artifact_id == "artifact-a"
    assert artifact.tx_channel == 0
    assert artifact.rf_frequency_hz == pytest.approx(2_400_099_990.8447)
    assert artifact.capture_quality_passed
    assert not artifact.overall_quality_passed
    assert artifact.state("ANT6").quality_passed
    assert not artifact.state("ANT7").quality_passed


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda document: document.update(schema=2), "schema"),
        (
            lambda document: document.update(analysis_kind="clock_coherent_rx2_phase20_fft"),
            "not a Fast20",
        ),
        (
            lambda document: document["phase"].update(continuity_verified=False),
            "continuity",
        ),
        (
            lambda document: document["phase"]["states"][1].update(name="ANT3"),
            "ordered",
        ),
        (
            lambda document: document["quality_gate"].update(passed=True),
            "inconsistent",
        ),
        (
            lambda document: document["phase"]["states"][1].update(phase_relative_to_ant1_deg=11),
            "relative phase is inconsistent",
        ),
    ],
)
def test_rejects_invalid_or_internally_inconsistent_documents(mutation, message: str) -> None:
    document = _document(
        "artifact-a",
        tx_channel=0,
        stream_id=101,
        failed_states=("ANT7",),
    )
    mutation(document)

    with pytest.raises(ValueError, match=message):
        parse_fast20_phase_document(document)


def test_summarizes_wrapped_ant1_relative_replicates_and_quality_masks() -> None:
    first = _artifact(
        "capture-a",
        tx_channel=0,
        stream_id=1,
        relative_phases=(0, 179, 20, 30, 40, 50, 60, 70),
    )
    second = _artifact(
        "capture-b",
        tx_channel=0,
        stream_id=2,
        relative_phases=(0, -179, 21, 31, 41, 51, 61, 71),
    )
    rejected = _artifact(
        "capture-c",
        tx_channel=0,
        stream_id=3,
        relative_phases=(0, 40, 22, 32, 42, 52, 62, 72),
        failed_states=("ANT2",),
    )

    summaries = summarize_phase_replicates((rejected, second, first))
    ant2 = _summary(summaries, "ANT2")

    assert len(summaries) == 8
    assert ant2.attempted_count == 3
    assert ant2.accepted_count == 2
    assert ant2.samples_deg == (179.0, -179.0)
    assert abs(abs(ant2.circular_mean_deg) - 180.0) < 1e-9
    assert ant2.circular_std_deg == pytest.approx(1.0000253865)
    assert ant2.resultant_length == pytest.approx(0.9998476952)
    assert ant2.attempted_artifact_ids == ("capture-a", "capture-b", "capture-c")
    assert ant2.accepted_artifact_ids == ("capture-a", "capture-b")


def test_summarizer_groups_transmitters_and_frequencies_separately() -> None:
    artifacts = (
        _artifact("tx1-24", tx_channel=0, stream_id=1),
        _artifact("tx2-24", tx_channel=1, stream_id=2),
        _artifact(
            "tx1-58",
            tx_channel=0,
            stream_id=3,
            center_frequency_hz=5_800_000_000,
        ),
    )

    summaries = summarize_phase_replicates(artifacts)

    assert len(summaries) == 24
    assert {(item.tx_channel, item.center_frequency_hz) for item in summaries} == {
        (0, 2_400_000_000),
        (1, 2_400_000_000),
        (0, 5_800_000_000),
    }


def test_capture_wide_failure_attempts_but_accepts_no_states() -> None:
    artifact = _artifact(
        "short-capture",
        tx_channel=0,
        stream_id=1,
        complete_cycles=19,
    )

    summaries = summarize_phase_replicates((artifact,))

    assert all(summary.attempted_count == 1 for summary in summaries)
    assert all(summary.accepted_count == 0 for summary in summaries)
    assert all(summary.circular_mean_deg is None for summary in summaries)


@pytest.mark.parametrize("duplicate", ["artifact", "stream"])
def test_replicates_must_be_independent(duplicate: str) -> None:
    first = _artifact("capture-a", tx_channel=0, stream_id=1)
    second_document = _document(
        "capture-a" if duplicate == "artifact" else "capture-b",
        tx_channel=0,
        stream_id=1 if duplicate == "stream" else 2,
    )
    second = parse_fast20_phase_document(second_document)

    with pytest.raises(ValueError, match="distinct"):
        summarize_phase_replicates((first, second))


def test_paired_summary_uses_raw_tx2_minus_tx1_without_ant1_subtraction() -> None:
    tx1_first = _artifact(
        "tx1-a",
        tx_channel=0,
        stream_id=1,
        raw_phases=(100, 170, 30, 40, 50, 60, 70, 80),
        relative_phases=(0, 70, -70, -60, -50, -40, -30, -20),
    )
    tx2_first = _artifact(
        "tx2-a",
        tx_channel=1,
        stream_id=2,
        raw_phases=(110, -170, 35, 45, 55, 65, 75, 85),
        relative_phases=(0, 80, -75, -65, -55, -45, -35, -25),
    )
    tx1_second = _artifact(
        "tx1-b",
        tx_channel=0,
        stream_id=3,
        raw_phases=(120, -170, 30, 40, 50, 60, 70, 80),
        relative_phases=(0, 70, -90, -80, -70, -60, -50, -40),
    )
    tx2_second = _artifact(
        "tx2-b",
        tx_channel=1,
        stream_id=4,
        raw_phases=(130, 170, 35, 45, 55, 65, 75, 85),
        relative_phases=(0, 40, -95, -85, -75, -65, -55, -45),
        failed_states=("ANT2",),
    )

    summaries = summarize_paired_tx_phase_differences(
        ((tx1_first, tx2_first), (tx1_second, tx2_second))
    )
    ant1 = _summary(summaries, "ANT1")
    ant2 = _summary(summaries, "ANT2")

    assert ant1.samples_deg == (10.0, 10.0)
    assert ant1.accepted_count == 2
    assert ant2.samples_deg == (20.0,)
    assert ant2.attempted_count == 2
    assert ant2.accepted_count == 1
    assert ant2.circular_mean_deg == pytest.approx(20.0)
    assert ant2.accepted_artifact_pairs == (("tx1-a", "tx2-a"),)


def test_paired_summary_rejects_wrong_order_or_frequency() -> None:
    tx1 = _artifact("tx1", tx_channel=0, stream_id=1)
    tx2 = _artifact("tx2", tx_channel=1, stream_id=2)
    tx2_other_frequency = _artifact(
        "tx2-58",
        tx_channel=1,
        stream_id=3,
        center_frequency_hz=5_800_000_000,
    )

    with pytest.raises(ValueError, match="ordered"):
        summarize_paired_tx_phase_differences(((tx2, tx1),))
    with pytest.raises(ValueError, match="center frequency"):
        summarize_paired_tx_phase_differences(((tx1, tx2_other_frequency),))


def test_input_document_is_not_mutated() -> None:
    document = _document("capture-a", tx_channel=0, stream_id=1)
    original = copy.deepcopy(document)

    parse_fast20_phase_document(document)

    assert document == original
