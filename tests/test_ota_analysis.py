from pathlib import Path

import numpy as np
import pytest

from smateway.ota_analysis import (
    ALL_OFF,
    ContinuityBlock,
    _labels_and_interior,
    analyze_fast20_phase_sensitive,
    analyze_fast20_tone,
    estimate_coherent_pilot_offset,
)
from smateway.profile import ControlProfile, load_profile

PROFILE_ROOT = Path("profiles/fast20-v1")
SAMPLE_RATE_HZ = 20_000
TONE_OFFSET_HZ = 2_000
PHASE_SAMPLE_RATE_HZ = 10_000
PHASE_TONE_OFFSET_HZ = 1_000


@pytest.fixture
def profile() -> ControlProfile:
    return load_profile(PROFILE_ROOT / "control_profile.json")


def _state_at(
    time_ms: np.ndarray,
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    profile: ControlProfile,
) -> np.ndarray:
    scale = cycle_ms / profile.nominal_cycle_ms
    position = np.mod(time_ms - marker_phase_ms, cycle_ms)
    labels = np.full(time_ms.size, ALL_OFF, dtype=object)
    cursor = (profile.marker_body_ms + profile.guard_ms) * scale
    for state in profile.states:
        end = cursor + state.dwell_ms * scale
        labels[(position >= cursor) & (position < end)] = state.name
        cursor = end + profile.guard_ms * scale
    return labels


def _synthetic_capture(
    profile: ControlProfile,
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    amplitudes: dict[str, float],
    seed: int,
    duration_ms: float = 850.0,
    noise_amplitude: float = 0.02,
    transition_contamination: bool = False,
) -> np.ndarray:
    sample_count = round(SAMPLE_RATE_HZ * duration_ms / 1000.0)
    sample_index = np.arange(sample_count)
    time_ms = sample_index * 1000.0 / SAMPLE_RATE_HZ
    labels = _state_at(
        time_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        profile=profile,
    )
    envelope = np.asarray([amplitudes[str(label)] for label in labels])
    if transition_contamination:
        transitions = np.flatnonzero(labels[1:] != labels[:-1]) + 1
        for transition in transitions:
            radius = SAMPLE_RATE_HZ // 1000
            envelope[max(0, transition - radius) : transition + radius] = 2.0
    tone = envelope * np.exp(2j * np.pi * TONE_OFFSET_HZ * sample_index / SAMPLE_RATE_HZ)
    rng = np.random.default_rng(seed)
    noise = noise_amplitude * (
        rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count)
    )
    return np.asarray(tone + noise, dtype=np.complex64)


def _circular_error(actual: float, expected: float, cycle_ms: float) -> float:
    separation = abs(actual - expected)
    return min(separation, cycle_ms - separation)


def _paired_phase_capture(
    profile: ControlProfile,
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    state_deltas: dict[str, complex],
    duration_ms: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    sample_count = round(PHASE_SAMPLE_RATE_HZ * duration_ms / 1000.0)
    sample_index = np.arange(sample_count)
    time_ms = sample_index * 1000.0 / PHASE_SAMPLE_RATE_HZ
    labels = _state_at(
        time_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        profile=profile,
    )
    selected = np.asarray([state_deltas[str(label)] for label in labels])
    reference_phasor = (1.0 + 0.04 * np.sin(2.0 * np.pi * time_ms / 713.0)) * np.exp(
        1j * (0.25 + 0.08 * np.sin(2.0 * np.pi * time_ms / 1_271.0))
    )
    leakage = (
        2.2
        + 0.35j
        + 0.12 * np.sin(2.0 * np.pi * time_ms / 937.0)
        + 0.08j * np.cos(2.0 * np.pi * time_ms / 1_109.0)
    )
    carrier = np.exp(2j * np.pi * PHASE_TONE_OFFSET_HZ * sample_index / PHASE_SAMPLE_RATE_HZ)
    rng = np.random.default_rng(seed)
    rx1_noise = 0.06 * (rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count))
    rx2_noise = 0.06 * (rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count))
    rx1 = reference_phasor * carrier + rx1_noise
    rx2 = (leakage * reference_phasor + selected) * carrier + rx2_noise
    return np.asarray(rx1, dtype=np.complex64), np.asarray(rx2, dtype=np.complex64)


def _continuity_ledger(sample_count: int) -> tuple[ContinuityBlock, ...]:
    blocks = []
    block_samples = 25_000
    for start in range(0, sample_count, block_samples):
        count = min(block_samples, sample_count - start)
        blocks.append(
            ContinuityBlock(
                sample_start=start,
                sample_count=count,
                utc_ns=1_700_000_000_000_000_000
                + round(start * 1_000_000_000 / PHASE_SAMPLE_RATE_HZ),
            )
        )
    return tuple(blocks)


def test_rounded_modulo_cycle_boundary_maps_to_all_off(profile: ControlProfile) -> None:
    labels, interior = _labels_and_interior(
        np.asarray([0.0]),
        cycle_ms=386.0,
        marker_phase_ms=1e-15,
        edge_exclusion_ms=0.0,
        profile=profile,
    )

    assert labels.tolist() == [len(profile.states)]
    assert interior.tolist() == [True]


def test_arbitrary_capture_phase_reports_all_state_tones(
    profile: ControlProfile,
) -> None:
    expected = {
        ALL_OFF: 0.004,
        "ANT1": 0.08,
        "ANT2": 0.12,
        "ANT3": 0.18,
        "ANT4": 0.25,
        "ANT5": 0.35,
        "ANT6": 0.50,
        "ANT7": 0.70,
        "ANT8": 0.95,
    }
    samples = _synthetic_capture(
        profile,
        cycle_ms=386.0,
        marker_phase_ms=137.4,
        amplitudes=expected,
        seed=41,
    )

    result = analyze_fast20_tone(
        samples,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        profile=profile,
    )

    assert result.cycle_ms == pytest.approx(386.0, abs=0.75)
    assert _circular_error(result.marker_phase_ms, 137.4, result.cycle_ms) < 1.0
    assert tuple(estimate.name for estimate in result.states) == tuple(
        state.name for state in profile.states
    )
    for name in (state.name for state in profile.states):
        estimate = result.estimate(name)
        assert estimate.amplitude == pytest.approx(expected[name], rel=0.06)
        assert estimate.power == pytest.approx(expected[name] ** 2, rel=0.12)
        assert estimate.bin_count > 30
    assert result.estimate(ALL_OFF) is result.all_off
    assert result.all_off.amplitude < 0.01
    assert result.median_contrast_db > 30.0
    assert result.alignment_score > 0.9
    assert result.cycle_repeatability_score > 0.75
    assert result.confidence > 0.8


def test_clock_drift_and_edge_corruption_do_not_bias_state_estimates(
    profile: ControlProfile,
) -> None:
    expected = {
        ALL_OFF: 0.006,
        "ANT1": 0.70,
        "ANT2": 0.20,
        "ANT3": 0.55,
        "ANT4": 0.12,
        "ANT5": 0.42,
        "ANT6": 0.32,
        "ANT7": 0.85,
        "ANT8": 0.25,
    }
    samples = _synthetic_capture(
        profile,
        cycle_ms=389.2,
        marker_phase_ms=331.7,
        amplitudes=expected,
        seed=72,
        duration_ms=900.0,
        transition_contamination=True,
    )

    result = analyze_fast20_tone(
        samples,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        profile=profile,
    )

    assert result.cycle_ms == pytest.approx(389.2, abs=0.45)
    assert _circular_error(result.marker_phase_ms, 331.7, result.cycle_ms) < 1.2
    assert result.edge_exclusion_ms == pytest.approx(1.0)
    for name in (state.name for state in profile.states):
        assert result.estimate(name).amplitude == pytest.approx(expected[name], rel=0.08)
    assert result.confidence > 0.75


def test_phase_sensitive_reference_cancellation_recovers_complex_states(
    profile: ControlProfile,
) -> None:
    amplitudes = np.asarray([0.09, 0.12, 0.16, 0.21, 0.27, 0.34, 0.42, 0.52])
    phases_deg = np.asarray([-145.0, -100.0, -55.0, -10.0, 30.0, 65.0, 105.0, 150.0])
    expected = {ALL_OFF: 0j}
    expected.update(
        {
            state.name: complex(amplitude * np.exp(1j * np.deg2rad(phase)))
            for state, amplitude, phase in zip(profile.states, amplitudes, phases_deg, strict=True)
        }
    )
    rx1, rx2 = _paired_phase_capture(
        profile,
        cycle_ms=385.2,
        marker_phase_ms=123.4,
        state_deltas=expected,
        duration_ms=10_000.0,
        seed=20260825,
    )

    result = analyze_fast20_phase_sensitive(
        rx1,
        rx2,
        sample_rate_hz=PHASE_SAMPLE_RATE_HZ,
        tone_offset_hz=PHASE_TONE_OFFSET_HZ,
        profile=profile,
        continuity_ledger=_continuity_ledger(rx1.size),
    )

    assert result.cycle_ms == pytest.approx(385.2, abs=0.6)
    assert _circular_error(result.marker_phase_ms, 123.4, result.cycle_ms) < 1.5
    assert result.complete_cycle_count >= 24
    assert result.continuity_verified
    assert result.continuity_block_count == 4
    assert result.even_odd_cycle_agreement > 0.9
    assert result.confidence > 0.75
    assert max(estimate.relative_db for estimate in result.states) == pytest.approx(0.0)
    for state in profile.states:
        estimate = result.estimate(state.name)
        target = expected[state.name]
        assert estimate.amplitude == pytest.approx(abs(target), rel=0.12)
        phase_error = abs(
            ((estimate.phase_deg - np.rad2deg(np.angle(target)) + 180.0) % 360.0) - 180.0
        )
        assert phase_error < 8.0
        assert estimate.cycle_coherence > 0.8
        assert estimate.confidence > 0.65


def test_phase_sensitive_noise_only_result_remains_low_confidence(
    profile: ControlProfile,
) -> None:
    no_states = {ALL_OFF: 0j, **{state.name: 0j for state in profile.states}}
    rx1, rx2 = _paired_phase_capture(
        profile,
        cycle_ms=386.0,
        marker_phase_ms=77.0,
        state_deltas=no_states,
        duration_ms=3_000.0,
        seed=731,
    )

    result = analyze_fast20_phase_sensitive(
        rx1,
        rx2,
        sample_rate_hz=PHASE_SAMPLE_RATE_HZ,
        tone_offset_hz=PHASE_TONE_OFFSET_HZ,
        profile=profile,
    )

    assert not result.continuity_verified
    assert result.confidence < 0.4
    assert max(estimate.confidence for estimate in result.states) < 0.55


def test_phase_sensitive_continuity_ledger_fails_closed(profile: ControlProfile) -> None:
    samples = np.ones(20_000, dtype=np.complex64)
    ledger = (
        ContinuityBlock(sample_start=0, sample_count=10_000, utc_ns=1),
        ContinuityBlock(sample_start=10_001, sample_count=9_999, utc_ns=2),
    )

    with pytest.raises(ValueError, match="contiguous"):
        analyze_fast20_phase_sensitive(
            samples,
            samples,
            sample_rate_hz=PHASE_SAMPLE_RATE_HZ,
            tone_offset_hz=PHASE_TONE_OFFSET_HZ,
            profile=profile,
            continuity_ledger=ledger,
        )


@pytest.mark.parametrize("residual_hz", [0.375, -0.425])
def test_coherent_pilot_estimator_refines_nominal_dds_offset(residual_hz: float) -> None:
    duration_s = 2.0
    sample_count = round(PHASE_SAMPLE_RATE_HZ * duration_s)
    sample_index = np.arange(sample_count)
    time_s = sample_index / PHASE_SAMPLE_RATE_HZ
    nominal_hz = PHASE_TONE_OFFSET_HZ
    amplitude = 0.9 + 0.08 * np.sin(2.0 * np.pi * time_s / 0.25)
    phase_wander = 0.025 * np.sin(2.0 * np.pi * time_s / 0.2)
    rng = np.random.default_rng(45)
    noise = 0.05 * (rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count))
    samples = (
        amplitude * np.exp(2j * np.pi * (nominal_hz + residual_hz) * time_s + 1j * phase_wander)
        + noise
    )

    estimate = estimate_coherent_pilot_offset(
        np.asarray(samples, dtype=np.complex64),
        sample_rate_hz=PHASE_SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=nominal_hz,
    )

    assert estimate.residual_offset_hz == pytest.approx(residual_hz, abs=0.01)
    assert estimate.estimated_offset_hz == pytest.approx(nominal_hz + residual_hz, abs=0.01)
    assert estimate.fit_standard_error_hz < 0.01
    assert estimate.bin_count == 2_000
    assert estimate.used_bin_count > 1_990
    assert estimate.phase_step_coherence > 0.98
    assert estimate.confidence > 0.9


def test_coherent_pilot_estimator_marks_noise_as_low_confidence() -> None:
    rng = np.random.default_rng(987)
    noise = rng.standard_normal(20_000) + 1j * rng.standard_normal(20_000)

    estimate = estimate_coherent_pilot_offset(
        np.asarray(noise, dtype=np.complex64),
        sample_rate_hz=PHASE_SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=PHASE_TONE_OFFSET_HZ,
    )

    assert estimate.confidence < 0.2


def test_coherent_pilot_estimator_rejects_ambiguous_search_span() -> None:
    samples = np.ones(2_000, dtype=np.complex64)

    with pytest.raises(ValueError, match="45%"):
        estimate_coherent_pilot_offset(
            samples,
            sample_rate_hz=1_000,
            nominal_tone_offset_hz=100,
            maximum_residual_hz=451,
        )


@pytest.mark.parametrize(
    ("samples", "sample_rate_hz", "tone_offset_hz", "message"),
    [
        (np.ones(100, dtype=np.float64), 20_000, 2_000, "complex"),
        (np.ones(20_000, dtype=np.complex64), 0, 2_000, "sample rate"),
        (np.ones(20_000, dtype=np.complex64), 20_000, 10_000, "Nyquist"),
        (np.ones(2_000, dtype=np.complex64), 20_000, 2_000, "two"),
    ],
)
def test_invalid_inputs_fail_closed(
    profile: ControlProfile,
    samples: np.ndarray,
    sample_rate_hz: float,
    tone_offset_hz: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        analyze_fast20_tone(
            samples,
            sample_rate_hz=sample_rate_hz,
            tone_offset_hz=tone_offset_hz,
            profile=profile,
        )
