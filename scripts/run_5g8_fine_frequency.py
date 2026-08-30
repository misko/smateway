#!/usr/bin/env python3
"""Plan or execute the immutable conducted 5.60--5.95 GHz T7 sweep."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
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
from smateway import global_ledger
from smateway.fine_frequency import (
    BANDWIDTH_HZ,
    BYTES_PER_CAPTURE,
    DDS_SCALE,
    EXPERIMENTAL_POLICY,
    FRAME_COUNT,
    KERNEL_BUFFERS,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TONE_OFFSET_HZ,
    TOTAL_SAMPLES,
    TX_CHANNEL,
    TX_HARDWARE_GAIN_DB,
    FineFrequencyError,
    build_coarse_schedule,
    build_fine_schedule,
    build_plan_contract,
    canonical_json_sha256,
    coherent_measurement_document,
    normalized_observation_from_evidence,
    plan_envelope,
    validate_live_condition_evidence,
    validate_campaign_cross_binding,
    validate_plan_envelope,
)
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    sha256_path,
    write_json_atomic,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import attest_runtime, validate_runtime_attestation
from smateway.ota_analysis import estimate_coherent_pilot_offset

if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))
import scripts.run_5g8_leakage_ladder as leakage_runner

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
RESULTS_FILENAME = "fine-frequency-results.json"
GLOBAL_LEDGER_POLICY_ID = "t7-5g8-fine-frequency-v1"
RUN_KIND = "5g8_bidirectional_fine_frequency_sweep"
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
LOAD_INPUT_LIMIT_DBM = 0.0
REQUIRED_MARGIN_DB = 10.0
GIT_COMMIT_LENGTH = 40
SHA256_LENGTH = 64
PREPARED_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "manifest_kind",
        "run_id",
        "status",
        "created_at",
        "updated_at",
        "plan_path",
        "plan_sha256",
        "plan_contract_sha256",
        "run_state_ledger",
        "attempts",
        "condition_results",
        "accepted_condition_count",
        "campaign_accepted",
        "error",
    }
)
SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/global_ledger.py",
    "src/smateway/bench.py",
    "src/smateway/fine_frequency.py",
    "src/smateway/hexcal.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/ota_analysis.py",
    "src/smateway/profile.py",
    "src/smateway/rf_policy.py",
    "src/smateway/selector_flash_attestation.py",
    "scripts/run_5g8_leakage_ladder.py",
    "scripts/run_5g8_fine_frequency.py",
    "scripts/analyze_5g8_fine_frequency.py",
)

TOPOLOGIES: dict[str, dict[str, Any]] = {
    "direct_rx2_termination": {
        "token": "DIRECT_RX2_50OHM_AT_PLUTO",
        "selector_connected": False,
    },
    "rx2_cable_terminated": {
        "token": "RX2_CABLE_FAR_END_50OHM",
        "selector_connected": False,
    },
    "powered_selector_all_inputs_terminated": {
        "token": "POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM",
        "selector_connected": True,
    },
    "full_conducted_fixture": {
        "token": "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
        "selector_connected": True,
    },
}


class FineFrequencyRunError(RuntimeError):
    """The run failed before the complete campaign could be accepted."""


def _global_ledger_backend() -> global_ledger.LedgerBackend:
    """Return the source-fixed production adapter; tests inject a local backend."""

    return global_ledger.SudoLedgerBackend()


def _assert_privileged_ledger_entry(
    path: Path,
    label: str,
    *,
    authority: Mapping[str, Any],
) -> None:
    storage = authority.get("storage")
    if isinstance(storage, Mapping) and storage.get("private_test_storage") is True:
        return
    observed = path.stat(follow_symlinks=False)
    if (
        observed.st_uid != 0
        or observed.st_mode & 0o022
        or os.access(path, os.W_OK)
        or os.access(path.parent, os.W_OK)
    ):
        raise FineFrequencyRunError(
            f"{label} is not protected by the privileged shared-ledger boundary"
        )


class ConditionBoundary(Protocol):
    def __call__(
        self,
        contract: Mapping[str, Any],
        condition: Mapping[str, Any],
        capture_root: Path,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise FineFrequencyRunError(f"{label} path contains a symlink: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise FineFrequencyRunError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FineFrequencyRunError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FineFrequencyRunError(f"{label} must contain one JSON object")
    return value


def _assert_declared_paths_no_symlink(
    value: object,
    *,
    base_directory: Path,
    label: str,
) -> None:
    """Reject every original path spelling before any helper resolves symlinks."""

    def visit(item: object, trail: tuple[str, ...]) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                child_trail = (*trail, key)
                if isinstance(child, str) and child and (key == "path" or key.endswith("_path")):
                    candidate = Path(child).expanduser()
                    if not candidate.is_absolute():
                        candidate = base_directory / candidate
                    _assert_no_symlink_chain(candidate, f"{label} {'.'.join(child_trail)}")
                visit(child, child_trail)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, (*trail, str(index)))

    visit(value, ())


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


def _file_identity(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise FineFrequencyRunError(f"{label} is not a regular non-symlink file")
    return {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": exact.stat().st_size,
    }


def _verify_file_identity(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise FineFrequencyRunError(f"{label} identity is missing")
    path = Path(str(value.get("path", "")))
    observed = _file_identity(path, label)
    expected = {
        "path": value.get("path"),
        "sha256": value.get("sha256"),
        "size_bytes": value.get("size_bytes"),
    }
    if observed != expected:
        raise FineFrequencyRunError(f"{label} bytes differ from the immutable plan")


def _repository_source_identity(repository: Path = _REPOSITORY) -> dict[str, Any]:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(commit) != GIT_COMMIT_LENGTH or any(c not in "0123456789abcdef" for c in commit):
        raise FineFrequencyRunError("Smateway HEAD is not a full lowercase Git object ID")
    files = attest_source_files_at_commit(
        repository,
        expected_commit=commit,
        relative_paths=SOURCE_FILES,
    )
    dependency = attest_pluto_plus_utils_source()
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": commit,
        "scientific_files": files["files"],
        "scientific_files_sha256": canonical_json_sha256(files["files"]),
        "pluto_plus_utils": dependency,
        "pluto_plus_utils_sha256": canonical_json_sha256(dependency),
    }


def _fixture_identity(
    fixture_path: Path,
    setup_path: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
    topology_stage: str,
    selector_flash_path: Path | None,
    selector_flash_sha256: str | None,
    selector_flash_run_id: str | None,
    bench_manifest_path: Path | None,
    openocd_config_path: Path | None,
    profile_path: Path | None,
) -> dict[str, Any]:
    topology = TOPOLOGIES.get(topology_stage)
    if topology is None:
        raise FineFrequencyRunError("topology stage is outside the reviewed T7 ladder")
    direct_paths = (
        (fixture_path, "fixture manifest"),
        (setup_path, "setup attestation"),
        (selector_flash_path, "selector flash evidence"),
        (bench_manifest_path, "bench manifest"),
        (openocd_config_path, "OpenOCD config"),
        (profile_path, "selector profile"),
    )
    for path, label in direct_paths:
        if path is not None:
            _assert_no_symlink_chain(path, label)
    if profile_path is not None:
        _assert_no_symlink_chain(
            profile_path.with_name("control_profile.h"),
            "selector profile generated header",
        )
    fixture_document = _read_json(fixture_path, "fixture manifest v2")
    setup_document = _read_json(setup_path, "run setup attestation")
    _assert_declared_paths_no_symlink(
        fixture_document,
        base_directory=fixture_path.expanduser().absolute().parent,
        label="fixture manifest v2",
    )
    _assert_declared_paths_no_symlink(
        setup_document,
        base_directory=setup_path.expanduser().absolute().parent,
        label="run setup attestation",
    )
    selector_arguments = (
        selector_flash_path,
        selector_flash_sha256,
        selector_flash_run_id,
        bench_manifest_path,
        openocd_config_path,
        profile_path,
    )
    connected = bool(topology["selector_connected"])
    if connected and any(item is None for item in selector_arguments):
        raise FineFrequencyRunError(
            "selector-connected stages require the sealed flash path/hash/run and "
            "bench manifest/OpenOCD/profile tuple"
        )
    if not connected and any(item is not None for item in selector_arguments):
        raise FineFrequencyRunError(
            "selector evidence/control is forbidden when the selector is disconnected"
        )
    if selector_flash_path is not None:
        flash_document = _read_json(selector_flash_path, "sealed selector flash evidence")
        _assert_declared_paths_no_symlink(
            flash_document,
            base_directory=selector_flash_path.expanduser().absolute().parent,
            label="sealed selector flash evidence",
        )
    try:
        fixture = leakage_runner._fixture_evidence_from_manifests(
            fixture_path,
            setup_path,
            run_id=run_id,
            board_id=board_id,
            serial=serial,
            stage=topology_stage,
            selector_flash_evidence_path=selector_flash_path,
            selector_flash_evidence_sha256=selector_flash_sha256,
            selector_flash_run_id=selector_flash_run_id,
        )
        if connected:
            assert bench_manifest_path is not None
            assert openocd_config_path is not None
            assert profile_path is not None
            fixture_flash = fixture.get("selector_flash_evidence")
            if not isinstance(fixture_flash, Mapping):
                raise FineFrequencyRunError("fixture-v2 lacks sealed selector evidence")
            selector_control = leakage_runner._selector_control_contract(
                bench_manifest_path=bench_manifest_path,
                openocd_config_path=openocd_config_path,
                profile_path=profile_path,
                selector_flash_evidence=fixture_flash,
            )
        else:
            selector_control = None
    except FineFrequencyRunError:
        raise
    except Exception as error:
        raise FineFrequencyRunError(f"fixture-v2/selector binding failed: {error}") from error
    return {
        "schema": 1,
        "identity_kind": "5g8_t7_fixture_v2_binding",
        "topology_stage": topology_stage,
        "topology_token": topology["token"],
        "selector_connected": connected,
        "fixture_evidence_v2": fixture,
        "selector_control": selector_control,
    }


def _verify_fixture_identity(identity: object) -> None:
    if not isinstance(identity, Mapping):
        raise FineFrequencyRunError("fixture identity is missing")
    stage = identity.get("topology_stage")
    if stage not in TOPOLOGIES or identity.get("topology_token") != TOPOLOGIES[str(stage)]["token"]:
        raise FineFrequencyRunError("fixture topology differs from the reviewed ladder")
    fixture = identity.get("fixture_evidence_v2")
    if not isinstance(fixture, Mapping):
        raise FineFrequencyRunError("fixture-v2 evidence is missing")
    source_files = fixture.get("source_files")
    if not isinstance(source_files, Mapping):
        raise FineFrequencyRunError("fixture-v2 source files are missing")
    fixture_file = source_files.get("fixture_manifest")
    setup_file = source_files.get("setup_attestation")
    if not isinstance(fixture_file, Mapping) or not isinstance(setup_file, Mapping):
        raise FineFrequencyRunError("fixture-v2 source file evidence is malformed")
    selector_control = identity.get("selector_control")
    flash = fixture.get("selector_flash_evidence")
    connected = bool(TOPOLOGIES[str(stage)]["selector_connected"])
    selector_flash_path: Path | None = None
    selector_flash_sha256: str | None = None
    selector_flash_run_id: str | None = None
    bench_manifest_path: Path | None = None
    openocd_config_path: Path | None = None
    profile_path: Path | None = None
    if connected:
        if not isinstance(selector_control, Mapping) or not isinstance(flash, Mapping):
            raise FineFrequencyRunError("selector-connected fixture binding is incomplete")
        bench = selector_control.get("bench_manifest")
        openocd = selector_control.get("openocd_config")
        profile = selector_control.get("control_profile")
        if not all(isinstance(item, Mapping) for item in (bench, openocd, profile)):
            raise FineFrequencyRunError("selector-control artifact tuple is malformed")
        assert isinstance(bench, Mapping)
        assert isinstance(openocd, Mapping)
        assert isinstance(profile, Mapping)
        selector_flash_path = Path(str(flash.get("path", "")))
        selector_flash_sha256 = str(flash.get("sha256", ""))
        selector_flash_run_id = str(flash.get("run_id", ""))
        bench_manifest_path = Path(str(bench.get("path", "")))
        openocd_config_path = Path(str(openocd.get("path", "")))
        profile_path = Path(str(profile.get("path", "")))
    else:
        if selector_control is not None or flash is not None:
            raise FineFrequencyRunError(
                "disconnected selector stage unexpectedly binds selector evidence"
            )
    _assert_declared_paths_no_symlink(
        identity,
        base_directory=Path("/"),
        label="frozen fixture identity",
    )
    rebuilt = _fixture_identity(
        Path(str(fixture_file.get("path", ""))),
        Path(str(setup_file.get("path", ""))),
        run_id=str(fixture.get("run_id", "")),
        board_id=str(fixture.get("board_id", "")),
        serial=(
            str(fixture["shared_fixture"]["pluto"].get("serial", ""))
            if isinstance(fixture.get("shared_fixture"), Mapping)
            and isinstance(fixture["shared_fixture"].get("pluto"), Mapping)
            else ""
        ),
        topology_stage=str(stage),
        selector_flash_path=selector_flash_path,
        selector_flash_sha256=selector_flash_sha256,
        selector_flash_run_id=selector_flash_run_id,
        bench_manifest_path=bench_manifest_path,
        openocd_config_path=openocd_config_path,
        profile_path=profile_path,
    )
    if rebuilt != dict(identity):
        raise FineFrequencyRunError("current fixture-v2/selector bytes differ from the plan")
    if connected:
        try:
            assert isinstance(selector_control, Mapping)
            leakage_runner._verify_selector_artifacts(selector_control)
        except Exception as error:
            raise FineFrequencyRunError(f"sealed selector artifacts failed: {error}") from error


def _nearest_existing(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists():
        if candidate == candidate.parent:
            raise FineFrequencyRunError("cannot find an existing storage parent")
        candidate = candidate.parent
    return candidate


def _filesystem_device(path: Path) -> int:
    """Return the backing filesystem device through a narrow test seam."""

    return int(os.stat(path).st_dev)


def _assert_local_rpi_storage(path: Path) -> None:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "local T7 storage")
    forbidden_roots = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or exact.is_relative_to(root) for root in forbidden_roots):
        raise FineFrequencyRunError("T7 storage must not use removable or Pluto media")
    try:
        home_device = _filesystem_device(Path("/home/pi"))
        storage_device = _filesystem_device(_nearest_existing(exact))
    except OSError as error:
        raise FineFrequencyRunError(f"cannot attest T7 local storage: {error}") from error
    if storage_device != home_device:
        raise FineFrequencyRunError("T7 storage is not on the Raspberry Pi local filesystem")


def _free_bytes(path: Path) -> int:
    _assert_local_rpi_storage(path)
    return int(shutil.disk_usage(_nearest_existing(path)).free)


def _run_root(state_root: Path, board_id: str, run_id: str) -> Path:
    return state_root.expanduser().absolute() / "boards" / board_id / "5g8-fine-frequency" / run_id


def _augment_storage(
    contract: dict[str, Any],
    run_root: Path,
    *,
    state_root: Path,
) -> dict[str, Any]:
    exact_run_root = run_root.expanduser().absolute()
    exact_state_root = state_root.expanduser().absolute()
    plan_path = exact_run_root / PLAN_FILENAME
    augmented = dict(contract)
    augmented["execution_storage"] = {
        "state_root": str(exact_state_root),
        "run_root": str(exact_run_root),
        "capture_root": str(exact_run_root / "captures"),
        "medium": "raspberry_pi_local_filesystem",
        "pluto_onboard_storage_used": False,
        "global_run_ledger_authority": _new_global_ledger_authority(
            contract,
            plan_path=plan_path,
            state_root=exact_state_root,
        ),
    }
    return augmented


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _inode_identity(
    path: Path,
    *,
    directory: bool,
    label: str,
    expected_nlink: int = 1,
) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    _assert_local_rpi_storage(exact)
    try:
        observed = exact.lstat()
    except OSError as error:
        raise FineFrequencyRunError(f"cannot stat {label}: {error}") from error
    expected_kind = stat.S_ISDIR(observed.st_mode) if directory else stat.S_ISREG(observed.st_mode)
    if exact.is_symlink() or not expected_kind:
        kind = "directory" if directory else "regular file"
        raise FineFrequencyRunError(f"{label} must be a non-symlink {kind}")
    if not directory and observed.st_nlink != expected_nlink:
        raise FineFrequencyRunError(
            f"{label} must have exactly {expected_nlink} durable inode links"
        )
    return {
        "path": str(exact),
        "st_dev": int(observed.st_dev),
        "st_ino": int(observed.st_ino),
    }


def _canonical_run_ledger_identity(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "plan_path": str(plan_path.expanduser().absolute()),
    }


def _global_run_namespace(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "policy_id": GLOBAL_LEDGER_POLICY_ID,
        "namespace_kind": "5g8_fine_frequency_board_run_id_v1",
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
    }


def _new_global_ledger_authority(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    state_root: Path,
) -> dict[str, Any]:
    try:
        return global_ledger.authority_from_storage(
            policy_id=GLOBAL_LEDGER_POLICY_ID,
            namespace=_global_run_namespace(contract),
            canonical_identity=_canonical_run_ledger_identity(
                contract,
                plan_path=plan_path,
            ),
            state_root=state_root,
            backend=_global_ledger_backend(),
        )
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        raise FineFrequencyRunError(f"shared global T7 ledger authority failed: {error}") from error


def _validate_global_ledger_authority(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
) -> dict[str, Any]:
    storage = contract.get("execution_storage")
    if not isinstance(storage, Mapping):
        raise FineFrequencyRunError("immutable plan has no T7 execution storage contract")
    state_root = Path(str(storage.get("state_root", ""))).expanduser().absolute()
    try:
        return global_ledger.validate_authority(
            storage.get("global_run_ledger_authority"),
            policy_id=GLOBAL_LEDGER_POLICY_ID,
            namespace=_global_run_namespace(contract),
            canonical_identity=_canonical_run_ledger_identity(
                contract,
                plan_path=plan_path,
            ),
            state_root=state_root,
            backend=_global_ledger_backend(),
        )
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        raise FineFrequencyRunError(
            f"shared global T7 ledger authority differs: {error}"
        ) from error


def _ledger_paths(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    authority = _validate_global_ledger_authority(contract, plan_path=plan_path)
    ledger_directory = Path(str(authority["ledger_directory_path"]))
    return (
        ledger_directory,
        ledger_directory / global_ledger.RESERVATION_FILENAME,
        ledger_directory / global_ledger.BURN_GUARD_FILENAME,
        ledger_directory / global_ledger.BURN_MARKER_FILENAME,
        ledger_directory / global_ledger.FAILURE_RECEIPT_FILENAME,
    )


def _global_ledger_mutation(
    *,
    authority: Mapping[str, Any],
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one exact shared-ledger transition and return validated evidence."""

    try:
        request = global_ledger.mutation_request(
            authority=authority,
            operation=operation,
            payload=payload,
        )
        response = _global_ledger_backend().mutate(request)
        validated = global_ledger.validate_response(request, response)
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        raise FineFrequencyRunError(
            f"privileged shared T7 ledger mutation failed closed: {error}"
        ) from error
    return dict(validated["evidence"])


def _global_ledger_inspection(authority: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return dict(_global_ledger_backend().inspect(authority))
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        raise FineFrequencyRunError(
            f"authoritative shared T7 ledger inspection failed: {error}"
        ) from error


def _anchor_names_for_identity(anchor_directory: Path, *, ledger_key: str) -> set[str]:
    prefix = f".anchor.{ledger_key}."
    try:
        return {entry.name for entry in anchor_directory.iterdir() if entry.name.startswith(prefix)}
    except OSError as error:
        raise FineFrequencyRunError(f"cannot scan T7 inode-anchor history: {error}") from error


def _assert_no_prior_anchor_history(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
) -> None:
    authority = _validate_global_ledger_authority(contract, plan_path=plan_path)
    anchor_directory = Path(str(authority["storage"]["anchor_directory"]["path"]))
    if _anchor_names_for_identity(
        anchor_directory,
        ledger_key=str(authority["ledger_key"]),
    ):
        raise FineFrequencyRunError("T7 run ID already has durable inode-anchor history")


def _new_run_ledger_binding(
    *,
    contract: Mapping[str, Any],
    run_root: Path,
) -> dict[str, Any]:
    run_id = str(contract["run_id"])
    reservation_id = uuid.uuid4().hex
    plan_path = run_root.expanduser().absolute() / PLAN_FILENAME
    authority = _validate_global_ledger_authority(contract, plan_path=plan_path)
    ledger_directory, reservation_path, guard_path, marker_path, failure_receipt_path = (
        _ledger_paths(
            contract,
            plan_path=plan_path,
        )
    )
    shared_storage = authority["storage"]
    anchor_directory = Path(str(shared_storage["anchor_directory"]["path"]))
    _assert_no_prior_anchor_history(contract, plan_path=plan_path)
    _assert_no_symlink_chain(ledger_directory, "T7 external run ledger")
    _assert_local_rpi_storage(ledger_directory)
    ledger_key = str(authority["ledger_key"])
    reservation_anchor = anchor_directory / f".anchor.{ledger_key}.{reservation_id}.reservation"
    guard_anchor = anchor_directory / f".anchor.{ledger_key}.{reservation_id}.burn-guard"
    failure_receipt_anchor = (
        anchor_directory / f".anchor.{ledger_key}.{reservation_id}.failure-receipt"
    )
    reservation_evidence = _global_ledger_mutation(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": reservation_id},
    )
    binding = {
        "schema": 3,
        "ledger_kind": "5g8_fine_frequency_fixed_global_run_id_ledger_v3",
        "reservation_id": reservation_id,
        "board_id": contract["board_id"],
        "run_id": run_id,
        "global_ledger_authority": authority,
        "canonical_run_identity": authority["canonical_run_identity"],
        "canonical_run_identity_sha256": authority["canonical_run_identity_sha256"],
        "ledger_key": ledger_key,
        "run_directory": _inode_identity(
            run_root,
            directory=True,
            label="T7 run directory",
        ),
        "ledger_directory": _inode_identity(
            ledger_directory,
            directory=True,
            label="T7 external run ledger",
        ),
        "anchor_directory": _inode_identity(
            anchor_directory,
            directory=True,
            label="T7 ledger inode-anchor directory",
        ),
        "reservation_slot": _inode_identity(
            reservation_path,
            directory=False,
            label="T7 reservation slot",
            expected_nlink=2,
        ),
        "reservation_anchor": _inode_identity(
            reservation_anchor,
            directory=False,
            label="T7 reservation inode anchor",
            expected_nlink=2,
        ),
        "burn_guard": _inode_identity(
            guard_path,
            directory=False,
            label="T7 burn guard",
            expected_nlink=2,
        ),
        "burn_guard_anchor": _inode_identity(
            guard_anchor,
            directory=False,
            label="T7 burn-guard inode anchor",
            expected_nlink=2,
        ),
        "burn_marker_path": str(marker_path),
        "failure_receipt_slot": _inode_identity(
            failure_receipt_path,
            directory=False,
            label="T7 failure-receipt slot",
            expected_nlink=2,
        ),
        "failure_receipt_anchor": _inode_identity(
            failure_receipt_anchor,
            directory=False,
            label="T7 failure-receipt inode anchor",
            expected_nlink=2,
        ),
        "external_to_run_directory": True,
        "external_to_caller_state_root": True,
        "consumed_to_prepared_transition_forbidden": True,
    }
    expected_reservation_evidence = {
        "reservation_id": reservation_id,
        "ledger_directory": binding["ledger_directory"],
        "slots": {
            "reservation": binding["reservation_slot"],
            "burn-guard": binding["burn_guard"],
            "failure-receipt": binding["failure_receipt_slot"],
        },
        "anchors": {
            "reservation": binding["reservation_anchor"],
            "burn-guard": binding["burn_guard_anchor"],
            "failure-receipt": binding["failure_receipt_anchor"],
        },
        "slot_files": {
            name: {
                **_file_evidence(path, f"T7 prepared {name} slot", expected_nlink=2),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "nlink": path.stat().st_nlink,
            }
            for name, path in {
                "reservation": reservation_path,
                "burn-guard": guard_path,
                "failure-receipt": failure_receipt_path,
            }.items()
        },
        "anchor_files": {
            name: {
                **_file_evidence(path, f"T7 prepared {name} anchor", expected_nlink=2),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "nlink": path.stat().st_nlink,
            }
            for name, path in {
                "reservation": reservation_anchor,
                "burn-guard": guard_anchor,
                "failure-receipt": failure_receipt_anchor,
            }.items()
        },
    }
    if reservation_evidence != expected_reservation_evidence:
        raise FineFrequencyRunError(
            "privileged shared T7 reservation evidence differs from observed inodes"
        )
    return binding


def _validate_run_ledger_binding(
    contract: Mapping[str, Any],
    *,
    run_root: Path,
    value: object,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FineFrequencyRunError("prepared manifest has no external T7 run ledger")
    binding = dict(value)
    expected_keys = {
        "schema",
        "ledger_kind",
        "reservation_id",
        "board_id",
        "run_id",
        "global_ledger_authority",
        "canonical_run_identity",
        "canonical_run_identity_sha256",
        "ledger_key",
        "run_directory",
        "ledger_directory",
        "anchor_directory",
        "reservation_slot",
        "reservation_anchor",
        "burn_guard",
        "burn_guard_anchor",
        "burn_marker_path",
        "failure_receipt_slot",
        "failure_receipt_anchor",
        "external_to_run_directory",
        "external_to_caller_state_root",
        "consumed_to_prepared_transition_forbidden",
    }
    run_id = str(contract["run_id"])
    plan_path = run_root.expanduser().absolute() / PLAN_FILENAME
    authority = _validate_global_ledger_authority(contract, plan_path=plan_path)
    (
        ledger_directory,
        reservation_path,
        guard_path,
        marker_path,
        failure_receipt_path,
    ) = _ledger_paths(contract, plan_path=plan_path)
    reservation_id = binding.get("reservation_id")
    ledger_key = str(authority["ledger_key"])
    anchor_directory = Path(str(authority["storage"]["anchor_directory"]["path"]))
    reservation_anchor = anchor_directory / f".anchor.{ledger_key}.{reservation_id}.reservation"
    guard_anchor = anchor_directory / f".anchor.{ledger_key}.{reservation_id}.burn-guard"
    failure_receipt_anchor = (
        anchor_directory / f".anchor.{ledger_key}.{reservation_id}.failure-receipt"
    )
    expected_anchor_names = {
        reservation_anchor.name,
        guard_anchor.name,
        failure_receipt_anchor.name,
    }
    for path, label in (
        (ledger_directory, "global T7 per-run ledger directory"),
        (reservation_path, "global T7 reservation"),
        (reservation_anchor, "global T7 reservation anchor"),
        (guard_path, "global T7 burn guard"),
        (guard_anchor, "global T7 burn-guard anchor"),
        (failure_receipt_path, "global T7 failure-receipt slot"),
        (failure_receipt_anchor, "global T7 failure-receipt anchor"),
    ):
        _assert_privileged_ledger_entry(path, label, authority=authority)
    if (
        set(binding) != expected_keys
        or binding.get("schema") != 3
        or binding.get("ledger_kind") != "5g8_fine_frequency_fixed_global_run_id_ledger_v3"
        or not isinstance(reservation_id, str)
        or len(reservation_id) != 32
        or any(character not in "0123456789abcdef" for character in reservation_id)
        or binding.get("board_id") != contract.get("board_id")
        or binding.get("run_id") != run_id
        or binding.get("global_ledger_authority") != authority
        or binding.get("canonical_run_identity") != authority["canonical_run_identity"]
        or binding.get("canonical_run_identity_sha256")
        != authority["canonical_run_identity_sha256"]
        or binding.get("ledger_key") != ledger_key
        or binding.get("run_directory")
        != _inode_identity(run_root, directory=True, label="T7 run directory")
        or binding.get("ledger_directory")
        != _inode_identity(
            ledger_directory,
            directory=True,
            label="T7 external run ledger",
        )
        or binding.get("anchor_directory")
        != _inode_identity(
            anchor_directory,
            directory=True,
            label="T7 ledger inode-anchor directory",
        )
        or _anchor_names_for_identity(anchor_directory, ledger_key=ledger_key)
        != expected_anchor_names
        or binding.get("reservation_slot")
        != _inode_identity(
            reservation_path,
            directory=False,
            label="T7 reservation slot",
            expected_nlink=2,
        )
        or binding.get("reservation_anchor")
        != _inode_identity(
            reservation_anchor,
            directory=False,
            label="T7 reservation inode anchor",
            expected_nlink=2,
        )
        or binding.get("burn_guard")
        != _inode_identity(
            guard_path,
            directory=False,
            label="T7 burn guard",
            expected_nlink=2,
        )
        or binding.get("burn_guard_anchor")
        != _inode_identity(
            guard_anchor,
            directory=False,
            label="T7 burn-guard inode anchor",
            expected_nlink=2,
        )
        or binding["reservation_slot"]["st_dev"] != binding["reservation_anchor"]["st_dev"]
        or binding["reservation_slot"]["st_ino"] != binding["reservation_anchor"]["st_ino"]
        or binding["burn_guard"]["st_dev"] != binding["burn_guard_anchor"]["st_dev"]
        or binding["burn_guard"]["st_ino"] != binding["burn_guard_anchor"]["st_ino"]
        or binding.get("burn_marker_path") != str(marker_path)
        or binding.get("failure_receipt_slot")
        != _inode_identity(
            failure_receipt_path,
            directory=False,
            label="T7 failure-receipt slot",
            expected_nlink=2,
        )
        or binding.get("failure_receipt_anchor")
        != _inode_identity(
            failure_receipt_anchor,
            directory=False,
            label="T7 failure-receipt inode anchor",
            expected_nlink=2,
        )
        or binding["failure_receipt_slot"]["st_dev"] != binding["failure_receipt_anchor"]["st_dev"]
        or binding["failure_receipt_slot"]["st_ino"] != binding["failure_receipt_anchor"]["st_ino"]
        or binding.get("external_to_run_directory") is not True
        or binding.get("external_to_caller_state_root") is not True
        or binding.get("consumed_to_prepared_transition_forbidden") is not True
    ):
        raise FineFrequencyRunError("external T7 run ledger identity differs from preparation")
    return binding


def _prepared_manifest_document(
    plan_path: Path,
    envelope: Mapping[str, Any],
    *,
    ledger_binding: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    if not created_at:
        raise FineFrequencyRunError("prepared manifest timestamp is missing")
    return {
        "schema": 1,
        "manifest_kind": RUN_KIND,
        "run_id": envelope["plan_contract"]["run_id"],
        "status": "prepared",
        "created_at": created_at,
        "updated_at": created_at,
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": envelope["plan_contract_sha256"],
        "run_state_ledger": dict(ledger_binding),
        "attempts": [],
        "condition_results": [],
        "accepted_condition_count": 0,
        "campaign_accepted": False,
        "error": None,
    }


def _manifest_wire_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _new_manifest(
    plan_path: Path,
    envelope: Mapping[str, Any],
    *,
    ledger_binding: Mapping[str, Any],
) -> dict[str, Any]:
    return _prepared_manifest_document(
        plan_path,
        envelope,
        ledger_binding=ledger_binding,
        created_at=_now(),
    )


def _validate_exact_prepared_manifest(
    manifest: object,
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    run_root: Path,
) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise FineFrequencyRunError("prepared T7 manifest is not an object")
    document = dict(manifest)
    created_at = document.get("created_at")
    updated_at = document.get("updated_at")
    if (
        set(document) != PREPARED_MANIFEST_KEYS
        or document.get("schema") != 1
        or document.get("manifest_kind") != RUN_KIND
        or document.get("run_id") != contract.get("run_id")
        or document.get("status") != "prepared"
        or not isinstance(created_at, str)
        or not created_at
        or updated_at != created_at
        or document.get("plan_path") != str(plan_path.expanduser().absolute())
        or document.get("plan_sha256") != sha256_path(plan_path)
        or document.get("plan_contract_sha256") != canonical_json_sha256(contract)
        or document.get("attempts") != []
        or document.get("condition_results") != []
        or document.get("accepted_condition_count") != 0
        or document.get("campaign_accepted") is not False
        or document.get("error") is not None
    ):
        raise FineFrequencyRunError("run is not the exact never-attempted prepared campaign")
    _validate_run_ledger_binding(
        contract,
        run_root=run_root,
        value=document.get("run_state_ledger"),
    )
    return document


def _reservation_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    ledger_binding: Mapping[str, Any],
    prepared_manifest_sha256: str,
    prepared_manifest_size_bytes: int,
    reserved_at: object,
) -> dict[str, Any]:
    if not isinstance(reserved_at, str) or not reserved_at:
        raise FineFrequencyRunError("T7 ledger reservation timestamp is invalid")
    storage = contract["execution_storage"]
    return {
        "schema": 3,
        "marker_kind": "5g8_fine_frequency_global_run_id_reservation_v3",
        "reservation_id": ledger_binding["reservation_id"],
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "reserved_at": reserved_at,
        "run_root": str(Path(str(storage["run_root"])).expanduser().absolute()),
        "capture_root": str(Path(str(storage["capture_root"])).expanduser().absolute()),
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "global_ledger_authority": ledger_binding["global_ledger_authority"],
        "global_ledger_authority_sha256": canonical_json_sha256(
            ledger_binding["global_ledger_authority"]
        ),
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            ledger_binding["global_ledger_authority"]
        ),
        "canonical_run_identity_sha256": ledger_binding["canonical_run_identity_sha256"],
        "ledger_key": ledger_binding["ledger_key"],
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "prepared_manifest_sha256": prepared_manifest_sha256,
        "prepared_manifest_size_bytes": prepared_manifest_size_bytes,
        "run_state_ledger": dict(ledger_binding),
        "run_id_reserved_outside_run_directory": True,
        "run_id_reserved_outside_caller_state_root": True,
        "replacement_recreation_or_replay_forbidden": True,
    }


def _write_reserved_json_slot(
    path: Path,
    document: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, Any],
) -> None:
    authority = document.get("global_ledger_authority")
    if not isinstance(authority, Mapping):
        raise FineFrequencyRunError("T7 reservation lacks its shared ledger authority")
    evidence = _global_ledger_mutation(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "document": dict(document),
            "expected_identity": dict(expected_identity),
        },
    )
    observed = {
        **_file_evidence(path, "sealed T7 reservation", expected_nlink=2),
        "mode": stat.S_IMODE(path.stat().st_mode),
        "nlink": path.stat().st_nlink,
    }
    expected_evidence = {
        "slot": "reservation",
        "file": observed,
        "document_sha256": canonical_json_sha256(document),
    }
    if evidence != expected_evidence:
        raise FineFrequencyRunError("privileged T7 reservation response differs from bytes")


def _file_evidence(
    path: Path,
    label: str,
    *,
    expected_nlink: int = 1,
) -> dict[str, Any]:
    identity = _inode_identity(
        path,
        directory=False,
        label=label,
        expected_nlink=expected_nlink,
    )
    return {
        **identity,
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
    }


def _validate_reservation(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    ledger_binding: Mapping[str, Any],
    prepared_manifest_sha256: str | None,
    prepared_manifest_size_bytes: int | None,
) -> dict[str, Any]:
    reservation_path = Path(str(ledger_binding["reservation_slot"]["path"]))
    identity = _inode_identity(
        reservation_path,
        directory=False,
        label="T7 reservation",
        expected_nlink=2,
    )
    observed_stat = reservation_path.stat()
    if identity != ledger_binding["reservation_slot"] or observed_stat.st_mode & 0o222:
        raise FineFrequencyRunError("T7 reservation is not the immutable prepared slot")
    document = _read_json(reservation_path, "T7 reservation")
    bound_manifest_sha256 = document.get("prepared_manifest_sha256")
    bound_manifest_size = document.get("prepared_manifest_size_bytes")
    if not _is_sha256(bound_manifest_sha256) or not isinstance(bound_manifest_size, int):
        raise FineFrequencyRunError("T7 reservation has invalid prepared-manifest evidence")
    if (
        prepared_manifest_sha256 is not None and bound_manifest_sha256 != prepared_manifest_sha256
    ) or (
        prepared_manifest_size_bytes is not None
        and bound_manifest_size != prepared_manifest_size_bytes
    ):
        raise FineFrequencyRunError("prepared manifest bytes differ from the T7 reservation")
    expected = _reservation_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=ledger_binding,
        prepared_manifest_sha256=str(bound_manifest_sha256),
        prepared_manifest_size_bytes=int(bound_manifest_size),
        reserved_at=document.get("reserved_at"),
    )
    if document != expected:
        raise FineFrequencyRunError("T7 reservation content differs from the immutable plan")
    return {
        "path": str(reservation_path),
        "sha256": sha256_path(reservation_path),
        "size_bytes": reservation_path.stat().st_size,
        "document": document,
    }


def _ledger_entry_names(ledger_binding: Mapping[str, Any]) -> set[str]:
    ledger_directory = Path(str(ledger_binding["ledger_directory"]["path"]))
    try:
        return {entry.name for entry in ledger_directory.iterdir()}
    except OSError as error:
        raise FineFrequencyRunError(f"cannot scan external T7 run ledger: {error}") from error


def _validate_prepared_run_ledger(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    run_root = plan_path.expanduser().absolute().parent
    prepared = _validate_exact_prepared_manifest(
        manifest,
        contract=contract,
        plan_path=plan_path,
        run_root=run_root,
    )
    binding = prepared["run_state_ledger"]
    assert isinstance(binding, Mapping)
    if _ledger_entry_names(binding) != {
        global_ledger.RESERVATION_FILENAME,
        global_ledger.BURN_GUARD_FILENAME,
        global_ledger.FAILURE_RECEIPT_FILENAME,
    }:
        raise FineFrequencyRunError("T7 run ID is consumed or ledger history is malformed")
    reservation = _validate_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=binding,
        prepared_manifest_sha256=sha256_path(manifest_path),
        prepared_manifest_size_bytes=manifest_path.stat().st_size,
    )
    guard_path = Path(str(binding["burn_guard"]["path"]))
    guard_identity = _inode_identity(
        guard_path,
        directory=False,
        label="T7 burn guard",
        expected_nlink=2,
    )
    guard_stat = guard_path.stat()
    if (
        guard_identity != binding["burn_guard"]
        or guard_stat.st_size != 0
        or stat.S_IMODE(guard_stat.st_mode) != global_ledger.PREPARED_SLOT_MODE
    ):
        raise FineFrequencyRunError("T7 run ID burn guard is already consumed or malformed")
    failure_receipt_path = Path(str(binding["failure_receipt_slot"]["path"]))
    failure_receipt_identity = _inode_identity(
        failure_receipt_path,
        directory=False,
        label="T7 failure-receipt slot",
        expected_nlink=2,
    )
    failure_receipt_stat = failure_receipt_path.stat()
    if (
        failure_receipt_identity != binding["failure_receipt_slot"]
        or failure_receipt_stat.st_size != 0
        or stat.S_IMODE(failure_receipt_stat.st_mode) != global_ledger.PREPARED_SLOT_MODE
    ):
        raise FineFrequencyRunError("T7 run ID already has durable failure history")
    return {"binding": dict(binding), "reservation": reservation}


def _burn_marker_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    ledger_binding: Mapping[str, Any],
    reservation: Mapping[str, Any],
    burn_guard: Mapping[str, Any],
    execution_nonce: object,
    consumed_at: object,
) -> dict[str, Any]:
    if not isinstance(consumed_at, str) or not consumed_at:
        raise FineFrequencyRunError("T7 burn timestamp is invalid")
    if (
        not isinstance(execution_nonce, str)
        or len(execution_nonce) != 32
        or any(character not in "0123456789abcdef" for character in execution_nonce)
    ):
        raise FineFrequencyRunError("T7 execution nonce is invalid")
    reservation_document = reservation.get("document")
    if not isinstance(reservation_document, Mapping):
        raise FineFrequencyRunError("T7 reservation evidence is malformed")
    return {
        "schema": 3,
        "marker_kind": "5g8_fine_frequency_global_execution_consumed_v3",
        "execution_nonce": execution_nonce,
        "reservation_id": ledger_binding["reservation_id"],
        "board_id": contract["board_id"],
        "run_id": contract["run_id"],
        "consumed_at": consumed_at,
        "run_root": str(plan_path.expanduser().absolute().parent),
        "plan_path": str(plan_path.expanduser().absolute()),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "global_ledger_authority": ledger_binding["global_ledger_authority"],
        "global_ledger_authority_sha256": canonical_json_sha256(
            ledger_binding["global_ledger_authority"]
        ),
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            ledger_binding["global_ledger_authority"]
        ),
        "canonical_run_identity_sha256": ledger_binding["canonical_run_identity_sha256"],
        "ledger_key": ledger_binding["ledger_key"],
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "prepared_manifest_sha256": reservation_document["prepared_manifest_sha256"],
        "prepared_manifest_size_bytes": reservation_document["prepared_manifest_size_bytes"],
        "run_state_ledger_sha256": canonical_json_sha256(ledger_binding),
        "reservation_sha256": reservation["sha256"],
        "burn_guard_sha256": burn_guard["sha256"],
        "burn_guard_size_bytes": burn_guard["size_bytes"],
        "run_id_burned_before_external_or_hardware_access": True,
        "consumed_to_prepared_transition_forbidden": True,
    }


def _validate_consumed_run_ledger(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    ledger_binding: object,
    burn_evidence: object,
) -> dict[str, Any]:
    binding = _validate_run_ledger_binding(
        contract,
        run_root=plan_path.expanduser().absolute().parent,
        value=ledger_binding,
    )
    if _ledger_entry_names(binding) != {
        global_ledger.RESERVATION_FILENAME,
        global_ledger.BURN_GUARD_FILENAME,
        global_ledger.BURN_MARKER_FILENAME,
        global_ledger.FAILURE_RECEIPT_FILENAME,
    }:
        raise FineFrequencyRunError("consumed T7 external ledger entries are incomplete")
    reservation = _validate_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=binding,
        prepared_manifest_sha256=None,
        prepared_manifest_size_bytes=None,
    )
    guard_path = Path(str(binding["burn_guard"]["path"]))
    guard = _file_evidence(
        guard_path,
        "consumed T7 burn guard",
        expected_nlink=2,
    )
    guard_stat = guard_path.stat()
    if (
        guard["size_bytes"] != 1
        or stat.S_IMODE(guard_stat.st_mode) != global_ledger.SEALED_FILE_MODE
    ):
        raise FineFrequencyRunError("consumed T7 burn guard is not immutable and monotonic")
    failure_receipt_path = Path(str(binding["failure_receipt_slot"]["path"]))
    failure_receipt_identity = _inode_identity(
        failure_receipt_path,
        directory=False,
        label="T7 failure-receipt slot",
        expected_nlink=2,
    )
    failure_receipt_stat = failure_receipt_path.stat()
    if (
        failure_receipt_identity != binding["failure_receipt_slot"]
        or failure_receipt_stat.st_size != 0
        or stat.S_IMODE(failure_receipt_stat.st_mode) != global_ledger.PREPARED_SLOT_MODE
    ):
        raise FineFrequencyRunError("successful T7 burn has failure-receipt history")
    marker_path = Path(str(binding["burn_marker_path"]))
    marker_identity = _inode_identity(
        marker_path,
        directory=False,
        label="T7 external execution burn marker",
    )
    marker_stat = marker_path.stat()
    if marker_stat.st_mode & 0o222:
        raise FineFrequencyRunError("T7 external execution burn marker is mutable")
    _assert_privileged_ledger_entry(
        marker_path,
        "global T7 external execution burn marker",
        authority=binding["global_ledger_authority"],
    )
    marker = _read_json(marker_path, "T7 external execution burn marker")
    expected_marker = _burn_marker_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=binding,
        reservation=reservation,
        burn_guard=guard,
        execution_nonce=marker.get("execution_nonce"),
        consumed_at=marker.get("consumed_at"),
    )
    if marker != expected_marker:
        raise FineFrequencyRunError("T7 external execution burn marker differs")
    observed = {
        "schema": 3,
        "evidence_kind": "5g8_fine_frequency_global_run_burn_v3",
        "global_ledger_authority": binding["global_ledger_authority"],
        "global_ledger_authority_sha256": canonical_json_sha256(binding["global_ledger_authority"]),
        "run_state_ledger": binding,
        "reservation": reservation,
        "burn_guard": guard,
        "burn_marker": {
            **marker_identity,
            "sha256": sha256_path(marker_path),
            "size_bytes": marker_path.stat().st_size,
            "document": marker,
        },
        "burn_completed_before_source_dependency_fixture_or_hardware_access": True,
    }
    if burn_evidence is not None and observed != burn_evidence:
        raise FineFrequencyRunError("T7 external burn evidence binding differs")
    return observed


def _burn_run_ledger(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = _validate_prepared_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
    binding = prepared["binding"]
    reservation = prepared["reservation"]
    if progress is not None:
        progress["binding"] = binding
        progress["reservation"] = reservation
        progress["prepared_validation_passed"] = True
    execution_nonce = uuid.uuid4().hex
    expected_guard = {
        "sha256": hashlib.sha256(b"\x01").hexdigest(),
        "size_bytes": 1,
    }
    marker = _burn_marker_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=binding,
        reservation=reservation,
        burn_guard=expected_guard,
        execution_nonce=execution_nonce,
        consumed_at=_now(),
    )
    if progress is not None:
        progress["execution_nonce"] = execution_nonce
        progress["execution_document"] = marker
    burn_evidence = _global_ledger_mutation(
        authority=binding["global_ledger_authority"],
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": dict(binding["burn_guard"]),
            "document": marker,
        },
    )
    observed = _validate_consumed_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=binding,
        burn_evidence=None,
    )
    if progress is not None:
        progress["external_burn"] = observed
    observed_guard = observed["burn_guard"]
    observed_marker = observed["burn_marker"]
    response_guard = burn_evidence.get("guard")
    response_marker = burn_evidence.get("marker")
    if (
        burn_evidence.get("state") != "burn_complete"
        or burn_evidence.get("execution_nonce") != execution_nonce
        or burn_evidence.get("document_sha256") != canonical_json_sha256(marker)
        or not isinstance(response_guard, Mapping)
        or any(response_guard.get(key) != observed_guard.get(key) for key in observed_guard)
        or not isinstance(response_marker, Mapping)
        or any(
            response_marker.get(key) != observed_marker.get(key)
            for key in observed_marker
            if key != "document"
        )
    ):
        raise FineFrequencyRunError("privileged T7 atomic burn response differs from ledger bytes")
    return observed


def _execution_storage_paths(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
) -> tuple[Path, Path]:
    """Validate the exact local destinations frozen into one T7 plan."""

    storage = contract.get("execution_storage")
    if not isinstance(storage, Mapping):
        raise FineFrequencyRunError("immutable plan has no T7 execution storage contract")
    exact_plan = plan_path.expanduser().absolute()
    exact_manifest = manifest_path.expanduser().absolute()
    planned_state_root = Path(str(storage.get("state_root", ""))).expanduser().absolute()
    planned_run_root = Path(str(storage.get("run_root", ""))).expanduser().absolute()
    planned_capture_root = Path(str(storage.get("capture_root", ""))).expanduser().absolute()
    if (
        exact_plan != planned_run_root / PLAN_FILENAME
        or exact_manifest != planned_run_root / MANIFEST_FILENAME
        or planned_run_root
        != _run_root(
            planned_state_root,
            str(contract.get("board_id", "")),
            str(contract.get("run_id", "")),
        )
        or planned_capture_root != planned_run_root / "captures"
        or storage.get("medium") != "raspberry_pi_local_filesystem"
        or storage.get("pluto_onboard_storage_used") is not False
    ):
        raise FineFrequencyRunError("planned storage paths differ from the exact local run root")
    _validate_global_ledger_authority(contract, plan_path=exact_plan)
    for path, label in (
        (planned_state_root, "caller state root"),
        (exact_plan, "immutable plan"),
        (exact_manifest, "run manifest"),
        (planned_run_root, "run root"),
        (planned_capture_root, "capture root"),
    ):
        _assert_no_symlink_chain(path, label)
        _assert_local_rpi_storage(path)
    return planned_run_root, planned_capture_root


def _assert_prepared_run_unburned(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
    manifest_path: Path,
) -> tuple[Path, Path]:
    """Reject all surviving run history before revalidation or hardware access.

    A first T7 execution starts from exactly two preparation artifacts.  Any
    other entry is run-derived history, including a capture/staging/quarantine
    tree, analyzer result, execution/failure tombstone, or unknown destination.
    The generic allowlist also closes prepared-manifest rollback plus deleted
    tombstone attacks while any run-derived artifact survives.
    """

    run_root, capture_root = _execution_storage_paths(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
    )
    if run_root.is_symlink() or not run_root.is_dir():
        raise FineFrequencyRunError("T7 run root must be a regular non-symlink directory")
    if (
        plan_path.is_symlink()
        or not plan_path.is_file()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        raise FineFrequencyRunError("T7 preparation artifacts must be regular non-symlink files")
    allowed = {PLAN_FILENAME, MANIFEST_FILENAME}
    try:
        entries = tuple(run_root.iterdir())
    except OSError as error:
        raise FineFrequencyRunError(f"cannot scan T7 run history: {error}") from error
    run_derived = sorted(entry.name for entry in entries if entry.name not in allowed)
    if run_derived:
        raise FineFrequencyRunError(
            "run ID is already burned by surviving run-derived artifacts: " + ", ".join(run_derived)
        )
    manifest = _read_json(manifest_path, "run manifest")
    _validate_prepared_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
    return run_root, capture_root


def _reanalyze_coarse_results(run_root: Path) -> dict[str, Any]:
    """Invoke the source-bound offline analyzer; this path never opens RF hardware."""

    try:
        import scripts.analyze_5g8_fine_frequency as analyzer

        return analyzer.analyze_campaign(run_root)
    except Exception as error:
        raise FineFrequencyRunError(
            f"authoritative coarse campaign reanalysis failed: {error}"
        ) from error


def _load_coarse_results(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    results = _read_json(path, "coarse sweep results")
    if (
        results.get("schema") != 1
        or results.get("results_kind") != "5g8_bidirectional_frequency_results"
        or results.get("mode") != "coarse"
    ):
        raise FineFrequencyRunError("coarse results are not a completed T7 analysis")
    selection = results.get("refinement_selection")
    if not isinstance(selection, Mapping) or not selection.get("selected_centers_hz"):
        raise FineFrequencyRunError("coarse results have no deterministic refinement selection")
    plan_contract_sha256 = results.get("plan_contract_sha256")
    try:
        campaign_binding = validate_campaign_cross_binding(results.get("campaign_binding"))
    except FineFrequencyError as error:
        raise FineFrequencyRunError(f"coarse campaign cross-binding failed: {error}") from error
    if (
        not isinstance(plan_contract_sha256, str)
        or len(plan_contract_sha256) != 64
        or results.get("board_id") != campaign_binding["board_id"]
        or results.get("campaign_binding_sha256") != canonical_json_sha256(campaign_binding)
        or results.get("coarse_results_binding") is not None
        or selection.get("coarse_plan_contract_sha256") != plan_contract_sha256
        or selection.get("selection_kind") != "multiplicity_corrected_local_extrema_v1"
    ):
        raise FineFrequencyRunError("coarse selection does not bind its analyzed plan")
    coarse_plan_path = Path(str(results.get("plan_path", ""))).expanduser().absolute()
    _assert_no_symlink_chain(coarse_plan_path, "coarse immutable plan")
    if coarse_plan_path.name != PLAN_FILENAME:
        raise FineFrequencyRunError("coarse results do not identify an immutable T7 plan")
    recomputed = _reanalyze_coarse_results(coarse_plan_path.parent)
    if recomputed != results:
        raise FineFrequencyRunError("coarse results differ from source-bound raw-IQ reanalysis")
    return (
        {
            "path": str(path.expanduser().absolute()),
            "sha256": sha256_path(path.expanduser().absolute()),
            "size_bytes": path.expanduser().absolute().stat().st_size,
            "coarse_plan_contract_sha256": plan_contract_sha256,
            "campaign_binding": campaign_binding,
            "campaign_binding_sha256": canonical_json_sha256(campaign_binding),
        },
        dict(selection),
    )


def _build_contract(args: argparse.Namespace) -> dict[str, Any]:
    required = {
        "serial": args.serial,
        "uri": args.uri,
        "fixture_manifest": args.fixture_manifest,
        "setup_attestation": args.setup_attestation,
        "topology_stage": args.topology_stage,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise FineFrequencyRunError("planning arguments missing: " + ", ".join(missing))
    if not str(args.uri).startswith("usb:"):
        raise FineFrequencyRunError("T7 requires one exact usb: URI")
    run_root = _run_root(args.state_root, args.board_id, args.run_id)
    coarse_binding = None
    selection = None
    if args.plan_fine:
        if args.coarse_results is None:
            raise FineFrequencyRunError("--plan-fine requires --coarse-results")
        coarse_binding, selection = _load_coarse_results(args.coarse_results)
        schedule = build_fine_schedule(tuple(selection["selected_centers_hz"]))
    else:
        if args.coarse_results is not None:
            raise FineFrequencyRunError("--coarse-results is valid only with --plan-fine")
        schedule = build_coarse_schedule()
    contract = build_plan_contract(
        run_id=args.run_id,
        board_id=args.board_id,
        schedule=schedule,
        source_identity=_repository_source_identity(),
        native_identity=validate_runtime_attestation(attest_runtime()),
        fixture_identity=_fixture_identity(
            args.fixture_manifest,
            args.setup_attestation,
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            topology_stage=args.topology_stage,
            selector_flash_path=args.selector_flash_evidence,
            selector_flash_sha256=args.selector_flash_evidence_sha256,
            selector_flash_run_id=args.selector_flash_run_id,
            bench_manifest_path=args.bench_manifest,
            openocd_config_path=args.openocd_config,
            profile_path=args.profile,
        ),
        device_identity={"serial": args.serial, "uri": args.uri},
        free_bytes=_free_bytes(run_root),
        coarse_results_binding=coarse_binding,
        refinement_selection=selection,
    )
    return _augment_storage(
        contract,
        run_root,
        state_root=args.state_root,
    )


def _prepare_plan(
    plan_path: Path,
    manifest_path: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _execution_storage_paths(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
    )
    envelope = plan_envelope(contract)
    run_root = plan_path.parent
    if run_root.exists() or run_root.is_symlink():
        if (
            run_root.is_dir()
            and not run_root.is_symlink()
            and plan_path.is_file()
            and not plan_path.is_symlink()
            and manifest_path.is_file()
            and not manifest_path.is_symlink()
        ):
            observed = _read_json(plan_path, "immutable plan")
            manifest = _read_json(manifest_path, "run manifest")
            if observed == envelope and manifest.get("status") == "prepared":
                _assert_prepared_run_unburned(
                    contract,
                    plan_path=plan_path,
                    manifest_path=manifest_path,
                )
                return observed, manifest
        raise FineFrequencyRunError("run ID has prior plan, execution, or tombstone history")
    ledger_directory, *_ledger_files = _ledger_paths(contract, plan_path=plan_path)
    if ledger_directory.exists() or ledger_directory.is_symlink():
        raise FineFrequencyRunError("run ID already has external T7 ledger history")
    _assert_no_prior_anchor_history(contract, plan_path=plan_path)
    _write_immutable_json(plan_path, envelope)
    ledger_binding = _new_run_ledger_binding(contract=contract, run_root=run_root)
    manifest = _new_manifest(
        plan_path,
        envelope,
        ledger_binding=ledger_binding,
    )
    write_json_atomic(manifest_path, manifest)
    reservation = _reservation_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_binding=ledger_binding,
        prepared_manifest_sha256=sha256_path(manifest_path),
        prepared_manifest_size_bytes=manifest_path.stat().st_size,
        reserved_at=_now(),
    )
    _write_reserved_json_slot(
        Path(str(ledger_binding["reservation_slot"]["path"])),
        reservation,
        expected_identity=ledger_binding["reservation_slot"],
    )
    _assert_prepared_run_unburned(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
    )
    return envelope, manifest


def _validate_confirmations(
    args: argparse.Namespace, contract: Mapping[str, Any]
) -> dict[str, Any]:
    common_flags = {
        "experimental_policy_reviewed": args.confirm_experimental_policy,
        "no_antennas": args.confirm_no_antennas,
        "tx2_terminated_muted": args.confirm_tx2_terminated_muted,
        "rx1_protected_reference": args.confirm_rx1_protected_reference,
        "no_movement": args.confirm_no_movement,
    }
    missing = [name for name, passed in common_flags.items() if not passed]
    if missing:
        raise FineFrequencyRunError("missing execution confirmations: " + ", ".join(missing))
    fixture = contract["fixture_identity"]
    stage = str(fixture["topology_stage"])
    stage_flags = {
        "direct_rx2_termination": args.confirm_direct_rx2_termination,
        "rx2_cable_terminated": args.confirm_rx2_cable_terminated,
        "powered_selector_all_inputs_terminated": (
            args.confirm_powered_selector_all_inputs_terminated
        ),
        "full_conducted_fixture": args.confirm_fully_conducted,
    }
    if stage_flags.get(stage) is not True:
        raise FineFrequencyRunError(
            f"execution requires the truthful stage confirmation for {stage}"
        )
    contradictory = [name for name, value in stage_flags.items() if name != stage and value]
    if contradictory:
        raise FineFrequencyRunError(
            "contradictory topology confirmations: " + ", ".join(contradictory)
        )
    selector_connected = fixture.get("selector_connected") is True
    if args.confirm_selector_static_all_off is not selector_connected:
        requirement = "required" if selector_connected else "forbidden"
        raise FineFrequencyRunError(
            f"--confirm-selector-static-all-off is {requirement} for {stage}"
        )
    if args.confirm_topology_token != fixture["topology_token"]:
        raise FineFrequencyRunError(
            "execution requires --confirm-topology-token " + str(fixture["topology_token"])
        )
    if contract["experimental_policy"]["id"] != EXPERIMENTAL_POLICY:
        raise FineFrequencyRunError("plan does not carry the reviewed experimental policy")
    return {
        "confirmed_at": _now(),
        "topology_stage": stage,
        "topology_token": fixture["topology_token"],
        "selector_static_all_off": selector_connected,
        **common_flags,
        **{f"stage_{name}": value for name, value in stage_flags.items()},
    }


def _strict_mute(serial: str, purpose: str) -> dict[str, Any]:
    started_at = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "status": "failed",
            "serial": serial,
            "purpose": purpose,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    return {
        "status": "passed",
        "serial": serial,
        "purpose": purpose,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("purpose") == purpose
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
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
    if not blocks:
        raise FineFrequencyRunError("capture returned no metadata blocks")
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


def _tone_plan(contract: Mapping[str, Any], condition: Mapping[str, Any]) -> SafeDdsTonePlan:
    device = contract["device_identity"]
    return SafeDdsTonePlan(
        uri=str(device["uri"]),
        serial=str(device["serial"]),
        center_frequency_hz=int(condition["frequency_hz"]),
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


def _settings(center_frequency_hz: int) -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


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
        "local_rpi_storage": True,
        "pluto_storage_used": False,
    }


def _live_condition(
    contract: Mapping[str, Any],
    condition: Mapping[str, Any],
    capture_root: Path,
) -> dict[str, Any]:
    _assert_no_symlink_chain(capture_root, "capture root")
    _assert_local_rpi_storage(capture_root)
    serial = str(contract["device_identity"]["serial"])
    uri = str(contract["device_identity"]["uri"])
    fixture_identity = contract.get("fixture_identity")
    if not isinstance(fixture_identity, Mapping):
        raise FineFrequencyRunError("condition fixture identity is missing")
    selector_connected = fixture_identity.get("selector_connected") is True
    selector_control = fixture_identity.get("selector_control")
    if selector_connected and not isinstance(selector_control, Mapping):
        raise FineFrequencyRunError("selector-connected condition lacks frozen control")
    if not selector_connected and selector_control is not None:
        raise FineFrequencyRunError("selector-disconnected condition binds selector control")
    resolved_uri = resolve_iio_uri(uri, serial)
    if resolved_uri != uri:
        raise FineFrequencyRunError("current USB URI differs from the immutable plan")
    selector_before: dict[str, Any] | None = None
    selector_after: dict[str, Any] | None = None
    selector_cleanup: dict[str, Any] | None = None
    initial_mute = _strict_mute(serial, "pre_condition_exact_mute")
    if not _mute_passed(initial_mute, serial=serial, purpose="pre_condition_exact_mute"):
        if selector_connected:
            assert isinstance(selector_control, Mapping)
            selector_cleanup = leakage_runner._call_selector(
                leakage_runner._live_selector_all_off_boundary,
                selector_control,
                "condition_cleanup_all_off",
            )
        raise FineFrequencyRunError("initial exact-radio mute failed")
    if selector_connected:
        assert isinstance(selector_control, Mapping)
        selector_before = leakage_runner._call_selector(
            leakage_runner._live_selector_all_off_boundary,
            selector_control,
            "before_condition",
        )
        if not leakage_runner._selector_passed(
            selector_before,
            selector_control=selector_control,
            purpose="before_condition",
        ):
            selector_cleanup = leakage_runner._call_selector(
                leakage_runner._live_selector_all_off_boundary,
                selector_control,
                "condition_cleanup_all_off",
            )
            raise FineFrequencyRunError("selector static ALL_OFF pre-condition failed")
    plan = _tone_plan(contract, condition)
    settings = _settings(int(condition["frequency_hz"]))
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    capture: Any | None = None
    final_mute: dict[str, Any] | None = None
    pending_error: BaseException | None = None
    try:
        capture = capture_continuous_safe_dds_tone(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        pending_error = error
    finally:
        final_mute = _strict_mute(serial, "final_condition_exact_mute")
        if selector_connected:
            assert isinstance(selector_control, Mapping)
            selector_after = leakage_runner._call_selector(
                leakage_runner._live_selector_all_off_boundary,
                selector_control,
                "after_condition",
            )
            selector_cleanup = leakage_runner._call_selector(
                leakage_runner._live_selector_all_off_boundary,
                selector_control,
                "condition_cleanup_all_off",
            )
    if not _mute_passed(final_mute, serial=serial, purpose="final_condition_exact_mute"):
        pending_error = FineFrequencyRunError("mandatory final exact-radio mute failed")
    if selector_connected:
        assert isinstance(selector_control, Mapping)
        if not leakage_runner._selector_passed(
            selector_after,
            selector_control=selector_control,
            purpose="after_condition",
        ):
            pending_error = FineFrequencyRunError(
                "selector static ALL_OFF post-capture readback failed"
            )
        if not leakage_runner._selector_passed(
            selector_cleanup,
            selector_control=selector_control,
            purpose="condition_cleanup_all_off",
        ):
            pending_error = FineFrequencyRunError("selector static ALL_OFF cleanup failed")
    if pending_error is not None:
        retained.clear()
        raise pending_error
    assert capture is not None
    assert final_mute is not None
    if capture.identity.serial != serial or capture.identity.uri != uri:
        raise FineFrequencyRunError("capture identity differs from the exact device plan")
    if capture.settings != settings:
        raise FineFrequencyRunError("exact RX/TX LO or receiver settings readback failed")
    if (
        capture.sample_count != TOTAL_SAMPLES
        or len(capture.frames) != FRAME_COUNT
        or capture.kernel_buffers != KERNEL_BUFFERS
        or len(retained) != FRAME_COUNT
    ):
        raise FineFrequencyRunError("capture count differs from the immutable condition")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in retained):
        raise FineFrequencyRunError("capture frame is not exact dual-RX 100k IQ")
    ledger = _block_ledger(retained)
    continuity = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if continuity.metadata_abi != 2 or continuity.first_buffer_sequence != 0:
        raise FineFrequencyRunError("condition is not one fresh ABI2 stream")
    monitor = AdcHeadroomMonitor(receiver_count=2)
    for block in retained:
        monitor.observe(block.samples)
    headroom = monitor.result()
    clipped = sum(receiver.clipped_sample_count for receiver in headroom.receivers)
    if not headroom.passed or clipped:
        raise FineFrequencyRunError("ADC headroom admission failed")
    dds_frequencies = tuple(float(value) for value in capture.dds_frequency_readback_hz)
    tone_readback_hz = (abs(dds_frequencies[0]) + abs(dds_frequencies[2])) / 2.0
    rx1 = np.concatenate([block.samples[0] for block in retained])
    rx2 = np.concatenate([block.samples[1] for block in retained])
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=SAMPLE_RATE_HZ,
        nominal_tone_offset_hz=tone_readback_hz,
    )
    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=pilot.estimated_offset_hz,
    )
    transfer = analysis.rx2_over_rx1
    if not analysis.quality_passed or not analysis.rx1.tone_detected:
        raise FineFrequencyRunError(
            "exact-tone transfer quality failed: "
            + ", ".join(analysis.quality_rejection_reasons or ("RX1 reference tone not detected",))
        )
    if analysis.rx2.tone_detected:
        if transfer.phasor is None or transfer.amplitude_ratio is None:
            raise FineFrequencyRunError("detected RX2 transfer has no complex phasor")
    elif transfer.amplitude_upper_bound_ratio is None or (
        transfer.amplitude_upper_bound_ratio <= 0.0
    ):
        raise FineFrequencyRunError("RX2 nondetection has no positive amplitude upper bound")
    measurement = coherent_measurement_document(pilot, analysis)
    writer = CaptureWriter(
        capture_root / str(condition["condition_id"]),
        radio=capture.identity,
        settings=settings,
        label=f"T7 {condition['condition_id']} conducted frequency sweep",
    )
    try:
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
    except BaseException as error:
        writer.fail(error)
        raise
    finally:
        retained.clear()
    if not verify_artifact(artifact):
        raise FineFrequencyRunError("persisted artifact failed SHA-256 verification")
    base_artifact_info = _artifact_evidence(artifact)
    if base_artifact_info["data_size_bytes"] != BYTES_PER_CAPTURE:
        raise FineFrequencyRunError("persisted artifact size differs from the exact storage plan")
    metadata = load_metadata(artifact)
    persisted_continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
    )
    if persisted_continuity["stream_id"] != continuity.stream_id:
        raise FineFrequencyRunError("persisted stream identity differs from live evidence")
    rf_readback = {
        "rx_lo_hz": int(capture.settings.center_frequency_hz),
        "tx_lo_hz": int(capture.plan.center_frequency_hz),
        "lo_readback_provenance": "pluto_plus_utils_continuous_exact_condition_preflight",
        "sample_rate_hz": int(capture.settings.sample_rate_hz),
        "bandwidth_hz": int(capture.settings.bandwidth_hz),
        "tx1_gain_db": float(capture.tx_gain_readback_db),
        "tx2_gain_db": -80.0,
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_enabled_readback": list(capture.dds_enabled_readback),
        "dds_scale_readback": [abs(float(value)) for value in capture.dds_scale_readback],
        "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
    }
    device_evidence = {
        "serial": serial,
        "uri": uri,
        "usb_sysfs_path": str(find_usb_sysfs_path(serial)),
        "radio_identity": capture.identity.model_dump(mode="json"),
    }
    capture_evidence = {
        "stream_id": continuity.stream_id,
        "metadata_abi": continuity.metadata_abi,
        "first_buffer_sequence": continuity.first_buffer_sequence,
        "sample_count": TOTAL_SAMPLES,
        "frame_count": FRAME_COUNT,
        "kernel_buffers": capture.kernel_buffers,
        "continuity_passed": True,
        "headroom_passed": headroom.passed,
        "clipped_sample_count": clipped,
        "final_mute_passed": True,
        "live_ledger": ledger,
        "persisted_continuity": persisted_continuity,
    }
    selector_evidence = (
        None
        if not selector_connected
        else {
            "before": selector_before,
            "after": selector_after,
            "cleanup": selector_cleanup,
        }
    )
    safety = {
        "initial_mute": initial_mute,
        "final_mute": final_mute,
        "persistence_began_only_after_final_mute_passed": True,
        "selector_all_off_passed_before_persistence": (True if selector_connected else None),
    }
    condition_record = {
        "schema": 1,
        "record_kind": "5g8_fine_frequency_raw_condition_v1",
        "run_id": contract["run_id"],
        "board_id": contract["board_id"],
        "plan_contract_sha256": canonical_json_sha256(contract),
        "condition": dict(condition),
        "device": device_evidence,
        "rf_readback": rf_readback,
        "capture": capture_evidence,
        "artifact": artifact.model_dump(mode="json"),
        "artifact_evidence_without_condition_record": base_artifact_info,
        "analysis": measurement,
        "safety": safety,
        "selector_static_all_off": selector_evidence,
        "standalone_record_is_not_campaign_acceptance": True,
    }
    record_path = Path(artifact.path) / "5g8-fine-frequency-condition.json"
    _write_immutable_json(record_path, condition_record)
    artifact_info = {
        **base_artifact_info,
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "condition_record_size_bytes": record_path.stat().st_size,
    }
    evidence = {
        "schema": 1,
        "evidence_kind": "5g8_fine_frequency_condition_v1",
        "condition_id": condition["condition_id"],
        "status": "passed",
        "device": device_evidence,
        "rf_readback": rf_readback,
        "capture": capture_evidence,
        "artifact": artifact_info,
        "analysis": measurement,
        "safety": safety,
        "selector_static_all_off": selector_evidence,
    }
    return {
        "evidence": evidence,
        "artifact": artifact.model_dump(mode="json"),
        "condition_record": condition_record,
    }


def _execution_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    *,
    external_burn: Mapping[str, Any],
) -> dict[str, Any]:
    document = {
        "schema": 1,
        "marker_kind": "5g8_fine_frequency_execution_started",
        "run_id": contract["run_id"],
        "created_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "run_state_ledger_sha256": canonical_json_sha256(external_burn["run_state_ledger"]),
        "external_run_burn_sha256": canonical_json_sha256(external_burn),
        "external_burn_marker_sha256": external_burn["burn_marker"]["sha256"],
        "run_id_burned": True,
        "resume_or_splice_forbidden": True,
    }
    _write_immutable_json(path, document)
    return document


def _validated_campaign_cleanup(
    *,
    exact_mute: object,
    serial: str,
    exact_mute_purpose: str,
    selector_image_admission: object,
    selector_all_off: object,
    selector_control: object,
    selector_purpose: str,
) -> dict[str, Any]:
    def safe_evidence(value: object, label: str) -> Any:
        try:
            return _json_safe(value)
        except (TypeError, ValueError) as error:
            return {
                "schema": 1,
                "evidence_kind": "invalid_non_json_campaign_cleanup_evidence_v1",
                "label": label,
                "error": _error_document(error),
            }

    exact_mute_evidence = safe_evidence(exact_mute, "exact_pluto_mute")
    selector_image_evidence = safe_evidence(
        selector_image_admission,
        "selector_image_admission",
    )
    selector_all_off_evidence = safe_evidence(selector_all_off, "selector_all_off")
    exact_mute_validated = leakage_runner._mute_passed(
        exact_mute_evidence,
        serial=serial,
        purpose=exact_mute_purpose,
    )
    selector_image_validated: bool | None = None
    if selector_image_evidence is not None:
        try:
            selector_image_validated = isinstance(
                selector_control, Mapping
            ) and leakage_runner._selector_image_admission_passed(
                selector_image_evidence,
                selector_control=selector_control,
            )
        except (TypeError, ValueError):
            selector_image_validated = False
    selector_write_permitted = selector_image_validated is True
    selector_all_off_validated: bool | None = None
    if selector_write_permitted:
        try:
            selector_all_off_validated = isinstance(
                selector_control, Mapping
            ) and leakage_runner._selector_passed(
                selector_all_off_evidence,
                selector_control=selector_control,
                purpose=selector_purpose,
            )
        except (TypeError, ValueError):
            selector_all_off_validated = False
    no_unauthorized_selector_write = selector_write_permitted or selector_all_off_evidence is None
    cleanup_validated = (
        exact_mute_validated
        and no_unauthorized_selector_write
        and (not selector_write_permitted or selector_all_off_validated is True)
    )
    return {
        "schema": 1,
        "evidence_kind": "5g8_fine_frequency_campaign_cleanup_v1",
        "exact_pluto_mute": exact_mute_evidence,
        "exact_pluto_mute_validation_passed": exact_mute_validated,
        "selector_image_admission": selector_image_evidence,
        "selector_image_admission_validation_passed": selector_image_validated,
        "selector_all_off": selector_all_off_evidence,
        "selector_all_off_purpose": selector_purpose,
        "selector_all_off_validation_passed": selector_all_off_validated,
        "selector_write_permitted_by_image_admission": selector_write_permitted,
        "no_unauthorized_selector_write": no_unauthorized_selector_write,
        "cleanup_validation_passed": cleanup_validated,
    }


def _failure_tombstone(
    path: Path,
    contract: Mapping[str, Any],
    plan_path: Path,
    error: BaseException,
    *,
    interrupted: bool,
    failure_cleanup: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    failure_phase: str,
    cleanup_errors: Sequence[Mapping[str, Any]],
    persistence_errors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    cleanup_evidence = _json_safe(failure_cleanup)
    fixture_identity = contract.get("fixture_identity")
    selector_control = (
        fixture_identity.get("selector_control")
        if isinstance(fixture_identity, Mapping)
        and fixture_identity.get("selector_connected") is True
        else None
    )
    if failure_phase == "external_burn_acquisition":
        expected_cleanup = _validate_burn_acquisition_no_live_cleanup(
            cleanup_evidence,
            burn_receipt=burn_receipt,
        )
    else:
        expected_cleanup = _validated_campaign_cleanup(
            exact_mute=cleanup_evidence.get("exact_pluto_mute"),
            serial=str(contract["device_identity"]["serial"]),
            exact_mute_purpose="campaign_failure",
            selector_image_admission=cleanup_evidence.get("selector_image_admission"),
            selector_all_off=cleanup_evidence.get("selector_all_off"),
            selector_control=selector_control,
            selector_purpose="exception_cleanup_all_off",
        )
    # Never seal caller-reported cleanup booleans without recomputing the exact readbacks.
    if cleanup_evidence != expected_cleanup:
        raise FineFrequencyRunError(
            "failure cleanup evidence differs from exact mute/selector validation"
        )
    burn_marker = burn_receipt.get("burn_marker")
    burn_marker_sha256 = burn_marker.get("sha256") if isinstance(burn_marker, Mapping) else None
    document = {
        "schema": 3,
        "marker_kind": "5g8_fine_frequency_failed_run_v3",
        "run_id": contract["run_id"],
        "failed_at": _now(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "run_consumption_receipt": dict(burn_receipt),
        "run_consumption_receipt_sha256": canonical_json_sha256(burn_receipt),
        "external_run_burn_sha256": canonical_json_sha256(burn_receipt),
        "external_burn_marker_sha256": burn_marker_sha256,
        "error": _error_document(error),
        "failure_phase": failure_phase,
        "cleanup_errors": [dict(item) for item in cleanup_errors],
        "persistence_errors": [dict(item) for item in persistence_errors],
        "interrupted": interrupted,
        "campaign_accepted": False,
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
        "partial_artifacts_are_forensic_only": True,
        "failure_cleanup_evidence": cleanup_evidence,
        "failure_cleanup_evidence_sha256": canonical_json_sha256(cleanup_evidence),
    }
    _write_immutable_json(path, document)
    return document


def _operation_error(operation: str, error: BaseException) -> dict[str, Any]:
    return {"operation": operation, "error": _error_document(error)}


def _observed_current_invocation_burn(
    context: Mapping[str, Any],
) -> dict[str, Any] | None:
    external_burn = context.get("external_burn")
    if isinstance(external_burn, Mapping):
        return dict(external_burn)
    return None


def _safe_canonical_hash(
    value: object,
    *,
    operation: str,
    persistence_errors: list[dict[str, Any]],
) -> str | None:
    try:
        return canonical_json_sha256(value)
    except BaseException as error:
        persistence_errors.append(_operation_error(operation, error))
        return None


def _burn_acquisition_no_live_cleanup(
    *,
    classification: str,
    authoritative_inspection: Mapping[str, Any],
) -> dict[str, Any]:
    if classification not in {"pristine", "partial", "full"}:
        raise FineFrequencyRunError("burn-acquisition classification is invalid")
    return {
        "schema": 2,
        "evidence_kind": "5g8_fine_frequency_burn_acquisition_no_live_cleanup_v2",
        "burn_classification": classification,
        "authoritative_ledger_state": authoritative_inspection.get("classification"),
        "authoritative_inspection_sha256": canonical_json_sha256(authoritative_inspection),
        "exact_pluto_mute": None,
        "selector_image_admission": None,
        "selector_all_off": None,
        "live_cleanup_call_count": 0,
        "live_cleanup_prohibited": True,
        "cleanup_validation_passed": True,
    }


def _validate_burn_acquisition_no_live_cleanup(
    value: object,
    *,
    burn_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FineFrequencyRunError("burn-acquisition no-live cleanup evidence is missing")
    inspection = burn_receipt.get("authoritative_inspection")
    classification = burn_receipt.get("burn_classification")
    if not isinstance(inspection, Mapping) or not isinstance(classification, str):
        raise FineFrequencyRunError("burn-acquisition emergency evidence is malformed")
    expected = _burn_acquisition_no_live_cleanup(
        classification=classification,
        authoritative_inspection=inspection,
    )
    if dict(value) != expected:
        raise FineFrequencyRunError("burn-acquisition no-live cleanup evidence differs")
    return expected


def _external_failure_receipt_document(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    binding: Mapping[str, Any],
    phase: str,
    burn_receipt: Mapping[str, Any],
    original_record: Mapping[str, Any],
    failure_cleanup: Mapping[str, Any],
    cleanup_errors: Sequence[Mapping[str, Any]],
    persistence_attempts: Sequence[Mapping[str, Any]],
    persistence_errors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    inspection = burn_receipt.get("authoritative_inspection")
    burn_marker = burn_receipt.get("burn_marker")
    burn_marker_document = burn_marker.get("document") if isinstance(burn_marker, Mapping) else None
    execution_nonce = (
        inspection.get("execution_nonce")
        if isinstance(inspection, Mapping)
        else (
            burn_marker_document.get("execution_nonce")
            if isinstance(burn_marker_document, Mapping)
            else None
        )
    )
    return {
        "schema": 2,
        "marker_kind": "5g8_fine_frequency_external_failure_receipt_v2",
        "run_id": contract["run_id"],
        "board_id": contract["board_id"],
        "failed_at": _now(),
        "failure_phase": phase,
        "plan_path": str(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "global_ledger_authority": binding["global_ledger_authority"],
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(
            binding["global_ledger_authority"]
        ),
        "execution_nonce": execution_nonce,
        "reservation_receipt": burn_receipt.get("reservation"),
        "run_consumption_receipt": dict(burn_receipt),
        "original_error": dict(original_record),
        "failure_cleanup_evidence": dict(failure_cleanup),
        "cleanup_errors": [dict(item) for item in cleanup_errors],
        "persistence_attempts": [dict(item) for item in persistence_attempts],
        "persistence_errors": [dict(item) for item in persistence_errors],
        "campaign_accepted": False,
        "automatic_retry_forbidden": True,
        "partial_artifacts_are_forensic_only": True,
    }


def _seal_external_failure_receipt(
    *,
    binding: Mapping[str, Any],
    document: Mapping[str, Any],
) -> None:
    failure_receipt_path = Path(str(binding["failure_receipt_slot"]["path"]))
    evidence = _global_ledger_mutation(
        authority=binding["global_ledger_authority"],
        operation="seal_slot",
        payload={
            "slot": "failure",
            "document": dict(document),
            "expected_identity": dict(binding["failure_receipt_slot"]),
        },
    )
    observed = _file_evidence(
        failure_receipt_path,
        "sealed T7 external failure receipt",
        expected_nlink=2,
    )
    observed.update(
        {
            "mode": stat.S_IMODE(failure_receipt_path.stat().st_mode),
            "nlink": failure_receipt_path.stat().st_nlink,
        }
    )
    expected_evidence = {
        "slot": "failure",
        "file": observed,
        "document_sha256": canonical_json_sha256(document),
    }
    if evidence != expected_evidence:
        raise FineFrequencyRunError("privileged T7 failure-receipt response differs from bytes")


def _recover_failed_burn_acquisition(
    context: dict[str, Any],
    original_error: BaseException,
    *,
    confirmations: Mapping[str, Any],
) -> None:
    """Classify an uncertain burn using only authoritative storage; never touch RF."""

    progress = context.get("burn_progress")
    contract = context.get("contract")
    manifest = context.get("manifest")
    plan_path = context.get("plan_path")
    manifest_path = context.get("manifest_path")
    if (
        not isinstance(progress, Mapping)
        or not isinstance(contract, Mapping)
        or not isinstance(manifest, dict)
        or not isinstance(plan_path, Path)
        or not isinstance(manifest_path, Path)
    ):
        return
    binding = progress.get("binding")
    reservation = progress.get("reservation")
    if not isinstance(binding, Mapping) or not isinstance(reservation, Mapping):
        return
    try:
        inspection = _global_ledger_inspection(binding["global_ledger_authority"])
    except BaseException as inspection_error:
        original_error.add_note(
            "burn acquisition remained unclassified; no live cleanup was attempted: "
            + repr(inspection_error)
        )
        return
    state = inspection.get("classification")
    classification = {
        "prepared": "pristine",
        "burn_committed_guard_pending": "partial",
        "burn_complete": "full",
    }.get(state)
    if classification is None:
        original_error.add_note(
            f"burn acquisition inspection returned non-recoverable state {state!r}; "
            "no live cleanup was attempted"
        )
        return
    burn_receipt = {
        "schema": 2,
        "evidence_kind": "5g8_fine_frequency_burn_acquisition_emergency_v2",
        "burn_classification": classification,
        "authoritative_state": state,
        "authoritative_inspection": inspection,
        "authoritative_inspection_sha256": canonical_json_sha256(inspection),
        "global_ledger_authority": binding["global_ledger_authority"],
        "global_ledger_authority_sha256": canonical_json_sha256(binding["global_ledger_authority"]),
        "run_state_ledger": dict(binding),
        "reservation": dict(reservation),
        "live_access_began": False,
        "live_cleanup_call_count": 0,
        "automatic_retry_forbidden": True,
    }
    failure_cleanup = _burn_acquisition_no_live_cleanup(
        classification=classification,
        authoritative_inspection=inspection,
    )
    phase = "external_burn_acquisition"
    original_record = {"phase": phase, "error": _error_document(original_error)}
    persistence_errors: list[dict[str, Any]] = []
    persistence_attempts: list[dict[str, Any]] = []
    attempt = {
        "started_at": None,
        "status": "failed",
        "completed_at": _now(),
        "confirmations": _json_safe(confirmations),
        "external_run_burn": burn_receipt,
        "external_run_burn_sha256": canonical_json_sha256(burn_receipt),
        "completed_condition_count": 0,
        "campaign_final_cleanup": failure_cleanup,
        "campaign_final_cleanup_sha256": canonical_json_sha256(failure_cleanup),
        "error": _error_document(original_error),
    }
    manifest["status"] = "failed"
    manifest["attempts"] = [attempt]
    manifest["campaign_accepted"] = False
    manifest["accepted_condition_count"] = 0
    manifest["condition_results"] = []
    manifest["error"] = attempt["error"]
    manifest["updated_at"] = _now()
    try:
        write_json_atomic(manifest_path, manifest)
        persistence_attempts.append(
            {"operation": "persist_burn_acquisition_failed_manifest", "status": "passed"}
        )
    except BaseException as error:
        record = _operation_error("persist_burn_acquisition_failed_manifest", error)
        persistence_errors.append(record)
        persistence_attempts.append({**record, "status": "failed"})
    failure_path = manifest_path.parent / FAILURE_TOMBSTONE_FILENAME
    failure: dict[str, Any] | None = None
    try:
        failure = _failure_tombstone(
            failure_path,
            contract,
            plan_path,
            original_error,
            interrupted=isinstance(original_error, KeyboardInterrupt),
            failure_cleanup=failure_cleanup,
            burn_receipt=burn_receipt,
            failure_phase=phase,
            cleanup_errors=[],
            persistence_errors=persistence_errors,
        )
        persistence_attempts.append(
            {"operation": "persist_burn_acquisition_failure_tombstone", "status": "passed"}
        )
    except BaseException as error:
        record = _operation_error("persist_burn_acquisition_failure_tombstone", error)
        persistence_errors.append(record)
        persistence_attempts.append({**record, "status": "failed"})
    if failure is not None:
        try:
            manifest["failure_tombstone"] = {
                "path": str(failure_path),
                "sha256": sha256_path(failure_path),
                "document": failure,
            }
            write_json_atomic(manifest_path, manifest)
            persistence_attempts.append(
                {"operation": "bind_burn_acquisition_failure_tombstone", "status": "passed"}
            )
        except BaseException as error:
            record = _operation_error("bind_burn_acquisition_failure_tombstone", error)
            persistence_errors.append(record)
            persistence_attempts.append({**record, "status": "failed"})
    receipt = _external_failure_receipt_document(
        contract=contract,
        plan_path=plan_path,
        binding=binding,
        phase=phase,
        burn_receipt=burn_receipt,
        original_record=original_record,
        failure_cleanup=failure_cleanup,
        cleanup_errors=[],
        persistence_attempts=persistence_attempts,
        persistence_errors=persistence_errors,
    )
    try:
        _seal_external_failure_receipt(binding=binding, document=receipt)
    except BaseException as error:
        original_error.add_note(
            "burn-acquisition emergency receipt persistence failed: " + repr(error)
        )


def _recover_failed_burned_execution(
    context: dict[str, Any],
    original_error: BaseException,
    *,
    confirmations: Mapping[str, Any],
    mute_boundary: leakage_runner.MuteBoundary,
    selector_image_boundary: leakage_runner.SelectorImageBoundary,
    selector_boundary: leakage_runner.SelectorBoundary,
) -> None:
    burn_receipt = _observed_current_invocation_burn(context)
    if burn_receipt is None:
        return
    contract = context.get("contract")
    manifest = context.get("manifest")
    plan_path = context.get("plan_path")
    manifest_path = context.get("manifest_path")
    if (
        not isinstance(contract, Mapping)
        or not isinstance(manifest, dict)
        or not isinstance(plan_path, Path)
        or not isinstance(manifest_path, Path)
    ):
        original_error.add_note("burn recovery lacked immutable execution context")
        return
    serial = str(contract["device_identity"]["serial"])
    phase = str(context.get("phase", "unknown_post_burn_phase"))
    original_record = {"phase": phase, "error": _error_document(original_error)}
    cleanup_errors: list[dict[str, Any]] = []
    persistence_errors: list[dict[str, Any]] = []
    if any(token in phase for token in ("persist", "hash", "tombstone", "attempt")):
        persistence_errors.append({"operation": phase, "error": _error_document(original_error)})

    failure_mute = leakage_runner._call_mute(mute_boundary, serial, "campaign_failure")
    if not leakage_runner._mute_passed(
        failure_mute,
        serial=serial,
        purpose="campaign_failure",
    ):
        cleanup_errors.append(
            {
                "operation": "campaign_failure_exact_mute",
                "error": failure_mute.get("error"),
            }
        )

    selector_connected = context.get("selector_connected") is True
    selector_control = context.get("selector_control")
    selector_image_admission = context.get("selector_image_admission")
    selector_cleanup_authorized = context.get("selector_cleanup_authorized") is True
    if (
        selector_connected
        and isinstance(selector_control, Mapping)
        and not selector_cleanup_authorized
        and selector_image_admission is None
    ):
        selector_image_admission = leakage_runner._call_selector_image_admission(
            selector_image_boundary,
            selector_control,
        )
        try:
            selector_cleanup_authorized = leakage_runner._selector_image_admission_passed(
                selector_image_admission,
                selector_control=selector_control,
            )
        except BaseException as error:
            selector_cleanup_authorized = False
            cleanup_errors.append(_operation_error("cleanup_selector_image_validation", error))
    if selector_connected and not selector_cleanup_authorized:
        cleanup_errors.append(
            {
                "operation": "cleanup_selector_image_admission",
                "error": (
                    selector_image_admission.get("error")
                    if isinstance(selector_image_admission, Mapping)
                    else None
                ),
            }
        )
    failure_selector = (
        leakage_runner._call_selector(
            selector_boundary,
            selector_control,
            "exception_cleanup_all_off",
        )
        if selector_cleanup_authorized and isinstance(selector_control, Mapping)
        else None
    )
    if (
        selector_cleanup_authorized
        and isinstance(selector_control, Mapping)
        and not (
            leakage_runner._selector_passed(
                failure_selector,
                selector_control=selector_control,
                purpose="exception_cleanup_all_off",
            )
        )
    ):
        cleanup_errors.append(
            {
                "operation": "exception_cleanup_all_off",
                "error": (
                    failure_selector.get("error") if isinstance(failure_selector, Mapping) else None
                ),
            }
        )
    try:
        failure_cleanup = _validated_campaign_cleanup(
            exact_mute=failure_mute,
            serial=serial,
            exact_mute_purpose="campaign_failure",
            selector_image_admission=selector_image_admission,
            selector_all_off=failure_selector,
            selector_control=selector_control,
            selector_purpose="exception_cleanup_all_off",
        )
    except BaseException as error:
        cleanup_errors.append(_operation_error("validate_failure_cleanup", error))
        failure_cleanup = {
            "schema": 1,
            "evidence_kind": "5g8_fine_frequency_unvalidated_campaign_cleanup_v1",
            "exact_pluto_mute": failure_mute,
            "selector_image_admission": selector_image_admission,
            "selector_all_off": failure_selector,
            "cleanup_validation_passed": False,
        }

    attempt_value = context.get("attempt")
    attempt = attempt_value if isinstance(attempt_value, dict) else {}
    attempt.setdefault("started_at", context.get("attempt_started_at"))
    attempt["status"] = "failed"
    attempt["completed_at"] = _now()
    attempt["confirmations"] = attempt.get("confirmations", dict(confirmations))
    attempt["external_run_burn"] = burn_receipt
    attempt["external_run_burn_sha256"] = _safe_canonical_hash(
        burn_receipt,
        operation="hash_failure_burn_receipt",
        persistence_errors=persistence_errors,
    )
    attempt["campaign_final_cleanup"] = failure_cleanup
    attempt["campaign_final_cleanup_sha256"] = _safe_canonical_hash(
        failure_cleanup,
        operation="hash_failure_cleanup",
        persistence_errors=persistence_errors,
    )
    attempt["error"] = _error_document(original_error)
    results = context.get("results")
    condition_results = results if isinstance(results, list) else []
    attempt["completed_condition_count"] = len(condition_results)
    manifest["status"] = "failed"
    manifest["attempts"] = [attempt]
    manifest["campaign_accepted"] = False
    manifest["accepted_condition_count"] = 0
    manifest["condition_results"] = condition_results
    manifest["error"] = attempt["error"]
    manifest["updated_at"] = _now()

    persistence_attempts: list[dict[str, Any]] = []
    try:
        write_json_atomic(manifest_path, manifest)
        persistence_attempts.append(
            {"operation": "persist_failed_manifest_before_tombstone", "status": "passed"}
        )
    except BaseException as error:
        record = _operation_error("persist_failed_manifest_before_tombstone", error)
        persistence_errors.append(record)
        persistence_attempts.append({**record, "status": "failed"})

    failure_path = manifest_path.parent / FAILURE_TOMBSTONE_FILENAME
    failure: dict[str, Any] | None = None
    try:
        failure = _failure_tombstone(
            failure_path,
            contract,
            plan_path,
            original_error,
            interrupted=isinstance(original_error, KeyboardInterrupt),
            failure_cleanup=failure_cleanup,
            burn_receipt=burn_receipt,
            failure_phase=phase,
            cleanup_errors=cleanup_errors,
            persistence_errors=persistence_errors,
        )
        persistence_attempts.append(
            {"operation": "persist_run_root_failure_tombstone", "status": "passed"}
        )
    except BaseException as error:
        record = _operation_error("persist_run_root_failure_tombstone", error)
        persistence_errors.append(record)
        persistence_attempts.append({**record, "status": "failed"})
    if failure is not None:
        try:
            manifest["failure_tombstone"] = {
                "path": str(failure_path),
                "sha256": sha256_path(failure_path),
                "document": failure,
            }
            write_json_atomic(manifest_path, manifest)
            persistence_attempts.append(
                {"operation": "persist_failed_manifest_tombstone_binding", "status": "passed"}
            )
        except BaseException as error:
            record = _operation_error("persist_failed_manifest_tombstone_binding", error)
            persistence_errors.append(record)
            persistence_attempts.append({**record, "status": "failed"})

    binding = burn_receipt.get("run_state_ledger")
    if not isinstance(binding, Mapping):
        original_error.add_note("external failure receipt unavailable: ledger binding missing")
        return
    receipt_document = _external_failure_receipt_document(
        contract=contract,
        plan_path=plan_path,
        binding=binding,
        phase=phase,
        burn_receipt=burn_receipt,
        original_record=original_record,
        failure_cleanup=failure_cleanup,
        cleanup_errors=cleanup_errors,
        persistence_attempts=persistence_attempts,
        persistence_errors=persistence_errors,
    )
    try:
        _seal_external_failure_receipt(binding=binding, document=receipt_document)
    except BaseException as error:
        original_error.add_note("external failure receipt persistence failed: " + str(error))
    for secondary in (*cleanup_errors, *persistence_errors):
        original_error.add_note(json.dumps(secondary, sort_keys=True))


def _execute_prepared_once(
    *,
    plan_path: Path,
    manifest_path: Path,
    confirmations: Mapping[str, Any],
    condition_boundary: ConditionBoundary = _live_condition,
    preflight_boundary: Callable[[Mapping[str, Any]], None] | None = None,
    acceptance_boundary: Callable[[Mapping[str, Any]], None] | None = None,
    mute_boundary: leakage_runner.MuteBoundary = _strict_mute,
    selector_image_boundary: leakage_runner.SelectorImageBoundary = (
        leakage_runner._live_selector_image_admission
    ),
    selector_boundary: leakage_runner.SelectorBoundary = (
        leakage_runner._live_selector_all_off_boundary
    ),
    failure_context: dict[str, Any],
) -> dict[str, Any]:
    envelope = _read_json(plan_path, "immutable plan")
    contract = validate_plan_envelope(envelope)
    manifest = _read_json(manifest_path, "run manifest")
    expected_manifest_identity = (
        manifest.get("schema") == 1
        and manifest.get("manifest_kind") == RUN_KIND
        and manifest.get("run_id") == contract.get("run_id")
        and manifest.get("plan_path") == str(plan_path)
        and manifest.get("plan_sha256") == sha256_path(plan_path)
        and manifest.get("plan_contract_sha256") == canonical_json_sha256(contract)
    )
    if not expected_manifest_identity:
        raise FineFrequencyRunError("manifest identity differs from the immutable plan")
    _planned_run_root, planned_capture_root = _assert_prepared_run_unburned(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
    )
    execution_path = manifest_path.parent / EXECUTION_TOMBSTONE_FILENAME
    required_free = int(contract["storage"]["required_free_bytes"])
    observed_free = _free_bytes(manifest_path.parent)
    if observed_free < required_free:
        raise FineFrequencyRunError(
            f"execution free space {observed_free} is below exact two-times gate {required_free}"
        )
    serial = str(contract["device_identity"]["serial"])
    fixture_for_cleanup = contract.get("fixture_identity")
    selector_connected = (
        isinstance(fixture_for_cleanup, Mapping)
        and fixture_for_cleanup.get("selector_connected") is True
    )
    selector_control: object = (
        fixture_for_cleanup.get("selector_control")
        if isinstance(fixture_for_cleanup, Mapping)
        else None
    )
    burn_progress: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    failure_context.update(
        {
            "phase": "external_burn_acquisition",
            "contract": contract,
            "manifest": manifest,
            "plan_path": plan_path,
            "manifest_path": manifest_path,
            "serial": serial,
            "selector_connected": selector_connected,
            "selector_control": selector_control,
            "selector_image_admission": None,
            "selector_cleanup_authorized": False,
            "burn_progress": burn_progress,
            "external_burn": None,
            "attempt": None,
            "results": results,
        }
    )
    external_burn = _burn_run_ledger(
        contract=contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
        progress=burn_progress,
    )
    failure_context["external_burn"] = external_burn
    failure_context["phase"] = "execution_tombstone_persistence"
    execution = _execution_tombstone(
        execution_path,
        contract,
        plan_path,
        external_burn=external_burn,
    )
    failure_context["phase"] = "execution_tombstone_hash_and_attempt_construction"
    attempt_started_at = _now()
    failure_context["attempt_started_at"] = attempt_started_at
    attempt = {
        "started_at": attempt_started_at,
        "status": "running",
        "confirmations": _json_safe(confirmations),
        "execution_tombstone": {
            "path": str(execution_path),
            "sha256": sha256_path(execution_path),
            "document": execution,
        },
        "external_run_burn": external_burn,
        "external_run_burn_sha256": canonical_json_sha256(external_burn),
        "completed_condition_count": 0,
        "campaign_preflight_exact_mute": None,
        "campaign_preflight_exact_mute_sha256": None,
        "selector_connected_preflight": None,
        "selector_connected_preflight_sha256": None,
        "campaign_final_cleanup": None,
        "campaign_final_cleanup_sha256": None,
        "error": None,
    }
    failure_context["attempt"] = attempt
    manifest["status"] = "running"
    manifest["attempts"] = [attempt]
    manifest["updated_at"] = _now()
    stream_ids: set[int] = set()
    artifact_hashes: set[str] = set()
    selector_image_admission: dict[str, Any] | None = None
    selector_cleanup_authorized = False
    try:
        failure_context["phase"] = "running_attempt_manifest_persistence"
        write_json_atomic(manifest_path, manifest)
        failure_context["phase"] = "campaign_preflight"
        if preflight_boundary is not None:
            preflight_boundary(contract)
        fixture_identity = contract.get("fixture_identity")
        if not isinstance(fixture_identity, Mapping):
            raise FineFrequencyRunError("immutable T7 fixture identity is missing")
        selector_connected = fixture_identity.get("selector_connected") is True
        selector_control = fixture_identity.get("selector_control")
        failure_context["selector_connected"] = selector_connected
        failure_context["selector_control"] = selector_control
        if selector_connected and not isinstance(selector_control, Mapping):
            raise FineFrequencyRunError("selector-connected T7 plan lacks frozen selector control")
        if not selector_connected and selector_control is not None:
            raise FineFrequencyRunError("selector-disconnected T7 plan binds selector control")
        selector_preflight: dict[str, Any] | None = None
        if selector_connected:
            assert isinstance(selector_control, Mapping)
            selector_preflight = {
                "exact_pluto_mute": None,
                "target_full_bin_uid_admission": None,
                "first_mailbox_operation": None,
                "required_order": [
                    "exact_pluto_mute",
                    "target_full_bin_uid_admission",
                    "first_mailbox_operation",
                ],
                "observed_order": [],
                "passed": False,
            }
            attempt["selector_connected_preflight"] = selector_preflight
            write_json_atomic(manifest_path, manifest)
        initial_mute = leakage_runner._call_mute(
            mute_boundary,
            serial,
            "campaign_preflight",
        )
        attempt["campaign_preflight_exact_mute"] = initial_mute
        attempt["campaign_preflight_exact_mute_sha256"] = canonical_json_sha256(initial_mute)
        if selector_preflight is not None:
            selector_preflight["exact_pluto_mute"] = initial_mute
        write_json_atomic(manifest_path, manifest)
        if not leakage_runner._mute_passed(
            initial_mute,
            serial=serial,
            purpose="campaign_preflight",
        ):
            raise FineFrequencyRunError("T7 campaign exact Pluto mute failed")
        if selector_preflight is not None:
            selector_preflight["observed_order"].append("exact_pluto_mute")
        if selector_connected:
            assert isinstance(selector_control, Mapping)
            assert selector_preflight is not None
            selector_image_admission = leakage_runner._call_selector_image_admission(
                selector_image_boundary,
                selector_control,
            )
            failure_context["selector_image_admission"] = selector_image_admission
            selector_preflight["target_full_bin_uid_admission"] = selector_image_admission
            # This authorization must survive any failure of the evidence write below.
            selector_cleanup_authorized = leakage_runner._selector_image_admission_passed(
                selector_image_admission,
                selector_control=selector_control,
            )
            failure_context["selector_cleanup_authorized"] = selector_cleanup_authorized
            if selector_cleanup_authorized:
                selector_preflight["observed_order"].append("target_full_bin_uid_admission")
            write_json_atomic(manifest_path, manifest)
            if not selector_cleanup_authorized:
                raise FineFrequencyRunError(
                    "T7 selector target full-BIN extent or UID admission failed"
                )
            initial_selector = leakage_runner._call_selector(
                selector_boundary,
                selector_control,
                "initial_state_before_command",
            )
            selector_preflight["first_mailbox_operation"] = initial_selector
            initial_selector_passed = leakage_runner._selector_passed(
                initial_selector,
                selector_control=selector_control,
                purpose="initial_state_before_command",
            )
            if initial_selector_passed:
                selector_preflight["observed_order"].append("first_mailbox_operation")
                selector_preflight["passed"] = True
                attempt["selector_connected_preflight_sha256"] = canonical_json_sha256(
                    selector_preflight
                )
            write_json_atomic(manifest_path, manifest)
            if not initial_selector_passed:
                raise FineFrequencyRunError("T7 first selector mailbox readback failed")
        capture_root = planned_capture_root
        failure_context["phase"] = "campaign_condition_capture_and_persistence"
        for condition in contract["schedule"]["conditions"]:
            returned = condition_boundary(contract, condition, capture_root)
            if not isinstance(returned, Mapping) or not isinstance(
                returned.get("evidence"), Mapping
            ):
                raise FineFrequencyRunError("condition boundary returned malformed evidence")
            evidence = validate_live_condition_evidence(
                contract,
                returned["evidence"],
                prior_stream_ids=stream_ids,
                prior_artifact_sha256s=artifact_hashes,
            )
            stream_id = int(evidence["capture"]["stream_id"])
            artifact_sha = str(evidence["artifact"]["data_sha256"])
            stream_ids.add(stream_id)
            artifact_hashes.add(artifact_sha)
            normalized = normalized_observation_from_evidence(condition, evidence)
            result = {
                "plan_index": condition["plan_index"],
                "condition_id": condition["condition_id"],
                "evidence": evidence,
                "evidence_sha256": canonical_json_sha256(evidence),
                "normalized_observation": normalized,
                "boundary_result": {
                    key: value for key, value in returned.items() if key != "evidence"
                },
                "campaign_acceptance_pending": True,
            }
            results.append(result)
            manifest["condition_results"] = results
            attempt["completed_condition_count"] = len(results)
            manifest["updated_at"] = _now()
            write_json_atomic(manifest_path, manifest)
        expected_count = int(contract["storage"]["condition_count"])
        if len(results) != expected_count:
            raise FineFrequencyRunError("completed condition count differs from immutable plan")
        if acceptance_boundary is not None:
            acceptance_boundary(contract)
        final_mute = leakage_runner._call_mute(mute_boundary, serial, "campaign_final")
        final_selector = (
            leakage_runner._call_selector(
                selector_boundary,
                selector_control,
                "final_cleanup_all_off",
            )
            if selector_cleanup_authorized and isinstance(selector_control, Mapping)
            else None
        )
        final_cleanup = _validated_campaign_cleanup(
            exact_mute=final_mute,
            serial=serial,
            exact_mute_purpose="campaign_final",
            selector_image_admission=selector_image_admission,
            selector_all_off=final_selector,
            selector_control=selector_control,
            selector_purpose="final_cleanup_all_off",
        )
        attempt["campaign_final_cleanup"] = final_cleanup
        attempt["campaign_final_cleanup_sha256"] = canonical_json_sha256(final_cleanup)
        if final_cleanup["cleanup_validation_passed"] is not True:
            raise FineFrequencyRunError("T7 campaign final mute/ALL_OFF cleanup failed")
        for result in results:
            result["campaign_acceptance_pending"] = False
            result["campaign_accepted"] = True
        attempt["status"] = "complete"
        attempt["completed_at"] = _now()
        manifest["status"] = "complete"
        manifest["campaign_accepted"] = True
        manifest["accepted_condition_count"] = expected_count
        manifest["condition_results"] = results
        manifest["updated_at"] = _now()
        manifest["error"] = None
        failure_context["phase"] = "complete_manifest_persistence"
        write_json_atomic(manifest_path, manifest)
    except BaseException:
        failure_context["selector_image_admission"] = selector_image_admission
        failure_context["selector_cleanup_authorized"] = selector_cleanup_authorized
        raise
    return manifest


def _execute_prepared(
    *,
    plan_path: Path,
    manifest_path: Path,
    confirmations: Mapping[str, Any],
    condition_boundary: ConditionBoundary = _live_condition,
    preflight_boundary: Callable[[Mapping[str, Any]], None] | None = None,
    acceptance_boundary: Callable[[Mapping[str, Any]], None] | None = None,
    mute_boundary: leakage_runner.MuteBoundary = _strict_mute,
    selector_image_boundary: leakage_runner.SelectorImageBoundary = (
        leakage_runner._live_selector_image_admission
    ),
    selector_boundary: leakage_runner.SelectorBoundary = (
        leakage_runner._live_selector_all_off_boundary
    ),
) -> dict[str, Any]:
    """Execute under one burn-to-completion cleanup and failure-evidence envelope."""

    failure_context: dict[str, Any] = {}
    try:
        return _execute_prepared_once(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations=confirmations,
            condition_boundary=condition_boundary,
            preflight_boundary=preflight_boundary,
            acceptance_boundary=acceptance_boundary,
            mute_boundary=mute_boundary,
            selector_image_boundary=selector_image_boundary,
            selector_boundary=selector_boundary,
            failure_context=failure_context,
        )
    except BaseException as error:
        try:
            if failure_context.get("phase") == "external_burn_acquisition":
                _recover_failed_burn_acquisition(
                    failure_context,
                    error,
                    confirmations=confirmations,
                )
            else:
                _recover_failed_burned_execution(
                    failure_context,
                    error,
                    confirmations=confirmations,
                    mute_boundary=mute_boundary,
                    selector_image_boundary=selector_image_boundary,
                    selector_boundary=selector_boundary,
                )
        except BaseException as recovery_error:
            error.add_note("unexpected burn recovery failure: " + repr(recovery_error))
        raise


def _validate_current_bindings(contract: Mapping[str, Any]) -> None:
    current_source = _repository_source_identity()
    if current_source != contract.get("source_identity"):
        raise FineFrequencyRunError("current source/dependency identity differs from the plan")
    current_native = validate_runtime_attestation(attest_runtime())
    if current_native != contract.get("native_identity"):
        raise FineFrequencyRunError("current native libiio identity differs from the plan")
    _verify_fixture_identity(contract.get("fixture_identity"))
    coarse = contract.get("coarse_results_binding")
    if coarse is not None:
        _verify_file_identity(coarse, "coarse results")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri")
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument("--setup-attestation", type=Path)
    parser.add_argument("--selector-flash-evidence", type=Path)
    parser.add_argument("--selector-flash-evidence-sha256")
    parser.add_argument("--selector-flash-run-id")
    parser.add_argument("--bench-manifest", type=Path)
    parser.add_argument("--openocd-config", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--topology-stage", choices=tuple(TOPOLOGIES))
    parser.add_argument("--coarse-results", type=Path)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local/state/smateway",
        help="local Raspberry Pi filesystem root; Pluto storage is never used",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-coarse", action="store_true")
    action.add_argument("--plan-fine", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-experimental-policy", action="store_true")
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-direct-rx2-termination", action="store_true")
    parser.add_argument("--confirm-rx2-cable-terminated", action="store_true")
    parser.add_argument("--confirm-powered-selector-all-inputs-terminated", action="store_true")
    parser.add_argument("--confirm-fully-conducted", action="store_true")
    parser.add_argument("--confirm-selector-static-all-off", action="store_true")
    parser.add_argument("--confirm-tx2-terminated-muted", action="store_true")
    parser.add_argument("--confirm-rx1-protected-reference", action="store_true")
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
        run_root = _run_root(args.state_root, args.board_id, args.run_id)
        plan_path = run_root / PLAN_FILENAME
        manifest_path = run_root / MANIFEST_FILENAME
        if args.plan_coarse or args.plan_fine:
            contract = _build_contract(args)
            envelope, manifest = _prepare_plan(plan_path, manifest_path, contract)
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "mode": contract["mode"],
                        "status": manifest["status"],
                        "condition_count": contract["storage"]["condition_count"],
                        "required_free_bytes": contract["storage"]["required_free_bytes"],
                        "plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        envelope = _read_json(plan_path, "immutable plan")
        contract = validate_plan_envelope(envelope)
        if contract.get("run_id") != args.run_id or contract.get("board_id") != args.board_id:
            raise FineFrequencyRunError("execution target differs from the immutable plan")
        device = contract["device_identity"]
        fixture = contract["fixture_identity"]
        if args.serial != device["serial"] or (args.uri is not None and args.uri != device["uri"]):
            raise FineFrequencyRunError("execution serial/URI differs from the immutable plan")
        if args.topology_stage is not None and args.topology_stage != fixture["topology_stage"]:
            raise FineFrequencyRunError("execution topology differs from the immutable plan")
        _assert_prepared_run_unburned(
            contract,
            plan_path=plan_path,
            manifest_path=manifest_path,
        )
        confirmations = _validate_confirmations(args, contract)
        manifest = _execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            confirmations=confirmations,
            preflight_boundary=_validate_current_bindings,
            acceptance_boundary=_validate_current_bindings,
        )
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "status": manifest["status"],
                    "accepted_condition_count": manifest["accepted_condition_count"],
                    "manifest": str(manifest_path),
                    "next": (
                        f"PYTHONPATH={_SMATEWAY_SOURCE} {_PINNED_PYTHON} "
                        f"{_REPOSITORY / 'scripts/analyze_5g8_fine_frequency.py'} "
                        f"{run_root}"
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    except (FineFrequencyError, FineFrequencyRunError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
