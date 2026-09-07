#!/usr/bin/env python3
"""Capture and analyze one autonomous C6 TX1 or TX2 timing condition."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / ".venv"
PYTHON = PREFIX / "bin/python"
LIB = PREFIX / "lib"
SOURCE = ROOT / "src"
loader_entries = tuple(
    Path(item).resolve()
    for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
    if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != PREFIX.resolve()
    or str(SOURCE) not in sys.path
    or not loader_entries
    or loader_entries[0] != LIB.resolve()
):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(SOURCE), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(LIB), environment.get("LD_LIBRARY_PATH", ""))
    ).rstrip(os.pathsep)
    os.execve(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], environment)

import numpy as np
from pluto_plus.hardware import IioRadioDevice, SampleBlockV2
from pluto_plus.hardware.iio import _mute_transmit, _release_device
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION, verify_metadata_runtime
from pluto_plus.models import GainMode, RadioSettings
from pluto_plus.tandem import TandemMode, TandemSessionRequestV1

from smateway.fast_tracking import (
    FastTrackingProfile,
    analyze_fast_bearings,
    analyze_fast_phase,
    coherent_product,
    decode_fast_schedule,
    estimate_frequency_difference,
)
from smateway.tracking import (
    BoardCalibrationLut,
    far_field_steering,
    load_ism_band_profiles,
)

RECEIVER_URI = "ip:192.168.1.15"
RECEIVER_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.179"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_OUTPUT_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v6")
SAMPLE_RATE_HZ = 2_000_000
BANDWIDTH_HZ = 1_600_000
FRAME_SAMPLES = 100_000
FRAME_COUNT = 80
KERNEL_BUFFERS = 64
RX_GAIN_DB = 60
TX_GAIN_DB = -35.0
DDS_SCALE = 0.25
TONE_HZ = 100_000
REFERENCE_TONE_HZ = -100_000
SOURCE_SETTLE_S = 1.0
MINIMUM_FREQUENCY_DIFFERENCE_OBJECTIVE = 0.25
FREQUENCY_PLAN = Path("docs/tracking_development_plan/data/ism-frequency-plan.json")
BOARD_LUT = Path("docs/pcb_direct_injection_calibration/data/calibration-lut.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--flash-evidence", type=Path, required=True)
    parser.add_argument("--frequency-hz", type=int, required=True)
    parser.add_argument("--tx-channel", type=int, choices=(0, 1), required=True)
    parser.add_argument("--edge-trim-us", type=float, default=5.0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _mute_readback(device: Any) -> dict[str, Any]:
    _mute_transmit(device)
    gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
    scales = [float(value) for value in device.dds_scales]
    if gains != [-80.0, -80.0] or len(scales) != 8 or any(scales):
        raise RuntimeError("source mute readback failed")
    return {"passed": True, "tx_gain_db": gains, "dds_scales": scales}


def _source_facts(device: Any) -> dict[str, str]:
    facts = {str(key): str(value) for key, value in device.ctx.attrs.items()}
    if facts.get("hw_serial") != SOURCE_SERIAL or facts.get("uri") != SOURCE_URI:
        raise RuntimeError("source identity differs from the pinned Pluto")
    return facts


def _enable_source(device: Any, frequency_hz: int, tx_channel: int) -> dict[str, Any]:
    _mute_readback(device)
    _mute_readback(device)
    device.sample_rate = SAMPLE_RATE_HZ
    device.tx_rf_bandwidth = BANDWIDTH_HZ
    device.tx_lo = frequency_hz
    if abs(float(device.tx_lo) - frequency_hz) > 5.0:
        raise RuntimeError("source LO readback is outside 5 Hz")
    if tx_channel == 0:
        device.tx_hardwaregain_chan0 = TX_GAIN_DB
        device.dds_single_tone(TONE_HZ, DDS_SCALE, channel=0)
        expected_gains = [TX_GAIN_DB, -80.0]
        difference_hz: int | None = None
        mode = "TX1 same-emitter RX2/RX1"
    else:
        device.tx_hardwaregain_chan0 = TX_GAIN_DB
        device.tx_hardwaregain_chan1 = TX_GAIN_DB
        device.dds_single_tone(TONE_HZ, 0.0, channel=0)
        device.dds_frequencies = [
            abs(REFERENCE_TONE_HZ),
            abs(REFERENCE_TONE_HZ),
            abs(REFERENCE_TONE_HZ),
            abs(REFERENCE_TONE_HZ),
            TONE_HZ,
            TONE_HZ,
            TONE_HZ,
            TONE_HZ,
        ]
        device.dds_phases = [0, 0, 90_000, 0, 90_000, 0, 0, 0]
        device.dds_scales = [DDS_SCALE, 0.0, DDS_SCALE, 0.0, DDS_SCALE, 0.0, DDS_SCALE, 0.0]
        device.dds_enabled = [1] * 8
        expected_gains = [TX_GAIN_DB, TX_GAIN_DB]
        difference_hz = TONE_HZ - REFERENCE_TONE_HZ
        mode = "TX2 with coherent frequency-separated TX1 pilot"
    gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
    scales = [float(value) for value in device.dds_scales]
    frequencies = [int(value) for value in device.dds_frequencies]
    phases = [int(value) for value in device.dds_phases]
    enabled = [int(value) for value in device.dds_enabled]
    if any(
        abs(actual - expected) > 0.25
        for actual, expected in zip(gains, expected_gains, strict=True)
    ):
        raise RuntimeError("source gain readback differs")
    expected_active = {0, 2} if tx_channel == 0 else {0, 2, 4, 6}
    if any(
        (index in expected_active) != (abs(scale - DDS_SCALE) <= 1e-6)
        for index, scale in enumerate(scales)
    ):
        raise RuntimeError("source DDS scale readback differs")
    expected_phases = (
        [90_000, 0, 0, 0, 0, 0, 0, 0]
        if tx_channel == 0
        else [0, 0, 90_000, 0, 90_000, 0, 0, 0]
    )
    if phases != expected_phases or enabled != [1] * 8:
        raise RuntimeError("source DDS phase or enable readback differs")
    active_frequencies = [frequencies[index] for index in expected_active]
    if any(abs(value - TONE_HZ) > 20 for value in active_frequencies):
        raise RuntimeError("source DDS frequency readback differs")
    if tx_channel == 1:
        difference_hz = frequencies[4] + frequencies[0]
    return {
        "mode": mode,
        "tx_channel": tx_channel,
        "tx_gain_db": gains,
        "dds_scale": scales,
        "dds_frequency_hz": frequencies,
        "dds_phase_millidegrees": phases,
        "dds_enabled": enabled,
        "tx_lo_readback_hz": int(device.tx_lo),
        "frequency_difference_hz": difference_hz,
    }


def _validate_block(block: SampleBlockV2, previous: SampleBlockV2 | None) -> None:
    if block.metadata_abi != 2 or block.samples.shape != (2, FRAME_SAMPLES):
        raise RuntimeError("capture returned a non-ABI-2 or partial dual-RX block")
    if block.missing_samples_before or block.overflow_observed:
        raise RuntimeError("capture metadata reports sample loss or overflow")
    if previous is None:
        if block.buffer_sequence != 0:
            raise RuntimeError("capture does not start at buffer sequence zero")
    elif (
        block.stream_id != previous.stream_id
        or block.buffer_sequence != previous.buffer_sequence + 1
        or block.first_sample_sequence != previous.last_sample_sequence_exclusive
    ):
        raise RuntimeError("capture stream/sample counters are discontinuous")


def _validate_flash(path: Path, profile: FastTrackingProfile) -> dict[str, Any]:
    value = _load_json(path)
    build = value.get("build")
    readback = value.get("post_program_readback")
    if (
        value.get("status") != "passed"
        or value.get("board_id") != BOARD_ID
        or not isinstance(build, dict)
        or build.get("dwell_us") != profile.dwell_us
        or build.get("cycle_us") != profile.cycle_us
        or build.get("ports") != list(profile.ports)
        or not isinstance(readback, dict)
        or readback.get("matches_binary") is not True
    ):
        raise ValueError("flash evidence does not bind the requested C6 profile")
    readback_path = Path(str(readback.get("path", ""))).resolve(strict=True)
    if _sha256(readback_path) != readback.get("sha256"):
        raise ValueError("flash readback evidence hash differs")
    return value


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_ota_authorization:
        raise SystemExit("capture requires --acknowledge-ota-authorization")
    if not 5_726_000_000 <= args.frequency_hz <= 5_874_000_000:
        raise SystemExit("timing capture centre must remain inside 5.726..5.874 GHz")
    verify_metadata_runtime(2, expected_firmware_version=V7_FIRMWARE_VERSION)
    profile = FastTrackingProfile.load(args.profile.resolve(strict=True))
    flash = _validate_flash(args.flash_evidence.resolve(strict=True), profile)
    band_profile = load_ism_band_profiles((ROOT / FREQUENCY_PLAN).resolve(strict=True))[
        "ism5800-c6-v1"
    ]
    if band_profile.geometry.ports != profile.ports:
        raise SystemExit("autonomous selector order differs from the C6 geometry")
    calibration = BoardCalibrationLut.load((ROOT / BOARD_LUT).resolve(strict=True)).evaluate(
        args.frequency_hz, profile.ports
    )
    bearings = np.arange(0.0, 360.0, 0.25)
    steering = far_field_steering(band_profile.geometry, args.frequency_hz, bearings)
    run_id = datetime.now(UTC).strftime(
        f"fast-{profile.dwell_us}us-{args.frequency_hz}-tx{args.tx_channel + 1}-%Y%m%dT%H%M%S.%fZ"
    )
    run_directory = args.output_root / "captures" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    rx1_path = run_directory / "rx1.cf32"
    rx2_path = run_directory / "rx2.cf32"
    record: dict[str, Any] = {
        "schema": 1,
        "evidence_kind": "autonomous_c6_phase_timing_capture",
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "profile_id": profile.profile_id,
            "frequency_hz": args.frequency_hz,
            "tx_channel": args.tx_channel,
            "tx_port": f"TX{args.tx_channel + 1}",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "frame_samples": FRAME_SAMPLES,
            "frame_count": FRAME_COUNT,
            "receiver_gain_db": RX_GAIN_DB,
            "tx_gain_db": TX_GAIN_DB,
            "dds_scale": DDS_SCALE,
            "tone_hz": TONE_HZ,
            "reference_tone_hz": REFERENCE_TONE_HZ if args.tx_channel == 1 else TONE_HZ,
            "source_settle_s": SOURCE_SETTLE_S,
            "edge_trim_us": args.edge_trim_us,
        },
        "identities": {
            "receiver_uri": RECEIVER_URI,
            "receiver_serial": RECEIVER_SERIAL,
            "source_uri": SOURCE_URI,
            "source_serial": SOURCE_SERIAL,
            "board_id": BOARD_ID,
        },
        "profile": {"path": str(args.profile.resolve()), "sha256": _sha256(args.profile)},
        "board_calibration": {
            "path": str((ROOT / BOARD_LUT).resolve()),
            "sha256": _sha256(ROOT / BOARD_LUT),
            "exact_knot": calibration.exact_knot,
            "interpolation_validated": calibration.interpolation_validated,
            "status": calibration.calibration_status,
        },
        "flash_evidence": {
            "path": str(args.flash_evidence.resolve()),
            "sha256": _sha256(args.flash_evidence),
            "run_id": flash["run_id"],
        },
        "capture": None,
        "analysis": None,
        "safety": {"source_final_mute": None},
        "error": None,
    }
    radio: IioRadioDevice | None = None
    session: Any | None = None
    source: Any | None = None
    previous: SampleBlockV2 | None = None
    first_sequence: int | None = None
    timeline: list[dict[str, Any]] = []
    peak = [0.0, 0.0]
    full_scale = [0, 0]
    try:
        with _lock(args.output_root / ".hardware.lock"):
            radio = IioRadioDevice(
                RECEIVER_URI,
                serial=RECEIVER_SERIAL,
                expected_metadata_abi=2,
                expected_firmware_version=V7_FIRMWARE_VERSION,
            )
            radio.open()
            settings = RadioSettings(
                center_frequency_hz=args.frequency_hz,
                sample_rate_hz=SAMPLE_RATE_HZ,
                bandwidth_hz=BANDWIDTH_HZ,
                gain_mode=GainMode.MANUAL,
                gain_db=RX_GAIN_DB,
                channels=(0, 1),
            )
            actual = radio.apply_settings(settings)
            if (
                abs(actual.center_frequency_hz - args.frequency_hz) > 5
                or actual.sample_rate_hz != SAMPLE_RATE_HZ
                or actual.channels != (0, 1)
            ):
                raise RuntimeError("receiver readback differs from the capture plan")
            source = importlib.import_module("adi").ad9361(uri=SOURCE_URI)
            source_facts = _source_facts(source)
            source_readback = _enable_source(source, args.frequency_hz, args.tx_channel)
            time.sleep(SOURCE_SETTLE_S)
            session = radio.begin_metadata_capture(
                FRAME_SAMPLES,
                kernel_buffers=KERNEL_BUFFERS,
                tandem_request=TandemSessionRequestV1(
                    mode=TandemMode.HOLD,
                    initial_gain_db=RX_GAIN_DB,
                ),
            )
            with rx1_path.open("xb") as rx1_stream, rx2_path.open("xb") as rx2_stream:
                for _ in range(FRAME_COUNT):
                    block = session.read_block()
                    _validate_block(block, previous)
                    if first_sequence is None:
                        first_sequence = block.first_sample_sequence
                    np.asarray(block.samples[0], dtype=np.complex64).tofile(rx1_stream)
                    np.asarray(block.samples[1], dtype=np.complex64).tofile(rx2_stream)
                    for channel in (0, 1):
                        values = block.samples[channel]
                        peak[channel] = max(
                            peak[channel],
                            float(max(np.max(np.abs(values.real)), np.max(np.abs(values.imag)))),
                        )
                        full_scale[channel] += int(
                            np.count_nonzero(
                                (np.abs(values.real) >= 2047.0)
                                | (np.abs(values.imag) >= 2047.0)
                            )
                        )
                    timeline.append(
                        {
                            "buffer_sequence": block.buffer_sequence,
                            "first_sample_sequence": block.first_sample_sequence,
                            "last_sample_sequence_exclusive": block.last_sample_sequence_exclusive,
                            "missing_samples_before": block.missing_samples_before,
                            "overflow_observed": block.overflow_observed,
                            "metadata_flags": block.metadata_flags,
                            "stream_id": block.stream_id,
                        }
                    )
                    previous = block
                rx1_stream.flush()
                rx2_stream.flush()
                os.fsync(rx1_stream.fileno())
                os.fsync(rx2_stream.fileno())
            assert first_sequence is not None
            if any(full_scale):
                raise RuntimeError("capture contains full-scale ADC samples")
            count = FRAME_COUNT * FRAME_SAMPLES
            rx1 = np.memmap(rx1_path, mode="r", dtype=np.complex64, shape=(count,))
            rx2 = np.memmap(rx2_path, mode="r", dtype=np.complex64, shape=(count,))
            synchronizer = np.asarray(rx2 * np.conjugate(rx1), dtype=np.complex64)
            decode = decode_fast_schedule(
                synchronizer,
                sample_rate_hz=SAMPLE_RATE_HZ,
                tone_offset_hz=0.0,
                profile=profile,
                edge_trim_us=args.edge_trim_us,
            )
            frequency_difference = None
            difference_for_product = source_readback["frequency_difference_hz"]
            if args.tx_channel == 1:
                frequency_difference = estimate_frequency_difference(
                    rx1,
                    rx2,
                    decode=decode,
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    nominal_difference_hz=float(difference_for_product),
                )
                difference_for_product = frequency_difference.frequency_difference_hz
            product = coherent_product(
                rx1,
                rx2,
                sample_rate_hz=SAMPLE_RATE_HZ,
                first_sample_sequence=first_sequence,
                difference_hz=difference_for_product,
            )
            analysis = analyze_fast_phase(product, decode=decode, profile=profile)
            analysis["frequency_difference_estimate"] = (
                None if frequency_difference is None else asdict(frequency_difference)
            )
            analysis["frequency_difference_acceptance"] = {
                "qualified": (
                    frequency_difference is None
                    or frequency_difference.search_objective
                    >= MINIMUM_FREQUENCY_DIFFERENCE_OBJECTIVE
                ),
                "minimum_search_objective": MINIMUM_FREQUENCY_DIFFERENCE_OBJECTIVE,
                "scope": (
                    "TX1 uses a same-frequency same-emitter reference; TX2 uses the "
                    "frequency-separated development pilot"
                ),
            }
            analysis["bearing_study"] = analyze_fast_bearings(
                product,
                decode=decode,
                profile=profile,
                calibration_coefficients=calibration.coefficients,
                steering=steering,
                bearings_deg=bearings,
                expected_bearing_deg=90.0 if args.tx_channel == 0 else 180.0,
            )
            analysis["schedule_decode"] = {
                key: value
                for key, value in asdict(decode).items()
                if key != "intervals"
            }
            record["capture"] = {
                "receiver_identity": radio.identity.model_dump(mode="json"),
                "receiver_settings": actual.model_dump(mode="json"),
                "source_facts": source_facts,
                "source_readback": source_readback,
                "frame_count": FRAME_COUNT,
                "total_samples_per_channel": count,
                "peak_component_counts": peak,
                "full_scale_sample_counts": full_scale,
                "timeline": timeline,
                "raw": {
                    "dtype": "complex64",
                    "rx1_path": str(rx1_path),
                    "rx1_sha256": _sha256(rx1_path),
                    "rx2_path": str(rx2_path),
                    "rx2_sha256": _sha256(rx2_path),
                },
            }
            record["analysis"] = analysis
            record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if source is not None:
            try:
                record["safety"]["source_final_mute"] = _mute_readback(source)
            except BaseException as cleanup_error:
                record["safety"]["source_final_mute"] = {
                    "passed": False,
                    "error": {"type": type(cleanup_error).__name__, "message": str(cleanup_error)},
                }
                record["status"] = "failed"
            try:
                _release_device(source)
            except BaseException:
                record["status"] = "failed"
        if session is not None:
            try:
                session.close()
            except BaseException:
                record["status"] = "failed"
        if radio is not None:
            try:
                radio.close()
            except BaseException:
                record["status"] = "failed"
        record["completed_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(run_directory / "run.json", record)
        print(f"run_dir={run_directory}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
