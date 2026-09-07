"""Coherent channel estimators for referenced and continuous switched captures."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class TransferEstimate:
    """Least-squares switched/reference complex transfer and quality."""

    value: complex
    coherence: float
    residual_snr_db: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class DwellInterval:
    """One selector state's half-open interval in a continuous sample stream."""

    port: str
    start: int
    stop: int


@dataclass(frozen=True, slots=True)
class DwellPhasor:
    """One tone phasor referred to the stream's global FPGA sample epoch."""

    port: str
    center_sample_sequence: float
    value: complex
    coherence: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class ToneFrequencyEstimate:
    """Common continuous-tone frequency estimated without crossing state edges."""

    frequency_hz: float
    adjacent_product_coherence: float
    adjacent_pair_count: int


@dataclass(frozen=True, slots=True)
class CrossFrequencyEstimate:
    """Per-dwell transfers between two coherent, frequency-separated pilots."""

    frequency_difference_hz: float
    frequency_error_hz: float
    search_objective: float
    bin_count: int
    phasors: tuple[DwellPhasor, ...]


def _vectors(
    reference: npt.ArrayLike,
    switched: npt.ArrayLike,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.complex128]]:
    first = np.asarray(reference, dtype=np.complex128)
    second = np.asarray(switched, dtype=np.complex128)
    if first.ndim != 1 or second.shape != first.shape or first.size < 2:
        raise ValueError("channel estimation requires equal one-dimensional sample vectors")
    if not (
        np.all(np.isfinite(first.real))
        and np.all(np.isfinite(first.imag))
        and np.all(np.isfinite(second.real))
        and np.all(np.isfinite(second.imag))
    ):
        raise ValueError("channel samples must be finite")
    return first, second


def estimate_same_emitter_transfer(
    reference: npt.ArrayLike,
    switched: npt.ArrayLike,
    *,
    window: npt.ArrayLike | None = None,
    epsilon: float | None = None,
) -> TransferEstimate:
    """Estimate ``switched/reference`` when both observe the same emitter.

    The source waveform, absolute source phase, and receiver common phase
    cancel. A scalar coherence and projection-residual SNR are retained so a
    weak or unrelated reference cannot be mistaken for a valid phase.
    """

    first, second = _vectors(reference, switched)
    if window is None:
        weights = np.ones(first.size, dtype=np.float64)
    else:
        weights = np.asarray(window, dtype=np.float64)
        if weights.shape != first.shape or not np.all(np.isfinite(weights)):
            raise ValueError("channel-estimation window is invalid")
        if np.any(weights < 0.0) or not np.any(weights > 0.0):
            raise ValueError("channel-estimation window must contain positive weights")
    reference_power = float(np.sum(weights * np.abs(first) ** 2))
    switched_power = float(np.sum(weights * np.abs(second) ** 2))
    floor = (
        np.finfo(np.float64).eps * max(reference_power, 1.0)
        if epsilon is None
        else float(epsilon)
    )
    if not np.isfinite(floor) or floor <= 0.0:
        raise ValueError("channel-estimation epsilon must be positive and finite")
    cross = complex(np.sum(weights * second * np.conjugate(first)))
    value = cross / (reference_power + floor)
    coherence_denominator = np.sqrt(max(reference_power * switched_power, floor**2))
    coherence = float(min(1.0, abs(cross) / coherence_denominator))
    residual = second - value * first
    residual_power = float(np.sum(weights * np.abs(residual) ** 2))
    projected_power = abs(value) ** 2 * reference_power
    residual_snr_db = float(10.0 * np.log10((projected_power + floor) / (residual_power + floor)))
    return TransferEstimate(value, coherence, residual_snr_db, first.size)


def estimate_dwell_phasors(
    samples: npt.ArrayLike,
    intervals: tuple[DwellInterval, ...],
    *,
    first_sample_sequence: int,
    sample_rate_hz: float,
    tone_offset_hz: float,
    edge_discard_samples: int = 0,
) -> tuple[DwellPhasor, ...]:
    """Project selector dwells at a tone using the global sample sequence.

    Global sample phase is the essential difference from independently
    restarted buffers: an arbitrary transmitter phase remains common to every
    returned port and therefore cannot steer the bearing.
    """

    signal = np.asarray(samples, dtype=np.complex128)
    if signal.ndim != 1 or signal.size < 2:
        raise ValueError("dwell projection requires a one-dimensional sample stream")
    if not np.all(np.isfinite(signal.real)) or not np.all(np.isfinite(signal.imag)):
        raise ValueError("dwell samples must be finite")
    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample rate must be positive and finite")
    if not np.isfinite(tone_offset_hz) or abs(tone_offset_hz) >= sample_rate_hz / 2.0:
        raise ValueError("tone offset is outside the complex Nyquist interval")
    if isinstance(first_sample_sequence, bool) or first_sample_sequence < 0:
        raise ValueError("first sample sequence must be a non-negative integer")
    if edge_discard_samples < 0:
        raise ValueError("edge discard must not be negative")

    output: list[DwellPhasor] = []
    for interval in intervals:
        start = interval.start + edge_discard_samples
        stop = interval.stop - edge_discard_samples
        if not interval.port or interval.start < 0 or interval.stop > signal.size or start >= stop:
            raise ValueError(f"invalid dwell interval for {interval.port!r}")
        selected = signal[start:stop]
        local_indices = np.arange(start, stop, dtype=np.float64)
        global_indices = float(first_sample_sequence) + local_indices
        oscillator = np.exp(-2j * np.pi * tone_offset_hz * global_indices / sample_rate_hz)
        baseband = selected * oscillator
        value = complex(np.mean(baseband))
        total_power = float(np.mean(np.abs(baseband) ** 2))
        coherence = float(min(1.0, abs(value) / np.sqrt(max(total_power, np.finfo(float).tiny))))
        output.append(
            DwellPhasor(
                port=interval.port,
                center_sample_sequence=float(first_sample_sequence + (start + stop - 1) / 2.0),
                value=value,
                coherence=coherence,
                sample_count=selected.size,
            )
        )
    return tuple(output)


def estimate_continuous_tone_frequency(
    samples: npt.ArrayLike,
    intervals: tuple[DwellInterval, ...],
    *,
    sample_rate_hz: float,
    nominal_tone_offset_hz: float,
    edge_discard_samples: int = 0,
) -> ToneFrequencyEstimate:
    """Estimate common tone frequency using only adjacent samples within dwells.

    Selector phase steps never enter the adjacent products because each dwell
    is processed independently. Summing the products weights strong paths more
    heavily while preserving one common carrier-frequency estimate.
    """

    signal = np.asarray(samples, dtype=np.complex128)
    if signal.ndim != 1 or signal.size < 2:
        raise ValueError("tone frequency estimation requires a one-dimensional stream")
    if not np.all(np.isfinite(signal.real)) or not np.all(np.isfinite(signal.imag)):
        raise ValueError("tone frequency samples must be finite")
    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample rate must be positive and finite")
    if not np.isfinite(nominal_tone_offset_hz) or abs(nominal_tone_offset_hz) >= (
        sample_rate_hz / 2.0
    ):
        raise ValueError("nominal tone is outside the complex Nyquist interval")
    if edge_discard_samples < 0:
        raise ValueError("edge discard must not be negative")

    accumulated = 0.0j
    normalization = 0.0
    pair_count = 0
    nominal_step = np.exp(-2j * np.pi * nominal_tone_offset_hz / sample_rate_hz)
    for interval in intervals:
        start = interval.start + edge_discard_samples
        stop = interval.stop - edge_discard_samples
        if (
            not interval.port
            or interval.start < 0
            or interval.stop > signal.size
            or stop - start < 2
        ):
            raise ValueError(f"invalid tone-frequency dwell for {interval.port!r}")
        selected = signal[start:stop]
        products = selected[1:] * np.conjugate(selected[:-1]) * nominal_step
        accumulated += complex(np.sum(products))
        normalization += float(np.sum(np.abs(selected[1:]) * np.abs(selected[:-1])))
        pair_count += products.size
    if pair_count == 0 or normalization <= np.finfo(float).tiny:
        raise ValueError("tone-frequency dwells contain no usable energy")
    residual_radians = float(np.angle(accumulated))
    frequency_hz = nominal_tone_offset_hz + (
        residual_radians * sample_rate_hz / (2.0 * np.pi)
    )
    coherence = float(min(1.0, abs(accumulated) / normalization))
    return ToneFrequencyEstimate(frequency_hz, coherence, pair_count)


def estimate_cross_frequency_transfers(
    reference: npt.ArrayLike,
    switched: npt.ArrayLike,
    intervals: tuple[DwellInterval, ...],
    *,
    first_sample_sequence: int,
    sample_rate_hz: float,
    nominal_frequency_difference_hz: float,
    edge_discard_samples: int = 0,
    frequency_search_span_hz: float = 100.0,
    frequency_bin_samples: int = 100,
) -> CrossFrequencyEstimate:
    """Estimate a switched target using a coherent pilot at another frequency.

    ``reference`` contains a pilot from one transmitter channel and ``switched``
    contains the target from another channel sharing the same RF LO and sample
    clock.  Their product cancels arbitrary source/receiver LO phase.  The
    remaining, nearly deterministic tone difference is fitted jointly over all
    selector dwells without ever integrating across a selector edge.

    Frequency separation also rejects pilot leakage into the array and target
    leakage into the reference input.  The returned phasors share one global
    sample epoch, so their relative phases are directly usable for bearing.
    """

    first, second = _vectors(reference, switched)
    if not intervals:
        raise ValueError("cross-frequency estimation requires at least one dwell")
    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample rate must be positive and finite")
    if (
        not np.isfinite(nominal_frequency_difference_hz)
        or abs(nominal_frequency_difference_hz) >= sample_rate_hz / 2.0
    ):
        raise ValueError("nominal frequency difference is outside complex Nyquist")
    if isinstance(first_sample_sequence, bool) or first_sample_sequence < 0:
        raise ValueError("first sample sequence must be a non-negative integer")
    if edge_discard_samples < 0:
        raise ValueError("edge discard must not be negative")
    if (
        not np.isfinite(frequency_search_span_hz)
        or frequency_search_span_hz <= 0.0
        or frequency_search_span_hz >= sample_rate_hz / 4.0
    ):
        raise ValueError("frequency search span is invalid")
    if frequency_bin_samples < 1:
        raise ValueError("frequency bin size must be positive")

    products: list[npt.NDArray[np.complex128]] = []
    bin_times_s: list[npt.NDArray[np.float64]] = []
    nominal_radians_per_sample = (
        2.0 * np.pi * nominal_frequency_difference_hz / sample_rate_hz
    )
    total_bins = 0
    for interval in intervals:
        start = interval.start + edge_discard_samples
        stop = interval.stop - edge_discard_samples
        if (
            not interval.port
            or interval.start < 0
            or interval.stop > first.size
            or start >= stop
        ):
            raise ValueError(f"invalid cross-frequency dwell for {interval.port!r}")
        usable = ((stop - start) // frequency_bin_samples) * frequency_bin_samples
        if usable < frequency_bin_samples:
            raise ValueError(f"cross-frequency dwell is too short for {interval.port!r}")
        stop = start + usable
        global_indices = float(first_sample_sequence) + np.arange(start, stop, dtype=np.float64)
        mixed = (
            second[start:stop]
            * np.conjugate(first[start:stop])
            * np.exp(-1j * nominal_radians_per_sample * global_indices)
        )
        binned = mixed.reshape(-1, frequency_bin_samples).mean(axis=1)
        centers = (
            float(first_sample_sequence + start)
            + (np.arange(binned.size, dtype=np.float64) + 0.5) * frequency_bin_samples
            - 0.5
        ) / sample_rate_hz
        products.append(binned)
        bin_times_s.append(centers)
        total_bins += binned.size

    # The absolute global sample count can be large.  Centering time preserves
    # the fitted frequency but avoids needless phase loss in the search oscillator.
    time_origin_s = bin_times_s[0][0]
    centered_times = tuple(values - time_origin_s for values in bin_times_s)

    def objective(error_hz: float) -> float:
        score = 0.0
        for values, times in zip(products, centered_times, strict=True):
            coherent_sum = np.sum(values * np.exp(-2j * np.pi * error_hz * times))
            energy = float(np.sum(np.abs(values) ** 2))
            score += float(abs(coherent_sum) ** 2 / max(values.size * energy, np.finfo(float).tiny))
        return score / len(products)

    # Three deterministic grid refinements avoid a heavy optimizer dependency.
    # The final resolution is much finer than needed to preserve phase across a
    # host-controlled screen, while the first grid catches normal crystal error.
    center_hz = 0.0
    half_span_hz = frequency_search_span_hz
    best_score = -np.inf
    for point_count in (401, 101, 101):
        candidates = np.linspace(center_hz - half_span_hz, center_hz + half_span_hz, point_count)
        scores = np.asarray([objective(float(candidate)) for candidate in candidates])
        best_index = int(np.argmax(scores))
        center_hz = float(candidates[best_index])
        best_score = float(scores[best_index])
        grid_step_hz = float(candidates[1] - candidates[0])
        half_span_hz = 2.0 * grid_step_hz
    if abs(center_hz) >= frequency_search_span_hz * 0.995:
        raise ValueError("cross-frequency solution reached the frequency-search boundary")

    fitted_difference_hz = nominal_frequency_difference_hz + center_hz
    fitted_radians_per_sample = 2.0 * np.pi * fitted_difference_hz / sample_rate_hz
    phasors: list[DwellPhasor] = []
    for interval in intervals:
        start = interval.start + edge_discard_samples
        stop = interval.stop - edge_discard_samples
        selected_first = first[start:stop]
        selected_second = second[start:stop]
        global_indices = float(first_sample_sequence) + np.arange(start, stop, dtype=np.float64)
        baseband = (
            selected_second
            * np.conjugate(selected_first)
            * np.exp(-1j * fitted_radians_per_sample * global_indices)
        )
        value = complex(np.mean(baseband))
        power = float(np.mean(np.abs(baseband) ** 2))
        coherence = float(min(1.0, abs(value) / np.sqrt(max(power, np.finfo(float).tiny))))
        phasors.append(
            DwellPhasor(
                port=interval.port,
                center_sample_sequence=float(first_sample_sequence + (start + stop - 1) / 2.0),
                value=value,
                coherence=coherence,
                sample_count=stop - start,
            )
        )
    return CrossFrequencyEstimate(
        frequency_difference_hz=fitted_difference_hz,
        frequency_error_hz=center_hz,
        search_objective=best_score,
        bin_count=total_bins,
        phasors=tuple(phasors),
    )
