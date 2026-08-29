"""Profile-driven fail-closed decoder for RF envelope intervals."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Literal

from .profile import ControlProfile, ControlState

UnknownReason = Literal[
    "no_observable_signal",
    "truncated_capture",
    "ambiguous_duration",
    "missed_or_extra_transition",
    "invalid_order",
    "no_valid_marker",
]


@dataclass(frozen=True, slots=True)
class ObservedInterval:
    signal_present: bool
    duration_ms: float

    def __post_init__(self) -> None:
        if self.duration_ms <= 0:
            raise ValueError("observed interval duration must be positive")


@dataclass(frozen=True, slots=True)
class DecodeResult:
    status: Literal["decoded", "unknown"]
    states: tuple[str, ...] = ()
    reason: UnknownReason | None = None
    marker_index: int | None = None


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """One complete framed selector cycle with its measured interval lengths."""

    marker_index: int
    marker_duration_ms: float
    states: tuple[str, ...]
    dwell_durations_ms: tuple[float, ...]
    guard_durations_ms: tuple[float, ...]
    marker_start_ms: float | None = None
    cycle_duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class DecodedScheduleTiming:
    """Absolute and aggregate timing for strictly decoded complete frames.

    ``marker_start_times_ms`` are measured from the beginning of the observed
    interval stream. ``cycle_jitter_ms`` is the median absolute deviation of
    the measured complete-frame durations from ``median_cycle_ms``. The marker
    phase is a circular mean modulo that median cycle, also referenced to the
    beginning of the observation.
    """

    marker_indices: tuple[int, ...]
    marker_start_times_ms: tuple[float, ...]
    cycle_durations_ms: tuple[float, ...]
    median_cycle_ms: float | None
    cycle_jitter_ms: float | None
    marker_phase_ms: float | None
    marker_count: int
    complete_frame_count: int
    strict_frame_count: int
    edge_truncated_marker_count: int
    rejected_marker_count: int


@dataclass(frozen=True, slots=True)
class FrameScanResult:
    """All complete cycles and fail-closed marker-candidate outcomes."""

    frames: tuple[DecodedFrame, ...]
    marker_count: int
    failures: tuple[DecodeResult, ...]

    @property
    def schedule_timing(self) -> DecodedScheduleTiming:
        """Return absolute timing and robust aggregate statistics."""

        return summarize_schedule_timing(self)


def _marker_phase_ms(
    marker_start_times_ms: tuple[float, ...], median_cycle_ms: float | None
) -> float | None:
    if not marker_start_times_ms or median_cycle_ms is None or median_cycle_ms <= 0:
        return None
    angles = tuple(
        math.tau * ((start_ms % median_cycle_ms) / median_cycle_ms)
        for start_ms in marker_start_times_ms
    )
    mean_sine = math.fsum(math.sin(angle) for angle in angles) / len(angles)
    mean_cosine = math.fsum(math.cos(angle) for angle in angles) / len(angles)
    phase_angle = math.atan2(mean_sine, mean_cosine) % math.tau
    phase_ms = phase_angle * median_cycle_ms / math.tau
    if math.isclose(phase_ms, median_cycle_ms, rel_tol=0.0, abs_tol=1e-12):
        return 0.0
    return phase_ms


def summarize_schedule_timing(scan: FrameScanResult) -> DecodedScheduleTiming:
    """Summarize decoded frame timing without weakening fail-closed decoding."""

    marker_indices = tuple(frame.marker_index for frame in scan.frames)
    marker_start_times_ms = tuple(
        frame.marker_start_ms for frame in scan.frames if frame.marker_start_ms is not None
    )
    cycle_durations_ms = tuple(
        frame.cycle_duration_ms for frame in scan.frames if frame.cycle_duration_ms is not None
    )
    if cycle_durations_ms:
        median_cycle_ms = float(median(cycle_durations_ms))
        cycle_jitter_ms = float(
            median(abs(duration_ms - median_cycle_ms) for duration_ms in cycle_durations_ms)
        )
    else:
        median_cycle_ms = None
        cycle_jitter_ms = None
    edge_truncated_marker_count = sum(
        failure.reason == "truncated_capture" for failure in scan.failures
    )
    return DecodedScheduleTiming(
        marker_indices=marker_indices,
        marker_start_times_ms=marker_start_times_ms,
        cycle_durations_ms=cycle_durations_ms,
        median_cycle_ms=median_cycle_ms,
        cycle_jitter_ms=cycle_jitter_ms,
        marker_phase_ms=_marker_phase_ms(marker_start_times_ms, median_cycle_ms),
        marker_count=scan.marker_count,
        complete_frame_count=len(scan.frames),
        strict_frame_count=len(scan.frames),
        edge_truncated_marker_count=edge_truncated_marker_count,
        rejected_marker_count=len(scan.failures) - edge_truncated_marker_count,
    )


def intervals_from_presence(
    presence: Sequence[bool], *, bin_duration_ms: float
) -> tuple[ObservedInterval, ...]:
    if bin_duration_ms <= 0:
        raise ValueError("bin duration must be positive")
    if not presence:
        return ()
    intervals: list[ObservedInterval] = []
    current = bool(presence[0])
    bins = 1
    for raw_value in presence[1:]:
        value = bool(raw_value)
        if value == current:
            bins += 1
            continue
        intervals.append(
            ObservedInterval(signal_present=current, duration_ms=bins * bin_duration_ms)
        )
        current = value
        bins = 1
    intervals.append(ObservedInterval(signal_present=current, duration_ms=bins * bin_duration_ms))
    return tuple(intervals)


def _normalized(intervals: tuple[ObservedInterval, ...]) -> tuple[ObservedInterval, ...]:
    merged: list[ObservedInterval] = []
    for interval in intervals:
        if merged and merged[-1].signal_present == interval.signal_present:
            previous = merged[-1]
            merged[-1] = ObservedInterval(
                signal_present=previous.signal_present,
                duration_ms=previous.duration_ms + interval.duration_ms,
            )
        else:
            merged.append(interval)
    return tuple(merged)


def _duration_state(duration_ms: float, profile: ControlProfile) -> ControlState | None:
    matches = [
        state for state in profile.states if state.window_ms[0] <= duration_ms <= state.window_ms[1]
    ]
    return matches[0] if len(matches) == 1 else None


def _candidate(
    intervals: tuple[ObservedInterval, ...], marker_index: int, profile: ControlProfile
) -> DecodeResult:
    cursor = marker_index + 1
    decoded: list[str] = []
    guard_tolerance = profile.guard_ms * profile.decoder_window_pct / 100.0
    guard_min = profile.guard_ms - guard_tolerance
    guard_max = profile.guard_ms + guard_tolerance

    for state_index, expected in enumerate(profile.states):
        if cursor >= len(intervals):
            return DecodeResult(status="unknown", reason="truncated_capture")
        observed = intervals[cursor]
        if not observed.signal_present:
            return DecodeResult(status="unknown", reason="missed_or_extra_transition")
        matched = _duration_state(observed.duration_ms, profile)
        if matched is None:
            if cursor + 1 == len(intervals):
                return DecodeResult(status="unknown", reason="truncated_capture")
            return DecodeResult(status="unknown", reason="ambiguous_duration")
        if matched.name != expected.name:
            return DecodeResult(status="unknown", reason="invalid_order")
        decoded.append(matched.name)
        cursor += 1

        if state_index + 1 == len(profile.states):
            break
        if cursor >= len(intervals):
            return DecodeResult(status="unknown", reason="truncated_capture")
        guard = intervals[cursor]
        if guard.signal_present or not guard_min <= guard.duration_ms <= guard_max:
            if cursor + 1 == len(intervals):
                return DecodeResult(status="unknown", reason="truncated_capture")
            return DecodeResult(status="unknown", reason="missed_or_extra_transition")
        cursor += 1

    return DecodeResult(
        status="decoded",
        states=tuple(decoded),
        marker_index=marker_index,
    )


def _decoded_frame(
    intervals: tuple[ObservedInterval, ...],
    marker_index: int,
    profile: ControlProfile,
    *,
    marker_start_ms: float,
) -> DecodedFrame | DecodeResult:
    result = _candidate(intervals, marker_index, profile)
    if result.status != "decoded":
        return result

    cursor = marker_index + 1
    dwell_durations_ms: list[float] = []
    guard_durations_ms: list[float] = []
    for state_index in range(len(profile.states)):
        dwell_durations_ms.append(intervals[cursor].duration_ms)
        cursor += 1
        if state_index + 1 < len(profile.states):
            guard_durations_ms.append(intervals[cursor].duration_ms)
            cursor += 1
    return DecodedFrame(
        marker_index=marker_index,
        marker_duration_ms=intervals[marker_index].duration_ms,
        states=result.states,
        dwell_durations_ms=tuple(dwell_durations_ms),
        guard_durations_ms=tuple(guard_durations_ms),
        marker_start_ms=marker_start_ms,
        cycle_duration_ms=math.fsum(
            (
                intervals[marker_index].duration_ms,
                *dwell_durations_ms,
                *guard_durations_ms,
            )
        ),
    )


def decode_complete_frames(
    intervals: tuple[ObservedInterval, ...], profile: ControlProfile
) -> FrameScanResult:
    """Decode every complete marker-framed cycle in a long observation.

    Partial cycles at either capture edge are retained as fail-closed outcomes;
    they are never promoted into the complete-frame collection.
    """

    normalized = _normalized(intervals)
    interval_start_times_ms: list[float] = []
    elapsed_ms = 0.0
    for interval in normalized:
        interval_start_times_ms.append(elapsed_ms)
        elapsed_ms = math.fsum((elapsed_ms, interval.duration_ms))
    markers = [
        index
        for index, interval in enumerate(normalized)
        if not interval.signal_present and interval.duration_ms >= profile.marker_decoder_min_ms
    ]
    frames: list[DecodedFrame] = []
    failures: list[DecodeResult] = []
    for marker_index in markers:
        result = _decoded_frame(
            normalized,
            marker_index,
            profile,
            marker_start_ms=interval_start_times_ms[marker_index],
        )
        if isinstance(result, DecodedFrame):
            frames.append(result)
        else:
            guard_tolerance = profile.guard_ms * profile.decoder_window_pct / 100.0
            minimum_after_marker_ms = sum(state.window_ms[0] for state in profile.states) + (
                len(profile.states) - 1
            ) * (profile.guard_ms - guard_tolerance)
            observed_after_marker_ms = sum(
                interval.duration_ms for interval in normalized[marker_index + 1 :]
            )
            if observed_after_marker_ms < minimum_after_marker_ms:
                result = DecodeResult(status="unknown", reason="truncated_capture")
            failures.append(result)
    return FrameScanResult(
        frames=tuple(frames),
        marker_count=len(markers),
        failures=tuple(failures),
    )


def decode_intervals(
    intervals: tuple[ObservedInterval, ...], profile: ControlProfile
) -> DecodeResult:
    normalized = _normalized(intervals)
    if not any(interval.signal_present for interval in normalized):
        return DecodeResult(status="unknown", reason="no_observable_signal")
    markers = [
        index
        for index, interval in enumerate(normalized)
        if not interval.signal_present and interval.duration_ms >= profile.marker_decoder_min_ms
    ]
    if not markers:
        return DecodeResult(status="unknown", reason="no_valid_marker")

    failures: list[DecodeResult] = []
    for marker_index in markers:
        result = _candidate(normalized, marker_index, profile)
        if result.status == "decoded":
            return result
        failures.append(result)
    reason_priority: tuple[UnknownReason, ...] = (
        "invalid_order",
        "ambiguous_duration",
        "missed_or_extra_transition",
        "truncated_capture",
    )
    for reason in reason_priority:
        if any(failure.reason == reason for failure in failures):
            return DecodeResult(status="unknown", reason=reason)
    return DecodeResult(status="unknown", reason="no_valid_marker")
