#!/usr/bin/env python3
"""Standalone 5 MS/s report; old reports and historical caches remain unchanged."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from analyze_jitter_comparison import relative_phase, save, verified_run  # noqa: E402
from render_completed_switching_analysis import table  # noqa: E402
from render_jitter_report import collect_after, rows, verify_completion  # noqa: E402
from run_5ms_full_campaign import CENTRES, OUTPUT, ROOT  # noqa: E402

from smateway.rate_timing import PORTS, complex_value, load, sha256  # noqa: E402

OUT = ROOT / "docs/full_5ms_campaign"
OLD = ROOT / "docs/jittered_fixture_comparison/data"
DWELLS = (25, 50, 100, 200, 1000)
COLORS = {"A": "#777777", "B": "#287c8e", "D": "#c05a32"}
LABELS = {"A": "Prior 2 MS/s / 1.6 MHz", "B": "New 5 MS/s / 1.6 MHz", "D": "New 5 MS/s / 4 MHz"}


def figure(fig, png, name):
    fig.savefig(png / name, dpi=160, bbox_inches="tight")
    plt.close(fig)


def transport_summary(root):
    records, inputs = [], []
    complete_requests = running_requests = recovered_requests = failed_requests = 0
    for path in sorted((root / "transport-attempts").glob("*.json")):
        evidence = load(path)
        inputs.append({"path": str(path), "sha256": sha256(path)})
        running_requests += evidence["status"] == "running"
        complete_requests += evidence["status"] == "passed"
        failed_requests += evidence["status"] == "failed"
        recovered_requests += evidence["status"] == "passed" and len(evidence["attempts"]) > 1
        for index, attempt in enumerate(evidence["attempts"], 1):
            records.append(
                {
                    "request": path.name,
                    "attempt": index,
                    "status": attempt["status"],
                    "run_json": attempt["run_json"],
                    "sha256": attempt["sha256"],
                    "error": (attempt.get("error") or {}).get("message"),
                }
            )
    return {
        "scope": (
            "Latest continuation only; includes recovery/screens, references and main captures"
        ),
        "complete_requests": complete_requests,
        "failed_requests": failed_requests,
        "running_requests": running_requests,
        "recovered_requests": recovered_requests,
        "recorded_attempts": len(records),
        "failed_attempts": sum(r["status"] == "failed" for r in records),
        "inputs": inputs,
    }, records


def condition_details(result):
    output = []
    for condition in result["conditions"]:
        selected = [
            r
            for r in result["rolling"]
            if r["block"] == condition["block"]
            and not r["control"]
            and r["dwell_us"] == condition["dwell_us"]
        ]
        row = dict(condition)
        row["bearing_passes"] = sum(bool(r.get("bearing_pass")) for r in selected)
        row["joint_main_passes"] = sum(
            bool(r["base_pass"] and r.get("bearing_pass")) for r in selected
        )
        row["clean_phase_and_bearing"] = bool(
            condition["clean_condition_pass"]
            and len(selected) == 3
            and row["joint_main_passes"] == 3
        )
        for metric in (
            "base_rms_deg",
            "bearing_rms_deg",
            "bearing_valid_percent",
            "p95_compute_ms",
        ):
            values = [r[metric] for r in selected if r.get(metric) is not None]
            row[f"{metric}_reported_trials"] = len(values)
            row[f"{metric}_median"] = float(np.median(values)) if values else None
            row[f"{metric}_max"] = max(values) if values else None
        output.append(row)
    return sorted(output, key=lambda r: (r["frequency_hz"], r["configuration"], r["dwell_us"]))


def analysis_coverage(root):
    """Account for rejected records and every planned rolling output separately."""
    counts = dict(
        records=0,
        rejected_records=0,
        expected_windows=0,
        analyzed_windows=0,
        failed_windows=0,
        unattempted_windows=0,
    )
    failures, inputs = [], []
    for item in load(root / "analysis/follower.json")["blocks"]:
        path = Path(item["timing_json"])
        if sha256(path) != item["timing_sha256"]:
            raise ValueError("Timing analysis hash changed during coverage audit")
        inputs.append({"path": str(path), "sha256": item["timing_sha256"]})
        for row in load(path)["rows"]:
            cfg = row["configuration"]
            expected = round((cfg["duration_s"] - 1) / 0.05)
            windows = (row.get("rolling_past_only") or {}).get("windows", [])
            if len(windows) > expected:
                raise ValueError("Unexpected rolling-window count")
            failed = [w for w in windows if w["status"] != "analyzed"]
            missing = expected - len(windows)
            counts["records"] += 1
            counts["rejected_records"] += row["status"] != "analyzed"
            counts["expected_windows"] += expected
            counts["analyzed_windows"] += len(windows) - len(failed)
            counts["failed_windows"] += len(failed)
            counts["unattempted_windows"] += missing
            if row["status"] != "analyzed" or failed or missing:
                reasons = sorted({str(w.get("error", "unspecified")) for w in failed})
                if row.get("error"):
                    reasons.append(row["error"])
                failures.append(
                    {
                        "frequency_hz": cfg["frequency_hz"],
                        "configuration": cfg["name"],
                        "dwell_us": cfg["dwell_us"],
                        "round": row["round"],
                        "control": row["control"],
                        "record_status": row["status"],
                        "failed_windows": len(failed),
                        "unattempted_windows": missing,
                        "error": "; ".join(sorted(set(reasons))),
                        "run_json": row["run_json"],
                    }
                )
    return {**counts, "inputs": inputs}, failures


def paired_controls(result):
    blocks = {b["id"]: b for b in result["blocks"]}
    pairs = []
    for main in result["rolling"]:
        if main["control"] or main["dwell_us"] not in (200, 1000):
            continue
        controls = [
            r
            for r in result["rolling"]
            if r["control"]
            and r["configuration"] == "A"
            and r["block"] == main["block"]
            and r["round"] == main["round"]
            and r["dwell_us"] == main["dwell_us"]
        ]
        if len(controls) != 1:
            raise ValueError("Expected one same-round same-dwell interleaved A control")
        control = controls[0]
        one, two = main.get("base_rms_deg"), control.get("base_rms_deg")
        pairs.append(
            {
                "block": main["block"],
                "frequency_hz": main["frequency_hz"],
                "configuration": main["configuration"],
                "dwell_us": main["dwell_us"],
                "round": main["round"],
                "main_phase_rms_deg": one,
                "control_phase_rms_deg": two,
                "phase_rms_difference_deg": one - two
                if one is not None and two is not None
                else None,
                "pair_phase_gain_bracket_pass": bool(
                    main["base_pass"]
                    and control["base_pass"]
                    and blocks[main["block"]]["bracket_pass"]
                ),
                "main_run_json": main["run_json"],
                "control_run_json": control["run_json"],
            }
        )
    return pairs


def paired_control_figure(pairs, png):
    columns = [(c, d) for c in ("B", "D") for d in (200, 1000)]
    values = np.full((4, 4), np.nan)
    labels = {}
    for i, (frequency, _gain) in enumerate(CENTRES):
        for j, (config, dwell) in enumerate(columns):
            selected = [
                r
                for r in pairs
                if r["frequency_hz"] == frequency
                and r["configuration"] == config
                and r["dwell_us"] == dwell
            ]
            differences = [
                r["phase_rms_difference_deg"]
                for r in selected
                if r["phase_rms_difference_deg"] is not None
            ]
            if differences:
                values[i, j] = np.median(differences)
                passed = sum(r["pair_phase_gain_bracket_pass"] for r in selected)
                labels[i, j] = f"{values[i, j]:+.2f}°\n{passed}/{len(selected)} pairs pass"
    finite = np.abs(values[np.isfinite(values)])
    limit = max(1.0, float(np.max(finite))) if finite.size else 1.0
    fig, ax = plt.subplots(figsize=(10, 6), layout="constrained")
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad("#eeeeee")
    plot = ax.imshow(values, cmap=cmap, vmin=-limit, vmax=limit, aspect="auto")
    for i in range(4):
        for j in range(4):
            ax.text(
                j,
                i,
                labels.get((i, j), "—"),
                ha="center",
                va="center",
                fontsize=9,
                bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "none"},
            )
    ax.set_xticks(range(4), [f"{c} · {d} µs" for c, d in columns])
    ax.set_yticks(range(4), [f"{f / 1e6:g} MHz" for f, _ in CENTRES])
    fig.colorbar(plot, ax=ax, label="Median paired phase-RMS difference (5 MS/s − 2 MS/s), °")
    ax.set_title(
        "Same-round interleaved controls · negative = lower phase RMS at 5 MS/s\n"
        "B holds 1.6 MHz bandwidth; D also widens bandwidth to 4 MHz\n"
        "Pair passes include phase/gain and bracket gates, not bearing validity"
    )
    figure(fig, png, "fig07_same_session_controls.png")


def compute_figure(result, png):
    fig, axes = plt.subplots(1, 4, figsize=(16, 4.5), sharey=True, layout="constrained")
    for ax, (frequency, _gain) in zip(axes, CENTRES, strict=True):
        for config in ("B", "D"):
            selected = [
                r
                for r in result["rolling"]
                if not r["control"]
                and r["configuration"] == config
                and r["frequency_hz"] == frequency
                and r.get("p95_compute_ms") is not None
            ]
            ax.scatter(
                [
                    DWELLS.index(r["dwell_us"]) + (0.08 if config == "D" else -0.08)
                    for r in selected
                ],
                [r["p95_compute_ms"] for r in selected],
                color=COLORS[config],
                label=LABELS[config],
                s=24,
            )
        ax.axhline(50, color="black", ls="--", lw=0.8)
        ax.set_xticks(range(5), DWELLS)
        ax.set_xlabel("Dwell (µs)")
        ax.set_title(f"{frequency / 1e6:g} MHz")
        ax.set_yscale("log")
        ax.grid(alpha=0.2)
        if not any(r["frequency_hz"] == frequency for r in result["rolling"]):
            ax.text(0.5, 0.5, "No completed block yet", ha="center", transform=ax.transAxes)
    axes[0].set_ylabel("Per-trial p95 replay compute time (ms)")
    axes[0].legend(fontsize=7)
    fig.suptitle(
        "Offline rolling-analysis cost · dashed line = 50 ms output cadence\n"
        "Shared-host replay measurements, not an end-to-end runtime benchmark"
    )
    figure(fig, png, "fig06_compute_budget.png")


def metrics_figures(result, png):
    old = rows(OLD / "after-rolling.csv")
    source = [r for r in old if r["configuration"] == "A" and not r["control"]]
    source += [r for r in result["rolling"] if not r["control"]]
    fig, axes = plt.subplots(1, 3, figsize=(14, 5), layout="constrained")
    for col, config in enumerate(("A", "B", "D")):
        ax = axes[col]
        matrix = np.full((4, 5), np.nan)
        for i, (frequency, _gain) in enumerate(CENTRES):
            for j, dwell in enumerate(DWELLS):
                selected = [
                    r
                    for r in source
                    if r["frequency_hz"] == frequency
                    and r["configuration"] == config
                    and r["dwell_us"] == dwell
                ]
                if selected:
                    passes = sum(r["base_pass"] for r in selected)
                    matrix[i, j] = passes / len(selected)
                    ax.text(j, i, f"{passes}/{len(selected)}", ha="center", va="center")
                else:
                    ax.text(j, i, "—", ha="center", va="center")
        cmap = plt.get_cmap("RdYlGn").copy()
        cmap.set_bad("#eeeeee")
        ax.imshow(matrix, cmap=cmap, vmin=0, vmax=1, aspect="auto")
        ax.set_xticks(range(5), DWELLS)
        ax.set_yticks(range(4), [f"{f / 1e6:g} MHz" for f, _ in CENTRES])
        ax.set_xlabel("Dwell (µs)")
        ax.set_title(LABELS[config])
    fig.suptitle(
        "Main-trial phase/gain passes · 50 ms past-only outputs\n"
        "Controls and reference brackets also required; grey = missing/not tested"
    )
    figure(fig, png, "fig01_dwell_pass_matrix.png")
    for filename, keys, labels, gates in (
        (
            "fig02_phase_gain.png",
            ("base_rms_deg", "max_gain_error_db"),
            ("Phase RMS (°)", "Maximum observable gain error (dB)"),
            (10, 1),
        ),
        (
            "fig03_bearing.png",
            ("bearing_rms_deg", "bearing_valid_percent"),
            ("Bearing RMS vs static fit (°)", "Model-valid outputs (%)"),
            (5, 95),
        ),
    ):
        fig, axes = plt.subplots(2, 4, figsize=(16, 8), sharey="row", layout="constrained")
        for col, (frequency, _gain) in enumerate(CENTRES):
            for config in ("A", "B", "D"):
                for ri, key in enumerate(keys):
                    selected = [
                        r
                        for r in source
                        if r["frequency_hz"] == frequency
                        and r["configuration"] == config
                        and r.get(key) is not None
                    ]
                    for passed, marker in ((True, "o"), (False, "x")):
                        chosen = [r for r in selected if r["base_pass"] == passed]
                        axes[ri, col].scatter(
                            [
                                DWELLS.index(r["dwell_us"])
                                + {"A": -0.15, "B": 0, "D": 0.15}[config]
                                for r in chosen
                            ],
                            [r[key] for r in chosen],
                            s=22,
                            marker=marker,
                            color=COLORS[config],
                            label=LABELS[config] if passed else None,
                        )
                    axes[ri, col].set_xticks(range(5), DWELLS)
                    axes[ri, col].grid(alpha=0.2)
            axes[0, col].set_title(f"{frequency / 1e6:g} MHz")
            for ri in range(2):
                axes[ri, col].axhline(gates[ri], color="black", ls="--", lw=0.7)
                if keys[ri] != "bearing_valid_percent":
                    axes[ri, col].set_yscale("symlog", linthresh=0.2)
                else:
                    axes[ri, col].set_ylim(-3, 103)
            axes[1, col].set_xlabel("Dwell (µs)")
        for ri in range(2):
            axes[ri, 0].set_ylabel(labels[ri])
            if keys[ri] != "bearing_valid_percent":
                values = [r[keys[ri]] for r in source if r.get(keys[ri]) is not None]
                axes[ri, 0].set_ylim(0, 1.2 * max([gates[ri], *values]))
        axes[0, 0].legend(fontsize=7)
        fig.suptitle(
            "Circles pass phase/gain gates; crosses fail · bearing admission is separate\n"
            "Before/after acquisition times and reference weights differ; no surveyed truth"
        )
        figure(fig, png, filename)


def static_figure(root, png):
    path = root / "screens.json"
    if not path.exists():
        return
    screen = load(path)
    recovery_path = root / "remaining-headroom.json"
    recovery = load(recovery_path) if recovery_path.exists() else {"captures": []}
    fig, axes = plt.subplots(4, 4, figsize=(16, 13), layout="constrained")
    for col, (frequency, _gain) in enumerate(CENTRES):
        for config in ("B", "D"):
            selected = {
                r["port"]: r
                for r in screen["captures"]
                if r["frequency_hz"] == frequency
                and r["configuration"] == config
                and r["mode"] == "static"
            }
            refreshed = {
                r["port"]: r
                for r in recovery["captures"]
                if r["frequency_hz"] == frequency
                and r["configuration"] == config
                and r["mode"] == "static"
                and r["status"] == "passed"
            }
            if set(refreshed) == set(PORTS):
                selected = refreshed
            if set(selected) != set(PORTS):
                continue
            for r in selected.values():
                verified_run(r)
            h = np.array([complex_value(selected[p]["reference"]["transfer"]) for p in PORTS])
            values = (
                20 * np.log10(abs(h)),
                relative_phase(h),
                [selected[p]["reference"]["phase_rms_10ms_deg"] for p in PORTS],
                [selected[p]["reference"]["coherence"] for p in PORTS],
            )
            for ri, data in enumerate(values):
                axes[ri, col].plot(
                    range(6), data, "o-", color=COLORS[config], label=LABELS[config], markersize=3
                )
        for ri in range(4):
            axes[ri, col].set_xticks(range(6), PORTS, rotation=45)
            axes[ri, col].grid(alpha=0.2)
        axes[0, col].set_title(f"{frequency / 1e6:g} MHz")
        axes[3, col].set_ylim(0, 1)
    for ri, label in enumerate(
        (
            "RX2/RX1 magnitude (dB)",
            "Phase relative ANT8 (°)",
            "Static 10 ms phase RMS (°)",
            "Raw normalized coherence |ρ|",
        )
    ):
        axes[ri, 0].set_ylabel(label)
    axes[0, 0].legend(fontsize=7)
    fig.suptitle(
        "Fresh native 5 MS/s antenna comparison · same RX gain per centre\n"
        "B = 1.6 MHz bandwidth; D = 4 MHz bandwidth; no per-port fitting"
    )
    figure(fig, png, "fig04_static_antennas.png")


def dense_figure(new, png):
    old = rows(OLD / "dense-after.csv")
    fig, axes = plt.subplots(3, 2, figsize=(15, 11), sharey="row", layout="constrained")
    for col, tx in enumerate(("TX1", "TX2")):
        for label, dataset, color in (
            ("Prior 2 MS/s", old, COLORS["B"]),
            ("New 5 MS/s", new, COLORS["D"]),
        ):
            for ri, key in enumerate(
                (
                    "full_bearing_deg",
                    "full_bearing_residual_phase_rms_deg",
                    "phase_10deg_wall_latency_ms",
                )
            ):
                for valid, marker in ((True, "o"), (False, "x")):
                    selected = [
                        r
                        for r in dataset
                        if r["tx_port"] == tx
                        and r.get(key) is not None
                        and r["full_bearing_valid"] == valid
                    ]
                    axes[ri, col].scatter(
                        [r["frequency_hz"] / 1e6 for r in selected],
                        [r[key] for r in selected],
                        marker=marker,
                        color=color,
                        s=10,
                        label=f"{label} {'admitted' if valid else 'rejected'}",
                    )
                axes[ri, col].grid(alpha=0.2)
                axes[ri, col].set_xlim(5725, 5875)
        axes[0, col].set_title(tx)
        axes[0, col].set_ylim(-5, 365)
        axes[0, col].legend(fontsize=7, ncol=2)
        axes[1, col].axhline(45, color="black", lw=0.7, ls="--")
        axes[2, col].set_yscale("log")
        axes[2, col].set_xlabel("Frequency (MHz)")
    for ri, label in enumerate(
        (
            "Full-record bearing (°)",
            "Spatial phase residual RMS (°)",
            "First admitted phase budget (ms)",
        )
    ):
        axes[ri, 0].set_ylabel(label)
    fig.suptitle(
        "1 MHz dense grid · 200 µs dwell · identical historical geometry and PCB LUT\n"
        "Native 5 MS/s / 4 MHz vs prior 2 MS/s / 1.6 MHz; whole-record, not causal validation"
    )
    figure(fig, png, "fig05_dense_comparison.png")


def render(root, output=OUT):
    data, png = output / "data", output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    result = collect_after(root)
    for name in ("blocks", "conditions", "rolling", "fixed"):
        table(data / f"{name}.csv", result[name])
    details = condition_details(result)
    table(data / "condition-details.csv", details)
    coverage, coverage_failures = analysis_coverage(root)
    if coverage_failures:
        table(data / "analysis-failures.csv", coverage_failures)
    pairs = paired_controls(result)
    table(data / "paired-controls.csv", pairs)
    paired_control_figure(pairs, png)
    metrics_figures(result, png)
    compute_figure(result, png)
    static_figure(root, png)
    dense_path = root / "analysis/dense-after.csv"
    dense_rows = rows(dense_path) if dense_path.exists() else []
    if dense_rows:
        table(data / "dense.csv", dense_rows)
        dense_figure(dense_rows, png)
    transport, attempts = transport_summary(root)
    if attempts:
        table(data / "transport-attempts.csv", attempts)
    stages = {
        s: load(root / f"{s}.json")["status"] if (root / f"{s}.json").exists() else "not-started"
        for s in ("screens", "blocks", "dense")
    }
    complete = (
        all(s == "complete" for s in stages.values())
        and len(result["blocks"]) == 8
        and len(result["rolling"]) == 150
        and len(dense_rows) == 298
    )
    safety = verify_completion(root, dense_rows) if complete else None
    audit = {
        "status": "complete" if complete else "partial",
        "rolling_analysis_coverage": coverage,
        "stages": stages,
        "updated_at": datetime.now(UTC).isoformat(),
        "campaign_root": str(root),
        "raw_audit": result["audit"],
        "blocks": len(result["blocks"]),
        "rolling_records": len(result["rolling"]),
        "dense_records": len(dense_rows),
        "dense_analysis_successes": sum(r["status"] == "analyzed" for r in dense_rows),
        "dense_analysis_failures": sum(r["status"] != "analyzed" for r in dense_rows),
        "transport": transport,
        "phase_gain_passes": sum(r["base_pass"] for r in result["rolling"]),
        "final_safety": safety,
        "continuation": load(root / "plan.json").get("continuation"),
        "screen_sources": {
            str(p): sha256(p)
            for p in (root / "screens.json", root / "remaining-headroom.json")
            if p.exists()
        },
        "inputs": result["inputs"],
        "sources": {
            str(p): sha256(p)
            for p in (
                Path(__file__),
                root / "plan.json",
                ROOT / "scripts/analyze_full_5ms_dense.py",
                ROOT / "scripts/render_completed_switching_analysis.py",
                ROOT / "scripts/render_jitter_report.py",
                OLD / "after-rolling.csv",
                OLD / "dense-after.csv",
            )
        },
        "figures": {p.name: sha256(p) for p in sorted(png.glob("*.png"))},
    }
    save(data / "audit.json", audit)
    save(root / "analysis/report-summary.json", audit)
    lines = [
        "# Full native 5 MS/s tracking campaign",
        "",
        f"**Status: {audit['status']} · September 11, 2026**",
        "",
        f"{len(result['blocks'])}/8 completed blocks analyzed; {len(result['rolling'])}/150 "
        f"switched records; {len(dense_rows)}/298 dense records.",
        "",
        f"Among {len(details)} analyzed main-trial conditions, "
        f"{sum(r['clean_condition_pass'] for r in details)} meet the full phase/gain "
        "qualification gates (including controls and reference brackets), and "
        f"{sum(r['clean_phase_and_bearing'] for r in details)} also meet the main-trial "
        "bearing gates. Neither count establishes surveyed angular accuracy.",
        "",
        "## What changed",
        "",
        "New raw captures use 5 MS/s at every requested centre and on the full dense grid; "
        "no 2 MS/s recording is upsampled. D = 5 MS/s / 4 MHz RX bandwidth; B = 5 MS/s / "
        "1.6 MHz RX bandwidth. Separate B/D blocks run at 915, 2475, 5800 and 5811 MHz. "
        "Centre/dwell blocks use TX1. The 5726–5874 MHz, 1 MHz-step TX1/TX2 grid "
        "uses D at 200 µs dwell.",
        "",
        "The frozen timing recipe still uses independent A (2 MS/s) weight references and "
        "interleaved A controls. They are labeled controls, not 5 MS/s main trials. Each B/D "
        "block has fresh before/after A and rate-specific references. TX settings remain "
        "−35 dB hardware gain and DDS 0.25; source rate/bandwidth stay 2 MS/s / 1.6 MHz.",
        "",
        "## Fixture and what this test represents",
        "",
        "The confirmed fixture uses C6 ports ANT1, ANT2, ANT4, ANT8, ANT7, ANT5 on a "
        "nominal 51 mm-diameter circle. Wiring remains:",
        "",
        "```text",
        "TX1 -> two-way splitter -> attenuator -> RX1 (conducted reference)",
        "                        -> OTA emitter -> C6 antennas -> PCB common -> RX2",
        "TX2 -> separate OTA emitter -----------> same C6 antennas",
        "```",
        "",
        "TX2 is enabled only in explicitly selected coherent-pilot tests. The conducted RX1 "
        "reference makes this a controlled timing/phase experiment, not a qualification of "
        "an autonomous receiver locating arbitrary emitters. An eventual OTA reference and "
        "deployed antenna/cable responses need their own validation. Emitter positions were "
        "only approximately retained after the setup was jittered; no surveyed bearing truth "
        "is available. The existing board calibration LUT is used without fitting new "
        "per-port OTA corrections to these test records.",
        "",
        "## Dwell results",
        "",
        "| Centre / profile | Fastest clean tested dwell | Controls passing | Bracket |",
        "|---|---:|---:|---|",
    ]
    for b in sorted(result["blocks"], key=lambda r: (r["frequency_hz"], r["configuration"])):
        cond = [
            r for r in result["conditions"] if r["block"] == b["id"] and r["clean_condition_pass"]
        ]
        fastest = f"{min(r['dwell_us'] for r in cond)} µs" if cond else "None qualified"
        controls = [r for r in result["rolling"] if r["block"] == b["id"] and r["control"]]
        lines.append(
            f"| {b['frequency_hz'] / 1e6:g} MHz / {b['configuration']} | {fastest} | "
            f"{sum(r['base_pass'] for r in controls)}/{len(controls)} | "
            f"{'Pass' if b['bracket_pass'] else 'Fail'} |"
        )
    lines += [
        "",
        "All main trials, all controls, full window coverage and reference brackets "
        "must pass for a clean condition. Phase RMS uses 50 ms prediction windows after "
        "a one-second training history. Whole-record dense phase results use a different "
        "variable-integration criterion and must not be pooled with rolling qualification.",
        "",
    ]
    lines += [
        "## Timing-decoder coverage and rejected outputs",
        "",
        f"Across {coverage['records']} switched records, the recipe plans "
        f"{coverage['expected_windows']} rolling outputs. "
        f"{coverage['analyzed_windows']} windows were analyzed, "
        f"{coverage['failed_windows']} failed timing analysis, and "
        f"{coverage['unattempted_windows']} were not attempted after record-level rejection. "
        f"There are {coverage['rejected_records']} record-level analysis failures. "
        "Analyzed windows are not necessarily phase/gain or bearing passes.",
        "",
    ]
    if coverage_failures:
        lines += [
            "| MHz / profile | Dwell µs | Round | Role | Failed windows | Unattempted windows |",
            "|---|---:|---:|---|---:|---:|",
        ]
        for failure in coverage_failures:
            lines.append(
                f"| {failure['frequency_hz'] / 1e6:g} / {failure['configuration']} | "
                f"{failure['dwell_us']} | {failure['round']} | "
                f"{'Control' if failure['control'] else 'Main'} | "
                f"{failure['failed_windows']} | {failure['unattempted_windows']} |"
            )
        lines += [
            "",
            "Failure reasons and run identities are retained in "
            "[analysis failures](data/analysis-failures.csv). The observed harmonic-consensus "
            "rejection means the decoder could not find three switching-cycle frequency "
            "estimates agreeing within its 0.5% tolerance. It is a timing-recovery failure, "
            "not proof of physical switch malfunction. The frozen recipe and rejection "
            "thresholds were not relaxed, and failed windows were not compressed into a "
            "falsely continuous series.",
            "",
        ]
    lines += [
        "## Primary replay versus whole-record diagnostic",
        "",
        "| MHz / profile | Rolling 50 ms main-trial passes | Whole-record diagnostic passes |",
        "|---|---:|---:|",
    ]
    for b in sorted(result["blocks"], key=lambda r: (r["frequency_hz"], r["configuration"])):
        rolling = [r for r in result["rolling"] if r["block"] == b["id"] and not r["control"]]
        fixed = [r for r in result["fixed"] if r["block"] == b["id"] and not r["control"]]
        fixed_passes = sum(bool(r.get("any_window_pass") and r.get("passed")) for r in fixed)
        lines.append(
            f"| {b['frequency_hz'] / 1e6:g} / {b['configuration']} | "
            f"{sum(r['base_pass'] for r in rolling)}/{len(rolling)} | "
            f"{fixed_passes}/{len(fixed)} |"
        )
    lines += [
        "",
        "These are trial counts across all tested dwells, not complete-condition or bearing "
        "qualifications. The whole-record diagnostic uses timing estimated from the entire "
        "capture and allows its tested integration budgets; both phase repeatability and "
        "independent phase/gain closure must pass. It cannot replace a failed past-only rolling "
        "trial or establish causal latency. Detailed diagnostic results remain in "
        "[fixed replay](data/fixed.csv); the primary results are in "
        "[rolling replay](data/rolling.csv).",
        "",
    ]
    lines += [
        "## Phase repeatability is not bearing validity",
        "",
        "The fastest-clean-dwell table above qualifies phase/gain stability only. "
        "A direction-finding result additionally needs an admitted spatial fit and repeatable "
        "bearing. Low bearing RMS against a static fit can coexist with a consistently rejected "
        "spatial model; that is not successful localization. No emitter direction was surveyed.",
        "",
        "The following table retains all main trials. Phase RMS is the median of the "
        "per-trial rolling metrics, not a pooled RMS. Bearing-valid percentage is likewise a "
        "median across trials. Missing metrics remain absent, never zero; reported-trial "
        "counts and maxima are in [the detailed CSV](data/condition-details.csv).",
        "",
        "| MHz / profile | Dwell µs | Phase/gain passes | Phase RMS ° | "
        "Bearing passes | Valid bearing outputs % | Joint qualification |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in details:
        phase = row["base_rms_deg_median"]
        valid = row["bearing_valid_percent_median"]
        lines.append(
            f"| {row['frequency_hz'] / 1e6:g} / {row['configuration']} | {row['dwell_us']} | "
            f"{row['main_passes']}/{row['main_attempts']} | "
            f"{format(phase, '.2f') if phase is not None else '—'} | "
            f"{row['bearing_passes']}/{row['main_attempts']} | "
            f"{format(valid, '.1f') if valid is not None else '—'} | "
            f"{'Pass' if row['clean_phase_and_bearing'] else 'Not qualified'} |"
        )
    lines += [
        "",
        "Joint qualification requires all three main trials to pass phase/gain and bearing, "
        "plus the existing phase/gain control and reference-bracket gates. It still measures "
        "consistency with the assumed array model, not angular accuracy against ground truth.",
        "",
        "## Timing and sample-rate interpretation",
        "",
        "At 5 MS/s, 25/50/100/200/1000 µs dwells contain 125/250/500/1000/5000 "
        "raw samples respectively. More samples do not make the RF switch settle sooner, "
        "remove multipath, or improve an incorrect array model. Samples within the receiver "
        "bandwidth are correlated, so 2.5× the sample rate is not automatically 2.5× more "
        "independent information. Comparing B and D tests bandwidth at the same sample rate.",
        "",
        "The rolling estimator predicts 50 ms outputs using a one-second training history. "
        "Dwell, full-array cycle, integration time, output cadence and computation latency "
        "are distinct quantities. Figure 6 reports each trial's measured p95 replay cost. "
        "A value above 50 ms exceeds the output-cadence budget for a serial implementation "
        "of this replay path; a value below it alone does not prove real-time feasibility. "
        "These timings include shared-host contention and exclude a complete live I/O pipeline.",
        "",
        "The [frozen timing recipe](../comprehensive_fast_switching/"
        "REFERENCE-TIMING-VALIDATION-v1.md) aligns a separately measured, known-emitter "
        "six-port template. Passing these static-scene trials does not validate source-independent "
        "port labeling or timing acquisition for a moving unknown emitter. That requires its "
        "own detector and held-out motion tests; these measurements must not be presented "
        "as a completed general-purpose tracker.",
        "",
        "Figure 7 uses the same-round, same-dwell interleaved A controls, not the older baseline. "
        "B versus A holds RX bandwidth at 1.6 MHz and uses the same gain within each block; "
        "D versus A changes bandwidth as well as sample rate. Comparisons are available only "
        "at 200 and 1000 µs. There are no matched A controls at 25/50/100 µs in these blocks. "
        "Three interleaved pairs and passing reference brackets reduce some confounding but "
        "do not establish that small differences are statistically meaningful. "
        "[Individual matched pairs](data/paired-controls.csv).",
        "",
    ]
    if dense_rows:
        lines += [
            "| Source | Analyzed | Phase admission (variable budget) | Spatial-model admission |",
            "|---|---:|---:|---:|",
        ]
        for tx in ("TX1", "TX2"):
            subset = [r for r in dense_rows if r["tx_port"] == tx]
            lines.append(
                f"| {tx} | {sum(r['status'] == 'analyzed' for r in subset)}/{len(subset)} | "
                f"{sum(r['phase_10deg_wall_latency_ms'] is not None for r in subset)}"
                f"/{len(subset)} | {sum(r['full_bearing_valid'] for r in subset)}/{len(subset)} |"
            )
        lines += [
            "",
            "All captured records remain in the denominator, including analysis failures. "
            "Missing metrics are not plotted as zeros. Individual failure reasons and raw-record "
            "identities are retained in [dense results](data/dense.csv).",
        ]
    lines += ["", "## Figures", ""]
    for p in sorted(png.glob("*.png")):
        lines += [f"![{p.stem}](png/{p.name})", ""]
    if audit["continuation"]:
        lines += [
            "## Retained interruption and headroom-controlled continuation",
            "",
            "The original 5800 MHz / D block stopped at a 45 s capture-process timeout. "
            "The child retained all 80 frames, but transfer of four seconds of IQ took "
            "23.92 seconds. This is an acquisition/supervision event, not a failed phase "
            "measurement. The incomplete original block and its unindexed completed "
            "capture remain in the parent dataset and are not promoted into a full block.",
            "",
            "The first four completed 915/2475 MHz blocks and their original failed controls "
            "are retained unchanged. Remaining 5800/5811 MHz blocks are newly bracketed "
            "attempts at RX gain 50 dB instead of 60 dB because a previous peak of 1878 "
            "counts exceeded the conservative 1600-count headroom threshold. Dense scans "
            "also use gain 50 dB. TX power is unchanged. The supervisor permits 90 s per "
            "capture process, records stdout/timing, and still treats timeouts as failures.",
            "",
            "Both bandwidths use the same RX gain within each new centre comparison. "
            "However the prior dense 2 MS/s comparison now differs in RX gain as well as "
            "rate, bandwidth and acquisition time. Do not attribute differences to sample "
            "rate alone. Static figures use the fresh gain-50 high-band screen once complete; "
            "the earlier low-band screen remains its original gain and epoch.",
            "",
        ]
        staging = audit["continuation"].get("host_iq_staging")
        if staging:
            lines += [
                "A second interrupted attempt returned `ENODATA` from the metadata refill "
                "after 15 of 80 frames. Cleanup passed. Later muted and disk-backed transport "
                "diagnostics both passed, so the underlying cause is not established.",
                "",
                "The latest continuation stages each IQ record in bounded host RAM "
                "(320 MB for a four-second dual-channel 5 MS/s capture; maximum 512 MiB), "
                "mutes TX, and then persists it to bulk storage. This is host-side buffering, "
                "not a radio DDR-ring or firmware change. Partial received IQ is retained on "
                "failure, and no missing frames are accepted. Storage time is recorded "
                "separately from receive-loop time. Both interrupted blocks remain outside "
                "the qualified trial grid; their hashes and ancestry remain in the plan.",
                "",
            ]
        if audit["continuation"].get("transport_retry_policy"):
            lines += [
                "A later static reference also returned metadata `ENODATA` with RAM staging "
                "after 39/40 frames, so storage stalls are not a sufficient explanation. "
                "That third interrupted block and its partial IQ remain retained.",
                "",
                "The latest continuation declares a bounded transport-recovery policy before "
                "new acquisition: at most three whole-record attempts per scheduled capture, "
                "and only metadata-refill `OSError` errno 61 may trigger another attempt after "
                "verified cleanup. Every attempt has a hash-linked ledger. Frames from separate "
                "attempts are never joined; a selected record must satisfy all original sample "
                "continuity and clipping checks. Timeouts, cleanup errors and phase/gain/bearing "
                "failures are not retried. Phase results are therefore conditional on successful "
                "transport; transport failures must be counted separately, "
                "not hidden as RF passes.",
                "",
            ]
            lines += [
                "| Latest-continuation transport accounting | Count |",
                "|---|---:|",
                f"| Completed capture requests | {transport['complete_requests']} |",
                f"| Recorded attempts | {transport['recorded_attempts']} |",
                f"| Failed attempts retained | {transport['failed_attempts']} |",
                f"| Recovered requests | {transport['recovered_requests']} |",
                f"| Failed requests | {transport['failed_requests']} |",
                f"| Requests still running | {transport['running_requests']} |",
                "",
                "These counts include recovery/headroom screens and static references as well as "
                "switched/dense captures in the latest continuation. They exclude earlier retained "
                "blocks and are not a phase pass rate. "
                "[Full attempt ledger](data/transport-attempts.csv).",
                "",
                "Bounded recapture supports this offline campaign; it does not fix the underlying "
                "metadata-provider failure or qualify uninterrupted live tracking. That failure "
                "still needs investigation before deployment.",
                "",
            ]
    lines += [
        "## Interpretation and limits",
        "",
        "A stable phase estimate or admitted bearing is not surveyed localization accuracy. "
        "The PCB LUT is unchanged; no per-port OTA corrections are fitted. Geometry is the "
        "nominal 51 mm C6 for rolling results. Dense before/after comparisons use the same "
        "historical 49.9654 mm model, with a separate nominal-51-mm analysis retained per run.",
        "",
        "The prior dense comparison changes both sample rate and RX bandwidth and occurs "
        "at a different time. It cannot isolate sampling rate alone. The new B/D centre "
        "comparisons hold sample rate fixed but still require passing brackets to rule out "
        "observed drift. Controls and incomplete analyses remain failures, not omitted trials.",
        "",
        "Static acquisition/headroom admission checks complete samples and clipping, not "
        "direction-finding performance. Figure 4 also shows normalized raw cross-correlation "
        "magnitude |ρ|. Low |ρ| indicates little shared coherent power over the captured "
        "bandwidth; noise, interference and time-varying relative phase can all reduce it. "
        "This metric alone does not identify the cause or invalidate a longer integrated "
        "phase estimate. The independent rolling tests, brackets and spatial gates remain "
        "the qualification criteria.",
        "",
        "The receiver and source are serial-pinned at 192.168.1.15 and 192.168.1.179. "
        f"Raw IQ and linked inherited records are indexed under `{root}`. "
        "Data/run hashes, source hashes, window completeness and restore evidence are audited. "
        "No live runtime or angular-accuracy qualification is claimed.",
        "",
        (
            "Final recorded cleanup verified both radios muted, selector ALL_OFF and exact "
            "original firmware restored."
            if safety
            else "Final campaign cleanup is not yet verified."
        ),
        "",
        "## Reproduce the offline audit and figures",
        "",
        "From the repository root, using the retained captures and completed analysis:",
        "",
        "```bash",
        "env PYTHONPATH=src:scripts LD_LIBRARY_PATH=.venv/lib \\",
        "  .venv/bin/python scripts/audit_full_5ms_campaign.py \\",
        f"  --campaign-root {root}",
        "",
        "env PYTHONPATH=src:scripts LD_LIBRARY_PATH=.venv/lib \\",
        "  .venv/bin/python scripts/report_full_5ms_campaign.py \\",
        f"  --campaign-root {root}",
        "```",
        "",
        "These commands do not control the radios. The acquisition audit refuses a completion "
        "certificate while the planned grid is incomplete; use `--allow-partial` only for an "
        "explicitly partial audit. Analysis success, retained failure reasons, final hardware "
        "restoration and the rendered report must also be checked before claiming full completion.",
        "",
        "[Previous moved-fixture report](../jittered_fixture_comparison/README.md).",
        "",
    ]
    (output / "README.md").write_text("\n".join(lines))
    print(f"report={output} status={audit['status']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=OUTPUT)
    args = parser.parse_args()
    render(args.campaign_root)
