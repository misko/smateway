from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pluto_plus.direct_radio.usb import MetadataFlags
from pluto_plus.hardware import SampleBlockV2
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_leakage_ladder.py"
DOC_DIRECTORY = Path(__file__).resolve().parents[1] / "docs/5g8_root_cause_analysis"
SPEC = importlib.util.spec_from_file_location("run_5g8_leakage_ladder_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

FIXTURE_FACTORY_SCRIPT = Path(__file__).with_name("test_fixture_v2.py")
FIXTURE_FACTORY_SPEC = importlib.util.spec_from_file_location(
    "test_fixture_v2_factory_for_leakage_ladder", FIXTURE_FACTORY_SCRIPT
)
assert FIXTURE_FACTORY_SPEC is not None and FIXTURE_FACTORY_SPEC.loader is not None
fixture_v2_factory = importlib.util.module_from_spec(FIXTURE_FACTORY_SPEC)
sys.modules[FIXTURE_FACTORY_SPEC.name] = fixture_v2_factory
FIXTURE_FACTORY_SPEC.loader.exec_module(fixture_v2_factory)

SOURCE_COMMIT = "1" * 40
DEPENDENCY_COMMIT = "2" * 40
SERIAL = "serial-a"
URI = "usb:1.2.3"


def _dependency_attestation() -> dict[str, Any]:
    modules = (
        "pluto_plus.artifacts",
        "pluto_plus.bootstrap_firmware",
        "pluto_plus.hardware",
        "pluto_plus.hardware.iio",
        "pluto_plus.models",
    )
    return {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "repository_path": "/synthetic/pluto-plus-utils",
        "commit": DEPENDENCY_COMMIT,
        "head": DEPENDENCY_COMMIT,
        "clean_worktree_verified": True,
        "files": [{"relative_path": "src/pluto_plus/__init__.py", "sha256": "3" * 64}],
        "imported_modules": [{"module": module} for module in modules],
    }


def _native_attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(runner.REQUIRED_LIBIIO_PATH),
        "library_path_from_proc_maps": True,
        "library_sha256": runner.REQUIRED_LIBIIO_SHA256,
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {symbol: True for symbol in runner.REQUIRED_LIBIIO_SYMBOLS},
        "loader_search_path_first": "/usr/local/lib",
    }


def _selector_flash_binding() -> dict[str, Any]:
    return {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": "/synthetic/selector-flash-evidence.json",
        "sha256": "b" * 64,
        "campaign_id": "campaign-a",
        "run_id": "bench-flash-r01",
        "board_id": "board-a",
        "image_role": "bench",
    }


def _uncharacterized() -> dict[str, Any]:
    return {
        "status": "uncharacterized",
        "evidence_path": None,
        "evidence_sha256": None,
        "s_parameter_sha256": None,
        "return_loss_db_at_5g8": None,
    }


def _asset(identity: str, port_map: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": identity,
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_power_dbm": 20.0,
        "port_map": port_map,
        "characterization": _uncharacterized(),
        **extra,
    }


def _load(identity: str) -> dict[str, Any]:
    return _asset(identity, {"load": "LOAD"}, impedance_ohm=50.0)


def _connection(
    identity: str,
    source: tuple[str, str],
    destination: tuple[str, str],
    *,
    kind: str = "coaxial_cable",
) -> dict[str, Any]:
    return {
        "id": identity,
        "from": {"component_id": source[0], "port_id": source[1]},
        "to": {"component_id": destination[0], "port_id": destination[1]},
        "interconnect": {
            "id": f"{identity}-interconnect",
            "kind": kind,
            "rated_min_frequency_hz": 2_000_000_000,
            "rated_max_frequency_hz": 8_000_000_000,
            "maximum_input_power_dbm": 20.0,
            "characterization": _uncharacterized(),
        },
    }


def _fixture_evidence(stage: str = "direct_rx2_termination") -> dict[str, Any]:
    run_id = f"run-{stage}"
    selector_flash = (
        _selector_flash_binding() if stage in runner.SELECTOR_CONNECTED_STAGES else None
    )
    pluto = {
        "id": "pluto-a",
        "serial": SERIAL,
        "port_map": {"tx1": "TX1", "tx2": "TX2", "rx1": "RX1", "rx2": "RX2"},
    }
    splitter = _asset(
        "reference-splitter-a",
        {"input": "IN", "rx1_branch": "OUT1", "stimulus_branch": "OUT2"},
    )
    attenuator = _asset(
        "rx1-attenuator-a",
        {"input": "IN", "output": "OUT"},
        attenuation_db=30.0,
    )
    tx2_load = _load("tx2-termination-a")
    shared = {
        "pluto": pluto,
        "reference_planes": {
            "tx1": "tx1-sma-plane",
            "rx1": "rx1-sma-plane",
            "rx2": "rx2-sma-plane",
        },
        "tx1_reference_splitter": splitter,
        "rx1_attenuator": attenuator,
        "rx2_attenuator": {
            "state": "absent",
            "asset": None,
            "orientation": None,
            "pluto_connection": None,
        },
        "tx2_termination": tx2_load,
        "connections": {
            "tx1_to_splitter": _connection(
                "shared-tx1-splitter",
                ("pluto-a", "TX1"),
                ("reference-splitter-a", "IN"),
            ),
            "splitter_to_rx1_attenuator": _connection(
                "shared-splitter-attenuator",
                ("reference-splitter-a", "OUT1"),
                ("rx1-attenuator-a", "IN"),
            ),
            "rx1_attenuator_to_rx1": _connection(
                "shared-attenuator-rx1",
                ("rx1-attenuator-a", "OUT"),
                ("pluto-a", "RX1"),
            ),
            "tx2_to_termination": _connection(
                "shared-tx2-load",
                ("pluto-a", "TX2"),
                ("tx2-termination-a", "LOAD"),
                kind="direct_adapter",
            ),
        },
    }
    selector = _asset(
        "selector-a",
        {"common": "COMMON", **{ant: ant for ant in runner.ANTENNA_PORTS}},
        physical_board_id="pluto-rx2-8way-v5-board-a",
        hardware_revision="v5",
        bench_supply_id="bench-supply-a",
        bench_supply_output_id="channel-1",
        supply_voltage_v=5.0,
        supply_current_limit_a=0.5,
        power_positive_reference_id="j12-pin-1",
        power_ground_reference_id="j12-pin-2",
        control_ground_reference_id="j11-ground",
    )
    stimulus_load = _load("stimulus-termination-a")
    if stage in {"direct_rx2_termination", "rx2_cable_terminated"}:
        rx2_load = _load("rx2-termination-a")
        rx2_role = (
            "rx2_to_direct_termination"
            if stage == "direct_rx2_termination"
            else "rx2_to_far_end_termination"
        )
        components: dict[str, Any] = {
            "tx1_stimulus_termination": stimulus_load,
            "rx2_termination": rx2_load,
        }
        connections = {
            "splitter_stimulus_to_termination": _connection(
                "stage-stimulus-load",
                ("reference-splitter-a", "OUT2"),
                ("stimulus-termination-a", "LOAD"),
                kind="direct_adapter",
            ),
            rx2_role: _connection(
                ("stage-rx2-direct" if stage == "direct_rx2_termination" else "stage-rx2-common"),
                ("pluto-a", "RX2"),
                ("rx2-termination-a", "LOAD"),
                kind=("direct_adapter" if stage == "direct_rx2_termination" else "coaxial_cable"),
            ),
        }
    elif stage == "powered_selector_all_inputs_terminated":
        loads = {ant: _load(f"selector-{ant.lower()}-load") for ant in runner.ANTENNA_PORTS}
        components = {
            "tx1_stimulus_termination": stimulus_load,
            "selector": selector,
            "selector_input_terminations": loads,
        }
        connections = {
            "splitter_stimulus_to_termination": _connection(
                "stage-stimulus-load",
                ("reference-splitter-a", "OUT2"),
                ("stimulus-termination-a", "LOAD"),
                kind="direct_adapter",
            ),
            "rx2_to_selector_common": _connection(
                "stage-rx2-common",
                ("pluto-a", "RX2"),
                ("selector-a", "COMMON"),
            ),
            **{
                f"selector_{ant.lower()}_to_termination": _connection(
                    f"stage-{ant.lower()}-load",
                    ("selector-a", ant),
                    (f"selector-{ant.lower()}-load", "LOAD"),
                    kind="direct_adapter",
                )
                for ant in runner.ANTENNA_PORTS
            },
        }
    else:
        eight_way = _asset(
            "eight-way-a",
            {"input": "IN", **{ant: ant for ant in runner.ANTENNA_PORTS}},
        )
        components = {"eight_way_splitter": eight_way, "selector": selector}
        connections = {
            "splitter_stimulus_to_eight_way": _connection(
                "stage-stimulus-eight-way",
                ("reference-splitter-a", "OUT2"),
                ("eight-way-a", "IN"),
            ),
            "rx2_to_selector_common": _connection(
                "stage-rx2-common",
                ("pluto-a", "RX2"),
                ("selector-a", "COMMON"),
            ),
            **{
                f"eight_way_{ant.lower()}_to_selector_{ant.lower()}": _connection(
                    f"stage-eight-way-{ant.lower()}",
                    ("eight-way-a", ant),
                    ("selector-a", ant),
                )
                for ant in runner.ANTENNA_PORTS
            },
        }
    delta = {
        "schema": 1,
        "delta_id": f"delta-{stage}",
        "selector_rf_state": (
            "rf_disconnected"
            if stage in {"direct_rx2_termination", "rx2_cable_terminated"}
            else "rf_connected"
        ),
        "selector_power_state": (
            "bench_power_off"
            if stage in {"direct_rx2_termination", "rx2_cable_terminated"}
            else "bench_power_on"
        ),
        "selector_control_harness_state": (
            "disconnected"
            if stage in {"direct_rx2_termination", "rx2_cable_terminated"}
            else "connected_static_all_off"
        ),
        "components": components,
        "connections": connections,
    }
    shared_sha = runner.canonical_json_sha256(shared)
    delta_sha = runner.canonical_json_sha256(delta)
    prior_stage = runner.PRIOR_STAGE[stage]
    if prior_stage is None:
        prior = None
    else:
        prior_fixture = _fixture_evidence(prior_stage)
        comparison_anchor = runner._comparison_anchor_from_fixture_chain(
            stage=stage,
            prior_fixture=prior_fixture,
            current_stage_delta=delta,
        )
        prior = {
            "stage": prior_stage,
            "run_id": f"run-{prior_stage}",
            "plan_path": f"/synthetic/{prior_stage}/plan.json",
            "plan_file_sha256": "4" * 64,
            "plan_contract_sha256": "5" * 64,
            "fixture_evidence_sha256": "6" * 64,
            "shared_fixture_sha256": shared_sha,
            "prior_stage_delta_sha256": comparison_anchor["prior_stage_delta_sha256"],
            "comparison_anchor": comparison_anchor,
            "comparison_anchor_sha256": runner.canonical_json_sha256(comparison_anchor),
            "prior_selector_control_sha256": (
                runner.canonical_json_sha256(_selector_control())
                if prior_stage in runner.SELECTOR_CONNECTED_STAGES
                else None
            ),
            "campaign_id": "campaign-a",
            "comparable_fixture_group_id": "fixture-group-a",
            "prior_fixture_characterized": False,
        }
    component_ids, connection_ids = runner._fixture_identity_sets(shared, delta)
    setup_file = {
        "path": f"/synthetic/{run_id}-setup.json",
        "sha256": "a" * 64,
        "size_bytes": 2_048,
    }
    setup = {
        "schema": 1,
        "attestation_kind": runner.SETUP_ATTESTATION_KIND,
        "attestation_id": f"setup-{stage}",
        "created_at": "2026-08-29T12:00:00+00:00",
        "created_at_wall_clock_freshness_enforced": False,
        "run_id": run_id,
        "campaign_id": "campaign-a",
        "comparable_fixture_group_id": "fixture-group-a",
        "stage": stage,
        "fixture_manifest_sha256": "8" * 64,
        "shared_fixture_sha256": shared_sha,
        "stage_delta_sha256": delta_sha,
        "observed_component_ids": component_ids,
        "observed_connection_ids": connection_ids,
        "selector_flash_evidence": selector_flash,
        "setup_evidence": {
            "path": f"/synthetic/{run_id}-setup.png",
            "sha256": "9" * 64,
            "size_bytes": 4_096,
        },
        "setup_attestation_file": setup_file,
    }
    prior_characterized = prior is None or bool(prior["prior_fixture_characterized"])
    return {
        "schema": 2,
        "fixture_kind": runner.FIXTURE_KIND_V2,
        "campaign_id": "campaign-a",
        "comparable_fixture_group_id": "fixture-group-a",
        "stage": stage,
        "run_id": run_id,
        "board_id": "board-a",
        "source_files": {
            "fixture_manifest": {
                "path": f"/synthetic/{stage}-fixture.json",
                "sha256": "8" * 64,
                "size_bytes": 8_192,
            },
            "setup_attestation": setup_file,
        },
        "shared_fixture": shared,
        "shared_fixture_sha256": shared_sha,
        "stage_delta": delta,
        "stage_delta_sha256": delta_sha,
        "prior_stage_binding": prior,
        "setup_attestation": setup,
        "selector_flash_evidence": selector_flash,
        "component_ids": component_ids,
        "connection_ids": connection_ids,
        "characterization_summary": runner._characterization_summary(
            shared,
            delta,
            prior_characterized=prior_characterized,
        ),
    }


def _selector_control() -> dict[str, Any]:
    return {
        "schema": 1,
        "mode": "reviewed_static_selector_mailbox_all_off",
        "bench_manifest": {
            "path": "/synthetic/pluto_bench.manifest.json",
            "file_sha256": "4" * 64,
        },
        "openocd_config": {
            "path": "/synthetic/rpi4-swd.cfg",
            "file_sha256": "5" * 64,
        },
        "control_profile": {
            "path": "/synthetic/control_profile.json",
            "file_sha256": "6" * 64,
            "header_path": "/synthetic/control_profile.h",
            "header_file_sha256": "7" * 64,
            "all_off_code": 15,
        },
        "command": {
            "code": 15,
            "lease_ms": 0,
            "wait_until_applied": True,
            "readback_required": True,
        },
        "selector_flash_evidence": _selector_flash_binding(),
        "target_image_admission_contract": {
            "schema": 1,
            "flash_base_address": runner.FLASH_BASE_ADDRESS,
            "firmware_bin_path": "/synthetic/pluto_bench.bin",
            "firmware_bin_sha256": "a" * 64,
            "firmware_bin_size_bytes": 1024,
            "board_id": "board-a",
            "selector_flash_evidence_sha256": "b" * 64,
            "full_bin_extent_and_uid_required_before_mailbox": True,
        },
    }


def _contract(stage: str = "direct_rx2_termination") -> dict[str, Any]:
    return runner._build_plan_contract(
        run_id=f"run-{stage}",
        board_id="board-a",
        serial=SERIAL,
        uri=URI,
        stage=stage,
        source_commit=SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=_dependency_attestation(),
        selector_control=_selector_control() if stage in runner.SELECTOR_CONNECTED_STAGES else None,
        native_libiio_runtime_attestation=_native_attestation(),
        fixture_evidence=_fixture_evidence(stage),
    )


def _write_prior_plan_binding(
    tmp_path: Path,
    *,
    stage: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prior_stage = runner.PRIOR_STAGE[stage]
    assert prior_stage is not None
    prior_contract = _contract(prior_stage)
    prior_envelope = runner._plan_envelope(prior_contract)
    prior_plan_path = tmp_path / f"{prior_stage}-plan.json"
    runner._write_immutable_json(prior_plan_path, prior_envelope)
    return (
        {
            "stage": prior_stage,
            "run_id": prior_contract["run_id"],
            "plan_path": str(prior_plan_path),
            "plan_file_sha256": runner.sha256_path(prior_plan_path),
            "plan_contract_sha256": prior_envelope["plan_contract_sha256"],
            "fixture_evidence_sha256": prior_contract["fixture_evidence_sha256"],
        },
        prior_contract,
    )


def _bind_run_capture_root(contract: dict[str, Any], path: Path) -> None:
    contract["storage"] = {
        **contract["storage"],
        "run_capture_root": str(path),
    }


def _plan_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "plan_contract_sha256": "a" * 64,
        "plan_contract_hash_provenance": "synthetic canonical JSON",
        "plan_file_sha256": "b" * 64,
        "plan_file_hash_provenance": "synthetic file bytes",
    }


def _passing_identity(calls: list[tuple[str, str]] | None = None) -> Any:
    def identity(serial: str, requested_uri: str) -> dict[str, Any]:
        if calls is not None:
            calls.append((serial, requested_uri))
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "passed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": requested_uri,
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
            "error": None,
        }

    return identity


def _passing_runtime() -> Any:
    return lambda: _native_attestation()


def _passing_fixture() -> Any:
    def fixture(expected: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "general_fixture_v2_preflight",
            "status": "passed",
            "fixture_evidence": dict(expected),
            "fixture_evidence_sha256": runner.canonical_json_sha256(expected),
            "exact_frozen_evidence_match": True,
            "error": None,
        }

    return fixture


def _confirmation(contract: dict[str, Any]) -> dict[str, Any]:
    stage = contract["topology_stage"]
    return runner._validate_confirmations(
        stage=stage,
        confirm_stage=stage,
        topology_token=contract["stage_contract"]["confirmation_token"],
        no_antennas=True,
        tx1_matched=True,
        tx2_terminated_muted=True,
        rx1_conducted_reference=True,
        no_movement=True,
        fixture_evidence=contract["fixture_evidence"],
        selector_static_all_off=stage in runner.SELECTOR_CONNECTED_STAGES,
    )


def _passing_selector(calls: list[str] | None = None) -> Any:
    def snapshot(code: int) -> dict[str, Any]:
        return {
            "applied_code": code,
            "command_code": code,
            "command_lease_ms": 0,
            "command_sequence": 17,
            "acknowledged_sequence": 17,
            "command_valid": True,
            "lease_active": False,
            "remaining_lease_ms": 0,
            "guard_active": False,
            "invalid_command": False,
        }

    def selector(control: dict[str, Any], purpose: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(purpose)
        code = control["command"]["code"]
        read_only = purpose in runner.SELECTOR_READ_ONLY_PURPOSES
        pre_command = None if read_only else snapshot(code)
        commanded = None if read_only else snapshot(code)
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
            "pre_command": pre_command,
            "commanded": commanded,
            "readback": snapshot(code),
            "error": None,
        }

    return selector


def _passing_selector_image(calls: list[str] | None = None) -> Any:
    def admit(control: dict[str, Any]) -> dict[str, Any]:
        if calls is not None:
            calls.append("target_image")
        target = control["target_image_admission_contract"]
        flash = control["selector_flash_evidence"]
        return {
            "schema": 1,
            "evidence_kind": "contemporaneous_full_bin_extent_and_uid_admission_v1",
            "status": "passed",
            "selector_flash_evidence_sha256": flash["sha256"],
            "flash_base_address": target["flash_base_address"],
            "byte_count": target["firmware_bin_size_bytes"],
            "expected_bin_sha256": target["firmware_bin_sha256"],
            "observed_target_sha256": target["firmware_bin_sha256"],
            "expected_board_id": flash["board_id"],
            "observed_uid": flash["board_id"].removeprefix("stm32c011-"),
            "exact_bin_and_uid_match": True,
            "reviewed_image_started_only_after_exact_match": True,
            "target_may_have_started_before_failure_halt": False,
            "failure_halt_required": False,
            "failure_halt": None,
            "target_kept_halted_on_failure": False,
            "mailbox_access_performed": False,
            "error": None,
        }

    return admit


def _passing_selector_target_halt(
    control: Mapping[str, Any],
    purpose: str = "image_admission_failure_cleanup",
) -> dict[str, Any]:
    config = control["openocd_config"]
    return {
        "schema": 1,
        "evidence_kind": "selector_target_best_effort_halt_v1",
        "purpose": purpose,
        "status": "passed",
        "openocd_config_path": config["path"],
        "openocd_config_sha256": config["file_sha256"],
        "command": "init; halt; shutdown",
        "returncode": 0,
        "target_halted": True,
        "mailbox_access_performed": False,
        "error": None,
    }


def _passing_mute(calls: list[str] | None = None) -> Any:
    def mute(serial: str, purpose: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(purpose)
        return {
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        }

    return mute


def _settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=runner.CENTER_FREQUENCY_HZ,
        sample_rate_hz=runner.SAMPLE_RATE_HZ,
        bandwidth_hz=runner.BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=runner.RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _blocks(
    *,
    stream_id: int = 12345,
    rx1_phasor: complex = 400.0 * np.exp(0.4j),
    rx2_phasor: complex = 40.0 * np.exp(-0.8j),
    clipped: bool = False,
    tone_offset_hz: float = runner.TONE_OFFSET_HZ,
    rx2_alternating_phase: bool = False,
) -> list[SampleBlockV2]:
    flags = int(MetadataFlags.SAMPLE_SEQUENCE_VALID | MetadataFlags.HARDWARE_SAMPLE_COUNTER_VALID)
    rng = np.random.default_rng(20260829)
    blocks = []
    first_sample_sequence = 8_000_000
    realtime_base = 1_800_000_000_000_000_000
    monotonic_base = 2_000_000_000_000
    duration_ns = round(runner.SAMPLES_PER_FRAME / runner.SAMPLE_RATE_HZ * 1e9)
    for index in range(runner.FRAME_COUNT):
        start = index * runner.SAMPLES_PER_FRAME
        indices = np.arange(start, start + runner.SAMPLES_PER_FRAME, dtype=np.float64)
        carrier = np.exp(2j * np.pi * tone_offset_hz * indices / runner.SAMPLE_RATE_HZ)
        noise1 = 2.0 * (
            rng.standard_normal(runner.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.SAMPLES_PER_FRAME)
        )
        noise2 = 2.0 * (
            rng.standard_normal(runner.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.SAMPLES_PER_FRAME)
        )
        samples = np.asarray(
            [rx1_phasor * carrier + noise1, rx2_phasor * carrier + noise2],
            dtype=np.complex64,
        )
        if rx2_alternating_phase:
            analysis_block_samples = round(0.010 * runner.SAMPLE_RATE_HZ)
            for local_start in range(0, runner.SAMPLES_PER_FRAME, analysis_block_samples):
                local_stop = min(
                    local_start + analysis_block_samples,
                    runner.SAMPLES_PER_FRAME,
                )
                analysis_index = (start + local_start) // analysis_block_samples
                samples[1, local_start:local_stop] *= np.exp(
                    1j * np.deg2rad(35.0 if analysis_index % 2 else -35.0)
                )
        if clipped and index == 0:
            samples[1, 0] = 2_047.0 + 0j
        realtime_start = realtime_base + index * duration_ns
        monotonic_start = monotonic_base + index * duration_ns
        blocks.append(
            SampleBlockV2(
                utc_ns=realtime_start + duration_ns // 2,
                samples=samples,
                stream_id=stream_id,
                buffer_sequence=index,
                first_sample_sequence=(first_sample_sequence + index * runner.SAMPLES_PER_FRAME),
                metadata_flags=flags,
                metadata_abi=2,
                missing_samples_before=0,
                sample_time_realtime_start_ns=realtime_start,
                sample_time_realtime_end_ns=realtime_start + duration_ns,
                sample_time_monotonic_start_ns=monotonic_start,
                sample_time_monotonic_end_ns=monotonic_start + duration_ns,
                sample_time_uncertainty_ns=1_000,
            )
        )
    return blocks


def _capture_boundary(
    blocks: list[SampleBlockV2],
    *,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    def capture(
        plan: Any,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Any,
    ) -> Any:
        if calls is not None:
            calls.append(
                {
                    "plan": plan,
                    "samples_per_frame": samples_per_frame,
                    "frame_count": frame_count,
                    "kernel_buffers": kernel_buffers,
                }
            )
        for block in blocks:
            block_consumer(block)
        scales = [0.0] * 8
        scales[0] = runner.DDS_SCALE
        scales[2] = runner.DDS_SCALE
        enabled = [False] * 8
        enabled[0] = True
        enabled[2] = True
        frequencies = [0] * 8
        frequencies[0] = runner.TONE_OFFSET_HZ
        frequencies[2] = -runner.TONE_OFFSET_HZ
        identity = RadioIdentity(
            radio_id=SERIAL,
            serial=SERIAL,
            uri=URI,
            transport=Transport.IIO_USB,
            model="synthetic Pluto",
            firmware_version="v0.39-v7",
            usb_path="/synthetic/usb",
        )
        return SimpleNamespace(
            identity=identity,
            settings=_settings(),
            frames=tuple(blocks),
            sample_count=sum(block.sample_count for block in blocks),
            kernel_buffers=kernel_buffers,
            tx_gain_readback_db=plan.tx_hardware_gain_db,
            dds_scale_readback=tuple(scales),
            dds_enabled_readback=tuple(enabled),
            dds_frequency_readback_hz=tuple(frequencies),
        )

    return capture


def _completed_single_condition_run(
    tmp_path: Path,
    *,
    stream_id: int,
    stage: str = "direct_rx2_termination",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path, Path]:
    capture_root = tmp_path / "captures"
    contract = _contract(stage)
    _bind_run_capture_root(contract, capture_root)
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=_confirmation(contract),
        capture_root=capture_root,
        capture_boundary=_capture_boundary(_blocks(stream_id=stream_id)),
        mute_boundary=_passing_mute(),
        identity_boundary=_passing_identity(),
        selector_boundary=_passing_selector(),
        selector_image_boundary=_passing_selector_image(),
        runtime_attestation_boundary=_passing_runtime(),
        fixture_evidence_boundary=_passing_fixture(),
    )
    return contract, envelope, manifest, plan_path, manifest_path


def _write_x_fixture_chain(
    directory: Path,
    *,
    current_limit_a: float,
    run_prefix: str,
) -> dict[str, Any]:
    """Build a source-backed production A -> B -> C -> E fixture chain for X tests."""

    original_identity = (
        fixture_v2_factory.CAMPAIGN,
        fixture_v2_factory.GROUP,
        fixture_v2_factory.BOARD,
        fixture_v2_factory.SERIAL,
    )
    original_plan = fixture_v2_factory._plan

    def runner_compatible_plan(
        plan_directory: Path,
        *,
        stage: str,
        run_id: str,
        evidence: dict[str, Any],
        selector_binding: dict[str, Any],
        selector_control: dict[str, Any] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        plan_path, envelope = original_plan(
            plan_directory,
            stage=stage,
            run_id=run_id,
            evidence=evidence,
            selector_binding=selector_binding,
            selector_control=selector_control,
        )
        if stage in runner.SELECTOR_CONNECTED_STAGES and selector_control is None:
            envelope["plan_contract"]["selector_control"] = _selector_control_for_flash(
                selector_binding
            )
            envelope["plan_contract_sha256"] = runner.canonical_json_sha256(
                envelope["plan_contract"]
            )
            fixture_v2_factory._write_json(plan_path, envelope)
        return plan_path, envelope

    try:
        fixture_v2_factory.CAMPAIGN = "campaign-a"
        fixture_v2_factory.GROUP = "fixture-group-a"
        fixture_v2_factory.BOARD = "board-a"
        fixture_v2_factory.SERIAL = SERIAL
        fixture_v2_factory._plan = runner_compatible_plan
        return fixture_v2_factory._chain(
            directory,
            supply_current_limit_a=current_limit_a,
            run_prefix=run_prefix,
        )
    finally:
        (
            fixture_v2_factory.CAMPAIGN,
            fixture_v2_factory.GROUP,
            fixture_v2_factory.BOARD,
            fixture_v2_factory.SERIAL,
        ) = original_identity
        fixture_v2_factory._plan = original_plan


def _write_x_full_fixture(path: Path, *, current_limit_a: float) -> Path:
    chain = _write_x_fixture_chain(
        path.parent / f"{path.stem}-chain",
        current_limit_a=current_limit_a,
        run_prefix=path.stem,
    )
    return Path(chain["full_conducted_fixture"]["manifest"])


def _selector_control_for_flash(flash: Mapping[str, Any]) -> dict[str, Any]:
    control = json.loads(json.dumps(_selector_control()))
    control["selector_flash_evidence"] = dict(flash)
    target = control["target_image_admission_contract"]
    target["board_id"] = flash["board_id"]
    target["selector_flash_evidence_sha256"] = flash["sha256"]
    return control


def _x_contract(
    tmp_path: Path,
    *,
    stage: str,
    role: str,
    implicated_stage: str,
) -> dict[str, Any]:
    before_chain = _write_x_fixture_chain(
        tmp_path / "before-chain",
        current_limit_a=0.4,
        run_prefix="x-before",
    )
    after_chain = _write_x_fixture_chain(
        tmp_path / "after-chain",
        current_limit_a=0.5,
        run_prefix="x-after",
    )
    capture_chain = after_chain if role.endswith("_intervention") else before_chain
    before = Path(before_chain["full_conducted_fixture"]["manifest"])
    after = Path(after_chain["full_conducted_fixture"]["manifest"])
    capture = after if role.endswith("_intervention") else before
    fixture = capture_chain[stage]["evidence"]
    flash = capture_chain["selector"]
    run_id = fixture["run_id"]
    prebinding, context = runner._x_intervention_contract_from_manifests(
        contract_id="shield-current-limit-r01",
        run_role=role,
        implicated_boundary_stage=implicated_stage,
        installed_fixture_manifest_path=after,
        capture_fixture_manifest_path=capture,
        acquisition_index=11,
        freshness_epoch_id="x-epoch-r01",
        stage=stage,
        board_id="board-a",
        serial=SERIAL,
        fixture_evidence=fixture,
        selector_flash_evidence=flash,
    )
    return runner._build_plan_contract(
        run_id=run_id,
        board_id="board-a",
        serial=SERIAL,
        uri=URI,
        stage=stage,
        source_commit=SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=_dependency_attestation(),
        selector_control=(
            _selector_control_for_flash(flash)
            if stage in runner.SELECTOR_CONNECTED_STAGES
            else None
        ),
        native_libiio_runtime_attestation=_native_attestation(),
        fixture_evidence=fixture,
        x_intervention_prebinding=prebinding,
        x_intervention_capture_context=context,
    )


def test_plan_contract_freezes_exact_stages_and_bounded_tx1_ladder() -> None:
    assert tuple(runner.STAGE_CONTRACTS) == (
        "direct_rx2_termination",
        "rx2_cable_terminated",
        "powered_selector_all_inputs_terminated",
        "full_conducted_fixture",
    )
    for stage in runner.STAGES:
        contract = _contract(stage)
        conditions = contract["conditions"]
        assert contract["topology_stage"] == stage
        assert contract["stage_contract"]["confirmation_token"]
        assert contract["interpretation"]["selector_calibration_claim"] is False
        assert contract["interpretation"]["may_be_used_as_selector_calibration"] is False
        assert contract["interpretation"]["one_hot_path_diagnosis"] == {
            "implemented_by_this_runner": False,
            "required_future_runner": "run_5g8_one_hot_path_ladder.py",
            "reason": (
                "per-port path response requires a separate immutable state/readback plan; "
                "the present runner measures only static ALL_OFF topology leakage"
            ),
        }
        assert (
            contract["source"]["pluto_plus_utils_source_attestation"]["commit"] == DEPENDENCY_COMMIT
        )
        assert len(contract["source"]["pluto_plus_utils_source_attestation_sha256"]) == 64
        assert contract["source"]["native_libiio_runtime_attestation"] == (_native_attestation())
        assert len(contract["source"]["native_libiio_runtime_attestation_sha256"]) == 64
        assert (contract["selector_control"] is not None) == (
            stage in runner.SELECTOR_CONNECTED_STAGES
        )
        assert contract["fixture_evidence"] is not None
        assert contract["fixture_evidence"]["stage"] == stage
        assert contract["fixture_evidence"]["comparable_fixture_group_id"] == ("fixture-group-a")
        assert contract["storage"]["medium"] == "raspberry_pi_local_filesystem"
        assert contract["storage"]["pluto_onboard_storage_used"] is False
        assert [item["tx_hardware_gain_db"] for item in conditions[:6]] == list(
            runner.TX_HARDWARE_GAINS_DB
        )
        attribution = [
            item for item in conditions if item["tx_hardware_gain_db"] == runner.ATTRIBUTION_GAIN_DB
        ]
        assert len(conditions) == 10
        assert len(attribution) == runner.ATTRIBUTION_REPEAT_COUNT == 5
        assert [item["attribution_repeat_index"] for item in attribution] == [1, 2, 3, 4, 5]
        assert len({item["condition_id"] for item in conditions}) == len(conditions)
        assert [item["plan_index"] for item in conditions] == list(range(len(conditions)))
        assert all(item["center_frequency_hz"] == 5_800_000_000 for item in conditions)
        assert all(item["tone_offset_hz"] == 100_000 for item in conditions)
        assert all(item["tx_channel"] == 0 for item in conditions)
        assert all(item["tx2_required_exact_muted"] is True for item in conditions)
        assert all(item["kernel_buffers"] == 8 for item in conditions)
        assert all(item["fresh_stream_required"] is True for item in conditions)
        assert contract["configuration"]["automatic_retry_count"] == 0
        assert contract["configuration"]["attribution_repeat_count"] == 5


@pytest.mark.parametrize(
    ("serial", "uri", "message"),
    [
        ("", URI, "serial"),
        (SERIAL, "ip:192.168.2.1", "exact current"),
        (SERIAL, "pluto://usb:1.2.3", "exact current"),
        (SERIAL, "usb:any", "exact current"),
    ],
)
def test_plan_rejects_nonexact_device_identity(serial: str, uri: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        runner._build_plan_contract(
            run_id="run-a",
            board_id="board-a",
            serial=serial,
            uri=uri,
            stage="direct_rx2_termination",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
            require_fixture_evidence=False,
        )


def test_selector_connected_plan_requires_static_all_off_control_contract() -> None:
    fixture = _fixture_evidence("powered_selector_all_inputs_terminated")
    fixture["run_id"] = "run-selector"
    fixture["setup_attestation"]["run_id"] = "run-selector"
    with pytest.raises(ValueError, match="static ALL_OFF"):
        runner._build_plan_contract(
            run_id="run-selector",
            board_id="board-a",
            serial=SERIAL,
            uri=URI,
            stage="powered_selector_all_inputs_terminated",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
            fixture_evidence=fixture,
        )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.update(library_path="/lib/libiio.so.0.24"), "mapped from"),
        (lambda value: value.update(library_sha256="0" * 64), "reviewed binary"),
        (
            lambda value: value["version"].update(minor=24),
            "required 0.25 ABI",
        ),
        (
            lambda value: value["required_symbols"].update(
                iio_device_get_kernel_buffers_count=False
            ),
            "required capture symbol",
        ),
    ],
)
def test_native_libiio_attestation_fails_closed(mutator: Any, message: str) -> None:
    value = _native_attestation()
    mutator(value)
    with pytest.raises(ValueError, match=message):
        runner._validate_native_libiio_runtime_attestation(value)


@pytest.mark.parametrize("stage", runner.STAGES)
def test_fixture_v2_covers_every_general_stage_with_port_level_graph(stage: str) -> None:
    fixture = _fixture_evidence(stage)
    observed = runner._validate_fixture_evidence_v2(
        fixture,
        expected_stage=stage,
        expected_run_id=f"run-{stage}",
        expected_board_id="board-a",
        expected_serial=SERIAL,
    )
    assert observed["shared_fixture"]["pluto"]["serial"] == SERIAL
    assert set(observed["shared_fixture"]["reference_planes"]) == {"tx1", "rx1", "rx2"}
    assert observed["shared_fixture"]["rx2_attenuator"]["state"] == "absent"
    assert observed["component_ids"]
    assert observed["connection_ids"]
    assert observed["characterization_summary"]["causal_attribution_claim"] is False
    assert (observed["prior_stage_binding"] is None) == (stage == "direct_rx2_termination")
    expected_connected = stage in runner.SELECTOR_CONNECTED_STAGES
    assert observed["stage_delta"]["selector_rf_state"] == (
        "rf_connected" if expected_connected else "rf_disconnected"
    )
    assert observed["stage_delta"]["selector_power_state"] == (
        "bench_power_on" if expected_connected else "bench_power_off"
    )
    assert observed["stage_delta"]["selector_control_harness_state"] == (
        "connected_static_all_off" if expected_connected else "disconnected"
    )
    if expected_connected:
        selector = observed["stage_delta"]["components"]["selector"]
        assert selector["physical_board_id"] == "pluto-rx2-8way-v5-board-a"
        assert selector["hardware_revision"] == "v5"
        assert selector["supply_voltage_v"] == 5.0
        assert selector["supply_current_limit_a"] == 0.5


def test_present_rx2_attenuator_retargets_stage_graph_and_is_inventory_bound() -> None:
    fixture = _fixture_evidence("direct_rx2_termination")
    shared = json.loads(json.dumps(fixture["shared_fixture"]))
    delta = json.loads(json.dumps(fixture["stage_delta"]))
    shared["rx2_attenuator"] = {
        "state": "present",
        "asset": _asset(
            "rx2-attenuator-a",
            {"input": "IN", "output": "OUT"},
            attenuation_db=20.0,
        ),
        "orientation": {
            "fixture_side_port_role": "input",
            "pluto_side_port_role": "output",
        },
        "pluto_connection": _connection(
            "shared-rx2-attenuator",
            ("pluto-a", "RX2"),
            ("rx2-attenuator-a", "OUT"),
            kind="direct_adapter",
        ),
    }
    delta["connections"]["rx2_to_direct_termination"]["from"] = {
        "component_id": "rx2-attenuator-a",
        "port_id": "IN",
    }

    normalized_shared = runner._normalize_shared_fixture(shared, expected_serial=SERIAL)
    normalized_delta = runner._normalize_stage_delta(
        delta,
        stage="direct_rx2_termination",
        shared=normalized_shared,
    )
    component_ids, connection_ids = runner._fixture_identity_sets(
        normalized_shared, normalized_delta
    )

    assert "rx2-attenuator-a" in component_ids
    assert "shared-rx2-attenuator" in connection_ids
    assert normalized_delta["connections"]["rx2_to_direct_termination"]["from"] == {
        "component_id": "rx2-attenuator-a",
        "port_id": "IN",
    }


@pytest.mark.parametrize(
    "state",
    (
        {"state": "absent", "asset": {}, "orientation": None, "pluto_connection": None},
        {"state": "present", "asset": None, "orientation": None, "pluto_connection": None},
    ),
)
def test_optional_rx2_attenuator_state_fails_closed(state: dict[str, Any]) -> None:
    shared = _fixture_evidence("direct_rx2_termination")["shared_fixture"]
    shared["rx2_attenuator"] = state
    with pytest.raises(ValueError, match="RX2 attenuator"):
        runner._normalize_shared_fixture(shared, expected_serial=SERIAL)


@pytest.mark.parametrize(
    "stage",
    (
        "rx2_cable_terminated",
        "powered_selector_all_inputs_terminated",
        "full_conducted_fixture",
    ),
)
def test_prior_plan_derives_exact_cross_stage_comparison_anchor(
    tmp_path: Path,
    stage: str,
) -> None:
    fixture = _fixture_evidence(stage)
    raw_binding, _ = _write_prior_plan_binding(tmp_path, stage=stage)

    observed = runner._prior_stage_binding_from_plan(
        raw_binding,
        stage=stage,
        campaign_id=fixture["campaign_id"],
        comparable_fixture_group_id=fixture["comparable_fixture_group_id"],
        shared_fixture_sha256=fixture["shared_fixture_sha256"],
        current_stage_delta=fixture["stage_delta"],
        board_id=fixture["board_id"],
        serial=SERIAL,
        base_directory=tmp_path,
    )

    assert observed is not None
    assert observed["comparison_anchor"]["from_stage"] == runner.PRIOR_STAGE[stage]
    assert observed["comparison_anchor"]["to_stage"] == stage
    assert observed["comparison_anchor_sha256"] == runner.canonical_json_sha256(
        observed["comparison_anchor"]
    )


@pytest.mark.parametrize(
    ("stage", "mutator"),
    (
        (
            "rx2_cable_terminated",
            lambda delta: delta["components"]["tx1_stimulus_termination"].update(
                maximum_input_power_dbm=21.0
            ),
        ),
        (
            "rx2_cable_terminated",
            lambda delta: delta["components"]["rx2_termination"].update(
                maximum_input_power_dbm=21.0
            ),
        ),
        (
            "powered_selector_all_inputs_terminated",
            lambda delta: delta["connections"]["rx2_to_selector_common"]["interconnect"].update(
                id="substituted-rx2-cable"
            ),
        ),
        (
            "full_conducted_fixture",
            lambda delta: delta["components"]["selector"].update(
                physical_board_id="substituted-selector-board"
            ),
        ),
        (
            "full_conducted_fixture",
            lambda delta: delta["components"]["selector"].update(supply_voltage_v=4.8),
        ),
    ),
)
def test_prior_plan_comparison_anchor_rejects_substituted_fixture_assets(
    tmp_path: Path,
    stage: str,
    mutator: Any,
) -> None:
    fixture = _fixture_evidence(stage)
    raw_binding, _ = _write_prior_plan_binding(tmp_path, stage=stage)
    mutator(fixture["stage_delta"])

    with pytest.raises(runner.LeakageLadderError, match="substituted a comparison-anchor"):
        runner._prior_stage_binding_from_plan(
            raw_binding,
            stage=stage,
            campaign_id=fixture["campaign_id"],
            comparable_fixture_group_id=fixture["comparable_fixture_group_id"],
            shared_fixture_sha256=fixture["shared_fixture_sha256"],
            current_stage_delta=fixture["stage_delta"],
            board_id=fixture["board_id"],
            serial=SERIAL,
            base_directory=tmp_path,
        )


def test_stage_e_rejects_selector_control_different_from_stage_c_plan() -> None:
    control = _selector_control()
    control["bench_manifest"]["file_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="differs from the immediately prior Stage C"):
        runner._build_plan_contract(
            run_id="run-full_conducted_fixture",
            board_id="board-a",
            serial=SERIAL,
            uri=URI,
            stage="full_conducted_fixture",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
            selector_control=control,
            native_libiio_runtime_attestation=_native_attestation(),
            fixture_evidence=_fixture_evidence("full_conducted_fixture"),
        )


def test_fixture_v2_rejects_out_of_band_rating_and_incomplete_connection_graph() -> None:
    outside_rating = _fixture_evidence()
    outside_rating["stage_delta"]["components"]["rx2_termination"]["rated_max_frequency_hz"] = (
        5_000_000_000
    )
    with pytest.raises(ValueError, match="contain 5.8 GHz"):
        runner._validate_fixture_evidence_v2(
            outside_rating,
            expected_stage="direct_rx2_termination",
            expected_run_id="run-direct_rx2_termination",
            expected_board_id="board-a",
            expected_serial=SERIAL,
        )

    missing_connection = _fixture_evidence()
    del missing_connection["shared_fixture"]["connections"]["tx1_to_splitter"]
    with pytest.raises(ValueError, match="connection graph"):
        runner._validate_fixture_evidence_v2(
            missing_connection,
            expected_stage="direct_rx2_termination",
            expected_run_id="run-direct_rx2_termination",
            expected_board_id="board-a",
            expected_serial=SERIAL,
        )


@pytest.mark.parametrize(
    "mutator",
    (
        lambda fixture: fixture["shared_fixture"]["pluto"]["port_map"].update(rx2="RX1"),
        lambda fixture: fixture["shared_fixture"]["tx1_reference_splitter"]["port_map"].update(
            stimulus_branch="OUT1"
        ),
    ),
)
def test_fixture_v2_rejects_duplicate_physical_port_ids(mutator: Any) -> None:
    fixture = _fixture_evidence()
    mutator(fixture)

    with pytest.raises(ValueError, match="physical port IDs must be unique"):
        runner._validate_fixture_evidence_v2(
            fixture,
            expected_stage="direct_rx2_termination",
            expected_run_id="run-direct_rx2_termination",
            expected_board_id="board-a",
            expected_serial=SERIAL,
        )


def test_characterization_is_explicit_and_causal_fields_are_required_when_claimed() -> None:
    assert (
        runner._normalize_characterization(
            _uncharacterized(),
            label="synthetic asset",
        )
        == _uncharacterized()
    )
    incomplete_characterized = {
        "status": "characterized",
        "evidence_path": "/synthetic/characterization.json",
        "evidence_sha256": "a" * 64,
        "s_parameter_sha256": None,
        "return_loss_db_at_5g8": None,
    }
    with pytest.raises(ValueError, match="requires an S-parameter hash"):
        runner._normalize_characterization(
            incomplete_characterized,
            label="synthetic asset",
        )


def test_fixture_v2_hashes_unique_run_bound_setup_evidence_without_timestamp_freshness(
    tmp_path: Path,
) -> None:
    stage = "direct_rx2_termination"
    fixture = _fixture_evidence(stage)
    manifest = {
        "schema": 2,
        "fixture_kind": runner.FIXTURE_KIND_V2,
        "campaign_id": fixture["campaign_id"],
        "comparable_fixture_group_id": fixture["comparable_fixture_group_id"],
        "stage": stage,
        "board_id": "board-a",
        "shared_fixture": fixture["shared_fixture"],
        "stage_delta": fixture["stage_delta"],
        "prior_stage_binding": None,
    }
    manifest_path = tmp_path / "fixture.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    photograph = tmp_path / "setup.png"
    photograph.write_bytes(b"synthetic run-specific setup photograph")
    setup = {
        "schema": 1,
        "attestation_kind": runner.SETUP_ATTESTATION_KIND,
        "attestation_id": "setup-stage-a",
        "created_at": "2026-08-29T12:00:00+00:00",
        "run_id": f"run-{stage}",
        "campaign_id": fixture["campaign_id"],
        "comparable_fixture_group_id": fixture["comparable_fixture_group_id"],
        "stage": stage,
        "fixture_manifest_sha256": runner.sha256_path(manifest_path),
        "shared_fixture_sha256": fixture["shared_fixture_sha256"],
        "stage_delta_sha256": fixture["stage_delta_sha256"],
        "observed_component_ids": fixture["component_ids"],
        "observed_connection_ids": fixture["connection_ids"],
        "selector_flash_evidence": None,
        "setup_evidence_path": photograph.name,
        "setup_evidence_sha256": runner.sha256_path(photograph),
    }
    setup_path = tmp_path / "setup.json"
    setup_path.write_text(json.dumps(setup), encoding="utf-8")

    observed = runner._fixture_evidence_from_manifests(
        manifest_path,
        setup_path,
        run_id=f"run-{stage}",
        board_id="board-a",
        serial=SERIAL,
        stage=stage,
    )

    assert observed["source_files"]["fixture_manifest"]["sha256"] == runner.sha256_path(
        manifest_path
    )
    assert observed["setup_attestation"]["setup_evidence"]["sha256"] == runner.sha256_path(
        photograph
    )
    assert observed["setup_attestation"]["created_at_wall_clock_freshness_enforced"] is False
    photograph.write_bytes(b"changed setup")
    with pytest.raises(runner.LeakageLadderError, match="setup evidence differs"):
        runner._fixture_evidence_from_manifests(
            manifest_path,
            setup_path,
            run_id=f"run-{stage}",
            board_id="board-a",
            serial=SERIAL,
            stage=stage,
        )


def test_documented_fixture_and_setup_templates_are_valid_json_and_current_schema() -> None:
    fixture = json.loads(
        (DOC_DIRECTORY / "fixture_manifest_v2.stage-a.template.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (DOC_DIRECTORY / "setup_attestation_v1.template.json").read_text(encoding="utf-8")
    )

    assert fixture["schema"] == 2
    assert fixture["fixture_kind"] == runner.FIXTURE_KIND_V2
    assert fixture["stage_delta"]["selector_rf_state"] == "rf_disconnected"
    assert fixture["stage_delta"]["selector_power_state"] == "bench_power_off"
    assert setup["attestation_kind"] == runner.SETUP_ATTESTATION_KIND
    assert "rx1_load" not in json.dumps(fixture)


def test_immutable_plan_is_create_only_hash_bound_and_idempotent(tmp_path: Path) -> None:
    contract = _contract()
    path = tmp_path / "run" / runner.PLAN_FILENAME

    first = runner._prepare_plan(path, contract)
    second = runner._prepare_plan(path, contract)

    assert first == second
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert first["plan_contract_sha256"] == runner.canonical_json_sha256(contract)
    evidence = runner._plan_file_evidence(path, first)
    assert evidence["plan_file_sha256"] == runner.sha256_path(path)
    assert evidence["plan_contract_sha256"] == first["plan_contract_sha256"]
    assert evidence["plan_file_hash_provenance"] != evidence["plan_contract_hash_provenance"]

    os.chmod(path, 0o600)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["plan_contract"]["conditions"][0]["tx_channel"] = 1
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(runner.LeakageLadderError, match="hash"):
        runner._prepare_plan(path, contract)


def test_failed_run_tombstone_survives_manifest_deletion_and_rollback(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    run_root = tmp_path / "run"
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    envelope, manifest = runner._prepare_plan_only_run(
        plan_path=plan_path,
        manifest_path=manifest_path,
        contract=contract,
    )
    runner._persist_manifest(
        manifest_path,
        manifest,
        condition_count=len(contract["conditions"]),
    )
    prepared_manifest = json.loads(json.dumps(manifest))

    manifest["status"] = "failed"
    manifest["error"] = {"type": "SyntheticFailure", "message": "failed once"}
    runner._persist_manifest(
        manifest_path,
        manifest,
        condition_count=len(contract["conditions"]),
    )
    tombstone = run_root / runner.FAILURE_TOMBSTONE_FILENAME
    assert tombstone.is_file()
    assert stat.S_IMODE(tombstone.stat().st_mode) == 0o400
    assert not list(run_root.glob(f".{tombstone.name}.*.tmp"))

    manifest_path.unlink()
    with pytest.raises(runner.LeakageLadderError, match="tombstone forbids plan-only"):
        runner._prepare_plan_only_run(
            plan_path=plan_path,
            manifest_path=manifest_path,
            contract=contract,
        )

    manifest_path.write_text(
        json.dumps(prepared_manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(runner.LeakageLadderError, match="deleted or rolled back"):
        runner._load_manifest(
            manifest_path,
            plan_path=plan_path,
            envelope=envelope,
        )

    os.chmod(tombstone, 0o600)
    with pytest.raises(runner.LeakageLadderError, match="must remain read-only"):
        runner._validate_failure_tombstone(
            tombstone,
            run_id=manifest["run_id"],
            topology_stage=manifest["topology_stage"],
            immutable_plan=manifest["immutable_plan"],
        )


@pytest.mark.parametrize("history_kind", ("run_root", "plan", "capture"))
def test_plan_only_never_recreates_a_manifest_over_run_history(
    tmp_path: Path,
    history_kind: str,
) -> None:
    contract = _contract()
    capture_root = tmp_path / "captures"
    _bind_run_capture_root(contract, capture_root)
    run_root = tmp_path / "run"
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    if history_kind == "run_root":
        run_root.mkdir()
    elif history_kind == "plan":
        run_root.mkdir()
        plan_path.write_text("historical plan bytes", encoding="utf-8")
    else:
        capture_root.mkdir()

    with pytest.raises(runner.LeakageLadderError, match="history but no intact manifest"):
        runner._prepare_plan_only_run(
            plan_path=plan_path,
            manifest_path=manifest_path,
            contract=contract,
        )

    assert not manifest_path.exists()


def test_execution_confirmations_are_exact_and_stage_specific() -> None:
    stage = "powered_selector_all_inputs_terminated"
    token = runner.STAGE_CONTRACTS[stage]["confirmation_token"]
    fixture = _fixture_evidence(stage)
    confirmation = runner._validate_confirmations(
        stage=stage,
        confirm_stage=stage,
        topology_token=token,
        no_antennas=True,
        tx1_matched=True,
        tx2_terminated_muted=True,
        rx1_conducted_reference=True,
        no_movement=True,
        fixture_evidence=fixture,
        selector_static_all_off=True,
    )
    assert confirmation["stage"] == stage
    assert confirmation["topology_confirmation_token"] == token
    assert confirmation["fixture_evidence_sha256"] == runner.canonical_json_sha256(fixture)

    with pytest.raises(runner.LeakageLadderError, match="topology-token"):
        runner._validate_confirmations(
            stage=stage,
            confirm_stage=stage,
            topology_token="DIRECT_RX2_50OHM_AT_PLUTO",
            no_antennas=True,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            no_movement=True,
            fixture_evidence=fixture,
            selector_static_all_off=True,
        )
    with pytest.raises(runner.LeakageLadderError, match="no-antennas"):
        runner._validate_confirmations(
            stage=stage,
            confirm_stage=stage,
            topology_token=token,
            no_antennas=False,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            no_movement=True,
            fixture_evidence=fixture,
            selector_static_all_off=True,
        )
    with pytest.raises(runner.LeakageLadderError, match="confirm-no-movement"):
        runner._validate_confirmations(
            stage=stage,
            confirm_stage=stage,
            topology_token=token,
            no_antennas=True,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            no_movement=False,
            fixture_evidence=fixture,
            selector_static_all_off=True,
        )


def test_condition_capture_persists_raw_abi2_artifact_and_markerless_analysis(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    calls: list[dict[str, Any]] = []
    mute_calls: list[str] = []
    capture_root = tmp_path / "captures"

    result = runner._capture_condition(
        condition,
        contract=contract,
        plan_evidence=_plan_evidence(tmp_path / "plan.json"),
        capture_root=capture_root,
        forbidden_stream_ids=set(),
        capture_boundary=_capture_boundary(_blocks(tone_offset_hz=100_037.0), calls=calls),
        mute_boundary=_passing_mute(mute_calls),
    )

    assert mute_calls == ["post_condition"]
    assert len(calls) == 1
    live = calls[0]
    assert live["samples_per_frame"] == runner.SAMPLES_PER_FRAME
    assert live["frame_count"] == runner.FRAME_COUNT
    assert live["kernel_buffers"] == 8
    assert live["plan"].tx_channel == 0
    assert live["plan"].tx_hardware_gain_db == -35.0
    assert live["plan"].tone_frequency_hz == 100_000
    assert result["metadata_abi"] == 2
    assert result["stream_id"] == 12345
    assert result["headroom_passed"] is True
    assert result["measurement_quality_passed"] is True
    assert result["tone_offset_hz_requested"] == 100_000
    assert result["tone_offset_hz_readback"] == 100_000
    assert result["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert result["pilot_confidence"] >= runner.MINIMUM_PILOT_CONFIDENCE
    assert result["selector_calibration_claim"] is False

    artifact_root = Path(result["artifact_path"])
    record = json.loads((artifact_root / runner.CONDITION_RECORD_NAME).read_text())
    assert record["continuity_audit"]["metadata_abi"] == 2
    assert record["continuity_audit"]["abi2_flags_counters_order_and_rate_verified"] is True
    assert record["capture"]["kernel_buffers"] == 8
    assert record["capture"]["tx_channel"] == 0
    assert record["capture"]["tx2_required_exact_muted"] is True
    assert record["capture"]["tone_offset_hz_requested"] == 100_000
    assert record["capture"]["tone_offset_hz_readback"] == 100_000
    assert record["capture"]["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert record["capture"]["pilot_frequency_refinement"]["quality_passed"] is True
    assert record["capture"]["rf_readback_evidence"]["tx_hardware_gain_readback_db_by_channel"] == [
        -35.0,
        -80.0,
    ]
    assert record["marker_independent_analysis"]["rx2_over_rx1"]["amplitude_ratio"] == (
        pytest.approx(0.1, rel=0.01)
    )
    assert record["accepted_for_selector_calibration"] is False
    assert record["may_be_used_as_selector_calibration"] is False


def test_reused_stream_id_is_rejected_and_quarantined(tmp_path: Path) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    with pytest.raises(runner.ConditionCaptureFailure) as captured:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids={12345},
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=_passing_mute(),
        )

    quarantine = captured.value.quarantine
    assert quarantine["accepted"] is False
    assert quarantine["may_be_used_for_selector_calibration"] is False
    assert Path(quarantine["path"]).is_dir()
    assert any(item["name"].endswith(".sigmf-data") for item in quarantine["files"])
    assert not [
        path
        for path in (tmp_path / "captures").iterdir()
        if path.is_dir() and path.name != ".failed"
    ]


def test_enodata_fragment_and_headroom_failure_are_quarantined(tmp_path: Path) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    fragment = _blocks()

    def enodata_capture(plan: Any, **kwargs: Any) -> Any:
        del plan
        kwargs["block_consumer"](fragment[0])
        raise OSError(61, "No data available")

    with pytest.raises(runner.ConditionCaptureFailure, match="No data") as enodata:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "enodata",
            forbidden_stream_ids=set(),
            capture_boundary=enodata_capture,
            mute_boundary=_passing_mute(),
        )
    assert enodata.value.quarantine["accepted"] is False
    assert any(item["name"] == "failure.json" for item in enodata.value.quarantine["files"])

    with pytest.raises(runner.ConditionCaptureFailure, match="headroom") as headroom:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "headroom",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(stream_id=23456, clipped=True)),
            mute_boundary=_passing_mute(),
        )
    assert headroom.value.quarantine["accepted"] is False
    assert not [
        path
        for path in (tmp_path / "headroom").iterdir()
        if path.is_dir() and path.name != ".failed"
    ]


def test_stage_execution_uses_preflight_condition_and_final_exact_mutes(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    confirmation = _confirmation(contract)
    mute_calls: list[str] = []

    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=confirmation,
        capture_root=tmp_path / "captures",
        capture_boundary=_capture_boundary(_blocks(stream_id=34567)),
        mute_boundary=_passing_mute(mute_calls),
        identity_boundary=_passing_identity(),
        runtime_attestation_boundary=_passing_runtime(),
        fixture_evidence_boundary=_passing_fixture(),
    )

    assert mute_calls == ["preflight", "post_condition", "final"]
    assert manifest["status"] == "complete"
    assert manifest["final_mute"]["status"] == "passed"
    assert manifest["attempts"][0]["status"] == "complete"
    assert manifest["attempts"][0]["automatic_retry_attempted"] is False
    result = manifest["attempts"][0]["result"]
    assert result["native_libiio_runtime_attestation"] == _native_attestation()
    assert (
        result["native_libiio_runtime_attestation_sha256"]
        == (contract["source"]["native_libiio_runtime_attestation_sha256"])
    )
    assert result["fixture_evidence"] == contract["fixture_evidence"]
    assert result["fixture_evidence_sha256"] == contract["fixture_evidence_sha256"]
    record = json.loads(Path(result["condition_record_path"]).read_text(encoding="utf-8"))
    assert record["native_libiio_runtime_attestation"] == _native_attestation()
    assert record["fixture_evidence"] == contract["fixture_evidence"]
    assert manifest["summary"]["completed_conditions"] == 1
    assert manifest["summary"]["selector_calibration_claim"] is False
    assert manifest["x_intervention_capture_manifest"] is None
    assert not (tmp_path / runner.X_CAPTURE_MANIFEST_FILENAME).exists()


def test_complete_stage_execution_tombstone_rejects_reuse_without_hardware(
    tmp_path: Path,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path, stream_id=34_568
    )
    capture_calls: list[dict[str, Any]] = []
    mute_calls: list[str] = []

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(
                _blocks(stream_id=99_999),
                calls=capture_calls,
            ),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert capture_calls == []
    assert mute_calls == []
    assert manifest["status"] == "complete"
    assert len(manifest["attempts"]) == 1


def test_stage_completion_rejects_reused_artifact_bytes_across_conditions(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    contract = _contract()
    _bind_run_capture_root(contract, capture_root)
    contract["conditions"] = contract["conditions"][:2]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    block_sets = iter((_blocks(stream_id=34_600), _blocks(stream_id=34_601)))

    def repeated_bytes_capture(plan: Any, **kwargs: Any) -> Any:
        return _capture_boundary(next(block_sets))(plan, **kwargs)

    with pytest.raises(runner.LeakageLadderError, match="reused an artifact, hash"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=capture_root,
            capture_boundary=repeated_bytes_capture,
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert manifest["status"] == "failed"
    assert manifest["attempts"][0]["status"] == "complete"
    assert manifest["attempts"][1]["outcome"] == "resume_validation_failed"
    assert manifest["attempts"][1]["quarantine"]["accepted"] is False


def test_resume_rejects_attempt_condition_different_from_immutable_plan(
    tmp_path: Path,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path, stream_id=34_569
    )
    manifest["attempts"][0]["condition"]["tx_hardware_gain_db"] = -1.0
    artifact_path = Path(manifest["attempts"][0]["result"]["artifact_path"])
    mute_calls: list[str] = []

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(stream_id=99_998)),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert mute_calls == []
    assert manifest["status"] == "complete"
    assert artifact_path.exists()
    assert manifest["attempts"][0]["quarantine"] is None


@pytest.mark.parametrize(
    ("result_path_key", "mutation"),
    (
        ("artifact_data_path", b"\x00"),
        ("artifact_metadata_path", b"\n"),
        ("condition_record_path", b"\n"),
    ),
)
def test_resume_rejects_any_changed_artifact_record_bytes(
    tmp_path: Path,
    result_path_key: str,
    mutation: bytes,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path, stream_id=34_570
    )
    result = manifest["attempts"][0]["result"]
    changed_path = Path(result[result_path_key])
    with changed_path.open("ab") as handle:
        handle.write(mutation)

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(stream_id=99_997)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert manifest["status"] == "complete"
    assert manifest["attempts"][0]["quarantine"] is None


def test_resume_quarantines_finalized_and_partial_orphans_for_current_plan(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    finalized = capture_root / "finalized-orphan"
    finalized.mkdir(parents=True)
    plan_evidence = _plan_evidence(tmp_path / "plan.json")
    (finalized / runner.CONDITION_RECORD_NAME).write_text(
        json.dumps({"immutable_plan": plan_evidence}),
        encoding="utf-8",
    )
    partial = capture_root / ".partial" / "partial-orphan"
    partial.mkdir(parents=True)
    (partial / "fragment.bin").write_bytes(b"partial")

    quarantines = runner._quarantine_orphaned_current_plan_artifacts(
        capture_root,
        manifest={"attempts": []},
        plan_evidence=plan_evidence,
    )

    assert len(quarantines) == 2
    assert not finalized.exists()
    assert not partial.exists()
    assert all(item["accepted"] is False for item in quarantines)
    assert {Path(item["path"]).name for item in quarantines} == {
        "finalized-orphan.orphaned",
        "partial-orphan.orphaned",
    }


def test_resume_refuses_to_move_artifact_bound_to_a_different_plan(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    foreign = capture_root / "foreign-plan-artifact"
    foreign.mkdir(parents=True)
    (foreign / runner.CONDITION_RECORD_NAME).write_text(
        json.dumps({"immutable_plan": {"plan_file_sha256": "f" * 64}}),
        encoding="utf-8",
    )

    with pytest.raises(runner.LeakageLadderError, match="different immutable plan"):
        runner._quarantine_orphaned_current_plan_artifacts(
            capture_root,
            manifest={"attempts": []},
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
        )

    assert foreign.is_dir()


def test_orphan_quarantine_rejects_symlink_candidate_outside_run_root(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    external = tmp_path / "external-artifact"
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"must remain untouched")
    candidate = capture_root / "forged-orphan"
    candidate.symlink_to(external, target_is_directory=True)

    with pytest.raises(runner.LeakageLadderError, match="must not be a symlink"):
        runner._quarantine_orphaned_current_plan_artifacts(
            capture_root,
            manifest={"attempts": []},
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
        )

    assert candidate.is_symlink()
    assert sentinel.read_bytes() == b"must remain untouched"
    assert not (external / "failure.json").exists()


def test_orphan_quarantine_never_seals_a_tree_containing_a_symlink(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    candidate = capture_root / "nested-forged-orphan"
    candidate.mkdir(parents=True)
    external = tmp_path / "outside-record.json"
    external.write_text('{"external": true}', encoding="utf-8")
    (candidate / runner.CONDITION_RECORD_NAME).symlink_to(external)

    with pytest.raises(runner.LeakageLadderError, match="contains a symlink"):
        runner._quarantine_orphaned_current_plan_artifacts(
            capture_root,
            manifest={"attempts": []},
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
        )

    assert candidate.is_dir()
    assert external.read_text(encoding="utf-8") == '{"external": true}'
    assert not (external.parent / "failure.json").exists()


def test_resume_rejects_accepted_artifact_file_symlink_without_sealing_target(
    tmp_path: Path,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path, stream_id=34_571
    )
    result = manifest["attempts"][0]["result"]
    data_path = Path(result["artifact_data_path"])
    external = tmp_path / "external-data.sigmf-data"
    external.write_bytes(data_path.read_bytes())
    data_path.unlink()
    data_path.symlink_to(external)

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(stream_id=99_996)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert external.is_file()
    assert data_path.is_symlink()
    assert manifest["attempts"][0]["quarantine"] is None


def test_resume_downgrade_never_moves_an_artifact_outside_run_root(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    external = tmp_path / "outside-run-root"
    external.mkdir()
    attempt: dict[str, Any] = {"status": "complete"}
    error = runner.LeakageLadderError("synthetic invalid result")

    runner._downgrade_and_quarantine_completed_attempt(
        attempt,
        result={"artifact_path": str(external)},
        capture_root=capture_root,
        error=error,
    )

    assert external.is_dir()
    assert attempt["status"] == "failed"
    assert attempt["quarantine"] is None


def test_read_only_identity_mismatch_blocks_all_mute_and_capture_boundaries(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []
    capture_calls: list[dict[str, Any]] = []

    def mismatched_identity(serial: str, requested_uri: str) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": "usb:9.9.9",
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "error": None,
        }

    with pytest.raises(runner.LeakageLadderError, match="read-only USB identity"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=mismatched_identity,
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert capture_calls == []
    assert mute_calls == ["preflight", "final"]
    assert manifest["status"] == "failed"
    assert manifest["identity_preflight"]["resolved_uri"] == "usb:9.9.9"


def test_native_runtime_mismatch_blocks_fixture_identity_mute_and_capture(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    calls: list[str] = []

    def wrong_runtime() -> dict[str, Any]:
        value = _native_attestation()
        value["library_sha256"] = "0" * 64
        return value

    with pytest.raises(runner.LeakageLadderError, match="native libiio runtime differs"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=lambda *args, **kwargs: calls.append("capture"),
            mute_boundary=_passing_mute(calls),
            identity_boundary=lambda *args, **kwargs: calls.append("identity"),
            runtime_attestation_boundary=wrong_runtime,
            fixture_evidence_boundary=lambda *args, **kwargs: calls.append("fixture"),
        )

    assert calls == ["final"]
    assert manifest["status"] == "failed"
    assert manifest["native_runtime_preflight"]["status"] == "failed"
    assert manifest["fixture_evidence_preflight"] is None


def test_fixture_v2_mismatch_blocks_identity_mute_and_capture(tmp_path: Path) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    calls: list[str] = []

    def wrong_fixture(expected: dict[str, Any]) -> dict[str, Any]:
        observed = json.loads(json.dumps(expected))
        observed["shared_fixture"]["reference_planes"]["rx2"] = "different-plane"
        return {
            "schema": 1,
            "evidence_kind": "general_fixture_v2_preflight",
            "status": "failed",
            "fixture_evidence": observed,
            "fixture_evidence_sha256": runner.canonical_json_sha256(observed),
            "exact_frozen_evidence_match": False,
            "error": None,
        }

    with pytest.raises(runner.LeakageLadderError, match="fixture manifest, per-run setup"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=lambda *args, **kwargs: calls.append("capture"),
            mute_boundary=_passing_mute(calls),
            identity_boundary=lambda *args, **kwargs: calls.append("identity"),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=wrong_fixture,
        )

    assert calls == ["final"]
    assert manifest["status"] == "failed"
    assert manifest["native_runtime_preflight"]["status"] == "passed"
    assert manifest["fixture_evidence_preflight"]["status"] == "failed"


def test_selector_connected_condition_attests_static_all_off_before_and_after(
    tmp_path: Path,
) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    selector_calls: list[str] = []

    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=_confirmation(contract),
        capture_root=tmp_path / "captures",
        capture_boundary=_capture_boundary(_blocks(stream_id=45678)),
        mute_boundary=_passing_mute(),
        identity_boundary=_passing_identity(),
        selector_boundary=_passing_selector(selector_calls),
        selector_image_boundary=_passing_selector_image(),
        runtime_attestation_boundary=_passing_runtime(),
        fixture_evidence_boundary=_passing_fixture(),
    )

    assert selector_calls == [
        "initial_state_before_command",
        "before_condition",
        "after_condition",
        "condition_cleanup_all_off",
        "final_cleanup_all_off",
    ]
    result = manifest["attempts"][0]["result"]
    assert result["selector_static_all_off_before"]["status"] == "passed"
    assert result["selector_static_all_off_after"]["status"] == "passed"
    assert result["selector_static_all_off_cleanup"]["status"] == "passed"
    assert result["selector_static_all_off_before"]["pre_command_was_all_off"] is True
    assert result["selector_static_all_off_before"]["pre_command"] is not None
    assert result["selector_static_all_off_before"]["commanded"] is not None
    assert result["selector_static_all_off_after"]["operation"] == "read_only"
    assert result["selector_static_all_off_after"]["command_was_issued"] is False


def test_selector_connected_execution_uses_shared_exclusive_lock(
    tmp_path: Path,
) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    mute_calls: list[str] = []
    selector_calls: list[str] = []
    capture_calls: list[dict[str, Any]] = []
    lock_root = tmp_path / "shared-selector-lock"

    with (
        runner._board_lock(lock_root),
        pytest.raises(runner.LeakageLadderError, match="lock is already held"),
    ):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            selector_boundary=_passing_selector(selector_calls),
            selector_image_boundary=_passing_selector_image(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=lock_root,
        )

    assert mute_calls == []
    assert selector_calls == []
    assert capture_calls == []
    assert manifest["status"] == "prepared"


def test_selector_initial_state_must_already_be_all_off_without_silent_repair(
    tmp_path: Path,
) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    calls: list[str] = []
    mute_calls: list[str] = []
    capture_calls: list[dict[str, Any]] = []
    passing_selector = _passing_selector()

    def initially_not_all_off(
        control: dict[str, Any],
        purpose: str,
    ) -> dict[str, Any]:
        calls.append(purpose)
        evidence = passing_selector(control, purpose)
        if purpose == "initial_state_before_command":
            evidence["status"] = "failed"
            evidence["readback"]["applied_code"] ^= 1
        return evidence

    with pytest.raises(runner.LeakageLadderError, match="not already static ALL_OFF"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            selector_boundary=initially_not_all_off,
            selector_image_boundary=_passing_selector_image(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=tmp_path / "selector-lock",
        )

    assert calls == ["initial_state_before_command", "exception_cleanup_all_off"]
    assert mute_calls == ["preflight", "final"]
    assert capture_calls == []
    initial = manifest["selector_initial_state"]
    assert initial["operation"] == "read_only"
    assert initial["command_was_issued"] is False
    assert initial["status"] == "failed"
    assert manifest["final_selector_cleanup"]["status"] == "passed"
    assert (tmp_path / runner.FAILURE_TOMBSTONE_FILENAME).is_file()


@pytest.mark.parametrize(
    ("purpose", "mutation"),
    (
        (
            "initial_state_before_command",
            lambda evidence: evidence["readback"].pop("command_sequence"),
        ),
        (
            "before_condition",
            lambda evidence: evidence["commanded"].update(command_valid=False),
        ),
        (
            "condition_cleanup_all_off",
            lambda evidence: evidence["pre_command"].update(command_sequence=-1),
        ),
        (
            "condition_cleanup_all_off",
            lambda evidence: evidence.update(pre_command_was_all_off=False),
        ),
    ),
)
def test_selector_attestation_rejects_missing_or_inconsistent_snapshot_fields(
    purpose: str,
    mutation: Any,
) -> None:
    control = _selector_control()
    evidence = _passing_selector()(control, purpose)
    mutation(evidence)

    assert not runner._selector_passed(
        evidence,
        selector_control=control,
        purpose=purpose,
    )


@pytest.mark.parametrize(
    "manifest_field",
    ("selector_initial_state_attempts", "final_selector_cleanup_attempts"),
)
def test_resume_revalidates_manifest_selector_attestation_history(
    tmp_path: Path,
    manifest_field: str,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path,
        stream_id=45_680,
        stage="powered_selector_all_inputs_terminated",
    )
    manifest[manifest_field][0]["readback"]["applied_code"] ^= 1
    selector_calls: list[str] = []

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(stream_id=99_995)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            selector_boundary=_passing_selector(selector_calls),
            selector_image_boundary=_passing_selector_image(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=tmp_path / "selector-lock",
        )

    assert selector_calls == []
    assert manifest["status"] == "complete"


@pytest.mark.parametrize(
    "result_field",
    (
        "selector_static_all_off_before",
        "selector_static_all_off_after",
        "selector_static_all_off_cleanup",
    ),
)
def test_resume_revalidates_every_condition_selector_readback(
    tmp_path: Path,
    result_field: str,
) -> None:
    contract, envelope, manifest, plan_path, manifest_path = _completed_single_condition_run(
        tmp_path,
        stream_id=45_681,
        stage="powered_selector_all_inputs_terminated",
    )
    result = manifest["attempts"][0]["result"]
    result[result_field]["readback"]["applied_code"] ^= 1

    with pytest.raises(runner.LeakageLadderError, match="cannot be resumed or retried"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(stream_id=99_994)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            selector_boundary=_passing_selector(),
            selector_image_boundary=_passing_selector_image(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=tmp_path / "selector-lock",
        )

    assert manifest["attempts"][0]["outcome"] == "measurement_quality_passed"
    assert manifest["attempts"][0]["quarantine"] is None


def test_selector_all_off_readback_failure_blocks_rf_capture(tmp_path: Path) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    capture_calls: list[dict[str, Any]] = []

    def failed_selector(control: dict[str, Any], purpose: str) -> dict[str, Any]:
        code = control["command"]["code"]
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "all_off_code": code,
            "readback": {
                "applied_code": code ^ 1,
                "command_valid": True,
                "lease_active": False,
                "guard_active": False,
                "invalid_command": False,
            },
            "error": None,
        }

    with pytest.raises(runner.ConditionCaptureFailure, match="pre-condition"):
        runner._capture_condition(
            contract["conditions"][0],
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(),
            selector_boundary=failed_selector,
        )

    assert capture_calls == []


def test_measurement_quality_rejection_makes_stage_unsuccessful_but_keeps_raw_artifact(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"

    with pytest.raises(runner.LeakageLadderError, match="measurement quality"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(
                _blocks(stream_id=56789, rx2_alternating_phase=True)
            ),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert manifest["status"] == "failed"
    attempt = manifest["attempts"][0]
    assert attempt["status"] == "complete"
    assert attempt["outcome"] == "measurement_quality_rejected"
    assert attempt["quarantine"] is None
    assert Path(attempt["result"]["artifact_path"]).is_dir()
    assert (
        "detected_rx2_transfer_coherence_below_minimum"
        in attempt["result"]["measurement_quality_rejection_reasons"]
    )


def test_capture_failure_still_runs_final_mute_and_persists_failed_manifest(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []

    def failed_capture(plan: Any, **kwargs: Any) -> Any:
        del plan, kwargs
        raise OSError(61, "No data available")

    with pytest.raises(runner.ConditionCaptureFailure, match="No data"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=failed_capture,
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert mute_calls == ["preflight", "post_condition", "final"]
    persisted = json.loads(manifest_path.read_text())
    assert persisted["status"] == "failed"
    assert persisted["attempts"][0]["status"] == "failed"
    assert persisted["attempts"][0]["quarantine"]["accepted"] is False
    assert persisted["final_mute"]["status"] == "passed"


def test_malformed_post_condition_mute_is_failed_and_quarantined(tmp_path: Path) -> None:
    contract = _contract()

    def malformed_mute(serial: str, purpose: str) -> Any:
        del serial, purpose
        return None

    with pytest.raises(runner.ConditionCaptureFailure) as captured:
        runner._capture_condition(
            contract["conditions"][0],
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=malformed_mute,
        )

    assert captured.value.post_mute["status"] == "failed"
    assert captured.value.post_mute["error"]["type"] == "InvalidMuteAttestation"
    assert captured.value.quarantine["accepted"] is False


def test_failed_attempt_cannot_be_retried_if_top_level_status_is_damaged(
    tmp_path: Path,
) -> None:
    contract = _contract()
    _bind_run_capture_root(contract, tmp_path / "captures")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest["status"] = "prepared"
    manifest["attempts"].append(
        {
            "condition_id": contract["conditions"][0]["condition_id"],
            "status": "failed",
        }
    )
    mute_calls: list[str] = []

    with pytest.raises(runner.LeakageLadderError, match="cannot retry"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
        )

    assert mute_calls == ["final"]
    assert len(manifest["attempts"]) == 1


def test_selector_image_mismatch_is_ordered_after_mute_and_before_any_mailbox(
    tmp_path: Path,
) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    capture_root = tmp_path / "captures"
    _bind_run_capture_root(contract, capture_root)
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(f"mute:{purpose}")
        return _passing_mute()(serial, purpose)

    def mismatch(control: dict[str, Any]) -> dict[str, Any]:
        calls.append("target-image")
        evidence = _passing_selector_image()(control)
        evidence.update(
            {
                "status": "failed",
                "exact_bin_and_uid_match": False,
                "reviewed_image_started_only_after_exact_match": False,
                "target_may_have_started_before_failure_halt": False,
                "failure_halt_required": True,
                "failure_halt": _passing_selector_target_halt(control),
                "target_kept_halted_on_failure": True,
                "error": {"type": "SyntheticMismatch", "message": "mismatch"},
            }
        )
        return evidence

    def selector(_control: dict[str, Any], purpose: str) -> dict[str, Any]:
        calls.append(f"selector:{purpose}")
        raise AssertionError("mailbox must not be accessed after image mismatch")

    with pytest.raises(runner.LeakageLadderError, match="full-BIN extent or UID"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=capture_root,
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=mute,
            identity_boundary=_passing_identity(),
            selector_boundary=selector,
            selector_image_boundary=mismatch,
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=tmp_path / "selector-lock",
        )

    assert calls == ["mute:preflight", "target-image", "mute:final"]
    assert manifest["final_selector_cleanup"] is None
    tombstone = tmp_path / runner.EXECUTION_TOMBSTONE_FILENAME
    assert tombstone.is_file()
    assert not tombstone.stat().st_mode & 0o222


def test_selector_image_reset_run_failure_is_separately_halted_without_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firmware = b"reviewed-static-selector-image"
    uid = bytes.fromhex("00112233445566778899aabb")
    firmware_path = tmp_path / "pluto_bench.bin"
    firmware_path.write_bytes(firmware)
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    control = _selector_control()
    control["openocd_config"] = {
        "path": str(config_path),
        "file_sha256": runner.sha256_path(config_path),
    }
    control["selector_flash_evidence"]["board_id"] = f"stm32c011-{uid.hex()}"
    target = control["target_image_admission_contract"]
    target.update(
        {
            "firmware_bin_path": str(firmware_path),
            "firmware_bin_sha256": hashlib.sha256(firmware).hexdigest(),
            "firmware_bin_size_bytes": len(firmware),
            "board_id": control["selector_flash_evidence"]["board_id"],
        }
    )
    monkeypatch.setattr(
        runner,
        "_cross_bind_selector_control_to_sealed_image",
        lambda _control: None,
    )
    commands: list[str] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> SimpleNamespace:
        openocd_command = command[-1]
        commands.append(openocd_command)
        if "dump_image" in openocd_command:
            dump_paths = re.findall(r"dump_image \{([^}]+)\}", openocd_command)
            assert len(dump_paths) == 2
            Path(dump_paths[0]).write_bytes(firmware)
            Path(dump_paths[1]).write_bytes(uid)
            return SimpleNamespace(returncode=0)
        if openocd_command == "init; reset run; shutdown":
            return SimpleNamespace(returncode=17)
        assert openocd_command == "init; halt; shutdown"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    evidence = runner._live_selector_image_admission(control)

    assert commands[-2:] == ["init; reset run; shutdown", "init; halt; shutdown"]
    assert evidence["status"] == "failed"
    assert evidence["target_may_have_started_before_failure_halt"] is True
    assert evidence["failure_halt"]["command"] == "init; halt; shutdown"
    assert evidence["target_kept_halted_on_failure"] is True
    assert evidence["mailbox_access_performed"] is False
    assert runner._selector_image_failure_halted(evidence, selector_control=control)


def test_rejected_selector_image_evidence_triggers_conservative_separate_halt() -> None:
    control = _selector_control()
    halt_calls: list[str] = []

    def malformed_pass(candidate: dict[str, Any]) -> dict[str, Any]:
        evidence = _passing_selector_image()(candidate)
        evidence["byte_count"] += 1
        return evidence

    def halt(candidate: dict[str, Any], purpose: str) -> dict[str, Any]:
        halt_calls.append(purpose)
        return _passing_selector_target_halt(candidate, purpose)

    evidence = runner._call_selector_image_admission(
        malformed_pass,
        control,
        halt_boundary=halt,
    )

    assert halt_calls == ["image_admission_failure_cleanup"]
    assert evidence["status"] == "failed"
    assert evidence["target_may_have_started_before_failure_halt"] is True
    assert evidence["target_kept_halted_on_failure"] is True
    assert evidence["mailbox_access_performed"] is False
    assert runner._selector_image_failure_halted(evidence, selector_control=control)


def test_local_storage_rejection_precedes_run_burn_and_all_hardware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    capture_root = tmp_path / "captures"
    _bind_run_capture_root(contract, capture_root)
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "_assert_local_rpi_storage",
        lambda *_paths: (_ for _ in ()).throw(
            runner.LeakageLadderError("not on the Raspberry Pi local filesystem")
        ),
    )

    with pytest.raises(runner.LeakageLadderError, match="local filesystem"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=capture_root,
            mute_boundary=lambda *_args: calls.append("mute"),
            identity_boundary=lambda *_args: calls.append("identity"),
        )

    assert calls == []
    assert not (tmp_path / runner.EXECUTION_TOMBSTONE_FILENAME).exists()


def test_selector_control_cross_binding_rejects_substituted_openocd_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files: dict[str, Path] = {}
    for name in (
        "build_manifest",
        "elf",
        "firmware_bin",
        "openocd_config",
        "profile",
        "profile_header",
    ):
        path = tmp_path / name
        path.write_bytes(f"{name}\n".encode())
        files[name] = path

    def identity(path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "sha256": runner.sha256_path(path),
            "size_bytes": path.stat().st_size,
        }

    control = _selector_control()
    control.pop("target_image_admission_contract")
    control["bench_manifest"].update(
        {
            "path": str(files["build_manifest"]),
            "file_sha256": runner.sha256_path(files["build_manifest"]),
        }
    )
    control["openocd_config"].update(
        {
            "path": str(files["openocd_config"]),
            "file_sha256": runner.sha256_path(files["openocd_config"]),
        }
    )
    control["control_profile"].update(
        {
            "path": str(files["profile"]),
            "file_sha256": runner.sha256_path(files["profile"]),
            "header_path": str(files["profile_header"]),
            "header_file_sha256": runner.sha256_path(files["profile_header"]),
            "profile_id": "fast20-v1",
            "revision": 1,
            "contract_sha256": "c" * 64,
        }
    )
    sealed = {
        "frozen_inputs": {
            "files": {name: identity(path) for name, path in files.items()},
            "control_profile": {
                "id": "fast20-v1",
                "revision": 1,
                "contract_sha256": "c" * 64,
                "all_off_code": 15,
            },
        }
    }
    monkeypatch.setattr(runner, "_sealed_selector_document", lambda _flash: sealed)

    assert runner._cross_bind_selector_control_to_sealed_image(control) == sealed
    control["openocd_config"]["file_sha256"] = "0" * 64
    with pytest.raises(runner.LeakageLadderError, match="sealed selector frozen-input"):
        runner._cross_bind_selector_control_to_sealed_image(control)


def test_json_serialization_is_deterministic_for_complex_and_nonfinite_values() -> None:
    value = runner._json_safe(
        {
            "phasor": 1.5 - 2.5j,
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
        }
    )
    assert value == {
        "phasor": {"real": 1.5, "imag": -2.5},
        "positive_infinity": None,
        "negative_infinity": None,
    }
    json.dumps(value, allow_nan=False)


def test_x_cli_prebinding_is_optional_for_normal_runs_and_complete_for_x_mode() -> None:
    plain = SimpleNamespace(
        x_mode=False,
        x_intervention_contract_id=None,
        x_run_role=None,
        x_implicated_boundary_stage=None,
        x_installed_fixture_manifest=None,
        x_capture_fixture_manifest=None,
        x_acquisition_index=None,
        x_freshness_epoch_id=None,
        selector_flash_evidence=None,
        selector_flash_evidence_sha256=None,
        selector_flash_run_id=None,
    )
    runner._validate_x_cli_mode(plain)
    plain.x_run_role = "boundary_baseline"
    with pytest.raises(runner.LeakageLadderError, match="only with --x-mode"):
        runner._validate_x_cli_mode(plain)

    plain.x_mode = True
    with pytest.raises(runner.LeakageLadderError, match="--x-mode requires"):
        runner._validate_x_cli_mode(plain)
    plain.x_intervention_contract_id = "contract-r01"
    plain.x_implicated_boundary_stage = "direct_rx2_termination"
    plain.x_installed_fixture_manifest = Path("/tmp/installed.json")
    plain.x_capture_fixture_manifest = Path("/tmp/capture.json")
    plain.x_acquisition_index = 1
    plain.x_freshness_epoch_id = "epoch-r01"
    plain.selector_flash_evidence = Path("/tmp/flash.json")
    plain.selector_flash_evidence_sha256 = "a" * 64
    plain.selector_flash_run_id = "flash-r01"
    runner._validate_x_cli_mode(plain)


@pytest.mark.parametrize(
    ("stage", "role", "implicated"),
    (
        ("direct_rx2_termination", "boundary_baseline", "direct_rx2_termination"),
        ("rx2_cable_terminated", "boundary_intervention", "rx2_cable_terminated"),
        (
            "powered_selector_all_inputs_terminated",
            "boundary_baseline",
            "powered_selector_all_inputs_terminated",
        ),
        ("full_conducted_fixture", "full_fixture_intervention", "direct_rx2_termination"),
        ("full_conducted_fixture", "full_fixture_baseline", "full_conducted_fixture"),
    ),
)
def test_x_prebinding_accepts_exact_boundary_mapping_and_e_two_run_reduction(
    tmp_path: Path,
    stage: str,
    role: str,
    implicated: str,
) -> None:
    contract = _x_contract(
        tmp_path,
        stage=stage,
        role=role,
        implicated_stage=implicated,
    )
    prebinding = contract["x_intervention_prebinding"]
    context = contract["x_intervention_capture_context"]
    assert set(prebinding) == {
        "schema",
        "binding_kind",
        "contract_id",
        "run_role",
        "installed_fixture_revision_sha256",
    }
    assert prebinding["run_role"] == role
    assert context["implicated_boundary_stage"] == implicated
    assert context["capture_state_fixture"]["fixture_manifest_path"].startswith(str(tmp_path))


def test_x_prebinding_rejects_role_topology_and_fixture_revision_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(runner.LeakageLadderError, match="must run the predeclared"):
        _x_contract(
            tmp_path / "wrong-stage",
            stage="rx2_cable_terminated",
            role="boundary_baseline",
            implicated_stage="direct_rx2_termination",
        )
    with pytest.raises(runner.LeakageLadderError, match="exactly the two full-fixture"):
        runner._validate_x_role_topology(
            role="boundary_baseline",
            implicated_stage="full_conducted_fixture",
            run_stage="full_conducted_fixture",
        )
    before = _write_x_full_fixture(tmp_path / "same-before.json", current_limit_a=0.4)
    fixture = _fixture_evidence("direct_rx2_termination")
    with pytest.raises(runner.LeakageLadderError, match="distinct from installed-after"):
        runner._x_intervention_contract_from_manifests(
            contract_id="contract-r01",
            run_role="boundary_baseline",
            implicated_boundary_stage="direct_rx2_termination",
            installed_fixture_manifest_path=before,
            capture_fixture_manifest_path=before,
            acquisition_index=1,
            freshness_epoch_id="epoch-r01",
            stage="direct_rx2_termination",
            board_id="board-a",
            serial=SERIAL,
            fixture_evidence=fixture,
            selector_flash_evidence=_selector_flash_binding(),
        )


def test_x_prebinding_rejects_symlinked_global_fixture_before_any_run(
    tmp_path: Path,
) -> None:
    before = _write_x_full_fixture(tmp_path / "before.json", current_limit_a=0.4)
    after = _write_x_full_fixture(tmp_path / "after.json", current_limit_a=0.5)
    linked_after = tmp_path / "linked-after.json"
    linked_after.symlink_to(after)
    with pytest.raises(runner.LeakageLadderError, match="symlink"):
        runner._x_intervention_contract_from_manifests(
            contract_id="contract-r01",
            run_role="boundary_baseline",
            implicated_boundary_stage="direct_rx2_termination",
            installed_fixture_manifest_path=linked_after,
            capture_fixture_manifest_path=before,
            acquisition_index=1,
            freshness_epoch_id="epoch-r01",
            stage="direct_rx2_termination",
            board_id="board-a",
            serial=SERIAL,
            fixture_evidence=_fixture_evidence("direct_rx2_termination"),
            selector_flash_evidence=_selector_flash_binding(),
        )


@pytest.mark.parametrize(
    ("stage", "role", "safe_status"),
    (
        ("direct_rx2_termination", "boundary_baseline", "physical_disconnect_verified"),
        (
            "powered_selector_all_inputs_terminated",
            "boundary_intervention",
            "mailbox_all_off_verified",
        ),
    ),
)
def test_x_execution_emits_source_bound_abi2_manifest_and_truthful_selector_safety(
    tmp_path: Path,
    stage: str,
    role: str,
    safe_status: str,
) -> None:
    contract = _x_contract(
        tmp_path,
        stage=stage,
        role=role,
        implicated_stage=stage,
    )
    capture_root = tmp_path / "captures"
    _bind_run_capture_root(contract, capture_root)
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / runner.PLAN_FILENAME
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / runner.MANIFEST_FILENAME

    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=_confirmation(contract),
        capture_root=capture_root,
        capture_boundary=_capture_boundary(_blocks(stream_id=72_000)),
        mute_boundary=_passing_mute(),
        identity_boundary=_passing_identity(),
        selector_boundary=_passing_selector(),
        selector_image_boundary=_passing_selector_image(),
        runtime_attestation_boundary=_passing_runtime(),
        fixture_evidence_boundary=_passing_fixture(),
        selector_lock_root=tmp_path / "selector-lock",
    )

    binding = manifest["x_intervention_capture_manifest"]
    assert binding is not None
    x_path = Path(binding["path"])
    document = json.loads(x_path.read_text(encoding="utf-8"))
    assert document["run_kind"] == runner.X_CAPTURE_MANIFEST_KIND
    assert document["run_role"] == role
    assert document["topology_stage"] == stage
    assert document["topology_fixture_sha256"] == contract["fixture_evidence_sha256"]
    assert document["final_mute_verified"] is True
    assert document["final_selector_safe_state"]["status"] == safe_status
    assert document["measurement_quality_rejection_reasons"] == []
    assert len(document["captures"]) == 1
    capture = document["captures"][0]
    assert capture["abi2_continuity_verified"] is True
    assert capture["measurement_quality_passed"] is True
    for name in ("raw_iq_file", "metadata_file", "condition_record_file"):
        file_binding = capture[name]
        assert Path(file_binding["path"]).is_file()
        assert runner.sha256_path(Path(file_binding["path"])) == file_binding["sha256"]
    assert stat.S_IMODE(x_path.stat().st_mode) == 0o400

    repository = str(SCRIPT.parents[1])
    sys.path.insert(0, repository)
    try:
        from scripts import prepare_5g8_selected_state_inputs as t8_inputs
    finally:
        sys.path.remove(repository)

    admitted = t8_inputs._accepted_x_manifest(
        x_path,
        role=role,
        contract_id=str(contract["x_intervention_prebinding"]["contract_id"]),
        change_plan_sha256="f" * 64,
        expected_plan={
            "run_id": contract["run_id"],
            "plan_file": runner._x_bound_file(plan_path, "test X plan"),
        },
    )
    assert admitted["acceptance_revalidated"] is True


def test_x_connected_acceptance_rejects_missing_final_all_off_without_output(
    tmp_path: Path,
) -> None:
    contract = _x_contract(
        tmp_path,
        stage="powered_selector_all_inputs_terminated",
        role="boundary_intervention",
        implicated_stage="powered_selector_all_inputs_terminated",
    )
    capture_root = tmp_path / "captures"
    _bind_run_capture_root(contract, capture_root)
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / runner.PLAN_FILENAME
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / runner.MANIFEST_FILENAME

    def fail_final_selector(control: dict[str, Any], purpose: str) -> dict[str, Any]:
        evidence = _passing_selector()(control, purpose)
        if purpose == "final_cleanup_all_off":
            evidence["status"] = "failed"
            evidence["error"] = {"type": "SyntheticFailure", "message": "not all off"}
        return evidence

    with pytest.raises(runner.LeakageLadderError, match="final mute or selector"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_root=capture_root,
            capture_boundary=_capture_boundary(_blocks(stream_id=72_001)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            selector_boundary=fail_final_selector,
            selector_image_boundary=_passing_selector_image(),
            runtime_attestation_boundary=_passing_runtime(),
            fixture_evidence_boundary=_passing_fixture(),
            selector_lock_root=tmp_path / "selector-lock",
        )

    assert manifest["status"] == "failed"
    assert manifest["x_intervention_capture_manifest"] is None
    assert not (tmp_path / runner.X_CAPTURE_MANIFEST_FILENAME).exists()
