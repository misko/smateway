from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pluto_plus.models import RadioIdentity

from smateway import global_ledger
from smateway.fine_frequency import (
    DDS_SCALE,
    build_coarse_schedule,
    build_plan_contract,
    campaign_cross_binding_from_plan_contract,
    plan_envelope,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_fine_frequency.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_fine_frequency_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)
PRODUCTION_LEDGER_BACKEND_FACTORY = runner._global_ledger_backend

ANALYZER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_fine_frequency.py"
ANALYZER_SPEC = importlib.util.spec_from_file_location(
    "analyze_5g8_fine_frequency_under_test", ANALYZER_SCRIPT
)
assert ANALYZER_SPEC is not None and ANALYZER_SPEC.loader is not None
analyzer = importlib.util.module_from_spec(ANALYZER_SPEC)
sys.modules[ANALYZER_SPEC.name] = analyzer
ANALYZER_SPEC.loader.exec_module(analyzer)
ANALYZER_PRODUCTION_LEDGER_BACKEND_FACTORY = analyzer.runner._global_ledger_backend


@pytest.fixture(autouse=True)
def _provision_isolated_fixed_global_run_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> global_ledger.LocalLedgerBackend:
    backend = global_ledger.LocalLedgerBackend(
        storage=global_ledger.provision_local_test_storage(tmp_path / "shared-ledger")
    )
    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: backend)
    monkeypatch.setattr(analyzer.runner, "_global_ledger_backend", lambda: backend)
    return backend


def _fixture_binding(run_id: str, *, stage: str = "direct_rx2_termination") -> dict[str, Any]:
    fixture_source = {
        "path": "/tmp/fixture-v2.json",
        "sha256": "d" * 64,
        "size_bytes": 1,
    }
    setup_source = {
        "path": "/tmp/setup-v1.json",
        "sha256": "e" * 64,
        "size_bytes": 1,
    }
    connected = bool(runner.TOPOLOGIES[stage]["selector_connected"])
    shared_fixture = {"pluto": {"serial": "serial-a"}}
    stage_delta: dict[str, Any] = {}
    component_ids: list[str] = []
    connection_ids: list[str] = []
    flash = (
        {
            "schema": 1,
            "binding_kind": "sealed_selector_flash_evidence_v1",
            "path": "/tmp/selector-flash.json",
            "sha256": "f" * 64,
            "campaign_id": "campaign-a",
            "run_id": "flash-a",
            "board_id": "board-a",
            "image_role": "bench",
        }
        if connected
        else None
    )
    selector = (
        {
            "schema": 1,
            "mode": "reviewed_static_selector_mailbox_all_off",
            "bench_manifest": {
                "path": "/tmp/bench-manifest.json",
                "file_sha256": "1" * 64,
                "elf_sha256": "2" * 64,
                "mailbox_address": 0,
                "mailbox_size": 1,
                "mailbox_magic": 1,
                "mailbox_version": 1,
                "max_lease_ms": 1,
                "mailbox_offsets": {},
            },
            "openocd_config": {
                "path": "/tmp/openocd.cfg",
                "file_sha256": "3" * 64,
            },
            "control_profile": {
                "path": "/tmp/control_profile.json",
                "file_sha256": "4" * 64,
                "header_path": "/tmp/control_profile.h",
                "header_file_sha256": "5" * 64,
                "profile_id": "profile-a",
                "revision": 1,
                "contract_sha256": "6" * 64,
                "all_off_code": 0,
            },
            "command": {
                "code": 0,
                "lease_ms": 0,
                "wait_until_applied": True,
                "readback_required": True,
            },
            "selector_flash_evidence": flash,
            "target_image_admission_contract": {
                "schema": 1,
                "flash_base_address": runner.leakage_runner.FLASH_BASE_ADDRESS,
                "firmware_bin_path": "/tmp/pluto_bench.bin",
                "firmware_bin_sha256": "7" * 64,
                "firmware_bin_size_bytes": 1024,
                "board_id": "board-a",
                "selector_flash_evidence_sha256": "f" * 64,
                "full_bin_extent_and_uid_required_before_mailbox": True,
            },
        }
        if connected
        else None
    )
    return {
        "schema": 1,
        "identity_kind": "5g8_t7_fixture_v2_binding",
        "topology_stage": stage,
        "topology_token": runner.TOPOLOGIES[stage]["token"],
        "selector_connected": connected,
        "fixture_evidence_v2": {
            "schema": 2,
            "fixture_kind": "5g8_general_topology_stage_fixture",
            "campaign_id": "campaign-a",
            "comparable_fixture_group_id": "fixture-group-a",
            "stage": stage,
            "run_id": run_id,
            "board_id": "board-a",
            "source_files": {
                "fixture_manifest": fixture_source,
                "setup_attestation": setup_source,
            },
            "setup_attestation": {
                "schema": 1,
                "attestation_kind": "5g8_general_topology_run_setup",
                "attestation_id": "setup-a",
                "created_at": "2026-08-29T00:00:00+00:00",
                "created_at_wall_clock_freshness_enforced": False,
                "run_id": run_id,
                "campaign_id": "campaign-a",
                "comparable_fixture_group_id": "fixture-group-a",
                "stage": stage,
                "fixture_manifest_sha256": fixture_source["sha256"],
                "shared_fixture_sha256": runner.canonical_json_sha256(shared_fixture),
                "stage_delta_sha256": runner.canonical_json_sha256(stage_delta),
                "observed_component_ids": component_ids,
                "observed_connection_ids": connection_ids,
                "setup_attestation_file": setup_source,
                "selector_flash_evidence": flash,
                "setup_evidence": {
                    "path": "/tmp/setup-evidence.txt",
                    "sha256": "9" * 64,
                    "size_bytes": 1,
                },
            },
            "selector_flash_evidence": flash,
            "shared_fixture": shared_fixture,
            "shared_fixture_sha256": runner.canonical_json_sha256(shared_fixture),
            "stage_delta": stage_delta,
            "stage_delta_sha256": runner.canonical_json_sha256(stage_delta),
            "prior_stage_binding": None,
            "component_ids": component_ids,
            "connection_ids": connection_ids,
            "characterization_summary": {},
        },
        "selector_control": selector,
    }


def _contract(
    tmp_path: Path,
    *,
    run_id: str = "t7-test-a",
    stage: str = "direct_rx2_termination",
) -> dict[str, Any]:
    contract = build_plan_contract(
        run_id=run_id,
        board_id="board-a",
        schedule=build_coarse_schedule(),
        source_identity={"commit": "a" * 40, "scientific_files_sha256": "b" * 64},
        native_identity={"path": "/usr/local/lib/libiio.so.0.25", "sha256": "c" * 64},
        fixture_identity=_fixture_binding(run_id, stage=stage),
        device_identity={"serial": "serial-a", "uri": "usb:1.2.3"},
        free_bytes=100_000_000_000,
    )
    state_root = tmp_path / "caller-state"
    return runner._augment_storage(
        contract,
        runner._run_root(state_root, "board-a", run_id),
        state_root=state_root,
    )


def _prepared(tmp_path: Path, *, run_id: str = "t7-test-a") -> tuple[dict[str, Any], Path, Path]:
    contract = _contract(tmp_path, run_id=run_id)
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    return contract, plan_path, manifest_path


def _evidence(condition: dict[str, Any], *, stream_id: int) -> dict[str, Any]:
    continuity = {
        "metadata_abi": 2,
        "stream_id": stream_id,
        "block_count": 3,
        "total_samples": 300_000,
        "first_buffer_sequence": 0,
    }
    return {
        "schema": 1,
        "evidence_kind": "5g8_fine_frequency_condition_v1",
        "condition_id": condition["condition_id"],
        "status": "passed",
        "device": {
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "usb_sysfs_path": "/sys/fake-usb",
            "radio_identity": {"serial": "serial-a", "uri": "usb:1.2.3"},
        },
        "rf_readback": {
            "rx_lo_hz": condition["frequency_hz"],
            "tx_lo_hz": condition["frequency_hz"],
            "lo_readback_provenance": ("pluto_plus_utils_continuous_exact_condition_preflight"),
            "sample_rate_hz": 1_000_000,
            "bandwidth_hz": 800_000,
            "tx1_gain_db": -20.0,
            "tx2_gain_db": -80.0,
            "tx2_gain_readback_provenance": (
                "pluto_plus_utils_capture_helper_internal_exact_readback"
            ),
            "dds_scale_readback": [
                DDS_SCALE,
                0.0,
                DDS_SCALE,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ],
            "dds_enabled_readback": [
                True,
                False,
                True,
                False,
                False,
                False,
                False,
                False,
            ],
            "dds_frequency_readback_hz": [
                100_006,
                0,
                -100_006,
                0,
                0,
                0,
                0,
                0,
            ],
        },
        "capture": {
            "stream_id": stream_id,
            "metadata_abi": 2,
            "first_buffer_sequence": 0,
            "sample_count": 300_000,
            "frame_count": 3,
            "kernel_buffers": 8,
            "continuity_passed": True,
            "headroom_passed": True,
            "clipped_sample_count": 0,
            "final_mute_passed": True,
            "live_ledger": continuity,
            "persisted_continuity": continuity,
        },
        "artifact": {
            "artifact_id": f"artifact-{stream_id}",
            "path": f"/tmp/artifact-{stream_id}",
            "data_path": f"/tmp/artifact-{stream_id}/artifact.sigmf-data",
            "data_sha256": f"{stream_id:064x}",
            "data_size_bytes": runner.BYTES_PER_CAPTURE,
            "metadata_path": f"/tmp/artifact-{stream_id}/artifact.sigmf-meta",
            "metadata_sha256": "a" * 64,
            "metadata_size_bytes": 100,
            "condition_record_path": (
                f"/tmp/artifact-{stream_id}/5g8-fine-frequency-condition.json"
            ),
            "condition_record_sha256": "b" * 64,
            "condition_record_size_bytes": 100,
            "local_rpi_storage": True,
            "pluto_storage_used": False,
        },
        "analysis": {
            "schema": 1,
            "analysis_kind": "raw_ci16_coherent_rx2_over_rx1_v1",
            "pilot": {},
            "coherent_transfer": {},
            "quality_rejection_reasons": [],
            "rx1_reference_tone_detected": True,
            "rx2_tone_detected": True,
            "phasor": {"real": 1.0, "imag": 0.0},
            "magnitude": 1.0,
            "phase_deg": 0.0,
            "amplitude_upper_bound_ratio": None,
            "nondetection_is_phase_free": False,
            "quality_passed": True,
        },
        "safety": {
            "initial_mute": {
                "status": "passed",
                "serial": "serial-a",
                "purpose": "pre_condition_exact_mute",
                "attestation": "mute_returned_radio_exact_serial_readback",
                "error": None,
            },
            "final_mute": {
                "status": "passed",
                "serial": "serial-a",
                "purpose": "final_condition_exact_mute",
                "attestation": "mute_returned_radio_exact_serial_readback",
                "error": None,
            },
            "persistence_began_only_after_final_mute_passed": True,
            "selector_all_off_passed_before_persistence": None,
        },
        "selector_static_all_off": None,
    }


def _confirmation_args(token: str, *, stage: str = "direct_rx2_termination") -> SimpleNamespace:
    return SimpleNamespace(
        confirm_experimental_policy=True,
        confirm_no_antennas=True,
        confirm_direct_rx2_termination=stage == "direct_rx2_termination",
        confirm_rx2_cable_terminated=stage == "rx2_cable_terminated",
        confirm_powered_selector_all_inputs_terminated=(
            stage == "powered_selector_all_inputs_terminated"
        ),
        confirm_fully_conducted=stage == "full_conducted_fixture",
        confirm_selector_static_all_off=bool(runner.TOPOLOGIES[stage]["selector_connected"]),
        confirm_tx2_terminated_muted=True,
        confirm_rx1_protected_reference=True,
        confirm_no_movement=True,
        confirm_topology_token=token,
    )


def _selector_snapshot(code: int = 0) -> dict[str, Any]:
    return {
        "applied_code": code,
        "command_code": code,
        "command_lease_ms": 0,
        "command_sequence": 9,
        "acknowledged_sequence": 9,
        "remaining_lease_ms": 0,
        "command_valid": True,
        "lease_active": False,
        "guard_active": False,
        "invalid_command": False,
    }


def _selector_attestation(purpose: str) -> dict[str, Any]:
    readback = _selector_snapshot()
    base = {
        "schema": 1,
        "evidence_kind": "static_selector_all_off_mailbox_readback",
        "purpose": purpose,
        "status": "passed",
        "all_off_code": 0,
        "lease_ms": 0,
        "readback": readback,
        "error": None,
    }
    if purpose == "after_condition":
        return {
            **base,
            "operation": "read_only",
            "command_was_issued": False,
            "pre_command_was_all_off": None,
            "pre_command": None,
            "commanded": None,
        }
    return {
        **base,
        "operation": "command_all_off",
        "command_was_issued": True,
        "pre_command_was_all_off": True,
        "pre_command": _selector_snapshot(),
        "commanded": _selector_snapshot(),
    }


def test_prepared_plan_is_read_only_idempotent_and_not_mutable(tmp_path: Path) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path)
    first_bytes = plan_path.read_bytes()
    assert plan_path.stat().st_mode & 0o777 == 0o400
    observed, manifest = runner._prepare_plan(plan_path, manifest_path, contract)
    assert observed == plan_envelope(contract)
    assert manifest["status"] == "prepared"
    binding = manifest["run_state_ledger"]
    assert binding["schema"] == 3
    assert binding["ledger_kind"] == "5g8_fine_frequency_fixed_global_run_id_ledger_v3"
    reservation = json.loads(Path(binding["reservation_slot"]["path"]).read_text(encoding="utf-8"))
    assert reservation["schema"] == 3
    assert reservation["marker_kind"].endswith("_v3")
    assert reservation["shared_global_ledger_authority"] == (
        global_ledger.authority_receipt_binding(binding["global_ledger_authority"])
    )
    assert plan_path.read_bytes() == first_bytes
    changed = json.loads(json.dumps(contract))
    changed["source_identity"]["commit"] = "f" * 40
    changed["source_identity_sha256"] = runner.canonical_json_sha256(changed["source_identity"])
    with pytest.raises(runner.FineFrequencyRunError, match="prior plan"):
        runner._prepare_plan(plan_path, manifest_path, changed)
    (plan_path.parent / "captures").mkdir()
    with pytest.raises(runner.FineFrequencyRunError, match="surviving run-derived"):
        runner._prepare_plan(plan_path, manifest_path, contract)


def test_confirmation_gate_requires_policy_topology_and_all_physical_facts(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    token = runner.TOPOLOGIES["direct_rx2_termination"]["token"]
    confirmation = runner._validate_confirmations(_confirmation_args(token), contract)
    assert confirmation["experimental_policy_reviewed"] is True
    bad = _confirmation_args(token)
    bad.confirm_no_antennas = False
    with pytest.raises(runner.FineFrequencyRunError, match="no_antennas"):
        runner._validate_confirmations(bad, contract)
    with pytest.raises(runner.FineFrequencyRunError, match="confirm-topology-token"):
        runner._validate_confirmations(_confirmation_args("WRONG"), contract)


@pytest.mark.parametrize("stage", tuple(runner.TOPOLOGIES))
def test_confirmation_gate_requires_only_the_truthful_stage(tmp_path: Path, stage: str) -> None:
    contract = _contract(tmp_path, run_id=f"confirm-{stage}", stage=stage)
    token = runner.TOPOLOGIES[stage]["token"]
    accepted = runner._validate_confirmations(_confirmation_args(token, stage=stage), contract)
    assert accepted[f"stage_{stage}"] is True
    contradictory = _confirmation_args(token, stage=stage)
    contradictory.confirm_fully_conducted = True
    if stage == "full_conducted_fixture":
        contradictory.confirm_direct_rx2_termination = True
    with pytest.raises(runner.FineFrequencyRunError, match="contradictory"):
        runner._validate_confirmations(contradictory, contract)


def test_selector_connected_condition_requires_exact_static_all_off_readbacks(
    tmp_path: Path,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="selector-readback",
        stage="full_conducted_fixture",
    )
    condition = contract["schedule"]["conditions"][0]
    evidence = _evidence(condition, stream_id=81)
    evidence["safety"]["selector_all_off_passed_before_persistence"] = True
    evidence["selector_static_all_off"] = {
        "before": _selector_attestation("before_condition"),
        "after": _selector_attestation("after_condition"),
        "cleanup": _selector_attestation("condition_cleanup_all_off"),
    }
    admitted = runner.validate_live_condition_evidence(
        contract,
        evidence,
        prior_stream_ids=set(),
        prior_artifact_sha256s=set(),
    )
    assert admitted["selector_static_all_off"]["after"]["operation"] == "read_only"
    tampered = json.loads(json.dumps(evidence))
    tampered["selector_static_all_off"]["after"]["readback"]["applied_code"] = 1
    with pytest.raises(runner.FineFrequencyError, match="after evidence failed"):
        runner.validate_live_condition_evidence(
            contract,
            tampered,
            prior_stream_ids=set(),
            prior_artifact_sha256s=set(),
        )


def test_selector_connected_topology_requires_exact_sealed_control_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "fixture.json"
    setup = tmp_path / "setup.json"
    flash = tmp_path / "flash.json"
    fixture.write_text("{}\n", encoding="utf-8")
    setup.write_text("{}\n", encoding="utf-8")
    flash.write_text("{}\n", encoding="utf-8")
    common = {
        "run_id": "run-a",
        "board_id": "board-a",
        "serial": "serial-a",
        "topology_stage": "full_conducted_fixture",
    }
    with pytest.raises(runner.FineFrequencyRunError, match="sealed flash"):
        runner._fixture_identity(
            fixture,
            setup,
            **common,
            selector_flash_path=None,
            selector_flash_sha256=None,
            selector_flash_run_id=None,
            bench_manifest_path=None,
            openocd_config_path=None,
            profile_path=None,
        )
    fixture_v2 = _fixture_binding("run-a", stage="full_conducted_fixture")["fixture_evidence_v2"]
    selector_control = _fixture_binding("run-a", stage="full_conducted_fixture")["selector_control"]
    observed: dict[str, Any] = {}

    def fixture_boundary(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return fixture_v2

    monkeypatch.setattr(
        runner.leakage_runner,
        "_fixture_evidence_from_manifests",
        fixture_boundary,
    )
    monkeypatch.setattr(
        runner.leakage_runner,
        "_selector_control_contract",
        lambda **_kwargs: selector_control,
    )
    bench = tmp_path / "bench.json"
    openocd = tmp_path / "openocd.cfg"
    profile = tmp_path / "control_profile.json"
    for path in (bench, openocd, profile):
        path.write_text("{}\n", encoding="utf-8")
    identity = runner._fixture_identity(
        fixture,
        setup,
        **common,
        selector_flash_path=flash,
        selector_flash_sha256="f" * 64,
        selector_flash_run_id="flash-a",
        bench_manifest_path=bench,
        openocd_config_path=openocd,
        profile_path=profile,
    )
    assert identity["fixture_evidence_v2"] == fixture_v2
    assert identity["selector_control"] == selector_control
    assert observed["selector_flash_evidence_sha256"] == "f" * 64
    assert observed["selector_flash_run_id"] == "flash-a"
    with pytest.raises(runner.FineFrequencyRunError, match="forbidden"):
        runner._fixture_identity(
            fixture,
            setup,
            run_id="run-a",
            board_id="board-a",
            serial="serial-a",
            topology_stage="direct_rx2_termination",
            selector_flash_path=flash,
            selector_flash_sha256="f" * 64,
            selector_flash_run_id="flash-a",
            bench_manifest_path=bench,
            openocd_config_path=openocd,
            profile_path=profile,
        )


def test_fixture_input_symlink_is_rejected_before_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "fixture-target.json"
    target.write_text("{}\n", encoding="utf-8")
    fixture = tmp_path / "fixture-link.json"
    fixture.symlink_to(target)
    setup = tmp_path / "setup.json"
    setup.write_text("{}\n", encoding="utf-8")
    called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        runner.leakage_runner,
        "_fixture_evidence_from_manifests",
        forbidden,
    )
    with pytest.raises(runner.FineFrequencyRunError, match="symlink"):
        runner._fixture_identity(
            fixture,
            setup,
            run_id="run-a",
            board_id="board-a",
            serial="serial-a",
            topology_stage="direct_rx2_termination",
            selector_flash_path=None,
            selector_flash_sha256=None,
            selector_flash_run_id=None,
            bench_manifest_path=None,
            openocd_config_path=None,
            profile_path=None,
        )
    assert called is False


def test_interrupt_burns_run_and_forbids_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(tmp_path)
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    mute_calls: list[str] = []

    def interrupt(
        _contract: dict[str, Any], _condition: dict[str, Any], _root: Path
    ) -> dict[str, Any]:
        raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt, match="synthetic"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=interrupt,
            mute_boundary=_campaign_mute(mute_calls),
        )
    assert mute_calls == ["mute:campaign_preflight", "mute:campaign_failure"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["campaign_accepted"] is False
    assert manifest["accepted_condition_count"] == 0
    assert (manifest_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME).is_file()
    failure_path = manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME
    assert json.loads(failure_path.read_text(encoding="utf-8"))["interrupted"] is True
    with pytest.raises(runner.FineFrequencyRunError, match="burned"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=interrupt,
        )


@pytest.mark.parametrize(
    ("artifact_name", "is_directory"),
    (
        (runner.EXECUTION_TOMBSTONE_FILENAME, False),
        (runner.FAILURE_TOMBSTONE_FILENAME, False),
        (runner.RESULTS_FILENAME, False),
        ("analysis-started.tombstone.json", False),
        ("unexpected-run-output.tmp", False),
        ("captures", True),
        (".capture-staging", True),
        (".failed", True),
        ("quarantine", True),
    ),
)
def test_execution_rejects_every_surviving_run_derived_artifact_before_hardware(
    tmp_path: Path,
    artifact_name: str,
    is_directory: bool,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-history-{artifact_name.replace('.', '-')}",
    )
    artifact = plan_path.parent / artifact_name
    if is_directory:
        artifact.mkdir()
        if artifact_name == "captures":
            copied = artifact / "copied-stale-capture"
            copied.mkdir()
            (copied / "capture.sigmf-data").write_bytes(b"stale IQ")
    else:
        artifact.write_text("prior run history\n", encoding="utf-8")
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("hardware boundary called")

    with pytest.raises(runner.FineFrequencyRunError, match="surviving run-derived"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "prepared"


def test_restored_prepared_manifest_and_deleted_tombstone_cannot_splice_stale_capture(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path, run_id="t7-rollback-splice")
    prepared_manifest = manifest_path.read_bytes()
    execution_path = plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    execution_path.write_text("prior execution marker\n", encoding="utf-8")
    running = json.loads(prepared_manifest)
    running["status"] = "running"
    running["attempts"] = [{"status": "running"}]
    runner.write_json_atomic(manifest_path, running)
    stale_capture = plan_path.parent / "captures" / "copied-prior-condition"
    stale_capture.mkdir(parents=True)
    (stale_capture / "capture.sigmf-data").write_bytes(b"copied stale IQ")

    # Model an attacker rolling back mutable state while one run-derived tree survives.
    runner.write_json_atomic(manifest_path, json.loads(prepared_manifest))
    execution_path.unlink()
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("hardware boundary called after manifest rollback")

    with pytest.raises(runner.FineFrequencyRunError, match="captures"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []
    assert not execution_path.exists()
    assert manifest_path.read_bytes() == prepared_manifest


def test_execution_rejects_run_path_symlink_ancestry_before_hardware(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    _contract_value, plan_path, manifest_path = _prepared(
        state_root,
        run_id="t7-symlinked-run",
    )
    relocated = tmp_path / "relocated-state"
    state_root.rename(relocated)
    state_root.symlink_to(relocated, target_is_directory=True)
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("hardware boundary called through symlink ancestry")

    with pytest.raises(runner.FineFrequencyRunError, match="symlink"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []


def test_execution_rejects_nonlocal_destination_device_before_hardware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-nonlocal-storage",
    )
    monkeypatch.setattr(
        runner,
        "_filesystem_device",
        lambda path: 1 if Path(path) == Path("/home/pi") else 2,
    )
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("hardware boundary called on nonlocal storage")

    with pytest.raises(runner.FineFrequencyRunError, match="local filesystem"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []


def _burn_then_fail_run(
    *,
    plan_path: Path,
    manifest_path: Path,
) -> None:
    with pytest.raises(runner.FineFrequencyRunError, match="synthetic admitted failure"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                runner.FineFrequencyRunError("synthetic admitted failure")
            ),
            mute_boundary=_campaign_mute([]),
        )


def _assert_retry_rejected_without_live_boundaries(
    *,
    plan_path: Path,
    manifest_path: Path,
    match: str,
) -> None:
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("live boundary called during replay rejection")

    with pytest.raises(runner.FineFrequencyRunError, match=match):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            preflight_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []


def _external_failure_receipt(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slot = Path(manifest["run_state_ledger"]["failure_receipt_slot"]["path"])
    document = json.loads(slot.read_text(encoding="utf-8"))
    assert slot.stat().st_mode & 0o222 == 0
    assert slot.stat().st_nlink == 2
    assert document["campaign_accepted"] is False
    assert document["automatic_retry_forbidden"] is True
    return document


def test_external_guard_rejects_deleted_history_and_restored_pristine_manifest(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-delete-all-restore-prepared",
    )
    prepared_manifest = manifest_path.read_bytes()
    ledger_binding = json.loads(prepared_manifest)["run_state_ledger"]
    _burn_then_fail_run(plan_path=plan_path, manifest_path=manifest_path)

    for entry in plan_path.parent.iterdir():
        if entry.name not in {runner.PLAN_FILENAME, runner.MANIFEST_FILENAME}:
            entry.unlink()
    manifest_path.write_bytes(prepared_manifest)
    # Even deleting the descriptive external marker cannot undo the inode-bound guard.
    Path(ledger_binding["burn_marker_path"]).unlink()

    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="already burned|burn guard.*consumed|failure history",
    )
    guard = Path(ledger_binding["burn_guard"]["path"])
    assert guard.read_bytes() == b"\x01"
    assert guard.stat().st_mode & 0o222 == 0


def test_external_ledger_rejects_renamed_burned_root_and_reconstructed_two_file_root(
    tmp_path: Path,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-renamed-root-reconstruction",
    )
    plan_bytes = plan_path.read_bytes()
    prepared_manifest = manifest_path.read_bytes()
    _burn_then_fail_run(plan_path=plan_path, manifest_path=manifest_path)

    run_root = plan_path.parent
    forensic = run_root.with_name(f"{run_root.name}.forensic")
    run_root.rename(forensic)
    run_root.mkdir()
    plan_path.write_bytes(plan_bytes)
    plan_path.chmod(0o400)
    manifest_path.write_bytes(prepared_manifest)

    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="ledger identity differs",
    )
    assert forensic.is_dir()


@pytest.mark.parametrize(
    "mutation",
    ("extra-key", "missing-created-at", "missing-updated-at", "wrong-time-type", "reencoded"),
)
def test_prepared_manifest_is_exact_and_hash_bound_to_reservation(
    tmp_path: Path,
    mutation: str,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-exact-manifest-{mutation}",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "extra-key":
        manifest["unexpected"] = None
    elif mutation == "missing-created-at":
        del manifest["created_at"]
    elif mutation == "missing-updated-at":
        del manifest["updated_at"]
    elif mutation == "wrong-time-type":
        manifest["created_at"] = 123
        manifest["updated_at"] = 123
    elif mutation != "reencoded":
        raise AssertionError(f"unknown mutation {mutation}")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match=("prepared campaign" if mutation != "reencoded" else "manifest bytes differ"),
    )


@pytest.mark.parametrize("attack", ("missing", "moved", "recreated", "symlink"))
def test_reservation_deletion_move_recreation_or_symlink_fails_closed(
    tmp_path: Path,
    attack: str,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-reservation-{attack}",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reservation = Path(manifest["run_state_ledger"]["reservation_slot"]["path"])
    reservation_bytes = reservation.read_bytes()
    if attack == "missing":
        reservation.unlink()
    elif attack == "moved":
        reservation.rename(reservation.with_suffix(".moved"))
    elif attack == "recreated":
        reservation.unlink()
        reservation.write_bytes(reservation_bytes)
        reservation.chmod(global_ledger.SEALED_FILE_MODE)
    elif attack == "symlink":
        real = reservation.with_suffix(".real")
        reservation.rename(real)
        reservation.symlink_to(real)

    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="reservation|symlink|ledger identity",
    )


def test_symlinked_external_ledger_directory_is_rejected_before_live_boundaries(
    tmp_path: Path,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-ledger-symlink",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger_directory = Path(manifest["run_state_ledger"]["ledger_directory"]["path"])
    relocated = ledger_directory.with_name(f"{ledger_directory.name}.relocated")
    ledger_directory.rename(relocated)
    ledger_directory.symlink_to(relocated, target_is_directory=True)
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="symlink",
    )


def test_moved_run_and_ledger_cannot_be_replanned_while_inode_anchors_survive(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-moved-ledger-replan",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_root = plan_path.parent
    ledger_directory = Path(manifest["run_state_ledger"]["ledger_directory"]["path"])
    run_root.rename(run_root.with_name(f"{run_root.name}.forensic"))
    ledger_directory.rename(ledger_directory.with_name(f"{ledger_directory.name}.forensic"))

    with pytest.raises(runner.FineFrequencyRunError, match="inode-anchor history"):
        runner._prepare_plan(plan_path, manifest_path, contract)
    assert not run_root.exists()


def test_whole_caller_state_parent_relocation_cannot_replan_or_execute_same_identity(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-whole-caller-parent-relocation",
    )
    plan_bytes = plan_path.read_bytes()
    prepared_manifest_bytes = manifest_path.read_bytes()
    _burn_then_fail_run(plan_path=plan_path, manifest_path=manifest_path)
    state_root = Path(contract["execution_storage"]["state_root"])
    relocated = state_root.with_name(f"{state_root.name}.forensic")
    state_root.rename(relocated)

    with pytest.raises(runner.FineFrequencyRunError, match="external T7 ledger history"):
        runner._prepare_plan(plan_path, manifest_path, contract)
    assert not plan_path.parent.exists()

    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(plan_bytes)
    plan_path.chmod(0o400)
    manifest_path.write_bytes(prepared_manifest_bytes)
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="ledger identity differs|already burned|failure history",
    )
    assert relocated.is_dir()


def test_same_board_and_run_id_collides_globally_across_state_roots(tmp_path: Path) -> None:
    run_id = "t7-cross-state-global-collision"
    first = _contract(tmp_path / "state-a", run_id=run_id)
    first_root = Path(first["execution_storage"]["run_root"])
    runner._prepare_plan(
        first_root / runner.PLAN_FILENAME,
        first_root / runner.MANIFEST_FILENAME,
        first,
    )
    second = _contract(tmp_path / "state-b", run_id=run_id)
    second_root = Path(second["execution_storage"]["run_root"])
    assert (
        first["execution_storage"]["global_run_ledger_authority"]["ledger_key"]
        == second["execution_storage"]["global_run_ledger_authority"]["ledger_key"]
    )
    assert (
        first["execution_storage"]["global_run_ledger_authority"]["canonical_run_identity_sha256"]
        != second["execution_storage"]["global_run_ledger_authority"][
            "canonical_run_identity_sha256"
        ]
    )
    with pytest.raises(runner.FineFrequencyRunError, match="external T7 ledger history"):
        runner._prepare_plan(
            second_root / runner.PLAN_FILENAME,
            second_root / runner.MANIFEST_FILENAME,
            second,
        )
    assert not second_root.exists()


def test_distinct_run_ids_have_collision_free_global_namespaces(tmp_path: Path) -> None:
    first, first_plan, first_manifest = _prepared(tmp_path / "state-a", run_id="t7-key-a")
    second, second_plan, second_manifest = _prepared(tmp_path / "state-b", run_id="t7-key-b")
    assert first_plan.is_file() and first_manifest.is_file()
    assert second_plan.is_file() and second_manifest.is_file()
    assert (
        first["execution_storage"]["global_run_ledger_authority"]["ledger_key"]
        != second["execution_storage"]["global_run_ledger_authority"]["ledger_key"]
    )


@pytest.mark.parametrize("attack", ("missing", "recreated", "copied", "symlink"))
def test_fixed_global_root_relocation_or_recreation_fails_closed_before_live_access(
    tmp_path: Path,
    attack: str,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-global-root-{attack}",
    )
    storage = runner._global_ledger_backend().storage()
    root = Path(str(storage["global_root"]["path"]))
    moved = root.with_name(f"{root.name}.moved")
    root.rename(moved)
    if attack == "recreated":
        (root / global_ledger.ENTRIES_DIRECTORY).mkdir(
            parents=True, mode=global_ledger.DIRECTORY_MODE
        )
        (root / global_ledger.ANCHORS_DIRECTORY).mkdir(mode=global_ledger.DIRECTORY_MODE)
        root.chmod(global_ledger.DIRECTORY_MODE)
    elif attack == "copied":
        shutil.copytree(moved, root, copy_function=shutil.copy2)
        root.chmod(global_ledger.DIRECTORY_MODE)
        (root / global_ledger.ENTRIES_DIRECTORY).chmod(global_ledger.DIRECTORY_MODE)
        (root / global_ledger.ANCHORS_DIRECTORY).chmod(global_ledger.DIRECTORY_MODE)
    elif attack == "symlink":
        root.symlink_to(moved, target_is_directory=True)
    elif attack != "missing":
        raise AssertionError(f"unknown attack {attack}")

    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="shared global|symlink|ledger identity|external T7|No such file",
    )
    if attack == "missing":
        assert not root.exists()


@pytest.mark.parametrize("attack", ("missing", "recreated", "symlink"))
def test_fixed_global_seal_relocation_or_recreation_fails_closed_before_live_access(
    tmp_path: Path,
    attack: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-global-seal-{attack}",
    )
    real_backend = runner._global_ledger_backend()
    storage = dict(real_backend.storage())
    if attack == "missing":

        def failed_storage() -> Mapping[str, Any]:
            raise global_ledger.GlobalLedgerError("synthetic shared root seal missing")

        backend = SimpleNamespace(storage=failed_storage, mutate=real_backend.mutate)
    else:
        storage["global_root_seal"] = {
            "path": str(tmp_path / f"synthetic-{attack}-shared-root-seal")
        }
        backend = SimpleNamespace(storage=lambda: storage, mutate=real_backend.mutate)
    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: backend)
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="shared global|root seal|storage|differs",
    )


@pytest.mark.parametrize(
    "directory_name",
    (
        global_ledger.ENTRIES_DIRECTORY,
        global_ledger.ANCHORS_DIRECTORY,
    ),
)
@pytest.mark.parametrize("attack", ("recreated", "symlink"))
def test_fixed_global_namespace_directory_replacement_fails_closed(
    tmp_path: Path,
    directory_name: str,
    attack: str,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id=f"t7-global-namespace-{directory_name}-{attack}",
    )
    root = Path(str(runner._global_ledger_backend().storage()["global_root"]["path"]))
    directory = root / directory_name
    moved = directory.with_name(f"{directory.name}.moved")
    directory.rename(moved)
    if attack == "recreated":
        directory.mkdir(mode=global_ledger.DIRECTORY_MODE)
    elif attack == "symlink":
        directory.symlink_to(moved, target_is_directory=True)
    else:
        raise AssertionError(f"unknown attack {attack}")
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="shared global|symlink|ledger identity|external T7|cannot stat",
    )


@pytest.mark.parametrize("relationship", ("equal", "above", "below"))
def test_caller_state_tree_cannot_contain_or_be_contained_by_global_root(
    tmp_path: Path,
    relationship: str,
) -> None:
    augmented = _contract(tmp_path, run_id=f"t7-state-overlap-{relationship}")
    core = dict(augmented)
    del core["execution_storage"]
    root = Path(str(runner._global_ledger_backend().storage()["global_root"]["path"]))
    if relationship == "equal":
        state_root = root
    elif relationship == "above":
        state_root = root.parent
    elif relationship == "below":
        state_root = root / "caller-state"
    else:
        raise AssertionError(f"unknown relationship {relationship}")
    with pytest.raises(runner.FineFrequencyRunError, match="overlaps caller state"):
        runner._augment_storage(
            core,
            runner._run_root(state_root, "board-a", str(core["run_id"])),
            state_root=state_root,
        )


def test_production_global_authority_paths_are_source_fixed_and_privileged() -> None:
    assert Path("/var/lib/smateway/global-run-ledger-v1") == global_ledger.GLOBAL_ROOT
    assert Path("/etc/smateway/global-run-ledger-root-v1.json") == global_ledger.GLOBAL_SEAL
    assert Path("/usr/local/libexec/smateway-ledger-helper") == global_ledger.HELPER_PATH
    assert Path("/usr/bin/sudo") == global_ledger.SUDO_PATH
    assert Path("/etc/sudoers.d/smateway-ledger-helper") == global_ledger.SUDOERS_PATH
    assert isinstance(PRODUCTION_LEDGER_BACKEND_FACTORY(), global_ledger.SudoLedgerBackend)
    assert isinstance(
        ANALYZER_PRODUCTION_LEDGER_BACKEND_FACTORY(),
        global_ledger.SudoLedgerBackend,
    )
    assert "src/smateway/global_ledger.py" in runner.SOURCE_FILES
    parser = runner._parser()
    assert not any(action.dest.startswith("global_ledger") for action in parser._actions)
    analyzer_parser = analyzer._parser()
    assert not any(action.dest.startswith("global_ledger") for action in analyzer_parser._actions)


def test_t7_shared_sudo_backend_uses_exact_noninteractive_helper_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path, run_id="t7-production-sudo-protocol")
    authority = contract["execution_storage"]["global_run_ledger_authority"]
    request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": "a" * 32},
    )
    response = runner._global_ledger_backend().mutate(request)
    observed: dict[str, Any] = {}

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> SimpleNamespace:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response, sort_keys=True),
            stderr="",
        )

    monkeypatch.setattr(global_ledger.subprocess, "run", fake_run)
    monkeypatch.setattr(global_ledger, "attest_runner_runtime", lambda: {})
    assert global_ledger.SudoLedgerBackend().mutate(request) == response
    assert observed["command"] == (
        str(global_ledger.SUDO_PATH),
        "-n",
        "--",
        str(global_ledger.HELPER_PATH),
        "reserve_run",
    )
    kwargs = observed["kwargs"]
    assert kwargs["input"] == json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert kwargs["env"] == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 30


@pytest.mark.parametrize("stdout", ("not-json", "[]", "{}"))
def test_t7_shared_sudo_backend_rejects_malformed_helper_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    contract = _contract(tmp_path, run_id=f"t7-malformed-helper-{len(stdout)}")
    authority = contract["execution_storage"]["global_run_ledger_authority"]
    request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": "b" * 32},
    )
    monkeypatch.setattr(
        global_ledger.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=stdout,
            stderr="",
        ),
    )
    monkeypatch.setattr(global_ledger, "attest_runner_runtime", lambda: {})
    with pytest.raises(global_ledger.GlobalLedgerError, match="malformed|non-object|differs"):
        global_ledger.SudoLedgerBackend().mutate(request)


def test_t7_production_authority_fails_closed_when_privileged_storage_is_unprovisioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    augmented = _contract(tmp_path, run_id="t7-unprovisioned-shared-ledger")
    core = dict(augmented)
    del core["execution_storage"]

    def unavailable(_self: global_ledger.SudoLedgerBackend) -> dict[str, Any]:
        raise global_ledger.GlobalLedgerError("synthetic shared authority is unprovisioned")

    monkeypatch.setattr(global_ledger.SudoLedgerBackend, "storage", unavailable)
    monkeypatch.setattr(runner, "_global_ledger_backend", PRODUCTION_LEDGER_BACKEND_FACTORY)
    state_root = tmp_path / "new-state"
    with pytest.raises(runner.FineFrequencyRunError, match="unprovisioned"):
        runner._augment_storage(
            core,
            runner._run_root(state_root, "board-a", str(core["run_id"])),
            state_root=state_root,
        )


def test_t7_shared_mutation_fails_closed_when_sudo_privilege_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(tmp_path, run_id="t7-sudo-privilege-denied")
    authority = contract["execution_storage"]["global_run_ledger_authority"]
    real_backend = runner._global_ledger_backend()

    def denied(_request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise global_ledger.GlobalLedgerError("synthetic sudo privilege denied")

    backend = SimpleNamespace(storage=real_backend.storage, mutate=denied)
    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: backend)
    with pytest.raises(runner.FineFrequencyRunError, match="sudo privilege denied"):
        runner._global_ledger_mutation(
            authority=authority,
            operation="reserve_run",
            payload={"reservation_id": "c" * 32},
        )


def test_analyzer_reattests_shared_authority_before_campaign_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _plan_path, _manifest_path = _prepared(
        tmp_path,
        run_id="t7-analyzer-shared-authority",
    )
    real_backend = analyzer.runner._global_ledger_backend()
    changed_storage = dict(real_backend.storage())
    changed_storage["policy_registry_sha256"] = "f" * 64
    changed_backend = SimpleNamespace(
        storage=lambda: changed_storage,
        mutate=real_backend.mutate,
    )
    monkeypatch.setattr(analyzer.runner, "_global_ledger_backend", lambda: changed_backend)
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="shared global"):
        analyzer.analyze_campaign(Path(contract["execution_storage"]["run_root"]))


def test_nonlocal_external_ledger_device_is_rejected_before_live_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-ledger-nonlocal",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger_directory = Path(manifest["run_state_ledger"]["ledger_directory"]["path"])

    def device(path: Path) -> int:
        exact = Path(path).absolute()
        if exact == Path("/home/pi"):
            return 1
        if exact == ledger_directory or ledger_directory in exact.parents:
            return 2
        return 1

    monkeypatch.setattr(runner, "_filesystem_device", device)
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="local filesystem",
    )


def test_external_burn_is_durable_before_source_fixture_or_hardware_boundary(
    tmp_path: Path,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-burn-before-preflight",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["run_state_ledger"]
    calls: list[str] = []

    def stop_at_preflight(_contract: Mapping[str, Any]) -> None:
        calls.append("preflight")
        guard = Path(binding["burn_guard"]["path"])
        marker = Path(binding["burn_marker_path"])
        assert guard.read_bytes() == b"\x01"
        assert guard.stat().st_mode & 0o222 == 0
        assert marker.is_file()
        assert marker.stat().st_mode & 0o222 == 0
        raise runner.FineFrequencyRunError("synthetic source preflight stop")

    with pytest.raises(runner.FineFrequencyRunError, match="source preflight stop"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            preflight_boundary=stop_at_preflight,
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("capture boundary called")
            ),
            mute_boundary=_campaign_mute(calls),
        )
    assert calls == ["preflight", "mute:campaign_failure"]


def test_atomic_burn_marker_commit_response_loss_is_partial_and_never_touches_live_rf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-marker-seal-failure",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["run_state_ledger"]
    real_backend = runner._global_ledger_backend()
    touched: list[str] = []

    def lose_response(stage: str) -> None:
        if stage == "after_marker_commit":
            raise OSError("synthetic marker-commit response loss")

    failing_backend = global_ledger.LocalLedgerBackend(
        storage=real_backend.storage(),
        test_only_burn_fault=lose_response,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("live boundary called after incomplete ledger burn")

    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: failing_backend)
    with pytest.raises(runner.FineFrequencyRunError, match="marker-commit response loss"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            preflight_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []
    assert Path(binding["burn_guard"]["path"]).read_bytes() == b""
    assert Path(binding["burn_marker_path"]).is_file()
    failure_receipt = Path(binding["failure_receipt_slot"]["path"])
    receipt = json.loads(failure_receipt.read_text(encoding="utf-8"))
    assert receipt["schema"] == 2
    assert receipt["marker_kind"] == "5g8_fine_frequency_external_failure_receipt_v2"
    assert receipt["shared_global_ledger_authority"] == (
        global_ledger.authority_receipt_binding(binding["global_ledger_authority"])
    )
    burn = receipt["run_consumption_receipt"]
    assert burn["evidence_kind"] == "5g8_fine_frequency_burn_acquisition_emergency_v2"
    assert burn["burn_classification"] == "partial"
    assert burn["authoritative_state"] == "burn_committed_guard_pending"
    assert burn["authoritative_inspection"]["classification"] == ("burn_committed_guard_pending")
    assert burn["live_access_began"] is False
    assert burn["live_cleanup_call_count"] == 0
    assert receipt["failure_cleanup_evidence"]["live_cleanup_call_count"] == 0
    assert receipt["failure_cleanup_evidence"]["live_cleanup_prohibited"] is True
    assert receipt["original_error"]["phase"] == "external_burn_acquisition"
    assert (
        analyzer._validate_external_failure_receipt_document(
            receipt,
            contract=_contract_value,
            plan_path=plan_path,
            ledger_binding=binding,
        )
        == receipt
    )
    forged_receipts: list[dict[str, Any]] = []
    missing = json.loads(json.dumps(receipt))
    del missing["failed_at"]
    forged_receipts.append(missing)
    extra = json.loads(json.dumps(receipt))
    extra["unexpected"] = None
    forged_receipts.append(extra)
    forged_timestamp = json.loads(json.dumps(receipt))
    forged_timestamp["failed_at"] = str(forged_timestamp["failed_at"]).replace("+00:00", "Z")
    forged_receipts.append(forged_timestamp)
    forged_inspection_hash = json.loads(json.dumps(receipt))
    forged_inspection_hash["run_consumption_receipt"]["authoritative_inspection_sha256"] = "0" * 64
    forged_receipts.append(forged_inspection_hash)
    forged_cleanup = json.loads(json.dumps(receipt))
    forged_cleanup["failure_cleanup_evidence"]["live_cleanup_call_count"] = 1
    forged_receipts.append(forged_cleanup)
    forged_nonce = json.loads(json.dumps(receipt))
    forged_nonce["execution_nonce"] = "0" * 32
    forged_receipts.append(forged_nonce)
    for forged in forged_receipts:
        with pytest.raises(analyzer.FineFrequencyAnalysisError):
            analyzer._validate_external_failure_receipt_document(
                forged,
                contract=_contract_value,
                plan_path=plan_path,
                ledger_binding=binding,
            )
    assert failure_receipt.stat().st_mode & 0o222 == 0

    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: real_backend)
    _assert_retry_rejected_without_live_boundaries(
        plan_path=plan_path,
        manifest_path=manifest_path,
        match="already burned|burn guard.*consumed|failure history",
    )


def test_atomic_burn_denial_is_pristine_and_never_touches_live_rf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-atomic-burn-denied",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["run_state_ledger"]
    real_backend = runner._global_ledger_backend()
    touched: list[str] = []

    def deny_burn(request: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.get("operation") == "burn_run":
            raise global_ledger.GlobalLedgerError("synthetic burn authorization denied")
        return real_backend.mutate(request)

    denied_backend = SimpleNamespace(
        storage=real_backend.storage,
        mutate=deny_burn,
        inspect=real_backend.inspect,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("live boundary called after denied burn")

    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: denied_backend)
    with pytest.raises(runner.FineFrequencyRunError, match="authorization denied"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            preflight_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []
    assert Path(binding["burn_guard"]["path"]).read_bytes() == b""
    assert not Path(binding["burn_marker_path"]).exists()
    receipt = _external_failure_receipt(manifest_path)
    burn = receipt["run_consumption_receipt"]
    assert burn["burn_classification"] == "pristine"
    assert burn["authoritative_state"] == "prepared"
    assert burn["authoritative_inspection"]["classification"] == "prepared"
    assert burn["live_access_began"] is False
    assert burn["live_cleanup_call_count"] == 0
    assert receipt["execution_nonce"] is None
    assert (
        analyzer._validate_external_failure_receipt_document(
            receipt,
            contract=_contract_value,
            plan_path=plan_path,
            ledger_binding=binding,
        )
        == receipt
    )


def test_atomic_burn_full_commit_response_loss_is_full_and_never_touches_live_rf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-atomic-burn-full-response-loss",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["run_state_ledger"]
    real_backend = runner._global_ledger_backend()
    touched: list[str] = []

    def lose_response(stage: str) -> None:
        if stage == "after_burn_commit":
            raise OSError("synthetic full-burn response loss")

    failing_backend = global_ledger.LocalLedgerBackend(
        storage=real_backend.storage(),
        test_only_burn_fault=lose_response,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("live boundary called after uncertain full burn")

    monkeypatch.setattr(runner, "_global_ledger_backend", lambda: failing_backend)
    with pytest.raises(runner.FineFrequencyRunError, match="full-burn response loss"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            preflight_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert touched == []
    assert Path(binding["burn_guard"]["path"]).read_bytes() == b"\x01"
    assert Path(binding["burn_marker_path"]).is_file()
    receipt = _external_failure_receipt(manifest_path)
    burn = receipt["run_consumption_receipt"]
    assert burn["burn_classification"] == "full"
    assert burn["authoritative_state"] == "burn_complete"
    assert burn["authoritative_inspection"]["classification"] == "burn_complete"
    assert burn["live_access_began"] is False
    assert burn["live_cleanup_call_count"] == 0
    assert receipt["execution_nonce"] == burn["authoritative_inspection"]["execution_nonce"]
    assert (
        analyzer._validate_external_failure_receipt_document(
            receipt,
            contract=_contract_value,
            plan_path=plan_path,
            ledger_binding=binding,
        )
        == receipt
    )


def test_execution_tombstone_write_failure_after_burn_cleans_up_and_seals_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-post-burn-execution-tombstone-write",
    )
    real_write = runner._write_immutable_json
    live_calls: list[str] = []
    mute_calls: list[str] = []

    def fail_execution_tombstone(path: Path, document: Mapping[str, Any]) -> None:
        if path.name == runner.EXECUTION_TOMBSTONE_FILENAME:
            raise OSError("synthetic execution-tombstone write failure")
        real_write(path, document)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        live_calls.append("called")
        raise AssertionError("post-burn failure reached a forbidden live boundary")

    monkeypatch.setattr(runner, "_write_immutable_json", fail_execution_tombstone)
    with pytest.raises(OSError, match="execution-tombstone write"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            preflight_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=_campaign_mute(mute_calls),
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert live_calls == []
    assert mute_calls == ["mute:campaign_failure"]
    receipt = _external_failure_receipt(manifest_path)
    assert receipt["failure_phase"] == "execution_tombstone_persistence"
    assert receipt["original_error"]["error"]["type"] == "OSError"
    assert receipt["run_consumption_receipt"]["evidence_kind"] == (
        "5g8_fine_frequency_global_run_burn_v3"
    )
    burn_marker = receipt["run_consumption_receipt"]["burn_marker"]["document"]
    assert burn_marker["schema"] == 3
    assert burn_marker["marker_kind"] == "5g8_fine_frequency_global_execution_consumed_v3"
    assert burn_marker["shared_global_ledger_authority"] == (
        global_ledger.authority_receipt_binding(
            receipt["run_consumption_receipt"]["global_ledger_authority"]
        )
    )


def test_execution_tombstone_hash_failure_after_write_cleans_up_and_seals_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-post-burn-execution-tombstone-hash",
    )
    execution_path = plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    real_sha256 = runner.sha256_path
    mute_calls: list[str] = []

    def fail_execution_hash(path: Path) -> str:
        if path.expanduser().absolute() == execution_path:
            raise OSError("synthetic execution-tombstone hash failure")
        return real_sha256(path)

    monkeypatch.setattr(runner, "sha256_path", fail_execution_hash)
    with pytest.raises(OSError, match="execution-tombstone hash"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            preflight_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("preflight must not run")
            ),
            mute_boundary=_campaign_mute(mute_calls),
        )
    assert execution_path.is_file()
    assert mute_calls == ["mute:campaign_failure"]
    receipt = _external_failure_receipt(manifest_path)
    assert receipt["failure_phase"] == "execution_tombstone_hash_and_attempt_construction"
    assert any(
        item["operation"] == "execution_tombstone_hash_and_attempt_construction"
        for item in receipt["persistence_errors"]
    )


def test_running_attempt_manifest_write_failure_cleans_up_and_seals_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-post-burn-running-attempt-write",
    )
    real_write = runner.write_json_atomic
    failed_once = False
    mute_calls: list[str] = []

    def fail_first_running_write(path: Path, document: Mapping[str, Any]) -> None:
        nonlocal failed_once
        if not failed_once and document.get("status") == "running":
            failed_once = True
            raise OSError("synthetic running-attempt persistence failure")
        real_write(path, document)

    monkeypatch.setattr(runner, "write_json_atomic", fail_first_running_write)
    with pytest.raises(OSError, match="running-attempt persistence"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            preflight_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("preflight must not run")
            ),
            mute_boundary=_campaign_mute(mute_calls),
        )
    assert failed_once is True
    assert mute_calls == ["mute:campaign_failure"]
    receipt = _external_failure_receipt(manifest_path)
    assert receipt["failure_phase"] == "running_attempt_manifest_persistence"
    assert any(
        item["operation"] == "running_attempt_manifest_persistence"
        for item in receipt["persistence_errors"]
    )


def test_second_external_burn_hash_failure_is_recovered_without_live_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-post-burn-attempt-hash",
    )
    real_hash = runner.canonical_json_sha256
    burn_hash_calls = 0
    mute_calls: list[str] = []

    def fail_second_burn_hash(value: object) -> str:
        nonlocal burn_hash_calls
        if isinstance(value, Mapping) and value.get("evidence_kind") == (
            "5g8_fine_frequency_global_run_burn_v3"
        ):
            burn_hash_calls += 1
            if burn_hash_calls == 2:
                raise ValueError("synthetic running-attempt burn hash failure")
        return real_hash(value)

    monkeypatch.setattr(runner, "canonical_json_sha256", fail_second_burn_hash)
    with pytest.raises(ValueError, match="running-attempt burn hash"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            preflight_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("preflight must not run")
            ),
            mute_boundary=_campaign_mute(mute_calls),
        )
    assert burn_hash_calls >= 2
    assert mute_calls == ["mute:campaign_failure"]
    receipt = _external_failure_receipt(manifest_path)
    assert receipt["failure_phase"] == "execution_tombstone_hash_and_attempt_construction"


def test_compounded_post_burn_cleanup_and_run_root_persistence_failures_use_external_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-compounded-post-burn-recovery",
    )
    prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = prepared["run_state_ledger"]
    real_immutable_write = runner._write_immutable_json
    mute_attempts: list[str] = []

    def fail_run_root_tombstones(path: Path, document: Mapping[str, Any]) -> None:
        if path.name in {
            runner.EXECUTION_TOMBSTONE_FILENAME,
            runner.FAILURE_TOMBSTONE_FILENAME,
        }:
            raise OSError(f"synthetic {path.name} persistence failure")
        real_immutable_write(path, document)

    def fail_manifest_write(_path: Path, _document: Mapping[str, Any]) -> None:
        raise OSError("synthetic failed-manifest persistence failure")

    def fail_mute(_serial: str, purpose: str) -> dict[str, Any]:
        mute_attempts.append(purpose)
        raise OSError("synthetic cleanup mute failure")

    monkeypatch.setattr(runner, "_write_immutable_json", fail_run_root_tombstones)
    monkeypatch.setattr(runner, "write_json_atomic", fail_manifest_write)
    with pytest.raises(OSError, match="execution-started.*persistence"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            preflight_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("preflight must not run")
            ),
            mute_boundary=fail_mute,
        )
    assert mute_attempts == ["campaign_failure"]
    slot = Path(binding["failure_receipt_slot"]["path"])
    receipt = json.loads(slot.read_text(encoding="utf-8"))
    assert receipt["original_error"]["error"]["type"] == "OSError"
    assert any(
        item["operation"] == "campaign_failure_exact_mute" for item in receipt["cleanup_errors"]
    )
    failed_operations = {item["operation"] for item in receipt["persistence_errors"]}
    assert "persist_failed_manifest_before_tombstone" in failed_operations
    assert "persist_run_root_failure_tombstone" in failed_operations
    assert slot.stat().st_mode & 0o222 == 0


def test_selector_connected_early_post_burn_failure_authorizes_all_off_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-selector-early-post-burn-cleanup",
        stage="powered_selector_all_inputs_terminated",
    )
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    real_write = runner._write_immutable_json
    calls: list[str] = []

    def fail_execution_tombstone(path: Path, document: Mapping[str, Any]) -> None:
        if path.name == runner.EXECUTION_TOMBSTONE_FILENAME:
            raise OSError("synthetic early execution-tombstone failure")
        real_write(path, document)

    def target(control: Mapping[str, Any]) -> dict[str, Any]:
        calls.append("target-image")
        return _campaign_target(control, passed=True)

    def selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
        calls.append(f"selector:{purpose}")
        return _campaign_selector(control, purpose)

    monkeypatch.setattr(runner, "_write_immutable_json", fail_execution_tombstone)
    with pytest.raises(OSError, match="early execution-tombstone"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            preflight_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("preflight must not run")
            ),
            mute_boundary=_campaign_mute(calls),
            selector_image_boundary=target,
            selector_boundary=selector,
        )
    assert calls == [
        "mute:campaign_failure",
        "target-image",
        "selector:exception_cleanup_all_off",
    ]
    receipt = _external_failure_receipt(manifest_path)
    assert receipt["failure_cleanup_evidence"]["cleanup_validation_passed"] is True


def test_reused_stream_fails_whole_campaign_and_preserves_forensic_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(tmp_path, run_id="t7-duplicate")
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    mute_calls: list[str] = []

    def duplicate_stream(
        _contract: dict[str, Any], condition: dict[str, Any], _root: Path
    ) -> dict[str, Any]:
        return {"evidence": _evidence(condition, stream_id=1)}

    with pytest.raises(runner.FineFrequencyError, match="reused"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=duplicate_stream,
            mute_boundary=_campaign_mute(mute_calls),
        )
    assert mute_calls == ["mute:campaign_preflight", "mute:campaign_failure"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["accepted_condition_count"] == 0
    assert len(manifest["condition_results"]) == 1
    assert manifest["condition_results"][0]["campaign_acceptance_pending"] is True


def test_orphaned_running_manifest_is_rejected_without_replay_hardware_access(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path, run_id="t7-orphan")
    mute_calls: list[str] = []
    execution_path = manifest_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    execution_path.write_text("orphaned execution marker\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest["attempts"] = [{"status": "running"}]
    runner.write_json_atomic(manifest_path, manifest)
    hardware_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        hardware_calls.append("called")
        raise AssertionError("replay hardware boundary called")

    with pytest.raises(runner.FineFrequencyRunError, match="surviving run-derived"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={"all": True},
            condition_boundary=forbidden,
            acceptance_boundary=forbidden,
            mute_boundary=forbidden,
            selector_image_boundary=forbidden,
            selector_boundary=forbidden,
        )
    assert mute_calls == []
    assert hardware_calls == []
    unchanged = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert unchanged["status"] == "running"
    assert not (manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).exists()


def test_cli_exposes_separate_coarse_fine_execute_actions() -> None:
    options = {option for action in runner._parser()._actions for option in action.option_strings}
    assert {"--plan-coarse", "--plan-fine", "--execute"} <= options
    assert "--confirm-experimental-policy" in options
    assert "--coarse-results" in options


def test_cli_history_scan_precedes_source_native_fixture_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-cli-history-first",
    )
    (manifest_path.parent / "captures").mkdir()
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("revalidation or execution called before history rejection")

    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(runner, "_validate_current_bindings", forbidden)
    monkeypatch.setattr(runner, "_execute_prepared", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--run-id",
            str(contract["run_id"]),
            "--board-id",
            str(contract["board_id"]),
            "--serial",
            str(contract["device_identity"]["serial"]),
            "--execute",
            "--confirm-experimental-policy",
            "--confirm-no-antennas",
            "--confirm-direct-rx2-termination",
            "--confirm-tx2-terminated-muted",
            "--confirm-rx1-protected-reference",
            "--confirm-no-movement",
            "--confirm-topology-token",
            runner.TOPOLOGIES["direct_rx2_termination"]["token"],
            "--state-root",
            str(tmp_path),
        ],
    )
    assert runner.main() == 2
    assert touched == []


def test_fine_plan_accepts_coarse_results_only_after_authoritative_raw_reanalysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "coarse-run"
    run_root.mkdir()
    coarse_contract = _contract(tmp_path, run_id="coarse-a")
    campaign_binding = campaign_cross_binding_from_plan_contract(coarse_contract)
    document = {
        "schema": 1,
        "results_kind": "5g8_bidirectional_frequency_results",
        "mode": "coarse",
        "run_id": "coarse-a",
        "board_id": "board-a",
        "plan_path": str(run_root / runner.PLAN_FILENAME),
        "plan_contract_sha256": "a" * 64,
        "campaign_binding": campaign_binding,
        "campaign_binding_sha256": runner.canonical_json_sha256(campaign_binding),
        "coarse_results_binding": None,
        "refinement_selection": {
            "schema": 1,
            "selection_kind": "multiplicity_corrected_local_extrema_v1",
            "coarse_plan_contract_sha256": "a" * 64,
            "selected_centers_hz": [5_800_000_000],
        },
    }
    results_path = run_root / runner.RESULTS_FILENAME
    results_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(runner, "_reanalyze_coarse_results", lambda _root: document)
    binding, selection = runner._load_coarse_results(results_path)
    assert binding["sha256"] == runner.sha256_path(results_path)
    assert binding["campaign_binding"] == campaign_binding
    assert binding["campaign_binding_sha256"] == runner.canonical_json_sha256(campaign_binding)
    assert selection["selected_centers_hz"] == [5_800_000_000]
    monkeypatch.setattr(
        runner,
        "_reanalyze_coarse_results",
        lambda _root: {**document, "condition_count": 0},
    )
    with pytest.raises(runner.FineFrequencyRunError, match="raw-IQ reanalysis"):
        runner._load_coarse_results(results_path)


def test_live_block_ledger_uses_the_validated_abi2_schema() -> None:
    blocks = []
    for index in range(3):
        blocks.append(
            SimpleNamespace(
                samples=np.ones((2, 4), dtype=np.complex64),
                sample_count=4,
                utc_ns=1_000 + index,
                metadata_abi=2,
                stream_id=91,
                buffer_sequence=index,
                first_sample_sequence=500 + index * 4,
                last_sample_sequence_exclusive=504 + index * 4,
                metadata_flags=3,
                missing_samples_before=0,
                sample_time_realtime_start_ns=10_000 + index * 4,
                sample_time_realtime_end_ns=10_004 + index * 4,
                sample_time_monotonic_start_ns=20_000 + index * 4,
                sample_time_monotonic_end_ns=20_004 + index * 4,
                sample_time_uncertainty_ns=1,
            )
        )
    ledger = runner._block_ledger(blocks)
    summary = runner.validate_continuity_ledger(
        ledger,
        expected_total_samples=12,
        expected_samples_per_block=4,
    )
    assert summary.stream_id == 91
    assert summary.first_buffer_sequence == 0
    assert summary.last_buffer_sequence == 2


def _synthetic_capture_blocks(
    stream_id: int, *, rx2_tone_amplitude: float | None = 50.0
) -> list[Any]:
    sample_index = np.arange(runner.SAMPLES_PER_FRAME, dtype=np.float64)
    carrier = np.exp(2j * np.pi * runner.TONE_OFFSET_HZ * sample_index / runner.SAMPLE_RATE_HZ)
    rx1 = np.rint(500.0 * carrier.real) + 1j * np.rint(500.0 * carrier.imag)
    if rx2_tone_amplitude is None:
        rng = np.random.default_rng(42)
        rx2 = np.rint(2.0 * rng.standard_normal(carrier.size)) + 1j * np.rint(
            2.0 * rng.standard_normal(carrier.size)
        )
    else:
        shifted = carrier * np.exp(1j * np.deg2rad(25.0))
        rx2 = np.rint(rx2_tone_amplitude * shifted.real) + 1j * np.rint(
            rx2_tone_amplitude * shifted.imag
        )
    samples = np.asarray((rx1, rx2), dtype=np.complex64)
    duration_ns = round(runner.SAMPLES_PER_FRAME / runner.SAMPLE_RATE_HZ * 1e9)
    flags = (1 << 4) | (1 << 21)
    return [
        runner.SampleBlockV2(
            utc_ns=2_000_000_000 + index * duration_ns + duration_ns // 2,
            samples=samples.copy(),
            stream_id=stream_id,
            buffer_sequence=index,
            first_sample_sequence=index * runner.SAMPLES_PER_FRAME,
            metadata_flags=flags,
            metadata_abi=2,
            missing_samples_before=0,
            sample_time_realtime_start_ns=2_000_000_000 + index * duration_ns,
            sample_time_realtime_end_ns=2_000_000_000 + (index + 1) * duration_ns,
            sample_time_monotonic_start_ns=3_000_000_000 + index * duration_ns,
            sample_time_monotonic_end_ns=3_000_000_000 + (index + 1) * duration_ns,
            sample_time_uncertainty_ns=1_000,
        )
        for index in range(runner.FRAME_COUNT)
    ]


def _materialized_condition_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rx2_tone_detected: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    contract = _contract(tmp_path, run_id="raw-recompute")
    condition = contract["schedule"]["conditions"][0]
    capture_root = Path(contract["execution_storage"]["capture_root"])
    blocks = _synthetic_capture_blocks(
        777,
        rx2_tone_amplitude=50.0 if rx2_tone_detected else None,
    )
    identity = RadioIdentity(
        radio_id="radio-a",
        serial="serial-a",
        uri="usb:1.2.3",
        transport="iio_usb",
        model="Pluto+",
    )

    def capture_boundary(
        plan: Any,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Any,
    ) -> Any:
        assert samples_per_frame == runner.SAMPLES_PER_FRAME
        assert frame_count == runner.FRAME_COUNT
        assert kernel_buffers == runner.KERNEL_BUFFERS
        for block in blocks:
            block_consumer(block)
        return SimpleNamespace(
            identity=identity,
            settings=runner._settings(int(condition["frequency_hz"])),
            plan=plan,
            sample_count=runner.TOTAL_SAMPLES,
            frames=tuple(range(runner.FRAME_COUNT)),
            kernel_buffers=runner.KERNEL_BUFFERS,
            dds_frequency_readback_hz=(
                runner.TONE_OFFSET_HZ,
                0.0,
                -runner.TONE_OFFSET_HZ,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
            dds_enabled_readback=(True, False, True, False, False, False, False, False),
            dds_scale_readback=(
                runner.DDS_SCALE,
                0.0,
                runner.DDS_SCALE,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
            tx_gain_readback_db=runner.TX_HARDWARE_GAIN_DB,
        )

    monkeypatch.setattr(runner, "resolve_iio_uri", lambda uri, _serial: uri)
    monkeypatch.setattr(runner, "_assert_local_rpi_storage", lambda _path: None)
    monkeypatch.setattr(runner, "find_usb_sysfs_path", lambda _serial: "/sys/fake-usb")
    monkeypatch.setattr(runner, "capture_continuous_safe_dds_tone", capture_boundary)
    monkeypatch.setattr(
        runner,
        "_strict_mute",
        lambda serial, purpose: {
            "status": "passed",
            "serial": serial,
            "purpose": purpose,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        },
    )
    returned = runner._live_condition(contract, condition, capture_root)
    evidence = returned["evidence"]
    normalized = runner.normalized_observation_from_evidence(condition, evidence)
    result = {
        "plan_index": condition["plan_index"],
        "condition_id": condition["condition_id"],
        "campaign_accepted": True,
        "campaign_acceptance_pending": False,
        "evidence": evidence,
        "evidence_sha256": runner.canonical_json_sha256(evidence),
        "normalized_observation": normalized,
        "boundary_result": {
            "artifact": returned["artifact"],
            "condition_record": returned["condition_record"],
        },
    }
    return contract, condition, result, capture_root


def test_analyzer_recomputes_complex_transfer_from_raw_ci16_and_rejects_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, condition, result, capture_root = _materialized_condition_result(
        tmp_path, monkeypatch
    )
    observed = analyzer._reanalyze_condition(
        contract,
        condition,
        result,
        capture_root=capture_root,
        prior_stream_ids=set(),
        prior_artifact_sha256s=set(),
    )
    assert observed["detected"] is True
    assert observed["phasor"] is not None

    forged = json.loads(json.dumps(result))
    forged_analysis = forged["evidence"]["analysis"]
    forged_analysis["phasor"]["real"] *= 2.0
    forged_analysis["phasor"]["imag"] *= 2.0
    forged_analysis["magnitude"] *= 2.0
    record_path = Path(forged["evidence"]["artifact"]["condition_record_path"])
    forged_record = forged["boundary_result"]["condition_record"]
    forged_record["analysis"] = forged_analysis
    record_path.chmod(0o600)
    record_path.write_text(
        json.dumps(forged_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    forged["evidence"]["artifact"]["condition_record_sha256"] = runner.sha256_path(record_path)
    forged["evidence"]["artifact"]["condition_record_size_bytes"] = record_path.stat().st_size
    forged["normalized_observation"] = runner.normalized_observation_from_evidence(
        condition, forged["evidence"]
    )
    forged["evidence_sha256"] = runner.canonical_json_sha256(forged["evidence"])
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="recomputed IQ"):
        analyzer._reanalyze_condition(
            contract,
            condition,
            forged,
            capture_root=capture_root,
            prior_stream_ids=set(),
            prior_artifact_sha256s=set(),
        )


def test_raw_nondetection_is_admitted_only_as_a_phase_free_upper_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, condition, result, capture_root = _materialized_condition_result(
        tmp_path,
        monkeypatch,
        rx2_tone_detected=False,
    )
    analysis = result["evidence"]["analysis"]
    assert analysis["rx2_tone_detected"] is False
    assert analysis["phasor"] is None
    assert analysis["phase_deg"] is None
    assert analysis["amplitude_upper_bound_ratio"] > 0.0
    assert analysis["coherent_transfer"]["rx2_over_rx1"]["phasor"] is None
    assert analysis["coherent_transfer"]["rx2_over_rx1"]["phase_deg"] is None
    observed = analyzer._reanalyze_condition(
        contract,
        condition,
        result,
        capture_root=capture_root,
        prior_stream_ids=set(),
        prior_artifact_sha256s=set(),
    )
    assert observed["detected"] is False
    assert observed["phasor"] is None
    assert observed["nondetection_is_phase_free"] is True


def test_analyzer_rejects_prepared_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, _plan_path, _manifest_path = _prepared(tmp_path, run_id="t7-not-complete")
    monkeypatch.setattr(analyzer, "_verify_analyzer_source", lambda _contract: None)
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="not complete"):
        analyzer.analyze_campaign(Path(contract["execution_storage"]["run_root"]))


def _schema_only_complete_manifest(
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    timestamp = manifest["created_at"]
    count = int(contract["storage"]["condition_count"])
    external_burn = {"synthetic": "schema-only"}
    preflight_mute = {"synthetic": "schema-only-preflight"}
    final_cleanup = {"synthetic": "schema-only-cleanup"}
    manifest.update(
        {
            "status": "complete",
            "updated_at": timestamp,
            "attempts": [
                {
                    "started_at": timestamp,
                    "status": "complete",
                    "confirmations": {},
                    "execution_tombstone": {},
                    "external_run_burn": external_burn,
                    "external_run_burn_sha256": runner.canonical_json_sha256(external_burn),
                    "completed_condition_count": count,
                    "campaign_preflight_exact_mute": preflight_mute,
                    "campaign_preflight_exact_mute_sha256": runner.canonical_json_sha256(
                        preflight_mute
                    ),
                    "selector_connected_preflight": None,
                    "selector_connected_preflight_sha256": None,
                    "campaign_final_cleanup": final_cleanup,
                    "campaign_final_cleanup_sha256": runner.canonical_json_sha256(final_cleanup),
                    "error": None,
                    "completed_at": timestamp,
                }
            ],
            "condition_results": [
                {
                    "plan_index": index,
                    "condition_id": f"schema-only-{index}",
                    "evidence": {},
                    "evidence_sha256": "a" * 64,
                    "normalized_observation": {},
                    "boundary_result": {},
                    "campaign_acceptance_pending": False,
                    "campaign_accepted": True,
                }
                for index in range(count)
            ],
            "accepted_condition_count": count,
            "campaign_accepted": True,
            "error": None,
        }
    )
    return envelope, manifest


def test_analyzer_complete_manifest_schema_is_exact_and_timestamp_bound(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-complete-manifest-exact-schema",
    )
    envelope, valid = _schema_only_complete_manifest(contract, plan_path, manifest_path)
    attempt, results = analyzer._validate_complete_manifest_schema(
        valid,
        contract=contract,
        envelope=envelope,
        plan_path=plan_path,
    )
    assert attempt["status"] == "complete"
    assert len(results) == contract["storage"]["condition_count"]

    forged_documents: list[dict[str, Any]] = []
    missing = json.loads(json.dumps(valid))
    del missing["attempts"][0]["completed_at"]
    forged_documents.append(missing)
    extra = json.loads(json.dumps(valid))
    extra["unexpected"] = None
    forged_documents.append(extra)
    wrong_hash = json.loads(json.dumps(valid))
    wrong_hash["attempts"][0]["external_run_burn_sha256"] = "0" * 64
    forged_documents.append(wrong_hash)
    retyped_count = json.loads(json.dumps(valid))
    retyped_count["accepted_condition_count"] = True
    forged_documents.append(retyped_count)
    forged_timestamp = json.loads(json.dumps(valid))
    forged_timestamp["updated_at"] = str(forged_timestamp["updated_at"]).replace("+00:00", "Z")
    forged_documents.append(forged_timestamp)
    extra_result_field = json.loads(json.dumps(valid))
    extra_result_field["condition_results"][0]["unexpected"] = None
    forged_documents.append(extra_result_field)

    for forged in forged_documents:
        with pytest.raises(analyzer.FineFrequencyAnalysisError):
            analyzer._validate_complete_manifest_schema(
                forged,
                contract=contract,
                envelope=envelope,
                plan_path=plan_path,
            )


def test_analyzer_reconstructs_exact_prepared_manifest_wire_bytes(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-prepared-manifest-reconstruction",
    )
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
    external_burn = runner._burn_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=prepared,
    )
    completed = json.loads(json.dumps(prepared))
    started_at = runner._now()
    completed["attempts"] = [{"started_at": started_at}]
    analyzer._verify_prepared_manifest_reconstruction(
        contract=contract,
        envelope=envelope,
        plan_path=plan_path,
        manifest=completed,
        external_burn=external_burn,
    )

    prepared_document = runner._prepared_manifest_document(
        plan_path,
        envelope,
        ledger_binding=prepared["run_state_ledger"],
        created_at=prepared["created_at"],
    )
    compact_wire = json.dumps(
        prepared_document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    for field, forged_value in (
        ("prepared_manifest_sha256", "0" * 64),
        ("prepared_manifest_size_bytes", 1),
    ):
        forged_burn = json.loads(json.dumps(external_burn))
        forged_burn["reservation"]["document"][field] = forged_value
        forged_burn["burn_marker"]["document"][field] = forged_value
        with pytest.raises(analyzer.FineFrequencyAnalysisError, match="reconstructed"):
            analyzer._verify_prepared_manifest_reconstruction(
                contract=contract,
                envelope=envelope,
                plan_path=plan_path,
                manifest=completed,
                external_burn=forged_burn,
            )

    compact_forgery = json.loads(json.dumps(external_burn))
    for document in (
        compact_forgery["reservation"]["document"],
        compact_forgery["burn_marker"]["document"],
    ):
        document["prepared_manifest_sha256"] = hashlib.sha256(compact_wire).hexdigest()
        document["prepared_manifest_size_bytes"] = len(compact_wire)
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="reconstructed"):
        analyzer._verify_prepared_manifest_reconstruction(
            contract=contract,
            envelope=envelope,
            plan_path=plan_path,
            manifest=completed,
            external_burn=compact_forgery,
        )

    timestamp_forgery = json.loads(json.dumps(completed))
    timestamp_forgery["attempts"][0]["started_at"] = timestamp_forgery["created_at"]
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="reconstructed"):
        analyzer._verify_prepared_manifest_reconstruction(
            contract=contract,
            envelope=envelope,
            plan_path=plan_path,
            manifest=timestamp_forgery,
            external_burn=external_burn,
        )


def test_analyzer_rejects_any_failure_tombstone(tmp_path: Path) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path, run_id="t7-failed")
    run_root = manifest_path.parent
    failure_path = run_root / analyzer.FAILURE_TOMBSTONE_FILENAME
    failure_path.write_text("{}\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="failed campaign"):
        analyzer._verify_execution_tombstone(
            run_root,
            plan_path=plan_path,
            contract=contract,
            manifest=manifest,
        )


def _campaign_mute(calls: list[str]) -> Any:
    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(f"mute:{purpose}")
        return {
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        }

    return mute


def _campaign_target(control: Mapping[str, Any], *, passed: bool) -> dict[str, Any]:
    target = control["target_image_admission_contract"]
    flash = control["selector_flash_evidence"]
    config = control["openocd_config"]
    failure_halt = (
        None
        if passed
        else {
            "schema": 1,
            "evidence_kind": "selector_target_best_effort_halt_v1",
            "purpose": "image_admission_failure_cleanup",
            "status": "passed",
            "openocd_config_path": config["path"],
            "openocd_config_sha256": config["file_sha256"],
            "command": "init; halt; shutdown",
            "returncode": 0,
            "target_halted": True,
            "mailbox_access_performed": False,
            "error": None,
        }
    )
    return {
        "schema": 1,
        "evidence_kind": "contemporaneous_full_bin_extent_and_uid_admission_v1",
        "status": "passed" if passed else "failed",
        "selector_flash_evidence_sha256": flash["sha256"],
        "flash_base_address": target["flash_base_address"],
        "byte_count": target["firmware_bin_size_bytes"],
        "expected_bin_sha256": target["firmware_bin_sha256"],
        "observed_target_sha256": target["firmware_bin_sha256"] if passed else "0" * 64,
        "expected_board_id": flash["board_id"],
        "observed_uid": flash["board_id"].removeprefix("stm32c011-"),
        "exact_bin_and_uid_match": passed,
        "reviewed_image_started_only_after_exact_match": passed,
        "target_may_have_started_before_failure_halt": False,
        "failure_halt_required": not passed,
        "failure_halt": failure_halt,
        "target_kept_halted_on_failure": not passed,
        "mailbox_access_performed": False,
        "error": None if passed else {"type": "SyntheticMismatch", "message": "mismatch"},
    }


def _campaign_selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    code = int(control["command"]["code"])
    snapshot = {
        "applied_code": code,
        "command_code": code,
        "command_lease_ms": 0,
        "command_sequence": 7,
        "acknowledged_sequence": 7,
        "command_valid": True,
        "lease_active": False,
        "remaining_lease_ms": 0,
        "guard_active": False,
        "invalid_command": False,
    }
    read_only = purpose == "initial_state_before_command"
    return {
        "schema": 1,
        "evidence_kind": "static_selector_all_off_mailbox_readback",
        "purpose": purpose,
        "status": "passed",
        "all_off_code": code,
        "lease_ms": 0,
        "operation": "read_only" if read_only else "command_all_off",
        "command_was_issued": not read_only,
        "pre_command_was_all_off": None if read_only else True,
        "pre_command": None if read_only else dict(snapshot),
        "commanded": None if read_only else dict(snapshot),
        "readback": dict(snapshot),
        "error": None,
    }


def _complete_campaign_safety_attempt(contract: Mapping[str, Any]) -> dict[str, Any]:
    serial = str(contract["device_identity"]["serial"])
    preflight_mute = _campaign_mute([])(serial, "campaign_preflight")
    fixture = contract["fixture_identity"]
    selector_control = fixture["selector_control"]
    attempt: dict[str, Any] = {
        "campaign_preflight_exact_mute": preflight_mute,
        "campaign_preflight_exact_mute_sha256": runner.canonical_json_sha256(preflight_mute),
        "selector_connected_preflight": None,
        "selector_connected_preflight_sha256": None,
    }
    target_image: dict[str, Any] | None = None
    final_selector: dict[str, Any] | None = None
    if fixture["selector_connected"] is True:
        assert isinstance(selector_control, Mapping)
        target_image = _campaign_target(selector_control, passed=True)
        first_selector = _campaign_selector(
            selector_control,
            "initial_state_before_command",
        )
        required_order = [
            "exact_pluto_mute",
            "target_full_bin_uid_admission",
            "first_mailbox_operation",
        ]
        selector_preflight = {
            "exact_pluto_mute": preflight_mute,
            "target_full_bin_uid_admission": target_image,
            "first_mailbox_operation": first_selector,
            "required_order": required_order,
            "observed_order": required_order,
            "passed": True,
        }
        attempt["selector_connected_preflight"] = selector_preflight
        attempt["selector_connected_preflight_sha256"] = runner.canonical_json_sha256(
            selector_preflight
        )
        final_selector = _campaign_selector(
            selector_control,
            "final_cleanup_all_off",
        )
    final_mute = _campaign_mute([])(serial, "campaign_final")
    final_cleanup = runner._validated_campaign_cleanup(
        exact_mute=final_mute,
        serial=serial,
        exact_mute_purpose="campaign_final",
        selector_image_admission=target_image,
        selector_all_off=final_selector,
        selector_control=selector_control,
        selector_purpose="final_cleanup_all_off",
    )
    attempt["campaign_final_cleanup"] = final_cleanup
    attempt["campaign_final_cleanup_sha256"] = runner.canonical_json_sha256(final_cleanup)
    return attempt


@pytest.mark.parametrize(
    "stage",
    ("direct_rx2_termination", "powered_selector_all_inputs_terminated"),
)
def test_analyzer_accepts_revalidated_campaign_safety_evidence(
    tmp_path: Path,
    stage: str,
) -> None:
    contract = _contract(tmp_path, run_id=f"t7-safety-green-{stage}", stage=stage)
    analyzer._verify_campaign_safety_evidence(
        contract,
        _complete_campaign_safety_attempt(contract),
    )


def test_authoritative_execution_admission_invokes_campaign_safety_revalidation(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-authoritative-safety-admission",
    )
    run_root = manifest_path.parent
    prepared_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    external_burn = runner._burn_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=prepared_manifest,
    )
    execution_path = run_root / runner.EXECUTION_TOMBSTONE_FILENAME
    execution = runner._execution_tombstone(
        execution_path,
        contract,
        plan_path,
        external_burn=external_burn,
    )
    confirmations = runner._validate_confirmations(
        _confirmation_args(contract["fixture_identity"]["topology_token"]),
        contract,
    )
    attempt = {
        **_complete_campaign_safety_attempt(contract),
        "status": "complete",
        "error": None,
        "started_at": execution["created_at"],
        "completed_at": "2026-08-31T00:00:00+00:00",
        "completed_condition_count": contract["storage"]["condition_count"],
        "confirmations": confirmations,
        "execution_tombstone": {
            "path": str(execution_path),
            "sha256": runner.sha256_path(execution_path),
            "document": execution,
        },
        "external_run_burn": external_burn,
        "external_run_burn_sha256": runner.canonical_json_sha256(external_burn),
    }
    manifest = {
        "attempts": [attempt],
        "failure_tombstone": None,
        "run_state_ledger": prepared_manifest["run_state_ledger"],
    }
    admitted_burn = analyzer._verify_execution_tombstone(
        run_root,
        plan_path=plan_path,
        contract=contract,
        manifest=manifest,
    )
    assert admitted_burn == external_burn

    forged = json.loads(json.dumps(manifest))
    forged["attempts"][0]["campaign_final_cleanup"]["exact_pluto_mute"]["purpose"] = "forged_final"
    forged["attempts"][0]["campaign_final_cleanup_sha256"] = runner.canonical_json_sha256(
        forged["attempts"][0]["campaign_final_cleanup"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="final exact mute"):
        analyzer._verify_execution_tombstone(
            run_root,
            plan_path=plan_path,
            contract=contract,
            manifest=forged,
        )


def test_analyzer_rejects_forged_preflight_mute_order_and_selector_evidence(
    tmp_path: Path,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-safety-red-preflight",
        stage="powered_selector_all_inputs_terminated",
    )
    valid = _complete_campaign_safety_attempt(contract)

    missing_mute = json.loads(json.dumps(valid))
    missing_mute["campaign_preflight_exact_mute"] = None
    missing_mute["campaign_preflight_exact_mute_sha256"] = None
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="preflight exact"):
        analyzer._verify_campaign_safety_evidence(contract, missing_mute)

    missing_selector_preflight = json.loads(json.dumps(valid))
    missing_selector_preflight["selector_connected_preflight"] = None
    missing_selector_preflight["selector_connected_preflight_sha256"] = None
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="order evidence"):
        analyzer._verify_campaign_safety_evidence(contract, missing_selector_preflight)

    forged_mute = json.loads(json.dumps(valid))
    forged_mute["campaign_preflight_exact_mute"]["purpose"] = "forged_preflight"
    forged_mute["campaign_preflight_exact_mute_sha256"] = runner.canonical_json_sha256(
        forged_mute["campaign_preflight_exact_mute"]
    )
    forged_mute["selector_connected_preflight"]["exact_pluto_mute"] = forged_mute[
        "campaign_preflight_exact_mute"
    ]
    forged_mute["selector_connected_preflight_sha256"] = runner.canonical_json_sha256(
        forged_mute["selector_connected_preflight"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="preflight exact"):
        analyzer._verify_campaign_safety_evidence(contract, forged_mute)

    forged_order = json.loads(json.dumps(valid))
    forged_order["selector_connected_preflight"]["observed_order"] = [
        "exact_pluto_mute",
        "first_mailbox_operation",
        "target_full_bin_uid_admission",
    ]
    forged_order["selector_connected_preflight_sha256"] = runner.canonical_json_sha256(
        forged_order["selector_connected_preflight"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="order evidence"):
        analyzer._verify_campaign_safety_evidence(contract, forged_order)

    forged_target = json.loads(json.dumps(valid))
    forged_target["selector_connected_preflight"]["target_full_bin_uid_admission"][
        "observed_target_sha256"
    ] = "0" * 64
    forged_target["selector_connected_preflight_sha256"] = runner.canonical_json_sha256(
        forged_target["selector_connected_preflight"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="BIN/UID"):
        analyzer._verify_campaign_safety_evidence(contract, forged_target)

    forged_mailbox = json.loads(json.dumps(valid))
    forged_mailbox["selector_connected_preflight"]["first_mailbox_operation"]["readback"][
        "applied_code"
    ] = 1
    forged_mailbox["selector_connected_preflight_sha256"] = runner.canonical_json_sha256(
        forged_mailbox["selector_connected_preflight"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="first mailbox"):
        analyzer._verify_campaign_safety_evidence(contract, forged_mailbox)


def test_analyzer_rejects_forged_final_mute_and_selector_cleanup(
    tmp_path: Path,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-safety-red-final",
        stage="powered_selector_all_inputs_terminated",
    )
    valid = _complete_campaign_safety_attempt(contract)

    missing_cleanup = json.loads(json.dumps(valid))
    missing_cleanup["campaign_final_cleanup"] = None
    missing_cleanup["campaign_final_cleanup_sha256"] = None
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="final cleanup binding"):
        analyzer._verify_campaign_safety_evidence(contract, missing_cleanup)

    forged_mute = json.loads(json.dumps(valid))
    forged_mute["campaign_final_cleanup"]["exact_pluto_mute"]["purpose"] = "forged_final"
    forged_mute["campaign_final_cleanup_sha256"] = runner.canonical_json_sha256(
        forged_mute["campaign_final_cleanup"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="final exact mute"):
        analyzer._verify_campaign_safety_evidence(contract, forged_mute)

    forged_selector = json.loads(json.dumps(valid))
    forged_selector["campaign_final_cleanup"]["selector_all_off"]["readback"]["applied_code"] = 1
    forged_selector["campaign_final_cleanup_sha256"] = runner.canonical_json_sha256(
        forged_selector["campaign_final_cleanup"]
    )
    with pytest.raises(analyzer.FineFrequencyAnalysisError, match="selector ALL_OFF"):
        analyzer._verify_campaign_safety_evidence(contract, forged_selector)


def test_image_admission_authorizes_cleanup_before_target_evidence_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-image-admitted-write-failure",
        stage="powered_selector_all_inputs_terminated",
    )
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    real_write = runner.write_json_atomic
    persistence_failure_raised = False
    calls: list[str] = []

    def target(control: Mapping[str, Any]) -> dict[str, Any]:
        calls.append("target-image")
        return _campaign_target(control, passed=True)

    def selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
        calls.append(f"selector:{purpose}")
        return _campaign_selector(control, purpose)

    def fail_first_target_evidence_write(path: Path, document: Mapping[str, Any]) -> None:
        nonlocal persistence_failure_raised
        attempts = document.get("attempts")
        preflight: object = None
        if isinstance(attempts, list) and attempts and isinstance(attempts[0], Mapping):
            preflight = attempts[0].get("selector_connected_preflight")
        if (
            not persistence_failure_raised
            and isinstance(preflight, Mapping)
            and preflight.get("target_full_bin_uid_admission") is not None
            and preflight.get("first_mailbox_operation") is None
        ):
            persistence_failure_raised = True
            raise OSError("synthetic target evidence persistence failure")
        real_write(path, document)

    monkeypatch.setattr(runner, "write_json_atomic", fail_first_target_evidence_write)
    with pytest.raises(OSError, match="target evidence persistence"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            mute_boundary=_campaign_mute(calls),
            selector_image_boundary=target,
            selector_boundary=selector,
        )

    assert persistence_failure_raised is True
    assert calls == [
        "mute:campaign_preflight",
        "target-image",
        "mute:campaign_failure",
        "selector:exception_cleanup_all_off",
    ]
    failure_path = run_root / runner.FAILURE_TOMBSTONE_FILENAME
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    cleanup = failure["failure_cleanup_evidence"]
    assert cleanup["selector_image_admission_validation_passed"] is True
    assert cleanup["selector_write_permitted_by_image_admission"] is True
    assert cleanup["selector_all_off_validation_passed"] is True
    assert cleanup["cleanup_validation_passed"] is True
    assert failure["failure_cleanup_evidence_sha256"] == runner.canonical_json_sha256(cleanup)
    assert failure_path.stat().st_mode & 0o222 == 0
    failed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = failed_manifest["attempts"][0]
    assert attempt["campaign_final_cleanup"] == cleanup
    assert attempt["campaign_final_cleanup_sha256"] == runner.canonical_json_sha256(cleanup)


def test_failure_tombstone_records_invalid_exact_mute_without_claiming_cleanup_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-invalid-failure-mute",
    )
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    calls: list[str] = []
    passing_mute = _campaign_mute(calls)

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        evidence = passing_mute(serial, purpose)
        if purpose == "campaign_failure":
            evidence["status"] = "failed"
            evidence["error"] = {
                "type": "SyntheticFailureMuteError",
                "message": "failure mute did not read back",
            }
        return evidence

    def identity_failure(*_args: Any) -> dict[str, Any]:
        raise runner.FineFrequencyRunError("synthetic live identity failure")

    with pytest.raises(runner.FineFrequencyRunError, match="live identity"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=identity_failure,
            mute_boundary=mute,
        )

    failure = json.loads(
        (manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    cleanup = failure["failure_cleanup_evidence"]
    assert cleanup["exact_pluto_mute_validation_passed"] is False
    assert cleanup["selector_all_off_validation_passed"] is None
    assert cleanup["cleanup_validation_passed"] is False
    assert failure["failure_cleanup_evidence_sha256"] == runner.canonical_json_sha256(cleanup)


def test_failure_tombstone_records_invalid_selector_cleanup_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-invalid-failure-selector",
        stage="powered_selector_all_inputs_terminated",
    )
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)

    def target(control: Mapping[str, Any]) -> dict[str, Any]:
        return _campaign_target(control, passed=True)

    def selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
        evidence = _campaign_selector(control, purpose)
        if purpose == "exception_cleanup_all_off":
            evidence["readback"]["applied_code"] = 1
        return evidence

    with pytest.raises(runner.FineFrequencyRunError, match="synthetic condition"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                runner.FineFrequencyRunError("synthetic condition failure")
            ),
            mute_boundary=_campaign_mute([]),
            selector_image_boundary=target,
            selector_boundary=selector,
        )

    failure = json.loads((run_root / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8"))
    cleanup = failure["failure_cleanup_evidence"]
    assert cleanup["exact_pluto_mute_validation_passed"] is True
    assert cleanup["selector_image_admission_validation_passed"] is True
    assert cleanup["selector_all_off_validation_passed"] is False
    assert cleanup["cleanup_validation_passed"] is False
    assert failure["failure_cleanup_evidence_sha256"] == runner.canonical_json_sha256(cleanup)


def test_selector_disconnected_preflight_mute_blocks_live_iio_identity_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-disconnected-preflight-mute-failure",
    )
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    calls: list[str] = []
    passing_mute = _campaign_mute(calls)

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        evidence = passing_mute(serial, purpose)
        if purpose == "campaign_preflight":
            evidence["status"] = "failed"
            evidence["error"] = {
                "type": "SyntheticMuteFailure",
                "message": "preflight mute failed",
            }
        return evidence

    def live_identity(*_args: Any) -> dict[str, Any]:
        calls.append("live-iio-uri-identity")
        raise AssertionError("live IIO identity must remain untouched")

    with pytest.raises(runner.FineFrequencyRunError, match="exact Pluto mute failed"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=live_identity,
            mute_boundary=mute,
        )

    assert calls == ["mute:campaign_preflight", "mute:campaign_failure"]
    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["attempts"][0]
    assert attempt["campaign_preflight_exact_mute"]["status"] == "failed"
    assert attempt["selector_connected_preflight"] is None
    assert attempt["campaign_final_cleanup"]["exact_pluto_mute"]["status"] == "passed"
    assert attempt["campaign_final_cleanup"]["selector_all_off"] is None
    assert attempt["campaign_final_cleanup"]["selector_write_permitted_by_image_admission"] is False


def test_selector_disconnected_identity_failure_is_bracketed_by_exact_mutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contract_value, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-disconnected-identity-failure",
    )
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    calls: list[str] = []

    def live_identity(*_args: Any) -> dict[str, Any]:
        calls.append("live-iio-uri-identity")
        raise runner.FineFrequencyRunError("synthetic identity failure")

    with pytest.raises(runner.FineFrequencyRunError, match="synthetic identity failure"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=live_identity,
            mute_boundary=_campaign_mute(calls),
        )

    assert calls == [
        "mute:campaign_preflight",
        "live-iio-uri-identity",
        "mute:campaign_failure",
    ]
    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["attempts"][0]
    assert attempt["campaign_preflight_exact_mute"]["purpose"] == "campaign_preflight"
    cleanup = attempt["campaign_final_cleanup"]
    assert cleanup["exact_pluto_mute"]["purpose"] == "campaign_failure"
    assert cleanup["selector_all_off"] is None
    assert cleanup["selector_write_permitted_by_image_admission"] is False


def test_selector_disconnected_success_has_preflight_and_final_exact_mutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, plan_path, manifest_path = _prepared(
        tmp_path,
        run_id="t7-disconnected-success-mutes",
    )
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: 100_000_000_000)
    monkeypatch.setattr(runner, "write_json_atomic", lambda *_args: None)
    calls: list[str] = []

    def condition(
        _contract: Mapping[str, Any],
        planned: dict[str, Any],
        _root: Path,
    ) -> dict[str, Any]:
        calls.append(f"condition:{planned['plan_index']}")
        return {"evidence": _evidence(planned, stream_id=1000 + planned["plan_index"])}

    manifest = runner._execute_prepared(
        plan_path=plan_path,
        manifest_path=manifest_path,
        confirmations={},
        condition_boundary=condition,
        mute_boundary=_campaign_mute(calls),
    )

    assert calls[0] == "mute:campaign_preflight"
    assert calls[-1] == "mute:campaign_final"
    assert calls[1:-1] == [
        f"condition:{condition['plan_index']}" for condition in contract["schedule"]["conditions"]
    ]
    attempt = manifest["attempts"][0]
    assert attempt["campaign_preflight_exact_mute"]["status"] == "passed"
    assert attempt["campaign_final_cleanup"]["exact_pluto_mute"]["status"] == "passed"
    assert attempt["campaign_final_cleanup"]["selector_all_off"] is None
    assert attempt["campaign_final_cleanup"]["selector_write_permitted_by_image_admission"] is False


def test_selector_connected_image_mismatch_never_touches_mailbox_and_burns_run(
    tmp_path: Path,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-selector-image-mismatch",
        stage="powered_selector_all_inputs_terminated",
    )
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    calls: list[str] = []
    selector_calls: list[str] = []

    def target(control: Mapping[str, Any]) -> dict[str, Any]:
        calls.append("target-image")
        return _campaign_target(control, passed=False)

    def selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
        selector_calls.append(purpose)
        return _campaign_selector(control, purpose)

    with pytest.raises(runner.FineFrequencyRunError, match="full-BIN extent or UID"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=lambda *_args: (_ for _ in ()).throw(
                AssertionError("condition must not run")
            ),
            mute_boundary=_campaign_mute(calls),
            selector_image_boundary=target,
            selector_boundary=selector,
        )

    assert calls == ["mute:campaign_preflight", "target-image", "mute:campaign_failure"]
    assert selector_calls == []
    failed_attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["attempts"][0]
    assert failed_attempt["campaign_final_cleanup"]["selector_all_off"] is None
    assert (
        failed_attempt["campaign_final_cleanup"]["selector_write_permitted_by_image_admission"]
        is False
    )
    execution = run_root / runner.EXECUTION_TOMBSTONE_FILENAME
    assert execution.is_file()
    assert not execution.stat().st_mode & 0o222
    calls.clear()
    with pytest.raises(runner.FineFrequencyRunError, match="burned"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            mute_boundary=_campaign_mute(calls),
            selector_image_boundary=target,
            selector_boundary=selector,
        )
    assert calls == []
    assert selector_calls == []


def test_selector_connected_first_hardware_order_is_mute_image_mailbox(
    tmp_path: Path,
) -> None:
    contract = _contract(
        tmp_path,
        run_id="t7-selector-order",
        stage="powered_selector_all_inputs_terminated",
    )
    run_root = Path(contract["execution_storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract)
    calls: list[str] = []

    def target(control: Mapping[str, Any]) -> dict[str, Any]:
        calls.append("target-image")
        return _campaign_target(control, passed=True)

    def selector(control: Mapping[str, Any], purpose: str) -> dict[str, Any]:
        calls.append(f"selector:{purpose}")
        return _campaign_selector(control, purpose)

    def stop_after_preflight(*_args: Any) -> dict[str, Any]:
        calls.append("condition")
        raise runner.FineFrequencyRunError("synthetic stop")

    with pytest.raises(runner.FineFrequencyRunError, match="synthetic stop"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations={},
            condition_boundary=stop_after_preflight,
            mute_boundary=_campaign_mute(calls),
            selector_image_boundary=target,
            selector_boundary=selector,
        )

    assert calls[:3] == [
        "mute:campaign_preflight",
        "target-image",
        "selector:initial_state_before_command",
    ]
    assert "condition" in calls
    assert calls[-2:] == ["mute:campaign_failure", "selector:exception_cleanup_all_off"]
