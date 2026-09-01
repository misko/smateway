#!/usr/bin/env python3
"""Run a small fail-muted static selector screen with pinned network Plutos."""

from __future__ import annotations

import argparse
import errno
import json
import math
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import adi
import numpy as np
from pluto_plus.hardware.iio import _mute_transmit

from smateway.bench import BenchManifest, OpenOcdBench
from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.profile import load_profile

RADIO_URI = "ip:192.168.1.15"
RADIO_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.173"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
STLINK_SERIAL = "002D003A3335511035383531"

SAMPLE_RATE_HZ = 2_000_000
BANDWIDTH_HZ = 1_600_000
TONE_OFFSET_HZ = 100_000
SAMPLE_COUNT = 262_144
RX_GAIN_DB = 60
DDS_SCALE = 0.25
MAX_TX_GAIN_DB = -35.0
SELECTOR_LEASE_MS = 5_000
RX_BUSY_RETRIES = 3
RX_BUSY_RETRY_DELAY_S = 0.25


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("muted", "tone", "external"))
    parser.add_argument(
        "--frequency-hz",
        type=int,
        action="append",
        required=True,
        help="repeat for multiple center frequencies",
    )
    parser.add_argument(
        "--state",
        action="append",
        choices=("ALL_OFF", *(f"ANT{i}" for i in range(1, 9))),
        default=None,
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--tx-gain-db", type=float, default=MAX_TX_GAIN_DB)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("build/STM32C011F4P6/bench/pluto_bench.manifest.json"),
    )
    parser.add_argument(
        "--openocd-config",
        type=Path,
        default=Path("openocd/stlink-v3-stm32c011.cfg"),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(Path.home() / ".local/state/smateway/lab-runs/network-192.168.1.15/static-screen"),
    )
    return parser


def _assert_args(args: argparse.Namespace) -> None:
    if args.repeats < 1 or args.repeats > 10:
        raise SystemExit("repeats must be 1..10")
    if not args.frequency_hz or any(
        not 70_000_000 <= frequency <= 6_000_000_000 for frequency in args.frequency_hz
    ):
        raise SystemExit("every frequency must be within the AD9361 70 MHz..6 GHz range")
    if args.tx_gain_db > MAX_TX_GAIN_DB or args.tx_gain_db < -80.0:
        raise SystemExit(f"TX gain must remain within -80..{MAX_TX_GAIN_DB:g} dB")
    if args.mode == "muted" and args.state not in (None, ["ALL_OFF"]):
        raise SystemExit("muted capture permits only ALL_OFF")


def _git_head() -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()


def _radio_facts(device: Any, *, expected_uri: str, expected_serial: str) -> dict[str, str]:
    facts = dict(device.ctx.attrs)
    if facts.get("hw_serial") != expected_serial:
        raise RuntimeError(
            f"pinned radio identity mismatch: {facts.get('hw_serial')!r} != {expected_serial!r}"
        )
    if facts.get("uri") != expected_uri:
        raise RuntimeError(f"pinned radio URI mismatch: {facts.get('uri')!r} != {expected_uri!r}")
    return {str(key): str(value) for key, value in facts.items()}


def _configure_receive(device: Any, center_hz: int) -> None:
    device.rx_destroy_buffer()
    device.sample_rate = SAMPLE_RATE_HZ
    device.rx_rf_bandwidth = BANDWIDTH_HZ
    device.tx_rf_bandwidth = BANDWIDTH_HZ
    device.rx_lo = center_hz
    device.tx_lo = center_hz
    device.rx_enabled_channels = [0, 1]
    device.gain_control_mode_chan0 = "manual"
    device.gain_control_mode_chan1 = "manual"
    device.rx_hardwaregain_chan0 = RX_GAIN_DB
    device.rx_hardwaregain_chan1 = RX_GAIN_DB
    device.rx_buffer_size = SAMPLE_COUNT


def _configure_source(device: Any, center_hz: int) -> None:
    device.sample_rate = SAMPLE_RATE_HZ
    device.tx_rf_bandwidth = BANDWIDTH_HZ
    device.tx_lo = center_hz


def _selector_request(
    controller: OpenOcdBench,
    state_name: str,
    state_codes: dict[str, int],
) -> dict[str, int | bool]:
    lease_ms = 0 if state_name == "ALL_OFF" else SELECTOR_LEASE_MS
    status = controller.request(
        state_codes[state_name],
        lease_ms,
        wait_until_applied=state_name != "ALL_OFF",
    )
    if status.applied_code != state_codes[state_name] or status.invalid_command:
        raise RuntimeError(f"selector did not apply {state_name}: {status.as_dict()}")
    return status.as_dict()


def _readback_mute(device: Any) -> dict[str, Any]:
    gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
    scales = [float(value) for value in device.dds_scales]
    passed = gains == [-80.0, -80.0] and len(scales) == 8 and all(value == 0.0 for value in scales)
    result = {"passed": passed, "tx_gain_db": gains, "dds_scales": scales}
    if not passed:
        raise RuntimeError(f"exact radio mute readback failed: {result}")
    return result


def _receive_samples(device: Any, *, expected_shape: tuple[int, int]) -> np.ndarray:
    """Read one RX buffer, tolerating short-lived open and empty-refill races."""

    for attempt in range(1, RX_BUSY_RETRIES + 1):
        try:
            samples = np.asarray(device.rx(), dtype=np.complex64)
        except OSError as error:
            if error.errno != errno.EBUSY or attempt == RX_BUSY_RETRIES:
                raise
        else:
            if samples.shape == expected_shape:
                return samples
            if samples.size or attempt == RX_BUSY_RETRIES:
                raise RuntimeError(f"unexpected capture shape {samples.shape}")
        device.rx_destroy_buffer()
        time.sleep(RX_BUSY_RETRY_DELAY_S * attempt)
    raise AssertionError("unreachable")


def _capture(device: Any, *, muted: bool, tx_gain_db: float) -> tuple[np.ndarray, dict[str, Any]]:
    _mute_transmit(device)
    _readback_mute(device)
    # The pyadi RX buffer continues filling between calls. Recreate it only
    # after the selector and TX condition are fixed, or the next read can
    # return samples accumulated during the preceding muted cleanup interval.
    device.rx_destroy_buffer()
    if not muted:
        device.tx_hardwaregain_chan0 = tx_gain_db
        device.dds_single_tone(TONE_OFFSET_HZ, DDS_SCALE, channel=0)
        if float(device.tx_hardwaregain_chan0) > tx_gain_db + 0.25:
            raise RuntimeError("TX1 hardware-gain readback exceeds the requested bound")
        if max(abs(float(value)) for value in device.dds_scales) > DDS_SCALE + 1e-6:
            raise RuntimeError("DDS scale readback exceeds the requested bound")
    time.sleep(0.1)
    started = datetime.now(UTC).isoformat()
    samples = _receive_samples(device, expected_shape=(2, SAMPLE_COUNT))
    completed = datetime.now(UTC).isoformat()
    if samples.shape != (2, SAMPLE_COUNT):
        raise RuntimeError(f"unexpected capture shape {samples.shape}")
    if not np.all(np.isfinite(samples.real)) or not np.all(np.isfinite(samples.imag)):
        raise RuntimeError("capture contains non-finite samples")
    readback = {
        "started_utc": started,
        "completed_utc": completed,
        "tx_gain_db": [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)],
        "dds_scales": [float(value) for value in device.dds_scales],
        "dds_frequencies_hz": [int(value) for value in device.dds_frequencies],
    }
    return samples, readback


def _capture_external(
    receiver: Any,
    source: Any,
    *,
    tx_gain_db: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    _mute_transmit(receiver)
    receiver_mute = _readback_mute(receiver)
    _mute_transmit(source)
    _readback_mute(source)
    source.tx_hardwaregain_chan0 = tx_gain_db
    source.dds_single_tone(TONE_OFFSET_HZ, DDS_SCALE, channel=0)
    if float(source.tx_hardwaregain_chan0) > tx_gain_db + 0.25:
        raise RuntimeError("source TX1 hardware-gain readback exceeds the requested bound")
    if max(abs(float(value)) for value in source.dds_scales) > DDS_SCALE + 1e-6:
        raise RuntimeError("source DDS scale readback exceeds the requested bound")
    # Discard anything accumulated before the selector and external source were fixed.
    receiver.rx_destroy_buffer()
    time.sleep(0.1)
    started = datetime.now(UTC).isoformat()
    samples = _receive_samples(receiver, expected_shape=(2, SAMPLE_COUNT))
    completed = datetime.now(UTC).isoformat()
    if samples.shape != (2, SAMPLE_COUNT):
        raise RuntimeError(f"unexpected capture shape {samples.shape}")
    if not np.all(np.isfinite(samples.real)) or not np.all(np.isfinite(samples.imag)):
        raise RuntimeError("capture contains non-finite samples")
    return samples, {
        "started_utc": started,
        "completed_utc": completed,
        "receiver_mute": receiver_mute,
        "source_tx_gain_db": [
            float(source.tx_hardwaregain_chan0),
            float(source.tx_hardwaregain_chan1),
        ],
        "source_dds_scales": [float(value) for value in source.dds_scales],
        "source_dds_frequencies_hz": [int(value) for value in source.dds_frequencies],
    }


def _coarse_external_tone_offset(rx1: np.ndarray) -> float:
    """Find the independent-clock pilot before the bounded coherent refinement."""

    window = np.hanning(rx1.size)
    spectrum = np.fft.fft(rx1 * window)
    frequencies = np.fft.fftfreq(rx1.size, d=1.0 / SAMPLE_RATE_HZ)
    candidate = (frequencies >= 20_000.0) & (frequencies <= 250_000.0)
    indices = np.flatnonzero(candidate)
    if indices.size == 0:
        raise RuntimeError("external pilot acquisition band is empty")
    peak = int(indices[np.argmax(np.abs(spectrum[indices]))])
    return float(frequencies[peak])


def _project_tone(
    samples: np.ndarray,
    *,
    muted: bool,
    independent_clock: bool = False,
) -> dict[str, Any]:
    rx1, rx2 = samples
    peak_components = [
        float(max(np.max(np.abs(channel.real)), np.max(np.abs(channel.imag))))
        for channel in (rx1, rx2)
    ]
    rms = [float(np.sqrt(np.mean(np.abs(channel) ** 2))) for channel in (rx1, rx2)]
    result: dict[str, Any] = {
        "peak_component_counts": peak_components,
        "rms_counts": rms,
    }
    if muted:
        return result
    nominal_offset_hz = _coarse_external_tone_offset(rx1) if independent_clock else TONE_OFFSET_HZ
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=nominal_offset_hz,
        bin_ms=0.5,
        maximum_residual_hz=400.0,
    )
    frequency = pilot.estimated_offset_hz
    indices = np.arange(SAMPLE_COUNT, dtype=np.float64)
    oscillator = np.exp(-2j * np.pi * frequency * indices / SAMPLE_RATE_HZ)
    z1 = complex(np.mean(rx1 * oscillator))
    z2 = complex(np.mean(rx2 * oscillator))
    if abs(z1) <= np.finfo(float).tiny:
        raise RuntimeError("RX1 reference phasor is zero")
    transfer = z2 / z1
    result.update(
        {
            "configured_tone_offset_hz": TONE_OFFSET_HZ,
            "coarse_acquisition_offset_hz": nominal_offset_hz,
            "pilot": asdict(pilot),
            "rx1_phasor": {"real": z1.real, "imag": z1.imag, "magnitude": abs(z1)},
            "rx2_phasor": {"real": z2.real, "imag": z2.imag, "magnitude": abs(z2)},
            "transfer_rx2_over_rx1": {
                "real": transfer.real,
                "imag": transfer.imag,
                "magnitude": abs(transfer),
                "magnitude_db": 20.0 * math.log10(abs(transfer)),
                "phase_deg": math.degrees(math.atan2(transfer.imag, transfer.real)),
            },
        }
    )
    return result


def main() -> int:
    args = _parser().parse_args()
    _assert_args(args)
    states = args.state or (["ALL_OFF"] if args.mode == "muted" else ["ALL_OFF"])
    profile = load_profile(args.profile)
    state_codes = {state.name: state.gpio_code for state in profile.states}
    state_codes["ALL_OFF"] = profile.all_off_code
    manifest = BenchManifest.load(args.manifest)
    controller = OpenOcdBench(manifest, args.openocd_config)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    run: dict[str, Any] = {
        "schema": 1,
        "run_id": run_id,
        "mode": args.mode,
        "radio_uri": RADIO_URI,
        "radio_serial": RADIO_SERIAL,
        "source_radio_uri": SOURCE_URI if args.mode == "external" else None,
        "source_radio_serial": SOURCE_SERIAL if args.mode == "external" else None,
        "board_id": BOARD_ID,
        "stlink_serial": STLINK_SERIAL,
        "git_head": _git_head(),
        "configuration": {
            "frequencies_hz": args.frequency_hz,
            "states": states,
            "repeats": args.repeats,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "sample_count": SAMPLE_COUNT,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "rx_gain_db": RX_GAIN_DB,
            "tx_gain_db": None if args.mode == "muted" else args.tx_gain_db,
            "dds_scale": None if args.mode == "muted" else DDS_SCALE,
        },
        "observations": [],
        "final_radio_mute": None,
        "final_source_radio_mute": None,
        "final_selector": None,
        "error": None,
    }

    device: Any | None = None
    source: Any | None = None
    try:
        device = adi.ad9361(uri=RADIO_URI)
        run["radio_facts"] = _radio_facts(
            device,
            expected_uri=RADIO_URI,
            expected_serial=RADIO_SERIAL,
        )
        _mute_transmit(device)
        _readback_mute(device)
        if args.mode == "external":
            source = adi.ad9361(uri=SOURCE_URI)
            run["source_radio_facts"] = _radio_facts(
                source,
                expected_uri=SOURCE_URI,
                expected_serial=SOURCE_SERIAL,
            )
            _mute_transmit(source)
            _readback_mute(source)
        _selector_request(controller, "ALL_OFF", state_codes)

        for frequency_hz in args.frequency_hz:
            _configure_receive(device, frequency_hz)
            if source is not None:
                _configure_source(source, frequency_hz)
            for state_name in states:
                for repeat in range(1, args.repeats + 1):
                    before = _selector_request(controller, state_name, state_codes)
                    samples: np.ndarray | None = None
                    try:
                        if source is None:
                            samples, radio_readback = _capture(
                                device,
                                muted=args.mode == "muted",
                                tx_gain_db=args.tx_gain_db,
                            )
                        else:
                            samples, radio_readback = _capture_external(
                                device,
                                source,
                                tx_gain_db=args.tx_gain_db,
                            )
                    finally:
                        source_mute = None
                        if source is not None:
                            _mute_transmit(source)
                            source_mute = _readback_mute(source)
                        _mute_transmit(device)
                        mute = _readback_mute(device)
                        after = _selector_request(controller, "ALL_OFF", state_codes)

                    stem = f"{frequency_hz}-{state_name.lower()}-r{repeat}"
                    np.savez(
                        run_dir / f"{stem}.npz",
                        rx1=samples[0],
                        rx2=samples[1],
                    )
                    try:
                        analysis = _project_tone(
                            samples,
                            muted=args.mode == "muted",
                            independent_clock=args.mode == "external",
                        )
                        analysis_error = None
                    except ValueError as error:
                        analysis = None
                        analysis_error = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                    observation = {
                        "frequency_hz": frequency_hz,
                        "state": state_name,
                        "repeat": repeat,
                        "iq_file": f"{stem}.npz",
                        "selector_before": before,
                        "radio_readback": radio_readback,
                        "analysis": analysis,
                        "analysis_error": analysis_error,
                        "post_capture_mute": mute,
                        "post_capture_source_mute": source_mute,
                        "selector_after": after,
                    }
                    run["observations"].append(observation)
                    print(json.dumps(observation, sort_keys=True), flush=True)
    except BaseException as error:
        run["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if source is not None:
            _mute_transmit(source)
            run["final_source_radio_mute"] = _readback_mute(source)
        if device is not None:
            try:
                _mute_transmit(device)
                run["final_radio_mute"] = _readback_mute(device)
            finally:
                device.rx_destroy_buffer()
        run["final_selector"] = _selector_request(controller, "ALL_OFF", state_codes)
        (run_dir / "run.json").write_text(
            json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"run_dir={run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
