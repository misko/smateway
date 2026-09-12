import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resume_jitter_campaign as resume  # noqa: E402


def test_inherit_keeps_old_failure_and_completed_blocks(tmp_path, monkeypatch):
    parent, root = tmp_path / "parent", tmp_path / "continuation"
    parent.mkdir()
    entries = []
    for i, (frequency, config, _gain, _dwells) in enumerate(resume.campaign.BLOCK_SETTINGS[:4]):
        path = parent / f"block-{i}.json"
        path.write_text(json.dumps({"status": "diagnostic-complete"}))
        entries.append(
            {
                "block_json": str(path),
                "sha256": resume.sha256(path),
                "frequency_hz": frequency,
                "configuration": config,
                "status": "diagnostic-complete",
            }
        )
    failed = {"status": "failed", "block_json": "original-failed-attempt"}
    (parent / "blocks.json").write_text(
        json.dumps({"status": "failed", "blocks": entries + [failed]})
    )
    for name in ("screens.json", "fixture-dual-band.json", "fixture-915.json"):
        (parent / name).write_text(json.dumps({"confirmed_at": "old"}))
    (parent / "initial-selector-flash.bin").write_bytes(b"original")
    monkeypatch.setattr(resume.campaign, "verify_screen", lambda _root: {})
    old = {p.name: p.read_bytes() for p in parent.iterdir()}
    result = resume.inherit(parent, root, "please continue", "new-confirmation")
    assert len(result) == 4
    assert {p.name: p.read_bytes() for p in parent.iterdir()} == old
    manifest = resume.load(root / "continuation.json")
    assert manifest["retained_failed_blocks"] == [failed]
    assert resume.load(root / "fixture-dual-band.json")["confirmed_at"] == "new-confirmation"
    with pytest.raises(FileExistsError):
        resume.inherit(parent, root, "please continue", "new-confirmation")


def test_completed_blocks_skip_rf_and_hash_changes_reject(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.campaign, "verify_screen", lambda _root: {})
    monkeypatch.setattr(resume.campaign, "BLOCK_SETTINGS", ((5811000000, "A", 60, (200,)),))
    path = tmp_path / "complete.json"
    path.write_text("{}")
    record = {
        "blocks": [
            {
                "frequency_hz": 5811000000,
                "configuration": "A",
                "status": "diagnostic-complete",
                "block_json": str(path),
                "sha256": resume.sha256(path),
            }
        ]
    }
    resume.campaign.blocks(tmp_path, record, lambda: None)
    path.write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        resume.campaign.blocks(tmp_path, record, lambda: None)
