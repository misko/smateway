"""Autonomous C6 schedule decoding and coherent timing analysis."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.tracking.bearing import solve_bearing, wrap_degrees


@dataclass(frozen=True, slots=True)
class FastTrackingProfile:
    profile_id: str
    ports: tuple[str, ...]
    dwell_us: int
    guard_us: int
    marker_body_us: int
    cycle_us: int

    @classmethod
    def load(cls, path: Path) -> FastTrackingProfile:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("fast tracking profile must be an object")
        identity = value.get("profile")
        frame = value.get("frame")
        states = value.get("states")
        if not isinstance(identity, dict) or not isinstance(frame, dict):
            raise ValueError("fast tracking profile identity/frame is malformed")
        if not isinstance(states, list) or len(states) != 6:
            raise ValueError("fast tracking profile must contain six states")
        dwell_values = {item.get("dwell_us") for item in states if isinstance(item, dict)}
        if len(dwell_values) != 1:
            raise ValueError("fast tracking profile does not use equal dwells")
        dwell_us = next(iter(dwell_values))
        marker = frame.get("marker")
        if (
            not isinstance(dwell_us, int)
            or not isinstance(marker, dict)
            or not isinstance(frame.get("all_off_guard_us"), int)
            or not isinstance(marker.get("body_nominal_us"), int)
            or not isinstance(frame.get("nominal_cycle_us"), int)
        ):
            raise ValueError("fast tracking profile timing is malformed")
        ports = tuple(str(item.get("name")) for item in states if isinstance(item, dict))
        expected_cycle = marker["body_nominal_us"] + len(ports) * (
            frame["all_off_guard_us"] + dwell_us
        )
        if frame["nominal_cycle_us"] != expected_cycle:
            raise ValueError("fast tracking profile cycle is inconsistent")
        return cls(
            profile_id=str(identity.get("id")),
            ports=ports,
            dwell_us=dwell_us,
            guard_us=frame["all_off_guard_us"],
            marker_body_us=marker["body_nominal_us"],
            cycle_us=frame["nominal_cycle_us"],
        )

    @property
    def observable_marker_us(self) -> int:
        return self.marker_body_us + self.guard_us


@dataclass(frozen=True, slots=True)
class FastDwellInterval:
    frame_index: int
    port: str
    start: int
    stop: int
    predicted_start: float
    predicted_stop: float


@dataclass(frozen=True, slots=True)
class FastScheduleDecode:
    sample_rate_hz: int
    decode_method: str
    threshold_log_amplitude: float | None
    low_center_log_amplitude: float | None
    high_center_log_amplitude: float | None
    periodicity_frequency_hz: float | None
    periodicity_score: float | None
    alignment_score: float | None
    marker_end_bins: tuple[int, ...]
    cycle_scale_median: float
    cycle_scale_minimum: float
    cycle_scale_maximum: float
    intervals: tuple[FastDwellInterval, ...]


@dataclass(frozen=True, slots=True)
class FrequencyDifferenceEstimate:
    nominal_frequency_difference_hz: float
    frequency_difference_hz: float
    difference_error_hz: float
    search_objective: float
    dwell_count: int


def _complex_vector(value: npt.ArrayLike, label: str) -> npt.NDArray[np.complex128]:
    result = np.asarray(value)
    if result.ndim != 1 or not np.iscomplexobj(result) or result.size < 1:
        raise ValueError(f"{label} must be a nonempty one-dimensional complex vector")
    if not np.all(np.isfinite(result.real)) or not np.all(np.isfinite(result.imag)):
        raise ValueError(f"{label} must contain finite samples")
    return np.asarray(result, dtype=np.complex128)


def _two_cluster_threshold(values: npt.NDArray[np.float64]) -> tuple[float, float, float]:
    low, high = (float(item) for item in np.percentile(values, (20.0, 80.0)))
    if not high > low:
        raise ValueError("RF envelope has no amplitude contrast")
    for _ in range(32):
        threshold = 0.5 * (low + high)
        low_values = values[values <= threshold]
        high_values = values[values > threshold]
        if not low_values.size or not high_values.size:
            raise ValueError("RF envelope cannot be separated into active and ALL_OFF states")
        new_low = float(np.mean(low_values))
        new_high = float(np.mean(high_values))
        if max(abs(new_low - low), abs(new_high - high)) < 1e-9:
            low, high = new_low, new_high
            break
        low, high = new_low, new_high
    if high - low < math.log(10.0) / 20.0 * 6.0:
        raise ValueError("RF envelope active/ALL_OFF contrast is below 6 dB")
    return 0.5 * (low + high), low, high


def _runs(mask: npt.NDArray[np.bool_]) -> tuple[tuple[bool, int, int], ...]:
    boundaries = np.concatenate(
        (
            np.asarray([0]),
            np.flatnonzero(np.diff(mask.astype(np.int8))) + 1,
            np.asarray([mask.size]),
        )
    )
    return tuple(
        (bool(mask[start]), int(start), int(stop))
        for start, stop in zip(boundaries, boundaries[1:], strict=False)
    )


def _longest_marker_chain(candidates: list[int], nominal_cycle: float) -> list[int]:
    best: list[int] = []
    for first in range(len(candidates)):
        chain = [candidates[first]]
        cursor = candidates[first]
        for candidate in candidates[first + 1 :]:
            distance = candidate - cursor
            if 0.92 * nominal_cycle <= distance <= 1.08 * nominal_cycle:
                chain.append(candidate)
                cursor = candidate
            elif distance > 1.08 * nominal_cycle:
                break
        if len(chain) > len(best):
            best = chain
    return best


def _parabolic_frequency(
    magnitude: npt.NDArray[np.float64], index: int, bin_hz: float
) -> float:
    if index <= 0 or index + 1 >= magnitude.size:
        return index * bin_hz
    points = np.log(np.maximum(magnitude[index - 1 : index + 2], np.finfo(float).tiny))
    denominator = float(points[0] - 2.0 * points[1] + points[2])
    correction = (
        0.0 if abs(denominator) < 1e-15 else 0.5 * float(points[0] - points[2]) / denominator
    )
    return (index + float(np.clip(correction, -0.5, 0.5))) * bin_hz


def _weighted_median(values: npt.NDArray[np.float64], weights: npt.NDArray[np.float64]) -> float:
    order = np.argsort(values)
    ordered_values = values[order]
    ordered_weights = weights[order]
    crossing = np.searchsorted(np.cumsum(ordered_weights), 0.5 * np.sum(ordered_weights))
    return float(ordered_values[min(int(crossing), ordered_values.size - 1)])


def _cyclostationary_decode(
    smoothed: npt.NDArray[np.complex128],
    *,
    sample_rate_hz: int,
    samples_per_bin: int,
    profile: FastTrackingProfile,
    edge_trim_us: float,
    threshold: float | None,
    low: float | None,
    high: float | None,
) -> FastScheduleDecode:
    """Recover a periodic six-state schedule when ALL_OFF is not visibly dark."""

    centered = smoothed - np.mean(smoothed)
    sample_count = centered.size
    padded_count = 1 << max(1, (4 * sample_count - 1).bit_length())
    spectrum = np.fft.fft(centered, n=padded_count)
    magnitude = np.abs(spectrum)
    bin_hz = 1_000_000.0 / padded_count
    nominal_hz = 1_000_000.0 / profile.cycle_us
    energy_scale = math.sqrt(
        sample_count * float(np.sum(np.abs(centered) ** 2))
    )
    estimates: list[float] = []
    weights: list[float] = []
    coherences: list[float] = []
    for harmonic in range(1, 7):
        lower = harmonic * nominal_hz / 1.05
        upper = harmonic * nominal_hz / 0.95
        first = max(1, math.ceil(lower / bin_hz))
        stop = min(padded_count // 2, math.floor(upper / bin_hz) + 1)
        if stop <= first:
            continue
        peak = first + int(np.argmax(magnitude[first:stop]))
        frequency_hz = _parabolic_frequency(magnitude, peak, bin_hz)
        coherence = float(magnitude[peak] / max(energy_scale, np.finfo(float).tiny))
        estimates.append(frequency_hz / harmonic)
        weights.append(max(coherence, np.finfo(float).tiny) ** 2)
        coherences.append(coherence)
    if len(estimates) < 3:
        raise ValueError("autonomous C6 periodicity has fewer than three observable harmonics")
    estimate_vector = np.asarray(estimates, dtype=np.float64)
    weight_vector = np.asarray(weights, dtype=np.float64)
    provisional = _weighted_median(estimate_vector, weight_vector)
    consistent = np.abs(estimate_vector - provisional) <= 0.005 * provisional
    if np.count_nonzero(consistent) < 3:
        raise ValueError("autonomous C6 harmonic frequency estimates are inconsistent")
    cycle_frequency_hz = float(
        np.average(estimate_vector[consistent], weights=weight_vector[consistent])
    )
    cycle_scale = 1_000_000.0 / (cycle_frequency_hz * profile.cycle_us)
    if not 0.95 <= cycle_scale <= 1.05:
        raise ValueError("decoded selector clock is outside the conservative ±5% window")
    periodicity_score = float(max(coherences))
    if periodicity_score < 0.003:
        raise ValueError("autonomous C6 periodicity is below the coherent detection gate")

    cycle_bins = profile.cycle_us
    phase = np.floor(
        np.mod(np.arange(sample_count, dtype=np.float64) * cycle_frequency_hz / 1e6, 1.0)
        * cycle_bins
    ).astype(np.int32)
    counts = np.bincount(phase, minlength=cycle_bins)
    if np.any(counts == 0):
        raise ValueError("autonomous C6 fold has an empty phase bin")
    folded = (
        np.bincount(phase, weights=smoothed.real, minlength=cycle_bins)
        + 1j * np.bincount(phase, weights=smoothed.imag, minlength=cycle_bins)
    ) / counts
    inner_margin = max(3, math.ceil(edge_trim_us))
    inner_length = profile.dwell_us - 2 * inner_margin
    if inner_length < 4:
        raise ValueError("autonomous dwell is too short for coherent alignment")
    scores: list[float] = []
    for marker_end in range(cycle_bins):
        residual_energy = 0.0
        total_energy = 0.0
        for port_index in range(len(profile.ports)):
            start = (
                marker_end
                + port_index * (profile.dwell_us + profile.guard_us)
                + inner_margin
            )
            indices = np.arange(start, start + inner_length) % cycle_bins
            segment = folded[indices]
            mean = complex(np.mean(segment))
            residual_energy += float(np.sum(np.abs(segment - mean) ** 2))
            total_energy += float(np.sum(np.abs(segment) ** 2))
        marker_length = profile.observable_marker_us - 2 * inner_margin
        marker_indices = (
            np.arange(marker_end - profile.observable_marker_us + inner_margin,
                      marker_end - inner_margin)
            % cycle_bins
        )
        marker_segment = folded[marker_indices]
        marker_mean = complex(np.mean(marker_segment))
        residual_energy += float(np.sum(np.abs(marker_segment - marker_mean) ** 2))
        total_energy += float(np.sum(np.abs(marker_segment) ** 2))
        if marker_indices.size != marker_length:
            raise AssertionError("marker alignment window length changed")
        scores.append(
            1.0
            - residual_energy / max(total_energy, np.finfo(float).tiny)
        )
    marker_phase = int(np.argmax(scores))
    alignment_score = float(scores[marker_phase])
    if alignment_score < 0.25:
        raise ValueError("autonomous C6 six-plateau alignment is below the admission gate")

    marker_fraction = marker_phase / cycle_bins
    first_cycle = math.ceil(-marker_fraction)
    final_cycle = math.floor(
        sample_count * cycle_frequency_hz / 1e6 - marker_fraction
    )
    markers = [
        round((cycle + marker_fraction) * 1e6 / cycle_frequency_hz)
        for cycle in range(first_cycle, final_cycle + 1)
    ]
    markers = [value for value in markers if 0 <= value <= sample_count]
    if len(markers) < 9:
        raise ValueError("fewer than eight complete autonomous C6 frames are visible")

    intervals: list[FastDwellInterval] = []
    trim_samples = edge_trim_us * sample_rate_hz / 1e6
    for frame_index, (left, right) in enumerate(zip(markers, markers[1:], strict=False)):
        scale = (right - left) / profile.cycle_us
        for port_index, port in enumerate(profile.ports):
            predicted_start_bin = left + port_index * (
                profile.dwell_us + profile.guard_us
            ) * scale
            predicted_stop_bin = predicted_start_bin + profile.dwell_us * scale
            predicted_start = predicted_start_bin * samples_per_bin
            predicted_stop = predicted_stop_bin * samples_per_bin
            start = math.ceil(predicted_start + trim_samples)
            stop = math.floor(predicted_stop - trim_samples)
            if stop <= start or start < 0 or stop > sample_count * samples_per_bin:
                raise ValueError("trimmed autonomous dwell has no samples")
            intervals.append(
                FastDwellInterval(
                    frame_index=frame_index,
                    port=port,
                    start=start,
                    stop=stop,
                    predicted_start=predicted_start,
                    predicted_stop=predicted_stop,
                )
            )
    observed_scales = np.diff(markers) / profile.cycle_us
    return FastScheduleDecode(
        sample_rate_hz=sample_rate_hz,
        decode_method="cyclostationary_coherent_fold",
        threshold_log_amplitude=threshold,
        low_center_log_amplitude=low,
        high_center_log_amplitude=high,
        periodicity_frequency_hz=cycle_frequency_hz,
        periodicity_score=periodicity_score,
        alignment_score=alignment_score,
        marker_end_bins=tuple(markers),
        cycle_scale_median=float(np.median(observed_scales)),
        cycle_scale_minimum=float(np.min(observed_scales)),
        cycle_scale_maximum=float(np.max(observed_scales)),
        intervals=tuple(intervals),
    )


def decode_fast_schedule(
    rx2: npt.ArrayLike,
    *,
    sample_rate_hz: int,
    tone_offset_hz: float,
    profile: FastTrackingProfile,
    edge_trim_us: float,
) -> FastScheduleDecode:
    """Decode marker ends and produce trimmed active intervals from RF alone."""

    samples = _complex_vector(rx2, "RX2 samples")
    if sample_rate_hz < 1_000_000 or sample_rate_hz % 1_000_000:
        raise ValueError("sample rate must be an integer multiple of 1 MS/s")
    if not 0.0 <= edge_trim_us < profile.dwell_us / 2.0:
        raise ValueError("edge trim must be nonnegative and shorter than half a dwell")
    samples_per_bin = sample_rate_hz // 1_000_000
    usable = samples.size // samples_per_bin * samples_per_bin
    indices = np.arange(usable, dtype=np.float64)
    mixed = samples[:usable] * np.exp(
        -2j * np.pi * tone_offset_hz * indices / sample_rate_hz
    )
    one_us = mixed.reshape(-1, samples_per_bin).mean(axis=1)
    # Five microseconds is one full period of the 200 kHz TX1/TX2 tone
    # separation, so the marker detector rejects the other coherent source.
    smoothed = np.asarray(
        np.convolve(one_us, np.ones(5) / 5.0, mode="same"), dtype=np.complex128
    )
    envelope = np.abs(smoothed)
    log_envelope = np.log(np.maximum(envelope, np.finfo(float).tiny))
    threshold: float | None = None
    low: float | None = None
    high: float | None = None
    chain: list[int] = []
    try:
        threshold, low, high = _two_cluster_threshold(log_envelope)
        active = log_envelope > threshold
        majority = np.convolve(
            active.astype(np.int8), np.ones(3, dtype=np.int8), mode="same"
        )
        active = majority >= 2
        marker_minimum = 0.70 * profile.observable_marker_us
        marker_maximum = 1.30 * profile.observable_marker_us
        candidates = [
            stop
            for state, start, stop in _runs(active)
            if not state
            and marker_minimum <= stop - start <= marker_maximum
            and stop < active.size
            and active[stop]
        ]
        chain = _longest_marker_chain(candidates, float(profile.cycle_us))
    except ValueError:
        pass
    if len(chain) < 8:
        return _cyclostationary_decode(
            smoothed,
            sample_rate_hz=sample_rate_hz,
            samples_per_bin=samples_per_bin,
            profile=profile,
            edge_trim_us=edge_trim_us,
            threshold=threshold,
            low=low,
            high=high,
        )
    scales = np.diff(chain) / profile.cycle_us
    median_scale = float(np.median(scales))
    if not 0.95 <= median_scale <= 1.05:
        raise ValueError("decoded selector clock is outside the conservative ±5% window")
    admitted_markers = [chain[0]]
    for marker in chain[1:]:
        local_scale = (marker - admitted_markers[-1]) / profile.cycle_us
        if 0.98 * median_scale <= local_scale <= 1.02 * median_scale:
            admitted_markers.append(marker)
        else:
            break
    if len(admitted_markers) < 8:
        raise ValueError("selector marker chain is not locally continuous")
    intervals: list[FastDwellInterval] = []
    trim_samples = edge_trim_us * sample_rate_hz / 1e6
    for frame_index, (left, right) in enumerate(
        zip(admitted_markers, admitted_markers[1:], strict=False)
    ):
        scale = (right - left) / profile.cycle_us
        for port_index, port in enumerate(profile.ports):
            predicted_start_bin = left + port_index * (profile.dwell_us + profile.guard_us) * scale
            predicted_stop_bin = predicted_start_bin + profile.dwell_us * scale
            predicted_start = predicted_start_bin * samples_per_bin
            predicted_stop = predicted_stop_bin * samples_per_bin
            start = math.ceil(predicted_start + trim_samples)
            stop = math.floor(predicted_stop - trim_samples)
            if stop <= start or start < 0 or stop > samples.size:
                raise ValueError("trimmed autonomous dwell has no samples")
            intervals.append(
                FastDwellInterval(
                    frame_index=frame_index,
                    port=port,
                    start=start,
                    stop=stop,
                    predicted_start=predicted_start,
                    predicted_stop=predicted_stop,
                )
            )
    return FastScheduleDecode(
        sample_rate_hz=sample_rate_hz,
        decode_method="threshold_marker_chain",
        threshold_log_amplitude=threshold,
        low_center_log_amplitude=low,
        high_center_log_amplitude=high,
        periodicity_frequency_hz=None,
        periodicity_score=None,
        alignment_score=None,
        marker_end_bins=tuple(admitted_markers),
        cycle_scale_median=median_scale,
        cycle_scale_minimum=float(np.min(np.diff(admitted_markers) / profile.cycle_us)),
        cycle_scale_maximum=float(np.max(np.diff(admitted_markers) / profile.cycle_us)),
        intervals=tuple(intervals),
    )


def coherent_product(
    rx1: npt.ArrayLike,
    rx2: npt.ArrayLike,
    *,
    sample_rate_hz: int,
    first_sample_sequence: int,
    difference_hz: float | None,
) -> npt.NDArray[np.complex128]:
    reference = _complex_vector(rx1, "RX1 samples")
    selected = _complex_vector(rx2, "RX2 samples")
    if reference.size != selected.size:
        raise ValueError("RX1 and RX2 sample counts differ")
    product = selected * np.conjugate(reference)
    if difference_hz is not None:
        indices = first_sample_sequence + np.arange(product.size, dtype=np.float64)
        product *= np.exp(-2j * np.pi * difference_hz * indices / sample_rate_hz)
    return product


def estimate_frequency_difference(
    rx1: npt.ArrayLike,
    rx2: npt.ArrayLike,
    *,
    decode: FastScheduleDecode,
    sample_rate_hz: int,
    nominal_difference_hz: float,
) -> FrequencyDifferenceEstimate:
    """Fit cross-frequency rotation while retaining one intercept per port."""

    reference = _complex_vector(rx1, "RX1 samples")
    target = _complex_vector(rx2, "RX2 samples")
    if reference.size != target.size:
        raise ValueError("RX1 and RX2 sample counts differ")

    nominal_step = 2.0 * np.pi * nominal_difference_hz / sample_rate_hz
    by_port: dict[str, list[tuple[float, complex]]] = {}
    for interval in decode.intervals:
        indices = np.arange(interval.start, interval.stop, dtype=np.float64)
        product = (
            target[interval.start : interval.stop]
            * np.conjugate(reference[interval.start : interval.stop])
            * np.exp(-1j * nominal_step * indices)
        )
        center_s = (interval.start + interval.stop - 1) / (2.0 * sample_rate_hz)
        by_port.setdefault(interval.port, []).append((center_s, complex(np.mean(product))))
    if set(by_port) != set(interval.port for interval in decode.intervals):
        raise ValueError("frequency estimator is missing a decoded port")
    origin_s = min(item[0] for values in by_port.values() for item in values)

    def objective(error_hz: float) -> float:
        scores = []
        for observations in by_port.values():
            times = np.asarray([item[0] - origin_s for item in observations])
            phasors = np.asarray([item[1] for item in observations])
            coherent = np.sum(phasors * np.exp(-2j * np.pi * error_hz * times))
            energy = float(np.sum(np.abs(phasors) ** 2))
            scores.append(
                float(abs(coherent) ** 2 / max(phasors.size * energy, np.finfo(float).tiny))
            )
        return float(np.mean(scores))

    center_hz = 0.0
    half_span_hz = 200.0
    best_score = -np.inf
    for point_count in (801, 201, 201):
        candidates = np.linspace(center_hz - half_span_hz, center_hz + half_span_hz, point_count)
        scores = np.asarray([objective(float(candidate)) for candidate in candidates])
        best_index = int(np.argmax(scores))
        center_hz = float(candidates[best_index])
        best_score = float(scores[best_index])
        step_hz = float(candidates[1] - candidates[0])
        half_span_hz = 2.0 * step_hz
    if abs(center_hz) >= 199.0:
        raise ValueError("frequency-difference estimate reached its search boundary")
    return FrequencyDifferenceEstimate(
        nominal_frequency_difference_hz=nominal_difference_hz,
        frequency_difference_hz=nominal_difference_hz + center_hz,
        difference_error_hz=center_hz,
        search_objective=best_score,
        dwell_count=len(decode.intervals),
    )


def _phase_rms_deg(values: npt.NDArray[np.complex128], reference: complex) -> float:
    return float(np.sqrt(np.mean(np.angle(values / reference, deg=True) ** 2)))


def _settling_study(
    values: npt.NDArray[np.complex128],
    *,
    decode: FastScheduleDecode,
    profile: FastTrackingProfile,
    references: dict[str, complex],
    observable_ports: tuple[str, ...],
) -> tuple[list[dict[str, Any]], float | None]:
    samples_per_us = decode.sample_rate_hz / 1e6
    window_us = 5
    maximum_age_us = min(25, profile.dwell_us - window_us)
    rows: list[dict[str, Any]] = []
    for age_us in range(maximum_age_us):
        per_port_means: list[complex] = []
        individual_normalized: list[complex] = []
        per_port_normalized: dict[str, list[complex]] = {
            port: [] for port in observable_ports
        }
        for interval in decode.intervals:
            if interval.port not in per_port_normalized:
                continue
            start = round(interval.predicted_start + age_us * samples_per_us)
            stop = round(
                interval.predicted_start
                + (age_us + window_us) * samples_per_us
            )
            if start < 0 or stop > values.size or stop <= start:
                continue
            normalized = complex(np.mean(values[start:stop])) / references[interval.port]
            per_port_normalized[interval.port].append(normalized)
            individual_normalized.append(normalized)
        for port in observable_ports:
            if not per_port_normalized[port]:
                raise ValueError("settling study is missing a C6 port")
            per_port_means.append(complex(np.mean(per_port_normalized[port])))
        port_vector = np.asarray(per_port_means, dtype=np.complex128)
        individual = np.asarray(individual_normalized, dtype=np.complex128)
        port_phase = np.angle(port_vector, deg=True)
        port_gain_db = 20.0 * np.log10(np.maximum(np.abs(port_vector), np.finfo(float).tiny))
        rows.append(
            {
                "age_after_selected_edge_us": age_us,
                "ensemble_phase_rms_deg": float(np.sqrt(np.mean(port_phase**2))),
                "ensemble_absolute_phase_max_deg": float(np.max(np.abs(port_phase))),
                "ensemble_gain_error_rms_db": float(np.sqrt(np.mean(port_gain_db**2))),
                "ensemble_absolute_gain_error_max_db": float(np.max(np.abs(port_gain_db))),
                "single_transition_phase_rms_deg": float(
                    np.sqrt(np.mean(np.angle(individual, deg=True) ** 2))
                ),
                "transition_count": int(individual.size),
            }
        )
    settled_age: float | None = None
    for index, row in enumerate(rows):
        tail = rows[index:]
        if all(
            item["ensemble_phase_rms_deg"] <= 5.0
            and item["ensemble_absolute_phase_max_deg"] <= 10.0
            and item["ensemble_gain_error_rms_db"] <= 1.0
            and item["ensemble_absolute_gain_error_max_db"] <= 2.0
            for item in tail
        ):
            settled_age = float(row["age_after_selected_edge_us"] + window_us)
            break
    return rows, settled_age


def analyze_fast_phase(
    product: npt.ArrayLike,
    *,
    decode: FastScheduleDecode,
    profile: FastTrackingProfile,
    grouping_cycles: tuple[int, ...] = (
        1,
        2,
        4,
        8,
        16,
        32,
        64,
        128,
        256,
        512,
        1024,
        2048,
    ),
) -> dict[str, Any]:
    """Measure phase repeatability as active and wall-clock integration grow."""

    values = _complex_vector(product, "coherent product")
    by_port: dict[str, list[complex]] = {port: [] for port in profile.ports}
    sample_counts: list[int] = []
    for interval in decode.intervals:
        selected = values[interval.start : interval.stop]
        by_port[interval.port].append(complex(np.mean(selected)))
        sample_counts.append(selected.size)
    references = {
        port: complex(np.mean(np.asarray(port_values, dtype=np.complex128)))
        for port, port_values in by_port.items()
    }
    maximum_reference_magnitude = max(abs(value) for value in references.values())
    observable_ports = tuple(
        port
        for port in profile.ports
        if abs(references[port]) >= 0.1 * maximum_reference_magnitude
    )
    if len(observable_ports) < 4:
        raise ValueError("fewer than four C6 ports have observable full-capture signal")
    single_visit_snr_db = {}
    for port, port_values in by_port.items():
        vector = np.asarray(port_values, dtype=np.complex128)
        residual = float(np.sqrt(np.mean(np.abs(vector - references[port]) ** 2)))
        single_visit_snr_db[port] = float(
            20.0
            * np.log10(
                max(abs(references[port]), np.finfo(float).tiny)
                / max(residual, np.finfo(float).tiny)
            )
        )
    studies: list[dict[str, Any]] = []
    for cycles in grouping_cycles:
        per_port_rms: dict[str, float] = {}
        group_count = math.inf
        for port, port_values in by_port.items():
            vector = np.asarray(port_values, dtype=np.complex128)
            usable = vector.size // cycles * cycles
            if not usable:
                continue
            groups = vector[:usable].reshape(-1, cycles).mean(axis=1)
            per_port_rms[port] = _phase_rms_deg(groups, references[port])
            group_count = min(group_count, groups.size)
        if len(per_port_rms) != len(profile.ports):
            continue
        rms_values = np.asarray(tuple(per_port_rms.values()))
        observable_rms = np.asarray(
            [per_port_rms[port] for port in observable_ports], dtype=np.float64
        )
        reference_weights = np.asarray(
            [abs(references[port]) ** 2 for port in profile.ports], dtype=np.float64
        )
        weighted_rms = math.sqrt(
            float(
                np.average(
                    np.square([per_port_rms[port] for port in profile.ports]),
                    weights=reference_weights,
                )
            )
        )
        studies.append(
            {
                "cycles_averaged": cycles,
                "groups_per_port": int(group_count),
                "nominal_active_integration_ms": (
                    cycles * float(np.median(sample_counts)) / decode.sample_rate_hz * 1000.0
                ),
                "nominal_wall_latency_ms": cycles * profile.cycle_us / 1000.0,
                "measured_wall_latency_ms": (
                    cycles
                    * profile.cycle_us
                    * decode.cycle_scale_median
                    / 1000.0
                ),
                "phase_rms_deg_median_port": float(np.median(rms_values)),
                "phase_rms_deg_maximum_port": float(np.max(rms_values)),
                "phase_rms_deg_maximum_observable_port": float(
                    np.max(observable_rms)
                ),
                "phase_rms_deg_power_weighted_ports": weighted_rms,
                "per_port_phase_rms_deg": per_port_rms,
            }
        )
    settling, settled_age_us = _settling_study(
        values,
        decode=decode,
        profile=profile,
        references=references,
        observable_ports=observable_ports,
    )
    return {
        "frame_count": len(decode.marker_end_bins) - 1,
        "interval_count": len(decode.intervals),
        "samples_per_trimmed_dwell": {
            "minimum": min(sample_counts),
            "median": float(np.median(sample_counts)),
            "maximum": max(sample_counts),
        },
        "per_port_reference": {
            port: {"real": value.real, "imag": value.imag}
            for port, value in references.items()
        },
        "observable_ports": list(observable_ports),
        "observability_rule": (
            "full-capture coherent magnitude at least 10% of the strongest C6 port"
        ),
        "per_port_single_visit_coherent_snr_db": single_visit_snr_db,
        "integration_study": studies,
        "settling_study": settling,
        "settled_after_selected_edge_us": settled_age_us,
        "settling_acceptance": {
            "ensemble_phase_rms_deg_maximum": 5.0,
            "ensemble_absolute_phase_max_deg_maximum": 10.0,
            "ensemble_gain_error_rms_db_maximum": 1.0,
            "ensemble_absolute_gain_error_max_db_maximum": 2.0,
            "resolution_us": 1.0,
            "measurement_window_us": 5.0,
            "scope": "RF-visible response including receiver bandwidth; not GPIO-only timing",
        },
    }


def analyze_fast_bearings(
    product: npt.ArrayLike,
    *,
    decode: FastScheduleDecode,
    profile: FastTrackingProfile,
    calibration_coefficients: npt.ArrayLike,
    steering: npt.ArrayLike,
    bearings_deg: npt.ArrayLike,
    expected_bearing_deg: float | None,
    grouping_cycles: tuple[int, ...] = (
        1,
        2,
        4,
        8,
        16,
        32,
        64,
        128,
        256,
        512,
        1024,
        2048,
    ),
) -> dict[str, Any]:
    """Solve per-group ideal-manifold bearings and report repeatability."""

    values = _complex_vector(product, "coherent product")
    coefficients = np.asarray(calibration_coefficients, dtype=np.complex128)
    if coefficients.shape != (len(profile.ports),):
        raise ValueError("calibration coefficients disagree with C6 ports")
    frame_count = len(decode.marker_end_bins) - 1
    matrix = np.empty((frame_count, len(profile.ports)), dtype=np.complex128)
    matrix.fill(np.nan + 1j * np.nan)
    port_index = {port: index for index, port in enumerate(profile.ports)}
    for interval in decode.intervals:
        matrix[interval.frame_index, port_index[interval.port]] = np.mean(
            values[interval.start : interval.stop]
        )
    if not np.all(np.isfinite(matrix.real)) or not np.all(np.isfinite(matrix.imag)):
        raise ValueError("bearing matrix has a missing frame/port dwell")
    reference = solve_bearing(
        np.mean(matrix, axis=0) * coefficients,
        steering,
        bearings_deg,
        minimum_score=0.5,
        minimum_ambiguity_margin_db=1.0,
        maximum_residual_phase_rms_deg=45.0,
    )
    study: list[dict[str, Any]] = []
    for cycles in grouping_cycles:
        usable = frame_count // cycles * cycles
        if not usable:
            continue
        grouped = matrix[:usable].reshape(-1, cycles, len(profile.ports)).mean(axis=1)
        estimates = [
            solve_bearing(
                snapshot * coefficients,
                steering,
                bearings_deg,
                minimum_score=0.5,
                minimum_ambiguity_margin_db=1.0,
                maximum_residual_phase_rms_deg=45.0,
            )
            for snapshot in grouped
        ]
        bearing = np.asarray([item.bearing_deg for item in estimates])
        repeat_error = wrap_degrees(bearing - reference.bearing_deg)
        absolute_error = (
            None
            if expected_bearing_deg is None
            else wrap_degrees(bearing - expected_bearing_deg)
        )
        study.append(
            {
                "cycles_averaged": cycles,
                "group_count": len(estimates),
                "nominal_wall_latency_ms": cycles * profile.cycle_us / 1000.0,
                "measured_wall_latency_ms": (
                    cycles
                    * profile.cycle_us
                    * decode.cycle_scale_median
                    / 1000.0
                ),
                "bearing_repeatability_rms_deg": float(
                    np.sqrt(np.mean(repeat_error**2))
                ),
                "bearing_absolute_error_rms_deg": (
                    None
                    if absolute_error is None
                    else float(np.sqrt(np.mean(absolute_error**2)))
                ),
                "valid_percent": float(100.0 * np.mean([item.valid for item in estimates])),
                "score_median": float(np.median([item.score for item in estimates])),
                "ambiguity_margin_db_p05": float(
                    np.percentile([item.ambiguity_margin_db for item in estimates], 5.0)
                ),
                "residual_phase_rms_deg_p95": float(
                    np.percentile([item.residual_phase_rms_deg for item in estimates], 95.0)
                ),
            }
        )
    return {
        "expected_bearing_deg_approximate": expected_bearing_deg,
        "full_capture_reference": {
            "bearing_deg": reference.bearing_deg,
            "valid": reference.valid,
            "score": reference.score,
            "ambiguity_margin_db": reference.ambiguity_margin_db,
            "residual_phase_rms_deg": reference.residual_phase_rms_deg,
            "reasons": list(reference.reasons),
        },
        "integration_study": study,
    }
