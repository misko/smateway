#!/usr/bin/env python3
"""Offline, source-traceable synthesis; never opens radios or re-fits raw IQ."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import ListedColormap, LogNorm  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from matplotlib.ticker import NullLocator  # noqa: E402

from smateway.tracking.bearing import solve_bearing  # noqa: E402
from smateway.tracking.manifold import far_field_steering, near_field_steering  # noqa: E402
from smateway.tracking.schedule import ArrayGeometry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/calibration_error_budget_and_production_readiness"
PORTS = tuple(f"ANT{i}" for i in range(1, 9))
C6 = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
CENTRES = (915, 2475, 5800, 5811)
DWELLS = (25, 50, 100, 200, 1000)
COLORS = ("#277DA8", "#E28B39", "#188977")
EPOCHS = ("Original / 2 MS/s", "Jittered / 2 MS/s", "Latest / 5 MS/s")
REPORTS = (
    "5g8_root_cause_analysis",
    "5g8_external_fixture_campaign",
    "broadband_external_fixture_campaign",
    "broadband_future_sweep_comparison",
    "broadband_midpoint_campaign",
    "dense_1mhz_campaign",
    "pcb_direct_injection_calibration",
    "hexray_tx_in_middle_calibration",
    "tracking_verification_campaign",
    "fast_tracking_timing_campaign",
    "higher_sample_rate_timing_campaign",
    "comprehensive_fast_switching",
    "completed_switching_analysis",
    "subghz_915_diagnostic",
    "cross_band_tracking_analysis",
    "jittered_fixture_comparison",
    "full_5ms_campaign",
    "tracking_development_plan",
)
SOURCES = {
    "pcb": "docs/pcb_direct_injection_calibration/data/campaign-results.json",
    "lut": "docs/pcb_direct_injection_calibration/data/calibration-lut.csv",
    "midpoints": "docs/broadband_midpoint_campaign/data/campaign-results.json",
    "conducted_dense": "docs/dense_1mhz_campaign/data/campaign-results.json",
    "ota_original": "docs/fast_tracking_timing_campaign/data/dense_frequency_conditions.csv",
    "ota_jittered": "docs/jittered_fixture_comparison/data/dense-after.csv",
    "ota_latest": "docs/full_5ms_campaign/data/dense.csv",
    "conditions": "docs/full_5ms_campaign/data/condition-details.csv",
    "rolling": "docs/full_5ms_campaign/data/rolling.csv",
    "audit": "docs/full_5ms_campaign/data/audit.json",
    "static": "docs/jittered_fixture_comparison/data/static-before-after.csv",
    "static_bearings": "docs/jittered_fixture_comparison/data/static-bearings.csv",
    "bearing_code": "src/smateway/tracking/bearing.py",
    "manifold_code": "src/smateway/tracking/manifold.py",
    "geometry_code": "src/smateway/tracking/schedule.py",
    "higher_rate_findings": "docs/higher_sample_rate_timing_campaign/FINDINGS.md",
    "frozen_timing_recipe": "docs/comprehensive_fast_switching/REFERENCE-TIMING-VALIDATION-v1.md",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boolean(value: str) -> bool:
    if value not in ("True", "False"):
        raise ValueError(f"Expected an explicit Boolean, got {value!r}")
    return value == "True"


def number(value: str) -> float:
    return float(value) if value else float("nan")


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, records: list[dict]) -> None:
    if not records:
        raise ValueError("Refusing to write an empty evidence table")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def dense_summary(cohorts: list[list[dict]]) -> list[dict]:
    result = []
    expected = set(range(5726, 5875))
    for epoch, records in zip(EPOCHS, cohorts, strict=True):
        for tx in ("TX1", "TX2"):
            rows = [r for r in records if r["tx_port"] == tx]
            mhz = [float(r["frequency_hz"]) / 1e6 for r in rows]
            if len(rows) != 149 or set(mhz) != expected:
                raise ValueError(f"Incomplete or duplicate dense grid: {epoch}/{tx}")
            residual = np.array([number(r["full_bearing_residual_phase_rms_deg"]) for r in rows])
            if not np.isfinite(residual).all():
                raise ValueError("Dense residuals missing; update explicit failure accounting")
            result.append(
                {
                    "epoch": epoch,
                    "tx_port": tx,
                    "records": len(rows),
                    "phase_admissions_variable_budget": int(
                        sum(np.isfinite(number(r["phase_10deg_wall_latency_ms"])) for r in rows)
                    ),
                    "spatial_model_admissions": sum(boolean(r["full_bearing_valid"]) for r in rows),
                    "spatial_phase_residual_p10_deg": float(np.percentile(residual, 10)),
                    "spatial_phase_residual_median_deg": float(np.median(residual)),
                    "spatial_phase_residual_p90_deg": float(np.percentile(residual, 90)),
                    "surveyed_angular_accuracy": "not measured",
                }
            )
    return result


def schedule_metrics() -> list[dict]:
    rows = []
    for dwell in DWELLS:
        cycle = 6 * dwell + 6 * 20 + 180
        retained = dwell - 10
        rows.append(
            {
                "dwell_us": dwell,
                "cycle_us": cycle,
                "revisits_per_s": 1e6 / cycle,
                "retained_per_visit_us": retained,
                "useful_per_port_percent": 100 * retained / cycle,
                "retained_per_port_per_50ms_ms": 50 * retained / cycle,
                "samples_per_visit_at_5msps": dwell * 5,
            }
        )
    return rows


def geometry() -> ArrayGeometry:
    return ArrayGeometry.circular("synthesis-ideal-C6", C6, radius_mm=25.5)


def ideal_diagnostics() -> tuple[list[dict], list[dict]]:
    geom = geometry()
    grid = np.arange(0.0, 360.0, 0.25)
    ideal, spherical = [], []
    for mhz in CENTRES:
        steering = far_field_steering(geom, mhz * 1e6, grid)
        fit = solve_bearing(steering[360], steering, grid)
        ideal.append(
            {
                "frequency_mhz": mhz,
                "true_bearing_deg": 90,
                "fit_bearing_deg": fit.bearing_deg,
                "ambiguity_margin_db": fit.ambiguity_margin_db,
                "phase_residual_deg": fit.residual_phase_rms_deg,
                "legacy_gate_valid": fit.valid,
                "reason": " ".join(fit.reasons),
                "diameter_wavelengths": 0.051 * mhz * 1e6 / 299792458,
            }
        )
        for distance in (0.2, 0.3, 0.5, 1.0, 1.5, 3.0):
            # Deliberate ideal-model experiment: spherical emitter, plane-wave fit.
            vector = near_field_steering(geom, mhz * 1e6, [[distance, 0]])[0]
            fit = solve_bearing(vector, steering, grid)
            spherical.append(
                {
                    "frequency_mhz": mhz,
                    "range_m": distance,
                    "true_bearing_deg": 90,
                    "fit_bearing_deg": fit.bearing_deg,
                    "plane_wave_phase_residual_deg": fit.residual_phase_rms_deg,
                    "scope": "ideal isotropic C6 simulation; not measured range or error",
                }
            )
    return ideal, spherical


class Renderer:
    def __init__(self, output: Path):
        self.output = output
        self.png = output / "png"
        self.data = output / "data"
        self.png.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.figures: list[dict] = []
        self.inputs = {
            name: {"path": path, "sha256": sha(ROOT / path)} for name, path in SOURCES.items()
        }
        for name in REPORTS:
            path = f"docs/{name}/README.md"
            self.inputs[f"report:{name}"] = {"path": path, "sha256": sha(ROOT / path)}
        plt.rcParams.update(
            {
                "font.family": "DejaVu Sans",
                "font.size": 10,
                "axes.titlesize": 12,
                "axes.labelsize": 10,
                "figure.titlesize": 17,
                "axes.spines.top": False,
                "axes.spines.right": False,
                "axes.grid": True,
                "grid.alpha": 0.18,
                "figure.facecolor": "#ffffff",
                "axes.facecolor": "#fbfcfe",
                "savefig.facecolor": "#ffffff",
                "legend.frameon": False,
            }
        )

    def load(self, key: str):
        path = ROOT / SOURCES[key]
        return read_csv(path) if path.suffix == ".csv" else json.loads(path.read_text())

    def save(self, fig, name: str, title: str, note: str, sources: list[str], kind="measured"):
        fig.suptitle(title, x=0.03, ha="left", weight="bold")
        fig.text(0.03, 0.015, note, fontsize=9, color="#485563", va="bottom")
        fig.tight_layout(rect=(0.01, 0.075, 0.99, 0.92))
        path = self.png / f"{name}.png"
        fig.savefig(path, dpi=170)
        plt.close(fig)
        self.figures.append(
            {
                "path": f"png/{path.name}",
                "sha256": sha(path),
                "title": title,
                "evidence_kind": kind,
                "source_keys": sources,
                "scope_note": note,
            }
        )

    def overview(self):
        fig, ax = plt.subplots(figsize=(13, 6))
        ranges = [(0.5, 6), (5.00625, 5.99375), (2.1, 5.8), (2.15, 5.75), (5.726, 5.874)]
        labels = [
            "PCB knots / 12.5 MHz",
            "PCB independent holdouts / 6.25 MHz offsets",
            "Older splitter fixture / 1 MHz",
            "Older splitter midpoint holdouts / 100 MHz",
            "OTA dense / 1 MHz, 3 epochs",
        ]
        for y, ((start, stop), label) in enumerate(zip(ranges, labels, strict=True)):
            ax.plot([start, stop], [y, y], lw=12, solid_capstyle="butt", color=COLORS[y % 3])
            ax.text(start, y - 0.18, label, fontsize=10)
        ax.scatter(np.array(CENTRES) / 1000, [5] * 4, s=80, color="#663e85", zorder=4)
        ax.text(0.5, 4.72, "Latest native 5 MS/s: 915, 2475, 5800, 5811 MHz")
        ax.set(xlim=(0.4, 6.1), ylim=(5.6, -0.65), xlabel="Frequency (GHz)", yticks=[])
        self.save(
            fig,
            "fig01_evidence_coverage",
            "Coverage is not qualification",
            "Distinct calibration planes and validation roles. No production angular band "
            "is qualified; intervals are not permission to transmit.",
            ["lut", "pcb", "conducted_dense", "midpoints", "ota_latest", "conditions"],
        )

    def fixture(self):
        fig, (ax, geom_ax) = plt.subplots(1, 2, figsize=(14, 6), width_ratios=[1.7, 1])
        ax.set(xlim=(0, 10), ylim=(0, 7))
        ax.axis("off")

        def box(x, y, w, text, color="#e5f0f5"):
            ax.add_patch(
                FancyBboxPatch(
                    (x, y), w, 0.85, boxstyle="round,pad=0.08", facecolor=color, edgecolor="#567080"
                )
            )
            ax.text(x + w / 2, y + 0.425, text, ha="center", va="center", fontsize=10)

        def arrow(start, end):
            ax.annotate(
                "",
                xy=end,
                xytext=start,
                arrowprops={"arrowstyle": "->", "color": "#354e60", "lw": 1.8},
            )

        box(0.15, 4.7, 1.3, "TX1")
        box(2.0, 4.7, 1.5, "2-way\nsplitter")
        box(4.6, 5.8, 2, "Attenuator")
        box(8.0, 5.8, 1.6, "RX1\nreference")
        box(4.6, 3.7, 2, "OTA emitter")
        box(7.8, 3.7, 1.8, "Antennas\n+ final cables", "#fff0d9")
        box(7.8, 1.9, 1.8, "PCB selector", "#e0f2e6")
        box(7.8, 0.2, 1.8, "RX2\ncommon")
        box(0.15, 2.1, 1.3, "TX2")
        box(2.0, 2.1, 2.3, "Separate OTA\nemitter")
        for a, b in [
            ((1.45, 5.1), (2, 5.1)),
            ((3.5, 5.1), (4.6, 6.2)),
            ((6.6, 6.2), (8, 6.2)),
            ((3.5, 5.1), (4.6, 4.1)),
            ((6.6, 4.1), (7.8, 4.1)),
            ((8.7, 3.7), (8.7, 2.75)),
            ((8.7, 1.9), (8.7, 1.05)),
            ((1.45, 2.5), (2, 2.5)),
            ((4.3, 2.5), (7.8, 4.0)),
        ]:
            arrow(a, b)
        ax.text(
            0.1,
            0.25,
            "TX2 tests use a coherent TX1 pilot.\nThis is not yet an unknown-emitter reference.",
            fontsize=10,
        )
        ax.set_title("Current measurement architecture (not to scale)")
        g = geometry()
        xy = g.positions_m * 1000
        circle = plt.Circle((0, 0), 25.5, fill=False, ls="--", color="#a9b6be")
        geom_ax.add_patch(circle)
        geom_ax.scatter(xy[:, 0], xy[:, 1], s=240, color=COLORS[2])
        for port, (x, y) in zip(C6, xy, strict=True):
            geom_ax.text(x * 1.22, y * 1.22, port, ha="center", va="center", weight="bold")
        geom_ax.set(
            xlim=(-38, 38),
            ylim=(-38, 38),
            aspect="equal",
            xlabel="Right (+x), mm",
            ylabel="Forward (+y), mm",
        )
        geom_ax.set_title("Nominal C6: 51 mm diameter\nClockwise: 1, 2, 4, 8, 7, 5")
        self.save(
            fig,
            "fig02_fixture_and_ports",
            "The board calibration stops before the antennas",
            "PCB direct injection excluded deployed antennas/cables. Geometry shown is "
            "nominal, not surveyed. ANT3/ANT6 are omitted from C6.",
            ["report:full_5ms_campaign", "report:pcb_direct_injection_calibration"],
            "documented fixture / schematic",
        )

    def pcb(self):
        pcb = self.load("pcb")
        lut = self.load("lut")
        fig, axs = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
        base = sorted(
            [r for r in lut if r["port"] == "ANT1"], key=lambda r: float(r["frequency_hz"])
        )
        for port in PORTS[1:]:
            rr = sorted(
                [r for r in lut if r["port"] == port], key=lambda r: float(r["frequency_hz"])
            )
            if [r["frequency_hz"] for r in rr] != [r["frequency_hz"] for r in base]:
                raise ValueError("PCB port grids disagree")
            freq = np.array([float(r["frequency_hz"]) for r in rr]) / 1e9
            for ax, key in zip(
                axs, ["correction_gain_db", "correction_phase_unwrapped_deg"], strict=True
            ):
                # LUT is inverse transfer: negate its relative log gain/phase.
                val = np.array(
                    [float(b[key]) - float(r[key]) for b, r in zip(base, rr, strict=True)]
                )
                ax.plot(freq, val, label=port, lw=1.1)
        axs[0].set(ylabel="Relative path gain (dB)")
        axs[1].set(ylabel="Relative path phase (unwrapped °)", xlabel="Frequency (GHz)")
        axs[0].legend(ncol=7, loc="lower left")
        self.save(
            fig,
            "fig03_pcb_response",
            "Measured PCB paths contain repeatable frequency structure",
            "Relative to ANT1; inverse correction LUT converted back to transfer. "
            "Includes calibration fixture uncertainty, not antenna response.",
            ["lut"],
        )

        fig, axs = plt.subplots(1, 2, figsize=(14, 6))
        for j, (method, label) in enumerate(
            [
                ("complex_linear", "Cartesian linear"),
                ("logphase_linear", "Log/phase linear"),
                ("logphase_pchip", "Log/phase PCHIP"),
            ]
        ):
            vals = [pcb["holdout_metrics"][p][method]["phase_rms_deg"] for p in PORTS]
            axs[0].bar(
                np.arange(8) + (j - 1) * 0.25, vals, width=0.24, label=label, color=COLORS[j]
            )
        axs[0].set(xticks=range(8), xticklabels=PORTS, ylabel="Raw holdout phase RMS (°)")
        axs[0].legend(fontsize=9)
        axs[0].set_title("Independent 5–6 GHz holdouts\n80 observations per port")
        states = ("ALL_OFF",) + PORTS
        mat = np.array([[pcb["qualification_relative_db"][p][s] for s in states] for p in PORTS])
        im = axs[1].imshow(mat, vmin=-60, vmax=0, cmap="viridis", aspect="auto")
        for y in range(8):
            for x in range(9):
                axs[1].text(
                    x,
                    y,
                    f"{mat[y, x]:.0f}",
                    ha="center",
                    va="center",
                    color="black" if mat[y, x] > -25 else "white",
                    fontsize=8,
                )
        axs[1].set(
            xticks=range(9),
            xticklabels=states,
            yticks=range(8),
            yticklabels=PORTS,
            xlabel="Selected state",
            ylabel="Injected port",
            title="5.8 GHz isolation matrix (dB)",
        )
        axs[1].tick_params(axis="x", rotation=65)
        axs[1].grid(False)
        fig.colorbar(
            im, ax=axs[1], shrink=0.7, label="Relative to injected port's selected response"
        )
        self.save(
            fig,
            "fig04_pcb_holdout_and_isolation",
            "LUT holdout accuracy and leakage are separate limits",
            "Left: raw per-port holdouts, not detrended. Spatial common-mode-removed "
            "PCHIP RMS = 1.12°. Right: one-frequency fixture matrix.",
            ["pcb"],
        )

    def models(self):
        mid = self.load("midpoints")
        dense = self.load("conducted_dense")
        ids = ["log_harmonic_k0", "log_harmonic_k4", "frequency_table_log_linear"]
        labels = ["Delay only", "4 log harmonics", "100 MHz LUT"]
        vals = [
            next(m for m in mid["models"] if m["model_id"] == key)["midpoint_score"][
                "phase_rms_deg"
            ]
            for key in ids
        ]
        vals.append(
            mid["midpoint_repeatability"]["same_frequency_leave_one_sweep_out"]["phase_rms_deg"]
        )
        labels.append("Repeatability floor")
        fig, axs = plt.subplots(1, 2, figsize=(14, 6))
        bars = axs[0].bar(range(4), vals, color=[COLORS[0], COLORS[0], COLORS[0], "#8d9aa3"])
        axs[0].bar_label(bars, fmt="%.2f", padding=3)
        axs[0].set(
            xticks=range(4),
            xticklabels=labels,
            ylabel="Pooled phase RMS (°)",
            ylim=(0, 23),
            title="Five independent midpoint sweeps\nUnseen frequencies, 2.15–5.75 GHz",
        )
        axs[0].tick_params(axis="x", rotation=20)
        spacing = [5, 10, 25, 50]
        vv = [
            dense["aggregate_linear_complex_log_interpolation"][str(s)]["mean_path_phase_rms_deg"]
            for s in spacing
        ]
        bars = axs[1].bar(range(4), vv, color=COLORS[2])
        axs[1].bar_label(bars, fmt="%.3f", padding=3)
        axs[1].set(
            xticks=range(4),
            xticklabels=[f"{s} MHz" for s in spacing],
            ylabel="Mean of path phase RMS values (°)",
            ylim=(0, 4.4),
            title="One 1 MHz splitter sweep\nInterstitial interpolation, 2.1–5.8 GHz",
        )
        self.save(
            fig,
            "fig05_frequency_models",
            "Dense LUTs outperform a compact ripple explanation",
            "Older splitter-fixture evidence, NOT PCB-only calibration. Left and right "
            "have different cohorts and aggregations; do not pool.",
            ["midpoints", "conducted_dense"],
        )

    def ota(self, cohorts, summary):
        fig, axs = plt.subplots(1, 2, figsize=(13, 6), sharey=True)
        for j, tx in enumerate(("TX1", "TX2")):
            rows = [r for r in summary if r["tx_port"] == tx]
            x = np.arange(3)
            for offset, key, label, color in [
                (-0.18, "phase_admissions_variable_budget", "Phase", COLORS[0]),
                (0.18, "spatial_model_admissions", "Spatial model", COLORS[1]),
            ]:
                bars = axs[j].bar(
                    x + offset, [r[key] for r in rows], width=0.35, color=color, label=label
                )
                axs[j].bar_label(bars, padding=3)
            axs[j].set(
                xticks=x,
                xticklabels=["Original\n2 MS/s", "Jittered\n2 MS/s", "Latest\n5 MS/s"],
                ylim=(0, 200),
                title=tx,
                ylabel="Admitted frequencies / 149",
            )
            axs[j].legend(loc="upper right", fontsize=9)
        self.save(
            fig,
            "fig06_phase_vs_spatial_admission",
            "Stable phase does not establish a correct direction",
            "Dense whole-record diagnostics use variable integration. Spatial admissions "
            "are legacy model gates, NOT surveyed bearing accuracy.",
            ["ota_original", "ota_jittered", "ota_latest"],
        )

        fig, axs = plt.subplots(2, 2, figsize=(14, 9), sharex=True)
        for e, records in enumerate(cohorts):
            for col, tx in enumerate(("TX1", "TX2")):
                rows = sorted(
                    [r for r in records if r["tx_port"] == tx],
                    key=lambda r: float(r["frequency_hz"]),
                )
                f = np.array([float(r["frequency_hz"]) for r in rows]) / 1e6
                residual = [number(r["full_bearing_residual_phase_rms_deg"]) for r in rows]
                angle = [number(r["full_bearing_deg"]) for r in rows]
                axs[0, col].plot(f, residual, label=EPOCHS[e], color=COLORS[e], lw=1, alpha=0.85)
                axs[1, col].scatter(f, angle, s=8, color=COLORS[e], alpha=0.8)
                axs[0, col].set(title=tx, ylabel="Spatial phase residual (°)", ylim=(0, 135))
                axs[1, col].set(
                    ylabel="Model peak (°), including rejected fits",
                    xlabel="Frequency (MHz)",
                    ylim=(-5, 365),
                    yticks=[0, 90, 180, 270, 360],
                )
        axs[0, 0].legend(fontsize=9)
        self.save(
            fig,
            "fig07_ota_frequency_and_epoch",
            "The installed scene changes far more than the PCB holdout error",
            "Angles are diagnostic model peaks, not ground truth. Legacy dense geometry "
            "is 49.9654 mm. Rate, bandwidth, gain and epoch are confounded.",
            ["ota_original", "ota_jittered", "ota_latest"],
        )

    def static(self):
        records = self.load("static")
        fig, axs = plt.subplots(1, 2, figsize=(14, 7))
        labels = [f"{f} / {epoch}" for f in CENTRES for epoch in ("before", "after")]
        metrics = [
            ("magnitude_db", "Relative transfer magnitude (dB)", "viridis", None),
            ("phase_rms_10ms_deg", "10 ms phase RMS (°)", "magma_r", None),
        ]
        for ax, (metric, title, cmap, _) in zip(axs, metrics, strict=True):
            arr = np.array(
                [
                    [
                        float(
                            next(
                                r
                                for r in records
                                if r["epoch"] == epoch
                                and float(r["frequency_hz"]) == f * 1e6
                                and r["port"] == p
                            )[metric]
                        )
                        for p in C6
                    ]
                    for f in CENTRES
                    for epoch in ("before", "after")
                ]
            )
            im = ax.imshow(arr, aspect="auto", cmap=cmap)
            midpoint = (arr.max() + arr.min()) / 2
            for y in range(8):
                for x in range(6):
                    white = (arr[y, x] < midpoint) if cmap == "viridis" else (arr[y, x] > midpoint)
                    ax.text(
                        x,
                        y,
                        f"{arr[y, x]:.1f}",
                        ha="center",
                        va="center",
                        color="white" if white else "black",
                        fontsize=9,
                    )
            ax.set(
                xticks=range(6), xticklabels=C6, yticks=range(8), yticklabels=labels, title=title
            )
            ax.grid(False)
            fig.colorbar(im, ax=ax, shrink=0.8)
        self.save(
            fig,
            "fig08_static_port_imbalance",
            "Weak-port behavior is fixture- and frequency-dependent",
            "Before/after static references from the jitter comparison, NOT latest 5 MS/s "
            "screens. Transfer magnitude is not antenna efficiency or calibrated RF power.",
            ["static"],
        )

    def conditions(self):
        rows = self.load("conditions")
        fig, axs = plt.subplots(1, 2, figsize=(14, 7))
        rr = [(f, cfg) for f in CENTRES for cfg in ("B", "D")]
        rms = np.full((8, 5), np.nan)
        status = np.full((8, 5), np.nan)
        annotation = {}
        for y, (f, cfg) in enumerate(rr):
            for x, d in enumerate(DWELLS):
                cell = [
                    r
                    for r in rows
                    if float(r["frequency_hz"]) == f * 1e6
                    and r["configuration"] == cfg
                    and float(r["dwell_us"]) == d
                ]
                if not cell:
                    continue
                if len(cell) != 1:
                    raise ValueError("Duplicate condition")
                r = cell[0]
                rms[y, x] = number(r["base_rms_deg_median"])
                status[y, x] = int(boolean(r["clean_condition_pass"]))
                annotation[y, x] = f"{r['main_passes']}/{r['main_attempts']}"
        cm = plt.get_cmap("magma_r").copy()
        cm.set_bad("#e4e8ec")
        im = axs[0].imshow(rms, cmap=cm, norm=LogNorm(vmin=1, vmax=100), aspect="auto")
        cm2 = ListedColormap(["#f0c5bb", "#a6dbbf"])
        cm2.set_bad("#e4e8ec")
        axs[1].imshow(status, cmap=cm2, vmin=0, vmax=1, aspect="auto")
        for y in range(8):
            for x in range(5):
                axs[0].text(
                    x,
                    y,
                    f"{rms[y, x]:.1f}" if np.isfinite(rms[y, x]) else "—",
                    ha="center",
                    va="center",
                    color="white" if rms[y, x] > 12 else "black",
                )
                text = (
                    "Not tested"
                    if (y, x) not in annotation
                    else (("CLEAN" if status[y, x] else "Not clean") + "\nmain " + annotation[y, x])
                )
                axs[1].text(x, y, text, ha="center", va="center", fontsize=8)
        for ax, title in zip(
            axs,
            [
                "Median trial phase RMS (°), log color scale",
                "Full phase/gain gates, including controls",
            ],
            strict=True,
        ):
            ax.set(
                xticks=range(5),
                xticklabels=DWELLS,
                yticks=range(8),
                yticklabels=[f"{f} MHz / {c}" for f, c in rr],
                xlabel="Dwell (µs)",
                title=title,
            )
            ax.grid(False)
        fig.colorbar(im, ax=axs[0], shrink=0.7)
        self.save(
            fig,
            "fig09_latest_dwell_qualification",
            "Low phase RMS alone is not a qualification pass",
            "B = 5 MS/s / 1.6 MHz; D = 5 MS/s / 4 MHz. 50 ms prediction windows. "
            "Missing metrics remain missing; none is a qualified bearing condition.",
            ["conditions"],
        )

    def ideal(self, diagnostic, spherical):
        fig, axs = plt.subplots(1, 2, figsize=(14, 6))
        grid = np.arange(0, 360, 0.25)
        g = geometry()
        for i, f in enumerate(CENTRES[:3]):
            m = far_field_steering(g, f * 1e6, grid)
            fit = solve_bearing(m[360], m, grid)
            axs[0].plot(grid, fit.likelihood, label=f"{f} MHz", color=COLORS[i])
        axs[0].axvspan(70, 110, color="#aab9c8", alpha=0.2, label="±20° exclusion")
        axs[0].set(
            xlabel="Candidate bearing (°)",
            ylabel="Normalized matched score",
            title="True bearing = 90°, no noise, exact array model",
            xlim=(0, 360),
        )
        axs[0].legend(fontsize=9)
        bars = axs[1].bar(
            range(4),
            [r["ambiguity_margin_db"] for r in diagnostic],
            color=[COLORS[2] if r["legacy_gate_valid"] else COLORS[1] for r in diagnostic],
        )
        axs[1].bar_label(bars, fmt="%.3f", padding=3)
        axs[1].axhline(1, color="#a23830", ls="--", label="Legacy 1 dB gate")
        axs[1].set(
            xticks=range(4),
            xticklabels=CENTRES,
            xlabel="Frequency (MHz)",
            ylabel="Best / best outside ±20° (dB)",
            ylim=(0, 3.2),
            title="915 and 2475 MHz falsely rejected",
        )
        axs[1].legend()
        self.save(
            fig,
            "fig10_ideal_bearing_gate",
            "A broad main lobe is not a competing emitter direction",
            "SIMULATION: nominal 51 mm C6, equal weights, isotropic antennas. "
            "Zero residual and exact angle at all four frequencies; no hardware data.",
            ["bearing_code", "manifold_code", "geometry_code"],
            "ideal simulation",
        )

        fig, axs = plt.subplots(1, 2, figsize=(14, 6))
        for i, f in enumerate(CENTRES[:3]):
            rows = [r for r in spherical if r["frequency_mhz"] == f]
            axs[0].plot(
                [r["range_m"] for r in rows],
                [r["plane_wave_phase_residual_deg"] for r in rows],
                "o-",
                label=f"{f} MHz",
                color=COLORS[i],
            )
        axs[0].set(
            xlabel="Ideal source distance (m)",
            ylabel="Plane-wave fit phase residual (°)",
            title="Spherical propagation alone, true bearing 90°",
            xscale="log",
        )
        axs[0].set_xticks([0.2, 0.3, 0.5, 1, 1.5, 3], ["0.2", "0.3", "0.5", "1", "1.5", "3"])
        axs[0].xaxis.set_minor_locator(NullLocator())
        axs[0].legend()
        bars = axs[1].bar(
            range(4), [r["diameter_wavelengths"] for r in diagnostic], color=COLORS[0]
        )
        axs[1].bar_label(bars, fmt="%.3f", padding=3)
        axs[1].set(
            xticks=range(4),
            xticklabels=CENTRES,
            xlabel="Frequency (MHz)",
            ylabel="51 mm diameter / wavelength",
            ylim=(0, 1.2),
            title="The same physical array has different angular sensitivity",
        )
        self.save(
            fig,
            "fig11_aperture_and_range",
            "Neither close range nor small aperture is a complete error diagnosis",
            "SIMULATION / GEOMETRY: ideal point antennas, no coupling or reflections. "
            "Not a prediction of actual antenna phase centres or production accuracy.",
            ["bearing_code", "manifold_code", "geometry_code"],
            "ideal simulation",
        )

    def timing(self, schedule):
        rows = self.load("rolling")
        fig, axs = plt.subplots(1, 2, figsize=(14, 6))
        x = np.arange(5)
        bars = axs[0].bar(
            x, [r["retained_per_port_per_50ms_ms"] for r in schedule], color=COLORS[0]
        )
        axs[0].bar_label(bars, fmt="%.2f ms", padding=3)
        axs[0].set(
            xticks=x,
            xticklabels=DWELLS,
            xlabel="Dwell (µs)",
            ylim=(0, 9.5),
            ylabel="Useful integration per port / 50 ms (ms)",
            title="Nominal schedule, 5 µs trimmed at each edge",
        )
        for cfg, offset, color in [
            ("A", -0.14, "#87939e"),
            ("B", 0, COLORS[0]),
            ("D", 0.14, COLORS[1]),
        ]:
            first = True
            for j, d in enumerate(DWELLS):
                vals = [
                    number(r["p95_compute_ms"])
                    for r in rows
                    if r["configuration"] == cfg
                    and float(r["dwell_us"]) == d
                    and r["p95_compute_ms"]
                ]
                if vals:
                    jitter = np.linspace(-0.045, 0.045, len(vals))
                    axs[1].scatter(
                        j + offset + jitter,
                        vals,
                        s=12,
                        color=color,
                        alpha=0.6,
                        label=cfg if first else None,
                    )
                    first = False
        axs[1].axhline(50, color="#a23830", ls="--", label="50 ms output-cadence budget")
        axs[1].set(
            xticks=x,
            xticklabels=DWELLS,
            xlabel="Dwell (µs)",
            yscale="log",
            ylabel="Per-record p95 replay compute (ms)",
            title="Latest campaign, all reported records",
        )
        axs[1].legend(fontsize=9)
        self.save(
            fig,
            "fig12_dwell_and_compute",
            "More revisits do not guarantee more useful information or faster output",
            "Left: schedule arithmetic, not measured latency. Right: shared-host replay, "
            "not isolated live throughput; A is 2 MS/s control, B/D are 5 MS/s.",
            ["rolling", "report:completed_switching_analysis"],
            "schedule calculation / measured replay",
        )

    def audit(self):
        a = self.load("audit")
        coverage = a["rolling_analysis_coverage"]
        tr = a["transport"]
        fig, axs = plt.subplots(1, 2, figsize=(13, 6))
        vals = [coverage[k] for k in ("analyzed_windows", "failed_windows", "unattempted_windows")]
        bars = axs[0].bar(range(3), vals, color=[COLORS[0], COLORS[1], "#9dabb4"])
        axs[0].bar_label(bars, padding=4)
        axs[0].set(
            xticks=range(3),
            xticklabels=["Analyzed", "Timing failed", "Not attempted"],
            ylabel="50 ms prediction windows",
            ylim=(0, 10000),
            title=f"{coverage['expected_windows']:,} planned rolling outputs",
        )
        vals = [
            tr["complete_requests"] - tr["recovered_requests"],
            tr["recovered_requests"],
            tr["failed_requests"],
        ]
        bars = axs[1].bar(range(3), vals, color=[COLORS[0], COLORS[1], "#9dabb4"])
        axs[1].bar_label(bars, padding=4)
        axs[1].set(
            xticks=range(3),
            xticklabels=["First attempt", "Recovered", "Unrecovered"],
            ylabel="Capture requests",
            ylim=(0, 500),
            title="Latest continuation transport only",
        )
        self.save(
            fig,
            "fig13_failure_accounting",
            "Successful analysis and reliable delivery are different claims",
            "Analyzed does not mean phase/bearing passed. Transport: 458 attempts for "
            "448 requests, 10 failures retained. Earlier interruptions are separate.",
            ["audit"],
        )

    def roadmap(self):
        fig, ax = plt.subplots(figsize=(14, 8))
        ax.axis("off")
        columns = ["Mechanism", "Fixed correction?", "What establishes it?", "First action"]
        rows = [
            [
                "PCB frequency ripple",
                "Yes, within validated scope",
                "Interstitial + later holdouts",
                "Keep complex LUT",
            ],
            [
                "Final cable differences",
                "Yes, if routing is stable",
                "Cable-tip injection; reconnects",
                "Label and immobilize cables",
            ],
            [
                "Antenna / mounting / coupling",
                "Empirical angular response",
                "Held-out angles and positions",
                "Survey installed array",
            ],
            [
                "Changing room multipath",
                "Not one portable LUT",
                "Rigid-pair translations",
                "Low-reflection reference test",
            ],
            [
                "Weak signal / unequal SNR",
                "Gain yes; lost SNR no",
                "Per-port noise and closure",
                "Fix weakest useful baselines",
            ],
            [
                "Timing / predecessor transient",
                "Not a static phase offset",
                "Sample-aligned edges + order tests",
                "Independent synchronization",
            ],
            [
                "Low-band ambiguity gate",
                "Software, not RF correction",
                "Ideal and angular holdout tests",
                "Distinct peaks + uncertainty",
            ],
            [
                "Transport / runtime",
                "Not calibratable",
                "Counters, live p95/p99 latency",
                "Incremental timing state",
            ],
        ]
        table = ax.table(
            cellText=rows,
            colLabels=columns,
            cellLoc="left",
            loc="center",
            colWidths=[0.23, 0.23, 0.29, 0.25],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 2.9)
        for (row, _), cell in table.get_celld().items():
            cell.set_edgecolor("white")
            cell.set_facecolor("#dbeaf1" if row == 0 else ("#f1f5f8" if row % 2 else "#ffffff"))
        self.save(
            fig,
            "fig14_calibratable_vs_dynamic",
            "Fix deterministic response; measure the changing environment",
            "SYNTHESIS / PROPOSED ACTIONS: ranked priorities, not measured percentages "
            "of a common error budget.",
            [
                "report:pcb_direct_injection_calibration",
                "report:completed_switching_analysis",
                "report:full_5ms_campaign",
            ],
            "engineering synthesis",
        )

        fig, ax = plt.subplots(figsize=(14, 9))
        ax.set(xlim=(0, 10), ylim=(0, 10))
        ax.axis("off")
        stages = [
            (
                "1  Freeze the fixture",
                "5800 MHz; C6; fixed labeled cables; surveyed source / array",
                "Exit: static reference repeats close before any speed comparison",
            ),
            (
                "2  Establish correct directions",
                "Static + 200 µs switched measurements at four known angles",
                "Exit: separate static manifold error from switched timing error",
            ),
            (
                "3  Isolate timing",
                "Sample-aligned selector marker; forward / reverse / permuted order",
                "Exit: correct port labels; quantify edge and predecessor dependence",
            ),
            (
                "4  Calibrate the installed array",
                "Cable-tip closure; train angles; untouched intermediate angles",
                "Exit: accuracy, coverage and uncertainty hold in a second position",
            ),
            (
                "5  Remove the laboratory reference assumption",
                "OTA RX1 reference; unknown tone, then modulation / bursts",
                "Exit: maintain coherence and reject unlocks without a conducted pilot",
            ),
            (
                "6  Optimize, then expand frequencies",
                "Incremental estimator; live latency; motion, power and temperature",
                "Exit: frozen acceptance gates passed on held-out deployment conditions",
            ),
        ]
        for i, (title, action, gate) in enumerate(stages):
            y = 8.65 - i * 1.5
            ax.add_patch(
                FancyBboxPatch(
                    (0.25, y),
                    9.5,
                    1.18,
                    boxstyle="round,pad=.05",
                    facecolor="#edf4f7",
                    edgecolor="#95afbe",
                )
            )
            ax.text(0.5, y + 0.9, title, weight="bold", fontsize=12)
            ax.text(0.5, y + 0.53, action, fontsize=11)
            ax.text(0.5, y + 0.16, gate, fontsize=10, color="#436255")
            if i < 5:
                ax.annotate("", xy=(5, y - 0.28), xytext=(5, y), arrowprops={"arrowstyle": "->"})
        self.save(
            fig,
            "fig15_experiment_roadmap",
            "Next experiments should isolate causes before increasing data volume",
            "PROPOSED, not executed. New RF work requires a confirmed fixture and "
            "bounded acquisition plan. No PCB redesign is justified by current attribution.",
            ["report:tracking_development_plan", "report:full_5ms_campaign"],
            "proposed experiments",
        )

        fig, ax = plt.subplots(figsize=(14, 7))
        ax.axis("off")
        rows = [
            [
                "915 MHz",
                "Measured; lower-band holdouts open",
                "D / 200 µs; 1.16°",
                "No",
                "Low-band control; small aperture",
            ],
            [
                "2475 MHz",
                "Measured; lower-band holdouts open",
                "No clean latest condition",
                "No",
                "Resolve failed brackets / controls",
            ],
            [
                "5800 MHz",
                "5–6 GHz holdouts available",
                "B / 200 µs; 7.68°",
                "No",
                "First surveyed-angle experiment",
            ],
            [
                "5800 MHz, faster",
                "Same PCB calibration",
                "D / 100 µs; 9.90°",
                "No",
                "Limited phase margin; validate later",
            ],
            [
                "5811 MHz",
                "5–6 GHz holdouts available",
                "No clean latest condition",
                "No",
                "Keep failed controls in denominator",
            ],
            [
                "5726–5874 MHz",
                "Within high-band holdouts",
                "Dense variable-budget only",
                "No",
                "Not a qualified contiguous band",
            ],
            [
                "Other 0.5–6 GHz",
                "PCB knots only / varying holdouts",
                "No blanket claim",
                "No",
                "Qualify antennas and runtime by band",
            ],
        ]
        table = ax.table(
            cellText=rows,
            colLabels=[
                "Frequency",
                "PCB evidence",
                "Latest phase result",
                "Production DF?",
                "Next use",
            ],
            cellLoc="left",
            loc="center",
            colWidths=[0.15, 0.28, 0.22, 0.11, 0.24],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 3.0)
        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("white")
            cell.set_facecolor("#dbeaf1" if row == 0 else ("#f7e6df" if col == 3 else "#f1f5f8"))
        self.save(
            fig,
            "fig16_frequency_readiness",
            "Laboratory operating points are not a production tracking release",
            "Phase RMS is NOT bearing error. B = 5 MS/s / 1.6 MHz; D = 5 MS/s / 4 MHz. "
            "No surveyed-angle, unknown-emitter, live operating envelope is qualified.",
            ["conditions", "pcb", "ota_latest"],
            "evidence-based readiness assessment",
        )

    def run(self):
        cohorts = [self.load(k) for k in ("ota_original", "ota_jittered", "ota_latest")]
        summary = dense_summary(cohorts)
        schedule = schedule_metrics()
        ideal, spherical = ideal_diagnostics()
        write_csv(self.data / "dense-epoch-summary.csv", summary)
        write_csv(self.data / "schedule-theory.csv", schedule)
        write_csv(self.data / "ideal-bearing-gate.csv", ideal)
        write_csv(self.data / "ideal-spherical-source.csv", spherical)
        write_csv(self.data / "latest-conditions.csv", self.load("conditions"))
        self.overview()
        self.fixture()
        self.pcb()
        self.models()
        self.ota(cohorts, summary)
        self.static()
        self.conditions()
        self.ideal(ideal, spherical)
        self.timing(schedule)
        self.audit()
        self.roadmap()
        stats = {
            "scope": "Offline synthesis of normalized results; no new raw-IQ replay or RF work",
            "data_through": "2026-09-11",
            "dense_summary": summary,
            "clean_latest_phase_conditions": sum(
                boolean(r["clean_condition_pass"]) for r in self.load("conditions")
            ),
            "clean_latest_joint_conditions": sum(
                boolean(r["clean_phase_and_bearing"]) for r in self.load("conditions")
            ),
            "production_angular_accuracy_qualified": False,
            "pcb_spatial_holdout": self.load("pcb")["holdout_spatial_phase"],
            "latest_window_accounting": {
                k: v
                for k, v in self.load("audit")["rolling_analysis_coverage"].items()
                if k != "inputs"
            },
        }
        (self.data / "summary.json").write_text(json.dumps(stats, indent=2, allow_nan=False) + "\n")
        # Detect concurrent input changes rather than publishing a mixed snapshot.
        for entry in self.inputs.values():
            if sha(ROOT / entry["path"]) != entry["sha256"]:
                raise ValueError(f"Source changed during rendering: {entry['path']}")
        manifest = {
            "schema": "smateway.calibration-synthesis/v1",
            "data_through": "2026-09-11",
            "scope": stats["scope"],
            "inputs": self.inputs,
            "renderer": {
                "path": str(Path(__file__).relative_to(ROOT)),
                "sha256": sha(Path(__file__)),
            },
            "figures": self.figures,
            "tables": [
                {"path": str(p.relative_to(self.output)), "sha256": sha(p)}
                for p in sorted(self.data.iterdir())
                if p.name != "manifest.json"
            ],
        }
        report = self.output / "README.md"
        if report.exists():
            manifest["report"] = {"path": "README.md", "sha256": sha(report)}
        (self.data / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    stats = Renderer(args.output).run()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "figures": 16,
                "clean_phase_conditions": stats["clean_latest_phase_conditions"],
                "joint_conditions": stats["clean_latest_joint_conditions"],
            }
        )
    )


if __name__ == "__main__":
    main()
