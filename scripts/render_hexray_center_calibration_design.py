#!/usr/bin/env python3
"""Render deterministic figures for the HexRay TX-in-middle calibration design."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle, Wedge

REPOSITORY = Path(__file__).resolve().parents[1]
REPORT_DIRECTORY = REPOSITORY / "docs/hexray_tx_in_middle_calibration"
DEFAULT_SNAPSHOT = REPORT_DIRECTORY / "data/design-snapshot.json"
DEFAULT_OUTPUT_DIRECTORY = REPORT_DIRECTORY / "png"
DEFAULT_MANIFEST = REPORT_DIRECTORY / "data/figures-manifest.json"
FIGURE_NAMES = (
    "fig01_geometry_and_wavelengths.png",
    "fig02_high_rate_timing_and_capture_plan.png",
    "fig03_signal_chain_and_identifiability.png",
    "fig04_expected_phasors_and_acceptance.png",
    "fig05_rf_only_timing_qualification.png",
)

INK = "#17212b"
MUTED = "#5d6b78"
GRID = "#d9e0e6"
BLUE = "#1769aa"
TEAL = "#16877c"
AMBER = "#d99100"
RED = "#c73b3b"
PURPLE = "#7651a6"
PAPER = "#fbfcfd"
PALE_BLUE = "#e9f2f8"
PALE_TEAL = "#e9f5f3"
PALE_AMBER = "#fff4dc"


class DesignReportError(RuntimeError):
    """The compact design snapshot is malformed or rendered outputs differ."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--check",
        action="store_true",
        help="render temporarily and byte-compare the committed PNGs and manifest",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DesignReportError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DesignReportError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DesignReportError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise DesignReportError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DesignReportError(f"{label} must be an integer")
    return value


def _close(first: float, second: float, tolerance: float = 1e-6) -> bool:
    return abs(first - second) <= tolerance * max(1.0, abs(first), abs(second))


def load_snapshot(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DesignReportError(f"cannot load snapshot {path}: {error}") from error
    root = _mapping(document, "snapshot")
    if root.get("schema") != 1:
        raise DesignReportError("snapshot schema must be 1")
    if root.get("design_kind") != "hexray_tx1_center_high_rate_complex_calibration":
        raise DesignReportError("snapshot has the wrong design kind")
    expected_status = "pre-execution design; contains no measured calibration result"
    if root.get("evidence_status") != expected_status:
        raise DesignReportError("snapshot must remain explicitly pre-execution")

    geometry = _mapping(root.get("geometry"), "geometry")
    antennas = [
        _mapping(item, "antenna") for item in _sequence(geometry.get("antennas"), "antennas")
    ]
    if [item.get("name") for item in antennas] != [f"ANT{index}" for index in range(1, 7)]:
        raise DesignReportError("geometry must contain ordered ANT1 through ANT6")
    radius = _number(geometry.get("radius_mm"), "radius")
    diameter = _number(geometry.get("phase_center_circle_diameter_mm"), "diameter")
    if not _close(2.0 * radius, diameter):
        raise DesignReportError("radius and phase-centre diameter disagree")
    for antenna in antennas:
        position = _sequence(antenna.get("position_mm"), f"{antenna.get('name')} position")
        if len(position) != 2:
            raise DesignReportError("every antenna position must contain x and y")
        distance = math.hypot(
            _number(position[0], "antenna x"),
            _number(position[1], "antenna y"),
        )
        if not _close(distance, radius, tolerance=2e-7):
            raise DesignReportError(f"{antenna.get('name')} is not on the declared circle")

    schedule = _mapping(root.get("schedule"), "schedule")
    if schedule.get("profile_id") != "hexcal-v1":
        raise DesignReportError("schedule must use the implemented hexcal-v1 profile")
    states = _sequence(schedule.get("active_states"), "active states")
    if tuple(states) != tuple(f"ANT{index}" for index in range(1, 7)):
        raise DesignReportError("schedule must select only ANT1 through ANT6 in order")
    marker = _integer(schedule.get("marker_body_us"), "marker body")
    guard = _integer(schedule.get("pre_state_all_off_us"), "guard")
    dwell = _integer(schedule.get("active_dwell_us"), "active dwell")
    cycle = _integer(schedule.get("cycle_us"), "cycle")
    if marker + len(states) * (guard + dwell) != cycle:
        raise DesignReportError("schedule components do not add to the declared cycle")
    if guard <= _number(schedule.get("switch_settling_max_us"), "switch settling"):
        raise DesignReportError("ALL_OFF guard does not exceed switch settling")
    expected_scans = 1_000_000.0 / cycle
    if not _close(expected_scans, _number(schedule.get("scans_per_second"), "scan rate")):
        raise DesignReportError("cycle and scan rate disagree")

    plan = _mapping(root.get("capture_plan"), "capture plan")
    if "coarse schedule" not in str(plan.get("purpose")):
        raise DesignReportError("1 MS/s plan must remain scoped to coarse schedule decoding")
    if "not the evidence" not in str(plan.get("timing_evidence_limit")):
        raise DesignReportError("1 MS/s plan must disclaim microsecond timing evidence")
    gain = _mapping(plan.get("rx_gain_qualification"), "RX gain qualification")
    if gain.get("mode") != "manual" or gain.get("agc_allowed") is not False:
        raise DesignReportError("RX gain qualification must remain manual with AGC forbidden")
    if gain.get("selected_gain_db") is not None:
        raise DesignReportError("pre-execution snapshot must not claim a selected RX gain")
    if "lowest conservative" not in str(gain.get("initial_setting")):
        raise DesignReportError("RX gain qualification must begin conservatively")
    rounds = _integer(plan.get("rounds"), "round count")
    conditions = _integer(plan.get("conditions_per_round"), "conditions per round")
    if rounds * conditions != _integer(plan.get("planned_artifacts"), "planned artifacts"):
        raise DesignReportError("capture-plan artifact count is inconsistent")
    frequency = _mapping(root.get("frequency_plan"), "frequency plan")
    centers = tuple(
        _integer(value, "center frequency")
        for value in _sequence(frequency.get("center_frequencies_hz"), "center frequencies")
    )
    if 2_400_000_000 not in centers or 5_800_000_000 not in centers:
        raise DesignReportError("frequency plan must retain 2.4 and exact experimental 5.8 GHz")

    routes = _mapping(root.get("pcb_route_priors"), "PCB route priors")
    route_rows = [
        _mapping(item, "route row") for item in _sequence(routes.get("rows"), "route rows")
    ]
    if [item.get("name") for item in route_rows] != [f"ANT{index}" for index in range(1, 7)]:
        raise DesignReportError("route priors must contain ordered ANT1 through ANT6")
    model = _mapping(root.get("calibration_model"), "calibration model")
    if model.get("measurement_equation") != "H_i(f) = C_i(f) * A_i(f)":
        raise DesignReportError("calibration product equation changed")
    if "only the product" not in str(model.get("identifiability")):
        raise DesignReportError("snapshot no longer states the gauge ambiguity")

    qualification = _mapping(
        root.get("timing_and_gpio_qualification"),
        "timing and GPIO qualification",
    )
    fallback = _mapping(qualification.get("fallback_path"), "fallback evidence path")
    if "timing only" not in str(fallback.get("independently_observed")):
        raise DesignReportError("RF fallback must remain limited to timing-only evidence")
    if "not independently" not in str(fallback.get("not_independently_observed")):
        raise DesignReportError("RF fallback must label its GPIO evidence boundary")
    if fallback.get("selected") is not True:
        raise DesignReportError("paired RF fallback must be marked selected")

    rf_timing = _mapping(root.get("rf_timing_qualification"), "RF timing qualification")
    if not str(rf_timing.get("status")).startswith("selected pre-execution"):
        raise DesignReportError("RF timing qualification must remain explicitly pre-execution")
    timing_capture = _mapping(rf_timing.get("capture_contract"), "RF timing capture")
    exact_integer_fields = {
        "captures_per_tested_band": 2,
        "duration_ms_per_capture": 450,
        "sample_rate_hz": 2_000_000,
        "rf_bandwidth_hz": 1_600_000,
        "frames_per_capture": 9,
        "samples_per_frame": 100_000,
        "samples_per_capture": 900_000,
        "kernel_buffer_count": 8,
        "metadata_abi": 2,
    }
    for field, expected in exact_integer_fields.items():
        if _integer(timing_capture.get(field), f"RF timing {field}") != expected:
            raise DesignReportError(f"RF timing {field} changed from {expected}")
    if not _close(_number(timing_capture.get("native_sample_period_us"), "sample period"), 0.5):
        raise DesignReportError("2 MS/s timing capture must retain the 0.5 us sample period")
    expected_samples = int(
        _number(timing_capture["duration_ms_per_capture"], "duration")
        * _number(timing_capture["sample_rate_hz"], "sample rate")
        / 1_000.0
    )
    if expected_samples != timing_capture["samples_per_capture"]:
        raise DesignReportError("RF timing duration and sample count disagree")
    framed_samples = timing_capture["frames_per_capture"] * timing_capture["samples_per_frame"]
    if framed_samples != timing_capture["samples_per_capture"]:
        raise DesignReportError("RF timing frame and sample counts disagree")
    if timing_capture.get("experimental_5g8_opt_in_required") is not True:
        raise DesignReportError("RF timing must retain explicit 5.8 GHz opt-in")
    timing_detector = _mapping(rf_timing.get("detector"), "RF timing detector")
    if _integer(timing_detector.get("coherent_samples_per_bin"), "coherent bin") != 2:
        raise DesignReportError("RF timing detector must coherently combine two samples")
    if not _close(_number(timing_detector.get("complex_bin_duration_us"), "bin duration"), 1.0):
        raise DesignReportError("RF timing detector must retain one-microsecond bins")
    thresholds = tuple(
        _number(value, "RF timing threshold")
        for value in _sequence(timing_detector.get("threshold_sweep_q"), "threshold sweep")
    )
    if thresholds != (0.4, 0.5, 0.6):
        raise DesignReportError("RF timing q threshold sweep changed")
    if "two-mean complex changepoint" not in str(timing_detector.get("independent_estimator")):
        raise DesignReportError("RF timing independent changepoint is missing")
    timing_gates = _mapping(rf_timing.get("frozen_gates"), "RF timing gates")
    if _integer(timing_gates.get("minimum_complete_cycles_per_capture"), "cycles") != 290:
        raise DesignReportError("RF timing minimum cycle gate changed")
    if not _close(
        _number(timing_gates.get("minimum_decoded_cycle_fraction"), "decode fraction"),
        0.98,
    ):
        raise DesignReportError("RF timing decoded-cycle gate changed")
    if _integer(timing_gates.get("visible_edges_per_accepted_cycle"), "visible edges") != 12:
        raise DesignReportError("RF timing must require twelve visible edges per cycle")
    if _number(timing_gates.get("maximum_q40_q60_edge_span_us"), "q span") != 1.5:
        raise DesignReportError("RF timing q-sweep uncertainty gate changed")
    if _number(timing_gates.get("maximum_independent_estimator_delta_us"), "edge delta") != 1.5:
        raise DesignReportError("RF timing independent-estimator gate changed")
    limitations = tuple(
        str(value) for value in _sequence(rf_timing.get("rf_only_limitations"), "RF limits")
    )
    if not any("cannot separate" in item for item in limitations):
        raise DesignReportError("RF timing must disclose the combined marker limitation")
    if not any("not independently" in item for item in limitations):
        raise DesignReportError("RF timing must disclose its GPIO evidence boundary")

    sources = [
        _mapping(item, "source")
        for item in _sequence(root.get("source_documents"), "source documents")
    ]
    if len(sources) < 4:
        raise DesignReportError("snapshot must retain its source-hash inventory")
    for source in sources:
        digest = source.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise DesignReportError("every source must contain a SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in digest):
            raise DesignReportError("source SHA-256 must be lowercase hexadecimal")
    return root


def _configure_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": PAPER,
            "axes.facecolor": "white",
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "text.color": INK,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "grid.color": GRID,
            "grid.alpha": 0.75,
            "legend.frameon": False,
            "savefig.facecolor": PAPER,
        }
    )


def _title(fig: Figure, title: str, subtitle: str) -> None:
    fig.suptitle(title, x=0.055, y=0.975, ha="left", fontsize=20, fontweight="bold")
    fig.text(0.055, 0.925, subtitle, color=MUTED, fontsize=10.5, ha="left")


def _save(fig: Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        fig.savefig(
            temporary,
            dpi=160,
            format="png",
            metadata={
                "Software": "smateway deterministic HexRay calibration design",
                "Creation Time": None,
            },
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        plt.close(fig)


def _card(
    ax: Axes,
    x: float,
    y: float,
    width: float,
    height: float,
    heading: str,
    body: str,
    color: str,
) -> None:
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.014,rounding_size=0.018",
        linewidth=1.35,
        edgecolor=color,
        facecolor="white",
        transform=ax.transAxes,
    )
    ax.add_patch(patch)
    ax.text(
        x + 0.025,
        y + height - 0.045,
        heading,
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=10.5,
        fontweight="bold",
        color=color,
    )
    ax.text(
        x + 0.025,
        y + height - 0.095,
        textwrap.fill(body, width=max(24, int(width * 78))),
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.8,
        color=INK,
        linespacing=1.35,
    )


def _render_geometry(snapshot: Mapping[str, Any], path: Path) -> None:
    geometry = _mapping(snapshot["geometry"], "geometry")
    frequency = _mapping(snapshot["frequency_plan"], "frequency plan")
    rows = [
        _mapping(item, "wavelength row")
        for item in _sequence(frequency["wavelength_rows"], "wavelength rows")
    ]
    antennas = [_mapping(item, "antenna") for item in _sequence(geometry["antennas"], "antennas")]

    fig = plt.figure(figsize=(14, 8.4), constrained_layout=False)
    _title(
        fig,
        "HexRay centre calibration — geometry and RF scale",
        "Six receive phase centres lie on a 51 mm circle; ANT1 is forward and numbering "
        "continues clockwise around centered TX1.",
    )
    grid = fig.add_gridspec(
        1,
        2,
        left=0.055,
        right=0.97,
        bottom=0.08,
        top=0.86,
        width_ratios=(1.02, 0.98),
        wspace=0.22,
    )

    ax = fig.add_subplot(grid[0, 0])
    radius = _number(geometry["radius_mm"], "radius")
    ring = Circle((0, 0), radius, facecolor=PALE_BLUE, edgecolor=BLUE, linewidth=2.0)
    ax.add_patch(ring)
    for index, antenna in enumerate(antennas):
        position = np.asarray(_sequence(antenna["position_mm"], "position"), dtype=float)
        color = BLUE if index == 0 else TEAL
        ax.scatter(position[0], position[1], s=230, color=color, edgecolor="white", zorder=4)
        ax.text(
            position[0],
            position[1],
            str(index + 1),
            color="white",
            ha="center",
            va="center",
            fontweight="bold",
            zorder=5,
        )
        label_offset = 5.2 * position / radius
        ax.text(
            position[0] + label_offset[0],
            position[1] + label_offset[1],
            str(antenna["name"]),
            ha="center",
            va="center",
            fontsize=9,
            fontweight="bold",
            color=color,
        )
    ax.scatter(0, 0, s=280, marker="*", color=AMBER, edgecolor=INK, linewidth=0.8, zorder=6)
    ax.text(0, -5.4, "TX1\ncentre", ha="center", va="top", fontweight="bold", color=AMBER)
    ax.annotate(
        "25.5 mm radius",
        xy=(0, radius),
        xytext=(-17, 7),
        arrowprops={"arrowstyle": "<->", "color": MUTED, "lw": 1.4},
        color=MUTED,
        ha="center",
    )
    ax.annotate(
        "forward",
        xy=(0, 41),
        xytext=(0, 33),
        arrowprops={"arrowstyle": "->", "color": BLUE, "lw": 1.8},
        color=BLUE,
        ha="center",
        fontweight="bold",
    )
    clockwise = Wedge((0, 0), 39, -20, 58, width=0.1, facecolor="none", edgecolor=PURPLE)
    ax.add_patch(clockwise)
    ax.annotate(
        "clockwise",
        xy=(32, 19),
        xytext=(35, 31),
        arrowprops={"arrowstyle": "->", "color": PURPLE, "lw": 1.4},
        color=PURPLE,
        fontweight="bold",
    )
    ax.set_xlim(-47, 47)
    ax.set_ylim(-44, 47)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("x (mm, right)")
    ax.set_ylabel("y (mm, forward)")
    ax.set_title("User-confirmed top-view convention")
    ax.grid(True)

    facts = fig.add_subplot(grid[0, 1])
    facts.set_axis_off()
    facts.text(0.0, 0.98, "Why both bands matter", fontsize=13, fontweight="bold", va="top")
    band_colors = [BLUE, PURPLE]
    for index, (row, color) in enumerate(zip(rows, band_colors, strict=True)):
        y = 0.77 - index * 0.34
        ghz = _number(row["center_frequency_hz"], "frequency") / 1e9
        _card(
            facts,
            0.0,
            y,
            0.98,
            0.27,
            f"{ghz:.1f} GHz · λ₀ = {_number(row['free_space_wavelength_mm'], 'wavelength'):.1f} mm",
            (
                "Adjacent spacing = "
                f"{_number(row['adjacent_spacing_wavelengths'], 'spacing'):.3f} λ; "
                f"diameter = {_number(row['diameter_wavelengths'], 'diameter ratio'):.3f} λ. "
                f"At 25.5 mm range the 51 mm-aperture heuristic classifies the source as "
                f"{row['center_regime']}."
            ),
            color,
        )
    facts.text(0.0, 0.38, "Calibration consequences", fontsize=13, fontweight="bold")
    bullets = [
        "At 2.4 GHz, 0.204 λ spacing avoids spatial alias pressure but invites strong coupling.",
        "At 5.8 GHz, adjacent spacing is 0.493 λ — only 0.344 mm below λ/2.",
        "Near field does not break ideal C6 symmetry; geometry, polarization and coupling do.",
        "A 1 mm source offset creates ≈5.76° opposite-pair phase at 2.4 GHz and "
        "≈13.93° at 5.8 GHz.",
    ]
    for index, bullet in enumerate(bullets):
        y = 0.31 - index * 0.075
        facts.scatter(
            [0.018],
            [y + 0.012],
            s=32,
            color=[BLUE, PURPLE, TEAL, AMBER][index],
            transform=facts.transAxes,
        )
        facts.text(
            0.055,
            y,
            textwrap.fill(bullet, width=75),
            transform=facts.transAxes,
            fontsize=9.2,
            va="top",
        )
    _save(fig, path)


def _render_timing(snapshot: Mapping[str, Any], path: Path) -> None:
    schedule = _mapping(snapshot["schedule"], "schedule")
    plan = _mapping(snapshot["capture_plan"], "capture plan")
    frequency = _mapping(snapshot["frequency_plan"], "frequency plan")
    qualification = _mapping(
        snapshot["timing_and_gpio_qualification"],
        "timing and GPIO qualification",
    )

    fig = plt.figure(figsize=(14, 8.3), constrained_layout=False)
    _title(
        fig,
        "Selector frame and 1 MS/s complex-calibration matrix",
        f"{schedule['profile_id']}: the calibration decoder uses the marker and null grammar "
        "for coarse alignment; a separate stable 2 MS/s pair qualifies microsecond timing.",
    )
    grid = fig.add_gridspec(
        3,
        1,
        left=0.065,
        right=0.97,
        bottom=0.075,
        top=0.855,
        height_ratios=(1.0, 0.72, 0.92),
        hspace=0.45,
    )

    frame = fig.add_subplot(grid[0, 0])
    marker = _integer(schedule["marker_body_us"], "marker")
    guard = _integer(schedule["pre_state_all_off_us"], "guard")
    dwell = _integer(schedule["active_dwell_us"], "dwell")
    position = 0
    frame.barh(0, marker, left=position, height=0.56, color=INK)
    frame.text(position + marker / 2, 0, "marker\n180 µs", color="white", ha="center", va="center")
    position += marker
    active_colors = [BLUE, TEAL, PURPLE, AMBER, "#9a6b53", "#3f7d59"]
    for index, color in enumerate(active_colors, start=1):
        frame.barh(0, guard, left=position, height=0.56, color=RED)
        position += guard
        frame.barh(0, dwell, left=position, height=0.56, color=color)
        frame.text(
            position + dwell / 2,
            0,
            f"ANT{index}\n200 µs",
            color="white",
            ha="center",
            va="center",
            fontsize=8.5,
            fontweight="bold",
        )
        position += dwell
    frame.set_xlim(0, _integer(schedule["cycle_us"], "cycle"))
    frame.set_yticks([])
    frame.set_xlabel("frame time (µs)")
    frame.set_title("One 1,500 µs frame · red slots are 20 µs ALL_OFF")
    frame.grid(axis="x")
    frame.spines[["top", "right", "left"]].set_visible(False)

    zoom = fig.add_subplot(grid[1, 0])
    trim = _integer(schedule["analysis_edge_trim_us"], "edge trim")
    segments = [
        ("edge trim", trim, PALE_AMBER, AMBER),
        ("admitted active", dwell - 2 * trim, PALE_TEAL, TEAL),
        ("edge trim", trim, PALE_AMBER, AMBER),
        ("ALL_OFF trim", trim, "#fdeaea", RED),
        ("admitted null", guard - 2 * trim, "#f8dede", RED),
        ("ALL_OFF trim", trim, "#fdeaea", RED),
    ]
    position = 0
    for label, width, face, edge in segments:
        zoom.barh(0, width, left=position, height=0.52, color=face, edgecolor=edge)
        if width >= 10:
            zoom.text(
                position + width / 2,
                0,
                f"{label}\n{width} µs",
                ha="center",
                va="center",
                fontsize=8.3,
                color=edge,
                fontweight="bold",
            )
        position += width
    settle = _number(schedule["switch_settling_max_us"], "settling")
    zoom.annotate(
        f"switch settling ≤ {settle:.1f} µs",
        xy=(dwell + settle, 0.32),
        xytext=(dwell - 42, 0.43),
        arrowprops={"arrowstyle": "->", "color": RED},
        color=RED,
        fontsize=9,
        ha="right",
    )
    zoom.set_xlim(0, dwell + guard)
    zoom.set_ylim(-0.65, 0.62)
    zoom.set_yticks([])
    zoom.set_xlabel("selected-state edge and following null (µs)")
    zoom.set_title("1 MS/s complex analysis excludes 5 µs on both sides of every transition")
    zoom.grid(axis="x")
    zoom.spines[["top", "right", "left"]].set_visible(False)

    rounds = fig.add_subplot(grid[2, 0])
    rounds.set_axis_off()
    orders = [
        _sequence(item, "round order")
        for item in _sequence(frequency["round_orders_hz"], "round orders")
    ]
    rounds.text(
        0.0,
        1.02,
        "Complex-calibration matrix · three one-second rounds",
        fontsize=13,
        fontweight="bold",
        va="top",
    )
    for round_index, order in enumerate(orders):
        y = 0.73 - round_index * 0.27
        rounds.text(
            0.0,
            y + 0.055,
            f"Round {round_index + 1}",
            transform=rounds.transAxes,
            fontweight="bold",
            color=[BLUE, PURPLE, TEAL][round_index],
            va="center",
        )
        for index, value in enumerate(order):
            x = 0.105 + index * 0.145
            ghz = _number(value, "frequency") / 1e9
            box = FancyBboxPatch(
                (x, y),
                0.105,
                0.12,
                boxstyle="round,pad=0.008,rounding_size=0.015",
                edgecolor=RED if ghz > 5.0 else [BLUE, PURPLE, TEAL][round_index],
                facecolor="white",
                linewidth=1.2,
                transform=rounds.transAxes,
            )
            rounds.add_patch(box)
            rounds.text(
                x + 0.0525,
                y + 0.06,
                f"{ghz:.3f}",
                ha="center",
                va="center",
                transform=rounds.transAxes,
                fontsize=8.5,
                fontweight="bold",
            )
            if index < len(order) - 1:
                rounds.annotate(
                    "",
                    xy=(x + 0.138, y + 0.06),
                    xytext=(x + 0.11, y + 0.06),
                    xycoords=rounds.transAxes,
                    arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 1.0},
                )
    rounds.text(
        0.105,
        -0.02,
        (
            "18 immutable artifacts · 1 MS/s · "
            f"≥{_integer(plan['minimum_complete_frames_per_artifact'], 'minimum frames')} "
            "complete frames/artifact · 5.800 GHz requires explicit experimental opt-in"
        ),
        transform=rounds.transAxes,
        fontsize=9.3,
        color=MUTED,
    )
    fallback = _mapping(qualification["fallback_path"], "fallback evidence path")
    rounds.text(
        0.105,
        -0.13,
        (
            "Timing evidence is separate: two fresh 450 ms captures at 2 MS/s · "
            "see Figure 5 · GPIO identity/order remain source + readback-hash backed"
        ),
        transform=rounds.transAxes,
        fontsize=8.5,
        color=RED,
    )
    if fallback.get("name") != "source_readback_hash_plus_low_power_rf_timing":
        raise DesignReportError("unexpected fallback evidence path")
    _save(fig, path)


def _render_rf_timing(snapshot: Mapping[str, Any], path: Path) -> None:
    qualification = _mapping(
        snapshot["rf_timing_qualification"],
        "RF timing qualification",
    )
    contract = _mapping(qualification["capture_contract"], "RF timing capture")
    detector = _mapping(qualification["detector"], "RF timing detector")
    gates = _mapping(qualification["frozen_gates"], "RF timing gates")

    fig = plt.figure(figsize=(14, 10.5), constrained_layout=False)
    _title(
        fig,
        "Separate RF-only microsecond timing qualification",
        "Pre-execution contract: two independent 450 ms captures per claimed band measure "
        "RF-visible edges at 2 MS/s without claiming GPIO or connector identity.",
    )
    grid = fig.add_gridspec(
        3,
        2,
        left=0.055,
        right=0.97,
        bottom=0.065,
        top=0.855,
        height_ratios=(0.88, 1.02, 1.58),
        width_ratios=(1.08, 0.92),
        hspace=0.34,
        wspace=0.22,
    )

    flow = fig.add_subplot(grid[0, :])
    flow.set_axis_off()
    flow.text(
        0.0,
        0.97,
        "Fail-closed acquisition path",
        transform=flow.transAxes,
        fontsize=13,
        fontweight="bold",
        va="top",
    )
    stages = [
        (0.01, "Fresh stream", "capture A or B\nnever split one stream", BLUE),
        (0.21, "Continuous IQ", "450 ms · 2 MS/s\n1.6 MHz RF BW", TEAL),
        (0.41, "RAM hold", "9 × 250k frames\n8 kernel buffers", PURPLE),
        (0.61, "Mute + verify", "both TX channels\nexact-radio readback", RED),
        (0.81, "Persist", "only after cleanup\nthen analyze", AMBER),
    ]
    for x, title, subtitle, color in stages:
        box = FancyBboxPatch(
            (x, 0.38),
            0.16,
            0.25,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            edgecolor=color,
            facecolor="white",
            linewidth=1.5,
            transform=flow.transAxes,
        )
        flow.add_patch(box)
        flow.text(
            x + 0.08,
            0.555,
            title,
            transform=flow.transAxes,
            ha="center",
            va="center",
            fontsize=9.2,
            fontweight="bold",
            color=color,
        )
        flow.text(
            x + 0.08,
            0.455,
            subtitle,
            transform=flow.transAxes,
            ha="center",
            va="center",
            fontsize=7.4,
            color=MUTED,
            linespacing=1.15,
        )
    for left, right in zip(stages[:-1], stages[1:], strict=True):
        _arrow(flow, (left[0] + 0.165, 0.505), (right[0] - 0.006, 0.505))
    flow.text(
        0.01,
        0.12,
        "Band claim = A passes AND B passes. Repeat this pair at 2.4 GHz and again at exact "
        "experimental 5.8 GHz if timing is claimed at both; averaging cannot rescue a failure.",
        transform=flow.transAxes,
        fontsize=9.2,
        color=INK,
        bbox={"boxstyle": "round,pad=0.4", "facecolor": PALE_AMBER, "edgecolor": AMBER},
    )

    projection = fig.add_subplot(grid[1, 0])
    x = np.linspace(-4.0, 4.0, 401)
    q_curve = 0.5 * (1.0 + np.tanh(0.92 * x))
    projection.plot(x, q_curve, color=BLUE, linewidth=2.0, label="local complex projection q")
    bin_x = np.arange(-4.0, 4.1, 1.0)
    bin_q = 0.5 * (1.0 + np.tanh(0.92 * bin_x))
    projection.scatter(
        bin_x,
        bin_q,
        s=34,
        color=BLUE,
        edgecolor="white",
        zorder=4,
        label="1 µs coherent bins",
    )
    threshold_colors = {0.4: PURPLE, 0.5: RED, 0.6: TEAL}
    for threshold in _sequence(detector["threshold_sweep_q"], "threshold sweep"):
        q_value = _number(threshold, "threshold")
        crossing = float(np.interp(q_value, q_curve, x))
        color = threshold_colors[q_value]
        projection.axhline(
            q_value, color=color, linestyle=":" if q_value != 0.5 else "--", linewidth=1.2
        )
        projection.axvline(
            crossing, color=color, linestyle=":" if q_value != 0.5 else "--", linewidth=1.2
        )
        projection.text(
            crossing + (0.10 if q_value >= 0.5 else -0.10),
            q_value + 0.035,
            f"q{int(q_value * 100)}",
            color=color,
            ha="left" if q_value >= 0.5 else "right",
            fontsize=8.5,
            fontweight="bold",
        )
    projection.axvline(
        0.18, color=AMBER, linewidth=1.6, linestyle="-.", label="independent two-mean changepoint"
    )
    projection.set_xlim(-4.0, 4.0)
    projection.set_ylim(-0.04, 1.08)
    projection.set_xlabel("time relative to schematic RF edge (µs)")
    projection.set_ylabel("normalized complex projection q")
    projection.set_title("Local edge estimator schematic — not measured data")
    projection.grid(True)
    projection.legend(loc="lower right", fontsize=7.9)

    resolution = fig.add_subplot(grid[1, 1])
    resolution.set_axis_off()
    resolution.text(
        0.0,
        0.98,
        "Sampling and estimator independence",
        transform=resolution.transAxes,
        fontsize=13,
        fontweight="bold",
        va="top",
    )
    for index in range(5):
        x0 = 0.02 + index * 0.105
        resolution.add_patch(
            Rectangle(
                (x0, 0.66),
                0.09,
                0.15,
                transform=resolution.transAxes,
                facecolor=PALE_BLUE,
                edgecolor=BLUE,
                linewidth=1.1,
            )
        )
        resolution.text(
            x0 + 0.045,
            0.735,
            "0.2",
            transform=resolution.transAxes,
            ha="center",
            va="center",
            fontsize=8.3,
            color=BLUE,
        )
    resolution.annotate(
        "",
        xy=(0.69, 0.735),
        xytext=(0.56, 0.735),
        xycoords=resolution.transAxes,
        arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 1.4},
    )
    one_bin = FancyBboxPatch(
        (0.70, 0.65),
        0.27,
        0.17,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        edgecolor=TEAL,
        facecolor=PALE_TEAL,
        linewidth=1.4,
        transform=resolution.transAxes,
    )
    resolution.add_patch(one_bin)
    resolution.text(
        0.835,
        0.735,
        "1 µs\ncomplex bin",
        transform=resolution.transAxes,
        ha="center",
        va="center",
        color=TEAL,
        fontweight="bold",
    )
    resolution.text(
        0.02,
        0.57,
        "two native samples at 2 MS/s",
        transform=resolution.transAxes,
        fontsize=8.8,
        color=MUTED,
    )
    estimator_box = FancyBboxPatch(
        (0.02, 0.22),
        0.95,
        0.28,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        edgecolor=PURPLE,
        facecolor="white",
        linewidth=1.5,
        transform=resolution.transAxes,
    )
    resolution.add_patch(estimator_box)
    resolution.text(
        0.055,
        0.435,
        "Two edge estimates must agree",
        transform=resolution.transAxes,
        fontsize=10.2,
        fontweight="bold",
        color=PURPLE,
    )
    resolution.text(
        0.055,
        0.35,
        "q50 is the fractional crossing; q40–q60 span must be ≤ 1.5 µs.\n"
        "Independent two-mean changepoint must be within 1.5 µs of q50.",
        transform=resolution.transAxes,
        fontsize=7.9,
        va="top",
        color=INK,
        linespacing=1.35,
    )
    resolution.text(
        0.02,
        0.09,
        "Fractional interpolation is not an independent direct 0.5 µs edge fit.",
        transform=resolution.transAxes,
        fontsize=8.6,
        color=RED,
        fontweight="bold",
    )

    gate_ax = fig.add_subplot(grid[2, 0])
    gate_ax.set_axis_off()
    gate_ax.text(
        0.0,
        0.98,
        "Frozen per-artifact gates",
        fontsize=13,
        fontweight="bold",
        va="top",
        transform=gate_ax.transAxes,
    )
    gate_rows = [
        ("Decode", "≥290 complete cycles · ≥98% · exactly 12 visible edges/cycle"),
        ("Marker + dwells", "every value 190–210 µs"),
        ("Ordinary guards", "median 19–21 µs · conservative bounds 18–22 µs"),
        ("Cycle", "every value 1,425–1,575 µs"),
        ("Edge uncertainty", "q40–q60 ≤1.5 µs · changepoint–q50 ≤1.5 µs"),
        ("RF observability", "transition, pilot and state/null contrast each ≥20 dB"),
        ("Integrity", "ABI 2 continuous · zero gaps, flags and clips · near-FS ≤1e-4"),
    ]
    for index, (label, value) in enumerate(gate_rows):
        y = 0.82 - index * 0.112
        face = "white" if index % 2 == 0 else PALE_BLUE
        gate_ax.add_patch(
            Rectangle(
                (0.0, y),
                0.99,
                0.092,
                transform=gate_ax.transAxes,
                facecolor=face,
                edgecolor=GRID,
                linewidth=0.8,
            )
        )
        gate_ax.text(
            0.025,
            y + 0.046,
            label,
            transform=gate_ax.transAxes,
            va="center",
            fontsize=8.7,
            fontweight="bold",
            color=BLUE,
        )
        gate_ax.text(
            0.25,
            y + 0.046,
            value,
            transform=gate_ax.transAxes,
            va="center",
            fontsize=8.25,
            color=INK,
        )

    claim_ax = fig.add_subplot(grid[2, 1])
    claim_ax.set_axis_off()
    claim_ax.text(
        0.0,
        0.98,
        "What a passing pair can claim",
        fontsize=13,
        fontweight="bold",
        va="top",
        transform=claim_ax.transAxes,
    )
    for index, (band, note, color) in enumerate(
        [
            ("2.4 GHz", "capture A pass  ∧  capture B pass", BLUE),
            ("5.8 GHz", "separate A+B pass · experimental opt-in", PURPLE),
        ]
    ):
        y = 0.76 - index * 0.18
        _card(claim_ax, 0.0, y, 0.98, 0.15, band, note, color)
    claim_ax.text(
        0.0,
        0.49,
        "RF-only evidence boundary",
        fontsize=11.5,
        fontweight="bold",
        transform=claim_ax.transAxes,
    )
    limits = [
        "Qualifies only the combined marker, ordinary guards, dwells and cycle.",
        "Cannot split the 180 µs marker body from its contiguous 20 µs pre-ANT1 guard.",
        "No independent GPIO code, connector identity/order, illegal-state or GPIO "
        "break-before-make proof.",
        "ANT1 forward; ANT2–ANT6 clockwise stays source/profile + readback-hash backed.",
        "Times use the Pluto sample clock—not a calibrated SI timebase.",
    ]
    for index, item in enumerate(limits):
        y = 0.40 - index * 0.095
        claim_ax.scatter(
            [0.015],
            [y + 0.01],
            s=22,
            color=RED if index < 3 else AMBER,
            transform=claim_ax.transAxes,
        )
        claim_ax.text(
            0.045,
            y,
            textwrap.fill(item, width=64),
            transform=claim_ax.transAxes,
            fontsize=7.8,
            va="top",
            color=INK,
            linespacing=1.12,
        )

    expected_cycles = (
        _number(contract["duration_ms_per_capture"], "duration")
        * 1_000.0
        / _number(snapshot["schedule"]["cycle_us"], "cycle")
    )
    if not _close(expected_cycles, 300.0):
        raise DesignReportError("RF timing capture must contain 300 nominal cycles")
    if _integer(gates["minimum_complete_cycles_per_capture"], "minimum cycles") != 290:
        raise DesignReportError("unexpected RF timing minimum cycle gate")
    _save(fig, path)


def _arrow(
    ax: Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    color: str = MUTED,
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            transform=ax.transAxes,
            arrowstyle="-|>",
            mutation_scale=13,
            linewidth=1.5,
            color=color,
        )
    )


def _chain_box(
    ax: Axes,
    x: float,
    y: float,
    width: float,
    title: str,
    subtitle: str,
    color: str,
) -> None:
    box = FancyBboxPatch(
        (x, y),
        width,
        0.17,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        edgecolor=color,
        facecolor="white",
        linewidth=1.5,
        transform=ax.transAxes,
    )
    ax.add_patch(box)
    ax.text(
        x + width / 2,
        y + 0.115,
        title,
        transform=ax.transAxes,
        ha="center",
        fontweight="bold",
        color=color,
    )
    ax.text(
        x + width / 2,
        y + 0.055,
        subtitle,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=8.2,
        color=MUTED,
        linespacing=1.2,
    )


def _render_identifiability(snapshot: Mapping[str, Any], path: Path) -> None:
    model = _mapping(snapshot["calibration_model"], "calibration model")

    fig = plt.figure(figsize=(14, 8.4), constrained_layout=False)
    _title(
        fig,
        "What the centered OTA measurement can — and cannot — calibrate",
        "The observable is a repeatable end-to-end complex manifold; one OTA geometry "
        "cannot uniquely factor electronics from element response.",
    )
    grid = fig.add_gridspec(
        2,
        1,
        left=0.055,
        right=0.97,
        bottom=0.07,
        top=0.86,
        height_ratios=(0.92, 1.08),
        hspace=0.24,
    )

    chain = fig.add_subplot(grid[0, 0])
    chain.set_axis_off()
    chain.text(0.0, 0.96, "Centered OTA acquisition", fontsize=13, fontweight="bold", va="top")
    boxes = [
        (0.01, 0.47, 0.15, "TX1", "bounded coherent\npilot", AMBER),
        (0.21, 0.47, 0.17, "Aᵢ(f)", "element + coupling\n+ environment", PURPLE),
        (0.43, 0.47, 0.18, "Cᵢ(f)", "cable + connector\n+ PCB + switch", BLUE),
        (0.66, 0.47, 0.14, "RX2", "prequalified fixed\ngain + continuous IQ", TEAL),
        (0.85, 0.47, 0.14, "Hᵢ(f)", "null-subtracted\ncomplex phasor", RED),
    ]
    for x, y, width, heading, body, color in boxes:
        _chain_box(chain, x, y, width, heading, body, color)
    for first, second in zip(boxes[:-1], boxes[1:], strict=True):
        _arrow(chain, (first[0] + first[2] + 0.005, 0.555), (second[0] - 0.005, 0.555))
    chain.text(
        0.5,
        0.30,
        str(model["measurement_equation"]),
        transform=chain.transAxes,
        ha="center",
        fontsize=17,
        family="DejaVu Sans Mono",
        fontweight="bold",
        color=INK,
    )
    chain.text(
        0.5,
        0.16,
        "Gauge ambiguity: Cᵢ → qᵢCᵢ and Aᵢ → Aᵢ/qᵢ leave every Hᵢ unchanged.",
        transform=chain.transAxes,
        ha="center",
        fontsize=10.5,
        color=RED,
        bbox={"boxstyle": "round,pad=0.45", "facecolor": "white", "edgecolor": RED},
    )

    stages = fig.add_subplot(grid[1, 0])
    stages.set_axis_off()
    stages.text(
        0.0,
        0.98,
        "How to establish separate reference planes",
        fontsize=13,
        fontweight="bold",
        va="top",
    )
    stage_data = [
        (
            "1",
            "Board-plane through",
            "Characterized splitter at ANT1–ANT6 SMA planes estimates PCB/switch paths.",
            BLUE,
        ),
        (
            "2",
            "Feed-plane through",
            "Move the reference plane to the array feeds to add cable coefficients.",
            TEAL,
        ),
        (
            "3",
            "Centered OTA",
            "Divide by the through response; residual contains elements, coupling and room.",
            PURPLE,
        ),
        (
            "4",
            "Permutation check",
            "Cycle elements and splitter outputs; reject a non-separable two-factor fit.",
            AMBER,
        ),
    ]
    for index, (number, heading, body, color) in enumerate(stage_data):
        x = 0.01 + index * 0.25
        circle = Circle(
            (x + 0.035, 0.67), 0.035, transform=stages.transAxes, facecolor=color, edgecolor="none"
        )
        stages.add_patch(circle)
        stages.text(
            x + 0.035,
            0.67,
            number,
            color="white",
            ha="center",
            va="center",
            transform=stages.transAxes,
            fontweight="bold",
        )
        _card(stages, x, 0.27, 0.22, 0.31, heading, body, color)
        if index < len(stage_data) - 1:
            _arrow(stages, (x + 0.23, 0.43), (x + 0.255, 0.43), MUTED)
    stages.text(
        0.01,
        0.08,
        "Retained scope: center equalization is valid for this assembled setup. A directional "
        "manifold still requires surveyed off-centre/azimuth calibration positions.",
        transform=stages.transAxes,
        fontsize=10,
        color=INK,
        bbox={"boxstyle": "round,pad=0.5", "facecolor": PALE_AMBER, "edgecolor": AMBER},
    )
    _save(fig, path)


def _phasor_panel(ax: Axes, phases_deg: np.ndarray, title: str) -> None:
    grouped: list[tuple[float, list[int]]] = []
    for index, phase in enumerate(phases_deg, start=1):
        for group_phase, indices in grouped:
            if abs(phase - group_phase) < 1e-3:
                indices.append(index)
                break
        else:
            grouped.append((float(phase), [index]))
    grouped.sort(key=lambda item: item[0])
    colors = [BLUE, PURPLE, TEAL, AMBER]
    for (phase_deg, indices), color in zip(grouped, colors, strict=True):
        phase = np.deg2rad(-phase_deg)
        ax.annotate(
            "",
            xy=(phase, 1.0),
            xytext=(phase, 0.0),
            arrowprops={"arrowstyle": "-|>", "lw": 2.0, "color": color},
        )
        label = "/".join(str(index) for index in indices)
        ax.text(
            phase,
            1.09,
            label,
            color=color,
            ha="center",
            va="center",
            fontweight="bold",
            fontsize=8.5,
        )
    ax.set_ylim(0, 1.16)
    ax.set_yticklabels([])
    ax.set_theta_zero_location("E")
    ax.set_theta_direction(1)
    ax.grid(True)
    ax.set_title(title, pad=10)


def _render_phasors(snapshot: Mapping[str, Any], path: Path) -> None:
    routes = _mapping(snapshot["pcb_route_priors"], "PCB routes")
    rows = [_mapping(item, "route row") for item in _sequence(routes["rows"], "route rows")]
    gates = _mapping(snapshot["acceptance_gates"], "acceptance gates")
    symmetry = _mapping(gates["corrected_symmetry"], "symmetry gates")

    phases_2g4 = np.asarray([_number(row["phase_2g4_deg"], "2.4 phase") for row in rows])
    phases_5g8 = np.asarray([_number(row["phase_5g8_deg"], "5.8 phase") for row in rows])
    colors = [BLUE, TEAL, PURPLE, AMBER, "#9a6b53", "#3f7d59"]

    fig = plt.figure(figsize=(14, 8.5), constrained_layout=False)
    _title(
        fig,
        "Expected raw phasors and the calibrated C6 acceptance test",
        "Equal geometry does not imply equal raw phase: released unequal PCB routes alone "
        "span 73° at 2.4 GHz and 177° at 5.8 GHz.",
    )
    grid = fig.add_gridspec(
        2,
        3,
        left=0.055,
        right=0.97,
        bottom=0.075,
        top=0.79,
        height_ratios=(1.05, 0.95),
        width_ratios=(1.0, 1.0, 0.9),
        hspace=0.40,
        wspace=0.30,
    )
    ax24 = fig.add_subplot(grid[0, 0], projection="polar")
    _phasor_panel(ax24, phases_2g4, "PCB-only prior · 2.4 GHz")
    ax58 = fig.add_subplot(grid[0, 1], projection="polar")
    _phasor_panel(ax58, phases_5g8, "PCB-only prior · 5.8 GHz")

    route_ax = fig.add_subplot(grid[0, 2])
    lengths = np.asarray([_number(row["route_mm"], "route length") for row in rows])
    ypos = np.arange(6)
    route_ax.barh(ypos, lengths, color=colors, height=0.55)
    route_ax.set_yticks(ypos, [f"ANT{index}" for index in range(1, 7)])
    route_ax.invert_yaxis()
    route_ax.set_xlim(0, 40)
    route_ax.set_xlabel("released RF copper (mm)")
    route_ax.set_title("No PCB length matching")
    route_ax.grid(axis="x")
    route_ax.spines[["top", "right", "left"]].set_visible(False)
    for y, value in zip(ypos, lengths, strict=True):
        route_ax.text(value + 0.6, y, f"{value:.2f}", va="center", fontsize=8.5)

    modes = fig.add_subplot(grid[1, :2])
    mode_index = np.arange(6)
    ideal = np.asarray([0.0, -60.0, -60.0, -60.0, -60.0, -60.0])
    minimum = _number(symmetry["minimum_maximum_noncommon_mode_dbc"], "minimum mode")
    target = _number(symmetry["target_maximum_noncommon_mode_dbc"], "target mode")
    modes.bar(mode_index, ideal, color=[TEAL, BLUE, BLUE, BLUE, BLUE, BLUE], width=0.62)
    modes.axhline(
        minimum,
        color=RED,
        linestyle="--",
        linewidth=1.6,
        label=f"minimum non-common gate {minimum:.0f} dBc",
    )
    modes.axhline(
        target, color=AMBER, linestyle=":", linewidth=2.0, label=f"target {target:.0f} dBc"
    )
    modes.set_xticks(mode_index, [f"M{index}" for index in mode_index])
    modes.set_ylim(-65, 5)
    modes.set_ylabel("mode amplitude relative to M0 (dBc)")
    modes.set_title("After calibration, ideal centered C6 response contains only M0")
    modes.grid(axis="y")
    modes.legend(loc="lower left", fontsize=8.5)
    modes.text(0, 1.5, "common", ha="center", color=TEAL, fontweight="bold")
    modes.text(
        5.0,
        -62.0,
        "ideal non-common modes are zero; -60 dBc is the display floor",
        ha="right",
        va="bottom",
        fontsize=8.2,
        color=MUTED,
    )

    acceptance = fig.add_subplot(grid[1, 2])
    acceptance.set_axis_off()
    acceptance.text(0.0, 0.98, "Primary calibrated gates", fontsize=13, fontweight="bold", va="top")
    amplitude_span = _number(symmetry["maximum_amplitude_span_db"], "amplitude span")
    phase_rms = _number(symmetry["maximum_circular_phase_rms_deg"], "phase RMS")
    resultant = _number(symmetry["minimum_phase_resultant_length"], "resultant")
    pair_phase = _number(
        symmetry["maximum_opposite_pair_phase_mismatch_deg"],
        "pair phase",
    )
    gate_rows = [
        ("Amplitude span", f"≤ {amplitude_span:.1f} dB", BLUE),
        ("Circular phase RMS", f"≤ {phase_rms:.1f}°", PURPLE),
        ("Phase resultant", f"≥ {resultant:.3f}", TEAL),
        ("Opposite-pair phase", f"≤ {pair_phase:.1f}°", AMBER),
    ]
    for index, (label, value, color) in enumerate(gate_rows):
        y = 0.77 - index * 0.19
        acceptance.add_patch(
            Rectangle(
                (0.0, y),
                0.98,
                0.135,
                transform=acceptance.transAxes,
                facecolor="white",
                edgecolor=GRID,
                linewidth=1.0,
            )
        )
        acceptance.add_patch(
            Rectangle(
                (0.0, y),
                0.018,
                0.135,
                transform=acceptance.transAxes,
                facecolor=color,
                edgecolor=color,
            )
        )
        acceptance.text(
            0.045,
            y + 0.087,
            label,
            transform=acceptance.transAxes,
            fontsize=8.8,
            color=MUTED,
            va="center",
        )
        acceptance.text(
            0.95,
            y + 0.05,
            value,
            transform=acceptance.transAxes,
            fontsize=12,
            fontweight="bold",
            color=color,
            ha="right",
            va="center",
        )
    acceptance.text(
        0.0,
        0.005,
        "ANT3=ANT6 and ANT4=ANT5 are exact PCB-length pairs; use them as raw diagnostics, "
        "not substitutes for through calibration.",
        transform=acceptance.transAxes,
        fontsize=8.4,
        color=MUTED,
        va="bottom",
        wrap=True,
    )
    _save(fig, path)


def render_report(snapshot_path: Path, output_directory: Path) -> dict[str, str]:
    snapshot = load_snapshot(snapshot_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    _configure_style()
    renderers = (
        _render_geometry,
        _render_timing,
        _render_identifiability,
        _render_phasors,
        _render_rf_timing,
    )
    for filename, renderer in zip(FIGURE_NAMES, renderers, strict=True):
        renderer(snapshot, output_directory / filename)
    return {filename: _sha256(output_directory / filename) for filename in FIGURE_NAMES}


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise DesignReportError(f"generated figure is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _manifest_document(
    snapshot_path: Path,
    hashes: Mapping[str, str],
    figure_directory: Path,
) -> dict[str, Any]:
    figures = []
    for filename in FIGURE_NAMES:
        path = figure_directory / filename
        if hashes[filename] != _sha256(path):
            raise DesignReportError(f"figure hash changed while building manifest: {filename}")
        width, height = _png_dimensions(path)
        figures.append(
            {
                "path": f"docs/hexray_tx_in_middle_calibration/png/{filename}",
                "sha256": hashes[filename],
                "byte_size": path.stat().st_size,
                "width_px": width,
                "height_px": height,
            }
        )
    return {
        "schema": 1,
        "design_kind": "hexray_tx1_center_high_rate_complex_calibration_figures",
        "renderer": "scripts/render_hexray_center_calibration_design.py",
        "renderer_sha256": _sha256(Path(__file__).resolve()),
        "matplotlib_version": matplotlib.__version__,
        "snapshot": {
            "path": "docs/hexray_tx_in_middle_calibration/data/design-snapshot.json",
            "sha256": _sha256(snapshot_path),
        },
        "figures": figures,
    }


def _write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _check(snapshot_path: Path, output_directory: Path, manifest_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="smateway-hexray-design-") as temporary:
        temporary_directory = Path(temporary)
        hashes = render_report(snapshot_path, temporary_directory)
        for filename in FIGURE_NAMES:
            committed = output_directory / filename
            generated = temporary_directory / filename
            if not committed.is_file():
                raise DesignReportError(f"missing committed figure: {committed}")
            if committed.read_bytes() != generated.read_bytes():
                raise DesignReportError(f"figure is not byte-reproducible: {filename}")
        expected_manifest = _manifest_document(snapshot_path, hashes, temporary_directory)
        try:
            observed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DesignReportError(f"cannot load figure manifest: {error}") from error
        if observed_manifest != expected_manifest:
            raise DesignReportError("committed HexRay design figure manifest is stale")


def main() -> int:
    args = _parser().parse_args()
    snapshot_path = args.snapshot.expanduser().resolve()
    output_directory = args.output_directory.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    try:
        if args.check:
            _check(snapshot_path, output_directory, manifest_path)
            print(json.dumps({"status": "passed", "figures_checked": len(FIGURE_NAMES)}))
            return 0
        hashes = render_report(snapshot_path, output_directory)
        _write_manifest(
            manifest_path,
            _manifest_document(snapshot_path, hashes, output_directory),
        )
    except (OSError, DesignReportError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 2
    print(
        json.dumps(
            {
                "status": "rendered",
                "output_directory": str(output_directory),
                "figures": list(FIGURE_NAMES),
                "manifest": str(manifest_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
