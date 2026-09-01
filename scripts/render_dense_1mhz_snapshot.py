#!/usr/bin/env python3
"""Render a provenance-checked snapshot of the dense 1 MHz scan."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

STATES = ("ALL_OFF", *(f"ANT{index}" for index in range(1, 9)))
MODEL_STATES = tuple(f"ANT{index}" for index in range(1, 8))
START_HZ = 2_100_000_000
STOP_HZ = 5_800_000_000
STEP_HZ = 1_000_000


class SnapshotError(ValueError):
    """The completed-shard prefix is not admissible."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--evidence-json",
        type=Path,
        help="write machine-readable evidence here instead of OUTPUT_DIR/snapshot.json",
    )
    parser.add_argument(
        "--max-shard",
        type=int,
        help="freeze at this validated shard instead of the latest available shard",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _finite(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"{label} is not numeric") from error
    if not math.isfinite(result):
        raise SnapshotError(f"{label} is not finite")
    return result


def _load_manifest(path: Path, max_shard: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                break
            raise SnapshotError(f"manifest line {index + 1} is invalid") from error
        if not isinstance(value, dict):
            raise SnapshotError(f"manifest line {index + 1} is not an object")
        shard = value.get("shard")
        if not isinstance(shard, int):
            raise SnapshotError(f"manifest line {index + 1} has no integer shard")
        if max_shard is None or shard <= max_shard:
            rows.append(value)
    if not rows:
        raise SnapshotError("manifest has no completed shards")
    expected = list(range(1, rows[-1]["shard"] + 1))
    if [row["shard"] for row in rows] != expected:
        raise SnapshotError("manifest is not one exact contiguous shard prefix")
    if max_shard is not None and rows[-1]["shard"] != max_shard:
        raise SnapshotError(f"requested shard {max_shard} is not complete")
    return rows


def _validation(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("validation", row)
    if not isinstance(value, Mapping):
        raise SnapshotError(f"shard {row.get('shard')} validation is malformed")
    return value


def _load_transfer(
    manifest: Sequence[Mapping[str, Any]],
) -> tuple[np.ndarray, dict[tuple[int, str], complex], list[dict[str, Any]]]:
    transfer: dict[tuple[int, str], complex] = {}
    provenance: list[dict[str, Any]] = []
    frequencies: list[int] = []
    for manifest_row in manifest:
        shard = int(manifest_row["shard"])
        validation = _validation(manifest_row)
        if (
            validation.get("final_safety_passed") is not True
            or validation.get("analysis_error_count") != 0
        ):
            raise SnapshotError(f"shard {shard} did not pass stored validation")
        run_path = Path(str(validation.get("run_json", ""))).resolve()
        if run_path.name != "run.json" or not run_path.is_file() or run_path.is_symlink():
            raise SnapshotError(f"shard {shard} run.json is not a regular file")
        expected_sha = validation.get("run_json_sha256")
        actual_sha = _sha256(run_path)
        if actual_sha != expected_sha:
            raise SnapshotError(f"shard {shard} run.json SHA-256 differs")
        run = json.loads(run_path.read_text(encoding="utf-8"))
        if not isinstance(run, Mapping) or run.get("error") is not None:
            raise SnapshotError(f"shard {shard} run is failed or malformed")
        observations = run.get("observations")
        if not isinstance(observations, list):
            raise SnapshotError(f"shard {shard} observations are missing")
        shard_frequencies = run.get("configuration", {}).get("frequencies_hz")
        if not isinstance(shard_frequencies, list):
            raise SnapshotError(f"shard {shard} frequency configuration is missing")
        expected_order = [
            (int(frequency), state) for frequency in shard_frequencies for state in STATES
        ]
        actual_order = [
            (row.get("frequency_hz"), row.get("state"))
            for row in observations
            if isinstance(row, Mapping)
        ]
        if actual_order != expected_order:
            raise SnapshotError(f"shard {shard} observation lattice/order differs")
        for index, observation in enumerate(observations):
            if (
                not isinstance(observation, Mapping)
                or observation.get("analysis_error") is not None
            ):
                raise SnapshotError(f"shard {shard} observation {index} failed")
            analysis = observation.get("analysis")
            value = analysis.get("transfer_rx2_over_rx1") if isinstance(analysis, Mapping) else None
            if not isinstance(value, Mapping):
                raise SnapshotError(f"shard {shard} observation {index} has no transfer")
            phasor = complex(
                _finite(value.get("real"), "transfer real"),
                _finite(value.get("imag"), "transfer imag"),
            )
            key = (int(observation["frequency_hz"]), str(observation["state"]))
            if key in transfer or abs(phasor) <= np.finfo(float).tiny:
                raise SnapshotError(f"shard {shard} observation {index} is duplicate or zero")
            transfer[key] = phasor
        frequencies.extend(int(value) for value in shard_frequencies)
        provenance.append(
            {
                "shard": shard,
                "run_id": validation.get("run_id"),
                "run_json": str(run_path),
                "run_json_sha256": actual_sha,
            }
        )
    frequency_array = np.asarray(frequencies, dtype=np.int64)
    expected = np.arange(START_HZ, START_HZ + STEP_HZ * frequency_array.size, STEP_HZ)
    if not np.array_equal(frequency_array, expected):
        raise SnapshotError("combined frequency lattice is not the exact 1 MHz prefix")
    return frequency_array, transfer, provenance


def _path_ratio(
    frequencies: np.ndarray, transfer: Mapping[tuple[int, str], complex], state: str
) -> np.ndarray:
    values: list[complex] = []
    for raw_frequency in frequencies:
        frequency = int(raw_frequency)
        background = transfer[(frequency, "ALL_OFF")]
        path = transfer[(frequency, state)] - background
        reference = transfer[(frequency, "ANT8")] - background
        if abs(path) <= 1e-9 or abs(reference) <= 1e-9:
            raise SnapshotError(f"{frequency} {state} derived path is too small")
        values.append(path / reference)
    return np.asarray(values, dtype=np.complex128)


def _design(delta_ghz: np.ndarray, harmonics: int, ripple_delay_ns: float) -> np.ndarray:
    count = delta_ghz.size
    design = np.zeros((2 * count, 3 + 2 * harmonics), dtype=np.float64)
    design[:count, 0] = 1.0
    design[count:, 1] = 1.0
    design[count:, 2] = -2.0 * np.pi * delta_ghz
    for harmonic in range(1, harmonics + 1):
        angle = 2.0 * np.pi * harmonic * delta_ghz * ripple_delay_ns
        cosine = np.cos(angle)
        sine = np.sin(angle)
        column = 3 + 2 * (harmonic - 1)
        design[:count, column] = cosine
        design[:count, column + 1] = sine
        design[count:, column] = -sine
        design[count:, column + 1] = cosine
    return design


def _fit_model(frequencies: np.ndarray, values: np.ndarray, harmonics: int) -> dict[str, Any]:
    frequency_ghz = frequencies.astype(np.float64) / 1e9
    center_ghz = float(np.mean(frequency_ghz))
    delta_ghz = frequency_ghz - center_ghz
    measured_log = np.log(np.abs(values)) + 1j * np.unwrap(np.angle(values))

    if harmonics == 0:
        delay_candidates = np.asarray([0.0])
        search_indices = np.arange(values.size)
    else:
        delay_candidates = np.arange(0.05, 2.5001, 0.01)
        search_indices = np.arange(0, values.size, max(1, values.size // 200))
    search_delta = delta_ghz[search_indices]
    search_log = measured_log[search_indices]
    search_observation = np.concatenate((search_log.real, search_log.imag))
    best_delay = 0.0
    best_score = math.inf
    for delay_ns in delay_candidates:
        design = _design(search_delta, harmonics, float(delay_ns))
        parameters, *_ = np.linalg.lstsq(design, search_observation, rcond=None)
        residual = search_observation - design @ parameters
        score = float(np.mean(np.square(residual)))
        if score < best_score:
            best_score = score
            best_delay = float(delay_ns)
    if harmonics:
        lower = max(0.05, best_delay - 0.012)
        upper = min(2.5, best_delay + 0.012)
        for delay_ns in np.arange(lower, upper + 0.0001, 0.001):
            design = _design(search_delta, harmonics, float(delay_ns))
            parameters, *_ = np.linalg.lstsq(design, search_observation, rcond=None)
            residual = search_observation - design @ parameters
            score = float(np.mean(np.square(residual)))
            if score < best_score:
                best_score = score
                best_delay = float(delay_ns)

    full_design = _design(delta_ghz, harmonics, best_delay)
    observation = np.concatenate((measured_log.real, measured_log.imag))
    parameters, *_ = np.linalg.lstsq(full_design, observation, rcond=None)
    fitted = full_design @ parameters
    predicted_log = fitted[: values.size] + 1j * fitted[values.size :]
    phase_residual_deg = np.degrees(np.angle(values * np.exp(-1j * predicted_log.imag)))
    gain_residual_db = 20.0 / np.log(10.0) * (measured_log.real - predicted_log.real)
    return {
        "harmonics": harmonics,
        "path_delay_ns": float(parameters[2]),
        "ripple_delay_ns": best_delay if harmonics else None,
        "ripple_period_ghz": 1.0 / best_delay if harmonics else None,
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(phase_residual_deg)))),
        "phase_abs_p95_deg": float(np.percentile(np.abs(phase_residual_deg), 95)),
        "phase_abs_max_deg": float(np.max(np.abs(phase_residual_deg))),
        "gain_rms_db": float(np.sqrt(np.mean(np.square(gain_residual_db)))),
        "gain_abs_p95_db": float(np.percentile(np.abs(gain_residual_db), 95)),
        "gain_abs_max_db": float(np.max(np.abs(gain_residual_db))),
        "predicted_log": predicted_log,
        "phase_residual_deg": phase_residual_deg,
        "gain_residual_db": gain_residual_db,
    }


def _dual_design(delta_ghz: np.ndarray, slow_delay_ns: float, fast_delay_ns: float) -> np.ndarray:
    count = delta_ghz.size
    design = np.zeros((2 * count, 11), dtype=np.float64)
    design[:count, 0] = 1.0
    design[count:, 1] = 1.0
    design[count:, 2] = -2.0 * np.pi * delta_ghz
    column = 3
    for delay_ns in (slow_delay_ns, fast_delay_ns):
        for harmonic in (1, 2):
            angle = 2.0 * np.pi * harmonic * delta_ghz * delay_ns
            cosine = np.cos(angle)
            sine = np.sin(angle)
            design[:count, column] = cosine
            design[:count, column + 1] = sine
            design[count:, column] = -sine
            design[count:, column + 1] = cosine
            column += 2
    return design


def _fit_dual_model(
    frequencies: np.ndarray, values: np.ndarray, initial_slow_delay_ns: float
) -> dict[str, Any]:
    frequency_ghz = frequencies.astype(np.float64) / 1e9
    delta_ghz = frequency_ghz - np.mean(frequency_ghz)
    measured_log = np.log(np.abs(values)) + 1j * np.unwrap(np.angle(values))
    search_indices = np.arange(0, values.size, max(1, values.size // 300))
    search_delta = delta_ghz[search_indices]
    search_log = measured_log[search_indices]
    search_observation = np.concatenate((search_log.real, search_log.imag))

    def score(slow_delay_ns: float, fast_delay_ns: float) -> float:
        design = _dual_design(search_delta, slow_delay_ns, fast_delay_ns)
        parameters, *_ = np.linalg.lstsq(design, search_observation, rcond=None)
        return float(np.mean(np.square(search_observation - design @ parameters)))

    slow_delay_ns = initial_slow_delay_ns
    fast_candidates = np.arange(2.5, 15.0001, 0.01)
    fast_delay_ns = float(
        fast_candidates[np.argmin([score(slow_delay_ns, value) for value in fast_candidates])]
    )
    for _ in range(2):
        slow_candidates = np.arange(
            max(0.05, slow_delay_ns - 0.1),
            min(2.5, slow_delay_ns + 0.1) + 0.0001,
            0.002,
        )
        slow_delay_ns = float(
            slow_candidates[np.argmin([score(value, fast_delay_ns) for value in slow_candidates])]
        )
        fast_candidates = np.arange(
            max(2.5, fast_delay_ns - 0.1),
            min(15.0, fast_delay_ns + 0.1) + 0.0001,
            0.002,
        )
        fast_delay_ns = float(
            fast_candidates[np.argmin([score(slow_delay_ns, value) for value in fast_candidates])]
        )

    design = _dual_design(delta_ghz, slow_delay_ns, fast_delay_ns)
    observation = np.concatenate((measured_log.real, measured_log.imag))
    parameters, *_ = np.linalg.lstsq(design, observation, rcond=None)
    fitted = design @ parameters
    predicted_log = fitted[: values.size] + 1j * fitted[values.size :]
    phase_residual_deg = np.degrees(np.angle(values * np.exp(-1j * predicted_log.imag)))
    gain_residual_db = 20.0 / np.log(10.0) * (measured_log.real - predicted_log.real)
    return {
        "harmonics_per_ripple": 2,
        "path_delay_ns": float(parameters[2]),
        "slow_ripple_delay_ns": slow_delay_ns,
        "slow_ripple_period_ghz": 1.0 / slow_delay_ns,
        "fast_ripple_delay_ns": fast_delay_ns,
        "fast_ripple_period_ghz": 1.0 / fast_delay_ns,
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(phase_residual_deg)))),
        "phase_abs_p95_deg": float(np.percentile(np.abs(phase_residual_deg), 95)),
        "phase_abs_max_deg": float(np.max(np.abs(phase_residual_deg))),
        "gain_rms_db": float(np.sqrt(np.mean(np.square(gain_residual_db)))),
        "gain_abs_p95_db": float(np.percentile(np.abs(gain_residual_db), 95)),
        "gain_abs_max_db": float(np.max(np.abs(gain_residual_db))),
        "predicted_log": predicted_log,
        "phase_residual_deg": phase_residual_deg,
        "gain_residual_db": gain_residual_db,
    }


def _interpolation_evaluations(
    frequencies: np.ndarray, ratios: Mapping[str, np.ndarray]
) -> dict[int, dict[str, dict[str, Any]]]:
    frequency_ghz = frequencies.astype(np.float64) / 1e9
    result: dict[int, dict[str, dict[str, Any]]] = {}
    for spacing_mhz in (5, 10, 25, 50):
        training = np.unique(
            np.concatenate(
                (
                    np.arange(0, frequencies.size, spacing_mhz),
                    np.asarray([frequencies.size - 1]),
                )
            )
        )
        evaluation = np.ones(frequencies.size, dtype=bool)
        evaluation[training] = False
        result[spacing_mhz] = {}
        for state in MODEL_STATES:
            values = ratios[state]
            measured_log = np.log(np.abs(values)) + 1j * np.unwrap(np.angle(values))
            predicted_log = np.interp(
                frequency_ghz,
                frequency_ghz[training],
                measured_log.real[training],
            ) + 1j * np.interp(
                frequency_ghz,
                frequency_ghz[training],
                measured_log.imag[training],
            )
            phase_residual_deg = np.full(frequencies.size, np.nan)
            gain_residual_db = np.full(frequencies.size, np.nan)
            phase_residual_deg[evaluation] = np.degrees(
                np.angle(values[evaluation] * np.exp(-1j * predicted_log.imag[evaluation]))
            )
            gain_residual_db[evaluation] = (
                20.0
                / np.log(10.0)
                * (measured_log.real[evaluation] - predicted_log.real[evaluation])
            )
            result[spacing_mhz][state] = {
                "training_knot_count": int(training.size),
                "evaluation_count": int(np.count_nonzero(evaluation)),
                "phase_rms_deg": float(np.sqrt(np.nanmean(np.square(phase_residual_deg)))),
                "phase_abs_p95_deg": float(np.nanpercentile(np.abs(phase_residual_deg), 95)),
                "phase_abs_max_deg": float(np.nanmax(np.abs(phase_residual_deg))),
                "gain_rms_db": float(np.sqrt(np.nanmean(np.square(gain_residual_db)))),
                "gain_abs_p95_db": float(np.nanpercentile(np.abs(gain_residual_db), 95)),
                "gain_abs_max_db": float(np.nanmax(np.abs(gain_residual_db))),
                "phase_residual_deg": phase_residual_deg,
                "gain_residual_db": gain_residual_db,
            }
    return result


def _render(
    frequencies: np.ndarray,
    ratios: Mapping[str, np.ndarray],
    models: Mapping[str, Mapping[str, Mapping[str, Any]]],
    interpolation: Mapping[int, Mapping[str, Mapping[str, Any]]],
    output_dir: Path,
    cutoff_shard: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frequency_ghz = frequencies / 1e9
    complete = (
        int(frequencies[0]) == START_HZ
        and int(frequencies[-1]) == STOP_HZ
        and frequencies.size == (STOP_HZ - START_HZ) // STEP_HZ + 1
    )
    campaign_label = "Complete" if complete else "Partial"
    plt.rcParams.update({"font.size": 8, "figure.dpi": 120, "savefig.dpi": 180})
    figure, axes = plt.subplots(7, 2, figsize=(15, 20), sharex=True)
    for row_index, state in enumerate(MODEL_STATES):
        values = ratios[state]
        measured_phase = np.degrees(np.unwrap(np.angle(values)))
        measured_gain = 20.0 * np.log10(np.abs(values))
        delay = models[state]["delay"]
        ripple = models[state]["ripple"]
        dual = models[state]["dual_ripple"]
        for axis, measured, component in (
            (axes[row_index, 0], measured_phase, "imag"),
            (axes[row_index, 1], measured_gain, "real"),
        ):
            axis.plot(frequency_ghz, measured, color="#4C78A8", linewidth=0.75, label="measured")
            if component == "imag":
                delay_value = np.degrees(delay["predicted_log"].imag)
                ripple_value = np.degrees(ripple["predicted_log"].imag)
                dual_value = np.degrees(dual["predicted_log"].imag)
            else:
                scale = 20.0 / np.log(10.0)
                delay_value = scale * delay["predicted_log"].real
                ripple_value = scale * ripple["predicted_log"].real
                dual_value = scale * dual["predicted_log"].real
            axis.plot(
                frequency_ghz,
                delay_value,
                color="#6B7280",
                linestyle="--",
                linewidth=1.0,
                label="delay only",
            )
            axis.plot(
                frequency_ghz,
                ripple_value,
                color="#F58518",
                linewidth=1.0,
                label="delay + 2-harmonic ripple",
            )
            axis.plot(
                frequency_ghz,
                dual_value,
                color="#54A24B",
                linewidth=1.0,
                label="delay + dual ripple",
            )
            axis.grid(alpha=0.22)
        axes[row_index, 0].set_ylabel("Phase (deg)")
        axes[row_index, 1].set_ylabel("Gain (dB)")
        axes[row_index, 0].set_title(f"{state} vs ANT8 — unwrapped relative phase")
        axes[row_index, 1].set_title(f"{state} vs ANT8 — relative path gain")
    for axis in axes[-1, :]:
        axis.set_xlabel("RF frequency (GHz)")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    figure.legend(
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.966), ncol=4, frameon=False
    )
    figure.suptitle(
        f"{campaign_label} 1 MHz scan: measured antenna paths and fitted models\n"
        f"validated shards 1–{cutoff_shard}; {frequencies.size:,} frequencies; "
        f"{frequency_ghz[0]:.3f}–{frequency_ghz[-1]:.3f} GHz",
        fontsize=14,
        y=0.995,
    )
    figure.subplots_adjust(
        top=0.945, bottom=0.045, left=0.065, right=0.985, hspace=0.34, wspace=0.17
    )
    figure.savefig(output_dir / "fig01_antenna_comparison.png")
    plt.close(figure)

    figure, axes = plt.subplots(7, 2, figsize=(15, 20), sharex=True)
    for row_index, state in enumerate(MODEL_STATES):
        delay = models[state]["delay"]
        ripple = models[state]["ripple"]
        dual = models[state]["dual_ripple"]
        for axis, key, ylabel, unit in (
            (axes[row_index, 0], "phase_residual_deg", "Phase residual (deg)", "°"),
            (axes[row_index, 1], "gain_residual_db", "Gain residual (dB)", " dB"),
        ):
            rms_key = "phase_rms_deg" if key.startswith("phase") else "gain_rms_db"
            axis.axhline(0.0, color="#111827", linewidth=0.6)
            axis.plot(
                frequency_ghz,
                delay[key],
                color="#9CA3AF",
                linewidth=0.7,
                label=f"delay only (RMS {delay[rms_key]:.2f}{unit})",
            )
            axis.plot(
                frequency_ghz,
                ripple[key],
                color="#F58518",
                linewidth=0.8,
                label=f"single ripple (RMS {ripple[rms_key]:.2f}{unit})",
            )
            axis.plot(
                frequency_ghz,
                dual[key],
                color="#4C78A8",
                linewidth=0.8,
                label=f"dual ripple (RMS {dual[rms_key]:.2f}{unit})",
            )
            axis.set_ylabel(ylabel)
            axis.grid(alpha=0.22)
            axis.legend(loc="upper right", fontsize=7, frameon=False)
        axes[row_index, 0].set_title(f"{state} vs ANT8 — wrapped phase residual")
        axes[row_index, 1].set_title(f"{state} vs ANT8 — gain residual")
    for axis in axes[-1, :]:
        axis.set_xlabel("RF frequency (GHz)")
    figure.suptitle(
        f"{campaign_label} 1 MHz scan: residuals around fitted models\n"
        f"validated shards 1–{cutoff_shard}; {frequencies.size:,} frequencies; "
        f"{frequency_ghz[0]:.3f}–{frequency_ghz[-1]:.3f} GHz",
        fontsize=14,
        y=0.995,
    )
    figure.subplots_adjust(
        top=0.945, bottom=0.045, left=0.065, right=0.985, hspace=0.34, wspace=0.17
    )
    figure.savefig(output_dir / "fig02_model_residuals.png")
    plt.close(figure)

    figure, axes = plt.subplots(3, 1, figsize=(13, 13))
    x_positions = np.arange(len(MODEL_STATES))
    width = 0.24
    model_specs = (
        ("delay", "Delay only", "#9CA3AF"),
        ("ripple", "Single ripple", "#F58518"),
        ("dual_ripple", "Dual ripple", "#4C78A8"),
    )
    for offset, (model_name, label, color) in enumerate(model_specs):
        displacement = (offset - 1) * width
        axes[0].bar(
            x_positions + displacement,
            [models[state][model_name]["phase_rms_deg"] for state in MODEL_STATES],
            width,
            label=label,
            color=color,
        )
        axes[1].bar(
            x_positions + displacement,
            [models[state][model_name]["gain_rms_db"] for state in MODEL_STATES],
            width,
            label=label,
            color=color,
        )
    for axis, ylabel in ((axes[0], "Phase RMS (deg)"), (axes[1], "Gain RMS (dB)")):
        axis.set_xticks(x_positions, MODEL_STATES)
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", alpha=0.22)
        axis.legend(frameon=False, ncol=3)
    spacings = np.asarray(sorted(interpolation))
    phase_means = [
        np.mean([interpolation[value][state]["phase_rms_deg"] for state in MODEL_STATES])
        for value in spacings
    ]
    gain_means = [
        np.mean([interpolation[value][state]["gain_rms_db"] for state in MODEL_STATES])
        for value in spacings
    ]
    axes[2].plot(spacings, phase_means, marker="o", color="#4C78A8")
    axes[2].set_xlabel("Calibration LUT knot spacing (MHz)")
    axes[2].set_ylabel("Held-out phase RMS (deg)", color="#4C78A8")
    axes[2].tick_params(axis="y", labelcolor="#4C78A8")
    axes[2].set_xticks(spacings)
    axes[2].grid(alpha=0.22)
    gain_axis = axes[2].twinx()
    gain_axis.plot(spacings, gain_means, marker="s", color="#E45756")
    gain_axis.set_ylabel("Held-out gain RMS (dB)", color="#E45756")
    gain_axis.tick_params(axis="y", labelcolor="#E45756")
    figure.suptitle(
        f"{campaign_label} 1 MHz scan: model and interpolation error comparison\n"
        f"validated shards 1–{cutoff_shard}; {frequencies.size:,} frequencies; "
        "LUT errors exclude their training knots",
        fontsize=14,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.965))
    figure.savefig(output_dir / "fig03_model_comparison.png")
    plt.close(figure)

    figure, axes = plt.subplots(7, 2, figsize=(15, 20), sharex=True)
    for row_index, state in enumerate(MODEL_STATES):
        evaluation = interpolation[10][state]
        for axis, key, ylabel, unit in (
            (axes[row_index, 0], "phase_residual_deg", "Phase residual (deg)", "°"),
            (axes[row_index, 1], "gain_residual_db", "Gain residual (dB)", " dB"),
        ):
            rms_key = "phase_rms_deg" if key.startswith("phase") else "gain_rms_db"
            axis.axhline(0.0, color="#111827", linewidth=0.6)
            axis.plot(frequency_ghz, evaluation[key], color="#4C78A8", linewidth=0.75)
            axis.set_ylabel(ylabel)
            axis.grid(alpha=0.22)
            axis.set_title(
                f"{state} vs ANT8 — 10 MHz LUT held-out residual "
                f"(RMS {evaluation[rms_key]:.2f}{unit})"
            )
    for axis in axes[-1, :]:
        axis.set_xlabel("RF frequency (GHz)")
    figure.suptitle(
        f"{campaign_label} 1 MHz scan: linear complex-log interpolation from 10 MHz knots\n"
        f"validated shards 1–{cutoff_shard}; training knots omitted from residual traces",
        fontsize=14,
        y=0.995,
    )
    figure.subplots_adjust(
        top=0.945, bottom=0.045, left=0.065, right=0.985, hspace=0.42, wspace=0.17
    )
    figure.savefig(output_dir / "fig04_10mhz_lut_residuals.png")
    plt.close(figure)


def main() -> int:
    args = _parser().parse_args()
    campaign = args.campaign.resolve()
    manifest = _load_manifest(campaign / "shards.ndjson", args.max_shard)
    frequencies, transfer, provenance = _load_transfer(manifest)
    complete = (
        int(frequencies[0]) == START_HZ
        and int(frequencies[-1]) == STOP_HZ
        and frequencies.size == (STOP_HZ - START_HZ) // STEP_HZ + 1
    )
    ratios = {state: _path_ratio(frequencies, transfer, state) for state in MODEL_STATES}
    models: dict[str, dict[str, dict[str, Any]]] = {}
    for state in MODEL_STATES:
        ripple = _fit_model(frequencies, ratios[state], 2)
        models[state] = {
            "delay": _fit_model(frequencies, ratios[state], 0),
            "ripple": ripple,
            "dual_ripple": _fit_dual_model(frequencies, ratios[state], ripple["ripple_delay_ns"]),
        }
    interpolation = _interpolation_evaluations(frequencies, ratios)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _render(
        frequencies,
        ratios,
        models,
        interpolation,
        args.output_dir,
        manifest[-1]["shard"],
    )
    serializable_models = {
        state: {
            model_name: {
                key: value for key, value in model.items() if not isinstance(value, np.ndarray)
            }
            for model_name, model in state_models.items()
        }
        for state, state_models in models.items()
    }
    serializable_interpolation = {
        str(spacing): {
            state: {
                key: value for key, value in evaluation.items() if not isinstance(value, np.ndarray)
            }
            for state, evaluation in state_evaluations.items()
        }
        for spacing, state_evaluations in interpolation.items()
    }
    metric_names = (
        "phase_rms_deg",
        "phase_abs_p95_deg",
        "phase_abs_max_deg",
        "gain_rms_db",
        "gain_abs_p95_db",
        "gain_abs_max_db",
    )
    aggregate_models = {
        model_name: {
            f"mean_path_{metric}": float(
                np.mean([models[state][model_name][metric] for state in MODEL_STATES])
            )
            for metric in metric_names
        }
        for model_name in ("delay", "ripple", "dual_ripple")
    }
    aggregate_interpolation = {
        str(spacing): {
            f"mean_path_{metric}": float(
                np.mean([interpolation[spacing][state][metric] for state in MODEL_STATES])
            )
            for metric in metric_names
        }
        for spacing in sorted(interpolation)
    }
    validation_rows = [_validation(row) for row in manifest]
    raw_iq_files = [
        path for item in provenance for path in Path(item["run_json"]).parent.glob("*.npz")
    ]
    plan_path = campaign / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    evidence = {
        "schema": (
            "smateway.dense-1mhz-campaign/v1"
            if complete
            else "smateway.dense-1mhz-partial-snapshot/v1"
        ),
        "provisional": not complete,
        "campaign": str(campaign),
        "cutoff_shard": manifest[-1]["shard"],
        "frequency_count": int(frequencies.size),
        "first_hz": int(frequencies[0]),
        "last_hz": int(frequencies[-1]),
        "step_hz": STEP_HZ,
        "capture_count": int(frequencies.size * len(STATES)),
        "path_ratio_definition": "(H_state - H_ALL_OFF) / (H_ANT8 - H_ALL_OFF)",
        "validation": {
            "shard_count": len(manifest),
            "manifest_sha256": _sha256(campaign / "shards.ndjson"),
            "plan_sha256": _sha256(plan_path) if plan_path.is_file() else None,
            "raw_iq_file_count": len(raw_iq_files),
            "raw_iq_bytes": sum(path.stat().st_size for path in raw_iq_files),
            "analysis_error_count": int(
                sum(int(row["analysis_error_count"]) for row in validation_rows)
            ),
            "maximum_peak_component_counts": float(
                max(float(row["maximum_peak_component_counts"]) for row in validation_rows)
            ),
            "all_final_safety_passed": all(
                row.get("final_safety_passed") is True for row in validation_rows
            ),
            "excluded_attempts": plan.get("excluded_attempts", []),
        },
        "aggregate_models": aggregate_models,
        "aggregate_linear_complex_log_interpolation": aggregate_interpolation,
        "models": serializable_models,
        "linear_complex_log_interpolation": serializable_interpolation,
        "run_provenance": provenance,
    }
    evidence_path = args.evidence_json or (args.output_dir / "snapshot.json")
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: evidence[key]
                for key in (
                    "cutoff_shard",
                    "frequency_count",
                    "first_hz",
                    "last_hz",
                    "capture_count",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
