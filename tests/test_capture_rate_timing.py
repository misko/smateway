import importlib.util
import signal
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


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_identity_is_retained_before_normal_cleanup_path(signum):
    events = []
    with pytest.raises(KeyboardInterrupt):
        capture.record_interrupt(signum, events)
    assert events[0]["signal"] == signal.Signals(signum).name
    assert events[0]["number"] == signum
    assert events[0]["received_at"].endswith("+00:00")


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


def test_ambient_mode_is_explicit_and_not_a_tx1_reference():
    args = capture.parser().parse_args(
        [
            "--mode",
            "ambient",
            "--configuration",
            "A",
            "--port",
            "ANT1",
            "--output-root",
            "/tmp/unused-synthetic-test",
        ]
    )
    assert args.mode == "ambient"
    assert not args.acknowledge_ota_authorization


def test_gain_telemetry_retains_available_fields_without_inventing_endpoints():
    block = SimpleNamespace(
        tandem_metadata=SimpleNamespace(
            initial_gain_db=30,
            rx1_gain_index=35,
            rx2_gain_index=35,
            tandem_transition_count=0,
        )
    )
    result = capture.gain_telemetry(block)
    assert result["available"] and result["initial_gain_db"] == 30
    assert result["rx1_gain_index"] == result["rx2_gain_index"] == 35
    assert "rx1_gain_db_start" not in result
    assert capture.gain_telemetry(SimpleNamespace()) == {"available": False}
