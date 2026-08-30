from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from t8_production_helpers import build_x_plan_contract, sealed_bench_selector
from test_fixture_v2 import _chain as _production_fixture_chain

from smateway.intervention_support import (
    intervention_repeat_from_document,
    qualify_intervention_support,
)
from smateway.selected_state_qualification import (
    DEVICE_IDENTITY_KIND,
    FULL_CONDUCTED_STAGE,
    SelectedStateQualificationError,
    validate_device_identity_evidence,
    validate_intervention_change_plan,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_5g8_selected_state_inputs.py"
SPEC = importlib.util.spec_from_file_location("prepare_5g8_selected_state_inputs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = producer
SPEC.loader.exec_module(producer)

SERIAL = "104000b29905000e17000800065934759d"
URI = "usb:1.2.3"
BOARD = "stm32c011-4c0055000950313950363920"
CAMPAIGN = "5p8-debug-r1"
CONTRACT = "selector-current-limit-intervention-r01"
DOCS = Path(__file__).resolve().parents[1] / "docs/5g8_root_cause_analysis"


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "sysfs" / "1-2.3"
    root.mkdir(parents=True)
    values = {
        "serial": SERIAL,
        "idVendor": "0456",
        "idProduct": "b673",
        "manufacturer": "Analog Devices Inc.",
        "product": "PlutoSDR",
    }
    for name, value in values.items():
        (root / name).write_text(value + "\n", encoding="utf-8")
    return root


def _resolution(sysfs: Path) -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed",
        "serial": SERIAL,
        "requested_uri": URI,
        "resolved_uri": URI,
        "exact_uri_match": True,
        "sysfs_path": str(sysfs),
        "scan_mutates_radio_state": False,
        "started_at": "2026-08-30T10:00:00+00:00",
        "completed_at": "2026-08-30T10:00:01+00:00",
        "error": None,
    }


def _context() -> dict[str, Any]:
    return {
        "serial": SERIAL,
        "model": "PlutoSDR",
        "firmware_version": "v0.40-plutoplus-spf-tandem-agc-v7",
        "kernel_version": "6.1.0",
        "context_uri": URI,
        "phy_model": "ad9361",
        "buffer_metadata_abi": 2,
        "rx_scan_channels": ["voltage0", "voltage1", "voltage2", "voltage3"],
    }


def test_device_identity_producer_derives_read_only_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysfs = _sysfs(tmp_path)
    native = {"schema": 1, "library": "/usr/local/lib/libiio.so.0"}
    monkeypatch.setattr(producer, "attestation_sha256", producer.canonical_sha256)
    output = producer.produce_device_identity(
        serial=SERIAL,
        uri=URI,
        output=tmp_path / "device.json",
        identity_boundary=lambda _serial, _uri: _resolution(sysfs),
        context_boundary=lambda _uri: _context(),
        native_boundary=lambda: native,
        now=lambda: "2026-08-30T10:00:02+00:00",
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    evidence = validate_device_identity_evidence(document)
    assert document["evidence_kind"] == DEVICE_IDENTITY_KIND
    assert evidence.serial == SERIAL
    assert evidence.firmware_version.endswith("tandem-agc-v7")
    assert output.stat().st_mode & 0o222 == 0


def test_device_identity_rejects_self_asserted_or_mismatched_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysfs = _sysfs(tmp_path)
    monkeypatch.setattr(producer, "attestation_sha256", producer.canonical_sha256)
    output = producer.produce_device_identity(
        serial=SERIAL,
        uri=URI,
        output=tmp_path / "device.json",
        identity_boundary=lambda _serial, _uri: _resolution(sysfs),
        context_boundary=lambda _uri: _context(),
        native_boundary=lambda: {"schema": 1, "library": "/usr/local/lib/libiio.so.0"},
        now=lambda: "2026-08-30T10:00:02+00:00",
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    document["iio_context_facts"]["firmware_version"] = "self-asserted-firmware"
    with pytest.raises(SelectedStateQualificationError, match="hash is inconsistent"):
        validate_device_identity_evidence(document)


def _fixture(
    path: Path,
    supply_current_limit_a: float,
    *,
    selector_binding: dict[str, Any],
    selector_control: dict[str, Any],
) -> dict[str, Any]:
    """Build one complete production A -> B -> C -> E fixture chain."""

    return _production_fixture_chain(
        path,
        supply_current_limit_a=supply_current_limit_a,
        run_prefix=path.name,
        selector_binding=selector_binding,
        selector_control=selector_control,
    )


def _fixture_pair(
    tmp_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    Path,
    Path,
    dict[str, Any],
    dict[str, Any],
]:
    selector_binding, selector_control = sealed_bench_selector(tmp_path / "selector-seal")
    before_chain = _fixture(
        tmp_path / "before-chain",
        0.4,
        selector_binding=selector_binding,
        selector_control=selector_control,
    )
    after_chain = _fixture(
        tmp_path / "after-chain",
        0.5,
        selector_binding=selector_binding,
        selector_control=selector_control,
    )
    before_manifest = Path(before_chain[FULL_CONDUCTED_STAGE]["manifest"])
    after_manifest = Path(after_chain[FULL_CONDUCTED_STAGE]["manifest"])
    before_binding = producer.full_simultaneous_fixture_binding_from_manifest(before_manifest)
    after_binding = producer.full_simultaneous_fixture_binding_from_manifest(after_manifest)
    return (
        before_chain,
        after_chain,
        before_manifest,
        after_manifest,
        before_binding,
        after_binding,
    )


def _x_plan(
    path: Path,
    *,
    role: str,
    before_chain: dict[str, Any],
    after_chain: dict[str, Any],
    before_binding: dict[str, Any],
    after_binding: dict[str, Any],
    implicated_stage: str = "powered_selector_all_inputs_terminated",
) -> Path:
    topology_stage = implicated_stage if role.startswith("boundary") else FULL_CONDUCTED_STAGE
    state_chain = before_chain if role.endswith("baseline") else after_chain
    fixture_evidence = copy.deepcopy(state_chain[topology_stage]["evidence"])
    selector_binding = copy.deepcopy(state_chain["selector"])
    capture_fixture = before_binding if role.endswith("baseline") else after_binding
    role_index = producer.X_RUN_ROLES.index(role)
    selector_control = (
        state_chain["selector_control"]
        if topology_stage in producer.leakage_runner.SELECTOR_CONNECTED_STAGES
        else None
    )
    contract = build_x_plan_contract(
        role=role,
        contract_id=CONTRACT,
        implicated_stage=implicated_stage,
        acquisition_index=41 + role_index,
        freshness_epoch_id="fixture-epoch-17",
        fixture_evidence=fixture_evidence,
        capture_fixture=capture_fixture,
        installed_after_fixture=after_binding,
        selector_binding=selector_binding,
        selector_control=selector_control,
        serial=SERIAL,
    )
    return _write_json(path, producer.leakage_runner._plan_envelope(contract))


def test_intervention_plan_is_derived_from_fixture_bytes_and_prebound_runs(tmp_path: Path) -> None:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
        )
        for role in producer.X_RUN_ROLES
    }
    output = producer.produce_intervention_plan(
        contract_id=CONTRACT,
        campaign_id=CAMPAIGN,
        board_id=BOARD,
        before_fixture_manifest=before_manifest,
        after_fixture_manifest=after_manifest,
        component_id="selector-v5-01",
        property_path="/stage_delta/components/selector/supply_current_limit_a",
        restore_instruction="Restore the selector supply current limit to 0.4 A.",
        x_plan_paths=plans,
        output=tmp_path / "change-plan.json",
        now=lambda: "2026-08-30T10:00:00+00:00",
    )
    plan = json.loads(output.read_text(encoding="utf-8"))
    validated = validate_intervention_change_plan(plan)
    assert validated.before == 0.4
    assert validated.after == 0.5
    assert set(validated.expected_x_run_ids) == set(producer.X_RUN_ROLES)


def test_intervention_plan_rejects_unbound_or_mismatched_x_plan(tmp_path: Path) -> None:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
        )
        for role in producer.X_RUN_ROLES
    }
    bad = json.loads(plans["boundary_intervention"].read_text(encoding="utf-8"))
    bad["plan_contract"]["x_intervention_prebinding"]["contract_id"] = "other-contract"
    bad = producer.leakage_runner._plan_envelope(bad["plan_contract"])
    _write_json(plans["boundary_intervention"], bad)
    with pytest.raises(producer.SelectedStateInputError, match="differs from this contract"):
        producer.produce_intervention_plan(
            contract_id=CONTRACT,
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            before_fixture_manifest=before_manifest,
            after_fixture_manifest=after_manifest,
            component_id="selector-v5-01",
            property_path="/stage_delta/components/selector/supply_current_limit_a",
            restore_instruction="Restore the selector supply current limit to 0.4 A.",
            x_plan_paths=plans,
            output=tmp_path / "change-plan.json",
        )


def test_intervention_plan_rejects_component_absent_from_stage_before_output(
    tmp_path: Path,
) -> None:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    implicated_stage = "direct_rx2_termination"
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
            implicated_stage=implicated_stage,
        )
        for role in producer.X_RUN_ROLES
    }
    output = tmp_path / "change-plan.json"

    with pytest.raises(producer.SelectedStateInputError, match="absent from implicated stage"):
        producer.produce_intervention_plan(
            contract_id=CONTRACT,
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            before_fixture_manifest=before_manifest,
            after_fixture_manifest=after_manifest,
            component_id="selector-v5-01",
            property_path="/stage_delta/components/selector/supply_current_limit_a",
            restore_instruction="Restore the selector supply current limit to 0.4 A.",
            x_plan_paths=plans,
            output=output,
        )

    assert not output.exists()


def test_intervention_plan_rejects_replace_placeholder_input_before_output(tmp_path: Path) -> None:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
        )
        for role in producer.X_RUN_ROLES
    }
    output = tmp_path / "change-plan.json"
    with pytest.raises(producer.SelectedStateInputError, match="REPLACE placeholder"):
        producer.produce_intervention_plan(
            contract_id=CONTRACT,
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            before_fixture_manifest=before_manifest,
            after_fixture_manifest=after_manifest,
            component_id="selector-v5-01",
            property_path="/stage_delta/components/selector/supply_current_limit_a",
            restore_instruction="REPLACE_EXACT_REVERSAL_INSTRUCTION",
            x_plan_paths=plans,
            output=output,
        )
    assert not output.exists()


def _prepared_change_plan(
    tmp_path: Path, *, implicated_stage: str = "powered_selector_all_inputs_terminated"
) -> Path:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    roles = producer._x_roles_for_stage(implicated_stage)
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
            implicated_stage=implicated_stage,
        )
        for role in roles
    }
    return producer.produce_intervention_plan(
        contract_id=CONTRACT,
        campaign_id=CAMPAIGN,
        board_id=BOARD,
        before_fixture_manifest=before_manifest,
        after_fixture_manifest=after_manifest,
        component_id="selector-v5-01",
        property_path="/stage_delta/components/selector/supply_current_limit_a",
        restore_instruction="Restore the selector supply current limit to 0.4 A.",
        x_plan_paths=plans,
        output=tmp_path / "change-plan.json",
        now=lambda: "2026-08-30T10:00:00+00:00",
    )


def _accepted_manifest(
    tmp_path: Path,
    *,
    role: str,
    plan: dict[str, Any],
    index: int,
) -> Path:
    captures: list[dict[str, Any]] = []
    for repeat_index in range(1, 6):
        capture_files: dict[str, dict[str, Any]] = {}
        for name, suffix in (
            ("raw_iq_file", "sigmf-data"),
            ("metadata_file", "sigmf-meta"),
            ("condition_record_file", "condition.json"),
        ):
            path = tmp_path / f"{role}.{repeat_index}.{suffix}"
            path.write_bytes(f"{name}-{role}-{repeat_index}".encode())
            capture_files[name] = producer._file(path, name)
        captures.append(
            {
                "stream_id": f"stream-{role}-{repeat_index}",
                **capture_files,
                "abi2_continuity_verified": True,
                "measurement_quality_passed": True,
            }
        )
    before_revision = plan["before_fixture"]["fixture_revision_sha256"]
    after_revision = plan["installed_after_fixture"]["fixture_revision_sha256"]
    baseline = role.endswith("baseline")
    immutable_plan = json.loads(
        Path(plan["x_run_plans"][role]["plan_file"]["path"]).read_text(encoding="utf-8")
    )
    contract = immutable_plan["plan_contract"]
    context = contract["x_intervention_capture_context"]
    topology_stage = contract["topology_stage"]
    if topology_stage in producer.X_BOUNDARY_STAGES[:2]:
        safe_state = {
            "status": "physical_disconnect_verified",
            "topology_stage": topology_stage,
            "selector_rf_state": "rf_disconnected",
            "selector_power_state": "bench_power_off",
            "selector_control_harness_state": "disconnected",
        }
    else:
        safe_state = {
            "status": "mailbox_all_off_verified",
            "topology_stage": topology_stage,
            "mailbox_all_off_verified": True,
        }
    return _write_json(
        tmp_path / f"{role}.manifest.json",
        {
            "schema": 1,
            "run_kind": producer.X_MANIFEST_KIND,
            "contract_id": CONTRACT,
            "run_role": role,
            "run_id": plan["x_run_plans"][role]["run_id"],
            "status": "accepted",
            "captured_at": f"2026-08-30T10:0{index + 1}:00+00:00",
            "acquisition_index": context["acquisition_index"],
            "freshness_epoch_id": context["freshness_epoch_id"],
            "intervention_state_fixture_revision_sha256": (
                before_revision if baseline else after_revision
            ),
            "topology_stage": topology_stage,
            "topology_fixture_sha256": contract["fixture_evidence_sha256"],
            "source_commit": "a" * 40,
            "dependency_commit": "b" * 40,
            "selector_evidence_sha256": context["selector_flash_evidence"]["sha256"],
            "immutable_plan_file": plan["x_run_plans"][role]["plan_file"],
            "captures": captures,
            "measurement_quality_rejection_reasons": [],
            "final_mute_verified": True,
            "final_selector_safe_state": safe_state,
        },
    )


def _seal_inputs(
    tmp_path: Path, *, implicated_stage: str = "powered_selector_all_inputs_terminated"
) -> tuple[Path, dict[str, Path], Path, Path, Path | None, Path | None]:
    change_plan_path = _prepared_change_plan(tmp_path, implicated_stage=implicated_stage)
    plan = json.loads(change_plan_path.read_text(encoding="utf-8"))
    roles = producer._x_roles_for_stage(implicated_stage)
    manifests = {
        role: _accepted_manifest(tmp_path, role=role, plan=plan, index=index)
        for index, role in enumerate(roles)
    }
    full_intervention_plan = json.loads(
        Path(plan["x_run_plans"]["full_fixture_intervention"]["plan_file"]["path"]).read_text(
            encoding="utf-8"
        )
    )
    installed_fixture_evidence = full_intervention_plan["plan_contract"]["fixture_evidence"]
    installation = _write_json(
        tmp_path / "installation.json",
        {
            "schema": 1,
            "evidence_kind": "5g8_installed_after_state_attestation_v1",
            "contract_id": CONTRACT,
            "observed_at": "2026-08-30T10:06:00+00:00",
            "installed_state": "after",
            "fixture_revision_sha256": plan["installed_after_fixture"]["fixture_revision_sha256"],
            "fixture_manifest_file": producer._file(
                Path(plan["installed_after_fixture"]["fixture_manifest_path"]),
                "after fixture",
            ),
            "setup_attestation_file": installed_fixture_evidence["source_files"][
                "setup_attestation"
            ],
            "setup_evidence_file": installed_fixture_evidence["setup_attestation"][
                "setup_evidence"
            ],
            "accepted": True,
        },
    )
    manifest_documents = {
        role: json.loads(path.read_text(encoding="utf-8")) for role, path in manifests.items()
    }
    support_repeats = {
        role: [
            {
                "repeat_index": repeat_index,
                "condition_id": f"{role}-condition-{repeat_index}",
                "stream_id": capture["stream_id"],
                "raw_iq_sha256": capture["raw_iq_file"]["sha256"],
                "quality_passed": True,
                "rx1_amplitude_counts": 1000.0,
                "transfer_detected": True,
                "transfer_amplitude_ratio": (0.1 if role.endswith("baseline") else 0.04),
                "transfer_amplitude_upper_bound_ratio": None,
            }
            for repeat_index, capture in enumerate(document["captures"], start=1)
        ]
        for role, document in manifest_documents.items()
    }
    cohorts = {
        role: tuple(
            intervention_repeat_from_document(item, role=role) for item in support_repeats[role]
        )
        for role in roles
    }
    qualification = json.loads(
        json.dumps(asdict(qualify_intervention_support(cohorts)), default=str)
    )
    repository = Path(__file__).resolve().parents[1]
    support_source_paths = (
        "src/smateway/capture_admission.py",
        "src/smateway/file_artifact_admission.py",
        "src/smateway/hexcal.py",
        "src/smateway/intervention_support.py",
        "src/smateway/leakage_ladder.py",
        "src/smateway/native_iio_attestation.py",
        "src/smateway/ota_analysis.py",
        "src/smateway/selected_state_qualification.py",
        "scripts/run_5g8_leakage_ladder.py",
        "scripts/analyze_5g8_intervention_support.py",
    )
    source_files = [
        {
            "path": relative,
            "sha256": producer.sha256_path(repository / relative),
            "size_bytes": (repository / relative).stat().st_size,
        }
        for relative in support_source_paths
    ]
    plan_source = full_intervention_plan["plan_contract"]["source"]
    dependency_runtime = copy.deepcopy(plan_source["pluto_plus_utils_source_attestation"])
    native_runtime = copy.deepcopy(plan_source["native_libiio_runtime_attestation"])
    selector_sha = next(iter(manifest_documents.values()))["selector_evidence_sha256"]
    source_identity = {
        "smateway_commit": "a" * 40,
        "dependency_commit": "b" * 40,
        "dependency_attestation_sha256": producer.canonical_sha256(dependency_runtime),
        "native_attestation_sha256": producer.canonical_sha256(native_runtime),
        "selector_evidence_sha256": selector_sha,
    }
    analysis_runtime = {
        "source": {
            "schema": 1,
            "repository": str(repository),
            "commit": "a" * 40,
            "clean_source_files_verified": True,
            "files": source_files,
            "source_files_sha256": producer.canonical_sha256(source_files),
        },
        "dependency": dependency_runtime,
        "native": native_runtime,
        "source_commit": "a" * 40,
        "dependency_commit": "b" * 40,
        "native_attestation_sha256": producer.canonical_sha256(native_runtime),
    }
    plan_file = producer._file(change_plan_path, "change plan")
    manifest_files = {
        role: producer._file(path, f"X {role} manifest") for role, path in manifests.items()
    }
    manifest_hashes = {role: binding["sha256"] for role, binding in manifest_files.items()}
    analysis_input_identity = {
        "change_plan_file": plan_file,
        "x_run_manifest_files": manifest_files,
        "x_run_source_identity": source_identity,
        "analysis_runtime_identity": {
            "source_commit": "a" * 40,
            "source_files_sha256": analysis_runtime["source"]["source_files_sha256"],
            "dependency_commit": "b" * 40,
            "dependency_attestation_sha256": producer.canonical_sha256(dependency_runtime),
            "native_attestation_sha256": producer.canonical_sha256(native_runtime),
        },
    }
    analysis = _write_json(
        tmp_path / "support-analysis.json",
        {
            "schema": 1,
            "analysis_kind": "5g8_intervention_support_analysis_v1",
            "created_at": "2026-08-30T10:05:00+00:00",
            "contract_id": CONTRACT,
            "change_plan_file": plan_file,
            "x_run_manifest_files": manifest_files,
            "x_run_manifest_sha256s": manifest_hashes,
            "x_run_source_identity": source_identity,
            "analysis_runtime": analysis_runtime,
            "normalized_repeats": support_repeats,
            "qualification": qualification,
            "input_identity_sha256": producer.canonical_sha256(analysis_input_identity),
        },
    )
    support = _write_json(
        tmp_path / "support.json",
        {
            "schema": 1,
            "result_kind": "5g8_intervention_support_result_v1",
            "contract_id": CONTRACT,
            "decision": "supported_fix",
            "accepted": True,
            "x_run_manifest_sha256s": manifest_hashes,
            "analysis_file": producer._file(analysis, "analysis"),
            "simultaneous_improvement_gate_passed": True,
            "rejection_reasons": [],
        },
    )
    restoration: Path | None = None
    reapplication: Path | None = None
    if implicated_stage in producer.X_BOUNDARY_STAGES:
        restoration_photo = tmp_path / "restoration-photo.png"
        restoration_photo.write_bytes(b"restored-before")
        reapplication_photo = tmp_path / "reapplication-photo.png"
        reapplication_photo.write_bytes(b"reapplied-after")

        def transition(path: Path, observed_at: str, source: str, target: str, photo: Path) -> Path:
            return _write_json(
                path,
                {
                    "schema": 1,
                    "evidence_kind": "5g8_fixture_state_transition_attestation_v1",
                    "contract_id": CONTRACT,
                    "observed_at": observed_at,
                    "from_fixture_revision_sha256": source,
                    "to_fixture_revision_sha256": target,
                    "setup_evidence_file": producer._file(photo, "transition photo"),
                    "accepted": True,
                },
            )

        before_revision = plan["before_fixture"]["fixture_revision_sha256"]
        after_revision = plan["installed_after_fixture"]["fixture_revision_sha256"]
        restoration = transition(
            tmp_path / "restoration.json",
            "2026-08-30T10:02:30+00:00",
            after_revision,
            before_revision,
            restoration_photo,
        )
        reapplication = transition(
            tmp_path / "reapplication.json",
            "2026-08-30T10:03:30+00:00",
            before_revision,
            after_revision,
            reapplication_photo,
        )
    return change_plan_path, manifests, installation, support, restoration, reapplication


def test_intervention_seal_reopens_all_x_sources_and_installs_after_state(tmp_path: Path) -> None:
    plan, manifests, installation, support, restoration, reapplication = _seal_inputs(tmp_path)
    output = producer.produce_intervention_seal(
        change_plan_path=plan,
        x_manifest_paths=manifests,
        installation_attestation_path=installation,
        support_result_path=support,
        restoration_evidence_path=restoration,
        reapplication_evidence_path=reapplication,
        output=tmp_path / "intervention-evidence.json",
        now=lambda: "2026-08-30T10:07:00+00:00",
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    admitted = producer.validate_intervention_contract(document)
    assert admitted.diagnostic_restoration_status == "restored_then_reapplied"
    assert admitted.installed_after_fixture_revision_sha256 != (
        admitted.baseline_fixture_revision_sha256
    )


def test_four_role_seal_rejects_missing_transition_evidence(tmp_path: Path) -> None:
    plan, manifests, installation, support, _restoration, _reapplication = _seal_inputs(tmp_path)
    output = tmp_path / "intervention-evidence.json"
    with pytest.raises(producer.SelectedStateInputError, match="four-role X sealing requires"):
        producer.produce_intervention_seal(
            change_plan_path=plan,
            x_manifest_paths=manifests,
            installation_attestation_path=installation,
            support_result_path=support,
            output=output,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("implicated_stage", "expected_roles"),
    [
        (
            "powered_selector_all_inputs_terminated",
            (
                "boundary_baseline",
                "boundary_intervention",
                "full_fixture_baseline",
                "full_fixture_intervention",
            ),
        ),
        (
            FULL_CONDUCTED_STAGE,
            ("full_fixture_baseline", "full_fixture_intervention"),
        ),
    ],
)
def test_intervention_branches_use_truthful_distinct_x_roles_and_safe_state(
    tmp_path: Path, implicated_stage: str, expected_roles: tuple[str, ...]
) -> None:
    plan, manifests, installation, support, restoration, reapplication = _seal_inputs(
        tmp_path, implicated_stage=implicated_stage
    )
    plan_document = json.loads(plan.read_text(encoding="utf-8"))
    assert tuple(plan_document["x_run_plans"]) == tuple(sorted(expected_roles))
    output = producer.produce_intervention_seal(
        change_plan_path=plan,
        x_manifest_paths=manifests,
        installation_attestation_path=installation,
        support_result_path=support,
        restoration_evidence_path=restoration,
        reapplication_evidence_path=reapplication,
        output=tmp_path / "intervention-evidence.json",
        now=lambda: "2026-08-30T10:07:00+00:00",
    )
    admitted = producer.validate_intervention_contract(
        json.loads(output.read_text(encoding="utf-8"))
    )
    assert admitted.implicated_boundary_stage == implicated_stage
    if implicated_stage in producer.X_BOUNDARY_STAGES:
        boundary = json.loads(manifests["boundary_baseline"].read_text(encoding="utf-8"))
        expected_safe_status = (
            "physical_disconnect_verified"
            if implicated_stage in producer.X_BOUNDARY_STAGES[:2]
            else "mailbox_all_off_verified"
        )
        assert boundary["final_selector_safe_state"]["status"] == expected_safe_status
    else:
        assert not any(role.startswith("boundary") for role in plan_document["x_run_plans"])
        assert admitted.diagnostic_restoration_status == "not_performed"


def test_e_implicated_branch_rejects_four_role_masquerade(tmp_path: Path) -> None:
    before, after, before_manifest, after_manifest, before_binding, after_binding = _fixture_pair(
        tmp_path
    )
    plans = {
        role: _x_plan(
            tmp_path / f"{role}.plan.json",
            role=role,
            before_chain=before,
            after_chain=after,
            before_binding=before_binding,
            after_binding=after_binding,
            implicated_stage=FULL_CONDUCTED_STAGE,
        )
        for role in producer.X_FULL_FIXTURE_ROLES
    }
    plans["boundary_baseline"] = plans["full_fixture_baseline"]
    plans["boundary_intervention"] = plans["full_fixture_intervention"]
    with pytest.raises(producer.SelectedStateInputError, match="differs from this contract"):
        producer.produce_intervention_plan(
            contract_id=CONTRACT,
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            before_fixture_manifest=before_manifest,
            after_fixture_manifest=after_manifest,
            component_id="selector-v5-01",
            property_path="/stage_delta/components/selector/supply_current_limit_a",
            restore_instruction="Restore the selector supply current limit to 0.4 A.",
            x_plan_paths=plans,
            output=tmp_path / "change-plan.json",
        )


def test_intervention_seal_rejects_self_asserted_or_stale_x_artifact(tmp_path: Path) -> None:
    plan, manifests, installation, support, restoration, reapplication = _seal_inputs(tmp_path)
    stale = manifests["boundary_intervention"]
    document = json.loads(stale.read_text(encoding="utf-8"))
    raw_path = Path(document["captures"][0]["raw_iq_file"]["path"])
    raw_path.write_bytes(b"changed-after-acceptance")
    with pytest.raises(producer.SelectedStateInputError, match="bytes differ"):
        producer.produce_intervention_seal(
            change_plan_path=plan,
            x_manifest_paths=manifests,
            installation_attestation_path=installation,
            support_result_path=support,
            restoration_evidence_path=restoration,
            reapplication_evidence_path=reapplication,
            output=tmp_path / "intervention-evidence.json",
        )


def test_intervention_seal_rejects_replace_placeholder_in_x_manifest(tmp_path: Path) -> None:
    plan, manifests, installation, support, restoration, reapplication = _seal_inputs(tmp_path)
    manifest_path = manifests["boundary_intervention"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["captures"][0]["stream_id"] = "REPLACE_STREAM_ID"
    _write_json(manifest_path, manifest)
    output = tmp_path / "intervention-evidence.json"
    with pytest.raises(producer.SelectedStateInputError, match="REPLACE placeholder"):
        producer.produce_intervention_seal(
            change_plan_path=plan,
            x_manifest_paths=manifests,
            installation_attestation_path=installation,
            support_result_path=support,
            restoration_evidence_path=restoration,
            reapplication_evidence_path=reapplication,
            output=output,
        )
    assert not output.exists()


def test_producer_rejects_nonlocal_or_symlinked_output_parent(tmp_path: Path) -> None:
    with pytest.raises(producer.SelectedStateInputError, match="local Raspberry Pi storage"):
        producer._write_new(Path("/mnt/t8-must-not-be-written.json"), {"status": "unsafe"})

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(producer.SelectedStateInputError, match="contains a symlink"):
        producer._write_new(linked_parent / "evidence.json", {"status": "unsafe"})


def test_t8_operator_templates_and_copyable_workflow_are_published() -> None:
    installed = json.loads(
        (DOCS / "t8_installed_after_state.template.json").read_text(encoding="utf-8")
    )
    transition = json.loads(
        (DOCS / "t8_fixture_state_transition.template.json").read_text(encoding="utf-8")
    )
    workflow = (DOCS / "t8_selected_state_workflow.md").read_text(encoding="utf-8")
    assert installed["evidence_kind"] == "5g8_installed_after_state_attestation_v1"
    assert transition["evidence_kind"] == "5g8_fixture_state_transition_attestation_v1"
    for command in (
        "device-identity",
        "intervention-plan",
        "analyze_5g8_intervention_support.py",
        "intervention-seal",
        "generate_5g8_fixture_manifest.py",
        "--setup-from-fixture",
        "static-bench prepare",
        "static-bench capture",
        "static-bench analyze",
        "fast20-timing prepare",
        "fast20-matrix prepare",
    ):
        assert command in workflow
    assert producer.X_PREBINDING_KIND in workflow
    assert producer.X_MANIFEST_KIND in workflow
    assert "PYTHON=./.venv/bin/python" in workflow
    assert '"$PYTHON" "$RUNNER"' in workflow
    assert (
        workflow.index("### Q1 — sealed bench image")
        < workflow.index("### STOP — exact image transition")
        < workflow.index("### Q2 — sealed Fast20 image")
    )
    assert "--prepare-and-program" in workflow
    assert "--verify-after-power-cycle" in workflow
    assert "20260830" not in workflow
