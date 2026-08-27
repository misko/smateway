#!/usr/bin/env python3
"""Render deterministic PNGs from the committed Hexcal v2.2 result snapshot."""

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
DEFAULT_RESULT = REPOSITORY / "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.2-results.json"
DEFAULT_OUTPUT = REPOSITORY / "docs/hexray_tx_in_middle_calibration/png"
DEFAULT_MANIFEST = (
    REPOSITORY
    / "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.2-results-figures-manifest.json"
)
FIGURE_NAMES = (
    "fig06_measured_complex_corrections.png",
    "fig07_raw_vs_heldout_calibration.png",
    "fig08_v22_qualification_evidence.png",
)
SHA256 = re.compile(r"[0-9a-f]{64}")
ANTENNAS = tuple(f"ANT{index}" for index in range(1, 7))
COLORS = ("#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9")


class ResultFigureError(RuntimeError):
    """The committed result snapshot or rendered report is inconsistent."""


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


def load_result(path: Path) -> Mapping[str, Any]:
    try:
        root = _mapping(json.loads(path.read_text(encoding="utf-8")), "result")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResultFigureError(f"cannot load result snapshot {path}: {error}") from error
    if root.get("schema") != 1:
        raise ResultFigureError("result schema must be 1")
    if root.get("result_kind") != "hexcal_v2_2_2g4_tx1_center_calibration_result":
        raise ResultFigureError("result kind is unsupported")
    if root.get("protocol_id") != "hexcal-v2.2-2g4-stimulus" or root.get("status") != "passed":
        raise ResultFigureError("result must be the passed v2.2 protocol")
    if SHA256.fullmatch(str(root.get("source_commit"))) is not None:
        raise ResultFigureError("source commit must be a 40-character Git object ID, not SHA-256")
    if re.fullmatch(r"[0-9a-f]{40}", str(root.get("source_commit"))) is None:
        raise ResultFigureError("source commit is malformed")

    hardware = _mapping(root.get("hardware"), "hardware")
    if tuple(_sequence(hardware.get("array_order"), "array order")) != ANTENNAS:
        raise ResultFigureError("array order must be ANT1 through ANT6")
    if (
        hardware.get("array_direction") != "clockwise"
        or hardware.get("forward_reference") != "ANT1"
    ):
        raise ResultFigureError("array orientation differs from the physical declaration")

    for section_name, fields in {
        "firmware": ("evidence_sha256", "bin_sha256", "elf_sha256", "full_flash_sha256"),
        "stimulus_qualification": ("sha256",),
        "timing_qualification": ("manifest_sha256", "analysis_sha256"),
        "calibration_run": ("manifest_sha256", "calibration_sha256", "audit_sha256"),
    }.items():
        section = _mapping(root.get(section_name), section_name)
        for field in fields:
            if SHA256.fullmatch(str(section.get(field))) is None:
                raise ResultFigureError(f"{section_name}.{field} is not SHA-256")

    stimulus = _mapping(root.get("stimulus_qualification"), "stimulus qualification")
    if stimulus.get("fixed_calibration_receiver_gain_db") != 20:
        raise ResultFigureError("calibration RX gain must remain 20 dB")
    if _number(stimulus.get("selected_tx_hardware_gain_db"), "selected TX gain") != -10.0:
        raise ResultFigureError("selected TX gain must remain -10 dB")

    timing = _mapping(root.get("timing_qualification"), "timing qualification")
    if timing.get("sample_rate_hz") != 2_000_000 or timing.get("receiver_gain_db") != 30:
        raise ResultFigureError("timing must remain 2 MS/s at RX30")
    replicates = tuple(
        _mapping(item, "timing replicate")
        for item in _sequence(timing.get("replicates"), "timing replicates")
    )
    if len(replicates) != 2 or any(item.get("complete_cycles") != 300 for item in replicates):
        raise ResultFigureError("timing result must retain two 300-cycle replicates")
    if timing.get("passed") is not True:
        raise ResultFigureError("timing qualification must have passed")

    run = _mapping(root.get("calibration_run"), "calibration run")
    exact_run_fields = {
        "planned_artifacts": 15,
        "accepted_artifacts": 15,
        "unique_streams": 15,
        "retries": 0,
        "execution_failures": 0,
        "post_mute_failures": 0,
        "audit_issue_count": 0,
    }
    for field, expected in exact_run_fields.items():
        if run.get(field) != expected:
            raise ResultFigureError(f"calibration_run.{field} changed from {expected}")
    if run.get("audit_passed") is not True or run.get("final_exact_mute_passed") is not True:
        raise ResultFigureError("calibration audit and final mute must pass")

    results = tuple(
        _mapping(item, "frequency result")
        for item in _sequence(root.get("frequency_results"), "frequency results")
    )
    expected_frequencies = (
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
        2_472_000_000,
        2_483_000_000,
    )
    if tuple(item.get("center_frequency_hz") for item in results) != expected_frequencies:
        raise ResultFigureError("frequency result order differs from the frozen plan")
    for result in results:
        for field in ("correction_gain_db", "correction_phase_deg"):
            values = _sequence(result.get(field), field)
            if len(values) != 6:
                raise ResultFigureError(f"{field} must contain six antenna values")
            for value in values:
                _number(value, field)
        if result.get("passed") is not True:
            raise ResultFigureError("every per-frequency result must pass")

    interpolation = _mapping(
        root.get("leave_one_frequency_out_diagnostic"), "frequency interpolation diagnostic"
    )
    if interpolation.get("mandatory_gate") is not False:
        raise ResultFigureError("frequency interpolation diagnostic must remain non-gating")
    if interpolation.get("cross_frequency_interpolation_permitted") is not False:
        raise ResultFigureError("snapshot must not permit sparse cross-frequency interpolation")
    if len(_sequence(interpolation.get("folds"), "frequency folds")) != 5:
        raise ResultFigureError("frequency interpolation diagnostic must contain five folds")
    return root


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "axes.grid": True,
            "grid.alpha": 0.25,
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
        metadata={"Software": "smateway deterministic Hexcal result renderer"},
    )
    plt.close(fig)


def _frequency_rows(root: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        _mapping(item, "frequency result")
        for item in _sequence(root.get("frequency_results"), "frequency results")
    )


def _render_coefficients(root: Mapping[str, Any], path: Path) -> None:
    rows = _frequency_rows(root)
    frequencies = np.asarray(
        [_number(row["center_frequency_hz"], "frequency") / 1e9 for row in rows]
    )
    gains = np.asarray([row["correction_gain_db"] for row in rows], dtype=float)
    phases = np.asarray([row["correction_phase_deg"] for row in rows], dtype=float)
    fig, axes = plt.subplots(2, 1, figsize=(13.5, 9.2), sharex=True, constrained_layout=True)
    for index, (name, color) in enumerate(zip(ANTENNAS, COLORS, strict=True)):
        axes[0].plot(frequencies, gains[:, index], "o-", color=color, linewidth=2, label=name)
        axes[1].plot(frequencies, phases[:, index], "o-", color=color, linewidth=2, label=name)
    axes[0].axhline(0.0, color="#444444", linewidth=0.8)
    axes[1].axhline(0.0, color="#444444", linewidth=0.8)
    axes[0].set_ylabel("Complex correction gain (dB)")
    axes[1].set_ylabel("Complex correction phase (degrees)")
    axes[1].set_xlabel("RF center frequency (GHz)")
    axes[1].set_title(
        "Strong frequency dependence is measured; sparse interpolation is not qualified.",
        color="#8B1A1A",
        fontsize=11,
        fontweight="bold",
    )
    axes[0].set_title("Measured end-to-end correction coefficients — use only at listed centers")
    axes[0].legend(ncol=6, loc="upper center")
    axes[1].set_xticks(frequencies, [f"{value:.3f}" for value in frequencies])
    fig.suptitle("HexRay v2.2 centered-TX1 calibration", fontsize=17, fontweight="bold")
    _save(fig, path)


def _render_validation(root: Mapping[str, Any], path: Path) -> None:
    rows = _frequency_rows(root)
    labels = [f"{_number(row['center_frequency_hz'], 'frequency') / 1e9:.3f}" for row in rows]
    x = np.arange(len(rows), dtype=float)
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), constrained_layout=True)
    comparisons = (
        (axes[0, 0], "raw_gain_span_db", "heldout_gain_span_db", "Amplitude span (dB)", 1.0),
        (axes[0, 1], "raw_phase_rms_deg", "heldout_phase_rms_deg", "Phase RMS (degrees)", 5.0),
    )
    for axis, raw_field, held_field, title, gate in comparisons:
        raw = np.asarray([_number(row[raw_field], raw_field) for row in rows])
        held = np.asarray([_number(row[held_field], held_field) for row in rows])
        axis.bar(x - 0.18, raw, width=0.36, label="raw", color="#D55E00")
        axis.bar(x + 0.18, held, width=0.36, label="held-out corrected", color="#009E73")
        axis.axhline(gate, color="#333333", linestyle="--", label=f"gate {gate:g}")
        axis.set_yscale("log")
        axis.set_xticks(x, labels)
        axis.set_title(title)
        axis.legend()
    raw_modes = np.asarray(
        [_number(row["raw_largest_noncommon_mode_dbc"], "raw mode") for row in rows]
    )
    held_modes = np.asarray(
        [_number(row["heldout_largest_noncommon_mode_dbc"], "held-out mode") for row in rows]
    )
    axes[1, 0].plot(x, raw_modes, "o-", color="#D55E00", linewidth=2, label="raw")
    axes[1, 0].plot(x, held_modes, "o-", color="#009E73", linewidth=2, label="held-out corrected")
    axes[1, 0].axhline(-20.0, color="#333333", linestyle="--", label="target −20 dBc")
    axes[1, 0].set_xticks(x, labels)
    axes[1, 0].set_ylabel("Largest noncommon spatial mode (dBc)")
    axes[1, 0].set_title("C6 spatial-mode suppression")
    axes[1, 0].legend()

    interpolation = _mapping(root["leave_one_frequency_out_diagnostic"], "interpolation")
    folds = tuple(_mapping(item, "fold") for item in _sequence(interpolation["folds"], "folds"))
    gain_error = np.asarray([_number(item["gain_error_rms_db"], "gain error") for item in folds])
    phase_error = np.asarray(
        [_number(item["phase_error_rms_deg"], "phase error") for item in folds]
    )
    axis = axes[1, 1]
    axis.bar(x - 0.18, gain_error, width=0.36, color="#0072B2", label="gain RMS (dB)")
    other = axis.twinx()
    other.bar(x + 0.18, phase_error, width=0.36, color="#CC79A7", label="phase RMS (deg)")
    axis.set_xticks(x, labels)
    axis.set_ylabel("Gain error RMS (dB)", color="#0072B2")
    other.set_ylabel("Phase error RMS (degrees)", color="#CC79A7")
    axis.set_title("Leave-one-frequency-out diagnostic")
    handles1, labels1 = axis.get_legend_handles_labels()
    handles2, labels2 = other.get_legend_handles_labels()
    axis.legend(handles1 + handles2, labels1 + labels2, loc="upper right")
    fig.suptitle(
        "Held-out correction succeeds per center; cross-frequency prediction does not",
        fontsize=16,
        fontweight="bold",
    )
    fig.supxlabel("RF center frequency (GHz)")
    _save(fig, path)


def _render_evidence(root: Mapping[str, Any], path: Path) -> None:
    timing = _mapping(root["timing_qualification"], "timing")
    replicates = tuple(
        _mapping(item, "replicate") for item in _sequence(timing["replicates"], "replicates")
    )
    run = _mapping(root["calibration_run"], "run")
    fig = plt.figure(figsize=(14, 10), constrained_layout=True)
    grid = fig.add_gridspec(2, 2, height_ratios=(1.0, 1.1))
    snr_axis = fig.add_subplot(grid[0, 0])
    uncertainty_axis = fig.add_subplot(grid[0, 1])
    evidence_axis = fig.add_subplot(grid[1, :])

    x = np.arange(2, dtype=float)
    transition = [
        _number(item["minimum_transition_snr_db"], "transition SNR") for item in replicates
    ]
    pilot = [_number(item["minimum_pilot_snr_db"], "pilot SNR") for item in replicates]
    snr_axis.bar(x - 0.18, transition, width=0.36, color="#0072B2", label="transition SNR")
    snr_axis.bar(x + 0.18, pilot, width=0.36, color="#009E73", label="pilot/null SNR")
    snr_axis.axhline(19.0, color="#0072B2", linestyle="--", linewidth=1.2)
    snr_axis.axhline(17.0, color="#009E73", linestyle="--", linewidth=1.2)
    snr_axis.set_xticks(x, ("replicate 1", "replicate 2"))
    snr_axis.set_ylabel("Minimum over accepted cycles (dB)")
    snr_axis.set_title("RF observability gates passed")
    snr_axis.legend()

    q_span = [_number(item["maximum_q40_q60_edge_span_us"], "q span") for item in replicates]
    estimator = [
        _number(item["maximum_independent_estimator_delta_us"], "estimator delta")
        for item in replicates
    ]
    uncertainty_axis.bar(x - 0.18, q_span, width=0.36, color="#E69F00", label="q40–q60 span")
    uncertainty_axis.bar(
        x + 0.18, estimator, width=0.36, color="#CC79A7", label="independent delta"
    )
    uncertainty_axis.axhline(1.5, color="#333333", linestyle="--", label="1.5 µs gate")
    uncertainty_axis.set_xticks(x, ("replicate 1", "replicate 2"))
    uncertainty_axis.set_ylabel("Worst edge uncertainty (µs)")
    uncertainty_axis.set_title("Independent edge estimators agree")
    uncertainty_axis.legend()

    evidence_axis.set_axis_off()
    steps = (
        ("Firmware", "16 KiB readback\nUID + options\nPASS", "#DDEEFF"),
        ("Stimulus", "30 conditions\nRX20 / TX −10\nPASS", "#FFF0CC"),
        ("Timing", "2 × 300 cycles\n2 MS/s / RX30\nPASS", "#E8DDF5"),
        ("Matrix", "15 / 15 streams\n0 retries\nPASS", "#DDF3E4"),
        ("Aggregate", "5 frequencies\nall held-out gates\nPASS", "#DDF3E4"),
        ("Audit", f"{run['audit_issue_count']} issues\nraw replay\nPASS", "#BFE8CC"),
    )
    for index, (title, body, color) in enumerate(steps):
        left = 0.015 + index * 0.164
        evidence_axis.add_patch(
            plt.Rectangle(
                (left, 0.34), 0.14, 0.48, facecolor=color, edgecolor="#333333", linewidth=1.2
            )
        )
        evidence_axis.text(
            left + 0.07, 0.70, title, ha="center", va="center", fontsize=12, fontweight="bold"
        )
        evidence_axis.text(left + 0.07, 0.50, body, ha="center", va="center", fontsize=10)
        if index < len(steps) - 1:
            evidence_axis.annotate(
                "",
                xy=(left + 0.163, 0.58),
                xytext=(left + 0.142, 0.58),
                arrowprops={"arrowstyle": "->", "linewidth": 1.5, "color": "#333333"},
            )
    evidence_axis.text(
        0.5,
        0.17,
        "Final state: exact Pluto mute verified. RF timing qualifies durations only; "
        "GPIO identity remains source/readback backed.",
        ha="center",
        va="center",
        fontsize=11,
        fontweight="bold",
        color="#8B1A1A",
    )
    evidence_axis.set_xlim(0, 1)
    evidence_axis.set_ylim(0, 1)
    fig.suptitle("HexRay v2.2 qualification and evidence chain", fontsize=17, fontweight="bold")
    _save(fig, path)


def render_report(
    result_path: Path, output_directory: Path, manifest_path: Path
) -> Mapping[str, Any]:
    root = load_result(result_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    renderers = (_render_coefficients, _render_validation, _render_evidence)
    # Do not inherit or leak Matplotlib state. The design renderer is exercised
    # in the same test process and deliberately uses a different visual style.
    with plt.rc_context():
        plt.rcdefaults()
        _style()
        for name, renderer in zip(FIGURE_NAMES, renderers, strict=True):
            renderer(root, output_directory / name)
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
        "result_snapshot": {"path": _relative(result_path), "sha256": _sha256(result_path)},
        "matplotlib_version": matplotlib.__version__,
        "figures": figures,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def check_report(result_path: Path, output_directory: Path, manifest_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hexcal-v22-result-") as temporary:
        root = Path(temporary)
        temporary_manifest = root / "manifest.json"
        expected = render_report(result_path, root, temporary_manifest)
        try:
            actual = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ResultFigureError(
                f"cannot load committed result figure manifest: {error}"
            ) from error
        if actual != expected:
            raise ResultFigureError("committed Hexcal v2.2 result figure manifest is stale")
        for name in FIGURE_NAMES:
            if (root / name).read_bytes() != (output_directory / name).read_bytes():
                raise ResultFigureError(f"committed result figure is stale: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.check:
            check_report(args.result, args.output_directory, args.manifest)
            print(json.dumps({"status": "passed", "figures_checked": len(FIGURE_NAMES)}))
        else:
            manifest = render_report(args.result, args.output_directory, args.manifest)
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
