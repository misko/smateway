#!/usr/bin/env python3
"""Reanalyze one persisted phase20 SigMF artifact with the RX2-only FFT path."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from smateway.ota_analysis import (
    ContinuityBlock,
    GuardedFftPhaseAnalysis,
    analyze_guarded_single_fft_phase,
)
from smateway.profile import load_profile

FFT_SIZE = 65_536
DDS_PHASE_ACCUMULATOR_STEPS = 1 << 16


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/phase20-v1/control_profile.json"),
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


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
    artifact = args.artifact.resolve(strict=True)
    if not artifact.is_dir():
        raise SystemExit("artifact must be a capture directory")
    artifact_id = artifact.name
    metadata_path = artifact / f"{artifact_id}.sigmf-meta"
    data_path = artifact / f"{artifact_id}.sigmf-data"
    metadata = _mapping(json.loads(metadata_path.read_text(encoding="utf-8")), "metadata")
    global_fields = _mapping(metadata.get("global"), "global")
    expected_sha256 = str(global_fields.get("pluto:sha256") or "")
    if len(expected_sha256) != 64 or _sha256(data_path) != expected_sha256:
        raise ValueError("artifact data SHA-256 does not match SigMF metadata")

    capture_fields = _mapping(metadata.get("pluto:capture"), "capture")
    settings = _mapping(capture_fields.get("initial_settings"), "settings")
    sample_count = int(capture_fields["sample_count"])
    receiver_count = int(capture_fields["receiver_count"])
    sample_rate_hz = float(settings["sample_rate_hz"])
    center_frequency_hz = float(settings["center_frequency_hz"])
    if receiver_count != 2 or sample_count <= 0:
        raise ValueError("artifact must be a non-empty canonical dual-RX capture")

    continuity = _mapping(metadata.get("pluto:continuity"), "continuity")
    raw_blocks = continuity.get("blocks")
    if not isinstance(raw_blocks, list) or len(raw_blocks) < 3:
        raise ValueError("artifact continuity ledger must contain at least three blocks")
    blocks = [_mapping(value, "continuity block") for value in raw_blocks]
    stream_ids = {int(block["stream_id"]) for block in blocks}
    if len(stream_ids) != 1:
        raise ValueError("continuity blocks do not share one stream ID")
    for index, block in enumerate(blocks):
        if int(block["buffer_sequence"]) != index:
            raise ValueError("buffer sequence is not zero-based and consecutive")
        if int(block["missing_samples_before"]) != 0:
            raise ValueError("continuity metadata reports missing samples")
        if index and int(block["first_sample_sequence"]) != int(
            blocks[index - 1]["last_sample_sequence_exclusive"]
        ):
            raise ValueError("FPGA sample counters are not contiguous")
    ledger = tuple(
        ContinuityBlock(
            sample_start=int(block["sample_start"]),
            sample_count=int(block["sample_count"]),
            utc_ns=int(block["utc_ns"]),
        )
        for block in blocks
    )

    raw = np.memmap(data_path, dtype="<i2", mode="r")
    expected_components = sample_count * receiver_count * 2
    if raw.size != expected_components:
        raise ValueError("artifact data length does not match its capture metadata")
    components = raw.reshape(sample_count, receiver_count, 2)
    rx2 = components[:, 1, 0].astype(np.float32) + 1j * components[:, 1, 1].astype(
        np.float32
    )
    tone_offset_hz = (
        round(100_000 * DDS_PHASE_ACCUMULATOR_STEPS / sample_rate_hz)
        * sample_rate_hz
        / DDS_PHASE_ACCUMULATOR_STEPS
    )
    profile = load_profile(args.profile)
    analysis = analyze_guarded_single_fft_phase(
        rx2,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=tone_offset_hz,
        profile=profile,
        continuity_ledger=ledger,
        fft_size=FFT_SIZE,
        edge_exclusion_ms=2.0,
        reference_state="ANT1",
    )
    description = str(global_fields.get("core:description") or "")
    if "TX1" in description:
        tx_channel = 0
    elif "TX2" in description:
        tx_channel = 1
    else:
        raise ValueError("artifact description does not identify TX1 or TX2")
    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document = {
        "schema": 2,
        "analysis_kind": "clock_coherent_rx2_phase20_fft",
        "source_commit": source_commit,
        "artifact_id": artifact_id,
        "artifact_data_sha256": expected_sha256,
        "tx_channel": tx_channel,
        "center_frequency_hz": center_frequency_hz,
        "tone_offset_hz": tone_offset_hz,
        "sample_rate_hz": sample_rate_hz,
        "sample_count": sample_count,
        "stream_id": next(iter(stream_ids)),
        "continuity_block_count": len(blocks),
        "first_sample_sequence": int(blocks[0]["first_sample_sequence"]),
        "last_sample_sequence_exclusive": int(
            blocks[-1]["last_sample_sequence_exclusive"]
        ),
        "fft": {
            "cycle_ms": analysis.cycle_ms,
            "marker_phase_ms": analysis.marker_phase_ms,
            "complete_cycle_count": analysis.complete_cycle_count,
            "fft_size": analysis.fft_size,
            "fft_bin_index": analysis.fft_bin_index,
            "fft_bin_frequency_hz": analysis.fft_bin_frequency_hz,
            "reference_state": analysis.reference_state,
            "alignment_confidence": analysis.alignment_confidence,
            "continuity_verified": analysis.continuity_verified,
            "states": [
                {
                    **asdict(estimate),
                    "complex_delta": {
                        "real": estimate.complex_delta.real,
                        "imag": estimate.complex_delta.imag,
                    },
                }
                for estimate in analysis.states
            ],
            "pairwise_phase_deg": _phase_matrix(analysis),
        },
    }
    output_path = artifact / "phase20-rx2-fft-analysis.json"
    output_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"artifact_id": artifact_id, "analysis": str(output_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
