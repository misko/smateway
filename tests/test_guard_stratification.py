from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from smateway.guard_stratification import (
    DetectionThresholds,
    GuardStratificationError,
    _window_offsets_ms,
    aggregate_capture_centers,
    robust_complex_center,
    stratify_all_off_transfer,
)
from smateway.profile import load_profile

PROFILE_PATH = Path(__file__).parents[1] / "profiles/fast20-v1/control_profile.json"


def _synthetic_capture(
    *,
    capture_phase_rad: float,
    injected_stratum: str | None = None,
    injected_fraction: complex = 0.0j,
    edge_only: bool = False,
):
    profile = load_profile(PROFILE_PATH)
    bin_duration_ms = 0.1
    cycle_count = 30
    duration_ms = cycle_count * profile.nominal_cycle_ms
    times_ms = (np.arange(round(duration_ms / bin_duration_ms)) + 0.5) * bin_duration_ms
    h_off = 0.06 * np.exp(1j * capture_phase_rad)
    # Complex linear drift is removed exactly by bracketing marker anchors.
    transfer = h_off + h_off * (2.0e-7 + 1.5e-7j) * times_ms
    if injected_stratum is not None:
        offsets, _ = _window_offsets_ms(profile)
        offset = offsets[injected_stratum]
        phase_ms = np.mod(times_ms, profile.nominal_cycle_ms)
        if edge_only:
            selected = (phase_ms >= offset) & (phase_ms < offset + 0.5)
        else:
            selected = (phase_ms >= offset) & (phase_ms < offset + profile.guard_ms)
        transfer[selected] += h_off * injected_fraction
    return stratify_all_off_transfer(
        transfer,
        np.ones(transfer.size, dtype=np.bool_),
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=float(profile.nominal_cycle_ms),
        marker_phase_ms=0.0,
        profile=profile,
        minimum_complete_cycles=20,
    )


def test_schedule_windows_distinguish_protocol_guards_and_marker_entry() -> None:
    profile = load_profile(PROFILE_PATH)

    offsets, control = _window_offsets_ms(profile)

    assert offsets == {
        "after_ANT1": 105.0,
        "after_ANT2": 133.0,
        "after_ANT3": 164.0,
        "after_ANT4": 199.0,
        "after_ANT5": 238.0,
        "after_ANT6": 282.0,
        "after_ANT7": 331.0,
        "marker_entry_after_ANT8": 0.0,
        "pre_ANT1_no_transition_control": 80.0,
    }
    assert control == "pre_ANT1_no_transition_control"
    assert "after_ANT8" not in offsets


def test_injected_persistent_guard_signature_is_detected() -> None:
    target = "after_ANT4"
    injected = 0.02 * np.exp(0.7j)
    captures = [
        _synthetic_capture(
            capture_phase_rad=index * 0.23,
            injected_stratum=target,
            injected_fraction=injected,
        )
        for index in range(20)
    ]
    centers = {
        name: [robust_complex_center(item.adjusted_cycle_residuals[name]) for item in captures]
        for name in captures[0].adjusted_cycle_residuals
    }

    aggregate = aggregate_capture_centers(
        centers,
        thresholds=DetectionThresholds(
            minimum_amplitude_fraction_of_h_off=0.005,
            minimum_cross_capture_phase_coherence=0.75,
        ),
    )

    assert aggregate["persistent_selector_synchronous_signature_detected"] is True
    assert aggregate["detected_strata"] == [target]
    row = next(item for item in aggregate["strata"] if item["name"] == target)
    assert row["robust_amplitude_fraction_of_h_off"] == pytest.approx(0.02, abs=5e-5)
    assert row["cross_capture_phase_coherence"] == pytest.approx(1.0, abs=1e-10)


def test_linear_drift_and_edge_only_transient_do_not_trigger_central_gate() -> None:
    target = "after_ANT6"
    captures = [
        _synthetic_capture(
            capture_phase_rad=index * 0.19,
            injected_stratum=target,
            injected_fraction=0.05 * np.exp(1.1j),
            edge_only=True,
        )
        for index in range(20)
    ]
    centers = {
        name: [robust_complex_center(item.adjusted_cycle_residuals[name]) for item in captures]
        for name in captures[0].adjusted_cycle_residuals
    }

    aggregate = aggregate_capture_centers(centers)

    assert aggregate["persistent_selector_synchronous_signature_detected"] is False
    assert aggregate["detected_strata"] == []
    assert (
        max(float(item["robust_amplitude_fraction_of_h_off"]) for item in aggregate["strata"])
        < 1e-10
    )


def test_center_window_must_resolve_multiple_bins() -> None:
    profile = load_profile(PROFILE_PATH)
    times_ms = (np.arange(30 * profile.nominal_cycle_ms) + 0.5).astype(np.float64)

    with pytest.raises(GuardStratificationError, match="too short"):
        stratify_all_off_transfer(
            np.full(times_ms.size, 0.1 + 0.0j),
            np.ones(times_ms.size, dtype=np.bool_),
            times_ms,
            duration_ms=float(30 * profile.nominal_cycle_ms),
            cycle_ms=float(profile.nominal_cycle_ms),
            marker_phase_ms=0.0,
            profile=profile,
            center_window_after_entry_ms=(2.0, 3.0),
        )
