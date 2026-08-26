from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/aggregate_hexcal_calibration.py"
SPEC = importlib.util.spec_from_file_location("hexcal_aggregation_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
aggregation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aggregation
SPEC.loader.exec_module(aggregation)

BASE_GAIN_DB = (-0.4, 0.7, -0.8, 0.5, -0.2, 0.2)
BASE_PHASE_DEG = (0.0, 20.0, -35.0, 70.0, -95.0, 145.0)


def _document(*, gain_delta: float, phase_delta: float) -> dict[str, Any]:
    states = []
    for index, (gain, phase) in enumerate(zip(BASE_GAIN_DB, BASE_PHASE_DEG, strict=True)):
        adjusted_gain = gain + gain_delta * (index - 2.5) / 5.0
        adjusted_phase = phase + phase_delta * index / 5.0
        value = 10.0 ** (adjusted_gain / 20.0) * complex(
            math.cos(math.radians(adjusted_phase)),
            math.sin(math.radians(adjusted_phase)),
        )
        states.append(
            {
                "name": f"ANT{index + 1}",
                "normalized_gain_db": adjusted_gain,
                "phase_circular_centered_deg": adjusted_phase,
                "normalized_complex": {"real": value.real, "imag": value.imag},
                "relative_gain_db": adjusted_gain,
                "phase_relative_to_ant1_deg": adjusted_phase,
                "relative_complex": {"real": value.real, "imag": value.imag},
            }
        )
    return {
        "quality_gate": {"passed": True},
        "hexcal": {
            "states": states,
            "decoded_cycle_fraction": 0.999,
            "residual_common_tone_offset_hz": 1.5 + phase_delta / 10.0,
            "timing": {
                "cycle_us": {"median": 1500.0 + gain_delta},
                "marker_observable_us": {"median": 200.0},
            },
        },
    }


def _records(*, heldout_phase_delta: float = 0.4) -> list[dict[str, Any]]:
    orders = ("forward", "reverse", "rotate_left_1")
    deltas = ((-0.1, -0.4), (0.1, 0.4), (0.05, heldout_phase_delta))
    return [
        {
            "artifact_id": str(round_index + 1) * 32,
            "plan_index": round_index,
            "round_index": round_index,
            "round_order": orders[round_index],
            "order_index": 0,
            "center_frequency_hz": 2_440_000_000,
            "emitted_carrier_frequency_hz": 2_440_100_000.0,
            "document": _document(gain_delta=gain_delta, phase_delta=phase_delta),
        }
        for round_index, (gain_delta, phase_delta) in enumerate(deltas)
    ]


def test_three_rounds_create_two_training_repeats_and_one_heldout_verification() -> None:
    result = aggregation.aggregate_frequency(_records())

    assert result["center_frequency_hz"] == 2_440_000_000
    assert result["training_capture_count"] == 2
    assert result["heldout_capture_count"] == 1
    assert result["heldout_round_index"] == 2
    assert result["training_round_indices"] == [0, 1]
    assert result["quality_gate"]["passed"] is True
    assert set(result["acquisition_order_diagnostic"]) == {
        "forward",
        "reverse",
        "rotate_left_1",
    }
    assert len(result["order_effect_diagnostics"]["pairwise_repeat_deltas"]) == 3
    assert result["order_effect_diagnostics"]["maximum_pairwise_phase_delta_rms_deg"] < 1.0
    coefficients = result["end_to_end_complex_correction_coefficients"]
    assert [item["name"] for item in coefficients] == [f"ANT{index}" for index in range(1, 7)]
    assert result["coefficient_phase_gauge"]["method"] == ("six_element_circular_phase_centre")
    coefficient_phase_mean = (
        sum(
            complex(
                math.cos(math.radians(float(item["phase_deg"]))),
                math.sin(math.radians(float(item["phase_deg"]))),
            )
            for item in coefficients
        )
        / 6.0
    )
    assert math.degrees(math.atan2(coefficient_phase_mean.imag, coefficient_phase_mean.real)) == (
        pytest.approx(0.0, abs=1e-10)
    )
    heldout = result["heldout_verification"][0]
    assert heldout["diagnostics"]["amplitude_span_db"] < 0.1
    assert heldout["diagnostics"]["phase_rms_deg"] < 1.0
    assert heldout["diagnostics"]["phase_resultant"] > 0.999
    assert heldout["diagnostics"]["opposite_pair_phase_mismatch_max_deg"] < 1.0
    assert heldout["quality_gate"]["per_element_gain_residual_passed"] is True
    assert heldout["quality_gate"]["per_element_phase_residual_passed"] is True
    assert heldout["six_point_dft"]["mode0_to_nonzero_rms_db"] > 30.0
    assert heldout["quality_gate"]["largest_noncommon_mode_minimum_passed"] is True
    assert len(result["leave_one_round_out_folds"]) == 3
    assert [fold["heldout_round_index"] for fold in result["leave_one_round_out_folds"]] == [
        0,
        1,
        2,
    ]
    assert all(fold["quality_gate"]["passed"] for fold in result["leave_one_round_out_folds"])


def test_pair_and_six_mode_diagnostics_are_explicit() -> None:
    result = aggregation.aggregate_frequency(_records())

    assert [item["pair"] for item in result["pair_diagnostics"]["opposite_elements"]] == [
        "ANT1-ANT4",
        "ANT2-ANT5",
        "ANT3-ANT6",
    ]
    assert [item["pair"] for item in result["pair_diagnostics"]["equal_route_length_checks"]] == [
        "ANT3-ANT6",
        "ANT4-ANT5",
    ]
    modes = result["training_six_point_dft"]["modes"]
    assert [mode["mode"] for mode in modes] == list(range(6))
    assert all("complex" in mode and "power" in mode for mode in modes)


def test_large_heldout_phase_change_is_reported_not_silently_absorbed() -> None:
    result = aggregation.aggregate_frequency(_records(heldout_phase_delta=60.0))

    assert result["quality_gate"]["passed"] is False
    assert result["quality_gate"]["between_round_phase_spread_passed"] is False
    assert result["quality_gate"]["all_leave_one_round_out_folds_passed"] is False
    assert result["heldout_verification"][0]["diagnostics"]["phase_rms_deg"] > 10.0


def test_common_phase_rotation_cannot_change_heldout_diagnostics() -> None:
    states = _document(gain_delta=0.0, phase_delta=0.0)["hexcal"]["states"]
    coefficients, _ = aggregation._gauge_normalized_correction_coefficients(states)
    values = aggregation._state_vector(_document(gain_delta=0.1, phase_delta=0.4))
    baseline, baseline_diagnostics = aggregation._apply_correction(values, coefficients)
    rotation = complex(math.cos(math.radians(73.0)), math.sin(math.radians(73.0)))
    rotated, rotated_diagnostics = aggregation._apply_correction(
        tuple(value * rotation for value in values), coefficients
    )

    assert rotated == pytest.approx(baseline, abs=1e-12)
    for key in (
        "gain_rms_db",
        "gain_max_abs_db",
        "amplitude_span_db",
        "phase_rms_deg",
        "phase_max_abs_deg",
        "phase_resultant",
        "opposite_pair_phase_mismatch_max_deg",
    ):
        assert rotated_diagnostics[key] == pytest.approx(baseline_diagnostics[key], abs=1e-12)
    assert rotated_diagnostics["phase_gauge"]["removed_common_phase_deg"] == pytest.approx(
        aggregation.wrapped_phase_deg(
            baseline_diagnostics["phase_gauge"]["removed_common_phase_deg"] + 73.0
        ),
        abs=1e-12,
    )


def test_order_effect_removes_capture_common_phase_without_using_ant1() -> None:
    records = _records()
    for state in records[1]["document"]["hexcal"]["states"]:
        state["phase_relative_to_ant1_deg"] += 47.0
        state["phase_circular_centered_deg"] += 47.0

    diagnostic = aggregation._order_effect_diagnostics(records[:2])
    pair = diagnostic["pairwise_repeat_deltas"][0]

    assert pair["phase_delta_gauge"]["reference_element"] == "none"
    assert pair["phase_delta_rms_deg"] < 1.0
    assert pair["phase_delta_max_abs_deg"] < 1.0


def test_ant1_phase_outlier_is_not_broadcast_to_every_other_element() -> None:
    values = (complex(1.0),) * 6
    coefficients = (
        complex(math.cos(math.radians(6.0)), math.sin(math.radians(6.0))),
        *(complex(1.0) for _ in range(5)),
    )

    _corrected, diagnostic = aggregation._apply_correction(values, coefficients)

    assert diagnostic["phase_gauge"]["reference_element"] == "none"
    assert diagnostic["phase_max_abs_deg"] < 6.0
    assert diagnostic["phase_max_abs_deg"] > 4.0
    assert diagnostic["phase_rms_deg"] < 3.0


def test_per_element_gain_limit_is_stricter_than_span_gate() -> None:
    records = _records()
    residuals = (0.55, -0.11, -0.11, -0.11, -0.11, -0.11)
    heldout_states = records[2]["document"]["hexcal"]["states"]
    for index, residual in enumerate(residuals):
        gain = BASE_GAIN_DB[index] + residual
        phase = BASE_PHASE_DEG[index]
        value = 10.0 ** (gain / 20.0) * complex(
            math.cos(math.radians(phase)), math.sin(math.radians(phase))
        )
        heldout_states[index]["relative_gain_db"] = gain
        heldout_states[index]["phase_relative_to_ant1_deg"] = phase
        heldout_states[index]["relative_complex"] = {
            "real": value.real,
            "imag": value.imag,
        }
        heldout_states[index]["normalized_gain_db"] = gain
        heldout_states[index]["phase_circular_centered_deg"] = phase
        heldout_states[index]["normalized_complex"] = {
            "real": value.real,
            "imag": value.imag,
        }

    result = aggregation.aggregate_frequency(records)
    gate = result["heldout_verification"][0]["quality_gate"]
    diagnostics = result["heldout_verification"][0]["diagnostics"]

    assert diagnostics["amplitude_span_db"] < 1.0
    assert diagnostics["gain_max_abs_db"] > 0.5
    assert gate["amplitude_span_passed"] is True
    assert gate["per_element_gain_residual_passed"] is False
    assert gate["passed"] is False


def test_two_passed_captures_are_insufficient_for_independent_holdout() -> None:
    result = aggregation.aggregate_frequency(_records()[:2])

    assert result["training_capture_count"] == 2
    assert result["heldout_capture_count"] == 0
    assert result["heldout_round_index"] == 2
    assert result["quality_gate"]["predeclared_headline_holdout_present"] is False
    assert result["quality_gate"]["passed"] is False


def test_emitted_carrier_group_must_match_within_one_hertz() -> None:
    records = _records()
    records[2]["emitted_carrier_frequency_hz"] += 2.0

    result = aggregation.aggregate_frequency(records)

    assert result["emitted_carrier_frequency_spread_hz"] == pytest.approx(2.0)
    assert result["quality_gate"]["emitted_carrier_group_passed"] is False
    assert result["quality_gate"]["passed"] is False


def test_leave_one_frequency_out_is_report_only_2g4_and_never_bridges_5g8() -> None:
    results = []
    for center_frequency_hz in (
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
        5_800_000_000,
    ):
        records = _records()
        for record in records:
            record["center_frequency_hz"] = center_frequency_hz
            record["emitted_carrier_frequency_hz"] = center_frequency_hz + 100_000.0
        results.append(aggregation.aggregate_frequency(records))

    diagnostic = aggregation.leave_one_frequency_out_2g4(results)

    assert diagnostic["mandatory_gate"] is False
    assert diagnostic["cross_band_interpolation_permitted"] is False
    assert diagnostic["included_center_frequencies_hz"] == [
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
    ]
    assert diagnostic["excluded_separate_band_center_frequencies_hz"] == [5_800_000_000]
    assert all(
        5_800_000_000 not in fold["training_center_frequencies_hz"] for fold in diagnostic["folds"]
    )
