#!/usr/bin/env python3
"""Render deterministic figures for the schedule-alignment validation report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

REPOSITORY = Path(__file__).resolve().parents[1]
REPORT_DIRECTORY = REPOSITORY / "docs/schedule_alignment_red_green"
DEFAULT_EVIDENCE = REPORT_DIRECTORY / "data/capture-evidence.json"
DEFAULT_BENCHMARK = REPORT_DIRECTORY / "data/false-lock-verification.json"
DEFAULT_OUTPUT = REPORT_DIRECTORY / "png"
DEFAULT_MANIFEST = REPORT_DIRECTORY / "data/figures-manifest.json"
FIGURE_NAMES = (
    "fig01_false_lock_recovery_and_cost.png",
    "fig02_v2_frequency_repeat_matrix.png",
    "fig03_legacy_vs_v2_decisions.png",
    "fig04_exact_tone_quality_distributions.png",
)
FREQUENCIES_HZ = tuple(range(2_100_000_000, 2_500_000_001, 100_000_000))
TAIL_ARTIFACT = "841b1dd8df2e4370a29a562680f4af03"


class ReportFigureError(RuntimeError):
    """Committed evidence or a rendered report figure is inconsistent."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReportFigureError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ReportFigureError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportFigureError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ReportFigureError(f"{label} must be finite")
    return result


def _read(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReportFigureError(f"cannot read {label} {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPOSITORY).as_posix()


def load_inputs(
    evidence_path: Path, benchmark_path: Path
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    evidence = _read(evidence_path, "captured evidence")
    benchmark = _read(benchmark_path, "false-lock benchmark")
    captures = tuple(
        _mapping(row, "capture row") for row in _sequence(evidence.get("captures"), "captures")
    )
    if (
        evidence.get("schema") != 1
        or evidence.get("evidence_kind") != "schedule_alignment_v2_captured_validation"
    ):
        raise ReportFigureError("captured evidence identity is unsupported")
    if len(captures) != 150 or len({row.get("artifact_id") for row in captures}) != 150:
        raise ReportFigureError("captured evidence must retain 150 distinct artifacts")
    if sum(row.get("v2_status") == "admitted" for row in captures) != 145:
        raise ReportFigureError("captured evidence must retain 145 strict admissions")
    if sum(row.get("v2_status") == "quarantined" for row in captures) != 5:
        raise ReportFigureError("captured evidence must retain five quarantines")
    if (
        benchmark.get("schema") != 1
        or benchmark.get("artifact_id") != "be64aa4b22f9436c8ff25547a3589b98"
    ):
        raise ReportFigureError("false-lock benchmark identity is unsupported")
    searches = tuple(
        _mapping(row, "benchmark search")
        for row in _sequence(benchmark.get("searches"), "benchmark searches")
    )
    if tuple(row.get("mode") for row in searches) != (
        "exhaustive_fine",
        "global_refined",
        "transition_seeded",
    ):
        raise ReportFigureError("false-lock benchmark must retain three corrected searches")
    return evidence, benchmark


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "axes.grid": True,
            "grid.alpha": 0.22,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "axes.facecolor": "#fbfcfe",
        }
    )


def _save(fig: Figure, path: Path) -> None:
    fig.savefig(
        path,
        dpi=170,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": "smateway deterministic schedule-alignment renderer"},
    )
    plt.close(fig)


def _render_false_lock(benchmark: Mapping[str, Any], path: Path) -> None:
    searches = tuple(_mapping(row, "search") for row in benchmark["searches"])
    decoder = _mapping(benchmark["independent_decoder"], "independent decoder")
    legacy = _mapping(benchmark["legacy_greedy"], "legacy result")
    labels = [
        "Legacy\ngreedy",
        "Exhaustive\nfine",
        "Global\nrefined",
        "Transition\nseeded",
        "Independent\ndecoder",
    ]
    phases = [
        _number(legacy["marker_phase_ms"], "legacy marker"),
        *(_number(row["marker_phase_ms"], "search marker") for row in searches),
        _number(decoder["marker_phase_ms"], "decoder marker"),
    ]
    colors = ["#C0392B", "#5B5B5B", "#0072B2", "#009E73", "#6F42C1"]
    fig, axes = plt.subplots(1, 2, figsize=(14.5, 6.3), constrained_layout=True)

    axis = axes[0]
    x = np.arange(len(labels))
    axis.scatter(x, phases, s=115, color=colors, zorder=3)
    axis.axhline(phases[-1], color="#6F42C1", linestyle="--", linewidth=1.4)
    axis.set_xticks(x, labels)
    axis.set_ylabel("Selected marker phase (ms)")
    axis.set_ylim(35, 160)
    axis.set_title("The false lock moved the marker by 102.7 ms")
    for index, phase in enumerate(phases):
        axis.annotate(
            f"{phase:.3f}",
            (index, phase),
            xytext=(0, 9),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )
    axis.text(
        0.03,
        0.05,
        "Legacy score 0.7970\nCorrected score 0.99999965",
        transform=axis.transAxes,
        color="#8B1A1A",
        fontweight="bold",
    )

    axis = axes[1]
    names = ["Exhaustive", "Global", "Transition"]
    runtimes = np.asarray([_number(row["elapsed_s"], "runtime") for row in searches])
    candidates = np.asarray(
        [_number(row["candidate_count"], "candidate count") for row in searches]
    )
    bars = axis.bar(np.arange(3) - 0.18, runtimes, 0.36, color="#0072B2", label="Runtime (s)")
    axis.set_yscale("log")
    axis.set_ylabel("Runtime on devpi (s, log scale)", color="#0072B2")
    axis.set_xticks(np.arange(3), names)
    axis.tick_params(axis="y", labelcolor="#0072B2")
    twin = axis.twinx()
    twin.grid(False)
    twin.bar(np.arange(3) + 0.18, candidates, 0.36, color="#E69F00", label="Candidates")
    twin.set_yscale("log")
    twin.set_ylabel("Candidates evaluated (log scale)", color="#A76700")
    twin.tick_params(axis="y", labelcolor="#A76700")
    for bar, runtime in zip(bars, runtimes, strict=True):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            runtime * 1.14,
            f"{runtime:.2f}s",
            ha="center",
            color="#005B8F",
            fontsize=9,
        )
    for index, count in enumerate(candidates):
        twin.text(
            index + 0.18, count * 1.14, f"{int(count):,}", ha="center", color="#8A5700", fontsize=9
        )
    speedup = runtimes[0] / runtimes[2]
    axis.set_title(f"Transition seed: {speedup:.0f}× faster on this artifact")
    handles = [bars, Rectangle((0, 0), 1, 1, color="#E69F00")]
    axis.legend(handles, ["Runtime", "Candidates"], loc="lower left")

    fig.suptitle("Captured false-lock recovery and search cost", fontsize=17, fontweight="bold")
    _save(fig, path)


def _matrix(
    captures: Sequence[Mapping[str, Any]], cohort: str
) -> tuple[np.ndarray, list[Mapping[str, Any]]]:
    rows = sorted(
        (row for row in captures if row.get("cohort") == cohort),
        key=lambda row: (int(row["repeat_index"]), int(row["center_frequency_hz"])),
    )
    repeat_count = 5 if cohort == "focused" else 25
    values = np.full((repeat_count, len(FREQUENCIES_HZ)), np.nan)
    for row in rows:
        repeat = int(row["repeat_index"]) - 1
        frequency = FREQUENCIES_HZ.index(int(row["center_frequency_hz"]))
        values[repeat, frequency] = math.log10(_number(row["residual_fraction"], "residual"))
    if np.isnan(values).any():
        raise ReportFigureError(f"{cohort} matrix is incomplete")
    return values, rows


def _render_matrix(evidence: Mapping[str, Any], path: Path) -> None:
    captures = tuple(_mapping(row, "capture") for row in evidence["captures"])
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(14.5, 8.2),
        gridspec_kw={"width_ratios": [1, 2.15]},
    )
    fig.subplots_adjust(left=0.06, right=0.87, bottom=0.14, top=0.86, wspace=0.14)
    image = None
    for axis, cohort, title in zip(
        axes,
        ("focused", "historical"),
        ("Focused repeats 1–5", "Broadband repeats 1–25 · low-band slice"),
        strict=True,
    ):
        values, rows = _matrix(captures, cohort)
        image = axis.imshow(
            values, aspect="auto", cmap="viridis_r", vmin=-8.0, vmax=-5.5, interpolation="nearest"
        )
        axis.set_xticks(np.arange(5), [f"{value / 1e9:.1f}" for value in FREQUENCIES_HZ])
        axis.set_xlabel("Center frequency (GHz)")
        axis.set_yticks(
            np.arange(values.shape[0]), [str(index) for index in range(1, values.shape[0] + 1)]
        )
        axis.set_ylabel("Repeat")
        axis.set_title(title)
        axis.grid(False)
        for row in rows:
            x = FREQUENCIES_HZ.index(int(row["center_frequency_hz"]))
            y = int(row["repeat_index"]) - 1
            if row.get("v2_status") == "quarantined":
                axis.plot(x, y, marker="x", markersize=10, markeredgewidth=2.2, color="#D62728")
            if row.get("artifact_id") == TAIL_ARTIFACT:
                axis.add_patch(
                    Rectangle(
                        (x - 0.46, y - 0.46),
                        0.92,
                        0.92,
                        fill=False,
                        edgecolor="#FFBF00",
                        linewidth=2.5,
                    )
                )
    if image is None:
        raise ReportFigureError("matrix rendering failed")
    colorbar = fig.colorbar(image, ax=axes, shrink=0.92, pad=0.025)
    colorbar.set_label("log₁₀(alignment residual fraction) · lower is better")
    fig.suptitle("All 150 v2 fits by frequency and repeat", fontsize=17, fontweight="bold")
    fig.text(
        0.5,
        0.035,
        "Red × = timing quarantine (one rejected marker); amber outline = "
        "admitted quality-tail case 841b1d…",
        ha="center",
        color="#6B3E00",
        fontweight="bold",
    )
    _save(fig, path)


def _render_decisions(evidence: Mapping[str, Any], path: Path) -> None:
    captures = tuple(_mapping(row, "capture") for row in evidence["captures"])
    fig, axes = plt.subplots(1, 2, figsize=(15, 6.5))
    fig.subplots_adjust(left=0.06, right=0.98, bottom=0.19, top=0.84, wspace=0.12)
    colors = {"good": "#009E73", "old_bad": "#999999", "quarantine": "#D55E00"}
    for axis, cohort, total in zip(axes, ("focused", "historical"), (5, 25), strict=True):
        rows = [row for row in captures if row.get("cohort") == cohort]
        x = np.arange(5, dtype=float)
        legacy_pass = np.asarray(
            [
                sum(
                    row["center_frequency_hz"] == frequency
                    and row["legacy_manifest_outcome"] == "quality_passed"
                    for row in rows
                )
                for frequency in FREQUENCIES_HZ
            ]
        )
        v2_admit = np.asarray(
            [
                sum(
                    row["center_frequency_hz"] == frequency and row["v2_status"] == "admitted"
                    for row in rows
                )
                for frequency in FREQUENCIES_HZ
            ]
        )
        width = 0.34
        axis.bar(x - width / 2, legacy_pass, width, color=colors["good"], label="Pass / admit")
        axis.bar(
            x - width / 2,
            total - legacy_pass,
            width,
            bottom=legacy_pass,
            color=colors["old_bad"],
            label="Legacy reject",
        )
        axis.bar(x + width / 2, v2_admit, width, color=colors["good"])
        axis.bar(
            x + width / 2,
            total - v2_admit,
            width,
            bottom=v2_admit,
            color=colors["quarantine"],
            label="v2 quarantine",
        )
        for index in range(5):
            axis.text(
                index - width / 2,
                total + total * 0.025,
                "old",
                ha="center",
                fontsize=8,
                color="#555555",
            )
            axis.text(
                index + width / 2,
                total + total * 0.025,
                "v2",
                ha="center",
                fontsize=8,
                color="#555555",
            )
        axis.set_xticks(x, [f"{value / 1e9:.1f}" for value in FREQUENCIES_HZ])
        axis.set_xlabel("Center frequency (GHz)")
        axis.set_ylabel("Captures")
        axis.set_ylim(0, total * 1.16)
        axis.set_title("Focused 5-pass set" if cohort == "focused" else "Historical 25-pass set")
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.5, 0.005))
    fig.suptitle(
        "Frozen capture-time decisions versus corrected v2 reanalysis",
        fontsize=17,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.085,
        "The gray segments are stale analyzer outcomes preserved for provenance—"
        "not RF failures or new captures.",
        ha="center",
        color="#555555",
        fontweight="bold",
    )
    _save(fig, path)


def _ecdf(axis: Axes, values: Sequence[float], label: str, color: str) -> None:
    ordered = np.sort(np.asarray(values, dtype=float))
    y = np.arange(1, len(ordered) + 1, dtype=float) / len(ordered)
    axis.step(
        ordered, y, where="post", linewidth=2.1, label=f"{label} (n={len(ordered)})", color=color
    )


def _render_quality(evidence: Mapping[str, Any], path: Path) -> None:
    captures = tuple(
        _mapping(row, "capture")
        for row in evidence["captures"]
        if row.get("v2_status") == "admitted"
    )
    metrics = (
        ("residual_fraction", "Alignment residual fraction", True),
        ("minimum_state_detection_snr_db", "Worst-state exact-tone SNR (dB)", False),
        ("maximum_state_cycle_phase_std_deg", "Worst-state cycle phase spread (°)", True),
        ("decoder_marker_error_ms", "Marker error versus decoder (ms)", False),
    )
    fig, axes = plt.subplots(2, 2, figsize=(14.5, 9))
    fig.subplots_adjust(left=0.07, right=0.98, bottom=0.10, top=0.88, hspace=0.27, wspace=0.14)
    for axis, (field, label, log_scale) in zip(axes.flat, metrics, strict=True):
        for cohort, title, color in (
            ("focused", "Focused", "#0072B2"),
            ("historical", "Historical", "#E69F00"),
        ):
            values = [_number(row[field], field) for row in captures if row.get("cohort") == cohort]
            _ecdf(axis, values, title, color)
        if log_scale:
            axis.set_xscale("log")
        axis.set_xlabel(label)
        axis.set_ylabel("Empirical cumulative fraction")
        axis.set_ylim(0, 1.02)
        axis.legend(loc="lower right")
    tail = next(row for row in captures if row.get("artifact_id") == TAIL_ARTIFACT)
    axes[0, 0].axvline(
        _number(tail["residual_fraction"], "tail residual"), color="#CC79A7", linestyle="--"
    )
    axes[0, 0].text(
        0.97,
        0.15,
        "841b1d…\nadmitted tail",
        transform=axes[0, 0].transAxes,
        ha="right",
        color="#8F3E75",
        fontweight="bold",
    )
    fig.suptitle(
        "Alignment, timing, and exact-tone quality · 145 strict admissions",
        fontsize=17,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.025,
        "Each capture contributes one worst-state summary across ANT1–ANT8; "
        "values are descriptive and correlated within a capture.",
        ha="center",
        color="#555555",
    )
    _save(fig, path)


def render_report(
    evidence_path: Path,
    benchmark_path: Path,
    output_directory: Path,
    manifest_path: Path,
) -> Mapping[str, Any]:
    evidence, benchmark = load_inputs(evidence_path, benchmark_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    with plt.rc_context():
        plt.rcdefaults()
        _style()
        _render_false_lock(benchmark, output_directory / FIGURE_NAMES[0])
        _render_matrix(evidence, output_directory / FIGURE_NAMES[1])
        _render_decisions(evidence, output_directory / FIGURE_NAMES[2])
        _render_quality(evidence, output_directory / FIGURE_NAMES[3])
    figures = [
        {
            "path": f"docs/schedule_alignment_red_green/png/{name}",
            "sha256": _sha256(output_directory / name),
            "byte_size": (output_directory / name).stat().st_size,
        }
        for name in FIGURE_NAMES
    ]
    manifest = {
        "schema": 1,
        "renderer": _relative(Path(__file__)),
        "renderer_sha256": _sha256(Path(__file__)),
        "captured_evidence": {"path": _relative(evidence_path), "sha256": _sha256(evidence_path)},
        "false_lock_benchmark": {
            "path": _relative(benchmark_path),
            "sha256": _sha256(benchmark_path),
        },
        "matplotlib_version": matplotlib.__version__,
        "figures": figures,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def check_report(
    evidence_path: Path,
    benchmark_path: Path,
    output_directory: Path,
    manifest_path: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="schedule-alignment-report-") as temporary:
        root = Path(temporary)
        expected = render_report(evidence_path, benchmark_path, root, root / "manifest.json")
        actual = _read(manifest_path, "committed figure manifest")
        if actual != expected:
            raise ReportFigureError("committed figure manifest is stale")
        for name in FIGURE_NAMES:
            if (root / name).read_bytes() != (output_directory / name).read_bytes():
                raise ReportFigureError(f"committed figure is stale: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.check:
            check_report(args.evidence, args.benchmark, args.output_directory, args.manifest)
            print(json.dumps({"status": "passed", "figures_checked": len(FIGURE_NAMES)}))
        else:
            manifest = render_report(
                args.evidence, args.benchmark, args.output_directory, args.manifest
            )
            print(
                json.dumps(
                    {
                        "status": "rendered",
                        "figure_count": len(manifest["figures"]),
                        "manifest": str(args.manifest),
                    }
                )
            )
    except (ReportFigureError, OSError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
