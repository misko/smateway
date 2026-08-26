from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/render_hexray_center_calibration_design.py"
)
SPEC = importlib.util.spec_from_file_location("hexray_design_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_snapshot_locks_geometry_schedule_and_evidence_boundary() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)

    assert document["evidence_status"].startswith("pre-execution")
    assert document["geometry"]["phase_center_circle_diameter_mm"] == 51.0
    assert [row["name"] for row in document["geometry"]["antennas"]] == [
        "ANT1",
        "ANT2",
        "ANT3",
        "ANT4",
        "ANT5",
        "ANT6",
    ]
    assert document["schedule"]["profile_id"] == "hexcal-v1"
    assert document["schedule"]["cycle_us"] == 1500
    assert document["schedule"]["pre_state_all_off_us"] == 20
    assert document["capture_plan"]["planned_artifacts"] == 18
    assert "coarse schedule" in document["capture_plan"]["purpose"]
    assert "not the evidence" in document["capture_plan"]["timing_evidence_limit"]

    gain = document["capture_plan"]["rx_gain_qualification"]
    assert gain["mode"] == "manual"
    assert gain["selected_gain_db"] is None
    assert gain["agc_allowed"] is False
    assert "lowest conservative" in gain["initial_setting"]

    fallback = document["timing_and_gpio_qualification"]["fallback_path"]
    assert fallback["selected"] is True
    assert "timing only" in fallback["independently_observed"]
    assert "not independently" in fallback["not_independently_observed"]


def test_snapshot_locks_separate_rf_timing_pair_and_uncertainty_gates() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)
    qualification = document["rf_timing_qualification"]
    capture = qualification["capture_contract"]

    assert qualification["status"].startswith("selected pre-execution")
    assert capture["captures_per_tested_band"] == 2
    assert capture["duration_ms_per_capture"] == 450
    assert capture["sample_rate_hz"] == 5_000_000
    assert capture["native_sample_period_us"] == 0.2
    assert capture["rf_bandwidth_hz"] == 4_000_000
    assert capture["frames_per_capture"] == 9
    assert capture["samples_per_frame"] == 250_000
    assert capture["samples_per_capture"] == 2_250_000
    assert capture["kernel_buffer_count"] == 8
    assert capture["experimental_5g8_opt_in_required"] is True
    assert "in memory" in capture["retention_and_cleanup"]

    detector = qualification["detector"]
    assert detector["coherent_samples_per_bin"] == 5
    assert detector["complex_bin_duration_us"] == 1.0
    assert detector["native_samples_used_directly_for_edge_fit"] is False
    assert detector["threshold_sweep_q"] == [0.4, 0.5, 0.6]
    assert "two-mean complex changepoint" in detector["independent_estimator"]

    gates = qualification["frozen_gates"]
    assert gates["minimum_complete_cycles_per_capture"] == 290
    assert gates["minimum_decoded_cycle_fraction"] == 0.98
    assert gates["visible_edges_per_accepted_cycle"] == 12
    assert gates["each_ordinary_guard_median_window_us"] == [19.0, 21.0]
    assert gates["conservative_guard_lower_bound_minimum_us"] == 18.0
    assert gates["conservative_guard_upper_bound_maximum_us"] == 22.0
    assert gates["maximum_q40_q60_edge_span_us"] == 1.5
    assert gates["maximum_independent_estimator_delta_us"] == 1.5


def test_snapshot_rejects_a_weakened_rf_timing_pair(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["rf_timing_qualification"]["capture_contract"][
        "captures_per_tested_band"
    ] = 1
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.DesignReportError, match="captures_per_tested_band"):
        MODULE.load_snapshot(malformed)


def test_snapshot_retains_gauge_ambiguity_and_experimental_5g8() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)

    assert document["calibration_model"]["measurement_equation"] == (
        "H_i(f) = C_i(f) * A_i(f)"
    )
    assert "only the product" in document["calibration_model"]["identifiability"]
    assert document["frequency_plan"]["experimental_exact_center_hz"] == 5_800_000_000


def test_renderer_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, first)
    second_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, second)

    assert first_hashes == second_hashes
    assert tuple(first_hashes) == MODULE.FIGURE_NAMES
    for filename in MODULE.FIGURE_NAMES:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_snapshot_rejects_an_inconsistent_cycle(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["schedule"]["cycle_us"] = 1499
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.DesignReportError, match="do not add"):
        MODULE.load_snapshot(malformed)


def test_snapshot_rejects_a_claimed_measurement(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["evidence_status"] = "calibration passed"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.DesignReportError, match="pre-execution"):
        MODULE.load_snapshot(malformed)
