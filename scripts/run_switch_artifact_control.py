#!/usr/bin/env python3
"""Three-round switched source-muted diagnostic with exact selector restoration."""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from capture_rate_timing import record_interrupt

# Reuse the verified controller; invoke with the repository's Python/library environment.
from run_comprehensive_block import ROOT, _flash, _lock, _restore, run_switched
from screen_comprehensive_headroom import capture

from smateway.rate_timing import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_selector_flash:
        raise SystemExit("explicit selector programming acknowledgement required")
    root = Path("/srv/bulk/samteway/lab-data/tracking-comprehensive-20260908-v1")
    identifier = datetime.now(UTC).strftime("switched-muted-%Y%m%dT%H%M%S%fZ")
    path = root / f"{identifier}.json"
    record = {
        "schema": 1,
        "status": "running",
        "captures": [],
        "flashes": [],
        "restores": [],
        "interrupt_events": [],
        "scope": "switched source-muted diagnostic, not calibration",
        "source_sha256": sha256(Path(__file__)),
    }
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            signum, lambda received, _frame: record_interrupt(received, record["interrupt_events"])
        )
    rows = []
    rng = random.Random(202609082310)
    for number in (1, 2, 3):
        order = [25, 200, 1000]
        rng.shuffle(order)
        rows.extend({"round": number, "dwell_us": dwell} for dwell in order)
    record["planned_rows"] = rows

    def save():
        path.write_text(json.dumps(record, indent=2) + "\n")

    def flash(dwell):
        result = _flash(root, dwell)
        record["flashes"].append({"dwell_us": dwell, "path": str(result), "sha256": sha256(result)})
        save()
        return result

    def acquire(row, evidence):
        print(f"[muted-switch] {row}", flush=True)
        result, run = capture(
            [
                sys.executable,
                str(ROOT / "scripts/capture_rate_timing.py"),
                "--configuration",
                "A",
                "--mode",
                "fast-ambient",
                "--duration-s",
                "4",
                "--frequency-hz",
                "5811000000",
                "--gain-db",
                "60",
                "--profile",
                str(ROOT / f"profiles/tracking-c6-{row['dwell_us']}us-v1/control_profile.json"),
                "--flash-evidence",
                str(evidence),
                "--output-root",
                str(root),
                "--protocol-json",
                str(ROOT / "docs/comprehensive_fast_switching/data/protocol-v1.json"),
                "--tag",
                f"{identifier}-r{row['round']}-{row['dwell_us']}us",
            ]
        )
        record["captures"].append(
            {**row, "run_json": str(result), "sha256": sha256(result), "status": run["status"]}
        )
        save()
        if run["status"] != "passed":
            raise RuntimeError("source-muted acquisition failed; no silent replacement")

    def restore(original):
        result = _restore(root, original)
        record["restores"].append({"path": str(result), "sha256": sha256(result)})
        save()

    try:
        with _lock(root / ".block.lock"):
            run_switched(rows, flash=flash, acquire=acquire, restore=restore)
        record["status"] = "completed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save()
        print(f"switched_muted_evidence={path}", flush=True)


if __name__ == "__main__":
    main()
