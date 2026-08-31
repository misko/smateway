#!/usr/bin/env python3
"""Compare the exact original-three broadband sweeps with the exact later five."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

FREQUENCIES_HZ = tuple(range(2_100_000_000, 5_800_000_001, 100_000_000))
STATES = ("ALL_OFF", *(f"ANT{i}" for i in range(1, 9)))
MODEL_STATES = tuple(f"ANT{i}" for i in range(1, 8))
ORIGINAL_RUN_SHA256 = {
    "20260830T211358.287767Z": "21c17f36641fbc1793c0c6469b70c4d8307afacbe0b58d8ad4a1549b47866d7b",
    "20260830T212857.254746Z": "1b6b28dce8059e54cbdbb2ec6a47bab6629564a122808ddcc9ce5359365203ea",
    "20260830T214309.897237Z": "b41deb6c9396ef7cf85859e61b5dbec87d42143f9df23f1b5b4bfed092d41e54",
}
FUTURE_RUN_SHA256 = {
    "20260830T233718.194719Z": "b7db1b4bfb9d6f3d3e329fba8a67c672cfc8830b5be0d7c87593402b416ffb10",
    "20260830T235102.223267Z": "8f119dd47b5cdf1d4ad3915d2f3996956aaa3c997576143572f06379b76f50ad",
    "20260831T000448.252130Z": "3c7e920e16618057af2584fe9ae7564d208647a2f13531661060116922294963",
    "20260831T001833.322413Z": "e09855fab94f8973d69346c27667cb5f61fcb8e49985c27791e7b4b27e956988",
    "20260831T003219.696398Z": "86edc271c0ae3208dad1e9576658dd7424c22e7d8fdcd3ce8a54e78a53d9dded",
}
EXCLUDED_INTERVENING_RUN_ID = "20260830T231939.843226Z"
ORIGINAL_SOURCE_COMMIT = "4a163644ab54c804680e2784da1f73dcb1c2167a"
FUTURE_SOURCE_COMMIT = "f87e963791d448b11942d65723392e9a493721cd"
EXPECTED_CONFIGURATION = {
    "bandwidth_hz": 1_600_000,
    "dds_scale": 0.25,
    "frequencies_hz": list(FREQUENCIES_HZ),
    "repeats": 1,
    "rx_gain_db": 60,
    "sample_count": 262_144,
    "sample_rate_hz": 2_000_000,
    "states": list(STATES),
    "tone_offset_hz": 100_000,
    "tx_gain_db": -40.0,
}
EXPECTED_FIXTURE = {
    "radio_uri": "ip:192.168.1.15",
    "radio_serial": "104000b29905000e17000800065934759d",
    "source_radio_uri": "ip:192.168.1.173",
    "source_radio_serial": "104473b80a16000de6ff2000f8a6beca79",
    "board_id": "stm32c011-4c0055000950313950363920",
    "stlink_serial": "002D003A3335511035383531",
}
RIPPLE_DELAY_GRID_NS = np.linspace(0.05, 2.5, 2_451)
FIGURE_FILENAMES = (
    "fig01_acquisition_quality.png",
    "fig02_repeatability_comparison.png",
    "fig03_model_complexity_vs_future_error.png",
    "fig04_future_model_error_by_path.png",
    "fig05_phase_model_overlay.png",
    "fig06_gain_model_overlay.png",
    "fig07_cohort_mean_calibration_shift.png",
)


class ComparisonError(ValueError):
    """The inputs do not describe the exact predeclared comparison."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-run", type=Path, action="append", required=True)
    parser.add_argument("--future-run", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--figure-dir", type=Path, required=True)
    return parser


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda x: (_ for _ in ()).throw(
                ComparisonError(f"non-finite JSON constant {x}")
            ),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ComparisonError(f"cannot load {path}: {error}") from error
    if not isinstance(value, dict):
        raise ComparisonError(f"{path} is not a JSON object")
    return value


def _finite_number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ComparisonError(f"{label} is not numeric") from error
    if not math.isfinite(result):
        raise ComparisonError(f"{label} is non-finite")
    return result


def _validate_final_safety(run: Mapping[str, Any], label: str) -> None:
    for key in ("final_radio_mute", "final_source_radio_mute"):
        value = run.get(key)
        if not isinstance(value, Mapping) or value.get("passed") is not True:
            raise ComparisonError(f"{label} {key} did not pass")
        if value.get("tx_gain_db") != [-80.0, -80.0] or value.get("dds_scales") != [0.0] * 8:
            raise ComparisonError(f"{label} {key} is not the exact muted state")
    selector = run.get("final_selector")
    if not isinstance(selector, Mapping):
        raise ComparisonError(f"{label} final selector is missing")
    required = {
        "applied_code": 8,
        "command_code": 8,
        "lease_active": False,
        "remaining_lease_ms": 0,
        "command_lease_ms": 0,
        "command_valid": True,
        "invalid_command": False,
        "guard_active": False,
        "status_flags": 1,
    }
    if any(selector.get(key) != value for key, value in required.items()):
        raise ComparisonError(f"{label} did not finish lease-free ALL_OFF")


def _validate_group(
    paths: Sequence[Path],
    expected_hashes: Mapping[str, str],
    expected_source_commit: str,
    label: str,
) -> list[dict[str, Any]]:
    if len(paths) != len(expected_hashes):
        raise ComparisonError(f"{label} requires exactly {len(expected_hashes)} runs")
    documents: dict[str, tuple[Path, dict[str, Any]]] = {}
    for raw_path in paths:
        path = raw_path.resolve()
        if path.name != "run.json" or not path.is_file() or path.is_symlink():
            raise ComparisonError(f"{label} input is not a regular run.json")
        run = _load_json(path)
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or run_id not in expected_hashes:
            if run_id == EXCLUDED_INTERVENING_RUN_ID:
                raise ComparisonError(
                    f"{label} includes explicitly excluded intervening run {run_id}"
                )
            raise ComparisonError(f"{label} contains an unpinned run")
        if run_id in documents:
            raise ComparisonError(f"{label} contains duplicate run {run_id}")
        if sha256_path(path) != expected_hashes[run_id]:
            raise ComparisonError(f"{label} run {run_id} differs from its pinned SHA-256")
        documents[run_id] = (path, run)
    if set(documents) != set(expected_hashes):
        raise ComparisonError(f"{label} exact run set is incomplete")

    validated: list[dict[str, Any]] = []
    expected_order = [(frequency, state) for frequency in FREQUENCIES_HZ for state in STATES]
    for run_id in sorted(documents):
        path, run = documents[run_id]
        if run.get("schema") != 1 or run.get("mode") != "external" or run.get("error") is not None:
            raise ComparisonError(f"{label} run {run_id} is failed or not external mode")
        if run.get("configuration") != EXPECTED_CONFIGURATION:
            raise ComparisonError(f"{label} run {run_id} configuration differs")
        if run.get("git_head") != expected_source_commit:
            raise ComparisonError(f"{label} run {run_id} capture commit differs")
        if any(run.get(key) != value for key, value in EXPECTED_FIXTURE.items()):
            raise ComparisonError(f"{label} run {run_id} fixture identity differs")
        _validate_final_safety(run, f"{label} run {run_id}")
        observations = run.get("observations")
        if not isinstance(observations, list) or len(observations) != 342:
            raise ComparisonError(f"{label} run {run_id} does not have 342 observations")
        actual_order = [
            (row.get("frequency_hz"), row.get("state"))
            for row in observations
            if isinstance(row, Mapping)
        ]
        if actual_order != expected_order:
            raise ComparisonError(f"{label} run {run_id} lattice/order differs")
        transfer: dict[tuple[int, str], complex] = {}
        quality = {
            "pilot_phase_residual_deg": [],
            "pilot_fit_standard_error_hz": [],
            "pilot_phase_step_coherence": [],
            "pilot_confidence": [],
            "rx1_coherent_amplitude_counts": [],
            "peak_component_counts": [],
        }
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping) or row.get("analysis_error") is not None:
                raise ComparisonError(f"{label} run {run_id} observation {index} failed")
            analysis = row.get("analysis")
            if not isinstance(analysis, Mapping):
                raise ComparisonError(f"{label} run {run_id} observation {index} has no analysis")
            value = analysis.get("transfer_rx2_over_rx1")
            pilot = analysis.get("pilot")
            peaks = analysis.get("peak_component_counts")
            if not isinstance(value, Mapping) or not isinstance(pilot, Mapping):
                raise ComparisonError(f"{label} run {run_id} observation {index} is malformed")
            if not isinstance(peaks, list) or len(peaks) != 2:
                raise ComparisonError(f"{label} run {run_id} observation {index} peaks differ")
            phasor = complex(
                _finite_number(value.get("real"), "transfer real"),
                _finite_number(value.get("imag"), "transfer imag"),
            )
            if abs(phasor) <= np.finfo(float).tiny:
                raise ComparisonError(f"{label} run {run_id} observation {index} transfer is zero")
            frequency = int(row["frequency_hz"])
            state = str(row["state"])
            transfer[(frequency, state)] = phasor
            quality["pilot_phase_residual_deg"].append(
                math.degrees(_finite_number(pilot.get("phase_residual_rms_rad"), "pilot residual"))
            )
            quality["pilot_fit_standard_error_hz"].append(
                _finite_number(pilot.get("fit_standard_error_hz"), "pilot fit SE")
            )
            quality["pilot_phase_step_coherence"].append(
                _finite_number(pilot.get("phase_step_coherence"), "pilot coherence")
            )
            quality["pilot_confidence"].append(
                _finite_number(pilot.get("confidence"), "pilot confidence")
            )
            quality["rx1_coherent_amplitude_counts"].append(
                _finite_number(pilot.get("coherent_amplitude"), "RX1 coherent amplitude")
            )
            quality["peak_component_counts"].append(
                max(_finite_number(item, "peak component") for item in peaks)
            )
        validated.append(
            {
                "run_id": run_id,
                "path": str(path),
                "run_json_sha256": expected_hashes[run_id],
                "git_head": run.get("git_head"),
                "first_capture_started_utc": observations[0]["radio_readback"]["started_utc"],
                "last_capture_completed_utc": observations[-1]["radio_readback"]["completed_utc"],
                "transfer": transfer,
                "quality": quality,
            }
        )
    return validated


def _path_ratios(run: Mapping[str, Any], state: str) -> np.ndarray:
    transfer = run["transfer"]
    values: list[complex] = []
    for frequency in FREQUENCIES_HZ:
        all_off = transfer[(frequency, "ALL_OFF")]
        path = transfer[(frequency, state)] - all_off
        reference = transfer[(frequency, "ANT8")] - all_off
        if abs(path) <= 1e-9 or abs(reference) <= 1e-9:
            raise ComparisonError(f"{run['run_id']} {frequency} {state} derived path is too small")
        values.append(path / reference)
    return np.asarray(values, dtype=np.complex128)


def _complex_log(values: np.ndarray) -> np.ndarray:
    return np.log(np.abs(values)) + 1j * np.unwrap(np.angle(values))


def _wrap_deg(values: np.ndarray) -> np.ndarray:
    return (values + 180.0) % 360.0 - 180.0


def _summary(values: Sequence[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": int(array.size),
        "minimum": float(np.min(array)),
        "median": float(np.median(array)),
        "p95": float(np.percentile(array, 95.0)),
        "maximum": float(np.max(array)),
    }


def _quality(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    samples: dict[str, list[float]] = {}
    per_run: list[dict[str, Any]] = []
    for run in group:
        run_summary: dict[str, Any] = {"run_id": run["run_id"]}
        for metric, values in run["quality"].items():
            samples.setdefault(metric, []).extend(values)
            run_summary[metric] = _summary(values)
        per_run.append(run_summary)
    return {
        "capture_count": sum(len(run["quality"]["peak_component_counts"]) for run in group),
        "samples": samples,
        "summary": {metric: _summary(values) for metric, values in samples.items()},
        "per_run": per_run,
    }


def _repeatability(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        ratios = [_path_ratios(run, state) for run in group]
        for index, frequency in enumerate(FREQUENCIES_HZ):
            coefficients = 1.0 / np.asarray([values[index] for values in ratios])
            magnitude_db = 20.0 * np.log10(np.abs(coefficients))
            mean_phase = np.angle(np.mean(coefficients))
            phase_offsets_deg = np.degrees(np.angle(coefficients * np.exp(-1j * mean_phase)))
            cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "magnitude_span_db": float(np.ptp(magnitude_db)),
                    "phase_span_deg": float(np.ptp(phase_offsets_deg)),
                }
            )
    magnitude = [row["magnitude_span_db"] for row in cells]
    phase = [row["phase_span_deg"] for row in cells]
    return {
        "cell_count": len(cells),
        "cells": cells,
        "magnitude_span_db": _summary(magnitude),
        "phase_span_deg": _summary(phase),
    }


def _transfer_repeatability(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for cohort_id, states in (
        ("selected", STATES[1:]),
        ("all_off", ("ALL_OFF",)),
    ):
        cells: list[dict[str, Any]] = []
        for frequency in FREQUENCIES_HZ:
            for state in states:
                values = np.asarray(
                    [run["transfer"][(frequency, state)] for run in group],
                    dtype=np.complex128,
                )
                magnitude_db = 20.0 * np.log10(np.abs(values))
                mean_phase = np.angle(np.mean(values))
                phase_offsets_deg = np.degrees(np.angle(values * np.exp(-1j * mean_phase)))
                cells.append(
                    {
                        "frequency_hz": frequency,
                        "state": state,
                        "magnitude_span_db": float(np.ptp(magnitude_db)),
                        "phase_span_deg": float(np.ptp(phase_offsets_deg)),
                    }
                )
        result[cohort_id] = {
            "cells": cells,
            "magnitude_span_db": _summary([row["magnitude_span_db"] for row in cells]),
            "phase_span_deg": _summary([row["phase_span_deg"] for row in cells]),
        }
    return result


def _cohort_mean_shift(
    original: Sequence[Mapping[str, Any]], future: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        original_ratios = [_path_ratios(run, state) for run in original]
        future_ratios = [_path_ratios(run, state) for run in future]
        original_coefficients = 1.0 / np.asarray(original_ratios)
        future_coefficients = 1.0 / np.asarray(future_ratios)
        for index, frequency in enumerate(FREQUENCIES_HZ):
            old_mean = complex(np.mean(original_coefficients[:, index]))
            new_mean = complex(np.mean(future_coefficients[:, index]))
            delta = new_mean / old_mean
            cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "gain_delta_db": float(20.0 * np.log10(abs(delta))),
                    "phase_delta_deg": float(np.degrees(np.angle(delta))),
                }
            )
    absolute_gain = [abs(row["gain_delta_db"]) for row in cells]
    absolute_phase = [abs(row["phase_delta_deg"]) for row in cells]
    return {
        "cells": cells,
        "absolute_gain_delta_db": _summary(absolute_gain),
        "absolute_phase_delta_deg": _summary(absolute_phase),
    }


def _harmonic_design(
    delta_frequency_ghz: np.ndarray, harmonics: int, delay_ns: float
) -> np.ndarray:
    count = delta_frequency_ghz.size
    design = np.zeros((2 * count, 3 + 2 * harmonics), dtype=np.float64)
    design[:count, 0] = 1.0
    design[count:, 1] = 1.0
    design[count:, 2] = -2.0 * np.pi * delta_frequency_ghz
    for harmonic in range(1, harmonics + 1):
        angle = 2.0 * np.pi * harmonic * delta_frequency_ghz * delay_ns
        cosine = np.cos(angle)
        sine = np.sin(angle)
        column = 3 + 2 * (harmonic - 1)
        design[:count, column] = cosine
        design[:count, column + 1] = sine
        design[count:, column] = -sine
        design[count:, column + 1] = cosine
    return design


def fit_log_harmonic_model(values: Sequence[complex], harmonics: int) -> dict[str, Any]:
    """Fit delay plus K harmonics of one log-response ripple period."""
    phasors = np.asarray(values, dtype=np.complex128)
    if phasors.shape != (len(FREQUENCIES_HZ),) or harmonics < 0:
        raise ComparisonError("harmonic model input shape/order differs")
    if np.any(np.abs(phasors) <= np.finfo(float).tiny) or not np.all(np.isfinite(phasors)):
        raise ComparisonError("harmonic model input is zero or non-finite")
    frequency_ghz = np.asarray(FREQUENCIES_HZ, dtype=np.float64) / 1e9
    delta_frequency_ghz = frequency_ghz - np.mean(frequency_ghz)
    measured_log = _complex_log(phasors)
    observation = np.concatenate((measured_log.real, measured_log.imag))
    delays = [0.0] if harmonics == 0 else RIPPLE_DELAY_GRID_NS
    best: tuple[float, float, np.ndarray, np.ndarray] | None = None
    for delay_ns in delays:
        design = _harmonic_design(delta_frequency_ghz, harmonics, float(delay_ns))
        parameters, *_ = np.linalg.lstsq(design, observation, rcond=None)
        residual = observation - design @ parameters
        score = float(np.mean(residual**2))
        if best is None or score < best[0]:
            best = (score, float(delay_ns), design, parameters)
    if best is None:
        raise ComparisonError("harmonic model search failed")
    score, delay_ns, design, parameters = best
    fitted = design @ parameters
    predicted_log = fitted[: phasors.size] + 1j * fitted[phasors.size :]
    predicted = np.exp(predicted_log)
    phase_error = np.degrees(np.angle(phasors * np.conj(predicted)))
    gain_error = 20.0 * np.log10(np.abs(phasors) / np.abs(predicted))
    return {
        "harmonics": harmonics,
        "parameter_count": 3 + 2 * harmonics + (1 if harmonics else 0),
        "ripple_delay_ns": delay_ns if harmonics else None,
        "ripple_period_ghz": 1.0 / delay_ns if harmonics else None,
        "training_phase_rms_deg": float(np.sqrt(np.mean(phase_error**2))),
        "training_gain_rms_db": float(np.sqrt(np.mean(gain_error**2))),
        "joint_log_mean_square": score,
        "predicted_real": predicted.real.tolist(),
        "predicted_imag": predicted.imag.tolist(),
    }


def fit_exact_single_echo_model(values: Sequence[complex]) -> dict[str, Any]:
    """Fit R=b0*exp(-jw*tau)+b1*exp(-jw*(tau+delta)) by variable projection."""
    phasors = np.asarray(values, dtype=np.complex128)
    if phasors.shape != (len(FREQUENCIES_HZ),):
        raise ComparisonError("exact echo model input shape/order differs")
    if np.any(np.abs(phasors) <= np.finfo(float).tiny) or not np.all(np.isfinite(phasors)):
        raise ComparisonError("exact echo model input is zero or non-finite")
    frequency_ghz = np.asarray(FREQUENCIES_HZ, dtype=np.float64) / 1e9
    delta_frequency_ghz = frequency_ghz - np.mean(frequency_ghz)
    path_delay_grid_ns = np.arange(-2.5, 2.5, 0.01)
    transformed = (
        phasors[np.newaxis, :]
        * np.exp(
            2j * np.pi * path_delay_grid_ns[:, np.newaxis] * delta_frequency_ghz[np.newaxis, :]
        )
    ).T
    energy = float(np.sum(np.abs(phasors) ** 2))
    best: tuple[float, float, float, np.ndarray] | None = None
    for echo_delay_ns in np.arange(0.05, 2.5001, 0.01):
        design = np.column_stack(
            (
                np.ones(phasors.size),
                np.exp(-2j * np.pi * delta_frequency_ghz * echo_delay_ns),
            )
        )
        projected = design.conj().T @ transformed
        coefficients = np.linalg.solve(design.conj().T @ design, projected)
        scores = (
            energy - np.real(np.sum(np.conj(coefficients) * projected, axis=0))
        ) / phasors.size
        index = int(np.argmin(scores))
        candidate = (
            float(scores[index]),
            float(path_delay_grid_ns[index]),
            float(echo_delay_ns),
            coefficients[:, index],
        )
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        raise ComparisonError("exact echo search failed")

    _, coarse_path_delay_ns, coarse_echo_delay_ns, _ = best
    for echo_delay_ns in np.arange(
        max(0.05, coarse_echo_delay_ns - 0.012),
        min(2.5, coarse_echo_delay_ns + 0.012) + 0.0001,
        0.001,
    ):
        for path_delay_ns in np.arange(
            coarse_path_delay_ns - 0.012,
            coarse_path_delay_ns + 0.0121,
            0.001,
        ):
            design = np.column_stack(
                (
                    np.exp(-2j * np.pi * delta_frequency_ghz * path_delay_ns),
                    np.exp(-2j * np.pi * delta_frequency_ghz * (path_delay_ns + echo_delay_ns)),
                )
            )
            coefficients, *_ = np.linalg.lstsq(design, phasors, rcond=None)
            score = float(np.mean(np.abs(phasors - design @ coefficients) ** 2))
            if score < best[0]:
                best = (score, float(path_delay_ns), float(echo_delay_ns), coefficients)

    score, path_delay_ns, echo_delay_ns, coefficients = best
    prediction = coefficients[0] * np.exp(
        -2j * np.pi * delta_frequency_ghz * path_delay_ns
    ) + coefficients[1] * np.exp(
        -2j * np.pi * delta_frequency_ghz * (path_delay_ns + echo_delay_ns)
    )
    phase_error = np.degrees(np.angle(phasors * np.conj(prediction)))
    gain_error = 20.0 * np.log10(np.abs(phasors) / np.abs(prediction))
    echo_ratio = coefficients[1] / coefficients[0]
    return {
        "parameter_count": 6,
        "base_delay_ns": path_delay_ns,
        "echo_delay_ns": echo_delay_ns,
        "echo_relative_magnitude": float(abs(echo_ratio)),
        "echo_phase_at_center_deg": float(np.degrees(np.angle(echo_ratio))),
        "training_phase_rms_deg": float(np.sqrt(np.mean(phase_error**2))),
        "training_gain_rms_db": float(np.sqrt(np.mean(gain_error**2))),
        "complex_mean_square": score,
        "predicted_real": prediction.real.tolist(),
        "predicted_imag": prediction.imag.tolist(),
    }


def _score_predictions(
    predictions: Mapping[str, np.ndarray], future: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    per_path: list[dict[str, Any]] = []
    all_phase: list[float] = []
    all_gain: list[float] = []
    closure: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        prediction = predictions[state]
        path_phase: list[float] = []
        path_gain: list[float] = []
        for run in future:
            measured = _path_ratios(run, state)
            phase = np.degrees(np.angle(measured * np.conj(prediction)))
            gain = 20.0 * np.log10(np.abs(measured) / np.abs(prediction))
            path_phase.extend(phase.tolist())
            path_gain.extend(gain.tolist())
            for index, frequency in enumerate(FREQUENCIES_HZ):
                closure.append(
                    {
                        "run_id": run["run_id"],
                        "frequency_hz": frequency,
                        "state": state,
                        "phase_error_deg": float(phase[index]),
                        "gain_error_db": float(gain[index]),
                    }
                )
        all_phase.extend(path_phase)
        all_gain.extend(path_gain)
        per_path.append(
            {
                "state": state,
                "phase_rms_deg": float(np.sqrt(np.mean(np.square(path_phase)))),
                "gain_rms_db": float(np.sqrt(np.mean(np.square(path_gain)))),
                "maximum_absolute_phase_error_deg": float(np.max(np.abs(path_phase))),
                "maximum_absolute_gain_error_db": float(np.max(np.abs(path_gain))),
            }
        )
    return {
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(all_phase)))),
        "gain_rms_db": float(np.sqrt(np.mean(np.square(all_gain)))),
        "maximum_absolute_phase_error_deg": float(np.max(np.abs(all_phase))),
        "maximum_absolute_gain_error_db": float(np.max(np.abs(all_gain))),
        "per_path": per_path,
        "closure": closure,
    }


def _chebyshev_predictions(
    training_mean: Mapping[str, np.ndarray], degree: int
) -> dict[str, np.ndarray]:
    frequency = np.asarray(FREQUENCIES_HZ, dtype=np.float64)
    x = (frequency - np.mean(frequency)) / (np.ptp(frequency) / 2.0)
    result: dict[str, np.ndarray] = {}
    for state, values in training_mean.items():
        measured_log = _complex_log(values)
        real = np.polynomial.chebyshev.chebfit(x, measured_log.real, degree)
        imag = np.polynomial.chebyshev.chebfit(x, measured_log.imag, degree)
        result[state] = np.exp(
            np.polynomial.chebyshev.chebval(x, real) + 1j * np.polynomial.chebyshev.chebval(x, imag)
        )
    return result


def _linear_knot_predictions(
    training_mean: Mapping[str, np.ndarray], knot_count: int
) -> dict[str, np.ndarray]:
    frequency = np.asarray(FREQUENCIES_HZ, dtype=np.float64)
    x = (frequency - np.min(frequency)) / np.ptp(frequency)
    knots = np.linspace(0.0, 1.0, knot_count)
    basis = np.zeros((x.size, knot_count), dtype=np.float64)
    for row, value in enumerate(x):
        if value <= knots[0]:
            basis[row, 0] = 1.0
        elif value >= knots[-1]:
            basis[row, -1] = 1.0
        else:
            left = int(np.searchsorted(knots, value) - 1)
            fraction = (value - knots[left]) / (knots[left + 1] - knots[left])
            basis[row, left] = 1.0 - fraction
            basis[row, left + 1] = fraction
    result: dict[str, np.ndarray] = {}
    for state, values in training_mean.items():
        measured_log = _complex_log(values)
        real, *_ = np.linalg.lstsq(basis, measured_log.real, rcond=None)
        imag, *_ = np.linalg.lstsq(basis, measured_log.imag, rcond=None)
        result[state] = np.exp(basis @ real + 1j * basis @ imag)
    return result


def _serializable_predictions(values: Mapping[str, np.ndarray]) -> list[dict[str, Any]]:
    return [
        {
            "state": state,
            "real": prediction.real.tolist(),
            "imag": prediction.imag.tolist(),
        }
        for state, prediction in values.items()
    ]


def analyze_comparison(
    original_paths: Sequence[Path], future_paths: Sequence[Path]
) -> dict[str, Any]:
    original = _validate_group(
        original_paths,
        ORIGINAL_RUN_SHA256,
        ORIGINAL_SOURCE_COMMIT,
        "original cohort",
    )
    future = _validate_group(
        future_paths,
        FUTURE_RUN_SHA256,
        FUTURE_SOURCE_COMMIT,
        "future cohort",
    )
    training_mean = {
        state: np.mean([_path_ratios(run, state) for run in original], axis=0)
        for state in MODEL_STATES
    }

    models: list[dict[str, Any]] = []
    harmonic_predictions: dict[int, dict[str, np.ndarray]] = {}
    harmonic_fits: dict[int, list[dict[str, Any]]] = {}
    for harmonic_count in range(5):
        predictions: dict[str, np.ndarray] = {}
        fits: list[dict[str, Any]] = []
        for state in MODEL_STATES:
            fit = fit_log_harmonic_model(training_mean[state], harmonic_count)
            predicted = np.asarray(fit.pop("predicted_real")) + 1j * np.asarray(
                fit.pop("predicted_imag")
            )
            predictions[state] = predicted
            fits.append({"state": state, **fit})
        harmonic_predictions[harmonic_count] = predictions
        harmonic_fits[harmonic_count] = fits
        scored = _score_predictions(predictions, future)
        scored.pop("closure")
        models.append(
            {
                "model_id": f"log_harmonic_k{harmonic_count}",
                "label": "delay only"
                if harmonic_count == 0
                else f"{harmonic_count} log-ripple harmonic{'s' if harmonic_count != 1 else ''}",
                "parameter_count_per_path": fits[0]["parameter_count"],
                "future_score": scored,
                "path_fits": fits,
            }
        )

    exact_echo_predictions: dict[str, np.ndarray] = {}
    exact_echo_fits: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        fit = fit_exact_single_echo_model(training_mean[state])
        predicted = np.asarray(fit.pop("predicted_real")) + 1j * np.asarray(
            fit.pop("predicted_imag")
        )
        exact_echo_predictions[state] = predicted
        exact_echo_fits.append({"state": state, **fit})
    exact_echo_score = _score_predictions(exact_echo_predictions, future)
    exact_echo_score.pop("closure")
    models.append(
        {
            "model_id": "exact_single_echo",
            "label": "exact single echo",
            "parameter_count_per_path": 6,
            "future_score": exact_echo_score,
            "path_fits": exact_echo_fits,
        }
    )

    for degree in (5, 9, 15):
        predictions = _chebyshev_predictions(training_mean, degree)
        score = _score_predictions(predictions, future)
        score.pop("closure")
        models.append(
            {
                "model_id": f"chebyshev_log_degree_{degree}",
                "label": f"log-Chebyshev degree {degree}",
                "parameter_count_per_path": 2 * (degree + 1),
                "future_score": score,
            }
        )
    for knot_count in (10, 20, 26):
        predictions = _linear_knot_predictions(training_mean, knot_count)
        score = _score_predictions(predictions, future)
        score.pop("closure")
        models.append(
            {
                "model_id": f"linear_log_{knot_count}_knots",
                "label": f"piecewise-linear log, {knot_count} knots",
                "parameter_count_per_path": 2 * knot_count,
                "future_score": score,
            }
        )

    frequency_table = {
        state: 1.0 / np.mean([1.0 / _path_ratios(run, state) for run in original], axis=0)
        for state in MODEL_STATES
    }
    table_score = _score_predictions(frequency_table, future)
    table_closure = table_score.pop("closure")
    models.append(
        {
            "model_id": "frequency_indexed_complex_table",
            "label": "100 MHz complex table",
            "parameter_count_per_path": 2 * len(FREQUENCIES_HZ),
            "future_score": table_score,
        }
    )

    quality_original = _quality(original)
    quality_future = _quality(future)
    repeatability_original = _repeatability(original)
    repeatability_future = _repeatability(future)
    transfer_repeatability_original = _transfer_repeatability(original)
    transfer_repeatability_future = _transfer_repeatability(future)
    cohort_mean_shift = _cohort_mean_shift(original, future)
    curve_data: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        future_values = np.asarray([_path_ratios(run, state) for run in future])
        curve_data.append(
            {
                "state": state,
                "training_mean_real": training_mean[state].real.tolist(),
                "training_mean_imag": training_mean[state].imag.tolist(),
                "future_real": future_values.real.tolist(),
                "future_imag": future_values.imag.tolist(),
                "one_ripple_real": harmonic_predictions[1][state].real.tolist(),
                "one_ripple_imag": harmonic_predictions[1][state].imag.tolist(),
                "exact_echo_real": exact_echo_predictions[state].real.tolist(),
                "exact_echo_imag": exact_echo_predictions[state].imag.tolist(),
                "three_harmonic_real": harmonic_predictions[3][state].real.tolist(),
                "three_harmonic_imag": harmonic_predictions[3][state].imag.tolist(),
                "table_real": frequency_table[state].real.tolist(),
                "table_imag": frequency_table[state].imag.tolist(),
            }
        )

    return {
        "schema": 1,
        "evidence_kind": "smateway.broadband-cohort-comparison/v1",
        "comparison_id": "original-3-vs-future-5-20260831",
        "cohort_definition": {
            "original_role": "training and calibration construction only",
            "future_role": "future-held-out scoring only",
            "excluded_intervening_run_id": EXCLUDED_INTERVENING_RUN_ID,
            "exclusion_reason": (
                "not one of the five consecutive sweeps explicitly requested after the "
                "report update"
            ),
            "original_runs": [
                {
                    key: value
                    for key, value in run.items()
                    if key not in {"transfer", "quality", "path"}
                }
                for run in original
            ],
            "future_runs": [
                {
                    key: value
                    for key, value in run.items()
                    if key not in {"transfer", "quality", "path"}
                }
                for run in future
            ],
        },
        "method": {
            "frequencies_hz": list(FREQUENCIES_HZ),
            "model_states": list(MODEL_STATES),
            "path_ratio": "R_i(f) = (H_i-H_ALL_OFF)/(H_ANT8-H_ALL_OFF)",
            "calibration_coefficient": "C_i(f) = 1/R_i(f)",
            "future_error": "phase/gain of measured future R_i divided by training prediction",
            "log_harmonic_model": (
                "log R = a + j(phi-2*pi*(f-f0)*tau) + sum_k q_k exp(-j*2*pi*k*(f-f0)*delta)"
            ),
            "exact_single_echo_model": (
                "R = b0*exp(-j*2*pi*(f-f0)*tau) + b1*exp(-j*2*pi*(f-f0)*(tau+delta))"
            ),
            "model_selection_warning": (
                "comparison of model families on the five future sweeps is exploratory; "
                "a later untouched cohort is required for confirmatory selection"
            ),
            "raw_replay_scope": (
                "run.json phasors are SHA-256 pinned here; raw-IQ replay is documented "
                "in the source campaign reports and is not repeated by this analyzer"
            ),
        },
        "quality": {"original_3": quality_original, "future_5": quality_future},
        "repeatability": {
            "original_3": repeatability_original,
            "future_5": repeatability_future,
        },
        "transfer_repeatability": {
            "original_3": transfer_repeatability_original,
            "future_5": transfer_repeatability_future,
        },
        "cohort_mean_calibration_shift": cohort_mean_shift,
        "models": models,
        "frequency_table_closure": table_closure,
        "curve_data": curve_data,
        "frequency_table_predictions": _serializable_predictions(frequency_table),
    }


def _model(result: Mapping[str, Any], model_id: str) -> Mapping[str, Any]:
    return next(item for item in result["models"] if item["model_id"] == model_id)


def _phasor(real: Sequence[float], imag: Sequence[float]) -> np.ndarray:
    return np.asarray(real, dtype=np.float64) + 1j * np.asarray(imag, dtype=np.float64)


def _aligned_unwrapped_phase_deg(values: np.ndarray, reference: np.ndarray) -> np.ndarray:
    phase = np.degrees(np.unwrap(np.angle(values)))
    reference_phase = np.degrees(np.unwrap(np.angle(reference)))
    phase += 360.0 * round(float(np.median(reference_phase - phase)) / 360.0)
    return phase


def render_figures(result: Mapping[str, Any], figure_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 120, "savefig.dpi": 180, "font.size": 10})
    colors = {"original_3": "#4C78A8", "future_5": "#F58518"}

    quality_metrics = [
        ("pilot_phase_residual_deg", "Pilot phase residual RMS (deg)"),
        ("pilot_fit_standard_error_hz", "Pilot frequency-fit SE (Hz)"),
        ("rx1_coherent_amplitude_counts", "RX1 coherent amplitude (counts)"),
        ("peak_component_counts", "Maximum ADC component (counts)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for axis, (metric, title) in zip(axes.flat, quality_metrics, strict=True):
        datasets = [
            result["quality"][group]["samples"][metric] for group in ("original_3", "future_5")
        ]
        boxes = axis.boxplot(
            datasets, tick_labels=["original 3", "future 5"], showfliers=False, patch_artist=True
        )
        for box, group in zip(boxes["boxes"], ("original_3", "future_5"), strict=True):
            box.set_facecolor(colors[group])
            box.set_alpha(0.75)
        axis.set_title(title)
        axis.grid(axis="y", alpha=0.25)
        medians = [np.median(values) for values in datasets]
        axis.text(
            0.02,
            0.96,
            f"medians: {medians[0]:.4g} / {medians[1]:.4g}",
            transform=axis.transAxes,
            va="top",
        )
    fig.suptitle("Acquisition quality: broadly comparable, not uniformly improved")
    fig.savefig(figure_dir / "fig01_acquisition_quality.png")
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for column, (metric, title) in enumerate(
        (
            ("magnitude_span_db", "Calibration magnitude span (dB)"),
            ("phase_span_deg", "Calibration phase span (deg)"),
        )
    ):
        for group in ("original_3", "future_5"):
            values = np.sort([row[metric] for row in result["repeatability"][group]["cells"]])
            cdf = np.arange(1, values.size + 1) / values.size
            axes[0, column].plot(
                values, cdf, label=group.replace("_", " "), color=colors[group], linewidth=2
            )
        axes[0, column].set_xlabel(title)
        axes[0, column].set_ylabel("Empirical CDF")
        axes[0, column].grid(alpha=0.25)
        axes[0, column].legend()
        for group in ("original_3", "future_5"):
            cells = result["repeatability"][group]["cells"]
            by_frequency = []
            for frequency in FREQUENCIES_HZ:
                cohort = [row[metric] for row in cells if row["frequency_hz"] == frequency]
                by_frequency.append(max(cohort))
            axes[1, column].plot(
                np.asarray(FREQUENCIES_HZ) / 1e9,
                by_frequency,
                label=group.replace("_", " "),
                color=colors[group],
            )
        axes[1, column].set_xlabel("RF frequency (GHz)")
        axes[1, column].set_ylabel(f"Worst port {title.lower()}")
        axes[1, column].grid(alpha=0.25)
    fig.suptitle("Within-cohort calibration repeatability: phase improved; magnitude tail did not")
    fig.savefig(figure_dir / "fig02_repeatability_comparison.png")
    plt.close(fig)

    display_ids = [
        "log_harmonic_k0",
        "log_harmonic_k1",
        "exact_single_echo",
        "log_harmonic_k2",
        "log_harmonic_k3",
        "log_harmonic_k4",
        "chebyshev_log_degree_9",
        "linear_log_20_knots",
        "linear_log_26_knots",
        "frequency_indexed_complex_table",
    ]
    selected = [_model(result, model_id) for model_id in display_ids]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.4), constrained_layout=True)
    for axis, key, ylabel in (
        (axes[0], "phase_rms_deg", "Future-held-out phase RMS (deg)"),
        (axes[1], "gain_rms_db", "Future-held-out gain RMS (dB)"),
    ):
        for item in selected:
            count = item["parameter_count_per_path"]
            score = item["future_score"][key]
            marker = "*" if item["model_id"] == "frequency_indexed_complex_table" else "o"
            size = 150 if marker == "*" else 55
            axis.scatter(count, score, s=size, marker=marker, label=item["label"])
            short_label = (
                item["model_id"]
                .replace("log_harmonic_k", "K")
                .replace("frequency_indexed_complex_table", "table")
                .replace("exact_single_echo", "exact echo")
            )
            axis.annotate(
                short_label,
                (count, score),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
        axis.set_xlabel("Real parameters per path")
        axis.set_ylabel(ylabel)
        axis.grid(alpha=0.25)
    fig.suptitle("Compact smooth models miss deterministic frequency structure")
    fig.savefig(figure_dir / "fig03_model_complexity_vs_future_error.png")
    plt.close(fig)

    bar_ids = [
        "log_harmonic_k0",
        "log_harmonic_k1",
        "exact_single_echo",
        "log_harmonic_k3",
        "frequency_indexed_complex_table",
    ]
    bar_models = [_model(result, model_id) for model_id in bar_ids]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    x = np.arange(len(MODEL_STATES))
    width = 0.16
    for model_index, item in enumerate(bar_models):
        by_path = {row["state"]: row for row in item["future_score"]["per_path"]}
        axes[0].bar(
            x + (model_index - 2.0) * width,
            [by_path[state]["phase_rms_deg"] for state in MODEL_STATES],
            width,
            label=item["label"],
        )
        axes[1].bar(
            x + (model_index - 2.0) * width,
            [by_path[state]["gain_rms_db"] for state in MODEL_STATES],
            width,
            label=item["label"],
        )
    axes[0].set_ylabel("Future phase RMS (deg)")
    axes[1].set_ylabel("Future gain RMS (dB)")
    for axis in axes:
        axis.set_xticks(x, MODEL_STATES)
        axis.grid(axis="y", alpha=0.25)
        axis.legend(ncols=2)
    fig.suptitle("Future-held-out error by selector path")
    fig.savefig(figure_dir / "fig04_future_model_error_by_path.png")
    plt.close(fig)

    frequency_ghz = np.asarray(FREQUENCIES_HZ) / 1e9
    fig, axes = plt.subplots(4, 2, figsize=(14, 14), constrained_layout=True, sharex=True)
    for axis, row in zip(axes.flat, result["curve_data"], strict=False):
        training = _phasor(row["training_mean_real"], row["training_mean_imag"])
        future = np.asarray(row["future_real"]) + 1j * np.asarray(row["future_imag"])
        training_phase = np.degrees(np.unwrap(np.angle(training)))
        future_phase = np.asarray(
            [_aligned_unwrapped_phase_deg(values, training) for values in future]
        )
        axis.fill_between(
            frequency_ghz,
            np.min(future_phase, axis=0),
            np.max(future_phase, axis=0),
            color="#4C78A8",
            alpha=0.18,
            label="future-5 range",
        )
        axis.plot(
            frequency_ghz,
            np.mean(future_phase, axis=0),
            color="#4C78A8",
            linewidth=1.4,
            label="future-5 mean",
        )
        axis.scatter(
            frequency_ghz, training_phase, color="black", s=13, label="original-3 mean", zorder=3
        )
        for real_key, imag_key, color, label, style in (
            ("one_ripple_real", "one_ripple_imag", "#F58518", "one ripple", "--"),
            ("exact_echo_real", "exact_echo_imag", "#B279A2", "exact echo", "-"),
            ("three_harmonic_real", "three_harmonic_imag", "#E45756", "3 harmonics", "-."),
            ("table_real", "table_imag", "#54A24B", "100 MHz table", ":"),
        ):
            model_values = _phasor(row[real_key], row[imag_key])
            axis.plot(
                frequency_ghz,
                _aligned_unwrapped_phase_deg(model_values, training),
                color=color,
                linestyle=style,
                linewidth=1.4,
                label=label,
            )
        axis.set_title(row["state"] + " vs ANT8")
        axis.set_ylabel("Unwrapped phase (deg)")
        axis.grid(alpha=0.25)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    axes.flat[-1].legend(handles, labels, loc="center", frameon=False)
    for axis in axes[-1, :]:
        if axis.axison:
            axis.set_xlabel("RF frequency (GHz)")
    fig.suptitle("Phase structure is stable but not captured by one smooth ripple")
    fig.savefig(figure_dir / "fig05_phase_model_overlay.png")
    plt.close(fig)

    fig, axes = plt.subplots(4, 2, figsize=(14, 14), constrained_layout=True, sharex=True)
    for axis, row in zip(axes.flat, result["curve_data"], strict=False):
        training = _phasor(row["training_mean_real"], row["training_mean_imag"])
        future = np.asarray(row["future_real"]) + 1j * np.asarray(row["future_imag"])
        future_gain = 20.0 * np.log10(np.abs(future))
        axis.fill_between(
            frequency_ghz,
            np.min(future_gain, axis=0),
            np.max(future_gain, axis=0),
            color="#4C78A8",
            alpha=0.18,
            label="future-5 range",
        )
        axis.plot(
            frequency_ghz,
            np.mean(future_gain, axis=0),
            color="#4C78A8",
            linewidth=1.4,
            label="future-5 mean",
        )
        axis.scatter(
            frequency_ghz,
            20.0 * np.log10(np.abs(training)),
            color="black",
            s=13,
            label="original-3 mean",
            zorder=3,
        )
        for real_key, imag_key, color, label, style in (
            ("one_ripple_real", "one_ripple_imag", "#F58518", "one ripple", "--"),
            ("exact_echo_real", "exact_echo_imag", "#B279A2", "exact echo", "-"),
            ("three_harmonic_real", "three_harmonic_imag", "#E45756", "3 harmonics", "-."),
            ("table_real", "table_imag", "#54A24B", "100 MHz table", ":"),
        ):
            model_values = _phasor(row[real_key], row[imag_key])
            axis.plot(
                frequency_ghz,
                20.0 * np.log10(np.abs(model_values)),
                color=color,
                linestyle=style,
                linewidth=1.4,
                label=label,
            )
        axis.set_title(row["state"] + " vs ANT8")
        axis.set_ylabel("Relative path gain (dB)")
        axis.grid(alpha=0.25)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    axes.flat[-1].legend(handles, labels, loc="center", frameon=False)
    for axis in axes[-1, :]:
        if axis.axison:
            axis.set_xlabel("RF frequency (GHz)")
    fig.suptitle("Gain ripple repeats across cohorts; the frequency table tracks it")
    fig.savefig(figure_dir / "fig06_gain_model_overlay.png")
    plt.close(fig)

    shift_cells = result["cohort_mean_calibration_shift"]["cells"]
    gain_shift = np.asarray(
        [
            [
                next(
                    row["gain_delta_db"]
                    for row in shift_cells
                    if row["state"] == state and row["frequency_hz"] == frequency
                )
                for frequency in FREQUENCIES_HZ
            ]
            for state in MODEL_STATES
        ]
    )
    phase_shift = np.asarray(
        [
            [
                next(
                    row["phase_delta_deg"]
                    for row in shift_cells
                    if row["state"] == state and row["frequency_hz"] == frequency
                )
                for frequency in FREQUENCIES_HZ
            ]
            for state in MODEL_STATES
        ]
    )
    fig, axes = plt.subplots(2, 1, figsize=(14, 6.8), constrained_layout=True)
    for axis, matrix, label in (
        (axes[0], gain_shift, "Future minus original calibration gain (dB)"),
        (axes[1], phase_shift, "Future minus original calibration phase (deg)"),
    ):
        limit = float(np.max(np.abs(matrix)))
        image = axis.imshow(
            matrix,
            aspect="auto",
            cmap="RdBu_r",
            vmin=-limit,
            vmax=limit,
            extent=(2.05, 5.85, len(MODEL_STATES) - 0.5, -0.5),
        )
        axis.set_yticks(np.arange(len(MODEL_STATES)), MODEL_STATES)
        axis.set_ylabel("Selector path")
        axis.set_xlabel("RF frequency (GHz)")
        axis.set_title(label)
        fig.colorbar(image, ax=axis, pad=0.015)
    fig.suptitle("Cohort-mean relative calibration is stable at the measured knots")
    fig.savefig(figure_dir / "fig07_cohort_mean_calibration_shift.png")
    plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = analyze_comparison(args.original_run, args.future_run)
    render_figures(result, args.figure_dir)
    actual_figure_names = {path.name for path in args.figure_dir.glob("*.png")}
    if actual_figure_names != set(FIGURE_FILENAMES):
        raise ComparisonError("figure directory contains a stale or missing PNG")
    result["figures"] = [
        {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_path(path),
        }
        for path in (args.figure_dir / name for name in FIGURE_FILENAMES)
    ]
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
