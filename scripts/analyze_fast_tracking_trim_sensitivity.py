#!/usr/bin/env python3
"""Re-evaluate dense C6 TX1 phase RMS versus post-select sample discard."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN = Path(
    "/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v6/campaign.json"
)
DEFAULT_OUTPUT = ROOT / "docs/fast_tracking_timing_campaign"
POST_SELECT_DISCARDS_US = (5, 10, 15, 20, 25, 30)
TRAILING_TRIM_US = 5
GROUPING_CYCLES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048)
PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
SAMPLE_RATE_HZ = 2_000_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
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


def _interval_bounds(
    run: dict[str, Any], post_select_discard_us: int
) -> tuple[np.ndarray, np.ndarray, int]:
    configuration = run["configuration"]
    dwell_us = int(configuration["profile_id"].split("-")[-2].removesuffix("us"))
    decode = run["analysis"]["schedule_decode"]
    markers = np.asarray(decode["marker_end_bins"], dtype=np.float64)
    if markers.ndim != 1 or markers.size < 9 or not np.all(np.diff(markers) > 0):
        raise ValueError("stored selector marker chain is invalid")
    cycle_us = 180 + len(PORTS) * (20 + dwell_us)
    scale = np.diff(markers) / cycle_us
    port_offset_us = np.arange(len(PORTS), dtype=np.float64) * (dwell_us + 20)
    predicted_start_bins = markers[:-1, None] + scale[:, None] * port_offset_us
    predicted_stop_bins = predicted_start_bins + scale[:, None] * dwell_us
    samples_per_bin = SAMPLE_RATE_HZ / 1e6
    leading_samples = post_select_discard_us * SAMPLE_RATE_HZ / 1e6
    trailing_samples = TRAILING_TRIM_US * SAMPLE_RATE_HZ / 1e6
    starts = np.ceil(predicted_start_bins * samples_per_bin + leading_samples).astype(np.int64)
    stops = np.floor(predicted_stop_bins * samples_per_bin - trailing_samples).astype(np.int64)
    sample_count = int(run["capture"]["total_samples_per_channel"])
    if (
        np.any(stops <= starts)
        or np.any(starts < 0)
        or np.any(stops > sample_count)
    ):
        raise ValueError("trimmed interval bounds are invalid")
    return starts, stops, dwell_us


def _phase_metrics(
    matrix: np.ndarray,
    *,
    cycle_scale: float,
    cycle_us: int,
) -> list[dict[str, Any]]:
    references = np.mean(matrix, axis=0)
    weights = np.abs(references) ** 2
    strongest = float(np.max(np.abs(references)))
    observable = np.abs(references) >= 0.1 * strongest
    if np.count_nonzero(observable) < 4 or not np.any(weights > 0.0):
        raise ValueError("trimmed analysis has fewer than four observable ports")
    rows = []
    for cycles in GROUPING_CYCLES:
        usable = matrix.shape[0] // cycles * cycles
        if not usable:
            continue
        grouped = matrix[:usable].reshape(-1, cycles, len(PORTS)).mean(axis=1)
        per_port = np.sqrt(
            np.mean(np.angle(grouped / references[None, :], deg=True) ** 2, axis=0)
        )
        rows.append(
            {
                "cycles_averaged": cycles,
                "groups_per_port": int(grouped.shape[0]),
                "measured_wall_latency_ms": cycles * cycle_us * cycle_scale / 1000.0,
                "phase_rms_deg_maximum_port": float(np.max(per_port)),
                "phase_rms_deg_maximum_observable_port": float(
                    np.max(per_port[observable])
                ),
                "phase_rms_deg_power_weighted_ports": float(
                    np.sqrt(np.average(per_port**2, weights=weights))
                ),
            }
        )
    return rows


def _condition(
    run: dict[str, Any], post_select_discards_us: tuple[int, ...]
) -> list[dict[str, Any]]:
    raw = run["capture"]["raw"]
    sample_count = int(run["capture"]["total_samples_per_channel"])
    rx1 = np.memmap(
        Path(raw["rx1_path"]), mode="r", dtype=np.complex64, shape=(sample_count,)
    )
    rx2 = np.memmap(
        Path(raw["rx2_path"]), mode="r", dtype=np.complex64, shape=(sample_count,)
    )
    product = np.asarray(rx2 * np.conjugate(rx1), dtype=np.complex64)
    prefix = np.empty(sample_count + 1, dtype=np.complex128)
    prefix[0] = 0.0
    np.cumsum(product, dtype=np.complex128, out=prefix[1:])
    rows = []
    for discard_us in post_select_discards_us:
        starts, stops, dwell_us = _interval_bounds(run, discard_us)
        means = (prefix[stops] - prefix[starts]) / (stops - starts)
        cycle_us = 180 + len(PORTS) * (20 + dwell_us)
        study = _phase_metrics(
            means,
            cycle_scale=float(run["analysis"]["schedule_decode"]["cycle_scale_median"]),
            cycle_us=cycle_us,
        )
        qualified = next(
            (
                item
                for item in study
                if item["groups_per_port"] >= 8
                and item["phase_rms_deg_power_weighted_ports"] <= 10.0
            ),
            None,
        )
        rows.append(
            {
                "post_select_discard_us": discard_us,
                "median_samples_per_dwell": float(np.median(stops - starts)),
                "first_qualified_wall_latency_ms": (
                    None if qualified is None else qualified["measured_wall_latency_ms"]
                ),
                "first_qualified_cycles": (
                    None if qualified is None else qualified["cycles_averaged"]
                ),
                "single_cycle_power_weighted_phase_rms_deg": study[0][
                    "phase_rms_deg_power_weighted_ports"
                ],
                "integration_study": study,
            }
        )
    return rows


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p95": None, "maximum": None}
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": int(array.size),
        "median": float(np.median(array)),
        "p95": float(np.percentile(array, 95.0)),
        "maximum": float(np.max(array)),
    }


def _summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for discard_us in POST_SELECT_DISCARDS_US:
        selected = [
            item for item in rows if item["post_select_discard_us"] == discard_us
        ]
        latency = [
            float(item["first_qualified_wall_latency_ms"])
            for item in selected
            if item["first_qualified_wall_latency_ms"] is not None
        ]
        result.append(
            {
                "post_select_discard_us": discard_us,
                "frequency_count": len(selected),
                "phase_10deg_frequency_pass_count": len(latency),
                "phase_10deg_frequency_pass_percent": 100.0 * len(latency) / len(selected),
                "median_samples_per_dwell": float(
                    np.median([item["median_samples_per_dwell"] for item in selected])
                ),
                "first_qualified_wall_latency_ms": _stats(latency),
                "single_cycle_power_weighted_phase_rms_p95_deg": float(
                    np.percentile(
                        [
                            item["single_cycle_power_weighted_phase_rms_deg"]
                            for item in selected
                        ],
                        95.0,
                    )
                ),
            }
        )
    return result


def _figure(
    rows: list[dict[str, Any]], summary: list[dict[str, Any]], output: Path
) -> None:
    frequencies = sorted({item["frequency_hz"] for item in rows})
    lookup = {
        (item["post_select_discard_us"], item["frequency_hz"]): item
        for item in rows
    }
    matrix = np.asarray(
        [
            [
                lookup[(discard, frequency)]["first_qualified_wall_latency_ms"]
                for frequency in frequencies
            ]
            for discard in POST_SELECT_DISCARDS_US
        ],
        dtype=float,
    )
    fig, axes = plt.subplots(2, 1, figsize=(13, 7.2), constrained_layout=True)
    image = axes[0].pcolormesh(
        np.asarray(frequencies) / 1e6,
        np.asarray(POST_SELECT_DISCARDS_US),
        matrix,
        cmap="viridis",
        shading="nearest",
    )
    axes[0].set_ylabel("discard after select (µs)")
    axes[0].set_title("TX1 first ≤10° weighted-RMS wall latency")
    colorbar = fig.colorbar(image, ax=axes[0], pad=0.015)
    colorbar.set_label("wall latency (ms); white = no qualification")
    discards = [item["post_select_discard_us"] for item in summary]
    axes[1].plot(
        discards,
        [item["phase_10deg_frequency_pass_percent"] for item in summary],
        color="#0072B2",
        marker="o",
        label="frequency pass rate",
    )
    axes[1].set_xlabel("discard after select (µs)")
    axes[1].set_ylabel("≤10° frequency pass rate (%)", color="#0072B2")
    axes[1].tick_params(axis="y", labelcolor="#0072B2")
    latency_axis = axes[1].twinx()
    latency_axis.plot(
        discards,
        [item["first_qualified_wall_latency_ms"]["p95"] for item in summary],
        color="#D55E00",
        marker="s",
        label="p95 qualifying latency",
    )
    latency_axis.set_ylabel("p95 qualifying latency (ms)", color="#D55E00")
    latency_axis.tick_params(axis="y", labelcolor="#D55E00")
    axes[1].set_ylim(0.0, 105.0)
    fig.suptitle("Post-select discard sensitivity from retained dense raw IQ")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> int:
    args = _parser().parse_args()
    campaign_path = args.campaign.resolve(strict=True)
    campaign = _load(campaign_path)
    if campaign.get("status") != "complete":
        raise SystemExit("fast timing campaign is incomplete")
    conditions = campaign.get("dense_conditions")
    if not isinstance(conditions, list) or len(conditions) != 298:
        raise SystemExit("fast timing campaign does not contain 298 dense conditions")
    selected = sorted(
        (item for item in conditions if item["tx_channel"] == 0),
        key=lambda item: item["frequency_hz"],
    )
    if len(selected) != 149:
        raise SystemExit("fast timing campaign does not contain 149 TX1 conditions")
    rows = []
    crosscheck_errors_ms = []
    for ordinal, condition in enumerate(selected, start=1):
        run_path = Path(condition["run_json"]).resolve(strict=True)
        if _sha256(run_path) != condition["run_sha256"]:
            raise ValueError(f"run hash differs: {run_path}")
        run = _load(run_path)
        if run.get("status") != "passed" or run["configuration"]["tx_channel"] != 0:
            raise ValueError(f"TX1 run evidence is invalid: {run_path}")
        print(
            f"[{ordinal}/149] {condition['frequency_hz'] / 1e6:.3f} MHz",
            flush=True,
        )
        condition_results = _condition(run, POST_SELECT_DISCARDS_US)
        online_qualified = next(
            (
                item
                for item in run["analysis"]["integration_study"]
                if item["groups_per_port"] >= 8
                and item["phase_rms_deg_power_weighted_ports"] <= 10.0
            ),
            None,
        )
        online_latency = (
            None
            if online_qualified is None
            else float(online_qualified["measured_wall_latency_ms"])
        )
        replay_latency = condition_results[0]["first_qualified_wall_latency_ms"]
        if (online_latency is None) != (replay_latency is None):
            raise ValueError(f"5 us replay qualification differs: {run_path}")
        if online_latency is not None and replay_latency is not None:
            error_ms = abs(online_latency - replay_latency)
            if error_ms > 1e-9:
                raise ValueError(f"5 us replay latency differs: {run_path}")
            crosscheck_errors_ms.append(error_ms)
        for result in condition_results:
            result.update(
                {
                    "frequency_hz": condition["frequency_hz"],
                    "run_json": str(run_path),
                }
            )
            rows.append(result)
    summary = _summary(rows)
    document = {
        "schema": 1,
        "evidence_kind": "tx1_post_select_discard_sensitivity",
        "campaign": {"path": str(campaign_path), "sha256": _sha256(campaign_path)},
        "source": "TX1 same-frequency same-emitter reference",
        "frequency_grid_hz": {
            "start": 5_726_000_000,
            "stop": 5_874_000_000,
            "step": 1_000_000,
            "count": 149,
        },
        "post_select_discards_us": list(POST_SELECT_DISCARDS_US),
        "trailing_trim_us": TRAILING_TRIM_US,
        "online_5us_crosscheck": {
            "passed": True,
            "condition_count": len(selected),
            "qualification_mismatch_count": 0,
            "maximum_latency_absolute_error_ms": max(crosscheck_errors_ms, default=0.0),
        },
        "summary": summary,
        "conditions": rows,
    }
    data = args.output / "data"
    png = args.output / "png"
    data.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)
    result_path = data / "trim_sensitivity.json"
    _write_json(result_path, document)
    with (data / "trim_sensitivity_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        fieldnames = (
            "post_select_discard_us",
            "frequency_count",
            "phase_10deg_frequency_pass_count",
            "phase_10deg_frequency_pass_percent",
            "median_samples_per_dwell",
            "latency_median_ms",
            "latency_p95_ms",
            "latency_maximum_ms",
            "single_cycle_power_weighted_phase_rms_p95_deg",
        )
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for item in summary:
            latency = item["first_qualified_wall_latency_ms"]
            writer.writerow(
                {
                    **{key: item[key] for key in fieldnames[:5]},
                    "latency_median_ms": latency["median"],
                    "latency_p95_ms": latency["p95"],
                    "latency_maximum_ms": latency["maximum"],
                    "single_cycle_power_weighted_phase_rms_p95_deg": item[
                        "single_cycle_power_weighted_phase_rms_p95_deg"
                    ],
                }
            )
    _figure(rows, summary, png / "fig10_tx1_post_select_trim_sensitivity.png")
    print(f"trim_analysis={result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
