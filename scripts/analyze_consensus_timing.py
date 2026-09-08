#!/usr/bin/env python3
"""Bounded exploratory harmonic-consensus replay; frozen v1 is unchanged."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

import numpy as np
from analyze_comprehensive_bearings import ROOT, block_references

from smateway.causal_timing import predict_intervals
from smateway.fast_tracking import FastTrackingProfile
from smateway.harmonic_consensus import train_consensus_timing
from smateway.rate_timing import (
    PORTS,
    complex_json,
    complex_value,
    integration_metrics,
    interval_moments,
    load,
    sha256,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--dwell-us", type=int, default=25)
    parser.add_argument("--round", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--clock-input", choices=("cross", "rx2"), default="cross")
    args = parser.parse_args()
    block = load(args.block)
    refs, frozen, drift = block_references(block)
    item = next(
        r
        for r in block["captures"]
        if r["dwell_us"] == args.dwell_us and r["round"] == args.round and not r["control"]
    )
    path = Path(item["run_json"])
    if sha256(path) != item["sha256"]:
        raise ValueError("record hash differs")
    run = load(path)
    if run["status"] != "passed":
        raise ValueError("passed acquisition required")
    raw = run["capture"]["raw"]
    if any(sha256(Path(r["path"])) != r["sha256"] for r in raw):
        raise ValueError("raw hash differs")
    one, two = [np.memmap(r["path"], dtype=np.complex64, mode="r") for r in raw]
    cfg = run["configuration"]
    fs, hop = cfg["sample_rate_hz"], cfg["sample_rate_hz"] // 20
    expected = [
        complex_value(refs[cfg["name"]]["before"][p]["reference"]["transfer"]) for p in PORTS
    ]
    profile = FastTrackingProfile.load(
        ROOT / f"profiles/tracking-c6-{args.dwell_us}us-v1/control_profile.json"
    )
    output = args.block.with_name(
        args.block.stem + f"-consensus-{args.clock_input}-{args.dwell_us}us-r{args.round}.json"
    )
    report = {
        "schema": 1,
        "status": "running",
        "scope": "exploratory stored-IQ replay, no v1 relabeling",
        "block_path": str(args.block),
        "block_sha256": sha256(args.block),
        "run_json": str(path),
        "run_sha256": sha256(path),
        "configuration": cfg,
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                Path(__file__).resolve(),
                ROOT / "src/smateway/harmonic_consensus.py",
                ROOT / "src/smateway/reference_timing.py",
                ROOT / "src/smateway/fast_tracking.py",
                ROOT / "src/smateway/rate_timing.py",
            )
        },
        "reference_drift": drift,
        "frozen_reference": frozen,
        "windows": [],
    }
    for start in range(fs, len(one) - hop + 1, hop):
        clock_start = perf_counter()
        training_start = start - fs
        window = {
            "training_start_sample": training_start,
            "training_stop_sample": start,
            "prediction_start_sample": start,
            "prediction_stop_sample": start + hop,
            "observation_ms": 50,
        }
        try:
            model, detail = train_consensus_timing(
                one[training_start:],
                two[training_start:],
                profile,
                expected,
                fs=fs,
                clock_input=args.clock_input,
            )
            left, right = predict_intervals(model, profile, fs + hop)
            left, right = left - fs, right - fs
            cross, power = interval_moments(
                one[start : start + hop], two[start : start + hop], left, right, fs=fs
            )
            h = cross / np.maximum(power, 1e-300)
            window.update(
                {
                    "status": "analyzed",
                    "model": asdict(model),
                    "fit": detail,
                    "cycles": len(h),
                    "mean_transfer": [complex_json(v) for v in h.mean(axis=0)],
                    "usable_per_port_ms": (np.sum(right - left, axis=0) / fs * 1000).tolist(),
                }
            )
        except ValueError as error:
            window.update({"status": "analysis-failed", "error": str(error)})
        window["host_replay_compute_s"] = perf_counter() - clock_start
        report["windows"].append(window)
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    report["complete"] = all(w["status"] == "analyzed" for w in report["windows"])
    if report["complete"]:
        matrix = np.array(
            [[complex_value(v) for v in w["mean_transfer"]] for w in report["windows"]]
        )
        report["metrics"] = integration_metrics(
            matrix,
            cycle_ms=50,
            weights=frozen["weights"],
            references=expected,
            observable=frozen["observable"],
        )
    report["status"] = "analyzed"
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"consensus_evidence={output}")


if __name__ == "__main__":
    main()
