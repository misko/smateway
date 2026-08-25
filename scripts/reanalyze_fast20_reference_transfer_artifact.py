#!/usr/bin/env python3
"""Measure Fast20 RX2 paths relative to an OTA reference antenna on RX1."""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from typing import Any

from capture_fast20_dwell import _continuity_ledger, _load_channel, _write_json_atomic
from pluto_plus.artifacts import load_metadata, verify_artifact
from pluto_plus.models import ArtifactSummary

from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.profile import load_profile
from smateway.reference_transfer import (
    CyclePhasorSummary,
    Fast20ReferenceTransferAnalysis,
    ReferenceTransferStateEstimate,
    analyze_fast20_reference_transfer,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
MINIMUM_COMPLETE_CYCLES = 20
MINIMUM_ALIGNMENT_SCORE = 0.75
MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT = 0.75
MINIMUM_REFERENCE_VALID_BIN_FRACTION = 0.95
MINIMUM_RX1_CYCLE_COHERENCE = 0.90
MINIMUM_TRANSFER_DETECTION_SNR_DB = 15.0
MINIMUM_TRANSFER_CYCLE_COHERENCE = 0.75
MINIMUM_TRANSFER_EVEN_ODD_AGREEMENT = 0.75
MAXIMUM_TRANSFER_CYCLE_PHASE_STD_DEG = 30.0
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
    return parser


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": value.real, "imag": value.imag}


def _configured_tone_offset_hz(capture: dict[str, Any]) -> float:
    tx_channel = int(capture["tx_channel"])
    if tx_channel not in (0, 1):
        raise ValueError("capture TX channel must be zero or one")
    readback = capture.get("dds_frequency_readback_hz")
    if not isinstance(readback, (list, tuple)) or len(readback) != 8:
        raise ValueError("capture DDS frequency read-back is not the canonical 2T2R layout")
    active = (tx_channel * 4, tx_channel * 4 + 2)
    frequencies = tuple(abs(float(readback[index])) for index in active)
    if not all(isfinite(value) and value > 0.0 for value in frequencies):
        raise ValueError("active DDS frequency read-backs must be positive and finite")
    if abs(frequencies[0] - frequencies[1]) > 1.0:
        raise ValueError("active I/Q DDS frequency read-backs disagree")
    return sum(frequencies) / len(frequencies)


def _capture_headroom_admission_passed(capture: dict[str, Any]) -> bool:
    admission = capture.get("adc_headroom_admission")
    if not isinstance(admission, dict) or admission.get("passed") is not True:
        return False
    receivers = admission.get("receivers")
    if not isinstance(receivers, list) or len(receivers) != 2:
        return False
    sample_count = capture.get("sample_count")
    observed: set[int] = set()
    for receiver in receivers:
        if not isinstance(receiver, dict) or receiver.get("passed") is not True:
            return False
        index = receiver.get("receiver")
        if not isinstance(index, int) or isinstance(index, bool) or index not in (0, 1):
            return False
        if index in observed or receiver.get("clipped_sample_count") != 0:
            return False
        if sample_count is not None and receiver.get("sample_count") != sample_count:
            return False
        near_fraction = receiver.get("near_full_scale_fraction")
        maximum_fraction = receiver.get("maximum_near_full_scale_fraction")
        if not isinstance(near_fraction, (int, float)) or not isinstance(
            maximum_fraction, (int, float)
        ):
            return False
        if not isfinite(float(near_fraction)) or not isfinite(float(maximum_fraction)):
            return False
        if float(near_fraction) > float(maximum_fraction):
            return False
        observed.add(index)
    return observed == {0, 1}


def _repeat_quality(
    summary: CyclePhasorSummary,
    *,
    minimum_cycle_coherence: float,
) -> tuple[bool, tuple[str, ...]]:
    reasons = []
    if summary.cycle_coherence < minimum_cycle_coherence:
        reasons.append("cycle_coherence_below_minimum")
    if summary.even_odd_phase_agreement < MINIMUM_TRANSFER_EVEN_ODD_AGREEMENT:
        reasons.append("even_odd_agreement_below_minimum")
    if summary.cycle_phase_std_deg > MAXIMUM_TRANSFER_CYCLE_PHASE_STD_DEG:
        reasons.append("cycle_phase_std_above_maximum")
    return not reasons, tuple(reasons)


def _phasor_document(
    summary: CyclePhasorSummary,
    *,
    minimum_cycle_coherence: float,
) -> dict[str, Any]:
    passed, reasons = _repeat_quality(
        summary,
        minimum_cycle_coherence=minimum_cycle_coherence,
    )
    return {
        "phasor": _complex_document(summary.phasor),
        "amplitude": summary.amplitude,
        "phase_deg": summary.phase_deg,
        "cycle_coherence": summary.cycle_coherence,
        "cycle_phase_std_deg": summary.cycle_phase_std_deg,
        "even_odd_phase_agreement": summary.even_odd_phase_agreement,
        "cycle_phasors": [_complex_document(value) for value in summary.cycle_phasors],
        "repeat_quality_passed": passed,
        "repeat_quality_rejection_reasons": reasons,
    }


def _state_quality(estimate: ReferenceTransferStateEstimate) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    rx1_passed, rx1_reasons = _repeat_quality(
        estimate.rx1,
        minimum_cycle_coherence=MINIMUM_RX1_CYCLE_COHERENCE,
    )
    if not rx1_passed:
        reasons.extend(f"rx1_{reason}" for reason in rx1_reasons)
    if estimate.transfer_detection_snr_db < MINIMUM_TRANSFER_DETECTION_SNR_DB:
        reasons.append("transfer_detection_snr_below_minimum")
    transfer = estimate.all_off_subtracted_rx2_over_rx1
    transfer_passed, transfer_reasons = _repeat_quality(
        transfer,
        minimum_cycle_coherence=MINIMUM_TRANSFER_CYCLE_COHERENCE,
    )
    if not transfer_passed:
        reasons.extend(f"transfer_{reason}" for reason in transfer_reasons)
    return not reasons, tuple(reasons)


def _analysis_document(
    *,
    artifact: ArtifactSummary,
    capture: dict[str, Any],
    pilot: dict[str, Any],
    analysis: Fast20ReferenceTransferAnalysis,
    source_commit: str,
) -> dict[str, Any]:
    capture_headroom_passed = _capture_headroom_admission_passed(capture)
    state_quality = {estimate.name: _state_quality(estimate) for estimate in analysis.states}
    global_reasons = []
    if not capture_headroom_passed:
        global_reasons.append("capture_headroom_admission_missing_or_failed")
    if not analysis.continuity_verified:
        global_reasons.append("capture_continuity_not_verified")
    if analysis.complete_cycle_count < MINIMUM_COMPLETE_CYCLES:
        global_reasons.append("complete_cycle_count_below_minimum")
    if analysis.alignment_score < MINIMUM_ALIGNMENT_SCORE:
        global_reasons.append("schedule_alignment_score_below_minimum")
    if analysis.alignment_even_odd_agreement < MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT:
        global_reasons.append("schedule_even_odd_agreement_below_minimum")
    if analysis.reference_valid_bin_fraction < MINIMUM_REFERENCE_VALID_BIN_FRACTION:
        global_reasons.append("rx1_reference_coverage_below_minimum")
    quality_passed = not global_reasons and all(value[0] for value in state_quality.values())

    center_frequency_hz = int(capture["center_frequency_hz"])
    configured_tone_offset_hz = _configured_tone_offset_hz(capture)
    carrier_frequency_hz = center_frequency_hz + configured_tone_offset_hz
    estimated_received_carrier_frequency_hz = center_frequency_hz + float(
        pilot["estimated_offset_hz"]
    )
    states = []
    for estimate in analysis.states:
        passed, reasons = state_quality[estimate.name]
        states.append(
            {
                "name": estimate.name,
                "rx1": _phasor_document(
                    estimate.rx1,
                    minimum_cycle_coherence=MINIMUM_RX1_CYCLE_COHERENCE,
                ),
                "raw_rx2_over_rx1": _phasor_document(
                    estimate.raw_rx2_over_rx1,
                    minimum_cycle_coherence=MINIMUM_TRANSFER_CYCLE_COHERENCE,
                ),
                "all_off_subtracted_rx2_over_rx1": _phasor_document(
                    estimate.all_off_subtracted_rx2_over_rx1,
                    minimum_cycle_coherence=MINIMUM_TRANSFER_CYCLE_COHERENCE,
                ),
                "transfer_detection_snr_db": estimate.transfer_detection_snr_db,
                "transfer_approximate_phase_standard_error_deg": (
                    estimate.transfer_approximate_phase_standard_error_deg
                ),
                "bin_count": estimate.bin_count,
                "quality_passed": passed,
                "quality_rejection_reasons": reasons,
            }
        )

    return {
        "schema": 1,
        "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
        "source_commit": source_commit,
        "artifact": artifact.model_dump(mode="json"),
        "capture": capture,
        "aggregation_key": {
            "artifact_id": artifact.artifact_id,
            "stream_id": int(capture["stream_id"]),
            "tx_channel": int(capture["tx_channel"]),
            "center_frequency_hz": center_frequency_hz,
            "carrier_frequency_hz": carrier_frequency_hz,
            "configured_dds_tone_offset_hz": configured_tone_offset_hz,
            "estimated_received_carrier_frequency_hz": (estimated_received_carrier_frequency_hz),
            "sample_rate_hz": int(capture["sample_rate_hz"]),
            "receiver_gain_db": int(capture["receiver_gain_db"]),
        },
        "pilot": pilot,
        "reference_model": {
            "rx1_role": "continuously_illuminated_ota_reference_antenna",
            "raw_transfer": "coherent RX2 / RX1",
            "reported_transfer": (
                "raw RX2/RX1 minus the locally interpolated RX2/RX1 observed during "
                "Fast20 ALL_OFF intervals"
            ),
            "terminated_rx1_leakage_interpretation": False,
        },
        "quality_gate": {
            "passed": quality_passed,
            "global_rejection_reasons": global_reasons,
            "capture_headroom_admission_passed": capture_headroom_passed,
            "minimum_complete_cycles": MINIMUM_COMPLETE_CYCLES,
            "minimum_alignment_score": MINIMUM_ALIGNMENT_SCORE,
            "minimum_alignment_even_odd_agreement": (MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT),
            "minimum_reference_valid_bin_fraction": MINIMUM_REFERENCE_VALID_BIN_FRACTION,
            "minimum_rx1_cycle_coherence": MINIMUM_RX1_CYCLE_COHERENCE,
            "minimum_transfer_detection_snr_db": MINIMUM_TRANSFER_DETECTION_SNR_DB,
            "minimum_transfer_cycle_coherence": MINIMUM_TRANSFER_CYCLE_COHERENCE,
            "minimum_transfer_even_odd_agreement": (MINIMUM_TRANSFER_EVEN_ODD_AGREEMENT),
            "maximum_transfer_cycle_phase_std_deg": (MAXIMUM_TRANSFER_CYCLE_PHASE_STD_DEG),
        },
        "transfer": {
            "cycle_ms": analysis.cycle_ms,
            "marker_phase_ms": analysis.marker_phase_ms,
            "bin_duration_ms": analysis.bin_duration_ms,
            "bin_count": analysis.bin_count,
            "complete_cycle_count": analysis.complete_cycle_count,
            "edge_exclusion_ms": analysis.edge_exclusion_ms,
            "alignment_score": analysis.alignment_score,
            "alignment_even_odd_agreement": analysis.alignment_even_odd_agreement,
            "reference_valid_bin_fraction": analysis.reference_valid_bin_fraction,
            "continuity_verified": analysis.continuity_verified,
            "continuity_block_count": analysis.continuity_block_count,
            "all_off_anchor_count": analysis.all_off_anchor_count,
            "all_off": {
                "rx1": _phasor_document(
                    analysis.all_off_rx1,
                    minimum_cycle_coherence=MINIMUM_RX1_CYCLE_COHERENCE,
                ),
                "raw_rx2_over_rx1": _phasor_document(
                    analysis.all_off_raw_rx2_over_rx1,
                    minimum_cycle_coherence=MINIMUM_TRANSFER_CYCLE_COHERENCE,
                ),
                "used_as_global_admission_gate": False,
            },
            "states": states,
        },
        "interpretation": (
            "Within-capture dual-receiver transfer. It retains the RX1 antenna path and "
            "also includes receiver-channel, selector, PCB, antenna, coupling and multipath "
            "phase. Cross-capture geometry requires repeat validation and differential-path "
            "calibration or multiple known emitter positions."
        ),
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
    capture_path = artifact_root / "fast20-dwell-isolation.json"
    capture_document = json.loads(capture_path.read_text(encoding="utf-8"))
    artifact = ArtifactSummary.model_validate(capture_document["artifact"])
    if Path(artifact.path) != artifact_root or artifact.artifact_id != args.artifact_id:
        raise RuntimeError("analysis artifact identity does not match the requested path")
    if not verify_artifact(artifact):
        raise RuntimeError("persisted fast20 artifact failed its SHA-256 check")
    capture = capture_document["capture"]
    if not isinstance(capture, dict):
        raise RuntimeError("capture provenance is not an object")
    sample_rate_hz = float(capture["sample_rate_hz"])
    nominal_tone_offset_hz = (
        round(TONE_OFFSET_HZ * DDS_PHASE_ACCUMULATOR_STEPS / sample_rate_hz)
        * sample_rate_hz
        / DDS_PHASE_ACCUMULATOR_STEPS
    )
    ledger = _continuity_ledger(load_metadata(artifact))
    rx1 = _load_channel(artifact, 0)
    try:
        pilot_estimate = estimate_coherent_pilot_offset(
            rx1,
            sample_rate_hz=sample_rate_hz,
            nominal_tone_offset_hz=nominal_tone_offset_hz,
        )
        pilot = asdict(pilot_estimate)
        rx2 = _load_channel(artifact, 1)
        try:
            analysis = analyze_fast20_reference_transfer(
                rx1,
                rx2,
                sample_rate_hz=sample_rate_hz,
                tone_offset_hz=pilot_estimate.estimated_offset_hz,
                profile=load_profile(args.profile),
                continuity_ledger=ledger,
                edge_exclusion_bins=2,
            )
        finally:
            del rx2
    finally:
        del rx1
        gc.collect()

    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document = _analysis_document(
        artifact=artifact,
        capture=capture,
        pilot=pilot,
        analysis=analysis,
        source_commit=source_commit,
    )
    output_path = artifact_root / "fast20-reference-transfer.json"
    _write_json_atomic(output_path, document)
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "analysis": str(output_path),
                "quality_passed": document["quality_gate"]["passed"],
                "complete_cycle_count": analysis.complete_cycle_count,
            },
            sort_keys=True,
        )
    )
    return 0 if document["quality_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
