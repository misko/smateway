from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest

from smateway.arm_preserving_d2 import (
    ARMS,
    OBSERVATION_KIND,
    SETUP_KIND,
    TOPOLOGY_AUTHORITY,
    TOPOLOGY_LIMITATION_CODE,
    ArmPreservingD2Error,
    assemble_closure_campaign,
    build_c_d2_fragment,
    build_fixture_v2,
    canonical_sha256,
    cohort_document,
    cohort_from_document,
    expected_setup_inventory,
    fragment_document,
    validate_fixture_v2,
    validate_observation,
    validate_setup_attestation,
)
from smateway.closure_qualification import (
    ClosureCohort,
    ClosureRepeat,
    ComplexDetection,
    leaf_source_set_sha256,
    qualify_closure,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _termination(label: str) -> dict[str, Any]:
    return {
        "termination_id": f"load-{label}",
        "identity_sha256": _hash(f"load-identity-{label}"),
        "impedance_ohm": 50.0,
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_dbm": 20.0,
    }


def _fixture_document() -> dict[str, Any]:
    components = {
        role: {"component_id": f"component-{role}", "identity_sha256": _hash(role)}
        for role in (
            "pluto",
            "two_way_splitter",
            "eight_way_splitter",
            "selector",
            "rx1_attenuation_chain",
            "tx2_termination",
        )
    }
    connections = {
        role: f"connection-{role}"
        for role in (
            "tx1_to_two_way",
            "two_way_reference_to_rx1_attenuation",
            "rx1_attenuation_to_rx1",
            "selector_common_to_rx2",
            "tx2_to_termination",
        )
    }
    reference_planes: dict[str, Any] = {
        role: f"plane-{role}"
        for role in (
            "tx1_source",
            "two_way_stimulus_output",
            "eight_way_input",
            "rx1_protected_input",
            "selector_common",
            "rx2_input",
        )
    }
    reference_planes["arms"] = {
        arm: {
            "splitter_output": f"plane-{arm}-splitter-output",
            "selector_input": f"plane-{arm}-selector-input",
        }
        for arm in ARMS
    }
    arm_paths = {
        arm: {
            "path_id": f"e-path-{arm}",
            "identity_sha256": _hash(f"e-path-{arm}"),
            "splitter_output_port": f"F{index}",
            "selector_input_port": arm,
            "splitter_output_reference_plane": f"plane-{arm}-splitter-output",
            "selector_input_reference_plane": f"plane-{arm}-selector-input",
        }
        for index, arm in enumerate(ARMS, 1)
    }
    return build_fixture_v2(
        campaign_id="arm-d2-campaign",
        board_id="board-a",
        pluto_serial="pluto-a",
        source_commit="a" * 40,
        components=components,
        fixed_connection_ids=connections,
        reference_planes=reference_planes,
        arm_paths=arm_paths,
        splitter_output_terminations={arm: _termination(f"splitter-{arm}") for arm in ARMS},
        selector_input_terminations={arm: _termination(f"selector-{arm}") for arm in ARMS},
        selector_flash_attestation={
            "file": {
                "path": "/home/pi/evidence/selector-flash.json",
                "sha256": _hash("selector-flash"),
                "size_bytes": 123,
            },
            "campaign_id": "arm-d2-campaign",
            "run_id": "selector-flash-run",
            "board_id": "board-a",
            "image_role": "bench",
        },
        linearity_evidence_sha256s={arm: _hash(f"linearity-{arm}") for arm in ARMS},
        rf_safety={
            "source_peak_output_bound_dbm": 7.0,
            "receiver_input_limit_dbm": 0.0,
            "minimum_path_attenuation_before_rx1_db": 20.0,
            "required_margin_db": 10.0,
        },
    )


def _setup(
    fixture_document: dict[str, Any], *, role: str, arm: str, repeat_index: int, run_id: str
) -> dict[str, Any]:
    fixture = validate_fixture_v2(fixture_document)
    return {
        "schema": 1,
        "attestation_kind": SETUP_KIND,
        "attestation_id": f"setup-{run_id}",
        "created_at": "2026-08-29T20:00:00+00:00",
        "operator_id": "operator-a",
        "campaign_id": fixture.campaign_id,
        "run_id": run_id,
        "role": role,
        "arm": arm,
        "repeat_index": repeat_index,
        "fixture_file_sha256": _hash("fixture-file"),
        "observed_inventory": expected_setup_inventory(fixture, role=role, arm=arm),
        "setup_evidence": {
            "path": f"/home/pi/evidence/setup-{run_id}.png",
            "sha256": _hash(f"setup-evidence-{run_id}"),
            "size_bytes": 456,
        },
        "confirmations": {
            "no_antennas": True,
            "tx1_drives_two_way_then_eight_way_input": True,
            "two_way_reference_feeds_protected_rx1": True,
            "selector_common_feeds_rx2": True,
            "tx2_physically_terminated_and_digitally_muted": True,
            "static_bench_image_live": True,
            "selector_lease_free_all_off": True,
            "exact_connected_paths_only": True,
            "every_listed_load_is_independent_and_5g8_rated": True,
            "reference_planes_match_fixture_v2": True,
            "no_unlisted_connection_or_movement": True,
            "topology_limitation_understood": True,
        },
    }


def _observation(
    fixture_document: dict[str, Any], *, role: str, arm: str, repeat_index: int
) -> dict[str, Any]:
    fixture = validate_fixture_v2(fixture_document)
    run_id = f"run-{role}-{arm}-{repeat_index}"
    raw_sha = _hash(f"raw-{run_id}")
    artifact_base = {
        "artifact_id": f"artifact-{run_id}",
        "path": f"/home/pi/captures/{run_id}",
        "raw_iq_path": f"/home/pi/captures/{run_id}/capture.sigmf-data",
        "raw_iq_sha256": raw_sha,
        "metadata_path": f"/home/pi/captures/{run_id}/capture.sigmf-meta",
        "metadata_sha256": _hash(f"metadata-{run_id}"),
    }
    artifact = {
        **artifact_base,
        "artifact_sha256": canonical_sha256(artifact_base),
    }
    phasor = 0.1 + (0.01 if role == "d2_i" else 0.0)
    return {
        "schema": 1,
        "observation_kind": OBSERVATION_KIND,
        "campaign_id": fixture.campaign_id,
        "board_id": fixture.board_id,
        "pluto_serial": fixture.pluto_serial,
        "role": role,
        "arm": arm,
        "repeat_index": repeat_index,
        "run_id": run_id,
        "condition_id": (f"{fixture.campaign_id}.{role}.{arm}.repeat-{repeat_index}.{run_id}"),
        "fixture_file": {
            "path": "/home/pi/evidence/fixture.json",
            "sha256": _hash("fixture-file"),
            "size_bytes": 1000,
        },
        "fixture_sha256": fixture.fixture_sha256,
        "setup_attestation_file": {
            "path": f"/home/pi/evidence/setup-{run_id}.json",
            "sha256": _hash(f"setup-file-{run_id}"),
            "size_bytes": 200,
        },
        "selector_flash_attestation_file": fixture.selector_flash_attestation["file"],
        "closure_plan_sha256": fixture.plan_identity.sha256,
        "topology_sha256": fixture.topology(arm, role).sha256,
        "fixture_graph_sha256": fixture.fixture_graph_identity.sha256,
        "reference_plane_sha256": fixture.reference_plane_identity.sha256,
        "source": {
            "smateway_commit": fixture.source_commit,
            "smateway_files_sha256": _hash("smateway-files"),
            "dependency_commit": "b" * 40,
            "dependency_files_sha256": _hash("dependency-files"),
            "native_libiio_attestation_sha256": _hash("native-libiio"),
        },
        "capture": {
            "stream_id": f"stream-{run_id}",
            "metadata_abi": 2,
            "center_frequency_hz": 5_800_000_000,
            "sample_rate_hz": 1_000_000,
            "bandwidth_hz": 800_000,
            "tone_offset_hz": 100_000,
            "samples_per_frame": 100_000,
            "frame_count": 3,
            "sample_count": 300_000,
            "kernel_buffers": 8,
            "receiver_gain_db": 40.0,
            "tx_hardware_gain_db": -20.0,
            "dds_scale": 0.125,
            "continuity_passed": True,
            "rf_readback_passed": True,
        },
        "artifact": artifact,
        "condition_record_sha256": _hash(f"record-{run_id}"),
        "leaf_source_sha256s": [raw_sha],
        "leaf_source_set_sha256": leaf_source_set_sha256((raw_sha,)),
        "transfer": {
            "detected": True,
            "phasor": {"real": phasor, "imag": 0.0},
            "magnitude_upper_bound": None,
        },
        "quality": {
            "passed": True,
            "rejection_reasons": [],
            "reference_tone_snr_db": 40.0,
            "adc_headroom_passed": True,
            "clipped_sample_count_by_receiver": [0, 0],
        },
        "safety": {
            "initial_exact_mute_passed": True,
            "selector_all_off_before_passed": True,
            "selector_all_off_after_passed": True,
            "selector_all_off_cleanup_passed": True,
            "final_exact_mute_passed": True,
            "persistence_after_final_mute_only": True,
            "automatic_retry_count": 0,
            "accepted_from_quarantine": False,
        },
        "topology_limitation": fixture.document["topology_limitation"],
    }


def _generic_cohort(
    fixture_document: dict[str, Any], *, role: str, arm: str | None, value: complex
) -> ClosureCohort:
    fixture = validate_fixture_v2(fixture_document)
    topology = fixture.topology_identities[role if arm is None else f"{arm}.{role}"]
    repeats = []
    for index in range(1, 6):
        label = f"generic-{role}-{arm}-{index}"
        leaf = _hash(f"leaf-{label}")
        repeats.append(
            ClosureRepeat(
                repeat_index=index,
                run_id=f"run-{label}",
                condition_id=f"condition-{label}",
                stream_id=f"stream-{label}",
                artifact_sha256=_hash(f"artifact-{label}"),
                raw_iq_sha256=_hash(f"raw-{label}"),
                metadata_sha256=_hash(f"metadata-{label}"),
                condition_record_sha256=_hash(f"record-{label}"),
                leaf_source_sha256s=(leaf,),
                leaf_source_set_sha256=leaf_source_set_sha256((leaf,)),
                plan_sha256=fixture.plan_identity.sha256,
                topology_sha256=topology.sha256,
                source_commit=fixture.source_commit,
                quality_passed=True,
                value=ComplexDetection(True, value, None),
            )
        )
    return ClosureCohort(
        role=role,
        arm=arm,
        plan_sha256=fixture.plan_identity.sha256,
        source_commit=fixture.source_commit,
        topology_identity=topology,
        repeats=tuple(repeats),
    )


def test_fixture_derives_exact_topologies_and_explicit_diagnostic_limit() -> None:
    document = _fixture_document()
    fixture = validate_fixture_v2(document)
    assert fixture.plan_identity.payload()["splitter_multiport_characterized"] is False
    assert document["topology_limitation"] == {
        "code": TOPOLOGY_LIMITATION_CODE,
        "reason": (
            "Single-arm terminated-port captures do not characterize simultaneous 8-way "
            "splitter multiport interaction; arm-preserving closure is diagnostic only."
        ),
        "closure_authority": TOPOLOGY_AUTHORITY,
        "diagnostic_only": True,
        "closure_claim_permitted": False,
    }
    d2 = fixture.topology("ANT4", "d2_i").payload()["topology_details"]
    assert d2["exact_connected_splitter_output"] == "F4"
    assert d2["other_splitter_outputs_terminated"] == 7
    assert d2["other_selector_inputs_terminated"] == 7
    assert len(d2["other_splitter_output_termination_ids"]) == 7
    assert len(d2["other_selector_input_termination_ids"]) == 7


def test_fixture_rejects_reused_load_identity_and_mutated_derived_topology() -> None:
    document = _fixture_document()
    reused = deepcopy(document)
    reused["selector_input_terminations"]["ANT8"] = deepcopy(
        reused["splitter_output_terminations"]["ANT1"]
    )
    with pytest.raises(ArmPreservingD2Error, match="16 independent"):
        validate_fixture_v2(reused)

    mutated = deepcopy(document)
    mutated["topology_identities"]["arms"]["ANT2"]["d2_i"]["sha256"] = "f" * 64
    with pytest.raises(ArmPreservingD2Error, match="derived identities"):
        validate_fixture_v2(mutated)


def test_setup_attestation_requires_exact_c_and_d2_inventories() -> None:
    document = _fixture_document()
    fixture = validate_fixture_v2(document)
    c_setup = _setup(document, role="c_i", arm="ANT1", repeat_index=1, run_id="c-run")
    validated = validate_setup_attestation(
        c_setup,
        fixture=fixture,
        fixture_file_sha256=_hash("fixture-file"),
        run_id="c-run",
        role="c_i",
        arm="ANT1",
        repeat_index=1,
    )
    assert len(validated["observed_inventory"]["splitter_output_termination_ids"]) == 8
    d2_setup = _setup(document, role="d2_i", arm="ANT3", repeat_index=2, run_id="d2-run")
    assert len(d2_setup["observed_inventory"]["selector_input_termination_ids"]) == 7
    d2_setup["observed_inventory"]["connected_e_arm_path_ids"] = ["e-path-ANT4"]
    with pytest.raises(ArmPreservingD2Error, match="inventory"):
        validate_setup_attestation(
            d2_setup,
            fixture=fixture,
            fixture_file_sha256=_hash("fixture-file"),
            run_id="d2-run",
            role="d2_i",
            arm="ANT3",
            repeat_index=2,
        )


def test_observation_fails_closed_on_safety_and_never_synthesizes_phase() -> None:
    document = _fixture_document()
    fixture = validate_fixture_v2(document)
    observation = _observation(document, role="d2_i", arm="ANT1", repeat_index=1)
    assert validate_observation(observation, fixture=fixture).value.detected is True

    unsafe = deepcopy(observation)
    unsafe["safety"]["final_exact_mute_passed"] = False
    with pytest.raises(ArmPreservingD2Error, match="safety admission"):
        validate_observation(unsafe, fixture=fixture)

    nondetection = deepcopy(observation)
    nondetection["transfer"] = {
        "detected": False,
        "phasor": {"real": 0.0, "imag": 0.0},
        "magnitude_upper_bound": 0.01,
    }
    with pytest.raises(ArmPreservingD2Error, match="must not synthesize phase"):
        validate_observation(nondetection, fixture=fixture)


def test_exact_80_capture_fragment_rejects_missing_or_reused_sources() -> None:
    document = _fixture_document()
    fixture = validate_fixture_v2(document)
    raw = [
        _observation(document, role=role, arm=arm, repeat_index=index)
        for arm in ARMS
        for role in ("c_i", "d2_i")
        for index in range(1, 6)
    ]
    observations = [validate_observation(item, fixture=fixture) for item in raw]
    fragment = build_c_d2_fragment(observations, fixture=fixture)
    serialized = fragment_document(fragment)
    assert serialized["accepted_observation_count"] == 80
    assert serialized["topology_limitation"]["closure_claim_permitted"] is False
    assert tuple(serialized["arms"]) == ARMS

    with pytest.raises(ArmPreservingD2Error, match="exactly 80"):
        build_c_d2_fragment(observations[:-1], fixture=fixture)

    reused = deepcopy(raw)
    reused[1]["artifact"]["raw_iq_sha256"] = reused[0]["artifact"]["raw_iq_sha256"]
    reused_descriptor = {
        name: value for name, value in reused[1]["artifact"].items() if name != "artifact_sha256"
    }
    reused[1]["artifact"]["artifact_sha256"] = canonical_sha256(reused_descriptor)
    reused[1]["leaf_source_sha256s"] = reused[0]["leaf_source_sha256s"]
    reused[1]["leaf_source_set_sha256"] = reused[0]["leaf_source_set_sha256"]
    with pytest.raises(ArmPreservingD2Error, match="reused raw_iq"):
        build_c_d2_fragment(
            [validate_observation(item, fixture=fixture) for item in reused],
            fixture=fixture,
        )


def test_serialized_cohort_round_trips_without_losing_identity() -> None:
    document = _fixture_document()
    cohort = _generic_cohort(document, role="global_h_c", arm=None, value=0.1 + 0j)
    loaded = cohort_from_document(cohort_document(cohort))
    assert loaded == cohort


def test_fragment_invokes_shared_closure_model_and_stays_diagnostic_only() -> None:
    document = _fixture_document()
    fixture = validate_fixture_v2(document)
    observations = [
        validate_observation(
            _observation(document, role=role, arm=arm, repeat_index=index),
            fixture=fixture,
        )
        for arm in ARMS
        for role in ("c_i", "d2_i")
        for index in range(1, 6)
    ]
    fragment = build_c_d2_fragment(observations, fixture=fixture)
    campaign = assemble_closure_campaign(
        fixture=fixture,
        fragment=fragment,
        global_h_c=_generic_cohort(document, role="global_h_c", arm=None, value=0.1 + 0j),
        observed_e=_generic_cohort(document, role="observed_e", arm=None, value=0.18 + 0j),
        d1_by_arm={
            arm: _generic_cohort(document, role="d1_i", arm=arm, value=0.11 + 0j) for arm in ARMS
        },
    )
    result = qualify_closure(campaign, bootstrap_draw_count=256, bootstrap_seed=1)
    assert result.status == "evaluated"
    assert result.quality is not None and result.quality.full_complex_equivalent is True
    assert result.d2_validation_quality is not None
    assert result.d2_validation_quality.full_complex_equivalent is True
    assert result.closure_authority == TOPOLOGY_AUTHORITY
    assert result.closure_claim_supported is False
