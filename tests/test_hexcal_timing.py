from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from smateway.hexcal import HexcalAnalysisError, load_hexcal_profile
from smateway.hexcal_timing import (
    SAMPLE_RATE_HZ,
    _interpolated_state_metrics,
    analyze_hexcal_timing_samples,
    coherent_one_microsecond_detector,
)

PROFILE_PATH = Path("profiles/hexcal-v1/control_profile.json")
LEVELS = np.asarray(
    (
        0.90 * np.exp(0.20j),
        1.15 * np.exp(1.10j),
        0.82 * np.exp(-0.80j),
        1.30 * np.exp(2.20j),
        0.95 * np.exp(-2.40j),
        1.05 * np.exp(0.70j),
    ),
    dtype=np.complex128,
)


def _synthetic_raw(
    *,
    cycles: int,
    guard_us: int = 20,
    start_phase_us: float = 317.4,
    tone_offset_hz: float = 100_000.0,
    residual_hz: float = 7.0,
    noise_sigma: float = 0.006,
    corrupt_extra_cycle: int | None = None,
    corrupt_missing_cycle: int | None = None,
    seed: int = 4,
) -> np.ndarray:
    cycle_us = 1_380 + 6 * guard_us
    sample_count = cycles * cycle_us * 5
    time_us = np.arange(sample_count, dtype=np.float64) / 5.0
    absolute_phase = time_us + start_phase_us
    cycle_id = np.floor(absolute_phase / cycle_us).astype(np.int64)
    phase_us = np.mod(absolute_phase, cycle_us)
    envelope = np.full(sample_count, 0.010 + 0.006j, dtype=np.complex128)
    cursor = 180 + guard_us
    state_bounds: list[tuple[int, int]] = []
    for index, level in enumerate(LEVELS):
        start = cursor
        stop = cursor + 200
        state_bounds.append((start, stop))
        selected = (phase_us >= start) & (phase_us < stop)
        envelope[selected] += level
        cursor = stop + (guard_us if index < 5 else 0)

    if corrupt_extra_cycle is not None:
        extra = (
            (cycle_id == corrupt_extra_cycle)
            & (phase_us >= 50.0)
            & (phase_us < 56.0)
        )
        envelope[extra] += 0.7 * np.exp(1.7j)
    if corrupt_missing_cycle is not None:
        # Remove both RF edges around the ANT3->ANT4 ordinary guard by making
        # that guard continue ANT3.  The eventual ANT3->ANT4 change remains,
        # so a schedule-snapping decoder could be fooled; this decoder rejects it.
        ant3_stop = state_bounds[2][1]
        missing = (
            (cycle_id == corrupt_missing_cycle)
            & (phase_us >= ant3_stop)
            & (phase_us < ant3_stop + guard_us)
        )
        envelope[missing] += LEVELS[2]

    rng = np.random.default_rng(seed)
    noise = noise_sigma * (
        rng.normal(size=sample_count) + 1j * rng.normal(size=sample_count)
    )
    sample_indices = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(
        2j
        * np.pi
        * (tone_offset_hz + residual_hz)
        / SAMPLE_RATE_HZ
        * sample_indices
    )
    return np.asarray((envelope + noise) * carrier, dtype=np.complex64)


@pytest.fixture(scope="module")
def good_timing_analysis() -> dict[str, object]:
    return analyze_hexcal_timing_samples(
        _synthetic_raw(cycles=300),
        sample_rate_hz=SAMPLE_RATE_HZ,
        dds_readback_hz=100_000.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )


def test_full_450ms_detector_recovers_submicrosecond_edges_and_passes(
    good_timing_analysis: dict[str, object],
) -> None:
    result = good_timing_analysis
    quality = result["quality"]
    decode = result["decode"]
    timing = result["timing"]
    pilot = result["pilot"]

    assert isinstance(quality, dict) and quality["passed"] is True
    assert quality["frozen_gates"][
        "maximum_refined_pilot_residual_from_dds_readback_hz"
    ] == pytest.approx(2_000.0)
    assert quality["frozen_gates"][
        "minimum_pilot_phase_step_coherence"
    ] == pytest.approx(0.95)
    assert isinstance(decode, dict) and decode["complete_cycle_count"] >= 290
    assert decode["decoded_cycle_fraction"] >= 0.98
    assert decode["visible_edges_per_accepted_cycle"] == 12
    assert isinstance(timing, dict)
    assert timing["combined_rf_marker_us"]["median"] == pytest.approx(200.0, abs=0.1)
    for stats in timing["dwells_us"].values():
        assert stats["median"] == pytest.approx(200.0, abs=0.1)
    for stats in timing["ordinary_guards_us"].values():
        assert stats["q50_us"]["median"] == pytest.approx(20.0, abs=0.1)
        assert stats["conservative_minimum_lower_bound_us"] >= 18.0
        assert stats["conservative_maximum_upper_bound_us"] <= 22.0
    assert timing["maximum_q40_q60_edge_span_us"] < 1.0
    assert timing["maximum_independent_estimator_delta_us"] < 1.0
    assert isinstance(pilot, dict)
    assert pilot["refined_pilot_offset_hz"] == pytest.approx(100_007.0, abs=1.0)
    assert abs(pilot["residual_from_dds_readback_hz"]) <= 2_000.0
    assert pilot["phase_step_coherence"] >= 0.95
    assert result["continuity_verified"] is True
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("guard_us", range(17, 24))
def test_guard_sweep_measures_17_through_23us_without_schedule_snapping(
    guard_us: int,
) -> None:
    result = analyze_hexcal_timing_samples(
        _synthetic_raw(
            cycles=26,
            guard_us=guard_us,
            start_phase_us=0.4,
            tone_offset_hz=0.0,
            residual_hz=3.0,
            seed=guard_us,
        ),
        sample_rate_hz=SAMPLE_RATE_HZ,
        dds_readback_hz=0.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )
    timing = result["timing"]
    quality = result["quality"]
    assert isinstance(timing, dict) and isinstance(quality, dict)
    measured = [
        document["q50_us"]["median"]
        for document in timing["ordinary_guards_us"].values()
    ]
    assert measured == pytest.approx([float(guard_us)] * 5, abs=0.12)
    aggregate_reasons = [
        reason
        for reason in quality["rejection_reasons"]
        if "aggregate_guard_outside_19_21_us" in reason
    ]
    if guard_us not in (19, 20, 21):
        assert aggregate_reasons


def test_missing_and_extra_edges_are_rejected_then_decoder_resynchronizes() -> None:
    result = analyze_hexcal_timing_samples(
        _synthetic_raw(
            cycles=45,
            start_phase_us=0.4,
            tone_offset_hz=0.0,
            residual_hz=2.0,
            corrupt_extra_cycle=10,
            corrupt_missing_cycle=25,
            seed=44,
        ),
        sample_rate_hz=SAMPLE_RATE_HZ,
        dds_readback_hz=0.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )
    decode = result["decode"]
    cycles = result["cycles"]
    assert isinstance(decode, dict) and isinstance(cycles, list)
    assert decode["complete_cycle_count"] < decode["conservative_possible_complete_cycles"]
    assert decode["decoded_cycle_fraction"] < 0.98
    assert any(float(cycle["marker_start_us"]) > 26 * 1_500 for cycle in cycles)
    assert result["quality"]["passed"] is False
    assert "decoded_cycle_fraction_below_98_percent" in result["quality"][
        "rejection_reasons"
    ]


def test_detector_rejects_no_signal_and_unverified_continuity() -> None:
    profile = load_hexcal_profile(PROFILE_PATH)
    no_signal = np.ones(75_000, dtype=np.complex64)
    with pytest.raises(HexcalAnalysisError, match="transitions"):
        analyze_hexcal_timing_samples(
            no_signal,
            sample_rate_hz=SAMPLE_RATE_HZ,
            dds_readback_hz=0.0,
            profile=profile,
            continuity_verified=True,
        )
    with pytest.raises(ValueError, match="verified ABI2 continuity"):
        analyze_hexcal_timing_samples(
            no_signal,
            sample_rate_hz=SAMPLE_RATE_HZ,
            dds_readback_hz=0.0,
            profile=profile,
            continuity_verified=False,
        )


def test_coherent_detector_requires_exact_shape_and_rate() -> None:
    values = np.ones(50, dtype=np.complex64)
    detected = coherent_one_microsecond_detector(
        values, sample_rate_hz=SAMPLE_RATE_HZ, tone_offset_hz=0.0
    )
    assert detected.shape == (10,)
    with pytest.raises(ValueError, match="exactly 5 MS/s"):
        coherent_one_microsecond_detector(
            values, sample_rate_hz=1_000_000, tone_offset_hz=0.0
        )
    with pytest.raises(ValueError, match="divisible"):
        coherent_one_microsecond_detector(
            values[:-1], sample_rate_hz=SAMPLE_RATE_HZ, tone_offset_hz=0.0
        )


def test_time_weighted_null_noise_is_per_bin_and_exact_at_20db_threshold() -> None:
    angles = 2.0 * np.pi * np.arange(200, dtype=float) / 17.0
    noise = np.exp(1j * angles).astype(np.complex128)
    baseline = _interpolated_state_metrics(
        noise,
        noise,
        noise,
        active_center_us=10.0,
        before_center_us=0.0,
        after_center_us=30.0,
    )
    propagated = baseline["propagated_per_bin_complex_noise"]
    result = _interpolated_state_metrics(
        noise + 10.0 * propagated,
        noise,
        noise,
        active_center_us=10.0,
        before_center_us=0.0,
        after_center_us=30.0,
    )

    expected = np.sqrt(
        result["active_per_bin_complex_noise"] ** 2
        + (2.0 / 3.0 * result["before_null_per_bin_complex_noise"]) ** 2
        + (1.0 / 3.0 * result["after_null_per_bin_complex_noise"]) ** 2
    )
    assert result["before_null_weight"] == pytest.approx(2.0 / 3.0)
    assert result["after_null_weight"] == pytest.approx(1.0 / 3.0)
    assert result["propagated_per_bin_complex_noise"] == pytest.approx(expected)
    assert result["pilot_snr_db"] == pytest.approx(20.0, abs=1e-10)
