"""Profile-driven fail-closed decoder for RF envelope intervals."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
    intervals.append(
        ObservedInterval(signal_present=current, duration_ms=bins * bin_duration_ms)
    )
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
        state
        for state in profile.states
        if state.window_ms[0] <= duration_ms <= state.window_ms[1]
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
            return DecodeResult(status="unknown", reason="missed_or_extra_transition")
        cursor += 1

    return DecodeResult(
        status="decoded",
        states=tuple(decoded),
        marker_index=marker_index,
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
        if not interval.signal_present
        and interval.duration_ms >= profile.marker_decoder_min_ms
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
