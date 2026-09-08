import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "rate_capture", ROOT / "scripts/capture_rate_timing.py"
)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_wrong_source_is_released_without_rf_control(monkeypatch):
    device = SimpleNamespace(ctx=SimpleNamespace(attrs={"hw_serial": "another-radio"}))
    adi = SimpleNamespace(ad9361=lambda **_kwargs: device)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _name: adi)
    released, muted = [], []
    monkeypatch.setattr(capture, "_release_device", released.append)
    monkeypatch.setattr(capture, "_mute_readback", muted.append)
    with pytest.raises(RuntimeError, match="source serial differs"):
        capture.open_source("ip:192.168.1.179")
    assert released == [device]
    assert muted == []


def test_matching_source_is_admitted_for_control(monkeypatch):
    device = SimpleNamespace(ctx=SimpleNamespace(attrs={"hw_serial": capture.SOURCE_SERIAL}))
    adi = SimpleNamespace(ad9361=lambda **_kwargs: device)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _name: adi)
    admitted, facts = capture.open_source("ip:192.168.1.179")
    assert admitted is device
    assert facts["hw_serial"] == capture.SOURCE_SERIAL
