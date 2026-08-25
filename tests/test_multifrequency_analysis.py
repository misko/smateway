import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from smateway.phase_distribution import STATE_NAMES, Fast20PhaseArtifact, Fast20PhaseState

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_dualband_phase_distribution.py"
SPEC = importlib.util.spec_from_file_location("multifrequency_analysis_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)

FREQUENCIES_HZ = (2_401_000_000, 2_437_000_000, 5_800_000_000)


def _condition_order(frequencies_hz: tuple[int, ...]) -> list[dict[str, int]]:
    return [
        {"center_frequency_hz": frequency_hz, "tx_channel": tx_channel}
        for frequency_hz in frequencies_hz
        for tx_channel in (0, 1)
    ]


def _artifact(
    *,
    frequency_hz: int,
    frequency_index: int,
    tx_channel: int,
    round_index: int,
    stream_id: int,
) -> Fast20PhaseArtifact:
    tx1_phase = [round_index * 3.0 + antenna_index * 7.0 for antenna_index in range(8)]
    common_tx_phase = 17.0 + round_index * 2.0
    profile_phase = [
        common_tx_phase + antenna_index * (frequency_index + 1) * 5.0 for antenna_index in range(8)
    ]
    raw_phase = (
        tx1_phase
        if tx_channel == 0
        else [
            tx1_value + profile_value
            for tx1_value, profile_value in zip(tx1_phase, profile_phase, strict=True)
        ]
    )
    ant1_phase = raw_phase[0]
    states = tuple(
        Fast20PhaseState(
            name=name,
            raw_phase_deg=value,
            phase_relative_to_ant1_deg=value - ant1_phase,
            quality_passed=True,
        )
        for name, value in zip(STATE_NAMES, raw_phase, strict=True)
    )
    return Fast20PhaseArtifact(
        artifact_id=f"{stream_id:032x}",
        artifact_sha256="a" * 64,
        tx_channel=tx_channel,
        center_frequency_hz=frequency_hz,
        rf_frequency_hz=float(frequency_hz + 100_000),
        stream_id=stream_id,
        capture_quality_passed=True,
        overall_quality_passed=True,
        states=states,
    )


def _captures() -> tuple[object, ...]:
    captures = []
    stream_id = 1
    plan_index = 0
    for round_index in (1, 2):
        for frequency_index, frequency_hz in enumerate(FREQUENCIES_HZ):
            for tx_channel in (0, 1):
                artifact = _artifact(
                    frequency_hz=frequency_hz,
                    frequency_index=frequency_index,
                    tx_channel=tx_channel,
                    round_index=round_index,
                    stream_id=stream_id,
                )
                captures.append(
                    analysis.CompletedCapture(
                        plan_index=plan_index,
                        round_index=round_index,
                        center_frequency_hz=frequency_hz,
                        tx_channel=tx_channel,
                        attempt_id=plan_index + 1,
                        started_at="2026-08-25T00:00:00+00:00",
                        completed_at="2026-08-25T00:00:10+00:00",
                        analysis_path=Path(f"/tmp/{artifact.artifact_id}.json"),
                        analysis_sha256="b" * 64,
                        metadata_path=Path(f"/tmp/{artifact.artifact_id}.sigmf-meta"),
                        metadata_sha256="c" * 64,
                        continuity=None,
                        artifact=artifact,
                        analyzer_standard_error_deg=(1.0,) * 8,
                    )
                )
                stream_id += 1
                plan_index += 1
    return tuple(captures)


def test_manifest_order_drives_dynamic_interleaved_plan() -> None:
    configuration = {"condition_order": _condition_order(FREQUENCIES_HZ)}

    conditions = analysis._validated_condition_order(configuration)
    plan = analysis._expected_plan(2, conditions)

    assert conditions == tuple(
        (item["center_frequency_hz"], item["tx_channel"])
        for item in configuration["condition_order"]
    )
    assert len(plan) == 12
    assert [item["plan_index"] for item in plan] == list(range(12))
    assert [item["round"] for item in plan] == [1] * 6 + [2] * 6
    assert [item["condition_index"] for item in plan] == list(range(1, 7)) * 2


def test_new_runner_configuration_reconstructs_alternating_round_plan() -> None:
    frequencies = FREQUENCIES_HZ
    configuration = {
        "center_frequencies_hz": list(frequencies),
        "condition_order": _condition_order(frequencies),
        "round_order_policy": [
            {
                "pattern": pattern_index,
                "name": pattern_name,
                "conditions": [
                    {"center_frequency_hz": frequency_hz, "tx_channel": tx_channel}
                    for frequency_hz, tx_channel in analysis._round_condition_order(
                        frequencies, pattern_index
                    )
                ],
            }
            for pattern_index, pattern_name in enumerate(analysis.ROUND_ORDER_POLICY, start=1)
        ],
    }

    assert analysis._validated_multifrequency_configuration(configuration) == frequencies
    conditions = analysis._validated_condition_order(configuration)
    plan = analysis._expected_plan(3, conditions, alternating_round_order=True)
    conditions_per_round = len(frequencies) * 2

    for round_index in range(1, 4):
        start = (round_index - 1) * conditions_per_round
        observed = tuple(
            (item["center_frequency_hz"], item["tx_channel"])
            for item in plan[start : start + conditions_per_round]
        )
        assert observed == analysis._round_condition_order(frequencies, round_index)
        assert {
            item["round_order_pattern"] for item in plan[start : start + conditions_per_round]
        } == {round_index}


@pytest.mark.parametrize(
    ("order", "message"),
    [
        (
            [{"center_frequency_hz": 2_401_000_000, "tx_channel": 0}],
            "complete TX1/TX2 pairs",
        ),
        (
            [
                {"center_frequency_hz": 2_401_000_000, "tx_channel": 1},
                {"center_frequency_hz": 2_401_000_000, "tx_channel": 0},
            ],
            "adjacent same-frequency",
        ),
        (
            _condition_order((2_401_000_000, 2_401_000_000)),
            "duplicate frequency",
        ),
        (
            _condition_order((5_810_000_000,)),
            "unsupported center frequency",
        ),
    ],
)
def test_manifest_order_rejects_unpaired_duplicate_or_unsupported_profiles(
    order: list[dict[str, int]], message: str
) -> None:
    with pytest.raises(analysis.AnalysisError, match=message):
        analysis._validated_condition_order({"condition_order": order})


def test_systematic_floor_covers_2g4_ism_and_only_exact_5g8() -> None:
    assert analysis._systematic_floor(2_400_000_000, 17.0, 33.0) == 17.0
    assert analysis._systematic_floor(2_483_500_000, 17.0, 33.0) == 17.0
    assert analysis._systematic_floor(5_800_000_000, 17.0, 33.0) == 33.0
    with pytest.raises(analysis.AnalysisError, match="unsupported center frequency"):
        analysis._systematic_floor(2_483_500_001, 17.0, 33.0)


def test_pairs_and_aggregates_each_frequency_once_without_repeat_pseudoreplication() -> None:
    captures = _captures()
    pairs = analysis._pair_captures(captures)
    centered_positions = np.asarray(
        (
            (-15.0, -62.5),
            (-30.0, -62.5),
            (-75.0, -4.5),
            (-75.0, 13.5),
            (75.0, 13.5),
            (75.0, -4.5),
            (30.0, -62.5),
            (15.0, -62.5),
        )
    )

    (
        selected_names,
        accepted_counts,
        measurements,
        geometry,
        raw_pair_rows,
        frequency_rows,
    ) = analysis._localization_inputs(
        pairs,
        centered_positions,
        floor_2g4=17.0,
        floor_5g8=33.0,
    )

    assert len(pairs) == 6
    assert len(raw_pair_rows) == 6
    assert len(frequency_rows) == 3
    assert [row["center_frequency_hz"] for row in frequency_rows] == list(FREQUENCIES_HZ)
    assert [row["replicate_pair_count"] for row in frequency_rows] == [2, 2, 2]
    assert [row["systematic_floor_deg"] for row in frequency_rows] == [17.0, 17.0, 33.0]
    assert all(not row["used_as_independent_posterior_row"] for row in raw_pair_rows)
    assert measurements.capture_pair_count == 3
    np.testing.assert_allclose(
        measurements.carrier_frequency_hz,
        np.asarray(FREQUENCIES_HZ, dtype=np.float64) + 100_000.0,
    )
    assert selected_names == STATE_NAMES
    assert geometry.antenna_positions_mm.shape == (8, 2)
    assert all(count == 2 for counts in accepted_counts.values() for count in counts.values())

    posterior = analysis.infer_dual_tx_importance(
        measurements,
        geometry,
        sample_count=1_000,
        seed=1234,
        likelihood=analysis.CircularLikelihood(
            systematic_phase_std_deg=0.0,
            minimum_phase_std_deg=0.1,
        ),
    )
    diagnosed_rows = analysis._frequency_rows_with_map_residuals(frequency_rows, posterior)
    assert len(diagnosed_rows) == 3
    assert all(row["map_residual_diagnostics"]["valid_state_count"] == 8 for row in diagnosed_rows)
    assert all(row["map_residual_diagnostics"]["weighted_rms_deg"] >= 0 for row in diagnosed_rows)


def test_pairing_rejects_a_missing_tx_capture_at_any_dynamic_frequency() -> None:
    captures = tuple(
        capture
        for capture in _captures()
        if not (
            capture.round_index == 2
            and capture.center_frequency_hz == FREQUENCIES_HZ[1]
            and capture.tx_channel == 1
        )
    )

    with pytest.raises(analysis.AnalysisError, match="missing an explicit same-frequency TX pair"):
        analysis._pair_captures(captures)
