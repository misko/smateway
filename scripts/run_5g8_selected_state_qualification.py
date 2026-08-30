#!/usr/bin/env python3
"""Plan, capture, and independently qualify selected-state re-entry evidence.

``prepare`` and ``analyze`` are deliberately RF-inert.  ``capture`` is the only
action that may open the exact planned Pluto and selector.  It produces raw
dual-RX CI16, SigMF ABI-2 metadata, and a condition record under local Raspberry
Pi storage.  Analysis then reopens those files, verifies every byte identity,
reaudits continuity and RF/selector readbacks, and recomputes transfer/timing
from IQ.  Caller-supplied quality or phasor claims are never acceptance inputs.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

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
        raise SystemExit(f"pinned qualification Python is not executable: {_PINNED_PYTHON}")
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

if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from smateway.hexcal import attest_pluto_plus_utils_source, attest_source_files_at_commit
from smateway.hexcal import audit_continuity_metadata, validate_tx1_rf_readback_evidence
from smateway.capture_continuity import validate_continuity_ledger
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import attestation_sha256, attest_runtime
from smateway.ota_analysis import (
    ContinuityBlock,
    analyze_fast20_dwell_isolation,
    estimate_coherent_pilot_offset,
)
from smateway.profile import load_profile
from smateway.reference_transfer import analyze_fast20_reference_transfer
from smateway.schedule_alignment import AlignmentSearchMode
from smateway.selector_flash_attestation import (
    SelectorFlashError,
    validate_sealed_selector_evidence,
)
from smateway.selected_state_qualification import (
    ALL_OFF,
    DEVICE_IDENTITY_KIND as _DEVICE_IDENTITY_KIND,
    EXPECTED_STATES,
    FULL_CONDUCTED_STAGE,
    MATRIX_KIND,
    STATIC_KIND,
    STATIC_RESULT_KIND,
    TIMING_KIND,
    TIMING_RESULT_KIND,
    FullSimultaneousFixture,
    SelectedStateQualificationError,
    SelectorEvidenceBinding,
    canonical_sha256,
    full_simultaneous_fixture_binding_from_manifest,
    qualify_fast20_matrix,
    qualify_fast20_timing,
    qualify_selected_state_release,
    qualify_static_bench,
    device_identity_snapshot_from_evidence,
    selector_binding_from_sealed,
    sha256_path,
    validate_full_simultaneous_fixture,
    validate_device_identity_evidence,
    validate_intervention_contract,
)

from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.hardware import SafeDdsTonePlan, SampleBlockV2, capture_continuous_safe_dds_tone
from pluto_plus.models import ArtifactSummary, GainMode, RadioSettings

from scripts import run_5g8_leakage_ladder as leakage_runner
from scripts import run_5g8_one_hot_path_ladder as one_hot_runner

Mode = Literal["static-bench", "fast20-timing", "fast20-matrix"]
Action = Literal["prepare", "capture", "analyze"]

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/smateway"
PLAN_FILENAME = "plan.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
ANALYSIS_TOMBSTONE_FILENAME = "analysis-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
RESULT_FILENAME = "qualification-result.json"
FIXTURE_BINDING_FILENAME = "full-simultaneous-fixture-binding.json"
CAPTURE_EVIDENCE_FILENAME = "capture-evidence.json"
CONDITION_RECORD_FILENAME = "selected-state-condition-record.json"
FAILURE_SAFETY_EVIDENCE_KIND = "5g8_selected_state_failure_safety_cleanup_v1"
FAST20_FAILURE_CLEANUP_KIND = "fast20_direct_gpio_all_off_cleanup_v1"

PLAN_KIND = "5g8_selected_state_qualification_plan_v1"
RUN_KIND = "5g8_selected_state_qualification_run_v1"
DEVICE_IDENTITY_KIND = _DEVICE_IDENTITY_KIND
CAPTURE_RECORD_KIND = "5g8_selected_state_capture_record_v1"
MAXIMUM_DEVICE_IDENTITY_AGE_SECONDS = 300
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")

CENTER_FREQUENCY_HZ = 5_800_000_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
RECEIVER_GAIN_DB = 60
TX_HARDWARE_GAIN_DB = -20.0
DDS_SCALE = 0.25
KERNEL_BUFFERS = 8
SAMPLES_PER_FRAME = 100_000
STATIC_FRAME_COUNT = 3
FAST20_FRAME_COUNT = 100
STATIC_SAMPLE_COUNT = SAMPLES_PER_FRAME * STATIC_FRAME_COUNT
FAST20_SAMPLE_COUNT = SAMPLES_PER_FRAME * FAST20_FRAME_COUNT
MINIMUM_REFERENCE_SNR_DB = 20.0
MINIMUM_PILOT_CONFIDENCE = 0.90
MINIMUM_PILOT_STEP_COHERENCE = 0.995
MAXIMUM_PILOT_PHASE_RMS_DEG = 6.0
MINIMUM_FAST20_CYCLES = 20

SOURCE_FILES = (
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/intervention_support.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/ota_analysis.py",
    "src/smateway/profile.py",
    "src/smateway/reference_transfer.py",
    "src/smateway/schedule_alignment.py",
    "src/smateway/selected_state_qualification.py",
    "src/smateway/selector_flash_attestation.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/hexcal.py",
    "scripts/run_5g8_leakage_ladder.py",
    "scripts/run_5g8_one_hot_path_ladder.py",
    "scripts/prepare_5g8_selected_state_inputs.py",
    "scripts/run_5g8_selected_state_qualification.py",
)


class SelectedStateRunError(RuntimeError):
    """A plan or evidence set cannot safely enter qualification."""


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
    def __call__(self, serial: str, requested_uri: str) -> dict[str, Any]: ...


class StaticSelectorBoundary(Protocol):
    def __call__(
        self,
        selector_control: Mapping[str, Any],
        state_name: str,
        state_code: int,
        purpose: str,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise SelectedStateRunError(f"{label} is not a safe identifier")
    return value


def _validate_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SelectedStateRunError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _assert_no_symlink_chain(path: Path, label: str, *, allow_missing: bool = False) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise SelectedStateRunError(f"{label} does not exist: {current}") from None
        if current.is_symlink():
            raise SelectedStateRunError(f"{label} path contains a symlink: {current}")


def _nearest_existing(path: Path) -> Path:
    current = path.expanduser().absolute()
    while not current.exists():
        if current == current.parent:
            raise SelectedStateRunError("cannot resolve an existing storage parent")
        current = current.parent
    return current


def _filesystem_device(path: Path) -> int:
    """Return one filesystem device identifier behind a narrow test seam."""

    return int(os.stat(path).st_dev)


def _assert_local_rpi_storage(path: Path) -> None:
    """Require the same filesystem device as /home/pi, not only a path label."""

    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "local storage path", allow_missing=True)
    forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or root in exact.parents for root in forbidden):
        raise SelectedStateRunError(
            "state root must be local RPi storage, never removable or Pluto storage"
        )
    try:
        home_device = _filesystem_device(Path("/home/pi"))
        storage_device = _filesystem_device(_nearest_existing(exact))
    except OSError as error:
        raise SelectedStateRunError(f"cannot attest local storage device: {error}") from error
    if storage_device != home_device:
        raise SelectedStateRunError("state root is not on the Raspberry Pi local filesystem")


def _safe_local_state_root(path: Path) -> Path:
    exact = path.expanduser().absolute()
    _assert_local_rpi_storage(exact)
    return exact


def _read_json(path: Path, label: str, *, canonical: bool = False) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise SelectedStateRunError(f"{label} must be a regular non-symlink file")
    try:
        raw = exact.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectedStateRunError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise SelectedStateRunError(f"{label} must contain one JSON object")
    if canonical and raw != _canonical_bytes(value):
        raise SelectedStateRunError(f"{label} is not canonical JSON")
    return value


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _file_evidence(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise SelectedStateRunError(f"{label} must be a regular non-symlink file")
    return {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": exact.stat().st_size,
    }


def _validate_file_evidence(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size_bytes"}:
        raise SelectedStateRunError(f"{label} file identity is incomplete")
    path_value = value.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateRunError(f"{label} path must be absolute")
    current = _file_evidence(Path(path_value), label)
    if dict(value) != current:
        raise SelectedStateRunError(f"{label} bytes differ from immutable plan")
    return current


def _openocd_tcl_path(path: Path) -> str:
    """Brace one path for OpenOCD Tcl, rejecting characters unsafe in braces."""

    value = str(path.expanduser().absolute())
    if any(character in value for character in ("{", "}", "\r", "\n")):
        raise SelectedStateRunError("OpenOCD evidence path contains unsafe Tcl characters")
    return "{" + value + "}"


def _ensure_local_directory(path: Path, label: str, *, create_only: bool = False) -> Path:
    """Create/reopen one local directory without traversing symlinks or mounts."""

    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label, allow_missing=True)
    _assert_local_rpi_storage(exact)
    if create_only and (exact.exists() or exact.is_symlink()):
        raise SelectedStateRunError(f"{label} already exists")
    exact.mkdir(parents=True, exist_ok=not create_only, mode=0o700)
    _assert_no_symlink_chain(exact, label)
    if exact.is_symlink() or not exact.is_dir():
        raise SelectedStateRunError(f"{label} is not one regular directory")
    _assert_local_rpi_storage(exact)
    return exact


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    exact = path.expanduser().absolute()
    _ensure_local_directory(exact.parent, "immutable output parent")
    descriptor = os.open(exact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_canonical_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o400)
        _fsync_directory(exact.parent)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        raise


def _repository_source_attestation(repository: Path = _REPOSITORY) -> dict[str, Any]:
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise SelectedStateRunError("cannot resolve Smateway source revision") from error
    if GIT_COMMIT.fullmatch(head) is None:
        raise SelectedStateRunError("Smateway HEAD is not a full Git commit")
    try:
        source = attest_source_files_at_commit(
            repository,
            expected_commit=head,
            relative_paths=SOURCE_FILES,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SelectedStateRunError(str(error)) from error
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": head,
        "clean_source_files_verified": True,
        "files": source["files"],
        "source_files_sha256": canonical_sha256(source["files"]),
    }


def _local_runtime_bindings() -> dict[str, Any]:
    """Attest source/dependency/native process identity without opening hardware."""

    try:
        dependency = attest_pluto_plus_utils_source()
        native = attest_runtime()
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SelectedStateRunError(str(error)) from error
    source = _repository_source_attestation()
    return {
        "source": source,
        "dependency": dependency,
        "native": native,
        "source_commit": _validate_identifier(source["commit"], "source commit"),
        "dependency_commit": _validate_identifier(dependency.get("commit"), "dependency commit"),
        "native_attestation_sha256": attestation_sha256(native),
    }


def _device_identity(
    path: Path,
    *,
    serial: str,
    uri: str,
    reference_time: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _read_json(path, "device identity")
    try:
        evidence = validate_device_identity_evidence(document)
    except SelectedStateQualificationError as error:
        raise SelectedStateRunError(str(error)) from error
    if evidence.serial != serial or evidence.usb_uri != uri:
        raise SelectedStateRunError("device identity does not admit the requested Pluto/USB URI")
    if reference_time is not None:
        try:
            reference = datetime.fromisoformat(reference_time)
        except ValueError as error:
            raise SelectedStateRunError("plan time is not ISO-8601") from error
        age_seconds = (reference - evidence.observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > MAXIMUM_DEVICE_IDENTITY_AGE_SECONDS:
            raise SelectedStateRunError("device identity observation is stale or future-dated")
        sysfs = document.get("sysfs_attributes")
        if not isinstance(sysfs, Mapping):
            raise SelectedStateRunError("device identity lacks USB sysfs attributes")
        sysfs_path = Path(str(sysfs.get("path", ""))).expanduser().absolute()
        if not sysfs_path.is_dir() or sysfs_path.is_symlink():
            raise SelectedStateRunError("observed Pluto sysfs path is no longer current")
        for name in ("serial", "idVendor", "idProduct", "manufacturer", "product"):
            try:
                current = (sysfs_path / name).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as error:
                raise SelectedStateRunError(
                    f"cannot re-read current Pluto sysfs {name}: {error}"
                ) from error
            if current != sysfs.get(name):
                raise SelectedStateRunError(
                    f"current Pluto sysfs {name} differs from the device observation"
                )
    return document, _file_evidence(path, "device identity")


def _mode_role(mode: Mode) -> Literal["bench", "fast20"]:
    return "bench" if mode == "static-bench" else "fast20"


def _bound_prior_files(
    mode: Mode,
    *,
    intervention_contract: Path | None,
    static_result: Path | None,
    timing_result: Path | None,
) -> dict[str, Any] | None:
    supplied = (intervention_contract, static_result, timing_result)
    if mode != "fast20-matrix":
        if any(value is not None for value in supplied):
            raise SelectedStateRunError("prior result bindings apply only to fast20-matrix")
        return None
    if any(value is None for value in supplied):
        raise SelectedStateRunError(
            "fast20-matrix requires intervention, static-result, and timing-result files"
        )
    assert intervention_contract is not None
    assert static_result is not None
    assert timing_result is not None
    return {
        "intervention_contract": _file_evidence(intervention_contract, "intervention contract"),
        "static_result": _file_evidence(static_result, "static qualification result"),
        "timing_result": _file_evidence(timing_result, "timing qualification result"),
    }


def _selector_flash_binding(selector: SelectorEvidenceBinding) -> dict[str, Any]:
    return {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": selector.path,
        "sha256": selector.sha256,
        "campaign_id": selector.campaign_id,
        "run_id": selector.run_id,
        "board_id": selector.board_id,
        "image_role": selector.image_role,
    }


def _fixture_evidence_from_inputs(
    manifest_path: Path,
    setup_path: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
    selector: SelectorEvidenceBinding,
) -> dict[str, Any]:
    """Normalize the full-conducted fixture and its run-specific setup proof.

    This intentionally reuses the authoritative A/B/C/E graph and setup
    normalizers.  T8 differs only in allowing the exact sealed image role to be
    ``bench`` or ``fast20`` during post-fix re-entry.
    """

    exact_manifest = manifest_path.expanduser().absolute()
    exact_setup = setup_path.expanduser().absolute()
    _assert_no_symlink_chain(exact_manifest, "fixture manifest")
    _assert_no_symlink_chain(exact_setup, "setup attestation")
    raw = _read_json(exact_manifest, "fixture manifest v2")
    if (
        raw.get("schema") != 2
        or raw.get("fixture_kind") != leakage_runner.FIXTURE_KIND_V2
        or raw.get("stage") != FULL_CONDUCTED_STAGE
        or raw.get("board_id") != board_id
    ):
        raise SelectedStateRunError(
            "T8 requires fixture-v2 full_conducted_fixture for the exact board"
        )
    campaign_id = _validate_identifier(raw.get("campaign_id"), "fixture campaign ID")
    if campaign_id != selector.campaign_id:
        raise SelectedStateRunError("fixture and sealed selector campaign IDs differ")
    group_id = _validate_identifier(
        raw.get("comparable_fixture_group_id"), "fixture comparison group ID"
    )
    try:
        shared = leakage_runner._normalize_shared_fixture(
            raw.get("shared_fixture"),
            expected_serial=serial,
            base_directory=exact_manifest.parent,
            verify_files=True,
        )
        shared_sha = canonical_sha256(shared)
        delta = leakage_runner._normalize_stage_delta(
            raw.get("stage_delta"),
            stage=FULL_CONDUCTED_STAGE,
            shared=shared,
            base_directory=exact_manifest.parent,
            verify_files=True,
        )
        delta_sha = canonical_sha256(delta)
        prior = leakage_runner._prior_stage_binding_from_plan(
            raw.get("prior_stage_binding"),
            stage=FULL_CONDUCTED_STAGE,
            campaign_id=campaign_id,
            comparable_fixture_group_id=group_id,
            shared_fixture_sha256=shared_sha,
            current_stage_delta=delta,
            board_id=board_id,
            serial=serial,
            base_directory=exact_manifest.parent,
        )
        component_ids, connection_ids = leakage_runner._fixture_identity_sets(shared, delta)
        setup = leakage_runner._normalize_setup_attestation(
            exact_setup,
            run_id=run_id,
            campaign_id=campaign_id,
            comparable_fixture_group_id=group_id,
            stage=FULL_CONDUCTED_STAGE,
            fixture_manifest_sha256=sha256_path(exact_manifest),
            shared_fixture_sha256=shared_sha,
            stage_delta_sha256=delta_sha,
            component_ids=component_ids,
            connection_ids=connection_ids,
            selector_flash_evidence=_selector_flash_binding(selector),
        )
    except (OSError, ValueError, leakage_runner.LeakageLadderError) as error:
        raise SelectedStateRunError(str(error)) from error
    return {
        "schema": 2,
        "fixture_kind": leakage_runner.FIXTURE_KIND_V2,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": group_id,
        "stage": FULL_CONDUCTED_STAGE,
        "run_id": run_id,
        "board_id": board_id,
        "source_files": {
            "fixture_manifest": _file_evidence(exact_manifest, "fixture manifest"),
            "setup_attestation": _file_evidence(exact_setup, "setup attestation"),
        },
        "shared_fixture": shared,
        "shared_fixture_sha256": shared_sha,
        "stage_delta": delta,
        "stage_delta_sha256": delta_sha,
        "prior_stage_binding": prior,
        "setup_attestation": setup,
        "selector_flash_evidence": _selector_flash_binding(selector),
        "component_ids": component_ids,
        "connection_ids": connection_ids,
    }


def _selector_control_from_files(
    *,
    selector: SelectorEvidenceBinding,
    bench_manifest_path: Path | None,
    openocd_config_path: Path,
    profile_path: Path,
    source_commit: str,
) -> dict[str, Any] | None:
    profile_file = _file_evidence(profile_path, "selector profile")
    profile = load_profile(profile_path.expanduser().absolute())
    if profile.profile_id != "fast20-v1" or profile.contract_sha256 != (
        selector.profile_contract_sha256
    ):
        raise SelectedStateRunError("selector profile differs from sealed live-image evidence")
    if selector.image_role == "fast20":
        try:
            sealed = validate_sealed_selector_evidence(
                Path(selector.path),
                expected_sha256=selector.sha256,
                expected_campaign_id=selector.campaign_id,
                expected_run_id=selector.run_id,
                expected_board_id=selector.board_id,
                expected_image_role="fast20",
            )
        except SelectorFlashError as error:
            raise SelectedStateRunError(str(error)) from error
        frozen = sealed.get("frozen_inputs")
        files = frozen.get("files") if isinstance(frozen, Mapping) else None
        if not isinstance(files, Mapping):
            raise SelectedStateRunError("sealed Fast20 evidence lacks frozen files")
        firmware = files.get("firmware_bin")
        config = files.get("openocd_config")
        frozen_profile = files.get("profile")
        if not all(isinstance(item, Mapping) for item in (firmware, config, frozen_profile)):
            raise SelectedStateRunError("sealed Fast20 evidence lacks BIN/OpenOCD/profile files")
        assert isinstance(firmware, Mapping)
        assert isinstance(config, Mapping)
        assert isinstance(frozen_profile, Mapping)
        firmware_file = _validate_file_evidence(firmware, "sealed Fast20 BIN")
        config_file = _validate_file_evidence(config, "sealed OpenOCD config")
        requested_config_file = _file_evidence(
            openocd_config_path, "requested Fast20 OpenOCD config"
        )
        if requested_config_file != config_file:
            raise SelectedStateRunError(
                "requested Fast20 OpenOCD path/hash/size differs from sealed selector inputs"
            )
        frozen_profile_file = _validate_file_evidence(
            frozen_profile, "sealed Fast20 control profile"
        )
        if profile_file != frozen_profile_file:
            raise SelectedStateRunError(
                "requested Fast20 profile path/hash/size differs from sealed selector inputs"
            )
        return {
            "schema": 1,
            "control_kind": "sealed_fast20_autonomous_schedule",
            "profile": profile_file,
            "firmware_bin": firmware_file,
            "openocd_config": config_file,
            "profile_contract_sha256": profile.contract_sha256,
            "state_order": list(EXPECTED_STATES),
            "all_off_code": profile.all_off_code,
            "state_codes": {
                "ALL_OFF": profile.all_off_code,
                **{state.name: state.gpio_code for state in profile.states},
            },
        }
    if bench_manifest_path is None:
        raise SelectedStateRunError("static-bench requires the sealed bench build manifest")
    try:
        return one_hot_runner._one_hot_selector_control_contract(
            bench_manifest_path=bench_manifest_path,
            openocd_config_path=openocd_config_path,
            profile_path=profile_path,
            source_commit=source_commit,
            selector_flash_evidence=_selector_flash_binding(selector),
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SelectedStateRunError(str(error)) from error


def build_plan(
    *,
    mode: Mode,
    run_id: str,
    campaign_id: str,
    board_id: str,
    serial: str,
    uri: str,
    selector_evidence_path: Path,
    selector_evidence_sha256: str,
    selector_run_id: str,
    device_identity_path: Path,
    state_root: Path,
    fixture_manifest_path: Path,
    setup_attestation_path: Path,
    profile_path: Path,
    openocd_config_path: Path,
    bench_manifest_path: Path | None = None,
    intervention_contract_path: Path | None = None,
    static_result_path: Path | None = None,
    timing_result_path: Path | None = None,
    require_one_degree: bool = False,
    runtime_bindings: Mapping[str, Any] | None = None,
    selector_loader: Callable[..., SelectorEvidenceBinding] = selector_binding_from_sealed,
    fixture_evidence_loader: Callable[..., dict[str, Any]] = _fixture_evidence_from_inputs,
    selector_control_builder: Callable[..., dict[str, Any] | None] = (_selector_control_from_files),
    now: Callable[[], str] = _now,
) -> Path:
    """Freeze one create-only qualification plan and return its absolute path."""

    run = _validate_identifier(run_id, "run ID")
    campaign = _validate_identifier(campaign_id, "campaign ID")
    board = _validate_identifier(board_id, "board ID")
    pluto_serial = _validate_identifier(serial, "Pluto serial")
    prepared_at = now()
    if USB_URI.fullmatch(uri) is None:
        raise SelectedStateRunError("plan requires an explicit current usb: URI")
    role = _mode_role(mode)
    root = _safe_local_state_root(state_root)
    try:
        fixture_document = full_simultaneous_fixture_binding_from_manifest(fixture_manifest_path)
    except SelectedStateQualificationError as error:
        raise SelectedStateRunError(str(error)) from error
    fixture_file: dict[str, Any] | None = None
    try:
        fixture = validate_full_simultaneous_fixture(fixture_document)
    except SelectedStateQualificationError as error:
        raise SelectedStateRunError(str(error)) from error
    if fixture.board_id != board or fixture.pluto_serial != pluto_serial:
        raise SelectedStateRunError("fixture board or Pluto serial differs from the plan")
    selector = selector_loader(
        selector_evidence_path,
        expected_sha256=_validate_sha256(selector_evidence_sha256, "selector evidence SHA-256"),
        campaign_id=campaign,
        run_id=selector_run_id,
        board_id=board,
        image_role=role,
    )
    if (
        selector.image_role != role
        or selector.campaign_id != campaign
        or selector.run_id != selector_run_id
        or selector.board_id != board
        or selector.sha256 != selector_evidence_sha256
        or Path(selector.path) != selector_evidence_path.expanduser().absolute()
    ):
        raise SelectedStateRunError(
            "selector loader returned evidence outside the exact requested identity"
        )
    device, device_file = _device_identity(
        device_identity_path,
        serial=pluto_serial,
        uri=uri,
        reference_time=prepared_at,
    )
    bindings = dict(runtime_bindings if runtime_bindings is not None else _local_runtime_bindings())
    required_runtime = {
        "source",
        "dependency",
        "native",
        "source_commit",
        "dependency_commit",
        "native_attestation_sha256",
    }
    if set(bindings) != required_runtime:
        raise SelectedStateRunError("runtime bindings are incomplete or unexpected")
    source_commit = bindings.get("source_commit")
    dependency_commit = bindings.get("dependency_commit")
    if not isinstance(source_commit, str) or GIT_COMMIT.fullmatch(source_commit) is None:
        raise SelectedStateRunError("runtime binding source commit is invalid")
    if not isinstance(dependency_commit, str) or GIT_COMMIT.fullmatch(dependency_commit) is None:
        raise SelectedStateRunError("runtime binding dependency commit is invalid")
    native_sha = _validate_sha256(
        bindings.get("native_attestation_sha256"), "native attestation SHA-256"
    )
    if device.get("native_libiio_runtime_attestation_sha256") != native_sha:
        raise SelectedStateRunError(
            "device observation and plan runtime do not share exact native libiio identity"
        )
    try:
        fixture_evidence = fixture_evidence_loader(
            fixture_manifest_path,
            setup_attestation_path,
            run_id=run,
            board_id=board,
            serial=pluto_serial,
            selector=selector,
        )
        selector_control = selector_control_builder(
            selector=selector,
            bench_manifest_path=bench_manifest_path,
            openocd_config_path=openocd_config_path,
            profile_path=profile_path,
            source_commit=source_commit,
        )
    except (OSError, ValueError, SelectedStateRunError) as error:
        if isinstance(error, SelectedStateRunError):
            raise
        raise SelectedStateRunError(str(error)) from error
    prior = _bound_prior_files(
        mode,
        intervention_contract=intervention_contract_path,
        static_result=static_result_path,
        timing_result=timing_result_path,
    )
    if prior is not None:
        intervention_file = prior["intervention_contract"]
        assert isinstance(intervention_file, Mapping)
        try:
            intervention = validate_intervention_contract(
                _read_json(Path(str(intervention_file["path"])), "intervention contract"),
                fixture=fixture,
            )
        except SelectedStateQualificationError as error:
            raise SelectedStateRunError(str(error)) from error
        if (
            intervention.campaign_id != campaign
            or intervention.board_id != board
            or intervention.source_commit != source_commit
            or intervention.dependency_commit != dependency_commit
        ):
            raise SelectedStateRunError(
                "intervention evidence differs from matrix campaign/source identity"
            )
    if require_one_degree and mode != "fast20-matrix":
        raise SelectedStateRunError("one-degree release applies only to fast20-matrix")
    run_directory = root / "boards" / board / "5g8-selected-state" / campaign / mode / run
    if run_directory.exists() or run_directory.is_symlink():
        raise SelectedStateRunError("run ID already exists and is burned")
    derived_path = run_directory / FIXTURE_BINDING_FILENAME
    try:
        _write_new_json(derived_path, fixture_document)
    except OSError as error:
        raise SelectedStateRunError(f"cannot freeze derived fixture binding: {error}") from error
    fixture_file = _file_evidence(derived_path, "derived fixture binding")
    assert fixture_file is not None
    capture_root = run_directory / "captures"
    contract = {
        "schema": 1,
        "plan_kind": PLAN_KIND,
        "run_kind": RUN_KIND,
        "mode": mode,
        "run_id": run,
        "campaign_id": campaign,
        "board_id": board,
        "serial": pluto_serial,
        "uri": uri,
        "image_role": role,
        "fixture": asdict(fixture),
        "fixture_binding_file": fixture_file,
        "fixture_evidence": fixture_evidence,
        "fixture_evidence_sha256": canonical_sha256(fixture_evidence),
        "selector": asdict(selector),
        "selector_control": selector_control,
        "device_identity": device,
        "device_identity_file": device_file,
        "source_attestation": bindings["source"],
        "dependency_attestation": bindings["dependency"],
        "native_attestation": bindings["native"],
        "source_commit": source_commit,
        "dependency_commit": dependency_commit,
        "native_attestation_sha256": native_sha,
        "device_identity_sha256": canonical_sha256(device),
        "prior_qualification_files": prior,
        "require_one_degree": require_one_degree,
        "state_root": str(root),
        "run_directory": str(run_directory),
        "capture_root": str(capture_root),
        "local_rpi_storage_only": True,
        "hardware_access_policy": {
            "prepare": False,
            "analyze": False,
            "capture": True,
        },
        "capture_settings": {
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "tx_channel": 0,
            "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
            "dds_scale": DDS_SCALE,
            "kernel_buffers": KERNEL_BUFFERS,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": (STATIC_FRAME_COUNT if mode == "static-bench" else FAST20_FRAME_COUNT),
            "sample_count": (
                STATIC_SAMPLE_COUNT if mode == "static-bench" else FAST20_SAMPLE_COUNT
            ),
        },
        "evidence_requirements": {
            "static-bench": ("exact ALL_OFF+ANT1..ANT8 bench captures with mailbox/GPIO readback"),
            "fast20-timing": (
                "two source-distinct ABI2 streams with exact counts/boundaries/guards/dwells"
            ),
            "fast20-matrix": (
                "five fresh full-state streams plus intervention/static/timing prerequisites"
            ),
        }[mode],
    }
    envelope = {
        "schema": 1,
        "immutable": True,
        "created_at": prepared_at,
        "plan_contract": contract,
        "plan_contract_sha256": canonical_sha256(contract),
    }
    plan_path = run_directory / PLAN_FILENAME
    try:
        _write_new_json(plan_path, envelope)
    except OSError as error:
        raise SelectedStateRunError(f"cannot create immutable plan: {error}") from error
    return plan_path


def _load_plan(path: Path) -> tuple[dict[str, Any], str]:
    envelope = _read_json(path, "immutable qualification plan", canonical=True)
    if set(envelope) != {
        "schema",
        "immutable",
        "created_at",
        "plan_contract",
        "plan_contract_sha256",
    }:
        raise SelectedStateRunError("qualification plan envelope is malformed")
    contract = envelope.get("plan_contract")
    if not isinstance(contract, dict):
        raise SelectedStateRunError("qualification plan contract is not an object")
    digest = _validate_sha256(envelope.get("plan_contract_sha256"), "plan contract SHA-256")
    if canonical_sha256(contract) != digest:
        raise SelectedStateRunError("qualification plan contract hash is inconsistent")
    if (
        envelope.get("schema") != 1
        or envelope.get("immutable") is not True
        or contract.get("schema") != 1
        or contract.get("plan_kind") != PLAN_KIND
        or contract.get("run_kind") != RUN_KIND
        or contract.get("hardware_access_policy")
        != {"prepare": False, "analyze": False, "capture": True}
        or contract.get("local_rpi_storage_only") is not True
    ):
        raise SelectedStateRunError("qualification plan acceptance contract is invalid")
    run_directory = contract.get("run_directory")
    if not isinstance(run_directory, str) or Path(run_directory) != path.parent.absolute():
        raise SelectedStateRunError("qualification plan path differs from its run directory")
    _safe_local_state_root(Path(str(contract.get("state_root", ""))))
    capture_root = Path(str(contract.get("capture_root", ""))).absolute()
    if capture_root != path.parent.absolute() / "captures":
        raise SelectedStateRunError("capture root differs from the immutable local run root")
    _assert_local_rpi_storage(capture_root)
    return contract, digest


def _revalidate_plan_inputs(
    contract: Mapping[str, Any],
    *,
    runtime_bindings: Mapping[str, Any] | None,
    selector_loader: Callable[..., SelectorEvidenceBinding],
    fixture_evidence_loader: Callable[..., dict[str, Any]] = _fixture_evidence_from_inputs,
    selector_control_builder: Callable[..., dict[str, Any] | None] = (_selector_control_from_files),
    device_identity_reference_time: str | None = None,
) -> tuple[FullSimultaneousFixture, SelectorEvidenceBinding]:
    fixture_file = _validate_file_evidence(contract.get("fixture_binding_file"), "fixture binding")
    fixture_document = _read_json(Path(fixture_file["path"]), "fixture binding")
    try:
        fixture = validate_full_simultaneous_fixture(fixture_document)
    except SelectedStateQualificationError as error:
        raise SelectedStateRunError(str(error)) from error
    if canonical_sha256(asdict(fixture)) != canonical_sha256(contract.get("fixture")):
        raise SelectedStateRunError("current fixture binding differs from immutable plan")
    selector_snapshot = contract.get("selector")
    if not isinstance(selector_snapshot, Mapping):
        raise SelectedStateRunError("selector plan binding is missing")
    role = contract.get("image_role")
    if role not in {"bench", "fast20"}:
        raise SelectedStateRunError("selector role in plan is invalid")
    selector = selector_loader(
        Path(str(selector_snapshot.get("path"))),
        expected_sha256=str(selector_snapshot.get("sha256")),
        campaign_id=str(selector_snapshot.get("campaign_id")),
        run_id=str(selector_snapshot.get("run_id")),
        board_id=str(selector_snapshot.get("board_id")),
        image_role=role,
    )
    if asdict(selector) != dict(selector_snapshot):
        raise SelectedStateRunError("current selector evidence differs from immutable plan")
    device_file = _validate_file_evidence(contract.get("device_identity_file"), "device identity")
    device, _ = _device_identity(
        Path(device_file["path"]),
        serial=str(contract.get("serial")),
        uri=str(contract.get("uri")),
        reference_time=device_identity_reference_time,
    )
    if device != contract.get("device_identity"):
        raise SelectedStateRunError("current device identity differs from immutable plan")
    current = dict(runtime_bindings if runtime_bindings is not None else _local_runtime_bindings())
    for field, current_key in (
        ("source_attestation", "source"),
        ("dependency_attestation", "dependency"),
        ("native_attestation", "native"),
        ("source_commit", "source_commit"),
        ("dependency_commit", "dependency_commit"),
        ("native_attestation_sha256", "native_attestation_sha256"),
    ):
        if contract.get(field) != current.get(current_key):
            raise SelectedStateRunError(f"current {field} differs from immutable plan")
    frozen_fixture_evidence = contract.get("fixture_evidence")
    if not isinstance(frozen_fixture_evidence, Mapping):
        raise SelectedStateRunError("plan lacks fixture-v2/run-specific setup evidence")
    source_files = frozen_fixture_evidence.get("source_files")
    if not isinstance(source_files, Mapping):
        raise SelectedStateRunError("fixture evidence lacks source files")
    manifest_file = _validate_file_evidence(
        source_files.get("fixture_manifest"), "fixture manifest"
    )
    setup_file = _validate_file_evidence(source_files.get("setup_attestation"), "setup attestation")
    current_fixture_evidence = fixture_evidence_loader(
        Path(manifest_file["path"]),
        Path(setup_file["path"]),
        run_id=str(contract.get("run_id")),
        board_id=str(contract.get("board_id")),
        serial=str(contract.get("serial")),
        selector=selector,
    )
    if current_fixture_evidence != dict(frozen_fixture_evidence) or canonical_sha256(
        current_fixture_evidence
    ) != contract.get("fixture_evidence_sha256"):
        raise SelectedStateRunError("current fixture/setup evidence differs from immutable plan")
    frozen_control = contract.get("selector_control")
    if not isinstance(frozen_control, Mapping):
        raise SelectedStateRunError("plan lacks exact selector control/profile evidence")
    if selector.image_role == "bench":
        bench = frozen_control.get("bench_manifest")
        config = frozen_control.get("openocd_config")
        profile = frozen_control.get("control_profile")
        if not all(isinstance(item, Mapping) for item in (bench, config, profile)):
            raise SelectedStateRunError("bench selector control files are incomplete")
        assert isinstance(bench, Mapping)
        assert isinstance(config, Mapping)
        assert isinstance(profile, Mapping)
        current_control = selector_control_builder(
            selector=selector,
            bench_manifest_path=Path(str(bench.get("path", ""))),
            openocd_config_path=Path(str(config.get("path", ""))),
            profile_path=Path(str(profile.get("path", ""))),
            source_commit=str(contract.get("source_commit")),
        )
    else:
        profile = frozen_control.get("profile")
        config = frozen_control.get("openocd_config")
        if not isinstance(profile, Mapping) or not isinstance(config, Mapping):
            raise SelectedStateRunError("Fast20 selector profile/config binding is incomplete")
        current_control = selector_control_builder(
            selector=selector,
            bench_manifest_path=None,
            openocd_config_path=Path(str(config.get("path", ""))),
            profile_path=Path(str(profile.get("path", ""))),
            source_commit=str(contract.get("source_commit")),
        )
    if current_control != dict(frozen_control):
        raise SelectedStateRunError("current selector control/profile differs from immutable plan")
    return fixture, selector


def _qualification_context(contract: Mapping[str, Any], plan_sha256: str) -> dict[str, Any]:
    selector = contract.get("selector")
    fixture = contract.get("fixture")
    if not isinstance(selector, Mapping) or not isinstance(fixture, Mapping):
        raise SelectedStateRunError("plan lacks fixture/selector identity")
    return {
        "campaign_id": contract.get("campaign_id"),
        "board_id": contract.get("board_id"),
        "fixture_revision_sha256": fixture.get("fixture_revision_sha256"),
        "selector_evidence_sha256": selector.get("sha256"),
        "selector_image_role": contract.get("image_role"),
        "source_commit": contract.get("source_commit"),
        "dependency_commit": contract.get("dependency_commit"),
        "native_attestation_sha256": contract.get("native_attestation_sha256"),
        "device_identity_sha256": contract.get("device_identity_sha256"),
        "device_identity_snapshot": device_identity_snapshot_from_evidence(
            contract["device_identity"]
        ),
        "plan_sha256": plan_sha256,
    }


def _reanalyze_prior_result(
    file_identity: Mapping[str, Any],
    *,
    expected_mode: Literal["static-bench", "fast20-timing"],
    expected_kind: str,
    label: str,
    current_contract: Mapping[str, Any],
    current_fixture: FullSimultaneousFixture,
    runtime_bindings: Mapping[str, Any] | None,
    selector_loader: Callable[..., SelectorEvidenceBinding],
    fixture_evidence_loader: Callable[..., dict[str, Any]],
    selector_control_builder: Callable[..., dict[str, Any] | None],
) -> dict[str, Any]:
    """Recursively re-admit a prerequisite from its original retained bytes."""

    result_file = _validate_file_evidence(file_identity, label)
    result_path = Path(result_file["path"])
    stored = _read_json(result_path, label, canonical=True)
    qualification_input = _validate_file_evidence(
        stored.get("qualification_input"), f"{label} qualification input"
    )
    execution_file = _validate_file_evidence(
        stored.get("execution_tombstone"), f"{label} execution tombstone"
    )
    analysis_file = _validate_file_evidence(
        stored.get("analysis_tombstone"), f"{label} analysis tombstone"
    )
    execution_path = Path(execution_file["path"])
    analysis_path = Path(analysis_file["path"])
    execution = _read_json(execution_path, f"{label} execution tombstone", canonical=True)
    plan_value = execution.get("plan_path")
    if not isinstance(plan_value, str) or not Path(plan_value).is_absolute():
        raise SelectedStateRunError(f"{label} execution tombstone lacks an absolute plan")
    plan_path = Path(plan_value)
    prior_contract, prior_plan_sha = _load_plan(plan_path)
    if (
        prior_contract.get("mode") != expected_mode
        or result_path != plan_path.parent / RESULT_FILENAME
        or execution_path != plan_path.parent / EXECUTION_TOMBSTONE_FILENAME
        or analysis_path != plan_path.parent / ANALYSIS_TOMBSTONE_FILENAME
        or (plan_path.parent / FAILURE_TOMBSTONE_FILENAME).exists()
    ):
        raise SelectedStateRunError(f"{label} path/mode/failure state is invalid")
    input_path = Path(qualification_input["path"])
    if (
        execution.get("schema") != 1
        or execution.get("evidence_kind") != "5g8_selected_state_capture_started_v1"
        or execution.get("run_id") != prior_contract.get("run_id")
        or execution.get("mode") != expected_mode
        or execution.get("plan_path") != str(plan_path)
        or execution.get("plan_file_sha256") != sha256_path(plan_path)
        or execution.get("plan_contract_sha256") != prior_plan_sha
        or execution.get("expected_capture_set_path") != str(input_path)
        or execution.get("run_id_burned") is not True
        or execution.get("hardware_access_authorized_only_for_this_action") is not True
    ):
        raise SelectedStateRunError(f"{label} execution tombstone is inconsistent")
    analysis = _read_json(analysis_path, f"{label} analysis tombstone", canonical=True)
    if (
        analysis.get("schema") != 1
        or analysis.get("evidence_kind") != "5g8_selected_state_analysis_started_v1"
        or analysis.get("run_id") != prior_contract.get("run_id")
        or analysis.get("mode") != expected_mode
        or analysis.get("plan_path") != str(plan_path)
        or analysis.get("plan_file_sha256") != sha256_path(plan_path)
        or analysis.get("plan_contract_sha256") != prior_plan_sha
        or analysis.get("qualification_input") != qualification_input
        or analysis.get("capture_execution_tombstone_sha256") != execution_file["sha256"]
        or analysis.get("hardware_access_permitted") is not False
        or analysis.get("artifact_acceptance_pending") is not True
    ):
        raise SelectedStateRunError(f"{label} analysis tombstone is inconsistent")
    prior_fixture, prior_selector = _revalidate_plan_inputs(
        prior_contract,
        runtime_bindings=runtime_bindings,
        selector_loader=selector_loader,
        fixture_evidence_loader=fixture_evidence_loader,
        selector_control_builder=selector_control_builder,
    )
    if asdict(prior_fixture) != asdict(current_fixture):
        raise SelectedStateRunError(f"{label} fixture differs from matrix fixture")
    for field in (
        "campaign_id",
        "board_id",
        "serial",
        "source_commit",
        "dependency_commit",
        "native_attestation_sha256",
    ):
        if prior_contract.get(field) != current_contract.get(field):
            raise SelectedStateRunError(f"{label} {field} differs from matrix plan")
    prior_identity = device_identity_snapshot_from_evidence(prior_contract["device_identity"])
    current_identity = device_identity_snapshot_from_evidence(current_contract["device_identity"])
    prior_stable_identity = {
        key: value for key, value in prior_identity.items() if key != "usb_uri"
    }
    current_stable_identity = {
        key: value for key, value in current_identity.items() if key != "usb_uri"
    }
    if prior_stable_identity != current_stable_identity:
        raise SelectedStateRunError(f"{label} stable device identity differs from matrix plan")
    capture_set = _read_json(input_path, f"{label} raw capture set")
    scientific = _reanalyze_capture_evidence(
        capture_set,
        contract=prior_contract,
        plan_sha=prior_plan_sha,
    )
    if expected_mode == "static-bench":
        recomputed = qualify_static_bench(
            scientific,
            fixture=prior_fixture,
            selector=prior_selector,
        )
    else:
        recomputed = qualify_fast20_timing(
            scientific,
            fixture=prior_fixture,
            selector=prior_selector,
        )
    expected_output = {
        **recomputed,
        "qualification_input": qualification_input,
        "execution_tombstone": execution_file,
        "analysis_tombstone": analysis_file,
        "release": None,
        "result_accepted": True,
    }
    if (
        recomputed.get("schema") != 1
        or recomputed.get("result_kind") != expected_kind
        or recomputed.get("accepted") is not True
        or stored != _json_safe(expected_output)
    ):
        raise SelectedStateRunError(
            f"{label} does not equal independent reanalysis of retained evidence"
        )
    return recomputed


def _json_safe(value: object) -> Any:
    return leakage_runner._json_safe(value)


def _capture_shape(mode: Mode) -> tuple[int, int]:
    if mode == "static-bench":
        return STATIC_FRAME_COUNT, STATIC_SAMPLE_COUNT
    return FAST20_FRAME_COUNT, FAST20_SAMPLE_COUNT


def _expected_capture_count(mode: Mode) -> int:
    return {
        "static-bench": len(EXPECTED_STATES),
        "fast20-timing": 2,
        "fast20-matrix": 5,
    }[mode]


def _metadata_continuity_blocks(metadata: Mapping[str, Any]) -> tuple[ContinuityBlock, ...]:
    continuity = metadata.get("pluto:continuity")
    if not isinstance(continuity, Mapping):
        raise SelectedStateRunError("capture metadata lacks pluto:continuity")
    raw_blocks = continuity.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise SelectedStateRunError("capture metadata continuity ledger has no blocks")
    blocks: list[ContinuityBlock] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, Mapping):
            raise SelectedStateRunError(f"continuity block {index} is malformed")
        blocks.append(
            ContinuityBlock(
                sample_start=int(raw.get("sample_start", -1)),
                sample_count=int(raw.get("sample_count", -1)),
                utc_ns=int(raw.get("utc_ns", 0)),
            )
        )
    return tuple(blocks)


def _load_dual_rx_ci16(path: Path, *, sample_count: int) -> tuple[np.ndarray, np.ndarray]:
    expected_bytes = sample_count * 2 * 2 * np.dtype("<i2").itemsize
    if path.stat().st_size != expected_bytes:
        raise SelectedStateRunError("raw IQ byte extent is not exact dual-RX CI16")
    raw = np.memmap(path, dtype="<i2", mode="r")
    components = raw.reshape(sample_count, 2, 2)
    rx1 = components[:, 0, 0].astype(np.float32) + 1j * components[:, 0, 1].astype(np.float32)
    rx2 = components[:, 1, 0].astype(np.float32) + 1j * components[:, 1, 1].astype(np.float32)
    return rx1.astype(np.complex64), rx2.astype(np.complex64)


def _raw_headroom(rx1: np.ndarray, rx2: np.ndarray) -> tuple[int, float]:
    clipped = 0
    worst_peak = 0.0
    for samples in (rx1, rx2):
        for component in (samples.real, samples.imag):
            absolute = np.abs(component)
            clipped += int(np.count_nonzero(absolute >= 2_047.0))
            worst_peak = max(worst_peak, float(np.max(absolute, initial=0.0)))
    if worst_peak <= 0.0:
        return clipped, float("inf")
    return clipped, 20.0 * math.log10(2_047.0 / worst_peak)


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _leakage_transfer_document(analysis: Any) -> dict[str, Any]:
    transfer = analysis.rx2_over_rx1
    if transfer.phasor is None or analysis.rx2.tone_detected is not True:
        upper = transfer.amplitude_upper_bound_ratio
        if upper is None or not math.isfinite(float(upper)) or float(upper) <= 0.0:
            raise SelectedStateRunError("IQ nondetection lacks a phase-free magnitude bound")
        return {
            "detected": False,
            "h": None,
            "magnitude_upper_bound": float(upper),
            "coherence": None,
            "phase_rms_deg": None,
        }
    if transfer.block_phase_rms_deg is None:
        raise SelectedStateRunError("detected IQ transfer lacks phase residual evidence")
    return {
        "detected": True,
        "h": _complex_document(transfer.phasor),
        "magnitude_upper_bound": None,
        "coherence": float(transfer.block_phase_coherence),
        "phase_rms_deg": float(transfer.block_phase_rms_deg),
    }


def _cycle_summary_transfer_document(summary: Any) -> dict[str, Any]:
    values = np.asarray(tuple(summary.cycle_phasors), dtype=np.complex128)
    if values.size < 2:
        raise SelectedStateRunError("Fast20 state has fewer than two complete-cycle phasors")
    center = complex(summary.phasor)
    radial = np.abs(values - center)
    sigma = 1.4826 * float(np.median(radial))
    standard_error = max(sigma / math.sqrt(values.size), np.finfo(np.float64).tiny)
    detection_snr_db = 20.0 * math.log10(max(abs(center), np.finfo(float).tiny) / standard_error)
    detected = (
        detection_snr_db >= 6.0
        and float(summary.cycle_coherence) >= 0.80
        and math.isfinite(float(summary.cycle_phase_std_deg))
    )
    if not detected:
        return {
            "detected": False,
            "h": None,
            "magnitude_upper_bound": float(abs(center) + 1.96 * standard_error),
            "coherence": None,
            "phase_rms_deg": None,
        }
    return {
        "detected": True,
        "h": _complex_document(center),
        "magnitude_upper_bound": None,
        "coherence": float(summary.cycle_coherence),
        "phase_rms_deg": float(summary.cycle_phase_std_deg),
    }


def _capture_binding_from_record(
    binding: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    plan_sha: str,
    mode: Mode,
    capture_index: int,
) -> tuple[dict[str, Any], dict[str, Any], np.ndarray, np.ndarray]:
    expected_fields = {
        "run_id",
        "stream_id",
        "artifact_id",
        "raw_iq_path",
        "raw_iq_sha256",
        "raw_iq_size_bytes",
        "metadata_path",
        "metadata_sha256",
        "metadata_size_bytes",
        "condition_record_path",
        "condition_record_sha256",
        "condition_record_size_bytes",
        "leaf_source_sha256s",
        "plan_sha256",
        "fixture_revision_sha256",
        "selector_evidence_sha256",
        "source_commit",
        "dependency_commit",
        "native_attestation_sha256",
        "device_identity_sha256",
    }
    if set(binding) != expected_fields:
        raise SelectedStateRunError("capture binding fields are incomplete or unexpected")
    for name, path_key, sha_key, size_key in (
        ("raw IQ", "raw_iq_path", "raw_iq_sha256", "raw_iq_size_bytes"),
        ("metadata", "metadata_path", "metadata_sha256", "metadata_size_bytes"),
        (
            "condition record",
            "condition_record_path",
            "condition_record_sha256",
            "condition_record_size_bytes",
        ),
    ):
        path = Path(str(binding.get(path_key, "")))
        observed = _file_evidence(path, name)
        if observed != {
            "path": binding.get(path_key),
            "sha256": binding.get(sha_key),
            "size_bytes": binding.get(size_key),
        }:
            raise SelectedStateRunError(f"{name} differs from its capture binding")
        _assert_local_rpi_storage(path)
    record = _read_json(Path(str(binding["condition_record_path"])), "condition record")
    if (
        record.get("schema") != 1
        or record.get("record_kind") != CAPTURE_RECORD_KIND
        or record.get("mode") != mode
        or record.get("run_id") != contract.get("run_id")
        or record.get("capture_index") != capture_index
        or record.get("plan_contract_sha256") != plan_sha
        or record.get("context") != _qualification_context(contract, plan_sha)
        or record.get("fixture_evidence_sha256") != contract.get("fixture_evidence_sha256")
        or record.get("selector") != contract.get("selector")
    ):
        raise SelectedStateRunError("condition record is not bound to the exact plan/capture")
    artifact_evidence = record.get("artifact_evidence")
    if not isinstance(artifact_evidence, Mapping):
        raise SelectedStateRunError("condition record lacks artifact evidence")
    expected_artifact = {
        "artifact_id": binding.get("artifact_id"),
        "data_path": binding.get("raw_iq_path"),
        "data_sha256": binding.get("raw_iq_sha256"),
        "data_size_bytes": binding.get("raw_iq_size_bytes"),
        "metadata_path": binding.get("metadata_path"),
        "metadata_sha256": binding.get("metadata_sha256"),
        "metadata_size_bytes": binding.get("metadata_size_bytes"),
    }
    if dict(artifact_evidence) != expected_artifact:
        raise SelectedStateRunError("condition record artifact bytes differ from capture binding")
    artifact_raw = record.get("artifact")
    try:
        artifact = ArtifactSummary.model_validate(artifact_raw)
    except Exception as error:
        raise SelectedStateRunError("condition record artifact summary is invalid") from error
    if artifact.artifact_id != binding.get("artifact_id") or not verify_artifact(artifact):
        raise SelectedStateRunError("retained artifact failed its independent SHA-256 verification")
    settings = contract.get("capture_settings")
    capture = record.get("capture")
    if not isinstance(settings, Mapping) or not isinstance(capture, Mapping):
        raise SelectedStateRunError("condition record lacks capture settings/readbacks")
    frame_count, sample_count = _capture_shape(mode)
    expected_capture = {
        "serial": contract.get("serial"),
        "uri": contract.get("uri"),
        "model": (
            contract["device_identity"]["iio_context_facts"]["model"]
            if isinstance(contract.get("device_identity"), Mapping)
            else None
        ),
        "firmware_version": (
            contract["device_identity"]["iio_context_facts"]["firmware_version"]
            if isinstance(contract.get("device_identity"), Mapping)
            else None
        ),
        "phy_model": (
            contract["device_identity"]["iio_context_facts"]["phy_model"]
            if isinstance(contract.get("device_identity"), Mapping)
            else None
        ),
        "center_frequency_hz": CENTER_FREQUENCY_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "receiver_gain_db": RECEIVER_GAIN_DB,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": frame_count,
        "sample_count": sample_count,
        "kernel_buffers": KERNEL_BUFFERS,
        "metadata_abi": 2,
        "stream_id": int(binding["stream_id"]),
    }
    if any(capture.get(key) != value for key, value in expected_capture.items()):
        raise SelectedStateRunError("condition capture facts differ from immutable settings")
    identity = record.get("identity_preflight")
    if not leakage_runner._identity_passed(
        identity,
        serial=str(contract.get("serial")),
        requested_uri=str(contract.get("uri")),
    ):
        raise SelectedStateRunError("capture lacks exact current USB identity preflight")
    if not leakage_runner._mute_passed(
        record.get("initial_mute"),
        serial=str(contract.get("serial")),
        purpose="pre_capture",
    ) or not leakage_runner._mute_passed(
        record.get("final_mute"),
        serial=str(contract.get("serial")),
        purpose="post_capture",
    ):
        raise SelectedStateRunError("capture lacks exact pre/post TX mute evidence")
    rf_readback = record.get("rf_readback")
    if not isinstance(rf_readback, Mapping):
        raise SelectedStateRunError("capture lacks RF readback evidence")
    try:
        validate_tx1_rf_readback_evidence(
            rf_readback,
            planned_kernel_buffers=KERNEL_BUFFERS,
            planned_tx_gain_db=TX_HARDWARE_GAIN_DB,
            planned_dds_scale=DDS_SCALE,
            planned_tone_hz=TONE_OFFSET_HZ,
            sample_rate_hz=SAMPLE_RATE_HZ,
        )
    except ValueError as error:
        raise SelectedStateRunError(str(error)) from error
    metadata = _read_json(Path(str(binding["metadata_path"])), "capture metadata")
    try:
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=sample_count,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
    except ValueError as error:
        raise SelectedStateRunError(f"ABI2 continuity re-audit failed: {error}") from error
    if str(continuity.get("stream_id")) != str(binding.get("stream_id")):
        raise SelectedStateRunError("metadata stream ID differs from capture binding")
    rx1, rx2 = _load_dual_rx_ci16(Path(str(binding["raw_iq_path"])), sample_count=sample_count)
    return record, continuity, rx1, rx2


def _quality_document(
    *,
    sample_count: int,
    continuity: Mapping[str, Any],
    rx1: np.ndarray,
    rx2: np.ndarray,
    reference_detected: bool,
    reference_snr_db: float,
    final_mute: bool,
    final_selector: bool,
) -> dict[str, Any]:
    clipped, headroom_db = _raw_headroom(rx1, rx2)
    return {
        "metadata_abi": int(continuity.get("metadata_abi", -1)),
        "expected_sample_count": sample_count,
        "observed_sample_count": sample_count,
        "raw_sample_count": int(rx1.size),
        "continuity_verified": True,
        "missing_sample_count": 0,
        "clipped_sample_count": clipped,
        "adc_headroom_db": headroom_db,
        "reference_detected": bool(reference_detected),
        "reference_snr_db": float(reference_snr_db),
        "final_mute_verified": bool(final_mute),
        "final_selector_control_verified": bool(final_selector),
    }


def _target_image_preflight_passed(value: object, *, contract: Mapping[str, Any]) -> bool:
    selector = contract.get("selector")
    control = contract.get("selector_control")
    return (
        isinstance(selector, Mapping)
        and isinstance(control, Mapping)
        and isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_live_selector_image_readback_v1"
        and value.get("status") == "passed"
        and value.get("image_role") == contract.get("image_role")
        and value.get("selector_evidence_sha256") == selector.get("sha256")
        and value.get("firmware_bin_sha256") == selector.get("firmware_bin_sha256")
        and value.get("profile_contract_sha256") == selector.get("profile_contract_sha256")
        and _target_image_write_admitted(value, contract=contract)
        and value.get("full_bin_and_uid_compared_before_reset_run") is True
        and value.get("reviewed_image_started_only_after_exact_match") is True
        and value.get("mailbox_access_performed") is False
        and value.get("reset_run_succeeded") is True
        and value.get("error") is None
    )


def _target_image_write_admitted(value: object, *, contract: Mapping[str, Any]) -> bool:
    """Admit selector writes only after exact flash bytes and UID are proven."""

    selector = contract.get("selector")
    board_id = contract.get("board_id")
    expected_uid = board_id.removeprefix("stm32c011-") if isinstance(board_id, str) else None
    return (
        isinstance(selector, Mapping)
        and isinstance(board_id, str)
        and board_id.startswith("stm32c011-")
        and isinstance(expected_uid, str)
        and len(expected_uid) == 24
        and all(character in "0123456789abcdef" for character in expected_uid)
        and isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_live_selector_image_readback_v1"
        and value.get("image_role") == contract.get("image_role")
        and value.get("selector_evidence_sha256") == selector.get("sha256")
        and value.get("firmware_bin_sha256") == selector.get("firmware_bin_sha256")
        and value.get("profile_contract_sha256") == selector.get("profile_contract_sha256")
        and value.get("exact_byte_match") is True
        and value.get("uid_exact_match") is True
        and value.get("expected_board_id") == board_id
        and value.get("observed_uid") == expected_uid
        and value.get("expected_target_sha256") == selector.get("firmware_bin_sha256")
        and value.get("observed_target_sha256") == selector.get("firmware_bin_sha256")
        and value.get("full_bin_and_uid_compared_before_reset_run") is True
        and value.get("mailbox_access_performed") is False
    )


def _target_image_admission_binding(
    value: object, *, contract: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not _target_image_write_admitted(value, contract=contract):
        return None
    assert isinstance(value, Mapping)
    selector = contract["selector"]
    assert isinstance(selector, Mapping)
    return {
        "schema": 1,
        "evidence_kind": "exact_selector_image_and_uid_admission_binding_v1",
        "image_role": contract["image_role"],
        "selector_evidence_sha256": selector["sha256"],
        "firmware_bin_sha256": selector["firmware_bin_sha256"],
        "profile_contract_sha256": selector["profile_contract_sha256"],
        "expected_board_id": value["expected_board_id"],
        "observed_uid": value["observed_uid"],
        "expected_target_sha256": value["expected_target_sha256"],
        "observed_target_sha256": value["observed_target_sha256"],
        "full_bin_and_uid_compared_before_reset_run": True,
        "mailbox_access_performed": False,
        "source_evidence_sha256": canonical_sha256(dict(value)),
    }


def _target_image_admission_binding_passed(value: object, *, contract: Mapping[str, Any]) -> bool:
    selector = contract.get("selector")
    board_id = contract.get("board_id")
    expected_uid = board_id.removeprefix("stm32c011-") if isinstance(board_id, str) else None
    expected_fields = {
        "schema",
        "evidence_kind",
        "image_role",
        "selector_evidence_sha256",
        "firmware_bin_sha256",
        "profile_contract_sha256",
        "expected_board_id",
        "observed_uid",
        "expected_target_sha256",
        "observed_target_sha256",
        "full_bin_and_uid_compared_before_reset_run",
        "mailbox_access_performed",
        "source_evidence_sha256",
    }
    return (
        isinstance(selector, Mapping)
        and isinstance(board_id, str)
        and board_id.startswith("stm32c011-")
        and isinstance(expected_uid, str)
        and len(expected_uid) == 24
        and all(character in "0123456789abcdef" for character in expected_uid)
        and isinstance(value, Mapping)
        and set(value) == expected_fields
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_selector_image_and_uid_admission_binding_v1"
        and value.get("image_role") == contract.get("image_role")
        and value.get("selector_evidence_sha256") == selector.get("sha256")
        and value.get("firmware_bin_sha256") == selector.get("firmware_bin_sha256")
        and value.get("profile_contract_sha256") == selector.get("profile_contract_sha256")
        and value.get("expected_board_id") == board_id
        and value.get("observed_uid") == expected_uid
        and value.get("expected_target_sha256") == selector.get("firmware_bin_sha256")
        and value.get("observed_target_sha256") == selector.get("firmware_bin_sha256")
        and value.get("full_bin_and_uid_compared_before_reset_run") is True
        and value.get("mailbox_access_performed") is False
        and isinstance(value.get("source_evidence_sha256"), str)
        and len(str(value["source_evidence_sha256"])) == 64
        and all(character in "0123456789abcdef" for character in value["source_evidence_sha256"])
    )


def _capture_command_document(
    attestation: Mapping[str, Any], *, state: str, code: int
) -> dict[str, Any]:
    readback = attestation.get("readback")
    gpio = attestation.get("gpio_output_latch_readback")
    if not isinstance(readback, Mapping) or not isinstance(gpio, Mapping):
        raise SelectedStateRunError("static selector attestation lacks mailbox/GPIO readback")
    return {
        "commanded_state": state,
        "commanded_code": code,
        "command_sequence": readback.get("command_sequence"),
        "acknowledged_sequence": readback.get("acknowledged_sequence"),
        "applied_state": state,
        "applied_code": readback.get("applied_code"),
        "gpio_latch_code": gpio.get("masked_selector_code"),
        "lease_ms": readback.get("command_lease_ms"),
        "command_valid": readback.get("command_valid"),
        "readback_passed": attestation.get("status") == "passed",
    }


def _reanalyze_capture_evidence(
    capture_set: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    plan_sha: str,
) -> dict[str, Any]:
    """Build qualification input only from retained bytes and bound live records."""

    mode = contract.get("mode")
    if mode not in {"static-bench", "fast20-timing", "fast20-matrix"}:
        raise SelectedStateRunError("capture plan mode is invalid")
    if set(capture_set) != {
        "schema",
        "evidence_kind",
        "mode",
        "context",
        "captures",
    } or (
        capture_set.get("schema") != 1
        or capture_set.get("evidence_kind") != "5g8_selected_state_raw_capture_set_v1"
        or capture_set.get("mode") != mode
        or capture_set.get("context") != _qualification_context(contract, plan_sha)
    ):
        raise SelectedStateRunError("capture set is not bound to the immutable qualification plan")
    raw_captures = capture_set.get("captures")
    if not isinstance(raw_captures, list) or len(raw_captures) != _expected_capture_count(mode):
        raise SelectedStateRunError("capture set does not contain the exact planned stream count")
    captures: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    analyses: list[tuple[dict[str, Any], dict[str, Any]]] = []
    profile = None
    selector_control = contract.get("selector_control")
    if not isinstance(selector_control, Mapping):
        raise SelectedStateRunError("plan selector control/profile binding is missing")
    if mode == "static-bench":
        try:
            normalized_control = one_hot_runner._validate_one_hot_selector_control(selector_control)
            state_codes = one_hot_runner._state_map(normalized_control)
        except (OSError, ValueError) as error:
            raise SelectedStateRunError(str(error)) from error
    else:
        if selector_control.get(
            "control_kind"
        ) != "sealed_fast20_autonomous_schedule" or selector_control.get("state_order") != list(
            EXPECTED_STATES
        ):
            raise SelectedStateRunError("Fast20 control/profile contract is invalid")
        profile_file = _validate_file_evidence(selector_control.get("profile"), "Fast20 profile")
        profile = load_profile(Path(profile_file["path"]))
        if profile.contract_sha256 != selector_control.get("profile_contract_sha256"):
            raise SelectedStateRunError("Fast20 profile bytes differ from the plan")
        state_codes = dict(selector_control.get("state_codes", {}))

    for index, raw_binding in enumerate(raw_captures, start=1):
        if not isinstance(raw_binding, Mapping):
            raise SelectedStateRunError("capture binding must be an object")
        binding = dict(raw_binding)
        record, continuity, rx1, rx2 = _capture_binding_from_record(
            binding,
            contract=contract,
            plan_sha=plan_sha,
            mode=mode,
            capture_index=index,
        )
        try:
            reference = analyze_coherent_leakage(
                rx1,
                rx2,
                sample_rate_hz=SAMPLE_RATE_HZ,
                tone_offset_hz=float(record["capture"]["tone_offset_hz_readback"]),
                block_duration_s=0.1,
                minimum_block_count=3,
            )
            final_mute_passed = leakage_runner._mute_passed(
                record.get("final_mute"),
                serial=str(contract.get("serial")),
                purpose="post_capture",
            )
            if mode == "static-bench":
                state = str(record.get("state"))
                if state != EXPECTED_STATES[index - 1] or state not in state_codes:
                    raise SelectedStateRunError("static capture state order differs from plan")
                code = int(state_codes[state])
                before = record.get("selector_before")
                after = record.get("selector_after")
                cleanup = record.get("selector_cleanup")
                selector_passed = (
                    one_hot_runner._selector_passed(
                        before,
                        selector_control=normalized_control,
                        state_name=state,
                        state_code=code,
                        purpose="before_condition",
                    )
                    and one_hot_runner._selector_passed(
                        after,
                        selector_control=normalized_control,
                        state_name=state,
                        state_code=code,
                        purpose="after_pluto_mute",
                    )
                    and one_hot_runner._selector_hold_command_unchanged(before, after)
                    and one_hot_runner._selector_passed(
                        cleanup,
                        selector_control=normalized_control,
                        state_name=state,
                        state_code=code,
                        purpose="cleanup_all_off",
                    )
                )
                if not selector_passed or not _target_image_preflight_passed(
                    record.get("target_image_preflight"), contract=contract
                ):
                    raise SelectedStateRunError(
                        "static capture lacks exact image/state/readback/cleanup evidence"
                    )
                if not isinstance(before, Mapping):
                    raise SelectedStateRunError(
                        "static capture lacks pre-condition selector evidence"
                    )
                quality = _quality_document(
                    sample_count=STATIC_SAMPLE_COUNT,
                    continuity=continuity,
                    rx1=rx1,
                    rx2=rx2,
                    reference_detected=reference.rx1.tone_detected,
                    reference_snr_db=reference.rx1.tone_to_noise_snr_db,
                    final_mute=final_mute_passed,
                    final_selector=selector_passed,
                )
                analyses.append(
                    (
                        quality,
                        {
                            "state": state,
                            "command": _capture_command_document(
                                before,
                                state=state,
                                code=code,
                            ),
                            "transfer": _leakage_transfer_document(reference),
                        },
                    )
                )
            else:
                assert profile is not None
                if record.get("state") is not None or not _target_image_preflight_passed(
                    record.get("target_image_preflight"), contract=contract
                ):
                    raise SelectedStateRunError(
                        "Fast20 capture lacks exact live image/schedule evidence"
                    )
                fast20_cleanup_passed = _fast20_failure_cleanup_passed(
                    record.get("selector_cleanup"),
                    selector_control=selector_control,
                )
                if not fast20_cleanup_passed:
                    raise SelectedStateRunError(
                        "Fast20 capture lacks sealed electrical GPIO/MODER ALL_OFF evidence"
                    )
                pilot = estimate_coherent_pilot_offset(
                    rx1,
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    nominal_tone_offset_hz=float(record["capture"]["tone_offset_hz_readback"]),
                )
                pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
                pilot_passed = (
                    pilot.confidence >= MINIMUM_PILOT_CONFIDENCE
                    and pilot.phase_step_coherence >= MINIMUM_PILOT_STEP_COHERENCE
                    and pilot_phase_rms_deg <= MAXIMUM_PILOT_PHASE_RMS_DEG
                )
                quality = _quality_document(
                    sample_count=FAST20_SAMPLE_COUNT,
                    continuity=continuity,
                    rx1=rx1,
                    rx2=rx2,
                    reference_detected=reference.rx1.tone_detected and pilot_passed,
                    reference_snr_db=reference.rx1.tone_to_noise_snr_db,
                    final_mute=final_mute_passed,
                    final_selector=fast20_cleanup_passed,
                )
                if mode == "fast20-timing":
                    dwell = analyze_fast20_dwell_isolation(
                        rx2,
                        sample_rate_hz=SAMPLE_RATE_HZ,
                        tone_offset_hz=pilot.estimated_offset_hz,
                        profile=profile,
                        continuity_ledger=_metadata_continuity_blocks(
                            _read_json(Path(binding["metadata_path"]), "capture metadata")
                        ),
                        minimum_complete_frames=MINIMUM_FAST20_CYCLES,
                    )
                    timing = {
                        "state_order": list(EXPECTED_STATES),
                        "isolation_verified": dwell.isolation_verified,
                        "continuity_verified": dwell.continuity_verified,
                        "complete_cycle_count": dwell.complete_frame_count,
                        "rejected_marker_count": dwell.rejected_marker_count,
                        "threshold_stable": dwell.threshold_stable,
                        "dwell_by_state": {
                            state.name: {
                                "observed_count": len(state.durations_ms),
                                "duration_min_ms": state.minimum_ms,
                                "duration_max_ms": state.maximum_ms,
                            }
                            for state in dwell.states
                        },
                    }
                    analyses.append((quality, {"timing": timing}))
                else:
                    transfer = analyze_fast20_reference_transfer(
                        rx1,
                        rx2,
                        sample_rate_hz=SAMPLE_RATE_HZ,
                        tone_offset_hz=pilot.estimated_offset_hz,
                        profile=profile,
                        continuity_ledger=_metadata_continuity_blocks(
                            _read_json(Path(binding["metadata_path"]), "capture metadata")
                        ),
                        alignment_search_mode=AlignmentSearchMode.TRANSITION_SEEDED,
                    )
                    if (
                        not transfer.continuity_verified
                        or transfer.complete_cycle_count < MINIMUM_FAST20_CYCLES
                        or transfer.reference_valid_bin_fraction < 0.95
                    ):
                        raise SelectedStateRunError(
                            "Fast20 matrix schedule/reference admission failed"
                        )
                    states = {
                        ALL_OFF: _cycle_summary_transfer_document(transfer.all_off_raw_rx2_over_rx1)
                    }
                    states.update(
                        {
                            estimate.name: _cycle_summary_transfer_document(
                                estimate.raw_rx2_over_rx1
                            )
                            for estimate in transfer.states
                        }
                    )
                    analyses.append((quality, {"states": states}))
        finally:
            del rx1, rx2
            gc.collect()
        captures.append(binding)
        records.append(record)

    context = _qualification_context(contract, plan_sha)
    profile_contract = contract["selector"]["profile_contract_sha256"]
    if mode == "static-bench":
        observations = [
            {
                "state": details["state"],
                "capture": capture,
                "command": details["command"],
                "quality": quality,
                "transfer": details["transfer"],
            }
            for capture, (quality, details) in zip(captures, analyses, strict=True)
        ]
        final_cleanup = records[-1]["selector_cleanup"]
        final_readback = final_cleanup["readback"]
        final_gpio = final_cleanup["gpio_output_latch_readback"]
        return {
            "schema": 1,
            "evidence_kind": STATIC_KIND,
            "context": context,
            "state_order": list(EXPECTED_STATES),
            "state_codes": state_codes,
            "observations": observations,
            "final_mute_verified": leakage_runner._mute_passed(
                records[-1].get("final_mute"),
                serial=str(contract.get("serial")),
                purpose="post_capture",
            ),
            "final_all_off_readback": {
                "state": ALL_OFF,
                "mailbox_code": final_readback["applied_code"],
                "gpio_latch_code": final_gpio["masked_selector_code"],
                "passed": one_hot_runner._selector_passed(
                    final_cleanup,
                    selector_control=normalized_control,
                    state_name=str(records[-1]["state"]),
                    state_code=int(state_codes[str(records[-1]["state"])]),
                    purpose="cleanup_all_off",
                ),
            },
        }
    assert profile is not None
    final_schedule = {
        "image_role": "fast20",
        "profile_contract_sha256": profile_contract,
        "passed": all(
            _target_image_preflight_passed(record["target_image_preflight"], contract=contract)
            and _fast20_failure_cleanup_passed(
                record.get("selector_cleanup"),
                selector_control=selector_control,
            )
            for record in records
        ),
    }
    final_mute_verified = all(
        leakage_runner._mute_passed(
            record.get("final_mute"),
            serial=str(contract.get("serial")),
            purpose="post_capture",
        )
        for record in records
    )
    if mode == "fast20-timing":
        return {
            "schema": 1,
            "evidence_kind": TIMING_KIND,
            "context": context,
            "profile": {
                "profile_id": profile.profile_id,
                "profile_contract_sha256": profile.contract_sha256,
                "state_order": list(EXPECTED_STATES),
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "expected_sample_count": FAST20_SAMPLE_COUNT,
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FAST20_FRAME_COUNT,
                "minimum_complete_cycles": MINIMUM_FAST20_CYCLES,
                "dwell_window_ms_by_state": {
                    state.name: list(state.window_ms) for state in profile.states
                },
            },
            "runs": [
                {"capture": capture, "quality": quality, "timing": details["timing"]}
                for capture, (quality, details) in zip(captures, analyses, strict=True)
            ],
            "final_mute_verified": final_mute_verified,
            "final_fast20_schedule_verified": final_schedule,
        }
    return {
        "schema": 1,
        "evidence_kind": MATRIX_KIND,
        "context": context,
        "state_order": list(EXPECTED_STATES),
        "repeat_count": 5,
        "streams": [
            {
                "repeat_index": index,
                "capture": capture,
                "quality": quality,
                "state_order": list(EXPECTED_STATES),
                "states": details["states"],
            }
            for index, (capture, (quality, details)) in enumerate(
                zip(captures, analyses, strict=True), start=1
            )
        ],
        "final_mute_verified": final_mute_verified,
        "final_fast20_schedule_verified": final_schedule,
    }


def _live_capture_boundary(
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
    return SafeDdsTonePlan(
        uri=str(contract["uri"]),
        serial=str(contract["serial"]),
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=0,
        tx_hardware_gain_db=TX_HARDWARE_GAIN_DB,
        dds_scale=DDS_SCALE,
        receiver_gain_db=float(RECEIVER_GAIN_DB),
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )


def _radio_settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _rf_readback(capture: Any, plan: SafeDdsTonePlan) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [capture.tx_gain_readback_db, -80.0],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
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
    return evidence


def _tone_readback_hz(evidence: Mapping[str, Any]) -> float:
    values = evidence.get("dds_frequency_readback_hz")
    if not isinstance(values, list) or len(values) != 8:
        raise SelectedStateRunError("DDS frequency readback is malformed")
    active = (abs(float(values[0])), abs(float(values[2])))
    if abs(active[0] - active[1]) > math.ceil(SAMPLE_RATE_HZ / (1 << 16)):
        raise SelectedStateRunError("TX1 I/Q tone readbacks disagree")
    return sum(active) / 2.0


def _validate_live_capture(
    capture: Any,
    blocks: Sequence[SampleBlockV2],
    *,
    contract: Mapping[str, Any],
    forbidden_stream_ids: set[int],
) -> tuple[int, dict[str, Any]]:
    mode = contract.get("mode")
    assert mode in {"static-bench", "fast20-timing", "fast20-matrix"}
    frame_count, sample_count = _capture_shape(mode)
    device = contract.get("device_identity")
    facts = device.get("iio_context_facts") if isinstance(device, Mapping) else None
    if (
        not isinstance(facts, Mapping)
        or capture.identity.serial != contract.get("serial")
        or capture.identity.uri != contract.get("uri")
        or capture.identity.model != facts.get("model")
        or capture.identity.firmware_version != facts.get("firmware_version")
        or capture.settings != _radio_settings()
        or capture.sample_count != sample_count
        or len(capture.frames) != frame_count
        or capture.kernel_buffers != KERNEL_BUFFERS
        or len(blocks) != frame_count
        or any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in blocks)
    ):
        raise SelectedStateRunError("live capture identity/shape differs from immutable plan")
    ledger = leakage_runner._block_ledger(list(blocks))
    if ledger is None:
        raise SelectedStateRunError("live capture has no ABI2 block ledger")
    try:
        summary = validate_continuity_ledger(
            ledger,
            expected_total_samples=sample_count,
            expected_samples_per_block=SAMPLES_PER_FRAME,
        )
    except ValueError as error:
        raise SelectedStateRunError(str(error)) from error
    if summary.metadata_abi != 2 or summary.first_buffer_sequence != 0:
        raise SelectedStateRunError("live capture is not one fresh ABI2 stream")
    if summary.stream_id in forbidden_stream_ids:
        raise SelectedStateRunError("capture reused an earlier stream ID")
    for proof, block in zip(capture.frames, blocks, strict=True):
        if (
            proof.stream_id,
            proof.buffer_sequence,
            proof.first_sample_sequence,
            proof.last_sample_sequence_exclusive,
            proof.metadata_abi,
        ) != (
            block.stream_id,
            block.buffer_sequence,
            block.first_sample_sequence,
            block.last_sample_sequence_exclusive,
            block.metadata_abi,
        ):
            raise SelectedStateRunError("capture proof differs from retained ABI2 block")
    return summary.stream_id, _rf_readback(capture, _tone_plan(contract))


def _live_target_image_preflight(
    contract: Mapping[str, Any],
    *,
    evidence_root: Path,
    admission_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    role = contract.get("image_role")
    selector = contract.get("selector")
    control = contract.get("selector_control")
    if not isinstance(selector, Mapping) or not isinstance(control, Mapping):
        raise SelectedStateRunError("target-image preflight lacks selector bindings")
    if role == "bench":
        result = one_hot_runner._live_target_image_attestation(control)
        passed = one_hot_runner._target_image_passed(result, selector_control=control)
        exact_image_and_uid = result.get("exact_bin_and_uid_match") is True
        full_bin_and_uid_compared = (
            isinstance(result.get("byte_count"), int)
            and int(result["byte_count"]) > 0
            and isinstance(result.get("observed_target_sha256"), str)
            and isinstance(result.get("observed_uid"), str)
        )
        normalized = {
            "schema": 1,
            "evidence_kind": "exact_live_selector_image_readback_v1",
            "status": "passed" if passed else "failed",
            "image_role": "bench",
            "selector_evidence_sha256": selector.get("sha256"),
            "firmware_bin_sha256": selector.get("firmware_bin_sha256"),
            "profile_contract_sha256": selector.get("profile_contract_sha256"),
            "exact_byte_match": exact_image_and_uid,
            "uid_exact_match": exact_image_and_uid,
            "expected_board_id": result.get("expected_board_id"),
            "observed_uid": result.get("observed_uid"),
            "expected_target_sha256": result.get("expected_bin_sha256"),
            "observed_target_sha256": result.get("observed_target_sha256"),
            "full_bin_and_uid_compared_before_reset_run": full_bin_and_uid_compared,
            "reviewed_image_started_only_after_exact_match": passed,
            "mailbox_access_performed": False,
            "reset_run_succeeded": passed,
            "upstream": result,
            "error": None if passed else {"type": "TargetImageMismatch", "message": "bench"},
        }
        binding = _target_image_admission_binding(normalized, contract=contract)
        if admission_state is not None:
            admission_state["selector_image_admitted"] = binding is not None
            admission_state["selector_image_admission"] = binding
        return normalized
    firmware = _validate_file_evidence(control.get("firmware_bin"), "Fast20 firmware BIN")
    config = _validate_file_evidence(control.get("openocd_config"), "Fast20 OpenOCD config")
    evidence_root = _ensure_local_directory(
        evidence_root,
        "target-image evidence directory",
        create_only=True,
    )
    dump_path = evidence_root / "live-fast20-flash.bin"
    uid_path = evidence_root / "live-fast20-uid.bin"
    log_path = evidence_root / "openocd-readback.log"
    reset_log_path = evidence_root / "openocd-reset-run.log"
    size = int(firmware["size_bytes"])
    board_id = str(contract.get("board_id", ""))
    expected_uid = board_id.removeprefix("stm32c011-")
    if (
        not board_id.startswith("stm32c011-")
        or len(expected_uid) != 24
        or any(character not in "0123456789abcdef" for character in expected_uid)
    ):
        raise SelectedStateRunError("selector board ID is not one exact STM32C011 UID")
    command = (
        "openocd",
        "-f",
        str(config["path"]),
        "-c",
        (
            f"init; reset halt; dump_image {_openocd_tcl_path(dump_path)} "
            f"0x08000000 {size}; dump_image {_openocd_tcl_path(uid_path)} "
            f"0x{leakage_runner.STM32C011_UID_ADDRESS:x} "
            f"{leakage_runner.STM32C011_UID_SIZE_BYTES}; shutdown"
        ),
    )
    primary_result = subprocess.run(command, capture_output=True, text=True, check=False)
    log_path.write_text(primary_result.stdout + primary_result.stderr, encoding="utf-8")
    preflight_command_succeeded = primary_result.returncode == 0
    observed_flash = dump_path.read_bytes() if dump_path.is_file() else b""
    observed_uid_bytes = uid_path.read_bytes() if uid_path.is_file() else b""
    observed_sha256 = hashlib.sha256(observed_flash).hexdigest() if observed_flash else None
    observed_uid = observed_uid_bytes.hex() if observed_uid_bytes else None
    uid_exact_match = (
        len(observed_uid_bytes) == leakage_runner.STM32C011_UID_SIZE_BYTES
        and observed_uid == expected_uid
    )
    full_bin_and_uid_compared = (
        len(observed_flash) == size
        and len(observed_uid_bytes) == leakage_runner.STM32C011_UID_SIZE_BYTES
    )
    exact_match = (
        preflight_command_succeeded
        and full_bin_and_uid_compared
        and observed_sha256 == firmware["sha256"]
        and observed_flash == Path(str(firmware["path"])).read_bytes()
        and uid_exact_match
    )
    admission_claim = {
        "schema": 1,
        "evidence_kind": "exact_live_selector_image_readback_v1",
        "image_role": "fast20",
        "selector_evidence_sha256": selector.get("sha256"),
        "firmware_bin_sha256": selector.get("firmware_bin_sha256"),
        "profile_contract_sha256": selector.get("profile_contract_sha256"),
        "exact_byte_match": exact_match,
        "uid_exact_match": uid_exact_match,
        "expected_board_id": board_id,
        "observed_uid": observed_uid,
        "expected_target_sha256": firmware["sha256"],
        "observed_target_sha256": observed_sha256,
        "full_bin_and_uid_compared_before_reset_run": full_bin_and_uid_compared,
        "mailbox_access_performed": False,
    }
    binding = _target_image_admission_binding(admission_claim, contract=contract)
    if admission_state is not None:
        admission_state["selector_image_admitted"] = binding is not None
        admission_state["selector_image_admission"] = binding
    reset_result: subprocess.CompletedProcess[str] | None = None
    if exact_match:
        reset_result = subprocess.run(
            (
                "openocd",
                "-f",
                str(config["path"]),
                "-c",
                "init; reset run; shutdown",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        reset_log_path.write_text(
            reset_result.stdout + reset_result.stderr,
            encoding="utf-8",
        )
    reset_run_attempted = reset_result is not None
    reset_run_succeeded = reset_result is not None and reset_result.returncode == 0
    passed = exact_match and reset_run_succeeded
    return {
        "schema": 1,
        "evidence_kind": "exact_live_selector_image_readback_v1",
        "status": "passed" if passed else "failed",
        "image_role": "fast20",
        "selector_evidence_sha256": selector.get("sha256"),
        "firmware_bin_sha256": selector.get("firmware_bin_sha256"),
        "profile_contract_sha256": selector.get("profile_contract_sha256"),
        "exact_byte_match": exact_match,
        "uid_exact_match": uid_exact_match,
        "expected_board_id": board_id,
        "observed_uid": observed_uid,
        "expected_target_sha256": firmware["sha256"],
        "observed_target_sha256": observed_sha256,
        "full_bin_and_uid_compared_before_reset_run": full_bin_and_uid_compared,
        "reviewed_image_started_only_after_exact_match": reset_run_attempted,
        "mailbox_access_performed": False,
        "target_kept_halted_or_unknown_on_failure": not passed,
        "reset_run_succeeded": reset_run_succeeded,
        "reset_run_attempted": reset_run_attempted,
        "preflight_command_succeeded": preflight_command_succeeded,
        "flash_readback": (
            _file_evidence(dump_path, "Fast20 flash readback") if dump_path.is_file() else None
        ),
        "uid_readback": (
            _file_evidence(uid_path, "Fast20 UID readback") if uid_path.is_file() else None
        ),
        "openocd_log": _file_evidence(log_path, "Fast20 OpenOCD log"),
        "reset_run_openocd_log": (
            _file_evidence(reset_log_path, "Fast20 reset-run OpenOCD log")
            if reset_run_attempted
            else None
        ),
        "error": None if passed else {"type": "TargetImageMismatch", "message": "fast20"},
    }


def _live_fast20_all_off_cleanup(
    selector_control: Mapping[str, Any], *, evidence_root: Path
) -> dict[str, Any]:
    """Halt one admitted Fast20 image and prove its GPIO latch is ALL_OFF.

    This boundary deliberately never uses the firmware mailbox and never resumes
    the target.  Its caller must first prove the exact image bytes and STM32 UID.
    """

    config = _validate_file_evidence(
        selector_control.get("openocd_config"), "Fast20 cleanup OpenOCD config"
    )
    states = selector_control.get("state_codes")
    if not isinstance(states, Mapping) or isinstance(states.get("ALL_OFF"), bool):
        raise SelectedStateRunError("Fast20 cleanup lacks one exact ALL_OFF code")
    all_off_code = int(states["ALL_OFF"])
    selector_mask = int(one_hot_runner.SELECTOR_GPIO_MASK)
    if all_off_code < 0 or all_off_code & ~selector_mask:
        raise SelectedStateRunError("Fast20 cleanup ALL_OFF code is outside selector GPIOs")
    evidence_root = _ensure_local_directory(
        evidence_root,
        "Fast20 cleanup evidence directory",
        create_only=True,
    )
    odr_path = evidence_root / "gpioa-odr.bin"
    moder_path = evidence_root / "gpioa-moder.bin"
    moder_before_path = evidence_root / "gpioa-moder-before.bin"
    read_log_path = evidence_root / "openocd-moder-read.log"
    action_log_path = evidence_root / "openocd-all-off.log"
    gpio_odr_address = int(one_hot_runner.GPIOA_ODR_ADDRESS)
    gpio_moder_address = gpio_odr_address - 0x14
    gpio_bsrr_address = gpio_odr_address + 4
    selector_mode_mask = 0xFF
    selector_output_modes = 0x55
    read_command = (
        "openocd",
        "-f",
        str(config["path"]),
        "-c",
        (
            f"init; halt; dump_image {_openocd_tcl_path(moder_before_path)} "
            f"0x{gpio_moder_address:x} 4; shutdown"
        ),
    )
    read_result = subprocess.run(read_command, capture_output=True, text=True, check=False)
    read_log_path.write_text(read_result.stdout + read_result.stderr, encoding="utf-8")
    moder_before_bytes = moder_before_path.read_bytes() if moder_before_path.is_file() else b""
    raw_moder_before = (
        int.from_bytes(moder_before_bytes, "little") if len(moder_before_bytes) == 4 else None
    )
    # Latch ALL_OFF before enabling output mode.  Preserve every non-selector
    # MODER field, set PA0..PA3 to GPIO output, and leave the target halted.
    bsrr_word = ((selector_mask & ~all_off_code) << 16) | all_off_code
    desired_moder = (
        (raw_moder_before & ~selector_mode_mask) | selector_output_modes
        if raw_moder_before is not None
        else None
    )
    action_result: subprocess.CompletedProcess[str] | None = None
    if read_result.returncode == 0 and desired_moder is not None:
        action_command = (
            "openocd",
            "-f",
            str(config["path"]),
            "-c",
            (
                f"init; halt; mww 0x{gpio_bsrr_address:x} 0x{bsrr_word:08x}; "
                f"mww 0x{gpio_moder_address:x} 0x{desired_moder:08x}; "
                f"dump_image {_openocd_tcl_path(odr_path)} "
                f"0x{gpio_odr_address:x} 4; "
                f"dump_image {_openocd_tcl_path(moder_path)} "
                f"0x{gpio_moder_address:x} 4; shutdown"
            ),
        )
        action_result = subprocess.run(action_command, capture_output=True, text=True, check=False)
        action_log_path.write_text(action_result.stdout + action_result.stderr, encoding="utf-8")
    odr_bytes = odr_path.read_bytes() if odr_path.is_file() else b""
    moder_bytes = moder_path.read_bytes() if moder_path.is_file() else b""
    raw_odr = int.from_bytes(odr_bytes, "little") if len(odr_bytes) == 4 else None
    raw_moder = int.from_bytes(moder_bytes, "little") if len(moder_bytes) == 4 else None
    masked_code = raw_odr & selector_mask if raw_odr is not None else None
    masked_modes = raw_moder & selector_mode_mask if raw_moder is not None else None
    passed = (
        read_result.returncode == 0
        and action_result is not None
        and action_result.returncode == 0
        and masked_code == all_off_code
        and masked_modes == selector_output_modes
    )
    return {
        "schema": 1,
        "evidence_kind": FAST20_FAILURE_CLEANUP_KIND,
        "status": "passed" if passed else "failed",
        "selector_write_authorized_by_exact_image_and_uid_admission": True,
        "mailbox_access_performed": False,
        "target_resume_command_issued": False,
        "target_left_halted": (
            read_result.returncode == 0
            and action_result is not None
            and action_result.returncode == 0
        ),
        "gpio_output_mode_before": {
            "register": "GPIOA_MODER",
            "address": gpio_moder_address,
            "raw_value": raw_moder_before,
            "preserved_non_selector_mask": (~selector_mode_mask) & 0xFFFFFFFF,
            "desired_value": desired_moder,
        },
        "gpio_output_latch_readback": {
            "register": "GPIOA_ODR",
            "address": gpio_odr_address,
            "selector_mask": selector_mask,
            "raw_value": raw_odr,
            "masked_selector_code": masked_code,
            "expected_selector_code": all_off_code,
            "passed": masked_code == all_off_code,
            "physical_rf_state_proven": False,
        },
        "gpio_output_mode_readback": {
            "register": "GPIOA_MODER",
            "address": gpio_moder_address,
            "selector_mode_mask": selector_mode_mask,
            "raw_value": raw_moder,
            "masked_selector_modes": masked_modes,
            "expected_selector_output_modes": selector_output_modes,
            "passed": masked_modes == selector_output_modes,
        },
        "electrical_selector_all_off_proven": passed,
        "openocd_returncodes": {
            "moder_read": read_result.returncode,
            "all_off_and_mode_write": (
                action_result.returncode if action_result is not None else None
            ),
        },
        "moder_read_openocd_log": _file_evidence(
            read_log_path, "Fast20 cleanup MODER-read OpenOCD log"
        ),
        "all_off_openocd_log": (
            _file_evidence(action_log_path, "Fast20 cleanup ALL_OFF OpenOCD log")
            if action_log_path.is_file()
            else None
        ),
        "gpio_moder_before_readback": (
            _file_evidence(moder_before_path, "Fast20 cleanup initial GPIOA MODER readback")
            if moder_before_path.is_file()
            else None
        ),
        "gpio_odr_readback": (
            _file_evidence(odr_path, "Fast20 cleanup GPIOA ODR readback")
            if odr_path.is_file()
            else None
        ),
        "gpio_moder_readback": (
            _file_evidence(moder_path, "Fast20 cleanup GPIOA MODER readback")
            if moder_path.is_file()
            else None
        ),
        "error": (
            None
            if passed
            else {
                "type": "Fast20AllOffCleanupFailed",
                "message": (
                    "exact ALL_OFF GPIO latch/output mode and halted target were not proven"
                ),
            }
        ),
    }


def _fast20_failure_cleanup_passed(value: object, *, selector_control: Mapping[str, Any]) -> bool:
    states = selector_control.get("state_codes")
    if not isinstance(states, Mapping) or isinstance(states.get("ALL_OFF"), bool):
        return False
    all_off_code = int(states["ALL_OFF"])
    gpio = value.get("gpio_output_latch_readback") if isinstance(value, Mapping) else None
    modes = value.get("gpio_output_mode_readback") if isinstance(value, Mapping) else None
    modes_before = value.get("gpio_output_mode_before") if isinstance(value, Mapping) else None
    returncodes = value.get("openocd_returncodes") if isinstance(value, Mapping) else None
    if not all(isinstance(item, Mapping) for item in (gpio, modes, modes_before, returncodes)):
        return False
    assert isinstance(value, Mapping)
    assert isinstance(gpio, Mapping)
    assert isinstance(modes, Mapping)
    assert isinstance(modes_before, Mapping)
    assert isinstance(returncodes, Mapping)
    try:
        binary_files = {
            field: _validate_file_evidence(value.get(field), label)
            for field, label in (
                ("gpio_odr_readback", "Fast20 cleanup GPIOA ODR readback"),
                ("gpio_moder_readback", "Fast20 cleanup GPIOA MODER readback"),
                (
                    "gpio_moder_before_readback",
                    "Fast20 cleanup initial GPIOA MODER readback",
                ),
            )
        }
        for file in binary_files.values():
            _assert_local_rpi_storage(Path(str(file["path"])))
        for field, label in (
            ("moder_read_openocd_log", "Fast20 cleanup MODER-read OpenOCD log"),
            ("all_off_openocd_log", "Fast20 cleanup ALL_OFF OpenOCD log"),
        ):
            log_file = _validate_file_evidence(value.get(field), label)
            _assert_local_rpi_storage(Path(str(log_file["path"])))
        binary_values = {
            field: int.from_bytes(Path(str(file["path"])).read_bytes(), "little")
            for field, file in binary_files.items()
            if int(file["size_bytes"]) == 4
        }
    except (OSError, SelectedStateRunError):
        return False
    if set(binary_values) != set(binary_files):
        return False
    raw_odr = binary_values["gpio_odr_readback"]
    raw_moder = binary_values["gpio_moder_readback"]
    raw_moder_before = binary_values["gpio_moder_before_readback"]
    desired_moder = (raw_moder_before & ~0xFF) | 0x55
    return (
        value.get("schema") == 1
        and value.get("evidence_kind") == FAST20_FAILURE_CLEANUP_KIND
        and value.get("status") == "passed"
        and value.get("selector_write_authorized_by_exact_image_and_uid_admission") is True
        and value.get("mailbox_access_performed") is False
        and value.get("target_resume_command_issued") is False
        and value.get("target_left_halted") is True
        and value.get("electrical_selector_all_off_proven") is True
        and gpio.get("register") == "GPIOA_ODR"
        and gpio.get("address") == one_hot_runner.GPIOA_ODR_ADDRESS
        and gpio.get("selector_mask") == one_hot_runner.SELECTOR_GPIO_MASK
        and gpio.get("raw_value") == raw_odr
        and gpio.get("masked_selector_code") == all_off_code
        and gpio.get("expected_selector_code") == all_off_code
        and gpio.get("passed") is True
        and gpio.get("physical_rf_state_proven") is False
        and modes.get("register") == "GPIOA_MODER"
        and modes.get("address") == one_hot_runner.GPIOA_ODR_ADDRESS - 0x14
        and modes.get("selector_mode_mask") == 0xFF
        and modes.get("raw_value") == raw_moder
        and modes.get("masked_selector_modes") == 0x55
        and modes.get("expected_selector_output_modes") == 0x55
        and modes.get("passed") is True
        and modes_before.get("register") == "GPIOA_MODER"
        and modes_before.get("address") == one_hot_runner.GPIOA_ODR_ADDRESS - 0x14
        and modes_before.get("raw_value") == raw_moder_before
        and modes_before.get("preserved_non_selector_mask") == 0xFFFFFF00
        and modes_before.get("desired_value") == desired_moder
        and raw_moder == desired_moder
        and returncodes.get("moder_read") == 0
        and returncodes.get("all_off_and_mode_write") == 0
        and value.get("error") is None
    )


def _failure_safety_cleanup(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    capture_index: int | None,
    original_error: BaseException,
    selector_image_admission: Mapping[str, Any] | None,
    mute_boundary: MuteBoundary,
    selector_boundary: StaticSelectorBoundary,
    fast20_cleanup_boundary: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Best-effort mute and, only after image admission, force ALL_OFF."""

    serial = str(contract.get("serial"))
    selector_image_admitted = _target_image_admission_binding_passed(
        selector_image_admission,
        contract=contract,
    )
    mute = leakage_runner._call_mute(mute_boundary, serial, "failure_cleanup")
    mute_passed = leakage_runner._mute_passed(mute, serial=serial, purpose="failure_cleanup")
    selector_cleanup: dict[str, Any] | None = None
    selector_cleanup_passed: bool | None = None
    if selector_image_admitted:
        control = contract.get("selector_control")
        try:
            if not isinstance(control, Mapping):
                raise SelectedStateRunError("failure cleanup lacks selector control binding")
            if contract.get("mode") == "static-bench":
                static_states = one_hot_runner._state_map(control)
                all_off_code = int(static_states["ALL_OFF"])
                selector_cleanup = one_hot_runner._call_selector(
                    selector_boundary,
                    control,
                    "ALL_OFF",
                    all_off_code,
                    "final_cleanup_all_off",
                )
                selector_cleanup_passed = one_hot_runner._selector_passed(
                    selector_cleanup,
                    selector_control=control,
                    state_name="ALL_OFF",
                    state_code=all_off_code,
                    purpose="final_cleanup_all_off",
                )
            else:
                fast20_states = control.get("state_codes")
                if not isinstance(fast20_states, Mapping) or isinstance(
                    fast20_states.get("ALL_OFF"), bool
                ):
                    raise SelectedStateRunError("failure cleanup lacks exact ALL_OFF state")
                suffix = "session" if capture_index is None else f"capture-{capture_index:02d}"
                selector_cleanup = fast20_cleanup_boundary(
                    control,
                    evidence_root=plan_path.parent / "failure-safety-live-evidence" / suffix,
                )
                selector_cleanup_passed = _fast20_failure_cleanup_passed(
                    selector_cleanup,
                    selector_control=control,
                )
        except BaseException as cleanup_error:
            selector_cleanup = {
                "schema": 1,
                "evidence_kind": "selector_failure_cleanup_exception_v1",
                "status": "failed",
                "error": _error_document(cleanup_error),
            }
            selector_cleanup_passed = False
    cleanup_passed = mute_passed and (
        selector_cleanup_passed is True if selector_image_admitted else True
    )
    return {
        "schema": 1,
        "evidence_kind": FAILURE_SAFETY_EVIDENCE_KIND,
        "failed_at": _now(),
        "run_id": contract.get("run_id"),
        "mode": contract.get("mode"),
        "capture_index": capture_index,
        "original_error": _error_document(original_error),
        "exact_mute": mute,
        "exact_mute_passed": mute_passed,
        "selector_image_and_uid_admitted": selector_image_admitted,
        "selector_image_admission": (
            dict(selector_image_admission) if selector_image_admission is not None else None
        ),
        "selector_image_admission_sha256": (
            canonical_sha256(dict(selector_image_admission))
            if selector_image_admission is not None
            else None
        ),
        "selector_cleanup_attempted": selector_image_admitted,
        "selector_cleanup": selector_cleanup,
        "selector_cleanup_passed": selector_cleanup_passed,
        "cleanup_passed": cleanup_passed,
        "artifacts_accepted": False,
    }


def _artifact_evidence(artifact: ArtifactSummary) -> dict[str, Any]:
    root = Path(artifact.path)
    _assert_no_symlink_chain(root, "capture artifact directory")
    _assert_local_rpi_storage(root)
    raw = data_path(artifact)
    metadata = root / f"{artifact.artifact_id}.sigmf-meta"
    return {
        "artifact_id": artifact.artifact_id,
        "data_path": str(raw),
        "data_sha256": sha256_path(raw),
        "data_size_bytes": raw.stat().st_size,
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
        "metadata_size_bytes": metadata.stat().st_size,
    }


def _capture_binding_document(
    *,
    contract: Mapping[str, Any],
    plan_sha: str,
    capture_run_id: str,
    stream_id: int,
    artifact: ArtifactSummary,
    artifact_evidence: Mapping[str, Any],
    record_path: Path,
) -> dict[str, Any]:
    fixture = contract["fixture"]
    selector = contract["selector"]
    assert isinstance(fixture, Mapping) and isinstance(selector, Mapping)
    return {
        "run_id": capture_run_id,
        "stream_id": str(stream_id),
        "artifact_id": artifact.artifact_id,
        "raw_iq_path": artifact_evidence["data_path"],
        "raw_iq_sha256": artifact_evidence["data_sha256"],
        "raw_iq_size_bytes": artifact_evidence["data_size_bytes"],
        "metadata_path": artifact_evidence["metadata_path"],
        "metadata_sha256": artifact_evidence["metadata_sha256"],
        "metadata_size_bytes": artifact_evidence["metadata_size_bytes"],
        "condition_record_path": str(record_path),
        "condition_record_sha256": sha256_path(record_path),
        "condition_record_size_bytes": record_path.stat().st_size,
        "leaf_source_sha256s": [artifact_evidence["data_sha256"]],
        "plan_sha256": plan_sha,
        "fixture_revision_sha256": fixture["fixture_revision_sha256"],
        "selector_evidence_sha256": selector["sha256"],
        "source_commit": contract["source_commit"],
        "dependency_commit": contract["dependency_commit"],
        "native_attestation_sha256": contract["native_attestation_sha256"],
        "device_identity_sha256": contract["device_identity_sha256"],
    }


def _capture_one_stream_impl(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    plan_sha: str,
    capture_index: int,
    state: str | None,
    forbidden_stream_ids: set[int],
    capture_boundary: CaptureBoundary,
    mute_boundary: MuteBoundary,
    identity_boundary: IdentityBoundary,
    selector_boundary: StaticSelectorBoundary,
    target_image_boundary: Callable[..., dict[str, Any]],
    fast20_cleanup_boundary: Callable[..., dict[str, Any]],
    safety_state: dict[str, Any],
) -> dict[str, Any]:
    mode = contract.get("mode")
    assert mode in {"static-bench", "fast20-timing", "fast20-matrix"}
    capture_root = Path(str(contract["capture_root"]))
    capture_root = _ensure_local_directory(capture_root, "capture root")
    safety_state.setdefault("selector_image_admitted", False)
    safety_state.setdefault("selector_image_admission", None)
    initial_mute = leakage_runner._call_mute(mute_boundary, str(contract["serial"]), "pre_capture")
    if not leakage_runner._mute_passed(
        initial_mute, serial=str(contract["serial"]), purpose="pre_capture"
    ):
        raise SelectedStateRunError("pre-capture exact-radio mute failed")
    identity = leakage_runner._call_identity(
        identity_boundary, str(contract["serial"]), str(contract["uri"])
    )
    if not leakage_runner._identity_passed(
        identity,
        serial=str(contract["serial"]),
        requested_uri=str(contract["uri"]),
    ):
        raise SelectedStateRunError("exact serial/current USB URI preflight failed")
    image_root = plan_path.parent / "selector-live-evidence" / f"capture-{capture_index:02d}"
    target_image = target_image_boundary(
        contract,
        evidence_root=image_root,
        admission_state=safety_state,
    )
    returned_admission = _target_image_admission_binding(target_image, contract=contract)
    safety_state["selector_image_admission"] = returned_admission
    safety_state["selector_image_admitted"] = isinstance(
        safety_state.get("selector_image_admission"), Mapping
    )
    if not _target_image_preflight_passed(target_image, contract=contract):
        raise SelectedStateRunError("exact live selector-image preflight failed")

    control = contract.get("selector_control")
    if not isinstance(control, Mapping):
        raise SelectedStateRunError("selector control binding is missing")
    selector_before: dict[str, Any] | None = None
    selector_after: dict[str, Any] | None = None
    selector_cleanup: dict[str, Any] | None = None
    if mode == "static-bench":
        assert state is not None
        state_codes = one_hot_runner._state_map(control)
        code = int(state_codes[state])
        selector_before = one_hot_runner._call_selector(
            selector_boundary, control, state, code, "before_condition"
        )
        if not one_hot_runner._selector_passed(
            selector_before,
            selector_control=control,
            state_name=state,
            state_code=code,
            purpose="before_condition",
        ):
            raise SelectedStateRunError("static selector pre-capture state readback failed")
    elif state is not None:
        raise SelectedStateRunError("Fast20 capture must not claim a static selector state")

    plan = _tone_plan(contract)
    frame_count, _ = _capture_shape(mode)
    blocks: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        blocks.append(replace(block, samples=block.samples.copy(order="C")))

    capture: Any | None = None
    capture_error: BaseException | None = None
    try:
        capture = capture_boundary(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=frame_count,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        capture_error = error
    final_mute = leakage_runner._call_mute(mute_boundary, str(contract["serial"]), "post_capture")
    if mode == "static-bench":
        assert state is not None
        state_codes = one_hot_runner._state_map(control)
        code = int(state_codes[state])
        selector_after = one_hot_runner._call_selector(
            selector_boundary, control, state, code, "after_pluto_mute"
        )
        selector_cleanup = one_hot_runner._call_selector(
            selector_boundary, control, state, code, "cleanup_all_off"
        )
    else:
        selector_cleanup = fast20_cleanup_boundary(
            control,
            evidence_root=image_root / "success-all-off",
        )
    final_mute_passed = leakage_runner._mute_passed(
        final_mute, serial=str(contract["serial"]), purpose="post_capture"
    )
    selector_passed = True
    if mode == "static-bench":
        assert state is not None and selector_before is not None
        assert selector_after is not None and selector_cleanup is not None
        state_codes = one_hot_runner._state_map(control)
        code = int(state_codes[state])
        selector_passed = (
            one_hot_runner._selector_passed(
                selector_after,
                selector_control=control,
                state_name=state,
                state_code=code,
                purpose="after_pluto_mute",
            )
            and one_hot_runner._selector_hold_command_unchanged(selector_before, selector_after)
            and one_hot_runner._selector_passed(
                selector_cleanup,
                selector_control=control,
                state_name=state,
                state_code=code,
                purpose="cleanup_all_off",
            )
        )
    else:
        selector_passed = _fast20_failure_cleanup_passed(
            selector_cleanup,
            selector_control=control,
        )
    if capture_error is not None or not final_mute_passed or not selector_passed:
        blocks.clear()
        if capture_error is not None:
            raise SelectedStateRunError(
                f"capture failed before artifact acceptance: {capture_error}"
            )
        if not final_mute_passed:
            raise SelectedStateRunError("mandatory post-capture exact-radio mute failed")
        raise SelectedStateRunError("mandatory selector electrical ALL_OFF cleanup failed")
    assert capture is not None
    stream_id, rf_readback = _validate_live_capture(
        capture, blocks, contract=contract, forbidden_stream_ids=forbidden_stream_ids
    )
    writer = CaptureWriter(
        capture_root,
        radio=capture.identity,
        settings=_radio_settings(),
        label=f"T8 {mode} capture {capture_index}",
    )
    artifact: ArtifactSummary | None = None
    try:
        for block in blocks:
            writer.append(block, _radio_settings(), revision=1)
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise SelectedStateRunError("persisted artifact failed SHA-256 verification")
        artifact_evidence = _artifact_evidence(artifact)
        metadata = load_metadata(artifact)
        audit_continuity_metadata(
            metadata,
            expected_total_samples=_capture_shape(mode)[1],
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        capture_run_id = f"{contract['run_id']}-capture-{capture_index:02d}"
        record = {
            "schema": 1,
            "record_kind": CAPTURE_RECORD_KIND,
            "created_at": _now(),
            "mode": mode,
            "run_id": contract["run_id"],
            "capture_run_id": capture_run_id,
            "capture_index": capture_index,
            "state": state,
            "plan_path": str(plan_path),
            "plan_file_sha256": sha256_path(plan_path),
            "plan_contract_sha256": plan_sha,
            "context": _qualification_context(contract, plan_sha),
            "fixture_evidence_sha256": contract["fixture_evidence_sha256"],
            "selector": contract["selector"],
            "identity_preflight": identity,
            "initial_mute": initial_mute,
            "target_image_preflight": target_image,
            "selector_before": selector_before,
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": artifact_evidence,
            "capture": {
                "serial": contract["serial"],
                "uri": contract["uri"],
                "model": contract["device_identity"]["iio_context_facts"]["model"],
                "firmware_version": contract["device_identity"]["iio_context_facts"][
                    "firmware_version"
                ],
                "phy_model": contract["device_identity"]["iio_context_facts"]["phy_model"],
                "center_frequency_hz": CENTER_FREQUENCY_HZ,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "receiver_gain_db": RECEIVER_GAIN_DB,
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": frame_count,
                "sample_count": _capture_shape(mode)[1],
                "kernel_buffers": KERNEL_BUFFERS,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "tone_offset_hz_readback": _tone_readback_hz(rf_readback),
            },
            "rf_readback": rf_readback,
            "final_mute": final_mute,
            "selector_after": selector_after,
            "selector_cleanup": selector_cleanup,
            "raw_artifact_acceptance_requires_independent_reanalysis": True,
        }
        record_path = Path(artifact.path) / CONDITION_RECORD_FILENAME
        _write_new_json(record_path, record)
        return _capture_binding_document(
            contract=contract,
            plan_sha=plan_sha,
            capture_run_id=capture_run_id,
            stream_id=stream_id,
            artifact=artifact,
            artifact_evidence=artifact_evidence,
            record_path=record_path,
        )
    except BaseException as error:
        failed_root = _ensure_local_directory(
            capture_root.parent / ".failed", "failed artifact quarantine"
        )
        if artifact is not None:
            source = Path(artifact.path)
            destination = failed_root / f"{artifact.artifact_id}.failed"
            _assert_no_symlink_chain(
                destination,
                "failed artifact quarantine destination",
                allow_missing=True,
            )
            _assert_local_rpi_storage(destination)
            if destination.exists() or destination.is_symlink():
                raise SelectedStateRunError(
                    "failed artifact quarantine destination exists"
                ) from error
            if source.exists():
                os.replace(source, destination)
                _assert_no_symlink_chain(destination, "failed artifact quarantine destination")
                _assert_local_rpi_storage(destination)
        else:
            with suppress(BaseException):
                writer.fail(error)
        raise
    finally:
        blocks.clear()


def _capture_one_stream(
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    plan_sha: str,
    capture_index: int,
    state: str | None,
    forbidden_stream_ids: set[int],
    capture_boundary: CaptureBoundary,
    mute_boundary: MuteBoundary,
    identity_boundary: IdentityBoundary,
    selector_boundary: StaticSelectorBoundary,
    target_image_boundary: Callable[..., dict[str, Any]],
    safety_state: dict[str, Any] | None = None,
    fast20_cleanup_boundary: Callable[..., dict[str, Any]] = _live_fast20_all_off_cleanup,
) -> dict[str, Any]:
    """Run one stream and leave immutable evidence for every failure cleanup."""

    current_safety_state = safety_state if safety_state is not None else {}
    current_safety_state.setdefault("selector_image_admitted", False)
    current_safety_state.setdefault("selector_image_admission", None)
    try:
        return _capture_one_stream_impl(
            contract=contract,
            plan_path=plan_path,
            plan_sha=plan_sha,
            capture_index=capture_index,
            state=state,
            forbidden_stream_ids=forbidden_stream_ids,
            capture_boundary=capture_boundary,
            mute_boundary=mute_boundary,
            identity_boundary=identity_boundary,
            selector_boundary=selector_boundary,
            target_image_boundary=target_image_boundary,
            fast20_cleanup_boundary=fast20_cleanup_boundary,
            safety_state=current_safety_state,
        )
    except BaseException as error:
        cleanup = _failure_safety_cleanup(
            contract=contract,
            plan_path=plan_path,
            capture_index=capture_index,
            original_error=error,
            selector_image_admission=(
                current_safety_state.get("selector_image_admission")
                if isinstance(current_safety_state.get("selector_image_admission"), Mapping)
                else None
            ),
            mute_boundary=mute_boundary,
            selector_boundary=selector_boundary,
            fast20_cleanup_boundary=fast20_cleanup_boundary,
        )
        evidence_path = plan_path.parent / f"capture-{capture_index:02d}.failure-safety.json"
        write_error: BaseException | None = None
        try:
            _write_new_json(evidence_path, cleanup)
        except BaseException as cleanup_write_error:
            write_error = cleanup_write_error
        details: list[str] = []
        if cleanup.get("cleanup_passed") is not True:
            details.append("mandatory failure mute/ALL_OFF cleanup was not proven")
        if write_error is not None:
            details.append(f"failure cleanup evidence could not be sealed: {write_error}")
        suffix = f"; {'; '.join(details)}" if details else ""
        raise SelectedStateRunError(f"{error}{suffix}") from error


def _assert_capture_run_unburned(run_directory: Path) -> None:
    """Reject replay before revalidation, mute, identity, OpenOCD, or RF access."""

    exact = _ensure_local_directory(run_directory, "capture run directory")
    preparation_files = {PLAN_FILENAME, FIXTURE_BINDING_FILENAME}
    run_derived = sorted(
        entry.name for entry in exact.iterdir() if entry.name not in preparation_files
    )
    if run_derived:
        raise SelectedStateRunError(
            "capture run ID is already burned by run-derived artifacts: " + ", ".join(run_derived)
        )


def execute_capture(
    *,
    plan_path: Path,
    runtime_bindings: Mapping[str, Any] | None = None,
    selector_loader: Callable[..., SelectorEvidenceBinding] = selector_binding_from_sealed,
    fixture_evidence_loader: Callable[..., dict[str, Any]] = _fixture_evidence_from_inputs,
    selector_control_builder: Callable[..., dict[str, Any] | None] = (_selector_control_from_files),
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = leakage_runner._strict_mute,
    identity_boundary: IdentityBoundary = leakage_runner._live_identity_boundary,
    selector_boundary: StaticSelectorBoundary = one_hot_runner._live_selector_boundary,
    target_image_boundary: Callable[..., dict[str, Any]] = _live_target_image_preflight,
    fast20_cleanup_boundary: Callable[..., dict[str, Any]] = _live_fast20_all_off_cleanup,
    now: Callable[[], str] = _now,
) -> Path:
    """Execute the sole hardware-capable action and publish one raw capture set."""

    exact_plan = plan_path.expanduser().absolute()
    contract, plan_sha = _load_plan(exact_plan)
    run_directory = exact_plan.parent
    execution_path = run_directory / EXECUTION_TOMBSTONE_FILENAME
    failure_path = run_directory / FAILURE_TOMBSTONE_FILENAME
    evidence_path = run_directory / CAPTURE_EVIDENCE_FILENAME
    _assert_capture_run_unburned(run_directory)
    capture_started_at = now()
    try:
        _write_new_json(
            execution_path,
            {
                "schema": 1,
                "evidence_kind": "5g8_selected_state_capture_started_v1",
                "started_at": capture_started_at,
                "run_id": contract["run_id"],
                "mode": contract["mode"],
                "plan_path": str(exact_plan),
                "plan_file_sha256": sha256_path(exact_plan),
                "plan_contract_sha256": plan_sha,
                "expected_capture_set_path": str(evidence_path),
                "run_id_burned": True,
                "hardware_access_authorized_only_for_this_action": True,
            },
        )
    except FileExistsError as error:
        raise SelectedStateRunError("capture run ID was concurrently burned") from error
    safety_state: dict[str, Any] = {
        "selector_image_admitted": False,
        "selector_image_admission": None,
    }
    try:
        _revalidate_plan_inputs(
            contract,
            runtime_bindings=runtime_bindings,
            selector_loader=selector_loader,
            fixture_evidence_loader=fixture_evidence_loader,
            selector_control_builder=selector_control_builder,
            device_identity_reference_time=capture_started_at,
        )
        mode = contract["mode"]
        states: Sequence[str | None]
        if mode == "static-bench":
            states = EXPECTED_STATES
        else:
            states = (None,) * _expected_capture_count(mode)
        bindings: list[dict[str, Any]] = []
        streams: set[int] = set()
        for capture_index, state in enumerate(states, start=1):
            binding = _capture_one_stream(
                contract=contract,
                plan_path=exact_plan,
                plan_sha=plan_sha,
                capture_index=capture_index,
                state=state,
                forbidden_stream_ids=streams,
                capture_boundary=capture_boundary,
                mute_boundary=mute_boundary,
                identity_boundary=identity_boundary,
                selector_boundary=selector_boundary,
                target_image_boundary=target_image_boundary,
                safety_state=safety_state,
                fast20_cleanup_boundary=fast20_cleanup_boundary,
            )
            streams.add(int(binding["stream_id"]))
            bindings.append(binding)
        capture_set = {
            "schema": 1,
            "evidence_kind": "5g8_selected_state_raw_capture_set_v1",
            "mode": mode,
            "context": _qualification_context(contract, plan_sha),
            "captures": bindings,
        }
        # Reopen/recompute before publishing the capture-set authority.  This is
        # not a qualification result; analyze repeats the same admission later.
        _reanalyze_capture_evidence(capture_set, contract=contract, plan_sha=plan_sha)
        _write_new_json(evidence_path, capture_set)
        return evidence_path
    except BaseException as error:
        session_cleanup = _failure_safety_cleanup(
            contract=contract,
            plan_path=exact_plan,
            capture_index=None,
            original_error=error,
            selector_image_admission=(
                safety_state.get("selector_image_admission")
                if isinstance(safety_state.get("selector_image_admission"), Mapping)
                else None
            ),
            mute_boundary=mute_boundary,
            selector_boundary=selector_boundary,
            fast20_cleanup_boundary=fast20_cleanup_boundary,
        )
        session_cleanup_path = run_directory / "session.failure-safety.json"
        session_cleanup_evidence: dict[str, Any] | None = None
        with suppress(BaseException):
            _write_new_json(session_cleanup_path, session_cleanup)
            session_cleanup_evidence = _file_evidence(
                session_cleanup_path, "session failure safety cleanup"
            )
        capture_cleanup_evidence = [
            _file_evidence(path, "capture failure safety cleanup")
            for path in sorted(run_directory.glob("capture-*.failure-safety.json"))
            if path.is_file() and not path.is_symlink()
        ]
        final_failure_cleanup_passed = (
            session_cleanup.get("cleanup_passed") is True and session_cleanup_evidence is not None
        )
        with suppress(BaseException):
            _write_new_json(
                failure_path,
                {
                    "schema": 1,
                    "evidence_kind": "5g8_selected_state_failed_run_v1",
                    "failed_at": now(),
                    "run_id": contract["run_id"],
                    "mode": contract["mode"],
                    "plan_contract_sha256": plan_sha,
                    "execution_tombstone_sha256": sha256_path(execution_path),
                    "error": _error_document(error),
                    "failure_safety_evidence": {
                        "session": session_cleanup_evidence,
                        "captures": capture_cleanup_evidence,
                    },
                    "final_failure_cleanup_passed": (final_failure_cleanup_passed),
                    "artifacts_accepted": False,
                    "run_id_burned": True,
                    "automatic_retry_attempted": False,
                },
            )
        cleanup_suffix = (
            ""
            if final_failure_cleanup_passed
            else "; mandatory final failure mute/ALL_OFF cleanup was not proven"
        )
        if isinstance(error, (SelectedStateRunError, SelectedStateQualificationError)):
            raise SelectedStateRunError(f"{error}{cleanup_suffix}") from error
        raise SelectedStateRunError(f"capture failed: {error}{cleanup_suffix}") from error


def execute_qualification(
    *,
    plan_path: Path,
    evidence_path: Path,
    runtime_bindings: Mapping[str, Any] | None = None,
    selector_loader: Callable[..., SelectorEvidenceBinding] = selector_binding_from_sealed,
    fixture_evidence_loader: Callable[..., dict[str, Any]] = _fixture_evidence_from_inputs,
    selector_control_builder: Callable[..., dict[str, Any] | None] = (_selector_control_from_files),
    now: Callable[[], str] = _now,
    bootstrap_draws: int = 32_768,
) -> Path:
    """RF-inertly re-open and qualify only this runner's completed capture set."""

    contract, plan_sha = _load_plan(plan_path.expanduser().absolute())
    mode = contract.get("mode")
    if mode not in {"static-bench", "fast20-timing", "fast20-matrix"}:
        raise SelectedStateRunError("qualification plan mode is invalid")
    run_directory = plan_path.expanduser().absolute().parent
    execution_path = run_directory / EXECUTION_TOMBSTONE_FILENAME
    analysis_path = run_directory / ANALYSIS_TOMBSTONE_FILENAME
    failure_path = run_directory / FAILURE_TOMBSTONE_FILENAME
    result_path = run_directory / RESULT_FILENAME
    if failure_path.exists():
        raise SelectedStateRunError("failed capture run IDs cannot enter analysis")
    execution = _read_json(execution_path, "capture execution tombstone", canonical=True)
    exact_evidence_path = evidence_path.expanduser().absolute()
    if (
        execution.get("schema") != 1
        or execution.get("evidence_kind") != "5g8_selected_state_capture_started_v1"
        or execution.get("run_id") != contract.get("run_id")
        or execution.get("mode") != mode
        or execution.get("plan_path") != str(plan_path.expanduser().absolute())
        or execution.get("plan_file_sha256") != sha256_path(plan_path.expanduser().absolute())
        or execution.get("plan_contract_sha256") != plan_sha
        or execution.get("expected_capture_set_path") != str(exact_evidence_path)
        or execution.get("run_id_burned") is not True
    ):
        raise SelectedStateRunError("analysis lacks this runner's exact capture-start tombstone")
    evidence_file = _file_evidence(evidence_path, "qualification evidence")
    _write_new_json(
        analysis_path,
        {
            "schema": 1,
            "evidence_kind": "5g8_selected_state_analysis_started_v1",
            "started_at": now(),
            "run_id": contract.get("run_id"),
            "mode": mode,
            "plan_path": str(plan_path.expanduser().absolute()),
            "plan_file_sha256": sha256_path(plan_path.expanduser().absolute()),
            "plan_contract_sha256": plan_sha,
            "qualification_input": evidence_file,
            "capture_execution_tombstone_sha256": sha256_path(execution_path),
            "hardware_access_permitted": False,
            "artifact_acceptance_pending": True,
        },
    )
    try:
        fixture, selector = _revalidate_plan_inputs(
            contract,
            runtime_bindings=runtime_bindings,
            selector_loader=selector_loader,
            fixture_evidence_loader=fixture_evidence_loader,
            selector_control_builder=selector_control_builder,
        )
        capture_set = _read_json(evidence_path, "raw capture-set evidence")
        evidence = _reanalyze_capture_evidence(
            capture_set,
            contract=contract,
            plan_sha=plan_sha,
        )
        expected_context = _qualification_context(contract, plan_sha)
        if evidence.get("context") != expected_context:
            raise SelectedStateRunError(
                "qualification evidence context differs from immutable plan"
            )
        result: dict[str, Any]
        release: dict[str, Any] | None = None
        if mode == "static-bench":
            result = qualify_static_bench(evidence, fixture=fixture, selector=selector)
        elif mode == "fast20-timing":
            result = qualify_fast20_timing(evidence, fixture=fixture, selector=selector)
        else:
            prior = contract.get("prior_qualification_files")
            if not isinstance(prior, Mapping) or set(prior) != {
                "intervention_contract",
                "static_result",
                "timing_result",
            }:
                raise SelectedStateRunError("matrix plan lacks exact prerequisite bindings")
            prior_files = {
                name: _validate_file_evidence(prior[name], f"matrix prior {name}")
                for name in ("intervention_contract", "static_result", "timing_result")
            }
            intervention = validate_intervention_contract(
                _read_json(
                    Path(prior_files["intervention_contract"]["path"]),
                    "intervention contract",
                ),
                fixture=fixture,
            )
            static_result = _reanalyze_prior_result(
                prior_files["static_result"],
                expected_mode="static-bench",
                expected_kind=STATIC_RESULT_KIND,
                label="static result",
                current_contract=contract,
                current_fixture=fixture,
                runtime_bindings=runtime_bindings,
                selector_loader=selector_loader,
                fixture_evidence_loader=fixture_evidence_loader,
                selector_control_builder=selector_control_builder,
            )
            timing_result = _reanalyze_prior_result(
                prior_files["timing_result"],
                expected_mode="fast20-timing",
                expected_kind=TIMING_RESULT_KIND,
                label="timing result",
                current_contract=contract,
                current_fixture=fixture,
                runtime_bindings=runtime_bindings,
                selector_loader=selector_loader,
                fixture_evidence_loader=fixture_evidence_loader,
                selector_control_builder=selector_control_builder,
            )
            forbidden_streams = [
                *intervention.baseline_stream_ids,
                *intervention.intervention_stream_ids,
                *static_result["source_stream_ids"],
                *timing_result["source_stream_ids"],
            ]
            forbidden_raw = [
                *intervention.baseline_raw_iq_sha256s,
                *intervention.intervention_raw_iq_sha256s,
                *static_result["raw_iq_sha256s"],
                *timing_result["raw_iq_sha256s"],
            ]
            result = qualify_fast20_matrix(
                evidence,
                fixture=fixture,
                selector=selector,
                forbidden_stream_ids=forbidden_streams,
                forbidden_raw_iq_sha256s=forbidden_raw,
                bootstrap_draws=bootstrap_draws,
            )
            release = qualify_selected_state_release(
                intervention=intervention,
                static_result=static_result,
                timing_result=timing_result,
                matrix_result=result,
            )
        accepted = result.get("accepted") is True
        if release is not None:
            accepted = release.get("operational_coefficient_release_allowed") is True
            if contract.get("require_one_degree") is True:
                accepted = (
                    accepted and release.get("one_degree_coefficient_release_allowed") is True
                )
        output = {
            **result,
            "qualification_input": evidence_file,
            "execution_tombstone": _file_evidence(execution_path, "execution tombstone"),
            "analysis_tombstone": _file_evidence(analysis_path, "analysis tombstone"),
            "release": release,
            "result_accepted": accepted,
        }
        _write_new_json(result_path, output)
        if not accepted:
            raise SelectedStateRunError(
                "qualification gates rejected evidence; see immutable qualification result"
            )
        return result_path
    except BaseException as error:
        with suppress(BaseException):
            _write_new_json(
                failure_path,
                {
                    "schema": 1,
                    "evidence_kind": "5g8_selected_state_failed_run_v1",
                    "failed_at": now(),
                    "run_id": contract.get("run_id"),
                    "mode": mode,
                    "plan_contract_sha256": plan_sha,
                    "execution_tombstone_sha256": sha256_path(execution_path),
                    "analysis_tombstone_sha256": sha256_path(analysis_path),
                    "qualification_input": evidence_file,
                    "error": _error_document(error),
                    "artifacts_accepted": False,
                    "run_id_burned": True,
                    "final_cleanup_evidence_recomputed_from_condition_records": True,
                },
            )
        if isinstance(error, (SelectedStateRunError, SelectedStateQualificationError)):
            raise SelectedStateRunError(str(error)) from error
        raise SelectedStateRunError(f"qualification failed: {error}") from error


def _add_mode_parser(subparsers: Any, mode: Mode) -> None:
    parser = subparsers.add_parser(mode)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--campaign-id", required=True)
    prepare.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    prepare.add_argument("--serial", default=DEFAULT_SERIAL)
    prepare.add_argument("--uri", required=True)
    prepare.add_argument("--fixture-manifest", type=Path, required=True)
    prepare.add_argument("--setup-attestation", type=Path, required=True)
    prepare.add_argument("--selector-evidence", type=Path, required=True)
    prepare.add_argument("--selector-evidence-sha256", required=True)
    prepare.add_argument("--selector-run-id", required=True)
    prepare.add_argument("--device-identity", type=Path, required=True)
    prepare.add_argument("--profile", type=Path, required=True)
    prepare.add_argument("--openocd-config", type=Path, required=True)
    if mode == "static-bench":
        prepare.add_argument("--bench-manifest", type=Path, required=True)
    prepare.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    if mode == "fast20-matrix":
        prepare.add_argument("--intervention-contract", type=Path, required=True)
        prepare.add_argument("--static-result", type=Path, required=True)
        prepare.add_argument("--timing-result", type=Path, required=True)
        prepare.add_argument("--require-one-degree", action="store_true")
    capture = actions.add_parser("capture")
    capture.add_argument("--plan", type=Path, required=True)
    analyze = actions.add_parser("analyze")
    analyze.add_argument("--plan", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("static-bench", "fast20-timing", "fast20-matrix"):
        _add_mode_parser(modes, mode)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            path = build_plan(
                mode=args.mode,
                run_id=args.run_id,
                campaign_id=args.campaign_id,
                board_id=args.board_id,
                serial=args.serial,
                uri=args.uri,
                selector_evidence_path=args.selector_evidence,
                selector_evidence_sha256=args.selector_evidence_sha256,
                selector_run_id=args.selector_run_id,
                device_identity_path=args.device_identity,
                state_root=args.state_root,
                fixture_manifest_path=args.fixture_manifest,
                setup_attestation_path=args.setup_attestation,
                profile_path=args.profile,
                openocd_config_path=args.openocd_config,
                bench_manifest_path=getattr(args, "bench_manifest", None),
                intervention_contract_path=getattr(args, "intervention_contract", None),
                static_result_path=getattr(args, "static_result", None),
                timing_result_path=getattr(args, "timing_result", None),
                require_one_degree=getattr(args, "require_one_degree", False),
            )
        elif args.action == "capture":
            path = execute_capture(plan_path=args.plan)
        else:
            path = execute_qualification(
                plan_path=args.plan,
                evidence_path=args.plan.expanduser().absolute().parent / CAPTURE_EVIDENCE_FILENAME,
            )
        print(path)
        return 0
    except (OSError, SelectedStateRunError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
