#!/usr/bin/env python3
"""Aggregate passed hexcal repeats into per-frequency complex corrections."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_SMATEWAY_SOURCE = Path(__file__).resolve().parents[1] / "src"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX or str(_SMATEWAY_SOURCE) not in sys.path
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned hexcal Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

import numpy as np

from smateway.hexcal import (
    EXPECTED_STATE_NAMES,
    HEXCAL_AGGREGATION_SOURCE_FILES,
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    canonical_json_sha256,
    correction_coefficients,
    dft_document,
    sha256_path,
    wrapped_phase_deg,
    write_json_atomic,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
ANALYSIS_FILENAME = "hexcal-analysis.json"
OUTPUT_FILENAME = "hexcal-calibration.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _git_commit(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _complex(value: Mapping[str, Any]) -> complex:
    return complex(float(value["real"]), float(value["imag"]))


def _complex_dict(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _circular(values: Sequence[float]) -> tuple[float, float, float]:
    radians = np.deg2rad(np.asarray(values, dtype=float))
    mean = complex(np.mean(np.exp(1j * radians)))
    resultant = min(1.0, abs(mean))
    phase = wrapped_phase_deg(math.degrees(math.atan2(mean.imag, mean.real)))
    std = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(resultant, 1e-15)))))
    return phase, resultant, std


def _remove_circular_phase_gauge(
    values: Sequence[float],
) -> tuple[np.ndarray, dict[str, float | str]]:
    """Remove one common circular phase without privileging ANT1.

    The centred-TX experiment identifies relative element phases only up to one
    common rotation.  The circular mean of the six unit phasors is the frozen
    gauge used by the design.  A vanishing resultant makes that gauge undefined
    and is rejected rather than silently falling back to an element reference.
    """

    phases = np.asarray(values, dtype=float)
    if phases.shape != (6,) or not np.all(np.isfinite(phases)):
        raise ValueError("phase-gauge input must contain six finite values")
    mean = complex(np.mean(np.exp(1j * np.deg2rad(phases))))
    resultant = abs(mean)
    if resultant < 1e-12:
        raise ValueError("circular common-phase gauge is undefined")
    common_phase = wrapped_phase_deg(math.degrees(math.atan2(mean.imag, mean.real)))
    centred = np.asarray(
        [wrapped_phase_deg(float(value) - common_phase) for value in phases],
        dtype=float,
    )
    return centred, {
        "method": "six_element_circular_phase_centre",
        "removed_common_phase_deg": common_phase,
        "source_phase_resultant": float(resultant),
        "reference_element": "none",
    }


def _gauge_normalized_correction_coefficients(
    states: Sequence[Mapping[str, Any]],
) -> tuple[tuple[complex, ...], dict[str, float | str]]:
    raw = correction_coefficients(states)
    raw_phases = [math.degrees(math.atan2(value.imag, value.real)) for value in raw]
    centred_phases, gauge = _remove_circular_phase_gauge(raw_phases)
    normalized = tuple(
        complex(abs(value) * np.exp(1j * math.radians(float(phase))))
        for value, phase in zip(raw, centred_phases, strict=True)
    )
    return normalized, gauge


def _state_vector(document: Mapping[str, Any]) -> tuple[complex, ...]:
    hexcal = _mapping(document.get("hexcal"), "hexcal")
    raw_states = hexcal.get("states")
    if not isinstance(raw_states, list) or len(raw_states) != 6:
        raise ValueError("analysis does not contain six states")
    values = []
    for index, raw_state in enumerate(raw_states):
        state = _mapping(raw_state, f"states[{index}]")
        if state.get("name") != EXPECTED_STATE_NAMES[index]:
            raise ValueError("analysis state order differs from clockwise ANT1..ANT6")
        values.append(_complex(_mapping(state.get("normalized_complex"), "normalized_complex")))
    return tuple(values)


def _aggregate_states(documents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, name in enumerate(EXPECTED_STATE_NAMES):
        gains: list[float] = []
        phases: list[float] = []
        for document in documents:
            states = _mapping(document["hexcal"], "hexcal")["states"]
            if not isinstance(states, list):
                raise ValueError("hexcal states are malformed")
            state = _mapping(states[index], f"states[{index}]")
            gains.append(float(state["normalized_gain_db"]))
            phases.append(float(state["phase_circular_centered_deg"]))
        phase, resultant, phase_std = _circular(phases)
        gain = float(np.mean(gains))
        output.append(
            {
                "name": name,
                "normalized_gain_db": gain,
                "repeat_gain_std_db": float(np.std(gains)),
                "phase_circular_centered_deg": phase,
                "repeat_phase_resultant": resultant,
                "repeat_phase_std_deg": phase_std,
                "repeat_count": len(gains),
                "normalized_complex": _complex_dict(
                    complex(10.0 ** (gain / 20.0) * np.exp(1j * math.radians(phase)))
                ),
            }
        )
    return output


def _pair_document(states: Sequence[Mapping[str, Any]], first: int, second: int) -> dict[str, Any]:
    a = states[first]
    b = states[second]
    return {
        "pair": f"{a['name']}-{b['name']}",
        "gain_difference_db": float(a["normalized_gain_db"]) - float(b["normalized_gain_db"]),
        "phase_difference_deg": wrapped_phase_deg(
            float(a["phase_circular_centered_deg"]) - float(b["phase_circular_centered_deg"])
        ),
    }


def _apply_correction(
    values: Sequence[complex], coefficients: Sequence[complex]
) -> tuple[tuple[complex, ...], dict[str, Any]]:
    corrected = np.asarray(values, dtype=np.complex128) * np.asarray(
        coefficients, dtype=np.complex128
    )
    gains = 20.0 * np.log10(np.maximum(np.abs(corrected), 1e-15))
    gains -= np.mean(gains)
    raw_phases = np.rad2deg(np.angle(corrected))
    phases, phase_gauge = _remove_circular_phase_gauge(raw_phases.tolist())
    normalized = 10.0 ** (gains / 20.0) * np.exp(1j * np.deg2rad(phases))
    resultant = abs(complex(np.mean(np.exp(1j * np.deg2rad(phases)))))
    opposite_mismatch = max(
        abs(wrapped_phase_deg(float(phases[first] - phases[second])))
        for first, second in ((0, 3), (1, 4), (2, 5))
    )
    diagnostics = {
        "gain_rms_db": float(math.sqrt(np.mean(gains**2))),
        "gain_max_abs_db": float(np.max(np.abs(gains))),
        "amplitude_span_db": float(np.max(gains) - np.min(gains)),
        "phase_rms_deg": float(math.sqrt(np.mean(phases**2))),
        "phase_max_abs_deg": float(np.max(np.abs(phases))),
        "phase_resultant": float(resultant),
        "opposite_pair_phase_mismatch_max_deg": float(opposite_mismatch),
        "phase_gauge": phase_gauge,
    }
    return tuple(complex(value) for value in normalized), diagnostics


def _timing_diagnostics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cycles: list[float] = []
    markers: list[float] = []
    decoded: list[float] = []
    residuals: list[float] = []
    for record in records:
        document = _mapping(record["document"], "analysis document")
        hexcal = _mapping(document["hexcal"], "hexcal")
        timing = _mapping(hexcal["timing"], "timing")
        cycles.append(float(_mapping(timing["cycle_us"], "cycle_us")["median"]))
        markers.append(
            float(_mapping(timing["marker_observable_us"], "marker_observable_us")["median"])
        )
        decoded.append(float(hexcal["decoded_cycle_fraction"]))
        residuals.append(float(hexcal["residual_common_tone_offset_hz"]))
    return {
        "capture_count": len(records),
        "cycle_median_us_range": [min(cycles), max(cycles)],
        "marker_median_us_range": [min(markers), max(markers)],
        "decoded_cycle_fraction_range": [min(decoded), max(decoded)],
        "residual_common_tone_offset_hz_range": [min(residuals), max(residuals)],
    }


def _order_effect_diagnostics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pairwise = []
    for first_index, first in enumerate(records):
        first_states = _mapping(first["document"], "first document")["hexcal"]["states"]
        if not isinstance(first_states, list):
            raise ValueError("first order-diagnostic state list is malformed")
        first_gain = np.asarray(
            [float(_mapping(state, "first state")["normalized_gain_db"]) for state in first_states]
        )
        first_phase = np.asarray(
            [
                float(_mapping(state, "first state")["phase_circular_centered_deg"])
                for state in first_states
            ]
        )
        for second in records[first_index + 1 :]:
            second_states = _mapping(second["document"], "second document")["hexcal"]["states"]
            if not isinstance(second_states, list):
                raise ValueError("second order-diagnostic state list is malformed")
            second_gain = np.asarray(
                [
                    float(_mapping(state, "second state")["normalized_gain_db"])
                    for state in second_states
                ]
            )
            second_phase = np.asarray(
                [
                    float(_mapping(state, "second state")["phase_circular_centered_deg"])
                    for state in second_states
                ]
            )
            gain_delta = first_gain - second_gain
            raw_phase_delta = np.asarray(
                [wrapped_phase_deg(float(value)) for value in first_phase - second_phase]
            )
            phase_delta, phase_delta_gauge = _remove_circular_phase_gauge(raw_phase_delta.tolist())
            pairwise.append(
                {
                    "first_artifact_id": first["artifact_id"],
                    "first_round_order": first["round_order"],
                    "second_artifact_id": second["artifact_id"],
                    "second_round_order": second["round_order"],
                    "gain_delta_rms_db": float(math.sqrt(np.mean(gain_delta**2))),
                    "gain_delta_max_abs_db": float(np.max(np.abs(gain_delta))),
                    "phase_delta_rms_deg": float(math.sqrt(np.mean(phase_delta**2))),
                    "phase_delta_max_abs_deg": float(np.max(np.abs(phase_delta))),
                    "phase_delta_gauge": phase_delta_gauge,
                }
            )
    return {
        "pairwise_repeat_deltas": pairwise,
        "maximum_pairwise_gain_delta_rms_db": max(
            (item["gain_delta_rms_db"] for item in pairwise), default=0.0
        ),
        "maximum_pairwise_phase_delta_rms_deg": max(
            (item["phase_delta_rms_deg"] for item in pairwise), default=0.0
        ),
        "maximum_pairwise_gain_delta_max_abs_db": max(
            (item["gain_delta_max_abs_db"] for item in pairwise), default=0.0
        ),
        "maximum_pairwise_phase_delta_max_abs_deg": max(
            (item["phase_delta_max_abs_deg"] for item in pairwise), default=0.0
        ),
    }


def _leave_one_round_out_fold(
    records: Sequence[Mapping[str, Any]], heldout_round: int
) -> dict[str, Any]:
    expected_rounds = {0, 1, 2}
    training_rounds = expected_rounds - {heldout_round}
    training_records = [item for item in records if int(item["round_index"]) in training_rounds]
    heldout_records = [item for item in records if int(item["round_index"]) == heldout_round]
    training_documents = [
        _mapping(item["document"], "training document") for item in training_records
    ]
    aggregate_states = _aggregate_states(training_documents) if training_documents else []
    if aggregate_states:
        coefficients, coefficient_gauge = _gauge_normalized_correction_coefficients(
            aggregate_states
        )
    else:
        coefficients = ()
        coefficient_gauge = None
    heldout: list[dict[str, Any]] = []
    for item in heldout_records:
        if len(coefficients) != 6:
            heldout.append(
                {
                    "artifact_id": item["artifact_id"],
                    "round_index": item["round_index"],
                    "round_order": item["round_order"],
                    "corrected_states": [],
                    "diagnostics": None,
                    "six_point_dft": None,
                    "quality_gate": {
                        "passed": False,
                        "reason": "two_training_rounds_not_available",
                    },
                }
            )
            continue
        values = _state_vector(_mapping(item["document"], "heldout document"))
        corrected, diagnostics = _apply_correction(values, coefficients)
        dft = dft_document(corrected)
        gate = {
            "amplitude_span_passed": diagnostics["amplitude_span_db"] <= 1.0,
            "per_element_gain_residual_passed": diagnostics["gain_max_abs_db"] <= 0.5,
            "phase_rms_passed": diagnostics["phase_rms_deg"] <= 5.0,
            "per_element_phase_residual_passed": diagnostics["phase_max_abs_deg"] <= 5.0,
            "phase_resultant_passed": diagnostics["phase_resultant"] >= 0.995,
            "opposite_pair_phase_passed": diagnostics["opposite_pair_phase_mismatch_max_deg"]
            <= 5.0,
            "largest_noncommon_mode_minimum_passed": dft["largest_noncommon_mode_minimum_passed"],
            "largest_noncommon_mode_target_passed": dft["largest_noncommon_mode_target_passed"],
        }
        gate["passed"] = all(
            value for key, value in gate.items() if key != "largest_noncommon_mode_target_passed"
        )
        heldout.append(
            {
                "artifact_id": item["artifact_id"],
                "round_index": item["round_index"],
                "round_order": item["round_order"],
                "corrected_states": [
                    {"name": name, "complex": _complex_dict(value)}
                    for name, value in zip(EXPECTED_STATE_NAMES, corrected, strict=True)
                ],
                "diagnostics": diagnostics,
                "six_point_dft": dft,
                "quality_gate": gate,
            }
        )
    exact_round_membership = (
        len(training_records) == 2
        and {int(item["round_index"]) for item in training_records} == training_rounds
        and len(heldout_records) == 1
    )
    heldout_passed = exact_round_membership and all(
        bool(item["quality_gate"]["passed"]) for item in heldout
    )
    return {
        "heldout_round_index": heldout_round,
        "training_round_indices": sorted(training_rounds),
        "training_capture_count": len(training_records),
        "heldout_capture_count": len(heldout_records),
        "training_states": aggregate_states,
        "coefficients": coefficients,
        "coefficient_phase_gauge": coefficient_gauge,
        "heldout_verification": heldout,
        "quality_gate": {
            "passed": heldout_passed,
            "exact_round_membership": exact_round_membership,
            "all_heldout_gates_passed": bool(heldout)
            and all(bool(item["quality_gate"]["passed"]) for item in heldout),
        },
    }


def aggregate_frequency(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build all LORO folds with round 3 as the predeclared headline holdout."""

    if not records:
        raise ValueError("cannot aggregate an empty frequency")
    ordered = sorted(records, key=lambda item: (int(item["round_index"]), int(item["plan_index"])))
    folds = [_leave_one_round_out_fold(ordered, heldout) for heldout in range(3)]
    headline = folds[2]
    aggregate_states = headline["training_states"]
    coefficients = headline["coefficients"]
    heldout = headline["heldout_verification"]
    order_effect = _order_effect_diagnostics(ordered)
    between_gain_passed = order_effect["maximum_pairwise_gain_delta_max_abs_db"] <= 0.5
    between_phase_passed = order_effect["maximum_pairwise_phase_delta_max_abs_deg"] <= 5.0
    carriers = [float(item["emitted_carrier_frequency_hz"]) for item in ordered]
    carrier_spread_hz = max(carriers) - min(carriers)
    carrier_group_passed = carrier_spread_hz <= 1.0
    training_vector = tuple(
        _complex(_mapping(state["normalized_complex"], "normalized_complex"))
        for state in aggregate_states
    )
    order_groups: dict[str, list[str]] = defaultdict(list)
    for item in ordered:
        order_groups[str(item["round_order"])].append(str(item["artifact_id"]))
    return {
        "center_frequency_hz": int(ordered[0]["center_frequency_hz"]),
        "emitted_carrier_frequency_hz": float(np.mean(carriers)),
        "emitted_carrier_frequency_spread_hz": carrier_spread_hz,
        "maximum_emitted_carrier_group_spread_hz": 1.0,
        "passed_capture_count": len(ordered),
        "training_capture_count": headline["training_capture_count"],
        "heldout_capture_count": headline["heldout_capture_count"],
        "heldout_round_index": 2,
        "training_round_indices": [0, 1],
        "acquisition_order_diagnostic": dict(sorted(order_groups.items())),
        "order_effect_diagnostics": order_effect,
        "timing_diagnostics": _timing_diagnostics(ordered),
        "training_states": aggregate_states,
        "coefficient_phase_gauge": headline["coefficient_phase_gauge"],
        "end_to_end_complex_correction_coefficients": [
            {
                "name": name,
                "complex": _complex_dict(value),
                "gain_db": 20.0 * math.log10(max(abs(value), 1e-15)),
                "phase_deg": wrapped_phase_deg(math.degrees(math.atan2(value.imag, value.real))),
            }
            for name, value in zip(EXPECTED_STATE_NAMES, coefficients, strict=True)
        ],
        "pair_diagnostics": {
            "opposite_elements": [
                _pair_document(aggregate_states, 0, 3),
                _pair_document(aggregate_states, 1, 4),
                _pair_document(aggregate_states, 2, 5),
            ]
            if aggregate_states
            else [],
            "equal_route_length_checks": [
                _pair_document(aggregate_states, 2, 5),
                _pair_document(aggregate_states, 3, 4),
            ]
            if aggregate_states
            else [],
            "equal_route_length_basis": (
                "ANT3-ANT6 and ANT4-ANT5 are the PCB equal-route-length checks; "
                "opposite-element symmetry is diagnosed separately."
            ),
        },
        "training_six_point_dft": dft_document(training_vector) if training_vector else None,
        "heldout_verification": heldout,
        "leave_one_round_out_folds": [
            {
                **{key: value for key, value in fold.items() if key != "coefficients"},
                "end_to_end_complex_correction_coefficients": [
                    {
                        "name": name,
                        "complex": _complex_dict(value),
                        "gain_db": 20.0 * math.log10(max(abs(value), 1e-15)),
                        "phase_deg": wrapped_phase_deg(
                            math.degrees(math.atan2(value.imag, value.real))
                        ),
                    }
                    for name, value in zip(EXPECTED_STATE_NAMES, fold["coefficients"], strict=True)
                ],
            }
            for fold in folds
        ],
        "quality_gate": {
            "passed": (
                between_gain_passed
                and between_phase_passed
                and carrier_group_passed
                and all(bool(fold["quality_gate"]["passed"]) for fold in folds)
            ),
            "predeclared_headline_holdout_round_index": 2,
            "predeclared_headline_holdout_present": headline["heldout_capture_count"] == 1,
            "between_round_gain_spread_passed": between_gain_passed,
            "between_round_phase_spread_passed": between_phase_passed,
            "emitted_carrier_group_passed": carrier_group_passed,
            "all_leave_one_round_out_folds_passed": all(
                bool(fold["quality_gate"]["passed"]) for fold in folds
            ),
            "maximum_between_round_gain_spread_db": 0.5,
            "maximum_between_round_phase_spread_deg": 5.0,
            "maximum_emitted_carrier_spread_hz": 1.0,
            "heldout_corrected_amplitude_span_maximum_db": 1.0,
            "heldout_per_element_gain_residual_maximum_db": 0.5,
            "heldout_corrected_phase_rms_maximum_deg": 5.0,
            "heldout_per_element_phase_residual_maximum_deg": 5.0,
            "heldout_corrected_phase_resultant_minimum": 0.995,
            "heldout_opposite_pair_phase_mismatch_maximum_deg": 5.0,
            "largest_noncommon_mode_target_maximum_dbc": -20.0,
            "largest_noncommon_mode_minimum_maximum_dbc": -15.0,
        },
    }


def _load_passed_records(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in manifest.get("attempts", []):
        if not isinstance(raw, Mapping) or raw.get("outcome") != "quality_passed":
            continue
        identity = _mapping(raw.get("artifact_identity"), "artifact identity")
        analysis_path = Path(str(identity["path"])) / ANALYSIS_FILENAME
        if sha256_path(analysis_path) != identity.get("analysis_sha256"):
            raise ValueError("analysis SHA-256 changed after runner acceptance")
        document = json.loads(analysis_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("analysis root is not an object")
        quality = _mapping(document.get("quality_gate"), "quality gate")
        if quality.get("passed") is not True:
            raise ValueError("runner marks a non-passing analysis as quality passed")
        key = _mapping(document.get("aggregation_key"), "aggregation key")
        dds_readback = key.get("dds_frequency_readback_hz")
        emitted_carrier = key.get("emitted_carrier_frequency_hz")
        if (
            not isinstance(dds_readback, list)
            or len(dds_readback) != 8
            or identity.get("dds_frequency_readback_hz") != dds_readback
            or key.get("implementation_source_commit")
            != identity.get("implementation_source_commit")
            or key.get("firmware_evidence_sha256") != identity.get("firmware_evidence_sha256")
            or key.get("firmware_bin_sha256") != identity.get("firmware_bin_sha256")
            or key.get("full_flash_readback_sha256") != identity.get("full_flash_readback_sha256")
            or key.get("pluto_plus_utils_source_attestation_sha256")
            != identity.get("pluto_plus_utils_source_attestation_sha256")
            or key.get("rf_readback_evidence_sha256") != identity.get("rf_readback_evidence_sha256")
            or not isinstance(emitted_carrier, (int, float))
            or isinstance(emitted_carrier, bool)
            or not math.isfinite(float(emitted_carrier))
            or identity.get("emitted_carrier_frequency_hz") != emitted_carrier
        ):
            raise ValueError("analysis emitted carrier differs from runner identity")
        records.append(
            {
                "artifact_id": identity["artifact_id"],
                "plan_index": raw["plan_index"],
                "round_index": raw["round_index"],
                "round_order": raw["round_order"],
                "order_index": raw["order_index"],
                "center_frequency_hz": raw["center_frequency_hz"],
                "dds_frequency_readback_hz": dds_readback,
                "emitted_carrier_frequency_hz": float(emitted_carrier),
                "document": document,
            }
        )
    return records


def leave_one_frequency_out_2g4(
    frequency_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Report 2.4 GHz-only coefficient interpolation without crossing to 5.8 GHz."""

    qualified = sorted(
        (
            result
            for result in frequency_results
            if 2_400_000_000 <= int(result["center_frequency_hz"]) <= 2_483_500_000
        ),
        key=lambda item: int(item["center_frequency_hz"]),
    )
    excluded = sorted(
        int(result["center_frequency_hz"])
        for result in frequency_results
        if not 2_400_000_000 <= int(result["center_frequency_hz"]) <= 2_483_500_000
    )
    folds = []
    for target in qualified:
        others = [item for item in qualified if item is not target]
        state_errors: list[dict[str, Any]] = []
        if len(others) >= 2:
            other_x = np.asarray(
                [float(item["emitted_carrier_frequency_hz"]) for item in others],
                dtype=float,
            )
            target_x = float(target["emitted_carrier_frequency_hz"])
            x_origin = float(np.mean(other_x))
            x_scale = 1_000_000.0
            for state_index, name in enumerate(EXPECTED_STATE_NAMES):
                other_coefficients = [
                    item["end_to_end_complex_correction_coefficients"][state_index]
                    for item in others
                ]
                gains = np.asarray(
                    [float(coefficient["gain_db"]) for coefficient in other_coefficients]
                )
                phases = np.unwrap(
                    np.deg2rad(
                        [float(coefficient["phase_deg"]) for coefficient in other_coefficients]
                    )
                )
                gain_fit = np.polyfit((other_x - x_origin) / x_scale, gains, 1)
                phase_fit = np.polyfit((other_x - x_origin) / x_scale, phases, 1)
                target_coordinate = (target_x - x_origin) / x_scale
                predicted_gain = float(np.polyval(gain_fit, target_coordinate))
                predicted_phase = wrapped_phase_deg(
                    math.degrees(float(np.polyval(phase_fit, target_coordinate)))
                )
                actual = target["end_to_end_complex_correction_coefficients"][state_index]
                state_errors.append(
                    {
                        "name": name,
                        "predicted_gain_db": predicted_gain,
                        "actual_gain_db": float(actual["gain_db"]),
                        "gain_error_db": predicted_gain - float(actual["gain_db"]),
                        "predicted_phase_deg": predicted_phase,
                        "actual_phase_deg": float(actual["phase_deg"]),
                        "phase_error_deg": wrapped_phase_deg(
                            predicted_phase - float(actual["phase_deg"])
                        ),
                    }
                )
        gain_errors = [float(item["gain_error_db"]) for item in state_errors]
        phase_errors = [float(item["phase_error_deg"]) for item in state_errors]
        folds.append(
            {
                "heldout_center_frequency_hz": int(target["center_frequency_hz"]),
                "training_center_frequencies_hz": [
                    int(item["center_frequency_hz"]) for item in others
                ],
                "state_residuals": state_errors,
                "gain_error_rms_db": (
                    float(math.sqrt(np.mean(np.asarray(gain_errors) ** 2))) if gain_errors else None
                ),
                "phase_error_rms_deg": (
                    float(math.sqrt(np.mean(np.asarray(phase_errors) ** 2)))
                    if phase_errors
                    else None
                ),
                "available": bool(state_errors),
            }
        )
    return {
        "mandatory_gate": False,
        "method": (
            "leave-one-frequency-out linear least-squares gain/unwrapped-phase fit "
            "within the qualified 2.4 GHz band"
        ),
        "included_center_frequencies_hz": [int(item["center_frequency_hz"]) for item in qualified],
        "excluded_separate_band_center_frequencies_hz": excluded,
        "cross_band_interpolation_permitted": False,
        "folds": folds,
    }


def build_calibration_scientific_payload(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically rebuild every scientific calibration result.

    This pure payload builder is shared with the independent auditor.  Identity,
    path and source-attestation fields remain the responsibility of their
    respective entry points; every coefficient and gate is produced here.
    """

    configuration = _mapping(manifest.get("configuration"), "configuration")
    raw_expected = configuration.get("center_frequencies_hz")
    if not isinstance(raw_expected, list) or not raw_expected:
        raise ValueError("planned center-frequency list is missing")
    expected = [int(value) for value in raw_expected]
    if len(set(expected)) != len(expected):
        raise ValueError("planned center frequencies must be unique")
    records = _load_passed_records(manifest)
    by_frequency: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_frequency[int(record["center_frequency_hz"])].append(record)
    unexpected = sorted(set(by_frequency) - set(expected))
    if unexpected:
        raise ValueError(f"passing artifacts contain unplanned frequencies: {unexpected}")
    frequencies = [
        aggregate_frequency(by_frequency[frequency])
        for frequency in expected
        if by_frequency[frequency]
    ]
    observed = {int(item["center_frequency_hz"]) for item in frequencies}
    missing = [frequency for frequency in expected if frequency not in observed]
    all_frequency_gates_passed = all(bool(item["quality_gate"]["passed"]) for item in frequencies)
    return {
        "frequency_results": frequencies,
        "leave_one_frequency_out_2g4": leave_one_frequency_out_2g4(frequencies),
        "missing_passing_frequencies_hz": missing,
        "quality_gate": {
            "passed": not missing and all_frequency_gates_passed,
            "all_planned_frequencies_have_passing_repeats": not missing,
            "all_frequency_gates_passed": all_frequency_gates_passed,
        },
    }


def main() -> int:
    args = _parser().parse_args()
    board_root = Path.home() / ".local/state/smateway/boards" / args.board_id
    run_root = board_root / "hexcal-distributions" / args.run_id
    manifest_path = run_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load run manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("experiment_kind") not in {
        "hexcal_v1_tx1_center_calibration",
        "hexcal_v2_2g4_tx1_center_calibration",
    }:
        raise SystemExit("manifest is not a supported Hexcal calibration run")
    configuration = _mapping(manifest.get("configuration"), "configuration")
    protocol_id = configuration.get("protocol_id", "hexcal-v1")
    expected_experiment_kind = (
        "hexcal_v2_2g4_tx1_center_calibration"
        if protocol_id == "hexcal-v2-2g4-stimulus"
        else "hexcal_v1_tx1_center_calibration"
    )
    if manifest.get("experiment_kind") != expected_experiment_kind:
        raise SystemExit("manifest experiment kind differs from its protocol ID")
    if configuration.get("serial") != args.serial or configuration.get("uri") != args.uri:
        raise SystemExit("explicit serial/URI differ from the persisted run")
    if manifest.get("status") != "complete":
        raise SystemExit("run must complete before calibration aggregation")
    repository = Path(__file__).resolve().parents[1]
    implementation_commit = str(configuration.get("implementation_source_commit", ""))
    try:
        configured_dependency = _mapping(
            configuration.get("pluto_plus_utils_source_attestation"),
            "configured pluto-plus-utils source attestation",
        )
        dependency_attestation = attest_pluto_plus_utils_source()
        dependency_sha256 = canonical_json_sha256(dependency_attestation)
        if dependency_attestation != dict(
            configured_dependency
        ) or dependency_sha256 != configuration.get("pluto_plus_utils_source_attestation_sha256"):
            raise ValueError("aggregation dependency runtime differs from the persisted run")
        source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=implementation_commit,
            relative_paths=HEXCAL_AGGREGATION_SOURCE_FILES,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"aggregation source attestation failed: {error}") from error
    try:
        scientific_payload = build_calibration_scientific_payload(manifest)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot aggregate attested analyses: {error}") from error
    frequencies = scientific_payload["frequency_results"]
    overall_passed = bool(scientific_payload["quality_gate"]["passed"])
    qualification_kind = "stimulus" if "stimulus_qualification" in configuration else "gain"
    qualification = _mapping(
        configuration.get(f"{qualification_kind}_qualification"),
        f"{qualification_kind} qualification",
    )
    output = {
        "schema": 1,
        "calibration_kind": (
            "hexcal_v2_2g4_tx1_center_end_to_end_complex_correction"
            if protocol_id == "hexcal-v2-2g4-stimulus"
            else "hexcal_v1_tx1_center_end_to_end_complex_correction"
        ),
        "protocol_id": protocol_id,
        "source_commit": implementation_commit,
        "aggregation_runtime_head": _git_commit(repository),
        "aggregation_python_runtime": {
            "executable": dependency_attestation["python_executable"],
            "prefix": dependency_attestation["python_prefix"],
        },
        "aggregation_source_attestation": source_attestation,
        "capture_implementation_source_commit": configuration["implementation_source_commit"],
        "run_id": args.run_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "serial": args.serial,
        "uri": args.uri,
        "profile_file_sha256": configuration["profile_file_sha256"],
        "profile_contract_sha256": configuration["profile_contract_sha256"],
        "receiver_gain_db": configuration["receiver_gain_db"],
        "qualification_kind": qualification_kind,
        "qualification": dict(qualification),
        "gain_qualification": configuration.get("gain_qualification"),
        "stimulus_qualification": configuration.get("stimulus_qualification"),
        "firmware_evidence": configuration["firmware_evidence"],
        "pluto_plus_utils_source_attestation": dependency_attestation,
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "array_geometry": {
            "element_count": 6,
            "diameter_mm": 51.0,
            "order": list(EXPECTED_STATE_NAMES),
            "direction": "clockwise",
            "forward_reference": "ANT1",
            "clockwise_bearings_from_forward_deg": [0, 60, 120, 180, 240, 300],
            "source": "TX1 at nominal array center",
        },
        **scientific_payload,
        "scope_limit": (
            "Coefficients flatten the measured centered-TX1 near-field manifold at each "
            "listed frequency. Do not treat them as geometry-only or apply them as "
            "absolute phase across independent captures."
        ),
    }
    output_path = run_root / OUTPUT_FILENAME
    write_json_atomic(output_path, output)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "calibration": str(output_path),
                "calibration_sha256": sha256_path(output_path),
                "quality_passed": overall_passed,
                "frequency_count": len(frequencies),
            },
            sort_keys=True,
        )
    )
    return 0 if overall_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
