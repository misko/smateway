from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/analyze_fast_tracking_trim_sensitivity.py"
SPEC = importlib.util.spec_from_file_location("trim_sensitivity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
trim = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trim
SPEC.loader.exec_module(trim)


def _run() -> dict[str, object]:
    markers = list(range(100, 13_601, 1_500))
    return {
        "configuration": {"profile_id": "tracking-c6-200us-v1"},
        "capture": {"total_samples_per_channel": 30_000},
        "analysis": {
            "schedule_decode": {
                "marker_end_bins": markers,
                "cycle_scale_median": 1.0,
            }
        },
    }


def test_interval_bounds_vary_only_leading_discard() -> None:
    starts_5, stops_5, dwell = trim._interval_bounds(_run(), 5)
    starts_30, stops_30, _ = trim._interval_bounds(_run(), 30)
    assert dwell == 200
    assert starts_5.shape == (9, 6)
    assert starts_5[0, 0] == 210
    assert stops_5[0, 0] == 590
    assert starts_30[0, 0] == 260
    assert stops_30[0, 0] == stops_5[0, 0]
    assert np.all(starts_30 - starts_5 == 50)


def test_phase_metrics_recover_stable_six_port_vector() -> None:
    rng = np.random.default_rng(17)
    reference = np.exp(1j * np.linspace(-1.0, 1.0, 6))
    matrix = reference[None, :] * np.exp(
        1j * rng.normal(scale=np.deg2rad(2.0), size=(256, 6))
    )
    rows = trim._phase_metrics(matrix, cycle_scale=0.996, cycle_us=1_500)
    assert rows[0]["groups_per_port"] == 256
    assert rows[0]["phase_rms_deg_power_weighted_ports"] == pytest.approx(
        2.0, abs=0.25
    )
    assert rows[0]["measured_wall_latency_ms"] == pytest.approx(1.494)
    assert rows[-1]["cycles_averaged"] == 256
