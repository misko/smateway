#!/usr/bin/env python3
"""Offline-only follower: analyze completed immutable blocks and dense captures.

Does not discover, open, transmit, flash or control any hardware. Acquisition
failures stop the follower without silently retrying measurements.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from analyze_jitter_comparison import DEFAULT_ROOT, ROOT, analyze_dense_run, save

from smateway.rate_timing import load, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.campaign_root.resolve(strict=True)
    output = root / "analysis/follower.json"
    if output.exists():
        raise SystemExit("Follower evidence exists; inspect it before a separate explicit replay")
    record = {
        "status": "running",
        "blocks": [],
        "dense": [],
        "started_at": datetime.now(UTC).isoformat(),
        "source_sha256": sha256(Path(__file__)),
    }
    analyzed_blocks, analyzed_runs = set(), set()
    continuation = root / "continuation.json"
    if continuation.exists():
        parent = Path(load(continuation)["parent"])
        inherited = load(parent / "analysis/follower.json")
        admitted = {
            r["block_json"]: r["sha256"] for r in load(continuation)["inherited_complete_blocks"]
        }
        for item in inherited["blocks"]:
            if (
                admitted.get(item["block_json"]) != item["sha256"]
                or sha256(Path(item["block_json"])) != item["sha256"]
                or sha256(Path(item["timing_json"])) != item["timing_sha256"]
            ):
                raise ValueError("Inherited analysis source hash differs")
            record["blocks"].append(item)
            analyzed_blocks.add(item["block_json"])
        record["inherited_follower"] = {
            "path": str(parent / "analysis/follower.json"),
            "sha256": sha256(parent / "analysis/follower.json"),
        }
    save(output, record)
    try:
        while True:
            blocks = load(root / "blocks.json")
            for item in blocks["blocks"]:
                path = Path(item["block_json"])
                if str(path) in analyzed_blocks:
                    continue
                if sha256(path) != item["sha256"]:
                    raise ValueError("Block integrity differs")
                if item["status"] != "diagnostic-complete":
                    raise RuntimeError(f"Acquisition block failed; evidence retained: {path}")
                print(f"[rolling] {path.name}", flush=True)
                log = root / "analysis" / f"{path.stem}-rolling.log"
                with log.open("x") as stream:
                    subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/analyze_reference_timing.py"),
                            "--block",
                            str(path),
                            "--rolling",
                        ],
                        check=True,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        cwd=ROOT,
                    )
                timing = path.with_name(path.stem + "-reference-timing.json")
                if load(timing)["status"] != "analyzed":
                    raise ValueError("Rolling analysis incomplete")
                record["blocks"].append(
                    {
                        "block_json": str(path),
                        "sha256": item["sha256"],
                        "timing_json": str(timing),
                        "timing_sha256": sha256(timing),
                    }
                )
                analyzed_blocks.add(str(path))
                save(output, record)
            dense_path = root / "dense.json"
            dense = load(dense_path) if dense_path.exists() else None
            if dense:
                for item in dense["captures"]:
                    if item["run_json"] in analyzed_runs:
                        continue
                    result = analyze_dense_run(item, root)
                    record["dense"].append(
                        {
                            "run_json": item["run_json"],
                            "run_sha256": item["sha256"],
                            "analysis_status": result["status"],
                        }
                    )
                    analyzed_runs.add(item["run_json"])
                    print(f"[dense] {len(analyzed_runs)}/298 {result['status']}", flush=True)
                    save(output, record)
            if blocks["status"] == "failed" or (dense and dense["status"] == "failed"):
                raise RuntimeError("Acquisition stage failed; partial analyses retained")
            if (
                blocks["status"] == "complete"
                and dense
                and dense["status"] == "complete"
                and len(analyzed_runs) == len(dense["captures"])
            ):
                break
            time.sleep(10)
        record["status"] = "complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        record["updated_at"] = datetime.now(UTC).isoformat()
        save(output, record)


if __name__ == "__main__":
    main()
