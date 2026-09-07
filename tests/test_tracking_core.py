from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from smateway.tracking import (
    BoardCalibrationLut,
    CircularAlphaBetaTracker,
    DwellInterval,
    SampleTimeBlock,
    SelectorEvent,
    estimate_continuous_tone_frequency,
    estimate_cross_frequency_transfers,
    estimate_dwell_phasors,
    estimate_same_emitter_transfer,
    far_field_steering,
    fuse_log_likelihoods,
    load_ism_band_profiles,
    near_field_steering,
    realtime_to_sample_sequence,
    selector_dwell_intervals,
    solve_bearing,
)

ROOT = Path(__file__).resolve().parents[1]
FREQUENCY_PLAN = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
BOARD_LUT = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"
LO_ACCEPTANCE = (
    ROOT
    / "docs/tracking_development_plan/data/receiver-ism-lo-acceptance-20260903.json"
)


def test_ism_profiles_cover_the_hardware_supported_allocations() -> None:
    profiles = load_ism_band_profiles(FREQUENCY_PLAN)

    assert tuple(profiles) == (
        "ism433-c6-v1",
        "ism915-c6-v1",
        "ism2450-c6-v1",
        "ism5800-c6-v1",
    )
    assert profiles["ism433-c6-v1"].primary_centres_hz == (433_920_000,)
    assert profiles["ism915-c6-v1"].primary_centres_hz == (915_000_000,)
    assert profiles["ism2450-c6-v1"].primary_centres_hz == (
        2_425_000_000,
        2_475_000_000,
    )
    assert profiles["ism5800-c6-v1"].primary_centres_hz == (
        5_750_000_000,
        5_800_000_000,
        5_850_000_000,
    )
    for profile in profiles.values():
        assert profile.geometry.ports == ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
        assert profile.geometry.positions_m.shape == (6, 2)
        radii = np.linalg.norm(profile.geometry.positions_m, axis=1)
        assert np.allclose(radii[0], radii)
    assert profiles["ism5800-c6-v1"].current_fixture_ready
    assert all(
        not profile.current_fixture_ready
        for profile_id, profile in profiles.items()
        if profile_id != "ism5800-c6-v1"
    )


def test_board_lut_marks_exact_and_unvalidated_interpolated_frequencies() -> None:
    lut = BoardCalibrationLut.load(BOARD_LUT)
    ports = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")

    high = lut.evaluate(5_800_000_000, ports)
    assert high.exact_knot
    assert high.interpolation_validated
    assert high.coefficients.shape == (6,)
    assert np.all(np.isfinite(high.coefficients))

    low = lut.evaluate(915_000_000, ports)
    assert not low.exact_knot
    assert not low.interpolation_validated
    with pytest.raises(ValueError, match="extrapolation"):
        lut.evaluate(6_000_000_001, ports)
    with pytest.raises(ValueError, match="extrapolation"):
        lut.evaluate(433_920_000, ports)


def test_live_receiver_accepted_every_planned_primary_and_holdout_lo() -> None:
    evidence = json.loads(LO_ACCEPTANCE.read_text(encoding="utf-8"))
    profiles = load_ism_band_profiles(FREQUENCY_PLAN)
    planned = {
        frequency
        for profile in profiles.values()
        for frequency in (
            *profile.primary_centres_hz,
            *profile.blind_frequency_holdouts_hz,
        )
    }
    observed = {item["requested_hz"] for item in evidence["observations"]}

    assert evidence["status"] == "passed"
    assert evidence["safety"]["ota_energy_emitted"] is False
    assert observed == planned
    assert all(item["accepted"] for item in evidence["observations"])


def test_same_emitter_transfer_cancels_arbitrary_source_phase() -> None:
    rng = np.random.default_rng(3)
    count = 8192
    source = rng.choice(np.asarray([1.0, 1.0j, -1.0, -1.0j]), size=count)
    common_phase = np.exp(1j * np.cumsum(rng.normal(0.0, 0.03, size=count)))
    reference = 1.7 * source * common_phase
    expected = 0.42 * np.exp(1j * np.deg2rad(127.0))
    switched = expected * reference

    estimate = estimate_same_emitter_transfer(reference, switched, window=np.hanning(count))

    assert estimate.value == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert estimate.coherence == pytest.approx(1.0)
    assert estimate.residual_snr_db > 120.0


def test_continuous_dwell_projection_uses_global_sample_epoch() -> None:
    sample_rate_hz = 2_000_000.0
    tone_hz = 99_407.0
    first_sequence = 17_000_003
    interval_size = 1200
    ports = ("ANT1", "ANT8", "ANT2")
    expected = np.asarray(
        [
            np.exp(1j * 0.3),
            0.8 * np.exp(-1j * 1.2),
            1.1 * np.exp(1j * 2.1),
        ]
    )
    samples = np.empty(interval_size * len(ports), dtype=np.complex128)
    intervals: list[DwellInterval] = []
    for index, (port, coefficient) in enumerate(zip(ports, expected, strict=True)):
        start = index * interval_size
        stop = start + interval_size
        sequence = first_sequence + np.arange(start, stop)
        samples[start:stop] = coefficient * np.exp(2j * np.pi * tone_hz * sequence / sample_rate_hz)
        intervals.append(DwellInterval(port, start, stop))

    estimates = estimate_dwell_phasors(
        samples,
        tuple(intervals),
        first_sample_sequence=first_sequence,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=tone_hz,
        edge_discard_samples=100,
    )

    assert [item.port for item in estimates] == list(ports)
    assert np.allclose([item.value for item in estimates], expected, atol=1e-10)
    assert min(item.coherence for item in estimates) > 0.999999


def test_selector_events_map_conservatively_onto_metadata_timeline() -> None:
    blocks = tuple(
        SampleTimeBlock(
            first_sample_sequence=1_000_000 + index * 1000,
            sample_count=1000,
            realtime_start_ns=index * 1_000_000,
            realtime_end_ns=(index + 1) * 1_000_000,
            uncertainty_ns=1000,
        )
        for index in range(5)
    )
    events = (
        SelectorEvent("ALL_OFF", 100_000, 200_000),
        SelectorEvent("ANT1", 1_000_000, 1_200_000),
        SelectorEvent("ANT2", 2_800_000, 3_000_000),
        SelectorEvent("ALL_OFF", 4_500_000, 4_700_000),
    )

    assert realtime_to_sample_sequence(2_500_000, blocks) == pytest.approx(1_002_500)
    intervals = selector_dwell_intervals(
        blocks,
        events,
        admitted_ports=("ANT1", "ANT2"),
        settle_guard_ns=100_000,
    )

    assert intervals == (
        DwellInterval("ANT1", 1300, 2700),
        DwellInterval("ANT2", 3100, 4400),
    )


def test_continuous_tone_frequency_ignores_selector_phase_steps() -> None:
    sample_rate_hz = 1_000_000.0
    truth_hz = 100_123.4
    intervals = (
        DwellInterval("ANT1", 1000, 18_000),
        DwellInterval("ANT8", 20_000, 38_000),
        DwellInterval("ANT2", 40_000, 59_000),
    )
    samples = np.zeros(60_000, dtype=np.complex128)
    phases = (0.2, -2.1, 1.3)
    for interval, phase in zip(intervals, phases, strict=True):
        indices = np.arange(interval.start, interval.stop)
        samples[interval.start : interval.stop] = np.exp(
            1j * (2.0 * np.pi * truth_hz * indices / sample_rate_hz + phase)
        )

    estimate = estimate_continuous_tone_frequency(
        samples,
        intervals,
        sample_rate_hz=sample_rate_hz,
        nominal_tone_offset_hz=100_000.0,
        edge_discard_samples=100,
    )

    assert estimate.frequency_hz == pytest.approx(truth_hz, abs=1e-9)
    assert estimate.adjacent_product_coherence > 0.999999


def test_cross_frequency_pilot_cancels_common_phase_and_rejects_tone_leakage() -> None:
    rng = np.random.default_rng(19)
    sample_rate_hz = 1_000_000.0
    first_sequence = 91_000_007
    pilot_hz = -100_000.0
    target_hz = 99_984.75
    interval_size = 90_000
    gap_size = 3000
    ports = ("ANT1", "ANT2", "ANT1", "ANT2")
    coefficients = {
        "ANT1": 0.18 * np.exp(1j * np.deg2rad(32.0)),
        "ANT2": 0.11 * np.exp(1j * np.deg2rad(-117.0)),
    }
    count = len(ports) * (interval_size + gap_size)
    indices = first_sequence + np.arange(count, dtype=np.float64)
    # This deliberately includes non-linear common LO phase: it cancels in the
    # cross-frequency product and need not be modeled by the tracker.
    common_phase = 0.7 + 0.00002 * np.arange(count) + np.cumsum(
        rng.normal(0.0, 2e-4, size=count)
    )
    pilot = np.exp(1j * (2.0 * np.pi * pilot_hz * indices / sample_rate_hz + common_phase))
    target = np.exp(1j * (2.0 * np.pi * target_hz * indices / sample_rate_hz + common_phase))
    reference = 2.0 * pilot + 0.04 * target
    switched = 0.3 * pilot
    intervals: list[DwellInterval] = []
    for dwell_index, port in enumerate(ports):
        start = dwell_index * (interval_size + gap_size)
        stop = start + interval_size
        switched[start:stop] += coefficients[port] * target[start:stop]
        intervals.append(DwellInterval(port, start, stop))
    reference += rng.normal(0.0, 0.05, count) + 1j * rng.normal(0.0, 0.05, count)
    switched += rng.normal(0.0, 0.2, count) + 1j * rng.normal(0.0, 0.2, count)

    estimate = estimate_cross_frequency_transfers(
        reference,
        switched,
        tuple(intervals),
        first_sample_sequence=first_sequence,
        sample_rate_hz=sample_rate_hz,
        nominal_frequency_difference_hz=200_000.0,
        edge_discard_samples=1000,
    )

    assert estimate.frequency_difference_hz == pytest.approx(
        target_hz - pilot_hz, abs=0.02
    )
    values = np.asarray([item.value for item in estimate.phasors])
    measured_ratio = values[1] / values[0]
    expected_ratio = coefficients["ANT2"] / coefficients["ANT1"]
    assert np.angle(measured_ratio / expected_ratio, deg=True) == pytest.approx(0.0, abs=0.4)
    assert abs(values[2] / values[0] - 1.0) < 0.02
    assert abs(values[3] / values[1] - 1.0) < 0.02
    assert estimate.search_objective > 0.8


@pytest.mark.parametrize(
    ("profile_id", "frequency_hz"),
    [
        ("ism915-c6-v1", 915_000_000),
        ("ism2450-c6-v1", 2_425_000_000),
        ("ism5800-c6-v1", 5_800_000_000),
    ],
)
def test_board_corrected_bearing_recovers_tx1_and_tx2_global_phases(
    profile_id: str,
    frequency_hz: int,
) -> None:
    profiles = load_ism_band_profiles(FREQUENCY_PLAN)
    profile = profiles[profile_id]
    lut = BoardCalibrationLut.load(BOARD_LUT)
    correction = lut.evaluate(frequency_hz, profile.geometry.ports).coefficients
    grid = np.arange(0.0, 360.0, 0.25)
    steering = far_field_steering(profile.geometry, frequency_hz, grid)
    truth = 73.25
    true_vector = far_field_steering(profile.geometry, frequency_hz, np.asarray([truth]))[0]

    recovered: list[float] = []
    for arbitrary_source_phase in (0.2, -2.7):
        raw = true_vector / correction * np.exp(1j * arbitrary_source_phase)
        result = solve_bearing(
            raw * correction,
            steering,
            grid,
            minimum_score=0.9,
            minimum_ambiguity_margin_db=0.0,
            maximum_residual_phase_rms_deg=1.0,
        )
        assert result.valid
        assert result.score > 0.999999
        assert result.residual_phase_rms_deg < 1e-8
        recovered.append(result.bearing_deg)
    assert recovered == pytest.approx([truth, truth])


def test_near_field_model_and_log_likelihood_fusion() -> None:
    profile = load_ism_band_profiles(FREQUENCY_PLAN)["ism5800-c6-v1"]
    positions = np.asarray([[0.12, -0.18], [-0.2, 0.1]])
    steering = near_field_steering(profile.geometry, 5_800_000_000, positions)
    assert steering.shape == (2, 6)
    assert not np.allclose(np.abs(steering[0]), np.ones(6))

    fused = fuse_log_likelihoods(
        np.asarray([[0.1, 1.0, 0.2], [0.2, 0.8, 0.1]]),
        frequency_weights=np.asarray([1.0, 2.0]),
    )
    assert np.argmax(fused) == 1
    assert fused[1] == pytest.approx(1.0)


def test_circular_tracker_crosses_zero_and_drops_after_invalid_coasts() -> None:
    tracker = CircularAlphaBetaTracker(alpha=0.5, beta=0.1, maximum_coasts=2)

    first = tracker.update(0.0, 359.0, valid=True)
    second = tracker.update(1.0, 1.0, valid=True)
    assert first is not None and second is not None
    assert second.bearing_deg == pytest.approx(0.0)
    assert second.angular_velocity_deg_s == pytest.approx(0.2)
    assert tracker.update(2.0, None, valid=False) is not None
    assert tracker.update(3.0, None, valid=False) is not None
    assert tracker.update(4.0, None, valid=False) is None
