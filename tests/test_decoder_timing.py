from pathlib import Path

import pytest

from smateway.decoder import (
    DecodedFrame,
    FrameScanResult,
    ObservedInterval,
    decode_complete_frames,
)
from smateway.profile import ControlProfile, load_profile

PROFILE_ROOT = Path("profiles/fast20-v1")


@pytest.fixture
def profile() -> ControlProfile:
    return load_profile(PROFILE_ROOT / "control_profile.json")


def _frame(
    profile: ControlProfile, *, dwell_adjustments_ms: tuple[float, ...] | None = None
) -> tuple[ObservedInterval, ...]:
    adjustments = dwell_adjustments_ms or (0.0,) * len(profile.states)
    intervals = [ObservedInterval(False, profile.marker_body_ms + profile.guard_ms)]
    for index, (state, adjustment_ms) in enumerate(zip(profile.states, adjustments, strict=True)):
        intervals.append(ObservedInterval(True, state.dwell_ms + adjustment_ms))
        if index + 1 < len(profile.states):
            intervals.append(ObservedInterval(False, profile.guard_ms))
    return tuple(intervals)


def test_complete_frames_expose_absolute_marker_and_cycle_timing(
    profile: ControlProfile,
) -> None:
    prefix_ms = 7.0
    intervals = (
        ObservedInterval(True, prefix_ms),
        *_frame(profile),
        *_frame(profile),
    )

    result = decode_complete_frames(intervals, profile)

    assert result.marker_count == 2
    assert tuple(frame.marker_index for frame in result.frames) == (1, 17)
    assert tuple(frame.marker_start_ms for frame in result.frames) == pytest.approx(
        (prefix_ms, prefix_ms + profile.nominal_cycle_ms)
    )
    assert tuple(frame.cycle_duration_ms for frame in result.frames) == pytest.approx(
        (profile.nominal_cycle_ms, profile.nominal_cycle_ms)
    )

    timing = result.schedule_timing
    assert timing.marker_indices == (1, 17)
    assert timing.marker_start_times_ms == pytest.approx((7.0, 393.0))
    assert timing.cycle_durations_ms == pytest.approx((386.0, 386.0))
    assert timing.median_cycle_ms == pytest.approx(386.0)
    assert timing.cycle_jitter_ms == pytest.approx(0.0)
    assert timing.marker_phase_ms == pytest.approx(prefix_ms)
    assert timing.marker_count == 2
    assert timing.complete_frame_count == 2
    assert timing.strict_frame_count == 2
    assert timing.edge_truncated_marker_count == 0
    assert timing.rejected_marker_count == 0


def test_schedule_timing_reports_robust_jitter_and_fail_closed_counts(
    profile: ControlProfile,
) -> None:
    shorter = (-1.0,) + (0.0,) * (len(profile.states) - 1)
    longer = (1.0,) + (0.0,) * (len(profile.states) - 1)
    intervals = (
        ObservedInterval(True, 3.0),
        *_frame(profile, dwell_adjustments_ms=shorter),
        *_frame(profile),
        *_frame(profile, dwell_adjustments_ms=longer),
        ObservedInterval(False, profile.marker_body_ms + profile.guard_ms),
        ObservedInterval(True, profile.states[1].dwell_ms),
        ObservedInterval(False, profile.guard_ms),
        ObservedInterval(True, 400.0),
        ObservedInterval(False, profile.marker_body_ms + profile.guard_ms),
    )

    result = decode_complete_frames(intervals, profile)
    timing = result.schedule_timing

    assert timing.cycle_durations_ms == pytest.approx((385.0, 386.0, 387.0))
    assert timing.median_cycle_ms == pytest.approx(386.0)
    assert timing.cycle_jitter_ms == pytest.approx(1.0)
    assert timing.marker_start_times_ms == pytest.approx((3.0, 388.0, 774.0))
    assert timing.marker_phase_ms == pytest.approx(7.0 / 3.0, abs=0.01)
    assert timing.marker_count == 5
    assert timing.complete_frame_count == 3
    assert timing.strict_frame_count == 3
    assert timing.edge_truncated_marker_count == 1
    assert timing.rejected_marker_count == 1


def test_existing_result_constructors_remain_compatible() -> None:
    frame = DecodedFrame(
        marker_index=0,
        marker_duration_ms=85.0,
        states=("ANT1",),
        dwell_durations_ms=(20.0,),
        guard_durations_ms=(),
    )
    result = FrameScanResult(frames=(frame,), marker_count=1, failures=())

    assert frame.marker_start_ms is None
    assert frame.cycle_duration_ms is None
    assert result.schedule_timing.marker_indices == (0,)
    assert result.schedule_timing.marker_start_times_ms == ()
    assert result.schedule_timing.cycle_durations_ms == ()
    assert result.schedule_timing.median_cycle_ms is None
    assert result.schedule_timing.marker_phase_ms is None


def test_marker_phase_uses_circular_mean_across_cycle_wrap() -> None:
    frames = tuple(
        DecodedFrame(
            marker_index=index,
            marker_duration_ms=85.0,
            states=("ANT1",),
            dwell_durations_ms=(20.0,),
            guard_durations_ms=(),
            marker_start_ms=start_ms,
            cycle_duration_ms=386.0,
        )
        for index, start_ms in enumerate((385.5, 772.5))
    )

    timing = FrameScanResult(frames=frames, marker_count=2, failures=()).schedule_timing

    assert timing.marker_phase_ms == pytest.approx(0.0, abs=1e-12)
