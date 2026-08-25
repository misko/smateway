#!/usr/bin/env python3
"""Reanalyze a persisted fast20 dwell artifact without enabling RF."""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from capture_fast20_dwell import (
    DDS_PHASE_ACCUMULATOR_STEPS,
    MINIMUM_COMPLETE_FRAMES,
    TONE_OFFSET_HZ,
    _continuity_ledger,
    _load_channel,
)
from pluto_plus.artifacts import load_metadata, verify_artifact
from pluto_plus.models import ArtifactSummary

from smateway.ota_analysis import (
    analyze_fast20_dwell_isolation,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_id")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    artifact_root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "pluto-usb-captures"
        / args.artifact_id
    )
    analysis_path = artifact_root / "fast20-dwell-isolation.json"
    document = json.loads(analysis_path.read_text())
    artifact = ArtifactSummary.model_validate(document["artifact"])
    if Path(artifact.path) != artifact_root or artifact.artifact_id != args.artifact_id:
        raise RuntimeError("analysis artifact identity does not match the requested path")
    if not verify_artifact(artifact):
        raise RuntimeError("persisted fast20 artifact failed its SHA-256 check")

    capture = document["capture"]
    sample_rate_hz = int(capture["sample_rate_hz"])
    coherent_tone_offset_hz = (
        round(TONE_OFFSET_HZ * DDS_PHASE_ACCUMULATOR_STEPS / sample_rate_hz)
        * sample_rate_hz
        / DDS_PHASE_ACCUMULATOR_STEPS
    )
    ledger = _continuity_ledger(load_metadata(artifact))
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
        profile=load_profile(args.profile),
        continuity_ledger=ledger,
        minimum_complete_frames=MINIMUM_COMPLETE_FRAMES,
    )
    del rx2
    gc.collect()

    analysis_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document["pilot"] = asdict(pilot)
    document["dwell_isolation"] = asdict(dwell)
    document["reanalysis"] = {
        "source_commit": analysis_commit,
        "capture_source_commit": capture["source_commit"],
        "rf_enabled": False,
    }
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
