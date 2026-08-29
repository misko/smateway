#!/usr/bin/env python3
"""Measure all fast20 antenna phases from a persisted continuous artifact."""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
from dataclasses import asdict
from math import atan, degrees
from pathlib import Path

from capture_fast20_dwell import _continuity_ledger, _load_channel
from pluto_plus.artifacts import load_metadata, verify_artifact
from pluto_plus.models import ArtifactSummary

from smateway.ota_analysis import (
    Fast20PhaseAnalysis,
    analyze_fast20_dwell_isolation,
    analyze_fast20_phase_sensitive,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile
from smateway.schedule_alignment import AlignmentSearchMode, ScheduleAlignmentResult

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
MINIMUM_COMPLETE_CYCLES = 20
MINIMUM_STATE_SNR_DB = 15.0
MINIMUM_STATE_COHERENCE = 0.75
MINIMUM_STATE_CONFIDENCE = 0.75
MINIMUM_OVERALL_CONFIDENCE = 0.9
MINIMUM_ALIGNMENT_EXPLAINED_FRACTION = 0.90
DDS_PHASE_ACCUMULATOR_STEPS = 1 << 16
TONE_OFFSET_HZ = 100_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_id")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    parser.add_argument(
        "--alignment-search-mode",
        type=AlignmentSearchMode,
        choices=tuple(AlignmentSearchMode),
        default=AlignmentSearchMode.TRANSITION_SEEDED,
    )
    return parser


def _schedule_alignment_document(
    alignment: ScheduleAlignmentResult | None,
) -> dict[str, object] | None:
    if alignment is None:
        return None
    document = asdict(alignment)
    provenance = document["provenance"]
    assert isinstance(provenance, dict)
    provenance["mode"] = alignment.provenance.mode.value
    return document


def _wrapped_phase_difference(first_deg: float, second_deg: float) -> float:
    return float((first_deg - second_deg + 180.0) % 360.0 - 180.0)


def _pairwise_phase(analysis: Fast20PhaseAnalysis) -> dict[str, dict[str, float]]:
    return {
        first.name: {
            second.name: _wrapped_phase_difference(first.phase_deg, second.phase_deg)
            for second in analysis.states
        }
        for first in analysis.states
    }


def main() -> int:
    args = _parser().parse_args()
    artifact_root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "pluto-usb-captures"
        / args.artifact_id
    )
    capture_analysis_path = artifact_root / "fast20-dwell-isolation.json"
    capture_document = json.loads(capture_analysis_path.read_text())
    artifact = ArtifactSummary.model_validate(capture_document["artifact"])
    if Path(artifact.path) != artifact_root or artifact.artifact_id != args.artifact_id:
        raise RuntimeError("analysis artifact identity does not match the requested path")
    if not verify_artifact(artifact):
        raise RuntimeError("persisted fast20 artifact failed its SHA-256 check")

    capture = capture_document["capture"]
    sample_rate_hz = float(capture["sample_rate_hz"])
    nominal_tone_offset_hz = (
        round(TONE_OFFSET_HZ * DDS_PHASE_ACCUMULATOR_STEPS / sample_rate_hz)
        * sample_rate_hz
        / DDS_PHASE_ACCUMULATOR_STEPS
    )
    ledger = _continuity_ledger(load_metadata(artifact))
    profile = load_profile(args.profile)
    rx1 = _load_channel(artifact, 0)
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=sample_rate_hz,
        nominal_tone_offset_hz=nominal_tone_offset_hz,
    )
    rx2 = _load_channel(artifact, 1)
    dwell = analyze_fast20_dwell_isolation(
        rx2,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=ledger,
        minimum_complete_frames=MINIMUM_COMPLETE_CYCLES,
    )
    if (
        args.alignment_search_mode is AlignmentSearchMode.TRANSITION_SEEDED
        and not dwell.isolation_verified
    ):
        raise RuntimeError("transition-seeded alignment requires strict, stable dwell decoding")
    analysis = analyze_fast20_phase_sensitive(
        rx1,
        rx2,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=ledger,
        edge_exclusion_bins=2,
        alignment_search_mode=args.alignment_search_mode,
        decoded_timing=dwell.schedule_timing,
    )
    del rx1, rx2
    gc.collect()

    strongest = max(analysis.states, key=lambda estimate: estimate.amplitude)
    ant1 = analysis.estimate("ANT1")
    state_quality = {
        estimate.name: (
            estimate.detection_snr_db >= MINIMUM_STATE_SNR_DB
            and estimate.cycle_coherence >= MINIMUM_STATE_COHERENCE
            and estimate.confidence >= MINIMUM_STATE_CONFIDENCE
        )
        for estimate in analysis.states
    }
    quality_passed = (
        analysis.continuity_verified
        and analysis.complete_cycle_count >= MINIMUM_COMPLETE_CYCLES
        and analysis.confidence >= MINIMUM_OVERALL_CONFIDENCE
        and analysis.schedule_alignment is not None
        and analysis.schedule_alignment.quality.explained_fraction
        >= MINIMUM_ALIGNMENT_EXPLAINED_FRACTION
        and (
            analysis.schedule_alignment.decoded_timing_agreement is None
            or analysis.schedule_alignment.decoded_timing_agreement.agrees
        )
        and all(state_quality.values())
    )
    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    states = []
    for estimate in analysis.states:
        detection_ratio = 10.0 ** (estimate.detection_snr_db / 20.0)
        states.append(
            {
                **asdict(estimate),
                "complex_delta": {
                    "real": estimate.complex_delta.real,
                    "imag": estimate.complex_delta.imag,
                },
                "phase_relative_to_ant1_deg": _wrapped_phase_difference(
                    estimate.phase_deg, ant1.phase_deg
                ),
                "phase_relative_to_strongest_deg": _wrapped_phase_difference(
                    estimate.phase_deg, strongest.phase_deg
                ),
                "approximate_phase_standard_error_deg": degrees(
                    atan(1.0 / detection_ratio)
                ),
                "quality_passed": state_quality[estimate.name],
            }
        )
    document = {
        "schema": 1,
        "analysis_kind": "fast20_rx1_referenced_relative_phase",
        "source_commit": source_commit,
        "artifact": artifact.model_dump(mode="json"),
        "capture": capture,
        "pilot": asdict(pilot),
        "quality_gate": {
            "passed": quality_passed,
            "minimum_complete_cycles": MINIMUM_COMPLETE_CYCLES,
            "minimum_state_snr_db": MINIMUM_STATE_SNR_DB,
            "minimum_state_coherence": MINIMUM_STATE_COHERENCE,
            "minimum_state_confidence": MINIMUM_STATE_CONFIDENCE,
            "minimum_overall_confidence": MINIMUM_OVERALL_CONFIDENCE,
            "minimum_alignment_explained_fraction": (
                MINIMUM_ALIGNMENT_EXPLAINED_FRACTION
            ),
        },
        "phase": {
            "strongest_state": strongest.name,
            "ant1_reference_state": "ANT1",
            "cycle_ms": analysis.cycle_ms,
            "marker_phase_ms": analysis.marker_phase_ms,
            "complete_cycle_count": analysis.complete_cycle_count,
            "alignment_score": analysis.alignment_score,
            "schedule_alignment": _schedule_alignment_document(
                analysis.schedule_alignment
            ),
            "even_odd_cycle_agreement": analysis.even_odd_cycle_agreement,
            "jackknife_stability": analysis.jackknife_stability,
            "confidence": analysis.confidence,
            "continuity_verified": analysis.continuity_verified,
            "continuity_block_count": analysis.continuity_block_count,
            "states": states,
            "pairwise_phase_deg": _pairwise_phase(analysis),
        },
        "interpretation": (
            "Within-capture uncalibrated RF-path phase. Includes selector, PCB, antenna, "
            "coupling and receiver path; not a geometric position or a cross-capture "
            "absolute phase without per-path calibration."
        ),
    }
    output_path = artifact_root / "fast20-relative-phase.json"
    output_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "analysis": str(output_path),
                "quality_passed": quality_passed,
                "strongest_state": strongest.name,
                "complete_cycle_count": analysis.complete_cycle_count,
            }
        )
    )
    return 0 if quality_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
