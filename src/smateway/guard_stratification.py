"""Pure analysis for selector-synchronous Fast20 ``ALL_OFF`` strata.

The normal Fast20 transfer analysis deliberately pools the long marker and
every short ``ALL_OFF`` interval.  This module keeps the same fitted cycle and
marker reference, but compares the clean center of each short interval with a
drift-interpolated marker baseline.  It contains no artifact discovery, file
I/O, plotting, or hardware access so synthetic detection tests can exercise
the complete numerical decision path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import atan2, isfinite, pi

import numpy as np
import numpy.typing as npt

from .profile import ControlProfile
from .schedule_alignment import complete_cycle_ids


class GuardStratificationError(ValueError):
    """The transfer or timing evidence cannot support guard stratification."""


@dataclass(frozen=True, slots=True)
class DetectionThresholds:
    """Conjunctive admission thresholds for a persistent differential signature."""

    minimum_amplitude_fraction_of_h_off: float = 0.005
    minimum_cross_capture_phase_coherence: float = 0.75

    def __post_init__(self) -> None:
        if (
            not isfinite(self.minimum_amplitude_fraction_of_h_off)
            or self.minimum_amplitude_fraction_of_h_off <= 0.0
        ):
            raise GuardStratificationError("minimum amplitude must be positive and finite")
        if (
            not isfinite(self.minimum_cross_capture_phase_coherence)
            or not 0.0 < self.minimum_cross_capture_phase_coherence <= 1.0
        ):
            raise GuardStratificationError("minimum phase coherence must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class CaptureStratification:
    """Robust selector-synchronous estimates from one continuous capture."""

    complete_cycle_count: int
    analyzed_cycle_count: int
    h_off: complex
    control_name: str
    raw_cycle_residuals: Mapping[str, npt.NDArray[np.complex128]]
    adjusted_cycle_residuals: Mapping[str, npt.NDArray[np.complex128]]


def robust_complex_center(values: npt.ArrayLike) -> complex:
    """Return the component-wise median of one finite complex vector."""

    array = np.asarray(values, dtype=np.complex128)
    if array.ndim != 1 or array.size < 1:
        raise GuardStratificationError("complex center requires a nonempty vector")
    if not np.all(np.isfinite(array.real)) or not np.all(np.isfinite(array.imag)):
        raise GuardStratificationError("complex center requires finite values")
    return complex(float(np.median(array.real)), float(np.median(array.imag)))


def phase_coherence(values: npt.ArrayLike) -> float:
    """Return unit-phasor coherence, treating zero values as uninformative."""

    array = np.asarray(values, dtype=np.complex128)
    if array.ndim != 1 or array.size < 1:
        raise GuardStratificationError("phase coherence requires a nonempty vector")
    amplitudes = np.abs(array)
    nonzero = amplitudes > np.finfo(np.float64).tiny
    if not np.any(nonzero):
        return 0.0
    return float(np.clip(abs(np.mean(array[nonzero] / amplitudes[nonzero])), 0.0, 1.0))


def _window_offsets_ms(profile: ControlProfile) -> tuple[dict[str, float], str]:
    """Return transition-following windows plus the no-transition control.

    Fast20 has seven ordinary inter-state guards.  The first five milliseconds
    of the long marker immediately follow ANT8 and are analyzed as a marker-entry
    stratum, not mislabeled as a protocol guard.  The contiguous pre-ANT1 guard
    has no RF transition at its nominal start and is the negative control.
    """

    if len(profile.states) != 8:
        raise GuardStratificationError("guard stratification requires exactly eight states")
    offsets: dict[str, float] = {}
    cursor = float(profile.marker_body_ms + profile.guard_ms)
    for index, state in enumerate(profile.states):
        cursor += state.dwell_ms
        if index + 1 < len(profile.states):
            offsets[f"after_{state.name}"] = cursor
            cursor += profile.guard_ms
    if not np.isclose(cursor, profile.nominal_cycle_ms, rtol=0.0, atol=1e-12):
        raise GuardStratificationError("derived schedule does not close at the cycle boundary")
    offsets[f"marker_entry_after_{profile.states[-1].name}"] = 0.0
    control_name = f"pre_{profile.states[0].name}_no_transition_control"
    offsets[control_name] = float(profile.marker_body_ms)
    return offsets, control_name


def _validate_time_axis(times_ms: npt.NDArray[np.float64]) -> float:
    if times_ms.ndim != 1 or times_ms.size < 2:
        raise GuardStratificationError("time axis must contain at least two bins")
    if not np.all(np.isfinite(times_ms)) or np.any(np.diff(times_ms) <= 0.0):
        raise GuardStratificationError("time axis must be finite and strictly increasing")
    steps = np.diff(times_ms)
    bin_duration_ms = float(np.median(steps))
    if not np.allclose(steps, bin_duration_ms, rtol=0.0, atol=bin_duration_ms * 1e-6):
        raise GuardStratificationError("time bins must be uniformly spaced")
    return bin_duration_ms


def stratify_all_off_transfer(
    transfer: npt.ArrayLike,
    reference_valid: npt.ArrayLike,
    times_ms: npt.ArrayLike,
    *,
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    profile: ControlProfile,
    marker_anchor_window_ms: tuple[float, float] = (10.0, 70.0),
    center_window_after_entry_ms: tuple[float, float] = (2.0, 3.0),
    minimum_complete_cycles: int = 20,
) -> CaptureStratification:
    """Measure state-history-dependent differences between nominally identical OFF states.

    Marker anchors are component-wise medians from the quiet marker body.  A
    straight complex line between consecutive anchors removes slow within-run
    drift.  Each short-window result is normalized by the capture's marker
    ``H_off`` and paired with the no-transition pre-ANT1 control from the same
    cycle before cross-capture aggregation.
    """

    values = np.asarray(transfer, dtype=np.complex128)
    valid = np.asarray(reference_valid, dtype=np.bool_)
    times = np.asarray(times_ms, dtype=np.float64)
    if values.ndim != 1 or valid.ndim != 1 or values.size != valid.size:
        raise GuardStratificationError("transfer and reference-valid mask must match")
    if values.size != times.size:
        raise GuardStratificationError("transfer and time axis must match")
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise GuardStratificationError("transfer must be finite")
    if not isfinite(duration_ms) or duration_ms <= 0.0:
        raise GuardStratificationError("duration must be positive and finite")
    if not isfinite(cycle_ms) or cycle_ms <= 0.0 or not isfinite(marker_phase_ms):
        raise GuardStratificationError("cycle and marker timing must be finite")
    if minimum_complete_cycles < 2:
        raise GuardStratificationError("at least two complete cycles are required")
    bin_duration_ms = _validate_time_axis(times)
    anchor_start, anchor_stop = marker_anchor_window_ms
    center_start, center_stop = center_window_after_entry_ms
    if not 0.0 < anchor_start < anchor_stop < profile.marker_body_ms:
        raise GuardStratificationError("marker anchor window must lie inside the marker body")
    if not 0.0 < center_start < center_stop < profile.guard_ms:
        raise GuardStratificationError("center window must lie inside a Fast20 guard")
    if center_stop - center_start < 2.0 * bin_duration_ms:
        raise GuardStratificationError("center window is too short for the coherent-bin grid")

    offsets, control_name = _window_offsets_ms(profile)
    cycle_ids, complete_ids = complete_cycle_ids(
        times,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
    )
    if complete_ids.size < minimum_complete_cycles:
        raise GuardStratificationError("capture has too few complete cycles")
    scale = cycle_ms / profile.nominal_cycle_ms
    anchors: dict[int, complex] = {}
    anchor_times: dict[int, float] = {}
    for raw_cycle_id in complete_ids:
        cycle_id = int(raw_cycle_id)
        cycle_start = marker_phase_ms + cycle_id * cycle_ms
        position = times - cycle_start
        selected = (
            (cycle_ids == cycle_id)
            & valid
            & (position >= anchor_start * scale)
            & (position <= anchor_stop * scale)
        )
        if np.count_nonzero(selected) < 10:
            raise GuardStratificationError("complete cycle has too few marker-anchor bins")
        anchors[cycle_id] = robust_complex_center(values[selected])
        anchor_times[cycle_id] = float(np.mean(times[selected]))

    h_off = robust_complex_center(np.asarray(list(anchors.values()), dtype=np.complex128))
    if abs(h_off) <= np.finfo(np.float64).tiny:
        raise GuardStratificationError("marker H_off is zero")
    raw_residuals: dict[str, list[complex]] = {name: [] for name in offsets}
    analyzed_cycle_ids = [
        int(cycle_id)
        for cycle_id in complete_ids
        if int(cycle_id) - 1 in anchors and int(cycle_id) + 1 in anchors
    ]
    if len(analyzed_cycle_ids) < minimum_complete_cycles - 2:
        raise GuardStratificationError("capture lacks bracketing marker anchors")
    marker_entry_name = f"marker_entry_after_{profile.states[-1].name}"
    for cycle_id in analyzed_cycle_ids:
        cycle_start = marker_phase_ms + cycle_id * cycle_ms
        for name, nominal_offset in offsets.items():
            if name == marker_entry_name:
                first_id, second_id = cycle_id - 1, cycle_id
            else:
                first_id, second_id = cycle_id, cycle_id + 1
            first_anchor = anchors[first_id]
            second_anchor = anchors[second_id]
            first_time = anchor_times[first_id]
            second_time = anchor_times[second_id]
            if second_time <= first_time:
                raise GuardStratificationError("marker anchors do not advance")
            window_start = cycle_start + (nominal_offset + center_start) * scale
            window_stop = cycle_start + (nominal_offset + center_stop) * scale
            selected = valid & (times >= window_start) & (times <= window_stop)
            if np.count_nonzero(selected) < 2:
                raise GuardStratificationError(f"{name} has too few admitted bins")
            window_time = float(np.mean(times[selected]))
            window_value = robust_complex_center(values[selected])
            fraction = (window_time - first_time) / (second_time - first_time)
            baseline = first_anchor + fraction * (second_anchor - first_anchor)
            raw_residuals[name].append((window_value - baseline) / h_off)

    raw_arrays = {
        name: np.asarray(items, dtype=np.complex128) for name, items in raw_residuals.items()
    }
    control = raw_arrays[control_name]
    adjusted = {
        name: values_for_name - control
        for name, values_for_name in raw_arrays.items()
        if name != control_name
    }
    return CaptureStratification(
        complete_cycle_count=int(complete_ids.size),
        analyzed_cycle_count=len(analyzed_cycle_ids),
        h_off=h_off,
        control_name=control_name,
        raw_cycle_residuals=raw_arrays,
        adjusted_cycle_residuals=adjusted,
    )


def aggregate_capture_centers(
    centers_by_stratum: Mapping[str, Sequence[complex]],
    *,
    thresholds: DetectionThresholds | None = None,
) -> dict[str, object]:
    """Apply the fixed cross-capture persistent-signature decision contract."""

    if thresholds is None:
        thresholds = DetectionThresholds()
    if not centers_by_stratum:
        raise GuardStratificationError("aggregate requires at least one stratum")
    lengths = {len(values) for values in centers_by_stratum.values()}
    if len(lengths) != 1 or next(iter(lengths)) < 2:
        raise GuardStratificationError("strata must contain equal multi-capture vectors")
    strata: list[dict[str, object]] = []
    for name, raw_values in centers_by_stratum.items():
        values = np.asarray(raw_values, dtype=np.complex128)
        center = robust_complex_center(values)
        coherence = phase_coherence(values)
        amplitude = abs(center)
        passes_amplitude = amplitude >= thresholds.minimum_amplitude_fraction_of_h_off
        passes_coherence = coherence >= thresholds.minimum_cross_capture_phase_coherence
        strata.append(
            {
                "name": name,
                "capture_count": int(values.size),
                "robust_center": {"real": center.real, "imag": center.imag},
                "robust_amplitude_fraction_of_h_off": amplitude,
                "robust_amplitude_percent_of_h_off": 100.0 * amplitude,
                "robust_phase_deg": atan2(center.imag, center.real) * 180.0 / pi,
                "median_capture_amplitude_fraction_of_h_off": float(np.median(np.abs(values))),
                "p90_capture_amplitude_fraction_of_h_off": float(np.quantile(np.abs(values), 0.9)),
                "cross_capture_phase_coherence": coherence,
                "passes_amplitude_gate": bool(passes_amplitude),
                "passes_phase_coherence_gate": bool(passes_coherence),
                "persistent_signature_detected": bool(passes_amplitude and passes_coherence),
            }
        )
    detected = [str(item["name"]) for item in strata if item["persistent_signature_detected"]]
    return {
        "thresholds": {
            "minimum_amplitude_fraction_of_h_off": (thresholds.minimum_amplitude_fraction_of_h_off),
            "minimum_amplitude_percent_of_h_off": (
                100.0 * thresholds.minimum_amplitude_fraction_of_h_off
            ),
            "minimum_cross_capture_phase_coherence": (
                thresholds.minimum_cross_capture_phase_coherence
            ),
            "conjunction_required": True,
        },
        "strata": strata,
        "detected_strata": detected,
        "persistent_selector_synchronous_signature_detected": bool(detected),
    }


__all__ = [
    "CaptureStratification",
    "DetectionThresholds",
    "GuardStratificationError",
    "aggregate_capture_centers",
    "phase_coherence",
    "robust_complex_center",
    "stratify_all_off_transfer",
]
