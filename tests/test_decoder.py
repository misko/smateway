from pathlib import Path

import pytest

from smateway.decoder import ObservedInterval, decode_complete_frames, decode_intervals
from smateway.profile import ControlProfile, load_profile

PROFILE_ROOT = Path("profiles/fast20-v1")


@pytest.fixture
def profile() -> ControlProfile:
    return load_profile(PROFILE_ROOT / "control_profile.json")


def valid_frame(profile: ControlProfile) -> tuple[ObservedInterval, ...]:
    intervals = [
        ObservedInterval(
            signal_present=False,
            duration_ms=profile.marker_body_ms + profile.guard_ms,
        )
    ]
    for index, state in enumerate(profile.states):
        intervals.append(ObservedInterval(signal_present=True, duration_ms=state.dwell_ms))
        if index + 1 < len(profile.states):
            intervals.append(
                ObservedInterval(signal_present=False, duration_ms=profile.guard_ms)
            )
    return tuple(intervals)


def test_valid_complete_frame_decodes(profile: ControlProfile) -> None:
    result = decode_intervals(valid_frame(profile), profile)

    assert result.status == "decoded"
    assert result.states == tuple(state.name for state in profile.states)
    assert result.marker_index == 0


@pytest.mark.parametrize(
    ("intervals", "reason"),
    [
        ((ObservedInterval(False, 850),), "no_observable_signal"),
        (
            (
                ObservedInterval(False, 85),
                ObservedInterval(True, 20),
                ObservedInterval(False, 5),
            ),
            "truncated_capture",
        ),
        (
            (
                ObservedInterval(False, 85),
                ObservedInterval(True, 10),
                ObservedInterval(False, 5),
            ),
            "ambiguous_duration",
        ),
        (
            (
                ObservedInterval(False, 85),
                ObservedInterval(True, 23),
            ),
            "invalid_order",
        ),
        (
            (
                ObservedInterval(False, 85),
                ObservedInterval(True, 20),
                ObservedInterval(False, 7),
                ObservedInterval(True, 23),
            ),
            "missed_or_extra_transition",
        ),
    ],
)
def test_fail_closed_rejection_reasons(
    profile: ControlProfile,
    intervals: tuple[ObservedInterval, ...],
    reason: str,
) -> None:
    result = decode_intervals(intervals, profile)

    assert result.status == "unknown"
    assert result.reason == reason


def test_capture_without_marker_is_unknown(profile: ControlProfile) -> None:
    intervals = valid_frame(profile)[1:]

    result = decode_intervals(intervals, profile)

    assert result.status == "unknown"
    assert result.reason == "no_valid_marker"


def test_long_capture_decodes_every_complete_frame(profile: ControlProfile) -> None:
    first = valid_frame(profile)
    second = tuple(
        ObservedInterval(interval.signal_present, interval.duration_ms + 0.25)
        if interval.signal_present
        else interval
        for interval in valid_frame(profile)
    )
    intervals = (
        ObservedInterval(True, 7),
        *first,
        ObservedInterval(False, profile.guard_ms),
        *second,
        ObservedInterval(False, profile.marker_body_ms + profile.guard_ms),
        ObservedInterval(True, profile.states[0].dwell_ms),
    )

    result = decode_complete_frames(intervals, profile)

    assert result.marker_count == 3
    assert len(result.frames) == 2
    assert result.frames[0].states == tuple(state.name for state in profile.states)
    assert result.frames[0].dwell_durations_ms == tuple(
        state.dwell_ms for state in profile.states
    )
    assert result.frames[1].dwell_durations_ms == tuple(
        state.dwell_ms + 0.25 for state in profile.states
    )
    assert tuple(failure.reason for failure in result.failures) == ("truncated_capture",)


def test_partial_final_dwell_is_classified_as_capture_edge(
    profile: ControlProfile,
) -> None:
    intervals = list(valid_frame(profile))
    intervals[-1] = ObservedInterval(True, 16)

    result = decode_complete_frames(tuple(intervals), profile)

    assert result.marker_count == 1
    assert not result.frames
    assert tuple(failure.reason for failure in result.failures) == ("truncated_capture",)
