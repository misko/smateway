#!/usr/bin/env python3
"""Validate and render the 2026-09-03 two-source tracking campaign."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from smateway.tracking import (
    fuse_log_likelihoods,
    load_ism_band_profiles,
    near_field_steering,
    solve_bearing,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/tracking_verification_campaign/data/campaign-manifest.json"
DEFAULT_PLAN = ROOT / "docs/tracking_development_plan/data/ism-frequency-plan.json"
DEFAULT_OUTPUT = ROOT / "docs/tracking_verification_campaign"
PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
COLORS = {"TX1": "#0072B2", "TX2": "#D55E00"}
DWELL_LENGTHS_MS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--frequency-plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return document


def _wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _second_peak_margin(
    bearings: np.ndarray,
    likelihood: np.ndarray,
    best_index: int,
) -> float:
    separation = np.abs((bearings - bearings[best_index] + 180.0) % 360.0 - 180.0)
    candidates = np.flatnonzero(separation >= 20.0)
    second = float(np.max(likelihood[candidates]))
    return float(10.0 * np.log10(likelihood[best_index] / max(second, np.finfo(float).tiny)))


def _validate_timeline(capture: dict[str, Any]) -> bool:
    timeline = capture["timeline"]
    if not timeline or capture["frame_count"] != len(timeline):
        return False
    if capture["first_sample_sequence"] != timeline[0]["first_sample_sequence"]:
        return False
    for left, right in zip(timeline, timeline[1:], strict=False):
        if (
            left["first_sample_sequence"] + left["sample_count"]
            != right["first_sample_sequence"]
        ):
            return False
    return (
        timeline[-1]["first_sample_sequence"] + timeline[-1]["sample_count"]
        == capture["last_sample_sequence_exclusive"]
    )


def _summarize(
    manifest: dict[str, Any],
    plan_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profiles = load_ism_band_profiles(plan_path)
    profile_id = manifest["profile_id"]
    profile = profiles[profile_id]
    expected = manifest["expected_sources"]
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for declared in manifest["records"]:
        path = Path(declared["run_json"])
        run = _load_json(path)
        configuration = run["configuration"]
        analysis = run["analysis"]
        capture = run["capture"]
        safety = run["safety"]
        tx_port = declared["tx_port"]
        frequency_hz = declared["frequency_hz"]
        identity = (tx_port, frequency_hz)
        if identity in seen:
            raise ValueError(f"duplicate campaign condition: {identity}")
        seen.add(identity)
        if (
            run["status"] != "passed"
            or configuration["tx_port"] != tx_port
            or configuration["frequency_hz"] != frequency_hz
            or configuration["profile_id"] != profile_id
        ):
            raise ValueError(f"run does not match campaign declaration: {path}")
        if not _validate_timeline(capture):
            raise ValueError(f"run does not have a continuous FPGA sample timeline: {path}")
        selector = safety["selector_final_all_off"]
        cleanup_passed = bool(
            safety["source_final_mute"]["passed"]
            and not selector["lease_active"]
            and selector["applied_code"] == selector["command_code"]
        )
        if not cleanup_passed:
            raise ValueError(f"run cleanup did not close: {path}")
        calibrated = np.asarray(analysis["board_calibration"]["corrected_real"]) + 1j * np.asarray(
            analysis["board_calibration"]["corrected_imag"]
        )
        if calibrated.shape != (len(PORTS),):
            raise ValueError(f"run has the wrong calibrated port vector: {path}")
        phase_relative = np.angle(calibrated / calibrated[0], deg=True)
        magnitude_db = 20.0 * np.log10(np.abs(calibrated) / np.max(np.abs(calibrated)))
        far = analysis["ideal_far_field_bearing"]
        bearing_grid = np.asarray(far["bearing_grid_deg"], dtype=float)
        likelihood = np.asarray(far["likelihood"], dtype=float)
        expected_bearing = float(expected[tx_port]["bearing_deg_clockwise_from_forward"])
        expected_range = float(expected[tx_port]["range_m"])
        positions = np.column_stack(
            (
                expected_range * np.sin(np.deg2rad(bearing_grid)),
                expected_range * np.cos(np.deg2rad(bearing_grid)),
            )
        )
        near_steering = near_field_steering(profile.geometry, frequency_hz, positions)
        near = solve_bearing(
            calibrated,
            near_steering,
            bearing_grid,
            minimum_score=0.5,
            minimum_ambiguity_margin_db=1.0,
            maximum_residual_phase_rms_deg=45.0,
        )
        record = {
            "run_id": run["run_id"],
            "run_json": str(path),
            "tx_port": tx_port,
            "frequency_hz": frequency_hz,
            "frequency_role": (
                "primary"
                if frequency_hz in profile.primary_centres_hz
                else "blind_frequency_holdout"
            ),
            "expected_bearing_deg": expected_bearing,
            "phase_estimator": analysis["phase_estimator"],
            "frame_count": capture["frame_count"],
            "samples_per_channel": capture["total_samples_per_channel"],
            "sample_timeline_continuous": True,
            "capture_peak_component_counts": capture["peak_component_counts"],
            "admitted_peak_component_counts": analysis["admitted_peak_component_counts"],
            "admitted_full_scale_sample_counts": analysis[
                "admitted_full_scale_sample_counts"
            ],
            "cleanup_passed": True,
            "minimum_coherent_estimator_snr_db": analysis[
                "minimum_coherent_estimator_snr_db"
            ],
            "maximum_repeat_phase_rms_deg": analysis["maximum_repeat_phase_rms_deg"],
            "frequency_estimate": analysis["tone_frequency"],
            "far_field": {
                key: far[key]
                for key in (
                    "valid",
                    "reasons",
                    "bearing_deg_clockwise_from_forward",
                    "score",
                    "second_bearing_deg_clockwise_from_forward",
                    "ambiguity_margin_db",
                    "residual_phase_rms_deg",
                )
            },
            "near_field_at_declared_range": {
                "valid": near.valid,
                "reasons": list(near.reasons),
                "bearing_deg_clockwise_from_forward": near.bearing_deg,
                "score": near.score,
                "ambiguity_margin_db": near.ambiguity_margin_db,
                "residual_phase_rms_deg": near.residual_phase_rms_deg,
            },
            "phase_deg_relative_ant1": phase_relative.tolist(),
            "magnitude_db_relative_strongest_port": magnitude_db.tolist(),
            "_bearing_grid": bearing_grid,
            "_likelihood": likelihood,
        }
        records.append(record)

    required_frequencies = {
        *profile.primary_centres_hz,
        *profile.blind_frequency_holdouts_hz,
    }
    if seen != {
        (tx_port, frequency)
        for tx_port in ("TX1", "TX2")
        for frequency in required_frequencies
    }:
        raise ValueError("campaign does not cover both sources at every planned 5.8 GHz frequency")

    fused: dict[str, Any] = {}
    for tx_port in ("TX1", "TX2"):
        selected = [
            record
            for record in records
            if record["tx_port"] == tx_port and record["far_field"]["valid"]
        ]
        if not selected:
            fused[tx_port] = {"valid_frequency_count": 0, "valid": False}
            continue
        likelihoods = np.asarray([record["_likelihood"] for record in selected])
        fused_likelihood = fuse_log_likelihoods(likelihoods)
        grid = selected[0]["_bearing_grid"]
        best = int(np.argmax(fused_likelihood))
        expected_bearing = float(expected[tx_port]["bearing_deg_clockwise_from_forward"])
        fused[tx_port] = {
            "valid_frequency_count": len(selected),
            "frequencies_hz": [record["frequency_hz"] for record in selected],
            "bearing_deg_clockwise_from_forward": float(grid[best]),
            "error_deg": _wrap_degrees(float(grid[best]) - expected_bearing),
            "ambiguity_margin_db": _second_peak_margin(grid, fused_likelihood, best),
            "valid": True,
            "_bearing_grid": grid,
            "_likelihood": fused_likelihood,
        }

    summary = {
        "schema": 1,
        "campaign_id": manifest["campaign_id"],
        "status": "capture_complete_bearing_model_partially_qualified",
        "profile_id": profile_id,
        "expected_sources": expected,
        "frequency_grid_hz": sorted(required_frequencies),
        "run_count": len(records),
        "total_samples_both_channels": int(
            sum(2 * record["samples_per_channel"] for record in records)
        ),
        "all_sample_timelines_continuous": all(
            record["sample_timeline_continuous"] for record in records
        ),
        "all_cleanup_passed": all(record["cleanup_passed"] for record in records),
        "all_admitted_dwells_unclipped": all(
            not any(record["admitted_full_scale_sample_counts"]) for record in records
        ),
        "fused_valid_frequency_results": fused,
        "records": records,
    }
    return summary, records


def _dwell_length_study(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Replay shorter coherent windows inside each admitted hardware dwell."""

    sample_rate_hz = 1_000_000.0
    collected: dict[str, dict[float, dict[str, list[float]]]] = {
        tx_port: {
            duration_ms: {"phase_error_deg": [], "coherent_snr_db": []}
            for duration_ms in DWELL_LENGTHS_MS
        }
        for tx_port in ("TX1", "TX2")
    }
    for record in records:
        run = _load_json(Path(record["run_json"]))
        capture = run["capture"]
        analysis = run["analysis"]
        sample_count = capture["total_samples_per_channel"]
        rx1 = np.memmap(
            capture["raw"]["rx1_path"], mode="r", dtype=np.complex64, shape=(sample_count,)
        )
        rx2 = np.memmap(
            capture["raw"]["rx2_path"], mode="r", dtype=np.complex64, shape=(sample_count,)
        )
        first_sequence = capture["first_sample_sequence"]
        difference_hz = analysis["tone_frequency"].get("frequency_difference_hz")
        for interval in analysis["intervals"]:
            start = interval["start"] + 10_000
            stop = interval["stop"] - 10_000
            indices = first_sequence + np.arange(start, stop, dtype=np.float64)
            product = np.asarray(rx2[start:stop], dtype=np.complex128) * np.conjugate(
                np.asarray(rx1[start:stop], dtype=np.complex128)
            )
            if difference_hz is not None:
                product *= np.exp(-2j * np.pi * difference_hz * indices / sample_rate_hz)
            full_dwell = complex(np.mean(product))
            for duration_ms in DWELL_LENGTHS_MS:
                window_samples = round(duration_ms * sample_rate_hz / 1000.0)
                usable = (product.size // window_samples) * window_samples
                windows = product[:usable].reshape(-1, window_samples)
                means = np.mean(windows, axis=1)
                powers = np.mean(np.abs(windows) ** 2, axis=1)
                coherence = np.minimum(
                    1.0,
                    np.abs(means) / np.sqrt(np.maximum(powers, np.finfo(float).tiny)),
                )
                fraction = np.clip(
                    coherence**2,
                    np.finfo(float).eps,
                    1.0 - np.finfo(float).eps,
                )
                coherent_snr_db = 10.0 * np.log10(
                    window_samples * fraction / (1.0 - fraction)
                )
                phase_error = np.angle(means / full_dwell, deg=True)
                bucket = collected[record["tx_port"]][duration_ms]
                bucket["phase_error_deg"].extend(phase_error.tolist())
                bucket["coherent_snr_db"].extend(coherent_snr_db.tolist())

    result: dict[str, list[dict[str, Any]]] = {}
    for tx_port, by_duration in collected.items():
        result[tx_port] = []
        for duration_ms, values in by_duration.items():
            phase_error = np.asarray(values["phase_error_deg"])
            coherent_snr_db = np.asarray(values["coherent_snr_db"])
            jointly_admitted = (np.abs(phase_error) <= 10.0) & (coherent_snr_db >= 5.0)
            result[tx_port].append(
                {
                    "duration_ms": duration_ms,
                    "chunk_count": int(phase_error.size),
                    "phase_error_rms_deg": float(np.sqrt(np.mean(phase_error**2))),
                    "absolute_phase_error_p95_deg": float(
                        np.percentile(np.abs(phase_error), 95.0)
                    ),
                    "coherent_snr_db_median": float(np.median(coherent_snr_db)),
                    "coherent_snr_db_p05": float(np.percentile(coherent_snr_db, 5.0)),
                    "joint_phase_and_snr_admission_percent": float(
                        100.0 * np.mean(jointly_admitted)
                    ),
                }
            )
    return result


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 140,
            "savefig.dpi": 180,
            "font.size": 9,
            "axes.grid": True,
            "grid.alpha": 0.22,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def _save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _figure_frequency_matrix(plan: dict[str, Any], output: Path) -> None:
    profiles = plan["profiles"]
    fig, axis = plt.subplots(figsize=(10.5, 4.4))
    for row, profile in enumerate(profiles):
        allocation = profile["allocation_hz"]
        lower = allocation["min"] / 1e6
        upper = allocation["max"] / 1e6
        ready = profile["current_fixture_ready"]
        color = "#009E73" if ready else "#777777"
        axis.plot([lower, upper], [row, row], color=color, linewidth=8, solid_capstyle="round")
        primary = np.asarray(profile["primary_centres_hz"]) / 1e6
        holdout = np.asarray(profile["blind_frequency_holdouts_hz"]) / 1e6
        axis.scatter(primary, np.full(primary.shape, row), s=65, color="#0072B2", zorder=3)
        axis.scatter(
            holdout,
            np.full(holdout.shape, row),
            s=55,
            marker="D",
            facecolor="white",
            edgecolor="#D55E00",
            linewidth=1.5,
            zorder=3,
        )
        axis.text(upper * 1.025, row, "installed" if ready else "staged", va="center", color=color)
    axis.set_xscale("log")
    axis.set_xlim(390, 6500)
    axis.set_yticks(range(len(profiles)), [profile["id"] for profile in profiles])
    axis.set_xlabel("RF centre frequency (MHz, logarithmic axis)")
    axis.set_title("Complete hardware-reachable ISM plan: 50 MHz primary lattice and holdouts")
    axis.scatter([], [], s=65, color="#0072B2", label="primary training centre")
    axis.scatter(
        [],
        [],
        s=55,
        marker="D",
        facecolor="white",
        edgecolor="#D55E00",
        label="blind holdout",
    )
    axis.legend(loc="upper left", frameon=False, ncol=2)
    _save(fig, output / "fig01_ism_frequency_matrix.png")


def _records_for(records: list[dict[str, Any]], tx_port: str) -> list[dict[str, Any]]:
    return sorted(
        (record for record in records if record["tx_port"] == tx_port),
        key=lambda record: record["frequency_hz"],
    )


def _figure_bearings(summary: dict[str, Any], records: list[dict[str, Any]], output: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), sharex=True)
    for axis, tx_port in zip(axes, ("TX1", "TX2"), strict=True):
        selected = _records_for(records, tx_port)
        frequencies = np.asarray([record["frequency_hz"] / 1e6 for record in selected])
        bearings = np.asarray(
            [record["far_field"]["bearing_deg_clockwise_from_forward"] for record in selected]
        )
        valid = np.asarray([record["far_field"]["valid"] for record in selected])
        expected = selected[0]["expected_bearing_deg"]
        axis.axhline(
            expected,
            color="black",
            linestyle="--",
            linewidth=1.2,
            label="stated placement",
        )
        axis.plot(frequencies, bearings, color=COLORS[tx_port], alpha=0.4, linewidth=1)
        axis.scatter(
            frequencies[valid],
            bearings[valid],
            color=COLORS[tx_port],
            s=55,
            label="admitted",
        )
        axis.scatter(
            frequencies[~valid],
            bearings[~valid],
            facecolor="white",
            edgecolor=COLORS[tx_port],
            marker="X",
            s=70,
            linewidth=1.5,
            label="rejected by quality gate",
        )
        fused = summary["fused_valid_frequency_results"][tx_port]
        axis.text(
            0.03,
            0.04,
            f"valid-only fusion: {fused['bearing_deg_clockwise_from_forward']:.2f}°\n"
            f"error {fused['error_deg']:+.2f}°, margin {fused['ambiguity_margin_db']:.2f} dB",
            transform=axis.transAxes,
            va="bottom",
            bbox={"facecolor": "white", "edgecolor": "#bbbbbb", "alpha": 0.9},
        )
        axis.set_title(f"{tx_port}: {selected[0]['phase_estimator'].replace('_', ' ')}")
        axis.set_xlabel("RF centre (MHz)")
        axis.set_ylabel("bearing clockwise from forward (degrees)")
        axis.set_ylim(0, 360)
        axis.set_yticks(np.arange(0, 361, 60))
        axis.legend(frameon=False, fontsize=8)
    fig.suptitle("5.8 GHz two-source screen: individual-frequency bearings")
    fig.tight_layout()
    _save(fig, output / "fig02_bearings_by_frequency.png")


def _figure_quality(records: list[dict[str, Any]], output: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(11.5, 7.2), sharex=True)
    fields = (
        ("minimum_coherent_estimator_snr_db", "minimum coherent SNR (dB)", 5.0, "above"),
        ("maximum_repeat_phase_rms_deg", "maximum repeat phase RMS (degrees)", 20.0, "below"),
        (
            "far_field.residual_phase_rms_deg",
            "ideal-manifold residual RMS (degrees)",
            45.0,
            "below",
        ),
        ("far_field.ambiguity_margin_db", "ideal-manifold ambiguity margin (dB)", 1.0, "above"),
    )
    for axis, (field, label, gate, direction) in zip(axes.flat, fields, strict=True):
        for tx_port in ("TX1", "TX2"):
            selected = _records_for(records, tx_port)
            x = [record["frequency_hz"] / 1e6 for record in selected]
            if field.startswith("far_field."):
                key = field.split(".", 1)[1]
                y = [record["far_field"][key] for record in selected]
            else:
                y = [record[field] for record in selected]
            axis.plot(x, y, marker="o", color=COLORS[tx_port], label=tx_port)
        axis.axhline(gate, color="black", linestyle="--", linewidth=1, label=f"gate ({direction})")
        axis.set_ylabel(label)
        axis.set_xlabel("RF centre (MHz)")
    axes[0, 0].legend(frameon=False, ncol=3)
    fig.suptitle("Acquisition quality is strong; ideal-manifold mismatch limits admission")
    fig.tight_layout()
    _save(fig, output / "fig03_quality_gates.png")


def _heatmap(
    records: list[dict[str, Any]],
    field: str,
    title: str,
    output_path: Path,
    *,
    cmap: str,
    vmin: float,
    vmax: float,
    colorbar_label: str,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.5), sharey=True, constrained_layout=True)
    image = None
    for axis, tx_port in zip(axes, ("TX1", "TX2"), strict=True):
        selected = _records_for(records, tx_port)
        matrix = np.asarray([record[field] for record in selected])
        image = axis.imshow(matrix, cmap=cmap, vmin=vmin, vmax=vmax, aspect="auto")
        axis.set_xticks(range(len(PORTS)), PORTS, rotation=35, ha="right")
        axis.set_yticks(
            range(len(selected)),
            [f"{record['frequency_hz'] / 1e6:.0f}" for record in selected],
        )
        axis.set_title(tx_port)
        axis.set_xlabel("selector port")
        axis.set_ylabel("RF centre (MHz)")
        for row in range(matrix.shape[0]):
            for column in range(matrix.shape[1]):
                value = matrix[row, column]
                text_color = "white" if abs(value) > 0.55 * max(abs(vmin), abs(vmax)) else "black"
                axis.text(
                    column,
                    row,
                    f"{value:.0f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color=text_color,
                )
    assert image is not None
    colorbar = fig.colorbar(image, ax=axes, shrink=0.9, pad=0.02)
    colorbar.set_label(colorbar_label)
    fig.suptitle(title)
    _save(fig, output_path)


def _figure_likelihoods(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    output: Path,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.2), subplot_kw={"projection": "polar"})
    for axis, tx_port in zip(axes, ("TX1", "TX2"), strict=True):
        selected = _records_for(records, tx_port)
        for record in selected:
            grid = np.deg2rad(record["_bearing_grid"])
            axis.plot(
                grid,
                record["_likelihood"],
                linewidth=1,
                label=f"{record['frequency_hz']/1e6:.0f}",
            )
        fused = summary["fused_valid_frequency_results"][tx_port]
        axis.plot(
            np.deg2rad(fused["_bearing_grid"]),
            fused["_likelihood"],
            color="black",
            linewidth=2.5,
            label="valid-only fusion",
        )
        expected = math.radians(selected[0]["expected_bearing_deg"])
        axis.plot([expected, expected], [0, 1], linestyle="--", color="#777777", linewidth=1.2)
        axis.set_theta_zero_location("N")
        axis.set_theta_direction(-1)
        axis.set_ylim(0, 1)
        axis.set_title(tx_port)
        axis.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, -0.25),
            ncol=3,
            frameon=False,
            fontsize=8,
        )
    fig.suptitle("Ideal-array likelihoods and valid-frequency fusion")
    fig.subplots_adjust(bottom=0.19, top=0.86, wspace=0.28)
    _save(fig, output / "fig06_likelihoods.png")


def _figure_dwell_study(summary: dict[str, Any], output: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.3), sharex=True)
    for tx_port in ("TX1", "TX2"):
        study = summary["offline_dwell_length_study"][tx_port]
        durations = [item["duration_ms"] for item in study]
        phase_p95 = [item["absolute_phase_error_p95_deg"] for item in study]
        admitted = [item["joint_phase_and_snr_admission_percent"] for item in study]
        axes[0].plot(durations, phase_p95, marker="o", color=COLORS[tx_port], label=tx_port)
        axes[1].plot(durations, admitted, marker="o", color=COLORS[tx_port], label=tx_port)
    axes[0].axhline(10.0, color="black", linestyle="--", linewidth=1, label="10° target")
    axes[1].axhline(95.0, color="black", linestyle="--", linewidth=1, label="95% target")
    axes[0].set_ylabel("absolute phase error p95 (degrees)")
    axes[1].set_ylabel("phase ≤10° and coherent SNR ≥5 dB (%)")
    for axis in axes:
        axis.set_xscale("log")
        axis.set_xlabel("coherent integration per selector state (ms)")
        axis.legend(frameon=False)
    fig.suptitle("Offline dwell replay at the measured link budget")
    fig.tight_layout()
    _save(fig, output / "fig07_dwell_length_replay.png")


def _serializable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def main() -> int:
    args = _parser().parse_args()
    manifest = _load_json(args.manifest)
    plan = _load_json(args.frequency_plan)
    summary, records = _summarize(manifest, args.frequency_plan)
    summary["offline_dwell_length_study"] = _dwell_length_study(records)
    output = args.output
    png = output / "png"
    data = output / "data"
    png.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    _style()
    _figure_frequency_matrix(plan, png)
    _figure_bearings(summary, records, png)
    _figure_quality(records, png)
    _heatmap(
        records,
        "phase_deg_relative_ant1",
        "PCB-corrected measured phase, referenced to ANT1",
        png / "fig04_corrected_phase_matrix.png",
        cmap="twilight_shifted",
        vmin=-180.0,
        vmax=180.0,
        colorbar_label="relative phase (degrees)",
    )
    _heatmap(
        records,
        "magnitude_db_relative_strongest_port",
        "PCB-corrected received magnitude by selector port",
        png / "fig05_corrected_magnitude_matrix.png",
        cmap="viridis",
        vmin=-30.0,
        vmax=0.0,
        colorbar_label="relative magnitude (dB)",
    )
    _figure_likelihoods(summary, records, png)
    _figure_dwell_study(summary, png)
    summary_path = data / "campaign-summary.json"
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(_serializable(summary), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(summary_path)
    print(f"validated_runs={len(records)}")
    print(f"summary={summary_path}")
    print(f"figures={png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
