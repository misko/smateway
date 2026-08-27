from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_hexcal_5g8_results.py"
SPEC = importlib.util.spec_from_file_location("hexcal_5g8_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_result_retains_rejected_scope_and_corrected_rerun() -> None:
    summary, phase = MODULE.load_results(MODULE.DEFAULT_SUMMARY, MODULE.DEFAULT_PHASE)

    assert summary["status"] == "rejected_before_timing_or_calibration"
    assert summary["experimental_scope"]["officially_qualified_operating_point"] is False
    assert summary["findings"]["all_off_amplitude_contrast_available"] is False
    assert summary["findings"]["timing_qualification_started"] is False
    assert summary["sweeps"][-1]["user_reported_tx2_antenna_state"] == "removed"
    assert summary["sweeps"][-1]["peak_abs_component_counts_by_gain_rx1_rx2"][-1][1] == 389
    assert phase["conclusions"]["may_be_used_as_array_calibration"] is False


def test_result_rejects_promotion_to_calibration(tmp_path: Path) -> None:
    summary = json.loads(MODULE.DEFAULT_SUMMARY.read_text(encoding="utf-8"))
    summary["experimental_scope"]["calibration_coefficients_released"] = True
    malformed = tmp_path / "promoted.json"
    malformed.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(MODULE.ResultFigureError, match="may not release coefficients"):
        MODULE.load_results(malformed, MODULE.DEFAULT_PHASE)


def test_renderer_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = tmp_path / "first-manifest.json"
    second_manifest = tmp_path / "second-manifest.json"

    first_document = MODULE.render_report(
        MODULE.DEFAULT_SUMMARY, MODULE.DEFAULT_PHASE, first, first_manifest
    )
    second_document = MODULE.render_report(
        MODULE.DEFAULT_SUMMARY, MODULE.DEFAULT_PHASE, second, second_manifest
    )

    assert first_document == second_document
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    for filename in MODULE.FIGURE_NAMES:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_committed_figures_match_results() -> None:
    MODULE.check_report(
        MODULE.DEFAULT_SUMMARY,
        MODULE.DEFAULT_PHASE,
        MODULE.DEFAULT_OUTPUT,
        MODULE.DEFAULT_MANIFEST,
    )
