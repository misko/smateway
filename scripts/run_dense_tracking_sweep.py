#!/usr/bin/env python3
"""Run or resume a source-pinned dense 5.8 GHz TX1/TX2 tracking sweep."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY / "scripts/run_continuous_tracking_screen.py"
DEFAULT_OUTPUT_ROOT = Path(
    "/srv/bulk/samteway/lab-data/tracking-dense-1mhz-20260903-v1"
)
MIN_CENTER_HZ = 5_726_000_000
MAX_CENTER_HZ = 5_874_000_000
FINE_LATTICE_HZ = 100_000
RUN_DIRECTORY = re.compile(r"^run_dir=(/.+)$", re.MULTILINE)
SOURCE_CONTRACT = (
    "scripts/run_dense_tracking_sweep.py",
    "scripts/run_continuous_tracking_screen.py",
    "src/smateway/bench.py",
    "src/smateway/tracking/__init__.py",
    "src/smateway/tracking/bearing.py",
    "src/smateway/tracking/calibration.py",
    "src/smateway/tracking/channel.py",
    "src/smateway/tracking/manifold.py",
    "src/smateway/tracking/schedule.py",
    "src/smateway/tracking/timeline.py",
    "docs/tracking_development_plan/data/ism-frequency-plan.json",
    "docs/pcb_direct_injection_calibration/data/calibration-lut.json",
    "profiles/fast20-v1/control_profile.json",
    "build/STM32C011F4P6/bench/pluto_bench.manifest.json",
    "openocd/stlink-v3-stm32c011.cfg",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-hz", type=int, default=MIN_CENTER_HZ)
    parser.add_argument("--stop-hz", type=int, default=MAX_CENTER_HZ)
    parser.add_argument("--step-hz", type=int, default=1_000_000)
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--dwell-ms", type=int, default=100)
    parser.add_argument("--tx-gain-db", type=float, default=-35.0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_contract() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in SOURCE_CONTRACT:
        path = REPOSITORY / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"campaign source is absent or not a regular file: {relative}")
        result[relative] = _sha256(path)
    return result


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _exclusive_lock(path: Path) -> Any:
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"dense tracking campaign is already running: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validate_grid(start_hz: int, stop_hz: int, step_hz: int, passes: int) -> None:
    if (
        start_hz < MIN_CENTER_HZ
        or stop_hz > MAX_CENTER_HZ
        or start_hz > stop_hz
        or start_hz % FINE_LATTICE_HZ
        or stop_hz % FINE_LATTICE_HZ
        or step_hz < FINE_LATTICE_HZ
        or step_hz % FINE_LATTICE_HZ
        or (stop_hz - start_hz) % step_hz
    ):
        raise ValueError(
            "dense sweep must be an inclusive 100 kHz-aligned grid within "
            "5.726..5.874 GHz"
        )
    if passes < 1 or passes > 3:
        raise ValueError("dense sweep passes must be 1..3")


def _frequency_order(
    start_hz: int,
    stop_hz: int,
    step_hz: int,
    pass_index: int,
) -> tuple[int, ...]:
    frequencies = tuple(range(start_hz, stop_hz + 1, step_hz))
    return frequencies if pass_index % 2 == 0 else tuple(reversed(frequencies))


def _condition_key(pass_index: int, frequency_hz: int, tx_channel: int) -> str:
    return f"pass{pass_index + 1}:f{frequency_hz}:tx{tx_channel + 1}"


def _new_campaign(args: argparse.Namespace, source_contract: dict[str, str]) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "schema": 1,
        "campaign_id": "tracking-dense-1mhz-20260903-v1",
        "evidence_kind": "resumable_dense_continuous_two_source_tracking_sweep",
        "status": "running",
        "started_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "source_commit": commit,
        "source_contract_sha256": source_contract,
        "configuration": {
            "start_hz": args.start_hz,
            "stop_hz": args.stop_hz,
            "step_hz": args.step_hz,
            "passes": args.passes,
            "tx_channels": [0, 1],
            "dwell_ms": args.dwell_ms,
            "tx_gain_db": args.tx_gain_db,
            "profile_id": "ism5800-c6-v1",
            "frequency_count_per_pass": (
                (args.stop_hz - args.start_hz) // args.step_hz + 1
            ),
            "condition_count": (
                2 * args.passes * ((args.stop_hz - args.start_hz) // args.step_hz + 1)
            ),
            "allocation_edge_guard_hz": 100_000,
        },
        "conditions": [],
        "error": None,
    }


def _load_or_create_campaign(
    path: Path,
    args: argparse.Namespace,
    source_contract: dict[str, str],
) -> dict[str, Any]:
    if not path.exists():
        document = _new_campaign(args, source_contract)
        _write_json_atomic(path, document)
        return document
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise RuntimeError("existing dense campaign manifest is invalid")
    expected = _new_campaign(args, source_contract)
    if document.get("configuration") != expected["configuration"]:
        raise RuntimeError("existing dense campaign configuration differs")
    if document.get("source_contract_sha256") != source_contract:
        raise RuntimeError("existing dense campaign source contract differs")
    if document.get("status") == "complete":
        return document
    document["status"] = "running"
    document["updated_at"] = _now()
    document["error"] = None
    _write_json_atomic(path, document)
    return document


def _validate_child_run(
    run_path: Path,
    *,
    frequency_hz: int,
    tx_channel: int,
) -> dict[str, Any]:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    configuration = run.get("configuration", {})
    safety = run.get("safety", {})
    selector = safety.get("selector_final_all_off", {})
    if (
        run.get("status") != "passed"
        or configuration.get("frequency_hz") != frequency_hz
        or configuration.get("tx_channel") != tx_channel
        or configuration.get("dense_5g8_campaign") is not True
        or safety.get("source_final_mute", {}).get("passed") is not True
        or selector.get("lease_active") is not False
        or selector.get("applied_code") != selector.get("command_code")
    ):
        raise RuntimeError(f"dense child run did not satisfy its condition: {run_path}")
    return run


def _completed_by_key(campaign: dict[str, Any]) -> dict[str, dict[str, Any]]:
    conditions = campaign.get("conditions")
    if not isinstance(conditions, list):
        raise RuntimeError("dense campaign conditions are malformed")
    completed: dict[str, dict[str, Any]] = {}
    for condition in conditions:
        if not isinstance(condition, dict) or not isinstance(condition.get("key"), str):
            raise RuntimeError("dense campaign condition record is malformed")
        if condition["key"] in completed:
            raise RuntimeError("dense campaign repeats a condition key")
        run_json = Path(str(condition.get("run_json")))
        _validate_child_run(
            run_json,
            frequency_hz=int(condition["frequency_hz"]),
            tx_channel=int(condition["tx_channel"]),
        )
        completed[condition["key"]] = condition
    return completed


def _run_condition(
    args: argparse.Namespace,
    runs_root: Path,
    *,
    frequency_hz: int,
    tx_channel: int,
) -> tuple[Path, str, float]:
    command = (
        sys.executable,
        str(RUNNER),
        "--profile-id",
        "ism5800-c6-v1",
        "--frequency-hz",
        str(frequency_hz),
        "--tx-channel",
        str(tx_channel),
        "--scan-pairs",
        "1",
        "--dwell-ms",
        str(args.dwell_ms),
        "--tx-gain-db",
        str(args.tx_gain_db),
        "--dense-5g8-campaign",
        "--acknowledge-ota-authorization",
        "--output-root",
        str(runs_root),
    )
    started = time.monotonic()
    process = subprocess.run(
        command,
        cwd=REPOSITORY,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.monotonic() - started
    match = RUN_DIRECTORY.search(process.stdout)
    if process.returncode != 0 or match is None:
        raise RuntimeError(
            f"dense child failed at {frequency_hz} Hz TX{tx_channel + 1} "
            f"with exit {process.returncode}:\n{process.stdout}"
        )
    run_directory = Path(match.group(1))
    return run_directory / "run.json", process.stdout, elapsed


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_ota_authorization:
        raise SystemExit("dense RF sweep requires --acknowledge-ota-authorization")
    try:
        _validate_grid(args.start_hz, args.stop_hz, args.step_hz, args.passes)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    args.output_root.mkdir(parents=True, exist_ok=True)
    runs_root = args.output_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_root / "campaign.json"
    source_contract = _source_contract()
    with _exclusive_lock(args.output_root / ".campaign.lock"):
        campaign = _load_or_create_campaign(manifest_path, args, source_contract)
        completed = _completed_by_key(campaign)
        total = int(campaign["configuration"]["condition_count"])
        if campaign["status"] == "complete" and len(completed) == total:
            print(f"campaign already complete: {manifest_path}")
            return 0
        try:
            for pass_index in range(args.passes):
                for frequency_hz in _frequency_order(
                    args.start_hz, args.stop_hz, args.step_hz, pass_index
                ):
                    for tx_channel in (0, 1):
                        key = _condition_key(pass_index, frequency_hz, tx_channel)
                        if key in completed:
                            continue
                        if _source_contract() != source_contract:
                            raise RuntimeError(
                                "campaign source changed while the sweep was running"
                            )
                        ordinal = len(completed) + 1
                        print(
                            f"[{ordinal}/{total}] pass={pass_index + 1} "
                            f"frequency={frequency_hz / 1e6:.3f}MHz TX{tx_channel + 1}",
                            flush=True,
                        )
                        run_json, output, elapsed = _run_condition(
                            args,
                            runs_root,
                            frequency_hz=frequency_hz,
                            tx_channel=tx_channel,
                        )
                        run = _validate_child_run(
                            run_json,
                            frequency_hz=frequency_hz,
                            tx_channel=tx_channel,
                        )
                        far = run["analysis"]["ideal_far_field_bearing"]
                        condition = {
                            "key": key,
                            "pass_index": pass_index,
                            "frequency_hz": frequency_hz,
                            "tx_channel": tx_channel,
                            "run_id": run["run_id"],
                            "run_json": str(run_json),
                            "elapsed_s": elapsed,
                            "minimum_coherent_estimator_snr_db": run["analysis"][
                                "minimum_coherent_estimator_snr_db"
                            ],
                            "maximum_repeat_phase_rms_deg": run["analysis"][
                                "maximum_repeat_phase_rms_deg"
                            ],
                            "ideal_bearing_deg": far[
                                "bearing_deg_clockwise_from_forward"
                            ],
                            "ideal_bearing_valid": far["valid"],
                            "child_stdout": output,
                        }
                        campaign["conditions"].append(condition)
                        completed[key] = condition
                        campaign["updated_at"] = _now()
                        _write_json_atomic(manifest_path, campaign)
            campaign["status"] = "complete"
            campaign["completed_at"] = _now()
            campaign["updated_at"] = campaign["completed_at"]
            _write_json_atomic(manifest_path, campaign)
        except BaseException as error:
            campaign["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            campaign["updated_at"] = _now()
            campaign["error"] = {"type": type(error).__name__, "message": str(error)}
            _write_json_atomic(manifest_path, campaign)
            raise
    print(f"campaign complete: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
