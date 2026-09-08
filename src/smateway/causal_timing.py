"""Past-only timing training and frozen-clock held-out bearing diagnostics.

This module does not assert live delivery or implement relocking. All model
parameters are fitted only on the explicit preceding preamble.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np

from smateway.fast_tracking import (
    FastTrackingProfile,
    decode_fast_schedule,
    estimate_frequency_difference,
)
from smateway.rate_timing import interval_moments
from smateway.tracking.bearing import solve_bearing, wrap_degrees


@dataclass(frozen=True)
class FrozenTiming:
    training_samples: int
    sample_rate_hz: int
    origin_sample: float
    cycle_samples: float
    difference_hz: float
    frequency_fit_objective: float | None
    training_marker_residual_rms_us: float


def train_timing(
    rx1, rx2, profile: FastTrackingProfile, *, fs, training_s=1, nominal_difference_hz=None
) -> FrozenTiming:
    count = round(training_s * fs)
    if count <= 0 or len(rx1) <= count or len(rx2) != len(rx1):
        raise ValueError("training requires a prefix and separate unseen samples")
    # Deliberately slice before forming products or estimating any parameter.
    one = np.asarray(rx1[:count])
    two = np.asarray(rx2[:count])
    decode = decode_fast_schedule(
        np.asarray(two * one.conj(), dtype=np.complex64),
        sample_rate_hz=fs,
        tone_offset_hz=0,
        profile=profile,
        edge_trim_us=5,
    )
    markers = np.asarray(decode.marker_end_bins, dtype=float) * fs / 1e6
    if markers.size < 8:
        raise ValueError("fewer than eight training markers")
    # Do not propagate the rounded median cycle: its sub-sample error accumulates.
    indices = np.arange(markers.size, dtype=float)
    period, origin = np.polyfit(indices, markers, 1)
    residual = markers - (origin + period * indices)
    difference, objective = 0.0, None
    if nominal_difference_hz is not None:
        fit = estimate_frequency_difference(
            one,
            two,
            decode=decode,
            sample_rate_hz=fs,
            nominal_difference_hz=nominal_difference_hz,
        )
        difference, objective = fit.frequency_difference_hz, fit.search_objective
        if objective < 0.25:
            raise ValueError("training-only TX2 frequency fit below 0.25")
    return FrozenTiming(
        count,
        fs,
        float(origin),
        float(period),
        float(difference),
        objective,
        float(np.sqrt(np.mean(residual**2)) / fs * 1e6),
    )


def predict_intervals(
    model: FrozenTiming,
    profile: FastTrackingProfile,
    sample_stop: int,
    *,
    leading_us=5,
    trailing_us=5,
):
    if leading_us < 0 or trailing_us < 0 or leading_us + trailing_us >= profile.dwell_us:
        raise ValueError("discard leaves no active dwell")
    first = max(0, math.ceil((model.training_samples - model.origin_sample) / model.cycle_samples))
    end = math.floor((sample_stop - model.origin_sample) / model.cycle_samples)
    if end <= first:
        raise ValueError("no complete held-out frames")
    cycle = np.arange(first, end, dtype=float)
    starts = model.origin_sample + cycle[:, None] * model.cycle_samples
    scale = model.cycle_samples / profile.cycle_us
    starts = starts + np.arange(6)[None, :] * (profile.dwell_us + profile.guard_us) * scale
    stops = starts + profile.dwell_us * scale
    left = np.ceil(starts + leading_us * model.sample_rate_hz / 1e6).astype(np.int64)
    right = np.floor(stops - trailing_us * model.sample_rate_hz / 1e6).astype(np.int64)
    if (
        np.any(left < model.training_samples)
        or np.any(right > sample_stop)
        or np.any(left >= right)
    ):
        raise ValueError("held-out interval support is invalid")
    return left, right


def bearing_study(
    matrix,
    *,
    cycle_ms,
    coefficients,
    steering,
    bearing_grid,
    reference_bearing_deg=None,
    truth_bearing_deg=None,
    grouping_cycles=None,
):
    """Score each disjoint vector independently; ensemble metrics are post-evaluation."""
    values = np.asarray(matrix, dtype=np.complex128)
    if values.ndim != 2 or values.shape[1] != 6 or not np.all(np.isfinite(values)):
        raise ValueError("bearing study requires finite six-port vectors")
    counts = grouping_cycles or sorted(
        {
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
            *[max(1, math.ceil(ms / cycle_ms)) for ms in (1, 2, 5, 10, 20, 50, 100, 200, 400)],
        }
    )
    rows = []
    for cycles in counts:
        groups = len(values) // cycles
        if not groups:
            continue
        grouped = values[: groups * cycles].reshape(groups, cycles, 6).mean(axis=1)
        estimates = [solve_bearing(v * coefficients, steering, bearing_grid) for v in grouped]
        angles = np.array([e.bearing_deg for e in estimates])
        valid = np.array([e.valid for e in estimates])
        # This circular centre is an evaluation statistic, never fed to a prediction.
        centre = float(np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(angles))))) % 360)
        errors = wrap_degrees(angles - centre)
        rms = float(np.sqrt(np.mean(errors**2)))
        valid_rms = float(np.sqrt(np.mean(errors[valid] ** 2))) if np.any(valid) else None
        reasons = Counter(reason for e in estimates for reason in e.reasons)
        yield_percent = float(100 * valid.mean())
        rows.append(
            {
                "cycles": int(cycles),
                "groups": int(groups),
                "rf_window_ms": cycles * cycle_ms,
                "model_valid_percent": yield_percent,
                "circular_centre_deg": centre,
                "all_group_repeatability_rms_deg": rms,
                "accepted_group_rms_about_all_group_centre_deg": valid_rms,
                "rms_about_independent_bearing_deg": None
                if reference_bearing_deg is None
                else float(np.sqrt(np.mean(wrap_degrees(angles - reference_bearing_deg) ** 2))),
                "surveyed_angle_error_rms_deg": None
                if truth_bearing_deg is None
                else float(np.sqrt(np.mean(wrap_degrees(angles - truth_bearing_deg) ** 2))),
                "rejection_counts": dict(reasons),
                "ambiguity_margin_db_p05": float(
                    np.percentile([e.ambiguity_margin_db for e in estimates], 5)
                ),
                "residual_phase_rms_deg_p95": float(
                    np.percentile([e.residual_phase_rms_deg for e in estimates], 95)
                ),
                "repeatable_bearing_pass": groups >= 30 and yield_percent >= 95 and rms <= 5,
            }
        )
    return rows


def analyze_causal_bearings(
    rx1,
    rx2,
    profile,
    *,
    fs,
    coefficients,
    steering,
    bearing_grid,
    nominal_difference_hz=None,
    training_s=1,
    grouping_cycles=None,
):
    model = train_timing(
        rx1, rx2, profile, fs=fs, training_s=training_s, nominal_difference_hz=nominal_difference_hz
    )
    left, right = predict_intervals(model, profile, len(rx1))
    cross, _power = interval_moments(
        rx1, rx2, left, right, fs=fs, difference_hz=model.difference_hz
    )
    # Match the historical cross-product estimator so timing is the changed variable.
    matrix = cross / (right - left)
    study = bearing_study(
        matrix,
        cycle_ms=model.cycle_samples / fs * 1000,
        coefficients=coefficients,
        steering=steering,
        bearing_grid=bearing_grid,
        grouping_cycles=grouping_cycles,
    )
    return {
        "scope": "past-only clock/frequency training; frozen open-loop replay; not live delivery",
        "model": asdict(model),
        "frames": len(matrix),
        "first_heldout_sample": int(left.min()),
        "last_heldout_sample": int(right.max()),
        "median_samples_per_port_visit": float(np.median(right - left)),
        "independent_static_closure_available": False,
        "relock_implemented": False,
        "buffer_delivery_latency_measured": False,
        "bearing_study": study,
    }
