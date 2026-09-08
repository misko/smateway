#!/usr/bin/env python3
"""Provenance-separated progress figures for replay and fresh bounded diagnostics."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from smateway.campaign_protocol import coverage_grid, read_protocol
from smateway.rate_timing import PORTS, complex_value, load, sha256
from smateway.tracking.bearing import solve_bearing
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def csv_file(path, rows):
    if rows:
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def acquisition_row(path):
    run = load(path)
    cfg, cap = run["configuration"], run.get("capture", {})
    timeline = cap.get("timeline", run.get("partial_timeline", []))
    rejected = run.get("rejected_block", {})
    return {
        "run_json": str(path),
        "sha256": sha256(path),
        "status": run["status"],
        "frequency_mhz": cfg["frequency_hz"] / 1e6,
        "configuration": cfg["name"],
        "mode": cfg["mode"],
        "gain_db": cfg.get("receiver_gain_db", 60),
        "port": cfg["port"],
        "dwell_us": cfg["dwell_us"],
        "accepted_duration_s": len(timeline) * cfg["frame_samples"] / cfg["sample_rate_hz"],
        "missing_samples_before_rejected_block": rejected.get("missing_samples_before", 0),
        "rx1_peak": cap.get("peak_component_counts", [None, None])[0],
        "rx2_peak": cap.get("peak_component_counts", [None, None])[1],
        "error": (run.get("error") or {}).get("message"),
        "final_source_mute": run.get("safety", {}).get("final_source_mute", {}).get("passed"),
    }


def fresh_outputs(campaign_root, blocks, data, png):
    phases, bearings, per_port = [], [], []
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.7))
    for block in blocks:
        frequency = block["frequency_hz"] / 1e6
        ax = axes[0 if frequency < 3000 else 1]
        for row in block["captures"]:
            if "analysis_json" not in row:
                continue
            analysis = load(Path(row["analysis_json"]))
            variant = next(
                (
                    v
                    for v in analysis.get("variants", [])
                    if v["method"] == "native_refined" and v["leading_discard_us"] == 5
                ),
                None,
            )
            metrics = variant["metrics"] if variant else {}
            closure = metrics.get("closure", {})
            phases.append(
                {
                    "frequency_mhz": frequency,
                    "dwell_us": row["dwell_us"],
                    "round": row["round"],
                    "control": row["control"],
                    "phase_and_closure_pass": metrics.get("passed", False),
                    "first_phase_pass_ms": metrics.get("first_phase_pass_ms"),
                    "maximum_observable_phase_bias_deg": closure.get(
                        "maximum_observable_phase_bias_deg"
                    ),
                    "maximum_observable_gain_error_db": closure.get(
                        "maximum_observable_gain_error_db"
                    ),
                    "error": analysis.get("error"),
                    "analysis_json": row["analysis_json"],
                }
            )
            for i, port in enumerate(PORTS):
                per_port.append(
                    {
                        "frequency_mhz": frequency,
                        "dwell_us": row["dwell_us"],
                        "round": row["round"],
                        "control": row["control"],
                        "port": port,
                        "observable": analysis.get("frozen_weights", {}).get(
                            "observable", [None] * 6
                        )[i],
                        "frozen_weight": analysis.get("frozen_weights", {}).get(
                            "weights", [None] * 6
                        )[i],
                        "relative_phase_bias_deg": closure.get(
                            "relative_phase_bias_deg", [None] * 6
                        )[i],
                        "gain_error_db": closure.get("gain_error_db", [None] * 6)[i],
                        "error": analysis.get("error"),
                    }
                )
            if variant and not row["control"]:
                study = metrics["studies"]
                ax.loglog(
                    [s["wall_ms"] for s in study],
                    [s["weighted_phase_rms_deg"] for s in study],
                    "o-",
                    alpha=0.6,
                    label=f"{row['dwell_us']} µs / r{row['round']}",
                )
    for ax, label in zip(axes, ("2.45 GHz", "5.8 GHz"), strict=True):
        ax.set(
            title=label,
            xlabel="Total RF observation (ms)",
            ylabel="Weighted relative phase RMS (°)",
        )
        ax.axhline(10, linestyle=":", color="gray")
        ax.grid(alpha=0.2)
        if ax.lines and ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
    fig.suptitle(
        "Fresh independent-reference phase diagnostics; phase-only threshold is not bearing qualification"
    )
    save(fig, png / "fig05_fresh_phase_integration.png")
    csv_file(data / "fresh-phase-comparison.csv", phases)
    csv_file(data / "fresh-all-port-closure.csv", per_port)
    if per_port:
        fig, axes = plt.subplots(1, 2, figsize=(12, max(4, len(per_port) / 6 * 0.4)))
        labels = [
            f"{r['frequency_mhz']:.0f} / {r['dwell_us']}µs / r{r['round']}"
            + (" control" if r["control"] else "")
            for r in per_port[::6]
        ]
        for ax, key, limit, title in zip(
            axes,
            ("relative_phase_bias_deg", "gain_error_db"),
            (40, 5),
            ("Relative phase bias (°)", "Gain error (dB)"),
            strict=True,
        ):
            matrix = np.array([np.nan if r[key] is None else r[key] for r in per_port]).reshape(
                -1, 6
            )
            art = ax.imshow(matrix, cmap="coolwarm", vmin=-limit, vmax=limit, aspect="auto")
            ax.set_xticks(range(6), PORTS)
            ax.set_yticks(range(len(labels)), labels)
            ax.set_title(title + " — all ports retained")
            for y in range(len(labels)):
                for x in range(6):
                    row = per_port[y * 6 + x]
                    value = matrix[y, x]
                    label = (
                        "—"
                        if not np.isfinite(value)
                        else f"{value:.1f}" + ("*" if row["observable"] is False else "")
                    )
                    ax.text(x, y, label, ha="center", va="center", fontsize=8)
            fig.colorbar(
                art,
                ax=ax,
                shrink=0.8,
                label="Colour scale clipped; annotations retain exact magnitude",
            )
        fig.suptitle(
            "Independent static-reference closure; * = below frozen visibility threshold, not qualified"
        )
        save(fig, png / "fig09_all_port_closure.png")
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex="col")
    full_bearing_reports = []
    for path in sorted(campaign_root.glob("block-*-bearings.json")):
        report = load(path)
        full_bearing_reports.append({"path": str(path), "sha256": sha256(path), "analysis": report})
        for row in report["rows"]:
            if "configuration" not in row or row["control"]:
                continue
            cfg = row["configuration"]
            frequency = cfg["frequency_hz"] / 1e6
            col = 0 if frequency < 3000 else 1
            for method, style in (("retrospective_full", "-"), ("causal_open_loop", "--")):
                studies = (row.get(method) or {}).get("frozen_weight_bearings", [])
                if not studies:
                    continue
                selected = min(studies, key=lambda s: abs(s["rf_window_ms"] - 25))
                bearings.append(
                    {
                        "frequency_mhz": frequency,
                        "dwell_us": cfg["dwell_us"],
                        "round": row["round"],
                        "method": method,
                        **selected,
                    }
                )
                color = "#2563eb" if cfg["dwell_us"] == 200 else "#d97706"
                label = f"{cfg['dwell_us']} µs {method}" if row["round"] == 1 else None
                for ax, key in zip(
                    axes[:, col],
                    ("model_valid_percent", "all_group_repeatability_rms_deg"),
                    strict=True,
                ):
                    ax.semilogx(
                        [s["rf_window_ms"] for s in studies],
                        [s[key] for s in studies],
                        style,
                        color=color,
                        alpha=0.6,
                        label=label,
                    )
    for col, label in enumerate(("2.45 GHz", "5.8 GHz")):
        axes[0, col].set(title=label, ylabel="Model-valid bearings (%)", ylim=(-2, 102))
        axes[1, col].set(xlabel="Total RF observation (ms)", ylabel="All-group circular RMS (°)")
        axes[0, col].axhline(95, color="gray", linestyle=":")
        axes[1, col].axhline(5, color="gray", linestyle=":")
        for ax in axes[:, col]:
            ax.grid(alpha=0.2)
        if axes[0, col].get_legend_handles_labels()[0]:
            axes[0, col].legend(fontsize=7)
    fig.suptitle(
        "Fresh nominal-geometry bearings: independent frozen weights; lines with <30 groups remain diagnostic"
    )
    save(fig, png / "fig06_fresh_bearing_comparison.png")
    csv_file(data / "fresh-bearing-comparison.csv", bearings)
    (data / "fresh-bearing-studies.json").write_text(
        json.dumps({"schema": 1, "reports": full_bearing_reports}, indent=2, allow_nan=False) + "\n"
    )
    return phases, bearings


def ideal_gate_outputs(data, png):
    geometry = ArrayGeometry.circular("user-confirmed-51mm", PORTS, radius_mm=25.5)
    grid = np.arange(0, 360, 0.25)
    rows = []
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for ax, frequency in zip(axes, (2_450_000_000, 5_800_000_000), strict=True):
        steering = far_field_steering(geometry, frequency, grid)
        estimate = solve_bearing(steering[360], steering, grid)
        ax.plot(grid, 10 * np.log10(np.maximum(estimate.likelihood, 1e-12)))
        ax.axvline(90, color="#15803d", linestyle=":", label="True noiseless bearing")
        ax.axvline(
            estimate.second_bearing_deg,
            color="#dc2626",
            linestyle="--",
            label="Current 'second peak'",
        )
        ax.set(
            xlabel="Bearing clockwise from forward (°)",
            ylabel="Normalized likelihood (dB)",
            ylim=(-25, 1),
            title=f"{frequency / 1e9:.2f} GHz: margin {estimate.ambiguity_margin_db:.3f} dB; pass={estimate.valid}",
        )
        ax.legend(fontsize=8)
        ax.grid(alpha=0.2)
        rows.append(
            {
                "frequency_hz": frequency,
                "geometry_radius_mm": 25.5,
                "truth_bearing_deg": 90,
                "estimated_bearing_deg": estimate.bearing_deg,
                "reported_second_bearing_deg": estimate.second_bearing_deg,
                "ambiguity_margin_db": estimate.ambiguity_margin_db,
                "passed": estimate.valid,
            }
        )
    fig.suptitle("Noise-free model check: the legacy 20° exclusion can select a main-lobe shoulder")
    save(fig, png / "fig07_noiseless_ambiguity_gate.png")
    csv_file(data / "noiseless-model-gate.csv", rows)


def ambient_outputs(paths, data, png):
    rows = []
    fig, axes = plt.subplots(2, 1, figsize=(11, 7))
    for path in paths:
        run = load(path)
        if run["status"] != "passed" or run["configuration"]["mode"] != "ambient":
            continue
        cfg, cap = run["configuration"], run["capture"]
        raw = cap["raw"][1]
        if sha256(Path(raw["path"])) != raw["sha256"]:
            raise ValueError("ambient diagnostic raw hash differs")
        signal = np.memmap(raw["path"], dtype=np.complex64, mode="r")
        count = cfg["sample_rate_hz"] // 200
        power = np.mean(abs(signal[: len(signal) // count * count].reshape(-1, count)) ** 2, axis=1)
        log_power = 10 * np.log10(np.maximum(power, 1e-12))
        label = f"{cfg['frame_samples']} samples/block ({cfg['frame_samples'] / cfg['sample_rate_hz'] * 1000:.0f} ms)"
        axes[0].plot(np.arange(len(power)) * 0.005, log_power, label=label, alpha=0.75)
        frequency = np.fft.rfftfreq(len(power), 0.005)
        modulation = abs(np.fft.rfft(log_power - log_power.mean()))
        axes[1].plot(frequency, modulation / max(modulation.max(), 1e-12), label=label)
        k = 1 + np.argmax(modulation[1:])
        rows.append(
            {
                "run_json": str(path),
                "sha256": sha256(path),
                "frame_samples": cfg["frame_samples"],
                "gain_db": cfg["receiver_gain_db"],
                "strongest_power_modulation_hz_not_necessarily_fundamental": float(frequency[k]),
                "peak_component_counts_rx2": cap["peak_component_counts"][1],
                "source_muted": run["source_settings"] == {"mode": "muted"},
                "per_sample_gain_telemetry_available": all(
                    t["gain_telemetry"]["available"] for t in cap["timeline"]
                ),
            }
        )
    axes[0].set(
        xlim=(0, 0.5),
        xlabel="Acquired time within separate run (s)",
        ylabel="RX2 5 ms mean power (dB counts²)",
    )
    axes[1].set(
        xlim=(0, 50),
        xlabel="Power modulation frequency (Hz)",
        ylabel="Normalized modulation spectrum",
    )
    for ax in axes:
        ax.grid(alpha=0.2)
        if rows:
            ax.legend(fontsize=8)
    fig.suptitle(
        "Selected ANT1, both source TXs muted: independently restarted transport-block-size controls"
    )
    save(fig, png / "fig08_source_muted_block_size_control.png")
    csv_file(data / "ambient-block-size-controls.csv", rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/comprehensive_fast_switching")
    args = parser.parse_args()
    data, png = args.output / "data", args.output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    paths = sorted((args.campaign_root / "captures").glob("*/run.json"))
    rows = [acquisition_row(p) for p in paths if load(p)["status"] != "running"]
    csv_file(data / "fresh-acquisitions.csv", rows)
    replay = load(data / "reproduction-with-causal-v1.json")
    comparison = []
    fig, axes = plt.subplots(2, 4, figsize=(16, 7), sharex="col")
    for column, run in enumerate(replay["bearing_reproductions"]):
        cfg = run["configuration"]
        dwell = int(Path(run["run_json"]).parent.name.split("-")[1].removesuffix("us"))
        title = f"{cfg['frequency_hz'] / 1e9:.3f} GHz TX{cfg['tx_channel'] + 1}\n{dwell} µs dwell"
        axes[0, column].set_title(title)
        for label, records, color in (
            ("Whole-record timing", run["retrospective_same_time_holdout"], "#2563eb"),
            (
                "1 s past-only, then open-loop",
                run["causal_open_loop_replay"]["bearing_study"],
                "#d97706",
            ),
        ):
            x = [r["rf_window_ms"] for r in records]
            axes[0, column].semilogx(
                x, [r["model_valid_percent"] for r in records], "o-", color=color, label=label
            )
            axes[1, column].semilogx(
                x, [r["all_group_repeatability_rms_deg"] for r in records], "o-", color=color
            )
            target = next(r for r in records if r["cycles"] == run["stored"]["cycles_averaged"])
            comparison.append(
                {
                    "frequency_mhz": cfg["frequency_hz"] / 1e6,
                    "tx": cfg["tx_channel"] + 1,
                    "dwell_us": dwell,
                    "timing": label,
                    **target,
                }
            )
        axes[0, column].axhline(95, color="gray", linestyle=":")
        axes[0, column].set_ylim(-2, 102)
        axes[1, column].axhline(5, color="gray", linestyle=":")
        axes[1, column].set_xlabel("Held-out RF integration (ms)")
        for ax in axes[:, column]:
            ax.grid(alpha=0.2)
    axes[0, 0].set_ylabel("Model-valid bearings (%)")
    axes[1, 0].set_ylabel("All-group circular RMS (°)")
    axes[0, 0].legend(fontsize=8, loc="lower right")
    fig.suptitle(
        "Historical IQ replay: same unseen final 3 s; neither path demonstrates live latency",
        y=1.02,
    )
    save(fig, png / "fig01_causal_vs_retrospective.png")
    csv_file(data / "historical-causal-comparison.csv", comparison)

    muted = [r for r in rows if r["mode"] == "muted"]
    fig, ax = plt.subplots(figsize=(11, max(4, len(muted) * 0.35)))
    labels = [
        f"{r['frequency_mhz'] / 1000:.2f} GHz / {r['configuration']} / {r['gain_db']} dB / attempt {i + 1}"
        for i, r in enumerate(muted)
    ]
    ax.barh(
        labels,
        [r["accepted_duration_s"] for r in muted],
        color=["#15803d" if r["status"] == "passed" else "#dc2626" for r in muted],
    )
    ax.axvline(30, color="gray", linestyle=":")
    ax.set_xlabel("Counter-continuous accepted RF seconds (failed clipping attempts remain red)")
    ax.set_title("Fresh muted transport/headroom attempts — A: 2, B: 5, C: 10 MS/s")
    ax.invert_yaxis()
    save(fig, png / "fig02_muted_integrity.png")

    static = []
    for p in paths:
        run = load(p)
        if run["status"] != "passed" or "reference" not in run:
            continue
        cfg, ref = run["configuration"], run["reference"]
        static.append(
            {
                "run_json": str(p),
                "frequency_mhz": cfg["frequency_hz"] / 1e6,
                "port": cfg["port"],
                "gain_db": cfg["receiver_gain_db"],
                "amplitude": abs(complex_value(ref["transfer"])),
                "phase_rms_10ms_deg": ref["phase_rms_10ms_deg"],
                "coherence": ref["coherence"],
            }
        )
    csv_file(data / "fresh-static-diagnostics.csv", static)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for frequency, color in ((2450, "#d97706"), (5800, "#2563eb")):
        for index, port in enumerate(PORTS):
            records = [r for r in static if r["frequency_mhz"] == frequency and r["port"] == port]
            offset = -0.12 if frequency == 2450 else 0.12
            for ax, key in zip(axes, ("phase_rms_10ms_deg", "coherence"), strict=True):
                ax.scatter(
                    [index + offset] * len(records),
                    [r[key] for r in records],
                    color=color,
                    alpha=0.65,
                    label=f"{frequency / 1000:.2f} GHz" if index == 0 else None,
                )
    for ax in axes:
        ax.set_xticks(range(6), PORTS)
        ax.grid(alpha=0.2)
        ax.legend()
    axes[0].set_ylabel("Full-band reference phase RMS / 10 ms (°)")
    axes[1].set_ylabel("Full-band RX1–RX2 coherence")
    fig.suptitle(
        "Fresh static captures: headroom acceptance does not establish a clean phase reference"
    )
    save(fig, png / "fig03_static_reference_quality.png")

    evidence = next(
        (p for p in paths if p.parent.name.startswith("stimulus-2450000000-A-g40-ANT1-")), None
    )
    if evidence:
        run = load(evidence)
        vectors = []
        for raw in run["capture"]["raw"]:
            if sha256(Path(raw["path"])) != raw["sha256"]:
                raise ValueError("spectral diagnostic raw hash differs")
            vectors.append(np.memmap(raw["path"], dtype=np.complex64, mode="r"))
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.7))
        n, fs = 16384, run["configuration"]["sample_rate_hz"]
        freq = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs)) / 1000
        for i, signal in enumerate(vectors):
            usable = len(signal) // n * n
            spec = np.fft.fft(signal[:usable].reshape(-1, n) * np.hanning(n), axis=1)
            power = np.fft.fftshift(np.mean(abs(spec) ** 2, axis=0)) / n**2
            axes[0].plot(freq, 10 * np.log10(np.maximum(power, 1e-20)), label=f"RX{i + 1}")
            block = 100_000
            rms = np.mean(abs(signal.reshape(-1, block)) ** 2, axis=1)
            axes[1].plot(
                np.arange(len(rms)) * block / fs, 10 * np.log10(rms), "o-", label=f"RX{i + 1}"
            )
        axes[0].axvline(100, color="gray", linestyle=":", label="Nominal test tone")
        axes[0].set(xlabel="Baseband frequency (kHz)", ylabel="Windowed FFT-bin power (dB counts²)")
        axes[1].set(xlabel="Acquired time (s)", ylabel="50 ms block mean power (dB counts²)")
        for ax in axes:
            ax.legend()
            ax.grid(alpha=0.2)
        fig.suptitle(
            "2.45 GHz / ANT1 / 40 dB: weak test tone versus strong intermittent broadband energy"
        )
        save(fig, png / "fig04_lower_band_spectrum.png")

    fixture = load(data / "fixture-c6-51mm-v2.json")
    grid = coverage_grid(read_protocol(data / "protocol-v1.json"), fixture["approved_intervals_hz"])
    for row in grid:
        row["completed_calibration"] = False
        row["fresh_diagnostic_capture_count"] = sum(
            r["frequency_mhz"] * 1e6 == row["frequency_hz"] for r in rows
        )
    csv_file(data / "frequency-coverage.csv", grid)
    blocks = [
        load(p)
        for p in sorted(args.campaign_root.glob("block-*.json"))
        if not p.name.endswith("-bearings.json")
    ]
    phases, bearings = fresh_outputs(args.campaign_root, blocks, data, png)
    ideal_gate_outputs(data, png)
    ambient = ambient_outputs(paths, data, png)
    summary = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "progress only; full campaign not complete",
        "campaign_root": str(args.campaign_root),
        "completed_attempts": len(rows),
        "failed_attempts": sum(r["status"] == "failed" for r in rows),
        "historical_reproduction_passed": replay["reproduction_passed"],
        "fresh_blocks": blocks,
        "fixture_sha256": sha256(data / "fixture-c6-51mm-v2.json"),
    }
    (data / "progress-summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )
    text = [
        "# Comprehensive fast-switching campaign — progress report",
        "",
        f"Snapshot: {summary['generated_at']}. **Full campaign incomplete.**",
        "",
        "This report keeps historical replay, fresh muted acquisition, source-enabled static diagnostics, "
        "and fresh switching blocks separate. No surveyed-angle accuracy or live delivery latency is claimed.",
        "",
        "## Fixture and scope",
        "",
        "The user confirmed unchanged dual-band wiring, US location, a current 51 mm opposite-antenna-centre "
        "diameter and the recommended clockwise C6 order. Use radius 25.5 mm in both bands. "
        "This is user-confirmed nominal geometry, not a survey or STL-derived model. Transmitter angles are approximate.",
        "",
        "[Protocol](PROTOCOL.md), [readiness](READINESS.md), [1 ms control verification](LONG-CONTROL-VERIFICATION.md). "
        "US candidate limits plus the 1 MHz provisional guard leave 231 of the 252 intent-grid rows eligible; "
        "the other 21 are explicit exclusions. The margin does not certify emissions compliance.",
        "",
        "## Historical reproduction and causality",
        "",
        "All four named historical bearing rows and all three September 8 independent-reference outcomes "
        "were reproduced from hash-checked IQ. That establishes reproducibility, not new validation. "
        "The past-only experiment trains on one second, freezes clock/frequency estimates, then evaluates "
        "the unseen final three seconds. Its failure does not establish that all causal tracking is impossible: "
        "relocking is not implemented and RF-inferred timing is not independent GPIO truth.",
        "",
        "| MHz / TX | Dwell µs | Timing | RF window ms | Valid % | All-group RMS ° |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for row in comparison:
        text.append(
            f"| {row['frequency_mhz']:.0f} / TX{row['tx']} | {row['dwell_us']} | {row['timing']} | "
            f"{row['rf_window_ms']:.3f} | {row['model_valid_percent']:.2f} | {row['all_group_repeatability_rms_deg']:.2f} |"
        )
    text.extend(
        [
            "",
            "![Historical causal comparison](png/fig01_causal_vs_retrospective.png)",
            "",
            "## Fresh acquisition and headroom",
            "",
            f"{len(rows)} completed bounded attempts; {summary['failed_attempts']} retained failures. "
            "Successful counters alone do not imply an unclipped or useful phase measurement. "
            "The 10 MS/s lower-band attempt lost samples; 5.8 GHz/5 MS/s also has a retained dropout. "
            "Do not attribute either solely to network saturation or switch settling.",
            "",
            "![Muted integrity](png/fig02_muted_integrity.png)",
            "",
            "Source-enabled six-port headroom screening selected RX40 dB at 2.45 GHz and RX60 dB at "
            "5.8 GHz after two independent rounds per band. Source gain remained −35 dB and DDS scale 0.25. "
            "Lower-band 50 dB clipped RX2 and remains failed. These headroom captures are not calibration holdouts.",
            "",
            "![Static reference quality](png/fig03_static_reference_quality.png)",
            "",
            "The 2.45 GHz static records show weak reference-correlated tone plus strong intermittent "
            "broadband RX2 energy and poor full-band phase RMS. The periodic block-power pattern warrants "
            "separating RF interference from acquisition artifacts; these data alone do not identify the cause. "
            "At 5.8 GHz ANT1 remains substantially less stable than the other ports. No port is silently removed.",
            "",
            "![Lower-band spectrum](png/fig04_lower_band_spectrum.png)",
            "",
            "## Fresh switching and remaining work",
            "",
            "Each baseline block brackets three independent four-second captures per dwell, plus separate "
            "200 µs controls, with per-port static references. The 1 ms profile changes only the six dwell "
            "words in the existing executable; its bounded watchdog proof was checked before deployment.",
            "",
            "| Frequency MHz | Block status | Captures collected | Exact restores |",
            "|---:|---|---:|---:|",
        ]
    )
    for block in blocks:
        text.append(
            f"| {block['frequency_hz'] / 1e6:.0f} | {block['status']} | {len(block['captures'])} | {len(block['restores'])} |"
        )
    text.extend(
        [
            "",
            "![Fresh phase integration](png/fig05_fresh_phase_integration.png)",
            "",
            "| MHz | Dwell µs | Round | Control | Observable-port phase/closure pass | First phase window ms | Max observable phase bias ° | Max observable gain dB |",
            "|---:|---:|---:|---|---|---:|---:|---:|",
        ]
    )
    for row in phases:

        def number(key, row=row):
            return "—" if row[key] is None else f"{row[key]:.2f}"

        text.append(
            f"| {row['frequency_mhz']:.0f} | {row['dwell_us']} | {row['round']} | {row['control']} | {row['phase_and_closure_pass']} | {number('first_phase_pass_ms')} | {number('maximum_observable_phase_bias_deg')} | {number('maximum_observable_gain_error_db')} |"
        )
    text.extend(
        [
            "",
            "Decoder failures remain failed rows, not discarded trials; see the machine-readable phase table for reasons.",
            "",
            "At 5.8 GHz the independent before-reference places ANT1 below the frozen −20 dB visibility threshold. It still contributes its small frozen weight, but is excluded from the maximum-observable-port gates. Thus the 1 ms result is a five-observable-port diagnostic, not a six-port qualification. ANT1's excess gain error remains visible below; no post-hoc port removal was used.",
            "",
            "![All-port independent closure](png/fig09_all_port_closure.png)",
            "",
            "![Fresh bearing outcomes](png/fig06_fresh_bearing_comparison.png)",
            "",
            "## A model-gate limitation, separate from hardware",
            "",
            "A noiseless equal-weight 51 mm C6 model at 2.45 GHz estimates the correct 90° direction yet fails the existing ambiguity gate: its reported second direction is 110°, on the broad main lobe, with only 0.455 dB margin. The solver selects the largest likelihood at *any* direction at least 20° away, not necessarily a distinct local maximum. At 5.8 GHz the same ideal test gives 2.612 dB and passes. The fixed gate is therefore unsuitable as an unconditional lower-band calibration success test at this aperture.",
            "",
            "![Noiseless gate diagnostic](png/fig07_noiseless_ambiguity_gate.png)",
            "",
            "Frozen historical and fresh decisions are preserved. A revised method must separately report distinct competing lobes, angular uncertainty/main-lobe width, and repeatability, with newly frozen policy and new held-out captures. Do not retroactively convert these failures into passes or infer a PCB fault from the geometry-limited gate.",
            "",
        ]
    )
    text.extend(
        [
            "",
            "The 5.8 GHz baseline block completed. The 2.45 GHz block stopped on its eighth switched attempt after RX2 clipping and was restored; no after-reference bracket or replacement holdout was obtained. Available records are analyzed as incomplete-block diagnostics only. Pending: new lower-band validation after addressing signal/headroom limitations; full dwell/rate screening and frequency "
            "holdouts; dense permitted 1 MHz mapping; independently coherent TX2 settled references; "
            "causal relocking/live delivery checks. The proposed 20 ms TX2 reference profile is not implemented "
            "or admitted. No completed whole-band calibration is claimed.",
            "",
            "Machine-readable evidence: [acquisitions](data/fresh-acquisitions.csv), "
            "[static diagnostics](data/fresh-static-diagnostics.csv), [coverage ledger](data/frequency-coverage.csv), "
            "[progress summary](data/progress-summary.json). Raw IQ remains on bulk storage; no failed attempt was deleted.",
            "",
        ]
    )
    if ambient:
        text.extend(
            [
                "## Source-muted selected-port control",
                "",
                "![Ambient block-size control](png/fig08_source_muted_block_size_control.png)",
                "",
                "The intermittent broadband energy persists with both source TX channels muted and ANT1 selected. "
                "Changing the host transport block size does not establish the cause, but permits a check in acquired-time coordinates. "
                "The modulation-spectrum maximum is not necessarily the fundamental repetition rate. "
                "The current ABI-2 capture interface exposes no per-block tandem gain telemetry, so it is explicitly unavailable rather than claimed stable.",
                "",
                "| Samples/block | Block duration ms | Strongest power-modulation bin Hz | RX2 peak counts | Source muted |",
                "|---:|---:|---:|---:|---|",
            ]
        )
        for row in ambient:
            text.append(
                f"| {row['frame_samples']} | {row['frame_samples'] / 2000:.0f} | {row['strongest_power_modulation_hz_not_necessarily_fundamental']:.1f} | {row['peak_component_counts_rx2']:.0f} | {row['source_muted']} |"
            )
        text.append("")
    (args.output / "README.md").write_text("\n".join(text))
    print(f"report={args.output / 'README.md'}")


if __name__ == "__main__":
    main()
