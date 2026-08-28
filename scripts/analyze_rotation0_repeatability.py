#!/usr/bin/env python3
"""Analyze repeated rotation-0 broadband sweeps and render qualification figures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

FREQUENCIES_HZ = tuple(range(2_100_000_000, 5_800_000_001, 100_000_000))
ANTENNAS = tuple(f"ANT{index}" for index in range(1, 9))
REFERENCE_ANTENNA = "ANT8"
HIGH_BAND_MIN_HZ = 3_600_000_000
HIGH_BAND_MAX_HZ = 5_800_000_000
EXPECTED_MAPPING = {f"F{index}": f"ANT{index}" for index in range(1, 9)}
SPEED_OF_LIGHT_MM_PER_PS = 0.299792458
MINIMUM_OPERATIONAL_RAW_CONTRAST_DB = 20.0
PRECISION_PHASE_TARGET_DEG = 1.0
MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB = 20.0 * math.log10(
    1.0 / math.sin(math.radians(PRECISION_PHASE_TARGET_DEG))
)
DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR = 0.85
MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE = 0.95


class RepeatabilityAnalysisError(RuntimeError):
    """The supplied manifests cannot support a traceable repeatability result."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument(
        "--repeat-manifest",
        type=Path,
        action="append",
        required=True,
        help="Rotation-0 repeat manifest; pass at least twice",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figure-directory", type=Path)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RepeatabilityAnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise RepeatabilityAnalysisError(f"{label} must be an array")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepeatabilityAnalysisError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RepeatabilityAnalysisError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RepeatabilityAnalysisError(f"{label} must be finite")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RepeatabilityAnalysisError(f"{label} must be a nonempty string")
    return value


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RepeatabilityAnalysisError(f"cannot load {label} {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise RepeatabilityAnalysisError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _wrap_phase_deg(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _circular_mean_deg(values: Sequence[float]) -> float:
    angles = np.deg2rad(np.asarray(values, dtype=np.float64))
    phasor = np.mean(np.exp(1j * angles))
    return _wrap_phase_deg(float(np.rad2deg(np.angle(phasor))))


def _circular_std_deg(values: Sequence[float]) -> float:
    angles = np.deg2rad(np.asarray(values, dtype=np.float64))
    resultant = min(1.0, max(1e-15, float(abs(np.mean(np.exp(1j * angles))))))
    return float(np.rad2deg(math.sqrt(max(0.0, -2.0 * math.log(resultant)))))


def _sample_std(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def _load_run(label: str, manifest_path: Path) -> dict[str, Any]:
    manifest = _read_json(manifest_path, f"{label} manifest")
    if manifest.get("schema") != 1:
        raise RepeatabilityAnalysisError(f"{label} manifest schema must be 1")
    if manifest.get("experiment_kind") != "fast20_fully_conducted_broadband_board_calibration":
        raise RepeatabilityAnalysisError(f"{label} is not a broadband conducted sweep")
    configuration = _mapping(manifest.get("configuration"), f"{label} configuration")
    if tuple(_sequence(configuration.get("frequencies_hz"), "frequency grid")) != FREQUENCIES_HZ:
        raise RepeatabilityAnalysisError(f"{label} does not use the exact 2.1-5.8 GHz grid")
    if configuration.get("storage_medium") != "raspberry_pi_local_filesystem":
        raise RepeatabilityAnalysisError(f"{label} does not attest local Raspberry Pi storage")
    if configuration.get("pluto_onboard_storage_used") is not False:
        raise RepeatabilityAnalysisError(f"{label} does not exclude Pluto onboard storage")
    final_mute = _mapping(manifest.get("final_mute"), f"{label} final mute")
    if final_mute.get("status") != "passed":
        raise RepeatabilityAnalysisError(f"{label} final mute did not pass")

    raw_attempts = _sequence(manifest.get("attempts"), f"{label} attempts")
    rotation0_attempts = []
    failed_attempts = []
    attempts_by_frequency: dict[int, Mapping[str, Any]] = {}
    for raw_attempt in raw_attempts:
        attempt = _mapping(raw_attempt, f"{label} attempt")
        if attempt.get("stage") != "rotation0":
            continue
        rotation0_attempts.append(attempt)
        status = attempt.get("status")
        if status not in {"complete", "failed"}:
            raise RepeatabilityAnalysisError(f"{label} has an unfinished rotation-0 attempt")
        post_mute = _mapping(attempt.get("post_mute"), "post mute")
        if post_mute.get("status") != "passed":
            raise RepeatabilityAnalysisError(f"{label} has a rotation-0 attempt without mute")
        if status == "failed":
            capture = _mapping(attempt.get("capture"), "failed capture")
            stderr = str(capture.get("stderr", ""))
            failed_attempts.append(
                {
                    "attempt_id": _integer(attempt.get("attempt_id"), "attempt ID"),
                    "center_frequency_hz": _integer(
                        attempt.get("center_frequency_hz"), "failed attempt frequency"
                    ),
                    "retry": _integer(attempt.get("retry"), "retry count"),
                    "return_code": capture.get("return_code"),
                    "artifact_id": attempt.get("artifact_id"),
                    "post_mute_status": "passed",
                    "error": str(attempt.get("error", "")),
                    "failure_kind": (
                        "libiio_buffer_refill_enodata"
                        if "Errno 61" in stderr and "No data available" in stderr
                        else "other_execution_failure"
                    ),
                    "stderr_tail": stderr.splitlines()[-1] if stderr else None,
                }
            )
            continue
        frequency_hz = _integer(attempt.get("center_frequency_hz"), "attempt frequency")
        if frequency_hz in attempts_by_frequency:
            raise RepeatabilityAnalysisError(f"{label} duplicates {frequency_hz} Hz")
        if attempt.get("mapping") != EXPECTED_MAPPING:
            raise RepeatabilityAnalysisError(f"{label} {frequency_hz} Hz mapping is not rotation 0")
        attempts_by_frequency[frequency_hz] = attempt
    if set(attempts_by_frequency) != set(FREQUENCIES_HZ):
        missing = sorted(set(FREQUENCIES_HZ) - set(attempts_by_frequency))
        raise RepeatabilityAnalysisError(f"{label} lacks complete rotation-0 captures: {missing}")

    observations: dict[int, dict[str, Any]] = {}
    source_analyses = []
    for frequency_hz in FREQUENCIES_HZ:
        attempt = attempts_by_frequency[frequency_hz]
        quality = _mapping(attempt.get("quality_result"), "quality result")
        analysis_path = Path(_string(quality.get("analysis_path"), "analysis path"))
        analysis = _read_json(analysis_path, "reference-transfer analysis")
        if analysis.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer":
            raise RepeatabilityAnalysisError(f"{analysis_path} has an unsupported analysis kind")
        aggregation = _mapping(analysis.get("aggregation_key"), "aggregation key")
        if _integer(aggregation.get("center_frequency_hz"), "analysis frequency") != frequency_hz:
            raise RepeatabilityAnalysisError(f"{analysis_path} frequency differs from manifest")
        artifact_id = _string(attempt.get("artifact_id"), "artifact ID")
        artifact = _mapping(analysis.get("artifact"), "analysis artifact")
        if artifact.get("artifact_id") != artifact_id:
            raise RepeatabilityAnalysisError(f"{analysis_path} artifact identity differs")
        transfer = _mapping(analysis.get("transfer"), "transfer")
        capture = _mapping(analysis.get("capture"), "capture")
        if transfer.get("continuity_verified") is not True:
            raise RepeatabilityAnalysisError(f"{analysis_path} continuity is not verified")
        headroom = _mapping(capture.get("adc_headroom_admission"), "headroom admission")
        if headroom.get("passed") is not True:
            raise RepeatabilityAnalysisError(f"{analysis_path} ADC headroom did not pass")
        all_off = _mapping(transfer.get("all_off"), "ALL_OFF transfer")
        all_off_raw = _mapping(all_off.get("raw_rx2_over_rx1"), "ALL_OFF raw transfer")
        all_off_raw_amplitude = _number(all_off_raw.get("amplitude"), "ALL_OFF raw amplitude")
        if all_off_raw_amplitude <= 0.0:
            raise RepeatabilityAnalysisError(
                f"{analysis_path} ALL_OFF raw amplitude must be positive"
            )
        states: dict[str, Any] = {}
        for raw_state in _sequence(transfer.get("states"), "transfer states"):
            state = _mapping(raw_state, "transfer state")
            antenna = _string(state.get("name"), "state name")
            if antenna in states:
                raise RepeatabilityAnalysisError(f"{analysis_path} duplicates {antenna}")
            subtracted = _mapping(
                state.get("all_off_subtracted_rx2_over_rx1"), "subtracted transfer"
            )
            raw_transfer = _mapping(state.get("raw_rx2_over_rx1"), "raw transfer")
            raw_amplitude = _number(raw_transfer.get("amplitude"), "raw transfer amplitude")
            if raw_amplitude <= 0.0:
                raise RepeatabilityAnalysisError(
                    f"{analysis_path} {antenna} raw amplitude must be positive"
                )
            states[antenna] = {
                "quality_passed": state.get("quality_passed") is True,
                "quality_rejection_reasons": list(
                    _sequence(state.get("quality_rejection_reasons"), "rejection reasons")
                ),
                "phase_deg": _number(subtracted.get("phase_deg"), "transfer phase"),
                "amplitude": _number(subtracted.get("amplitude"), "transfer amplitude"),
                "cycle_phase_std_deg": _number(
                    subtracted.get("cycle_phase_std_deg"), "cycle phase standard deviation"
                ),
                "cycle_coherence": _number(subtracted.get("cycle_coherence"), "cycle coherence"),
                "transfer_detection_snr_db": _number(
                    state.get("transfer_detection_snr_db"), "transfer SNR"
                ),
                "raw_selected_to_all_off_contrast_db": 20.0
                * math.log10(raw_amplitude / all_off_raw_amplitude),
            }
        if set(states) != set(ANTENNAS):
            raise RepeatabilityAnalysisError(f"{analysis_path} does not contain ANT1-ANT8")
        observations[frequency_hz] = {
            "artifact_id": artifact_id,
            "artifact_sha256": _string(artifact.get("sha256"), "artifact SHA-256"),
            "analysis_path": str(analysis_path),
            "analysis_sha256": _sha256(analysis_path),
            "complete_cycle_count": _integer(
                transfer.get("complete_cycle_count"), "complete cycle count"
            ),
            "reference_valid_bin_fraction": _number(
                transfer.get("reference_valid_bin_fraction"), "reference-valid fraction"
            ),
            "alignment_score": _number(transfer.get("alignment_score"), "alignment score"),
            "alignment_even_odd_agreement": _number(
                transfer.get("alignment_even_odd_agreement"),
                "alignment even/odd agreement",
            ),
            "marker_phase_ms": _number(transfer.get("marker_phase_ms"), "marker phase"),
            "states": states,
        }
        source_analyses.append(
            {
                "center_frequency_hz": frequency_hz,
                "artifact_id": artifact_id,
                "artifact_sha256": observations[frequency_hz]["artifact_sha256"],
                "analysis_sha256": observations[frequency_hz]["analysis_sha256"],
            }
        )

    return {
        "label": label,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha256(manifest_path),
        "run_id": _string(manifest.get("run_id"), "run ID"),
        "board_id": _string(configuration.get("board_id"), "board ID"),
        "pluto_serial": _string(configuration.get("serial"), "Pluto serial"),
        "profile_id": _string(configuration.get("profile_id"), "profile ID"),
        "profile_contract_sha256": _string(
            configuration.get("profile_contract_sha256"), "profile contract SHA-256"
        ),
        "firmware_binary_sha256": _string(
            configuration.get("firmware_binary_sha256"), "firmware SHA-256"
        ),
        "final_mute": dict(final_mute),
        "execution_attempt_count": len(rotation0_attempts),
        "failed_attempts": failed_attempts,
        "source_analyses": source_analyses,
        "observations": observations,
    }


def _validate_common_identity(runs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    fields = (
        "board_id",
        "pluto_serial",
        "profile_id",
        "profile_contract_sha256",
        "firmware_binary_sha256",
    )
    identity = {field: _string(runs[0].get(field), field) for field in fields}
    for run in runs[1:]:
        for field, expected in identity.items():
            if run.get(field) != expected:
                raise RepeatabilityAnalysisError(f"source runs differ in {field}")
    return identity


def _relative_measurement(states: Mapping[str, Any], antenna: str) -> tuple[float, float]:
    selected = _mapping(states.get(antenna), antenna)
    reference = _mapping(states.get(REFERENCE_ANTENNA), REFERENCE_ANTENNA)
    phase = _wrap_phase_deg(
        _number(selected.get("phase_deg"), "selected phase")
        - _number(reference.get("phase_deg"), "reference phase")
    )
    amplitude_ratio_db = 20.0 * math.log10(
        _number(selected.get("amplitude"), "selected amplitude")
        / _number(reference.get("amplitude"), "reference amplitude")
    )
    return phase, amplitude_ratio_db


def _delay_fit(repeat_runs: Sequence[Mapping[str, Any]], antenna: str) -> dict[str, Any]:
    fits = []
    for run in repeat_runs:
        frequencies = np.asarray(
            [
                frequency_hz
                for frequency_hz in FREQUENCIES_HZ
                if HIGH_BAND_MIN_HZ <= frequency_hz <= HIGH_BAND_MAX_HZ
            ],
            dtype=np.float64,
        )
        phases = []
        observations = _mapping(run.get("observations"), "observations")
        for frequency_hz in frequencies.astype(np.int64):
            row = _mapping(observations.get(int(frequency_hz)), "frequency observation")
            phase, _ = _relative_measurement(
                _mapping(row.get("states"), "frequency states"), antenna
            )
            phases.append(phase)
        unwrapped = np.unwrap(np.deg2rad(np.asarray(phases, dtype=np.float64)))
        centered = frequencies - float(np.mean(frequencies))
        design = np.column_stack((np.ones(frequencies.size), centered))
        intercept, slope = np.linalg.lstsq(design, unwrapped, rcond=None)[0]
        prediction = design @ np.asarray((intercept, slope))
        residual_deg = np.rad2deg(unwrapped - prediction)
        delay_ps = float(-slope / (2.0 * math.pi) * 1e12)
        fits.append(
            {
                "run_label": run["label"],
                "measured_relative_delay_ps": delay_ps,
                "equivalent_free_space_path_mm": delay_ps * SPEED_OF_LIGHT_MM_PER_PS,
                "phase_residual_rms_deg": float(math.sqrt(float(np.mean(np.square(residual_deg))))),
                "phase_residual_max_abs_deg": float(np.max(np.abs(residual_deg))),
            }
        )
    delays = [row["measured_relative_delay_ps"] for row in fits]
    residuals = [row["phase_residual_rms_deg"] for row in fits]
    return {
        "antenna": antenna,
        "reference_antenna": REFERENCE_ANTENNA,
        "frequency_min_hz": HIGH_BAND_MIN_HZ,
        "frequency_max_hz": HIGH_BAND_MAX_HZ,
        "mean_relative_delay_ps": statistics.mean(delays),
        "relative_delay_std_ps": _sample_std(delays),
        "mean_equivalent_free_space_path_mm": statistics.mean(delays) * SPEED_OF_LIGHT_MM_PER_PS,
        "mean_phase_residual_rms_deg": statistics.mean(residuals),
        "maximum_phase_residual_rms_deg": max(residuals),
        "per_run": fits,
    }


def _temporal_drift(repeat_runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pass_axis = np.arange(len(repeat_runs), dtype=np.float64)
    design = np.column_stack((np.ones(pass_axis.size), pass_axis))
    rows = []
    for frequency_hz in FREQUENCIES_HZ:
        if not HIGH_BAND_MIN_HZ <= frequency_hz <= HIGH_BAND_MAX_HZ:
            continue
        observations = [
            _mapping(
                _mapping(run.get("observations"), "observations").get(frequency_hz),
                "frequency observation",
            )
            for run in repeat_runs
        ]
        if any(
            _number(observation.get("alignment_score"), "alignment score")
            < MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
            for observation in observations
        ):
            continue
        for antenna in ANTENNAS:
            if antenna == REFERENCE_ANTENNA:
                continue
            phases = []
            amplitudes_db = []
            for observation in observations:
                phase, amplitude_db = _relative_measurement(
                    _mapping(observation.get("states"), "frequency states"), antenna
                )
                phases.append(phase)
                amplitudes_db.append(amplitude_db)
            unwrapped_phase = np.rad2deg(
                np.unwrap(np.deg2rad(np.asarray(phases, dtype=np.float64)))
            )
            amplitude = np.asarray(amplitudes_db, dtype=np.float64)
            _, phase_slope = np.linalg.lstsq(design, unwrapped_phase, rcond=None)[0]
            _, amplitude_slope = np.linalg.lstsq(design, amplitude, rcond=None)[0]
            half = len(repeat_runs) // 2
            rows.append(
                {
                    "center_frequency_hz": frequency_hz,
                    "antenna": antenna,
                    "reference_antenna": REFERENCE_ANTENNA,
                    "phase_linear_drift_deg_per_pass": float(phase_slope),
                    "phase_first_to_last_deg": float(unwrapped_phase[-1] - unwrapped_phase[0]),
                    "phase_second_half_minus_first_half_deg": float(
                        np.mean(unwrapped_phase[-half:]) - np.mean(unwrapped_phase[:half])
                    ),
                    "amplitude_linear_drift_db_per_pass": float(amplitude_slope),
                    "amplitude_first_to_last_db": float(amplitude[-1] - amplitude[0]),
                    "amplitude_second_half_minus_first_half_db": float(
                        np.mean(amplitude[-half:]) - np.mean(amplitude[:half])
                    ),
                }
            )

    def absolute_summary(key: str) -> dict[str, Any]:
        values = [abs(_number(row.get(key), key)) for row in rows]
        worst = max(rows, key=lambda row: abs(_number(row.get(key), key)))
        return {
            "median": statistics.median(values),
            "p95": _percentile(values, 95.0),
            "maximum": max(values),
            "worst_case": {
                "center_frequency_hz": worst["center_frequency_hz"],
                "antenna": worst["antenna"],
                "signed_value": worst[key],
            },
        }

    return {
        "pass_count": len(repeat_runs),
        "frequency_min_hz": HIGH_BAND_MIN_HZ,
        "frequency_max_hz": HIGH_BAND_MAX_HZ,
        "path_frequency_count": len(rows),
        "phase_absolute_linear_drift_deg_per_pass": absolute_summary(
            "phase_linear_drift_deg_per_pass"
        ),
        "phase_absolute_first_to_last_deg": absolute_summary("phase_first_to_last_deg"),
        "phase_absolute_second_half_minus_first_half_deg": absolute_summary(
            "phase_second_half_minus_first_half_deg"
        ),
        "amplitude_absolute_linear_drift_db_per_pass": absolute_summary(
            "amplitude_linear_drift_db_per_pass"
        ),
        "amplitude_absolute_first_to_last_db": absolute_summary(
            "amplitude_first_to_last_db"
        ),
        "amplitude_absolute_second_half_minus_first_half_db": absolute_summary(
            "amplitude_second_half_minus_first_half_db"
        ),
        "rows": rows,
    }


def analyze(
    baseline_run: Mapping[str, Any], repeat_runs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if len(repeat_runs) < 2:
        raise RepeatabilityAnalysisError("at least two repeat manifests are required")
    identity = _validate_common_identity((baseline_run, *repeat_runs))
    run_pass_counts = []
    run_unambiguous_counts = []
    run_indeterminate_counts = []
    run_false_accept_counts = []
    repeat_run_results = []
    frequency_results = []
    failure_reasons: Counter[str] = Counter()

    for run in repeat_runs:
        observations = _mapping(run.get("observations"), "observations")
        conditions = []
        for frequency_hz in FREQUENCIES_HZ:
            observation = _mapping(observations.get(frequency_hz), "frequency row")
            alignment_score = _number(observation.get("alignment_score"), "alignment score")
            quality_passed = all(
                _mapping(_mapping(observation.get("states"), "states").get(antenna), antenna).get(
                    "quality_passed"
                )
                is True
                for antenna in ANTENNAS
            )
            alignment_unambiguous = alignment_score >= MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
            alignment_indeterminate = (
                DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR
                <= alignment_score
                < MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
            )
            conditions.append(
                {
                    "center_frequency_hz": frequency_hz,
                    "current_quality_gate_passed": quality_passed,
                    "alignment_score": alignment_score,
                    "alignment_unambiguous": alignment_unambiguous,
                    "alignment_indeterminate": alignment_indeterminate,
                    "classification": (
                        "unambiguous_alignment"
                        if alignment_unambiguous
                        else (
                            "indeterminate_alignment"
                            if alignment_indeterminate
                            else (
                                "ambiguous_alignment_false_accept"
                                if quality_passed
                                else "ambiguous_alignment_rejected"
                            )
                        )
                    ),
                }
            )
        pass_count = sum(row["current_quality_gate_passed"] for row in conditions)
        unambiguous_count = sum(row["alignment_unambiguous"] for row in conditions)
        indeterminate_count = sum(row["alignment_indeterminate"] for row in conditions)
        false_accept_count = sum(
            row["classification"] == "ambiguous_alignment_false_accept" for row in conditions
        )
        run_pass_counts.append(pass_count)
        run_unambiguous_counts.append(unambiguous_count)
        run_indeterminate_counts.append(indeterminate_count)
        run_false_accept_counts.append(false_accept_count)
        repeat_run_results.append(
            {
                "label": run["label"],
                "run_id": run["run_id"],
                "condition_pass_count": pass_count,
                "unambiguous_alignment_count": unambiguous_count,
                "indeterminate_alignment_count": indeterminate_count,
                "ambiguous_alignment_false_accept_count": false_accept_count,
                "condition_count": len(FREQUENCIES_HZ),
                "execution_attempt_count": run["execution_attempt_count"],
                "failed_attempt_count": len(run["failed_attempts"]),
                "conditions": conditions,
            }
        )
        for frequency_hz in FREQUENCIES_HZ:
            states = _mapping(
                _mapping(observations.get(frequency_hz), "frequency row").get("states"),
                "states",
            )
            for antenna in ANTENNAS:
                state = _mapping(states.get(antenna), antenna)
                failure_reasons.update(state.get("quality_rejection_reasons", []))

    baseline_observations = _mapping(baseline_run.get("observations"), "baseline observations")
    baseline_pass_count = 0
    for frequency_hz in FREQUENCIES_HZ:
        baseline_observation = _mapping(baseline_observations.get(frequency_hz), "baseline row")
        baseline_states = _mapping(baseline_observation.get("states"), "baseline states")
        baseline_alignment_score = _number(
            baseline_observation.get("alignment_score"), "baseline alignment score"
        )
        baseline_passed = all(
            _mapping(baseline_states.get(antenna), antenna).get("quality_passed") is True
            for antenna in ANTENNAS
        )
        baseline_pass_count += int(baseline_passed)

        condition_pass_count = 0
        state_pass_count = 0
        ambiguous_false_accept_count = 0
        indeterminate_alignment_count = 0
        unambiguous_runs = []
        for run in repeat_runs:
            observations = _mapping(run.get("observations"), "observations")
            observation = _mapping(observations.get(frequency_hz), "frequency row")
            states = _mapping(observation.get("states"), "states")
            condition_passed = all(
                _mapping(states.get(antenna), antenna).get("quality_passed") is True
                for antenna in ANTENNAS
            )
            condition_pass_count += int(condition_passed)
            state_pass_count += sum(
                _mapping(states.get(antenna), antenna).get("quality_passed") is True
                for antenna in ANTENNAS
            )
            alignment_score = _number(observation.get("alignment_score"), "alignment score")
            if alignment_score >= MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE:
                unambiguous_runs.append(run)
            elif alignment_score >= DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR:
                indeterminate_alignment_count += 1
            elif condition_passed:
                ambiguous_false_accept_count += 1

        per_antenna = []
        raw_contrasts_by_observation = []
        for antenna in ANTENNAS:
            phases = []
            amplitudes_db = []
            snrs = []
            raw_contrasts_db = []
            state_passes = 0
            for run in unambiguous_runs:
                observations = _mapping(run.get("observations"), "observations")
                states = _mapping(
                    _mapping(observations.get(frequency_hz), "frequency row").get("states"),
                    "states",
                )
                phase, amplitude_db = _relative_measurement(states, antenna)
                phases.append(phase)
                amplitudes_db.append(amplitude_db)
                state = _mapping(states.get(antenna), antenna)
                snrs.append(_number(state.get("transfer_detection_snr_db"), "SNR"))
                contrast = _number(
                    state.get("raw_selected_to_all_off_contrast_db"),
                    "raw selected-to-ALL_OFF contrast",
                )
                raw_contrasts_db.append(contrast)
                raw_contrasts_by_observation.append(contrast)
                state_passes += int(state.get("quality_passed") is True)
            per_antenna.append(
                {
                    "antenna": antenna,
                    "reference_antenna": REFERENCE_ANTENNA,
                    "valid_capture_count": len(unambiguous_runs),
                    "valid_state_pass_count": state_passes,
                    "relative_phase_mean_deg": (_circular_mean_deg(phases) if phases else None),
                    "relative_phase_circular_std_deg": (
                        _circular_std_deg(phases) if len(phases) >= 2 else None
                    ),
                    "relative_amplitude_mean_db": (
                        statistics.mean(amplitudes_db) if amplitudes_db else None
                    ),
                    "relative_amplitude_std_db": (
                        _sample_std(amplitudes_db) if len(amplitudes_db) >= 2 else None
                    ),
                    "median_transfer_detection_snr_db": (statistics.median(snrs) if snrs else None),
                    "minimum_transfer_detection_snr_db": min(snrs) if snrs else None,
                    "minimum_raw_selected_to_all_off_contrast_db": (
                        min(raw_contrasts_db) if raw_contrasts_db else None
                    ),
                    "median_raw_selected_to_all_off_contrast_db": (
                        statistics.median(raw_contrasts_db) if raw_contrasts_db else None
                    ),
                    "maximum_raw_selected_to_all_off_contrast_db": (
                        max(raw_contrasts_db) if raw_contrasts_db else None
                    ),
                    "operational_raw_isolation_pass_count": sum(
                        value >= MINIMUM_OPERATIONAL_RAW_CONTRAST_DB for value in raw_contrasts_db
                    ),
                    "one_degree_raw_isolation_pass_count": sum(
                        value >= MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB for value in raw_contrasts_db
                    ),
                }
            )

        nonreference = [row for row in per_antenna if row["antenna"] != REFERENCE_ANTENNA]
        phase_stds = [
            row["relative_phase_circular_std_deg"]
            for row in nonreference
            if row["relative_phase_circular_std_deg"] is not None
        ]
        amplitude_stds = [
            row["relative_amplitude_std_db"]
            for row in nonreference
            if row["relative_amplitude_std_db"] is not None
        ]
        frequency_results.append(
            {
                "center_frequency_hz": frequency_hz,
                "baseline_condition_passed": baseline_passed,
                "baseline_alignment_score": baseline_alignment_score,
                "baseline_alignment_unambiguous": (
                    baseline_alignment_score >= MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
                ),
                "baseline_alignment_indeterminate": (
                    DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR
                    <= baseline_alignment_score
                    < MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
                ),
                "repeat_condition_pass_count": condition_pass_count,
                "repeat_condition_count": len(repeat_runs),
                "unambiguous_alignment_capture_count": len(unambiguous_runs),
                "indeterminate_alignment_capture_count": indeterminate_alignment_count,
                "ambiguous_alignment_capture_count": len(repeat_runs)
                - len(unambiguous_runs)
                - indeterminate_alignment_count,
                "ambiguous_alignment_false_accept_count": ambiguous_false_accept_count,
                "repeat_state_pass_count": state_pass_count,
                "repeat_state_count": len(repeat_runs) * len(ANTENNAS),
                "median_relative_phase_circular_std_deg": (
                    statistics.median(phase_stds) if phase_stds else None
                ),
                "maximum_relative_phase_circular_std_deg": (
                    max(phase_stds) if phase_stds else None
                ),
                "median_relative_amplitude_std_db": (
                    statistics.median(amplitude_stds) if amplitude_stds else None
                ),
                "maximum_relative_amplitude_std_db": (
                    max(amplitude_stds) if amplitude_stds else None
                ),
                "minimum_raw_selected_to_all_off_contrast_db": (
                    min(raw_contrasts_by_observation) if raw_contrasts_by_observation else None
                ),
                "median_raw_selected_to_all_off_contrast_db": (
                    statistics.median(raw_contrasts_by_observation)
                    if raw_contrasts_by_observation
                    else None
                ),
                "maximum_raw_selected_to_all_off_contrast_db": (
                    max(raw_contrasts_by_observation) if raw_contrasts_by_observation else None
                ),
                "operational_raw_isolation_pass_count": sum(
                    value >= MINIMUM_OPERATIONAL_RAW_CONTRAST_DB
                    for value in raw_contrasts_by_observation
                ),
                "one_degree_raw_isolation_pass_count": sum(
                    value >= MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB
                    for value in raw_contrasts_by_observation
                ),
                "raw_isolation_observation_count": len(raw_contrasts_by_observation),
                "all_unambiguous_states_operational_raw_isolation_passed": bool(
                    raw_contrasts_by_observation
                )
                and all(
                    value >= MINIMUM_OPERATIONAL_RAW_CONTRAST_DB
                    for value in raw_contrasts_by_observation
                ),
                "all_unambiguous_states_one_degree_raw_isolation_passed": bool(
                    raw_contrasts_by_observation
                )
                and all(
                    value >= MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB
                    for value in raw_contrasts_by_observation
                ),
                "paths": per_antenna,
            }
        )

    path_results = []
    for antenna in ANTENNAS:
        state_pass_count = 0
        snrs = []
        raw_contrasts = []
        within_capture_phase_std = []
        frequency_phase_stds = []
        frequency_amplitude_stds = []
        for frequency_hz in FREQUENCIES_HZ:
            row = next(
                item for item in frequency_results if item["center_frequency_hz"] == frequency_hz
            )
            path = next(item for item in row["paths"] if item["antenna"] == antenna)
            if path["relative_phase_circular_std_deg"] is not None:
                frequency_phase_stds.append(path["relative_phase_circular_std_deg"])
            if path["relative_amplitude_std_db"] is not None:
                frequency_amplitude_stds.append(path["relative_amplitude_std_db"])
            for run in repeat_runs:
                states = _mapping(
                    _mapping(
                        _mapping(run.get("observations"), "observations").get(frequency_hz),
                        "frequency row",
                    ).get("states"),
                    "states",
                )
                state = _mapping(states.get(antenna), antenna)
                state_pass_count += int(state.get("quality_passed") is True)
                snrs.append(_number(state.get("transfer_detection_snr_db"), "SNR"))
                raw_contrasts.append(
                    _number(
                        state.get("raw_selected_to_all_off_contrast_db"),
                        "raw selected-to-ALL_OFF contrast",
                    )
                )
                within_capture_phase_std.append(
                    _number(state.get("cycle_phase_std_deg"), "cycle phase standard deviation")
                )
        path_results.append(
            {
                "antenna": antenna,
                "state_pass_count": state_pass_count,
                "state_count": len(repeat_runs) * len(FREQUENCIES_HZ),
                "state_pass_fraction": state_pass_count / (len(repeat_runs) * len(FREQUENCIES_HZ)),
                "median_transfer_detection_snr_db": statistics.median(snrs),
                "minimum_raw_selected_to_all_off_contrast_db": min(raw_contrasts),
                "median_raw_selected_to_all_off_contrast_db": statistics.median(raw_contrasts),
                "median_within_capture_phase_std_deg": statistics.median(within_capture_phase_std),
                "median_cross_run_relative_phase_std_deg": statistics.median(frequency_phase_stds),
                "median_cross_run_relative_amplitude_std_db": statistics.median(
                    frequency_amplitude_stds
                ),
            }
        )

    high_band_rows = [
        row
        for row in frequency_results
        if HIGH_BAND_MIN_HZ <= row["center_frequency_hz"] <= HIGH_BAND_MAX_HZ
    ]
    high_band_phase = [
        path["relative_phase_circular_std_deg"]
        for row in high_band_rows
        for path in row["paths"]
        if path["antenna"] != REFERENCE_ANTENNA
        and path["relative_phase_circular_std_deg"] is not None
    ]
    high_band_amplitude = [
        path["relative_amplitude_std_db"]
        for row in high_band_rows
        for path in row["paths"]
        if path["antenna"] != REFERENCE_ANTENNA and path["relative_amplitude_std_db"] is not None
    ]
    all_runs = (baseline_run, *repeat_runs)
    operational_frequencies = [
        row["center_frequency_hz"]
        for row in frequency_results
        if row["unambiguous_alignment_capture_count"] == len(repeat_runs)
        and row["all_unambiguous_states_operational_raw_isolation_passed"]
    ]
    precision_frequencies = [
        row["center_frequency_hz"]
        for row in frequency_results
        if row["unambiguous_alignment_capture_count"] == len(repeat_runs)
        and row["all_unambiguous_states_one_degree_raw_isolation_passed"]
    ]
    alignment_scores = [
        _number(row.get("alignment_score"), "alignment score")
        for run in repeat_runs
        for row in _mapping(run.get("observations"), "observations").values()
    ]
    unambiguous_alignment_scores = [
        score for score in alignment_scores if score >= MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
    ]
    ambiguous_alignment_scores = [
        score for score in alignment_scores if score < DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR
    ]
    indeterminate_alignment_scores = [
        score
        for score in alignment_scores
        if DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR
        <= score
        < MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE
    ]
    cycle_counts = [
        _integer(row.get("complete_cycle_count"), "complete cycle count")
        for run in repeat_runs
        for row in _mapping(run.get("observations"), "observations").values()
    ]
    reference_fractions = [
        _number(row.get("reference_valid_bin_fraction"), "reference-valid fraction")
        for run in repeat_runs
        for row in _mapping(run.get("observations"), "observations").values()
    ]
    artifact_ids = [
        _string(row.get("artifact_id"), "artifact ID")
        for run in repeat_runs
        for row in _mapping(run.get("observations"), "observations").values()
    ]
    artifact_hashes = [
        _string(row.get("artifact_sha256"), "artifact SHA-256")
        for run in repeat_runs
        for row in _mapping(run.get("observations"), "observations").values()
    ]
    failed_attempts = [
        {"run_label": run["label"], **dict(failure)}
        for run in repeat_runs
        for failure in _sequence(run.get("failed_attempts"), "failed attempts")
    ]
    execution_attempt_count = sum(
        _integer(run.get("execution_attempt_count"), "execution attempt count")
        for run in repeat_runs
    )
    return {
        "schema": 1,
        "analysis_kind": "rotation0_broadband_repeatability",
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": identity,
        "scope": {
            "baseline_run_label": baseline_run["label"],
            "repeat_run_labels": [run["label"] for run in repeat_runs],
            "rotation": 0,
            "mapping": EXPECTED_MAPPING,
            "frequency_min_hz": FREQUENCIES_HZ[0],
            "frequency_max_hz": FREQUENCIES_HZ[-1],
            "frequency_step_hz": 100_000_000,
            "frequency_count": len(FREQUENCIES_HZ),
            "reference_antenna": REFERENCE_ANTENNA,
        },
        "source_runs": [
            {
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
            }
            for run in all_runs
        ],
        "acquisition_integrity": {
            "repeat_capture_count": len(artifact_ids),
            "repeat_execution_attempt_count": execution_attempt_count,
            "repeat_failed_attempt_count": len(failed_attempts),
            "repeat_failed_attempt_fraction": len(failed_attempts) / execution_attempt_count,
            "repeat_failed_attempts": failed_attempts,
            "all_failed_attempts_quarantined_without_artifact": all(
                row["artifact_id"] is None for row in failed_attempts
            ),
            "all_failed_attempts_post_muted": all(
                row["post_mute_status"] == "passed" for row in failed_attempts
            ),
            "unique_repeat_artifact_count": len(set(artifact_ids)),
            "unique_repeat_artifact_sha256_count": len(set(artifact_hashes)),
            "minimum_complete_cycle_count": min(cycle_counts),
            "maximum_complete_cycle_count": max(cycle_counts),
            "minimum_reference_valid_bin_fraction": min(reference_fractions),
            "all_continuity_and_headroom_admitted": True,
            "all_post_attempt_mutes_passed": True,
            "all_final_mutes_passed": True,
        },
        "pass_summary": {
            "baseline_condition_pass_count": baseline_pass_count,
            "baseline_condition_count": len(FREQUENCIES_HZ),
            "repeat_condition_pass_counts": run_pass_counts,
            "repeat_unambiguous_alignment_counts": run_unambiguous_counts,
            "repeat_indeterminate_alignment_counts": run_indeterminate_counts,
            "repeat_ambiguous_alignment_false_accept_counts": run_false_accept_counts,
            "repeat_run_results": repeat_run_results,
            "repeat_condition_pass_mean": statistics.mean(run_pass_counts),
            "repeat_condition_pass_std": _sample_std(run_pass_counts),
            "repeat_condition_pass_fraction": sum(run_pass_counts)
            / (len(repeat_runs) * len(FREQUENCIES_HZ)),
            "repeat_state_pass_count": sum(row["state_pass_count"] for row in path_results),
            "repeat_state_count": len(repeat_runs) * len(FREQUENCIES_HZ) * len(ANTENNAS),
            "repeat_state_pass_fraction": sum(row["state_pass_count"] for row in path_results)
            / (len(repeat_runs) * len(FREQUENCIES_HZ) * len(ANTENNAS)),
            "failure_reason_occurrences": dict(failure_reasons.most_common()),
        },
        "alignment_diagnostics": {
            "current_minimum_alignment_score": 0.75,
            "diagnostic_alignment_mode_separator": DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR,
            "minimum_unambiguous_alignment_score": MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE,
            "capture_count": len(alignment_scores),
            "unambiguous_alignment_capture_count": len(unambiguous_alignment_scores),
            "indeterminate_alignment_capture_count": len(indeterminate_alignment_scores),
            "ambiguous_alignment_capture_count": len(ambiguous_alignment_scores),
            "ambiguous_alignment_false_accept_count": sum(run_false_accept_counts),
            "ambiguous_alignment_quality_reject_count": len(ambiguous_alignment_scores)
            - sum(run_false_accept_counts),
            "indeterminate_alignment_quality_pass_count": sum(
                row["current_quality_gate_passed"]
                for result in repeat_run_results
                for row in result["conditions"]
                if row["alignment_indeterminate"]
            ),
            "minimum_unambiguous_alignment_score_observed": min(unambiguous_alignment_scores),
            "maximum_ambiguous_alignment_score_observed": (
                max(ambiguous_alignment_scores) if ambiguous_alignment_scores else None
            ),
            "minimum_indeterminate_alignment_score_observed": (
                min(indeterminate_alignment_scores) if indeterminate_alignment_scores else None
            ),
            "maximum_indeterminate_alignment_score_observed": (
                max(indeterminate_alignment_scores) if indeterminate_alignment_scores else None
            ),
            "observed_score_gap": (
                min(unambiguous_alignment_scores) - max(ambiguous_alignment_scores)
                if ambiguous_alignment_scores and unambiguous_alignment_scores
                else None
            ),
            "recommended_minimum_alignment_score": 0.95,
            "finding": (
                "the 0.75 gate admits a wrong dwell-lock mode; scores in the gap are "
                "retained as indeterminate rather than RF evidence"
            ),
        },
        "frequency_results": frequency_results,
        "path_results": path_results,
        "high_band_repeatability": {
            "frequency_min_hz": HIGH_BAND_MIN_HZ,
            "frequency_max_hz": HIGH_BAND_MAX_HZ,
            "frequency_count": len(high_band_rows),
            "all_repeat_conditions_passed": all(
                row["repeat_condition_pass_count"] == len(repeat_runs)
                and row["unambiguous_alignment_capture_count"] == len(repeat_runs)
                for row in high_band_rows
            ),
            "relative_phase_std_median_deg": statistics.median(high_band_phase),
            "relative_phase_std_p95_deg": _percentile(high_band_phase, 95.0),
            "relative_phase_std_max_deg": max(high_band_phase),
            "relative_amplitude_std_median_db": statistics.median(high_band_amplitude),
            "relative_amplitude_std_p95_db": _percentile(high_band_amplitude, 95.0),
            "relative_amplitude_std_max_db": max(high_band_amplitude),
            "qualification_note": (
                "repeatability is computed after ALL_OFF subtraction and does not imply "
                "adequate raw selected-to-ALL_OFF isolation"
            ),
        },
        "raw_isolation": {
            "minimum_operational_raw_contrast_db": MINIMUM_OPERATIONAL_RAW_CONTRAST_DB,
            "minimum_raw_contrast_for_one_degree_bound_db": (MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB),
            "repeatable_and_operational_frequencies_hz": operational_frequencies,
            "repeatable_and_one_degree_isolation_frequencies_hz": precision_frequencies,
        },
        "relative_delay_fits": [
            _delay_fit(repeat_runs, antenna) for antenna in ANTENNAS if antenna != REFERENCE_ANTENNA
        ],
        "temporal_drift": _temporal_drift(repeat_runs),
        "interpretation": {
            "repeatable_after_all_off_subtraction_band_hz": [
                HIGH_BAND_MIN_HZ,
                HIGH_BAND_MAX_HZ,
            ],
            "repeatable_and_operational_frequencies_hz": operational_frequencies,
            "repeatable_and_one_degree_isolation_frequencies_hz": precision_frequencies,
            "primary_failure_mode": "ambiguous dwell-schedule alignment",
            "frequencies_without_unambiguous_repeat_capture_hz": [
                row["center_frequency_hz"]
                for row in frequency_results
                if row["unambiguous_alignment_capture_count"] == 0
            ],
            "single_delay_model_sufficient": False,
            "recommended_calibration_model": (
                "per-frequency complex correction with validity metadata"
            ),
            "deembedding_status": "rotation0 mixes splitter-arm, cable, and board-path response",
            "five_point_eight_ghz_status": "repeatable_after_subtraction_but_leakage_limited",
            "recommended_next_action": (
                "tighten alignment admission and reanalyze the existing immutable captures "
                "before reacquiring or remapping the fixture"
            ),
        },
    }


def _render_figures(document: Mapping[str, Any], directory: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    directory.mkdir(parents=True, exist_ok=True)
    rows = list(_sequence(document.get("frequency_results"), "frequency results"))
    frequencies_ghz = np.asarray(
        [
            _integer(_mapping(row, "frequency row").get("center_frequency_hz"), "frequency") / 1e9
            for row in rows
        ]
    )
    pass_summary = _mapping(document.get("pass_summary"), "pass summary")
    repeat_results = _sequence(pass_summary.get("repeat_run_results"), "repeat run results")
    run_labels = ["Baseline", *[f"Repeat {index}" for index in range(1, len(repeat_results) + 1)]]
    matrix = np.zeros((len(run_labels), len(FREQUENCIES_HZ)), dtype=np.float64)
    matrix[0] = [
        (
            3
            if bool(_mapping(row, "frequency row").get("baseline_alignment_unambiguous"))
            else (
                2
                if bool(_mapping(row, "frequency row").get("baseline_alignment_indeterminate"))
                else (
                    1
                    if bool(_mapping(row, "frequency row").get("baseline_condition_passed"))
                    else 0
                )
            )
        )
        for row in rows
    ]
    for repeat_index, raw_result in enumerate(repeat_results):
        result = _mapping(raw_result, "repeat run result")
        conditions = _sequence(result.get("conditions"), "repeat conditions")
        matrix[repeat_index + 1] = [
            {
                "ambiguous_alignment_rejected": 0,
                "ambiguous_alignment_false_accept": 1,
                "indeterminate_alignment": 2,
                "unambiguous_alignment": 3,
            }[_string(_mapping(row, "repeat condition").get("classification"), "classification")]
            for row in conditions
        ]

    figure_height = max(3.8, 1.5 + 0.48 * len(run_labels))
    figure, axis = plt.subplots(figsize=(14.0, figure_height), constrained_layout=True)
    axis.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap=ListedColormap(["#c94b40", "#d99a31", "#765aa6", "#2d8a57"]),
        vmin=0,
        vmax=3,
    )
    axis.set_title("Rotation-0 dwell-alignment classification by run")
    axis.set_xlabel("Centre frequency (GHz)")
    axis.set_ylabel("Capture run")
    axis.set_yticks(range(len(run_labels)), run_labels)
    tick_columns = list(range(0, len(frequencies_ghz), 2))
    axis.set_xticks(
        tick_columns,
        [f"{frequencies_ghz[index]:.1f}" for index in tick_columns],
        rotation=45,
        ha="right",
    )
    for boundary in (2.65, 2.95, 3.55):
        axis.axvline((boundary - 2.1) / 0.1, color="white", linewidth=0.8, alpha=0.7)
    axis.legend(
        handles=[
            Patch(color="#2d8a57", label="Unambiguous alignment"),
            Patch(color="#765aa6", label="Indeterminate alignment"),
            Patch(color="#d99a31", label="Ambiguous; current gate passed"),
            Patch(color="#c94b40", label="Ambiguous; current gate rejected"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.32),
        ncol=4,
    )
    figure.savefig(directory / "fig01_run_frequency_quality_matrix.png", dpi=180)
    plt.close(figure)

    def optional_number(value: object, label: str) -> float:
        return float("nan") if value is None else _number(value, label)

    median_phase = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("median_relative_phase_circular_std_deg"),
                "median phase",
            )
            for row in rows
        ]
    )
    maximum_phase = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("maximum_relative_phase_circular_std_deg"),
                "maximum phase",
            )
            for row in rows
        ]
    )
    median_amplitude = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("median_relative_amplitude_std_db"),
                "median amplitude",
            )
            for row in rows
        ]
    )
    maximum_amplitude = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("maximum_relative_amplitude_std_db"),
                "maximum amplitude",
            )
            for row in rows
        ]
    )
    figure, axes = plt.subplots(3, 1, figsize=(12.0, 10.5), sharex=True, constrained_layout=True)
    axes[0].semilogy(frequencies_ghz, median_phase, marker="o", label="Median across ANT1-ANT7")
    axes[0].semilogy(frequencies_ghz, maximum_phase, marker=".", label="Worst path")
    axes[0].axhline(1.0, color="black", linewidth=0.8, linestyle="--", label="1 degree")
    axes[0].axvspan(3.6, 5.8, alpha=0.12, color="#2d8a57")
    axes[0].set_ylabel("Cross-run phase sigma (degrees)")
    axes[0].set_title(
        f"Relative ANTn/ANT8 repeatability across {len(repeat_results)} rotation-0 sweeps"
    )
    axes[0].grid(True, which="both", alpha=0.25)
    axes[0].legend(ncol=3)
    axes[1].semilogy(frequencies_ghz, median_amplitude, marker="o", label="Median across ANT1-ANT7")
    axes[1].semilogy(frequencies_ghz, maximum_amplitude, marker=".", label="Worst path")
    axes[1].axhline(0.2, color="black", linewidth=0.8, linestyle="--", label="0.2 dB")
    axes[1].axvspan(3.6, 5.8, alpha=0.12, color="#2d8a57")
    axes[1].set_xlabel("Centre frequency (GHz)")
    axes[1].set_ylabel("Cross-run amplitude sigma (dB)")
    axes[1].grid(True, which="both", alpha=0.25)
    axes[1].legend(ncol=3)
    minimum_raw_contrast = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("minimum_raw_selected_to_all_off_contrast_db"),
                "minimum raw contrast",
            )
            for row in rows
        ]
    )
    median_raw_contrast = np.asarray(
        [
            optional_number(
                _mapping(row, "frequency row").get("median_raw_selected_to_all_off_contrast_db"),
                "median raw contrast",
            )
            for row in rows
        ]
    )
    axes[2].plot(frequencies_ghz, median_raw_contrast, marker="o", label="Median")
    axes[2].plot(frequencies_ghz, minimum_raw_contrast, marker=".", label="Worst path/run")
    axes[2].axhline(
        MINIMUM_OPERATIONAL_RAW_CONTRAST_DB,
        color="black",
        linewidth=0.8,
        linestyle="--",
        label="20 dB operational",
    )
    axes[2].axhline(
        MINIMUM_ONE_DEGREE_RAW_CONTRAST_DB,
        color="black",
        linewidth=0.8,
        linestyle=":",
        label="35.16 dB one-degree bound",
    )
    axes[2].set_xlabel("Centre frequency (GHz)")
    axes[2].set_ylabel("Raw selected/ALL_OFF contrast (dB)")
    axes[2].grid(True, alpha=0.25)
    axes[2].legend(ncol=4)
    figure.savefig(directory / "fig02_relative_phase_amplitude_repeatability.png", dpi=180)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(12.0, 5.6), constrained_layout=True)
    for repeat_index, raw_result in enumerate(repeat_results, start=1):
        result = _mapping(raw_result, "repeat run result")
        conditions = _sequence(result.get("conditions"), "repeat conditions")
        scores = [
            _number(_mapping(row, "repeat condition").get("alignment_score"), "alignment score")
            for row in conditions
        ]
        axis.plot(
            frequencies_ghz,
            scores,
            marker="o",
            markersize=3,
            linewidth=0.8,
            label=f"Repeat {repeat_index}",
        )
    axis.axhline(
        DIAGNOSTIC_ALIGNMENT_MODE_SEPARATOR,
        color="black",
        linestyle="--",
        linewidth=0.9,
        label="0.85 bad-mode separator",
    )
    axis.axhline(
        MINIMUM_UNAMBIGUOUS_ALIGNMENT_SCORE,
        color="black",
        linestyle=":",
        linewidth=0.9,
        label="0.95 production admission",
    )
    axis.set_ylim(0.74, 1.02)
    axis.set_xlabel("Centre frequency (GHz)")
    axis.set_ylabel("Dwell-alignment score")
    axis.set_title("Alignment-score modes explain the apparent RF instability")
    axis.grid(True, alpha=0.25)
    axis.legend(ncol=3)
    figure.savefig(directory / "fig04_alignment_score_modes.png", dpi=180)
    plt.close(figure)

    paths = list(_sequence(document.get("path_results"), "path results"))
    pass_percent = [
        100.0 * _number(_mapping(row, "path row").get("state_pass_fraction"), "pass fraction")
        for row in paths
    ]
    figure, axis = plt.subplots(figsize=(9.0, 4.8), constrained_layout=True)
    bars = axis.bar(ANTENNAS, pass_percent, color="#477db3")
    axis.set_ylim(80.0, 100.5)
    axis.set_ylabel("Current-gate path acceptance (%)")
    axis.set_xlabel("Selector state")
    axis.set_title("Pre-correction path acceptance (includes ambiguous dwell locks)")
    axis.grid(True, axis="y", alpha=0.25)
    for bar, value in zip(bars, pass_percent, strict=True):
        axis.text(
            bar.get_x() + bar.get_width() / 2.0,
            value + 0.35,
            f"{value:.1f}%",
            ha="center",
            va="bottom",
        )
    figure.savefig(directory / "fig03_path_reliability.png", dpi=180)
    plt.close(figure)

    temporal_drift = _mapping(document.get("temporal_drift"), "temporal drift")
    temporal_rows = [
        _mapping(row, "temporal drift row")
        for row in _sequence(temporal_drift.get("rows"), "temporal drift rows")
    ]
    drift_frequencies = sorted(
        {_integer(row.get("center_frequency_hz"), "drift frequency") for row in temporal_rows}
    )
    drift_frequencies_ghz = [frequency_hz / 1e9 for frequency_hz in drift_frequencies]
    phase_medians = []
    phase_maxima = []
    amplitude_medians = []
    amplitude_maxima = []
    for frequency_hz in drift_frequencies:
        frequency_rows = [
            row
            for row in temporal_rows
            if _integer(row.get("center_frequency_hz"), "drift frequency") == frequency_hz
        ]
        phase_values = [
            abs(_number(row.get("phase_first_to_last_deg"), "phase drift"))
            for row in frequency_rows
        ]
        amplitude_values = [
            abs(_number(row.get("amplitude_first_to_last_db"), "amplitude drift"))
            for row in frequency_rows
        ]
        phase_medians.append(statistics.median(phase_values))
        phase_maxima.append(max(phase_values))
        amplitude_medians.append(statistics.median(amplitude_values))
        amplitude_maxima.append(max(amplitude_values))

    figure, axes = plt.subplots(2, 1, figsize=(12.0, 7.5), sharex=True, constrained_layout=True)
    axes[0].plot(
        drift_frequencies_ghz,
        phase_medians,
        marker="o",
        markersize=3,
        linewidth=1.0,
        label="median path",
    )
    axes[0].plot(
        drift_frequencies_ghz,
        phase_maxima,
        marker="o",
        markersize=3,
        linewidth=1.0,
        label="worst path",
    )
    axes[0].axhline(1.0, color="black", linestyle="--", linewidth=0.9, label="1 degree")
    axes[0].set_ylabel("|First-to-last phase change| (degrees)")
    axes[0].set_title("Ten-pass temporal drift remains small across the repeatable band")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend()

    axes[1].plot(
        drift_frequencies_ghz,
        amplitude_medians,
        marker="o",
        markersize=3,
        linewidth=1.0,
        label="median path",
    )
    axes[1].plot(
        drift_frequencies_ghz,
        amplitude_maxima,
        marker="o",
        markersize=3,
        linewidth=1.0,
        label="worst path",
    )
    axes[1].axhline(0.2, color="black", linestyle="--", linewidth=0.9, label="0.2 dB")
    axes[1].set_xlabel("Centre frequency (GHz)")
    axes[1].set_ylabel("|First-to-last amplitude change| (dB)")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend()
    figure.savefig(directory / "fig05_temporal_drift.png", dpi=180)
    plt.close(figure)


def main() -> int:
    arguments = _parser().parse_args()
    if len(arguments.repeat_manifest) < 2:
        raise RepeatabilityAnalysisError("pass --repeat-manifest at least twice")
    baseline = _load_run("baseline", arguments.baseline_manifest)
    repeats = [
        _load_run(f"repeat-{index}", path)
        for index, path in enumerate(arguments.repeat_manifest, start=1)
    ]
    document = analyze(baseline, repeats)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if arguments.figure_directory is not None:
        _render_figures(document, arguments.figure_directory)
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "figure_directory": str(arguments.figure_directory)
                if arguments.figure_directory
                else None,
                "repeat_capture_count": document["acquisition_integrity"]["repeat_capture_count"],
                "repeatable_after_all_off_subtraction_band_hz": document["interpretation"][
                    "repeatable_after_all_off_subtraction_band_hz"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
