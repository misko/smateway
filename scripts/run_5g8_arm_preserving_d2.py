#!/usr/bin/env python3
"""Plan or capture one source-distinct 5.8-GHz arm-preserving C_i/D2_i repeat."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SMATEWAY_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SMATEWAY_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned capture Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
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

import numpy as np
from pluto_plus.artifacts import (
    CaptureWriter,
    complex_to_ci16,
    data_path,
    verify_artifact,
)
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import SafeDdsTonePlan, SampleBlockV2, capture_continuous_safe_dds_tone
from pluto_plus.hardware.iio import find_usb_sysfs_path, resolve_iio_uri
from pluto_plus.models import GainMode, RadioSettings

from smateway.arm_preserving_d2 import (
    BANDWIDTH_HZ,
    CENTER_FREQUENCY_HZ,
    DDS_SCALE,
    FRAME_COUNT,
    KERNEL_BUFFERS,
    MINIMUM_REFERENCE_SNR_DB,
    OBSERVATION_KIND,
    RECEIVER_GAIN_DB,
    ROLES,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TONE_OFFSET_HZ,
    TOTAL_SAMPLES,
    TX_HARDWARE_GAIN_DB,
    ArmPreservingD2Error,
    ValidatedArmPreservingFixture,
    canonical_sha256,
    complex_detection_document,
    validate_fixture_v2,
    validate_observation,
    validate_setup_attestation,
)
from smateway.bench import BenchManifest, OpenOcdBench, decode_mailbox
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.capture_continuity import validate_continuity_ledger
from smateway.closure_qualification import ARMS, ComplexDetection, leaf_source_set_sha256
from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
)
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    canonical_json_sha256,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import (
    attestation_sha256,
    attest_runtime,
    validate_runtime_attestation,
)
from smateway.selector_flash_attestation import (
    FLASH_BASE_ADDRESS,
    GPIOA_ODR_ADDRESS,
    SELECTOR_GPIO_MASK,
    STM32C011_UID_ADDRESS,
    STM32C011_UID_SIZE_BYTES,
    validate_sealed_selector_evidence,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
RUN_KIND = "5g8_arm_preserving_c_i_or_d2_i_one_stream"
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
CONDITION_RECORD_FILENAME = "condition-record.json"
OBSERVATION_FILENAME = "normalized-observation.json"

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
SOURCE_FILES = (
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


class ArmPreservingRunError(RuntimeError):
    """The condition failed before one raw artifact could be accepted."""


class CaptureBoundary(Protocol):
    def __call__(
        self,
        plan: SafeDdsTonePlan,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Callable[[SampleBlockV2], None],
    ) -> Any: ...


class MuteBoundary(Protocol):
    def __call__(self, serial: str, purpose: str) -> dict[str, Any]: ...


class IdentityBoundary(Protocol):
    def __call__(self, serial: str, uri: str) -> dict[str, Any]: ...


class SelectorBoundary(Protocol):
    def __call__(
        self,
        control: Mapping[str, Any],
        purpose: str,
        evidence_root: Path,
    ) -> dict[str, Any]: ...


class TargetImageBoundary(Protocol):
    def __call__(
        self,
        control: Mapping[str, Any],
        evidence_root: Path,
        source_binding: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class TargetHaltBoundary(Protocol):
    def __call__(
        self,
        control: Mapping[str, Any],
        purpose: str,
        evidence_root: Path,
        source_binding: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, default=str))


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None:
        raise ArmPreservingRunError(f"{label} is not a safe identifier")
    return value


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ArmPreservingRunError(f"{label} path contains a symlink: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise ArmPreservingRunError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArmPreservingRunError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ArmPreservingRunError(f"{label} must contain one JSON object")
    return value


def _file_evidence(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise ArmPreservingRunError(f"{label} must be a regular non-symlink file")
    return {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": exact.stat().st_size,
    }


def _verify_file_evidence(value: Mapping[str, Any], label: str) -> None:
    expected = _file_evidence(Path(str(value.get("path"))), label)
    if expected != dict(value):
        raise ArmPreservingRunError(f"{label} path/hash/size binding is stale")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact.parent, "immutable output parent")
    exact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(exact.parent, "immutable output parent")
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    descriptor = os.open(exact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            exact.unlink()
        raise
    _fsync_directory(exact.parent)


def _repository_source_attestation(repository: Path = _REPOSITORY) -> dict[str, Any]:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if GIT_COMMIT.fullmatch(head) is None:
        raise ArmPreservingRunError("Smateway HEAD is not a full Git commit")
    attestation = attest_source_files_at_commit(
        repository,
        expected_commit=head,
        relative_paths=SOURCE_FILES,
    )
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": head,
        "files": attestation["files"],
        "source_files_sha256": canonical_json_sha256(attestation["files"]),
        "listed_files_clean_and_equal_to_commit": True,
    }


def _safe_local_state_root(path: Path) -> Path:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "state root")
    try:
        assert_local_rpi_storage(exact, label="state root")
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or root in exact.parents for root in forbidden):
        raise ArmPreservingRunError(
            "state root must be local Raspberry Pi storage, not Pluto/removable storage"
        )
    return exact


def _ensure_local_directory(path: Path, label: str) -> Path:
    """Create one directory only on the local Pi filesystem without symlink ancestry."""

    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    try:
        assert_local_rpi_storage(exact, label=label)
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    exact.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_dir():
        raise ArmPreservingRunError(f"{label} must be a real directory")
    try:
        assert_local_rpi_storage(exact, label=label)
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    return exact


def _live_source_binding(contract: Mapping[str, Any], control: Mapping[str, Any]) -> dict[str, Any]:
    """Bind live safety evidence to the immutable source and sealed selector image."""

    source = contract.get("source")
    smateway = source.get("smateway") if isinstance(source, Mapping) else None
    dependency = source.get("pluto_plus_utils") if isinstance(source, Mapping) else None
    native = source.get("native_libiio") if isinstance(source, Mapping) else None
    selector = control.get("selector_flash_attestation")
    if not all(
        isinstance(item, Mapping) for item in (source, smateway, dependency, native, selector)
    ):
        raise ArmPreservingRunError("live safety source binding is malformed")
    assert isinstance(source, Mapping)
    assert isinstance(smateway, Mapping)
    assert isinstance(dependency, Mapping)
    assert isinstance(selector, Mapping)
    binding = {
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
    if (
        GIT_COMMIT.fullmatch(str(binding["smateway_commit"])) is None
        or GIT_COMMIT.fullmatch(str(binding["dependency_commit"])) is None
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(binding[name])) is None
            for name in (
                "plan_source_sha256",
                "smateway_files_sha256",
                "dependency_files_sha256",
                "native_libiio_attestation_sha256",
                "selector_flash_attestation_sha256",
            )
        )
    ):
        raise ArmPreservingRunError("live safety source binding is incomplete")
    return binding


def _require_local_storage_contract(
    contract: Mapping[str, Any], *, condition_root: Path
) -> tuple[Path, Path]:
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise ArmPreservingRunError("plan local-storage contract is missing")
    raw_condition = storage.get("condition_root")
    raw_capture = storage.get("capture_root")
    if (
        storage.get("local_rpi_only") is not True
        or storage.get("pluto_storage_forbidden") is not True
        or not isinstance(raw_condition, str)
        or not Path(raw_condition).is_absolute()
        or not isinstance(raw_capture, str)
        or not Path(raw_capture).is_absolute()
    ):
        raise ArmPreservingRunError("plan local-storage contract is malformed")
    exact_condition = Path(raw_condition).expanduser().absolute()
    exact_capture = Path(raw_capture).expanduser().absolute()
    if exact_condition != condition_root.expanduser().absolute():
        raise ArmPreservingRunError("plan condition root differs from immutable plan location")
    try:
        assert_local_rpi_storage(exact_condition, label="condition storage")
        assert_local_rpi_storage(exact_capture, label="capture storage")
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    return exact_condition, exact_capture


def _selector_control_from_sealed(
    sealed: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    frozen = sealed.get("frozen_inputs")
    if not isinstance(frozen, Mapping):
        raise ArmPreservingRunError("sealed selector evidence lacks frozen inputs")
    files = frozen.get("files")
    profile = frozen.get("control_profile")
    if not isinstance(files, Mapping) or not isinstance(profile, Mapping):
        raise ArmPreservingRunError("sealed selector evidence lacks bench files/profile")
    manifest_binding = files.get("build_manifest")
    config_binding = files.get("openocd_config")
    firmware_binding = files.get("firmware_bin")
    extent = frozen.get("firmware_bin_extent")
    target_identity = sealed.get("target_identity")
    if not all(
        isinstance(item, Mapping)
        for item in (manifest_binding, config_binding, firmware_binding, extent, target_identity)
    ):
        raise ArmPreservingRunError(
            "sealed selector evidence lacks manifest/OpenOCD/firmware/target binding"
        )
    assert isinstance(manifest_binding, Mapping)
    assert isinstance(config_binding, Mapping)
    assert isinstance(firmware_binding, Mapping)
    assert isinstance(extent, Mapping)
    assert isinstance(target_identity, Mapping)
    manifest_file = dict(manifest_binding)
    config_file = dict(config_binding)
    firmware_file = dict(firmware_binding)
    _verify_file_evidence(manifest_file, "sealed bench build manifest")
    _verify_file_evidence(config_file, "sealed OpenOCD configuration")
    _verify_file_evidence(firmware_file, "sealed firmware BIN")
    board_id = str(binding.get("board_id"))
    expected_uid = board_id.removeprefix("stm32c011-")
    if (
        extent.get("flash_base_address") != FLASH_BASE_ADDRESS
        or extent.get("size_bytes") != firmware_file["size_bytes"]
        or extent.get("sha256") != firmware_file["sha256"]
        or target_identity.get("uid") != expected_uid
        or target_identity.get("matches_board_id") is not True
    ):
        raise ArmPreservingRunError("sealed firmware extent/UID identity is inconsistent")
    manifest = BenchManifest.load(Path(str(manifest_file["path"])))
    all_off_code = profile.get("all_off_code")
    if isinstance(all_off_code, bool) or not isinstance(all_off_code, int):
        raise ArmPreservingRunError("sealed bench ALL_OFF code is not an integer")
    if not 0 <= all_off_code <= SELECTOR_GPIO_MASK:
        raise ArmPreservingRunError("sealed bench ALL_OFF code does not fit selector GPIO")
    return {
        "schema": 1,
        "control_kind": "sealed_bench_static_all_off",
        "selector_flash_attestation": _json_safe(binding["file"]),
        "build_manifest": manifest_file,
        "openocd_config": config_file,
        "target_image_admission": {
            "schema": 1,
            "flash_base_address": FLASH_BASE_ADDRESS,
            "firmware_bin": firmware_file,
            "board_id": board_id,
            "expected_uid": expected_uid,
            "selector_flash_attestation_sha256": binding["file"]["sha256"],
            "full_bin_extent_and_uid_required_before_mailbox": True,
            "mismatch_must_remain_halted": True,
        },
        "all_off_code": all_off_code,
        "mailbox": {
            "address": manifest.address,
            "size": manifest.size,
            "magic": manifest.magic,
            "version": manifest.version,
            "max_lease_ms": manifest.max_lease_ms,
            "offsets": manifest.offsets,
        },
        "gpioa_odr_address": GPIOA_ODR_ADDRESS,
        "selector_gpio_mask": SELECTOR_GPIO_MASK,
        "required_lease_ms": 0,
        "live_raw_mailbox_and_gpio_readback_required": True,
    }


def _validate_selector_control(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        value.get("schema") != 1
        or value.get("control_kind") != "sealed_bench_static_all_off"
        or value.get("required_lease_ms") != 0
        or value.get("gpioa_odr_address") != GPIOA_ODR_ADDRESS
        or value.get("selector_gpio_mask") != SELECTOR_GPIO_MASK
        or value.get("live_raw_mailbox_and_gpio_readback_required") is not True
    ):
        raise ArmPreservingRunError("selector control contract is invalid")
    for name in ("selector_flash_attestation", "build_manifest", "openocd_config"):
        raw = value.get(name)
        if not isinstance(raw, Mapping):
            raise ArmPreservingRunError(f"selector control lacks {name} file binding")
        _verify_file_evidence(raw, f"selector control {name}")
    target = value.get("target_image_admission")
    if not isinstance(target, Mapping):
        raise ArmPreservingRunError("selector control lacks target-image admission")
    firmware = target.get("firmware_bin")
    selector_binding = value["selector_flash_attestation"]
    if not isinstance(firmware, Mapping) or not isinstance(selector_binding, Mapping):
        raise ArmPreservingRunError("selector target-image file binding is malformed")
    _verify_file_evidence(firmware, "selector target-image firmware BIN")
    board_id = target.get("board_id")
    expected_uid = str(board_id).removeprefix("stm32c011-") if isinstance(board_id, str) else None
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
        or not isinstance(board_id, str)
        or not board_id.startswith("stm32c011-")
        or expected_uid is None
        or len(expected_uid) != 24
        or any(character not in "0123456789abcdef" for character in expected_uid)
        or target.get("expected_uid") != expected_uid
        or target.get("selector_flash_attestation_sha256") != selector_binding.get("sha256")
        or target.get("full_bin_extent_and_uid_required_before_mailbox") is not True
        or target.get("mismatch_must_remain_halted") is not True
    ):
        raise ArmPreservingRunError("selector target-image admission contract is invalid")
    manifest = BenchManifest.load(Path(str(value["build_manifest"]["path"])))
    expected_mailbox = {
        "address": manifest.address,
        "size": manifest.size,
        "magic": manifest.magic,
        "version": manifest.version,
        "max_lease_ms": manifest.max_lease_ms,
        "offsets": manifest.offsets,
    }
    if value.get("mailbox") != expected_mailbox:
        raise ArmPreservingRunError("selector mailbox contract differs from build manifest")
    code = value.get("all_off_code")
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 15:
        raise ArmPreservingRunError("selector ALL_OFF code is invalid")
    normalized = _json_safe(value)
    if not isinstance(normalized, dict):  # pragma: no cover - input is a mapping.
        raise AssertionError("canonical selector normalization returned a non-object")
    return normalized


def _validate_sealed_selector_for_fixture(
    fixture: ValidatedArmPreservingFixture,
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = fixture.selector_flash_attestation
    file_binding = binding["file"]
    _verify_file_evidence(file_binding, "selector flash attestation")
    sealed = validate_sealed_selector_evidence(
        Path(str(file_binding["path"])),
        expected_sha256=str(file_binding["sha256"]),
        expected_campaign_id=str(binding["campaign_id"]),
        expected_run_id=str(binding["run_id"]),
        expected_board_id=str(binding["board_id"]),
        expected_image_role="bench",
    )
    return sealed, _selector_control_from_sealed(sealed, binding)


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    role: str,
    arm: str,
    repeat_index: int,
    fixture_document: Mapping[str, Any],
    fixture_file: Mapping[str, Any],
    setup_document: Mapping[str, Any],
    setup_file: Mapping[str, Any],
    selector_control: Mapping[str, Any],
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    native_attestation: Mapping[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    run = _validate_identifier(run_id, "run ID")
    _validate_identifier(board_id, "board ID")
    _validate_identifier(serial, "Pluto serial")
    if USB_URI.fullmatch(uri) is None:
        raise ArmPreservingRunError("capture requires an explicit current usb: URI")
    if role not in ROLES or arm not in ARMS or repeat_index not in range(1, 6):
        raise ArmPreservingRunError("condition must be c_i|d2_i, ANT1..ANT8, repeat 1..5")
    fixture = validate_fixture_v2(fixture_document)
    if fixture.board_id != board_id or fixture.pluto_serial != serial:
        raise ArmPreservingRunError("fixture board/Pluto identity differs from CLI")
    fixture_file_sha = str(fixture_file.get("sha256"))
    if canonical_sha256(fixture.document) != fixture.fixture_sha256:
        raise ArmPreservingRunError("fixture canonical identity is inconsistent")
    setup = validate_setup_attestation(
        setup_document,
        fixture=fixture,
        fixture_file_sha256=fixture_file_sha,
        run_id=run,
        role=role,
        arm=arm,
        repeat_index=repeat_index,
    )
    if source_attestation.get("commit") != fixture.source_commit:
        raise ArmPreservingRunError("current Smateway commit differs from fixture closure plan")
    source_files_hash = source_attestation.get("source_files_sha256")
    dependency_commit = dependency_attestation.get("commit")
    dependency_files = dependency_attestation.get("files")
    if (
        not isinstance(source_files_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_files_hash) is None
    ):
        raise ArmPreservingRunError("source attestation lacks a source-file hash")
    if not isinstance(dependency_commit, str) or GIT_COMMIT.fullmatch(dependency_commit) is None:
        raise ArmPreservingRunError("dependency attestation lacks a full Git commit")
    if not isinstance(dependency_files, list) or not dependency_files:
        raise ArmPreservingRunError("dependency attestation lacks source files")
    native = validate_runtime_attestation(native_attestation)
    native_sha = attestation_sha256(native)
    selector = _validate_selector_control(selector_control)
    exact_state = _safe_local_state_root(state_root)
    condition_root = (
        exact_state
        / "boards"
        / board_id
        / "5g8-arm-preserving-d2"
        / fixture.campaign_id
        / role
        / arm
        / f"repeat-{repeat_index}"
        / run
    )
    capture_root = (
        exact_state
        / "boards"
        / board_id
        / "pluto-usb-captures"
        / "5g8-arm-preserving-d2"
        / fixture.campaign_id
        / role
        / arm
        / f"repeat-{repeat_index}"
        / run
    )
    condition_id = f"{fixture.campaign_id}.{role}.{arm}.repeat-{repeat_index}.{run}"
    rf_safety = fixture.document["rf_safety"]
    return {
        "schema": 1,
        "run_kind": RUN_KIND,
        "run_id": run,
        "condition_id": condition_id,
        "campaign_id": fixture.campaign_id,
        "board_id": board_id,
        "configuration": {"serial": serial, "uri": uri},
        "condition": {
            "role": role,
            "arm": arm,
            "repeat_index": repeat_index,
            "topology_identity": {
                "canonical_json": fixture.topology(arm, role).canonical_json,
                "sha256": fixture.topology(arm, role).sha256,
            },
        },
        "fixture": {
            "document": fixture.document,
            "file": _json_safe(fixture_file),
            "fixture_sha256": fixture.fixture_sha256,
            "fixture_graph_sha256": fixture.fixture_graph_identity.sha256,
            "reference_plane_sha256": fixture.reference_plane_identity.sha256,
            "closure_plan_sha256": fixture.plan_identity.sha256,
        },
        "setup_attestation": {"document": setup, "file": _json_safe(setup_file)},
        "selector_control": selector,
        "acquisition": {
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": TOTAL_SAMPLES,
            "kernel_buffers": KERNEL_BUFFERS,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
            "dds_scale": DDS_SCALE,
            "minimum_reference_snr_db": MINIMUM_REFERENCE_SNR_DB,
            "rf_safety": rf_safety,
        },
        "source": {
            "smateway": _json_safe(source_attestation),
            "pluto_plus_utils": _json_safe(dependency_attestation),
            "dependency_files_sha256": canonical_json_sha256(dependency_files),
            "native_libiio": native,
            "native_libiio_sha256": native_sha,
        },
        "storage": {
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
            "condition_root": str(condition_root),
            "capture_root": str(capture_root),
        },
        "execution": {
            "one_capture_stream_per_run": True,
            "exact_five_source_distinct_repeats_per_condition": True,
            "automatic_retry": False,
            "failed_run_id_burned": True,
            "persistence_only_after_final_mute_and_all_off": True,
            "quarantine_never_accepted": True,
            "topology_diagnostic_only": True,
            "closure_claim_permitted": False,
        },
    }


def _plan_envelope(contract: Mapping[str, Any]) -> dict[str, Any]:
    frozen = _json_safe(contract)
    return {
        "schema": 1,
        "immutable": True,
        "plan_contract": frozen,
        "plan_contract_sha256": canonical_sha256(frozen),
        "hash_provenance": "UTF-8 canonical JSON, sorted keys, compact separators",
    }


def _new_manifest(plan_path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    return {
        "schema": 1,
        "run_kind": RUN_KIND,
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_path(plan_path),
            "contract_sha256": envelope["plan_contract_sha256"],
        },
        "attempts": [],
        "result": None,
        "error": None,
        "accepted_stream_count": 0,
    }


def _prepare_plan(
    plan_path: Path,
    manifest_path: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    condition_root, capture_root = _require_local_storage_contract(
        contract, condition_root=plan_path.parent
    )
    envelope = _plan_envelope(contract)
    if condition_root.exists() or condition_root.is_symlink() or capture_root.exists():
        if (
            condition_root.is_dir()
            and not condition_root.is_symlink()
            and plan_path.is_file()
            and manifest_path.is_file()
            and not plan_path.is_symlink()
            and not manifest_path.is_symlink()
        ):
            observed = _read_json(plan_path, "immutable plan")
            manifest = _read_json(manifest_path, "manifest")
            if observed != envelope or manifest.get("status") != "prepared":
                raise ArmPreservingRunError(
                    "existing run ID is not an intact matching prepared plan"
                )
            if capture_root.exists() or capture_root.is_symlink():
                raise ArmPreservingRunError("prepared run unexpectedly has capture history")
            return observed, manifest
        raise ArmPreservingRunError("run ID has prior plan, tombstone, or capture history")
    _write_immutable_json(plan_path, envelope)
    manifest = _new_manifest(plan_path, envelope)
    write_json_atomic(manifest_path, manifest)
    return envelope, manifest


def _strict_mute(serial: str, purpose: str) -> dict[str, Any]:
    started_at = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "status": "failed",
            "purpose": purpose,
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    return {
        "status": "passed",
        "purpose": purpose,
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": [-80.0, -80.0],
        "dds_scale_readback": [0.0] * 8,
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
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


def _call_mute(boundary: MuteBoundary, serial: str, purpose: str) -> dict[str, Any]:
    try:
        value = boundary(serial, purpose)
    except BaseException as error:
        return {
            "status": "failed",
            "purpose": purpose,
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": _error_document(error),
        }
    return value


def _live_identity(serial: str, uri: str) -> dict[str, Any]:
    resolved = resolve_iio_uri(uri, serial)
    return {
        "status": "passed" if resolved == uri else "failed",
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": resolved,
        "exact_uri_match": resolved == uri,
        "sysfs_path": find_usb_sysfs_path(serial),
        "scan_mutates_radio_state": False,
    }


def _identity_passed(value: object, *, serial: str, uri: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("requested_uri") == uri
        and value.get("resolved_uri") == uri
        and value.get("exact_uri_match") is True
        and value.get("scan_mutates_radio_state") is False
    )


def _openocd_tcl_path(path: Path) -> str:
    value = str(path.expanduser().absolute())
    if any(character in value for character in ("{", "}", "\n", "\r")):
        raise ArmPreservingRunError("OpenOCD evidence path contains unsafe Tcl characters")
    return "{" + value + "}"


def _live_target_halt(
    control: Mapping[str, Any],
    purpose: str,
    evidence_root: Path,
    source_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Issue and attest one independent halt without reading or writing the mailbox."""

    config = control.get("openocd_config")
    if not isinstance(config, Mapping):
        raise ArmPreservingRunError("target-halt control lacks its frozen OpenOCD configuration")
    _verify_file_evidence(config, "target-halt OpenOCD configuration")
    frozen_control = _json_safe(control)
    if not isinstance(frozen_control, dict):  # pragma: no cover - input is a mapping.
        raise AssertionError("target-halt control did not normalize to an object")
    root = evidence_root / purpose
    if root.exists() or root.is_symlink():
        raise ArmPreservingRunError("target-halt evidence directory already exists")
    _ensure_local_directory(root, f"target halt {purpose} evidence")
    config_path = Path(str(config["path"]))
    command = "init; halt; shutdown"
    result = subprocess.run(
        ("openocd", "-f", str(config_path), "-c", command),
        cwd=_REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path = root / "halt-openocd.json"
    _write_immutable_json(
        log_path,
        {
            "argv": ["openocd", "-f", str(config_path), "-c", command],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    _ensure_local_directory(root, f"target halt {purpose} evidence")
    passed = result.returncode == 0
    return {
        "schema": 1,
        "evidence_kind": "selector_target_best_effort_halt_v1",
        "status": "passed" if passed else "failed",
        "purpose": purpose,
        "source_binding": _json_safe(source_binding),
        "source_binding_sha256": canonical_sha256(source_binding),
        "control_sha256": canonical_sha256(frozen_control),
        "openocd_config": _json_safe(config),
        "command": command,
        "returncode": result.returncode,
        "target_halted": passed,
        "mailbox_access_performed": False,
        "openocd_log": _file_evidence(log_path, f"target halt {purpose} OpenOCD log"),
        "error": (
            None
            if passed
            else {
                "type": "TargetHaltFailed",
                "message": "independent best-effort target halt returned nonzero",
            }
        ),
    }


def _call_target_halt(
    boundary: TargetHaltBoundary,
    control: Mapping[str, Any],
    purpose: str,
    evidence_root: Path,
    source_binding: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return boundary(control, purpose, evidence_root, source_binding)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "selector_target_best_effort_halt_v1",
            "status": "failed",
            "purpose": purpose,
            "source_binding": _json_safe(source_binding),
            "source_binding_sha256": canonical_sha256(source_binding),
            "control_sha256": canonical_sha256(control),
            "openocd_config": _json_safe(control.get("openocd_config")),
            "command": "init; halt; shutdown",
            "returncode": None,
            "target_halted": False,
            "mailbox_access_performed": False,
            "openocd_log": None,
            "error": _error_document(error),
        }


def _target_halt_passed(
    value: object,
    *,
    control: Mapping[str, Any],
    purpose: str,
    source_binding: Mapping[str, Any],
) -> bool:
    try:
        config = control.get("openocd_config")
        if not isinstance(config, Mapping):
            return False
        _verify_file_evidence(config, "target-halt OpenOCD configuration")
        if not isinstance(value, Mapping):
            return False
        log_binding = value.get("openocd_log")
        if not isinstance(log_binding, Mapping):
            return False
        _verify_file_evidence(log_binding, f"target halt {purpose} OpenOCD log")
        log = _read_json(Path(str(log_binding["path"])), f"target halt {purpose} OpenOCD log")
        return (
            value.get("schema") == 1
            and value.get("evidence_kind") == "selector_target_best_effort_halt_v1"
            and value.get("status") == "passed"
            and value.get("purpose") == purpose
            and value.get("source_binding") == dict(source_binding)
            and value.get("source_binding_sha256") == canonical_sha256(source_binding)
            and value.get("control_sha256") == canonical_sha256(control)
            and value.get("openocd_config") == dict(config)
            and value.get("command") == "init; halt; shutdown"
            and value.get("returncode") == 0
            and value.get("target_halted") is True
            and value.get("mailbox_access_performed") is False
            and log.get("returncode") == 0
            and log.get("argv", [])[-1] == "init; halt; shutdown"
            and value.get("error") is None
        )
    except (KeyError, OSError, TypeError, ValueError, ArmPreservingRunError):
        return False


def _live_target_image_admission(
    control: Mapping[str, Any],
    evidence_root: Path,
    source_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit exact selector flash bytes and UID while halted, before mailbox access."""

    validated = _validate_selector_control(control)
    target = validated["target_image_admission"]
    assert isinstance(target, Mapping)
    root = evidence_root / "target-image-admission"
    if root.exists() or root.is_symlink():
        raise ArmPreservingRunError("target-image admission directory already exists")
    _ensure_local_directory(root, "target-image live evidence")
    expected_path = Path(str(target["firmware_bin"]["path"]))
    expected_bytes = expected_path.read_bytes()
    expected_sha256 = str(target["firmware_bin"]["sha256"])
    byte_count = int(target["firmware_bin"]["size_bytes"])
    expected_uid = str(target["expected_uid"])
    config_path = Path(str(validated["openocd_config"]["path"]))
    target_path = root / "target-flash.bin"
    uid_path = root / "target-uid.bin"
    read_command = (
        "init; reset halt; "
        f"dump_image {_openocd_tcl_path(target_path)} 0x{FLASH_BASE_ADDRESS:x} "
        f"0x{byte_count:x}; "
        f"dump_image {_openocd_tcl_path(uid_path)} 0x{STM32C011_UID_ADDRESS:x} "
        f"0x{STM32C011_UID_SIZE_BYTES:x}; shutdown"
    )
    read_result = subprocess.run(
        ("openocd", "-f", str(config_path), "-c", read_command),
        cwd=_REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    read_log_path = root / "readback-openocd.json"
    _write_immutable_json(
        read_log_path,
        {
            "argv": ["openocd", "-f", str(config_path), "-c", read_command],
            "returncode": read_result.returncode,
            "stdout": read_result.stdout,
            "stderr": read_result.stderr,
        },
    )
    observed_bytes = target_path.read_bytes() if target_path.is_file() else b""
    uid_bytes = uid_path.read_bytes() if uid_path.is_file() else b""
    observed_sha256 = hashlib.sha256(observed_bytes).hexdigest() if observed_bytes else None
    observed_uid = uid_bytes.hex() if uid_bytes else None
    compared_full_extent_and_uid = (
        read_result.returncode == 0
        and len(observed_bytes) == byte_count
        and len(uid_bytes) == STM32C011_UID_SIZE_BYTES
    )
    exact_match = (
        compared_full_extent_and_uid
        and observed_bytes == expected_bytes
        and observed_sha256 == expected_sha256
        and observed_uid == expected_uid
    )
    followup_command = "init; reset run; shutdown" if exact_match else "init; halt; shutdown"
    followup_result = subprocess.run(
        ("openocd", "-f", str(config_path), "-c", followup_command),
        cwd=_REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    followup_log_path = root / "target-state-openocd.json"
    _write_immutable_json(
        followup_log_path,
        {
            "argv": ["openocd", "-f", str(config_path), "-c", followup_command],
            "returncode": followup_result.returncode,
            "stdout": followup_result.stdout,
            "stderr": followup_result.stderr,
        },
    )
    target_running = exact_match and followup_result.returncode == 0
    passed = target_running
    failure_halt: dict[str, Any] | None = None
    failure_halt_passed = False
    if not passed:
        failure_halt = _call_target_halt(
            _live_target_halt,
            validated,
            "image_admission_failure_cleanup",
            evidence_root,
            source_binding,
        )
        failure_halt_passed = _target_halt_passed(
            failure_halt,
            control=validated,
            purpose="image_admission_failure_cleanup",
            source_binding=source_binding,
        )
    target_halted_on_failure = not passed and failure_halt_passed
    _ensure_local_directory(root, "target-image live evidence")
    for path in (target_path, uid_path):
        if path.is_file():
            path.chmod(0o400)
    _fsync_directory(root)
    return {
        "schema": 1,
        "evidence_kind": "arm_preserving_contemporaneous_full_bin_uid_admission_v1",
        "status": "passed" if passed else "failed",
        "purpose": "pre_mailbox_target_image_admission",
        "source_binding": _json_safe(source_binding),
        "source_binding_sha256": canonical_sha256(source_binding),
        "selector_flash_attestation_sha256": target["selector_flash_attestation_sha256"],
        "flash_base_address": FLASH_BASE_ADDRESS,
        "byte_count": byte_count,
        "expected_bin_sha256": expected_sha256,
        "observed_target_sha256": observed_sha256,
        "expected_board_id": target["board_id"],
        "observed_uid": observed_uid,
        "full_bin_and_uid_compared_while_halted": compared_full_extent_and_uid,
        "exact_bin_and_uid_match": exact_match,
        "reviewed_image_started_only_after_exact_match": target_running,
        "target_may_have_started_before_failure_halt": not passed,
        "failure_halt_required": not passed,
        "failure_halt": failure_halt,
        "target_kept_halted_on_failure": target_halted_on_failure,
        "mailbox_access_performed": False,
        "operation_order": [
            "target_reset_halt",
            "full_firmware_bin_extent_readback",
            "stm32_uid_readback",
            "exact_bytes_and_uid_compare",
            "reset_run_after_exact_match" if exact_match else "halt_after_mismatch",
            *(["independent_halt_after_failed_admission"] if not passed else []),
        ],
        "target_flash_readback": (
            _file_evidence(target_path, "target flash readback") if target_path.is_file() else None
        ),
        "target_uid_readback": (
            _file_evidence(uid_path, "target UID readback") if uid_path.is_file() else None
        ),
        "readback_openocd_log": _file_evidence(read_log_path, "target readback OpenOCD log"),
        "target_state_openocd_log": _file_evidence(followup_log_path, "target-state OpenOCD log"),
        "error": (
            None
            if passed
            else {
                "type": (
                    "TargetResetRunFailure"
                    if exact_match
                    else (
                        "SelectorImageOrUidMismatch"
                        if compared_full_extent_and_uid
                        else "TargetReadbackFailure"
                    )
                ),
                "message": (
                    "exact target image could not be started and was independently halted"
                    if exact_match
                    else "target full-BIN extent or UID differs from sealed evidence"
                ),
            }
        ),
    }


def _call_target_image_admission(
    boundary: TargetImageBoundary,
    control: Mapping[str, Any],
    evidence_root: Path,
    source_binding: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        value = boundary(control, evidence_root, source_binding)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "arm_preserving_contemporaneous_full_bin_uid_admission_v1",
            "status": "failed",
            "purpose": "pre_mailbox_target_image_admission",
            "source_binding": _json_safe(source_binding),
            "source_binding_sha256": canonical_sha256(source_binding),
            "mailbox_access_performed": False,
            "error": _error_document(error),
        }
    return value


def _target_image_passed(
    value: object,
    *,
    control: Mapping[str, Any],
    source_binding: Mapping[str, Any],
) -> bool:
    try:
        validated = _validate_selector_control(control)
        target = validated["target_image_admission"]
        assert isinstance(target, Mapping)
        if not isinstance(value, Mapping):
            return False
        target_file = value.get("target_flash_readback")
        uid_file = value.get("target_uid_readback")
        read_log = value.get("readback_openocd_log")
        state_log = value.get("target_state_openocd_log")
        if not all(
            isinstance(item, Mapping) for item in (target_file, uid_file, read_log, state_log)
        ):
            return False
        assert isinstance(target_file, Mapping)
        assert isinstance(uid_file, Mapping)
        assert isinstance(read_log, Mapping)
        assert isinstance(state_log, Mapping)
        for item, label in (
            (target_file, "target flash readback"),
            (uid_file, "target UID readback"),
            (read_log, "target readback OpenOCD log"),
            (state_log, "target-state OpenOCD log"),
        ):
            assert isinstance(item, Mapping)
            _verify_file_evidence(item, label)
        expected_path = Path(str(target["firmware_bin"]["path"]))
        observed_path = Path(str(target_file["path"]))
        uid_path = Path(str(uid_file["path"]))
        expected_sha256 = str(target["firmware_bin"]["sha256"])
        byte_count = int(target["firmware_bin"]["size_bytes"])
        expected_uid = str(target["expected_uid"])
        read_log_document = _read_json(Path(str(read_log["path"])), "target readback OpenOCD log")
        state_log_document = _read_json(Path(str(state_log["path"])), "target-state OpenOCD log")
        return (
            value.get("schema") == 1
            and value.get("evidence_kind")
            == "arm_preserving_contemporaneous_full_bin_uid_admission_v1"
            and value.get("status") == "passed"
            and value.get("purpose") == "pre_mailbox_target_image_admission"
            and value.get("source_binding") == dict(source_binding)
            and value.get("source_binding_sha256") == canonical_sha256(source_binding)
            and value.get("selector_flash_attestation_sha256")
            == target.get("selector_flash_attestation_sha256")
            and value.get("flash_base_address") == FLASH_BASE_ADDRESS
            and value.get("byte_count") == byte_count
            and value.get("expected_bin_sha256") == expected_sha256
            and value.get("observed_target_sha256") == expected_sha256
            and value.get("expected_board_id") == target.get("board_id")
            and value.get("observed_uid") == expected_uid
            and value.get("full_bin_and_uid_compared_while_halted") is True
            and value.get("exact_bin_and_uid_match") is True
            and value.get("reviewed_image_started_only_after_exact_match") is True
            and value.get("target_may_have_started_before_failure_halt") is False
            and value.get("failure_halt_required") is False
            and value.get("failure_halt") is None
            and value.get("target_kept_halted_on_failure") is False
            and value.get("mailbox_access_performed") is False
            and value.get("operation_order")
            == [
                "target_reset_halt",
                "full_firmware_bin_extent_readback",
                "stm32_uid_readback",
                "exact_bytes_and_uid_compare",
                "reset_run_after_exact_match",
            ]
            and observed_path.stat().st_size == byte_count
            and observed_path.read_bytes() == expected_path.read_bytes()
            and uid_path.read_bytes().hex() == expected_uid
            and read_log_document.get("returncode") == 0
            and state_log_document.get("returncode") == 0
            and state_log_document.get("argv", [])[-1] == "init; reset run; shutdown"
            and value.get("error") is None
        )
    except (AssertionError, KeyError, OSError, TypeError, ValueError, ArmPreservingRunError):
        return False


def _live_selector_all_off(
    control: Mapping[str, Any], purpose: str, evidence_root: Path
) -> dict[str, Any]:
    validated = _validate_selector_control(control)
    root = evidence_root / purpose
    if root.exists() or root.is_symlink():
        raise ArmPreservingRunError("selector live-evidence directory already exists")
    _ensure_local_directory(root, f"selector {purpose} live evidence")
    manifest_path = Path(str(validated["build_manifest"]["path"]))
    config_path = Path(str(validated["openocd_config"]["path"]))
    manifest = BenchManifest.load(manifest_path)
    code = int(validated["all_off_code"])
    bench = OpenOcdBench(manifest, config_path)
    requested = bench.request(code, 0, wait_until_applied=True)
    mailbox_path = root / "mailbox.bin"
    gpio_path = root / "gpioa-odr.bin"
    command = (
        "init; "
        f"dump_image {_openocd_tcl_path(mailbox_path)} 0x{manifest.address:08x} "
        f"0x{manifest.size:x}; "
        f"dump_image {_openocd_tcl_path(gpio_path)} 0x{GPIOA_ODR_ADDRESS:08x} 0x4; "
        "resume; shutdown"
    )
    result = subprocess.run(
        ("openocd", "-f", str(config_path), "-c", command),
        cwd=_REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path = root / "openocd.json"
    _write_immutable_json(
        log_path,
        {
            "argv": ["openocd", "-f", str(config_path), "-c", command],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    if result.returncode != 0:
        raise ArmPreservingRunError("OpenOCD mailbox/GPIO readback failed")
    _ensure_local_directory(root, f"selector {purpose} live evidence")
    status = decode_mailbox(mailbox_path.read_bytes(), manifest)
    gpio_bytes = gpio_path.read_bytes()
    if len(gpio_bytes) != 4:
        raise ArmPreservingRunError("GPIOA ODR readback is not exactly four bytes")
    gpio = int.from_bytes(gpio_bytes, "little")
    passed = (
        requested.command_sequence == status.command_sequence
        and status.command_sequence == status.acknowledged_sequence
        and status.command_code == code
        and status.applied_code == code
        and status.command_lease_ms == 0
        and status.remaining_lease_ms == 0
        and status.command_valid
        and not status.lease_active
        and not status.guard_active
        and not status.invalid_command
        and gpio & SELECTOR_GPIO_MASK == code
    )
    if not passed:
        raise ArmPreservingRunError("live mailbox/GPIO readback does not prove lease-free ALL_OFF")
    mailbox_path.chmod(0o400)
    gpio_path.chmod(0o400)
    _fsync_directory(root)
    return {
        "status": "passed",
        "purpose": purpose,
        "control_sha256": canonical_sha256(validated),
        "all_off_code": code,
        "lease_ms": 0,
        "mailbox": status.as_dict(),
        "gpioa_odr_raw_value": gpio,
        "gpioa_odr_masked_selector_code": gpio & SELECTOR_GPIO_MASK,
        "mailbox_readback": _file_evidence(mailbox_path, "mailbox readback"),
        "gpioa_odr_readback": _file_evidence(gpio_path, "GPIOA ODR readback"),
        "openocd_log": _file_evidence(log_path, "OpenOCD log"),
        "command_valid": True,
        "raw_mailbox_and_gpio_readback_passed": True,
        "error": None,
    }


def _call_selector(
    boundary: SelectorBoundary,
    control: Mapping[str, Any],
    purpose: str,
    evidence_root: Path,
) -> dict[str, Any]:
    try:
        return boundary(control, purpose, evidence_root)
    except BaseException as error:
        return {
            "status": "failed",
            "purpose": purpose,
            "control_sha256": canonical_sha256(control),
            "all_off_code": control.get("all_off_code"),
            "lease_ms": 0,
            "command_valid": False,
            "raw_mailbox_and_gpio_readback_passed": False,
            "error": _error_document(error),
        }


def _selector_passed(value: object, *, control: Mapping[str, Any], purpose: str) -> bool:
    try:
        validated = _validate_selector_control(control)
        if not isinstance(value, Mapping):
            return False
        mailbox_binding = value.get("mailbox_readback")
        gpio_binding = value.get("gpioa_odr_readback")
        log_binding = value.get("openocd_log")
        if not all(
            isinstance(item, Mapping) for item in (mailbox_binding, gpio_binding, log_binding)
        ):
            return False
        assert isinstance(mailbox_binding, Mapping)
        assert isinstance(gpio_binding, Mapping)
        assert isinstance(log_binding, Mapping)
        for item, label in (
            (mailbox_binding, "mailbox readback"),
            (gpio_binding, "GPIOA ODR readback"),
            (log_binding, "OpenOCD log"),
        ):
            assert isinstance(item, Mapping)
            _verify_file_evidence(item, label)
        manifest = BenchManifest.load(Path(str(validated["build_manifest"]["path"])))
        mailbox = decode_mailbox(
            Path(str(mailbox_binding["path"])).read_bytes(),
            manifest,
        )
        gpio_bytes = Path(str(gpio_binding["path"])).read_bytes()
        log = _read_json(Path(str(log_binding["path"])), "selector OpenOCD log")
        if len(gpio_bytes) != 4:
            return False
        gpio = int.from_bytes(gpio_bytes, "little")
        code = int(validated["all_off_code"])
        return (
            value.get("status") == "passed"
            and value.get("purpose") == purpose
            and value.get("control_sha256") == canonical_sha256(validated)
            and value.get("all_off_code") == code
            and value.get("lease_ms") == 0
            and value.get("mailbox") == mailbox.as_dict()
            and mailbox.command_sequence == mailbox.acknowledged_sequence
            and mailbox.command_code == code
            and mailbox.applied_code == code
            and mailbox.command_lease_ms == 0
            and mailbox.remaining_lease_ms == 0
            and mailbox.command_valid
            and not mailbox.lease_active
            and not mailbox.guard_active
            and not mailbox.invalid_command
            and value.get("gpioa_odr_raw_value") == gpio
            and value.get("gpioa_odr_masked_selector_code") == gpio & SELECTOR_GPIO_MASK == code
            and value.get("command_valid") is True
            and value.get("raw_mailbox_and_gpio_readback_passed") is True
            and log.get("returncode") == 0
            and value.get("error") is None
        )
    except (AssertionError, KeyError, OSError, TypeError, ValueError, ArmPreservingRunError):
        return False


def _live_capture(
    plan: SafeDdsTonePlan,
    *,
    samples_per_frame: int,
    frame_count: int,
    kernel_buffers: int,
    block_consumer: Callable[[SampleBlockV2], None],
) -> Any:
    return capture_continuous_safe_dds_tone(
        plan,
        samples_per_frame=samples_per_frame,
        frame_count=frame_count,
        kernel_buffers=kernel_buffers,
        block_consumer=block_consumer,
    )


def _settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _tone_plan(contract: Mapping[str, Any]) -> SafeDdsTonePlan:
    configuration = contract["configuration"]
    safety = contract["acquisition"]["rf_safety"]
    return SafeDdsTonePlan(
        uri=str(configuration["uri"]),
        serial=str(configuration["serial"]),
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=0,
        tx_hardware_gain_db=TX_HARDWARE_GAIN_DB,
        dds_scale=DDS_SCALE,
        receiver_gain_db=RECEIVER_GAIN_DB,
        source_peak_output_bound_dbm=float(safety["source_peak_output_bound_dbm"]),
        load_input_limit_dbm=float(safety["receiver_input_limit_dbm"]),
        path_attenuation_before_load_db=float(safety["minimum_path_attenuation_before_rx1_db"]),
        required_margin_db=float(safety["required_margin_db"]),
        settle_ms=100,
    )


def _block_ledger(blocks: Sequence[SampleBlockV2]) -> dict[str, Any]:
    if not blocks:
        raise ArmPreservingRunError("capture returned no ABI2 blocks")
    records: list[dict[str, int]] = []
    sample_start = 0
    for block in blocks:
        record = {
            "sample_start": sample_start,
            "sample_count": block.sample_count,
            "utc_ns": block.utc_ns,
            "metadata_abi": block.metadata_abi,
            "stream_id": block.stream_id,
            "buffer_sequence": block.buffer_sequence,
            "first_sample_sequence": block.first_sample_sequence,
            "last_sample_sequence_exclusive": block.last_sample_sequence_exclusive,
            "metadata_flags": block.metadata_flags,
            "missing_samples_before": block.missing_samples_before,
        }
        timing = {
            "sample_time_realtime_start_ns": block.sample_time_realtime_start_ns,
            "sample_time_realtime_end_ns": block.sample_time_realtime_end_ns,
            "sample_time_monotonic_start_ns": block.sample_time_monotonic_start_ns,
            "sample_time_monotonic_end_ns": block.sample_time_monotonic_end_ns,
            "sample_time_uncertainty_ns": block.sample_time_uncertainty_ns,
        }
        record.update({key: int(value) for key, value in timing.items() if value is not None})
        records.append(record)
        sample_start += block.sample_count
    return {
        "schema_version": 1,
        "metadata_abi": blocks[0].metadata_abi,
        "stream_id": blocks[0].stream_id,
        "block_count": len(blocks),
        "total_samples": sample_start,
        "first_sample_sequence": blocks[0].first_sample_sequence,
        "last_sample_sequence_exclusive": blocks[-1].last_sample_sequence_exclusive,
        "sample_sequence_span": (
            blocks[-1].last_sample_sequence_exclusive - blocks[0].first_sample_sequence
        ),
        "blocks": records,
    }


def _validate_capture(
    capture: Any,
    blocks: Sequence[SampleBlockV2],
    *,
    plan: SafeDdsTonePlan,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
        raise ArmPreservingRunError("capture identity differs from exact serial/current USB URI")
    if capture.settings != _settings():
        raise ArmPreservingRunError("capture settings differ from immutable plan")
    if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
        raise ArmPreservingRunError("capture sample/frame count differs from immutable plan")
    if capture.kernel_buffers != KERNEL_BUFFERS or len(blocks) != FRAME_COUNT:
        raise ArmPreservingRunError("kernel-buffer or retained-frame count differs")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in blocks):
        raise ArmPreservingRunError("capture does not contain exact paired RX frames")
    ledger = _block_ledger(blocks)
    continuity = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if continuity.metadata_abi != 2 or continuity.first_buffer_sequence != 0:
        raise ArmPreservingRunError("capture did not start one fresh continuous ABI2 stream")
    for proof, block in zip(capture.frames, blocks, strict=True):
        observed = (
            proof.stream_id,
            proof.buffer_sequence,
            proof.first_sample_sequence,
            proof.last_sample_sequence_exclusive,
            proof.metadata_abi,
        )
        expected = (
            block.stream_id,
            block.buffer_sequence,
            block.first_sample_sequence,
            block.last_sample_sequence_exclusive,
            block.metadata_abi,
        )
        if observed != expected:
            raise ArmPreservingRunError("capture proof differs from retained ABI2 block")
    rf = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [capture.tx_gain_readback_db, -80.0],
        "tx2_gain_readback_provenance": "capture_helper_internal_exact_readback",
        "dds_scale_requested": plan.dds_scale,
        "dds_scale_readback": list(capture.dds_scale_readback),
        "dds_enabled_readback": list(capture.dds_enabled_readback),
        "tone_frequency_hz_requested": plan.tone_frequency_hz,
        "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
        "active_dds_indices": [0, 2],
        "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
        "inactive_dds_rf_activity_contract": (
            "exact_zero_scale; enable_and_frequency_are raw diagnostics"
        ),
    }
    validate_tx1_rf_readback_evidence(
        rf,
        planned_kernel_buffers=KERNEL_BUFFERS,
        planned_tx_gain_db=TX_HARDWARE_GAIN_DB,
        planned_dds_scale=DDS_SCALE,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return continuity.stream_id, ledger, rf


def _quarantine_blocks(
    contract: Mapping[str, Any],
    blocks: Sequence[SampleBlockV2],
    error: BaseException,
) -> dict[str, Any]:
    capture_root = Path(str(contract["storage"]["capture_root"]))
    failed_parent = capture_root.parent / ".failed"
    _ensure_local_directory(failed_parent, "initial-memory quarantine root")
    artifact_id = uuid.uuid4().hex
    destination = failed_parent / f"{contract['run_id']}-{artifact_id}.failed"
    if destination.exists() or destination.is_symlink():
        raise ArmPreservingRunError("quarantine destination already exists")
    _ensure_local_directory(destination, "initial-memory quarantine destination")
    raw_path = destination / "retained.sigmf-data"
    with raw_path.open("xb") as stream:
        for block in blocks:
            stream.write(complex_to_ci16(block.samples).tobytes(order="C"))
        stream.flush()
        os.fsync(stream.fileno())
    _ensure_local_directory(destination, "initial-memory quarantine destination")
    failure = {
        "schema": 1,
        "failure_kind": "5g8_arm_preserving_capture_quarantine",
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "accepted": False,
        "may_be_used_for_closure": False,
        "automatic_retry_attempted": False,
        "retained_frame_count": len(blocks),
        "retained_sample_count": sum(block.sample_count for block in blocks),
        "raw_iq": _file_evidence(raw_path, "quarantined raw IQ"),
        "error": _error_document(error),
        "created_at": _now(),
    }
    _write_immutable_json(destination / "failure.json", failure)
    return {
        "path": str(destination),
        "accepted": False,
        "may_be_used_for_closure": False,
        "failure_sha256": sha256_path(destination / "failure.json"),
    }


def _quarantine_post_persistence_staging(
    contract: Mapping[str, Any], staging_root: Path
) -> Path | None:
    """Move failed persisted bytes only into a fresh local, non-symlink quarantine."""

    capture_root = Path(str(contract["storage"]["capture_root"]))
    failed_parent = capture_root.parent / ".failed"
    _ensure_local_directory(failed_parent, "post-persistence quarantine root")
    failed_destination = failed_parent / f"{contract['run_id']}-post-persistence.failed"
    if not staging_root.exists() and not staging_root.is_symlink():
        return None
    _assert_no_symlink_chain(staging_root, "post-persistence staging root")
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise ArmPreservingRunError("post-persistence staging root must be a real directory")
    if failed_destination.exists() or failed_destination.is_symlink():
        raise ArmPreservingRunError("post-persistence quarantine destination already exists")
    try:
        assert_local_rpi_storage(staging_root, label="post-persistence staging root")
        assert_local_rpi_storage(
            failed_destination, label="post-persistence quarantine destination"
        )
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    os.replace(staging_root, failed_destination)
    _assert_no_symlink_chain(failed_destination, "post-persistence quarantine destination")
    try:
        assert_local_rpi_storage(
            failed_destination, label="post-persistence quarantine destination"
        )
    except FileArtifactAdmissionError as error:
        raise ArmPreservingRunError(str(error)) from error
    _fsync_directory(failed_parent)
    return failed_destination


def _persist_blocks(
    root: Path,
    *,
    capture: Any,
    blocks: Sequence[SampleBlockV2],
    label: str,
) -> Any:
    writer = CaptureWriter(root, radio=capture.identity, settings=_settings(), label=label)
    try:
        for block in blocks:
            writer.append(block, _settings(), revision=1)
        artifact = writer.finalize()
    except BaseException as error:
        writer.fail(error)
        raise
    if not verify_artifact(artifact):
        raise ArmPreservingRunError("persisted artifact failed SHA-256 verification")
    return artifact


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    root = Path(artifact.path)
    raw = data_path(artifact)
    metadata = root / f"{artifact.artifact_id}.sigmf-meta"
    base = {
        "artifact_id": artifact.artifact_id,
        "path": str(root),
        "raw_iq_path": str(raw),
        "raw_iq_sha256": sha256_path(raw),
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
    }
    return {**base, "artifact_sha256": canonical_sha256(base)}


def _relocate_artifact_evidence(value: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    artifact_id = str(value["artifact_id"])
    base = {
        **dict(value),
        "path": str(destination),
        "raw_iq_path": str(destination / f"{artifact_id}.sigmf-data"),
        "metadata_path": str(destination / f"{artifact_id}.sigmf-meta"),
    }
    descriptor = {name: base[name] for name in base if name != "artifact_sha256"}
    return {**descriptor, "artifact_sha256": canonical_sha256(descriptor)}


def _execute_one_stream(
    contract: Mapping[str, Any],
    *,
    capture_boundary: CaptureBoundary = _live_capture,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity,
    target_image_boundary: TargetImageBoundary = _live_target_image_admission,
    target_halt_boundary: TargetHaltBoundary = _live_target_halt,
    selector_boundary: SelectorBoundary = _live_selector_all_off,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> dict[str, Any]:
    serial = str(contract["configuration"]["serial"])
    uri = str(contract["configuration"]["uri"])
    fixture = validate_fixture_v2(contract["fixture"]["document"])
    _verify_file_evidence(contract["fixture"]["file"], "fixture file")
    _verify_file_evidence(contract["setup_attestation"]["file"], "setup attestation file")
    _, live_control = _validate_sealed_selector_for_fixture(fixture)
    control = _validate_selector_control(contract["selector_control"])
    if live_control != control:
        raise ArmPreservingRunError("live sealed selector control differs from immutable plan")
    runtime_native = validate_runtime_attestation(native_boundary())
    if runtime_native != contract["source"]["native_libiio"]:
        raise ArmPreservingRunError("runtime native libiio differs from immutable plan")
    runtime_root = Path(str(contract["storage"]["condition_root"])) / "selector-live-evidence"
    if runtime_root.exists() or runtime_root.is_symlink():
        raise ArmPreservingRunError("selector live-evidence root already exists")
    _ensure_local_directory(runtime_root, "selector live-evidence root")
    source_binding = _live_source_binding(contract, control)

    plan = _tone_plan(contract)
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    capture: Any | None = None
    pending_error: BaseException | None = None
    identity: dict[str, Any] | None = None
    initial_mute: dict[str, Any] | None = None
    target_admission: dict[str, Any] | None = None
    selector_image_admitted = False
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    cleanup: dict[str, Any] | None = None
    final_mute: dict[str, Any] | None = None
    live_action_order: list[str] = []
    try:
        live_action_order.append("pre_capture_exact_mute")
        initial_mute = _call_mute(mute_boundary, serial, "pre_capture_exact_mute")
        if not _mute_passed(initial_mute, serial=serial, purpose="pre_capture_exact_mute"):
            raise ArmPreservingRunError("initial exact-radio mute failed")

        live_action_order.append("live_usb_identity")
        identity = identity_boundary(serial, uri)
        if not _identity_passed(identity, serial=serial, uri=uri):
            raise ArmPreservingRunError("current USB URI/serial identity preflight failed")

        live_action_order.append("target_full_bin_uid_admission")
        target_admission = _call_target_image_admission(
            target_image_boundary,
            control,
            runtime_root,
            source_binding,
        )
        selector_image_admitted = _target_image_passed(
            target_admission,
            control=control,
            source_binding=source_binding,
        )
        if not selector_image_admitted:
            live_action_order.append("target_admission_rejection_halt")
            rejection_halt = _call_target_halt(
                target_halt_boundary,
                control,
                "target-admission-rejection-halt",
                runtime_root,
                source_binding,
            )
            rejection_halt_passed = _target_halt_passed(
                rejection_halt,
                control=control,
                purpose="target-admission-rejection-halt",
                source_binding=source_binding,
            )
            rejection_path = runtime_root / "target-admission-rejection.json"
            _write_immutable_json(
                rejection_path,
                {
                    "schema": 1,
                    "evidence_kind": "arm_preserving_target_admission_rejection_v1",
                    "status": "failed_safe" if rejection_halt_passed else "halt_unproven",
                    "source_binding": source_binding,
                    "source_binding_sha256": canonical_sha256(source_binding),
                    "target_image_admission": target_admission,
                    "target_image_admission_sha256": canonical_sha256(target_admission),
                    "independent_rejection_halt": rejection_halt,
                    "independent_rejection_halt_sha256": canonical_sha256(rejection_halt),
                    "independent_rejection_halt_passed": rejection_halt_passed,
                    "mailbox_access_performed": False,
                    "live_action_order": live_action_order,
                    "created_at": _now(),
                },
            )
            rejection_binding = _file_evidence(
                rejection_path, "target-admission rejection evidence"
            )
            if not rejection_halt_passed:
                raise ArmPreservingRunError(
                    "selector target admission failed and an independent halt could not be "
                    f"proven; evidence={json.dumps(rejection_binding, sort_keys=True)}"
                )
            raise ArmPreservingRunError(
                "selector target full-BIN extent/UID admission failed before mailbox access; "
                f"independent halt proven; evidence={json.dumps(rejection_binding, sort_keys=True)}"
            )

        live_action_order.append("selector_all_off_before")
        before = _call_selector(selector_boundary, control, "before_capture", runtime_root)
        if not _selector_passed(before, control=control, purpose="before_capture"):
            raise ArmPreservingRunError("selector static ALL_OFF pre-capture attestation failed")

        live_action_order.append("capture")
        capture = capture_boundary(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        pending_error = error
    finally:
        live_action_order.append("final_acceptance_exact_mute")
        final_mute = _call_mute(mute_boundary, serial, "final_acceptance_exact_mute")
        if selector_image_admitted:
            live_action_order.append("selector_all_off_after")
            after = _call_selector(selector_boundary, control, "after_capture", runtime_root)
            live_action_order.append("cleanup_all_off")
            cleanup = _call_selector(selector_boundary, control, "cleanup_all_off", runtime_root)

    if not _mute_passed(final_mute, serial=serial, purpose="final_acceptance_exact_mute"):
        pending_error = ArmPreservingRunError("mandatory final exact-radio mute failed")
    if selector_image_admitted:
        if not _selector_passed(after, control=control, purpose="after_capture"):
            pending_error = ArmPreservingRunError("selector static ALL_OFF post-capture failed")
        if not _selector_passed(cleanup, control=control, purpose="cleanup_all_off"):
            pending_error = ArmPreservingRunError("selector static ALL_OFF cleanup failed")
    if pending_error is not None:
        quarantine = _quarantine_blocks(contract, retained, pending_error)
        retained.clear()
        wrapped_failure = ArmPreservingRunError(str(pending_error))
        wrapped_failure.add_note(f"quarantine={json.dumps(quarantine, sort_keys=True)}")
        raise wrapped_failure from pending_error
    assert capture is not None
    assert identity is not None and initial_mute is not None and target_admission is not None
    assert (
        final_mute is not None and before is not None and after is not None and cleanup is not None
    )

    try:
        stream_id, ledger, rf_readback = _validate_capture(capture, retained, plan=plan)
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        if not headroom.passed:
            raise ArmPreservingRunError("ADC headroom admission failed")
        clipped = [receiver.clipped_sample_count for receiver in headroom.receivers]
        if clipped != [0, 0]:
            raise ArmPreservingRunError("capture contains clipped samples")
        values = np.concatenate([block.samples for block in retained], axis=1)
        analysis = analyze_coherent_leakage(
            values[0],
            values[1],
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=TONE_OFFSET_HZ,
            block_duration_s=0.1,
            minimum_block_count=3,
        )
        del values
        if not analysis.quality_passed:
            raise ArmPreservingRunError(
                "coherent transfer analysis failed: "
                + ", ".join(analysis.quality_rejection_reasons)
            )
        if analysis.rx1.tone_to_noise_snr_db < MINIMUM_REFERENCE_SNR_DB:
            raise ArmPreservingRunError("RX1 conducted reference SNR is below 20 dB")
        transfer = analysis.rx2_over_rx1
        if transfer.phasor is not None and analysis.rx2.tone_detected:
            detection = ComplexDetection(True, transfer.phasor, None)
        else:
            if transfer.amplitude_upper_bound_ratio is None:
                raise ArmPreservingRunError("RX2 nondetection lacks a magnitude upper bound")
            detection = ComplexDetection(False, None, transfer.amplitude_upper_bound_ratio)
    except BaseException as error:
        quarantine = _quarantine_blocks(contract, retained, error)
        retained.clear()
        wrapped = ArmPreservingRunError(str(error))
        wrapped.add_note(f"quarantine={json.dumps(quarantine, sort_keys=True)}")
        raise wrapped from error

    capture_root = Path(str(contract["storage"]["capture_root"]))
    condition_root = Path(str(contract["storage"]["condition_root"]))
    staging_root = capture_root.parent / f".{capture_root.name}.staging"
    if capture_root.exists() or capture_root.is_symlink() or staging_root.exists():
        raise ArmPreservingRunError("accepted capture/staging root already exists")
    record_path = condition_root / CONDITION_RECORD_FILENAME
    observation_path = condition_root / OBSERVATION_FILENAME
    try:
        staged = _persist_blocks(
            staging_root,
            capture=capture,
            blocks=retained,
            label=(
                f"5.8 GHz arm-preserving {contract['condition']['role']} "
                f"{contract['condition']['arm']} repeat {contract['condition']['repeat_index']}"
            ),
        )
        staged_evidence = _artifact_evidence(staged)
        destination = capture_root / staged.artifact_id
        artifact_evidence = _relocate_artifact_evidence(staged_evidence, destination)
        relocated = staged.model_copy(update={"path": str(destination)})
        condition_record = {
            "schema": 1,
            "record_kind": "5g8_arm_preserving_condition_record",
            "created_at": _now(),
            "run_id": contract["run_id"],
            "condition_id": contract["condition_id"],
            "plan_contract_sha256": canonical_sha256(contract),
            "condition": contract["condition"],
            "fixture": contract["fixture"],
            "setup_attestation": contract["setup_attestation"],
            "source": contract["source"],
            "live_safety_source_binding": source_binding,
            "live_action_order": live_action_order,
            "identity_preflight": identity,
            "initial_mute": initial_mute,
            "target_image_admission": target_admission,
            "selector_all_off_before": before,
            "capture": {
                "artifact": relocated.model_dump(mode="json"),
                "artifact_evidence": artifact_evidence,
                "stream_id": stream_id,
                "continuity_ledger": ledger,
                "rf_readback": rf_readback,
                "headroom": asdict(headroom),
                "analysis": _json_safe(asdict(analysis)),
            },
            "final_mute": final_mute,
            "selector_all_off_after": after,
            "selector_all_off_cleanup": cleanup,
            "accepted_only_by_complete_manifest": True,
            "topology_limitation": fixture.document["topology_limitation"],
        }
        _write_immutable_json(record_path, condition_record)
        raw_sha = str(artifact_evidence["raw_iq_sha256"])
        leaves = tuple(sorted((raw_sha,)))
        observation = {
            "schema": 1,
            "observation_kind": OBSERVATION_KIND,
            "campaign_id": contract["campaign_id"],
            "board_id": contract["board_id"],
            "pluto_serial": serial,
            "role": contract["condition"]["role"],
            "arm": contract["condition"]["arm"],
            "repeat_index": contract["condition"]["repeat_index"],
            "run_id": contract["run_id"],
            "condition_id": contract["condition_id"],
            "fixture_file": contract["fixture"]["file"],
            "fixture_sha256": fixture.fixture_sha256,
            "setup_attestation_file": contract["setup_attestation"]["file"],
            "selector_flash_attestation_file": fixture.selector_flash_attestation["file"],
            "closure_plan_sha256": fixture.plan_identity.sha256,
            "topology_sha256": contract["condition"]["topology_identity"]["sha256"],
            "fixture_graph_sha256": fixture.fixture_graph_identity.sha256,
            "reference_plane_sha256": fixture.reference_plane_identity.sha256,
            "source": {
                "smateway_commit": contract["source"]["smateway"]["commit"],
                "smateway_files_sha256": contract["source"]["smateway"]["source_files_sha256"],
                "dependency_commit": contract["source"]["pluto_plus_utils"]["commit"],
                "dependency_files_sha256": contract["source"]["dependency_files_sha256"],
                "native_libiio_attestation_sha256": contract["source"]["native_libiio_sha256"],
            },
            "capture": {
                "stream_id": str(stream_id),
                "metadata_abi": 2,
                "center_frequency_hz": CENTER_FREQUENCY_HZ,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "tone_offset_hz": TONE_OFFSET_HZ,
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FRAME_COUNT,
                "sample_count": TOTAL_SAMPLES,
                "kernel_buffers": KERNEL_BUFFERS,
                "receiver_gain_db": RECEIVER_GAIN_DB,
                "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
                "dds_scale": DDS_SCALE,
                "continuity_passed": True,
                "rf_readback_passed": True,
            },
            "artifact": artifact_evidence,
            "condition_record_sha256": sha256_path(record_path),
            "leaf_source_sha256s": list(leaves),
            "leaf_source_set_sha256": leaf_source_set_sha256(leaves),
            "transfer": complex_detection_document(detection),
            "quality": {
                "passed": True,
                "rejection_reasons": [],
                "reference_tone_snr_db": analysis.rx1.tone_to_noise_snr_db,
                "adc_headroom_passed": True,
                "clipped_sample_count_by_receiver": clipped,
            },
            "safety": {
                "initial_exact_mute_passed": True,
                "selector_all_off_before_passed": True,
                "selector_all_off_after_passed": True,
                "selector_all_off_cleanup_passed": True,
                "final_exact_mute_passed": True,
                "persistence_after_final_mute_only": True,
                "automatic_retry_count": 0,
                "accepted_from_quarantine": False,
            },
            "topology_limitation": fixture.document["topology_limitation"],
        }
        validate_observation(observation, fixture=fixture)
        _write_immutable_json(observation_path, observation)
        capture_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root, capture_root)
        _fsync_directory(capture_root.parent)
    except BaseException as error:
        _quarantine_post_persistence_staging(contract, staging_root)
        raise ArmPreservingRunError(f"post-capture persistence failed: {error}") from error
    finally:
        retained.clear()
    return {
        "accepted_stream_count": 1,
        "stream_id": str(stream_id),
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "observation_path": str(observation_path),
        "observation_sha256": sha256_path(observation_path),
        "artifact": artifact_evidence,
        "target_image_admission": target_admission,
        "live_action_order": live_action_order,
        "live_safety_source_binding": source_binding,
        "final_mute": final_mute,
        "selector_all_off_cleanup": cleanup,
        "topology_limitation": fixture.document["topology_limitation"],
    }


def _execution_tombstone(
    path: Path, contract: Mapping[str, Any], plan_path: Path
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_arm_preserving_execution_started_tombstone",
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "created_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _failure_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    error: BaseException,
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_arm_preserving_failed_run_tombstone",
        "run_id": contract["run_id"],
        "condition_id": contract["condition_id"],
        "failed_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "error": _error_document(error),
        "accepted_artifact": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _execute_prepared(
    *,
    plan_path: Path,
    manifest_path: Path,
    expected_contract: Mapping[str, Any],
    execute_boundary: Callable[[Mapping[str, Any]], dict[str, Any]] = _execute_one_stream,
) -> dict[str, Any]:
    _require_local_storage_contract(expected_contract, condition_root=plan_path.parent)
    if _read_json(plan_path, "immutable plan") != _plan_envelope(expected_contract):
        raise ArmPreservingRunError("execution arguments/evidence differ from immutable plan")
    manifest = _read_json(manifest_path, "manifest")
    if manifest.get("status") != "prepared" or manifest.get("attempts") != []:
        raise ArmPreservingRunError("run is not a never-attempted prepared condition")
    execution_path = manifest_path.parent / EXECUTION_TOMBSTONE_FILENAME
    failure_path = manifest_path.parent / FAILURE_TOMBSTONE_FILENAME
    if any(path.exists() or path.is_symlink() for path in (execution_path, failure_path)):
        raise ArmPreservingRunError("run ID is already burned by a tombstone")
    execution = _execution_tombstone(execution_path, expected_contract, plan_path)
    attempt: dict[str, Any] = {
        "started_at": _now(),
        "status": "running",
        "execution_tombstone": {
            "path": str(execution_path),
            "sha256": sha256_path(execution_path),
            "document": execution,
        },
        "result": None,
        "error": None,
    }
    manifest["status"] = "running"
    manifest["attempts"] = [attempt]
    manifest["updated_at"] = _now()
    try:
        write_json_atomic(manifest_path, manifest)
        result = execute_boundary(expected_contract)
        if result.get("accepted_stream_count") != 1:
            raise ArmPreservingRunError("condition did not return exactly one accepted stream")
        attempt["status"] = "complete"
        attempt["completed_at"] = _now()
        attempt["result"] = result
        manifest["status"] = "complete"
        manifest["result"] = result
        manifest["error"] = None
        manifest["accepted_stream_count"] = 1
        manifest["updated_at"] = _now()
        write_json_atomic(manifest_path, manifest)
    except BaseException as error:
        attempt["status"] = "failed"
        attempt["completed_at"] = _now()
        attempt["error"] = _error_document(error)
        attempt["result"] = None
        manifest["status"] = "failed"
        manifest["result"] = None
        manifest["error"] = attempt["error"]
        manifest["accepted_stream_count"] = 0
        manifest["updated_at"] = _now()
        failure = _failure_tombstone(failure_path, expected_contract, plan_path, error)
        manifest["failure_tombstone"] = {
            "path": str(failure_path),
            "sha256": sha256_path(failure_path),
            "document": failure,
        }
        write_json_atomic(manifest_path, manifest)
        raise
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--repeat-index", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--setup-attestation", type=Path, required=True)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local/state/smateway",
        help="local Raspberry Pi state root; Pluto/removable storage is forbidden",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def _install_signal_handlers() -> None:
    def stop(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"received {signal.Signals(signum).name}")

    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, stop)


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    try:
        fixture_document = _read_json(args.fixture, "fixture")
        fixture = validate_fixture_v2(fixture_document)
        setup_document = _read_json(args.setup_attestation, "setup attestation")
        _, selector_control = _validate_sealed_selector_for_fixture(fixture)
        source = _repository_source_attestation()
        dependency = attest_pluto_plus_utils_source()
        native = attest_runtime()
        contract = _build_plan_contract(
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            role=args.role,
            arm=args.arm,
            repeat_index=args.repeat_index,
            fixture_document=fixture_document,
            fixture_file=_file_evidence(args.fixture, "fixture"),
            setup_document=setup_document,
            setup_file=_file_evidence(args.setup_attestation, "setup attestation"),
            selector_control=selector_control,
            source_attestation=source,
            dependency_attestation=dependency,
            native_attestation=native,
            state_root=args.state_root,
        )
        condition_root = Path(str(contract["storage"]["condition_root"]))
        plan_path = condition_root / PLAN_FILENAME
        manifest_path = condition_root / MANIFEST_FILENAME
        if args.plan_only:
            envelope, manifest = _prepare_plan(plan_path, manifest_path, contract)
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": manifest["status"],
                        "plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "condition_id": contract["condition_id"],
                        "topology_limitation": contract["fixture"]["document"][
                            "topology_limitation"
                        ],
                    }
                )
            )
            return 0
        manifest = _execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
        )
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "status": manifest["status"],
                    "accepted_stream_count": manifest["accepted_stream_count"],
                    "observation": manifest["result"]["observation_path"],
                    "closure_claim_permitted": False,
                }
            )
        )
        return 0
    except (
        OSError,
        ArmPreservingD2Error,
        ArmPreservingRunError,
        RuntimeError,
        ValueError,
    ) as error:
        print(json.dumps({"status": "failed", "error": _error_document(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
