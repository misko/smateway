"""Pure qualification for a source-bound 5.8-GHz intervention comparison.

The file-facing producer lives in ``scripts/analyze_5g8_intervention_support.py``.
This module deliberately accepts only normalized repeat observations so the
statistical decision can be tested without hardware, files, libiio, or Git.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, log10

import numpy as np
import numpy.typing as npt

INTERVENTION_SUPPORT_ANALYSIS_KIND = "5g8_intervention_support_analysis_v1"
INTERVENTION_SUPPORT_RESULT_KIND = "5g8_intervention_support_result_v1"

BOUNDARY_BASELINE = "boundary_baseline"
BOUNDARY_INTERVENTION = "boundary_intervention"
FULL_FIXTURE_BASELINE = "full_fixture_baseline"
FULL_FIXTURE_INTERVENTION = "full_fixture_intervention"

FULL_ROLE_ORDER = (
    BOUNDARY_BASELINE,
    BOUNDARY_INTERVENTION,
    FULL_FIXTURE_BASELINE,
    FULL_FIXTURE_INTERVENTION,
)
FULL_FIXTURE_ROLE_ORDER = (FULL_FIXTURE_BASELINE, FULL_FIXTURE_INTERVENTION)
ROLE_PAIRS = {
    "boundary": (BOUNDARY_BASELINE, BOUNDARY_INTERVENTION),
    "full_fixture": (FULL_FIXTURE_BASELINE, FULL_FIXTURE_INTERVENTION),
}

ATTRIBUTION_REPEAT_COUNT = 5
MINIMUM_IMPROVEMENT_DB = 3.0
MAXIMUM_AFTER_TO_BEFORE_RATIO = 10.0 ** (-MINIMUM_IMPROVEMENT_DB / 20.0)
MAXIMUM_ABS_RX1_REFERENCE_CHANGE_DB = 1.0
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_DRAW_COUNT = 32_768
DEFAULT_BOOTSTRAP_SEED = 0x5A8A11


class InterventionSupportError(ValueError):
    """Normalized evidence is malformed or reuses an acquisition source."""


@dataclass(frozen=True, slots=True)
class InterventionRepeat:
    """One independently reanalyzed attribution-gain capture."""

    repeat_index: int
    condition_id: str
    stream_id: str
    raw_iq_sha256: str
    quality_passed: bool
    rx1_amplitude_counts: float
    transfer_detected: bool
    transfer_amplitude_ratio: float | None
    transfer_amplitude_upper_bound_ratio: float | None


@dataclass(frozen=True, slots=True)
class ScalarInterval:
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class InterventionPairResult:
    pair: str
    baseline_role: str
    intervention_role: str
    repeat_count_per_state: int
    intervention_uses_phase_free_upper_bounds: bool
    point_after_to_before_ratio: float
    point_improvement_db: float
    after_to_before_ratio_interval: ScalarInterval
    point_rx1_reference_change_db: float
    rx1_reference_change_db_interval: ScalarInterval


@dataclass(frozen=True, slots=True)
class InterventionSupportQualification:
    required_pairs: tuple[str, ...]
    confidence_level: float
    bootstrap_draw_count: int
    bootstrap_seed: int
    minimum_improvement_db: float
    maximum_after_to_before_ratio: float
    maximum_abs_rx1_reference_change_db: float
    pair_results: tuple[InterventionPairResult, ...]
    simultaneous_leakage_ratio_upper_bound: float | None
    simultaneous_rx1_abs_change_db_upper_bound: float | None
    joint_constraint_score_upper_bound: float | None
    simultaneous_improvement_gate_passed: bool
    rejection_reasons: tuple[str, ...]


_REPEAT_FIELDS = set(InterventionRepeat.__dataclass_fields__)


def _finite_positive(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InterventionSupportError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result) or result <= 0.0:
        raise InterventionSupportError(f"{label} must be finite and positive")
    return result


def _validate_repeat(value: InterventionRepeat, *, role: str) -> None:
    label = f"{role} repeat {value.repeat_index}"
    if isinstance(value.repeat_index, bool) or not isinstance(value.repeat_index, int):
        raise InterventionSupportError(f"{label} index is malformed")
    if (
        not isinstance(value.condition_id, str)
        or not value.condition_id
        or not isinstance(value.stream_id, str)
        or not value.stream_id
    ):
        raise InterventionSupportError(f"{label} source identity is missing")
    if not isinstance(value.quality_passed, bool) or not isinstance(value.transfer_detected, bool):
        raise InterventionSupportError(f"{label} quality/detection flags must be boolean")
    if (
        not isinstance(value.raw_iq_sha256, str)
        or len(value.raw_iq_sha256) != 64
        or any(character not in "0123456789abcdef" for character in value.raw_iq_sha256)
    ):
        raise InterventionSupportError(f"{label} raw-IQ SHA-256 is malformed")
    _finite_positive(value.rx1_amplitude_counts, f"{label} RX1 amplitude")
    if value.transfer_detected:
        _finite_positive(value.transfer_amplitude_ratio, f"{label} detected transfer")
        if value.transfer_amplitude_upper_bound_ratio is not None:
            raise InterventionSupportError(
                f"{label} detected transfer must not substitute an upper bound"
            )
    else:
        if value.transfer_amplitude_ratio is not None:
            raise InterventionSupportError(
                f"{label} nondetection must not claim a complex transfer amplitude"
            )
        _finite_positive(
            value.transfer_amplitude_upper_bound_ratio,
            f"{label} phase-free transfer upper bound",
        )


def intervention_repeat_from_document(value: object, *, role: str) -> InterventionRepeat:
    """Strictly reconstruct one repeat emitted by the file-facing analyzer."""

    if not isinstance(value, Mapping) or set(value) != _REPEAT_FIELDS:
        raise InterventionSupportError(f"{role} repeat fields are incomplete or unexpected")
    repeat = InterventionRepeat(
        repeat_index=value["repeat_index"],
        condition_id=value["condition_id"],
        stream_id=value["stream_id"],
        raw_iq_sha256=value["raw_iq_sha256"],
        quality_passed=value["quality_passed"],
        rx1_amplitude_counts=value["rx1_amplitude_counts"],
        transfer_detected=value["transfer_detected"],
        transfer_amplitude_ratio=value["transfer_amplitude_ratio"],
        transfer_amplitude_upper_bound_ratio=value["transfer_amplitude_upper_bound_ratio"],
    )
    _validate_repeat(repeat, role=role)
    return repeat


def _quantile(values: npt.NDArray[np.float64], probability: float) -> float:
    return float(np.quantile(values, probability, method="higher"))


def _interval(values: npt.NDArray[np.float64], *, confidence_level: float) -> ScalarInterval:
    tail = (1.0 - confidence_level) / 2.0
    return ScalarInterval(lower=_quantile(values, tail), upper=_quantile(values, 1.0 - tail))


def _detected_transfer_ratio(value: InterventionRepeat) -> float:
    ratio = value.transfer_amplitude_ratio
    if not value.transfer_detected or ratio is None:
        raise InterventionSupportError("baseline transfer must be detected")
    return ratio


def _effective_intervention_transfer_ratio(value: InterventionRepeat) -> float:
    ratio = (
        value.transfer_amplitude_ratio
        if value.transfer_detected
        else value.transfer_amplitude_upper_bound_ratio
    )
    if ratio is None:
        raise InterventionSupportError("intervention transfer evidence is incomplete")
    return ratio


def _bootstrap_pair(
    *,
    pair: str,
    baseline_role: str,
    intervention_role: str,
    baseline: Sequence[InterventionRepeat],
    intervention: Sequence[InterventionRepeat],
    draw_count: int,
    confidence_level: float,
    rng: np.random.Generator,
) -> tuple[InterventionPairResult, npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    before_transfer = np.asarray(
        [_detected_transfer_ratio(item) for item in baseline], dtype=np.float64
    )
    after_transfer = np.asarray(
        [_effective_intervention_transfer_ratio(item) for item in intervention],
        dtype=np.float64,
    )
    before_rx1 = np.asarray([item.rx1_amplitude_counts for item in baseline], dtype=np.float64)
    after_rx1 = np.asarray([item.rx1_amplitude_counts for item in intervention], dtype=np.float64)
    before_indices = rng.integers(0, len(baseline), size=(draw_count, len(baseline)))
    after_indices = rng.integers(0, len(intervention), size=(draw_count, len(intervention)))
    before_transfer_draws = np.median(before_transfer[before_indices], axis=1)
    after_transfer_draws = np.median(after_transfer[after_indices], axis=1)
    ratio_draws = np.asarray(
        after_transfer_draws / before_transfer_draws,
        dtype=np.float64,
    )
    before_rx1_draws = np.median(before_rx1[before_indices], axis=1)
    after_rx1_draws = np.median(after_rx1[after_indices], axis=1)
    rx1_db_draws = np.asarray(20.0 * np.log10(after_rx1_draws / before_rx1_draws), dtype=np.float64)

    point_ratio = float(np.median(after_transfer) / np.median(before_transfer))
    point_rx1_db = float(20.0 * log10(float(np.median(after_rx1) / np.median(before_rx1))))
    return (
        InterventionPairResult(
            pair=pair,
            baseline_role=baseline_role,
            intervention_role=intervention_role,
            repeat_count_per_state=len(baseline),
            intervention_uses_phase_free_upper_bounds=any(
                not item.transfer_detected for item in intervention
            ),
            point_after_to_before_ratio=point_ratio,
            point_improvement_db=float(-20.0 * log10(point_ratio)),
            after_to_before_ratio_interval=_interval(
                ratio_draws, confidence_level=confidence_level
            ),
            point_rx1_reference_change_db=point_rx1_db,
            rx1_reference_change_db_interval=_interval(
                rx1_db_draws, confidence_level=confidence_level
            ),
        ),
        ratio_draws,
        rx1_db_draws,
    )


def qualify_intervention_support(
    cohorts: Mapping[str, Sequence[InterventionRepeat]],
    *,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    bootstrap_draw_count: int = DEFAULT_BOOTSTRAP_DRAW_COUNT,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
    maximum_after_to_before_ratio: float = MAXIMUM_AFTER_TO_BEFORE_RATIO,
    maximum_abs_rx1_reference_change_db: float = MAXIMUM_ABS_RX1_REFERENCE_CHANGE_DB,
) -> InterventionSupportQualification:
    """Recompute the fail-closed joint intervention-improvement decision.

    The gate is the 95th percentile (or the requested confidence level) of a
    joint bootstrap maximum.  A draw's score is the largest normalized leakage
    ratio or absolute RX1-reference shift across every required X pair.  Thus a
    score upper bound no greater than one proves every constraint
    simultaneously, rather than applying several uncorrected marginal tests.
    """

    observed_roles = tuple(cohorts)
    observed_set = set(observed_roles)
    ordered_roles: tuple[str, ...]
    required_pairs: tuple[str, ...]
    if observed_set == set(FULL_ROLE_ORDER) and len(observed_roles) == len(FULL_ROLE_ORDER):
        ordered_roles = FULL_ROLE_ORDER
        required_pairs = ("boundary", "full_fixture")
    elif observed_set == set(FULL_FIXTURE_ROLE_ORDER) and len(observed_roles) == len(
        FULL_FIXTURE_ROLE_ORDER
    ):
        ordered_roles = FULL_FIXTURE_ROLE_ORDER
        required_pairs = ("full_fixture",)
    else:
        raise InterventionSupportError(
            "cohorts must contain exactly the four boundary/full-fixture roles or the two "
            "full-fixture roles"
        )
    if not 0.5 < confidence_level < 1.0:
        raise InterventionSupportError("confidence level must be within (0.5, 1)")
    if (
        isinstance(bootstrap_draw_count, bool)
        or not isinstance(bootstrap_draw_count, int)
        or bootstrap_draw_count < 1_000
    ):
        raise InterventionSupportError("bootstrap draw count must be an integer of at least 1000")
    if (
        isinstance(bootstrap_seed, bool)
        or not isinstance(bootstrap_seed, int)
        or bootstrap_seed < 0
    ):
        raise InterventionSupportError("bootstrap seed must be a nonnegative integer")
    leakage_limit = _finite_positive(maximum_after_to_before_ratio, "maximum after-to-before ratio")
    rx1_limit = _finite_positive(
        maximum_abs_rx1_reference_change_db,
        "maximum absolute RX1 reference change",
    )
    if leakage_limit >= 1.0:
        raise InterventionSupportError("maximum after-to-before ratio must be below one")

    normalized = {role: tuple(cohorts[role]) for role in ordered_roles}
    all_stream_ids: list[str] = []
    all_raw_hashes: list[str] = []
    reasons: list[str] = []
    for role in ordered_roles:
        repeats = normalized[role]
        for repeat in repeats:
            _validate_repeat(repeat, role=role)
            all_stream_ids.append(repeat.stream_id)
            all_raw_hashes.append(repeat.raw_iq_sha256)
        indices = sorted(item.repeat_index for item in repeats)
        if len(repeats) != ATTRIBUTION_REPEAT_COUNT or indices != list(
            range(1, ATTRIBUTION_REPEAT_COUNT + 1)
        ):
            reasons.append(f"{role}_requires_exactly_five_indexed_repeats")
        if any(not item.quality_passed for item in repeats):
            reasons.append(f"{role}_contains_measurement_quality_failure")
    if len(set(all_stream_ids)) != len(all_stream_ids):
        raise InterventionSupportError("intervention cohorts reuse an ABI-2 stream ID")
    if len(set(all_raw_hashes)) != len(all_raw_hashes):
        raise InterventionSupportError("intervention cohorts reuse raw-IQ bytes")

    for pair in required_pairs:
        baseline_role, _ = ROLE_PAIRS[pair]
        if any(not item.transfer_detected for item in normalized[baseline_role]):
            reasons.append(f"{pair}_baseline_nondetection_prevents_improvement_proof")

    if reasons:
        return InterventionSupportQualification(
            required_pairs=required_pairs,
            confidence_level=confidence_level,
            bootstrap_draw_count=bootstrap_draw_count,
            bootstrap_seed=bootstrap_seed,
            minimum_improvement_db=float(-20.0 * log10(leakage_limit)),
            maximum_after_to_before_ratio=leakage_limit,
            maximum_abs_rx1_reference_change_db=rx1_limit,
            pair_results=(),
            simultaneous_leakage_ratio_upper_bound=None,
            simultaneous_rx1_abs_change_db_upper_bound=None,
            joint_constraint_score_upper_bound=None,
            simultaneous_improvement_gate_passed=False,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )

    rng = np.random.default_rng(bootstrap_seed)
    pair_results: list[InterventionPairResult] = []
    ratio_draw_sets: list[npt.NDArray[np.float64]] = []
    rx1_draw_sets: list[npt.NDArray[np.float64]] = []
    for pair in required_pairs:
        baseline_role, intervention_role = ROLE_PAIRS[pair]
        result, ratio_draws, rx1_db_draws = _bootstrap_pair(
            pair=pair,
            baseline_role=baseline_role,
            intervention_role=intervention_role,
            baseline=normalized[baseline_role],
            intervention=normalized[intervention_role],
            draw_count=bootstrap_draw_count,
            confidence_level=confidence_level,
            rng=rng,
        )
        pair_results.append(result)
        ratio_draw_sets.append(ratio_draws)
        rx1_draw_sets.append(rx1_db_draws)

    maximum_ratio_draws = np.max(np.vstack(ratio_draw_sets), axis=0)
    maximum_abs_rx1_draws = np.max(np.abs(np.vstack(rx1_draw_sets)), axis=0)
    joint_score_draws = np.maximum(
        maximum_ratio_draws / leakage_limit,
        maximum_abs_rx1_draws / rx1_limit,
    )
    simultaneous_ratio_upper = _quantile(maximum_ratio_draws, confidence_level)
    simultaneous_rx1_upper = _quantile(maximum_abs_rx1_draws, confidence_level)
    joint_upper = _quantile(joint_score_draws, confidence_level)
    if simultaneous_ratio_upper > leakage_limit:
        reasons.append("simultaneous_three_db_leakage_improvement_not_proven")
    if simultaneous_rx1_upper > rx1_limit:
        reasons.append("simultaneous_rx1_reference_stability_not_proven")
    if joint_upper > 1.0 and not reasons:
        reasons.append("joint_simultaneous_confidence_gate_not_proven")
    return InterventionSupportQualification(
        required_pairs=required_pairs,
        confidence_level=confidence_level,
        bootstrap_draw_count=bootstrap_draw_count,
        bootstrap_seed=bootstrap_seed,
        minimum_improvement_db=float(-20.0 * log10(leakage_limit)),
        maximum_after_to_before_ratio=leakage_limit,
        maximum_abs_rx1_reference_change_db=rx1_limit,
        pair_results=tuple(pair_results),
        simultaneous_leakage_ratio_upper_bound=simultaneous_ratio_upper,
        simultaneous_rx1_abs_change_db_upper_bound=simultaneous_rx1_upper,
        joint_constraint_score_upper_bound=joint_upper,
        simultaneous_improvement_gate_passed=not reasons and joint_upper <= 1.0,
        rejection_reasons=tuple(reasons),
    )


__all__ = [
    "ATTRIBUTION_REPEAT_COUNT",
    "BOUNDARY_BASELINE",
    "BOUNDARY_INTERVENTION",
    "DEFAULT_BOOTSTRAP_DRAW_COUNT",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_CONFIDENCE_LEVEL",
    "FULL_FIXTURE_BASELINE",
    "FULL_FIXTURE_INTERVENTION",
    "FULL_FIXTURE_ROLE_ORDER",
    "FULL_ROLE_ORDER",
    "INTERVENTION_SUPPORT_ANALYSIS_KIND",
    "INTERVENTION_SUPPORT_RESULT_KIND",
    "InterventionPairResult",
    "InterventionRepeat",
    "InterventionSupportError",
    "InterventionSupportQualification",
    "MAXIMUM_ABS_RX1_REFERENCE_CHANGE_DB",
    "MAXIMUM_AFTER_TO_BEFORE_RATIO",
    "MINIMUM_IMPROVEMENT_DB",
    "ROLE_PAIRS",
    "ScalarInterval",
    "intervention_repeat_from_document",
    "qualify_intervention_support",
]
