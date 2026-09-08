"""Experimental reference-labeled timing; never fit on the evaluated suffix."""

from dataclasses import replace
from time import perf_counter

import numpy as np

from smateway.causal_timing import predict_intervals, train_timing
from smateway.rate_timing import complex_json, interval_moments, native_fold


def align_reference_fold(fold, references, profile, *, edge_us=5):
    """Score every circular origin against six known complex plateau levels.

    Guards and transition edges are omitted. The interior ALL_OFF marker is
    included as zero, so a quiet plateau cannot freely become a labeled port.
    A single common complex scale is a nuisance parameter of the alignment fit,
    not a per-port calibration or a change to independent closure criteria.
    """
    values = np.asarray(fold, dtype=complex)
    refs = np.asarray(references, dtype=complex)
    if (
        values.ndim != 1
        or refs.shape != (6,)
        or not len(values)
        or not np.all(np.isfinite(values))
        or not np.all(np.isfinite(refs))
        or np.max(abs(refs)) == 0
    ):
        raise ValueError("finite fold and six nonzero-energy references required")
    if edge_us < 0 or 2 * edge_us >= min(profile.dwell_us, profile.observable_marker_us):
        raise ValueError("alignment trim leaves no plateau")
    t = np.arange(len(values)) / len(values) * profile.cycle_us
    template = np.zeros(len(values), dtype=complex)
    mask = np.zeros(len(values), dtype=float)
    for port, ref in enumerate(refs):
        start = port * (profile.dwell_us + profile.guard_us)
        inside = (t >= start + edge_us) & (t < start + profile.dwell_us - edge_us)
        template[inside] = ref
        mask[inside] = 1
    marker = (t >= profile.cycle_us - profile.observable_marker_us + edge_us) & (
        t < profile.cycle_us - edge_us
    )
    mask[marker] = 1
    cross = np.fft.ifft(np.conj(np.fft.fft(template)) * np.fft.fft(values))
    energy = np.maximum(
        np.fft.ifft(np.conj(np.fft.fft(mask)) * np.fft.fft(abs(values) ** 2)).real, 1e-300
    )
    template_energy = float(np.sum(abs(template) ** 2))
    scores = np.clip(abs(cross) ** 2 / (template_energy * energy), 0, 1)
    best = int(np.argmax(scores))
    signed = best if best <= len(values) // 2 else best - len(values)
    return {
        "shift_bins": signed,
        "shift_nominal_us": signed / len(values) * profile.cycle_us,
        "score": float(scores[best]),
        "unshifted_score": float(scores[0]),
        "common_scale": {
            "real": float((cross[best] / template_energy).real),
            "imag": float((cross[best] / template_energy).imag),
        },
        "scope": "training-only label diagnostic; score is not a qualification gate",
    }


def train_reference_timing(rx1, rx2, profile, references, *, fs, training_s=1):
    base = train_timing(rx1, rx2, profile, fs=fs, training_s=training_s)
    count = base.training_samples
    folded, _ = native_fold(
        rx1[:count],
        rx2[:count],
        fs=fs,
        cycle_hz=fs / base.cycle_samples,
        marker_us=base.origin_sample / fs * 1e6,
        cycle_us=profile.cycle_us,
    )
    alignment = align_reference_fold(folded, references, profile)
    shift = alignment["shift_bins"] / len(folded) * base.cycle_samples
    return replace(base, origin_sample=base.origin_sample + shift), alignment


def rolling_reference_windows(rx1, rx2, profile, references, *, fs, lookback_s=1, hop_s=0.05):
    """Past-only refits with explicit fixed output windows and retained failures.

    No cycle straddles an output-window boundary. The reported observation budget
    is the whole hop, including dropped boundary visits, not cycles times period.
    This is a stored-IQ causal replay, not measured real-time delivery.
    """
    lookback, hop = round(lookback_s * fs), round(hop_s * fs)
    if lookback <= 0 or hop <= 0 or len(rx1) != len(rx2) or len(rx1) < lookback + hop:
        raise ValueError("rolling timing requires training and complete unseen windows")
    rows = []
    for start in range(lookback, len(rx1) - hop + 1, hop):
        started = perf_counter()
        end = start + hop
        training_start = start - lookback
        row = {
            "training_start_sample": training_start,
            "training_stop_sample": start,
            "prediction_start_sample": start,
            "prediction_stop_sample": end,
            "observation_ms": hop / fs * 1000,
        }
        try:
            model, alignment = train_reference_timing(
                rx1[training_start:],
                rx2[training_start:],
                profile,
                references,
                fs=fs,
                training_s=lookback / fs,
            )
            left, right = predict_intervals(model, profile, end - training_start)
            left += training_start
            right += training_start
            cross, power = interval_moments(
                rx1[start:end],
                rx2[start:end],
                left - start,
                right - start,
                fs=fs,
            )
            h = cross / np.maximum(power, 1e-300)
            row.update(
                {
                    "status": "analyzed",
                    "cycles": len(h),
                    "alignment": alignment,
                    "mean_transfer": [complex_json(v) for v in h.mean(axis=0)],
                    "usable_per_port_ms": (np.sum(right - left, axis=0) / fs * 1000).tolist(),
                    "origin_sample": model.origin_sample + training_start,
                    "cycle_samples": model.cycle_samples,
                }
            )
        except ValueError as error:
            row.update({"status": "analysis-failed", "error": str(error)})
        row["host_replay_compute_s"] = perf_counter() - started
        rows.append(row)
    return rows
