#!/usr/bin/env python3
"""Bounded, per-band TX1 headroom screening; no selector firmware changes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from smateway.campaign_protocol import admit_capture
from smateway.rate_timing import PORTS, load, sha256

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs/comprehensive_fast_switching/data/protocol-v1.json"


def headroom_pass(run):
    capture = run.get("capture", {})
    peaks = capture.get("peak_component_counts", [])
    clips = capture.get("clipped_samples", [])
    return (
        run.get("status") == "passed"
        and len(peaks) == 2
        and all(0 <= p < 1600 for p in peaks)
        and clips == [0, 0]
        and run.get("safety", {}).get("final_source_mute", {}).get("passed") is True
    )


def save(path, record):
    record["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def capture_once(command):
    timeout = float(os.environ.get("SMATEWAY_CAPTURE_TIMEOUT_S", "45"))
    if not 45 <= timeout <= 120:
        raise ValueError("Capture supervisor timeout must be within 45–120 seconds")
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException as error:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        if "--output-root" in command:
            root = Path(command[command.index("--output-root") + 1]) / "supervisor-errors"
            root.mkdir(parents=True, exist_ok=True)
            name = datetime.now(UTC).strftime("capture-%Y%m%dT%H%M%S%fZ.json")
            save(
                root / name,
                {
                    "timeout_s": timeout,
                    "elapsed_s": time.monotonic() - started,
                    "command": command,
                    "stdout": output,
                    "returncode": process.returncode,
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
            )
        raise
    match = re.search(r"^run_dir=(/.+)$", output, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"capture child did not return evidence: {output}")
    path = Path(match.group(1)) / "run.json"
    save(
        path.parent / "capture-supervisor.json",
        {
            "timeout_s": timeout,
            "elapsed_s": time.monotonic() - started,
            "returncode": process.returncode,
            "stdout": output,
        },
    )
    run = load(path)
    if not run.get("safety", {}).get("final_source_mute", {}).get("passed"):
        raise RuntimeError(f"capture did not verify final mute: {path}")
    if any(k.endswith("cleanup_error") or k == "mute_error" for k in run["safety"]):
        raise RuntimeError(f"capture cleanup failed: {path}")
    return path, run


def retryable_metadata_error(run):
    error = run.get("error") or {}
    traceback_text = run.get("error_traceback", "")
    return (
        run.get("status") == "failed"
        and error.get("type") == "OSError"
        and error.get("errno") == 61
        and "iio_metadata.py" in traceback_text
        and ".refill()" in traceback_text
    )


def capture(command):
    retries = int(os.environ.get("SMATEWAY_METADATA_RETRIES", "0"))
    if not 0 <= retries <= 2:
        raise ValueError("Metadata retries must be within 0–2")
    if not retries:
        return capture_once(command)
    if "--output-root" not in command:
        raise ValueError("Retry policy requires a persistent evidence root")
    directory = Path(command[command.index("--output-root") + 1]) / "transport-attempts"
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / datetime.now(UTC).strftime("attempts-%Y%m%dT%H%M%S%fZ.json")
    record = {
        "policy": "Whole-record metadata ENODATA retries only; no quality-based selection",
        "maximum_attempts": retries + 1,
        "status": "running",
        "command": command,
        "attempts": [],
        "source_sha256": sha256(Path(__file__)),
    }
    save(log, record)
    try:
        for attempt in range(retries + 1):
            path, run = capture_once(command)  # Already verifies mute and all cleanup errors.
            record["attempts"].append(
                {
                    "run_json": str(path),
                    "sha256": sha256(path),
                    "status": run["status"],
                    "error": run.get("error"),
                }
            )
            retry = retryable_metadata_error(run) and attempt < retries
            if not retry:
                record.update(status=run["status"], selected_run_json=str(path))
            save(log, record)
            if not retry:
                sidecar = path.parent / "capture-attempts.json"
                save(sidecar, record)
                run["transport_attempts"] = {"path": str(sidecar), "sha256": sha256(sidecar)}
                return path, run
            print(f"[metadata retry {attempt + 1}/{retries}] retained {path}", flush=True)
    except BaseException as error:
        record.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        save(log, record)
        raise
    raise AssertionError("Bounded retry loop returned no capture")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--fixture-json", type=Path, required=True)
    parser.add_argument(
        "--frequencies-hz",
        type=int,
        nargs="+",
        choices=(2_450_000_000, 5_800_000_000),
        default=[2_450_000_000, 5_800_000_000],
    )
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization:
        raise SystemExit("bounded RF headroom screening requires acknowledgment")
    for frequency in args.frequencies_hz:
        admit_capture(PROTOCOL, frequency, muted=False, fixture_path=args.fixture_json)
    args.output_root.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(UTC).strftime("headroom-%Y%m%dT%H%M%S%fZ")
    path = args.output_root / f"{identifier}.json"
    record = {
        "schema": 1,
        "scope": "source-enabled headroom only; not calibration holdouts",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": sha256(PROTOCOL),
        "fixture_sha256": sha256(args.fixture_json),
        "source_sha256": sha256(Path(__file__)),
        "bands": [],
    }
    save(path, record)
    try:
        for frequency in args.frequencies_hz:
            band = {"frequency_hz": frequency, "selected_gain_db": None, "candidates": []}
            record["bands"].append(band)
            gains = (
                (40, 30, 20, 10, 0) if frequency < 3_000_000_000 else (60, 50, 40, 30, 20, 10, 0)
            )
            for gain in gains:
                candidate = {"gain_db": gain, "passed": False, "captures": []}
                band["candidates"].append(candidate)
                accepted = True
                for round_number in (1, 2):
                    for port in PORTS if round_number == 1 else reversed(PORTS):
                        tag = f"{identifier}-{frequency}-g{gain}-r{round_number}-{port}"
                        print(
                            f"[headroom] {frequency}Hz gain={gain} round={round_number} {port}",
                            flush=True,
                        )
                        run_path, run = capture(
                            [
                                sys.executable,
                                str(ROOT / "scripts/capture_rate_timing.py"),
                                "--configuration",
                                "A",
                                "--mode",
                                "static",
                                "--duration-s",
                                "2",
                                "--frequency-hz",
                                str(frequency),
                                "--gain-db",
                                str(gain),
                                "--port",
                                port,
                                "--protocol-json",
                                str(PROTOCOL),
                                "--fixture-json",
                                str(args.fixture_json),
                                "--output-root",
                                str(args.output_root),
                                "--tag",
                                tag,
                                "--acknowledge-ota-authorization",
                            ]
                        )
                        passed = headroom_pass(run)
                        candidate["captures"].append(
                            {
                                "round": round_number,
                                "port": port,
                                "run_json": str(run_path),
                                "sha256": sha256(run_path),
                                "headroom_passed": passed,
                                "error": run.get("error"),
                            }
                        )
                        save(path, record)
                        if run.get("rejected_block") or run.get("partial_timeline"):
                            raise RuntimeError(
                                f"transport failure is not repaired by lowering gain: {run_path}"
                            )
                        if not passed:
                            accepted = False
                            break
                    if not accepted:
                        break
                candidate["passed"] = accepted
                if accepted:
                    band["selected_gain_db"] = gain
                    save(path, record)
                    print(
                        f"[headroom-qualified] {frequency}Hz gain={gain}; "
                        "12 independent static captures",
                        flush=True,
                    )
                    break
            save(path, record)
        record["status"] = (
            "passed"
            if all(b["selected_gain_db"] is not None for b in record["bands"])
            else "failed"
        )
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save(path, record)
        print(f"headroom_evidence={path}", flush=True)
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
