"""Microsecond-native capture evidence and coherent analysis for ``hexcal-v1``.

The module deliberately does not talk to hardware.  It loads the generated
firmware contract, validates immutable SigMF evidence, decodes the RF-visible
null marker/guards, and estimates the six end-to-end complex array paths.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.capture_continuity import validate_sigmf_continuity

REQUIRED_METADATA_FLAGS = (1 << 4) | (1 << 21)
FAILURE_METADATA_FLAGS = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 17) | (1 << 20)
EXPECTED_PROFILE_ID = "hexcal-v1"
EXPECTED_STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 7))
EXPECTED_GPIO_CODES = ("0000", "0100", "0010", "0110", "0001", "0101")
EXPECTED_ALL_OFF_CODE = "1000"
EXPECTED_CONTRACT_SHA256 = "06324954fb43710af89f6f9e439a7d4bf652c18de714e56645d71f8099689726"
EXPECTED_HEXCAL_ELF_SHA256 = "8e0cc535f98d30be02f7b9662938516d3d5d2a8bbc5d72440e1494617c7dc9c9"
EXPECTED_HEXCAL_BIN_SHA256 = "6d0a06f9160d91e6c04f9ba29e8d90c3aaf65e1386a6d7311fbd6689a103e6b3"
EXPECTED_HEXCAL_BIN_SIZE_BYTES = 1152
EXPECTED_HEXCAL_FULL_FLASH_SHA256 = (
    "1ac75057a6dbb3235b6dfb07899a2ae5ef025d9b1d5c0dee37df4cdc72b2453e"
)
ANALYZER_VERSION = 2
EDGE_EXCLUSION_US = 5.0
DETECTION_BIN_US = 5.0
MINIMUM_PHASE_GAUGE_RESULTANT = 0.25
STM32C011_FLASH_SIZE_BYTES = 16 * 1024
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
HEXCAL_ANALYSIS_SOURCE_FILES = (
    "profiles/hexcal-v1/control_profile.json",
    "scripts/reanalyze_hexcal_artifact.py",
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/hexcal.py",
    "pyproject.toml",
    "uv.lock",
)
HEXCAL_AGGREGATION_SOURCE_FILES = (
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.1-2g4-stimulus.json",
    "scripts/aggregate_hexcal_calibration.py",
    "src/smateway/hexcal.py",
    "src/smateway/hexcal_gain.py",
    "pyproject.toml",
    "uv.lock",
)
PLUTO_PLUS_UTILS_REPOSITORY = Path("/home/pi/pluto-plus-utils")
PLUTO_PLUS_UTILS_PIN = "5551d29bc6c326f26285670efd20fc149caef474"
PLUTO_PLUS_UTILS_PYTHON = PLUTO_PLUS_UTILS_REPOSITORY / ".venv/bin/python"
PLUTO_PLUS_UTILS_PYTHON_PREFIX = PLUTO_PLUS_UTILS_REPOSITORY / ".venv"
PLUTO_PLUS_UTILS_LOCK_FILES = ("pyproject.toml", "uv.lock")
PLUTO_PLUS_UTILS_IMPORTED_MODULES = (
    ("pluto_plus", "src/pluto_plus/__init__.py"),
    ("pluto_plus.artifacts", "src/pluto_plus/artifacts.py"),
    ("pluto_plus.bootstrap_firmware", "src/pluto_plus/bootstrap_firmware.py"),
    ("pluto_plus.hardware", "src/pluto_plus/hardware/__init__.py"),
    ("pluto_plus.hardware.iio", "src/pluto_plus/hardware/iio.py"),
    ("pluto_plus.hardware.iio_metadata", "src/pluto_plus/hardware/iio_metadata.py"),
    ("pluto_plus.hardware.preflight", "src/pluto_plus/hardware/preflight.py"),
    ("pluto_plus.hardware.stimulus", "src/pluto_plus/hardware/stimulus.py"),
    ("pluto_plus.models", "src/pluto_plus/models.py"),
)
TX1_ACTIVE_DDS_INDICES = (0, 2)
TX1_INACTIVE_DDS_INDICES = (1, 3, 4, 5, 6, 7)


@dataclass(frozen=True, slots=True)
class HexcalState:
    """One generated selector state in the exact firmware order."""

    name: str
    gpio_code_pa3_pa0: str
    dwell_us: int
    window_us: tuple[float, float]


@dataclass(frozen=True, slots=True)
class HexcalProfile:
    """Validated, integer-microsecond host view of ``hexcal-v1``."""

    path: Path
    file_sha256: str
    contract_sha256: str
    revision: int
    timer_nominal_hz: int
    timer_resolution_us: int
    marker_body_us: int
    marker_observable_us: int
    marker_window_us: tuple[float, float]
    guard_us: int
    guard_window_us: tuple[float, float]
    cycle_us: int
    order_direction: str
    forward_reference: str
    states: tuple[HexcalState, ...]

    @property
    def state_names(self) -> tuple[str, ...]:
        return tuple(state.name for state in self.states)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": EXPECTED_PROFILE_ID,
            "revision": self.revision,
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "contract_sha256": self.contract_sha256,
            "timer_nominal_hz": self.timer_nominal_hz,
            "timer_resolution_us": self.timer_resolution_us,
            "marker_body_us": self.marker_body_us,
            "marker_observable_us": self.marker_observable_us,
            "marker_window_us": list(self.marker_window_us),
            "guard_us": self.guard_us,
            "guard_window_us": list(self.guard_window_us),
            "cycle_us": self.cycle_us,
            "order_direction": self.order_direction,
            "forward_reference": self.forward_reference,
            "states": [
                {
                    "name": state.name,
                    "gpio_code_pa3_pa0": state.gpio_code_pa3_pa0,
                    "dwell_us": state.dwell_us,
                    "window_us": list(state.window_us),
                }
                for state in self.states
            ],
        }


@dataclass(frozen=True, slots=True)
class HexcalFirmwareEvidence:
    """Validated full-flash readback evidence for the exact hexcal image."""

    path: Path
    file_sha256: str
    board_id: str
    target_uid: str
    target_uid_readback_path: Path
    target_uid_readback_sha256: str
    target_uid_readback_size_bytes: int
    source_commit: str
    profile_file_sha256: str
    profile_contract_sha256: str
    firmware_elf_path: Path
    firmware_elf_sha256: str
    firmware_elf_size_bytes: int
    firmware_bin_path: Path
    firmware_bin_sha256: str
    firmware_bin_size_bytes: int
    full_flash_readback_path: Path
    full_flash_readback_sha256: str
    full_flash_readback_size_bytes: int
    verified_at: str
    verification_method: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "board_id": self.board_id,
            "target_uid": self.target_uid,
            "target_uid_readback_path": str(self.target_uid_readback_path),
            "target_uid_readback_sha256": self.target_uid_readback_sha256,
            "target_uid_readback_size_bytes": self.target_uid_readback_size_bytes,
            "source_commit": self.source_commit,
            "profile_file_sha256": self.profile_file_sha256,
            "profile_contract_sha256": self.profile_contract_sha256,
            "firmware_elf_path": str(self.firmware_elf_path),
            "firmware_elf_sha256": self.firmware_elf_sha256,
            "firmware_elf_size_bytes": self.firmware_elf_size_bytes,
            "firmware_bin_path": str(self.firmware_bin_path),
            "firmware_bin_sha256": self.firmware_bin_sha256,
            "firmware_bin_size_bytes": self.firmware_bin_size_bytes,
            "full_flash_readback_path": str(self.full_flash_readback_path),
            "full_flash_readback_sha256": self.full_flash_readback_sha256,
            "full_flash_readback_size_bytes": self.full_flash_readback_size_bytes,
            "verified_at": self.verified_at,
            "verification_method": self.verification_method,
            "image_prefix_matches": True,
            "erased_tail_verified": True,
            "target_identity_verified": True,
        }


class HexcalAnalysisError(ValueError):
    """The persisted samples cannot be decoded without guessing."""


def sha256_path(path: Path) -> str:
    """Hash one file without relying on artifact-library summary fields."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def attest_source_files_at_commit(
    repository: Path,
    *,
    expected_commit: str,
    relative_paths: Sequence[str],
) -> dict[str, Any]:
    """Bind current analyzer bytes to exact files stored in one Git commit.

    A later documentation-only commit is permitted, but every listed scientific
    implementation file must remain byte-for-byte equal to ``expected_commit``
    and clean in the worktree/index.
    """

    root = repository.expanduser().resolve(strict=True)
    if GIT_COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("source commit must be a full lowercase Git object ID")
    if not relative_paths:
        raise ValueError("source attestation requires at least one file")
    normalized: list[str] = []
    for raw_path in relative_paths:
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts or raw_path in {"", "."}:
            raise ValueError(f"source attestation path is unsafe: {raw_path!r}")
        normalized.append(candidate.as_posix())
    if len(set(normalized)) != len(normalized):
        raise ValueError("source attestation paths must be unique")

    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all", "--", *normalized),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ValueError("scientific implementation files are dirty")

    files: list[dict[str, Any]] = []
    for relative_path in normalized:
        current = root / relative_path
        if not current.is_file():
            raise ValueError(f"source attestation file is missing: {relative_path}")
        committed = subprocess.run(
            ("git", "show", f"{expected_commit}:{relative_path}"),
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        current_bytes = current.read_bytes()
        if current_bytes != committed:
            raise ValueError(
                f"scientific implementation differs from {expected_commit}: {relative_path}"
            )
        files.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(current_bytes).hexdigest(),
                "size_bytes": len(current_bytes),
            }
        )
    return {"commit": expected_commit, "files": files}


def canonical_json_sha256(document: object) -> str:
    """Hash one JSON-compatible evidence object using deterministic bytes."""

    wire = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(wire).hexdigest()


def attest_pluto_plus_utils_source(
    *,
    repository: Path = PLUTO_PLUS_UTILS_REPOSITORY,
    expected_commit: str = PLUTO_PLUS_UTILS_PIN,
    imported_modules: Sequence[tuple[str, str]] = PLUTO_PLUS_UTILS_IMPORTED_MODULES,
    require_repository_runtime: bool = True,
) -> dict[str, Any]:
    """Attest the exact clean local dependency source used by this process.

    Every Python source file in the pinned package plus its project and lock
    metadata is bound to the Git object.  Critical imports must resolve to the
    corresponding files below the same checkout, preventing an ambient wheel or
    another editable checkout from silently supplying the RF control code.
    """

    root = repository.expanduser().resolve(strict=True)
    if GIT_COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("pluto-plus-utils pin must be a full lowercase Git object ID")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != expected_commit:
        raise ValueError("pluto-plus-utils HEAD differs from the pinned commit")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ValueError("pluto-plus-utils source tree is dirty")
    invoked_python = Path(os.path.abspath(sys.executable))
    runtime_prefix = Path(sys.prefix).resolve(strict=True)
    expected_python = root / ".venv/bin/python"
    expected_prefix = (root / ".venv").resolve()
    if require_repository_runtime and (
        invoked_python != expected_python or runtime_prefix != expected_prefix
    ):
        raise ValueError("hexcal must run under the pinned pluto-plus-utils repository Python")
    tracked = subprocess.run(
        ("git", "ls-tree", "-r", "--name-only", expected_commit),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    source_files = sorted(
        path for path in tracked if path.startswith("src/pluto_plus/") and path.endswith(".py")
    )
    if not source_files or any(path not in tracked for path in PLUTO_PLUS_UTILS_LOCK_FILES):
        raise ValueError("pluto-plus-utils package source or lock metadata is incomplete")
    relative_paths = (*source_files, *PLUTO_PLUS_UTILS_LOCK_FILES)
    attestation = attest_source_files_at_commit(
        root,
        expected_commit=expected_commit,
        relative_paths=relative_paths,
    )

    resolved_modules: list[dict[str, Any]] = []
    for module_name, expected_relative_path in imported_modules:
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise ValueError(f"cannot resolve imported dependency module: {module_name}")
        try:
            origin = Path(spec.origin).resolve(strict=True)
        except OSError as error:
            raise ValueError(
                f"cannot resolve imported dependency source: {module_name}: {error}"
            ) from error
        expected_origin = (root / expected_relative_path).resolve(strict=True)
        if origin != expected_origin or not origin.is_relative_to(root):
            raise ValueError(
                f"imported dependency module escaped the pinned checkout: {module_name}"
            )
        resolved_modules.append(
            {
                "module": module_name,
                "path": str(origin),
                "relative_path": expected_relative_path,
                "sha256": sha256_path(origin),
                "size_bytes": origin.stat().st_size,
            }
        )
    return {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "repository_path": str(root),
        "commit": expected_commit,
        "head": head,
        "python_executable": str(invoked_python),
        "python_prefix": str(runtime_prefix),
        "clean_worktree_verified": True,
        "lock_metadata_files": list(PLUTO_PLUS_UTILS_LOCK_FILES),
        "files": attestation["files"],
        "imported_modules": resolved_modules,
    }


def validate_tx1_rf_readback_evidence(
    evidence: Mapping[str, Any],
    *,
    planned_kernel_buffers: int,
    planned_tx_gain_db: float,
    planned_dds_scale: float,
    planned_tone_hz: float,
    sample_rate_hz: float,
) -> dict[str, Any]:
    """Validate all live TX1-only and kernel-buffer readbacks without inference."""

    if evidence.get("schema") != 1 or evidence.get("evidence_kind") != (
        "pluto_tx1_dds_live_readback"
    ):
        raise ValueError("RF readback evidence schema or kind is unsupported")
    if evidence.get("tx_channel") != 0 or evidence.get("tx_port") != "TX1":
        raise ValueError("RF readback evidence is not TX1-only")
    if evidence.get("kernel_buffers") != planned_kernel_buffers:
        raise ValueError("kernel-buffer readback differs from the exact plan")
    numeric_plan = (
        planned_tx_gain_db,
        planned_dds_scale,
        planned_tone_hz,
        sample_rate_hz,
    )
    if not all(math.isfinite(float(value)) for value in numeric_plan):
        raise ValueError("RF readback plan contains a non-finite value")
    if evidence.get("tx_hardware_gain_db_requested") != planned_tx_gain_db:
        raise ValueError("RF readback requested TX gain differs from the plan")
    if evidence.get("dds_scale_requested") != planned_dds_scale:
        raise ValueError("RF readback requested DDS scale differs from the plan")
    if evidence.get("tone_frequency_hz_requested") != planned_tone_hz:
        raise ValueError("RF readback requested DDS frequency differs from the plan")

    raw_gains = evidence.get("tx_hardware_gain_readback_db_by_channel")
    if not isinstance(raw_gains, list) or len(raw_gains) != 2:
        raise ValueError("TX hardware-gain readback must contain TX1 and TX2")
    try:
        gains = tuple(float(value) for value in raw_gains)
    except (TypeError, ValueError) as error:
        raise ValueError("TX hardware-gain readback is non-numeric") from error
    if not all(math.isfinite(value) for value in gains):
        raise ValueError("TX hardware-gain readback is non-finite")
    if gains[0] > planned_tx_gain_db + 0.25:
        raise ValueError("selected TX1 gain readback is above the planned gain")
    if abs(gains[1] - -80.0) > 0.25:
        raise ValueError("inactive TX2 gain readback is not the -80 dB mute limit")
    if evidence.get("tx2_gain_readback_provenance") != (
        "pluto_plus_utils_capture_helper_internal_exact_readback"
    ):
        raise ValueError("TX2 gain readback provenance is not the pinned helper")

    raw_scales = evidence.get("dds_scale_readback")
    raw_enabled = evidence.get("dds_enabled_readback")
    raw_frequencies = evidence.get("dds_frequency_readback_hz")
    if (
        not isinstance(raw_scales, list)
        or not isinstance(raw_enabled, list)
        or not isinstance(raw_frequencies, list)
        or len(raw_scales) != 8
        or len(raw_enabled) != 8
        or len(raw_frequencies) != 8
    ):
        raise ValueError("DDS readbacks must use the canonical eight-source layout")
    try:
        scales = tuple(float(value) for value in raw_scales)
    except (TypeError, ValueError) as error:
        raise ValueError("DDS scale readback is non-numeric") from error
    if not all(math.isfinite(value) for value in scales):
        raise ValueError("DDS scale readback is non-finite")
    if any(not isinstance(value, bool) for value in raw_enabled):
        raise ValueError("DDS enable readback must contain exact booleans")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_frequencies):
        raise ValueError("DDS frequency readback must contain exact integers")
    for index in TX1_ACTIVE_DDS_INDICES:
        if abs(abs(scales[index]) - planned_dds_scale) > 1e-6:
            raise ValueError("TX1 I/Q DDS scale readback differs from the plan")
        if raw_enabled[index] is not True:
            raise ValueError("TX1 I/Q DDS enable readback is not active")
    # pyadi exposes global enable state and may retain inactive frequency
    # registers.  Exact zero scale is the pinned helper's RF-inactivity
    # contract; every raw enable/frequency value remains hash-bound evidence.
    for index in TX1_INACTIVE_DDS_INDICES:
        if scales[index] != 0.0:
            raise ValueError("inactive DDS scale readback is nonzero")
    frequency_tolerance_hz = math.ceil(sample_rate_hz / (1 << 16))
    active_frequencies = tuple(
        abs(float(raw_frequencies[index])) for index in TX1_ACTIVE_DDS_INDICES
    )
    if any(
        abs(frequency - abs(planned_tone_hz)) > frequency_tolerance_hz
        for frequency in active_frequencies
    ):
        raise ValueError("TX1 I/Q DDS frequency readback differs from the plan")
    if abs(active_frequencies[0] - active_frequencies[1]) > frequency_tolerance_hz:
        raise ValueError("TX1 I/Q DDS frequency readback differs from the plan")
    expected_indices = list(TX1_ACTIVE_DDS_INDICES)
    if evidence.get("active_dds_indices") != expected_indices:
        raise ValueError("RF readback active DDS index declaration differs")
    if evidence.get("inactive_dds_indices") != list(TX1_INACTIVE_DDS_INDICES):
        raise ValueError("RF readback inactive DDS index declaration differs")
    if evidence.get("inactive_dds_rf_activity_contract") != (
        "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
    ):
        raise ValueError("inactive DDS RF-activity semantics are not explicit")
    return {
        "kernel_buffers": planned_kernel_buffers,
        "tx1_gain_readback_db": gains[0],
        "tx2_gain_readback_db": gains[1],
        "dds_scale_readback": list(scales),
        "dds_enabled_readback": list(raw_enabled),
        "dds_frequency_readback_hz": list(raw_frequencies),
    }


def write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Durably replace one JSON document and fsync its containing directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _window(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must contain exactly two numbers")
    low, high = value
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, (int, float))
        or not isinstance(high, (int, float))
        or not math.isfinite(float(low))
        or not math.isfinite(float(high))
        or float(low) <= 0.0
        or float(high) < float(low)
    ):
        raise ValueError(f"{label} is malformed")
    return float(low), float(high)


def load_hexcal_profile(path: Path) -> HexcalProfile:
    """Load and narrowly validate the generated six-state microsecond profile."""

    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load hexcal profile: {error}") from error
    root = _mapping(document, "profile root")
    identity = _mapping(root.get("profile"), "profile")
    if root.get("schema") != 1 or root.get("time_unit") != "microseconds":
        raise ValueError("hexcal profile must use schema 1 integer microseconds")
    if identity.get("id") != EXPECTED_PROFILE_ID or identity.get("revision") != 1:
        raise ValueError("host analysis requires the exact hexcal-v1 revision 1 profile")
    protocol = root.get("protocol")
    if protocol != "framed_guarded_equal_dwell_hexcal_v1":
        raise ValueError("hexcal profile protocol differs from the reviewed contract")

    clock = _mapping(root.get("clock"), "clock")
    frame = _mapping(root.get("frame"), "frame")
    marker = _mapping(frame.get("marker"), "frame.marker")
    safety = _mapping(root.get("safety"), "safety")
    array_order_raw = root.get("array_order", {})
    array_order = _mapping(array_order_raw, "array_order")
    states_raw = root.get("states")
    if not isinstance(states_raw, list) or len(states_raw) != 6:
        raise ValueError("hexcal profile must contain exactly six states")
    states: list[HexcalState] = []
    for index, item in enumerate(states_raw):
        state = _mapping(item, f"states[{index}]")
        states.append(
            HexcalState(
                name=str(state.get("name")),
                gpio_code_pa3_pa0=str(state.get("gpio_code_pa3_pa0")),
                dwell_us=_integer(state.get("dwell_us"), f"states[{index}].dwell_us", minimum=1),
                window_us=_window(state.get("window_us"), f"states[{index}].window_us"),
            )
        )
    result = HexcalProfile(
        path=resolved,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        contract_sha256=str(root.get("contract_sha256")),
        revision=1,
        timer_nominal_hz=_integer(clock.get("timer_nominal_hz"), "timer_nominal_hz", minimum=1),
        timer_resolution_us=_integer(
            clock.get("timer_resolution_us"), "timer_resolution_us", minimum=1
        ),
        marker_body_us=_integer(marker.get("body_nominal_us"), "marker.body_nominal_us", minimum=1),
        marker_observable_us=_integer(
            marker.get("observable_nominal_us"), "marker.observable_nominal_us", minimum=1
        ),
        marker_window_us=_window(marker.get("observable_window_us"), "marker.observable_window_us"),
        guard_us=_integer(frame.get("all_off_guard_us"), "frame.all_off_guard_us", minimum=1),
        guard_window_us=_window(frame.get("guard_window_us"), "frame.guard_window_us"),
        cycle_us=_integer(frame.get("nominal_cycle_us"), "frame.nominal_cycle_us", minimum=1),
        order_direction=str(array_order.get("direction", "clockwise")),
        forward_reference=str(array_order.get("forward_reference", "ANT1")),
        states=tuple(states),
    )
    if result.state_names != EXPECTED_STATE_NAMES:
        raise ValueError("hexcal profile order must be ANT1 through ANT6")
    observed_codes = tuple(state.gpio_code_pa3_pa0 for state in result.states)
    if observed_codes != EXPECTED_GPIO_CODES or len(set(observed_codes)) != 6:
        raise ValueError("hexcal GPIO map differs from the frozen ANT1..ANT6 code map")
    if safety.get("all_off_code") != EXPECTED_ALL_OFF_CODE:
        raise ValueError("hexcal ALL_OFF GPIO code must be exactly 1000")
    if EXPECTED_ALL_OFF_CODE in observed_codes:
        raise ValueError("hexcal ALL_OFF GPIO code aliases an active antenna")
    if result.order_direction != "clockwise" or result.forward_reference != "ANT1":
        raise ValueError("hexcal array order must be clockwise from ANT1")
    if result.timer_nominal_hz != 1_000_000 or result.timer_resolution_us != 1:
        raise ValueError("hexcal profile timer contract changed")
    if (
        result.marker_body_us != 180
        or result.marker_observable_us != 200
        or result.guard_us != 20
        or any(state.dwell_us != 200 for state in result.states)
        or result.cycle_us != 1500
    ):
        raise ValueError("hexcal-v1 timing contract changed")
    if result.contract_sha256 != EXPECTED_CONTRACT_SHA256:
        raise ValueError("hexcal profile contract SHA-256 differs from the frozen digest")
    return result


def _resolved_evidence_file(evidence_path: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path must be non-empty")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = evidence_path.parent / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"cannot resolve {label}: {error}") from error


def _sha256_string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def load_hexcal_firmware_evidence(
    path: Path,
    *,
    expected_board_id: str,
    expected_source_commit: str,
    expected_profile: HexcalProfile,
) -> HexcalFirmwareEvidence:
    """Validate image bytes against a target-bound, complete 16 KiB readback."""

    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load firmware evidence: {error}") from error
    root = _mapping(document, "firmware evidence")
    if root.get("schema") != 1 or root.get("evidence_kind") != "hexcal_v1_full_flash_readback":
        raise ValueError("firmware evidence schema or kind is unsupported")
    if root.get("board_id") != expected_board_id:
        raise ValueError("firmware evidence board ID differs from the selected target")
    if root.get("source_commit") != expected_source_commit:
        raise ValueError("firmware evidence source commit differs from the implementation")
    if root.get("profile_file_sha256") != expected_profile.file_sha256:
        raise ValueError("firmware evidence profile file SHA-256 differs")
    if root.get("profile_contract_sha256") != expected_profile.contract_sha256:
        raise ValueError("firmware evidence profile contract SHA-256 differs")
    target_uid = root.get("target_uid")
    board_prefix = "stm32c011-"
    if not expected_board_id.startswith(board_prefix):
        raise ValueError("selected board ID is not an stm32c011 UID-derived ID")
    board_uid = expected_board_id.removeprefix(board_prefix)
    if (
        not isinstance(target_uid, str)
        or not target_uid
        or any(character not in "0123456789abcdef" for character in target_uid)
        or target_uid != board_uid
    ):
        raise ValueError(
            "firmware evidence target UID must exactly match the selected board ID suffix"
        )
    uid_readback = _mapping(root.get("target_uid_readback"), "target_uid_readback")
    firmware_elf = _mapping(root.get("firmware_elf"), "firmware_elf")
    firmware = _mapping(root.get("firmware_bin"), "firmware_bin")
    readback = _mapping(root.get("full_flash_readback"), "full_flash_readback")
    verification = _mapping(root.get("verification"), "verification")
    uid_readback_path = _resolved_evidence_file(
        resolved, uid_readback.get("path"), "target UID readback"
    )
    firmware_elf_path = _resolved_evidence_file(resolved, firmware_elf.get("path"), "firmware ELF")
    firmware_path = _resolved_evidence_file(resolved, firmware.get("path"), "firmware bin")
    readback_path = _resolved_evidence_file(resolved, readback.get("path"), "full flash readback")
    uid_readback_sha = _sha256_string(uid_readback.get("sha256"), "target UID readback SHA-256")
    firmware_elf_sha = _sha256_string(firmware_elf.get("sha256"), "firmware ELF SHA-256")
    firmware_sha = _sha256_string(firmware.get("sha256"), "firmware bin SHA-256")
    readback_sha = _sha256_string(readback.get("sha256"), "full flash readback SHA-256")
    uid_readback_size = _integer(
        uid_readback.get("size_bytes"), "target UID readback size_bytes", minimum=1
    )
    firmware_elf_size = _integer(
        firmware_elf.get("size_bytes"), "firmware ELF size_bytes", minimum=1
    )
    firmware_size = _integer(firmware.get("size_bytes"), "firmware bin size_bytes", minimum=1)
    readback_size = _integer(
        readback.get("size_bytes"), "full flash readback size_bytes", minimum=1
    )
    if uid_readback_size != 12:
        raise ValueError("target UID readback must contain exactly 12 bytes")
    if (
        uid_readback_path.stat().st_size != uid_readback_size
        or sha256_path(uid_readback_path) != uid_readback_sha
        or uid_readback_path.read_bytes().hex() != target_uid
    ):
        raise ValueError("raw target UID readback differs from the selected board identity")
    if (
        firmware_elf_sha != EXPECTED_HEXCAL_ELF_SHA256
        or firmware_sha != EXPECTED_HEXCAL_BIN_SHA256
        or firmware_size != EXPECTED_HEXCAL_BIN_SIZE_BYTES
        or readback_sha != EXPECTED_HEXCAL_FULL_FLASH_SHA256
    ):
        raise ValueError("firmware evidence is not the exact reviewed hexcal-v1 image")
    if readback_size != STM32C011_FLASH_SIZE_BYTES:
        raise ValueError("full flash readback must contain exactly 16 KiB")
    if (
        firmware_elf_path.stat().st_size != firmware_elf_size
        or sha256_path(firmware_elf_path) != firmware_elf_sha
    ):
        raise ValueError("firmware ELF bytes differ from firmware evidence")
    if firmware_path.stat().st_size != firmware_size or sha256_path(firmware_path) != firmware_sha:
        raise ValueError("firmware image bytes differ from firmware evidence")
    if readback_path.stat().st_size != readback_size or sha256_path(readback_path) != readback_sha:
        raise ValueError("full flash readback bytes differ from firmware evidence")
    firmware_bytes = firmware_path.read_bytes()
    readback_bytes = readback_path.read_bytes()
    if readback_bytes[:firmware_size] != firmware_bytes:
        raise ValueError("full flash readback prefix differs from the firmware image")
    if any(value != 0xFF for value in readback_bytes[firmware_size:]):
        raise ValueError("full flash readback tail is not erased")
    required_true = (
        "target_identity_verified",
        "full_flash_readback_verified",
        "image_prefix_matches",
        "erased_tail_verified",
    )
    if any(verification.get(field) is not True for field in required_true):
        raise ValueError("firmware evidence verification attestations are incomplete")
    verified_at = verification.get("verified_at")
    method = verification.get("method")
    if not isinstance(verified_at, str) or not verified_at.strip():
        raise ValueError("firmware evidence verified_at must be non-empty")
    if not isinstance(method, str) or not method.strip():
        raise ValueError("firmware evidence method must be non-empty")
    return HexcalFirmwareEvidence(
        path=resolved,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        board_id=expected_board_id,
        target_uid=target_uid,
        target_uid_readback_path=uid_readback_path,
        target_uid_readback_sha256=uid_readback_sha,
        target_uid_readback_size_bytes=uid_readback_size,
        source_commit=expected_source_commit,
        profile_file_sha256=expected_profile.file_sha256,
        profile_contract_sha256=expected_profile.contract_sha256,
        firmware_elf_path=firmware_elf_path,
        firmware_elf_sha256=firmware_elf_sha,
        firmware_elf_size_bytes=firmware_elf_size,
        firmware_bin_path=firmware_path,
        firmware_bin_sha256=firmware_sha,
        firmware_bin_size_bytes=firmware_size,
        full_flash_readback_path=readback_path,
        full_flash_readback_sha256=readback_sha,
        full_flash_readback_size_bytes=readback_size,
        verified_at=verified_at,
        verification_method=method,
    )


def audit_continuity_metadata(
    metadata: Mapping[str, Any],
    *,
    expected_total_samples: int,
    expected_samples_per_block: int,
    expected_sample_rate_hz: float = 1_000_000.0,
) -> dict[str, Any]:
    """Independently enforce ABI2 flags, counters, block shape, and time evidence."""

    summary = validate_sigmf_continuity(
        metadata,
        expected_total_samples=expected_total_samples,
        expected_samples_per_block=expected_samples_per_block,
    )
    if summary.metadata_abi != 2:
        raise ValueError("hexcal capture requires metadata ABI 2")
    continuity = _mapping(metadata.get("pluto:continuity"), "pluto:continuity")
    blocks = continuity.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("pluto:continuity.blocks must be an array")
    maximum_uncertainty_ns = 0
    maximum_duration_error_ns = 0
    maximum_block_boundary_error_ns = 0
    observed_flags: set[int] = set()
    prior_realtime_start: int | None = None
    prior_realtime_end: int | None = None
    prior_monotonic_start: int | None = None
    prior_monotonic_end: int | None = None
    prior_uncertainty_ns: int | None = None
    for index, raw_block in enumerate(blocks):
        block = _mapping(raw_block, f"blocks[{index}]")
        flags = _integer(block.get("metadata_flags"), f"blocks[{index}].metadata_flags")
        if flags & REQUIRED_METADATA_FLAGS != REQUIRED_METADATA_FLAGS:
            raise ValueError(f"blocks[{index}] lacks required ABI2 validity flags")
        failure = flags & FAILURE_METADATA_FLAGS
        if failure:
            raise ValueError(f"blocks[{index}] reports failure flags 0x{failure:x}")
        observed_flags.add(flags)
        timing = (
            "sample_time_realtime_start_ns",
            "sample_time_realtime_end_ns",
            "sample_time_monotonic_start_ns",
            "sample_time_monotonic_end_ns",
            "sample_time_uncertainty_ns",
        )
        values = {
            name: _integer(block.get(name), f"blocks[{index}].{name}", minimum=1) for name in timing
        }
        if values["sample_time_realtime_end_ns"] <= values["sample_time_realtime_start_ns"]:
            raise ValueError(f"blocks[{index}] realtime interval does not advance")
        if values["sample_time_monotonic_end_ns"] <= values["sample_time_monotonic_start_ns"]:
            raise ValueError(f"blocks[{index}] monotonic interval does not advance")
        expected_duration_ns = round(int(block["sample_count"]) / expected_sample_rate_hz * 1e9)
        realtime_duration = (
            values["sample_time_realtime_end_ns"] - values["sample_time_realtime_start_ns"]
        )
        monotonic_duration = (
            values["sample_time_monotonic_end_ns"] - values["sample_time_monotonic_start_ns"]
        )
        duration_tolerance = 2 * values["sample_time_uncertainty_ns"] + 1_000
        duration_error = max(
            abs(realtime_duration - expected_duration_ns),
            abs(monotonic_duration - expected_duration_ns),
        )
        if duration_error > duration_tolerance:
            raise ValueError(f"blocks[{index}] host time duration disagrees with sample rate")
        if prior_realtime_start is not None:
            assert prior_realtime_end is not None
            assert prior_monotonic_start is not None
            assert prior_monotonic_end is not None
            assert prior_uncertainty_ns is not None
            if (
                values["sample_time_realtime_start_ns"] <= prior_realtime_start
                or values["sample_time_realtime_end_ns"] <= prior_realtime_end
            ):
                raise ValueError(f"blocks[{index}] realtime mapping is not ordered")
            if (
                values["sample_time_monotonic_start_ns"] <= prior_monotonic_start
                or values["sample_time_monotonic_end_ns"] <= prior_monotonic_end
            ):
                raise ValueError(f"blocks[{index}] monotonic mapping is not ordered")
            boundary_error = max(
                abs(values["sample_time_realtime_start_ns"] - prior_realtime_end),
                abs(values["sample_time_monotonic_start_ns"] - prior_monotonic_end),
            )
            boundary_tolerance = prior_uncertainty_ns + values["sample_time_uncertainty_ns"] + 1_000
            if boundary_error > boundary_tolerance:
                raise ValueError(f"blocks[{index}] cross-block time boundary is not contiguous")
            maximum_block_boundary_error_ns = max(maximum_block_boundary_error_ns, boundary_error)
        prior_realtime_start = values["sample_time_realtime_start_ns"]
        prior_realtime_end = values["sample_time_realtime_end_ns"]
        prior_monotonic_start = values["sample_time_monotonic_start_ns"]
        prior_monotonic_end = values["sample_time_monotonic_end_ns"]
        prior_uncertainty_ns = values["sample_time_uncertainty_ns"]
        maximum_duration_error_ns = max(maximum_duration_error_ns, duration_error)
        maximum_uncertainty_ns = max(maximum_uncertainty_ns, values["sample_time_uncertainty_ns"])
    return {
        **summary.as_dict(),
        "required_metadata_flags": REQUIRED_METADATA_FLAGS,
        "failure_metadata_flags_mask": FAILURE_METADATA_FLAGS,
        "observed_metadata_flags": sorted(observed_flags),
        "maximum_sample_time_uncertainty_ns": maximum_uncertainty_ns,
        "maximum_block_duration_error_ns": maximum_duration_error_ns,
        "maximum_block_boundary_error_ns": maximum_block_boundary_error_ns,
        "abi2_flags_counters_order_and_rate_verified": True,
    }


def load_ci16_channel(
    data_file: Path,
    *,
    sample_count: int,
    receiver_count: int,
    channel: int,
) -> np.ndarray:
    """Load one channel from canonical sample-major dual-CI16 SigMF data."""

    if receiver_count != 2 or channel not in (0, 1):
        raise ValueError("hexcal requires canonical dual-RX data and RX1 or RX2")
    raw = np.memmap(data_file, dtype="<i2", mode="r")
    expected = sample_count * receiver_count * 2
    if raw.size != expected:
        raise ValueError(f"CI16 data has {raw.size} components; expected {expected}")
    components = raw.reshape(sample_count, receiver_count, 2)
    output = np.empty(sample_count, dtype=np.complex64)
    for start in range(0, sample_count, 250_000):
        stop = min(sample_count, start + 250_000)
        output[start:stop].real = components[start:stop, channel, 0]
        output[start:stop].imag = components[start:stop, channel, 1]
    return output


def _runs(mask: npt.NDArray[np.bool_], value: bool) -> list[tuple[int, int]]:
    selected = np.asarray(mask == value, dtype=np.int8)
    changes = np.flatnonzero(np.diff(np.pad(selected, (1, 1))))
    return [(int(start), int(stop)) for start, stop in changes.reshape(-1, 2)]


def _slice_median(values: np.ndarray, start: float, stop: float) -> float:
    left = max(0, int(math.ceil(start)))
    right = min(values.size, int(math.floor(stop)))
    if right <= left:
        return float("nan")
    return float(np.median(values[left:right]))


def _candidate_chain(
    candidates: Sequence[tuple[int, int]],
    seed: int,
    *,
    nominal_period_bins: float,
    tolerance_bins: float,
) -> list[tuple[int, int]]:
    chain = [candidates[seed]]
    cursor = seed + 1
    current = float(candidates[seed][0])
    while cursor < len(candidates):
        low = current + nominal_period_bins - tolerance_bins
        high = current + nominal_period_bins + tolerance_bins
        while cursor < len(candidates) and candidates[cursor][0] < low:
            cursor += 1
        options: list[tuple[float, int]] = []
        probe = cursor
        while probe < len(candidates) and candidates[probe][0] <= high:
            options.append((abs(candidates[probe][0] - current - nominal_period_bins), probe))
            probe += 1
        if not options:
            break
        _, selected = min(options)
        chain.append(candidates[selected])
        current = float(candidates[selected][0])
        cursor = selected + 1
    return chain


def _alignment_contrast_db(
    amplitude_db: np.ndarray,
    starts: Sequence[int],
    profile: HexcalProfile,
    *,
    bin_us: float,
) -> tuple[float, float, float]:
    active: list[float] = []
    off: list[float] = []
    for first, second in zip(starts[:80], starts[1:81], strict=False):
        period = float(second - first)
        scale = period / (profile.cycle_us / bin_us)
        marker_stop = first + (profile.marker_body_us - EDGE_EXCLUSION_US) / bin_us * scale
        off.append(
            _slice_median(
                amplitude_db,
                first + EDGE_EXCLUSION_US / bin_us * scale,
                marker_stop,
            )
        )
        for state_index, state in enumerate(profile.states):
            active_start_us = (
                profile.marker_body_us
                + profile.guard_us
                + state_index * (state.dwell_us + profile.guard_us)
            )
            active.append(
                _slice_median(
                    amplitude_db,
                    first + (active_start_us + EDGE_EXCLUSION_US) / bin_us * scale,
                    first + (active_start_us + state.dwell_us - EDGE_EXCLUSION_US) / bin_us * scale,
                )
            )
            guard_start_us = active_start_us - profile.guard_us
            off.append(
                _slice_median(
                    amplitude_db,
                    first + (guard_start_us + EDGE_EXCLUSION_US) / bin_us * scale,
                    first + (active_start_us - EDGE_EXCLUSION_US) / bin_us * scale,
                )
            )
    active_array = np.asarray(active, dtype=float)
    off_array = np.asarray(off, dtype=float)
    active_array = active_array[np.isfinite(active_array)]
    off_array = off_array[np.isfinite(off_array)]
    if not active_array.size or not off_array.size:
        return float("-inf"), float("nan"), float("nan")
    active_level = float(np.median(active_array))
    off_level = float(np.median(off_array))
    return active_level - off_level, active_level, off_level


def _find_marker_chain(
    present: npt.NDArray[np.bool_],
    amplitude_db: np.ndarray,
    profile: HexcalProfile,
    *,
    bin_us: float,
) -> tuple[list[tuple[int, int]], dict[str, float | int]]:
    absent_runs = _runs(present, False)
    minimum_bins = max(2, math.floor(profile.marker_window_us[0] / bin_us) - 3)
    maximum_bins = math.ceil(profile.marker_window_us[1] / bin_us) + 3
    candidates = [
        run
        for run in absent_runs
        if minimum_bins <= run[1] - run[0] <= maximum_bins and run[0] > 0 and run[1] < present.size
    ]
    if len(candidates) < 3:
        raise HexcalAnalysisError("no repeated long ALL_OFF marker is visible")
    nominal_bins = profile.cycle_us / bin_us
    tolerance_bins = nominal_bins * 0.05
    seed_limit = candidates[0][0] + 2.1 * nominal_bins
    ranked: list[tuple[float, int, list[tuple[int, int]], float, float]] = []
    for seed, candidate in enumerate(candidates):
        if candidate[0] > seed_limit:
            break
        chain = _candidate_chain(
            candidates,
            seed,
            nominal_period_bins=nominal_bins,
            tolerance_bins=tolerance_bins,
        )
        if len(chain) < 3:
            continue
        starts = [item[0] for item in chain]
        contrast, active_level, off_level = _alignment_contrast_db(
            amplitude_db, starts, profile, bin_us=bin_us
        )
        ranked.append((contrast, len(chain), chain, active_level, off_level))
    if not ranked:
        raise HexcalAnalysisError("no periodic marker chain satisfies the RF schedule")
    maximum_length = max(item[1] for item in ranked)
    minimum_eligible_length = max(3, math.floor(maximum_length * 0.95))
    eligible = [item for item in ranked if item[1] >= minimum_eligible_length]
    best = max(eligible, key=lambda item: (item[0], item[1]))
    if not math.isfinite(best[0]):
        raise HexcalAnalysisError("periodic marker chain has no finite RF contrast")
    contrast, _, chain, active_level, off_level = best
    return chain, {
        "candidate_count": len(candidates),
        "marker_count": len(chain),
        "contrast_db": contrast,
        "active_level_db": active_level,
        "all_off_level_db": off_level,
    }


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)], dtype=float)
    if not finite.size:
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "mean": None,
            "maximum": None,
            "std": None,
        }
    return {
        "count": int(finite.size),
        "minimum": float(np.min(finite)),
        "median": float(np.median(finite)),
        "mean": float(np.mean(finite)),
        "maximum": float(np.max(finite)),
        "std": float(np.std(finite)),
    }


def _complex_dict(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def wrapped_phase_deg(value: float) -> float:
    return float((value + 180.0) % 360.0 - 180.0)


def _circular_summary_deg(values: npt.ArrayLike) -> tuple[float, float, float]:
    radians = np.deg2rad(np.asarray(values, dtype=float))
    unit = np.exp(1j * radians)
    mean = complex(np.mean(unit))
    resultant = min(1.0, abs(mean))
    phase = wrapped_phase_deg(math.degrees(math.atan2(mean.imag, mean.real)))
    std = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(resultant, 1e-15)))))
    return phase, resultant, std


def six_point_dft(values: Sequence[complex]) -> tuple[complex, ...]:
    """Return unitary-by-count circular modes m=0..5 in clockwise ANT order."""

    array = np.asarray(values, dtype=np.complex128)
    if array.shape != (6,) or not np.all(np.isfinite(array)):
        raise ValueError("six-point DFT requires six finite complex values")
    return tuple(complex(value) for value in np.fft.fft(array) / 6.0)


def dft_document(values: Sequence[complex]) -> dict[str, Any]:
    modes = six_point_dft(values)
    mode_zero = abs(modes[0])
    nonzero_rms = math.sqrt(sum(abs(value) ** 2 for value in modes[1:]) / 5.0)
    rejection = 20.0 * math.log10(max(mode_zero, 1e-15) / max(nonzero_rms, 1e-15))
    largest_noncommon = max(abs(value) for value in modes[1:])
    largest_noncommon_dbc = 20.0 * math.log10(max(largest_noncommon, 1e-15) / max(mode_zero, 1e-15))
    return {
        "normalization": "(1/6) sum_i z_i exp(-j 2 pi m i / 6)",
        "modes": [
            {
                "mode": index,
                "complex": _complex_dict(value),
                "amplitude": abs(value),
                "power": abs(value) ** 2,
                "phase_deg": wrapped_phase_deg(math.degrees(math.atan2(value.imag, value.real))),
            }
            for index, value in enumerate(modes)
        ],
        "mode0_to_nonzero_rms_db": rejection,
        "largest_noncommon_mode_dbc": largest_noncommon_dbc,
        "largest_noncommon_mode_target_maximum_dbc": -20.0,
        "largest_noncommon_mode_minimum_maximum_dbc": -15.0,
        "largest_noncommon_mode_target_passed": largest_noncommon_dbc <= -20.0,
        "largest_noncommon_mode_minimum_passed": largest_noncommon_dbc <= -15.0,
    }


def analyze_hexcal_samples(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_offset_hz: float,
    profile: HexcalProfile,
    continuity_verified: bool,
) -> dict[str, Any]:
    """Decode and estimate six end-to-end path coefficients from RX2 IQ.

    Each cycle removes one geometric-mean amplitude and one six-element circular
    phase centre.  ANT1 remains the physical forward-direction diagnostic but is
    never the statistical phase reference.  The returned normalized manifold is
    meaningful without RX1 provided all six states occur in one continuous ABI2
    stream.  This is an end-to-end near-field manifold, not a separable
    electronics-only calibration.
    """

    values = np.asarray(samples)
    if values.ndim != 1 or not np.iscomplexobj(values) or values.size < 3_000:
        raise HexcalAnalysisError("hexcal IQ must be a one-dimensional complex capture")
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise HexcalAnalysisError("hexcal IQ contains non-finite samples")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be positive and finite")
    if not math.isfinite(tone_offset_hz) or not 0.0 < abs(tone_offset_hz) < sample_rate_hz / 2:
        raise ValueError("tone_offset_hz must be finite, nonzero, and inside Nyquist")
    bin_samples = round(sample_rate_hz * DETECTION_BIN_US * 1e-6)
    if bin_samples < 2:
        raise ValueError("sample rate is too low for five-microsecond detection bins")
    bin_us = bin_samples / sample_rate_hz * 1e6
    if abs(bin_us - DETECTION_BIN_US) > 0.25:
        raise ValueError("sample rate does not provide an exact five-microsecond detector bin")

    sample_numbers = np.arange(values.size, dtype=np.float64)
    oscillator = np.exp(-2j * np.pi * tone_offset_hz / sample_rate_hz * sample_numbers)
    baseband = values.astype(np.complex128, copy=False) * oscillator
    complete_bins = values.size // bin_samples
    coherent_bins = (
        baseband[: complete_bins * bin_samples].reshape(complete_bins, bin_samples).mean(axis=1)
    )
    amplitude_db = 20.0 * np.log10(np.maximum(np.abs(coherent_bins), 1e-12))
    low_level = float(np.percentile(amplitude_db, 10.0))
    high_level = float(np.percentile(amplitude_db, 55.0))
    if high_level - low_level < 3.0:
        raise HexcalAnalysisError("ALL_OFF marker has no usable RF amplitude contrast")
    threshold_db = (low_level + high_level) / 2.0
    present = amplitude_db > threshold_db
    marker_chain, alignment = _find_marker_chain(present, amplitude_db, profile, bin_us=bin_us)
    marker_starts_bins = [run[0] for run in marker_chain]
    marker_starts_samples = [start * bin_samples for start in marker_starts_bins]
    periods_us = [
        (second - first) / sample_rate_hz * 1e6
        for first, second in zip(marker_starts_samples, marker_starts_samples[1:], strict=False)
    ]
    timing_kernel = np.ones(bin_samples, dtype=np.float64) / bin_samples
    timing_envelope = np.abs(np.convolve(baseband, timing_kernel, mode="same"))
    timing_amplitude_db = 20.0 * np.log10(np.maximum(timing_envelope, 1e-12))
    timing_present = timing_amplitude_db > threshold_db
    absent_runs = _runs(timing_present, False)
    present_runs = _runs(timing_present, True)

    def containing_duration(
        runs: Sequence[tuple[int, int]],
        point: float,
        *,
        edge_adjustment_samples: int,
    ) -> float:
        target = int(round(point))
        for start, stop in runs:
            if start <= target < stop:
                adjusted = max(0, stop - start + edge_adjustment_samples)
                return adjusted / sample_rate_hz * 1e6
            if start > target:
                break
        return float("nan")

    marker_durations_us = [
        containing_duration(
            absent_runs,
            start + profile.marker_observable_us / 2.0 * sample_rate_hz * 1e-6,
            edge_adjustment_samples=bin_samples - 1,
        )
        for start in marker_starts_samples
    ]

    cycle_phasors: list[list[complex]] = []
    cycle_pilot_snr_db: list[list[float]] = []
    cycle_isolation_db: list[list[float]] = []
    state_center_samples: list[list[float]] = []
    guard_durations_us: list[float] = []
    active_durations_us: list[list[float]] = []
    rejected_cycles: list[dict[str, Any]] = []
    for cycle_index, (cycle_start, cycle_stop) in enumerate(
        zip(marker_starts_samples, marker_starts_samples[1:], strict=False)
    ):
        period_samples = cycle_stop - cycle_start
        period_us = period_samples / sample_rate_hz * 1e6
        if not profile.cycle_us * 0.95 <= period_us <= profile.cycle_us * 1.05:
            rejected_cycles.append({"cycle": cycle_index, "reason": "cycle_period_outside_window"})
            continue
        scale = period_samples / (profile.cycle_us * sample_rate_hz * 1e-6)
        phasors: list[complex] = []
        snrs: list[float] = []
        isolations: list[float] = []
        centers: list[float] = []
        measured_guards: list[float] = []
        measured_active: list[float] = []
        valid = True
        for state_index, state in enumerate(profile.states):
            active_start_us = (
                profile.marker_body_us
                + profile.guard_us
                + state_index * (state.dwell_us + profile.guard_us)
            )
            active_start = cycle_start + active_start_us * sample_rate_hz * 1e-6 * scale
            active_stop = active_start + state.dwell_us * sample_rate_hz * 1e-6 * scale
            edge = EDGE_EXCLUSION_US * sample_rate_hz * 1e-6 * scale
            left = int(math.ceil(active_start + edge))
            right = int(math.floor(active_stop - edge))
            before_left = int(
                math.ceil(
                    active_start
                    - (profile.guard_us - EDGE_EXCLUSION_US) * sample_rate_hz * 1e-6 * scale
                )
            )
            before_right = int(
                math.floor(active_start - EDGE_EXCLUSION_US * sample_rate_hz * 1e-6 * scale)
            )
            after_start_us = active_start_us + state.dwell_us
            after_start = cycle_start + after_start_us * sample_rate_hz * 1e-6 * scale
            after_stop_us = after_start_us + profile.guard_us
            after_stop = cycle_start + after_stop_us * sample_rate_hz * 1e-6 * scale
            after_left = int(
                math.ceil(after_start + EDGE_EXCLUSION_US * sample_rate_hz * 1e-6 * scale)
            )
            after_right = int(
                math.floor(after_stop - EDGE_EXCLUSION_US * sample_rate_hz * 1e-6 * scale)
            )
            if (
                right - left < 20
                or before_right - before_left < 4
                or after_right - after_left < 4
                or right > baseband.size
                or after_right > baseband.size
            ):
                valid = False
                break
            active_values = baseband[left:right]
            before_values = baseband[before_left:before_right]
            after_values = baseband[after_left:after_right]
            active_mean = complex(np.mean(active_values))
            before_mean = complex(np.mean(before_values))
            after_mean = complex(np.mean(after_values))
            before_center = (before_left + before_right) / 2.0
            after_center = (after_left + after_right) / 2.0
            active_center = (left + right) / 2.0
            interpolation_fraction = (active_center - before_center) / (
                after_center - before_center
            )
            interpolated_null = before_mean + interpolation_fraction * (after_mean - before_mean)
            delta = active_mean - interpolated_null
            active_variance = float(np.mean(np.abs(active_values - active_mean) ** 2))
            before_variance = float(np.mean(np.abs(before_values - before_mean) ** 2))
            after_variance = float(np.mean(np.abs(after_values - after_mean) ** 2))
            noise = math.sqrt(
                active_variance
                + (1.0 - interpolation_fraction) ** 2 * before_variance
                + interpolation_fraction**2 * after_variance
            )
            snr = 20.0 * math.log10(max(abs(delta), 1e-15) / max(noise, 1e-15))
            active_rms = math.sqrt(float(np.mean(np.abs(active_values) ** 2)))
            null_samples = np.concatenate((before_values, after_values))
            null_rms = math.sqrt(float(np.mean(np.abs(null_samples) ** 2)))
            isolation = 20.0 * math.log10(max(active_rms, 1e-15) / max(null_rms, 1e-15))
            phasors.append(delta)
            snrs.append(snr)
            isolations.append(isolation)
            centers.append((left + right) / 2.0)
            guard_mid_sample = active_start - profile.guard_us / 2.0 * sample_rate_hz * 1e-6 * scale
            active_mid_sample = active_start + state.dwell_us / 2.0 * sample_rate_hz * 1e-6 * scale
            if state_index > 0:
                measured_guards.append(
                    containing_duration(
                        absent_runs,
                        guard_mid_sample,
                        edge_adjustment_samples=bin_samples - 1,
                    )
                )
            measured_active.append(
                containing_duration(
                    present_runs,
                    active_mid_sample,
                    edge_adjustment_samples=-(bin_samples - 1),
                )
            )
        if not valid or len(phasors) != 6 or any(abs(value) <= 1e-12 for value in phasors):
            rejected_cycles.append({"cycle": cycle_index, "reason": "truncated_or_zero_state"})
            continue
        cycle_phasors.append(phasors)
        cycle_pilot_snr_db.append(snrs)
        cycle_isolation_db.append(isolations)
        state_center_samples.append(centers)
        guard_durations_us.extend(measured_guards)
        active_durations_us.append(measured_active)

    if len(cycle_phasors) < 2:
        raise HexcalAnalysisError("fewer than two complete six-state cycles decoded")
    phasor_array = np.asarray(cycle_phasors, dtype=np.complex128)
    center_array = np.asarray(state_center_samples, dtype=np.float64)
    per_state_residual_hz: list[float] = []
    for state_index in range(phasor_array.shape[1]):
        state_phase = np.unwrap(np.angle(phasor_array[:, state_index]))
        state_time = center_array[:, state_index] / sample_rate_hz
        state_slope = float(np.polyfit(state_time - state_time[0], state_phase, 1)[0])
        per_state_residual_hz.append(state_slope / (2.0 * math.pi))
    residual_hz = float(np.median(np.asarray(per_state_residual_hz, dtype=float)))
    sample_time = (center_array - center_array[0, 0]) / sample_rate_hz
    corrected = phasor_array * np.exp(-2j * np.pi * residual_hz * sample_time)
    amplitudes = np.maximum(np.abs(corrected), 1e-15)
    cycle_gain_db = 20.0 * np.log10(amplitudes)
    cycle_gain_db -= np.mean(cycle_gain_db, axis=1, keepdims=True)
    unit_phasors = corrected / amplitudes
    phase_centres = np.mean(unit_phasors, axis=1)
    phase_gauge_resultants = np.abs(phase_centres)
    if np.any(phase_gauge_resultants < 1e-12):
        raise HexcalAnalysisError("six-element circular phase centre is undefined")
    common_phase = np.angle(phase_centres)
    normalized_phase_deg = np.rad2deg(
        np.angle(corrected * np.exp(-1j * common_phase[:, np.newaxis]))
    )
    ant1_relative_phase_deg = np.rad2deg(np.angle(corrected / corrected[:, [0]]))

    state_documents: list[dict[str, Any]] = []
    aggregate_values: list[complex] = []
    snr_array = np.asarray(cycle_pilot_snr_db, dtype=float)
    isolation_array = np.asarray(cycle_isolation_db, dtype=float)
    for state_index, state in enumerate(profile.states):
        phase, coherence, phase_std = _circular_summary_deg(normalized_phase_deg[:, state_index])
        ant1_relative_phase, _, _ = _circular_summary_deg(ant1_relative_phase_deg[:, state_index])
        gain = float(np.median(cycle_gain_db[:, state_index]))
        gain_std = float(np.std(cycle_gain_db[:, state_index]))
        aggregate = 10.0 ** (gain / 20.0) * np.exp(1j * math.radians(phase))
        aggregate_values.append(complex(aggregate))
        ant1_relative_aggregate = 10.0 ** (gain / 20.0) * np.exp(
            1j * math.radians(ant1_relative_phase)
        )
        even_phase, _, _ = _circular_summary_deg(normalized_phase_deg[0::2, state_index])
        odd_values = normalized_phase_deg[1::2, state_index]
        if odd_values.size:
            odd_phase, _, _ = _circular_summary_deg(odd_values)
            even_odd = max(0.0, math.cos(math.radians(wrapped_phase_deg(even_phase - odd_phase))))
        else:
            even_odd = 0.0
        state_documents.append(
            {
                "name": state.name,
                "normalized_complex": _complex_dict(complex(aggregate)),
                "normalized_gain_db": gain,
                "normalized_gain_std_db": gain_std,
                "phase_circular_centered_deg": phase,
                "relative_complex": _complex_dict(complex(ant1_relative_aggregate)),
                "relative_gain_db": gain,
                "relative_gain_std_db": gain_std,
                "phase_relative_to_ant1_deg": ant1_relative_phase,
                "cycle_coherence": coherence,
                "cycle_phase_std_deg": phase_std,
                "even_odd_phase_agreement": even_odd,
                "pilot_snr_db": float(np.median(snr_array[:, state_index])),
                "null_isolation_db": float(np.median(isolation_array[:, state_index])),
                "cycle_relative_gain_db": [float(value) for value in cycle_gain_db[:, state_index]],
                "cycle_normalized_gain_db": [
                    float(value) for value in cycle_gain_db[:, state_index]
                ],
                "cycle_phase_circular_centered_deg": [
                    wrapped_phase_deg(float(value))
                    for value in normalized_phase_deg[:, state_index]
                ],
                "cycle_relative_phase_deg": [
                    wrapped_phase_deg(float(value))
                    for value in ant1_relative_phase_deg[:, state_index]
                ],
                "cycle_phase_relative_to_ant1_deg": [
                    wrapped_phase_deg(float(value))
                    for value in ant1_relative_phase_deg[:, state_index]
                ],
            }
        )

    complete_intervals = len(marker_chain) - 1
    valid_cycles = len(cycle_phasors)
    active_by_state = np.asarray(active_durations_us, dtype=float)
    return {
        "schema": 1,
        "analysis_kind": "hexcal_v1_rx2_end_to_end_complex_manifold",
        "analyzer_version": ANALYZER_VERSION,
        "profile": profile.as_dict(),
        "sample_rate_hz": float(sample_rate_hz),
        "sample_count": int(values.size),
        "tone_offset_hz": float(tone_offset_hz),
        "continuity_verified": bool(continuity_verified),
        "alignment": {
            **alignment,
            "threshold_db": threshold_db,
            "detector_bin_us": bin_us,
            "marker_start_samples": marker_starts_samples,
        },
        "timing": {
            "nominal_cycle_us": profile.cycle_us,
            "cycle_us": _stats(periods_us),
            "marker_observable_us": _stats(marker_durations_us),
            "guard_observable_us": _stats(guard_durations_us),
            "active_observable_us_by_state": {
                state.name: _stats(active_by_state[:, index].tolist())
                for index, state in enumerate(profile.states)
            },
        },
        "complete_marker_intervals": complete_intervals,
        "valid_cycle_count": valid_cycles,
        "rejected_cycle_count": len(rejected_cycles),
        "decoded_cycle_fraction": valid_cycles / max(complete_intervals, 1),
        "rejected_cycles": rejected_cycles,
        "residual_common_tone_offset_hz": residual_hz,
        "common_tone_fit": {
            "method": "median of six independently unwrapped repeated-state phase slopes",
            "per_state_residual_hz": [
                {
                    "name": state.name,
                    "residual_hz": float(per_state_residual_hz[index]),
                }
                for index, state in enumerate(profile.states)
            ],
            "median_residual_hz": residual_hz,
            "peak_to_peak_residual_hz": float(
                np.ptp(np.asarray(per_state_residual_hz, dtype=float))
            ),
            "reference_element": "none",
        },
        "normalization_gauge": {
            "amplitude_method": "per-cycle six-element geometric-mean amplitude",
            "phase_method": "per-cycle six-element circular phase centre",
            "phase_reference_element": "none",
            "phase_gauge_resultant": _stats(phase_gauge_resultants.tolist()),
            "minimum_resultant_for_admission": MINIMUM_PHASE_GAUGE_RESULTANT,
            "cycle_removed_common_phase_deg": [
                wrapped_phase_deg(math.degrees(float(value))) for value in common_phase
            ],
        },
        "phase_reference": (
            "six-element circular phase centre per continuous cycle; ANT1-relative "
            "values are retained as diagnostics only"
        ),
        "null_estimator": (
            "complex linear interpolation of edge-trimmed adjacent ALL_OFF means "
            "to each selected-dwell centre; ANT6 uses the following frame marker"
        ),
        "pilot_snr_estimator": (
            "selected-minus-interpolated-null amplitude divided by the root-sum "
            "per-sample complex noise variance of the selected window and the two "
            "interpolation-weighted ALL_OFF windows; no sqrt(N) coherent-mean gain"
        ),
        "timing_estimator": (
            "five-sample coherent moving-average RF edges with the known filter edge "
            "extension removed before duration gates"
        ),
        "states": state_documents,
        "six_point_dft": dft_document(aggregate_values),
        "interpretation": (
            "End-to-end centered near-field manifold including selector, PCB, cable, "
            "antenna and coupling. It is not an electronics-only calibration and is "
            "not automatically transferable to another source position or far field."
        ),
    }


def evaluate_hexcal_quality(
    analysis: Mapping[str, Any],
    *,
    headroom_passed: bool,
    minimum_complete_cycles: int = 600,
    minimum_decoded_fraction: float = 0.98,
    minimum_marker_contrast_db: float = 20.0,
    minimum_state_snr_db: float = 20.0,
    minimum_state_coherence: float = 0.995,
    maximum_state_phase_std_deg: float = 6.0,
    minimum_null_isolation_db: float = 20.0,
    minimum_phase_gauge_resultant: float = MINIMUM_PHASE_GAUGE_RESULTANT,
) -> dict[str, Any]:
    """Apply explicit acceptance gates without hiding rejected measurements."""

    global_reasons: list[str] = []
    if not headroom_passed:
        global_reasons.append("adc_headroom_admission_failed")
    if analysis.get("continuity_verified") is not True:
        global_reasons.append("capture_continuity_not_verified")
    if int(analysis.get("valid_cycle_count", 0)) < minimum_complete_cycles:
        global_reasons.append("complete_cycle_count_below_minimum")
    decoded_fraction = float(analysis.get("decoded_cycle_fraction", float("nan")))
    if not math.isfinite(decoded_fraction) or decoded_fraction < minimum_decoded_fraction:
        global_reasons.append("decoded_cycle_fraction_below_minimum")
    alignment = _mapping(analysis.get("alignment"), "analysis.alignment")
    marker_contrast = float(alignment.get("contrast_db", float("nan")))
    if not math.isfinite(marker_contrast) or marker_contrast < minimum_marker_contrast_db:
        global_reasons.append("marker_contrast_below_minimum")
    normalization_gauge = _mapping(
        analysis.get("normalization_gauge"), "analysis.normalization_gauge"
    )
    gauge_stats = _mapping(
        normalization_gauge.get("phase_gauge_resultant"),
        "analysis.normalization_gauge.phase_gauge_resultant",
    )
    minimum_gauge_resultant = gauge_stats.get("minimum")
    if (
        not isinstance(minimum_gauge_resultant, (int, float))
        or isinstance(minimum_gauge_resultant, bool)
        or not math.isfinite(float(minimum_gauge_resultant))
        or float(minimum_gauge_resultant) < minimum_phase_gauge_resultant
    ):
        global_reasons.append("circular_phase_gauge_ill_conditioned")
    timing = _mapping(analysis.get("timing"), "analysis.timing")
    cycle = _mapping(timing.get("cycle_us"), "analysis.timing.cycle_us")
    marker = _mapping(
        timing.get("marker_observable_us"),
        "analysis.timing.marker_observable_us",
    )
    cycle_nominal = float(timing.get("nominal_cycle_us", 0.0))

    def timing_stats_pass(stats: Mapping[str, Any], *, nominal: float, expected_count: int) -> bool:
        minimum = stats.get("minimum")
        maximum = stats.get("maximum")
        count = stats.get("count")
        return (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and math.isfinite(float(minimum))
            and isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and math.isfinite(float(maximum))
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count == expected_count
            and nominal * 0.95 <= float(minimum)
            and float(maximum) <= nominal * 1.05
        )

    complete_intervals = int(analysis.get("complete_marker_intervals", 0))
    if not timing_stats_pass(cycle, nominal=cycle_nominal, expected_count=complete_intervals):
        global_reasons.append("cycle_timing_outside_profile_window")
    profile_document = _mapping(analysis.get("profile"), "analysis.profile")
    marker_nominal = float(profile_document.get("marker_observable_us", 0.0))
    marker_count = int(alignment.get("marker_count", 0))
    if not timing_stats_pass(marker, nominal=marker_nominal, expected_count=marker_count):
        global_reasons.append("marker_timing_outside_profile_window")
    guard = _mapping(timing.get("guard_observable_us"), "analysis.timing.guard_observable_us")
    guard_minimum = guard.get("minimum")
    valid_cycles = int(analysis.get("valid_cycle_count", 0))
    expected_guard_observations = valid_cycles * 5
    if (
        not isinstance(guard_minimum, (int, float))
        or isinstance(guard_minimum, bool)
        or not math.isfinite(float(guard_minimum))
        or float(guard_minimum) < 18.0
        or guard.get("count") != expected_guard_observations
    ):
        global_reasons.append("ordinary_guard_timing_below_minimum_or_unobserved")
    active_timing = _mapping(
        timing.get("active_observable_us_by_state"),
        "analysis.timing.active_observable_us_by_state",
    )
    profile_states = profile_document.get("states")
    if not isinstance(profile_states, list) or len(profile_states) != 6:
        raise ValueError("analysis profile must contain six timing states")
    for index, name in enumerate(EXPECTED_STATE_NAMES):
        observed = _mapping(active_timing.get(name), f"active timing {name}")
        profile_state = _mapping(profile_states[index], f"profile state {name}")
        dwell_nominal = float(profile_state.get("dwell_us", 0.0))
        if not timing_stats_pass(observed, nominal=dwell_nominal, expected_count=valid_cycles):
            global_reasons.append(f"{name.lower()}_dwell_timing_outside_window_or_unobserved")

    raw_states = analysis.get("states")
    if not isinstance(raw_states, list) or len(raw_states) != 6:
        raise ValueError("analysis must contain six state estimates")
    state_results: list[dict[str, Any]] = []
    for index, raw_state in enumerate(raw_states):
        state = _mapping(raw_state, f"analysis.states[{index}]")
        reasons = []
        if state.get("name") != EXPECTED_STATE_NAMES[index]:
            reasons.append("state_order_mismatch")
        pilot_snr = float(state.get("pilot_snr_db", float("nan")))
        if not math.isfinite(pilot_snr) or pilot_snr < minimum_state_snr_db:
            reasons.append("pilot_snr_below_minimum")
        coherence = float(state.get("cycle_coherence", float("nan")))
        if not math.isfinite(coherence) or coherence < minimum_state_coherence:
            reasons.append("cycle_coherence_below_minimum")
        phase_std = float(state.get("cycle_phase_std_deg", float("nan")))
        if not math.isfinite(phase_std) or phase_std > maximum_state_phase_std_deg:
            reasons.append("cycle_phase_std_above_maximum")
        null_isolation = float(state.get("null_isolation_db", float("nan")))
        if not math.isfinite(null_isolation) or null_isolation < minimum_null_isolation_db:
            reasons.append("null_isolation_below_minimum")
        state_results.append(
            {
                "name": EXPECTED_STATE_NAMES[index],
                "passed": not reasons,
                "rejection_reasons": reasons,
            }
        )
    passed = not global_reasons and all(item["passed"] for item in state_results)
    return {
        "passed": passed,
        "global_rejection_reasons": global_reasons,
        "states": state_results,
        "thresholds": {
            "minimum_complete_cycles": minimum_complete_cycles,
            "minimum_decoded_fraction": minimum_decoded_fraction,
            "minimum_marker_contrast_db": minimum_marker_contrast_db,
            "minimum_state_snr_db": minimum_state_snr_db,
            "minimum_state_coherence": minimum_state_coherence,
            "maximum_state_phase_std_deg": maximum_state_phase_std_deg,
            "minimum_null_isolation_db": minimum_null_isolation_db,
            "minimum_phase_gauge_resultant": minimum_phase_gauge_resultant,
        },
    }


def correction_coefficients(states: Sequence[Mapping[str, Any]]) -> tuple[complex, ...]:
    """Invert one symmetrically normalized six-state manifold."""

    if len(states) != 6:
        raise ValueError("correction requires exactly six states")
    gains = np.asarray([float(state["normalized_gain_db"]) for state in states], dtype=float)
    phases = np.asarray(
        [float(state["phase_circular_centered_deg"]) for state in states], dtype=float
    )
    correction_gain = -gains
    correction_gain -= np.mean(correction_gain)
    correction_phase = -phases
    return tuple(
        complex(10.0 ** (gain / 20.0) * np.exp(1j * math.radians(phase)))
        for gain, phase in zip(correction_gain, correction_phase, strict=True)
    )


__all__ = [
    "ANALYZER_VERSION",
    "EXPECTED_PROFILE_ID",
    "EXPECTED_CONTRACT_SHA256",
    "EXPECTED_HEXCAL_ELF_SHA256",
    "EXPECTED_HEXCAL_BIN_SHA256",
    "EXPECTED_HEXCAL_BIN_SIZE_BYTES",
    "EXPECTED_HEXCAL_FULL_FLASH_SHA256",
    "EXPECTED_GPIO_CODES",
    "EXPECTED_ALL_OFF_CODE",
    "EXPECTED_STATE_NAMES",
    "FAILURE_METADATA_FLAGS",
    "HexcalAnalysisError",
    "HexcalFirmwareEvidence",
    "HexcalProfile",
    "HexcalState",
    "HEXCAL_ANALYSIS_SOURCE_FILES",
    "HEXCAL_AGGREGATION_SOURCE_FILES",
    "MINIMUM_PHASE_GAUGE_RESULTANT",
    "PLUTO_PLUS_UTILS_IMPORTED_MODULES",
    "PLUTO_PLUS_UTILS_LOCK_FILES",
    "PLUTO_PLUS_UTILS_PIN",
    "PLUTO_PLUS_UTILS_PYTHON",
    "PLUTO_PLUS_UTILS_PYTHON_PREFIX",
    "PLUTO_PLUS_UTILS_REPOSITORY",
    "REQUIRED_METADATA_FLAGS",
    "TX1_ACTIVE_DDS_INDICES",
    "TX1_INACTIVE_DDS_INDICES",
    "analyze_hexcal_samples",
    "attest_pluto_plus_utils_source",
    "attest_source_files_at_commit",
    "audit_continuity_metadata",
    "canonical_json_sha256",
    "correction_coefficients",
    "dft_document",
    "evaluate_hexcal_quality",
    "load_ci16_channel",
    "load_hexcal_firmware_evidence",
    "load_hexcal_profile",
    "sha256_path",
    "six_point_dft",
    "validate_tx1_rf_readback_evidence",
    "wrapped_phase_deg",
    "write_json_atomic",
]
