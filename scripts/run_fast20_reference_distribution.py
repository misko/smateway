#!/usr/bin/env python3
"""Run a resumable, fail-muted Fast20 OTA-reference paired-TX sweep."""

from __future__ import annotations

import argparse
import fcntl
import json
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

from pluto_plus.bootstrap_firmware import mute_returned_radio

from smateway.rf_policy import (
    EXPERIMENTAL_5G8_CENTER_HZ,
    classify_fast20_center_frequency,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_URI = "usb:1.3.5"
DEFAULT_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
DEFAULT_ROUNDS = 5
DEFAULT_TIMEOUT_S = 180
DEFAULT_RECEIVER_GAIN_DB = 20
DEFAULT_STIMULUS = "phase"
DEFAULT_CENTER_FREQUENCIES_HZ = (2_400_000_000, EXPERIMENTAL_5G8_CENTER_HZ)
SAMPLE_RATE_HZ = 1_000_000
CAPTURE_ACCEPTED_RETURN_CODES = {0, 2, 3}
REANALYSIS_ACCEPTED_RETURN_CODES = {0, 2}
REFERENCE_ANALYSIS_KIND = "fast20_dual_rx_ota_reference_transfer"
REFERENCE_ANALYSIS_FILENAME = "fast20-reference-transfer-v2.json"
ARTIFACT_TOKEN = "{artifact_id}"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ROUND_ORDER_POLICY = (
    "supplied_frequency_order_tx1_then_tx2",
    "reverse_frequency_order_tx2_then_tx1",
    "rotate_frequency_order_alternate_tx_order",
)


class ExperimentError(RuntimeError):
    """A persisted experiment invariant or live command failed."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--run-id", help="reuse an existing ID to resume its manifest")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--receiver-gain-db",
        type=int,
        choices=range(63),
        default=DEFAULT_RECEIVER_GAIN_DB,
        help="common RX1/RX2 tandem-HOLD gain in 0..62 dB",
    )
    parser.add_argument(
        "--stimulus",
        choices=("qualification", "phase"),
        default=DEFAULT_STIMULUS,
    )
    parser.add_argument(
        "--center-frequency-hz",
        action="append",
        type=int,
        dest="center_frequencies_hz",
        metavar="HZ",
        help=(
            "center frequency to sweep; repeat for multiple centers "
            "(default: 2400000000 and 5800000000)"
        ),
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
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _board_root(board_id: str) -> Path:
    return Path.home() / ".local/state/smateway/boards" / board_id


def _validate_center_frequencies(values: Sequence[int] | None) -> tuple[int, ...]:
    frequencies = DEFAULT_CENTER_FREQUENCIES_HZ if values is None else tuple(values)
    if not frequencies:
        raise ValueError("at least one center frequency is required")

    validated: list[int] = []
    for frequency_hz in frequencies:
        if not isinstance(frequency_hz, int) or isinstance(frequency_hz, bool):
            raise ValueError("center frequencies must be integers")
        classify_fast20_center_frequency(
            frequency_hz,
            allow_experimental_5g8=frequency_hz == EXPERIMENTAL_5G8_CENTER_HZ,
        )
        if frequency_hz in validated:
            raise ValueError(f"center frequencies must be unique: {frequency_hz}")
        validated.append(frequency_hz)
    return tuple(validated)


@contextmanager
def _board_lock(board_root: Path) -> Iterator[None]:
    board_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (board_root / ".bench.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _round_condition_order(
    center_frequencies_hz: Sequence[int], round_index: int
) -> tuple[tuple[int, int], ...]:
    """Return a deterministic order with adjacent TX1/TX2 frequency pairs."""

    pattern_index = (round_index - 1) % len(ROUND_ORDER_POLICY)
    frequencies = tuple(center_frequencies_hz)
    if pattern_index == 0:
        ordered_frequencies = frequencies
        tx_orders = ((0, 1),) * len(frequencies)
    elif pattern_index == 1:
        ordered_frequencies = tuple(reversed(frequencies))
        tx_orders = ((1, 0),) * len(frequencies)
    else:
        ordered_frequencies = frequencies[1:] + frequencies[:1]
        tx_orders = tuple((0, 1) if index % 2 == 0 else (1, 0) for index in range(len(frequencies)))
    return tuple(
        (frequency_hz, tx_channel)
        for frequency_hz, tx_order in zip(ordered_frequencies, tx_orders, strict=True)
        for tx_channel in tx_order
    )


def _condition_plan(
    rounds: int,
    center_frequencies_hz: Sequence[int],
) -> list[dict[str, int | str]]:
    plan: list[dict[str, int | str]] = []
    for round_index in range(1, rounds + 1):
        order = _round_condition_order(center_frequencies_hz, round_index)
        for condition_index, (frequency_hz, tx_channel) in enumerate(order, start=1):
            plan.append(
                {
                    "plan_index": len(plan),
                    "round": round_index,
                    "round_order_pattern": ((round_index - 1) % len(ROUND_ORDER_POLICY)) + 1,
                    "condition_index": condition_index,
                    "center_frequency_hz": frequency_hz,
                    "tx_channel": tx_channel,
                    "tx_name": f"TX{tx_channel + 1}",
                }
            )
    return plan


def _capture_command(
    python: Path,
    repository: Path,
    condition: Mapping[str, Any],
    *,
    board_id: str,
    serial: str,
    uri: str,
    receiver_gain_db: int,
    stimulus: str,
) -> list[str]:
    frequency_hz = int(condition["center_frequency_hz"])
    command = [
        str(python),
        str(repository / "scripts/capture_fast20_dwell.py"),
        "--tx-channel",
        str(condition["tx_channel"]),
        "--stimulus",
        stimulus,
        "--receiver-gain-db",
        str(receiver_gain_db),
        "--sample-rate-hz",
        str(SAMPLE_RATE_HZ),
        "--center-frequency-hz",
        str(frequency_hz),
        "--board-id",
        board_id,
        "--serial",
        serial,
        "--uri",
        uri,
    ]
    if frequency_hz == EXPERIMENTAL_5G8_CENTER_HZ:
        command.append("--allow-experimental-5g8")
    return command


def _reanalyze_command(
    python: Path,
    repository: Path,
    artifact_id: str,
    board_id: str,
) -> list[str]:
    return [
        str(python),
        str(repository / "scripts/reanalyze_fast20_reference_transfer_artifact.py"),
        artifact_id,
        "--board-id",
        board_id,
    ]


def _configuration(
    *,
    rounds: int,
    board_id: str,
    serial: str,
    uri: str,
    python: Path,
    timeout_s: int,
    receiver_gain_db: int,
    stimulus: str,
    center_frequencies_hz: Sequence[int],
) -> dict[str, Any]:
    if not isinstance(receiver_gain_db, int) or isinstance(receiver_gain_db, bool):
        raise ValueError("receiver gain must be an integer")
    if not 0 <= receiver_gain_db <= 62:
        raise ValueError("receiver gain must be within 0..62 dB")
    if stimulus not in {"qualification", "phase"}:
        raise ValueError("stimulus must be qualification or phase")
    frequencies = tuple(center_frequencies_hz)
    return {
        "rounds": rounds,
        "center_frequencies_hz": list(frequencies),
        "round_order_policy": list(ROUND_ORDER_POLICY),
        "receiver_gain_db": receiver_gain_db,
        "stimulus": stimulus,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "python": str(python),
        "timeout_s": timeout_s,
        "capture_program": "scripts/capture_fast20_dwell.py",
        "reference_reanalysis_program": ("scripts/reanalyze_fast20_reference_transfer_artifact.py"),
        "reference_analysis_kind": REFERENCE_ANALYSIS_KIND,
    }


def _execution_plan(
    configuration: Mapping[str, Any],
    repository: Path,
) -> list[dict[str, Any]]:
    rounds = int(configuration["rounds"])
    frequencies = tuple(int(value) for value in configuration["center_frequencies_hz"])
    python = Path(str(configuration["python"]))
    plan: list[dict[str, Any]] = []
    for base in _condition_plan(rounds, frequencies):
        condition: dict[str, Any] = dict(base)
        condition["receiver_gain_db"] = int(configuration["receiver_gain_db"])
        condition["stimulus"] = str(configuration["stimulus"])
        condition["sample_rate_hz"] = SAMPLE_RATE_HZ
        condition["capture_command"] = _capture_command(
            python,
            repository,
            condition,
            board_id=str(configuration["board_id"]),
            serial=str(configuration["serial"]),
            uri=str(configuration["uri"]),
            receiver_gain_db=int(configuration["receiver_gain_db"]),
            stimulus=str(configuration["stimulus"]),
        )
        condition["reference_reanalysis_command_template"] = _reanalyze_command(
            python,
            repository,
            ARTIFACT_TOKEN,
            str(configuration["board_id"]),
        )
        plan.append(condition)
    return plan


def _completed_attempt_is_valid(
    attempt: Mapping[str, Any],
    condition: Mapping[str, Any],
) -> bool:
    for key, value in condition.items():
        if attempt.get(key) != value:
            return False
    outcome = attempt.get("outcome")
    if (
        attempt.get("status") != "complete"
        or outcome not in {"quality_passed", "quality_rejected"}
        or attempt.get("failure_kind") is not None
        or attempt.get("error") is not None
    ):
        return False
    post_mute = attempt.get("post_mute")
    if not isinstance(post_mute, dict) or post_mute.get("status") != "passed":
        return False
    artifact_id = attempt.get("artifact_id")
    if not isinstance(artifact_id, str) or ARTIFACT_ID.fullmatch(artifact_id) is None:
        return False
    capture = attempt.get("capture")
    if (
        not isinstance(capture, dict)
        or capture.get("status") != "complete"
        or capture.get("accepted") is not True
        or capture.get("timed_out") is not False
        or capture.get("return_code") not in CAPTURE_ACCEPTED_RETURN_CODES
        or capture.get("command") != condition.get("capture_command")
    ):
        return False
    reanalysis = attempt.get("reanalysis")
    template = condition.get("reference_reanalysis_command_template")
    if not isinstance(template, list):
        return False
    expected_reanalysis = [artifact_id if item == ARTIFACT_TOKEN else item for item in template]
    expected_quality = outcome == "quality_passed"
    if (
        not isinstance(reanalysis, dict)
        or reanalysis.get("status") != "complete"
        or reanalysis.get("accepted") is not True
        or reanalysis.get("timed_out") is not False
        or reanalysis.get("return_code") != (0 if expected_quality else 2)
        or reanalysis.get("command") != expected_reanalysis
    ):
        return False
    parsed = reanalysis.get("parsed_output")
    if (
        not isinstance(parsed, dict)
        or parsed.get("artifact_id") != artifact_id
        or parsed.get("quality_passed") is not expected_quality
    ):
        return False
    artifact = attempt.get("artifact_identity")
    if not isinstance(artifact, dict) or artifact.get("artifact_id") != artifact_id:
        return False
    sha256 = artifact.get("sha256")
    artifact_path = artifact.get("path")
    if (
        not isinstance(sha256, str)
        or SHA256.fullmatch(sha256) is None
        or not isinstance(artifact_path, str)
        or Path(artifact_path).name != artifact_id
    ):
        return False
    quality = attempt.get("quality_result")
    if not isinstance(quality, dict):
        return False
    return (
        quality.get("artifact_id") == artifact_id
        and quality.get("quality_passed") is expected_quality
        and quality.get("status") == ("passed" if expected_quality else "rejected")
        and quality.get("tx_channel") == condition.get("tx_channel")
        and quality.get("center_frequency_hz") == condition.get("center_frequency_hz")
        and quality.get("receiver_gain_db") == condition.get("receiver_gain_db")
    )


def _validate_attempt_history(document: Mapping[str, Any]) -> None:
    plan = document.get("plan")
    attempts = document.get("attempts")
    if not isinstance(plan, list) or not isinstance(attempts, list):
        raise ExperimentError("resume manifest plan or attempts are malformed")
    conditions = {
        int(item["plan_index"]): item
        for item in plan
        if isinstance(item, dict) and isinstance(item.get("plan_index"), int)
    }
    if len(conditions) != len(plan):
        raise ExperimentError("resume manifest plan indices are malformed")
    retry_counts: dict[int, int] = {}
    completed: set[int] = set()
    for attempt_id, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict) or attempt.get("attempt_id") != attempt_id:
            raise ExperimentError("resume manifest attempt IDs are malformed")
        plan_index = attempt.get("plan_index")
        if not isinstance(plan_index, int) or plan_index not in conditions:
            raise ExperimentError("resume manifest attempt references an unknown plan condition")
        retry = retry_counts.get(plan_index, 0)
        if attempt.get("retry") != retry:
            raise ExperimentError("resume manifest retry counters are malformed")
        retry_counts[plan_index] = retry + 1
        condition = conditions[plan_index]
        if any(attempt.get(key) != value for key, value in condition.items()):
            raise ExperimentError("resume manifest attempt differs from its persisted plan")
        status = attempt.get("status")
        if status == "complete":
            if plan_index in completed:
                raise ExperimentError("resume manifest has duplicate completed conditions")
            if not _completed_attempt_is_valid(attempt, condition):
                raise ExperimentError("resume manifest completed attempt is not fully attested")
            completed.add(plan_index)
        elif status not in {"running", "failed"}:
            raise ExperimentError("resume manifest attempt status is unsupported")


def _new_manifest(
    run_id: str,
    configuration: Mapping[str, Any],
    repository: Path,
) -> dict[str, Any]:
    created_at = _now()
    return {
        "schema": 1,
        "experiment_kind": "fast20_ota_reference_distribution",
        "run_id": run_id,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "running",
        "configuration": dict(configuration),
        "plan": _execution_plan(configuration, repository),
        "attempts": [],
        "final_mute_attempts": [],
        "resume_count": 0,
        "summary": {},
    }


def _load_manifest(
    path: Path,
    configuration: Mapping[str, Any],
    repository: Path,
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"cannot load resume manifest: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ExperimentError("resume manifest has an unsupported schema")
    if document.get("experiment_kind") != "fast20_ota_reference_distribution":
        raise ExperimentError("resume manifest is for another experiment")
    if document.get("configuration") != dict(configuration):
        raise ExperimentError("resume arguments do not match the persisted configuration")
    if document.get("plan") != _execution_plan(configuration, repository):
        raise ExperimentError("resume manifest execution plan changed")
    attempts = document.get("attempts")
    if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
        raise ExperimentError("resume manifest attempts are malformed")
    final_mutes = document.get("final_mute_attempts")
    if not isinstance(final_mutes, list) or not all(isinstance(item, dict) for item in final_mutes):
        raise ExperimentError("resume manifest final mute attempts are malformed")
    _validate_attempt_history(document)
    document["resume_count"] = int(document.get("resume_count", 0)) + 1
    document["status"] = "running"
    document["updated_at"] = _now()
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
    started_at = _now()
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
            "started_at": started_at,
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
        "started_at": started_at,
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


def _artifact_from_capture(
    capture_result: Mapping[str, Any],
    before: set[str],
    capture_root: Path,
) -> tuple[str | None, str | None]:
    parsed = capture_result.get("parsed_output")
    if isinstance(parsed, dict):
        candidate = parsed.get("artifact_id")
        if candidate is not None:
            if (
                isinstance(candidate, str)
                and ARTIFACT_ID.fullmatch(candidate)
                and candidate not in before
                and (capture_root / candidate).is_dir()
            ):
                return candidate, "stdout"
            return None, None
    created = sorted(_artifact_ids(capture_root) - before)
    if len(created) == 1:
        return created[0], "directory_diff"
    return None, None


def _command_environment(repository: Path) -> dict[str, str]:
    environment = dict(os.environ)
    source = str(repository / "src")
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not prior else f"{source}{os.pathsep}{prior}"
    return environment


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentError(f"{label} is not an object")
    return value


def _reference_quality_result(
    artifact_root: Path,
    condition: Mapping[str, Any],
    *,
    receiver_gain_db: int,
) -> dict[str, Any]:
    path = artifact_root / REFERENCE_ANALYSIS_FILENAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"cannot load reference reanalysis: {error}") from error
    root = _mapping(document, "reference reanalysis")
    if root.get("schema") != 1 or root.get("analysis_kind") != REFERENCE_ANALYSIS_KIND:
        raise ExperimentError("reanalysis is not the OTA-reference transfer schema")
    artifact = _mapping(root.get("artifact"), "reference artifact")
    artifact_id = artifact_root.name
    if artifact.get("artifact_id") != artifact_id or artifact.get("path") != str(artifact_root):
        raise ExperimentError("reference reanalysis artifact identity differs from capture")
    artifact_sha256 = artifact.get("sha256")
    if not isinstance(artifact_sha256, str) or SHA256.fullmatch(artifact_sha256) is None:
        raise ExperimentError("reference reanalysis artifact SHA-256 is malformed")
    aggregation = _mapping(root.get("aggregation_key"), "reference aggregation key")
    expected_tx = int(condition["tx_channel"])
    expected_frequency = int(condition["center_frequency_hz"])
    if aggregation.get("artifact_id") != artifact_id:
        raise ExperimentError("reference aggregation artifact differs from capture")
    if aggregation.get("tx_channel") != expected_tx:
        raise ExperimentError("reference aggregation TX channel differs from plan")
    if aggregation.get("center_frequency_hz") != expected_frequency:
        raise ExperimentError("reference aggregation center frequency differs from plan")
    if aggregation.get("receiver_gain_db") != receiver_gain_db:
        raise ExperimentError("reference aggregation receiver gain differs from plan")
    if aggregation.get("sample_rate_hz") != condition.get("sample_rate_hz"):
        raise ExperimentError("reference aggregation sample rate differs from plan")
    carrier_frequency_hz = aggregation.get("carrier_frequency_hz")
    if (
        not isinstance(carrier_frequency_hz, (int, float))
        or isinstance(carrier_frequency_hz, bool)
        or carrier_frequency_hz <= 0
    ):
        raise ExperimentError("reference aggregation carrier frequency is malformed")
    quality = _mapping(root.get("quality_gate"), "reference quality gate")
    quality_passed = quality.get("passed")
    if not isinstance(quality_passed, bool):
        raise ExperimentError("reference quality result is not boolean")
    transfer = _mapping(root.get("transfer"), "reference transfer")
    states = transfer.get("states")
    if not isinstance(states, list) or len(states) != 8:
        raise ExperimentError("reference transfer does not contain eight states")
    rejected_states = []
    observed_state_names = []
    for state in states:
        state_mapping = _mapping(state, "reference state")
        name = state_mapping.get("name")
        state_passed = state_mapping.get("quality_passed")
        if not isinstance(name, str) or not isinstance(state_passed, bool):
            raise ExperimentError("reference state quality is malformed")
        observed_state_names.append(name)
        if not state_passed:
            rejected_states.append(name)
    if observed_state_names != [f"ANT{index}" for index in range(1, 9)]:
        raise ExperimentError("reference transfer state order is not ANT1 through ANT8")
    if quality_passed and rejected_states:
        raise ExperimentError("passing reference quality contains rejected states")
    return {
        "status": "passed" if quality_passed else "rejected",
        "quality_passed": quality_passed,
        "global_rejection_reasons": quality.get("global_rejection_reasons", []),
        "rejected_states": rejected_states,
        "analysis_path": str(path),
        "analysis_kind": REFERENCE_ANALYSIS_KIND,
        "artifact_id": artifact_id,
        "artifact_path": str(artifact_root),
        "artifact_sha256": artifact_sha256,
        "tx_channel": expected_tx,
        "center_frequency_hz": expected_frequency,
        "carrier_frequency_hz": carrier_frequency_hz,
        "receiver_gain_db": receiver_gain_db,
    }


def _persist_progress(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _now()
    manifest["summary"] = _summary(manifest)
    _write_manifest(path, manifest)


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    attempts = manifest.get("attempts", [])
    records = [item for item in attempts if isinstance(item, dict)]
    complete = [
        item
        for item in records
        if item.get("status") == "complete"
        and item.get("outcome") in {"quality_passed", "quality_rejected"}
    ]
    latest_complete = {int(item["plan_index"]): item for item in complete}
    quality_passed = sum(
        item.get("outcome") == "quality_passed" for item in latest_complete.values()
    )
    quality_rejected = sum(
        item.get("outcome") == "quality_rejected" for item in latest_complete.values()
    )
    final_mutes = manifest.get("final_mute_attempts", [])
    final_records = [item for item in final_mutes if isinstance(item, dict)]
    final_mute_passed = bool(final_records and final_records[-1].get("status") == "passed")
    planned = len(manifest.get("plan", []))
    completed_count = len(latest_complete)
    distribution_quality_passed = (
        planned > 0 and completed_count == planned and quality_passed == planned
    )
    return {
        "planned_conditions": planned,
        "execution_attempts": len(records),
        "completed_conditions": completed_count,
        "remaining_conditions": planned - completed_count,
        "quality_passed": quality_passed,
        "quality_rejected": quality_rejected,
        "failed_attempts": sum(item.get("status") == "failed" for item in records),
        "execution_failures": sum(item.get("failure_kind") == "execution" for item in records),
        "post_mute_failures": sum(
            item.get("failure_kind") == "post_attempt_mute" for item in records
        ),
        "final_mute_attempts": len(final_records),
        "final_mute_passed": final_mute_passed,
        "distribution_quality_passed": distribution_quality_passed,
    }


def _completed_plan_indices(manifest: Mapping[str, Any]) -> set[int]:
    plan = manifest.get("plan", [])
    conditions = {
        int(item["plan_index"]): item
        for item in plan
        if isinstance(item, dict) and isinstance(item.get("plan_index"), int)
    }
    attempts = manifest.get("attempts", [])
    completed: set[int] = set()
    for item in attempts:
        if not isinstance(item, dict) or item.get("status") != "complete":
            continue
        plan_index = item.get("plan_index")
        if not isinstance(plan_index, int):
            raise ExperimentError("completed attempt has a malformed plan index")
        condition = conditions.get(plan_index)
        if condition is None or not _completed_attempt_is_valid(item, condition):
            raise ExperimentError("completed attempt is not fully bound to the persisted plan")
        if plan_index in completed:
            raise ExperimentError("multiple completed attempts exist for one plan condition")
        completed.add(plan_index)
    return completed


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
            "error": _error_text(error),
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


def _planned_command(condition: Mapping[str, Any], key: str) -> list[str]:
    value = condition.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ExperimentError(f"persisted {key} is malformed")
    return list(value)


def _run_attempt(
    manifest: dict[str, Any],
    manifest_path: Path,
    condition: Mapping[str, Any],
    *,
    repository: Path,
    board_id: str,
    serial: str,
    timeout_s: int,
    receiver_gain_db: int,
) -> None:
    attempts = manifest["attempts"]
    assert isinstance(attempts, list)
    prior_retries = sum(
        isinstance(item, dict) and item.get("plan_index") == condition["plan_index"]
        for item in attempts
    )
    capture_command = _planned_command(condition, "capture_command")
    reanalysis_template = _planned_command(condition, "reference_reanalysis_command_template")
    attempt: dict[str, Any] = {
        "attempt_id": len(attempts) + 1,
        "retry": prior_retries,
        **condition,
        "started_at": _now(),
        "completed_at": None,
        "status": "running",
        "outcome": None,
        "failure_kind": None,
        "artifact_id": None,
        "artifact_identity": None,
        "capture": {"status": "pending", "command": capture_command},
        "reanalysis": {
            "status": "pending",
            "command_template": reanalysis_template,
        },
        "quality_result": None,
        "post_mute": None,
        "error": None,
    }
    attempts.append(attempt)
    _persist_progress(manifest_path, manifest)

    capture_root = _board_root(board_id) / "pluto-usb-captures"
    before = _artifact_ids(capture_root)
    environment = _command_environment(repository)
    pending_error: BaseException | None = None
    try:
        capture = _run_command(
            capture_command,
            cwd=repository,
            environment=environment,
            timeout_s=timeout_s,
        )
        capture["parsed_output"] = _extract_json_object(str(capture["stdout"]))
        artifact_id, artifact_source = _artifact_from_capture(capture, before, capture_root)
        capture["accepted"] = (
            not capture.get("timed_out")
            and capture.get("return_code") in CAPTURE_ACCEPTED_RETURN_CODES
        )
        capture["artifact_source"] = artifact_source
        capture["status"] = "complete"
        attempt["capture"] = capture
        attempt["artifact_id"] = artifact_id
        _persist_progress(manifest_path, manifest)

        if not capture["accepted"]:
            raise ExperimentError(
                f"capture returned {capture.get('return_code')}, expected 0, 2 or 3"
            )
        if artifact_id is None:
            raise ExperimentError("accepted capture did not identify exactly one artifact")

        reanalysis_command = [
            artifact_id if item == ARTIFACT_TOKEN else item for item in reanalysis_template
        ]
        if ARTIFACT_TOKEN in reanalysis_command:
            raise ExperimentError("reference reanalysis artifact token was not resolved")
        reanalysis = _run_command(
            reanalysis_command,
            cwd=repository,
            environment=environment,
            timeout_s=timeout_s,
        )
        reanalysis["parsed_output"] = _extract_json_object(str(reanalysis["stdout"]))
        reanalysis["accepted"] = (
            not reanalysis.get("timed_out")
            and reanalysis.get("return_code") in REANALYSIS_ACCEPTED_RETURN_CODES
        )
        reanalysis["status"] = "complete"
        attempt["reanalysis"] = reanalysis
        _persist_progress(manifest_path, manifest)
        if not reanalysis["accepted"]:
            raise ExperimentError(
                f"reference reanalysis returned {reanalysis.get('return_code')}, expected 0 or 2"
            )
        parsed = reanalysis.get("parsed_output")
        if not isinstance(parsed, dict) or parsed.get("artifact_id") != artifact_id:
            raise ExperimentError("reference reanalysis output did not match capture artifact")

        quality_result = _reference_quality_result(
            capture_root / artifact_id,
            condition,
            receiver_gain_db=receiver_gain_db,
        )
        quality_passed = bool(quality_result["quality_passed"])
        expected_return_code = 0 if quality_passed else 2
        if reanalysis.get("return_code") != expected_return_code:
            raise ExperimentError("reference quality and reanalysis return code disagree")
        if parsed.get("quality_passed") is not quality_passed:
            raise ExperimentError("reference quality and reanalysis stdout disagree")
        attempt["quality_result"] = quality_result
        attempt["artifact_identity"] = {
            "artifact_id": quality_result["artifact_id"],
            "path": quality_result["artifact_path"],
            "sha256": quality_result["artifact_sha256"],
        }
        attempt["outcome"] = "quality_passed" if quality_passed else "quality_rejected"
        _persist_progress(manifest_path, manifest)
    except BaseException as error:
        attempt["error"] = _error_text(error)
        attempt["failure_kind"] = "execution"
        attempt["outcome"] = "execution_failed"
        pending_error = error
    finally:
        attempt["post_mute"] = _strict_mute(serial, "post_attempt")
        attempt["completed_at"] = _now()
        post_mute = attempt["post_mute"]
        assert isinstance(post_mute, dict)
        if post_mute["status"] != "passed":
            mute_error = ExperimentError(f"strict post-attempt mute failed: {post_mute['error']}")
            attempt["error"] = _error_text(mute_error)
            attempt["failure_kind"] = "post_attempt_mute"
            attempt["outcome"] = "post_mute_failed"
            pending_error = mute_error
        attempt["status"] = "complete" if pending_error is None else "failed"
        _persist_progress(manifest_path, manifest)
    if pending_error is not None:
        raise pending_error


def _run_experiment(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    repository: Path,
    board_id: str,
    serial: str,
    timeout_s: int,
    receiver_gain_db: int,
) -> int:
    pending_error: BaseException | None = None
    try:
        completed = _completed_plan_indices(manifest)
        plan = manifest["plan"]
        assert isinstance(plan, list)
        for condition in plan:
            assert isinstance(condition, dict)
            plan_index = int(condition["plan_index"])
            if plan_index in completed:
                continue
            _run_attempt(
                manifest,
                manifest_path,
                condition,
                repository=repository,
                board_id=board_id,
                serial=serial,
                timeout_s=timeout_s,
                receiver_gain_db=receiver_gain_db,
            )
            completed.add(plan_index)
    except BaseException as error:
        pending_error = error
    finally:
        final_mute = _strict_mute(serial, "final")
        final_mutes = manifest["final_mute_attempts"]
        assert isinstance(final_mutes, list)
        final_mutes.append(final_mute)
        manifest["final_mute"] = final_mute
        _persist_progress(manifest_path, manifest)

    if final_mute["status"] != "passed":
        if pending_error is not None:
            final_mute["prior_error"] = _error_text(pending_error)
            _persist_progress(manifest_path, manifest)
        raise ExperimentError(f"strict final mute failed: {final_mute['error']}")
    if pending_error is not None:
        raise pending_error
    if len(_completed_plan_indices(manifest)) != len(manifest["plan"]):
        raise ExperimentError("experiment ended before every planned condition completed")

    manifest["status"] = "complete"
    manifest["completed_at"] = _now()
    _persist_progress(manifest_path, manifest)
    print(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "manifest": str(manifest_path),
                "status": manifest["status"],
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
    if not 1 <= args.rounds <= 20:
        raise SystemExit("rounds must be within 1..20")
    if not 30 <= args.timeout_s <= 600:
        raise SystemExit("timeout must be within 30..600 seconds")
    try:
        center_frequencies_hz = _validate_center_frequencies(args.center_frequencies_hz)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    board_id = _validate_identifier(args.board_id, "board ID")
    run_id = _validate_identifier(args.run_id or _new_run_id(), "run ID")
    python = Path(os.path.abspath(args.python.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"capture Python is not executable: {python}")
    repository = Path(__file__).resolve().parents[1]
    board_root = _board_root(board_id)
    manifest_path = board_root / "reference-distributions" / run_id / "manifest.json"
    configuration = _configuration(
        rounds=args.rounds,
        board_id=board_id,
        serial=args.serial,
        uri=args.uri,
        python=python,
        timeout_s=args.timeout_s,
        receiver_gain_db=args.receiver_gain_db,
        stimulus=args.stimulus,
        center_frequencies_hz=center_frequencies_hz,
    )

    with _board_lock(board_root):
        manifest = (
            _load_manifest(manifest_path, configuration, repository)
            if manifest_path.exists()
            else _new_manifest(run_id, configuration, repository)
        )
        # The complete deterministic plan and exact command arrays are durable
        # before the first condition is permitted to emit RF.
        _persist_progress(manifest_path, manifest)
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
                timeout_s=args.timeout_s,
                receiver_gain_db=args.receiver_gain_db,
            )
        except KeyboardInterrupt as error:
            manifest["status"] = "interrupted"
            manifest["error"] = _error_text(error)
            _persist_progress(manifest_path, manifest)
            return 130
        except (ExperimentError, OSError, RuntimeError, ValueError) as error:
            manifest["status"] = "failed"
            manifest["error"] = _error_text(error)
            _persist_progress(manifest_path, manifest)
            print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
