"""Offline fast20 state measurements from a known OTA tone."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import atan2, exp, log10, pi, sqrt

import numpy as np
import numpy.typing as npt

from .profile import ControlProfile

ALL_OFF = "ALL_OFF"


@dataclass(frozen=True, slots=True)
class ToneStateEstimate:
    """Robust tone measurement for one selector state."""

    name: str
    amplitude: float
    power: float
    power_db: float
    contrast_db: float
    robust_spread_db: float
    bin_count: int


@dataclass(frozen=True, slots=True)
class Fast20ToneAnalysis:
    """Profile-aligned per-state result in the input IQ sample units."""

    cycle_ms: float
    marker_phase_ms: float
    bin_duration_ms: float
    bin_count: int
    observed_cycles: float
    edge_exclusion_ms: float
    alignment_score: float
    cycle_repeatability_score: float
    confidence: float
    median_contrast_db: float
    states: tuple[ToneStateEstimate, ...]
    all_off: ToneStateEstimate

    def estimate(self, name: str) -> ToneStateEstimate:
        """Return a named ANT estimate or the ALL_OFF estimate."""

        if name == ALL_OFF:
            return self.all_off
        for estimate in self.states:
            if estimate.name == name:
                return estimate
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class ContinuityBlock:
    """Caller-validated source block included in a contiguous IQ capture."""

    sample_start: int
    sample_count: int
    utc_ns: int


@dataclass(frozen=True, slots=True)
class PhaseStateEstimate:
    """Phase-sensitive OTA estimate relative to the strongest ANT state."""

    name: str
    complex_delta: complex
    amplitude: float
    relative_db: float
    phase_deg: float
    detection_snr_db: float
    cycle_coherence: float
    even_odd_phase_agreement: float
    confidence: float
    cycle_count: int
    bin_count: int


@dataclass(frozen=True, slots=True)
class Fast20PhaseAnalysis:
    """Two-channel leakage-cancelled fast20 analysis."""

    cycle_ms: float
    marker_phase_ms: float
    bin_duration_ms: float
    bin_count: int
    complete_cycle_count: int
    edge_exclusion_ms: float
    leakage_coefficient: complex
    all_off_residual_amplitude: float
    alignment_score: float
    even_odd_cycle_agreement: float
    jackknife_stability: float
    confidence: float
    continuity_verified: bool
    continuity_block_count: int
    states: tuple[PhaseStateEstimate, ...]

    def estimate(self, name: str) -> PhaseStateEstimate:
        """Return one named ANT estimate."""

        for estimate in self.states:
            if estimate.name == name:
                return estimate
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class CoherentPilotEstimate:
    """Average pilot-frequency refinement and phase-fit diagnostics."""

    nominal_offset_hz: float
    estimated_offset_hz: float
    residual_offset_hz: float
    fit_standard_error_hz: float
    bin_duration_ms: float
    bin_count: int
    used_bin_count: int
    coherent_amplitude: float
    phase_residual_rms_rad: float
    phase_step_coherence: float
    confidence: float


@dataclass(frozen=True, slots=True)
class FftPhaseStateEstimate:
    """FFT-bin transfer estimate relative to the selected reference state."""

    name: str
    complex_delta: complex
    amplitude: float
    relative_db: float
    phase_deg: float
    cycle_phase_std_deg: float
    cycle_coherence: float
    cycle_count: int


@dataclass(frozen=True, slots=True)
class GuardedFftPhaseAnalysis:
    """Per-state FFT comparison aligned to one guarded selector schedule."""

    cycle_ms: float
    marker_phase_ms: float
    complete_cycle_count: int
    fft_size: int
    fft_bin_index: int
    fft_bin_frequency_hz: float
    requested_tone_offset_hz: float
    reference_state: str
    alignment_confidence: float
    continuity_verified: bool
    continuity_block_count: int
    states: tuple[FftPhaseStateEstimate, ...]

    def estimate(self, name: str) -> FftPhaseStateEstimate:
        """Return one named antenna estimate."""

        for estimate in self.states:
            if estimate.name == name:
                return estimate
        raise KeyError(name)

    def phase_difference_deg(self, first: str, second: str) -> float:
        """Return wrapped phase(first) minus phase(second), in degrees."""

        difference = self.estimate(first).phase_deg - self.estimate(second).phase_deg
        return float((difference + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True, slots=True)
class _Alignment:
    cycle_ms: float
    marker_phase_ms: float
    loss: float


@dataclass(frozen=True, slots=True)
class _PhaseAlignment:
    cycle_ms: float
    marker_phase_ms: float
    score: float
    even_odd_agreement: float
    cycle_coherence: float
    complete_cycle_count: int


def _grid(low: float, high: float, step: float) -> npt.NDArray[np.float64]:
    count = max(1, int(np.floor((high - low) / step)) + 1)
    values = low + np.arange(count, dtype=np.float64) * step
    if values[-1] < high - step * 0.25:
        values = np.append(values, high)
    return values


def _labels_and_interior(
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.bool_]]:
    """Assign bins to the scaled profile and remove transition-adjacent bins."""

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
    # NumPy can round a tiny negative dividend modulo ``cycle_ms`` to exactly
    # ``cycle_ms``. That value is the cycle boundary (position zero), not a
    # seventeenth schedule segment.
    position[position >= cycle_ms] = 0.0
    segment_index = np.searchsorted(boundary_array, position, side="right") - 1
    label_lookup = np.asarray(segment_labels[:-1], dtype=np.int16)
    segment_index = np.clip(segment_index, 0, label_lookup.size - 1)
    labels = label_lookup[segment_index]
    distance_from_previous = position - boundary_array[segment_index]
    distance_to_next = boundary_array[segment_index + 1] - position
    interior = np.minimum(distance_from_previous, distance_to_next) >= edge_exclusion_ms
    return labels, interior


def _clipped_group_loss(
    values_db: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    interior: npt.NDArray[np.bool_],
    *,
    group_count: int,
    clip_db: float,
) -> float:
    selected_labels = labels[interior]
    selected_values = values_db[interior]
    counts = np.bincount(selected_labels, minlength=group_count)
    if np.any(counts < 3):
        return float("inf")
    sums = np.bincount(selected_labels, weights=selected_values, minlength=group_count)
    centers = sums / counts
    residual = np.minimum(np.abs(selected_values - centers[selected_labels]), clip_db)
    group_losses = np.bincount(selected_labels, weights=residual, minlength=group_count) / counts
    return float(np.mean(group_losses))


def _alignment_loss(
    values_db: npt.NDArray[np.float64],
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    clip_db: float,
    profile: ControlProfile,
) -> float:
    labels, interior = _labels_and_interior(
        times_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    return _clipped_group_loss(
        values_db,
        labels,
        interior,
        group_count=len(profile.states) + 1,
        clip_db=clip_db,
    )


def _search_alignment(
    values_db: npt.NDArray[np.float64],
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_range_ms: tuple[float, float],
    bin_duration_ms: float,
    edge_exclusion_ms: float,
    clip_db: float,
    profile: ControlProfile,
) -> _Alignment:
    low, high = cycle_range_ms
    coarse_cycle_step = max(0.25, bin_duration_ms / 2.0)
    coarse_phase_step = bin_duration_ms
    best = _Alignment(cycle_ms=low, marker_phase_ms=0.0, loss=float("inf"))

    for cycle_ms in _grid(low, high, coarse_cycle_step):
        for marker_phase_ms in np.arange(0.0, cycle_ms, coarse_phase_step):
            loss = _alignment_loss(
                values_db,
                times_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=float(marker_phase_ms),
                edge_exclusion_ms=edge_exclusion_ms,
                clip_db=clip_db,
                profile=profile,
            )
            if loss < best.loss:
                best = _Alignment(float(cycle_ms), float(marker_phase_ms), loss)

    fine_step = max(0.05, bin_duration_ms / 10.0)
    fine_cycles = _grid(
        max(low, best.cycle_ms - coarse_cycle_step),
        min(high, best.cycle_ms + coarse_cycle_step),
        fine_step,
    )
    phase_offsets = _grid(-bin_duration_ms, bin_duration_ms, fine_step)
    for cycle_ms in fine_cycles:
        for offset_ms in phase_offsets:
            marker_phase_ms = float((best.marker_phase_ms + offset_ms) % cycle_ms)
            loss = _alignment_loss(
                values_db,
                times_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=marker_phase_ms,
                edge_exclusion_ms=edge_exclusion_ms,
                clip_db=clip_db,
                profile=profile,
            )
            if loss < best.loss:
                best = _Alignment(float(cycle_ms), marker_phase_ms, loss)
    return best


def _baseline_loss(
    values_db: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    interior: npt.NDArray[np.bool_],
    *,
    group_count: int,
    clip_db: float,
) -> float:
    center = float(np.median(values_db[interior]))
    losses = []
    for index in range(group_count):
        selected = values_db[interior & (labels == index)]
        losses.append(float(np.mean(np.minimum(np.abs(selected - center), clip_db))))
    return float(np.mean(losses))


def _repeatability_score(
    values_db: npt.NDArray[np.float64],
    times_ms: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    interior: npt.NDArray[np.bool_],
    *,
    alignment: _Alignment,
    edge_exclusion_ms: float,
    clip_db: float,
    baseline_loss: float,
    profile: ControlProfile,
) -> float:
    paired = times_ms + alignment.cycle_ms <= times_ms[-1]
    if not np.any(paired):
        return 0.0
    shifted_times = times_ms[paired] + alignment.cycle_ms
    shifted_values = np.interp(shifted_times, times_ms, values_db)
    shifted_labels, shifted_interior = _labels_and_interior(
        shifted_times,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    usable = interior[paired] & shifted_interior & (labels[paired] == shifted_labels)
    if np.count_nonzero(usable) < len(profile.states) + 1:
        return 0.0
    difference = np.minimum(np.abs(values_db[paired][usable] - shifted_values[usable]), clip_db)
    repeat_loss = float(np.mean(difference))
    denominator = max(baseline_loss, np.finfo(np.float64).eps)
    return float(np.clip(1.0 - repeat_loss / denominator, 0.0, 1.0))


def _state_estimate(
    name: str,
    amplitudes: npt.NDArray[np.float64],
    powers: npt.NDArray[np.float64],
    powers_db: npt.NDArray[np.float64],
    *,
    off_power_db: float,
) -> ToneStateEstimate:
    power_db = float(np.median(powers_db))
    median_absolute_deviation = float(np.median(np.abs(powers_db - power_db)))
    return ToneStateEstimate(
        name=name,
        amplitude=float(np.median(amplitudes)),
        power=float(np.median(powers)),
        power_db=power_db,
        contrast_db=power_db - off_power_db,
        robust_spread_db=1.4826 * median_absolute_deviation,
        bin_count=int(amplitudes.size),
    )


def analyze_fast20_tone(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    profile: ControlProfile,
    bin_ms: float = 1.0,
    edge_exclusion_bins: int = 1,
    cycle_search_ms: tuple[float, float] | None = None,
) -> Fast20ToneAnalysis:
    """Measure a known tone in every fast20 state without touching hardware.

    The tone is coherently demodulated into approximately ``bin_ms`` bins. The
    profile is uniformly time-scaled while cycle length and marker phase are
    searched, so captures may start at any point in a roughly 382--390 ms frame.
    Bins next to every RF selection transition are excluded from all estimates.
    Amplitude and power remain in the caller's IQ units; ``power_db`` is dB
    relative to one squared IQ unit.
    """

    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive and finite")
    if not np.isfinite(tone_offset_hz) or abs(tone_offset_hz) >= sample_rate_hz / 2.0:
        raise ValueError("tone offset must be finite and strictly inside Nyquist")
    if not np.isfinite(bin_ms) or bin_ms <= 0:
        raise ValueError("bin duration must be positive and finite")
    if edge_exclusion_bins < 0:
        raise ValueError("edge exclusion must not be negative")

    raw = np.asarray(samples)
    if raw.ndim != 1 or not np.iscomplexobj(raw):
        raise ValueError("samples must be a one-dimensional complex array")
    values = raw.astype(np.complex128, copy=False)
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise ValueError("samples must be finite")

    cycle_range = cycle_search_ms or (
        profile.nominal_cycle_ms - 4.0,
        profile.nominal_cycle_ms + 4.0,
    )
    cycle_low, cycle_high = cycle_range
    if (
        not np.isfinite(cycle_low)
        or not np.isfinite(cycle_high)
        or cycle_low <= 0
        or cycle_high < cycle_low
    ):
        raise ValueError("cycle search bounds must be finite, positive and ordered")

    samples_per_bin = round(sample_rate_hz * bin_ms / 1000.0)
    if samples_per_bin < 1:
        raise ValueError("bin duration is shorter than one sample")
    complete_bins = values.size // samples_per_bin
    actual_bin_ms = samples_per_bin * 1000.0 / sample_rate_hz
    edge_exclusion_ms = edge_exclusion_bins * actual_bin_ms
    if complete_bins * actual_bin_ms < 2.0 * cycle_high:
        raise ValueError("capture must span at least two maximum-length candidate cycles")

    used_sample_count = complete_bins * samples_per_bin
    sample_index = np.arange(used_sample_count, dtype=np.float64)
    oscillator = np.exp(-2j * np.pi * tone_offset_hz * sample_index / sample_rate_hz)
    mixed = values[:used_sample_count] * oscillator
    phasors = np.mean(mixed.reshape(complete_bins, samples_per_bin), axis=1)
    amplitudes = np.abs(phasors)
    powers = amplitudes**2
    peak_power = float(np.max(powers))
    power_floor = max(peak_power * 1e-12, np.finfo(np.float64).tiny)
    powers_db = 10.0 * np.log10(np.maximum(powers, power_floor))
    times_ms = (np.arange(complete_bins, dtype=np.float64) + 0.5) * actual_bin_ms

    global_center = float(np.median(powers_db))
    global_deviation = np.abs(powers_db - global_center)
    clip_db = max(1.0, float(np.percentile(global_deviation, 90.0)))
    alignment = _search_alignment(
        powers_db,
        times_ms,
        cycle_range_ms=cycle_range,
        bin_duration_ms=actual_bin_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        clip_db=clip_db,
        profile=profile,
    )
    labels, interior = _labels_and_interior(
        times_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    group_count = len(profile.states) + 1
    baseline_loss = _baseline_loss(
        powers_db,
        labels,
        interior,
        group_count=group_count,
        clip_db=clip_db,
    )
    alignment_score = float(
        np.clip(
            1.0 - alignment.loss / max(baseline_loss, np.finfo(np.float64).eps),
            0.0,
            1.0,
        )
    )
    repeatability_score = _repeatability_score(
        powers_db,
        times_ms,
        labels,
        interior,
        alignment=alignment,
        edge_exclusion_ms=edge_exclusion_ms,
        clip_db=clip_db,
        baseline_loss=baseline_loss,
        profile=profile,
    )

    all_off_mask = interior & (labels == len(profile.states))
    off_power_db = float(np.median(powers_db[all_off_mask]))
    all_off = _state_estimate(
        ALL_OFF,
        amplitudes[all_off_mask],
        powers[all_off_mask],
        powers_db[all_off_mask],
        off_power_db=off_power_db,
    )
    state_estimates = []
    for index, state in enumerate(profile.states):
        mask = interior & (labels == index)
        state_estimates.append(
            _state_estimate(
                state.name,
                amplitudes[mask],
                powers[mask],
                powers_db[mask],
                off_power_db=off_power_db,
            )
        )
    median_contrast_db = float(np.median([estimate.contrast_db for estimate in state_estimates]))
    contrast_factor = 1.0 - exp(-max(0.0, median_contrast_db) / 6.0)
    confidence = float(
        np.clip(
            sqrt(alignment_score * repeatability_score) * contrast_factor,
            0.0,
            1.0,
        )
    )

    return Fast20ToneAnalysis(
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        bin_duration_ms=actual_bin_ms,
        bin_count=complete_bins,
        observed_cycles=complete_bins * actual_bin_ms / alignment.cycle_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        alignment_score=alignment_score,
        cycle_repeatability_score=repeatability_score,
        confidence=confidence,
        median_contrast_db=median_contrast_db,
        states=tuple(state_estimates),
        all_off=all_off,
    )


def _validate_complex_pair(
    rx1_samples: npt.ArrayLike, rx2_samples: npt.ArrayLike
) -> tuple[np.ndarray, np.ndarray]:
    reference = np.asarray(rx1_samples)
    measurement = np.asarray(rx2_samples)
    if reference.ndim != 1 or measurement.ndim != 1:
        raise ValueError("RX1 and RX2 samples must be one-dimensional")
    if not np.iscomplexobj(reference) or not np.iscomplexobj(measurement):
        raise ValueError("RX1 and RX2 samples must be complex")
    if reference.size != measurement.size:
        raise ValueError("RX1 and RX2 sample counts must match")
    if reference.size == 0:
        raise ValueError("RX1 and RX2 samples must not be empty")
    chunk_samples = 1_048_576
    for start in range(0, reference.size, chunk_samples):
        stop = min(reference.size, start + chunk_samples)
        for label, values in (
            ("RX1", reference[start:stop]),
            ("RX2", measurement[start:stop]),
        ):
            if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
                raise ValueError(f"{label} samples must be finite")
    return reference, measurement


def _validate_continuity_ledger(
    ledger: Sequence[ContinuityBlock] | None, *, sample_count: int
) -> tuple[bool, int]:
    if ledger is None:
        return False, 0
    if not ledger:
        raise ValueError("continuity ledger must not be empty")
    expected_start = 0
    previous_utc_ns: int | None = None
    for block in ledger:
        if block.sample_start != expected_start:
            raise ValueError("continuity ledger sample ranges must be contiguous")
        if block.sample_count <= 0:
            raise ValueError("continuity ledger block counts must be positive")
        if block.utc_ns < 0:
            raise ValueError("continuity ledger timestamps must not be negative")
        if previous_utc_ns is not None and block.utc_ns <= previous_utc_ns:
            raise ValueError("continuity ledger timestamps must increase")
        expected_start += block.sample_count
        previous_utc_ns = block.utc_ns
    if expected_start != sample_count:
        raise ValueError("continuity ledger must cover the complete paired capture")
    return True, len(ledger)


def _coherent_pair_bins(
    reference: np.ndarray,
    measurement: np.ndarray,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    samples_per_bin: int,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.complex128]]:
    complete_bins = reference.size // samples_per_bin
    reference_bins = np.empty(complete_bins, dtype=np.complex128)
    measurement_bins = np.empty(complete_bins, dtype=np.complex128)
    chunk_bins = 256
    for first_bin in range(0, complete_bins, chunk_bins):
        last_bin = min(complete_bins, first_bin + chunk_bins)
        first_sample = first_bin * samples_per_bin
        last_sample = last_bin * samples_per_bin
        sample_index = np.arange(first_sample, last_sample, dtype=np.float64)
        oscillator = np.exp(-2j * np.pi * tone_offset_hz * sample_index / sample_rate_hz)
        shape = (last_bin - first_bin, samples_per_bin)
        reference_bins[first_bin:last_bin] = np.mean(
            reference[first_sample:last_sample].astype(np.complex128, copy=False).reshape(shape)
            * oscillator.reshape(shape),
            axis=1,
        )
        measurement_bins[first_bin:last_bin] = np.mean(
            measurement[first_sample:last_sample].astype(np.complex128, copy=False).reshape(shape)
            * oscillator.reshape(shape),
            axis=1,
        )
    return reference_bins, measurement_bins


def _coherent_single_bins(
    samples: np.ndarray,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    samples_per_bin: int,
) -> npt.NDArray[np.complex128]:
    complete_bins = samples.size // samples_per_bin
    phasors = np.empty(complete_bins, dtype=np.complex128)
    chunk_bins = 256
    for first_bin in range(0, complete_bins, chunk_bins):
        last_bin = min(complete_bins, first_bin + chunk_bins)
        first_sample = first_bin * samples_per_bin
        last_sample = last_bin * samples_per_bin
        sample_index = np.arange(first_sample, last_sample, dtype=np.float64)
        oscillator = np.exp(-2j * np.pi * tone_offset_hz * sample_index / sample_rate_hz)
        shape = (last_bin - first_bin, samples_per_bin)
        phasors[first_bin:last_bin] = np.mean(
            samples[first_sample:last_sample].astype(np.complex128, copy=False).reshape(shape)
            * oscillator.reshape(shape),
            axis=1,
        )
    return phasors


def _weighted_phase_line(
    times_s: npt.NDArray[np.float64],
    phases_rad: npt.NDArray[np.float64],
    base_weights: npt.NDArray[np.float64],
) -> tuple[float, float, npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    weights = base_weights.copy()
    slope = 0.0
    intercept = 0.0
    residual = phases_rad.copy()
    for _ in range(4):
        weight_sum = float(np.sum(weights))
        mean_time = float(np.sum(weights * times_s) / weight_sum)
        mean_phase = float(np.sum(weights * phases_rad) / weight_sum)
        centered_time = times_s - mean_time
        denominator = float(np.sum(weights * centered_time**2))
        if denominator <= np.finfo(np.float64).tiny:
            raise ValueError("pilot capture has insufficient time span")
        slope = float(np.sum(weights * centered_time * (phases_rad - mean_phase)) / denominator)
        intercept = mean_phase - slope * mean_time
        residual = phases_rad - (intercept + slope * times_s)
        scale = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        if scale <= np.finfo(np.float64).eps:
            break
        huber_limit = 1.5 * scale
        robust_weights = np.minimum(1.0, huber_limit / np.maximum(np.abs(residual), huber_limit))
        weights = base_weights * robust_weights
    return slope, intercept, residual, weights


def estimate_coherent_pilot_offset(
    rx1_samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    nominal_tone_offset_hz: float,
    bin_ms: float = 1.0,
    maximum_residual_hz: float | None = None,
) -> CoherentPilotEstimate:
    """Refine a nominal pilot offset from the terminated RX1 reference.

    The estimator coherently demodulates bounded chunks at the nominal offset,
    obtains a circular phase-step estimate, and robustly fits the remaining
    unwrapped phase slope. It estimates one average frequency over the capture;
    it does not track chirps, sample discontinuities, multiple unresolved pilots,
    or calibrated oscillator error. The true residual must remain within
    ``maximum_residual_hz`` and below the phasor-bin Nyquist limit. Inspect
    ``confidence`` before passing ``estimated_offset_hz`` to
    :func:`analyze_fast20_phase_sensitive`.
    """

    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive and finite")
    if (
        not np.isfinite(nominal_tone_offset_hz)
        or abs(nominal_tone_offset_hz) >= sample_rate_hz / 2.0
    ):
        raise ValueError("nominal tone offset must be finite and strictly inside Nyquist")
    if not np.isfinite(bin_ms) or bin_ms <= 0:
        raise ValueError("bin duration must be positive and finite")
    raw = np.asarray(rx1_samples)
    if raw.ndim != 1 or not np.iscomplexobj(raw):
        raise ValueError("RX1 samples must be a one-dimensional complex array")
    if raw.size == 0:
        raise ValueError("RX1 samples must not be empty")
    chunk_samples = 1_048_576
    for start in range(0, raw.size, chunk_samples):
        values = raw[start : start + chunk_samples]
        if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
            raise ValueError("RX1 samples must be finite")

    samples_per_bin = round(sample_rate_hz * bin_ms / 1000.0)
    if samples_per_bin < 1:
        raise ValueError("bin duration is shorter than one sample")
    actual_bin_s = samples_per_bin / sample_rate_hz
    unambiguous_limit_hz = 0.45 / actual_bin_s
    residual_limit_hz = 0.4 / actual_bin_s if maximum_residual_hz is None else maximum_residual_hz
    if (
        not np.isfinite(residual_limit_hz)
        or residual_limit_hz <= 0
        or residual_limit_hz > unambiguous_limit_hz
    ):
        raise ValueError("maximum residual must be positive and no more than 45% of bin rate")

    phasors = _coherent_single_bins(
        raw,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=nominal_tone_offset_hz,
        samples_per_bin=samples_per_bin,
    )
    if phasors.size < 128:
        raise ValueError("pilot capture must contain at least 128 coherent bins")
    amplitudes = np.abs(phasors)
    median_amplitude = float(np.median(amplitudes))
    if median_amplitude <= np.finfo(np.float64).tiny:
        raise ValueError("RX1 has no usable coherent pilot")
    valid = amplitudes >= 0.2 * median_amplitude
    if np.count_nonzero(valid) < 128:
        raise ValueError("RX1 has fewer than 128 usable coherent pilot bins")
    pair_valid = valid[:-1] & valid[1:]
    if np.count_nonzero(pair_valid) < 64:
        raise ValueError("RX1 pilot is too discontinuous for phase-step estimation")
    unit_phasors = phasors / np.maximum(amplitudes, np.finfo(np.float64).tiny)
    phase_steps = unit_phasors[1:] * np.conj(unit_phasors[:-1])
    mean_step = complex(np.mean(phase_steps[pair_valid]))
    coarse_residual_hz = atan2(mean_step.imag, mean_step.real) / (2.0 * pi * actual_bin_s)

    all_times_s = (np.arange(phasors.size, dtype=np.float64) + 0.5) * actual_bin_s
    times_s = all_times_s[valid]
    coarse_removed = phasors[valid] * np.exp(-2j * pi * coarse_residual_hz * times_s)
    phases_rad = np.unwrap(np.angle(coarse_removed))
    valid_amplitudes = amplitudes[valid]
    amplitude_cap = max(float(np.percentile(valid_amplitudes, 90.0)), median_amplitude)
    base_weights = np.minimum(valid_amplitudes, amplitude_cap) ** 2
    slope, _, residual_phase, weights = _weighted_phase_line(times_s, phases_rad, base_weights)
    refined_residual_hz = coarse_residual_hz + slope / (2.0 * pi)
    if abs(refined_residual_hz) > residual_limit_hz:
        raise ValueError("estimated pilot residual exceeds the configured search limit")

    weight_sum = float(np.sum(weights))
    phase_rms = sqrt(float(np.sum(weights * residual_phase**2) / weight_sum))
    mean_time = float(np.sum(weights * times_s) / weight_sum)
    time_energy = float(np.sum(weights * (times_s - mean_time) ** 2))
    effective_count = max(3.0, weight_sum**2 / float(np.sum(weights**2)))
    residual_variance = float(np.sum(weights * residual_phase**2)) / (effective_count - 2.0)
    slope_standard_error = sqrt(residual_variance / max(time_energy, np.finfo(float).tiny))
    frequency_standard_error = slope_standard_error / (2.0 * pi)

    corrected_steps = phase_steps[pair_valid] * np.exp(
        -2j * pi * refined_residual_hz * actual_bin_s
    )
    phase_step_coherence = float(np.clip(abs(np.mean(corrected_steps)), 0.0, 1.0))
    coverage = float(np.count_nonzero(valid) / phasors.size)
    duration_factor = float(np.clip(phasors.size * actual_bin_s, 0.0, 1.0))
    confidence = phase_step_coherence * coverage * exp(-phase_rms / 1.5) * duration_factor
    return CoherentPilotEstimate(
        nominal_offset_hz=float(nominal_tone_offset_hz),
        estimated_offset_hz=float(nominal_tone_offset_hz + refined_residual_hz),
        residual_offset_hz=float(refined_residual_hz),
        fit_standard_error_hz=float(frequency_standard_error),
        bin_duration_ms=actual_bin_s * 1000.0,
        bin_count=int(phasors.size),
        used_bin_count=int(np.count_nonzero(valid)),
        coherent_amplitude=median_amplitude,
        phase_residual_rms_rad=phase_rms,
        phase_step_coherence=phase_step_coherence,
        confidence=float(np.clip(confidence, 0.0, 1.0)),
    )


def _complete_cycle_ids(
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    raw_cycle_ids = np.floor((times_ms - marker_phase_ms) / cycle_ms).astype(np.int64)
    unique_ids = np.unique(raw_cycle_ids)
    starts = marker_phase_ms + unique_ids * cycle_ms
    tolerance = np.finfo(np.float64).eps * max(duration_ms, cycle_ms) * 8.0
    complete_ids = unique_ids[
        (starts >= -tolerance) & (starts + cycle_ms <= duration_ms + tolerance)
    ]
    return raw_cycle_ids, complete_ids


def _linear_cancelled_cycle_deltas(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.int64], float] | None:
    labels, interior = _labels_and_interior(
        times_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    raw_cycle_ids, complete_ids = _complete_cycle_ids(
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
    )
    if complete_ids.size < 4:
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
    if np.any(denominator <= 0):
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
    fit_score = float(
        np.clip(
            1.0 - residual_energy / max(null_energy, np.finfo(np.float64).tiny),
            0.0,
            1.0,
        )
    )
    deltas = (means[:, :all_off_index] - means[:, all_off_index, None]).astype(np.complex128)
    return deltas, counts.astype(np.int64), fit_score


def _phase_candidate(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
) -> _PhaseAlignment | None:
    result = _linear_cancelled_cycle_deltas(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    if result is None:
        return None
    deltas, _, fit_score = result
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
    score = max(agreement, 0.0) * coherence * strength * fit_score
    return _PhaseAlignment(
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        score=float(np.clip(score, 0.0, 1.0)),
        even_odd_agreement=agreement,
        cycle_coherence=float(np.clip(coherence, 0.0, 1.0)),
        complete_cycle_count=deltas.shape[0],
    )


def _search_phase_alignment(
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    duration_ms: float,
    cycle_range_ms: tuple[float, float],
    bin_duration_ms: float,
    edge_exclusion_ms: float,
    profile: ControlProfile,
) -> _PhaseAlignment:
    low, high = cycle_range_ms
    coarse_cycle_step = max(0.5, bin_duration_ms)
    coarse_phase_step = max(1.0, 2.0 * bin_duration_ms)
    best: _PhaseAlignment | None = None
    for cycle_ms in _grid(low, high, coarse_cycle_step):
        for marker_phase_ms in np.arange(0.0, cycle_ms, coarse_phase_step):
            candidate = _phase_candidate(
                transfer,
                reference_valid,
                times_ms,
                duration_ms=duration_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=float(marker_phase_ms),
                edge_exclusion_ms=edge_exclusion_ms,
                profile=profile,
            )
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
    if best is None:
        raise ValueError("capture does not contain four complete candidate cycles")

    fine_step = max(0.1, bin_duration_ms / 5.0)
    fine_cycles = _grid(
        max(low, best.cycle_ms - coarse_cycle_step),
        min(high, best.cycle_ms + coarse_cycle_step),
        fine_step,
    )
    phase_offsets = _grid(-coarse_phase_step, coarse_phase_step, fine_step)
    for cycle_ms in fine_cycles:
        for offset_ms in phase_offsets:
            marker_phase_ms = float((best.marker_phase_ms + offset_ms) % cycle_ms)
            candidate = _phase_candidate(
                transfer,
                reference_valid,
                times_ms,
                duration_ms=duration_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=marker_phase_ms,
                edge_exclusion_ms=edge_exclusion_ms,
                profile=profile,
            )
            if candidate is not None and candidate.score > best.score:
                best = candidate
    return best


def _local_all_off_baseline(
    transfer: npt.NDArray[np.complex128],
    times_ms: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    usable: npt.NDArray[np.bool_],
    *,
    all_off_index: int,
    maximum_anchor_bins: int = 10,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.complex128]]:
    indices = np.flatnonzero(usable & (labels == all_off_index))
    if indices.size < 2:
        raise ValueError("aligned capture has insufficient ALL_OFF reference bins")
    cuts = np.concatenate(
        (
            np.asarray([0]),
            np.flatnonzero(np.diff(indices) > 1) + 1,
            np.asarray([indices.size]),
        )
    )
    anchor_times: list[float] = []
    anchor_values: list[complex] = []
    for first, last in zip(cuts[:-1], cuts[1:], strict=True):
        run = indices[first:last]
        chunk_count = max(1, int(np.ceil(run.size / maximum_anchor_bins)))
        for chunk in np.array_split(run, chunk_count):
            anchor_times.append(float(np.mean(times_ms[chunk])))
            anchor_values.append(
                complex(
                    float(np.median(transfer[chunk].real)),
                    float(np.median(transfer[chunk].imag)),
                )
            )
    anchor_time_array = np.asarray(anchor_times, dtype=np.float64)
    anchor_value_array = np.asarray(anchor_values, dtype=np.complex128)
    baseline = np.interp(times_ms, anchor_time_array, anchor_value_array.real) + 1j * np.interp(
        times_ms, anchor_time_array, anchor_value_array.imag
    )
    return baseline, anchor_value_array


def _group_cycle_deltas(
    values: npt.NDArray[np.complex128],
    times_ms: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    usable: npt.NDArray[np.bool_],
    *,
    duration_ms: float,
    alignment: _PhaseAlignment,
    profile: ControlProfile,
) -> tuple[
    npt.NDArray[np.complex128],
    npt.NDArray[np.int64],
    npt.NDArray[np.bool_],
]:
    raw_cycle_ids, complete_ids = _complete_cycle_ids(
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
    )
    selected = usable & np.isin(raw_cycle_ids, complete_ids)
    local_cycles = np.searchsorted(complete_ids, raw_cycle_ids[selected])
    selected_labels = labels[selected]
    group_count = len(profile.states) + 1
    combined = local_cycles * group_count + selected_labels
    counts = np.bincount(combined, minlength=complete_ids.size * group_count).reshape(
        complete_ids.size, group_count
    )
    sums = np.bincount(
        combined, weights=values[selected].real, minlength=complete_ids.size * group_count
    ) + 1j * np.bincount(
        combined, weights=values[selected].imag, minlength=complete_ids.size * group_count
    )
    means = sums.reshape(complete_ids.size, group_count) / counts
    all_off_index = len(profile.states)
    deltas = (means[:, :all_off_index] - means[:, all_off_index, None]).astype(np.complex128)
    return deltas, counts.astype(np.int64), selected


def _vector_even_odd_agreement(deltas: npt.NDArray[np.complex128]) -> float:
    even = np.mean(deltas[::2], axis=0)
    odd = np.mean(deltas[1::2], axis=0)
    denominator = sqrt(float(np.vdot(even, even).real * np.vdot(odd, odd).real))
    if denominator <= np.finfo(np.float64).tiny:
        return 0.0
    return float(np.clip(np.vdot(even, odd).real / denominator, -1.0, 1.0))


def _jackknife_standard_error(values: npt.NDArray[np.complex128]) -> float:
    count = values.size
    if count < 2:
        return float("inf")
    jackknife = (np.sum(values) - values) / (count - 1)
    center = np.mean(jackknife)
    return sqrt(float((count - 1) / count * np.sum(np.abs(jackknife - center) ** 2)))


def analyze_fast20_phase_sensitive(
    rx1_samples: npt.ArrayLike,
    rx2_samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    profile: ControlProfile,
    continuity_ledger: Sequence[ContinuityBlock] | None = None,
    bin_ms: float = 1.0,
    edge_exclusion_bins: int = 1,
    cycle_search_ms: tuple[float, float] | None = None,
) -> Fast20PhaseAnalysis:
    """Cancel coherent RX leakage and measure the complex fast20 state phasors.

    RX1 is the terminated leakage reference and RX2 is the board-common receive
    path. A complex pilot is coherently reduced to bounded bins, then the RX2/RX1
    leakage transfer is learned only from ALL_OFF intervals and locally
    interpolated across selected states. Search confidence requires complex
    agreement between even and odd complete frames. ``relative_db`` is relative
    to the strongest ANT result in this capture, not calibrated RF power.

    ``phase_deg`` is an uncalibrated within-capture RF-path fingerprint. It
    includes selector, PCB trace, antenna, coupling and receiver-channel phase;
    it is not a geometric position estimate and must not be compared across
    independently started captures without complex per-path and in-situ antenna
    calibration. Confidence describes schedule alignment and repeatability, not
    the probability that an emitter occupies a physical location.

    A supplied continuity ledger is structurally checked here; the caller remains
    responsible for validating the timestamp semantics of its capture backend.
    """

    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive and finite")
    if not np.isfinite(tone_offset_hz) or abs(tone_offset_hz) >= sample_rate_hz / 2.0:
        raise ValueError("tone offset must be finite and strictly inside Nyquist")
    if not np.isfinite(bin_ms) or bin_ms <= 0:
        raise ValueError("bin duration must be positive and finite")
    if edge_exclusion_bins < 0:
        raise ValueError("edge exclusion must not be negative")
    reference, measurement = _validate_complex_pair(rx1_samples, rx2_samples)
    continuity_verified, continuity_block_count = _validate_continuity_ledger(
        continuity_ledger, sample_count=reference.size
    )

    cycle_range = cycle_search_ms or (
        profile.nominal_cycle_ms - 4.0,
        profile.nominal_cycle_ms + 4.0,
    )
    cycle_low, cycle_high = cycle_range
    if (
        not np.isfinite(cycle_low)
        or not np.isfinite(cycle_high)
        or cycle_low <= 0
        or cycle_high < cycle_low
    ):
        raise ValueError("cycle search bounds must be finite, positive and ordered")
    samples_per_bin = round(sample_rate_hz * bin_ms / 1000.0)
    if samples_per_bin < 1:
        raise ValueError("bin duration is shorter than one sample")
    complete_bins = reference.size // samples_per_bin
    actual_bin_ms = samples_per_bin * 1000.0 / sample_rate_hz
    duration_ms = complete_bins * actual_bin_ms
    if duration_ms < 5.0 * cycle_high:
        raise ValueError("paired capture must span at least five candidate cycles")

    reference_bins, measurement_bins = _coherent_pair_bins(
        reference,
        measurement,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=tone_offset_hz,
        samples_per_bin=samples_per_bin,
    )
    reference_amplitude = np.abs(reference_bins)
    median_reference_amplitude = float(np.median(reference_amplitude))
    if median_reference_amplitude <= np.finfo(np.float64).tiny:
        raise ValueError("terminated RX1 has no usable coherent reference tone")
    reference_floor = median_reference_amplitude * 0.1
    reference_valid = reference_amplitude >= reference_floor
    if np.count_nonzero(reference_valid) < int(0.95 * complete_bins):
        raise ValueError("terminated RX1 reference tone is not continuously usable")
    transfer = np.zeros(complete_bins, dtype=np.complex128)
    transfer[reference_valid] = measurement_bins[reference_valid] / reference_bins[reference_valid]
    times_ms = (np.arange(complete_bins, dtype=np.float64) + 0.5) * actual_bin_ms
    edge_exclusion_ms = edge_exclusion_bins * actual_bin_ms
    alignment = _search_phase_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        cycle_range_ms=cycle_range,
        bin_duration_ms=actual_bin_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    labels, interior = _labels_and_interior(
        times_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    usable = interior & reference_valid
    baseline, baseline_anchors = _local_all_off_baseline(
        transfer,
        times_ms,
        labels,
        usable,
        all_off_index=len(profile.states),
    )
    cancelled_rx2 = (transfer - baseline) * reference_bins
    deltas, counts, selected = _group_cycle_deltas(
        cancelled_rx2,
        times_ms,
        labels,
        usable,
        duration_ms=duration_ms,
        alignment=alignment,
        profile=profile,
    )
    complete_cycle_count = deltas.shape[0]
    even_odd_agreement = _vector_even_odd_agreement(deltas)
    mean_delta = np.mean(deltas, axis=0)
    state_coherence = np.abs(mean_delta) ** 2 / np.maximum(
        np.mean(np.abs(deltas) ** 2, axis=0), np.finfo(np.float64).tiny
    )
    robust_deltas = np.median(deltas.real, axis=0) + 1j * np.median(deltas.imag, axis=0)
    amplitudes = np.abs(robust_deltas)
    strongest_amplitude = max(float(np.max(amplitudes)), np.finfo(np.float64).tiny)
    cycle_factor = float(np.clip((complete_cycle_count - 3) / 8.0, 0.0, 1.0))

    state_estimates: list[PhaseStateEstimate] = []
    stability_factors: list[float] = []
    for index, state in enumerate(profile.states):
        values = deltas[:, index]
        even = complex(np.mean(values[::2]))
        odd = complex(np.mean(values[1::2]))
        even_odd_phase = float(np.cos(atan2(even.imag, even.real) - atan2(odd.imag, odd.real)))
        standard_error = _jackknife_standard_error(values)
        detection_ratio = float(amplitudes[index]) / max(standard_error, np.finfo(np.float64).tiny)
        stability = 1.0 - exp(-detection_ratio / 3.0)
        stability_factors.append(stability)
        state_confidence = (
            sqrt(float(state_coherence[index]) * max(even_odd_phase, 0.0))
            * stability
            * cycle_factor
        )
        state_estimates.append(
            PhaseStateEstimate(
                name=state.name,
                complex_delta=complex(robust_deltas[index]),
                amplitude=float(amplitudes[index]),
                relative_db=20.0
                * log10(
                    max(float(amplitudes[index]), strongest_amplitude * 1e-12) / strongest_amplitude
                ),
                phase_deg=atan2(float(robust_deltas[index].imag), float(robust_deltas[index].real))
                * 180.0
                / pi,
                detection_snr_db=20.0 * log10(max(detection_ratio, np.finfo(np.float64).tiny)),
                cycle_coherence=float(np.clip(state_coherence[index], 0.0, 1.0)),
                even_odd_phase_agreement=even_odd_phase,
                confidence=float(np.clip(state_confidence, 0.0, 1.0)),
                cycle_count=complete_cycle_count,
                bin_count=int(np.sum(counts[:, index])),
            )
        )

    jackknife_stability = float(np.median(stability_factors))
    confidence = (
        sqrt(alignment.score * max(even_odd_agreement, 0.0)) * jackknife_stability * cycle_factor
    )
    all_off_mask = selected & (labels == len(profile.states))
    return Fast20PhaseAnalysis(
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        bin_duration_ms=actual_bin_ms,
        bin_count=complete_bins,
        complete_cycle_count=complete_cycle_count,
        edge_exclusion_ms=edge_exclusion_ms,
        leakage_coefficient=complex(
            float(np.median(baseline_anchors.real)),
            float(np.median(baseline_anchors.imag)),
        ),
        all_off_residual_amplitude=float(np.median(np.abs(cancelled_rx2[all_off_mask]))),
        alignment_score=alignment.score,
        even_odd_cycle_agreement=even_odd_agreement,
        jackknife_stability=jackknife_stability,
        confidence=float(np.clip(confidence, 0.0, 1.0)),
        continuity_verified=continuity_verified,
        continuity_block_count=continuity_block_count,
        states=tuple(state_estimates),
    )


def _fft_transfer_at_center(
    reference: np.ndarray,
    measurement: np.ndarray,
    *,
    center_sample: int,
    fft_size: int,
    fft_bin_index: int,
    window: npt.NDArray[np.float64],
) -> complex:
    start = center_sample - fft_size // 2
    stop = start + fft_size
    if start < 0 or stop > reference.size:
        raise ValueError("FFT window falls outside the complete capture")
    reference_fft = np.fft.fft(
        reference[start:stop].astype(np.complex128, copy=False) * window
    )[fft_bin_index]
    measurement_fft = np.fft.fft(
        measurement[start:stop].astype(np.complex128, copy=False) * window
    )[fft_bin_index]
    if abs(reference_fft) <= np.finfo(np.float64).tiny:
        raise ValueError("RX1 reference has no usable energy in the selected FFT bin")
    return complex(measurement_fft / reference_fft)


def _complex_group_loss(
    values: npt.NDArray[np.complex128],
    valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_ms: float,
    marker_phase_ms: float,
    edge_exclusion_ms: float,
    clip_amplitude: float,
    profile: ControlProfile,
) -> float:
    labels, interior = _labels_and_interior(
        times_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    usable = valid & interior
    group_losses = []
    for index in range(len(profile.states) + 1):
        selected = values[usable & (labels == index)]
        if selected.size < 3:
            return float("inf")
        center = complex(float(np.median(selected.real)), float(np.median(selected.imag)))
        group_losses.append(float(np.mean(np.minimum(np.abs(selected - center), clip_amplitude))))
    return float(np.mean(group_losses))


def _search_complex_alignment(
    values: npt.NDArray[np.complex128],
    valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    *,
    cycle_range_ms: tuple[float, float],
    bin_duration_ms: float,
    edge_exclusion_ms: float,
    clip_amplitude: float,
    profile: ControlProfile,
) -> _Alignment:
    """Align a short equal-dwell schedule from the complex RX2/RX1 transfer."""

    low, high = cycle_range_ms
    coarse_cycle_step = max(0.25, bin_duration_ms / 2.0)
    coarse_phase_step = bin_duration_ms
    best = _Alignment(cycle_ms=low, marker_phase_ms=0.0, loss=float("inf"))
    for cycle_ms in _grid(low, high, coarse_cycle_step):
        for marker_phase_ms in np.arange(0.0, cycle_ms, coarse_phase_step):
            loss = _complex_group_loss(
                values,
                valid,
                times_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=float(marker_phase_ms),
                edge_exclusion_ms=edge_exclusion_ms,
                clip_amplitude=clip_amplitude,
                profile=profile,
            )
            if loss < best.loss:
                best = _Alignment(float(cycle_ms), float(marker_phase_ms), loss)

    fine_step = max(0.05, bin_duration_ms / 10.0)
    for cycle_ms in _grid(
        max(low, best.cycle_ms - coarse_cycle_step),
        min(high, best.cycle_ms + coarse_cycle_step),
        fine_step,
    ):
        for phase_offset_ms in _grid(-bin_duration_ms, bin_duration_ms, fine_step):
            marker_phase_ms = float((best.marker_phase_ms + phase_offset_ms) % cycle_ms)
            loss = _complex_group_loss(
                values,
                valid,
                times_ms,
                cycle_ms=float(cycle_ms),
                marker_phase_ms=marker_phase_ms,
                edge_exclusion_ms=edge_exclusion_ms,
                clip_amplitude=clip_amplitude,
                profile=profile,
            )
            if loss < best.loss:
                best = _Alignment(float(cycle_ms), marker_phase_ms, loss)
    if not np.isfinite(best.loss):
        raise ValueError("capture cannot be aligned to the guarded selector profile")
    return best


def analyze_guarded_fft_phase(
    rx1_samples: npt.ArrayLike,
    rx2_samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    profile: ControlProfile,
    continuity_ledger: Sequence[ContinuityBlock] | None = None,
    fft_size: int = 65_536,
    edge_exclusion_ms: float = 2.0,
    reference_state: str = "ANT1",
) -> GuardedFftPhaseAnalysis:
    """Compare selector-state phases from a common complex FFT bin.

    The generated schedule is first aligned from the complex RX2/RX1 tone
    transfer. A Hann-windowed FFT is then evaluated at the same
    tone bin for the central portion of every complete marker and antenna dwell.
    RX2/RX1 transfer during the marker and inter-state ALL_OFF guards is used
    to interpolate and subtract the local leakage baseline. Reported phases are
    relative to ``reference_state``.

    This is a coherent within-capture comparison, not geometric calibration.
    Selector, PCB, antenna, coupling and receiver-path phase remain in the
    result. Buffer/sample-counter continuity must be verified by the capture
    backend before its ledger is supplied here.
    """

    if fft_size < 16 or fft_size & (fft_size - 1):
        raise ValueError("FFT size must be a power of two and at least 16")
    if not np.isfinite(edge_exclusion_ms) or edge_exclusion_ms < 0:
        raise ValueError("FFT edge exclusion must be finite and non-negative")
    if reference_state not in {state.name for state in profile.states}:
        raise ValueError("FFT reference state is not present in the profile")
    reference, measurement = _validate_complex_pair(rx1_samples, rx2_samples)
    minimum_interior_ms = min(
        profile.marker_body_ms - 2.0 * edge_exclusion_ms,
        *(state.dwell_ms - 2.0 * edge_exclusion_ms for state in profile.states),
    )
    if minimum_interior_ms <= 0:
        raise ValueError("FFT edge exclusion leaves no state interior")
    available_samples = int(np.floor(minimum_interior_ms * sample_rate_hz / 1000.0))
    if fft_size > available_samples:
        raise ValueError("FFT size does not fit inside every edge-excluded dwell")

    continuity_verified, continuity_block_count = _validate_continuity_ledger(
        continuity_ledger,
        sample_count=reference.size,
    )
    samples_per_bin = round(sample_rate_hz / 1000.0)
    reference_bins, measurement_bins = _coherent_pair_bins(
        reference,
        measurement,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=tone_offset_hz,
        samples_per_bin=samples_per_bin,
    )
    reference_amplitudes = np.abs(reference_bins)
    reference_floor = 0.2 * float(np.median(reference_amplitudes))
    reference_valid = reference_amplitudes >= reference_floor
    if np.count_nonzero(reference_valid) < 128:
        raise ValueError("RX1 reference has fewer than 128 usable coherent bins")
    transfer = np.zeros(reference_bins.size, dtype=np.complex128)
    transfer[reference_valid] = (
        measurement_bins[reference_valid] / reference_bins[reference_valid]
    )
    transfer_center = complex(
        float(np.median(transfer[reference_valid].real)),
        float(np.median(transfer[reference_valid].imag)),
    )
    transfer_residual = np.abs(transfer[reference_valid] - transfer_center)
    clip_amplitude = max(
        float(np.percentile(transfer_residual, 90.0)),
        np.finfo(np.float64).eps,
    )
    bin_duration_ms = samples_per_bin * 1000.0 / sample_rate_hz
    times_ms = (np.arange(transfer.size, dtype=np.float64) + 0.5) * bin_duration_ms
    cycle_range_ms = (
        profile.nominal_cycle_ms - 4.0,
        profile.nominal_cycle_ms + 4.0,
    )
    if transfer.size * bin_duration_ms < 2.0 * cycle_range_ms[1]:
        raise ValueError("capture must span at least two maximum-length candidate cycles")
    aligned = _search_complex_alignment(
        transfer,
        reference_valid,
        times_ms,
        cycle_range_ms=cycle_range_ms,
        bin_duration_ms=bin_duration_ms,
        edge_exclusion_ms=max(edge_exclusion_ms, bin_duration_ms),
        clip_amplitude=clip_amplitude,
        profile=profile,
    )
    baseline_loss = float(np.mean(np.minimum(transfer_residual, clip_amplitude)))
    alignment_confidence = float(
        np.clip(
            1.0 - aligned.loss / max(baseline_loss, np.finfo(np.float64).eps),
            0.0,
            1.0,
        )
    )
    aligned_labels, aligned_interior = _labels_and_interior(
        times_ms,
        cycle_ms=aligned.cycle_ms,
        marker_phase_ms=aligned.marker_phase_ms,
        edge_exclusion_ms=max(edge_exclusion_ms, bin_duration_ms),
        profile=profile,
    )
    leakage_baseline, _ = _local_all_off_baseline(
        transfer,
        times_ms,
        aligned_labels,
        reference_valid & aligned_interior,
        all_off_index=len(profile.states),
    )
    duration_ms = reference.size * 1000.0 / sample_rate_hz
    first_cycle = int(np.ceil(-aligned.marker_phase_ms / aligned.cycle_ms))
    final_cycle = int(
        np.floor(
            (duration_ms - aligned.marker_phase_ms - aligned.cycle_ms)
            / aligned.cycle_ms
        )
    )
    if final_cycle < first_cycle:
        raise ValueError("capture contains no complete FFT-analysis cycle")
    fft_bin_index = int(round(tone_offset_hz * fft_size / sample_rate_hz)) % fft_size
    bin_frequency_hz = float(np.fft.fftfreq(fft_size, d=1.0 / sample_rate_hz)[fft_bin_index])
    window = np.hanning(fft_size).astype(np.float64)
    cycle_deltas: list[list[complex]] = []
    schedule_scale = aligned.cycle_ms / profile.nominal_cycle_ms

    for cycle_id in range(first_cycle, final_cycle + 1):
        cycle_start_ms = aligned.marker_phase_ms + cycle_id * aligned.cycle_ms
        cursor_ms = cycle_start_ms + (
            profile.marker_body_ms + profile.guard_ms
        ) * schedule_scale
        deltas = []
        for state in profile.states:
            center_ms = cursor_ms + state.dwell_ms * schedule_scale / 2.0
            center_sample = round(center_ms * sample_rate_hz / 1000.0)
            fft_transfer = _fft_transfer_at_center(
                reference,
                measurement,
                center_sample=center_sample,
                fft_size=fft_size,
                fft_bin_index=fft_bin_index,
                window=window,
            )
            local_baseline = complex(
                float(np.interp(center_ms, times_ms, leakage_baseline.real)),
                float(np.interp(center_ms, times_ms, leakage_baseline.imag)),
            )
            deltas.append(fft_transfer - local_baseline)
            cursor_ms += (state.dwell_ms + profile.guard_ms) * schedule_scale
        cycle_deltas.append(deltas)

    values = np.asarray(cycle_deltas, dtype=np.complex128)
    robust = np.median(values.real, axis=0) + 1j * np.median(values.imag, axis=0)
    amplitudes = np.abs(robust)
    strongest = max(float(np.max(amplitudes)), np.finfo(np.float64).tiny)
    reference_index = next(
        index for index, state in enumerate(profile.states) if state.name == reference_state
    )
    reference_delta = complex(robust[reference_index])
    if abs(reference_delta) <= strongest * 1e-9:
        raise ValueError("FFT reference state is too weak for relative phase")

    estimates = []
    for index, state in enumerate(profile.states):
        state_values = values[:, index]
        state_center = complex(robust[index])
        if abs(state_center) <= strongest * 1e-12:
            coherence = 0.0
            phase_std_deg = 180.0
        else:
            unit = state_values / np.maximum(
                np.abs(state_values), np.finfo(np.float64).tiny
            )
            coherence = float(np.clip(abs(np.mean(unit)), 0.0, 1.0))
            residual = np.angle(state_values * np.conj(state_center))
            phase_std_deg = float(np.sqrt(np.mean(residual**2)) * 180.0 / pi)
        relative = state_center * np.conj(reference_delta)
        estimates.append(
            FftPhaseStateEstimate(
                name=state.name,
                complex_delta=state_center,
                amplitude=float(amplitudes[index]),
                relative_db=20.0
                * log10(max(float(amplitudes[index]), strongest * 1e-12) / strongest),
                phase_deg=float(atan2(relative.imag, relative.real) * 180.0 / pi),
                cycle_phase_std_deg=phase_std_deg,
                cycle_coherence=coherence,
                cycle_count=values.shape[0],
            )
        )

    return GuardedFftPhaseAnalysis(
        cycle_ms=aligned.cycle_ms,
        marker_phase_ms=aligned.marker_phase_ms,
        complete_cycle_count=values.shape[0],
        fft_size=fft_size,
        fft_bin_index=fft_bin_index,
        fft_bin_frequency_hz=bin_frequency_hz,
        requested_tone_offset_hz=float(tone_offset_hz),
        reference_state=reference_state,
        alignment_confidence=alignment_confidence,
        continuity_verified=continuity_verified,
        continuity_block_count=continuity_block_count,
        states=tuple(estimates),
    )
