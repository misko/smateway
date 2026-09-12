#!/usr/bin/env python3
"""Bounded repeat acquisition after the September 10 operator-confirmed move.

Reuses the serial-pinned capture/flash/restore paths and their frozen RF levels.
No calibration or decoder is changed here. A stage is never silently rerun.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from run_fast_tracking_timing_campaign import _flash, _lock, _restore
from screen_comprehensive_headroom import capture, headroom_pass

from smateway.campaign_protocol import admit_capture
from smateway.rate_timing import PORTS, load, sha256

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("/srv/bulk/samteway/lab-data/tracking-jitter-20260910-v1")
DUAL = ROOT / "docs/comprehensive_fast_switching/data/protocol-v1.json"
LOW = ROOT / "docs/subghz_915_diagnostic/data/protocol-v1.json"
RECIPE = ROOT / "docs/comprehensive_fast_switching/REFERENCE-TIMING-VALIDATION-v1.md"
SCREEN_SETTINGS = ((5800000000, 60), (2475000000, 40), (5811000000, 60), (915000000, 50))
BLOCK_SETTINGS = (
    (5800000000, "A", 60, (200, 1000)),
    (2475000000, "A", 40, (25, 50, 100, 200, 1000)),
    (2475000000, "B", 40, (25, 50, 100, 200, 1000)),
    (915000000, "A", 50, (200, 1000)),
    (5811000000, "A", 60, (25, 50, 100, 200, 1000)),
    (5811000000, "B", 60, (25, 50, 100, 200, 1000)),
    (5811000000, "D", 60, (25, 50, 100, 200, 1000)),
)
CONTRACT_FILES = (
    "scripts/capture_rate_timing.py",
    "scripts/capture_fast_tracking_timing.py",
    "scripts/run_comprehensive_block.py",
    "scripts/flash_tracking_c6_firmware.py",
    "scripts/restore_tracking_selector_backup.py",
    "src/smateway/rate_timing.py",
    "src/smateway/fast_tracking.py",
    "src/smateway/reference_timing.py",
    "src/smateway/causal_timing.py",
    "src/smateway/campaign_protocol.py",
    "docs/pcb_direct_injection_calibration/data/calibration-lut.json",
)


def save(path, record):
    record["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def binding(root, frequency):
    protocol = LOW if frequency == 915000000 else DUAL
    fixture = root / ("fixture-915.json" if frequency == 915000000 else "fixture-dual-band.json")
    admitted = admit_capture(protocol, frequency, muted=False, fixture_path=fixture)
    return protocol, fixture, admitted


def record_capture(path, run, **extra):
    return {
        **extra,
        "run_json": str(path),
        "sha256": sha256(path),
        "status": run["status"],
        "headroom_pass": headroom_pass(run),
        "error": run.get("error"),
        "peak_component_counts": run.get("capture", {}).get("peak_component_counts"),
        "reference": run.get("reference"),
        **(
            {"transport_attempts": run["transport_attempts"]} if "transport_attempts" in run else {}
        ),
    }


def capture_args(root, frequency, gain, mode, *, tag, port=None, flash=None, tx=0):
    protocol, fixture, _ = binding(root, frequency)
    command = [
        sys.executable,
        str(ROOT / "scripts/capture_rate_timing.py"),
        "--configuration",
        "A",
        "--mode",
        mode,
        "--duration-s",
        "4" if mode == "fast" else "2",
        "--frequency-hz",
        str(frequency),
        "--gain-db",
        str(gain),
        "--tx-channel",
        str(tx),
        "--protocol-json",
        str(protocol),
        "--fixture-json",
        str(fixture),
        "--output-root",
        str(root),
        "--tag",
        tag,
        "--acknowledge-ota-authorization",
    ]
    if port:
        command.extend(("--port", port))
    if flash:
        command.extend(
            (
                "--flash-evidence",
                str(flash),
                "--profile",
                str(ROOT / "profiles/tracking-c6-200us-v1/control_profile.json"),
            )
        )
    return command


def screens(root, record, write):
    for frequency, gain in SCREEN_SETTINGS:
        for mode in ("ambient", "static"):
            for port in PORTS:
                print(
                    f"[screen {len(record['captures']) + 1}/48] "
                    f"{frequency / 1e6:g} MHz g{gain} {mode} {port}",
                    flush=True,
                )
                path, run = capture(
                    capture_args(
                        root,
                        frequency,
                        gain,
                        mode,
                        tag=f"screen-{frequency}-{mode}-{port}",
                        port=port,
                    )
                )
                record["captures"].append(
                    record_capture(
                        path, run, frequency_hz=frequency, gain_db=gain, mode=mode, port=port
                    )
                )
                write()
                peaks = run.get("capture", {}).get("peak_component_counts")
                print(
                    f"  {run['status']} peaks={peaks} reference={run.get('reference')}", flush=True
                )
                if not headroom_pass(run):
                    raise RuntimeError(f"Headroom/acquisition failure retained: {path}")


def verify_screen(root):
    path = root / "screens.json"
    screen = load(path)
    if screen["status"] != "complete" or len(screen["captures"]) != 48:
        raise ValueError("Complete source-on/muted headroom screen required")
    expected = {
        (f, mode, p) for f, _g in SCREEN_SETTINGS for mode in ("ambient", "static") for p in PORTS
    }
    actual = {(r["frequency_hz"], r["mode"], r["port"]) for r in screen["captures"]}
    if actual != expected:
        raise ValueError("Screen frequency/port grid differs")
    for r in screen["captures"]:
        run_path = Path(r["run_json"])
        if sha256(run_path) != r["sha256"] or not headroom_pass(load(run_path)):
            raise ValueError("Screen source hash or headroom differs")
    return {"path": str(path), "sha256": sha256(path)}


def blocks(root, record, write):
    record["screen_binding"] = verify_screen(root)
    for index, (frequency, config, gain, dwells) in enumerate(BLOCK_SETTINGS):
        existing = [
            r
            for r in record["blocks"]
            if r["frequency_hz"] == frequency and r["configuration"] == config
        ]
        if existing:
            if len(existing) != 1 or existing[0]["status"] != "diagnostic-complete":
                raise ValueError("Only one verified complete block may be inherited per condition")
            item = existing[0]
            if sha256(Path(item["block_json"])) != item["sha256"]:
                raise ValueError("Inherited block hash differs")
            print(f"[block-{index + 1}] retaining completed {frequency} / {config}", flush=True)
            continue
        protocol, fixture, _ = binding(root, frequency)
        tag = f"block-{index + 1}-{frequency}-{config}"
        print(f"[{tag}] starting bracketed three-round dwell block", flush=True)
        command = [
            sys.executable,
            str(ROOT / "scripts/run_comprehensive_block.py"),
            "--output-root",
            str(root),
            "--protocol-json",
            str(protocol),
            "--fixture-json",
            str(fixture),
            "--frequency-hz",
            str(frequency),
            "--gain-db",
            str(gain),
            "--configuration",
            config,
            "--dwells-us",
            *map(str, dwells),
            "--timing-recipe",
            str(RECIPE),
            "--acknowledge-ota-authorization",
            "--acknowledge-selector-flash",
        ]
        log = root / f"{tag}.log"
        with log.open("x") as stream:
            process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            try:
                code = process.wait()
            except BaseException:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=90)
                raise
        candidates = [
            line.split("=", 1)[1]
            for line in log.read_text().splitlines()
            if line.startswith("block_evidence=")
        ]
        item = {
            "frequency_hz": frequency,
            "configuration": config,
            "gain_db": gain,
            "dwell_us": list(dwells),
            "returncode": code,
            "log": str(log),
        }
        if candidates:
            path = Path(candidates[-1])
            item.update(
                {"block_json": str(path), "sha256": sha256(path), "status": load(path)["status"]}
            )
        record["blocks"].append(item)
        write()
        if code != 0 or item.get("status") != "diagnostic-complete":
            raise RuntimeError(f"Block did not complete; retained evidence in {log}")
        print(f"[{tag}] acquired; fixed-policy analysis and exact restore complete", flush=True)


def dense(root, record, write):
    record["screen_binding"] = verify_screen(root)
    # This fixed 200 us schedule is a matched historical comparison, not promotion
    # based on the new fixture's performance. No selector control overlaps capture.
    flash = _flash(root, 200)
    record["flash"] = {"path": str(flash), "sha256": sha256(flash)}
    write()
    try:
        for frequency in range(5726000000, 5874000001, 1000000):
            for tx in (0, 1):
                print(
                    f"[dense {len(record['captures']) + 1}/298] {frequency / 1e6:g} MHz TX{tx + 1}",
                    flush=True,
                )
                path, run = capture(
                    capture_args(
                        root,
                        frequency,
                        60,
                        "fast",
                        tx=tx,
                        flash=flash,
                        tag=f"dense-{frequency}-tx{tx + 1}",
                    )
                )
                record["captures"].append(
                    record_capture(path, run, frequency_hz=frequency, tx_channel=tx, dwell_us=200)
                )
                write()
                if not headroom_pass(run):
                    raise RuntimeError(f"Dense acquisition/headroom failed: {path}")
    finally:
        restored = _restore(root, flash)
        record["restore"] = {"path": str(restored), "sha256": sha256(restored)}
        write()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", choices=("screens", "blocks", "dense"), required=True)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization:
        raise SystemExit("Current bounded OTA authorization required")
    if args.stage != "screens" and not args.acknowledge_selector_flash:
        raise SystemExit("Selector programming acknowledgment required")
    root = args.output_root.resolve(strict=True)
    path = root / f"{args.stage}.json"
    if path.exists():
        raise SystemExit("Stage evidence already exists; no silent retry or overwrite")
    bound = [binding(root, f)[2] for f, _g in SCREEN_SETTINGS]
    contract = {
        str(Path(__file__).relative_to(ROOT)): sha256(Path(__file__)),
        **{name: sha256(ROOT / name) for name in CONTRACT_FILES},
    }
    record = {
        "schema": 1,
        "campaign_id": "jittered-ota-comparison-20260910-v1",
        "stage": args.stage,
        "status": "running",
        "captures": [],
        "blocks": [],
        "fixture_bindings": bound,
        "source_contract": contract,
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "New moved-fixture evidence; approximate angles only, no per-port refit",
    }

    def write():
        save(path, record)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        with _lock(root / ".jitter-hardware.lock"):
            write()
            {"screens": screens, "blocks": blocks, "dense": dense}[args.stage](root, record, write)
        record["status"] = "complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        write()
        print(f"stage_evidence={path}", flush=True)


if __name__ == "__main__":
    main()
