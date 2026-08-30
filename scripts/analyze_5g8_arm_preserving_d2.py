#!/usr/bin/env python3
"""Aggregate exact arm-preserving C_i/D2_i cohorts with closure fail-closed."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np

_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE = _REPOSITORY / "src"
_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned analysis Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SOURCE) if not prior_pythonpath else f"{_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    loader_entries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(_REQUIRED_LIBIIO_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from smateway.arm_preserving_d2 import (  # noqa: E402
    BANDWIDTH_HZ,
    CENTER_FREQUENCY_HZ,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TONE_OFFSET_HZ,
    TOTAL_SAMPLES,
    ArmPreservingD2Error,
    build_c_d2_fragment,
    canonical_sha256,
    fragment_document,
    validate_fixture_v2,
    validate_observation,
    validate_setup_attestation,
)
from smateway.bench import BenchManifest, decode_mailbox  # noqa: E402
from smateway.capture_admission import AdcHeadroomMonitor  # noqa: E402
from smateway.file_artifact_admission import (  # noqa: E402
    admit_dual_rx_ci16_artifact,
    assert_local_rpi_storage,
    read_json_file,
    verify_source_tree_binding,
)
from smateway.hexcal import sha256_path  # noqa: E402
from smateway.leakage_ladder import analyze_coherent_leakage  # noqa: E402
from smateway.native_iio_attestation import (  # noqa: E402
    attest_runtime,
    attestation_sha256,
    validate_runtime_attestation,
)
from smateway.selector_flash_attestation import (  # noqa: E402
    FLASH_BASE_ADDRESS,
    GPIOA_ODR_ADDRESS,
    SELECTOR_GPIO_MASK,
    STM32C011_UID_SIZE_BYTES,
)

OUTPUT_KIND = "smateway.5g8.arm-preserving-analysis/v1"
OBSERVATION_FILENAME = "normalized-observation.json"
REQUIRED_SOURCE_FILES = (
    "src/smateway/arm_preserving_d2.py",
    "src/smateway/bench.py",
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/closure_qualification.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/hexcal.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/profile.py",
    "src/smateway/selector_flash_attestation.py",
    "scripts/run_5g8_arm_preserving_d2.py",
    "scripts/analyze_5g8_arm_preserving_d2.py",
)


class ArmPreservingAnalysisError(RuntimeError):
    """Input artifacts cannot support a source-distinct arm-preserving result."""


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ArmPreservingAnalysisError(f"{label} contains a symlink: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise ArmPreservingAnalysisError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArmPreservingAnalysisError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ArmPreservingAnalysisError(f"{label} must contain one JSON object")
    return value


def _verify_bound_file(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ArmPreservingAnalysisError(f"{label} binding must be an object")
    path = Path(str(value.get("path")))
    _assert_no_symlink_chain(path, label)
    if path.is_symlink() or not path.is_file():
        raise ArmPreservingAnalysisError(f"{label} does not name a regular file")
    if value.get("sha256") != sha256_path(path) or value.get("size_bytes") != path.stat().st_size:
        raise ArmPreservingAnalysisError(f"{label} path/hash/size binding is stale")


def _verify_observation_files(document: Mapping[str, Any]) -> None:
    for name in (
        "fixture_file",
        "setup_attestation_file",
        "selector_flash_attestation_file",
    ):
        _verify_bound_file(document.get(name), name)
    artifact = document.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ArmPreservingAnalysisError("observation artifact binding is missing")
    raw_path = Path(str(artifact.get("raw_iq_path")))
    metadata_path = Path(str(artifact.get("metadata_path")))
    for path, field, label in (
        (raw_path, "raw_iq_sha256", "raw IQ"),
        (metadata_path, "metadata_sha256", "SigMF metadata"),
    ):
        _assert_no_symlink_chain(path, label)
        if path.is_symlink() or not path.is_file() or sha256_path(path) != artifact.get(field):
            raise ArmPreservingAnalysisError(f"{label} file/hash binding is stale")


def _verify_selector_control(value: object) -> dict[str, Any]:
    control = value if isinstance(value, Mapping) else None
    if (
        control is None
        or control.get("schema") != 1
        or control.get("control_kind") != "sealed_bench_static_all_off"
        or control.get("required_lease_ms") != 0
        or control.get("gpioa_odr_address") != GPIOA_ODR_ADDRESS
        or control.get("selector_gpio_mask") != SELECTOR_GPIO_MASK
        or control.get("live_raw_mailbox_and_gpio_readback_required") is not True
    ):
        raise ArmPreservingAnalysisError("arm-preserving selector control is malformed")
    for name in ("selector_flash_attestation", "build_manifest", "openocd_config"):
        _verify_bound_file(control.get(name), f"selector control {name}")
    target = control.get("target_image_admission")
    if not isinstance(target, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving target-image contract is missing")
    firmware = target.get("firmware_bin")
    selector_flash = control.get("selector_flash_attestation")
    if not isinstance(firmware, Mapping) or not isinstance(selector_flash, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving target-image binding is malformed")
    _verify_bound_file(firmware, "selector target-image firmware BIN")
    board_id = target.get("board_id")
    expected_uid = board_id.removeprefix("stm32c011-") if isinstance(board_id, str) else None
    if (
        set(target)
        != {
            "schema",
            "flash_base_address",
            "firmware_bin",
            "board_id",
            "expected_uid",
            "selector_flash_attestation_sha256",
            "full_bin_extent_and_uid_required_before_mailbox",
            "mismatch_must_remain_halted",
        }
        or target.get("schema") != 1
        or target.get("flash_base_address") != FLASH_BASE_ADDRESS
        or not isinstance(expected_uid, str)
        or len(expected_uid) != 24
        or any(character not in "0123456789abcdef" for character in expected_uid)
        or target.get("expected_uid") != expected_uid
        or target.get("selector_flash_attestation_sha256") != selector_flash.get("sha256")
        or target.get("full_bin_extent_and_uid_required_before_mailbox") is not True
        or target.get("mismatch_must_remain_halted") is not True
    ):
        raise ArmPreservingAnalysisError("arm-preserving target-image contract differs")
    manifest = BenchManifest.load(Path(str(control["build_manifest"]["path"])))
    expected_mailbox = {
        "address": manifest.address,
        "size": manifest.size,
        "magic": manifest.magic,
        "version": manifest.version,
        "max_lease_ms": manifest.max_lease_ms,
        "offsets": manifest.offsets,
    }
    code = control.get("all_off_code")
    if (
        control.get("mailbox") != expected_mailbox
        or isinstance(code, bool)
        or not isinstance(code, int)
        or not 0 <= code <= 15
    ):
        raise ArmPreservingAnalysisError("arm-preserving selector mailbox/code differs")
    return dict(control)


def _reattest_native_runtime(
    stored: object,
    *,
    boundary: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare a fresh in-process native libiio identity to the stored exact document."""

    try:
        frozen_stored = validate_runtime_attestation(stored)
    except (TypeError, ValueError) as error:
        raise ArmPreservingAnalysisError("stored native libiio identity is invalid") from error
    try:
        fresh = validate_runtime_attestation(boundary())
    except Exception as error:
        raise ArmPreservingAnalysisError(
            f"current native libiio runtime could not be re-attested: {error}"
        ) from error
    if fresh != frozen_stored or attestation_sha256(fresh) != attestation_sha256(frozen_stored):
        raise ArmPreservingAnalysisError(
            "current native libiio runtime differs from the stored source identity"
        )
    return fresh


def _expected_live_source_binding(
    contract: Mapping[str, Any], control: Mapping[str, Any]
) -> dict[str, Any]:
    source = contract.get("source")
    smateway = source.get("smateway") if isinstance(source, Mapping) else None
    dependency = source.get("pluto_plus_utils") if isinstance(source, Mapping) else None
    native = source.get("native_libiio") if isinstance(source, Mapping) else None
    selector = control.get("selector_flash_attestation")
    if not all(
        isinstance(item, Mapping) for item in (source, smateway, dependency, native, selector)
    ):
        raise ArmPreservingAnalysisError("live safety source binding is malformed")
    assert isinstance(source, Mapping)
    assert isinstance(smateway, Mapping)
    assert isinstance(dependency, Mapping)
    assert isinstance(selector, Mapping)
    return {
        "schema": 1,
        "evidence_kind": "arm_preserving_live_safety_source_binding_v1",
        "plan_source_sha256": canonical_sha256(source),
        "smateway_commit": smateway.get("commit"),
        "smateway_files_sha256": smateway.get("source_files_sha256"),
        "dependency_commit": dependency.get("commit"),
        "dependency_files_sha256": source.get("dependency_files_sha256"),
        "native_libiio_attestation_sha256": source.get("native_libiio_sha256"),
        "selector_flash_attestation_sha256": selector.get("sha256"),
    }


def _verify_live_bound_file(value: object, *, label: str, root: Path) -> Path:
    _verify_bound_file(value, label)
    assert isinstance(value, Mapping)
    path = Path(str(value["path"])).expanduser().absolute()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ArmPreservingAnalysisError(
            f"{label} is outside selector live-evidence root"
        ) from error
    assert_local_rpi_storage(path, label=label)
    return path


def _mute_evidence_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "passed"
        and value.get("purpose") == purpose
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("tx_gain_readback_db_by_channel") == [-80.0, -80.0]
        and value.get("dds_scale_readback") == [0.0] * 8
        and value.get("error") is None
    )


def _verify_target_image_admission(
    value: object,
    *,
    control: Mapping[str, Any],
    source_binding: Mapping[str, Any],
    evidence_root: Path,
) -> None:
    if not isinstance(value, Mapping):
        raise ArmPreservingAnalysisError("target-image admission evidence is missing")
    target = control.get("target_image_admission")
    if not isinstance(target, Mapping) or not isinstance(target.get("firmware_bin"), Mapping):
        raise ArmPreservingAnalysisError("target-image plan contract is malformed")
    target_file = _verify_live_bound_file(
        value.get("target_flash_readback"),
        label="target flash readback",
        root=evidence_root,
    )
    uid_file = _verify_live_bound_file(
        value.get("target_uid_readback"),
        label="target UID readback",
        root=evidence_root,
    )
    read_log_path = _verify_live_bound_file(
        value.get("readback_openocd_log"),
        label="target readback OpenOCD log",
        root=evidence_root,
    )
    state_log_path = _verify_live_bound_file(
        value.get("target_state_openocd_log"),
        label="target-state OpenOCD log",
        root=evidence_root,
    )
    firmware = target["firmware_bin"]
    assert isinstance(firmware, Mapping)
    expected_path = Path(str(firmware["path"]))
    expected_sha256 = str(firmware["sha256"])
    expected_size = int(firmware["size_bytes"])
    expected_uid = str(target["expected_uid"])
    read_log = _read_json(read_log_path, "target readback OpenOCD log")
    state_log = _read_json(state_log_path, "target-state OpenOCD log")
    if (
        value.get("schema") != 1
        or value.get("evidence_kind") != "arm_preserving_contemporaneous_full_bin_uid_admission_v1"
        or value.get("status") != "passed"
        or value.get("purpose") != "pre_mailbox_target_image_admission"
        or value.get("source_binding") != dict(source_binding)
        or value.get("source_binding_sha256") != canonical_sha256(source_binding)
        or value.get("selector_flash_attestation_sha256")
        != target.get("selector_flash_attestation_sha256")
        or value.get("flash_base_address") != FLASH_BASE_ADDRESS
        or value.get("byte_count") != expected_size
        or value.get("expected_bin_sha256") != expected_sha256
        or value.get("observed_target_sha256") != expected_sha256
        or value.get("expected_board_id") != target.get("board_id")
        or value.get("observed_uid") != expected_uid
        or value.get("full_bin_and_uid_compared_while_halted") is not True
        or value.get("exact_bin_and_uid_match") is not True
        or value.get("reviewed_image_started_only_after_exact_match") is not True
        or value.get("target_may_have_started_before_failure_halt") is not False
        or value.get("failure_halt_required") is not False
        or value.get("failure_halt") is not None
        or value.get("target_kept_halted_on_failure") is not False
        or value.get("mailbox_access_performed") is not False
        or value.get("operation_order")
        != [
            "target_reset_halt",
            "full_firmware_bin_extent_readback",
            "stm32_uid_readback",
            "exact_bytes_and_uid_compare",
            "reset_run_after_exact_match",
        ]
        or target_file.stat().st_size != expected_size
        or sha256_path(target_file) != expected_sha256
        or target_file.read_bytes() != expected_path.read_bytes()
        or uid_file.stat().st_size != STM32C011_UID_SIZE_BYTES
        or uid_file.read_bytes().hex() != expected_uid
        or read_log.get("returncode") != 0
        or state_log.get("returncode") != 0
        or state_log.get("argv", [])[-1] != "init; reset run; shutdown"
        or value.get("error") is not None
    ):
        raise ArmPreservingAnalysisError(
            "target-image admission is not exact, source-bound, and pre-mailbox"
        )


def _verify_selector_all_off_evidence(
    value: object,
    *,
    control: Mapping[str, Any],
    purpose: str,
    evidence_root: Path,
) -> None:
    if not isinstance(value, Mapping):
        raise ArmPreservingAnalysisError(f"selector {purpose} evidence is missing")
    mailbox_path = _verify_live_bound_file(
        value.get("mailbox_readback"),
        label=f"selector {purpose} mailbox readback",
        root=evidence_root,
    )
    gpio_path = _verify_live_bound_file(
        value.get("gpioa_odr_readback"),
        label=f"selector {purpose} GPIO readback",
        root=evidence_root,
    )
    log_path = _verify_live_bound_file(
        value.get("openocd_log"),
        label=f"selector {purpose} OpenOCD log",
        root=evidence_root,
    )
    manifest = BenchManifest.load(Path(str(control["build_manifest"]["path"])))
    mailbox = decode_mailbox(mailbox_path.read_bytes(), manifest)
    gpio_bytes = gpio_path.read_bytes()
    log = _read_json(log_path, f"selector {purpose} OpenOCD log")
    if len(gpio_bytes) != 4:
        raise ArmPreservingAnalysisError(f"selector {purpose} GPIO readback size differs")
    gpio = int.from_bytes(gpio_bytes, "little")
    code = int(control["all_off_code"])
    if (
        value.get("status") != "passed"
        or value.get("purpose") != purpose
        or value.get("control_sha256") != canonical_sha256(control)
        or value.get("all_off_code") != code
        or value.get("lease_ms") != 0
        or value.get("mailbox") != mailbox.as_dict()
        or mailbox.command_sequence != mailbox.acknowledged_sequence
        or mailbox.command_code != code
        or mailbox.applied_code != code
        or mailbox.command_lease_ms != 0
        or mailbox.remaining_lease_ms != 0
        or not mailbox.command_valid
        or mailbox.lease_active
        or mailbox.guard_active
        or mailbox.invalid_command
        or value.get("gpioa_odr_raw_value") != gpio
        or value.get("gpioa_odr_masked_selector_code") != gpio & SELECTOR_GPIO_MASK
        or gpio & SELECTOR_GPIO_MASK != code
        or value.get("command_valid") is not True
        or value.get("raw_mailbox_and_gpio_readback_passed") is not True
        or log.get("returncode") != 0
        or value.get("error") is not None
    ):
        raise ArmPreservingAnalysisError(f"selector {purpose} is not raw lease-free ALL_OFF")


def _verify_live_safety_record(
    record: Mapping[str, Any], *, contract: Mapping[str, Any], root: Path
) -> None:
    control = _verify_selector_control(contract.get("selector_control"))
    source_binding = _expected_live_source_binding(contract, control)
    if record.get("live_safety_source_binding") != source_binding:
        raise ArmPreservingAnalysisError("live safety evidence differs from plan source identity")
    expected_order = [
        "pre_capture_exact_mute",
        "live_usb_identity",
        "target_full_bin_uid_admission",
        "selector_all_off_before",
        "capture",
        "final_acceptance_exact_mute",
        "selector_all_off_after",
        "cleanup_all_off",
    ]
    if record.get("live_action_order") != expected_order:
        raise ArmPreservingAnalysisError(
            "live action order does not prove mute/image-before-mailbox"
        )
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ArmPreservingAnalysisError("live safety configuration is malformed")
    serial = str(configuration.get("serial"))
    uri = str(configuration.get("uri"))
    identity = record.get("identity_preflight")
    if (
        not isinstance(identity, Mapping)
        or identity.get("status") != "passed"
        or identity.get("serial") != serial
        or identity.get("requested_uri") != uri
        or identity.get("resolved_uri") != uri
        or identity.get("exact_uri_match") is not True
        or identity.get("scan_mutates_radio_state") is not False
    ):
        raise ArmPreservingAnalysisError("live USB identity evidence differs from plan")
    if not _mute_evidence_passed(
        record.get("initial_mute"), serial=serial, purpose="pre_capture_exact_mute"
    ) or not _mute_evidence_passed(
        record.get("final_mute"), serial=serial, purpose="final_acceptance_exact_mute"
    ):
        raise ArmPreservingAnalysisError("exact initial/final Pluto mute evidence is incomplete")
    evidence_root = root / "selector-live-evidence"
    _assert_no_symlink_chain(evidence_root, "selector live-evidence root")
    assert_local_rpi_storage(evidence_root, label="selector live-evidence root")
    _verify_target_image_admission(
        record.get("target_image_admission"),
        control=control,
        source_binding=source_binding,
        evidence_root=evidence_root,
    )
    for field, purpose in (
        ("selector_all_off_before", "before_capture"),
        ("selector_all_off_after", "after_capture"),
        ("selector_all_off_cleanup", "cleanup_all_off"),
    ):
        _verify_selector_all_off_evidence(
            record.get(field),
            control=control,
            purpose=purpose,
            evidence_root=evidence_root,
        )


def _load_observation(
    path: Path,
    fixture: Any,
    *,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> Any:
    document = _read_json(path, "normalized observation")
    _verify_observation_files(document)
    _reverify_complete_run(path, document, fixture, native_boundary=native_boundary)
    record_path = path.parent / "condition-record.json"
    if (
        record_path.is_symlink()
        or not record_path.is_file()
        or sha256_path(record_path) != document.get("condition_record_sha256")
    ):
        raise ArmPreservingAnalysisError(
            "normalized observation lacks its exact condition-record sibling"
        )
    return validate_observation(document, fixture=fixture)


def _canonical(value: object) -> Any:
    """Normalize dataclasses/NumPy values exactly as the capture runner did."""

    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, default=str))


def _recomputed_transfer(analysis: Any) -> dict[str, Any]:
    transfer = analysis.rx2_over_rx1
    if transfer.phasor is not None and analysis.rx2.tone_detected:
        return {
            "detected": True,
            "phasor": {
                "real": float(transfer.phasor.real),
                "imag": float(transfer.phasor.imag),
            },
            "magnitude_upper_bound": None,
        }
    upper = transfer.amplitude_upper_bound_ratio
    if upper is None:
        raise ArmPreservingAnalysisError("raw-IQ nondetection lacks a phase-free upper bound")
    return {"detected": False, "phasor": None, "magnitude_upper_bound": float(upper)}


def _reverify_complete_run(
    path: Path,
    observation: Mapping[str, Any],
    fixture: Any,
    *,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> None:
    """Re-admit the immutable run, raw bytes, ABI2 ledger, and complex estimate."""

    root = path.expanduser().absolute().parent
    plan_path = root / "plan.json"
    manifest_path = root / "manifest.json"
    execution_path = root / "execution-started.tombstone.json"
    failure_path = root / "failed-run.tombstone.json"
    if failure_path.exists() or failure_path.is_symlink():
        raise ArmPreservingAnalysisError("arm-preserving run has a failure tombstone")
    plan = read_json_file(plan_path, label="arm-preserving immutable plan")
    manifest = read_json_file(manifest_path, label="arm-preserving manifest")
    execution = read_json_file(execution_path, label="arm-preserving execution tombstone")
    if plan_path.stat().st_mode & stat.S_IWUSR or execution_path.stat().st_mode & stat.S_IWUSR:
        raise ArmPreservingAnalysisError("arm-preserving plan/tombstone is not immutable")
    contract = plan.get("plan_contract")
    if not isinstance(contract, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving plan contract is missing")
    contract_sha = canonical_sha256(contract)
    if (
        plan.get("schema") != 1
        or plan.get("immutable") is not True
        or plan.get("plan_contract_sha256") != contract_sha
        or contract.get("run_kind") != "5g8_arm_preserving_c_i_or_d2_i_one_stream"
        or contract.get("run_id") != observation.get("run_id")
        or contract.get("condition_id") != observation.get("condition_id")
        or not isinstance(contract.get("fixture"), Mapping)
        or contract["fixture"].get("document") != fixture.document
    ):
        raise ArmPreservingAnalysisError("arm-preserving immutable plan is inconsistent")
    fixture_contract = contract["fixture"]
    assert isinstance(fixture_contract, Mapping)
    setup_contract = contract.get("setup_attestation")
    condition_contract = contract.get("condition")
    if not isinstance(setup_contract, Mapping) or not isinstance(condition_contract, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving fixture/setup contract is malformed")
    fixture_file = fixture_contract.get("file")
    if not isinstance(fixture_file, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving fixture file binding is malformed")
    _verify_bound_file(fixture_file, "arm-preserving fixture")
    _verify_bound_file(setup_contract.get("file"), "arm-preserving setup attestation")
    if observation.get("fixture_file") != fixture_contract.get("file") or observation.get(
        "setup_attestation_file"
    ) != setup_contract.get("file"):
        raise ArmPreservingAnalysisError("normalized fixture/setup files differ from the plan")
    validated_setup = validate_setup_attestation(
        setup_contract.get("document"),
        fixture=fixture,
        fixture_file_sha256=str(fixture_file.get("sha256")),
        run_id=str(contract.get("run_id")),
        role=str(condition_contract.get("role")),
        arm=str(condition_contract.get("arm")),
        repeat_index=int(condition_contract.get("repeat_index", 0)),
    )
    if validated_setup != setup_contract.get("document"):
        raise ArmPreservingAnalysisError("arm-preserving setup attestation differs from plan")
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise ArmPreservingAnalysisError("arm-preserving source contract is missing")
    smateway_source = source.get("smateway")
    dependency_source = source.get("pluto_plus_utils")
    native_source = source.get("native_libiio")
    if not all(
        isinstance(item, Mapping) for item in (smateway_source, dependency_source, native_source)
    ):
        raise ArmPreservingAnalysisError("arm-preserving source identity is malformed")
    assert isinstance(smateway_source, Mapping)
    assert isinstance(dependency_source, Mapping)
    assert isinstance(native_source, Mapping)
    smateway_files = smateway_source.get("files")
    dependency_files = dependency_source.get("files")
    if (
        not isinstance(smateway_files, list)
        or not smateway_files
        or not isinstance(dependency_files, list)
        or not dependency_files
        or smateway_source.get("source_files_sha256") != canonical_sha256(smateway_files)
        or source.get("dependency_files_sha256") != canonical_sha256(dependency_files)
        or source.get("native_libiio_sha256")
        != attestation_sha256(validate_runtime_attestation(native_source))
    ):
        raise ArmPreservingAnalysisError("arm-preserving source hash identity is inconsistent")
    _reattest_native_runtime(native_source, boundary=native_boundary)
    verify_source_tree_binding(
        smateway_source,
        label="arm-preserving Smateway",
        required_relative_paths=REQUIRED_SOURCE_FILES,
    )
    verify_source_tree_binding(dependency_source, label="arm-preserving pluto-plus-utils")
    normalized_source = observation.get("source")
    if (
        not isinstance(normalized_source, Mapping)
        or normalized_source.get("smateway_commit") != smateway_source.get("commit")
        or normalized_source.get("smateway_files_sha256")
        != smateway_source.get("source_files_sha256")
        or normalized_source.get("dependency_commit") != dependency_source.get("commit")
        or normalized_source.get("dependency_files_sha256") != source.get("dependency_files_sha256")
        or normalized_source.get("native_libiio_attestation_sha256")
        != source.get("native_libiio_sha256")
    ):
        raise ArmPreservingAnalysisError("normalized source identity differs from the plan")
    storage = contract.get("storage")
    if (
        not isinstance(storage, Mapping)
        or storage.get("local_rpi_only") is not True
        or storage.get("pluto_storage_forbidden") is not True
        or storage.get("condition_root") != str(root)
        or not isinstance(storage.get("capture_root"), str)
        or not Path(str(storage["capture_root"])).is_absolute()
    ):
        raise ArmPreservingAnalysisError("arm-preserving local-storage contract is malformed")
    assert_local_rpi_storage(root, label="arm-preserving condition storage")
    capture_root = assert_local_rpi_storage(
        Path(str(storage["capture_root"])), label="arm-preserving capture storage"
    )
    _verify_selector_control(contract.get("selector_control"))
    plan_binding = manifest.get("plan")
    attempts = manifest.get("attempts")
    result = manifest.get("result")
    if (
        not isinstance(plan_binding, Mapping)
        or plan_binding.get("path") != str(plan_path)
        or plan_binding.get("sha256") != sha256_path(plan_path)
        or plan_binding.get("contract_sha256") != contract_sha
        or manifest.get("schema") != 1
        or manifest.get("run_id") != contract.get("run_id")
        or manifest.get("condition_id") != contract.get("condition_id")
        or manifest.get("status") != "complete"
        or manifest.get("accepted_stream_count") != 1
        or manifest.get("error") is not None
        or not isinstance(attempts, list)
        or len(attempts) != 1
        or not isinstance(attempts[0], Mapping)
        or attempts[0].get("status") != "complete"
        or attempts[0].get("error") is not None
        or not isinstance(result, Mapping)
        or attempts[0].get("result") != result
    ):
        raise ArmPreservingAnalysisError("arm-preserving manifest is not one accepted stream")
    tombstone_binding = attempts[0].get("execution_tombstone")
    if (
        not isinstance(tombstone_binding, Mapping)
        or tombstone_binding.get("path") != str(execution_path)
        or tombstone_binding.get("sha256") != sha256_path(execution_path)
        or tombstone_binding.get("document") != execution
        or execution.get("run_id") != contract.get("run_id")
        or execution.get("condition_id") != contract.get("condition_id")
        or execution.get("plan_path") != str(plan_path)
        or execution.get("plan_sha256") != sha256_path(plan_path)
        or execution.get("plan_contract_sha256") != contract_sha
        or execution.get("run_id_burned") is not True
        or execution.get("automatic_retry_forbidden") is not True
    ):
        raise ArmPreservingAnalysisError("arm-preserving execution tombstone is inconsistent")
    record_path = root / "condition-record.json"
    record = read_json_file(record_path, label="arm-preserving condition record")
    if (
        result.get("observation_path") != str(path.expanduser().absolute())
        or result.get("observation_sha256") != sha256_path(path.expanduser().absolute())
        or result.get("condition_record_path") != str(record_path)
        or result.get("condition_record_sha256") != sha256_path(record_path)
        or observation.get("condition_record_sha256") != sha256_path(record_path)
        or record.get("plan_contract_sha256") != contract_sha
        or record.get("condition") != contract.get("condition")
        or record.get("fixture") != contract.get("fixture")
        or record.get("setup_attestation") != contract.get("setup_attestation")
        or record.get("source") != contract.get("source")
    ):
        raise ArmPreservingAnalysisError("condition record does not bind the immutable run")
    _verify_live_safety_record(record, contract=contract, root=root)
    artifact = observation.get("artifact")
    if not isinstance(artifact, Mapping) or result.get("artifact") != artifact:
        raise ArmPreservingAnalysisError("manifest/observation artifact bindings differ")
    capture = record.get("capture")
    if not isinstance(capture, Mapping) or capture.get("artifact_evidence") != artifact:
        raise ArmPreservingAnalysisError("condition record artifact binding differs")
    samples, continuity, raw_path, metadata_path = admit_dual_rx_ci16_artifact(
        artifact,
        label="arm-preserving artifact",
        expected_sample_count=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        expected_stream_id=observation.get("capture", {}).get("stream_id")
        if isinstance(observation.get("capture"), Mapping)
        else None,
        expected_artifact_id=str(artifact.get("artifact_id")),
    )
    expected_artifact_root = capture_root / str(artifact.get("artifact_id"))
    if raw_path.parent != expected_artifact_root or metadata_path.parent != expected_artifact_root:
        raise ArmPreservingAnalysisError("arm-preserving artifact is outside its capture root")
    metadata = read_json_file(metadata_path, label="arm-preserving SigMF metadata")
    global_section = metadata.get("global")
    captures = metadata.get("captures")
    configuration = contract.get("configuration")
    expected_settings = {
        "bandwidth_hz": float(BANDWIDTH_HZ),
        "center_frequency_hz": float(CENTER_FREQUENCY_HZ),
        "channels": [0, 1],
        "gain_db": float(RECEIVER_GAIN_DB),
        "gain_mode": "manual",
        "sample_rate_hz": float(SAMPLE_RATE_HZ),
    }
    if (
        not isinstance(global_section, Mapping)
        or not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
        or not isinstance(configuration, Mapping)
        or global_section.get("core:sample_rate") != float(SAMPLE_RATE_HZ)
        or not isinstance(global_section.get("pluto:radio"), Mapping)
        or global_section["pluto:radio"].get("serial") != configuration.get("serial")
        or global_section["pluto:radio"].get("uri") != configuration.get("uri")
        or captures[0].get("settings") != expected_settings
    ):
        raise ArmPreservingAnalysisError("arm-preserving SigMF radio/settings differ from plan")
    persisted_ledger = metadata.get("pluto:continuity")
    if persisted_ledger is None:
        persisted_ledger = (
            global_section.get("pluto:continuity") if isinstance(global_section, Mapping) else None
        )
    if (
        capture.get("stream_id") != continuity.get("stream_id")
        or capture.get("continuity_ledger") != persisted_ledger
    ):
        raise ArmPreservingAnalysisError("live and persisted ABI2 ledgers differ")
    monitor = AdcHeadroomMonitor(receiver_count=2)
    monitor.observe(samples)
    headroom = monitor.result()
    if _canonical(asdict(headroom)) != capture.get("headroom"):
        raise ArmPreservingAnalysisError("stored ADC-headroom result differs from raw IQ")
    analysis = analyze_coherent_leakage(
        samples[0],
        samples[1],
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        block_duration_s=0.1,
        minimum_block_count=3,
    )
    del samples
    if _canonical(asdict(analysis)) != capture.get("analysis"):
        raise ArmPreservingAnalysisError("stored coherent analysis differs from raw IQ")
    if not analysis.quality_passed or analysis.rx1.tone_to_noise_snr_db < 20.0:
        raise ArmPreservingAnalysisError("recomputed raw-IQ quality does not pass")
    if observation.get("transfer") != _recomputed_transfer(analysis):
        raise ArmPreservingAnalysisError("normalized transfer differs from raw-IQ recomputation")
    quality = observation.get("quality")
    clipped = [receiver.clipped_sample_count for receiver in headroom.receivers]
    if (
        not isinstance(quality, Mapping)
        or quality.get("reference_tone_snr_db") != analysis.rx1.tone_to_noise_snr_db
        or quality.get("clipped_sample_count_by_receiver") != clipped
    ):
        raise ArmPreservingAnalysisError("normalized quality differs from raw-IQ recomputation")


def _discover(root: Path) -> list[Path]:
    exact = root.expanduser().absolute()
    _assert_no_symlink_chain(exact, "observation root")
    if exact.is_symlink() or not exact.is_dir():
        raise ArmPreservingAnalysisError("observation root must be a real directory")
    results: list[Path] = []
    for directory, directory_names, file_names in os.walk(exact, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            if (directory_path / name).is_symlink():
                raise ArmPreservingAnalysisError("observation tree contains a symlink")
        if OBSERVATION_FILENAME in file_names:
            candidate = directory_path / OBSERVATION_FILENAME
            if candidate.is_symlink():
                raise ArmPreservingAnalysisError("observation tree contains a symlink")
            results.append(candidate)
    return sorted(results)


def _json_value(value: object) -> Any:
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, np.ndarray):
        return [_json_value(item) for item in value.tolist()]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    exact = path.expanduser().absolute()
    assert_local_rpi_storage(exact, label="arm-preserving analysis output")
    _assert_no_symlink_chain(exact.parent, "output parent")
    exact.parent.mkdir(parents=True, exist_ok=True)
    if exact.exists() or exact.is_symlink():
        raise ArmPreservingAnalysisError("analysis output already exists")
    wire = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(exact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(wire)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if exact.exists():
            exact.unlink()
        raise


def analyze(
    *,
    fixture_document: Mapping[str, Any],
    observation_paths: Sequence[Path],
    global_h_c_document: Mapping[str, Any] | None = None,
    observed_e_document: Mapping[str, Any] | None = None,
    d1_documents: Mapping[str, Mapping[str, Any]] | None = None,
    bootstrap_draws: int = 32_768,
    bootstrap_seed: int = 0x5A8C10,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> dict[str, Any]:
    """Build the exact T4 fragment; unaudited summary-only closure is fail-closed."""

    optional_supplied = (
        global_h_c_document is not None,
        observed_e_document is not None,
        d1_documents is not None,
    )
    if any(optional_supplied):
        raise ArmPreservingAnalysisError(
            "full closure qualification is disabled: global H_C, observed E, and D1 summary "
            "JSON lack recursive producer plan/manifest/condition-record/metadata/raw-IQ "
            "admission"
        )

    fixture = validate_fixture_v2(fixture_document)
    paths = tuple(path.expanduser().absolute() for path in observation_paths)
    if len(set(paths)) != len(paths):
        raise ArmPreservingAnalysisError("observation path list contains duplicates")
    observations = [
        _load_observation(path, fixture, native_boundary=native_boundary) for path in paths
    ]
    source_identities = {
        canonical_sha256(observation.document.get("source")) for observation in observations
    }
    if len(source_identities) != 1:
        raise ArmPreservingAnalysisError(
            "arm-preserving observations mix source/dependency/native identities"
        )
    fragment = build_c_d2_fragment(observations, fixture=fixture)
    serialized_fragment = fragment_document(fragment)
    return {
        "schema": 1,
        "document_kind": OUTPUT_KIND,
        "fixture_sha256": fixture.fixture_sha256,
        "plan_sha256": fixture.plan_identity.sha256,
        "accepted_observation_count": 80,
        "observation_paths": [str(path) for path in paths],
        "fragment": serialized_fragment,
        "qualification": None,
        "full_closure_qualification_status": (
            "disabled_until_recursive_upstream_raw_provenance_admission_is_implemented"
        ),
        "topology_limitation": fixture.document["topology_limitation"],
        "closure_claim_permitted": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--observation-root", type=Path)
    source.add_argument("--observation", type=Path, action="append")
    parser.add_argument(
        "--global-h-c",
        type=Path,
        help="DISABLED: summary JSON is not authoritative upstream evidence",
    )
    parser.add_argument(
        "--observed-e",
        type=Path,
        help="DISABLED: summary JSON is not authoritative upstream evidence",
    )
    parser.add_argument(
        "--d1-cohort",
        action="append",
        default=[],
        metavar="ANTn=/absolute/path.json",
        help="DISABLED: D1 summary JSON is not recursively admitted",
    )
    parser.add_argument("--bootstrap-draws", type=int, default=32_768)
    parser.add_argument("--bootstrap-seed", type=int, default=0x5A8C10)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        fixture_document = _read_json(args.fixture, "fixture")
        paths = (
            _discover(args.observation_root)
            if args.observation_root is not None
            else list(args.observation or [])
        )
        optional = (args.global_h_c, args.observed_e, bool(args.d1_cohort))
        if any(item is not None and item is not False for item in optional):
            raise ArmPreservingAnalysisError(
                "full closure qualification is disabled until every upstream producer's "
                "plan, manifest, condition record, metadata, and raw IQ are recursively admitted"
            )
        result = analyze(
            fixture_document=fixture_document,
            observation_paths=paths,
            global_h_c_document=None,
            observed_e_document=None,
            d1_documents=None,
            bootstrap_draws=args.bootstrap_draws,
            bootstrap_seed=args.bootstrap_seed,
        )
        _write_new(args.output, result)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "output": str(args.output.expanduser().absolute()),
                    "accepted_observation_count": 80,
                    "closure_claim_permitted": False,
                }
            )
        )
        return 0
    except (OSError, ValueError, ArmPreservingD2Error, ArmPreservingAnalysisError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
