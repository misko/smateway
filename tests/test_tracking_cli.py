from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from smateway.tracking import BoardCalibrationLut, far_field_steering, load_ism_band_profiles
from smateway.tracking.cli import solve_snapshot

ROOT = Path(__file__).resolve().parents[1]
FREQUENCY_PLAN = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
BOARD_LUT = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"


def _snapshot(path: Path, profile_id: str, frequency_hz: int, truth_deg: float) -> None:
    profile = load_ism_band_profiles(FREQUENCY_PLAN)[profile_id]
    correction = BoardCalibrationLut.load(BOARD_LUT).evaluate(
        frequency_hz, profile.geometry.ports
    ).coefficients
    spatial = far_field_steering(
        profile.geometry,
        frequency_hz,
        np.asarray([truth_deg]),
    )[0]
    raw = spatial / correction * np.exp(1.7j)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "profile_id": profile_id,
                "frequency_hz": frequency_hz,
                "ports": list(profile.geometry.ports),
                "same_emitter_reference_valid": True,
                "continuous_phase_valid": False,
                "transfer_real": raw.real.tolist(),
                "transfer_imag": raw.imag.tolist(),
                "weights": [1.0] * len(raw),
            }
        ),
        encoding="utf-8",
    )


def test_cli_snapshot_recovers_exact_high_band_bearing(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    _snapshot(snapshot, "ism5800-c6-v1", 5_800_000_000, 41.25)

    result = solve_snapshot(
        snapshot,
        frequency_plan_path=FREQUENCY_PLAN,
        board_lut_path=BOARD_LUT,
        profile_id="ism5800-c6-v1",
        bearing_step_deg=0.25,
        minimum_score=0.9,
        minimum_ambiguity_db=0.0,
        maximum_residual_phase_deg=1.0,
    )

    assert result["bearing"]["valid"] is True
    assert result["bearing"]["bearing_deg_clockwise_from_forward"] == 41.25
    assert result["board_calibration"]["exact_knot"] is True


def test_cli_fails_closed_on_unvalidated_915_mhz_interpolation(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    _snapshot(snapshot, "ism915-c6-v1", 915_000_000, 120.0)

    result = solve_snapshot(
        snapshot,
        frequency_plan_path=FREQUENCY_PLAN,
        board_lut_path=BOARD_LUT,
        profile_id="ism915-c6-v1",
        bearing_step_deg=0.25,
        minimum_score=0.9,
        minimum_ambiguity_db=0.0,
        maximum_residual_phase_deg=1.0,
    )

    assert result["bearing"]["valid"] is False
    assert "board_interpolation_not_independently_validated" in result["bearing"]["reasons"]
