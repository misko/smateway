from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from smateway.fast_tracking import (
    FastTrackingProfile,
    analyze_fast_bearings,
    analyze_fast_phase,
    coherent_product,
    decode_fast_schedule,
    estimate_frequency_difference,
)

ROOT = Path(__file__).resolve().parents[1]
PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")


def _profile(dwell_us: int = 50) -> FastTrackingProfile:
    return FastTrackingProfile.load(
        ROOT / f"profiles/tracking-c6-{dwell_us}us-v1/control_profile.json"
    )


def _synthetic_capture(
    profile: FastTrackingProfile,
    *,
    cycles: int = 120,
    sample_rate_hz: int = 2_000_000,
    difference_hz: float = 200_000.0,
    selector_scale: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(48)
    sample_count = round(
        (cycles * profile.cycle_us * selector_scale + 317.0) * sample_rate_hz / 1e6
    )
    sample = np.arange(sample_count, dtype=np.float64)
    time_us = sample / sample_rate_hz * 1e6 + 113.0
    phase_us = np.mod(time_us / selector_scale, profile.cycle_us)
    gain = np.full(sample_count, 0.015 + 0.005j, dtype=np.complex128)
    port_gains = np.asarray(
        [
            0.72 * np.exp(0.2j),
            0.91 * np.exp(-0.7j),
            0.63 * np.exp(1.1j),
            1.05 * np.exp(-1.9j),
            0.82 * np.exp(2.4j),
            0.54 * np.exp(-2.8j),
        ]
    )
    for index in range(6):
        start = profile.marker_body_us + profile.guard_us + index * (
            profile.dwell_us + profile.guard_us
        )
        selected = (phase_us >= start) & (phase_us < start + profile.dwell_us)
        gain[selected] = port_gains[index]
    reference_tone_hz = -100_000.0
    target_tone_hz = reference_tone_hz + difference_hz
    rx1 = 1200.0 * np.exp(2j * np.pi * reference_tone_hz * sample / sample_rate_hz)
    rx2 = 900.0 * gain * np.exp(2j * np.pi * target_tone_hz * sample / sample_rate_hz)
    noise = 15.0 * (
        rng.standard_normal((2, sample_count))
        + 1j * rng.standard_normal((2, sample_count))
    )
    return rx1 + noise[0], rx2 + noise[1]


def test_generated_profile_loader_matches_physical_c6_order() -> None:
    for dwell_us in (25, 50, 100, 200):
        profile = _profile(dwell_us)
        assert profile.profile_id == f"tracking-c6-{dwell_us}us-v1"
        assert profile.ports == PORTS
        assert profile.dwell_us == dwell_us
        assert profile.guard_us == 20
        assert profile.cycle_us == 180 + 6 * (20 + dwell_us)


def test_rf_decode_and_phase_analysis_recover_fast_schedule() -> None:
    profile = _profile(50)
    rx1, rx2 = _synthetic_capture(profile)
    decode = decode_fast_schedule(
        rx2,
        sample_rate_hz=2_000_000,
        tone_offset_hz=100_000.0,
        profile=profile,
        edge_trim_us=5.0,
    )
    assert len(decode.marker_end_bins) >= 100
    assert decode.cycle_scale_median == pytest.approx(1.0, abs=0.002)
    assert tuple(interval.port for interval in decode.intervals[:6]) == PORTS
    frequency = estimate_frequency_difference(
        rx1,
        rx2,
        decode=decode,
        sample_rate_hz=2_000_000,
        nominal_difference_hz=200_000.0,
    )
    assert frequency.frequency_difference_hz == pytest.approx(200_000.0, abs=0.2)
    assert frequency.search_objective > 0.99
    product = coherent_product(
        rx1,
        rx2,
        sample_rate_hz=2_000_000,
        first_sample_sequence=41_000,
        difference_hz=frequency.frequency_difference_hz,
    )
    analysis = analyze_fast_phase(product, decode=decode, profile=profile)
    assert analysis["frame_count"] >= 99
    assert analysis["samples_per_trimmed_dwell"]["median"] == pytest.approx(80, abs=1)
    assert analysis["integration_study"][0]["phase_rms_deg_maximum_port"] < 0.3
    assert analysis["settled_after_selected_edge_us"] is not None
    assert analysis["settled_after_selected_edge_us"] <= 7.0
    assert analysis["settling_acceptance"]["measurement_window_us"] == 5.0
    bearings = np.arange(0.0, 360.0, 1.0)
    steering = np.exp(1j * np.deg2rad(bearings[:, None]) * np.arange(6)[None, :])
    bearing = analyze_fast_bearings(
        product,
        decode=decode,
        profile=profile,
        calibration_coefficients=np.ones(6),
        steering=steering,
        bearings_deg=bearings,
        expected_bearing_deg=None,
    )
    assert bearing["integration_study"][0]["group_count"] >= 99
    assert bearing["integration_study"][0]["bearing_repeatability_rms_deg"] < 1.0


def test_profile_loader_rejects_inconsistent_cycle(tmp_path: Path) -> None:
    path = ROOT / "profiles/tracking-c6-50us-v1/control_profile.json"
    value = json.loads(path.read_text())
    value["frame"]["nominal_cycle_us"] += 1
    broken = tmp_path / "profile.json"
    broken.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="cycle is inconsistent"):
        FastTrackingProfile.load(broken)


@pytest.mark.parametrize("dwell_us", [25, 200])
def test_rf_decode_tracks_selector_clock_error(dwell_us: int) -> None:
    profile = _profile(dwell_us)
    _rx1, rx2 = _synthetic_capture(profile, cycles=40, selector_scale=1.023)
    decode = decode_fast_schedule(
        rx2,
        sample_rate_hz=2_000_000,
        tone_offset_hz=100_000.0,
        profile=profile,
        edge_trim_us=5.0,
    )
    assert decode.cycle_scale_median == pytest.approx(1.023, abs=0.002)
    assert len(decode.marker_end_bins) >= 30


def test_coherent_fold_decodes_when_dark_marker_threshold_is_ambiguous() -> None:
    profile = _profile(200)
    rx1, rx2 = _synthetic_capture(profile, cycles=40, difference_hz=0.0)
    synchronizer = rx2 * np.conjugate(rx1)
    # Equalize the marker magnitude to one selected port so a dark-marker-only
    # detector is deliberately ambiguous; the repeated complex plateaus remain.
    magnitude = np.abs(synchronizer)
    synchronizer = np.where(
        magnitude < 100_000.0,
        700_000.0 * np.exp(0.2j),
        synchronizer,
    )
    decode = decode_fast_schedule(
        synchronizer,
        sample_rate_hz=2_000_000,
        tone_offset_hz=0.0,
        profile=profile,
        edge_trim_us=5.0,
    )
    assert decode.decode_method == "cyclostationary_coherent_fold"
    assert len(decode.marker_end_bins) >= 30
    assert decode.cycle_scale_median == pytest.approx(1.0, abs=0.002)
    assert tuple(interval.port for interval in decode.intervals[:6]) == PORTS


def test_cross_frequency_fit_uses_long_repeated_port_baseline() -> None:
    profile = _profile(100)
    rx1, rx2 = _synthetic_capture(profile, cycles=180, difference_hz=200_015.25)
    decode = decode_fast_schedule(
        rx2,
        sample_rate_hz=2_000_000,
        tone_offset_hz=100_000.0,
        profile=profile,
        edge_trim_us=5.0,
    )
    estimate = estimate_frequency_difference(
        rx1,
        rx2,
        decode=decode,
        sample_rate_hz=2_000_000,
        nominal_difference_hz=200_000.0,
    )
    assert estimate.frequency_difference_hz == pytest.approx(200_015.25, abs=0.1)
    assert estimate.search_objective > 0.99
