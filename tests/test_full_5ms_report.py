import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import report_full_5ms_campaign as report  # noqa: E402


@pytest.fixture
def result():
    return {
        "conditions": [
            {
                "block": "test",
                "configuration": "D",
                "frequency_hz": 915000000,
                "dwell_us": 200,
                "main_attempts": 3,
                "main_passes": 3,
                "clean_condition_pass": True,
            }
        ],
        "rolling": [
            {
                "block": "test",
                "configuration": "D",
                "frequency_hz": 915000000,
                "dwell_us": 200,
                "control": False,
                "base_pass": True,
                "bearing_pass": False,
                "base_rms_deg": float(i + 1),
                "bearing_valid_percent": 0,
                "bearing_rms_deg": 0.5,
                "p95_compute_ms": 200,
            }
            for i in range(3)
        ],
    }


def test_stable_phase_does_not_qualify_rejected_bearing(result):
    row = report.condition_details(result)[0]
    assert row["base_rms_deg_median"] == 2
    assert row["bearing_passes"] == 0
    assert row["clean_phase_and_bearing"] is False


def test_metric_figures_share_scales_and_do_not_show_negative_rms(result, monkeypatch, tmp_path):
    figures = {}
    monkeypatch.setattr(report, "rows", lambda _path: [])
    monkeypatch.setattr(report, "figure", lambda fig, _png, name: figures.update({name: fig}))
    try:
        report.metrics_figures(result, tmp_path)
        for name in ("fig02_phase_gain.png", "fig03_bearing.png"):
            axes = figures[name].axes
            assert len({ax.get_ylim() for ax in axes[:4]}) == 1
            assert axes[0].get_ylim()[0] == 0
            assert "bearing admission is separate" in figures[name]._suptitle.get_text()
    finally:
        for fig in figures.values():
            report.plt.close(fig)


def test_dense_figures_use_common_tx1_tx2_scales(monkeypatch, tmp_path):
    figures = []
    monkeypatch.setattr(report, "rows", lambda _path: [])
    monkeypatch.setattr(report, "figure", lambda fig, _png, _name: figures.append(fig))
    data = [
        {
            "tx_port": tx,
            "frequency_hz": 5800000000,
            "full_bearing_deg": 100,
            "full_bearing_residual_phase_rms_deg": residual,
            "phase_10deg_wall_latency_ms": latency,
            "full_bearing_valid": False,
        }
        for tx, residual, latency in (("TX1", 70, 100), ("TX2", 20, 3))
    ]
    try:
        report.dense_figure(data, tmp_path)
        axes = figures[0].axes
        for i in (0, 2, 4):
            assert axes[i].get_ylim() == axes[i + 1].get_ylim()
    finally:
        for fig in figures:
            report.plt.close(fig)


def test_coverage_keeps_rejected_and_unattempted_outputs(tmp_path):
    cfg = {"duration_s": 4, "frequency_hz": 5800000000, "name": "B", "dwell_us": 100}
    good = {"status": "analyzed"}
    bad = {"status": "analysis-failed", "error": "harmonic disagreement"}
    timing = tmp_path / "timing.json"
    report.save(
        timing,
        {
            "rows": [
                {
                    "configuration": cfg,
                    "round": i,
                    "control": False,
                    "run_json": str(i),
                    "status": "analyzed" if i < 3 else "analysis-failed",
                    "error": None if i < 3 else "harmonic disagreement",
                    "rolling_past_only": {"windows": windows},
                }
                for i, windows in ((1, [good] * 60), (2, [good] * 59 + [bad]), (3, []))
            ]
        },
    )
    report.save(
        tmp_path / "analysis/follower.json",
        {"blocks": [{"timing_json": str(timing), "timing_sha256": report.sha256(timing)}]},
    )
    coverage, failures = report.analysis_coverage(tmp_path)
    assert coverage["expected_windows"] == 180
    assert coverage["analyzed_windows"] == 119
    assert coverage["failed_windows"] == 1
    assert coverage["unattempted_windows"] == 60
    assert coverage["rejected_records"] == 1
    assert len(failures) == 2
    report.save(timing, {"rows": []})
    with pytest.raises(ValueError, match="hash changed"):
        report.analysis_coverage(tmp_path)


def test_joint_requires_all_main_trials_and_control_brackets(result):
    for row in result["rolling"]:
        row["bearing_pass"] = True
    assert report.condition_details(result)[0]["clean_phase_and_bearing"]
    result["conditions"][0]["clean_condition_pass"] = False
    assert not report.condition_details(result)[0]["clean_phase_and_bearing"]


def test_missing_metrics_not_zero_or_success(result):
    result["rolling"][0]["base_rms_deg"] = None
    result["rolling"][0]["bearing_pass"] = None
    row = report.condition_details(result)[0]
    assert row["base_rms_deg_reported_trials"] == 2
    assert row["base_rms_deg_median"] == 2.5
    assert row["main_attempts"] == 3
    assert not row["clean_phase_and_bearing"]


def test_controls_never_pooled_into_main_metrics(result):
    result["rolling"].append({**result["rolling"][0], "control": True, "base_rms_deg": 1000})
    assert report.condition_details(result)[0]["base_rms_deg_median"] == 2


def test_transport_accounting_keeps_failed_attempts_and_pending_requests(tmp_path):
    root = tmp_path / "transport-attempts"
    root.mkdir()
    attempt = {
        "status": "failed",
        "run_json": "failed.json",
        "sha256": "a",
        "error": {"message": "ENODATA"},
    }
    report.save(
        root / "recovered.json",
        {
            "status": "passed",
            "attempts": [
                attempt,
                {"status": "passed", "run_json": "good.json", "sha256": "b", "error": None},
            ],
        },
    )
    report.save(root / "running.json", {"status": "running", "attempts": []})
    report.save(root / "failed.json", {"status": "failed", "attempts": [attempt]})
    summary, rows = report.transport_summary(tmp_path)
    assert summary["recorded_attempts"] == 3
    assert summary["failed_attempts"] == 2
    assert summary["complete_requests"] == summary["recovered_requests"] == 1
    assert summary["failed_requests"] == summary["running_requests"] == 1
    assert len(rows) == 3


def test_paired_comparison_uses_same_round_and_dwell():
    main = {
        "block": "b",
        "configuration": "D",
        "frequency_hz": 5800000000,
        "dwell_us": 200,
        "round": 1,
        "control": False,
        "base_pass": True,
        "base_rms_deg": 8.0,
        "run_json": "main",
    }
    control = {
        **main,
        "configuration": "A",
        "control": True,
        "base_rms_deg": 9.0,
        "run_json": "control",
    }
    wrong_round = {**control, "round": 2, "base_rms_deg": 100.0}
    result = {
        "blocks": [{"id": "b", "bracket_pass": True}],
        "rolling": [main, control, wrong_round],
    }
    pairs = report.paired_controls(result)
    assert len(pairs) == 1
    assert pairs[0]["phase_rms_difference_deg"] == -1
    assert pairs[0]["pair_phase_gain_bracket_pass"]
    result["blocks"][0]["bracket_pass"] = False
    assert not report.paired_controls(result)[0]["pair_phase_gain_bracket_pass"]
    result["rolling"] = [main, wrong_round]
    with pytest.raises(ValueError, match="same-round"):
        report.paired_controls(result)
