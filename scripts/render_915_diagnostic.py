#!/usr/bin/env python3
"""Render hash-checked static/background evidence and optional switching results."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from smateway.rate_timing import PORTS, complex_value, load, sha256


def verified_run(item):
    path = Path(item["run_json"])
    if sha256(path) != item["sha256"]:
        raise ValueError(f"run hash differs: {path}")
    run = load(path)
    if run["status"] != "passed":
        raise ValueError(f"acquisition failed: {path}")
    configuration = run.get("configuration", {})
    if (
        configuration.get("frequency_hz") != 915_000_000
        or configuration.get("name") != "A"
        or configuration.get("sample_rate_hz") != 2_000_000
        or configuration.get("bandwidth_hz") != 1_600_000
    ):
        raise ValueError(f"run differs from the 915 MHz diagnostic configuration: {path}")
    for raw in run["capture"]["raw"]:
        target = Path(raw["path"])
        if sha256(target) != raw["sha256"] or target.stat().st_size != raw["bytes"]:
            raise ValueError(f"raw IQ hash or size differs: {target}")
    return run


def raw_power(raw):
    values = np.memmap(raw["path"], dtype=np.complex64, mode="r")
    return float(np.mean(np.abs(values) ** 2, dtype=np.float64))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screens", type=Path, nargs="+", required=True)
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data, png = args.output / "data", args.output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    rows, histories, audit = [], {}, []
    for screen_path in args.screens:
        screen = load(screen_path)
        if screen["status"] != "complete":
            raise ValueError("screen must be complete; failed attempts must be reported separately")
        runs = {}
        for item in screen["captures"]:
            run = verified_run(item)
            runs[item["mode"], item["port"]] = run
            audit.append({
                "run_json": item["run_json"], "sha256": item["sha256"],
                "raw_bytes": sum(raw["bytes"] for raw in run["capture"]["raw"]),
                "source_muted": run["safety"]["final_source_mute"]["passed"],
                "selector_all_off": run["selector_after_capture"]["applied_code"] == 8,
            })
        for port in PORTS:
            muted, enabled = runs["ambient", port], runs["static", port]
            ref = enabled["reference"]
            background = raw_power(muted["capture"]["raw"][1])
            gain = screen["gain_db"]
            vector = np.array([complex_value(v) for v in ref["group_transfers"]])
            transfer = complex_value(ref["transfer"])
            histories[gain, port] = np.angle(vector / transfer, deg=True)
            rows.append({
                "port": port, "receiver_gain_db": gain,
                "rx2_background_rms_counts": np.sqrt(background),
                "rx2_source_on_rms_counts": np.sqrt(ref["rx2_power_counts2"]),
                "source_on_over_muted_power_db": 10 * np.log10(
                    ref["rx2_power_counts2"] / background
                ),
                "coherence": ref["coherence"],
                "phase_rms_10ms_deg": ref["phase_rms_10ms_deg"],
                "relative_transfer_magnitude_db": 20 * np.log10(abs(transfer)),
                "phase_vs_rx1_deg": np.angle(transfer, deg=True),
                "peak_rx1_counts": enabled["capture"]["peak_component_counts"][0],
                "peak_rx2_counts": enabled["capture"]["peak_component_counts"][1],
            })
    with (data / "static-summary.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    summary = {"schema": 1, "screens": [
        {"path": str(p.resolve()), "sha256": sha256(p)} for p in args.screens
    ], "static": rows, "audit": audit}
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    gains = sorted({r["receiver_gain_db"] for r in rows})
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    for gain in gains:
        subset = [r for r in rows if r["receiver_gain_db"] == gain]
        label = f"RX gain {gain} dB"
        for ax, key, title in zip(axes.flat, (
            "source_on_over_muted_power_db", "coherence", "phase_rms_10ms_deg",
            "peak_rx1_counts",
        ), (
            "RX2 source-on / source-muted power (dB)", "Wideband RX2/RX1 coherence",
            "Phase RMS per 10 ms average (degrees)", "RX1 peak I/Q component (counts)",
        ), strict=True):
            ax.plot(PORTS, [r[key] for r in subset], "o-", label=label)
            ax.set_title(title)
            ax.grid(alpha=0.25)
    axes[0, 1].axhline(0.9, color="gray", linestyle="--", label="Static screen threshold")
    axes[1, 1].axhline(1600, color="gray", linestyle="--", label="Conservative headroom limit")
    axes[0, 0].legend()
    axes[0, 1].legend()
    axes[1, 1].legend()
    fig.suptitle("915 MHz feasibility — existing antennas, unchanged TX power\n"
                 "Source-on/off contrast is not calibrated antenna gain or narrowband SNR")
    fig.savefig(png / "fig01_static_link_quality.png", dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(3, 2, figsize=(12, 8), layout="constrained", sharex=True, sharey=True)
    for ax, port in zip(axes.flat, PORTS, strict=True):
        for gain in gains:
            residual = histories[gain, port]
            ax.plot((np.arange(len(residual)) + 0.5) * 0.01, residual,
                    label=f"RX gain {gain} dB", alpha=0.8)
        ax.axhline(0, color="black", linewidth=0.6)
        ax.set_title(port)
        ax.set_ylabel("Phase residual (°)")
        ax.grid(alpha=0.25)
    axes[0, 0].legend()
    axes[-1, 0].set_xlabel("Time within each separate static capture (s)")
    axes[-1, 1].set_xlabel("Time within each separate static capture (s)")
    fig.suptitle("915 MHz static phase repeatability\n"
                 "Each trace is relative to its own 2 s mean; not a bearing error")
    fig.savefig(png / "fig02_static_phase_repeatability.png", dpi=160)
    plt.close(fig)
    if args.timing:
        timing = load(args.timing)
        block_path = Path(timing["block"])
        if sha256(block_path) != timing["block_sha256"] or timing["status"] != "analyzed":
            raise ValueError("timing parent hash/status differs")
        block = load(block_path)
        for item in block["references"] + block["captures"]:
            run = verified_run(item)
            audit.append({
                "run_json": item["run_json"], "sha256": item["sha256"],
                "raw_bytes": sum(raw["bytes"] for raw in run["capture"]["raw"]),
                "source_muted": run["safety"]["final_source_mute"]["passed"],
            })
        switched = []
        for row in timing["rows"]:
            rolling = row.get("rolling_past_only", {})
            metrics = rolling.get("metrics", {})
            studies = metrics.get("studies", [])
            base = next((s for s in studies if s["wall_ms"] == 50), {})
            closure = metrics.get("closure", {})
            switched.append({
                "round": row["round"], "control": row["control"],
                "dwell_us": row["configuration"]["dwell_us"],
                "complete": rolling.get("complete", False),
                "phase_rms_50ms_deg": base.get("weighted_phase_rms_deg"),
                "per_port_phase_rms_50ms_deg": base.get("per_port_phase_rms_deg"),
                "per_port_gain_error_db": closure.get("gain_error_db"),
                "max_phase_bias_deg": closure.get("maximum_observable_phase_bias_deg"),
                "max_gain_error_db": closure.get("maximum_observable_gain_error_db"),
                "passed": bool(rolling.get("complete") and base.get("passed")
                               and closure.get("passed")),
            })
        summary.update({"timing": {"path": str(args.timing), "sha256": sha256(args.timing)},
                        "switched": switched, "reference_drift": timing["reference_drift"],
                        "frozen_reference": timing["frozen_reference"]})
        fig, axes = plt.subplots(1, 3, figsize=(14, 4), layout="constrained")
        for row in switched:
            x = row["dwell_us"] * (1 + 0.035 * (row["round"] - 2))
            for ax, key in zip(axes, ("phase_rms_50ms_deg", "max_phase_bias_deg",
                                    "max_gain_error_db"), strict=True):
                if row[key] is not None:
                    ax.scatter(x, row[key], marker="x" if row["control"] else "o",
                               color="#167b69" if row["passed"] else "#cb4a32", s=55)
        for ax, title, threshold in zip(axes, (
            "Weighted phase RMS / 50 ms (°)", "Maximum port phase bias (°)",
            "Maximum port gain error (dB)",
        ), (10, 10, 1), strict=True):
            ax.set_title(title)
            ax.set_xlabel("Port dwell (µs)")
            ax.set_xscale("log")
            ax.set_xticks([200, 1000], labels=["200", "1000"])
            ax.minorticks_off()
            ax.axhline(threshold, color="gray", linestyle="--")
            ax.grid(alpha=0.25)
        observed_rms = [r["phase_rms_50ms_deg"] for r in switched
                        if r["phase_rms_50ms_deg"] is not None]
        axes[0].set_ylim(0, max(0.6, 1.2 * max(observed_rms, default=0)))
        if axes[0].get_ylim()[1] < 10:
            axes[0].text(0.04, 0.94, "Pass limit: 10° (above displayed range)",
                         transform=axes[0].transAxes, fontsize=9)
        fig.suptitle(
            "915 MHz known-emitter switching replay — 3 independent rounds\n"
            "Circles: main runs; crosses: separate 200 µs controls; green: pass; red: fail"
        )
        fig.savefig(png / "fig03_switching_phase_closure.png", dpi=160)
        plt.close(fig)
        fig, axes = plt.subplots(1, 2, figsize=(13, 6), layout="constrained")
        labels = [f"R{r['round']} / {r['dwell_us']} µs"
                  + (" control" if r["control"] else "") for r in switched]
        for ax, key, title in zip(axes, (
            "per_port_phase_rms_50ms_deg", "per_port_gain_error_db",
        ), ("Each port's phase RMS / 50 ms (°)", "Each port's gain closure error (dB)"),
                strict=True):
            matrix = np.array([r[key] if r[key] is not None else [np.nan] * 6
                               for r in switched])
            im = ax.imshow(np.abs(matrix), aspect="auto", cmap="YlOrRd", vmin=0)
            for (y, x), value in np.ndenumerate(matrix):
                ax.text(x, y, f"{value:.2f}" if np.isfinite(value) else "unscored",
                        ha="center", va="center", fontsize=9,
                        bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "none"})
            ax.set_xticks(range(6), labels=PORTS)
            ax.set_yticks(range(len(labels)), labels=labels)
            ax.set_title(title)
            fig.colorbar(im, ax=ax, label="Magnitude; annotations retain sign")
        fig.suptitle("915 MHz per-port switching diagnostics\n"
                     "All six ports retained, including the weakest ANT2")
        fig.savefig(png / "fig04_switching_port_matrix.png", dpi=160)
        plt.close(fig)
    (data / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    print(f"summary={data / 'summary.json'}")
    print(f"verified_runs={len(audit)}, raw_bytes={sum(r['raw_bytes'] for r in audit)}")


if __name__ == "__main__":
    main()
