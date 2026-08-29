#!/usr/bin/env python3
"""Collect compact, auditable evidence for the schedule-alignment validation report."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BOARD_ID = "stm32c011-4c0055000950313950363920"
FREQUENCIES_HZ = tuple(range(2_100_000_000, 2_500_000_001, 100_000_000))
EXPECTED_SOURCE_COMMIT = "f81a758c297d22bea53422ab18e9c91ac50ffc4d"
EXPECTED_QUARANTINES = {
    "94cb97f0c729457ab97e702571f4bedb",
    "1fa1f1c12b5744d1a1496cf13242f59b",
    "573d17b596ad422794a1ced00299c51d",
    "236b4d3d35c44da59a683486af03dc83",
    "d70decd2dbc549c78d7b4e0d8e9e6ac6",
}
EXPECTED_TAIL_ARTIFACT = "841b1dd8df2e4370a29a562680f4af03"
REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_BOARD_ROOT = Path.home() / ".local/state/smateway/boards" / BOARD_ID
DEFAULT_OUTPUT = REPOSITORY / "docs/schedule_alignment_red_green/data/capture-evidence.json"
DEFAULT_SUMMARY_OUTPUT = (
    REPOSITORY / "docs/schedule_alignment_red_green/data/captured-validation.json"
)
PROFILE_PATH = REPOSITORY / "profiles/fast20-v1/control_profile.json"
CONDUCTED_FIXTURE_ID = "tx1-2way-rx1-and-8way-board-rx2-v1"
REPEAT = re.compile(r"repeat(\d+)$")


class EvidenceError(RuntimeError):
    """A source artifact does not match the frozen validation scope."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{label} must be numeric")
    return float(value)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"{label} must be an integer")
    return value


def _read(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot read {label} {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repeat_index(run_directory: Path) -> int:
    match = REPEAT.search(run_directory.name)
    if match is None:
        raise EvidenceError(f"cannot parse repeat index from {run_directory.name}")
    return int(match.group(1))


def _manifest_paths(board_root: Path, cohort: str) -> tuple[Path, ...]:
    sweep_root = board_root / "closed-loop-frequency-sweeps"
    if cohort == "focused":
        prefix = "broadband-board-calibration-20260828-r0-lowband-repeat"
        count = 5
    else:
        prefix = "broadband-board-calibration-20260828-r0-repeat"
        count = 25
    paths = tuple(
        sweep_root / f"{prefix}{index}" / "manifest.json" for index in range(1, count + 1)
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise EvidenceError(f"missing {cohort} manifests: {missing}")
    return paths


def _capture_row(
    board_root: Path,
    cohort: str,
    manifest_path: Path,
    attempt: Mapping[str, Any],
    profile_contract_sha256: str,
) -> dict[str, Any]:
    artifact_id = str(attempt.get("artifact_id"))
    artifact_directory = board_root / "pluto-usb-captures" / artifact_id
    strict_path = artifact_directory / "fast20-reference-transfer-v2.json"
    global_path = artifact_directory / "fast20-reference-transfer-v2-global.json"
    if strict_path.is_file():
        current_status = "admitted"
        selected_path = strict_path
    elif global_path.is_file():
        current_status = "quarantined"
        selected_path = global_path
    else:
        raise EvidenceError(f"{artifact_id}: no v2 analysis sidecar exists")
    selected = _read(selected_path, "selected v2 sidecar")

    if selected.get("source_commit") != EXPECTED_SOURCE_COMMIT:
        raise EvidenceError(f"{artifact_id}: unexpected v2 source commit")
    artifact = _mapping(selected.get("artifact"), "artifact")
    identity = _mapping(attempt.get("artifact_identity"), "manifest artifact identity")
    if artifact.get("artifact_id") != artifact_id:
        raise EvidenceError(f"{artifact_id}: sidecar identity differs")
    if artifact.get("sha256") != identity.get("sha256"):
        raise EvidenceError(f"{artifact_id}: manifest and sidecar SHA-256 differ")

    raw_path = artifact_directory / f"{artifact_id}.sigmf-data"
    meta_path = artifact_directory / f"{artifact_id}.sigmf-meta"
    if not raw_path.is_file() or not meta_path.is_file():
        raise EvidenceError(f"{artifact_id}: raw SigMF pair is incomplete")
    meta = _read(meta_path, "SigMF metadata")
    global_meta = _mapping(meta.get("global"), "SigMF global metadata")
    if global_meta.get("pluto:artifact_id") != artifact_id:
        raise EvidenceError(f"{artifact_id}: SigMF metadata artifact ID differs")
    if global_meta.get("pluto:sha256") != artifact.get("sha256"):
        raise EvidenceError(f"{artifact_id}: SigMF metadata artifact SHA-256 differs")

    capture = _mapping(selected.get("capture"), "capture")
    headroom = _mapping(capture.get("adc_headroom_admission"), "ADC headroom admission")
    if headroom.get("passed") is not True:
        raise EvidenceError(f"{artifact_id}: ADC headroom admission failed")
    if capture.get("profile_contract_sha256") != profile_contract_sha256:
        raise EvidenceError(f"{artifact_id}: Fast20 profile contract differs")
    if _number(capture.get("duration_s"), "capture duration") != 10.0:
        raise EvidenceError(f"{artifact_id}: capture duration changed")
    if _integer(capture.get("sample_rate_hz"), "captured sample rate") != 1_000_000:
        raise EvidenceError(f"{artifact_id}: captured sample rate changed")
    if _integer(capture.get("tx_channel"), "captured TX channel") != 0:
        raise EvidenceError(f"{artifact_id}: captured TX channel changed")
    if _number(capture.get("receiver_gain_db"), "captured receiver gain") != 40.0:
        raise EvidenceError(f"{artifact_id}: captured receiver gain changed")
    if capture.get("conducted_fixture_id") != CONDUCTED_FIXTURE_ID:
        raise EvidenceError(f"{artifact_id}: conducted fixture identity changed")
    if capture.get("fully_conducted_user_confirmation") is not True:
        raise EvidenceError(f"{artifact_id}: conducted fixture confirmation is missing")

    isolation_path = artifact_directory / "fast20-dwell-isolation.json"
    isolation = _read(isolation_path, "dwell-isolation analysis")
    isolation_artifact = _mapping(isolation.get("artifact"), "isolation artifact")
    isolation_capture = _mapping(isolation.get("capture"), "isolation capture")
    if isolation_artifact.get("artifact_id") != artifact_id:
        raise EvidenceError(f"{artifact_id}: isolation artifact ID differs")
    if isolation_artifact.get("sha256") != artifact.get("sha256"):
        raise EvidenceError(f"{artifact_id}: isolation artifact SHA-256 differs")
    dwell_isolation = _mapping(isolation.get("dwell_isolation"), "dwell isolation")
    if dwell_isolation.get("continuity_verified") is not True:
        raise EvidenceError(f"{artifact_id}: dwell-isolation continuity failed")
    if dwell_isolation.get("threshold_stable") is not True:
        raise EvidenceError(f"{artifact_id}: dwell threshold sweep is unstable")

    transfer = _mapping(selected.get("transfer"), "transfer")
    if transfer.get("continuity_verified") is not True:
        raise EvidenceError(f"{artifact_id}: reference-transfer continuity failed")
    alignment = _mapping(transfer.get("schedule_alignment"), "schedule alignment")
    search = _mapping(alignment.get("search"), "alignment search")
    selected_fit = _mapping(
        _mapping(alignment.get("selected"), "selected alignment").get("fit"), "fit"
    )
    decoded = _mapping(alignment.get("decoded_timing"), "decoded timing")
    agreement = _mapping(alignment.get("decoder_agreement"), "decoder agreement")
    quality_gate = _mapping(selected.get("quality_gate"), "quality gate")
    states = tuple(
        _mapping(state, "state") for state in _sequence(transfer.get("states"), "states")
    )
    if len(states) != 8 or tuple(state.get("name") for state in states) != tuple(
        f"ANT{index}" for index in range(1, 9)
    ):
        raise EvidenceError(f"{artifact_id}: state order is not ANT1 through ANT8")

    repeat_index = _repeat_index(manifest_path.parent)
    frequency_hz = _integer(attempt.get("center_frequency_hz"), "center frequency")
    sigmf_captures = tuple(
        _mapping(item, "SigMF capture")
        for item in _sequence(meta.get("captures"), "SigMF captures")
    )
    if len(sigmf_captures) != 1:
        raise EvidenceError(f"{artifact_id}: expected exactly one SigMF capture segment")
    sigmf_settings = _mapping(sigmf_captures[0].get("settings"), "SigMF capture settings")
    aggregation_key = _mapping(selected.get("aggregation_key"), "aggregation key")
    frequency_authorities = {
        "manifest": float(frequency_hz),
        "analysis artifact": _number(
            artifact.get("center_frequency_hz"), "artifact center frequency"
        ),
        "analysis capture": _number(capture.get("center_frequency_hz"), "capture center frequency"),
        "analysis aggregation key": _number(
            aggregation_key.get("center_frequency_hz"), "aggregation center frequency"
        ),
        "isolation artifact": _number(
            isolation_artifact.get("center_frequency_hz"),
            "isolation artifact center frequency",
        ),
        "isolation capture": _number(
            isolation_capture.get("center_frequency_hz"),
            "isolation capture center frequency",
        ),
        "SigMF settings": _number(
            sigmf_settings.get("center_frequency_hz"), "SigMF center frequency"
        ),
    }
    if any(value != float(frequency_hz) for value in frequency_authorities.values()):
        raise EvidenceError(
            f"{artifact_id}: center-frequency authorities disagree: {frequency_authorities}"
        )
    legacy_outcome = str(attempt.get("outcome"))
    if legacy_outcome not in {"quality_passed", "quality_rejected"}:
        raise EvidenceError(f"{artifact_id}: unsupported legacy outcome {legacy_outcome}")
    rejection_reasons = list(_sequence(quality_gate.get("global_rejection_reasons"), "reasons"))
    if current_status == "admitted" and rejection_reasons:
        raise EvidenceError(f"{artifact_id}: admitted result retains global rejection reasons")
    if current_status == "quarantined" and rejection_reasons != [
        "schedule_transition_decoder_rejected_markers"
    ]:
        raise EvidenceError(f"{artifact_id}: quarantine reason changed: {rejection_reasons}")
    expected_gate = current_status == "admitted"
    if (quality_gate.get("passed") is True) != expected_gate:
        raise EvidenceError(f"{artifact_id}: quality-gate result contradicts v2 status")
    expected_mode = "transition_seeded" if expected_gate else "global_refined"
    if search.get("mode") != expected_mode:
        raise EvidenceError(f"{artifact_id}: expected {expected_mode} search")
    if agreement.get("agrees") is not True:
        raise EvidenceError(f"{artifact_id}: selected fit disagrees with decoded timing")
    if any(state.get("quality_passed") is not True for state in states):
        raise EvidenceError(f"{artifact_id}: one or more exact-tone state gates failed")
    reference_valid_fraction = _number(
        transfer.get("reference_valid_bin_fraction"), "reference-valid fraction"
    )
    minimum_reference_fraction = _number(
        quality_gate.get("minimum_reference_valid_bin_fraction"),
        "minimum reference-valid fraction",
    )
    if reference_valid_fraction < minimum_reference_fraction:
        raise EvidenceError(f"{artifact_id}: reference-valid fraction failed")

    state_evidence = []
    for state in states:
        corrected = _mapping(state.get("all_off_subtracted_rx2_over_rx1"), "state transfer")
        state_evidence.append(
            {
                "name": state["name"],
                "quality_passed": True,
                "detection_snr_db": _number(state.get("transfer_detection_snr_db"), "state SNR"),
                "cycle_coherence": _number(corrected.get("cycle_coherence"), "state coherence"),
                "cycle_phase_std_deg": _number(
                    corrected.get("cycle_phase_std_deg"), "state phase spread"
                ),
            }
        )

    return {
        "cohort": cohort,
        "repeat_index": repeat_index,
        "run_id": manifest_path.parent.name,
        "manifest_sha256": _sha256(manifest_path),
        "artifact_id": artifact_id,
        "artifact_sha256": artifact["sha256"],
        "raw_iq_byte_size": raw_path.stat().st_size,
        "analysis_sidecar": selected_path.name,
        "analysis_sha256": _sha256(selected_path),
        "isolation_analysis_sha256": _sha256(isolation_path),
        "center_frequency_hz": frequency_hz,
        "tx_channel": _integer(attempt.get("tx_channel"), "TX channel"),
        "rotation": _integer(attempt.get("rotation"), "rotation"),
        "sample_rate_hz": _integer(attempt.get("sample_rate_hz"), "sample rate"),
        "receiver_gain_db": _number(attempt.get("receiver_gain_db"), "receiver gain"),
        "legacy_manifest_outcome": legacy_outcome,
        "v2_status": current_status,
        "global_rejection_reasons": rejection_reasons,
        "quality_gate_passed": quality_gate.get("passed") is True,
        "capture_headroom_admission_passed": True,
        "continuity_verified": True,
        "reference_valid_bin_fraction": reference_valid_fraction,
        "search_mode": search.get("mode"),
        "candidate_count": _integer(search.get("candidate_count"), "candidate count"),
        "combined_score": _number(selected_fit.get("combined_score"), "combined score"),
        "explained_fraction": _number(selected_fit.get("explained_fraction"), "explained fraction"),
        "residual_fraction": _number(selected_fit.get("residual_fraction"), "residual fraction"),
        "cycle_ms": _number(
            _mapping(alignment.get("selected"), "selected").get("cycle_ms"), "cycle"
        ),
        "marker_phase_ms": _number(
            _mapping(alignment.get("selected"), "selected").get("marker_phase_ms"), "marker phase"
        ),
        "complete_cycle_count": _integer(transfer.get("complete_cycle_count"), "complete cycles"),
        "decoder_strict_frame_count": _integer(decoded.get("strict_frame_count"), "strict frames"),
        "decoder_marker_count": _integer(decoded.get("marker_count"), "marker count"),
        "decoder_rejected_marker_count": _integer(
            decoded.get("rejected_marker_count"), "rejected markers"
        ),
        "decoder_agrees": agreement.get("agrees") is True,
        "decoder_cycle_error_ms": _number(agreement.get("cycle_error_ms"), "cycle error"),
        "decoder_marker_error_ms": _number(agreement.get("marker_error_ms"), "marker error"),
        "decoder_cycle_tolerance_ms": _number(
            agreement.get("cycle_tolerance_ms"), "cycle tolerance"
        ),
        "decoder_marker_tolerance_ms": _number(
            agreement.get("marker_tolerance_ms"), "marker tolerance"
        ),
        "isolation_complete_frame_count": _integer(
            dwell_isolation.get("complete_frame_count"), "isolation complete frames"
        ),
        "isolation_marker_count": _integer(
            dwell_isolation.get("marker_count"), "isolation marker count"
        ),
        "isolation_rejected_marker_count": _integer(
            dwell_isolation.get("rejected_marker_count"), "isolation rejected markers"
        ),
        "isolation_threshold_stable": True,
        "isolation_threshold_sweep_frame_counts": list(
            _sequence(
                dwell_isolation.get("threshold_sweep_frame_counts"),
                "threshold sweep frame counts",
            )
        ),
        "isolation_verified": dwell_isolation.get("isolation_verified") is True,
        "distinct_runner_up_present": alignment.get("distinct_runner_up") is not None,
        "state_gate_pass_count": len(state_evidence),
        "minimum_state_detection_snr_db": min(
            state["detection_snr_db"] for state in state_evidence
        ),
        "minimum_state_cycle_coherence": min(state["cycle_coherence"] for state in state_evidence),
        "maximum_state_cycle_phase_std_deg": max(
            state["cycle_phase_std_deg"] for state in state_evidence
        ),
        "states": state_evidence,
    }


def collect(board_root: Path) -> dict[str, Any]:
    profile = _read(PROFILE_PATH, "Fast20 control profile")
    profile_contract_sha256 = str(profile.get("contract_sha256"))
    if not re.fullmatch(r"[0-9a-f]{64}", profile_contract_sha256):
        raise EvidenceError("Fast20 profile contract SHA-256 is malformed")
    profile_states = tuple(
        _mapping(state, "Fast20 profile state")
        for state in _sequence(profile.get("states"), "Fast20 profile states")
    )
    antenna_dwells_ms = tuple(
        _integer(state.get("dwell_ms"), "Fast20 antenna dwell") for state in profile_states
    )
    if antenna_dwells_ms != (20, 23, 26, 30, 34, 39, 44, 50):
        raise EvidenceError("Fast20 unique-dwell sequence changed")
    frame = _mapping(profile.get("frame"), "Fast20 frame")
    marker = _mapping(frame.get("marker"), "Fast20 marker")
    all_off_guard_ms = _integer(frame.get("all_off_guard_ms"), "ALL_OFF guard")
    marker_body_ms = _integer(marker.get("body_nominal_ms"), "marker body")
    nominal_cycle_ms = _integer(frame.get("nominal_cycle_ms"), "nominal cycle")
    captures: list[dict[str, Any]] = []
    for cohort in ("focused", "historical"):
        for manifest_path in _manifest_paths(board_root, cohort):
            manifest = _read(manifest_path, "sweep manifest")
            attempts = tuple(
                _mapping(attempt, "attempt")
                for attempt in _sequence(manifest.get("attempts"), "attempts")
                if attempt.get("center_frequency_hz") in FREQUENCIES_HZ
            )
            if tuple(attempt.get("center_frequency_hz") for attempt in attempts) != FREQUENCIES_HZ:
                raise EvidenceError(f"{manifest_path}: low-band attempt sequence changed")
            captures.extend(
                _capture_row(
                    board_root,
                    cohort,
                    manifest_path,
                    attempt,
                    profile_contract_sha256,
                )
                for attempt in attempts
            )

    if len(captures) != 150 or len({row["artifact_id"] for row in captures}) != 150:
        raise EvidenceError("expected 150 distinct captures")
    if any(row["tx_channel"] != 0 or row["rotation"] != 0 for row in captures):
        raise EvidenceError("scope changed from TX1 rotation 0")
    if any(row["sample_rate_hz"] != 1_000_000 for row in captures):
        raise EvidenceError("scope changed from 1 MS/s")
    if any(row["receiver_gain_db"] != 40.0 for row in captures):
        raise EvidenceError("scope changed from 40 dB receiver gain")
    if any(row["raw_iq_byte_size"] != 80_000_000 for row in captures):
        raise EvidenceError("one or more raw IQ file sizes changed")
    if any(
        not row["decoder_agrees"]
        or not row["capture_headroom_admission_passed"]
        or not row["continuity_verified"]
        or row["state_gate_pass_count"] != 8
        for row in captures
    ):
        raise EvidenceError("one or more universal v2 evidence gates failed")
    quarantines = {row["artifact_id"] for row in captures if row["v2_status"] == "quarantined"}
    if quarantines != EXPECTED_QUARANTINES:
        raise EvidenceError(f"quarantine set changed: {sorted(quarantines)}")
    quarantine_rows = [row for row in captures if row["v2_status"] == "quarantined"]
    if any(
        row["quality_gate_passed"]
        or row["search_mode"] != "global_refined"
        or row["decoder_strict_frame_count"] != 25
        or row["decoder_marker_count"] != 26
        or row["decoder_rejected_marker_count"] != 1
        or row["isolation_complete_frame_count"] != 25
        or row["isolation_marker_count"] != 26
        or row["isolation_rejected_marker_count"] != 1
        or row["isolation_threshold_sweep_frame_counts"] != [25, 25, 25]
        for row in quarantine_rows
    ):
        raise EvidenceError("quarantine timing evidence changed")
    admitted_rows = [row for row in captures if row["v2_status"] == "admitted"]
    if any(
        not row["quality_gate_passed"] or row["search_mode"] != "transition_seeded"
        for row in admitted_rows
    ):
        raise EvidenceError("strict admission evidence changed")
    if not any(row["artifact_id"] == EXPECTED_TAIL_ARTIFACT for row in captures):
        raise EvidenceError("known admitted quality-tail artifact is missing")

    reconciliation: dict[str, Any] = {}
    for cohort in ("focused", "historical", "total"):
        rows = (
            captures if cohort == "total" else [row for row in captures if row["cohort"] == cohort]
        )
        legacy = Counter(row["legacy_manifest_outcome"] for row in rows)
        current = Counter(row["v2_status"] for row in rows)
        cross_tab = Counter(f"{row['legacy_manifest_outcome']}->{row['v2_status']}" for row in rows)
        reconciliation[cohort] = {
            "capture_count": len(rows),
            "legacy_manifest": {
                "passed": legacy["quality_passed"],
                "rejected": legacy["quality_rejected"],
            },
            "v2_reanalysis": {
                "admitted": current["admitted"],
                "quarantined": current["quarantined"],
            },
            "cross_tab": dict(sorted(cross_tab.items())),
        }

    per_frequency = []
    for frequency_hz in FREQUENCIES_HZ:
        rows = [row for row in captures if row["center_frequency_hz"] == frequency_hz]
        per_frequency.append(
            {
                "center_frequency_hz": frequency_hz,
                "capture_count": len(rows),
                "legacy_pass_count": sum(
                    row["legacy_manifest_outcome"] == "quality_passed" for row in rows
                ),
                "v2_admitted_count": sum(row["v2_status"] == "admitted" for row in rows),
                "v2_quarantined_count": sum(row["v2_status"] == "quarantined" for row in rows),
            }
        )

    return {
        "schema": 1,
        "evidence_kind": "schedule_alignment_v2_captured_validation",
        "verified_at": "2026-08-29",
        "host": "devpi",
        "board_id": BOARD_ID,
        "collector": {
            "path": Path(__file__).resolve().relative_to(REPOSITORY).as_posix(),
            "sha256": _sha256(Path(__file__)),
        },
        "analysis_source_commit": EXPECTED_SOURCE_COMMIT,
        "scope": {
            "frequencies_hz": list(FREQUENCIES_HZ),
            "tx_channel": 0,
            "rotation": 0,
            "sample_rate_hz": 1_000_000,
            "receiver_gain_db": 40,
            "capture_duration_s": 10,
            "antenna_dwells_ms": list(antenna_dwells_ms),
            "all_off_guard_ms": all_off_guard_ms,
            "marker_body_nominal_ms": marker_body_ms,
            "nominal_cycle_ms": nominal_cycle_ms,
            "conducted_fixture_id": CONDUCTED_FIXTURE_ID,
            "profile_contract_sha256": profile_contract_sha256,
            "profile_sha256": _sha256(PROFILE_PATH),
            "focused_repeat_count": 5,
            "historical_repeat_count": 25,
        },
        "source_layout": {
            "board_root": f"~/.local/state/smateway/boards/{BOARD_ID}",
            "capture": "pluto-usb-captures/<artifact-id>",
            "focused_manifest": (
                "closed-loop-frequency-sweeps/broadband-board-calibration-"
                "20260828-r0-lowband-repeat<1..5>/manifest.json"
            ),
            "historical_manifest": (
                "closed-loop-frequency-sweeps/broadband-board-calibration-"
                "20260828-r0-repeat<1..25>/manifest.json"
            ),
        },
        "integrity": {
            "capture_count": len(captures),
            "unique_artifact_count": len({row["artifact_id"] for row in captures}),
            "raw_iq_byte_size": sum(row["raw_iq_byte_size"] for row in captures),
            "all_declared_manifest_sigmf_analysis_hashes_agree": True,
            "raw_iq_content_rehashed_during_collection": False,
            "all_raw_sigmf_pairs_present": True,
        },
        "decision_reconciliation": reconciliation,
        "per_frequency_decisions": per_frequency,
        "known_admitted_quality_tail_artifact": EXPECTED_TAIL_ARTIFACT,
        "captures": captures,
    }


def _distribution(values: Sequence[float]) -> dict[str, float]:
    if not values:
        raise EvidenceError("cannot summarize an empty distribution")
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _cohort_summary(captures: Sequence[Mapping[str, Any]], cohort: str) -> dict[str, Any]:
    requested = [row for row in captures if row["cohort"] == cohort]
    admitted = [row for row in requested if row["v2_status"] == "admitted"]
    states = [
        _mapping(state, "captured state evidence")
        for row in admitted
        for state in _sequence(row.get("states"), "captured states")
    ]
    result: dict[str, Any] = {
        "requested_capture_count": len(requested),
        "strict_pass_count": len(admitted),
        "quarantined_count": len(requested) - len(admitted),
        "frequency_range_hz": [FREQUENCIES_HZ[0], FREQUENCIES_HZ[-1]],
        "combined_score": _distribution(
            [_number(row["combined_score"], "combined score") for row in admitted]
        ),
        "explained_fraction": _distribution(
            [_number(row["explained_fraction"], "explained fraction") for row in admitted]
        ),
        "residual_fraction": _distribution(
            [_number(row["residual_fraction"], "residual fraction") for row in admitted]
        ),
        "decoder_marker_error_ms": _distribution(
            [_number(row["decoder_marker_error_ms"], "marker error") for row in admitted]
        ),
        "state_estimate_count": len(states),
        "state_detection_snr_db": _distribution(
            [_number(state["detection_snr_db"], "state SNR") for state in states]
        ),
        "state_cycle_coherence": _distribution(
            [_number(state["cycle_coherence"], "state coherence") for state in states]
        ),
        "state_cycle_phase_std_deg": _distribution(
            [_number(state["cycle_phase_std_deg"], "state phase spread") for state in states]
        ),
    }
    if cohort == "historical":
        result["decoder_cycle_error_ms"] = _distribution(
            [_number(row["decoder_cycle_error_ms"], "cycle error") for row in admitted]
        )
    return result


def build_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    captures = tuple(
        _mapping(row, "capture evidence") for row in _sequence(evidence.get("captures"), "captures")
    )
    quarantines = sorted(
        (row for row in captures if row["v2_status"] == "quarantined"),
        key=lambda row: (row["repeat_index"], row["center_frequency_hz"]),
    )
    return {
        "schema": 2,
        "verified_at": evidence["verified_at"],
        "host": evidence["host"],
        "source_commit": evidence["analysis_source_commit"],
        "generated_from": "data/capture-evidence.json",
        "focused_low_band_5pass": _cohort_summary(captures, "focused"),
        "historical_low_band_25pass": _cohort_summary(captures, "historical"),
        "quarantined_global_fallbacks": [
            {
                "artifact_id": row["artifact_id"],
                "repeat_index": row["repeat_index"],
                "frequency_hz": row["center_frequency_hz"],
                "tx_channel": row["tx_channel"],
                "complete_frame_count": row["decoder_strict_frame_count"],
                "rejected_marker_count": row["decoder_rejected_marker_count"],
                "combined_score": row["combined_score"],
                "explained_fraction": row["explained_fraction"],
                "residual_fraction": row["residual_fraction"],
                "decoder_agrees": row["decoder_agrees"],
                "quality_passed": row["quality_gate_passed"],
                "global_rejection_reasons": row["global_rejection_reasons"],
            }
            for row in quarantines
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-root", type=Path, default=DEFAULT_BOARD_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        evidence = collect(args.board_root)
        encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        summary = build_summary(evidence)
        summary_encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        if args.check:
            if args.output.read_text(encoding="utf-8") != encoded:
                raise EvidenceError(f"committed evidence is stale: {args.output}")
            if args.summary_output.read_text(encoding="utf-8") != summary_encoded:
                raise EvidenceError(f"committed summary is stale: {args.summary_output}")
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "captures_checked": len(evidence["captures"]),
                        "summary_checked": True,
                    }
                )
            )
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
            args.summary_output.parent.mkdir(parents=True, exist_ok=True)
            args.summary_output.write_text(summary_encoded, encoding="utf-8")
            print(
                json.dumps(
                    {
                        "status": "written",
                        "captures": len(evidence["captures"]),
                        "output": str(args.output),
                        "summary_output": str(args.summary_output),
                    }
                )
            )
    except (EvidenceError, OSError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
