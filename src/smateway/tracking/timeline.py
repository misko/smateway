"""Map acknowledged selector transitions onto a continuous FPGA sample timeline."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from .channel import DwellInterval


@dataclass(frozen=True, slots=True)
class SampleTimeBlock:
    """One contiguous block's sample sequence and estimated realtime interval."""

    first_sample_sequence: int
    sample_count: int
    realtime_start_ns: int
    realtime_end_ns: int
    uncertainty_ns: int

    @property
    def last_sample_sequence_exclusive(self) -> int:
        return self.first_sample_sequence + self.sample_count


@dataclass(frozen=True, slots=True)
class SelectorEvent:
    """One selector state acknowledged during a bounded host-time interval."""

    state: str
    request_start_realtime_ns: int
    request_end_realtime_ns: int


def _validate_blocks(blocks: tuple[SampleTimeBlock, ...]) -> None:
    if not blocks:
        raise ValueError("sample timeline has no blocks")
    for block in blocks:
        if (
            block.first_sample_sequence < 0
            or block.sample_count <= 0
            or block.realtime_start_ns < 0
            or block.realtime_end_ns <= block.realtime_start_ns
            or block.uncertainty_ns < 0
        ):
            raise ValueError("sample timeline contains an invalid block")
    for left, right in pairwise(blocks):
        if left.last_sample_sequence_exclusive != right.first_sample_sequence:
            raise ValueError("sample timeline is not sequence-contiguous")
        if (
            right.realtime_start_ns <= left.realtime_start_ns
            or right.realtime_end_ns <= left.realtime_end_ns
        ):
            raise ValueError("sample timeline realtime anchors do not increase")


def realtime_to_sample_sequence(
    realtime_ns: int,
    blocks: tuple[SampleTimeBlock, ...],
) -> float:
    """Interpolate one realtime instant onto a validated sample sequence."""

    _validate_blocks(blocks)
    if realtime_ns < blocks[0].realtime_start_ns or realtime_ns > blocks[-1].realtime_end_ns:
        raise ValueError("realtime instant lies outside the captured sample timeline")
    for index, block in enumerate(blocks):
        if block.realtime_start_ns <= realtime_ns <= block.realtime_end_ns:
            fraction = (realtime_ns - block.realtime_start_ns) / (
                block.realtime_end_ns - block.realtime_start_ns
            )
            return block.first_sample_sequence + fraction * block.sample_count
        if index + 1 < len(blocks):
            following = blocks[index + 1]
            if block.realtime_end_ns < realtime_ns < following.realtime_start_ns:
                fraction = (realtime_ns - block.realtime_end_ns) / (
                    following.realtime_start_ns - block.realtime_end_ns
                )
                return (
                    block.last_sample_sequence_exclusive
                    + fraction
                    * (
                        following.first_sample_sequence
                        - block.last_sample_sequence_exclusive
                    )
                )
    raise AssertionError("validated realtime instant could not be mapped")


def selector_dwell_intervals(
    blocks: tuple[SampleTimeBlock, ...],
    events: tuple[SelectorEvent, ...],
    *,
    admitted_ports: tuple[str, ...],
    settle_guard_ns: int,
) -> tuple[DwellInterval, ...]:
    """Return conservative stable-state intervals relative to the first block.

    A state is admitted only after its command has returned plus the settling
    guard, and it ends before the next command begins minus that guard. This
    excludes both the unknown command/application interval and RF transients.
    """

    _validate_blocks(blocks)
    if not admitted_ports or len(set(admitted_ports)) != len(admitted_ports):
        raise ValueError("admitted ports must be a non-empty unique sequence")
    if settle_guard_ns < 0:
        raise ValueError("settle guard must not be negative")
    if len(events) < 2:
        raise ValueError("selector timeline needs at least two events")
    for event in events:
        if (
            not event.state
            or event.request_start_realtime_ns < 0
            or event.request_end_realtime_ns < event.request_start_realtime_ns
        ):
            raise ValueError("selector timeline contains an invalid event")
    for left, right in pairwise(events):
        if right.request_start_realtime_ns < left.request_end_realtime_ns:
            raise ValueError("selector command intervals overlap or go backwards")

    first_sequence = blocks[0].first_sample_sequence
    total_samples = blocks[-1].last_sample_sequence_exclusive - first_sequence
    intervals: list[DwellInterval] = []
    admitted = set(admitted_ports)
    for current, following in pairwise(events):
        if current.state not in admitted:
            continue
        stable_start_ns = current.request_end_realtime_ns + settle_guard_ns
        stable_stop_ns = following.request_start_realtime_ns - settle_guard_ns
        if stable_stop_ns <= stable_start_ns:
            raise ValueError(f"selector dwell for {current.state} has no stable interior")
        absolute_start = realtime_to_sample_sequence(stable_start_ns, blocks)
        absolute_stop = realtime_to_sample_sequence(stable_stop_ns, blocks)
        start = max(0, math.ceil(absolute_start - first_sequence))
        stop = min(total_samples, math.floor(absolute_stop - first_sequence))
        if start >= stop:
            raise ValueError(f"selector dwell for {current.state} maps to no samples")
        intervals.append(DwellInterval(current.state, start, stop))
    if not intervals:
        raise ValueError("selector timeline contains no admitted port dwell")
    return tuple(intervals)
