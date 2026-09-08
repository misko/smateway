#!/usr/bin/env python3
"""Render only the new sample-rate campaign, including rejected acquisitions."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from smateway.rate_timing import (
    CONFIGURATIONS,
    PORTS,
    RECEIVER_SERIAL,
    SOURCE_SERIAL,
    complex_value,
    frozen_reference,
    load,
    sha256,
)

COLORS = {"A": "#2563eb", "B": "#dc7d19", "D": "#188770"}


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def variant(analysis, method="native_refined", discard=5):
    return next(
        (
            v
            for v in analysis.get("variants", [])
            if v["method"] == method and v["leading_discard_us"] == discard
        ),
        None,
    )


def reference_outputs(campaign, data, png):
    refs = campaign["references"]
    baseline = {
        r["port"]: r["reference"]
        for r in refs
        if r["configuration"] == "A" and r["position"] == "before"
    }
    frozen = frozen_reference(baseline) if len(baseline) == 6 else None
    rows = []
    for r in refs:
        ref = r["reference"]
        index = PORTS.index(r["port"])
        rows.append(
            {
                "configuration": r["configuration"],
                "position": r["position"],
                "port": r["port"],
                "transfer_amplitude": abs(complex_value(ref["transfer"])),
                "phase_rms_10ms_deg": ref["phase_rms_10ms_deg"],
                "coherence": ref["coherence"],
                "frozen_weight": frozen["weights"][index] if frozen else None,
                "observable": frozen["observable"][index] if frozen else None,
                "run_json": r["run_json"],
            }
        )
    write_csv(data / "static_references.csv", rows)
    if rows:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
        for name in COLORS:
            for position, style in (("before", "-"), ("after", "--")):
                matched = {
                    r["port"]: r
                    for r in rows
                    if r["configuration"] == name and r["position"] == position
                }
                if len(matched) != 6:
                    continue
                for ax, key in zip(axes, ("transfer_amplitude", "phase_rms_10ms_deg"), strict=True):
                    ax.plot(
                        PORTS,
                        [matched[p][key] for p in PORTS],
                        style,
                        marker="o",
                        color=COLORS[name],
                        label=f"{name} {position}",
                        alpha=0.8,
                    )
                    ax.grid(alpha=0.2)
        axes[0].set(yscale="log", ylabel="|RX2 / RX1 transfer|", title="Settled per-port signal")
        axes[1].set(ylabel="Phase RMS (degrees)", title="Settled 10 ms phase repeatability")
        axes[0].legend(fontsize=8)
        save(fig, png / "fig07_static_references.png")
    drift = campaign.get("reference_drift") or {}
    drift_rows = []
    for name, c in drift.items():
        for i, port in enumerate(PORTS):
            drift_rows.append(
                {
                    "configuration": name,
                    "port": port,
                    "relative_phase_drift_deg": c["relative_phase_bias_deg"][i],
                    "gain_drift_db": c["gain_error_db"][i],
                    "observable": frozen["observable"][i],
                }
            )
    write_csv(data / "reference_drift.csv", drift_rows)
    if drift:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
        for name, c in drift.items():
            axes[0].plot(PORTS, c["relative_phase_bias_deg"], "o-", label=name, color=COLORS[name])
            axes[1].plot(PORTS, c["gain_error_db"], "o-", label=name, color=COLORS[name])
        for ax, limit, label in zip(
            axes, (10, 1), ("Relative phase drift (degrees)", "Gain drift (dB)"), strict=True
        ):
            ax.axhline(limit, color="black", ls="--", lw=1)
            ax.axhline(-limit, color="black", ls="--", lw=1)
            ax.set_ylabel(label)
            ax.grid(alpha=0.2)
            ax.legend()
        fig.suptitle("Independent static references: after / before; one common phase removed")
        save(fig, png / "fig08_reference_drift.png")
    return rows, frozen


def verify_evidence(campaign):
    """Read-only rehash of all admitted raw captures and their ledger bindings."""
    paths = set()
    raw_bytes = 0
    source_contracts = set()
    for row in campaign["references"] + campaign["screen"]:
        path = Path(row["run_json"]).resolve()
        if path in paths:
            raise ValueError("capture reused across campaign conditions")
        paths.add(path)
        run = load(path)
        if "run_sha256" in row and sha256(path) != row["run_sha256"]:
            raise ValueError("run ledger hash differs")
        if "analysis_json" in row:
            analysis_path = Path(row["analysis_json"])
            if sha256(analysis_path) != row["analysis_sha256"]:
                raise ValueError("analysis ledger hash differs")
            analysis = load(analysis_path)
            if Path(analysis["run_json"]).resolve() != path:
                raise ValueError("analysis refers to another capture")
            if "run_sha256" in analysis and analysis["run_sha256"] != sha256(path):
                raise ValueError("analysis run hash differs")
        cfg, capture = run["configuration"], run["capture"]
        identities = run["identities"]
        if (
            run["status"] != "passed"
            or identities["receiver_serial"] != RECEIVER_SERIAL
            or identities["source_serial"] != SOURCE_SERIAL
            or cfg["name"] != row["configuration"]
            or cfg["frequency_hz"] != 5_800_000_000
            or any(capture["clipped_samples"])
            or len(capture["raw"]) != 2
            or capture["samples_per_channel"] != round(cfg["duration_s"] * cfg["sample_rate_hz"])
        ):
            raise ValueError("capture identity, configuration or integrity differs")
        timeline = capture["timeline"]
        if len(timeline) != cfg["frames"]:
            raise ValueError("capture block count differs")
        for i, block in enumerate(timeline):
            if (
                block["buffer_sequence"] != i
                or block["missing_samples_before"]
                or block["overflow_observed"]
                or block["stream_id"] != timeline[0]["stream_id"]
                or block["last_sample_sequence_exclusive"] - block["first_sample_sequence"]
                != cfg["frame_samples"]
                or (
                    i > 0
                    and block["first_sample_sequence"]
                    != timeline[i - 1]["last_sample_sequence_exclusive"]
                )
            ):
                raise ValueError("capture has discontinuous sample counters")
        for item in capture["raw"]:
            raw = Path(item["path"])
            size = raw.stat().st_size
            if size != capture["samples_per_channel"] * 8 or sha256(raw) != item["sha256"]:
                raise ValueError("raw sample length/hash differs")
            raw_bytes += size
        if not run["safety"]["final_source_mute"]["passed"]:
            raise ValueError("capture did not verify its final source mute")
        source_contracts.add(json.dumps(run["source_contract"], sort_keys=True))
    if not paths:
        raise ValueError("no captured evidence to verify")
    return {
        "passed": True,
        "unique_admitted_raw_captures": len(paths),
        "raw_bytes_rehashed": raw_bytes,
        "capture_source_contract_variants": len(source_contracts),
        "scope": "raw hashes, identities, sample continuity, ledger hashes and per-run mute",
    }


def render(root: Path, output: Path, *, verify_raw=False):
    campaign = load(root / "campaign.json")
    png = output / "png"
    data = output / "data"
    png.mkdir(parents=True, exist_ok=True)
    data.mkdir(exist_ok=True)
    integrity = verify_evidence(campaign) if verify_raw else None
    if integrity:
        (data / "integrity.json").write_text(json.dumps(integrity, indent=2) + "\n")
    reference_rows, frozen = reference_outputs(campaign, data, png)
    runs = [(row, load(Path(row["analysis_json"]))) for row in campaign["screen"]]
    regular = [(r, a) for r, a in runs if not r.get("control")]
    rows = []
    for r, a in runs:
        for v in a.get("variants", []):
            m = v["metrics"]
            c = m["closure"]
            rows.append(
                {
                    "configuration": r["configuration"],
                    "dwell_us": r["dwell_us"],
                    "round": r["round"],
                    "control": r["control"],
                    "method": v["method"],
                    "leading_discard_us": v["leading_discard_us"],
                    "sample_count": v["median_samples"],
                    "phase_pass_ms": m["first_phase_pass_ms"],
                    "weighted_phase_bias_deg": c["weighted_phase_bias_deg"],
                    "maximum_observable_phase_bias_deg": c["maximum_observable_phase_bias_deg"],
                    "maximum_gain_error_db": c["maximum_observable_gain_error_db"],
                    "passed": m["passed"],
                    "run_json": r["run_json"],
                }
            )
    write_csv(data / "screen_variants.csv", rows)
    integration_rows = []
    for r, a in regular:
        v = variant(a)
        if v is None:
            continue
        fs = CONFIGURATIONS[r["configuration"]].sample_rate_hz
        for s in v["metrics"]["studies"]:
            integration_rows.append(
                {
                    "configuration": r["configuration"],
                    "dwell_us": r["dwell_us"],
                    "round": r["round"],
                    "integration_ms": s["wall_ms"],
                    "cycles": s["cycles"],
                    "groups": s["groups"],
                    "active_per_port_ms": s["cycles"] * v["median_samples"] / fs * 1000,
                    "weighted_phase_rms_deg": s["weighted_phase_rms_deg"],
                    "passed": s["passed"],
                    "run_json": r["run_json"],
                }
            )
    write_csv(data / "integration.csv", integration_rows)
    if integration_rows:
        fig, axes = plt.subplots(1, 3, figsize=(14, 4.8), sharey=True)
        for ax, name in zip(axes, COLORS, strict=True):
            for dwell, color in zip(
                (25, 50, 100, 200), plt.cm.viridis(np.linspace(0, 0.85, 4)), strict=True
            ):
                labeled = False
                for repeat in (1, 2, 3):
                    selected = [
                        s
                        for s in integration_rows
                        if s["configuration"] == name
                        and s["dwell_us"] == dwell
                        and s["round"] == repeat
                        and s["groups"] >= 8
                    ]
                    if selected:
                        ax.plot(
                            [s["active_per_port_ms"] for s in selected],
                            [s["weighted_phase_rms_deg"] for s in selected],
                            color=color,
                            alpha=0.65,
                            label=f"{dwell} µs" if not labeled else None,
                        )
                        labeled = True
            ax.axhline(10, color="black", ls="--", lw=1)
            ax.set(xscale="log", xlabel="Active integration per port (ms)", title=name)
            ax.grid(alpha=0.2)
            if ax.get_legend_handles_labels()[0]:
                ax.legend()
        axes[0].set_ylabel("Weighted phase RMS (degrees)")
        fig.suptitle("Matched active time · common 5 µs trim; separate from closure qualification")
        save(fig, png / "fig09_active_integration.png")
    analysis_failures = [
        {
            "configuration": r["configuration"],
            "dwell_us": r["dwell_us"],
            "round": r["round"],
            "control": r["control"],
            "error": a.get("error"),
            "run_json": r["run_json"],
        }
        for r, a in runs
        if a.get("status") == "analysis-failed"
    ]
    write_csv(data / "analysis_failures.csv", analysis_failures)
    preflight = []
    for path in sorted((root / "captures").glob("preflight-*/run.json")):
        r = load(path)
        c = r.get("capture", {})
        cfg = r["configuration"]
        timeline = c.get("timeline", r.get("partial_timeline", []))
        wall = c.get("wall_time_s", timeline[-1]["arrival_elapsed_s"] if timeline else 0)
        count = c.get("samples_per_channel", len(timeline) * cfg["frame_samples"])
        preflight.append(
            {
                "run_id": r["run_id"],
                "configuration": cfg["name"],
                "status": r["status"],
                "sample_rate_msps": cfg["sample_rate_hz"] / 1e6,
                "frame_samples": cfg["frame_samples"],
                "accepted_samples": count,
                "accepted_rf_seconds": count / cfg["sample_rate_hz"],
                "wall_seconds": wall,
                "effective_msps": count / wall / 1e6 if wall else 0,
                "error": str(r.get("error")),
                "run_json": str(path),
            }
        )
    write_csv(data / "preflight.csv", preflight)
    if regular:
        fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharey=True)
        for ax, dwell in zip(axes.flat, (25, 50, 100, 200), strict=True):
            labeled = set()
            for row, a in regular:
                if row["dwell_us"] != dwell:
                    continue
                v = variant(a)
                if v is None:
                    continue
                study = [s for s in v["metrics"]["studies"] if s["groups"] >= 8]
                name = row["configuration"]
                ax.plot(
                    [s["wall_ms"] for s in study],
                    [s["weighted_phase_rms_deg"] for s in study],
                    color=COLORS[name],
                    alpha=0.6,
                    marker=".",
                    label=name if name not in labeled else None,
                )
                labeled.add(name)
            ax.axhline(10, color="#303030", ls="--", lw=1)
            ax.set_xscale("log")
            ax.set_title(f"{dwell} µs dwell")
            ax.set_xlabel("Measured integration (ms)")
            ax.set_ylabel("Weighted phase RMS (degrees)")
            ax.grid(alpha=0.2)
            if labeled:
                ax.legend(title="RX configuration")
        fig.suptitle("Phase repeatability · native refinement, 5 µs leading/trailing trim", y=1.02)
        save(fig, png / "fig01_phase_integration.png")

        matrix = np.full((5, 4), np.nan)
        totals = np.zeros((5, 4), dtype=int)
        for i, name in enumerate(CONFIGURATIONS):
            for j, dwell in enumerate((25, 50, 100, 200)):
                matched = [
                    variant(a)
                    for r, a in regular
                    if r["configuration"] == name and r["dwell_us"] == dwell
                ]
                vs = [v for v in matched if v]
                if matched:
                    matrix[i, j] = sum(v["metrics"]["passed"] for v in vs)
                    totals[i, j] = len(matched)
        fig, ax = plt.subplots(figsize=(8, 4.5))
        im = ax.imshow(matrix, vmin=0, vmax=3, cmap="YlGn")
        fig.colorbar(im, ax=ax, label="Passing captures")
        ax.set_xticks(range(4), ["25 µs", "50 µs", "100 µs", "200 µs"])
        ax.set_yticks(
            range(5),
            [
                f"{n}: {c.sample_rate_hz / 1e6:g} MS/s, {c.bandwidth_hz / 1e6:g} MHz"
                for n, c in CONFIGURATIONS.items()
            ],
        )
        for i in range(5):
            for j in range(4):
                label = (
                    "unavailable"
                    if np.isnan(matrix[i, j])
                    else f"{int(matrix[i, j])}/{totals[i, j]}"
                )
                ax.text(j, i, label, ha="center", va="center", fontsize=9)
        ax.set_title("Repeatability + independent closure · common 5 µs recipe")
        save(fig, png / "fig02_quality_matrix.png")

        labels = []
        bias = []
        gain = []
        for name in COLORS:
            for dwell in (25, 50, 100, 200):
                vs = [
                    variant(a)
                    for r, a in regular
                    if r["configuration"] == name and r["dwell_us"] == dwell
                ]
                vs = [v for v in vs if v]
                if not vs:
                    continue
                labels.append(f"{name} / {dwell} µs")
                bias.append(
                    np.median(
                        [v["metrics"]["closure"]["relative_phase_bias_deg"] for v in vs], axis=0
                    )
                )
                gain.append(
                    np.median([v["metrics"]["closure"]["gain_error_db"] for v in vs], axis=0)
                )
        fig, axes = plt.subplots(1, 2, figsize=(12, 7))
        for ax, values, limit, title in zip(
            axes,
            (bias, gain),
            (30, 3),
            ("Independent phase bias (degrees)", "Independent gain error (dB)"),
            strict=True,
        ):
            im = ax.imshow(values, aspect="auto", cmap="RdBu_r", vmin=-limit, vmax=limit)
            ax.set_xticks(range(6), PORTS, rotation=45)
            ax.set_yticks(range(len(labels)), labels)
            ax.set_title(title)
            fig.colorbar(im, ax=ax)
        save(fig, png / "fig03_port_closure.png")

        fig, axes = plt.subplots(2, 2, figsize=(12, 8))
        for column, dwell in enumerate((25, 200)):
            for name in COLORS:
                selected = [
                    a
                    for r, a in regular
                    if r["configuration"] == name and r["dwell_us"] == dwell and a.get("settling")
                ]
                if not selected:
                    continue
                trace = selected[-1]["settling"]
                for ax, key in (
                    (axes[0, column], "weighted_phase_bias_deg"),
                    (axes[1, column], "maximum_observable_gain_error_db"),
                ):
                    ax.plot(
                        [s["window_end_us"] for s in trace],
                        [s[key] for s in trace],
                        label=name,
                        color=COLORS[name],
                    )
                    ax.set_xlabel("5 µs window end after RF-inferred selection (µs)")
                    ax.grid(alpha=0.2)
            axes[0, column].axhline(5, color="black", ls="--")
            axes[1, column].axhline(1, color="black", ls="--")
            axes[0, column].set_title(f"{dwell} µs dwell · latest repeat")
            axes[0, column].set_ylabel("Weighted phase bias (degrees)")
            axes[1, column].set_ylabel("Max observable gain error (dB)")
            if axes[0, column].get_legend_handles_labels()[0]:
                axes[0, column].legend()
        save(fig, png / "fig04_settling.png")

        fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
        for name in COLORS:
            selected = [
                (r, a) for r, a in regular if r["configuration"] == name and a.get("refinement")
            ]
            axes[0].scatter(
                [r["dwell_us"] for r, a in selected],
                [a["refinement"]["offset_us"] for r, a in selected],
                label=name,
                color=COLORS[name],
                alpha=0.7,
            )
            for _r, a in selected:
                old, new = variant(a, "legacy"), variant(a)
                if old and new:
                    axes[1].scatter(
                        old["metrics"]["closure"]["weighted_phase_bias_deg"],
                        new["metrics"]["closure"]["weighted_phase_bias_deg"],
                        color=COLORS[name],
                        alpha=0.7,
                    )
        axes[0].set(xlabel="Dwell (µs)", ylabel="Local RF timing correction (µs)")
        axes[0].legend()
        axes[1].set(
            xlabel="Legacy independent phase bias (degrees)",
            ylabel="Refined independent phase bias (degrees)",
        )
        lim = max(axes[1].get_xlim()[1], axes[1].get_ylim()[1])
        axes[1].plot([0, lim], [0, lim], "k--", lw=1)
        save(fig, png / "fig05_decoder_comparison.png")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    labels = [f"{r['configuration']}\n{r['frame_samples'] // 1000}k" for r in preflight]
    axes[0].bar(
        range(len(preflight)),
        [r["effective_msps"] for r in preflight],
        color=["#188770" if r["status"] == "passed" else "#b44747" for r in preflight],
    )
    axes[0].set_xticks(range(len(preflight)), labels)
    axes[0].set_ylabel("Accepted samples / elapsed time (MS/s)")
    axes[0].set_title("Muted acquisition: observed drain rate")
    for r in preflight:
        record = load(Path(r["run_json"]))
        c = record.get("capture", {})
        timeline = c.get("timeline", record.get("partial_timeline", []))
        if not timeline:
            continue
        axes[1].plot(
            [t["arrival_elapsed_s"] for t in timeline],
            (np.arange(len(timeline)) + 1) * r["frame_samples"] / (r["sample_rate_msps"] * 1e6),
            label=f"{r['configuration']} {r['frame_samples'] // 1000}k {r['status']}",
        )
    axes[1].plot([0, 5], [0, 5], "k--", lw=1)
    axes[1].set(
        xlim=(0, 5),
        ylim=(0, 5),
        xlabel="Wall time (s)",
        ylabel="Accepted RF time (s)",
        title="First 5 seconds; failing runs end early",
    )
    if axes[1].get_legend_handles_labels()[0]:
        axes[1].legend(fontsize=7)
    save(fig, png / "fig06_acquisition.png")

    summary = {
        "campaign_id": campaign["campaign_id"],
        "status": campaign["status"],
        "campaign_path": str(root / "campaign.json"),
        "campaign_sha256": sha256(root / "campaign.json"),
        "switching_runs": len(regular),
        "controls": len(runs) - len(regular),
        "reference_count": len(campaign["references"]),
        "analysis_failures": analysis_failures,
        "selection": campaign["selection"],
        "validation": campaign.get("screen_validation"),
        "reference_drift": campaign.get("reference_drift"),
        "unavailable": campaign.get("unavailable_configurations"),
        "restores": campaign["restores"],
        "final_mutes": campaign.get("final_mutes"),
        "frozen_reference": frozen,
        "integrity": integrity,
        "preflight": preflight,
    }
    (data / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    admitted = {r["configuration"] for r in reference_rows}
    unavailable = campaign.get("unavailable_configurations") or {}
    validation_status = (summary["validation"] or {}).get("passed", "pending")
    lines = [
        "# Higher sample rate and shorter dwell campaign",
        "",
        f"Campaign: `{campaign['campaign_id']}`. Status: **{campaign['status']}**.",
        "",
        f"Recorded {len(regular)} switching runs, {len(runs) - len(regular)} controls and "
        f"{len(campaign['references'])} independent static references.",
        "",
        "[Engineering findings, operating decision and next experiment](FINDINGS.md).",
        "",
        "Only this campaign supplies the phase results below. Earlier campaigns and the retired "
        "v1 pilot are not pooled into its training, references or validation.",
        "",
        "## Receiver configurations and acquisition",
        "",
        "| ID | Requested rate MS/s | Requested RX bandwidth MHz | Availability |",
        "| :--- | ---: | ---: | :--- |",
        *[
            f"| {n} | {c.sample_rate_hz / 1e6:g} | {c.bandwidth_hz / 1e6:g} | "
            f"{'unqualified' if n in unavailable else 'admitted' if n in admitted else 'pending'} |"
            for n, c in CONFIGURATIONS.items()
        ],
        "",
        "C and E share the 10 MS/s transport gate; a C failure excludes E without pretending "
        "that E received a separate RF test. Blocks are capped at 250k samples per channel: "
        "50 ms at 5 MS/s, 25 ms at 10 MS/s. Rate acceptance is not continuity qualification.",
        "",
        "| Configuration | Result | RF seconds accepted | Wall seconds | Drain MS/s |",
        "| :--- | :--- | ---: | ---: | ---: |",
        *[
            f"| {r['configuration']} | {r['status']} | {r['accepted_rf_seconds']:.3f} "
            f"| {r['wall_seconds']:.3f} | {r['effective_msps']:.3f} |"
            for r in preflight
        ],
        "",
        "![Acquisition](png/fig06_acquisition.png)",
        "",
        "## Switching comparison",
        "",
        "| Configuration | Dwell µs | Round | Method | Discard µs | Phase latency ms "
        "| Phase bias ° | Gain error dB | Pass |",
        "| :--- | ---: | ---: | :--- | ---: | ---: | ---: | ---: | :--- |",
    ]
    for r in rows:
        if r["control"] or r["method"] != "native_refined" or r["leading_discard_us"] != 5:
            continue
        latency = "—" if r["phase_pass_ms"] is None else f"{r['phase_pass_ms']:.2f}"
        lines.append(
            f"| {r['configuration']} | {r['dwell_us']} | {r['round']} | refined | 5 | {latency} | "
            f"{r['weighted_phase_bias_deg']:.2f} | {r['maximum_gain_error_db']:.2f} "
            f"| {r['passed']} |"
        )
    lines.extend(
        [
            "",
            "This table fixes the same 5 µs leading/trailing trim for comparison. "
            "The frozen training-selected recipe and independent validation are recorded below. "
            "All discard/method variants remain in `data/screen_variants.csv`.",
            "",
            f"{len(analysis_failures)} captures passed acquisition integrity but failed schedule "
            "decoding. They are absent from the numeric table above, counted as failures in "
            "the quality matrix, and retained in "
            "[analysis_failures.csv](data/analysis_failures.csv).",
            "",
            "| Configuration | Dwell µs | Round | Decoder error |",
            "| :--- | ---: | ---: | :--- |",
            *[
                f"| {r['configuration']} | {r['dwell_us']} | {r['round']} | {r['error']} |"
                for r in analysis_failures
            ],
            "",
            "## Frozen selection and validation",
            "",
            f"Training selection: `{json.dumps(summary['selection'], sort_keys=True)}`.",
            "",
            f"Independent screen validation: `{validation_status}`. "
            "Full metrics and reference-drift decisions are retained in "
            "[summary.json](data/summary.json).",
            "",
        ]
    )
    lines.extend(
        [
            "| Holdout round | Phase latency ms | Weighted bias ° "
            "| Max port bias ° | Max gain dB | Pass |",
            "| ---: | ---: | ---: | ---: | ---: | :--- |",
        ]
    )
    for v in (summary["validation"] or {}).get("validation", []):
        m = v.get("metrics") or {}
        c = m.get("closure") or {}
        lines.append(
            f"| {v['round']} | {m.get('first_phase_pass_ms')} "
            f"| {c.get('weighted_phase_bias_deg')} | {c.get('maximum_observable_phase_bias_deg')} "
            f"| {c.get('maximum_observable_gain_error_db')} | {v['passed']} |"
        )
    lines.extend(["", "## Fresh static references and drift", ""])
    if frozen:
        lines.extend(
            [
                "| Port | Frozen weight | Observable | A before 10 ms phase RMS ° |",
                "| :--- | ---: | :--- | ---: |",
                *[
                    f"| {r['port']} | {r['frozen_weight']:.5f} | {r['observable']} "
                    f"| {r['phase_rms_10ms_deg']:.2f} |"
                    for r in reference_rows
                    if r["configuration"] == "A" and r["position"] == "before"
                ],
                "",
            ]
        )
    lines.extend(
        [
            "| Configuration | Weighted phase drift ° | Max port drift ° "
            "| Max gain drift dB | Pass |",
            "| :--- | ---: | ---: | ---: | :--- |",
            *[
                f"| {n} | {c['weighted_phase_bias_deg']:.2f} "
                f"| {c['maximum_observable_phase_bias_deg']:.2f} "
                f"| {c['maximum_observable_gain_error_db']:.2f} | {c['passed']} |"
                for n, c in (summary["reference_drift"] or {}).items()
            ],
            "",
            "## Bracketing controls",
            "",
            "Same A/200 µs configuration, common refined 5 µs recipe. Rounds 11–13 denote "
            "the end of screening rounds 1–3; rounds 1–3 are their starts.",
            "",
            "| Control label | Phase latency ms | Weighted bias ° "
            "| Max port bias ° | Max gain dB | Pass |",
            "| ---: | ---: | ---: | ---: | ---: | :--- |",
            *[
                f"| {r['round']} | {r['phase_pass_ms']} | {r['weighted_phase_bias_deg']:.2f} "
                f"| {r['maximum_observable_phase_bias_deg']:.2f} "
                f"| {r['maximum_gain_error_db']:.2f} "
                f"| {r['passed']} |"
                for r in rows
                if r["control"] and r["method"] == "native_refined" and r["leading_discard_us"] == 5
            ],
            "",
        ]
    )
    for filename, title in (
        ("fig01_phase_integration.png", "Repeatability versus integration"),
        ("fig02_quality_matrix.png", "Quality matrix"),
        ("fig03_port_closure.png", "Independent per-port closure"),
        ("fig04_settling.png", "RF-visible settling"),
        ("fig05_decoder_comparison.png", "Same-IQ decoder comparison"),
        ("fig07_static_references.png", "Static per-port signal and repeatability"),
        ("fig08_reference_drift.png", "Before/after fixture stability"),
        ("fig09_active_integration.png", "Repeatability versus active per-port integration"),
    ):
        if (png / filename).exists():
            lines.extend([f"## {title}", "", f"![{title}](png/{filename})", ""])
    lines.extend(
        [
            "## Interpretation and limits",
            "",
            "Phase repeatability uses frozen power weights from the independent A reference. "
            "Closure compares with a separate settled vector for the same receiver configuration; "
            "one common phase rotation is removed for spatial closure, with no per-port refitting. "
            "The target is ≤10° repeatability, ≤5° weighted bias, ≤10° observable-port bias, "
            "and ≤1 dB observable-port gain error.",
            "",
            "Analysis is retrospective and RF-inferred. The time-to-quality values exclude "
            "initial synchronization, transfer and processing latency; they are not measured "
            "live-tracker delivery times. No synchronized GPIO marker was acquired. "
            "This dataset does not establish surveyed OTA bearing accuracy.",
            "",
            "Dense frequency validation is gated on an independently validated improvement. "
            "Absent measurements must not be inferred from a failed gate.",
            "",
            "Native refinement is a local ±5 µs plateau-consistency search, not a GPIO timestamp. "
            "A result at its search boundary is a diagnostic warning. The 5 µs settling windows "
            "overlap and are not independent trials. 'Phase latency' is the first tested "
            "integration budget meeting RMS, not a precisely estimated minimum.",
            "",
            f"Raw IQ and run hashes remain under `{root}`. The experimental procedure is in "
            "[the plan](../higher_sample_rate_timing_plan/README.md).",
            "",
        ]
    )
    (output / "README.md").write_text("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--verify-raw", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("docs/higher_sample_rate_timing_campaign")
    )
    args = parser.parse_args()
    render(args.campaign_root, args.output, verify_raw=args.verify_raw)
