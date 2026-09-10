import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_cross_band_tracking_report import (  # noqa: E402
    DEST,
    INPUTS,
    array_response,
    clean_condition,
    collect,
    fixed_pass,
    schedule,
    truth,
    verify_inputs,
)


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("True", True),
                                           ("False", False), ("", False), (None, False)])
def test_csv_boolean_is_explicit(value, expected):
    assert truth(value) is expected


def test_unknown_boolean_rejected():
    with pytest.raises(ValueError, match="Unrecognized boolean"):
        truth("maybe")


@pytest.mark.parametrize("phase,closure,error,expected", [
    ("True", "True", "", True), ("False", "True", "", False),
    ("True", "False", "", False), ("True", "", "", False),
    ("True", "True", "decoder failure", False),
])
def test_fixed_pass_requires_phase_and_closure(phase, closure, error, expected):
    assert fixed_pass({"any_window_pass": phase, "passed": closure, "error": error}) is expected


def test_controls_brackets_and_distinct_trials_are_required():
    main = [{"round": i, "passed": True} for i in (1, 2, 3)]
    controls = [{"round": i, "passed": True} for i in (1, 2, 3)]
    assert clean_condition(main, controls, True)
    assert not clean_condition(main, [], True)
    assert not clean_condition(main, controls[:1], True)
    assert not clean_condition(main, [controls[0]] * 3, True)
    assert not clean_condition(main, controls, True, expected_controls=6)
    assert clean_condition(main, controls * 2, True, expected_controls=6)
    assert not clean_condition(main, controls, False)
    assert not clean_condition(main, [{"round": 1, "passed": False}], True)
    assert not clean_condition([main[0]] * 3, controls, True)
    assert not clean_condition(main[:2], controls, True)


def test_schedule_budget_and_nonnegative_retained_duration():
    row = schedule(200)
    assert row["cycle_us"] == 1500
    assert row["retained_us_per_visit"] == 190
    assert row["revisits_per_s"] == pytest.approx(666.6666667)
    assert row["nominal_retained_ms_per_port_in_50ms"] == pytest.approx(6.333333333)
    for dwell in (2, 10, 25, 50, 100, 200, 1000):
        row = schedule(dwell)
        assert row["retained_us_per_visit"] >= 0
        assert sum(row[key] for key in ("all_ports_retained_percent", "trim_percent",
                                        "guard_percent", "marker_percent")) == pytest.approx(100)
    assert schedule(25)["samples_per_retained_visit_10msps"] == 150
    with pytest.raises(ValueError, match="Invalid schedule"):
        schedule(0)


def test_noiseless_model_normalization_and_frequency_dependence():
    angles = np.array([70, 90, 110])
    low = array_response(915e6, angles)
    high = array_response(5811e6, angles)
    assert low[1] == pytest.approx(1)
    assert high[1] == pytest.approx(1)
    assert np.all(high[[0, 2]] < low[[0, 2]])
    assert np.all((low >= 0) & (low <= 1 + 1e-12))


def test_frozen_inputs_verify():
    verify_inputs(json.loads((DEST / "data/source-lock.json").read_text()))


def test_hash_change_is_rejected(tmp_path):
    lock = json.loads((DEST / "data/source-lock.json").read_text())
    name = INPUTS[0]
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("changed")
    with pytest.raises(ValueError, match="Input SHA-256 mismatch"):
        verify_inputs(lock, root=tmp_path)


def test_missing_lock_inventory_is_rejected():
    with pytest.raises(ValueError, match="inventory mismatch"):
        verify_inputs({"schema": 1, "inputs": {}})


def test_cross_band_conditions_do_not_promote_missing_or_failed_evidence():
    trials, conditions = collect()
    assert len(trials) == 117  # 39 rolling + 9 sub-GHz + 69 fixed-policy records.
    assert len(conditions) == 29
    sub = [r for r in conditions if r["frequency_mhz"] == 915]
    assert {r["dwell_us"] for r in sub} == {200, 1000}
    assert all(r["clean_condition_pass"] for r in sub)
    a = [r for r in conditions if r["frequency_mhz"] == 2475 and r["configuration"] == "A"]
    b = [r for r in conditions if r["frequency_mhz"] == 2475 and r["configuration"] == "B"]
    assert {r["dwell_us"] for r in a if r["clean_condition_pass"]} == {100, 200, 1000}
    assert {r["dwell_us"] for r in b if r["clean_condition_pass"]} == {200, 1000}
    fixed = [r for r in conditions if r["method"] == "fixed_any_budget"]
    assert not any(r["clean_condition_pass"] for r in fixed)
    assert all(r["phase_rms_deg"] is None for r in trials if r["method"] == "fixed_any_budget")
    assert all(len(r["observable_ports"].split()) == 5 for r in a + b)
