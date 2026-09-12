#!/usr/bin/env python3
"""Offline-only follower and report finalizer for the 5 MS/s acquisition."""

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from analyze_full_5ms_dense import analyze, compact
from analyze_jitter_comparison import save, table
from run_5ms_full_campaign import OUTPUT, ROOT

from smateway.rate_timing import load, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=OUTPUT)
    args = parser.parse_args()
    root = args.campaign_root.resolve(strict=True)
    path = root / "analysis/follower.json"
    if path.exists():
        raise SystemExit("Follower evidence already exists; no silent overwrite")
    state = {
        "status": "running",
        "blocks": [],
        "dense": [],
        "started_at": datetime.now(UTC).isoformat(),
        "source_sha256": sha256(Path(__file__)),
    }
    seen, dense_seen, dense_rows = set(), set(), []
    continuation = load(root / "plan.json").get("continuation")
    if continuation:
        parent = Path(continuation["parent"])
        previous = load(parent / "analysis/follower.json")
        admitted = {r["block_json"]: r["sha256"] for r in continuation["inherited_complete_blocks"]}
        for item in previous["blocks"]:
            if (
                admitted.get(item["block_json"]) != item["sha256"]
                or sha256(Path(item["block_json"])) != item["sha256"]
                or sha256(Path(item["timing_json"])) != item["timing_sha256"]
            ):
                raise ValueError("Inherited analysis hashes differ")
            state["blocks"].append(item)
            seen.add(item["block_json"])
        state["inherited_follower_sha256"] = sha256(parent / "analysis/follower.json")

    def render():
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/report_full_5ms_campaign.py"),
                "--campaign-root",
                str(root),
            ],
            cwd=ROOT,
            check=True,
        )

    try:
        save(path, state)
        render()
        while True:
            blocks = load(root / "blocks.json")
            for item in blocks["blocks"]:
                if item.get("block_json") in seen:
                    continue
                if item.get("status") != "diagnostic-complete":
                    raise RuntimeError(f"Failed acquisition block retained: {item}")
                p = Path(item["block_json"])
                if sha256(p) != item["sha256"]:
                    raise ValueError("Completed block hash differs")
                log = root / "analysis" / f"{p.stem}-rolling.log"
                print(f"[rolling] {p.name}", flush=True)
                with log.open("x") as stream:
                    subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/analyze_reference_timing.py"),
                            "--block",
                            str(p),
                            "--rolling",
                        ],
                        cwd=ROOT,
                        check=True,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                    )
                timing = p.with_name(p.stem + "-reference-timing.json")
                if load(timing)["status"] != "analyzed":
                    raise ValueError("Rolling analysis incomplete")
                state["blocks"].append(
                    {
                        "block_json": str(p),
                        "sha256": item["sha256"],
                        "timing_json": str(timing),
                        "timing_sha256": sha256(timing),
                    }
                )
                seen.add(str(p))
                save(path, state)
                render()
            dense_path = root / "dense.json"
            dense = load(dense_path) if dense_path.exists() else None
            if dense:
                for item in dense["captures"]:
                    if item["run_json"] in dense_seen:
                        continue
                    result = analyze(item, root)
                    row = compact(item, result)
                    dense_rows.append(row)
                    dense_seen.add(item["run_json"])
                    state["dense"].append(
                        {
                            "run_json": item["run_json"],
                            "sha256": item["sha256"],
                            "analysis_status": result["status"],
                        }
                    )
                    save(path, state)
                    table(root / "analysis/dense-after.csv", dense_rows)
                    print(
                        f"[dense analyzed {len(dense_rows)}/298] "
                        f"{row['frequency_hz'] / 1e6:g} {row['tx_port']} {row['status']}",
                        flush=True,
                    )
            if blocks["status"] == "failed" or (dense and dense["status"] == "failed"):
                raise RuntimeError("Acquisition stopped; partial evidence retained")
            if blocks["status"] == "complete" and dense and dense["status"] == "complete":
                if len(seen) != 8 or len(dense_rows) != 298:
                    raise ValueError("Planned analysis grid is incomplete")
                save(
                    root / "analysis/dense-summary.json",
                    {
                        "manifest_sha256": sha256(dense_path),
                        "analyzed_records": len(dense_rows),
                        "analysis_successes": sum(r["status"] == "analyzed" for r in dense_rows),
                    },
                )
                render()
                if load(root / "analysis/report-summary.json")["status"] != "complete":
                    raise ValueError("Final report validation failed")
                state["status"] = "complete"
                break
            time.sleep(10)
    except BaseException as error:
        state.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        save(path, state)
        try:
            render()
        except Exception as report_error:
            state["report_error"] = str(report_error)
        raise
    finally:
        state["updated_at"] = datetime.now(UTC).isoformat()
        save(path, state)


if __name__ == "__main__":
    main()
