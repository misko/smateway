#!/usr/bin/env python3
"""Re-use frozen past-only timing; compare an intercept estimator separately."""

import argparse
import json
from pathlib import Path

import numpy as np
from analyze_comprehensive_bearings import ROOT, block_references

from smateway.affine_transfer import affine_interval_transfer
from smateway.causal_timing import FrozenTiming, predict_intervals
from smateway.fast_tracking import FastTrackingProfile
from smateway.rate_timing import (
    PORTS,
    complex_json,
    complex_value,
    integration_metrics,
    load,
    sha256,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timing-report", type=Path, required=True)
    parser.add_argument("--dwell-us", type=int, default=25)
    parser.add_argument("--round", type=int, default=1)
    args = parser.parse_args()
    timing = load(args.timing_report)
    block_path = Path(timing["block"])
    if sha256(block_path) != timing["block_sha256"]:
        raise ValueError("block hash differs")
    refs, frozen, drift = block_references(load(block_path))
    row = next(
        r
        for r in timing["rows"]
        if r["configuration"]["dwell_us"] == args.dwell_us
        and r["round"] == args.round
        and not r["control"]
    )
    run_path = Path(row["run_json"])
    if sha256(run_path) != row["sha256"]:
        raise ValueError("run hash differs")
    run = load(run_path)
    cfg = run["configuration"]
    raw = run["capture"]["raw"]
    if any(sha256(Path(r["path"])) != r["sha256"] for r in raw):
        raise ValueError("raw hash differs")
    one, two = [np.memmap(r["path"], dtype=np.complex64, mode="r") for r in raw]
    profile = FastTrackingProfile.load(
        ROOT / f"profiles/tracking-c6-{args.dwell_us}us-v1/control_profile.json"
    )
    expected = [
        complex_value(refs[cfg["name"]]["before"][p]["reference"]["transfer"]) for p in PORTS
    ]
    result = {
        "schema": 1,
        "scope": "exploratory intercept comparator; frozen v1 result unchanged",
        "timing_report": str(args.timing_report),
        "timing_sha256": sha256(args.timing_report),
        "run_json": str(run_path),
        "run_sha256": sha256(run_path),
        "configuration": cfg,
        "reference_drift": drift,
        "frozen_reference": frozen,
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (Path(__file__).resolve(), ROOT / "src/smateway/affine_transfer.py")
        },
        "frozen_v1_metrics": row.get("rolling_past_only", {}).get("metrics"),
        "windows": [],
    }
    for window in row["rolling_past_only"]["windows"]:
        result_window = {
            "start": window["prediction_start_sample"],
            "stop": window["prediction_stop_sample"],
        }
        try:
            if window["status"] != "analyzed":
                raise ValueError("frozen timing window failed; no replacement")
            start, stop = result_window["start"], result_window["stop"]
            clock = FrozenTiming(
                start,
                cfg["sample_rate_hz"],
                window["origin_sample"],
                window["cycle_samples"],
                0,
                None,
                0,
            )
            a, b = predict_intervals(clock, profile, stop)
            h, intercept, fraction = affine_interval_transfer(
                one[start:stop], two[start:stop], a - start, b - start
            )
            result_window.update(
                {
                    "status": "analyzed",
                    "mean_transfer": [complex_json(v) for v in h.mean(axis=0)],
                    "mean_intercept_counts": [complex_json(v) for v in intercept.mean(axis=0)],
                    "minimum_reference_energy_fraction": float(fraction.min()),
                }
            )
        except ValueError as error:
            result_window.update({"status": "analysis-failed", "error": str(error)})
        result["windows"].append(result_window)
    result["complete"] = all(w["status"] == "analyzed" for w in result["windows"])
    if result["complete"]:
        matrix = [[complex_value(v) for v in w["mean_transfer"]] for w in result["windows"]]
        result["intercept_metrics"] = integration_metrics(
            matrix,
            cycle_ms=50,
            weights=frozen["weights"],
            references=expected,
            observable=frozen["observable"],
        )
    output = block_path.with_name(
        block_path.stem + f"-intercept-{args.dwell_us}us-r{args.round}.json"
    )
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"intercept_evidence={output}")


if __name__ == "__main__":
    main()
