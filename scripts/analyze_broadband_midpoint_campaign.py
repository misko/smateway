#!/usr/bin/env python3
"""Validate five unseen 50 MHz midpoint sweeps and score frozen broadband models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import analyze_broadband_cohort_comparison as cohort
import analyze_pinned_broadband_campaign as raw_analyzer
import numpy as np

MIDPOINT_FREQUENCIES_HZ = tuple(range(2_150_000_000, 5_750_000_001, 100_000_000))
MIDPOINT_RUN_SHA256 = {
    "20260831T012101.150426Z": "9e6b7dde591fa31dc536625c8f5cf723b2bb4728d6e08a443bc20089666e1a3e",
    "20260831T013428.712215Z": "a089e15757d4fcec95ef0720334a03c2820a13d4b08fe3b5a5b9fd9ba7b92e98",
    "20260831T014754.522922Z": "a57c6b7cc4d1272800c06b0b27c2af298fa7d844519903bdae3bff50ab880a75",
    "20260831T020127.934844Z": "a3eb3d158acf3be5bd1799f4062ec772709bc5da0f74a3c1c129a8811e6b009c",
    "20260831T021459.216318Z": "2267be84215282274a3275071dbf6c4a025e4cb26dc4533cf74622a464c7f643",
}
MIDPOINT_CONFIGURATION = {
    **cohort.EXPECTED_CONFIGURATION,
    "frequencies_hz": list(MIDPOINT_FREQUENCIES_HZ),
}
FIGURE_FILENAMES = (
    "fig01_midpoint_model_summary.png",
    "fig02_midpoint_error_by_path.png",
    "fig03_midpoint_phase_error_heatmaps.png",
    "fig04_midpoint_gain_error_heatmaps.png",
    "fig05_midpoint_phase_overlays.png",
    "fig06_midpoint_gain_overlays.png",
)


class MidpointError(ValueError):
    """The midpoint campaign or model result is inadmissible."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-run", type=Path, action="append", required=True)
    parser.add_argument("--midpoint-run", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--figure-dir", type=Path, required=True)
    parser.add_argument(
        "--skip-raw-replay",
        action="store_true",
        help="development-only: trust stored phasors instead of replaying every NPZ",
    )
    return parser


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_midpoint_group(paths: Sequence[Path], *, replay_raw: bool) -> list[dict[str, Any]]:
    if len(paths) != len(MIDPOINT_RUN_SHA256):
        raise MidpointError("midpoint campaign requires exactly five runs")
    by_id: dict[str, tuple[Path, dict[str, Any]]] = {}
    for raw_path in paths:
        path = raw_path.resolve()
        if path.name != "run.json" or not path.is_file() or path.is_symlink():
            raise MidpointError("midpoint input is not a regular run.json")
        document = cohort._load_json(path)
        run_id = document.get("run_id")
        if not isinstance(run_id, str) or run_id not in MIDPOINT_RUN_SHA256:
            raise MidpointError("midpoint campaign contains an unpinned run")
        if run_id in by_id:
            raise MidpointError(f"midpoint campaign duplicates {run_id}")
        if cohort.sha256_path(path) != MIDPOINT_RUN_SHA256[run_id]:
            raise MidpointError(f"midpoint run {run_id} fails its pinned SHA-256")
        by_id[run_id] = (path, document)
    if set(by_id) != set(MIDPOINT_RUN_SHA256):
        raise MidpointError("exact midpoint campaign is incomplete")

    expected_order = [
        (frequency, state) for frequency in MIDPOINT_FREQUENCIES_HZ for state in cohort.STATES
    ]
    validated: list[dict[str, Any]] = []
    replay_index = 0
    for run_id in sorted(by_id):
        path, document = by_id[run_id]
        if (
            document.get("schema") != 1
            or document.get("mode") != "external"
            or document.get("error") is not None
        ):
            raise MidpointError(f"midpoint run {run_id} is failed or not external mode")
        if document.get("configuration") != MIDPOINT_CONFIGURATION:
            raise MidpointError(f"midpoint run {run_id} configuration differs")
        if document.get("git_head") != cohort.FUTURE_SOURCE_COMMIT:
            raise MidpointError(f"midpoint run {run_id} capture commit differs")
        if any(document.get(key) != value for key, value in cohort.EXPECTED_FIXTURE.items()):
            raise MidpointError(f"midpoint run {run_id} fixture identity differs")
        cohort._validate_final_safety(document, f"midpoint run {run_id}")
        observations = document.get("observations")
        if not isinstance(observations, list) or len(observations) != 333:
            raise MidpointError(f"midpoint run {run_id} does not have 333 observations")
        actual_order = [
            (row.get("frequency_hz"), row.get("state"))
            for row in observations
            if isinstance(row, Mapping)
        ]
        if actual_order != expected_order:
            raise MidpointError(f"midpoint run {run_id} lattice/order differs")
        expected_names = {
            f"{frequency}-{state.lower()}-r1.npz" for frequency, state in expected_order
        }
        artifacts = list(path.parent.glob("*.npz"))
        if {item.name for item in artifacts} != expected_names or len(artifacts) != 333:
            raise MidpointError(f"midpoint run {run_id} NPZ set differs")
        if any(item.is_symlink() or not item.is_file() for item in artifacts):
            raise MidpointError(f"midpoint run {run_id} NPZ set contains a non-file")

        transfer: dict[tuple[int, str], complex] = {}
        quality = {
            "pilot_phase_residual_deg": [],
            "pilot_fit_standard_error_hz": [],
            "pilot_phase_step_coherence": [],
            "peak_component_counts": [],
        }
        artifact_manifest: list[dict[str, Any]] = []
        maximum_replay_delta = 0.0
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping) or row.get("analysis_error") is not None:
                raise MidpointError(f"midpoint run {run_id} observation {index} failed")
            analysis = row.get("analysis")
            pilot = analysis.get("pilot") if isinstance(analysis, Mapping) else None
            stored = (
                analysis.get("transfer_rx2_over_rx1") if isinstance(analysis, Mapping) else None
            )
            if not isinstance(pilot, Mapping) or not isinstance(stored, Mapping):
                raise MidpointError(f"midpoint run {run_id} observation {index} is malformed")
            stored_phasor = complex(
                cohort._finite_number(stored.get("real"), "stored transfer real"),
                cohort._finite_number(stored.get("imag"), "stored transfer imag"),
            )
            if abs(stored_phasor) <= np.finfo(float).tiny:
                raise MidpointError(f"midpoint run {run_id} observation {index} is zero")
            frequency = int(row["frequency_hz"])
            state = str(row["state"])
            iq_name = f"{frequency}-{state.lower()}-r1.npz"
            if row.get("iq_file") != iq_name:
                raise MidpointError(f"midpoint run {run_id} observation {index} IQ name differs")
            iq_path = path.parent / iq_name
            if replay_raw:
                replay, peaks, rms = raw_analyzer._replay_transfer(iq_path)
                replay_phasor = complex(replay["real"], replay["imag"])
                delta = abs(replay_phasor - stored_phasor)
                if not np.isclose(replay_phasor, stored_phasor, rtol=1e-11, atol=1e-12):
                    raise MidpointError(
                        f"midpoint run {run_id} observation {index} fails raw replay"
                    )
                if not np.allclose(
                    peaks,
                    analysis["peak_component_counts"],
                    rtol=1e-7,
                    atol=1e-7,
                ) or not np.allclose(
                    rms,
                    analysis["rms_counts"],
                    rtol=1e-7,
                    atol=1e-7,
                ):
                    raise MidpointError(
                        f"midpoint run {run_id} observation {index} replay statistics differ"
                    )
                maximum_replay_delta = max(maximum_replay_delta, delta)
                replay_index += 1
                if replay_index % 50 == 0:
                    print(f"raw_replay_progress={replay_index}/1665", flush=True)
            transfer[(frequency, state)] = stored_phasor
            quality["pilot_phase_residual_deg"].append(
                math.degrees(
                    cohort._finite_number(pilot.get("phase_residual_rms_rad"), "pilot residual")
                )
            )
            quality["pilot_fit_standard_error_hz"].append(
                cohort._finite_number(pilot.get("fit_standard_error_hz"), "pilot fit SE")
            )
            quality["pilot_phase_step_coherence"].append(
                cohort._finite_number(pilot.get("phase_step_coherence"), "pilot coherence")
            )
            quality["peak_component_counts"].append(
                max(float(item) for item in analysis["peak_component_counts"])
            )
            artifact_manifest.append(
                {
                    "name": iq_name,
                    "size_bytes": iq_path.stat().st_size,
                    "sha256": cohort.sha256_path(iq_path),
                }
            )
        validated.append(
            {
                "run_id": run_id,
                "run_json_sha256": MIDPOINT_RUN_SHA256[run_id],
                "git_head": document["git_head"],
                "first_capture_started_utc": observations[0]["radio_readback"]["started_utc"],
                "last_capture_completed_utc": observations[-1]["radio_readback"]["completed_utc"],
                "artifact_manifest_sha256": _canonical_sha256(artifact_manifest),
                "raw_iq_bytes": sum(item["size_bytes"] for item in artifact_manifest),
                "maximum_raw_replay_absolute_delta": maximum_replay_delta,
                "transfer": transfer,
                "quality": quality,
            }
        )
    return validated


def _path_ratios(run: Mapping[str, Any], state: str, frequencies_hz: Sequence[int]) -> np.ndarray:
    transfer = run["transfer"]
    result: list[complex] = []
    for frequency in frequencies_hz:
        all_off = transfer[(frequency, "ALL_OFF")]
        path = transfer[(frequency, state)] - all_off
        reference = transfer[(frequency, "ANT8")] - all_off
        if abs(path) <= 1e-9 or abs(reference) <= 1e-9:
            raise MidpointError(f"{run['run_id']} {frequency} {state} derived path is too small")
        result.append(path / reference)
    return np.asarray(result, dtype=np.complex128)


def _complex_log(values: np.ndarray) -> np.ndarray:
    return np.log(np.abs(values)) + 1j * np.unwrap(np.angle(values))


def _harmonic_design(
    delta_frequency_ghz: np.ndarray, harmonics: int, delay_ns: float
) -> np.ndarray:
    count = delta_frequency_ghz.size
    design = np.zeros((2 * count, 3 + 2 * harmonics))
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


def fit_harmonic_predict(
    training_values: Sequence[complex],
    evaluation_frequencies_hz: Sequence[int],
    harmonics: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    training = np.asarray(training_values, dtype=np.complex128)
    training_frequency_ghz = np.asarray(cohort.FREQUENCIES_HZ) / 1e9
    center_ghz = float(np.mean(training_frequency_ghz))
    training_delta = training_frequency_ghz - center_ghz
    evaluation_delta = np.asarray(evaluation_frequencies_hz) / 1e9 - center_ghz
    measured_log = _complex_log(training)
    observation = np.concatenate((measured_log.real, measured_log.imag))
    delays = [0.0] if harmonics == 0 else cohort.RIPPLE_DELAY_GRID_NS
    best: tuple[float, float, np.ndarray] | None = None
    for delay_ns in delays:
        design = _harmonic_design(training_delta, harmonics, float(delay_ns))
        parameters, *_ = np.linalg.lstsq(design, observation, rcond=None)
        score = float(np.mean(np.square(observation - design @ parameters)))
        if best is None or score < best[0]:
            best = (score, float(delay_ns), parameters)
    if best is None:
        raise MidpointError("harmonic search failed")
    score, delay_ns, parameters = best
    evaluation_design = _harmonic_design(evaluation_delta, harmonics, delay_ns)
    fitted = evaluation_design @ parameters
    count = len(evaluation_frequencies_hz)
    prediction = np.exp(fitted[:count] + 1j * fitted[count:])
    return prediction, {
        "harmonics": harmonics,
        "parameter_count": 3 + 2 * harmonics + (1 if harmonics else 0),
        "ripple_delay_ns": delay_ns if harmonics else None,
        "training_joint_log_mean_square": score,
    }


def fit_exact_echo_predict(
    training_values: Sequence[complex], evaluation_frequencies_hz: Sequence[int]
) -> tuple[np.ndarray, dict[str, Any]]:
    training = np.asarray(training_values, dtype=np.complex128)
    training_frequency_ghz = np.asarray(cohort.FREQUENCIES_HZ) / 1e9
    center_ghz = float(np.mean(training_frequency_ghz))
    training_delta = training_frequency_ghz - center_ghz
    evaluation_delta = np.asarray(evaluation_frequencies_hz) / 1e9 - center_ghz
    path_grid = np.arange(-2.5, 2.5, 0.01)
    transformed = (
        training[np.newaxis, :]
        * np.exp(2j * np.pi * path_grid[:, np.newaxis] * training_delta[np.newaxis, :])
    ).T
    energy = float(np.sum(np.abs(training) ** 2))
    best: tuple[float, float, float, np.ndarray] | None = None
    for echo_delay_ns in np.arange(0.05, 2.5001, 0.01):
        design = np.column_stack(
            (
                np.ones(training.size),
                np.exp(-2j * np.pi * training_delta * echo_delay_ns),
            )
        )
        projected = design.conj().T @ transformed
        coefficients = np.linalg.solve(design.conj().T @ design, projected)
        scores = (
            energy - np.real(np.sum(np.conj(coefficients) * projected, axis=0))
        ) / training.size
        index = int(np.argmin(scores))
        candidate = (
            float(scores[index]),
            float(path_grid[index]),
            float(echo_delay_ns),
            coefficients[:, index],
        )
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        raise MidpointError("exact echo search failed")
    _, coarse_path, coarse_echo, _ = best
    for echo_delay_ns in np.arange(
        max(0.05, coarse_echo - 0.012), min(2.5, coarse_echo + 0.012) + 0.0001, 0.001
    ):
        for path_delay_ns in np.arange(coarse_path - 0.012, coarse_path + 0.0121, 0.001):
            design = np.column_stack(
                (
                    np.exp(-2j * np.pi * training_delta * path_delay_ns),
                    np.exp(-2j * np.pi * training_delta * (path_delay_ns + echo_delay_ns)),
                )
            )
            coefficients, *_ = np.linalg.lstsq(design, training, rcond=None)
            score = float(np.mean(np.abs(training - design @ coefficients) ** 2))
            if score < best[0]:
                best = (score, float(path_delay_ns), float(echo_delay_ns), coefficients)
    score, path_delay_ns, echo_delay_ns, coefficients = best
    prediction = coefficients[0] * np.exp(
        -2j * np.pi * evaluation_delta * path_delay_ns
    ) + coefficients[1] * np.exp(-2j * np.pi * evaluation_delta * (path_delay_ns + echo_delay_ns))
    return prediction, {
        "parameter_count": 6,
        "base_delay_ns": path_delay_ns,
        "echo_delay_ns": echo_delay_ns,
        "echo_relative_magnitude": float(abs(coefficients[1] / coefficients[0])),
        "training_complex_mean_square": score,
    }


def _chebyshev_predict(
    training_values: np.ndarray, evaluation_frequencies_hz: Sequence[int], degree: int
) -> np.ndarray:
    training_frequency = np.asarray(cohort.FREQUENCIES_HZ, dtype=np.float64)
    center = float(np.mean(training_frequency))
    scale = float(np.ptp(training_frequency) / 2.0)
    training_x = (training_frequency - center) / scale
    evaluation_x = (np.asarray(evaluation_frequencies_hz) - center) / scale
    measured_log = _complex_log(training_values)
    real = np.polynomial.chebyshev.chebfit(training_x, measured_log.real, degree)
    imag = np.polynomial.chebyshev.chebfit(training_x, measured_log.imag, degree)
    return np.exp(
        np.polynomial.chebyshev.chebval(evaluation_x, real)
        + 1j * np.polynomial.chebyshev.chebval(evaluation_x, imag)
    )


def _linear_basis(values: np.ndarray, knot_count: int) -> np.ndarray:
    knots = np.linspace(0.0, 1.0, knot_count)
    basis = np.zeros((values.size, knot_count))
    for row, value in enumerate(values):
        if value <= 0.0:
            basis[row, 0] = 1.0
        elif value >= 1.0:
            basis[row, -1] = 1.0
        else:
            left = int(np.searchsorted(knots, value) - 1)
            fraction = (value - knots[left]) / (knots[left + 1] - knots[left])
            basis[row, left] = 1.0 - fraction
            basis[row, left + 1] = fraction
    return basis


def _linear_knot_predict(
    training_values: np.ndarray, evaluation_frequencies_hz: Sequence[int], knot_count: int
) -> np.ndarray:
    training_frequency = np.asarray(cohort.FREQUENCIES_HZ, dtype=np.float64)
    minimum = float(np.min(training_frequency))
    span = float(np.ptp(training_frequency))
    training_basis = _linear_basis((training_frequency - minimum) / span, knot_count)
    evaluation_basis = _linear_basis(
        (np.asarray(evaluation_frequencies_hz) - minimum) / span, knot_count
    )
    measured_log = _complex_log(training_values)
    real, *_ = np.linalg.lstsq(training_basis, measured_log.real, rcond=None)
    imag, *_ = np.linalg.lstsq(training_basis, measured_log.imag, rcond=None)
    return np.exp(evaluation_basis @ real + 1j * evaluation_basis @ imag)


def _score_predictions(
    predictions: Mapping[str, np.ndarray], midpoint: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    all_phase: list[float] = []
    all_gain: list[float] = []
    per_path: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for state in cohort.MODEL_STATES:
        path_phase: list[float] = []
        path_gain: list[float] = []
        prediction = predictions[state]
        for run in midpoint:
            measured = _path_ratios(run, state, MIDPOINT_FREQUENCIES_HZ)
            phase = np.degrees(np.angle(measured * np.conj(prediction)))
            gain = 20.0 * np.log10(np.abs(measured) / np.abs(prediction))
            path_phase.extend(phase.tolist())
            path_gain.extend(gain.tolist())
            for index, frequency in enumerate(MIDPOINT_FREQUENCIES_HZ):
                cells.append(
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
        "cells": cells,
    }


def _midpoint_repeatability(midpoint: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    span_cells: list[dict[str, Any]] = []
    loo_phase: list[float] = []
    loo_gain: list[float] = []
    loo_per_path: list[dict[str, Any]] = []
    for state in cohort.MODEL_STATES:
        ratios = [_path_ratios(run, state, MIDPOINT_FREQUENCIES_HZ) for run in midpoint]
        path_phase: list[float] = []
        path_gain: list[float] = []
        for index, frequency in enumerate(MIDPOINT_FREQUENCIES_HZ):
            coefficients = 1.0 / np.asarray([values[index] for values in ratios])
            magnitude_db = 20.0 * np.log10(np.abs(coefficients))
            mean_phase = np.angle(np.mean(coefficients))
            phase_offsets = np.degrees(np.angle(coefficients * np.exp(-1j * mean_phase)))
            span_cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "magnitude_span_db": float(np.ptp(magnitude_db)),
                    "phase_span_deg": float(np.ptp(phase_offsets)),
                }
            )
        for holdout, measured in enumerate(ratios):
            coefficient = np.mean(
                [1.0 / ratios[index] for index in range(len(ratios)) if index != holdout],
                axis=0,
            )
            corrected = coefficient * measured
            phase = np.degrees(np.angle(corrected))
            gain = 20.0 * np.log10(np.abs(corrected))
            path_phase.extend(phase.tolist())
            path_gain.extend(gain.tolist())
        loo_phase.extend(path_phase)
        loo_gain.extend(path_gain)
        loo_per_path.append(
            {
                "state": state,
                "phase_rms_deg": float(np.sqrt(np.mean(np.square(path_phase)))),
                "gain_rms_db": float(np.sqrt(np.mean(np.square(path_gain)))),
            }
        )
    return {
        "span_cells": span_cells,
        "magnitude_span_db": cohort._summary([row["magnitude_span_db"] for row in span_cells]),
        "phase_span_deg": cohort._summary([row["phase_span_deg"] for row in span_cells]),
        "same_frequency_leave_one_sweep_out": {
            "phase_rms_deg": float(np.sqrt(np.mean(np.square(loo_phase)))),
            "gain_rms_db": float(np.sqrt(np.mean(np.square(loo_gain)))),
            "maximum_absolute_phase_error_deg": float(np.max(np.abs(loo_phase))),
            "maximum_absolute_gain_error_db": float(np.max(np.abs(loo_gain))),
            "per_path": loo_per_path,
        },
    }


def _serialize_predictions(predictions: Mapping[str, np.ndarray]) -> list[dict[str, Any]]:
    return [
        {"state": state, "real": value.real.tolist(), "imag": value.imag.tolist()}
        for state, value in predictions.items()
    ]


def analyze_campaign(
    original_paths: Sequence[Path],
    midpoint_paths: Sequence[Path],
    *,
    replay_raw: bool,
) -> dict[str, Any]:
    original = cohort._validate_group(
        original_paths,
        cohort.ORIGINAL_RUN_SHA256,
        cohort.ORIGINAL_SOURCE_COMMIT,
        "original cohort",
    )
    midpoint = _validate_midpoint_group(midpoint_paths, replay_raw=replay_raw)
    training_mean = {
        state: np.mean([cohort._path_ratios(run, state) for run in original], axis=0)
        for state in cohort.MODEL_STATES
    }
    models: list[dict[str, Any]] = []
    all_predictions: dict[str, dict[str, np.ndarray]] = {}

    for harmonics in range(5):
        predictions: dict[str, np.ndarray] = {}
        fits: list[dict[str, Any]] = []
        for state in cohort.MODEL_STATES:
            prediction, fit = fit_harmonic_predict(
                training_mean[state], MIDPOINT_FREQUENCIES_HZ, harmonics
            )
            predictions[state] = prediction
            fits.append({"state": state, **fit})
        model_id = f"log_harmonic_k{harmonics}"
        all_predictions[model_id] = predictions
        models.append(
            {
                "model_id": model_id,
                "label": "delay only"
                if harmonics == 0
                else f"{harmonics} log-ripple harmonic{'s' if harmonics != 1 else ''}",
                "parameter_count_per_path": fits[0]["parameter_count"],
                "midpoint_score": _score_predictions(predictions, midpoint),
                "path_fits": fits,
            }
        )

    exact_predictions: dict[str, np.ndarray] = {}
    exact_fits: list[dict[str, Any]] = []
    for state in cohort.MODEL_STATES:
        prediction, fit = fit_exact_echo_predict(training_mean[state], MIDPOINT_FREQUENCIES_HZ)
        exact_predictions[state] = prediction
        exact_fits.append({"state": state, **fit})
    all_predictions["exact_single_echo"] = exact_predictions
    models.append(
        {
            "model_id": "exact_single_echo",
            "label": "exact single echo",
            "parameter_count_per_path": 6,
            "midpoint_score": _score_predictions(exact_predictions, midpoint),
            "path_fits": exact_fits,
        }
    )

    for degree in (5, 9, 15):
        predictions = {
            state: _chebyshev_predict(training_mean[state], MIDPOINT_FREQUENCIES_HZ, degree)
            for state in cohort.MODEL_STATES
        }
        model_id = f"chebyshev_log_degree_{degree}"
        all_predictions[model_id] = predictions
        models.append(
            {
                "model_id": model_id,
                "label": f"log-Chebyshev degree {degree}",
                "parameter_count_per_path": 2 * (degree + 1),
                "midpoint_score": _score_predictions(predictions, midpoint),
            }
        )

    for knot_count in (10, 20, 26):
        predictions = {
            state: _linear_knot_predict(training_mean[state], MIDPOINT_FREQUENCIES_HZ, knot_count)
            for state in cohort.MODEL_STATES
        }
        model_id = f"linear_log_{knot_count}_knots"
        all_predictions[model_id] = predictions
        models.append(
            {
                "model_id": model_id,
                "label": f"piecewise-linear log, {knot_count} knots",
                "parameter_count_per_path": 2 * knot_count,
                "midpoint_score": _score_predictions(predictions, midpoint),
            }
        )

    table_knots = {
        state: 1.0 / np.mean([1.0 / cohort._path_ratios(run, state) for run in original], axis=0)
        for state in cohort.MODEL_STATES
    }
    original_frequency = np.asarray(cohort.FREQUENCIES_HZ, dtype=np.float64)
    midpoint_frequency = np.asarray(MIDPOINT_FREQUENCIES_HZ, dtype=np.float64)
    for mode in ("log_linear", "cartesian_linear"):
        predictions: dict[str, np.ndarray] = {}
        for state, knots in table_knots.items():
            if mode == "log_linear":
                logged = _complex_log(knots)
                predictions[state] = np.exp(
                    np.interp(midpoint_frequency, original_frequency, logged.real)
                    + 1j * np.interp(midpoint_frequency, original_frequency, logged.imag)
                )
            else:
                predictions[state] = np.interp(
                    midpoint_frequency, original_frequency, knots.real
                ) + 1j * np.interp(midpoint_frequency, original_frequency, knots.imag)
        model_id = f"frequency_table_{mode}"
        all_predictions[model_id] = predictions
        models.append(
            {
                "model_id": model_id,
                "label": f"100 MHz table, {mode.replace('_', '-')} interpolation",
                "parameter_count_per_path": 2 * len(cohort.FREQUENCIES_HZ),
                "midpoint_score": _score_predictions(predictions, midpoint),
            }
        )

    quality_samples: dict[str, list[float]] = {}
    for run in midpoint:
        for metric, values in run["quality"].items():
            quality_samples.setdefault(metric, []).extend(values)
    model_predictions = {
        model_id: _serialize_predictions(predictions)
        for model_id, predictions in all_predictions.items()
    }
    measured_midpoints = [
        {
            "state": state,
            "runs": [
                {
                    "run_id": run["run_id"],
                    "real": values.real.tolist(),
                    "imag": values.imag.tolist(),
                }
                for run in midpoint
                for values in [_path_ratios(run, state, MIDPOINT_FREQUENCIES_HZ)]
            ],
            "training_knots_real": training_mean[state].real.tolist(),
            "training_knots_imag": training_mean[state].imag.tolist(),
        }
        for state in cohort.MODEL_STATES
    ]
    return {
        "schema": 1,
        "evidence_kind": "smateway.broadband-midpoint-campaign/v1",
        "campaign_id": "external-broadband-midpoints-2g15-5g75-20260831",
        "cohort_contract": {
            "training_role": "exact original three 100 MHz sweeps; fit only",
            "evaluation_role": "exact five new 50 MHz midpoint sweeps; score only",
            "training_runs": [
                {
                    key: value
                    for key, value in run.items()
                    if key not in {"path", "transfer", "quality"}
                }
                for run in original
            ],
            "midpoint_runs": [
                {key: value for key, value in run.items() if key not in {"transfer", "quality"}}
                for run in midpoint
            ],
        },
        "method": {
            "training_frequencies_hz": list(cohort.FREQUENCIES_HZ),
            "evaluation_frequencies_hz": list(MIDPOINT_FREQUENCIES_HZ),
            "evaluation_frequency_definition": (
                "the 37 unseen 50 MHz midpoints between adjacent 100 MHz training knots"
            ),
            "path_ratio": "R_i(f) = (H_i-H_ALL_OFF)/(H_ANT8-H_ALL_OFF)",
            "model_freeze": (
                "all parameters and table knots use only the original three sweeps; "
                "midpoint sweeps are never used for fitting"
            ),
            "raw_replay_performed": replay_raw,
        },
        "summary": {
            "midpoint_sweep_count": len(midpoint),
            "capture_count": sum(333 for _ in midpoint),
            "analysis_error_count": 0,
            "raw_iq_bytes": sum(run["raw_iq_bytes"] for run in midpoint),
            "maximum_raw_replay_absolute_delta": max(
                run["maximum_raw_replay_absolute_delta"] for run in midpoint
            ),
            "maximum_peak_component_counts": max(
                max(run["quality"]["peak_component_counts"]) for run in midpoint
            ),
            "final_safety_passed": True,
        },
        "quality": {metric: cohort._summary(values) for metric, values in quality_samples.items()},
        "midpoint_repeatability": _midpoint_repeatability(midpoint),
        "models": models,
        "model_predictions": model_predictions,
        "measured_midpoints": measured_midpoints,
    }


def _model(result: Mapping[str, Any], model_id: str) -> Mapping[str, Any]:
    return next(item for item in result["models"] if item["model_id"] == model_id)


def _prediction(result: Mapping[str, Any], model_id: str, state: str) -> np.ndarray:
    row = next(item for item in result["model_predictions"][model_id] if item["state"] == state)
    return np.asarray(row["real"]) + 1j * np.asarray(row["imag"])


def _measured(result: Mapping[str, Any], state: str) -> tuple[np.ndarray, np.ndarray]:
    row = next(item for item in result["measured_midpoints"] if item["state"] == state)
    runs = np.asarray(
        [np.asarray(run["real"]) + 1j * np.asarray(run["imag"]) for run in row["runs"]]
    )
    knots = np.asarray(row["training_knots_real"]) + 1j * np.asarray(row["training_knots_imag"])
    return runs, knots


def render_figures(result: Mapping[str, Any], figure_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 120, "savefig.dpi": 180, "font.size": 10})
    display_ids = [
        "log_harmonic_k0",
        "log_harmonic_k1",
        "exact_single_echo",
        "log_harmonic_k2",
        "log_harmonic_k3",
        "log_harmonic_k4",
        "chebyshev_log_degree_9",
        "linear_log_10_knots",
        "linear_log_20_knots",
        "linear_log_26_knots",
        "frequency_table_log_linear",
        "frequency_table_cartesian_linear",
    ]
    selected = [_model(result, model_id) for model_id in display_ids]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), constrained_layout=True)
    for axis, key, ylabel in (
        (axes[0], "phase_rms_deg", "Unseen-midpoint phase RMS (deg)"),
        (axes[1], "gain_rms_db", "Unseen-midpoint gain RMS (dB)"),
    ):
        for item in selected:
            axis.scatter(item["parameter_count_per_path"], item["midpoint_score"][key], s=55)
            label = (
                item["model_id"]
                .replace("log_harmonic_k", "K")
                .replace("exact_single_echo", "exact echo")
                .replace("chebyshev_log_degree_", "Cheb")
                .replace("linear_log_", "linear ")
                .replace("_knots", "k")
                .replace("frequency_table_log_linear", "table log")
                .replace("frequency_table_cartesian_linear", "table IQ")
            )
            annotation_offset = {
                "log_harmonic_k3": (4, 8),
                "log_harmonic_k4": (4, -12),
                "chebyshev_log_degree_9": (4, -12),
                "frequency_table_cartesian_linear": (4, -12),
            }.get(item["model_id"], (4, 4))
            axis.annotate(
                label,
                (item["parameter_count_per_path"], item["midpoint_score"][key]),
                xytext=annotation_offset,
                textcoords="offset points",
                fontsize=8,
            )
        floor = result["midpoint_repeatability"]["same_frequency_leave_one_sweep_out"][key]
        axis.axhline(floor, color="black", linestyle=":", label="same-frequency LOO floor")
        axis.set_xlabel("Real training parameters per path")
        axis.set_ylabel(ylabel)
        axis.grid(alpha=0.25)
        axis.legend()
    fig.suptitle("Unseen 50 MHz midpoints expose interpolation error")
    fig.savefig(figure_dir / FIGURE_FILENAMES[0])
    plt.close(fig)

    comparison_ids = [
        "log_harmonic_k1",
        "log_harmonic_k3",
        "log_harmonic_k4",
        "chebyshev_log_degree_9",
        "frequency_table_log_linear",
    ]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    x = np.arange(len(cohort.MODEL_STATES))
    width = 0.16
    for model_index, model_id in enumerate(comparison_ids):
        item = _model(result, model_id)
        by_path = {row["state"]: row for row in item["midpoint_score"]["per_path"]}
        offset = (model_index - 2.0) * width
        axes[0].bar(
            x + offset,
            [by_path[state]["phase_rms_deg"] for state in cohort.MODEL_STATES],
            width,
            label=item["label"],
        )
        axes[1].bar(
            x + offset,
            [by_path[state]["gain_rms_db"] for state in cohort.MODEL_STATES],
            width,
            label=item["label"],
        )
    axes[0].set_ylabel("Midpoint phase RMS (deg)")
    axes[1].set_ylabel("Midpoint gain RMS (dB)")
    for axis in axes:
        axis.set_xticks(x, cohort.MODEL_STATES)
        axis.grid(axis="y", alpha=0.25)
        axis.legend(ncols=3, fontsize=8)
    fig.suptitle("Midpoint interpolation error by path")
    fig.savefig(figure_dir / FIGURE_FILENAMES[1])
    plt.close(fig)

    heatmap_ids = (
        "log_harmonic_k1",
        "log_harmonic_k3",
        "log_harmonic_k4",
        "frequency_table_log_linear",
    )
    midpoint_ghz = np.asarray(MIDPOINT_FREQUENCIES_HZ) / 1e9
    for metric, filename, title in (
        ("phase_error_deg", FIGURE_FILENAMES[2], "Phase RMS across five sweeps (deg)"),
        ("gain_error_db", FIGURE_FILENAMES[3], "Gain RMS across five sweeps (dB)"),
    ):
        fig, axes = plt.subplots(2, 2, figsize=(14, 7.5), constrained_layout=True)
        matrices: list[np.ndarray] = []
        for model_id in heatmap_ids:
            cells = _model(result, model_id)["midpoint_score"]["cells"]
            matrix = np.asarray(
                [
                    [
                        math.sqrt(
                            np.mean(
                                [
                                    row[metric] ** 2
                                    for row in cells
                                    if row["state"] == state and row["frequency_hz"] == frequency
                                ]
                            )
                        )
                        for frequency in MIDPOINT_FREQUENCIES_HZ
                    ]
                    for state in cohort.MODEL_STATES
                ]
            )
            matrices.append(matrix)
        maximum = max(float(np.max(matrix)) for matrix in matrices)
        for axis, model_id, matrix in zip(axes.flat, heatmap_ids, matrices, strict=True):
            image = axis.imshow(
                matrix,
                aspect="auto",
                cmap="magma",
                vmin=0.0,
                vmax=maximum,
                extent=(2.1, 5.8, len(cohort.MODEL_STATES) - 0.5, -0.5),
            )
            axis.set_yticks(np.arange(len(cohort.MODEL_STATES)), cohort.MODEL_STATES)
            axis.set_xlabel("RF frequency (GHz)")
            axis.set_title(_model(result, model_id)["label"])
            fig.colorbar(image, ax=axis, pad=0.01)
        fig.suptitle(title)
        fig.savefig(figure_dir / filename)
        plt.close(fig)

    overlay_models = (
        ("log_harmonic_k3", "#E45756", "K3"),
        ("log_harmonic_k4", "#B279A2", "K4"),
        ("chebyshev_log_degree_9", "#F58518", "Cheb9"),
        ("frequency_table_log_linear", "#54A24B", "table log-linear"),
    )
    training_ghz = np.asarray(cohort.FREQUENCIES_HZ) / 1e9
    for quantity, filename, title in (
        ("phase", FIGURE_FILENAMES[4], "Phase at original knots and unseen midpoints"),
        ("gain", FIGURE_FILENAMES[5], "Gain at original knots and unseen midpoints"),
    ):
        fig, axes = plt.subplots(4, 2, figsize=(14, 14), constrained_layout=True, sharex=True)
        for axis, state in zip(axes.flat, cohort.MODEL_STATES, strict=False):
            measured_runs, training_knots = _measured(result, state)
            if quantity == "phase":
                training_values = np.degrees(np.unwrap(np.angle(training_knots)))
                midpoint_values = []
                for values in measured_runs:
                    phase = np.degrees(np.unwrap(np.angle(values)))
                    phase += 360.0 * round(
                        float(
                            np.median(
                                np.interp(midpoint_ghz, training_ghz, training_values) - phase
                            )
                        )
                        / 360.0
                    )
                    midpoint_values.append(phase)
                midpoint_values = np.asarray(midpoint_values)
                ylabel = "Unwrapped phase (deg)"
            else:
                training_values = 20.0 * np.log10(np.abs(training_knots))
                midpoint_values = 20.0 * np.log10(np.abs(measured_runs))
                ylabel = "Relative path gain (dB)"
            axis.scatter(training_ghz, training_values, color="black", s=12, label="original knots")
            axis.fill_between(
                midpoint_ghz,
                np.min(midpoint_values, axis=0),
                np.max(midpoint_values, axis=0),
                color="#4C78A8",
                alpha=0.18,
                label="midpoint five-run range",
            )
            axis.scatter(
                midpoint_ghz,
                np.mean(midpoint_values, axis=0),
                color="#4C78A8",
                s=12,
                label="midpoint mean",
            )
            for model_id, color, label in overlay_models:
                prediction = _prediction(result, model_id, state)
                if quantity == "phase":
                    values = np.degrees(np.unwrap(np.angle(prediction)))
                    values += 360.0 * round(
                        float(
                            np.median(
                                np.interp(midpoint_ghz, training_ghz, training_values) - values
                            )
                        )
                        / 360.0
                    )
                else:
                    values = 20.0 * np.log10(np.abs(prediction))
                axis.plot(midpoint_ghz, values, color=color, linewidth=1.2, label=label)
            axis.set_title(state + " vs ANT8")
            axis.set_ylabel(ylabel)
            axis.grid(alpha=0.25)
        axes.flat[-1].axis("off")
        handles, labels = axes.flat[0].get_legend_handles_labels()
        axes.flat[-1].legend(handles, labels, loc="center", frameon=False)
        for axis in axes[-1, :]:
            if axis.axison:
                axis.set_xlabel("RF frequency (GHz)")
        fig.suptitle(title)
        fig.savefig(figure_dir / filename)
        plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = analyze_campaign(
        args.original_run,
        args.midpoint_run,
        replay_raw=not args.skip_raw_replay,
    )
    render_figures(result, args.figure_dir)
    actual_names = {path.name for path in args.figure_dir.glob("*.png")}
    if actual_names != set(FIGURE_FILENAMES):
        raise MidpointError("figure directory contains a stale or missing PNG")
    result["figures"] = [
        {
            "filename": name,
            "size_bytes": (args.figure_dir / name).stat().st_size,
            "sha256": cohort.sha256_path(args.figure_dir / name),
        }
        for name in FIGURE_FILENAMES
    ]
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
