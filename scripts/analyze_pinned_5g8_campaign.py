#!/usr/bin/env python3
"""Validate and summarize the pinned external-source 5.8 GHz campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

FREQUENCIES_HZ = (
    5_725_000_000,
    5_750_000_000,
    5_775_000_000,
    5_800_000_000,
    5_825_000_000,
    5_850_000_000,
    5_875_000_000,
)
STATES = ("ALL_OFF", *(f"ANT{i}" for i in range(1, 9)))
SELECTED_STATES = STATES[1:]
STATE_CODES = {
    "ALL_OFF": 8,
    "ANT1": 0,
    "ANT2": 4,
    "ANT3": 2,
    "ANT4": 6,
    "ANT5": 1,
    "ANT6": 5,
    "ANT7": 3,
    "ANT8": 7,
}
POWER_GAINS_DB = (-55.0, -50.0, -45.0, -40.0, -35.0)
RADIO_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
STLINK_SERIAL = "002D003A3335511035383531"


class CampaignError(ValueError):
    """A capture set cannot support the pinned campaign result."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--muted-run", type=Path, required=True)
    parser.add_argument("--band-run", type=Path, required=True)
    parser.add_argument("--reverse-run", type=Path, required=True)
    parser.add_argument("--power-run", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--figure-dir", type=Path, required=True)
    return parser


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def wrap_phase_deg(value: float) -> float:
    wrapped = (value + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(wrapped, -180.0, abs_tol=1e-12) else wrapped


def _mean_complex(values: Sequence[complex]) -> complex:
    if not values:
        raise CampaignError("cannot aggregate an empty phasor cohort")
    return complex(np.mean(np.asarray(values, dtype=np.complex128)))


def aggregate_transfer(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    transfers = [
        row["transfer"]
        if isinstance(row.get("transfer"), Mapping)
        else row["analysis"]["transfer_rx2_over_rx1"]
        for row in rows
    ]
    phasors = [complex(float(value["real"]), float(value["imag"])) for value in transfers]
    magnitudes = np.asarray([abs(value) for value in phasors], dtype=np.float64)
    phases = np.asarray([math.degrees(math.atan2(value.imag, value.real)) for value in phasors])
    mean_phasor = _mean_complex(phasors)
    mean_phase = math.degrees(math.atan2(mean_phasor.imag, mean_phasor.real))
    phase_offsets = np.asarray([wrap_phase_deg(value - mean_phase) for value in phases])
    magnitude_db = 20.0 * np.log10(magnitudes)
    return {
        "real": mean_phasor.real,
        "imag": mean_phasor.imag,
        "coherent_magnitude": abs(mean_phasor),
        "coherent_magnitude_db": 20.0 * math.log10(abs(mean_phasor)),
        "mean_magnitude": float(np.mean(magnitudes)),
        "mean_magnitude_db": 20.0 * math.log10(float(np.mean(magnitudes))),
        "phase_deg": mean_phase,
        "magnitude_span_db": float(np.ptp(magnitude_db)),
        "phase_span_deg": float(np.ptp(phase_offsets)),
    }


def calibration_coefficient(
    reference: Mapping[str, float], path: Mapping[str, float]
) -> dict[str, float]:
    reference_phasor = complex(reference["real"], reference["imag"])
    path_phasor = complex(path["real"], path["imag"])
    if abs(path_phasor) <= np.finfo(float).tiny:
        raise CampaignError("cannot calibrate a zero path phasor")
    coefficient = reference_phasor / path_phasor
    return {
        "real": coefficient.real,
        "imag": coefficient.imag,
        "magnitude": abs(coefficient),
        "gain_db": 20.0 * math.log10(abs(coefficient)),
        "phase_deg": math.degrees(math.atan2(coefficient.imag, coefficient.real)),
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot load {path}: {error}") from error
    if not isinstance(value, dict):
        raise CampaignError(f"{path} is not a JSON object")
    return value


def _require_mute(value: object, label: str) -> None:
    if not isinstance(value, Mapping) or value.get("passed") is not True:
        raise CampaignError(f"{label} did not pass exact mute readback")
    if value.get("tx_gain_db") != [-80.0, -80.0] or value.get("dds_scales") != [0.0] * 8:
        raise CampaignError(f"{label} does not contain the exact muted state")


def _validate_run(path: Path, role: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = _load(path)
    if run.get("schema") != 1 or run.get("error") is not None:
        raise CampaignError(f"{role} run is incomplete or failed")
    if run.get("radio_serial") != RADIO_SERIAL or run.get("board_id") != BOARD_ID:
        raise CampaignError(f"{role} run receiver/board identity differs")
    if run.get("stlink_serial") != STLINK_SERIAL:
        raise CampaignError(f"{role} run ST-Link identity differs")
    mode = run.get("mode")
    if mode not in {"muted", "external"}:
        raise CampaignError(f"{role} run mode is unsupported")
    if mode == "external" and run.get("source_radio_serial") != SOURCE_SERIAL:
        raise CampaignError(f"{role} run source identity differs")
    _require_mute(run.get("final_radio_mute"), f"{role} final receiver")
    if mode == "external":
        _require_mute(run.get("final_source_radio_mute"), f"{role} final source")
    selector = run.get("final_selector")
    if (
        not isinstance(selector, Mapping)
        or selector.get("applied_code") != STATE_CODES["ALL_OFF"]
        or selector.get("lease_active") is not False
    ):
        raise CampaignError(f"{role} run did not end in ALL_OFF")
    observations = run.get("observations")
    if not isinstance(observations, list) or not observations:
        raise CampaignError(f"{role} run contains no observations")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(observations):
        if not isinstance(raw, dict):
            raise CampaignError(f"{role} observation {index} is malformed")
        state = raw.get("state")
        if state not in STATES:
            raise CampaignError(f"{role} observation {index} has an unknown state")
        before = raw.get("selector_before")
        after = raw.get("selector_after")
        if not isinstance(before, Mapping) or before.get("applied_code") != STATE_CODES[state]:
            raise CampaignError(f"{role} observation {index} selector state differs")
        if not isinstance(after, Mapping) or after.get("applied_code") != STATE_CODES["ALL_OFF"]:
            raise CampaignError(f"{role} observation {index} cleanup state differs")
        _require_mute(raw.get("post_capture_mute"), f"{role} observation {index} receiver")
        if mode == "external":
            _require_mute(raw.get("post_capture_source_mute"), f"{role} observation {index} source")
        if raw.get("analysis_error") is not None or not isinstance(raw.get("analysis"), Mapping):
            raise CampaignError(f"{role} observation {index} analysis failed")
        analysis = raw["analysis"]
        if mode == "external" and not isinstance(analysis.get("transfer_rx2_over_rx1"), Mapping):
            raise CampaignError(f"{role} observation {index} transfer is missing")
        iq_name = raw.get("iq_file")
        iq_path = path.parent / str(iq_name)
        if not isinstance(iq_name, str) or not iq_path.is_file():
            raise CampaignError(f"{role} observation {index} raw IQ is missing")
        record: dict[str, Any] = {
            "role": role,
            "run_id": run["run_id"],
            "frequency_hz": int(raw["frequency_hz"]),
            "state": state,
            "repeat": int(raw["repeat"]),
            "tx_gain_db": run["configuration"].get("tx_gain_db"),
            "iq_file": iq_name,
            "iq_size_bytes": iq_path.stat().st_size,
            "iq_sha256": sha256_path(iq_path),
            "peak_component_counts": analysis["peak_component_counts"],
            "rms_counts": analysis["rms_counts"],
        }
        if mode == "external":
            transfer = analysis["transfer_rx2_over_rx1"]
            record.update(
                {
                    "rx1_phasor_magnitude": analysis["rx1_phasor"]["magnitude"],
                    "rx2_phasor_magnitude": analysis["rx2_phasor"]["magnitude"],
                    "transfer": transfer,
                    "pilot": analysis["pilot"],
                }
            )
        normalized.append(record)
    return run, normalized


def _validate_lattice(
    run: Mapping[str, Any],
    *,
    frequencies: Sequence[int],
    states: Sequence[str],
    repeats: int,
    gain_db: float | None,
) -> None:
    configuration = run.get("configuration")
    if not isinstance(configuration, Mapping):
        raise CampaignError("run configuration is missing")
    expected_count = len(frequencies) * len(states) * repeats
    if (
        configuration.get("frequencies_hz") != list(frequencies)
        or configuration.get("states") != list(states)
        or configuration.get("repeats") != repeats
        or configuration.get("tx_gain_db") != gain_db
        or len(run["observations"]) != expected_count
    ):
        raise CampaignError("run does not contain the exact requested campaign lattice")
    actual = {
        (int(row["frequency_hz"]), str(row["state"]), int(row["repeat"]))
        for row in run["observations"]
    }
    expected = {
        (frequency, state, repeat)
        for frequency in frequencies
        for state in states
        for repeat in range(1, repeats + 1)
    }
    if actual != expected:
        raise CampaignError("run campaign lattice contains missing or duplicate cells")


def _cohort(
    rows: Sequence[Mapping[str, Any]], *, frequency_hz: int, state: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["frequency_hz"] == frequency_hz and row["state"] == state]


def _power_cohort(
    rows: Sequence[Mapping[str, Any]], *, gain_db: float, state: str
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if row["tx_gain_db"] == gain_db
        and row["frequency_hz"] == 5_800_000_000
        and row["state"] == state
    ]


def analyze_campaign(
    *,
    muted_path: Path,
    band_path: Path,
    reverse_path: Path,
    power_paths: Sequence[Path],
) -> dict[str, Any]:
    muted_run, muted_rows = _validate_run(muted_path, "muted")
    band_run, band_rows = _validate_run(band_path, "band_ascending")
    reverse_run, reverse_rows = _validate_run(reverse_path, "band_descending")
    power_loaded = [_validate_run(path, f"power_{index}") for index, path in enumerate(power_paths)]
    _validate_lattice(
        muted_run,
        frequencies=FREQUENCIES_HZ,
        states=("ALL_OFF",),
        repeats=3,
        gain_db=None,
    )
    _validate_lattice(band_run, frequencies=FREQUENCIES_HZ, states=STATES, repeats=3, gain_db=-40.0)
    _validate_lattice(
        reverse_run,
        frequencies=tuple(reversed(FREQUENCIES_HZ)),
        states=STATES,
        repeats=1,
        gain_db=-40.0,
    )
    power_by_gain: dict[float, list[dict[str, Any]]] = {-40.0: band_rows}
    power_runs: list[dict[str, Any]] = []
    for run, rows in power_loaded:
        gain = float(run["configuration"]["tx_gain_db"])
        if gain in power_by_gain or gain not in POWER_GAINS_DB:
            raise CampaignError("power runs contain a duplicate or unexpected gain")
        _validate_lattice(run, frequencies=(5_800_000_000,), states=STATES, repeats=3, gain_db=gain)
        power_by_gain[gain] = rows
        power_runs.append(run)
    if set(power_by_gain) != set(POWER_GAINS_DB):
        raise CampaignError("power campaign is incomplete")
    commits = {
        str(run["git_head"])
        for run in (muted_run, band_run, reverse_run, *(run for run, _rows in power_loaded))
    }
    if len(commits) != 1:
        raise CampaignError("campaign runs do not share one source-freeze commit")

    band_cells: list[dict[str, Any]] = []
    calibration: list[dict[str, Any]] = []
    contrasts: list[dict[str, Any]] = []
    for frequency in FREQUENCIES_HZ:
        aggregates = {
            state: aggregate_transfer(_cohort(band_rows, frequency_hz=frequency, state=state))
            for state in STATES
        }
        for state in STATES:
            band_cells.append({"frequency_hz": frequency, "state": state, **aggregates[state]})
        reference = aggregates["ANT8"]
        for state in SELECTED_STATES:
            calibration.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "reference_state": "ANT8",
                    **calibration_coefficient(reference, aggregates[state]),
                }
            )
            contrasts.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "selected_over_all_off_db": aggregates[state]["mean_magnitude_db"]
                    - aggregates["ALL_OFF"]["mean_magnitude_db"],
                }
            )

    direction: list[dict[str, Any]] = []
    for frequency in FREQUENCIES_HZ:
        for state in STATES:
            ascending = aggregate_transfer(_cohort(band_rows, frequency_hz=frequency, state=state))
            descending = aggregate_transfer(
                _cohort(reverse_rows, frequency_hz=frequency, state=state)
            )
            direction.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "magnitude_delta_db": descending["mean_magnitude_db"]
                    - ascending["mean_magnitude_db"],
                    "phase_delta_deg": wrap_phase_deg(
                        descending["phase_deg"] - ascending["phase_deg"]
                    ),
                }
            )

    power_cells: list[dict[str, Any]] = []
    for gain in POWER_GAINS_DB:
        rows = power_by_gain[gain]
        for state in STATES:
            cohort = _power_cohort(rows, gain_db=gain, state=state)
            aggregate = aggregate_transfer(cohort)
            power_cells.append(
                {
                    "tx_gain_db": gain,
                    "state": state,
                    **aggregate,
                    "rx1_phasor_magnitude": float(
                        np.mean([row["rx1_phasor_magnitude"] for row in cohort])
                    ),
                }
            )

    rx1_by_gain = {
        gain: next(
            cell["rx1_phasor_magnitude"]
            for cell in power_cells
            if cell["tx_gain_db"] == gain and cell["state"] == "ANT1"
        )
        for gain in POWER_GAINS_DB
    }
    slope, intercept = np.polyfit(
        np.asarray(POWER_GAINS_DB),
        20.0 * np.log10(np.asarray([rx1_by_gain[gain] for gain in POWER_GAINS_DB])),
        1,
    )
    selected_direction = [row for row in direction if row["state"] != "ALL_OFF"]
    selected_band = [row for row in band_cells if row["state"] != "ALL_OFF"]
    selected_power_spans = []
    for state in SELECTED_STATES:
        values = [cell["mean_magnitude_db"] for cell in power_cells if cell["state"] == state]
        selected_power_spans.append({"state": state, "span_db": float(np.ptp(values))})
    all_rows = [
        *muted_rows,
        *band_rows,
        *reverse_rows,
        *(row for rows in power_by_gain.values() if rows is not band_rows for row in rows),
    ]
    run_documents = [muted_run, band_run, reverse_run, *power_runs]
    run_paths = [muted_path, band_path, reverse_path, *power_paths]
    summary = {
        "capture_count": len(all_rows),
        "raw_iq_bytes": sum(int(row["iq_size_bytes"]) for row in all_rows),
        "analysis_error_count": 0,
        "source_commit": next(iter(commits)),
        "maximum_driven_peak_counts": max(
            max(row["peak_component_counts"]) for row in all_rows if row["role"] != "muted"
        ),
        "maximum_selected_repeat_span_db": max(row["magnitude_span_db"] for row in selected_band),
        "maximum_selected_repeat_phase_span_deg": max(
            row["phase_span_deg"] for row in selected_band
        ),
        "maximum_direction_magnitude_delta_db": max(
            abs(row["magnitude_delta_db"]) for row in selected_direction
        ),
        "maximum_direction_phase_delta_deg": max(
            abs(row["phase_delta_deg"]) for row in selected_direction
        ),
        "minimum_selected_over_all_off_db": min(
            row["selected_over_all_off_db"] for row in contrasts
        ),
        "maximum_selected_over_all_off_db": max(
            row["selected_over_all_off_db"] for row in contrasts
        ),
        "maximum_selected_power_span_db": max(row["span_db"] for row in selected_power_spans),
        "rx1_power_slope_db_per_db": float(slope),
        "rx1_power_fit_intercept_db": float(intercept),
        "final_safety_passed": True,
    }
    return {
        "schema": 1,
        "evidence_kind": "smateway.pinned-external-5g8-campaign/v1",
        "campaign_id": "external-5g8-20260830",
        "fixture": {
            "receiver_serial": RADIO_SERIAL,
            "source_serial": SOURCE_SERIAL,
            "selector_board_id": BOARD_ID,
            "stlink_serial": STLINK_SERIAL,
            "reference_channel": "RX1 behind the 2-way branch and 10 dB attenuator",
            "selected_channel": "RX2 behind 2-way, 8-way, and selector",
        },
        "source_runs": [
            {
                "run_id": run["run_id"],
                "mode": run["mode"],
                "tx_gain_db": run["configuration"].get("tx_gain_db"),
                "run_json_sha256": sha256_path(path),
            }
            for run, path in zip(run_documents, run_paths, strict=True)
        ],
        "summary": summary,
        "muted": muted_rows,
        "band_cells": band_cells,
        "calibration_relative_to_ant8": calibration,
        "selected_over_all_off": contrasts,
        "direction_repeatability": direction,
        "power_cells": power_cells,
        "power_spans": selected_power_spans,
        "raw_observations": all_rows,
    }


def _series(rows: Iterable[Mapping[str, Any]], state: str, field: str) -> list[float]:
    return [float(row[field]) for row in rows if row["state"] == state]


def render_figures(result: Mapping[str, Any], output: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    frequency_mhz = np.asarray(FREQUENCIES_HZ, dtype=np.float64) / 1e6
    band = result["band_cells"]
    calibration = result["calibration_relative_to_ant8"]
    contrast = result["selected_over_all_off"]
    power = result["power_cells"]
    direction = result["direction_repeatability"]
    names: list[str] = []

    figure, axis = plt.subplots(figsize=(9, 5.2), constrained_layout=True)
    axis.plot(
        frequency_mhz,
        _series(band, "ALL_OFF", "mean_magnitude_db"),
        "--o",
        color="black",
        label="ALL_OFF",
    )
    for state in SELECTED_STATES:
        axis.plot(frequency_mhz, _series(band, state, "mean_magnitude_db"), "-o", label=state)
    axis.set(xlabel="Center frequency (MHz)", ylabel="20 log10 |RX2/RX1| (dB)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=3, fontsize=8)
    name = "fig01_band_transfer.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, (gain_axis, phase_axis) = plt.subplots(
        2, 1, figsize=(9, 8), sharex=True, constrained_layout=True
    )
    for state in SELECTED_STATES:
        state_rows = [row for row in calibration if row["state"] == state]
        gain_axis.plot(frequency_mhz, [row["gain_db"] for row in state_rows], "-o", label=state)
        phases = np.rad2deg(np.unwrap(np.deg2rad([row["phase_deg"] for row in state_rows])))
        phase_axis.plot(frequency_mhz, phases, "-o", label=state)
    gain_axis.set(ylabel="Gain correction vs ANT8 (dB)")
    phase_axis.set(xlabel="Center frequency (MHz)", ylabel="Unwrapped phase correction (degrees)")
    gain_axis.grid(True, alpha=0.3)
    phase_axis.grid(True, alpha=0.3)
    gain_axis.legend(ncol=4, fontsize=8)
    name = "fig02_frequency_indexed_calibration.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, axis = plt.subplots(figsize=(9, 5.2), constrained_layout=True)
    for state in SELECTED_STATES:
        axis.plot(
            frequency_mhz,
            _series(contrast, state, "selected_over_all_off_db"),
            "-o",
            label=state,
        )
    axis.axhline(20.0, color="black", linestyle="--", linewidth=1, label="20 dB gate")
    axis.set(xlabel="Center frequency (MHz)", ylabel="Selected / ALL_OFF contrast (dB)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=3, fontsize=8)
    name = "fig03_selected_off_contrast.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, axis = plt.subplots(figsize=(9, 5.2), constrained_layout=True)
    for state in STATES:
        style = "--o" if state == "ALL_OFF" else "-o"
        axis.plot(
            POWER_GAINS_DB,
            _series(power, state, "mean_magnitude_db"),
            style,
            label=state,
        )
    axis.set(xlabel="Source TX1 hardware gain (dB)", ylabel="20 log10 |RX2/RX1| (dB)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=3, fontsize=8)
    name = "fig04_power_linearity.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    selected_direction = [row for row in direction if row["state"] != "ALL_OFF"]
    figure, (magnitude_axis, phase_axis) = plt.subplots(
        2, 1, figsize=(9, 7.5), sharex=True, constrained_layout=True
    )
    for state in SELECTED_STATES:
        magnitude_axis.plot(
            frequency_mhz,
            _series(selected_direction, state, "magnitude_delta_db"),
            "-o",
            label=state,
        )
        phase_axis.plot(
            frequency_mhz,
            _series(selected_direction, state, "phase_delta_deg"),
            "-o",
            label=state,
        )
    magnitude_axis.set(ylabel="Descending − ascending (dB)")
    phase_axis.set(xlabel="Center frequency (MHz)", ylabel="Descending − ascending (degrees)")
    magnitude_axis.grid(True, alpha=0.3)
    phase_axis.grid(True, alpha=0.3)
    magnitude_axis.legend(ncol=4, fontsize=8)
    name = "fig05_sweep_direction_repeatability.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)
    return names


def main() -> int:
    args = _parser().parse_args()
    result = analyze_campaign(
        muted_path=args.muted_run,
        band_path=args.band_run,
        reverse_path=args.reverse_run,
        power_paths=args.power_run,
    )
    figure_names = render_figures(result, args.figure_dir)
    result["figures"] = figure_names
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
