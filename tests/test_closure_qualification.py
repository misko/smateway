from __future__ import annotations

import hashlib
from dataclasses import replace
from math import cos, radians, sin

import pytest

from smateway.closure_qualification import (
    ARMS,
    PLAN_SCHEMA,
    TOPOLOGY_SCHEMA,
    ArmClosureEvidence,
    ClosureCampaignEvidence,
    ClosureCohort,
    ClosureQualificationError,
    ClosureRepeat,
    ComplexDetection,
    JointWeightVectorCohort,
    JointWeightVectorRepeat,
    leaf_source_set_sha256,
    make_canonical_identity,
    qualify_closure,
)

SOURCE_COMMIT = "a" * 40


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _phasor(magnitude: float, phase_deg: float) -> complex:
    angle = radians(phase_deg)
    return magnitude * complex(cos(angle), sin(angle))


def _detected(value: complex) -> ComplexDetection:
    return ComplexDetection(True, value, None)


def _topology(
    method: str,
    role: str,
    arm: str | None,
    fixture_hash: str,
    reference_hash: str,
    details: dict[str, object],
):
    configurations = {
        "global_h_c": "all_selector_inputs_terminated_global",
        "observed_e": "simultaneous_8way_feed",
        "c_i": "all_selector_inputs_terminated_dedicated",
        "d1_i": "direct_one_hot",
        "d2_i": (
            "arm_preserving_exact_e_arm" if method == "arm_preserving" else "weighted_input_arm"
        ),
        "joint_weights": "joint_board_input_weight_measurement",
    }
    return make_canonical_identity(
        {
            "schema": TOPOLOGY_SCHEMA,
            "campaign_id": "campaign-5g8-closure",
            "method": method,
            "role": role,
            "arm": arm,
            "fixture_graph_sha256": fixture_hash,
            "reference_plane_sha256": reference_hash,
            "source_configuration": configurations[role],
            "topology_details": details,
            "upstream_sha256s": {"fixture_manifest": _hash("fixture-manifest")},
        }
    )


class _SourceFactory:
    def __init__(self, plan_sha256: str, source_commit: str) -> None:
        self.plan_sha256 = plan_sha256
        self.source_commit = source_commit

    def cohort(
        self,
        *,
        label: str,
        role: str,
        arm: str | None,
        topology,
        values: list[complex],
    ) -> ClosureCohort:
        repeats = tuple(
            ClosureRepeat(
                repeat_index=index,
                run_id=f"run-{label}-{index}",
                condition_id=f"condition-{label}-{index}",
                stream_id=f"stream-{label}-{index}",
                artifact_sha256=_hash(f"artifact-{label}-{index}"),
                raw_iq_sha256=_hash(f"raw-{label}-{index}"),
                metadata_sha256=_hash(f"metadata-{label}-{index}"),
                condition_record_sha256=_hash(f"condition-record-{label}-{index}"),
                leaf_source_sha256s=(_hash(f"leaf-{label}-{index}"),),
                leaf_source_set_sha256=leaf_source_set_sha256((_hash(f"leaf-{label}-{index}"),)),
                plan_sha256=self.plan_sha256,
                topology_sha256=topology.sha256,
                source_commit=self.source_commit,
                quality_passed=True,
                value=_detected(value),
            )
            for index, value in enumerate(values, start=1)
        )
        return ClosureCohort(
            role=role,
            arm=arm,
            plan_sha256=self.plan_sha256,
            source_commit=self.source_commit,
            topology_identity=topology,
            repeats=repeats,
        )

    def weights(
        self,
        *,
        topology,
        vectors: list[list[complex]],
    ) -> JointWeightVectorCohort:
        repeats = tuple(
            JointWeightVectorRepeat(
                repeat_index=index,
                run_id=f"run-weights-{index}",
                condition_id=f"condition-weights-{index}",
                stream_id=f"stream-weights-{index}",
                artifact_sha256=_hash(f"artifact-weights-{index}"),
                metadata_sha256=_hash(f"metadata-weights-{index}"),
                condition_record_sha256=_hash(f"condition-record-weights-{index}"),
                leaf_source_sha256s=(_hash(f"leaf-weights-{index}"),),
                leaf_source_set_sha256=leaf_source_set_sha256((_hash(f"leaf-weights-{index}"),)),
                plan_sha256=self.plan_sha256,
                topology_sha256=topology.sha256,
                source_commit=self.source_commit,
                quality_passed=True,
                weights=tuple(_detected(value) for value in vector),
            )
            for index, vector in enumerate(vectors, start=1)
        )
        return JointWeightVectorCohort(
            role="joint_weights",
            plan_sha256=self.plan_sha256,
            source_commit=self.source_commit,
            topology_identity=topology,
            repeats=repeats,
        )


def _build_campaign(
    method: str,
    *,
    characterized: bool = True,
    weight_variation: float = 0.0001,
) -> ClosureCampaignEvidence:
    fixture_hash = _hash("fixture-graph")
    reference_hash = _hash("board-input-reference-plane")
    arm_cable_hashes = {arm: _hash(f"E-{arm}-arm-and-cable") for arm in ARMS}
    global_topology = _topology(
        method,
        "global_h_c",
        None,
        fixture_hash,
        reference_hash,
        {"all_input_load_set_sha256": _hash("global-load-set")},
    )
    e_topology = _topology(
        method,
        "observed_e",
        None,
        fixture_hash,
        reference_hash,
        {"arm_cable_sha256s": arm_cable_hashes},
    )
    arm_topologies = {}
    for arm in ARMS:
        c_topology = _topology(
            method,
            "c_i",
            arm,
            fixture_hash,
            reference_hash,
            {
                "all_selector_inputs_terminated": True,
                "valid_comparator_roles": ["d1_i", "d2_i"],
            },
        )
        d1_topology = _topology(
            method,
            "d1_i",
            arm,
            fixture_hash,
            reference_hash,
            {
                "reference_c_i_topology_sha256": c_topology.sha256,
                "board_input_reference_plane_sha256": reference_hash,
                "linearity_evidence_sha256": _hash(f"{arm}-d1-linearity"),
            },
        )
        d2_topology = _topology(
            method,
            "d2_i",
            arm,
            fixture_hash,
            reference_hash,
            {
                "reference_c_i_topology_sha256": c_topology.sha256,
                "board_input_reference_plane_sha256": reference_hash,
                "e_topology_sha256": e_topology.sha256,
                "e_arm_cable_sha256": arm_cable_hashes[arm],
                "other_splitter_outputs_terminated": 7,
                "other_selector_inputs_terminated": 7,
            },
        )
        arm_topologies[arm] = {
            "c_i": c_topology,
            "d1_i": d1_topology,
            "d2_i": d2_topology,
        }
    weight_topology = (
        _topology(
            method,
            "joint_weights",
            None,
            fixture_hash,
            reference_hash,
            {
                "vector_arms": list(ARMS),
                "weight_definition": (
                    "e_excitation_over_d1_excitation_at_same_board_input_reference_plane"
                ),
                "board_input_reference_plane_sha256": reference_hash,
                "e_topology_sha256": e_topology.sha256,
                "d1_topology_sha256s": {arm: arm_topologies[arm]["d1_i"].sha256 for arm in ARMS},
                "common_phase_reference_sha256": _hash("weight-common-phase-reference"),
            },
        )
        if method == "weighted"
        else None
    )
    plan_identity = make_canonical_identity(
        {
            "schema": PLAN_SCHEMA,
            "campaign_id": "campaign-5g8-closure",
            "method": method,
            "source_commit": SOURCE_COMMIT,
            "fixture_graph_sha256": fixture_hash,
            "reference_plane_sha256": reference_hash,
            "splitter_multiport_characterized": characterized,
            "splitter_multiport_characterization_sha256": (
                _hash("splitter-multiport-characterization") if characterized else None
            ),
            "e_arm_cable_sha256s": arm_cable_hashes,
            "topology_sha256s": {
                "global_h_c": global_topology.sha256,
                "observed_e": e_topology.sha256,
                "arms": {
                    arm: {
                        role: arm_topologies[arm][role].sha256 for role in ("c_i", "d1_i", "d2_i")
                    }
                    for arm in ARMS
                },
                "joint_weights": None if weight_topology is None else weight_topology.sha256,
            },
        }
    )
    source = _SourceFactory(plan_identity.sha256, SOURCE_COMMIT)
    h_c = 0.8 + 0.15j
    c_values = [0.35 + 0.08j + index * (0.002 - 0.001j) for index in range(len(ARMS))]
    d1_increments = [
        _phasor(0.025 + index * 0.0015, -70.0 + index * 19.0) for index in range(len(ARMS))
    ]
    weight_centers = [
        _phasor(0.72 + index * 0.015, -25.0 + index * 8.0) for index in range(len(ARMS))
    ]
    common_offsets = (-2.0, -1.0, 0.0, 1.0, 2.0)
    weight_vectors = [
        [center * (1.0 + offset * weight_variation) for center in weight_centers]
        for offset in common_offsets
    ]
    if method == "weighted":
        primary_increments = [
            weight * increment
            for weight, increment in zip(weight_centers, d1_increments, strict=True)
        ]
    else:
        primary_increments = [increment * (0.91 + 0.03j) for increment in d1_increments]
    e_value = h_c + sum(primary_increments, 0.0 + 0.0j)
    arms = []
    for index, arm in enumerate(ARMS):
        c_value = c_values[index]
        d1_value = c_value + d1_increments[index]
        d2_value = c_value + primary_increments[index]
        arms.append(
            ArmClosureEvidence(
                arm=arm,
                c_i=source.cohort(
                    label=f"{arm}-c",
                    role="c_i",
                    arm=arm,
                    topology=arm_topologies[arm]["c_i"],
                    values=[c_value] * 5,
                ),
                d1_i=source.cohort(
                    label=f"{arm}-d1",
                    role="d1_i",
                    arm=arm,
                    topology=arm_topologies[arm]["d1_i"],
                    values=[d1_value] * 5,
                ),
                d2_i=source.cohort(
                    label=f"{arm}-d2",
                    role="d2_i",
                    arm=arm,
                    topology=arm_topologies[arm]["d2_i"],
                    values=[d2_value] * 5,
                ),
            )
        )
    return ClosureCampaignEvidence(
        plan_identity=plan_identity,
        global_h_c=source.cohort(
            label="global-c",
            role="global_h_c",
            arm=None,
            topology=global_topology,
            values=[h_c] * 5,
        ),
        observed_e=source.cohort(
            label="observed-e",
            role="observed_e",
            arm=None,
            topology=e_topology,
            values=[e_value] * 5,
        ),
        arms=tuple(arms),
        joint_weights=(
            None
            if weight_topology is None
            else source.weights(topology=weight_topology, vectors=weight_vectors)
        ),
    )


def _replace_values(cohort: ClosureCohort, values: list[complex]) -> ClosureCohort:
    return replace(
        cohort,
        repeats=tuple(
            replace(repeat, value=_detected(value))
            for repeat, value in zip(cohort.repeats, values, strict=True)
        ),
    )


def test_arm_preserving_exact_closure_is_diagnostic_without_multiport_characterization() -> None:
    result = qualify_closure(
        _build_campaign("arm_preserving", characterized=False), bootstrap_draw_count=512
    )

    assert result.status == "evaluated"
    assert result.quality is not None and result.quality.full_complex_equivalent
    assert result.closure_authority == "diagnostic_only_uncharacterized_splitter_multiport"
    assert result.splitter_multiport_characterization_sha256 is None
    assert not result.closure_claim_supported
    assert result.weight_vector_covariance_real_imag is None
    assert len(result.arm_diagnostics) == 8
    assert len(result.source_uncertainties) == 26


def test_characterized_arm_preserving_exact_closure_supports_claim() -> None:
    result = qualify_closure(_build_campaign("arm_preserving"), bootstrap_draw_count=512)

    assert result.closure_authority == "closure_qualified"
    assert result.splitter_multiport_characterization_sha256 == _hash(
        "splitter-multiport-characterization"
    )
    assert result.closure_claim_supported
    assert result.d2_validation_quality is not None
    assert result.d2_validation_quality.full_complex_equivalent


def test_weighted_closure_preserves_joint_cross_arm_weight_covariance() -> None:
    result = qualify_closure(
        _build_campaign("weighted", weight_variation=0.01),
        bootstrap_draw_count=4096,
        bootstrap_seed=123,
    )

    assert result.status == "evaluated"
    assert result.joint_weight_covariance_preserved
    assert result.weight_vector_covariance_real_imag is not None
    # ANT1.real and ANT2.real co-vary because every acquisition is one shared vector row.
    assert abs(result.weight_vector_covariance_real_imag[0][2]) > 0.0
    assert result.primary_increment_vector_covariance_real_imag is not None
    assert abs(result.primary_increment_vector_covariance_real_imag[0][2]) > 0.0
    assert all(item.weight_center is not None for item in result.arm_diagnostics)


def test_joint_weight_rows_preserve_anticorrelation_that_cancels_in_sum() -> None:
    evidence = _build_campaign("weighted", weight_variation=0.0)
    assert evidence.joint_weights is not None
    c1 = evidence.arms[0].c_i.repeats[0].value.phasor
    d1 = evidence.arms[0].d1_i.repeats[0].value.phasor
    c2 = evidence.arms[1].c_i.repeats[0].value.phasor
    d2 = evidence.arms[1].d1_i.repeats[0].value.phasor
    assert c1 is not None and d1 is not None and c2 is not None and d2 is not None
    increment1 = d1 - c1
    increment2 = d2 - c2
    original_vectors = [repeat.weights for repeat in evidence.joint_weights.repeats]
    offsets = (-2e-4, -1e-4, 0.0, 1e-4, 2e-4)
    modified_repeats = []
    for repeat, original, offset in zip(
        evidence.joint_weights.repeats, original_vectors, offsets, strict=True
    ):
        weight1 = original[0].phasor
        weight2 = original[1].phasor
        assert weight1 is not None and weight2 is not None
        weights = list(original)
        weights[0] = _detected(weight1 + offset / increment1)
        weights[1] = _detected(weight2 - offset / increment2)
        modified_repeats.append(replace(repeat, weights=tuple(weights)))
    joint_weights = replace(evidence.joint_weights, repeats=tuple(modified_repeats))

    result = qualify_closure(
        replace(evidence, joint_weights=joint_weights),
        bootstrap_draw_count=4096,
        bootstrap_seed=765,
    )

    assert result.primary_increment_vector_covariance_real_imag is not None
    # Contribution-real covariance is negative and cancels in the coherent sum.
    covariance = result.primary_increment_vector_covariance_real_imag
    assert covariance[0][2] < 0.0
    assert result.predicted_e is not None
    assert abs(result.predicted_e.covariance_real_imag[0][0]) < abs(covariance[0][2]) * 1e-12


def test_weighted_exact_closure_passes_all_three_quality_gates() -> None:
    result = qualify_closure(_build_campaign("weighted"), bootstrap_draw_count=1024)

    assert result.quality is not None
    assert result.quality.magnitude_gate_passed
    assert result.quality.phase_gate_passed
    assert result.quality.residual_gate_passed
    assert result.closure_claim_supported
    assert result.d2_validation_quality is not None
    assert result.d2_validation_quality.full_complex_equivalent


@pytest.mark.parametrize("count", [3, 4, 6])
def test_rejects_any_repeat_count_other_than_five(count: int) -> None:
    evidence = _build_campaign("arm_preserving")
    repeats = tuple(evidence.arms[0].d2_i.repeats)
    if count == 6:
        repeats += (replace(repeats[-1], repeat_index=6, run_id="sixth-run"),)
    else:
        repeats = repeats[:count]
    d2 = replace(evidence.arms[0].d2_i, repeats=repeats)
    arms = (replace(evidence.arms[0], d2_i=d2), *evidence.arms[1:])

    with pytest.raises(ClosureQualificationError, match="exactly five"):
        qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=256)


def test_rejects_missing_or_reordered_antenna_arm() -> None:
    evidence = _build_campaign("arm_preserving")

    with pytest.raises(ClosureQualificationError, match="exactly ANT1..ANT8"):
        qualify_closure(replace(evidence, arms=evidence.arms[1:]), bootstrap_draw_count=256)

    swapped = (evidence.arms[1], evidence.arms[0], *evidence.arms[2:])
    with pytest.raises(ClosureQualificationError, match="exactly ANT1..ANT8"):
        qualify_closure(replace(evidence, arms=swapped), bootstrap_draw_count=256)


def test_global_h_c_must_be_source_disjoint_from_every_dedicated_c_i() -> None:
    evidence = _build_campaign("arm_preserving")
    global_repeat = evidence.global_h_c.repeats[0]
    c_i = evidence.arms[0].c_i
    original = c_i.repeats[0]
    reused = replace(
        original,
        run_id=global_repeat.run_id,
        condition_id=global_repeat.condition_id,
        stream_id=global_repeat.stream_id,
        artifact_sha256=global_repeat.artifact_sha256,
        raw_iq_sha256=global_repeat.raw_iq_sha256,
        metadata_sha256=global_repeat.metadata_sha256,
        condition_record_sha256=global_repeat.condition_record_sha256,
    )
    modified_c_i = replace(c_i, repeats=(reused, *c_i.repeats[1:]))
    arms = (replace(evidence.arms[0], c_i=modified_c_i), *evidence.arms[1:])

    with pytest.raises(ClosureQualificationError, match="not source-disjoint"):
        qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=256)


def test_expanded_leaf_sources_detect_reuse_hidden_by_new_derived_hashes() -> None:
    evidence = _build_campaign("arm_preserving")
    global_repeat = evidence.global_h_c.repeats[0]
    c_i = evidence.arms[0].c_i
    reused_leaf = global_repeat.leaf_source_sha256s
    disguised = replace(
        c_i.repeats[0],
        leaf_source_sha256s=reused_leaf,
        leaf_source_set_sha256=leaf_source_set_sha256(reused_leaf),
    )
    modified_c_i = replace(c_i, repeats=(disguised, *c_i.repeats[1:]))
    arms = (replace(evidence.arms[0], c_i=modified_c_i), *evidence.arms[1:])

    with pytest.raises(ClosureQualificationError, match="raw leaf source"):
        qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=256)


def test_rejects_topology_identity_not_bound_by_plan() -> None:
    evidence = _build_campaign("arm_preserving")
    c_i = replace(
        evidence.arms[0].c_i,
        topology_identity=evidence.arms[1].c_i.topology_identity,
    )
    arms = (replace(evidence.arms[0], c_i=c_i), *evidence.arms[1:])

    with pytest.raises(ClosureQualificationError, match="immutable plan"):
        qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=256)


def test_rejects_stale_source_commit_even_when_artifacts_are_unique() -> None:
    evidence = _build_campaign("arm_preserving")
    d1 = replace(evidence.arms[2].d1_i, source_commit="b" * 40)
    arms = (*evidence.arms[:2], replace(evidence.arms[2], d1_i=d1), *evidence.arms[3:])

    with pytest.raises(ClosureQualificationError, match="source commit"):
        qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=256)


def test_nondetection_fails_closed_and_retains_only_phase_free_bound() -> None:
    evidence = _build_campaign("weighted")
    d2 = evidence.arms[4].d2_i
    nondetected = replace(
        d2.repeats[2],
        value=ComplexDetection(False, None, 0.0125),
    )
    modified_d2 = replace(d2, repeats=(*d2.repeats[:2], nondetected, *d2.repeats[3:]))
    arms = (*evidence.arms[:4], replace(evidence.arms[4], d2_i=modified_d2), *evidence.arms[5:])

    result = qualify_closure(replace(evidence, arms=arms), bootstrap_draw_count=512)

    assert result.status == "not_evaluable_nondetection"
    assert result.quality is None
    assert result.predicted_e is None
    assert not result.closure_claim_supported
    assert result.nondetections[0].label == "ANT5.d2_i.repeat3"
    assert result.nondetections[0].magnitude_upper_bound == pytest.approx(0.0125)
    assert not result.nondetections[0].phase_synthesized
    assert result.phase_free_bound is not None
    assert not result.phase_free_bound.phase_available
    assert not result.phase_free_bound.zero_phase_synthesized
    assert result.phase_free_bound.residual_magnitude_upper_bound > 0.0


def test_weight_nondetection_also_fails_closed_without_zero_phase() -> None:
    evidence = _build_campaign("weighted")
    assert evidence.joint_weights is not None
    repeat = evidence.joint_weights.repeats[0]
    weights = list(repeat.weights)
    weights[7] = ComplexDetection(False, None, 0.2)
    modified_repeat = replace(repeat, weights=tuple(weights))
    weight_cohort = replace(
        evidence.joint_weights,
        repeats=(modified_repeat, *evidence.joint_weights.repeats[1:]),
    )

    result = qualify_closure(
        replace(evidence, joint_weights=weight_cohort), bootstrap_draw_count=256
    )

    assert result.status == "not_evaluable_nondetection"
    assert result.nondetections[0].label == "joint_weights.repeat1.ANT8"
    assert result.phase_free_bound is not None


def test_weight_vectors_must_be_five_complete_eight_arm_rows() -> None:
    evidence = _build_campaign("weighted")
    assert evidence.joint_weights is not None
    first = evidence.joint_weights.repeats[0]
    incomplete = replace(first, weights=first.weights[:-1])
    cohort = replace(
        evidence.joint_weights,
        repeats=(incomplete, *evidence.joint_weights.repeats[1:]),
    )

    with pytest.raises(ClosureQualificationError, match="exactly ANT1..ANT8"):
        qualify_closure(replace(evidence, joint_weights=cohort), bootstrap_draw_count=256)


def test_quality_output_reports_material_complex_closure_failure() -> None:
    evidence = _build_campaign("weighted")
    rotated = _phasor(1.0, 12.0)
    e_values = [repeat.value.phasor * rotated for repeat in evidence.observed_e.repeats]
    assert all(value is not None for value in e_values)
    observed_e = _replace_values(evidence.observed_e, [complex(value) for value in e_values])

    result = qualify_closure(replace(evidence, observed_e=observed_e), bootstrap_draw_count=512)

    assert result.quality is not None
    assert not result.quality.full_complex_equivalent
    assert not result.quality.phase_gate_passed
    assert not result.quality.residual_gate_passed
    assert result.quality.failure_reasons
    assert not result.closure_claim_supported


def test_equal_magnitude_opposite_phase_is_not_complex_closure() -> None:
    evidence = _build_campaign("weighted")
    opposite_values = []
    for repeat in evidence.observed_e.repeats:
        assert repeat.value.phasor is not None
        opposite_values.append(-repeat.value.phasor)
    observed_e = _replace_values(evidence.observed_e, opposite_values)

    result = qualify_closure(replace(evidence, observed_e=observed_e), bootstrap_draw_count=512)

    assert result.quality is not None
    assert result.quality.magnitude_gate_passed
    assert not result.quality.phase_gate_passed
    assert not result.quality.residual_gate_passed
    assert not result.quality.full_complex_equivalent


def test_d1_and_d2_are_both_required_for_every_arm() -> None:
    evidence = _build_campaign("weighted")
    malformed = replace(evidence.arms[0], d1_i=None)  # type: ignore[arg-type]

    with pytest.raises(ClosureQualificationError, match="ClosureCohort"):
        qualify_closure(
            replace(evidence, arms=(malformed, *evidence.arms[1:])), bootstrap_draw_count=256
        )


def test_plan_and_result_bind_all_immutable_topology_hashes() -> None:
    result = qualify_closure(_build_campaign("weighted"), bootstrap_draw_count=256)

    names = tuple(name for name, _ in result.topology_sha256s)
    assert names[:2] == ("global_h_c", "observed_e")
    assert names[-1] == "joint_weights"
    assert len(names) == 27
    assert all(len(digest) == 64 for _, digest in result.topology_sha256s)
    assert result.source_disjointness_verified


def test_canonical_identity_is_not_mutated_through_its_input_mapping() -> None:
    nested: dict[str, object] = {"value": "before"}
    payload: dict[str, object] = {"schema": "test/v1", "nested": nested}
    identity = make_canonical_identity(payload)

    nested["value"] = "after"

    assert identity.payload()["nested"] == {"value": "before"}
