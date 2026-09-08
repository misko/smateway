"""Experimental harmonic consensus, isolated from the frozen legacy decoder."""

from dataclasses import dataclass

import numpy as np

from smateway.fast_tracking import _parabolic_frequency
from smateway.rate_timing import coarse_product, native_fold
from smateway.reference_timing import align_reference_fold


def consensus_mask(estimates, weights):
    values, power = np.asarray(estimates), np.asarray(weights)
    if (
        values.ndim != 1
        or power.shape != values.shape
        or len(values) < 3
        or not np.all(np.isfinite(values))
        or np.any(values <= 0)
        or not np.all(np.isfinite(power))
        or np.any(power < 0)
        or power.sum() <= 0
    ):
        raise ValueError("invalid harmonic estimates/weights")
    candidates = {tuple(abs(values - v) <= 0.005 * v) for v in values}
    count = max(sum(mask) for mask in candidates)
    if count < 3:
        raise ValueError("fewer than three mutually consistent harmonics")
    winners = [np.array(mask) for mask in candidates if sum(mask) == count]
    # Distinct equally supported families are ambiguous; don't choose by a loud outlier.
    if len(winners) > 1 and any(not np.any(winners[0] & mask) for mask in winners[1:]):
        raise ValueError("multiple disjoint harmonic families have equal support")
    return max(winners, key=lambda mask: float(power[mask].sum()))


@dataclass(frozen=True)
class ConsensusClock:
    training_samples: int
    sample_rate_hz: int
    origin_sample: float
    cycle_samples: float


def train_consensus_timing(rx1, rx2, profile, references, *, fs, training_s=1):
    count = round(training_s * fs)
    if count <= 0 or len(rx1) <= count or len(rx1) != len(rx2):
        raise ValueError("training prefix and unseen data required")
    one, two = rx1[:count], rx2[:count]
    values = np.convolve(coarse_product(one, two, fs), np.ones(5) / 5, mode="same")
    values -= values.mean()
    size = 1 << (4 * len(values) - 1).bit_length()
    amplitude = abs(np.fft.fft(values, n=size))
    bin_hz = 1e6 / size
    nominal = 1e6 / profile.cycle_us
    norm = np.sqrt(len(values) * np.sum(abs(values) ** 2))
    estimates, weights = [], []
    for harmonic in range(1, 7):
        first = max(1, int(np.ceil(harmonic * nominal / 1.05 / bin_hz)))
        stop = min(size // 2, int(np.floor(harmonic * nominal / 0.95 / bin_hz)) + 1)
        peak = first + int(np.argmax(amplitude[first:stop]))
        estimates.append(_parabolic_frequency(amplitude, peak, bin_hz) / harmonic)
        weights.append(float(amplitude[peak] / max(norm, 1e-300)) ** 2)
    mask = consensus_mask(estimates, weights)
    # Same coherent-detection and clock-range limits as the legacy decoder.
    if np.sqrt(max(np.asarray(weights)[mask])) < 0.003:
        raise ValueError("consensus family below coherent detection gate")
    frequency = float(np.average(np.asarray(estimates)[mask], weights=np.asarray(weights)[mask]))
    scale = nominal / frequency
    if not 0.95 <= scale <= 1.05:
        raise ValueError("clock outside conservative five-percent range")
    fold, _ = native_fold(
        one, two, fs=fs, cycle_hz=frequency, marker_us=0, cycle_us=profile.cycle_us
    )
    alignment = align_reference_fold(fold, references, profile)
    period = fs / frequency
    model = ConsensusClock(count, fs, alignment["shift_bins"] / len(fold) * period, period)
    return model, {
        "method": "experimental maximum harmonic consensus",
        "estimates_hz": estimates,
        "weights": weights,
        "consensus": mask.tolist(),
        "frequency_hz": frequency,
        "alignment": alignment,
    }
