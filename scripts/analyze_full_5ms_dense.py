#!/usr/bin/env python3
"""Native 5 MS/s dense replay; same physical trims/gates, no invented samples."""

from dataclasses import asdict
from pathlib import Path

import numpy as np
from analyze_jitter_comparison import GRID, LUT, PLAN, ROOT, fixture_geometry, save, verified_run

from smateway.fast_tracking import (
    FastTrackingProfile,
    analyze_fast_bearings,
    analyze_fast_phase,
    coherent_product,
    decode_fast_schedule,
    estimate_frequency_difference,
)
from smateway.rate_timing import PORTS, load, sha256
from smateway.tracking.calibration import BoardCalibrationLut
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import load_ism_band_profiles


def contract(root):
    paths = [
        Path(__file__),
        ROOT / "scripts/analyze_jitter_comparison.py",
        LUT,
        PLAN,
        root / "fixture-dual-band.json",
        root / "plan.json",
        ROOT / "profiles/tracking-c6-200us-v1/control_profile.json",
    ]
    paths += sorted((ROOT / "src/smateway").glob("*.py"))
    paths += sorted((ROOT / "src/smateway/tracking").glob("*.py"))
    return {str(p): sha256(p) for p in paths}


def analyze(item, root, *, output_path=None):
    path = Path(item["run_json"])
    output = output_path or path.parent / "five-ms-dense-analysis.json"
    run = verified_run(item)
    provenance = contract(root)
    if output.exists():
        result = load(output)
        if result["run_sha256"] != item["sha256"] or result["source_contract"] != provenance:
            raise ValueError("Cached five-MS analysis source or input differs")
        return result
    result = {
        "run_json": str(path),
        "run_sha256": item["sha256"],
        "source_contract": provenance,
        "status": "analysis-failed",
    }
    try:
        cfg = run["configuration"]
        if (
            cfg["name"] != "D"
            or cfg["sample_rate_hz"] != 5000000
            or cfg["bandwidth_hz"] != 4000000
            or cfg["dwell_us"] != 200
            or cfg["mode"] != "fast"
            or cfg["frequency_hz"] != item["frequency_hz"]
            or cfg["tx_channel"] != item["tx_channel"]
        ):
            raise ValueError("Expected native 5 MS/s / 4 MHz / 200 us matched dense record")
        one, two = [
            np.memmap(r["path"], mode="r", dtype=np.complex64) for r in run["capture"]["raw"]
        ]
        profile = FastTrackingProfile.load(
            ROOT / "profiles/tracking-c6-200us-v1/control_profile.json"
        )
        fs = cfg["sample_rate_hz"]
        decode = decode_fast_schedule(
            np.asarray(two * one.conj(), dtype=np.complex64),
            sample_rate_hz=fs,
            tone_offset_hz=0,
            profile=profile,
            edge_trim_us=5,
        )
        fit = None
        if cfg["tx_channel"] == 1:
            fit = estimate_frequency_difference(
                one,
                two,
                decode=decode,
                sample_rate_hz=fs,
                nominal_difference_hz=run["source_settings"]["frequency_difference_hz"],
            )
        product = coherent_product(
            one,
            two,
            sample_rate_hz=fs,
            first_sample_sequence=run["capture"]["timeline"][0]["first_sample_sequence"],
            difference_hz=None if fit is None else fit.frequency_difference_hz,
        )
        result["phase"] = analyze_fast_phase(product, decode=decode, profile=profile)
        result["frequency_difference_fit"] = None if fit is None else asdict(fit)
        result["frequency_difference_qualified"] = fit is None or fit.search_objective >= 0.25
        result["decode"] = {k: v for k, v in asdict(decode).items() if k != "intervals"}
        coefficients = (
            BoardCalibrationLut.load(LUT).evaluate(cfg["frequency_hz"], PORTS).coefficients
        )
        historical = load_ism_band_profiles(PLAN)["ism5800-c6-v1"].geometry
        for name, geometry in (
            ("historical_geometry", historical),
            ("nominal_51mm", fixture_geometry(root)),
        ):
            result[name] = analyze_fast_bearings(
                product,
                decode=decode,
                profile=profile,
                calibration_coefficients=coefficients,
                steering=far_field_steering(geometry, cfg["frequency_hz"], GRID),
                bearings_deg=GRID,
                expected_bearing_deg=None,
            )
        result["status"] = "analyzed"
    except ValueError as error:
        result["error"] = str(error)
    save(output, result)
    return result


def compact(item, result):
    row = {
        "frequency_hz": item["frequency_hz"],
        "tx_port": f"TX{item['tx_channel'] + 1}",
        "status": result["status"],
        "phase_10deg_wall_latency_ms": None,
        "full_bearing_deg": None,
        "full_bearing_valid": False,
        "full_bearing_residual_phase_rms_deg": None,
        "nominal_51mm_bearing_deg": None,
        "nominal_51mm_valid": False,
        "error": result.get("error"),
        "run_json": item["run_json"],
        "run_sha256": item["sha256"],
    }
    if result["status"] == "analyzed":
        passing = [
            r
            for r in result["phase"]["integration_study"]
            if r["groups_per_port"] >= 8
            and r["phase_rms_deg_power_weighted_ports"] <= 10
            and result["frequency_difference_qualified"]
        ]
        old, nominal = [
            result[k]["full_capture_reference"] for k in ("historical_geometry", "nominal_51mm")
        ]
        row.update(
            phase_10deg_wall_latency_ms=passing[0]["measured_wall_latency_ms"] if passing else None,
            full_bearing_deg=old["bearing_deg"],
            full_bearing_valid=old["valid"],
            full_bearing_residual_phase_rms_deg=old["residual_phase_rms_deg"],
            nominal_51mm_bearing_deg=nominal["bearing_deg"],
            nominal_51mm_valid=nominal["valid"],
        )
    return row
