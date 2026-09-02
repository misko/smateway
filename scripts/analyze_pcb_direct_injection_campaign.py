#!/usr/bin/env python3
"""Validate and report the controlled ANT1-ANT8 PCB direct-injection campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import analyze_pinned_broadband_campaign as raw_analyzer
import matplotlib.pyplot as plt
import numpy as np

PORTS = tuple(f"ANT{i}" for i in range(1, 9))
STATES = ("ALL_OFF", *PORTS)
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
DISPLAY_REFERENCE_PORT = "ANT1"
FULL_FREQUENCIES_HZ = tuple(range(500_000_000, 6_000_000_001, 12_500_000))
HOLDOUT_FREQUENCIES_HZ = tuple(range(5_006_250_000, 5_993_750_001, 12_500_000))
QUALIFICATION_FREQUENCY_HZ = 5_800_000_000
RADIO_URI = "ip:192.168.1.15"
RADIO_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.173"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
STLINK_SERIAL = "002D003A3335511035383531"
CAPTURE_COMMIT = "4833e41b15adaa07efa5bad5737b4a6427da3ac5"
LIGHT_SPEED_M_S = 299_792_458.0
ARRAY_DESIGN_MAX_FREQUENCY_HZ = 6_000_000_000
C6_RECOMMENDED_PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
C6_OMITTED_PORTS = ("ANT3", "ANT6")
C6_OPPOSITE_PAIR_SCAN = ("ANT1", "ANT8", "ANT2", "ANT7", "ANT4", "ANT5")
C8_RECOMMENDED_PORTS = ("ANT1", "ANT2", "ANT3", "ANT4", "ANT8", "ANT7", "ANT6", "ANT5")
C8_OPPOSITE_PAIR_SCAN = ("ANT1", "ANT8", "ANT2", "ANT7", "ANT3", "ANT6", "ANT4", "ANT5")

RUNS: dict[str, dict[str, tuple[str, ...]]] = {
    "ANT1": {
        "full": ("ant1-0500-6000-12p5mhz-selected/20260902T154313.787163Z/run.json",),
        "holdout": ("ant1-5000-6000-missing-6p25mhz-selected/20260902T161602.438628Z/run.json",),
        "qualification": ("ant1-5g8-qualification/20260902T154018.580959Z/run.json",),
    },
    "ANT2": {
        "full": (
            "ant2-0500-6000-25mhz-selected/20260901T185155.319303Z/run.json",
            "ant2-0500-6000-missing-12p5mhz-selected/20260901T191536.025518Z/run.json",
        ),
        "holdout": ("ant2-5000-6000-missing-6p25mhz-selected/20260901T193325.229080Z/run.json",),
        "qualification": ("ant2-5g8-qualification/20260901T183905.680262Z/run.json",),
    },
    "ANT3": {
        "full": ("ant3-0500-6000-12p5mhz-selected/20260901T200112.536069Z/run.json",),
        "holdout": ("ant3-5000-6000-missing-6p25mhz-selected/20260901T203528.526192Z/run.json",),
        "qualification": ("ant3-5g8-qualification/20260901T195821.480816Z/run.json",),
    },
    "ANT4": {
        "full": ("ant4-0500-6000-12p5mhz-selected/20260901T215731.607881Z/run.json",),
        "holdout": ("ant4-5000-6000-missing-6p25mhz-selected/20260901T223116.040837Z/run.json",),
        "qualification": ("ant4-5g8-qualification/20260901T215456.814904Z/run.json",),
    },
    "ANT5": {
        "full": ("ant5-0500-6000-12p5mhz-selected/20260901T224436.038957Z/run.json",),
        "holdout": ("ant5-5000-6000-missing-6p25mhz-selected/20260901T231735.061003Z/run.json",),
        "qualification": ("ant5-5g8-qualification/20260901T224153.862197Z/run.json",),
    },
    "ANT6": {
        "full": ("ant6-0500-6000-12p5mhz-selected/20260901T234445.232953Z/run.json",),
        "holdout": ("ant6-5000-6000-missing-6p25mhz-selected/20260902T001838.405070Z/run.json",),
        "qualification": ("ant6-5g8-qualification/20260901T234206.442911Z/run.json",),
    },
    "ANT7": {
        "full": ("ant7-0500-6000-12p5mhz-selected/20260902T013045.276130Z/run.json",),
        "holdout": ("ant7-5000-6000-missing-6p25mhz-selected/20260902T020402.732673Z/run.json",),
        "qualification": ("ant7-5g8-qualification/20260902T012802.189572Z/run.json",),
    },
    "ANT8": {
        "full": ("ant8-0500-6000-12p5mhz-selected/20260902T021837.563992Z/run.json",),
        "holdout": ("ant8-5000-6000-missing-6p25mhz-selected/20260902T025336.217277Z/run.json",),
        "qualification": ("ant8-5g8-qualification/20260902T021519.885275Z/run.json",),
    },
}

FIGURES = (
    "fig01_campaign_setup_and_reference_planes.png",
    "fig02_frequency_coverage_and_chronology.png",
    "fig03_acquisition_quality_and_safety.png",
    "fig04_5g8_selector_isolation_matrix.png",
    "fig05_port_magnitude_response.png",
    "fig06_port_unwrapped_phase.png",
    "fig07_relative_gain_correction_heatmap.png",
    "fig08_relative_phase_correction_heatmap.png",
    "fig09_pairwise_5g8_correction_matrices.png",
    "fig10_delay_models_and_residuals.png",
    "fig11_holdout_error_by_port.png",
    "fig12_holdout_residual_heatmaps.png",
    "fig13_model_and_lut_comparison.png",
    "fig14_df_error_budget_and_array_geometry.png",
    "fig15_recommended_c6_c8_layout_and_port_map.png",
)


class CampaignError(ValueError):
    """The pinned direct-injection evidence is incomplete or inconsistent."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/srv/bulk/samteway/state/lab-runs/network-192.168.1.15/pcb-direct-injection"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/pcb_direct_injection_calibration"),
    )
    parser.add_argument(
        "--skip-raw-replay",
        action="store_true",
        help="development-only: hash raw IQ but trust stored analysis instead of replaying it",
    )
    parser.add_argument(
        "--skip-raw-hash",
        action="store_true",
        help="development-only: validate filenames and sizes without hashing raw IQ",
    )
    return parser


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise CampaignError(f"{path} contains non-finite JSON constant {value}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"cannot load {path}: {error}") from error
    if not isinstance(value, dict):
        raise CampaignError(f"{path} is not a JSON object")
    return value


def _finite(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CampaignError(f"{label} is not numeric") from error
    if not math.isfinite(result):
        raise CampaignError(f"{label} is not finite")
    return result


def _require_mute(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} is missing")
    if (
        value.get("passed") is not True
        or value.get("tx_gain_db") != [-80.0, -80.0]
        or value.get("dds_scales") != [0.0] * 8
    ):
        raise CampaignError(f"{label} does not prove exact mute")


def _require_all_off(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} is missing")
    if (
        value.get("applied_code") != STATE_CODES["ALL_OFF"]
        or value.get("command_code") != STATE_CODES["ALL_OFF"]
        or value.get("lease_active") is not False
        or value.get("remaining_lease_ms") != 0
        or value.get("command_valid") is not True
        or value.get("invalid_command") is not False
        or value.get("guard_active") is not False
    ):
        raise CampaignError(f"{label} is not lease-free ALL_OFF")


def _expected_common_configuration() -> dict[str, Any]:
    return {
        "bandwidth_hz": 1_600_000,
        "dds_scale": 0.25,
        "rx_gain_db": 60,
        "sample_count": 262_144,
        "sample_rate_hz": 2_000_000,
        "tone_offset_hz": 100_000,
    }


def _validate_run_header(
    path: Path,
    document: Mapping[str, Any],
    *,
    role: str,
    port: str,
) -> None:
    if (
        document.get("schema") != 1
        or document.get("mode") != "external"
        or document.get("error") is not None
        or document.get("git_head") != CAPTURE_COMMIT
        or document.get("radio_uri") != RADIO_URI
        or document.get("radio_serial") != RADIO_SERIAL
        or document.get("source_radio_uri") != SOURCE_URI
        or document.get("source_radio_serial") != SOURCE_SERIAL
        or document.get("board_id") != BOARD_ID
        or document.get("stlink_serial") != STLINK_SERIAL
    ):
        raise CampaignError(f"{path} has an unexpected identity or failed run header")
    configuration = document.get("configuration")
    if not isinstance(configuration, Mapping):
        raise CampaignError(f"{path} configuration is missing")
    common = _expected_common_configuration()
    if any(configuration.get(key) != value for key, value in common.items()):
        raise CampaignError(f"{path} common RF configuration differs")
    if role == "full":
        if (
            configuration.get("states") != [port]
            or configuration.get("repeats") != 1
            or configuration.get("tx_gain_db") != -55.0
        ):
            raise CampaignError(f"{path} is not a selected-only full sweep")
    elif role == "holdout":
        expected_gain_db = -55.0 if port == "ANT2" else -40.0
        if (
            configuration.get("states") != [port]
            or configuration.get("repeats") != 1
            or configuration.get("tx_gain_db") != expected_gain_db
            or configuration.get("frequencies_hz") != list(HOLDOUT_FREQUENCIES_HZ)
        ):
            raise CampaignError(f"{path} is not the exact high-band midpoint holdout")
    elif role == "qualification":
        if (
            configuration.get("states") != list(STATES)
            or configuration.get("frequencies_hz") != [QUALIFICATION_FREQUENCY_HZ]
            or configuration.get("repeats") != 5
            or configuration.get("tx_gain_db") != -40.0
        ):
            raise CampaignError(f"{path} is not the exact 5.8 GHz qualification")
    else:
        raise AssertionError(role)
    _require_mute(document.get("final_radio_mute"), f"{path} receiver final mute")
    _require_mute(document.get("final_source_radio_mute"), f"{path} source final mute")
    _require_all_off(document.get("final_selector"), f"{path} final selector")


def _transfer_from_observation(observation: Mapping[str, Any], label: str) -> complex:
    analysis = observation.get("analysis")
    transfer = analysis.get("transfer_rx2_over_rx1") if isinstance(analysis, Mapping) else None
    if not isinstance(transfer, Mapping):
        raise CampaignError(f"{label} transfer is missing")
    result = complex(
        _finite(transfer.get("real"), f"{label} transfer real"),
        _finite(transfer.get("imag"), f"{label} transfer imag"),
    )
    if abs(result) <= np.finfo(float).tiny:
        raise CampaignError(f"{label} transfer is zero")
    return result


def _validate_and_inventory_run(
    path: Path,
    *,
    role: str,
    port: str,
    hash_raw: bool,
    replay_raw: bool,
    progress: list[int],
    progress_total: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    if path.name != "run.json" or not path.is_file() or path.is_symlink():
        raise CampaignError(f"{path} is not a regular run.json")
    document = _load_json(path)
    _validate_run_header(path, document, role=role, port=port)
    observations = document.get("observations")
    if not isinstance(observations, list) or not observations:
        raise CampaignError(f"{path} has no observations")
    configuration = document["configuration"]
    expected_order = [
        (int(frequency), state, repeat)
        for frequency in configuration["frequencies_hz"]
        for state in configuration["states"]
        for repeat in range(1, int(configuration["repeats"]) + 1)
    ]
    actual_order = [
        (row.get("frequency_hz"), row.get("state"), row.get("repeat"))
        for row in observations
        if isinstance(row, Mapping)
    ]
    if actual_order != expected_order:
        raise CampaignError(f"{path} observation order/lattice differs")
    expected_names = {
        f"{frequency}-{state.lower()}-r{repeat}.npz" for frequency, state, repeat in expected_order
    }
    artifacts = sorted(path.parent.glob("*.npz"))
    if {item.name for item in artifacts} != expected_names or len(artifacts) != len(observations):
        raise CampaignError(f"{path} raw-IQ artifact set differs")
    if any(item.is_symlink() or not item.is_file() for item in artifacts):
        raise CampaignError(f"{path} raw-IQ set contains a non-regular file")

    artifact_rows: list[dict[str, Any]] = []
    maximum_replay_delta = 0.0
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping) or observation.get("analysis_error") is not None:
            raise CampaignError(f"{path} observation {index} failed")
        frequency, state, repeat = expected_order[index]
        expected_name = f"{frequency}-{state.lower()}-r{repeat}.npz"
        if observation.get("iq_file") != expected_name:
            raise CampaignError(f"{path} observation {index} IQ filename differs")
        _require_mute(observation.get("post_capture_mute"), f"{path} observation {index} RX")
        _require_mute(
            observation.get("post_capture_source_mute"),
            f"{path} observation {index} source",
        )
        _require_all_off(
            observation.get("selector_after"), f"{path} observation {index} selector after"
        )
        stored = _transfer_from_observation(observation, f"{path} observation {index}")
        iq_path = path.parent / expected_name
        artifact = {"name": expected_name, "size_bytes": iq_path.stat().st_size}
        if hash_raw:
            artifact["sha256"] = _sha256_path(iq_path)
        if replay_raw:
            replay, peaks, rms = raw_analyzer._replay_transfer(iq_path)
            replay_complex = complex(replay["real"], replay["imag"])
            delta = abs(replay_complex - stored)
            maximum_replay_delta = max(maximum_replay_delta, delta)
            if not np.isclose(replay_complex, stored, rtol=1e-11, atol=1e-12):
                raise CampaignError(f"{path} observation {index} fails raw-IQ replay")
            analysis = observation["analysis"]
            if not np.allclose(
                peaks, analysis["peak_component_counts"], rtol=1e-7, atol=1e-7
            ) or not np.allclose(rms, analysis["rms_counts"], rtol=1e-7, atol=1e-7):
                raise CampaignError(f"{path} observation {index} replay statistics differ")
        artifact_rows.append(artifact)
        progress[0] += 1
        if progress[0] % 100 == 0 or progress[0] == progress_total:
            print(f"raw_validation_progress={progress[0]}/{progress_total}", flush=True)

    quality = {
        "minimum_phase_step_coherence": min(
            _finite(row["analysis"]["pilot"]["phase_step_coherence"], "coherence")
            for row in observations
        ),
        "maximum_peak_component_counts": max(
            max(float(value) for value in row["analysis"]["peak_component_counts"])
            for row in observations
        ),
        "maximum_raw_replay_absolute_delta": maximum_replay_delta if replay_raw else None,
    }
    inventory = {
        "relative_path": str(path.relative_to(path.parents[2])),
        "absolute_path": str(path),
        "run_id": document["run_id"],
        "role": role,
        "port": port,
        "observation_count": len(observations),
        "raw_iq_bytes": sum(item["size_bytes"] for item in artifact_rows),
        "run_json_sha256": _sha256_path(path),
        "artifact_manifest_sha256": _canonical_sha256(artifact_rows),
        "raw_sha256_included": hash_raw,
        "raw_replay_completed": replay_raw,
        "quality": quality,
        "first_capture_started_utc": observations[0]["radio_readback"]["started_utc"],
        "last_capture_completed_utc": observations[-1]["radio_readback"]["completed_utc"],
    }
    return document, inventory


def _pchip_derivatives(x: np.ndarray, values: np.ndarray) -> np.ndarray:
    if x.ndim != 1 or values.ndim != 1 or x.size != values.size or x.size < 3:
        raise ValueError("PCHIP requires equal one-dimensional arrays with at least three points")
    h = np.diff(x)
    if np.any(h <= 0.0):
        raise ValueError("PCHIP knots must be strictly increasing")
    delta = np.diff(values) / h
    derivatives = np.zeros_like(values)
    for index in range(1, values.size - 1):
        left = delta[index - 1]
        right = delta[index]
        if left * right > 0.0:
            weight_one = 2.0 * h[index] + h[index - 1]
            weight_two = h[index] + 2.0 * h[index - 1]
            derivatives[index] = (weight_one + weight_two) / (
                weight_one / left + weight_two / right
            )

    def endpoint(h0: float, h1: float, delta0: float, delta1: float) -> float:
        value = ((2.0 * h0 + h1) * delta0 - h0 * delta1) / (h0 + h1)
        if value * delta0 <= 0.0:
            return 0.0
        if delta0 * delta1 < 0.0 and abs(value) > 3.0 * abs(delta0):
            return 3.0 * delta0
        return value

    derivatives[0] = endpoint(h[0], h[1], delta[0], delta[1])
    derivatives[-1] = endpoint(h[-1], h[-2], delta[-1], delta[-2])
    return derivatives


def pchip_interpolate(x: np.ndarray, values: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Evaluate shape-preserving cubic Hermite interpolation without SciPy."""
    derivatives = _pchip_derivatives(x, values)
    indices = np.searchsorted(x, query, side="right") - 1
    indices = np.clip(indices, 0, x.size - 2)
    if np.any(query < x[0]) or np.any(query > x[-1]):
        raise ValueError("PCHIP extrapolation is forbidden")
    h = x[indices + 1] - x[indices]
    t = (query - x[indices]) / h
    h00 = 2.0 * t**3 - 3.0 * t**2 + 1.0
    h10 = t**3 - 2.0 * t**2 + t
    h01 = -2.0 * t**3 + 3.0 * t**2
    h11 = t**3 - t**2
    return (
        h00 * values[indices]
        + h10 * h * derivatives[indices]
        + h01 * values[indices + 1]
        + h11 * h * derivatives[indices + 1]
    )


def _prediction_metrics(
    prediction: np.ndarray,
    actual: np.ndarray,
    frequency_hz: np.ndarray,
) -> tuple[dict[str, float], np.ndarray, np.ndarray, np.ndarray]:
    phase_error_deg = np.rad2deg(np.unwrap(np.angle(prediction / actual)))
    magnitude_error_db = 20.0 * np.log10(np.abs(prediction) / np.abs(actual))
    centered_ghz = (frequency_hz - np.mean(frequency_hz)) / 1e9
    design = np.column_stack((np.ones_like(centered_ghz), centered_ghz))
    offset_deg, slope_deg_per_ghz = np.linalg.lstsq(design, phase_error_deg, rcond=None)[0]
    detrended = phase_error_deg - design @ np.asarray([offset_deg, slope_deg_per_ghz])
    metrics = {
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(phase_error_deg)))),
        "phase_p95_abs_deg": float(np.percentile(np.abs(phase_error_deg), 95.0)),
        "phase_max_abs_deg": float(np.max(np.abs(phase_error_deg))),
        "phase_affine_offset_deg": float(offset_deg),
        "phase_affine_slope_deg_per_ghz": float(slope_deg_per_ghz),
        "phase_affine_delay_delta_ns": float(-slope_deg_per_ghz / 360.0),
        "phase_affine_detrended_rms_deg": float(np.sqrt(np.mean(np.square(detrended)))),
        "phase_affine_detrended_p95_abs_deg": float(np.percentile(np.abs(detrended), 95.0)),
        "phase_affine_detrended_max_abs_deg": float(np.max(np.abs(detrended))),
        "magnitude_rms_db": float(np.sqrt(np.mean(np.square(magnitude_error_db)))),
        "magnitude_p95_abs_db": float(np.percentile(np.abs(magnitude_error_db), 95.0)),
        "magnitude_max_abs_db": float(np.max(np.abs(magnitude_error_db))),
    }
    return metrics, phase_error_deg, magnitude_error_db, detrended


def _interpolation_analysis(
    frequency_hz: np.ndarray,
    training: np.ndarray,
    holdout_frequency_hz: np.ndarray,
    holdout: np.ndarray,
) -> tuple[dict[str, dict[str, float]], dict[str, np.ndarray]]:
    magnitude_db = 20.0 * np.log10(np.abs(training))
    phase_rad = np.unwrap(np.angle(training))
    right = np.searchsorted(frequency_hz, holdout_frequency_hz)
    left = right - 1
    if (
        np.any(left < 0)
        or np.any(right >= frequency_hz.size)
        or not np.array_equal(
            (frequency_hz[left] + frequency_hz[right]) / 2.0, holdout_frequency_hz
        )
    ):
        raise CampaignError("holdout grid is not exactly interstitial")
    complex_linear = (training[left] + training[right]) / 2.0
    logphase_linear = 10.0 ** (((magnitude_db[left] + magnitude_db[right]) / 2.0) / 20.0) * np.exp(
        1j * (phase_rad[left] + phase_rad[right]) / 2.0
    )
    logphase_pchip = 10.0 ** (
        pchip_interpolate(frequency_hz, magnitude_db, holdout_frequency_hz) / 20.0
    ) * np.exp(1j * pchip_interpolate(frequency_hz, phase_rad, holdout_frequency_hz))
    predictions = {
        "complex_linear": complex_linear,
        "logphase_linear": logphase_linear,
        "logphase_pchip": logphase_pchip,
    }
    metrics: dict[str, dict[str, float]] = {}
    errors: dict[str, np.ndarray] = {}
    for method, prediction in predictions.items():
        score, phase_error, magnitude_error, detrended = _prediction_metrics(
            prediction, holdout, holdout_frequency_hz
        )
        metrics[method] = score
        errors[f"{method}_phase_deg"] = phase_error
        errors[f"{method}_magnitude_db"] = magnitude_error
        errors[f"{method}_phase_detrended_deg"] = detrended
    return metrics, errors


def _fit_delay(
    frequency_hz: np.ndarray, transfer: np.ndarray
) -> tuple[dict[str, float], np.ndarray, np.ndarray]:
    phase_rad = np.unwrap(np.angle(transfer))
    centered = frequency_hz - np.mean(frequency_hz)
    design = np.column_stack((np.ones_like(centered), centered))
    intercept, slope = np.linalg.lstsq(design, phase_rad, rcond=None)[0]
    fitted = design @ np.asarray([intercept, slope])
    residual_deg = np.rad2deg(phase_rad - fitted)
    return (
        {
            "delay_ns": float(-slope / (2.0 * math.pi) * 1e9),
            "phase_at_band_center_deg": float(np.rad2deg(intercept)),
            "residual_rms_deg": float(np.sqrt(np.mean(np.square(residual_deg)))),
            "residual_p95_abs_deg": float(np.percentile(np.abs(residual_deg), 95.0)),
            "residual_max_abs_deg": float(np.max(np.abs(residual_deg))),
        },
        fitted,
        residual_deg,
    )


def _wrap_deg(values: np.ndarray | float) -> np.ndarray | float:
    return (values + 180.0) % 360.0 - 180.0


def _annotated_heatmap(
    axis: plt.Axes,
    values: np.ndarray,
    *,
    xlabels: Sequence[str],
    ylabels: Sequence[str],
    title: str,
    colorbar_label: str,
    cmap: str = "coolwarm",
    center_zero: bool = True,
    fmt: str = ".1f",
) -> None:
    if center_zero:
        extent = max(float(np.max(np.abs(values))), np.finfo(float).eps)
        image = axis.imshow(values, cmap=cmap, vmin=-extent, vmax=extent, aspect="auto")
    else:
        image = axis.imshow(values, cmap=cmap, aspect="auto")
    axis.set_xticks(range(len(xlabels)), xlabels, rotation=45, ha="right")
    axis.set_yticks(range(len(ylabels)), ylabels)
    axis.set_title(title)
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            axis.text(
                column,
                row,
                format(float(values[row, column]), fmt),
                ha="center",
                va="center",
                fontsize=7,
                color="black",
            )
    axis.figure.colorbar(image, ax=axis, label=colorbar_label, shrink=0.82)


def _save_figure(figure: plt.Figure, path: Path) -> None:
    figure.savefig(path, dpi=180, metadata={"Software": "smateway"})
    plt.close(figure)


def _draw_fixture(path: Path) -> None:
    figure, axis = plt.subplots(figsize=(14, 7), constrained_layout=True)
    axis.set_xlim(0, 14)
    axis.set_ylim(0, 8)
    axis.axis("off")

    def box(x: float, y: float, width: float, height: float, text: str, color: str) -> None:
        axis.add_patch(
            plt.Rectangle((x, y), width, height, facecolor=color, edgecolor="black", lw=1.4)
        )
        axis.text(x + width / 2.0, y + height / 2.0, text, ha="center", va="center")

    def arrow(x1: float, y1: float, x2: float, y2: float, label: str = "") -> None:
        axis.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops={"arrowstyle": "->"})
        if label:
            axis.text((x1 + x2) / 2.0, (y1 + y2) / 2.0 + 0.2, label, ha="center")

    box(0.3, 5.8, 1.7, 1.0, "source Pluto .173\nTX1", "#ffd8a8")
    box(2.7, 5.8, 1.7, 1.0, "2-way splitter", "#e7f5ff")
    box(5.2, 6.7, 1.6, 0.8, "10 dB pad", "#e7f5ff")
    box(7.6, 6.5, 2.0, 1.2, "receiver Pluto .15\nRX1 reference", "#d3f9d8")
    box(5.2, 4.6, 2.0, 1.0, "fixed-shape\ndirect cable", "#fff3bf")
    box(8.0, 4.2, 2.4, 1.7, "PCB ANTn launch\nselector path\ncommon launch", "#ffc9c9")
    box(11.5, 4.5, 2.0, 1.2, "receiver Pluto .15\nRX2", "#d3f9d8")
    box(2.5, 1.4, 2.0, 1.1, "8-way splitter\ninput: 50 ohm", "#e7f5ff")
    box(
        6.0,
        0.8,
        3.0,
        2.1,
        "outputs except n remain on\nother PCB inputs\n\n"
        "output n: disconnected\nand terminated 50 ohm",
        "#f1f3f5",
    )
    box(0.3, 3.3, 1.7, 0.9, "source TX2\nmuted + 50 ohm", "#f1f3f5")
    arrow(2.0, 6.3, 2.7, 6.3)
    arrow(4.4, 6.3, 5.2, 7.1)
    arrow(6.8, 7.1, 7.6, 7.1)
    arrow(4.4, 6.1, 5.2, 5.1)
    arrow(7.2, 5.1, 8.0, 5.1, "one port at a time")
    arrow(10.4, 5.1, 11.5, 5.1, "PCB common")
    arrow(4.5, 2.0, 6.0, 2.0)
    axis.text(
        7.0,
        3.45,
        "RX2/RX1 removes common source amplitude and oscillator phase;\n"
        "ANTn changes only after safe mute + ALL_OFF.",
        ha="center",
        va="center",
        fontsize=11,
    )
    axis.text(
        7.0,
        0.15,
        "Topology and cable placement are operator-attested, not encoded in run.json.",
        ha="center",
        fontsize=10,
        color="#9c2f2f",
    )
    figure.suptitle("Controlled PCB direct-injection fixture and calibration reference planes")
    _save_figure(figure, path)


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise CampaignError(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


def _circular_layout(port_order: Sequence[str], maximum_frequency_hz: float) -> dict[str, Any]:
    count = len(port_order)
    wavelength_m = LIGHT_SPEED_M_S / maximum_frequency_hz
    adjacent_spacing_m = wavelength_m / 2.0
    radius_m = adjacent_spacing_m / (2.0 * math.sin(math.pi / count))
    elements = []
    for index, port in enumerate(port_order):
        bearing_deg = index * 360.0 / count
        bearing_rad = math.radians(bearing_deg)
        elements.append(
            {
                "port": port,
                "bearing_deg_clockwise_from_forward": bearing_deg,
                "position_mm": {
                    "x_right": radius_m * math.sin(bearing_rad) * 1e3,
                    "y_forward": radius_m * math.cos(bearing_rad) * 1e3,
                },
            }
        )
    return {
        "element_count": count,
        "maximum_frequency_hz": int(maximum_frequency_hz),
        "wavelength_at_maximum_frequency_mm": wavelength_m * 1e3,
        "adjacent_spacing_mm": adjacent_spacing_m * 1e3,
        "radius_mm": radius_m * 1e3,
        "diameter_mm": radius_m * 2e3,
        "elements_clockwise_from_forward": elements,
    }


def _array_bias_rms_deg(
    phase_error_deg: np.ndarray,
    frequency_hz: np.ndarray,
    positions_m: np.ndarray,
    bearings_deg: np.ndarray,
    *,
    ula: bool,
) -> np.ndarray:
    result: list[float] = []
    errors_rad = np.deg2rad(phase_error_deg)
    for bearing_deg in bearings_deg:
        bearing = math.radians(float(bearing_deg))
        per_frequency: list[float] = []
        for index, frequency in enumerate(frequency_hz):
            wave_number = 2.0 * math.pi * frequency / LIGHT_SPEED_M_S
            if ula:
                derivative = wave_number * positions_m[:, 0] * math.cos(bearing)
            else:
                derivative = wave_number * (
                    -positions_m[:, 0] * math.sin(bearing) + positions_m[:, 1] * math.cos(bearing)
                )
            derivative -= np.mean(derivative)
            error = errors_rad[: positions_m.shape[0], index]
            error = error - np.mean(error)
            per_frequency.append(float(np.dot(derivative, error) / np.dot(derivative, derivative)))
        result.append(float(np.rad2deg(np.sqrt(np.mean(np.square(per_frequency))))))
    return np.asarray(result)


def _uca8_leakage_bias_deg(
    mixing_matrix: np.ndarray,
    positions_m: np.ndarray,
    *,
    frequency_hz: float,
    bearings_deg: np.ndarray,
) -> np.ndarray:
    """Estimate ideal-manifold bearing bias from the measured 5.8 GHz leakage screen."""
    wave_number = 2.0 * math.pi * frequency_hz / LIGHT_SPEED_M_S
    search_deg = np.arange(0.0, 360.0, 0.25)
    search_rad = np.deg2rad(search_deg)
    steering = np.exp(
        -1j
        * wave_number
        * (
            positions_m[:, 0, None] * np.cos(search_rad)[None, :]
            + positions_m[:, 1, None] * np.sin(search_rad)[None, :]
        )
    )
    result: list[float] = []
    for bearing_deg in bearings_deg:
        bearing = math.radians(float(bearing_deg) % 360.0)
        ideal = np.exp(
            -1j
            * wave_number
            * (positions_m[:, 0] * math.cos(bearing) + positions_m[:, 1] * math.sin(bearing))
        )
        measured = mixing_matrix @ ideal
        score = np.abs(np.conjugate(steering).T @ measured)
        estimate = float(search_deg[int(np.argmax(score))])
        result.append(float((estimate - bearing_deg + 180.0) % 360.0 - 180.0))
    return np.asarray(result)


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    output_dir = (
        (repository / args.output_dir).resolve()
        if not args.output_dir.is_absolute()
        else args.output_dir
    )
    data_dir = output_dir / "data"
    figure_dir = output_dir / "png"
    data_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    expected_total = sum(
        len(_load_json(args.data_root / relative)["observations"])
        for port in PORTS
        for role in ("full", "holdout", "qualification")
        for relative in RUNS[port][role]
    )
    progress = [0]
    documents: dict[str, dict[str, list[dict[str, Any]]]] = {}
    inventory: list[dict[str, Any]] = []
    for port in PORTS:
        documents[port] = {}
        for role in ("full", "holdout", "qualification"):
            documents[port][role] = []
            for relative in RUNS[port][role]:
                document, item = _validate_and_inventory_run(
                    args.data_root / relative,
                    role=role,
                    port=port,
                    hash_raw=not args.skip_raw_hash,
                    replay_raw=not args.skip_raw_replay,
                    progress=progress,
                    progress_total=expected_total,
                )
                documents[port][role].append(document)
                inventory.append(item)

    frequency_hz = np.asarray(FULL_FREQUENCIES_HZ, dtype=np.float64)
    holdout_frequency_hz = np.asarray(HOLDOUT_FREQUENCIES_HZ, dtype=np.float64)
    full_transfer = np.zeros((len(PORTS), frequency_hz.size), dtype=np.complex128)
    full_coherence = np.zeros((len(PORTS), frequency_hz.size), dtype=np.float64)
    full_peak_counts = np.zeros((len(PORTS), frequency_hz.size), dtype=np.float64)
    holdout_transfer = np.zeros((len(PORTS), holdout_frequency_hz.size), dtype=np.complex128)
    quality_rows: dict[str, dict[str, Any]] = {}
    qualification_relative_db = np.zeros((len(PORTS), len(STATES)))
    qualification_complex = np.zeros((len(PORTS), len(STATES)), dtype=np.complex128)
    holdout_metrics: dict[str, dict[str, dict[str, float]]] = {}
    holdout_errors: dict[str, dict[str, np.ndarray]] = {}

    for port_index, port in enumerate(PORTS):
        full_by_frequency: dict[int, complex] = {}
        full_observation_by_frequency: dict[int, dict[str, Any]] = {}
        full_observations: list[dict[str, Any]] = []
        for document in documents[port]["full"]:
            for observation in document["observations"]:
                value = int(observation["frequency_hz"])
                if value in full_by_frequency:
                    raise CampaignError(f"{port} full lattice duplicates {value}")
                full_by_frequency[value] = _transfer_from_observation(observation, port)
                full_observation_by_frequency[value] = observation
                full_observations.append(observation)
        if tuple(sorted(full_by_frequency)) != FULL_FREQUENCIES_HZ:
            raise CampaignError(f"{port} full 12.5 MHz lattice is incomplete")
        full_transfer[port_index] = [full_by_frequency[value] for value in FULL_FREQUENCIES_HZ]
        full_coherence[port_index] = [
            full_observation_by_frequency[value]["analysis"]["pilot"]["phase_step_coherence"]
            for value in FULL_FREQUENCIES_HZ
        ]
        full_peak_counts[port_index] = [
            max(full_observation_by_frequency[value]["analysis"]["peak_component_counts"])
            for value in FULL_FREQUENCIES_HZ
        ]

        holdout_document = documents[port]["holdout"][0]
        holdout_by_frequency = {
            int(observation["frequency_hz"]): _transfer_from_observation(observation, port)
            for observation in holdout_document["observations"]
        }
        if tuple(sorted(holdout_by_frequency)) != HOLDOUT_FREQUENCIES_HZ:
            raise CampaignError(f"{port} holdout lattice is incomplete")
        holdout_transfer[port_index] = [
            holdout_by_frequency[value] for value in HOLDOUT_FREQUENCIES_HZ
        ]

        qualification = documents[port]["qualification"][0]
        means: dict[str, float] = {}
        standard_deviations: dict[str, float] = {}
        phase_standard_deviations: dict[str, float] = {}
        for state_index, state in enumerate(STATES):
            state_observations = [
                observation
                for observation in qualification["observations"]
                if observation["state"] == state
            ]
            values = np.asarray(
                [
                    observation["analysis"]["transfer_rx2_over_rx1"]["magnitude_db"]
                    for observation in state_observations
                ],
                dtype=np.float64,
            )
            complex_values = np.asarray(
                [
                    _transfer_from_observation(observation, f"{port} qualification {state}")
                    for observation in state_observations
                ],
                dtype=np.complex128,
            )
            means[state] = float(np.mean(values))
            standard_deviations[state] = float(np.std(values, ddof=1))
            mean_complex = complex(np.mean(complex_values))
            qualification_complex[port_index, state_index] = mean_complex
            phase_standard_deviations[state] = float(
                np.std(np.rad2deg(np.angle(complex_values / mean_complex)), ddof=1)
            )
        qualification_relative_db[port_index] = 20.0 * np.log10(
            np.abs(qualification_complex[port_index])
            / abs(qualification_complex[port_index, STATES.index(port)])
        )
        strongest_wrong = max((state for state in PORTS if state != port), key=means.__getitem__)
        all_observations = [*full_observations, *holdout_document["observations"]]
        quality_rows[port] = {
            "full_observation_count": len(full_observations),
            "holdout_observation_count": len(holdout_document["observations"]),
            "minimum_phase_step_coherence": min(
                float(row["analysis"]["pilot"]["phase_step_coherence"]) for row in all_observations
            ),
            "maximum_peak_component_counts": max(
                max(float(value) for value in row["analysis"]["peak_component_counts"])
                for row in all_observations
            ),
            "qualification_selected_mean_db": means[port],
            "qualification_selected_sd_db": standard_deviations[port],
            "qualification_selected_phase_sd_deg": phase_standard_deviations[port],
            "qualification_selected_over_all_off_db": means[port] - means["ALL_OFF"],
            "qualification_strongest_wrong_state": strongest_wrong,
            "qualification_selected_over_strongest_wrong_db": (
                means[port] - means[strongest_wrong]
            ),
        }
        metrics, errors = _interpolation_analysis(
            frequency_hz,
            full_transfer[port_index],
            holdout_frequency_hz,
            holdout_transfer[port_index],
        )
        holdout_metrics[port] = metrics
        holdout_errors[port] = errors

    reference_index = PORTS.index(DISPLAY_REFERENCE_PORT)
    reference = full_transfer[reference_index]
    relative = full_transfer / reference[None, :]
    relative_gain_db = 20.0 * np.log10(np.abs(relative))
    relative_phase_unwrapped_deg = np.rad2deg(np.unwrap(np.angle(relative), axis=1))
    correction_gain_db = np.mean(relative_gain_db, axis=0)[None, :] - relative_gain_db
    correction_phase_unwrapped_deg = (
        np.mean(relative_phase_unwrapped_deg, axis=0)[None, :] - relative_phase_unwrapped_deg
    )
    correction = 10.0 ** (correction_gain_db / 20.0) * np.exp(
        1j * np.deg2rad(correction_phase_unwrapped_deg)
    )

    at_5g8 = int(np.flatnonzero(frequency_hz == QUALIFICATION_FREQUENCY_HZ)[0])
    transfer_5g8 = full_transfer[:, at_5g8]
    gain_5g8 = 20.0 * np.log10(np.abs(transfer_5g8))
    pairwise_gain_5g8 = gain_5g8[:, None] - gain_5g8[None, :]
    pairwise_phase_5g8 = np.asarray(
        _wrap_deg(np.rad2deg(np.angle(transfer_5g8[:, None] / transfer_5g8[None, :])))
    )
    qualification_active = qualification_complex[:, 1:]
    qualification_normalized = qualification_active / np.diag(qualification_active)[:, None]
    mixing_matrix_5g8 = qualification_normalized.T
    mixing_singular_values = np.linalg.svd(mixing_matrix_5g8, compute_uv=False)
    mixing_condition_number = float(mixing_singular_values[0] / mixing_singular_values[-1])
    qualification_phase_deg = np.asarray(_wrap_deg(np.rad2deg(np.angle(qualification_normalized))))

    delay_metrics: dict[str, dict[str, dict[str, float]]] = {}
    delay_fit = np.zeros_like(relative_phase_unwrapped_deg)
    delay_residual = np.zeros_like(relative_phase_unwrapped_deg)
    for port_index, port in enumerate(PORTS):
        delay_metrics[port] = {}
        for band_name, first, last in (
            ("full", 0.5e9, 6.0e9),
            ("5_to_6_ghz", 5.0e9, 6.0e9),
            ("5p7_to_5p9_ghz", 5.7e9, 5.9e9),
        ):
            selected = (frequency_hz >= first) & (frequency_hz <= last)
            metrics, fitted, residual = _fit_delay(
                frequency_hz[selected], relative[port_index, selected]
            )
            delay_metrics[port][band_name] = metrics
            if band_name == "full":
                delay_fit[port_index, selected] = np.rad2deg(fitted)
                delay_residual[port_index, selected] = residual

    high_selected = (frequency_hz >= 5e9) & (frequency_hz <= 6e9)
    pairwise_delay_5_to_6 = np.zeros((len(PORTS), len(PORTS)))
    pairwise_residual_5_to_6 = np.zeros_like(pairwise_delay_5_to_6)
    for row in range(len(PORTS)):
        for column in range(len(PORTS)):
            metrics, _, _ = _fit_delay(
                frequency_hz[high_selected],
                full_transfer[row, high_selected] / full_transfer[column, high_selected],
            )
            pairwise_delay_5_to_6[row, column] = metrics["delay_ns"]
            pairwise_residual_5_to_6[row, column] = metrics["residual_rms_deg"]

    method_names = ("complex_linear", "logphase_linear", "logphase_pchip")
    pchip_phase_error = np.asarray(
        [holdout_errors[port]["logphase_pchip_phase_deg"] for port in PORTS]
    )
    pchip_gain_error = np.asarray(
        [holdout_errors[port]["logphase_pchip_magnitude_db"] for port in PORTS]
    )
    pchip_spatial_phase_error = pchip_phase_error - np.mean(pchip_phase_error, axis=0)
    spatial_phase_rms = float(np.sqrt(np.mean(np.square(pchip_spatial_phase_error))))
    spatial_phase_p95 = float(np.percentile(np.abs(pchip_spatial_phase_error), 95.0))
    spatial_phase_max = float(np.max(np.abs(pchip_spatial_phase_error)))

    wavelength_5g8_m = LIGHT_SPEED_M_S / QUALIFICATION_FREQUENCY_HZ
    ula_spacing_m = wavelength_5g8_m / 2.0
    ula_positions = np.column_stack(((np.arange(8) - 3.5) * ula_spacing_m, np.zeros(8)))
    c6_angles = np.deg2rad(np.arange(6) * 60.0 + 90.0)
    c6_positions = np.column_stack((0.0255 * np.cos(c6_angles), 0.0255 * np.sin(c6_angles)))
    c8_radius_m = ula_spacing_m / (2.0 * math.sin(math.pi / 8.0))
    c8_angles = np.deg2rad(np.arange(8) * 45.0 + 90.0)
    c8_positions = np.column_stack(
        (c8_radius_m * np.cos(c8_angles), c8_radius_m * np.sin(c8_angles))
    )
    bearings = np.arange(-75.0, 75.1, 1.0)
    df_bias = {
        "ula8_half_wavelength": _array_bias_rms_deg(
            pchip_spatial_phase_error,
            holdout_frequency_hz,
            ula_positions,
            bearings,
            ula=True,
        ),
        "c6_51mm_diameter": _array_bias_rms_deg(
            pchip_spatial_phase_error[:6],
            holdout_frequency_hz,
            c6_positions,
            bearings,
            ula=False,
        ),
        "c8_half_wavelength_chord": _array_bias_rms_deg(
            pchip_spatial_phase_error,
            holdout_frequency_hz,
            c8_positions,
            bearings,
            ula=False,
        ),
    }
    leakage_bearings = np.arange(0.0, 360.0, 1.0)
    leakage_bias = _uca8_leakage_bias_deg(
        mixing_matrix_5g8,
        c8_positions,
        frequency_hz=float(QUALIFICATION_FREQUENCY_HZ),
        bearings_deg=leakage_bearings,
    )
    recommended_c6 = _circular_layout(C6_RECOMMENDED_PORTS, float(ARRAY_DESIGN_MAX_FREQUENCY_HZ))
    recommended_c8 = _circular_layout(C8_RECOMMENDED_PORTS, float(ARRAY_DESIGN_MAX_FREQUENCY_HZ))
    array_recommendations = {
        "schema": 1,
        "status": "engineering_recommendation_requires_installed_array_ota_qualification",
        "scope": (
            "first 5.8 GHz circular-array build, mechanically sized to avoid spatial aliasing "
            "through the board LUT upper support at 6.0 GHz"
        ),
        "coordinate_system": {
            "origin": "surveyed array center",
            "x_positive": "right when viewed from above",
            "y_positive": "forward",
            "bearing_zero": "forward through the first listed element",
            "bearing_positive": "clockwise when viewed from above",
        },
        "c6": {
            **recommended_c6,
            "port_order_clockwise": list(C6_RECOMMENDED_PORTS),
            "omitted_ports": list(C6_OMITTED_PORTS),
            "port_selection_basis": {
                port: {
                    "selected_mean_db_at_5p8_ghz": quality_rows[port][
                        "qualification_selected_mean_db"
                    ],
                    "selected_over_strongest_wrong_db_at_5p8_ghz": quality_rows[port][
                        "qualification_selected_over_strongest_wrong_db"
                    ],
                    "pchip_holdout_phase_p95_abs_deg_5_to_6_ghz": holdout_metrics[port][
                        "logphase_pchip"
                    ]["phase_p95_abs_deg"],
                }
                for port in PORTS
            },
            "selection_rationale": (
                "omit ANT3 and ANT6 for the first 5.8 GHz C6 article because both are 6-9 dB "
                "weaker and have the lowest driven/wrong isolation margins; ANT3 also has the "
                "largest independent interpolation residual; place the three retained "
                "electrically matched PCB-path pairs on array diameters"
            ),
            "temporal_scan": {
                "forward_opposite_pair_order": list(C6_OPPOSITE_PAIR_SCAN),
                "reverse_order_next_cycle": list(reversed(C6_OPPOSITE_PAIR_SCAN)),
                "rationale": (
                    "sample the widest baselines close together in time and alternate direction "
                    "to expose or cancel first-order motion bias"
                ),
            },
        },
        "c8": {
            **recommended_c8,
            "port_order_clockwise": list(C8_RECOMMENDED_PORTS),
            "temporal_scan": {
                "forward_opposite_pair_order": list(C8_OPPOSITE_PAIR_SCAN),
                "reverse_order_next_cycle": list(reversed(C8_OPPOSITE_PAIR_SCAN)),
                "rationale": (
                    "sample diametric baselines close together in time and alternate direction "
                    "to expose or cancel first-order motion bias"
                ),
            },
            "weak_port_policy": (
                "retain ANT3 and ANT6 for aperture, but propagate measured per-port noise/SNR "
                "weights after complex correction; repeat ANT3 before promotion"
            ),
            "mapping_rationale": (
                "place the measured PCB-path pairs ANT1/ANT8, ANT2/ANT7, ANT3/ANT6, and "
                "ANT4/ANT5 on array diameters to minimize electronics mismatch on the longest "
                "baselines"
            ),
        },
        "calibration_stack": [
            "simultaneous RX2/RX1 temporal-reference ratio for every selector dwell",
            "board-path complex LUT C_board_i(f) from calibration-lut.json",
            "final installed cable complex correction C_cable_i(f)",
            "surveyed element coordinates and array yaw",
            "known-angle installed-array OTA steering manifold including stable coupling/patterns",
            "per-port noise covariance or SNR whitening before the direction solver",
        ],
        "board_lut_subset_regauge": (
            "optional for C6: C6_i(f) = C8_i(f) / geometric_mean_selected(C8_i(f)); this is a "
            "common complex gauge change and cannot change bearing"
        ),
        "mechanical_notes": {
            "coordinate_tolerance_target_mm": 0.25,
            "phase_error_per_mm_at_6_ghz_deg": (
                360.0 * ARRAY_DESIGN_MAX_FREQUENCY_HZ * 1e-3 / LIGHT_SPEED_M_S
            ),
            "warning": (
                "electrical phase centers, not enclosure or connector centers, define the useful "
                "coordinates; estimate residual phase-center behavior with OTA calibration"
            ),
        },
        "band_warning": (
            "a 6 GHz alias-safe circle has progressively less electrical aperture at low "
            "frequency; the board LUT bandwidth does not imply useful 0.5-6 GHz bearing "
            "resolution from one fixed compact array or one antenna type"
        ),
        "legacy_profile_warning": (
            "hexcal-v1 assumes ANT1 through ANT6 clockwise; the recommended C6 mapping requires "
            "a new profile/geometry identity and must not reuse hexcal-v1 metadata"
        ),
    }

    plt.style.use("seaborn-v0_8-whitegrid")
    colors = plt.cm.tab10(np.linspace(0.0, 0.9, len(PORTS)))
    _draw_fixture(figure_dir / FIGURES[0])

    figure, axes = plt.subplots(2, 1, figsize=(14, 8), constrained_layout=True)
    for port_index, _port in enumerate(PORTS):
        axes[0].plot(frequency_hz / 1e9, np.full(frequency_hz.size, port_index), ".", ms=1.2)
        axes[0].plot(
            holdout_frequency_hz / 1e9,
            np.full(holdout_frequency_hz.size, port_index + 0.22),
            ".",
            ms=1.8,
        )
    axes[0].set_yticks(range(len(PORTS)), PORTS)
    axes[0].set_xlabel("Frequency (GHz)")
    axes[0].set_title("Full 12.5 MHz knots and interleaved 6.25 MHz high-band holdouts")
    starts: list[datetime] = []
    for item in inventory:
        starts.append(datetime.fromisoformat(item["first_capture_started_utc"]))
    origin = min(starts)
    role_marker = {"full": "o", "holdout": "s", "qualification": "^"}
    for item in inventory:
        start = datetime.fromisoformat(item["first_capture_started_utc"])
        end = datetime.fromisoformat(item["last_capture_completed_utc"])
        y = PORTS.index(item["port"])
        x0 = (start - origin).total_seconds() / 3600.0
        x1 = (end - origin).total_seconds() / 3600.0
        axes[1].plot([x0, x1], [y, y], lw=5, solid_capstyle="butt")
        axes[1].plot(x0, y, role_marker[item["role"]], color="black", ms=4)
    axes[1].set_yticks(range(len(PORTS)), PORTS)
    axes[1].set_xlabel(f"Hours since {origin.astimezone(UTC).isoformat()}")
    axes[1].set_title("Capture chronology (circle full, square holdout, triangle qualification)")
    figure.suptitle("Campaign coverage and chronology")
    _save_figure(figure, figure_dir / FIGURES[1])

    figure, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    x = np.arange(len(PORTS))
    selected_off = [quality_rows[port]["qualification_selected_over_all_off_db"] for port in PORTS]
    selected_wrong = [
        quality_rows[port]["qualification_selected_over_strongest_wrong_db"] for port in PORTS
    ]
    axes[0, 0].bar(x - 0.18, selected_off, 0.36, label="selected / ALL_OFF")
    axes[0, 0].bar(x + 0.18, selected_wrong, 0.36, label="selected / strongest wrong")
    axes[0, 0].axhline(20.0, color="black", ls="--", label="20 dB gate")
    axes[0, 0].set_xticks(x, PORTS)
    axes[0, 0].set_ylabel("Contrast (dB)")
    axes[0, 0].legend(fontsize=8)
    axes[0, 0].set_title("5.8 GHz routing contrast")
    axes[0, 1].bar(x, [quality_rows[p]["qualification_selected_sd_db"] for p in PORTS])
    axes[0, 1].set_xticks(x, PORTS)
    axes[0, 1].set_ylabel("Selected magnitude SD (dB)")
    axes[0, 1].set_title("Five-repeat selected-state stability")
    axes[1, 0].bar(x, [quality_rows[p]["minimum_phase_step_coherence"] for p in PORTS])
    axes[1, 0].set_ylim(0.995, 1.0001)
    axes[1, 0].set_xticks(x, PORTS)
    axes[1, 0].set_ylabel("Minimum coherence")
    axes[1, 0].set_title("Worst full/holdout phase-step coherence")
    axes[1, 1].bar(x, [quality_rows[p]["maximum_peak_component_counts"] for p in PORTS])
    axes[1, 1].axhline(2047.0, color="black", ls="--", label="ADC full-scale component")
    axes[1, 1].set_xticks(x, PORTS)
    axes[1, 1].set_ylabel("Maximum absolute ADC component")
    axes[1, 1].set_title("Peak counts; no clipping")
    axes[1, 1].legend(fontsize=8)
    figure.suptitle("Acquisition quality and admission gates")
    _save_figure(figure, figure_dir / FIGURES[2])

    figure, axes = plt.subplots(1, 2, figsize=(18, 7), constrained_layout=True)
    _annotated_heatmap(
        axes[0],
        qualification_relative_db,
        xlabels=STATES,
        ylabels=PORTS,
        title="Injected physical port (row) versus selected state (column), normalized to diagonal",
        colorbar_label="Relative transfer (dB)",
        cmap="viridis",
        center_zero=False,
    )
    _annotated_heatmap(
        axes[1],
        qualification_phase_deg,
        xlabels=PORTS,
        ylabels=PORTS,
        title="Injected physical port (row) versus selected state (column)",
        colorbar_label="Phase relative to driven diagonal (degrees)",
        cmap="twilight_shifted",
    )
    figure.suptitle(
        "5.8 GHz selector isolation/leakage matrix; "
        f"active 8×8 condition number {mixing_condition_number:.2f}"
    )
    _save_figure(figure, figure_dir / FIGURES[3])

    figure, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True, constrained_layout=True)
    for index, port in enumerate(PORTS):
        axes[0].plot(
            frequency_hz / 1e9,
            20.0 * np.log10(np.abs(full_transfer[index])),
            color=colors[index],
            label=port,
        )
        axes[1].plot(
            frequency_hz / 1e9,
            relative_gain_db[index],
            color=colors[index],
            label=port,
        )
    axes[0].set_ylabel("RX2/RX1 magnitude (dB)")
    axes[0].set_title("Absolute fixture-normalized selected transfer")
    axes[0].legend(ncol=4)
    axes[1].axhline(0.0, color="black", lw=1)
    axes[1].set_ylabel(f"Port / {DISPLAY_REFERENCE_PORT} (dB)")
    axes[1].set_xlabel("Frequency (GHz)")
    axes[1].set_title("Relative PCB-path response")
    figure.suptitle("Direct-injection magnitude response")
    _save_figure(figure, figure_dir / FIGURES[4])

    figure, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True, constrained_layout=True)
    absolute_phase = np.rad2deg(np.unwrap(np.angle(full_transfer), axis=1))
    for index, port in enumerate(PORTS):
        axes[0].plot(frequency_hz / 1e9, absolute_phase[index], color=colors[index], label=port)
        axes[1].plot(
            frequency_hz / 1e9,
            relative_phase_unwrapped_deg[index],
            color=colors[index],
            label=port,
        )
    axes[0].set_ylabel("Unwrapped phase (degrees)")
    axes[0].set_title("Absolute RX2/RX1 phase (common fixture delay retained)")
    axes[0].legend(ncol=4)
    axes[1].axhline(0.0, color="black", lw=1)
    axes[1].set_ylabel(f"Port / {DISPLAY_REFERENCE_PORT} phase (degrees)")
    axes[1].set_xlabel("Frequency (GHz)")
    axes[1].set_title("Relative phase requiring board-path calibration")
    figure.suptitle("Direct-injection phase response")
    _save_figure(figure, figure_dir / FIGURES[5])

    for filename, values, label, cmap in (
        (FIGURES[6], correction_gain_db, "Correction gain (dB)", "coolwarm"),
        (
            FIGURES[7],
            np.asarray(_wrap_deg(correction_phase_unwrapped_deg)),
            "Wrapped correction phase (degrees)",
            "twilight_shifted",
        ),
    ):
        figure, axis = plt.subplots(figsize=(14, 6), constrained_layout=True)
        extent = max(float(np.max(np.abs(values))), np.finfo(float).eps)
        image = axis.imshow(
            values,
            aspect="auto",
            origin="lower",
            extent=[frequency_hz[0] / 1e9, frequency_hz[-1] / 1e9, 0.5, 8.5],
            cmap=cmap,
            vmin=-extent,
            vmax=extent,
        )
        axis.set_yticks(range(1, 9), PORTS)
        axis.set_xlabel("Frequency (GHz)")
        axis.set_ylabel("PCB port")
        axis.set_title("Apply to each selected complex sample; geometric-mean gauge")
        figure.colorbar(image, ax=axis, label=label)
        figure.suptitle(label + " LUT")
        _save_figure(figure, figure_dir / filename)

    figure, axes = plt.subplots(1, 2, figsize=(16, 7), constrained_layout=True)
    _annotated_heatmap(
        axes[0],
        pairwise_gain_5g8,
        xlabels=PORTS,
        ylabels=PORTS,
        title="Row / column gain",
        colorbar_label="dB",
    )
    _annotated_heatmap(
        axes[1],
        pairwise_phase_5g8,
        xlabels=PORTS,
        ylabels=PORTS,
        title="Row / column wrapped phase",
        colorbar_label="degrees",
        cmap="twilight_shifted",
    )
    figure.suptitle("Pairwise direct-injection response at 5.8 GHz")
    _save_figure(figure, figure_dir / FIGURES[8])

    figure, axes = plt.subplots(2, 2, figsize=(15, 10), constrained_layout=True)
    for index, port in enumerate(PORTS):
        axes[0, 0].plot(
            frequency_hz / 1e9,
            relative_phase_unwrapped_deg[index],
            color=colors[index],
            label=port,
        )
        axes[0, 0].plot(frequency_hz / 1e9, delay_fit[index], color=colors[index], ls="--")
        axes[1, 0].plot(frequency_hz / 1e9, delay_residual[index], color=colors[index], label=port)
    axes[0, 0].set_ylabel("Relative unwrapped phase (degrees)")
    axes[0, 0].set_title("Measured (solid) and one-delay fit (dashed)")
    axes[0, 0].legend(ncol=4, fontsize=8)
    axes[1, 0].set_xlabel("Frequency (GHz)")
    axes[1, 0].set_ylabel("Delay-fit residual (degrees)")
    axes[1, 0].set_title("A single delay cannot reproduce the ripple")
    _annotated_heatmap(
        axes[0, 1],
        pairwise_delay_5_to_6,
        xlabels=PORTS,
        ylabels=PORTS,
        title="5–6 GHz pairwise delay: row / column",
        colorbar_label="ns",
        fmt=".3f",
    )
    _annotated_heatmap(
        axes[1, 1],
        pairwise_residual_5_to_6,
        xlabels=PORTS,
        ylabels=PORTS,
        title="5–6 GHz pairwise delay-only residual RMS",
        colorbar_label="degrees",
        cmap="magma",
        center_zero=False,
    )
    figure.suptitle("Delay-only model diagnosis")
    _save_figure(figure, figure_dir / FIGURES[9])

    figure, axes = plt.subplots(2, 1, figsize=(14, 9), constrained_layout=True)
    width = 0.25
    for method_index, method in enumerate(method_names):
        axes[0].bar(
            x + (method_index - 1) * width,
            [holdout_metrics[p][method]["phase_rms_deg"] for p in PORTS],
            width,
            label=method,
        )
        axes[1].bar(
            x + (method_index - 1) * width,
            [holdout_metrics[p][method]["magnitude_rms_db"] for p in PORTS],
            width,
            label=method,
        )
    axes[0].set_xticks(x, PORTS)
    axes[0].set_ylabel("Phase RMS (degrees)")
    axes[0].set_title("Independent interleaved-frequency phase error")
    axes[0].legend(ncol=3)
    axes[1].set_xticks(x, PORTS)
    axes[1].set_ylabel("Magnitude RMS (dB)")
    axes[1].set_title("Independent interleaved-frequency magnitude error")
    figure.suptitle("12.5 MHz LUT interpolation: model comparison by port")
    _save_figure(figure, figure_dir / FIGURES[10])

    figure, axes = plt.subplots(2, 1, figsize=(14, 8), constrained_layout=True)
    for axis, values, label, cmap in (
        (axes[0], pchip_phase_error, "Phase error (degrees)", "coolwarm"),
        (axes[1], pchip_gain_error, "Magnitude error (dB)", "coolwarm"),
    ):
        extent = max(float(np.max(np.abs(values))), np.finfo(float).eps)
        image = axis.imshow(
            values,
            aspect="auto",
            origin="lower",
            extent=[
                holdout_frequency_hz[0] / 1e9,
                holdout_frequency_hz[-1] / 1e9,
                0.5,
                8.5,
            ],
            cmap=cmap,
            vmin=-extent,
            vmax=extent,
        )
        axis.set_yticks(range(1, 9), PORTS)
        axis.set_ylabel("PCB port")
        figure.colorbar(image, ax=axis, label=label)
    axes[1].set_xlabel("Holdout frequency (GHz)")
    axes[0].set_title("Log-phase PCHIP phase residual")
    axes[1].set_title("Log-phase PCHIP gain residual")
    figure.suptitle("Independent 6.25 MHz midpoint residual heatmaps")
    _save_figure(figure, figure_dir / FIGURES[11])

    figure, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    aggregate_phase = []
    aggregate_gain = []
    for method in method_names:
        aggregate_phase.append(
            np.concatenate([holdout_errors[p][f"{method}_phase_deg"] for p in PORTS])
        )
        aggregate_gain.append(
            np.concatenate([holdout_errors[p][f"{method}_magnitude_db"] for p in PORTS])
        )
    axes[0].boxplot(aggregate_phase, tick_labels=method_names, showfliers=False)
    axes[0].tick_params(axis="x", rotation=25)
    axes[0].set_ylabel("Phase error (degrees)")
    axes[0].set_title("All 640 holdout cells")
    axes[1].boxplot(aggregate_gain, tick_labels=method_names, showfliers=False)
    axes[1].tick_params(axis="x", rotation=25)
    axes[1].set_ylabel("Magnitude error (dB)")
    axes[1].set_title("All 640 holdout cells")
    sorted_absolute = np.sort(np.abs(pchip_spatial_phase_error.ravel()))
    axes[2].plot(sorted_absolute, np.arange(1, sorted_absolute.size + 1) / sorted_absolute.size)
    axes[2].axvline(
        spatial_phase_p95,
        color="black",
        ls="--",
        label=f"p95 {spatial_phase_p95:.2f}°",
    )
    axes[2].set_xlabel("Common-mode-removed |phase error| (degrees)")
    axes[2].set_ylabel("Empirical CDF")
    axes[2].set_title("Direction-finding-relevant spatial residual")
    axes[2].legend()
    figure.suptitle("Model choice and residual distribution")
    _save_figure(figure, figure_dir / FIGURES[12])

    figure, axes = plt.subplots(1, 3, figsize=(21, 7), constrained_layout=True)
    layout_styles = (
        (ula_positions, "ULA8 λ/2 at 5.8 GHz", "o"),
        (c6_positions, "C6 / 51 mm diameter", "s"),
        (c8_positions, "C8 / λ/2 chord at 5.8 GHz", "^"),
    )
    for positions, label, marker in layout_styles:
        axes[0].plot(positions[:, 0] * 1e3, positions[:, 1] * 1e3, marker + "-", label=label)
    axes[0].set_aspect("equal")
    axes[0].set_xlabel("x (mm)")
    axes[0].set_ylabel("y (mm)")
    axes[0].set_title("Candidate array geometries (port order follows markers)")
    axes[0].legend()
    for name, values in df_bias.items():
        axes[1].plot(bearings, values, label=name.replace("_", " "))
    axes[1].set_yscale("log")
    axes[1].set_xlabel("True bearing from broadside/forward (degrees)")
    axes[1].set_ylabel("Linearized RMS bearing bias (degrees)")
    axes[1].set_title("Empirical PCHIP residual propagated through ideal manifolds")
    axes[1].legend()
    axes[2].plot(leakage_bearings, leakage_bias)
    axes[2].axhline(0.0, color="black", lw=1)
    axes[2].set_xlabel("True C8 bearing (degrees)")
    axes[2].set_ylabel("Ideal-manifold estimate bias (degrees)")
    axes[2].set_title(
        f"5.8 GHz measured leakage screen\nmax |bias| {np.max(np.abs(leakage_bias)):.2f}°"
    )
    figure.suptitle("Direction-finding geometry and calibration-only error budget")
    _save_figure(figure, figure_dir / FIGURES[13])

    figure, axes = plt.subplots(1, 3, figsize=(21, 7), constrained_layout=True)
    for axis, layout, title in (
        (axes[0], recommended_c6, "Recommended C6 port map"),
        (axes[1], recommended_c8, "Recommended C8 port map"),
    ):
        elements = layout["elements_clockwise_from_forward"]
        positions = np.asarray(
            [
                [item["position_mm"]["x_right"], item["position_mm"]["y_forward"]]
                for item in elements
            ]
        )
        closed = np.vstack((positions, positions[0]))
        axis.plot(closed[:, 0], closed[:, 1], "-", color="0.6", lw=1.5)
        axis.scatter(positions[:, 0], positions[:, 1], s=130, c=np.arange(len(elements)))
        for item, position in zip(elements, positions, strict=True):
            axis.annotate(
                f"{item['port']}\n{item['bearing_deg_clockwise_from_forward']:.0f}°",
                position,
                xytext=(0, 12),
                textcoords="offset points",
                ha="center",
                fontsize=10,
            )
        axis.annotate(
            "forward",
            xy=(0.0, layout["radius_mm"] * 0.65),
            xytext=(0.0, layout["radius_mm"] * 0.18),
            arrowprops={"arrowstyle": "->", "lw": 1.5},
            ha="center",
        )
        axis.set_xlim(-layout["radius_mm"] * 1.35, layout["radius_mm"] * 1.35)
        axis.set_ylim(-layout["radius_mm"] * 1.35, layout["radius_mm"] * 1.35)
        axis.set_aspect("equal")
        axis.set_xlabel("x right (mm)")
        axis.set_ylabel("y forward (mm)")
        axis.set_title(
            f"{title}\nr={layout['radius_mm']:.2f} mm, chord={layout['adjacent_spacing_mm']:.2f} mm"
        )
    axes[2].axis("off")
    axes[2].text(
        0.02,
        0.98,
        "Recommended first 5.8 GHz build\n"
        "(mechanically alias-safe through 6.0 GHz)\n\n"
        "C6 clockwise:\n"
        "  ANT1, ANT2, ANT4, ANT8, ANT7, ANT5\n"
        "  omit ANT3/ANT6 (weakest + least isolation)\n"
        "  pair scan: ANT1, ANT8, ANT2, ANT7, ANT4, ANT5\n\n"
        "C8 clockwise:\n"
        "  ANT1, ANT2, ANT3, ANT4, ANT8, ANT7, ANT6, ANT5\n"
        "  pair scan: ANT1, ANT8, ANT2, ANT7,\n"
        "             ANT3, ANT6, ANT4, ANT5\n\n"
        "Alternate each scan with its reverse.\n"
        "Use RX1 continuously; apply board LUT,\n"
        "final-cable correction, surveyed geometry,\n"
        "OTA manifold, then noise whitening.\n\n"
        "This mapping is a 5.8 GHz evidence-based\n"
        "recommendation, not broadband qualification.",
        va="top",
        family="monospace",
        fontsize=11,
    )
    figure.suptitle("Circular-array wiring, geometry, and temporal scan recommendation")
    _save_figure(figure, figure_dir / FIGURES[14])

    calibration_ports: dict[str, Any] = {}
    for index, port in enumerate(PORTS):
        calibration_ports[port] = {
            "correction_gain_db": correction_gain_db[index].tolist(),
            "correction_phase_unwrapped_deg": correction_phase_unwrapped_deg[index].tolist(),
            "correction_real": correction[index].real.tolist(),
            "correction_imag": correction[index].imag.tolist(),
            "knot_quality": {
                "phase_step_coherence": full_coherence[index].tolist(),
                "maximum_peak_component_counts": full_peak_counts[index].tolist(),
            },
            "high_band_holdout": holdout_metrics[port]["logphase_pchip"],
        }
    calibration_lut = {
        "schema": 1,
        "status": "engineering_candidate_reconnect_and_ota_unqualified",
        "reference_gauge": "per_frequency_geometric_mean",
        "display_reference_port": DISPLAY_REFERENCE_PORT,
        "coefficient_definition": ("corrected_port_i = measured_port_i * H_geometric_mean / H_i"),
        "support_hz": [FULL_FREQUENCIES_HZ[0], FULL_FREQUENCIES_HZ[-1]],
        "frequency_hz": list(FULL_FREQUENCIES_HZ),
        "interpolation": {
            "method": "shape_preserving_cubic_hermite_pchip",
            "domains": ["correction_gain_db", "correction_phase_unwrapped_deg"],
            "extrapolation": "reject",
            "validated_midpoint_band_hz": [
                HOLDOUT_FREQUENCIES_HZ[0],
                HOLDOUT_FREQUENCIES_HZ[-1],
            ],
        },
        "ports": calibration_ports,
    }
    _write_json(data_dir / "calibration-lut.json", calibration_lut)
    _write_json(data_dir / "array-layout-recommendations.json", array_recommendations)
    with (data_dir / "calibration-lut.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "frequency_hz",
                "port",
                "correction_gain_db",
                "correction_phase_unwrapped_deg",
                "correction_real",
                "correction_imag",
            ]
        )
        for frequency_index, frequency in enumerate(FULL_FREQUENCIES_HZ):
            for port_index, port in enumerate(PORTS):
                writer.writerow(
                    [
                        frequency,
                        port,
                        f"{correction_gain_db[port_index, frequency_index]:.12g}",
                        f"{correction_phase_unwrapped_deg[port_index, frequency_index]:.12g}",
                        f"{correction[port_index, frequency_index].real:.12g}",
                        f"{correction[port_index, frequency_index].imag:.12g}",
                    ]
                )

    manifest = {
        "schema": 1,
        "campaign": "pcb_direct_injection_ant1_ant8_20260901_20260902",
        "evidence_boundary": {
            "machine_attested": (
                "run identity, radio identities, board identity, RF configuration, observations, "
                "per-capture cleanup, final safety, raw IQ artifact content"
            ),
            "operator_attested": (
                "direct-cable placement, unchanged cable shape, 8-way input termination, "
                "disconnected-output termination, and unchanged RX1/common cabling"
            ),
        },
        "raw_hashing_completed": not args.skip_raw_hash,
        "raw_replay_completed": not args.skip_raw_replay,
        "runs": inventory,
        "source_hashes": {
            "analyzer_sha256": _sha256_path(Path(__file__).resolve()),
            "capture_runner_sha256": _sha256_path(
                repository / "scripts/run_pinned_static_screen.py"
            ),
            "raw_replay_analyzer_sha256": _sha256_path(
                repository / "scripts/analyze_pinned_broadband_campaign.py"
            ),
            "uv_lock_sha256": _sha256_path(repository / "uv.lock"),
        },
    }
    manifest["canonical_manifest_sha256"] = _canonical_sha256(manifest)
    _write_json(data_dir / "campaign-manifest.json", manifest)

    correction_at_5g8: dict[str, Any] = {}
    for index, port in enumerate(PORTS):
        correction_at_5g8[port] = {
            "gain_db": float(correction_gain_db[index, at_5g8]),
            "phase_wrapped_deg": float(_wrap_deg(correction_phase_unwrapped_deg[index, at_5g8])),
            "real": float(correction[index, at_5g8].real),
            "imag": float(correction[index, at_5g8].imag),
        }
    results = {
        "schema": 1,
        "reference_gauge": "per_frequency_geometric_mean",
        "display_reference_port": DISPLAY_REFERENCE_PORT,
        "quality_by_port": quality_rows,
        "qualification_relative_db": {
            port: {
                state: float(qualification_relative_db[row, column])
                for column, state in enumerate(STATES)
            }
            for row, port in enumerate(PORTS)
        },
        "qualification_active_complex_matrix": {
            "orientation": "rows_selected_state_columns_injected_port",
            "normalization": "each injected-port column normalized by its driven diagonal",
            "real": mixing_matrix_5g8.real.tolist(),
            "imag": mixing_matrix_5g8.imag.tolist(),
            "singular_values": mixing_singular_values.tolist(),
            "condition_number": mixing_condition_number,
        },
        "holdout_metrics": holdout_metrics,
        "holdout_spatial_phase": {
            "rms_deg": spatial_phase_rms,
            "p95_abs_deg": spatial_phase_p95,
            "max_abs_deg": spatial_phase_max,
        },
        "correction_at_5p8_ghz": correction_at_5g8,
        "pairwise_5p8_ghz": {
            "row_over_column_gain_db": pairwise_gain_5g8.tolist(),
            "row_over_column_phase_deg": pairwise_phase_5g8.tolist(),
        },
        "delay_metrics_relative_to_reference": delay_metrics,
        "pairwise_5_to_6_ghz": {
            "row_over_column_delay_ns": pairwise_delay_5_to_6.tolist(),
            "delay_only_residual_rms_deg": pairwise_residual_5_to_6.tolist(),
        },
        "direction_finding_proxy": {
            "wavelength_at_5p8_ghz_mm": wavelength_5g8_m * 1e3,
            "ula8_spacing_mm": ula_spacing_m * 1e3,
            "c6_radius_mm": 25.5,
            "c8_radius_mm": c8_radius_m * 1e3,
            "bearing_grid_deg": bearings.tolist(),
            "linearized_rms_bias_deg": {key: value.tolist() for key, value in df_bias.items()},
            "measured_5p8_ghz_leakage_c8": {
                "bearing_grid_deg": leakage_bearings.tolist(),
                "ideal_manifold_estimate_bias_deg": leakage_bias.tolist(),
                "maximum_absolute_bias_deg": float(np.max(np.abs(leakage_bias))),
                "rms_bias_deg": float(np.sqrt(np.mean(np.square(leakage_bias)))),
            },
            "limitations": (
                "ideal uncoupled manifold; calibration interpolation residual only; excludes SNR, "
                "multipath, switching-time source evolution, antenna patterns, coupling, "
                "and geometry error"
            ),
        },
    }
    _write_json(data_dir / "campaign-results.json", results)

    figures = []
    for filename in FIGURES:
        path = figure_dir / filename
        width, height = _png_dimensions(path)
        figures.append(
            {
                "filename": filename,
                "width_px": width,
                "height_px": height,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_path(path),
            }
        )
    figure_manifest = {"schema": 1, "figures": figures}
    figure_manifest["canonical_manifest_sha256"] = _canonical_sha256(figure_manifest)
    _write_json(data_dir / "figures-manifest.json", figure_manifest)

    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "evidence_observations": expected_total,
                "raw_iq_bytes": sum(item["raw_iq_bytes"] for item in inventory),
                "raw_hashing_completed": not args.skip_raw_hash,
                "raw_replay_completed": not args.skip_raw_replay,
                "spatial_phase_rms_deg": spatial_phase_rms,
                "spatial_phase_p95_abs_deg": spatial_phase_p95,
                "figures": len(FIGURES),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
