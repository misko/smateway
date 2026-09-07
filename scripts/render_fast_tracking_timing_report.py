#!/usr/bin/env python3
"""Render the dense frequency and autonomous C6 switching campaign report."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = Path(
    "/srv/bulk/samteway/lab-data/tracking-dense-1mhz-20260903-v1/analysis/summary.json"
)
DEFAULT_FAST = Path(
    "/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v6/campaign.json"
)
DEFAULT_OUTPUT = ROOT / "docs/fast_tracking_timing_campaign"
DEFAULT_TRIM = DEFAULT_OUTPUT / "data/trim_sensitivity.json"
TX_COLORS = {"TX1": "#0072B2", "TX2": "#D55E00"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--fast-campaign", type=Path, default=DEFAULT_FAST)
    parser.add_argument("--trim-analysis", type=Path, default=DEFAULT_TRIM)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"cannot write an empty table: {path}")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _stats(values: list[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)], dtype=float)
    if not finite.size:
        return {"count": 0, "median": None, "p95": None, "maximum": None}
    return {
        "count": int(finite.size),
        "median": float(np.median(finite)),
        "p95": float(np.percentile(finite, 95.0)),
        "maximum": float(np.max(finite)),
    }


def _fmt(value: object, digits: int = 3) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}g}"
    return "—"


def _phase_row(run: dict[str, Any], cycles: int) -> dict[str, Any]:
    for row in run["analysis"]["integration_study"]:
        if row["cycles_averaged"] == cycles:
            return row
    raise ValueError(f"run has no phase row for {cycles} cycles")


def _bearing_row(run: dict[str, Any], cycles: int) -> dict[str, Any]:
    for row in run["analysis"]["bearing_study"]["integration_study"]:
        if row["cycles_averaged"] == cycles:
            return row
    raise ValueError(f"run has no bearing row for {cycles} cycles")


def _qualified_row(run: dict[str, Any]) -> dict[str, Any] | None:
    frequency_acceptance = run["analysis"].get("frequency_difference_acceptance")
    if isinstance(frequency_acceptance, dict) and not frequency_acceptance.get(
        "qualified", False
    ):
        return None
    for row in run["analysis"]["integration_study"]:
        if (
            row["groups_per_port"] >= 8
            and row["phase_rms_deg_power_weighted_ports"] <= 10.0
        ):
            return row
    return None


def _phase_settle_end_us(run: dict[str, Any]) -> float | None:
    rows = run["analysis"]["settling_study"]
    window_us = run["analysis"]["settling_acceptance"]["measurement_window_us"]
    for index in range(max(0, len(rows) - 2)):
        window = rows[index : index + 3]
        if all(
            row["ensemble_phase_rms_deg"] <= 5.0
            and row["ensemble_absolute_phase_max_deg"] <= 10.0
            for row in window
        ):
            return float(rows[index]["age_after_selected_edge_us"] + window_us)
    return None


def _validated_runs(
    campaign: dict[str, Any], key: str, expected_count: int
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    conditions = campaign.get(key)
    if not isinstance(conditions, list) or len(conditions) != expected_count:
        raise ValueError(f"{key} does not contain exactly {expected_count} conditions")
    result = []
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError(f"{key} condition is malformed")
        path = Path(str(condition.get("run_json", ""))).resolve(strict=True)
        if _sha256(path) != condition.get("run_sha256"):
            raise ValueError(f"run hash differs: {path}")
        run = _load(path)
        if (
            run.get("status") != "passed"
            or run.get("safety", {}).get("source_final_mute", {}).get("passed") is not True
        ):
            raise ValueError(f"run is not passed and fail-muted: {path}")
        configuration = run.get("configuration", {})
        if (
            configuration.get("frequency_hz") != condition.get("frequency_hz")
            or configuration.get("tx_channel") != condition.get("tx_channel")
            or not str(configuration.get("profile_id", "")).endswith(
                f"-{condition.get('dwell_us')}us-v1"
            )
        ):
            raise ValueError(f"condition and run configuration disagree: {path}")
        capture = run.get("capture", {})
        timeline = capture.get("timeline")
        if (
            not isinstance(timeline, list)
            or not timeline
            or any(
                item.get("missing_samples_before") != 0
                or item.get("overflow_observed") is not False
                for item in timeline
            )
            or any(capture.get("full_scale_sample_counts", (1,)))
        ):
            raise ValueError(f"run sample continuity or ADC range failed: {path}")
        sample_count = capture.get("total_samples_per_channel")
        raw = capture.get("raw", {})
        if not isinstance(sample_count, int) or sample_count < 1:
            raise ValueError(f"run sample count is invalid: {path}")
        for channel in ("rx1", "rx2"):
            raw_path = Path(str(raw.get(f"{channel}_path", ""))).resolve(strict=True)
            if not raw_path.is_file() or raw_path.stat().st_size != 8 * sample_count:
                raise ValueError(f"run raw file size is invalid: {raw_path}")
        result.append((condition, run))
    return result


def _flatten(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]]
) -> list[dict[str, Any]]:
    records = []
    for condition, run in pairs:
        qualified = _qualified_row(run)
        first = _phase_row(run, 1)
        frequency_acceptance = run["analysis"].get(
            "frequency_difference_acceptance", {"qualified": True}
        )
        frequency_estimate = run["analysis"].get("frequency_difference_estimate")
        if not isinstance(frequency_estimate, dict):
            frequency_estimate = {}
        reference = run["analysis"]["bearing_study"]["full_capture_reference"]
        decode = run["analysis"]["schedule_decode"]
        records.append(
            {
                "dwell_us": condition["dwell_us"],
                "frequency_hz": condition["frequency_hz"],
                "tx_channel": condition["tx_channel"],
                "tx_port": f"TX{condition['tx_channel'] + 1}",
                "cycle_us": 180 + 6 * (20 + condition["dwell_us"]),
                "theoretical_scans_per_second": 1e6
                / (180 + 6 * (20 + condition["dwell_us"])),
                "frequency_difference_qualified": frequency_acceptance["qualified"],
                "frequency_difference_search_objective": frequency_estimate.get(
                    "search_objective"
                ),
                "frequency_difference_error_hz": frequency_estimate.get(
                    "difference_error_hz"
                ),
                "single_cycle_phase_rms_deg_maximum_port": first[
                    "phase_rms_deg_maximum_port"
                ],
                "single_cycle_phase_rms_deg_maximum_observable_port": first[
                    "phase_rms_deg_maximum_observable_port"
                ],
                "single_cycle_phase_rms_deg_power_weighted_ports": first[
                    "phase_rms_deg_power_weighted_ports"
                ],
                "phase_10deg_wall_latency_ms": (
                    None if qualified is None else qualified["measured_wall_latency_ms"]
                ),
                "phase_10deg_active_integration_ms": (
                    None
                    if qualified is None
                    else qualified["nominal_active_integration_ms"]
                ),
                "phase_10deg_cycles": (
                    None if qualified is None else qualified["cycles_averaged"]
                ),
                "phase_10deg_maximum_port_rms_deg": (
                    None
                    if qualified is None
                    else qualified["phase_rms_deg_maximum_port"]
                ),
                "phase_10deg_maximum_observable_port_rms_deg": (
                    None
                    if qualified is None
                    else qualified["phase_rms_deg_maximum_observable_port"]
                ),
                "phase_10deg_power_weighted_rms_deg": (
                    None
                    if qualified is None
                    else qualified["phase_rms_deg_power_weighted_ports"]
                ),
                "observable_port_count": len(run["analysis"]["observable_ports"]),
                "observable_ports": ",".join(run["analysis"]["observable_ports"]),
                "settled_after_selected_edge_us": run["analysis"][
                    "settled_after_selected_edge_us"
                ],
                "phase_only_settled_after_selected_edge_us": _phase_settle_end_us(
                    run
                ),
                "decode_method": decode["decode_method"],
                "selector_clock_scale": decode["cycle_scale_median"],
                "selector_cycle_frequency_hz": decode["periodicity_frequency_hz"],
                "decode_periodicity_score": decode["periodicity_score"],
                "decode_alignment_score": decode["alignment_score"],
                "full_bearing_deg": reference["bearing_deg"],
                "full_bearing_valid": reference["valid"],
                "full_bearing_score": reference["score"],
                "full_bearing_residual_phase_rms_deg": reference[
                    "residual_phase_rms_deg"
                ],
                "run_json": condition["run_json"],
            }
        )
    return records


def _flatten_ports(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for condition, run in pairs:
        qualified = _qualified_row(run)
        references = run["analysis"]["per_port_reference"]
        snr = run["analysis"]["per_port_single_visit_coherent_snr_db"]
        observable = set(run["analysis"]["observable_ports"])
        for port in ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5"):
            reference = references[port]
            rows.append(
                {
                    "dwell_us": condition["dwell_us"],
                    "frequency_hz": condition["frequency_hz"],
                    "tx_port": f"TX{condition['tx_channel'] + 1}",
                    "port": port,
                    "observable": port in observable,
                    "single_visit_coherent_snr_db": snr[port],
                    "full_capture_reference_magnitude": math.hypot(
                        reference["real"], reference["imag"]
                    ),
                    "qualified_cycles": (
                        None if qualified is None else qualified["cycles_averaged"]
                    ),
                    "qualified_measured_wall_latency_ms": (
                        None
                        if qualified is None
                        else qualified["measured_wall_latency_ms"]
                    ),
                    "phase_rms_at_weighted_10deg_gate_deg": (
                        None
                        if qualified is None
                        else qualified["per_port_phase_rms_deg"][port]
                    ),
                    "run_json": condition["run_json"],
                }
            )
    return rows


def _ladder_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for dwell_us in sorted({item["dwell_us"] for item in records}):
        for tx_port in ("TX1", "TX2"):
            selected = [
                item
                for item in records
                if item["dwell_us"] == dwell_us and item["tx_port"] == tx_port
            ]
            latency = [
                float(item["phase_10deg_wall_latency_ms"])
                for item in selected
                if item["phase_10deg_wall_latency_ms"] is not None
            ]
            settling = [
                float(item["settled_after_selected_edge_us"])
                for item in selected
                if item["settled_after_selected_edge_us"] is not None
            ]
            phase_settling = [
                float(item["phase_only_settled_after_selected_edge_us"])
                for item in selected
                if item["phase_only_settled_after_selected_edge_us"] is not None
            ]
            rows.append(
                {
                    "dwell_us": dwell_us,
                    "tx_port": tx_port,
                    "sentinel_frequency_count": len(selected),
                    "phase_10deg_frequency_pass_count": len(latency),
                    "frequency_difference_qualified_count": sum(
                        bool(item["frequency_difference_qualified"])
                        for item in selected
                    ),
                    "phase_10deg_wall_latency_median_ms": (
                        None if not latency else float(np.median(latency))
                    ),
                    "phase_10deg_wall_latency_worst_ms": (
                        None if not latency else max(latency)
                    ),
                    "single_cycle_phase_rms_p95_deg": float(
                        np.percentile(
                            [
                                item["single_cycle_phase_rms_deg_maximum_port"]
                                for item in selected
                            ],
                            95.0,
                        )
                    ),
                    "single_cycle_power_weighted_phase_rms_p95_deg": float(
                        np.percentile(
                            [
                                item[
                                    "single_cycle_phase_rms_deg_power_weighted_ports"
                                ]
                                for item in selected
                            ],
                            95.0,
                        )
                    ),
                    "rf_settling_p95_us": (
                        None if not settling else float(np.percentile(settling, 95.0))
                    ),
                    "strict_rf_settling_frequency_pass_count": len(settling),
                    "phase_only_settling_p95_us": (
                        None
                        if not phase_settling
                        else float(np.percentile(phase_settling, 95.0))
                    ),
                    "phase_only_settling_frequency_pass_count": len(
                        phase_settling
                    ),
                    "theoretical_scans_per_second": selected[0][
                        "theoretical_scans_per_second"
                    ],
                }
            )
    return rows


def _dense_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, Any] = {}
    for tx_port in ("TX1", "TX2"):
        selected = [item for item in records if item["tx_port"] == tx_port]
        latency = [
            float(item["phase_10deg_wall_latency_ms"])
            for item in selected
            if item["phase_10deg_wall_latency_ms"] is not None
        ]
        settling = [
            float(item["settled_after_selected_edge_us"])
            for item in selected
            if item["settled_after_selected_edge_us"] is not None
        ]
        phase_settling = [
            float(item["phase_only_settled_after_selected_edge_us"])
            for item in selected
            if item["phase_only_settled_after_selected_edge_us"] is not None
        ]
        one_cycle = [
            float(item["single_cycle_phase_rms_deg_maximum_port"]) for item in selected
        ]
        one_cycle_weighted = [
            float(item["single_cycle_phase_rms_deg_power_weighted_ports"])
            for item in selected
        ]
        bearings = np.asarray([item["full_bearing_deg"] for item in selected])
        expected = 90.0 if tx_port == "TX1" else 180.0
        bearing_errors = (bearings - expected + 180.0) % 360.0 - 180.0
        by_source[tx_port] = {
            "frequency_count": len(selected),
            "phase_10deg_frequency_pass_count": len(latency),
            "frequency_difference_qualified_count": sum(
                bool(item["frequency_difference_qualified"]) for item in selected
            ),
            "phase_10deg_frequency_pass_percent": 100.0 * len(latency) / len(selected),
            "phase_10deg_wall_latency_ms": _stats(latency),
            "single_cycle_phase_rms_maximum_port_deg": _stats(one_cycle),
            "single_cycle_phase_rms_power_weighted_deg": _stats(
                one_cycle_weighted
            ),
            "rf_settling_us": _stats(settling),
            "strict_rf_settling_frequency_pass_count": len(settling),
            "phase_only_settling_us": _stats(phase_settling),
            "phase_only_settling_frequency_pass_count": len(phase_settling),
            "full_capture_valid_bearing_percent": 100.0
            * np.mean([item["full_bearing_valid"] for item in selected]),
            "full_capture_bearing_error_to_approximate_setup_deg": _stats(
                bearing_errors.tolist()
            ),
        }
    return by_source


def _flatten_failures(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in campaign.get("capture_failures", []):
        run_json = item.get("run_json")
        if not isinstance(run_json, str):
            raise ValueError("failed capture has no run evidence")
        path = Path(run_json).resolve(strict=True)
        if _sha256(path) != item.get("run_sha256"):
            raise ValueError(f"failed-capture run hash differs: {path}")
        failed_run = _load(path)
        if failed_run.get("status") != "failed":
            raise ValueError(f"rejected capture is not failed evidence: {path}")
        capture_error = item.get("capture_error")
        if not isinstance(capture_error, dict):
            capture_error = {}
        rows.append(
            {
                "dwell_us": item["dwell_us"],
                "frequency_hz": item["frequency_hz"],
                "tx_port": f"TX{item['tx_channel'] + 1}",
                "attempt": item["attempt"],
                "error_type": capture_error.get("type"),
                "error_message": capture_error.get("message"),
                "run_json": str(path),
                "run_sha256": item.get("run_sha256"),
            }
        )
    return rows


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 130,
            "savefig.dpi": 180,
            "font.size": 8.5,
            "axes.grid": True,
            "grid.alpha": 0.2,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def _save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _figure_baseline(baseline: dict[str, Any], png: Path) -> None:
    fig, axis = plt.subplots(figsize=(8.8, 4.6))
    for tx_port in ("TX1", "TX2"):
        rows = baseline["by_source"][tx_port]["integration_time_study"]
        axis.plot(
            [item["integration_ms"] for item in rows],
            [item["phase_rms_deg_p95_across_frequencies"] for item in rows],
            marker="o",
            color=TX_COLORS[tx_port],
            label=tx_port,
        )
    axis.axhline(10.0, color="black", linestyle="--", linewidth=1, label="10° target")
    axis.set_xscale("log")
    axis.set_xlabel("stable coherent integration (ms)")
    axis.set_ylabel("across-frequency p95 phase RMS (degrees)")
    axis.set_title("1 MHz baseline: estimator lower bound inside stable host dwells")
    axis.legend(frameon=False)
    fig.tight_layout()
    _save(fig, png / "fig01_baseline_integration.png")


def _figure_ladder(records: list[dict[str, Any]], png: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.4))
    dwells = sorted({item["dwell_us"] for item in records})
    for tx_port in ("TX1", "TX2"):
        passes = []
        worst = []
        for dwell in dwells:
            values = [
                item["phase_10deg_wall_latency_ms"]
                for item in records
                if item["dwell_us"] == dwell
                and item["tx_port"] == tx_port
                and item["phase_10deg_wall_latency_ms"] is not None
            ]
            passes.append(len(values))
            worst.append(np.nan if len(values) != 7 else max(values))
        axes[0].plot(
            dwells, passes, marker="o", color=TX_COLORS[tx_port], label=tx_port
        )
        axes[1].plot(dwells, worst, marker="o", color=TX_COLORS[tx_port], label=tx_port)
    axes[0].set_xscale("log")
    axes[0].set_xlabel("active dwell (µs)")
    axes[0].set_ylabel("sentinel centres reaching 10°")
    axes[0].set_ylim(-0.3, 7.5)
    axes[0].axhline(7, color="black", linestyle="--", linewidth=1)
    axes[0].legend(frameon=False)
    axes[1].set_xscale("log")
    axes[1].set_yscale("log")
    axes[1].set_xlabel("active dwell (µs)")
    axes[1].set_ylabel("worst all-sentinel 10° latency (ms)")
    axes[1].legend(frameon=False)
    scan_rate = [1e6 / (180 + 6 * (20 + dwell)) for dwell in dwells]
    duty = [100.0 * dwell / (180 + 6 * (20 + dwell)) for dwell in dwells]
    axes[2].plot(dwells, scan_rate, marker="o", color="#009E73", label="full C6 scans/s")
    other = axes[2].twinx()
    other.plot(dwells, duty, marker="s", color="#CC79A7", label="per-port duty cycle")
    axes[2].set_xscale("log")
    axes[2].set_xlabel("active dwell (µs)")
    axes[2].set_ylabel("physical scans/s")
    other.set_ylabel("per-port active duty cycle (%)")
    axes[2].legend(frameon=False, loc="upper left")
    other.legend(frameon=False, loc="lower right")
    fig.suptitle("Autonomous C6 dwell ladder: speed versus phase-qualified latency")
    fig.tight_layout()
    _save(fig, png / "fig02_dwell_ladder.png")


def _matrix(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    tx_channel: int,
    field: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    selected = sorted(
        ((condition, run) for condition, run in pairs if condition["tx_channel"] == tx_channel),
        key=lambda item: item[0]["frequency_hz"],
    )
    frequency = np.asarray([item[0]["frequency_hz"] / 1e6 for item in selected])
    cycles = np.asarray(
        [row["cycles_averaged"] for row in selected[0][1]["analysis"]["integration_study"]]
    )
    matrix = np.asarray(
        [
            [row[field] for row in run["analysis"]["integration_study"]]
            for _condition, run in selected
        ],
        dtype=float,
    ).T
    return frequency, cycles, matrix


def _figure_dense_phase(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], png: Path, dwell_us: int
) -> None:
    cycle_us = 180 + 6 * (20 + dwell_us)
    fig, axes = plt.subplots(2, 1, figsize=(13, 7.2), sharex=True, constrained_layout=True)
    image = None
    for tx_channel, axis in enumerate(axes):
        frequency, cycles, matrix = _matrix(
            pairs, tx_channel, "phase_rms_deg_power_weighted_ports"
        )
        wall_ms = cycles * cycle_us / 1000.0
        image = axis.pcolormesh(
            frequency,
            wall_ms,
            matrix,
            cmap="magma",
            vmin=0.0,
            vmax=45.0,
            shading="nearest",
        )
        axis.set_yscale("log")
        axis.set_ylabel("wall latency (ms)")
        axis.set_title(f"TX{tx_channel + 1}")
    axes[-1].set_xlabel("RF centre (MHz)")
    if image is not None:
        colorbar = fig.colorbar(image, ax=axes, pad=0.015)
        colorbar.set_label("received-power-weighted phase RMS (degrees)")
    fig.suptitle(
        f"Selected {dwell_us} µs schedule: weighted phase RMS versus frequency and latency"
    )
    _save(fig, png / "fig03_dense_phase_frequency_latency.png")


def _figure_dense_summary(records: list[dict[str, Any]], png: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    for tx_port in ("TX1", "TX2"):
        selected = sorted(
            (item for item in records if item["tx_port"] == tx_port),
            key=lambda item: item["frequency_hz"],
        )
        frequency = [item["frequency_hz"] / 1e6 for item in selected]
        axes[0].plot(
            frequency,
            [item["phase_10deg_wall_latency_ms"] for item in selected],
            color=TX_COLORS[tx_port],
            label=tx_port,
        )
        axes[1].plot(
            frequency,
            [
                item["phase_only_settled_after_selected_edge_us"]
                for item in selected
            ],
            # A three-window phase-only bound remains meaningful when a weak
            # path prevents the stricter amplitude-and-phase tail criterion.
            color=TX_COLORS[tx_port],
            label=tx_port,
        )
    axes[0].set_ylabel("first 10° phase-qualified wall latency (ms)")
    axes[0].set_yscale("log")
    axes[1].set_ylabel("RF-visible settle bound after select (µs)")
    axes[1].set_xlabel("RF centre (MHz)")
    for axis in axes:
        axis.legend(frameon=False)
    fig.suptitle("Selected autonomous timing across every 1 MHz centre")
    fig.tight_layout()
    _save(fig, png / "fig04_dense_qualified_latency_and_settling.png")


def _figure_bearing(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], png: Path, dwell_us: int
) -> None:
    cycle_us = 180 + 6 * (20 + dwell_us)
    fig, axes = plt.subplots(2, 1, figsize=(13, 7.2), sharex=True, constrained_layout=True)
    image = None
    for tx_channel, axis in enumerate(axes):
        selected = sorted(
            ((condition, run) for condition, run in pairs if condition["tx_channel"] == tx_channel),
            key=lambda item: item[0]["frequency_hz"],
        )
        frequency = np.asarray([item[0]["frequency_hz"] / 1e6 for item in selected])
        cycles = np.asarray(
            [
                row["cycles_averaged"]
                for row in selected[0][1]["analysis"]["bearing_study"]["integration_study"]
            ]
        )
        matrix = np.asarray(
            [
                [
                    row["bearing_repeatability_rms_deg"]
                    for row in run["analysis"]["bearing_study"]["integration_study"]
                ]
                for _condition, run in selected
            ]
        ).T
        image = axis.pcolormesh(
            frequency,
            cycles * cycle_us / 1000.0,
            matrix,
            cmap="viridis",
            vmin=0.0,
            vmax=30.0,
            shading="nearest",
        )
        axis.set_yscale("log")
        axis.set_ylabel("wall latency (ms)")
        axis.set_title(f"TX{tx_channel + 1}")
    axes[-1].set_xlabel("RF centre (MHz)")
    if image is not None:
        colorbar = fig.colorbar(image, ax=axes, pad=0.015)
        colorbar.set_label("bearing repeatability RMS (degrees)")
    fig.suptitle("Ideal-manifold bearing repeatability (diagnostic, not surveyed accuracy)")
    _save(fig, png / "fig05_dense_bearing_repeatability.png")


def _figure_settling(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], png: Path
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.5), sharey=True)
    for tx_channel, axis in enumerate(axes):
        for dwell_us in sorted({item[0]["dwell_us"] for item in pairs}):
            selected = [
                run
                for condition, run in pairs
                if condition["tx_channel"] == tx_channel
                and condition["dwell_us"] == dwell_us
            ]
            age = np.asarray(
                [
                    item["age_after_selected_edge_us"]
                    for item in selected[0]["analysis"]["settling_study"]
                ]
            )
            matrix = np.asarray(
                [
                    [item["ensemble_phase_rms_deg"] for item in run["analysis"]["settling_study"]]
                    for run in selected
                ]
            )
            axis.plot(
                age,
                np.percentile(matrix, 95.0, axis=0),
                label=f"{dwell_us} µs dwell",
            )
        axis.axhline(5.0, color="black", linestyle="--", linewidth=1)
        axis.set_xlabel("age after selected edge (µs)")
        axis.set_title(f"TX{tx_channel + 1}")
        axis.legend(frameon=False)
    axes[0].set_ylabel("sentinel-frequency p95 ensemble phase RMS (degrees)")
    fig.suptitle("RF-visible selected-path settling, including receiver bandwidth")
    fig.tight_layout()
    _save(fig, png / "fig06_rf_settling.png")


def _figure_port_rms(
    port_rows: list[dict[str, Any]], png: Path
) -> None:
    order = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
    fig, axes = plt.subplots(2, 1, figsize=(13, 5.8), sharex=True, constrained_layout=True)
    image = None
    for tx_port, axis in zip(("TX1", "TX2"), axes, strict=True):
        selected = [row for row in port_rows if row["tx_port"] == tx_port]
        frequencies = sorted({row["frequency_hz"] for row in selected})
        lookup = {(row["port"], row["frequency_hz"]): row for row in selected}
        matrix = np.asarray(
            [
                [
                    lookup[(port, frequency)]["phase_rms_at_weighted_10deg_gate_deg"]
                    for frequency in frequencies
                ]
                for port in order
            ],
            dtype=float,
        )
        image = axis.pcolormesh(
            np.asarray(frequencies) / 1e6,
            np.arange(len(order)),
            matrix,
            cmap="magma",
            vmin=0.0,
            vmax=60.0,
            shading="nearest",
        )
        axis.set_yticks(np.arange(len(order)), labels=order)
        axis.set_title(tx_port)
    axes[-1].set_xlabel("RF centre (MHz)")
    if image is not None:
        colorbar = fig.colorbar(image, ax=axes, pad=0.015)
        colorbar.set_label("per-port phase RMS at weighted 10° latency (degrees)")
    fig.suptitle("Per-port detail: weak/null arms remain visible instead of being averaged away")
    _save(fig, png / "fig07_dense_per_port_rms.png")


def _figure_decoder(records: list[dict[str, Any]], png: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 6.5), sharex=True)
    for tx_port in ("TX1", "TX2"):
        selected = sorted(
            (item for item in records if item["tx_port"] == tx_port),
            key=lambda item: item["frequency_hz"],
        )
        frequency = [item["frequency_hz"] / 1e6 for item in selected]
        axes[0].plot(
            frequency,
            [item["selector_clock_scale"] for item in selected],
            color=TX_COLORS[tx_port],
            label=tx_port,
        )
        axes[1].plot(
            frequency,
            [item["decode_alignment_score"] for item in selected],
            color=TX_COLORS[tx_port],
            label=tx_port,
        )
    axes[0].axhline(1.0, color="black", linestyle="--", linewidth=1)
    axes[0].set_ylabel("measured / nominal cycle")
    axes[1].set_ylabel("coherent plateau alignment score")
    axes[1].set_xlabel("RF centre (MHz)")
    for axis in axes:
        axis.legend(frameon=False)
    fig.suptitle("RF-only autonomous schedule recovery quality")
    fig.tight_layout()
    _save(fig, png / "fig08_decoder_clock_and_alignment.png")


def _figure_tx2_coherence(records: list[dict[str, Any]], png: Path) -> None:
    fig, axis = plt.subplots(figsize=(10.5, 4.8))
    for dwell_us in sorted({item["dwell_us"] for item in records}):
        selected = sorted(
            (
                item
                for item in records
                if item["tx_port"] == "TX2" and item["dwell_us"] == dwell_us
            ),
            key=lambda item: item["frequency_hz"],
        )
        axis.plot(
            [item["frequency_hz"] / 1e6 for item in selected],
            [item["frequency_difference_search_objective"] for item in selected],
            marker="o",
            label=f"{dwell_us} µs dwell",
        )
    axis.axhline(0.25, color="black", linestyle="--", linewidth=1, label="qualification")
    axis.set_xlabel("RF centre (MHz)")
    axis.set_ylabel("cross-frequency coherent-fit objective")
    axis.set_ylim(bottom=0.0)
    axis.set_title("TX2 development-pilot coherence is an explicit quality gate")
    axis.legend(frameon=False, ncol=3)
    fig.tight_layout()
    _save(fig, png / "fig09_tx2_cross_frequency_coherence.png")


def _report(
    output: Path,
    results: dict[str, Any],
    ladder_rows: list[dict[str, Any]],
) -> None:
    selected = results["selected_dwell_us"]
    dense = results["dense_by_source"]
    failure_count = results["capture_failure_count"]
    trim_summary = results["trim_sensitivity"]["summary"]
    profiles = []
    for dwell in sorted({row["dwell_us"] for row in ladder_rows}):
        cycle = 180 + 6 * (20 + dwell)
        profiles.append(
            f"| {dwell} | 20 | {cycle} | {1e6 / cycle:.1f} | "
            f"{100.0 * dwell / cycle:.2f}% |"
        )
    ladder_table = []
    for row in ladder_rows:
        latency = row["phase_10deg_wall_latency_worst_ms"]
        settle = row["phase_only_settling_p95_us"]
        ladder_table.append(
            f"| {row['dwell_us']} | {row['tx_port']} | "
            f"{row['frequency_difference_qualified_count']}/7 | "
            f"{row['phase_10deg_frequency_pass_count']}/7 | "
            f"{_fmt(latency)} | "
            f"{row['single_cycle_phase_rms_p95_deg']:.2f} | "
            f"{row['single_cycle_power_weighted_phase_rms_p95_deg']:.2f} | "
            f"{row['phase_only_settling_frequency_pass_count']}/7 | "
            f"{_fmt(settle)} |"
        )
    source_table = []
    for tx_port in ("TX1", "TX2"):
        value = dense[tx_port]
        latency = value["phase_10deg_wall_latency_ms"]
        settling = value["phase_only_settling_us"]
        source_table.append(
            f"| {tx_port} | {value['phase_10deg_frequency_pass_count']}/149 | "
            f"{value['frequency_difference_qualified_count']}/149 | "
            f"{_fmt(latency['median'])} | {_fmt(latency['p95'])} | "
            f"{_fmt(latency['maximum'])} | {_fmt(settling['p95'])} | "
            f"{value['phase_only_settling_frequency_pass_count']}/149 | "
            f"{value['full_capture_valid_bearing_percent']:.1f}% |"
        )
    trim_table = []
    for item in trim_summary:
        latency = item["first_qualified_wall_latency_ms"]
        trim_table.append(
            f"| {item['post_select_discard_us']} | "
            f"{item['median_samples_per_dwell']:.0f} | "
            f"{item['phase_10deg_frequency_pass_count']}/149 | "
            f"{_fmt(latency['median'])} | {_fmt(latency['p95'])} | "
            f"{_fmt(latency['maximum'])} |"
        )
    text = f"""# Dense 1 MHz autonomous C6 switching campaign

Date: 2026-09-03

## Outcome

The campaign first measured the estimator floor at every 1 MHz centre from 5.726 through
5.874 GHz with long, host-controlled dwells. It then tested autonomous C6 schedules at
25, 50, 100, and 200 µs on seven sentinel frequencies, selected **{selected} µs** by a
predeclared worst-frequency phase-RMS rule, and repeated that schedule at all 149 centres
for both TX1 and TX2.

The result separates four quantities that must not be conflated: the 20 µs passive
break-before-make interval, RF-visible response settling after a path is selected, the active
dwell, and the multi-cycle coherent integration needed to reach a phase-RMS target. A fast
selector can revisit all ports rapidly even when a weak signal needs many revisits before a
bearing is emitted.

![Stable-dwell baseline](png/fig01_baseline_integration.png)

## Exact autonomous schedules

Every schedule uses the physical clockwise array order `ANT1, ANT2, ANT4, ANT8, ANT7, ANT5`,
a 180 µs frame marker, and a 20 µs `ALL_OFF` guard before each active state.

| Active dwell (µs) | Guard (µs) | Full cycle (µs) | Ideal C6 scans/s | Per-port duty |
| ---: | ---: | ---: | ---: | ---: |
{os.linesep.join(profiles)}

The 20 µs guard is an experimental campaign waiver. It does not replace the released 5 ms
transition guard until independent GPIO/logic-analyzer and environmental qualification closes.

![Dwell ladder](png/fig02_dwell_ladder.png)

| Dwell µs | Src | Fit | ≤10° | Worst ms | Max p95° | Wt p95° | Settle n | Settle p95µs |
| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{os.linesep.join(ladder_table)}

Selection minimized worst-sentinel wall latency with at least eight independent groups per
frequency; a tie would favor the shorter dwell. Qualification uses received-power-weighted phase
RMS, because an antenna in a physical signal null has undefined phase and must not determine the
selector clock. Maximum-port and maximum-observable-port RMS remain in the CSV and per-port figure.
Ideal-manifold bearing validity is also reported separately: it tests the present OTA calibration
and room, not switch timing. This optimizes time-to-reliable-vector, not the largest headline scan
rate.

The runner allowed at most three attempts per condition. It retained **{failure_count}** failed
capture attempt(s) as negative evidence; only independently passed retries enter the timing and RMS
statistics. A condition that failed all three attempts would have stopped the campaign.

## Full 1 MHz result at the selected schedule

| Src | ≤10° | Fit | Median ms | p95 ms | Max ms | Settle p95µs | Settle n | Bearing valid |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{os.linesep.join(source_table)}

![Phase versus frequency and latency](png/fig03_dense_phase_frequency_latency.png)

![Qualified latency and RF settling](png/fig04_dense_qualified_latency_and_settling.png)

The stable-dwell replay is an estimator lower bound. The autonomous result includes periodic
marker/guard overhead, real switching, RF filtering, and phase aggregation across revisits.
RMS is calculated independently per port against that port's full-capture complex reference;
the qualification aggregate weights those errors by coherent received power. This is the correct
array-processing limit: a null arm contributes almost no useful likelihood information, while its
raw RMS still remains visible for diagnosis.

![Per-port RMS](png/fig07_dense_per_port_rms.png)

The per-port heat map is the companion to the weighted qualification map. It makes deep/null arms,
frequency-local fades, and any persistently weak PCB path visible rather than silently averaging
them away. Exact values, observability flags, single-visit coherent SNR, and complex-reference
magnitudes are in `data/dense_port_metrics.csv`.

## Direction-finding diagnostic

![Bearing repeatability](png/fig05_dense_bearing_repeatability.png)

These are ideal far-field manifold diagnostics after applying the PCB complex LUT. The antenna
positions were approximate (TX1 near 90°, TX2 near 180°), not surveyed calibration points, so
bearing repeatability and likelihood validity are meaningful while absolute angle error is not
yet a production accuracy claim. Installed antennas, final cables, mutual coupling, mounting,
and room multipath remain in the measured vector.

The 5.763–5.765 GHz region is an especially useful counterexample: TX1 switching phase reaches the
10° timing gate, yet the ideal-manifold solution remains invalid and ambiguous. A stable switched
vector can disagree with a wrong spatial model. Faster firmware or more averaging cannot repair
that; the next direction-finding step is a surveyed empirical installed-array manifold.

## What “switch settling” means here

![RF settling](png/fig06_rf_settling.png)

The RF settling trace advances in one-microsecond steps and uses a five-microsecond coherent window
after every selected edge, then compares each port with its full-capture per-port complex reference.
It includes the RF switch, PCB path,
AD9361 analog/digital filtering, and timing-decoder uncertainty. It therefore bounds the usable
sample age for this receiver configuration; it is not a sub-microsecond GPIO-only measurement.
The frequency summary calls phase settled after three consecutive windows meet 5° ensemble RMS
and 10° worst observable-port error. The machine-readable result also retains the stricter bound
that requires the remainder of the dwell to meet both phase and gain-error limits; missing strict
bounds remain explicit rather than being imputed.

## Post-select sample discard

![TX1 post-select discard sensitivity](png/fig10_tx1_post_select_trim_sensitivity.png)

The retained TX1 raw IQ was replayed with the leading discard varied from 5 through 30 µs while
the trailing trim remained fixed at 5 µs. The 5 µs replay reproduces every online pass/fail result
and latency exactly.

| Discard µs | Samples/dwell | ≤10° | Median ms | p95 ms | Max ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
{os.linesep.join(trim_table)}

The 30 µs discard retains roughly 328 coherent samples per selected dwell and changes neither the
147/149 pass count nor the 191.2 ms p95 latency. Use **30 µs as the provisional runtime discard**:
it covers the largest observed 27 µs phase-only bound without a measured p95 penalty. This is a
conservative software setting, not proof of universal 30 µs physical settling; 5.847 and 5.858 GHz
still fail the 10° criterion and must remain invalid rather than interpolated.

## Synchronization and measured selector clock

![Decoder clock and alignment](png/fig08_decoder_clock_and_alignment.png)

The campaign does not depend on a visibly dark `ALL_OFF` marker. RX2×RX1* provides a coherent pilot;
the decoder estimates the schedule period from six harmonics, folds thousands of cycles, and jointly
aligns the marker plus six plateaus. This survived the band-edge case that defeated amplitude-only
thresholding. Clock scale and alignment score are stored for every frequency, so decoder drift
cannot masquerade as phase RMS.

![TX2 cross-frequency coherence](png/fig09_tx2_cross_frequency_coherence.png)

TX1 is the deployment-like measurement: RX1 and RX2 observe the same emitter at the same RF
frequency, so emitter phase cancels directly. TX2 is a useful laboratory stress test built from a
frequency-separated pilot on the same source Pluto, but it does not model an arbitrary field
emitter. Its coherent-fit objective must exceed 0.25 before TX2 phase may qualify a dwell. A lower
score remains visible as a result, rather than aborting acquisition or being promoted by relaxing
the threshold.

## Recommended runtime

1. Run the selector continuously at the selected schedule and keep one uninterrupted dual-RX
   stream. Decode the periodic marker, reject any broken frame, discard the first 30 µs after each
   selected edge, and keep the final 5 µs excluded from each dwell.
2. Form `RX2 × conj(RX1)` for TX1 or apply the measured cross-frequency rotation for TX2. Apply
   the PCB complex LUT per frequency and accumulate each port across successive frames.
3. Emit a new bearing when the power-weighted phase/SNR gate and likelihood-quality gate pass.
   Flag persistently weak arms rather than waiting for an undefined phase in a spatial null. Use a
   rolling accumulator so physical scan cadence stays high while output latency adapts to signal
   strength.
4. Treat the ideal manifold as a bring-up diagnostic. For deployed direction finding, collect a
   surveyed azimuth manifold with the final antennas and cables, split train/holdout angles, and
   validate motion separately.

## Evidence and limitations

- Frequency lattice: 149 centres, 5.726–5.874 GHz inclusive, exact 1 MHz spacing.
- Sources: TX1 same-emitter conducted reference and TX2 with a frequency-separated coherent
  TX1 pilot; both use the same physical source Pluto.
- Receiver: serial `104000b29905000e17000800065934759d` at `192.168.1.15` only.
- Each autonomous run: continuous dual-RX ABI-2 capture at 2 MS/s, exact counter-continuity
  checks, source readback, raw SHA-256, and final exact source mute.
- Retry policy: at most three attempts per condition; {failure_count} rejected attempt(s) retained
  in the campaign manifest and `data/capture_failures.csv` when nonzero.
- Firmware: each image is profile-generated and schedule/symbol/memory checked, then programmed
  and byte-for-byte read back with the STM32C0-capable upstream OpenOCD revision pinned in flash
  evidence. The original 16 KiB bench image is restored, read back, and commanded `ALL_OFF` at
  campaign end.
- This campaign tests 5.8 GHz timing in the current room. It does not qualify other ISM bands,
  temperature extremes, supply variation, antenna reconnects, or multiple simultaneous emitters.

Machine-readable compact results are in [`data/results.json`](data/results.json). Per-condition
tables are in [`data/ladder_conditions.csv`](data/ladder_conditions.csv) and
[`data/dense_frequency_conditions.csv`](data/dense_frequency_conditions.csv). The six-port matrix is
[`data/dense_port_metrics.csv`](data/dense_port_metrics.csv); immutable raw IQ
and run records remain under `/srv/bulk/samteway/lab-data/`. Post-select replay details are in
[`data/trim_sensitivity.json`](data/trim_sensitivity.json) and
[`data/trim_sensitivity_summary.csv`](data/trim_sensitivity_summary.csv).
"""
    (output / "README.md").write_text(text, encoding="utf-8")


def main() -> int:
    args = _parser().parse_args()
    baseline = _load(args.baseline.resolve(strict=True))
    campaign = _load(args.fast_campaign.resolve(strict=True))
    trim = _load(args.trim_analysis.resolve(strict=True))
    if baseline.get("campaign_status") != "complete" or baseline.get(
        "completed_condition_count"
    ) != 298:
        raise SystemExit("dense stable-dwell baseline is incomplete")
    if campaign.get("status") != "complete":
        raise SystemExit("autonomous timing campaign is incomplete")
    if (
        trim.get("online_5us_crosscheck", {}).get("passed") is not True
        or trim.get("campaign", {}).get("sha256") != _sha256(args.fast_campaign)
        or len(trim.get("summary", [])) != 6
    ):
        raise SystemExit("post-select trim analysis is invalid or belongs to another campaign")
    selection = campaign.get("selection")
    if not isinstance(selection, dict) or selection.get("selected_dwell_us") not in (
        25,
        50,
        100,
        200,
    ):
        raise SystemExit("autonomous campaign selection is invalid")
    restore = campaign.get("restore_evidence")
    if not isinstance(restore, dict):
        raise SystemExit("autonomous campaign has no selector restore evidence")
    restore_path = Path(str(restore.get("path", ""))).resolve(strict=True)
    if _sha256(restore_path) != restore.get("sha256") or _load(restore_path).get(
        "status"
    ) != "passed":
        raise SystemExit("selector restore evidence is invalid")
    ladder_pairs = _validated_runs(campaign, "ladder_conditions", 56)
    dense_pairs = _validated_runs(campaign, "dense_conditions", 298)
    ladder_keys = {
        (item[0]["dwell_us"], item[0]["frequency_hz"], item[0]["tx_channel"])
        for item in ladder_pairs
    }
    expected_ladder_keys = {
        (dwell_us, frequency_hz, tx_channel)
        for dwell_us in (25, 50, 100, 200)
        for frequency_hz in (5_726_000_000, 5_750_000_000, 5_775_000_000,
                             5_800_000_000, 5_825_000_000, 5_850_000_000,
                             5_874_000_000)
        for tx_channel in (0, 1)
    }
    dense_keys = {
        (item[0]["frequency_hz"], item[0]["tx_channel"])
        for item in dense_pairs
    }
    expected_dense_keys = {
        (frequency_hz, tx_channel)
        for frequency_hz in range(5_726_000_000, 5_874_000_001, 1_000_000)
        for tx_channel in (0, 1)
    }
    if ladder_keys != expected_ladder_keys or dense_keys != expected_dense_keys:
        raise SystemExit("campaign condition lattice is incomplete or duplicated")
    ladder_records = _flatten(ladder_pairs)
    dense_records = _flatten(dense_pairs)
    dense_port_rows = _flatten_ports(dense_pairs)
    failure_rows = _flatten_failures(campaign)
    ladder_rows = _ladder_summary(ladder_records)
    dense_by_source = _dense_summary(dense_records)
    results = {
        "schema": 1,
        "campaign_id": campaign["campaign_id"],
        "baseline_campaign_id": baseline["campaign_id"],
        "frequency_grid_hz": {
            "start": 5_726_000_000,
            "stop": 5_874_000_000,
            "step": 1_000_000,
            "count": 149,
        },
        "selected_dwell_us": selection["selected_dwell_us"],
        "selection": selection,
        "ladder_summary": ladder_rows,
        "dense_by_source": dense_by_source,
        "capture_failure_count": len(failure_rows),
        "trim_sensitivity": {
            "online_5us_crosscheck": trim["online_5us_crosscheck"],
            "trailing_trim_us": trim["trailing_trim_us"],
            "summary": trim["summary"],
        },
        "source_evidence": {
            "baseline_summary": {
                "path": str(args.baseline.resolve()),
                "sha256": _sha256(args.baseline),
            },
            "fast_campaign": {
                "path": str(args.fast_campaign.resolve()),
                "sha256": _sha256(args.fast_campaign),
            },
            "selector_restore": restore,
            "trim_sensitivity": {
                "path": str(args.trim_analysis.resolve()),
                "sha256": _sha256(args.trim_analysis),
            },
        },
    }
    data = args.output / "data"
    png = args.output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    _write_json(data / "results.json", results)
    _write_csv(data / "ladder_conditions.csv", ladder_records)
    _write_csv(data / "dense_frequency_conditions.csv", dense_records)
    _write_csv(data / "dense_port_metrics.csv", dense_port_rows)
    _write_csv(data / "ladder_summary.csv", ladder_rows)
    if failure_rows:
        _write_csv(data / "capture_failures.csv", failure_rows)
    _style()
    _figure_baseline(baseline, png)
    _figure_ladder(ladder_records, png)
    selected_dwell = int(selection["selected_dwell_us"])
    _figure_dense_phase(dense_pairs, png, selected_dwell)
    _figure_dense_summary(dense_records, png)
    _figure_bearing(dense_pairs, png, selected_dwell)
    _figure_settling(ladder_pairs, png)
    _figure_port_rms(dense_port_rows, png)
    _figure_decoder(dense_records, png)
    _figure_tx2_coherence(ladder_records, png)
    _report(args.output, results, ladder_rows)
    print(
        json.dumps(
            {
                "report": str(args.output / "README.md"),
                "results": str(data / "results.json"),
                "figures": str(png),
                "selected_dwell_us": selected_dwell,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
