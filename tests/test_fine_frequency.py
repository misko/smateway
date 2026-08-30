from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from smateway.fine_frequency import (
    ANCHOR_CADENCE_NON_ANCHOR_VISITS,
    ANCHOR_FREQUENCY_HZ,
    BYTES_PER_CAPTURE,
    COARSE_MAXIMUM_HZ,
    COARSE_MINIMUM_HZ,
    COARSE_STEP_HZ,
    DDS_SCALE,
    DIRECTIONS,
    REPEATS_PER_VISIT,
    FineFrequencyError,
    analyze_sweep,
    build_coarse_schedule,
    build_fine_schedule,
    build_plan_contract,
    campaign_cross_binding_from_plan_contract,
    canonical_json_sha256,
    classify_center_frequency,
    direction_strata_test,
    plan_envelope,
    select_coarse_refinements,
    storage_contract,
    validate_live_condition_evidence,
    validate_plan_envelope,
    validate_schedule,
)


def _contract(*, mode: str = "coarse", centers: tuple[int, ...] = ()) -> dict[str, Any]:
    schedule = build_coarse_schedule() if mode == "coarse" else build_fine_schedule(centers)
    run_id = f"test-{mode}"
    fixture_source = {
        "path": "/tmp/fixture-v2.json",
        "sha256": "d" * 64,
        "size_bytes": 1,
    }
    setup_source = {
        "path": "/tmp/setup-v1.json",
        "sha256": "e" * 64,
        "size_bytes": 1,
    }
    shared_fixture = {"pluto": {"serial": "serial-a"}}
    stage_delta: dict[str, Any] = {}
    component_ids: list[str] = []
    connection_ids: list[str] = []
    fixture_identity = {
        "schema": 1,
        "identity_kind": "5g8_t7_fixture_v2_binding",
        "topology_stage": "direct_rx2_termination",
        "topology_token": "DIRECT_RX2_50OHM_AT_PLUTO",
        "selector_connected": False,
        "fixture_evidence_v2": {
            "schema": 2,
            "fixture_kind": "5g8_general_topology_stage_fixture",
            "campaign_id": "campaign-a",
            "comparable_fixture_group_id": "fixture-group-a",
            "stage": "direct_rx2_termination",
            "run_id": run_id,
            "board_id": "board-a",
            "source_files": {
                "fixture_manifest": fixture_source,
                "setup_attestation": setup_source,
            },
            "setup_attestation": {
                "schema": 1,
                "attestation_kind": "5g8_general_topology_run_setup",
                "attestation_id": "setup-a",
                "created_at": "2026-08-29T00:00:00+00:00",
                "created_at_wall_clock_freshness_enforced": False,
                "run_id": run_id,
                "campaign_id": "campaign-a",
                "comparable_fixture_group_id": "fixture-group-a",
                "stage": "direct_rx2_termination",
                "fixture_manifest_sha256": fixture_source["sha256"],
                "shared_fixture_sha256": canonical_json_sha256(shared_fixture),
                "stage_delta_sha256": canonical_json_sha256(stage_delta),
                "observed_component_ids": component_ids,
                "observed_connection_ids": connection_ids,
                "setup_attestation_file": setup_source,
                "selector_flash_evidence": None,
                "setup_evidence": {
                    "path": "/tmp/setup-evidence.txt",
                    "sha256": "9" * 64,
                    "size_bytes": 1,
                },
            },
            "selector_flash_evidence": None,
            "shared_fixture": shared_fixture,
            "shared_fixture_sha256": canonical_json_sha256(shared_fixture),
            "stage_delta": stage_delta,
            "stage_delta_sha256": canonical_json_sha256(stage_delta),
            "prior_stage_binding": None,
            "component_ids": component_ids,
            "connection_ids": connection_ids,
            "characterization_summary": {},
        },
        "selector_control": None,
    }
    source_identity = {"commit": "a" * 40, "tree_clean": True}
    native_identity = {"path": "/usr/local/lib/libiio.so.0.25", "sha256": "b" * 64}
    device_identity = {"serial": "serial-a", "uri": "usb:1.2.3"}
    if mode == "fine":
        coarse_fixture = json.loads(json.dumps(fixture_identity))
        coarse_fixture["fixture_evidence_v2"]["run_id"] = "test-coarse"
        coarse_fixture["fixture_evidence_v2"]["setup_attestation"]["run_id"] = "test-coarse"
        coarse_contract = build_plan_contract(
            run_id="test-coarse",
            board_id="board-a",
            schedule=build_coarse_schedule(),
            source_identity=source_identity,
            native_identity=native_identity,
            fixture_identity=coarse_fixture,
            device_identity={"serial": "serial-a", "uri": "usb:9.8.7"},
            free_bytes=100_000_000_000,
        )
        coarse_plan_sha256 = canonical_json_sha256(coarse_contract)
        campaign_binding = campaign_cross_binding_from_plan_contract(coarse_contract)
        selection = {
            "schema": 1,
            "selected_centers_hz": list(centers),
            "selection_kind": "multiplicity_corrected_local_extrema_v1",
            "coarse_plan_contract_sha256": coarse_plan_sha256,
        }
        coarse_binding = {
            "path": "/tmp/coarse-results.json",
            "sha256": "c" * 64,
            "size_bytes": 1,
            "coarse_plan_contract_sha256": coarse_plan_sha256,
            "campaign_binding": campaign_binding,
            "campaign_binding_sha256": canonical_json_sha256(campaign_binding),
        }
    else:
        selection = None
        coarse_binding = None
    return build_plan_contract(
        run_id=run_id,
        board_id="board-a",
        schedule=schedule,
        source_identity=source_identity,
        native_identity=native_identity,
        fixture_identity=fixture_identity,
        device_identity=device_identity,
        free_bytes=100_000_000_000,
        coarse_results_binding=coarse_binding,
        refinement_selection=selection,
    )


def _observations(
    contract: dict[str, Any],
    *,
    maxima: tuple[int, ...] = (),
    minimum: int | None = None,
    descending_scale: float = 1.0,
) -> list[dict[str, Any]]:
    output = []
    for condition in contract["schedule"]["conditions"]:
        magnitude = 1.0
        if condition["frequency_hz"] in maxima and condition["role"] == "primary":
            magnitude = 3.0
        if condition["frequency_hz"] == minimum and condition["role"] == "primary":
            magnitude = 0.2
        if condition["direction"] == "descending":
            magnitude *= descending_scale
        condition_id = condition["condition_id"]
        output.append(
            {
                "condition_id": condition_id,
                "accepted": True,
                "refinement_id": condition["refinement_id"],
                "direction": condition["direction"],
                "frequency_hz": condition["frequency_hz"],
                "role": condition["role"],
                "repeat_index": condition["repeat_index"],
                "stream_id": condition["plan_index"] + 1,
                "artifact_sha256": hashlib.sha256(condition_id.encode()).hexdigest(),
                "detected": True,
                "phasor": {"real": magnitude, "imag": 0.0},
                "magnitude": magnitude,
                "phase_deg": 0.0,
                "amplitude_upper_bound_ratio": None,
                "nondetection_is_phase_free": False,
            }
        )
    return output


def _live_evidence(condition: dict[str, Any], *, detected: bool = True) -> dict[str, Any]:
    stream_id = 42
    continuity = {
        "metadata_abi": 2,
        "stream_id": stream_id,
        "block_count": 3,
        "total_samples": 300_000,
        "first_buffer_sequence": 0,
    }
    analysis = {
        "schema": 1,
        "analysis_kind": "raw_ci16_coherent_rx2_over_rx1_v1",
        "pilot": {},
        "coherent_transfer": {},
        "quality_passed": True,
        "quality_rejection_reasons": [],
        "rx1_reference_tone_detected": True,
        "rx2_tone_detected": detected,
        "phasor": {"real": 1.0, "imag": 0.0} if detected else None,
        "magnitude": 1.0 if detected else None,
        "phase_deg": 0.0 if detected else None,
        "amplitude_upper_bound_ratio": None if detected else 0.01,
        "nondetection_is_phase_free": not detected,
    }
    return {
        "schema": 1,
        "evidence_kind": "5g8_fine_frequency_condition_v1",
        "condition_id": condition["condition_id"],
        "status": "passed",
        "device": {
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "usb_sysfs_path": "/sys/fake-usb",
            "radio_identity": {"serial": "serial-a", "uri": "usb:1.2.3"},
        },
        "rf_readback": {
            "rx_lo_hz": condition["frequency_hz"],
            "tx_lo_hz": condition["frequency_hz"],
            "lo_readback_provenance": ("pluto_plus_utils_continuous_exact_condition_preflight"),
            "sample_rate_hz": 1_000_000,
            "bandwidth_hz": 800_000,
            "tx1_gain_db": -20.0,
            "tx2_gain_db": -80.0,
            "tx2_gain_readback_provenance": (
                "pluto_plus_utils_capture_helper_internal_exact_readback"
            ),
            "dds_scale_readback": [DDS_SCALE, 0.0, DDS_SCALE, 0.0, 0.0, 0.0, 0.0, 0.0],
            "dds_enabled_readback": [True, False, True, False, False, False, False, False],
            "dds_frequency_readback_hz": [
                100_006.0,
                0.0,
                -100_006.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ],
        },
        "capture": {
            "stream_id": stream_id,
            "metadata_abi": 2,
            "first_buffer_sequence": 0,
            "sample_count": 300_000,
            "frame_count": 3,
            "kernel_buffers": 8,
            "continuity_passed": True,
            "headroom_passed": True,
            "clipped_sample_count": 0,
            "final_mute_passed": True,
            "live_ledger": continuity,
            "persisted_continuity": continuity,
        },
        "artifact": {
            "artifact_id": "artifact-a",
            "path": "/tmp/artifact-a",
            "data_path": "/tmp/artifact-a/artifact-a.sigmf-data",
            "data_sha256": "f" * 64,
            "data_size_bytes": BYTES_PER_CAPTURE,
            "metadata_path": "/tmp/artifact-a/artifact-a.sigmf-meta",
            "metadata_sha256": "a" * 64,
            "metadata_size_bytes": 100,
            "condition_record_path": "/tmp/artifact-a/5g8-fine-frequency-condition.json",
            "condition_record_sha256": "b" * 64,
            "condition_record_size_bytes": 100,
            "local_rpi_storage": True,
            "pluto_storage_used": False,
        },
        "analysis": analysis,
        "safety": {
            "initial_mute": {
                "status": "passed",
                "serial": "serial-a",
                "purpose": "pre_condition_exact_mute",
                "attestation": "mute_returned_radio_exact_serial_readback",
                "error": None,
            },
            "final_mute": {
                "status": "passed",
                "serial": "serial-a",
                "purpose": "final_condition_exact_mute",
                "attestation": "mute_returned_radio_exact_serial_readback",
                "error": None,
            },
            "persistence_began_only_after_final_mute_passed": True,
            "selector_all_off_passed_before_persistence": None,
        },
        "selector_static_all_off": None,
    }


def test_coarse_schedule_has_exact_endpoints_directions_repeats_and_anchor_cadence() -> None:
    schedule = build_coarse_schedule()
    assert len(schedule.primary_frequencies_by_refinement["coarse"]) == 36
    assert len(schedule.visits) == 86
    assert len(schedule.conditions) == 430
    assert schedule.conditions[0].frequency_hz == COARSE_MINIMUM_HZ
    assert schedule.conditions[-1].frequency_hz == ANCHOR_FREQUENCY_HZ
    for direction in DIRECTIONS:
        visits = [visit for visit in schedule.visits if visit.direction == direction]
        primary = [visit.frequency_hz for visit in visits if visit.role == "primary"]
        assert primary[0] == (COARSE_MINIMUM_HZ if direction == "ascending" else COARSE_MAXIMUM_HZ)
        assert primary[-1] == (COARSE_MAXIMUM_HZ if direction == "ascending" else COARSE_MINIMUM_HZ)
        anchors = [visit for visit in visits if visit.role == "interleaved_anchor"]
        assert len(anchors) == 7
        assert all(visit.frequency_hz == ANCHOR_FREQUENCY_HZ for visit in anchors)
        assert [visit.anchor_group_index for visit in anchors] == list(range(1, 8))
    grouped: dict[tuple[str, int], list[int]] = {}
    for condition in schedule.conditions:
        grouped.setdefault((condition.direction, condition.visit_index), []).append(
            condition.repeat_index
        )
    assert all(
        sorted(repeats) == list(range(1, REPEATS_PER_VISIT + 1)) for repeats in grouped.values()
    )


def test_fine_schedule_is_one_mhz_plus_minus_ten_with_four_anchors_per_direction() -> None:
    schedule = build_fine_schedule((5_750_000_000, ANCHOR_FREQUENCY_HZ))
    assert len(schedule.conditions) == 500
    for refinement_id, primary in schedule.primary_frequencies_by_refinement.items():
        center = int(refinement_id.removeprefix("fine-"))
        assert len(primary) == 21
        assert primary[0] == center - 10_000_000
        assert primary[-1] == center + 10_000_000
        assert all(
            right - left == 1_000_000 for left, right in zip(primary, primary[1:], strict=False)
        )
        for direction in DIRECTIONS:
            anchors = [
                visit
                for visit in schedule.visits
                if visit.refinement_id == refinement_id
                and visit.direction == direction
                and visit.role == "interleaved_anchor"
            ]
            assert len(anchors) == 4


def test_policy_rejects_out_of_range_off_grid_and_edge_refinement() -> None:
    with pytest.raises(FineFrequencyError, match="outside"):
        classify_center_frequency(5_951_000_000, grid_step_hz=1_000_000)
    with pytest.raises(FineFrequencyError, match="grid"):
        classify_center_frequency(5_800_500_000, grid_step_hz=1_000_000)
    with pytest.raises(FineFrequencyError, match="leaves"):
        build_fine_schedule((COARSE_MINIMUM_HZ,))
    with pytest.raises(FineFrequencyError, match="integer"):
        build_fine_schedule((5_800_000_000.5,))  # type: ignore[arg-type]
    with pytest.raises(FineFrequencyError, match="one or two"):
        build_fine_schedule((5_750_000_000, 5_800_000_000, 5_850_000_000))


def test_validator_rejects_consistently_relabelled_coarse_endpoints() -> None:
    schedule = build_coarse_schedule()
    truncated = schedule.primary_frequencies_by_refinement["coarse"][1:]
    with pytest.raises(FineFrequencyError, match="endpoints/grid"):
        validate_schedule(
            replace(
                schedule,
                primary_frequencies_by_refinement={"coarse": truncated},
            )
        )


def test_schedule_validator_rejects_duplicate_condition_and_missing_anchor() -> None:
    schedule = build_coarse_schedule()
    duplicated = replace(schedule, conditions=(schedule.conditions[0], *schedule.conditions))
    with pytest.raises(FineFrequencyError, match="duplicated"):
        validate_schedule(duplicated)
    removed_anchor = next(
        index for index, visit in enumerate(schedule.visits) if visit.role == "interleaved_anchor"
    )
    bad_visits = schedule.visits[:removed_anchor] + schedule.visits[removed_anchor + 1 :]
    with pytest.raises(FineFrequencyError, match="visit order/anchor cadence"):
        validate_schedule(replace(schedule, visits=bad_visits))


def test_exact_storage_and_two_times_free_space_gate() -> None:
    schedule = build_coarse_schedule()
    raw = len(schedule.conditions) * BYTES_PER_CAPTURE
    evidence = storage_contract(schedule, free_bytes=raw * 2)
    assert evidence["condition_count"] == 430
    assert evidence["estimated_raw_iq_bytes"] == 1_032_000_000
    assert evidence["required_free_bytes"] == 2_064_000_000
    assert evidence["capture_duration_s"] == 129.0
    assert evidence["minimum_settle_duration_s"] == 43.0
    assert evidence["minimum_rf_runtime_s"] == 172.0
    assert evidence["runtime_excludes_usb_and_filesystem_overhead"] is True
    with pytest.raises(FineFrequencyError, match="two-times"):
        storage_contract(schedule, free_bytes=raw * 2 - 1)


def test_immutable_fine_plan_binds_coarse_hash_and_selected_centers() -> None:
    contract = _contract(mode="fine", centers=(5_750_000_000, 5_900_000_000))
    envelope = plan_envelope(contract)
    assert validate_plan_envelope(envelope) == contract
    assert envelope["plan_contract_sha256"] == canonical_json_sha256(contract)
    coarse_campaign = contract["coarse_results_binding"]["campaign_binding"]
    assert coarse_campaign["device_uri_observation"] == "usb:9.8.7"
    assert contract["device_identity"]["uri"] == "usb:1.2.3"
    assert (
        coarse_campaign["device_uri_reobservation_policy"]
        == "fresh_usb_uri_allowed_only_for_same_serial"
    )
    changed = dict(envelope)
    changed["plan_contract_sha256"] = "0" * 64
    with pytest.raises(FineFrequencyError, match="hash"):
        validate_plan_envelope(changed)
    unsafe_contract = json.loads(json.dumps(contract))
    unsafe_contract["acquisition"]["sample_rate_hz"] = 2_000_000
    self_hashed_unsafe = {
        "schema": 1,
        "immutable": True,
        "plan_contract": unsafe_contract,
        "plan_contract_sha256": canonical_json_sha256(unsafe_contract),
    }
    with pytest.raises(FineFrequencyError, match="regenerated policy"):
        validate_plan_envelope(self_hashed_unsafe)


@pytest.mark.parametrize(
    "changed_identity",
    ("board", "topology", "fixture_reference", "source", "native", "device"),
)
def test_fine_plan_rejects_cross_campaign_coarse_identity(changed_identity: str) -> None:
    contract = _contract(mode="fine", centers=(5_800_000_000,))
    unsafe = json.loads(json.dumps(contract))
    coarse = unsafe["coarse_results_binding"]
    campaign = coarse["campaign_binding"]
    if changed_identity == "board":
        campaign["board_id"] = "board-b"
    elif changed_identity == "topology":
        campaign["topology_stage"] = "rx2_cable_terminated"
        campaign["topology_token"] = "RX2_CABLE_FAR_END_50OHM"
    elif changed_identity == "fixture_reference":
        reference = campaign["fixture_reference_planes"]
        reference["component_ids"].append("different-reference-plane")
        campaign["fixture_reference_planes_sha256"] = canonical_json_sha256(reference)
    elif changed_identity == "source":
        campaign["source_identity"]["commit"] = "f" * 40
        campaign["source_identity_sha256"] = canonical_json_sha256(campaign["source_identity"])
    elif changed_identity == "native":
        campaign["native_identity"]["sha256"] = "e" * 64
        campaign["native_identity_sha256"] = canonical_json_sha256(campaign["native_identity"])
    else:
        campaign["device_stable_identity"]["serial"] = "serial-b"
        campaign["device_stable_identity_sha256"] = canonical_json_sha256(
            campaign["device_stable_identity"]
        )
    coarse["campaign_binding_sha256"] = canonical_json_sha256(campaign)
    envelope = {
        "schema": 1,
        "immutable": True,
        "plan_contract": unsafe,
        "plan_contract_sha256": canonical_json_sha256(unsafe),
    }

    with pytest.raises(FineFrequencyError, match="differs from coarse"):
        validate_plan_envelope(envelope)


def test_deterministic_extrema_selection_uses_multiplicity_intervals_and_lower_tie() -> None:
    contract = _contract()
    observations = _observations(
        contract,
        maxima=(5_750_000_000, 5_850_000_000),
        minimum=5_900_000_000,
    )
    selection = select_coarse_refinements(contract, observations)
    assert selection["directions_pooled"] is True
    assert selection["selected_centers_hz"] == [5_750_000_000, 5_900_000_000]
    assert selection["selected"][0]["kind"] == "local_maximum"
    assert selection["selected"][1]["kind"] == "local_minimum"


def test_no_significant_extremum_falls_back_to_5p8_anchor() -> None:
    contract = _contract()
    selection = select_coarse_refinements(contract, _observations(contract))
    assert selection["selected_centers_hz"] == [ANCHOR_FREQUENCY_HZ]
    assert selection["selected"][0]["kind"] == "fallback_anchor"


def test_direction_test_prevents_pooling_when_strata_differ() -> None:
    contract = _contract()
    observations = _observations(contract, descending_scale=1.1)
    decision = direction_strata_test(contract, observations)
    assert decision["pooling_allowed"] is False
    analysis = analyze_sweep(contract, observations)
    assert analysis["pooling_performed"] is False
    assert analysis["pooled_rows"] == []
    assert len(analysis["anchor_drift"]["rows"]) == 14


def test_observation_duplicate_missing_and_reused_streams_are_rejected() -> None:
    contract = _contract(mode="fine", centers=(ANCHOR_FREQUENCY_HZ,))
    observations = _observations(contract)
    with pytest.raises(FineFrequencyError, match="incomplete"):
        analyze_sweep(contract, observations[:-1])
    duplicated = observations.copy()
    duplicated[-1] = duplicated[0]
    with pytest.raises(FineFrequencyError, match="duplicates"):
        analyze_sweep(contract, duplicated)
    reused = [dict(item) for item in observations]
    reused[1]["stream_id"] = reused[0]["stream_id"]
    with pytest.raises(FineFrequencyError, match="reuse"):
        analyze_sweep(contract, reused)


def test_exact_lo_dds_continuity_and_local_storage_evidence() -> None:
    contract = _contract(mode="fine", centers=(ANCHOR_FREQUENCY_HZ,))
    condition = contract["schedule"]["conditions"][0]
    evidence = _live_evidence(condition)
    assert (
        validate_live_condition_evidence(
            contract,
            evidence,
            prior_stream_ids=set(),
            prior_artifact_sha256s=set(),
        )["condition_id"]
        == condition["condition_id"]
    )
    wrong_lo = {**evidence, "rf_readback": {**evidence["rf_readback"], "rx_lo_hz": 1}}
    with pytest.raises(FineFrequencyError, match="LO"):
        validate_live_condition_evidence(
            contract,
            wrong_lo,
            prior_stream_ids=set(),
            prior_artifact_sha256s=set(),
        )
    inferred_tx2 = {
        **evidence,
        "rf_readback": {
            **evidence["rf_readback"],
            "tx2_gain_readback_provenance": "inferred_from_requested_value",
        },
    }
    with pytest.raises(FineFrequencyError, match="TX2 mute readback provenance"):
        validate_live_condition_evidence(
            contract,
            inferred_tx2,
            prior_stream_ids=set(),
            prior_artifact_sha256s=set(),
        )


def test_quality_passed_nondetection_preserves_only_phase_free_bound() -> None:
    contract = _contract(mode="fine", centers=(ANCHOR_FREQUENCY_HZ,))
    condition = contract["schedule"]["conditions"][0]
    evidence = _live_evidence(condition, detected=False)
    admitted = validate_live_condition_evidence(
        contract,
        evidence,
        prior_stream_ids=set(),
        prior_artifact_sha256s=set(),
    )
    assert admitted["analysis"]["phasor"] is None
    assert admitted["analysis"]["amplitude_upper_bound_ratio"] == 0.01


def test_equal_magnitude_opposite_direction_phase_is_never_pooled() -> None:
    contract = _contract()
    observations = _observations(contract)
    for item in observations:
        if item["direction"] == "descending":
            item["phasor"] = {"real": -float(item["magnitude"]), "imag": 0.0}
            item["phase_deg"] = -180.0
    decision = direction_strata_test(contract, observations)
    assert decision["pooling_allowed"] is False
    assert all(row["amplitude_equivalence_passed"] for row in decision["rows"])
    assert all(not row["phase_equivalence_passed"] for row in decision["rows"])


def test_anchor_constant_documents_frozen_cadence() -> None:
    assert ANCHOR_CADENCE_NON_ANCHOR_VISITS == 5
    assert COARSE_STEP_HZ == 10_000_000
