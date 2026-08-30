from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_pinned_static_screen.py"
SPEC = importlib.util.spec_from_file_location("run_pinned_static_screen_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "mode": "external",
        "frequency_hz": [5_800_000_000],
        "state": ["ALL_OFF", "ANT1"],
        "repeats": 3,
        "tx_gain_db": -40.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repeats": 0}, "repeats must be 1..10"),
        ({"repeats": 11}, "repeats must be 1..10"),
        ({"frequency_hz": [6_000_000_001]}, "AD9361 70 MHz..6 GHz"),
        ({"tx_gain_db": -34.9}, "TX gain must remain within"),
        ({"tx_gain_db": -80.1}, "TX gain must remain within"),
        ({"mode": "muted", "state": ["ANT1"]}, "muted capture permits only ALL_OFF"),
    ],
)
def test_argument_safety_bounds(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        runner._assert_args(_args(**overrides))


def test_exact_mute_readback() -> None:
    device = SimpleNamespace(
        tx_hardwaregain_chan0=-80.0,
        tx_hardwaregain_chan1=-80.0,
        dds_scales=[0.0] * 8,
    )
    assert runner._readback_mute(device)["passed"] is True

    device.dds_scales[3] = 0.01
    with pytest.raises(RuntimeError, match="exact radio mute readback failed"):
        runner._readback_mute(device)


def test_coherent_transfer_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_count = 160_000
    monkeypatch.setattr(runner, "SAMPLE_COUNT", sample_count)
    indices = np.arange(sample_count, dtype=np.float64)
    tone = np.exp(2j * np.pi * runner.TONE_OFFSET_HZ * indices / runner.SAMPLE_RATE_HZ)
    expected = 0.1 * np.exp(1j * np.deg2rad(37.0))
    samples = np.asarray([100.0 * tone, 100.0 * expected * tone], dtype=np.complex64)

    result = runner._project_tone(samples, muted=False)
    transfer = result["transfer_rx2_over_rx1"]

    assert transfer["magnitude_db"] == pytest.approx(-20.0, abs=0.01)
    assert transfer["phase_deg"] == pytest.approx(37.0, abs=0.01)


def test_muted_projection_does_not_invent_transfer() -> None:
    samples = np.zeros((2, runner.SAMPLE_COUNT), dtype=np.complex64)
    result = runner._project_tone(samples, muted=True)
    assert set(result) == {"peak_component_counts", "rms_counts"}


def test_pinned_bench_identities() -> None:
    assert runner.RADIO_URI == "ip:192.168.1.15"
    assert runner.RADIO_SERIAL == "104000b29905000e17000800065934759d"
    assert runner.SOURCE_URI == "ip:192.168.1.173"
    assert runner.SOURCE_SERIAL == "104473b80a16000de6ff2000f8a6beca79"
    assert runner.STLINK_SERIAL == "002D003A3335511035383531"
