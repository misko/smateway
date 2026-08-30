from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from math import pi
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from smateway import global_ledger
from smateway.file_artifact_admission import FileArtifactAdmissionError
from smateway.hexcal import sha256_path
from smateway.port_pair_matrix import HeadroomPreflight, canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/analyze_5g8_port_pair_matrix.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_port_pair_matrix_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)

HELPER_SCRIPT = ROOT / "tests/test_run_5g8_port_pair_matrix.py"
HELPER_SPEC = importlib.util.spec_from_file_location("port_pair_runner_test_helpers", HELPER_SCRIPT)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
helpers = importlib.util.module_from_spec(HELPER_SPEC)
sys.modules[HELPER_SPEC.name] = helpers
HELPER_SPEC.loader.exec_module(helpers)
runner = helpers.runner


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def _mute(serial: str, purpose: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": purpose,
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": [-80.0, -80.0],
        "dds_scale_readback": [0.0] * 8,
        "error": None,
    }


def _identity(serial: str, uri: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": runner.IDENTITY_PURPOSE,
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": uri,
        "exact_uri_match": True,
        "sysfs_path": "/sys/bus/usb/devices/1-2.3",
        "attestation": runner.IDENTITY_ATTESTATION,
        "scan_mutates_radio_state": False,
        "error": None,
    }


def _current_execution(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_current_analysis_execution_v1",
        "python_executable": str(analyzer._PINNED_PYTHON),
        "python_prefix": str(analyzer._PINNED_PREFIX),
        "loader_search_path_first": str(analyzer._REQUIRED_LIBIIO_DIRECTORY),
        "pluto_plus_utils": contract["source"]["pluto_plus_utils"],
        "native_libiio": contract["source"]["native_libiio"],
    }


def _load_verified(
    plan: Path,
    manifest: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> tuple[Any, dict[str, Any]]:
    contract = json.loads(plan.read_text(encoding="utf-8"))["plan_contract"]
    return analyzer.load_verified_repeat(
        plan,
        manifest,
        current_execution_boundary=lambda: _current_execution(contract),
        ledger_backend=ledger_backend,
    )


def _rebind_all_execution_safety_layers(
    manifest_path: Path,
    *,
    field: str,
    evidence: Mapping[str, Any],
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = manifest["result"]
    record_path = Path(result["condition_record_path"])
    observation_path = Path(result["observation_path"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    record[field] = dict(evidence)
    attempt = manifest["attempts"][0]
    safety_evidence = {
        "attempt_started": {
            "started_at": attempt["started_at"],
            "started_monotonic_ns": attempt["started_monotonic_ns"],
            "started_clock_boot_id": attempt["started_clock_boot_id"],
        },
        "execution_tombstone": record["execution_tombstone"],
        "identity_preflight": record["identity_preflight"],
        "initial_mute": record["initial_mute"],
        "capture_timeline": record["capture_timeline"],
        "final_mute": record["final_mute"],
        "permanent_run_reservation": record["permanent_run_reservation"],
        "irreversible_execution_burn": record["irreversible_execution_burn"],
    }
    digest = canonical_sha256(safety_evidence)
    record["execution_safety_sha256"] = digest
    artifact_digest = canonical_sha256(evidence)
    record[f"{field}_sha256"] = artifact_digest
    _write_json(record_path, record)
    for name in ("preflight", "main"):
        observation[name]["condition_record_sha256"] = sha256_path(record_path)
    observation[field] = dict(evidence)
    observation["execution_safety_sha256"] = digest
    observation[f"{field}_sha256"] = artifact_digest
    _write_json(observation_path, observation)
    result["condition_record_sha256"] = sha256_path(record_path)
    result["observation_sha256"] = sha256_path(observation_path)
    result[field] = dict(evidence)
    result["execution_safety_sha256"] = digest
    result[f"{field}_sha256"] = artifact_digest
    manifest["result"] = result
    manifest["attempts"][0]["result"] = result
    _write_json(manifest_path, manifest)


def _tone_samples(sample_count: int, *, test_index: int, reference_index: int) -> np.ndarray:
    index = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(2j * pi * 100_000.0 * index / 1_000_000.0)
    values = np.empty((2, sample_count), dtype=np.complex64)
    for receiver, raw in (
        (reference_index, 500.0 * carrier),
        (test_index, 80.0 * carrier * np.exp(0.3j)),
    ):
        values[receiver] = np.rint(raw.real).astype(np.float32) + 1j * np.rint(raw.imag).astype(
            np.float32
        )
    return values


def _artifact(
    root: Path,
    *,
    artifact_id: str,
    samples: np.ndarray,
    stream_id: int,
    samples_per_block: int,
    serial: str,
    uri: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_root = root / artifact_id
    artifact_root.mkdir(parents=True)
    sample_count = samples.shape[1]
    components = np.empty((sample_count, 2, 2), dtype="<i2")
    for receiver in range(2):
        components[:, receiver, 0] = np.rint(samples[receiver].real).astype("<i2")
        components[:, receiver, 1] = np.rint(samples[receiver].imag).astype("<i2")
    raw = artifact_root / f"{artifact_id}.sigmf-data"
    raw.write_bytes(components.tobytes())
    blocks = []
    for sequence, start in enumerate(range(0, sample_count, samples_per_block)):
        duration_ns = samples_per_block * 1_000
        realtime_start = 1_000_000_000 + sequence * duration_ns
        monotonic_start = 10_000_000_000 + sequence * duration_ns
        blocks.append(
            {
                "sample_start": start,
                "sample_count": samples_per_block,
                "utc_ns": 1_000_000 + sequence,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "buffer_sequence": sequence,
                "first_sample_sequence": 5_000 + start,
                "last_sample_sequence_exclusive": 5_000 + start + samples_per_block,
                "metadata_flags": 2_982_931,
                "missing_samples_before": 0,
                "sample_time_realtime_start_ns": realtime_start,
                "sample_time_realtime_end_ns": realtime_start + duration_ns,
                "sample_time_monotonic_start_ns": monotonic_start,
                "sample_time_monotonic_end_ns": monotonic_start + duration_ns,
                "sample_time_uncertainty_ns": 1,
            }
        )
    ledger = {
        "schema_version": 1,
        "metadata_abi": 2,
        "stream_id": stream_id,
        "block_count": len(blocks),
        "total_samples": sample_count,
        "first_sample_sequence": 5_000,
        "last_sample_sequence_exclusive": 5_000 + sample_count,
        "sample_sequence_span": sample_count,
        "blocks": blocks,
    }
    metadata_document = {
        "global": {
            "core:datatype": "ci16_le",
            "core:num_channels": 2,
            "core:sample_rate": 1_000_000.0,
            "pluto:artifact_id": artifact_id,
            "pluto:radio": {"serial": serial, "uri": uri},
        },
        "captures": [
            {
                "sample_start": 0,
                "settings": {
                    "bandwidth_hz": 800_000.0,
                    "center_frequency_hz": 5_800_000_000.0,
                    "channels": [0, 1],
                    "gain_db": 40.0,
                    "gain_mode": "manual",
                    "sample_rate_hz": 1_000_000.0,
                },
            }
        ],
        "pluto:capture": {"sample_count": sample_count},
        "pluto:continuity": ledger,
    }
    metadata = artifact_root / f"{artifact_id}.sigmf-meta"
    _write_json(metadata, metadata_document)
    evidence = {
        "artifact_id": artifact_id,
        "path": str(artifact_root),
        "raw_iq_path": str(raw),
        "raw_iq_sha256": sha256_path(raw),
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
    }
    return evidence, ledger


def _accepted_condition(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, Any], global_ledger.LocalLedgerBackend]:
    fixture_document = helpers._fixture()
    fixture_path = tmp_path / "fixture.json"
    _write_json(fixture_path, fixture_document)
    calibration_document = helpers._calibration(canonical_sha256(fixture_document))
    calibration_path = tmp_path / "calibration.json"
    _write_json(calibration_path, calibration_document)
    source_repository = tmp_path / "smateway-source"
    dependency_repository = tmp_path / "pluto-plus-utils-source"
    dependency_path = dependency_repository / "src" / "capture.py"
    dependency_path.parent.mkdir(parents=True)
    dependency_path.write_text("CAPTURE = 1\n", encoding="utf-8")
    source_files: list[dict[str, Any]] = []
    for relative in runner.SOURCE_FILES:
        source_path = source_repository / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# {relative}\n", encoding="utf-8")
        source_files.append(
            {
                "path": relative,
                "sha256": sha256_path(source_path),
                "size_bytes": source_path.stat().st_size,
            }
        )
    dependency_files: list[dict[str, Any]] = [
        {
            "path": "src/capture.py",
            "sha256": sha256_path(dependency_path),
            "size_bytes": dependency_path.stat().st_size,
        }
    ]
    contract = runner._build_plan_contract(
        run_id="matrix-run-a",
        campaign_id="matrix-campaign",
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        cell_id="TX1_RX1",
        repeat_index=1,
        fixture_document=fixture_document,
        fixture_file={
            "path": str(fixture_path),
            "sha256": sha256_path(fixture_path),
            "size_bytes": fixture_path.stat().st_size,
        },
        calibration_document=calibration_document,
        calibration_file={
            "path": str(calibration_path),
            "sha256": sha256_path(calibration_path),
            "size_bytes": calibration_path.stat().st_size,
        },
        source_attestation={
            "repository": str(source_repository),
            "commit": "a" * 40,
            "files": source_files,
            "source_files_sha256": canonical_sha256(source_files),
        },
        dependency_attestation={
            "repository_path": str(dependency_repository),
            "commit": "b" * 40,
            "files": dependency_files,
        },
        native_attestation=helpers._native_attestation(),
        state_root=tmp_path / "state",
    )
    run_root = Path(contract["storage"]["condition_root"])
    plan_path = run_root / "plan.json"
    manifest_path = run_root / "manifest.json"
    storage = global_ledger.provision_local_test_storage(tmp_path / "ledger-authority")
    ledger_backend = global_ledger.LocalLedgerBackend(storage=storage)
    runner._prepare_plan(
        plan_path,
        manifest_path,
        contract,
        ledger_backend=ledger_backend,
    )
    reservation = runner._validate_reservation_receipt(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_backend=ledger_backend,
    )
    attempt_started = runner._stamp_fields(runner._clock_stamp(), "started")
    burn = runner._acquire_execution_burn(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation_receipt=reservation,
        attempt_started=attempt_started,
        ledger_backend=ledger_backend,
    )
    execution_path = run_root / "execution-started.tombstone.json"
    runner._execution_tombstone(
        execution_path,
        contract,
        plan_path,
        reservation_receipt=reservation,
        burn_receipt=burn,
        attempt_started=attempt_started,
    )
    execution_receipt = runner._validate_execution_tombstone_receipt(
        contract,
        plan_path=plan_path,
        reservation_receipt=reservation,
        burn_receipt=burn,
        attempt_started=attempt_started,
    )
    cell = contract["condition"]
    test_index = 0 if cell["test_receiver"] == "RX1" else 1
    reference_index = 0 if cell["reference_receiver"] == "RX1" else 1
    preflight_samples = _tone_samples(
        100_000, test_index=test_index, reference_index=reference_index
    )
    main_samples = _tone_samples(300_000, test_index=test_index, reference_index=reference_index)
    capture_root = Path(contract["storage"]["capture_root"])
    preflight_evidence, preflight_ledger = _artifact(
        capture_root / "preflight",
        artifact_id="preflight-artifact",
        samples=preflight_samples,
        stream_id=101,
        samples_per_block=100_000,
        serial="serial-a",
        uri="usb:1.2.3",
    )
    main_evidence, main_ledger = _artifact(
        capture_root / "main",
        artifact_id="main-artifact",
        samples=main_samples,
        stream_id=102,
        samples_per_block=100_000,
        serial="serial-a",
        uri="usb:1.2.3",
    )
    peaks, clipped = runner._headroom([SimpleNamespace(samples=preflight_samples)])
    headroom = HeadroomPreflight(
        preflight_tx_gain_db=-40.0,
        capture_tx_gain_db=-20.0,
        clip_threshold_abs_counts=2_047.0,
        peak_abs_counts_by_receiver=peaks,
        clipped_sample_count_by_receiver=clipped,
    )
    main_blocks = [
        SimpleNamespace(samples=main_samples[:, start : start + 100_000])
        for start in range(0, 300_000, 100_000)
    ]
    main_analysis = runner._main_analysis(main_blocks, contract)
    scales = [0.0] * 8
    scales[0] = scales[2] = 0.125
    main_readback = {
        "tx_gain_readback_db_by_channel": [-20.0, -80.0],
        "dds_scale_readback": scales,
    }
    identity_preflight = runner._call_identity(
        _identity,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
    )
    initial_mute = runner._call_mute(
        _mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
        purpose="pre_preflight_exact_mute",
    )
    preflight_started = runner._clock_stamp()
    preflight_completed = runner._clock_stamp()
    post_preflight_mute = runner._call_mute(
        _mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
        purpose="post_preflight_exact_mute",
    )
    main_started = runner._clock_stamp()
    main_completed = runner._clock_stamp()
    post_main_mute = runner._call_mute(
        _mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
        purpose="post_main_exact_mute",
    )
    final_mute = runner._call_mute(
        _mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
        purpose="final_acceptance_exact_mute",
    )
    capture_timeline = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_capture_mute_timeline_v1",
        "preflight_capture": runner._capture_timing(
            purpose="preflight_capture",
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=execution_receipt,
            started=preflight_started,
            completed=preflight_completed,
        ),
        "post_preflight_mute": post_preflight_mute,
        "main_capture": runner._capture_timing(
            purpose="main_capture",
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=execution_receipt,
            started=main_started,
            completed=main_completed,
        ),
        "post_main_mute": post_main_mute,
    }
    execution_safety = runner._validated_execution_safety(
        identity=identity_preflight,
        initial_mute=initial_mute,
        final_mute=final_mute,
        capture_timeline=capture_timeline,
        attempt_started=attempt_started,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=execution_receipt,
    )
    record_path = run_root / "condition-record.json"
    record = {
        "schema": 1,
        "record_kind": "5g8_protected_port_pair_condition_record",
        "campaign_plan_sha256": contract["campaign_plan"]["sha256"],
        "plan_contract_sha256": canonical_sha256(contract),
        "condition": contract["condition"],
        "fixture": contract["fixture"],
        "calibration": contract["calibration"],
        "identity_preflight": identity_preflight,
        "initial_mute": initial_mute,
        "capture_timeline": capture_timeline,
        "execution_tombstone": execution_receipt,
        "execution_safety_sha256": execution_safety["evidence_sha256"],
        "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
        "initial_mute_sha256": execution_safety["initial_mute_sha256"],
        "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
        "final_mute_sha256": execution_safety["final_mute_sha256"],
        "execution_tombstone_receipt_sha256": execution_safety[
            "execution_tombstone_receipt_sha256"
        ],
        "permanent_run_reservation": reservation,
        "irreversible_execution_burn": burn,
        "headroom_preflight": {
            "input": asdict(headroom),
            "admission": asdict(analyzer.evaluate_headroom_preflight(headroom)),
        },
        "preflight": {
            "evidence": preflight_evidence,
            "stream_id": 101,
            "continuity_ledger": preflight_ledger,
        },
        "main": {
            "evidence": main_evidence,
            "stream_id": 102,
            "continuity_ledger": main_ledger,
            "rf_readback": main_readback,
            "analysis": main_analysis,
        },
        "final_mute": final_mute,
    }
    _write_json(record_path, record)
    physical = {
        "inactive_tx_termination_sha256": contract["fixture"]["inactive_tx_termination_sha256"],
        "test_receiver_termination_sha256": contract["fixture"]["test_receiver_termination_sha256"],
        "reference_chain_sha256": contract["fixture"]["reference_chain_sha256"],
        "rx1_protection_sha256": contract["fixture"]["rx1_protection_sha256"],
    }
    observation = {
        "schema": 1,
        "observation_kind": "5g8_port_pair_normalized_observation",
        "campaign_id": contract["campaign_id"],
        "run_id": contract["run_id"],
        "cell_id": cell["cell_id"],
        "repeat_index": 1,
        "campaign_plan_sha256": contract["campaign_plan"]["sha256"],
        "plan_contract_sha256": canonical_sha256(contract),
        "fixture_sha256": contract["fixture"]["identity_sha256"],
        "calibration_sha256": contract["calibration"]["identity_sha256"],
        "topology_sha256": cell["topology_sha256"],
        "source_commit": "a" * 40,
        "dependency_commit": "b" * 40,
        "native_attestation_sha256": contract["source"]["native_libiio_sha256"],
        "preflight": {
            "stream_id": 101,
            "artifact": preflight_evidence,
            "condition_record_sha256": sha256_path(record_path),
            "headroom": record["headroom_preflight"],
            "continuity_passed": True,
        },
        "main": {
            "stream_id": 102,
            "artifact": main_evidence,
            "condition_record_sha256": sha256_path(record_path),
            "rf_readback": main_readback,
            "clipped_sample_count_by_receiver": [0, 0],
            "analysis": main_analysis,
            "continuity_passed": True,
        },
        "physical_safety": physical,
        "identity_preflight": identity_preflight,
        "initial_mute": initial_mute,
        "capture_timeline": capture_timeline,
        "execution_tombstone": execution_receipt,
        "final_mute": final_mute,
        "execution_safety_sha256": execution_safety["evidence_sha256"],
        "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
        "initial_mute_sha256": execution_safety["initial_mute_sha256"],
        "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
        "final_mute_sha256": execution_safety["final_mute_sha256"],
        "execution_tombstone_receipt_sha256": execution_safety[
            "execution_tombstone_receipt_sha256"
        ],
        "permanent_run_reservation": reservation,
        "irreversible_execution_burn": burn,
        "quality_passed": True,
        "raw_channel_amplitudes_comparable": False,
    }
    observation_path = run_root / "normalized-observation.json"
    _write_json(observation_path, observation)
    result = {
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "observation_path": str(observation_path),
        "observation_sha256": sha256_path(observation_path),
        "preflight_artifact": preflight_evidence,
        "main_artifact": main_evidence,
        "accepted_stream_count": 2,
        "identity_preflight": identity_preflight,
        "initial_mute": initial_mute,
        "capture_timeline": capture_timeline,
        "execution_tombstone": execution_receipt,
        "final_mute": final_mute,
        "execution_safety_sha256": execution_safety["evidence_sha256"],
        "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
        "initial_mute_sha256": execution_safety["initial_mute_sha256"],
        "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
        "final_mute_sha256": execution_safety["final_mute_sha256"],
        "execution_tombstone_receipt_sha256": execution_safety[
            "execution_tombstone_receipt_sha256"
        ],
        "permanent_run_reservation": reservation,
        "irreversible_execution_burn": burn,
    }
    confirmations = {
        "topology_token": cell["topology_token"],
        "no_antennas": True,
        "inactive_tx_physically_terminated": True,
        "test_receiver_terminated": True,
        "rx1_protection_unchanged": True,
        "separate_reference_attenuator": True,
        "reference_planes_match": True,
        "no_movement": True,
    }
    attempt = {
        **attempt_started,
        **runner._stamp_fields(runner._clock_stamp(), "completed"),
        "status": "complete",
        "error": None,
        "confirmations": confirmations,
        "permanent_run_reservation": reservation,
        "irreversible_execution_burn": burn,
        "execution_tombstone": execution_receipt,
        "result": result,
    }
    manifest = {
        "schema": 1,
        "run_kind": runner.RUN_KIND,
        "run_id": contract["run_id"],
        "campaign_id": contract["campaign_id"],
        "cell_id": cell["cell_id"],
        "repeat_index": 1,
        "status": "complete",
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_path(plan_path),
            "contract_sha256": canonical_sha256(contract),
        },
        "attempts": [attempt],
        "result": result,
        "error": None,
        "accepted_stream_count": 2,
    }
    _write_json(manifest_path, manifest)
    return plan_path, manifest_path, contract, ledger_backend


def test_one_condition_is_recursively_read_and_recomputed_from_raw_iq(tmp_path: Path) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)

    repeat, evidence = _load_verified(plan, manifest, ledger_backend)

    assert repeat.cell_id == "TX1_RX1"
    assert repeat.repeat_index == 1
    assert repeat.preflight_capture.stream_id == "101"
    assert repeat.main_capture.stream_id == "102"
    assert repeat.test_receiver_tone.detected
    assert evidence["run_id"] == "matrix-run-a"


@pytest.mark.parametrize("target", ("raw", "metadata", "record"))
def test_raw_metadata_or_condition_record_tamper_is_rejected(tmp_path: Path, target: str) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    result = document["result"]
    if target == "raw":
        path = Path(result["main_artifact"]["raw_iq_path"])
        path.write_bytes(path.read_bytes() + b"\0\0")
    elif target == "metadata":
        path = Path(result["main_artifact"]["metadata_path"])
        path.write_text("{}", encoding="utf-8")
    else:
        path = Path(result["condition_record_path"])
        path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        (analyzer.PortPairAnalysisError, ValueError),
        match="SHA-256|hash|record does not bind",
    ):
        _load_verified(plan, manifest, ledger_backend)


def test_failure_tombstone_and_fabricated_observation_are_rejected(tmp_path: Path) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    (manifest.parent / "failed-run.tombstone.json").write_text("{}", encoding="utf-8")
    with pytest.raises(analyzer.PortPairAnalysisError, match="failure tombstone"):
        _load_verified(plan, manifest, ledger_backend)

    (manifest.parent / "failed-run.tombstone.json").unlink()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    observation = Path(document["result"]["observation_path"])
    fabricated = json.loads(observation.read_text(encoding="utf-8"))
    fabricated["main"]["analysis"]["reference_tone_snr_db"] = 999.0
    _write_json(observation, fabricated)
    document["result"]["observation_sha256"] = sha256_path(observation)
    document["attempts"][0]["result"] = document["result"]
    _write_json(manifest, document)
    with pytest.raises(analyzer.PortPairAnalysisError, match="analysis differs"):
        _load_verified(plan, manifest, ledger_backend)


def test_source_bytes_and_normalized_source_identity_are_reverified(tmp_path: Path) -> None:
    plan, manifest, contract, ledger_backend = _accepted_condition(tmp_path)
    source_repository = Path(contract["source"]["smateway"]["repository"])
    (source_repository / runner.SOURCE_FILES[0]).write_text("# changed\n", encoding="utf-8")
    with pytest.raises(FileArtifactAdmissionError, match="SHA-256"):
        _load_verified(plan, manifest, ledger_backend)

    plan, manifest, _, identity_backend = _accepted_condition(tmp_path / "identity")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    observation = Path(document["result"]["observation_path"])
    fabricated = json.loads(observation.read_text(encoding="utf-8"))
    fabricated["source_commit"] = "c" * 40
    _write_json(observation, fabricated)
    document["result"]["observation_sha256"] = sha256_path(observation)
    document["attempts"][0]["result"] = document["result"]
    _write_json(manifest, document)
    with pytest.raises(analyzer.PortPairAnalysisError, match="identity differs"):
        _load_verified(plan, manifest, identity_backend)


def test_twenty_condition_aggregator_rejects_duplicate_paths_before_loading(
    tmp_path: Path,
) -> None:
    paths = tuple((tmp_path / f"p-{index}", tmp_path / f"m-{index}") for index in range(20))
    duplicated = (*paths[:-1], paths[0])
    with pytest.raises(analyzer.PortPairAnalysisError, match="reuses"):
        analyzer.analyze_conditions(
            duplicated,
            bootstrap_draw_count=256,
            bootstrap_seed=1,
        )


def test_import_and_parser_have_no_hardware_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "_live_capture",
        lambda *_args, **_kwargs: pytest.fail("analysis invoked RF capture"),
    )
    options = {option for action in analyzer._parser()._actions for option in action.option_strings}
    assert {"--condition", "--output"} <= options


@pytest.mark.parametrize("drift", ("ambient-interpreter", "module-origin", "native-libiio"))
def test_current_analysis_execution_drift_is_rejected(tmp_path: Path, drift: str) -> None:
    plan, _, contract, _ledger_backend = _accepted_condition(tmp_path)
    current = copy.deepcopy(_current_execution(contract))
    if drift == "ambient-interpreter":
        current["python_executable"] = "/usr/bin/python3"
        match = "pinned pluto-plus-utils Python"
    elif drift == "module-origin":
        current["pluto_plus_utils"]["repository_path"] = "/tmp/ambient-wheel"
        match = "source/import origins"
    else:
        current["native_libiio"]["library_sha256"] = "f" * 64
        match = "native libiio"

    frozen = json.loads(plan.read_text(encoding="utf-8"))["plan_contract"]
    with pytest.raises(analyzer.PortPairAnalysisError, match=match):
        analyzer._verify_current_execution(frozen, current=current)


@pytest.mark.parametrize("field", ("identity_preflight", "initial_mute", "final_mute"))
def test_fully_rehashed_rebound_execution_safety_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    original = copy.deepcopy(document["result"][field])
    if field == "identity_preflight":
        original["serial"] = "other-pluto"
    elif field == "initial_mute":
        original["purpose"] = "another_run_initial_mute"
    else:
        original["serial"] = "other-pluto"
    _rebind_all_execution_safety_layers(manifest, field=field, evidence=original)

    with pytest.raises(
        analyzer.PortPairAnalysisError,
        match="complete execution timeline is invalid|execution safety evidence failed",
    ):
        _load_verified(plan, manifest, ledger_backend)


def test_observation_only_safety_rebinding_is_rejected(tmp_path: Path) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    result = document["result"]
    observation_path = Path(result["observation_path"])
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    rebound = copy.deepcopy(observation["initial_mute"])
    rebound["serial"] = "other-pluto"
    observation["initial_mute"] = rebound
    _write_json(observation_path, observation)
    result["observation_sha256"] = sha256_path(observation_path)
    document["result"] = result
    document["attempts"][0]["result"] = result
    _write_json(manifest, document)

    with pytest.raises(analyzer.PortPairAnalysisError, match="recursively cross-bound"):
        _load_verified(plan, manifest, ledger_backend)


def test_result_record_and_observation_paths_cannot_be_rebound_outside_run_root(
    tmp_path: Path,
) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    result = document["result"]
    original = Path(result["observation_path"])
    rebound = tmp_path / "rebound-observation.json"
    rebound.write_bytes(original.read_bytes())
    result["observation_path"] = str(rebound)
    result["observation_sha256"] = sha256_path(rebound)
    document["result"] = result
    document["attempts"][0]["result"] = result
    _write_json(manifest, document)

    with pytest.raises(analyzer.PortPairAnalysisError, match="escape the exact condition root"):
        _load_verified(plan, manifest, ledger_backend)


@pytest.mark.parametrize("clock_axis", ("utc", "monotonic"))
def test_analyzer_rejects_fully_rehashed_identity_before_execution_marker(
    tmp_path: Path, clock_axis: str
) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    identity = copy.deepcopy(document["result"]["identity_preflight"])
    marker = document["result"]["execution_tombstone"]["document"]
    if clock_axis == "utc":
        identity["started_at"] = "2000-01-01T00:00:00+00:00"
    else:
        identity["started_monotonic_ns"] = marker["created_monotonic_ns"] - 1
    _rebind_all_execution_safety_layers(
        manifest,
        field="identity_preflight",
        evidence=identity,
    )

    with pytest.raises(analyzer.PortPairAnalysisError, match="complete execution timeline"):
        _load_verified(plan, manifest, ledger_backend)


@pytest.mark.parametrize("clock_axis", ("utc", "monotonic"))
def test_analyzer_rejects_attempt_completion_before_final_mute(
    tmp_path: Path, clock_axis: str
) -> None:
    plan, manifest, _, ledger_backend = _accepted_condition(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    attempt = document["attempts"][0]
    final_mute = document["result"]["final_mute"]
    if clock_axis == "utc":
        attempt["completed_at"] = "2000-01-01T00:00:00+00:00"
    else:
        attempt["completed_monotonic_ns"] = final_mute["completed_monotonic_ns"] - 1
    _write_json(manifest, document)

    with pytest.raises(analyzer.PortPairAnalysisError, match="complete execution timeline"):
        _load_verified(plan, manifest, ledger_backend)
