import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_jitter_report as report  # noqa: E402
from render_completed_switching_analysis import collect  # noqa: E402


def test_csv_booleans_missing_values_and_numbers(tmp_path):
    path = tmp_path / "values.csv"
    path.write_text("passed,missing,count,name\nFalse,,3,ANT1\n")
    assert report.rows(path) == [{"passed": False, "missing": None, "count": 3, "name": "ANT1"}]


def test_selected_block_collection_never_reads_unfinished_blocks(tmp_path):
    (tmp_path / "block-bad.json").write_text("not json")
    result = collect(tmp_path, {"rows": []}, block_paths=[])
    assert result["blocks"] == []
    with pytest.raises(ValueError):
        collect(tmp_path, {"rows": []})


def test_fixed_phase_repeatability_alone_is_not_combined_pass():
    after = {
        "rolling": [],
        "fixed": [
            {
                "frequency_hz": 5800000000,
                "configuration": "A",
                "dwell_us": 200,
                "round": 1,
                "control": False,
                "any_window_pass": True,
                "passed": False,
                "block": "new",
                "run_json": "/test/run.json",
            }
        ],
    }
    result = [r for r in report.trial_rows(after) if r["epoch"] == "after"]
    assert len(result) == 1
    assert result[0]["method"] == "fixed_any_budget"
    assert result[0]["passed"] is False


def test_baseline_rows_are_separate_and_unchanged():
    result = report.trial_rows({"rolling": [], "fixed": []})
    assert len(result) == 117
    assert all(r["epoch"] == "before" for r in result)
    assert {r["method"] for r in result} == {"rolling_50ms", "fixed_any_budget"}


def test_generated_results_preserve_handwritten_report(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("Intro\n<!-- RESULTS:START -->old<!-- RESULTS:END -->\nConclusion")
    report.update_results(path, "new", False)
    assert path.read_text() == (
        "Intro\n<!-- RESULTS:START -->\nnew\n<!-- RESULTS:END -->\nConclusion"
    )
    path.write_text("handwritten report without markers")
    with pytest.raises(ValueError, match="unambiguous"):
        report.update_results(path, "new", False)
    assert path.read_text() == "handwritten report without markers"


def test_completion_requires_full_grid_and_final_muted_restore(monkeypatch, tmp_path):
    dense = [
        {"frequency_hz": f, "tx_port": tx, "run_json": f"/test/{f}-{tx}"}
        for f in range(5726000000, 5874000001, 1000000)
        for tx in ("TX1", "TX2")
    ]
    restore = {
        "status": "passed",
        "completed_at": "test-time",
        "restored_flash": {
            "matches_backup": True,
            "size_bytes": 16384,
            "path": "/test/restored.bin",
            "sha256": "hash",
        },
        "selector_status": {"applied_code": 8, "command_code": 8, "lease_active": False},
        "final_radio_mute": [
            {"serial": serial, "tx_gain_db": [-80.0, -80.0], "dds_scales": [0.0] * 8}
            for serial in (
                "104000b29905000e17000800065934759d",
                "104473b80a16000de6ff2000f8a6beca79",
            )
        ],
    }
    documents = {
        "dense.json": {
            "captures": dense,
            "restore": {"path": "/test/restore.json", "sha256": "hash"},
        },
        "dense-summary.json": {"manifest_sha256": "hash"},
        "restore.json": restore,
    }
    monkeypatch.setattr(report, "load", lambda p: documents[p.name])
    monkeypatch.setattr(report, "sha256", lambda _p: "hash")
    assert report.verify_completion(tmp_path, dense)["both_pinned_radios_muted"]
    with pytest.raises(ValueError, match="grid"):
        report.verify_completion(tmp_path, dense[:-1] + [dense[0]])
    restore["final_radio_mute"][0]["dds_scales"][0] = 0.25
    with pytest.raises(ValueError, match="mute"):
        report.verify_completion(tmp_path, dense)
