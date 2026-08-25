#!/usr/bin/env python3
"""Capture and FFT-analyze one bounded phase20 Pluto transmission."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.hardware import (
    SafeDdsTonePlan,
    SampleBlockV2,
    capture_continuous_safe_dds_tone,
)
from pluto_plus.models import GainMode, RadioSettings

from smateway.ota_analysis import (
    ContinuityBlock,
    GuardedFftPhaseAnalysis,
    analyze_guarded_fft_phase,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_URI = "usb:1.3.5"
CENTER_FREQUENCY_HZ = 2_400_000_000
SAMPLE_RATE_HZ = 5_000_000
BANDWIDTH_HZ = 4_000_000
TONE_OFFSET_HZ = 100_000
SAMPLES_PER_FRAME = 250_000
FRAME_COUNT = 9
KERNEL_BUFFERS = 8
FFT_SIZE = 65_536


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx-channel", type=int, choices=(0, 1), required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/phase20-v1/control_profile.json"),
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


def _load_iq(artifact: Any) -> tuple[np.ndarray, np.ndarray]:
    raw = np.memmap(data_path(artifact), dtype="<i2", mode="r")
    expected = artifact.sample_count * artifact.receiver_count * 2
    if raw.size != expected or artifact.receiver_count != 2:
        raise ValueError("artifact is not canonical dual-RX CI16")
    components = raw.reshape(artifact.sample_count, 2, 2)
    rx1 = components[:, 0, 0].astype(np.float32) + 1j * components[:, 0, 1].astype(
        np.float32
    )
    rx2 = components[:, 1, 0].astype(np.float32) + 1j * components[:, 1, 1].astype(
        np.float32
    )
    return np.asarray(rx1, dtype=np.complex64), np.asarray(rx2, dtype=np.complex64)


def _phase_matrix(analysis: GuardedFftPhaseAnalysis) -> dict[str, dict[str, float]]:
    names = [estimate.name for estimate in analysis.states]
    return {
        first: {
            second: analysis.phase_difference_deg(first, second) for second in names
        }
        for first in names
    }


def main() -> int:
    args = _parser().parse_args()
    profile = load_profile(args.profile)
    if profile.profile_id != "phase20-v1" or profile.nominal_cycle_ms != 220:
        raise SystemExit("capture requires the exact generated phase20-v1 profile")
    root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "pluto-usb-captures"
    )
    settings = RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=60,
        channels=(0, 1),
    )
    plan = SafeDdsTonePlan(
        uri=args.uri,
        serial=args.serial,
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=args.tx_channel,
        tx_hardware_gain_db=-20.0,
        dds_scale=0.25,
        receiver_gain_db=60.0,
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    label = f"phase20 5MS/s TX{args.tx_channel + 1} 450ms FFT qualification"
    retained: list[SampleBlockV2] = []

    def retain_block(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy()))

    capture = capture_continuous_safe_dds_tone(
        plan,
        samples_per_frame=SAMPLES_PER_FRAME,
        frame_count=FRAME_COUNT,
        kernel_buffers=KERNEL_BUFFERS,
        block_consumer=retain_block,
    )
    if capture.identity.serial != args.serial or capture.settings != settings:
        raise RuntimeError("capture identity or setting readback differs from the plan")
    if len(retained) != FRAME_COUNT:
        raise RuntimeError("in-memory capture did not retain every validated frame")

    writer = CaptureWriter(root, radio=capture.identity, settings=settings, label=label)
    try:
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
    except Exception as error:
        writer.fail(error)
        raise
    finally:
        retained.clear()
    if not verify_artifact(artifact):
        raise RuntimeError("persisted phase20 artifact failed its SHA-256 check")

    metadata = load_metadata(artifact)
    ledger = _continuity_ledger(metadata)
    rx1, rx2 = _load_iq(artifact)
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=TONE_OFFSET_HZ,
    )
    fft = analyze_guarded_fft_phase(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=ledger,
        fft_size=FFT_SIZE,
        edge_exclusion_ms=2.0,
        reference_state="ANT1",
    )
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
            "tx_channel": args.tx_channel,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": capture.sample_count,
            "kernel_buffers": capture.kernel_buffers,
            "first_sample_sequence": capture.frames[0].first_sample_sequence,
            "last_sample_sequence_exclusive": (
                capture.frames[-1].last_sample_sequence_exclusive
            ),
            "stream_id": capture.frames[0].stream_id,
            "tx_gain_readback_db": capture.tx_gain_readback_db,
            "dds_scale_readback": capture.dds_scale_readback,
            "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
        },
        "pilot": asdict(pilot),
        "fft": {
            "cycle_ms": fft.cycle_ms,
            "marker_phase_ms": fft.marker_phase_ms,
            "complete_cycle_count": fft.complete_cycle_count,
            "fft_size": fft.fft_size,
            "fft_bin_index": fft.fft_bin_index,
            "fft_bin_frequency_hz": fft.fft_bin_frequency_hz,
            "requested_tone_offset_hz": fft.requested_tone_offset_hz,
            "reference_state": fft.reference_state,
            "alignment_confidence": fft.alignment_confidence,
            "continuity_verified": fft.continuity_verified,
            "continuity_block_count": fft.continuity_block_count,
            "states": [
                {
                    **asdict(estimate),
                    "complex_delta": {
                        "real": estimate.complex_delta.real,
                        "imag": estimate.complex_delta.imag,
                    },
                }
                for estimate in fft.states
            ],
            "pairwise_phase_deg": _phase_matrix(fft),
        },
    }
    analysis_path = Path(artifact.path) / "phase20-fft-analysis.json"
    analysis_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"artifact_id": artifact.artifact_id, "analysis": str(analysis_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
