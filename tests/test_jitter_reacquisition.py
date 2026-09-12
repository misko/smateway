import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_jitter_reacquisition as campaign  # noqa: E402


def test_campaign_repeats_prior_scope_not_extra_subghz():
    assert campaign.SCREEN_SETTINGS == (
        (5800000000, 60), (2475000000, 40), (5811000000, 60), (915000000, 50))
    low = [r for r in campaign.BLOCK_SETTINGS if r[0] < 1000000000]
    assert low == [(915000000, "A", 50, (200, 1000))]
    assert len(campaign.BLOCK_SETTINGS) == 7
    assert all(config in ("A", "B", "D") for _, config, _, _ in campaign.BLOCK_SETTINGS)


def test_capture_arguments_bind_fixture_and_preserve_two_source_mode(monkeypatch, tmp_path):
    calls = []

    def binding(root, frequency):
        calls.append((root, frequency))
        return tmp_path / "protocol.json", tmp_path / "fixture.json", {}

    monkeypatch.setattr(campaign, "binding", binding)
    args = campaign.capture_args(tmp_path, 5811000000, 60, "fast", tag="test",
                                 flash=tmp_path / "flash.json", tx=1)
    assert calls == [(tmp_path, 5811000000)]
    assert args[args.index("--tx-channel") + 1] == "1"
    assert args[args.index("--duration-s") + 1] == "4"
    assert args[args.index("--gain-db") + 1] == "60"
    assert args[args.index("--configuration") + 1] == "A"
    assert "--fixture-json" in args and "--profile" in args


def test_dense_restores_after_capture_failure(monkeypatch, tmp_path):
    restored = []
    monkeypatch.setattr(campaign, "verify_screen", lambda _root: {})
    monkeypatch.setattr(campaign, "sha256", lambda _path: "testhash")
    monkeypatch.setattr(campaign, "_flash", lambda _root, _dwell: tmp_path / "flash.json")
    monkeypatch.setattr(campaign, "capture_args", lambda *_args, **_kw: [])

    def capture(_command):
        raise RuntimeError("synthetic acquisition failure")

    def restore(_root, flash):
        restored.append(flash)
        return tmp_path / "restore.json"

    monkeypatch.setattr(campaign, "capture", capture)
    monkeypatch.setattr(campaign, "_restore", restore)
    record = {"captures": []}
    with pytest.raises(RuntimeError, match="synthetic acquisition failure"):
        campaign.dense(tmp_path, record, lambda: None)
    assert restored == [tmp_path / "flash.json"]
    assert record["restore"]["sha256"] == "testhash"
