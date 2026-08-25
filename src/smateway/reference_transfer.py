"""Coherent Fast20 RX2/RX1 transfer analysis for an OTA RX1 reference antenna."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import atan2, log10, pi, sqrt

import numpy as np
import numpy.typing as npt

from .ota_analysis import (
    ContinuityBlock,
    _coherent_pair_bins,
    _complete_cycle_ids,
    _labels_and_interior,
    _local_all_off_baseline,
    _search_phase_alignment,
    _validate_complex_pair,
    _validate_continuity_ledger,
)
from .profile import ControlProfile


@dataclass(frozen=True, slots=True)
class CyclePhasorSummary:
    """Robust center and repeat diagnostics for complex per-cycle phasors."""

    phasor: complex
    amplitude: float
    phase_deg: float
    cycle_coherence: float
    cycle_phase_std_deg: float
    even_odd_phase_agreement: float
    cycle_phasors: tuple[complex, ...]


@dataclass(frozen=True, slots=True)
class ReferenceTransferStateEstimate:
    """RX1 and raw/delta RX2-over-RX1 phasors for one selector state."""

    name: str
    rx1: CyclePhasorSummary
    raw_rx2_over_rx1: CyclePhasorSummary
    all_off_subtracted_rx2_over_rx1: CyclePhasorSummary
    transfer_detection_snr_db: float
    transfer_approximate_phase_standard_error_deg: float
    bin_count: int


@dataclass(frozen=True, slots=True)
class Fast20ReferenceTransferAnalysis:
    """One continuity-attested OTA-reference transfer measurement."""

    cycle_ms: float
    marker_phase_ms: float
    bin_duration_ms: float
    bin_count: int
    complete_cycle_count: int
    edge_exclusion_ms: float
    alignment_score: float
    alignment_even_odd_agreement: float
    reference_valid_bin_fraction: float
    continuity_verified: bool
    continuity_block_count: int
    all_off_anchor_count: int
    all_off_rx1: CyclePhasorSummary
    all_off_raw_rx2_over_rx1: CyclePhasorSummary
    states: tuple[ReferenceTransferStateEstimate, ...]

    @property
    def all_off_transfer_phasor(self) -> complex:
        """Compatibility shorthand for the aggregate raw ALL_OFF transfer."""

        return self.all_off_raw_rx2_over_rx1.phasor

    def estimate(self, name: str) -> ReferenceTransferStateEstimate:
        for estimate in self.states:
            if estimate.name == name:
                return estimate
        raise KeyError(name)


def _group_cycle_means(
    values: npt.NDArray[np.complex128],
    times_ms: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int16],
    usable: npt.NDArray[np.bool_],
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    group_count: int,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.int64]]:
    raw_cycle_ids, complete_ids = _complete_cycle_ids(
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
    )
    selected = usable & np.isin(raw_cycle_ids, complete_ids)
    local_cycles = np.searchsorted(complete_ids, raw_cycle_ids[selected])
    combined = local_cycles * group_count + labels[selected]
    counts = np.bincount(combined, minlength=complete_ids.size * group_count).reshape(
        complete_ids.size, group_count
    )
    if not counts.size or np.any(counts == 0):
        raise ValueError("complete cycles do not contain every guarded schedule state")
    sums = np.bincount(
        combined,
        weights=values[selected].real,
        minlength=complete_ids.size * group_count,
    ) + 1j * np.bincount(
        combined,
        weights=values[selected].imag,
        minlength=complete_ids.size * group_count,
    )
    means = sums.reshape(complete_ids.size, group_count) / counts
    return means.astype(np.complex128), counts.astype(np.int64)


def _phasor_summary(values: npt.NDArray[np.complex128]) -> CyclePhasorSummary:
    if values.ndim != 1 or values.size < 1:
        raise ValueError("cycle phasors must be a non-empty vector")
    center = complex(float(np.median(values.real)), float(np.median(values.imag)))
    amplitudes = np.abs(values)
    tiny = np.finfo(np.float64).tiny
    unit = values / np.maximum(amplitudes, tiny)
    coherence = float(np.clip(abs(np.mean(unit)), 0.0, 1.0))
    if abs(center) <= tiny:
        phase_deg = 0.0
        phase_std_deg = 180.0
    else:
        phase_deg = atan2(center.imag, center.real) * 180.0 / pi
        residual = np.angle(values * np.conj(center))
        phase_std_deg = sqrt(float(np.mean(residual**2))) * 180.0 / pi
    if values.size < 2:
        even_odd = 0.0
    else:
        even = complex(np.mean(values[::2]))
        odd = complex(np.mean(values[1::2]))
        if abs(even) <= tiny or abs(odd) <= tiny:
            even_odd = 0.0
        else:
            even_odd = float(np.cos(atan2(even.imag, even.real) - atan2(odd.imag, odd.real)))
    return CyclePhasorSummary(
        phasor=center,
        amplitude=abs(center),
        phase_deg=phase_deg,
        cycle_coherence=coherence,
        cycle_phase_std_deg=phase_std_deg,
        even_odd_phase_agreement=even_odd,
        cycle_phasors=tuple(complex(value) for value in values),
    )


def _jackknife_standard_error(values: npt.NDArray[np.complex128]) -> float:
    if values.size < 2:
        return float("inf")
    jackknife = (np.sum(values) - values) / (values.size - 1)
    center = np.mean(jackknife)
    return sqrt(float((values.size - 1) / values.size * np.sum(np.abs(jackknife - center) ** 2)))


def analyze_fast20_reference_transfer(
    rx1_samples: npt.ArrayLike,
    rx2_samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    profile: ControlProfile,
    continuity_ledger: Sequence[ContinuityBlock] | None = None,
    bin_ms: float = 1.0,
    edge_exclusion_bins: int = 2,
    cycle_search_ms: tuple[float, float] | None = None,
) -> Fast20ReferenceTransferAnalysis:
    """Measure switched RX2 relative to a continuously illuminated RX1 antenna.

    This is not the terminated-RX1 leakage estimator. The raw coherent transfer
    is ``RX2 / RX1``. A locally interpolated transfer observed during Fast20
    ``ALL_OFF`` intervals is subtracted, leaving the switched path relative to
    the OTA RX1 reference. Raw RX1, raw transfer, and subtracted transfer phasors
    are retained independently for every state and complete cycle.
    """

    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample rate must be positive and finite")
    if not np.isfinite(tone_offset_hz) or abs(tone_offset_hz) >= sample_rate_hz / 2.0:
        raise ValueError("tone offset must be finite and strictly inside Nyquist")
    if not np.isfinite(bin_ms) or bin_ms <= 0.0:
        raise ValueError("bin duration must be positive and finite")
    if edge_exclusion_bins < 0:
        raise ValueError("edge exclusion must not be negative")

    reference, measurement = _validate_complex_pair(rx1_samples, rx2_samples)
    continuity_verified, continuity_block_count = _validate_continuity_ledger(
        continuity_ledger,
        sample_count=reference.size,
    )
    cycle_range = cycle_search_ms or (
        profile.nominal_cycle_ms - 4.0,
        profile.nominal_cycle_ms + 4.0,
    )
    low, high = cycle_range
    if not np.isfinite(low) or not np.isfinite(high) or low <= 0.0 or high < low:
        raise ValueError("cycle search bounds must be finite, positive and ordered")

    samples_per_bin = round(sample_rate_hz * bin_ms / 1000.0)
    if samples_per_bin < 1:
        raise ValueError("bin duration is shorter than one sample")
    reference_bins, measurement_bins = _coherent_pair_bins(
        reference,
        measurement,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=tone_offset_hz,
        samples_per_bin=samples_per_bin,
    )
    bin_duration_ms = samples_per_bin * 1000.0 / sample_rate_hz
    duration_ms = reference_bins.size * bin_duration_ms
    if duration_ms < 5.0 * high:
        raise ValueError("paired capture must span at least five candidate cycles")

    reference_amplitudes = np.abs(reference_bins)
    median_reference = float(np.median(reference_amplitudes))
    if median_reference <= np.finfo(np.float64).tiny:
        raise ValueError("RX1 OTA reference has no usable coherent tone")
    reference_valid = reference_amplitudes >= 0.2 * median_reference
    reference_valid_fraction = float(np.mean(reference_valid))
    if reference_valid_fraction < 0.95:
        raise ValueError("RX1 OTA reference is not continuously usable")
    transfer = np.zeros(reference_bins.size, dtype=np.complex128)
    transfer[reference_valid] = measurement_bins[reference_valid] / reference_bins[reference_valid]
    times_ms = (np.arange(transfer.size, dtype=np.float64) + 0.5) * bin_duration_ms
    edge_exclusion_ms = edge_exclusion_bins * bin_duration_ms
    alignment = _search_phase_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        cycle_range_ms=cycle_range,
        bin_duration_ms=bin_duration_ms,
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
    usable = reference_valid & interior
    baseline, baseline_anchors = _local_all_off_baseline(
        transfer,
        times_ms,
        labels,
        usable,
        all_off_index=len(profile.states),
    )
    subtracted_transfer = transfer - baseline
    group_count = len(profile.states) + 1
    rx1_means, counts = _group_cycle_means(
        reference_bins,
        times_ms,
        labels,
        usable,
        duration_ms=duration_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        group_count=group_count,
    )
    raw_transfer_means, raw_counts = _group_cycle_means(
        transfer,
        times_ms,
        labels,
        usable,
        duration_ms=duration_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        group_count=group_count,
    )
    subtracted_means, subtracted_counts = _group_cycle_means(
        subtracted_transfer,
        times_ms,
        labels,
        usable,
        duration_ms=duration_ms,
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        group_count=group_count,
    )
    if not np.array_equal(counts, raw_counts) or not np.array_equal(counts, subtracted_counts):
        raise RuntimeError("coherent grouped phasor counts disagree")

    estimates = []
    for index, state in enumerate(profile.states):
        delta_values = subtracted_means[:, index]
        delta_summary = _phasor_summary(delta_values)
        standard_error = _jackknife_standard_error(delta_values)
        detection_ratio = delta_summary.amplitude / max(standard_error, np.finfo(np.float64).tiny)
        estimates.append(
            ReferenceTransferStateEstimate(
                name=state.name,
                rx1=_phasor_summary(rx1_means[:, index]),
                raw_rx2_over_rx1=_phasor_summary(raw_transfer_means[:, index]),
                all_off_subtracted_rx2_over_rx1=delta_summary,
                transfer_detection_snr_db=20.0
                * log10(max(detection_ratio, np.finfo(np.float64).tiny)),
                transfer_approximate_phase_standard_error_deg=(
                    atan2(1.0, detection_ratio) * 180.0 / pi
                ),
                bin_count=int(np.sum(counts[:, index])),
            )
        )

    all_off_index = len(profile.states)
    all_off_rx1 = _phasor_summary(rx1_means[:, all_off_index])
    all_off_raw_transfer = _phasor_summary(raw_transfer_means[:, all_off_index])
    return Fast20ReferenceTransferAnalysis(
        cycle_ms=alignment.cycle_ms,
        marker_phase_ms=alignment.marker_phase_ms,
        bin_duration_ms=bin_duration_ms,
        bin_count=int(reference_bins.size),
        complete_cycle_count=int(rx1_means.shape[0]),
        edge_exclusion_ms=edge_exclusion_ms,
        alignment_score=alignment.score,
        alignment_even_odd_agreement=alignment.even_odd_agreement,
        reference_valid_bin_fraction=reference_valid_fraction,
        continuity_verified=continuity_verified,
        continuity_block_count=continuity_block_count,
        all_off_anchor_count=int(baseline_anchors.size),
        all_off_rx1=all_off_rx1,
        all_off_raw_rx2_over_rx1=all_off_raw_transfer,
        states=tuple(estimates),
    )
