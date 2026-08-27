"""RF-only, microsecond timing qualification for the ``hexcal-v1`` selector.

This module is intentionally hardware-free.  It coherently reduces a 2 MS/s
RX2 stream to one complex value per microsecond, finds RF-visible transitions,
and measures the selector schedule from those transitions.  Expected slot
positions are used only to recognize the source-backed frame grammar; accepted
edge times come from local RF change estimates and are never snapped to the
nominal schedule.

The resulting evidence is relative to the Pluto sample clock.  RF cannot prove
the GPIO code, selected physical connector, or 180 us marker-body/20 us
pre-ANT1 split: it sees their combined approximately 200 us ALL_OFF plateau.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.hexcal import HexcalAnalysisError, HexcalProfile

SAMPLE_RATE_HZ = 2_000_000
BANDWIDTH_HZ = 1_600_000
SAMPLES_PER_COHERENT_BIN = 2
TIMING_RECEIVER_GAIN_DB = 30
BIN_DURATION_US = 1.0
NOMINAL_CYCLE_US = 1_500.0
MINIMUM_COMPLETE_CYCLES = 290
MINIMUM_DECODE_FRACTION = 0.98
MINIMUM_TRANSITION_SNR_DB = 19.0
MINIMUM_PILOT_SNR_DB = 17.0
MINIMUM_STATE_NULL_CONTRAST_DB = 17.0
EDGE_MEAN_HALF_WINDOW_US = 4
EDGE_PROJECTION_PLATEAU_US = 7
EDGE_SEARCH_RADIUS_US = 4
EDGE_THRESHOLD_SIGMA = 8.0
MAXIMUM_THRESHOLD_SWEEP_US = 1.5
MAXIMUM_INDEPENDENT_ESTIMATOR_DELTA_US = 1.5
MAXIMUM_REFINED_PILOT_RESIDUAL_HZ = 2_000.0
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.95
THRESHOLD_SWEEP = (0.4, 0.5, 0.6)
LONG_INTERVAL_WINDOW_US = (150.0, 250.0)
SHORT_INTERVAL_WINDOW_US = (8.0, 40.0)
_INTERVAL_PATTERN = (
    "long",  # combined marker / pre-ANT1 guard
    "long",  # ANT1
    "short",
    "long",  # ANT2
    "short",
    "long",  # ANT3
    "short",
    "long",  # ANT4
    "short",
    "long",  # ANT5
    "short",
    "long",  # ANT6
)


@dataclass(frozen=True, slots=True)
class TransitionCandidate:
    """One independently detected local complex change."""

    bin_index: int
    strength: float
    threshold_run_width: int


@dataclass(frozen=True, slots=True)
class TransitionEstimate:
    """Fractional crossing and independent changepoint estimates for one edge."""

    candidate: TransitionCandidate
    q40_us: float
    q50_us: float
    q60_us: float
    changepoint_us: float
    transition_snr_db: float
    threshold_span_us: float
    independent_delta_us: float

    @property
    def all_times_us(self) -> tuple[float, float, float, float]:
        return self.q40_us, self.q50_us, self.q60_us, self.changepoint_us

    def as_dict(self) -> dict[str, float | int]:
        return {
            "candidate_bin": self.candidate.bin_index,
            "candidate_strength": self.candidate.strength,
            "threshold_run_width_bins": self.candidate.threshold_run_width,
            "q40_us": self.q40_us,
            "q50_us": self.q50_us,
            "q60_us": self.q60_us,
            "independent_two_mean_changepoint_us": self.changepoint_us,
            "transition_snr_db": self.transition_snr_db,
            "threshold_span_us": self.threshold_span_us,
            "independent_estimator_delta_us": self.independent_delta_us,
        }


def _finite_complex_vector(values: npt.ArrayLike, label: str) -> npt.NDArray[np.complex128]:
    array = np.asarray(values)
    if array.ndim != 1 or not np.iscomplexobj(array) or array.size < 1:
        raise ValueError(f"{label} must be a nonempty one-dimensional complex vector")
    if not np.all(np.isfinite(array.real)) or not np.all(np.isfinite(array.imag)):
        raise ValueError(f"{label} must contain only finite samples")
    return np.asarray(array, dtype=np.complex128)


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)], dtype=float)
    if not finite.size:
        return {
            "count": 0,
            "minimum": None,
            "p01": None,
            "median": None,
            "mean": None,
            "p99": None,
            "maximum": None,
            "std": None,
        }
    return {
        "count": int(finite.size),
        "minimum": float(np.min(finite)),
        "p01": float(np.percentile(finite, 1.0)),
        "median": float(np.median(finite)),
        "mean": float(np.mean(finite)),
        "p99": float(np.percentile(finite, 99.0)),
        "maximum": float(np.max(finite)),
        "std": float(np.std(finite)),
    }


def _required_stat(document: Mapping[str, float | int | None], field: str, label: str) -> float:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HexcalAnalysisError(f"{label} has no finite {field}")
    number = float(value)
    if not math.isfinite(number):
        raise HexcalAnalysisError(f"{label} has a non-finite {field}")
    return number


def _complex_mean(values: npt.NDArray[np.complex128]) -> complex:
    if not values.size:
        raise HexcalAnalysisError("timing plateau has no samples")
    return complex(np.mean(values))


def _complex_noise(values: npt.NDArray[np.complex128], center: complex) -> float:
    if values.size < 2:
        return float("inf")
    residual = np.abs(values - center)
    # The median absolute complex residual is robust to occasional RF settling.
    return max(float(np.median(residual)) / math.sqrt(math.log(2.0)), 1e-15)


def _ratio_db(numerator: float, denominator: float) -> float:
    return 20.0 * math.log10(max(numerator, 1e-15) / max(denominator, 1e-15))


def coherent_one_microsecond_detector(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
) -> npt.NDArray[np.complex128]:
    """Mix and average two consecutive 2 MS/s samples into 1 us complex bins."""

    raw = _finite_complex_vector(samples, "RX2 samples")
    if sample_rate_hz != SAMPLE_RATE_HZ:
        raise ValueError("hexcal RF timing requires exactly 2 MS/s")
    if not math.isfinite(tone_offset_hz) or abs(tone_offset_hz) >= sample_rate_hz / 2.0:
        raise ValueError("tone offset must be finite and strictly inside Nyquist")
    if raw.size % SAMPLES_PER_COHERENT_BIN:
        raise ValueError("RX2 sample count is not divisible into exact 1 us bins")
    output = np.empty(raw.size // SAMPLES_PER_COHERENT_BIN, dtype=np.complex128)
    chunk_bins = 50_000
    for first_bin in range(0, output.size, chunk_bins):
        last_bin = min(output.size, first_bin + chunk_bins)
        first_sample = first_bin * SAMPLES_PER_COHERENT_BIN
        last_sample = last_bin * SAMPLES_PER_COHERENT_BIN
        indices = np.arange(first_sample, last_sample, dtype=np.float64)
        mixed = raw[first_sample:last_sample] * np.exp(
            -2j * np.pi * tone_offset_hz / sample_rate_hz * indices
        )
        output[first_bin:last_bin] = mixed.reshape(-1, SAMPLES_PER_COHERENT_BIN).mean(axis=1)
    return output


def _edge_candidates(
    envelope: npt.NDArray[np.complex128],
) -> tuple[list[TransitionCandidate], dict[str, float | int]]:
    half = EDGE_MEAN_HALF_WINDOW_US
    if envelope.size < 2 * half + 1:
        raise HexcalAnalysisError("timing envelope is too short for edge detection")
    cumulative = np.concatenate((np.zeros(1, dtype=np.complex128), np.cumsum(envelope)))
    indices = np.arange(half, envelope.size - half + 1, dtype=np.int64)
    before = (cumulative[indices] - cumulative[indices - half]) / half
    after = (cumulative[indices + half] - cumulative[indices]) / half
    local_strength = np.abs(after - before)
    strength = np.zeros(envelope.size + 1, dtype=np.float64)
    strength[indices] = local_strength

    differences = np.diff(envelope)
    difference_center = complex(np.median(differences.real), np.median(differences.imag))
    difference_residual = np.abs(differences - difference_center)
    noise_per_complex_sample = max(
        float(np.median(difference_residual)) / math.sqrt(2.0 * math.log(2.0)),
        np.finfo(float).tiny,
    )
    expected_mean_difference_noise = noise_per_complex_sample * math.sqrt(2.0 / half)
    ordinary = local_strength
    ordinary_median = float(np.median(ordinary))
    ordinary_mad = 1.4826 * float(np.median(np.abs(ordinary - ordinary_median)))
    threshold = max(
        EDGE_THRESHOLD_SIGMA * expected_mean_difference_noise,
        ordinary_median + 6.0 * ordinary_mad,
        np.finfo(float).tiny,
    )
    above = strength > threshold
    transitions = np.flatnonzero(np.diff(np.pad(above.astype(np.int8), (1, 1))))
    candidates: list[TransitionCandidate] = []
    oversized_runs = 0
    for start, stop in transitions.reshape(-1, 2):
        if start < half or stop > envelope.size - half + 1:
            continue
        width = int(stop - start)
        selected = int(start + np.argmax(strength[start:stop]))
        if width > 2 * half + 4:
            oversized_runs += 1
        candidates.append(
            TransitionCandidate(
                bin_index=selected,
                strength=float(strength[selected]),
                threshold_run_width=width,
            )
        )
    if len(candidates) < 13:
        raise HexcalAnalysisError("fewer than thirteen RF transitions are visible")
    return candidates, {
        "candidate_count": len(candidates),
        "edge_strength_threshold": threshold,
        "robust_complex_noise_per_1us_bin": noise_per_complex_sample,
        "oversized_threshold_run_count": oversized_runs,
        "local_two_mean_half_window_us": half,
    }


def _interval_class(duration_us: float) -> str | None:
    if LONG_INTERVAL_WINDOW_US[0] <= duration_us <= LONG_INTERVAL_WINDOW_US[1]:
        return "long"
    if SHORT_INTERVAL_WINDOW_US[0] <= duration_us <= SHORT_INTERVAL_WINDOW_US[1]:
        return "short"
    return None


def _frame_proposals(
    candidates: Sequence[TransitionCandidate],
) -> list[tuple[int, ...]]:
    proposals: list[tuple[int, ...]] = []
    for first in range(0, len(candidates) - len(_INTERVAL_PATTERN)):
        selected = tuple(range(first, first + len(_INTERVAL_PATTERN) + 1))
        durations = [
            candidates[right].bin_index - candidates[left].bin_index
            for left, right in zip(selected, selected[1:], strict=False)
        ]
        classes = tuple(_interval_class(float(value)) for value in durations)
        if classes == _INTERVAL_PATTERN:
            proposals.append(selected)
    if not proposals:
        raise HexcalAnalysisError(
            "no repeated long-marker/six-dwell RF grammar is visible without snapping"
        )
    starts = [candidates[item[0]].bin_index for item in proposals]
    for left, right in zip(starts, starts[1:], strict=False):
        if right - left < 0.65 * NOMINAL_CYCLE_US:
            raise HexcalAnalysisError("ambiguous overlapping RF marker interpretations")
    return proposals


def _crossing_time_us(
    projection: npt.NDArray[np.float64],
    first_bin: int,
    q: float,
) -> float:
    matches = np.flatnonzero((projection[:-1] < q) & (projection[1:] >= q))
    if matches.size != 1:
        raise HexcalAnalysisError(f"RF edge has {matches.size} directional q={q:.1f} crossings")
    local = int(matches[0])
    low = float(projection[local])
    high = float(projection[local + 1])
    if not high > low:
        raise HexcalAnalysisError("RF edge projection does not increase at its crossing")
    fraction = (q - low) / (high - low)
    # Each envelope value describes a [n,n+1) us bin and is located at n+0.5 us.
    return float(first_bin + local + 0.5 + fraction)


def _transition_estimate(
    envelope: npt.NDArray[np.complex128], candidate: TransitionCandidate
) -> TransitionEstimate:
    if candidate.threshold_run_width > 2 * EDGE_MEAN_HALF_WINDOW_US + 4:
        raise HexcalAnalysisError("RF edge response is too wide for an unambiguous transition")
    index = candidate.bin_index
    plateau = EDGE_PROJECTION_PLATEAU_US
    search = EDGE_SEARCH_RADIUS_US
    if index - plateau - 3 < 0 or index + plateau + 3 >= envelope.size:
        raise HexcalAnalysisError("RF edge is too close to a capture boundary")
    before_values = envelope[index - plateau - 2 : index - 2]
    after_values = envelope[index + 2 : index + plateau + 2]
    before = _complex_mean(before_values)
    after = _complex_mean(after_values)
    delta = after - before
    delta_power = abs(delta) ** 2
    before_noise = _complex_noise(before_values, before)
    after_noise = _complex_noise(after_values, after)
    # This is a per-bin edge SNR.  Propagate both independent plateau noises;
    # averaging their variances would overstate SNR by 3 dB for equal noise.
    transition_noise = math.sqrt(before_noise**2 + after_noise**2)
    if delta_power <= np.finfo(float).tiny:
        raise HexcalAnalysisError("RF edge has no finite complex plateau contrast")
    first_bin = index - search
    last_bin = index + search + 1
    local_values = envelope[first_bin:last_bin]
    projection = np.asarray(
        np.real((local_values - before) * np.conj(delta)) / delta_power,
        dtype=np.float64,
    )
    crossing_times = {q: _crossing_time_us(projection, first_bin, q) for q in THRESHOLD_SWEEP}

    cp_first = index - plateau
    cp_last = index + plateau
    segment = envelope[cp_first:cp_last]
    changepoints: list[tuple[float, int]] = []
    for split in range(3, segment.size - 2):
        left = segment[:split]
        right = segment[split:]
        left_mean = np.mean(left)
        right_mean = np.mean(right)
        loss = float(
            np.sum(np.abs(left - left_mean) ** 2) + np.sum(np.abs(right - right_mean) ** 2)
        )
        changepoints.append((loss, split))
    changepoints.sort()
    if len(changepoints) < 2:
        raise HexcalAnalysisError("RF edge has no independent changepoint search")
    best_loss, best_split = changepoints[0]
    second_loss = changepoints[1][0]
    if second_loss <= best_loss * (1.0 + 1e-9):
        raise HexcalAnalysisError("RF edge has an ambiguous two-mean changepoint")
    changepoint_us = float(cp_first + best_split)
    q40 = crossing_times[0.4]
    q50 = crossing_times[0.5]
    q60 = crossing_times[0.6]
    return TransitionEstimate(
        candidate=candidate,
        q40_us=q40,
        q50_us=q50,
        q60_us=q60,
        changepoint_us=changepoint_us,
        transition_snr_db=_ratio_db(abs(delta), transition_noise),
        threshold_span_us=max(q40, q50, q60) - min(q40, q50, q60),
        independent_delta_us=abs(changepoint_us - q50),
    )


def _pilot_refinement(
    envelope: npt.NDArray[np.complex128],
    candidates: Sequence[TransitionCandidate],
    proposals: Sequence[tuple[int, ...]],
) -> dict[str, float | int]:
    cross_sum = 0.0j
    cross_abs_sum = 0.0
    phase_steps: list[float] = []
    pair_count = 0
    for proposal in proposals[: min(len(proposals), 300)]:
        boundaries = [candidates[index].bin_index for index in proposal]
        for state in range(6):
            start = boundaries[2 * state + 1] + 10
            stop = boundaries[2 * state + 2] - 10
            values = envelope[start:stop]
            if values.size < 20:
                continue
            products = values[1:] * np.conj(values[:-1])
            weights = np.abs(products)
            valid = weights > np.percentile(weights, 10.0)
            selected = products[valid]
            if not selected.size:
                continue
            cross_sum += complex(np.sum(selected))
            cross_abs_sum += float(np.sum(np.abs(selected)))
            phase_steps.extend(np.angle(selected).tolist())
            pair_count += int(selected.size)
    if pair_count < 10_000 or cross_abs_sum <= np.finfo(float).tiny:
        raise HexcalAnalysisError("insufficient active-plateau samples to refine the pilot")
    mean_step = math.atan2(cross_sum.imag, cross_sum.real)
    residual_hz = mean_step / (2.0 * math.pi * 1e-6)
    coherence = min(1.0, abs(cross_sum) / cross_abs_sum)
    centered_steps = np.angle(np.exp(1j * (np.asarray(phase_steps) - mean_step)))
    standard_error_hz = float(np.std(centered_steps)) / (
        2.0 * math.pi * 1e-6 * math.sqrt(pair_count)
    )
    if abs(residual_hz) > MAXIMUM_REFINED_PILOT_RESIDUAL_HZ:
        raise HexcalAnalysisError("refined pilot differs from DDS readback by over 2 kHz")
    if coherence < MINIMUM_PILOT_PHASE_STEP_COHERENCE:
        raise HexcalAnalysisError("refined pilot phase-step coherence is below 0.95")
    return {
        "residual_from_dds_readback_hz": residual_hz,
        "fit_standard_error_hz": standard_error_hz,
        "phase_step_coherence": coherence,
        "used_adjacent_plateau_pairs": pair_count,
    }


def _correct_residual(
    envelope: npt.NDArray[np.complex128], residual_hz: float
) -> npt.NDArray[np.complex128]:
    times_s = (np.arange(envelope.size, dtype=np.float64) + 0.5) * 1e-6
    return np.asarray(
        envelope * np.exp(-2j * np.pi * residual_hz * times_s),
        dtype=np.complex128,
    )


def _segment_bounds(
    first: TransitionEstimate, second: TransitionEstimate
) -> tuple[float, float, float]:
    duration = second.q50_us - first.q50_us
    lower = min(second.all_times_us) - max(first.all_times_us)
    upper = max(second.all_times_us) - min(first.all_times_us)
    return duration, lower, upper


def _slice_by_time(
    envelope: npt.NDArray[np.complex128], start_us: float, stop_us: float
) -> npt.NDArray[np.complex128]:
    first = max(0, int(math.ceil(start_us - 0.5)))
    last = min(envelope.size, int(math.floor(stop_us - 0.5)) + 1)
    if last <= first:
        raise HexcalAnalysisError("RF plateau interval is empty after edge exclusion")
    return envelope[first:last]


def _slice_by_time_with_center(
    envelope: npt.NDArray[np.complex128], start_us: float, stop_us: float
) -> tuple[npt.NDArray[np.complex128], float]:
    values = _slice_by_time(envelope, start_us, stop_us)
    first = max(0, int(math.ceil(start_us - 0.5)))
    last = first + values.size
    # Complex 1 us bins are centered at n+0.5 us.
    return values, (first + last) / 2.0


def _interpolated_state_metrics(
    active_values: npt.NDArray[np.complex128],
    before_values: npt.NDArray[np.complex128],
    after_values: npt.NDArray[np.complex128],
    *,
    active_center_us: float,
    before_center_us: float,
    after_center_us: float,
) -> dict[str, float]:
    """Propagate per-bin noise through time-centered two-sided null interpolation."""

    if not before_center_us < active_center_us < after_center_us:
        raise HexcalAnalysisError("null windows do not bracket the active plateau center")
    span = after_center_us - before_center_us
    after_weight = (active_center_us - before_center_us) / span
    before_weight = 1.0 - after_weight
    active = _complex_mean(active_values)
    before = _complex_mean(before_values)
    after = _complex_mean(after_values)
    null = before_weight * before + after_weight * after
    signal = active - null
    active_noise = _complex_noise(active_values, active)
    before_noise = _complex_noise(before_values, before)
    after_noise = _complex_noise(after_values, after)
    propagated_noise = math.sqrt(
        active_noise**2 + before_weight**2 * before_noise**2 + after_weight**2 * after_noise**2
    )
    return {
        "pilot_snr_db": _ratio_db(abs(signal), propagated_noise),
        "state_to_null_contrast_db": _ratio_db(abs(signal), max(abs(null), propagated_noise)),
        "signal_amplitude": abs(signal),
        "interpolated_null_amplitude": abs(null),
        "propagated_per_bin_complex_noise": propagated_noise,
        "active_per_bin_complex_noise": active_noise,
        "before_null_per_bin_complex_noise": before_noise,
        "after_null_per_bin_complex_noise": after_noise,
        "before_null_weight": before_weight,
        "after_null_weight": after_weight,
        "active_center_us": active_center_us,
        "before_null_center_us": before_center_us,
        "after_null_center_us": after_center_us,
    }


def _state_rf_measurements(
    envelope: npt.NDArray[np.complex128], edges: Sequence[TransitionEstimate]
) -> list[dict[str, float | str]]:
    documents: list[dict[str, float | str]] = []
    for state in range(6):
        start_index = 2 * state + 1
        stop_index = 2 * state + 2
        active_start = edges[start_index].q50_us
        active_stop = edges[stop_index].q50_us
        active_values, active_center = _slice_by_time_with_center(
            envelope, active_start + 10.0, active_stop - 10.0
        )
        if state == 0:
            before_values, before_center = _slice_by_time_with_center(
                envelope, edges[0].q50_us + 10.0, active_start - 5.0
            )
        else:
            before_values, before_center = _slice_by_time_with_center(
                envelope, edges[start_index - 1].q50_us + 3.0, active_start - 3.0
            )
        if state == 5:
            after_values, after_center = _slice_by_time_with_center(
                envelope, active_stop + 5.0, active_stop + 25.0
            )
        else:
            after_values, after_center = _slice_by_time_with_center(
                envelope, active_stop + 3.0, edges[stop_index + 1].q50_us - 3.0
            )
        metrics = _interpolated_state_metrics(
            active_values,
            before_values,
            after_values,
            active_center_us=active_center,
            before_center_us=before_center,
            after_center_us=after_center,
        )
        documents.append(
            {
                "name": f"ANT{state + 1}",
                **metrics,
            }
        )
    return documents


def _quality_document(
    *,
    complete_cycles: int,
    decoded_fraction: float,
    marker: Mapping[str, float | int | None],
    dwells: Mapping[str, Mapping[str, float | int | None]],
    guards: Mapping[str, Mapping[str, Any]],
    cycle: Mapping[str, float | int | None],
    maximum_threshold_span_us: float,
    maximum_independent_delta_us: float,
    minimum_transition_snr_db: float,
    minimum_pilot_snr_db: float,
    minimum_contrast_db: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    if complete_cycles < MINIMUM_COMPLETE_CYCLES:
        reasons.append("fewer_than_290_complete_cycles")
    if decoded_fraction < MINIMUM_DECODE_FRACTION:
        reasons.append("decoded_cycle_fraction_below_98_percent")
    marker_min = _required_stat(marker, "minimum", "marker timing")
    marker_max = _required_stat(marker, "maximum", "marker timing")
    if marker_min < 190.0 or marker_max > 210.0:
        reasons.append("combined_rf_marker_outside_190_210_us")
    for name, stats in dwells.items():
        minimum = _required_stat(stats, "minimum", f"{name} dwell")
        maximum = _required_stat(stats, "maximum", f"{name} dwell")
        if minimum < 190.0 or maximum > 210.0:
            reasons.append(f"{name.lower()}_dwell_outside_190_210_us")
    guard_lower_bounds: list[float] = []
    guard_upper_bounds: list[float] = []
    for name, document in guards.items():
        stats_raw = document.get("q50_us")
        if not isinstance(stats_raw, Mapping):
            raise HexcalAnalysisError(f"{name} guard statistics are malformed")
        median = _required_stat(stats_raw, "median", f"{name} guard")
        lower = document.get("conservative_minimum_lower_bound_us")
        upper = document.get("conservative_maximum_upper_bound_us")
        if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
            raise HexcalAnalysisError(f"{name} guard uncertainty bounds are malformed")
        guard_lower_bounds.append(float(lower))
        guard_upper_bounds.append(float(upper))
        if not 19.0 <= median <= 21.0:
            reasons.append(f"{name.lower()}_aggregate_guard_outside_19_21_us")
    if min(guard_lower_bounds) < 18.0:
        reasons.append("conservative_guard_lower_bound_below_18_us")
    if max(guard_upper_bounds) > 22.0:
        reasons.append("conservative_guard_upper_bound_above_22_us")
    cycle_min = _required_stat(cycle, "minimum", "cycle timing")
    cycle_max = _required_stat(cycle, "maximum", "cycle timing")
    if cycle_min < 1_425.0 or cycle_max > 1_575.0:
        reasons.append("cycle_outside_1425_1575_us")
    if maximum_threshold_span_us > MAXIMUM_THRESHOLD_SWEEP_US:
        reasons.append("q40_q60_edge_span_above_1_5_us")
    if maximum_independent_delta_us > MAXIMUM_INDEPENDENT_ESTIMATOR_DELTA_US:
        reasons.append("independent_edge_estimator_delta_above_1_5_us")
    if minimum_transition_snr_db < MINIMUM_TRANSITION_SNR_DB:
        reasons.append("transition_snr_below_19_db")
    if minimum_pilot_snr_db < MINIMUM_PILOT_SNR_DB:
        reasons.append("pilot_snr_below_17_db")
    if minimum_contrast_db < MINIMUM_STATE_NULL_CONTRAST_DB:
        reasons.append("state_null_contrast_below_17_db")
    return {
        "passed": not reasons,
        "rejection_reasons": reasons,
        "frozen_gates": {
            "minimum_complete_cycles": MINIMUM_COMPLETE_CYCLES,
            "minimum_decode_fraction": MINIMUM_DECODE_FRACTION,
            "visible_edges_per_cycle": 12,
            "combined_marker_window_us": [190.0, 210.0],
            "each_dwell_window_us": [190.0, 210.0],
            "each_ordinary_guard_aggregate_window_us": [19.0, 21.0],
            "conservative_guard_lower_bound_us": 18.0,
            "conservative_guard_upper_bound_us": 22.0,
            "cycle_window_us": [1_425.0, 1_575.0],
            "maximum_q40_q60_edge_span_us": MAXIMUM_THRESHOLD_SWEEP_US,
            "maximum_independent_estimator_delta_us": (MAXIMUM_INDEPENDENT_ESTIMATOR_DELTA_US),
            "maximum_refined_pilot_residual_from_dds_readback_hz": (
                MAXIMUM_REFINED_PILOT_RESIDUAL_HZ
            ),
            "minimum_pilot_phase_step_coherence": (MINIMUM_PILOT_PHASE_STEP_COHERENCE),
            "minimum_transition_snr_db": MINIMUM_TRANSITION_SNR_DB,
            "minimum_pilot_snr_db": MINIMUM_PILOT_SNR_DB,
            "minimum_state_null_contrast_db": MINIMUM_STATE_NULL_CONTRAST_DB,
        },
    }


def analyze_hexcal_timing_envelope(
    envelope: npt.ArrayLike,
    *,
    dds_readback_hz: float,
    profile: HexcalProfile,
) -> dict[str, Any]:
    """Decode and directly measure one already-demodulated 1 us RX2 envelope."""

    values = _finite_complex_vector(envelope, "1 us RX2 envelope")
    if profile.cycle_us != 1_500 or profile.guard_us != 20:
        raise ValueError("timing analysis requires the exact hexcal-v1 schedule")
    preliminary_candidates, _ = _edge_candidates(values)
    preliminary_proposals = _frame_proposals(preliminary_candidates)
    pilot = _pilot_refinement(values, preliminary_candidates, preliminary_proposals)
    residual_hz = float(pilot["residual_from_dds_readback_hz"])
    corrected = _correct_residual(values, residual_hz)
    candidates, detector = _edge_candidates(corrected)
    proposals = _frame_proposals(candidates)

    edge_cache: dict[int, TransitionEstimate] = {}
    cycles: list[dict[str, Any]] = []
    rejected_edge_estimation = 0
    state_rf: dict[str, list[dict[str, float | str]]] = {f"ANT{index}": [] for index in range(1, 7)}
    for proposal in proposals:
        try:
            edges = []
            for candidate_index in proposal:
                if candidate_index not in edge_cache:
                    edge_cache[candidate_index] = _transition_estimate(
                        corrected, candidates[candidate_index]
                    )
                edges.append(edge_cache[candidate_index])
            q50 = [edge.q50_us for edge in edges]
            marker_us, marker_lower, marker_upper = _segment_bounds(edges[0], edges[1])
            dwell_documents: dict[str, dict[str, float]] = {}
            guard_documents: dict[str, dict[str, float]] = {}
            for state in range(6):
                start = 2 * state + 1
                duration, lower, upper = _segment_bounds(edges[start], edges[start + 1])
                dwell_documents[f"ANT{state + 1}"] = {
                    "q50_us": duration,
                    "conservative_lower_bound_us": lower,
                    "conservative_upper_bound_us": upper,
                }
                if state < 5:
                    guard_duration, guard_lower, guard_upper = _segment_bounds(
                        edges[start + 1], edges[start + 2]
                    )
                    guard_documents[f"ANT{state + 1}_TO_ANT{state + 2}"] = {
                        "q50_us": guard_duration,
                        "conservative_lower_bound_us": guard_lower,
                        "conservative_upper_bound_us": guard_upper,
                    }
            cycle_us, cycle_lower, cycle_upper = _segment_bounds(edges[0], edges[-1])
            rf_documents = _state_rf_measurements(corrected, edges)
            for document in rf_documents:
                state_rf[str(document["name"])].append(document)
            cycles.append(
                {
                    "marker_start_us": q50[0],
                    "visible_edge_count": 12,
                    "edge_estimates": [edge.as_dict() for edge in edges],
                    "combined_rf_marker": {
                        "q50_us": marker_us,
                        "conservative_lower_bound_us": marker_lower,
                        "conservative_upper_bound_us": marker_upper,
                    },
                    "dwells": dwell_documents,
                    "ordinary_guards": guard_documents,
                    "cycle": {
                        "q50_us": cycle_us,
                        "conservative_lower_bound_us": cycle_lower,
                        "conservative_upper_bound_us": cycle_upper,
                    },
                    "rf": rf_documents,
                }
            )
        except HexcalAnalysisError:
            rejected_edge_estimation += 1
    if len(cycles) < 3:
        raise HexcalAnalysisError("fewer than three unambiguous RF cycles remain")

    cycle_values = [float(item["cycle"]["q50_us"]) for item in cycles]
    measured_period = float(np.median(cycle_values))
    conservative_possible_cycles = max(1, math.floor(values.size / measured_period) - 1)
    decoded_fraction = min(1.0, len(cycles) / conservative_possible_cycles)
    marker_values = [float(item["combined_rf_marker"]["q50_us"]) for item in cycles]
    dwell_stats: dict[str, dict[str, float | int | None]] = {}
    for state in range(1, 7):
        name = f"ANT{state}"
        dwell_stats[name] = _stats([float(item["dwells"][name]["q50_us"]) for item in cycles])
    guard_stats: dict[str, dict[str, Any]] = {}
    for state in range(1, 6):
        name = f"ANT{state}_TO_ANT{state + 1}"
        q50_values = [float(item["ordinary_guards"][name]["q50_us"]) for item in cycles]
        lower_values = [
            float(item["ordinary_guards"][name]["conservative_lower_bound_us"]) for item in cycles
        ]
        upper_values = [
            float(item["ordinary_guards"][name]["conservative_upper_bound_us"]) for item in cycles
        ]
        guard_stats[name] = {
            "q50_us": _stats(q50_values),
            "conservative_lower_bound_us": _stats(lower_values),
            "conservative_upper_bound_us": _stats(upper_values),
            "conservative_minimum_lower_bound_us": min(lower_values),
            "conservative_maximum_upper_bound_us": max(upper_values),
        }

    all_edges = [edge for item in cycles for edge in item["edge_estimates"]]
    maximum_threshold_span = max(float(edge["threshold_span_us"]) for edge in all_edges)
    maximum_independent_delta = max(
        float(edge["independent_estimator_delta_us"]) for edge in all_edges
    )
    minimum_transition_snr = min(float(edge["transition_snr_db"]) for edge in all_edges)
    rf_states: list[dict[str, Any]] = []
    for state in range(1, 7):
        name = f"ANT{state}"
        documents = state_rf[name]
        snr_values = [float(item["pilot_snr_db"]) for item in documents]
        contrast_values = [float(item["state_to_null_contrast_db"]) for item in documents]
        rf_states.append(
            {
                "name": name,
                "pilot_snr_db": _stats(snr_values),
                "state_to_null_contrast_db": _stats(contrast_values),
            }
        )
    minimum_pilot_snr = min(
        _required_stat(item["pilot_snr_db"], "minimum", str(item["name"])) for item in rf_states
    )
    minimum_contrast = min(
        _required_stat(item["state_to_null_contrast_db"], "minimum", str(item["name"]))
        for item in rf_states
    )
    quality = _quality_document(
        complete_cycles=len(cycles),
        decoded_fraction=decoded_fraction,
        marker=_stats(marker_values),
        dwells=dwell_stats,
        guards=guard_stats,
        cycle=_stats(cycle_values),
        maximum_threshold_span_us=maximum_threshold_span,
        maximum_independent_delta_us=maximum_independent_delta,
        minimum_transition_snr_db=minimum_transition_snr,
        minimum_pilot_snr_db=minimum_pilot_snr,
        minimum_contrast_db=minimum_contrast,
    )
    return {
        "schema": 1,
        "analysis_kind": "hexcal_v1_rf_timing_only",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "detector": {
            **detector,
            "coherent_samples_per_bin": SAMPLES_PER_COHERENT_BIN,
            "bin_duration_us": BIN_DURATION_US,
            "threshold_sweep_q": list(THRESHOLD_SWEEP),
            "accepted_edge_time": "local_complex_projection_q50_fractional_crossing",
            "independent_estimator": "local_two_mean_complex_changepoint",
        },
        "pilot": {
            "dds_frequency_readback_hz": dds_readback_hz,
            "refined_pilot_offset_hz": dds_readback_hz + residual_hz,
            **pilot,
        },
        "decode": {
            "candidate_pattern_cycle_count": len(proposals),
            "edge_estimation_rejection_count": rejected_edge_estimation,
            "complete_cycle_count": len(cycles),
            "conservative_possible_complete_cycles": conservative_possible_cycles,
            "decoded_cycle_fraction": decoded_fraction,
            "visible_edges_per_accepted_cycle": 12,
            "missing_or_extra_patterns_are_rejected": True,
            "nominal_positions_used_only_for_source_backed_grammar": True,
        },
        "timing": {
            "combined_rf_marker_us": _stats(marker_values),
            "dwells_us": dwell_stats,
            "ordinary_guards_us": guard_stats,
            "cycle_us": _stats(cycle_values),
            "maximum_q40_q60_edge_span_us": maximum_threshold_span,
            "maximum_independent_estimator_delta_us": maximum_independent_delta,
        },
        "rf_admission": {
            "minimum_transition_snr_db": minimum_transition_snr,
            "minimum_pilot_snr_db": minimum_pilot_snr,
            "minimum_state_to_null_contrast_db": minimum_contrast,
            "states": rf_states,
        },
        "quality": quality,
        "cycles": cycles,
        "limitations": [
            "RF observes the combined approximately 200 us ALL_OFF marker; it cannot "
            "separate the 180 us marker body from the contiguous pre-ANT1 guard.",
            "Slot order and nominal positions are source-backed and are not independent "
            "proof of GPIO code or physical connector identity.",
            "All durations are relative to the Pluto sample clock, not a calibrated SI "
            "timebase or an independent logic analyzer.",
        ],
    }


def analyze_hexcal_timing_samples(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    dds_readback_hz: float,
    profile: HexcalProfile,
    continuity_verified: bool,
) -> dict[str, Any]:
    """Run the complete 2 MS/s detector and timing-only RF qualification."""

    if continuity_verified is not True:
        raise ValueError("RF timing analysis requires independently verified ABI2 continuity")
    envelope = coherent_one_microsecond_detector(
        samples,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=dds_readback_hz,
    )
    result = analyze_hexcal_timing_envelope(
        envelope,
        dds_readback_hz=dds_readback_hz,
        profile=profile,
    )
    result["continuity_verified"] = True
    result["raw_sample_count"] = int(np.asarray(samples).size)
    result["one_microsecond_bin_count"] = int(envelope.size)
    return result


__all__ = [
    "BANDWIDTH_HZ",
    "BIN_DURATION_US",
    "MINIMUM_COMPLETE_CYCLES",
    "SAMPLES_PER_COHERENT_BIN",
    "SAMPLE_RATE_HZ",
    "TIMING_RECEIVER_GAIN_DB",
    "analyze_hexcal_timing_envelope",
    "analyze_hexcal_timing_samples",
    "coherent_one_microsecond_detector",
]
