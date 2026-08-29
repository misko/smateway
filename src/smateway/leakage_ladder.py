"""Marker-independent coherent leakage analysis for continuous dual-RX captures.

The analyzer is deliberately hardware-free.  Callers provide one continuous,
equal-length pair of ADC-scale complex sample vectors and the known stimulus
offset.  Per-block coherent projections are combined with component-wise
medians so that an isolated corrupt block cannot dominate the reported tone or
RX2/RX1 transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, isfinite, log10, pi, sqrt
from numbers import Integral, Real

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class LeakageAnalysisThresholds:
    """Frozen quality and detection thresholds for one analysis."""

    minimum_rx1_snr_db: float = 20.0
    minimum_rx1_coherence: float = 0.995
    maximum_rx1_phase_rms_deg: float = 6.0
    minimum_rx2_detection_snr_db: float = 6.0
    minimum_rx2_detection_coherence: float = 0.80
    minimum_detected_transfer_coherence: float = 0.995
    maximum_detected_transfer_phase_rms_deg: float = 6.0
    minimum_reference_block_snr_db: float = 3.0
    minimum_reference_valid_block_fraction: float = 0.80
    adc_clip_threshold_abs: float = 2_047.0


DEFAULT_LEAKAGE_ANALYSIS_THRESHOLDS = LeakageAnalysisThresholds()


@dataclass(frozen=True, slots=True)
class ReceiverToneEstimate:
    """Robust known-tone and ADC-headroom estimate for one receiver."""

    phasor: complex
    amplitude_counts: float
    phase_deg: float | None
    tone_power_counts_squared: float
    noise_power_counts_squared: float
    tone_to_noise_snr_db: float
    block_phase_coherence: float
    block_phase_rms_deg: float | None
    peak_abs_component_counts: float
    adc_headroom_counts: float
    adc_headroom_db: float
    adc_headroom_passed: bool
    tone_detected: bool


@dataclass(frozen=True, slots=True)
class ComplexTransferEstimate:
    """Robust coherent RX2/RX1 transfer across usable reference blocks."""

    phasor: complex | None
    amplitude_ratio: float | None
    amplitude_db: float | None
    amplitude_upper_bound_ratio: float | None
    amplitude_upper_bound_db: float | None
    amplitude_upper_bound_method: str
    phase_deg: float | None
    block_phase_coherence: float
    block_phase_rms_deg: float | None
    valid_block_count: int
    valid_block_fraction: float


@dataclass(frozen=True, slots=True)
class CoherentLeakageAnalysis:
    """Complete marker-independent dual-receiver leakage result."""

    sample_count: int
    sample_rate_hz: float
    tone_offset_hz: float
    block_duration_s: float
    block_count: int
    thresholds: LeakageAnalysisThresholds
    rx1: ReceiverToneEstimate
    rx2: ReceiverToneEstimate
    rx2_over_rx1: ComplexTransferEstimate
    quality_passed: bool
    quality_rejection_reasons: tuple[str, ...]


def _finite_real(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _validate_thresholds(thresholds: LeakageAnalysisThresholds) -> None:
    if not isinstance(thresholds, LeakageAnalysisThresholds):
        raise ValueError("thresholds must be LeakageAnalysisThresholds")
    _finite_real(thresholds.minimum_rx1_snr_db, "minimum RX1 SNR")
    _finite_real(thresholds.minimum_rx2_detection_snr_db, "minimum RX2 detection SNR")
    coherence_fields = (
        (thresholds.minimum_rx1_coherence, "minimum RX1 coherence"),
        (thresholds.minimum_rx2_detection_coherence, "minimum RX2 detection coherence"),
        (
            thresholds.minimum_detected_transfer_coherence,
            "minimum detected-transfer coherence",
        ),
    )
    for raw_coherence, label in coherence_fields:
        coherence = _finite_real(raw_coherence, label)
        if not 0.0 <= coherence <= 1.0:
            raise ValueError(f"{label} must be within 0..1")
    phase_rms_fields = (
        (thresholds.maximum_rx1_phase_rms_deg, "maximum RX1 phase RMS"),
        (
            thresholds.maximum_detected_transfer_phase_rms_deg,
            "maximum detected-transfer phase RMS",
        ),
    )
    for raw_phase_rms, label in phase_rms_fields:
        if _finite_real(raw_phase_rms, label) <= 0.0:
            raise ValueError(f"{label} must be positive")
    _finite_real(
        thresholds.minimum_reference_block_snr_db,
        "minimum reference block SNR",
    )
    valid_fraction = _finite_real(
        thresholds.minimum_reference_valid_block_fraction,
        "minimum reference valid block fraction",
    )
    if not 0.0 < valid_fraction <= 1.0:
        raise ValueError("minimum reference valid block fraction must be within (0, 1]")
    clip = _finite_real(thresholds.adc_clip_threshold_abs, "ADC clip threshold")
    if clip <= 0.0:
        raise ValueError("ADC clip threshold must be positive")


def _complex_vector(samples: npt.ArrayLike, label: str) -> npt.NDArray[np.complex128]:
    values = np.asarray(samples)
    if values.ndim != 1:
        raise ValueError(f"{label} samples must be a one-dimensional vector")
    if values.size < 1:
        raise ValueError(f"{label} samples must not be empty")
    if not np.iscomplexobj(values):
        raise ValueError(f"{label} samples must be complex")
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise ValueError(f"{label} samples must be finite")
    return np.asarray(values, dtype=np.complex128)


def _snr_db(signal_power: float, noise_power: float) -> float:
    if signal_power <= 0.0:
        return float("-inf")
    if noise_power <= 0.0:
        return float("inf")
    return 10.0 * log10(signal_power / noise_power)


def _phase_summary(values: npt.NDArray[np.complex128]) -> tuple[complex, float, float | None]:
    center = complex(float(np.median(values.real)), float(np.median(values.imag)))
    amplitudes = np.abs(values)
    nonzero = amplitudes > np.finfo(np.float64).tiny
    units = np.zeros(values.shape, dtype=np.complex128)
    units[nonzero] = values[nonzero] / amplitudes[nonzero]
    coherence = float(np.clip(abs(np.mean(units)), 0.0, 1.0))
    if abs(center) <= np.finfo(np.float64).tiny or not np.any(nonzero):
        return center, coherence, None
    residual_rad = np.angle(values[nonzero] * np.conj(center))
    phase_rms_deg = sqrt(float(np.mean(residual_rad**2))) * 180.0 / pi
    return center, coherence, phase_rms_deg


def _phase_deg(value: complex) -> float | None:
    if abs(value) <= np.finfo(np.float64).tiny:
        return None
    return atan2(value.imag, value.real) * 180.0 / pi


def _block_boundaries(
    sample_count: int,
    *,
    sample_rate_hz: float,
    block_duration_s: float,
    minimum_block_count: int,
) -> tuple[tuple[int, int], ...]:
    target_size = round(sample_rate_hz * block_duration_s)
    if target_size < 2:
        raise ValueError("block duration must span at least two samples")
    block_count = sample_count // target_size
    if block_count < minimum_block_count:
        raise ValueError(
            f"capture must contain at least {minimum_block_count} complete analysis blocks"
        )
    base_size, extra = divmod(sample_count, block_count)
    boundaries = []
    start = 0
    for block_index in range(block_count):
        stop = start + base_size + (1 if block_index < extra else 0)
        boundaries.append((start, stop))
        start = stop
    return tuple(boundaries)


def _block_tone_statistics(
    samples: npt.NDArray[np.complex128],
    *,
    phase_step_rad: float,
    boundaries: tuple[tuple[int, int], ...],
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.float64]]:
    phasors = np.empty(len(boundaries), dtype=np.complex128)
    noise_powers = np.empty(len(boundaries), dtype=np.float64)
    for block_index, (start, stop) in enumerate(boundaries):
        indices = np.arange(start, stop, dtype=np.float64)
        carrier = np.exp(1j * phase_step_rad * indices)
        block = samples[start:stop]
        phasor = complex(np.mean(block * np.conj(carrier)))
        residual = block - phasor * carrier
        phasors[block_index] = phasor
        noise_powers[block_index] = float(np.mean(np.abs(residual) ** 2))
    return phasors, noise_powers


def _receiver_estimate(
    samples: npt.NDArray[np.complex128],
    phasors: npt.NDArray[np.complex128],
    noise_powers: npt.NDArray[np.float64],
    *,
    thresholds: LeakageAnalysisThresholds,
    minimum_snr_db: float,
    minimum_coherence: float,
    maximum_phase_rms_deg: float | None,
) -> ReceiverToneEstimate:
    center, coherence, phase_rms_deg = _phase_summary(phasors)
    tone_power = abs(center) ** 2
    noise_power = float(np.median(noise_powers))
    snr_db = _snr_db(tone_power, noise_power)
    peak = max(float(np.max(np.abs(samples.real))), float(np.max(np.abs(samples.imag))))
    headroom_counts = max(thresholds.adc_clip_threshold_abs - peak, 0.0)
    headroom_db = (
        float("inf") if peak <= 0.0 else 20.0 * log10(thresholds.adc_clip_threshold_abs / peak)
    )
    adc_passed = peak < thresholds.adc_clip_threshold_abs
    detected = snr_db >= minimum_snr_db and coherence >= minimum_coherence
    if maximum_phase_rms_deg is not None:
        detected = detected and phase_rms_deg is not None and phase_rms_deg <= maximum_phase_rms_deg
    return ReceiverToneEstimate(
        phasor=center,
        amplitude_counts=abs(center),
        phase_deg=_phase_deg(center),
        tone_power_counts_squared=tone_power,
        noise_power_counts_squared=noise_power,
        tone_to_noise_snr_db=snr_db,
        block_phase_coherence=coherence,
        block_phase_rms_deg=phase_rms_deg,
        peak_abs_component_counts=peak,
        adc_headroom_counts=headroom_counts,
        adc_headroom_db=headroom_db,
        adc_headroom_passed=adc_passed,
        tone_detected=detected,
    )


def _transfer_estimate(
    rx1_phasors: npt.NDArray[np.complex128],
    rx2_phasors: npt.NDArray[np.complex128],
    reference_block_snr_db: npt.NDArray[np.float64],
    *,
    minimum_reference_block_snr_db: float,
) -> ComplexTransferEstimate:
    valid = (reference_block_snr_db >= minimum_reference_block_snr_db) & (
        np.abs(rx1_phasors) > np.finfo(np.float64).tiny
    )
    valid_count = int(np.count_nonzero(valid))
    valid_fraction = valid_count / rx1_phasors.size
    if valid_count == 0:
        return ComplexTransferEstimate(
            phasor=None,
            amplitude_ratio=None,
            amplitude_db=None,
            amplitude_upper_bound_ratio=None,
            amplitude_upper_bound_db=None,
            amplitude_upper_bound_method=("median block-ratio amplitude plus three scaled MAD"),
            phase_deg=None,
            block_phase_coherence=0.0,
            block_phase_rms_deg=None,
            valid_block_count=0,
            valid_block_fraction=0.0,
        )
    ratios = rx2_phasors[valid] / rx1_phasors[valid]
    center, coherence, phase_rms_deg = _phase_summary(ratios)
    amplitude = abs(center)
    amplitude_db = float("-inf") if amplitude <= 0.0 else 20.0 * log10(amplitude)
    ratio_amplitudes = np.abs(ratios)
    median_ratio_amplitude = float(np.median(ratio_amplitudes))
    scaled_mad = 1.4826 * float(np.median(np.abs(ratio_amplitudes - median_ratio_amplitude)))
    upper_bound = max(amplitude, median_ratio_amplitude + 3.0 * scaled_mad)
    upper_bound_db = float("-inf") if upper_bound <= 0.0 else 20.0 * log10(upper_bound)
    return ComplexTransferEstimate(
        phasor=center,
        amplitude_ratio=amplitude,
        amplitude_db=amplitude_db,
        amplitude_upper_bound_ratio=upper_bound,
        amplitude_upper_bound_db=upper_bound_db,
        amplitude_upper_bound_method="median block-ratio amplitude plus three scaled MAD",
        phase_deg=_phase_deg(center),
        block_phase_coherence=coherence,
        block_phase_rms_deg=phase_rms_deg,
        valid_block_count=valid_count,
        valid_block_fraction=valid_fraction,
    )


def analyze_coherent_leakage(
    rx1_samples: npt.ArrayLike,
    rx2_samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    block_duration_s: float = 0.010,
    minimum_block_count: int = 8,
    thresholds: LeakageAnalysisThresholds = DEFAULT_LEAKAGE_ANALYSIS_THRESHOLDS,
) -> CoherentLeakageAnalysis:
    """Analyze a continuous known-tone dual-RX capture without a switch marker.

    RX1 is the conducted reference.  A successful return proves structural and
    numeric input validation; invalid input raises ``ValueError``.  The result's
    quality gate deliberately does not require an RX2 tone, because an absent
    RX2 tone is the desired outcome for a well-isolated leakage-ladder stage.
    Input samples are expected in ADC component-count units unless the caller
    supplies a matching ``adc_clip_threshold_abs``.
    """

    rate = _finite_real(sample_rate_hz, "sample rate")
    if rate <= 0.0:
        raise ValueError("sample rate must be positive")
    offset = _finite_real(tone_offset_hz, "tone offset")
    if abs(offset) >= rate / 2.0:
        raise ValueError("tone offset must be strictly inside Nyquist")
    duration = _finite_real(block_duration_s, "block duration")
    if duration <= 0.0:
        raise ValueError("block duration must be positive")
    if (
        isinstance(minimum_block_count, bool)
        or not isinstance(minimum_block_count, Integral)
        or minimum_block_count < 3
    ):
        raise ValueError("minimum block count must be an integer of at least three")
    _validate_thresholds(thresholds)

    rx1 = _complex_vector(rx1_samples, "RX1")
    rx2 = _complex_vector(rx2_samples, "RX2")
    if rx1.size != rx2.size:
        raise ValueError("RX1 and RX2 sample vectors must have equal length")
    boundaries = _block_boundaries(
        int(rx1.size),
        sample_rate_hz=rate,
        block_duration_s=duration,
        minimum_block_count=int(minimum_block_count),
    )
    phase_step_rad = 2.0 * pi * offset / rate
    rx1_phasors, rx1_noise = _block_tone_statistics(
        rx1,
        phase_step_rad=phase_step_rad,
        boundaries=boundaries,
    )
    rx2_phasors, rx2_noise = _block_tone_statistics(
        rx2,
        phase_step_rad=phase_step_rad,
        boundaries=boundaries,
    )

    rx1_estimate = _receiver_estimate(
        rx1,
        rx1_phasors,
        rx1_noise,
        thresholds=thresholds,
        minimum_snr_db=thresholds.minimum_rx1_snr_db,
        minimum_coherence=thresholds.minimum_rx1_coherence,
        maximum_phase_rms_deg=thresholds.maximum_rx1_phase_rms_deg,
    )
    rx2_estimate = _receiver_estimate(
        rx2,
        rx2_phasors,
        rx2_noise,
        thresholds=thresholds,
        minimum_snr_db=thresholds.minimum_rx2_detection_snr_db,
        minimum_coherence=thresholds.minimum_rx2_detection_coherence,
        maximum_phase_rms_deg=None,
    )
    reference_block_snr_db = np.asarray(
        [
            _snr_db(abs(phasor) ** 2, float(noise_power))
            for phasor, noise_power in zip(rx1_phasors, rx1_noise, strict=True)
        ],
        dtype=np.float64,
    )
    transfer = _transfer_estimate(
        rx1_phasors,
        rx2_phasors,
        reference_block_snr_db,
        minimum_reference_block_snr_db=thresholds.minimum_reference_block_snr_db,
    )

    rejection_reasons = []
    if rx1_estimate.tone_to_noise_snr_db < thresholds.minimum_rx1_snr_db:
        rejection_reasons.append("rx1_reference_snr_below_minimum")
    if rx1_estimate.block_phase_coherence < thresholds.minimum_rx1_coherence:
        rejection_reasons.append("rx1_reference_coherence_below_minimum")
    if (
        rx1_estimate.block_phase_rms_deg is None
        or rx1_estimate.block_phase_rms_deg > thresholds.maximum_rx1_phase_rms_deg
    ):
        rejection_reasons.append("rx1_reference_phase_rms_above_maximum")
    if not rx1_estimate.adc_headroom_passed:
        rejection_reasons.append("rx1_adc_headroom_failed")
    if not rx2_estimate.adc_headroom_passed:
        rejection_reasons.append("rx2_adc_headroom_failed")
    if transfer.valid_block_fraction < thresholds.minimum_reference_valid_block_fraction:
        rejection_reasons.append("reference_valid_block_fraction_below_minimum")
    if rx2_estimate.tone_detected:
        if transfer.phasor is None:
            rejection_reasons.append("detected_rx2_transfer_unavailable")
        if transfer.block_phase_coherence < thresholds.minimum_detected_transfer_coherence:
            rejection_reasons.append("detected_rx2_transfer_coherence_below_minimum")
        if (
            transfer.block_phase_rms_deg is None
            or transfer.block_phase_rms_deg > thresholds.maximum_detected_transfer_phase_rms_deg
        ):
            rejection_reasons.append("detected_rx2_transfer_phase_rms_above_maximum")

    return CoherentLeakageAnalysis(
        sample_count=int(rx1.size),
        sample_rate_hz=rate,
        tone_offset_hz=offset,
        block_duration_s=duration,
        block_count=len(boundaries),
        thresholds=thresholds,
        rx1=rx1_estimate,
        rx2=rx2_estimate,
        rx2_over_rx1=transfer,
        quality_passed=not rejection_reasons,
        quality_rejection_reasons=tuple(rejection_reasons),
    )


__all__ = [
    "CoherentLeakageAnalysis",
    "ComplexTransferEstimate",
    "DEFAULT_LEAKAGE_ANALYSIS_THRESHOLDS",
    "LeakageAnalysisThresholds",
    "ReceiverToneEstimate",
    "analyze_coherent_leakage",
]
