#!/usr/bin/env python3
"""Run one manually confirmed 5.8 GHz coherent-leakage topology stage."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_SMATEWAY_SOURCE = Path(__file__).resolve().parents[1] / "src"
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
    prior_loader_path = environment.get("LD_LIBRARY_PATH", "")
    loader_entries = [
        item
        for item in prior_loader_path.split(os.pathsep)
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
    load_metadata,
    verify_artifact,
)
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import (
    SafeDdsTonePlan,
    SampleBlockV2,
    capture_continuous_safe_dds_tone,
)
from pluto_plus.hardware.iio import find_usb_sysfs_path, resolve_iio_uri
from pluto_plus.models import ArtifactSummary, GainMode, RadioSettings

from smateway.bench import BenchManifest, OpenOcdBench
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.capture_continuity import validate_continuity_ledger
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway import native_iio_attestation as _native_iio_attestation
from smateway.native_iio_attestation import (
    RuntimeAttestationBoundary,
    attest_runtime as _native_libiio_runtime_attestation,
    call_runtime_preflight as _shared_call_runtime_preflight,
    runtime_preflight_passed as _shared_runtime_preflight_passed,
    validate_runtime_attestation as _validate_native_libiio_runtime_attestation,
)
from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.profile import load_profile
from smateway.rf_policy import EXPERIMENTAL_5G8_CENTER_HZ, classify_fast20_center_frequency

REQUIRED_LIBIIO_DIRECTORY = _native_iio_attestation.REQUIRED_LIBIIO_DIRECTORY
REQUIRED_LIBIIO_PATH = _native_iio_attestation.REQUIRED_LIBIIO_PATH
REQUIRED_LIBIIO_SHA256 = _native_iio_attestation.REQUIRED_LIBIIO_SHA256
REQUIRED_LIBIIO_SYMBOLS = _native_iio_attestation.REQUIRED_LIBIIO_SYMBOLS
REQUIRED_LIBIIO_VERSION = _native_iio_attestation.REQUIRED_LIBIIO_VERSION

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
CENTER_FREQUENCY_HZ = EXPERIMENTAL_5G8_CENTER_HZ
TONE_OFFSET_HZ = 100_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 3
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
RECEIVER_GAIN_DB = 60
TX_HARDWARE_GAINS_DB = (-35.0, -30.0, -25.0, -20.0, -15.0, -10.0)
ATTRIBUTION_GAIN_DB = -20.0
ATTRIBUTION_REPEAT_COUNT = 5
DDS_SCALE = 0.125
MINIMUM_PILOT_CONFIDENCE = 0.25
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.995
MAXIMUM_PILOT_PHASE_RMS_DEG = 6.0
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
LOAD_INPUT_LIMIT_DBM = 0.0
REQUIRED_MARGIN_DB = 10.0
PATH_ATTENUATION_BEFORE_LOAD_DB = 0.0
CONDITION_RECORD_NAME = "5g8-leakage-condition.json"
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
FIXTURE_KIND_V2 = "5g8_general_topology_stage_fixture"
SETUP_ATTESTATION_KIND = "5g8_general_topology_run_setup"
ANTENNA_PORTS = tuple(f"ANT{index}" for index in range(1, 9))
SHARED_CONNECTION_ROLES = (
    "tx1_to_splitter",
    "splitter_to_rx1_attenuator",
    "rx1_attenuator_to_rx1",
    "tx2_to_termination",
)
PRIOR_STAGE: dict[str, str | None] = {
    "direct_rx2_termination": None,
    "rx2_cable_terminated": "direct_rx2_termination",
    "powered_selector_all_inputs_terminated": "rx2_cable_terminated",
    "full_conducted_fixture": "powered_selector_all_inputs_terminated",
}

STAGE_CONTRACTS: dict[str, dict[str, Any]] = {
    "direct_rx2_termination": {
        "order": 0,
        "confirmation_token": "DIRECT_RX2_50OHM_AT_PLUTO",
        "rx2_topology": "5.8 GHz 50 ohm termination directly on Pluto RX2",
        "selector_topology": "selector and RX2 cable disconnected",
        "selector_state_contract": (
            "selector RF disconnected, bench power off, and control/ground harness disconnected; "
            "controller forbidden"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "rx2_cable_terminated": {
        "order": 1,
        "confirmation_token": "RX2_CABLE_FAR_END_50OHM",
        "rx2_topology": "test cable on Pluto RX2 with a 5.8 GHz 50 ohm far-end termination",
        "selector_topology": "selector disconnected from the RX2 test cable",
        "selector_state_contract": (
            "selector RF disconnected, bench power off, and control/ground harness disconnected; "
            "controller forbidden"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "powered_selector_all_inputs_terminated": {
        "order": 2,
        "confirmation_token": "POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM",
        "rx2_topology": "RX2 test cable connects Pluto RX2 to the selector common port",
        "selector_topology": (
            "selector is powered and all eight ANT input ports have 5.8 GHz 50 ohm terminations"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "full_conducted_fixture": {
        "order": 3,
        "confirmation_token": "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
        "rx2_topology": "selector common connects through the fixed test cable to Pluto RX2",
        "selector_topology": (
            "TX1 two-way branch feeds the 2-8 GHz eight-way splitter and all eight selector "
            "ANT inputs; the other attenuated two-way branch feeds RX1"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": "RX1 is the attenuated conducted reference branch",
    },
}
STAGES = tuple(STAGE_CONTRACTS)
SELECTOR_CONNECTED_STAGES = frozenset(
    {"powered_selector_all_inputs_terminated", "full_conducted_fixture"}
)
SELECTOR_READ_ONLY_PURPOSES = frozenset({"initial_state_before_command", "after_condition"})
SELECTOR_COMMAND_PURPOSES = frozenset({"before_condition"})
SELECTOR_CLEANUP_PURPOSES = frozenset(
    {
        "condition_cleanup_all_off",
        "final_cleanup_all_off",
        "exception_cleanup_all_off",
        "resume_cleanup_all_off",
    }
)


class LeakageLadderError(RuntimeError):
    """A frozen plan, live safety, continuity, or artifact invariant failed."""


class ConditionCaptureFailure(LeakageLadderError):
    """One condition failed after preserving an explicit quarantine."""

    def __init__(
        self,
        message: str,
        *,
        quarantine: Mapping[str, Any],
        post_mute: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.quarantine = dict(quarantine)
        self.post_mute = dict(post_mute)


class CaptureBoundary(Protocol):
    """Injectable seam around the sole live RF helper."""

    def __call__(
        self,
        plan: SafeDdsTonePlan,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Callable[[SampleBlockV2], None],
    ) -> Any: ...


MuteBoundary = Callable[[str, str], dict[str, Any]]


class IdentityBoundary(Protocol):
    """Injectable read-only identity scan before any RF helper is called."""

    def __call__(self, serial: str, requested_uri: str) -> dict[str, Any]: ...


class FixtureEvidenceBoundary(Protocol):
    """Injectable revalidation of the frozen physical-fixture evidence."""

    def __call__(self, fixture_evidence: Mapping[str, Any]) -> dict[str, Any]: ...


class SelectorBoundary(Protocol):
    """Injectable static ALL_OFF command plus mailbox-readback boundary."""

    def __call__(
        self,
        selector_control: Mapping[str, Any],
        purpose: str,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _validate_serial(value: str) -> str:
    if not value or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("serial must be a non-empty exact device identifier")
    return value


def _validate_usb_uri(value: str) -> str:
    if USB_URI.fullmatch(value) is None:
        raise ValueError("URI must be an exact current usb: bus.address IIO URI")
    return value


def _validate_commit(value: str, label: str) -> str:
    if GIT_COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return value


def _validate_sha256(value: object, label: str) -> str:
    digest = str(value)
    if SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically publish one durable read-only JSON file without overwrite."""

    _assert_path_chain_has_no_symlink(path.parent, label="immutable JSON parent")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o400)
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
        _fsync_directory(path.parent)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LeakageLadderError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise LeakageLadderError(f"{label} root must be an object")
    return value


def _board_root(board_id: str) -> Path:
    return Path.home() / ".local/state/smateway/boards" / board_id


def _selector_lock_root() -> Path:
    return Path.home() / ".local/state/smateway/hardware-locks" / "pluto-rx2-8way-selector-bench"


@contextmanager
def _board_lock(board_root: Path) -> Iterator[None]:
    board_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = board_root / ".bench.lock"
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LeakageLadderError(f"board bench lock is already held: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _repository_commit_and_require_clean(repository: Path, label: str) -> str:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _validate_commit(head, f"{label} commit")
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise LeakageLadderError(f"{label} source tree must be clean before freezing an RF plan")
    return head


def _validate_dependency_source_attestation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and freeze the complete source/import attestation used at runtime."""

    document = _json_safe(dict(value))
    if not isinstance(document, dict):
        raise ValueError("pluto-plus-utils source attestation must be an object")
    commit = str(document.get("commit", ""))
    _validate_commit(commit, "pluto-plus-utils attested commit")
    if (
        document.get("schema") != 1
        or document.get("dependency") != "pluto-plus-utils"
        or document.get("head") != commit
        or document.get("clean_worktree_verified") is not True
        or not isinstance(document.get("files"), list)
        or not document["files"]
        or not isinstance(document.get("imported_modules"), list)
    ):
        raise ValueError("pluto-plus-utils source attestation is incomplete")
    imported = {
        item.get("module") for item in document["imported_modules"] if isinstance(item, Mapping)
    }
    required = {
        "pluto_plus.artifacts",
        "pluto_plus.bootstrap_firmware",
        "pluto_plus.hardware",
        "pluto_plus.hardware.iio",
        "pluto_plus.models",
    }
    if not required.issubset(imported):
        raise ValueError("pluto-plus-utils imported-module attestation is incomplete")
    return document


def _validate_file_evidence(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} evidence must be an object")
    document = _json_safe(dict(value))
    if not isinstance(document, dict):
        raise ValueError(f"{label} evidence must normalize to an object")
    if set(document) != {"path", "sha256", "size_bytes"}:
        raise ValueError(f"{label} evidence fields are incomplete")
    path = Path(str(document["path"]))
    if not path.is_absolute():
        raise ValueError(f"{label} evidence path must be absolute")
    document["path"] = str(path)
    document["sha256"] = _validate_sha256(document["sha256"], f"{label} evidence hash")
    size = document["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"{label} evidence size must be positive")
    return document


def _normalize_characterization(
    value: object,
    *,
    label: str,
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} characterization must be an object")
    document = _json_safe(dict(value))
    if not isinstance(document, dict) or set(document) != {
        "status",
        "evidence_path",
        "evidence_sha256",
        "s_parameter_sha256",
        "return_loss_db_at_5g8",
    }:
        raise ValueError(f"{label} characterization fields are incomplete or unexpected")
    status = document["status"]
    if status not in {"characterized", "uncharacterized"}:
        raise ValueError(f"{label} characterization status is invalid")
    if document["s_parameter_sha256"] is not None:
        document["s_parameter_sha256"] = _validate_sha256(
            document["s_parameter_sha256"], f"{label} S-parameter hash"
        )
    return_loss = document["return_loss_db_at_5g8"]
    if return_loss is not None and (
        isinstance(return_loss, bool)
        or not isinstance(return_loss, (int, float))
        or not math.isfinite(return_loss)
        or float(return_loss) < 0
    ):
        raise ValueError(f"{label} return loss must be a non-negative finite dB value")
    evidence_path = document["evidence_path"]
    evidence_hash = document["evidence_sha256"]
    if status == "uncharacterized":
        if any(
            item is not None
            for item in (
                evidence_path,
                evidence_hash,
                document["s_parameter_sha256"],
                return_loss,
            )
        ):
            raise ValueError(
                f"{label} must use explicit null characterization fields when uncharacterized"
            )
        return document
    if document["s_parameter_sha256"] is None or return_loss is None:
        raise ValueError(
            f"{label} characterized evidence requires an S-parameter hash and 5.8-GHz return loss"
        )
    if not isinstance(evidence_path, str) or not evidence_path:
        raise ValueError(f"{label} characterized evidence requires a file path")
    exact_path = Path(evidence_path).expanduser()
    if not exact_path.is_absolute():
        if base_directory is None:
            raise ValueError(f"{label} characterization evidence path must be absolute")
        exact_path = base_directory / exact_path
    if verify_files:
        exact_path = exact_path.resolve(strict=True)
        if not exact_path.is_file():
            raise LeakageLadderError(f"{label} characterization evidence is not a file")
    else:
        exact_path = exact_path.resolve()
    document["evidence_path"] = str(exact_path)
    document["evidence_sha256"] = _validate_sha256(
        evidence_hash, f"{label} characterization evidence hash"
    )
    if verify_files and sha256_path(exact_path) != document["evidence_sha256"]:
        raise LeakageLadderError(f"{label} characterization evidence hash differs")
    return document


def _normalize_rated_asset(
    value: object,
    *,
    label: str,
    port_names: tuple[str, ...],
    extra_numeric_fields: tuple[str, ...] = (),
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    document = _json_safe(dict(value))
    required = {
        "id",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "port_map",
        "characterization",
        *extra_numeric_fields,
    }
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError(f"{label} fields are incomplete or unexpected")
    document["id"] = _validate_identifier(str(document["id"]), f"{label} ID")
    numeric_fields = {
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        *extra_numeric_fields,
    }
    for field in numeric_fields:
        item = document[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{label} {field} must be a finite number")
    minimum = float(document["rated_min_frequency_hz"])
    maximum = float(document["rated_max_frequency_hz"])
    if minimum < 0 or minimum > CENTER_FREQUENCY_HZ or maximum < CENTER_FREQUENCY_HZ:
        raise ValueError(f"{label} frequency rating must contain 5.8 GHz")
    if maximum <= minimum:
        raise ValueError(f"{label} frequency rating is inverted")
    if float(document["maximum_input_power_dbm"]) < LOAD_INPUT_LIMIT_DBM:
        raise ValueError(f"{label} maximum input rating is below the frozen load limit")
    if "attenuation_db" in extra_numeric_fields and float(document["attenuation_db"]) <= 0:
        raise ValueError(f"{label} attenuation must be positive")
    if "impedance_ohm" in extra_numeric_fields and not math.isclose(
        float(document["impedance_ohm"]), 50.0, rel_tol=0.0, abs_tol=0.01
    ):
        raise ValueError(f"{label} must be rated 50 ohm")
    port_map = document["port_map"]
    if not isinstance(port_map, Mapping) or set(port_map) != set(port_names):
        raise ValueError(f"{label} port map is incomplete or unexpected")
    document["port_map"] = {
        name: _validate_identifier(str(port_map[name]), f"{label} {name} port ID")
        for name in port_names
    }
    if len(set(document["port_map"].values())) != len(document["port_map"]):
        raise ValueError(f"{label} physical port IDs must be unique within its port map")
    document["characterization"] = _normalize_characterization(
        document["characterization"],
        label=label,
        base_directory=base_directory,
        verify_files=verify_files,
    )
    return document


def _normalize_interconnect(
    value: object,
    *,
    label: str,
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} interconnect must be an object")
    document = _json_safe(dict(value))
    if not isinstance(document, dict) or set(document) != {
        "id",
        "kind",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "characterization",
    }:
        raise ValueError(f"{label} interconnect fields are incomplete or unexpected")
    if document["kind"] not in {
        "coaxial_cable",
        "direct_adapter",
        "sma_barrel",
        "integrated_launch",
    }:
        raise ValueError(f"{label} interconnect kind is invalid")
    rated = _normalize_rated_asset(
        {key: value for key, value in document.items() if key not in {"kind"}} | {"port_map": {}},
        label=f"{label} interconnect",
        port_names=(),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    rated["kind"] = document["kind"]
    rated.pop("port_map")
    return rated


def _normalize_endpoint(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"component_id", "port_id"}:
        raise ValueError(f"{label} endpoint is malformed")
    return {
        "component_id": _validate_identifier(str(value["component_id"]), f"{label} component"),
        "port_id": _validate_identifier(str(value["port_id"]), f"{label} port"),
    }


def _normalize_connection(
    value: object,
    *,
    label: str,
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} connection must be an object")
    document = _json_safe(dict(value))
    if not isinstance(document, dict) or set(document) != {"id", "from", "to", "interconnect"}:
        raise ValueError(f"{label} connection fields are incomplete or unexpected")
    document["id"] = _validate_identifier(str(document["id"]), f"{label} connection ID")
    document["from"] = _normalize_endpoint(document["from"], f"{label} source")
    document["to"] = _normalize_endpoint(document["to"], f"{label} destination")
    document["interconnect"] = _normalize_interconnect(
        document["interconnect"],
        label=label,
        base_directory=base_directory,
        verify_files=verify_files,
    )
    return document


def _require_connection(
    connection: Mapping[str, Any],
    *,
    source: tuple[str, str],
    destination: tuple[str, str],
    label: str,
    required_kind: str | None = None,
) -> None:
    if connection.get("from") != {"component_id": source[0], "port_id": source[1]} or (
        connection.get("to") != {"component_id": destination[0], "port_id": destination[1]}
    ):
        raise ValueError(f"{label} endpoints differ from the frozen port-level graph")
    interconnect = connection.get("interconnect")
    if required_kind is not None and (
        not isinstance(interconnect, Mapping) or interconnect.get("kind") != required_kind
    ):
        raise ValueError(f"{label} requires a {required_kind} interconnect")


def _normalize_pluto(value: object, *, expected_serial: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"id", "serial", "port_map"}:
        raise ValueError("shared Pluto identity is malformed")
    serial = _validate_serial(str(value["serial"]))
    if serial != expected_serial:
        raise ValueError("shared fixture Pluto serial differs from the exact plan serial")
    port_map = value["port_map"]
    if not isinstance(port_map, Mapping) or set(port_map) != {"tx1", "tx2", "rx1", "rx2"}:
        raise ValueError("shared Pluto port map is incomplete or unexpected")
    normalized_ports = {
        name: _validate_identifier(str(port_map[name]), f"Pluto {name} port ID")
        for name in ("tx1", "tx2", "rx1", "rx2")
    }
    if len(set(normalized_ports.values())) != len(normalized_ports):
        raise ValueError("Pluto physical port IDs must be unique within its port map")
    return {
        "id": _validate_identifier(str(value["id"]), "Pluto fixture ID"),
        "serial": serial,
        "port_map": normalized_ports,
    }


def _normalize_shared_fixture(
    value: object,
    *,
    expected_serial: str,
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "pluto",
        "reference_planes",
        "tx1_reference_splitter",
        "rx1_attenuator",
        "tx2_termination",
        "connections",
    }:
        raise ValueError("shared fixture fields are incomplete or unexpected")
    pluto = _normalize_pluto(value["pluto"], expected_serial=expected_serial)
    planes = value["reference_planes"]
    if not isinstance(planes, Mapping) or set(planes) != {"tx1", "rx1", "rx2"}:
        raise ValueError("TX1/RX1/RX2 reference planes must be separate explicit IDs")
    reference_planes = {
        name: _validate_identifier(str(planes[name]), f"{name} reference-plane ID")
        for name in ("tx1", "rx1", "rx2")
    }
    if len(set(reference_planes.values())) != 3:
        raise ValueError("TX1, RX1, and RX2 reference-plane IDs must be distinct")
    splitter = _normalize_rated_asset(
        value["tx1_reference_splitter"],
        label="TX1 two-way reference splitter",
        port_names=("input", "rx1_branch", "stimulus_branch"),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    attenuator = _normalize_rated_asset(
        value["rx1_attenuator"],
        label="RX1 attenuator",
        port_names=("input", "output"),
        extra_numeric_fields=("attenuation_db",),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    tx2_load = _normalize_rated_asset(
        value["tx2_termination"],
        label="TX2 termination load",
        port_names=("load",),
        extra_numeric_fields=("impedance_ohm",),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    raw_connections = value["connections"]
    if not isinstance(raw_connections, Mapping) or set(raw_connections) != set(
        SHARED_CONNECTION_ROLES
    ):
        raise ValueError("shared fixture connection graph is incomplete or unexpected")
    connections = {
        role: _normalize_connection(
            raw_connections[role],
            label=f"shared {role}",
            base_directory=base_directory,
            verify_files=verify_files,
        )
        for role in SHARED_CONNECTION_ROLES
    }
    pluto_id = pluto["id"]
    pluto_ports = pluto["port_map"]
    splitter_id = splitter["id"]
    splitter_ports = splitter["port_map"]
    attenuator_id = attenuator["id"]
    attenuator_ports = attenuator["port_map"]
    _require_connection(
        connections["tx1_to_splitter"],
        source=(pluto_id, pluto_ports["tx1"]),
        destination=(splitter_id, splitter_ports["input"]),
        label="TX1-to-splitter connection",
    )
    _require_connection(
        connections["splitter_to_rx1_attenuator"],
        source=(splitter_id, splitter_ports["rx1_branch"]),
        destination=(attenuator_id, attenuator_ports["input"]),
        label="splitter-to-RX1-attenuator connection",
    )
    _require_connection(
        connections["rx1_attenuator_to_rx1"],
        source=(attenuator_id, attenuator_ports["output"]),
        destination=(pluto_id, pluto_ports["rx1"]),
        label="RX1-attenuator-to-receiver connection",
    )
    _require_connection(
        connections["tx2_to_termination"],
        source=(pluto_id, pluto_ports["tx2"]),
        destination=(tx2_load["id"], tx2_load["port_map"]["load"]),
        label="TX2 termination connection",
    )
    return {
        "pluto": pluto,
        "reference_planes": reference_planes,
        "tx1_reference_splitter": splitter,
        "rx1_attenuator": attenuator,
        "tx2_termination": tx2_load,
        "connections": connections,
    }


def _normalize_load(
    value: object,
    *,
    label: str,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    return _normalize_rated_asset(
        value,
        label=label,
        port_names=("load",),
        extra_numeric_fields=("impedance_ohm",),
        base_directory=base_directory,
        verify_files=verify_files,
    )


def _normalize_selector(
    value: object,
    *,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("selector must be an object")
    identity_fields = {
        "physical_board_id",
        "hardware_revision",
        "bench_supply_id",
        "bench_supply_output_id",
        "power_positive_reference_id",
        "power_ground_reference_id",
        "control_ground_reference_id",
    }
    numeric_fields = {"supply_voltage_v", "supply_current_limit_a"}
    generic_fields = {
        "id",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "port_map",
        "characterization",
        *numeric_fields,
    }
    if set(value) != generic_fields | identity_fields:
        raise ValueError("selector physical/power identity fields are incomplete or unexpected")
    normalized = _normalize_rated_asset(
        {field: value[field] for field in generic_fields},
        label="selector",
        port_names=("common", *ANTENNA_PORTS),
        extra_numeric_fields=tuple(sorted(numeric_fields)),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    if normalized["supply_voltage_v"] <= 0 or normalized["supply_current_limit_a"] <= 0:
        raise ValueError("selector bench-supply voltage/current limit must be positive")
    normalized.update(
        {
            field: _validate_identifier(str(value[field]), f"selector {field}")
            for field in sorted(identity_fields)
        }
    )
    return normalized


def _normalize_eight_way_splitter(
    value: object,
    *,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    return _normalize_rated_asset(
        value,
        label="eight-way splitter",
        port_names=("input", *ANTENNA_PORTS),
        base_directory=base_directory,
        verify_files=verify_files,
    )


def _normalize_stage_delta(
    value: object,
    *,
    stage: str,
    shared: Mapping[str, Any],
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "delta_id",
        "selector_rf_state",
        "selector_power_state",
        "selector_control_harness_state",
        "components",
        "connections",
    }:
        raise ValueError("stage-specific fixture delta fields are incomplete or unexpected")
    if value["schema"] != 1:
        raise ValueError("stage-specific fixture delta schema is invalid")
    selector_connected = stage in SELECTOR_CONNECTED_STAGES
    expected_rf = "rf_connected" if selector_connected else "rf_disconnected"
    expected_power = "bench_power_on" if selector_connected else "bench_power_off"
    expected_harness = "connected_static_all_off" if selector_connected else "disconnected"
    if (
        value["selector_rf_state"] != expected_rf
        or value["selector_power_state"] != expected_power
        or value["selector_control_harness_state"] != expected_harness
    ):
        raise ValueError(
            "stage selector RF, bench-power, or control-harness state differs from the "
            "topology contract"
        )
    components = value["components"]
    connections = value["connections"]
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise ValueError("stage components and connections must be objects")
    pluto = shared["pluto"]
    splitter = shared["tx1_reference_splitter"]
    pluto_id = pluto["id"]
    pluto_ports = pluto["port_map"]
    splitter_id = splitter["id"]
    stimulus_port = splitter["port_map"]["stimulus_branch"]
    normalized_components: dict[str, Any]
    normalized_connections: dict[str, Any]

    if stage in {"direct_rx2_termination", "rx2_cable_terminated"}:
        if set(components) != {"tx1_stimulus_termination", "rx2_termination"}:
            raise ValueError("Stage A/B components are incomplete or unexpected")
        expected_connections = {
            "splitter_stimulus_to_termination",
            (
                "rx2_to_direct_termination"
                if stage == "direct_rx2_termination"
                else "rx2_to_far_end_termination"
            ),
        }
        if set(connections) != expected_connections:
            raise ValueError("Stage A/B connection graph is incomplete or unexpected")
        normalized_components = {
            "tx1_stimulus_termination": _normalize_load(
                components["tx1_stimulus_termination"],
                label="TX1 stimulus-branch termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "rx2_termination": _normalize_load(
                components["rx2_termination"],
                label="RX2 termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
        }
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        stimulus_load = normalized_components["tx1_stimulus_termination"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_termination"],
            source=(splitter_id, stimulus_port),
            destination=(stimulus_load["id"], stimulus_load["port_map"]["load"]),
            label="TX1 stimulus-branch termination connection",
        )
        rx2_load = normalized_components["rx2_termination"]
        rx2_role = next(role for role in expected_connections if role.startswith("rx2_"))
        _require_connection(
            normalized_connections[rx2_role],
            source=(pluto_id, pluto_ports["rx2"]),
            destination=(rx2_load["id"], rx2_load["port_map"]["load"]),
            label="RX2 termination connection",
            required_kind=(
                "direct_adapter" if stage == "direct_rx2_termination" else "coaxial_cable"
            ),
        )
    elif stage == "powered_selector_all_inputs_terminated":
        if set(components) != {
            "tx1_stimulus_termination",
            "selector",
            "selector_input_terminations",
        }:
            raise ValueError("Stage C components are incomplete or unexpected")
        loads = components["selector_input_terminations"]
        if not isinstance(loads, Mapping) or set(loads) != set(ANTENNA_PORTS):
            raise ValueError("Stage C requires eight individually identified selector loads")
        normalized_loads = {
            ant: _normalize_load(
                loads[ant],
                label=f"selector {ant} termination",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for ant in ANTENNA_PORTS
        }
        normalized_components = {
            "tx1_stimulus_termination": _normalize_load(
                components["tx1_stimulus_termination"],
                label="TX1 stimulus-branch termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector": _normalize_selector(
                components["selector"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector_input_terminations": normalized_loads,
        }
        expected_connections = {
            "splitter_stimulus_to_termination",
            "rx2_to_selector_common",
            *(f"selector_{ant.lower()}_to_termination" for ant in ANTENNA_PORTS),
        }
        if set(connections) != expected_connections:
            raise ValueError("Stage C connection graph is incomplete or unexpected")
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        stimulus_load = normalized_components["tx1_stimulus_termination"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_termination"],
            source=(splitter_id, stimulus_port),
            destination=(stimulus_load["id"], stimulus_load["port_map"]["load"]),
            label="TX1 stimulus termination connection",
        )
        selector = normalized_components["selector"]
        _require_connection(
            normalized_connections["rx2_to_selector_common"],
            source=(pluto_id, pluto_ports["rx2"]),
            destination=(selector["id"], selector["port_map"]["common"]),
            label="RX2-to-selector-common connection",
            required_kind="coaxial_cable",
        )
        for ant in ANTENNA_PORTS:
            load = normalized_loads[ant]
            _require_connection(
                normalized_connections[f"selector_{ant.lower()}_to_termination"],
                source=(selector["id"], selector["port_map"][ant]),
                destination=(load["id"], load["port_map"]["load"]),
                label=f"selector {ant} termination connection",
            )
    elif stage == "full_conducted_fixture":
        if set(components) != {"eight_way_splitter", "selector"}:
            raise ValueError("Stage E components are incomplete or unexpected")
        normalized_components = {
            "eight_way_splitter": _normalize_eight_way_splitter(
                components["eight_way_splitter"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector": _normalize_selector(
                components["selector"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
        }
        expected_connections = {
            "splitter_stimulus_to_eight_way",
            "rx2_to_selector_common",
            *(f"eight_way_{ant.lower()}_to_selector_{ant.lower()}" for ant in ANTENNA_PORTS),
        }
        if set(connections) != expected_connections:
            raise ValueError("Stage E connection graph is incomplete or unexpected")
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        eight_way = normalized_components["eight_way_splitter"]
        selector = normalized_components["selector"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_eight_way"],
            source=(splitter_id, stimulus_port),
            destination=(eight_way["id"], eight_way["port_map"]["input"]),
            label="TX1 stimulus-to-eight-way connection",
        )
        _require_connection(
            normalized_connections["rx2_to_selector_common"],
            source=(pluto_id, pluto_ports["rx2"]),
            destination=(selector["id"], selector["port_map"]["common"]),
            label="RX2-to-selector-common connection",
            required_kind="coaxial_cable",
        )
        for ant in ANTENNA_PORTS:
            _require_connection(
                normalized_connections[f"eight_way_{ant.lower()}_to_selector_{ant.lower()}"],
                source=(eight_way["id"], eight_way["port_map"][ant]),
                destination=(selector["id"], selector["port_map"][ant]),
                label=f"eight-way {ant} feed connection",
                required_kind="coaxial_cable",
            )
    else:
        raise ValueError(f"unsupported topology stage: {stage}")
    return {
        "schema": 1,
        "delta_id": _validate_identifier(str(value["delta_id"]), "stage delta ID"),
        "selector_rf_state": expected_rf,
        "selector_power_state": expected_power,
        "selector_control_harness_state": expected_harness,
        "components": normalized_components,
        "connections": normalized_connections,
    }


def _fixture_identity_sets(
    shared: Mapping[str, Any],
    stage_delta: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    component_ids: list[str] = []
    connection_ids: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if set(("id", "from", "to", "interconnect")).issubset(value):
                connection_ids.append(str(value["id"]))
            elif isinstance(value.get("id"), str):
                component_ids.append(str(value["id"]))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(shared)
    visit(stage_delta)
    if len(component_ids) != len(set(component_ids)):
        raise ValueError("fixture component/interconnect IDs must be globally unique")
    if len(connection_ids) != len(set(connection_ids)):
        raise ValueError("fixture connection IDs must be globally unique")
    return sorted(component_ids), sorted(connection_ids)


def _characterization_summary(
    shared: Mapping[str, Any],
    stage_delta: Mapping[str, Any],
    *,
    prior_characterized: bool,
) -> dict[str, Any]:
    characterized: list[str] = []
    uncharacterized: list[str] = []

    def visit(value: object, identity: str | None = None) -> None:
        if isinstance(value, Mapping):
            current = str(value.get("id")) if isinstance(value.get("id"), str) else identity
            characterization = value.get("characterization")
            if isinstance(characterization, Mapping) and current is not None:
                target = (
                    characterized
                    if characterization.get("status") == "characterized"
                    else uncharacterized
                )
                target.append(current)
            for item in value.values():
                visit(item, current)
        elif isinstance(value, list):
            for item in value:
                visit(item, identity)

    visit(shared)
    visit(stage_delta)
    all_current = not uncharacterized and bool(characterized)
    return {
        "characterized_asset_ids": sorted(set(characterized)),
        "uncharacterized_asset_ids": sorted(set(uncharacterized)),
        "all_current_stage_assets_characterized": all_current,
        "prior_stage_fixture_characterized": prior_characterized,
        "causal_attribution_fixture_eligible": all_current and prior_characterized,
        "screening_capture_allowed_when_uncharacterized": True,
        "causal_attribution_claim": False,
    }


def _rx2_connection_without_far_endpoint(connection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": connection["id"],
        "from": connection["from"],
        "interconnect": connection["interconnect"],
    }


def _current_comparison_invariants(
    stage: str,
    stage_delta: Mapping[str, Any],
) -> dict[str, Any]:
    components = stage_delta.get("components")
    connections = stage_delta.get("connections")
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise ValueError("comparison-anchor stage delta is malformed")
    if stage == "rx2_cable_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_termination": components["rx2_termination"],
        }
    if stage == "powered_selector_all_inputs_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_common_cable_without_far_endpoint": (
                _rx2_connection_without_far_endpoint(connections["rx2_to_selector_common"])
            ),
        }
    if stage == "full_conducted_fixture":
        return {
            "selector": components["selector"],
            "rx2_to_selector_common": connections["rx2_to_selector_common"],
        }
    raise ValueError("Stage A has no prior-stage comparison anchor")


def _prior_comparison_invariants(
    stage: str,
    prior_delta: Mapping[str, Any],
) -> dict[str, Any]:
    components = prior_delta.get("components")
    connections = prior_delta.get("connections")
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise LeakageLadderError("prior-stage delta is malformed")
    if stage == "rx2_cable_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_termination": components["rx2_termination"],
        }
    if stage == "powered_selector_all_inputs_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_common_cable_without_far_endpoint": (
                _rx2_connection_without_far_endpoint(connections["rx2_to_far_end_termination"])
            ),
        }
    if stage == "full_conducted_fixture":
        return {
            "selector": components["selector"],
            "rx2_to_selector_common": connections["rx2_to_selector_common"],
        }
    raise LeakageLadderError("Stage A has no prior-stage comparison anchor")


def _comparison_anchor_from_fixture_chain(
    *,
    stage: str,
    prior_fixture: Mapping[str, Any],
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any]:
    expected_prior = PRIOR_STAGE[stage]
    prior_delta = prior_fixture.get("stage_delta")
    if expected_prior is None or prior_fixture.get("stage") != expected_prior:
        raise LeakageLadderError("comparison anchor does not use the immediate prior stage")
    if not isinstance(prior_delta, Mapping):
        raise LeakageLadderError("prior-stage fixture lacks its stage delta")
    prior_delta_sha = _validate_sha256(
        prior_fixture.get("stage_delta_sha256"), "prior stage-delta hash"
    )
    if canonical_json_sha256(prior_delta) != prior_delta_sha:
        raise LeakageLadderError("prior-stage delta differs from its frozen hash")
    current = _current_comparison_invariants(stage, current_stage_delta)
    prior = _prior_comparison_invariants(stage, prior_delta)
    if current != prior:
        raise LeakageLadderError(
            "current stage substituted a comparison-anchor load, cable, selector, or power identity"
        )
    return {
        "schema": 1,
        "from_stage": expected_prior,
        "to_stage": stage,
        "prior_stage_delta_sha256": prior_delta_sha,
        "preserved_assets": current,
    }


def _validate_frozen_comparison_anchor(
    value: object,
    *,
    stage: str,
    prior_stage_delta_sha256: str,
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "from_stage",
        "to_stage",
        "prior_stage_delta_sha256",
        "preserved_assets",
    }:
        raise ValueError("prior-stage comparison anchor is incomplete or unexpected")
    document = _json_safe(dict(value))
    assert isinstance(document, dict)
    if (
        document["schema"] != 1
        or document["from_stage"] != PRIOR_STAGE[stage]
        or document["to_stage"] != stage
        or document["prior_stage_delta_sha256"] != prior_stage_delta_sha256
        or document["preserved_assets"]
        != _current_comparison_invariants(stage, current_stage_delta)
    ):
        raise ValueError("prior-stage comparison anchor differs from the current fixture")
    return document


def _validate_frozen_prior_stage_binding(
    value: object,
    *,
    stage: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    shared_fixture_sha256: str,
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any] | None:
    expected_stage = PRIOR_STAGE[stage]
    if expected_stage is None:
        if value is not None:
            raise ValueError("Stage A must not bind a prior-stage plan")
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "stage",
        "run_id",
        "plan_path",
        "plan_file_sha256",
        "plan_contract_sha256",
        "fixture_evidence_sha256",
        "shared_fixture_sha256",
        "prior_stage_delta_sha256",
        "comparison_anchor",
        "comparison_anchor_sha256",
        "prior_selector_control_sha256",
        "campaign_id",
        "comparable_fixture_group_id",
        "prior_fixture_characterized",
    }:
        raise ValueError("prior-stage binding fields are incomplete or unexpected")
    document = _json_safe(dict(value))
    assert isinstance(document, dict)
    if (
        document["stage"] != expected_stage
        or document["campaign_id"] != campaign_id
        or document["comparable_fixture_group_id"] != comparable_fixture_group_id
        or document["shared_fixture_sha256"] != shared_fixture_sha256
        or not isinstance(document["prior_fixture_characterized"], bool)
    ):
        raise ValueError("prior-stage binding differs from the comparable fixture chain")
    document["run_id"] = _validate_identifier(str(document["run_id"]), "prior run ID")
    plan_path = Path(str(document["plan_path"]))
    if not plan_path.is_absolute():
        raise ValueError("prior-stage plan path must be absolute")
    document["plan_path"] = str(plan_path)
    for field in (
        "plan_file_sha256",
        "plan_contract_sha256",
        "fixture_evidence_sha256",
        "shared_fixture_sha256",
        "prior_stage_delta_sha256",
        "comparison_anchor_sha256",
    ):
        document[field] = _validate_sha256(document[field], f"prior-stage {field}")
    prior_selector_sha = document["prior_selector_control_sha256"]
    if expected_stage in SELECTOR_CONNECTED_STAGES:
        document["prior_selector_control_sha256"] = _validate_sha256(
            prior_selector_sha,
            "prior-stage selector-control hash",
        )
    elif prior_selector_sha is not None:
        raise ValueError("selector-disconnected prior stage must not bind selector control")
    document["comparison_anchor"] = _validate_frozen_comparison_anchor(
        document["comparison_anchor"],
        stage=stage,
        prior_stage_delta_sha256=document["prior_stage_delta_sha256"],
        current_stage_delta=current_stage_delta,
    )
    if canonical_json_sha256(document["comparison_anchor"]) != document["comparison_anchor_sha256"]:
        raise ValueError("prior-stage comparison-anchor hash is inconsistent")
    return document


def _prior_stage_binding_from_plan(
    value: object,
    *,
    stage: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    shared_fixture_sha256: str,
    current_stage_delta: Mapping[str, Any],
    board_id: str,
    serial: str,
    base_directory: Path,
) -> dict[str, Any] | None:
    expected_stage = PRIOR_STAGE[stage]
    if expected_stage is None:
        if value is not None:
            raise LeakageLadderError("Stage A fixture manifest must use null prior_stage_binding")
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "stage",
        "run_id",
        "plan_path",
        "plan_file_sha256",
        "plan_contract_sha256",
        "fixture_evidence_sha256",
    }:
        raise LeakageLadderError("prior-stage fixture binding is incomplete or unexpected")
    if value["stage"] != expected_stage:
        raise LeakageLadderError(f"{stage} must bind the immediately prior {expected_stage} plan")
    plan_path = Path(str(value["plan_path"])).expanduser()
    if not plan_path.is_absolute():
        plan_path = base_directory / plan_path
    plan_path = plan_path.resolve(strict=True)
    declared_file_sha = _validate_sha256(value["plan_file_sha256"], "prior plan file hash")
    if sha256_path(plan_path) != declared_file_sha:
        raise LeakageLadderError("prior-stage plan file differs from its declared hash")
    envelope = _read_json(plan_path, "prior-stage immutable plan")
    contract = envelope.get("plan_contract")
    if (
        envelope.get("schema") != 1
        or envelope.get("immutable") is not True
        or not isinstance(contract, Mapping)
        or envelope.get("plan_contract_sha256") != canonical_json_sha256(contract)
    ):
        raise LeakageLadderError("prior-stage immutable plan envelope is invalid")
    declared_contract_sha = _validate_sha256(
        value["plan_contract_sha256"], "prior plan contract hash"
    )
    if declared_contract_sha != envelope["plan_contract_sha256"]:
        raise LeakageLadderError("prior-stage contract hash differs from its immutable plan")
    prior_fixture = contract.get("fixture_evidence")
    declared_fixture_sha = _validate_sha256(
        value["fixture_evidence_sha256"], "prior fixture evidence hash"
    )
    prior_characterization = (
        prior_fixture.get("characterization_summary")
        if isinstance(prior_fixture, Mapping)
        else None
    )
    if (
        contract.get("topology_stage") != expected_stage
        or contract.get("run_id") != value["run_id"]
        or contract.get("board_id") != board_id
        or contract.get("configuration", {}).get("serial") != serial
        or not isinstance(prior_fixture, Mapping)
        or contract.get("fixture_evidence_sha256") != declared_fixture_sha
        or canonical_json_sha256(prior_fixture) != declared_fixture_sha
        or prior_fixture.get("campaign_id") != campaign_id
        or prior_fixture.get("comparable_fixture_group_id") != comparable_fixture_group_id
        or prior_fixture.get("shared_fixture_sha256") != shared_fixture_sha256
        or not isinstance(prior_characterization, Mapping)
    ):
        raise LeakageLadderError("prior-stage plan is not comparable with this fixture")
    comparison_anchor = _comparison_anchor_from_fixture_chain(
        stage=stage,
        prior_fixture=prior_fixture,
        current_stage_delta=current_stage_delta,
    )
    prior_selector_control = contract.get("selector_control")
    if expected_stage in SELECTOR_CONNECTED_STAGES:
        if not isinstance(prior_selector_control, Mapping):
            raise LeakageLadderError("prior selector-connected plan lacks selector control")
        prior_selector_control_sha256 = canonical_json_sha256(prior_selector_control)
    else:
        if prior_selector_control is not None:
            raise LeakageLadderError("prior selector-disconnected plan contains selector control")
        prior_selector_control_sha256 = None
    normalized = {
        "stage": expected_stage,
        "run_id": _validate_identifier(str(value["run_id"]), "prior run ID"),
        "plan_path": str(plan_path),
        "plan_file_sha256": declared_file_sha,
        "plan_contract_sha256": declared_contract_sha,
        "fixture_evidence_sha256": declared_fixture_sha,
        "shared_fixture_sha256": shared_fixture_sha256,
        "prior_stage_delta_sha256": comparison_anchor["prior_stage_delta_sha256"],
        "comparison_anchor": comparison_anchor,
        "comparison_anchor_sha256": canonical_json_sha256(comparison_anchor),
        "prior_selector_control_sha256": prior_selector_control_sha256,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": comparable_fixture_group_id,
        "prior_fixture_characterized": bool(
            prior_characterization.get("causal_attribution_fixture_eligible")
        ),
    }
    return _validate_frozen_prior_stage_binding(
        normalized,
        stage=stage,
        campaign_id=campaign_id,
        comparable_fixture_group_id=comparable_fixture_group_id,
        shared_fixture_sha256=shared_fixture_sha256,
        current_stage_delta=current_stage_delta,
    )


def _normalize_setup_attestation(
    path: Path,
    *,
    run_id: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    stage: str,
    fixture_manifest_sha256: str,
    shared_fixture_sha256: str,
    stage_delta_sha256: str,
    component_ids: list[str],
    connection_ids: list[str],
) -> dict[str, Any]:
    exact_path = path.expanduser().resolve(strict=True)
    raw = _read_json(exact_path, "per-run setup attestation")
    if set(raw) != {
        "schema",
        "attestation_kind",
        "attestation_id",
        "created_at",
        "run_id",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "fixture_manifest_sha256",
        "shared_fixture_sha256",
        "stage_delta_sha256",
        "observed_component_ids",
        "observed_connection_ids",
        "setup_evidence_path",
        "setup_evidence_sha256",
    }:
        raise LeakageLadderError("per-run setup attestation fields are incomplete or unexpected")
    try:
        created = datetime.fromisoformat(str(raw["created_at"]))
    except ValueError as error:
        raise LeakageLadderError("setup attestation created_at is not ISO-8601") from error
    if created.tzinfo is None:
        raise LeakageLadderError("setup attestation created_at must include a timezone")
    if (
        raw["schema"] != 1
        or raw["attestation_kind"] != SETUP_ATTESTATION_KIND
        or raw["run_id"] != run_id
        or raw["campaign_id"] != campaign_id
        or raw["comparable_fixture_group_id"] != comparable_fixture_group_id
        or raw["stage"] != stage
        or raw["fixture_manifest_sha256"] != fixture_manifest_sha256
        or raw["shared_fixture_sha256"] != shared_fixture_sha256
        or raw["stage_delta_sha256"] != stage_delta_sha256
        or raw["observed_component_ids"] != component_ids
        or raw["observed_connection_ids"] != connection_ids
    ):
        raise LeakageLadderError("per-run setup attestation is not bound to this exact fixture")
    evidence_path = Path(str(raw["setup_evidence_path"])).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = exact_path.parent / evidence_path
    evidence_path = evidence_path.resolve(strict=True)
    if not evidence_path.is_file():
        raise LeakageLadderError("setup evidence must be a regular file")
    evidence_sha = _validate_sha256(raw["setup_evidence_sha256"], "setup evidence hash")
    if sha256_path(evidence_path) != evidence_sha:
        raise LeakageLadderError("setup evidence differs from its declared hash")
    return {
        "schema": 1,
        "attestation_kind": SETUP_ATTESTATION_KIND,
        "attestation_id": _validate_identifier(str(raw["attestation_id"]), "setup attestation ID"),
        "created_at": created.isoformat(),
        "created_at_wall_clock_freshness_enforced": False,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": comparable_fixture_group_id,
        "stage": stage,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "shared_fixture_sha256": shared_fixture_sha256,
        "stage_delta_sha256": stage_delta_sha256,
        "observed_component_ids": component_ids,
        "observed_connection_ids": connection_ids,
        "setup_evidence": {
            "path": str(evidence_path),
            "sha256": evidence_sha,
            "size_bytes": evidence_path.stat().st_size,
        },
        "setup_attestation_file": {
            "path": str(exact_path),
            "sha256": sha256_path(exact_path),
            "size_bytes": exact_path.stat().st_size,
        },
    }


def _validate_fixture_evidence_v2(
    value: Mapping[str, Any],
    *,
    expected_stage: str,
    expected_run_id: str,
    expected_board_id: str,
    expected_serial: str,
) -> dict[str, Any]:
    document = _json_safe(dict(value))
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "fixture_kind",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "run_id",
        "board_id",
        "source_files",
        "shared_fixture",
        "shared_fixture_sha256",
        "stage_delta",
        "stage_delta_sha256",
        "prior_stage_binding",
        "setup_attestation",
        "component_ids",
        "connection_ids",
        "characterization_summary",
    }:
        raise ValueError("fixture-evidence v2 fields are incomplete or unexpected")
    if (
        document["schema"] != 2
        or document["fixture_kind"] != FIXTURE_KIND_V2
        or document["stage"] != expected_stage
        or document["run_id"] != expected_run_id
        or document["board_id"] != expected_board_id
    ):
        raise ValueError("fixture-evidence v2 identity differs from the plan")
    campaign_id = _validate_identifier(str(document["campaign_id"]), "campaign ID")
    group_id = _validate_identifier(
        str(document["comparable_fixture_group_id"]), "comparable fixture group ID"
    )
    source_files = document["source_files"]
    if not isinstance(source_files, Mapping) or set(source_files) != {
        "fixture_manifest",
        "setup_attestation",
    }:
        raise ValueError("fixture source-file evidence is incomplete")
    document["source_files"] = {
        name: _validate_file_evidence(source_files[name], name)
        for name in ("fixture_manifest", "setup_attestation")
    }
    shared = _normalize_shared_fixture(document["shared_fixture"], expected_serial=expected_serial)
    shared_sha = _validate_sha256(document["shared_fixture_sha256"], "shared fixture hash")
    if canonical_json_sha256(shared) != shared_sha:
        raise ValueError("shared fixture hash differs from normalized fixture identity")
    delta = _normalize_stage_delta(document["stage_delta"], stage=expected_stage, shared=shared)
    delta_sha = _validate_sha256(document["stage_delta_sha256"], "stage delta hash")
    if canonical_json_sha256(delta) != delta_sha:
        raise ValueError("stage delta hash differs from normalized stage identity")
    prior = _validate_frozen_prior_stage_binding(
        document["prior_stage_binding"],
        stage=expected_stage,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        shared_fixture_sha256=shared_sha,
        current_stage_delta=delta,
    )
    component_ids, connection_ids = _fixture_identity_sets(shared, delta)
    if document["component_ids"] != component_ids or document["connection_ids"] != connection_ids:
        raise ValueError("fixture component/connection ID inventory differs from its graph")
    setup = document["setup_attestation"]
    if not isinstance(setup, Mapping) or (
        setup.get("run_id") != expected_run_id
        or setup.get("campaign_id") != campaign_id
        or setup.get("comparable_fixture_group_id") != group_id
        or setup.get("stage") != expected_stage
        or setup.get("created_at_wall_clock_freshness_enforced") is not False
        or setup.get("fixture_manifest_sha256")
        != document["source_files"]["fixture_manifest"]["sha256"]
        or setup.get("shared_fixture_sha256") != shared_sha
        or setup.get("stage_delta_sha256") != delta_sha
        or setup.get("observed_component_ids") != component_ids
        or setup.get("observed_connection_ids") != connection_ids
    ):
        raise ValueError("per-run setup attestation binding is invalid")
    if setup.get("setup_attestation_file") != document["source_files"]["setup_attestation"]:
        raise ValueError("setup-attestation file evidence differs from source binding")
    _validate_file_evidence(setup.get("setup_evidence"), "setup evidence")
    prior_characterized = prior is None or bool(prior["prior_fixture_characterized"])
    summary = _characterization_summary(
        shared,
        delta,
        prior_characterized=prior_characterized,
    )
    if document["characterization_summary"] != summary:
        raise ValueError("fixture characterization summary is inconsistent")
    document.update(
        {
            "campaign_id": campaign_id,
            "comparable_fixture_group_id": group_id,
            "shared_fixture": shared,
            "stage_delta": delta,
            "prior_stage_binding": prior,
            "setup_attestation": dict(setup),
        }
    )
    return document


def _fixture_evidence_from_manifests(
    fixture_manifest_path: Path,
    setup_attestation_path: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
    stage: str,
) -> dict[str, Any]:
    manifest_path = fixture_manifest_path.expanduser().resolve(strict=True)
    raw = _read_json(manifest_path, "fixture manifest v2")
    if set(raw) != {
        "schema",
        "fixture_kind",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "board_id",
        "shared_fixture",
        "stage_delta",
        "prior_stage_binding",
    }:
        raise LeakageLadderError("fixture manifest v2 fields are incomplete or unexpected")
    if (
        raw["schema"] != 2
        or raw["fixture_kind"] != FIXTURE_KIND_V2
        or raw["stage"] != stage
        or raw["board_id"] != board_id
    ):
        raise LeakageLadderError("fixture manifest v2 identity differs from requested plan")
    campaign_id = _validate_identifier(str(raw["campaign_id"]), "campaign ID")
    group_id = _validate_identifier(
        str(raw["comparable_fixture_group_id"]), "comparable fixture group ID"
    )
    shared = _normalize_shared_fixture(
        raw["shared_fixture"],
        expected_serial=serial,
        base_directory=manifest_path.parent,
        verify_files=True,
    )
    shared_sha = canonical_json_sha256(shared)
    delta = _normalize_stage_delta(
        raw["stage_delta"],
        stage=stage,
        shared=shared,
        base_directory=manifest_path.parent,
        verify_files=True,
    )
    delta_sha = canonical_json_sha256(delta)
    prior = _prior_stage_binding_from_plan(
        raw["prior_stage_binding"],
        stage=stage,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        shared_fixture_sha256=shared_sha,
        current_stage_delta=delta,
        board_id=board_id,
        serial=serial,
        base_directory=manifest_path.parent,
    )
    component_ids, connection_ids = _fixture_identity_sets(shared, delta)
    manifest_file_evidence = {
        "path": str(manifest_path),
        "sha256": sha256_path(manifest_path),
        "size_bytes": manifest_path.stat().st_size,
    }
    setup = _normalize_setup_attestation(
        setup_attestation_path,
        run_id=run_id,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        stage=stage,
        fixture_manifest_sha256=str(manifest_file_evidence["sha256"]),
        shared_fixture_sha256=shared_sha,
        stage_delta_sha256=delta_sha,
        component_ids=component_ids,
        connection_ids=connection_ids,
    )
    prior_characterized = prior is None or bool(prior["prior_fixture_characterized"])
    normalized = {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND_V2,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": group_id,
        "stage": stage,
        "run_id": run_id,
        "board_id": board_id,
        "source_files": {
            "fixture_manifest": manifest_file_evidence,
            "setup_attestation": setup["setup_attestation_file"],
        },
        "shared_fixture": shared,
        "shared_fixture_sha256": shared_sha,
        "stage_delta": delta,
        "stage_delta_sha256": delta_sha,
        "prior_stage_binding": prior,
        "setup_attestation": setup,
        "component_ids": component_ids,
        "connection_ids": connection_ids,
        "characterization_summary": _characterization_summary(
            shared,
            delta,
            prior_characterized=prior_characterized,
        ),
    }
    return _validate_fixture_evidence_v2(
        normalized,
        expected_stage=stage,
        expected_run_id=run_id,
        expected_board_id=board_id,
        expected_serial=serial,
    )


def _selector_control_contract(
    *,
    bench_manifest_path: Path,
    openocd_config_path: Path,
    profile_path: Path,
) -> dict[str, Any]:
    """Freeze the exact static-selector mailbox and ALL_OFF control artifacts."""

    manifest_path = bench_manifest_path.expanduser().resolve(strict=True)
    config_path = openocd_config_path.expanduser().resolve(strict=True)
    exact_profile_path = profile_path.expanduser().resolve(strict=True)
    profile_header_path = exact_profile_path.with_name("control_profile.h").resolve(strict=True)
    manifest = BenchManifest.load(manifest_path)
    profile = load_profile(exact_profile_path)
    return {
        "schema": 1,
        "mode": "reviewed_static_selector_mailbox_all_off",
        "bench_manifest": {
            "path": str(manifest_path),
            "file_sha256": sha256_path(manifest_path),
            "elf_sha256": manifest.elf_sha256,
            "mailbox_address": manifest.address,
            "mailbox_size": manifest.size,
            "mailbox_magic": manifest.magic,
            "mailbox_version": manifest.version,
            "max_lease_ms": manifest.max_lease_ms,
            "mailbox_offsets": dict(manifest.offsets),
        },
        "openocd_config": {
            "path": str(config_path),
            "file_sha256": sha256_path(config_path),
        },
        "control_profile": {
            "path": str(exact_profile_path),
            "file_sha256": sha256_path(exact_profile_path),
            "header_path": str(profile_header_path),
            "header_file_sha256": sha256_path(profile_header_path),
            "profile_id": profile.profile_id,
            "revision": profile.revision,
            "contract_sha256": profile.contract_sha256,
            "all_off_code": profile.all_off_code,
        },
        "command": {
            "code": profile.all_off_code,
            "lease_ms": 0,
            "wait_until_applied": True,
            "readback_required": True,
        },
    }


def _validate_selector_control_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    document = _json_safe(dict(value))
    if not isinstance(document, dict):
        raise ValueError("selector-control contract must be an object")
    manifest = document.get("bench_manifest")
    config = document.get("openocd_config")
    profile = document.get("control_profile")
    command = document.get("command")
    if (
        document.get("schema") != 1
        or document.get("mode") != "reviewed_static_selector_mailbox_all_off"
        or not all(isinstance(item, Mapping) for item in (manifest, config, profile, command))
    ):
        raise ValueError("selector-control contract is malformed")
    assert isinstance(manifest, Mapping)
    assert isinstance(config, Mapping)
    assert isinstance(profile, Mapping)
    assert isinstance(command, Mapping)
    if (
        command.get("code") != profile.get("all_off_code")
        or command.get("lease_ms") != 0
        or command.get("wait_until_applied") is not True
        or command.get("readback_required") is not True
    ):
        raise ValueError("selector-control contract does not require static ALL_OFF readback")
    for artifact, key in (
        (manifest, "file_sha256"),
        (config, "file_sha256"),
        (profile, "file_sha256"),
        (profile, "header_file_sha256"),
    ):
        digest = str(artifact.get(key, ""))
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("selector-control artifact hash is malformed")
    return document


def _tone_plan(condition: Mapping[str, Any], contract: Mapping[str, Any]) -> SafeDdsTonePlan:
    configuration = contract["configuration"]
    if not isinstance(configuration, Mapping):
        raise LeakageLadderError("plan configuration is malformed")
    return SafeDdsTonePlan(
        uri=str(configuration["uri"]),
        serial=str(configuration["serial"]),
        center_frequency_hz=int(condition["center_frequency_hz"]),
        sample_rate_hz=int(condition["sample_rate_hz"]),
        bandwidth_hz=int(condition["bandwidth_hz"]),
        tone_frequency_hz=int(condition["tone_offset_hz"]),
        tx_channel=int(condition["tx_channel"]),
        tx_hardware_gain_db=float(condition["tx_hardware_gain_db"]),
        dds_scale=float(condition["dds_scale"]),
        receiver_gain_db=float(condition["receiver_gain_db"]),
        source_peak_output_bound_dbm=SOURCE_PEAK_OUTPUT_BOUND_DBM,
        load_input_limit_dbm=LOAD_INPUT_LIMIT_DBM,
        path_attenuation_before_load_db=PATH_ATTENUATION_BEFORE_LOAD_DB,
        required_margin_db=REQUIRED_MARGIN_DB,
        settle_ms=100,
    )


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    stage: str,
    source_commit: str,
    pluto_plus_utils_source_attestation: Mapping[str, Any],
    selector_control: Mapping[str, Any] | None = None,
    native_libiio_runtime_attestation: Mapping[str, Any] | None = None,
    fixture_evidence: Mapping[str, Any] | None = None,
    freeze_attribution_repeats: bool = True,
) -> dict[str, Any]:
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    exact_serial = _validate_serial(serial)
    exact_uri = _validate_usb_uri(uri)
    if stage not in STAGE_CONTRACTS:
        raise ValueError(f"unsupported topology stage: {stage}")
    source = _validate_commit(source_commit, "smateway source commit")
    dependency = _validate_dependency_source_attestation(pluto_plus_utils_source_attestation)
    native_runtime = (
        _validate_native_libiio_runtime_attestation(native_libiio_runtime_attestation)
        if native_libiio_runtime_attestation is not None
        else None
    )
    frozen_fixture_evidence = (
        _validate_fixture_evidence_v2(
            fixture_evidence,
            expected_stage=stage,
            expected_run_id=run,
            expected_board_id=board,
            expected_serial=exact_serial,
        )
        if fixture_evidence is not None
        else None
    )
    if stage in SELECTOR_CONNECTED_STAGES:
        if selector_control is None:
            raise ValueError("selector-connected stage requires frozen static ALL_OFF control")
        frozen_selector_control = _validate_selector_control_contract(selector_control)
    elif selector_control is not None:
        raise ValueError("selector-disconnected stage must not include selector control")
    else:
        frozen_selector_control = None
    if stage == "full_conducted_fixture" and frozen_fixture_evidence is not None:
        prior_binding = frozen_fixture_evidence.get("prior_stage_binding")
        if (
            not isinstance(prior_binding, Mapping)
            or frozen_selector_control is None
            or prior_binding.get("prior_selector_control_sha256")
            != canonical_json_sha256(frozen_selector_control)
        ):
            raise ValueError(
                "Stage E selector control differs from the immediately prior Stage C plan"
            )
    policy = classify_fast20_center_frequency(
        CENTER_FREQUENCY_HZ,
        allow_experimental_5g8=True,
    )
    conditions: list[dict[str, Any]] = []
    for index, tx_gain_db in enumerate(TX_HARDWARE_GAINS_DB):
        is_attribution_gain = tx_gain_db == ATTRIBUTION_GAIN_DB
        condition = {
            "plan_index": index,
            "condition_id": f"{stage}-tx{tx_gain_db:g}db",
            "stage": stage,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "center_frequency_policy": policy,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_required_exact_muted": True,
            "tx_hardware_gain_db": tx_gain_db,
            "dds_scale": DDS_SCALE,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": TOTAL_SAMPLES,
            "kernel_buffers": KERNEL_BUFFERS,
            "fresh_stream_required": True,
        }
        if freeze_attribution_repeats:
            condition.update(
                {
                    "condition_role": (
                        "linearity_ladder_and_attribution_repeat"
                        if is_attribution_gain
                        else "linearity_ladder"
                    ),
                    "attribution_repeat_index": 1 if is_attribution_gain else None,
                    "attribution_repeat_count": (
                        ATTRIBUTION_REPEAT_COUNT if is_attribution_gain else None
                    ),
                }
            )
        _tone_plan(
            condition,
            {"configuration": {"serial": exact_serial, "uri": exact_uri}},
        )
        conditions.append(condition)
    if freeze_attribution_repeats:
        for repeat_index in range(2, ATTRIBUTION_REPEAT_COUNT + 1):
            condition = {
                **conditions[TX_HARDWARE_GAINS_DB.index(ATTRIBUTION_GAIN_DB)],
                "plan_index": len(conditions),
                "condition_id": (
                    f"{stage}-tx{ATTRIBUTION_GAIN_DB:g}db-attribution-repeat{repeat_index}"
                ),
                "condition_role": "attribution_repeat",
                "attribution_repeat_index": repeat_index,
            }
            _tone_plan(
                condition,
                {"configuration": {"serial": exact_serial, "uri": exact_uri}},
            )
            conditions.append(condition)
    board_state_root = _board_root(board)
    characterization = (
        frozen_fixture_evidence.get("characterization_summary")
        if isinstance(frozen_fixture_evidence, Mapping)
        else None
    )
    causal_fixture_eligible = bool(
        isinstance(characterization, Mapping)
        and characterization.get("causal_attribution_fixture_eligible") is True
    )
    return {
        "schema": 1,
        "plan_kind": "5g8_marker_independent_coherent_leakage_ladder",
        "run_id": run,
        "board_id": board,
        "topology_stage": stage,
        "stage_contract": dict(STAGE_CONTRACTS[stage]),
        "source": {
            "smateway_commit": source,
            "pluto_plus_utils_source_attestation": dependency,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
            "native_libiio_runtime_attestation": native_runtime,
            "native_libiio_runtime_attestation_sha256": (
                canonical_json_sha256(native_runtime) if native_runtime is not None else None
            ),
            "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
            "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
            "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
            "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
        },
        "fixture_evidence": frozen_fixture_evidence,
        "fixture_evidence_sha256": (
            canonical_json_sha256(frozen_fixture_evidence)
            if frozen_fixture_evidence is not None
            else None
        ),
        "selector_control": frozen_selector_control,
        "configuration": {
            "serial": exact_serial,
            "uri": exact_uri,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_required_exact_muted": True,
            "dds_scale": DDS_SCALE,
            "tx_hardware_gains_db": list(TX_HARDWARE_GAINS_DB),
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count_per_condition": TOTAL_SAMPLES,
            "duration_s_per_condition": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
            "kernel_buffers": KERNEL_BUFFERS,
            "fresh_stream_per_condition": True,
            "metadata_abi": 2,
            "automatic_retry_count": 0,
            "attribution_gain_db": ATTRIBUTION_GAIN_DB,
            "attribution_repeat_count": ATTRIBUTION_REPEAT_COUNT,
            "attribution_repeats_require_unique_fresh_streams": True,
            "pilot_frequency_refinement_required": True,
            "minimum_pilot_confidence": MINIMUM_PILOT_CONFIDENCE,
            "minimum_pilot_phase_step_coherence": MINIMUM_PILOT_PHASE_STEP_COHERENCE,
            "maximum_pilot_phase_rms_deg": MAXIMUM_PILOT_PHASE_RMS_DEG,
        },
        "operator_confirmations_required": {
            "no_antennas_anywhere": True,
            "tx1_matched_conducted_network": True,
            "tx2_muted_and_50ohm_terminated": True,
            "rx1_attenuated_conducted_reference": True,
            "no_component_or_connection_movement_since_setup_attestation": True,
            "fixture_evidence_sha256": (
                canonical_json_sha256(frozen_fixture_evidence)
                if frozen_fixture_evidence is not None
                else None
            ),
            "exact_stage": stage,
            "topology_confirmation_token": STAGE_CONTRACTS[stage]["confirmation_token"],
        },
        "safety": {
            "exact_serial_and_current_usb_uri_required": True,
            "tx1_only": True,
            "tx2_gain_readback_required_db": -80.0,
            "inactive_dds_scales_required_zero": True,
            "exact_mute_before_stage": True,
            "exact_mute_after_every_condition": True,
            "exact_mute_in_stage_finally": True,
            "headroom_failure_stops_stronger_conditions": True,
            "failure_fragments_are_quarantined": True,
            "automatic_retry_count": 0,
            "read_only_usb_identity_scan_before_rf": True,
            "resolved_usb_uri_must_equal_requested_uri": True,
            "native_libiio_exact_path_version_hash_required": native_runtime is not None,
            "fixture_v2_files_and_prior_plan_rehashed_before_rf": (
                frozen_fixture_evidence is not None
            ),
            "selector_static_all_off_readback_required": stage in SELECTOR_CONNECTED_STAGES,
        },
        "storage": {
            "medium": "raspberry_pi_local_filesystem",
            "board_state_root": str(board_state_root),
            "artifact_root": str(board_state_root / "pluto-usb-captures"),
            "run_capture_root": str(board_state_root / "pluto-usb-captures" / "leakage-runs" / run),
            "pluto_onboard_storage_used": False,
            "estimated_raw_iq_bytes": (
                len(conditions) * TOTAL_SAMPLES * 2 * 2 * np.dtype("<i2").itemsize
            ),
        },
        "interpretation": {
            "purpose": "diagnose coherent TX1-to-RX2 leakage by physical topology stage",
            "marker_required": False,
            "selector_calibration_claim": False,
            "causal_attribution_claim": False,
            "causal_attribution_fixture_eligible": causal_fixture_eligible,
            "uncharacterized_fixture_is_screening_only": not causal_fixture_eligible,
            "may_be_used_as_selector_calibration": False,
            "rx2_tone_absence_is_a_valid_low_leakage_result": True,
            "one_hot_path_diagnosis": {
                "implemented_by_this_runner": False,
                "required_future_runner": "run_5g8_one_hot_path_ladder.py",
                "reason": (
                    "per-port path response requires a separate immutable state/readback plan; "
                    "the present runner measures only static ALL_OFF topology leakage"
                ),
            },
        },
        "conditions": conditions,
    }


def _plan_envelope(contract: Mapping[str, Any]) -> dict[str, Any]:
    frozen = dict(contract)
    return {
        "schema": 1,
        "plan_contract": frozen,
        "plan_contract_sha256": canonical_json_sha256(frozen),
        "plan_contract_hash_provenance": (
            "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
        ),
        "immutable": True,
    }


def _validate_plan_envelope(
    document: Mapping[str, Any],
    *,
    expected_contract: Mapping[str, Any],
) -> dict[str, Any]:
    contract = document.get("plan_contract")
    if document.get("schema") != 1 or document.get("immutable") is not True:
        raise LeakageLadderError("immutable plan envelope schema is invalid")
    if not isinstance(contract, Mapping):
        raise LeakageLadderError("immutable plan contract is malformed")
    observed_sha = canonical_json_sha256(contract)
    if document.get("plan_contract_sha256") != observed_sha:
        raise LeakageLadderError("immutable plan canonical contract hash does not match")
    if document.get("plan_contract_hash_provenance") != (
        "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
    ):
        raise LeakageLadderError("immutable plan contract hash provenance is invalid")
    if dict(contract) != dict(expected_contract):
        raise LeakageLadderError("requested execution differs from the immutable local plan")
    return dict(document)


def _prepare_plan(path: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    expected = _plan_envelope(contract)
    if path.exists():
        return _validate_plan_envelope(
            _read_json(path, "immutable plan"),
            expected_contract=contract,
        )
    _write_immutable_json(path, expected)
    return expected


def _prepare_plan_only_run(
    *,
    plan_path: Path,
    manifest_path: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a new run once, or reload its intact prepared manifest idempotently."""

    run_root = plan_path.parent
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise LeakageLadderError("plan storage contract is malformed")
    capture_root = Path(str(storage.get("run_capture_root", "")))
    tombstone = _failure_tombstone_path(manifest_path)
    if tombstone.exists() or tombstone.is_symlink():
        raise LeakageLadderError("failed-run tombstone forbids plan-only reuse")
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file() or not plan_path.is_file():
            raise LeakageLadderError("existing run state is incomplete or uses a symlink")
        envelope = _prepare_plan(plan_path, contract)
        return envelope, _load_manifest(
            manifest_path,
            plan_path=plan_path,
            envelope=envelope,
        )
    if (
        run_root.exists()
        or run_root.is_symlink()
        or plan_path.exists()
        or plan_path.is_symlink()
        or capture_root.exists()
        or capture_root.is_symlink()
    ):
        raise LeakageLadderError(
            "run ID has plan, directory, tombstone, or capture history but no intact manifest"
        )
    envelope = _prepare_plan(plan_path, contract)
    return envelope, _new_manifest(plan_path, envelope)


def _plan_file_evidence(path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "plan_contract_sha256": str(envelope["plan_contract_sha256"]),
        "plan_contract_hash_provenance": str(envelope["plan_contract_hash_provenance"]),
        "plan_file_sha256": sha256_path(path),
        "plan_file_hash_provenance": "SHA-256 over exact immutable plan.json file bytes",
    }


def _new_manifest(plan_path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    return {
        "schema": 1,
        "run_kind": "5g8_marker_independent_coherent_leakage_ladder",
        "run_id": contract["run_id"],
        "topology_stage": contract["topology_stage"],
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "immutable_plan": _plan_file_evidence(plan_path, envelope),
        "confirmations": [],
        "native_runtime_preflight_attempts": [],
        "native_runtime_preflight": None,
        "fixture_evidence_preflight_attempts": [],
        "fixture_evidence_preflight": None,
        "selector_initial_state_attempts": [],
        "selector_initial_state": None,
        "identity_preflight_attempts": [],
        "identity_preflight": None,
        "preflight_mute_attempts": [],
        "attempts": [],
        "recovery_mute_attempts": [],
        "recovery_selector_cleanup_attempts": [],
        "orphan_quarantine_attempts": [],
        "final_mute_attempts": [],
        "final_mute": None,
        "final_selector_cleanup_attempts": [],
        "final_selector_cleanup": None,
        "error": None,
        "summary": {},
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
    }


def _failure_tombstone_path(manifest_path: Path) -> Path:
    return manifest_path.parent / FAILURE_TOMBSTONE_FILENAME


def _validate_failure_tombstone(
    path: Path,
    *,
    run_id: object,
    topology_stage: object,
    immutable_plan: object,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LeakageLadderError("failed-run tombstone must be a regular non-symlink file")
    if path.stat().st_mode & 0o222:
        raise LeakageLadderError("failed-run tombstone must remain read-only")
    document = _read_json(path, "failed-run tombstone")
    if (
        set(document)
        != {
            "schema",
            "marker_kind",
            "run_id",
            "topology_stage",
            "immutable_plan",
            "first_failed_at",
            "manifest_path",
            "first_failure_error",
            "retry_forbidden",
        }
        or document.get("schema") != 1
        or document.get("marker_kind") != "5g8_general_ladder_failed_run_tombstone"
        or document.get("run_id") != run_id
        or document.get("topology_stage") != topology_stage
        or document.get("immutable_plan") != immutable_plan
        or not isinstance(document.get("first_failed_at"), str)
        or not document.get("first_failed_at")
        or document.get("manifest_path") != str(path.parent / MANIFEST_FILENAME)
        or document.get("retry_forbidden") is not True
    ):
        raise LeakageLadderError("failed-run tombstone identity is invalid")
    return document


def _ensure_failure_tombstone(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    path = _failure_tombstone_path(manifest_path)
    if path.exists() or path.is_symlink():
        return _validate_failure_tombstone(
            path,
            run_id=manifest.get("run_id"),
            topology_stage=manifest.get("topology_stage"),
            immutable_plan=manifest.get("immutable_plan"),
        )
    document = {
        "schema": 1,
        "marker_kind": "5g8_general_ladder_failed_run_tombstone",
        "run_id": manifest.get("run_id"),
        "topology_stage": manifest.get("topology_stage"),
        "immutable_plan": manifest.get("immutable_plan"),
        "first_failed_at": str(manifest.get("updated_at") or _now()),
        "manifest_path": str(manifest_path),
        "first_failure_error": _json_safe(manifest.get("error")),
        "retry_forbidden": True,
    }
    with suppress(FileExistsError):
        _write_immutable_json(path, document)
    return _validate_failure_tombstone(
        path,
        run_id=manifest.get("run_id"),
        topology_stage=manifest.get("topology_stage"),
        immutable_plan=manifest.get("immutable_plan"),
    )


def _manifest_summary(manifest: Mapping[str, Any], condition_count: int) -> dict[str, Any]:
    attempts = [item for item in manifest.get("attempts", []) if isinstance(item, Mapping)]
    complete = [item for item in attempts if item.get("status") == "complete"]
    return {
        "planned_conditions": condition_count,
        "attempted_conditions": len(attempts),
        "completed_conditions": len(complete),
        "remaining_conditions": condition_count - len(complete),
        "measurement_quality_passed": sum(
            item.get("outcome") == "measurement_quality_passed" for item in complete
        ),
        "measurement_quality_rejected": sum(
            item.get("outcome") == "measurement_quality_rejected" for item in complete
        ),
        "failed_conditions": sum(item.get("status") == "failed" for item in attempts),
        "quarantine_count": sum(bool(item.get("quarantine")) for item in attempts),
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
    }


def _persist_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    condition_count: int,
) -> None:
    manifest["updated_at"] = _now()
    manifest["summary"] = _manifest_summary(manifest, condition_count)
    tombstone = _failure_tombstone_path(path)
    if tombstone.exists() or tombstone.is_symlink():
        _validate_failure_tombstone(
            tombstone,
            run_id=manifest.get("run_id"),
            topology_stage=manifest.get("topology_stage"),
            immutable_plan=manifest.get("immutable_plan"),
        )
        if manifest.get("status") != "failed":
            raise LeakageLadderError("failed-run tombstone forbids manifest rollback or retry")
    elif manifest.get("status") == "failed":
        _ensure_failure_tombstone(path, manifest)
    write_json_atomic(path, manifest)


def _load_manifest(
    path: Path,
    *,
    plan_path: Path,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    document = _read_json(path, "leakage-ladder manifest")
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    if (
        document.get("schema") != 1
        or document.get("run_kind") != "5g8_marker_independent_coherent_leakage_ladder"
        or document.get("run_id") != contract.get("run_id")
        or document.get("topology_stage") != contract.get("topology_stage")
        or document.get("selector_calibration_claim") is not False
        or document.get("causal_attribution_claim") is not False
    ):
        raise LeakageLadderError("manifest identity differs from the immutable plan")
    if document.get("immutable_plan") != _plan_file_evidence(plan_path, envelope):
        raise LeakageLadderError("manifest immutable plan hashes differ from plan bytes/contract")
    tombstone = _failure_tombstone_path(path)
    if tombstone.exists() or tombstone.is_symlink():
        _validate_failure_tombstone(
            tombstone,
            run_id=document.get("run_id"),
            topology_stage=document.get("topology_stage"),
            immutable_plan=document.get("immutable_plan"),
        )
        if document.get("status") != "failed":
            raise LeakageLadderError(
                "failed-run tombstone proves the manifest was deleted or rolled back"
            )
    list_fields = (
        "confirmations",
        "native_runtime_preflight_attempts",
        "fixture_evidence_preflight_attempts",
        "selector_initial_state_attempts",
        "identity_preflight_attempts",
        "preflight_mute_attempts",
        "attempts",
        "recovery_mute_attempts",
        "recovery_selector_cleanup_attempts",
        "orphan_quarantine_attempts",
        "final_mute_attempts",
        "final_selector_cleanup_attempts",
    )
    if any(not isinstance(document.get(field), list) for field in list_fields):
        raise LeakageLadderError("manifest progress arrays are malformed")
    _validate_manifest_selector_history(
        document,
        selector_control=contract.get("selector_control"),
    )
    return document


def _validate_confirmations(
    *,
    stage: str,
    confirm_stage: str | None,
    topology_token: str | None,
    no_antennas: bool,
    tx1_matched: bool,
    tx2_terminated_muted: bool,
    rx1_conducted_reference: bool,
    no_movement: bool,
    fixture_evidence: Mapping[str, Any],
    selector_static_all_off: bool = False,
) -> dict[str, Any]:
    if stage not in STAGE_CONTRACTS:
        raise ValueError(f"unsupported topology stage: {stage}")
    if not no_antennas:
        raise LeakageLadderError("execution requires --confirm-no-antennas")
    if not tx1_matched:
        raise LeakageLadderError("execution requires --confirm-tx1-matched-conducted")
    if not tx2_terminated_muted:
        raise LeakageLadderError("execution requires --confirm-tx2-terminated-muted")
    if not rx1_conducted_reference:
        raise LeakageLadderError("execution requires --confirm-rx1-conducted-reference")
    if not no_movement:
        raise LeakageLadderError("execution requires --confirm-no-movement")
    if confirm_stage != stage:
        raise LeakageLadderError(f"execution requires --confirm-stage {stage}")
    expected_token = str(STAGE_CONTRACTS[stage]["confirmation_token"])
    if topology_token != expected_token:
        raise LeakageLadderError(f"execution requires --confirm-topology-token {expected_token}")
    if stage in SELECTOR_CONNECTED_STAGES and not selector_static_all_off:
        raise LeakageLadderError(
            "selector-connected execution requires --confirm-selector-static-all-off"
        )
    if stage not in SELECTOR_CONNECTED_STAGES and selector_static_all_off:
        raise LeakageLadderError(
            "selector-disconnected execution must not confirm selector ALL_OFF"
        )
    if fixture_evidence.get("stage") != stage:
        raise LeakageLadderError("no-movement confirmation fixture stage is inconsistent")
    setup = fixture_evidence.get("setup_attestation")
    if not isinstance(setup, Mapping):
        raise LeakageLadderError("no-movement confirmation lacks per-run setup evidence")
    return {
        "confirmed_at": _now(),
        "stage": stage,
        "topology_confirmation_token": expected_token,
        "no_antennas_anywhere": True,
        "tx1_matched_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "no_component_or_connection_movement_since_setup_attestation": True,
        "fixture_evidence_sha256": canonical_json_sha256(fixture_evidence),
        "shared_fixture_sha256": fixture_evidence["shared_fixture_sha256"],
        "stage_delta_sha256": fixture_evidence["stage_delta_sha256"],
        "setup_attestation_sha256": setup["setup_attestation_file"]["sha256"],
        "setup_evidence_sha256": setup["setup_evidence"]["sha256"],
        "observed_component_ids": list(fixture_evidence["component_ids"]),
        "observed_connection_ids": list(fixture_evidence["connection_ids"]),
        "campaign_id": fixture_evidence["campaign_id"],
        "comparable_fixture_group_id": fixture_evidence["comparable_fixture_group_id"],
        "prior_stage_binding": fixture_evidence["prior_stage_binding"],
        "selector_static_all_off_physically_expected": stage in SELECTOR_CONNECTED_STAGES,
        "confirmation_method": "explicit CLI flags after physical inspection",
    }


def _confirmation_fixture_binding_passed(
    confirmation: Mapping[str, Any],
    fixture_evidence: Mapping[str, Any],
) -> bool:
    setup = fixture_evidence.get("setup_attestation")
    if not isinstance(setup, Mapping):
        return False
    setup_file = setup.get("setup_attestation_file")
    setup_evidence = setup.get("setup_evidence")
    return (
        confirmation.get("stage") == fixture_evidence.get("stage")
        and confirmation.get("no_component_or_connection_movement_since_setup_attestation") is True
        and confirmation.get("fixture_evidence_sha256") == canonical_json_sha256(fixture_evidence)
        and confirmation.get("shared_fixture_sha256")
        == fixture_evidence.get("shared_fixture_sha256")
        and confirmation.get("stage_delta_sha256") == fixture_evidence.get("stage_delta_sha256")
        and isinstance(setup_file, Mapping)
        and confirmation.get("setup_attestation_sha256") == setup_file.get("sha256")
        and isinstance(setup_evidence, Mapping)
        and confirmation.get("setup_evidence_sha256") == setup_evidence.get("sha256")
        and confirmation.get("observed_component_ids") == fixture_evidence.get("component_ids")
        and confirmation.get("observed_connection_ids") == fixture_evidence.get("connection_ids")
        and confirmation.get("campaign_id") == fixture_evidence.get("campaign_id")
        and confirmation.get("comparable_fixture_group_id")
        == fixture_evidence.get("comparable_fixture_group_id")
        and confirmation.get("prior_stage_binding") == fixture_evidence.get("prior_stage_binding")
    )


def _strict_mute(serial: str, purpose: str) -> dict[str, Any]:
    started_at = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_mute(boundary: MuteBoundary, serial: str, purpose: str) -> dict[str, Any]:
    try:
        result = boundary(serial, purpose)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": None,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": None,
            "completed_at": _now(),
            "error": {
                "type": "InvalidMuteAttestation",
                "message": "mute boundary did not return an object",
            },
        }
    return result


def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _live_identity_boundary(serial: str, requested_uri: str) -> dict[str, Any]:
    """Resolve the current USB context without opening a radio or changing RF state."""

    started_at = _now()
    resolved_uri = resolve_iio_uri(requested_uri, serial)
    return {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed" if resolved_uri == requested_uri else "failed",
        "serial": serial,
        "requested_uri": requested_uri,
        "resolved_uri": resolved_uri,
        "exact_uri_match": resolved_uri == requested_uri,
        "sysfs_path": find_usb_sysfs_path(serial),
        "scan_mutates_radio_state": False,
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_identity(
    boundary: IdentityBoundary,
    serial: str,
    requested_uri: str,
) -> dict[str, Any]:
    try:
        result = boundary(serial, requested_uri)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": None,
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": None,
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "completed_at": _now(),
            "error": {
                "type": "InvalidIdentityAttestation",
                "message": "identity boundary did not return an object",
            },
        }
    return result


def _identity_passed(value: object, *, serial: str, requested_uri: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "read_only_current_usb_uri_resolution"
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("requested_uri") == requested_uri
        and value.get("resolved_uri") == requested_uri
        and value.get("exact_uri_match") is True
        and value.get("scan_mutates_radio_state") is False
        and value.get("error") is None
    )


def _call_runtime_attestation(boundary: RuntimeAttestationBoundary) -> dict[str, Any]:
    return _shared_call_runtime_preflight(
        boundary,
        now=_now,
        error_document=_error_document,
    )


def _runtime_attestation_passed(
    value: object,
    *,
    expected: Mapping[str, Any],
) -> bool:
    return _shared_runtime_preflight_passed(value, expected=expected)


def _live_fixture_evidence_boundary(
    fixture_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    started_at = _now()
    source_files = fixture_evidence.get("source_files")
    if not isinstance(source_files, Mapping):
        raise LeakageLadderError("frozen fixture source-file evidence is malformed")
    manifest = source_files.get("fixture_manifest")
    setup = source_files.get("setup_attestation")
    if not isinstance(manifest, Mapping) or not isinstance(setup, Mapping):
        raise LeakageLadderError("frozen fixture manifest/setup evidence is malformed")
    observed = _fixture_evidence_from_manifests(
        Path(str(manifest.get("path", ""))),
        Path(str(setup.get("path", ""))),
        run_id=str(fixture_evidence.get("run_id", "")),
        board_id=str(fixture_evidence.get("board_id", "")),
        serial=str(
            fixture_evidence.get("shared_fixture", {}).get("pluto", {}).get("serial", "")
            if isinstance(fixture_evidence.get("shared_fixture"), Mapping)
            else ""
        ),
        stage=str(fixture_evidence.get("stage", "")),
    )
    exact_match = observed == dict(fixture_evidence)
    return {
        "schema": 1,
        "evidence_kind": "general_fixture_v2_preflight",
        "status": "passed" if exact_match else "failed",
        "fixture_evidence": observed,
        "fixture_evidence_sha256": canonical_json_sha256(observed),
        "exact_frozen_evidence_match": exact_match,
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_fixture_evidence(
    boundary: FixtureEvidenceBoundary,
    fixture_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    started_at = _now()
    try:
        result = boundary(fixture_evidence)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "general_fixture_v2_preflight",
            "status": "failed",
            "fixture_evidence": None,
            "fixture_evidence_sha256": None,
            "exact_frozen_evidence_match": False,
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "general_fixture_v2_preflight",
            "status": "failed",
            "fixture_evidence": None,
            "fixture_evidence_sha256": None,
            "exact_frozen_evidence_match": False,
            "started_at": started_at,
            "completed_at": _now(),
            "error": {
                "type": "InvalidFixtureEvidence",
                "message": "fixture boundary did not return an object",
            },
        }
    return result


def _fixture_evidence_passed(
    value: object,
    *,
    expected: Mapping[str, Any],
) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "general_fixture_v2_preflight"
        and value.get("status") == "passed"
        and value.get("fixture_evidence") == dict(expected)
        and value.get("fixture_evidence_sha256") == canonical_json_sha256(expected)
        and value.get("exact_frozen_evidence_match") is True
        and value.get("error") is None
    )


def _verify_selector_artifacts(selector_control: Mapping[str, Any]) -> None:
    for section_name, path_key, hash_key in (
        ("bench_manifest", "path", "file_sha256"),
        ("openocd_config", "path", "file_sha256"),
        ("control_profile", "path", "file_sha256"),
        ("control_profile", "header_path", "header_file_sha256"),
    ):
        section = selector_control.get(section_name)
        if not isinstance(section, Mapping):
            raise LeakageLadderError("selector-control artifact section is malformed")
        path = Path(str(section[path_key])).resolve(strict=True)
        if sha256_path(path) != section.get(hash_key):
            raise LeakageLadderError("selector-control artifact differs from immutable plan")


def _live_selector_all_off_boundary(
    selector_control: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    started_at = _now()
    _verify_selector_artifacts(selector_control)
    manifest_document = selector_control["bench_manifest"]
    config_document = selector_control["openocd_config"]
    profile_document = selector_control["control_profile"]
    command_document = selector_control["command"]
    assert isinstance(manifest_document, Mapping)
    assert isinstance(config_document, Mapping)
    assert isinstance(profile_document, Mapping)
    assert isinstance(command_document, Mapping)
    manifest = BenchManifest.load(Path(str(manifest_document["path"])))
    profile = load_profile(Path(str(profile_document["path"])))
    code = int(command_document["code"])
    if code != profile.all_off_code:
        raise LeakageLadderError("live profile ALL_OFF code differs from immutable plan")
    controller = OpenOcdBench(manifest, Path(str(config_document["path"])))
    pre_command = None
    commanded = None
    pre_command_document: dict[str, int | bool] | None
    if purpose in SELECTOR_READ_ONLY_PURPOSES:
        readback = controller.status()
        operation = "read_only"
    elif purpose in SELECTOR_COMMAND_PURPOSES | SELECTOR_CLEANUP_PURPOSES:
        pre_command = controller.status()
        initial_pre_command_passed = _selector_all_off_snapshot_passed(
            pre_command.as_dict(),
            expected_code=code,
        )
        operation = "command_all_off"
        if purpose in SELECTOR_COMMAND_PURPOSES and not initial_pre_command_passed:
            readback = pre_command
        else:
            commanded = controller.request(code, 0, wait_until_applied=True)
            readback = controller.status()
    else:
        raise ValueError("unsupported general-ladder selector boundary purpose")
    pre_command_document = pre_command.as_dict() if pre_command is not None else None
    commanded_document = commanded.as_dict() if commanded is not None else None
    readback_document = readback.as_dict()
    pre_command_passed = (
        _selector_all_off_snapshot_passed(pre_command_document, expected_code=code)
        if pre_command_document is not None
        else None
    )
    commanded_passed = (
        _selector_all_off_command_passed(commanded_document, expected_code=code)
        if commanded_document is not None
        else None
    )
    readback_passed = _selector_all_off_snapshot_passed(
        readback_document,
        expected_code=code,
    )
    passed = readback_passed
    if purpose in SELECTOR_COMMAND_PURPOSES:
        passed = pre_command_passed is True and commanded_passed is True and readback_passed
    elif purpose in SELECTOR_CLEANUP_PURPOSES:
        passed = commanded_passed is True and readback_passed
    return {
        "schema": 1,
        "evidence_kind": "static_selector_all_off_mailbox_readback",
        "purpose": purpose,
        "status": "passed" if passed else "failed",
        "all_off_code": code,
        "lease_ms": 0,
        "operation": operation,
        "command_was_issued": commanded is not None,
        "pre_command_was_all_off": pre_command_passed,
        "pre_command": pre_command_document,
        "commanded": commanded_document,
        "readback": readback_document,
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _selector_all_off_snapshot_passed(
    value: object,
    *,
    expected_code: object,
) -> bool:
    return (
        _selector_snapshot_well_formed(value)
        and isinstance(value, Mapping)
        and value.get("applied_code") == expected_code
        and value.get("command_code") == expected_code
        and value.get("command_lease_ms") == 0
        and value.get("command_sequence") == value.get("acknowledged_sequence")
        and value.get("command_valid") is True
        and value.get("lease_active") is False
        and value.get("remaining_lease_ms") == 0
        and value.get("guard_active") is False
        and value.get("invalid_command") is False
    )


def _selector_snapshot_well_formed(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    integer_fields = (
        "applied_code",
        "command_code",
        "command_lease_ms",
        "command_sequence",
        "acknowledged_sequence",
        "remaining_lease_ms",
    )
    boolean_fields = (
        "command_valid",
        "lease_active",
        "guard_active",
        "invalid_command",
    )
    return all(
        not isinstance(value.get(field), bool)
        and isinstance(value.get(field), int)
        and int(value[field]) >= 0
        for field in integer_fields
    ) and all(isinstance(value.get(field), bool) for field in boolean_fields)


def _selector_all_off_command_passed(
    value: object,
    *,
    expected_code: object,
) -> bool:
    return _selector_all_off_snapshot_passed(
        value,
        expected_code=expected_code,
    )


def _call_selector(
    boundary: SelectorBoundary,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    try:
        result = boundary(selector_control, purpose)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "all_off_code": selector_control.get("command", {}).get("code")
            if isinstance(selector_control.get("command"), Mapping)
            else None,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "completed_at": _now(),
            "error": {
                "type": "InvalidSelectorAttestation",
                "message": "selector boundary did not return an object",
            },
        }
    return result


def _selector_passed(
    value: object,
    *,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> bool:
    command = selector_control.get("command")
    if not isinstance(command, Mapping):
        return False
    expected_code = command.get("code")
    if not (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "static_selector_all_off_mailbox_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("all_off_code") == expected_code
        and value.get("lease_ms") == 0
        and value.get("error") is None
    ):
        return False
    if purpose in SELECTOR_READ_ONLY_PURPOSES:
        if (
            value.get("operation") != "read_only"
            or value.get("command_was_issued") is not False
            or value.get("pre_command") is not None
            or value.get("commanded") is not None
            or value.get("pre_command_was_all_off") is not None
        ):
            return False
    elif purpose in SELECTOR_COMMAND_PURPOSES:
        if (
            value.get("operation") != "command_all_off"
            or value.get("command_was_issued") is not True
            or value.get("pre_command_was_all_off") is not True
            or not _selector_all_off_snapshot_passed(
                value.get("pre_command"), expected_code=expected_code
            )
            or not _selector_all_off_command_passed(
                value.get("commanded"), expected_code=expected_code
            )
        ):
            return False
    elif purpose in SELECTOR_CLEANUP_PURPOSES:
        pre_command = value.get("pre_command")
        pre_command_was_all_off = value.get("pre_command_was_all_off")
        if (
            value.get("operation") != "command_all_off"
            or value.get("command_was_issued") is not True
            or not isinstance(pre_command_was_all_off, bool)
            or not _selector_snapshot_well_formed(pre_command)
            or pre_command_was_all_off
            is not _selector_all_off_snapshot_passed(
                pre_command,
                expected_code=expected_code,
            )
            or not _selector_all_off_command_passed(
                value.get("commanded"), expected_code=expected_code
            )
        ):
            return False
    else:
        return False
    readback = value.get("readback")
    return _selector_all_off_snapshot_passed(
        readback,
        expected_code=expected_code,
    )


def _validate_manifest_selector_history(
    manifest: Mapping[str, Any],
    *,
    selector_control: object,
) -> None:
    """Fail closed if any persisted selector boundary evidence was changed."""

    initial_attempts = manifest.get("selector_initial_state_attempts")
    recovery_attempts = manifest.get("recovery_selector_cleanup_attempts")
    final_attempts = manifest.get("final_selector_cleanup_attempts")
    if not all(
        isinstance(value, list) for value in (initial_attempts, recovery_attempts, final_attempts)
    ):
        raise LeakageLadderError("manifest selector evidence arrays are malformed")
    assert isinstance(initial_attempts, list)
    assert isinstance(recovery_attempts, list)
    assert isinstance(final_attempts, list)

    if selector_control is None:
        if (
            initial_attempts
            or recovery_attempts
            or final_attempts
            or manifest.get("selector_initial_state") is not None
            or manifest.get("final_selector_cleanup") is not None
        ):
            raise LeakageLadderError(
                "selector-disconnected manifest contains selector boundary evidence"
            )
        return
    if not isinstance(selector_control, Mapping):
        raise LeakageLadderError("selector-connected manifest lacks selector control")

    for evidence in initial_attempts:
        if not _selector_passed(
            evidence,
            selector_control=selector_control,
            purpose="initial_state_before_command",
        ):
            raise LeakageLadderError("persisted initial selector attestation is invalid")
    for evidence in recovery_attempts:
        if not _selector_passed(
            evidence,
            selector_control=selector_control,
            purpose="resume_cleanup_all_off",
        ):
            raise LeakageLadderError("persisted recovery selector attestation is invalid")
    for evidence in final_attempts:
        purpose = evidence.get("purpose") if isinstance(evidence, Mapping) else None
        if purpose not in {"final_cleanup_all_off", "exception_cleanup_all_off"} or not (
            _selector_passed(
                evidence,
                selector_control=selector_control,
                purpose=str(purpose),
            )
        ):
            raise LeakageLadderError("persisted final selector attestation is invalid")

    expected_initial = initial_attempts[-1] if initial_attempts else None
    expected_final = final_attempts[-1] if final_attempts else None
    if manifest.get("selector_initial_state") != expected_initial:
        raise LeakageLadderError("latest initial selector attestation binding is invalid")
    if manifest.get("final_selector_cleanup") != expected_final:
        raise LeakageLadderError("latest final selector attestation binding is invalid")


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


def _block_ledger(blocks: list[SampleBlockV2]) -> dict[str, Any] | None:
    if not blocks:
        return None
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


def _assert_path_chain_has_no_symlink(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise LeakageLadderError(f"{label} contains a symlink: {current}")


def _assert_tree_has_no_symlink(root: Path, *, label: str) -> Path:
    _assert_path_chain_has_no_symlink(root, label=label)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise LeakageLadderError(f"{label} is not a directory")
    for directory, directory_names, file_names in os.walk(resolved, followlinks=False):
        directory_path = Path(directory)
        for name in (*directory_names, *file_names):
            child = directory_path / name
            if child.is_symlink():
                raise LeakageLadderError(f"{label} contains a symlink: {child}")
            child_resolved = child.resolve(strict=True)
            if not child_resolved.is_relative_to(resolved):
                raise LeakageLadderError(f"{label} resolves outside its directory")
    return resolved


def _safe_quarantine_parent(capture_root: Path) -> tuple[Path, Path]:
    _assert_path_chain_has_no_symlink(capture_root, label="capture root")
    capture_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    exact_root = capture_root.resolve(strict=True)
    if not exact_root.is_dir():
        raise LeakageLadderError("capture root is not a directory")
    failed_root = capture_root / ".failed"
    _assert_path_chain_has_no_symlink(failed_root, label="quarantine root")
    failed_root.mkdir(mode=0o700, exist_ok=True)
    exact_failed = failed_root.resolve(strict=True)
    if exact_failed.parent != exact_root or not exact_failed.is_dir():
        raise LeakageLadderError("quarantine root resolves outside the capture root")
    return exact_root, exact_failed


def _quarantine_inventory(root: Path, artifact_id: str) -> dict[str, Any]:
    _assert_tree_has_no_symlink(root, label="quarantine directory")
    files = [
        {
            "name": path.name,
            "path": str(path),
            "sha256": sha256_path(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(root.iterdir())
        if path.is_file()
    ]
    return {
        "artifact_id": artifact_id,
        "path": str(root),
        "accepted": False,
        "may_be_used_for_selector_calibration": False,
        "files": files,
    }


def _persist_memory_quarantine(
    capture_root: Path,
    *,
    blocks: list[SampleBlockV2],
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_id = uuid.uuid4().hex
    _, failed_root = _safe_quarantine_parent(capture_root)
    temporary = failed_root / f".{artifact_id}.partial"
    destination = failed_root / f"{artifact_id}.failed"
    if (
        temporary.exists()
        or temporary.is_symlink()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise LeakageLadderError("quarantine destination already exists")
    temporary.mkdir(mode=0o700)
    data_file = temporary / f"{artifact_id}.sigmf-data"
    digest = hashlib.sha256()
    with data_file.open("xb") as stream:
        for block in blocks:
            wire = complex_to_ci16(block.samples).tobytes(order="C")
            stream.write(wire)
            digest.update(wire)
        stream.flush()
        os.fsync(stream.fileno())
    ledger = _block_ledger(blocks)
    metadata: dict[str, Any] = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": SAMPLE_RATE_HZ,
            "core:num_channels": 2,
            "pluto:artifact_id": artifact_id,
            "pluto:sha256": digest.hexdigest(),
        },
        "pluto:capture": {
            "sample_count": sum(block.sample_count for block in blocks),
            "receiver_count": 2,
            "incomplete": True,
        },
        "smateway:quarantine": {
            "accepted": False,
            "may_be_used_for_selector_calibration": False,
            "automatic_retry_attempted": False,
            "error": _error_document(error),
            "context": _json_safe(context),
        },
    }
    if ledger is not None:
        metadata["pluto:continuity"] = ledger
    write_json_atomic(temporary / f"{artifact_id}.sigmf-meta", metadata)
    failure = {
        "schema": 1,
        "failure_kind": "5g8_leakage_ladder_capture_quarantine",
        "artifact_id": artifact_id,
        "accepted": False,
        "automatic_retry_attempted": False,
        "may_be_used_for_selector_calibration": False,
        "error": _error_document(error),
        "retained_frame_count": len(blocks),
        "retained_sample_count": sum(block.sample_count for block in blocks),
        "context": _json_safe(context),
        "created_at": _now(),
    }
    write_json_atomic(temporary / "failure.json", failure)
    _fsync_directory(temporary)
    os.replace(temporary, destination)
    _fsync_directory(failed_root)
    return _quarantine_inventory(destination, artifact_id)


def _seal_failed_directory(
    root: Path,
    *,
    artifact_id: str,
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    _assert_path_chain_has_no_symlink(root, label="failed artifact directory")
    root.mkdir(parents=True, exist_ok=True)
    _assert_tree_has_no_symlink(root, label="failed artifact directory")
    write_json_atomic(
        root / "leakage-ladder-quarantine.json",
        {
            "schema": 1,
            "failure_kind": "5g8_leakage_ladder_post_capture_quarantine",
            "artifact_id": artifact_id,
            "accepted": False,
            "may_be_used_for_selector_calibration": False,
            "automatic_retry_attempted": False,
            "error": _error_document(error),
            "context": _json_safe(context),
            "created_at": _now(),
        },
    )
    return _quarantine_inventory(root, artifact_id)


def _rf_readback_evidence(capture: Any, plan: SafeDdsTonePlan) -> dict[str, Any]:
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
        planned_tx_gain_db=plan.tx_hardware_gain_db,
        planned_dds_scale=DDS_SCALE,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return evidence


def _active_tone_readback_hz(evidence: Mapping[str, Any]) -> float:
    raw = evidence.get("dds_frequency_readback_hz")
    if not isinstance(raw, list) or len(raw) != 8:
        raise LeakageLadderError("DDS frequency readback is malformed")
    active = (abs(float(raw[0])), abs(float(raw[2])))
    tolerance = math.ceil(SAMPLE_RATE_HZ / (1 << 16))
    if abs(active[0] - active[1]) > tolerance:
        raise LeakageLadderError("TX1 I/Q DDS frequency readbacks disagree")
    return sum(active) / 2.0


def _validate_capture_result(
    capture: Any,
    retained: list[SampleBlockV2],
    *,
    plan: SafeDdsTonePlan,
    settings: RadioSettings,
    forbidden_stream_ids: set[int],
) -> tuple[int, dict[str, Any], float]:
    if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
        raise LeakageLadderError("capture identity differs from exact serial/current USB URI")
    if capture.settings != settings:
        raise LeakageLadderError("capture settings readback differs from the frozen condition")
    if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
        raise LeakageLadderError("capture sample/frame count differs from the frozen condition")
    if capture.kernel_buffers != KERNEL_BUFFERS:
        raise LeakageLadderError("kernel-buffer readback differs from exact value eight")
    if len(retained) != FRAME_COUNT:
        raise LeakageLadderError("not every metadata frame was retained")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in retained):
        raise LeakageLadderError("retained data is not canonical dual-RX 100k-sample frames")
    ledger = _block_ledger(retained)
    if ledger is None:
        raise LeakageLadderError("capture has no ABI2 continuity ledger")
    summary = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if summary.metadata_abi != 2 or summary.first_buffer_sequence != 0:
        raise LeakageLadderError("capture did not begin a fresh ABI2 stream")
    if summary.stream_id in forbidden_stream_ids:
        raise LeakageLadderError("fresh condition reused an earlier stream ID")
    for proof, block in zip(capture.frames, retained, strict=True):
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
            raise LeakageLadderError("capture frame proof differs from retained ABI2 block")
    evidence = _rf_readback_evidence(capture, plan)
    return summary.stream_id, evidence, _active_tone_readback_hz(evidence)


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


def _capture_condition(
    condition: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
    forbidden_stream_ids: set[int],
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_mute,
    selector_boundary: SelectorBoundary = _live_selector_all_off_boundary,
) -> dict[str, Any]:
    plan = _tone_plan(condition, contract)
    settings = RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    context: dict[str, Any] = {
        "condition": dict(condition),
        "topology_stage": contract["topology_stage"],
        "stage_contract": contract["stage_contract"],
        "native_libiio_runtime_attestation": contract["source"].get(
            "native_libiio_runtime_attestation"
        ),
        "native_libiio_runtime_attestation_sha256": contract["source"].get(
            "native_libiio_runtime_attestation_sha256"
        ),
        "fixture_evidence": contract.get("fixture_evidence"),
        "fixture_evidence_sha256": contract.get("fixture_evidence_sha256"),
        "immutable_plan": dict(plan_evidence),
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
    }
    stage = str(contract["topology_stage"])
    raw_selector_control = contract.get("selector_control")
    selector_control = raw_selector_control if isinstance(raw_selector_control, Mapping) else None
    selector_before: dict[str, Any] | None = None
    selector_after: dict[str, Any] | None = None
    selector_cleanup: dict[str, Any] | None = None
    capture: Any | None = None
    capture_error: BaseException | None = None
    try:
        if stage in SELECTOR_CONNECTED_STAGES:
            if selector_control is None:
                raise LeakageLadderError(
                    "selector-connected condition lacks frozen static ALL_OFF control"
                )
            selector_before = _call_selector(
                selector_boundary,
                selector_control,
                "before_condition",
            )
            context["selector_static_all_off_before"] = selector_before
            if not _selector_passed(
                selector_before,
                selector_control=selector_control,
                purpose="before_condition",
            ):
                raise LeakageLadderError("selector static ALL_OFF pre-condition failed")
        elif selector_control is not None:
            raise LeakageLadderError("selector-disconnected condition included selector control")
        capture = capture_boundary(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        capture_error = error
    finally:
        post_mute = _call_mute(mute_boundary, plan.serial, "post_condition")
        context["post_condition_exact_serial_mute"] = post_mute
        if selector_control is not None:
            selector_after = _call_selector(
                selector_boundary,
                selector_control,
                "after_condition",
            )
            context["selector_static_all_off_after"] = selector_after
            selector_cleanup = _call_selector(
                selector_boundary,
                selector_control,
                "condition_cleanup_all_off",
            )
            context["selector_static_all_off_cleanup"] = selector_cleanup

    post_mute_passed = _mute_passed(post_mute, serial=plan.serial, purpose="post_condition")
    selector_after_passed = selector_control is None or _selector_passed(
        selector_after,
        selector_control=selector_control,
        purpose="after_condition",
    )
    selector_cleanup_passed = selector_control is None or _selector_passed(
        selector_cleanup,
        selector_control=selector_control,
        purpose="condition_cleanup_all_off",
    )
    if (
        capture_error is not None
        or not post_mute_passed
        or not selector_after_passed
        or not selector_cleanup_passed
    ):
        if capture_error is not None:
            failure = capture_error
        elif not post_mute_passed:
            failure = LeakageLadderError("exact post-condition mute attestation failed")
        elif not selector_after_passed:
            failure = LeakageLadderError("selector static ALL_OFF post-condition failed")
        else:
            failure = LeakageLadderError("selector static ALL_OFF cleanup failed")
        quarantine = _persist_memory_quarantine(
            capture_root,
            blocks=retained,
            error=failure,
            context=context,
        )
        retained.clear()
        raise ConditionCaptureFailure(
            str(failure),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from capture_error
    assert capture is not None

    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        stream_id, rf_readback, tone_readback_hz = _validate_capture_result(
            capture,
            retained,
            plan=plan,
            settings=settings,
            forbidden_stream_ids=forbidden_stream_ids,
        )
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        context["adc_headroom_admission"] = asdict(headroom)
        if not headroom.passed:
            raise LeakageLadderError(
                "ADC headroom admission failed; stronger ladder conditions are forbidden"
            )

        rx1 = np.concatenate([block.samples[0] for block in retained])
        rx2 = np.concatenate([block.samples[1] for block in retained])
        pilot = estimate_coherent_pilot_offset(
            rx1,
            sample_rate_hz=SAMPLE_RATE_HZ,
            nominal_tone_offset_hz=tone_readback_hz,
        )
        pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
        pilot_rejection_reasons: list[str] = []
        if pilot.confidence < MINIMUM_PILOT_CONFIDENCE:
            pilot_rejection_reasons.append("rx1_pilot_confidence_below_minimum")
        if pilot.phase_step_coherence < MINIMUM_PILOT_PHASE_STEP_COHERENCE:
            pilot_rejection_reasons.append("rx1_pilot_phase_step_coherence_below_minimum")
        if pilot_phase_rms_deg > MAXIMUM_PILOT_PHASE_RMS_DEG:
            pilot_rejection_reasons.append("rx1_pilot_phase_rms_above_maximum")
        analysis = analyze_coherent_leakage(
            rx1,
            rx2,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=pilot.estimated_offset_hz,
        )
        measurement_rejection_reasons = [
            *pilot_rejection_reasons,
            *analysis.quality_rejection_reasons,
        ]
        measurement_quality_passed = not measurement_rejection_reasons
        del rx1, rx2

        writer = CaptureWriter(
            capture_root,
            radio=capture.identity,
            settings=settings,
            label=(
                "EXPLORATORY marker-independent 5.8 GHz leakage diagnostic "
                f"{contract['topology_stage']} TX1={plan.tx_hardware_gain_db:g}dB"
            ),
        )
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise LeakageLadderError("finalized SigMF data failed SHA-256 verification")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        if continuity["stream_id"] != stream_id or continuity["metadata_abi"] != 2:
            raise LeakageLadderError("persisted continuity identity differs from live capture")

        evidence = _artifact_evidence(artifact)
        analysis_document = _json_safe(asdict(analysis))
        record = {
            "schema": 1,
            "record_kind": "5g8_marker_independent_coherent_leakage_condition",
            "created_at": _now(),
            "accepted_raw_artifact": False,
            "accepted_raw_artifact_pending_manifest_commit": True,
            "standalone_condition_record_is_not_acceptance": True,
            "acceptance_authority": (
                "plan-bound complete manifest attempt plus artifact revalidation"
            ),
            "accepted_for_selector_calibration": False,
            "may_be_used_as_selector_calibration": False,
            "causal_attribution_claim": False,
            "causal_attribution_fixture_eligible": contract["interpretation"][
                "causal_attribution_fixture_eligible"
            ],
            "immutable_plan": {
                **dict(plan_evidence),
            },
            "native_libiio_runtime_attestation": contract["source"][
                "native_libiio_runtime_attestation"
            ],
            "native_libiio_runtime_attestation_sha256": contract["source"][
                "native_libiio_runtime_attestation_sha256"
            ],
            "fixture_evidence": contract.get("fixture_evidence"),
            "fixture_evidence_sha256": contract.get("fixture_evidence_sha256"),
            "condition": dict(condition),
            "topology": {
                "stage": contract["topology_stage"],
                "contract": contract["stage_contract"],
                "operator_confirmation_required_by_plan": contract[
                    "operator_confirmations_required"
                ],
            },
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": evidence,
            "capture": {
                "serial": plan.serial,
                "uri": plan.uri,
                "center_frequency_hz": CENTER_FREQUENCY_HZ,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "receiver_gain_db": RECEIVER_GAIN_DB,
                "tx_channel": 0,
                "tx_port": "TX1",
                "tx2_required_exact_muted": True,
                "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
                "dds_scale_requested": DDS_SCALE,
                "tone_offset_hz_requested": TONE_OFFSET_HZ,
                "tone_offset_hz_readback": tone_readback_hz,
                "tone_offset_hz_measured": pilot.estimated_offset_hz,
                "pilot_frequency_refinement": {
                    **_json_safe(asdict(pilot)),
                    "phase_residual_rms_deg": pilot_phase_rms_deg,
                    "minimum_confidence": MINIMUM_PILOT_CONFIDENCE,
                    "minimum_phase_step_coherence": MINIMUM_PILOT_PHASE_STEP_COHERENCE,
                    "maximum_phase_rms_deg": MAXIMUM_PILOT_PHASE_RMS_DEG,
                    "quality_passed": not pilot_rejection_reasons,
                    "quality_rejection_reasons": pilot_rejection_reasons,
                },
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FRAME_COUNT,
                "sample_count": TOTAL_SAMPLES,
                "kernel_buffers": KERNEL_BUFFERS,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
                "rf_readback_evidence": rf_readback,
                "adc_headroom_admission": asdict(headroom),
            },
            "continuity_audit": continuity,
            "marker_independent_analysis": analysis_document,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "safety": {
                "post_condition_exact_serial_mute": post_mute,
                "persistence_began_only_after_helper_returned_and_exact_mute_passed": True,
                "tx1_only_readback_validated": True,
                "tx2_minus80db_and_zero_dds_scales_validated": True,
                "fresh_stream_validated": True,
                "selector_static_all_off_before": selector_before,
                "selector_static_all_off_after": selector_after,
                "selector_static_all_off_cleanup": selector_cleanup,
                "automatic_retry_count": 0,
                "selector_calibration_claim": False,
            },
            "interpretation": (
                "Topology leakage diagnostic only; this record does not measure or calibrate "
                "selector state paths."
            ),
        }
        record_path = Path(artifact.path) / CONDITION_RECORD_NAME
        write_json_atomic(record_path, record)
        return {
            "condition_id": condition["condition_id"],
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_data_path": evidence["data_path"],
            "artifact_data_sha256": evidence["data_sha256"],
            "artifact_metadata_path": evidence["metadata_path"],
            "artifact_metadata_sha256": evidence["metadata_sha256"],
            "condition_record_path": str(record_path),
            "condition_record_sha256": sha256_path(record_path),
            "immutable_plan": dict(plan_evidence),
            "topology_stage": contract["topology_stage"],
            "tx_hardware_gain_db": condition["tx_hardware_gain_db"],
            "attribution_repeat_index": condition.get("attribution_repeat_index"),
            "attribution_repeat_count": condition.get("attribution_repeat_count"),
            "native_libiio_runtime_attestation": contract["source"][
                "native_libiio_runtime_attestation"
            ],
            "native_libiio_runtime_attestation_sha256": contract["source"][
                "native_libiio_runtime_attestation_sha256"
            ],
            "fixture_evidence": contract.get("fixture_evidence"),
            "fixture_evidence_sha256": contract.get("fixture_evidence_sha256"),
            "stream_id": stream_id,
            "metadata_abi": 2,
            "headroom_passed": headroom.passed,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "tone_offset_hz_readback": tone_readback_hz,
            "tone_offset_hz_measured": pilot.estimated_offset_hz,
            "pilot_confidence": pilot.confidence,
            "pilot_phase_step_coherence": pilot.phase_step_coherence,
            "pilot_phase_residual_rms_deg": pilot_phase_rms_deg,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "rx2_over_rx1": analysis_document["rx2_over_rx1"],
            "post_condition_exact_serial_mute": post_mute,
            "selector_static_all_off_before": selector_before,
            "selector_static_all_off_after": selector_after,
            "selector_static_all_off_cleanup": selector_cleanup,
            "selector_calibration_claim": False,
            "causal_attribution_claim": False,
            "causal_attribution_fixture_eligible": contract["interpretation"][
                "causal_attribution_fixture_eligible"
            ],
        }
    except BaseException as error:
        context["post_capture_error"] = _error_document(error)
        if artifact is not None:
            source = Path(artifact.path)
            exact_root, failed_root = _safe_quarantine_parent(capture_root)
            exact_source = _assert_tree_has_no_symlink(
                source,
                label="failed finalized artifact",
            )
            if exact_source.parent != exact_root:
                raise LeakageLadderError("failed artifact resolves outside capture root") from error
            destination = failed_root / f"{artifact.artifact_id}.failed"
            if destination.exists() or destination.is_symlink():
                raise LeakageLadderError("failed artifact quarantine destination exists") from error
            os.replace(exact_source, destination)
            quarantine = _seal_failed_directory(
                destination,
                artifact_id=artifact.artifact_id,
                error=error,
                context=context,
            )
        elif writer is not None:
            finalized_candidate = capture_root / writer.artifact_id
            if finalized_candidate.exists():
                exact_root, failed_root = _safe_quarantine_parent(capture_root)
                exact_candidate = _assert_tree_has_no_symlink(
                    finalized_candidate,
                    label="failed writer artifact",
                )
                if exact_candidate.parent != exact_root:
                    raise LeakageLadderError(
                        "writer artifact resolves outside capture root"
                    ) from error
                destination = failed_root / f"{writer.artifact_id}.failed"
                if destination.exists() or destination.is_symlink():
                    raise LeakageLadderError("writer quarantine destination exists") from error
                os.replace(exact_candidate, destination)
            else:
                destination = writer.fail(error)
            quarantine = _seal_failed_directory(
                destination,
                artifact_id=writer.artifact_id,
                error=error,
                context=context,
            )
        else:
            quarantine = _persist_memory_quarantine(
                capture_root,
                blocks=retained,
                error=error,
                context=context,
            )
        raise ConditionCaptureFailure(
            str(error),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from error
    finally:
        retained.clear()


def _verify_completed_result_files(
    result: Mapping[str, Any],
    *,
    condition: Mapping[str, Any],
    contract: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
) -> None:
    """Revalidate every accepted byte and semantic binding before resume skips it."""

    if result.get("immutable_plan") != plan_evidence:
        raise LeakageLadderError("completed result differs from immutable plan evidence")
    try:
        raw_artifact_root = Path(str(result["artifact_path"]))
        raw_data_file = Path(str(result["artifact_data_path"]))
        raw_metadata_file = Path(str(result["artifact_metadata_path"]))
        raw_record_file = Path(str(result["condition_record_path"]))
        _assert_path_chain_has_no_symlink(capture_root, label="capture root")
        for raw_path, label in (
            (raw_artifact_root, "completed artifact root"),
            (raw_data_file, "completed artifact data"),
            (raw_metadata_file, "completed artifact metadata"),
            (raw_record_file, "completed condition record"),
        ):
            _assert_path_chain_has_no_symlink(raw_path, label=label)
        exact_capture_root = capture_root.resolve(strict=True)
        artifact_root = _assert_tree_has_no_symlink(
            raw_artifact_root,
            label="completed artifact",
        )
        data_file = raw_data_file.resolve(strict=True)
        metadata_file = raw_metadata_file.resolve(strict=True)
        record_file = raw_record_file.resolve(strict=True)
    except (KeyError, OSError, RuntimeError) as error:
        raise LeakageLadderError("completed result artifact path is missing") from error
    if (
        not artifact_root.is_dir()
        or artifact_root.parent != exact_capture_root
        or data_file.parent != artifact_root
        or metadata_file.parent != artifact_root
        or record_file.parent != artifact_root
        or record_file.name != CONDITION_RECORD_NAME
        or not data_file.is_file()
        or not metadata_file.is_file()
        or not record_file.is_file()
    ):
        raise LeakageLadderError("completed result artifact layout is invalid")
    for path, key in (
        (data_file, "artifact_data_sha256"),
        (metadata_file, "artifact_metadata_sha256"),
        (record_file, "condition_record_sha256"),
    ):
        expected_sha = _validate_sha256(result.get(key), key)
        if sha256_path(path) != expected_sha:
            raise LeakageLadderError("completed result artifact hash differs from evidence")
    record = _read_json(record_file, "leakage condition record")
    artifact_document = record.get("artifact")
    artifact_evidence = record.get("artifact_evidence")
    topology = record.get("topology")
    capture = record.get("capture")
    continuity_record = record.get("continuity_audit")
    analysis = record.get("marker_independent_analysis")
    safety = record.get("safety")
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise LeakageLadderError("completed result plan configuration is malformed")
    try:
        artifact = ArtifactSummary.model_validate(artifact_document)
        if (
            artifact.artifact_id != result.get("artifact_id")
            or Path(artifact.path).resolve(strict=True) != artifact_root
            or artifact.sha256 != result.get("artifact_data_sha256")
            or not verify_artifact(artifact)
        ):
            raise LeakageLadderError("completed SigMF artifact verification failed")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=int(configuration["sample_count_per_condition"]),
            expected_samples_per_block=int(configuration["samples_per_frame"]),
            expected_sample_rate_hz=float(configuration["sample_rate_hz"]),
        )
    except (OSError, TypeError, ValueError) as error:
        raise LeakageLadderError("completed SigMF artifact/ABI2 audit failed") from error
    pilot = capture.get("pilot_frequency_refinement") if isinstance(capture, Mapping) else None
    headroom = capture.get("adc_headroom_admission") if isinstance(capture, Mapping) else None
    rf_readback = capture.get("rf_readback_evidence") if isinstance(capture, Mapping) else None
    if not isinstance(rf_readback, Mapping):
        raise LeakageLadderError("completed result lacks RF readback evidence")
    try:
        validate_tx1_rf_readback_evidence(
            rf_readback,
            planned_kernel_buffers=int(configuration["kernel_buffers"]),
            planned_tx_gain_db=float(condition["tx_hardware_gain_db"]),
            planned_dds_scale=float(configuration["dds_scale"]),
            planned_tone_hz=float(configuration["tone_offset_hz_requested"]),
            sample_rate_hz=float(configuration["sample_rate_hz"]),
        )
        active_tone_hz = _active_tone_readback_hz(rf_readback)
    except (KeyError, RuntimeError, TypeError, ValueError) as error:
        raise LeakageLadderError("completed RF readback is invalid") from error
    fixture = contract.get("fixture_evidence")
    source = contract.get("source")
    rx2 = analysis.get("rx2") if isinstance(analysis, Mapping) else None
    stage = contract.get("topology_stage")
    selector_control = contract.get("selector_control")
    selector_before = result.get("selector_static_all_off_before")
    selector_after = result.get("selector_static_all_off_after")
    selector_cleanup = result.get("selector_static_all_off_cleanup")
    if stage in SELECTOR_CONNECTED_STAGES:
        selector_evidence_passed = (
            isinstance(selector_control, Mapping)
            and _selector_passed(
                selector_before,
                selector_control=selector_control,
                purpose="before_condition",
            )
            and _selector_passed(
                selector_after,
                selector_control=selector_control,
                purpose="after_condition",
            )
            and _selector_passed(
                selector_cleanup,
                selector_control=selector_control,
                purpose="condition_cleanup_all_off",
            )
        )
    else:
        selector_evidence_passed = (
            selector_control is None
            and selector_before is None
            and selector_after is None
            and selector_cleanup is None
        )
    if (
        record.get("schema") != 1
        or record.get("record_kind") != "5g8_marker_independent_coherent_leakage_condition"
        or record.get("accepted_raw_artifact") is not False
        or record.get("accepted_raw_artifact_pending_manifest_commit") is not True
        or record.get("standalone_condition_record_is_not_acceptance") is not True
        or record.get("acceptance_authority")
        != "plan-bound complete manifest attempt plus artifact revalidation"
        or record.get("accepted_for_selector_calibration") is not False
        or record.get("may_be_used_as_selector_calibration") is not False
        or record.get("causal_attribution_claim") is not False
        or record.get("immutable_plan") != plan_evidence
        or record.get("condition") != dict(condition)
        or not isinstance(source, Mapping)
        or record.get("native_libiio_runtime_attestation")
        != source.get("native_libiio_runtime_attestation")
        or record.get("native_libiio_runtime_attestation_sha256")
        != source.get("native_libiio_runtime_attestation_sha256")
        or record.get("fixture_evidence") != fixture
        or record.get("fixture_evidence_sha256") != contract.get("fixture_evidence_sha256")
        or not isinstance(artifact_evidence, Mapping)
        or artifact_evidence.get("artifact_id") != result.get("artifact_id")
        or artifact_evidence.get("path") != str(artifact_root)
        or artifact_evidence.get("data_path") != str(data_file)
        or artifact_evidence.get("data_sha256") != result.get("artifact_data_sha256")
        or artifact_evidence.get("metadata_path") != str(metadata_file)
        or artifact_evidence.get("metadata_sha256") != result.get("artifact_metadata_sha256")
        or not isinstance(topology, Mapping)
        or topology.get("stage") != contract.get("topology_stage")
        or topology.get("contract") != contract.get("stage_contract")
        or topology.get("operator_confirmation_required_by_plan")
        != contract.get("operator_confirmations_required")
        or not isinstance(capture, Mapping)
        or capture.get("serial") != configuration.get("serial")
        or capture.get("uri") != configuration.get("uri")
        or capture.get("center_frequency_hz") != configuration.get("center_frequency_hz")
        or capture.get("sample_rate_hz") != configuration.get("sample_rate_hz")
        or capture.get("bandwidth_hz") != configuration.get("bandwidth_hz")
        or capture.get("receiver_gain_db") != configuration.get("receiver_gain_db")
        or capture.get("tx_channel") != configuration.get("tx_channel")
        or capture.get("tx_port") != configuration.get("tx_port")
        or capture.get("tx2_required_exact_muted") != configuration.get("tx2_required_exact_muted")
        or capture.get("tx_hardware_gain_db_requested") != condition.get("tx_hardware_gain_db")
        or capture.get("dds_scale_requested") != configuration.get("dds_scale")
        or capture.get("samples_per_frame") != configuration.get("samples_per_frame")
        or capture.get("frame_count") != configuration.get("frame_count")
        or capture.get("sample_count") != configuration.get("sample_count_per_condition")
        or capture.get("kernel_buffers") != configuration.get("kernel_buffers")
        or capture.get("metadata_abi") != 2
        or capture.get("stream_id") != result.get("stream_id")
        or capture.get("tone_offset_hz_requested") != result.get("tone_offset_hz_requested")
        or capture.get("tone_offset_hz_readback") != result.get("tone_offset_hz_readback")
        or capture.get("tone_offset_hz_readback") != active_tone_hz
        or capture.get("tone_offset_hz_measured") != result.get("tone_offset_hz_measured")
        or not isinstance(pilot, Mapping)
        or pilot.get("confidence") != result.get("pilot_confidence")
        or not isinstance(headroom, Mapping)
        or headroom.get("passed") != result.get("headroom_passed")
        or result.get("headroom_passed") is not True
        or record.get("measurement_quality_passed") != result.get("measurement_quality_passed")
        or record.get("measurement_quality_rejection_reasons")
        != result.get("measurement_quality_rejection_reasons")
        or record.get("rx2_tone_detected") != result.get("rx2_tone_detected")
        or not isinstance(analysis, Mapping)
        or not isinstance(rx2, Mapping)
        or rx2.get("tone_detected") != result.get("rx2_tone_detected")
        or analysis.get("rx2_over_rx1") != result.get("rx2_over_rx1")
        or continuity != continuity_record
        or continuity.get("stream_id") != result.get("stream_id")
        or continuity.get("metadata_abi") != 2
        or not isinstance(safety, Mapping)
        or safety.get("post_condition_exact_serial_mute")
        != result.get("post_condition_exact_serial_mute")
        or safety.get("selector_static_all_off_before") != selector_before
        or safety.get("selector_static_all_off_after") != selector_after
        or safety.get("selector_static_all_off_cleanup") != selector_cleanup
        or not selector_evidence_passed
        or safety.get("fresh_stream_validated") is not True
        or safety.get("automatic_retry_count") != 0
        or result.get("selector_calibration_claim") is not False
        or result.get("causal_attribution_claim") is not False
    ):
        raise LeakageLadderError("completed result condition record is inconsistent")


def _downgrade_and_quarantine_completed_attempt(
    attempt: Mapping[str, Any],
    *,
    result: object,
    capture_root: Path,
    error: BaseException,
) -> None:
    quarantine: dict[str, Any] | None = None
    if isinstance(result, Mapping) and isinstance(result.get("artifact_path"), str):
        try:
            raw_source = Path(str(result["artifact_path"]))
            _assert_path_chain_has_no_symlink(
                raw_source,
                label="completed artifact path",
            )
            exact_root, failed_root = _safe_quarantine_parent(capture_root)
            source = _assert_tree_has_no_symlink(
                raw_source,
                label="completed artifact",
            )
            if source.parent == exact_root:
                destination = failed_root / f"{source.name}.resume-invalid"
                if destination.exists() or destination.is_symlink():
                    raise LeakageLadderError("resume-invalid destination already exists")
                os.replace(source, destination)
                quarantine = _seal_failed_directory(
                    destination,
                    artifact_id=source.name,
                    error=error,
                    context={"invalid_completed_result": _json_safe(result)},
                )
        except (OSError, LeakageLadderError):
            quarantine = None
    if isinstance(attempt, dict):
        attempt["status"] = "failed"
        attempt["outcome"] = "resume_validation_failed"
        attempt["failure_kind"] = "completed_artifact_or_evidence_invalid"
        attempt["quarantine"] = quarantine
        attempt["error"] = _error_document(error)
        attempt["completed_at"] = _now()


def _quarantine_orphaned_current_plan_artifacts(
    capture_root: Path,
    *,
    manifest: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if capture_root.is_symlink():
        raise LeakageLadderError("capture root must not be a symlink")
    if not capture_root.exists():
        return []
    _assert_path_chain_has_no_symlink(capture_root, label="capture root")
    exact_root = capture_root.resolve(strict=True)
    if not exact_root.is_dir():
        raise LeakageLadderError("capture root is not a directory")
    failed_entry = capture_root / ".failed"
    if failed_entry.is_symlink():
        raise LeakageLadderError("orphan quarantine root must not be a symlink")
    partial_root = capture_root / ".partial"
    if partial_root.is_symlink():
        raise LeakageLadderError("partial capture root must not be a symlink")
    referenced = {
        str(Path(str(result["artifact_path"])).resolve())
        for attempt in manifest.get("attempts", [])
        if isinstance(attempt, Mapping)
        and attempt.get("status") == "complete"
        and isinstance((result := attempt.get("result")), Mapping)
        and isinstance(result.get("artifact_path"), str)
    }
    candidates: list[tuple[Path, Path]] = []
    for path in capture_root.iterdir():
        if path.name in {".failed", ".partial"}:
            continue
        if path.is_symlink():
            raise LeakageLadderError(f"orphan candidate must not be a symlink: {path}")
        if path.is_dir():
            candidates.append((path, exact_root))
    if partial_root.is_dir():
        exact_partial = partial_root.resolve(strict=True)
        if exact_partial.parent != exact_root:
            raise LeakageLadderError("partial capture root resolves outside run root")
        for path in partial_root.iterdir():
            if path.is_symlink():
                raise LeakageLadderError(f"partial orphan candidate must not be a symlink: {path}")
            if path.is_dir():
                candidates.append((path, exact_partial))
    quarantines: list[dict[str, Any]] = []
    for candidate, expected_parent in candidates:
        exact_candidate = _assert_tree_has_no_symlink(
            candidate,
            label="orphan candidate",
        )
        if exact_candidate.parent != expected_parent:
            raise LeakageLadderError("orphan candidate resolves outside its run directory")
        if str(exact_candidate) in referenced:
            continue
        record_file = exact_candidate / CONDITION_RECORD_NAME
        record: Mapping[str, Any] | None = None
        if record_file.is_file():
            try:
                record = _read_json(record_file, "orphan leakage condition record")
            except LeakageLadderError:
                record = None
            if record is not None and record.get("immutable_plan") != plan_evidence:
                raise LeakageLadderError(
                    "run-specific capture root contains a different immutable plan"
                )
        error = LeakageLadderError(
            "current-plan artifact has no complete immutable manifest attempt"
        )
        _, failed_root = _safe_quarantine_parent(capture_root)
        destination = failed_root / f"{exact_candidate.name}.orphaned"
        if destination.exists() or destination.is_symlink():
            raise LeakageLadderError("orphan quarantine destination already exists")
        os.replace(exact_candidate, destination)
        _assert_tree_has_no_symlink(destination, label="moved orphan quarantine")
        quarantine = _seal_failed_directory(
            destination,
            artifact_id=exact_candidate.name,
            error=error,
            context={
                "immutable_plan": dict(plan_evidence),
                "orphan_condition_record_present": record is not None,
            },
        )
        quarantines.append(quarantine)
    return quarantines


def _completed_condition_ids(
    manifest: Mapping[str, Any],
    *,
    planned_conditions: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
    serial: str,
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
    downgrade_invalid: bool = True,
) -> set[str]:
    completed: set[str] = set()
    stream_ids: set[int] = set()
    artifact_ids: set[str] = set()
    data_hashes: set[str] = set()
    metadata_hashes: set[str] = set()
    record_hashes: set[str] = set()
    source = contract.get("source")
    fixture = contract.get("fixture_evidence")
    for raw in manifest.get("attempts", []):
        if not isinstance(raw, Mapping):
            raise LeakageLadderError("manifest attempt is malformed")
        condition_id = raw.get("condition_id")
        if not isinstance(condition_id, str) or condition_id not in planned_conditions:
            raise LeakageLadderError("manifest attempt is not bound to an immutable condition")
        if raw.get("status") != "complete":
            raise LeakageLadderError("manifest contains a non-complete or unknown-status attempt")
        condition = planned_conditions[condition_id]
        result = raw.get("result")
        malformed = LeakageLadderError("completed leakage attempt evidence is malformed")
        if (
            condition_id in completed
            or not isinstance(result, Mapping)
            or raw.get("condition") != dict(condition)
            or raw.get("automatic_retry_attempted") is not False
            or raw.get("failure_kind") is not None
            or raw.get("quarantine") is not None
            or raw.get("error") is not None
            or raw.get("post_condition_exact_serial_mute")
            != result.get("post_condition_exact_serial_mute")
            or raw.get("outcome")
            != (
                "measurement_quality_passed"
                if result.get("measurement_quality_passed") is True
                else "measurement_quality_rejected"
            )
            or result.get("condition_id") != condition_id
            or result.get("topology_stage") != contract.get("topology_stage")
            or result.get("tx_hardware_gain_db") != condition.get("tx_hardware_gain_db")
            or result.get("attribution_repeat_index") != condition.get("attribution_repeat_index")
            or result.get("attribution_repeat_count") != condition.get("attribution_repeat_count")
            or result.get("metadata_abi") != 2
            or isinstance(result.get("stream_id"), bool)
            or not isinstance(result.get("stream_id"), int)
            or not isinstance(source, Mapping)
            or result.get("native_libiio_runtime_attestation")
            != source.get("native_libiio_runtime_attestation")
            or result.get("native_libiio_runtime_attestation_sha256")
            != source.get("native_libiio_runtime_attestation_sha256")
            or result.get("fixture_evidence") != fixture
            or result.get("fixture_evidence_sha256") != contract.get("fixture_evidence_sha256")
            or not _mute_passed(
                result.get("post_condition_exact_serial_mute"),
                serial=serial,
                purpose="post_condition",
            )
            or result.get("selector_calibration_claim") is not False
            or result.get("causal_attribution_claim") is not False
        ):
            if downgrade_invalid:
                _downgrade_and_quarantine_completed_attempt(
                    raw,
                    result=result,
                    capture_root=capture_root,
                    error=malformed,
                )
            raise malformed
        try:
            _verify_completed_result_files(
                result,
                condition=condition,
                contract=contract,
                plan_evidence=plan_evidence,
                capture_root=capture_root,
            )
        except BaseException as error:
            if downgrade_invalid:
                _downgrade_and_quarantine_completed_attempt(
                    raw,
                    result=result,
                    capture_root=capture_root,
                    error=error,
                )
            raise
        stream_id = int(result["stream_id"])
        artifact_id = result.get("artifact_id")
        data_sha = result.get("artifact_data_sha256")
        metadata_sha = result.get("artifact_metadata_sha256")
        record_sha = result.get("condition_record_sha256")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or stream_id in stream_ids
            or artifact_id in artifact_ids
            or data_sha in data_hashes
            or metadata_sha in metadata_hashes
            or record_sha in record_hashes
        ):
            reuse = LeakageLadderError(
                "completed conditions reused an artifact, hash, or ABI2 stream identity"
            )
            if downgrade_invalid:
                _downgrade_and_quarantine_completed_attempt(
                    raw,
                    result=result,
                    capture_root=capture_root,
                    error=reuse,
                )
            raise reuse
        stream_ids.add(stream_id)
        artifact_ids.add(artifact_id)
        data_hashes.add(str(data_sha))
        metadata_hashes.add(str(metadata_sha))
        record_hashes.add(str(record_sha))
        completed.add(condition_id)
    return completed


def _execute_stage_under_selector_lock(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_root: Path,
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity_boundary,
    selector_boundary: SelectorBoundary = _live_selector_all_off_boundary,
    runtime_attestation_boundary: RuntimeAttestationBoundary = (_native_libiio_runtime_attestation),
    fixture_evidence_boundary: FixtureEvidenceBoundary = _live_fixture_evidence_boundary,
) -> None:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    conditions = contract["conditions"]
    assert isinstance(conditions, list)
    serial = str(contract["configuration"]["serial"])
    uri = str(contract["configuration"]["uri"])
    stage = str(contract["topology_stage"])
    raw_selector_control = contract.get("selector_control")
    selector_control = raw_selector_control if isinstance(raw_selector_control, Mapping) else None
    condition_count = len(conditions)
    storage = contract.get("storage")
    if (
        not isinstance(storage, Mapping)
        or Path(str(storage.get("run_capture_root", ""))).resolve() != capture_root.resolve()
    ):
        raise LeakageLadderError("execution capture root differs from immutable run storage")
    expected_fixture = contract.get("fixture_evidence")
    if not isinstance(expected_fixture, Mapping):
        raise LeakageLadderError("immutable general-ladder plan lacks fixture-evidence v2")
    if not _confirmation_fixture_binding_passed(confirmation, expected_fixture):
        error = LeakageLadderError(
            "no-movement confirmation is not bound to the exact fixture/setup hashes and IDs"
        )
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error
    source = contract.get("source")
    expected_runtime = (
        source.get("native_libiio_runtime_attestation") if isinstance(source, Mapping) else None
    )
    if not isinstance(expected_runtime, Mapping):
        raise LeakageLadderError("immutable plan lacks native libiio runtime attestation")
    runtime_preflight = _call_runtime_attestation(runtime_attestation_boundary)
    manifest["native_runtime_preflight_attempts"].append(runtime_preflight)
    manifest["native_runtime_preflight"] = runtime_preflight
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not _runtime_attestation_passed(runtime_preflight, expected=expected_runtime):
        error = LeakageLadderError(
            "native libiio runtime differs from the immutable path/version/hash attestation"
        )
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    fixture_preflight = _call_fixture_evidence(
        fixture_evidence_boundary,
        expected_fixture,
    )
    manifest["fixture_evidence_preflight_attempts"].append(fixture_preflight)
    manifest["fixture_evidence_preflight"] = fixture_preflight
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not _fixture_evidence_passed(fixture_preflight, expected=expected_fixture):
        error = LeakageLadderError(
            "fixture manifest, per-run setup attestation, or evidence differs from the plan"
        )
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    _validate_manifest_selector_history(
        manifest,
        selector_control=selector_control,
    )

    if stage in SELECTOR_CONNECTED_STAGES:
        if selector_control is None:
            raise LeakageLadderError("selector-connected stage lacks selector control")
        initial_selector = _call_selector(
            selector_boundary,
            selector_control,
            "initial_state_before_command",
        )
        manifest["selector_initial_state_attempts"].append(initial_selector)
        manifest["selector_initial_state"] = initial_selector
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not _selector_passed(
            initial_selector,
            selector_control=selector_control,
            purpose="initial_state_before_command",
        ):
            error = LeakageLadderError(
                "selector was not already static ALL_OFF before the first command"
            )
            manifest["status"] = "failed"
            manifest["error"] = _error_document(error)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            raise error
    elif selector_control is not None:
        raise LeakageLadderError("selector-disconnected stage includes selector control")

    identity = _call_identity(identity_boundary, serial, uri)
    manifest["identity_preflight_attempts"].append(identity)
    manifest["identity_preflight"] = identity
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not _identity_passed(identity, serial=serial, requested_uri=uri):
        error = LeakageLadderError(
            "read-only USB identity scan did not resolve the exact requested current URI"
        )
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    if manifest.get("status") == "failed" or any(
        isinstance(item, Mapping) and item.get("status") == "failed"
        for item in manifest["attempts"]
    ):
        recovery = _call_mute(mute_boundary, serial, "resume_recovery")
        manifest["recovery_mute_attempts"].append(recovery)
        if selector_control is not None:
            recovery_selector = _call_selector(
                selector_boundary,
                selector_control,
                "resume_cleanup_all_off",
            )
            manifest["recovery_selector_cleanup_attempts"].append(recovery_selector)
        error = LeakageLadderError("failed ladder runs cannot retry; prepare a new run ID")
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    stale = [
        item
        for item in manifest["attempts"]
        if isinstance(item, dict) and item.get("status") == "running"
    ]
    pending_error: BaseException | None = None
    manifest["confirmations"].append(dict(confirmation))
    manifest["status"] = "running"
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    try:
        if stale:
            recovery = _call_mute(mute_boundary, serial, "resume_recovery")
            manifest["recovery_mute_attempts"].append(recovery)
            stale_recovery_selector = (
                _call_selector(
                    selector_boundary,
                    selector_control,
                    "resume_cleanup_all_off",
                )
                if selector_control is not None
                else None
            )
            if stale_recovery_selector is not None:
                manifest["recovery_selector_cleanup_attempts"].append(stale_recovery_selector)
            for stale_attempt in stale:
                stale_attempt["status"] = "failed"
                stale_attempt["outcome"] = "stale_process_failed"
                stale_attempt["failure_kind"] = "stale_process"
                stale_attempt["recovery_mute"] = recovery
                stale_attempt["recovery_selector_cleanup"] = stale_recovery_selector
                stale_attempt["completed_at"] = _now()
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            raise LeakageLadderError("stale live attempt recovered; use a new run ID")

        preflight = _call_mute(mute_boundary, serial, "preflight")
        manifest["preflight_mute_attempts"].append(preflight)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not _mute_passed(preflight, serial=serial, purpose="preflight"):
            raise LeakageLadderError("exact preflight mute attestation failed")

        planned_conditions = {
            str(condition["condition_id"]): condition
            for condition in conditions
            if isinstance(condition, Mapping)
        }
        if len(planned_conditions) != len(conditions):
            raise LeakageLadderError("immutable plan condition IDs are malformed or duplicated")
        plan_evidence = _plan_file_evidence(plan_path, envelope)
        orphan_quarantines = _quarantine_orphaned_current_plan_artifacts(
            capture_root,
            manifest=manifest,
            plan_evidence=plan_evidence,
        )
        manifest["orphan_quarantine_attempts"].extend(orphan_quarantines)
        if orphan_quarantines:
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        completed = _completed_condition_ids(
            manifest,
            planned_conditions=planned_conditions,
            contract=contract,
            serial=serial,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
        )
        forbidden_stream_ids = {
            int(item["result"]["stream_id"])
            for item in manifest["attempts"]
            if isinstance(item, Mapping)
            and item.get("status") == "complete"
            and isinstance(item.get("result"), Mapping)
        }
        for raw_condition in conditions:
            if not isinstance(raw_condition, Mapping):
                raise LeakageLadderError("immutable plan condition is malformed")
            condition = dict(raw_condition)
            condition_id = str(condition["condition_id"])
            if condition_id in completed:
                continue
            attempt: dict[str, Any] = {
                "attempt_id": len(manifest["attempts"]) + 1,
                "condition_id": condition_id,
                "condition": condition,
                "started_at": _now(),
                "completed_at": None,
                "status": "running",
                "outcome": None,
                "failure_kind": None,
                "result": None,
                "quarantine": None,
                "post_condition_exact_serial_mute": None,
                "error": None,
                "automatic_retry_attempted": False,
            }
            manifest["attempts"].append(attempt)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            try:
                result = _capture_condition(
                    condition,
                    contract=contract,
                    plan_evidence=_plan_file_evidence(plan_path, envelope),
                    capture_root=capture_root,
                    forbidden_stream_ids=forbidden_stream_ids,
                    capture_boundary=capture_boundary,
                    mute_boundary=mute_boundary,
                    selector_boundary=selector_boundary,
                )
            except ConditionCaptureFailure as error:
                attempt["status"] = "failed"
                attempt["outcome"] = "condition_failed"
                attempt["failure_kind"] = "capture_or_validation"
                attempt["quarantine"] = error.quarantine
                attempt["post_condition_exact_serial_mute"] = error.post_mute
                attempt["error"] = _error_document(error)
                attempt["completed_at"] = _now()
                _persist_manifest(manifest_path, manifest, condition_count=condition_count)
                raise
            attempt["result"] = result
            attempt["post_condition_exact_serial_mute"] = result["post_condition_exact_serial_mute"]
            attempt["outcome"] = (
                "measurement_quality_passed"
                if result["measurement_quality_passed"]
                else "measurement_quality_rejected"
            )
            attempt["status"] = "complete"
            attempt["completed_at"] = _now()
            forbidden_stream_ids.add(int(result["stream_id"]))
            completed.add(condition_id)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        completed = _completed_condition_ids(
            manifest,
            planned_conditions=planned_conditions,
            contract=contract,
            serial=serial,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
        )
        if completed != set(planned_conditions):
            raise LeakageLadderError(
                "completed manifest does not contain every immutable planned condition"
            )
        rejected = [
            item
            for item in manifest["attempts"]
            if isinstance(item, Mapping) and item.get("outcome") == "measurement_quality_rejected"
        ]
        if rejected:
            raise LeakageLadderError(
                f"{len(rejected)} condition(s) failed measurement quality; stage is not successful"
            )
        manifest["status"] = "conditions_complete"
    except BaseException as error:
        pending_error = error
        manifest["error"] = _error_document(error)
        manifest["status"] = "failed"
    finally:
        final_mute = _call_mute(mute_boundary, serial, "final")
        final_selector = (
            _call_selector(
                selector_boundary,
                selector_control,
                "final_cleanup_all_off",
            )
            if selector_control is not None
            else None
        )
        manifest["final_mute_attempts"].append(final_mute)
        manifest["final_mute"] = final_mute
        if final_selector is not None:
            manifest["final_selector_cleanup_attempts"].append(final_selector)
        manifest["final_selector_cleanup"] = final_selector
        final_selector_passed = selector_control is None or _selector_passed(
            final_selector,
            selector_control=selector_control,
            purpose="final_cleanup_all_off",
        )
        if (
            not _mute_passed(final_mute, serial=serial, purpose="final")
            or not final_selector_passed
        ):
            pending_error = LeakageLadderError(
                "exact final mute or selector ALL_OFF cleanup attestation failed"
            )
            manifest["error"] = _error_document(pending_error)
            manifest["status"] = "failed"
        elif pending_error is None:
            manifest["status"] = "complete"
            manifest["completed_at"] = _now()
            manifest["error"] = None
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if pending_error is not None:
        raise pending_error


def _execute_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_root: Path,
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity_boundary,
    selector_boundary: SelectorBoundary = _live_selector_all_off_boundary,
    runtime_attestation_boundary: RuntimeAttestationBoundary = (_native_libiio_runtime_attestation),
    fixture_evidence_boundary: FixtureEvidenceBoundary = _live_fixture_evidence_boundary,
    selector_lock_root: Path | None = None,
) -> None:
    contract = envelope["plan_contract"]
    if not isinstance(contract, Mapping):
        raise LeakageLadderError("immutable plan contract is malformed")
    stage = str(contract.get("topology_stage", ""))
    selector_control = contract.get("selector_control")
    serial = str(contract.get("configuration", {}).get("serial", ""))
    condition_count = len(contract.get("conditions", []))
    lock = (
        _board_lock(selector_lock_root or _selector_lock_root())
        if stage in SELECTOR_CONNECTED_STAGES
        else nullcontext()
    )
    with lock:
        final_attempt_count = len(manifest.get("final_mute_attempts", []))
        try:
            _execute_stage_under_selector_lock(
                manifest,
                manifest_path,
                envelope=envelope,
                plan_path=plan_path,
                confirmation=confirmation,
                capture_root=capture_root,
                capture_boundary=capture_boundary,
                mute_boundary=mute_boundary,
                identity_boundary=identity_boundary,
                selector_boundary=selector_boundary,
                runtime_attestation_boundary=runtime_attestation_boundary,
                fixture_evidence_boundary=fixture_evidence_boundary,
            )
        except BaseException as error:
            if len(manifest.get("final_mute_attempts", [])) == final_attempt_count:
                cleanup_error: BaseException = error
                final_mute = _call_mute(mute_boundary, serial, "final")
                final_selector = (
                    _call_selector(
                        selector_boundary,
                        selector_control,
                        "exception_cleanup_all_off",
                    )
                    if isinstance(selector_control, Mapping)
                    else None
                )
                manifest["final_mute_attempts"].append(final_mute)
                manifest["final_mute"] = final_mute
                if final_selector is not None:
                    manifest["final_selector_cleanup_attempts"].append(final_selector)
                manifest["final_selector_cleanup"] = final_selector
                selector_cleanup_passed = not isinstance(
                    selector_control, Mapping
                ) or _selector_passed(
                    final_selector,
                    selector_control=selector_control,
                    purpose="exception_cleanup_all_off",
                )
                if (
                    not _mute_passed(final_mute, serial=serial, purpose="final")
                    or not selector_cleanup_passed
                ):
                    cleanup_error = LeakageLadderError(
                        "exception-path exact mute or selector ALL_OFF cleanup failed"
                    )
                manifest["status"] = "failed"
                manifest["error"] = _error_document(cleanup_error)
                _persist_manifest(
                    manifest_path,
                    manifest,
                    condition_count=condition_count,
                )
                if cleanup_error is not error:
                    raise cleanup_error from error
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="current exact usb: IIO URI")
    parser.add_argument("--stage", choices=STAGES, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-only", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-tx1-matched-conducted", action="store_true")
    parser.add_argument("--confirm-tx2-terminated-muted", action="store_true")
    parser.add_argument("--confirm-rx1-conducted-reference", action="store_true")
    parser.add_argument(
        "--confirm-no-movement",
        action="store_true",
        help="confirm no component or connection moved since this run's setup attestation",
    )
    parser.add_argument("--confirm-selector-static-all-off", action="store_true")
    parser.add_argument("--confirm-stage", choices=STAGES)
    parser.add_argument("--confirm-topology-token")
    parser.add_argument("--bench-manifest", type=Path)
    parser.add_argument("--openocd-config", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        help=("required for every stage: campaign fixture-v2 component/port/connection graph"),
    )
    parser.add_argument(
        "--setup-attestation",
        type=Path,
        help=(
            "required unique run-bound setup attestation and setup-evidence hash; timestamp is "
            "descriptive and has no wall-clock freshness guarantee"
        ),
    )
    return parser


def _signal_handler(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received {signal.Signals(signum).name}; entering fail-muted cleanup")


def _install_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    repository = Path(__file__).resolve().parents[1]
    try:
        source_commit = _repository_commit_and_require_clean(repository, "smateway")
        dependency_attestation = attest_pluto_plus_utils_source()
        native_runtime_attestation = _native_libiio_runtime_attestation()
        if args.fixture_manifest is None or args.setup_attestation is None:
            raise LeakageLadderError(
                "every general-ladder stage requires --fixture-manifest and --setup-attestation"
            )
        fixture_evidence = _fixture_evidence_from_manifests(
            args.fixture_manifest,
            args.setup_attestation,
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            stage=args.stage,
        )
        selector_control = None
        if args.stage in SELECTOR_CONNECTED_STAGES:
            if not all((args.bench_manifest, args.openocd_config, args.profile)):
                raise LeakageLadderError(
                    "selector-connected stage requires --bench-manifest, --openocd-config, "
                    "and --profile"
                )
            selector_control = _selector_control_contract(
                bench_manifest_path=args.bench_manifest,
                openocd_config_path=args.openocd_config,
                profile_path=args.profile,
            )
        elif any((args.bench_manifest, args.openocd_config, args.profile)):
            raise LeakageLadderError(
                "selector-disconnected stage must not include selector-control files"
            )
        contract = _build_plan_contract(
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            stage=args.stage,
            source_commit=source_commit,
            pluto_plus_utils_source_attestation=dependency_attestation,
            selector_control=selector_control,
            native_libiio_runtime_attestation=native_runtime_attestation,
            fixture_evidence=fixture_evidence,
        )
    except (LeakageLadderError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    board_root = _board_root(str(contract["board_id"]))
    run_root = board_root / "5g8-leakage-ladder" / str(contract["run_id"])
    plan_path = run_root / PLAN_FILENAME
    manifest_path = run_root / MANIFEST_FILENAME
    with _board_lock(board_root):
        if args.plan_only:
            try:
                envelope, manifest = _prepare_plan_only_run(
                    plan_path=plan_path,
                    manifest_path=manifest_path,
                    contract=contract,
                )
            except LeakageLadderError as error:
                raise SystemExit(str(error)) from error
            _persist_manifest(
                manifest_path,
                manifest,
                condition_count=len(contract["conditions"]),
            )
            print(
                json.dumps(
                    {
                        "run_id": contract["run_id"],
                        "stage": contract["topology_stage"],
                        "status": manifest["status"],
                        "immutable_plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "plan_file_sha256": sha256_path(plan_path),
                        "manifest": str(manifest_path),
                        "condition_count": len(contract["conditions"]),
                        "selector_calibration_claim": False,
                    },
                    sort_keys=True,
                )
            )
            return 0

        tombstone_path = _failure_tombstone_path(manifest_path)
        if tombstone_path.exists() or tombstone_path.is_symlink():
            raise SystemExit("failed-run tombstone forbids execution or retry")
        if (
            plan_path.is_symlink()
            or manifest_path.is_symlink()
            or not plan_path.is_file()
            or not manifest_path.is_file()
        ):
            raise SystemExit("execute requires a prior successful --plan-only invocation")
        try:
            envelope = _validate_plan_envelope(
                _read_json(plan_path, "immutable plan"),
                expected_contract=contract,
            )
            manifest = _load_manifest(
                manifest_path,
                plan_path=plan_path,
                envelope=envelope,
            )
            confirmation = _validate_confirmations(
                stage=args.stage,
                confirm_stage=args.confirm_stage,
                topology_token=args.confirm_topology_token,
                no_antennas=args.confirm_no_antennas,
                tx1_matched=args.confirm_tx1_matched_conducted,
                tx2_terminated_muted=args.confirm_tx2_terminated_muted,
                rx1_conducted_reference=args.confirm_rx1_conducted_reference,
                no_movement=args.confirm_no_movement,
                fixture_evidence=fixture_evidence,
                selector_static_all_off=args.confirm_selector_static_all_off,
            )
            _execute_stage(
                manifest,
                manifest_path,
                envelope=envelope,
                plan_path=plan_path,
                confirmation=confirmation,
                capture_root=Path(str(contract["storage"]["run_capture_root"])),
            )
        except (LeakageLadderError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "run_id": contract["run_id"],
                    "stage": contract["topology_stage"],
                    "status": manifest["status"],
                    "manifest": str(manifest_path),
                    "summary": manifest["summary"],
                    "selector_calibration_claim": False,
                },
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
