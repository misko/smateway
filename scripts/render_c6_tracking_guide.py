#!/usr/bin/env python3
"""Render the practical dual-band C6 guide from explicitly labeled evidence."""

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from c6_tracking_example import LUT, PORTS, PROFILE, ROOT  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch  # noqa: E402

from smateway.rate_timing import complex_value, load, sha256  # noqa: E402
from smateway.tracking.schedule import ArrayGeometry  # noqa: E402

OUT = ROOT / "docs/c6_dual_band_tracking_guide"
BLUE, ORANGE, GREEN = "#277da8", "#dc843b", "#208b73"


def save(fig, name, title, note):
    fig.suptitle(title, x=0.03, ha="left", weight="bold", fontsize=17)
    fig.text(0.03, 0.02, note, fontsize=9, color="#465865")
    fig.tight_layout(rect=(0.01, 0.08, 0.99, 0.91))
    path = OUT / "png" / f"{name}.png"
    fig.savefig(path, dpi=170)
    plt.close(fig)


def boxes(ax, steps):
    ax.set(xlim=(0, 10), ylim=(0, len(steps) * 1.5))
    ax.axis("off")
    for index, (title, text) in enumerate(steps):
        y = (len(steps) - index - 1) * 1.5 + 0.15
        ax.add_patch(
            FancyBboxPatch(
                (0.2, y),
                9.6,
                1.15,
                boxstyle="round,pad=.05",
                facecolor="#ecf4f8",
                edgecolor="#96b2c2",
            )
        )
        ax.text(0.45, y + 0.78, title, weight="bold", fontsize=12)
        ax.text(0.45, y + 0.3, text, fontsize=10)
        if index + 1 < len(steps):
            ax.annotate("", xy=(5, y - 0.2), xytext=(5, y), arrowprops={"arrowstyle": "->"})


def main():
    (OUT / "png").mkdir(parents=True, exist_ok=True)
    demos = [load(OUT / f"data/demo-{f}.json") for f in (2475, 5800)]
    real = load(OUT / "data/replay-5800-B-200us.json")
    profile = load(PROFILE)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.grid": True,
            "grid.alpha": 0.18,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "figure.facecolor": "white",
        }
    )
    fig, axs = plt.subplots(1, 2, figsize=(13, 6))
    geom = ArrayGeometry.circular("C6", PORTS, radius_mm=25.5)
    for ax, diameter, title in zip(
        axs,
        [51, 51 * 5800 / 2450],
        ["Existing shared array: 51 mm diameter", "Optional 2.45 GHz-specific scale: ≈121 mm"],
        strict=True,
    ):
        xy = geom.positions_m * 1000 * diameter / 51
        ax.add_patch(Circle((0, 0), diameter / 2, fill=False, ls="--", color="#a8b6bf"))
        ax.scatter(xy[:, 0], xy[:, 1], s=170, color=GREEN)
        for port, (x, y) in zip(PORTS, xy, strict=True):
            ax.text(x * 1.22, y * 1.22, port, ha="center", va="center", weight="bold")
        ax.set(
            xlim=(-80, 80),
            ylim=(-80, 80),
            aspect="equal",
            xlabel="Right (mm)",
            ylabel="Forward (mm)",
            title=title,
        )
    save(
        fig,
        "fig01_array_geometry",
        "One six-port order; geometry and antennas determine band performance",
        "Nominal layouts, not surveyed. Larger low-band array is a proposal, not a "
        "drop-in dual-band upgrade; validate ambiguities at 5.8 GHz.",
    )

    fig, ax = plt.subplots(figsize=(13, 7))
    boxes(
        ax,
        [
            (
                "Laboratory: TX1 → splitter → RX1 + OTA emitter",
                "RX1 observes a conducted copy; RX2 observes the same tone "
                "through the switched C6 array.",
            ),
            (
                "Receive-only target: emitter → fixed reference antenna → RX1",
                "The same emitter also illuminates six array antennas → PCB common → RX2.",
            ),
            (
                "Six switched antennas + one reference antenna = seven physical antennas",
                "The extra reference is not a seventh switched port; "
                "do not split one array element without redesign.",
            ),
            (
                "One dual-RX Pluto observes one RF band at a time",
                "2.4 ↔ 5.8 GHz changes require retuning, settling, "
                "fresh timing lock and the correct calibration.",
            ),
        ],
    )
    save(
        fig,
        "fig02_reference_architecture",
        "The reference cancels emitter phase; it does not need to know absolute TX phase",
        "Architecture guide: same-signal OTA reference, moving-source timing and "
        "simultaneous multi-emitter operation remain unqualified.",
    )

    fig, ax = plt.subplots(figsize=(14, 4.8))
    ax.broken_barh([(0, 180)], (0.15, 0.7), facecolors="#bdc8d0")
    ax.text(90, 0.5, "Marker\n180 µs", ha="center", va="center", fontsize=9)
    for i, port in enumerate(PORTS):
        guard = 180 + i * 220
        start = guard + 20
        ax.broken_barh([(guard, 20)], (0.15, 0.7), facecolors=ORANGE)
        ax.broken_barh([(start, 200)], (0.15, 0.7), facecolors=BLUE)
        ax.broken_barh([(start + 5, 190)], (0.25, 0.5), facecolors=GREEN)
        ax.text(start + 100, 0.5, port, ha="center", va="center", color="white", fontsize=10)
    ax.annotate(
        "Continuous ALL_OFF before ANT1 = 180 + 20 = 200 µs",
        xy=(180, 0.9),
        xytext=(330, 1.35),
        arrowprops={"arrowstyle": "->"},
    )
    ax.set(xlim=(0, 1500), ylim=(0, 1.8), yticks=[], xlabel="Nominal time in one cycle (µs)")
    ax.text(500, 0.02, "Blue edges discarded: 5 µs each; green retained: 190 µs", fontsize=10)
    save(
        fig,
        "fig03_selector_timeline",
        "200 µs dwell produces a 1.5 ms C6 cycle, not a 200 µs six-antenna snapshot",
        "Exact generated grammar: 6 × (20 µs guard + 200 µs dwell) + 180 µs marker. "
        "Trim is analysis policy, not proof of settling.",
    )

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.axis("off")
    rows = [
        [
            "Selector",
            "tracking-c6-200us-v1",
            "Autonomous; 20 µs experimental guard",
            "Static bench image is different",
        ],
        [
            "Receiver",
            "v0.40 tandem-agc-v7",
            "2 RX; metadata ABI 2; manual gains",
            "Match host libiio/PPU ABI",
        ],
        [
            "Lab source",
            "v0.43 ddr-ring-v1",
            "TX1 pilot; 2 MS/s / 1.6 MHz",
            "Not needed for receive-only target",
        ],
        [
            "5.8 GHz starting point",
            "5 MS/s / 1.6 MHz / 200 µs",
            "7.68° median phase RMS, latest B",
            "No production bearing qualification",
        ],
        [
            "2.475 GHz starting control",
            "2 MS/s / 1.6 MHz / 200 µs",
            "Conservative diagnostic, bracket first",
            "Latest 5 MS/s B/D blocks failed",
        ],
    ]
    table = ax.table(
        cellText=rows,
        colLabels=["Layer", "Choice", "Meaning", "Important limit"],
        cellLoc="left",
        loc="center",
        colWidths=[0.18, 0.25, 0.29, 0.28],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 3.0)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor("white")
        cell.set_facecolor("#dcebf2" if row == 0 else "#f0f5f8")
    save(
        fig,
        "fig04_firmware_and_settings",
        "Firmware compatibility is a stack, not just an accepted LO setting",
        "Radio versions shortened here; exact versions, image hashes and readback "
        "requirements are in the report. These are lab recipes, not released tracker modes.",
    )

    fig, ax = plt.subplots(figsize=(13, 9))
    boxes(
        ax,
        [
            (
                "1  Validate the paired capture",
                "Same stream, continuous counters, two simultaneous RX channels, no clipping.",
            ),
            (
                "2  Select the common signal",
                "Find tone or isolate a subband; align differential delay for wideband signals.",
            ),
            (
                "3  Determine sample-to-port timing",
                "Independent edge labels preferred; current replay uses a known-emitter template.",
            ),
            (
                "4  Estimate complex transfer per port",
                "Sum w · RX2 · conj(RX1), divide by summed w · |RX1|² over settled samples.",
            ),
            (
                "5  Apply calibration once",
                "PCB LUT at actual RF frequency; then final cables / installed angular response.",
            ),
            (
                "6  Solve and report uncertainty",
                "Compare the complex vector with surveyed steering; "
                "preserve ambiguous/invalid outputs.",
            ),
        ],
    )
    save(
        fig,
        "fig05_processing_pipeline",
        "A small, auditable path from paired IQ to a diagnostic direction",
        "No FFT-per-dwell requirement. A calibrated complex coefficient contains "
        "amplitude and phase; spectrum selection and wideband processing are separate tasks.",
    )

    fig, axs = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    demo = demos[1]
    time = demo["time_us"]
    axs[0].plot(time, demo["rx1_real_excerpt"], lw=0.5, label="RX1 real", color=BLUE)
    axs[0].plot(time, demo["rx2_real_excerpt"], lw=0.5, label="RX2 real", color=ORANGE, alpha=0.7)
    axs[0].set(ylabel="Synthetic sample amplitude", title="A carrier oscillates inside each dwell")
    axs[0].legend(loc="upper right")
    axs[1].plot(time, demo["cross_real_excerpt"], lw=0.7, label="Re[RX2 conj(RX1)]", color=BLUE)
    axs[1].plot(time, demo["cross_imag_excerpt"], lw=0.7, label="Im[RX2 conj(RX1)]", color=ORANGE)
    axs[1].set(
        xlabel="Time (µs)", ylabel="Complex product", title="Common transmitter phase cancels"
    )
    axs[1].legend(loc="upper right")
    save(
        fig,
        "fig06_iq_to_complex_transfer",
        "The useful quantity becomes a complex plateau after correlation",
        "SYNTHETIC 5.8 GHz signal, +100 kHz baseband tone; exact switch labels, "
        "isotropic array and additive noise. No hardware timing is tested.",
    )

    fig, axs = plt.subplots(2, 2, figsize=(14, 9))
    for col, d in enumerate(demos):
        raw = np.array([complex_value(v) for v in d["raw_transfer"]])
        cal = np.array([complex_value(v) for v in d["calibrated_transfer"]])
        ideal = np.array([complex_value(v) for v in d["expected_corrected_transfer"]])
        # Show relative phase so the common calibration gauge does not look like an error.
        for vec, label, color in [
            (raw, "Uncorrected", ORANGE),
            (cal, "PCB corrected", GREEN),
            (ideal, "Ideal truth", BLUE),
        ]:
            axs[0, col].plot(
                range(6), np.angle(vec / vec[0], deg=True), "o-", label=label, color=color
            )
        axs[0, col].set(
            xticks=range(6),
            xticklabels=PORTS,
            ylabel="Phase relative to ANT1 (wrapped °)",
            title=f"{d['frequency_hz'] / 1e6:.0f} MHz; true angle {d['true_bearing_deg']:.0f}°",
        )
        axs[0, col].legend(fontsize=9)
        axs[1, col].plot(
            d["bearing_grid_deg"], d["raw_model_likelihood"], label="Uncorrected", color=ORANGE
        )
        axs[1, col].plot(d["bearing_grid_deg"], d["likelihood"], label="PCB corrected", color=GREEN)
        axs[1, col].axvline(d["true_bearing_deg"], color=BLUE, ls="--", label="Truth")
        axs[1, col].set(
            xlabel="Candidate bearing (°)",
            ylabel="Normalized score",
            xlim=(0, 360),
            title=f"Corrected {d['bearing_deg']:.2f}°; legacy gate "
            + ("accepts" if d["legacy_gate_valid"] else "rejects"),
        )
        axs[1, col].legend(fontsize=9)
    save(
        fig,
        "fig07_synthetic_calibration_and_bearing",
        "Applying the PCB LUT once recovers the synthetic geometry",
        "SYNTHETIC identity demonstration: the same measured LUT generates and "
        "corrects the response. This is not independent validation or achievable accuracy.",
    )

    fig, axs = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    rows = real["windows"]
    valid = [r for r in rows if r["status"] == "analyzed"]
    t = [r["prediction_stop_sample"] / real["configuration"]["sample_rate_hz"] for r in valid]
    axs[0].plot(t, [r["direction"]["bearing_deg"] for r in valid], ".", color=BLUE)
    axs[0].set(
        ylabel="Diagnostic model peak (°)",
        ylim=(0, 360),
        title="New offline replay of one retained 5800 MHz / B / 200 µs record",
    )
    axs[1].plot(
        t, [r["direction"]["spatial_phase_residual_deg"] for r in valid], ".-", color=ORANGE
    )
    axs[1].set(
        xlabel="Prediction-window end in retained capture (s)", ylabel="Spatial phase residual (°)"
    )
    for row in rows:
        if row["status"] != "analyzed":
            for ax in axs:
                ax.axvspan(
                    row["prediction_start_sample"] / 5e6,
                    row["prediction_stop_sample"] / 5e6,
                    color="#bdc6ce",
                )
    save(
        fig,
        "fig08_real_record_replay",
        "The real example preserves the difference between a result and a qualified bearing",
        "REAL retained IQ; 1 s training / 50 ms predictions; nominal-center RF "
        "convention. Known-emitter template, no surveyed truth; failures are not deleted.",
    )

    fig, axs = plt.subplots(1, 2, figsize=(13, 6))
    dwell = np.array([25, 50, 100, 200, 1000])
    cycle = 6 * dwell + 300
    axs[0].plot(dwell, 1e6 / cycle, "o-", color=BLUE)
    axs[0].set(
        xscale="log",
        xlabel="Per-port dwell (µs)",
        ylabel="Nominal revisits / second",
        title="Revisit rate is not delivered direction rate",
    )
    axs[1].plot(dwell, 50 * (dwell - 10) / cycle, "o-", color=GREEN)
    axs[1].set(
        xscale="log",
        xlabel="Per-port dwell (µs)",
        ylabel="Useful per-port time / 50 ms (ms)",
        title="Shorter dwells spend more time in fixed overhead",
    )
    for ax in axs:
        ax.set_xticks(dwell, [str(x) for x in dwell])
    save(
        fig,
        "fig09_dwell_tradeoff",
        "Choose dwell for phase quality and motion, not the largest nominal switch count",
        "Schedule theory with 5 µs edge trims. Current replay needs 1 s training "
        "and is not a real-time implementation; 25/50 µs are not qualified defaults.",
    )

    fig, (ax, left) = plt.subplots(1, 2, figsize=(14, 7), width_ratios=[1, 1.4])
    ax.add_patch(Circle((0, 0), 1, fill=False, color="#abb8c0"))
    for theta, label, color in [
        (np.arange(0, 360, 30), "Training directions (illustrative)", BLUE),
        (np.arange(15, 360, 30), "Untouched angles", ORANGE),
    ]:
        rad = np.deg2rad(theta)
        ax.scatter(np.sin(rad), np.cos(rad), label=label, color=color, s=50)
    ax.scatter([0], [0], marker="h", s=200, color=GREEN)
    ax.set(
        xlim=(-1.3, 1.3),
        ylim=(-1.3, 1.3),
        aspect="equal",
        title="Hold out angles, not just samples",
    )
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.06), fontsize=9)
    ax.axis("off")
    boxes(
        left,
        [
            (
                "First, 5800 MHz static + 200 µs",
                "Survey four angles and compare switched versus static.",
            ),
            (
                "Then, installed response per band",
                "Cable-tip correction, angular grid and new-room holdouts.",
            ),
            (
                "Next, 2.4 GHz reference closure",
                "Resolve current failed brackets before faster timing.",
            ),
            (
                "Finally, OTA reference and live motion",
                "Freeze gates; measure latency, dropouts and false-valid output.",
            ),
        ],
    )
    save(
        fig,
        "fig10_validation_plan",
        "Calibration becomes tracking only after independent angular and runtime tests",
        "PROPOSED: diagram uses 30°/15° for readability; report suggests an initial "
        "10° training / 5° offset grid. No production operating envelope is qualified.",
    )

    with (OUT / "data/port-map.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["slot", "port", "gpio_PA3_PA0", "x_m", "y_m"])
        for i, (state, xy) in enumerate(zip(profile["states"], geom.positions_m, strict=True)):
            writer.writerow([i, state["name"], state["gpio_code_pa3_pa0"], *xy])
    paths = [
        LUT,
        PROFILE,
        ROOT / "scripts/c6_tracking_example.py",
        Path(__file__).resolve(),
        ROOT / "src/smateway/tracking/bearing.py",
        ROOT / "src/smateway/tracking/calibration.py",
        ROOT / "src/smateway/tracking/manifold.py",
        ROOT / "src/smateway/reference_timing.py",
        ROOT / "firmware/stm32c011/apps/hexcal/main.c",
        ROOT / "Makefile",
        ROOT / "scripts/verify_tracking_c6_firmware.py",
        ROOT / "scripts/flash_tracking_c6_firmware.py",
        ROOT / "scripts/restore_tracking_selector_backup.py",
        ROOT / "scripts/capture_rate_timing.py",
        ROOT / "docs/full_5ms_campaign/data/condition-details.csv",
    ]
    manifest = {
        "schema": 1,
        "scope": "guide, synthetic demonstration and one retained-IQ replay",
        "production_qualified": False,
        "sources": {str(p.relative_to(ROOT)): sha256(p) for p in paths},
        "data": {
            str(p.relative_to(OUT)): sha256(p)
            for p in sorted((OUT / "data").iterdir())
            if p.name != "manifest.json"
        },
        "figures": {
            str(p.relative_to(OUT)): sha256(p) for p in sorted((OUT / "png").glob("*.png"))
        },
    }
    if (OUT / "README.md").exists():
        manifest["report_sha256"] = sha256(OUT / "README.md")
    (OUT / "data/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Rendered 10 PNGs in {OUT}")


if __name__ == "__main__":
    main()
