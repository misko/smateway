from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/attest_selector_flash_pluto_mute.py"
SPEC = importlib.util.spec_from_file_location("attest_selector_flash_pluto_mute_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = producer
SPEC.loader.exec_module(producer)

SERIAL = "104000b29905000e17000800065934759d"
URI = "usb:1.2.3"


def _readback(checkpoint: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
        "purpose": checkpoint,
        "status": "passed",
        "serial": SERIAL,
        "uri": URI,
        "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
        "dds_raw_readback": [0.0] * 8,
        "dds_scale_readback": [0.0] * 8,
        "dds_enabled_readback": [False] * 8,
        "started_at": "2026-08-30T12:00:00+00:00",
        "completed_at": "2026-08-30T12:00:01+00:00",
        "error": None,
    }


def test_producer_writes_create_only_source_bound_exact_mute(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    source = {
        "repository": str(producer._REPOSITORY),
        "commit": "a" * 40,
        "clean_worktree_verified": True,
    }
    monkeypatch.setattr(producer, "_source_identity", lambda *_args: source)
    fake = SimpleNamespace(
        _strict_exact_mute=lambda serial, uri, purpose: _readback(purpose),
        _mute_passed=lambda value, **_kwargs: value["status"] == "passed",
    )
    monkeypatch.setattr(producer.importlib, "import_module", lambda _name: fake)
    output = tmp_path / "phase1-mute.json"

    assert (
        producer.main(
            [
                "--checkpoint",
                "phase1_pre_openocd",
                "--serial",
                SERIAL,
                "--uri",
                URI,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert result["openocd_access"] is False
    assert result["rf_transmission"] is False
    assert result["owner_writable"] is False
    assert document["source"] == source
    assert document["tx_hardware_gain_db_by_channel"] == [-80.0, -80.0]
    assert document["dds_scale_readback"] == [0.0] * 8
    assert output.stat().st_mode & 0o200 == 0

    assert (
        producer.main(
            [
                "--checkpoint",
                "phase1_pre_openocd",
                "--serial",
                SERIAL,
                "--uri",
                URI,
                "--output",
                str(output),
            ]
        )
        == 2
    )


def test_producer_publishes_nothing_when_exact_mute_fails(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        producer,
        "_source_identity",
        lambda *_args: {
            "repository": str(producer._REPOSITORY),
            "commit": "a" * 40,
            "clean_worktree_verified": True,
        },
    )
    failed = _readback("phase2_pre_openocd")
    failed["status"] = "failed"
    fake = SimpleNamespace(
        _strict_exact_mute=lambda *_args: failed,
        _mute_passed=lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(producer.importlib, "import_module", lambda _name: fake)
    output = tmp_path / "phase2-mute.json"

    assert (
        producer.main(
            [
                "--checkpoint",
                "phase2_pre_openocd",
                "--serial",
                SERIAL,
                "--uri",
                URI,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
