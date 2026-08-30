from __future__ import annotations

import hashlib
from dataclasses import asdict, replace

import pytest

from smateway.intervention_support import (
    FULL_ROLE_ORDER,
    MAXIMUM_AFTER_TO_BEFORE_RATIO,
    InterventionRepeat,
    InterventionSupportError,
    intervention_repeat_from_document,
    qualify_intervention_support,
)


def _repeat(
    role: str,
    index: int,
    *,
    ratio: float | None,
    upper: float | None = None,
    rx1: float = 1000.0,
    quality: bool = True,
) -> InterventionRepeat:
    return InterventionRepeat(
        repeat_index=index,
        condition_id=f"{role}-condition-{index}",
        stream_id=f"{role}-stream-{index}",
        raw_iq_sha256=hashlib.sha256(f"{role}-raw-{index}".encode()).hexdigest(),
        quality_passed=quality,
        rx1_amplitude_counts=rx1,
        transfer_detected=ratio is not None,
        transfer_amplitude_ratio=ratio,
        transfer_amplitude_upper_bound_ratio=upper,
    )


def _cohorts(
    *,
    boundary_after: float = 0.04,
    full_after: float = 0.04,
    intervention_rx1: float = 1000.0,
) -> dict[str, tuple[InterventionRepeat, ...]]:
    ratios = {
        "boundary_baseline": 0.1,
        "boundary_intervention": boundary_after,
        "full_fixture_baseline": 0.1,
        "full_fixture_intervention": full_after,
    }
    return {
        role: tuple(
            _repeat(
                role,
                index,
                ratio=ratios[role],
                rx1=(intervention_rx1 if role.endswith("intervention") else 1000.0),
            )
            for index in range(1, 6)
        )
        for role in FULL_ROLE_ORDER
    }


def test_simultaneous_gate_passes_only_from_recomputed_repeats() -> None:
    result = qualify_intervention_support(_cohorts(), bootstrap_draw_count=1000)
    assert result.simultaneous_improvement_gate_passed is True
    assert result.rejection_reasons == ()
    assert result.required_pairs == ("boundary", "full_fixture")
    assert all(item.point_improvement_db > 3.0 for item in result.pair_results)


@pytest.mark.parametrize(
    ("cohorts", "reason"),
    [
        (
            _cohorts(full_after=MAXIMUM_AFTER_TO_BEFORE_RATIO * 0.1 * 1.0001),
            "simultaneous_three_db_leakage_improvement_not_proven",
        ),
        (
            _cohorts(intervention_rx1=1000.0 * 10.0 ** (1.01 / 20.0)),
            "simultaneous_rx1_reference_stability_not_proven",
        ),
    ],
)
def test_simultaneous_gate_fails_closed_at_either_constraint(
    cohorts: dict[str, tuple[InterventionRepeat, ...]], reason: str
) -> None:
    result = qualify_intervention_support(cohorts, bootstrap_draw_count=1000)
    assert result.simultaneous_improvement_gate_passed is False
    assert reason in result.rejection_reasons


def test_exact_three_db_threshold_is_inclusive_and_deterministic() -> None:
    exact_after = 0.1 * MAXIMUM_AFTER_TO_BEFORE_RATIO
    cohorts = _cohorts(boundary_after=exact_after, full_after=exact_after)
    first = qualify_intervention_support(cohorts, bootstrap_draw_count=1000, bootstrap_seed=7)
    second = qualify_intervention_support(cohorts, bootstrap_draw_count=1000, bootstrap_seed=7)
    assert first == second
    assert first.simultaneous_improvement_gate_passed is True


def test_phase_free_intervention_nondetection_uses_only_upper_bound() -> None:
    cohorts = _cohorts()
    cohorts["full_fixture_intervention"] = tuple(
        _repeat("full_fixture_intervention", index, ratio=None, upper=0.04) for index in range(1, 6)
    )
    result = qualify_intervention_support(cohorts, bootstrap_draw_count=1000)
    assert result.simultaneous_improvement_gate_passed is True
    full = next(item for item in result.pair_results if item.pair == "full_fixture")
    assert full.intervention_uses_phase_free_upper_bounds is True


def test_baseline_nondetection_cannot_prove_improvement() -> None:
    cohorts = _cohorts()
    cohorts["boundary_baseline"] = tuple(
        _repeat("boundary_baseline", index, ratio=None, upper=0.1) for index in range(1, 6)
    )
    result = qualify_intervention_support(cohorts, bootstrap_draw_count=1000)
    assert result.simultaneous_improvement_gate_passed is False
    assert result.rejection_reasons == (
        "boundary_baseline_nondetection_prevents_improvement_proof",
    )


def test_repeat_sources_must_be_disjoint_and_quality_flags_boolean() -> None:
    cohorts = _cohorts()
    duplicate = cohorts["boundary_baseline"][0]
    cohorts["boundary_intervention"] = (
        replace(cohorts["boundary_intervention"][0], stream_id=duplicate.stream_id),
        *cohorts["boundary_intervention"][1:],
    )
    with pytest.raises(InterventionSupportError, match="reuse an ABI-2 stream"):
        qualify_intervention_support(cohorts, bootstrap_draw_count=1000)

    malformed = asdict(_repeat("role", 1, ratio=0.1))
    malformed["quality_passed"] = 1
    with pytest.raises(InterventionSupportError, match="must be boolean"):
        intervention_repeat_from_document(malformed, role="role")

    malformed = asdict(_repeat("role", 1, ratio=0.1))
    malformed["raw_iq_sha256"] = ["a"] * 64
    with pytest.raises(InterventionSupportError, match="SHA-256 is malformed"):
        intervention_repeat_from_document(malformed, role="role")
