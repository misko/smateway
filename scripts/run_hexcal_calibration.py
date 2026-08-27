#!/usr/bin/env python3
"""Run a resumable, fail-muted, multi-frequency ``hexcal-v1`` calibration."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_SMATEWAY_SOURCE = Path(__file__).resolve().parents[1] / "src"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX or str(_SMATEWAY_SOURCE) not in sys.path
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned hexcal Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

from pluto_plus.bootstrap_firmware import mute_returned_radio

from smateway.hexcal import (
    HEXCAL_ANALYSIS_SOURCE_FILES,
    PLUTO_PLUS_UTILS_PYTHON,
    HexcalFirmwareEvidence,
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    canonical_json_sha256,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
)
from smateway.hexcal_gain import (
    QUALIFICATION_SOURCE_FILES,
    STIMULUS_CENTER_FREQUENCIES_HZ,
    STIMULUS_PROTOCOL_ID,
    HexcalGainQualification,
    HexcalStimulusQualification,
    load_hexcal_gain_qualification,
    load_hexcal_stimulus_qualification,
)
from smateway.rf_policy import EXPERIMENTAL_5G8_CENTER_HZ, classify_fast20_center_frequency

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_PYTHON = PLUTO_PLUS_UTILS_PYTHON
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
DEFAULT_FREQUENCIES_HZ = (
    2_400_000_000,
    2_423_000_000,
    2_440_000_000,
    2_458_000_000,
    2_483_000_000,
    EXPERIMENTAL_5G8_CENTER_HZ,
)
DEFAULT_ROUNDS = 3
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 180
ARTIFACT_TOKEN = "{artifact_id}"
CAPTURE_FILENAME = "hexcal-capture.json"
ANALYSIS_FILENAME = "hexcal-analysis.json"
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class ExperimentError(RuntimeError):
    """A persisted invariant, live command, or exact mute failed."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="exact usb: IIO URI")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--run-id")
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--firmware-evidence", type=Path, required=True)
    parser.add_argument(
        "--gain-qualification",
        type=Path,
        help="passed exploratory ledger; its lowest sufficient gain is fixed for this run",
    )
    parser.add_argument(
        "--stimulus-qualification",
        type=Path,
        help="passed hexcal-v2 ledger; its fixed RX gain and selected TX1 level are used",
    )
    parser.add_argument("--protocol-v2", "--protocol-v21", "--protocol-v22", action="store_true")
    parser.add_argument("--allow-experimental-5g8", action="store_true")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--max-attempts-per-condition", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--tx-hardware-gain-db", type=float)
    parser.add_argument("--dds-scale", type=float, default=0.125)
    parser.add_argument(
        "--center-frequency-hz",
        action="append",
        type=int,
        dest="center_frequencies_hz",
        metavar="HZ",
        help="repeat to replace the six-frequency default plan",
    )
    return parser


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _cooperative_termination(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _new_run_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _board_root(board_id: str) -> Path:
    return Path.home() / ".local/state/smateway/boards" / board_id


def _repository_commit_and_require_clean(repository: Path) -> str:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ExperimentError(
            "hexcal runner refuses to persist a plan from a dirty implementation tree"
        )
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ExperimentError("implementation source commit is malformed")
    return commit


@contextmanager
def _board_lock(board_root: Path) -> Iterator[None]:
    board_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (board_root / ".bench.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _write_manifest(path: Path, document: Mapping[str, Any]) -> None:
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


def _validate_frequencies(
    values: Sequence[int] | None,
    *,
    allow_experimental_5g8: bool,
    defaults: Sequence[int] = DEFAULT_FREQUENCIES_HZ,
) -> tuple[int, ...]:
    frequencies = tuple(defaults) if values is None else tuple(values)
    if not frequencies:
        raise ValueError("at least one center frequency is required")
    output: list[int] = []
    for frequency_hz in frequencies:
        classify_fast20_center_frequency(
            frequency_hz,
            allow_experimental_5g8=allow_experimental_5g8,
        )
        if frequency_hz in output:
            raise ValueError(f"center frequencies must be unique: {frequency_hz}")
        output.append(frequency_hz)
    return tuple(output)


def _round_order(round_index: int, frequencies: Sequence[int]) -> tuple[str, tuple[int, ...]]:
    values = tuple(frequencies)
    if round_index % 3 == 0:
        return "forward", values
    if round_index % 3 == 1:
        return "reverse", tuple(reversed(values))
    rotation = (2 + round_index // 3) % len(values)
    if rotation == 0:
        rotation = len(values)
    return f"rotate_left_{rotation}", values[rotation:] + values[:rotation]


def _capture_command(
    python: Path,
    repository: Path,
    *,
    board_id: str,
    serial: str,
    uri: str,
    profile: Path,
    frequency_hz: int,
    receiver_gain_db: int,
    tx_hardware_gain_db: float,
    dds_scale: float,
    source_commit: str,
    pluto_plus_utils_attestation_sha256: str,
    firmware_evidence: Mapping[str, Any],
    allow_experimental_5g8: bool,
) -> list[str]:
    command = [
        str(python),
        str(repository / "scripts/capture_hexcal.py"),
        "--board-id",
        board_id,
        "--serial",
        serial,
        "--uri",
        uri,
        "--profile",
        str(profile),
        "--source-commit",
        source_commit,
        "--pluto-plus-utils-attestation-sha256",
        pluto_plus_utils_attestation_sha256,
        "--firmware-evidence",
        str(firmware_evidence["path"]),
        "--firmware-evidence-sha256",
        str(firmware_evidence["file_sha256"]),
        "--center-frequency-hz",
        str(frequency_hz),
        "--receiver-gain-db",
        str(receiver_gain_db),
        "--tx-hardware-gain-db",
        str(tx_hardware_gain_db),
        "--dds-scale",
        str(dds_scale),
    ]
    if frequency_hz == EXPERIMENTAL_5G8_CENTER_HZ and allow_experimental_5g8:
        command.append("--allow-experimental-5g8")
    return command


def _reanalyze_command(
    python: Path,
    repository: Path,
    *,
    board_id: str,
    serial: str,
    uri: str,
    profile: Path,
) -> list[str]:
    return [
        str(python),
        str(repository / "scripts/reanalyze_hexcal_artifact.py"),
        ARTIFACT_TOKEN,
        "--board-id",
        board_id,
        "--serial",
        serial,
        "--uri",
        uri,
        "--profile",
        str(profile),
    ]


def _configuration(
    *,
    rounds: int,
    max_attempts: int,
    board_id: str,
    serial: str,
    uri: str,
    python: Path,
    profile: Path,
    profile_file_sha256: str,
    profile_contract_sha256: str,
    timeout_s: int,
    receiver_gain_db: int,
    tx_hardware_gain_db: float,
    dds_scale: float,
    center_frequencies_hz: Sequence[int],
    source_commit: str,
    pluto_plus_utils_source_attestation: Mapping[str, Any],
    firmware_evidence: HexcalFirmwareEvidence,
    gain_qualification: HexcalGainQualification | None,
    stimulus_qualification: HexcalStimulusQualification | None = None,
    allow_experimental_5g8: bool,
) -> dict[str, Any]:
    if (gain_qualification is None) == (stimulus_qualification is None):
        raise ExperimentError("exactly one gain/stimulus qualification is required")
    if gain_qualification is not None:
        if receiver_gain_db != gain_qualification.selected_receiver_gain_db:
            raise ExperimentError("calibration gain differs from the passed qualification")
        qualification_field = "gain_qualification"
        qualification_document = gain_qualification.as_dict()
        protocol_id = "hexcal-v1"
    else:
        assert stimulus_qualification is not None
        if (
            receiver_gain_db != stimulus_qualification.fixed_receiver_gain_db
            or tx_hardware_gain_db != stimulus_qualification.selected_tx_hardware_gain_db
            or dds_scale != stimulus_qualification.dds_scale
            or tuple(center_frequencies_hz) != stimulus_qualification.center_frequencies_hz
        ):
            raise ExperimentError("calibration stimulus differs from the passed v2 qualification")
        qualification_field = "stimulus_qualification"
        qualification_document = stimulus_qualification.as_dict()
        protocol_id = STIMULUS_PROTOCOL_ID
    dependency_attestation = dict(pluto_plus_utils_source_attestation)
    dependency_sha256 = canonical_json_sha256(dependency_attestation)
    configuration = {
        "protocol_id": protocol_id,
        "rounds": rounds,
        "max_attempts_per_condition": max_attempts,
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "python": str(python),
        "profile": str(profile),
        "profile_file_sha256": profile_file_sha256,
        "profile_contract_sha256": profile_contract_sha256,
        "timeout_s": timeout_s,
        "receiver_gain_db": receiver_gain_db,
        "tx_hardware_gain_db": tx_hardware_gain_db,
        "dds_scale": dds_scale,
        "implementation_source_commit": source_commit,
        "pluto_plus_utils_source_attestation": dependency_attestation,
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "firmware_evidence": firmware_evidence.as_dict(),
        "allow_experimental_5g8": allow_experimental_5g8,
        "center_frequencies_hz": list(center_frequencies_hz),
        "sample_rate_hz": 1_000_000,
        "samples_per_frame": 100_000,
        "frame_count": 10,
        "kernel_buffers": 8,
        "duration_s": 1.0,
        "tx_channel": 0,
        "tx_port": "TX1",
        "round_order_policy": ["forward", "reverse", "rotated"],
    }
    configuration[qualification_field] = qualification_document
    return configuration


def _execution_plan(configuration: Mapping[str, Any], repository: Path) -> list[dict[str, Any]]:
    frequencies = tuple(int(value) for value in configuration["center_frequencies_hz"])
    python = Path(str(configuration["python"]))
    profile = Path(str(configuration["profile"]))
    qualification_kind = "stimulus" if "stimulus_qualification" in configuration else "gain"
    qualification = _mapping(
        configuration.get(f"{qualification_kind}_qualification"),
        f"{qualification_kind} qualification",
    )
    plan: list[dict[str, Any]] = []
    for round_index in range(int(configuration["rounds"])):
        order_name, ordered = _round_order(round_index, frequencies)
        for order_index, frequency_hz in enumerate(ordered):
            condition = {
                "plan_index": len(plan),
                "round_index": round_index,
                "round_number": round_index + 1,
                "round_order": order_name,
                "order_index": order_index,
                "center_frequency_hz": frequency_hz,
                "tx_channel": 0,
                "tx_port": "TX1",
                "sample_rate_hz": 1_000_000,
                "receiver_gain_db": int(configuration["receiver_gain_db"]),
                "qualification_kind": (qualification_kind),
                "qualification_id": str(qualification["qualification_id"]),
                "qualification_sha256": str(qualification["file_sha256"]),
                "planned_tx_hardware_gain_db": float(configuration["tx_hardware_gain_db"]),
                "planned_dds_scale": float(configuration["dds_scale"]),
                "profile_file_sha256": str(configuration["profile_file_sha256"]),
                "implementation_source_commit": str(configuration["implementation_source_commit"]),
                "pluto_plus_utils_source_attestation_sha256": str(
                    configuration["pluto_plus_utils_source_attestation_sha256"]
                ),
                "firmware_evidence_sha256": str(configuration["firmware_evidence"]["file_sha256"]),
                "firmware_bin_sha256": str(
                    configuration["firmware_evidence"]["firmware_bin_sha256"]
                ),
                "full_flash_readback_sha256": str(
                    configuration["firmware_evidence"]["full_flash_readback_sha256"]
                ),
            }
            condition[f"{qualification_kind}_qualification_id"] = condition["qualification_id"]
            condition[f"{qualification_kind}_qualification_sha256"] = condition[
                "qualification_sha256"
            ]
            condition["capture_command"] = _capture_command(
                python,
                repository,
                board_id=str(configuration["board_id"]),
                serial=str(configuration["serial"]),
                uri=str(configuration["uri"]),
                profile=profile,
                frequency_hz=frequency_hz,
                receiver_gain_db=int(configuration["receiver_gain_db"]),
                tx_hardware_gain_db=float(configuration["tx_hardware_gain_db"]),
                dds_scale=float(configuration["dds_scale"]),
                source_commit=str(configuration["implementation_source_commit"]),
                pluto_plus_utils_attestation_sha256=str(
                    configuration["pluto_plus_utils_source_attestation_sha256"]
                ),
                firmware_evidence=configuration["firmware_evidence"],
                allow_experimental_5g8=bool(configuration["allow_experimental_5g8"]),
            )
            condition["reanalysis_command_template"] = _reanalyze_command(
                python,
                repository,
                board_id=str(configuration["board_id"]),
                serial=str(configuration["serial"]),
                uri=str(configuration["uri"]),
                profile=profile,
            )
            plan.append(condition)
    return plan


def _new_manifest(
    run_id: str, configuration: Mapping[str, Any], repository: Path
) -> dict[str, Any]:
    now = _now()
    return {
        "schema": 1,
        "experiment_kind": (
            "hexcal_v2_2_2g4_tx1_center_calibration"
            if configuration.get("protocol_id") == STIMULUS_PROTOCOL_ID
            else "hexcal_v1_tx1_center_calibration"
        ),
        "run_id": run_id,
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "configuration": dict(configuration),
        "plan": _execution_plan(configuration, repository),
        "attempts": [],
        "recovery_mute_attempts": [],
        "final_mute_attempts": [],
        "resume_count": 0,
        "summary": {},
    }


def _validate_attempt_history(document: Mapping[str, Any]) -> None:
    plan = document.get("plan")
    attempts = document.get("attempts")
    if not isinstance(plan, list) or not isinstance(attempts, list):
        raise ExperimentError("resume plan or attempts are malformed")
    conditions = {
        int(item["plan_index"]): item
        for item in plan
        if isinstance(item, Mapping) and isinstance(item.get("plan_index"), int)
    }
    if len(conditions) != len(plan):
        raise ExperimentError("resume plan indices are malformed")
    configuration = document.get("configuration")
    if not isinstance(configuration, Mapping) or not isinstance(configuration.get("serial"), str):
        raise ExperimentError("resume configuration serial is malformed")
    serial = str(configuration["serial"])
    retry_counts: dict[int, int] = {}
    completed: set[int] = set()
    for expected_attempt_id, raw in enumerate(attempts, start=1):
        if not isinstance(raw, Mapping) or raw.get("attempt_id") != expected_attempt_id:
            raise ExperimentError("resume attempt IDs are malformed")
        index = raw.get("plan_index")
        if not isinstance(index, int) or index not in conditions:
            raise ExperimentError("resume attempt references an unknown condition")
        retry = retry_counts.get(index, 0)
        if raw.get("retry") != retry:
            raise ExperimentError("resume retry counters are malformed")
        retry_counts[index] = retry + 1
        condition = conditions[index]
        if any(raw.get(key) != value for key, value in condition.items()):
            raise ExperimentError("resume attempt differs from its persisted plan")
        status = raw.get("status")
        if status == "complete":
            if index in completed:
                raise ExperimentError("resume has duplicate completed conditions")
            if raw.get("outcome") not in {"quality_passed", "quality_rejected"}:
                raise ExperimentError("completed resume attempt has no quality outcome")
            if not isinstance(raw.get("artifact_identity"), Mapping):
                raise ExperimentError("completed resume attempt lacks artifact identity")
            if not isinstance(raw.get("quality_result"), Mapping):
                raise ExperimentError("completed resume attempt lacks quality evidence")
            post_mute = raw.get("post_mute")
            if not _mute_attestation_passed(post_mute, serial=serial, purpose="post_attempt"):
                raise ExperimentError("completed resume attempt lacks exact post mute")
            completed.add(index)
        elif status == "failed":
            if raw.get("artifact_identity") is not None:
                raise ExperimentError("failed resume attempt accepts an artifact identity")
        elif status != "running":
            raise ExperimentError("resume attempt status is unsupported")


def _load_manifest(
    path: Path, configuration: Mapping[str, Any], repository: Path
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"cannot load resume manifest: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ExperimentError("resume manifest schema is unsupported")
    expected_experiment_kind = (
        "hexcal_v2_2_2g4_tx1_center_calibration"
        if configuration.get("protocol_id") == STIMULUS_PROTOCOL_ID
        else "hexcal_v1_tx1_center_calibration"
    )
    if document.get("experiment_kind") != expected_experiment_kind:
        raise ExperimentError("resume manifest belongs to another experiment")
    if document.get("configuration") != dict(configuration):
        raise ExperimentError("resume arguments differ from persisted configuration")
    if document.get("plan") != _execution_plan(configuration, repository):
        raise ExperimentError("resume execution plan differs from persisted plan")
    if not isinstance(document.get("attempts"), list):
        raise ExperimentError("resume attempts are malformed")
    _validate_attempt_history(document)
    document["resume_count"] = int(document.get("resume_count", 0)) + 1
    document["status"] = "running"
    document.pop("completed_at", None)
    document.pop("error", None)
    return document


def _extract_json_object(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _stop_process(process: subprocess.Popen[str], grace_s: int = 15) -> tuple[str, str]:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
    try:
        return process.communicate(timeout=grace_s)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return process.communicate()


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_s: int,
) -> dict[str, Any]:
    started = _now()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        return {
            "command": list(command),
            "started_at": started,
            "completed_at": _now(),
            "return_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "error": _error_text(error),
        }
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr = _stop_process(process)
    except BaseException:
        _stop_process(process)
        raise
    return {
        "command": list(command),
        "started_at": started,
        "completed_at": _now(),
        "return_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "error": "command timed out" if timed_out else None,
    }


def _artifact_ids(capture_root: Path) -> set[str]:
    if not capture_root.is_dir():
        return set()
    return {
        item.name
        for item in capture_root.iterdir()
        if item.is_dir() and ARTIFACT_ID.fullmatch(item.name) is not None
    }


def _failed_artifact_ids(capture_root: Path) -> set[str]:
    failed_root = capture_root / ".failed"
    if not failed_root.is_dir():
        return set()
    return {
        item.name
        for item in failed_root.iterdir()
        if item.is_dir() and ARTIFACT_ID.fullmatch(item.name) is not None
    }


def _quarantined_failure(capture_root: Path, artifact_id: str) -> dict[str, Any]:
    root = capture_root / ".failed" / artifact_id
    files = []
    for path in sorted(item for item in root.iterdir() if item.is_file()):
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "sha256": sha256_path(path),
                "size_bytes": path.stat().st_size,
            }
        )
    failure_path = root / "failure.json"
    failure_record: Mapping[str, Any] | None = None
    if failure_path.is_file():
        try:
            raw = json.loads(failure_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, Mapping):
            failure_record = raw
    return {
        "artifact_id": artifact_id,
        "path": str(root),
        "accepted": False,
        "failure_record": None if failure_record is None else dict(failure_record),
        "files": files,
    }


def _fresh_artifact_id(
    result: Mapping[str, Any], before: set[str], capture_root: Path
) -> tuple[str | None, str | None]:
    parsed = result.get("parsed_output")
    if isinstance(parsed, Mapping):
        candidate = parsed.get("artifact_id")
        if isinstance(candidate, str):
            if (
                ARTIFACT_ID.fullmatch(candidate) is not None
                and candidate not in before
                and (capture_root / candidate).is_dir()
            ):
                return candidate, "stdout"
            return None, None
    created = sorted(_artifact_ids(capture_root) - before)
    if len(created) == 1:
        return created[0], "directory_diff"
    return None, None


def _environment(repository: Path) -> dict[str, str]:
    environment = dict(os.environ)
    source = str(repository / "src")
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not prior else f"{source}{os.pathsep}{prior}"
    return environment


def _strict_mute(serial: str, purpose: str) -> dict[str, Any]:
    started = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started,
            "completed_at": _now(),
            "error": _error_text(error),
        }
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": started,
        "completed_at": _now(),
        "error": None,
    }


def _mute_attestation_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentError(f"{label} is not an object")
    return value


def _analysis_identity(
    artifact_root: Path,
    condition: Mapping[str, Any],
    *,
    serial: str,
    uri: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture_path = artifact_root / CAPTURE_FILENAME
    analysis_path = artifact_root / ANALYSIS_FILENAME
    try:
        capture_document = json.loads(capture_path.read_text(encoding="utf-8"))
        analysis_document = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"cannot load completed artifact records: {error}") from error
    capture_root = _mapping(capture_document, "capture record")
    analysis_root = _mapping(analysis_document, "analysis record")
    capture_dependency = _mapping(
        capture_root.get("pluto_plus_utils_source_attestation"),
        "capture pluto-plus-utils source attestation",
    )
    analysis_dependency = _mapping(
        analysis_root.get("pluto_plus_utils_source_attestation"),
        "analysis pluto-plus-utils source attestation",
    )
    dependency_sha256 = canonical_json_sha256(capture_dependency)
    capture_firmware = _mapping(capture_root.get("firmware_evidence"), "capture firmware evidence")
    capture_settings = _mapping(capture_root.get("capture"), "capture settings")
    artifact_id = artifact_root.name
    capture_artifact = _mapping(capture_root.get("artifact"), "capture artifact")
    analysis_artifact = _mapping(analysis_root.get("artifact"), "analysis artifact")
    if (
        capture_artifact.get("artifact_id") != artifact_id
        or analysis_artifact.get("artifact_id") != artifact_id
        or capture_artifact.get("path") != str(artifact_root)
        or analysis_artifact.get("path") != str(artifact_root)
    ):
        raise ExperimentError("artifact identity differs between capture and reanalysis")
    key = _mapping(analysis_root.get("aggregation_key"), "aggregation key")
    analysis_source = _mapping(
        analysis_root.get("analysis_source_attestation"),
        "analysis source attestation",
    )
    source_files = analysis_source.get("files")
    observed_source_paths = (
        [item.get("path") for item in source_files if isinstance(item, Mapping)]
        if isinstance(source_files, list)
        else []
    )
    if (
        key.get("artifact_id") != artifact_id
        or key.get("serial") != serial
        or key.get("uri") != uri
        or key.get("tx_channel") != 0
        or key.get("tx_port") != "TX1"
        or key.get("center_frequency_hz") != condition.get("center_frequency_hz")
        or key.get("sample_rate_hz") != 1_000_000
        or key.get("receiver_gain_db") != condition.get("receiver_gain_db")
        or key.get("profile_file_sha256") != condition.get("profile_file_sha256")
        or key.get("implementation_source_commit") != condition.get("implementation_source_commit")
        or key.get("firmware_evidence_sha256") != condition.get("firmware_evidence_sha256")
        or key.get("firmware_bin_sha256") != condition.get("firmware_bin_sha256")
        or key.get("full_flash_readback_sha256") != condition.get("full_flash_readback_sha256")
        or analysis_root.get("source_commit") != condition.get("implementation_source_commit")
        or analysis_source.get("commit") != condition.get("implementation_source_commit")
        or observed_source_paths != list(HEXCAL_ANALYSIS_SOURCE_FILES)
        or dict(analysis_dependency) != dict(capture_dependency)
        or dependency_sha256 != condition.get("pluto_plus_utils_source_attestation_sha256")
        or capture_root.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
        or analysis_root.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
        or key.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
        or capture_root.get("source_commit") != condition.get("implementation_source_commit")
        or capture_firmware.get("file_sha256") != condition.get("firmware_evidence_sha256")
        or capture_firmware.get("firmware_bin_sha256") != condition.get("firmware_bin_sha256")
        or capture_firmware.get("full_flash_readback_sha256")
        != condition.get("full_flash_readback_sha256")
    ):
        raise ExperimentError("reanalysis aggregation identity differs from the plan")
    rf_readback = _mapping(
        capture_settings.get("rf_readback_evidence"), "capture RF readback evidence"
    )
    rf_readback_sha256 = canonical_json_sha256(rf_readback)
    try:
        normalized_rf_readback = validate_tx1_rf_readback_evidence(
            rf_readback,
            planned_kernel_buffers=8,
            planned_tx_gain_db=float(condition["planned_tx_hardware_gain_db"]),
            planned_dds_scale=float(condition["planned_dds_scale"]),
            planned_tone_hz=100_000.0,
            sample_rate_hz=1_000_000.0,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentError(f"RF readback evidence failed validation: {error}") from error
    if (
        capture_settings.get("rf_readback_evidence_sha256") != rf_readback_sha256
        or key.get("rf_readback_evidence_sha256") != rf_readback_sha256
        or capture_settings.get("kernel_buffers") != normalized_rf_readback["kernel_buffers"]
        or capture_settings.get("tx_gain_readback_db")
        != normalized_rf_readback["tx1_gain_readback_db"]
        or capture_settings.get("dds_scale_readback")
        != normalized_rf_readback["dds_scale_readback"]
        or capture_settings.get("dds_enabled_readback")
        != normalized_rf_readback["dds_enabled_readback"]
        or capture_settings.get("dds_frequency_readback_hz")
        != normalized_rf_readback["dds_frequency_readback_hz"]
    ):
        raise ExperimentError("RF readback evidence is not hash-bound to capture/reanalysis")
    raw_dds_readback = normalized_rf_readback["dds_frequency_readback_hz"]
    key_dds_readback = key.get("dds_frequency_readback_hz")
    if (
        not isinstance(raw_dds_readback, list)
        or len(raw_dds_readback) != 8
        or not isinstance(key_dds_readback, list)
        or len(key_dds_readback) != 8
    ):
        raise ExperimentError("DDS frequency readback identity is malformed")
    try:
        dds_readback = tuple(float(value) for value in raw_dds_readback)
        key_readback = tuple(float(value) for value in key_dds_readback)
    except (TypeError, ValueError) as error:
        raise ExperimentError("DDS frequency readback identity is malformed") from error
    if (
        not all(math.isfinite(value) for value in dds_readback + key_readback)
        or dds_readback != key_readback
    ):
        raise ExperimentError("analysis does not bind the exact TX1 DDS readback")
    active_tones = (abs(dds_readback[0]), abs(dds_readback[2]))
    actual_tone_offset_hz = sum(active_tones) / 2.0
    tone_offset_hz = key.get("dds_tone_offset_hz")
    emitted_carrier_hz = key.get("emitted_carrier_frequency_hz")
    if (
        abs(active_tones[0] - active_tones[1])
        > math.ceil(float(condition["sample_rate_hz"]) / (1 << 16))
        or not isinstance(tone_offset_hz, (int, float))
        or isinstance(tone_offset_hz, bool)
        or not math.isfinite(float(tone_offset_hz))
        or not 0.0 < abs(float(tone_offset_hz)) < 500_000.0
        or abs(float(tone_offset_hz) - actual_tone_offset_hz) > 1e-6
        or not isinstance(emitted_carrier_hz, (int, float))
        or isinstance(emitted_carrier_hz, bool)
        or not math.isfinite(float(emitted_carrier_hz))
        or abs(
            float(emitted_carrier_hz)
            - (float(condition["center_frequency_hz"]) + float(tone_offset_hz))
        )
        > 1e-6
        or abs(abs(float(tone_offset_hz)) - 100_000.0) > 16.0
    ):
        raise ExperimentError("DDS readback or emitted carrier identity is malformed")
    evidence = _mapping(analysis_root.get("artifact_evidence"), "analysis evidence")
    data_file = artifact_root / f"{artifact_id}.sigmf-data"
    meta_file = artifact_root / f"{artifact_id}.sigmf-meta"
    data_sha = sha256_path(data_file)
    meta_sha = sha256_path(meta_file)
    capture_sha = sha256_path(capture_path)
    analysis_sha = sha256_path(analysis_path)
    if (
        capture_artifact.get("sha256") != data_sha
        or evidence.get("data_sha256") != data_sha
        or evidence.get("metadata_sha256") != meta_sha
        or evidence.get("metadata_size_bytes") != meta_file.stat().st_size
        or evidence.get("capture_record_sha256") != capture_sha
    ):
        raise ExperimentError("reanalysis hashes do not attest the finalized artifact")
    quality = _mapping(analysis_root.get("quality_gate"), "quality gate")
    quality_passed = quality.get("passed")
    if not isinstance(quality_passed, bool):
        raise ExperimentError("quality result is not boolean")
    stream_id = evidence.get("stream_id")
    metadata_abi = evidence.get("metadata_abi")
    if not isinstance(stream_id, int) or metadata_abi != 2:
        raise ExperimentError("reanalysis stream or ABI2 identity is malformed")
    identity = {
        "artifact_id": artifact_id,
        "path": str(artifact_root),
        "data_sha256": data_sha,
        "data_size_bytes": data_file.stat().st_size,
        "metadata_sha256": meta_sha,
        "metadata_size_bytes": meta_file.stat().st_size,
        "capture_record_sha256": capture_sha,
        "capture_record_size_bytes": capture_path.stat().st_size,
        "analysis_sha256": analysis_sha,
        "analysis_size_bytes": analysis_path.stat().st_size,
        "stream_id": stream_id,
        "metadata_abi": metadata_abi,
        "implementation_source_commit": condition["implementation_source_commit"],
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "firmware_evidence_sha256": condition["firmware_evidence_sha256"],
        "firmware_bin_sha256": condition["firmware_bin_sha256"],
        "full_flash_readback_sha256": condition["full_flash_readback_sha256"],
        "rf_readback_evidence_sha256": rf_readback_sha256,
        "dds_frequency_readback_hz": list(dds_readback),
        "dds_tone_offset_hz": float(tone_offset_hz),
        "emitted_carrier_frequency_hz": float(emitted_carrier_hz),
    }
    result = {
        "artifact_id": artifact_id,
        "quality_passed": quality_passed,
        "status": "passed" if quality_passed else "rejected",
        "global_rejection_reasons": quality.get("global_rejection_reasons", []),
        "analysis_path": str(analysis_path),
        "center_frequency_hz": key["center_frequency_hz"],
        "dds_tone_offset_hz": float(tone_offset_hz),
        "emitted_carrier_frequency_hz": float(emitted_carrier_hz),
        "round_index": condition["round_index"],
        "round_order": condition["round_order"],
    }
    return identity, result


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    attempts = [item for item in manifest.get("attempts", []) if isinstance(item, Mapping)]
    complete = {
        int(item["plan_index"]): item
        for item in attempts
        if item.get("status") == "complete"
        and item.get("outcome") in {"quality_passed", "quality_rejected"}
    }
    final = [item for item in manifest.get("final_mute_attempts", []) if isinstance(item, Mapping)]
    planned = len(manifest.get("plan", []))
    passed = sum(item.get("outcome") == "quality_passed" for item in complete.values())
    rejected = sum(item.get("outcome") == "quality_rejected" for item in complete.values())
    return {
        "planned_conditions": planned,
        "execution_attempts": len(attempts),
        "completed_conditions": len(complete),
        "remaining_conditions": planned - len(complete),
        "quality_passed": passed,
        "quality_rejected": rejected,
        "execution_failures": sum(item.get("failure_kind") == "execution" for item in attempts),
        "post_mute_failures": sum(
            item.get("failure_kind") == "post_attempt_mute" for item in attempts
        ),
        "fresh_stream_retries": sum(int(item.get("retry", 0)) > 0 for item in attempts),
        "unique_accepted_streams": len(
            {
                item["artifact_identity"]["stream_id"]
                for item in complete.values()
                if isinstance(item.get("artifact_identity"), Mapping)
            }
        ),
        "final_mute_passed": bool(final and final[-1].get("status") == "passed"),
        "distribution_quality_passed": planned > 0 and passed == planned,
    }


def _persist(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _now()
    manifest["summary"] = _summary(manifest)
    _write_manifest(path, manifest)


def _completed_indices(manifest: Mapping[str, Any]) -> set[int]:
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping) or not isinstance(configuration.get("serial"), str):
        raise ExperimentError("manifest configuration serial is malformed")
    serial = str(configuration["serial"])
    result: set[int] = set()
    for raw in manifest.get("attempts", []):
        if not isinstance(raw, Mapping) or raw.get("status") != "complete":
            continue
        if raw.get("outcome") not in {"quality_passed", "quality_rejected"}:
            continue
        index = raw.get("plan_index")
        if not isinstance(index, int) or index in result:
            raise ExperimentError("completed attempt plan indices are malformed or duplicated")
        identity = raw.get("artifact_identity")
        post_mute = raw.get("post_mute")
        if not isinstance(identity, Mapping) or not isinstance(post_mute, Mapping):
            raise ExperimentError("completed attempt lacks immutable identity or mute evidence")
        if not _mute_attestation_passed(post_mute, serial=serial, purpose="post_attempt"):
            raise ExperimentError("completed attempt lacks a passed exact-serial mute")
        result.add(index)
    return result


def _reattest_completed_artifacts(manifest: Mapping[str, Any], *, serial: str, uri: str) -> None:
    plan = manifest.get("plan")
    if not isinstance(plan, list):
        raise ExperimentError("persisted plan is malformed")
    conditions = {
        int(item["plan_index"]): item
        for item in plan
        if isinstance(item, Mapping) and isinstance(item.get("plan_index"), int)
    }
    for raw in manifest.get("attempts", []):
        if (
            not isinstance(raw, Mapping)
            or raw.get("status") != "complete"
            or raw.get("outcome") not in {"quality_passed", "quality_rejected"}
        ):
            continue
        identity = _mapping(raw.get("artifact_identity"), "completed artifact identity")
        condition = conditions[int(raw["plan_index"])]
        observed_identity, observed_quality = _analysis_identity(
            Path(str(identity["path"])), condition, serial=serial, uri=uri
        )
        if dict(identity) != observed_identity or raw.get("quality_result") != observed_quality:
            raise ExperimentError("completed artifact changed since runner acceptance")


def _recover_stale_attempts(manifest: dict[str, Any], manifest_path: Path, *, serial: str) -> None:
    stale = [
        item
        for item in manifest.get("attempts", [])
        if isinstance(item, dict) and item.get("status") == "running"
    ]
    unrecovered_post_mute = [
        item
        for item in manifest.get("attempts", [])
        if isinstance(item, dict)
        and item.get("failure_kind") == "post_attempt_mute"
        and not _mute_attestation_passed(
            item.get("recovery_mute"), serial=serial, purpose="resume_recovery"
        )
    ]
    if not stale and not unrecovered_post_mute:
        return
    recovery = _strict_mute(serial, "resume_recovery")
    recoveries = manifest.setdefault("recovery_mute_attempts", [])
    assert isinstance(recoveries, list)
    recoveries.append(recovery)
    for item in stale:
        item["status"] = "failed"
        item["outcome"] = "execution_failed"
        item["failure_kind"] = "execution"
        item["error"] = "ExperimentError: prior process ended with attempt still running"
        item["post_mute"] = None
        item["completed_at"] = _now()
        item["artifact_identity"] = None
        item["recovered_stale_process"] = True
        item["recovery_mute"] = recovery
    for item in unrecovered_post_mute:
        item["recovery_mute"] = recovery
    _persist(manifest_path, manifest)
    if not _mute_attestation_passed(recovery, serial=serial, purpose="resume_recovery"):
        raise ExperimentError(f"resume recovery mute failed: {recovery['error']}")


def _run_attempt(
    manifest: dict[str, Any],
    manifest_path: Path,
    condition: Mapping[str, Any],
    *,
    repository: Path,
    board_id: str,
    serial: str,
    uri: str,
    timeout_s: int,
) -> tuple[bool, str | None]:
    attempts = manifest["attempts"]
    assert isinstance(attempts, list)
    retry = sum(
        isinstance(item, Mapping) and item.get("plan_index") == condition["plan_index"]
        for item in attempts
    )
    capture_command = list(condition["capture_command"])
    reanalysis_template = list(condition["reanalysis_command_template"])
    attempt: dict[str, Any] = {
        "attempt_id": len(attempts) + 1,
        "retry": retry,
        **condition,
        "started_at": _now(),
        "completed_at": None,
        "status": "running",
        "outcome": None,
        "failure_kind": None,
        "artifact_id": None,
        "artifact_identity": None,
        "quarantined_failures": [],
        "capture": {"status": "pending", "command": capture_command},
        "reanalysis": {"status": "pending", "command_template": reanalysis_template},
        "quality_result": None,
        "post_mute": None,
        "error": None,
    }
    attempts.append(attempt)
    _persist(manifest_path, manifest)
    capture_root = _board_root(board_id) / "pluto-usb-captures"
    before = _artifact_ids(capture_root)
    before_failed = _failed_artifact_ids(capture_root)
    environment = _environment(repository)
    pending_error: BaseException | None = None
    try:
        capture_result = _run_command(
            capture_command, cwd=repository, environment=environment, timeout_s=timeout_s
        )
        capture_result["parsed_output"] = _extract_json_object(str(capture_result["stdout"]))
        quarantined = [
            _quarantined_failure(capture_root, artifact_id)
            for artifact_id in sorted(_failed_artifact_ids(capture_root) - before_failed)
        ]
        artifact_id, source = _fresh_artifact_id(capture_result, before, capture_root)
        capture_result["artifact_source"] = source
        capture_result["accepted"] = (
            not capture_result.get("timed_out") and capture_result.get("return_code") == 0
        )
        capture_result["status"] = "complete"
        capture_result["quarantined_failures"] = quarantined
        attempt["capture"] = capture_result
        attempt["artifact_id"] = artifact_id
        attempt["quarantined_failures"] = quarantined
        _persist(manifest_path, manifest)
        if not capture_result["accepted"]:
            raise ExperimentError(
                f"capture returned {capture_result.get('return_code')}, expected 0"
            )
        if quarantined:
            raise ExperimentError("successful capture also created a quarantined failure")
        if artifact_id is None:
            raise ExperimentError("capture did not identify exactly one fresh artifact")

        command = [
            artifact_id if value == ARTIFACT_TOKEN else value for value in reanalysis_template
        ]
        if ARTIFACT_TOKEN in command:
            raise ExperimentError("reanalysis artifact token was not resolved")
        reanalysis_result = _run_command(
            command, cwd=repository, environment=environment, timeout_s=timeout_s
        )
        reanalysis_result["parsed_output"] = _extract_json_object(str(reanalysis_result["stdout"]))
        reanalysis_result["accepted"] = not reanalysis_result.get(
            "timed_out"
        ) and reanalysis_result.get("return_code") in {0, 2}
        reanalysis_result["status"] = "complete"
        attempt["reanalysis"] = reanalysis_result
        _persist(manifest_path, manifest)
        if not reanalysis_result["accepted"]:
            raise ExperimentError(
                f"reanalysis returned {reanalysis_result.get('return_code')}, expected 0 or 2"
            )
        parsed = reanalysis_result.get("parsed_output")
        if not isinstance(parsed, Mapping) or parsed.get("artifact_id") != artifact_id:
            raise ExperimentError("reanalysis stdout identity differs from fresh artifact")
        identity, quality = _analysis_identity(
            capture_root / artifact_id, condition, serial=serial, uri=uri
        )
        quality_passed = bool(quality["quality_passed"])
        if reanalysis_result.get("return_code") != (0 if quality_passed else 2):
            raise ExperimentError("reanalysis return code disagrees with persisted quality")
        if parsed.get("quality_passed") is not quality_passed:
            raise ExperimentError("reanalysis stdout disagrees with persisted quality")
        if parsed.get("analysis_sha256") != identity["analysis_sha256"]:
            raise ExperimentError("reanalysis stdout hash differs from finalized analysis")
        attempt["artifact_identity"] = identity
        attempt["quality_result"] = quality
        attempt["outcome"] = "quality_passed" if quality_passed else "quality_rejected"
    except BaseException as error:
        pending_error = error
        attempt["failure_kind"] = "execution"
        attempt["outcome"] = "execution_failed"
        attempt["error"] = _error_text(error)
        attempt["artifact_identity"] = None
    finally:
        mute = _strict_mute(serial, "post_attempt")
        attempt["post_mute"] = mute
        attempt["completed_at"] = _now()
        if not _mute_attestation_passed(mute, serial=serial, purpose="post_attempt"):
            pending_error = ExperimentError(f"strict post-attempt mute failed: {mute['error']}")
            attempt["failure_kind"] = "post_attempt_mute"
            attempt["outcome"] = "post_mute_failed"
            attempt["error"] = _error_text(pending_error)
            attempt["artifact_identity"] = None
        attempt["status"] = "complete" if pending_error is None else "failed"
        _persist(manifest_path, manifest)
    return pending_error is None, None if pending_error is None else attempt["failure_kind"]


def _run_experiment(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    repository: Path,
    board_id: str,
    serial: str,
    uri: str,
    timeout_s: int,
    max_attempts: int,
) -> int:
    pending_error: BaseException | None = None
    try:
        _recover_stale_attempts(manifest, manifest_path, serial=serial)
        _reattest_completed_artifacts(manifest, serial=serial, uri=uri)
        complete = _completed_indices(manifest)
        plan = manifest["plan"]
        assert isinstance(plan, list)
        for condition in plan:
            assert isinstance(condition, Mapping)
            index = int(condition["plan_index"])
            if index in complete:
                continue
            prior = sum(
                isinstance(item, Mapping) and item.get("plan_index") == index
                for item in manifest["attempts"]
            )
            while prior < max_attempts:
                succeeded, failure_kind = _run_attempt(
                    manifest,
                    manifest_path,
                    condition,
                    repository=repository,
                    board_id=board_id,
                    serial=serial,
                    uri=uri,
                    timeout_s=timeout_s,
                )
                prior += 1
                if succeeded:
                    complete.add(index)
                    break
                if failure_kind == "post_attempt_mute":
                    raise ExperimentError("post-attempt exact mute failed; refusing retry")
                # Every execution failure reaches here only after exact mute.
                # The next attempt creates a new metadata session/stream/artifact.
            if index not in complete:
                raise ExperimentError(
                    f"condition {index} exhausted {max_attempts} fresh-stream attempts"
                )
    except BaseException as error:
        pending_error = error
    finally:
        final_mute = _strict_mute(serial, "final")
        final_mutes = manifest["final_mute_attempts"]
        assert isinstance(final_mutes, list)
        final_mutes.append(final_mute)
        manifest["final_mute"] = final_mute
        _persist(manifest_path, manifest)
    if not _mute_attestation_passed(final_mute, serial=serial, purpose="final"):
        if pending_error is not None:
            final_mute["prior_error"] = _error_text(pending_error)
            _persist(manifest_path, manifest)
        raise ExperimentError(f"strict final mute failed: {final_mute['error']}")
    if pending_error is not None:
        raise pending_error
    if len(_completed_indices(manifest)) != len(manifest["plan"]):
        raise ExperimentError("run ended without every plan condition completed")
    manifest["status"] = "complete"
    manifest["completed_at"] = _now()
    _persist(manifest_path, manifest)
    print(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "manifest": str(manifest_path),
                "status": "complete",
                "summary": manifest["summary"],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    args = _parser().parse_args()
    signal.signal(signal.SIGTERM, _cooperative_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _cooperative_termination)
    if not args.serial.strip() or not args.uri.startswith("usb:"):
        raise SystemExit("explicit non-empty --serial and exact usb: --uri are required")
    if args.rounds != DEFAULT_ROUNDS:
        raise SystemExit("Hexcal requires exactly three predeclared rounds")
    if args.protocol_v2:
        if args.stimulus_qualification is None or args.gain_qualification is not None:
            raise SystemExit("--protocol-v22 requires only --stimulus-qualification")
        if args.tx_hardware_gain_db is not None:
            raise SystemExit("hexcal-v2.2 derives TX1 gain from its qualification ledger")
        if args.allow_experimental_5g8:
            raise SystemExit("hexcal-v2.2 does not permit the experimental 5.8 GHz band")
    elif args.gain_qualification is None or args.stimulus_qualification is not None:
        raise SystemExit("legacy hexcal-v1 requires only --gain-qualification")
    if not 1 <= args.max_attempts_per_condition <= 10:
        raise SystemExit("max attempts must be within 1..10")
    if not 30 <= args.timeout_s <= 600:
        raise SystemExit("timeout must be within 30..600 seconds")
    repository = Path(__file__).resolve().parents[1]
    try:
        frequencies = _validate_frequencies(
            args.center_frequencies_hz,
            allow_experimental_5g8=args.allow_experimental_5g8,
            defaults=(
                STIMULUS_CENTER_FREQUENCIES_HZ if args.protocol_v2 else DEFAULT_FREQUENCIES_HZ
            ),
        )
        if args.protocol_v2 and frequencies != STIMULUS_CENTER_FREQUENCIES_HZ:
            raise ValueError("hexcal-v2.2 requires the exact frozen five-frequency plan")
        board_id = _validate_identifier(args.board_id, "board ID")
        run_id = _validate_identifier(args.run_id or _new_run_id(), "run ID")
        profile = load_hexcal_profile(args.profile)
        source_commit = _repository_commit_and_require_clean(repository)
        dependency_attestation = attest_pluto_plus_utils_source()
        firmware_evidence = load_hexcal_firmware_evidence(
            args.firmware_evidence,
            expected_board_id=board_id,
            expected_source_commit=source_commit,
            expected_profile=profile,
        )
        qualification_source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        dependency_sha256 = canonical_json_sha256(dependency_attestation)
        gain_qualification: HexcalGainQualification | None = None
        stimulus_qualification: HexcalStimulusQualification | None = None
        if args.protocol_v2:
            assert isinstance(args.stimulus_qualification, Path)
            stimulus_qualification = load_hexcal_stimulus_qualification(
                args.stimulus_qualification,
                expected_board_id=board_id,
                expected_serial=args.serial,
                expected_uri=args.uri,
                expected_source_commit=source_commit,
                expected_source_attestation=qualification_source_attestation,
                expected_profile=profile,
                expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
                expected_pluto_plus_utils_source_attestation_sha256=dependency_sha256,
                expected_center_frequencies_hz=frequencies,
                expected_dds_scale=args.dds_scale,
            )
        else:
            assert isinstance(args.gain_qualification, Path)
            tx_hardware_gain_db = (
                -40.0 if args.tx_hardware_gain_db is None else args.tx_hardware_gain_db
            )
            gain_qualification = load_hexcal_gain_qualification(
                args.gain_qualification,
                expected_board_id=board_id,
                expected_serial=args.serial,
                expected_uri=args.uri,
                expected_source_commit=source_commit,
                expected_source_attestation=qualification_source_attestation,
                expected_profile=profile,
                expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
                expected_pluto_plus_utils_source_attestation_sha256=dependency_sha256,
                expected_center_frequencies_hz=frequencies,
                expected_tx_hardware_gain_db=tx_hardware_gain_db,
                expected_dds_scale=args.dds_scale,
            )
    except (ExperimentError, ValueError) as error:
        raise SystemExit(str(error)) from error
    python = Path(os.path.abspath(args.python.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"capture Python is not executable: {python}")
    if python != DEFAULT_PYTHON or dependency_attestation.get("python_executable") != str(python):
        raise SystemExit(
            "capture/reanalysis Python differs from the attested pinned dependency runtime"
        )
    profile_path = profile.path
    if stimulus_qualification is not None:
        receiver_gain_db = stimulus_qualification.fixed_receiver_gain_db
        tx_hardware_gain_db = stimulus_qualification.selected_tx_hardware_gain_db
        dds_scale = stimulus_qualification.dds_scale
    else:
        assert gain_qualification is not None
        receiver_gain_db = gain_qualification.selected_receiver_gain_db
        tx_hardware_gain_db = (
            -40.0 if args.tx_hardware_gain_db is None else args.tx_hardware_gain_db
        )
        dds_scale = args.dds_scale
    configuration = _configuration(
        rounds=args.rounds,
        max_attempts=args.max_attempts_per_condition,
        board_id=board_id,
        serial=args.serial,
        uri=args.uri,
        python=python,
        profile=profile_path,
        profile_file_sha256=profile.file_sha256,
        profile_contract_sha256=profile.contract_sha256,
        timeout_s=args.timeout_s,
        receiver_gain_db=receiver_gain_db,
        tx_hardware_gain_db=tx_hardware_gain_db,
        dds_scale=dds_scale,
        center_frequencies_hz=frequencies,
        source_commit=source_commit,
        pluto_plus_utils_source_attestation=dependency_attestation,
        firmware_evidence=firmware_evidence,
        gain_qualification=gain_qualification,
        stimulus_qualification=stimulus_qualification,
        allow_experimental_5g8=args.allow_experimental_5g8,
    )
    board_root = _board_root(board_id)
    manifest_path = board_root / "hexcal-distributions" / run_id / "manifest.json"
    with _board_lock(board_root):
        manifest = (
            _load_manifest(manifest_path, configuration, repository)
            if manifest_path.exists()
            else _new_manifest(run_id, configuration, repository)
        )
        # Exact commands, source profile hash, ordering and RF bounds are
        # durable before the first capture is allowed to enable TX1.
        _persist(manifest_path, manifest)
        print(
            json.dumps(
                {"run_id": run_id, "manifest": str(manifest_path), "status": "running"},
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            return _run_experiment(
                manifest,
                manifest_path,
                repository=repository,
                board_id=board_id,
                serial=args.serial,
                uri=args.uri,
                timeout_s=args.timeout_s,
                max_attempts=args.max_attempts_per_condition,
            )
        except KeyboardInterrupt as error:
            manifest["status"] = "interrupted"
            manifest["error"] = _error_text(error)
            _persist(manifest_path, manifest)
            return 130
        except (ExperimentError, OSError, RuntimeError, ValueError) as error:
            manifest["status"] = "failed"
            manifest["error"] = _error_text(error)
            _persist(manifest_path, manifest)
            print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
