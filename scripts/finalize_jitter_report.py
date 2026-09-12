#!/usr/bin/env python3
"""Offline-only progress renderer and finalizer for the bounded jitter campaign."""

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from analyze_jitter_comparison import DEFAULT_ROOT, ROOT, save

from smateway.rate_timing import load, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.campaign_root.resolve(strict=True)
    evidence = root / "analysis/finalizer.json"
    if evidence.exists():
        raise SystemExit("Finalizer evidence exists; no silent replacement")
    state = {
        "status": "running",
        "source_sha256": sha256(Path(__file__)),
        "renders": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    previous = -1

    def run(script, *args):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--campaign-root", str(root), *args],
            check=True,
            cwd=ROOT,
        )

    try:
        save(evidence, state)
        while True:
            follower_path = root / "analysis/follower.json"
            follower = load(follower_path) if follower_path.exists() else {"status": "running"}
            count = len(follower.get("blocks", []))
            if count != previous:
                run("render_jitter_report.py")
                state["renders"].append({"blocks": count, "at": datetime.now(UTC).isoformat()})
                save(evidence, state)
                previous = count
            if follower["status"] == "failed":
                raise RuntimeError("Offline follower failed; partial evidence remains visible")
            if follower["status"] == "complete":
                run("analyze_jitter_comparison.py", "--stage", "static")
                run("analyze_jitter_comparison.py", "--stage", "dense")
                run("render_jitter_report.py")
                summary = load(root / "analysis/report-summary.json")
                if summary["status"] != "complete":
                    raise ValueError("All planned evidence must be accounted for")
                state["status"] = "complete"
                break
            time.sleep(15)
    except BaseException as error:
        state["status"] = "failed"
        state["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        state["updated_at"] = datetime.now(UTC).isoformat()
        save(evidence, state)


if __name__ == "__main__":
    main()
