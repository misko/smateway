"""Ensure the published report's decisions agree with its checked evidence tables."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "docs/completed_switching_analysis"


def read(name):
    return json.loads((ROOT / "data" / name).read_text())


def test_completed_report_keeps_all_fresh_trials_and_failures():
    summary = read("analysis-summary.json")
    assert summary["status"] == "completed-offline-analysis"
    assert len(summary["blocks"]) == 8
    assert len(summary["fixed"]) == 125
    assert len(summary["rolling"]) == 39
    assert all(r["complete"] and r["recorded_windows"] == 60 for r in summary["rolling"])
    assert sum(r["base_pass"] for r in summary["rolling"]) == 28
    assert not any(r["bearing_pass"] for r in summary["rolling"])
    passed = {
        (r["configuration"], r["dwell_us"])
        for r in summary["conditions"]
        if r["clean_condition_pass"]
    }
    assert passed == {("A", 100), ("A", 200), ("A", 1000), ("B", 200), ("B", 1000)}
    assert all(r["unobservable_ports"] == "ANT5" for r in summary["rolling"])


def test_figures_links_and_registered_raw_audit_are_complete():
    manifest = read("figures-manifest.json")
    assert len(manifest["figures"]) == 9
    for row in manifest["figures"]:
        path = ROOT / row["path"]
        assert path.stat().st_size == row["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    audit = read("acquisition-audit.json")
    assert audit["passed"] and len(audit["rows"]) == 298 and audit["raw_files"] == 570
    for link in re.findall(r"\]\(([^)]+)\)", (ROOT / "README.md").read_text()):
        if not link.startswith(("https:", "http:", "#")):
            assert (ROOT / link.split("#")[0]).exists(), link


def test_muted_spectra_include_both_channel_readback_evidence():
    spectrum = read("source-muted-spectrum.json")
    muted = [r for r in spectrum["rows"] if not r["source_enabled"]]
    assert len(muted) == 9
    for row in muted:
        for key in ("initial_source_mute", "source_after_capture_mute", "final_source_mute"):
            readback = row["source_mute_readbacks"][key]
            assert readback["passed"]
            assert readback["dds_scales"] == [0.0] * 8
            assert readback["tx_gain_db"] == [-80.0, -80.0]
