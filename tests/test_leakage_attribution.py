from __future__ import annotations

from dataclasses import replace
from math import cos, radians, sin, sqrt
from typing import Any

import pytest

from smateway import leakage_attribution as attribution_module
from smateway.leakage_attribution import (
    ATTRIBUTION_REPEAT_COUNT,
    CONDITIONED_EXCESS_DISCRIMINATOR_RATIO,
    FULL_COMPLEX_RESIDUAL_FRACTION,
    METROLOGY_LOW_RATIO,
    OPERATIONAL_LOW_RATIO,
    AttributionRepeat,
    AttributionThresholds,
    ClosureComponentEvidence,
    LeakageAttributionError,
    StageAttributionEvidence,
    summarize_staged_attribution,
)

SHARED_FIXTURE = {
    "campaign_fixture": "fixture-a",
    "rx1_reference_plane": "pluto-rx1-sma",
    "rx2_measurement_plane": "pluto-rx2-sma",
}
PROVENANCE = {
    "center_frequency_hz": 5_800_000_000,
    "attribution_gain_db": -20.0,
    "smateway_commit": "1" * 40,
    "native_libiio_sha256": "2" * 64,
}
GROUP_ID = "campaign-5g8-a"
STAGE_SEEDS = {"A": 10, "B": 20, "C": 30, "E": 40}


def _digest(value: int) -> str:
    return f"{value:064x}"


def _polar(magnitude: float, phase_deg: float) -> complex:
    angle = radians(phase_deg)
    return magnitude * complex(cos(angle), sin(angle))


def _repeats(
    owner: str,
    seed: int,
    values: complex | None | list[complex | None],
    *,
    upper_bound: float | list[float] | None = None,
) -> tuple[AttributionRepeat, ...]:
    raw_values = [values] * ATTRIBUTION_REPEAT_COUNT if not isinstance(values, list) else values
    if len(raw_values) != ATTRIBUTION_REPEAT_COUNT:
        raise AssertionError("synthetic helper requires five values")
    if isinstance(upper_bound, list):
        bounds = upper_bound
    else:
        bounds = [upper_bound] * ATTRIBUTION_REPEAT_COUNT
    return tuple(
        AttributionRepeat(
            repeat_index=index,
            condition_id=f"{owner}-repeat-{index}",
            stream_id=seed * 100 + index,
            artifact_sha256=_digest(seed * 100 + index),
            quality_passed=True,
            detected=value is not None,
            phasor=value,
            amplitude_upper_bound_ratio=(None if value is not None else bounds[index - 1]),
        )
        for index, value in enumerate(raw_values, start=1)
    )


def _stage(
    stage: str,
    value: complex | None | list[complex | None],
    *,
    upper_bound: float | list[float] | None = None,
    group_id: str = GROUP_ID,
    shared_fixture: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> StageAttributionEvidence:
    return StageAttributionEvidence(
        stage=stage,
        run_id=f"run-{stage.lower()}",
        contemporaneous_group_id=group_id,
        shared_fixture_identity=(
            dict(SHARED_FIXTURE) if shared_fixture is None else shared_fixture
        ),
        provenance_identity=dict(PROVENANCE) if provenance is None else provenance,
        stage_fixture_identity={"stage": stage, "evidence": f"fixture-{stage.lower()}"},
        repeats=_repeats(
            f"stage-{stage.lower()}",
            STAGE_SEEDS[stage],
            value,
            upper_bound=upper_bound,
        ),
    )


def _campaign(
    *,
    a: complex | None | list[complex | None] = 0.03 + 0.0j,
    b: complex | None | list[complex | None] = 0.04 + 0.0j,
    c: complex | None | list[complex | None] = 0.05 + 0.0j,
    e: complex | None | list[complex | None] = 0.06 + 0.0j,
    a_upper: float | list[float] | None = None,
    b_upper: float | list[float] | None = None,
    c_upper: float | list[float] | None = None,
    e_upper: float | list[float] | None = None,
) -> tuple[StageAttributionEvidence, ...]:
    return (
        _stage("A", a, upper_bound=a_upper),
        _stage("B", b, upper_bound=b_upper),
        _stage("C", c, upper_bound=c_upper),
        _stage("E", e, upper_bound=e_upper),
    )


def _component(
    name: str,
    value: complex | None | list[complex | None],
    seed: int,
    *,
    upper_bound: float | list[float] | None = None,
    group_id: str = GROUP_ID,
    shared_fixture: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    run_id: str | None = None,
    upstream_artifact_sha256s: tuple[str, ...] | None = None,
) -> ClosureComponentEvidence:
    return ClosureComponentEvidence(
        name=name,
        run_id=f"component-run-{seed}" if run_id is None else run_id,
        contemporaneous_group_id=group_id,
        shared_fixture_identity=(
            dict(SHARED_FIXTURE) if shared_fixture is None else shared_fixture
        ),
        provenance_identity=dict(PROVENANCE) if provenance is None else provenance,
        component_evidence_identity={"component": name, "source": f"one-hot-{seed}"},
        upstream_artifact_sha256s=(
            (_digest(seed * 100_000 + 1), _digest(seed * 100_000 + 2))
            if upstream_artifact_sha256s is None
            else upstream_artifact_sha256s
        ),
        repeats=_repeats(name, seed, value, upper_bound=upper_bound),
    )


def test_green_campaign_requires_and_reports_fresh_repeated_stage_e() -> None:
    summary = summarize_staged_attribution(tuple(reversed(_campaign())))

    assert summary.contemporaneous_stage_e_reference_verified is True
    assert summary.all_five_repeats_per_stage_verified is True
    assert summary.stage_e_run_id == "run-e"
    assert summary.stage("E").disposition == "full_complex_equivalent"
    assert (
        summary.stage("E").versus_stage_e.normalized_complex_residual_upper_simultaneous_95 == 0.0
    )
    assert summary.shared_fixture_identity_sha256
    assert summary.provenance_identity_sha256


def test_full_magnitude_is_not_misreported_as_full_complex_equivalence() -> None:
    summary = summarize_staged_attribution(_campaign(a=-0.06 + 0.0j))
    result = summary.stage("A")

    assert result.versus_stage_e.full_magnitude_equivalent is True
    assert result.versus_stage_e.full_complex_equivalent is False
    assert result.disposition == "full_magnitude_only"
    assert abs(result.versus_stage_e.phase_error_deg or 0.0) == pytest.approx(180.0)


def test_full_complex_boundary_accepts_joint_point_inside_frozen_guards() -> None:
    magnitude = 0.06 * 10.0 ** (0.2 / 20.0)
    candidate = _polar(magnitude, 2.0)
    summary = summarize_staged_attribution(_campaign(a=candidate))
    result = summary.stage("A").versus_stage_e

    assert result.full_magnitude_equivalent is True
    assert result.full_complex_equivalent is True
    assert result.normalized_complex_residual_upper_simultaneous_95 is not None
    assert (
        result.normalized_complex_residual_upper_simultaneous_95 <= FULL_COMPLEX_RESIDUAL_FRACTION
    )
    assert result.simultaneous_confidence == pytest.approx(0.95)
    assert result.per_metric_bonferroni_confidence == pytest.approx(1.0 - 0.05 / 3.0)


def test_simultaneous_gate_rejects_case_that_marginal_95_amplitude_would_accept() -> None:
    candidate = [
        0.06094642076407505 + 0j,
        0.06016303551575711 + 0j,
        0.06008425415331469 + 0j,
        0.06025153049766692 + 0j,
        0.06026230629077018 + 0j,
    ]
    reference = [
        0.06004856988054499 + 0j,
        0.05986542628822310 + 0j,
        0.06013023331548095 + 0j,
        0.05950615588456369 + 0j,
        0.05932232644441370 + 0j,
    ]
    result = (
        summarize_staged_attribution(_campaign(a=candidate, e=reference)).stage("A").versus_stage_e
    )

    # The former marginal 95% amplitude interval ended at 0.1554 dB and
    # passed.  The Bonferroni family-wise interval reaches 0.2077 dB, so the
    # combined three-metric equivalence decision must fail closed.
    assert result.amplitude_error_simultaneous_95_interval_db is not None
    assert result.amplitude_error_simultaneous_95_interval_db[1] > 0.2
    assert result.full_magnitude_equivalent is False
    assert result.full_complex_equivalent is False
    assert "bonferroni_simultaneous" in result.statistical_method


def test_phase_wrap_near_zero_remains_full_complex_equivalent() -> None:
    phases = [-1.0, 1.0, -0.5, 0.5, 0.0]
    values = [_polar(0.06, phase) for phase in phases]
    result = summarize_staged_attribution(_campaign(a=values)).stage("A")

    assert result.versus_stage_e.full_complex_equivalent is True
    interval = result.versus_stage_e.phase_error_simultaneous_95_interval_deg
    assert interval is not None
    assert interval[0] >= -2.0
    assert interval[1] <= 2.0


def test_complex_bootstrap_decision_is_invariant_to_common_phase_rotation() -> None:
    candidate = [
        0.05990716602163367 + 0.0006571509781721387j,
        0.05985186961279038 + 0.0004927779038670832j,
        0.05909896651058225 - 0.0004545112876987766j,
        0.06016524285396831 + 0.00024059311287885068j,
        0.059634694962410535 - 0.0015947610103475348j,
    ]
    reference = [0.06 + 0j] * ATTRIBUTION_REPEAT_COUNT
    rotation = _polar(1.0, 37.0)
    baseline = (
        summarize_staged_attribution(_campaign(a=candidate, e=reference)).stage("A").versus_stage_e
    )
    rotated = (
        summarize_staged_attribution(
            _campaign(
                a=[value * rotation for value in candidate],
                e=[value * rotation for value in reference],
            )
        )
        .stage("A")
        .versus_stage_e
    )

    assert baseline.full_magnitude_equivalent is rotated.full_magnitude_equivalent
    assert baseline.full_complex_equivalent is rotated.full_complex_equivalent
    assert rotated.amplitude_error_db == pytest.approx(baseline.amplitude_error_db, abs=1e-10)
    assert rotated.phase_error_deg == pytest.approx(baseline.phase_error_deg, abs=1e-10)
    assert rotated.normalized_complex_residual_upper_simultaneous_95 == pytest.approx(
        baseline.normalized_complex_residual_upper_simultaneous_95,
        abs=1e-10,
    )


def test_rotation_equivariant_center_accepts_generic_noncollinear_samples() -> None:
    values = [
        -0.9891213503478509 + 0.5771037912572513j,
        -0.3677866514678832 - 0.6364636463709805j,
        1.2879252612892487 + 0.5419522204102933j,
        0.1939744191326132 - 0.3165954511658161j,
        0.9202308996398569 - 0.32238911615896015j,
    ]
    estimate = summarize_staged_attribution(_campaign(a=values)).stage("A").estimate

    assert estimate.all_repeats_detected is True
    assert estimate.center in values
    assert "tie_averaged_complex_medoid" in estimate.interval_method


def test_complex_medoid_bootstrap_is_repeat_permutation_invariant() -> None:
    a = 0.060 + 0j
    b = 0.061 + 0j
    c = 0.0605 + 1j * (sqrt(3.0) / 2.0 * 0.001)
    forward = summarize_staged_attribution(_campaign(a=[a, a, b, b, c])).stage("A")
    reordered = summarize_staged_attribution(_campaign(a=[b, b, a, a, c])).stage("A")
    forward_distribution = attribution_module._raw_bootstrap_distribution(
        tuple(
            attribution_module._Sample(value, abs(value), abs(value)) for value in (a, a, b, b, c)
        )
    )
    reordered_distribution = attribution_module._raw_bootstrap_distribution(
        tuple(
            attribution_module._Sample(value, abs(value), abs(value)) for value in (b, b, a, a, c)
        )
    )

    assert forward.estimate.center == pytest.approx(reordered.estimate.center, abs=1e-14)

    def sort_complex(value: complex) -> tuple[float, float]:
        return value.real, value.imag

    assert sorted(forward_distribution.draws, key=sort_complex) == pytest.approx(
        sorted(reordered_distribution.draws, key=sort_complex),
        abs=1e-14,
    )
    assert forward.estimate.uncertainty_radius_95 == pytest.approx(
        reordered.estimate.uncertainty_radius_95,
        abs=1e-14,
    )
    assert forward.versus_stage_e.full_magnitude_equivalent is (
        reordered.versus_stage_e.full_magnitude_equivalent
    )
    assert forward.versus_stage_e.full_complex_equivalent is (
        reordered.versus_stage_e.full_complex_equivalent
    )
    assert forward.versus_stage_e.amplitude_error_simultaneous_95_interval_db == pytest.approx(
        reordered.versus_stage_e.amplitude_error_simultaneous_95_interval_db,
        abs=1e-12,
    )


def test_conditioned_excess_is_partial_material_when_not_full_magnitude() -> None:
    summary = summarize_staged_attribution(
        _campaign(a=CONDITIONED_EXCESS_DISCRIMINATOR_RATIO + 0.0j)
    )
    result = summary.stage("A")

    assert result.conditioned_excess_supported is True
    assert result.partial_material is True
    assert result.small_detected is False
    assert result.disposition == "partial_material"


def test_material_support_starts_above_operational_not_conditioned_threshold() -> None:
    result = summarize_staged_attribution(_campaign(a=0.020 + 0.0j)).stage("A")

    assert result.operational_low_supported is False
    assert result.partial_material is True
    assert result.conditioned_excess_supported is False
    assert result.disposition == "partial_material"


def test_detected_below_operational_threshold_is_small_detected() -> None:
    result = summarize_staged_attribution(_campaign(a=0.01 + 0.0j)).stage("A")

    assert result.conditioned_excess_supported is False
    assert result.small_detected is True
    assert result.operational_low_supported is True
    assert result.disposition == "small_detected"


@pytest.mark.parametrize(
    ("bound", "disposition", "operational", "metrology"),
    [
        (METROLOGY_LOW_RATIO, "metrology_low", True, True),
        (0.010, "indeterminate", True, False),
        (0.020, "indeterminate", False, False),
    ],
)
def test_nondetections_use_phase_free_bounds(
    bound: float,
    disposition: str,
    operational: bool,
    metrology: bool,
) -> None:
    result = summarize_staged_attribution(_campaign(a=None, a_upper=bound)).stage("A")

    assert result.estimate.center is None
    assert result.estimate.phase_deg is None
    assert result.estimate.nondetection_phase_synthesized is False
    assert result.estimate.conservative_magnitude_upper == pytest.approx(bound)
    assert result.operational_low_supported is operational
    assert result.metrology_low_supported is metrology
    assert result.disposition == disposition
    assert result.versus_stage_e.available is False


def test_operational_low_threshold_is_inclusive_and_frozen() -> None:
    result = summarize_staged_attribution(_campaign(a=None, a_upper=OPERATIONAL_LOW_RATIO)).stage(
        "A"
    )

    assert result.operational_low_supported is True
    assert result.metrology_low_supported is False
    assert result.metrology_low_interval_straddled is True
    assert result.disposition == "indeterminate"


@pytest.mark.parametrize(
    ("threshold", "half_span", "straddle_field"),
    [
        (METROLOGY_LOW_RATIO, 0.0005, "metrology_low_interval_straddled"),
        (OPERATIONAL_LOW_RATIO, 0.001, "operational_low_interval_straddled"),
        (
            CONDITIONED_EXCESS_DISCRIMINATOR_RATIO,
            0.002,
            "conditioned_excess_interval_straddled",
        ),
    ],
)
def test_threshold_straddles_are_primary_indeterminate(
    threshold: float,
    half_span: float,
    straddle_field: str,
) -> None:
    values = [
        complex(threshold - half_span),
        complex(threshold - half_span),
        complex(threshold),
        complex(threshold + half_span),
        complex(threshold + half_span),
    ]
    result = summarize_staged_attribution(_campaign(a=values)).stage("A")

    assert getattr(result, straddle_field) is True
    assert result.disposition == "indeterminate"
    assert result.small_detected is False


def test_mixed_detection_and_nondetection_never_invents_complex_center() -> None:
    values: list[complex | None] = [0.001 + 0.0j, None, 0.001 + 0.0j, None, 0.001 + 0.0j]
    result = summarize_staged_attribution(_campaign(a=values, a_upper=0.0015)).stage("A")

    assert result.estimate.detected_repeat_count == 3
    assert result.estimate.nondetected_repeat_count == 2
    assert result.estimate.center is None
    assert result.estimate.phase_deg is None
    assert result.metrology_low_supported is True


def test_robust_center_is_not_the_mean_of_one_outlier() -> None:
    values = [0.03 + 0.0j] * 4 + [0.30 + 0.0j]
    estimate = summarize_staged_attribution(_campaign(a=values)).stage("A").estimate

    assert estimate.center == pytest.approx(0.03 + 0.0j)
    assert estimate.uncertainty_radius_95 is not None
    assert estimate.uncertainty_radius_95 > 0.0


def test_detected_but_unresolved_complex_vector_is_indeterminate() -> None:
    values = [0.03 + 0j, -0.03 + 0j, 0.02 + 0j, -0.02 + 0j, 0.001 + 0j]
    result = summarize_staged_attribution(_campaign(a=values)).stage("A")

    assert result.estimate.all_repeats_detected is True
    assert result.estimate.magnitude_excludes_zero is False
    assert result.operational_low_supported is False
    assert result.disposition == "indeterminate"
    assert result.indeterminate is True


def test_independent_complex_delta_detects_phase_cancellation_hidden_by_magnitudes() -> None:
    summary = summarize_staged_attribution(_campaign(a=0.03 + 0.0j, b=-0.03 + 0.0j))
    delta = summary.boundary("B_MINUS_A")

    assert delta.statistically_paired_repeats is False
    assert delta.complex_two_sample_difference_available is True
    assert delta.difference_method == ("independent_two_sample_complex_bootstrap_center_difference")
    assert delta.assessment.estimate.center == pytest.approx(-0.06 + 0.0j)
    assert delta.assessment.estimate.magnitude == pytest.approx(0.06)
    assert delta.assessment.versus_stage_e.full_magnitude_equivalent is True
    assert delta.assessment.versus_stage_e.full_complex_equivalent is False


def test_independent_delta_with_nondetection_reports_triangle_bounds_without_phase() -> None:
    summary = summarize_staged_attribution(_campaign(a=None, a_upper=0.002, b=0.04 + 0.0j))
    delta = summary.boundary("B_MINUS_A")

    assert delta.statistically_paired_repeats is False
    assert delta.complex_two_sample_difference_available is False
    assert delta.difference_method == "independent_two_sample_phase_free_triangle_bounds"
    assert delta.assessment.estimate.center is None
    assert delta.assessment.estimate.phase_deg is None
    assert delta.assessment.estimate.conservative_magnitude_lower == pytest.approx(0.038)
    assert delta.assessment.estimate.conservative_magnitude_upper == pytest.approx(0.042)
    assert delta.assessment.conditioned_excess_supported is True


def test_separate_stage_runs_do_not_receive_false_matched_pair_precision() -> None:
    a_values = [0.01 + 0j, 0.02 + 0j, 0.03 + 0j, 0.04 + 0j, 0.05 + 0j]
    b_values = [0.02 + 0j, 0.03 + 0j, 0.04 + 0j, 0.05 + 0j, 0.06 + 0j]
    delta = summarize_staged_attribution(_campaign(a=a_values, b=b_values)).boundary("B_MINUS_A")

    # Elementwise subtraction would be a misleading constant +0.01.  Separate
    # rewired stage runs instead carry independent two-sample uncertainty.
    assert delta.assessment.estimate.center == pytest.approx(0.01 + 0j)
    assert delta.assessment.estimate.uncertainty_radius_95 is not None
    assert delta.assessment.estimate.uncertainty_radius_95 > 0.0
    assert "independent_multisample" in delta.assessment.estimate.interval_method
    algebraic_delta = next(
        term
        for term in summarize_staged_attribution(
            _campaign(a=a_values, b=b_values)
        ).algebraic_decomposition.terms
        if term.name == "delta_b_minus_a"
    )
    assert algebraic_delta.estimate.uncertainty_radius_95 == pytest.approx(
        delta.assessment.estimate.uncertainty_radius_95
    )
    assert algebraic_delta.estimate.uncertainty_radius_95 > 0.0
    assert "independent_multisample" in algebraic_delta.estimate.interval_method


def test_e_minus_c_comparison_retains_shared_stage_e_covariance() -> None:
    e_values = [0.05 + 0j, 0.055 + 0j, 0.06 + 0j, 0.065 + 0j, 0.07 + 0j]
    delta = summarize_staged_attribution(_campaign(c=0.02 + 0j, e=e_values)).boundary("E_MINUS_C")

    assert delta.statistically_paired_repeats is False
    assert delta.difference_method == (
        "independent_stage_e_minus_c_complex_bootstrap_with_shared_reference_covariance"
    )
    assert "shared_stage_e_covariance" in delta.assessment.versus_stage_e.statistical_method
    assert delta.assessment.estimate.center == pytest.approx(0.04 + 0j)


def test_candidate_vs_stage_e_does_not_use_elementwise_repeat_ratios() -> None:
    values = [0.05 + 0j, 0.055 + 0j, 0.06 + 0j, 0.065 + 0j, 0.07 + 0j]
    comparison = (
        summarize_staged_attribution(_campaign(a=values, e=values)).stage("A").versus_stage_e
    )

    # Elementwise ratios are exactly one, but A and E are separate runs and are
    # therefore compared with an independent two-sample bootstrap.
    assert comparison.amplitude_error_db == pytest.approx(0.0)
    assert comparison.amplitude_error_simultaneous_95_interval_db is not None
    assert comparison.amplitude_error_simultaneous_95_interval_db != (0.0, 0.0)
    assert comparison.full_magnitude_equivalent is False
    assert "independent_two_sample" in comparison.statistical_method


def test_algebraic_decomposition_closes_but_is_explicitly_not_independent() -> None:
    summary = summarize_staged_attribution(_campaign())
    decomposition = summary.algebraic_decomposition

    assert decomposition.available is True
    assert decomposition.algebraic_identity_only is True
    assert decomposition.independent_closure_claim is False
    assert decomposition.closure_residual is not None
    assert decomposition.closure_residual.center == pytest.approx(0.0 + 0.0j)
    assert decomposition.closure_residual.uncertainty_radius_95 is None
    assert decomposition.closure_residual.interval_method == (
        "algebraic_point_only_no_inferential_interval"
    )
    assert [term.name for term in decomposition.terms] == [
        "stage_a_common",
        "delta_b_minus_a",
        "delta_c_minus_b",
        "delta_e_minus_c",
    ]
    assert len(decomposition.counterfactuals) == 4


def test_algebraic_counterfactual_preserves_vector_cancellation() -> None:
    summary = summarize_staged_attribution(
        _campaign(a=0.05 + 0j, b=0.03 + 0j, c=0.03 + 0j, e=0.06 + 0j)
    )
    counterfactual = next(
        item
        for item in summary.algebraic_decomposition.counterfactuals
        if item.removed_term == "delta_b_minus_a"
    )

    # delta BA is -0.02; removing this cancelling term increases the total.
    assert counterfactual.estimate.center == pytest.approx(0.08 + 0j)
    assert counterfactual.estimate.uncertainty_radius_95 is None
    assert counterfactual.magnitude_change_from_stage_e == pytest.approx(0.02)
    assert counterfactual.versus_stage_e.available is False
    assert counterfactual.versus_stage_e.reason_unavailable == (
        "algebraic_counterfactual_is_not_independently_measured"
    )


def test_nondetected_stage_disables_complex_algebraic_counterfactuals() -> None:
    decomposition = summarize_staged_attribution(
        _campaign(a=None, a_upper=0.002)
    ).algebraic_decomposition

    assert decomposition.available is False
    assert decomposition.counterfactuals == ()
    assert decomposition.reason_unavailable is not None


def test_independent_components_can_close_stage_e_in_magnitude_and_complex_plane() -> None:
    stages = _campaign(c=0.02 + 0j, e=0.06 + 0j)
    components = (
        _component("d_ant1_increment", 0.01 + 0j, 50),
        _component("d_ant2_increment", 0.03 + 0j, 60),
    )

    closure = summarize_staged_attribution(
        stages,
        closure_components=components,
    ).independent_closure

    assert closure is not None
    assert closure.independent_of_algebraic_decomposition is True
    assert closure.evidence_source_disjointness_verified is True
    assert closure.stochastic_independence_proven is False
    assert closure.component_count == 2
    assert all(
        component.evidence_source_disjointness_verified
        and len(component.component_evidence_identity_sha256) == 64
        and len(component.upstream_artifact_set_sha256) == 64
        for component in closure.components
    )
    assert closure.predicted_stage_e.estimate.center == pytest.approx(0.06 + 0j)
    assert closure.magnitude_closure_supported is True
    assert closure.complex_closure_supported is True
    assert closure.observed_minus_predicted.conservative_magnitude_upper == pytest.approx(0.0)


def test_independent_closure_distinguishes_magnitude_only_prediction() -> None:
    stages = _campaign(c=-0.02 + 0j, e=0.06 + 0j)
    closure = summarize_staged_attribution(
        stages,
        closure_components=(_component("negative_increment", -0.04 + 0j, 50),),
    ).independent_closure

    assert closure is not None
    assert closure.predicted_stage_e.estimate.center == pytest.approx(-0.06 + 0j)
    assert closure.magnitude_closure_supported is True
    assert closure.complex_closure_supported is False


def test_independent_counterfactual_can_show_removal_increases_total_magnitude() -> None:
    stages = _campaign(c=0.05 + 0j, e=0.06 + 0j)
    closure = summarize_staged_attribution(
        stages,
        closure_components=(
            _component("cancelling_increment", -0.02 + 0j, 50),
            _component("reinforcing_increment", 0.03 + 0j, 60),
        ),
    ).independent_closure
    assert closure is not None
    counterfactual = next(
        item for item in closure.counterfactuals if item.removed_term == "cancelling_increment"
    )

    assert counterfactual.estimate.center == pytest.approx(0.08 + 0j)
    assert counterfactual.magnitude_change_from_stage_e == pytest.approx(0.02)


def test_nondetected_closure_component_cannot_claim_complex_closure() -> None:
    stages = _campaign(c=0.055 + 0j, e=0.06 + 0j)
    closure = summarize_staged_attribution(
        stages,
        closure_components=(_component("bounded_increment", None, 50, upper_bound=0.005),),
    ).independent_closure

    assert closure is not None
    assert closure.predicted_stage_e.estimate.center is None
    assert closure.predicted_stage_e.estimate.phase_deg is None
    assert closure.magnitude_closure_supported is False
    assert closure.complex_closure_supported is False


def test_missing_or_duplicate_stage_is_rejected() -> None:
    stages = list(_campaign())
    with pytest.raises(LeakageAttributionError, match="exactly stages"):
        summarize_staged_attribution(stages[:3])

    stages[-1] = replace(stages[-1], stage="C", run_id="run-c-second")
    with pytest.raises(LeakageAttributionError, match="only once"):
        summarize_staged_attribution(stages)


def test_historical_or_mismatched_stage_e_group_is_rejected() -> None:
    stages = list(_campaign())
    stages[-1] = replace(stages[-1], contemporaneous_group_id="historical-stage-e")

    with pytest.raises(LeakageAttributionError, match="contemporaneous"):
        summarize_staged_attribution(stages)


def test_shared_fixture_and_provenance_mismatches_are_rejected() -> None:
    stages = list(_campaign())
    stages[1] = replace(stages[1], shared_fixture_identity={"fixture": "different"})
    with pytest.raises(LeakageAttributionError, match="shared fixture"):
        summarize_staged_attribution(stages)

    stages = list(_campaign())
    stages[2] = replace(stages[2], provenance_identity={"commit": "different"})
    with pytest.raises(LeakageAttributionError, match="provenance"):
        summarize_staged_attribution(stages)


def test_nested_identity_key_order_does_not_create_a_false_mismatch() -> None:
    first = {"nested": {"a": 1, "b": 2}, "fixture": "same"}
    second = {"fixture": "same", "nested": {"b": 2, "a": 1}}
    stages = list(_campaign())
    stages[0] = replace(stages[0], shared_fixture_identity=first)
    stages[1] = replace(stages[1], shared_fixture_identity=second)
    stages[2] = replace(stages[2], shared_fixture_identity=first)
    stages[3] = replace(stages[3], shared_fixture_identity=second)

    summary = summarize_staged_attribution(stages)

    assert summary.shared_fixture_identity == {
        "fixture": "same",
        "nested": {"a": 1, "b": 2},
    }


def test_every_stage_requires_exactly_five_unique_quality_repeats() -> None:
    stages = list(_campaign())
    stages[0] = replace(stages[0], repeats=stages[0].repeats[:4])
    with pytest.raises(LeakageAttributionError, match="exactly five"):
        summarize_staged_attribution(stages)

    stages = list(_campaign())
    repeats = list(stages[0].repeats)
    repeats[-1] = replace(repeats[-1], repeat_index=1)
    stages[0] = replace(stages[0], repeats=repeats)
    with pytest.raises(LeakageAttributionError, match="duplicate repeat"):
        summarize_staged_attribution(stages)

    stages = list(_campaign())
    repeats = list(stages[0].repeats)
    repeats[2] = replace(repeats[2], quality_passed=False)
    stages[0] = replace(stages[0], repeats=repeats)
    with pytest.raises(LeakageAttributionError, match="failed measurement quality"):
        summarize_staged_attribution(stages)


@pytest.mark.parametrize("identity_field", ["condition_id", "stream_id", "artifact_sha256"])
def test_repeat_evidence_identities_must_be_globally_unique(identity_field: str) -> None:
    stages = list(_campaign())
    repeats = list(stages[1].repeats)
    duplicate = getattr(stages[0].repeats[0], identity_field)
    repeats[0] = replace(repeats[0], **{identity_field: duplicate})
    stages[1] = replace(stages[1], repeats=repeats)

    with pytest.raises(LeakageAttributionError, match="globally unique"):
        summarize_staged_attribution(stages)


def test_nondetection_cannot_carry_a_phasor_and_detection_cannot_carry_a_bound() -> None:
    stages = list(_campaign())
    repeats = list(stages[0].repeats)
    repeats[0] = replace(repeats[0], detected=False, amplitude_upper_bound_ratio=0.01)
    stages[0] = replace(stages[0], repeats=repeats)
    with pytest.raises(LeakageAttributionError, match="synthetic phasor"):
        summarize_staged_attribution(stages)

    stages = list(_campaign())
    repeats = list(stages[0].repeats)
    repeats[0] = replace(repeats[0], amplitude_upper_bound_ratio=0.01)
    stages[0] = replace(stages[0], repeats=repeats)
    with pytest.raises(LeakageAttributionError, match="must not substitute"):
        summarize_staged_attribution(stages)


def test_stage_e_must_be_five_detected_nonzero_complex_references() -> None:
    with pytest.raises(LeakageAttributionError, match="five detected complex"):
        summarize_staged_attribution(_campaign(e=None, e_upper=0.01))

    unresolved = [0.06 + 0j, -0.06 + 0j, 0.04 + 0j, -0.04 + 0j, 0.001 + 0j]
    with pytest.raises(LeakageAttributionError, match="uncertainty includes zero"):
        summarize_staged_attribution(_campaign(e=unresolved))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operational_low_ratio", 0.0, "must be positive"),
        ("metrology_low_ratio", 0.02, "must not exceed"),
        ("conditioned_excess_discriminator_ratio", 0.001, "must not exceed conditioned"),
        ("bootstrap_confidence", 0.90, "frozen at 0.95"),
    ],
)
def test_threshold_contract_fails_closed(field: str, value: float, message: str) -> None:
    with pytest.raises(LeakageAttributionError, match=message):
        AttributionThresholds(**{field: value})


@pytest.mark.parametrize("mismatch", ["group", "fixture", "provenance"])
def test_independent_component_must_share_comparison_identity(mismatch: str) -> None:
    kwargs: dict[str, Any] = {}
    if mismatch == "group":
        kwargs["group_id"] = "other-group"
    elif mismatch == "fixture":
        kwargs["shared_fixture"] = {"fixture": "other"}
    else:
        kwargs["provenance"] = {"source": "other"}
    component = _component("bad-component", 0.01 + 0j, 50, **kwargs)

    with pytest.raises(LeakageAttributionError, match=mismatch):
        summarize_staged_attribution(_campaign(), closure_components=(component,))


def test_duplicate_and_reserved_component_names_are_rejected() -> None:
    duplicate = _component("same", 0.01 + 0j, 50)
    second = _component("same", 0.02 + 0j, 60)
    with pytest.raises(LeakageAttributionError, match="unique and unreserved"):
        summarize_staged_attribution(_campaign(), closure_components=(duplicate, second))

    reserved = _component("stage_c_baseline", 0.01 + 0j, 50)
    with pytest.raises(LeakageAttributionError, match="unique and unreserved"):
        summarize_staged_attribution(_campaign(), closure_components=(reserved,))


def test_closure_component_raw_dependencies_must_be_disjoint_from_stages() -> None:
    stages = _campaign()
    reused_stage_artifact = stages[2].repeats[0].artifact_sha256
    component = _component(
        "reused-stage-c-baseline",
        0.01 + 0j,
        50,
        upstream_artifact_sha256s=(reused_stage_artifact,),
    )

    with pytest.raises(LeakageAttributionError, match="evidence-source disjoint"):
        summarize_staged_attribution(stages, closure_components=(component,))


def test_closure_components_cannot_share_raw_dependencies_or_run_identity() -> None:
    shared_upstream = (_digest(9_000_001),)
    first = _component(
        "first",
        0.01 + 0j,
        50,
        upstream_artifact_sha256s=shared_upstream,
    )
    second = _component(
        "second",
        0.02 + 0j,
        60,
        upstream_artifact_sha256s=shared_upstream,
    )
    with pytest.raises(LeakageAttributionError, match="evidence-source disjoint"):
        summarize_staged_attribution(_campaign(), closure_components=(first, second))

    duplicate_run = replace(second, run_id=first.run_id)
    with pytest.raises(LeakageAttributionError, match="run IDs must be globally unique"):
        summarize_staged_attribution(
            _campaign(),
            closure_components=(first, duplicate_run),
        )


def test_closure_component_evidence_identities_must_be_unique() -> None:
    first = _component("first", 0.01 + 0j, 50)
    second = replace(
        _component("second", 0.02 + 0j, 60),
        component_evidence_identity=first.component_evidence_identity,
    )

    with pytest.raises(LeakageAttributionError, match="evidence identities"):
        summarize_staged_attribution(_campaign(), closure_components=(first, second))
