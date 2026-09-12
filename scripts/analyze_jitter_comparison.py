#!/usr/bin/env python3
"""Hash-audited offline comparison of the moved fixture; no hardware imports."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from smateway.fast_tracking import (  # noqa: E402
    FastTrackingProfile,
    analyze_fast_bearings,
    analyze_fast_phase,
    coherent_product,
    decode_fast_schedule,
    estimate_frequency_difference,
)
from smateway.rate_timing import PORTS, complex_value, load, sha256  # noqa: E402
from smateway.tracking.bearing import solve_bearing  # noqa: E402
from smateway.tracking.calibration import BoardCalibrationLut  # noqa: E402
from smateway.tracking.manifold import far_field_steering  # noqa: E402
from smateway.tracking.schedule import ArrayGeometry, load_ism_band_profiles  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-jitter-20260910-v1")
OLD_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-comprehensive-20260908-v1")
OLD_BLOCKS = {
    5800000000: OLD_ROOT / "block-20260908T164249289170Z.json",
    2475000000: OLD_ROOT / "block-20260908T225855806955Z.json",
    5811000000: OLD_ROOT / "block-20260908T222235133339Z.json",
    915000000: Path(
        "/srv/bulk/samteway/lab-data/tracking-915-diagnostic-20260909-v1/"
        "block-20260909T170658793466Z.json"
    ),
}
LUT = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"
PLAN = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
GRID = np.arange(0, 360, 0.25)


def source_contract(root):
    paths = [Path(__file__), LUT, PLAN, root / "fixture-dual-band.json"]
    paths.extend(sorted((ROOT / "src/smateway").glob("*.py")))
    paths.extend(sorted((ROOT / "src/smateway/tracking").glob("*.py")))
    paths.append(ROOT / "profiles/tracking-c6-200us-v1/control_profile.json")
    return {str(p.resolve()): sha256(p) for p in paths}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def table(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def verified_run(item):
    path = Path(item["run_json"])
    if sha256(path) != item["sha256"]:
        raise ValueError("Run-record hash differs")
    run = load(path)
    if run["status"] != "passed":
        raise ValueError("Acquisition failure is not admissible")
    for raw in run["capture"]["raw"]:
        data_path = Path(raw["path"])
        if (
            sha256(data_path) != raw["sha256"]
            or data_path.stat().st_size != run["capture"]["samples_per_channel"] * 8
        ):
            raise ValueError("Raw IQ hash or length differs")
    return run


def relative_phase(values, anchor=3):
    values = np.asarray(values, complex)
    if abs(values[anchor]) == 0:
        raise ValueError("Reference antenna transfer is zero")
    return np.angle(values / values[anchor], deg=True)


def fixture_geometry(root):
    fixture = load(root / "fixture-dual-band.json")
    return ArrayGeometry(fixture["fixture_id"], PORTS, fixture["positions_m"], np.arange(6) * 60)


def static_comparison(root):
    screen = load(root / "screens.json")
    output = root / "analysis"
    output.mkdir(exist_ok=True)
    rows, bearings = [], []
    geometry = fixture_geometry(root)
    lut = BoardCalibrationLut.load(LUT)
    complete_frequencies = []
    for frequency, old_path in OLD_BLOCKS.items():
        after = [
            r
            for r in screen["captures"]
            if r["frequency_hz"] == frequency and r["mode"] == "static" and r["status"] == "passed"
        ]
        if len(after) != 6 or {r["port"] for r in after} != set(PORTS):
            continue
        old_block = load(old_path)
        before = [
            r
            for r in old_block["references"]
            if r["position"] == "before" and r.get("configuration", "A") == "A"
        ]
        if len(before) != 6 or {r["port"] for r in before} != set(PORTS):
            raise ValueError("Historical reference grid differs")
        complete_frequencies.append(frequency)
        for label, entries in (("before", before), ("after", after)):
            runs = {r["port"]: (r, verified_run(r)) for r in entries}
            transfers = np.array(
                [complex_value(runs[p][1]["reference"]["transfer"]) for p in PORTS]
            )
            phases = relative_phase(transfers)
            for i, port in enumerate(PORTS):
                entry, run = runs[port]
                cfg, ref = run["configuration"], run["reference"]
                if (
                    cfg["frequency_hz"] != frequency
                    or cfg["receiver_gain_db"] != old_block["gain_db"]
                ):
                    raise ValueError("Static before/after gain or frequency mismatch")
                rows.append(
                    {
                        "epoch": label,
                        "frequency_hz": frequency,
                        "port": port,
                        "gain_db": cfg["receiver_gain_db"],
                        "magnitude_db": float(20 * np.log10(abs(transfers[i]))),
                        "phase_relative_ant8_deg": float(phases[i]),
                        "phase_rms_10ms_deg": ref["phase_rms_10ms_deg"],
                        "coherence": ref["coherence"],
                        "run_json": entry["run_json"],
                        "sha256": entry["sha256"],
                    }
                )
            result = solve_bearing(
                transfers * lut.evaluate(frequency, PORTS).coefficients,
                far_field_steering(geometry, frequency, GRID),
                GRID,
            )
            bearings.append(
                {
                    "epoch": label,
                    "frequency_hz": frequency,
                    "bearing_deg": result.bearing_deg,
                    "valid": result.valid,
                    "score": result.score,
                    "phase_residual_deg": result.residual_phase_rms_deg,
                    "ambiguity_margin_db": result.ambiguity_margin_db,
                    "scope": "same 51 mm model, equal weights; not surveyed accuracy",
                }
            )
    table(output / "static-before-after.csv", rows)
    table(output / "static-bearings.csv", bearings)
    if rows:
        fig, axes = plt.subplots(
            3, len(complete_frequencies), figsize=(14, 9), squeeze=False, layout="constrained"
        )
        for col, frequency in enumerate(complete_frequencies):
            for epoch, color in (("before", "#287c8e"), ("after", "#c05a32")):
                subset = [r for r in rows if r["frequency_hz"] == frequency and r["epoch"] == epoch]
                for row, key in enumerate(
                    ("magnitude_db", "phase_relative_ant8_deg", "phase_rms_10ms_deg")
                ):
                    axes[row, col].plot(
                        range(6),
                        [r[key] for r in subset],
                        "o-",
                        color=color,
                        label=epoch,
                        markersize=4,
                    )
                    axes[row, col].set_xticks(range(6), PORTS, rotation=45)
                    axes[row, col].grid(alpha=0.2)
            axes[0, col].set_title(f"{frequency / 1e6:g} MHz")
            axes[0, col].legend()
        for row, label in enumerate(
            ("RX2/RX1 magnitude (dB)", "Phase relative to ANT8 (°)", "Static 10 ms phase RMS (°)")
        ):
            axes[row, 0].set_ylabel(label)
        fig.suptitle(
            "Before / after small physical perturbation · matched static RX settings\n"
            "All six ports shown; phase relative to ANT8; no per-port correction fitted",
            fontsize=14,
        )
        fig.savefig(output / "fig01_static_before_after.png", dpi=160, bbox_inches="tight")
        plt.close(fig)
    save(
        output / "static-audit.json",
        {
            "schema": 1,
            "status": "partial" if len(complete_frequencies) < 4 else "complete",
            "source_sha256": sha256(Path(__file__)),
            "screen_sha256": sha256(root / "screens.json"),
            "frequencies_analyzed_hz": complete_frequencies,
            "runs_and_raw_iq_verified": len(rows),
            "old_blocks": {str(p): sha256(p) for p in OLD_BLOCKS.values()},
        },
    )
    print(f"Static comparison: {len(complete_frequencies)}/4 complete frequencies", flush=True)


def analyze_dense_run(item, root):
    path = Path(item["run_json"])
    output = path.parent / "jitter-dense-analysis.json"
    contract = source_contract(root)
    # Even cached analysis must remain bound to the current run and raw bytes.
    run = verified_run(item)
    if output.exists():
        cached = load(output)
        if cached["run_sha256"] != item["sha256"] or cached["source_contract"] != contract:
            raise ValueError("Cached dense analysis belongs to different source/run")
        return cached
    result = {
        "schema": 1,
        "run_json": str(path),
        "run_sha256": item["sha256"],
        "source_contract": contract,
        "status": "analysis-failed",
    }
    try:
        cfg = run["configuration"]
        result["configuration"] = cfg
        if cfg["mode"] != "fast" or cfg["sample_rate_hz"] != 2000000 or cfg["dwell_us"] != 200:
            raise ValueError(
                "Dense comparison requires the fixed historical 200 us / 2 MS/s schedule"
            )
        vectors = [
            np.memmap(Path(r["path"]), mode="r", dtype=np.complex64) for r in run["capture"]["raw"]
        ]
        one, two = vectors
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
        result["frequency_difference_fit"] = None if fit is None else asdict(fit)
        result["frequency_difference_qualified"] = fit is None or fit.search_objective >= 0.25
        result["phase"] = analyze_fast_phase(product, decode=decode, profile=profile)
        result["decode"] = {k: v for k, v in asdict(decode).items() if k != "intervals"}
        coefficients = (
            BoardCalibrationLut.load(LUT).evaluate(cfg["frequency_hz"], PORTS).coefficients
        )
        legacy = load_ism_band_profiles(PLAN)["ism5800-c6-v1"].geometry
        for label, geometry in (
            ("historical_geometry", legacy),
            ("nominal_51mm", fixture_geometry(root)),
        ):
            result[label] = analyze_fast_bearings(
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


def dense_comparison(root):
    manifest = load(root / "dense.json")
    rows = []
    for i, item in enumerate(manifest["captures"]):
        print(
            f"[dense analysis {i + 1}/{len(manifest['captures'])}] {item['run_json']}", flush=True
        )
        result = analyze_dense_run(item, root)
        row = {
            "frequency_hz": item["frequency_hz"],
            "tx_port": f"TX{item['tx_channel'] + 1}",
            "status": result["status"],
            "frequency_difference_qualified": False,
            "phase_10deg_wall_latency_ms": None,
            "full_bearing_deg": None,
            "full_bearing_valid": False,
            "full_bearing_residual_phase_rms_deg": None,
            "nominal_51mm_bearing_deg": None,
            "nominal_51mm_valid": False,
            "error": result.get("error"),
            "run_json": item["run_json"],
        }
        if result["status"] == "analyzed":
            phase = result["phase"]["integration_study"]
            qualified = result["frequency_difference_qualified"]
            passing = [
                s
                for s in phase
                if s["groups_per_port"] >= 8
                and s["phase_rms_deg_power_weighted_ports"] <= 10
                and qualified
            ]
            legacy = result["historical_geometry"]["full_capture_reference"]
            nominal = result["nominal_51mm"]["full_capture_reference"]
            row.update(
                {
                    "frequency_difference_qualified": qualified,
                    "phase_10deg_wall_latency_ms": passing[0]["measured_wall_latency_ms"]
                    if passing
                    else None,
                    "full_bearing_deg": legacy["bearing_deg"],
                    "full_bearing_valid": legacy["valid"],
                    "full_bearing_residual_phase_rms_deg": legacy["residual_phase_rms_deg"],
                    "nominal_51mm_bearing_deg": nominal["bearing_deg"],
                    "nominal_51mm_valid": nominal["valid"],
                }
            )
        rows.append(row)
    output = root / "analysis"
    output.mkdir(exist_ok=True)
    table(output / "dense-after.csv", rows)
    save(
        output / "dense-summary.json",
        {
            "schema": 1,
            "analyzed_records": len(rows),
            "analysis_successes": sum(r["status"] == "analyzed" for r in rows),
            "manifest_sha256": sha256(root / "dense.json"),
            "source_sha256": sha256(Path(__file__)),
            "scope": (
                "Before/after uses identical historical geometry; 51 mm is a separate comparator"
            ),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--stage", choices=("static", "dense"), required=True)
    args = parser.parse_args()
    {"static": static_comparison, "dense": dense_comparison}[args.stage](args.campaign_root)


if __name__ == "__main__":
    main()
