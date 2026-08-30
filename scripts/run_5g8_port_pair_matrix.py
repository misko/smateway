#!/usr/bin/env python3
"""Plan or execute one protected 5.8-GHz TX/RX port-pair matrix repeat."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
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
from pluto_plus.artifacts import CaptureWriter, data_path, verify_artifact
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import SafeDdsTonePlan, SampleBlockV2, capture_continuous_safe_dds_tone
from pluto_plus.hardware.iio import find_usb_sysfs_path, resolve_iio_uri
from pluto_plus.models import GainMode, RadioSettings

from smateway.capture_continuity import validate_continuity_ledger
from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
)
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    canonical_json_sha256,
    sha256_path,
    write_json_atomic,
)
from smateway import global_ledger
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import (
    attestation_sha256,
    attest_runtime,
    validate_runtime_attestation,
)
from smateway.port_pair_matrix import (
    BANDWIDTH_HZ,
    CAPTURE_TX_GAIN_DB,
    CELL_IDS,
    CENTER_FREQUENCY_HZ,
    DDS_SCALE,
    NORMALIZED_OBSERVATION_KIND,
    PREFLIGHT_TX_GAIN_DB,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SOURCE_PEAK_OUTPUT_BOUND_DBM,
    TONE_OFFSET_HZ,
    TX_PORTS,
    HeadroomPreflight,
    PortPairMatrixError,
    canonical_sha256,
    evaluate_headroom_preflight,
    validate_calibration,
    validate_fixture,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
CONDITION_RECORD_FILENAME = "condition-record.json"
OBSERVATION_FILENAME = "normalized-observation.json"
RUN_KIND = "5g8_protected_tx_rx_port_pair_repeat"
IDENTITY_PURPOSE = "pre_preflight_identity"
IDENTITY_ATTESTATION = "resolve_iio_uri_exact_serial_usb_match"
FINAL_ACCEPTANCE_MUTE_PURPOSE = "final_acceptance_exact_mute"
FAILURE_CLEANUP_MUTE_PURPOSE = "failed_run_final_exact_mute"
FAILURE_CLEANUP_KIND = "5g8_port_pair_failure_cleanup_v1"
IDENTITY_EVIDENCE_KIND = "5g8_port_pair_initial_identity_v1"
MUTE_EVIDENCE_KIND = "5g8_port_pair_exact_mute_v1"
RUN_RESERVATION_KIND = "5g8_port_pair_permanent_run_reservation_v1"
EXECUTION_BURN_KIND = "5g8_port_pair_irreversible_execution_burn_v1"
GLOBAL_LEDGER_POLICY_ID = "t6-5g8-port-pair-matrix-v1"
EMERGENCY_FAILURE_KIND = "5g8_port_pair_emergency_failure_receipt_v1"
DEGRADED_AUTHORIZATION_KIND = "5g8_port_pair_degraded_execution_authorization_v1"
DEGRADED_CLEANUP_KIND = "5g8_port_pair_degraded_failure_cleanup_v1"
CAPTURE_TIMING_KIND = "5g8_port_pair_capture_timing_v1"
EXECUTION_SAFETY_KIND = "5g8_port_pair_execution_safety_v2"

PREFLIGHT_SAMPLES_PER_FRAME = 100_000
PREFLIGHT_FRAME_COUNT = 1
MAIN_SAMPLES_PER_FRAME = 100_000
MAIN_FRAME_COUNT = 3
KERNEL_BUFFERS = 8
ADC_CLIP_THRESHOLD_COUNTS = 2_047.0
MINIMUM_REFERENCE_SNR_DB = 20.0

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
SOURCE_FILES = (
    "src/smateway/capture_continuity.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/hexcal.py",
    "src/smateway/global_ledger.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/port_pair_matrix.py",
    "scripts/run_5g8_port_pair_matrix.py",
    "scripts/analyze_5g8_port_pair_matrix.py",
)


class PortPairRunError(RuntimeError):
    """The condition failed before two artifacts could be accepted."""


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as error:
        raise PortPairRunError(f"cannot read the kernel boot ID: {error}") from error
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) is None:
        raise PortPairRunError("kernel boot ID is malformed")
    return value


def _clock_stamp() -> dict[str, Any]:
    """Return one runner-owned wall/monotonic reading from the current boot."""

    return {
        "utc": _now(),
        "monotonic_ns": time.monotonic_ns(),
        "boot_id": _boot_id(),
    }


def _stamp_fields(stamp: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_at": stamp["utc"],
        f"{prefix}_monotonic_ns": stamp["monotonic_ns"],
        f"{prefix}_clock_boot_id": stamp["boot_id"],
    }


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, default=str))


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None:
        raise PortPairRunError(f"{label} is not a safe identifier")
    return value


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise PortPairRunError(f"{label} contains parent traversal")
    exact = expanded.absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise PortPairRunError(f"{label} path contains a symlink: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise PortPairRunError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PortPairRunError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise PortPairRunError(f"{label} must contain one JSON object")
    return value


def _file_evidence(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise PortPairRunError(f"{label} must be a regular non-symlink file")
    return {"path": str(exact), "sha256": sha256_path(exact), "size_bytes": exact.stat().st_size}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact.parent, "immutable output parent")
    exact.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert_local_rpi_storage(exact.parent, label="immutable output storage")
    except FileArtifactAdmissionError as error:
        raise PortPairRunError(str(error)) from error
    descriptor = os.open(
        exact,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    os.chmod(exact, 0o400)
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
        raise PortPairRunError("Smateway HEAD is not a full Git commit")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise PortPairRunError("Smateway must be clean before planning or execution")
    files = attest_source_files_at_commit(
        repository,
        expected_commit=head,
        relative_paths=SOURCE_FILES,
    )
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": head,
        "clean_worktree_verified": True,
        "files": files["files"],
        "source_files_sha256": canonical_json_sha256(files["files"]),
    }


def _safe_local_state_root(path: Path) -> Path:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "state root")
    try:
        assert_local_rpi_storage(exact, label="state root")
    except FileArtifactAdmissionError as error:
        raise PortPairRunError(str(error)) from error
    forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or root in exact.parents for root in forbidden):
        raise PortPairRunError("state root must be local RPi storage, not removable/Pluto storage")
    return exact


def _require_local_storage_contract(
    contract: Mapping[str, Any], *, condition_root: Path
) -> tuple[Path, Path]:
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise PortPairRunError("plan local-storage contract is missing")
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
        raise PortPairRunError("plan local-storage contract is malformed")
    exact_condition = Path(raw_condition).expanduser().absolute()
    exact_capture = Path(raw_capture).expanduser().absolute()
    if exact_condition != condition_root.expanduser().absolute():
        raise PortPairRunError("plan condition root differs from immutable plan location")
    try:
        assert_local_rpi_storage(exact_condition, label="condition storage")
        assert_local_rpi_storage(exact_capture, label="capture storage")
    except FileArtifactAdmissionError as error:
        raise PortPairRunError(str(error)) from error
    return exact_condition, exact_capture


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PortPairRunError(
            f"{label} keys differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _parse_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PortPairRunError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PortPairRunError(f"{label} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PortPairRunError(f"{label} must be timezone-aware UTC")
    return parsed


def _validate_ordered_timestamps(started: object, completed: object, label: str) -> None:
    if _parse_utc_timestamp(completed, f"{label} completed_at") < _parse_utc_timestamp(
        started, f"{label} started_at"
    ):
        raise PortPairRunError(f"{label} timestamps are reversed")


def _clock_point(
    value: Mapping[str, Any],
    *,
    prefix: str,
    label: str,
) -> tuple[datetime, int, str]:
    utc = _parse_utc_timestamp(value.get(f"{prefix}_at"), f"{label} {prefix}_at")
    monotonic = value.get(f"{prefix}_monotonic_ns")
    boot_id = value.get(f"{prefix}_clock_boot_id")
    if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < 0:
        raise PortPairRunError(f"{label} {prefix}_monotonic_ns must be a nonnegative integer")
    if (
        not isinstance(boot_id, str)
        or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id)
        is None
    ):
        raise PortPairRunError(f"{label} {prefix}_clock_boot_id is malformed")
    return utc, monotonic, boot_id


def _assert_clock_order(
    points: Sequence[tuple[datetime, int, str]],
    *,
    label: str,
) -> None:
    if not points:
        raise PortPairRunError(f"{label} has no clock points")
    boot_ids = {point[2] for point in points}
    if len(boot_ids) != 1:
        raise PortPairRunError(f"{label} crosses a kernel boot and cannot be ordered monotonically")
    utc_values = tuple(point[0] for point in points)
    monotonic_values = tuple(point[1] for point in points)
    if (
        tuple(sorted(utc_values)) != utc_values
        or tuple(sorted(monotonic_values)) != monotonic_values
    ):
        raise PortPairRunError(f"{label} UTC/monotonic events are not in the required order")


def _validate_clock_interval(value: Mapping[str, Any], *, label: str) -> None:
    _assert_clock_order(
        (
            _clock_point(value, prefix="started", label=label),
            _clock_point(value, prefix="completed", label=label),
        ),
        label=label,
    )


def _run_key(contract: Mapping[str, Any]) -> dict[str, Any]:
    condition = contract.get("condition")
    if not isinstance(condition, Mapping):
        raise PortPairRunError("port-pair run key lacks its condition")
    repeat = condition.get("repeat_index")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat not in range(1, 6):
        raise PortPairRunError("port-pair run key has an invalid repeat index")
    key = {
        "board_id": _validate_identifier(str(contract.get("board_id")), "board ID"),
        "campaign_id": _validate_identifier(str(contract.get("campaign_id")), "campaign ID"),
        "cell_id": str(condition.get("cell_id")),
        "repeat_index": repeat,
        "run_id": _validate_identifier(str(contract.get("run_id")), "run ID"),
    }
    if key["cell_id"] not in CELL_IDS:
        raise PortPairRunError("port-pair run key has an invalid cell")
    return key


def _board_root_and_exact_storage(contract: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise PortPairRunError("plan local-storage contract is missing")
    condition_root = Path(str(storage.get("condition_root", ""))).expanduser().absolute()
    capture_root = Path(str(storage.get("capture_root", ""))).expanduser().absolute()
    key = _run_key(contract)
    if len(condition_root.parents) < 5:
        raise PortPairRunError("condition root is too shallow for the canonical board layout")
    board_root = condition_root.parents[4]
    expected_condition = (
        board_root
        / "5g8-port-pair-matrix"
        / str(key["campaign_id"])
        / str(key["cell_id"])
        / f"repeat-{key['repeat_index']}"
        / str(key["run_id"])
    )
    expected_capture = (
        board_root
        / "pluto-usb-captures"
        / "5g8-port-pair-matrix"
        / str(key["campaign_id"])
        / str(key["cell_id"])
        / f"repeat-{key['repeat_index']}"
        / str(key["run_id"])
    )
    if (
        board_root.name != key["board_id"]
        or condition_root != expected_condition
        or capture_root != expected_capture
    ):
        raise PortPairRunError("planned storage differs from the canonical board/run layout")
    for path, label in (
        (board_root, "board state root"),
        (condition_root, "condition root"),
        (capture_root, "capture root"),
    ):
        _assert_no_symlink_chain(path, label)
        try:
            assert_local_rpi_storage(path, label=label)
        except FileArtifactAdmissionError as error:
            raise PortPairRunError(str(error)) from error
    return board_root, condition_root, capture_root


def _directory_identity(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_dir():
        raise PortPairRunError(f"{label} must be a regular non-symlink directory")
    try:
        status = exact.stat()
    except OSError as error:
        raise PortPairRunError(f"cannot attest {label}: {error}") from error
    return {"path": str(exact), "st_dev": int(status.st_dev), "st_ino": int(status.st_ino)}


def _global_ledger_authority(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    board_root, condition_root, _ = _board_root_and_exact_storage(contract)
    key = _run_key(contract)
    state_root = board_root.parents[1]
    namespace = {
        "schema": 1,
        "policy_id": GLOBAL_LEDGER_POLICY_ID,
        "namespace_kind": "5g8_port_pair_board_campaign_cell_repeat_run_id_v1",
        **key,
    }
    canonical_identity = {
        "schema": 1,
        **key,
        "plan_contract_sha256": canonical_sha256(contract),
        "run_root": str(condition_root),
        "plan_path": str(plan_path.expanduser().absolute()),
    }
    try:
        return global_ledger.authority_from_storage(
            policy_id=GLOBAL_LEDGER_POLICY_ID,
            namespace=namespace,
            canonical_identity=canonical_identity,
            state_root=state_root,
            backend=backend,
        )
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"shared global ledger authority failed: {error}") from error


def _ledger_paths(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    backend: global_ledger.LedgerBackend,
) -> tuple[Path, Path, Path, Path]:
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=backend)
    parent = Path(str(authority["ledger_directory_path"]))
    return (
        parent / global_ledger.RESERVATION_FILENAME,
        parent / global_ledger.BURN_GUARD_FILENAME,
        parent / global_ledger.BURN_MARKER_FILENAME,
        parent / global_ledger.FAILURE_RECEIPT_FILENAME,
    )


def _receipt(path: Path, document: Mapping[str, Any], kind: str) -> dict[str, Any]:
    observed = path.stat()
    return {
        "schema": 1,
        "evidence_kind": kind,
        "path": str(path.expanduser().absolute()),
        "st_dev": int(observed.st_dev),
        "st_ino": int(observed.st_ino),
        "sha256": sha256_path(path),
        "size_bytes": observed.st_size,
        "document": _json_safe(document),
    }


def _reservation_document(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
    prepared_manifest: Mapping[str, Any],
    global_authority: Mapping[str, Any],
    reservation_binding: Mapping[str, Any],
) -> dict[str, Any]:
    _, condition_root, capture_root = _board_root_and_exact_storage(contract)
    key = _run_key(contract)
    reserved = _clock_stamp()
    return {
        "schema": 1,
        "marker_kind": RUN_RESERVATION_KIND,
        "reservation_key": key,
        "reservation_key_sha256": canonical_sha256(key),
        "condition_root_identity": _directory_identity(condition_root, "reserved condition root"),
        "capture_root": str(capture_root),
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_file_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "prepared_manifest": _json_safe(prepared_manifest),
        "prepared_manifest_sha256": canonical_sha256(prepared_manifest),
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(global_authority),
        "global_reservation_binding": dict(reservation_binding),
        **_stamp_fields(reserved, "reserved"),
        "state": "permanently_reserved",
        "reservation_never_released": True,
        "replacement_or_replay_forbidden": True,
    }


def _validate_reservation_receipt(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
    receipt: object | None = None,
) -> dict[str, Any]:
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=ledger_backend)
    reservation_path, guard_path, _burn_path, failure_path = _ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    document = _read_json(reservation_path, "permanent run reservation")
    _exact_keys(
        document,
        {
            "schema",
            "marker_kind",
            "reservation_key",
            "reservation_key_sha256",
            "condition_root_identity",
            "capture_root",
            "plan_path",
            "plan_file_sha256",
            "plan_contract_sha256",
            "manifest_path",
            "prepared_manifest",
            "prepared_manifest_sha256",
            "shared_global_ledger_authority",
            "global_reservation_binding",
            "reserved_at",
            "reserved_monotonic_ns",
            "reserved_clock_boot_id",
            "state",
            "reservation_never_released",
            "replacement_or_replay_forbidden",
        },
        "permanent run reservation",
    )
    _, condition_root, capture_root = _board_root_and_exact_storage(contract)
    prepared_manifest = document.get("prepared_manifest")
    reservation_binding = document.get("global_reservation_binding")
    if not isinstance(reservation_binding, Mapping):
        raise PortPairRunError("permanent reservation lacks global slot binding")
    slots = reservation_binding.get("slots")
    anchors = reservation_binding.get("anchors")
    if not isinstance(slots, Mapping) or not isinstance(anchors, Mapping):
        raise PortPairRunError("permanent reservation global slots are malformed")
    try:
        global_ledger.validate_receipt_document_binding(document, authority=authority)
        current_reservation = global_ledger.file_evidence(
            reservation_path,
            label="T6 global reservation",
            expected_nlink=2,
        )
        current_guard = global_ledger.inode_identity(
            guard_path,
            directory=False,
            label="T6 global burn guard",
            expected_nlink=2,
        )
        current_failure = global_ledger.inode_identity(
            failure_path,
            directory=False,
            label="T6 global failure slot",
            expected_nlink=2,
        )
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"shared global reservation failed: {error}") from error
    if (
        document.get("schema") != 1
        or document.get("marker_kind") != RUN_RESERVATION_KIND
        or document.get("reservation_key") != _run_key(contract)
        or document.get("reservation_key_sha256") != canonical_sha256(_run_key(contract))
        or document.get("condition_root_identity")
        != _directory_identity(condition_root, "reserved condition root")
        or document.get("capture_root") != str(capture_root)
        or document.get("plan_path") != str(plan_path.expanduser().absolute())
        or document.get("plan_file_sha256") != sha256_path(plan_path)
        or document.get("plan_contract_sha256") != canonical_sha256(contract)
        or document.get("manifest_path") != str(manifest_path.expanduser().absolute())
        or not isinstance(prepared_manifest, Mapping)
        or document.get("prepared_manifest_sha256") != canonical_sha256(prepared_manifest)
        or document.get("shared_global_ledger_authority")
        != global_ledger.authority_receipt_binding(authority)
        or slots.get("reservation")
        != {key: current_reservation[key] for key in ("path", "st_dev", "st_ino")}
        or slots.get("burn-guard") != current_guard
        or slots.get("failure-receipt") != current_failure
        or document.get("state") != "permanently_reserved"
        or document.get("reservation_never_released") is not True
        or document.get("replacement_or_replay_forbidden") is not True
    ):
        raise PortPairRunError("permanent run reservation identity differs")
    _clock_point(document, prefix="reserved", label="reservation")
    expected_receipt = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_permanent_run_reservation_receipt_v2",
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
        "global_reservation_binding": dict(reservation_binding),
        **current_reservation,
        "document": _json_safe(document),
    }
    if receipt is not None and receipt != expected_receipt:
        raise PortPairRunError("permanent run reservation receipt binding differs")
    return expected_receipt


def _acquire_execution_burn(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
    reservation_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=ledger_backend)
    _reservation_path, _guard_path, burn_path, _failure_path = _ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    prepared_manifest = _read_json(manifest_path, "prepared manifest at execution burn")
    reservation_document = reservation_receipt.get("document")
    if not isinstance(
        reservation_document, Mapping
    ) or prepared_manifest != reservation_document.get("prepared_manifest"):
        raise PortPairRunError("prepared manifest differs from its permanent reservation")
    binding = reservation_receipt.get("global_reservation_binding")
    slots = binding.get("slots") if isinstance(binding, Mapping) else None
    if not isinstance(slots, Mapping) or not isinstance(slots.get("burn-guard"), Mapping):
        raise PortPairRunError("permanent reservation lacks its global burn guard")
    burned = _clock_stamp()
    attempt_point = _clock_point(attempt_started, prefix="started", label="attempt")
    reservation_point = _clock_point(reservation_document, prefix="reserved", label="reservation")
    burn_point = (
        _parse_utc_timestamp(burned["utc"], "execution burn burned_at"),
        int(burned["monotonic_ns"]),
        str(burned["boot_id"]),
    )
    _assert_clock_order(
        (reservation_point, attempt_point, burn_point),
        label="reservation→attempt start→execution burn",
    )
    execution_nonce = uuid.uuid4().hex
    expected_guard = {
        **dict(slots["burn-guard"]),
        "size_bytes": 1,
        "mode": global_ledger.SEALED_FILE_MODE,
        "nlink": 2,
        "sha256": hashlib.sha256(b"\x01").hexdigest(),
    }
    document = {
        "schema": 1,
        "marker_kind": EXECUTION_BURN_KIND,
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
        "reservation_key": _run_key(contract),
        "reservation_key_sha256": canonical_sha256(_run_key(contract)),
        "permanent_run_reservation": dict(reservation_receipt),
        "condition_root_identity": reservation_document["condition_root_identity"],
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_file_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "prepared_manifest_sha256": canonical_sha256(prepared_manifest),
        "consumed_burn_guard": expected_guard,
        "execution_nonce": execution_nonce,
        "attempt_started_at": attempt_started["started_at"],
        "attempt_started_monotonic_ns": attempt_started["started_monotonic_ns"],
        "attempt_started_clock_boot_id": attempt_started["started_clock_boot_id"],
        **_stamp_fields(burned, "burned"),
        "state": "irreversibly_burned",
        "hardware_access_authorized_once": True,
        "automatic_retry_forbidden": True,
    }
    burn_request = global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": slots["burn-guard"],
            "document": document,
        },
    )
    try:
        burn_response = global_ledger.validate_response(
            burn_request, ledger_backend.mutate(burn_request)
        )
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"cannot atomically burn the global run ID: {error}") from error
    evidence = burn_response["evidence"]
    marker_evidence = evidence.get("marker") if isinstance(evidence, Mapping) else None
    guard_evidence = evidence.get("guard") if isinstance(evidence, Mapping) else None
    if (
        not isinstance(marker_evidence, Mapping)
        or marker_evidence.get("path") != str(burn_path)
        or guard_evidence != expected_guard
        or evidence.get("execution_nonce") != execution_nonce
        or evidence.get("state") != "burn_complete"
    ):
        raise PortPairRunError("global atomic execution-burn response differs")
    return {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_irreversible_execution_burn_receipt_v2",
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
        "consumed_burn_guard": expected_guard,
        **dict(marker_evidence),
        "document": _json_safe(document),
    }


def _validate_execution_burn_receipt(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
    reservation_receipt: Mapping[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
    receipt: object | None = None,
) -> dict[str, Any]:
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=ledger_backend)
    _reservation_path, _guard_path, burn_path, _failure_path = _ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    document = _read_json(burn_path, "irreversible execution burn")
    _exact_keys(
        document,
        {
            "schema",
            "marker_kind",
            "shared_global_ledger_authority",
            "reservation_key",
            "reservation_key_sha256",
            "permanent_run_reservation",
            "condition_root_identity",
            "plan_path",
            "plan_file_sha256",
            "plan_contract_sha256",
            "manifest_path",
            "prepared_manifest_sha256",
            "consumed_burn_guard",
            "execution_nonce",
            "attempt_started_at",
            "attempt_started_monotonic_ns",
            "attempt_started_clock_boot_id",
            "burned_at",
            "burned_monotonic_ns",
            "burned_clock_boot_id",
            "state",
            "hardware_access_authorized_once",
            "automatic_retry_forbidden",
        },
        "irreversible execution burn",
    )
    reservation_document = reservation_receipt.get("document")
    try:
        execution_nonce = document.get("execution_nonce")
        global_ledger.validate_receipt_document(
            document,
            authority=authority,
            role="execution",
            execution_nonce=execution_nonce if isinstance(execution_nonce, str) else None,
        )
        marker_evidence = global_ledger.file_evidence(burn_path, label="T6 global execution marker")
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"shared global execution marker failed: {error}") from error
    if (
        not isinstance(reservation_document, Mapping)
        or document.get("schema") != 1
        or document.get("marker_kind") != EXECUTION_BURN_KIND
        or document.get("shared_global_ledger_authority")
        != global_ledger.authority_receipt_binding(authority)
        or document.get("reservation_key") != _run_key(contract)
        or document.get("reservation_key_sha256") != canonical_sha256(_run_key(contract))
        or document.get("permanent_run_reservation") != reservation_receipt
        or document.get("condition_root_identity")
        != reservation_document.get("condition_root_identity")
        or document.get("plan_path") != str(plan_path.expanduser().absolute())
        or document.get("plan_file_sha256") != sha256_path(plan_path)
        or document.get("plan_contract_sha256") != canonical_sha256(contract)
        or document.get("manifest_path") != str(manifest_path.expanduser().absolute())
        or document.get("prepared_manifest_sha256")
        != reservation_document.get("prepared_manifest_sha256")
        or not isinstance(document.get("execution_nonce"), str)
        or document.get("attempt_started_at") is None
        or document.get("attempt_started_monotonic_ns") is None
        or document.get("state") != "irreversibly_burned"
        or document.get("hardware_access_authorized_once") is not True
        or document.get("automatic_retry_forbidden") is not True
    ):
        raise PortPairRunError("irreversible execution burn identity differs")
    _assert_clock_order(
        (
            _clock_point(reservation_document, prefix="reserved", label="reservation"),
            _clock_point(
                {
                    "started_at": document.get("attempt_started_at"),
                    "started_monotonic_ns": document.get("attempt_started_monotonic_ns"),
                    "started_clock_boot_id": document.get("attempt_started_clock_boot_id"),
                },
                prefix="started",
                label="attempt",
            ),
            _clock_point(document, prefix="burned", label="execution burn"),
        ),
        label="reservation→attempt start→execution burn",
    )
    consumed_guard = document.get("consumed_burn_guard")
    if not isinstance(consumed_guard, Mapping):
        raise PortPairRunError("irreversible burn lacks its consumed guard evidence")
    expected_receipt = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_irreversible_execution_burn_receipt_v2",
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
        "consumed_burn_guard": dict(consumed_guard),
        **marker_evidence,
        "document": _json_safe(document),
    }
    if receipt is not None and receipt != expected_receipt:
        raise PortPairRunError("irreversible execution burn receipt binding differs")
    return expected_receipt


def _execution_authorization(
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = {
        "permanent_run_reservation": _json_safe(reservation_receipt),
        "irreversible_execution_burn": _json_safe(burn_receipt),
    }
    return {
        "evidence": evidence,
        "sha256": canonical_sha256(evidence),
    }


def _assert_run_unburned_before_hardware(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    """Reject every surviving artifact from a prior execution attempt.

    This gate runs before the execution tombstone authorizes hardware access.
    It therefore catches a rolled-back prepared manifest even when an attacker
    deleted the original tombstone but left capture, staging, quarantine, result,
    or other run-derived history behind.
    """

    condition_root, capture_root = _require_local_storage_contract(
        contract, condition_root=plan_path.parent
    )
    exact_plan = plan_path.expanduser().absolute()
    exact_manifest = manifest_path.expanduser().absolute()
    if exact_plan.parent != condition_root or exact_manifest.parent != condition_root:
        raise PortPairRunError("plan/manifest paths differ from the planned condition root")
    for path, label in (
        (condition_root, "condition root"),
        (exact_plan, "immutable plan"),
        (exact_manifest, "manifest"),
        (capture_root, "capture root"),
        (capture_root.parent, "capture parent"),
    ):
        _assert_no_symlink_chain(path, label)
        try:
            assert_local_rpi_storage(path, label=label)
        except FileArtifactAdmissionError as error:
            raise PortPairRunError(str(error)) from error
    if (
        exact_plan.is_symlink()
        or exact_manifest.is_symlink()
        or not exact_plan.is_file()
        or not exact_manifest.is_file()
    ):
        raise PortPairRunError("prepared plan/manifest must be regular non-symlink files")
    allowed_preparation = {PLAN_FILENAME, MANIFEST_FILENAME}
    surviving_condition_history = sorted(
        entry.name for entry in condition_root.iterdir() if entry.name not in allowed_preparation
    )
    staging_root = capture_root.parent / f".{capture_root.name}.staging"
    quarantine_root = capture_root.parent / ".failed" / capture_root.name
    for path, label in (
        (staging_root, "capture staging root"),
        (quarantine_root, "failed-capture quarantine root"),
    ):
        _assert_no_symlink_chain(path, label)
        try:
            assert_local_rpi_storage(path, label=label)
        except FileArtifactAdmissionError as error:
            raise PortPairRunError(str(error)) from error
    surviving_capture_history = [
        str(path)
        for path in (capture_root, staging_root, quarantine_root)
        if path.exists() or path.is_symlink()
    ]
    if surviving_condition_history or surviving_capture_history:
        details = surviving_condition_history + surviving_capture_history
        raise PortPairRunError(
            "run ID is already burned by surviving run-derived history: " + ", ".join(details)
        )
    reservation = _validate_reservation_receipt(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_backend=ledger_backend,
    )
    _reservation_path, guard_path, burn_path, failure_path = _ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    if burn_path.exists() or burn_path.is_symlink():
        raise PortPairRunError("external execution ledger proves this run ID is already burned")
    for path, label in (
        (guard_path, "global execution burn guard"),
        (failure_path, "global emergency failure slot"),
    ):
        observed = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 2
            or observed.st_size != 0
            or stat.S_IMODE(observed.st_mode) != global_ledger.PREPARED_SLOT_MODE
        ):
            raise PortPairRunError(f"{label} is already consumed or malformed")
    return reservation


def _build_plan_contract(
    *,
    run_id: str,
    campaign_id: str,
    board_id: str,
    serial: str,
    uri: str,
    cell_id: str,
    repeat_index: int,
    fixture_document: Mapping[str, Any],
    fixture_file: Mapping[str, Any],
    calibration_document: Mapping[str, Any],
    calibration_file: Mapping[str, Any],
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    native_attestation: Mapping[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    _validate_identifier(run_id, "run ID")
    _validate_identifier(campaign_id, "campaign ID")
    _validate_identifier(board_id, "board ID")
    _validate_identifier(serial, "Pluto serial")
    if USB_URI.fullmatch(uri) is None:
        raise PortPairRunError("capture requires an explicit current usb: URI")
    if cell_id not in CELL_IDS:
        raise PortPairRunError(f"cell must be one of {CELL_IDS}")
    if repeat_index not in range(1, 6):
        raise PortPairRunError("repeat index must be exactly 1..5")
    fixture = validate_fixture(fixture_document)
    calibration = validate_calibration(calibration_document, fixture)
    for evidence, label in (
        (fixture_file, "fixture"),
        (calibration_file, "calibration"),
    ):
        digest = evidence.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PortPairRunError(f"{label} file evidence lacks a SHA-256")
    cell = fixture.cell(cell_id)
    normalized_native = validate_runtime_attestation(native_attestation)
    source_commit = source_attestation.get("commit")
    dependency_commit = dependency_attestation.get("commit")
    if not isinstance(source_commit, str) or GIT_COMMIT.fullmatch(source_commit) is None:
        raise PortPairRunError("source attestation has no full Git commit")
    if not isinstance(dependency_commit, str) or GIT_COMMIT.fullmatch(dependency_commit) is None:
        raise PortPairRunError("dependency attestation has no full Git commit")
    exact_state = _safe_local_state_root(state_root)
    condition_root = (
        exact_state
        / "boards"
        / board_id
        / "5g8-port-pair-matrix"
        / campaign_id
        / cell_id
        / f"repeat-{repeat_index}"
        / run_id
    )
    capture_root = (
        exact_state
        / "boards"
        / board_id
        / "pluto-usb-captures"
        / "5g8-port-pair-matrix"
        / campaign_id
        / cell_id
        / f"repeat-{repeat_index}"
        / run_id
    )
    acquisition = {
        "center_frequency_hz": CENTER_FREQUENCY_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "tone_offset_hz": TONE_OFFSET_HZ,
        "receiver_gain_db": RECEIVER_GAIN_DB,
        "dds_scale": DDS_SCALE,
        "preflight": {
            "tx_gain_db": PREFLIGHT_TX_GAIN_DB,
            "samples_per_frame": PREFLIGHT_SAMPLES_PER_FRAME,
            "frame_count": PREFLIGHT_FRAME_COUNT,
        },
        "main": {
            "tx_gain_db": CAPTURE_TX_GAIN_DB,
            "samples_per_frame": MAIN_SAMPLES_PER_FRAME,
            "frame_count": MAIN_FRAME_COUNT,
        },
        "kernel_buffers": KERNEL_BUFFERS,
        "adc_clip_threshold_counts": ADC_CLIP_THRESHOLD_COUNTS,
        "minimum_reference_snr_db": MINIMUM_REFERENCE_SNR_DB,
    }
    native_sha256 = attestation_sha256(normalized_native)
    campaign_plan_contract = {
        "schema": 1,
        "plan_kind": "5g8_protected_tx_rx_port_pair_matrix_campaign",
        "campaign_id": campaign_id,
        "board_id": board_id,
        "configuration": {"serial": serial, "uri": uri},
        "fixture_sha256": fixture.fixture_sha256,
        "fixture_file_sha256": fixture_file["sha256"],
        "calibration_sha256": calibration.calibration_sha256,
        "calibration_file_sha256": calibration_file["sha256"],
        "source_commit": source_commit,
        "source_files_sha256": source_attestation.get("source_files_sha256"),
        "dependency_commit": dependency_commit,
        "dependency_attestation_sha256": canonical_sha256(dependency_attestation),
        "native_libiio_sha256": native_sha256,
        "acquisition": acquisition,
        "conditions": [
            {
                "cell_id": campaign_cell.cell_id,
                "repeat_index": campaign_repeat,
                "topology_sha256": campaign_cell.topology_sha256,
                "topology_token": campaign_cell.topology_token,
            }
            for campaign_cell in fixture.cells
            for campaign_repeat in range(1, 6)
        ],
        "condition_count": 20,
        "accepted_main_repeats_per_cell": 5,
        "preflight_streams_per_condition": 1,
        "main_streams_per_condition": 1,
        "rx1_protection_sha256": fixture.rx1_protection.identity_sha256,
        "rx2_reference_chain_sha256": fixture.rx2_reference_chain.identity_sha256,
        "local_rpi_only": True,
        "raw_channel_amplitude_comparison_forbidden": True,
    }
    campaign_plan = {
        "contract": campaign_plan_contract,
        "sha256": canonical_sha256(campaign_plan_contract),
    }
    return {
        "schema": 1,
        "run_kind": RUN_KIND,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "board_id": board_id,
        "campaign_plan": campaign_plan,
        "condition": {
            "cell_id": cell_id,
            "repeat_index": repeat_index,
            "active_tx": cell.active_tx,
            "inactive_tx": cell.inactive_tx,
            "test_receiver": cell.test_receiver,
            "reference_receiver": cell.reference_receiver,
            "topology_token": cell.topology_token,
            "topology_sha256": cell.topology_sha256,
            "topology_canonical_json": cell.topology_canonical_json,
        },
        "configuration": {"serial": serial, "uri": uri},
        "acquisition": acquisition,
        "fixture": {
            "file": _json_safe(fixture_file),
            "identity_sha256": fixture.fixture_sha256,
            "fixed_graph_sha256": fixture.fixed_graph_sha256,
            "receiver_input_limit_dbm": fixture.receiver_input_limit_dbm,
            "required_safety_margin_db": fixture.required_safety_margin_db,
            "rx1_protection_sha256": fixture.rx1_protection.identity_sha256,
            "rx2_reference_chain_sha256": fixture.rx2_reference_chain.identity_sha256,
            "inactive_tx_termination_sha256": cell.inactive_tx_termination_sha256,
            "test_receiver_termination_sha256": cell.test_receiver_termination_sha256,
            "reference_chain_sha256": cell.reference_chain_sha256,
            "active_tx_reference_plane_sha256": cell.active_tx_reference_plane_sha256,
            "test_receiver_reference_plane_sha256": (cell.test_receiver_reference_plane_sha256),
            "reference_receiver_reference_plane_sha256": (
                cell.reference_receiver_reference_plane_sha256
            ),
            "path_attenuation_before_reference_receiver_db": (
                cell.path_attenuation_before_reference_receiver_db
            ),
        },
        "calibration": {
            "file": _json_safe(calibration_file),
            "identity_sha256": calibration.calibration_sha256,
        },
        "source": {
            "smateway": _json_safe(source_attestation),
            "pluto_plus_utils": _json_safe(dependency_attestation),
            "native_libiio": normalized_native,
            "native_libiio_sha256": native_sha256,
        },
        "storage": {
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
            "condition_root": str(condition_root),
            "capture_root": str(capture_root),
        },
        "execution": {
            "preflight_before_main_required": True,
            "two_streams_per_repeat": True,
            "automatic_retry": False,
            "failed_run_id_burned": True,
            "artifact_persistence_only_after_exact_final_mute": True,
            "inactive_tx_physical_termination_and_digital_mute_required": True,
            "raw_channel_amplitude_comparison_forbidden": True,
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
        "campaign_id": contract["campaign_id"],
        "cell_id": contract["condition"]["cell_id"],
        "repeat_index": contract["condition"]["repeat_index"],
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
    *,
    ledger_backend: global_ledger.LedgerBackend,
) -> tuple[dict[str, Any], dict[str, Any]]:
    condition_root, capture_root = _require_local_storage_contract(
        contract, condition_root=plan_path.parent
    )
    envelope = _plan_envelope(contract)
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=ledger_backend)
    _reservation_path, _guard_path, burn_path, _failure_path = _ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    if (
        condition_root.exists()
        or condition_root.is_symlink()
        or capture_root.exists()
        or capture_root.is_symlink()
    ):
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
                raise PortPairRunError("existing run ID is not an intact matching prepared plan")
            if capture_root.exists():
                raise PortPairRunError("prepared run unexpectedly has capture history")
            _validate_reservation_receipt(
                contract,
                plan_path=plan_path,
                manifest_path=manifest_path,
                ledger_backend=ledger_backend,
            )
            if burn_path.exists() or burn_path.is_symlink():
                raise PortPairRunError("prepared run ID is already irreversibly burned")
            return observed, manifest
        raise PortPairRunError("run ID has prior plan, execution, tombstone, or captures")
    if Path(str(authority["ledger_directory_path"])).exists():
        raise PortPairRunError("run ID already has an external reservation or burn record")
    _write_immutable_json(plan_path, envelope)
    manifest = _new_manifest(plan_path, envelope)
    write_json_atomic(manifest_path, manifest)
    reserve_request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": uuid.uuid4().hex},
    )
    try:
        reserve_response = global_ledger.validate_response(
            reserve_request, ledger_backend.mutate(reserve_request)
        )
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"cannot reserve the global T6 run ID: {error}") from error
    reservation_binding = reserve_response["evidence"]
    if not isinstance(reservation_binding, Mapping):
        raise PortPairRunError("global T6 reservation response is malformed")
    reservation = _reservation_document(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        prepared_manifest=manifest,
        global_authority=authority,
        reservation_binding=reservation_binding,
    )
    slots = reservation_binding.get("slots")
    if not isinstance(slots, Mapping) or not isinstance(slots.get("reservation"), Mapping):
        raise PortPairRunError("global T6 reservation lacks its sealed slot")
    seal_request = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "expected_identity": slots["reservation"],
            "document": reservation,
        },
    )
    try:
        global_ledger.validate_response(seal_request, ledger_backend.mutate(seal_request))
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"cannot seal the global T6 reservation: {error}") from error
    _validate_reservation_receipt(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_backend=ledger_backend,
    )
    return envelope, manifest


def _validate_execution_confirmations(
    args: argparse.Namespace, contract: Mapping[str, Any]
) -> dict[str, Any]:
    flags = {
        "no_antennas": args.confirm_no_antennas,
        "inactive_tx_physically_terminated": args.confirm_inactive_tx_physically_terminated,
        "test_receiver_terminated": args.confirm_test_receiver_terminated,
        "rx1_protection_unchanged": args.confirm_rx1_protection_unchanged,
        "separate_reference_attenuator": args.confirm_separate_reference_attenuator,
        "reference_planes_match": args.confirm_reference_planes_match,
        "no_movement": args.confirm_no_movement,
    }
    missing = [name for name, passed in flags.items() if not passed]
    if missing:
        raise PortPairRunError("missing execution confirmations: " + ", ".join(missing))
    expected_token = contract["condition"]["topology_token"]
    if args.confirm_topology_token != expected_token:
        raise PortPairRunError(f"execution requires --confirm-topology-token {expected_token}")
    return {"confirmed_at": _now(), "topology_token": expected_token, **flags}


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
        "error": None if resolved == uri else {"type": "IdentityMismatch", "message": "URI"},
    }


def _validate_error(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PortPairRunError(f"{label} must be an error object")
    _exact_keys(value, {"type", "message"}, label)
    if not isinstance(value.get("type"), str) or not isinstance(value.get("message"), str):
        raise PortPairRunError(f"{label} type/message must be strings")
    return {"type": str(value["type"]), "message": str(value["message"])}


def _normalize_usb_sysfs_identity(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PortPairRunError("USB sysfs identity path must be a string")
    path = Path(value)
    root = Path("/sys/bus/usb/devices")
    if (
        not path.is_absolute()
        or path.parent != root
        or re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", path.name) is None
    ):
        raise PortPairRunError("USB sysfs identity is not one normalized USB device path")
    return {"root": str(root), "device_name": path.name, "path": str(path)}


def _authorization_digest(
    reservation_receipt: Mapping[str, Any], burn_receipt: Mapping[str, Any]
) -> str:
    return str(_execution_authorization(reservation_receipt, burn_receipt)["sha256"])


def _execution_marker_digest(execution_marker_receipt: Mapping[str, Any]) -> str:
    return canonical_sha256(execution_marker_receipt)


def _execution_marker_file_sha256(execution_marker_receipt: Mapping[str, Any]) -> str:
    value = execution_marker_receipt.get("sha256")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PortPairRunError("execution tombstone receipt lacks an exact file SHA-256")
    return value


def _call_identity(
    boundary: IdentityBoundary,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    configuration = contract["configuration"]
    assert isinstance(configuration, Mapping)
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    started = _clock_stamp()
    try:
        raw = boundary(serial, uri)
        if not isinstance(raw, Mapping):
            raise PortPairRunError("identity boundary returned non-object evidence")
        normalized_sysfs = _normalize_usb_sysfs_identity(raw.get("sysfs_path"))
        raw_error = raw.get("error")
        error = None if raw_error is None else _validate_error(raw_error, "identity error")
        status = raw.get("status")
        if status not in {"passed", "failed"}:
            raise PortPairRunError("identity boundary status is invalid")
        completed = _clock_stamp()
        evidence = {
            "schema": 1,
            "evidence_kind": IDENTITY_EVIDENCE_KIND,
            "status": status,
            "purpose": IDENTITY_PURPOSE,
            "run_id": contract["run_id"],
            "plan_contract_sha256": canonical_sha256(contract),
            "execution_authorization_sha256": _authorization_digest(
                reservation_receipt, burn_receipt
            ),
            "execution_tombstone_receipt_sha256": _execution_marker_digest(
                execution_marker_receipt
            ),
            "execution_tombstone_file_sha256": _execution_marker_file_sha256(
                execution_marker_receipt
            ),
            "serial": raw.get("serial"),
            "requested_uri": raw.get("requested_uri"),
            "resolved_uri": raw.get("resolved_uri"),
            "exact_uri_match": raw.get("exact_uri_match"),
            "usb_sysfs_identity": normalized_sysfs,
            "attestation": IDENTITY_ATTESTATION,
            "scan_mutates_radio_state": raw.get("scan_mutates_radio_state"),
            **_stamp_fields(started, "started"),
            **_stamp_fields(completed, "completed"),
            "error": error,
        }
    except BaseException as error:
        completed = _clock_stamp()
        evidence = {
            "schema": 1,
            "evidence_kind": IDENTITY_EVIDENCE_KIND,
            "status": "failed",
            "purpose": IDENTITY_PURPOSE,
            "run_id": contract["run_id"],
            "plan_contract_sha256": canonical_sha256(contract),
            "execution_authorization_sha256": _authorization_digest(
                reservation_receipt, burn_receipt
            ),
            "execution_tombstone_receipt_sha256": _execution_marker_digest(
                execution_marker_receipt
            ),
            "execution_tombstone_file_sha256": _execution_marker_file_sha256(
                execution_marker_receipt
            ),
            "serial": serial,
            "requested_uri": uri,
            "resolved_uri": None,
            "exact_uri_match": False,
            "usb_sysfs_identity": None,
            "attestation": IDENTITY_ATTESTATION,
            "scan_mutates_radio_state": False,
            **_stamp_fields(started, "started"),
            **_stamp_fields(completed, "completed"),
            "error": _error_document(error),
        }
    return evidence


def _validate_identity_evidence(
    value: object,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PortPairRunError("identity evidence must be an object")
    _exact_keys(
        value,
        {
            "schema",
            "evidence_kind",
            "status",
            "purpose",
            "run_id",
            "plan_contract_sha256",
            "execution_authorization_sha256",
            "execution_tombstone_receipt_sha256",
            "execution_tombstone_file_sha256",
            "serial",
            "requested_uri",
            "resolved_uri",
            "exact_uri_match",
            "usb_sysfs_identity",
            "attestation",
            "scan_mutates_radio_state",
            "started_at",
            "started_monotonic_ns",
            "started_clock_boot_id",
            "completed_at",
            "completed_monotonic_ns",
            "completed_clock_boot_id",
            "error",
        },
        "identity evidence",
    )
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PortPairRunError("identity evidence lacks planned configuration")
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    _validate_clock_interval(value, label="identity")
    sysfs = value.get("usb_sysfs_identity")
    normalized_sysfs = None
    if isinstance(sysfs, Mapping):
        _exact_keys(sysfs, {"root", "device_name", "path"}, "USB sysfs identity")
        normalized_sysfs = _normalize_usb_sysfs_identity(sysfs.get("path"))
        if dict(sysfs) != normalized_sysfs:
            raise PortPairRunError("USB sysfs identity is not canonical")
    status = value.get("status")
    error = value.get("error")
    common_valid = (
        value.get("schema") == 1
        and value.get("evidence_kind") == IDENTITY_EVIDENCE_KIND
        and value.get("purpose") == IDENTITY_PURPOSE
        and value.get("run_id") == contract.get("run_id")
        and value.get("plan_contract_sha256") == canonical_sha256(contract)
        and value.get("execution_authorization_sha256")
        == _authorization_digest(reservation_receipt, burn_receipt)
        and value.get("execution_tombstone_receipt_sha256")
        == _execution_marker_digest(execution_marker_receipt)
        and value.get("execution_tombstone_file_sha256")
        == _execution_marker_file_sha256(execution_marker_receipt)
        and value.get("serial") == serial
        and value.get("requested_uri") == uri
        and value.get("attestation") == IDENTITY_ATTESTATION
        and value.get("scan_mutates_radio_state") is False
    )
    if status == "passed":
        semantic_valid = (
            value.get("resolved_uri") == uri
            and value.get("exact_uri_match") is True
            and normalized_sysfs is not None
            and error is None
        )
    elif status == "failed":
        _validate_error(error, "identity failure error")
        semantic_valid = value.get("exact_uri_match") is False
    else:
        semantic_valid = False
    if not common_valid or not semantic_valid:
        raise PortPairRunError("identity evidence semantics differ from the immutable run")
    return dict(value)


def _identity_passed(
    value: object,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> bool:
    try:
        normalized = _validate_identity_evidence(
            value,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
        )
    except PortPairRunError:
        return False
    status = normalized.get("status")
    return isinstance(status, str) and status == "passed"


def _call_mute(
    boundary: MuteBoundary,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    """Invoke exact mute and wrap it in a schema-closed run-bound artifact."""

    configuration = contract["configuration"]
    assert isinstance(configuration, Mapping)
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    started = _clock_stamp()
    try:
        raw = boundary(serial, purpose)
        if not isinstance(raw, Mapping):
            raise PortPairRunError("mute boundary returned non-object evidence")
        raw_error = raw.get("error")
        error = None if raw_error is None else _validate_error(raw_error, "mute error")
        status = raw.get("status")
        if status not in {"passed", "failed"}:
            raise PortPairRunError("mute boundary status is invalid")
        gains = raw.get("tx_gain_readback_db_by_channel")
        scales = raw.get("dds_scale_readback")
        if status == "failed":
            gains = None
            scales = None
    except BaseException as error_value:
        status = "failed"
        gains = None
        scales = None
        error = _error_document(error_value)
    completed = _clock_stamp()
    return {
        "schema": 1,
        "evidence_kind": MUTE_EVIDENCE_KIND,
        "status": status,
        "purpose": purpose,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "execution_authorization_sha256": _authorization_digest(reservation_receipt, burn_receipt),
        "execution_tombstone_receipt_sha256": _execution_marker_digest(execution_marker_receipt),
        "execution_tombstone_file_sha256": _execution_marker_file_sha256(execution_marker_receipt),
        "serial": serial,
        "uri": uri,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": _json_safe(gains),
        "dds_scale_readback": _json_safe(scales),
        **_stamp_fields(started, "started"),
        **_stamp_fields(completed, "completed"),
        "error": error,
    }


def _validate_mute_evidence(
    value: object,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PortPairRunError("mute evidence must be an object")
    _exact_keys(
        value,
        {
            "schema",
            "evidence_kind",
            "status",
            "purpose",
            "run_id",
            "plan_contract_sha256",
            "execution_authorization_sha256",
            "execution_tombstone_receipt_sha256",
            "execution_tombstone_file_sha256",
            "serial",
            "uri",
            "attestation",
            "tx_gain_readback_db_by_channel",
            "dds_scale_readback",
            "started_at",
            "started_monotonic_ns",
            "started_clock_boot_id",
            "completed_at",
            "completed_monotonic_ns",
            "completed_clock_boot_id",
            "error",
        },
        "mute evidence",
    )
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PortPairRunError("mute evidence lacks planned configuration")
    _validate_clock_interval(value, label="mute")
    common_valid = (
        value.get("schema") == 1
        and value.get("evidence_kind") == MUTE_EVIDENCE_KIND
        and value.get("purpose") == purpose
        and value.get("run_id") == contract.get("run_id")
        and value.get("plan_contract_sha256") == canonical_sha256(contract)
        and value.get("execution_authorization_sha256")
        == _authorization_digest(reservation_receipt, burn_receipt)
        and value.get("execution_tombstone_receipt_sha256")
        == _execution_marker_digest(execution_marker_receipt)
        and value.get("execution_tombstone_file_sha256")
        == _execution_marker_file_sha256(execution_marker_receipt)
        and value.get("serial") == configuration.get("serial")
        and value.get("uri") == configuration.get("uri")
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
    )
    gains = value.get("tx_gain_readback_db_by_channel")
    scales = value.get("dds_scale_readback")
    status = value.get("status")
    error = value.get("error")
    if status == "passed":
        typed_gains = (
            isinstance(gains, list)
            and len(gains) == 2
            and all(type(item) is float for item in gains)
        )
        typed_scales = (
            isinstance(scales, list)
            and len(scales) == 8
            and all(type(item) is float for item in scales)
        )
        semantic_valid = (
            typed_gains
            and typed_scales
            and gains == [-80.0, -80.0]
            and scales == [0.0] * 8
            and error is None
        )
    elif status == "failed":
        _validate_error(error, "mute failure error")
        semantic_valid = gains is None and scales is None
    else:
        semantic_valid = False
    if not common_valid or not semantic_valid:
        raise PortPairRunError("mute evidence semantics differ from the immutable run")
    return dict(value)


def _mute_passed(
    value: object,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    purpose: str,
) -> bool:
    try:
        normalized = _validate_mute_evidence(
            value,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose=purpose,
        )
    except PortPairRunError:
        return False
    status = normalized.get("status")
    return isinstance(status, str) and status == "passed"


def _capture_timing(
    *,
    purpose: str,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    started: Mapping[str, Any],
    completed: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": CAPTURE_TIMING_KIND,
        "purpose": purpose,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "execution_authorization_sha256": _authorization_digest(reservation_receipt, burn_receipt),
        "execution_tombstone_receipt_sha256": _execution_marker_digest(execution_marker_receipt),
        "execution_tombstone_file_sha256": _execution_marker_file_sha256(execution_marker_receipt),
        "serial": contract["configuration"]["serial"],
        "uri": contract["configuration"]["uri"],
        **_stamp_fields(started, "started"),
        **_stamp_fields(completed, "completed"),
    }


def _validate_capture_timing(
    value: object,
    *,
    purpose: str,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PortPairRunError(f"{purpose} timing must be an object")
    _exact_keys(
        value,
        {
            "schema",
            "evidence_kind",
            "purpose",
            "run_id",
            "plan_contract_sha256",
            "execution_authorization_sha256",
            "execution_tombstone_receipt_sha256",
            "execution_tombstone_file_sha256",
            "serial",
            "uri",
            "started_at",
            "started_monotonic_ns",
            "started_clock_boot_id",
            "completed_at",
            "completed_monotonic_ns",
            "completed_clock_boot_id",
        },
        f"{purpose} timing",
    )
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PortPairRunError("capture timing lacks planned configuration")
    _validate_clock_interval(value, label=f"{purpose} timing")
    if (
        value.get("schema") != 1
        or value.get("evidence_kind") != CAPTURE_TIMING_KIND
        or value.get("purpose") != purpose
        or value.get("run_id") != contract.get("run_id")
        or value.get("plan_contract_sha256") != canonical_sha256(contract)
        or value.get("execution_authorization_sha256")
        != _authorization_digest(reservation_receipt, burn_receipt)
        or value.get("execution_tombstone_receipt_sha256")
        != _execution_marker_digest(execution_marker_receipt)
        or value.get("execution_tombstone_file_sha256")
        != _execution_marker_file_sha256(execution_marker_receipt)
        or value.get("serial") != configuration.get("serial")
        or value.get("uri") != configuration.get("uri")
    ):
        raise PortPairRunError(f"{purpose} timing differs from the immutable run")
    return dict(value)


def _validated_execution_safety(
    *,
    identity: object,
    initial_mute: object,
    final_mute: object,
    capture_timeline: object,
    attempt_started: Mapping[str, Any],
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute exact predicates and the complete pre-completion event order."""

    normalized_identity = _validate_identity_evidence(
        identity,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )
    if normalized_identity["status"] != "passed":
        raise PortPairRunError("initial identity safety evidence is not exact and passing")
    normalized_initial = _validate_mute_evidence(
        initial_mute,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose="pre_preflight_exact_mute",
    )
    if normalized_initial["status"] != "passed":
        raise PortPairRunError("initial exact-mute safety evidence is not exact and passing")
    normalized_final = _validate_mute_evidence(
        final_mute,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose=FINAL_ACCEPTANCE_MUTE_PURPOSE,
    )
    if normalized_final["status"] != "passed":
        raise PortPairRunError("final exact-mute safety evidence is not exact and passing")
    if not isinstance(capture_timeline, Mapping):
        raise PortPairRunError("execution safety lacks its capture/mute timeline")
    _exact_keys(
        capture_timeline,
        {
            "schema",
            "evidence_kind",
            "preflight_capture",
            "post_preflight_mute",
            "main_capture",
            "post_main_mute",
        },
        "capture/mute timeline",
    )
    if (
        capture_timeline.get("schema") != 1
        or capture_timeline.get("evidence_kind") != "5g8_port_pair_capture_mute_timeline_v1"
    ):
        raise PortPairRunError("capture/mute timeline kind is invalid")
    preflight_capture = _validate_capture_timing(
        capture_timeline.get("preflight_capture"),
        purpose="preflight_capture",
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )
    post_preflight = _validate_mute_evidence(
        capture_timeline.get("post_preflight_mute"),
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose="post_preflight_exact_mute",
    )
    main_capture = _validate_capture_timing(
        capture_timeline.get("main_capture"),
        purpose="main_capture",
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )
    post_main = _validate_mute_evidence(
        capture_timeline.get("post_main_mute"),
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose="post_main_exact_mute",
    )
    if post_preflight.get("status") != "passed" or post_main.get("status") != "passed":
        raise PortPairRunError("inter-capture exact-mute evidence is not passing")
    reservation_document = reservation_receipt.get("document")
    burn_document = burn_receipt.get("document")
    marker_document = execution_marker_receipt.get("document")
    if not all(
        isinstance(item, Mapping) for item in (reservation_document, burn_document, marker_document)
    ):
        raise PortPairRunError("execution safety lacks immutable authorization documents")
    assert isinstance(reservation_document, Mapping)
    assert isinstance(burn_document, Mapping)
    assert isinstance(marker_document, Mapping)
    if (
        burn_document.get("attempt_started_at") != attempt_started.get("started_at")
        or burn_document.get("attempt_started_monotonic_ns")
        != attempt_started.get("started_monotonic_ns")
        or burn_document.get("attempt_started_clock_boot_id")
        != attempt_started.get("started_clock_boot_id")
    ):
        raise PortPairRunError("execution burn does not bind the exact attempt start")
    _assert_clock_order(
        (
            _clock_point(reservation_document, prefix="reserved", label="reservation"),
            _clock_point(attempt_started, prefix="started", label="attempt"),
            _clock_point(burn_document, prefix="burned", label="execution burn"),
            _clock_point(marker_document, prefix="created", label="execution tombstone"),
            _clock_point(normalized_identity, prefix="started", label="identity"),
            _clock_point(normalized_identity, prefix="completed", label="identity"),
            _clock_point(normalized_initial, prefix="started", label="initial mute"),
            _clock_point(normalized_initial, prefix="completed", label="initial mute"),
            _clock_point(preflight_capture, prefix="started", label="preflight capture"),
            _clock_point(preflight_capture, prefix="completed", label="preflight capture"),
            _clock_point(post_preflight, prefix="started", label="post-preflight mute"),
            _clock_point(post_preflight, prefix="completed", label="post-preflight mute"),
            _clock_point(main_capture, prefix="started", label="main capture"),
            _clock_point(main_capture, prefix="completed", label="main capture"),
            _clock_point(post_main, prefix="started", label="post-main mute"),
            _clock_point(post_main, prefix="completed", label="post-main mute"),
            _clock_point(normalized_final, prefix="started", label="final mute"),
            _clock_point(normalized_final, prefix="completed", label="final mute"),
        ),
        label=(
            "reservation→attempt start→burn→execution marker→identity→initial mute→"
            "captures/inter-capture mutes→final mute"
        ),
    )
    normalized_capture_timeline = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_capture_mute_timeline_v1",
        "preflight_capture": preflight_capture,
        "post_preflight_mute": post_preflight,
        "main_capture": main_capture,
        "post_main_mute": post_main,
    }
    evidence = {
        "attempt_started": dict(attempt_started),
        "execution_tombstone": _json_safe(execution_marker_receipt),
        "identity_preflight": normalized_identity,
        "initial_mute": normalized_initial,
        "capture_timeline": normalized_capture_timeline,
        "final_mute": normalized_final,
        "permanent_run_reservation": _json_safe(reservation_receipt),
        "irreversible_execution_burn": _json_safe(burn_receipt),
    }
    return {
        "schema": 1,
        "evidence_kind": EXECUTION_SAFETY_KIND,
        "evidence": evidence,
        "evidence_sha256": canonical_sha256(evidence),
        "identity_preflight_sha256": canonical_sha256(normalized_identity),
        "initial_mute_sha256": canonical_sha256(normalized_initial),
        "capture_timeline_sha256": canonical_sha256(normalized_capture_timeline),
        "final_mute_sha256": canonical_sha256(normalized_final),
        "execution_tombstone_receipt_sha256": _execution_marker_digest(execution_marker_receipt),
        "all_predicates_recomputed_and_passed": True,
    }


def _validated_failure_cleanup(
    value: object,
    *,
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive cleanup status only from exact mute readback evidence."""

    exact_mute = _validate_mute_evidence(
        value,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose=FAILURE_CLEANUP_MUTE_PURPOSE,
    )
    burn_document = burn_receipt.get("document")
    if not isinstance(burn_document, Mapping):
        raise PortPairRunError("failure cleanup lacks the irreversible burn document")
    _assert_clock_order(
        (
            _clock_point(burn_document, prefix="burned", label="execution burn"),
            _clock_point(exact_mute, prefix="started", label="failure cleanup"),
            _clock_point(exact_mute, prefix="completed", label="failure cleanup"),
        ),
        label="execution burn→failure cleanup",
    )
    if exact_mute.get("execution_tombstone_receipt_sha256") != _execution_marker_digest(
        execution_marker_receipt
    ):
        raise PortPairRunError("failure cleanup predates the irreversible execution burn")
    return {
        "schema": 1,
        "evidence_kind": FAILURE_CLEANUP_KIND,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "execution_authorization_sha256": _authorization_digest(reservation_receipt, burn_receipt),
        "purpose": FAILURE_CLEANUP_MUTE_PURPOSE,
        "exact_mute": exact_mute,
        "exact_mute_passed": _mute_passed(
            exact_mute,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose=FAILURE_CLEANUP_MUTE_PURPOSE,
        ),
        "mandatory_final_cleanup_attempted": True,
    }


def _validate_completed_attempt_timeline(
    *,
    attempt: Mapping[str, Any],
    result: Mapping[str, Any],
    contract: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    attempt_started = {
        "started_at": attempt.get("started_at"),
        "started_monotonic_ns": attempt.get("started_monotonic_ns"),
        "started_clock_boot_id": attempt.get("started_clock_boot_id"),
    }
    attempt_completed = {
        "completed_at": attempt.get("completed_at"),
        "completed_monotonic_ns": attempt.get("completed_monotonic_ns"),
        "completed_clock_boot_id": attempt.get("completed_clock_boot_id"),
    }
    safety = _validated_execution_safety(
        identity=result.get("identity_preflight"),
        initial_mute=result.get("initial_mute"),
        final_mute=result.get("final_mute"),
        capture_timeline=result.get("capture_timeline"),
        attempt_started=attempt_started,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )
    final_mute = result.get("final_mute")
    if not isinstance(final_mute, Mapping):
        raise PortPairRunError("completed attempt lacks final exact-mute evidence")
    _assert_clock_order(
        (
            _clock_point(final_mute, prefix="completed", label="final mute"),
            _clock_point(attempt_completed, prefix="completed", label="attempt"),
        ),
        label="final mute→attempt completion",
    )
    if (
        result.get("execution_tombstone") != execution_marker_receipt
        or result.get("execution_tombstone_receipt_sha256")
        != safety["execution_tombstone_receipt_sha256"]
        or result.get("execution_safety_sha256") != safety["evidence_sha256"]
        or result.get("identity_preflight_sha256") != safety["identity_preflight_sha256"]
        or result.get("initial_mute_sha256") != safety["initial_mute_sha256"]
        or result.get("capture_timeline_sha256") != safety["capture_timeline_sha256"]
        or result.get("final_mute_sha256") != safety["final_mute_sha256"]
    ):
        raise PortPairRunError("completed attempt safety/timeline bindings differ")
    return safety


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


def _tone_plan(contract: Mapping[str, Any], tx_gain_db: float) -> SafeDdsTonePlan:
    configuration = contract["configuration"]
    condition = contract["condition"]
    return SafeDdsTonePlan(
        uri=str(configuration["uri"]),
        serial=str(configuration["serial"]),
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=TX_PORTS.index(str(condition["active_tx"])),
        tx_hardware_gain_db=tx_gain_db,
        dds_scale=DDS_SCALE,
        receiver_gain_db=RECEIVER_GAIN_DB,
        source_peak_output_bound_dbm=SOURCE_PEAK_OUTPUT_BOUND_DBM,
        load_input_limit_dbm=float(contract["fixture"]["receiver_input_limit_dbm"]),
        path_attenuation_before_load_db=float(
            contract["fixture"]["path_attenuation_before_reference_receiver_db"]
        ),
        required_margin_db=float(contract["fixture"]["required_safety_margin_db"]),
        settle_ms=100,
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


def _block_ledger(blocks: Sequence[SampleBlockV2]) -> dict[str, Any]:
    records = []
    sample_start = 0
    for block in blocks:
        records.append(
            {
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
        )
        sample_start += block.sample_count
    if not records:
        raise PortPairRunError("capture returned no ABI2 blocks")
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
    samples_per_frame: int,
    frame_count: int,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
        raise PortPairRunError("capture identity differs from exact serial/current USB URI")
    if capture.settings != _settings():
        raise PortPairRunError("capture settings differ from the immutable matrix plan")
    expected_samples = samples_per_frame * frame_count
    if capture.sample_count != expected_samples or len(capture.frames) != frame_count:
        raise PortPairRunError("capture sample/frame count differs from the plan")
    if capture.kernel_buffers != KERNEL_BUFFERS or len(blocks) != frame_count:
        raise PortPairRunError("capture kernel-buffer or retained-frame count differs")
    if any(block.samples.shape != (2, samples_per_frame) for block in blocks):
        raise PortPairRunError("capture block does not contain exact paired RX samples")
    ledger = _block_ledger(blocks)
    continuity = validate_continuity_ledger(
        ledger,
        expected_total_samples=expected_samples,
        expected_samples_per_block=samples_per_frame,
    )
    if continuity.metadata_abi != 2 or continuity.first_buffer_sequence != 0:
        raise PortPairRunError("capture did not start one fresh continuous ABI2 stream")
    active_indices = {plan.tx_channel * 4, plan.tx_channel * 4 + 2}
    scales = tuple(float(value) for value in capture.dds_scale_readback)
    if len(scales) != 8:
        raise PortPairRunError("DDS readback is not the canonical eight-value layout")
    for index, value in enumerate(scales):
        expected = DDS_SCALE if index in active_indices else 0.0
        if abs(abs(value) - expected) > 1e-6:
            raise PortPairRunError("DDS readback does not prove exactly one active TX")
    gains = [-80.0, -80.0]
    gains[plan.tx_channel] = float(capture.tx_gain_readback_db)
    return (
        continuity.stream_id,
        ledger,
        {
            "tx_gain_readback_db_by_channel": gains,
            "dds_scale_readback": list(scales),
            "dds_enabled_readback": list(capture.dds_enabled_readback),
            "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
            "inactive_tx_internal_exact_mute_proven_by_capture_helper": True,
        },
    )


def _headroom(blocks: Sequence[SampleBlockV2]) -> tuple[tuple[float, float], tuple[int, int]]:
    peaks = [0.0, 0.0]
    clipped = [0, 0]
    for block in blocks:
        for receiver in range(2):
            values = block.samples[receiver]
            component_peak = max(
                float(np.max(np.abs(values.real))),
                float(np.max(np.abs(values.imag))),
            )
            peaks[receiver] = max(peaks[receiver], component_peak)
            clipped[receiver] += int(
                np.count_nonzero(
                    (np.abs(values.real) >= ADC_CLIP_THRESHOLD_COUNTS)
                    | (np.abs(values.imag) >= ADC_CLIP_THRESHOLD_COUNTS)
                )
            )
    return (peaks[0], peaks[1]), (clipped[0], clipped[1])


def _main_analysis(blocks: Sequence[SampleBlockV2], contract: Mapping[str, Any]) -> dict[str, Any]:
    values = np.concatenate([block.samples for block in blocks], axis=1)
    condition = contract["condition"]
    test_index = 0 if condition["test_receiver"] == "RX1" else 1
    reference_index = 0 if condition["reference_receiver"] == "RX1" else 1
    analysis = analyze_coherent_leakage(
        values[reference_index],
        values[test_index],
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        block_duration_s=0.1,
        minimum_block_count=3,
    )
    if not analysis.quality_passed:
        raise PortPairRunError(
            "coherent matrix analysis failed: " + ", ".join(analysis.quality_rejection_reasons)
        )
    if analysis.rx1.tone_to_noise_snr_db < MINIMUM_REFERENCE_SNR_DB:
        raise PortPairRunError("conducted reference receiver SNR is below 20 dB")
    reference_phasor = analysis.rx1.phasor
    if reference_phasor is None:
        raise PortPairRunError("conducted reference receiver tone phasor is unavailable")
    transfer = analysis.rx2_over_rx1
    detected = transfer.phasor is not None and analysis.rx2.tone_detected
    if detected:
        test_upper = None
    else:
        amplitude_upper_bound = transfer.amplitude_upper_bound_ratio
        if amplitude_upper_bound is None:
            raise PortPairRunError("test-receiver nondetection lacks a phase-free bound")
        test_upper = float(amplitude_upper_bound) * abs(reference_phasor)
    return {
        "test_receiver_tone_detected": detected,
        "test_receiver_tone": _complex_document(analysis.rx2.phasor) if detected else None,
        "test_receiver_tone_magnitude_upper_bound": test_upper,
        "reference_receiver_tone": _complex_document(analysis.rx1.phasor),
        "reference_tone_snr_db": analysis.rx1.tone_to_noise_snr_db,
        "raw_channel_amplitudes_comparable": False,
        "normalization_required": True,
        "analysis": _json_safe(asdict(analysis)),
    }


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    root = Path(artifact.path)
    raw = data_path(artifact)
    metadata = root / f"{artifact.artifact_id}.sigmf-meta"
    return {
        "artifact_id": artifact.artifact_id,
        "path": str(root),
        "raw_iq_path": str(raw),
        "raw_iq_sha256": sha256_path(raw),
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
    }


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
        raise PortPairRunError("persisted artifact failed SHA-256 verification")
    return artifact


def _relocate_artifact(artifact: Any, destination_root: Path) -> Any:
    destination = destination_root / artifact.artifact_id
    return artifact.model_copy(update={"path": str(destination)})


def _relocate_artifact_evidence(
    staged_evidence: Mapping[str, Any], relocated_artifact: Any
) -> dict[str, Any]:
    root = Path(relocated_artifact.path)
    artifact_id = str(relocated_artifact.artifact_id)
    return {
        **dict(staged_evidence),
        "path": str(root),
        "raw_iq_path": str(root / f"{artifact_id}.sigmf-data"),
        "metadata_path": str(root / f"{artifact_id}.sigmf-meta"),
    }


def _quarantine_staging(staging_root: Path, capture_root: Path) -> None:
    if not staging_root.exists() or staging_root.is_symlink():
        return
    failed_parent = capture_root.parent / ".failed"
    for path, label in (
        (staging_root, "capture staging root"),
        (capture_root.parent, "capture parent"),
        (failed_parent, "failed-capture quarantine parent"),
    ):
        _assert_no_symlink_chain(path, label)
        try:
            assert_local_rpi_storage(path, label=label)
        except FileArtifactAdmissionError as error:
            raise PortPairRunError(str(error)) from error
    failed_parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(failed_parent, "failed-capture quarantine parent")
    failed_destination = failed_parent / capture_root.name
    if failed_destination.exists() or failed_destination.is_symlink():
        raise PortPairRunError("failed-capture quarantine destination already exists")
    os.replace(staging_root, failed_destination)
    _fsync_directory(failed_parent)


def _execute_condition(
    contract: Mapping[str, Any],
    *,
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
    capture_boundary: CaptureBoundary = _live_capture,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> dict[str, Any]:
    preflight_plan = _tone_plan(contract, PREFLIGHT_TX_GAIN_DB)
    main_plan = _tone_plan(contract, CAPTURE_TX_GAIN_DB)
    preflight_blocks: list[SampleBlockV2] = []
    main_blocks: list[SampleBlockV2] = []

    def retain_preflight(block: SampleBlockV2) -> None:
        preflight_blocks.append(replace(block, samples=block.samples.copy(order="C")))

    def retain_main(block: SampleBlockV2) -> None:
        main_blocks.append(replace(block, samples=block.samples.copy(order="C")))

    pending_error: BaseException | None = None
    preflight_capture: Any | None = None
    main_capture: Any | None = None
    preflight_stream: int | None = None
    main_stream: int | None = None
    preflight_ledger: dict[str, Any] | None = None
    main_ledger: dict[str, Any] | None = None
    preflight_readback: dict[str, Any] | None = None
    main_readback: dict[str, Any] | None = None
    headroom_input: HeadroomPreflight | None = None
    admission: Any | None = None
    analysis: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None
    initial_mute: dict[str, Any] | None = None
    final_mute: dict[str, Any] | None = None
    post_preflight: dict[str, Any] | None = None
    post_main: dict[str, Any] | None = None
    preflight_timing: dict[str, Any] | None = None
    main_timing: dict[str, Any] | None = None
    try:
        identity = _call_identity(
            identity_boundary,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
        )
        if not _identity_passed(
            identity,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
        ):
            raise PortPairRunError("current USB URI/serial identity preflight failed")
        runtime_native = validate_runtime_attestation(native_boundary())
        if runtime_native != contract["source"]["native_libiio"]:
            raise PortPairRunError("runtime native libiio differs from immutable plan")
        initial_mute = _call_mute(
            mute_boundary,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="pre_preflight_exact_mute",
        )
        if not _mute_passed(
            initial_mute,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="pre_preflight_exact_mute",
        ):
            raise PortPairRunError("initial exact-radio mute failed")
        preflight_started = _clock_stamp()
        preflight_capture = capture_boundary(
            preflight_plan,
            samples_per_frame=PREFLIGHT_SAMPLES_PER_FRAME,
            frame_count=PREFLIGHT_FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain_preflight,
        )
        preflight_completed = _clock_stamp()
        preflight_timing = _capture_timing(
            purpose="preflight_capture",
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            started=preflight_started,
            completed=preflight_completed,
        )
        post_preflight = _call_mute(
            mute_boundary,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="post_preflight_exact_mute",
        )
        if not _mute_passed(
            post_preflight,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="post_preflight_exact_mute",
        ):
            raise PortPairRunError("post-preflight exact-radio mute failed")
        preflight_stream, preflight_ledger, preflight_readback = _validate_capture(
            preflight_capture,
            preflight_blocks,
            plan=preflight_plan,
            samples_per_frame=PREFLIGHT_SAMPLES_PER_FRAME,
            frame_count=PREFLIGHT_FRAME_COUNT,
        )
        peaks, clipped = _headroom(preflight_blocks)
        headroom_input = HeadroomPreflight(
            preflight_tx_gain_db=PREFLIGHT_TX_GAIN_DB,
            capture_tx_gain_db=CAPTURE_TX_GAIN_DB,
            clip_threshold_abs_counts=ADC_CLIP_THRESHOLD_COUNTS,
            peak_abs_counts_by_receiver=peaks,
            clipped_sample_count_by_receiver=clipped,
        )
        admission = evaluate_headroom_preflight(headroom_input)
        if not admission.passed:
            raise PortPairRunError(
                "projected main-capture headroom failed: " + ", ".join(admission.rejection_reasons)
            )
        main_started = _clock_stamp()
        main_capture = capture_boundary(
            main_plan,
            samples_per_frame=MAIN_SAMPLES_PER_FRAME,
            frame_count=MAIN_FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain_main,
        )
        main_completed = _clock_stamp()
        main_timing = _capture_timing(
            purpose="main_capture",
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            started=main_started,
            completed=main_completed,
        )
        post_main = _call_mute(
            mute_boundary,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="post_main_exact_mute",
        )
        if not _mute_passed(
            post_main,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose="post_main_exact_mute",
        ):
            raise PortPairRunError("post-main exact-radio mute failed")
        main_stream, main_ledger, main_readback = _validate_capture(
            main_capture,
            main_blocks,
            plan=main_plan,
            samples_per_frame=MAIN_SAMPLES_PER_FRAME,
            frame_count=MAIN_FRAME_COUNT,
        )
        if preflight_stream == main_stream:
            raise PortPairRunError("preflight and main captures reused one ABI2 stream")
        _, main_clipped = _headroom(main_blocks)
        if main_clipped != (0, 0):
            raise PortPairRunError("main capture contains clipped samples")
        analysis = _main_analysis(main_blocks, contract)
    except BaseException as error:
        pending_error = error
    finally:
        final_mute = _call_mute(
            mute_boundary,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
            purpose=FINAL_ACCEPTANCE_MUTE_PURPOSE,
        )

    if not _mute_passed(
        final_mute,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
        purpose=FINAL_ACCEPTANCE_MUTE_PURPOSE,
    ):
        pending_error = PortPairRunError("mandatory final exact-radio mute failed")
    if pending_error is not None:
        preflight_blocks.clear()
        main_blocks.clear()
        raise pending_error
    assert preflight_capture is not None and main_capture is not None
    assert preflight_stream is not None and main_stream is not None
    assert preflight_ledger is not None and main_ledger is not None
    assert preflight_readback is not None and main_readback is not None
    assert headroom_input is not None and admission is not None
    assert analysis is not None and final_mute is not None
    assert identity is not None and initial_mute is not None
    assert post_preflight is not None and post_main is not None
    assert preflight_timing is not None and main_timing is not None
    capture_timeline = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_capture_mute_timeline_v1",
        "preflight_capture": preflight_timing,
        "post_preflight_mute": post_preflight,
        "main_capture": main_timing,
        "post_main_mute": post_main,
    }
    execution_safety = _validated_execution_safety(
        identity=identity,
        initial_mute=initial_mute,
        final_mute=final_mute,
        capture_timeline=capture_timeline,
        attempt_started=attempt_started,
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )

    capture_root = Path(str(contract["storage"]["capture_root"]))
    if capture_root.exists() or capture_root.is_symlink():
        raise PortPairRunError("capture root existed before accepted persistence")
    staging_root = capture_root.parent / f".{capture_root.name}.staging"
    if staging_root.exists() or staging_root.is_symlink():
        raise PortPairRunError("capture staging root already exists")
    condition_root = Path(str(contract["storage"]["condition_root"]))
    record_path = condition_root / CONDITION_RECORD_FILENAME
    observation_path = condition_root / OBSERVATION_FILENAME
    preflight_evidence: dict[str, Any]
    main_evidence: dict[str, Any]
    try:
        staged_preflight = _persist_blocks(
            staging_root / "preflight",
            capture=preflight_capture,
            blocks=preflight_blocks,
            label=f"protected matrix {contract['condition']['cell_id']} preflight",
        )
        staged_main = _persist_blocks(
            staging_root / "main",
            capture=main_capture,
            blocks=main_blocks,
            label=f"protected matrix {contract['condition']['cell_id']} main",
        )
        staged_preflight_evidence = _artifact_evidence(staged_preflight)
        staged_main_evidence = _artifact_evidence(staged_main)
        preflight_artifact = _relocate_artifact(staged_preflight, capture_root / "preflight")
        main_artifact = _relocate_artifact(staged_main, capture_root / "main")
        preflight_evidence = _relocate_artifact_evidence(
            staged_preflight_evidence, preflight_artifact
        )
        main_evidence = _relocate_artifact_evidence(staged_main_evidence, main_artifact)
        record = {
            "schema": 1,
            "record_kind": "5g8_protected_port_pair_condition_record",
            "created_at": _now(),
            "campaign_plan_sha256": contract["campaign_plan"]["sha256"],
            "plan_contract_sha256": canonical_sha256(contract),
            "condition": contract["condition"],
            "fixture": contract["fixture"],
            "calibration": contract["calibration"],
            "identity_preflight": identity,
            "initial_mute": initial_mute,
            "capture_timeline": capture_timeline,
            "execution_tombstone": dict(execution_marker_receipt),
            "execution_safety_sha256": execution_safety["evidence_sha256"],
            "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
            "initial_mute_sha256": execution_safety["initial_mute_sha256"],
            "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
            "final_mute_sha256": execution_safety["final_mute_sha256"],
            "execution_tombstone_receipt_sha256": execution_safety[
                "execution_tombstone_receipt_sha256"
            ],
            "permanent_run_reservation": dict(reservation_receipt),
            "irreversible_execution_burn": dict(burn_receipt),
            "headroom_preflight": {
                "input": asdict(headroom_input),
                "admission": asdict(admission),
            },
            "preflight": {
                "artifact": preflight_artifact.model_dump(mode="json"),
                "evidence": preflight_evidence,
                "stream_id": preflight_stream,
                "continuity_ledger": preflight_ledger,
                "rf_readback": preflight_readback,
            },
            "main": {
                "artifact": main_artifact.model_dump(mode="json"),
                "evidence": main_evidence,
                "stream_id": main_stream,
                "continuity_ledger": main_ledger,
                "rf_readback": main_readback,
                "analysis": analysis,
            },
            "final_mute": final_mute,
            "raw_channel_amplitude_comparison_forbidden": True,
        }
        _write_immutable_json(record_path, record)
        observation = {
            "schema": 1,
            "observation_kind": NORMALIZED_OBSERVATION_KIND,
            "campaign_id": contract["campaign_id"],
            "run_id": contract["run_id"],
            "cell_id": contract["condition"]["cell_id"],
            "repeat_index": contract["condition"]["repeat_index"],
            "campaign_plan_sha256": contract["campaign_plan"]["sha256"],
            "plan_contract_sha256": canonical_sha256(contract),
            "fixture_sha256": contract["fixture"]["identity_sha256"],
            "calibration_sha256": contract["calibration"]["identity_sha256"],
            "topology_sha256": contract["condition"]["topology_sha256"],
            "source_commit": contract["source"]["smateway"]["commit"],
            "dependency_commit": contract["source"]["pluto_plus_utils"]["commit"],
            "native_attestation_sha256": contract["source"]["native_libiio_sha256"],
            "preflight": {
                "stream_id": preflight_stream,
                "artifact": preflight_evidence,
                "condition_record_sha256": sha256_path(record_path),
                "headroom": {
                    "input": asdict(headroom_input),
                    "admission": asdict(admission),
                },
                "continuity_passed": True,
            },
            "main": {
                "stream_id": main_stream,
                "artifact": main_evidence,
                "condition_record_sha256": sha256_path(record_path),
                "rf_readback": main_readback,
                "clipped_sample_count_by_receiver": [0, 0],
                "analysis": analysis,
                "continuity_passed": True,
            },
            "physical_safety": {
                "inactive_tx_termination_sha256": contract["fixture"][
                    "inactive_tx_termination_sha256"
                ],
                "test_receiver_termination_sha256": contract["fixture"][
                    "test_receiver_termination_sha256"
                ],
                "reference_chain_sha256": contract["fixture"]["reference_chain_sha256"],
                "rx1_protection_sha256": contract["fixture"]["rx1_protection_sha256"],
            },
            "identity_preflight": identity,
            "initial_mute": initial_mute,
            "capture_timeline": capture_timeline,
            "execution_tombstone": dict(execution_marker_receipt),
            "final_mute": final_mute,
            "execution_safety_sha256": execution_safety["evidence_sha256"],
            "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
            "initial_mute_sha256": execution_safety["initial_mute_sha256"],
            "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
            "final_mute_sha256": execution_safety["final_mute_sha256"],
            "execution_tombstone_receipt_sha256": execution_safety[
                "execution_tombstone_receipt_sha256"
            ],
            "permanent_run_reservation": dict(reservation_receipt),
            "irreversible_execution_burn": dict(burn_receipt),
            "quality_passed": True,
            "raw_channel_amplitudes_comparable": False,
        }
        _write_immutable_json(observation_path, observation)
        capture_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root, capture_root)
        _fsync_directory(capture_root.parent)
    except BaseException:
        _quarantine_staging(staging_root, capture_root)
        raise
    finally:
        preflight_blocks.clear()
        main_blocks.clear()
    return {
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "observation_path": str(observation_path),
        "observation_sha256": sha256_path(observation_path),
        "preflight_artifact": preflight_evidence,
        "main_artifact": main_evidence,
        "accepted_stream_count": 2,
        "identity_preflight": identity,
        "initial_mute": initial_mute,
        "capture_timeline": capture_timeline,
        "execution_tombstone": dict(execution_marker_receipt),
        "final_mute": final_mute,
        "execution_safety_sha256": execution_safety["evidence_sha256"],
        "identity_preflight_sha256": execution_safety["identity_preflight_sha256"],
        "initial_mute_sha256": execution_safety["initial_mute_sha256"],
        "capture_timeline_sha256": execution_safety["capture_timeline_sha256"],
        "final_mute_sha256": execution_safety["final_mute_sha256"],
        "execution_tombstone_receipt_sha256": execution_safety[
            "execution_tombstone_receipt_sha256"
        ],
        "permanent_run_reservation": dict(reservation_receipt),
        "irreversible_execution_burn": dict(burn_receipt),
    }


def _execution_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    *,
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
) -> dict[str, Any]:
    created = _clock_stamp()
    document = {
        "schema": 1,
        "marker_kind": "5g8_port_pair_execution_started_tombstone",
        "run_id": contract["run_id"],
        "cell_id": contract["condition"]["cell_id"],
        "repeat_index": contract["condition"]["repeat_index"],
        "attempt_started_at": attempt_started["started_at"],
        "attempt_started_monotonic_ns": attempt_started["started_monotonic_ns"],
        "attempt_started_clock_boot_id": attempt_started["started_clock_boot_id"],
        **_stamp_fields(created, "created"),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "permanent_run_reservation": dict(reservation_receipt),
        "irreversible_execution_burn": dict(burn_receipt),
        "execution_authorization_sha256": _authorization_digest(reservation_receipt, burn_receipt),
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _validate_execution_tombstone_receipt(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
    receipt: object | None = None,
) -> dict[str, Any]:
    path = plan_path.expanduser().absolute().parent / EXECUTION_TOMBSTONE_FILENAME
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise PortPairRunError("execution tombstone must be a regular read-only non-symlink file")
    document = _read_json(path, "execution tombstone")
    _exact_keys(
        document,
        {
            "schema",
            "marker_kind",
            "run_id",
            "cell_id",
            "repeat_index",
            "attempt_started_at",
            "attempt_started_monotonic_ns",
            "attempt_started_clock_boot_id",
            "created_at",
            "created_monotonic_ns",
            "created_clock_boot_id",
            "plan_path",
            "plan_sha256",
            "plan_contract_sha256",
            "permanent_run_reservation",
            "irreversible_execution_burn",
            "execution_authorization_sha256",
            "run_id_burned",
            "automatic_retry_forbidden",
        },
        "execution tombstone",
    )
    condition = contract.get("condition")
    if not isinstance(condition, Mapping):
        raise PortPairRunError("execution tombstone lacks its condition")
    if (
        document.get("schema") != 1
        or document.get("marker_kind") != "5g8_port_pair_execution_started_tombstone"
        or document.get("run_id") != contract.get("run_id")
        or document.get("cell_id") != condition.get("cell_id")
        or document.get("repeat_index") != condition.get("repeat_index")
        or document.get("attempt_started_at") != attempt_started.get("started_at")
        or document.get("attempt_started_monotonic_ns")
        != attempt_started.get("started_monotonic_ns")
        or document.get("attempt_started_clock_boot_id")
        != attempt_started.get("started_clock_boot_id")
        or document.get("plan_path") != str(plan_path.expanduser().absolute())
        or document.get("plan_sha256") != sha256_path(plan_path)
        or document.get("plan_contract_sha256") != canonical_sha256(contract)
        or document.get("permanent_run_reservation") != reservation_receipt
        or document.get("irreversible_execution_burn") != burn_receipt
        or document.get("execution_authorization_sha256")
        != _authorization_digest(reservation_receipt, burn_receipt)
        or document.get("run_id_burned") is not True
        or document.get("automatic_retry_forbidden") is not True
    ):
        raise PortPairRunError("execution tombstone identity differs")
    reservation_document = reservation_receipt.get("document")
    burn_document = burn_receipt.get("document")
    if not isinstance(reservation_document, Mapping) or not isinstance(burn_document, Mapping):
        raise PortPairRunError("execution tombstone lacks authorization documents")
    _assert_clock_order(
        (
            _clock_point(reservation_document, prefix="reserved", label="reservation"),
            _clock_point(attempt_started, prefix="started", label="attempt"),
            _clock_point(burn_document, prefix="burned", label="execution burn"),
            _clock_point(document, prefix="created", label="execution tombstone"),
        ),
        label="reservation→attempt start→burn→execution tombstone",
    )
    expected = _receipt(
        path,
        document,
        "5g8_port_pair_execution_tombstone_receipt_v1",
    )
    if receipt is not None and receipt != expected:
        raise PortPairRunError("execution tombstone receipt binding differs")
    return expected


def _raw_file_state_without_hash(path: Path) -> dict[str, Any]:
    """Collect rescue evidence without invoking normal read/hash admission."""

    exact = path.expanduser().absolute()
    try:
        observed = os.lstat(exact)
    except OSError as error:
        return {
            "path": str(exact),
            "state": "unavailable",
            "stat_error": _error_document(error),
        }
    if stat.S_ISREG(observed.st_mode):
        state = "regular_file_present"
    elif stat.S_ISLNK(observed.st_mode):
        state = "symlink_present"
    else:
        state = "unexpected_file_type_present"
    return {
        "path": str(exact),
        "state": state,
        "st_dev": int(observed.st_dev),
        "st_ino": int(observed.st_ino),
        "st_nlink": int(observed.st_nlink),
        "mode": f"{stat.S_IMODE(observed.st_mode):04o}",
        "size_bytes": int(observed.st_size),
        "stat_error": None,
    }


def _degraded_authorization_evidence(
    contract: Mapping[str, Any],
    *,
    reservation_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
    burn_guard_path: Path,
    burn_path: Path,
    execution_path: Path,
    authorization_error: BaseException,
    validated_burn_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": DEGRADED_AUTHORIZATION_KIND,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "attempt_started": dict(attempt_started),
        "permanent_run_reservation": dict(reservation_receipt),
        "validated_burn_receipt": (
            dict(validated_burn_receipt) if validated_burn_receipt is not None else None
        ),
        "burn_guard_raw_state": _raw_file_state_without_hash(burn_guard_path),
        "burn_marker_raw_state": _raw_file_state_without_hash(burn_path),
        "execution_tombstone_raw_state": _raw_file_state_without_hash(execution_path),
        "receipt_or_hash_error": _error_document(authorization_error),
        "normal_hash_or_receipt_not_claimed": True,
        "hardware_access_may_have_been_authorized": True,
    }
    return {**evidence, "evidence_sha256": canonical_sha256(evidence)}


def _call_degraded_cleanup_mute(
    boundary: MuteBoundary,
    *,
    contract: Mapping[str, Any],
    degraded_authorization: Mapping[str, Any],
) -> dict[str, Any]:
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PortPairRunError("degraded cleanup lacks the immutable configuration")
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    started = _clock_stamp()
    error: dict[str, str] | None = None
    gains: object = None
    scales: object = None
    status = "failed"
    try:
        raw = boundary(serial, FAILURE_CLEANUP_MUTE_PURPOSE)
        if not isinstance(raw, Mapping):
            raise PortPairRunError("degraded cleanup mute returned non-object evidence")
        raw_error = raw.get("error")
        error = None if raw_error is None else _validate_error(raw_error, "cleanup mute error")
        gains = raw.get("tx_gain_readback_db_by_channel")
        scales = raw.get("dds_scale_readback")
        typed_gains = (
            isinstance(gains, list)
            and len(gains) == 2
            and all(type(item) is float for item in gains)
        )
        typed_scales = (
            isinstance(scales, list)
            and len(scales) == 8
            and all(type(item) is float for item in scales)
        )
        if (
            raw.get("status") == "passed"
            and error is None
            and typed_gains
            and typed_scales
            and gains == [-80.0, -80.0]
            and scales == [0.0] * 8
        ):
            status = "passed"
        else:
            gains = None
            scales = None
            if error is None:
                error = {
                    "type": "MuteReadbackError",
                    "message": "exact mute readback was not proven",
                }
    except BaseException as mute_error:
        gains = None
        scales = None
        error = _error_document(mute_error)
    completed = _clock_stamp()
    return {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_degraded_exact_mute_v1",
        "status": status,
        "purpose": FAILURE_CLEANUP_MUTE_PURPOSE,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "degraded_authorization_sha256": degraded_authorization["evidence_sha256"],
        "serial": serial,
        "uri": uri,
        "attestation": "mute_returned_radio_exact_serial_readback_under_degraded_authorization",
        "tx_gain_readback_db_by_channel": _json_safe(gains),
        "dds_scale_readback": _json_safe(scales),
        **_stamp_fields(started, "started"),
        **_stamp_fields(completed, "completed"),
        "error": error,
    }


def _degraded_failure_cleanup(
    exact_mute: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    degraded_authorization: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_clock_interval(exact_mute, label="degraded failure cleanup mute")
    passed = exact_mute.get("status") == "passed"
    return {
        "schema": 1,
        "evidence_kind": DEGRADED_CLEANUP_KIND,
        "run_id": contract["run_id"],
        "plan_contract_sha256": canonical_sha256(contract),
        "degraded_authorization": dict(degraded_authorization),
        "purpose": FAILURE_CLEANUP_MUTE_PURPOSE,
        "exact_mute": dict(exact_mute),
        "exact_mute_passed": passed,
        "mandatory_final_cleanup_attempted": True,
        "normal_execution_authorization_not_claimed": True,
    }


def _seal_emergency_failure_slot(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
    reservation_receipt: Mapping[str, Any],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    authority = _global_ledger_authority(contract, plan_path=plan_path, backend=ledger_backend)
    binding = reservation_receipt.get("global_reservation_binding")
    slots = binding.get("slots") if isinstance(binding, Mapping) else None
    if not isinstance(slots, Mapping) or not isinstance(slots.get("failure-receipt"), Mapping):
        raise PortPairRunError("reservation lacks its global emergency failure slot")
    sealed_document = {
        **dict(document),
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
    }
    request = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "failure",
            "expected_identity": slots["failure-receipt"],
            "document": sealed_document,
        },
    )
    try:
        response = global_ledger.validate_response(request, ledger_backend.mutate(request))
    except global_ledger.GlobalLedgerError as error:
        raise PortPairRunError(f"cannot seal the global emergency failure slot: {error}") from error
    evidence = response["evidence"]
    if not isinstance(evidence, Mapping):
        raise PortPairRunError("global emergency failure response is malformed")
    return {**dict(evidence), "document": sealed_document}


def _failure_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    error: BaseException,
    *,
    failure_cleanup: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    execution_tombstone_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    recomputed_cleanup = _validated_failure_cleanup(
        failure_cleanup.get("exact_mute"),
        contract=contract,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_marker_receipt,
    )
    if dict(failure_cleanup) != recomputed_cleanup:
        raise PortPairRunError("failure cleanup evidence differs from recomputed mute safety")
    document = {
        "schema": 1,
        "marker_kind": "5g8_port_pair_failed_run_tombstone",
        "run_id": contract["run_id"],
        "cell_id": contract["condition"]["cell_id"],
        "repeat_index": contract["condition"]["repeat_index"],
        "failed_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "permanent_run_reservation": dict(reservation_receipt),
        "irreversible_execution_burn": dict(burn_receipt),
        "execution_authorization_sha256": _authorization_digest(reservation_receipt, burn_receipt),
        "execution_tombstone_evidence": (
            dict(execution_tombstone_evidence) if execution_tombstone_evidence is not None else None
        ),
        "error": _error_document(error),
        "failure_cleanup_evidence": recomputed_cleanup,
        "failure_cleanup_evidence_sha256": canonical_sha256(recomputed_cleanup),
        "final_failure_cleanup_passed": recomputed_cleanup["exact_mute_passed"],
        "accepted_artifacts": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _degraded_failure_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    error: BaseException,
    *,
    failure_cleanup: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    degraded_authorization: Mapping[str, Any],
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_port_pair_failed_run_tombstone_degraded_authorization_v1",
        "run_id": contract["run_id"],
        "cell_id": contract["condition"]["cell_id"],
        "repeat_index": contract["condition"]["repeat_index"],
        "failed_at": _now(),
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_contract_sha256": canonical_sha256(contract),
        "permanent_run_reservation": dict(reservation_receipt),
        "degraded_execution_authorization": dict(degraded_authorization),
        "error": _error_document(error),
        "failure_cleanup_evidence": dict(failure_cleanup),
        "failure_cleanup_evidence_sha256": canonical_sha256(failure_cleanup),
        "final_failure_cleanup_passed": failure_cleanup.get("exact_mute_passed") is True,
        "accepted_artifacts": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
        "normal_burn_or_marker_receipt_not_claimed": True,
    }
    _write_immutable_json(path, document)
    return document


def _emergency_failure_document(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    attempt_started: Mapping[str, Any],
    original_error: BaseException,
    persistence_error: BaseException,
    authorization_evidence: Mapping[str, Any],
    failure_cleanup: Mapping[str, Any],
) -> dict[str, Any]:
    execution_nonce: str | None = None
    if authorization_evidence.get("evidence_kind") == (
        "5g8_port_pair_validated_execution_authorization_v1"
    ):
        burn = authorization_evidence.get("irreversible_execution_burn")
        burn_document = burn.get("document") if isinstance(burn, Mapping) else None
        candidate = (
            burn_document.get("execution_nonce") if isinstance(burn_document, Mapping) else None
        )
        if isinstance(candidate, str):
            execution_nonce = candidate
    else:
        burn = authorization_evidence.get("validated_burn_receipt")
        burn_document = burn.get("document") if isinstance(burn, Mapping) else None
        candidate = (
            burn_document.get("execution_nonce") if isinstance(burn_document, Mapping) else None
        )
        if isinstance(candidate, str):
            execution_nonce = candidate
    return {
        "schema": 1,
        "receipt_kind": EMERGENCY_FAILURE_KIND,
        "run_id": contract["run_id"],
        "board_id": contract["board_id"],
        "campaign_id": contract["campaign_id"],
        "cell_id": contract["condition"]["cell_id"],
        "repeat_index": contract["condition"]["repeat_index"],
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_contract_sha256": canonical_sha256(contract),
        "execution_nonce": execution_nonce,
        "attempt_started": dict(attempt_started),
        "recorded_at": _now(),
        "execution_authorization_or_degraded_state": dict(authorization_evidence),
        "failure_cleanup": dict(failure_cleanup),
        "original_error": _error_document(original_error),
        "ordinary_persistence_error": _error_document(persistence_error),
        "accepted_artifacts": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
        "emergency_slot_is_independent_of_local_run_persistence": True,
    }


def _execute_prepared(
    *,
    plan_path: Path,
    manifest_path: Path,
    expected_contract: Mapping[str, Any],
    confirmations: Mapping[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
    execute_boundary: (
        Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] | None
    ) = None,
    mute_boundary: MuteBoundary = _strict_mute,
) -> dict[str, Any]:
    _require_local_storage_contract(expected_contract, condition_root=plan_path.parent)
    if _read_json(plan_path, "immutable plan") != _plan_envelope(expected_contract):
        raise PortPairRunError("execution arguments/evidence differ from immutable plan")
    manifest = _read_json(manifest_path, "manifest")
    if manifest.get("status") != "prepared" or manifest.get("attempts") != []:
        raise PortPairRunError("run is not a never-attempted prepared condition")
    reservation_receipt = _assert_run_unburned_before_hardware(
        expected_contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_backend=ledger_backend,
    )
    # Resolve the rescue paths while the normal authority is still healthy.  Once
    # an irreversible burn begins, the cleanup path must not depend on another
    # authority read, receipt validation, or content hash before muting the radio.
    _reservation_path, guard_path, burn_path, _failure_slot_path = _ledger_paths(
        expected_contract, plan_path=plan_path, backend=ledger_backend
    )
    execution_path = manifest_path.parent / EXECUTION_TOMBSTONE_FILENAME
    failure_path = manifest_path.parent / FAILURE_TOMBSTONE_FILENAME
    burn_receipt: dict[str, Any] | None = None
    execution_evidence: dict[str, Any] | None = None
    attempt_started_stamp = _clock_stamp()
    attempt_started = _stamp_fields(attempt_started_stamp, "started")
    attempt = {
        **attempt_started,
        "status": "running",
        "confirmations": _json_safe(confirmations),
        "permanent_run_reservation": reservation_receipt,
        "irreversible_execution_burn": None,
        "execution_tombstone": None,
        "result": None,
        "error": None,
    }
    try:
        burn_receipt = _acquire_execution_burn(
            expected_contract,
            plan_path=plan_path,
            manifest_path=manifest_path,
            reservation_receipt=reservation_receipt,
            attempt_started=attempt_started,
            ledger_backend=ledger_backend,
        )
        attempt["irreversible_execution_burn"] = burn_receipt
        execution = _execution_tombstone(
            execution_path,
            expected_contract,
            plan_path,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            attempt_started=attempt_started,
        )
        execution_evidence = _validate_execution_tombstone_receipt(
            expected_contract,
            plan_path=plan_path,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            attempt_started=attempt_started,
        )
        if execution_evidence.get("document") != execution:
            raise PortPairRunError("execution tombstone changed before receipt validation")
        attempt["execution_tombstone"] = execution_evidence
        manifest["status"] = "running"
        manifest["attempts"] = [attempt]
        manifest["updated_at"] = _now()
        write_json_atomic(manifest_path, manifest)
        result = (
            _execute_condition(
                expected_contract,
                reservation_receipt=reservation_receipt,
                burn_receipt=burn_receipt,
                execution_marker_receipt=execution_evidence,
                attempt_started=attempt_started,
                mute_boundary=mute_boundary,
            )
            if execute_boundary is None
            else execute_boundary(
                expected_contract,
                {
                    "attempt_started": attempt_started,
                    "permanent_run_reservation": reservation_receipt,
                    "irreversible_execution_burn": burn_receipt,
                    "execution_tombstone": execution_evidence,
                },
            )
        )
        if (
            result.get("accepted_stream_count") != 2
            or result.get("permanent_run_reservation") != reservation_receipt
            or result.get("irreversible_execution_burn") != burn_receipt
            or result.get("execution_tombstone") != execution_evidence
        ):
            raise PortPairRunError(
                "condition did not return exact streams and external execution receipts"
            )
        attempt["status"] = "complete"
        attempt.update(_stamp_fields(_clock_stamp(), "completed"))
        attempt["result"] = result
        _validate_completed_attempt_timeline(
            attempt=attempt,
            result=result,
            contract=expected_contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_evidence,
        )
        manifest["status"] = "complete"
        manifest["result"] = result
        manifest["updated_at"] = _now()
        manifest["accepted_stream_count"] = 2
        manifest["error"] = None
        write_json_atomic(manifest_path, manifest)
    except BaseException as error:
        burn_raw_state = _raw_file_state_without_hash(burn_path)
        guard_raw_state = _raw_file_state_without_hash(guard_path)
        burn_may_have_started = (
            burn_raw_state.get("state") != "unavailable" or guard_raw_state.get("size_bytes") == 1
        )
        if not burn_may_have_started:
            raise
        normal_authorization = burn_receipt is not None and execution_evidence is not None
        degraded_authorization: dict[str, Any] | None = None
        if normal_authorization:
            assert burn_receipt is not None and execution_evidence is not None
            cleanup_mute = _call_mute(
                mute_boundary,
                contract=expected_contract,
                reservation_receipt=reservation_receipt,
                burn_receipt=burn_receipt,
                execution_marker_receipt=execution_evidence,
                purpose=FAILURE_CLEANUP_MUTE_PURPOSE,
            )
            failure_cleanup = _validated_failure_cleanup(
                cleanup_mute,
                contract=expected_contract,
                reservation_receipt=reservation_receipt,
                burn_receipt=burn_receipt,
                execution_marker_receipt=execution_evidence,
            )
            authorization_evidence: Mapping[str, Any] = {
                "schema": 1,
                "evidence_kind": "5g8_port_pair_validated_execution_authorization_v1",
                "permanent_run_reservation": reservation_receipt,
                "irreversible_execution_burn": burn_receipt,
                "execution_tombstone": execution_evidence,
            }
        else:
            degraded_authorization = _degraded_authorization_evidence(
                expected_contract,
                reservation_receipt=reservation_receipt,
                attempt_started=attempt_started,
                burn_guard_path=guard_path,
                burn_path=burn_path,
                execution_path=execution_path,
                authorization_error=error,
                validated_burn_receipt=burn_receipt,
            )
            cleanup_mute = _call_degraded_cleanup_mute(
                mute_boundary,
                contract=expected_contract,
                degraded_authorization=degraded_authorization,
            )
            failure_cleanup = _degraded_failure_cleanup(
                cleanup_mute,
                contract=expected_contract,
                degraded_authorization=degraded_authorization,
            )
            authorization_evidence = degraded_authorization
        attempt["status"] = "failed"
        attempt.update(_stamp_fields(_clock_stamp(), "completed"))
        attempt["error"] = _error_document(error)
        attempt["result"] = None
        manifest["status"] = "failed"
        manifest["result"] = None
        manifest["error"] = attempt["error"]
        manifest["updated_at"] = _now()
        manifest["accepted_stream_count"] = 0
        attempt["permanent_run_reservation"] = reservation_receipt
        attempt["irreversible_execution_burn"] = burn_receipt
        attempt["execution_tombstone"] = execution_evidence
        manifest["attempts"] = [attempt]
        attempt["failure_cleanup_evidence"] = failure_cleanup
        attempt["failure_cleanup_evidence_sha256"] = canonical_sha256(failure_cleanup)
        try:
            if normal_authorization:
                assert burn_receipt is not None and execution_evidence is not None
                failure = _failure_tombstone(
                    failure_path,
                    expected_contract,
                    plan_path,
                    error,
                    failure_cleanup=failure_cleanup,
                    reservation_receipt=reservation_receipt,
                    burn_receipt=burn_receipt,
                    execution_marker_receipt=execution_evidence,
                    execution_tombstone_evidence=execution_evidence,
                )
            else:
                assert degraded_authorization is not None
                failure = _degraded_failure_tombstone(
                    failure_path,
                    expected_contract,
                    plan_path,
                    error,
                    failure_cleanup=failure_cleanup,
                    reservation_receipt=reservation_receipt,
                    degraded_authorization=degraded_authorization,
                )
            manifest["failure_tombstone"] = _receipt(
                failure_path,
                failure,
                "5g8_port_pair_failure_tombstone_receipt_v1",
            )
            write_json_atomic(manifest_path, manifest)
        except BaseException as persistence_error:
            emergency_document = _emergency_failure_document(
                expected_contract,
                plan_path=plan_path,
                attempt_started=attempt_started,
                original_error=error,
                persistence_error=persistence_error,
                authorization_evidence=authorization_evidence,
                failure_cleanup=failure_cleanup,
            )
            try:
                _seal_emergency_failure_slot(
                    contract=expected_contract,
                    plan_path=plan_path,
                    ledger_backend=ledger_backend,
                    reservation_receipt=reservation_receipt,
                    document=emergency_document,
                )
            except BaseException as emergency_error:
                raise PortPairRunError(
                    f"{error}; ordinary failure persistence failed: {persistence_error}; "
                    f"emergency failure persistence failed: {emergency_error}"
                ) from error
        if failure_cleanup["exact_mute_passed"] is not True:
            raise PortPairRunError(
                f"{error}; mandatory failed-run final exact mute was not proven"
            ) from error
        raise
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--cell", choices=CELL_IDS, required=True)
    parser.add_argument("--repeat-index", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local/state/smateway",
        help="local Raspberry Pi state root; Pluto/removable storage is forbidden",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-inactive-tx-physically-terminated", action="store_true")
    parser.add_argument("--confirm-test-receiver-terminated", action="store_true")
    parser.add_argument("--confirm-rx1-protection-unchanged", action="store_true")
    parser.add_argument("--confirm-separate-reference-attenuator", action="store_true")
    parser.add_argument("--confirm-reference-planes-match", action="store_true")
    parser.add_argument("--confirm-no-movement", action="store_true")
    parser.add_argument("--confirm-topology-token")
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
        calibration_document = _read_json(args.calibration, "calibration")
        source = _repository_source_attestation()
        dependency = attest_pluto_plus_utils_source()
        native = attest_runtime()
        contract = _build_plan_contract(
            run_id=args.run_id,
            campaign_id=args.campaign_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            cell_id=args.cell,
            repeat_index=args.repeat_index,
            fixture_document=fixture_document,
            fixture_file=_file_evidence(args.fixture, "fixture"),
            calibration_document=calibration_document,
            calibration_file=_file_evidence(args.calibration, "calibration"),
            source_attestation=source,
            dependency_attestation=dependency,
            native_attestation=native,
            state_root=args.state_root,
        )
        condition_root = Path(str(contract["storage"]["condition_root"]))
        plan_path = condition_root / PLAN_FILENAME
        manifest_path = condition_root / MANIFEST_FILENAME
        ledger_backend = global_ledger.SudoLedgerBackend()
        if args.plan_only:
            envelope, manifest = _prepare_plan(
                plan_path,
                manifest_path,
                contract,
                ledger_backend=ledger_backend,
            )
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": manifest["status"],
                        "plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "topology_token": contract["condition"]["topology_token"],
                    }
                )
            )
            return 0
        confirmations = _validate_execution_confirmations(args, contract)
        manifest = _execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations=confirmations,
            ledger_backend=ledger_backend,
        )
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "status": manifest["status"],
                    "accepted_stream_count": manifest["accepted_stream_count"],
                    "observation": manifest["result"]["observation_path"],
                }
            )
        )
        return 0
    except (OSError, PortPairMatrixError, PortPairRunError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": _error_document(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
