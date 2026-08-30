"""Fail-closed two-phase selector firmware programming attestation.

The module deliberately separates operator-observed power facts from command output.  Phase 1
freezes a clean source/build identity, verifies the target UID, programs with OpenOCD ``verify``,
and explicitly returns the MCU to ``reset run``.  Phase 2 can only consume that immutable phase-1
record after a separately recorded power cycle.  It reads back exactly the BIN extent, checks the
target UID again, resumes the MCU, and seals role-specific startup evidence.

All command execution is injected.  Tests therefore exercise every state transition without
opening OpenOCD or touching a target.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from smateway.bench import BenchManifest, decode_mailbox, next_sequence
from smateway.profile import load_profile

ImageRole = Literal["bench", "fast20"]

SCHEMA = 1
EVIDENCE_KIND = "selector_flash_attestation_v1"
PHASE1_KIND = "selector_flash_phase1_programming_v1"
FAILURE_KIND = "selector_flash_failure_v1"
PRE_PROGRAM_ATTESTATION_KIND = "selector_pre_program_power_swd_operator_attestation_v1"
POWER_CYCLE_ATTESTATION_KIND = "selector_power_cycle_operator_attestation_v1"
PLUTO_MUTE_EVIDENCE_KIND = "selector_flash_pluto_exact_mute_v1"
PLUTO_MUTE_CHECKPOINTS = frozenset({"phase1_pre_openocd", "phase2_pre_openocd"})
MAXIMUM_PLUTO_MUTE_AGE_SECONDS = 300.0
FLASH_BASE_ADDRESS = 0x08000000
STM32C011_FLASH_SIZE_BYTES = 16 * 1024
STM32C011_UID_ADDRESS = 0x1FFF7550
STM32C011_UID_SIZE_BYTES = 12
GPIOA_ODR_ADDRESS = 0x50000014
SELECTOR_GPIO_MASK = 0xF
STARTUP_SETTLE_SECONDS = 0.2

PHASE1_FILENAME = "phase1-programming-evidence.json"
FAILURE_FILENAME = "selector-flash-failure.json"
FINAL_EVIDENCE_FILENAME = "selector-flash-evidence.json"
FINAL_DIGEST_FILENAME = "selector-flash-evidence.sha256"
POWER_CYCLE_TEMPLATE_FILENAME = "power-cycle-attestation.template.json"
POWER_CYCLE_SEALED_FILENAME = "power-cycle-attestation.json"

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PLUTO_USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GPIO_READBACK = re.compile(rf"0x{GPIOA_ODR_ADDRESS:08x}:\s+([0-9A-Fa-f]{{8}})", re.IGNORECASE)


class SelectorFlashError(RuntimeError):
    """The workflow could not produce admissible selector-flash evidence."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result from one subprocess boundary invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandBoundary(Protocol):
    """Injectable subprocess seam used by both phases."""

    def __call__(self, argv: tuple[str, ...], *, cwd: Path) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class Phase1Result:
    """Location and digest of an immutable awaiting-power-cycle record."""

    run_directory: Path
    phase1_path: Path
    phase1_sha256: str
    power_cycle_template_path: Path


@dataclass(frozen=True, slots=True)
class SealedSelectorEvidence:
    """Downstream binding tuple for a completed selector flash."""

    path: Path
    sha256: str
    run_directory: Path


def subprocess_boundary(argv: tuple[str, ...], *, cwd: Path) -> CommandResult:
    """Execute one command without a shell and return all output."""

    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise SelectorFlashError(f"{label} contains unsupported characters")
    return value


def _validate_sha256(value: object, label: str) -> str:
    digest = str(value)
    if SHA256.fullmatch(digest) is None:
        raise SelectorFlashError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelectorFlashError(f"{label} must be an object")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SelectorFlashError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectorFlashError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SelectorFlashError(f"{label} must be a finite number")
    return result


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectorFlashError(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SelectorFlashError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise SelectorFlashError(f"{label} must include an explicit UTC offset")
    return value


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    """Hash one regular file without following a final symlink."""

    _require_regular_file(path, "hash input")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_symlink_chain(path: Path, label: str, *, allow_missing: bool = False) -> None:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise SelectorFlashError(f"{label} does not exist: {current}") from None
        if os.path.islink(current):
            raise SelectorFlashError(f"{label} must not contain symlinks: {current}")
        if not current.exists() and status:
            raise SelectorFlashError(f"{label} path is not accessible: {current}")


def _require_regular_file(path: Path, label: str) -> Path:
    _assert_no_symlink_chain(path, label)
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise SelectorFlashError(f"{label} must be a regular file: {resolved}")
    return resolved


def _normalized_path(path: Path, *, repository: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = repository / candidate
    return candidate.absolute()


def _write_new_bytes(path: Path, value: bytes) -> None:
    """Atomically publish read-only bytes and refuse any pre-existing destination."""

    _assert_no_symlink_chain(path.parent, "evidence output parent")
    if path.exists() or path.is_symlink():
        raise SelectorFlashError(f"refusing to overwrite evidence path: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o400)
        os.link(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    _write_new_bytes(path, _canonical_bytes(document))


def _read_json(path: Path, label: str, *, require_canonical: bool = False) -> dict[str, Any]:
    resolved = _require_regular_file(path, label)
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectorFlashError(f"cannot read {label}: {error}") from error
    document = _mapping(value, label)
    if require_canonical and raw != _canonical_bytes(document):
        raise SelectorFlashError(f"{label} is not canonical JSON")
    return document


def _file_identity(path: Path, label: str) -> dict[str, Any]:
    resolved = _require_regular_file(path, label)
    return {
        "path": str(resolved),
        "sha256": sha256_path(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _validate_file_identity(value: object, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    if set(item) != {"path", "sha256", "size_bytes"}:
        raise SelectorFlashError(f"{label} has unexpected fields")
    path_value = item.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectorFlashError(f"{label}.path must be absolute")
    path = _require_regular_file(Path(path_value), f"{label}.path")
    expected_hash = _validate_sha256(item.get("sha256"), f"{label}.sha256")
    expected_size = _integer(item.get("size_bytes"), f"{label}.size_bytes", minimum=1)
    if path.stat().st_size != expected_size or sha256_path(path) != expected_hash:
        raise SelectorFlashError(f"{label} bytes differ from their frozen identity")
    return {"path": str(path), "sha256": expected_hash, "size_bytes": expected_size}


def _load_attestation(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = _require_regular_file(path, label)
    document = _read_json(resolved, label)
    return document, _file_identity(resolved, f"{label} source")


def validate_pluto_mute_evidence(
    document: Mapping[str, Any],
    *,
    checkpoint: str,
    serial: str,
    uri: str,
    source: Mapping[str, Any],
    validated_at: str,
) -> dict[str, Any]:
    """Validate one fresh exact-radio mute before an OpenOCD state mutation."""

    expected_fields = {
        "schema",
        "evidence_kind",
        "checkpoint",
        "status",
        "serial",
        "uri",
        "tx_hardware_gain_db_by_channel",
        "dds_raw_readback",
        "dds_scale_readback",
        "dds_enabled_readback",
        "started_at",
        "completed_at",
        "source",
        "error",
    }
    if set(document) != expected_fields:
        raise SelectorFlashError("Pluto mute evidence fields are incomplete or unexpected")
    if checkpoint not in PLUTO_MUTE_CHECKPOINTS:
        raise SelectorFlashError("Pluto mute checkpoint is unsupported")
    if (
        document.get("schema") != SCHEMA
        or document.get("evidence_kind") != PLUTO_MUTE_EVIDENCE_KIND
        or document.get("checkpoint") != checkpoint
        or document.get("status") != "passed"
        or document.get("serial") != serial
        or document.get("uri") != uri
        or document.get("source") != dict(source)
        or document.get("error") is not None
    ):
        raise SelectorFlashError(
            "Pluto mute evidence identity, checkpoint, source, or status differs"
        )
    if not isinstance(serial, str) or not serial or PLUTO_USB_URI.fullmatch(uri) is None:
        raise SelectorFlashError("Pluto mute evidence requires an exact serial and USB URI")
    if document.get("tx_hardware_gain_db_by_channel") != [-80.0, -80.0]:
        raise SelectorFlashError("Pluto mute evidence does not prove exact TX1/TX2 -80 dB gains")
    if document.get("dds_raw_readback") != [0.0] * 8:
        raise SelectorFlashError("Pluto mute evidence does not prove all DDS raw values are zero")
    if document.get("dds_scale_readback") != [0.0] * 8:
        raise SelectorFlashError("Pluto mute evidence does not prove all DDS scales are zero")
    if document.get("dds_enabled_readback") != [False] * 8:
        raise SelectorFlashError("Pluto mute evidence does not prove all DDS enables are false")
    started_text = _timestamp(document.get("started_at"), "Pluto mute started_at")
    completed_text = _timestamp(document.get("completed_at"), "Pluto mute completed_at")
    validated_text = _timestamp(validated_at, "Pluto mute validation time")
    started = datetime.fromisoformat(started_text)
    completed = datetime.fromisoformat(completed_text)
    current = datetime.fromisoformat(validated_text)
    if completed < started or (completed - started).total_seconds() > 60.0:
        raise SelectorFlashError("Pluto mute readback duration/order is invalid")
    age = (current - completed).total_seconds()
    if age < -5.0 or age > MAXIMUM_PLUTO_MUTE_AGE_SECONDS:
        raise SelectorFlashError("Pluto mute readback is not contemporaneous with OpenOCD access")
    return {
        "schema": SCHEMA,
        "evidence_kind": PLUTO_MUTE_EVIDENCE_KIND,
        "checkpoint": checkpoint,
        "status": "passed",
        "serial": serial,
        "uri": uri,
        "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
        "dds_raw_readback": [0.0] * 8,
        "dds_scale_readback": [0.0] * 8,
        "dds_enabled_readback": [False] * 8,
        "started_at": started_text,
        "completed_at": completed_text,
        "source": dict(source),
        "error": None,
    }


def _load_immutable_pluto_mute_evidence(
    path: Path,
    *,
    checkpoint: str,
    serial: str,
    uri: str,
    source: Mapping[str, Any],
    validated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = _require_regular_file(path, "Pluto exact-mute evidence")
    if resolved.stat().st_mode & 0o200:
        raise SelectorFlashError("Pluto exact-mute evidence must be immutable (owner read-only)")
    raw = _read_json(resolved, "Pluto exact-mute evidence", require_canonical=True)
    normalized = validate_pluto_mute_evidence(
        raw,
        checkpoint=checkpoint,
        serial=serial,
        uri=uri,
        source=source,
        validated_at=validated_at,
    )
    if raw != normalized:
        raise SelectorFlashError("Pluto exact-mute evidence is not canonical normalized evidence")
    return normalized, _file_identity(resolved, "Pluto exact-mute evidence source")


def _common_power_fields(
    document: Mapping[str, Any],
    *,
    expected_kind: str,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    extra_fields: set[str],
) -> dict[str, Any]:
    common_fields = {
        "schema",
        "evidence_kind",
        "campaign_id",
        "run_id",
        "board_id",
        "image_role",
        "operator_id",
        "observed_at",
        "power_source",
        "supply_output_id",
        "positive_lead_id",
        "power_ground_id",
        "control_ground_id",
        "supply_set_voltage_v",
        "supply_current_limit_a",
        "supply_displayed_current_a",
        "j11_pin1_voltage_v",
        "j1_usb_c_disconnected",
        "pi_power_rails_disconnected",
        "j11_pin1_vtref_only",
        "common_ground_confirmed",
        "nrst_continuity_confirmed",
        "swd_wiring_confirmed",
        "openocd_stopped_confirmed",
        "target_powered",
        "unexpected_heat_observed",
    }
    if set(document) != common_fields | extra_fields:
        missing = sorted((common_fields | extra_fields) - set(document))
        extra = sorted(set(document) - (common_fields | extra_fields))
        raise SelectorFlashError(
            f"operator attestation fields differ (missing={missing}, extra={extra})"
        )
    if (
        document.get("schema") != SCHEMA
        or document.get("evidence_kind") != expected_kind
        or document.get("campaign_id") != campaign_id
        or document.get("run_id") != run_id
        or document.get("board_id") != board_id
        or document.get("image_role") != image_role
    ):
        raise SelectorFlashError("operator attestation identity differs from this flash run")
    operator_id = _validate_identifier(str(document.get("operator_id")), "operator ID")
    observed_at = _timestamp(document.get("observed_at"), "operator observed_at")
    if document.get("power_source") != "J12_bench_5V":
        raise SelectorFlashError("selector flash requires the attested J12 bench 5 V source")
    identifiers = {
        name: _validate_identifier(str(document.get(name)), name)
        for name in (
            "supply_output_id",
            "positive_lead_id",
            "power_ground_id",
            "control_ground_id",
        )
    }
    set_voltage = _number(document.get("supply_set_voltage_v"), "supply set voltage")
    current_limit = _number(document.get("supply_current_limit_a"), "supply current limit")
    displayed_current = _number(
        document.get("supply_displayed_current_a"), "supply displayed current"
    )
    j11_voltage = _number(document.get("j11_pin1_voltage_v"), "J11.1 voltage")
    if not 4.75 <= set_voltage <= 5.25:
        raise SelectorFlashError("bench supply set voltage must be within 4.75..5.25 V")
    if not 0.05 <= current_limit <= 2.0:
        raise SelectorFlashError("bench current limit must be within 0.05..2.0 A")
    if not 0.0 <= displayed_current <= current_limit:
        raise SelectorFlashError("displayed current must be nonnegative and no greater than limit")
    if not 3.26 <= j11_voltage <= 3.34:
        raise SelectorFlashError("J11.1 must be within the reviewed 3.26..3.34 V interval")
    required_true = (
        "j1_usb_c_disconnected",
        "pi_power_rails_disconnected",
        "j11_pin1_vtref_only",
        "common_ground_confirmed",
        "nrst_continuity_confirmed",
        "swd_wiring_confirmed",
        "openocd_stopped_confirmed",
        "target_powered",
    )
    if any(document.get(name) is not True for name in required_true):
        raise SelectorFlashError("operator power/SWD safety confirmations are incomplete")
    if document.get("unexpected_heat_observed") is not False:
        raise SelectorFlashError("unexpected target heat blocks selector flashing")
    return {
        "schema": SCHEMA,
        "evidence_kind": expected_kind,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "board_id": board_id,
        "image_role": image_role,
        "operator_id": operator_id,
        "observed_at": observed_at,
        "power_source": "J12_bench_5V",
        **identifiers,
        "supply_set_voltage_v": set_voltage,
        "supply_current_limit_a": current_limit,
        "supply_displayed_current_a": displayed_current,
        "j11_pin1_voltage_v": j11_voltage,
        **{name: document[name] for name in required_true},
        "unexpected_heat_observed": False,
    }


def validate_pre_program_attestation(
    document: Mapping[str, Any],
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
) -> dict[str, Any]:
    """Validate operator-observed power and SWD facts before the destructive phase."""

    return _common_power_fields(
        document,
        expected_kind=PRE_PROGRAM_ATTESTATION_KIND,
        campaign_id=campaign_id,
        run_id=run_id,
        board_id=board_id,
        image_role=image_role,
        extra_fields=set(),
    )


def pre_program_attestation_template(
    *, campaign_id: str, run_id: str, board_id: str, image_role: ImageRole
) -> dict[str, Any]:
    """Return an intentionally incomplete, run-bound phase-1 operator template."""

    return {
        "schema": SCHEMA,
        "evidence_kind": PRE_PROGRAM_ATTESTATION_KIND,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "board_id": board_id,
        "image_role": image_role,
        "operator_id": "REPLACE_OPERATOR_ID",
        "observed_at": "REPLACE_ISO8601_WITH_UTC_OFFSET",
        "power_source": "J12_bench_5V",
        "supply_output_id": "REPLACE_SUPPLY_OUTPUT_ID",
        "positive_lead_id": "REPLACE_POSITIVE_LEAD_ID",
        "power_ground_id": "REPLACE_POWER_GROUND_ID",
        "control_ground_id": "REPLACE_CONTROL_GROUND_ID",
        "supply_set_voltage_v": None,
        "supply_current_limit_a": None,
        "supply_displayed_current_a": None,
        "j11_pin1_voltage_v": None,
        "j1_usb_c_disconnected": False,
        "pi_power_rails_disconnected": False,
        "j11_pin1_vtref_only": False,
        "common_ground_confirmed": False,
        "nrst_continuity_confirmed": False,
        "swd_wiring_confirmed": False,
        "openocd_stopped_confirmed": False,
        "target_powered": False,
        "unexpected_heat_observed": None,
    }


def write_pre_program_attestation_template(
    path: Path,
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
) -> Path:
    """Create one editable, run-bound phase-1 operator template without target access."""

    campaign = _validate_identifier(campaign_id, "campaign ID")
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    if image_role not in ("bench", "fast20"):
        raise SelectorFlashError("image role must be bench or fast20")
    output = path.expanduser().absolute()
    _write_new_json(
        output,
        pre_program_attestation_template(
            campaign_id=campaign,
            run_id=run,
            board_id=board,
            image_role=image_role,
        ),
    )
    os.chmod(output, 0o600)
    return output


def validate_power_cycle_attestation(
    document: Mapping[str, Any],
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    phase1_path: Path,
    phase1_sha256: str,
) -> dict[str, Any]:
    """Validate the human-observed, post-program five-second power cycle."""

    normalized = _common_power_fields(
        document,
        expected_kind=POWER_CYCLE_ATTESTATION_KIND,
        campaign_id=campaign_id,
        run_id=run_id,
        board_id=board_id,
        image_role=image_role,
        extra_fields={
            "power_removed_duration_s",
            "no_rf_or_wiring_connection_changed",
            "phase1_path",
            "phase1_sha256",
        },
    )
    duration = _number(document.get("power_removed_duration_s"), "power-off duration")
    if duration < 5.0:
        raise SelectorFlashError("target power must have been removed for at least five seconds")
    if document.get("no_rf_or_wiring_connection_changed") is not True:
        raise SelectorFlashError("power-cycle attestation must confirm no connection changed")
    if document.get("phase1_path") != str(phase1_path):
        raise SelectorFlashError("power-cycle attestation is not bound to this phase-1 path")
    if document.get("phase1_sha256") != phase1_sha256:
        raise SelectorFlashError("power-cycle attestation is not bound to this phase-1 SHA-256")
    return {
        **normalized,
        "power_removed_duration_s": duration,
        "no_rf_or_wiring_connection_changed": True,
        "phase1_path": str(phase1_path),
        "phase1_sha256": phase1_sha256,
    }


def power_cycle_attestation_template(
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    phase1_path: Path,
    phase1_sha256: str,
) -> dict[str, Any]:
    """Return the intentionally incomplete operator template written by phase 1."""

    return {
        "schema": SCHEMA,
        "evidence_kind": POWER_CYCLE_ATTESTATION_KIND,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "board_id": board_id,
        "image_role": image_role,
        "operator_id": "REPLACE_OPERATOR_ID",
        "observed_at": "REPLACE_ISO8601_WITH_UTC_OFFSET",
        "power_source": "J12_bench_5V",
        "supply_output_id": "REPLACE_SUPPLY_OUTPUT_ID",
        "positive_lead_id": "REPLACE_POSITIVE_LEAD_ID",
        "power_ground_id": "REPLACE_POWER_GROUND_ID",
        "control_ground_id": "REPLACE_CONTROL_GROUND_ID",
        "supply_set_voltage_v": None,
        "supply_current_limit_a": None,
        "supply_displayed_current_a": None,
        "j11_pin1_voltage_v": None,
        "j1_usb_c_disconnected": False,
        "pi_power_rails_disconnected": False,
        "j11_pin1_vtref_only": False,
        "common_ground_confirmed": False,
        "nrst_continuity_confirmed": False,
        "swd_wiring_confirmed": False,
        "openocd_stopped_confirmed": False,
        "target_powered": False,
        "unexpected_heat_observed": None,
        "power_removed_duration_s": None,
        "no_rf_or_wiring_connection_changed": False,
        "phase1_path": str(phase1_path),
        "phase1_sha256": phase1_sha256,
    }


def _source_identity(repository: Path, boundary: CommandBoundary) -> dict[str, Any]:
    head_result = boundary(("git", "rev-parse", "HEAD"), cwd=repository)
    if head_result.returncode != 0:
        raise SelectorFlashError("cannot determine Smateway source commit")
    commit = head_result.stdout.strip()
    if COMMIT.fullmatch(commit) is None:
        raise SelectorFlashError("Smateway HEAD is not a full lowercase Git commit")
    status_result = boundary(
        ("git", "status", "--porcelain", "--untracked-files=normal"), cwd=repository
    )
    if status_result.returncode != 0:
        raise SelectorFlashError("cannot inspect Smateway source worktree")
    if status_result.stdout.strip():
        raise SelectorFlashError("Smateway source tree must be clean before selector flashing")
    return {"repository": str(repository), "commit": commit, "clean_worktree_verified": True}


def _role_paths(
    repository: Path,
    image_role: ImageRole,
    *,
    elf: Path,
    firmware_bin: Path,
    build_manifest: Path | None,
    profile: Path | None,
    openocd_config: Path,
) -> dict[str, Path]:
    target_directory = repository / "build/STM32C011F4P6" / image_role
    expected = {
        "elf": target_directory
        / ("pluto_bench.elf" if image_role == "bench" else "pluto_fast20.elf"),
        "firmware_bin": target_directory
        / ("pluto_bench.bin" if image_role == "bench" else "pluto_fast20.bin"),
        "openocd_config": repository / "openocd/rpi4-swd.cfg",
        "profile": repository / "profiles/fast20-v1/control_profile.json",
        "profile_header": repository / "profiles/fast20-v1/control_profile.h",
        "makefile": repository / "Makefile",
        "verifier": repository
        / (
            "scripts/verify_bench_elf.py"
            if image_role == "bench"
            else "scripts/verify_fast20_elf.py"
        ),
    }
    if _normalized_path(elf, repository=repository) != expected["elf"]:
        raise SelectorFlashError(f"{image_role} ELF path differs from the reviewed build target")
    if _normalized_path(firmware_bin, repository=repository) != expected["firmware_bin"]:
        raise SelectorFlashError(f"{image_role} BIN path differs from the reviewed build target")
    if _normalized_path(openocd_config, repository=repository) != expected["openocd_config"]:
        raise SelectorFlashError("OpenOCD config differs from reviewed rpi4-swd.cfg")
    if image_role == "bench":
        expected_manifest = target_directory / "pluto_bench.manifest.json"
        if build_manifest is None or profile is not None:
            raise SelectorFlashError("bench role requires --build-manifest and forbids --profile")
        if _normalized_path(build_manifest, repository=repository) != expected_manifest:
            raise SelectorFlashError("bench manifest path differs from the reviewed build target")
        expected["build_manifest"] = expected_manifest
        expected["bench_protocol"] = repository / "firmware/stm32c011/apps/bench/bench_protocol.h"
    else:
        if profile is None or build_manifest is not None:
            raise SelectorFlashError("fast20 role requires --profile and forbids --build-manifest")
        if _normalized_path(profile, repository=repository) != expected["profile"]:
            raise SelectorFlashError("Fast20 profile path differs from fast20-v1")
    return expected


def _validate_built_inputs(paths: Mapping[str, Path], image_role: ImageRole) -> dict[str, Any]:
    identities = {
        name: _file_identity(path, f"built input {name}") for name, path in sorted(paths.items())
    }
    firmware_size = int(identities["firmware_bin"]["size_bytes"])
    if firmware_size < 8 or firmware_size > STM32C011_FLASH_SIZE_BYTES:
        raise SelectorFlashError("firmware BIN extent is outside the STM32C011 16 KiB flash")
    profile = load_profile(paths["profile"])
    if profile.profile_id != "fast20-v1" or profile.revision != 1:
        raise SelectorFlashError("selector build is not bound to the reviewed fast20-v1 profile")
    if image_role == "bench":
        manifest = BenchManifest.load(paths["build_manifest"])
        raw_manifest = _read_json(paths["build_manifest"], "bench build manifest")
        if (
            raw_manifest.get("schema") != 1
            or manifest.elf_sha256 != identities["elf"]["sha256"]
            or raw_manifest.get("protocol_sha256") != identities["bench_protocol"]["sha256"]
        ):
            raise SelectorFlashError("bench manifest does not bind the exact ELF and protocol")
    return {
        "files": identities,
        "firmware_bin_extent": {
            "flash_base_address": FLASH_BASE_ADDRESS,
            "size_bytes": firmware_size,
            "sha256": identities["firmware_bin"]["sha256"],
        },
        "control_profile": {
            "id": profile.profile_id,
            "revision": profile.revision,
            "contract_sha256": profile.contract_sha256,
            "all_off_code": profile.all_off_code,
        },
    }


def _command_log(
    boundary: CommandBoundary,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    log_path: Path,
    now: Callable[[], str],
) -> tuple[CommandResult, dict[str, Any]]:
    started_at = now()
    exception: dict[str, str] | None = None
    try:
        result = boundary(argv, cwd=cwd)
        if not isinstance(result, CommandResult):
            raise TypeError("command boundary did not return CommandResult")
    except BaseException as error:
        exception = {"type": type(error).__name__, "message": str(error)}
        result = CommandResult(255, "", str(error))
    document = {
        "schema": SCHEMA,
        "evidence_kind": "selector_flash_command_log_v1",
        "argv": list(argv),
        "cwd": str(cwd),
        "started_at": started_at,
        "completed_at": now(),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "boundary_exception": exception,
    }
    _write_new_json(log_path, document)
    return result, _file_identity(log_path, "command log")


def _run_checked(
    boundary: CommandBoundary,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    log_path: Path,
    label: str,
    now: Callable[[], str],
) -> dict[str, Any]:
    result, identity = _command_log(boundary, argv, cwd=cwd, log_path=log_path, now=now)
    if result.returncode != 0:
        raise SelectorFlashError(f"{label} failed with exit status {result.returncode}")
    return identity


def _tcl_path(path: Path) -> str:
    value = str(path)
    if any(character in value for character in "{}\r\n"):
        raise SelectorFlashError("OpenOCD evidence paths must not contain Tcl metacharacters")
    return "{" + value + "}"


def _openocd_argv(config: Path, command: str) -> tuple[str, ...]:
    return ("openocd", "-f", str(config), "-c", command)


def _reset_run(
    boundary: CommandBoundary,
    *,
    repository: Path,
    config: Path,
    log_path: Path,
    now: Callable[[], str],
) -> tuple[bool, dict[str, Any]]:
    result, identity = _command_log(
        boundary,
        _openocd_argv(config, "init; reset run; shutdown"),
        cwd=repository,
        log_path=log_path,
        now=now,
    )
    return result.returncode == 0, identity


def _halt_target(
    boundary: CommandBoundary,
    *,
    repository: Path,
    config: Path,
    log_path: Path,
    now: Callable[[], str],
) -> tuple[bool, dict[str, Any]]:
    result, identity = _command_log(
        boundary,
        _openocd_argv(config, "init; halt; shutdown"),
        cwd=repository,
        log_path=log_path,
        now=now,
    )
    return result.returncode == 0, identity


def _target_uid_from_file(path: Path, board_id: str) -> str:
    resolved = _require_regular_file(path, "target UID readback")
    value = resolved.read_bytes()
    if len(value) != STM32C011_UID_SIZE_BYTES:
        raise SelectorFlashError("target UID readback must contain exactly 12 bytes")
    expected_prefix = "stm32c011-"
    if not board_id.startswith(expected_prefix):
        raise SelectorFlashError("board ID must be derived from an STM32C011 UID")
    expected_uid = board_id.removeprefix(expected_prefix)
    if len(expected_uid) != 24 or any(
        character not in "0123456789abcdef" for character in expected_uid
    ):
        raise SelectorFlashError("board ID contains an invalid STM32C011 UID suffix")
    observed_uid = value.hex()
    if observed_uid != expected_uid:
        raise SelectorFlashError("live target UID differs from the selected board ID")
    return observed_uid


def _partial_identities(run_directory: Path) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for path in sorted(run_directory.iterdir()):
        if path.name == FAILURE_FILENAME or not path.is_file() or path.is_symlink():
            continue
        try:
            identities.append(_file_identity(path, "partial evidence"))
        except SelectorFlashError:
            continue
    return identities


def _write_failure(
    run_directory: Path,
    *,
    phase: str,
    error: BaseException,
    target_state: str,
    reset_run_attempted: bool,
    reset_run_succeeded: bool,
    now: Callable[[], str],
) -> None:
    path = run_directory / FAILURE_FILENAME
    if path.exists() or path.is_symlink():
        return
    _write_new_json(
        path,
        {
            "schema": SCHEMA,
            "evidence_kind": FAILURE_KIND,
            "status": "failed",
            "phase": phase,
            "failed_at": now(),
            "error": {"type": type(error).__name__, "message": str(error)},
            "target_state_after_failure": target_state,
            "reset_run_attempted": reset_run_attempted,
            "reset_run_succeeded": reset_run_succeeded,
            "downstream_use_permitted": False,
            "partial_evidence": _partial_identities(run_directory),
        },
    )


def _create_run_directory(evidence_root: Path, run_id: str) -> Path:
    root = evidence_root.expanduser().absolute()
    _assert_no_symlink_chain(root, "evidence root", allow_missing=True)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(root, "evidence root")
    run_directory = root / run_id
    if run_directory.exists() or run_directory.is_symlink():
        raise SelectorFlashError(
            f"selector flash evidence directory already exists: {run_directory}"
        )
    run_directory.mkdir(mode=0o700)
    _assert_no_symlink_chain(run_directory, "selector flash run directory")
    return run_directory


def _existing_run_directory(evidence_root: Path, run_id: str) -> Path:
    run_directory = evidence_root.expanduser().absolute() / run_id
    _assert_no_symlink_chain(run_directory, "selector flash run directory")
    if not run_directory.is_dir():
        raise SelectorFlashError("selector flash run directory is not a directory")
    return run_directory


def prepare_and_program(
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    elf: Path,
    firmware_bin: Path,
    build_manifest: Path | None,
    profile: Path | None,
    openocd_config: Path,
    evidence_root: Path,
    pre_program_attestation: Path,
    pluto_serial: str,
    pluto_uri: str,
    pluto_mute_evidence: Path,
    repository: Path,
    python_executable: Path,
    require_current_image_match: bool = False,
    boundary: CommandBoundary = subprocess_boundary,
    now: Callable[[], str] = _now,
) -> Phase1Result:
    """Build, verify, UID-check, program, resume, and freeze phase-1 evidence."""

    campaign = _validate_identifier(campaign_id, "campaign ID")
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    if image_role not in {"bench", "fast20"}:
        raise SelectorFlashError("image role must be bench or fast20")
    repository = _require_regular_file(repository / "Makefile", "repository Makefile").parent
    role_paths = _role_paths(
        repository,
        image_role,
        elf=elf,
        firmware_bin=firmware_bin,
        build_manifest=build_manifest,
        profile=profile,
        openocd_config=openocd_config,
    )
    attestation_raw, attestation_source = _load_attestation(
        pre_program_attestation, "pre-program operator attestation"
    )
    attestation = validate_pre_program_attestation(
        attestation_raw,
        campaign_id=campaign,
        run_id=run,
        board_id=board,
        image_role=image_role,
    )
    source = _source_identity(repository, boundary)
    mute, mute_source = _load_immutable_pluto_mute_evidence(
        pluto_mute_evidence,
        checkpoint="phase1_pre_openocd",
        serial=pluto_serial,
        uri=pluto_uri,
        source=source,
        validated_at=now(),
    )
    run_directory = _create_run_directory(evidence_root, run)
    run_created_path = run_directory / "run-created.json"
    _write_new_json(
        run_created_path,
        {
            "schema": SCHEMA,
            "evidence_kind": "selector_flash_run_created_v1",
            "status": "created",
            "campaign_id": campaign,
            "run_id": run,
            "board_id": board,
            "image_role": image_role,
            "source": source,
            "pre_program_attestation_source": attestation_source,
            "pluto_pre_openocd_mute_source": mute_source,
            "created_at": now(),
            "run_id_burned_on_any_later_failure": True,
        },
    )
    snapshot_path = run_directory / "pre-program-attestation.json"
    _write_new_json(snapshot_path, attestation)
    snapshot_identity = _file_identity(snapshot_path, "pre-program attestation snapshot")
    mute_snapshot_path = run_directory / "phase1-pluto-exact-mute.json"
    _write_new_json(mute_snapshot_path, mute)
    mute_snapshot_identity = _file_identity(mute_snapshot_path, "phase1 Pluto mute snapshot")
    reset_attempted = False
    reset_succeeded = False
    target_state = "not_touched"
    logs: dict[str, Any] = {}
    try:
        logs["build"] = _run_checked(
            boundary,
            ("make", image_role),
            cwd=repository,
            log_path=run_directory / "phase1-build.json",
            label=f"make {image_role}",
            now=now,
        )
        logs["verify"] = _run_checked(
            boundary,
            (str(python_executable), str(role_paths["verifier"]), str(role_paths["elf"])),
            cwd=repository,
            log_path=run_directory / "phase1-verify.json",
            label=f"{image_role} ELF verification",
            now=now,
        )
        built_inputs = _validate_built_inputs(role_paths, image_role)
        derived_bin = run_directory / "elf-derived.bin"
        logs["elf_to_bin"] = _run_checked(
            boundary,
            (
                "arm-none-eabi-objcopy",
                "-O",
                "binary",
                str(role_paths["elf"]),
                str(derived_bin),
            ),
            cwd=repository,
            log_path=run_directory / "phase1-elf-to-bin.json",
            label="ELF-to-BIN derivation",
            now=now,
        )
        _require_regular_file(derived_bin, "ELF-derived BIN")
        if derived_bin.read_bytes() != role_paths["firmware_bin"].read_bytes():
            raise SelectorFlashError("firmware BIN differs from bytes derived from the ELF")
        derived_identity = _file_identity(derived_bin, "ELF-derived BIN")

        config = role_paths["openocd_config"]
        # Recheck freshness after the build and immediately before the first
        # reset/halt.  A slow/stalled build therefore cannot turn stale radio
        # state into permission for OpenOCD access.
        validate_pluto_mute_evidence(
            mute,
            checkpoint="phase1_pre_openocd",
            serial=pluto_serial,
            uri=pluto_uri,
            source=source,
            validated_at=now(),
        )
        uid_path = run_directory / "phase1-target-uid.bin"
        uid_command = (
            "init; reset halt; "
            f"dump_image {_tcl_path(uid_path)} 0x{STM32C011_UID_ADDRESS:x} "
            f"0x{STM32C011_UID_SIZE_BYTES:x}; shutdown"
        )
        uid_result, logs["target_uid"] = _command_log(
            boundary,
            _openocd_argv(config, uid_command),
            cwd=repository,
            log_path=run_directory / "phase1-target-uid.json",
            now=now,
        )
        target_state = "halted_or_unknown"
        reset_attempted = True
        reset_succeeded, logs["reset_after_uid"] = _reset_run(
            boundary,
            repository=repository,
            config=config,
            log_path=run_directory / "phase1-reset-run-after-uid.json",
            now=now,
        )
        target_state = "running" if reset_succeeded else "halted_or_unknown"
        if uid_result.returncode != 0:
            raise SelectorFlashError("pre-program target UID readback failed")
        if not reset_succeeded:
            halt_succeeded, logs["halt_after_uid_resume_failure"] = _halt_target(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase1-halt-after-uid-resume-failure.json",
                now=now,
            )
            target_state = "halted" if halt_succeeded else "halted_or_unknown"
            raise SelectorFlashError("target could not be resumed after UID readback")
        target_uid = _target_uid_from_file(uid_path, board)
        uid_identity = _file_identity(uid_path, "phase1 target UID")

        pre_program_image_identity: dict[str, Any] | None = None
        if require_current_image_match:
            byte_count = int(built_inputs["firmware_bin_extent"]["size_bytes"])
            pre_program_image_path = run_directory / "phase1-pre-program-flash.bin"
            pre_program_command = (
                "init; reset halt; "
                f"dump_image {_tcl_path(pre_program_image_path)} 0x{FLASH_BASE_ADDRESS:x} "
                f"0x{byte_count:x}; shutdown"
            )
            pre_program_result, logs["pre_program_image_readback"] = _command_log(
                boundary,
                _openocd_argv(config, pre_program_command),
                cwd=repository,
                log_path=run_directory / "phase1-pre-program-image-readback.json",
                now=now,
            )
            target_state = "halted_or_unknown"
            reset_attempted = True
            reset_succeeded, logs["reset_after_pre_program_image_readback"] = _reset_run(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase1-reset-after-pre-program-image-readback.json",
                now=now,
            )
            target_state = "running" if reset_succeeded else "halted_or_unknown"
            if pre_program_result.returncode != 0:
                raise SelectorFlashError("pre-program current-image readback failed")
            if not reset_succeeded:
                raise SelectorFlashError(
                    "target could not be resumed after pre-program image readback"
                )
            pre_program_image_identity = _file_identity(
                pre_program_image_path, "pre-program current image readback"
            )
            if pre_program_image_path.read_bytes() != role_paths["firmware_bin"].read_bytes():
                raise SelectorFlashError(
                    "current target image differs from the reviewed BIN; programming refused"
                )

        program_command = (
            "init; reset halt; "
            f"program {_tcl_path(role_paths['firmware_bin'])} 0x{FLASH_BASE_ADDRESS:x} verify; "
            "shutdown"
        )
        program_result, logs["program_verify"] = _command_log(
            boundary,
            _openocd_argv(config, program_command),
            cwd=repository,
            log_path=run_directory / "phase1-program-verify.json",
            now=now,
        )
        target_state = "halted_or_unknown"
        reset_succeeded = False
        if program_result.returncode != 0:
            halt_succeeded, logs["halt_after_program_failure"] = _halt_target(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase1-halt-after-program-failure.json",
                now=now,
            )
            target_state = "halted" if halt_succeeded else "halted_or_unknown"
            raise SelectorFlashError("OpenOCD program-with-verify failed")
        reset_attempted = True
        reset_succeeded, logs["final_reset_run"] = _reset_run(
            boundary,
            repository=repository,
            config=config,
            log_path=run_directory / "phase1-final-reset-run.json",
            now=now,
        )
        target_state = "running" if reset_succeeded else "halted_or_unknown"
        if not reset_succeeded:
            halt_succeeded, logs["halt_after_final_reset_failure"] = _halt_target(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase1-halt-after-final-reset-failure.json",
                now=now,
            )
            target_state = "halted" if halt_succeeded else "halted_or_unknown"
            raise SelectorFlashError("programmed target could not be returned to reset run")

        template_path = run_directory / POWER_CYCLE_TEMPLATE_FILENAME
        phase1_path = run_directory / PHASE1_FILENAME
        phase1_document = {
            "schema": SCHEMA,
            "evidence_kind": PHASE1_KIND,
            "status": "awaiting_power_cycle",
            "campaign_id": campaign,
            "run_id": run,
            "board_id": board,
            "image_role": image_role,
            "source": source,
            "run_created": _file_identity(run_created_path, "run-created evidence"),
            "pre_program_operator_attestation": {
                "source": attestation_source,
                "snapshot": snapshot_identity,
                "validated": True,
            },
            "pluto_pre_openocd_mute": {
                "source": mute_source,
                "snapshot": mute_snapshot_identity,
                "serial": pluto_serial,
                "uri": pluto_uri,
                "checkpoint": "phase1_pre_openocd",
                "validated_immediately_before_openocd": True,
            },
            "build": {
                "target": image_role,
                "inputs": built_inputs,
                "elf_derived_bin": derived_identity,
                "elf_derived_bin_exact_match": True,
            },
            "target_identity": {
                "uid": target_uid,
                "uid_readback": uid_identity,
                "matches_board_id": True,
            },
            "programming": {
                "flash_base_address": FLASH_BASE_ADDRESS,
                "byte_count": built_inputs["firmware_bin_extent"]["size_bytes"],
                "pre_program_current_image_match_required": require_current_image_match,
                "pre_program_current_image_readback": pre_program_image_identity,
                "pre_program_current_image_exact_match": (
                    True if require_current_image_match else None
                ),
                "program_with_verify_succeeded": True,
                "explicit_final_reset_run_succeeded": True,
                "target_state": "running",
            },
            "logs": logs,
            "power_cycle_template_expected_path": str(template_path),
            "completed_at": now(),
            "downstream_use_permitted": False,
        }
        _write_new_json(phase1_path, phase1_document)
        phase1_sha256 = sha256_path(phase1_path)
        _write_new_json(
            template_path,
            power_cycle_attestation_template(
                campaign_id=campaign,
                run_id=run,
                board_id=board,
                image_role=image_role,
                phase1_path=phase1_path,
                phase1_sha256=phase1_sha256,
            ),
        )
        # This is explicitly an operator-editable draft, never phase-2
        # authority.  A separate hardware-inert seal step validates it and
        # publishes the canonical read-only file consumed by phase 2.
        os.chmod(template_path, 0o600)
        return Phase1Result(
            run_directory=run_directory,
            phase1_path=phase1_path,
            phase1_sha256=phase1_sha256,
            power_cycle_template_path=template_path,
        )
    except BaseException as error:
        _write_failure(
            run_directory,
            phase="prepare_and_program",
            error=error,
            target_state=target_state,
            reset_run_attempted=reset_attempted,
            reset_run_succeeded=reset_succeeded,
            now=now,
        )
        if isinstance(error, SelectorFlashError):
            raise
        raise SelectorFlashError(str(error)) from error


def _validate_phase1(
    document: Mapping[str, Any],
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
) -> dict[str, Any]:
    if (
        document.get("schema") != SCHEMA
        or document.get("evidence_kind") != PHASE1_KIND
        or document.get("status") != "awaiting_power_cycle"
        or document.get("campaign_id") != campaign_id
        or document.get("run_id") != run_id
        or document.get("board_id") != board_id
        or document.get("image_role") != image_role
        or document.get("downstream_use_permitted") is not False
    ):
        raise SelectorFlashError("phase-1 evidence identity or status is invalid")
    source = _mapping(document.get("source"), "phase1.source")
    if (
        COMMIT.fullmatch(str(source.get("commit"))) is None
        or source.get("clean_worktree_verified") is not True
    ):
        raise SelectorFlashError("phase-1 source identity is invalid")
    _validate_file_identity(document.get("run_created"), "phase1 run-created evidence")
    template_path = document.get("power_cycle_template_expected_path")
    if not isinstance(template_path, str) or not Path(template_path).is_absolute():
        raise SelectorFlashError("phase-1 power-cycle template path is invalid")
    pre_program = _mapping(
        document.get("pre_program_operator_attestation"),
        "phase1.pre_program_operator_attestation",
    )
    if pre_program.get("validated") is not True:
        raise SelectorFlashError("phase-1 pre-program attestation was not validated")
    _validate_file_identity(pre_program.get("source"), "phase1 pre-program source")
    _validate_file_identity(pre_program.get("snapshot"), "phase1 pre-program snapshot")
    mute_record = _mapping(
        document.get("pluto_pre_openocd_mute"),
        "phase1.pluto_pre_openocd_mute",
    )
    if (
        set(mute_record)
        != {
            "source",
            "snapshot",
            "serial",
            "uri",
            "checkpoint",
            "validated_immediately_before_openocd",
        }
        or mute_record.get("validated_immediately_before_openocd") is not True
    ):
        raise SelectorFlashError("phase-1 Pluto mute binding is incomplete")
    mute_source_identity = _validate_file_identity(
        mute_record.get("source"), "phase1 Pluto mute source"
    )
    mute_snapshot_identity = _validate_file_identity(
        mute_record.get("snapshot"), "phase1 Pluto mute snapshot"
    )
    mute_source_document = _read_json(
        Path(mute_source_identity["path"]),
        "phase1 Pluto mute source",
        require_canonical=True,
    )
    mute_snapshot_document = _read_json(
        Path(mute_snapshot_identity["path"]),
        "phase1 Pluto mute snapshot",
        require_canonical=True,
    )
    if mute_source_document != mute_snapshot_document:
        raise SelectorFlashError("phase-1 Pluto mute source/snapshot bytes differ")
    validate_pluto_mute_evidence(
        mute_snapshot_document,
        checkpoint="phase1_pre_openocd",
        serial=str(mute_record.get("serial")),
        uri=str(mute_record.get("uri")),
        source=source,
        validated_at=str(mute_snapshot_document.get("completed_at")),
    )
    build = _mapping(document.get("build"), "phase1.build")
    inputs = _mapping(build.get("inputs"), "phase1.build.inputs")
    files = _mapping(inputs.get("files"), "phase1.build.inputs.files")
    normalized_files = {
        name: _validate_file_identity(item, f"phase1 input {name}")
        for name, item in sorted(files.items())
    }
    extent = _mapping(inputs.get("firmware_bin_extent"), "phase1 firmware extent")
    if (
        extent.get("flash_base_address") != FLASH_BASE_ADDRESS
        or extent.get("size_bytes") != normalized_files["firmware_bin"]["size_bytes"]
        or extent.get("sha256") != normalized_files["firmware_bin"]["sha256"]
    ):
        raise SelectorFlashError("phase-1 firmware extent is not bound to its BIN")
    if build.get("elf_derived_bin_exact_match") is not True:
        raise SelectorFlashError("phase-1 ELF-derived BIN check did not pass")
    _validate_file_identity(build.get("elf_derived_bin"), "phase1 ELF-derived BIN")
    programming = _mapping(document.get("programming"), "phase1.programming")
    if (
        programming.get("program_with_verify_succeeded") is not True
        or programming.get("explicit_final_reset_run_succeeded") is not True
        or programming.get("target_state") != "running"
    ):
        raise SelectorFlashError("phase-1 programming did not finish in reset run")
    match_required = programming.get("pre_program_current_image_match_required")
    if not isinstance(match_required, bool):
        raise SelectorFlashError("phase-1 current-image match policy is missing")
    if match_required:
        current_image = _validate_file_identity(
            programming.get("pre_program_current_image_readback"),
            "phase1 pre-program current image",
        )
        if (
            programming.get("pre_program_current_image_exact_match") is not True
            or current_image["sha256"] != normalized_files["firmware_bin"]["sha256"]
            or current_image["size_bytes"] != normalized_files["firmware_bin"]["size_bytes"]
        ):
            raise SelectorFlashError("phase-1 pre-program current image did not match the BIN")
    elif (
        programming.get("pre_program_current_image_readback") is not None
        or programming.get("pre_program_current_image_exact_match") is not None
    ):
        raise SelectorFlashError("phase-1 current-image evidence contradicts its policy")
    target = _mapping(document.get("target_identity"), "phase1.target_identity")
    if target.get("uid") != board_id.removeprefix("stm32c011-"):
        raise SelectorFlashError("phase-1 target UID differs from board ID")
    _validate_file_identity(target.get("uid_readback"), "phase1 target UID readback")
    logs = _mapping(document.get("logs"), "phase1.logs")
    for name, item in logs.items():
        _validate_file_identity(item, f"phase1 log {name}")
    return {**document, "build": {**build, "inputs": {**inputs, "files": normalized_files}}}


def _validate_exact_current_inputs(
    phase1: Mapping[str, Any],
    current_source: Mapping[str, Any],
    role_paths: Mapping[str, Path],
    image_role: ImageRole,
) -> dict[str, Any]:
    source = _mapping(phase1.get("source"), "phase1.source")
    if source.get("commit") != current_source.get("commit"):
        raise SelectorFlashError("Smateway source commit differs from frozen phase 1")
    current_inputs = _validate_built_inputs(role_paths, image_role)
    phase1_build = _mapping(phase1.get("build"), "phase1.build")
    frozen_inputs = _mapping(phase1_build.get("inputs"), "phase1.build.inputs")
    if current_inputs != frozen_inputs:
        raise SelectorFlashError("phase-2 build inputs differ byte-for-byte from phase 1")
    return current_inputs


def seal_power_cycle_attestation(
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    evidence_root: Path,
    power_cycle_draft: Path,
) -> Path:
    """Validate an editable power-cycle draft and publish immutable phase-2 authority.

    This function is filesystem-only: it does not inspect the current source
    tree, invoke OpenOCD, access Pluto, or mutate the target.
    """

    campaign = _validate_identifier(campaign_id, "campaign ID")
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    if image_role not in {"bench", "fast20"}:
        raise SelectorFlashError("image role must be bench or fast20")
    run_directory = _existing_run_directory(evidence_root, run)
    if (run_directory / FAILURE_FILENAME).exists():
        raise SelectorFlashError("failed selector-flash evidence directory is permanently burned")
    phase1_path = run_directory / PHASE1_FILENAME
    phase1 = _validate_phase1(
        _read_json(phase1_path, "phase-1 evidence", require_canonical=True),
        campaign_id=campaign,
        run_id=run,
        board_id=board,
        image_role=image_role,
    )
    del phase1
    expected_draft = run_directory / POWER_CYCLE_TEMPLATE_FILENAME
    draft = _require_regular_file(power_cycle_draft, "power-cycle operator draft")
    if draft != expected_draft:
        raise SelectorFlashError("power-cycle draft must be the exact phase-1 generated template")
    normalized = validate_power_cycle_attestation(
        _read_json(draft, "power-cycle operator draft"),
        campaign_id=campaign,
        run_id=run,
        board_id=board,
        image_role=image_role,
        phase1_path=phase1_path,
        phase1_sha256=sha256_path(phase1_path),
    )
    output = run_directory / POWER_CYCLE_SEALED_FILENAME
    _write_new_json(output, normalized)
    return output


def _bench_startup_evidence(
    *,
    boundary: CommandBoundary,
    repository: Path,
    run_directory: Path,
    config: Path,
    manifest_path: Path,
    all_off_code: int,
    now: Callable[[], str],
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = BenchManifest.load(manifest_path)
    initial_mailbox_path = run_directory / "phase2-bench-mailbox-before-command.bin"
    initial_command = (
        "init; halt; "
        f"dump_image {_tcl_path(initial_mailbox_path)} 0x{manifest.address:x} "
        f"0x{manifest.size:x}; resume; shutdown"
    )
    initial_result, initial_log = _command_log(
        boundary,
        _openocd_argv(config, initial_command),
        cwd=repository,
        log_path=run_directory / "phase2-bench-mailbox-before-command.json",
        now=now,
    )
    if initial_result.returncode != 0:
        raise SelectorFlashError("initial bench mailbox startup probe failed")
    initial_status = decode_mailbox(
        _require_regular_file(initial_mailbox_path, "initial bench mailbox readback").read_bytes(),
        manifest,
    )
    sequence = next_sequence(initial_status.acknowledged_sequence)
    command_code_address = manifest.field_address("command_code")
    command_lease_address = manifest.field_address("command_lease_ms")
    command_sequence_address = manifest.field_address("command_sequence")
    all_off_command = (
        "init; "
        f"mww 0x{command_code_address:08x} 0x{all_off_code:08x}; "
        f"mww 0x{command_lease_address:08x} 0x00000000; "
        f"mww 0x{command_sequence_address:08x} 0x{sequence:08x}; shutdown"
    )
    command_result, command_log = _command_log(
        boundary,
        _openocd_argv(config, all_off_command),
        cwd=repository,
        log_path=run_directory / "phase2-bench-command-all-off.json",
        now=now,
    )
    if command_result.returncode != 0:
        raise SelectorFlashError("bench lease-free ALL_OFF mailbox command failed")
    sleep(0.05)

    mailbox_path = run_directory / "phase2-bench-mailbox.bin"
    gpio_path = run_directory / "phase2-gpioa-odr.bin"
    probe_command = (
        "init; halt; "
        f"dump_image {_tcl_path(mailbox_path)} 0x{manifest.address:x} 0x{manifest.size:x}; "
        f"dump_image {_tcl_path(gpio_path)} 0x{GPIOA_ODR_ADDRESS:x} 0x4; "
        f"mdw 0x{GPIOA_ODR_ADDRESS:x} 1; resume; shutdown"
    )
    result, probe_log = _command_log(
        boundary,
        _openocd_argv(config, probe_command),
        cwd=repository,
        log_path=run_directory / "phase2-bench-mailbox-gpio.json",
        now=now,
    )
    if result.returncode != 0:
        raise SelectorFlashError("final bench mailbox/GPIO startup probe failed")
    status = decode_mailbox(
        _require_regular_file(mailbox_path, "bench mailbox readback").read_bytes(), manifest
    )
    mailbox_passed = (
        status.applied_code == all_off_code
        and status.command_code == all_off_code
        and status.command_lease_ms == 0
        and status.command_sequence == sequence
        and status.acknowledged_sequence == sequence
        and status.remaining_lease_ms == 0
        and status.command_valid
        and not status.lease_active
        and not status.guard_active
        and not status.invalid_command
    )
    output = result.stdout + result.stderr
    matches = GPIO_READBACK.findall(output)
    if len(matches) != 1:
        raise SelectorFlashError("bench GPIO probe did not report one exact GPIOA ODR value")
    raw_gpio = int(matches[0], 16)
    masked_gpio = raw_gpio & SELECTOR_GPIO_MASK
    raw_gpio_bytes = _require_regular_file(gpio_path, "GPIOA ODR raw readback").read_bytes()
    if len(raw_gpio_bytes) != 4 or int.from_bytes(raw_gpio_bytes, "little") != raw_gpio:
        raise SelectorFlashError("raw GPIOA ODR dump differs from textual OpenOCD readback")
    gpio_passed = masked_gpio == all_off_code
    if not mailbox_passed or not gpio_passed:
        raise SelectorFlashError("bench image did not start in mailbox/GPIO ALL_OFF")
    evidence = {
        "evidence_kind": "bench_mailbox_gpio_all_off_startup_v1",
        "explicit_lease_free_all_off_command": {
            "sequence": sequence,
            "code": all_off_code,
            "lease_ms": 0,
            "passed": True,
        },
        "initial_mailbox": initial_status.as_dict(),
        "initial_mailbox_readback": _file_identity(
            initial_mailbox_path, "initial bench mailbox readback"
        ),
        "mailbox": status.as_dict(),
        "mailbox_readback": _file_identity(mailbox_path, "bench mailbox readback"),
        "gpio_output_latch": {
            "register": "GPIOA_ODR",
            "address": GPIOA_ODR_ADDRESS,
            "selector_mask": SELECTOR_GPIO_MASK,
            "raw_value": raw_gpio,
            "masked_selector_code": masked_gpio,
            "expected_all_off_code": all_off_code,
            "raw_readback": _file_identity(gpio_path, "GPIOA ODR raw readback"),
        },
        "mailbox_all_off_passed": True,
        "gpio_latch_all_off_passed": True,
        "physical_rf_state_proven": False,
    }
    return evidence, {
        "initial_mailbox_probe": initial_log,
        "all_off_command": command_log,
        "final_mailbox_gpio_probe": probe_log,
    }


def verify_after_power_cycle(
    *,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
    elf: Path,
    firmware_bin: Path,
    build_manifest: Path | None,
    profile: Path | None,
    openocd_config: Path,
    evidence_root: Path,
    power_cycle_attestation: Path,
    pluto_serial: str,
    pluto_uri: str,
    pluto_mute_evidence: Path,
    repository: Path,
    boundary: CommandBoundary = subprocess_boundary,
    now: Callable[[], str] = _now,
    sleep: Callable[[float], None] = time.sleep,
) -> SealedSelectorEvidence:
    """Verify the post-power-cycle target and seal canonical selector-flash evidence."""

    campaign = _validate_identifier(campaign_id, "campaign ID")
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    if image_role not in {"bench", "fast20"}:
        raise SelectorFlashError("image role must be bench or fast20")
    repository = _require_regular_file(repository / "Makefile", "repository Makefile").parent
    role_paths = _role_paths(
        repository,
        image_role,
        elf=elf,
        firmware_bin=firmware_bin,
        build_manifest=build_manifest,
        profile=profile,
        openocd_config=openocd_config,
    )
    run_directory = _existing_run_directory(evidence_root, run)
    if (run_directory / FAILURE_FILENAME).exists():
        raise SelectorFlashError("failed selector-flash evidence directory is permanently burned")
    forbidden_outputs = {
        FINAL_EVIDENCE_FILENAME,
        FINAL_DIGEST_FILENAME,
    }
    partial_phase2 = [
        path
        for path in run_directory.iterdir()
        if path.name in forbidden_outputs or path.name.startswith("phase2-")
    ]
    if partial_phase2:
        raise SelectorFlashError("phase-2 output already exists; refusing partial-log overwrite")
    phase1_path = run_directory / PHASE1_FILENAME
    phase1_raw = _read_json(phase1_path, "phase-1 evidence", require_canonical=True)
    phase1 = _validate_phase1(
        phase1_raw,
        campaign_id=campaign,
        run_id=run,
        board_id=board,
        image_role=image_role,
    )
    current_source = _source_identity(repository, boundary)
    current_inputs = _validate_exact_current_inputs(phase1, current_source, role_paths, image_role)
    expected_power_path = run_directory / POWER_CYCLE_SEALED_FILENAME
    supplied_power_path = _require_regular_file(
        power_cycle_attestation, "sealed power-cycle operator attestation"
    )
    if supplied_power_path != expected_power_path:
        raise SelectorFlashError(
            "phase 2 requires the exact run-bound sealed power-cycle attestation"
        )
    if supplied_power_path.stat().st_mode & 0o222:
        raise SelectorFlashError("sealed power-cycle attestation must be read-only")
    power_raw = _read_json(
        supplied_power_path,
        "sealed power-cycle operator attestation",
        require_canonical=True,
    )
    power_source = _file_identity(supplied_power_path, "sealed power-cycle attestation")
    validate_power_cycle_attestation(
        power_raw,
        campaign_id=campaign,
        run_id=run,
        board_id=board,
        image_role=image_role,
        phase1_path=phase1_path,
        phase1_sha256=sha256_path(phase1_path),
    )
    phase2_mute, phase2_mute_source = _load_immutable_pluto_mute_evidence(
        pluto_mute_evidence,
        checkpoint="phase2_pre_openocd",
        serial=pluto_serial,
        uri=pluto_uri,
        source=current_source,
        validated_at=now(),
    )
    phase1_mute = _mapping(phase1.get("pluto_pre_openocd_mute"), "phase1 Pluto mute binding")
    if phase1_mute.get("source") == phase2_mute_source:
        raise SelectorFlashError("phase 2 requires fresh, distinct Pluto mute evidence")
    power_snapshot = power_source
    phase2_mute_snapshot_path = run_directory / "phase2-pluto-exact-mute.json"
    _write_new_json(phase2_mute_snapshot_path, phase2_mute)
    phase2_mute_snapshot = _file_identity(phase2_mute_snapshot_path, "phase2 Pluto mute snapshot")

    reset_attempted = False
    reset_succeeded = False
    target_state = "not_touched_by_phase2"
    verified_image = False
    logs: dict[str, Any] = {}
    try:
        validate_pluto_mute_evidence(
            phase2_mute,
            checkpoint="phase2_pre_openocd",
            serial=pluto_serial,
            uri=pluto_uri,
            source=current_source,
            validated_at=now(),
        )
        phase2_started_path = run_directory / "phase2-started.json"
        _write_new_json(
            phase2_started_path,
            {
                "schema": SCHEMA,
                "evidence_kind": "selector_flash_phase2_started_v1",
                "status": "started",
                "campaign_id": campaign,
                "run_id": run,
                "board_id": board,
                "image_role": image_role,
                "phase1": _file_identity(phase1_path, "phase-1 evidence"),
                "power_cycle_attestation": power_snapshot,
                "pluto_pre_openocd_mute_source": phase2_mute_source,
                "pluto_pre_openocd_mute_snapshot": phase2_mute_snapshot,
                "started_at": now(),
                "run_id_burned_if_final_evidence_is_not_sealed": True,
            },
        )
        readback_path = run_directory / "phase2-target-flash.bin"
        uid_path = run_directory / "phase2-target-uid.bin"
        extent = _mapping(current_inputs["firmware_bin_extent"], "firmware BIN extent")
        byte_count = int(extent["size_bytes"])
        config = role_paths["openocd_config"]
        readback_command = (
            "init; reset halt; "
            f"dump_image {_tcl_path(readback_path)} 0x{FLASH_BASE_ADDRESS:x} 0x{byte_count:x}; "
            f"dump_image {_tcl_path(uid_path)} 0x{STM32C011_UID_ADDRESS:x} "
            f"0x{STM32C011_UID_SIZE_BYTES:x}; shutdown"
        )
        readback_result, logs["bin_extent_and_uid_readback"] = _command_log(
            boundary,
            _openocd_argv(config, readback_command),
            cwd=repository,
            log_path=run_directory / "phase2-readback-uid.json",
            now=now,
        )
        target_state = "halted_or_unknown"
        if readback_result.returncode != 0:
            raise SelectorFlashError("post-power-cycle BIN/UID readback failed")
        target_uid = _target_uid_from_file(uid_path, board)
        uid_identity = _file_identity(uid_path, "phase2 target UID")
        readback_identity = _file_identity(readback_path, "phase2 BIN-extent readback")
        expected_bin = role_paths["firmware_bin"].read_bytes()
        observed_bin = readback_path.read_bytes()
        if len(observed_bin) != byte_count or observed_bin != expected_bin:
            raise SelectorFlashError("target BIN-extent readback differs byte-for-byte")
        if readback_identity["sha256"] != extent["sha256"]:
            raise SelectorFlashError("target BIN-extent SHA-256 differs from phase 1")
        verified_image = True

        reset_attempted = True
        reset_succeeded, logs["reset_run_after_readback"] = _reset_run(
            boundary,
            repository=repository,
            config=config,
            log_path=run_directory / "phase2-reset-run.json",
            now=now,
        )
        target_state = "running" if reset_succeeded else "halted_or_unknown"
        if not reset_succeeded:
            halt_succeeded, logs["halt_after_readback_reset_failure"] = _halt_target(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase2-halt-after-reset-failure.json",
                now=now,
            )
            target_state = "halted" if halt_succeeded else "halted_or_unknown"
            raise SelectorFlashError("target could not be returned to reset run after readback")

        sleep(STARTUP_SETTLE_SECONDS)
        startup: dict[str, Any]
        if image_role == "bench":
            profile_contract = _mapping(current_inputs.get("control_profile"), "control profile")
            target_state = "halted_or_unknown"
            reset_succeeded = False
            startup, startup_logs = _bench_startup_evidence(
                boundary=boundary,
                repository=repository,
                run_directory=run_directory,
                config=config,
                manifest_path=role_paths["build_manifest"],
                all_off_code=int(profile_contract["all_off_code"]),
                now=now,
                sleep=sleep,
            )
            logs.update({f"bench_{name}": value for name, value in startup_logs.items()})
            reset_attempted = True
            reset_succeeded, logs["final_reset_run"] = _reset_run(
                boundary,
                repository=repository,
                config=config,
                log_path=run_directory / "phase2-final-reset-run.json",
                now=now,
            )
            target_state = "running" if reset_succeeded else "halted_or_unknown"
            if not reset_succeeded:
                raise SelectorFlashError("bench target could not be returned to final reset run")
            startup["explicit_final_reset_run_succeeded"] = True
        else:
            startup = {
                "evidence_kind": "fast20_exact_image_reset_run_identity_v1",
                "exact_bin_extent_readback_passed": True,
                "target_uid_matches_board_id": True,
                "explicit_reset_run_succeeded": True,
                "startup_settle_seconds": STARTUP_SETTLE_SECONDS,
                "autonomous_schedule_timing_proven": False,
                "runtime_gpio_sequence_proven": False,
                "claim_scope": (
                    "exact image bytes, exact target UID, and successful reset-run only"
                ),
            }

        phase1_identity = _file_identity(phase1_path, "phase-1 evidence")
        manifest = {
            "schema": SCHEMA,
            "evidence_kind": EVIDENCE_KIND,
            "status": "passed",
            "campaign_id": campaign,
            "run_id": run,
            "board_id": board,
            "image_role": image_role,
            "source": current_source,
            "phase1": phase1_identity,
            "phase2_started": _file_identity(phase2_started_path, "phase-2 started evidence"),
            "phase1_status_consumed": "awaiting_power_cycle",
            "operator_attestations": {
                "pre_program": _mapping(
                    _mapping(phase1.get("pre_program_operator_attestation"), "pre-program").get(
                        "snapshot"
                    ),
                    "pre-program snapshot",
                ),
                "power_cycle_source": power_source,
                "power_cycle_snapshot": power_snapshot,
                "power_cycle_validated": True,
                "phase2_pluto_mute_source": phase2_mute_source,
                "phase2_pluto_mute_snapshot": phase2_mute_snapshot,
                "phase2_pluto_mute_validated_immediately_before_openocd": True,
            },
            "frozen_inputs": current_inputs,
            "target_identity": {
                "uid": target_uid,
                "uid_readback": uid_identity,
                "matches_board_id": True,
            },
            "target_flash_readback": {
                **readback_identity,
                "flash_base_address": FLASH_BASE_ADDRESS,
                "byte_count": byte_count,
                "exact_bin_extent_only": True,
                "exact_byte_match": True,
                "expected_bin_sha256": extent["sha256"],
                "observed_target_sha256": readback_identity["sha256"],
            },
            "startup": startup,
            "logs": logs,
            "target_state_after_attestation": target_state,
            "sealed_at": now(),
            "downstream_use_permitted": True,
            "limitations": {
                "digital_readback_does_not_prove_physical_rf_state": True,
                "fast20_timing_requires_separate_capture_qualification": True,
            },
        }
        final_path = run_directory / FINAL_EVIDENCE_FILENAME
        _write_new_json(final_path, manifest)
        final_sha256 = sha256_path(final_path)
        digest_path = run_directory / FINAL_DIGEST_FILENAME
        _write_new_bytes(
            digest_path,
            f"{final_sha256}  {FINAL_EVIDENCE_FILENAME}\n".encode("ascii"),
        )
        return SealedSelectorEvidence(
            path=final_path, sha256=final_sha256, run_directory=run_directory
        )
    except BaseException as error:
        if target_state == "halted_or_unknown" and not reset_succeeded:
            recovery_name = (
                "phase2-failure-reset-run.json" if verified_image else "phase2-failure-halt.json"
            )
            recovery_path = run_directory / recovery_name
            if not recovery_path.exists():
                if verified_image:
                    reset_attempted = True
                    recovery_succeeded, _ = _reset_run(
                        boundary,
                        repository=repository,
                        config=role_paths["openocd_config"],
                        log_path=recovery_path,
                        now=now,
                    )
                    reset_succeeded = recovery_succeeded
                    target_state = "running" if recovery_succeeded else "halted_or_unknown"
                else:
                    halt_succeeded, _ = _halt_target(
                        boundary,
                        repository=repository,
                        config=role_paths["openocd_config"],
                        log_path=recovery_path,
                        now=now,
                    )
                    target_state = "halted" if halt_succeeded else "halted_or_unknown"
        _write_failure(
            run_directory,
            phase="verify_after_power_cycle",
            error=error,
            target_state=target_state,
            reset_run_attempted=reset_attempted,
            reset_run_succeeded=reset_succeeded,
            now=now,
        )
        if isinstance(error, SelectorFlashError):
            raise
        raise SelectorFlashError(str(error)) from error


def validate_sealed_selector_evidence(
    path: Path,
    *,
    expected_sha256: str,
    expected_campaign_id: str,
    expected_run_id: str,
    expected_board_id: str,
    expected_image_role: ImageRole,
) -> dict[str, Any]:
    """Validate the exact downstream path/hash tuple and all retained leaf artifacts."""

    resolved = _require_regular_file(path, "selector flash evidence")
    expected_digest = _validate_sha256(expected_sha256, "selector flash evidence SHA-256")
    if sha256_path(resolved) != expected_digest:
        raise SelectorFlashError("selector flash evidence file SHA-256 differs")
    document = _read_json(resolved, "selector flash evidence", require_canonical=True)
    if (
        document.get("schema") != SCHEMA
        or document.get("evidence_kind") != EVIDENCE_KIND
        or document.get("status") != "passed"
        or document.get("campaign_id") != expected_campaign_id
        or document.get("run_id") != expected_run_id
        or document.get("board_id") != expected_board_id
        or document.get("image_role") != expected_image_role
        or document.get("downstream_use_permitted") is not True
        or document.get("target_state_after_attestation") != "running"
    ):
        raise SelectorFlashError("selector flash evidence identity or acceptance state differs")
    phase1_identity = _validate_file_identity(document.get("phase1"), "selector flash phase1")
    phase2_started_identity = _validate_file_identity(
        document.get("phase2_started"), "selector flash phase2-started evidence"
    )
    phase1_path = Path(str(phase1_identity["path"]))
    phase1 = _validate_phase1(
        _read_json(phase1_path, "sealed phase-1 evidence", require_canonical=True),
        campaign_id=expected_campaign_id,
        run_id=expected_run_id,
        board_id=expected_board_id,
        image_role=expected_image_role,
    )
    source = _mapping(document.get("source"), "selector flash source")
    if source != phase1.get("source"):
        raise SelectorFlashError("sealed source identity differs from phase 1")
    frozen_inputs = _mapping(document.get("frozen_inputs"), "sealed frozen inputs")
    phase1_build = _mapping(phase1.get("build"), "sealed phase1 build")
    if frozen_inputs != phase1_build.get("inputs"):
        raise SelectorFlashError("sealed frozen inputs differ from phase 1")
    frozen_files = _mapping(frozen_inputs.get("files"), "sealed frozen input files")
    target = _mapping(document.get("target_identity"), "selector flash target identity")
    if (
        target.get("uid") != expected_board_id.removeprefix("stm32c011-")
        or target.get("matches_board_id") is not True
    ):
        raise SelectorFlashError("sealed target UID differs from expected board")
    uid_identity = _validate_file_identity(target.get("uid_readback"), "sealed target UID readback")
    _target_uid_from_file(Path(str(uid_identity["path"])), expected_board_id)
    readback = _mapping(document.get("target_flash_readback"), "target flash readback")
    readback_identity = _validate_file_identity(
        {name: readback.get(name) for name in ("path", "sha256", "size_bytes")},
        "target flash readback",
    )
    if (
        readback.get("flash_base_address") != FLASH_BASE_ADDRESS
        or readback.get("byte_count") != readback_identity["size_bytes"]
        or readback.get("exact_bin_extent_only") is not True
        or readback.get("exact_byte_match") is not True
        or readback.get("expected_bin_sha256") != readback_identity["sha256"]
        or readback.get("observed_target_sha256") != readback_identity["sha256"]
    ):
        raise SelectorFlashError("sealed target flash readback contract is invalid")
    firmware_bin_identity = _validate_file_identity(
        frozen_files.get("firmware_bin"), "sealed firmware BIN"
    )
    if (
        readback_identity["sha256"] != firmware_bin_identity["sha256"]
        or readback_identity["size_bytes"] != firmware_bin_identity["size_bytes"]
        or Path(str(readback_identity["path"])).read_bytes()
        != Path(str(firmware_bin_identity["path"])).read_bytes()
    ):
        raise SelectorFlashError("sealed target readback differs from the frozen firmware BIN")
    logs = _mapping(document.get("logs"), "selector flash logs")
    for name, value in logs.items():
        _validate_file_identity(value, f"selector flash log {name}")
    operator = _mapping(document.get("operator_attestations"), "operator attestations")
    for name in (
        "pre_program",
        "power_cycle_source",
        "power_cycle_snapshot",
        "phase2_pluto_mute_source",
        "phase2_pluto_mute_snapshot",
    ):
        _validate_file_identity(operator.get(name), f"operator attestation {name}")
    if operator.get("phase2_pluto_mute_validated_immediately_before_openocd") is not True:
        raise SelectorFlashError("sealed phase-2 Pluto mute validation is missing")
    pre_program_identity = _validate_file_identity(
        operator.get("pre_program"), "sealed pre-program attestation"
    )
    phase1_pre_program = _mapping(
        phase1.get("pre_program_operator_attestation"),
        "sealed phase1 pre-program attestation",
    )
    if phase1_pre_program.get("snapshot") != pre_program_identity:
        raise SelectorFlashError("sealed pre-program attestation differs from phase 1")
    validate_pre_program_attestation(
        _read_json(Path(str(pre_program_identity["path"])), "sealed pre-program attestation"),
        campaign_id=expected_campaign_id,
        run_id=expected_run_id,
        board_id=expected_board_id,
        image_role=expected_image_role,
    )
    power_identity = _validate_file_identity(
        operator.get("power_cycle_snapshot"), "sealed power-cycle attestation"
    )
    validate_power_cycle_attestation(
        _read_json(Path(str(power_identity["path"])), "sealed power-cycle attestation"),
        campaign_id=expected_campaign_id,
        run_id=expected_run_id,
        board_id=expected_board_id,
        image_role=expected_image_role,
        phase1_path=phase1_path,
        phase1_sha256=str(phase1_identity["sha256"]),
    )
    phase2_started = _read_json(
        Path(str(phase2_started_identity["path"])),
        "sealed phase2-started evidence",
        require_canonical=True,
    )
    phase2_mute_source_identity = _validate_file_identity(
        operator.get("phase2_pluto_mute_source"), "sealed phase2 Pluto mute source"
    )
    phase2_mute_snapshot_identity = _validate_file_identity(
        operator.get("phase2_pluto_mute_snapshot"), "sealed phase2 Pluto mute snapshot"
    )
    phase2_mute_source = _read_json(
        Path(str(phase2_mute_source_identity["path"])),
        "sealed phase2 Pluto mute source",
        require_canonical=True,
    )
    phase2_mute_snapshot = _read_json(
        Path(str(phase2_mute_snapshot_identity["path"])),
        "sealed phase2 Pluto mute snapshot",
        require_canonical=True,
    )
    if phase2_mute_source != phase2_mute_snapshot:
        raise SelectorFlashError("sealed phase2 Pluto mute source/snapshot differ")
    validate_pluto_mute_evidence(
        phase2_mute_snapshot,
        checkpoint="phase2_pre_openocd",
        serial=str(phase2_mute_snapshot.get("serial")),
        uri=str(phase2_mute_snapshot.get("uri")),
        source=source,
        validated_at=str(phase2_mute_snapshot.get("completed_at")),
    )
    if (
        phase2_started.get("schema") != SCHEMA
        or phase2_started.get("evidence_kind") != "selector_flash_phase2_started_v1"
        or phase2_started.get("status") != "started"
        or phase2_started.get("campaign_id") != expected_campaign_id
        or phase2_started.get("run_id") != expected_run_id
        or phase2_started.get("board_id") != expected_board_id
        or phase2_started.get("image_role") != expected_image_role
        or phase2_started.get("phase1") != phase1_identity
        or phase2_started.get("power_cycle_attestation") != power_identity
        or phase2_started.get("pluto_pre_openocd_mute_source") != phase2_mute_source_identity
        or phase2_started.get("pluto_pre_openocd_mute_snapshot") != phase2_mute_snapshot_identity
        or phase2_started.get("run_id_burned_if_final_evidence_is_not_sealed") is not True
    ):
        raise SelectorFlashError("sealed phase2-started evidence binding is invalid")
    startup = _mapping(document.get("startup"), "selector startup evidence")
    if expected_image_role == "bench":
        if (
            startup.get("evidence_kind") != "bench_mailbox_gpio_all_off_startup_v1"
            or startup.get("mailbox_all_off_passed") is not True
            or startup.get("gpio_latch_all_off_passed") is not True
            or startup.get("explicit_final_reset_run_succeeded") is not True
            or startup.get("physical_rf_state_proven") is not False
        ):
            raise SelectorFlashError("sealed bench ALL_OFF startup evidence is invalid")
        _validate_file_identity(
            startup.get("initial_mailbox_readback"), "initial bench mailbox readback"
        )
        mailbox_identity = _validate_file_identity(
            startup.get("mailbox_readback"), "bench mailbox readback"
        )
        build_manifest_identity = _validate_file_identity(
            frozen_files.get("build_manifest"), "sealed bench build manifest"
        )
        manifest = BenchManifest.load(Path(str(build_manifest_identity["path"])))
        decoded = decode_mailbox(Path(str(mailbox_identity["path"])).read_bytes(), manifest)
        profile_contract = _mapping(frozen_inputs.get("control_profile"), "sealed control profile")
        all_off_code = _integer(profile_contract.get("all_off_code"), "sealed ALL_OFF code")
        command = _mapping(
            startup.get("explicit_lease_free_all_off_command"),
            "sealed explicit ALL_OFF command",
        )
        if (
            decoded.as_dict() != startup.get("mailbox")
            or decoded.command_code != all_off_code
            or decoded.applied_code != all_off_code
            or decoded.command_lease_ms != 0
            or decoded.remaining_lease_ms != 0
            or decoded.command_sequence != decoded.acknowledged_sequence
            or not decoded.command_valid
            or decoded.lease_active
            or decoded.guard_active
            or decoded.invalid_command
            or command.get("sequence") != decoded.command_sequence
            or command.get("code") != all_off_code
            or command.get("lease_ms") != 0
            or command.get("passed") is not True
        ):
            raise SelectorFlashError("sealed raw bench mailbox does not prove explicit ALL_OFF")
        gpio = _mapping(startup.get("gpio_output_latch"), "GPIO output latch")
        gpio_identity = _validate_file_identity(gpio.get("raw_readback"), "GPIOA ODR raw readback")
        gpio_bytes = Path(str(gpio_identity["path"])).read_bytes()
        if (
            len(gpio_bytes) != 4
            or int.from_bytes(gpio_bytes, "little") != gpio.get("raw_value")
            or int(gpio.get("raw_value", -1)) & SELECTOR_GPIO_MASK != all_off_code
            or gpio.get("masked_selector_code") != all_off_code
            or gpio.get("expected_all_off_code") != all_off_code
        ):
            raise SelectorFlashError("sealed raw GPIOA ODR does not prove ALL_OFF")
    elif (
        startup.get("evidence_kind") != "fast20_exact_image_reset_run_identity_v1"
        or startup.get("exact_bin_extent_readback_passed") is not True
        or startup.get("target_uid_matches_board_id") is not True
        or startup.get("explicit_reset_run_succeeded") is not True
        or startup.get("autonomous_schedule_timing_proven") is not False
        or startup.get("runtime_gpio_sequence_proven") is not False
    ):
        raise SelectorFlashError("sealed Fast20 startup evidence overclaims or is incomplete")
    return document
