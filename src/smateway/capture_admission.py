"""Streaming ADC headroom admission for bounded dual-receiver captures."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

ADC_CLIP_THRESHOLD_ABS = 2_047
ADC_NEAR_FULL_SCALE_THRESHOLD_ABS = 1_843
MAXIMUM_NEAR_FULL_SCALE_SAMPLE_FRACTION = 0.0001


@dataclass(frozen=True, slots=True)
class ReceiverHeadroomAdmission:
    """One receiver's observed ADC headroom and admission decision."""

    receiver: int
    sample_count: int
    peak_abs_component_counts: float
    clip_threshold_abs: int
    clipped_sample_count: int
    clipping_fraction: float
    near_full_scale_threshold_abs: int
    near_full_scale_sample_count: int
    near_full_scale_fraction: float
    maximum_near_full_scale_fraction: float
    passed: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdcHeadroomAdmission:
    """Per-channel clipping and near-full-scale admission result."""

    passed: bool
    receivers: tuple[ReceiverHeadroomAdmission, ...]


class AdcHeadroomMonitor:
    """Accumulate exact per-channel headroom counts without retaining IQ."""

    def __init__(
        self,
        *,
        receiver_count: int = 2,
        clip_threshold_abs: int = ADC_CLIP_THRESHOLD_ABS,
        near_full_scale_threshold_abs: int = ADC_NEAR_FULL_SCALE_THRESHOLD_ABS,
        maximum_near_full_scale_fraction: float = MAXIMUM_NEAR_FULL_SCALE_SAMPLE_FRACTION,
    ) -> None:
        if receiver_count < 1:
            raise ValueError("receiver_count must be positive")
        if not 1 <= near_full_scale_threshold_abs < clip_threshold_abs:
            raise ValueError("near-full-scale threshold must be positive and below clipping")
        if clip_threshold_abs < 2:
            raise ValueError("clip threshold must be at least two counts")
        if not 0.0 <= maximum_near_full_scale_fraction <= 1.0:
            raise ValueError("maximum near-full-scale fraction must be within 0..1")
        self._receiver_count = receiver_count
        self._clip_threshold_abs = clip_threshold_abs
        self._near_full_scale_threshold_abs = near_full_scale_threshold_abs
        self._maximum_near_full_scale_fraction = maximum_near_full_scale_fraction
        self._sample_count = 0
        self._peak = np.zeros(receiver_count, dtype=np.float64)
        self._clipped = np.zeros(receiver_count, dtype=np.int64)
        self._near_full_scale = np.zeros(receiver_count, dtype=np.int64)

    def observe(self, samples: npt.ArrayLike) -> None:
        """Observe one receiver-by-sample complex block before CI16 conversion."""

        values = np.asarray(samples)
        if values.ndim != 2 or values.shape[0] != self._receiver_count:
            raise ValueError(f"headroom block must have shape ({self._receiver_count}, samples)")
        if values.shape[1] < 1 or not np.iscomplexobj(values):
            raise ValueError("headroom block must contain complex samples")
        if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
            raise ValueError("headroom block must contain finite samples")

        for receiver in range(self._receiver_count):
            absolute_real = np.abs(values[receiver].real)
            absolute_imag = np.abs(values[receiver].imag)
            self._peak[receiver] = max(
                self._peak[receiver],
                float(np.max(absolute_real, initial=0.0)),
                float(np.max(absolute_imag, initial=0.0)),
            )
            self._clipped[receiver] += int(
                np.count_nonzero(
                    (absolute_real >= self._clip_threshold_abs)
                    | (absolute_imag >= self._clip_threshold_abs)
                )
            )
            self._near_full_scale[receiver] += int(
                np.count_nonzero(
                    (absolute_real >= self._near_full_scale_threshold_abs)
                    | (absolute_imag >= self._near_full_scale_threshold_abs)
                )
            )
        self._sample_count += int(values.shape[1])

    def result(self) -> AdcHeadroomAdmission:
        """Return the immutable admission result after at least one block."""

        if self._sample_count < 1:
            raise RuntimeError("headroom admission observed no samples")
        receivers = []
        for receiver in range(self._receiver_count):
            clipped = int(self._clipped[receiver])
            near_full_scale = int(self._near_full_scale[receiver])
            clipping_fraction = clipped / self._sample_count
            near_full_scale_fraction = near_full_scale / self._sample_count
            reasons = []
            if clipped:
                reasons.append("clipping_detected")
            if near_full_scale_fraction > self._maximum_near_full_scale_fraction:
                reasons.append("near_full_scale_fraction_exceeded")
            receivers.append(
                ReceiverHeadroomAdmission(
                    receiver=receiver,
                    sample_count=self._sample_count,
                    peak_abs_component_counts=float(self._peak[receiver]),
                    clip_threshold_abs=self._clip_threshold_abs,
                    clipped_sample_count=clipped,
                    clipping_fraction=clipping_fraction,
                    near_full_scale_threshold_abs=self._near_full_scale_threshold_abs,
                    near_full_scale_sample_count=near_full_scale,
                    near_full_scale_fraction=near_full_scale_fraction,
                    maximum_near_full_scale_fraction=self._maximum_near_full_scale_fraction,
                    passed=not reasons,
                    rejection_reasons=tuple(reasons),
                )
            )
        result = tuple(receivers)
        return AdcHeadroomAdmission(
            passed=all(receiver.passed for receiver in result),
            receivers=result,
        )
