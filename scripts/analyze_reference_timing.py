#!/usr/bin/env python3
"""Separately labeled prefix-only reference-origin experiment; no legacy relabeling."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
from analyze_comprehensive_bearings import ROOT, block_references

from smateway.causal_timing import FrozenTiming, predict_intervals
from smateway.fast_tracking import FastTrackingProfile, decode_fast_schedule
from smateway.rate_timing import (
    PORTS,
    coarse_product,
    complex_value,
    integration_metrics,
    interval_moments,
    load,
    native_fold,
    sha256,
)
from smateway.reference_timing import (
    align_reference_fold,
    rolling_reference_windows,
    train_reference_timing,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--rolling", action="store_true")
    args = parser.parse_args()
    block = load(args.block)
    refs, frozen, drift = block_references(block)
    result = {
        "schema": 1,
        "status": "running",
        "block": str(args.block),
        "block_sha256": sha256(args.block),
        "scope": "exploratory prefix-only label alignment; no new held-out campaign validation",
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                Path(__file__).resolve(),
                ROOT / "src/smateway/reference_timing.py",
                ROOT / "src/smateway/causal_timing.py",
                ROOT / "src/smateway/rate_timing.py",
                ROOT / "src/smateway/fast_tracking.py",
            )
        },
        "frozen_reference": frozen,
        "reference_drift": drift,
        "rows": [],
    }
    output = args.block.with_name(args.block.stem + "-reference-timing.json")
    for item in block["captures"]:
        path = Path(item["run_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("capture record hash differs")
        run = load(path)
        cfg = run["configuration"]
        row = {
            "run_json": str(path),
            "sha256": sha256(path),
            "configuration": cfg,
            "round": item["round"],
            "control": item["control"],
        }
        print(f"[reference-origin] {path.parent.name}", flush=True)
        try:
            if run["status"] != "passed":
                raise ValueError("failed acquisition remains failed")
            vectors = []
            for raw in run["capture"]["raw"]:
                raw_path = Path(raw["path"])
                if (
                    sha256(raw_path) != raw["sha256"]
                    or raw_path.stat().st_size != run["capture"]["samples_per_channel"] * 8
                ):
                    raise ValueError("IQ hash or length differs")
                vectors.append(np.memmap(raw_path, dtype=np.complex64, mode="r"))
            one, two = vectors
            fs = cfg["sample_rate_hz"]
            expected = np.array(
                [
                    complex_value(refs[cfg["name"]]["before"][p]["reference"]["transfer"])
                    for p in PORTS
                ]
            )
            profile = FastTrackingProfile.load(
                ROOT / f"profiles/tracking-c6-{cfg['dwell_us']}us-v1/control_profile.json"
            )
            model, alignment = train_reference_timing(one, two, profile, expected, fs=fs)
            left, right = predict_intervals(model, profile, len(one))
            cross, power = interval_moments(one, two, left, right, fs=fs)
            row.update(
                {
                    "status": "analyzed",
                    "model": asdict(model),
                    "alignment": alignment,
                    "unseen_prefix_boundary_samples": model.training_samples,
                    "heldout": integration_metrics(
                        cross / np.maximum(power, 1e-300),
                        cycle_ms=model.cycle_samples / fs * 1000,
                        weights=frozen["weights"],
                        references=expected,
                        observable=frozen["observable"],
                    ),
                }
            )
            # Deliberately separate exploratory whole-record label diagnosis.
            # This must never replace the preceding held-out result or old policy.
            decode = decode_fast_schedule(
                coarse_product(one, two, fs),
                sample_rate_hz=1000000,
                tone_offset_hz=0,
                profile=profile,
                edge_trim_us=5,
            )
            cycle_hz = decode.periodicity_frequency_hz or 1e6 / (
                profile.cycle_us * decode.cycle_scale_median
            )
            marker = decode.marker_end_bins[0]
            folded, _ = native_fold(
                one, two, fs=fs, cycle_hz=cycle_hz, marker_us=marker, cycle_us=profile.cycle_us
            )
            retrospective_alignment = align_reference_fold(folded, expected, profile)
            model_all = FrozenTiming(
                0,
                fs,
                marker * fs / 1e6
                + retrospective_alignment["shift_bins"] / len(folded) * fs / cycle_hz,
                fs / cycle_hz,
                0,
                None,
                0,
            )
            a, b = predict_intervals(model_all, profile, len(one))
            cross_all, power_all = interval_moments(one, two, a, b, fs=fs)
            row["retrospective_reference_labeled"] = {
                "scope": "whole-record exploratory timing fit; not held-out validation",
                "alignment": retrospective_alignment,
                "metrics": integration_metrics(
                    cross_all / np.maximum(power_all, 1e-300),
                    cycle_ms=1000 / cycle_hz,
                    weights=frozen["weights"],
                    references=expected,
                    observable=frozen["observable"],
                ),
            }
            if args.rolling:
                windows = rolling_reference_windows(one, two, profile, expected, fs=fs)
                rolling = {
                    "scope": "1 s lookback / 50 ms hop, past-only stored-IQ replay",
                    "windows": windows,
                    "complete": all(w["status"] == "analyzed" for w in windows),
                }
                # Never compress failed windows into a falsely continuous time grid.
                if rolling["complete"]:
                    transfers = np.array(
                        [[complex_value(v) for v in w["mean_transfer"]] for w in windows]
                    )
                    rolling["metrics"] = integration_metrics(
                        transfers,
                        cycle_ms=50,
                        weights=frozen["weights"],
                        references=expected,
                        observable=frozen["observable"],
                    )
                row["rolling_past_only"] = rolling
        except ValueError as error:
            row.update({"status": "analysis-failed", "error": str(error)})
        result["rows"].append(row)
        output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    result["status"] = "analyzed"
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"reference_timing_evidence={output}", flush=True)


if __name__ == "__main__":
    main()
