from __future__ import annotations

import hashlib
import json
from math import atan2, degrees, log10
from typing import Any

import pytest

from smateway.one_hot_ladder import (
    ALL_OFF_STATE,
    ANTENNA_GPIO_CODES,
    ANTENNA_STATES,
    ONE_HOT_STATE_ORDER,
    TOPOLOGY_IDENTITY,
    _seal_verified_one_hot_row_bundle,
    physical_confirmation_token,
    summarize_complete_one_hot_matrix,
    summarize_one_hot_run,
    validate_one_hot_state_codes,
)

GAINS = (-20.0, -10.0)
ATTRIBUTION_GAIN = -20.0
ATTRIBUTION_REPEATS = 3
SHARED_FIXTURE = {
    "feed_arm_id": "feed-arm-a",
    "feed_cable_id": "feed-cable-a",
    "termination_load_set_id": "loads-a",
    "rx1_reference_plane_id": "rx1-plane-a",
    "rx2_reference_plane_id": "rx2-plane-a",
}


def _fixture(evidence_index: int = 1) -> dict[str, Any]:
    return {
        "shared_hardware": dict(SHARED_FIXTURE),
        "setup_evidence": {
            "path": f"/evidence/row-{evidence_index}.json",
            "file_sha256": _digest(500 + evidence_index),
        },
        "attribution_repeats_without_cable_movement_required": True,
    }


def _digest(value: int) -> str:
    return f"{value:064x}"


def _matrix_identity() -> dict[str, Any]:
    acquisition = {
        "center_frequency_hz": 5_800_000_000,
        "sample_rate_hz": 1_000_000,
        "tx_hardware_gains_db": [-20.0, -10.0],
    }
    acquisition_sha = hashlib.sha256(
        json.dumps(
            acquisition,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "board_id": "board-a",
        "pluto_serial": "pluto-a",
        "smateway_commit": "1" * 40,
        "pluto_plus_utils_source_attestation_sha256": _digest(1),
        "bench_manifest_sha256": _digest(2),
        "bench_elf_sha256": _digest(3),
        "bench_bin_sha256": _digest(4),
        "bench_protocol_sha256": _digest(5),
        "bench_verifier_sha256": _digest(6),
        "openocd_config_sha256": _digest(7),
        "control_profile_contract_sha256": _digest(8),
        "control_profile_sha256": _digest(9),
        "control_profile_header_sha256": _digest(10),
        "control_profile_provenance_sha256": _digest(11),
        "acquisition_configuration": acquisition,
        "acquisition_configuration_sha256": acquisition_sha,
    }


def _transfer(phasor: complex, *, detected: bool) -> dict[str, Any]:
    amplitude = abs(phasor)
    return {
        "phasor": {"real": phasor.real, "imag": phasor.imag},
        "amplitude_ratio": amplitude,
        "amplitude_db": 20.0 * log10(amplitude) if detected else None,
        "phase_deg": degrees(atan2(phasor.imag, phasor.real)) if detected else None,
        "amplitude_upper_bound_ratio": None if detected else 0.01,
        "amplitude_upper_bound_db": None if detected else -40.0,
    }


def _row(
    driven_input: str,
    *,
    common_all_off_tone: bool = False,
    quality_failure: tuple[str, float, int] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for gain in GAINS:
        repeat_count = ATTRIBUTION_REPEATS if gain == ATTRIBUTION_GAIN else 1
        for state in ONE_HOT_STATE_ORDER:
            for repeat_index in range(repeat_count):
                all_off_phasor = complex(0.01, 0.0)
                intended = state == driven_input
                if state == ALL_OFF_STATE:
                    phasor = 0.10 + 0.01j if common_all_off_tone else all_off_phasor
                    detected = common_all_off_tone
                elif intended:
                    phasor = (
                        0.10 + 0.01j
                        if common_all_off_tone
                        else all_off_phasor + 0.10 * complex(1.0, 0.01 * repeat_index)
                    )
                    detected = True
                else:
                    phasor = 0.004 + 0.001j
                    detected = False
                quality = quality_failure != (state, gain, repeat_index)
                results.append(
                    {
                        "topology_identity": TOPOLOGY_IDENTITY,
                        "driven_input": driven_input,
                        "fixture_identity": _fixture(ANTENNA_STATES.index(driven_input) + 1),
                        "selector_state_name": state,
                        "tx_hardware_gain_db": gain,
                        "repeat_index": repeat_index,
                        "repeat_count_at_gain": repeat_count,
                        "measurement_quality_passed": quality,
                        "measurement_quality_rejection_reasons": (
                            [] if quality else ["synthetic_quality_failure"]
                        ),
                        "rx2_tone_detected": detected,
                        "rx2_over_rx1": _transfer(phasor, detected=detected),
                    }
                )
    return results


def _summarize(results: list[dict[str, Any]], driven_input: str = "ANT3") -> Any:
    return summarize_one_hot_run(
        results,
        driven_input=driven_input,
        fixture_identity=_fixture(ANTENNA_STATES.index(driven_input) + 1),
        planned_gains_db=GAINS,
        attribution_gain_db=ATTRIBUTION_GAIN,
        attribution_repeat_count=ATTRIBUTION_REPEATS,
        minimum_detected_attribution_repeats=3,
    )


def _verified_bundle(driven_input: str, row_index: int) -> dict[str, Any]:
    plan_contract_sha = _digest(100 + row_index)
    plan_file_sha = _digest(200 + row_index)
    results = _row(driven_input)
    for condition_index, result in enumerate(results):
        identity = row_index * 1_000 + condition_index + 1
        result.update(
            {
                "immutable_plan": {
                    "plan_contract_sha256": plan_contract_sha,
                    "plan_file_sha256": plan_file_sha,
                },
                "artifact_id": f"artifact-{identity}",
                "artifact_data_sha256": _digest(10_000 + identity),
                "artifact_metadata_sha256": _digest(20_000 + identity),
                "condition_record_sha256": _digest(30_000 + identity),
                "stream_id": 40_000 + identity,
            }
        )
    return {
        "schema": 1,
        "row_bundle_kind": "verified_one_hot_row",
        "run_id": f"row-{driven_input.lower()}",
        "topology_identity": TOPOLOGY_IDENTITY,
        "driven_input": driven_input,
        "manifest_status": "complete",
        "manifest_sha256": _digest(300 + row_index),
        "plan_contract_sha256": plan_contract_sha,
        "plan_file_sha256": plan_file_sha,
        "physical_confirmation_verified": True,
        "physical_confirmation_token": physical_confirmation_token(driven_input),
        "fixture_identity": _fixture(row_index),
        "matrix_identity": _matrix_identity(),
        "verification_evidence": {
            "verification_kind": "local_manifest_plan_artifact_byte_verification",
            "manifest_file_sha256": _digest(300 + row_index),
            "plan_contract_sha256": plan_contract_sha,
            "plan_file_sha256": plan_file_sha,
            "condition_artifacts_reverified": True,
            "abi2_continuity_reaudited": True,
            "physical_confirmation_reverified": True,
        },
        "results": results,
    }


def test_profile_state_codes_require_exact_reviewed_mapping() -> None:
    antenna_states = [
        {"name": name, "gpio_code": code}
        for name, code in zip(ANTENNA_STATES, ANTENNA_GPIO_CODES, strict=True)
    ]

    states = validate_one_hot_state_codes(antenna_states, all_off_code=8)

    assert tuple(item["name"] for item in states) == ONE_HOT_STATE_ORDER
    assert tuple(item["gpio_code"] for item in states) == (8, *ANTENNA_GPIO_CODES)


@pytest.mark.parametrize(
    ("states", "all_off", "message"),
    [
        (
            [
                {"name": name, "gpio_code": code}
                for name, code in zip(
                    reversed(ANTENNA_STATES),
                    ANTENNA_GPIO_CODES,
                    strict=True,
                )
            ],
            8,
            "exact sequential order",
        ),
        (
            [
                {"name": name, "gpio_code": code}
                for name, code in zip(ANTENNA_STATES, range(8), strict=True)
            ],
            8,
            "reviewed exact mapping",
        ),
        (
            [
                {"name": name, "gpio_code": code}
                for name, code in zip(ANTENNA_STATES, ANTENNA_GPIO_CODES, strict=True)
            ],
            9,
            "exactly 8",
        ),
    ],
)
def test_rejects_noncanonical_state_maps(
    states: list[dict[str, Any]],
    all_off: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_one_hot_state_codes(states, all_off_code=all_off)


def test_one_row_reports_sweep_and_three_true_attribution_repeats() -> None:
    summary = _summarize(_row("ANT3"))

    assert summary.quality_passed
    assert summary.planned_condition_count == 36
    assert summary.attribution_repeat_count == 3
    intended = next(state for state in summary.states if state.selector_state_name == "ANT3")
    assert intended.cell_role == "intended_through"
    assert intended.condition_count == 4
    assert intended.attribution_repeat_count == 3
    assert intended.attribution_contrast_detected_count == 3
    assert intended.attribution_minimum_contrast_over_all_off_db == pytest.approx(20.0)
    assert intended.attribution_contrast_phase_coherence is not None
    assert intended.attribution_contrast_phase_coherence > 0.99


def test_common_all_off_leakage_cannot_satisfy_intended_path_gate() -> None:
    summary = _summarize(_row("ANT3", common_all_off_tone=True))

    assert not summary.quality_passed
    assert (
        "ANT3_intended_through_insufficient_attribution_repeats"
        in summary.quality_rejection_reasons
    )
    assert "ANT3_attribution_increment_confidence_includes_zero" in (
        summary.quality_rejection_reasons
    )
    intended = next(state for state in summary.states if state.selector_state_name == "ANT3")
    assert intended.detected_count == 4
    assert intended.attribution_contrast_detected_count == 0


def test_raw_detection_without_amplitude_or_phase_is_rejected() -> None:
    results = _row("ANT3")
    intended = next(
        result
        for result in results
        if result["selector_state_name"] == "ANT3"
        and result["tx_hardware_gain_db"] == -10.0
    )
    intended["rx2_over_rx1"]["phase_deg"] = None

    with pytest.raises(ValueError, match="lacks amplitude or phase"):
        _summarize(results)


def test_absent_tone_requires_consistent_finite_bound_and_measurement_flags() -> None:
    results = _row("ANT3")
    absent = next(
        result
        for result in results
        if result["selector_state_name"] == "ANT2"
        and result["tx_hardware_gain_db"] == -10.0
    )
    absent["rx2_over_rx1"]["amplitude_upper_bound_ratio"] = None
    absent["rx2_over_rx1"]["amplitude_upper_bound_db"] = None
    with pytest.raises(ValueError, match="absent tone lacks"):
        _summarize(results)

    inconsistent_bound = _row("ANT3")
    inconsistent_bound[0]["rx2_over_rx1"]["amplitude_upper_bound_db"] = -30.0
    with pytest.raises(ValueError, match="bound ratio and dB disagree"):
        _summarize(inconsistent_bound)

    below_phasor = _row("ANT3")
    below_phasor[0]["rx2_over_rx1"]["amplitude_upper_bound_ratio"] = 0.001
    below_phasor[0]["rx2_over_rx1"]["amplitude_upper_bound_db"] = -60.0
    with pytest.raises(ValueError, match="bound is below"):
        _summarize(below_phasor)

    inconsistent_quality = _row("ANT3")
    inconsistent_quality[0]["measurement_quality_rejection_reasons"] = ["rejected"]
    with pytest.raises(ValueError, match="flag and reasons disagree"):
        _summarize(inconsistent_quality)

    inconsistent_phasor = _row("ANT3")
    detected = next(
        result
        for result in inconsistent_phasor
        if result["selector_state_name"] == "ANT3"
    )
    detected["rx2_over_rx1"]["phase_deg"] += 1.0
    with pytest.raises(ValueError, match="phase contradicts"):
        _summarize(inconsistent_phasor)


def test_quality_failure_and_duplicate_or_missing_repeat_fail_closed() -> None:
    summary = _summarize(_row("ANT3", quality_failure=("ANT2", -20.0, 1)))
    assert not summary.quality_passed
    assert "ANT3_ANT2_condition_quality_rejected" in summary.quality_rejection_reasons

    complete = _row("ANT3")
    with pytest.raises(ValueError, match="duplicate"):
        _summarize([*complete, complete[0]])
    with pytest.raises(ValueError, match="incomplete"):
        _summarize(complete[:-1])


def test_public_aggregator_cannot_downgrade_to_one_attribution_repeat() -> None:
    with pytest.raises(ValueError, match="at least three"):
        summarize_one_hot_run(
            [
                {
                    **result,
                    "repeat_count_at_gain": 1,
                    "repeat_index": 0,
                }
                for result in _row("ANT3")
                if result["repeat_index"] == 0
            ],
            driven_input="ANT3",
            fixture_identity=_fixture(3),
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
            attribution_repeat_count=1,
            minimum_detected_attribution_repeats=1,
        )


def test_complete_matrix_requires_eight_independent_provenance_bound_rows() -> None:
    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]

    summary = summarize_complete_one_hot_matrix(
        [_seal_verified_one_hot_row_bundle(row) for row in rows],
        planned_gains_db=GAINS,
        attribution_gain_db=ATTRIBUTION_GAIN,
        attribution_repeat_count=ATTRIBUTION_REPEATS,
    )

    assert summary.quality_passed
    assert summary.independently_confirmed_row_count == 8
    assert summary.all_off_cell_count == 8
    assert summary.intended_through_cell_count == 8
    assert summary.wrong_state_cell_count == 56
    assert summary.planned_condition_count == 288
    assert summary.wrong_state_condition_count == 224


def test_matrix_rejects_bare_results_and_cross_row_artifact_reuse() -> None:
    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    with pytest.raises(ValueError, match="file-reading row loader"):
        summarize_complete_one_hot_matrix(
            rows,  # type: ignore[arg-type]
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )

    rows[1]["results"][0]["artifact_data_sha256"] = rows[0]["results"][0][
        "artifact_data_sha256"
    ]
    with pytest.raises(ValueError, match="reuses a raw artifact"):
        summarize_complete_one_hot_matrix(
            [_seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )


def test_post_load_document_mutation_cannot_change_sealed_row() -> None:
    rows = [
        _seal_verified_one_hot_row_bundle(_verified_bundle(driven_input, index))
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    mutable_copy = rows[0].document
    mutable_copy["results"][0]["rx2_over_rx1"]["phasor"]["real"] = 999.0

    summary = summarize_complete_one_hot_matrix(
        rows,
        planned_gains_db=GAINS,
        attribution_gain_db=ATTRIBUTION_GAIN,
    )

    assert summary.quality_passed


def test_matrix_rejects_changed_shared_feed_fixture_or_reused_setup_evidence() -> None:
    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    rows[1]["fixture_identity"]["shared_hardware"]["feed_cable_id"] = "different"
    for result in rows[1]["results"]:
        result["fixture_identity"] = rows[1]["fixture_identity"]
    with pytest.raises(ValueError, match="one characterized fixture"):
        summarize_complete_one_hot_matrix(
            [_seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )

    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    rows[1]["fixture_identity"]["setup_evidence"] = dict(
        rows[0]["fixture_identity"]["setup_evidence"]
    )
    for result in rows[1]["results"]:
        result["fixture_identity"] = rows[1]["fixture_identity"]
    with pytest.raises(ValueError, match="reuse setup evidence"):
        summarize_complete_one_hot_matrix(
            [_seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )


def test_matrix_rejects_different_board_or_target_image_identity() -> None:
    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    rows[1]["matrix_identity"]["board_id"] = "board-b"
    with pytest.raises(ValueError, match="DUT/control/acquisition"):
        summarize_complete_one_hot_matrix(
            [_seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )

    rows = [
        _verified_bundle(driven_input, index)
        for index, driven_input in enumerate(ANTENNA_STATES, start=1)
    ]
    rows[1]["matrix_identity"]["bench_bin_sha256"] = _digest(999)
    with pytest.raises(ValueError, match="DUT/control/acquisition"):
        summarize_complete_one_hot_matrix(
            [_seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=GAINS,
            attribution_gain_db=ATTRIBUTION_GAIN,
        )
