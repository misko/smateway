#!/usr/bin/env python3
"""Plan or capture one hardened 5.8 GHz P2 input-drive-off Fast20 run."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, MutableMapping, Sequence
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
from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import (
    SafeDdsTonePlan,
    SampleBlockV2,
    capture_continuous_safe_dds_tone,
)
from pluto_plus.hardware.iio import find_usb_sysfs_path, resolve_iio_uri
from pluto_plus.models import GainMode, RadioSettings

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.capture_continuity import validate_continuity_ledger
from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
)
from smateway import global_ledger
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.input_off_control import (
    BANDWIDTH_HZ,
    CAMPAIGN_ID,
    CENTER_FREQUENCY_HZ,
    DDS_SCALE,
    EDGE_EXCLUSION_BINS,
    FRAME_COUNT,
    InputOffContractError,
    KERNEL_BUFFERS,
    MINIMUM_COMPLETE_FAST20_FRAMES,
    MINIMUM_PILOT_SNR_DB,
    OBSERVATION_KIND,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TONE_OFFSET_HZ,
    TOPOLOGY_STAGE,
    TOPOLOGY_TOKEN,
    TOTAL_SAMPLES,
    TX_CHANNEL,
    TX_HARDWARE_GAIN_DB,
    acquisition_contract,
    canonical_sha256,
    coherent_tone_snr_db,
    complex_document,
    phase_free_complex_upper_bound,
    validate_fixture_v2,
    validate_observation,
    validate_p0_cohort,
    validate_setup_attestation,
)
from smateway.native_iio_attestation import (
    attestation_sha256,
    attest_runtime,
    validate_runtime_attestation,
)
from smateway.p0_normalized_evidence import (
    P0NormalizedEvidenceError,
    admit_normalized_p0_evidence,
)
from smateway.ota_analysis import (
    ContinuityBlock,
    analyze_fast20_dwell_isolation,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile
from smateway.reference_transfer import analyze_fast20_reference_transfer
from smateway.schedule_alignment import AlignmentSearchMode
from smateway.selector_flash_attestation import (
    EVIDENCE_KIND as SEALED_SELECTOR_EVIDENCE_KIND,
    SelectorFlashError,
    validate_sealed_selector_evidence,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_PROFILE = Path("profiles/fast20-v1/control_profile.json")
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
OBSERVATION_FILENAME = "5g8-input-off-observation.json"
LEDGER_RESERVATION_FILENAME = global_ledger.RESERVATION_FILENAME
LEDGER_BURN_GUARD_FILENAME = global_ledger.BURN_GUARD_FILENAME
LEDGER_BURN_MARKER_FILENAME = global_ledger.BURN_MARKER_FILENAME
LEDGER_FAILURE_RECEIPT_FILENAME = global_ledger.FAILURE_RECEIPT_FILENAME
GLOBAL_LEDGER_DIRECTORY_MODE = global_ledger.DIRECTORY_MODE
GLOBAL_LEDGER_PREPARED_SLOT_MODE = global_ledger.PREPARED_SLOT_MODE
GLOBAL_LEDGER_SEALED_FILE_MODE = global_ledger.SEALED_FILE_MODE
P2_LEDGER_POLICY_ID = "p2-5g8-input-off-v1"
RUN_KIND = "5g8_input_drive_off_fast20_one_stream"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
SOURCE_FILES = (
    "profiles/fast20-v1/control_profile.json",
    "src/smateway/__init__.py",
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/bench.py",
    "src/smateway/decoder.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/global_ledger.py",
    "src/smateway/hexcal.py",
    "src/smateway/input_off_control.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/ota_analysis.py",
    "src/smateway/p0_normalized_evidence.py",
    "src/smateway/profile.py",
    "src/smateway/reference_transfer.py",
    "src/smateway/schedule_alignment.py",
    "src/smateway/selector_flash_attestation.py",
    "scripts/run_5g8_input_off_control.py",
    "scripts/analyze_5g8_input_off_cohort.py",
)
MINIMUM_PILOT_CONFIDENCE = 0.25
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.995
MAXIMUM_PILOT_PHASE_RMS_DEG = 6.0
MINIMUM_ALIGNMENT_SCORE = 0.75
MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT = 0.75
MINIMUM_REFERENCE_VALID_BIN_FRACTION = 0.95
MINIMUM_RX1_CYCLE_COHERENCE = 0.90
MINIMUM_ALL_OFF_CYCLE_COHERENCE = 0.75
MINIMUM_ALL_OFF_EVEN_ODD_AGREEMENT = 0.75
MAXIMUM_ALL_OFF_CYCLE_PHASE_STD_DEG = 30.0
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
LOAD_INPUT_LIMIT_DBM = 0.0
REQUIRED_MARGIN_DB = 10.0


class InputOffRunError(RuntimeError):
    """The run failed before an artifact could be accepted."""


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


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, default=str))


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None:
        raise InputOffRunError(f"{label} is not a safe identifier")
    return value


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise InputOffRunError(f"{label} path contains a symlink: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise InputOffRunError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputOffRunError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise InputOffRunError(f"{label} must contain one JSON object")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    _assert_no_symlink_chain(path.parent, "immutable evidence parent")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(path.parent, "immutable evidence parent")
    wire = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(wire)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    path.chmod(0o400)
    _fsync_directory(path.parent)


def _inode_identity(
    path: Path,
    *,
    directory: bool,
    label: str,
    expected_nlink: int | None = None,
) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    try:
        assert_local_rpi_storage(exact, label=label)
    except FileArtifactAdmissionError as error:
        raise InputOffRunError(str(error)) from error
    if exact.is_symlink() or (not exact.is_dir() if directory else not exact.is_file()):
        raise InputOffRunError(f"{label} has the wrong file type")
    observed = exact.stat()
    if expected_nlink is not None and observed.st_nlink != expected_nlink:
        raise InputOffRunError(f"{label} hard-link count differs")
    return {
        "path": str(exact),
        "st_dev": int(observed.st_dev),
        "st_ino": int(observed.st_ino),
    }


def _shared_global_run_namespace(*, board_id: str, run_id: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "policy_id": P2_LEDGER_POLICY_ID,
        "namespace_kind": global_ledger.POLICIES[P2_LEDGER_POLICY_ID].namespace_kind,
        "board_id": board_id,
        "run_id": run_id,
    }


def _shared_canonical_run_identity(
    *, board_id: str, run_id: str, run_root: Path, capture_root: Path
) -> dict[str, Any]:
    return {
        "schema": 1,
        "board_id": board_id,
        "run_id": run_id,
        "run_root": str(run_root.expanduser().absolute()),
        "capture_root": str(capture_root.expanduser().absolute()),
        "plan_path": str(run_root.expanduser().absolute() / PLAN_FILENAME),
    }


def _shared_new_global_ledger_authority(
    *,
    board_id: str,
    run_id: str,
    run_root: Path,
    capture_root: Path,
    state_root: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    return global_ledger.authority_from_storage(
        policy_id=P2_LEDGER_POLICY_ID,
        namespace=_shared_global_run_namespace(board_id=board_id, run_id=run_id),
        canonical_identity=_shared_canonical_run_identity(
            board_id=board_id,
            run_id=run_id,
            run_root=run_root,
            capture_root=capture_root,
        ),
        state_root=state_root,
        backend=ledger_backend,
    )


def _shared_validate_global_ledger_authority(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    execution = contract.get("execution")
    storage = contract.get("storage")
    if not isinstance(execution, Mapping) or not isinstance(storage, Mapping):
        raise InputOffRunError("immutable P2 plan lacks shared ledger authority")
    run_root = Path(str(storage.get("run_root", ""))).expanduser().absolute()
    capture_root = Path(str(storage.get("capture_root", ""))).expanduser().absolute()
    state_root = Path(str(storage.get("state_root", ""))).expanduser().absolute()
    identity = _shared_canonical_run_identity(
        board_id=str(contract.get("board_id", "")),
        run_id=str(contract.get("run_id", "")),
        run_root=run_root,
        capture_root=capture_root,
    )
    if identity["plan_path"] != str(plan_path.expanduser().absolute()):
        raise InputOffRunError("shared P2 authority plan path differs")
    try:
        return global_ledger.validate_authority(
            execution.get("global_run_ledger_authority"),
            policy_id=P2_LEDGER_POLICY_ID,
            namespace=_shared_global_run_namespace(
                board_id=str(contract.get("board_id", "")),
                run_id=str(contract.get("run_id", "")),
            ),
            canonical_identity=identity,
            state_root=state_root,
            backend=ledger_backend,
        )
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 global authority is invalid: {error}") from error


def _ledger_paths(authority: Mapping[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    directory = Path(str(authority["ledger_directory_path"]))
    return (
        directory,
        directory / LEDGER_RESERVATION_FILENAME,
        directory / LEDGER_BURN_GUARD_FILENAME,
        directory / LEDGER_BURN_MARKER_FILENAME,
        directory / LEDGER_FAILURE_RECEIPT_FILENAME,
    )


def _file_evidence_with_identity(
    path: Path, label: str, *, expected_nlink: int = 1
) -> dict[str, Any]:
    identity = _inode_identity(
        path,
        directory=False,
        label=label,
        expected_nlink=expected_nlink,
    )
    return {
        **identity,
        "size_bytes": path.stat().st_size,
        "mode": stat.S_IMODE(path.stat().st_mode),
        "nlink": int(path.stat().st_nlink),
        "sha256": sha256_path(path),
    }


def _anchor_names(authority: Mapping[str, Any]) -> set[str]:
    anchor_directory = Path(str(authority["storage"]["anchor_directory"]["path"]))
    prefix = f".anchor.{authority['ledger_key']}."
    try:
        return {entry.name for entry in anchor_directory.iterdir() if entry.name.startswith(prefix)}
    except OSError as error:
        raise InputOffRunError(f"cannot scan global P2 inode-anchor history: {error}") from error


def _new_global_reservation_binding(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    authority = _shared_validate_global_ledger_authority(
        contract, plan_path=plan_path, ledger_backend=ledger_backend
    )
    if _anchor_names(authority):
        raise InputOffRunError("P2 run ID already has durable global ledger history")
    ledger_directory, _reservation_path, _guard_path, marker_path, _failure_path = _ledger_paths(
        authority
    )
    reservation_id = uuid.uuid4().hex
    request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": reservation_id},
    )
    try:
        response = ledger_backend.mutate(request)
        normalized_response = global_ledger.validate_response(request, response)
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 reservation failed: {error}") from error
    evidence = normalized_response["evidence"]
    slots = evidence["slots"]
    anchors = evidence["anchors"]
    assert isinstance(slots, Mapping)
    assert isinstance(anchors, Mapping)
    binding = {
        "schema": 1,
        "binding_kind": "5g8_input_off_shared_global_run_reservation_binding_v1",
        "reservation_id": reservation_id,
        "ledger_key": authority["ledger_key"],
        "canonical_run_identity_sha256": authority["canonical_run_identity_sha256"],
        "global_ledger_authority": authority,
        "ledger_directory": dict(evidence["ledger_directory"]),
        "reservation_slot": dict(slots["reservation"]),
        "burn_guard": dict(slots["burn-guard"]),
        "failure_receipt_slot": dict(slots["failure-receipt"]),
        "burn_marker_path": str(marker_path),
        "reservation_anchor": dict(anchors["reservation"]),
        "burn_guard_anchor": dict(anchors["burn-guard"]),
        "failure_receipt_anchor": dict(anchors["failure-receipt"]),
        "privileged_reservation_response": dict(normalized_response),
        "global_reservation_outside_run_and_state_roots": True,
    }
    observed_ledger = _inode_identity(
        ledger_directory, directory=True, label="shared P2 ledger directory"
    )
    if observed_ledger != binding["ledger_directory"]:
        raise InputOffRunError("shared P2 ledger directory differs after reservation")
    return binding


def _reservation_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    binding: Mapping[str, Any],
    reserved_at: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "marker_kind": "5g8_input_off_global_run_id_reservation_v1",
        "reservation_id": binding["reservation_id"],
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "reserved_at": reserved_at,
        "run_root": contract["storage"]["run_root"],
        "capture_root": contract["storage"]["capture_root"],
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "global_ledger_authority": binding["global_ledger_authority"],
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            binding["global_ledger_authority"]
        ),
        "canonical_run_identity_sha256": binding["canonical_run_identity_sha256"],
        "ledger_key": binding["ledger_key"],
        "run_state_ledger_binding_sha256": canonical_sha256(binding),
        "replacement_recreation_or_replay_forbidden": True,
    }


def _seal_global_reservation(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    binding: Mapping[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    path = Path(str(binding["reservation_slot"]["path"]))
    document = _reservation_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        binding=binding,
        reserved_at=_now(),
    )
    request = global_ledger.mutation_request(
        authority=binding["global_ledger_authority"],
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "document": document,
            "expected_identity": binding["reservation_slot"],
        },
    )
    try:
        response = global_ledger.validate_response(request, ledger_backend.mutate(request))
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 reservation sealing failed: {error}") from error
    return {
        "binding": dict(binding),
        "reservation": {
            **_file_evidence_with_identity(path, "P2 global reservation", expected_nlink=2),
            "document": document,
            "privileged_seal_response": response,
        },
    }


def _validate_global_reservation(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    value: object,
    require_prepared_guard: bool,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InputOffRunError("manifest lacks global P2 reservation evidence")
    binding = value.get("binding")
    reservation = value.get("reservation")
    if not isinstance(binding, Mapping) or not isinstance(reservation, Mapping):
        raise InputOffRunError("global P2 reservation evidence is malformed")
    authority = _shared_validate_global_ledger_authority(
        contract, plan_path=plan_path, ledger_backend=ledger_backend
    )
    if binding.get("global_ledger_authority") != authority:
        raise InputOffRunError("global P2 reservation authority differs")
    reserve_request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": binding.get("reservation_id")},
    )
    try:
        reserve_response = global_ledger.validate_response(
            reserve_request, binding.get("privileged_reservation_response")
        )
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 reservation response is invalid: {error}") from error
    ledger_directory, reservation_path, guard_path, marker_path, failure_path = _ledger_paths(
        authority
    )
    if set(entry.name for entry in ledger_directory.iterdir()) not in (
        {
            LEDGER_RESERVATION_FILENAME,
            LEDGER_BURN_GUARD_FILENAME,
            LEDGER_FAILURE_RECEIPT_FILENAME,
        },
        {
            LEDGER_RESERVATION_FILENAME,
            LEDGER_BURN_GUARD_FILENAME,
            LEDGER_BURN_MARKER_FILENAME,
            LEDGER_FAILURE_RECEIPT_FILENAME,
        },
    ):
        raise InputOffRunError("global P2 ledger inventory is incomplete or unexpected")
    for path, name in (
        (reservation_path, "reservation_slot"),
        (guard_path, "burn_guard"),
        (failure_path, "failure_receipt_slot"),
    ):
        if _inode_identity(
            path, directory=False, label=f"P2 {name}", expected_nlink=2
        ) != binding.get(name):
            raise InputOffRunError(f"global P2 {name} identity differs")
    if binding.get("burn_marker_path") != str(marker_path):
        raise InputOffRunError("global P2 burn-marker path differs")
    anchor_directory = Path(str(authority["storage"]["anchor_directory"]["path"]))
    prefix = f".anchor.{binding.get('ledger_key')}.{binding.get('reservation_id')}"
    anchor_paths = {
        "reservation_anchor": anchor_directory / f"{prefix}.reservation",
        "burn_guard_anchor": anchor_directory / f"{prefix}.burn-guard",
        "failure_receipt_anchor": anchor_directory / f"{prefix}.failure-receipt",
    }
    if _anchor_names(authority) != {path.name for path in anchor_paths.values()}:
        raise InputOffRunError("global P2 inode-anchor history is incomplete or unexpected")
    slot_names = {
        "reservation_anchor": "reservation_slot",
        "burn_guard_anchor": "burn_guard",
        "failure_receipt_anchor": "failure_receipt_slot",
    }
    for anchor_name, anchor_path in anchor_paths.items():
        anchor_identity = _inode_identity(
            anchor_path,
            directory=False,
            label=f"P2 {anchor_name}",
            expected_nlink=2,
        )
        slot_identity = binding[slot_names[anchor_name]]
        if (
            binding.get(anchor_name) != anchor_identity
            or anchor_identity["st_dev"] != slot_identity["st_dev"]
            or anchor_identity["st_ino"] != slot_identity["st_ino"]
        ):
            raise InputOffRunError(f"global P2 {anchor_name} identity differs")
    reservation_document = _read_json(reservation_path, "global P2 reservation")
    expected_document = _reservation_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        binding=binding,
        reserved_at=str(reservation_document.get("reserved_at", "")),
    )
    expected_reservation = {
        **_file_evidence_with_identity(reservation_path, "global P2 reservation", expected_nlink=2),
        "document": expected_document,
        "privileged_seal_response": reservation.get("privileged_seal_response"),
    }
    seal_request = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "expected_identity": binding["reservation_slot"],
            "document": expected_document,
        },
    )
    try:
        global_ledger.validate_response(seal_request, reservation.get("privileged_seal_response"))
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 reservation seal is invalid: {error}") from error
    reservation_stat = reservation_path.stat()
    if (
        reservation_document != expected_document
        or dict(reservation) != expected_reservation
        or stat.S_IMODE(reservation_stat.st_mode) != GLOBAL_LEDGER_SEALED_FILE_MODE
    ):
        raise InputOffRunError("global P2 reservation document differs")
    if reserve_response["evidence"]["ledger_directory"] != binding.get("ledger_directory"):
        raise InputOffRunError("shared P2 ledger directory binding differs")
    guard_stat = guard_path.stat()
    failure_stat = failure_path.stat()
    if require_prepared_guard and (
        guard_stat.st_size != 0
        or stat.S_IMODE(guard_stat.st_mode) != GLOBAL_LEDGER_PREPARED_SLOT_MODE
        or marker_path.exists()
        or marker_path.is_symlink()
        or failure_stat.st_size != 0
        or stat.S_IMODE(failure_stat.st_mode) != GLOBAL_LEDGER_PREPARED_SLOT_MODE
    ):
        raise InputOffRunError("global P2 run ID is already consumed or failed")
    return {"binding": dict(binding), "reservation": expected_reservation}


def _burn_marker_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    reservation: Mapping[str, Any],
    guard: Mapping[str, Any],
    consumed_at: str,
    execution_nonce: str,
) -> dict[str, Any]:
    binding = reservation["binding"]
    return {
        "schema": 1,
        "marker_kind": "5g8_input_off_global_execution_consumed_v1",
        "reservation_id": binding["reservation_id"],
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "execution_nonce": execution_nonce,
        "consumed_at": consumed_at,
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "global_ledger_authority": binding["global_ledger_authority"],
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            binding["global_ledger_authority"]
        ),
        "canonical_run_identity_sha256": binding["canonical_run_identity_sha256"],
        "ledger_key": binding["ledger_key"],
        "reservation_sha256": reservation["reservation"]["sha256"],
        "burn_guard_sha256": guard["sha256"],
        "burn_guard_size_bytes": guard["size_bytes"],
        "run_id_burned_before_source_dependency_fixture_or_hardware_access": True,
        "consumed_to_prepared_transition_forbidden": True,
    }


def _validate_global_burn(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    reservation: Mapping[str, Any],
    value: object,
    ledger_backend: global_ledger.LedgerBackend,
    burn_response: object | None = None,
) -> dict[str, Any]:
    validated_reservation = _validate_global_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        value=reservation,
        require_prepared_guard=False,
        ledger_backend=ledger_backend,
    )
    binding = validated_reservation["binding"]
    authority = binding["global_ledger_authority"]
    try:
        inspection = ledger_backend.inspect(authority)
        global_ledger.validate_inspection_evidence(authority, inspection)
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 burn inspection failed: {error}") from error
    if inspection.get("classification") != "burn_complete":
        raise InputOffRunError("global P2 execution burn is not fully committed")
    guard_record = inspection.get("burn_guard")
    marker_record = inspection.get("execution_marker")
    failure_record = inspection.get("failure_receipt")
    if (
        not isinstance(guard_record, Mapping)
        or not isinstance(marker_record, Mapping)
        or not isinstance(failure_record, Mapping)
        or guard_record.get("state") != "consumed"
        or marker_record.get("state") != "sealed"
        or failure_record.get("state") != "prepared"
        or not isinstance(guard_record.get("evidence"), Mapping)
        or not isinstance(marker_record.get("evidence"), Mapping)
        or not isinstance(marker_record.get("document"), Mapping)
    ):
        raise InputOffRunError("global P2 burn inspection components are malformed")
    guard = dict(guard_record["evidence"])
    marker = dict(marker_record["document"])
    execution_nonce = inspection.get("execution_nonce")
    if not isinstance(execution_nonce, str):
        raise InputOffRunError("global P2 execution burn lacks its nonce")
    expected_marker = _burn_marker_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation=validated_reservation,
        guard=guard,
        consumed_at=str(marker.get("consumed_at", "")),
        execution_nonce=execution_nonce,
    )
    if isinstance(value, Mapping) and burn_response is None:
        burn_response = value.get("privileged_burn_response")
    burn_request = global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": binding["burn_guard"],
            "document": expected_marker,
        },
    )
    try:
        normalized_burn_response = global_ledger.validate_response(burn_request, burn_response)
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 burn response is invalid: {error}") from error
    evidence = {
        "schema": 1,
        "evidence_kind": "5g8_input_off_global_execution_burn_v2",
        "global_run_reservation": validated_reservation,
        "execution_nonce": execution_nonce,
        "burn_guard": guard,
        "privileged_burn_response": normalized_burn_response,
        "burn_marker": {
            **dict(marker_record["evidence"]),
            "document": expected_marker,
        },
        "authoritative_post_burn_inspection": dict(inspection),
        "burn_completed_before_source_dependency_fixture_or_hardware_access": True,
    }
    if marker != expected_marker or (
        value is not None and (not isinstance(value, Mapping) or dict(value) != evidence)
    ):
        raise InputOffRunError("global P2 execution-burn evidence differs")
    return evidence


def _burn_global_run(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    reservation: Mapping[str, Any],
    progress: dict[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    validated = _validate_global_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        value=reservation,
        require_prepared_guard=True,
        ledger_backend=ledger_backend,
    )
    progress["reservation"] = validated
    binding = validated["binding"]
    progress["guard_transition_started"] = True
    execution_nonce = uuid.uuid4().hex
    progress["execution_nonce"] = execution_nonce
    guard = {
        **dict(binding["burn_guard"]),
        "size_bytes": 1,
        "mode": GLOBAL_LEDGER_SEALED_FILE_MODE,
        "nlink": 2,
        "sha256": hashlib.sha256(b"\x01").hexdigest(),
    }
    marker = _burn_marker_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation=validated,
        guard=guard,
        consumed_at=_now(),
        execution_nonce=execution_nonce,
    )
    burn_request = global_ledger.mutation_request(
        authority=binding["global_ledger_authority"],
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": binding["burn_guard"],
            "document": marker,
        },
    )
    try:
        burn_response = global_ledger.validate_response(
            burn_request, ledger_backend.mutate(burn_request)
        )
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 atomic execution burn failed: {error}") from error
    progress["guard_consumed"] = True
    progress["burn_guard"] = dict(burn_response["evidence"]["guard"])
    progress["burn_marker_persisted"] = True
    burn = _validate_global_burn(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation=validated,
        value=None,
        ledger_backend=ledger_backend,
        burn_response=burn_response,
    )
    progress["global_execution_burn"] = burn
    return burn


def _failure_receipt_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    reservation: Mapping[str, Any],
    progress: Mapping[str, Any],
    original_error: BaseException,
    cleanup_errors: Sequence[Mapping[str, str]],
    quarantine: Mapping[str, Any] | None,
    local_failure_tombstone: Mapping[str, Any] | None,
    failed_at: str,
) -> dict[str, Any]:
    binding = reservation["binding"]
    return {
        "schema": 1,
        "marker_kind": "5g8_input_off_global_failure_receipt_v1",
        "reservation_id": binding["reservation_id"],
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "failed_at": failed_at,
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "global_run_reservation": dict(reservation),
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            binding["global_ledger_authority"]
        ),
        "global_execution_burn": progress.get("global_execution_burn"),
        **(
            {"execution_nonce": progress["execution_nonce"]}
            if isinstance(progress.get("execution_nonce"), str)
            else {}
        ),
        "guard_transition_started": progress.get("guard_transition_started") is True,
        "guard_consumed": progress.get("guard_consumed") is True,
        "burn_marker_persisted": progress.get("burn_marker_persisted") is True,
        "original_error": _error_document(original_error),
        "cleanup_errors": [dict(item) for item in cleanup_errors],
        "quarantine": dict(quarantine) if quarantine is not None else None,
        "local_failure_tombstone": (
            dict(local_failure_tombstone) if local_failure_tombstone is not None else None
        ),
        "accepted_artifact": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }


def _seal_global_failure_receipt(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    reservation: Mapping[str, Any],
    progress: Mapping[str, Any],
    original_error: BaseException,
    cleanup_errors: Sequence[Mapping[str, str]],
    quarantine: Mapping[str, Any] | None,
    local_failure_tombstone: Mapping[str, Any] | None,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    binding = reservation["binding"]
    path = Path(str(binding["failure_receipt_slot"]["path"]))
    document = _failure_receipt_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        reservation=reservation,
        progress=progress,
        original_error=original_error,
        cleanup_errors=cleanup_errors,
        quarantine=quarantine,
        local_failure_tombstone=local_failure_tombstone,
        failed_at=_now(),
    )
    request = global_ledger.mutation_request(
        authority=binding["global_ledger_authority"],
        operation="seal_slot",
        payload={
            "slot": "failure",
            "document": document,
            "expected_identity": binding["failure_receipt_slot"],
        },
    )
    try:
        response = global_ledger.validate_response(request, ledger_backend.mutate(request))
    except global_ledger.GlobalLedgerError as error:
        raise InputOffRunError(f"shared P2 failure-receipt sealing failed: {error}") from error
    return {
        **_file_evidence_with_identity(path, "global P2 failure receipt", expected_nlink=2),
        "document": document,
        "privileged_seal_response": response,
    }


def _verify_file_evidence(evidence: Mapping[str, Any], label: str) -> None:
    if set(evidence) != {"path", "sha256", "size_bytes"}:
        raise InputOffRunError(f"{label} file binding is incomplete or unexpected")
    path = Path(str(evidence["path"]))
    _assert_no_symlink_chain(path, label)
    if path.is_symlink() or not path.is_file():
        raise InputOffRunError(f"{label} evidence path is not a regular file")
    if path.stat().st_size != evidence["size_bytes"] or sha256_path(path) != evidence["sha256"]:
        raise InputOffRunError(f"{label} evidence bytes differ from the fixture")


def _file_evidence(path: Path) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "evidence")
    if exact.is_symlink() or not exact.is_file():
        raise InputOffRunError(f"evidence path is not a regular file: {exact}")
    return {"path": str(exact), "sha256": sha256_path(exact), "size_bytes": exact.stat().st_size}


def _validate_fast20_live_image(
    evidence: Mapping[str, Any],
    *,
    campaign_id: str,
    board_id: str,
) -> dict[str, Any]:
    _verify_file_evidence(evidence, "Fast20 live-image evidence")
    path = Path(str(evidence["path"]))
    document = _read_json(path, "Fast20 live-image evidence")
    if document.get("evidence_kind") != SEALED_SELECTOR_EVIDENCE_KIND:
        raise InputOffRunError("P2 requires recursively sealed Fast20 live-image evidence")
    flash_run_id = document.get("run_id")
    if not isinstance(flash_run_id, str):
        raise InputOffRunError("sealed Fast20 evidence has no flash run ID")
    _validate_identifier(flash_run_id, "Fast20 flash run ID")
    try:
        sealed = validate_sealed_selector_evidence(
            path,
            expected_sha256=str(evidence["sha256"]),
            expected_campaign_id=campaign_id,
            expected_run_id=flash_run_id,
            expected_board_id=board_id,
            expected_image_role="fast20",
        )
    except SelectorFlashError as error:
        raise InputOffRunError(f"sealed Fast20 live-image evidence failed: {error}") from error
    frozen = sealed.get("frozen_inputs")
    startup = sealed.get("startup")
    if not isinstance(frozen, Mapping) or not isinstance(startup, Mapping):
        raise InputOffRunError("sealed Fast20 evidence lacks frozen inputs/startup")
    files = frozen.get("files")
    control_profile = frozen.get("control_profile")
    if (
        not isinstance(files, Mapping)
        or not isinstance(control_profile, Mapping)
        or control_profile.get("id") != "fast20-v1"
        or startup.get("evidence_kind") != "fast20_exact_image_reset_run_identity_v1"
    ):
        raise InputOffRunError("sealed evidence is not the reviewed Fast20 image")
    profile = files.get("profile")
    firmware = files.get("firmware_bin")
    if not isinstance(profile, Mapping) or not isinstance(firmware, Mapping):
        raise InputOffRunError("sealed Fast20 evidence lacks profile/firmware file bindings")
    _verify_file_evidence(profile, "sealed Fast20 profile")
    _verify_file_evidence(firmware, "sealed Fast20 firmware")
    target = sealed.get("target_flash_readback")
    if not isinstance(target, Mapping):
        raise InputOffRunError("sealed Fast20 evidence lacks target readback")
    target_file = {name: target.get(name) for name in ("path", "sha256", "size_bytes")}
    _verify_file_evidence(target_file, "sealed Fast20 target readback")
    return {
        "schema": 1,
        "binding_kind": "sealed_fast20_live_image_v1",
        "evidence": dict(evidence),
        "campaign_id": campaign_id,
        "flash_run_id": flash_run_id,
        "board_id": board_id,
        "image_role": "fast20",
        "profile": dict(profile),
        "firmware_bin": dict(firmware),
        "target_readback": target_file,
    }


def _repository_source_attestation(repository: Path = _REPOSITORY) -> dict[str, Any]:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if GIT_COMMIT.fullmatch(head) is None:
        raise InputOffRunError("Smateway HEAD is not a full Git object ID")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise InputOffRunError("Smateway must be clean before P2 planning or execution")
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
        "source_files_sha256": canonical_sha256(files["files"]),
    }


def _safe_local_state_root(path: Path) -> Path:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "state root")
    forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or root in exact.parents for root in forbidden):
        raise InputOffRunError(
            "state root must be local Raspberry Pi storage, not Pluto/removable storage"
        )
    try:
        assert_local_rpi_storage(exact, label="state root")
    except FileArtifactAdmissionError as error:
        raise InputOffRunError(str(error)) from error
    return exact


def _require_local_storage_contract(
    contract: Mapping[str, Any], *, run_root: Path
) -> tuple[Path, Path]:
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise InputOffRunError("plan local-storage contract is missing")
    raw_run = storage.get("run_root")
    raw_capture = storage.get("capture_root")
    raw_state = storage.get("state_root")
    local_device = storage.get("local_storage_device")
    if (
        storage.get("local_rpi_only") is not True
        or storage.get("pluto_storage_forbidden") is not True
        or not isinstance(raw_run, str)
        or not Path(raw_run).is_absolute()
        or not isinstance(raw_capture, str)
        or not Path(raw_capture).is_absolute()
        or not isinstance(raw_state, str)
        or not Path(raw_state).is_absolute()
        or isinstance(local_device, bool)
        or not isinstance(local_device, int)
        or local_device != Path("/home/pi").stat().st_dev
    ):
        raise InputOffRunError("plan local-storage contract is malformed")
    exact_run = Path(raw_run).expanduser().absolute()
    exact_capture = Path(raw_capture).expanduser().absolute()
    exact_state = Path(raw_state).expanduser().absolute()
    if exact_run != run_root.expanduser().absolute():
        raise InputOffRunError("plan run root differs from immutable plan location")
    if exact_state not in exact_run.parents or exact_state not in exact_capture.parents:
        raise InputOffRunError("run/capture roots escape the immutable local state root")
    try:
        assert_local_rpi_storage(exact_run, label="run storage")
        assert_local_rpi_storage(exact_capture, label="capture storage")
    except FileArtifactAdmissionError as error:
        raise InputOffRunError(str(error)) from error
    return exact_run, exact_capture


def _fixture_evidence(
    fixture_path: Path,
    setup_path: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
) -> dict[str, Any]:
    fixture_file = _file_evidence(fixture_path)
    fixture = validate_fixture_v2(
        _read_json(fixture_path, "P2 fixture-v2 manifest"),
        run_id=run_id,
        board_id=board_id,
        serial=serial,
    )
    setup_file = _file_evidence(setup_path)
    setup = validate_setup_attestation(
        _read_json(setup_path, "P2 setup attestation"),
        fixture=fixture,
        fixture_file_sha256=str(fixture_file["sha256"]),
        run_id=run_id,
    )
    for label, evidence in (
        ("baseline topology", fixture["baseline_topology_evidence"]),
        ("Fast20 profile", fixture["fast20_control"]["profile"]),
        ("Fast20 live image", fixture["fast20_control"]["live_image_evidence"]),
        ("run setup", setup["setup_evidence"]),
    ):
        _verify_file_evidence(evidence, label)
    fast20_control = fixture["fast20_control"]
    if not isinstance(fast20_control, Mapping):
        raise InputOffRunError("P2 fixture Fast20 control is malformed")
    sealed_fast20 = _validate_fast20_live_image(
        fast20_control["live_image_evidence"],
        campaign_id=str(fixture["campaign_id"]),
        board_id=board_id,
    )
    if sealed_fast20["profile"] != fast20_control["profile"]:
        raise InputOffRunError("sealed Fast20 image and fixture bind different profiles")
    characterization_documents = [
        component["characterization"] for component in fixture["components"].values()
    ] + [
        connection["interconnect"]["characterization"]
        for connection in fixture["connections"].values()
    ]
    rx2_attenuator = fixture["rx2_attenuator"]
    if rx2_attenuator["state"] == "present":
        characterization_documents.extend(
            (
                rx2_attenuator["component"]["characterization"],
                rx2_attenuator["pluto_connection"]["interconnect"]["characterization"],
            )
        )
    for index, characterization in enumerate(characterization_documents):
        if characterization["status"] != "characterized":
            continue
        characterization_path = Path(str(characterization["evidence_path"]))
        _assert_no_symlink_chain(characterization_path, "fixture characterization")
        if characterization_path.is_symlink() or not characterization_path.is_file():
            raise InputOffRunError(
                f"fixture characterization {index} evidence is not a regular file"
            )
        if sha256_path(characterization_path) != characterization["evidence_sha256"]:
            raise InputOffRunError(f"fixture characterization {index} evidence hash differs")
    normalized = {
        "schema": 2,
        "fixture": fixture,
        "setup_attestation": setup,
        "sealed_fast20_live_image": sealed_fast20,
        "source_files": {"fixture_manifest": fixture_file, "setup_attestation": setup_file},
    }
    normalized["fixture_evidence_sha256"] = canonical_sha256(normalized)
    return normalized


def _p0_bindings(
    paths: Sequence[Path],
    *,
    expected_profile_sha256: str,
    expected_source_commit: str,
    expected_dependency_attestation: Mapping[str, Any],
    expected_native_attestation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if len(paths) != 5:
        raise InputOffRunError("P2 planning requires exactly five normalized P0 observations")
    bindings: list[dict[str, Any]] = []
    observations = []
    for path in paths:
        try:
            document, binding = admit_normalized_p0_evidence(
                path,
                expected_normalizer_repository=_REPOSITORY,
                expected_normalizer_commit=expected_source_commit,
                required_source_paths=SOURCE_FILES,
                expected_dependency_attestation=expected_dependency_attestation,
                expected_native_attestation=expected_native_attestation,
            )
        except (FileArtifactAdmissionError, P0NormalizedEvidenceError) as error:
            raise InputOffRunError(f"normalized P0 recursive admission failed: {error}") from error
        observation = validate_observation(document, expected_cohort="P0")
        if observation.profile_contract_sha256 != expected_profile_sha256:
            raise InputOffRunError("P0 observation profile differs from the P2 Fast20 profile")
        if observation.source_commit != expected_source_commit:
            raise InputOffRunError("P0 observation predates the frozen P2 tooling revision")
        observations.append(observation)
        bindings.append(binding)
    for label, values in (
        ("run IDs", [item.run_id for item in observations]),
        ("artifact IDs", [item.artifact_id for item in observations]),
        ("stream IDs", [item.stream_id for item in observations]),
    ):
        if len(set(values)) != 5:
            raise InputOffRunError(f"P0 {label} are not source-distinct")
    validate_p0_cohort(observations)
    return bindings


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    profile_path: Path,
    fixture_evidence: Mapping[str, Any],
    p0_bindings: Sequence[Mapping[str, Any]],
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    native_attestation: Mapping[str, Any],
    state_root: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> dict[str, Any]:
    _validate_identifier(run_id, "run ID")
    _validate_identifier(board_id, "board ID")
    _validate_identifier(serial, "Pluto serial")
    if USB_URI.fullmatch(uri) is None:
        raise InputOffRunError("P2 requires an explicit current usb: URI")
    profile_file = _file_evidence(profile_path)
    profile = load_profile(Path(profile_file["path"]))
    fixture_profile = fixture_evidence["fixture"]["fast20_control"]["profile"]
    if fixture_profile != profile_file:
        raise InputOffRunError("CLI Fast20 profile differs from fixture profile evidence")
    normalized_native = validate_runtime_attestation(native_attestation)
    source_commit = source_attestation.get("commit")
    if not isinstance(source_commit, str) or GIT_COMMIT.fullmatch(source_commit) is None:
        raise InputOffRunError("source attestation has no valid commit")
    exact_state = _safe_local_state_root(state_root)
    run_root = exact_state / "boards" / board_id / "5g8-input-off-control" / run_id
    capture_root = (
        exact_state / "boards" / board_id / "pluto-usb-captures" / "input-off-runs" / run_id
    )
    authority = _shared_new_global_ledger_authority(
        board_id=board_id,
        run_id=run_id,
        run_root=run_root,
        capture_root=capture_root,
        state_root=exact_state,
        ledger_backend=ledger_backend,
    )
    return {
        "schema": 1,
        "run_kind": RUN_KIND,
        "campaign_id": CAMPAIGN_ID,
        "run_id": run_id,
        "board_id": board_id,
        "topology_stage": TOPOLOGY_STAGE,
        "topology_token": TOPOLOGY_TOKEN,
        "configuration": {"serial": serial, "uri": uri},
        "acquisition": acquisition_contract(),
        "profile": {
            **profile_file,
            "profile_id": profile.profile_id,
            "revision": profile.revision,
            "contract_sha256": profile.contract_sha256,
        },
        "fixture_evidence": _json_safe(fixture_evidence),
        "fixture_evidence_sha256": fixture_evidence["fixture_evidence_sha256"],
        "p0_baseline_bindings": _json_safe(list(p0_bindings)),
        "source": {
            "smateway": _json_safe(source_attestation),
            "pluto_plus_utils": _json_safe(dependency_attestation),
            "native_libiio": normalized_native,
            "native_libiio_sha256": attestation_sha256(normalized_native),
        },
        "storage": {
            "local_rpi_only": True,
            "state_root": str(exact_state),
            "run_root": str(run_root),
            "capture_root": str(capture_root),
            "local_storage_device": int(Path("/home/pi").stat().st_dev),
            "pluto_storage_forbidden": True,
        },
        "execution": {
            "one_stream_per_run": True,
            "automatic_retry": False,
            "failed_run_id_burned": True,
            "artifact_persistence_only_after_exact_final_mute": True,
            "legacy_fully_conducted_label_forbidden": True,
            "global_run_ledger_authority": authority,
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


def _new_manifest(
    plan_path: Path,
    envelope: Mapping[str, Any],
    *,
    global_reservation: Mapping[str, Any],
) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    return {
        "schema": 1,
        "run_kind": RUN_KIND,
        "run_id": contract["run_id"],
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_path(plan_path),
            "contract_sha256": envelope["plan_contract_sha256"],
        },
        "global_run_reservation": dict(global_reservation),
        "global_execution_burn": None,
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
    run_root, capture_root = _require_local_storage_contract(contract, run_root=plan_path.parent)
    envelope = _plan_envelope(contract)
    if (
        run_root.exists()
        or run_root.is_symlink()
        or capture_root.exists()
        or capture_root.is_symlink()
    ):
        if (
            run_root.is_dir()
            and not run_root.is_symlink()
            and plan_path.is_file()
            and manifest_path.is_file()
            and not plan_path.is_symlink()
            and not manifest_path.is_symlink()
        ):
            observed = _read_json(plan_path, "immutable plan")
            manifest = _read_json(manifest_path, "run manifest")
            if observed != envelope or manifest.get("status") != "prepared":
                raise InputOffRunError("existing run ID is not an intact matching prepared plan")
            _validate_global_reservation(
                contract=contract,
                plan_path=plan_path,
                manifest_path=manifest_path,
                value=manifest.get("global_run_reservation"),
                require_prepared_guard=True,
                ledger_backend=ledger_backend,
            )
            if Path(str(contract["storage"]["capture_root"])).exists():
                raise InputOffRunError("prepared run unexpectedly has capture history")
            return observed, manifest
        raise InputOffRunError("run ID has prior plan, execution, tombstone, or capture history")
    _write_immutable_json(plan_path, envelope)
    binding = _new_global_reservation_binding(
        contract, plan_path=plan_path, ledger_backend=ledger_backend
    )
    global_reservation = _seal_global_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        binding=binding,
        ledger_backend=ledger_backend,
    )
    manifest = _new_manifest(
        plan_path,
        envelope,
        global_reservation=global_reservation,
    )
    write_json_atomic(manifest_path, manifest)
    return envelope, manifest


def _validate_execution_confirmations(args: argparse.Namespace) -> dict[str, Any]:
    flags = {
        "no_antennas": args.confirm_no_antennas,
        "two_distinct_terminations": args.confirm_two_distinct_terminations,
        "downstream_unchanged": args.confirm_downstream_unchanged,
        "rx1_protected_reference": args.confirm_rx1_protected_reference,
        "tx2_terminated_muted": args.confirm_tx2_terminated_muted,
        "fast20_live": args.confirm_fast20_live,
        "no_movement": args.confirm_no_movement,
    }
    missing = [name for name, passed in flags.items() if not passed]
    if missing:
        raise InputOffRunError("missing execution confirmations: " + ", ".join(missing))
    if args.confirm_topology_token != TOPOLOGY_TOKEN:
        raise InputOffRunError(f"execution requires --confirm-topology-token {TOPOLOGY_TOKEN}")
    return {"confirmed_at": _now(), "topology_token": TOPOLOGY_TOKEN, **flags}


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
        and value.get("error") is None
    )


def _live_identity(serial: str, uri: str) -> dict[str, Any]:
    resolved = resolve_iio_uri(uri, serial)
    return {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed" if resolved == uri else "failed",
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": resolved,
        "exact_uri_match": resolved == uri,
        "sysfs_path": find_usb_sysfs_path(serial),
        "scan_mutates_radio_state": False,
        "observed_at": _now(),
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


def _tone_plan(contract: Mapping[str, Any]) -> SafeDdsTonePlan:
    configuration = contract["configuration"]
    return SafeDdsTonePlan(
        uri=str(configuration["uri"]),
        serial=str(configuration["serial"]),
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=TX_CHANNEL,
        tx_hardware_gain_db=TX_HARDWARE_GAIN_DB,
        dds_scale=DDS_SCALE,
        receiver_gain_db=RECEIVER_GAIN_DB,
        source_peak_output_bound_dbm=SOURCE_PEAK_OUTPUT_BOUND_DBM,
        load_input_limit_dbm=LOAD_INPUT_LIMIT_DBM,
        path_attenuation_before_load_db=0.0,
        required_margin_db=REQUIRED_MARGIN_DB,
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
        for name in (
            "sample_time_realtime_start_ns",
            "sample_time_realtime_end_ns",
            "sample_time_monotonic_start_ns",
            "sample_time_monotonic_end_ns",
            "sample_time_uncertainty_ns",
        ):
            raw = getattr(block, name)
            if raw is not None:
                record[name] = int(raw)
        records.append(record)
        sample_start += block.sample_count
    if not records:
        raise InputOffRunError("capture returned no ABI2 blocks")
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


def _continuity_blocks(blocks: Sequence[SampleBlockV2]) -> tuple[ContinuityBlock, ...]:
    sample_start = 0
    output = []
    for block in blocks:
        output.append(
            ContinuityBlock(
                sample_start=sample_start,
                sample_count=block.sample_count,
                utc_ns=block.utc_ns,
            )
        )
        sample_start += block.sample_count
    return tuple(output)


def _rf_readback(capture: Any, plan: SafeDdsTonePlan) -> tuple[dict[str, Any], float]:
    evidence = {
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
            "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
        ),
    }
    validate_tx1_rf_readback_evidence(
        evidence,
        planned_kernel_buffers=KERNEL_BUFFERS,
        planned_tx_gain_db=TX_HARDWARE_GAIN_DB,
        planned_dds_scale=DDS_SCALE,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    frequencies = evidence["dds_frequency_readback_hz"]
    tone_readback = (abs(float(frequencies[0])) + abs(float(frequencies[2]))) / 2.0
    return evidence, tone_readback


def _validate_capture(
    capture: Any,
    blocks: Sequence[SampleBlockV2],
    *,
    plan: SafeDdsTonePlan,
    settings: RadioSettings,
) -> tuple[int, dict[str, Any], float, dict[str, Any]]:
    if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
        raise InputOffRunError("capture identity differs from exact serial/current USB URI")
    if capture.settings != settings:
        raise InputOffRunError("capture settings differ from the exact P0-matched contract")
    if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
        raise InputOffRunError("capture sample/frame count differs from 10-second Fast20 plan")
    if capture.kernel_buffers != KERNEL_BUFFERS or len(blocks) != FRAME_COUNT:
        raise InputOffRunError("capture kernel-buffer or retained-frame count differs")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in blocks):
        raise InputOffRunError("capture block is not dual-RX 100k-sample data")
    ledger = _block_ledger(blocks)
    summary = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if summary.metadata_abi != 2 or summary.first_buffer_sequence != 0:
        raise InputOffRunError("capture did not start one fresh ABI2 stream")
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
            raise InputOffRunError("capture proof differs from retained ABI2 frame")
    rf_readback, tone_readback = _rf_readback(capture, plan)
    return summary.stream_id, rf_readback, tone_readback, ledger


def _analyze_blocks(
    blocks: Sequence[SampleBlockV2],
    *,
    profile: Any,
    tone_readback_hz: float,
) -> dict[str, Any]:
    rx1 = np.concatenate([block.samples[0] for block in blocks])
    rx2 = np.concatenate([block.samples[1] for block in blocks])
    continuity = _continuity_blocks(blocks)
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=tone_readback_hz,
    )
    pilot_snr_db = coherent_tone_snr_db(
        rx1,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_hz=pilot.estimated_offset_hz,
    )
    dwell = analyze_fast20_dwell_isolation(
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=continuity,
        minimum_complete_frames=MINIMUM_COMPLETE_FAST20_FRAMES,
    )
    if not dwell.isolation_verified or dwell.schedule_timing is None:
        raise InputOffRunError("Fast20 timing was not independently verified in this stream")
    transfer = analyze_fast20_reference_transfer(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=pilot.estimated_offset_hz,
        profile=profile,
        continuity_ledger=continuity,
        edge_exclusion_bins=EDGE_EXCLUSION_BINS,
        alignment_search_mode=AlignmentSearchMode.TRANSITION_SEEDED,
        decoded_timing=dwell.schedule_timing,
    )
    pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
    all_off = transfer.all_off_raw_rx2_over_rx1
    rejection_reasons: list[str] = []
    checks = (
        (pilot.confidence >= MINIMUM_PILOT_CONFIDENCE, "pilot_confidence_below_minimum"),
        (
            pilot.phase_step_coherence >= MINIMUM_PILOT_PHASE_STEP_COHERENCE,
            "pilot_phase_step_coherence_below_minimum",
        ),
        (pilot_phase_rms_deg <= MAXIMUM_PILOT_PHASE_RMS_DEG, "pilot_phase_rms_above_maximum"),
        (pilot_snr_db >= MINIMUM_PILOT_SNR_DB, "pilot_snr_below_20db"),
        (transfer.complete_cycle_count >= MINIMUM_COMPLETE_FAST20_FRAMES, "too_few_cycles"),
        (transfer.alignment_score >= MINIMUM_ALIGNMENT_SCORE, "alignment_score_below_minimum"),
        (
            transfer.alignment_even_odd_agreement >= MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT,
            "alignment_even_odd_below_minimum",
        ),
        (
            transfer.reference_valid_bin_fraction >= MINIMUM_REFERENCE_VALID_BIN_FRACTION,
            "reference_valid_fraction_below_minimum",
        ),
        (
            transfer.all_off_rx1.cycle_coherence >= MINIMUM_RX1_CYCLE_COHERENCE,
            "rx1_cycle_coherence_below_minimum",
        ),
    )
    rejection_reasons.extend(reason for passed, reason in checks if not passed)
    if rejection_reasons:
        raise InputOffRunError("P2 analysis quality failed: " + ", ".join(rejection_reasons))
    detection_checks = (
        (
            all_off.cycle_coherence >= MINIMUM_ALL_OFF_CYCLE_COHERENCE,
            "all_off_cycle_coherence_below_minimum",
        ),
        (
            all_off.even_odd_phase_agreement >= MINIMUM_ALL_OFF_EVEN_ODD_AGREEMENT,
            "all_off_even_odd_below_minimum",
        ),
        (
            all_off.cycle_phase_std_deg <= MAXIMUM_ALL_OFF_CYCLE_PHASE_STD_DEG,
            "all_off_cycle_phase_std_above_maximum",
        ),
    )
    detection_rejection_reasons = [reason for passed, reason in detection_checks if not passed]
    transfer_detected = not detection_rejection_reasons
    phase_free_upper_bound = (
        None if transfer_detected else phase_free_complex_upper_bound(all_off.cycle_phasors)
    )
    return {
        "pilot": {**_json_safe(asdict(pilot)), "snr_db": pilot_snr_db},
        "dwell": _json_safe(asdict(dwell)),
        "reference_transfer": _json_safe(asdict(transfer)),
        "all_off_transfer": all_off.phasor,
        "all_off_transfer_detected": transfer_detected,
        "all_off_transfer_upper_bound": phase_free_upper_bound,
        "all_off_detection_rejection_reasons": detection_rejection_reasons,
        "rx1_reference_amplitude": transfer.all_off_rx1.amplitude,
        "detected_pilot_snr_db": pilot_snr_db,
        "quality_rejection_reasons": rejection_reasons,
    }


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    root = Path(artifact.path)
    data_file = data_path(artifact)
    metadata_file = root / f"{artifact.artifact_id}.sigmf-meta"
    return {
        "artifact_id": artifact.artifact_id,
        "path": str(root),
        "data_path": str(data_file),
        "data_sha256": sha256_path(data_file),
        "data_size_bytes": data_file.stat().st_size,
        "metadata_path": str(metadata_file),
        "metadata_sha256": sha256_path(metadata_file),
        "metadata_size_bytes": metadata_file.stat().st_size,
    }


def _open_absolute_directory_nofollow(path: Path) -> int:
    exact = path.expanduser().absolute()
    if not exact.is_absolute() or ".." in exact.parts:
        raise InputOffRunError("directory-FD path is not absolute and normalized")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(exact.anchor, flags)
    try:
        for part in exact.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_bound_capture_root(path: Path, *, expected_device: int) -> tuple[int, dict[str, Any]]:
    exact = path.expanduser().absolute()
    if not exact.is_absolute() or ".." in exact.parts or exact.name in {"", ".", ".."}:
        raise InputOffRunError("capture root is not an absolute normalized directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(exact.anchor, flags)
    try:
        for part in exact.parts[1:-1]:
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            observed = os.fstat(next_descriptor)
            if observed.st_dev != expected_device:
                os.close(next_descriptor)
                raise InputOffRunError("capture parent escaped local RPi storage")
            os.close(descriptor)
            descriptor = next_descriptor
        parent_stat = os.fstat(descriptor)
        if parent_stat.st_dev != expected_device:
            raise InputOffRunError("capture parent differs from immutable local device")
        try:
            os.mkdir(exact.name, mode=0o700, dir_fd=descriptor)
        except FileExistsError as error:
            raise InputOffRunError("capture root existed before accepted persistence") from error
        os.fsync(descriptor)
        capture_fd = os.open(exact.name, flags, dir_fd=descriptor)
        capture_stat = os.fstat(capture_fd)
        if capture_stat.st_dev != expected_device:
            os.close(capture_fd)
            raise InputOffRunError("new capture root escaped immutable local storage")
        binding = {
            "schema": 1,
            "binding_kind": "p2_capture_root_directory_fd_v1",
            "path": str(exact),
            "st_dev": int(capture_stat.st_dev),
            "st_ino": int(capture_stat.st_ino),
            "parent_st_dev": int(parent_stat.st_dev),
            "parent_st_ino": int(parent_stat.st_ino),
            "created_via_parent_dirfd_no_follow": True,
            "local_storage_device": expected_device,
        }
        return capture_fd, binding
    finally:
        os.close(descriptor)


def _validate_capture_root_binding(value: object) -> tuple[int, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise InputOffRunError("capture-root directory-FD binding is missing")
    binding = dict(value)
    path_value = binding.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise InputOffRunError("capture-root binding path is malformed")
    path = Path(path_value).expanduser().absolute()
    try:
        descriptor = _open_absolute_directory_nofollow(path)
    except OSError as error:
        raise InputOffRunError(
            "capture-root path cannot be reopened without following links"
        ) from error
    try:
        observed = os.fstat(descriptor)
        parent_fd = _open_absolute_directory_nofollow(path.parent)
        try:
            parent = os.fstat(parent_fd)
        finally:
            os.close(parent_fd)
        expected = {
            "schema": 1,
            "binding_kind": "p2_capture_root_directory_fd_v1",
            "path": str(path),
            "st_dev": int(observed.st_dev),
            "st_ino": int(observed.st_ino),
            "parent_st_dev": int(parent.st_dev),
            "parent_st_ino": int(parent.st_ino),
            "created_via_parent_dirfd_no_follow": True,
            "local_storage_device": int(observed.st_dev),
        }
        if (
            binding != expected
            or observed.st_dev != Path("/home/pi").stat().st_dev
            or not stat.S_ISDIR(observed.st_mode)
        ):
            raise InputOffRunError("capture-root path/device/inode binding changed")
        return descriptor, expected
    except BaseException:
        os.close(descriptor)
        raise


def _directory_names(descriptor: int) -> set[str]:
    return set(os.listdir(descriptor))


def _open_child_directory(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def _seal_directory_tree(descriptor: int, *, directory_mode: int = 0o500) -> None:
    for name in os.listdir(descriptor):
        observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(observed.st_mode):
            raise InputOffRunError("P2 artifact/quarantine tree contains a symlink")
        if stat.S_ISDIR(observed.st_mode):
            child = _open_child_directory(descriptor, name)
            try:
                _seal_directory_tree(child, directory_mode=directory_mode)
            finally:
                os.close(child)
        elif stat.S_ISREG(observed.st_mode):
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                os.fchmod(file_fd, 0o400)
            finally:
                os.close(file_fd)
        else:
            raise InputOffRunError("P2 artifact/quarantine tree has a special file")
    os.fchmod(descriptor, directory_mode)
    os.fsync(descriptor)


def _validate_accepted_capture_inventory(capture_fd: int, *, artifact_id: str) -> dict[str, Any]:
    names = _directory_names(capture_fd)
    if names - {artifact_id, ".partial", ".failed"}:
        raise InputOffRunError("P2 capture root contains an extra sibling artifact")
    if artifact_id not in names:
        raise InputOffRunError("P2 capture root lacks its exact accepted artifact")
    for optional in (".partial", ".failed"):
        if optional in names:
            child = _open_child_directory(capture_fd, optional)
            try:
                if _directory_names(child):
                    raise InputOffRunError(f"P2 successful capture has nonempty {optional}")
            finally:
                os.close(child)
    artifact_fd = _open_child_directory(capture_fd, artifact_id)
    try:
        expected = {
            f"{artifact_id}.sigmf-data",
            f"{artifact_id}.sigmf-meta",
            OBSERVATION_FILENAME,
            "5g8-input-off-condition.json",
        }
        if _directory_names(artifact_fd) != expected:
            raise InputOffRunError("P2 artifact inventory is incomplete or unexpected")
        observed = os.fstat(artifact_fd)
        return {
            "path": artifact_id,
            "st_dev": int(observed.st_dev),
            "st_ino": int(observed.st_ino),
        }
    finally:
        os.close(artifact_fd)


def _quarantine_artifact_by_binding(
    *,
    capture_binding: Mapping[str, Any],
    artifact_id: str,
    error: BaseException,
    artifact_directory_identity: Mapping[str, Any] | None = None,
    capture_fd: int | None = None,
) -> dict[str, Any]:
    expected_artifact_identity = (
        dict(artifact_directory_identity) if artifact_directory_identity is not None else None
    )
    if expected_artifact_identity is not None and (
        set(expected_artifact_identity) != {"path", "st_dev", "st_ino"}
        or expected_artifact_identity.get("path") != artifact_id
    ):
        raise InputOffRunError("P2 quarantine artifact-directory binding is malformed")

    def prepare_bound_artifact_for_move(parent_fd: int) -> None:
        artifact_fd = _open_child_directory(parent_fd, artifact_id)
        try:
            observed_artifact = os.fstat(artifact_fd)
            if expected_artifact_identity is not None and (
                int(observed_artifact.st_dev),
                int(observed_artifact.st_ino),
            ) != (
                expected_artifact_identity.get("st_dev"),
                expected_artifact_identity.get("st_ino"),
            ):
                raise InputOffRunError("P2 finalized artifact directory binding changed")
            os.fchmod(artifact_fd, 0o700)
            os.fsync(artifact_fd)
        finally:
            os.close(artifact_fd)

    if capture_fd is None:
        active_fd, binding = _validate_capture_root_binding(capture_binding)
    else:
        active_fd = os.dup(capture_fd)
        observed = os.fstat(active_fd)
        binding = dict(capture_binding)
        if (observed.st_dev, observed.st_ino) != (
            binding.get("st_dev"),
            binding.get("st_ino"),
        ):
            os.close(active_fd)
            raise InputOffRunError("held P2 capture directory FD changed identity")
    try:
        names = _directory_names(active_fd)
        if ".failed" not in names:
            os.mkdir(".failed", mode=0o700, dir_fd=active_fd)
        failed_fd = _open_child_directory(active_fd, ".failed")
        try:
            failed_names = _directory_names(failed_fd)
            if artifact_id in names:
                if artifact_id in failed_names:
                    raise InputOffRunError("P2 quarantine destination already exists")
                prepare_bound_artifact_for_move(active_fd)
                os.rename(artifact_id, artifact_id, src_dir_fd=active_fd, dst_dir_fd=failed_fd)
            elif ".partial" in names:
                partial_fd = _open_child_directory(active_fd, ".partial")
                try:
                    if artifact_id not in _directory_names(partial_fd):
                        if artifact_id not in failed_names:
                            raise InputOffRunError(
                                "uncommitted P2 artifact is absent from bound capture root"
                            )
                    elif artifact_id in failed_names:
                        raise InputOffRunError("P2 quarantine destination already exists")
                    else:
                        prepare_bound_artifact_for_move(partial_fd)
                        os.rename(
                            artifact_id,
                            artifact_id,
                            src_dir_fd=partial_fd,
                            dst_dir_fd=failed_fd,
                        )
                finally:
                    os.close(partial_fd)
            elif artifact_id not in failed_names:
                raise InputOffRunError("uncommitted P2 artifact is absent from bound capture root")
            os.fsync(active_fd)
            os.fsync(failed_fd)
            artifact_fd = _open_child_directory(failed_fd, artifact_id)
            try:
                bound_artifact = os.fstat(artifact_fd)
                if expected_artifact_identity is not None and (
                    int(bound_artifact.st_dev),
                    int(bound_artifact.st_ino),
                ) != (
                    expected_artifact_identity.get("st_dev"),
                    expected_artifact_identity.get("st_ino"),
                ):
                    raise InputOffRunError("P2 finalized artifact directory binding changed")
                os.fchmod(artifact_fd, 0o700)
                failure_document = {
                    "schema": 1,
                    "evidence_kind": "p2_uncommitted_artifact_quarantine_v1",
                    "artifact_id": artifact_id,
                    "quarantined_at": _now(),
                    "error": _error_document(error),
                    "accepted": False,
                }
                failure_wire = (
                    json.dumps(failure_document, indent=2, sort_keys=True, allow_nan=False) + "\n"
                ).encode()
                failure_name = (
                    "runner-failure.json"
                    if "failure.json" in _directory_names(artifact_fd)
                    else "failure.json"
                )
                failure_fd = os.open(
                    failure_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                    dir_fd=artifact_fd,
                )
                try:
                    view = memoryview(failure_wire)
                    while view:
                        written = os.write(failure_fd, view)
                        if written <= 0:
                            raise InputOffRunError("short write in P2 quarantine")
                        view = view[written:]
                    os.fsync(failure_fd)
                finally:
                    os.close(failure_fd)
                files: list[dict[str, Any]] = []
                for name in sorted(_directory_names(artifact_fd)):
                    observed = os.stat(name, dir_fd=artifact_fd, follow_symlinks=False)
                    if not stat.S_ISREG(observed.st_mode):
                        raise InputOffRunError("P2 quarantine inventory contains non-files")
                    file_fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=artifact_fd,
                    )
                    try:
                        digest = hashlib.sha256()
                        while chunk := os.read(file_fd, 1 << 20):
                            digest.update(chunk)
                    finally:
                        os.close(file_fd)
                    files.append(
                        {
                            "name": name,
                            "sha256": digest.hexdigest(),
                            "size_bytes": int(observed.st_size),
                        }
                    )
                _seal_directory_tree(artifact_fd)
                artifact_stat = os.fstat(artifact_fd)
            finally:
                os.close(artifact_fd)
        finally:
            os.close(failed_fd)
        _seal_directory_tree(active_fd)
        return {
            "schema": 1,
            "evidence_kind": "p2_uncommitted_artifact_quarantine_receipt_v1",
            "capture_root_binding": binding,
            "artifact_id": artifact_id,
            "path": str(Path(str(binding["path"])) / ".failed" / artifact_id),
            "st_dev": int(artifact_stat.st_dev),
            "st_ino": int(artifact_stat.st_ino),
            "files": files,
            "files_sha256": canonical_sha256(files),
            "readonly_tree_verified": True,
            "accepted": False,
        }
    finally:
        os.close(active_fd)


def _attach_quarantine(error: BaseException, quarantine: Mapping[str, Any]) -> None:
    error.__dict__["p2_quarantine"] = dict(quarantine)


_FINALIZED_ARTIFACT_CONTEXT_KEY = "finalized_artifact_binding"


def _publish_finalized_artifact_binding(
    execution_context: MutableMapping[str, Any],
    *,
    capture_root_binding: Mapping[str, Any],
    artifact_id: str,
    artifact_directory_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish the durable artifact identity before returning across the capture boundary."""

    if _FINALIZED_ARTIFACT_CONTEXT_KEY in execution_context:
        raise InputOffRunError("P2 finalized artifact binding was published more than once")
    identity = dict(artifact_directory_identity)
    if (
        IDENTIFIER.fullmatch(artifact_id) is None
        or set(identity) != {"path", "st_dev", "st_ino"}
        or identity.get("path") != artifact_id
        or type(identity.get("st_dev")) is not int
        or type(identity.get("st_ino")) is not int
    ):
        raise InputOffRunError("P2 finalized artifact directory identity is malformed")
    binding = dict(capture_root_binding)
    if type(binding.get("st_dev")) is not int or identity.get("st_dev") != binding.get("st_dev"):
        raise InputOffRunError("P2 finalized artifact escaped its bound capture device")
    finalized = {
        "schema": 1,
        "binding_kind": "p2_finalized_artifact_execution_context_v1",
        "capture_root_binding": binding,
        "artifact_id": artifact_id,
        "artifact_directory_identity": identity,
    }
    execution_context[_FINALIZED_ARTIFACT_CONTEXT_KEY] = finalized
    return finalized


def _finalized_artifact_binding_from_context(
    execution_context: Mapping[str, Any],
) -> dict[str, Any] | None:
    value = execution_context.get(_FINALIZED_ARTIFACT_CONTEXT_KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InputOffRunError("P2 finalized artifact execution context is malformed")
    finalized = dict(value)
    if set(finalized) != {
        "schema",
        "binding_kind",
        "capture_root_binding",
        "artifact_id",
        "artifact_directory_identity",
    }:
        raise InputOffRunError("P2 finalized artifact execution context has unexpected fields")
    capture_binding = finalized.get("capture_root_binding")
    artifact_id = finalized.get("artifact_id")
    identity = finalized.get("artifact_directory_identity")
    if (
        finalized.get("schema") != 1
        or finalized.get("binding_kind") != "p2_finalized_artifact_execution_context_v1"
        or not isinstance(capture_binding, Mapping)
        or not isinstance(artifact_id, str)
        or IDENTIFIER.fullmatch(artifact_id) is None
        or not isinstance(identity, Mapping)
        or set(identity) != {"path", "st_dev", "st_ino"}
        or identity.get("path") != artifact_id
        or type(identity.get("st_dev")) is not int
        or type(identity.get("st_ino")) is not int
        or type(capture_binding.get("st_dev")) is not int
        or identity.get("st_dev") != capture_binding.get("st_dev")
    ):
        raise InputOffRunError("P2 finalized artifact execution context is malformed")
    return finalized


def _execute_one_stream(
    contract: Mapping[str, Any],
    *,
    execution_context: MutableMapping[str, Any],
    execution_burn: Mapping[str, Any] | None = None,
    capture_boundary: CaptureBoundary = _live_capture,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
) -> dict[str, Any]:
    serial = str(contract["configuration"]["serial"])
    uri = str(contract["configuration"]["uri"])
    plan = _tone_plan(contract)
    settings = _settings()
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    capture: Any | None = None
    identity: dict[str, Any] | None = None
    runtime_native: Mapping[str, Any] | None = None
    initial_mute: dict[str, Any] | None = None
    post_mute: dict[str, Any] | None = None
    final_mute: dict[str, Any] | None = None
    pending_error: BaseException | None = None
    stream_id: int | None = None
    rf_readback: dict[str, Any] | None = None
    live_ledger: dict[str, Any] | None = None
    headroom: Any | None = None
    analysis: dict[str, Any] | None = None
    tone_readback_hz: float | None = None
    try:
        initial_mute = mute_boundary(serial, "pre_capture_exact_mute")
        if not _mute_passed(initial_mute, serial=serial, purpose="pre_capture_exact_mute"):
            raise InputOffRunError("initial exact-radio mute failed")
        identity = identity_boundary(serial, uri)
        if not _identity_passed(identity, serial=serial, uri=uri):
            raise InputOffRunError("current USB URI/serial identity preflight failed")
        runtime_native = validate_runtime_attestation(native_boundary())
        if runtime_native != contract["source"]["native_libiio"]:
            raise InputOffRunError("runtime native libiio differs from the immutable plan")
        capture = capture_boundary(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
        post_mute = mute_boundary(serial, "post_capture_exact_mute")
        if not _mute_passed(post_mute, serial=serial, purpose="post_capture_exact_mute"):
            raise InputOffRunError("post-capture exact-radio mute failed")
        stream_id, rf_readback, tone_readback_hz, live_ledger = _validate_capture(
            capture,
            retained,
            plan=plan,
            settings=settings,
        )
        monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            monitor.observe(block.samples)
        headroom = monitor.result()
        if not headroom.passed:
            raise InputOffRunError("ADC headroom admission failed")
        profile = load_profile(Path(str(contract["profile"]["path"])))
        analysis = _analyze_blocks(retained, profile=profile, tone_readback_hz=tone_readback_hz)
    except BaseException as error:
        pending_error = error
    finally:
        try:
            final_mute = mute_boundary(serial, "final_acceptance_exact_mute")
        except BaseException as error:
            pending_error = InputOffRunError(
                f"mandatory final exact-radio mute raised {type(error).__name__}: {error}"
            )

    if not _mute_passed(final_mute, serial=serial, purpose="final_acceptance_exact_mute"):
        pending_error = InputOffRunError("mandatory final exact-radio mute failed")
    if pending_error is not None:
        retained.clear()
        raise pending_error
    assert capture is not None
    assert identity is not None
    assert runtime_native is not None
    assert initial_mute is not None
    assert post_mute is not None
    assert stream_id is not None
    assert rf_readback is not None
    assert live_ledger is not None
    assert headroom is not None
    assert analysis is not None
    assert tone_readback_hz is not None
    assert final_mute is not None

    capture_root = Path(str(contract["storage"]["capture_root"])).expanduser().absolute()
    expected_device = int(contract["storage"]["local_storage_device"])
    capture_fd, capture_binding = _create_bound_capture_root(
        capture_root, expected_device=expected_device
    )
    fd_root = Path(f"/proc/self/fd/{capture_fd}")
    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        writer = CaptureWriter(
            fd_root,
            radio=capture.identity,
            settings=settings,
            label="P2 input-drive-off Fast20 5.8 GHz TX1 -20 dB two distinct input loads",
        )
        for block in retained:
            writer.append(block, settings, revision=1)
        live_artifact = writer.finalize()
        retained.clear()
        if not verify_artifact(live_artifact):
            raise InputOffRunError("persisted P2 artifact failed SHA-256 verification")
        metadata = load_metadata(live_artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        if continuity["stream_id"] != stream_id or continuity["metadata_abi"] != 2:
            raise InputOffRunError("persisted ABI2 identity differs from the live stream")
        live_evidence = _artifact_evidence(live_artifact)
        artifact_id = str(live_artifact.artifact_id)
        artifact_root = capture_root / artifact_id
        artifact = live_artifact.model_copy(update={"path": str(artifact_root)})
        artifact_evidence = {
            **live_evidence,
            "path": str(artifact_root),
            "data_path": str(artifact_root / f"{artifact_id}.sigmf-data"),
            "metadata_path": str(artifact_root / f"{artifact_id}.sigmf-meta"),
        }
        source = contract["source"]["smateway"]
        fixture = contract["fixture_evidence"]
        observation = {
            "schema": 1,
            "observation_kind": OBSERVATION_KIND,
            "cohort": "P2",
            "run_id": contract["run_id"],
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "stream_id": stream_id,
                "sha256": artifact_evidence["data_sha256"],
            },
            "acquisition": acquisition_contract(),
            "profile_contract_sha256": contract["profile"]["contract_sha256"],
            "analysis": {
                "transfer_detected": analysis["all_off_transfer_detected"],
                "all_off_transfer": (
                    complex_document(analysis["all_off_transfer"])
                    if analysis["all_off_transfer_detected"]
                    else None
                ),
                "all_off_transfer_upper_bound": analysis["all_off_transfer_upper_bound"],
                "rx1_reference_amplitude": analysis["rx1_reference_amplitude"],
                "detected_pilot_snr_db": analysis["detected_pilot_snr_db"],
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
                "source_commit": source["commit"],
                "source_files_sha256": source["source_files_sha256"],
                "native_attestation_sha256": contract["source"]["native_libiio_sha256"],
                "fixture_evidence_sha256": contract["fixture_evidence_sha256"],
                "fixture_fixed_graph_sha256": fixture["fixture"]["fixed_graph_sha256"],
                "comparable_fixture_group_id": fixture["fixture"]["comparable_fixture_group_id"],
            },
        }
        validate_observation(observation, expected_cohort="P2")
        record = {
            "schema": 1,
            "record_kind": "5g8_input_drive_off_condition_record",
            "created_at": _now(),
            "standalone_record_is_not_acceptance": True,
            "acceptance_authority": "complete plan-bound run manifest",
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": artifact_evidence,
            "capture": {
                "serial": serial,
                "uri": uri,
                "stream_id": stream_id,
                "live_continuity_ledger": live_ledger,
                "persisted_continuity_audit": continuity,
                "rf_readback": rf_readback,
                "adc_headroom_admission": asdict(headroom),
                "tone_offset_hz_readback": tone_readback_hz,
            },
            "analysis": _json_safe(analysis),
            "safety": {
                "initial_mute": initial_mute,
                "post_capture_mute": post_mute,
                "final_acceptance_mute": final_mute,
                "persistence_began_only_after_final_mute_passed": True,
            },
            "fixture_evidence": fixture,
            "immutable_plan_contract_sha256": canonical_sha256(contract),
            "global_execution_burn": dict(execution_burn) if execution_burn is not None else None,
            "capture_root_binding": capture_binding,
            "normalized_observation": observation,
        }
        live_artifact_root = Path(str(live_artifact.path))
        live_observation_path = live_artifact_root / OBSERVATION_FILENAME
        live_condition_path = live_artifact_root / "5g8-input-off-condition.json"
        write_json_atomic(live_observation_path, observation)
        write_json_atomic(live_condition_path, record)
        observation_sha256 = sha256_path(live_observation_path)
        condition_sha256 = sha256_path(live_condition_path)
        artifact_identity = _validate_accepted_capture_inventory(
            capture_fd, artifact_id=artifact_id
        )
        artifact_fd = _open_child_directory(capture_fd, artifact_id)
        try:
            _seal_directory_tree(artifact_fd)
        finally:
            os.close(artifact_fd)
        rebound_fd, rebound_binding = _validate_capture_root_binding(capture_binding)
        try:
            if rebound_binding != capture_binding:
                raise InputOffRunError("P2 capture root rebound after persistence")
            post_artifact_identity = _validate_accepted_capture_inventory(
                rebound_fd, artifact_id=artifact_id
            )
        finally:
            os.close(rebound_fd)
        if post_artifact_identity != artifact_identity:
            raise InputOffRunError("P2 artifact directory rebound during finalization")
        _publish_finalized_artifact_binding(
            execution_context,
            capture_root_binding=capture_binding,
            artifact_id=artifact_id,
            artifact_directory_identity=artifact_identity,
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "artifact_id": artifact_id,
            "artifact_evidence": artifact_evidence,
            "capture_root_binding": capture_binding,
            "artifact_directory_identity": artifact_identity,
            "observation_path": str(artifact_root / OBSERVATION_FILENAME),
            "observation_sha256": observation_sha256,
            "condition_record_path": str(artifact_root / "5g8-input-off-condition.json"),
            "condition_record_sha256": condition_sha256,
            "stream_id": stream_id,
            "final_mute": final_mute,
            "identity_preflight": identity,
            "native_runtime_preflight": runtime_native,
            "global_execution_burn": (dict(execution_burn) if execution_burn is not None else None),
        }
    except BaseException as error:
        retained.clear()
        if writer is not None:
            with suppress(BaseException):
                writer.fail(error)
            artifact_id = writer.artifact_id
            try:
                quarantine = _quarantine_artifact_by_binding(
                    capture_binding=capture_binding,
                    artifact_id=artifact_id,
                    error=error,
                    capture_fd=capture_fd,
                )
                _attach_quarantine(error, quarantine)
            except BaseException as quarantine_error:
                error.__dict__["p2_quarantine_error"] = _error_document(quarantine_error)
        raise
    finally:
        retained.clear()
        os.close(capture_fd)


def _execution_tombstone(
    path: Path,
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    global_execution_burn: Mapping[str, Any],
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_input_off_execution_started_tombstone",
        "run_id": contract["run_id"],
        "created_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "global_execution_burn": dict(global_execution_burn),
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _failure_tombstone(
    path: Path,
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    error: BaseException,
    global_reservation: Mapping[str, Any],
    global_execution_burn: Mapping[str, Any] | None,
    quarantine: Mapping[str, Any] | None,
    cleanup_errors: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_input_off_failed_run_tombstone",
        "run_id": contract["run_id"],
        "failed_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_sha256(contract),
        "error": _error_document(error),
        "cleanup_errors": [dict(item) for item in cleanup_errors],
        "global_run_reservation": dict(global_reservation),
        "global_execution_burn": (
            dict(global_execution_burn) if global_execution_burn is not None else None
        ),
        "quarantine": dict(quarantine) if quarantine is not None else None,
        "accepted_artifact": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _default_execute_boundary(
    contract: Mapping[str, Any],
    burn: Mapping[str, Any],
    execution_context: MutableMapping[str, Any],
) -> dict[str, Any]:
    _revalidate_frozen_execution_evidence(contract)
    return _execute_one_stream(
        contract,
        execution_context=execution_context,
        execution_burn=burn,
    )


def _revalidate_frozen_execution_evidence(contract: Mapping[str, Any]) -> None:
    """Re-attest every mutable dependency after the global run burn."""

    source = contract.get("source")
    fixture = contract.get("fixture_evidence")
    profile_contract = contract.get("profile")
    configuration = contract.get("configuration")
    p0_bindings = contract.get("p0_baseline_bindings")
    if not all(
        isinstance(value, Mapping) for value in (source, fixture, profile_contract, configuration)
    ) or not isinstance(p0_bindings, list):
        raise InputOffRunError("immutable P2 execution evidence is malformed")
    assert isinstance(source, Mapping)
    assert isinstance(fixture, Mapping)
    assert isinstance(profile_contract, Mapping)
    assert isinstance(configuration, Mapping)
    current_source = _repository_source_attestation()
    current_dependency = attest_pluto_plus_utils_source()
    current_native = validate_runtime_attestation(attest_runtime())
    if (
        current_source != source.get("smateway")
        or current_dependency != source.get("pluto_plus_utils")
        or current_native != source.get("native_libiio")
        or attestation_sha256(current_native) != source.get("native_libiio_sha256")
    ):
        raise InputOffRunError("post-burn source/dependency/native evidence differs from plan")
    profile_path = Path(str(profile_contract.get("path", ""))).expanduser().absolute()
    profile_file = _file_evidence(profile_path)
    profile = load_profile(profile_path)
    expected_profile = {
        **profile_file,
        "profile_id": profile.profile_id,
        "revision": profile.revision,
        "contract_sha256": profile.contract_sha256,
    }
    if expected_profile != profile_contract:
        raise InputOffRunError("post-burn Fast20 profile differs from immutable plan")
    fixture_sources = fixture.get("source_files")
    if not isinstance(fixture_sources, Mapping):
        raise InputOffRunError("immutable P2 fixture source bindings are malformed")
    fixture_manifest = fixture_sources.get("fixture_manifest")
    setup_attestation = fixture_sources.get("setup_attestation")
    if not isinstance(fixture_manifest, Mapping) or not isinstance(setup_attestation, Mapping):
        raise InputOffRunError("immutable P2 fixture source files are malformed")
    current_fixture = _fixture_evidence(
        Path(str(fixture_manifest.get("path", ""))),
        Path(str(setup_attestation.get("path", ""))),
        run_id=str(contract.get("run_id", "")),
        board_id=str(contract.get("board_id", "")),
        serial=str(configuration.get("serial", "")),
    )
    if current_fixture != fixture:
        raise InputOffRunError("post-burn fixture evidence differs from immutable plan")
    if not all(isinstance(value, Mapping) for value in p0_bindings):
        raise InputOffRunError("immutable P2 P0 baseline bindings are malformed")
    current_p0 = _p0_bindings(
        [Path(str(value.get("path", ""))) for value in p0_bindings],
        expected_profile_sha256=profile.contract_sha256,
        expected_source_commit=str(current_source["commit"]),
        expected_dependency_attestation=current_dependency,
        expected_native_attestation=current_native,
    )
    if current_p0 != p0_bindings:
        raise InputOffRunError("post-burn P0 baseline evidence differs from immutable plan")


def _prepared_execution_contract(
    args: argparse.Namespace,
    *,
    ledger_backend: global_ledger.LedgerBackend,
) -> tuple[dict[str, Any], Path, Path]:
    """Load a prepared plan and compare CLI identity without touching frozen evidence."""

    run_id = _validate_identifier(str(args.run_id), "run ID")
    board_id = _validate_identifier(str(args.board_id), "board ID")
    serial = _validate_identifier(str(args.serial), "Pluto serial")
    uri = str(args.uri)
    if USB_URI.fullmatch(uri) is None:
        raise InputOffRunError("P2 requires an explicit current usb: URI")
    state_root = _safe_local_state_root(args.state_root)
    run_root = state_root / "boards" / board_id / "5g8-input-off-control" / run_id
    plan_path = run_root / PLAN_FILENAME
    manifest_path = run_root / MANIFEST_FILENAME
    envelope = _read_json(plan_path, "immutable plan")
    raw_contract = envelope.get("plan_contract")
    if not isinstance(raw_contract, Mapping):
        raise InputOffRunError("immutable plan lacks its P2 contract")
    contract = dict(raw_contract)
    if envelope != _plan_envelope(contract):
        raise InputOffRunError("immutable plan envelope/hash differs")
    _require_local_storage_contract(contract, run_root=run_root)
    configuration = contract.get("configuration")
    profile = contract.get("profile")
    fixture = contract.get("fixture_evidence")
    p0_bindings = contract.get("p0_baseline_bindings")
    if (
        contract.get("run_id") != run_id
        or contract.get("board_id") != board_id
        or not isinstance(configuration, Mapping)
        or configuration.get("serial") != serial
        or configuration.get("uri") != uri
        or not isinstance(profile, Mapping)
        or profile.get("path") != str(args.profile.expanduser().absolute())
        or not isinstance(fixture, Mapping)
        or not isinstance(p0_bindings, list)
    ):
        raise InputOffRunError("execution CLI identity differs from immutable P2 plan")
    fixture_sources = fixture.get("source_files")
    if not isinstance(fixture_sources, Mapping):
        raise InputOffRunError("immutable P2 fixture source bindings are malformed")
    expected_fixture_paths = (
        str(args.fixture_manifest.expanduser().absolute()),
        str(args.setup_attestation.expanduser().absolute()),
    )
    observed_fixture_paths = tuple(
        str(_mapping.get("path", "")) if isinstance(_mapping, Mapping) else ""
        for _mapping in (
            fixture_sources.get("fixture_manifest"),
            fixture_sources.get("setup_attestation"),
        )
    )
    expected_p0_paths = [str(path.expanduser().absolute()) for path in args.p0_observation]
    observed_p0_paths = [
        str(value.get("path", "")) if isinstance(value, Mapping) else "" for value in p0_bindings
    ]
    if observed_fixture_paths != expected_fixture_paths or observed_p0_paths != expected_p0_paths:
        raise InputOffRunError("execution evidence paths/order differ from immutable P2 plan")
    _shared_validate_global_ledger_authority(
        contract, plan_path=plan_path, ledger_backend=ledger_backend
    )
    return contract, plan_path, manifest_path


def _execute_prepared(
    *,
    plan_path: Path,
    manifest_path: Path,
    expected_contract: Mapping[str, Any],
    confirmations: Mapping[str, Any],
    ledger_backend: global_ledger.LedgerBackend,
    execute_boundary: Callable[
        [Mapping[str, Any], Mapping[str, Any], MutableMapping[str, Any]], dict[str, Any]
    ] = _default_execute_boundary,
) -> dict[str, Any]:
    _require_local_storage_contract(expected_contract, run_root=plan_path.parent)
    envelope = _read_json(plan_path, "immutable plan")
    expected_envelope = _plan_envelope(expected_contract)
    if envelope != expected_envelope:
        raise InputOffRunError("execution arguments/evidence differ from immutable plan")
    manifest = _read_json(manifest_path, "run manifest")
    if manifest.get("status") != "prepared" or manifest.get("attempts") != []:
        raise InputOffRunError("run is not a never-attempted prepared plan")
    global_reservation = _validate_global_reservation(
        contract=expected_contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        value=manifest.get("global_run_reservation"),
        require_prepared_guard=True,
        ledger_backend=ledger_backend,
    )
    execution_path = manifest_path.parent / EXECUTION_TOMBSTONE_FILENAME
    failure_path = manifest_path.parent / FAILURE_TOMBSTONE_FILENAME
    if (
        execution_path.exists()
        or execution_path.is_symlink()
        or failure_path.exists()
        or failure_path.is_symlink()
    ):
        raise InputOffRunError("run ID is already burned by an execution/failure tombstone")
    progress: dict[str, Any] = {}
    execution_context: dict[str, Any] = {}
    attempt = {
        "started_at": _now(),
        "status": "running",
        "confirmations": _json_safe(confirmations),
        "global_execution_burn": None,
        "execution_tombstone": None,
        "result": None,
        "error": None,
    }
    manifest["attempts"] = [attempt]
    try:
        burn = _burn_global_run(
            contract=expected_contract,
            plan_path=plan_path,
            manifest_path=manifest_path,
            reservation=global_reservation,
            progress=progress,
            ledger_backend=ledger_backend,
        )
        manifest["global_execution_burn"] = burn
        attempt["global_execution_burn"] = burn
        execution = _execution_tombstone(
            execution_path,
            contract=expected_contract,
            plan_path=plan_path,
            global_execution_burn=burn,
        )
        attempt["execution_tombstone"] = {
            "path": str(execution_path),
            "sha256": sha256_path(execution_path),
            "document": execution,
        }
        manifest["status"] = "running"
        manifest["attempts"] = [attempt]
        manifest["updated_at"] = _now()
        write_json_atomic(manifest_path, manifest)
        result = execute_boundary(expected_contract, burn, execution_context)
        if result.get("global_execution_burn") not in (None, burn):
            raise InputOffRunError("P2 result global execution-burn binding differs")
        result["global_execution_burn"] = burn
        attempt["status"] = "complete"
        attempt["completed_at"] = _now()
        attempt["result"] = result
        manifest["status"] = "complete"
        manifest["result"] = result
        manifest["updated_at"] = _now()
        manifest["accepted_stream_count"] = 1
        manifest["error"] = None
        write_json_atomic(manifest_path, manifest)
    except BaseException as error:
        cleanup_errors: list[dict[str, str]] = []
        uncommitted_result = attempt.get("result")
        quarantine_value = getattr(error, "p2_quarantine", None)
        quarantine = dict(quarantine_value) if isinstance(quarantine_value, Mapping) else None
        attached_cleanup = getattr(error, "p2_quarantine_error", None)
        if isinstance(attached_cleanup, Mapping):
            cleanup_errors.append(dict(attached_cleanup))
        finalized_binding: dict[str, Any] | None = None
        try:
            finalized_binding = _finalized_artifact_binding_from_context(execution_context)
        except BaseException as cleanup_error:
            cleanup_errors.append(_error_document(cleanup_error))
        if finalized_binding is not None and quarantine is not None:
            if (
                quarantine.get("capture_root_binding") != finalized_binding["capture_root_binding"]
                or quarantine.get("artifact_id") != finalized_binding["artifact_id"]
            ):
                cleanup_errors.append(
                    _error_document(
                        InputOffRunError(
                            "P2 attached quarantine differs from finalized execution context"
                        )
                    )
                )
        elif finalized_binding is not None:
            try:
                quarantine = _quarantine_artifact_by_binding(
                    capture_binding=finalized_binding["capture_root_binding"],
                    artifact_id=str(finalized_binding["artifact_id"]),
                    artifact_directory_identity=finalized_binding["artifact_directory_identity"],
                    error=error,
                )
            except BaseException as cleanup_error:
                cleanup_errors.append(_error_document(cleanup_error))
        elif isinstance(uncommitted_result, Mapping) and quarantine is None:
            binding = uncommitted_result.get("capture_root_binding")
            artifact_id = uncommitted_result.get("artifact_id")
            artifact_identity = uncommitted_result.get("artifact_directory_identity")
            if isinstance(binding, Mapping) and isinstance(artifact_id, str):
                try:
                    quarantine = _quarantine_artifact_by_binding(
                        capture_binding=binding,
                        artifact_id=artifact_id,
                        artifact_directory_identity=(
                            artifact_identity if isinstance(artifact_identity, Mapping) else None
                        ),
                        error=error,
                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(_error_document(cleanup_error))
        attempt["status"] = "failed"
        attempt["completed_at"] = _now()
        attempt["error"] = _error_document(error)
        attempt["uncommitted_result"] = uncommitted_result
        attempt["result"] = None
        manifest["status"] = "failed"
        manifest["result"] = None
        manifest["error"] = attempt["error"]
        manifest["updated_at"] = _now()
        manifest["accepted_stream_count"] = 0
        manifest["quarantine"] = quarantine
        manifest["cleanup_errors"] = cleanup_errors
        local_failure_evidence: dict[str, Any] | None = None
        try:
            failure = _failure_tombstone(
                failure_path,
                contract=expected_contract,
                plan_path=plan_path,
                error=error,
                global_reservation=global_reservation,
                global_execution_burn=progress.get("global_execution_burn"),
                quarantine=quarantine,
                cleanup_errors=cleanup_errors,
            )
            local_failure_evidence = {
                "path": str(failure_path),
                "sha256": sha256_path(failure_path),
                "size_bytes": failure_path.stat().st_size,
                "document": failure,
            }
            manifest["failure_tombstone"] = local_failure_evidence
        except BaseException as cleanup_error:
            cleanup_errors.append(_error_document(cleanup_error))
        try:
            manifest["cleanup_errors"] = cleanup_errors
            write_json_atomic(manifest_path, manifest)
        except BaseException as cleanup_error:
            cleanup_errors.append(_error_document(cleanup_error))
        if progress.get("guard_transition_started") is True:
            try:
                global_failure = _seal_global_failure_receipt(
                    contract=expected_contract,
                    plan_path=plan_path,
                    manifest_path=manifest_path,
                    reservation=global_reservation,
                    progress=progress,
                    original_error=error,
                    cleanup_errors=cleanup_errors,
                    quarantine=quarantine,
                    local_failure_tombstone=local_failure_evidence,
                    ledger_backend=ledger_backend,
                )
                manifest["global_failure_receipt"] = global_failure
                manifest["cleanup_errors"] = cleanup_errors
                try:
                    write_json_atomic(manifest_path, manifest)
                except BaseException as cleanup_error:
                    cleanup_errors.append(_error_document(cleanup_error))
            except BaseException as cleanup_error:
                cleanup_errors.append(_error_document(cleanup_error))
        if cleanup_errors:
            composite = InputOffRunError(
                f"P2 execution failed ({type(error).__name__}: {error}); cleanup errors: "
                + json.dumps(cleanup_errors, sort_keys=True)
            )
            raise composite from error
        raise
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--setup-attestation", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--p0-observation",
        type=Path,
        action="append",
        default=[],
        help="repeat exactly five times with normalized, accepted 5.8-GHz P0 observations",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local/state/smateway",
        help="local Raspberry Pi state root; Pluto storage is never used",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-two-distinct-terminations", action="store_true")
    parser.add_argument("--confirm-downstream-unchanged", action="store_true")
    parser.add_argument("--confirm-rx1-protected-reference", action="store_true")
    parser.add_argument("--confirm-tx2-terminated-muted", action="store_true")
    parser.add_argument("--confirm-fast20-live", action="store_true")
    parser.add_argument("--confirm-no-movement", action="store_true")
    parser.add_argument("--confirm-topology-token")
    return parser


def _stop_on_signal(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received {signal.Signals(signum).name}")


def _install_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _stop_on_signal)


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    try:
        ledger_backend: global_ledger.LedgerBackend = global_ledger.SudoLedgerBackend()
        if args.plan_only:
            source = _repository_source_attestation()
            dependency = attest_pluto_plus_utils_source()
            native = attest_runtime()
            fixture = _fixture_evidence(
                args.fixture_manifest,
                args.setup_attestation,
                run_id=args.run_id,
                board_id=args.board_id,
                serial=args.serial,
            )
            profile = load_profile(args.profile.expanduser().absolute())
            p0 = _p0_bindings(
                args.p0_observation,
                expected_profile_sha256=profile.contract_sha256,
                expected_source_commit=str(source["commit"]),
                expected_dependency_attestation=dependency,
                expected_native_attestation=native,
            )
            contract = _build_plan_contract(
                run_id=args.run_id,
                board_id=args.board_id,
                serial=args.serial,
                uri=args.uri,
                profile_path=args.profile,
                fixture_evidence=fixture,
                p0_bindings=p0,
                source_attestation=source,
                dependency_attestation=dependency,
                native_attestation=native,
                state_root=args.state_root,
                ledger_backend=ledger_backend,
            )
            run_root = Path(str(contract["storage"]["run_root"]))
            plan_path = run_root / PLAN_FILENAME
            manifest_path = run_root / MANIFEST_FILENAME
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
                        "next": (
                            "physically confirm fixture, then rerun with --execute and "
                            "confirmations"
                        ),
                    }
                )
            )
            return 0
        contract, plan_path, manifest_path = _prepared_execution_contract(
            args, ledger_backend=ledger_backend
        )
        confirmations = _validate_execution_confirmations(args)
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
                    "artifact": manifest["result"]["artifact"]["artifact_id"],
                    "observation": manifest["result"]["observation_path"],
                }
            )
        )
        return 0
    except (InputOffContractError, InputOffRunError, OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": _error_document(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
