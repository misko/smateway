#!/usr/bin/env python3
"""Render deterministic figures for the rejected experimental 5.8 GHz Hexcal run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

REPOSITORY = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = REPOSITORY / "docs/hexray_tx_in_middle_calibration/data"
DEFAULT_SUMMARY = DATA_DIRECTORY / "hexcal-v2.4-5g8-experiment-summary.json"
DEFAULT_PHASE = DATA_DIRECTORY / "hexcal-v2.4-5g8-phase-leakage-results.json"
DEFAULT_OUTPUT = REPOSITORY / "docs/hexray_tx_in_middle_calibration/png"
DEFAULT_MANIFEST = DATA_DIRECTORY / "hexcal-v2.4-5g8-results-figures-manifest.json"
FIGURE_NAMES = (
    "fig09_v24_5g8_gain_screen.png",
    "fig10_v24_5g8_phase_signature.png",
    "fig11_v24_5g8_failure_localization.png",
)
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
ANTENNAS = tuple(f"ANT{index}" for index in range(1, 7))


class ResultFigureError(RuntimeError):
    """A 5.8 GHz result input or committed rendering is inconsistent."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ResultFigureError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ResultFigureError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultFigureError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ResultFigureError(f"{label} must be finite")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY).as_posix()
    except ValueError:
        return path.name


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResultFigureError(f"cannot load {label} {path}: {error}") from error


def load_results(
    summary_path: Path, phase_path: Path
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    summary = _read_json(summary_path, "5.8 GHz summary")
    phase = _read_json(phase_path, "5.8 GHz phase diagnostic")
    if summary.get("schema") != 1 or phase.get("schema") != 1:
        raise ResultFigureError("both result inputs must use schema 1")
    if summary.get("result_kind") != "hexcal_v2_4_experimental_5g8_rejected_calibration_summary":
        raise ResultFigureError("summary kind is unsupported")
    if summary.get("status") != "rejected_before_timing_or_calibration":
        raise ResultFigureError("5.8 GHz summary must retain its rejected status")
    if phase.get("status") != "exploratory_phase_signature_reproduced_but_calibration_rejected":
        raise ResultFigureError("phase diagnostic must retain its exploratory rejected status")
    if summary.get("center_frequency_hz") != 5_800_000_000:
        raise ResultFigureError("summary center frequency must remain exact 5.8 GHz")
    if phase.get("center_frequency_hz") != 5_800_000_000:
        raise ResultFigureError("phase center frequency must remain exact 5.8 GHz")

    hardware = _mapping(summary.get("hardware"), "hardware")
    if tuple(_sequence(hardware.get("array_order"), "array order")) != ANTENNAS:
        raise ResultFigureError("array order must remain ANT1 through ANT6")
    scope = _mapping(summary.get("experimental_scope"), "experimental scope")
    if scope.get("officially_qualified_operating_point") is not False:
        raise ResultFigureError("experimental AD9363 scope may not be promoted")
    if scope.get("calibration_coefficients_released") is not False:
        raise ResultFigureError("the rejected run may not release coefficients")

    stimulus = _mapping(summary["stimulus"], "stimulus")
    gains = tuple(_sequence(stimulus["tx_hardware_gains_db"], "gains"))
    if gains != (-35, -30, -25, -20, -15, -10):
        raise ResultFigureError("TX gain ladder differs from the frozen screen")
    sweeps = tuple(_mapping(item, "sweep") for item in _sequence(summary.get("sweeps"), "sweeps"))
    if len(sweeps) != 4:
        raise ResultFigureError("summary must retain all four 5.8 GHz screens")
    for sweep in sweeps:
        if COMMIT.fullmatch(str(sweep.get("source_commit"))) is None:
            raise ResultFigureError("sweep source commit is malformed")
        for field in ("qualification_ledger_sha256", "firmware_evidence_sha256"):
            if SHA256.fullmatch(str(sweep.get(field))) is None:
                raise ResultFigureError(f"sweep {field} is malformed")
        peaks = _sequence(sweep.get("peak_abs_component_counts_by_gain_rx1_rx2"), "peaks")
        if len(peaks) != len(gains):
            raise ResultFigureError("each sweep must contain one peak pair per gain")
        for pair in peaks:
            values = _sequence(pair, "peak pair")
            if len(values) != 2 or any(_number(value, "peak") < 0 for value in values):
                raise ResultFigureError("each peak pair must contain two nonnegative values")
        if sweep.get("status") != "failed" or sweep.get("final_exact_mute") != "passed":
            raise ResultFigureError("each screen must remain failed with final mute passed")

    discriminator = _mapping(summary.get("tx2_antenna_discriminator"), "TX2 discriminator")
    if discriminator.get("removed_rx2_peak_counts_at_minus_10_db") != 389:
        raise ResultFigureError("TX2 antenna removal result changed")
    attached_peaks = _sequence(
        discriminator.get("attached_rx2_peak_counts_at_minus_10_db"), "attached peaks"
    )
    if attached_peaks != [376, 390]:
        raise ResultFigureError("attached TX2 comparison changed")
    findings = _mapping(summary.get("findings"), "findings")
    if findings.get("all_off_amplitude_contrast_available") is not False:
        raise ResultFigureError("ALL_OFF contrast failure may not be promoted")
    if findings.get("timing_qualification_started") is not False:
        raise ResultFigureError("timing qualification was not started")

    phase_reference = _mapping(summary.get("phase_diagnostic"), "phase reference")
    if phase_reference.get("sha256") != _sha256(phase_path):
        raise ResultFigureError("phase diagnostic hash differs from the summary")
    if phase_reference.get("trial_count") != 12 or phase.get("trial_count") != 12:
        raise ResultFigureError("phase diagnostic must retain 12 rejected trials")
    if _mapping(phase.get("conclusions"), "phase conclusions").get(
        "may_be_used_as_array_calibration"
    ) is not False:
        raise ResultFigureError("phase signature may not be used as calibration")
    rows = tuple(
        _mapping(item, "phase gain row")
        for item in _sequence(phase.get("per_gain_reproducibility"), "phase rows")
    )
    if tuple(row.get("tx_hardware_gain_db") for row in rows) != gains:
        raise ResultFigureError("phase gain rows differ from the frozen ladder")
    for row in rows:
        states = _sequence(row.get("phase_by_state_relative_to_ant1"), "phase states")
        if tuple(_mapping(item, "phase state").get("name") for item in states) != ANTENNAS:
            raise ResultFigureError("phase state order differs from ANT1 through ANT6")
    return summary, phase


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "axes.grid": True,
            "grid.alpha": 0.24,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "axes.facecolor": "#fbfcfe",
        }
    )


def _save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(
        path,
        dpi=160,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": "smateway deterministic Hexcal 5.8 GHz renderer"},
    )
    plt.close(fig)


def _render_gain_screen(summary: Mapping[str, Any], path: Path) -> None:
    gains = np.asarray(summary["stimulus"]["tx_hardware_gains_db"], dtype=float)
    sweeps = tuple(_mapping(item, "sweep") for item in summary["sweeps"])[1:]
    styles = (
        ("screen A · TX2 antenna attached", "#0072B2", "o-"),
        ("screen B · TX2 antenna attached", "#56B4E9", "s--"),
        ("rerun · TX2 antenna removed", "#D55E00", "D-"),
    )
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.2), constrained_layout=True)
    for receiver, axis in enumerate(axes):
        for sweep, (label, color, style) in zip(sweeps, styles, strict=True):
            peaks = np.asarray(sweep["peak_abs_component_counts_by_gain_rx1_rx2"], dtype=float)
            axis.plot(gains, peaks[:, receiver], style, color=color, linewidth=2, label=label)
        axis.set_yscale("log")
        axis.set_xticks(gains)
        axis.set_xlabel("TX1 hardware gain (dB)")
        axis.set_ylabel("Peak absolute ADC component (counts)")
        axis.set_title(f"RX{receiver + 1} headroom observation")
        axis.legend(fontsize=9)
    axes[1].annotate(
        "389 after removal\nvs 376 / 390 attached",
        xy=(-10, 389),
        xytext=(-22, 520),
        arrowprops={"arrowstyle": "->", "color": "#8B1A1A", "linewidth": 1.5},
        color="#8B1A1A",
        fontweight="bold",
    )
    fig.suptitle(
        "Exact 5.8 GHz screen — removing the TX2 antenna did not remove RX2 coupling",
        fontsize=16,
        fontweight="bold",
    )
    fig.text(
        0.5,
        -0.015,
        "All conditions retained ADC headroom and final exact mute, but every screen failed "
        "because the ALL_OFF amplitude marker was not usable.",
        ha="center",
        color="#8B1A1A",
        fontweight="bold",
    )
    _save(fig, path)


def _render_phase_signature(phase: Mapping[str, Any], path: Path) -> None:
    rows = tuple(_mapping(item, "phase row") for item in phase["per_gain_reproducibility"])
    x = np.arange(1, 7, dtype=float)
    colors = plt.cm.viridis(np.linspace(0.08, 0.92, len(rows)))
    fig, axis = plt.subplots(figsize=(13, 7.4), constrained_layout=False)
    fig.subplots_adjust(left=0.09, right=0.98, bottom=0.17, top=0.84)
    for row, color in zip(rows, colors, strict=True):
        states = tuple(_mapping(item, "state") for item in row["phase_by_state_relative_to_ant1"])
        means = np.asarray([_number(item["mean_deg"], "mean phase") for item in states])
        spread = np.asarray([_number(item["circular_std_deg"], "phase spread") for item in states])
        label = f"TX {row['tx_hardware_gain_db']:.0f} dB"
        axis.errorbar(
            x,
            means,
            yerr=spread,
            marker="o",
            linewidth=1.8,
            capsize=3,
            color=color,
            label=label,
        )
    axis.axhline(0, color="#444444", linewidth=0.8)
    axis.set_xticks(x, ANTENNAS)
    axis.set_ylim(-180, 180)
    axis.set_yticks(np.arange(-180, 181, 60))
    axis.set_xlabel("Selector state")
    axis.set_ylabel("Leakage-subtracted phase relative to ANT1 (degrees)")
    axis.set_title("Two rejected screens still contain a repeatable phase-only fingerprint")
    axis.legend(ncol=3, loc="upper left")
    fig.suptitle(
        "Exploratory 5.8 GHz phase signature — diagnostic only, not calibration",
        fontsize=16,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.035,
        "Phase alignment is inferred from the cyclic RF record after common ALL_OFF subtraction; "
        "the amplitude marker and independent GPIO timing gates did not pass.",
        ha="center",
        color="#8B1A1A",
        fontweight="bold",
    )
    _save(fig, path)


def _box(
    axis: plt.Axes,
    x: float,
    y: float,
    width: float,
    height: float,
    text: str,
    color: str,
) -> None:
    axis.add_patch(
        plt.Rectangle((x, y), width, height, facecolor=color, edgecolor="#333333", linewidth=1.4)
    )
    axis.text(x + width / 2, y + height / 2, text, ha="center", va="center", fontsize=10)


def _arrow(
    axis: plt.Axes, start: tuple[float, float], end: tuple[float, float], color: str
) -> None:
    axis.annotate(
        "",
        xy=end,
        xytext=start,
        arrowprops={"arrowstyle": "->", "linewidth": 2, "color": color},
    )


def _render_failure_localization(summary: Mapping[str, Any], path: Path) -> None:
    discriminator = _mapping(summary["tx2_antenna_discriminator"], "discriminator")
    fig, axis = plt.subplots(figsize=(14, 8.2), constrained_layout=True)
    axis.set_axis_off()
    _box(axis, 0.03, 0.42, 0.15, 0.18, "Pluto TX1\n5.8001 GHz tone", "#FFF0CC")
    _box(axis, 0.28, 0.68, 0.19, 0.16, "Pluto internal\nTX1 → RX2 leakage", "#FADBD8")
    _box(axis, 0.28, 0.42, 0.19, 0.16, "TX1 antenna → RX2\ncable / common path", "#FADBD8")
    _box(axis, 0.28, 0.16, 0.19, 0.16, "Selector ALL_OFF\nfinite isolation / PCB", "#FADBD8")
    _box(axis, 0.59, 0.42, 0.15, 0.18, "Pluto RX2\ncoherent tone", "#DDEEFF")
    _box(axis, 0.81, 0.42, 0.16, 0.18, "ALL_OFF marker\nmasked → REJECT", "#F4CCCC")
    for y in (0.76, 0.50, 0.24):
        _arrow(axis, (0.18, 0.51), (0.28, y), "#A33A2B")
        _arrow(axis, (0.47, y), (0.59, 0.51), "#A33A2B")
    _arrow(axis, (0.74, 0.51), (0.81, 0.51), "#333333")

    _box(axis, 0.03, 0.02, 0.29, 0.10, "TX2-antenna reradiation: DEPRIORITIZED", "#DDF3E4")
    _box(axis, 0.36, 0.02, 0.27, 0.10, "External Wi-Fi: DEPRIORITIZED", "#DDF3E4")
    _box(axis, 0.67, 0.02, 0.30, 0.10, "Exact mute after every sweep: PASS", "#DDF3E4")
    axis.text(
        0.175,
        0.135,
        f"RX2 −10 dB peak: {discriminator['removed_rx2_peak_counts_at_minus_10_db']} removed\n"
        "vs 376 / 390 attached",
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        color="#176A38",
    )
    axis.text(
        0.495,
        0.135,
        "Tone follows commanded TX1 gain\nand repeats in phase",
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        color="#176A38",
    )
    axis.text(
        0.5,
        0.94,
        "What the corrected rerun rules out — and what remains unresolved",
        ha="center",
        fontsize=17,
        fontweight="bold",
    )
    axis.text(
        0.5,
        0.89,
        "Next discriminator: TX1 on, Pluto RX2 terminated at its own reference plane",
        ha="center",
        fontsize=12,
        color="#8B1A1A",
        fontweight="bold",
    )
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    _save(fig, path)


def render_report(
    summary_path: Path, phase_path: Path, output_directory: Path, manifest_path: Path
) -> Mapping[str, Any]:
    summary, phase = load_results(summary_path, phase_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    with plt.rc_context():
        plt.rcdefaults()
        _style()
        _render_gain_screen(summary, output_directory / FIGURE_NAMES[0])
        _render_phase_signature(phase, output_directory / FIGURE_NAMES[1])
        _render_failure_localization(summary, output_directory / FIGURE_NAMES[2])
    figures = []
    for name in FIGURE_NAMES:
        path = output_directory / name
        figures.append(
            {
                "path": f"docs/hexray_tx_in_middle_calibration/png/{name}",
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
            }
        )
    manifest = {
        "schema": 1,
        "renderer": _relative(Path(__file__)),
        "renderer_sha256": _sha256(Path(__file__)),
        "summary": {"path": _relative(summary_path), "sha256": _sha256(summary_path)},
        "phase_diagnostic": {"path": _relative(phase_path), "sha256": _sha256(phase_path)},
        "matplotlib_version": matplotlib.__version__,
        "figures": figures,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def check_report(
    summary_path: Path, phase_path: Path, output_directory: Path, manifest_path: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="hexcal-v24-5g8-") as temporary:
        root = Path(temporary)
        expected = render_report(summary_path, phase_path, root, root / "manifest.json")
        try:
            actual = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ResultFigureError(f"cannot load committed 5.8 GHz manifest: {error}") from error
        if actual != expected:
            raise ResultFigureError("committed 5.8 GHz figure manifest is stale")
        for name in FIGURE_NAMES:
            if (root / name).read_bytes() != (output_directory / name).read_bytes():
                raise ResultFigureError(f"committed 5.8 GHz result figure is stale: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--phase", type=Path, default=DEFAULT_PHASE)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.check:
            check_report(args.summary, args.phase, args.output_directory, args.manifest)
            print(json.dumps({"status": "passed", "figures_checked": len(FIGURE_NAMES)}))
        else:
            manifest = render_report(
                args.summary, args.phase, args.output_directory, args.manifest
            )
            print(
                json.dumps(
                    {
                        "status": "rendered",
                        "figures": [item["path"] for item in manifest["figures"]],
                        "manifest": str(args.manifest),
                    },
                    sort_keys=True,
                )
            )
    except ResultFigureError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
