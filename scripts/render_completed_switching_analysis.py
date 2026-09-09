#!/usr/bin/env python3
"""Reproducible offline report of the completed September 8 acquisitions."""

# ruff: noqa: E501

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from analyze_comprehensive_bearings import block_references

from smateway.campaign_analysis import (
    base_window_pass,
    condition_summary,
    relative_phase_trace,
    window_audit,
)
from smateway.causal_timing import bearing_study
from smateway.rate_timing import PORTS, complex_value, load, sha256
from smateway.tracking.bearing import solve_bearing
from smateway.tracking.calibration import BoardCalibrationLut
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]
FROZEN_HASHES = {
    "scripts/analyze_reference_timing.py": "1ca253ae48ad81ddf465e4a5dfb567c300a820ba9ea4c4ea5367a9e587080c7c",
    "src/smateway/reference_timing.py": "b3215738c600607eee727f7e5afcbb05c0d3b82c50623d4cbcdb6f93a115b2c0",
    "src/smateway/causal_timing.py": "0ba68f4fbf5429157a8140c8db46c08a0eeac670dae43552256d3b4df0c383f9",
    "src/smateway/rate_timing.py": "9b9e6cdd7c2eeda6c01eb18dd6768cf503db3204da4b5ce90c2b578939817167",
    "src/smateway/fast_tracking.py": "2a6bcf554a6965f0173c165a5885c72231eef6d444fb03b6fb9b0fb15d29d37b",
}
DWELLS = (25, 50, 100, 200, 1000)
COLOURS = ("#2563eb", "#d97706", "#15803d", "#dc2626", "#7c3aed", "#0891b2")


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def table(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def save(fig, path):
    fig.tight_layout(pad=1.6)
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def checked(path, expected=None):
    if expected is not None and sha256(path) != expected:
        raise ValueError(f"evidence hash differs: {path}")
    return load(path)


def label(block):
    return f"{block['frequency_hz'] / 1e6:g} MHz / {block['configuration']} / {block['started_at'][11:19]}"


def collect(campaign, audit, *, allow_partial=False):
    blocks, fixed, drift_rows, rolling, conditions, traces, inputs = [], [], [], [], [], {}, []
    audited = {r["run_json"]: r for r in audit["rows"]}
    lut = BoardCalibrationLut.load(
        ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"
    )
    geometry = ArrayGeometry.circular("confirmed-C6-51mm", PORTS, radius_mm=25.5)
    grid = np.arange(0, 360, 0.25)
    for path in sorted(campaign.glob("block-*.json")):
        if len(path.stem.split("-")) != 2:
            continue
        block = checked(path)
        inputs.append({"path": str(path), "sha256": sha256(path)})
        refs, frozen, drift = block_references(block)
        for item in block["captures"] + block["references"]:
            if audited[item["run_json"]]["sha256"] != item["sha256"]:
                raise ValueError("capture differs from raw-audited record")
        if len({r["run_json"] for r in block["captures"] + block["references"]}) != len(
            block["captures"]
        ) + len(block["references"]):
            raise ValueError("capture/reference reuse")
        restored = all(
            checked(Path(r["path"]), r["sha256"])["restored_flash"]["matches_backup"]
            for r in block["restores"]
        )
        summary = {
            "id": path.stem,
            "label": label(block),
            "started_at": block["started_at"],
            "frequency_hz": block["frequency_hz"],
            "configuration": block["configuration"],
            "status": block["status"],
            "switched_attempts": len(block["captures"]),
            "reference_attempts": len(block["references"]),
            "restored": bool(block["restores"] and restored),
            "bracket_pass": all(d["available"] and d["passed"] for d in drift.values()),
            "observable_ports": " ".join(
                p for p, ok in zip(PORTS, frozen["observable"], strict=True) if ok
            ),
            "unobservable_ports": " ".join(
                p for p, ok in zip(PORTS, frozen["observable"], strict=True) if not ok
            ),
            "recipe_bound": bool(block.get("timing_replay_recipe")),
        }
        blocks.append(summary)
        bearing_path = path.with_name(path.stem + "-bearings.json")
        if bearing_path.exists():
            bearing_report = checked(bearing_path)
            checked(path, bearing_report["block_sha256"])
            if bearing_report["status"] != "analyzed":
                raise ValueError("bearing analysis is still running")
            summary["legacy_bearing_rows"] = len(bearing_report["rows"])
            inputs.append({"path": str(bearing_path), "sha256": sha256(bearing_path)})
        for name, d in drift.items():
            drift_rows.append({"block": path.stem, "configuration": name, **d})
        supplement_path = path.with_name(path.stem + "-phase.json")
        supplement = {}
        if supplement_path.exists():
            sidecar = checked(supplement_path)
            checked(path, sidecar["block_sha256"])
            supplement = {r["run_json"]: r for r in sidecar["rows"]}
            inputs.append({"path": str(supplement_path), "sha256": sha256(supplement_path)})
        for original in block["captures"]:
            row = {**original, **supplement.get(original["run_json"], {})}
            if not row.get("analysis_json"):
                raise ValueError(f"unprocessed fixed-policy capture: {row['run_json']}")
            analysis = checked(Path(row["analysis_json"]), row["analysis_sha256"])
            metrics = next(
                (
                    v["metrics"]
                    for v in analysis.get("variants", [])
                    if v["method"] == "native_refined" and v["leading_discard_us"] == 5
                ),
                {},
            )
            fixed.append(
                {
                    "block": path.stem,
                    "run_json": row["run_json"],
                    "configuration": row.get("configuration", block["configuration"]),
                    "dwell_us": row["dwell_us"],
                    "round": row["round"],
                    "control": row["control"],
                    "any_window_pass": metrics.get("passed", False),
                    "first_phase_pass_ms": metrics.get("first_phase_pass_ms"),
                    "error": analysis.get("error"),
                    **metrics.get("closure", {}),
                }
            )
        if not summary["recipe_bound"]:
            continue
        binding = block["timing_replay_recipe"]
        if sha256(Path(binding["path"])) != binding["sha256"]:
            raise ValueError("frozen recipe differs")
        timing_path = path.with_name(path.stem + "-reference-timing.json")
        timing = checked(timing_path)
        checked(path, timing["block_sha256"])
        if timing["source_sha256"] != FROZEN_HASHES:
            raise ValueError("frozen timing implementation differs")
        if timing["status"] != "analyzed" and not allow_partial:
            raise ValueError("timing analysis is still running")
        inputs.append({"path": str(timing_path), "sha256": sha256(timing_path)})
        timing_rows = {r["run_json"]: r for r in timing["rows"]}
        if not allow_partial and set(timing_rows) != {r["run_json"] for r in block["captures"]}:
            raise ValueError("timing report omits a capture")
        coefficients = lut.evaluate(block["frequency_hz"], PORTS).coefficients
        steering = far_field_steering(geometry, block["frequency_hz"], grid)
        for item in block["captures"]:
            source = timing_rows.get(item["run_json"], {})
            run = checked(Path(item["run_json"]), item["sha256"])
            cfg = run["configuration"]
            roll = source.get("rolling_past_only", {})
            windows, metrics = roll.get("windows", []), roll.get("metrics", {})
            support = window_audit(
                windows,
                sample_rate_hz=cfg["sample_rate_hz"],
                samples_per_channel=run["capture"]["samples_per_channel"],
            )
            study = metrics.get("studies", [{}])[0]
            closure = metrics.get("closure", {})
            compute = [
                w["host_replay_compute_s"] * 1000 for w in windows if "host_replay_compute_s" in w
            ]
            row = {
                "block": path.stem,
                "run_json": item["run_json"],
                "run_sha256": item["sha256"],
                "configuration": cfg["name"],
                "sample_rate_hz": cfg["sample_rate_hz"],
                "frequency_hz": cfg["frequency_hz"],
                "dwell_us": item["dwell_us"],
                "round": item["round"],
                "control": item["control"],
                "base_pass": bool(support["complete"] and base_window_pass(metrics)),
                "base_rms_deg": study.get("weighted_phase_rms_deg"),
                "max_phase_bias_deg": closure.get("maximum_observable_phase_bias_deg"),
                "max_gain_error_db": closure.get("maximum_observable_gain_error_db"),
                "mean_compute_ms": float(np.mean(compute)) if compute else None,
                "p95_compute_ms": float(np.percentile(compute, 95)) if compute else None,
                "observable_ports": summary["observable_ports"],
                "unobservable_ports": summary["unobservable_ports"],
                "analysis_error": source.get("error"),
                **support,
            }
            if support["complete"]:
                h = np.array([[complex_value(v) for v in w["mean_transfer"]] for w in windows])
                expected = np.array(
                    [
                        complex_value(refs[cfg["name"]]["before"][p]["reference"]["transfer"])
                        for p in PORTS
                    ]
                )
                independent = solve_bearing(
                    expected * coefficients, steering, grid, weights=frozen["weights"]
                )
                bearing = bearing_study(
                    h,
                    cycle_ms=50,
                    coefficients=coefficients,
                    steering=steering,
                    bearing_grid=grid,
                    weights=frozen["weights"],
                    grouping_cycles=[1],
                    reference_bearing_deg=independent.bearing_deg,
                )[0]
                row.update(
                    {
                        "bearing_valid_percent": bearing["model_valid_percent"],
                        "bearing_rms_deg": bearing["all_group_repeatability_rms_deg"],
                        "bearing_pass": bearing["repeatable_bearing_pass"],
                    }
                )
                traces[item["run_json"]] = {
                    "relative_phase_deg": relative_phase_trace(
                        h, expected, frozen["weights"]
                    ).tolist(),
                    "closure": closure,
                    "compute_ms": compute,
                    "cycle_us": [w["cycle_samples"] / cfg["sample_rate_hz"] * 1e6 for w in windows],
                    "usable_per_port_ms": [w["usable_per_port_ms"] for w in windows],
                    "bearing": bearing,
                }
            rolling.append(row)
        group = [r for r in rolling if r["block"] == path.stem]
        for dwell in DWELLS:
            main = [r for r in group if not r["control"] and r["dwell_us"] == dwell]
            controls = [r for r in group if r["control"]]
            expected_controls = sorted(
                {r["dwell_us"] for r in block["planned_rows"] if r["control"]}
            )
            outcome = condition_summary(
                main,
                controls,
                expected_control_dwells=expected_controls,
                bracket_pass=summary["bracket_pass"],
                block_complete=block["status"] == "diagnostic-complete",
            )
            conditions.append(
                {
                    "block": path.stem,
                    "configuration": block["configuration"],
                    "dwell_us": dwell,
                    **outcome,
                }
            )
    return {
        "blocks": blocks,
        "fixed": fixed,
        "drift": drift_rows,
        "rolling": rolling,
        "conditions": conditions,
        "traces": traces,
        "inputs": inputs,
    }


def diagnostics(campaign):
    rows = []
    paths = [
        campaign / "block-20260908T222235133339Z-consensus-25us-r1.json",
        campaign / "block-20260908T222235133339Z-consensus-rx2-25us-r1.json",
        campaign / "offline-analysis-20260909/intercept-25us-r1-finalized.json",
    ]
    for path in paths:
        report = checked(path)
        checked(Path(report["run_json"]), report["run_sha256"])
        if "block_path" in report:
            checked(Path(report["block_path"]), report["block_sha256"])
        else:
            checked(Path(report["timing_report"]), report["timing_sha256"])
        metrics = report.get("intercept_metrics", report.get("metrics", {}))
        method = (
            "DC-intercept regression"
            if "intercept" in path.name
            else "RX2 clock consensus"
            if "rx2" in path.name
            else "Cross-product clock consensus"
        )
        windows = report["windows"]
        rows.append(
            {
                "method": method,
                "frequency_mhz": report["configuration"]["frequency_hz"] / 1e6,
                "path": str(path),
                "sha256": sha256(path),
                "run_json": report["run_json"],
                "complete": report["complete"],
                "windows": len(windows),
                "analyzed_windows": sum(w["status"] == "analyzed" for w in windows),
                "base_rms_deg": metrics.get("studies", [{}])[0].get("weighted_phase_rms_deg"),
                "max_gain_error_db": metrics.get("closure", {}).get(
                    "maximum_observable_gain_error_db"
                ),
                "base_pass": bool(report["complete"] and base_window_pass(metrics)),
            }
        )
    baseline_report = checked(Path(rows[-1]["path"]))
    baseline = baseline_report["frozen_v1_metrics"]
    rows.append(
        {
            "method": "Frozen v1 (intercept comparator)",
            "frequency_mhz": 2475,
            "path": str(paths[-1]),
            "sha256": sha256(paths[-1]),
            "run_json": rows[-1]["run_json"],
            "complete": True,
            "windows": 60,
            "analyzed_windows": 60,
            "base_rms_deg": baseline["studies"][0]["weighted_phase_rms_deg"],
            "max_gain_error_db": baseline["closure"]["maximum_observable_gain_error_db"],
            "base_pass": base_window_pass(baseline),
        }
    )
    return rows


def figures(result, spectrum, png):
    blocks, fixed, rows, traces = (
        result["blocks"],
        result["fixed"],
        result["rolling"],
        result["traces"],
    )
    columns = [(d, False) for d in DWELLS] + [(200, True), (1000, True)]
    fig, ax = plt.subplots(figsize=(12, 6))
    matrix = np.full((len(blocks), len(columns)), np.nan)
    for i, block in enumerate(blocks):
        for j, (d, control) in enumerate(columns):
            group = [
                r
                for r in fixed
                if r["block"] == block["id"] and r["dwell_us"] == d and r["control"] == control
            ]
            if group:
                n = sum(r["any_window_pass"] for r in group)
                matrix[i, j] = n / len(group)
                ax.text(j, i, f"{n}/{len(group)}", ha="center", va="center", fontsize=10)
    ax.imshow(matrix, vmin=0, vmax=1, cmap="YlGn", aspect="auto")
    ax.set_yticks(
        range(len(blocks)), [b["label"] + (" *" if b["status"] == "failed" else "") for b in blocks]
    )
    ax.set_xticks(
        range(len(columns)), [f"{d} µs" + ("\nA control" if c else "") for d, c in columns]
    )
    ax.set_title(
        "Fixed native-refined policy: individual phase/closure passes\nAny tested integration window; * incomplete block; blank = not collected"
    )
    save(fig, png / "fig01_fixed_policy_matrix.png")

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharey="row")
    fresh = [b for b in blocks if b["recipe_bound"]]
    for col, block in enumerate(fresh):
        for rnd, marker in zip((1, 2, 3), ("o", "s", "^"), strict=True):
            group = sorted(
                [
                    r
                    for r in rows
                    if r["block"] == block["id"] and not r["control"] and r["round"] == rnd
                ],
                key=lambda r: r["dwell_us"],
            )
            for ax, key in zip(axes[:, col], ("base_rms_deg", "max_gain_error_db"), strict=True):
                ax.plot(
                    [r["dwell_us"] for r in group],
                    [r[key] if r[key] is not None else np.nan for r in group],
                    marker + "-",
                    label=f"Trial {rnd}",
                    color=COLOURS[rnd - 1],
                )
                ax.set_xscale("log")
                ax.set_xticks(DWELLS, [str(d) for d in DWELLS])
                ax.grid(alpha=0.2)
        axes[0, col].set_title(block["label"])
        axes[0, col].set_ylabel("Weighted phase RMS at 50 ms (°)")
        axes[0, col].axhline(10, color="gray", ls=":")
        axes[1, col].set(
            xlabel="Active dwell per port (µs)", ylabel="Max observable gain error (dB)"
        )
        axes[1, col].axhline(1, color="gray", ls=":")
        axes[0, col].legend()
    fig.suptitle(
        "Frozen-recipe fresh trials — identical gates, independently acquired blocks\nANT5 below the frozen visibility threshold in both; no live/bearing qualification"
    )
    save(fig, png / "fig02_fresh_dwell_comparison.png")

    selected = [r for r in rows if r["complete"]]
    fig, axes = plt.subplots(1, 2, figsize=(13, max(6, len(selected) * 0.27)))
    for ax, key, lim, title in zip(
        axes,
        ("relative_phase_bias_deg", "gain_error_db"),
        (10, 2),
        ("Phase bias (degrees)", "Gain error (dB)"),
        strict=True,
    ):
        values = np.array([traces[r["run_json"]]["closure"][key] for r in selected])
        art = ax.imshow(values, cmap="RdBu_r", vmin=-lim, vmax=lim, aspect="auto")
        ax.set_xticks(range(6), PORTS)
        ax.set_yticks(
            range(len(selected)),
            [
                f"{next(b['started_at'][11:16] for b in blocks if b['id'] == r['block'])} {r['configuration']} {r['dwell_us']}µs r{r['round']}"
                + (" control" if r["control"] else "")
                + (" pass" if r["base_pass"] else " FAIL")
                for r in selected
            ],
            fontsize=7,
        )
        ax.set_title(title)
        for i, row in enumerate(selected):
            for j, port in enumerate(PORTS):
                value = values[i, j]
                ax.text(
                    j,
                    i,
                    f"{value:.1f}" + ("*" if port in row["unobservable_ports"].split() else ""),
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="white" if abs(value) > lim * 0.65 else "black",
                )
        fig.colorbar(art, ax=ax, shrink=0.5, label="Colour clipped; numbers retain full magnitude")
    fig.suptitle(
        "Fresh rolling trials: independent static-reference closure, all six ports\n* below predeclared visibility threshold; pass requires base RMS and closure"
    )
    save(fig, png / "fig03_all_port_closure.png")

    fig, axes = plt.subplots(2, 2, figsize=(12, 7))
    for col, block in enumerate(fresh):
        row = next(
            r
            for r in rows
            if r["block"] == block["id"]
            and r["dwell_us"] == 100
            and r["round"] == 1
            and not r["control"]
        )
        if row["run_json"] not in traces:
            continue
        trace = traces[row["run_json"]]
        t = 1 + np.arange(len(trace["cycle_us"])) * 0.05
        phase = np.array(trace["relative_phase_deg"])
        for i, port in enumerate(PORTS):
            axes[0, col].plot(
                t,
                phase[:, i],
                color=COLOURS[i],
                lw=1,
                label=port + ("*" if port in row["unobservable_ports"].split() else ""),
            )
        axes[0, col].set(
            title=block["label"] + " / 100 µs / trial 1",
            ylabel="Independent-reference phase residual (°)",
        )
        axes[0, col].legend(ncol=3, fontsize=8)
        axes[1, col].plot(t, trace["cycle_us"], color="#334155")
        axes[1, col].set(
            xlabel="RF time from capture start (s)", ylabel="Past-only estimated cycle length (µs)"
        )
        for ax in axes[:, col]:
            ax.grid(alpha=0.2)
    fig.suptitle(
        "Per-window residuals and clock estimates; 1 s training then 50 ms predictions\nCommon phase removed per window, no per-port offset fitting; RF-inferred clock is not GPIO truth"
    )
    save(fig, png / "fig04_residual_and_clock.png")

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for block, color in zip(fresh, COLOURS, strict=False):
        for d in DWELLS:
            group = [
                r
                for r in rows
                if r["block"] == block["id"] and not r["control"] and r["dwell_us"] == d
            ]
            values = [v for r in group for v in traces.get(r["run_json"], {}).get("compute_ms", [])]
            if values:
                axes[0].scatter([d] * len(values), values, s=5, alpha=0.1, color=color)
                axes[0].scatter(
                    d,
                    np.median(values),
                    s=55,
                    marker="D",
                    color=color,
                    label=block["configuration"] if d == 25 else None,
                )
            for r in group:
                if "bearing_valid_percent" in r:
                    axes[1].scatter(
                        r["base_rms_deg"],
                        r["bearing_valid_percent"],
                        color=color,
                        marker="o" if r["base_pass"] else "x",
                        label=block["configuration"] if d == 25 and r["round"] == 1 else None,
                    )
    axes[0].axhline(50, color="black", ls="--", label="50 ms output budget")
    axes[0].set(
        xscale="log",
        yscale="log",
        xlabel="Dwell (µs)",
        ylabel="Host compute per 50 ms prediction (ms)",
        title="Offline processing, not an isolated rate benchmark",
    )
    axes[0].set_xticks(DWELLS, [str(d) for d in DWELLS])
    axes[1].axhline(95, color="gray", ls=":")
    axes[1].set(
        xlabel="Weighted phase RMS at 50 ms (°)",
        ylabel="Legacy model-valid bearings (%)",
        ylim=(-3, 103),
        title="Stable phase does not guarantee a valid bearing",
    )
    for ax in axes:
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    save(fig, png / "fig05_processing_and_bearing_limits.png")

    fig, axes = plt.subplots(3, 1, figsize=(12, 8))
    freq = np.asarray(spectrum["frequency_hz"])
    for ax, dwell in zip(axes, (25, 200, 1000), strict=True):
        for enabled, color, name in (
            (True, COLOURS[0], "Source on"),
            (False, COLOURS[1], "Both TX channels muted"),
        ):
            group = [
                r
                for r in spectrum["rows"]
                if r["dwell_us"] == dwell and r["source_enabled"] == enabled
            ]
            values = np.array([r["rx2_psd_counts2_per_hz"] for r in group])
            db = 10 * np.log10(np.maximum(values, 1e-30))
            ax.fill_between(freq, db.min(axis=0), db.max(axis=0), color=color, alpha=0.13)
            ax.plot(
                freq,
                10 * np.log10(np.maximum(values.mean(axis=0), 1e-30)),
                color=color,
                lw=0.7,
                label=name,
            )
        ax.set(
            xlim=(0, 15000),
            ylabel="PSD (dB counts²/Hz)",
            title=f"{dwell} µs dwell; line = mean PSD, shading = range of three independent captures",
        )
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    axes[-1].set_xlabel("RX2 baseband frequency (Hz), native-rate PSD without extra decimation")
    fig.suptitle(
        "5.811 GHz / 2 MS/s: switching-related spectral energy remains with the source muted\nNoncontemporaneous control; does not distinguish electronics coupling, DC tracking or ambient RF"
    )
    save(fig, png / "fig06_source_muted_spectra.png")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    geometry = ArrayGeometry.circular("confirmed-C6-51mm", PORTS, radius_mm=25.5)
    grid = np.arange(0, 360, 0.25)
    for ax, frequency in zip(axes, (2475000000, 5811000000), strict=True):
        steering = far_field_steering(geometry, frequency, grid)
        estimate = solve_bearing(steering[360], steering, grid)
        ax.plot(grid, 10 * np.log10(np.maximum(estimate.likelihood, 1e-12)), color=COLOURS[0])
        ax.axvline(90, color=COLOURS[2], ls="--", label="True / fitted bearing")
        ax.axvline(
            estimate.second_bearing_deg,
            color=COLOURS[3],
            ls=":",
            label="Reported competing direction",
        )
        ax.set(
            xlabel="Bearing (degrees clockwise)",
            ylabel="Normalized likelihood (dB)",
            ylim=(-25, 1),
            title=f"{frequency / 1e6:g} MHz: margin {estimate.ambiguity_margin_db:.3f} dB; valid={estimate.valid}",
        )
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    fig.suptitle(
        "Separate noiseless simulation: a broad main-lobe shoulder can fail the legacy gate"
    )
    save(fig, png / "fig07_noiseless_bearing_gate.png")

    diag = result["diagnostics"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.5))
    labels = [f"{d['frequency_mhz']:g} MHz\n{d['method']}" for d in diag]
    for ax, key, title in zip(
        axes,
        ("analyzed_windows", "base_rms_deg", "max_gain_error_db"),
        (
            "Evaluated windows / 60",
            "50 ms weighted phase RMS (°)",
            "Max observable gain error (dB)",
        ),
        strict=True,
    ):
        for i, row in enumerate(diag):
            if row[key] is None:
                ax.text(
                    i, 0.5, "not scored", rotation=90, ha="center", va="bottom", color="#64748b"
                )
            else:
                ax.bar(i, row[key], color="#94a3b8" if i == 3 else "#d97706")
                ax.text(i, row[key], f"{row[key]:.1f}", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(range(len(diag)), labels, rotation=28, ha="right", fontsize=8)
        ax.set_title(title)
        ax.set_xlim(-0.6, len(diag) - 0.4)
        ax.grid(axis="y", alpha=0.2)
    axes[0].axhline(60, color="gray", ls=":")
    axes[1].axhline(10, color="gray", ls=":")
    axes[2].axhline(1, color="gray", ls=":")
    fig.suptitle(
        "Isolated 25 µs diagnostics: neither clock consensus nor a DC intercept rescues calibration\nExploratory single captures; separate frequencies; incomplete windows never compressed"
    )
    save(fig, png / "fig08_negative_diagnostics.png")

    runtime = result["runtime_profile"]["rows"]
    names = ["_raw_fft", "_two_cluster_threshold", "_cyclostationary_decode", "native_fold"]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for index, config in enumerate(("A", "B")):
        records = [r for r in runtime if r["configuration"] == config]
        total = float(np.mean([r["total_profiled_s"] * 1000 for r in records]))
        parts = [
            float(
                np.mean(
                    [
                        sum(f["self_s"] for f in r["functions"] if f["function"] == name) * 1000
                        for r in records
                    ]
                )
            )
            for name in names
        ]
        parts.append(total - sum(parts))
        left = 0
        for j, (name, value) in enumerate(zip([*names, "other self time"], parts, strict=True)):
            ax.barh(
                index,
                value,
                left=left,
                color=COLOURS[j],
                label=name.lstrip("_").replace("_", " ") if index == 0 else None,
            )
            left += value
        ax.text(total + 5, index, f"{total:.0f} ms", va="center")
    ax.axvline(50, color="black", ls="--", label="50 ms output budget")
    ax.set_yticks((0, 1), ("A / 2 MS/s", "B / 5 MS/s"))
    ax.set(
        xlabel="Mean profiled self time per predicted window (ms)",
        title="Where offline compute goes: identical 100 µs recipe, first 1 s → 50 ms window\nThree repeated replays per configuration; profiling adds overhead; not a live benchmark",
    )
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=8)
    ax.grid(axis="x", alpha=0.2)
    save(fig, png / "fig09_runtime_profile.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/completed_switching_analysis")
    parser.add_argument("--allow-partial-preview", action="store_true")
    args = parser.parse_args()
    data, png = args.output / "data", args.output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    audit = load(data / "acquisition-audit.json")
    if not audit["passed"]:
        raise ValueError("raw audit did not pass")
    result = collect(args.campaign_root, audit, allow_partial=args.allow_partial_preview)
    result["diagnostics"] = diagnostics(args.campaign_root)
    result["runtime_profile"] = load(data / "runtime-profile.json")
    spectrum_path = args.campaign_root / "offline-analysis-20260909/source-muted-spectrum.json"
    spectrum = load(spectrum_path)
    result["inputs"].append({"path": str(spectrum_path), "sha256": sha256(spectrum_path)})
    for key in ("blocks", "fixed", "drift", "rolling", "conditions", "diagnostics"):
        table(data / f"{key}.csv", result[key])
    dump(data / "rolling-traces.json", result["traces"])
    dump(data / "source-muted-spectrum.json", spectrum)
    result["generated_at"] = datetime.now(UTC).isoformat()
    result["raw_audit_sha256"] = sha256(data / "acquisition-audit.json")
    result["report_source_sha256"] = {
        str(p.relative_to(ROOT)): sha256(p)
        for p in (Path(__file__), ROOT / "src/smateway/campaign_analysis.py")
    }
    result["status"] = "preview" if args.allow_partial_preview else "completed-offline-analysis"
    dump(data / "analysis-summary.json", {k: v for k, v in result.items() if k != "traces"})
    figures(result, spectrum, png)
    dump(
        data / "figures-manifest.json",
        {
            "schema": 1,
            "figures": [
                {
                    "path": str(p.relative_to(args.output)),
                    "sha256": sha256(p),
                    "bytes": p.stat().st_size,
                }
                for p in sorted(png.glob("*.png"))
            ],
        },
    )
    print(f"analysis_data={data}")


if __name__ == "__main__":
    main()
