import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "rate_campaign", ROOT / "scripts/run_rate_timing_campaign.py"
)
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


def row(tmp_path, name, dwell, round_number, latency, passed=True):
    path = tmp_path / f"{name}-{dwell}-{round_number}.json"
    path.write_text(
        json.dumps(
            {
                "variants": [
                    {
                        "method": "legacy",
                        "leading_discard_us": 5,
                        "metrics": {"passed": passed, "first_phase_pass_ms": latency},
                    }
                ]
            }
        )
    )
    return {
        "configuration": name,
        "dwell_us": dwell,
        "round": round_number,
        "control": False,
        "analysis_json": str(path),
        "run_json": str(path),
    }


def test_validation_cannot_select_a_different_winner(tmp_path):
    rows = [
        row(tmp_path, "A", 200, 1, 50),
        row(tmp_path, "B", 50, 1, 20),
        row(tmp_path, "B", 25, 1, 10, False),
        row(tmp_path, "B", 25, 2, 1, True),
        row(tmp_path, "B", 50, 2, 21),
        row(tmp_path, "B", 50, 3, 22),
    ]
    candidate = campaign.select_candidate(rows)
    assert candidate["dwell_us"] == 50
    assert campaign.evaluate_candidate(candidate, rows)["passed"]


def test_two_validation_repeats_are_required(tmp_path):
    rows = [
        row(tmp_path, "A", 200, 1, 50),
        row(tmp_path, "B", 50, 1, 20),
        row(tmp_path, "B", 50, 2, 21),
    ]
    candidate = campaign.select_candidate(rows)
    assert not campaign.evaluate_candidate(candidate, rows)["passed"]


def test_one_failed_validation_rejects_candidate(tmp_path):
    rows = [
        row(tmp_path, "A", 200, 1, 50),
        row(tmp_path, "B", 50, 1, 20),
        row(tmp_path, "B", 50, 2, 21),
        row(tmp_path, "B", 50, 3, 22, False),
    ]
    assert not campaign.evaluate_candidate(campaign.select_candidate(rows), rows)["passed"]


def test_no_baseline_closure_means_no_promoted_winner(tmp_path):
    rows = [row(tmp_path, "A", 200, 1, 50, False), row(tmp_path, "B", 25, 1, 2)]
    assert campaign.select_candidate(rows) is None


def test_control_capture_cannot_be_reused_as_training(tmp_path, monkeypatch):
    c = campaign.Campaign(tmp_path)
    c.current_dwell = 200
    c.flash = tmp_path / "flash.json"
    control = tmp_path / "captures/screen-r1-A-200us-control-20260908T070000.123456Z/run.json"
    control.parent.mkdir(parents=True)
    control.write_text(
        json.dumps(
            {
                "status": "passed",
                "configuration": {
                    "name": "A",
                    "mode": "fast",
                    "duration_s": 4,
                    "port": None,
                    "dwell_us": 200,
                },
            }
        )
    )
    calls = []

    def capture_new(command):
        calls.append(command)
        target = tmp_path / "captures/screen-r1-A-200us-20260908T070001.123456Z/run.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"status":"passed"}')
        return f"run_dir={target.parent}\n"

    monkeypatch.setattr(campaign, "execute", capture_new)
    result = c.capture("screen-r1-A-200us", "A", "fast", 4)
    assert calls
    assert result != control


def test_duplicate_capture_rejected_even_if_ledger_is_corrupt(tmp_path):
    r = row(tmp_path, "A", 200, 1, 50)
    duplicate = {**r, "round": 2}
    with pytest.raises(ValueError, match="reused"):
        campaign.select_candidate([r, duplicate])


def test_decoder_failure_is_a_failed_holdout_not_an_exception(tmp_path):
    rows = [
        row(tmp_path, "A", 200, 1, 50),
        row(tmp_path, "B", 50, 1, 20),
        row(tmp_path, "B", 50, 2, 21),
        row(tmp_path, "B", 50, 3, 22),
    ]
    candidate = campaign.select_candidate(rows)
    Path(rows[-1]["analysis_json"]).write_text('{"variants": [], "error": "marker not found"}')
    result = campaign.evaluate_candidate(candidate, rows)
    assert not result["passed"]
    assert result["validation"][-1]["analysis_error"] == "marker not found"


def test_two_captures_from_same_holdout_round_do_not_qualify(tmp_path):
    rows = [
        row(tmp_path, "A", 200, 1, 50),
        row(tmp_path, "B", 50, 1, 20),
        row(tmp_path, "B", 50, 2, 21),
        row(tmp_path, "B", 50, 3, 22),
    ]
    candidate = campaign.select_candidate(rows)
    rows[-1]["round"] = 2
    assert not campaign.evaluate_candidate(candidate, rows)["passed"]
