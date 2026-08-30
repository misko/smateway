#!/usr/bin/env python3
"""Validate and analyze the pinned three-pass 2.1-5.8 GHz campaign."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import math
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from smateway.ota_analysis import estimate_coherent_pilot_offset

FREQUENCIES_HZ = tuple(range(2_100_000_000, 5_800_000_001, 100_000_000))
STATES = ("ALL_OFF", *(f"ANT{i}" for i in range(1, 9)))
SELECTED_STATES = STATES[1:]
MODEL_STATES = SELECTED_STATES[:-1]
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
RADIO_URI = "ip:192.168.1.15"
RADIO_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.173"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
STLINK_SERIAL = "002D003A3335511035383531"
SOURCE_COMMIT = "4a163644ab54c804680e2784da1f73dcb1c2167a"
RUN_JSON_SHA256_BY_ID = {
    "20260830T211358.287767Z": "21c17f36641fbc1793c0c6469b70c4d8307afacbe0b58d8ad4a1549b47866d7b",
    "20260830T212857.254746Z": "1b6b28dce8059e54cbdbb2ec6a47bab6629564a122808ddcc9ce5359365203ea",
    "20260830T214309.897237Z": "b41deb6c9396ef7cf85859e61b5dbec87d42143f9df23f1b5b4bfed092d41e54",
}
TX_GAIN_DB = -40.0
LIGHT_SPEED_M_S = 299_792_458.0
MINIMUM_DERIVED_PATH_MAGNITUDE = 1e-9
DELAY_SEARCH_MIN_NS = -2.5
DELAY_SEARCH_MAX_NS = 2.5
DELAY_SEARCH_POINTS = 20_001
RIPPLE_DELAY_MIN_NS = 0.05
RIPPLE_DELAY_MAX_NS = 2.5
RIPPLE_DELAY_SEARCH_POINTS = 2_451


class CampaignError(ValueError):
    """A capture set cannot support the pinned campaign result."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--figure-dir", type=Path, required=True)
    return parser


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256_canonical_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git_blob_sha256(repository: Path, revision: str, relative_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CampaignError(f"cannot read {relative_path} from capture commit")
    return hashlib.sha256(result.stdout).hexdigest()


def wrap_phase_deg(value: float) -> float:
    wrapped = (value + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(wrapped, -180.0, abs_tol=1e-12) else wrapped


def _wrap_array_deg(values: np.ndarray) -> np.ndarray:
    return (values + 180.0) % 360.0 - 180.0


def _load(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise CampaignError(f"{path} contains non-finite JSON constant {value}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
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


def _require_selector(
    value: object,
    *,
    code: int,
    lease_active: bool,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} is missing")
    expected_lease_ms = 5000 if lease_active else 0
    expected_status_flags = 3 if lease_active else 1
    sequence = value.get("command_sequence")
    if (
        value.get("applied_code") != code
        or value.get("command_code") != code
        or value.get("lease_active") is not lease_active
        or value.get("command_lease_ms") != expected_lease_ms
        or value.get("command_valid") is not True
        or value.get("invalid_command") is not False
        or value.get("guard_active") is not False
        or value.get("status_flags") != expected_status_flags
        or not isinstance(sequence, int)
        or sequence <= 0
        or value.get("acknowledged_sequence") != sequence
    ):
        raise CampaignError(f"{label} does not contain the requested selector state")
    remaining = value.get("remaining_lease_ms")
    if lease_active:
        if not isinstance(remaining, int) or not 0 < remaining <= expected_lease_ms:
            raise CampaignError(f"{label} does not contain a live selected-state lease")
    elif remaining != 0:
        raise CampaignError(f"{label} contains a residual ALL_OFF lease")


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CampaignError(f"{label} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CampaignError(f"{label} is not an ISO timestamp") from error
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CampaignError(f"{label} is not UTC")
    return parsed


def _require_radio_readback(value: object, label: str) -> tuple[datetime, datetime]:
    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} is missing")
    _require_mute(value.get("receiver_mute"), f"{label} receiver")
    if value.get("source_tx_gain_db") != [TX_GAIN_DB, -80.0]:
        raise CampaignError(f"{label} source gain differs from the pinned stimulus")
    if value.get("source_dds_scales") != [0.25, 0.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0]:
        raise CampaignError(f"{label} source DDS differs from the pinned stimulus")
    started = _parse_utc(value.get("started_utc"), f"{label} start")
    completed = _parse_utc(value.get("completed_utc"), f"{label} completion")
    if completed < started:
        raise CampaignError(f"{label} completion precedes start")
    return started, completed


def _validate_radio_facts(run: Mapping[str, Any], role: str) -> None:
    receiver = run.get("radio_facts")
    source = run.get("source_radio_facts")
    if not isinstance(receiver, Mapping) or not isinstance(source, Mapping):
        raise CampaignError(f"{role} radio facts are missing")
    receiver_expected = {
        "uri": RADIO_URI,
        "hw_serial": RADIO_SERIAL,
        "ad9361-phy,model": "ad9361",
        "fw_version": "v0.40-plutoplus-spf-tandem-agc-v7",
    }
    source_expected = {
        "uri": SOURCE_URI,
        "hw_serial": SOURCE_SERIAL,
        "ad9361-phy,model": "ad9361",
        "fw_version": "v0.43-plutoplus-spf-ddr-ring-v1",
    }
    if any(receiver.get(key) != expected for key, expected in receiver_expected.items()):
        raise CampaignError(f"{role} receiver facts differ")
    if any(source.get(key) != expected for key, expected in source_expected.items()):
        raise CampaignError(f"{role} source facts differ")


def _finite_transfer(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} is missing")
    try:
        real = float(value["real"])
        imag = float(value["imag"])
    except (KeyError, TypeError, ValueError) as error:
        raise CampaignError(f"{label} is malformed") from error
    if not math.isfinite(real) or not math.isfinite(imag) or abs(complex(real, imag)) == 0.0:
        raise CampaignError(f"{label} is non-finite or zero")
    return {"real": real, "imag": imag}


def _replay_transfer(iq_path: Path) -> tuple[dict[str, float], list[float], list[float]]:
    """Recompute the simultaneous RX2/RX1 transfer from authoritative raw IQ."""
    try:
        with np.load(iq_path, allow_pickle=False) as archive:
            if set(archive.files) != {"rx1", "rx2"}:
                raise CampaignError(f"{iq_path.name} does not contain exactly rx1/rx2")
            rx1 = np.asarray(archive["rx1"])
            rx2 = np.asarray(archive["rx2"])
    except (OSError, ValueError) as error:
        raise CampaignError(f"cannot load raw IQ {iq_path}: {error}") from error
    for name, values in (("rx1", rx1), ("rx2", rx2)):
        if values.shape != (262_144,) or values.dtype != np.dtype(np.complex64):
            raise CampaignError(f"{iq_path.name} {name} shape/dtype differs")
        if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
            raise CampaignError(f"{iq_path.name} {name} contains non-finite samples")
    window = np.hanning(rx1.size)
    spectrum = np.fft.fft(rx1 * window)
    frequencies = np.fft.fftfreq(rx1.size, d=1.0 / 2_000_000.0)
    candidate = (frequencies >= 20_000.0) & (frequencies <= 250_000.0)
    indices = np.flatnonzero(candidate)
    if indices.size == 0:
        raise CampaignError(f"{iq_path.name} pilot acquisition band is empty")
    nominal_offset_hz = float(frequencies[int(indices[np.argmax(np.abs(spectrum[indices]))])])
    pilot = estimate_coherent_pilot_offset(
        rx1,
        sample_rate_hz=2_000_000,
        nominal_tone_offset_hz=nominal_offset_hz,
        bin_ms=0.5,
        maximum_residual_hz=400.0,
    )
    oscillator = np.exp(
        -2j
        * np.pi
        * pilot.estimated_offset_hz
        * np.arange(rx1.size, dtype=np.float64)
        / 2_000_000.0
    )
    z1 = complex(np.mean(rx1 * oscillator))
    z2 = complex(np.mean(rx2 * oscillator))
    if abs(z1) <= np.finfo(float).tiny:
        raise CampaignError(f"{iq_path.name} replay RX1 reference phasor is zero")
    transfer = z2 / z1
    peak = [
        float(max(np.max(np.abs(channel.real)), np.max(np.abs(channel.imag))))
        for channel in (rx1, rx2)
    ]
    rms = [float(np.sqrt(np.mean(np.abs(channel) ** 2))) for channel in (rx1, rx2)]
    return {"real": transfer.real, "imag": transfer.imag}, peak, rms


def _validate_configuration(run: Mapping[str, Any], role: str) -> None:
    expected = {
        "bandwidth_hz": 1_600_000,
        "dds_scale": 0.25,
        "frequencies_hz": list(FREQUENCIES_HZ),
        "repeats": 1,
        "rx_gain_db": 60,
        "sample_count": 262_144,
        "sample_rate_hz": 2_000_000,
        "states": list(STATES),
        "tone_offset_hz": 100_000,
        "tx_gain_db": TX_GAIN_DB,
    }
    if run.get("configuration") != expected:
        raise CampaignError(f"{role} configuration differs from the pinned campaign")


def _validate_run(
    path: Path,
    role: str,
    sweep_index: int,
    expected_run_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path.name != "run.json" or path.is_symlink() or not path.is_file():
        raise CampaignError(f"{role} input is not a regular run.json")
    expected_run_sha256 = RUN_JSON_SHA256_BY_ID[expected_run_id]
    if sha256_path(path) != expected_run_sha256:
        raise CampaignError(f"{role} run.json differs from the pinned campaign")
    run = _load(path)
    expected_identity = {
        "radio_uri": RADIO_URI,
        "radio_serial": RADIO_SERIAL,
        "source_radio_uri": SOURCE_URI,
        "source_radio_serial": SOURCE_SERIAL,
        "board_id": BOARD_ID,
        "stlink_serial": STLINK_SERIAL,
        "git_head": SOURCE_COMMIT,
    }
    if run.get("schema") != 1 or run.get("mode") != "external" or run.get("error") is not None:
        raise CampaignError(f"{role} is incomplete, failed, or not external-source mode")
    if run.get("run_id") != expected_run_id:
        raise CampaignError(f"{role} run ID differs from the pinned campaign")
    if any(run.get(key) != value for key, value in expected_identity.items()):
        raise CampaignError(f"{role} identity or source-freeze commit differs")
    _validate_configuration(run, role)
    _validate_radio_facts(run, role)
    _require_mute(run.get("final_radio_mute"), f"{role} final receiver")
    _require_mute(run.get("final_source_radio_mute"), f"{role} final source")
    _require_selector(
        run.get("final_selector"),
        code=STATE_CODES["ALL_OFF"],
        lease_active=False,
        label=f"{role} final selector",
    )
    observations = run.get("observations")
    if not isinstance(observations, list) or len(observations) != len(FREQUENCIES_HZ) * len(STATES):
        raise CampaignError(f"{role} does not contain exactly 342 observations")
    if any(not isinstance(row, Mapping) for row in observations):
        raise CampaignError(f"{role} contains a malformed observation")
    expected_order = [(frequency, state) for frequency in FREQUENCIES_HZ for state in STATES]
    actual_order = [(row.get("frequency_hz"), row.get("state")) for row in observations]
    if actual_order != expected_order:
        raise CampaignError(f"{role} observation order/lattice differs")
    expected_iq_names = {
        f"{frequency}-{state.lower()}-r1.npz" for frequency, state in expected_order
    }
    actual_iq_paths = list(path.parent.glob("*.npz"))
    if {item.name for item in actual_iq_paths} != expected_iq_names or len(actual_iq_paths) != len(
        expected_iq_names
    ):
        raise CampaignError(f"{role} raw-IQ artifact set differs from the exact lattice")
    if any(item.is_symlink() or not item.is_file() for item in actual_iq_paths):
        raise CampaignError(f"{role} raw-IQ artifact set contains a symlink or non-file")

    normalized: list[dict[str, Any]] = []
    previous_completed: datetime | None = None
    for index, raw in enumerate(observations):
        if not isinstance(raw, dict):
            raise CampaignError(f"{role} observation {index} is malformed")
        frequency = int(raw["frequency_hz"])
        state = str(raw["state"])
        if raw.get("repeat") != 1:
            raise CampaignError(f"{role} observation {index} repeat differs")
        _require_selector(
            raw.get("selector_before"),
            code=STATE_CODES[state],
            lease_active=state != "ALL_OFF",
            label=f"{role} observation {index} selector-before",
        )
        _require_selector(
            raw.get("selector_after"),
            code=STATE_CODES["ALL_OFF"],
            lease_active=False,
            label=f"{role} observation {index} selector-after",
        )
        _require_mute(raw.get("post_capture_mute"), f"{role} observation {index} receiver")
        _require_mute(raw.get("post_capture_source_mute"), f"{role} observation {index} source")
        started, completed = _require_radio_readback(
            raw.get("radio_readback"), f"{role} observation {index} radio readback"
        )
        if previous_completed is not None and started < previous_completed:
            raise CampaignError(f"{role} observation timestamps are not monotonic")
        previous_completed = completed
        analysis = raw.get("analysis")
        if raw.get("analysis_error") is not None or not isinstance(analysis, Mapping):
            raise CampaignError(f"{role} observation {index} analysis failed")
        transfer = _finite_transfer(
            analysis.get("transfer_rx2_over_rx1"), f"{role} observation {index} transfer"
        )
        expected_iq_name = f"{frequency}-{state.lower()}-r1.npz"
        iq_name = raw.get("iq_file")
        iq_path = path.parent / expected_iq_name
        if iq_name != expected_iq_name or not iq_path.is_file() or iq_path.is_symlink():
            raise CampaignError(f"{role} observation {index} raw IQ is missing")
        if iq_path.resolve().parent != path.parent.resolve():
            raise CampaignError(f"{role} observation {index} raw IQ escapes its run directory")
        iq_sha256 = sha256_path(iq_path)
        replay_transfer, replay_peak, replay_rms = _replay_transfer(iq_path)
        stored_complex = complex(transfer["real"], transfer["imag"])
        replay_complex = complex(replay_transfer["real"], replay_transfer["imag"])
        replay_delta = abs(replay_complex - stored_complex)
        if not np.isclose(replay_complex, stored_complex, rtol=1e-11, atol=1e-12):
            raise CampaignError(f"{role} observation {index} stored transfer fails raw-IQ replay")
        if not np.allclose(replay_peak, analysis["peak_component_counts"], rtol=1e-7, atol=1e-7):
            raise CampaignError(f"{role} observation {index} stored peaks fail raw-IQ replay")
        if not np.allclose(replay_rms, analysis["rms_counts"], rtol=1e-7, atol=1e-7):
            raise CampaignError(f"{role} observation {index} stored RMS fails raw-IQ replay")
        normalized.append(
            {
                "role": role,
                "sweep_index": sweep_index,
                "run_id": run["run_id"],
                "frequency_hz": frequency,
                "state": state,
                "repeat": 1,
                "iq_file": iq_name,
                "iq_size_bytes": iq_path.stat().st_size,
                "iq_sha256": iq_sha256,
                "peak_component_counts": replay_peak,
                "rms_counts": replay_rms,
                "transfer": replay_transfer,
                "stored_transfer_replay_absolute_delta": replay_delta,
            }
        )
    return run, normalized


def _phasor(row: Mapping[str, Any]) -> complex:
    transfer = row["transfer"]
    return complex(float(transfer["real"]), float(transfer["imag"]))


def aggregate_phasors(values: Sequence[complex]) -> dict[str, float]:
    if not values:
        raise CampaignError("cannot aggregate an empty phasor cohort")
    phasors = np.asarray(values, dtype=np.complex128)
    if not np.all(np.isfinite(phasors)) or np.any(np.abs(phasors) <= np.finfo(float).tiny):
        raise CampaignError("cannot aggregate a non-finite or zero phasor")
    mean = complex(np.mean(phasors))
    if abs(mean) <= np.finfo(float).tiny:
        raise CampaignError("cannot aggregate a zero mean phasor")
    magnitudes_db = 20.0 * np.log10(np.abs(phasors))
    mean_phase = math.degrees(math.atan2(mean.imag, mean.real))
    phase_offsets = np.asarray(
        [wrap_phase_deg(math.degrees(np.angle(value)) - mean_phase) for value in phasors]
    )
    return {
        "real": mean.real,
        "imag": mean.imag,
        "coherent_magnitude": abs(mean),
        "coherent_magnitude_db": 20.0 * math.log10(abs(mean)),
        "mean_magnitude": float(np.mean(np.abs(phasors))),
        "mean_magnitude_db": float(np.mean(magnitudes_db)),
        "phase_deg": mean_phase,
        "magnitude_span_db": float(np.ptp(magnitudes_db)),
        "phase_span_deg": float(np.ptp(phase_offsets)),
    }


def fit_delay_model(
    frequencies_hz: Sequence[int], coefficients: Sequence[complex]
) -> dict[str, Any]:
    """Fit a bounded circular delay model with two alternating-frequency folds."""
    if len(frequencies_hz) != len(coefficients) or len(coefficients) < 4:
        raise CampaignError("delay fit requires matching frequency/phasor arrays with four points")
    frequency = np.asarray(frequencies_hz, dtype=np.float64)
    phasors = np.asarray(coefficients, dtype=np.complex128)
    if not np.all(np.isfinite(phasors)) or np.any(np.abs(phasors) == 0.0):
        raise CampaignError("delay fit coefficients must be finite and non-zero")
    center_hz = float(np.mean(frequency))
    delta_frequency_hz = frequency - center_hz
    phase_deg = np.rad2deg(np.unwrap(np.angle(phasors)))
    gain_db = 20.0 * np.log10(np.abs(phasors))
    unit_phasors = phasors / np.abs(phasors)
    delay_grid_ns = np.linspace(
        DELAY_SEARCH_MIN_NS,
        DELAY_SEARCH_MAX_NS,
        DELAY_SEARCH_POINTS,
        endpoint=False,
    )

    def circular_fit(mask: np.ndarray) -> dict[str, Any]:
        delta = delta_frequency_hz[mask]
        observed = unit_phasors[mask]
        undo_delay = np.exp(2j * np.pi * delay_grid_ns[:, np.newaxis] * 1e-9 * delta[np.newaxis, :])
        intercept_by_delay = np.angle(np.mean(observed[np.newaxis, :] * undo_delay, axis=1))
        prediction = np.exp(
            1j
            * (
                intercept_by_delay[:, np.newaxis]
                - 2.0 * np.pi * delay_grid_ns[:, np.newaxis] * 1e-9 * delta[np.newaxis, :]
            )
        )
        residual = np.angle(observed[np.newaxis, :] * np.conj(prediction))
        score = np.sqrt(np.mean(residual**2, axis=1))
        best_index = int(np.argmin(score))
        delay_ns = float(delay_grid_ns[best_index])
        intercept_rad = float(intercept_by_delay[best_index])
        predicted_all = np.exp(
            1j * (intercept_rad - 2.0 * np.pi * delay_ns * 1e-9 * delta_frequency_hz)
        )
        residual_all_deg = np.rad2deg(np.angle(unit_phasors * np.conj(predicted_all)))
        gain_constant = float(np.mean(gain_db[mask]))
        gain_residual = gain_db - gain_constant
        return {
            "delay_ns": delay_ns,
            "phase_at_center_deg": math.degrees(intercept_rad),
            "gain_db": gain_constant,
            "residual_deg": residual_all_deg,
            "gain_residual_db": gain_residual,
            "search_boundary": best_index < 4 or best_index >= len(delay_grid_ns) - 4,
        }

    folds: list[dict[str, Any]] = []
    heldout_phase_residuals: list[float] = []
    training_phase_residuals: list[float] = []
    heldout_gain_residuals: list[float] = []
    training_gain_residuals: list[float] = []
    for parity in (0, 1):
        train = np.arange(len(frequency)) % 2 == parity
        heldout = ~train
        fit = circular_fit(train)
        phase_residual = fit["residual_deg"]
        gain_residual = fit["gain_residual_db"]
        training_phase_residuals.extend(phase_residual[train].tolist())
        heldout_phase_residuals.extend(phase_residual[heldout].tolist())
        training_gain_residuals.extend(gain_residual[train].tolist())
        heldout_gain_residuals.extend(gain_residual[heldout].tolist())
        folds.append(
            {
                "training_parity": "even" if parity == 0 else "odd",
                "training_indices": np.flatnonzero(train).tolist(),
                "heldout_indices": np.flatnonzero(heldout).tolist(),
                "delay_ns": fit["delay_ns"],
                "phase_at_center_deg": fit["phase_at_center_deg"],
                "gain_db": fit["gain_db"],
                "training_phase_rms_deg": float(np.sqrt(np.mean(phase_residual[train] ** 2))),
                "heldout_phase_rms_deg": float(np.sqrt(np.mean(phase_residual[heldout] ** 2))),
                "maximum_heldout_phase_error_deg": float(np.max(np.abs(phase_residual[heldout]))),
                "training_gain_rms_db": float(np.sqrt(np.mean(gain_residual[train] ** 2))),
                "heldout_gain_rms_db": float(np.sqrt(np.mean(gain_residual[heldout] ** 2))),
                "search_boundary": fit["search_boundary"],
            }
        )

    full = circular_fit(np.ones(len(frequency), dtype=bool))
    delay_ns = float(full["delay_ns"])
    wrapped_intercept = float(full["phase_at_center_deg"])
    unwrapped_center_estimate = float(np.interp(center_hz, frequency, phase_deg))
    unwrapped_intercept = wrapped_intercept + 360.0 * round(
        (unwrapped_center_estimate - wrapped_intercept) / 360.0
    )
    predicted_unwrapped = unwrapped_intercept - 360.0 * delay_ns * delta_frequency_hz / 1e9
    full_residual = np.asarray(full["residual_deg"])
    full_gain_residual = np.asarray(full["gain_residual_db"])
    heldout_phase = np.asarray(heldout_phase_residuals)
    training_phase = np.asarray(training_phase_residuals)
    heldout_gain = np.asarray(heldout_gain_residuals)
    training_gain = np.asarray(training_gain_residuals)
    return {
        "center_frequency_hz": center_hz,
        "training_point_count_per_fold": len(frequency) // 2,
        "heldout_point_count_per_fold": len(frequency) // 2,
        "cross_validation": folds,
        "delay_ns": delay_ns,
        "free_space_equivalent_length_mm": LIGHT_SPEED_M_S * delay_ns * 1e-6,
        "vf_0p70_equivalent_length_mm": 0.70 * LIGHT_SPEED_M_S * delay_ns * 1e-6,
        "phase_at_center_deg": wrap_phase_deg(wrapped_intercept),
        "phase_at_center_unwrapped_deg": unwrapped_intercept,
        "gain_db": float(full["gain_db"]),
        "training_phase_rms_deg": float(np.sqrt(np.mean(training_phase**2))),
        "heldout_phase_rms_deg": float(np.sqrt(np.mean(heldout_phase**2))),
        "all_phase_rms_deg": float(np.sqrt(np.mean(full_residual**2))),
        "maximum_heldout_phase_error_deg": float(np.max(np.abs(heldout_phase))),
        "training_gain_rms_db": float(np.sqrt(np.mean(training_gain**2))),
        "heldout_gain_rms_db": float(np.sqrt(np.mean(heldout_gain**2))),
        "all_gain_rms_db": float(np.sqrt(np.mean(full_gain_residual**2))),
        "predicted_phase_deg": predicted_unwrapped.tolist(),
        "phase_residual_deg": full_residual.tolist(),
        "measured_unwrapped_phase_deg": phase_deg.tolist(),
        "full_fit": {
            "delay_ns": delay_ns,
            "free_space_equivalent_length_mm": LIGHT_SPEED_M_S * delay_ns * 1e-6,
            "vf_0p70_equivalent_length_mm": 0.70 * LIGHT_SPEED_M_S * delay_ns * 1e-6,
            "phase_at_center_deg": wrap_phase_deg(wrapped_intercept),
            "phase_at_center_unwrapped_deg": unwrapped_intercept,
            "phase_rms_deg": float(np.sqrt(np.mean(full_residual**2))),
            "maximum_phase_error_deg": float(np.max(np.abs(full_residual))),
            "predicted_phase_deg": predicted_unwrapped.tolist(),
            "phase_residual_deg": full_residual.tolist(),
            "search_boundary": bool(full["search_boundary"]),
        },
    }


def fit_log_ripple_model(
    frequencies_hz: Sequence[int], coefficients: Sequence[complex]
) -> dict[str, Any]:
    """Fit linear delay plus one complex log-response ripple with two-fold validation."""
    if len(frequencies_hz) != len(coefficients) or len(coefficients) < 8:
        raise CampaignError(
            "ripple fit requires matching frequency/phasor arrays with eight points"
        )
    frequency = np.asarray(frequencies_hz, dtype=np.float64)
    phasors = np.asarray(coefficients, dtype=np.complex128)
    if not np.all(np.isfinite(phasors)) or np.any(np.abs(phasors) == 0.0):
        raise CampaignError("ripple fit coefficients must be finite and non-zero")
    center_hz = float(np.mean(frequency))
    delta_frequency_ghz = (frequency - center_hz) / 1e9
    measured_log = np.log(np.abs(phasors)) + 1j * np.unwrap(np.angle(phasors))
    ripple_delay_grid_ns = np.linspace(
        RIPPLE_DELAY_MIN_NS,
        RIPPLE_DELAY_MAX_NS,
        RIPPLE_DELAY_SEARCH_POINTS,
    )
    sample_count = len(frequency)

    def design_matrix(ripple_delay_ns: float) -> np.ndarray:
        angle = 2.0 * np.pi * delta_frequency_ghz * ripple_delay_ns
        cosine = np.cos(angle)
        sine = np.sin(angle)
        design = np.zeros((2 * sample_count, 5), dtype=np.float64)
        # log(R) = log(A) + j(phi - 2*pi*df*tau) + q*exp(-j*2*pi*df*delta)
        # with q = q_real + j*q_imag.
        design[:sample_count, 0] = 1.0
        design[:sample_count, 3] = cosine
        design[:sample_count, 4] = sine
        design[sample_count:, 1] = 1.0
        design[sample_count:, 2] = -2.0 * np.pi * delta_frequency_ghz
        design[sample_count:, 3] = -sine
        design[sample_count:, 4] = cosine
        return design

    def linear_fit(mask: np.ndarray) -> dict[str, Any]:
        selected = np.flatnonzero(mask)
        rows = np.concatenate((selected, sample_count + selected))
        selected_log = np.log(np.abs(phasors[selected])) + 1j * np.unwrap(
            np.angle(phasors[selected])
        )
        selected_observation = np.concatenate((selected_log.real, selected_log.imag))
        best_score = math.inf
        best_index = -1
        best_parameters: np.ndarray | None = None
        best_design: np.ndarray | None = None
        for index, ripple_delay_ns in enumerate(ripple_delay_grid_ns):
            design = design_matrix(float(ripple_delay_ns))
            parameters, *_ = np.linalg.lstsq(design[rows], selected_observation, rcond=None)
            residual = selected_observation - design[rows] @ parameters
            score = float(np.mean(residual**2))
            if score < best_score:
                best_score = score
                best_index = index
                best_parameters = parameters
                best_design = design
        if best_parameters is None or best_design is None:
            raise CampaignError("ripple search did not produce a finite model")
        predicted_log = best_design @ best_parameters
        predicted_log_complex = (
            predicted_log[:sample_count] + 1j * predicted_log[sample_count:]
        )
        predicted = np.exp(predicted_log_complex)
        phase_residual_deg = np.rad2deg(np.angle(phasors * np.conj(predicted)))
        gain_residual_db = 20.0 * np.log10(np.abs(phasors) / np.abs(predicted))
        ripple = complex(best_parameters[3], best_parameters[4])
        return {
            "base_gain_db": float(20.0 * best_parameters[0] / math.log(10.0)),
            "base_phase_at_center_deg": wrap_phase_deg(math.degrees(best_parameters[1])),
            "base_phase_at_center_unwrapped_deg": float(math.degrees(best_parameters[1])),
            "base_delay_ns": float(best_parameters[2]),
            "ripple_delay_ns": float(ripple_delay_grid_ns[best_index]),
            "ripple_period_ghz": float(1.0 / ripple_delay_grid_ns[best_index]),
            "ripple_log_amplitude": float(abs(ripple)),
            "ripple_phase_at_center_deg": wrap_phase_deg(math.degrees(np.angle(ripple))),
            "predicted_phase_deg": np.rad2deg(predicted_log_complex.imag).tolist(),
            "predicted_gain_db": (20.0 * predicted_log_complex.real / math.log(10.0)).tolist(),
            "phase_residual_deg": phase_residual_deg,
            "gain_residual_db": gain_residual_db,
            "joint_log_rms": math.sqrt(best_score),
            "search_boundary": best_index < 4
            or best_index >= len(ripple_delay_grid_ns) - 4,
        }

    folds: list[dict[str, Any]] = []
    heldout_phase_residuals: list[float] = []
    training_phase_residuals: list[float] = []
    heldout_gain_residuals: list[float] = []
    training_gain_residuals: list[float] = []
    for parity in (0, 1):
        train = np.arange(sample_count) % 2 == parity
        heldout = ~train
        fit = linear_fit(train)
        phase_residual = fit["phase_residual_deg"]
        gain_residual = fit["gain_residual_db"]
        training_phase_residuals.extend(phase_residual[train].tolist())
        heldout_phase_residuals.extend(phase_residual[heldout].tolist())
        training_gain_residuals.extend(gain_residual[train].tolist())
        heldout_gain_residuals.extend(gain_residual[heldout].tolist())
        folds.append(
            {
                "training_parity": "even" if parity == 0 else "odd",
                "training_indices": np.flatnonzero(train).tolist(),
                "heldout_indices": np.flatnonzero(heldout).tolist(),
                "base_delay_ns": fit["base_delay_ns"],
                "ripple_delay_ns": fit["ripple_delay_ns"],
                "ripple_log_amplitude": fit["ripple_log_amplitude"],
                "training_phase_rms_deg": float(
                    np.sqrt(np.mean(phase_residual[train] ** 2))
                ),
                "heldout_phase_rms_deg": float(
                    np.sqrt(np.mean(phase_residual[heldout] ** 2))
                ),
                "maximum_heldout_phase_error_deg": float(
                    np.max(np.abs(phase_residual[heldout]))
                ),
                "training_gain_rms_db": float(np.sqrt(np.mean(gain_residual[train] ** 2))),
                "heldout_gain_rms_db": float(np.sqrt(np.mean(gain_residual[heldout] ** 2))),
                "search_boundary": fit["search_boundary"],
            }
        )

    full = linear_fit(np.ones(sample_count, dtype=bool))
    heldout_phase = np.asarray(heldout_phase_residuals)
    training_phase = np.asarray(training_phase_residuals)
    heldout_gain = np.asarray(heldout_gain_residuals)
    training_gain = np.asarray(training_gain_residuals)
    full_phase_residual = np.asarray(full["phase_residual_deg"])
    full_gain_residual = np.asarray(full["gain_residual_db"])
    return {
        "center_frequency_hz": center_hz,
        "parameter_count": 6,
        "training_point_count_per_fold": sample_count // 2,
        "heldout_point_count_per_fold": sample_count // 2,
        "cross_validation": folds,
        "base_gain_db": full["base_gain_db"],
        "base_phase_at_center_deg": full["base_phase_at_center_deg"],
        "base_phase_at_center_unwrapped_deg": full["base_phase_at_center_unwrapped_deg"],
        "base_delay_ns": full["base_delay_ns"],
        "ripple_delay_ns": full["ripple_delay_ns"],
        "ripple_period_ghz": full["ripple_period_ghz"],
        "ripple_log_amplitude": full["ripple_log_amplitude"],
        "ripple_phase_at_center_deg": full["ripple_phase_at_center_deg"],
        "training_phase_rms_deg": float(np.sqrt(np.mean(training_phase**2))),
        "heldout_phase_rms_deg": float(np.sqrt(np.mean(heldout_phase**2))),
        "maximum_heldout_phase_error_deg": float(np.max(np.abs(heldout_phase))),
        "training_gain_rms_db": float(np.sqrt(np.mean(training_gain**2))),
        "heldout_gain_rms_db": float(np.sqrt(np.mean(heldout_gain**2))),
        "all_phase_rms_deg": float(np.sqrt(np.mean(full_phase_residual**2))),
        "all_gain_rms_db": float(np.sqrt(np.mean(full_gain_residual**2))),
        "predicted_phase_deg": full["predicted_phase_deg"],
        "predicted_gain_db": full["predicted_gain_db"],
        "phase_residual_deg": full_phase_residual.tolist(),
        "gain_residual_db": full_gain_residual.tolist(),
        "measured_unwrapped_phase_deg": np.rad2deg(measured_log.imag).tolist(),
        "measured_gain_db": (20.0 * measured_log.real / math.log(10.0)).tolist(),
        "full_fit": {
            key: value
            for key, value in full.items()
            if key not in {"phase_residual_deg", "gain_residual_db"}
        },
    }


def _cohort(
    rows: Sequence[Mapping[str, Any]], *, frequency_hz: int, state: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["frequency_hz"] == frequency_hz and row["state"] == state]


def analyze_campaign(run_paths: Sequence[Path]) -> dict[str, Any]:
    if len(run_paths) != 3:
        raise CampaignError("the pinned campaign requires exactly three independent runs")
    resolved_paths = [path.resolve() for path in run_paths]
    if len(set(resolved_paths)) != 3:
        raise CampaignError("the pinned campaign inputs are not three distinct files")
    paths_by_run_id: dict[str, Path] = {}
    for path in resolved_paths:
        run_id = _load(path).get("run_id")
        if run_id not in RUN_JSON_SHA256_BY_ID or not isinstance(run_id, str):
            raise CampaignError("an input is not one of the three pinned campaign runs")
        if run_id in paths_by_run_id:
            raise CampaignError("the pinned campaign contains a duplicate run ID")
        paths_by_run_id[run_id] = path
    if set(paths_by_run_id) != set(RUN_JSON_SHA256_BY_ID):
        raise CampaignError("the exact pinned three-run campaign is incomplete")
    canonical_run_ids = tuple(sorted(RUN_JSON_SHA256_BY_ID))
    canonical_paths = [paths_by_run_id[run_id] for run_id in canonical_run_ids]
    loaded = [
        _validate_run(path, f"sweep_{index}", index, run_id)
        for index, (run_id, path) in enumerate(
            zip(canonical_run_ids, canonical_paths, strict=True), start=1
        )
    ]
    runs = [item[0] for item in loaded]
    rows_by_run = [item[1] for item in loaded]
    all_rows = [row for rows in rows_by_run for row in rows]

    transfer_cells: list[dict[str, Any]] = []
    calibration_cells: list[dict[str, Any]] = []
    raw_calibration_cells: list[dict[str, Any]] = []
    contrast_cells: list[dict[str, Any]] = []
    path_contrast_cells: list[dict[str, Any]] = []
    path_ratio_by_run_state: dict[tuple[int, str], list[complex]] = {}
    for frequency in FREQUENCIES_HZ:
        transfers: dict[str, list[complex]] = {}
        for state in STATES:
            cohort = _cohort(all_rows, frequency_hz=frequency, state=state)
            values = [_phasor(row) for row in cohort]
            if len(values) != 3:
                raise CampaignError("an aggregate cell does not contain exactly three sweeps")
            transfers[state] = values
            transfer_cells.append(
                {"frequency_hz": frequency, "state": state, **aggregate_phasors(values)}
            )
        for state in SELECTED_STATES:
            raw_coefficients = [
                transfers["ANT8"][index] / transfers[state][index] for index in range(3)
            ]
            path_values = [
                transfers[state][index] - transfers["ALL_OFF"][index] for index in range(3)
            ]
            reference_path_values = [
                transfers["ANT8"][index] - transfers["ALL_OFF"][index] for index in range(3)
            ]
            for label, values in (
                (f"{frequency} {state} path", path_values),
                (f"{frequency} ANT8 reference path", reference_path_values),
            ):
                if any(
                    not math.isfinite(value.real)
                    or not math.isfinite(value.imag)
                    or abs(value) <= MINIMUM_DERIVED_PATH_MAGNITUDE
                    for value in values
                ):
                    raise CampaignError(f"{label} is non-finite or below the derived-path floor")
            coefficients = [reference_path_values[index] / path_values[index] for index in range(3)]
            path_ratios = [path_values[index] / reference_path_values[index] for index in range(3)]
            calibration_cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "reference_state": "ANT8",
                    "all_off_subtracted": True,
                    **aggregate_phasors(coefficients),
                }
            )
            raw_calibration_cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "reference_state": "ANT8",
                    "all_off_subtracted": False,
                    **aggregate_phasors(raw_coefficients),
                }
            )
            contrasts = np.asarray(
                [
                    20.0
                    * math.log10(abs(transfers[state][index]) / abs(transfers["ALL_OFF"][index]))
                    for index in range(3)
                ]
            )
            contrast_cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "mean_db": float(np.mean(contrasts)),
                    "span_db": float(np.ptp(contrasts)),
                    "values_db": contrasts.tolist(),
                }
            )
            path_contrasts = np.asarray(
                [
                    20.0 * math.log10(abs(path_values[index]) / abs(transfers["ALL_OFF"][index]))
                    for index in range(3)
                ]
            )
            path_contrast_cells.append(
                {
                    "frequency_hz": frequency,
                    "state": state,
                    "mean_db": float(np.mean(path_contrasts)),
                    "span_db": float(np.ptp(path_contrasts)),
                    "values_db": path_contrasts.tolist(),
                }
            )
            for run_index, path_ratio in enumerate(path_ratios, start=1):
                path_ratio_by_run_state.setdefault((run_index, state), []).append(path_ratio)

    path_models: list[dict[str, Any]] = []
    path_ripple_models: list[dict[str, Any]] = []
    for state in MODEL_STATES:
        # Aggregate by frequency, not by sweep: P_i/P_8 is the physical relative path model.
        mean_path_ratios = [
            complex(
                np.mean(
                    [
                        path_ratio_by_run_state[(index, state)][frequency_index]
                        for index in range(1, 4)
                    ]
                )
            )
            for frequency_index in range(len(FREQUENCIES_HZ))
        ]
        model = fit_delay_model(FREQUENCIES_HZ, mean_path_ratios)
        sweep_delays = [
            fit_delay_model(FREQUENCIES_HZ, path_ratio_by_run_state[(index, state)])["full_fit"][
                "delay_ns"
            ]
            for index in range(1, 4)
        ]
        model.update(
            {
                "state": state,
                "reference_state": "ANT8",
                "sweep_full_fit_delay_ns": sweep_delays,
                "sweep_delay_span_ns": float(np.ptp(sweep_delays)),
            }
        )
        if model["full_fit"]["search_boundary"] or any(
            fold["search_boundary"] for fold in model["cross_validation"]
        ):
            raise CampaignError(f"{state} delay fit touches the predeclared search boundary")
        path_models.append(model)
        ripple_model = fit_log_ripple_model(FREQUENCIES_HZ, mean_path_ratios)
        ripple_model.update({"state": state, "reference_state": "ANT8"})
        if ripple_model["full_fit"]["search_boundary"] or any(
            fold["search_boundary"] for fold in ripple_model["cross_validation"]
        ):
            raise CampaignError(f"{state} ripple fit touches the predeclared search boundary")
        path_ripple_models.append(ripple_model)

    selected_transfers = [row for row in transfer_cells if row["state"] != "ALL_OFF"]
    selected_calibration = [row for row in calibration_cells if row["state"] != "ANT8"]
    contrast_5g8 = [
        row["mean_db"] for row in contrast_cells if row["frequency_hz"] == 5_800_000_000
    ]
    path_contrast_5g8 = [
        row["mean_db"] for row in path_contrast_cells if row["frequency_hz"] == 5_800_000_000
    ]
    run_documents: list[dict[str, Any]] = []
    for index, (run, path, rows) in enumerate(
        zip(runs, canonical_paths, rows_by_run, strict=True), start=1
    ):
        artifacts = [
            {
                "name": row["iq_file"],
                "size_bytes": row["iq_size_bytes"],
                "sha256": row["iq_sha256"],
            }
            for row in rows
        ]
        run_documents.append(
            {
                "sweep_index": index,
                "run_id": run["run_id"],
                "run_json_sha256": sha256_path(path),
                "capture_count": len(run["observations"]),
                "configuration": run["configuration"],
                "first_capture_started_utc": run["observations"][0]["radio_readback"][
                    "started_utc"
                ],
                "last_capture_completed_utc": run["observations"][-1]["radio_readback"][
                    "completed_utc"
                ],
                "artifact_manifest_sha256": _sha256_canonical_json(artifacts),
                "artifacts": artifacts,
            }
        )
    if len({document["artifact_manifest_sha256"] for document in run_documents}) != 3:
        raise CampaignError("campaign raw artifact manifests are not distinct")
    analyzer_path = Path(__file__).resolve()
    capture_runner_path = analyzer_path.with_name("run_pinned_static_screen.py")
    repository = analyzer_path.parents[1]
    capture_runner_commit_sha256 = _git_blob_sha256(
        repository, SOURCE_COMMIT, "scripts/run_pinned_static_screen.py"
    )
    if sha256_path(capture_runner_path) != capture_runner_commit_sha256:
        raise CampaignError("working-tree capture runner differs from the pinned capture commit")
    pilot_source_raw = inspect.getsourcefile(estimate_coherent_pilot_offset)
    if pilot_source_raw is None:
        raise CampaignError("cannot locate the coherent pilot estimator source")
    pilot_source_path = Path(pilot_source_raw).resolve()
    summary = {
        "capture_count": len(all_rows),
        "raw_iq_bytes": sum(int(row["iq_size_bytes"]) for row in all_rows),
        "analysis_error_count": 0,
        "source_commit": SOURCE_COMMIT,
        "frequency_count": len(FREQUENCIES_HZ),
        "state_count": len(STATES),
        "complete_sweep_count": 3,
        "maximum_peak_component_counts": max(max(row["peak_component_counts"]) for row in all_rows),
        "maximum_selected_transfer_repeat_span_db": max(
            row["magnitude_span_db"] for row in selected_transfers
        ),
        "maximum_selected_transfer_repeat_phase_span_deg": max(
            row["phase_span_deg"] for row in selected_transfers
        ),
        "maximum_calibration_repeat_span_db": max(
            row["magnitude_span_db"] for row in selected_calibration
        ),
        "maximum_calibration_repeat_phase_span_deg": max(
            row["phase_span_deg"] for row in selected_calibration
        ),
        "minimum_selected_over_all_off_db": min(row["mean_db"] for row in contrast_cells),
        "maximum_selected_over_all_off_db": max(row["mean_db"] for row in contrast_cells),
        "minimum_path_over_all_off_db": min(row["mean_db"] for row in path_contrast_cells),
        "maximum_path_over_all_off_db": max(row["mean_db"] for row in path_contrast_cells),
        "minimum_5g8_selected_over_all_off_db": min(contrast_5g8),
        "maximum_5g8_selected_over_all_off_db": max(contrast_5g8),
        "minimum_5g8_path_over_all_off_db": min(path_contrast_5g8),
        "maximum_5g8_path_over_all_off_db": max(path_contrast_5g8),
        "maximum_model_heldout_phase_rms_deg": max(
            row["heldout_phase_rms_deg"] for row in path_models
        ),
        "minimum_model_heldout_phase_rms_deg": min(
            row["heldout_phase_rms_deg"] for row in path_models
        ),
        "maximum_model_heldout_gain_rms_db": max(row["heldout_gain_rms_db"] for row in path_models),
        "maximum_model_sweep_delay_span_ns": max(row["sweep_delay_span_ns"] for row in path_models),
        "maximum_ripple_model_heldout_phase_rms_deg": max(
            row["heldout_phase_rms_deg"] for row in path_ripple_models
        ),
        "minimum_ripple_model_heldout_phase_rms_deg": min(
            row["heldout_phase_rms_deg"] for row in path_ripple_models
        ),
        "maximum_ripple_model_heldout_gain_rms_db": max(
            row["heldout_gain_rms_db"] for row in path_ripple_models
        ),
        "delay_alias_period_ns": 10.0,
        "cross_validation_training_alias_period_ns": 5.0,
        "delay_search_interval_ns": [DELAY_SEARCH_MIN_NS, DELAY_SEARCH_MAX_NS],
        "maximum_raw_replay_absolute_delta": max(
            row["stored_transfer_replay_absolute_delta"] for row in all_rows
        ),
        "final_safety_passed": True,
    }
    return {
        "schema": 1,
        "evidence_kind": "smateway.pinned-external-broadband-campaign/v1",
        "campaign_id": "external-broadband-2g1-5g8-20260830",
        "fixture": {
            "receiver_uri": RADIO_URI,
            "receiver_serial": RADIO_SERIAL,
            "source_uri": SOURCE_URI,
            "source_serial": SOURCE_SERIAL,
            "selector_board_id": BOARD_ID,
            "stlink_serial": STLINK_SERIAL,
            "reference_channel": "RX1 behind the 2-way branch and 10 dB attenuator",
            "selected_channel": "RX2 behind the 2-way, 8-way, and selector",
        },
        "method": {
            "frequencies_hz": list(FREQUENCIES_HZ),
            "states": list(STATES),
            "complete_consecutive_sweeps": 3,
            "tx_gain_db": TX_GAIN_DB,
            "path_definition": "P_i(f) = H_i(f) - H_ALL_OFF(f)",
            "calibration_definition": "C_i(f) = P_ANT8(f) / P_i(f)",
            "delay_model": (
                "P_i(f)/P_ANT8(f) = A_i exp(j(phi_i - 2*pi*(f-f0)*tau_i)); "
                "positive tau means path i lags ANT8"
            ),
            "model_cross_validation": (
                "two folds: even bins train/odd bins held out, then odd train/even held out"
            ),
            "delay_search_interval_ns": [DELAY_SEARCH_MIN_NS, DELAY_SEARCH_MAX_NS],
            "delay_alias_note": (
                "100 MHz full-grid sampling aliases delay every 10 ns; each 200 MHz training "
                "fold aliases every 5 ns, so a predeclared [-2.5,+2.5) ns physical prior "
                "selects one branch"
            ),
            "log_ripple_model": (
                "log(P_i/P_ANT8) = log(A_i) + j(phi_i - 2*pi*(f-f0)*tau_i) "
                "+ q_i*exp(-j*2*pi*(f-f0)*delta_i); delta is searched on [0.05,2.5] ns "
                "and all other parameters are solved by linear least squares"
            ),
            "log_ripple_interpretation_limit": (
                "the ripple term is the first log-response harmonic of a weak single echo; "
                "its fitted delay is descriptive and is not a proven reflection location"
            ),
            "raw_replay": (
                "every NPZ is loaded with pickle disabled and the stored complex transfer is "
                "checked against a fresh pilot fit/projection"
            ),
            "artifact_binding_limit": (
                "SHA-256 binds the raw bytes as observed during report generation; no external "
                "capture-time seal exists"
            ),
        },
        "algorithm": {
            "analyzer_sha256": sha256_path(analyzer_path),
            "capture_runner_commit_sha256": capture_runner_commit_sha256,
            "pilot_estimator_source_sha256": sha256_path(pilot_source_path),
            "uv_lock_sha256": sha256_path(repository / "uv.lock"),
            "capture_commit": SOURCE_COMMIT,
            "python_version": sys.version.split()[0],
            "numpy_version": importlib.metadata.version("numpy"),
            "json_serialization": "sorted keys, finite JSON numbers, trailing newline",
        },
        "source_runs": run_documents,
        "summary": summary,
        "transfer_cells": transfer_cells,
        "calibration_relative_to_ant8": calibration_cells,
        "raw_calibration_relative_to_ant8": raw_calibration_cells,
        "selected_over_all_off": contrast_cells,
        "path_over_all_off": path_contrast_cells,
        "path_models": path_models,
        "path_ripple_models": path_ripple_models,
        "raw_observations": all_rows,
    }


def _series(rows: Iterable[Mapping[str, Any]], state: str, field: str) -> list[float]:
    return [float(row[field]) for row in rows if row["state"] == state]


def render_figures(result: Mapping[str, Any], output: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    frequency_ghz = np.asarray(FREQUENCIES_HZ, dtype=np.float64) / 1e9
    transfer = result["transfer_cells"]
    calibration = result["calibration_relative_to_ant8"]
    contrast = result["selected_over_all_off"]
    models = result["path_models"]
    ripple_models = result["path_ripple_models"]
    names: list[str] = []

    figure, axis = plt.subplots(figsize=(10, 5.8), constrained_layout=True)
    axis.plot(
        frequency_ghz,
        _series(transfer, "ALL_OFF", "mean_magnitude_db"),
        "--",
        color="black",
        label="ALL_OFF",
    )
    for state in SELECTED_STATES:
        axis.plot(frequency_ghz, _series(transfer, state, "mean_magnitude_db"), label=state)
    axis.set(xlabel="Center frequency (GHz)", ylabel="20 log10 |RX2/RX1| (dB)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=3, fontsize=8)
    name = "fig01_broadband_transfer.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, (gain_axis, phase_axis) = plt.subplots(
        2, 1, figsize=(10, 8), sharex=True, constrained_layout=True
    )
    for state in SELECTED_STATES:
        rows = [row for row in calibration if row["state"] == state]
        gain_axis.plot(frequency_ghz, [row["mean_magnitude_db"] for row in rows], label=state)
        phase = np.rad2deg(np.unwrap(np.angle([complex(row["real"], row["imag"]) for row in rows])))
        phase_axis.plot(frequency_ghz, phase, label=state)
    gain_axis.set(ylabel="Gain correction vs ANT8 (dB)")
    phase_axis.set(xlabel="Center frequency (GHz)", ylabel="Unwrapped phase correction (degrees)")
    gain_axis.grid(True, alpha=0.3)
    phase_axis.grid(True, alpha=0.3)
    gain_axis.legend(ncol=4, fontsize=8)
    name = "fig02_frequency_indexed_calibration.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, axis = plt.subplots(figsize=(10, 5.8), constrained_layout=True)
    for state in SELECTED_STATES:
        axis.plot(frequency_ghz, _series(contrast, state, "mean_db"), label=state)
    axis.axhline(20.0, color="black", linestyle="--", linewidth=1, label="20 dB screen")
    axis.set(xlabel="Center frequency (GHz)", ylabel="Selected / ALL_OFF contrast (dB)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=3, fontsize=8)
    name = "fig03_selected_off_contrast.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, axes = plt.subplots(4, 2, figsize=(11, 12), sharex=True, constrained_layout=True)
    axes_flat = axes.flat
    for axis, model in zip(axes_flat, models, strict=False):
        measured = np.asarray(model["measured_unwrapped_phase_deg"])
        predicted = np.asarray(model["predicted_phase_deg"])
        display_fold = model["cross_validation"][0]
        train = np.asarray(display_fold["training_indices"], dtype=int)
        heldout = np.asarray(display_fold["heldout_indices"], dtype=int)
        axis.plot(frequency_ghz, predicted, color="black", label="full single-delay fit")
        axis.scatter(frequency_ghz[train], measured[train], s=18, label="even frequency bins")
        axis.scatter(
            frequency_ghz[heldout],
            measured[heldout],
            marker="x",
            s=24,
            label="odd frequency bins",
        )
        axis.set_title(
            f"{model['state']} vs ANT8: {model['delay_ns']:+.3f} ns, "
            f"holdout {model['heldout_phase_rms_deg']:.1f} deg"
        )
        axis.grid(True, alpha=0.3)
    axes_flat[0].legend(fontsize=7)
    for axis in axes[:, 0]:
        axis.set_ylabel("Unwrapped phase (degrees)")
    for axis in axes[-1, :]:
        axis.set_xlabel("Center frequency (GHz)")
    axes_flat[-1].set_visible(False)
    name = "fig04_path_delay_models.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, axis = plt.subplots(figsize=(10, 5.8), constrained_layout=True)
    for model in models:
        axis.plot(frequency_ghz, model["phase_residual_deg"], label=model["state"])
    axis.axhline(5.0, color="black", linestyle="--", linewidth=1)
    axis.axhline(-5.0, color="black", linestyle="--", linewidth=1, label="+/-5 degrees")
    axis.set(xlabel="Center frequency (GHz)", ylabel="Delay-model phase residual (degrees)")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=4, fontsize=8)
    name = "fig05_path_model_residuals.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    selected_transfer = [row for row in transfer if row["state"] != "ALL_OFF"]
    max_magnitude_span = [
        max(
            row["magnitude_span_db"]
            for row in selected_transfer
            if row["frequency_hz"] == frequency
        )
        for frequency in FREQUENCIES_HZ
    ]
    max_phase_span = [
        max(row["phase_span_deg"] for row in selected_transfer if row["frequency_hz"] == frequency)
        for frequency in FREQUENCIES_HZ
    ]
    figure, (magnitude_axis, phase_axis) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True, constrained_layout=True
    )
    magnitude_axis.plot(frequency_ghz, max_magnitude_span, "-o", markersize=3)
    phase_axis.plot(frequency_ghz, max_phase_span, "-o", markersize=3)
    magnitude_axis.set(ylabel="Max three-sweep span (dB)")
    phase_axis.set(xlabel="Center frequency (GHz)", ylabel="Max three-sweep span (degrees)")
    magnitude_axis.grid(True, alpha=0.3)
    phase_axis.grid(True, alpha=0.3)
    name = "fig06_sweep_repeatability.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, (phase_axis, gain_axis) = plt.subplots(2, 1, figsize=(9, 7), constrained_layout=True)
    states = [model["state"] for model in models]
    phase_axis.bar(states, [model["heldout_phase_rms_deg"] for model in models])
    gain_axis.bar(states, [model["heldout_gain_rms_db"] for model in models])
    phase_axis.axhline(5.0, color="black", linestyle="--", linewidth=1, label="5 degree reference")
    phase_axis.set(ylabel="Held-out phase RMS (degrees)")
    gain_axis.set(ylabel="Held-out gain RMS (dB)")
    phase_axis.grid(True, axis="y", alpha=0.3)
    gain_axis.grid(True, axis="y", alpha=0.3)
    phase_axis.legend(fontsize=8)
    name = "fig07_model_quality.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    ripple_by_state = {model["state"]: model for model in ripple_models}
    figure, axes = plt.subplots(4, 2, figsize=(11, 12), sharex=True, constrained_layout=True)
    axes_flat = axes.flat
    for axis, delay_model in zip(axes_flat, models, strict=False):
        ripple_model = ripple_by_state[delay_model["state"]]
        measured = np.asarray(delay_model["measured_unwrapped_phase_deg"])
        axis.scatter(frequency_ghz, measured, color="black", s=16, label="measured")
        axis.plot(
            frequency_ghz,
            delay_model["predicted_phase_deg"],
            "--",
            color="tab:gray",
            label="single delay",
        )
        axis.plot(
            frequency_ghz,
            ripple_model["predicted_phase_deg"],
            color="tab:red",
            label="delay + one ripple",
        )
        axis.set_title(
            f"{delay_model['state']}: CV phase "
            f"{delay_model['heldout_phase_rms_deg']:.1f}→"
            f"{ripple_model['heldout_phase_rms_deg']:.1f}°, "
            f"δ={ripple_model['ripple_delay_ns']:.3f} ns"
        )
        axis.grid(True, alpha=0.3)
    axes_flat[0].legend(fontsize=7)
    for axis in axes[:, 0]:
        axis.set_ylabel("Unwrapped relative phase (degrees)")
    for axis in axes[-1, :]:
        axis.set_xlabel("Center frequency (GHz)")
    axes_flat[-1].set_visible(False)
    name = "fig08_ripple_model_fits.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)

    figure, (phase_axis, gain_axis) = plt.subplots(2, 1, figsize=(9, 7), constrained_layout=True)
    positions = np.arange(len(states))
    width = 0.38
    phase_axis.bar(
        positions - width / 2,
        [model["heldout_phase_rms_deg"] for model in models],
        width,
        label="single delay",
    )
    phase_axis.bar(
        positions + width / 2,
        [ripple_by_state[state]["heldout_phase_rms_deg"] for state in states],
        width,
        label="delay + one ripple",
    )
    gain_axis.bar(
        positions - width / 2,
        [model["heldout_gain_rms_db"] for model in models],
        width,
        label="single delay",
    )
    gain_axis.bar(
        positions + width / 2,
        [ripple_by_state[state]["heldout_gain_rms_db"] for state in states],
        width,
        label="delay + one ripple",
    )
    for axis in (phase_axis, gain_axis):
        axis.set_xticks(positions, states)
        axis.grid(True, axis="y", alpha=0.3)
    phase_axis.set(ylabel="Held-out phase RMS (degrees)")
    gain_axis.set(ylabel="Held-out gain RMS (dB)")
    phase_axis.legend(fontsize=8)
    name = "fig09_ripple_model_quality.png"
    figure.savefig(output / name, dpi=180)
    plt.close(figure)
    names.append(name)
    return names


def main() -> int:
    args = _parser().parse_args()
    result = analyze_campaign(args.run)
    result["figures"] = render_figures(result, args.figure_dir)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary_output = args.output_json.with_name(f".{args.output_json.name}.tmp")
    temporary_output.write_text(serialized, encoding="utf-8")
    temporary_output.replace(args.output_json)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
