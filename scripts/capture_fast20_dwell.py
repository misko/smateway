#!/usr/bin/env python3
"""Capture 10 continuous seconds and verify every fast20 unique dwell."""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.hardware import (
    SafeDdsTonePlan,
    capture_continuous_safe_dds_tone,
)
from pluto_plus.hardware.iio import find_usb_sysfs_path
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

from smateway.ota_analysis import (
    ContinuityBlock,
    analyze_fast20_dwell_isolation,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_URI = "usb:1.3.5"
CENTER_FREQUENCY_HZ = 2_400_000_000
DEFAULT_SAMPLE_RATE_HZ = 1_000_000
TONE_OFFSET_HZ = 100_000
DDS_PHASE_ACCUMULATOR_STEPS = 1 << 16
KERNEL_BUFFERS = 8
MINIMUM_COMPLETE_FRAMES = 20


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx-channel", type=int, choices=(0, 1), required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument(
        "--sample-rate-hz",
        type=int,
        choices=(1_000_000, 5_000_000),
        default=DEFAULT_SAMPLE_RATE_HZ,
        help="1 MS/s is qualified on this Pi USB path; 5 MS/s requires a faster host path",
    )
    parser.add_argument(
        "--stimulus",
        choices=("qualification", "phase"),
        default="qualification",
        help="phase raises the bounded pilot by 14 dB to resolve deep antenna fades",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    return parser


def _continuity_ledger(metadata: dict[str, Any]) -> tuple[ContinuityBlock, ...]:
    continuity = metadata.get("pluto:continuity")
    if not isinstance(continuity, dict):
        raise ValueError("artifact has no continuity ledger")
    blocks = continuity.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("artifact continuity ledger has no blocks")
    return tuple(
        ContinuityBlock(
            sample_start=int(block["sample_start"]),
            sample_count=int(block["sample_count"]),
            utc_ns=int(block["utc_ns"]),
        )
        for block in blocks
        if isinstance(block, dict)
    )


def _load_channel(artifact: Any, channel: int) -> np.ndarray:
    raw = np.memmap(data_path(artifact), dtype="<i2", mode="r")
    expected = artifact.sample_count * artifact.receiver_count * 2
    if raw.size != expected or artifact.receiver_count != 2:
        raise ValueError("artifact is not canonical dual-RX CI16")
    components = raw.reshape(artifact.sample_count, 2, 2)
    output = np.empty(artifact.sample_count, dtype=np.complex64)
    chunk_samples = 1_000_000
    for start in range(0, artifact.sample_count, chunk_samples):
        stop = min(artifact.sample_count, start + chunk_samples)
        output[start:stop].real = components[start:stop, channel, 0]
        output[start:stop].imag = components[start:stop, channel, 1]
    return output


def _radio_identity(uri: str, serial: str) -> RadioIdentity:
    return RadioIdentity(
        radio_id=serial,
        serial=serial,
        uri=uri,
        transport=Transport.IIO_USB,
        model="Analog Devices PlutoSDR Rev.C (Z7010-AD9361)",
        firmware_version=V7_FIRMWARE_VERSION,
        usb_path=find_usb_sysfs_path(serial),
    )


def main() -> int:
    args = _parser().parse_args()
    profile = load_profile(args.profile)
    if profile.profile_id != "fast20-v1" or profile.nominal_cycle_ms != 386:
        raise SystemExit("capture requires the exact generated fast20-v1 profile")
    sample_rate_hz = args.sample_rate_hz
    if sample_rate_hz == 1_000_000:
        bandwidth_hz = 800_000
        samples_per_frame = 100_000
        frame_count = 100
    else:
        bandwidth_hz = 4_000_000
        samples_per_frame = 1_000_000
        frame_count = 50
    coherent_tone_offset_hz = (
        round(TONE_OFFSET_HZ * DDS_PHASE_ACCUMULATOR_STEPS / sample_rate_hz)
        * sample_rate_hz
        / DDS_PHASE_ACCUMULATOR_STEPS
    )
    if args.stimulus == "phase":
        tx_hardware_gain_db = -12.0
        dds_scale = 0.5
    else:
        tx_hardware_gain_db = -20.0
        dds_scale = 0.25

    root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "pluto-usb-captures"
    )
    settings = RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=sample_rate_hz,
        bandwidth_hz=bandwidth_hz,
        gain_mode=GainMode.MANUAL,
        gain_db=60,
        channels=(0, 1),
    )
    plan = SafeDdsTonePlan(
        uri=args.uri,
        serial=args.serial,
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=sample_rate_hz,
        bandwidth_hz=bandwidth_hz,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=args.tx_channel,
        tx_hardware_gain_db=tx_hardware_gain_db,
        dds_scale=dds_scale,
        receiver_gain_db=60.0,
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    identity = _radio_identity(args.uri, args.serial)
    label = (
        f"fast20 {args.stimulus} {sample_rate_hz}S/s 10s TX{args.tx_channel + 1} "
        f"{CENTER_FREQUENCY_HZ}Hz"
    )
    writer = CaptureWriter(root, radio=identity, settings=settings, label=label)
    try:
        capture = capture_continuous_safe_dds_tone(
            plan,
            samples_per_frame=samples_per_frame,
            frame_count=frame_count,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=lambda block: writer.append(block, settings, revision=1),
        )
        if capture.identity != identity or capture.settings != settings:
            raise RuntimeError("capture identity or setting readback differs from preflight")
        artifact = writer.finalize()
    except Exception as error:
        writer.fail(error)
        raise
    if not verify_artifact(artifact):
        raise RuntimeError("persisted fast20 artifact failed its SHA-256 check")

    metadata = load_metadata(artifact)
    ledger = _continuity_ledger(metadata)
    rx1 = _load_channel(artifact, 0)
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=sample_rate_hz,
        nominal_tone_offset_hz=coherent_tone_offset_hz,
    )
    del rx1
    gc.collect()
    rx2 = _load_channel(artifact, 1)
    dwell = analyze_fast20_dwell_isolation(
        rx2,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=ledger,
        minimum_complete_frames=MINIMUM_COMPLETE_FRAMES,
    )
    del rx2
    gc.collect()

    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document = {
        "schema": 1,
        "artifact": artifact.model_dump(mode="json"),
        "capture": {
            "source_commit": source_commit,
            "profile_contract_sha256": profile.contract_sha256,
            "tx_channel": args.tx_channel,
            "stimulus": args.stimulus,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "sample_rate_hz": sample_rate_hz,
            "samples_per_frame": samples_per_frame,
            "frame_count": frame_count,
            "sample_count": capture.sample_count,
            "duration_s": capture.duration_s,
            "kernel_buffers": capture.kernel_buffers,
            "first_buffer_sequence": capture.frames[0].buffer_sequence,
            "last_buffer_sequence": capture.frames[-1].buffer_sequence,
            "first_sample_sequence": capture.frames[0].first_sample_sequence,
            "last_sample_sequence_exclusive": (
                capture.frames[-1].last_sample_sequence_exclusive
            ),
            "stream_id": capture.frames[0].stream_id,
            "metadata_abi": capture.frames[0].metadata_abi,
            "tx_gain_readback_db": capture.tx_gain_readback_db,
            "dds_scale_readback": capture.dds_scale_readback,
            "dds_frequency_readback_hz": capture.dds_frequency_readback_hz,
            "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
        },
        "pilot": asdict(pilot),
        "dwell_isolation": asdict(dwell),
    }
    analysis_path = Path(artifact.path) / "fast20-dwell-isolation.json"
    analysis_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "analysis": str(analysis_path),
                "isolation_verified": dwell.isolation_verified,
                "complete_frame_count": dwell.complete_frame_count,
            }
        )
    )
    return 0 if dwell.isolation_verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
