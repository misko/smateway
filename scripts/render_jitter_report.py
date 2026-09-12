#!/usr/bin/env python3
"""Render the moved-fixture comparison, retaining failed and missing evidence."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from analyze_jitter_comparison import (  # noqa: E402
    DEFAULT_ROOT,
    GRID,
    LUT,
    ROOT,
    fixture_geometry,
    verified_run,
)
from render_completed_switching_analysis import collect, dump, table  # noqa: E402

from smateway.rate_timing import PORTS, load, sha256  # noqa: E402
from smateway.tracking.bearing import solve_bearing  # noqa: E402
from smateway.tracking.calibration import BoardCalibrationLut  # noqa: E402
from smateway.tracking.manifold import far_field_steering  # noqa: E402

OUT = ROOT / "docs/jittered_fixture_comparison"
CASES = ((915, "A"), (2475, "A"), (2475, "B"), (5800, "A"), (5811, "A"), (5811, "B"), (5811, "D"))
DWELLS = (25, 50, 100, 200, 1000)
COLOURS = {"before": "#287c8e", "after": "#c05a32"}
BASE = ROOT / "docs/cross_band_tracking_analysis/data"
DENSE_BASE = ROOT / "docs/fast_tracking_timing_campaign/data/dense_frequency_conditions.csv"


def rows(path):
    with path.open(newline="") as stream:
        result = list(csv.DictReader(stream))
    for row in result:
        for key, value in row.items():
            if value in ("True", "False"):
                row[key] = value == "True"
            elif value == "":
                row[key] = None
            else:
                with suppress(TypeError, ValueError):
                    row[key] = float(value)
    return result


def finish(fig, png, name):
    fig.savefig(png / name, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def collect_after(root):
    follower = (
        load(root / "analysis/follower.json") if (root / "analysis/follower.json").exists() else {}
    )
    paths, audited = [], []
    for item in follower.get("blocks", []):
        path = Path(item["block_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("Block hash changed")
        timing = Path(item["timing_json"])
        if sha256(timing) != item["timing_sha256"]:
            raise ValueError("Timing analysis hash changed")
        block = load(path)
        for capture in block["captures"] + block["references"]:
            verified_run(capture)
            audited.append(capture)
        paths.append(path)
    result = collect(root, {"rows": audited}, block_paths=paths)
    result["conditions"] = [r for r in result["conditions"] if r["main_attempts"]]
    result["audit"] = {
        "unique_raw_verified_runs": len({r["run_json"] for r in audited}),
        "block_count": len(paths),
        "at": datetime.now(UTC).isoformat(),
    }
    by_block = {r["id"]: r for r in result["blocks"]}
    for r in result["conditions"] + result["fixed"]:
        r["frequency_hz"] = by_block[r["block"]]["frequency_hz"]
    return result


def trial_rows(after):
    before = [{**r, "epoch": "before"} for r in rows(BASE / "trials.csv")]
    new = [
        {
            "epoch": "after",
            "method": "rolling_50ms",
            "frequency_mhz": r["frequency_hz"] / 1e6,
            "configuration": r["configuration"],
            "dwell_us": r["dwell_us"],
            "round": r["round"],
            "control": r["control"],
            "passed": r["base_pass"],
            "phase_rms_deg": r["base_rms_deg"],
            "max_gain_error_db": r["max_gain_error_db"],
            "block": r["block"],
            "source_run": r["run_json"],
        }
        for r in after["rolling"]
    ]
    new += [
        {
            "epoch": "after",
            "method": "fixed_any_budget",
            "frequency_mhz": r["frequency_hz"] / 1e6,
            "configuration": r["configuration"],
            "dwell_us": r["dwell_us"],
            "round": r["round"],
            "control": r["control"],
            "passed": bool(r["any_window_pass"] and r.get("passed", False)),
            "phase_rms_deg": None,
            "max_gain_error_db": r.get("maximum_observable_gain_error_db"),
            "block": r["block"],
            "source_run": r["run_json"],
        }
        for r in after["fixed"]
    ]
    return before + new


def draw_matrix(trials, png):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), layout="constrained")
    for ri, method in enumerate(("rolling_50ms", "fixed_any_budget")):
        for ci, epoch in enumerate(("before", "after")):
            ax = axes[ri, ci]
            matrix = np.full((len(CASES), len(DWELLS)), np.nan)
            counts = {}
            for i, (f, c) in enumerate(CASES):
                for j, dwell in enumerate(DWELLS):
                    group = [
                        r
                        for r in trials
                        if r["epoch"] == epoch
                        and r["method"] == method
                        and r["frequency_mhz"] == f
                        and r["configuration"] == c
                        and r["dwell_us"] == dwell
                        and not r["control"]
                    ]
                    if group:
                        matrix[i, j] = sum(r["passed"] for r in group) / len(group)
                        counts[i, j] = f"{sum(r['passed'] for r in group)}/{len(group)}"
            cmap = plt.get_cmap("RdYlGn").copy()
            cmap.set_bad("#ededed")
            ax.imshow(matrix, vmin=0, vmax=1, cmap=cmap, aspect="auto")
            for i in range(len(CASES)):
                for j in range(len(DWELLS)):
                    ax.text(j, i, counts.get((i, j), "—"), ha="center", va="center")
            ax.set_xticks(range(5), DWELLS)
            ax.set_yticks(range(len(CASES)), [f"{f} MHz / {c}" for f, c in CASES])
            ax.set_xlabel("Per-port dwell (µs)")
            ax.set_title(
                f"{epoch.title()} · {'past-only 50 ms' if ri == 0 else 'fixed, any budget'}"
            )
    fig.suptitle(
        "Main-trial passes / attempts · independent controls and brackets also required\n"
        "Grey means no matched analysis; upper and lower panels use different gates",
        fontsize=14,
    )
    finish(fig, png, "fig02_dwell_matrix.png")


def draw_phase(trials, png):
    fig, axes = plt.subplots(2, len(CASES), figsize=(20, 7), layout="constrained")
    for col, (frequency, config) in enumerate(CASES):
        for epoch in COLOURS:
            selected = [
                r
                for r in trials
                if r["epoch"] == epoch
                and r["method"] == "rolling_50ms"
                and r["frequency_mhz"] == frequency
                and r["configuration"] == config
                and not r["control"]
            ]
            for row, key in enumerate(("phase_rms_deg", "max_gain_error_db")):
                for passed, marker in ((True, "o"), (False, "x")):
                    group = [
                        r for r in selected if r["passed"] == passed and r.get(key) is not None
                    ]
                    axes[row, col].scatter(
                        [
                            DWELLS.index(r["dwell_us"]) + (-0.12 if epoch == "before" else 0.12)
                            for r in group
                        ],
                        [r[key] for r in group],
                        color=COLOURS[epoch],
                        marker=marker,
                        label=epoch if passed else None,
                        s=28,
                    )
                axes[row, col].axhline(10 if row == 0 else 1, color="black", ls="--", lw=0.8)
                axes[row, col].set_xticks(range(5), DWELLS, rotation=45)
                axes[row, col].set_yscale("symlog", linthresh=0.2)
                axes[row, col].grid(alpha=0.2)
        axes[0, col].set_title(f"{frequency} MHz / {config}")
        axes[1, col].set_xlabel("Dwell (µs)")
    axes[0, 0].set_ylabel("50 ms phase RMS (°)")
    axes[1, 0].set_ylabel("Maximum observable gain error (dB)")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Frozen past-only timing · circles pass combined gates; crosses fail\n"
        "New weights reflect the moved fixture; old 5.8 GHz has no matched rolling baseline"
    )
    finish(fig, png, "fig03_phase_and_gain.png")


def draw_bearings(after, png):
    before = rows(ROOT / "docs/completed_switching_analysis/data/rolling.csv")
    fig, axes = plt.subplots(2, len(CASES), figsize=(20, 7), layout="constrained")
    for col, (frequency, config) in enumerate(CASES):
        for epoch, items in (("before", before), ("after", after["rolling"])):
            group = [
                r
                for r in items
                if r["frequency_hz"] == frequency * 1e6
                and r["configuration"] == config
                and not r["control"]
            ]
            for row, key in enumerate(("bearing_rms_deg", "bearing_valid_percent")):
                valid = [r for r in group if r.get(key) is not None]
                axes[row, col].scatter(
                    [
                        DWELLS.index(r["dwell_us"]) + (-0.12 if epoch == "before" else 0.12)
                        for r in valid
                    ],
                    [r[key] for r in valid],
                    label=epoch,
                    color=COLOURS[epoch],
                    s=25,
                )
                axes[row, col].set_xticks(range(5), DWELLS, rotation=45)
                axes[row, col].grid(alpha=0.2)
        axes[0, col].set_title(f"{frequency} MHz / {config}")
        axes[0, col].set_yscale("symlog", linthresh=1)
        axes[1, col].set_ylim(-3, 103)
        axes[1, col].set_xlabel("Dwell (µs)")
    axes[0, 0].set_ylabel("Bearing variation vs static fit (° RMS)")
    axes[1, 0].set_ylabel("Model-valid outputs (%)")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Repeatability is not angular accuracy · all available 50 ms outputs retained\n"
        "Same nominal 51 mm model and PCB LUT; no surveyed post-move truth"
    )
    finish(fig, png, "fig04_bearing_repeatability.png")


def draw_dense(after, png):
    before = rows(DENSE_BASE)
    fig, axes = plt.subplots(3, 2, figsize=(15, 11), layout="constrained")
    for col, tx in enumerate(("TX1", "TX2")):
        for epoch, items in (("before", before), ("after", after)):
            group = [r for r in items if r["tx_port"] == tx]
            for valid, marker in ((True, "o"), (False, "x")):
                selected = [
                    r
                    for r in group
                    if r["full_bearing_valid"] == valid and r["full_bearing_deg"] is not None
                ]
                axes[0, col].scatter(
                    [r["frequency_hz"] / 1e6 for r in selected],
                    [r["full_bearing_deg"] for r in selected],
                    s=11,
                    marker=marker,
                    color=COLOURS[epoch],
                    alpha=0.75,
                    label=f"{epoch} {'admitted' if valid else 'rejected'}",
                )
            for ri, key in (
                (1, "full_bearing_residual_phase_rms_deg"),
                (2, "phase_10deg_wall_latency_ms"),
            ):
                selected = sorted(
                    [r for r in group if r.get(key) is not None], key=lambda r: r["frequency_hz"]
                )
                axes[ri, col].scatter(
                    [r["frequency_hz"] / 1e6 for r in selected],
                    [r[key] for r in selected],
                    s=10,
                    color=COLOURS[epoch],
                )
        axes[0, col].set_title(tx)
        axes[0, col].set_ylim(-5, 365)
        axes[0, col].legend(fontsize=8, ncol=2)
        axes[1, col].axhline(45, color="black", ls="--", lw=0.8)
        axes[2, col].set_yscale("log")
        axes[2, col].set_xlabel("Frequency (MHz)")
        for ri in range(3):
            axes[ri, col].grid(alpha=0.2)
            axes[ri, col].set_xlim(5725, 5875)
    axes[0, 0].set_ylabel("Full-record inferred bearing (°)")
    axes[1, 0].set_ylabel("Spatial phase residual (° RMS)")
    axes[2, 0].set_ylabel("First ≤10° phase RMS budget (ms)")
    fig.suptitle(
        "1 MHz grid · same historical geometry, LUT, native decoder and 200 µs dwell\n"
        "Whole-record analysis, not causal tracking; missing phase passes are omitted, not zero"
    )
    finish(fig, png, "fig05_dense_before_after.png")


def draw_static_model(root, static, png):
    geometry = fixture_geometry(root)
    lut = BoardCalibrationLut.load(LUT)
    frequencies = sorted({r["frequency_hz"] for r in static})
    fig, axes = plt.subplots(2, len(frequencies), figsize=(16, 7), layout="constrained")
    residuals = []
    for col, frequency in enumerate(frequencies):
        steering = far_field_steering(geometry, frequency, GRID)
        for epoch in COLOURS:
            subset = {
                r["port"]: r
                for r in static
                if r["frequency_hz"] == frequency and r["epoch"] == epoch
            }
            h = (
                np.array(
                    [
                        10 ** (subset[p]["magnitude_db"] / 20)
                        * np.exp(1j * np.deg2rad(subset[p]["phase_relative_ant8_deg"]))
                        for p in PORTS
                    ]
                )
                * lut.evaluate(frequency, PORTS).coefficients
            )
            fit = solve_bearing(h, steering, GRID)
            best = steering[np.argmax(fit.likelihood)]
            scale = np.vdot(best, h) / np.vdot(best, best)
            residual = np.angle(h / (scale * best), deg=True)
            axes[0, col].plot(
                GRID,
                fit.likelihood,
                color=COLOURS[epoch],
                label=f"{epoch}: {fit.bearing_deg:g}° ({'admit' if fit.valid else 'reject'})",
            )
            axes[1, col].plot(range(6), residual, "o-", color=COLOURS[epoch], markersize=4)
            for port, value in zip(PORTS, residual, strict=True):
                residuals.append(
                    {
                        "epoch": epoch,
                        "frequency_hz": frequency,
                        "port": port,
                        "phase_residual_deg": float(value),
                        "bearing_deg": fit.bearing_deg,
                        "valid": fit.valid,
                        "reasons": " ".join(fit.reasons),
                    }
                )
        axes[0, col].set_title(f"{frequency / 1e6:g} MHz")
        axes[0, col].set_ylim(0, 1)
        axes[0, col].set_xticks((0, 90, 180, 270, 360))
        axes[0, col].legend(fontsize=7)
        axes[0, col].set_xlabel("Candidate bearing (°)")
        axes[1, col].set_xticks(range(6), PORTS, rotation=45)
        axes[1, col].set_ylim(-180, 180)
        axes[1, col].axhline(0, color="black", lw=0.7)
        for ax in axes[:, col]:
            ax.grid(alpha=0.2)
    axes[0, 0].set_ylabel("Normalized manifold match score")
    axes[1, 0].set_ylabel("Residual vs best spatial fit (°)")
    fig.suptitle(
        "Why a bearing is rejected · same equal-weight 51 mm model and PCB LUT\n"
        "A broad or competing peak is not surveyed direction; only one common complex scale fitted"
    )
    finish(fig, png, "fig06_static_model_residuals.png")
    return residuals


def result_text(after, dense, complete):
    lines = [
        "## Measured results snapshot",
        "",
        (
            "**Full planned acquisition and offline analysis complete.**"
            if complete
            else "**Partial: acquisition and/or offline analysis is still incomplete.**"
        ),
        "",
        f"Completed bracketed blocks analyzed: **{len(after['blocks'])}/7**. "
        f"Switched records analyzed: **{len(after['rolling'])}/117**. "
        f"Dense records analyzed: **{len(dense)}/298**.",
        "",
        "| Centre / profile | Fastest clean tested dwell | Main-trial 50 ms phase RMS | "
        "Passing controls | Reference bracket | Observable ports |",
        "|---|---:|---:|---:|---|---|",
    ]
    for block in sorted(after["blocks"], key=lambda b: (b["frequency_hz"], b["configuration"])):
        conditions = [r for r in after["conditions"] if r["block"] == block["id"]]
        clean = [r for r in conditions if r["clean_condition_pass"]]
        chosen = min(clean, key=lambda r: r["dwell_us"]) if clean else None
        dwell = f"{chosen['dwell_us']} µs" if chosen else "None qualified"
        trials = [
            r
            for r in after["rolling"]
            if r["block"] == block["id"]
            and not r["control"]
            and chosen
            and r["dwell_us"] == chosen["dwell_us"]
        ]
        rms = [r["base_rms_deg"] for r in trials if r["base_rms_deg"] is not None]
        rms_text = f"{min(rms):.3f}–{max(rms):.3f}°" if rms else "—"
        controls = [r for r in after["rolling"] if r["block"] == block["id"] and r["control"]]
        lines.append(
            f"| {block['frequency_hz'] / 1e6:g} MHz / {block['configuration']} | {dwell} | "
            f"{rms_text} | {sum(r['base_pass'] for r in controls)}/{len(controls)} | "
            f"{'Pass' if block['bracket_pass'] else 'Fail'} | {block['observable_ports']} |"
        )
    lines.extend(
        [
            "",
            "A clean condition requires three main trials, every planned control, "
            "complete 50 ms prediction-window coverage and passing before/after references. "
            "This is phase/gain repeatability of a known laboratory source—not surveyed "
            "angular accuracy, unknown-source tracking or measured live throughput.",
            "",
        ]
    )
    if dense:
        before = rows(DENSE_BASE)
        lines.extend(
            [
                "| Source | Before phase admission | After phase admission | "
                "Before spatial-model admission | After spatial-model admission |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for tx in ("TX1", "TX2"):
            old = [r for r in before if r["tx_port"] == tx]
            new = [r for r in dense if r["tx_port"] == tx]
            counts = []
            for group, key in (
                (old, "phase_10deg_wall_latency_ms"),
                (new, "phase_10deg_wall_latency_ms"),
                (old, "full_bearing_valid"),
                (new, "full_bearing_valid"),
            ):
                counts.append(f"{sum(bool(r.get(key)) for r in group)}/{len(group)}")
            lines.append(f"| {tx} | " + " | ".join(counts) + " |")
        lines.extend(
            [
                "",
                "Historical phase admission allows variable integration and requires "
                "the recorded TX2 frequency-fit quality. Spatial admission here is the "
                "full-record legacy model gate, not a surveyed success rate. The two columns "
                "use different observation budgets and criteria.",
                "",
                "![Dense frequency comparison](png/fig05_dense_before_after.png)",
                "",
                "**Figure 5.** Matched historical geometry and native 200 µs decoder on both "
                "dates. Rejected bearing candidates remain visible; failed phase admissions "
                "are not plotted as zero latency.",
                "",
            ]
        )
    return "\n".join(lines)


def update_results(readme, text, complete):
    start, stop = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
    content = readme.read_text()
    if content.count(start) != 1 or content.count(stop) != 1:
        raise ValueError("Report needs one unambiguous generated-results section")
    left, rest = content.split(start)
    _old, right = rest.split(stop)
    content = left + start + "\n" + text + "\n" + stop + right
    if complete:
        content = content.replace(
            "**September 10, 2026 · acquisition and analysis in progress**",
            "**September 10, 2026 · completed repeat campaign**",
        )
        content = content.replace(
            "**September 10–11, 2026 · continuation in progress**",
            "**September 10–11, 2026 · completed repeat campaign; interrupted attempt retained**",
        )
        content = content.replace(
            "The accompanying figures are progressive snapshots, not completed qualification.",
            "The figures retain all completed results and recorded failures.",
        )
        content = content.replace(
            "Final post-campaign restoration and mute checks are still pending.",
            "Final recorded post-campaign checks confirm both pinned radios muted, selector "
            "ALL_OFF with no active lease, and a byte-exact restore of the initial 16 KiB image.",
        )
    readme.write_text(content)


def verify_completion(root, dense_rows):
    manifest = load(root / "dense.json")
    expected = {(f, tx) for f in range(5726000000, 5874000001, 1000000) for tx in ("TX1", "TX2")}
    actual = {(int(r["frequency_hz"]), r["tx_port"]) for r in dense_rows}
    if len(dense_rows) != 298 or actual != expected:
        raise ValueError("Dense grid is incomplete or duplicated")
    if {r["run_json"] for r in dense_rows} != {r["run_json"] for r in manifest["captures"]}:
        raise ValueError("Dense table and acquired records differ")
    summary = load(root / "analysis/dense-summary.json")
    if summary["manifest_sha256"] != sha256(root / "dense.json"):
        raise ValueError("Dense analysis predates final acquisition manifest")
    restore_item = manifest["restore"]
    restore_path = Path(restore_item["path"])
    if sha256(restore_path) != restore_item["sha256"]:
        raise ValueError("Final restore evidence hash differs")
    restore = load(restore_path)
    image = restore["restored_flash"]
    if (
        restore["status"] != "passed"
        or not image["matches_backup"]
        or image["size_bytes"] != 16384
        or sha256(Path(image["path"])) != image["sha256"]
        or image["sha256"] != sha256(root / "initial-selector-flash.bin")
    ):
        raise ValueError("Final selector image is not the exact initial image")
    selector = restore["selector_status"]
    if selector["applied_code"] != 8 or selector["command_code"] != 8 or selector["lease_active"]:
        raise ValueError("Selector was not left ALL_OFF")
    muted = restore["final_radio_mute"]
    serials = {"104000b29905000e17000800065934759d", "104473b80a16000de6ff2000f8a6beca79"}
    if (
        len(muted) != 2
        or {r["serial"] for r in muted} != serials
        or any(r["dds_scales"] != [0.0] * 8 or r["tx_gain_db"] != [-80.0, -80.0] for r in muted)
    ):
        raise ValueError("Final mute readbacks are incomplete")
    return {
        "restore_json": str(restore_path),
        "sha256": restore_item["sha256"],
        "verified_at": restore["completed_at"],
        "initial_image_sha256": image["sha256"],
        "both_pinned_radios_muted": True,
        "selector_all_off": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    root, output = args.campaign_root.resolve(strict=True), args.output_dir
    data, png = output / "data", output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    result = collect_after(root)
    trials = trial_rows(result)
    for name in ("blocks", "fixed", "rolling", "conditions"):
        table(data / f"after-{name}.csv", result[name])
    table(data / "comparison-trials.csv", trials)
    for name in ("static-before-after.csv", "static-bearings.csv", "static-audit.json"):
        shutil.copyfile(root / "analysis" / name, data / name)
    shutil.copyfile(
        root / "analysis/fig01_static_before_after.png", png / "fig01_static_before_after.png"
    )
    residuals = draw_static_model(root, rows(data / "static-before-after.csv"), png)
    table(data / "static-model-residuals.csv", residuals)
    draw_matrix(trials, png)
    draw_phase(trials, png)
    draw_bearings(result, png)
    dense_path = root / "analysis/dense-after.csv"
    dense_rows = rows(dense_path) if dense_path.exists() else []
    if dense_rows:
        shutil.copyfile(dense_path, data / "dense-after.csv")
        draw_dense(dense_rows, png)
    stages = {
        name: load(root / f"{name}.json")["status"]
        if (root / f"{name}.json").exists()
        else "not-started"
        for name in ("screens", "blocks", "dense")
    }
    complete = (
        all(s == "complete" for s in stages.values())
        and len(result["blocks"]) == 7
        and len(dense_rows) == 298
    )
    safety = verify_completion(root, dense_rows) if complete else None
    continuation_path = root / "continuation.json"
    continuation = load(continuation_path) if continuation_path.exists() else None
    if (output / "README.md").exists():
        update_results(output / "README.md", result_text(result, dense_rows, complete), complete)
    dump(
        data / "report-audit.json",
        {
            "schema": 1,
            "status": "complete" if complete else "partial",
            "stages": stages,
            "raw_audit": result["audit"],
            "dense_rows": len(dense_rows),
            "final_safety": safety,
            "campaign_root": str(root),
            "continuation": continuation,
            "continuation_sha256": sha256(continuation_path) if continuation else None,
            "analysis_inputs": result["inputs"],
            "sources": {
                str(p): sha256(p)
                for p in (
                    Path(__file__),
                    ROOT / "scripts/render_completed_switching_analysis.py",
                    BASE / "trials.csv",
                    BASE / "conditions.csv",
                    DENSE_BASE,
                )
            },
            "figures": {str(p.name): sha256(p) for p in sorted(png.glob("*.png"))},
        },
    )
    dump(
        root / "analysis/report-summary.json",
        {
            "status": "complete" if complete else "partial",
            "stages": stages,
            "blocks": result["blocks"],
            "conditions": result["conditions"],
            "rolling_attempts": len(result["rolling"]),
            "rolling_passes": sum(r["base_pass"] for r in result["rolling"]),
            "report": str(output),
        },
    )
    print(
        json.dumps(
            {
                "status": "complete" if complete else "partial",
                "blocks": len(result["blocks"]),
                "dense_rows": len(dense_rows),
            }
        )
    )


if __name__ == "__main__":
    main()
