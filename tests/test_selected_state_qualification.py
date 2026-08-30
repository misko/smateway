from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from math import cos, radians, sin
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
    ANTENNA_STATES,
    EXPECTED_STATES,
    FULL_CONDUCTED_STAGE,
    INTERVENTION_KIND,
    MATRIX_KIND,
    STATIC_KIND,
    TIMING_KIND,
    SelectedStateQualificationError,
    SelectorEvidenceBinding,
    _sealed_x_selector,
    _validate_change_stage_compatibility,
    canonical_sha256,
    full_simultaneous_fixture_binding_from_manifest,
    qualify_fast20_matrix,
    qualify_fast20_timing,
    qualify_selected_state_release,
    qualify_static_bench,
    validate_full_simultaneous_fixture,
    validate_intervention_change_plan,
    validate_intervention_contract,
    validate_selector_binding_snapshot,
)

CAMPAIGN = "5p8-debug-r1"
BOARD = "stm32c011-4c0055000950313950363920"
SOURCE = "a" * 40
DEPENDENCY = "b" * 40
NATIVE = "c" * 64
DEVICE = "d" * 64
HARDWARE_REVISION = "pluto-rx2-8way-v5"
PLUTO_SERIAL = "104000b29905000e17000800065934759d"
BENCH_SELECTOR_EVIDENCE_BYTES = b'{\n  "sealed": true\n}\n'
BENCH_SELECTOR_EVIDENCE_SHA256 = hashlib.sha256(BENCH_SELECTOR_EVIDENCE_BYTES).hexdigest()


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _file(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def _phasor(magnitude: float, phase_deg: float) -> complex:
    angle = radians(phase_deg)
    return magnitude * complex(cos(angle), sin(angle))


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": value.real, "imag": value.imag}


def _fixture_document(tmp_path: Path) -> dict[str, Any]:
    chain = _production_fixture_chain(
        tmp_path / "qualification-fixture-chain",
        run_prefix="qualification",
    )
    return full_simultaneous_fixture_binding_from_manifest(chain[FULL_CONDUCTED_STAGE]["manifest"])


def _selector(role: str, *, evidence_sha256: str | None = None) -> SelectorEvidenceBinding:
    assert role in ("bench", "fast20")
    return SelectorEvidenceBinding(
        path=f"/evidence/{role}/selector-flash-evidence.json",
        sha256=(
            evidence_sha256
            if evidence_sha256 is not None
            else (BENCH_SELECTOR_EVIDENCE_SHA256 if role == "bench" else _hash("selector-fast20"))
        ),
        campaign_id=CAMPAIGN,
        run_id=f"selector-{role}-r01",
        board_id=BOARD,
        image_role=role,  # type: ignore[arg-type]
        firmware_bin_sha256=_hash(f"firmware-{role}"),
        profile_contract_sha256=_hash("fast20-profile-v1"),
        startup_evidence_sha256=_hash(f"startup-{role}"),
    )


def _device_identity_snapshot(*, usb_uri: str = "usb:1.2") -> dict[str, Any]:
    return {
        "serial": PLUTO_SERIAL,
        "usb_uri": usb_uri,
        "model": "PlutoSDR",
        "firmware_version": "v0.40-plutoplus-spf-tandem-agc-v7",
        "kernel_version": "6.1.0",
        "phy_model": "ad9361",
        "metadata_abi": 2,
        "rx_scan_channels": ["voltage0", "voltage1", "voltage2", "voltage3"],
        "native_attestation_sha256": NATIVE,
    }


def _context(
    fixture_sha: str,
    selector: SelectorEvidenceBinding,
    plan: str,
    *,
    device_identity_sha256: str = DEVICE,
    usb_uri: str = "usb:1.2",
) -> dict[str, Any]:
    return {
        "campaign_id": CAMPAIGN,
        "board_id": BOARD,
        "fixture_revision_sha256": fixture_sha,
        "selector_evidence_sha256": selector.sha256,
        "selector_image_role": selector.image_role,
        "source_commit": SOURCE,
        "dependency_commit": DEPENDENCY,
        "native_attestation_sha256": NATIVE,
        "device_identity_sha256": device_identity_sha256,
        "device_identity_snapshot": _device_identity_snapshot(usb_uri=usb_uri),
        "plan_sha256": _hash(plan),
    }


def _capture(context: dict[str, Any], label: str) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix=f"t8-{label}-"))
    raw_path = root / f"{label}.sigmf-data"
    metadata_path = root / f"{label}.sigmf-meta"
    record_path = root / "selected-state-condition-record.json"
    raw_path.write_bytes(f"raw-{label}".encode())
    metadata_path.write_text(json.dumps({"label": label}), encoding="utf-8")
    record_path.write_text(json.dumps({"label": label}), encoding="utf-8")
    return {
        "run_id": f"run-{label}",
        "stream_id": f"stream-{label}",
        "artifact_id": f"artifact-{label}",
        "raw_iq_path": str(raw_path),
        "raw_iq_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "raw_iq_size_bytes": raw_path.stat().st_size,
        "metadata_path": str(metadata_path),
        "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "metadata_size_bytes": metadata_path.stat().st_size,
        "condition_record_path": str(record_path),
        "condition_record_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
        "condition_record_size_bytes": record_path.stat().st_size,
        "leaf_source_sha256s": [_hash(f"leaf-{label}")],
        "plan_sha256": context["plan_sha256"],
        "fixture_revision_sha256": context["fixture_revision_sha256"],
        "selector_evidence_sha256": context["selector_evidence_sha256"],
        "source_commit": context["source_commit"],
        "dependency_commit": context["dependency_commit"],
        "native_attestation_sha256": context["native_attestation_sha256"],
        "device_identity_sha256": context["device_identity_sha256"],
    }


def _quality(samples: int) -> dict[str, Any]:
    return {
        "metadata_abi": 2,
        "expected_sample_count": samples,
        "observed_sample_count": samples,
        "raw_sample_count": samples,
        "continuity_verified": True,
        "missing_sample_count": 0,
        "clipped_sample_count": 0,
        "adc_headroom_db": 12.0,
        "reference_detected": True,
        "reference_snr_db": 34.0,
        "final_mute_verified": True,
        "final_selector_control_verified": True,
    }


def _transfer(value: complex) -> dict[str, Any]:
    return {
        "detected": True,
        "h": _complex_document(value),
        "magnitude_upper_bound": None,
        "coherence": 0.999,
        "phase_rms_deg": 0.4,
    }


def _static_document(
    fixture_sha: str,
    *,
    device_identity_sha256: str = DEVICE,
    usb_uri: str = "usb:1.2",
    selector_evidence_sha256: str | None = None,
) -> tuple[dict[str, Any], SelectorEvidenceBinding]:
    selector = _selector("bench", evidence_sha256=selector_evidence_sha256)
    context = _context(
        fixture_sha,
        selector,
        "static-plan",
        device_identity_sha256=device_identity_sha256,
        usb_uri=usb_uri,
    )
    codes = {"ALL_OFF": 8, **{state: index for index, state in enumerate(ANTENNA_STATES)}}
    observations = []
    for index, state in enumerate(EXPECTED_STATES):
        code = codes[state]
        value = _phasor(0.001 if state == "ALL_OFF" else 0.1, index * 11.0)
        observations.append(
            {
                "state": state,
                "capture": _capture(context, f"static-{state.lower()}"),
                "command": {
                    "commanded_state": state,
                    "commanded_code": code,
                    "command_sequence": index + 1,
                    "acknowledged_sequence": index + 1,
                    "applied_state": state,
                    "applied_code": code,
                    "gpio_latch_code": code,
                    "lease_ms": 0 if state == "ALL_OFF" else 60_000,
                    "command_valid": True,
                    "readback_passed": True,
                },
                "quality": _quality(300_000),
                "transfer": _transfer(value),
            }
        )
    return (
        {
            "schema": 1,
            "evidence_kind": STATIC_KIND,
            "context": context,
            "state_order": list(EXPECTED_STATES),
            "state_codes": codes,
            "observations": observations,
            "final_mute_verified": True,
            "final_all_off_readback": {
                "state": "ALL_OFF",
                "mailbox_code": 8,
                "gpio_latch_code": 8,
                "passed": True,
            },
        },
        selector,
    )


def _timing_document(
    fixture_sha: str,
    *,
    device_identity_sha256: str = DEVICE,
    usb_uri: str = "usb:1.2",
) -> tuple[dict[str, Any], SelectorEvidenceBinding]:
    selector = _selector("fast20")
    context = _context(
        fixture_sha,
        selector,
        "timing-plan",
        device_identity_sha256=device_identity_sha256,
        usb_uri=usb_uri,
    )
    expected_samples = 10_000_000
    profile = {
        "profile_id": "fast20-v1",
        "profile_contract_sha256": selector.profile_contract_sha256,
        "state_order": list(EXPECTED_STATES),
        "sample_rate_hz": 1_000_000,
        "expected_sample_count": expected_samples,
        "samples_per_frame": 100_000,
        "frame_count": 100,
        "minimum_complete_cycles": 20,
        "dwell_window_ms_by_state": {
            state: [nominal * 0.95, nominal * 1.05]
            for state, nominal in zip(
                ANTENNA_STATES, (20.0, 23.0, 26.0, 30.0, 34.0, 39.0, 44.0, 50.0), strict=True
            )
        },
    }
    runs = []
    for repeat in (1, 2):
        runs.append(
            {
                "capture": _capture(context, f"timing-{repeat}"),
                "quality": _quality(expected_samples),
                "timing": {
                    "state_order": list(EXPECTED_STATES),
                    "isolation_verified": True,
                    "continuity_verified": True,
                    "complete_cycle_count": 20,
                    "rejected_marker_count": 0,
                    "threshold_stable": True,
                    "dwell_by_state": {
                        state: {
                            "observed_count": 20,
                            "duration_min_ms": nominal * 0.99,
                            "duration_max_ms": nominal * 1.01,
                        }
                        for state, nominal in zip(
                            ANTENNA_STATES,
                            (20.0, 23.0, 26.0, 30.0, 34.0, 39.0, 44.0, 50.0),
                            strict=True,
                        )
                    },
                },
            }
        )
    return (
        {
            "schema": 1,
            "evidence_kind": TIMING_KIND,
            "context": context,
            "profile": profile,
            "runs": runs,
            "final_mute_verified": True,
            "final_fast20_schedule_verified": {
                "image_role": "fast20",
                "profile_contract_sha256": selector.profile_contract_sha256,
                "passed": True,
            },
        },
        selector,
    )


def _matrix_document(
    fixture_sha: str,
    *,
    selected_magnitude: float = 0.1,
    amplitude_jitter: float = 0.0002,
    phase_jitter_deg: float = 0.02,
    device_identity_sha256: str = DEVICE,
    usb_uri: str = "usb:1.2",
) -> tuple[dict[str, Any], SelectorEvidenceBinding]:
    selector = _selector("fast20")
    context = _context(
        fixture_sha,
        selector,
        "matrix-plan",
        device_identity_sha256=device_identity_sha256,
        usb_uri=usb_uri,
    )
    streams = []
    for repeat in range(1, 6):
        states: dict[str, Any] = {}
        centered = repeat - 3
        states["ALL_OFF"] = _transfer(_phasor(0.001, 7.0 + centered * 0.01))
        for index, state in enumerate(ANTENNA_STATES):
            magnitude = selected_magnitude * (1.0 + centered * amplitude_jitter)
            phase = index * 23.0 + centered * phase_jitter_deg
            states[state] = _transfer(_phasor(magnitude, phase))
        streams.append(
            {
                "repeat_index": repeat,
                "capture": _capture(context, f"matrix-{repeat}"),
                "quality": _quality(10_000_000),
                "state_order": list(EXPECTED_STATES),
                "states": states,
            }
        )
    return (
        {
            "schema": 1,
            "evidence_kind": MATRIX_KIND,
            "context": context,
            "state_order": list(EXPECTED_STATES),
            "repeat_count": 5,
            "streams": streams,
            "final_mute_verified": True,
            "final_fast20_schedule_verified": {
                "image_role": "fast20",
                "profile_contract_sha256": selector.profile_contract_sha256,
                "passed": True,
            },
        },
        selector,
    )


def _intervention_document(
    tmp_path: Path,
    *,
    after_supply_current_limit_a: float = 0.5,
    after_splitter_power_dbm: float = 20.0,
) -> dict[str, Any]:
    contract_id = "selector-current-limit-intervention-r01"
    selector_binding, selector_control = sealed_bench_selector(
        tmp_path / "intervention-selector-seal"
    )
    before_chain = _production_fixture_chain(
        tmp_path / "intervention-before-chain",
        supply_current_limit_a=0.4,
        run_prefix="before",
        selector_binding=selector_binding,
        selector_control=selector_control,
    )
    after_chain = _production_fixture_chain(
        tmp_path / "intervention-after-chain",
        splitter_power_dbm=after_splitter_power_dbm,
        supply_current_limit_a=after_supply_current_limit_a,
        run_prefix="after",
        selector_binding=selector_binding,
        selector_control=selector_control,
    )
    before_path = Path(before_chain[FULL_CONDUCTED_STAGE]["manifest"])
    after_path = Path(after_chain[FULL_CONDUCTED_STAGE]["manifest"])
    before_binding = full_simultaneous_fixture_binding_from_manifest(before_path)
    after_binding = full_simultaneous_fixture_binding_from_manifest(after_path)
    implicated_stage = "powered_selector_all_inputs_terminated"
    roles = (
        "boundary_baseline",
        "boundary_intervention",
        "full_fixture_baseline",
        "full_fixture_intervention",
    )
    role_evidence = {
        "boundary_baseline": before_chain[implicated_stage]["evidence"],
        "boundary_intervention": after_chain[implicated_stage]["evidence"],
        "full_fixture_baseline": before_chain[FULL_CONDUCTED_STAGE]["evidence"],
        "full_fixture_intervention": after_chain[FULL_CONDUCTED_STAGE]["evidence"],
    }
    x_plans: dict[str, Any] = {}
    plan_contexts: dict[str, Any] = {}
    for index, role in enumerate(roles):
        fixture_evidence = copy.deepcopy(role_evidence[role])
        run_id = fixture_evidence["run_id"]
        topology_stage = fixture_evidence["stage"]
        capture_fixture = before_binding if role.endswith("baseline") else after_binding
        role_selector_binding = fixture_evidence["selector_flash_evidence"]
        capture_context = {
            "schema": 1,
            "binding_kind": "5g8_x_intervention_capture_context_v1",
            "implicated_boundary_stage": implicated_stage,
            "acquisition_index": 41 + index,
            "freshness_epoch_id": "fixture-epoch-17",
            "capture_state_fixture": capture_fixture,
            "installed_after_fixture": after_binding,
            "selector_flash_evidence": role_selector_binding,
        }
        plan_contexts[role] = {
            "topology_stage": topology_stage,
            "topology_fixture_sha256": canonical_sha256(fixture_evidence),
            "capture_context": capture_context,
            "selector_evidence_sha256": role_selector_binding["sha256"],
        }
        state_chain = before_chain if role.endswith("baseline") else after_chain
        contract = build_x_plan_contract(
            role=role,
            contract_id=contract_id,
            implicated_stage=implicated_stage,
            acquisition_index=41 + index,
            freshness_epoch_id="fixture-epoch-17",
            fixture_evidence=fixture_evidence,
            capture_fixture=capture_fixture,
            installed_after_fixture=after_binding,
            selector_binding=role_selector_binding,
            selector_control=state_chain["selector_control"],
            serial=PLUTO_SERIAL,
        )
        path = tmp_path / f"x-{role}-plan.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "plan_contract": contract,
                    "plan_contract_sha256": canonical_sha256(contract),
                    "plan_contract_hash_provenance": (
                        "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
                    ),
                    "immutable": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        x_plans[role] = {"run_id": run_id, "plan_file": _file(path)}
    change_plan = {
        "schema": 2,
        "plan_kind": "5g8_intervention_change_plan_v2",
        "contract_id": contract_id,
        "campaign_id": CAMPAIGN,
        "board_id": BOARD,
        "created_at": "2026-08-29T11:55:00+00:00",
        "before_fixture": before_binding,
        "installed_after_fixture": after_binding,
        "change": {
            "component_id": "selector-v5-01",
            "property_path": "/stage_delta/components/selector/supply_current_limit_a",
            "before": 0.4,
            "after": after_supply_current_limit_a,
            "reversible": True,
            "restore_instruction": "Restore the selector supply current limit to 0.4 A.",
        },
        "implicated_boundary_stage": implicated_stage,
        "x_run_plans": x_plans,
        "diagnostic_restoration_policy": (
            "restoration_is_diagnostic_only_and_requires_source_bound_reapplication_before_q"
        ),
    }
    change_plan_path = tmp_path / "intervention-change-plan.json"
    change_plan_path.write_text(json.dumps(change_plan, sort_keys=True), encoding="utf-8")
    change_plan_file = _file(change_plan_path)
    x_runs: dict[str, Any] = {}
    manifest_hashes: dict[str, str] = {}
    support_repeats: dict[str, list[dict[str, Any]]] = {}
    for index, role in enumerate(roles):
        captures: list[dict[str, Any]] = []
        streams: list[str] = []
        raw_files: list[dict[str, Any]] = []
        role_repeats: list[dict[str, Any]] = []
        for repeat_index in range(1, 6):
            raw = tmp_path / f"x-{role}-{repeat_index}.sigmf-data"
            raw.write_bytes(f"raw-{role}-{repeat_index}".encode())
            metadata = tmp_path / f"x-{role}-{repeat_index}.sigmf-meta"
            metadata.write_text(
                json.dumps({"role": role, "repeat_index": repeat_index}), encoding="utf-8"
            )
            condition = tmp_path / f"x-{role}-{repeat_index}-condition.json"
            condition.write_text(
                json.dumps({"role": role, "repeat_index": repeat_index}), encoding="utf-8"
            )
            stream = f"stream-{role}-{repeat_index}"
            raw_file = _file(raw)
            captures.append(
                {
                    "stream_id": stream,
                    "raw_iq_file": raw_file,
                    "metadata_file": _file(metadata),
                    "condition_record_file": _file(condition),
                    "abi2_continuity_verified": True,
                    "measurement_quality_passed": True,
                }
            )
            streams.append(stream)
            raw_files.append(raw_file)
            role_repeats.append(
                {
                    "repeat_index": repeat_index,
                    "condition_id": f"{role}-condition-{repeat_index}",
                    "stream_id": stream,
                    "raw_iq_sha256": raw_file["sha256"],
                    "quality_passed": True,
                    "rx1_amplitude_counts": 1000.0,
                    "transfer_detected": True,
                    "transfer_amplitude_ratio": (0.1 if role.endswith("baseline") else 0.04),
                    "transfer_amplitude_upper_bound_ratio": None,
                }
            )
        support_repeats[role] = role_repeats
        context = plan_contexts[role]
        safe_state = {
            "status": "mailbox_all_off_verified",
            "topology_stage": context["topology_stage"],
            "mailbox_all_off_verified": True,
        }
        manifest = tmp_path / f"x-{role}-manifest.json"
        baseline = role.endswith("baseline")
        acquisition_index = 41 + index
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "run_kind": "5g8_x_intervention_capture_v1",
                    "contract_id": contract_id,
                    "run_role": role,
                    "run_id": x_plans[role]["run_id"],
                    "status": "accepted",
                    "captured_at": f"2026-08-29T12:0{index}:00+00:00",
                    "acquisition_index": acquisition_index,
                    "freshness_epoch_id": "fixture-epoch-17",
                    "intervention_state_fixture_revision_sha256": (
                        before_binding["fixture_revision_sha256"]
                        if baseline
                        else after_binding["fixture_revision_sha256"]
                    ),
                    "topology_stage": context["topology_stage"],
                    "topology_fixture_sha256": context["topology_fixture_sha256"],
                    "source_commit": SOURCE,
                    "dependency_commit": DEPENDENCY,
                    "selector_evidence_sha256": context["selector_evidence_sha256"],
                    "immutable_plan_file": x_plans[role]["plan_file"],
                    "captures": captures,
                    "measurement_quality_rejection_reasons": [],
                    "final_mute_verified": True,
                    "final_selector_safe_state": safe_state,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        manifest_file = _file(manifest)
        manifest_hashes[role] = manifest_file["sha256"]
        x_runs[role] = {
            "schema": 1,
            "binding_kind": "5g8_accepted_x_run_binding_v1",
            "contract_id": contract_id,
            "change_plan_sha256": change_plan_file["sha256"],
            "run_role": role,
            "run_id": x_plans[role]["run_id"],
            "captured_at": f"2026-08-29T12:0{index}:00+00:00",
            "acquisition_index": acquisition_index,
            "freshness_epoch_id": "fixture-epoch-17",
            "intervention_state_fixture_revision_sha256": (
                before_binding["fixture_revision_sha256"]
                if baseline
                else after_binding["fixture_revision_sha256"]
            ),
            "topology_stage": context["topology_stage"],
            "topology_fixture_sha256": context["topology_fixture_sha256"],
            "source_commit": SOURCE,
            "dependency_commit": DEPENDENCY,
            "selector_evidence_sha256": context["selector_evidence_sha256"],
            "plan_file": x_plans[role]["plan_file"],
            "manifest_file": manifest_file,
            "stream_ids": streams,
            "raw_iq_files": raw_files,
            "acceptance_revalidated": True,
        }
    installed_setup = role_evidence["full_fixture_intervention"]["setup_attestation"]
    setup = Path(installed_setup["setup_attestation_file"]["path"])
    photograph = Path(installed_setup["setup_evidence"]["path"])
    installation = tmp_path / "installed-after-attestation.json"
    installation.write_text(
        json.dumps(
            {
                "schema": 1,
                "evidence_kind": "5g8_installed_after_state_attestation_v1",
                "contract_id": contract_id,
                "observed_at": "2026-08-29T12:05:00+00:00",
                "installed_state": "after",
                "fixture_revision_sha256": after_binding["fixture_revision_sha256"],
                "fixture_manifest_file": _file(after_path),
                "setup_attestation_file": _file(setup),
                "setup_evidence_file": _file(photograph),
                "accepted": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
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
            "sha256": _file(repository / relative)["sha256"],
            "size_bytes": (repository / relative).stat().st_size,
        }
        for relative in support_source_paths
    ]
    plan_source = contract["source"]
    dependency_runtime = copy.deepcopy(plan_source["pluto_plus_utils_source_attestation"])
    native_runtime = copy.deepcopy(plan_source["native_libiio_runtime_attestation"])
    source_identity = {
        "smateway_commit": SOURCE,
        "dependency_commit": DEPENDENCY,
        "dependency_attestation_sha256": canonical_sha256(dependency_runtime),
        "native_attestation_sha256": canonical_sha256(native_runtime),
        "selector_evidence_sha256": selector_binding["sha256"],
    }
    analysis_runtime = {
        "source": {
            "schema": 1,
            "repository": str(repository),
            "commit": SOURCE,
            "clean_source_files_verified": True,
            "files": source_files,
            "source_files_sha256": canonical_sha256(source_files),
        },
        "dependency": dependency_runtime,
        "native": native_runtime,
        "source_commit": SOURCE,
        "dependency_commit": DEPENDENCY,
        "native_attestation_sha256": canonical_sha256(native_runtime),
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
    manifest_files = {role: x_runs[role]["manifest_file"] for role in roles}
    analysis_input_identity = {
        "change_plan_file": change_plan_file,
        "x_run_manifest_files": manifest_files,
        "x_run_source_identity": source_identity,
        "analysis_runtime_identity": {
            "source_commit": SOURCE,
            "source_files_sha256": analysis_runtime["source"]["source_files_sha256"],
            "dependency_commit": DEPENDENCY,
            "dependency_attestation_sha256": canonical_sha256(dependency_runtime),
            "native_attestation_sha256": canonical_sha256(native_runtime),
        },
    }
    analysis = tmp_path / "support-analysis.json"
    analysis.write_text(
        json.dumps(
            {
                "schema": 1,
                "analysis_kind": "5g8_intervention_support_analysis_v1",
                "created_at": "2026-08-29T12:04:30+00:00",
                "contract_id": contract_id,
                "change_plan_file": change_plan_file,
                "x_run_manifest_files": manifest_files,
                "x_run_manifest_sha256s": manifest_hashes,
                "x_run_source_identity": source_identity,
                "analysis_runtime": analysis_runtime,
                "normalized_repeats": support_repeats,
                "qualification": qualification,
                "input_identity_sha256": canonical_sha256(analysis_input_identity),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    support = tmp_path / "support-result.json"
    support.write_text(
        json.dumps(
            {
                "schema": 1,
                "result_kind": "5g8_intervention_support_result_v1",
                "contract_id": contract_id,
                "decision": "supported_fix",
                "accepted": True,
                "x_run_manifest_sha256s": manifest_hashes,
                "analysis_file": _file(analysis),
                "simultaneous_improvement_gate_passed": True,
                "rejection_reasons": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    restoration_photo = tmp_path / "default-restoration-photo.png"
    restoration_photo.write_bytes(b"restored-before")
    reapplication_photo = tmp_path / "default-reapplication-photo.png"
    reapplication_photo.write_bytes(b"reapplied-after")

    def transition(path: Path, observed_at: str, source: str, target: str, photo: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "evidence_kind": "5g8_fixture_state_transition_attestation_v1",
                    "contract_id": contract_id,
                    "observed_at": observed_at,
                    "from_fixture_revision_sha256": source,
                    "to_fixture_revision_sha256": target,
                    "setup_evidence_file": _file(photo),
                    "accepted": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    restored = transition(
        tmp_path / "default-restoration.json",
        "2026-08-29T12:01:30+00:00",
        after_binding["fixture_revision_sha256"],
        before_binding["fixture_revision_sha256"],
        restoration_photo,
    )
    reapplied = transition(
        tmp_path / "default-reapplication.json",
        "2026-08-29T12:02:30+00:00",
        before_binding["fixture_revision_sha256"],
        after_binding["fixture_revision_sha256"],
        reapplication_photo,
    )
    return {
        "schema": 2,
        "contract_kind": INTERVENTION_KIND,
        "contract_id": contract_id,
        "sealed_at": "2026-08-29T12:06:00+00:00",
        "change_plan_file": change_plan_file,
        "change_plan_sha256": change_plan_file["sha256"],
        "x_runs": x_runs,
        "diagnostic_restoration": {
            "status": "restored_then_reapplied",
            "restoration_evidence_file": _file(restored),
            "reapplication_evidence_file": _file(reapplied),
        },
        "adoption": {
            "decision": "adopt_supported_fix",
            "installed_state": "after",
            "installed_fixture_revision_sha256": after_binding["fixture_revision_sha256"],
            "installation_attestation_file": _file(installation),
        },
        "support_evidence_file": _file(support),
    }


def _validated_fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    return validate_full_simultaneous_fixture(_fixture_document(tmp_path))


def _installed_fixture(document: Mapping[str, Any]):  # type: ignore[no-untyped-def]
    plan_path = Path(str(document["change_plan_file"]["path"]))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    return validate_full_simultaneous_fixture(plan["installed_after_fixture"])


def test_full_simultaneous_fixture_green_and_rejects_direct_one_hot(tmp_path: Path) -> None:
    document = _fixture_document(tmp_path)
    fixture = validate_full_simultaneous_fixture(document)
    assert fixture.fixture_revision_sha256 == document["fixture_revision_sha256"]
    bad = copy.deepcopy(document)
    bad["topology_label"] = "direct_one_hot"
    bad["direct_one_hot"] = True
    bad["fixture_revision_sha256"] = canonical_sha256(
        {
            "fixture_manifest_sha256": bad["fixture_manifest_sha256"],
            "board_id": bad["board_id"],
            "hardware_revision": bad["hardware_revision"],
            "pluto_serial": bad["pluto_serial"],
            "component_ids": bad["component_ids"],
            "connection_ids": bad["connection_ids"],
            "antenna_port_order": bad["antenna_port_order"],
            "topology_label": bad["topology_label"],
        }
    )
    with pytest.raises(SelectedStateQualificationError, match="never direct-one-hot"):
        validate_full_simultaneous_fixture(bad)


def test_fixture_rejects_missing_simultaneous_branch(tmp_path: Path) -> None:
    document = _fixture_document(tmp_path)
    document["connection_ids"]["eight_way_to_selector"].pop()
    with pytest.raises(SelectedStateQualificationError, match="eight unique"):
        validate_full_simultaneous_fixture(document)


def test_fixture_binding_rejects_graph_facts_not_in_manifest(tmp_path: Path) -> None:
    document = _fixture_document(tmp_path)
    document["component_ids"]["selector"] = "different-selector"
    with pytest.raises(SelectedStateQualificationError, match="differ from the fixture-v2"):
        validate_full_simultaneous_fixture(document)


def test_selector_snapshot_role_is_exact() -> None:
    snapshot = asdict(_selector("bench"))
    assert validate_selector_binding_snapshot(snapshot, expected_role="bench").image_role == "bench"
    with pytest.raises(SelectedStateQualificationError, match="requires exact fast20"):
        validate_selector_binding_snapshot(snapshot, expected_role="fast20")


def test_single_variable_intervention_green(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    fixture = _installed_fixture(document)
    result = validate_intervention_contract(document, fixture=fixture)
    assert result.changed_property_path.endswith("supply_current_limit_a")
    assert result.before == 0.4
    assert result.after == 0.5
    assert result.installed_after_fixture_revision_sha256 == fixture.fixture_revision_sha256


def _rewrite_x_plan_contract(
    change_plan: dict[str, Any],
    *,
    role: str,
    mutation: str,
) -> None:
    binding = change_plan["x_run_plans"][role]
    path = Path(binding["plan_file"]["path"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    contract = envelope["plan_contract"]
    if mutation == "minimal":
        retained = {
            "run_id",
            "topology_stage",
            "board_id",
            "configuration",
            "fixture_evidence",
            "fixture_evidence_sha256",
            "selector_control",
            "x_intervention_prebinding",
            "x_intervention_capture_context",
        }
        envelope["plan_contract"] = {
            key: value for key, value in contract.items() if key in retained
        }
    elif mutation == "source_missing":
        del contract["source"]
    elif mutation == "source_changed":
        contract["source"]["analyzer"] = "synthetic.unreviewed_analyzer"
    elif mutation == "configuration":
        contract["configuration"]["center_frequency_hz"] = 5_700_000_000
    elif mutation == "conditions":
        contract["conditions"][0]["sample_rate_hz"] = 2_000_000
    elif mutation == "storage":
        contract["storage"]["medium"] = "pluto_onboard_storage"
    else:  # pragma: no cover - test helper is closed over the parameters below
        raise AssertionError(f"unsupported test mutation: {mutation}")
    envelope["plan_contract_sha256"] = canonical_sha256(envelope["plan_contract"])
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    binding["plan_file"] = _file(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("minimal", "production X leakage-runner contract keys differ"),
        ("source_missing", "production X leakage-runner contract keys differ"),
        ("source_changed", "production X source does not name"),
        ("configuration", "production X configuration differs"),
        ("conditions", "production X conditions differ"),
        ("storage", "production X storage is not the fixed local-Raspberry-Pi"),
    ),
)
def test_intervention_rejects_self_hashed_nonproduction_x_contract(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    document = _intervention_document(tmp_path)
    plan_path = Path(document["change_plan_file"]["path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    _rewrite_x_plan_contract(plan, role="boundary_baseline", mutation=mutation)

    with pytest.raises(SelectedStateQualificationError, match=message):
        validate_intervention_change_plan(plan)


def test_production_x_rejects_opaque_selector_source(tmp_path: Path) -> None:
    opaque = tmp_path / "opaque-selector-evidence.json"
    opaque.write_text(
        json.dumps({"sealed": True}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    binding = {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": str(opaque.absolute()),
        "sha256": _file(opaque)["sha256"],
        "campaign_id": CAMPAIGN,
        "run_id": "opaque-selector-r01",
        "board_id": BOARD,
        "image_role": "bench",
    }

    with pytest.raises(SelectedStateQualificationError, match="selector flash evidence"):
        _sealed_x_selector(binding, campaign_id=CAMPAIGN, board_id=BOARD)


def test_intervention_rejects_provenance_only_revision_as_physical_change(
    tmp_path: Path,
) -> None:
    document = _intervention_document(tmp_path, after_supply_current_limit_a=0.4)
    plan = json.loads(Path(document["change_plan_file"]["path"]).read_text(encoding="utf-8"))

    with pytest.raises(SelectedStateQualificationError, match="exactly the predeclared leaf"):
        validate_intervention_change_plan(plan)


def test_intervention_rejects_second_physical_leaf_in_after_fixture(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path, after_splitter_power_dbm=21.0)
    plan = json.loads(Path(document["change_plan_file"]["path"]).read_text(encoding="utf-8"))

    with pytest.raises(SelectedStateQualificationError, match="exactly the predeclared leaf"):
        validate_intervention_change_plan(plan)


def test_intervention_rejects_empty_or_unrelated_installed_setup_attestation(
    tmp_path: Path,
) -> None:
    document = _intervention_document(tmp_path)
    empty_setup = tmp_path / "unrelated-empty-setup.json"
    empty_setup.write_text("{}", encoding="utf-8")
    installation_path = Path(document["adoption"]["installation_attestation_file"]["path"])
    installation = json.loads(installation_path.read_text(encoding="utf-8"))
    installation["setup_attestation_file"] = _file(empty_setup)
    installation_path.write_text(json.dumps(installation, sort_keys=True), encoding="utf-8")
    document["adoption"]["installation_attestation_file"] = _file(installation_path)

    with pytest.raises(SelectedStateQualificationError, match="installed after-state"):
        validate_intervention_contract(document)


def test_intervention_rejects_installed_setup_evidence_tamper(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    unrelated_evidence = tmp_path / "unrelated-setup-evidence.txt"
    unrelated_evidence.write_text("different fixture observation\n", encoding="utf-8")
    installation_path = Path(document["adoption"]["installation_attestation_file"]["path"])
    installation = json.loads(installation_path.read_text(encoding="utf-8"))
    installation["setup_evidence_file"] = _file(unrelated_evidence)
    installation_path.write_text(json.dumps(installation, sort_keys=True), encoding="utf-8")
    document["adoption"]["installation_attestation_file"] = _file(installation_path)

    with pytest.raises(SelectedStateQualificationError, match="installed after-state"):
        validate_intervention_contract(document)


def test_intervention_rejects_changed_predeclared_plan_bytes(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    path = Path(document["change_plan_file"]["path"])
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SelectedStateQualificationError, match="differs from its binding"):
        validate_intervention_contract(document)


def test_intervention_component_must_exist_in_bound_fixture(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    wrong_fixture = replace(_installed_fixture(document), fixture_revision_sha256="f" * 64)
    with pytest.raises(SelectedStateQualificationError, match="installed after-state"):
        validate_intervention_contract(document, fixture=wrong_fixture)


def test_intervention_rejects_restored_baseline_as_adopted_state(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    plan = json.loads(Path(document["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    document["adoption"]["installed_fixture_revision_sha256"] = plan["before_fixture"][
        "fixture_revision_sha256"
    ]
    with pytest.raises(SelectedStateQualificationError, match="after-state"):
        validate_intervention_contract(document)


def test_diagnostic_restoration_requires_source_bound_after_reapplication(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    plan = json.loads(Path(document["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    before = plan["before_fixture"]["fixture_revision_sha256"]
    after = plan["installed_after_fixture"]["fixture_revision_sha256"]
    restoration_photo = tmp_path / "restoration-photo.png"
    restoration_photo.write_bytes(b"restored-before")
    reapplication_photo = tmp_path / "reapplication-photo.png"
    reapplication_photo.write_bytes(b"reapplied-after")

    def transition(path: Path, observed_at: str, source: str, target: str, photo: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "evidence_kind": "5g8_fixture_state_transition_attestation_v1",
                    "contract_id": document["contract_id"],
                    "observed_at": observed_at,
                    "from_fixture_revision_sha256": source,
                    "to_fixture_revision_sha256": target,
                    "setup_evidence_file": _file(photo),
                    "accepted": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    restored = transition(
        tmp_path / "restored.json",
        "2026-08-29T12:01:30+00:00",
        after,
        before,
        restoration_photo,
    )
    reapplied = transition(
        tmp_path / "reapplied.json",
        "2026-08-29T12:02:30+00:00",
        before,
        after,
        reapplication_photo,
    )
    document["diagnostic_restoration"] = {
        "status": "restored_then_reapplied",
        "restoration_evidence_file": _file(restored),
        "reapplication_evidence_file": _file(reapplied),
    }
    assert validate_intervention_contract(document).diagnostic_restoration_status == (
        "restored_then_reapplied"
    )

    broken = copy.deepcopy(document)
    broken_reapplication = json.loads(reapplied.read_text(encoding="utf-8"))
    broken_reapplication["to_fixture_revision_sha256"] = before
    reapplied.write_text(json.dumps(broken_reapplication, sort_keys=True), encoding="utf-8")
    broken["diagnostic_restoration"]["reapplication_evidence_file"] = _file(reapplied)
    with pytest.raises(SelectedStateQualificationError, match="reapplication transition"):
        validate_intervention_contract(broken)


def test_four_role_x_rejects_missing_fixture_state_transitions(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    document["diagnostic_restoration"] = {
        "status": "not_performed",
        "restoration_evidence_file": None,
        "reapplication_evidence_file": None,
    }
    with pytest.raises(SelectedStateQualificationError, match="four-role X evidence requires"):
        validate_intervention_contract(document)


@pytest.mark.parametrize(
    ("evidence_name", "observed_at", "message"),
    [
        ("restoration_evidence_file", "2026-08-29T12:00:30+00:00", "restoration must occur"),
        (
            "reapplication_evidence_file",
            "2026-08-29T12:01:45+00:00",
            "reapplication must occur",
        ),
    ],
)
def test_four_role_x_transitions_must_bracket_the_exact_role_boundaries(
    tmp_path: Path, evidence_name: str, observed_at: str, message: str
) -> None:
    document = _intervention_document(tmp_path)
    binding = document["diagnostic_restoration"][evidence_name]
    path = Path(binding["path"])
    transition = json.loads(path.read_text(encoding="utf-8"))
    transition["observed_at"] = observed_at
    path.write_text(json.dumps(transition, sort_keys=True), encoding="utf-8")
    document["diagnostic_restoration"][evidence_name] = _file(path)
    with pytest.raises(SelectedStateQualificationError, match=message):
        validate_intervention_contract(document)


def test_x_change_plan_recursively_rejects_replace_placeholder(tmp_path: Path) -> None:
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    plan["change"]["restore_instruction"] = "nested REPLACE_EXACT_REVERSAL_INSTRUCTION"
    with pytest.raises(SelectedStateQualificationError, match="REPLACE placeholder"):
        validate_intervention_change_plan(plan)


@pytest.mark.parametrize(
    ("component_role", "property_path", "implicated_stage"),
    [
        ("pluto", "/shared_fixture/pluto/model", "direct_rx2_termination"),
        (
            "two_way_splitter",
            "/shared_fixture/tx1_reference_splitter/model",
            "rx2_cable_terminated",
        ),
        (
            "selector",
            "/stage_delta/components/selector/supply_current_limit_a",
            "powered_selector_all_inputs_terminated",
        ),
        (
            "eight_way_splitter",
            "/stage_delta/components/eight_way_splitter/model",
            FULL_CONDUCTED_STAGE,
        ),
    ],
)
def test_x_change_stage_compatibility_accepts_only_present_component_roles(
    component_role: str, property_path: str, implicated_stage: str
) -> None:
    _validate_change_stage_compatibility(
        component_role=component_role,
        property_path=property_path,
        implicated_stage=implicated_stage,
    )


@pytest.mark.parametrize(
    ("component_role", "property_path", "implicated_stage"),
    [
        (
            "selector",
            "/stage_delta/components/selector/supply_current_limit_a",
            "direct_rx2_termination",
        ),
        (
            "selector",
            "/stage_delta/components/selector/supply_current_limit_a",
            "rx2_cable_terminated",
        ),
        (
            "eight_way_splitter",
            "/stage_delta/components/eight_way_splitter/model",
            "powered_selector_all_inputs_terminated",
        ),
    ],
)
def test_x_change_stage_compatibility_rejects_absent_component_roles(
    component_role: str, property_path: str, implicated_stage: str
) -> None:
    with pytest.raises(SelectedStateQualificationError, match="absent from implicated stage"):
        _validate_change_stage_compatibility(
            component_role=component_role,
            property_path=property_path,
            implicated_stage=implicated_stage,
        )


def _rewrite_x_plan(change_plan: dict[str, Any], role: str, x_plan: dict[str, Any]) -> None:
    path = Path(change_plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan["plan_contract_sha256"] = canonical_sha256(x_plan["plan_contract"])
    path.write_text(json.dumps(x_plan, sort_keys=True), encoding="utf-8")
    change_plan["x_run_plans"][role]["plan_file"] = _file(path)


def test_x_change_plan_rejects_component_absent_from_implicated_stage(tmp_path: Path) -> None:
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    plan["implicated_boundary_stage"] = "direct_rx2_termination"

    with pytest.raises(SelectedStateQualificationError, match="absent from implicated stage"):
        validate_intervention_change_plan(plan)


@pytest.mark.parametrize(
    ("role", "wrong_state"),
    [
        ("boundary_baseline", 0.5),
        ("boundary_intervention", 0.4),
    ],
)
def test_x_change_plan_rejects_wrong_role_state_in_embedded_topology(
    tmp_path: Path, role: str, wrong_state: float
) -> None:
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    fixture = x_plan["plan_contract"]["fixture_evidence"]
    fixture["stage_delta"]["components"]["selector"]["supply_current_limit_a"] = wrong_state
    fixture["stage_delta_sha256"] = canonical_sha256(fixture["stage_delta"])
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(fixture)
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="production fixture/capture linkage|declared component/property state",
    ):
        validate_intervention_change_plan(plan)


def test_x_change_plan_rejects_missing_component_inventory_entry(tmp_path: Path) -> None:
    role = "boundary_baseline"
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    fixture = x_plan["plan_contract"]["fixture_evidence"]
    fixture["component_ids"].remove("selector-v5-01")
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(fixture)
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="source-derived production projection|inventory is not derived",
    ):
        validate_intervention_change_plan(plan)


def test_x_boundary_rejects_valid_but_substituted_shared_fixture_chain(tmp_path: Path) -> None:
    role = "boundary_baseline"
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    substituted = _production_fixture_chain(
        tmp_path / "substituted-boundary-chain",
        splitter_power_dbm=21.0,
        supply_current_limit_a=0.4,
        run_prefix="substituted",
    )
    evidence = copy.deepcopy(substituted["powered_selector_all_inputs_terminated"]["evidence"])
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    x_plan["plan_contract"]["run_id"] = evidence["run_id"]
    x_plan["plan_contract"]["fixture_evidence"] = evidence
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(evidence)
    x_plan["plan_contract"]["x_intervention_capture_context"]["selector_flash_evidence"] = (
        substituted["selector"]
    )
    plan["x_run_plans"][role]["run_id"] = evidence["run_id"]
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="exact capture-state physical graph",
    ):
        validate_intervention_change_plan(plan)


def test_x_plan_rejects_opaque_setup_even_when_embedded_hashes_are_resealed(
    tmp_path: Path,
) -> None:
    role = "full_fixture_baseline"
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    fixture = x_plan["plan_contract"]["fixture_evidence"]
    setup_path = Path(fixture["source_files"]["setup_attestation"]["path"])
    setup_path.write_text("{}\n", encoding="utf-8")
    fixture["source_files"]["setup_attestation"] = _file(setup_path)
    fixture["setup_attestation"] = {}
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(fixture)
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="per-run setup attestation fields",
    ):
        validate_intervention_change_plan(plan)


def test_change_plan_rejects_tampered_full_fixture_prior_plan_binding(tmp_path: Path) -> None:
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    manifest_path = Path(plan["before_fixture"]["fixture_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prior_stage_binding"]["plan_file_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    plan["before_fixture"]["fixture_manifest_sha256"] = _file(manifest_path)["sha256"]

    with pytest.raises(
        SelectedStateQualificationError,
        match="prior-stage plan file differs",
    ):
        validate_intervention_change_plan(plan)


def test_x_change_plan_rejects_source_graph_different_from_embedded_topology(
    tmp_path: Path,
) -> None:
    role = "boundary_baseline"
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    fixture = x_plan["plan_contract"]["fixture_evidence"]
    source_path = Path(fixture["source_files"]["fixture_manifest"]["path"])
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["stage_delta"]["components"]["selector"]["supply_current_limit_a"] = 0.5
    source_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    fixture["source_files"]["fixture_manifest"] = _file(source_path)
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(fixture)
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="path/size/hash binding|source graph differs",
    ):
        validate_intervention_change_plan(plan)


def test_x_full_role_requires_exact_capture_state_fixture_source(tmp_path: Path) -> None:
    role = "full_fixture_baseline"
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    x_path = Path(plan["x_run_plans"][role]["plan_file"]["path"])
    x_plan = json.loads(x_path.read_text(encoding="utf-8"))
    fixture = x_plan["plan_contract"]["fixture_evidence"]
    original = Path(fixture["source_files"]["fixture_manifest"]["path"])
    copied = tmp_path / "copied-full-baseline-fixture.json"
    copied.write_bytes(original.read_bytes())
    fixture["source_files"]["fixture_manifest"] = _file(copied)
    x_plan["plan_contract"]["fixture_evidence_sha256"] = canonical_sha256(fixture)
    _rewrite_x_plan(plan, role, x_plan)

    with pytest.raises(
        SelectedStateQualificationError,
        match="capture-revision manifest|differs from its capture state",
    ):
        validate_intervention_change_plan(plan)


def test_x_immutable_plan_recursively_rejects_nested_replace_placeholder(tmp_path: Path) -> None:
    contract = _intervention_document(tmp_path)
    plan = json.loads(Path(contract["change_plan_file"]["path"]).read_text(encoding="utf-8"))
    binding = plan["x_run_plans"]["boundary_baseline"]
    x_plan_path = Path(binding["plan_file"]["path"])
    x_plan = json.loads(x_plan_path.read_text(encoding="utf-8"))
    x_plan["plan_contract"]["x_intervention_capture_context"]["freshness_epoch_id"] = (
        "REPLACE_COMMON_FRESHNESS_EPOCH_ID"
    )
    x_plan["plan_contract_sha256"] = canonical_sha256(x_plan["plan_contract"])
    x_plan_path.write_text(json.dumps(x_plan, sort_keys=True), encoding="utf-8")
    binding["plan_file"] = _file(x_plan_path)
    with pytest.raises(SelectedStateQualificationError, match="REPLACE placeholder"):
        validate_intervention_change_plan(plan)


def test_x_manifest_recursively_rejects_nested_replace_placeholder(tmp_path: Path) -> None:
    document = _intervention_document(tmp_path)
    binding = document["x_runs"]["boundary_baseline"]
    manifest_path = Path(binding["manifest_file"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["captures"][0]["stream_id"] = "REPLACE_STREAM_ID"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    binding["manifest_file"] = _file(manifest_path)
    with pytest.raises(SelectedStateQualificationError, match="REPLACE placeholder"):
        validate_intervention_contract(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda analysis: analysis["qualification"].update(
                simultaneous_improvement_gate_passed=False
            ),
            "independently recomputed fixed gate",
        ),
        (
            lambda analysis: analysis["x_run_source_identity"].update(smateway_commit="d" * 40),
            "source identity differs",
        ),
        (
            lambda analysis: analysis["normalized_repeats"]["boundary_baseline"][0].update(
                raw_iq_sha256="e" * 64
            ),
            "not source-bound to admitted X captures",
        ),
        (
            lambda analysis: analysis["normalized_repeats"]["boundary_baseline"][0].update(
                condition_id="REPLACE_CONDITION_ID"
            ),
            "REPLACE placeholder",
        ),
    ],
)
def test_intervention_support_rejects_gate_provenance_or_source_tamper(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    document = _intervention_document(tmp_path)
    support_path = Path(document["support_evidence_file"]["path"])
    support = json.loads(support_path.read_text(encoding="utf-8"))
    analysis_path = Path(support["analysis_file"]["path"])
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    mutate(analysis)
    analysis_path.write_text(json.dumps(analysis, sort_keys=True), encoding="utf-8")
    support["analysis_file"] = _file(analysis_path)
    support_path.write_text(json.dumps(support, sort_keys=True), encoding="utf-8")
    document["support_evidence_file"] = _file(support_path)
    with pytest.raises(SelectedStateQualificationError, match=message):
        validate_intervention_contract(document)


@pytest.mark.parametrize("runtime_name", ("dependency", "native"))
def test_support_cannot_reseal_forged_runtime_over_immutable_x_plan_authority(
    tmp_path: Path,
    runtime_name: str,
) -> None:
    document = _intervention_document(tmp_path)
    support_path = Path(document["support_evidence_file"]["path"])
    support = json.loads(support_path.read_text(encoding="utf-8"))
    analysis_path = Path(support["analysis_file"]["path"])
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    runtime = analysis["analysis_runtime"]
    source_identity = analysis["x_run_source_identity"]
    if runtime_name == "dependency":
        forged = {"schema": 999, "commit": DEPENDENCY}
        runtime["dependency"] = forged
        source_identity["dependency_attestation_sha256"] = canonical_sha256(forged)
    else:
        forged = {"schema": 999}
        runtime["native"] = forged
        forged_sha = canonical_sha256(forged)
        runtime["native_attestation_sha256"] = forged_sha
        source_identity["native_attestation_sha256"] = forged_sha
    analysis["input_identity_sha256"] = canonical_sha256(
        {
            "change_plan_file": analysis["change_plan_file"],
            "x_run_manifest_files": analysis["x_run_manifest_files"],
            "x_run_source_identity": source_identity,
            "analysis_runtime_identity": {
                "source_commit": runtime["source_commit"],
                "source_files_sha256": runtime["source"]["source_files_sha256"],
                "dependency_commit": runtime["dependency_commit"],
                "dependency_attestation_sha256": source_identity["dependency_attestation_sha256"],
                "native_attestation_sha256": source_identity["native_attestation_sha256"],
            },
        }
    )
    analysis_path.write_text(json.dumps(analysis, sort_keys=True), encoding="utf-8")
    support["analysis_file"] = _file(analysis_path)
    support_path.write_text(json.dumps(support, sort_keys=True), encoding="utf-8")
    document["support_evidence_file"] = _file(support_path)

    with pytest.raises(
        SelectedStateQualificationError,
        match="source identity differs from immutable production X plans",
    ):
        validate_intervention_contract(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda doc: doc["x_runs"]["boundary_intervention"].update(acquisition_index=50),
            "binding differs",
        ),
        (
            lambda doc: doc["x_runs"]["boundary_intervention"].update(
                intervention_state_fixture_revision_sha256="f" * 64
            ),
            "wrong fixture revision",
        ),
        (
            lambda doc: doc["x_runs"]["boundary_intervention"].update(
                stream_ids=list(doc["x_runs"]["boundary_baseline"]["stream_ids"])
            ),
            "sealed streams differ",
        ),
    ],
)
def test_intervention_rejects_stale_or_non_disjoint_baseline(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    document = _intervention_document(tmp_path)
    mutation(document)
    with pytest.raises(SelectedStateQualificationError, match=message):
        validate_intervention_contract(document)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("source_commit", "c" * 40),
        ("dependency_commit", "d" * 40),
    ),
)
def test_accepted_x_identity_cannot_override_immutable_plan_source_authority(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    document = _intervention_document(tmp_path)
    binding = document["x_runs"]["boundary_baseline"]
    manifest_path = Path(binding["manifest_file"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = replacement
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    binding[field] = replacement
    binding["manifest_file"] = _file(manifest_path)

    assert binding[field] == manifest[field]
    with pytest.raises(
        SelectedStateQualificationError,
        match="binding differs from source evidence",
    ):
        validate_intervention_contract(document)


def test_static_bench_green(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _static_document(fixture.fixture_revision_sha256)
    result = qualify_static_bench(document, fixture=fixture, selector=selector)
    assert result["accepted"] is True
    assert result["observation_count"] == 9
    assert result["final_all_off_verified"] is True


def test_static_rejects_selected_state_readback_failure(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _static_document(fixture.fixture_revision_sha256)
    document["observations"][4]["command"]["applied_state"] = "ANT8"
    result = qualify_static_bench(document, fixture=fixture, selector=selector)
    assert result["accepted"] is False
    assert "ant4_selected_state_readback_failed" in result["rejection_reasons"]


def test_static_rejects_incomplete_or_reused_state_streams(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _static_document(fixture.fixture_revision_sha256)
    document["observations"].pop()
    with pytest.raises(SelectedStateQualificationError, match="exactly nine"):
        qualify_static_bench(document, fixture=fixture, selector=selector)

    document, selector = _static_document(fixture.fixture_revision_sha256)
    document["observations"][1]["capture"] = copy.deepcopy(document["observations"][0]["capture"])
    with pytest.raises(SelectedStateQualificationError, match="reuses stream IDs"):
        qualify_static_bench(document, fixture=fixture, selector=selector)


def test_static_requires_bench_image_and_final_all_off(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, _ = _static_document(fixture.fixture_revision_sha256)
    with pytest.raises(SelectedStateQualificationError, match="exact bench"):
        qualify_static_bench(document, fixture=fixture, selector=_selector("fast20"))
    document, selector = _static_document(fixture.fixture_revision_sha256)
    document["final_all_off_readback"]["passed"] = False
    result = qualify_static_bench(document, fixture=fixture, selector=selector)
    assert result["accepted"] is False
    assert "static_final_all_off_not_verified" in result["rejection_reasons"]


def test_fast20_timing_green(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _timing_document(fixture.fixture_revision_sha256)
    result = qualify_fast20_timing(document, fixture=fixture, selector=selector)
    assert result["accepted"] is True
    assert result["timing_run_count"] == 2
    assert all(run["accepted"] for run in result["runs"])


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("quality", "metadata_abi"), 1, "metadata_abi_not_2"),
        (("quality", "observed_sample_count"), 9_999_999, "sample_count_not_exact"),
        (("quality", "continuity_verified"), False, "continuity_not_verified"),
        (("quality", "clipped_sample_count"), 1, "clipped_samples_nonzero"),
        (
            ("timing", "isolation_verified"),
            False,
            "fast20_schedule_timing_not_verified",
        ),
        (("timing", "threshold_stable"), False, "fast20_schedule_timing_not_verified"),
    ],
)
def test_fast20_timing_independent_run_gates(
    tmp_path: Path, path: tuple[str, str], value: Any, reason: str
) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _timing_document(fixture.fixture_revision_sha256)
    document["runs"][1][path[0]][path[1]] = value
    result = qualify_fast20_timing(document, fixture=fixture, selector=selector)
    assert result["accepted"] is False
    assert any(reason in item for item in result["rejection_reasons"])
    assert result["runs"][0]["accepted"] is True
    assert result["runs"][1]["accepted"] is False


def test_fast20_timing_rejects_bad_dwell_and_final_cleanup(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _timing_document(fixture.fixture_revision_sha256)
    document["runs"][0]["timing"]["dwell_by_state"]["ANT6"]["duration_max_ms"] = 50.0
    document["final_mute_verified"] = False
    document["final_fast20_schedule_verified"]["passed"] = False
    result = qualify_fast20_timing(document, fixture=fixture, selector=selector)
    assert result["accepted"] is False
    assert "run1_ant6_dwell_duration_or_count_failed" in result["rejection_reasons"]
    assert "timing_final_mute_not_verified" in result["rejection_reasons"]
    assert "timing_final_fast20_schedule_not_verified" in result["rejection_reasons"]


def test_fast20_timing_rejects_one_run_or_reused_stream(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _timing_document(fixture.fixture_revision_sha256)
    document["runs"].pop()
    with pytest.raises(SelectedStateQualificationError, match="exactly two"):
        qualify_fast20_timing(document, fixture=fixture, selector=selector)
    document, selector = _timing_document(fixture.fixture_revision_sha256)
    document["runs"][1]["capture"] = copy.deepcopy(document["runs"][0]["capture"])
    with pytest.raises(SelectedStateQualificationError, match="reuses stream IDs"):
        qualify_fast20_timing(document, fixture=fixture, selector=selector)


def test_fast20_matrix_green_meets_one_degree_goal(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(fixture.fixture_revision_sha256)
    result = qualify_fast20_matrix(
        document,
        fixture=fixture,
        selector=selector,
        bootstrap_draws=4_096,
    )
    assert result["accepted"] is True
    assert result["operational_matrix_accepted"] is True
    assert result["one_degree_matrix_accepted"] is True
    assert result["simultaneous_gates"]["minimum_c_raw_lower_95_db"] > 39.0
    assert result["simultaneous_gates"]["minimum_c_path_lower_95_db"] > 39.0


def test_matrix_requires_exact_state_set_and_five_fresh_streams(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(fixture.fixture_revision_sha256)
    document["streams"][2]["states"].pop("ANT8")
    with pytest.raises(SelectedStateQualificationError, match="keys differ"):
        qualify_fast20_matrix(document, fixture=fixture, selector=selector, bootstrap_draws=2_000)
    document, selector = _matrix_document(fixture.fixture_revision_sha256)
    document["streams"][1]["capture"] = copy.deepcopy(document["streams"][0]["capture"])
    with pytest.raises(SelectedStateQualificationError, match="reuses stream IDs"):
        qualify_fast20_matrix(document, fixture=fixture, selector=selector, bootstrap_draws=2_000)


def test_matrix_rejects_timing_stream_reuse(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(fixture.fixture_revision_sha256)
    forbidden = document["streams"][3]["capture"]["stream_id"]
    with pytest.raises(SelectedStateQualificationError, match="prior stream"):
        qualify_fast20_matrix(
            document,
            fixture=fixture,
            selector=selector,
            forbidden_stream_ids=[forbidden],
            bootstrap_draws=2_000,
        )


def test_matrix_reports_operational_but_not_one_degree_release(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(fixture.fixture_revision_sha256, selected_magnitude=0.021)
    result = qualify_fast20_matrix(
        document, fixture=fixture, selector=selector, bootstrap_draws=4_096
    )
    assert result["accepted"] is True
    assert result["simultaneous_gates"]["c_raw_at_least_20db"] is True
    assert result["simultaneous_gates"]["c_path_at_least_20db"] is True
    assert result["one_degree_matrix_accepted"] is False


def test_matrix_distinguishes_c_raw_from_c_path(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(
        fixture.fixture_revision_sha256, selected_magnitude=0.0105
    )
    # Put every selected phasor on the same ray as ALL_OFF: subtraction loses 0.83 dB.
    for stream in document["streams"]:
        off = stream["states"]["ALL_OFF"]["h"]
        phase = 7.0
        for state in ANTENNA_STATES:
            stream["states"][state]["h"] = _complex_document(_phasor(0.0105, phase))
        off.update(_complex_document(_phasor(0.001, phase)))
    result = qualify_fast20_matrix(
        document, fixture=fixture, selector=selector, bootstrap_draws=2_000
    )
    assert result["simultaneous_gates"]["c_raw_at_least_20db"] is True
    assert result["simultaneous_gates"]["c_path_at_least_20db"] is False
    assert result["accepted"] is False


@pytest.mark.parametrize(
    ("amplitude_jitter", "phase_jitter", "reason"),
    [
        (0.08, 0.02, "amplitude_repeatability"),
        (0.0002, 4.0, "phase_repeatability"),
    ],
)
def test_matrix_repeatability_is_a_simultaneous_gate(
    tmp_path: Path, amplitude_jitter: float, phase_jitter: float, reason: str
) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(
        fixture.fixture_revision_sha256,
        amplitude_jitter=amplitude_jitter,
        phase_jitter_deg=phase_jitter,
    )
    result = qualify_fast20_matrix(
        document, fixture=fixture, selector=selector, bootstrap_draws=4_096
    )
    assert result["accepted"] is False
    assert any(reason in item for item in result["rejection_reasons"])


def test_matrix_quality_and_final_cleanup_are_blocking(tmp_path: Path) -> None:
    fixture = _validated_fixture(tmp_path)
    document, selector = _matrix_document(fixture.fixture_revision_sha256)
    document["streams"][0]["quality"]["clipped_sample_count"] = 3
    nondetection = document["streams"][1]["states"]["ANT3"]
    nondetection.update(
        detected=False,
        h=None,
        magnitude_upper_bound=0.002,
        coherence=None,
        phase_rms_deg=None,
    )
    document["final_fast20_schedule_verified"]["passed"] = False
    result = qualify_fast20_matrix(
        document, fixture=fixture, selector=selector, bootstrap_draws=2_000
    )
    assert result["accepted"] is False
    assert any("clipped_samples_nonzero" in reason for reason in result["rejection_reasons"])
    assert any("pilot_not_detected" in reason for reason in result["rejection_reasons"])
    assert "matrix_final_fast20_schedule_not_verified" in result["rejection_reasons"]


def _all_results(tmp_path: Path, *, selected_magnitude: float = 0.1):  # type: ignore[no-untyped-def]
    intervention_document = _intervention_document(tmp_path)
    fixture = _installed_fixture(intervention_document)
    intervention = validate_intervention_contract(intervention_document, fixture=fixture)
    static_document, bench_selector = _static_document(
        fixture.fixture_revision_sha256,
        device_identity_sha256=_hash("static-device-observation"),
        usb_uri="usb:1.2",
        selector_evidence_sha256=intervention.selector_evidence_sha256,
    )
    timing_document, fast20_selector = _timing_document(
        fixture.fixture_revision_sha256,
        device_identity_sha256=_hash("timing-device-observation"),
        usb_uri="usb:1.3",
    )
    matrix_document, _ = _matrix_document(
        fixture.fixture_revision_sha256,
        selected_magnitude=selected_magnitude,
        device_identity_sha256=_hash("matrix-device-observation"),
        usb_uri="usb:2.4",
    )
    static_result = qualify_static_bench(static_document, fixture=fixture, selector=bench_selector)
    timing_result = qualify_fast20_timing(
        timing_document, fixture=fixture, selector=fast20_selector
    )
    matrix_result = qualify_fast20_matrix(
        matrix_document,
        fixture=fixture,
        selector=fast20_selector,
        forbidden_stream_ids=timing_result["source_stream_ids"],
        forbidden_raw_iq_sha256s=timing_result["raw_iq_sha256s"],
        bootstrap_draws=2_000,
    )
    return intervention, static_result, timing_result, matrix_result


def test_release_requires_all_prerequisites_and_allows_one_degree(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    result = qualify_selected_state_release(
        intervention=intervention,
        static_result=static_result,
        timing_result=timing_result,
        matrix_result=matrix_result,
    )
    assert result["source_disjointness_verified"] is True
    assert result["operational_coefficient_release_allowed"] is True
    assert result["one_degree_coefficient_release_allowed"] is True


def test_release_accepts_distinct_fresh_observations_and_usb_addresses(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    contexts = [item["context"] for item in (static_result, timing_result, matrix_result)]
    assert len({item["device_identity_sha256"] for item in contexts}) == 3
    assert len({item["device_identity_snapshot"]["usb_uri"] for item in contexts}) == 3

    result = qualify_selected_state_release(
        intervention=intervention,
        static_result=static_result,
        timing_result=timing_result,
        matrix_result=matrix_result,
    )

    assert result["operational_coefficient_release_allowed"] is True


def test_release_rejects_x_and_q_static_bench_selector_mismatch(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    mismatched = replace(intervention, selector_evidence_sha256=_hash("other-bench-selector"))

    with pytest.raises(SelectedStateQualificationError, match="same exact bench selector"):
        qualify_selected_state_release(
            intervention=mismatched,
            static_result=static_result,
            timing_result=timing_result,
            matrix_result=matrix_result,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("serial", "different-pluto-serial"),
        ("model", "DifferentPlutoModel"),
        ("firmware_version", "different-firmware"),
        ("kernel_version", "different-kernel"),
        ("phy_model", "different-phy"),
        ("metadata_abi", 3),
        ("rx_scan_channels", ["voltage0", "voltage1"]),
        ("native_attestation_sha256", "e" * 64),
    ],
)
def test_release_rejects_stable_device_identity_mismatch(
    tmp_path: Path, field: str, replacement: object
) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    snapshot = matrix_result["context"]["device_identity_snapshot"]
    snapshot[field] = replacement
    if field == "native_attestation_sha256":
        matrix_result["context"]["native_attestation_sha256"] = replacement

    with pytest.raises(SelectedStateQualificationError):
        qualify_selected_state_release(
            intervention=intervention,
            static_result=static_result,
            timing_result=timing_result,
            matrix_result=matrix_result,
        )


def test_release_keeps_one_degree_objective_separate(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(
        tmp_path, selected_magnitude=0.021
    )
    result = qualify_selected_state_release(
        intervention=intervention,
        static_result=static_result,
        timing_result=timing_result,
        matrix_result=matrix_result,
    )
    assert result["operational_coefficient_release_allowed"] is True
    assert result["one_degree_coefficient_release_allowed"] is False


def test_release_rejects_reused_sources(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    matrix_result["source_stream_ids"][0] = timing_result["source_stream_ids"][0]
    with pytest.raises(SelectedStateQualificationError, match="reuse capture sources"):
        qualify_selected_state_release(
            intervention=intervention,
            static_result=static_result,
            timing_result=timing_result,
            matrix_result=matrix_result,
        )


def test_release_rejects_intervention_source_reuse(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    static_result["source_stream_ids"][0] = intervention.baseline_stream_ids[0]
    with pytest.raises(SelectedStateQualificationError, match="intervention-comparison"):
        qualify_selected_state_release(
            intervention=intervention,
            static_result=static_result,
            timing_result=timing_result,
            matrix_result=matrix_result,
        )


def test_release_blocks_failed_static_prerequisite(tmp_path: Path) -> None:
    intervention, static_result, timing_result, matrix_result = _all_results(tmp_path)
    static_result["accepted"] = False
    result = qualify_selected_state_release(
        intervention=intervention,
        static_result=static_result,
        timing_result=timing_result,
        matrix_result=matrix_result,
    )
    assert result["operational_coefficient_release_allowed"] is False
    assert "static_qualification_not_accepted" in result["rejection_reasons"]
