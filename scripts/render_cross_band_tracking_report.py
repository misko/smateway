#!/usr/bin/env python3
"""Offline synthesis of versioned reports; never opens radios or raw IQ.

Default renders must match the frozen input lock. --snapshot-inputs explicitly
starts a new source snapshot; it is not an automatic repair of a hash mismatch.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "docs/cross_band_tracking_analysis"
COMPLETED = "docs/completed_switching_analysis"
SUBGHZ = "docs/subghz_915_diagnostic"
COMPREHENSIVE = "docs/comprehensive_fast_switching"
DENSE = "docs/fast_tracking_timing_campaign"
PCB = "docs/pcb_direct_injection_calibration"
INPUTS = (
    f"{COMPLETED}/data/rolling.csv",
    f"{COMPLETED}/data/conditions.csv",
    f"{COMPLETED}/data/fixed.csv",
    f"{COMPLETED}/data/blocks.csv",
    f"{COMPLETED}/README.md",
    f"{SUBGHZ}/data/summary.json",
    f"{SUBGHZ}/README.md",
    f"{COMPREHENSIVE}/data/fresh-bearing-comparison.csv",
    f"{COMPREHENSIVE}/data/historical-causal-comparison.csv",
    f"{COMPREHENSIVE}/data/noiseless-model-gate.csv",
    f"{COMPREHENSIVE}/REFERENCE-TIMING-VALIDATION-v1.md",
    f"{COMPREHENSIVE}/README.md",
    f"{DENSE}/data/dense_frequency_conditions.csv",
    f"{DENSE}/data/results.json",
    "docs/tracking_verification_campaign/data/campaign-summary.json",
    "docs/higher_sample_rate_timing_campaign/FINDINGS.md",
    f"{PCB}/README.md",
    f"{PCB}/png/fig10_delay_models_and_residuals.png",
)
DWELLS = (25, 50, 100, 200, 1000)
COLORS = ("#237b86", "#3266b0", "#bc602f")
PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def read_csv(path: str) -> list[dict]:
    with (ROOT / path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def truth(value) -> bool:
    """CSV booleans must not use bool('False'). Blank means unavailable."""
    if value is True or value == "True":
        return True
    if value is False or value in ("False", "", None):
        return False
    raise ValueError(f"Unrecognized boolean: {value!r}")


def fixed_pass(row: dict) -> bool:
    return truth(row["any_window_pass"]) and truth(row["passed"]) and not row["error"]


def clean_condition(main: list[dict], controls: list[dict], bracket: bool,
                    *, expected_controls: int = 3) -> bool:
    return (
        len(main) == 3
        and {int(row["round"]) for row in main} == {1, 2, 3}
        and all(row["passed"] for row in main)
        and expected_controls in (3, 6)
        and len(controls) == expected_controls
        and Counter(int(row["round"]) for row in controls)
        == {round_: expected_controls // 3 for round_ in (1, 2, 3)}
        and all(row["passed"] for row in controls)
        and bracket
    )


def schedule(dwell_us: float, *, ports: int = 6, guard_us: float = 20,
             marker_us: float = 180, trim_us: float = 5) -> dict:
    if dwell_us <= 0 or ports <= 0 or min(guard_us, marker_us, trim_us) < 0:
        raise ValueError("Invalid schedule duration or port count")
    cycle = ports * (dwell_us + guard_us) + marker_us
    retained = max(dwell_us - 2 * trim_us, 0)
    return {
        "dwell_us": dwell_us,
        "cycle_us": cycle,
        "revisits_per_s": 1e6 / cycle,
        "retained_us_per_visit": retained,
        "per_port_duty_percent": 100 * retained / cycle,
        "nominal_retained_ms_per_port_in_50ms": 50 * retained / cycle,
        "all_ports_retained_percent": 100 * ports * retained / cycle,
        "trim_percent": 100 * ports * (dwell_us - retained) / cycle,
        "guard_percent": 100 * ports * guard_us / cycle,
        "marker_percent": 100 * marker_us / cycle,
        "samples_per_retained_visit_2msps": 2 * retained,
        "samples_per_retained_visit_5msps": 5 * retained,
        "samples_per_retained_visit_10msps": 10 * retained,
    }


def verify_inputs(lock: dict, root: Path = ROOT) -> None:
    if lock["schema"] != 1 or set(lock["inputs"]) != set(INPUTS):
        raise ValueError("Source lock schema or inventory mismatch")
    for name, expected in lock["inputs"].items():
        if digest(root / name) != expected["sha256"]:
            raise ValueError(f"Input SHA-256 mismatch: {name}")
        if (root / name).stat().st_size != expected["bytes"]:
            raise ValueError(f"Input length mismatch: {name}")


def collect() -> tuple[list[dict], list[dict]]:
    """Keep campaign, method, block, masks and controls separate."""
    trials, conditions = [], []
    blocks = {row["id"]: row for row in read_csv(f"{COMPLETED}/data/blocks.csv")}
    rolling = read_csv(f"{COMPLETED}/data/rolling.csv")
    for row in rolling:
        trials.append({
            "block": row["block"], "date": "2026-09-08", "method": "rolling_50ms",
            "frequency_mhz": float(row["frequency_hz"]) / 1e6,
            "configuration": row["configuration"], "dwell_us": int(row["dwell_us"]),
            "round": int(row["round"]), "control": truth(row["control"]),
            "passed": truth(row["base_pass"]) and truth(row["complete"]),
            "phase_rms_deg": float(row["base_rms_deg"]),
            "max_gain_error_db": float(row["max_gain_error_db"]),
            "observable_ports": row["observable_ports"],
            "source": f"{COMPLETED}/data/rolling.csv", "source_run": row["run_json"],
        })
    for row in read_csv(f"{COMPLETED}/data/conditions.csv"):
        main = [r for r in trials if r["block"] == row["block"] and not r["control"]
                and r["dwell_us"] == int(row["dwell_us"])]
        controls = [r for r in trials if r["block"] == row["block"] and r["control"]]
        computed = clean_condition(main, controls, truth(row["bracket_pass"]),
                                   expected_controls=3 if row["configuration"] == "A" else 6)
        if (sum(r["passed"] for r in main) != int(row["main_passes"])
                or len(controls) != int(row["control_attempts"])
                or computed != truth(row["clean_condition_pass"])):
            raise ValueError("Rolling summary and trial records disagree")
        conditions.append(condition_row(main, controls, truth(row["bracket_pass"]), computed))

    sub = json.loads((ROOT / f"{SUBGHZ}/data/summary.json").read_text())
    subtrials = []
    for row in sub["switched"]:
        subtrials.append({
            "block": "block-20260909T170658793466Z", "date": "2026-09-09",
            "method": "rolling_50ms", "frequency_mhz": 915.0, "configuration": "A",
            "dwell_us": row["dwell_us"], "round": row["round"], "control": row["control"],
            "passed": truth(row["passed"]) and truth(row["complete"]),
            "phase_rms_deg": row["phase_rms_50ms_deg"],
            "max_gain_error_db": row["max_gain_error_db"],
            "observable_ports": " ".join(p for p, visible in zip(
                sub["frozen_reference"]["ports"], sub["frozen_reference"]["observable"],
                strict=True) if visible),
            "source": f"{SUBGHZ}/data/summary.json", "source_run": "",
        })
    trials.extend(subtrials)
    controls = [r for r in subtrials if r["control"]]
    if len(controls) != 3 or {r["round"] for r in controls} != {1, 2, 3}:
        raise ValueError("915 MHz controls are incomplete")
    bracket = truth(sub["reference_drift"]["A"]["available"]) and truth(
        sub["reference_drift"]["A"]["passed"])
    for dwell in (200, 1000):
        main = [r for r in subtrials if not r["control"] and r["dwell_us"] == dwell]
        conditions.append(condition_row(main, controls, bracket,
                                        clean_condition(main, controls, bracket)))

    # Only the four 5.8 GHz fixed-policy blocks: no mixing with rolling reanalyses.
    fixed = read_csv(f"{COMPLETED}/data/fixed.csv")
    for block, meta in blocks.items():
        if float(meta["frequency_hz"]) < 5e9:
            continue
        selected = []
        for row in fixed:
            if row["block"] != block:
                continue
            selected.append({
                "block": block, "date": "2026-09-08", "method": "fixed_any_budget",
                "frequency_mhz": float(meta["frequency_hz"]) / 1e6,
                "configuration": row["configuration"], "dwell_us": int(row["dwell_us"]),
                "round": int(row["round"]), "control": truth(row["control"]),
                "passed": fixed_pass(row), "phase_rms_deg": None,
                "max_gain_error_db": (float(row["maximum_observable_gain_error_db"])
                                      if row["maximum_observable_gain_error_db"] else None),
                "observable_ports": meta["observable_ports"],
                "source": f"{COMPLETED}/data/fixed.csv", "source_run": row["run_json"],
            })
        trials.extend(selected)
        controls = [r for r in selected if r["control"]]
        for dwell in sorted({r["dwell_us"] for r in selected if not r["control"]}):
            main = [r for r in selected if not r["control"] and r["dwell_us"] == dwell]
            bracket = truth(meta["bracket_pass"])
            # Controls fail in every selected block. Do not promote any main-only pass.
            conditions.append(condition_row(main, controls, bracket,
                                            clean_condition(main, controls, bracket,
                                                expected_controls=3 if meta["configuration"]
                                                == "A" else 6)))
    return trials, conditions


def condition_row(main, controls, bracket, clean) -> dict:
    first = main[0]
    return {**{key: first[key] for key in (
        "block", "date", "method", "frequency_mhz", "configuration", "dwell_us")},
        "main_passes": sum(r["passed"] for r in main), "main_attempts": len(main),
        "control_passes": sum(r["passed"] for r in controls),
        "control_attempts": len(controls), "bracket_pass": bracket,
        "clean_condition_pass": clean,
        "observable_ports": first["observable_ports"]}


def save(fig, name: str) -> None:
    fig.savefig(DEST / "png" / f"{name}.png", dpi=170, bbox_inches="tight",
                facecolor="#fbfcfe")
    plt.close(fig)


def axes_style(ax, ylabel=None):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.2)
    ax.set_axisbelow(True)
    if ylabel:
        ax.set_ylabel(ylabel)


def plot_matrix(conditions):
    fig, axes = plt.subplots(2, 1, figsize=(12, 8.5), layout="constrained",
                             gridspec_kw={"height_ratios": [3, 4]})
    for ax, method, title in zip(axes, ("rolling_50ms", "fixed_any_budget"), (
        "Frozen reference-assisted timing · common 50 ms observations",
        "Earlier fixed decoder · passing at some tested integration budget"), strict=True):
        rows = [r for r in conditions if r["method"] == method]
        keys = sorted({(r["frequency_mhz"], r["configuration"], r["block"]) for r in rows})
        matrix = np.full((len(keys), len(DWELLS)), np.nan)
        labels = []
        for y, (freq, config, block) in enumerate(keys):
            subset = [r for r in rows if r["block"] == block]
            head = subset[0]
            labels.append(f"{freq:g} MHz / {config}\ncontrols "
                          f"{head['control_passes']}/{head['control_attempts']}; "
                          f"bracket {'pass' if head['bracket_pass'] else 'incomplete'}")
            for x, dwell in enumerate(DWELLS):
                match = [r for r in subset if r["dwell_us"] == dwell]
                if match:
                    r = match[0]
                    matrix[y, x] = r["main_passes"]
                    ax.text(x, y, f"{r['main_passes']}/{r['main_attempts']}"
                            + (" ✓" if r["clean_condition_pass"] else ""), ha="center",
                            va="center", fontsize=14, color="#162b3a")
                else:
                    ax.text(x, y, "not tested", ha="center", va="center", color="#687581")
        cmap = ListedColormap(["#edc2bd", "#e7d0b3", "#dbe3ac", "#9bd0bd"])
        cmap.set_bad("#edf0f4")
        ax.imshow(matrix, vmin=0, vmax=3, cmap=cmap, aspect="auto")
        ax.set_yticks(range(len(keys)), labels)
        ax.set_xticks(range(len(DWELLS)), [str(d) for d in DWELLS])
        ax.set_xlabel("Active dwell per port (µs)")
        ax.set_title(title, loc="left", weight="bold", pad=12)
    fig.suptitle("Main phase + gain passes are not whole-block qualification\n"
                 "✓ = three main trials + complete passing controls and reference bracket; "
                 "not bearing qualification", fontsize=13)
    save(fig, "fig01_cross_band_evidence_matrix")


def plot_phase(trials):
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.5), layout="constrained", sharex=True)
    for col, (freq, config) in enumerate(((915, "A"), (2475, "A"), (2475, "B"))):
        rows = [r for r in trials if r["method"] == "rolling_50ms"
                and r["frequency_mhz"] == freq and r["configuration"] == config
                and not r["control"]]
        for y, metric in enumerate(("phase_rms_deg", "max_gain_error_db")):
            ax = axes[y, col]
            for r in rows:
                x = DWELLS.index(r["dwell_us"]) + (r["round"] - 2) * 0.12
                ax.scatter(x, r[metric], marker="o" if r["passed"] else "x",
                           color=COLORS[col], s=42, zorder=3)
            ax.axhline(10 if y == 0 else 1, color="#b13a3a", ls="--", lw=1)
            axes_style(ax, "Weighted phase RMS (°)" if y == 0
                       else "Max. observable gain error (dB)")
            ax.set_xticks(range(len(DWELLS)), [str(d) for d in DWELLS])
            ax.set_xlim(-.5, 4.5)
            if y == 0:
                ax.set_yscale("log")
                ax.set_ylim(.08, 80)
            else:
                ax.set_xlabel("Active dwell (µs)")
                ax.set_ylim(bottom=0)
        axes[0, col].set_title(f"{freq} MHz · {'2' if config == 'A' else '5'} MS/s\n"
                               f"{'six' if freq == 915 else 'five'} observable ports")
    fig.suptitle("Same 50 ms estimator budget; separate blocks, dates and frozen weights\n"
                 "Each mark is one main trial · ○ combined pass · × combined fail", fontsize=14)
    save(fig, "fig02_phase_and_gain_by_dwell")


def plot_bearings():
    rolling = read_csv(f"{COMPLETED}/data/rolling.csv")
    fresh = [r for r in read_csv(f"{COMPREHENSIVE}/data/fresh-bearing-comparison.csv")
             if float(r["frequency_mhz"]) == 5800]
    fig, axes = plt.subplots(2, 2, figsize=(12, 7.5), layout="constrained")
    for col, rows in enumerate((rolling, fresh)):
        groups = ("A", "B") if col == 0 else ("retrospective_full", "causal_prefix_1s")
        if col == 1:
            groups = tuple(dict.fromkeys(r["method"] for r in rows))
        for j, group in enumerate(groups):
            subset = [r for r in rows if (r["configuration"] if col == 0 else r["method"])
                      == group and (col == 1 or not truth(r["control"]))]
            for y in (0, 1):
                ax = axes[y, col]
                metric = (("bearing_rms_deg" if y == 0 else "bearing_valid_percent")
                          if col == 0 else ("all_group_repeatability_rms_deg"
                                            if y == 0 else "model_valid_percent"))
                x = [DWELLS.index(int(r["dwell_us"])) + (j - .5) * .18
                     + (int(r["round"]) - 2) * .04 for r in subset]
                label = ({"A": "2 MS/s", "B": "5 MS/s"}.get(group, group)
                         .replace("retrospective_full", "Whole-record timing")
                         .replace("causal_prefix_1s", "1 s prefix / open-loop")
                         .replace("causal_open_loop", "1 s prefix / open-loop"))
                ax.scatter(x, [float(r[metric]) for r in subset], color=COLORS[j],
                           marker=("o", "x")[j], label=label, s=40)
                ax.set_xticks(range(5), [str(d) for d in DWELLS])
                ax.set_xlim(-.5, 4.5)
        axes[0, col].set_title("2475 MHz · fresh rolling / 50 ms" if col == 0
                               else "5800 MHz · earlier block / ≈24–25 ms")
        axes[0, col].set_yscale("log")
        axes[0, col].set_ylim(.35, 80)
        axes[0, col].legend(fontsize=9)
        axes[1, col].set_ylim(-5, 105)
        axes[1, col].set_xlabel("Active dwell (µs)")
        axes_style(axes[0, col], "All-output bearing jitter (° RMS)")
        axes_style(axes[1, col], "Legacy model-valid outputs (%)")
    fig.suptitle("Bearing repeatability is not surveyed-angle accuracy\n"
                 "Rejected outputs stay in the jitter calculation · 915 MHz bearing not tested",
                 fontsize=14)
    save(fig, "fig03_bearing_repeatability_and_validity")


def plot_dense():
    rows = read_csv(f"{DENSE}/data/dense_frequency_conditions.csv")
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.5), layout="constrained",
                             gridspec_kw={"height_ratios": [1, 3]})
    for col, tx in enumerate(("TX1", "TX2")):
        data = sorted((r for r in rows if r["tx_port"] == tx),
                      key=lambda r: int(r["frequency_hz"]))
        freq = np.array([int(r["frequency_hz"]) / 1e6 for r in data])
        phase = np.array([bool(r["phase_10deg_wall_latency_ms"]) for r in data])
        valid = np.array([truth(r["full_bearing_valid"]) for r in data])
        axes[0, col].imshow(np.array([phase, valid]), aspect="auto", vmin=0, vmax=1,
                            cmap=ListedColormap(["#dcb4b0", "#64af9b"]),
                            extent=[freq[0] - .5, freq[-1] + .5, 1.5, -.5])
        axes[0, col].set_yticks([0, 1], [f"Phase {sum(phase)}/{len(data)}",
                                         f"Bearing {sum(valid)}/{len(data)}"])
        axes[0, col].set_title(f"{tx} · 200 µs · historical whole-record decoder")
        bearing = np.array([float(r["full_bearing_deg"]) for r in data])
        ax = axes[1, col]
        ax.scatter(freq[~valid], bearing[~valid], color="#b5bdc5", marker="x", s=22,
                   label="Rejected estimate")
        ax.scatter(freq[valid], bearing[valid], color=COLORS[col], s=18,
                   label="Model admitted")
        ax.axhline(90 if tx == "TX1" else 180, color="#b55c42", ls="--",
                   label="Approximate setup direction")
        ax.set_ylim(0, 360)
        ax.set_yticks([0, 90, 180, 270, 360])
        ax.set_xlim(freq[0] - .5, freq[-1] + .5)
        ax.set_xlabel("Frequency (MHz)")
        axes_style(ax, "Estimated bearing (° clockwise from forward)")
        ax.legend(fontsize=8, loc="upper left")
    fig.suptitle("September 3 · 149 × 1 MHz centres per source · not pooled with September 8/9\n"
                 "Phase gate uses variable integration; bearing gate uses full captures",
                 fontsize=14)
    save(fig, "fig04_historical_frequency_and_bearing")


def plot_causal():
    rows = read_csv(f"{COMPREHENSIVE}/data/historical-causal-comparison.csv")
    keys = list(dict.fromkeys((r["frequency_mhz"], r["tx"], r["dwell_us"]) for r in rows))
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    for ax, metric, ylabel in zip(axes, ("all_group_repeatability_rms_deg", "model_valid_percent"),
                                 ("All-output bearing jitter (° RMS)", "Model-valid outputs (%)"),
                                 strict=True):
        for j, method in enumerate(("Whole-record timing", "1 s past-only, then open-loop")):
            values = [float(next(r for r in rows if (r["frequency_mhz"], r["tx"],
                      r["dwell_us"]) == key and r["timing"] == method)[metric]) for key in keys]
            ax.bar(np.arange(len(keys)) + (j - .5) * .34, values, width=.32,
                   color=COLORS[j], label=method)
        ax.set_xticks(range(len(keys)), [f"{float(f):g} MHz\nTX{tx} · {d} µs" for f, tx, d in keys])
        axes_style(ax, ylabel)
        ax.legend(fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.01))
    fig.suptitle("Historical IQ replayed two ways: a good whole-record fit is not a live clock\n"
                 "Paired captures; approximately matched integration budgets; no surveyed truth",
                 fontsize=14)
    save(fig, "fig05_retrospective_vs_causal")


def plot_schedule(rows):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5), layout="constrained")
    x = np.arange(len(rows))
    bottom = np.zeros(len(rows))
    for key, label, color in (("all_ports_retained_percent", "Retained active (all 6)", "#3c998d"),
                              ("trim_percent", "Edge trims", "#d5bb82"),
                              ("guard_percent", "Six guards", "#71859c"),
                              ("marker_percent", "Marker", "#bf7969")):
        values = np.array([r[key] for r in rows])
        axes[0].bar(x, values, bottom=bottom, label=label, color=color, width=.65)
        bottom += values
    axes_style(axes[0], "Nominal cycle allocation (%)")
    axes[0].legend(fontsize=9, loc="upper left", bbox_to_anchor=(0, 1.2), ncols=2)
    values = [r["nominal_retained_ms_per_port_in_50ms"] for r in rows]
    axes[1].bar(x, values, color=COLORS[0], width=.65)
    for i, value in enumerate(values):
        axes[1].text(i, value + .15, f"{value:.2f} ms", ha="center")
    axes[1].set_ylim(0, 9)
    axes_style(axes[1], "Retained time per port in 50 ms (ms)")
    for ax in axes:
        ax.set_xticks(x, [str(r["dwell_us"]) for r in rows])
        ax.set_xlabel("Active dwell (µs)")
    fig.suptitle("Calculated C6 budget: cycle = 6 × dwell + 300 µs\n"
                 "20 µs guards + 180 µs marker + 5 µs trims per edge; retained ≠ proven settled",
                 fontsize=14)
    save(fig, "fig06_schedule_and_integration_budget")


def array_response(frequency_hz, angles_deg, *, diameter_m=.051, truth_deg=90):
    """Ideal equal-weight C6 power response, not a measurement or a gate fit."""
    azimuths = np.arange(6) * np.pi / 3
    positions = diameter_m / 2 * np.column_stack([np.sin(azimuths), np.cos(azimuths)])
    angles = np.deg2rad(angles_deg)
    directions = np.column_stack([np.sin(angles), np.cos(angles)])
    truth_direction = np.array([np.sin(np.deg2rad(truth_deg)),
                                np.cos(np.deg2rad(truth_deg))])
    differential = positions @ (directions - truth_direction).T
    return np.abs(np.exp(2j * np.pi * frequency_hz / 299792458 * differential).mean(axis=0)) ** 2


def plot_aperture():
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5), layout="constrained",
                             gridspec_kw={"width_ratios": [1, 2]})
    a = np.arange(6) * np.pi / 3
    x, y = 25.5 * np.sin(a), 25.5 * np.cos(a)
    axes[0].plot(np.r_[x, x[0]], np.r_[y, y[0]], color="#c3cdd4")
    axes[0].scatter(x, y, s=65, color=COLORS[0])
    for px, py, port in zip(x, y, PORTS, strict=True):
        axes[0].text(px * 1.24, py * 1.24, port, ha="center", va="center")
    axes[0].annotate("TX bearing 90°", xy=(30, 0), xytext=(40, 0), va="center",
                     arrowprops={"arrowstyle": "->"})
    axes[0].set(xlim=(-42, 82), ylim=(-44, 44), aspect="equal", xlabel="Right (mm)",
                ylabel="Forward (mm)", title="Nominal 51 mm-diameter C6")
    angles = np.linspace(0, 360, 1441)
    for freq, color in zip((915, 2475, 5811), COLORS, strict=True):
        power = array_response(freq * 1e6, angles)
        axes[1].plot(angles, 10 * np.log10(np.maximum(power, 1e-5)), color=color,
                     label=f"{freq} MHz · D/λ={.051 * freq * 1e6 / 299792458:.3f}")
    axes[1].axvline(90, color="#777777", ls="--", lw=1)
    axes[1].set(xlim=(0, 360), ylim=(-35, 1), xlabel="Candidate bearing (°)",
                xticks=[0, 90, 180, 270, 360], title="Ideal normalized matched-array power")
    axes_style(axes[1], "Response relative to true direction (dB)")
    axes[1].legend(fontsize=9)
    fig.suptitle("Simulation only: equal phase precision does not mean equal angle precision\n"
                 "Equal weights, noiseless plane wave; no cables, coupling or multipath",
                 fontsize=14)
    save(fig, "fig09_aperture_and_ideal_response")


def plot_runtime():
    rows = read_csv(f"{COMPLETED}/data/rolling.csv")
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.5), layout="constrained")
    summary = []
    for i, config in enumerate(("A", "B")):
        records = [r for r in rows if r["configuration"] == config
                   and r["dwell_us"] == "100" and not truth(r["control"])]
        values = [float(r["mean_compute_ms"]) for r in records]
        mean = float(np.mean(values))
        summary.append({"configuration": config, "dwell_us": 100, "records": len(records),
                        "mean_of_record_mean_compute_ms": mean,
                        "observation_ms": 50, "processing_budget_ratio": mean / 50})
        axes[0].bar(i, mean, color=COLORS[i], width=.55)
        axes[0].scatter([i] * len(values), values, color="#192d3a", s=25, zorder=3)
        axes[0].text(i, max(values) + 15, f"{mean:.0f} ms mean\n{mean / 50:.1f}× budget",
                     ha="center")
    axes[0].axhline(50, color="#b44a3d", ls="--", label="50 ms output budget")
    axes[0].set_xticks([0, 1], ["2 MS/s", "5 MS/s"])
    axes[0].set_ylim(0, 750)
    axes[0].legend(fontsize=9)
    axes_style(axes[0], "Offline compute per output window (ms)")
    labels = ["100 µs dwell", "C6 cycle at 100 µs", "Observation / output hop",
              "Mean compute: 2 MS/s", "Mean compute: 5 MS/s", "Startup history"]
    values = [.1, .9, 50, summary[0]["mean_of_record_mean_compute_ms"],
              summary[1]["mean_of_record_mean_compute_ms"], 1000]
    axes[1].barh(range(6), values, color=["#9badba"] * 3 + list(COLORS[:2]) + ["#9badba"])
    axes[1].set_yticks(range(6), labels)
    axes[1].invert_yaxis()
    axes[1].set_xscale("log")
    axes[1].set_xlabel("Different time scales (ms; logarithmic)")
    axes[1].set_title("Not an additive end-to-end latency budget")
    fig.suptitle("The present offline replay does not meet real-time throughput\n"
                 "Three 100 µs records per rate; host load uncontrolled; delivery unmeasured",
                 fontsize=14)
    save(fig, "fig07_runtime_and_latency_scales")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-inputs", action="store_true",
                        help="Explicitly replace source lock with the current versioned inputs")
    args = parser.parse_args()
    (DEST / "png").mkdir(parents=True, exist_ok=True)
    (DEST / "data").mkdir(parents=True, exist_ok=True)
    lock_path = DEST / "data/source-lock.json"
    if args.snapshot_inputs:
        write_json(lock_path, {"schema": 1, "scope": "Compact artifacts only; no raw-IQ audit",
                              "inputs": {name: {"sha256": digest(ROOT / name),
                                                "bytes": (ROOT / name).stat().st_size}
                                         for name in INPUTS}})
    verify_inputs(json.loads(lock_path.read_text()))
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.titlesize": 11, "figure.facecolor": "#fbfcfe",
                         "axes.facecolor": "#fbfcfe"})
    trials, conditions = collect()
    budgets = [schedule(d) for d in DWELLS]
    write_csv(DEST / "data/trials.csv", trials)
    write_csv(DEST / "data/conditions.csv", conditions)
    write_csv(DEST / "data/schedule-budget.csv", budgets)
    plot_matrix(conditions)
    plot_phase(trials)
    plot_bearings()
    plot_dense()
    plot_causal()
    plot_schedule(budgets)
    plot_aperture()
    write_csv(DEST / "data/runtime-summary.csv", plot_runtime())
    write_json(DEST / "data/figures-manifest.json", {
        "schema": 1, "renderer_sha256": digest(Path(__file__)),
        "source_lock_sha256": digest(lock_path),
        "outputs": {str(path.relative_to(DEST)): {"sha256": digest(path),
                                                 "bytes": path.stat().st_size}
                    for path in sorted([*(DEST / "png").glob("*.png"),
                                        *(DEST / "data").glob("*.csv")])},
    })
    print(f"Verified {len(INPUTS)} source artifacts; rendered 8 PNGs and 4 CSVs in {DEST}")


if __name__ == "__main__":
    main()
