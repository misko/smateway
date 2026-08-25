#!/usr/bin/env python3
"""Render the retained RX1 reference negative-result report."""

from __future__ import annotations

import argparse
import hashlib
import json
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
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Rectangle

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = (
    REPOSITORY
    / "docs/localization/data/rx1-reference-20260825-a-report-snapshot.json"
)
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY / "docs/localization/png"
DEFAULT_MANIFEST = (
    REPOSITORY
    / "docs/localization/data/rx1-reference-20260825-a-figures-manifest.json"
)
FIGURE_NAMES = (
    "fig09_rx1_capture_integrity.png",
    "fig10_rx1_coherence_model_gate.png",
    "fig11_rx1_identifiability_and_next_experiment.png",
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


class ReportError(RuntimeError):
    """The compact report snapshot is malformed or outputs differ."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--check",
        action="store_true",
        help="render to a temporary directory and byte-compare committed PNGs and manifest",
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
        raise ReportError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReportError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{label} must be numeric")
    result = float(value)
    if not np.isfinite(result):
        raise ReportError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"{label} must be an integer")
    return value


def load_snapshot(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReportError(f"cannot load snapshot {path}: {error}") from error
    root = _mapping(document, "snapshot")
    if root.get("schema") != 1:
        raise ReportError("snapshot schema must be 1")
    if root.get("report_kind") != "rx1_reference_negative_localization_result":
        raise ReportError("snapshot has the wrong report kind")
    integrity = _mapping(root.get("integrity_audit"), "integrity_audit")
    if integrity.get("passed") is not True:
        raise ReportError("snapshot does not contain a passed acquisition-integrity audit")
    strict = _mapping(root.get("strict_model_gate"), "strict_model_gate")
    if strict.get("passed") is not False:
        raise ReportError("strict model gate must remain failed for this retained report")
    identifiability = _mapping(root.get("identifiability"), "identifiability")
    if identifiability.get("unique_planar_position_identified") is not False:
        raise ReportError("snapshot must not claim a unique RX1 position")
    if identifiability.get("accepted_range_difference_identified") is not False:
        raise ReportError("snapshot must not claim an accepted RX1 range difference")
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
                "Software": "smateway deterministic RX1 report",
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
    value: str,
    label: str,
) -> None:
    box = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.012,rounding_size=0.025",
        linewidth=1.1,
        edgecolor=GRID,
        facecolor="white",
        transform=ax.transAxes,
    )
    ax.add_patch(box)
    ax.text(
        x + width / 2,
        y + height * 0.61,
        value,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=18,
        fontweight="bold",
        color=BLUE,
    )
    ax.text(
        x + width / 2,
        y + height * 0.25,
        label,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=8.7,
        color=MUTED,
    )


def _render_integrity(snapshot: Mapping[str, Any], path: Path) -> None:
    plan = _mapping(snapshot["capture_plan"], "capture_plan")
    audit = _mapping(snapshot["integrity_audit"], "integrity_audit")
    quality = _mapping(snapshot["quality_audit"], "quality_audit")
    failure = _mapping(snapshot["transient_failure"], "transient_failure")

    fig = plt.figure(figsize=(14, 8.2), constrained_layout=False)
    _title(
        fig,
        "RX1 reference experiment — acquisition integrity",
        "The stream, artifacts and RF safety boundary passed; one transient USB attempt "
        "was quarantined and retried.",
    )
    grid = fig.add_gridspec(
        2,
        2,
        left=0.055,
        right=0.97,
        bottom=0.08,
        top=0.865,
        width_ratios=(1.05, 0.95),
        height_ratios=(1.0, 0.9),
        wspace=0.22,
        hspace=0.32,
    )

    cards = fig.add_subplot(grid[0, 0])
    cards.set_axis_off()
    cards.set_title("Immutable run inventory", loc="left", pad=8)
    _card(
        cards,
        0.00,
        0.53,
        0.30,
        0.34,
        f"{_integer(plan['finalized_artifacts'], 'finalized_artifacts')}/42",
        "finalized / planned",
    )
    _card(
        cards,
        0.35,
        0.53,
        0.30,
        0.34,
        f"{_integer(audit['total_blocks'], 'total_blocks'):,}",
        "continuous buffers",
    )
    _card(
        cards,
        0.70,
        0.53,
        0.30,
        0.34,
        "3.36 GB",
        "IQ bytes SHA-256 checked",
    )
    _card(
        cards,
        0.00,
        0.08,
        0.30,
        0.34,
        "0",
        "missing samples / gaps",
    )
    _card(cards, 0.35, 0.08, 0.30, 0.34, "0", "clip / near-full-scale")
    _card(
        cards,
        0.70,
        0.08,
        0.30,
        0.34,
        "PASS",
        "exact-serial final mute",
    )

    admissions = fig.add_subplot(grid[0, 1])
    admissions.set_title("Canonical OTA-reference admission")
    labels = ["Capture analyses", "Per-state judgments", "Artifact SHA-256"]
    passed = np.asarray(
        [
            _integer(quality["capture_analyses_passed"], "capture analyses passed"),
            _integer(quality["state_judgments_passed"], "state judgments passed"),
            _integer(audit["artifact_sha256_matches"], "artifact SHA matches"),
        ],
        dtype=float,
    )
    totals = np.asarray(
        [
            passed[0] + _integer(quality["capture_analyses_rejected"], "rejected captures"),
            _integer(quality["state_judgments_total"], "state judgments total"),
            _integer(plan["finalized_artifacts"], "finalized artifacts"),
        ],
        dtype=float,
    )
    fractions = 100.0 * passed / totals
    ypos = np.arange(len(labels))
    admissions.barh(ypos, np.full(3, 100.0), color="#edf1f4", height=0.48)
    admissions.barh(ypos, fractions, color=[TEAL, BLUE, PURPLE], height=0.48)
    admissions.set_yticks(ypos, labels)
    admissions.set_xlim(0, 102)
    admissions.set_xlabel("passed (%)")
    admissions.invert_yaxis()
    admissions.grid(axis="x")
    admissions.spines[["top", "right", "left"]].set_visible(False)
    for y, numerator, denominator, fraction in zip(
        ypos, passed.astype(int), totals.astype(int), fractions, strict=True
    ):
        admissions.text(
            min(fraction - 1.0, 98.5),
            y,
            f"{numerator}/{denominator}",
            ha="right",
            va="center",
            color="white" if fraction > 75 else INK,
            fontweight="bold",
        )
    headroom = fig.add_subplot(grid[1, 0])
    headroom.set_title("ADC peak headroom across 42 artifacts")
    receiver_peaks = [
        _number(_sequence(audit["rx1_peak_component_counts"], "RX1 peaks")[1], "RX1 max"),
        _number(_sequence(audit["rx2_peak_component_counts"], "RX2 peaks")[1], "RX2 max"),
    ]
    near_limit = _number(audit["near_full_scale_threshold_counts"], "near-full-scale limit")
    clip_limit = _number(audit["clip_threshold_counts"], "clip limit")
    headroom.barh([0, 1], receiver_peaks, color=[BLUE, TEAL], height=0.42)
    headroom.axvline(
        near_limit,
        color=AMBER,
        linestyle="--",
        linewidth=1.8,
        label="near full scale",
    )
    headroom.axvline(clip_limit, color=RED, linestyle=":", linewidth=1.8, label="clip threshold")
    headroom.set_yticks([0, 1], ["RX1", "RX2"])
    headroom.set_xlim(0, 2200)
    headroom.set_xlabel("maximum absolute I/Q component (ADC counts)")
    headroom.invert_yaxis()
    headroom.grid(axis="x")
    headroom.legend(loc="lower right")
    headroom.spines[["top", "right", "left"]].set_visible(False)
    for y, value in enumerate(receiver_peaks):
        headroom.text(value + 30, y, f"{value:.0f}", va="center", fontweight="bold")

    retry = fig.add_subplot(grid[1, 1])
    retry.set_axis_off()
    retry.set_title("Fail-closed ENODATA lifecycle", loc="left", pad=8)
    stages = [
        ("Attempt 40", "libiio refill\nENODATA", RED),
        ("Quarantine", "no artifact\naccepted", AMBER),
        ("Safety", "exact radio\nmuted", BLUE),
        ("Attempt 41", "identical retry\npassed", TEAL),
    ]
    for index, (heading, body, color) in enumerate(stages):
        x = 0.01 + index * 0.25
        box = FancyBboxPatch(
            (x, 0.35),
            0.20,
            0.38,
            boxstyle="round,pad=0.012,rounding_size=0.025",
            edgecolor=color,
            facecolor="white",
            linewidth=1.7,
            transform=retry.transAxes,
        )
        retry.add_patch(box)
        retry.text(
            x + 0.10,
            0.62,
            heading,
            transform=retry.transAxes,
            ha="center",
            fontweight="bold",
            color=color,
        )
        retry.text(
            x + 0.10,
            0.45,
            body,
            transform=retry.transAxes,
            ha="center",
            va="center",
            fontsize=9,
        )
        if index < len(stages) - 1:
            retry.annotate(
                "",
                xy=(x + 0.245, 0.54),
                xytext=(x + 0.205, 0.54),
                xycoords=retry.transAxes,
                arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 1.5},
            )
    retry.text(
        0.01,
        0.12,
        textwrap.fill(str(failure["interpretation"]), width=84),
        transform=retry.transAxes,
        color=MUTED,
        fontsize=9,
        va="center",
    )
    _save(fig, path)


def _render_model_gate(snapshot: Mapping[str, Any], path: Path) -> None:
    strict = _mapping(snapshot["strict_model_gate"], "strict_model_gate")
    repeat = _mapping(snapshot["repeatability"], "repeatability")
    rows = [_mapping(row, "frequency row") for row in _sequence(strict["frequency_rows"], "rows")]
    frequencies = np.asarray(
        [_number(row["carrier_frequency_hz"], "carrier frequency") / 1e9 for row in rows]
    )
    state_coherence = np.asarray(
        [_number(row["state_coherence"], "state coherence") for row in rows]
    )
    state_rms = np.asarray(
        [_number(row["state_phase_rms_deg"], "state RMS") for row in rows]
    )
    paired = _mapping(repeat["paired_repeat_coherence"], "paired repeat coherence")

    fig, axes = plt.subplots(1, 2, figsize=(14, 7.5))
    fig.subplots_adjust(left=0.07, right=0.97, bottom=0.18, top=0.82, wspace=0.24)
    _title(
        fig,
        "Repeatable phase, rejected free-space model",
        "Repeat pairs agree closely, but geometry-corrected selector states do not "
        "collapse to one RX1 range-difference phase.",
    )

    ax = axes[0]
    ax.plot(frequencies, state_coherence, marker="o", color=RED, linewidth=2.2, label="cross-state")
    ax.axhline(
        _number(strict["minimum_state_coherence"], "minimum state coherence"),
        color=INK,
        linestyle="--",
        linewidth=1.5,
        label="strict gate (0.50)",
    )
    ax.axhspan(
        _number(paired["minimum"], "pair minimum"),
        _number(paired["maximum"], "pair maximum"),
        color=TEAL,
        alpha=0.18,
        label="paired-repeat range",
    )
    ax.scatter(
        [frequencies.mean()],
        [_number(paired["median"], "pair median")],
        color=TEAL,
        marker="D",
        s=52,
        zorder=5,
        label="paired-repeat median",
    )
    ax.set_title("Circular coherence")
    ax.set_xlabel("carrier frequency (GHz)")
    ax.set_ylabel("resultant coherence")
    ax.set_ylim(0, 1.05)
    ax.grid(True)
    ax.legend(loc="center left", fontsize=8.5)
    for x, y in zip(frequencies, state_coherence, strict=True):
        ax.text(x, y - 0.055, f"{y:.3f}", ha="center", va="top", fontsize=8, color=RED)

    ax = axes[1]
    colors = [RED if value >= 80 else AMBER for value in state_rms]
    bars = ax.bar(frequencies, state_rms, width=0.0065, color=colors)
    ax.axhspan(0, 10, color=TEAL, alpha=0.12, label="10° systematic floor used by fit")
    ax.set_title("State disagreement after geometry correction")
    ax.set_xlabel("carrier frequency (GHz)")
    ax.set_ylabel("circular phase RMS (degrees)")
    ax.set_ylim(0, 105)
    ax.grid(axis="y")
    ax.legend(loc="upper left", fontsize=8.5)
    for bar, value in zip(bars, state_rms, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 2.0,
            f"{value:.1f}°",
            ha="center",
            fontsize=8,
        )

    fig.text(
        0.07,
        0.085,
        "Inference: the coherent measurement is stable; deterministic antenna response, "
        "geometry error, coupling and multipath violate the common direct-path state model.",
        fontsize=10.2,
        color=INK,
        bbox={"boxstyle": "round,pad=0.55", "facecolor": "white", "edgecolor": GRID},
    )
    _save(fig, path)


def _render_identifiability(snapshot: Mapping[str, Any], path: Path) -> None:
    geometry = _mapping(snapshot["coordinate_system"], "coordinate_system")
    diagnostic = _mapping(snapshot["invalid_ungated_diagnostic"], "ungated diagnostic")
    conclusion = _mapping(snapshot["conclusion"], "conclusion")
    tx1 = np.asarray(_sequence(geometry["tx1_position_mm"], "TX1 position"), dtype=float)
    tx2 = np.asarray(_sequence(geometry["tx2_position_mm"], "TX2 position"), dtype=float)
    aliases = np.asarray(_sequence(diagnostic["near_equal_aliases_mm"], "aliases"), dtype=float)

    fig = plt.figure(figsize=(14, 8.4), constrained_layout=False)
    _title(
        fig,
        "RX1 identifiability — why this run reports no point",
        "The plotted curves are deliberately ungated aliases: they visualize ambiguity "
        "and are not accepted RX1 loci.",
    )
    grid = fig.add_gridspec(
        1,
        2,
        left=0.055,
        right=0.97,
        bottom=0.08,
        top=0.86,
        width_ratios=(1.15, 0.85),
        wspace=0.18,
    )

    ax = fig.add_subplot(grid[0, 0])
    axis = np.linspace(-500.0, 500.0, 600)
    xx, yy = np.meshgrid(axis, axis)
    difference = np.hypot(xx - tx2[0], yy - tx2[1]) - np.hypot(
        xx - tx1[0], yy - tx1[1]
    )
    alias_colors = [RED, PURPLE, AMBER, BLUE, TEAL, "#9a6b53"]
    alias_handles: list[Line2D] = []
    for index, (alias, color) in enumerate(zip(aliases, alias_colors, strict=True)):
        ax.contour(
            xx,
            yy,
            difference,
            levels=[float(alias)],
            colors=[color],
            linewidths=1.25,
            linestyles="--",
            alpha=0.72,
        )
        if index < 3:
            alias_handles.append(
                Line2D(
                    [0],
                    [0],
                    color=color,
                    linewidth=1.25,
                    linestyle="--",
                    label=f"{alias:+.1f} mm",
                )
            )
    board = Rectangle((-45, -32.5), 90, 65, facecolor="#eaf2f8", edgecolor=BLUE, linewidth=1.4)
    ax.add_patch(board)
    ax.text(0, 0, "RX2 array\nboard", ha="center", va="center", color=BLUE, fontweight="bold")
    tx1_handle = ax.scatter(
        [tx1[0]], [tx1[1]], color=PURPLE, marker="^", s=90, zorder=6, label="TX1 anchor"
    )
    tx2_handle = ax.scatter(
        [tx2[0]], [tx2[1]], color=AMBER, marker="^", s=90, zorder=6, label="TX2 conditional"
    )
    ax.annotate("TX1", tx1, xytext=(8, -15), textcoords="offset points", fontweight="bold")
    ax.annotate("TX2", tx2, xytext=(8, -15), textcoords="offset points", fontweight="bold")
    ax.set_xlim(-500, 500)
    ax.set_ylim(-500, 500)
    ax.invert_yaxis()
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("x (mm, right/east)")
    ax.set_ylabel("y (mm, down/south)")
    ax.set_title("Nearly equal ungated aliases — none accepted")
    ax.grid(True)
    ax.legend(
        handles=[*alias_handles, tx1_handle, tx2_handle],
        loc="upper left",
        fontsize=8.3,
        ncol=2,
    )

    steps = fig.add_subplot(grid[0, 1])
    steps.set_axis_off()
    steps.text(
        0.0,
        0.96,
        "Even an ideal two-anchor result has rank one",
        fontsize=13,
        fontweight="bold",
    )
    steps.text(
        0.0,
        0.865,
        "d(RX1, TX2) − d(RX1, TX1) = Δ",
        fontsize=14,
        color=BLUE,
        family="DejaVu Sans Mono",
    )
    steps.text(
        0.0,
        0.79,
        "One accepted Δ would define a hyperbola, not a unique planar point.\n"
        "Here the strict model rejected Δ itself.",
        fontsize=10,
        color=MUTED,
        linespacing=1.5,
    )
    steps.text(0.0, 0.68, "Next experiment", fontsize=13, fontweight="bold")
    _sequence(conclusion["next_experiment"], "next experiment")
    colors = [BLUE, TEAL, PURPLE, AMBER]
    short_titles = ["Survey + jig", "3–4 TX anchors", "Wideband delay", "Calibrate manifold"]
    short_bodies = [
        "Survey all phase centres in x/y/z and lock the setup away from reflective metal.",
        "Use at least three non-collinear surveyed TX positions; four are preferred.",
        "Estimate RX2/RX1 group delay over a wider band and gate the earliest usable path.",
        "Splitter-calibrate every selector path, then record an OTA manifold at surveyed points.",
    ]
    for index, (body, color, heading) in enumerate(
        zip(short_bodies, colors, short_titles, strict=True)
    ):
        y = 0.53 - index * 0.145
        box = FancyBboxPatch(
            (0.0, y),
            0.98,
            0.112,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            facecolor="white",
            edgecolor=color,
            linewidth=1.4,
            transform=steps.transAxes,
        )
        steps.add_patch(box)
        steps.text(
            0.025,
            y + 0.081,
            heading,
            transform=steps.transAxes,
            color=color,
            fontweight="bold",
        )
        steps.text(
            0.025,
            y + 0.055,
            textwrap.fill(str(body), width=66),
            transform=steps.transAxes,
            fontsize=8.1,
            color=INK,
            va="top",
        )
    steps.text(
        0.0,
        0.002,
        "Retained result: measurement succeeded · geometry model rejected\n"
        "RX1 position not reported",
        transform=steps.transAxes,
        fontsize=9.2,
        fontweight="bold",
        color=RED,
        va="bottom",
    )
    _save(fig, path)


def render_report(snapshot_path: Path, output_directory: Path) -> dict[str, str]:
    snapshot = load_snapshot(snapshot_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    _configure_style()
    renderers = (_render_integrity, _render_model_gate, _render_identifiability)
    for filename, renderer in zip(FIGURE_NAMES, renderers, strict=True):
        renderer(snapshot, output_directory / filename)
    return {filename: _sha256(output_directory / filename) for filename in FIGURE_NAMES}


def _manifest_document(
    snapshot_path: Path,
    hashes: Mapping[str, str],
    figure_directory: Path,
) -> dict[str, Any]:
    figures = []
    for filename in FIGURE_NAMES:
        path = figure_directory / filename
        if hashes[filename] != _sha256(path):
            raise ReportError(f"figure hash changed while building manifest: {filename}")
        width, height = _png_dimensions(path)
        figures.append(
            {
                "path": f"docs/localization/png/{filename}",
                "sha256": hashes[filename],
                "byte_size": path.stat().st_size,
                "width_px": width,
                "height_px": height,
            }
        )
    return {
        "schema": 1,
        "report_kind": "rx1_reference_negative_localization_figures",
        "renderer": "scripts/render_rx1_reference_report.py",
        "renderer_sha256": _sha256(Path(__file__).resolve()),
        "matplotlib_version": matplotlib.__version__,
        "snapshot": {
            "path": "docs/localization/data/rx1-reference-20260825-a-report-snapshot.json",
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


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ReportError(f"generated figure is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _check(snapshot_path: Path, output_directory: Path, manifest_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="smateway-rx1-report-") as temporary:
        temporary_directory = Path(temporary)
        hashes = render_report(snapshot_path, temporary_directory)
        for filename in FIGURE_NAMES:
            committed = output_directory / filename
            generated = temporary_directory / filename
            if not committed.is_file():
                raise ReportError(f"missing committed figure: {committed}")
            if committed.read_bytes() != generated.read_bytes():
                raise ReportError(f"figure is not byte-reproducible: {filename}")
        expected_manifest = _manifest_document(snapshot_path, hashes, temporary_directory)
        try:
            observed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReportError(f"cannot load figure manifest: {error}") from error
        if observed_manifest != expected_manifest:
            raise ReportError("committed RX1 figure manifest is stale")


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
    except (OSError, ReportError, ValueError) as error:
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
