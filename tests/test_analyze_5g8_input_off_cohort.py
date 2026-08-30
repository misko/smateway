from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from smateway.hexcal import sha256_path
from smateway.input_off_control import (
    OBSERVATION_KIND,
    acquisition_contract,
    canonical_sha256,
)
from smateway.p0_normalized_evidence import reconstruct_legacy_closed_loop_plan

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_input_off_cohort.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_input_off_cohort_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)

P0_BINDINGS = [
    {
        "path": f"/evidence/p0-{index}.json",
        "sha256": f"{index:064x}",
        "size_bytes": 100 + index,
        "run_id": f"p0-{index}",
    }
    for index in range(1, 6)
]


def test_cli_does_not_expose_test_only_legacy_root_injection() -> None:
    help_text = analyzer._parser().format_help()
    assert "test-only" not in help_text
    assert "legacy-boards-root" not in help_text
    assert "runner-home" not in help_text


def _p2_observation() -> dict[str, Any]:
    return {
        "schema": 1,
        "observation_kind": OBSERVATION_KIND,
        "cohort": "P2",
        "run_id": "p2-run-a",
        "artifact": {
            "artifact_id": "artifact-a",
            "stream_id": 123,
            "sha256": "1" * 64,
        },
        "acquisition": acquisition_contract(),
        "profile_contract_sha256": "2" * 64,
        "analysis": {
            "transfer_detected": True,
            "all_off_transfer": {"real": 0.01, "imag": 0.002},
            "all_off_transfer_upper_bound": None,
            "rx1_reference_amplitude": 100.0,
            "detected_pilot_snr_db": 35.0,
        },
        "quality": {
            "passed": True,
            "continuity_verified": True,
            "metadata_abi": 2,
            "headroom_passed": True,
            "final_mute_passed": True,
            "fast20_schedule_verified": True,
            "central_all_off_windows_used": True,
        },
        "provenance": {
            "source_commit": "3" * 40,
            "source_files_sha256": canonical_sha256([]),
            "native_attestation_sha256": "5" * 64,
            "fixture_evidence_sha256": "6" * 64,
            "fixture_fixed_graph_sha256": "7" * 64,
            "comparable_fixture_group_id": "group-a",
        },
    }


def _provision_p2_global_ledger(tmp_path: Path) -> tuple[Any, Any]:
    runner = analyzer._p2_runner()
    storage = analyzer.global_ledger.provision_local_test_storage(
        tmp_path.parent / f"{tmp_path.name}-analyzer-shared-ledger"
    )
    backend = analyzer.global_ledger.LocalLedgerBackend(storage=storage)
    return runner, backend


def _accepted_files(tmp_path: Path) -> tuple[Path, Path, Any]:
    runner, ledger_backend = _provision_p2_global_ledger(tmp_path)
    observation = _p2_observation()
    source_repository = tmp_path / "smateway-source"
    dependency_repository = tmp_path / "pluto-plus-utils-source"
    dependency_path = dependency_repository / "src" / "capture.py"
    dependency_path.parent.mkdir(parents=True)
    dependency_path.write_text("CAPTURE = 1\n", encoding="utf-8")
    source_files = []
    for relative in analyzer._p2_runner().SOURCE_FILES:
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
    dependency_files = [
        {
            "path": "src/capture.py",
            "sha256": sha256_path(dependency_path),
            "size_bytes": dependency_path.stat().st_size,
        }
    ]
    observation["provenance"]["source_files_sha256"] = canonical_sha256(source_files)
    run_root = tmp_path / "run"
    capture_root = tmp_path / "captures" / "p2-run-a"
    capture_fd, capture_binding = runner._create_bound_capture_root(
        capture_root, expected_device=os.stat("/home/pi").st_dev
    )
    try:
        os.mkdir("artifact-a", mode=0o700, dir_fd=capture_fd)
    finally:
        os.close(capture_fd)
    artifact_root = capture_root / "artifact-a"
    observation_path = artifact_root / "5g8-input-off-observation.json"
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    manifest_path = run_root / "manifest.json"
    manifest_path.parent.mkdir()
    data_path = artifact_root / "artifact-a.sigmf-data"
    metadata_path = artifact_root / "artifact-a.sigmf-meta"
    data_path.write_bytes(b"synthetic IQ")
    metadata_path.write_bytes(b"synthetic metadata")
    artifact_evidence = {
        "artifact_id": "artifact-a",
        "path": str(artifact_root),
        "data_path": str(data_path),
        "data_sha256": sha256_path(data_path),
        "data_size_bytes": data_path.stat().st_size,
        "metadata_path": str(metadata_path),
        "metadata_sha256": sha256_path(metadata_path),
        "metadata_size_bytes": metadata_path.stat().st_size,
    }
    observation["artifact"]["sha256"] = artifact_evidence["data_sha256"]
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    native = {"synthetic": "native"}
    contract: dict[str, Any] = {
        "schema": 1,
        "board_id": "board-a",
        "run_id": "p2-run-a",
        "run_kind": "5g8_input_drive_off_fast20_one_stream",
        "acquisition": acquisition_contract(),
        "profile": {"contract_sha256": "2" * 64},
        "configuration": {"serial": "serial-a", "uri": "usb:1.2.3"},
        "source": {
            "smateway": {
                "repository": str(source_repository),
                "commit": "3" * 40,
                "files": source_files,
                "source_files_sha256": canonical_sha256(source_files),
            },
            "pluto_plus_utils": {
                "repository_path": str(dependency_repository),
                "files": dependency_files,
            },
            "native_libiio": native,
            "native_libiio_sha256": "5" * 64,
        },
        "fixture_evidence_sha256": "6" * 64,
        "fixture_evidence": {
            "fixture": {
                "fixed_graph_sha256": "7" * 64,
                "comparable_fixture_group_id": "group-a",
            }
        },
        "p0_baseline_bindings": P0_BINDINGS,
        "storage": {
            "state_root": str(tmp_path),
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
            "local_storage_device": os.stat("/home/pi").st_dev,
            "run_root": str(run_root),
            "capture_root": str(capture_root),
        },
        "execution": {},
    }
    contract["execution"]["global_run_ledger_authority"] = (
        runner._shared_new_global_ledger_authority(
            board_id="board-a",
            run_id="p2-run-a",
            run_root=run_root,
            capture_root=capture_root,
            state_root=tmp_path,
            ledger_backend=ledger_backend,
        )
    )
    contract_sha256 = canonical_sha256(contract)
    plan_path = manifest_path.parent / "plan.json"
    plan = {
        "schema": 1,
        "immutable": True,
        "plan_contract": contract,
        "plan_contract_sha256": contract_sha256,
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    plan_path.chmod(0o400)
    reservation_binding = runner._new_global_reservation_binding(
        contract, plan_path=plan_path, ledger_backend=ledger_backend
    )
    global_reservation = runner._seal_global_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        binding=reservation_binding,
        ledger_backend=ledger_backend,
    )
    burn_progress: dict[str, Any] = {}
    global_burn = runner._burn_global_run(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation=global_reservation,
        progress=burn_progress,
        ledger_backend=ledger_backend,
    )
    condition_path = artifact_root / "5g8-input-off-condition.json"
    condition = {
        "normalized_observation": observation,
        "artifact_evidence": artifact_evidence,
        "immutable_plan_contract_sha256": contract_sha256,
        "global_execution_burn": global_burn,
        "capture_root_binding": capture_binding,
        "safety": {"persistence_began_only_after_final_mute_passed": True},
    }
    condition_path.write_text(json.dumps(condition), encoding="utf-8")
    execution_path = manifest_path.parent / "execution-started.tombstone.json"
    execution_document = runner._execution_tombstone(
        execution_path,
        contract=contract,
        plan_path=plan_path,
        global_execution_burn=global_burn,
    )
    capture_fd, _ = runner._validate_capture_root_binding(capture_binding)
    try:
        artifact_identity = runner._validate_accepted_capture_inventory(
            capture_fd, artifact_id="artifact-a"
        )
    finally:
        os.close(capture_fd)
    result = {
        "observation_path": str(observation_path),
        "observation_sha256": sha256_path(observation_path),
        "condition_record_path": str(condition_path),
        "condition_record_sha256": sha256_path(condition_path),
        "stream_id": 123,
        "artifact": {"artifact_id": "artifact-a", "path": str(artifact_root)},
        "artifact_id": "artifact-a",
        "artifact_evidence": artifact_evidence,
        "capture_root_binding": capture_binding,
        "artifact_directory_identity": artifact_identity,
        "global_execution_burn": global_burn,
        "final_mute": {
            "status": "passed",
            "purpose": "final_acceptance_exact_mute",
            "error": None,
        },
        "native_runtime_preflight": native,
        "identity_preflight": {
            "status": "passed",
            "serial": "serial-a",
            "resolved_uri": "usb:1.2.3",
            "exact_uri_match": True,
        },
    }
    manifest = {
        "schema": 1,
        "run_kind": "5g8_input_drive_off_fast20_one_stream",
        "run_id": "p2-run-a",
        "status": "complete",
        "accepted_stream_count": 1,
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_path(plan_path),
            "contract_sha256": contract_sha256,
        },
        "global_run_reservation": global_reservation,
        "global_execution_burn": global_burn,
        "attempts": [
            {
                "status": "complete",
                "global_execution_burn": global_burn,
                "result": result,
                "execution_tombstone": {
                    "path": str(execution_path),
                    "sha256": sha256_path(execution_path),
                    "document": execution_document,
                },
            }
        ],
        "result": result,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifact_fd = runner._open_absolute_directory_nofollow(artifact_root)
    try:
        runner._seal_directory_tree(artifact_fd)
    finally:
        os.close(artifact_fd)
    assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o500
    return observation_path, manifest_path, ledger_backend


def test_p2_cohort_input_requires_complete_one_stream_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    observed = analyzer._accepted_p2_observation(
        observation_path,
        manifest_path,
        expected_p0_bindings=P0_BINDINGS,
        ledger_backend=ledger_backend,
    )
    assert observed["artifact"]["stream_id"] == 123

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["accepted_stream_count"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(analyzer.CohortAnalysisError, match="manifest acceptance"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
        )


def test_p2_failure_tombstone_overrides_apparently_complete_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    (manifest_path.parent / "failed-run.tombstone.json").write_text("{}", encoding="utf-8")
    with pytest.raises(analyzer.CohortAnalysisError, match="failure tombstone"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
        )


def test_p2_analyzer_rejects_manifest_result_global_burn_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged = dict(manifest["result"]["global_execution_burn"])
    forged["burn_completed_before_source_dependency_fixture_or_hardware_access"] = False
    manifest["result"]["global_execution_burn"] = forged
    manifest["attempts"][0]["result"]["global_execution_burn"] = forged
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(analyzer.CohortAnalysisError, match="global burn receipt"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
        )


def test_p2_analyzer_rejects_success_with_external_failure_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failure_slot = Path(
        manifest["global_run_reservation"]["binding"]["failure_receipt_slot"]["path"]
    )
    failure_slot.write_text('{"failed": true}\n', encoding="utf-8")
    failure_slot.chmod(analyzer._p2_runner().GLOBAL_LEDGER_SEALED_FILE_MODE)

    with pytest.raises(analyzer.CohortAnalysisError, match="global reserve/burn authority"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
        )


def test_p2_analyzer_rejects_extra_capture_root_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capture_root = Path(manifest["result"]["capture_root_binding"]["path"])
    (capture_root / "rogue-artifact").mkdir()

    with pytest.raises(analyzer.CohortAnalysisError, match="bound capture inventory"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
        )


def test_p2_plan_must_bind_exact_ordered_caller_p0_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)

    with pytest.raises(analyzer.CohortAnalysisError, match="exact ordered caller cohort"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=list(reversed(P0_BINDINGS)),
            ledger_backend=ledger_backend,
        )


def test_p2_analyzer_rehashes_optional_rx2_attenuator_characterization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def evidence(name: str, content: bytes = b"evidence") -> dict[str, Any]:
        path = tmp_path / name
        path.write_bytes(content)
        return {
            "path": str(path),
            "sha256": sha256_path(path),
            "size_bytes": path.stat().st_size,
        }

    profile = evidence("profile.json")
    fixture_file = evidence("fixture.json")
    setup_file = evidence("setup.json")
    baseline = evidence("baseline.png")
    live = evidence("fast20.json")
    setup_photo = evidence("setup.png")
    component_characterization = evidence("rx2-attenuator.s2p")
    connection_characterization = evidence("rx2-adapter.s2p")

    def characterized(binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "characterized",
            "evidence_path": binding["path"],
            "evidence_sha256": binding["sha256"],
            "s_parameter_sha256": "d" * 64,
            "return_loss_db_at_5g8": 15.0,
        }

    sealed = {"binding_kind": "sealed_fast20_live_image_v1"}
    fixture = {
        "source_files": {
            "fixture_manifest": fixture_file,
            "setup_attestation": setup_file,
        },
        "fixture": {
            "campaign_id": "5p8-debug-r1",
            "board_id": "board-a",
            "baseline_topology_evidence": baseline,
            "fast20_control": {"profile": profile, "live_image_evidence": live},
            "components": {},
            "connections": {},
            "rx2_attenuator": {
                "state": "present",
                "component": {"characterization": characterized(component_characterization)},
                "pluto_connection": {
                    "interconnect": {"characterization": characterized(connection_characterization)}
                },
            },
        },
        "setup_attestation": {"setup_evidence": setup_photo},
        "sealed_fast20_live_image": sealed,
    }
    monkeypatch.setattr(
        analyzer._p2_runner(), "_validate_fast20_live_image", lambda *_args, **_kwargs: sealed
    )

    analyzer._verify_p2_fixture_files(fixture, profile)
    Path(component_characterization["path"]).write_bytes(b"tampered")
    with pytest.raises(analyzer.FileArtifactAdmissionError, match="SHA-256 binding is stale"):
        analyzer._verify_p2_fixture_files(fixture, profile)


def _accepted_legacy_manifest(boards_root: Path) -> tuple[dict[str, Any], str]:
    artifact_id = "c" * 32
    board_id = "board-a"
    board_state_root = boards_root / board_id
    storage_root = board_state_root / "pluto-usb-captures"
    configuration = {
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "frequencies_hz": [5_800_000_000],
        "closure_frequencies_hz": [5_800_000_000],
        "stages": ["rotation0", "rotation1", "rotation2", "closure0"],
        "mappings": {
            "rotation0": {f"F{index}": f"ANT{index}" for index in range(1, 9)},
            "rotation1": {f"F{index}": f"ANT{index % 8 + 1}" for index in range(1, 9)},
            "rotation2": {f"F{index}": f"ANT{(index + 1) % 8 + 1}" for index in range(1, 9)},
            "closure0": {f"F{index}": f"ANT{index}" for index in range(1, 9)},
        },
        "fixture_id": "tx1-2way-rx1-and-8way-board-rx2-v1",
        "fully_conducted_required": True,
        "tx_channel": 0,
        "stimulus": "qualification",
        "receiver_gain_db": 40,
        "sample_rate_hz": 1_000_000,
        "duration_s": 10.0,
        "kernel_buffers": 8,
        "planned_capture_count": 4,
        "estimated_raw_iq_bytes": 320_000_000,
        "profile_id": "fast20-v1",
        "profile_contract_sha256": (
            "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
        ),
        "firmware_binary_sha256": (
            "aeaed9d2f892d2a59add1aba2a7477e349b750c99f81610632286d04d91326ac"
        ),
        "board_id": board_id,
        "serial": "serial-a",
        "uri": "usb:1.2.3",
        "python": "/home/pi/pluto-plus-utils/.venv/bin/python",
        "timeout_s": 180,
        "storage_medium": "raspberry_pi_local_filesystem",
        "board_state_root": str(board_state_root),
        "artifact_storage_root": str(storage_root),
        "pluto_onboard_storage_used": False,
    }
    plan = reconstruct_legacy_closed_loop_plan(
        configuration,
        expected_repository=analyzer.REPOSITORY,
        test_only_legacy_boards_root=boards_root,
    )
    condition = next(
        item
        for item in plan
        if item["stage"] == "rotation0" and item["center_frequency_hz"] == 5_800_000_000
    )
    analysis_path = storage_root / artifact_id / "fast20-reference-transfer-v2.json"
    reanalysis_command = [
        artifact_id if item == "{artifact_id}" else item
        for item in condition["reference_reanalysis_command_template"]
    ]
    manifest = {
        "schema": 1,
        "experiment_kind": configuration["experiment_kind"],
        "run_id": "p0-run-a",
        "status": "awaiting_rotation1",
        "final_mute": {"status": "passed", "purpose": "final_rotation0", "error": None},
        "configuration": configuration,
        "plan": plan,
        "attempts": [
            {
                **condition,
                "artifact_id": artifact_id,
                "status": "complete",
                "outcome": "quality_passed",
                "failure_kind": None,
                "error": None,
                "post_mute": {"status": "passed", "purpose": "post_attempt", "error": None},
                "capture": {
                    "status": "complete",
                    "accepted": True,
                    "timed_out": False,
                    "return_code": 0,
                    "command": condition["capture_command"],
                },
                "reanalysis": {
                    "status": "complete",
                    "accepted": True,
                    "timed_out": False,
                    "return_code": 0,
                    "command": reanalysis_command,
                    "parsed_output": {
                        "artifact_id": artifact_id,
                        "quality_passed": True,
                    },
                },
                "quality_result": {
                    "status": "passed",
                    "quality_passed": True,
                    "artifact_id": artifact_id,
                    "analysis_path": str(analysis_path),
                    "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
                    "artifact_path": str(analysis_path.parent),
                    "artifact_sha256": "d" * 64,
                    "tx_channel": 0,
                    "center_frequency_hz": 5_800_000_000,
                    "receiver_gain_db": 40,
                },
                "artifact_identity": {
                    "artifact_id": artifact_id,
                    "path": str(analysis_path.parent),
                    "sha256": "d" * 64,
                },
            }
        ],
    }
    return manifest, artifact_id


def test_legacy_p0_manifest_contract_uses_actual_rotation0_final_mute_labels(
    tmp_path: Path,
) -> None:
    boards_root = tmp_path / "boards"
    manifest, artifact_id = _accepted_legacy_manifest(boards_root)
    attempt = analyzer._legacy_attempt(
        manifest,
        run_id="p0-run-a",
        artifact_id=artifact_id,
        test_only_legacy_boards_root=boards_root,
    )
    assert attempt["outcome"] == "quality_passed"


@pytest.mark.parametrize("mutation", ("python", "capture_program", "reanalysis_program"))
def test_legacy_p0_rejects_self_consistent_command_or_interpreter_forgery(
    mutation: str, tmp_path: Path
) -> None:
    boards_root = tmp_path / "boards"
    manifest, artifact_id = _accepted_legacy_manifest(boards_root)
    attempt = manifest["attempts"][0]
    assert isinstance(attempt, dict)
    condition = next(
        item
        for item in manifest["plan"]
        if item["stage"] == "rotation0" and item["center_frequency_hz"] == 5_800_000_000
    )
    if mutation == "python":
        manifest["configuration"]["python"] = "/usr/bin/python3"
        for row in manifest["plan"]:
            row["capture_command"][0] = "/usr/bin/python3"
            row["reference_reanalysis_command_template"][0] = "/usr/bin/python3"
        attempt["capture_command"][0] = "/usr/bin/python3"
        attempt["reference_reanalysis_command_template"][0] = "/usr/bin/python3"
        attempt["capture"]["command"][0] = "/usr/bin/python3"
        attempt["reanalysis"]["command"][0] = "/usr/bin/python3"
        expected = "configuration.python"
    elif mutation == "capture_program":
        condition["capture_command"][1] = "/tmp/forged-capture.py"
        attempt["capture_command"][1] = "/tmp/forged-capture.py"
        attempt["capture"]["command"][1] = "/tmp/forged-capture.py"
        expected = "reconstructed immutable"
    else:
        condition["reference_reanalysis_command_template"][1] = "/tmp/forged-analysis.py"
        attempt["reference_reanalysis_command_template"][1] = "/tmp/forged-analysis.py"
        attempt["reanalysis"]["command"][1] = "/tmp/forged-analysis.py"
        expected = "reconstructed immutable"

    with pytest.raises(analyzer.CohortAnalysisError, match=expected):
        analyzer._legacy_attempt(
            manifest,
            run_id="p0-run-a",
            artifact_id=artifact_id,
            test_only_legacy_boards_root=boards_root,
        )


def _raw_reanalysis_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    summary = {
        "phasor": "(0.01+0.002j)",
        "amplitude": abs(0.01 + 0.002j),
        "phase_deg": 11.309932474020213,
        "cycle_coherence": 0.99,
        "cycle_phase_std_deg": 1.0,
        "even_odd_phase_agreement": 0.98,
        "cycle_phasors": ["(0.01+0.002j)"] * 20,
    }
    rx1 = {
        **summary,
        "phasor": "(100+0j)",
        "amplitude": 100.0,
        "phase_deg": 0.0,
        "cycle_phasors": ["(100+0j)"] * 20,
    }
    quality = {
        "explained_fraction": 0.99,
        "residual_fraction": 0.01,
        "residual_energy": 1.0,
        "null_energy": 100.0,
        "coherent_energy": 99.0,
        "cycle_deviation_energy": 1.0,
        "detection_ratio": 99.0,
        "detection_strength": 0.99,
        "even_odd_agreement": 0.99,
        "cycle_coherence": 0.99,
        "combined_score": 0.98,
        "selected_bin_count": 8000,
    }
    decoded = {
        "marker_count": 20,
        "strict_frame_count": 20,
        "complete_frame_count": 20,
        "rejected_marker_count": 0,
    }
    schedule = {
        "selected": {
            "cycle_ms": 386.0,
            "marker_phase_ms": 117.0,
            "quality": quality,
            "complete_cycle_count": 20,
        },
        "distinct_runner_up": None,
        "score_margin": None,
        "provenance": {
            "method_version": "schedule_alignment_v1",
            "mode": "transition_seeded",
            "cycle_range_ms": [382.0, 390.0],
            "fine_cycle_step_ms": 0.2,
            "fine_phase_step_ms": 0.2,
            "coarse_phase_step_ms": 2.0,
            "candidate_count": 231,
            "valid_candidate_count": 231,
            "coarse_candidate_count": 0,
            "fine_candidate_count": 231,
            "refinement_basin_count": 8,
            "transition_seed_used": True,
        },
        "decoded_timing_agreement": {
            "cycle_error_ms": 0.0,
            "marker_error_ms": 0.0,
            "cycle_tolerance_ms": 0.2,
            "marker_tolerance_ms": 1.2,
            "agrees": True,
        },
        "decoded_timing": decoded,
    }
    pilot = {
        "estimated_offset_hz": 100_006.0,
        "residual_offset_hz": 6.0,
        "confidence": 0.99,
        "phase_step_coherence": 0.999,
        "phase_residual_rms_rad": 0.01,
        "window_count": 20,
        "snr_db": 35.0,
    }
    reference = {
        "cycle_ms": 386.0,
        "marker_phase_ms": 117.0,
        "bin_duration_ms": 1.0,
        "bin_count": 10_000,
        "complete_cycle_count": 20,
        "edge_exclusion_ms": 2.0,
        "alignment_score": 0.98,
        "alignment_even_odd_agreement": 0.99,
        "reference_valid_bin_fraction": 1.0,
        "continuity_verified": True,
        "continuity_block_count": 100,
        "all_off_anchor_count": 20,
        "all_off_rx1": rx1,
        "all_off_raw_rx2_over_rx1": summary,
        "schedule_alignment": schedule,
    }
    recomputed = {
        "pilot": pilot,
        "reference_transfer": reference,
        "all_off_transfer": 0.01 + 0.002j,
        "rx1_reference_amplitude": 100.0,
        "detected_pilot_snr_db": 35.0,
    }
    analysis = {
        "pilot": {name: value for name, value in pilot.items() if name != "snr_db"},
        "transfer": {
            **{
                name: reference[name]
                for name in (
                    "cycle_ms",
                    "marker_phase_ms",
                    "bin_duration_ms",
                    "bin_count",
                    "complete_cycle_count",
                    "edge_exclusion_ms",
                    "alignment_score",
                    "alignment_even_odd_agreement",
                    "reference_valid_bin_fraction",
                    "continuity_verified",
                    "continuity_block_count",
                    "all_off_anchor_count",
                )
            },
            "schedule_alignment": analyzer._legacy_schedule_alignment_document(schedule),
            "all_off": {
                "rx1": analyzer._legacy_phasor_document(
                    rx1,
                    label="test RX1",
                    minimum_cycle_coherence=0.9,
                ),
                "raw_rx2_over_rx1": analyzer._legacy_phasor_document(
                    summary,
                    label="test transfer",
                    minimum_cycle_coherence=0.75,
                ),
                "used_as_global_admission_gate": False,
            },
        },
    }
    return analysis, recomputed


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("phasor_real", 0.011),
        ("phasor_imag", 0.003),
        ("transfer_amplitude", 0.02),
        ("rx1_amplitude", 101.0),
    ),
)
def test_fresh_p0_normalization_rejects_schema_valid_collapse_field_tampering(
    field: str, value: float
) -> None:
    analysis, recomputed = _raw_reanalysis_fixture()
    analyzer._require_raw_equivalent_legacy_collapse_fields(analysis, recomputed)
    if field == "phasor_real":
        analysis["transfer"]["all_off"]["raw_rx2_over_rx1"]["phasor"]["real"] = value
    elif field == "phasor_imag":
        analysis["transfer"]["all_off"]["raw_rx2_over_rx1"]["phasor"]["imag"] = value
    elif field == "transfer_amplitude":
        analysis["transfer"]["all_off"]["raw_rx2_over_rx1"]["amplitude"] = value
    else:
        analysis["transfer"]["all_off"]["rx1"]["amplitude"] = value

    with pytest.raises(analyzer.CohortAnalysisError, match="raw-IQ recomputation"):
        analyzer._require_raw_equivalent_legacy_collapse_fields(analysis, recomputed)


def test_fresh_p0_normalization_rejects_self_consistent_full_analysis_forgery() -> None:
    analysis, recomputed = _raw_reanalysis_fixture()
    analysis["pilot"]["estimated_offset_hz"] = 99_900.0
    analysis["transfer"]["cycle_ms"] = 400.0
    analysis["transfer"]["marker_phase_ms"] = 50.0
    analysis["transfer"]["schedule_alignment"]["selected"]["cycle_ms"] = 400.0
    forged = analysis["transfer"]["all_off"]["raw_rx2_over_rx1"]
    forged["phasor"] = {"real": 0.2, "imag": 0.1}
    forged["amplitude"] = abs(0.2 + 0.1j)

    with pytest.raises(analyzer.CohortAnalysisError, match="raw-IQ recomputation"):
        analyzer._require_raw_equivalent_legacy_collapse_fields(analysis, recomputed)


def test_analyzer_rejects_preloaded_smateway_before_path_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer,
        "sys",
        SimpleNamespace(modules={"smateway.ambient_alias": object()}),
    )
    with pytest.raises(analyzer.CohortAnalysisError, match="preloaded"):
        analyzer._reject_preloaded_smateway_modules()


def test_smateway_origin_attestation_rejects_ambient_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "smateway"
    source_file = repository / "src/smateway/__init__.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text('"""Frozen source."""\n', encoding="utf-8")
    binding = {
        "path": "src/smateway/__init__.py",
        "sha256": sha256_path(source_file),
        "size_bytes": source_file.stat().st_size,
    }
    source = {"repository": str(repository), "files": [binding]}
    ambient = tmp_path / "ambient/smateway/__init__.py"
    ambient.parent.mkdir(parents=True)
    ambient.write_text('"""Ambient alias."""\n', encoding="utf-8")
    module = SimpleNamespace(
        __file__=str(ambient),
        __spec__=SimpleNamespace(origin=str(ambient)),
    )
    monkeypatch.setattr(
        analyzer,
        "_p2_runner",
        lambda: SimpleNamespace(SOURCE_FILES=(binding["path"],)),
    )
    monkeypatch.setattr(analyzer, "sys", SimpleNamespace(modules={"smateway": module}))

    with pytest.raises(analyzer.CohortAnalysisError, match="does not originate"):
        analyzer._attest_smateway_import_origins(source)


def test_p2_analyzer_rejects_ambient_dependency_and_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation_path, manifest_path, ledger_backend = _accepted_files(tmp_path)
    monkeypatch.setattr(analyzer, "_verify_p2_fixture_files", lambda *_args: None)
    monkeypatch.setattr(analyzer, "_reverify_p2_raw", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: value)
    monkeypatch.setattr(analyzer, "attestation_sha256", lambda _value: "5" * 64)
    plan = json.loads((manifest_path.parent / "plan.json").read_text(encoding="utf-8"))
    source = plan["plan_contract"]["source"]

    observed = analyzer._accepted_p2_observation(
        observation_path,
        manifest_path,
        expected_p0_bindings=P0_BINDINGS,
        ledger_backend=ledger_backend,
        current_dependency_attestation=source["pluto_plus_utils"],
        current_native_attestation=source["native_libiio"],
    )
    assert observed["run_id"] == "p2-run-a"

    with pytest.raises(analyzer.CohortAnalysisError, match="current pinned analyzer imports"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
            current_dependency_attestation={"repository_path": "/ambient/wheel"},
            current_native_attestation=source["native_libiio"],
        )
    with pytest.raises(analyzer.CohortAnalysisError, match="current analyzer process"):
        analyzer._accepted_p2_observation(
            observation_path,
            manifest_path,
            expected_p0_bindings=P0_BINDINGS,
            ledger_backend=ledger_backend,
            current_dependency_attestation=source["pluto_plus_utils"],
            current_native_attestation={"synthetic": "ambient-native"},
        )


def test_compare_repeats_raw_normalization_instead_of_trusting_a_forged_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = tmp_path / "sealed-p0.json"
    sealed.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    analysis = tmp_path / "analysis.json"
    manifest.write_text("{}", encoding="utf-8")
    analysis.write_text("{}", encoding="utf-8")
    observation = {"run_id": "p0-run-a", "analysis": {"all_off_transfer": {"real": 0.01}}}
    monkeypatch.setattr(
        analyzer,
        "admit_normalized_p0_evidence",
        lambda *_args, **_kwargs: (observation, {"path": str(sealed)}),
    )
    monkeypatch.setattr(
        analyzer,
        "_read_json",
        lambda *_args, **_kwargs: {
            "source_artifacts": {
                "legacy_manifest": {"path": str(manifest)},
                "reference_transfer_analysis": {"path": str(analysis)},
            }
        },
    )
    monkeypatch.setattr(
        analyzer,
        "verify_file_binding",
        lambda value, **_kwargs: Path(value["path"]),
    )
    monkeypatch.setattr(
        analyzer,
        "normalize_legacy_p0",
        lambda **_kwargs: {
            "observation": {
                "run_id": "p0-run-a",
                "analysis": {"all_off_transfer": {"real": 0.02}},
            }
        },
    )
    monkeypatch.setattr(
        analyzer,
        "_p2_runner",
        lambda: type("SyntheticRunner", (), {"SOURCE_FILES": ()})(),
    )

    with pytest.raises(analyzer.CohortAnalysisError, match="fresh bound raw-IQ"):
        analyzer._admit_and_recompute_normalized_p0(
            sealed,
            normalizer_source={"commit": "a" * 40},
            dependency={"dependency": "synthetic"},
            native={"native": "synthetic"},
        )
