#!/usr/bin/env python3
"""Run a bounded subset of the predeclared conducted board-path sweep."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import run_fast20_reference_distribution as base  # type: ignore[import-not-found]

from smateway.rf_policy import (
    CONDUCTED_SWEEP_MAXIMUM_HZ,
    CONDUCTED_SWEEP_MINIMUM_HZ,
    CONDUCTED_SWEEP_STEP_HZ,
    classify_conducted_calibration_center_frequency,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_SERIAL = "104000b29905000e17000800065934759d"
DEFAULT_URI = "usb:1.4.5"
DEFAULT_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
DEFAULT_RUN_ID = "broadband-board-calibration-20260828"
DEFAULT_RECEIVER_GAIN_DB = 40
DEFAULT_TIMEOUT_S = 180
CONDUCTED_FIXTURE_ID = "tx1-2way-rx1-and-8way-board-rx2-v1"
PROFILE_ID = "fast20-v1"
PROFILE_CONTRACT_SHA256 = "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
FIRMWARE_BINARY_SHA256 = "aeaed9d2f892d2a59add1aba2a7477e349b750c99f81610632286d04d91326ac"
FREQUENCIES_HZ = tuple(
    range(
        CONDUCTED_SWEEP_MINIMUM_HZ,
        CONDUCTED_SWEEP_MAXIMUM_HZ + 1,
        CONDUCTED_SWEEP_STEP_HZ,
    )
)
CLOSURE_FREQUENCIES_HZ = (
    2_100_000_000,
    2_400_000_000,
    3_000_000_000,
    4_000_000_000,
    5_000_000_000,
    5_800_000_000,
)
STAGES = ("rotation0", "rotation1", "rotation2", "closure0")
ROTATION_BY_STAGE = {"rotation0": 0, "rotation1": 1, "rotation2": 2, "closure0": 0}


class ConductedSweepError(RuntimeError):
    """A predeclared sweep or safety invariant failed."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--receiver-gain-db", type=int, default=DEFAULT_RECEIVER_GAIN_DB)
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--frequency-min-hz",
        type=int,
        default=CONDUCTED_SWEEP_MINIMUM_HZ,
        help="inclusive lower edge on the fixed 100 MHz conducted sweep grid",
    )
    parser.add_argument(
        "--frequency-max-hz",
        type=int,
        default=CONDUCTED_SWEEP_MAXIMUM_HZ,
        help="inclusive upper edge on the fixed 100 MHz conducted sweep grid",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-only", action="store_true")
    action.add_argument("--execute-stage", choices=STAGES)
    parser.add_argument(
        "--confirm-fully-conducted",
        action="store_true",
        help="confirm TX1 has no antenna path and the exact conducted fixture is connected",
    )
    parser.add_argument(
        "--confirm-mapping",
        choices=STAGES,
        help="confirm the physical feed-to-board mapping for the selected stage",
    )
    return parser


def _mapping(rotation: int) -> dict[str, str]:
    return {f"F{index + 1}": f"ANT{(index + rotation) % 8 + 1}" for index in range(8)}


def _frequency_grid(minimum_hz: int, maximum_hz: int) -> tuple[int, ...]:
    if minimum_hz > maximum_hz:
        raise ValueError("frequency minimum must not exceed frequency maximum")
    if minimum_hz not in FREQUENCIES_HZ or maximum_hz not in FREQUENCIES_HZ:
        raise ValueError(
            "frequency bounds must lie on the 2.1–5.8 GHz conducted 100 MHz grid"
        )
    return tuple(
        frequency_hz
        for frequency_hz in FREQUENCIES_HZ
        if minimum_hz <= frequency_hz <= maximum_hz
    )


def _capture_command(
    python: Path,
    repository: Path,
    condition: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> list[str]:
    return [
        str(python),
        str(repository / "scripts/capture_fast20_dwell.py"),
        "--tx-channel",
        "0",
        "--stimulus",
        "qualification",
        "--receiver-gain-db",
        str(configuration["receiver_gain_db"]),
        "--sample-rate-hz",
        "1000000",
        "--center-frequency-hz",
        str(condition["center_frequency_hz"]),
        "--board-id",
        str(configuration["board_id"]),
        "--serial",
        str(configuration["serial"]),
        "--uri",
        str(configuration["uri"]),
        "--allow-conducted-calibration-sweep",
        "--conducted-fixture-id",
        CONDUCTED_FIXTURE_ID,
        "--confirm-fully-conducted",
    ]


def _configuration(
    *,
    board_id: str,
    serial: str,
    uri: str,
    python: Path,
    receiver_gain_db: int,
    timeout_s: int,
    frequencies_hz: tuple[int, ...] = FREQUENCIES_HZ,
) -> dict[str, Any]:
    if not 0 <= receiver_gain_db <= 62:
        raise ValueError("receiver gain must be within 0..62 dB")
    if not 30 <= timeout_s <= 600:
        raise ValueError("timeout must be within 30..600 seconds")
    if not frequencies_hz:
        raise ValueError("frequency grid must not be empty")
    if tuple(sorted(set(frequencies_hz))) != frequencies_hz:
        raise ValueError("frequencies must be unique and increasing")
    if any(frequency_hz not in FREQUENCIES_HZ for frequency_hz in frequencies_hz):
        raise ValueError("frequencies must use the conducted 100 MHz grid")
    for frequency_hz in frequencies_hz:
        classify_conducted_calibration_center_frequency(frequency_hz)
    closure_frequencies_hz = tuple(
        frequency_hz
        for frequency_hz in CLOSURE_FREQUENCIES_HZ
        if frequency_hz in frequencies_hz
    )
    planned_capture_count = 3 * len(frequencies_hz) + len(closure_frequencies_hz)
    board_root = base._board_root(board_id)
    return {
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "frequencies_hz": list(frequencies_hz),
        "closure_frequencies_hz": list(closure_frequencies_hz),
        "stages": list(STAGES),
        "mappings": {stage: _mapping(ROTATION_BY_STAGE[stage]) for stage in STAGES},
        "fixture_id": CONDUCTED_FIXTURE_ID,
        "fully_conducted_required": True,
        "tx_channel": 0,
        "stimulus": "qualification",
        "receiver_gain_db": receiver_gain_db,
        "sample_rate_hz": 1_000_000,
        "duration_s": 10,
        "kernel_buffers": 8,
        "planned_capture_count": planned_capture_count,
        "estimated_raw_iq_bytes": planned_capture_count * 10 * 1_000_000 * 2 * 4,
        "profile_id": PROFILE_ID,
        "profile_contract_sha256": PROFILE_CONTRACT_SHA256,
        "firmware_binary_sha256": FIRMWARE_BINARY_SHA256,
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "python": str(python),
        "timeout_s": timeout_s,
        "storage_medium": "raspberry_pi_local_filesystem",
        "board_state_root": str(board_root),
        "artifact_storage_root": str(board_root / "pluto-usb-captures"),
        "pluto_onboard_storage_used": False,
    }


def _execution_plan(configuration: Mapping[str, Any], repository: Path) -> list[dict[str, Any]]:
    python = Path(str(configuration["python"]))
    plan = []
    for stage in STAGES:
        frequencies = (
            configuration["closure_frequencies_hz"]
            if stage == "closure0"
            else configuration["frequencies_hz"]
        )
        rotation = ROTATION_BY_STAGE[stage]
        for order_index, frequency_hz in enumerate(frequencies):
            condition: dict[str, Any] = {
                "plan_index": len(plan),
                "stage": stage,
                "rotation": rotation,
                "stage_order_index": order_index,
                "center_frequency_hz": frequency_hz,
                "tx_channel": 0,
                "mapping": _mapping(rotation),
                "receiver_gain_db": int(configuration["receiver_gain_db"]),
                "stimulus": "qualification",
                "sample_rate_hz": 1_000_000,
            }
            condition["capture_command"] = _capture_command(
                python, repository, condition, configuration
            )
            condition["reference_reanalysis_command_template"] = base._reanalyze_command(
                python,
                repository,
                base.ARTIFACT_TOKEN,
                str(configuration["board_id"]),
            )
            plan.append(condition)
    return plan


def _git_head(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_clean(repository: Path) -> bool:
    return not subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _new_manifest(
    run_id: str,
    configuration: Mapping[str, Any],
    repository: Path,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "experiment_kind": configuration["experiment_kind"],
        "run_id": run_id,
        "status": "prepared",
        "created_at": base._now(),
        "completed_at": None,
        "runner_source_commit": _git_head(repository),
        "configuration": dict(configuration),
        "plan": _execution_plan(configuration, repository),
        "attempts": [],
        "mapping_confirmations": [],
        "final_mute_attempts": [],
        "final_mute": None,
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
        raise ConductedSweepError(f"cannot load sweep manifest: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ConductedSweepError("sweep manifest schema is invalid")
    if document.get("configuration") != configuration:
        raise ConductedSweepError("resume configuration differs from the persisted sweep")
    if document.get("plan") != _execution_plan(configuration, repository):
        raise ConductedSweepError("resume execution plan differs from the persisted sweep")
    if document.get("runner_source_commit") != _git_head(repository):
        raise ConductedSweepError("repository HEAD differs from the frozen runner source commit")
    base._validate_attempt_history(document)
    return document


def _stage_complete(manifest: Mapping[str, Any], stage: str) -> bool:
    complete = base._completed_plan_indices(manifest)
    conditions = [
        item for item in manifest["plan"] if isinstance(item, dict) and item.get("stage") == stage
    ]
    return bool(conditions) and all(int(item["plan_index"]) in complete for item in conditions)


def _check_stage_prerequisites(manifest: Mapping[str, Any], stage: str) -> None:
    index = STAGES.index(stage)
    missing = [prior for prior in STAGES[:index] if not _stage_complete(manifest, prior)]
    if missing:
        raise ConductedSweepError(f"complete prior stages before {stage}: {', '.join(missing)}")


def _execute_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    stage: str,
    repository: Path,
    configuration: Mapping[str, Any],
) -> None:
    _check_stage_prerequisites(manifest, stage)
    confirmations = manifest["mapping_confirmations"]
    assert isinstance(confirmations, list)
    confirmations.append(
        {
            "stage": stage,
            "rotation": ROTATION_BY_STAGE[stage],
            "mapping": _mapping(ROTATION_BY_STAGE[stage]),
            "fixture_id": CONDUCTED_FIXTURE_ID,
            "fully_conducted": True,
            "confirmed_at": base._now(),
            "confirmation": "operator CLI confirmation after physical inspection",
        }
    )
    manifest["status"] = f"running_{stage}"
    base._persist_progress(manifest_path, manifest)

    completed = base._completed_plan_indices(manifest)
    pending_error: BaseException | None = None
    try:
        for condition in manifest["plan"]:
            assert isinstance(condition, dict)
            if condition.get("stage") != stage:
                continue
            plan_index = int(condition["plan_index"])
            if plan_index in completed:
                continue
            base._run_attempt(
                manifest,
                manifest_path,
                condition,
                repository=repository,
                board_id=str(configuration["board_id"]),
                serial=str(configuration["serial"]),
                timeout_s=int(configuration["timeout_s"]),
                receiver_gain_db=int(configuration["receiver_gain_db"]),
            )
            completed.add(plan_index)
    except BaseException as error:
        pending_error = error
    finally:
        final_mute = base._strict_mute(str(configuration["serial"]), f"final_{stage}")
        final_mutes = manifest["final_mute_attempts"]
        assert isinstance(final_mutes, list)
        final_mutes.append(final_mute)
        manifest["final_mute"] = final_mute
        if final_mute["status"] != "passed":
            pending_error = ConductedSweepError(f"strict final mute failed: {final_mute['error']}")
        base._persist_progress(manifest_path, manifest)
    if pending_error is not None:
        manifest["status"] = f"failed_{stage}"
        base._persist_progress(manifest_path, manifest)
        raise pending_error
    if not _stage_complete(manifest, stage):
        raise ConductedSweepError(f"{stage} ended before all conditions completed")

    next_index = STAGES.index(stage) + 1
    if next_index == len(STAGES):
        manifest["status"] = "captures_complete"
        manifest["completed_at"] = base._now()
    else:
        manifest["status"] = f"awaiting_{STAGES[next_index]}"
    base._persist_progress(manifest_path, manifest)


def main() -> int:
    args = _parser().parse_args()
    signal.signal(signal.SIGTERM, base._cooperative_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, base._cooperative_termination)
    repository = Path(__file__).resolve().parents[1]
    if not _git_clean(repository):
        raise SystemExit("repository must be clean before preparing or resuming the RF sweep")
    run_id = base._validate_identifier(args.run_id, "run ID")
    board_id = base._validate_identifier(args.board_id, "board ID")
    python = Path(os.path.abspath(args.python.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"capture Python is not executable: {python}")
    try:
        frequencies_hz = _frequency_grid(args.frequency_min_hz, args.frequency_max_hz)
        configuration = _configuration(
            board_id=board_id,
            serial=args.serial,
            uri=args.uri,
            python=python,
            receiver_gain_db=args.receiver_gain_db,
            timeout_s=args.timeout_s,
            frequencies_hz=frequencies_hz,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error

    board_root = base._board_root(board_id)
    manifest_path = board_root / "closed-loop-frequency-sweeps" / run_id / "manifest.json"
    with base._board_lock(board_root):
        manifest = (
            _load_manifest(manifest_path, configuration, repository)
            if manifest_path.exists()
            else _new_manifest(run_id, configuration, repository)
        )
        base._persist_progress(manifest_path, manifest)
        if args.prepare_only:
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "manifest": str(manifest_path),
                        "status": manifest["status"],
                        "planned_capture_count": len(manifest["plan"]),
                    },
                    sort_keys=True,
                )
            )
            return 0
        stage = str(args.execute_stage)
        if not args.confirm_fully_conducted:
            raise SystemExit("stage execution requires --confirm-fully-conducted")
        if args.confirm_mapping != stage:
            raise SystemExit(f"stage execution requires --confirm-mapping {stage}")
        _execute_stage(
            manifest,
            manifest_path,
            stage=stage,
            repository=repository,
            configuration=configuration,
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "manifest": str(manifest_path),
                    "status": manifest["status"],
                    "summary": manifest["summary"],
                },
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
