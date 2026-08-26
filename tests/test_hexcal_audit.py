from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_hexcal_calibration.py"
SPEC = importlib.util.spec_from_file_location("hexcal_audit_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)

ARTIFACT_ID = "a" * 32
BOARD_ID = "stm32c011-4c0055000950313950363920"
SERIAL = "serial-a"
URI = "usb:1.2.3"
PROFILE_SHA = "b" * 64
SOURCE_COMMIT = "1" * 40
FLAGS = (1 << 4) | (1 << 21)
DDS_READBACK = [100_000.0, 0.0, -100_000.0, 0.0, 0.0, 0.0, 0.0, 0.0]
SOURCE_ATTESTATION = {"commit": SOURCE_COMMIT, "files": []}
DEPENDENCY_ATTESTATION = {
    "schema": 1,
    "dependency": "pluto-plus-utils",
    "commit": "2" * 40,
    "files": [],
}
DEPENDENCY_SHA = audit.canonical_json_sha256(DEPENDENCY_ATTESTATION)
QUALIFICATION_SOURCE_ATTESTATION = {
    "schema": 1,
    "commit": SOURCE_COMMIT,
    "files": [],
}
GAIN_QUALIFICATION = {
    "path": "/evidence/hexcal-gain-qualification.json",
    "file_sha256": "7" * 64,
    "qualification_id": "gain-a",
    "board_id": BOARD_ID,
    "serial": SERIAL,
    "uri": URI,
    "source_commit": SOURCE_COMMIT,
    "profile_file_sha256": PROFILE_SHA,
    "profile_contract_sha256": "3" * 64,
    "firmware_evidence_sha256": "c" * 64,
    "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
    "center_frequencies_hz": [2_440_000_000],
    "candidate_gains_db": [0, 1, 2],
    "tested_gains_db": [0, 1, 2],
    "selected_receiver_gain_db": 2,
    "completed_at": "2026-08-26T11:00:00+00:00",
}
RF_READBACK = {
    "schema": 1,
    "evidence_kind": "pluto_tx1_dds_live_readback",
    "tx_channel": 0,
    "tx_port": "TX1",
    "kernel_buffers": 8,
    "tx_hardware_gain_db_requested": -40.0,
    "tx_hardware_gain_readback_db_by_channel": [-40.0, -80.0],
    "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
    "dds_scale_requested": 0.125,
    "dds_scale_readback": [0.125, 0.0, -0.125, 0.0, 0.0, 0.0, 0.0, 0.0],
    "dds_enabled_readback": [True, False, True, False, False, False, False, False],
    "tone_frequency_hz_requested": 100_000.0,
    "dds_frequency_readback_hz": [100_000, 0, -100_000, 0, 0, 0, 0, 0],
    "active_dds_indices": [0, 2],
    "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
    "inactive_dds_rf_activity_contract": (
        "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
    ),
}
RF_READBACK_SHA = audit.canonical_json_sha256(RF_READBACK)


def _firmware_evidence() -> dict[str, Any]:
    return {
        "path": "/evidence/firmware-evidence.json",
        "file_sha256": "c" * 64,
        "board_id": BOARD_ID,
        "target_uid": "4c0055000950313950363920",
        "target_uid_readback_path": "/evidence/target-uid.bin",
        "target_uid_readback_sha256": "9" * 64,
        "target_uid_readback_size_bytes": 12,
        "source_commit": SOURCE_COMMIT,
        "profile_file_sha256": PROFILE_SHA,
        "profile_contract_sha256": "3" * 64,
        "firmware_elf_path": "/evidence/hexcal.elf",
        "firmware_elf_sha256": "a" * 64,
        "firmware_elf_size_bytes": 304428,
        "firmware_bin_path": "/evidence/hexcal.bin",
        "firmware_bin_sha256": "d" * 64,
        "firmware_bin_size_bytes": 4096,
        "full_flash_readback_path": "/evidence/full-flash.bin",
        "full_flash_readback_sha256": "e" * 64,
        "full_flash_readback_size_bytes": 16 * 1024,
        "verified_at": "2026-08-26T12:00:00+00:00",
        "verification_method": "synthetic full flash readback",
        "image_prefix_matches": True,
        "erased_tail_verified": True,
        "target_identity_verified": True,
    }


def _metadata() -> dict[str, Any]:
    first_sample = 10_000_000
    blocks = []
    for index in range(10):
        start = index * 100_000
        blocks.append(
            {
                "sample_start": start,
                "sample_count": 100_000,
                "utc_ns": 1_000_000_000 + start * 1000,
                "metadata_abi": 2,
                "stream_id": 777,
                "buffer_sequence": index,
                "first_sample_sequence": first_sample + start,
                "last_sample_sequence_exclusive": first_sample + start + 100_000,
                "metadata_flags": FLAGS,
                "missing_samples_before": 0,
                "sample_time_realtime_start_ns": 2_000_000_000 + start * 1000,
                "sample_time_realtime_end_ns": 2_100_000_000 + start * 1000,
                "sample_time_monotonic_start_ns": 3_000_000_000 + start * 1000,
                "sample_time_monotonic_end_ns": 3_100_000_000 + start * 1000,
                "sample_time_uncertainty_ns": 10_000,
            }
        )
    return {
        "pluto:capture": {"sample_count": 1_000_000, "receiver_count": 2},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "stream_id": 777,
            "block_count": 10,
            "total_samples": 1_000_000,
            "first_sample_sequence": first_sample,
            "last_sample_sequence_exclusive": first_sample + 1_000_000,
            "sample_sequence_span": 1_000_000,
            "blocks": blocks,
        },
    }


def _write_artifact(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    data_file = root / f"{ARTIFACT_ID}.sigmf-data"
    with data_file.open("wb") as stream:
        stream.truncate(8_000_000)
    meta_file = root / f"{ARTIFACT_ID}.sigmf-meta"
    meta_file.write_text(json.dumps(_metadata(), sort_keys=True) + "\n", encoding="utf-8")
    data_sha = audit.sha256_path(data_file)
    meta_sha = audit.sha256_path(meta_file)
    zero_receiver = {
        "sample_count": 1_000_000,
        "peak_abs_component_counts": 0.0,
        "clipped_sample_count": 0,
        "near_full_scale_sample_count": 0,
        "near_full_scale_fraction": 0.0,
        "passed": True,
    }
    capture = {
        "source_commit": SOURCE_COMMIT,
        "pluto_plus_utils_source_attestation": DEPENDENCY_ATTESTATION,
        "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
        "artifact": {
            "artifact_id": ARTIFACT_ID,
            "path": str(root),
            "sha256": data_sha,
        },
        "artifact_evidence": {
            "data_sha256": data_sha,
            "metadata_sha256": meta_sha,
            "metadata_size_bytes": meta_file.stat().st_size,
        },
        "source_profile": {"file_sha256": PROFILE_SHA},
        "firmware_evidence": _firmware_evidence(),
        "capture": {
            "board_id": BOARD_ID,
            "serial": SERIAL,
            "uri": URI,
            "tx_channel": 0,
            "tx_port": "TX1",
            "center_frequency_hz": 2_440_000_000,
            "sample_rate_hz": 1_000_000,
            "sample_count": 1_000_000,
            "samples_per_frame": 100_000,
            "frame_count": 10,
            "kernel_buffers": 8,
            "tx_hardware_gain_db_requested": -40.0,
            "tx_gain_readback_db": -40.0,
            "dds_scale_requested": 0.125,
            "dds_scale_readback": RF_READBACK["dds_scale_readback"],
            "dds_enabled_readback": RF_READBACK["dds_enabled_readback"],
            "dds_frequency_readback_hz": DDS_READBACK,
            "rf_readback_evidence": RF_READBACK,
            "rf_readback_evidence_sha256": RF_READBACK_SHA,
            "adc_headroom_admission": {
                "passed": True,
                "receivers": [
                    {"receiver": 0, **zero_receiver},
                    {"receiver": 1, **zero_receiver},
                ],
            },
        },
    }
    capture_file = root / audit.CAPTURE_FILENAME
    capture_file.write_text(json.dumps(capture, sort_keys=True) + "\n", encoding="utf-8")
    capture_sha = audit.sha256_path(capture_file)
    analysis = {
        "source_commit": SOURCE_COMMIT,
        "analysis_source_attestation": SOURCE_ATTESTATION,
        "pluto_plus_utils_source_attestation": DEPENDENCY_ATTESTATION,
        "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
        "artifact_evidence": {
            "data_sha256": data_sha,
            "metadata_sha256": meta_sha,
            "capture_record_sha256": capture_sha,
        },
        "aggregation_key": {
            "artifact_id": ARTIFACT_ID,
            "serial": SERIAL,
            "uri": URI,
            "profile_file_sha256": PROFILE_SHA,
            "center_frequency_hz": 2_440_000_000,
            "implementation_source_commit": SOURCE_COMMIT,
            "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
            "firmware_evidence_sha256": "c" * 64,
            "firmware_bin_sha256": "d" * 64,
            "full_flash_readback_sha256": "e" * 64,
            "rf_readback_evidence_sha256": RF_READBACK_SHA,
            "dds_frequency_readback_hz": DDS_READBACK,
            "dds_tone_offset_hz": 100_000.0,
            "emitted_carrier_frequency_hz": 2_440_100_000.0,
        },
        "quality_gate": {"passed": True},
        "hexcal": {"synthetic": True},
    }
    analysis_file = root / audit.ANALYSIS_FILENAME
    analysis_file.write_text(json.dumps(analysis, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "artifact_id": ARTIFACT_ID,
        "path": str(root),
        "data_sha256": data_sha,
        "data_size_bytes": data_file.stat().st_size,
        "metadata_sha256": meta_sha,
        "metadata_size_bytes": meta_file.stat().st_size,
        "capture_record_sha256": capture_sha,
        "capture_record_size_bytes": capture_file.stat().st_size,
        "analysis_sha256": audit.sha256_path(analysis_file),
        "analysis_size_bytes": analysis_file.stat().st_size,
        "stream_id": 777,
        "metadata_abi": 2,
        "implementation_source_commit": SOURCE_COMMIT,
        "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
        "firmware_evidence_sha256": "c" * 64,
        "firmware_bin_sha256": "d" * 64,
        "full_flash_readback_sha256": "e" * 64,
        "rf_readback_evidence_sha256": RF_READBACK_SHA,
        "dds_frequency_readback_hz": DDS_READBACK,
        "dds_tone_offset_hz": 100_000.0,
        "emitted_carrier_frequency_hz": 2_440_100_000.0,
    }


def _attempt(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_identity": identity,
        "center_frequency_hz": 2_440_000_000,
        "round_index": 0,
        "outcome": "quality_passed",
        "implementation_source_commit": SOURCE_COMMIT,
        "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
        "planned_tx_hardware_gain_db": -40.0,
        "planned_dds_scale": 0.125,
        "firmware_evidence_sha256": "c" * 64,
        "firmware_bin_sha256": "d" * 64,
        "full_flash_readback_sha256": "e" * 64,
    }


def _patch_scientific_replay(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        audit,
        "attest_source_files_at_commit",
        lambda *args, **kwargs: dict(SOURCE_ATTESTATION),
    )
    monkeypatch.setattr(
        audit,
        "attest_pluto_plus_utils_source",
        lambda: dict(DEPENDENCY_ATTESTATION),
    )
    monkeypatch.setattr(audit, "load_ci16_channel", lambda *args, **kwargs: np.zeros(1))
    monkeypatch.setattr(
        audit,
        "analyze_hexcal_samples",
        lambda *args, **kwargs: {"synthetic": True},
    )
    monkeypatch.setattr(
        audit,
        "evaluate_hexcal_quality",
        lambda *args, **kwargs: {"passed": True},
    )


def test_artifact_audit_recomputes_data_meta_analysis_abi2_and_headroom(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_scientific_replay(monkeypatch)
    artifact_root = tmp_path / ARTIFACT_ID
    identity = _write_artifact(artifact_root)
    attempt = _attempt(identity)

    result = audit._audit_artifact(
        attempt,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        profile_sha256=PROFILE_SHA,
        profile=SimpleNamespace(),
        source_commit=SOURCE_COMMIT,
        expected_firmware_evidence=_firmware_evidence(),
        expected_dependency_attestation=DEPENDENCY_ATTESTATION,
        expected_dependency_sha256=DEPENDENCY_SHA,
    )

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["sizes"]["data_size_bytes"] == 8_000_000
    assert result["continuity"]["metadata_abi"] == 2
    assert result["continuity"]["block_count"] == 10
    assert result["raw_adc_headroom"]["passed"] is True


def test_metadata_byte_tampering_is_detected_even_when_json_and_counters_remain_valid(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_scientific_replay(monkeypatch)
    artifact_root = tmp_path / ARTIFACT_ID
    identity = _write_artifact(artifact_root)
    meta_file = artifact_root / f"{ARTIFACT_ID}.sigmf-meta"
    meta_file.write_text(meta_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    attempt = _attempt(identity)

    result = audit._audit_artifact(
        attempt,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        profile_sha256=PROFILE_SHA,
        profile=SimpleNamespace(),
        source_commit=SOURCE_COMMIT,
        expected_firmware_evidence=_firmware_evidence(),
        expected_dependency_attestation=DEPENDENCY_ATTESTATION,
        expected_dependency_sha256=DEPENDENCY_SHA,
    )

    assert result["passed"] is False
    assert any("metadata_sha256" in issue for issue in result["issues"])
    assert result["continuity"]["metadata_abi"] == 2


def test_quarantined_partial_failure_is_hashed_and_can_never_be_accepted(
    tmp_path: Path,
) -> None:
    artifact_id = "f" * 32
    root = tmp_path / ".failed" / artifact_id
    root.mkdir(parents=True)
    failure_record = {
        "artifact_id": artifact_id,
        "error": "OSError: [Errno 61] No data available",
    }
    failure_path = root / "failure.json"
    partial_path = root / "partial.sigmf-data"
    failure_path.write_text(json.dumps(failure_record), encoding="utf-8")
    partial_path.write_bytes(b"partial")
    evidence = {
        "artifact_id": artifact_id,
        "path": str(root),
        "accepted": False,
        "failure_record": failure_record,
        "files": [
            {
                "name": path.name,
                "path": str(path),
                "sha256": audit.sha256_path(path),
                "size_bytes": path.stat().st_size,
            }
            for path in (failure_path, partial_path)
        ],
    }

    result, issues = audit._audit_quarantined_failure(evidence, accepted_artifact_ids=set())
    assert result["passed"] is True
    assert issues == []

    accepted, accepted_issues = audit._audit_quarantined_failure(
        evidence, accepted_artifact_ids={artifact_id}
    )
    assert accepted["passed"] is False
    assert "quarantined failure is also accepted" in accepted_issues


def test_manifest_audit_requires_recovery_after_failed_post_mute(
    tmp_path: Path, monkeypatch: Any
) -> None:
    qualification_calls: list[tuple[Path, dict[str, Any]]] = []

    def replay_qualification(path: Path, **kwargs: Any) -> SimpleNamespace:
        qualification_calls.append((path, kwargs))
        return SimpleNamespace(
            selected_receiver_gain_db=2,
            as_dict=lambda: dict(GAIN_QUALIFICATION),
        )

    condition = {
        "plan_index": 0,
        "receiver_gain_db": 2,
        "gain_qualification_id": "gain-a",
        "gain_qualification_sha256": "7" * 64,
    }
    recovery = {
        "purpose": "resume_recovery",
        "status": "passed",
        "serial": SERIAL,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }
    post_mute = {
        "purpose": "post_attempt",
        "status": "passed",
        "serial": SERIAL,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }
    final_mute = {
        "purpose": "final",
        "status": "passed",
        "serial": SERIAL,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }
    failed = {
        "attempt_id": 1,
        **condition,
        "status": "failed",
        "outcome": "post_mute_failed",
        "failure_kind": "post_attempt_mute",
        "artifact_identity": None,
        "post_mute": {"purpose": "post_attempt", "status": "failed", "serial": SERIAL},
        "quarantined_failures": [],
        "capture": {},
    }
    completed = {
        "attempt_id": 2,
        **condition,
        "status": "complete",
        "outcome": "quality_passed",
        "artifact_identity": {"artifact_id": ARTIFACT_ID, "stream_id": 777},
        "post_mute": post_mute,
    }
    manifest = {
        "schema": 1,
        "experiment_kind": "hexcal_v1_tx1_center_calibration",
        "run_id": "run-a",
        "status": "complete",
        "configuration": {
            "board_id": BOARD_ID,
            "serial": SERIAL,
            "uri": URI,
            "profile": "/profile.json",
            "profile_file_sha256": PROFILE_SHA,
            "profile_contract_sha256": "3" * 64,
            "implementation_source_commit": SOURCE_COMMIT,
            "firmware_evidence": _firmware_evidence(),
            "pluto_plus_utils_source_attestation": DEPENDENCY_ATTESTATION,
            "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
            "gain_qualification": GAIN_QUALIFICATION,
            "center_frequencies_hz": [2_440_000_000],
            "receiver_gain_db": 2,
            "tx_hardware_gain_db": -40.0,
            "dds_scale": 0.125,
        },
        "plan": [condition],
        "attempts": [failed, completed],
        "recovery_mute_attempts": [],
        "final_mute": final_mute,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    scientific_payload = {
        "frequency_results": [{"center_frequency_hz": 2_440_000_000}],
        "leave_one_frequency_out_2g4": {"folds": []},
        "missing_passing_frequencies_hz": [],
        "quality_gate": {
            "passed": True,
            "all_planned_frequencies_have_passing_repeats": True,
            "all_frequency_gates_passed": True,
        },
    }
    calibration = {
        "schema": 1,
        "calibration_kind": "hexcal_v1_tx1_center_end_to_end_complex_correction",
        "source_commit": SOURCE_COMMIT,
        "aggregation_source_attestation": QUALIFICATION_SOURCE_ATTESTATION,
        "aggregation_python_runtime": {"executable": None, "prefix": None},
        "capture_implementation_source_commit": SOURCE_COMMIT,
        "run_id": "run-a",
        "manifest_path": str(manifest_path),
        "manifest_sha256": audit.sha256_path(manifest_path),
        "serial": SERIAL,
        "uri": URI,
        "profile_file_sha256": PROFILE_SHA,
        "profile_contract_sha256": "3" * 64,
        "receiver_gain_db": 2,
        "gain_qualification": GAIN_QUALIFICATION,
        "firmware_evidence": _firmware_evidence(),
        "pluto_plus_utils_source_attestation": DEPENDENCY_ATTESTATION,
        "pluto_plus_utils_source_attestation_sha256": DEPENDENCY_SHA,
        "array_geometry": {
            "element_count": 6,
            "diameter_mm": 51.0,
            "order": [f"ANT{index}" for index in range(1, 7)],
            "direction": "clockwise",
            "forward_reference": "ANT1",
            "clockwise_bearings_from_forward_deg": [0, 60, 120, 180, 240, 300],
            "source": "TX1 at nominal array center",
        },
        **scientific_payload,
    }
    calibration_path = tmp_path / audit.CALIBRATION_FILENAME
    monkeypatch.setattr(
        audit,
        "load_hexcal_profile",
        lambda _path: SimpleNamespace(
            file_sha256=PROFILE_SHA,
            contract_sha256="3" * 64,
        ),
    )
    monkeypatch.setattr(
        audit,
        "load_hexcal_firmware_evidence",
        lambda *args, **kwargs: SimpleNamespace(
            file_sha256="c" * 64,
            as_dict=lambda: dict(_firmware_evidence()),
        ),
    )
    monkeypatch.setattr(
        audit,
        "attest_pluto_plus_utils_source",
        lambda: dict(DEPENDENCY_ATTESTATION),
    )
    monkeypatch.setattr(
        audit,
        "attest_source_files_at_commit",
        lambda *args, **kwargs: dict(QUALIFICATION_SOURCE_ATTESTATION),
    )
    monkeypatch.setattr(
        audit,
        "load_hexcal_gain_qualification",
        replay_qualification,
    )
    monkeypatch.setattr(
        audit,
        "_audit_artifact",
        lambda *args, **kwargs: {"passed": True, "artifact_id": ARTIFACT_ID},
    )
    monkeypatch.setattr(
        audit,
        "_load_aggregation_module",
        lambda: SimpleNamespace(
            build_calibration_scientific_payload=lambda _manifest: scientific_payload
        ),
    )

    rejected = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert rejected["passed"] is False
    assert any("resume recovery mute" in issue for issue in rejected["issues"])
    assert "required calibration artifact is missing" in rejected["issues"]

    failed["recovery_mute"] = recovery
    manifest["recovery_mute_attempts"] = [recovery]
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    accepted = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert accepted["passed"] is True
    assert accepted["gain_qualification_audit"]["passed"] is True
    assert accepted["gain_qualification_audit"]["source_attestation"] == (
        QUALIFICATION_SOURCE_ATTESTATION
    )
    assert accepted["calibration_artifact"]["scientific_payload_replayed"] is True
    assert accepted["calibration_artifact"]["scientific_mismatches"] == []
    qualification_path, qualification_kwargs = qualification_calls[-1]
    assert qualification_path == Path(GAIN_QUALIFICATION["path"])
    assert qualification_kwargs["expected_board_id"] == BOARD_ID
    assert qualification_kwargs["expected_serial"] == SERIAL
    assert qualification_kwargs["expected_uri"] == URI
    assert qualification_kwargs["expected_source_commit"] == SOURCE_COMMIT
    assert qualification_kwargs["expected_source_attestation"] == (QUALIFICATION_SOURCE_ATTESTATION)
    assert qualification_kwargs["expected_firmware_evidence_sha256"] == "c" * 64
    assert (
        qualification_kwargs["expected_pluto_plus_utils_source_attestation_sha256"]
        == DEPENDENCY_SHA
    )
    assert qualification_kwargs["expected_center_frequencies_hz"] == [2_440_000_000]
    assert qualification_kwargs["expected_tx_hardware_gain_db"] == -40.0
    assert qualification_kwargs["expected_dds_scale"] == 0.125

    tampered_calibration = dict(calibration)
    tampered_calibration["frequency_results"] = [
        {"center_frequency_hz": 2_440_000_000, "arbitrary_phase_deg": 91.0}
    ]
    calibration_path.write_text(json.dumps(tampered_calibration), encoding="utf-8")
    tampered = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert tampered["passed"] is False
    assert any(
        "calibration scientific payload differs from deterministic replay" in issue
        for issue in tampered["issues"]
    )
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")

    manifest["plan"][0]["gain_qualification_sha256"] = "0" * 64
    plan_mismatch = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert plan_mismatch["passed"] is False
    assert any(
        "plan condition 0 differs from the fixed RX-gain qualification" in issue
        for issue in plan_mismatch["issues"]
    )
    manifest["plan"][0]["gain_qualification_sha256"] = "7" * 64

    manifest["configuration"]["receiver_gain_db"] = 3
    gain_mismatch = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert gain_mismatch["passed"] is False
    assert any(
        "calibration receiver gain differs from the qualified selected gain" in issue
        for issue in gain_mismatch["issues"]
    )
    manifest["configuration"]["receiver_gain_db"] = 2

    failed["failure_kind"] = "execution"
    failed["outcome"] = "execution_failed"
    failed["post_mute"] = None
    failed["recovered_stale_process"] = True
    stale_recovered = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert stale_recovered["passed"] is True

    manifest["recovery_mute_attempts"] = []
    stale_missing_ledger = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert stale_missing_ledger["passed"] is False
    assert (
        "resume recovery evidence is absent from recovery ledger"
        in (stale_missing_ledger["issues"])
    )

    failed["failure_kind"] = "execution"
    failed["outcome"] = "execution_failed"
    failed["post_mute"] = post_mute
    failed.pop("recovered_stale_process")
    failed.pop("recovery_mute")
    failed["capture"] = {"stderr": "OSError: [Errno 61] No data available"}
    manifest["recovery_mute_attempts"] = []
    missing_quarantine = audit.audit_manifest(
        manifest,
        board_id=BOARD_ID,
        serial=SERIAL,
        uri=URI,
        manifest_path=manifest_path,
    )
    assert missing_quarantine["passed"] is False
    assert (
        "ENODATA attempt lacks persisted .failed quarantine evidence"
        in missing_quarantine["issues"]
    )
