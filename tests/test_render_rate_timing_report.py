import importlib.util
import json
from pathlib import Path

import pytest

from smateway.rate_timing import PORTS, RECEIVER_SERIAL, SOURCE_SERIAL, complex_json, sha256

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rate_report", ROOT / "scripts/render_rate_timing_report.py"
)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def test_empty_report_does_not_claim_acquisition_passes(tmp_path):
    campaign = {
        "campaign_id": "empty-test",
        "status": "prepared",
        "screen": [],
        "references": [],
        "selection": None,
        "restores": [],
    }
    (tmp_path / "campaign.json").write_text(json.dumps(campaign))
    output = tmp_path / "report"
    report.render(tmp_path, output)
    text = (output / "README.md").read_text()
    assert "| A | 2 | 1.6 | pending |" in text
    assert "| C | 10 | 1.6 | pending |" in text
    summary = json.loads((output / "data/summary.json").read_text())
    assert summary["validation"] is None
    assert summary["preflight"] == []
    assert (output / "png/fig06_acquisition.png").exists()


def test_reference_figures_keep_frozen_a_mask_even_if_b_degrades(tmp_path):
    refs = []
    for name in ("A", "B"):
        for i, port in enumerate(PORTS):
            refs.append(
                {
                    "port": port,
                    "configuration": name,
                    "position": "before",
                    "run_json": str(tmp_path / f"{name}-{port}.json"),
                    "reference": {
                        "transfer": complex_json(0.001 if name == "B" and i == 0 else 1),
                        "phase_rms_10ms_deg": 2,
                        "coherence": 0.9,
                    },
                }
            )
    data, png = tmp_path / "data", tmp_path / "png"
    data.mkdir()
    png.mkdir()
    rows, frozen = report.reference_outputs({"references": refs}, data, png)
    assert all(frozen["observable"])
    b1 = next(r for r in rows if r["configuration"] == "B" and r["port"] == "ANT1")
    assert b1["observable"]
    assert b1["transfer_amplitude"] == 0.001
    assert (png / "fig07_static_references.png").exists()


def test_missing_decoder_variant_remains_missing():
    assert report.variant({"status": "analysis-failed", "variants": []}) is None


def evidence_fixture(tmp_path):
    raw = []
    for name in ("rx1", "rx2"):
        path = tmp_path / f"{name}.cf32"
        path.write_bytes(bytes(32))
        raw.append({"path": str(path), "sha256": sha256(path)})
    run = {
        "status": "passed",
        "identities": {"receiver_serial": RECEIVER_SERIAL, "source_serial": SOURCE_SERIAL},
        "configuration": {
            "name": "A",
            "frequency_hz": 5_800_000_000,
            "duration_s": 2e-6,
            "sample_rate_hz": 2_000_000,
            "frames": 1,
            "frame_samples": 4,
        },
        "capture": {
            "samples_per_channel": 4,
            "clipped_samples": [0, 0],
            "raw": raw,
            "timeline": [
                {
                    "buffer_sequence": 0,
                    "stream_id": 2,
                    "missing_samples_before": 0,
                    "overflow_observed": False,
                    "first_sample_sequence": 40,
                    "last_sample_sequence_exclusive": 44,
                }
            ],
        },
        "safety": {"final_source_mute": {"passed": True}},
        "source_contract": {"capture.py": "example"},
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run))
    return {
        "references": [{"configuration": "A", "run_json": str(path), "run_sha256": sha256(path)}],
        "screen": [],
    }


def test_raw_replay_audit_rejects_tamper(tmp_path):
    campaign = evidence_fixture(tmp_path)
    verified = report.verify_evidence(campaign)
    assert verified["raw_bytes_rehashed"] == 64
    (tmp_path / "rx1.cf32").write_bytes(bytes([1]) * 32)
    with pytest.raises(ValueError, match="raw sample length/hash"):
        report.verify_evidence(campaign)


def test_raw_replay_audit_rejects_duplicate_captures(tmp_path):
    campaign = evidence_fixture(tmp_path)
    campaign["references"].append(campaign["references"][0])
    with pytest.raises(ValueError, match="reused"):
        report.verify_evidence(campaign)
