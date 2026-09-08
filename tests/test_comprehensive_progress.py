import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from smateway.rate_timing import PORTS
from smateway.tracking.bearing import solve_bearing
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "progress", ROOT / "scripts/render_comprehensive_progress.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_legacy_gate_can_reject_perfect_lower_band_geometry():
    geometry = ArrayGeometry.circular("confirmed51", PORTS, radius_mm=25.5)
    grid = np.arange(0, 360, 0.25)
    steering = far_field_steering(geometry, 2_450_000_000, grid)
    result = solve_bearing(steering[360], steering, grid)
    assert result.bearing_deg == 90
    assert result.score == pytest.approx(1)
    assert result.ambiguity_margin_db == pytest.approx(0.4549829622805299)
    assert result.reasons == ("ambiguous_peak",)


def test_failed_continuity_is_not_reported_as_complete_duration(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "status": "failed",
                "configuration": {
                    "frequency_hz": 5_800_000_000,
                    "name": "C",
                    "mode": "muted",
                    "receiver_gain_db": 60,
                    "port": None,
                    "dwell_us": None,
                    "frame_samples": 250_000,
                    "sample_rate_hz": 10_000_000,
                },
                "capture": {},
                "partial_timeline": [{}, {}],
                "rejected_block": {"missing_samples_before": 250_000},
                "error": {"message": "sample loss"},
                "safety": {"final_source_mute": {"passed": True}},
            }
        )
    )
    row = MODULE.acquisition_row(path)
    assert row["accepted_duration_s"] == 0.05
    assert row["status"] == "failed"
    assert row["missing_samples_before_rejected_block"] == 250_000


def test_current_geometry_binding_is_same_radius_in_both_bands():
    fixture = json.loads(
        (ROOT / "docs/comprehensive_fast_switching/data/fixture-c6-51mm-v2.json").read_text()
    )
    positions = np.asarray(fixture["positions_m"])
    assert np.linalg.norm(positions, axis=1) == pytest.approx(np.full(6, 0.0255))
    assert fixture["ports"] == list(PORTS)
    assert fixture["geometry_status"] == "user_confirmed_nominal"
    assert fixture["surveyed_transmitter_angles_deg"] is None
