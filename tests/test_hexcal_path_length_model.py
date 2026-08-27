from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_hexcal_path_length_model.py"
MARKDOWN_REPORT = (
    SCRIPT.parents[1]
    / "docs/hexray_tx_in_middle_calibration/path_length_inverse_report.md"
)
SPEC = importlib.util.spec_from_file_location("hexcal_path_length_model", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def test_phase_inverse_and_fixed_delay_recover_synthetic_path() -> None:
    frequencies = [2_400_000_000, 2_423_000_000, 2_440_000_000, 2_472_000_000]
    delay_ps = 85.0
    phases = [
        analysis.wrap_phase_deg(-360.0 * frequency * delay_ps * 1e-12)
        for frequency in frequencies
    ]

    result = analysis.fit_fixed_delay(phases, frequencies, prior_ps=84.0)

    assert result["fitted_delay_ps"] == pytest.approx(delay_ps, abs=1e-10)
    assert result["phase_residual_rms_deg"] == pytest.approx(0.0, abs=1e-10)
    assert result["inferred_delay_range_ps"] == pytest.approx(0.0, abs=1e-10)


def test_committed_evidence_rejects_one_delay_for_every_nonreference_antenna() -> None:
    report = analysis.build_report(
        analysis.DEFAULT_V22_RESULT,
        analysis.DEFAULT_5G8_PHASE,
        analysis.DEFAULT_DESIGN,
    )

    assert report["status"] == "single_fixed_path_delay_rejected_for_ant2_through_ant6"
    rows = report["antennas"]
    assert [row["name"] for row in rows] == [f"ANT{index}" for index in range(1, 7)]
    assert all(
        row["fixed_delay_model_within_diagnostic_tolerance"] is False for row in rows[1:]
    )
    assert min(row["phase_residual_rms_deg"] for row in rows[1:]) > 12.0
    assert report["common_frequency_structure"]["median_phase_residual_deg"] < -20.0
    assert all(row["diagnostic_5g8"]["admissible_calibration"] is False for row in rows)


def test_rejected_5g8_input_cannot_be_promoted(tmp_path: Path) -> None:
    phase = json.loads(analysis.DEFAULT_5G8_PHASE.read_text(encoding="utf-8"))
    phase["conclusions"]["may_be_used_as_array_calibration"] = True
    promoted = tmp_path / "promoted.json"
    promoted.write_text(json.dumps(phase), encoding="utf-8")

    with pytest.raises(analysis.PathLengthAnalysisError, match="must remain rejected"):
        analysis.build_report(analysis.DEFAULT_V22_RESULT, promoted, analysis.DEFAULT_DESIGN)


def test_committed_analysis_and_figure_are_reproducible() -> None:
    analysis.check(
        analysis.DEFAULT_V22_RESULT,
        analysis.DEFAULT_5G8_PHASE,
        analysis.DEFAULT_DESIGN,
        analysis.DEFAULT_OUTPUT,
        analysis.DEFAULT_FIGURE,
        analysis.DEFAULT_MANIFEST,
    )


def test_markdown_report_retains_headline_results_and_evidence_boundary() -> None:
    report = MARKDOWN_REPORT.read_text(encoding="utf-8")

    assert "One static relative path delay per antenna does **not** explain" in report
    assert "| ANT2 | 77.48 ps | 23.23 mm | 18.25° | reject |" in report
    assert "| ANT5 | 80.65 ps | 24.18 mm | 12.29° | reject |" in report
    assert "median of `-28.84°`" in report
    assert "diagnostic fingerprints—not admitted connector phases" in report
