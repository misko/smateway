from __future__ import annotations

import importlib.util
import json
import struct
import sys
from dataclasses import asdict
from math import pi
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from smateway.arm_preserving_d2 import build_fixture_v2, canonical_sha256, validate_fixture_v2
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.closure_qualification import leaf_source_set_sha256
from smateway.hexcal import sha256_path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_arm_preserving_d2.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_arm_preserving_d2_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)

RUNNER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_arm_preserving_d2.py"
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "run_5g8_arm_preserving_d2_for_analyzer_tests", RUNNER_SCRIPT
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)

DOMAIN_HELPERS_PATH = Path(__file__).with_name("test_arm_preserving_d2.py")
DOMAIN_HELPERS_SPEC = importlib.util.spec_from_file_location(
    "arm_preserving_domain_helpers_for_analyzer", DOMAIN_HELPERS_PATH
)
assert DOMAIN_HELPERS_SPEC is not None and DOMAIN_HELPERS_SPEC.loader is not None
domain_helpers = importlib.util.module_from_spec(DOMAIN_HELPERS_SPEC)
sys.modules[DOMAIN_HELPERS_SPEC.name] = domain_helpers
DOMAIN_HELPERS_SPEC.loader.exec_module(domain_helpers)


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def _binding(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
    }


def _mailbox_bytes(manifest: Any, *, code: int) -> bytes:
    words = [0] * 9
    values = {
        "magic": manifest.magic,
        "version": manifest.version,
        "command_sequence": 11,
        "command_code": code,
        "command_lease_ms": 0,
        "acknowledged_sequence": 11,
        "applied_code": code,
        "remaining_lease_ms": 0,
        "status_flags": 1,
    }
    for name, value in values.items():
        words[manifest.offsets[name] // 4] = value
    return struct.pack("<9I", *words)


def _selector_live_evidence(
    evidence_root: Path,
    *,
    purpose: str,
    selector_control: dict[str, Any],
) -> dict[str, Any]:
    root = evidence_root / purpose
    root.mkdir(parents=True)
    manifest = analyzer.BenchManifest.load(Path(selector_control["build_manifest"]["path"]))
    code = int(selector_control["all_off_code"])
    mailbox_path = root / "mailbox.bin"
    mailbox_path.write_bytes(_mailbox_bytes(manifest, code=code))
    gpio_path = root / "gpioa-odr.bin"
    gpio_path.write_bytes(code.to_bytes(4, "little"))
    log_path = root / "openocd.json"
    _write_json(log_path, {"returncode": 0})
    status = analyzer.decode_mailbox(mailbox_path.read_bytes(), manifest)
    return {
        "status": "passed",
        "purpose": purpose,
        "control_sha256": canonical_sha256(selector_control),
        "all_off_code": code,
        "lease_ms": 0,
        "mailbox": status.as_dict(),
        "gpioa_odr_raw_value": code,
        "gpioa_odr_masked_selector_code": code,
        "mailbox_readback": _binding(mailbox_path),
        "gpioa_odr_readback": _binding(gpio_path),
        "openocd_log": _binding(log_path),
        "command_valid": True,
        "raw_mailbox_and_gpio_readback_passed": True,
        "error": None,
    }


def _native_attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": "/usr/local/lib/libiio.so.0.25",
        "library_path_from_proc_maps": True,
        "library_sha256": "d0a18bddcb54d182262acb2a9e31a88c81618cb43789320b8381c149777bef89",
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {"iio_device_get_kernel_buffers_count": True},
        "loader_search_path_first": "/usr/local/lib",
    }


def _load_observation(path: Path, fixture: Any) -> Any:
    return analyzer._load_observation(
        path,
        fixture,
        native_boundary=lambda: _native_attestation(),
    )


def _artifact(
    capture_root: Path,
    *,
    artifact_id: str,
    stream_id: int,
) -> tuple[dict[str, Any], dict[str, Any], np.ndarray]:
    sample_count = 300_000
    samples_per_block = 100_000
    index = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(2j * pi * 100_000.0 * index / 1_000_000.0)
    samples = np.vstack((500.0 * carrier, 80.0 * carrier * np.exp(0.3j))).astype(np.complex64)
    samples = (
        np.rint(samples.real).astype(np.float32) + 1j * np.rint(samples.imag).astype(np.float32)
    ).astype(np.complex64)
    root = capture_root / artifact_id
    root.mkdir(parents=True)
    components = np.empty((sample_count, 2, 2), dtype="<i2")
    for receiver in range(2):
        components[:, receiver, 0] = samples[receiver].real.astype("<i2")
        components[:, receiver, 1] = samples[receiver].imag.astype("<i2")
    raw = root / f"{artifact_id}.sigmf-data"
    raw.write_bytes(components.tobytes())
    blocks = []
    for sequence, start in enumerate(range(0, sample_count, samples_per_block)):
        duration_ns = samples_per_block * 1_000
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
                "sample_time_realtime_start_ns": 1_000_000_000 + sequence * duration_ns,
                "sample_time_realtime_end_ns": 1_000_000_000 + (sequence + 1) * duration_ns,
                "sample_time_monotonic_start_ns": 10_000_000_000 + sequence * duration_ns,
                "sample_time_monotonic_end_ns": 10_000_000_000 + (sequence + 1) * duration_ns,
                "sample_time_uncertainty_ns": 1,
            }
        )
    ledger = {
        "schema_version": 1,
        "metadata_abi": 2,
        "stream_id": stream_id,
        "block_count": 3,
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
            "pluto:radio": {"serial": "pluto-a", "uri": "usb:1.2.3"},
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
    metadata = root / f"{artifact_id}.sigmf-meta"
    _write_json(metadata, metadata_document)
    base = {
        "artifact_id": artifact_id,
        "path": str(root),
        "raw_iq_path": str(raw),
        "raw_iq_sha256": sha256_path(raw),
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
    }
    return {**base, "artifact_sha256": canonical_sha256(base)}, ledger, samples


def _accepted_run(tmp_path: Path) -> tuple[Path, Any, dict[str, Any]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    selector_flash = tmp_path / "selector-flash.json"
    selector_flash.write_text('{"sealed":true}\n', encoding="utf-8")
    selector_binding = _binding(selector_flash)
    base_fixture = domain_helpers._fixture_document()
    board_id = runner.DEFAULT_BOARD_ID
    fixture_document = build_fixture_v2(
        campaign_id=base_fixture["campaign_id"],
        board_id=board_id,
        pluto_serial=base_fixture["pluto_serial"],
        source_commit=base_fixture["source_commit"],
        components=base_fixture["components"],
        fixed_connection_ids=base_fixture["fixed_connection_ids"],
        reference_planes=base_fixture["reference_planes"],
        arm_paths=base_fixture["arm_paths"],
        splitter_output_terminations=base_fixture["splitter_output_terminations"],
        selector_input_terminations=base_fixture["selector_input_terminations"],
        selector_flash_attestation={
            **base_fixture["selector_flash_attestation"],
            "file": selector_binding,
            "board_id": board_id,
        },
        linearity_evidence_sha256s=base_fixture["linearity_evidence_sha256s"],
        rf_safety=base_fixture["rf_safety"],
    )
    fixture = validate_fixture_v2(fixture_document)
    fixture_path = tmp_path / "fixture.json"
    _write_json(fixture_path, fixture_document)
    fixture_file = _binding(fixture_path)

    setup_evidence = tmp_path / "setup.png"
    setup_evidence.write_bytes(b"synthetic setup evidence")
    setup_document = domain_helpers._setup(
        fixture_document,
        role="c_i",
        arm="ANT1",
        repeat_index=1,
        run_id="arm-run-a",
    )
    setup_document["fixture_file_sha256"] = fixture_file["sha256"]
    setup_document["setup_evidence"] = _binding(setup_evidence)
    setup_path = tmp_path / "setup.json"
    _write_json(setup_path, setup_document)
    setup_file = _binding(setup_path)

    bench_manifest_document = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "build/STM32C011F4P6/bench/pluto_bench.manifest.json"
        ).read_text(encoding="utf-8")
    )
    bench_manifest_path = tmp_path / "pluto_bench.manifest.json"
    _write_json(bench_manifest_path, bench_manifest_document)
    openocd_path = tmp_path / "openocd.cfg"
    openocd_path.write_text("# synthetic\n", encoding="utf-8")
    firmware_path = tmp_path / "pluto_bench.bin"
    firmware_path.write_bytes(b"reviewed synthetic bench firmware")
    mailbox = bench_manifest_document["mailbox"]
    selector_control = {
        "schema": 1,
        "control_kind": "sealed_bench_static_all_off",
        "selector_flash_attestation": selector_binding,
        "build_manifest": _binding(bench_manifest_path),
        "openocd_config": _binding(openocd_path),
        "target_image_admission": {
            "schema": 1,
            "flash_base_address": analyzer.FLASH_BASE_ADDRESS,
            "firmware_bin": _binding(firmware_path),
            "board_id": fixture.board_id,
            "expected_uid": fixture.board_id.removeprefix("stm32c011-"),
            "selector_flash_attestation_sha256": selector_binding["sha256"],
            "full_bin_extent_and_uid_required_before_mailbox": True,
            "mismatch_must_remain_halted": True,
        },
        "mailbox": {
            "address": mailbox["address"],
            "size": mailbox["size"],
            "magic": mailbox["magic"],
            "version": mailbox["version"],
            "max_lease_ms": mailbox["max_lease_ms"],
            "offsets": mailbox["offsets"],
        },
        "all_off_code": 8,
        "required_lease_ms": 0,
        "gpioa_odr_address": analyzer.GPIOA_ODR_ADDRESS,
        "selector_gpio_mask": analyzer.SELECTOR_GPIO_MASK,
        "live_raw_mailbox_and_gpio_readback_required": True,
    }

    source_repository = tmp_path / "smateway-source"
    source_files = []
    for relative in analyzer.REQUIRED_SOURCE_FILES:
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
    dependency_repository = tmp_path / "pluto-plus-utils-source"
    dependency_path = dependency_repository / "src/pluto_plus/artifacts.py"
    dependency_path.parent.mkdir(parents=True)
    dependency_path.write_text("# dependency\n", encoding="utf-8")
    dependency_files = [
        {
            "path": "src/pluto_plus/artifacts.py",
            "sha256": sha256_path(dependency_path),
            "size_bytes": dependency_path.stat().st_size,
        }
    ]
    native = _native_attestation()
    source = {
        "smateway": {
            "repository": str(source_repository),
            "commit": "a" * 40,
            "files": source_files,
            "source_files_sha256": canonical_sha256(source_files),
        },
        "pluto_plus_utils": {
            "repository_path": str(dependency_repository),
            "commit": "b" * 40,
            "files": dependency_files,
        },
        "dependency_files_sha256": canonical_sha256(dependency_files),
        "native_libiio": native,
        "native_libiio_sha256": analyzer.attestation_sha256(native),
    }
    condition_root = tmp_path / "run"
    capture_root = tmp_path / "captures"
    condition = {
        "role": "c_i",
        "arm": "ANT1",
        "repeat_index": 1,
        "topology_identity": {
            "canonical_json": fixture.topology("ANT1", "c_i").canonical_json,
            "sha256": fixture.topology("ANT1", "c_i").sha256,
        },
    }
    contract = {
        "schema": 1,
        "run_kind": "5g8_arm_preserving_c_i_or_d2_i_one_stream",
        "run_id": "arm-run-a",
        "condition_id": "arm-d2-campaign.c_i.ANT1.repeat-1.arm-run-a",
        "campaign_id": fixture.campaign_id,
        "board_id": fixture.board_id,
        "configuration": {"serial": fixture.pluto_serial, "uri": "usb:1.2.3"},
        "condition": condition,
        "fixture": {
            "document": fixture.document,
            "file": fixture_file,
            "fixture_sha256": fixture.fixture_sha256,
            "fixture_graph_sha256": fixture.fixture_graph_identity.sha256,
            "reference_plane_sha256": fixture.reference_plane_identity.sha256,
            "closure_plan_sha256": fixture.plan_identity.sha256,
        },
        "setup_attestation": {"document": setup_document, "file": setup_file},
        "selector_control": selector_control,
        "acquisition": {
            "center_frequency_hz": 5_800_000_000,
            "sample_rate_hz": 1_000_000,
            "bandwidth_hz": 800_000,
            "tone_offset_hz": 100_000,
            "samples_per_frame": 100_000,
            "frame_count": 3,
            "sample_count": 300_000,
            "kernel_buffers": 8,
            "receiver_gain_db": 40.0,
            "tx_hardware_gain_db": -20.0,
            "dds_scale": 0.125,
            "minimum_reference_snr_db": 20.0,
            "rf_safety": fixture.document["rf_safety"],
        },
        "source": source,
        "storage": {
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
            "condition_root": str(condition_root),
            "capture_root": str(capture_root),
        },
        "execution": {"automatic_retry": False},
    }
    condition_root.mkdir()
    plan_path = condition_root / "plan.json"
    _write_json(
        plan_path,
        {
            "schema": 1,
            "immutable": True,
            "plan_contract": contract,
            "plan_contract_sha256": canonical_sha256(contract),
        },
    )
    plan_path.chmod(0o400)

    artifact, ledger, samples = _artifact(
        capture_root,
        artifact_id="arm-artifact-a",
        stream_id=202,
    )
    monitor = AdcHeadroomMonitor(receiver_count=2)
    monitor.observe(samples)
    headroom = monitor.result()
    analysis = analyzer.analyze_coherent_leakage(
        samples[0],
        samples[1],
        sample_rate_hz=1_000_000,
        tone_offset_hz=100_000,
        block_duration_s=0.1,
        minimum_block_count=3,
    )
    live_root = condition_root / "selector-live-evidence"
    target_root = live_root / "target-image-admission"
    target_root.mkdir(parents=True)
    target_flash_path = target_root / "target-flash.bin"
    target_flash_path.write_bytes(firmware_path.read_bytes())
    target_uid_path = target_root / "target-uid.bin"
    expected_uid = fixture.board_id.removeprefix("stm32c011-")
    target_uid_path.write_bytes(bytes.fromhex(expected_uid))
    target_read_log = target_root / "readback-openocd.json"
    _write_json(target_read_log, {"returncode": 0})
    target_state_log = target_root / "target-state-openocd.json"
    _write_json(
        target_state_log,
        {"argv": ["openocd", "-c", "init; reset run; shutdown"], "returncode": 0},
    )
    source_binding = analyzer._expected_live_source_binding(contract, selector_control)
    target_admission = {
        "schema": 1,
        "evidence_kind": "arm_preserving_contemporaneous_full_bin_uid_admission_v1",
        "status": "passed",
        "purpose": "pre_mailbox_target_image_admission",
        "source_binding": source_binding,
        "source_binding_sha256": canonical_sha256(source_binding),
        "selector_flash_attestation_sha256": selector_binding["sha256"],
        "flash_base_address": analyzer.FLASH_BASE_ADDRESS,
        "byte_count": firmware_path.stat().st_size,
        "expected_bin_sha256": sha256_path(firmware_path),
        "observed_target_sha256": sha256_path(firmware_path),
        "expected_board_id": fixture.board_id,
        "observed_uid": expected_uid,
        "full_bin_and_uid_compared_while_halted": True,
        "exact_bin_and_uid_match": True,
        "reviewed_image_started_only_after_exact_match": True,
        "target_may_have_started_before_failure_halt": False,
        "failure_halt_required": False,
        "failure_halt": None,
        "target_kept_halted_on_failure": False,
        "mailbox_access_performed": False,
        "operation_order": [
            "target_reset_halt",
            "full_firmware_bin_extent_readback",
            "stm32_uid_readback",
            "exact_bytes_and_uid_compare",
            "reset_run_after_exact_match",
        ],
        "target_flash_readback": _binding(target_flash_path),
        "target_uid_readback": _binding(target_uid_path),
        "readback_openocd_log": _binding(target_read_log),
        "target_state_openocd_log": _binding(target_state_log),
        "error": None,
    }
    mute = {
        "status": "passed",
        "serial": fixture.pluto_serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": [-80.0, -80.0],
        "dds_scale_readback": [0.0] * 8,
        "error": None,
    }
    record = {
        "schema": 1,
        "record_kind": "5g8_arm_preserving_condition_record",
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "condition": condition,
        "fixture": contract["fixture"],
        "setup_attestation": contract["setup_attestation"],
        "source": source,
        "live_safety_source_binding": source_binding,
        "live_action_order": [
            "pre_capture_exact_mute",
            "live_usb_identity",
            "target_full_bin_uid_admission",
            "selector_all_off_before",
            "capture",
            "final_acceptance_exact_mute",
            "selector_all_off_after",
            "cleanup_all_off",
        ],
        "identity_preflight": {
            "status": "passed",
            "serial": fixture.pluto_serial,
            "requested_uri": "usb:1.2.3",
            "resolved_uri": "usb:1.2.3",
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
        },
        "initial_mute": {**mute, "purpose": "pre_capture_exact_mute"},
        "target_image_admission": target_admission,
        "selector_all_off_before": _selector_live_evidence(
            live_root, purpose="before_capture", selector_control=selector_control
        ),
        "capture": {
            "artifact_evidence": artifact,
            "stream_id": 202,
            "continuity_ledger": ledger,
            "headroom": json.loads(json.dumps(asdict(headroom))),
            "analysis": analyzer._canonical(asdict(analysis)),
        },
        "final_mute": {**mute, "purpose": "final_acceptance_exact_mute"},
        "selector_all_off_after": _selector_live_evidence(
            live_root, purpose="after_capture", selector_control=selector_control
        ),
        "selector_all_off_cleanup": _selector_live_evidence(
            live_root, purpose="cleanup_all_off", selector_control=selector_control
        ),
    }
    record_path = condition_root / "condition-record.json"
    _write_json(record_path, record)
    observation = domain_helpers._observation(
        fixture_document,
        role="c_i",
        arm="ANT1",
        repeat_index=1,
    )
    observation.update(
        {
            "run_id": contract["run_id"],
            "condition_id": contract["condition_id"],
            "fixture_file": fixture_file,
            "setup_attestation_file": setup_file,
            "selector_flash_attestation_file": selector_binding,
            "source": {
                "smateway_commit": "a" * 40,
                "smateway_files_sha256": source["smateway"]["source_files_sha256"],
                "dependency_commit": "b" * 40,
                "dependency_files_sha256": source["dependency_files_sha256"],
                "native_libiio_attestation_sha256": source["native_libiio_sha256"],
            },
            "artifact": artifact,
            "condition_record_sha256": sha256_path(record_path),
            "leaf_source_sha256s": [artifact["raw_iq_sha256"]],
            "leaf_source_set_sha256": leaf_source_set_sha256((artifact["raw_iq_sha256"],)),
            "transfer": analyzer._recomputed_transfer(analysis),
            "quality": {
                "passed": True,
                "rejection_reasons": [],
                "reference_tone_snr_db": analysis.rx1.tone_to_noise_snr_db,
                "adc_headroom_passed": True,
                "clipped_sample_count_by_receiver": [
                    receiver.clipped_sample_count for receiver in headroom.receivers
                ],
            },
        }
    )
    observation["capture"]["stream_id"] = "202"
    observation_path = condition_root / analyzer.OBSERVATION_FILENAME
    _write_json(observation_path, observation)
    result = {
        "observation_path": str(observation_path),
        "observation_sha256": sha256_path(observation_path),
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "artifact": artifact,
        "accepted_stream_count": 1,
    }
    execution_path = condition_root / "execution-started.tombstone.json"
    execution = {
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_json(execution_path, execution)
    execution_path.chmod(0o400)
    attempt = {
        "status": "complete",
        "error": None,
        "execution_tombstone": {
            "path": str(execution_path),
            "sha256": sha256_path(execution_path),
            "document": execution,
        },
        "result": result,
    }
    manifest = {
        "schema": 1,
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "status": "complete",
        "accepted_stream_count": 1,
        "error": None,
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_path(plan_path),
            "contract_sha256": canonical_sha256(contract),
        },
        "attempts": [attempt],
        "result": result,
    }
    _write_json(condition_root / "manifest.json", manifest)
    return observation_path, fixture, contract


def test_discovery_is_recursive_deterministic_and_exact_filename(tmp_path: Path) -> None:
    first = tmp_path / "b" / analyzer.OBSERVATION_FILENAME
    second = tmp_path / "a" / "nested" / analyzer.OBSERVATION_FILENAME
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    (tmp_path / "a" / "ignored.json").write_text("{}", encoding="utf-8")
    assert analyzer._discover(tmp_path) == sorted((first, second))


def test_discovery_rejects_symlink_anywhere_in_tree(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="symlink"):
        analyzer._discover(tmp_path)


def test_analysis_output_is_create_once_and_complex_is_not_stringified(tmp_path: Path) -> None:
    assert analyzer._json_value(1.5 - 2j) == {"real": 1.5, "imag": -2.0}
    output = tmp_path / "analysis.json"
    analyzer._write_new(output, {"complex": analyzer._json_value(1.5 - 2j)})
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "complex": {"real": 1.5, "imag": -2.0}
    }
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="already exists"):
        analyzer._write_new(output, {"different": True})


def test_cli_requires_one_observation_source_and_an_immutable_output() -> None:
    parser = analyzer._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert {"--fixture", "--observation-root", "--observation", "--output"} <= options
    assert {"--global-h-c", "--observed-e", "--d1-cohort"} <= options
    assert analyzer.REQUIRED_SOURCE_FILES == runner.SOURCE_FILES
    assert {
        "src/smateway/bench.py",
        "src/smateway/profile.py",
        "src/smateway/selector_flash_attestation.py",
    } <= set(analyzer.REQUIRED_SOURCE_FILES)
    help_by_option = {
        option: action.help for action in parser._actions for option in action.option_strings
    }
    assert all(
        str(help_by_option[option]).startswith("DISABLED")
        for option in ("--global-h-c", "--observed-e", "--d1-cohort")
    )


def test_summary_only_full_closure_inputs_are_fail_closed_before_use() -> None:
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="recursive producer"):
        analyzer.analyze(
            fixture_document={},
            observation_paths=[],
            global_h_c_document={"self_declared": "summary"},
        )


def test_native_runtime_is_freshly_reattested_and_exactly_compared(
    tmp_path: Path,
) -> None:
    stored = _native_attestation()
    assert analyzer._reattest_native_runtime(stored, boundary=lambda: dict(stored)) == stored

    changed = json.loads(json.dumps(stored))
    changed["version"]["git_tag"] = "different-valid-runtime-tag"
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="current native libiio"):
        analyzer._reattest_native_runtime(stored, boundary=lambda: changed)

    malformed = dict(stored)
    malformed["library_sha256"] = "0" * 64
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="stored native libiio"):
        analyzer._reattest_native_runtime(malformed, boundary=lambda: stored)

    observation_path, fixture, _ = _accepted_run(tmp_path)
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="current native libiio"):
        analyzer._load_observation(
            observation_path,
            fixture,
            native_boundary=lambda: changed,
        )


def test_complete_run_is_reopened_reaudited_and_recomputed_from_raw_iq(
    tmp_path: Path,
) -> None:
    observation_path, fixture, _ = _accepted_run(tmp_path)

    observed = _load_observation(observation_path, fixture)

    assert observed.run_id == "arm-run-a"
    assert observed.value.detected
    assert observed.stream_id == "202"


@pytest.mark.parametrize("target", ("raw", "metadata", "record"))
def test_raw_metadata_or_condition_record_tamper_is_rejected(
    tmp_path: Path,
    target: str,
) -> None:
    observation_path, fixture, _ = _accepted_run(tmp_path)
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    if target == "raw":
        path = Path(observation["artifact"]["raw_iq_path"])
        path.write_bytes(path.read_bytes() + b"\0\0")
    elif target == "metadata":
        path = Path(observation["artifact"]["metadata_path"])
        path.write_text("{}", encoding="utf-8")
    else:
        path = observation_path.parent / "condition-record.json"
        path.write_text("{}", encoding="utf-8")

    with pytest.raises((analyzer.ArmPreservingAnalysisError, ValueError), match="SHA|hash|record"):
        _load_observation(observation_path, fixture)


def test_source_tamper_and_failure_tombstone_are_rejected(tmp_path: Path) -> None:
    observation_path, fixture, contract = _accepted_run(tmp_path)
    source = contract["source"]["smateway"]
    first = Path(source["repository"]) / source["files"][0]["path"]
    first.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        _load_observation(observation_path, fixture)

    observation_path, fixture, _ = _accepted_run(tmp_path / "failed")
    (observation_path.parent / "failed-run.tombstone.json").write_text("{}", encoding="utf-8")
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="failure tombstone"):
        _load_observation(observation_path, fixture)


def test_true_safety_booleans_cannot_hide_target_readback_tamper(tmp_path: Path) -> None:
    observation_path, fixture, _ = _accepted_run(tmp_path)
    record = json.loads((observation_path.parent / "condition-record.json").read_text())
    target = record["target_image_admission"]
    Path(target["target_flash_readback"]["path"]).write_bytes(b"forged target bytes")

    with pytest.raises(
        (analyzer.ArmPreservingAnalysisError, ValueError), match="target flash|SHA-256"
    ):
        _load_observation(observation_path, fixture)


def test_authoritative_safety_audit_rejects_forged_order_and_source_binding(
    tmp_path: Path,
) -> None:
    observation_path, _, contract = _accepted_run(tmp_path)
    record = json.loads((observation_path.parent / "condition-record.json").read_text())
    record["live_action_order"] = [
        "live_usb_identity",
        "pre_capture_exact_mute",
        *record["live_action_order"][2:],
    ]
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="action order"):
        analyzer._verify_live_safety_record(record, contract=contract, root=observation_path.parent)

    record = json.loads((observation_path.parent / "condition-record.json").read_text())
    record["live_safety_source_binding"]["smateway_commit"] = "f" * 40
    with pytest.raises(analyzer.ArmPreservingAnalysisError, match="source identity"):
        analyzer._verify_live_safety_record(record, contract=contract, root=observation_path.parent)
