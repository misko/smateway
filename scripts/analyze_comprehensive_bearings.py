#!/usr/bin/env python3
"""Fresh TX1 bearing/phase evaluation using separately bound current geometry."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from smateway.causal_timing import bearing_study, predict_intervals, train_timing
from smateway.fast_tracking import FastTrackingProfile, decode_fast_schedule
from smateway.rate_timing import (
    PORTS,
    analyze_rate_capture,
    closure,
    coarse_product,
    complex_value,
    frozen_reference,
    integration_metrics,
    interval_moments,
    load,
    sha256,
)
from smateway.tracking.bearing import solve_bearing
from smateway.tracking.calibration import BoardCalibrationLut
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]


def block_references(block):
    """Check disjoint configuration references and keep A-only weights frozen."""
    configurations = block.get("reference_configurations", [block["configuration"]])
    if "A" not in configurations:
        raise ValueError("independent A weight references required")
    references = {name: {"before": {}, "after": {}} for name in configurations}
    for item in block["references"]:
        path = Path(item["run_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("independent reference hash differs")
        ref = load(path)
        cfg = ref["configuration"]
        name = item.get("configuration", block["configuration"])
        if (
            ref["status"] != "passed"
            or cfg["mode"] != "static"
            or cfg["name"] != name
            or cfg["frequency_hz"] != block["frequency_hz"]
            or cfg["receiver_gain_db"] != block["gain_db"]
            or cfg["port"] != item["port"]
            or name not in references
            or item["position"] not in ("before", "after")
        ):
            raise ValueError("independent reference configuration differs")
        target = references[name][item["position"]]
        if item["port"] in target:
            raise ValueError("duplicate independent reference")
        target[item["port"]] = ref
    if any(set(group["before"]) != set(PORTS) for group in references.values()):
        raise ValueError("six before-references per configuration required")
    frozen = frozen_reference({p: references["A"]["before"][p]["reference"] for p in PORTS})
    drift = {}
    for name, group in references.items():
        available = set(group["after"]) == set(PORTS)
        drift[name] = {"available": available, "passed": False}
        if available:
            vectors = {
                position: [
                    complex_value(group[position][p]["reference"]["transfer"]) for p in PORTS
                ]
                for position in ("before", "after")
            }
            drift[name].update(
                closure(
                    vectors["after"], vectors["before"], frozen["weights"], frozen["observable"]
                )
            )
    return references, frozen, drift


def analyze(run_path, *, fixture, refs, frozen, lut):
    run = load(run_path)
    cfg = run["configuration"]
    if run["status"] != "passed" or cfg["mode"] != "fast" or cfg["tx_channel"] != 0:
        raise ValueError("passed TX1 fast acquisition required")
    fs = cfg["sample_rate_hz"]
    profile = FastTrackingProfile.load(
        ROOT / f"profiles/tracking-c6-{cfg['dwell_us']}us-v1/control_profile.json"
    )
    if fixture["ports"] != list(PORTS) or fixture["positions_m"] is None:
        raise ValueError("current nominal geometry must be bound")
    geometry = ArrayGeometry(
        fixture["fixture_id"], PORTS, fixture["positions_m"], np.arange(6) * 60
    )
    grid = np.arange(0, 360, 0.25)
    steering = far_field_steering(geometry, cfg["frequency_hz"], grid)
    coefficients = lut.evaluate(cfg["frequency_hz"], PORTS).coefficients
    weights = np.asarray(frozen["weights"])
    mask = np.asarray(frozen["observable"])
    expected = np.array([complex_value(refs[p]["reference"]["transfer"]) for p in PORTS])
    independent_bearing = solve_bearing(expected * coefficients, steering, grid, weights=weights)
    vectors = []
    for raw in run["capture"]["raw"]:
        path = Path(raw["path"])
        if (
            sha256(path) != raw["sha256"]
            or path.stat().st_size != run["capture"]["samples_per_channel"] * 8
        ):
            raise ValueError("raw IQ hash/length differs")
        vectors.append(np.memmap(path, dtype=np.complex64, mode="r"))
    one, two = vectors
    decode = decode_fast_schedule(
        coarse_product(one, two, fs),
        sample_rate_hz=1_000_000,
        tone_offset_hz=0,
        profile=profile,
        edge_trim_us=5,
    )
    left = (
        np.ceil(np.array([i.predicted_start for i in decode.intervals]) * fs / 1e6 + 5 * fs / 1e6)
        .astype(int)
        .reshape(-1, 6)
    )
    right = (
        np.floor(np.array([i.predicted_stop for i in decode.intervals]) * fs / 1e6 - 5 * fs / 1e6)
        .astype(int)
        .reshape(-1, 6)
    )
    cycle_ms = (
        1000 / decode.periodicity_frequency_hz
        if decode.periodicity_frequency_hz
        else profile.cycle_us * decode.cycle_scale_median / 1000
    )

    def study(left, right, cycle_ms):
        cross, power = interval_moments(one, two, left, right, fs=fs)
        h = cross / np.maximum(power, 1e-300)
        common = dict(
            cycle_ms=cycle_ms,
            coefficients=coefficients,
            steering=steering,
            bearing_grid=grid,
            reference_bearing_deg=independent_bearing.bearing_deg,
        )
        return {
            "frames": len(h),
            "median_samples_per_port_visit": float(np.median(right - left)),
            "independent_phase": integration_metrics(
                h, cycle_ms=cycle_ms, weights=weights, references=expected, observable=mask
            ),
            "frozen_weight_bearings": bearing_study(h, weights=weights, **common),
            "equal_weight_bearings_diagnostic": bearing_study(h, **common),
        }

    result = {
        "run_json": str(run_path),
        "sha256": sha256(run_path),
        "configuration": cfg,
        "scope": "nominal-geometry bearing repeatability; no surveyed truth or live latency",
        "independent_reference_bearing": {
            "bearing_deg": independent_bearing.bearing_deg,
            "model_valid": independent_bearing.valid,
            "reasons": independent_bearing.reasons,
            "source": "separate before-static references; not angle ground truth",
        },
        "retrospective_full": study(left, right, cycle_ms),
        "retrospective_heldout": study(
            left[left.min(axis=1) >= fs], right[left.min(axis=1) >= fs], cycle_ms
        ),
        "causal_open_loop": None,
    }
    try:
        model = train_timing(one, two, profile, fs=fs, training_s=1)
        causal_left, causal_right = predict_intervals(model, profile, len(one))
        result["causal_open_loop"] = {
            "model": asdict(model),
            "scope": "1 s past-only training, no relock, no measured buffer/delivery latency",
            **study(causal_left, causal_right, model.cycle_samples / fs * 1000),
        }
    except ValueError as error:
        result["causal_error"] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--allow-incomplete-diagnostic", action="store_true")
    parser.add_argument("--phase-only", action="store_true")
    args = parser.parse_args()
    block = load(args.block)
    incomplete = block["status"] not in ("acquired", "diagnostic-complete")
    if incomplete and not args.allow_incomplete_diagnostic:
        raise SystemExit("block is not completely acquired/restored")
    if not block["restores"]:
        raise SystemExit("no recorded selector restoration")
    restoration = load(Path(block["restores"][-1]["path"]))
    if restoration["status"] != "passed" or not restoration["restored_flash"]["matches_backup"]:
        raise SystemExit("selector restoration is not verified")
    fixture_binding = block["binding"]["fixture"]
    fixture_path = Path(fixture_binding["path"])
    if sha256(fixture_path) != fixture_binding["sha256"]:
        raise ValueError("fixture file hash differs")
    fixture = load(fixture_path)
    refs, frozen, drift = block_references(block)
    if args.phase_only:
        paths = {
            name: {
                r["port"]: Path(r["run_json"])
                for r in block["references"]
                if r["position"] == "before"
                and r.get("configuration", block["configuration"]) == name
            }
            for name in refs
        }
        phase_report = {
            "schema": 1,
            "status": "running",
            "block_path": str(args.block),
            "block_sha256": sha256(args.block),
            "incomplete_block_diagnostic_only": incomplete,
            "reference_drift": drift,
            "rows": [],
        }
        output = args.block.with_name(args.block.stem + "-phase.json")
        for row in block["captures"]:
            path = Path(row["run_json"])
            if sha256(path) != row["sha256"]:
                raise ValueError("capture hash differs")
            name = load(path)["configuration"]["name"]
            print(f"[phase] {path.parent.name}", flush=True)
            if row.get("analysis_json"):
                analysis_path = Path(row["analysis_json"])
                if sha256(analysis_path) != row["analysis_sha256"]:
                    raise ValueError("existing analysis hash differs")
            else:
                try:
                    analysis = analyze_rate_capture(path, paths[name], paths["A"])
                except ValueError as error:
                    analysis = {"status": "analysis-failed", "error": str(error), "variants": []}
                analysis_path = path.parent / "independent-analysis.json"
                analysis_path.write_text(json.dumps(analysis, indent=2, allow_nan=False) + "\n")
            phase_report["rows"].append(
                {
                    **row,
                    "configuration": name,
                    "analysis_json": str(analysis_path),
                    "analysis_sha256": sha256(analysis_path),
                }
            )
            output.write_text(json.dumps(phase_report, indent=2, allow_nan=False) + "\n")
        phase_report["status"] = "analyzed"
        output.write_text(json.dumps(phase_report, indent=2, allow_nan=False) + "\n")
        return
    lut_path = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"
    lut = BoardCalibrationLut.load(lut_path)
    output = args.block.with_name(args.block.stem + "-bearings.json")
    result = {
        "schema": 1,
        "status": "running",
        "block_path": str(args.block),
        "block_sha256": sha256(args.block),
        "block_status": block["status"],
        "incomplete_block_diagnostic_only": incomplete,
        "after_reference_bracket_available": all(d["available"] for d in drift.values()),
        "independent_reference_drift": drift,
        "independent_reference_drift_passed": all(d["passed"] for d in drift.values()),
        "fixture": fixture,
        "frozen_reference": frozen,
        "lut_sha256": sha256(lut_path),
        "rows": [],
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                Path(__file__).resolve(),
                ROOT / "src/smateway/causal_timing.py",
                ROOT / "src/smateway/fast_tracking.py",
                ROOT / "src/smateway/rate_timing.py",
                ROOT / "src/smateway/tracking/bearing.py",
            )
        },
    }
    for row in block["captures"]:
        path = Path(row["run_json"])
        print(f"[bearing] {path.parent.name}", flush=True)
        if sha256(path) != row["sha256"]:
            raise ValueError("capture evidence hash differs")
        try:
            name = load(path)["configuration"]["name"]
            report = analyze(
                path, fixture=fixture, refs=refs[name]["before"], frozen=frozen, lut=lut
            )
        except ValueError as error:
            report = {"run_json": str(path), "status": "analysis-failed", "error": str(error)}
        result["rows"].append({"round": row["round"], "control": row["control"], **report})
        output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    result["status"] = "analyzed"
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"bearing_evidence={output}", flush=True)


if __name__ == "__main__":
    main()
