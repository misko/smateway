import numpy as np
import pytest

from smateway.decoder import DecodedScheduleTiming
from smateway.profile import ControlProfile, ControlState
from smateway.schedule_alignment import (
    AlignmentCandidate,
    AlignmentSearchConfig,
    AlignmentSearchMode,
    evaluate_schedule_alignment,
    search_schedule_alignment,
)

TRUE_CYCLE_MS = 20.2
TRUE_MARKER_PHASE_MS = 7.4
DURATION_MS = 249.75
BIN_DURATION_MS = 0.25


@pytest.fixture
def profile() -> ControlProfile:
    states = (
        ControlState("ANT1", 1, 2, (1.5, 2.5)),
        ControlState("ANT2", 2, 3, (2.5, 3.5)),
        ControlState("ANT3", 3, 4, (3.5, 4.5)),
    )
    return ControlProfile(
        profile_id="alignment-test-v1",
        revision=1,
        contract_sha256="0" * 64,
        all_off_code=0,
        guard_ms=1,
        marker_body_ms=8,
        marker_decoder_min_ms=7,
        nominal_cycle_ms=20,
        recommended_capture_ms=100,
        minimum_complete_frame_capture_ms=40,
        decoder_window_pct=10.0,
        states=states,
    )


@pytest.fixture
def exact_capture(
    profile: ControlProfile,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    times_ms = np.arange(BIN_DURATION_MS / 2.0, DURATION_MS, BIN_DURATION_MS)
    position = np.mod(times_ms - TRUE_MARKER_PHASE_MS, TRUE_CYCLE_MS)
    labels = np.full(times_ms.size, len(profile.states), dtype=np.int16)
    scale = TRUE_CYCLE_MS / profile.nominal_cycle_ms
    cursor = (profile.marker_body_ms + profile.guard_ms) * scale
    for index, state in enumerate(profile.states):
        end = cursor + state.dwell_ms * scale
        labels[(position >= cursor) & (position < end)] = index
        cursor = end + profile.guard_ms * scale

    state_deltas = np.asarray(
        (1.0 + 0.3j, -0.4 + 0.8j, 0.65 - 0.55j, 0.0 + 0.0j),
        dtype=np.complex128,
    )
    # The evaluator deliberately estimates and removes a complex linear
    # ALL_OFF baseline independently in every complete cycle.
    baseline = (0.2 + 0.001 * times_ms) + 1j * (-0.1 + 0.0007 * times_ms)
    transfer = np.asarray(baseline + state_deltas[labels], dtype=np.complex128)
    reference_valid = np.ones(times_ms.size, dtype=np.bool_)
    return transfer, reference_valid, times_ms


def _phase_error_ms(actual: float, expected: float, period: float) -> float:
    separation = abs(actual - expected) % period
    return min(separation, period - separation)


def _config(mode: AlignmentSearchMode) -> AlignmentSearchConfig:
    return AlignmentSearchConfig(
        cycle_range_ms=(20.0, 20.4),
        bin_duration_ms=BIN_DURATION_MS,
        edge_exclusion_ms=0.3,
        mode=mode,
        fine_cycle_step_ms=0.2,
        fine_phase_step_ms=0.2,
        coarse_phase_step_ms=1.0,
        refinement_basin_count=4,
        distinct_cycle_separation_ms=0.3,
        distinct_phase_separation_ms=2.0,
    )


def test_exact_tone_candidate_reports_near_perfect_component_fit(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture

    candidate = evaluate_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        cycle_ms=TRUE_CYCLE_MS,
        marker_phase_ms=TRUE_MARKER_PHASE_MS,
        edge_exclusion_ms=0.3,
        profile=profile,
    )

    assert candidate is not None
    assert candidate.complete_cycle_count == 11
    assert candidate.quality.explained_fraction > 1.0 - 1e-12
    assert candidate.quality.residual_fraction < 1e-12
    assert candidate.quality.residual_energy < candidate.quality.null_energy * 1e-12
    assert candidate.quality.even_odd_agreement > 1.0 - 1e-12
    assert candidate.quality.cycle_coherence > 1.0 - 1e-12
    assert candidate.quality.detection_strength > 1.0 - 1e-12
    assert candidate.quality.combined_score > 1.0 - 1e-12
    assert candidate.quality.selected_bin_count > 500


def test_exhaustive_fine_search_is_a_complete_oracle_with_provenance(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture

    result = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        profile=profile,
        config=_config(AlignmentSearchMode.EXHAUSTIVE_FINE),
    )

    assert result.cycle_ms == pytest.approx(TRUE_CYCLE_MS, abs=0.2)
    assert _phase_error_ms(result.marker_phase_ms, TRUE_MARKER_PHASE_MS, result.cycle_ms) <= 0.3
    assert result.score > 1.0 - 1e-12
    assert result.quality is result.selected.quality
    assert result.provenance.mode is AlignmentSearchMode.EXHAUSTIVE_FINE
    assert result.provenance.candidate_count == result.provenance.fine_candidate_count
    assert result.provenance.coarse_candidate_count == 0
    assert result.provenance.valid_candidate_count > 0
    expected_candidate_count = sum(
        len(np.arange(0.0, cycle_ms, 0.2)) for cycle_ms in (20.0, 20.2, 20.4)
    )
    assert result.provenance.candidate_count == expected_candidate_count
    assert result.distinct_runner_up is not None
    assert result.score_margin is not None
    assert result.score_margin >= 0.0
    phase_distance = _phase_error_ms(
        result.marker_phase_ms,
        result.distinct_runner_up.marker_phase_ms,
        0.5 * (result.cycle_ms + result.distinct_runner_up.cycle_ms),
    )
    cycle_distance = abs(result.cycle_ms - result.distinct_runner_up.cycle_ms)
    assert phase_distance > 2.0 or cycle_distance > 0.3


def test_global_refined_search_matches_exhaustive_oracle(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture
    exhaustive = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        profile=profile,
        config=_config(AlignmentSearchMode.EXHAUSTIVE_FINE),
    )

    refined = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        profile=profile,
        config=_config(AlignmentSearchMode.GLOBAL_REFINED),
    )

    assert refined.cycle_ms == pytest.approx(exhaustive.cycle_ms, abs=0.2)
    assert (
        _phase_error_ms(refined.marker_phase_ms, exhaustive.marker_phase_ms, refined.cycle_ms)
        <= 0.3
    )
    assert refined.score == pytest.approx(exhaustive.score, abs=1e-12)
    assert refined.provenance.fine_candidate_count > 0
    assert refined.provenance.coarse_candidate_count > 0
    assert refined.provenance.candidate_count < exhaustive.provenance.candidate_count


def test_transition_seeded_search_matches_oracle_and_reports_agreement(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture
    timing = DecodedScheduleTiming(
        marker_indices=tuple(range(11)),
        marker_start_times_ms=tuple(TRUE_MARKER_PHASE_MS + i * TRUE_CYCLE_MS for i in range(11)),
        cycle_durations_ms=(TRUE_CYCLE_MS,) * 11,
        median_cycle_ms=TRUE_CYCLE_MS,
        cycle_jitter_ms=0.0,
        marker_phase_ms=TRUE_MARKER_PHASE_MS,
        marker_count=11,
        complete_frame_count=11,
        strict_frame_count=11,
        edge_truncated_marker_count=0,
        rejected_marker_count=0,
    )

    result = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        profile=profile,
        config=_config(AlignmentSearchMode.TRANSITION_SEEDED),
        decoded_timing=timing,
    )

    assert result.cycle_ms == pytest.approx(TRUE_CYCLE_MS, abs=0.2)
    assert _phase_error_ms(result.marker_phase_ms, TRUE_MARKER_PHASE_MS, result.cycle_ms) <= 0.3
    assert result.score > 1.0 - 1e-12
    assert result.provenance.transition_seed_used
    assert result.decoded_timing_agreement is not None
    assert result.decoded_timing_agreement.agrees
    assert result.decoded_timing_agreement.cycle_error_ms <= 0.2
    assert result.decoded_timing_agreement.marker_error_ms <= 0.3


def test_decoder_agreement_accepts_one_bin_quantized_fit_plateau(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture
    decoder_phase_ms = TRUE_MARKER_PHASE_MS + 0.6
    timing = DecodedScheduleTiming(
        marker_indices=tuple(range(11)),
        marker_start_times_ms=tuple(
            decoder_phase_ms + index * TRUE_CYCLE_MS for index in range(11)
        ),
        cycle_durations_ms=(TRUE_CYCLE_MS,) * 11,
        median_cycle_ms=TRUE_CYCLE_MS,
        cycle_jitter_ms=0.0,
        marker_phase_ms=decoder_phase_ms,
        marker_count=11,
        complete_frame_count=11,
        strict_frame_count=11,
        edge_truncated_marker_count=0,
        rejected_marker_count=0,
    )

    result = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        profile=profile,
        config=_config(AlignmentSearchMode.TRANSITION_SEEDED),
        decoded_timing=timing,
    )

    assert result.decoded_timing_agreement is not None
    assert result.decoded_timing_agreement.marker_error_ms == pytest.approx(0.6, abs=0.2)
    assert result.decoded_timing_agreement.marker_tolerance_ms == pytest.approx(0.65)
    assert result.decoded_timing_agreement.agrees


def test_transition_seeded_search_rejects_non_strict_decoder_timing(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture
    timing = DecodedScheduleTiming(
        marker_indices=tuple(range(5)),
        marker_start_times_ms=tuple(TRUE_MARKER_PHASE_MS + i * TRUE_CYCLE_MS for i in range(5)),
        cycle_durations_ms=(TRUE_CYCLE_MS,) * 5,
        median_cycle_ms=TRUE_CYCLE_MS,
        cycle_jitter_ms=0.0,
        marker_phase_ms=TRUE_MARKER_PHASE_MS,
        marker_count=6,
        complete_frame_count=5,
        strict_frame_count=5,
        edge_truncated_marker_count=0,
        rejected_marker_count=1,
    )

    with pytest.raises(ValueError, match="strict marker decoding"):
        search_schedule_alignment(
            transfer,
            reference_valid,
            times_ms,
            duration_ms=DURATION_MS,
            profile=profile,
            config=_config(AlignmentSearchMode.TRANSITION_SEEDED),
            decoded_timing=timing,
        )


def test_fit_components_degrade_monotonically_with_noise(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    transfer, reference_valid, times_ms = exact_capture
    rng = np.random.default_rng(20260829)

    def candidate(noise_scale: float) -> AlignmentCandidate:
        noise = noise_scale * (
            rng.standard_normal(transfer.size) + 1j * rng.standard_normal(transfer.size)
        )
        result = evaluate_schedule_alignment(
            transfer + noise,
            reference_valid,
            times_ms,
            duration_ms=DURATION_MS,
            cycle_ms=TRUE_CYCLE_MS,
            marker_phase_ms=TRUE_MARKER_PHASE_MS,
            edge_exclusion_ms=0.3,
            profile=profile,
        )
        assert result is not None
        return result

    exact = candidate(0.0)
    mild = candidate(0.03)
    noisy = candidate(0.30)

    assert exact.quality.explained_fraction > mild.quality.explained_fraction
    assert mild.quality.explained_fraction > noisy.quality.explained_fraction
    assert exact.quality.residual_fraction < mild.quality.residual_fraction
    assert mild.quality.residual_fraction < noisy.quality.residual_fraction
    assert exact.score > mild.score > noisy.score


def test_no_signal_reports_zero_detection_and_combined_score(
    profile: ControlProfile,
    exact_capture: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    _, reference_valid, times_ms = exact_capture
    candidate = evaluate_schedule_alignment(
        np.zeros(times_ms.size, dtype=np.complex128),
        reference_valid,
        times_ms,
        duration_ms=DURATION_MS,
        cycle_ms=TRUE_CYCLE_MS,
        marker_phase_ms=TRUE_MARKER_PHASE_MS,
        edge_exclusion_ms=0.3,
        profile=profile,
    )

    assert candidate is not None
    assert candidate.quality.detection_ratio == 0.0
    assert candidate.quality.detection_strength == 0.0
    assert candidate.quality.cycle_coherence == 0.0
    assert candidate.score == 0.0
