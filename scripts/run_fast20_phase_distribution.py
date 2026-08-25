#!/usr/bin/env python3
"""Run a resumable, fail-muted Fast20 dual-band phase distribution sweep."""

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

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_URI = "usb:1.3.5"
DEFAULT_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
DEFAULT_ROUNDS = 5
DEFAULT_TIMEOUT_S = 180
CAPTURE_ACCEPTED_RETURN_CODES = {0, 2, 3}
REANALYSIS_ACCEPTED_RETURN_CODES = {0, 2}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
CONDITION_ORDER = (
    (2_400_000_000, 0),
    (2_400_000_000, 1),
    (5_800_000_000, 0),
    (5_800_000_000, 1),
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


def _plan(rounds: int) -> list[dict[str, int | str]]:
    plan: list[dict[str, int | str]] = []
    for round_index in range(1, rounds + 1):
        for condition_index, (frequency_hz, tx_channel) in enumerate(CONDITION_ORDER, start=1):
            plan.append(
                {
                    "plan_index": len(plan),
                    "round": round_index,
                    "condition_index": condition_index,
                    "center_frequency_hz": frequency_hz,
                    "tx_channel": tx_channel,
                    "tx_name": f"TX{tx_channel + 1}",
                }
            )
    return plan


def _configuration(
    *, rounds: int, board_id: str, serial: str, uri: str, python: Path, timeout_s: int
) -> dict[str, Any]:
    return {
        "rounds": rounds,
        "condition_order": [
            {"center_frequency_hz": frequency, "tx_channel": tx}
            for frequency, tx in CONDITION_ORDER
        ],
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "python": str(python),
        "timeout_s": timeout_s,
    }


def _new_manifest(run_id: str, configuration: Mapping[str, Any]) -> dict[str, Any]:
    created_at = _now()
    rounds = int(configuration["rounds"])
    return {
        "schema": 1,
        "experiment_kind": "fast20_phase_distribution",
        "run_id": run_id,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "running",
        "configuration": dict(configuration),
        "plan": _plan(rounds),
        "attempts": [],
        "resume_count": 0,
        "summary": {},
    }


def _load_manifest(path: Path, configuration: Mapping[str, Any]) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"cannot load resume manifest: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ExperimentError("resume manifest has an unsupported schema")
    if document.get("experiment_kind") != "fast20_phase_distribution":
        raise ExperimentError("resume manifest is for another experiment")
    if document.get("configuration") != dict(configuration):
        raise ExperimentError("resume arguments do not match the persisted configuration")
    if document.get("plan") != _plan(int(configuration["rounds"])):
        raise ExperimentError("resume manifest condition order changed")
    attempts = document.get("attempts")
    if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
        raise ExperimentError("resume manifest attempts are malformed")
    document["resume_count"] = int(document.get("resume_count", 0)) + 1
    document["status"] = "running"
    document["updated_at"] = _now()
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
    command: Sequence[str], *, cwd: Path, environment: Mapping[str, str], timeout_s: int
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
    capture_result: Mapping[str, Any], before: set[str], capture_root: Path
) -> tuple[str | None, str | None]:
    parsed = capture_result.get("parsed_output")
    if isinstance(parsed, dict):
        candidate = parsed.get("artifact_id")
        if (
            isinstance(candidate, str)
            and ARTIFACT_ID.fullmatch(candidate)
            and (capture_root / candidate).is_dir()
        ):
            return candidate, "stdout"
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


def _capture_command(
    python: Path,
    repository: Path,
    condition: Mapping[str, Any],
    *,
    board_id: str,
    serial: str,
    uri: str,
) -> list[str]:
    frequency_hz = int(condition["center_frequency_hz"])
    command = [
        str(python),
        str(repository / "scripts/capture_fast20_dwell.py"),
        "--tx-channel",
        str(condition["tx_channel"]),
        "--stimulus",
        "phase",
        "--center-frequency-hz",
        str(frequency_hz),
        "--board-id",
        board_id,
        "--serial",
        serial,
        "--uri",
        uri,
    ]
    if frequency_hz == 5_800_000_000:
        command.append("--allow-experimental-5g8")
    return command


def _reanalyze_command(
    python: Path, repository: Path, artifact_id: str, board_id: str
) -> list[str]:
    return [
        str(python),
        str(repository / "scripts/reanalyze_fast20_phase_artifact.py"),
        artifact_id,
        "--board-id",
        board_id,
    ]


def _persist_progress(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _now()
    manifest["summary"] = _summary(manifest)
    _write_manifest(path, manifest)


def _summary(manifest: Mapping[str, Any]) -> dict[str, int]:
    attempts = manifest.get("attempts", [])
    records = [item for item in attempts if isinstance(item, dict)]
    complete = [item for item in records if item.get("status") == "complete"]
    latest_complete = {int(item["plan_index"]): item for item in complete}
    quality_passed = 0
    quality_rejected = 0
    for item in latest_complete.values():
        reanalysis = item.get("reanalysis")
        parsed = reanalysis.get("parsed_output") if isinstance(reanalysis, dict) else None
        if isinstance(parsed, dict) and parsed.get("quality_passed") is True:
            quality_passed += 1
        else:
            quality_rejected += 1
    return {
        "planned_conditions": len(manifest.get("plan", [])),
        "execution_attempts": len(records),
        "completed_conditions": len(latest_complete),
        "quality_passed": quality_passed,
        "quality_rejected": quality_rejected,
        "failed_attempts": sum(item.get("status") == "failed" for item in records),
    }


def _completed_plan_indices(manifest: Mapping[str, Any]) -> set[int]:
    attempts = manifest.get("attempts", [])
    return {
        int(item["plan_index"])
        for item in attempts
        if isinstance(item, dict)
        and item.get("status") == "complete"
        and isinstance(item.get("post_mute"), dict)
        and item["post_mute"].get("status") == "passed"
    }


def _strict_post_mute(serial: str) -> dict[str, Any]:
    started_at = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_text(error),
        }
    return {
        "status": "passed",
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _run_attempt(
    manifest: dict[str, Any],
    manifest_path: Path,
    condition: Mapping[str, Any],
    *,
    repository: Path,
    python: Path,
    board_id: str,
    serial: str,
    uri: str,
    timeout_s: int,
) -> None:
    attempts = manifest["attempts"]
    assert isinstance(attempts, list)
    prior_retries = sum(
        isinstance(item, dict) and item.get("plan_index") == condition["plan_index"]
        for item in attempts
    )
    attempt: dict[str, Any] = {
        "attempt_id": len(attempts) + 1,
        "retry": prior_retries,
        **condition,
        "started_at": _now(),
        "completed_at": None,
        "status": "running",
        "artifact_id": None,
        "capture": None,
        "reanalysis": None,
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
            _capture_command(
                python,
                repository,
                condition,
                board_id=board_id,
                serial=serial,
                uri=uri,
            ),
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
        attempt["capture"] = capture
        attempt["artifact_id"] = artifact_id
        _persist_progress(manifest_path, manifest)

        if not capture["accepted"]:
            raise ExperimentError(
                f"capture returned {capture.get('return_code')}, expected 0, 2 or 3"
            )
        if artifact_id is None:
            raise ExperimentError("accepted capture did not identify exactly one artifact")

        reanalysis = _run_command(
            _reanalyze_command(python, repository, artifact_id, board_id),
            cwd=repository,
            environment=environment,
            timeout_s=timeout_s,
        )
        reanalysis["parsed_output"] = _extract_json_object(str(reanalysis["stdout"]))
        reanalysis["accepted"] = (
            not reanalysis.get("timed_out")
            and reanalysis.get("return_code") in REANALYSIS_ACCEPTED_RETURN_CODES
        )
        attempt["reanalysis"] = reanalysis
        _persist_progress(manifest_path, manifest)
        if not reanalysis["accepted"]:
            raise ExperimentError(
                f"phase reanalysis returned {reanalysis.get('return_code')}, expected 0 or 2"
            )
        parsed = reanalysis.get("parsed_output")
        if not isinstance(parsed, dict) or parsed.get("artifact_id") != artifact_id:
            raise ExperimentError("phase reanalysis output did not match the capture artifact")
    except BaseException as error:
        attempt["error"] = _error_text(error)
        pending_error = error
    finally:
        attempt["post_mute"] = _strict_post_mute(serial)
        attempt["completed_at"] = _now()
        post_mute = attempt["post_mute"]
        assert isinstance(post_mute, dict)
        if post_mute["status"] != "passed":
            mute_error = ExperimentError(f"strict post-attempt mute failed: {post_mute['error']}")
            attempt["error"] = _error_text(mute_error)
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
    python: Path,
    board_id: str,
    serial: str,
    uri: str,
    timeout_s: int,
) -> int:
    completed = _completed_plan_indices(manifest)
    plan = manifest["plan"]
    assert isinstance(plan, list)
    for condition in plan:
        assert isinstance(condition, dict)
        if int(condition["plan_index"]) in completed:
            continue
        _run_attempt(
            manifest,
            manifest_path,
            condition,
            repository=repository,
            python=python,
            board_id=board_id,
            serial=serial,
            uri=uri,
            timeout_s=timeout_s,
        )
        completed.add(int(condition["plan_index"]))
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
    board_id = _validate_identifier(args.board_id, "board ID")
    run_id = _validate_identifier(args.run_id or _new_run_id(), "run ID")
    # Preserve the virtual-environment launcher path. Resolving its symlink to
    # the system interpreter would discard the release-local libiio runtime.
    python = Path(os.path.abspath(args.python.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"capture Python is not executable: {python}")
    repository = Path(__file__).resolve().parents[1]
    board_root = _board_root(board_id)
    manifest_path = board_root / "phase-distributions" / run_id / "manifest.json"
    configuration = _configuration(
        rounds=args.rounds,
        board_id=board_id,
        serial=args.serial,
        uri=args.uri,
        python=python,
        timeout_s=args.timeout_s,
    )

    with _board_lock(board_root):
        manifest = (
            _load_manifest(manifest_path, configuration)
            if manifest_path.exists()
            else _new_manifest(run_id, configuration)
        )
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
                python=python,
                board_id=board_id,
                serial=args.serial,
                uri=args.uri,
                timeout_s=args.timeout_s,
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
