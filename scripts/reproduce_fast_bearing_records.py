#!/usr/bin/env python3
"""Recompute named historical successes/failures from hash-verified raw IQ; no RF."""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from smateway.causal_timing import analyze_causal_bearings, bearing_study
from smateway.fast_tracking import (
    FastTrackingProfile,
    analyze_fast_bearings,
    coherent_product,
    decode_fast_schedule,
    estimate_frequency_difference,
)
from smateway.rate_timing import PORTS, analyze_rate_capture, load, sha256
from smateway.tracking.calibration import BoardCalibrationLut
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import load_ism_band_profiles

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = Path("/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v6/captures")
REFERENCE_CAMPAIGN = Path(
    "/srv/bulk/samteway/lab-data/tracking-rate-timing-20260908-v2/campaign.json"
)
TARGETS = (
    ("fast-200us-5811000000-tx1-20260903T213133.733492Z", 16),
    ("fast-200us-5750000000-tx2-20260903T210325.122871Z", 2),
    ("fast-100us-5775000000-tx2-20260903T204100.664291Z", 8),
    ("fast-200us-5800000000-tx1-20260903T212634.784215Z", 32),
)


def compare_fields(expected, observed, fields, *, tolerance=1e-8):
    differences = {}
    for field in fields:
        old, new = expected[field], observed[field]
        if isinstance(old, bool) or old is None:
            if old != new:
                differences[field] = {"stored": old, "recomputed": new}
        elif not math.isfinite(float(new)) or not math.isclose(
            old, new, rel_tol=0, abs_tol=tolerance
        ):
            differences[field] = {"stored": old, "recomputed": new}
    return differences


def reproduce_bearing(path: Path, cycles: int) -> dict:
    run = load(path)
    cfg, capture = run["configuration"], run["capture"]
    if run["status"] != "passed":
        raise ValueError("historical acquisition was not admitted")
    raw = capture["raw"]
    vectors = []
    for channel in ("rx1", "rx2"):
        raw_path = Path(raw[channel + "_path"])
        if sha256(raw_path) != raw[channel + "_sha256"]:
            raise ValueError("historical raw hash differs")
        if raw_path.stat().st_size != capture["total_samples_per_channel"] * 8:
            raise ValueError("historical raw length differs")
        vectors.append(np.memmap(raw_path, mode="r", dtype=np.complex64))
    profile_path = Path(run["profile"]["path"])
    calibration_path = Path(run["board_calibration"]["path"])
    if sha256(profile_path) != run["profile"]["sha256"]:
        raise ValueError("historical timing profile hash differs")
    if sha256(calibration_path) != run["board_calibration"]["sha256"]:
        raise ValueError("historical PCB LUT hash differs")
    profile = FastTrackingProfile.load(profile_path)
    fs = cfg["sample_rate_hz"]
    one, two = vectors
    decode = decode_fast_schedule(
        np.asarray(two * one.conj(), dtype=np.complex64),
        sample_rate_hz=fs,
        tone_offset_hz=0,
        profile=profile,
        edge_trim_us=cfg["edge_trim_us"],
    )
    difference = None
    fit = None
    if cfg["tx_channel"] == 1:
        fit = estimate_frequency_difference(
            one,
            two,
            decode=decode,
            sample_rate_hz=fs,
            nominal_difference_hz=capture["source_readback"]["frequency_difference_hz"],
        )
        difference = fit.frequency_difference_hz
    product = coherent_product(
        one,
        two,
        sample_rate_hz=fs,
        first_sample_sequence=capture["timeline"][0]["first_sample_sequence"],
        difference_hz=difference,
    )
    plan_path = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
    geometry = load_ism_band_profiles(plan_path)["ism5800-c6-v1"].geometry
    calibration = BoardCalibrationLut.load(calibration_path).evaluate(cfg["frequency_hz"], PORTS)
    grid = np.arange(0.0, 360.0, 0.25)
    study = analyze_fast_bearings(
        product,
        decode=decode,
        profile=profile,
        calibration_coefficients=calibration.coefficients,
        steering=far_field_steering(geometry, cfg["frequency_hz"], grid),
        bearings_deg=grid,
        expected_bearing_deg=run["analysis"]["bearing_study"]["expected_bearing_deg_approximate"],
        grouping_cycles=(cycles,),
    )
    observed = study["integration_study"][0]
    expected = next(
        r
        for r in run["analysis"]["bearing_study"]["integration_study"]
        if r["cycles_averaged"] == cycles
    )
    differences = compare_fields(expected, observed, tuple(expected))
    causal = None
    causal_error = None
    try:
        causal = analyze_causal_bearings(
            one,
            two,
            profile,
            fs=fs,
            coefficients=calibration.coefficients,
            steering=far_field_steering(geometry, cfg["frequency_hz"], grid),
            bearing_grid=grid,
            nominal_difference_hz=capture["source_readback"]["frequency_difference_hz"],
            grouping_cycles=(1, 2, 4, 8, 16, 32, 64, 128, 256),
        )
    except ValueError as error:
        causal_error = str(error)
    heldout_vectors = []
    intervals = decode.intervals
    for start in range(0, len(intervals), 6):
        frame = intervals[start : start + 6]
        if len(frame) == 6 and min(i.start for i in frame) >= fs:
            heldout_vectors.append([np.mean(product[i.start : i.stop]) for i in frame])
    retrospective_heldout = bearing_study(
        heldout_vectors,
        cycle_ms=(
            1e3 / decode.periodicity_frequency_hz
            if decode.periodicity_frequency_hz
            else profile.cycle_us * decode.cycle_scale_median / 1000
        ),
        coefficients=calibration.coefficients,
        steering=far_field_steering(geometry, cfg["frequency_hz"], grid),
        bearing_grid=grid,
        grouping_cycles=(1, 2, 4, 8, 16, 32, 64, 128, 256),
    )
    return {
        "run_json": str(path),
        "run_sha256": sha256(path),
        "configuration": cfg,
        "reproduction_passed": not differences,
        "differences": differences,
        "stored": expected,
        "recomputed": observed,
        "recomputed_full_capture_bearing": study["full_capture_reference"],
        "frequency_difference_fit": asdict(fit) if fit else None,
        "recomputed_decode": {k: v for k, v in asdict(decode).items() if k != "intervals"},
        "geometry_id": geometry.geometry_id,
        "geometry_positions_m": geometry.positions_m.tolist(),
        "geometry_plan_sha256": sha256(plan_path),
        "causal_open_loop_replay": causal,
        "causal_error": causal_error,
        "retrospective_same_time_holdout": retrospective_heldout,
        "scope": "historical raw replay; retrospective sync; no new RF or surveyed truth",
    }


def reproduce_independent_closure():
    campaign = load(REFERENCE_CAMPAIGN)
    references = {
        r["port"]: Path(r["run_json"])
        for r in campaign["references"]
        if r["configuration"] == "A" and r["position"] == "before"
    }
    result = []
    for row in campaign["screen"]:
        if row["control"] or row["configuration"] != "A" or row["dwell_us"] != 200:
            continue
        print(f"[reference-replay] A/200 round {row['round']}", flush=True)
        previous = load(Path(row["analysis_json"]))
        if sha256(Path(row["analysis_json"])) != row["analysis_sha256"]:
            raise ValueError("historical independent-analysis hash differs")
        current = analyze_rate_capture(Path(row["run_json"]), references, references)

        def chosen(record):
            return next(
                v["metrics"]
                for v in record["variants"]
                if v["method"] == "native_refined" and v["leading_discard_us"] == 0
            )

        old, new = chosen(previous), chosen(current)
        differences = compare_fields(old, new, ("passed", "first_phase_pass_ms"))
        differences.update(
            compare_fields(
                old["closure"],
                new["closure"],
                (
                    "weighted_phase_bias_deg",
                    "maximum_observable_phase_bias_deg",
                    "maximum_observable_gain_error_db",
                    "passed",
                ),
            )
        )
        result.append(
            {
                "round": row["round"],
                "run_json": row["run_json"],
                "reproduction_passed": not differences,
                "differences": differences,
                "independent_qualification_passed": new["passed"],
                "first_phase_pass_ms": new["first_phase_pass_ms"],
                "closure": new["closure"],
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/comprehensive_fast_switching/data/reproduction-v1.json",
    )
    args = parser.parse_args()
    started = time.monotonic()
    rows = []
    for name, cycles in TARGETS:
        print(f"[bearing-replay] {name} / {cycles} cycles", flush=True)
        row = reproduce_bearing(HISTORICAL / name / "run.json", cycles)
        rows.append(row)
        print(
            f"[reproduced] match={row['reproduction_passed']} "
            f"valid={row['recomputed']['valid_percent']:.4f}% "
            f"RMS={row['recomputed']['bearing_repeatability_rms_deg']:.4f}deg",
            flush=True,
        )
    independent = reproduce_independent_closure()
    output = {
        "schema": 1,
        "scope": "historical-only replay; not fresh validation",
        "protocol_sha256": sha256(ROOT / "docs/comprehensive_fast_switching/data/protocol-v1.json"),
        "bearing_reproductions": rows,
        "independent_reproductions": independent,
        "reproduction_passed": all(r["reproduction_passed"] for r in rows + independent),
        "wall_seconds": time.monotonic() - started,
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                Path(__file__).resolve(),
                ROOT / "src/smateway/fast_tracking.py",
                ROOT / "src/smateway/rate_timing.py",
                ROOT / "src/smateway/causal_timing.py",
                ROOT / "src/smateway/tracking/bearing.py",
                ROOT / "src/smateway/tracking/calibration.py",
                ROOT / "src/smateway/tracking/manifold.py",
                ROOT / "src/smateway/tracking/schedule.py",
            )
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(f"reproduction_json={args.output}", flush=True)
    return 0 if output["reproduction_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
