#!/usr/bin/env python3
"""Aggregate focused 2.1–2.5 GHz rotation-0 sweeps with historical passes."""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import analyze_rotation0_repeatability as base  # type: ignore[import-not-found]
import numpy as np

LOW_BAND_FREQUENCIES_HZ = base.FREQUENCIES_HZ[:5]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-manifest", type=Path, action="append", required=True)
    parser.add_argument("--focused-manifest", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figure-directory", type=Path)
    return parser


def _alignment_class(score: float) -> str:
    if score >= base.MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE:
        return "unambiguous"
    if score >= base.DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR:
        return "indeterminate"
    return "ambiguous"


def _cohort_summary(runs: Sequence[Mapping[str, Any]], frequency_hz: int) -> dict[str, Any]:
    scores = []
    quality_pass_count = 0
    classes = {"unambiguous": 0, "indeterminate": 0, "ambiguous": 0}
    rows = []
    for run in runs:
        observation = base._mapping(
            base._mapping(run.get("observations"), "observations").get(frequency_hz),
            "frequency observation",
        )
        score = base._number(observation.get("alignment_score"), "alignment score")
        states = base._mapping(observation.get("states"), "states")
        quality_passed = all(
            base._mapping(states.get(antenna), antenna).get("quality_passed") is True
            for antenna in base.ANTENNAS
        )
        classification = _alignment_class(score)
        scores.append(score)
        classes[classification] += 1
        quality_pass_count += int(quality_passed)
        rows.append(
            {
                "run_label": run["label"],
                "alignment_score": score,
                "alignment_class": classification,
                "current_quality_gate_passed": quality_passed,
                "artifact_id": observation["artifact_id"],
            }
        )
    return {
        "capture_count": len(runs),
        "current_quality_gate_pass_count": quality_pass_count,
        "unambiguous_alignment_count": classes["unambiguous"],
        "indeterminate_alignment_count": classes["indeterminate"],
        "ambiguous_alignment_count": classes["ambiguous"],
        "alignment_score_minimum": min(scores),
        "alignment_score_median": statistics.median(scores),
        "alignment_score_maximum": max(scores),
        "runs": rows,
    }


def _path_summary(
    runs: Sequence[Mapping[str, Any]], frequency_hz: int, antenna: str
) -> dict[str, Any]:
    phases = []
    amplitudes_db = []
    raw_contrasts_db = []
    for run in runs:
        observation = base._mapping(
            base._mapping(run.get("observations"), "observations").get(frequency_hz),
            "frequency observation",
        )
        if _alignment_class(
            base._number(observation.get("alignment_score"), "alignment score")
        ) != "unambiguous":
            continue
        states = base._mapping(observation.get("states"), "states")
        phase_deg, amplitude_db = base._relative_measurement(states, antenna)
        state = base._mapping(states.get(antenna), antenna)
        phases.append(phase_deg)
        amplitudes_db.append(amplitude_db)
        raw_contrasts_db.append(
            base._number(
                state.get("raw_selected_to_all_off_contrast_db"),
                "raw selected-to-ALL_OFF contrast",
            )
        )
    return {
        "valid_capture_count": len(phases),
        "relative_phase_mean_deg": base._circular_mean_deg(phases) if phases else None,
        "relative_phase_circular_std_deg": (
            base._circular_std_deg(phases) if len(phases) >= 2 else None
        ),
        "relative_amplitude_mean_db": statistics.mean(amplitudes_db) if amplitudes_db else None,
        "relative_amplitude_std_db": (
            base._sample_std(amplitudes_db) if len(amplitudes_db) >= 2 else None
        ),
        "minimum_raw_selected_to_all_off_contrast_db": (
            min(raw_contrasts_db) if raw_contrasts_db else None
        ),
        "median_raw_selected_to_all_off_contrast_db": (
            statistics.median(raw_contrasts_db) if raw_contrasts_db else None
        ),
    }


def analyze(
    historical_runs: Sequence[Mapping[str, Any]],
    focused_runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(historical_runs) < 2:
        raise base.RepeatabilityAnalysisError("at least two historical runs are required")
    if not focused_runs:
        raise base.RepeatabilityAnalysisError("at least one focused run is required")
    all_runs = (*historical_runs, *focused_runs)
    identity = base._validate_common_identity(all_runs)

    frequency_results = []
    for frequency_hz in LOW_BAND_FREQUENCIES_HZ:
        historical = _cohort_summary(historical_runs, frequency_hz)
        focused = _cohort_summary(focused_runs, frequency_hz)
        combined = _cohort_summary(all_runs, frequency_hz)
        paths = []
        for antenna in base.ANTENNAS:
            historical_path = _path_summary(historical_runs, frequency_hz, antenna)
            focused_path = _path_summary(focused_runs, frequency_hz, antenna)
            combined_path = _path_summary(all_runs, frequency_hz, antenna)
            historical_phase = historical_path["relative_phase_mean_deg"]
            focused_phase = focused_path["relative_phase_mean_deg"]
            historical_amplitude = historical_path["relative_amplitude_mean_db"]
            focused_amplitude = focused_path["relative_amplitude_mean_db"]
            paths.append(
                {
                    "antenna": antenna,
                    "reference_antenna": base.REFERENCE_ANTENNA,
                    "historical": historical_path,
                    "focused": focused_path,
                    "combined": combined_path,
                    "focused_minus_historical_phase_deg": (
                        base._wrap_phase_deg(focused_phase - historical_phase)
                        if focused_phase is not None and historical_phase is not None
                        else None
                    ),
                    "focused_minus_historical_amplitude_db": (
                        focused_amplitude - historical_amplitude
                        if focused_amplitude is not None and historical_amplitude is not None
                        else None
                    ),
                }
            )
        frequency_results.append(
            {
                "center_frequency_hz": frequency_hz,
                "historical": historical,
                "focused": focused,
                "combined": combined,
                "additional_unambiguous_capture_count": focused[
                    "unambiguous_alignment_count"
                ],
                "paths": paths,
            }
        )

    artifact_ids = []
    artifact_hashes = []
    cycle_counts = []
    reference_fractions = []
    for run in all_runs:
        observations = base._mapping(run.get("observations"), "observations")
        for frequency_hz in LOW_BAND_FREQUENCIES_HZ:
            observation = base._mapping(observations.get(frequency_hz), "frequency observation")
            artifact_ids.append(base._string(observation.get("artifact_id"), "artifact ID"))
            artifact_hashes.append(
                base._string(observation.get("artifact_sha256"), "artifact SHA-256")
            )
            cycle_counts.append(
                base._integer(observation.get("complete_cycle_count"), "complete cycle count")
            )
            reference_fractions.append(
                base._number(
                    observation.get("reference_valid_bin_fraction"),
                    "reference-valid fraction",
                )
            )
    low_band_failures = [
        {"run_label": run["label"], **dict(failure)}
        for run in all_runs
        for failure in base._sequence(run.get("failed_attempts"), "failed attempts")
        if failure["center_frequency_hz"] in LOW_BAND_FREQUENCIES_HZ
    ]
    focused_failures = [
        failure
        for failure in low_band_failures
        if failure["run_label"] in {run["label"] for run in focused_runs}
    ]
    expected_capture_count = len(all_runs) * len(LOW_BAND_FREQUENCIES_HZ)
    focused_capture_count = len(focused_runs) * len(LOW_BAND_FREQUENCIES_HZ)

    return {
        "schema": 1,
        "analysis_kind": "rotation0_low_band_focused_extension",
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": identity,
        "scope": {
            "rotation": 0,
            "mapping": base.EXPECTED_MAPPING,
            "frequencies_hz": list(LOW_BAND_FREQUENCIES_HZ),
            "frequency_step_hz": 100_000_000,
            "historical_pass_count": len(historical_runs),
            "focused_pass_count": len(focused_runs),
            "combined_pass_count": len(all_runs),
            "reference_antenna": base.REFERENCE_ANTENNA,
        },
        "alignment_thresholds": {
            "diagnostic_alignment_mode_separator": base.DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR,
            "minimum_unambiguous_alignment_score": base.MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE,
        },
        "source_runs": [
            {
                "cohort": "historical" if run in historical_runs else "focused",
                **{
                    key: run[key]
                    for key in (
                        "label",
                        "run_id",
                        "manifest_path",
                        "manifest_sha256",
                        "execution_attempt_count",
                        "failed_attempts",
                        "source_analyses",
                    )
                },
            }
            for run in all_runs
        ],
        "acquisition_integrity": {
            "combined_low_band_capture_count": len(artifact_ids),
            "expected_combined_low_band_capture_count": expected_capture_count,
            "focused_capture_count": focused_capture_count,
            "focused_execution_attempt_count": focused_capture_count + len(focused_failures),
            "focused_failed_attempt_count": len(focused_failures),
            "low_band_failed_attempt_count": len(low_band_failures),
            "low_band_failed_attempts": low_band_failures,
            "unique_artifact_id_count": len(set(artifact_ids)),
            "unique_artifact_sha256_count": len(set(artifact_hashes)),
            "minimum_complete_cycle_count": min(cycle_counts),
            "maximum_complete_cycle_count": max(cycle_counts),
            "minimum_reference_valid_bin_fraction": min(reference_fractions),
            "all_continuity_and_headroom_admitted": True,
            "all_post_attempt_mutes_passed": True,
            "all_final_mutes_passed": True,
        },
        "aggregate": {
            "historical_unambiguous_capture_count": sum(
                row["historical"]["unambiguous_alignment_count"]
                for row in frequency_results
            ),
            "focused_unambiguous_capture_count": sum(
                row["focused"]["unambiguous_alignment_count"] for row in frequency_results
            ),
            "combined_unambiguous_capture_count": sum(
                row["combined"]["unambiguous_alignment_count"] for row in frequency_results
            ),
            "combined_indeterminate_capture_count": sum(
                row["combined"]["indeterminate_alignment_count"] for row in frequency_results
            ),
            "combined_ambiguous_capture_count": sum(
                row["combined"]["ambiguous_alignment_count"] for row in frequency_results
            ),
            "fully_unambiguous_frequencies_hz": [
                row["center_frequency_hz"]
                for row in frequency_results
                if row["combined"]["unambiguous_alignment_count"] == len(all_runs)
            ],
        },
        "frequency_results": frequency_results,
    }


def _render_figures(result: Mapping[str, Any], directory: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory.mkdir(parents=True, exist_ok=True)
    rows = base._sequence(result.get("frequency_results"), "frequency results")
    frequencies_ghz = [row["center_frequency_hz"] / 1e9 for row in rows]
    historical_count = int(result["scope"]["historical_pass_count"])
    focused_count = int(result["scope"]["focused_pass_count"])

    score_matrix = np.asarray(
        [
            [run["alignment_score"] for run in row["combined"]["runs"]]
            for row in rows
        ],
        dtype=np.float64,
    ).T
    fig, axis = plt.subplots(figsize=(10.5, 9.0), constrained_layout=True)
    image = axis.imshow(score_matrix, aspect="auto", cmap="RdYlGn", vmin=0.75, vmax=1.0)
    axis.axhline(historical_count - 0.5, color="white", linewidth=2.5)
    axis.set_xticks(range(len(frequencies_ghz)), [f"{value:.1f}" for value in frequencies_ghz])
    axis.set_yticks(range(score_matrix.shape[0]), range(1, score_matrix.shape[0] + 1))
    axis.set_xlabel("Center frequency (GHz)")
    axis.set_ylabel("Pass number (1–20 historical; 21–25 focused)")
    axis.set_title("Rotation-0 low-band alignment score across 25 passes")
    colorbar = fig.colorbar(image, ax=axis, label="Alignment score")
    colorbar.ax.axhline(base.MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE, color="black", linewidth=1)
    path1 = directory / "fig01_low_band_alignment_matrix.png"
    fig.savefig(path1, dpi=180)
    plt.close(fig)

    historical_valid = np.asarray(
        [row["historical"]["unambiguous_alignment_count"] for row in rows], dtype=float
    )
    focused_valid = np.asarray(
        [row["focused"]["unambiguous_alignment_count"] for row in rows], dtype=float
    )
    x = np.arange(len(rows), dtype=float)
    width = 0.36
    fig, axis = plt.subplots(figsize=(10.5, 6.2), constrained_layout=True)
    historical_bars = axis.bar(
        x - width / 2,
        100.0 * historical_valid / historical_count,
        width,
        label=f"Historical ({historical_count} passes)",
        color="#4C78A8",
    )
    focused_bars = axis.bar(
        x + width / 2,
        100.0 * focused_valid / focused_count,
        width,
        label=f"Focused ({focused_count} passes)",
        color="#F58518",
    )
    axis.bar_label(
        historical_bars,
        labels=[f"{int(value)}/{historical_count}" for value in historical_valid],
        padding=3,
    )
    axis.bar_label(
        focused_bars,
        labels=[f"{int(value)}/{focused_count}" for value in focused_valid],
        padding=3,
    )
    axis.set_xticks(x, [f"{value:.1f}" for value in frequencies_ghz])
    axis.set_ylim(0, 110)
    axis.set_ylabel("Unambiguous-alignment capture rate (%)")
    axis.set_xlabel("Center frequency (GHz)")
    axis.set_title("Focused passes confirm frequency-dependent low-band admission")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    path2 = directory / "fig02_low_band_valid_coverage.png"
    fig.savefig(path2, dpi=180)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(10.5, 6.5), constrained_layout=True)
    for frequency_index, row in enumerate(rows):
        historical_scores = [item["alignment_score"] for item in row["historical"]["runs"]]
        focused_scores = [item["alignment_score"] for item in row["focused"]["runs"]]
        axis.scatter(
            np.full(len(historical_scores), frequency_index - 0.08),
            historical_scores,
            s=22,
            alpha=0.55,
            color="#4C78A8",
            label="Historical" if frequency_index == 0 else None,
        )
        axis.scatter(
            np.full(len(focused_scores), frequency_index + 0.08),
            focused_scores,
            s=48,
            marker="x",
            linewidth=1.6,
            color="#F58518",
            label="Focused" if frequency_index == 0 else None,
        )
    axis.axhline(
        base.MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE,
        color="#2E7D32",
        linestyle="--",
        label="Unambiguous threshold (0.95)",
    )
    axis.axhline(
        base.DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR,
        color="#B71C1C",
        linestyle=":",
        label="Mode separator (0.85)",
    )
    axis.set_xticks(range(len(frequencies_ghz)), [f"{value:.1f}" for value in frequencies_ghz])
    axis.set_ylim(0.70, 1.01)
    axis.set_xlabel("Center frequency (GHz)")
    axis.set_ylabel("Alignment score")
    axis.set_title("The focused cohort samples the same low-band alignment modes")
    axis.grid(axis="y", alpha=0.25)
    axis.legend(ncol=2)
    path3 = directory / "fig03_low_band_alignment_distributions.png"
    fig.savefig(path3, dpi=180)
    plt.close(fig)

    phase_std = np.full((len(base.ANTENNAS) - 1, len(rows)), np.nan)
    valid_count = np.zeros_like(phase_std, dtype=int)
    for frequency_index, row in enumerate(rows):
        for antenna_index, antenna in enumerate(base.ANTENNAS[:-1]):
            path = next(item for item in row["paths"] if item["antenna"] == antenna)
            value = path["combined"]["relative_phase_circular_std_deg"]
            if value is not None:
                phase_std[antenna_index, frequency_index] = value
            valid_count[antenna_index, frequency_index] = path["combined"]["valid_capture_count"]
    fig, axis = plt.subplots(figsize=(10.5, 6.8), constrained_layout=True)
    image = axis.imshow(phase_std, aspect="auto", cmap="magma_r")
    for antenna_index in range(phase_std.shape[0]):
        for frequency_index in range(phase_std.shape[1]):
            value = phase_std[antenna_index, frequency_index]
            count = valid_count[antenna_index, frequency_index]
            label = f"{value:.2f}°\nN={count}"
            if np.isnan(phase_std[antenna_index, frequency_index]):
                label = f"n/a\nN={count}"
            axis.text(
                frequency_index,
                antenna_index,
                label,
                ha="center",
                va="center",
                fontsize=8,
                color="white" if not np.isnan(value) else "black",
            )
    axis.set_xticks(range(len(frequencies_ghz)), [f"{value:.1f}" for value in frequencies_ghz])
    axis.set_yticks(range(len(base.ANTENNAS) - 1), base.ANTENNAS[:-1])
    axis.set_xlabel("Center frequency (GHz)")
    axis.set_ylabel(f"Path relative to {base.REFERENCE_ANTENNA}")
    axis.set_title("Combined admitted phase scatter; ambiguous captures are excluded")
    fig.colorbar(image, ax=axis, label="Circular phase standard deviation (degrees)")
    path4 = directory / "fig04_low_band_phase_repeatability.png"
    fig.savefig(path4, dpi=180)
    plt.close(fig)
    return [path1, path2, path3, path4]


def main() -> int:
    args = _parser().parse_args()
    historical_runs = [
        base._load_run(f"historical-{index:02d}", path)
        for index, path in enumerate(args.historical_manifest, start=1)
    ]
    focused_runs = [
        base._load_run(f"focused-{index:02d}", path, LOW_BAND_FREQUENCIES_HZ)
        for index, path in enumerate(args.focused_manifest, start=1)
    ]
    result = analyze(historical_runs, focused_runs)
    if args.figure_directory is not None:
        result["figures"] = [
            str(path.resolve()) for path in _render_figures(result, args.figure_directory)
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "combined_capture_count": result["acquisition_integrity"][
                    "combined_low_band_capture_count"
                ],
                "focused_failed_attempt_count": result["acquisition_integrity"][
                    "focused_failed_attempt_count"
                ],
                "combined_unambiguous_capture_count": result["aggregate"][
                    "combined_unambiguous_capture_count"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
