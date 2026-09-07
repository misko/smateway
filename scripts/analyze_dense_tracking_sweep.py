#!/usr/bin/env python3
"""Incrementally analyze dense tracking frequency and switching-time evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from smateway.tracking import SampleTimeBlock, realtime_to_sample_sequence

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN = Path(
    "/srv/bulk/samteway/lab-data/tracking-dense-1mhz-20260903-v1/campaign.json"
)
DEFAULT_OUTPUT = Path(
    "/srv/bulk/samteway/lab-data/tracking-dense-1mhz-20260903-v1/analysis"
)
INTEGRATION_MS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)
SETTLING_GUARD_US = (
    0,
    100,
    250,
    500,
    750,
    1_000,
    1_500,
    2_000,
    3_000,
    4_000,
    5_000,
    6_000,
    8_000,
    10_000,
)
SETTLING_INTEGRATION_MS = 20.0
SAMPLE_RATE_HZ = 1_000_000.0
COLORS = {"TX1": "#0072B2", "TX2": "#D55E00"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return document


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _coherent_snr_db(means: np.ndarray, powers: np.ndarray, count: int) -> np.ndarray:
    coherence = np.minimum(
        1.0,
        np.abs(means) / np.sqrt(np.maximum(powers, np.finfo(float).tiny)),
    )
    fraction = np.clip(
        coherence**2,
        np.finfo(float).eps,
        1.0 - np.finfo(float).eps,
    )
    return 10.0 * np.log10(count * fraction / (1.0 - fraction))


def _window_statistics(product: np.ndarray, window_samples: int) -> tuple[np.ndarray, np.ndarray]:
    usable = (product.size // window_samples) * window_samples
    if usable < window_samples:
        raise ValueError("analysis window is longer than a stable dwell")
    windows = product[:usable].reshape(-1, window_samples)
    return np.mean(windows, axis=1), np.mean(np.abs(windows) ** 2, axis=1)


def _phase_metrics(errors_deg: np.ndarray, snr_db: np.ndarray) -> dict[str, Any]:
    finite = np.isfinite(errors_deg) & np.isfinite(snr_db)
    errors = errors_deg[finite]
    snr = snr_db[finite]
    if not errors.size:
        raise ValueError("phase metric contains no finite observations")
    admitted = snr >= 5.0
    admitted_errors = errors[admitted]
    return {
        "observation_count": int(errors.size),
        "snr_admitted_count": int(admitted_errors.size),
        "snr_admission_percent": float(100.0 * np.mean(admitted)),
        "phase_error_rms_deg_all": float(np.sqrt(np.mean(errors**2))),
        "absolute_phase_error_p95_deg_all": float(np.percentile(np.abs(errors), 95.0)),
        "phase_error_rms_deg_snr_admitted": (
            float(np.sqrt(np.mean(admitted_errors**2))) if admitted_errors.size else None
        ),
        "absolute_phase_error_p95_deg_snr_admitted": (
            float(np.percentile(np.abs(admitted_errors), 95.0))
            if admitted_errors.size
            else None
        ),
        "joint_phase_10deg_snr_5db_percent": float(
            100.0 * np.mean((np.abs(errors) <= 10.0) & admitted)
        ),
        "coherent_snr_db_median": float(np.median(snr)),
        "coherent_snr_db_p05": float(np.percentile(snr, 5.0)),
    }


def _timeline(run: dict[str, Any]) -> tuple[SampleTimeBlock, ...]:
    return tuple(SampleTimeBlock(**item) for item in run["capture"]["timeline"])


def _product_for_interval(
    rx1: np.memmap,
    rx2: np.memmap,
    *,
    start: int,
    stop: int,
    first_sequence: int,
    difference_hz: float | None,
) -> np.ndarray:
    product = np.asarray(rx2[start:stop], dtype=np.complex128) * np.conjugate(
        np.asarray(rx1[start:stop], dtype=np.complex128)
    )
    if difference_hz is not None:
        indices = first_sequence + np.arange(start, stop, dtype=np.float64)
        product *= np.exp(-2j * np.pi * difference_hz * indices / SAMPLE_RATE_HZ)
    return product


def _analyze_run(run_path: Path) -> dict[str, Any]:
    run = _load_json(run_path)
    if run.get("status") != "passed":
        raise ValueError(f"dense run is not passed: {run_path}")
    capture = run["capture"]
    analysis = run["analysis"]
    configuration = run["configuration"]
    count = capture["total_samples_per_channel"]
    rx1 = np.memmap(capture["raw"]["rx1_path"], mode="r", dtype=np.complex64, shape=(count,))
    rx2 = np.memmap(capture["raw"]["rx2_path"], mode="r", dtype=np.complex64, shape=(count,))
    first_sequence = capture["first_sample_sequence"]
    difference_raw = analysis["tone_frequency"].get("frequency_difference_hz")
    difference_hz = float(difference_raw) if difference_raw is not None else None
    intervals = analysis["intervals"]

    integration_observations: dict[float, dict[str, list[float]]] = {
        duration: {"errors": [], "snr": []} for duration in INTEGRATION_MS
    }
    late_by_dwell: list[complex] = []
    late_snr_by_dwell: list[float] = []
    for interval in intervals:
        start = int(interval["start"]) + 10_000
        stop = int(interval["stop"]) - 10_000
        product = _product_for_interval(
            rx1,
            rx2,
            start=start,
            stop=stop,
            first_sequence=first_sequence,
            difference_hz=difference_hz,
        )
        full = complex(np.mean(product))
        late_by_dwell.append(full)
        full_power = float(np.mean(np.abs(product) ** 2))
        late_snr_by_dwell.append(
            float(_coherent_snr_db(np.asarray([full]), np.asarray([full_power]), product.size)[0])
        )
        for duration_ms in INTEGRATION_MS:
            window_samples = round(duration_ms * SAMPLE_RATE_HZ / 1000.0)
            means, powers = _window_statistics(product, window_samples)
            errors = np.angle(means / full, deg=True)
            snr = _coherent_snr_db(means, powers, window_samples)
            integration_observations[duration_ms]["errors"].extend(errors.tolist())
            integration_observations[duration_ms]["snr"].extend(snr.tolist())

    integration = []
    for duration_ms, values in integration_observations.items():
        integration.append(
            {
                "integration_ms": duration_ms,
                **_phase_metrics(np.asarray(values["errors"]), np.asarray(values["snr"])),
            }
        )

    blocks = _timeline(run)
    events = capture["selector_events"]
    active_events = [event for event in events if event["state"] != "ALL_OFF"]
    if len(active_events) != len(intervals):
        raise ValueError("selector active-event count differs from admitted intervals")
    settling_observations: dict[int, dict[str, list[float]]] = {
        guard: {"errors": [], "snr": []} for guard in SETTLING_GUARD_US
    }
    settling_samples = round(SETTLING_INTEGRATION_MS * SAMPLE_RATE_HZ / 1000.0)
    event_brackets_us: list[float] = []
    for dwell_index, (event, interval) in enumerate(zip(active_events, intervals, strict=True)):
        if event["state"] != interval["port"]:
            raise ValueError("selector event order differs from admitted interval order")
        event_brackets_us.append(
            (event["request_end_realtime_ns"] - event["request_start_realtime_ns"]) / 1000.0
        )
        late = late_by_dwell[dwell_index]
        for guard_us in SETTLING_GUARD_US:
            start_realtime_ns = event["request_end_realtime_ns"] + guard_us * 1000
            absolute = realtime_to_sample_sequence(start_realtime_ns, blocks)
            start = math.ceil(absolute - first_sequence)
            stop = start + settling_samples
            if start < 0 or stop > count:
                continue
            product = _product_for_interval(
                rx1,
                rx2,
                start=start,
                stop=stop,
                first_sequence=first_sequence,
                difference_hz=difference_hz,
            )
            mean = complex(np.mean(product))
            power = float(np.mean(np.abs(product) ** 2))
            error = float(np.angle(mean / late, deg=True))
            snr = float(
                _coherent_snr_db(
                    np.asarray([mean]), np.asarray([power]), settling_samples
                )[0]
            )
            settling_observations[guard_us]["errors"].append(error)
            settling_observations[guard_us]["snr"].append(snr)
    settling = []
    for guard_us, values in settling_observations.items():
        settling.append(
            {
                "post_command_guard_us": guard_us,
                "integration_ms": SETTLING_INTEGRATION_MS,
                **_phase_metrics(np.asarray(values["errors"]), np.asarray(values["snr"])),
            }
        )

    far = analysis["ideal_far_field_bearing"]
    ports = analysis["ports"]
    return {
        "schema": 1,
        "run_id": run["run_id"],
        "run_json": str(run_path),
        "frequency_hz": configuration["frequency_hz"],
        "tx_channel": configuration["tx_channel"],
        "tx_port": configuration["tx_port"],
        "sample_count_per_channel": count,
        "frame_count": capture["frame_count"],
        "maximum_sample_time_uncertainty_us": max(
            item["uncertainty_ns"] for item in capture["timeline"]
        )
        / 1000.0,
        "command_marker_bracket_us": {
            "minimum": min(event_brackets_us),
            "median": float(np.median(event_brackets_us)),
            "maximum": max(event_brackets_us),
        },
        "minimum_full_dwell_coherent_snr_db": min(late_snr_by_dwell),
        "maximum_repeat_phase_rms_deg": analysis["maximum_repeat_phase_rms_deg"],
        "per_port_repeat_phase_rms_deg": {
            item["port"]: item["repeat_phase_rms_deg"] for item in ports
        },
        "ideal_far_field": {
            "valid": far["valid"],
            "bearing_deg": far["bearing_deg_clockwise_from_forward"],
            "score": far["score"],
            "ambiguity_margin_db": far["ambiguity_margin_db"],
            "residual_phase_rms_deg": far["residual_phase_rms_deg"],
            "reasons": far["reasons"],
        },
        "integration_time_study": integration,
        "post_command_guard_study": settling,
    }


def _cache_run(run_path: Path, cache_root: Path) -> dict[str, Any]:
    run = _load_json(run_path)
    cache_path = cache_root / f"{run['run_id']}.json"
    if cache_path.exists():
        cached = _load_json(cache_path)
        if cached.get("run_json") != str(run_path):
            raise ValueError("dense analysis cache path binding differs")
        return cached
    result = _analyze_run(run_path)
    _write_json_atomic(cache_path, result)
    return result


def _aggregate(records: list[dict[str, Any]], campaign: dict[str, Any]) -> dict[str, Any]:
    by_tx: dict[str, list[dict[str, Any]]] = {}
    for tx_port in ("TX1", "TX2"):
        selected = sorted(
            (record for record in records if record["tx_port"] == tx_port),
            key=lambda record: record["frequency_hz"],
        )
        integration = []
        for index, duration_ms in enumerate(INTEGRATION_MS):
            values = [record["integration_time_study"][index] for record in selected]
            rms_values = np.asarray(
                [item["phase_error_rms_deg_snr_admitted"] for item in values], dtype=float
            )
            pass_values = np.asarray(
                [item["joint_phase_10deg_snr_5db_percent"] for item in values]
            )
            integration.append(
                {
                    "integration_ms": duration_ms,
                    "frequency_count": len(values),
                    "phase_rms_finite_frequency_count": int(
                        np.count_nonzero(np.isfinite(rms_values))
                    ),
                    "phase_rms_deg_median_across_frequencies": float(
                        np.nanmedian(rms_values)
                    ),
                    "phase_rms_deg_p95_across_frequencies": float(
                        np.nanpercentile(rms_values, 95.0)
                    ),
                    "joint_admission_percent_median_across_frequencies": float(
                        np.median(pass_values)
                    ),
                    "joint_admission_percent_p05_across_frequencies": float(
                        np.percentile(pass_values, 5.0)
                    ),
                }
            )
        settling = []
        for index, guard_us in enumerate(SETTLING_GUARD_US):
            values = [record["post_command_guard_study"][index] for record in selected]
            rms_values = np.asarray(
                [item["phase_error_rms_deg_snr_admitted"] for item in values], dtype=float
            )
            pass_values = np.asarray(
                [item["joint_phase_10deg_snr_5db_percent"] for item in values]
            )
            settling.append(
                {
                    "post_command_guard_us": guard_us,
                    "integration_ms": SETTLING_INTEGRATION_MS,
                    "frequency_count": len(values),
                    "phase_rms_finite_frequency_count": int(
                        np.count_nonzero(np.isfinite(rms_values))
                    ),
                    "phase_rms_deg_median_across_frequencies": float(
                        np.nanmedian(rms_values)
                    ),
                    "phase_rms_deg_p95_across_frequencies": float(
                        np.nanpercentile(rms_values, 95.0)
                    ),
                    "joint_admission_percent_median_across_frequencies": float(
                        np.median(pass_values)
                    ),
                    "joint_admission_percent_p05_across_frequencies": float(
                        np.percentile(pass_values, 5.0)
                    ),
                }
            )
        by_tx[tx_port] = {
            "frequency_count": len(selected),
            "integration_time_study": integration,
            "post_command_guard_study": settling,
        }
    return {
        "schema": 1,
        "campaign_id": campaign["campaign_id"],
        "campaign_status": campaign["status"],
        "completed_condition_count": len(records),
        "planned_condition_count": campaign["configuration"]["condition_count"],
        "analysis_scope": {
            "integration_time_ms": list(INTEGRATION_MS),
            "post_command_guard_us": list(SETTLING_GUARD_US),
            "settling_integration_ms": SETTLING_INTEGRATION_MS,
            "phase_target_deg": 10.0,
            "coherent_snr_target_db": 5.0,
        },
        "by_source": by_tx,
        "records": records,
    }


def _matrix(
    records: list[dict[str, Any]],
    tx_port: str,
    study_name: str,
    metric: str,
) -> tuple[np.ndarray, np.ndarray]:
    selected = sorted(
        (record for record in records if record["tx_port"] == tx_port),
        key=lambda record: record["frequency_hz"],
    )
    frequencies = np.asarray([record["frequency_hz"] / 1e6 for record in selected])
    matrix = np.empty((0, frequencies.size), dtype=float)
    if selected:
        row_count = len(selected[0][study_name])
        matrix = np.asarray(
            [
                [record[study_name][row][metric] for record in selected]
                for row in range(row_count)
            ],
            dtype=float,
        )
    return frequencies, matrix


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


def _heatmap_figure(
    records: list[dict[str, Any]],
    output: Path,
    *,
    study_name: str,
    metric: str,
    rows: tuple[float, ...],
    row_label: str,
    title: str,
    filename: str,
    vmax: float,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True, constrained_layout=True)
    image = None
    for axis, tx_port in zip(axes, ("TX1", "TX2"), strict=True):
        frequencies, matrix = _matrix(records, tx_port, study_name, metric)
        if not frequencies.size:
            continue
        extent = [frequencies[0] - 0.5, frequencies[-1] + 0.5, -0.5, len(rows) - 0.5]
        image = axis.imshow(
            matrix,
            origin="lower",
            aspect="auto",
            extent=extent,
            cmap="magma",
            vmin=0.0,
            vmax=vmax,
        )
        axis.set_yticks(range(len(rows)), [f"{value:g}" for value in rows])
        axis.set_ylabel(row_label)
        axis.set_title(tx_port)
    axes[-1].set_xlabel("RF centre (MHz)")
    if image is not None:
        colorbar = fig.colorbar(image, ax=axes, pad=0.015)
        colorbar.set_label("phase RMS (degrees), SNR-admitted observations")
    fig.suptitle(title)
    _save(fig, output / filename)


def _summary_curves(summary: dict[str, Any], output: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
    for tx_port in ("TX1", "TX2"):
        by_source = summary["by_source"][tx_port]
        integration = by_source["integration_time_study"]
        settling = by_source["post_command_guard_study"]
        axes[0].plot(
            [item["integration_ms"] for item in integration],
            [item["phase_rms_deg_p95_across_frequencies"] for item in integration],
            marker="o",
            color=COLORS[tx_port],
            label=tx_port,
        )
        axes[1].plot(
            [item["post_command_guard_us"] for item in settling],
            [item["phase_rms_deg_p95_across_frequencies"] for item in settling],
            marker="o",
            color=COLORS[tx_port],
            label=tx_port,
        )
    axes[0].set_xscale("log")
    axes[0].set_xlabel("stable coherent integration per state (ms)")
    axes[0].set_ylabel("across-frequency p95 phase RMS (degrees)")
    axes[1].set_xlabel("guard after command marker (microseconds)")
    axes[1].set_ylabel("across-frequency p95 phase RMS (degrees)")
    for axis in axes:
        axis.axhline(10.0, color="black", linestyle="--", linewidth=1, label="10° target")
        axis.legend(frameon=False)
    fig.suptitle("Switching/integration timing tradeoff over completed dense frequencies")
    fig.tight_layout()
    _save(fig, output / "fig03_timing_summary_curves.png")


def _frequency_quality(records: list[dict[str, Any]], output: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex=True)
    for tx_port in ("TX1", "TX2"):
        selected = sorted(
            (record for record in records if record["tx_port"] == tx_port),
            key=lambda record: record["frequency_hz"],
        )
        frequency = [record["frequency_hz"] / 1e6 for record in selected]
        axes[0, 0].plot(
            frequency,
            [record["maximum_repeat_phase_rms_deg"] for record in selected],
            color=COLORS[tx_port],
            label=tx_port,
        )
        axes[0, 1].plot(
            frequency,
            [record["minimum_full_dwell_coherent_snr_db"] for record in selected],
            color=COLORS[tx_port],
            label=tx_port,
        )
        axes[1, 0].plot(
            frequency,
            [record["ideal_far_field"]["bearing_deg"] for record in selected],
            color=COLORS[tx_port],
            label=tx_port,
        )
        axes[1, 1].plot(
            frequency,
            [record["ideal_far_field"]["residual_phase_rms_deg"] for record in selected],
            color=COLORS[tx_port],
            label=tx_port,
        )
    labels = (
        "forward/reverse repeat phase RMS (degrees)",
        "minimum full-dwell coherent SNR (dB)",
        "ideal diagnostic bearing (degrees)",
        "ideal-manifold residual phase RMS (degrees)",
    )
    for axis, label in zip(axes.flat, labels, strict=True):
        axis.set_ylabel(label)
        axis.set_xlabel("RF centre (MHz)")
        axis.legend(frameon=False)
    fig.suptitle("Dense campaign acquisition and ideal-manifold diagnostics")
    fig.tight_layout()
    _save(fig, output / "fig01_frequency_quality.png")


def _render(records: list[dict[str, Any]], summary: dict[str, Any], output: Path) -> None:
    png = output / "png"
    png.mkdir(parents=True, exist_ok=True)
    _style()
    _frequency_quality(records, png)
    _heatmap_figure(
        records,
        png,
        study_name="integration_time_study",
        metric="phase_error_rms_deg_snr_admitted",
        rows=INTEGRATION_MS,
        row_label="integration per state (ms)",
        title="Stable-dwell integration time versus frequency and phase RMS",
        filename="fig02_integration_frequency_rms.png",
        vmax=45.0,
    )
    _heatmap_figure(
        records,
        png,
        study_name="post_command_guard_study",
        metric="phase_error_rms_deg_snr_admitted",
        rows=tuple(float(value) for value in SETTLING_GUARD_US),
        row_label="post-command guard (microseconds)",
        title=(
            "Post-command guard versus frequency and phase RMS "
            f"({SETTLING_INTEGRATION_MS:g} ms integration)"
        ),
        filename="fig04_guard_frequency_rms.png",
        vmax=45.0,
    )
    _summary_curves(summary, png)


def main() -> int:
    args = _parser().parse_args()
    campaign = _load_json(args.campaign)
    conditions = campaign.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise SystemExit("dense campaign has no completed conditions")
    args.output.mkdir(parents=True, exist_ok=True)
    cache_root = args.output / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    records = [
        _cache_run(Path(str(condition["run_json"])), cache_root) for condition in conditions
    ]
    records.sort(key=lambda record: (record["frequency_hz"], record["tx_channel"]))
    summary = _aggregate(records, campaign)
    _write_json_atomic(args.output / "summary.json", summary)
    _render(records, summary, args.output)
    print(
        json.dumps(
            {
                "campaign_status": campaign["status"],
                "completed_conditions": len(records),
                "planned_conditions": campaign["configuration"]["condition_count"],
                "summary": str(args.output / "summary.json"),
                "figures": str(args.output / "png"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
