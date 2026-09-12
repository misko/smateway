"""Offline evidence, denominator, simulation and artifact checks for the synthesis."""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_calibration_synthesis import (  # noqa: E402
    DEFAULT_OUTPUT,
    ROOT,
    SOURCES,
    boolean,
    dense_summary,
    ideal_diagnostics,
    number,
    read_csv,
    schedule_metrics,
    sha,
)


@pytest.mark.parametrize("raw,expected", [("True", True), ("False", False)])
def test_explicit_csv_booleans(raw, expected):
    assert boolean(raw) is expected


@pytest.mark.parametrize("raw", ["", "yes", "0", None, True])
def test_unknown_or_missing_boolean_is_not_a_pass(raw):
    with pytest.raises(ValueError):
        boolean(raw)


def test_missing_measurement_is_not_zero():
    assert np.isnan(number(""))
    assert number("0") == 0


def cohorts():
    return [read_csv(ROOT / SOURCES[key]) for key in ("ota_original", "ota_jittered", "ota_latest")]


def test_dense_counts_preserve_all_three_epochs_and_sources():
    rows = dense_summary(cohorts())
    assert [r["records"] for r in rows] == [149] * 6
    assert [r["spatial_model_admissions"] for r in rows] == [73, 66, 0, 72, 0, 64]
    assert [r["phase_admissions_variable_budget"] for r in rows] == [147, 144, 147, 147, 149, 147]
    assert rows[4]["spatial_phase_residual_median_deg"] == pytest.approx(52.78, abs=0.01)
    assert all(r["surveyed_angular_accuracy"] == "not measured" for r in rows)
    json.dumps(rows, allow_nan=False)  # No numpy scalar leakage into the manifest.


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "metric"])
def test_dense_missing_or_duplicate_evidence_is_rejected(mutation):
    data = cohorts()
    if mutation == "missing":
        data[0] = data[0][1:]
    elif mutation == "duplicate":
        data[0] = [*data[0], data[0][0]]
    else:
        data[0][0] = dict(data[0][0], full_bearing_residual_phase_rms_deg="")
    with pytest.raises(ValueError):
        dense_summary(data)


def test_schedule_quantities_are_distinct_and_correct():
    rows = {r["dwell_us"]: r for r in schedule_metrics()}
    assert rows[200]["cycle_us"] == 1500
    assert rows[25]["cycle_us"] == 450
    assert rows[25]["samples_per_visit_at_5msps"] == 125
    assert rows[200]["useful_per_port_percent"] == pytest.approx(12.6666667)
    assert rows[200]["retained_per_port_per_50ms_ms"] == pytest.approx(6.3333333)
    assert rows[200]["useful_per_port_percent"] / rows[25][
        "useful_per_port_percent"
    ] == pytest.approx(3.8)


def test_ideal_gate_failure_does_not_mean_wrong_angle():
    ideal, spherical = ideal_diagnostics()
    assert [r["legacy_gate_valid"] for r in ideal] == [False, False, True, True]
    assert all(r["fit_bearing_deg"] == 90 for r in ideal)
    assert all(abs(r["phase_residual_deg"]) < 1e-10 for r in ideal)
    assert ideal[0]["ambiguity_margin_db"] == pytest.approx(0.0627, abs=0.0001)
    assert ideal[1]["ambiguity_margin_db"] == pytest.approx(0.4644, abs=0.0001)
    high = [r for r in spherical if r["frequency_mhz"] == 5800]
    assert next(
        r["plane_wave_phase_residual_deg"] for r in high if r["range_m"] == 0.3
    ) == pytest.approx(2.6603, abs=0.0001)
    assert all(
        a["plane_wave_phase_residual_deg"] > b["plane_wave_phase_residual_deg"]
        for a, b in zip(high, high[1:], strict=False)
    )


def test_latest_condition_counts_and_controls_are_not_hidden():
    rows = read_csv(ROOT / SOURCES["conditions"])
    assert len(rows) == 34
    assert sum(boolean(r["clean_condition_pass"]) for r in rows) == 7
    assert sum(boolean(r["clean_phase_and_bearing"]) for r in rows) == 0
    for r in rows:
        if boolean(r["clean_condition_pass"]):
            assert r["main_passes"] == r["main_attempts"] == "3"
            assert r["control_passes"] == r["control_attempts"] == "6"
            assert boolean(r["bracket_pass"])
        if r["frequency_hz"] == "5811000000":
            assert not boolean(r["clean_condition_pass"])
    fast = next(
        r
        for r in rows
        if r["frequency_hz"] == "5800000000"
        and r["configuration"] == "D"
        and r["dwell_us"] == "100"
    )
    assert number(fast["base_rms_deg_median"]) == pytest.approx(9.90, abs=0.01)


def test_analysis_and_transport_denominators_stay_separate():
    evidence = json.loads((ROOT / SOURCES["audit"]).read_text())
    a = evidence["rolling_analysis_coverage"]
    assert a["expected_windows"] == 9000
    assert a["analyzed_windows"] == 8750
    assert a["failed_windows"] == 70
    assert a["unattempted_windows"] == 180
    assert a["expected_windows"] == sum(
        a[k] for k in ("analyzed_windows", "failed_windows", "unattempted_windows")
    )
    t = evidence["transport"]
    assert t["complete_requests"] == 448
    assert t["recorded_attempts"] == 458
    assert t["failed_attempts"] == t["recovered_requests"] == 10
    assert t["failed_requests"] == 0


def test_artifact_manifest_and_source_hashes():
    m = json.loads((DEFAULT_OUTPUT / "data/manifest.json").read_text())
    assert len(m["figures"]) == 16
    for entry in [*m["inputs"].values(), m["renderer"]]:
        assert sha(ROOT / entry["path"]) == entry["sha256"]
    for entry in [*m["figures"], *m["tables"], m["report"]]:
        assert sha(DEFAULT_OUTPUT / entry["path"]) == entry["sha256"]
    for figure in m["figures"]:
        assert figure["evidence_kind"]
        assert figure["scope_note"]
        assert all(key in m["inputs"] for key in figure["source_keys"])
        with Image.open(DEFAULT_OUTPUT / figure["path"]) as im:
            assert im.format == "PNG"
            assert im.width >= 2000 and im.height >= 1000
            im.verify()


def test_all_report_links_resolve_and_all_figures_are_embedded():
    report = (DEFAULT_OUTPUT / "README.md").read_text()
    local = [
        s
        for s in re.findall(r"\]\(([^)]+)\)", report)
        if not s.startswith(("https://", "http://", "#"))
    ]
    for link in local:
        assert (DEFAULT_OUTPUT / link.split("#")[0]).is_file(), link
    embedded = re.findall(r"!\[[^\]]*\]\((png/[^)]+)\)", report)
    assert len(embedded) == len(set(embedded)) == 16
    assert "No frequency is currently qualified for production" in report
    assert "Proposed release criteria — not current achievements" in report
