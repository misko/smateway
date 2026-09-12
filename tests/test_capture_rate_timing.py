import importlib.util
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
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


@pytest.mark.parametrize("mode", ["muted", "ambient", "fast-ambient"])
def test_muted_controls_cannot_enter_source_enable_path(mode):
    assert not capture.source_enabled(mode)


def test_fast_ambient_is_explicit_not_a_fast_tx1_capture():
    args = capture.parser().parse_args(
        [
            "--configuration",
            "A",
            "--mode",
            "fast-ambient",
            "--output-root",
            "/tmp/test-switched-muted",
        ]
    )
    assert args.mode == "fast-ambient"
    assert not args.acknowledge_ota_authorization
    assert not capture.source_enabled(args.mode)


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


def test_iq_staging_is_opt_in_and_bounded(monkeypatch):
    monkeypatch.delenv("SMATEWAY_STAGE_IQ_IN_RAM", raising=False)
    assert capture.allocate_iq_stage(4, "fast") is None
    monkeypatch.setenv("SMATEWAY_STAGE_IQ_IN_RAM", "1")
    assert capture.allocate_iq_stage(4, "fast").shape == (2, 4)
    assert capture.allocate_iq_stage(4, "muted") is None
    with pytest.raises(ValueError, match="512 MiB"):
        capture.allocate_iq_stage(40_000_000, "fast")
    monkeypatch.setenv("SMATEWAY_STAGE_IQ_IN_RAM", "yes")
    with pytest.raises(ValueError, match="0 or 1"):
        capture.allocate_iq_stage(4, "fast")


def test_staged_iq_writes_only_received_samples_and_keeps_channels(tmp_path):
    paths = [tmp_path / name for name in ("rx1.cf32", "rx2.cf32")]
    for path in paths:
        path.touch()
    staged = np.array([[1 + 2j, 3 + 4j, 99], [5 + 6j, 7 + 8j, 99]], np.complex64)
    capture.persist_iq_stage(paths, staged, 2)
    for path, expected in zip(paths, staged, strict=True):
        np.testing.assert_array_equal(np.fromfile(path, dtype=np.complex64), expected[:2])
    capture.persist_iq_stage(paths, staged, 2)  # A cleanup call must not append twice.
    assert all(p.stat().st_size == 16 for p in paths)


def test_staged_partial_write_is_not_overwritten(tmp_path):
    path = tmp_path / "partial.cf32"
    path.write_bytes(b"incomplete")
    with pytest.raises(RuntimeError, match="without overwrite"):
        capture.persist_iq_stage([path], np.zeros((1, 4), np.complex64), 4)
    assert path.read_bytes() == b"incomplete"
