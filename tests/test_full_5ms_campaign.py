import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_full_5ms_dense as dense_analysis  # noqa: E402
import run_5ms_full_campaign as campaign  # noqa: E402
from run_comprehensive_block import schedule  # noqa: E402


def test_all_centres_have_both_five_ms_configurations():
    assert len(campaign.BLOCKS) == 8
    assert {r[1] for r in campaign.BLOCKS} == {"B", "D"}
    assert sum(len(schedule(d, 1, c)) for _f, c, _g, d in campaign.BLOCKS) == 150
    assert {f for f, _c, _g, _d in campaign.BLOCKS if f < 1e9} == {915000000}


def test_capture_keeps_tx_choice_and_uses_requested_rate(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign.prior, "binding", lambda *a: ("protocol", "fixture", {}))
    cmd = campaign.command(
        tmp_path, 5800000000, 60, "D", "fast", "test", flash=tmp_path / "flash.json", tx=1
    )
    assert cmd[cmd.index("--configuration") + 1] == "D"
    assert cmd[cmd.index("--tx-channel") + 1] == "1"
    assert cmd[cmd.index("--duration-s") + 1] == "4"


def test_dense_restores_and_records_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign, "sha256", lambda _p: "hash")
    monkeypatch.setattr(campaign.prior, "_flash", lambda *a: tmp_path / "flash.json")
    monkeypatch.setattr(campaign, "command", lambda *a, **kw: [])
    restored = []

    def capture(_args):
        raise OSError("synthetic interruption")

    def restore(root, flash):
        restored.append(flash)
        return root / "restore.json"

    monkeypatch.setattr(campaign.prior, "capture", capture)
    monkeypatch.setattr(campaign.prior, "_restore", restore)
    with pytest.raises(OSError, match="synthetic"):
        campaign.dense(tmp_path)
    d = campaign.load(tmp_path / "dense.json")
    assert d["status"] == "failed" and d["restore"]["sha256"] == "hash"
    assert len(restored) == 1


def test_dense_analysis_rejects_two_ms_not_upsampled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dense_analysis,
        "verified_run",
        lambda _item: {"configuration": {"name": "A", "sample_rate_hz": 2000000}},
    )
    monkeypatch.setattr(dense_analysis, "contract", lambda _root: {"code": "hash"})
    item = {"run_json": str(tmp_path / "run.json"), "sha256": "hash"}
    result = dense_analysis.analyze(item, tmp_path)
    assert result["status"] == "analysis-failed"
    assert "Expected native 5 MS/s" in result["error"]


def test_dense_phase_failure_is_not_replaced_by_longer_unqualified_fit():
    item = {"frequency_hz": 5811000000, "tx_channel": 1, "run_json": "test", "sha256": "hash"}
    ref = {"bearing_deg": 180, "valid": True, "residual_phase_rms_deg": 5}
    result = {
        "status": "analyzed",
        "frequency_difference_qualified": False,
        "phase": {
            "integration_study": [
                {
                    "groups_per_port": 100,
                    "phase_rms_deg_power_weighted_ports": 1,
                    "measured_wall_latency_ms": 50,
                }
            ]
        },
        "historical_geometry": {"full_capture_reference": ref},
        "nominal_51mm": {"full_capture_reference": ref},
    }
    row = dense_analysis.compact(item, result)
    assert row["phase_10deg_wall_latency_ms"] is None
    assert row["full_bearing_valid"] is True  # Distinct gate, not a phase qualification.
