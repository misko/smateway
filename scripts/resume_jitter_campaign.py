#!/usr/bin/env python3
"""Explicit continuation in a new directory; retain old failures and full blocks.

No automatic retries: a new stream failure still stops acquisition and restores
the selector. Completed trials are never silently replaced or reselected.
"""

import argparse
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path

import run_jitter_reacquisition as campaign
from flash_tracking_c6_firmware import (
    BOARD_ID,
    FLASH_BASE,
    FLASH_SIZE,
    OPENOCD_CONFIG,
    RECEIVER_SERIAL,
    RECEIVER_URI,
    SOURCE_SERIAL,
    SOURCE_URI,
    UID_ADDRESS,
    UID_SIZE,
    _mute,
    _openocd,
    _tcl,
)

from smateway.bench import BenchManifest, OpenOcdBench
from smateway.rate_timing import PORTS, load, sha256


def inherit(parent, root, confirmation, confirmed_at):
    old = load(parent / "blocks.json")
    if old["status"] != "failed" or (parent / "dense.json").exists():
        raise ValueError("This continuation requires stopped blocks and an unstarted dense stage")
    campaign.verify_screen(parent)
    complete = [r for r in old["blocks"] if r["status"] == "diagnostic-complete"]
    expected = {(f, c) for f, c, _g, _d in campaign.BLOCK_SETTINGS[:4]}
    if (
        len(complete) != 4
        or {(r["frequency_hz"], r["configuration"]) for r in complete} != expected
    ):
        raise ValueError("Expected exactly the four previously completed conditions")
    for row in complete:
        path = Path(row["block_json"])
        if sha256(path) != row["sha256"] or load(path)["status"] != "diagnostic-complete":
            raise ValueError("Inherited complete block differs")
    root.mkdir(parents=True, exist_ok=False)
    for name in ("screens.json", "initial-selector-flash.bin"):
        shutil.copyfile(parent / name, root / name)
    for name in ("fixture-dual-band.json", "fixture-915.json"):
        old_path = parent / name
        fixture = load(old_path)
        fixture.update(
            {
                "confirmed_at": confirmed_at,
                "operator_confirmation": confirmation,
                "continuation_of": {"path": str(old_path), "sha256": sha256(old_path)},
                "timestamp_scope": "User requested continuation after the readiness prompt.",
            }
        )
        campaign.save(root / name, fixture)
    manifest = {
        "parent": str(parent),
        "parent_blocks_sha256": sha256(parent / "blocks.json"),
        "confirmation": confirmation,
        "confirmed_at": confirmed_at,
        "inherited_complete_blocks": complete,
        "retained_failed_blocks": [
            r for r in old["blocks"] if r["status"] != "diagnostic-complete"
        ],
        "inherited_screen_sha256": sha256(parent / "screens.json"),
        "scope": (
            "September 11 continuation; September 10 screens retained for static "
            "comparison; fresh 5811 MHz screen required."
        ),
    }
    campaign.save(root / "continuation.json", manifest)
    return complete


def preflight(root, *, gain_db=60):
    path = root / "recovery-preflight.json"
    record = {"status": "running", "captures": [], "started_at": datetime.now(UTC).isoformat()}
    try:
        record["initial_mute"] = [
            _mute(RECEIVER_URI, RECEIVER_SERIAL),
            _mute(SOURCE_URI, SOURCE_SERIAL),
        ]
        uid, flash = root / "recovery-uid.bin", root / "recovery-flash.bin"
        result = _openocd(
            campaign.ROOT / OPENOCD_CONFIG,
            "init; reset halt; "
            f"dump_image {_tcl(uid)} 0x{UID_ADDRESS:x} 0x{UID_SIZE:x}; "
            f"dump_image {_tcl(flash)} 0x{FLASH_BASE:x} 0x{FLASH_SIZE:x}; "
            "reset run; shutdown",
            root / "recovery-readback.log",
        )
        if (
            result.returncode
            or uid.read_bytes().hex() != BOARD_ID.removeprefix("stm32c011-")
            or sha256(flash) != sha256(root / "initial-selector-flash.bin")
        ):
            raise ValueError("Recovery selector identity/full-image differs")
        controller = OpenOcdBench(
            BenchManifest.load(
                campaign.ROOT / "build/STM32C011F4P6/bench/pluto_bench.manifest.json"
            ),
            campaign.ROOT / OPENOCD_CONFIG,
        )
        state = controller.request(8, 0, wait_until_applied=True)
        if state.applied_code != 8 or state.lease_active:
            raise ValueError("Selector ALL_OFF readback failed")
        record["selector"] = state.as_dict()
        record["flash_sha256"] = sha256(flash)
        campaign.save(path, record)
        for mode in ("ambient", "static"):
            for port in PORTS:
                print(
                    f"[recovery screen {len(record['captures']) + 1}/12] {mode} {port}", flush=True
                )
                p, run = campaign.capture(
                    campaign.capture_args(
                        root, 5811000000, gain_db, mode, port=port, tag=f"recovery-{mode}-{port}"
                    )
                )
                record["captures"].append(campaign.record_capture(p, run, mode=mode, port=port))
                campaign.save(path, record)
                if not campaign.headroom_pass(run):
                    raise RuntimeError(f"Recovery screen failed; no automatic retry: {p}")
        record["status"] = "complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        campaign.save(path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--confirmed-at", required=True)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization or not args.acknowledge_selector_flash:
        raise SystemExit("Explicit bounded continuation acknowledgments required")
    root = args.output_root.resolve()
    complete = inherit(args.parent.resolve(strict=True), root, args.confirmation, args.confirmed_at)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt)
    record = {
        "schema": 1,
        "stage": "blocks",
        "status": "running",
        "blocks": complete,
        "captures": [],
        "started_at": datetime.now(UTC).isoformat(),
        "continuation": {
            "path": str(root / "continuation.json"),
            "sha256": sha256(root / "continuation.json"),
        },
        "source_contract": {name: sha256(campaign.ROOT / name) for name in campaign.CONTRACT_FILES},
        "source_sha256": sha256(Path(__file__)),
    }
    path = root / "blocks.json"
    campaign.save(path, record)
    try:
        with campaign._lock(root / ".jitter-hardware.lock"):
            preflight(root)
            campaign.blocks(root, record, lambda: campaign.save(path, record))
        record["status"] = "complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        campaign.save(path, record)
    print("[continuation] all bracketed blocks complete; starting dense repeat", flush=True)
    # Use the existing independently logged stage, with its own exact restore.
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            str(campaign.ROOT / "scripts/run_jitter_reacquisition.py"),
            "--output-root",
            str(root),
            "--stage",
            "dense",
            "--acknowledge-ota-authorization",
            "--acknowledge-selector-flash",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
