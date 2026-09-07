from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from pluto_plus.hardware import SampleBlockV2

from smateway.tracking import BoardCalibrationLut, load_ism_band_profiles

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_continuous_tracking_screen.py"
PLAN = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
LUT = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location("run_continuous_tracking_screen", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block(sequence: int, first_sample: int) -> SampleBlockV2:
    return SampleBlockV2(
        utc_ns=1_000_000_000 + sequence,
        samples=np.ones((2, 100_000), dtype=np.complex64),
        stream_id=9,
        buffer_sequence=sequence,
        first_sample_sequence=first_sample,
        metadata_flags=0,
        metadata_abi=2,
        missing_samples_before=0,
        sample_time_realtime_start_ns=2_000_000_000 + sequence * 50_000_000,
        sample_time_realtime_end_ns=2_050_000_000 + sequence * 50_000_000,
        sample_time_uncertainty_ns=1000,
    )


def test_capture_block_validation_requires_sample_counter_continuity() -> None:
    module = _load_script()
    first = _block(0, 400_000)
    second = _block(1, 500_000)
    module._validate_block(first, None)
    module._validate_block(second, first)

    discontinuous = _block(2, 600_001)
    with pytest.raises(RuntimeError, match="continuity"):
        module._validate_block(discontinuous, second)


def test_complex_aggregation_handles_phase_wrap() -> None:
    module = _load_script()
    values = [
        (np.exp(1j * np.deg2rad(179.0)), 1.0, 1000),
        (np.exp(1j * np.deg2rad(-179.0)), 1.0, 1000),
    ]

    mean, scatter, coherence = module._aggregate_complex(values)

    assert abs(abs(np.rad2deg(np.angle(mean))) - 180.0) < 1e-9
    assert scatter == pytest.approx(1.0)
    assert coherence == 1.0


class _FakeSource:
    def __init__(self) -> None:
        self.sample_rate = 0
        self.tx_rf_bandwidth = 0
        self.tx_lo = 0
        self.tx_hardwaregain_chan0 = -80.0
        self.tx_hardwaregain_chan1 = -80.0
        self.dds_scales = [0.0] * 8
        self.dds_frequencies = [0] * 8
        self.dds_phases = [0] * 8
        self.dds_enabled = [0] * 8

    def dds_single_tone(self, frequency: int, scale: float, *, channel: int) -> None:
        assert channel == 0
        self.dds_scales = [scale, 0.0, scale, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.dds_frequencies = [frequency] * 8
        self.dds_phases = [90_000, 0, 0, 0, 0, 0, 0, 0]
        self.dds_enabled = [1] * 8


def test_tx2_source_mode_keeps_frequency_separated_tx1_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    source = _FakeSource()
    monkeypatch.setattr(module, "_mute_readback", lambda _device: {"passed": True})

    readback = module._enable_source(
        source,
        frequency_hz=5_800_000_000,
        tx_channel=1,
        tx_gain_db=-35.0,
    )

    assert source.tx_hardwaregain_chan0 == -35.0
    assert source.tx_hardwaregain_chan1 == -35.0
    assert source.dds_scales == [0.25, 0.0, 0.25, 0.0, 0.25, 0.0, 0.25, 0.0]
    assert source.dds_phases == [0, 0, 90_000, 0, 90_000, 0, 0, 0]
    assert readback["reference_tone_readback_hz"] == -100_000
    assert readback["target_tone_readback_hz"] == 100_000
    assert readback["frequency_difference_readback_hz"] == 200_000


def test_433_tracking_capture_fails_closed_before_opening_hardware() -> None:
    module = _load_script()
    profile = load_ism_band_profiles(PLAN)["ism433-c6-v1"]
    lut = BoardCalibrationLut.load(LUT)
    args = argparse.Namespace(
        acknowledge_ota_authorization=True,
        scan_pairs=1,
        dwell_ms=250,
        tx_gain_db=-35.0,
        frequency_hz=433_920_000,
    )

    with pytest.raises(SystemExit, match="blocked for the current fixture"):
        module._assert_arguments(args, profile, lut)


def test_2450_tracking_capture_requires_the_declared_array_fixture() -> None:
    module = _load_script()
    profile = load_ism_band_profiles(PLAN)["ism2450-c6-v1"]
    lut = BoardCalibrationLut.load(LUT)
    args = argparse.Namespace(
        acknowledge_ota_authorization=True,
        scan_pairs=1,
        dwell_ms=250,
        tx_gain_db=-35.0,
        frequency_hz=2_425_000_000,
    )

    with pytest.raises(SystemExit, match="54 mm-radius C6 aperture"):
        module._assert_arguments(args, profile, lut)


def test_dense_5g8_opt_in_is_bounded_and_uses_a_100khz_lattice() -> None:
    module = _load_script()
    profile = load_ism_band_profiles(PLAN)["ism5800-c6-v1"]
    lut = BoardCalibrationLut.load(LUT)
    base = {
        "acknowledge_ota_authorization": True,
        "scan_pairs": 1,
        "dwell_ms": 100,
        "tx_gain_db": -35.0,
        "dense_5g8_campaign": True,
    }

    module._assert_arguments(
        argparse.Namespace(**base, frequency_hz=5_726_000_000), profile, lut
    )
    module._assert_arguments(
        argparse.Namespace(**base, frequency_hz=5_874_000_000), profile, lut
    )
    with pytest.raises(SystemExit, match="100 kHz lattice"):
        module._assert_arguments(
            argparse.Namespace(**base, frequency_hz=5_725_000_000), profile, lut
        )
    with pytest.raises(SystemExit, match="100 kHz lattice"):
        module._assert_arguments(
            argparse.Namespace(**base, frequency_hz=5_800_050_000), profile, lut
        )
