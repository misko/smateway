"""Pure, fail-closed attribution of repeated 5.8 GHz topology stages.

The hardware runners retain one transfer observation per attribution repeat.  This
module consumes already verified observations; it does not discover artifacts,
read files, or touch hardware.  Stages A, B, C, and a contemporaneous Stage-E
reference must each provide exactly five quality-passed, independently identified
repeats with equal comparison provenance and shared-fixture identity.

Detected transfers retain their complex phasor.  A nondetection is represented
only by a magnitude upper bound: it never acquires a synthetic zero phasor or
phase.  Stages are separate rewired runs, so boundary increments and
candidate-to-E comparisons use independent-sample complex bootstrap
distributions.  Their robust centers are subtracted as vectors so equal
magnitudes with different phases cannot hide a material increment.  Repeat-index
alignment is never used for inference; the explicitly algebraic telescoping
identity operates only on robust point centers and reuses independent boundary
uncertainties.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from itertools import product
from math import asin, atan2, degrees, isfinite, log10
from numbers import Complex, Integral, Real
from typing import Any

import numpy as np
import numpy.typing as npt

STAGE_ORDER = ("A", "B", "C", "E")
ATTRIBUTION_REPEAT_COUNT = 5

OPERATIONAL_LOW_RATIO = 0.011918
METROLOGY_LOW_RATIO = 0.002080
CONDITIONED_EXCESS_DISCRIMINATOR_RATIO = 0.028764
FULL_MAGNITUDE_TOLERANCE_DB = 0.2
FULL_PHASE_TOLERANCE_DEG = 2.0
FULL_COMPLEX_RESIDUAL_FRACTION = 0.0423
BOOTSTRAP_CONFIDENCE = 0.95
INDEPENDENT_BOOTSTRAP_DRAW_COUNT = 32_768

_EPSILON = 1e-12


class LeakageAttributionError(ValueError):
    """The supplied evidence cannot support the staged attribution contract."""


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise LeakageAttributionError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise LeakageAttributionError(f"{label} must be finite")
    return result


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeakageAttributionError(f"{label} must be a nonempty string")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LeakageAttributionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _json_value(value: object, label: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise LeakageAttributionError(f"{label} keys must be nonempty strings")
            normalized[raw_key] = _json_value(item, label)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, label) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return _finite_float(value, label)
    raise LeakageAttributionError(f"{label} must contain only finite JSON values")


def _identity(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise LeakageAttributionError(f"{label} must be a nonempty object")
    normalized = _json_value(value, label)
    if not isinstance(normalized, dict):  # pragma: no cover - root Mapping guarantees this.
        raise LeakageAttributionError(f"{label} must normalize to an object")
    return normalized


def _identity_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_complex(value: object, label: str) -> complex:
    if isinstance(value, bool) or not isinstance(value, Complex):
        raise LeakageAttributionError(f"{label} must be a complex number")
    result = complex(value)
    if not isfinite(result.real) or not isfinite(result.imag):
        raise LeakageAttributionError(f"{label} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class AttributionThresholds:
    """Frozen absolute and Stage-E-relative attribution thresholds."""

    operational_low_ratio: float = OPERATIONAL_LOW_RATIO
    metrology_low_ratio: float = METROLOGY_LOW_RATIO
    conditioned_excess_discriminator_ratio: float = CONDITIONED_EXCESS_DISCRIMINATOR_RATIO
    full_magnitude_tolerance_db: float = FULL_MAGNITUDE_TOLERANCE_DB
    full_phase_tolerance_deg: float = FULL_PHASE_TOLERANCE_DEG
    full_complex_residual_fraction: float = FULL_COMPLEX_RESIDUAL_FRACTION
    bootstrap_confidence: float = BOOTSTRAP_CONFIDENCE

    def __post_init__(self) -> None:
        positive = (
            (self.operational_low_ratio, "operational-low ratio"),
            (self.metrology_low_ratio, "metrology-low ratio"),
            (
                self.conditioned_excess_discriminator_ratio,
                "conditioned excess discriminator",
            ),
            (self.full_magnitude_tolerance_db, "full-magnitude tolerance"),
            (self.full_phase_tolerance_deg, "full-phase tolerance"),
            (self.full_complex_residual_fraction, "full-complex residual fraction"),
        )
        for value, label in positive:
            if _finite_float(value, label) <= 0.0:
                raise LeakageAttributionError(f"{label} must be positive")
        if self.metrology_low_ratio > self.operational_low_ratio:
            raise LeakageAttributionError(
                "metrology-low ratio must not exceed operational-low ratio"
            )
        if self.operational_low_ratio > self.conditioned_excess_discriminator_ratio:
            raise LeakageAttributionError(
                "operational-low ratio must not exceed conditioned excess discriminator"
            )
        confidence = _finite_float(self.bootstrap_confidence, "bootstrap confidence")
        if abs(confidence - BOOTSTRAP_CONFIDENCE) > _EPSILON:
            raise LeakageAttributionError("bootstrap confidence is frozen at 0.95")


DEFAULT_ATTRIBUTION_THRESHOLDS = AttributionThresholds()


@dataclass(frozen=True, slots=True)
class AttributionRepeat:
    """One verified attribution-gain observation from a unique ABI-2 stream."""

    repeat_index: int
    condition_id: str
    stream_id: int
    artifact_sha256: str
    quality_passed: bool
    detected: bool
    phasor: complex | None
    amplitude_upper_bound_ratio: float | None


@dataclass(frozen=True, slots=True)
class StageAttributionEvidence:
    """Five repeats for one physical topology stage."""

    stage: str
    run_id: str
    contemporaneous_group_id: str
    shared_fixture_identity: Mapping[str, Any]
    provenance_identity: Mapping[str, Any]
    stage_fixture_identity: Mapping[str, Any]
    repeats: Sequence[AttributionRepeat]


@dataclass(frozen=True, slots=True)
class ClosureComponentEvidence:
    """Five repeats of an independently measured increment above Stage C.

    A Stage-D one-hot caller should supply ``H_D,i - H_C`` as one component,
    rather than the raw ``H_D,i`` transfer.  Components must carry the same
    contemporaneous, shared-fixture, and comparison-provenance identities as
    A/B/C/E.  ``upstream_artifact_sha256s`` must enumerate every raw dependency
    used to derive the component.  The aggregator verifies those hashes are
    disjoint from all stage, derived-component, and other-component evidence;
    that establishes evidence-source disjointness, not stochastic independence
    under a common physical fixture.
    """

    name: str
    run_id: str
    contemporaneous_group_id: str
    shared_fixture_identity: Mapping[str, Any]
    provenance_identity: Mapping[str, Any]
    component_evidence_identity: Mapping[str, Any]
    upstream_artifact_sha256s: Sequence[str]
    repeats: Sequence[AttributionRepeat]


@dataclass(frozen=True, slots=True)
class ComplexAttributionEstimate:
    """Robust complex estimate or a phase-free conservative magnitude envelope."""

    repeat_count: int
    detected_repeat_count: int
    nondetected_repeat_count: int
    all_repeats_detected: bool
    center: complex | None
    magnitude: float | None
    phase_deg: float | None
    uncertainty_radius_95: float | None
    phase_uncertainty_95_deg: float | None
    conservative_magnitude_lower: float
    conservative_magnitude_upper: float
    magnitude_excludes_zero: bool
    interval_method: str
    nondetection_phase_synthesized: bool


@dataclass(frozen=True, slots=True)
class ReferenceEquivalence:
    """Independent-sample comparison with fresh repeated Stage E."""

    available: bool
    amplitude_error_db: float | None
    amplitude_error_simultaneous_95_interval_db: tuple[float, float] | None
    phase_error_deg: float | None
    phase_error_simultaneous_95_interval_deg: tuple[float, float] | None
    normalized_complex_residual: ComplexAttributionEstimate | None
    normalized_complex_residual_upper_simultaneous_95: float | None
    full_magnitude_equivalent: bool
    full_complex_equivalent: bool
    simultaneous_confidence: float
    per_metric_bonferroni_confidence: float
    statistical_method: str
    reason_unavailable: str | None


@dataclass(frozen=True, slots=True)
class AttributionAssessment:
    """Threshold flags and one fail-closed primary disposition."""

    name: str
    kind: str
    estimate: ComplexAttributionEstimate
    versus_stage_e: ReferenceEquivalence
    operational_low_supported: bool
    metrology_low_supported: bool
    conditioned_excess_supported: bool
    operational_low_interval_straddled: bool
    metrology_low_interval_straddled: bool
    conditioned_excess_interval_straddled: bool
    partial_material: bool
    small_detected: bool
    indeterminate: bool
    disposition: str
    fixture_identity_sha256: str | None


@dataclass(frozen=True, slots=True)
class BoundaryAttribution:
    """An independent two-sample, complex-vector topology increment."""

    name: str
    minuend_stage: str
    subtrahend_stage: str
    statistically_paired_repeats: bool
    complex_two_sample_difference_available: bool
    difference_method: str
    assessment: AttributionAssessment


@dataclass(frozen=True, slots=True)
class NamedEstimate:
    """A named vector term used by a decomposition or independent closure."""

    name: str
    estimate: ComplexAttributionEstimate


@dataclass(frozen=True, slots=True)
class ClosureComponentAttribution:
    """One closure component with auditable disjoint raw-evidence identity."""

    name: str
    run_id: str
    estimate: ComplexAttributionEstimate
    component_evidence_identity_sha256: str
    upstream_artifact_set_sha256: str
    evidence_source_disjointness_verified: bool


@dataclass(frozen=True, slots=True)
class CounterfactualMagnitude:
    """Predicted total after removing exactly one coherent term."""

    removed_term: str
    estimate: ComplexAttributionEstimate
    versus_stage_e: ReferenceEquivalence
    magnitude_change_from_stage_e: float | None
    magnitude_ratio_to_stage_e: float | None


@dataclass(frozen=True, slots=True)
class AlgebraicDecomposition:
    """A telescoping A + BA + CB + EC decomposition, never an independent closure."""

    available: bool
    algebraic_identity_only: bool
    independent_closure_claim: bool
    terms: tuple[NamedEstimate, ...]
    closure_residual: ComplexAttributionEstimate | None
    counterfactuals: tuple[CounterfactualMagnitude, ...]
    reason_unavailable: str | None


@dataclass(frozen=True, slots=True)
class IndependentClosure:
    """Stage-C plus independently measured components compared with Stage E."""

    component_count: int
    components: tuple[ClosureComponentAttribution, ...]
    predicted_stage_e: AttributionAssessment
    observed_minus_predicted: ComplexAttributionEstimate
    magnitude_closure_supported: bool
    complex_closure_supported: bool
    counterfactuals: tuple[CounterfactualMagnitude, ...]
    independent_of_algebraic_decomposition: bool
    evidence_source_disjointness_verified: bool
    stochastic_independence_proven: bool


@dataclass(frozen=True, slots=True)
class StagedAttributionSummary:
    """Complete hardware-free result for one contemporaneous A/B/C/E campaign."""

    contemporaneous_group_id: str
    stage_e_run_id: str
    shared_fixture_identity: Mapping[str, Any]
    shared_fixture_identity_sha256: str
    provenance_identity: Mapping[str, Any]
    provenance_identity_sha256: str
    contemporaneous_stage_e_reference_verified: bool
    all_five_repeats_per_stage_verified: bool
    thresholds: AttributionThresholds
    stages: tuple[AttributionAssessment, ...]
    boundaries: tuple[BoundaryAttribution, ...]
    algebraic_decomposition: AlgebraicDecomposition
    independent_closure: IndependentClosure | None

    def stage(self, name: str) -> AttributionAssessment:
        for assessment in self.stages:
            if assessment.name == name:
                return assessment
        raise KeyError(name)

    def boundary(self, name: str) -> BoundaryAttribution:
        for boundary in self.boundaries:
            if boundary.name == name:
                return boundary
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class _Sample:
    phasor: complex | None
    magnitude_lower: float
    magnitude_upper: float


@dataclass(frozen=True, slots=True)
class _Series:
    name: str
    samples: tuple[_Sample, ...]
    run_id: str | None
    fixture_identity_sha256: str | None
    evidence_identity_sha256: str | None
    upstream_artifact_set_sha256: str | None
    evidence_source_disjointness_verified: bool


@dataclass(frozen=True, slots=True)
class _BootstrapDistribution:
    point: complex
    draws: npt.NDArray[np.complex128]
    method: str


def _required_phasor(sample: _Sample) -> complex:
    if sample.phasor is None:  # pragma: no cover - guarded by each caller.
        raise LeakageAttributionError("internal complex operation received a nondetection")
    return sample.phasor


@cache
def _bootstrap_indices(count: int) -> npt.NDArray[np.int64]:
    return np.asarray(tuple(product(range(count), repeat=count)), dtype=np.int64)


def _complex_medoid_rows(
    values: npt.NDArray[np.complex128],
    sample_weights: npt.NDArray[np.float64] | None = None,
) -> npt.NDArray[np.complex128]:
    """Return rotation-equivariant robust sample medoids for complex rows."""

    if values.ndim != 2 or values.shape[1] < 1:
        raise LeakageAttributionError("internal complex-medoid array has invalid shape")
    weights_by_sample = (
        np.ones(values.shape, dtype=np.float64)
        if sample_weights is None
        else np.asarray(sample_weights, dtype=np.float64)
    )
    if weights_by_sample.shape != values.shape or np.any(weights_by_sample < 0.0):
        raise LeakageAttributionError("internal complex-medoid weights are invalid")
    total_weights = np.sum(weights_by_sample, axis=1)
    if np.any(total_weights <= 0.0):
        raise LeakageAttributionError("internal complex-medoid row has zero weight")
    pairwise_distances = np.asarray(
        np.abs(values[:, :, np.newaxis] - values[:, np.newaxis, :]),
        dtype=np.float64,
    )
    costs = np.sum(pairwise_distances * weights_by_sample[:, np.newaxis, :], axis=2)
    costs = np.where(weights_by_sample > 0.0, costs, np.inf)
    minimum_costs = np.min(costs, axis=1)
    tie_tolerances = 1e-12 * np.maximum(1.0, np.abs(minimum_costs))
    tied = costs <= minimum_costs[:, np.newaxis] + tie_tolerances[:, np.newaxis]
    tied_weights = np.where(tied, weights_by_sample, 0.0)
    tied_weight_sums = np.sum(tied_weights, axis=1)
    return np.asarray(
        np.sum(tied_weights * values, axis=1) / tied_weight_sums,
        dtype=np.complex128,
    )


@cache
def _bootstrap_count_patterns(
    count: int,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    indices = _bootstrap_indices(count)
    counts = np.stack(
        [np.count_nonzero(indices == item, axis=1) for item in range(count)],
        axis=1,
    )
    unique_counts, inverse = np.unique(counts, axis=0, return_inverse=True)
    return (
        np.asarray(unique_counts, dtype=np.float64),
        np.asarray(inverse, dtype=np.int64),
    )


def _complex_bootstrap_centers(
    values: npt.NDArray[np.complex128],
) -> npt.NDArray[np.complex128]:
    patterns, inverse = _bootstrap_count_patterns(int(values.size))
    rows = np.broadcast_to(values, patterns.shape)
    unique_centers = _complex_medoid_rows(rows, patterns)
    return np.asarray(unique_centers[inverse], dtype=np.complex128)


@cache
def _independent_draw_selector(distribution_size: int, lane: int) -> npt.NDArray[np.int64]:
    if distribution_size < 1 or lane < 0:
        raise LeakageAttributionError("internal bootstrap selector arguments are invalid")
    generator = np.random.default_rng(0x5A8_000 + lane * 104_729)
    return np.asarray(
        generator.integers(
            0,
            distribution_size,
            size=INDEPENDENT_BOOTSTRAP_DRAW_COUNT,
        ),
        dtype=np.int64,
    )


@cache
def _raw_bootstrap_distribution_cached(
    samples: tuple[_Sample, ...],
) -> _BootstrapDistribution:
    if not all(sample.phasor is not None for sample in samples):
        raise LeakageAttributionError("complex bootstrap requires detected phasors")
    values = np.asarray([_required_phasor(sample) for sample in samples], dtype=np.complex128)
    point = complex(_complex_medoid_rows(values[np.newaxis, :])[0])
    return _BootstrapDistribution(
        point=point,
        draws=_complex_bootstrap_centers(values),
        method=(
            "exhaustive_single_sample_rotation_and_permutation_equivariant_"
            "tie_averaged_complex_medoid_bootstrap"
        ),
    )


def _raw_bootstrap_distribution(samples: Sequence[_Sample]) -> _BootstrapDistribution:
    return _raw_bootstrap_distribution_cached(tuple(samples))


def _independent_linear_distribution(
    terms: Sequence[tuple[_BootstrapDistribution, complex]],
    *,
    lane_offset: int = 0,
) -> _BootstrapDistribution:
    if not terms:
        raise LeakageAttributionError("independent bootstrap requires at least one term")
    point = sum((coefficient * distribution.point for distribution, coefficient in terms), 0j)
    draws = np.zeros(INDEPENDENT_BOOTSTRAP_DRAW_COUNT, dtype=np.complex128)
    for lane, (distribution, coefficient) in enumerate(terms, start=lane_offset):
        selector = _independent_draw_selector(int(distribution.draws.size), lane)
        draws += coefficient * distribution.draws[selector]
    return _BootstrapDistribution(
        point=complex(point),
        draws=draws,
        method="deterministic_independent_multisample_complex_bootstrap",
    )


def _estimate_from_distribution(
    distribution: _BootstrapDistribution,
    *,
    thresholds: AttributionThresholds,
    repeat_count: int = ATTRIBUTION_REPEAT_COUNT,
) -> ComplexAttributionEstimate:
    distances = np.asarray(np.abs(distribution.draws - distribution.point), dtype=np.float64)
    radius = float(np.quantile(distances, thresholds.bootstrap_confidence, method="higher"))
    magnitude = abs(distribution.point)
    lower = max(0.0, magnitude - radius)
    upper = magnitude + radius
    phase = (
        None
        if magnitude <= _EPSILON
        else degrees(atan2(distribution.point.imag, distribution.point.real))
    )
    phase_uncertainty = (
        None
        if magnitude <= _EPSILON or radius >= magnitude
        else degrees(asin(min(1.0, radius / magnitude)))
    )
    return ComplexAttributionEstimate(
        repeat_count=repeat_count,
        detected_repeat_count=repeat_count,
        nondetected_repeat_count=0,
        all_repeats_detected=True,
        center=distribution.point,
        magnitude=magnitude,
        phase_deg=phase,
        uncertainty_radius_95=radius,
        phase_uncertainty_95_deg=phase_uncertainty,
        conservative_magnitude_lower=lower,
        conservative_magnitude_upper=upper,
        magnitude_excludes_zero=lower > _EPSILON,
        interval_method=f"{distribution.method}_95pct_disk",
        nondetection_phase_synthesized=False,
    )


def _algebraic_point_only_estimate(center: complex) -> ComplexAttributionEstimate:
    magnitude = abs(center)
    phase = None if magnitude <= _EPSILON else degrees(atan2(center.imag, center.real))
    return ComplexAttributionEstimate(
        repeat_count=ATTRIBUTION_REPEAT_COUNT,
        detected_repeat_count=ATTRIBUTION_REPEAT_COUNT,
        nondetected_repeat_count=0,
        all_repeats_detected=True,
        center=center,
        magnitude=magnitude,
        phase_deg=phase,
        uncertainty_radius_95=None,
        phase_uncertainty_95_deg=None,
        conservative_magnitude_lower=0.0,
        conservative_magnitude_upper=float("inf"),
        magnitude_excludes_zero=False,
        interval_method="algebraic_point_only_no_inferential_interval",
        nondetection_phase_synthesized=False,
    )


def _required_estimate_center(estimate: ComplexAttributionEstimate) -> complex:
    if estimate.center is None:
        raise LeakageAttributionError("algebraic term lacks a robust point center")
    return estimate.center


def _quantile_interval(
    values: npt.NDArray[np.float64],
    confidence: float,
) -> tuple[float, float]:
    tail = (1.0 - confidence) / 2.0
    lower = float(np.quantile(values, tail, method="lower"))
    upper = float(np.quantile(values, 1.0 - tail, method="higher"))
    return lower, upper


def _summarize_samples(
    samples: Sequence[_Sample],
    *,
    thresholds: AttributionThresholds,
) -> ComplexAttributionEstimate:
    if len(samples) != ATTRIBUTION_REPEAT_COUNT:
        raise LeakageAttributionError("internal attribution series does not contain five repeats")
    detected = [sample for sample in samples if sample.phasor is not None]
    all_detected = len(detected) == len(samples)
    if all_detected:
        return _estimate_from_distribution(
            _raw_bootstrap_distribution(samples),
            thresholds=thresholds,
            repeat_count=len(samples),
        )
    lower = min(sample.magnitude_lower for sample in samples)
    upper = max(sample.magnitude_upper for sample in samples)
    return ComplexAttributionEstimate(
        repeat_count=len(samples),
        detected_repeat_count=len(detected),
        nondetected_repeat_count=len(samples) - len(detected),
        all_repeats_detected=False,
        center=None,
        magnitude=None,
        phase_deg=None,
        uncertainty_radius_95=None,
        phase_uncertainty_95_deg=None,
        conservative_magnitude_lower=lower,
        conservative_magnitude_upper=upper,
        magnitude_excludes_zero=lower > _EPSILON,
        interval_method="phase_free_envelope_of_per_repeat_detection_bounds",
        nondetection_phase_synthesized=False,
    )


def _validate_repeat(
    value: AttributionRepeat,
    *,
    label: str,
) -> tuple[_Sample, tuple[str, int, str]]:
    if not isinstance(value, AttributionRepeat):
        raise LeakageAttributionError(f"{label} must contain AttributionRepeat objects")
    if (
        isinstance(value.repeat_index, bool)
        or not isinstance(value.repeat_index, int)
        or value.repeat_index not in range(1, ATTRIBUTION_REPEAT_COUNT + 1)
    ):
        raise LeakageAttributionError(f"{label} repeat index must be exactly 1..5")
    condition_id = _identifier(value.condition_id, f"{label} condition ID")
    if isinstance(value.stream_id, bool) or not isinstance(value.stream_id, int):
        raise LeakageAttributionError(f"{label} stream ID must be an integer")
    if value.stream_id < 1:
        raise LeakageAttributionError(f"{label} stream ID must be positive")
    artifact = _sha256(value.artifact_sha256, f"{label} artifact hash")
    if value.quality_passed is not True:
        raise LeakageAttributionError(f"{label} repeat failed measurement quality")
    if not isinstance(value.detected, bool):
        raise LeakageAttributionError(f"{label} detection flag must be boolean")
    if value.detected:
        if value.phasor is None:
            raise LeakageAttributionError(f"{label} detected repeat lacks a complex phasor")
        phasor = _finite_complex(value.phasor, f"{label} phasor")
        if abs(phasor) <= _EPSILON:
            raise LeakageAttributionError(f"{label} detected phasor must be nonzero")
        if value.amplitude_upper_bound_ratio is not None:
            raise LeakageAttributionError(
                f"{label} detected repeat must not substitute an amplitude upper bound"
            )
        sample = _Sample(phasor=phasor, magnitude_lower=abs(phasor), magnitude_upper=abs(phasor))
    else:
        if value.phasor is not None:
            raise LeakageAttributionError(
                f"{label} nondetection must not carry a synthetic phasor or phase"
            )
        upper = _finite_float(value.amplitude_upper_bound_ratio, f"{label} upper bound")
        if upper <= 0.0:
            raise LeakageAttributionError(f"{label} upper bound must be positive")
        sample = _Sample(phasor=None, magnitude_lower=0.0, magnitude_upper=upper)
    return sample, (condition_id, value.stream_id, artifact)


def _validate_repeat_series(
    repeats: Sequence[AttributionRepeat],
    *,
    label: str,
    global_condition_ids: set[str],
    global_stream_ids: set[int],
    global_artifact_hashes: set[str],
) -> tuple[_Sample, ...]:
    if isinstance(repeats, (str, bytes)) or len(repeats) != ATTRIBUTION_REPEAT_COUNT:
        raise LeakageAttributionError(f"{label} must contain exactly five attribution repeats")
    indexed: dict[int, _Sample] = {}
    for repeat in repeats:
        sample, (condition_id, stream_id, artifact_hash) = _validate_repeat(
            repeat,
            label=label,
        )
        if repeat.repeat_index in indexed:
            raise LeakageAttributionError(f"{label} contains a duplicate repeat index")
        if condition_id in global_condition_ids:
            raise LeakageAttributionError("attribution condition IDs must be globally unique")
        if stream_id in global_stream_ids:
            raise LeakageAttributionError("attribution ABI-2 stream IDs must be globally unique")
        if artifact_hash in global_artifact_hashes:
            raise LeakageAttributionError("attribution artifact hashes must be globally unique")
        indexed[repeat.repeat_index] = sample
        global_condition_ids.add(condition_id)
        global_stream_ids.add(stream_id)
        global_artifact_hashes.add(artifact_hash)
    if set(indexed) != set(range(1, ATTRIBUTION_REPEAT_COUNT + 1)):
        raise LeakageAttributionError(f"{label} repeat indices must be exactly 1..5")
    return tuple(indexed[index] for index in range(1, ATTRIBUTION_REPEAT_COUNT + 1))


def _independent_phase_free_difference(
    minuend: Sequence[_Sample],
    subtrahend: Sequence[_Sample],
    *,
    thresholds: AttributionThresholds,
) -> tuple[_Sample, ...]:
    left = _summarize_samples(minuend, thresholds=thresholds)
    right = _summarize_samples(subtrahend, thresholds=thresholds)
    lower = max(
        0.0,
        left.conservative_magnitude_lower - right.conservative_magnitude_upper,
        right.conservative_magnitude_lower - left.conservative_magnitude_upper,
    )
    upper = left.conservative_magnitude_upper + right.conservative_magnitude_upper
    return tuple(
        _Sample(phasor=None, magnitude_lower=lower, magnitude_upper=upper)
        for _ in range(ATTRIBUTION_REPEAT_COUNT)
    )


def _independent_phase_free_sum(
    series: Sequence[_Series],
    *,
    thresholds: AttributionThresholds,
) -> tuple[_Sample, ...]:
    if not series:
        raise LeakageAttributionError("cannot sum an empty independent component set")
    exact = [item for item in series if all(sample.phasor is not None for sample in item.samples)]
    bounded = [item for item in series if item not in exact]
    if exact:
        exact_distribution = _independent_linear_distribution(
            tuple((_raw_bootstrap_distribution(item.samples), 1.0 + 0.0j) for item in exact)
        )
        exact_estimate = _estimate_from_distribution(
            exact_distribution,
            thresholds=thresholds,
        )
        exact_lower = exact_estimate.conservative_magnitude_lower
        exact_upper = exact_estimate.conservative_magnitude_upper
    else:
        exact_lower = 0.0
        exact_upper = 0.0
    unknown_upper = sum(
        _summarize_samples(item.samples, thresholds=thresholds).conservative_magnitude_upper
        for item in bounded
    )
    lower = max(0.0, exact_lower - unknown_upper)
    upper = exact_upper + unknown_upper
    return tuple(
        _Sample(phasor=None, magnitude_lower=lower, magnitude_upper=upper)
        for _ in range(ATTRIBUTION_REPEAT_COUNT)
    )


def _unavailable_reference_equivalence(reason: str) -> ReferenceEquivalence:
    per_metric_confidence = 1.0 - (1.0 - BOOTSTRAP_CONFIDENCE) / 3.0
    return ReferenceEquivalence(
        available=False,
        amplitude_error_db=None,
        amplitude_error_simultaneous_95_interval_db=None,
        phase_error_deg=None,
        phase_error_simultaneous_95_interval_deg=None,
        normalized_complex_residual=None,
        normalized_complex_residual_upper_simultaneous_95=None,
        full_magnitude_equivalent=False,
        full_complex_equivalent=False,
        simultaneous_confidence=BOOTSTRAP_CONFIDENCE,
        per_metric_bonferroni_confidence=per_metric_confidence,
        statistical_method="unavailable_phase_free_candidate",
        reason_unavailable=reason,
    )


def _stage_e_identity_equivalence(
    *,
    thresholds: AttributionThresholds,
) -> ReferenceEquivalence:
    per_metric_confidence = 1.0 - (1.0 - thresholds.bootstrap_confidence) / 3.0
    zero_distribution = _BootstrapDistribution(
        point=0.0 + 0.0j,
        draws=np.zeros(INDEPENDENT_BOOTSTRAP_DRAW_COUNT, dtype=np.complex128),
        method="stage_e_identity_residual",
    )
    residual = _estimate_from_distribution(zero_distribution, thresholds=thresholds)
    return ReferenceEquivalence(
        available=True,
        amplitude_error_db=0.0,
        amplitude_error_simultaneous_95_interval_db=(0.0, 0.0),
        phase_error_deg=0.0,
        phase_error_simultaneous_95_interval_deg=(0.0, 0.0),
        normalized_complex_residual=residual,
        normalized_complex_residual_upper_simultaneous_95=0.0,
        full_magnitude_equivalent=True,
        full_complex_equivalent=True,
        simultaneous_confidence=thresholds.bootstrap_confidence,
        per_metric_bonferroni_confidence=per_metric_confidence,
        statistical_method="stage_e_reference_identity",
        reason_unavailable=None,
    )


def _reference_equivalence_from_joint_draws(
    *,
    candidate_point: complex,
    stage_e_point: complex,
    candidate_draws: npt.NDArray[np.complex128],
    reference_draws: npt.NDArray[np.complex128],
    thresholds: AttributionThresholds,
    statistical_method: str,
    residual_method: str,
) -> ReferenceEquivalence:
    if abs(stage_e_point) <= _EPSILON:
        raise LeakageAttributionError("internal Stage-E reference center is zero")
    if candidate_draws.shape != (INDEPENDENT_BOOTSTRAP_DRAW_COUNT,) or reference_draws.shape != (
        INDEPENDENT_BOOTSTRAP_DRAW_COUNT,
    ):
        raise LeakageAttributionError("internal reference comparison draw count is invalid")
    safe_reference = np.abs(reference_draws) > _EPSILON
    ratios = np.full(INDEPENDENT_BOOTSTRAP_DRAW_COUNT, complex(np.inf, 0.0))
    ratios[safe_reference] = candidate_draws[safe_reference] / reference_draws[safe_reference]
    point_ratio = candidate_point / stage_e_point
    # The three equivalence guards form one decision.  Give that joint decision
    # at least the frozen 95% coverage using a Bonferroni family-wise interval,
    # rather than silently treating three marginal 95% intervals as jointly 95%.
    per_metric_confidence = 1.0 - (1.0 - thresholds.bootstrap_confidence) / 3.0
    amplitude_point = (
        float("-inf") if abs(point_ratio) <= _EPSILON else 20.0 * log10(abs(point_ratio))
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        amplitude_errors = np.asarray(20.0 * np.log10(np.abs(ratios)), dtype=np.float64)
    amplitude_interval = _quantile_interval(
        amplitude_errors,
        per_metric_confidence,
    )
    safe_phase = safe_reference & (np.abs(candidate_draws) > _EPSILON)
    if abs(point_ratio) <= _EPSILON:
        phase_point, phase_interval = None, (-180.0, 180.0)
    else:
        # Undefined bootstrap phases consume probability mass instead of being
        # silently dropped.  Treat them as a worst-case 180-degree deviation,
        # then form a circular confidence ball around the robust point phase.
        phase_point = degrees(atan2(point_ratio.imag, point_ratio.real))
        phase_deviations = np.full(
            INDEPENDENT_BOOTSTRAP_DRAW_COUNT,
            180.0,
            dtype=np.float64,
        )
        phase_errors = np.asarray(
            np.degrees(np.angle(candidate_draws[safe_phase] / reference_draws[safe_phase])),
            dtype=np.float64,
        )
        phase_deviations[safe_phase] = np.abs(
            np.degrees(np.angle(np.exp(1j * np.radians(phase_errors - phase_point))))
        )
        phase_radius = float(np.quantile(phase_deviations, per_metric_confidence, method="higher"))
        phase_interval = (phase_point - phase_radius, phase_point + phase_radius)
    residual_draws = np.full(INDEPENDENT_BOOTSTRAP_DRAW_COUNT, complex(np.inf, 0.0))
    residual_draws[safe_reference] = (
        candidate_draws[safe_reference] - reference_draws[safe_reference]
    ) / reference_draws[safe_reference]
    residual_distribution = _BootstrapDistribution(
        point=(candidate_point - stage_e_point) / stage_e_point,
        draws=residual_draws,
        method=residual_method,
    )
    residual = _estimate_from_distribution(
        residual_distribution,
        thresholds=thresholds,
    )
    residual_distances = np.asarray(
        np.abs(residual_distribution.draws - residual_distribution.point),
        dtype=np.float64,
    )
    simultaneous_residual_radius = float(
        np.quantile(residual_distances, per_metric_confidence, method="higher")
    )
    residual_upper = abs(residual_distribution.point) + simultaneous_residual_radius
    magnitude_equivalent = (
        amplitude_interval[0] >= -thresholds.full_magnitude_tolerance_db - _EPSILON
        and amplitude_interval[1] <= thresholds.full_magnitude_tolerance_db + _EPSILON
    )
    phase_equivalent = (
        phase_point is not None
        and phase_interval[0] >= -thresholds.full_phase_tolerance_deg - _EPSILON
        and phase_interval[1] <= thresholds.full_phase_tolerance_deg + _EPSILON
    )
    complex_equivalent = (
        magnitude_equivalent
        and phase_equivalent
        and residual_upper <= thresholds.full_complex_residual_fraction + _EPSILON
    )
    return ReferenceEquivalence(
        available=True,
        amplitude_error_db=amplitude_point,
        amplitude_error_simultaneous_95_interval_db=amplitude_interval,
        phase_error_deg=phase_point,
        phase_error_simultaneous_95_interval_deg=phase_interval,
        normalized_complex_residual=residual,
        normalized_complex_residual_upper_simultaneous_95=residual_upper,
        full_magnitude_equivalent=magnitude_equivalent,
        full_complex_equivalent=complex_equivalent,
        simultaneous_confidence=thresholds.bootstrap_confidence,
        per_metric_bonferroni_confidence=per_metric_confidence,
        statistical_method=statistical_method,
        reason_unavailable=None,
    )


def _reference_equivalence_from_distributions(
    candidate: _BootstrapDistribution,
    stage_e: _BootstrapDistribution,
    *,
    thresholds: AttributionThresholds,
) -> ReferenceEquivalence:
    candidate_selector = _independent_draw_selector(int(candidate.draws.size), 10_000)
    reference_selector = _independent_draw_selector(int(stage_e.draws.size), 20_000)
    return _reference_equivalence_from_joint_draws(
        candidate_point=candidate.point,
        stage_e_point=stage_e.point,
        candidate_draws=candidate.draws[candidate_selector],
        reference_draws=stage_e.draws[reference_selector],
        thresholds=thresholds,
        statistical_method=(
            "deterministic_independent_two_sample_bonferroni_simultaneous_complex_bootstrap"
        ),
        residual_method="independent_candidate_vs_stage_e_normalized_residual_bootstrap",
    )


def _reference_equivalence(
    candidate: Sequence[_Sample],
    stage_e: Sequence[_Sample],
    *,
    thresholds: AttributionThresholds,
) -> ReferenceEquivalence:
    if not all(sample.phasor is not None for sample in candidate):
        return _unavailable_reference_equivalence(
            "candidate_has_one_or_more_phase-free_nondetections"
        )
    if not all(sample.phasor is not None for sample in stage_e):
        raise LeakageAttributionError("internal Stage-E reference lacks detected complex phasors")
    return _reference_equivalence_from_distributions(
        _raw_bootstrap_distribution(candidate),
        _raw_bootstrap_distribution(stage_e),
        thresholds=thresholds,
    )


def _assessment_from_estimate(
    name: str,
    kind: str,
    estimate: ComplexAttributionEstimate,
    comparison: ReferenceEquivalence,
    *,
    thresholds: AttributionThresholds,
    fixture_identity_sha256: str | None = None,
) -> AttributionAssessment:
    operational_low = (
        estimate.conservative_magnitude_upper <= thresholds.operational_low_ratio + _EPSILON
    )
    metrology_low = (
        estimate.conservative_magnitude_upper <= thresholds.metrology_low_ratio + _EPSILON
    )
    conditioned_excess = (
        estimate.conservative_magnitude_lower
        >= thresholds.conditioned_excess_discriminator_ratio - _EPSILON
    )
    operational_straddled = (
        estimate.conservative_magnitude_lower < thresholds.operational_low_ratio - _EPSILON
        and estimate.conservative_magnitude_upper > thresholds.operational_low_ratio + _EPSILON
    )
    metrology_straddled = (
        estimate.conservative_magnitude_lower < thresholds.metrology_low_ratio - _EPSILON
        and estimate.conservative_magnitude_upper > thresholds.metrology_low_ratio + _EPSILON
    )
    conditioned_excess_straddled = (
        estimate.conservative_magnitude_lower
        < thresholds.conditioned_excess_discriminator_ratio - _EPSILON
        and estimate.conservative_magnitude_upper
        > thresholds.conditioned_excess_discriminator_ratio + _EPSILON
    )
    any_threshold_straddled = (
        operational_straddled or metrology_straddled or conditioned_excess_straddled
    )
    partial_material = (
        estimate.conservative_magnitude_lower > thresholds.operational_low_ratio + _EPSILON
        and not comparison.full_magnitude_equivalent
    )
    small_detected = (
        estimate.all_repeats_detected
        and estimate.magnitude_excludes_zero
        and estimate.conservative_magnitude_upper <= thresholds.operational_low_ratio + _EPSILON
        and not comparison.full_magnitude_equivalent
        and not any_threshold_straddled
    )
    if any_threshold_straddled:
        disposition = "indeterminate"
    elif comparison.full_complex_equivalent:
        disposition = "full_complex_equivalent"
    elif comparison.full_magnitude_equivalent:
        disposition = "full_magnitude_only"
    elif partial_material:
        disposition = "partial_material"
    elif small_detected:
        disposition = "small_detected"
    elif metrology_low:
        disposition = "metrology_low"
    elif operational_low:
        disposition = "operational_low"
    else:
        disposition = "indeterminate"
    return AttributionAssessment(
        name=name,
        kind=kind,
        estimate=estimate,
        versus_stage_e=comparison,
        operational_low_supported=operational_low,
        metrology_low_supported=metrology_low,
        conditioned_excess_supported=conditioned_excess,
        operational_low_interval_straddled=operational_straddled,
        metrology_low_interval_straddled=metrology_straddled,
        conditioned_excess_interval_straddled=conditioned_excess_straddled,
        partial_material=partial_material,
        small_detected=small_detected,
        indeterminate=disposition == "indeterminate",
        disposition=disposition,
        fixture_identity_sha256=fixture_identity_sha256,
    )


def _algebraic_counterfactuals(
    terms: Sequence[NamedEstimate],
    stage_e: ComplexAttributionEstimate,
) -> tuple[CounterfactualMagnitude, ...]:
    if stage_e.center is None or stage_e.magnitude is None or stage_e.magnitude <= _EPSILON:
        raise LeakageAttributionError("Stage-E reference magnitude is unavailable")
    results: list[CounterfactualMagnitude] = []
    for removed in terms:
        center = sum(
            (
                _required_estimate_center(term.estimate)
                for term in terms
                if term.name != removed.name
            ),
            start=0j,
        )
        estimate = _algebraic_point_only_estimate(center)
        magnitude = abs(center)
        results.append(
            CounterfactualMagnitude(
                removed_term=removed.name,
                estimate=estimate,
                versus_stage_e=_unavailable_reference_equivalence(
                    "algebraic_counterfactual_is_not_independently_measured"
                ),
                magnitude_change_from_stage_e=magnitude - stage_e.magnitude,
                magnitude_ratio_to_stage_e=magnitude / stage_e.magnitude,
            )
        )
    return tuple(results)


def _independent_counterfactuals(
    terms: Sequence[_Series],
    stage_e: _Series,
    *,
    thresholds: AttributionThresholds,
) -> tuple[CounterfactualMagnitude, ...]:
    reference = _summarize_samples(stage_e.samples, thresholds=thresholds)
    if reference.magnitude is None or reference.magnitude <= _EPSILON:
        raise LeakageAttributionError("Stage-E reference magnitude is unavailable")
    stage_e_distribution = _raw_bootstrap_distribution(stage_e.samples)
    results: list[CounterfactualMagnitude] = []
    for removed in terms:
        retained = [term for term in terms if term.name != removed.name]
        if not retained:
            raise LeakageAttributionError("independent counterfactual removed every term")
        if all(sample.phasor is not None for term in retained for sample in term.samples):
            distribution = _independent_linear_distribution(
                tuple((_raw_bootstrap_distribution(term.samples), 1.0 + 0.0j) for term in retained)
            )
            estimate = _estimate_from_distribution(distribution, thresholds=thresholds)
            comparison = _reference_equivalence_from_distributions(
                distribution,
                stage_e_distribution,
                thresholds=thresholds,
            )
        else:
            bounded = _independent_phase_free_sum(retained, thresholds=thresholds)
            estimate = _summarize_samples(bounded, thresholds=thresholds)
            comparison = _unavailable_reference_equivalence(
                "counterfactual_has_phase-free_component_nondetection"
            )
        results.append(
            CounterfactualMagnitude(
                removed_term=removed.name,
                estimate=estimate,
                versus_stage_e=comparison,
                magnitude_change_from_stage_e=(
                    None if estimate.magnitude is None else estimate.magnitude - reference.magnitude
                ),
                magnitude_ratio_to_stage_e=(
                    None if estimate.magnitude is None else estimate.magnitude / reference.magnitude
                ),
            )
        )
    return tuple(results)


def _algebraic_decomposition(
    stages: Mapping[str, _Series],
    boundaries: Mapping[str, ComplexAttributionEstimate],
    *,
    thresholds: AttributionThresholds,
) -> AlgebraicDecomposition:
    if not all(sample.phasor is not None for stage in stages.values() for sample in stage.samples):
        return AlgebraicDecomposition(
            available=False,
            algebraic_identity_only=True,
            independent_closure_claim=False,
            terms=(),
            closure_residual=None,
            counterfactuals=(),
            reason_unavailable="one_or_more_stages_have_phase-free_nondetections",
        )
    stage_a_estimate = _summarize_samples(stages["A"].samples, thresholds=thresholds)
    stage_e_estimate = _summarize_samples(stages["E"].samples, thresholds=thresholds)
    terms = (
        NamedEstimate(name="stage_a_common", estimate=stage_a_estimate),
        NamedEstimate(name="delta_b_minus_a", estimate=boundaries["B_MINUS_A"]),
        NamedEstimate(name="delta_c_minus_b", estimate=boundaries["C_MINUS_B"]),
        NamedEstimate(name="delta_e_minus_c", estimate=boundaries["E_MINUS_C"]),
    )
    predicted_center = sum(
        (_required_estimate_center(term.estimate) for term in terms),
        start=0j,
    )
    residual_center = predicted_center - _required_estimate_center(stage_e_estimate)
    return AlgebraicDecomposition(
        available=True,
        algebraic_identity_only=True,
        independent_closure_claim=False,
        terms=terms,
        closure_residual=_algebraic_point_only_estimate(residual_center),
        counterfactuals=_algebraic_counterfactuals(
            terms,
            stage_e_estimate,
        ),
        reason_unavailable=None,
    )


def _closure_component_attribution(
    component: _Series,
    *,
    thresholds: AttributionThresholds,
) -> ClosureComponentAttribution:
    if (
        component.run_id is None
        or component.evidence_identity_sha256 is None
        or component.upstream_artifact_set_sha256 is None
        or not component.evidence_source_disjointness_verified
    ):
        raise LeakageAttributionError("internal closure component lacks evidence attestation")
    return ClosureComponentAttribution(
        name=component.name,
        run_id=component.run_id,
        estimate=_summarize_samples(component.samples, thresholds=thresholds),
        component_evidence_identity_sha256=component.evidence_identity_sha256,
        upstream_artifact_set_sha256=component.upstream_artifact_set_sha256,
        evidence_source_disjointness_verified=True,
    )


def _independent_closure(
    stage_c: _Series,
    stage_e: _Series,
    components: Sequence[_Series],
    *,
    thresholds: AttributionThresholds,
) -> IndependentClosure:
    terms = (
        _Series(
            name="stage_c_baseline",
            samples=stage_c.samples,
            run_id=stage_c.run_id,
            fixture_identity_sha256=stage_c.fixture_identity_sha256,
            evidence_identity_sha256=None,
            upstream_artifact_set_sha256=None,
            evidence_source_disjointness_verified=False,
        ),
        *components,
    )
    stage_e_distribution = _raw_bootstrap_distribution(stage_e.samples)
    all_complex = all(sample.phasor is not None for term in terms for sample in term.samples)
    if all_complex:
        predicted_distribution = _independent_linear_distribution(
            tuple((_raw_bootstrap_distribution(term.samples), 1.0 + 0.0j) for term in terms)
        )
        predicted_estimate = _estimate_from_distribution(
            predicted_distribution,
            thresholds=thresholds,
        )
        predicted_comparison = _reference_equivalence_from_distributions(
            predicted_distribution,
            stage_e_distribution,
            thresholds=thresholds,
        )
        residual_distribution = _independent_linear_distribution(
            (
                (stage_e_distribution, 1.0 + 0.0j),
                (predicted_distribution, -1.0 + 0.0j),
            ),
            lane_offset=100,
        )
        observed_minus_predicted = _estimate_from_distribution(
            residual_distribution,
            thresholds=thresholds,
        )
    else:
        predicted_bounds = _independent_phase_free_sum(terms, thresholds=thresholds)
        predicted_estimate = _summarize_samples(predicted_bounds, thresholds=thresholds)
        predicted_comparison = _unavailable_reference_equivalence(
            "closure_prediction_has_phase-free_component_nondetection"
        )
        residual_bounds = _independent_phase_free_difference(
            stage_e.samples,
            predicted_bounds,
            thresholds=thresholds,
        )
        observed_minus_predicted = _summarize_samples(
            residual_bounds,
            thresholds=thresholds,
        )
    predicted_assessment = _assessment_from_estimate(
        "INDEPENDENT_STAGE_E_PREDICTION",
        "independent_multisample_closure_prediction",
        predicted_estimate,
        predicted_comparison,
        thresholds=thresholds,
    )
    return IndependentClosure(
        component_count=len(components),
        components=tuple(
            _closure_component_attribution(
                component,
                thresholds=thresholds,
            )
            for component in components
        ),
        predicted_stage_e=predicted_assessment,
        observed_minus_predicted=observed_minus_predicted,
        magnitude_closure_supported=(predicted_assessment.versus_stage_e.full_magnitude_equivalent),
        complex_closure_supported=(predicted_assessment.versus_stage_e.full_complex_equivalent),
        counterfactuals=_independent_counterfactuals(
            terms,
            stage_e,
            thresholds=thresholds,
        ),
        independent_of_algebraic_decomposition=True,
        evidence_source_disjointness_verified=True,
        stochastic_independence_proven=False,
    )


def summarize_staged_attribution(
    stage_evidence: Sequence[StageAttributionEvidence],
    *,
    closure_components: Sequence[ClosureComponentEvidence] = (),
    thresholds: AttributionThresholds = DEFAULT_ATTRIBUTION_THRESHOLDS,
) -> StagedAttributionSummary:
    """Aggregate one provenance-equal, contemporaneous A/B/C/E campaign.

    The caller is responsible for verifying files and constructing these inputs.
    This function deliberately rejects incomplete campaigns instead of combining
    a fresh stage with a historical Stage-E magnitude.
    """

    if not isinstance(thresholds, AttributionThresholds):
        raise LeakageAttributionError("thresholds must be AttributionThresholds")
    if isinstance(stage_evidence, (str, bytes)) or len(stage_evidence) != len(STAGE_ORDER):
        raise LeakageAttributionError("attribution requires exactly stages A, B, C, and E")

    global_condition_ids: set[str] = set()
    global_stream_ids: set[int] = set()
    global_artifact_hashes: set[str] = set()
    stages: dict[str, _Series] = {}
    normalized_shared: dict[str, dict[str, Any]] = {}
    normalized_provenance: dict[str, dict[str, Any]] = {}
    group_ids: dict[str, str] = {}
    run_ids: set[str] = set()

    for evidence in stage_evidence:
        if not isinstance(evidence, StageAttributionEvidence):
            raise LeakageAttributionError("stage evidence must use StageAttributionEvidence")
        if evidence.stage not in STAGE_ORDER:
            raise LeakageAttributionError("stage identity must be exactly A, B, C, or E")
        if evidence.stage in stages:
            raise LeakageAttributionError("each topology stage may appear only once")
        run_id = _identifier(evidence.run_id, f"Stage {evidence.stage} run ID")
        if run_id in run_ids:
            raise LeakageAttributionError("topology stages must use distinct run IDs")
        run_ids.add(run_id)
        group_ids[evidence.stage] = _identifier(
            evidence.contemporaneous_group_id,
            f"Stage {evidence.stage} contemporaneous group ID",
        )
        normalized_shared[evidence.stage] = _identity(
            evidence.shared_fixture_identity,
            f"Stage {evidence.stage} shared fixture identity",
        )
        normalized_provenance[evidence.stage] = _identity(
            evidence.provenance_identity,
            f"Stage {evidence.stage} comparison provenance",
        )
        stage_fixture = _identity(
            evidence.stage_fixture_identity,
            f"Stage {evidence.stage} fixture identity",
        )
        samples = _validate_repeat_series(
            evidence.repeats,
            label=f"Stage {evidence.stage}",
            global_condition_ids=global_condition_ids,
            global_stream_ids=global_stream_ids,
            global_artifact_hashes=global_artifact_hashes,
        )
        stages[evidence.stage] = _Series(
            name=evidence.stage,
            samples=samples,
            run_id=run_id,
            fixture_identity_sha256=_identity_sha256(stage_fixture),
            evidence_identity_sha256=None,
            upstream_artifact_set_sha256=None,
            evidence_source_disjointness_verified=False,
        )

    if set(stages) != set(STAGE_ORDER):
        raise LeakageAttributionError("attribution requires one instance of A, B, C, and E")
    if len(set(group_ids.values())) != 1:
        raise LeakageAttributionError(
            "all stages must name the same contemporaneous Stage-E comparison group"
        )
    shared_values = list(normalized_shared.values())
    if any(value != shared_values[0] for value in shared_values[1:]):
        raise LeakageAttributionError("shared fixture identity differs across topology stages")
    provenance_values = list(normalized_provenance.values())
    if any(value != provenance_values[0] for value in provenance_values[1:]):
        raise LeakageAttributionError("comparison provenance differs across topology stages")
    if not all(sample.phasor is not None for sample in stages["E"].samples):
        raise LeakageAttributionError(
            "contemporaneous Stage E must have five detected complex reference repeats"
        )
    stage_e_summary = _summarize_samples(stages["E"].samples, thresholds=thresholds)
    if not stage_e_summary.magnitude_excludes_zero:
        raise LeakageAttributionError("contemporaneous Stage-E reference uncertainty includes zero")

    stage_assessments_list: list[AttributionAssessment] = []
    for stage in STAGE_ORDER:
        estimate = _summarize_samples(stages[stage].samples, thresholds=thresholds)
        comparison = (
            _stage_e_identity_equivalence(thresholds=thresholds)
            if stage == "E"
            else _reference_equivalence(
                stages[stage].samples,
                stages["E"].samples,
                thresholds=thresholds,
            )
        )
        stage_assessments_list.append(
            _assessment_from_estimate(
                stage,
                "cumulative_stage",
                estimate,
                comparison,
                thresholds=thresholds,
                fixture_identity_sha256=stages[stage].fixture_identity_sha256,
            )
        )
    stage_assessments = tuple(stage_assessments_list)

    boundary_specs = (
        ("B_MINUS_A", "B", "A"),
        ("C_MINUS_B", "C", "B"),
        ("E_MINUS_C", "E", "C"),
    )
    boundaries: list[BoundaryAttribution] = []
    stage_e_distribution = _raw_bootstrap_distribution(stages["E"].samples)
    for name, minuend, subtrahend in boundary_specs:
        complex_available = all(
            sample.phasor is not None
            for stage in (stages[minuend], stages[subtrahend])
            for sample in stage.samples
        )
        if complex_available:
            minuend_distribution = _raw_bootstrap_distribution(stages[minuend].samples)
            subtrahend_distribution = _raw_bootstrap_distribution(stages[subtrahend].samples)
            if name == "E_MINUS_C":
                # The boundary estimate still uses independent E and C stage
                # samples, but its comparison denominator is the *same* Stage-E
                # evidence present in the numerator.  Retaining that covariance
                # avoids pretending E-C and E came from disjoint observations.
                reference_selector = _independent_draw_selector(
                    int(stage_e_distribution.draws.size),
                    30_000,
                )
                stage_c_selector = _independent_draw_selector(
                    int(subtrahend_distribution.draws.size),
                    40_000,
                )
                reference_draws = stage_e_distribution.draws[reference_selector]
                difference_draws = reference_draws - subtrahend_distribution.draws[stage_c_selector]
                difference_distribution = _BootstrapDistribution(
                    point=stage_e_distribution.point - subtrahend_distribution.point,
                    draws=np.asarray(difference_draws, dtype=np.complex128),
                    method=(
                        "independent_stage_e_minus_c_complex_bootstrap_with_"
                        "shared_reference_covariance"
                    ),
                )
                comparison = _reference_equivalence_from_joint_draws(
                    candidate_point=difference_distribution.point,
                    stage_e_point=stage_e_distribution.point,
                    candidate_draws=difference_distribution.draws,
                    reference_draws=reference_draws,
                    thresholds=thresholds,
                    statistical_method=(
                        "deterministic_shared_stage_e_covariance_bonferroni_"
                        "simultaneous_complex_bootstrap"
                    ),
                    residual_method=("shared_stage_e_covariance_normalized_residual_bootstrap"),
                )
                difference_method = (
                    "independent_stage_e_minus_c_complex_bootstrap_with_shared_reference_covariance"
                )
            else:
                difference_distribution = _independent_linear_distribution(
                    (
                        (minuend_distribution, 1.0 + 0.0j),
                        (subtrahend_distribution, -1.0 + 0.0j),
                    )
                )
                comparison = _reference_equivalence_from_distributions(
                    difference_distribution,
                    stage_e_distribution,
                    thresholds=thresholds,
                )
                difference_method = "independent_two_sample_complex_bootstrap_center_difference"
            estimate = _estimate_from_distribution(
                difference_distribution,
                thresholds=thresholds,
            )
        else:
            bounded_difference = _independent_phase_free_difference(
                stages[minuend].samples,
                stages[subtrahend].samples,
                thresholds=thresholds,
            )
            estimate = _summarize_samples(bounded_difference, thresholds=thresholds)
            comparison = _unavailable_reference_equivalence(
                "boundary_has_one_or_more_phase-free_stage_nondetections"
            )
            difference_method = "independent_two_sample_phase_free_triangle_bounds"
        boundaries.append(
            BoundaryAttribution(
                name=name,
                minuend_stage=minuend,
                subtrahend_stage=subtrahend,
                statistically_paired_repeats=False,
                complex_two_sample_difference_available=complex_available,
                difference_method=difference_method,
                assessment=_assessment_from_estimate(
                    name,
                    "independent_two_sample_boundary_increment",
                    estimate,
                    comparison,
                    thresholds=thresholds,
                ),
            )
        )

    components: list[_Series] = []
    component_names: set[str] = set()
    component_identity_hashes: set[str] = set()
    for component_evidence in closure_components:
        if not isinstance(component_evidence, ClosureComponentEvidence):
            raise LeakageAttributionError("closure components must use ClosureComponentEvidence")
        name = _identifier(component_evidence.name, "closure component name")
        if name == "stage_c_baseline" or name in component_names:
            raise LeakageAttributionError("closure component names must be unique and unreserved")
        component_names.add(name)
        component_run_id = _identifier(
            component_evidence.run_id,
            f"closure component {name} run ID",
        )
        if component_run_id in run_ids:
            raise LeakageAttributionError(
                "stage and closure-component run IDs must be globally unique"
            )
        run_ids.add(component_run_id)
        if component_evidence.contemporaneous_group_id != group_ids["E"]:
            raise LeakageAttributionError(
                "closure component is not in the contemporaneous Stage-E group"
            )
        if (
            _identity(component_evidence.shared_fixture_identity, f"{name} shared fixture")
            != (shared_values[0])
        ):
            raise LeakageAttributionError("closure component shared fixture identity differs")
        if (
            _identity(component_evidence.provenance_identity, f"{name} provenance")
            != (provenance_values[0])
        ):
            raise LeakageAttributionError("closure component comparison provenance differs")
        component_identity = _identity(
            component_evidence.component_evidence_identity,
            f"{name} evidence identity",
        )
        component_identity_sha256 = _identity_sha256(component_identity)
        if component_identity_sha256 in component_identity_hashes:
            raise LeakageAttributionError(
                "closure component evidence identities must be globally unique"
            )
        component_identity_hashes.add(component_identity_sha256)
        upstream_values = component_evidence.upstream_artifact_sha256s
        if (
            not isinstance(upstream_values, Sequence)
            or isinstance(upstream_values, (str, bytes))
            or len(upstream_values) < 1
        ):
            raise LeakageAttributionError(
                "closure component upstream artifact hashes must be nonempty"
            )
        upstream_artifacts: set[str] = set()
        for raw_hash in upstream_values:
            artifact_hash = _sha256(raw_hash, f"{name} upstream artifact hash")
            if artifact_hash in upstream_artifacts:
                raise LeakageAttributionError(
                    "closure component upstream artifact hashes must be locally unique"
                )
            if artifact_hash in global_artifact_hashes:
                raise LeakageAttributionError(
                    "closure upstream artifacts must be globally evidence-source disjoint"
                )
            upstream_artifacts.add(artifact_hash)
        global_artifact_hashes.update(upstream_artifacts)
        upstream_artifact_set_sha256 = _identity_sha256(
            {"upstream_artifact_sha256s": sorted(upstream_artifacts)}
        )
        samples = _validate_repeat_series(
            component_evidence.repeats,
            label=f"closure component {name}",
            global_condition_ids=global_condition_ids,
            global_stream_ids=global_stream_ids,
            global_artifact_hashes=global_artifact_hashes,
        )
        components.append(
            _Series(
                name=name,
                samples=samples,
                run_id=component_run_id,
                fixture_identity_sha256=None,
                evidence_identity_sha256=component_identity_sha256,
                upstream_artifact_set_sha256=upstream_artifact_set_sha256,
                evidence_source_disjointness_verified=True,
            )
        )
    components.sort(key=lambda item: item.name)

    shared = shared_values[0]
    provenance = provenance_values[0]
    return StagedAttributionSummary(
        contemporaneous_group_id=group_ids["E"],
        stage_e_run_id=str(stages["E"].run_id),
        shared_fixture_identity=shared,
        shared_fixture_identity_sha256=_identity_sha256(shared),
        provenance_identity=provenance,
        provenance_identity_sha256=_identity_sha256(provenance),
        contemporaneous_stage_e_reference_verified=True,
        all_five_repeats_per_stage_verified=True,
        thresholds=thresholds,
        stages=stage_assessments,
        boundaries=tuple(boundaries),
        algebraic_decomposition=_algebraic_decomposition(
            stages,
            {boundary.name: boundary.assessment.estimate for boundary in boundaries},
            thresholds=thresholds,
        ),
        independent_closure=(
            _independent_closure(
                stages["C"],
                stages["E"],
                components,
                thresholds=thresholds,
            )
            if components
            else None
        ),
    )


__all__ = [
    "ATTRIBUTION_REPEAT_COUNT",
    "BOOTSTRAP_CONFIDENCE",
    "CONDITIONED_EXCESS_DISCRIMINATOR_RATIO",
    "DEFAULT_ATTRIBUTION_THRESHOLDS",
    "FULL_COMPLEX_RESIDUAL_FRACTION",
    "FULL_MAGNITUDE_TOLERANCE_DB",
    "FULL_PHASE_TOLERANCE_DEG",
    "METROLOGY_LOW_RATIO",
    "OPERATIONAL_LOW_RATIO",
    "AlgebraicDecomposition",
    "AttributionAssessment",
    "AttributionRepeat",
    "AttributionThresholds",
    "BoundaryAttribution",
    "ClosureComponentAttribution",
    "ClosureComponentEvidence",
    "ComplexAttributionEstimate",
    "CounterfactualMagnitude",
    "IndependentClosure",
    "LeakageAttributionError",
    "NamedEstimate",
    "ReferenceEquivalence",
    "StageAttributionEvidence",
    "StagedAttributionSummary",
    "summarize_staged_attribution",
]
