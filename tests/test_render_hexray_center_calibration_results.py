from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_hexray_center_calibration_results.py"
SPEC = importlib.util.spec_from_file_location("hexray_result_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_result_locks_passed_evidence_chain() -> None:
    document = MODULE.load_result(MODULE.DEFAULT_RESULT)

    assert document["source_commit"] == "89f8ed32f35a3c38e9c4df88e1a42e33d19805e4"
    assert document["firmware"]["full_flash_readback_verified"] is True
    assert document["stimulus_qualification"]["fixed_calibration_receiver_gain_db"] == 20
    assert document["stimulus_qualification"]["selected_tx_hardware_gain_db"] == -10.0

    timing = document["timing_qualification"]
    assert timing["sample_rate_hz"] == 2_000_000
    assert timing["receiver_gain_db"] == 30
    assert [item["complete_cycles"] for item in timing["replicates"]] == [300, 300]
    assert timing["passed"] is True

    run = document["calibration_run"]
    assert run["accepted_artifacts"] == 15
    assert run["unique_streams"] == 15
    assert run["retries"] == 0
    assert run["audit_issue_count"] == 0
    assert run["audit_passed"] is True
    assert run["final_exact_mute_passed"] is True


def test_result_locks_frequency_specific_coefficients_and_scope() -> None:
    document = MODULE.load_result(MODULE.DEFAULT_RESULT)
    results = document["frequency_results"]

    assert [item["center_frequency_hz"] for item in results] == [
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
        2_472_000_000,
        2_483_000_000,
    ]
    assert all(len(item["correction_gain_db"]) == 6 for item in results)
    assert all(len(item["correction_phase_deg"]) == 6 for item in results)
    assert all(item["passed"] is True for item in results)
    assert max(item["heldout_gain_span_db"] for item in results) < 0.012
    assert max(item["heldout_phase_rms_deg"] for item in results) < 0.022

    interpolation = document["leave_one_frequency_out_diagnostic"]
    assert interpolation["mandatory_gate"] is False
    assert interpolation["cross_frequency_interpolation_permitted"] is False
    assert max(item["phase_error_rms_deg"] for item in interpolation["folds"]) > 18.0


def test_result_rejects_a_missing_audit_pass(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_RESULT.read_text(encoding="utf-8"))
    document["calibration_run"]["audit_passed"] = False
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.ResultFigureError, match="audit and final mute"):
        MODULE.load_result(malformed)


def test_result_renderer_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = tmp_path / "first-manifest.json"
    second_manifest = tmp_path / "second-manifest.json"

    first_document = MODULE.render_report(MODULE.DEFAULT_RESULT, first, first_manifest)
    second_document = MODULE.render_report(MODULE.DEFAULT_RESULT, second, second_manifest)

    assert first_document == second_document
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    for filename in MODULE.FIGURE_NAMES:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_committed_result_figures_match_snapshot() -> None:
    MODULE.check_report(MODULE.DEFAULT_RESULT, MODULE.DEFAULT_OUTPUT, MODULE.DEFAULT_MANIFEST)
