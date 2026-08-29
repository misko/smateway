from pathlib import Path

import numpy as np
import pytest

from smateway.decoder import DecodedScheduleTiming
from smateway.ota_analysis import ALL_OFF, ContinuityBlock
from smateway.profile import ControlProfile, load_profile
from smateway.reference_transfer import analyze_fast20_reference_transfer
from smateway.schedule_alignment import AlignmentSearchMode

SAMPLE_RATE_HZ = 10_000
TONE_OFFSET_HZ = 1_000


def _labels(
    profile: ControlProfile,
    times_ms: np.ndarray,
    *,
    marker_phase_ms: float,
) -> np.ndarray:
    position = np.mod(times_ms - marker_phase_ms, profile.nominal_cycle_ms)
    labels = np.full(times_ms.size, ALL_OFF, dtype=object)
    cursor = profile.marker_body_ms + profile.guard_ms
    for state in profile.states:
        end = cursor + state.dwell_ms
        labels[(position >= cursor) & (position < end)] = state.name
        cursor = end + profile.guard_ms
    return labels


def _capture(
    profile: ControlProfile,
) -> tuple[np.ndarray, np.ndarray, dict[str, complex]]:
    duration_s = 3.2
    sample_count = round(SAMPLE_RATE_HZ * duration_s)
    sample_index = np.arange(sample_count, dtype=np.float64)
    times_s = sample_index / SAMPLE_RATE_HZ
    times_ms = times_s * 1_000.0
    labels = _labels(profile, times_ms, marker_phase_ms=117.0)
    state_transfer = {
        state.name: complex((0.12 + index * 0.035) * np.exp(1j * np.deg2rad(-140.0 + index * 38.0)))
        for index, state in enumerate(profile.states)
    }
    selected = np.asarray(
        [0j if label == ALL_OFF else state_transfer[str(label)] for label in labels],
        dtype=np.complex128,
    )
    reference_path = (0.9 + 0.03 * np.sin(2.0 * np.pi * times_s / 1.7)) * np.exp(
        1j * (0.45 + 0.025 * np.sin(2.0 * np.pi * times_s / 2.1))
    )
    baseline = 0.32 + 0.08j + 0.015 * np.sin(2.0 * np.pi * times_s / 1.9)
    carrier = np.exp(2j * np.pi * TONE_OFFSET_HZ * times_s)
    rng = np.random.default_rng(20260825)
    noise_scale = 0.002
    rx1 = reference_path * carrier + noise_scale * (
        rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count)
    )
    rx2 = (baseline + selected) * reference_path * carrier + noise_scale * (
        rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count)
    )
    return (
        np.asarray(rx1, dtype=np.complex64),
        np.asarray(rx2, dtype=np.complex64),
        state_transfer,
    )


def _ledger(sample_count: int) -> tuple[ContinuityBlock, ...]:
    blocks = []
    for start in range(0, sample_count, 8_000):
        count = min(8_000, sample_count - start)
        blocks.append(
            ContinuityBlock(
                sample_start=start,
                sample_count=count,
                utc_ns=1_700_000_000_000_000_000 + round(start * 1_000_000_000 / SAMPLE_RATE_HZ),
            )
        )
    return tuple(blocks)


def _decoded_timing(profile: ControlProfile) -> DecodedScheduleTiming:
    marker_starts = tuple(117.0 + index * profile.nominal_cycle_ms for index in range(7))
    return DecodedScheduleTiming(
        marker_indices=tuple(range(len(marker_starts))),
        marker_start_times_ms=marker_starts,
        cycle_durations_ms=(profile.nominal_cycle_ms,) * len(marker_starts),
        median_cycle_ms=profile.nominal_cycle_ms,
        cycle_jitter_ms=0.0,
        marker_phase_ms=117.0,
        marker_count=len(marker_starts),
        complete_frame_count=len(marker_starts),
        strict_frame_count=len(marker_starts),
        edge_truncated_marker_count=0,
        rejected_marker_count=0,
    )


def test_recovers_ota_reference_and_all_off_subtracted_transfer() -> None:
    profile = load_profile(Path("profiles/fast20-v1/control_profile.json"))
    rx1, rx2, expected = _capture(profile)

    analysis = analyze_fast20_reference_transfer(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        profile=profile,
        continuity_ledger=_ledger(rx1.size),
        alignment_search_mode=AlignmentSearchMode.TRANSITION_SEEDED,
        decoded_timing=_decoded_timing(profile),
    )

    assert analysis.continuity_verified
    assert analysis.complete_cycle_count >= 7
    assert analysis.reference_valid_bin_fraction > 0.99
    assert analysis.alignment_score > 0.8
    assert analysis.schedule_alignment is not None
    assert analysis.schedule_alignment.provenance.transition_seed_used
    assert analysis.schedule_alignment.decoded_timing_agreement is not None
    assert analysis.schedule_alignment.decoded_timing_agreement.agrees
    assert analysis.schedule_alignment.quality.explained_fraction > 0.99
    assert analysis.schedule_alignment.quality.residual_fraction < 0.01
    assert analysis.all_off_anchor_count > analysis.complete_cycle_count
    assert analysis.all_off_rx1.amplitude > 0.8
    assert analysis.all_off_rx1.cycle_coherence > 0.99
    assert analysis.all_off_raw_rx2_over_rx1.phasor == pytest.approx(0.32 + 0.08j, rel=0.08)
    assert len(analysis.all_off_raw_rx2_over_rx1.cycle_phasors) == analysis.complete_cycle_count
    for state in profile.states:
        estimate = analysis.estimate(state.name)
        target = expected[state.name]
        measured = estimate.all_off_subtracted_rx2_over_rx1.phasor
        phase_error = abs(
            (np.rad2deg(np.angle(measured * np.conj(target))) + 180.0) % 360.0 - 180.0
        )
        assert abs(measured) == pytest.approx(abs(target), rel=0.08)
        assert phase_error < 4.0
        assert estimate.rx1.amplitude > 0.8
        assert estimate.rx1.cycle_coherence > 0.99
        assert estimate.all_off_subtracted_rx2_over_rx1.cycle_coherence > 0.98
        assert len(estimate.rx1.cycle_phasors) == analysis.complete_cycle_count
        assert len(estimate.raw_rx2_over_rx1.cycle_phasors) == analysis.complete_cycle_count
        assert (
            len(estimate.all_off_subtracted_rx2_over_rx1.cycle_phasors)
            == analysis.complete_cycle_count
        )


def test_reference_transfer_requires_continuous_ota_reference() -> None:
    profile = load_profile(Path("profiles/fast20-v1/control_profile.json"))
    rx1, rx2, _ = _capture(profile)
    rx1[: rx1.size // 2] = 0j

    with pytest.raises(ValueError, match="not continuously usable"):
        analyze_fast20_reference_transfer(
            rx1,
            rx2,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=TONE_OFFSET_HZ,
            profile=profile,
            continuity_ledger=_ledger(rx1.size),
        )
