#!/usr/bin/env python3
"""Profile the unchanged timing recipe on one saved 50 ms output window."""

import argparse
import cProfile
import json
import pstats
from pathlib import Path

import numpy as np
from analyze_comprehensive_bearings import ROOT, block_references

from smateway.fast_tracking import FastTrackingProfile
from smateway.rate_timing import PORTS, complex_value, load, sha256
from smateway.reference_timing import rolling_reference_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for block_id in ("block-20260908T225855806955Z", "block-20260908T231415311061Z"):
        path = args.campaign_root / f"{block_id}.json"
        block = load(path)
        refs, _, _ = block_references(block)
        item = next(
            r
            for r in block["captures"]
            if r["dwell_us"] == 100 and r["round"] == 1 and not r["control"]
        )
        run_path = Path(item["run_json"])
        if sha256(run_path) != item["sha256"]:
            raise ValueError("capture hash differs")
        run = load(run_path)
        fs, name = run["configuration"]["sample_rate_hz"], run["configuration"]["name"]
        raw = run["capture"]["raw"]
        if any(sha256(Path(r["path"])) != r["sha256"] for r in raw):
            raise ValueError("raw hash differs")
        one, two = [
            np.memmap(r["path"], dtype=np.complex64, mode="r")[: fs + fs // 20] for r in raw
        ]
        expected = [complex_value(refs[name]["before"][p]["reference"]["transfer"]) for p in PORTS]
        profile = FastTrackingProfile.load(
            ROOT / "profiles/tracking-c6-100us-v1/control_profile.json"
        )
        for repeat in range(3):
            profiler = cProfile.Profile()
            profiler.enable()
            windows = rolling_reference_windows(one, two, profile, expected, fs=fs)
            profiler.disable()
            stats = pstats.Stats(profiler)
            functions = [
                {
                    "file": key[0],
                    "line": key[1],
                    "function": key[2],
                    "calls": value[1],
                    "self_s": value[2],
                    "cumulative_s": value[3],
                }
                for key, value in stats.stats.items()
            ]
            results.append(
                {
                    "block": str(path),
                    "block_sha256": sha256(path),
                    "configuration": name,
                    "run_json": str(run_path),
                    "run_sha256": sha256(run_path),
                    "repeat": repeat + 1,
                    "total_profiled_s": stats.total_tt,
                    "window": windows[0],
                    "functions": sorted(functions, key=lambda r: r["self_s"], reverse=True),
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema": 1,
                "status": "analyzed",
                "source_sha256": sha256(Path(__file__)),
                "scope": "Three cProfile replays per rate of the first 1 s/50 ms window; "
                "not independent RF measurements or an isolated real-time benchmark. "
                "Profiler adds overhead.",
                "rows": results,
            },
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    print(f"profile_evidence={args.output}")


if __name__ == "__main__":
    main()
