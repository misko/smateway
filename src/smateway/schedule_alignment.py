"""Schedule-alignment search and fit diagnostics for coherent fast-switch captures.

The exhaustive search in this module is deliberately simple: it is the offline
correctness oracle against which faster search strategies can be tested.  The
optimized strategies still use the same candidate evaluator and score, so a
search-policy change cannot silently change the definition of a good fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import exp, sqrt

import numpy as np
import numpy.typing as npt

from .decoder import DecodedScheduleTiming
from .profile import ControlProfile


class AlignmentSearchMode(StrEnum):
    """Supported schedule-search policies."""

    EXHAUSTIVE_FINE = "exhaustive_fine"
    GLOBAL_REFINED = "global_refined"
    TRANSITION_SEEDED = "transition_seeded"


@dataclass(frozen=True, slots=True)
class AlignmentSearchConfig:
    """Numerical policy for one alignment search.

    ``GLOBAL_REFINED`` scans every fine cycle value across the configured
    range, but initially uses a coarse marker grid.  It then refines several
    distinct marker basins.  This avoids coupling a fine cycle search to the
    marker basin selected by one coarse cycle candidate.
    """

    cycle_range_ms: tuple[float, float]
    bin_duration_ms: float
    edge_exclusion_ms: float
    mode: AlignmentSearchMode = AlignmentSearchMode.GLOBAL_REFINED
    fine_cycle_step_ms: float | None = None
    fine_phase_step_ms: float | None = None
    coarse_phase_step_ms: float | None = None
    refinement_basin_count: int = 8
    transition_cycle_radius_ms: float | None = None
    transition_phase_radius_ms: float | None = None
    distinct_cycle_separation_ms: float | None = None
    distinct_phase_separation_ms: float | None = None
    minimum_complete_cycles: int = 4

    def __post_init__(self) -> None:
        low, high = self.cycle_range_ms
        if not np.isfinite(low) or not np.isfinite(high) or low <= 0.0 or high < low:
            raise ValueError("cycle_range_ms must be finite, positive, and ordered")
        if not np.isfinite(self.bin_duration_ms) or self.bin_duration_ms <= 0.0:
            raise ValueError("bin_duration_ms must be finite and positive")
        if not np.isfinite(self.edge_exclusion_ms) or self.edge_exclusion_ms < 0.0:
            raise ValueError("edge_exclusion_ms must be finite and non-negative")
        if isinstance(self.mode, str):
            object.__setattr__(self, "mode", AlignmentSearchMode(self.mode))
        for label, value in (
            ("fine_cycle_step_ms", self.fine_cycle_step_ms),
            ("fine_phase_step_ms", self.fine_phase_step_ms),
            ("coarse_phase_step_ms", self.coarse_phase_step_ms),
            ("distinct_cycle_separation_ms", self.distinct_cycle_separation_ms),
            ("distinct_phase_separation_ms", self.distinct_phase_separation_ms),
        ):
            if value is not None and (not np.isfinite(value) or value <= 0.0):
                raise ValueError(f"{label} must be finite and positive")
        for label, value in (
            ("transition_cycle_radius_ms", self.transition_cycle_radius_ms),
            ("transition_phase_radius_ms", self.transition_phase_radius_ms),
        ):
            if value is not None and (not np.isfinite(value) or value < 0.0):
                raise ValueError(f"{label} must be finite and non-negative")
        if self.refinement_basin_count < 1:
            raise ValueError("refinement_basin_count must be positive")
        if self.minimum_complete_cycles < 4:
            raise ValueError("minimum_complete_cycles must be at least four")

    @property
    def resolved_fine_cycle_step_ms(self) -> float:
        return (
            self.fine_cycle_step_ms
            if self.fine_cycle_step_ms is not None
            else max(0.1, self.bin_duration_ms / 5.0)
        )

    @property
    def resolved_fine_phase_step_ms(self) -> float:
        return (
            self.fine_phase_step_ms
            if self.fine_phase_step_ms is not None
            else max(0.1, self.bin_duration_ms / 5.0)
        )

    @property
    def resolved_coarse_phase_step_ms(self) -> float:
        return (
            self.coarse_phase_step_ms
            if self.coarse_phase_step_ms is not None
            else max(1.0, 2.0 * self.bin_duration_ms)
        )

    @property
    def resolved_distinct_cycle_separation_ms(self) -> float:
        return (
            self.distinct_cycle_separation_ms
            if self.distinct_cycle_separation_ms is not None
            else max(self.bin_duration_ms, 3.0 * self.resolved_fine_cycle_step_ms)
        )

    @property
    def resolved_distinct_phase_separation_ms(self) -> float:
        return (
            self.distinct_phase_separation_ms
            if self.distinct_phase_separation_ms is not None
            else max(
                self.resolved_coarse_phase_step_ms,
                4.0 * self.resolved_fine_phase_step_ms,
            )
        )


@dataclass(frozen=True, slots=True)
class AlignmentFitQuality:
    """Independent components and combined score for one candidate schedule."""

    explained_fraction: float
    residual_fraction: float
    residual_energy: float
    null_energy: float
    coherent_energy: float
    cycle_deviation_energy: float
    detection_ratio: float
    detection_strength: float
    even_odd_agreement: float
    cycle_coherence: float
    combined_score: float
    selected_bin_count: int


@dataclass(frozen=True, slots=True)
class AlignmentCandidate:
    """A fully evaluated cycle/marker hypothesis."""

    cycle_ms: float
    marker_phase_ms: float
    quality: AlignmentFitQuality
    complete_cycle_count: int

    @property
    def score(self) -> float:
        """Compatibility alias for the former private alignment result."""

        return self.quality.combined_score

    @property
    def even_odd_agreement(self) -> float:
        return self.quality.even_odd_agreement

    @property
    def cycle_coherence(self) -> float:
        return self.quality.cycle_coherence


@dataclass(frozen=True, slots=True)
class AlignmentSearchProvenance:
    """Deterministic provenance for the selected search result."""

    method_version: str
    mode: AlignmentSearchMode
    cycle_range_ms: tuple[float, float]
    fine_cycle_step_ms: float
    fine_phase_step_ms: float
    coarse_phase_step_ms: float
    candidate_count: int
    valid_candidate_count: int
    coarse_candidate_count: int
    fine_candidate_count: int
    refinement_basin_count: int
    transition_seed_used: bool


@dataclass(frozen=True, slots=True)
class DecodedTimingAgreement:
    """Difference between independent transition timing and the phase fit."""

    cycle_error_ms: float
    marker_error_ms: float
    cycle_tolerance_ms: float
    marker_tolerance_ms: float
    agrees: bool


@dataclass(frozen=True, slots=True)
class ScheduleAlignmentResult:
    """Selected schedule alignment, alternatives, diagnostics, and provenance."""

    selected: AlignmentCandidate
    distinct_runner_up: AlignmentCandidate | None
    score_margin: float | None
    provenance: AlignmentSearchProvenance
    decoded_timing_agreement: DecodedTimingAgreement | None

    @property
    def cycle_ms(self) -> float:
        return self.selected.cycle_ms

    @property
    def marker_phase_ms(self) -> float:
        return self.selected.marker_phase_ms

    @property
    def quality(self) -> AlignmentFitQuality:
        return self.selected.quality

    @property
    def score(self) -> float:
        """Compatibility alias for the former private alignment result."""

        return self.selected.score

    @property
    def even_odd_agreement(self) -> float:
        return self.selected.even_odd_agreement

    @property
    def cycle_coherence(self) -> float:
        return self.selected.cycle_coherence

    @property
    def complete_cycle_count(self) -> int:
        return self.selected.complete_cycle_count


@dataclass(frozen=True, slots=True)
class _CycleFit:
    deltas: npt.NDArray[np.complex128]
    explained_fraction: float
    residual_energy: float
    null_energy: float
    selected_bin_count: int


def _grid(low: float, high: float, step: float) -> npt.NDArray[np.float64]:
    count = max(1, int(np.floor((high - low) / step)) + 1)
    values = low + np.arange(count, dtype=np.float64) * step
    if values[-1] < high - step * 0.25:
        values = np.append(values, high)
    return values


def labels_and_interior(
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.bool_]]:
    """Assign bins to scaled profile states and exclude transition edges."""

    scale = cycle_ms / profile.nominal_cycle_ms
    all_off_index = len(profile.states)
    boundaries = [0.0]
    segment_labels = [all_off_index]

    cursor = (profile.marker_body_ms + profile.guard_ms) * scale
    for index, state in enumerate(profile.states):
        start = cursor
        end = start + state.dwell_ms * scale
        boundaries.extend((start, end))
        segment_labels.extend((index, all_off_index))
        cursor = end
        if index + 1 < len(profile.states):
            cursor += profile.guard_ms * scale
    boundaries[-1] = cycle_ms

    boundary_array = np.asarray(boundaries, dtype=np.float64)
    position = np.mod(times_ms - marker_phase_ms, cycle_ms)
    position[position >= cycle_ms] = 0.0
    segment_index = np.searchsorted(boundary_array, position, side="right") - 1
    label_lookup = np.asarray(segment_labels[:-1], dtype=np.int16)
    segment_index = np.clip(segment_index, 0, label_lookup.size - 1)
    labels = label_lookup[segment_index]
    distance_from_previous = position - boundary_array[segment_index]
    distance_to_next = boundary_array[segment_index + 1] - position
    interior = np.minimum(distance_from_previous, distance_to_next) >= edge_exclusion_ms
    return labels, interior


def complete_cycle_ids(
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Return every bin's cycle id and the ids wholly inside the capture."""

    raw_cycle_ids = np.floor((times_ms - marker_phase_ms) / cycle_ms).astype(np.int64)
    unique_ids = np.unique(raw_cycle_ids)
    starts = marker_phase_ms + unique_ids * cycle_ms
    tolerance = np.finfo(np.float64).eps * max(duration_ms, cycle_ms) * 8.0
    complete_ids = unique_ids[
        (starts >= -tolerance) & (starts + cycle_ms <= duration_ms + tolerance)
    ]
    return raw_cycle_ids, complete_ids


def _cycle_fit(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
    minimum_complete_cycles: int,
) -> _CycleFit | None:
    labels, interior = labels_and_interior(
        times_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    raw_cycle_ids, complete_ids = complete_cycle_ids(
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
    )
    if complete_ids.size < minimum_complete_cycles:
        return None

    selected = interior & reference_valid & np.isin(raw_cycle_ids, complete_ids)
    local_cycles = np.searchsorted(complete_ids, raw_cycle_ids[selected])
    selected_labels = labels[selected]
    selected_times = times_ms[selected]
    selected_transfer = transfer[selected]
    group_count = len(profile.states) + 1
    combined = local_cycles * group_count + selected_labels
    counts = np.bincount(combined, minlength=complete_ids.size * group_count).reshape(
        complete_ids.size, group_count
    )
    if np.any(counts < 3):
        return None

    all_off_index = len(profile.states)
    off = selected_labels == all_off_index
    off_cycles = local_cycles[off]
    off_times = selected_times[off]
    off_transfer = selected_transfer[off]
    off_counts = counts[:, all_off_index].astype(np.float64)
    sum_time = np.bincount(off_cycles, weights=off_times, minlength=complete_ids.size)
    sum_time_squared = np.bincount(off_cycles, weights=off_times**2, minlength=complete_ids.size)
    sum_transfer = np.bincount(
        off_cycles, weights=off_transfer.real, minlength=complete_ids.size
    ) + 1j * np.bincount(off_cycles, weights=off_transfer.imag, minlength=complete_ids.size)
    sum_time_transfer = np.bincount(
        off_cycles,
        weights=off_times * off_transfer.real,
        minlength=complete_ids.size,
    ) + 1j * np.bincount(
        off_cycles,
        weights=off_times * off_transfer.imag,
        minlength=complete_ids.size,
    )
    mean_time = sum_time / off_counts
    mean_transfer = sum_transfer / off_counts
    denominator = sum_time_squared - sum_time**2 / off_counts
    if np.any(denominator <= 0.0):
        return None
    slope = (sum_time_transfer - sum_time * sum_transfer / off_counts) / denominator
    baseline = mean_transfer[local_cycles] + slope[local_cycles] * (
        selected_times - mean_time[local_cycles]
    )
    cancelled = selected_transfer - baseline
    sums = np.bincount(
        combined, weights=cancelled.real, minlength=complete_ids.size * group_count
    ) + 1j * np.bincount(
        combined, weights=cancelled.imag, minlength=complete_ids.size * group_count
    )
    means = sums.reshape(complete_ids.size, group_count) / counts
    null_energy = float(np.sum(np.abs(cancelled) ** 2))
    fitted = means[local_cycles, selected_labels]
    residual_energy = float(np.sum(np.abs(cancelled - fitted) ** 2))
    explained_fraction = float(
        np.clip(
            1.0 - residual_energy / max(null_energy, np.finfo(np.float64).tiny),
            0.0,
            1.0,
        )
    )
    deltas = (means[:, :all_off_index] - means[:, all_off_index, None]).astype(np.complex128)
    return _CycleFit(
        deltas=deltas,
        explained_fraction=explained_fraction,
        residual_energy=residual_energy,
        null_energy=null_energy,
        selected_bin_count=int(np.count_nonzero(selected)),
    )


def _validated_inputs(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    duration_ms: float,
) -> tuple[
    npt.NDArray[np.complex128],
    npt.NDArray[np.bool_],
    npt.NDArray[np.float64],
]:
    transfer_array = np.asarray(transfer, dtype=np.complex128)
    valid_array = np.asarray(reference_valid, dtype=np.bool_)
    time_array = np.asarray(times_ms, dtype=np.float64)
    if transfer_array.ndim != 1 or valid_array.ndim != 1 or time_array.ndim != 1:
        raise ValueError("transfer, reference_valid, and times_ms must be one-dimensional")
    if not (transfer_array.size == valid_array.size == time_array.size):
        raise ValueError("transfer, reference_valid, and times_ms must have equal lengths")
    if transfer_array.size == 0:
        raise ValueError("alignment input cannot be empty")
    if not np.isfinite(duration_ms) or duration_ms <= 0.0:
        raise ValueError("duration_ms must be finite and positive")
    if not np.all(np.isfinite(time_array)) or np.any(np.diff(time_array) <= 0.0):
        raise ValueError("times_ms must be finite and strictly increasing")
    finite_transfer = np.isfinite(transfer_array.real) & np.isfinite(transfer_array.imag)
    valid_array = valid_array & finite_transfer
    return transfer_array, valid_array, time_array


def evaluate_schedule_alignment(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
    minimum_complete_cycles: int = 4,
) -> AlignmentCandidate | None:
    """Evaluate one fixed schedule hypothesis and expose every score component."""

    if minimum_complete_cycles < 4:
        raise ValueError("minimum_complete_cycles must be at least four")
    if not np.isfinite(cycle_ms) or cycle_ms <= 0.0:
        raise ValueError("cycle_ms must be finite and positive")
    if not np.isfinite(marker_phase_ms):
        raise ValueError("marker_phase_ms must be finite")
    if not np.isfinite(edge_exclusion_ms) or edge_exclusion_ms < 0.0:
        raise ValueError("edge_exclusion_ms must be finite and non-negative")
    transfer_array, valid_array, time_array = _validated_inputs(
        transfer, reference_valid, times_ms, duration_ms
    )
    normalized_phase = float(marker_phase_ms % cycle_ms)
    fit = _cycle_fit(
        transfer_array,
        valid_array,
        time_array,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=normalized_phase,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
        minimum_complete_cycles=minimum_complete_cycles,
    )
    if fit is None:
        return None

    deltas = fit.deltas
    even = np.mean(deltas[::2], axis=0)
    odd = np.mean(deltas[1::2], axis=0)
    norm_product = sqrt(float(np.vdot(even, even).real * np.vdot(odd, odd).real))
    if norm_product <= np.finfo(np.float64).tiny:
        agreement = 0.0
    else:
        agreement = float(np.clip(np.vdot(even, odd).real / norm_product, -1.0, 1.0))
    mean_delta = np.mean(deltas, axis=0)
    coherent_energy = float(np.sum(np.abs(mean_delta) ** 2))
    total_energy = float(np.sum(np.mean(np.abs(deltas) ** 2, axis=0)))
    coherence = coherent_energy / max(total_energy, np.finfo(np.float64).tiny)
    deviation_energy = float(np.sum(np.mean(np.abs(deltas - mean_delta) ** 2, axis=0)))
    detection_ratio = coherent_energy / max(
        deviation_energy / deltas.shape[0], np.finfo(np.float64).tiny
    )
    strength = 1.0 - exp(-detection_ratio / 8.0)
    score = max(agreement, 0.0) * coherence * strength * fit.explained_fraction
    residual_fraction = fit.residual_energy / max(fit.null_energy, np.finfo(np.float64).tiny)
    quality = AlignmentFitQuality(
        explained_fraction=fit.explained_fraction,
        residual_fraction=float(max(residual_fraction, 0.0)),
        residual_energy=fit.residual_energy,
        null_energy=fit.null_energy,
        coherent_energy=coherent_energy,
        cycle_deviation_energy=deviation_energy,
        detection_ratio=detection_ratio,
        detection_strength=float(np.clip(strength, 0.0, 1.0)),
        even_odd_agreement=agreement,
        cycle_coherence=float(np.clip(coherence, 0.0, 1.0)),
        combined_score=float(np.clip(score, 0.0, 1.0)),
        selected_bin_count=fit.selected_bin_count,
    )
    return AlignmentCandidate(
        cycle_ms=float(cycle_ms),
        marker_phase_ms=normalized_phase,
        quality=quality,
        complete_cycle_count=deltas.shape[0],
    )


def _candidate_key(candidate: AlignmentCandidate) -> tuple[float, float, float, float]:
    return (
        candidate.score,
        candidate.quality.explained_fraction,
        candidate.cycle_coherence,
        candidate.even_odd_agreement,
    )


def _circular_distance(first: float, second: float, period: float) -> float:
    separation = abs((first - second) % period)
    return min(separation, period - separation)


def _is_distinct_basin(
    first: AlignmentCandidate,
    second: AlignmentCandidate,
    *,
    cycle_separation_ms: float,
    phase_separation_ms: float,
) -> bool:
    cycle_distance = abs(first.cycle_ms - second.cycle_ms)
    common_period = 0.5 * (first.cycle_ms + second.cycle_ms)
    phase_distance = _circular_distance(
        first.marker_phase_ms % common_period,
        second.marker_phase_ms % common_period,
        common_period,
    )
    return cycle_distance > cycle_separation_ms or phase_distance > phase_separation_ms


def _distinct_best(
    candidates: list[AlignmentCandidate],
    *,
    count: int,
    cycle_separation_ms: float,
    phase_separation_ms: float,
) -> list[AlignmentCandidate]:
    retained: list[AlignmentCandidate] = []
    for candidate in sorted(candidates, key=_candidate_key, reverse=True):
        if all(
            _is_distinct_basin(
                candidate,
                other,
                cycle_separation_ms=cycle_separation_ms,
                phase_separation_ms=phase_separation_ms,
            )
            for other in retained
        ):
            retained.append(candidate)
            if len(retained) >= count:
                break
    return retained


def _decoded_agreement(
    selected: AlignmentCandidate,
    timing: DecodedScheduleTiming | None,
    config: AlignmentSearchConfig,
) -> DecodedTimingAgreement | None:
    if timing is None or timing.median_cycle_ms is None or timing.marker_phase_ms is None:
        return None
    cycle_error = abs(selected.cycle_ms - timing.median_cycle_ms)
    period = 0.5 * (selected.cycle_ms + timing.median_cycle_ms)
    marker_error = _circular_distance(
        selected.marker_phase_ms % period,
        timing.marker_phase_ms % period,
        period,
    )
    cycle_jitter_ms = timing.cycle_jitter_ms or 0.0
    marker_residuals = (
        tuple(
            _circular_distance(start_ms % period, timing.marker_phase_ms % period, period)
            for start_ms in timing.marker_start_times_ms
        )
        if timing.marker_start_times_ms
        else (0.0,)
    )
    marker_jitter_ms = float(np.median(np.asarray(marker_residuals, dtype=np.float64)))
    cycle_tolerance = max(
        config.bin_duration_ms + config.resolved_fine_cycle_step_ms,
        3.0 * cycle_jitter_ms,
    )
    marker_tolerance = max(
        config.bin_duration_ms + config.resolved_fine_phase_step_ms,
        3.0 * marker_jitter_ms,
    )
    tolerance_epsilon = np.finfo(np.float64).eps * max(period, 1.0) * 8.0
    return DecodedTimingAgreement(
        cycle_error_ms=float(cycle_error),
        marker_error_ms=float(marker_error),
        cycle_tolerance_ms=float(cycle_tolerance),
        marker_tolerance_ms=float(marker_tolerance),
        agrees=bool(
            cycle_error <= cycle_tolerance + tolerance_epsilon
            and marker_error <= marker_tolerance + tolerance_epsilon
        ),
    )


def search_schedule_alignment(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    profile: ControlProfile,
    config: AlignmentSearchConfig,
    decoded_timing: DecodedScheduleTiming | None = None,
) -> ScheduleAlignmentResult:
    """Search cycle and marker phase using the selected policy.

    ``EXHAUSTIVE_FINE`` is the oracle. ``GLOBAL_REFINED`` performs a global
    marker scan for every fine cycle value before refining distinct basins.
    ``TRANSITION_SEEDED`` requires a stable independent decoder result and
    searches only its uncertainty neighbourhood.
    """

    transfer_array, valid_array, time_array = _validated_inputs(
        transfer, reference_valid, times_ms, duration_ms
    )
    fine_cycle_step = config.resolved_fine_cycle_step_ms
    fine_phase_step = config.resolved_fine_phase_step_ms
    coarse_phase_step = config.resolved_coarse_phase_step_ms
    distinct_cycle = config.resolved_distinct_cycle_separation_ms
    distinct_phase = config.resolved_distinct_phase_separation_ms
    low, high = config.cycle_range_ms

    evaluated: dict[tuple[float, float], AlignmentCandidate | None] = {}
    coarse_count = 0
    fine_count = 0

    def evaluate(cycle_ms: float, marker_phase_ms: float, *, coarse: bool) -> None:
        nonlocal coarse_count, fine_count
        normalized_phase = float(marker_phase_ms % cycle_ms)
        key = (round(float(cycle_ms), 12), round(normalized_phase, 12))
        if key in evaluated:
            return
        candidate = evaluate_schedule_alignment(
            transfer_array,
            valid_array,
            time_array,
            duration_ms=duration_ms,
            cycle_ms=float(cycle_ms),
            marker_phase_ms=normalized_phase,
            edge_exclusion_ms=config.edge_exclusion_ms,
            profile=profile,
            minimum_complete_cycles=config.minimum_complete_cycles,
        )
        evaluated[key] = candidate
        if coarse:
            coarse_count += 1
        else:
            fine_count += 1

    if config.mode is AlignmentSearchMode.EXHAUSTIVE_FINE:
        for cycle_ms in _grid(low, high, fine_cycle_step):
            for marker_phase_ms in np.arange(0.0, cycle_ms, fine_phase_step):
                evaluate(float(cycle_ms), float(marker_phase_ms), coarse=False)
    elif config.mode is AlignmentSearchMode.GLOBAL_REFINED:
        for cycle_ms in _grid(low, high, fine_cycle_step):
            for marker_phase_ms in np.arange(0.0, cycle_ms, coarse_phase_step):
                evaluate(float(cycle_ms), float(marker_phase_ms), coarse=True)
        coarse_candidates = [candidate for candidate in evaluated.values() if candidate]
        basins = _distinct_best(
            coarse_candidates,
            count=config.refinement_basin_count,
            cycle_separation_ms=distinct_cycle,
            phase_separation_ms=distinct_phase,
        )
        for basin in basins:
            offsets = _grid(-coarse_phase_step, coarse_phase_step, fine_phase_step)
            for offset_ms in offsets:
                evaluate(
                    basin.cycle_ms,
                    basin.marker_phase_ms + float(offset_ms),
                    coarse=False,
                )
    else:
        if decoded_timing is None:
            raise ValueError("transition_seeded search requires decoded_timing")
        if decoded_timing.median_cycle_ms is None or decoded_timing.marker_phase_ms is None:
            raise ValueError("transition timing does not contain a cycle and marker estimate")
        if decoded_timing.rejected_marker_count:
            raise ValueError("transition_seeded search requires strict marker decoding")
        if decoded_timing.strict_frame_count < config.minimum_complete_cycles:
            raise ValueError("transition timing contains too few complete frames")
        cycle_jitter_ms = decoded_timing.cycle_jitter_ms or 0.0
        cycle_radius = max(
            config.transition_cycle_radius_ms
            if config.transition_cycle_radius_ms is not None
            else config.bin_duration_ms,
            3.0 * cycle_jitter_ms + fine_cycle_step,
        )
        phase_radius = max(
            config.transition_phase_radius_ms
            if config.transition_phase_radius_ms is not None
            else coarse_phase_step,
            fine_phase_step,
        )
        seed_low = max(low, decoded_timing.median_cycle_ms - cycle_radius)
        seed_high = min(high, decoded_timing.median_cycle_ms + cycle_radius)
        if seed_high < seed_low:
            raise ValueError("decoded cycle lies outside the configured cycle range")
        for cycle_ms in _grid(seed_low, seed_high, fine_cycle_step):
            for offset_ms in _grid(-phase_radius, phase_radius, fine_phase_step):
                evaluate(
                    float(cycle_ms),
                    decoded_timing.marker_phase_ms + float(offset_ms),
                    coarse=False,
                )

    candidates = [candidate for candidate in evaluated.values() if candidate is not None]
    if not candidates:
        raise ValueError(
            f"capture does not contain {config.minimum_complete_cycles} complete candidate cycles"
        )
    selected = max(candidates, key=_candidate_key)
    alternatives = _distinct_best(
        candidates,
        count=2,
        cycle_separation_ms=distinct_cycle,
        phase_separation_ms=distinct_phase,
    )
    runner_up = next((candidate for candidate in alternatives if candidate != selected), None)
    score_margin = None if runner_up is None else selected.score - runner_up.score
    provenance = AlignmentSearchProvenance(
        method_version="schedule_alignment_v1",
        mode=config.mode,
        cycle_range_ms=config.cycle_range_ms,
        fine_cycle_step_ms=fine_cycle_step,
        fine_phase_step_ms=fine_phase_step,
        coarse_phase_step_ms=coarse_phase_step,
        candidate_count=len(evaluated),
        valid_candidate_count=len(candidates),
        coarse_candidate_count=coarse_count,
        fine_candidate_count=fine_count,
        refinement_basin_count=config.refinement_basin_count,
        transition_seed_used=config.mode is AlignmentSearchMode.TRANSITION_SEEDED,
    )
    return ScheduleAlignmentResult(
        selected=selected,
        distinct_runner_up=runner_up,
        score_margin=None if score_margin is None else float(score_margin),
        provenance=provenance,
        decoded_timing_agreement=_decoded_agreement(selected, decoded_timing, config),
    )


def search_phase_alignment(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_range_ms: tuple[float, float],
    bin_duration_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
    mode: AlignmentSearchMode = AlignmentSearchMode.GLOBAL_REFINED,
    decoded_timing: DecodedScheduleTiming | None = None,
) -> ScheduleAlignmentResult:
    """Convenience wrapper matching the former private search call shape."""

    return search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        profile=profile,
        config=AlignmentSearchConfig(
            cycle_range_ms=cycle_range_ms,
            bin_duration_ms=bin_duration_ms,
            edge_exclusion_ms=edge_exclusion_ms,
            mode=mode,
        ),
        decoded_timing=decoded_timing,
    )
