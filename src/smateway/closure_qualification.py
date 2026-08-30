"""Pure closure qualification for the 5.8 GHz selector fixture.

This module deliberately has no file-system, radio, or capture dependencies.  It
consumes source-verified complex observations from a frozen campaign plan and
answers one narrow question: does independently acquired Stage-D evidence
predict the simultaneous-feed Stage-E transfer within the predeclared complex
equivalence limits?

Two excitation models are supported:

* ``arm_preserving`` uses ``H_C + sum(H_D2,i - H_C,i)``.  It remains diagnostic
  unless splitter multiport behaviour was characterized in the immutable plan.
* ``weighted`` uses ``H_C + sum(w_i * (H_D1,i - H_C,i))``.  Every weight repeat
  is one joint eight-arm vector; the bootstrap resamples whole rows so measured
  cross-arm covariance is never destroyed.

All cohorts contain exactly five source-disjoint repeats.  A full deterministic
joint bootstrap resamples global H_C, E, every dedicated C_i, D1_i, D2_i, and
the joint weight vectors.  A nondetection is retained only as a magnitude upper
bound and makes complex closure non-evaluable; zero phase is never synthesized.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import atan2, degrees, isfinite, log10
from numbers import Complex, Integral, Real
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

ARMS = tuple(f"ANT{index}" for index in range(1, 9))
REPEAT_COUNT = 5
METHODS = ("arm_preserving", "weighted")

PLAN_SCHEMA = "smateway.5g8.closure-plan/v1"
TOPOLOGY_SCHEMA = "smateway.5g8.closure-topology/v1"

MAGNITUDE_TOLERANCE_DB = 0.2
PHASE_TOLERANCE_DEG = 2.0
COMPLEX_RESIDUAL_LIMIT = 0.0423
SIMULTANEOUS_CONFIDENCE = 0.95
DEFAULT_BOOTSTRAP_DRAWS = 32_768

_EPSILON = 1e-15

ClosureMethod = Literal["arm_preserving", "weighted"]
Matrix = tuple[tuple[float, ...], ...]


class ClosureQualificationError(ValueError):
    """The supplied evidence violates the frozen closure contract."""


def _reject_json_constant(value: str) -> None:
    raise ClosureQualificationError(f"canonical identity contains invalid JSON constant {value}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ClosureQualificationError("identity must contain only finite JSON values") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ClosureQualificationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_commit(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ClosureQualificationError(f"{label} must be a full lowercase Git commit")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClosureQualificationError(f"{label} must be a nonempty string")
    return value


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ClosureQualificationError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise ClosureQualificationError(f"{label} must be finite")
    return result


def _finite_complex(value: object, label: str) -> complex:
    if isinstance(value, bool) or not isinstance(value, Complex):
        raise ClosureQualificationError(f"{label} must be complex")
    result = complex(value)
    if not isfinite(result.real) or not isfinite(result.imag):
        raise ClosureQualificationError(f"{label} must be finite")
    return result


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ClosureQualificationError(
            f"{label} keys differ from the frozen schema; missing={missing}, extra={extra}"
        )


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    """Immutable canonical JSON plus the SHA-256 that binds it."""

    canonical_json: str
    sha256: str

    def payload(self) -> dict[str, Any]:
        """Return a fresh parsed object after rechecking canonical form and hash."""

        _sha256(self.sha256, "identity hash")
        if not isinstance(self.canonical_json, str) or not self.canonical_json:
            raise ClosureQualificationError("canonical identity JSON must be nonempty")
        try:
            parsed = json.loads(self.canonical_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ClosureQualificationError) as error:
            raise ClosureQualificationError("canonical identity JSON is invalid") from error
        if not isinstance(parsed, dict) or not parsed:
            raise ClosureQualificationError("canonical identity must be a nonempty object")
        if _canonical_json(parsed) != self.canonical_json:
            raise ClosureQualificationError("identity JSON is not in canonical form")
        actual_hash = _sha256_bytes(self.canonical_json.encode("utf-8"))
        if actual_hash != self.sha256:
            raise ClosureQualificationError("canonical identity hash mismatch")
        return parsed


def make_canonical_identity(payload: Mapping[str, Any]) -> CanonicalIdentity:
    """Construct an immutable, self-verifying identity from a JSON object."""

    canonical = _canonical_json(payload)
    return CanonicalIdentity(canonical, _sha256_bytes(canonical.encode("utf-8")))


def leaf_source_set_sha256(leaf_source_sha256s: Sequence[str]) -> str:
    """Hash a canonical, nonempty, duplicate-free set of raw leaf sources."""

    if isinstance(leaf_source_sha256s, (str, bytes)) or not leaf_source_sha256s:
        raise ClosureQualificationError("leaf-source set must be nonempty")
    normalized = tuple(_sha256(value, "leaf-source hash") for value in leaf_source_sha256s)
    if len(set(normalized)) != len(normalized):
        raise ClosureQualificationError("leaf-source set contains a duplicate hash")
    if normalized != tuple(sorted(normalized)):
        raise ClosureQualificationError("leaf-source hashes must be in canonical sorted order")
    return _sha256_bytes(_canonical_json(normalized).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ComplexDetection:
    """A detected complex value or a phase-free magnitude upper bound."""

    detected: bool
    phasor: complex | None
    magnitude_upper_bound: float | None


@dataclass(frozen=True, slots=True)
class ClosureRepeat:
    """One source-distinct transfer observation."""

    repeat_index: int
    run_id: str
    condition_id: str
    stream_id: str
    artifact_sha256: str
    raw_iq_sha256: str
    metadata_sha256: str
    condition_record_sha256: str
    leaf_source_sha256s: tuple[str, ...]
    leaf_source_set_sha256: str
    plan_sha256: str
    topology_sha256: str
    source_commit: str
    quality_passed: bool
    value: ComplexDetection


@dataclass(frozen=True, slots=True)
class ClosureCohort:
    """Exactly five repeats for one frozen role/topology."""

    role: str
    arm: str | None
    plan_sha256: str
    source_commit: str
    topology_identity: CanonicalIdentity
    repeats: Sequence[ClosureRepeat]


@dataclass(frozen=True, slots=True)
class JointWeightVectorRepeat:
    """One jointly acquired ANT1..ANT8 excitation-weight vector."""

    repeat_index: int
    run_id: str
    condition_id: str
    stream_id: str
    artifact_sha256: str
    metadata_sha256: str
    condition_record_sha256: str
    leaf_source_sha256s: tuple[str, ...]
    leaf_source_set_sha256: str
    plan_sha256: str
    topology_sha256: str
    source_commit: str
    quality_passed: bool
    weights: Sequence[ComplexDetection]


@dataclass(frozen=True, slots=True)
class JointWeightVectorCohort:
    """Five complete vector repeats; per-arm repeat collections are forbidden."""

    role: str
    plan_sha256: str
    source_commit: str
    topology_identity: CanonicalIdentity
    repeats: Sequence[JointWeightVectorRepeat]


@dataclass(frozen=True, slots=True)
class ArmClosureEvidence:
    """Dedicated C_i, direct D1_i, and closure-excitation D2_i for one arm."""

    arm: str
    c_i: ClosureCohort
    d1_i: ClosureCohort
    d2_i: ClosureCohort


@dataclass(frozen=True, slots=True)
class ClosureCampaignEvidence:
    """Complete frozen evidence needed for one closure decision."""

    plan_identity: CanonicalIdentity
    global_h_c: ClosureCohort
    observed_e: ClosureCohort
    arms: Sequence[ArmClosureEvidence]
    joint_weights: JointWeightVectorCohort | None


@dataclass(frozen=True, slots=True)
class ClosureThresholds:
    """Predeclared familywise complex-equivalence limits."""

    magnitude_tolerance_db: float = MAGNITUDE_TOLERANCE_DB
    phase_tolerance_deg: float = PHASE_TOLERANCE_DEG
    complex_residual_limit: float = COMPLEX_RESIDUAL_LIMIT
    simultaneous_confidence: float = SIMULTANEOUS_CONFIDENCE

    def __post_init__(self) -> None:
        for value, label in (
            (self.magnitude_tolerance_db, "magnitude tolerance"),
            (self.phase_tolerance_deg, "phase tolerance"),
            (self.complex_residual_limit, "complex residual limit"),
        ):
            if _finite_float(value, label) <= 0.0:
                raise ClosureQualificationError(f"{label} must be positive")
        confidence = _finite_float(self.simultaneous_confidence, "simultaneous confidence")
        if not 0.0 < confidence < 1.0:
            raise ClosureQualificationError("simultaneous confidence must lie in (0, 1)")


DEFAULT_CLOSURE_THRESHOLDS = ClosureThresholds()


@dataclass(frozen=True, slots=True)
class ScalarInterval:
    """Point estimate and familywise simultaneous interval."""

    point: float
    simultaneous_95_interval: tuple[float, float]


@dataclass(frozen=True, slots=True)
class ComplexSummary:
    """Complex point estimate and real/imaginary bootstrap covariance."""

    center: complex
    magnitude: float
    phase_deg: float | None
    covariance_real_imag: Matrix


@dataclass(frozen=True, slots=True)
class SourceUncertainty:
    """Bootstrap covariance retained for one input cohort."""

    name: str
    covariance_real_imag: Matrix


@dataclass(frozen=True, slots=True)
class ArmClosureDiagnostic:
    """Per-arm primary increment and independent D1/D2 consistency check."""

    arm: str
    c_i_center: complex
    d1_i_center: complex
    d2_i_center: complex
    weight_center: complex | None
    primary_increment_center: complex
    independently_observed_increment_center: complex
    increment_residual_center: complex
    increment_residual_covariance_real_imag: Matrix
    c_i_topology_sha256: str
    d1_i_topology_sha256: str
    d2_i_topology_sha256: str


@dataclass(frozen=True, slots=True)
class ClosureQuality:
    """Three-metric, familywise-95% complex-equivalence decision."""

    amplitude_error_db: ScalarInterval
    phase_error_deg: ScalarInterval
    normalized_complex_residual: ScalarInterval
    magnitude_gate_passed: bool
    phase_gate_passed: bool
    residual_gate_passed: bool
    full_complex_equivalent: bool
    failure_reasons: tuple[str, ...]
    simultaneous_confidence: float
    per_metric_bonferroni_confidence: float
    statistical_method: str


@dataclass(frozen=True, slots=True)
class PhaseFreeClosureBound:
    """Conservative magnitude-only result when any phase is unavailable."""

    predicted_magnitude_upper_bound: float
    observed_e_magnitude_upper_bound: float
    residual_magnitude_upper_bound: float
    phase_available: bool
    zero_phase_synthesized: bool
    method: str


@dataclass(frozen=True, slots=True)
class NondetectionRecord:
    """Auditable evidence item that blocked complex closure."""

    label: str
    magnitude_upper_bound: float
    phase_synthesized: bool


@dataclass(frozen=True, slots=True)
class ClosureQualificationResult:
    """Complete evaluated or fail-closed closure result."""

    status: str
    method: ClosureMethod
    campaign_id: str
    plan_sha256: str
    fixture_graph_sha256: str
    reference_plane_sha256: str
    closure_authority: str
    splitter_multiport_characterized: bool
    splitter_multiport_characterization_sha256: str | None
    source_disjointness_verified: bool
    joint_weight_covariance_preserved: bool
    predicted_e: ComplexSummary | None
    observed_e: ComplexSummary | None
    observed_minus_predicted: ComplexSummary | None
    quality: ClosureQuality | None
    closure_claim_supported: bool
    arm_diagnostics: tuple[ArmClosureDiagnostic, ...]
    source_uncertainties: tuple[SourceUncertainty, ...]
    weight_vector_covariance_real_imag: Matrix | None
    primary_increment_vector_covariance_real_imag: Matrix | None
    d2_validation_quality: ClosureQuality | None
    nondetections: tuple[NondetectionRecord, ...]
    phase_free_bound: PhaseFreeClosureBound | None
    topology_sha256s: tuple[tuple[str, str], ...]
    bootstrap_draw_count: int
    statistical_method: str


@dataclass(frozen=True, slots=True)
class _Plan:
    method: ClosureMethod
    campaign_id: str
    source_commit: str
    fixture_graph_sha256: str
    reference_plane_sha256: str
    splitter_multiport_characterized: bool
    splitter_multiport_characterization_sha256: str | None
    e_arm_cable_sha256s: dict[str, str]
    topology_hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ValidatedCohort:
    label: str
    values: npt.NDArray[np.complex128]
    upper_bounds: npt.NDArray[np.float64]
    detected: npt.NDArray[np.bool_]
    topology_sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedWeights:
    values: npt.NDArray[np.complex128]
    upper_bounds: npt.NDArray[np.float64]
    detected: npt.NDArray[np.bool_]
    topology_sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedCampaign:
    plan: _Plan
    global_h_c: _ValidatedCohort
    observed_e: _ValidatedCohort
    arms: tuple[tuple[str, _ValidatedCohort, _ValidatedCohort, _ValidatedCohort], ...]
    weights: _ValidatedWeights | None
    nondetections: tuple[NondetectionRecord, ...]
    topology_sha256s: tuple[tuple[str, str], ...]


def _parse_plan(identity: CanonicalIdentity) -> _Plan:
    payload = identity.payload()
    _require_exact_keys(
        payload,
        {
            "schema",
            "campaign_id",
            "method",
            "source_commit",
            "fixture_graph_sha256",
            "reference_plane_sha256",
            "splitter_multiport_characterized",
            "splitter_multiport_characterization_sha256",
            "e_arm_cable_sha256s",
            "topology_sha256s",
        },
        "closure plan",
    )
    if payload["schema"] != PLAN_SCHEMA:
        raise ClosureQualificationError(f"closure plan schema must be {PLAN_SCHEMA}")
    method = payload["method"]
    if method not in METHODS:
        raise ClosureQualificationError(f"closure method must be one of {METHODS}")
    campaign_id = _identifier(payload["campaign_id"], "campaign ID")
    source_commit = _git_commit(payload["source_commit"], "plan source commit")
    fixture_hash = _sha256(payload["fixture_graph_sha256"], "fixture graph hash")
    reference_hash = _sha256(payload["reference_plane_sha256"], "reference-plane hash")
    characterized = payload["splitter_multiport_characterized"]
    if not isinstance(characterized, bool):
        raise ClosureQualificationError("splitter multiport characterization flag must be boolean")
    raw_characterization_hash = payload["splitter_multiport_characterization_sha256"]
    characterization_hash: str | None
    if characterized:
        characterization_hash = _sha256(
            raw_characterization_hash,
            "splitter multiport characterization hash",
        )
    else:
        if raw_characterization_hash is not None:
            raise ClosureQualificationError(
                "uncharacterized splitter plan must set characterization hash to null"
            )
        characterization_hash = None

    raw_arm_cables = payload["e_arm_cable_sha256s"]
    if not isinstance(raw_arm_cables, Mapping) or tuple(raw_arm_cables) != ARMS:
        raise ClosureQualificationError(
            "plan must bind exactly the E arm/cable hashes for ANT1..ANT8"
        )
    arm_cable_hashes = {
        arm: _sha256(raw_arm_cables[arm], f"{arm} E arm/cable hash") for arm in ARMS
    }

    raw_topologies = payload["topology_sha256s"]
    if not isinstance(raw_topologies, Mapping):
        raise ClosureQualificationError("plan topology hashes must be an object")
    _require_exact_keys(
        raw_topologies,
        {"global_h_c", "observed_e", "arms", "joint_weights"},
        "plan topology hashes",
    )
    topology_hashes: dict[str, str] = {
        "global_h_c": _sha256(raw_topologies["global_h_c"], "global H_C topology hash"),
        "observed_e": _sha256(raw_topologies["observed_e"], "E topology hash"),
    }
    raw_arms = raw_topologies["arms"]
    if not isinstance(raw_arms, Mapping) or tuple(raw_arms) != ARMS:
        raise ClosureQualificationError("plan must bind exactly ANT1..ANT8 in sequential order")
    for arm in ARMS:
        raw_arm = raw_arms[arm]
        if not isinstance(raw_arm, Mapping):
            raise ClosureQualificationError(f"{arm} topology hashes must be an object")
        _require_exact_keys(raw_arm, {"c_i", "d1_i", "d2_i"}, f"{arm} topology hashes")
        for role in ("c_i", "d1_i", "d2_i"):
            topology_hashes[f"{arm}.{role}"] = _sha256(raw_arm[role], f"{arm} {role} topology hash")
    raw_weights = raw_topologies["joint_weights"]
    if method == "weighted":
        topology_hashes["joint_weights"] = _sha256(raw_weights, "joint weight topology hash")
    elif raw_weights is not None:
        raise ClosureQualificationError(
            "arm-preserving plan must set joint weight topology hash to null"
        )
    return _Plan(
        method=method,
        campaign_id=campaign_id,
        source_commit=source_commit,
        fixture_graph_sha256=fixture_hash,
        reference_plane_sha256=reference_hash,
        splitter_multiport_characterized=characterized,
        splitter_multiport_characterization_sha256=characterization_hash,
        e_arm_cable_sha256s=arm_cable_hashes,
        topology_hashes=topology_hashes,
    )


def _expected_source_configuration(method: ClosureMethod, role: str) -> str:
    if role == "global_h_c":
        return "all_selector_inputs_terminated_global"
    if role == "observed_e":
        return "simultaneous_8way_feed"
    if role == "c_i":
        return "all_selector_inputs_terminated_dedicated"
    if role == "d1_i":
        return "direct_one_hot"
    if role == "d2_i":
        return "arm_preserving_exact_e_arm" if method == "arm_preserving" else "weighted_input_arm"
    if role == "joint_weights":
        return "joint_board_input_weight_measurement"
    raise ClosureQualificationError(f"unknown closure role {role}")


def _validate_topology(
    identity: CanonicalIdentity,
    *,
    plan: _Plan,
    expected_sha256: str,
    role: str,
    arm: str | None,
) -> None:
    if identity.sha256 != expected_sha256:
        raise ClosureQualificationError(f"{role} topology hash does not match the immutable plan")
    payload = identity.payload()
    _require_exact_keys(
        payload,
        {
            "schema",
            "campaign_id",
            "method",
            "role",
            "arm",
            "fixture_graph_sha256",
            "reference_plane_sha256",
            "source_configuration",
            "topology_details",
            "upstream_sha256s",
        },
        f"{role} topology",
    )
    expected = {
        "schema": TOPOLOGY_SCHEMA,
        "campaign_id": plan.campaign_id,
        "method": plan.method,
        "role": role,
        "arm": arm,
        "fixture_graph_sha256": plan.fixture_graph_sha256,
        "reference_plane_sha256": plan.reference_plane_sha256,
        "source_configuration": _expected_source_configuration(plan.method, role),
    }
    for key, expected_value in expected.items():
        if payload[key] != expected_value:
            raise ClosureQualificationError(
                f"{role} topology {key} does not match the immutable campaign"
            )
    details = payload["topology_details"]
    if not isinstance(details, Mapping) or not details:
        raise ClosureQualificationError(f"{role} topology details must be a nonempty object")
    upstream = payload["upstream_sha256s"]
    if not isinstance(upstream, Mapping) or not upstream:
        raise ClosureQualificationError(f"{role} topology upstream hashes must be nonempty")
    for name, digest in upstream.items():
        _identifier(name, f"{role} upstream name")
        _sha256(digest, f"{role} upstream {name} hash")
    if role == "global_h_c":
        _sha256(details.get("all_input_load_set_sha256"), "global H_C load-set hash")
    elif role == "observed_e":
        raw_cables = details.get("arm_cable_sha256s")
        if not isinstance(raw_cables, Mapping) or tuple(raw_cables) != ARMS:
            raise ClosureQualificationError(
                "observed E topology must bind exactly ANT1..ANT8 arm/cable hashes"
            )
        if dict(raw_cables) != plan.e_arm_cable_sha256s:
            raise ClosureQualificationError(
                "observed E arm/cable hashes differ from the immutable plan"
            )
    elif role == "c_i":
        if details.get("all_selector_inputs_terminated") is not True:
            raise ClosureQualificationError("C_i topology must attest all inputs terminated")
        if details.get("valid_comparator_roles") != ["d1_i", "d2_i"]:
            raise ClosureQualificationError(
                "C_i topology must freeze its validity for both D1_i and D2_i"
            )
    elif role in {"d1_i", "d2_i"}:
        if arm is None:  # pragma: no cover - role binding is validated above.
            raise ClosureQualificationError(f"{role} topology requires an arm")
        if details.get("reference_c_i_topology_sha256") != plan.topology_hashes[f"{arm}.c_i"]:
            raise ClosureQualificationError(f"{role} does not bind its dedicated C_i topology")
        if details.get("board_input_reference_plane_sha256") != plan.reference_plane_sha256:
            raise ClosureQualificationError(f"{role} board-input reference plane is inconsistent")
        if role == "d1_i":
            _sha256(details.get("linearity_evidence_sha256"), "D1 linearity evidence hash")
        else:
            if details.get("e_topology_sha256") != plan.topology_hashes["observed_e"]:
                raise ClosureQualificationError("D2 does not bind the simultaneous E topology")
            if details.get("e_arm_cable_sha256") != plan.e_arm_cable_sha256s[arm]:
                raise ClosureQualificationError("D2 does not use the exact E arm/cable")
            if details.get("other_splitter_outputs_terminated") != 7:
                raise ClosureQualificationError(
                    "D2 must independently terminate the other seven splitter outputs"
                )
            if details.get("other_selector_inputs_terminated") != 7:
                raise ClosureQualificationError(
                    "D2 must independently terminate the other seven selector inputs"
                )
    elif role == "joint_weights":
        if details.get("vector_arms") != list(ARMS):
            raise ClosureQualificationError("weight topology must bind one ANT1..ANT8 vector")
        if details.get("weight_definition") != (
            "e_excitation_over_d1_excitation_at_same_board_input_reference_plane"
        ):
            raise ClosureQualificationError("weight topology uses an invalid weight definition")
        if details.get("board_input_reference_plane_sha256") != plan.reference_plane_sha256:
            raise ClosureQualificationError("weight reference plane differs from D1")
        if details.get("e_topology_sha256") != plan.topology_hashes["observed_e"]:
            raise ClosureQualificationError("weights do not bind the simultaneous E topology")
        raw_d1 = details.get("d1_topology_sha256s")
        expected_d1 = {arm_name: plan.topology_hashes[f"{arm_name}.d1_i"] for arm_name in ARMS}
        if not isinstance(raw_d1, Mapping) or dict(raw_d1) != expected_d1:
            raise ClosureQualificationError("weights do not bind every exact D1 topology")
        _sha256(
            details.get("common_phase_reference_sha256"),
            "joint-weight common phase-reference hash",
        )


class _SourceRegistry:
    def __init__(self) -> None:
        self.run_ids: set[str] = set()
        self.condition_ids: set[str] = set()
        self.stream_ids: set[str] = set()
        self.artifacts: set[str] = set()
        self.raw_iq: set[str] = set()
        self.metadata: set[str] = set()
        self.condition_records: set[str] = set()
        self.leaf_sources: set[str] = set()

    def add_repeat(self, repeat: ClosureRepeat, label: str) -> None:
        self._add(self.run_ids, repeat.run_id, f"{label} run ID")
        self._add(self.condition_ids, repeat.condition_id, f"{label} condition ID")
        self._add(self.stream_ids, repeat.stream_id, f"{label} stream ID")
        self._add(self.artifacts, repeat.artifact_sha256, f"{label} artifact hash")
        self._add(self.raw_iq, repeat.raw_iq_sha256, f"{label} raw-IQ hash")
        self._add(self.metadata, repeat.metadata_sha256, f"{label} metadata hash")
        self._add(
            self.condition_records,
            repeat.condition_record_sha256,
            f"{label} condition-record hash",
        )
        self._add_leaf_sources(
            repeat.leaf_source_sha256s,
            repeat.leaf_source_set_sha256,
            label,
        )

    def add_weight_repeat(self, repeat: JointWeightVectorRepeat, label: str) -> None:
        self._add(self.run_ids, repeat.run_id, f"{label} run ID")
        self._add(self.condition_ids, repeat.condition_id, f"{label} condition ID")
        self._add(self.stream_ids, repeat.stream_id, f"{label} stream ID")
        self._add(self.artifacts, repeat.artifact_sha256, f"{label} artifact hash")
        self._add(self.metadata, repeat.metadata_sha256, f"{label} metadata hash")
        self._add(
            self.condition_records,
            repeat.condition_record_sha256,
            f"{label} condition-record hash",
        )
        self._add_leaf_sources(
            repeat.leaf_source_sha256s,
            repeat.leaf_source_set_sha256,
            label,
        )

    def _add_leaf_sources(
        self,
        leaf_sources: tuple[str, ...],
        claimed_set_sha256: str,
        label: str,
    ) -> None:
        if not isinstance(leaf_sources, tuple):
            raise ClosureQualificationError(
                f"{label} leaf-source hashes must be an immutable tuple"
            )
        expected_set_hash = leaf_source_set_sha256(leaf_sources)
        if claimed_set_sha256 != expected_set_hash:
            raise ClosureQualificationError(f"{label} leaf-source set hash mismatch")
        for leaf_source in leaf_sources:
            if leaf_source in self.leaf_sources:
                raise ClosureQualificationError(
                    f"closure evidence is not source-disjoint: reused {label} raw leaf source"
                )
            self.leaf_sources.add(leaf_source)

    @staticmethod
    def _add(seen: set[str], value: object, label: str) -> None:
        identifier = _identifier(value, label)
        if identifier in seen:
            raise ClosureQualificationError(
                f"closure evidence is not source-disjoint: reused {label}"
            )
        seen.add(identifier)


def _validate_detection(value: ComplexDetection, label: str) -> tuple[complex, float, bool]:
    if not isinstance(value, ComplexDetection):
        raise ClosureQualificationError(f"{label} must be ComplexDetection")
    if not isinstance(value.detected, bool):
        raise ClosureQualificationError(f"{label} detected flag must be boolean")
    if value.detected:
        if value.phasor is None:
            raise ClosureQualificationError(f"{label} detected value lacks a phasor")
        phasor = _finite_complex(value.phasor, f"{label} phasor")
        if abs(phasor) <= _EPSILON:
            raise ClosureQualificationError(f"{label} detected phasor must be nonzero")
        if value.magnitude_upper_bound is not None:
            raise ClosureQualificationError(
                f"{label} detected value must not carry a magnitude upper bound"
            )
        return phasor, abs(phasor), True
    if value.phasor is not None:
        raise ClosureQualificationError(
            f"{label} nondetection must not synthesize a phasor or phase"
        )
    upper = _finite_float(value.magnitude_upper_bound, f"{label} magnitude upper bound")
    if upper <= 0.0:
        raise ClosureQualificationError(f"{label} magnitude upper bound must be positive")
    return 0.0 + 0.0j, upper, False


def _validate_cohort(
    cohort: ClosureCohort,
    *,
    label: str,
    role: str,
    arm: str | None,
    plan: _Plan,
    plan_sha256: str,
    expected_topology_sha256: str,
    sources: _SourceRegistry,
    nondetections: list[NondetectionRecord],
) -> _ValidatedCohort:
    if not isinstance(cohort, ClosureCohort):
        raise ClosureQualificationError(f"{label} must be ClosureCohort")
    if cohort.role != role or cohort.arm != arm:
        raise ClosureQualificationError(f"{label} role/arm binding is wrong")
    if cohort.plan_sha256 != plan_sha256:
        raise ClosureQualificationError(f"{label} plan hash is stale")
    if cohort.source_commit != plan.source_commit:
        raise ClosureQualificationError(f"{label} source commit differs from the frozen plan")
    _validate_topology(
        cohort.topology_identity,
        plan=plan,
        expected_sha256=expected_topology_sha256,
        role=role,
        arm=arm,
    )
    if isinstance(cohort.repeats, (str, bytes)) or len(cohort.repeats) != REPEAT_COUNT:
        raise ClosureQualificationError(f"{label} must contain exactly five repeats")
    indexed: dict[int, tuple[complex, float, bool]] = {}
    for repeat in cohort.repeats:
        if not isinstance(repeat, ClosureRepeat):
            raise ClosureQualificationError(f"{label} repeats must be ClosureRepeat")
        if (
            isinstance(repeat.repeat_index, bool)
            or not isinstance(repeat.repeat_index, Integral)
            or int(repeat.repeat_index) not in range(1, REPEAT_COUNT + 1)
        ):
            raise ClosureQualificationError(f"{label} repeat indices must be exactly 1..5")
        index = int(repeat.repeat_index)
        if index in indexed:
            raise ClosureQualificationError(f"{label} contains a duplicate repeat index")
        if repeat.plan_sha256 != plan_sha256:
            raise ClosureQualificationError(f"{label} repeat has a stale plan hash")
        if repeat.topology_sha256 != expected_topology_sha256:
            raise ClosureQualificationError(f"{label} repeat has a stale topology hash")
        if repeat.source_commit != plan.source_commit:
            raise ClosureQualificationError(f"{label} repeat source commit is inconsistent")
        if repeat.quality_passed is not True:
            raise ClosureQualificationError(f"{label} repeat failed acquisition quality")
        _sha256(repeat.artifact_sha256, f"{label} artifact hash")
        _sha256(repeat.raw_iq_sha256, f"{label} raw-IQ hash")
        _sha256(repeat.metadata_sha256, f"{label} metadata hash")
        _sha256(repeat.condition_record_sha256, f"{label} condition-record hash")
        sources.add_repeat(repeat, label)
        phasor, upper, detected = _validate_detection(repeat.value, f"{label} repeat {index}")
        if not detected:
            nondetections.append(
                NondetectionRecord(
                    label=f"{label}.repeat{index}",
                    magnitude_upper_bound=upper,
                    phase_synthesized=False,
                )
            )
        indexed[index] = phasor, upper, detected
    if set(indexed) != set(range(1, REPEAT_COUNT + 1)):
        raise ClosureQualificationError(f"{label} repeat indices must be exactly 1..5")
    ordered = [indexed[index] for index in range(1, REPEAT_COUNT + 1)]
    return _ValidatedCohort(
        label=label,
        values=np.asarray([item[0] for item in ordered], dtype=np.complex128),
        upper_bounds=np.asarray([item[1] for item in ordered], dtype=np.float64),
        detected=np.asarray([item[2] for item in ordered], dtype=np.bool_),
        topology_sha256=expected_topology_sha256,
    )


def _validate_weights(
    cohort: JointWeightVectorCohort,
    *,
    plan: _Plan,
    plan_sha256: str,
    expected_topology_sha256: str,
    sources: _SourceRegistry,
    nondetections: list[NondetectionRecord],
) -> _ValidatedWeights:
    if not isinstance(cohort, JointWeightVectorCohort):
        raise ClosureQualificationError("joint weights must be JointWeightVectorCohort")
    if cohort.role != "joint_weights":
        raise ClosureQualificationError("joint weight cohort role is wrong")
    if cohort.plan_sha256 != plan_sha256 or cohort.source_commit != plan.source_commit:
        raise ClosureQualificationError("joint weight cohort is stale")
    _validate_topology(
        cohort.topology_identity,
        plan=plan,
        expected_sha256=expected_topology_sha256,
        role="joint_weights",
        arm=None,
    )
    if isinstance(cohort.repeats, (str, bytes)) or len(cohort.repeats) != REPEAT_COUNT:
        raise ClosureQualificationError("joint weights must contain exactly five vector repeats")
    indexed: dict[int, tuple[list[complex], list[float], list[bool]]] = {}
    for repeat in cohort.repeats:
        if not isinstance(repeat, JointWeightVectorRepeat):
            raise ClosureQualificationError(
                "joint weights must be complete JointWeightVectorRepeat objects"
            )
        if (
            isinstance(repeat.repeat_index, bool)
            or not isinstance(repeat.repeat_index, Integral)
            or int(repeat.repeat_index) not in range(1, REPEAT_COUNT + 1)
        ):
            raise ClosureQualificationError("joint weight repeat indices must be exactly 1..5")
        index = int(repeat.repeat_index)
        if index in indexed:
            raise ClosureQualificationError("joint weights contain a duplicate repeat index")
        if repeat.plan_sha256 != plan_sha256:
            raise ClosureQualificationError("joint weight repeat has a stale plan hash")
        if repeat.topology_sha256 != expected_topology_sha256:
            raise ClosureQualificationError("joint weight repeat has a stale topology hash")
        if repeat.source_commit != plan.source_commit:
            raise ClosureQualificationError("joint weight repeat source commit is inconsistent")
        if repeat.quality_passed is not True:
            raise ClosureQualificationError("joint weight repeat failed acquisition quality")
        _sha256(repeat.artifact_sha256, "joint weight artifact hash")
        _sha256(repeat.metadata_sha256, "joint weight metadata hash")
        _sha256(repeat.condition_record_sha256, "joint weight condition-record hash")
        sources.add_weight_repeat(repeat, "joint weight")
        if isinstance(repeat.weights, (str, bytes)) or len(repeat.weights) != len(ARMS):
            raise ClosureQualificationError(
                "each joint weight repeat must contain exactly ANT1..ANT8 as one vector"
            )
        values: list[complex] = []
        bounds: list[float] = []
        detections: list[bool] = []
        for arm, weight in zip(ARMS, repeat.weights, strict=True):
            phasor, upper, detected = _validate_detection(
                weight, f"joint weight repeat {index} {arm}"
            )
            values.append(phasor)
            bounds.append(upper)
            detections.append(detected)
            if not detected:
                nondetections.append(
                    NondetectionRecord(
                        label=f"joint_weights.repeat{index}.{arm}",
                        magnitude_upper_bound=upper,
                        phase_synthesized=False,
                    )
                )
        indexed[index] = values, bounds, detections
    if set(indexed) != set(range(1, REPEAT_COUNT + 1)):
        raise ClosureQualificationError("joint weight repeat indices must be exactly 1..5")
    ordered = [indexed[index] for index in range(1, REPEAT_COUNT + 1)]
    return _ValidatedWeights(
        values=np.asarray([item[0] for item in ordered], dtype=np.complex128),
        upper_bounds=np.asarray([item[1] for item in ordered], dtype=np.float64),
        detected=np.asarray([item[2] for item in ordered], dtype=np.bool_),
        topology_sha256=expected_topology_sha256,
    )


def _validate_campaign(evidence: ClosureCampaignEvidence) -> _ValidatedCampaign:
    if not isinstance(evidence, ClosureCampaignEvidence):
        raise ClosureQualificationError("evidence must be ClosureCampaignEvidence")
    plan = _parse_plan(evidence.plan_identity)
    plan_sha256 = evidence.plan_identity.sha256
    sources = _SourceRegistry()
    nondetections: list[NondetectionRecord] = []
    topology_sha256s: list[tuple[str, str]] = []
    global_h_c = _validate_cohort(
        evidence.global_h_c,
        label="global_h_c",
        role="global_h_c",
        arm=None,
        plan=plan,
        plan_sha256=plan_sha256,
        expected_topology_sha256=plan.topology_hashes["global_h_c"],
        sources=sources,
        nondetections=nondetections,
    )
    topology_sha256s.append(("global_h_c", global_h_c.topology_sha256))
    observed_e = _validate_cohort(
        evidence.observed_e,
        label="observed_e",
        role="observed_e",
        arm=None,
        plan=plan,
        plan_sha256=plan_sha256,
        expected_topology_sha256=plan.topology_hashes["observed_e"],
        sources=sources,
        nondetections=nondetections,
    )
    topology_sha256s.append(("observed_e", observed_e.topology_sha256))
    if isinstance(evidence.arms, (str, bytes)) or tuple(item.arm for item in evidence.arms) != ARMS:
        raise ClosureQualificationError("closure evidence must contain exactly ANT1..ANT8 in order")
    arms: list[tuple[str, _ValidatedCohort, _ValidatedCohort, _ValidatedCohort]] = []
    for arm_evidence in evidence.arms:
        arm = arm_evidence.arm
        c_i = _validate_cohort(
            arm_evidence.c_i,
            label=f"{arm}.c_i",
            role="c_i",
            arm=arm,
            plan=plan,
            plan_sha256=plan_sha256,
            expected_topology_sha256=plan.topology_hashes[f"{arm}.c_i"],
            sources=sources,
            nondetections=nondetections,
        )
        d1_i = _validate_cohort(
            arm_evidence.d1_i,
            label=f"{arm}.d1_i",
            role="d1_i",
            arm=arm,
            plan=plan,
            plan_sha256=plan_sha256,
            expected_topology_sha256=plan.topology_hashes[f"{arm}.d1_i"],
            sources=sources,
            nondetections=nondetections,
        )
        d2_i = _validate_cohort(
            arm_evidence.d2_i,
            label=f"{arm}.d2_i",
            role="d2_i",
            arm=arm,
            plan=plan,
            plan_sha256=plan_sha256,
            expected_topology_sha256=plan.topology_hashes[f"{arm}.d2_i"],
            sources=sources,
            nondetections=nondetections,
        )
        arms.append((arm, c_i, d1_i, d2_i))
        topology_sha256s.extend(
            (
                (f"{arm}.c_i", c_i.topology_sha256),
                (f"{arm}.d1_i", d1_i.topology_sha256),
                (f"{arm}.d2_i", d2_i.topology_sha256),
            )
        )
    weights: _ValidatedWeights | None = None
    if plan.method == "weighted":
        if evidence.joint_weights is None:
            raise ClosureQualificationError(
                "weighted closure requires five jointly acquired weight-vector repeats"
            )
        weights = _validate_weights(
            evidence.joint_weights,
            plan=plan,
            plan_sha256=plan_sha256,
            expected_topology_sha256=plan.topology_hashes["joint_weights"],
            sources=sources,
            nondetections=nondetections,
        )
        topology_sha256s.append(("joint_weights", weights.topology_sha256))
    elif evidence.joint_weights is not None:
        raise ClosureQualificationError("arm-preserving closure must not include weight evidence")
    return _ValidatedCampaign(
        plan=plan,
        global_h_c=global_h_c,
        observed_e=observed_e,
        arms=tuple(arms),
        weights=weights,
        nondetections=tuple(nondetections),
        topology_sha256s=tuple(topology_sha256s),
    )


def _cohort_upper_bound(cohort: _ValidatedCohort) -> float:
    return float(np.max(cohort.upper_bounds))


def _phase_free_bound(campaign: _ValidatedCampaign) -> PhaseFreeClosureBound:
    predicted = _cohort_upper_bound(campaign.global_h_c)
    if campaign.plan.method == "arm_preserving":
        for _, c_i, _, d2_i in campaign.arms:
            predicted += _cohort_upper_bound(c_i) + _cohort_upper_bound(d2_i)
    else:
        if campaign.weights is None:  # pragma: no cover - validated above.
            raise ClosureQualificationError("internal weighted campaign lacks weights")
        weight_bounds = np.max(campaign.weights.upper_bounds, axis=0)
        for arm_index, (_, c_i, d1_i, _) in enumerate(campaign.arms):
            predicted += float(weight_bounds[arm_index]) * (
                _cohort_upper_bound(c_i) + _cohort_upper_bound(d1_i)
            )
    observed = _cohort_upper_bound(campaign.observed_e)
    return PhaseFreeClosureBound(
        predicted_magnitude_upper_bound=predicted,
        observed_e_magnitude_upper_bound=observed,
        residual_magnitude_upper_bound=predicted + observed,
        phase_available=False,
        zero_phase_synthesized=False,
        method="triangle_inequality_over_phase_free_per_repeat_magnitude_bounds",
    )


def _bootstrap_complex(
    values: npt.NDArray[np.complex128],
    *,
    generator: np.random.Generator,
    draw_count: int,
) -> tuple[complex, npt.NDArray[np.complex128]]:
    indices = generator.integers(0, REPEAT_COUNT, size=(draw_count, REPEAT_COUNT))
    return (
        complex(np.mean(values)),
        np.asarray(np.mean(values[indices], axis=1), dtype=np.complex128),
    )


def _bootstrap_weight_vectors(
    values: npt.NDArray[np.complex128],
    *,
    generator: np.random.Generator,
    draw_count: int,
) -> tuple[npt.NDArray[np.complex128], npt.NDArray[np.complex128]]:
    indices = generator.integers(0, REPEAT_COUNT, size=(draw_count, REPEAT_COUNT))
    # The same row indices apply to all eight arms.  This is the key covariance guarantee.
    return (
        np.asarray(np.mean(values, axis=0), dtype=np.complex128),
        np.asarray(np.mean(values[indices], axis=1), dtype=np.complex128),
    )


def _complex_covariance(values: npt.NDArray[np.complex128]) -> Matrix:
    components = np.stack((values.real, values.imag), axis=1)
    covariance = np.cov(components, rowvar=False, ddof=1)
    return tuple(tuple(float(item) for item in row) for row in np.atleast_2d(covariance))


def _complex_vector_covariance(values: npt.NDArray[np.complex128]) -> Matrix:
    components = np.empty((values.shape[0], values.shape[1] * 2), dtype=np.float64)
    components[:, 0::2] = values.real
    components[:, 1::2] = values.imag
    covariance = np.cov(components, rowvar=False, ddof=1)
    return tuple(tuple(float(item) for item in row) for row in np.atleast_2d(covariance))


def _complex_summary(center: complex, draws: npt.NDArray[np.complex128]) -> ComplexSummary:
    magnitude = abs(center)
    phase = None if magnitude <= _EPSILON else degrees(atan2(center.imag, center.real))
    return ComplexSummary(
        center=center,
        magnitude=magnitude,
        phase_deg=phase,
        covariance_real_imag=_complex_covariance(draws),
    )


def _wrap_degrees(value: npt.NDArray[np.float64] | float) -> npt.NDArray[np.float64] | float:
    return (value + 180.0) % 360.0 - 180.0


def _interval(
    values: npt.NDArray[np.float64],
    *,
    confidence: float,
    one_sided_upper: bool = False,
) -> tuple[float, float]:
    alpha = 1.0 - confidence
    if one_sided_upper:
        return 0.0, float(np.quantile(values, 1.0 - alpha, method="higher"))
    return (
        float(np.quantile(values, alpha / 2.0, method="lower")),
        float(np.quantile(values, 1.0 - alpha / 2.0, method="higher")),
    )


def _quality(
    predicted_center: complex,
    predicted_draws: npt.NDArray[np.complex128],
    observed_center: complex,
    observed_draws: npt.NDArray[np.complex128],
    *,
    thresholds: ClosureThresholds,
) -> ClosureQuality:
    if abs(predicted_center) <= _EPSILON or abs(observed_center) <= _EPSILON:
        raise ClosureQualificationError("complex closure reference center has zero magnitude")
    point_amplitude = 20.0 * log10(abs(predicted_center) / abs(observed_center))
    point_phase = float(_wrap_degrees(degrees(np.angle(predicted_center / observed_center))))
    point_residual = abs(predicted_center - observed_center) / abs(observed_center)

    valid = (np.abs(predicted_draws) > _EPSILON) & (np.abs(observed_draws) > _EPSILON)
    if not bool(np.all(valid)):
        raise ClosureQualificationError("a bootstrap draw has zero closure-reference magnitude")
    amplitude_draws = np.asarray(
        20.0 * np.log10(np.abs(predicted_draws) / np.abs(observed_draws)),
        dtype=np.float64,
    )
    raw_phase_draws = np.asarray(
        _wrap_degrees(np.degrees(np.angle(predicted_draws / observed_draws))),
        dtype=np.float64,
    )
    phase_draws = np.asarray(
        point_phase + _wrap_degrees(raw_phase_draws - point_phase), dtype=np.float64
    )
    residual_draws = np.asarray(
        np.abs(predicted_draws - observed_draws) / np.abs(observed_draws),
        dtype=np.float64,
    )
    alpha = 1.0 - thresholds.simultaneous_confidence
    per_metric_confidence = 1.0 - alpha / 3.0
    amplitude_interval = _interval(amplitude_draws, confidence=per_metric_confidence)
    phase_interval = _interval(phase_draws, confidence=per_metric_confidence)
    residual_interval = _interval(
        residual_draws,
        confidence=per_metric_confidence,
        one_sided_upper=True,
    )
    magnitude_passed = (
        amplitude_interval[0] >= -thresholds.magnitude_tolerance_db
        and amplitude_interval[1] <= thresholds.magnitude_tolerance_db
    )
    phase_passed = (
        phase_interval[0] >= -thresholds.phase_tolerance_deg
        and phase_interval[1] <= thresholds.phase_tolerance_deg
    )
    residual_passed = residual_interval[1] <= thresholds.complex_residual_limit
    reasons: list[str] = []
    if not magnitude_passed:
        reasons.append("simultaneous magnitude interval exceeds +/-0.2 dB")
    if not phase_passed:
        reasons.append("simultaneous phase interval exceeds +/-2 degrees")
    if not residual_passed:
        reasons.append("simultaneous residual upper bound exceeds 4.23 percent")
    return ClosureQuality(
        amplitude_error_db=ScalarInterval(point_amplitude, amplitude_interval),
        phase_error_deg=ScalarInterval(point_phase, phase_interval),
        normalized_complex_residual=ScalarInterval(point_residual, residual_interval),
        magnitude_gate_passed=magnitude_passed,
        phase_gate_passed=phase_passed,
        residual_gate_passed=residual_passed,
        full_complex_equivalent=magnitude_passed and phase_passed and residual_passed,
        failure_reasons=tuple(reasons),
        simultaneous_confidence=thresholds.simultaneous_confidence,
        per_metric_bonferroni_confidence=per_metric_confidence,
        statistical_method=(
            "full_joint_nonparametric_mean_bootstrap_with_bonferroni_familywise_95pct_gates"
        ),
    )


def qualify_closure(
    evidence: ClosureCampaignEvidence,
    *,
    thresholds: ClosureThresholds = DEFAULT_CLOSURE_THRESHOLDS,
    bootstrap_draw_count: int = DEFAULT_BOOTSTRAP_DRAWS,
    bootstrap_seed: int = 0x5A8C10,
) -> ClosureQualificationResult:
    """Validate and qualify one complete arm-preserving or weighted campaign."""

    if (
        isinstance(bootstrap_draw_count, bool)
        or not isinstance(bootstrap_draw_count, Integral)
        or int(bootstrap_draw_count) < 256
    ):
        raise ClosureQualificationError("bootstrap draw count must be an integer of at least 256")
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, Integral):
        raise ClosureQualificationError("bootstrap seed must be an integer")
    draw_count = int(bootstrap_draw_count)
    campaign = _validate_campaign(evidence)
    authority = (
        "closure_qualified"
        if campaign.plan.method == "weighted" or campaign.plan.splitter_multiport_characterized
        else "diagnostic_only_uncharacterized_splitter_multiport"
    )
    if campaign.nondetections:
        return ClosureQualificationResult(
            status="not_evaluable_nondetection",
            method=campaign.plan.method,
            campaign_id=campaign.plan.campaign_id,
            plan_sha256=evidence.plan_identity.sha256,
            fixture_graph_sha256=campaign.plan.fixture_graph_sha256,
            reference_plane_sha256=campaign.plan.reference_plane_sha256,
            closure_authority=authority,
            splitter_multiport_characterized=campaign.plan.splitter_multiport_characterized,
            splitter_multiport_characterization_sha256=(
                campaign.plan.splitter_multiport_characterization_sha256
            ),
            source_disjointness_verified=True,
            joint_weight_covariance_preserved=campaign.plan.method == "weighted",
            predicted_e=None,
            observed_e=None,
            observed_minus_predicted=None,
            quality=None,
            closure_claim_supported=False,
            arm_diagnostics=(),
            source_uncertainties=(),
            weight_vector_covariance_real_imag=None,
            primary_increment_vector_covariance_real_imag=None,
            d2_validation_quality=None,
            nondetections=campaign.nondetections,
            phase_free_bound=_phase_free_bound(campaign),
            topology_sha256s=campaign.topology_sha256s,
            bootstrap_draw_count=0,
            statistical_method="phase_free_fail_closed_no_complex_bootstrap",
        )

    generator = np.random.default_rng(int(bootstrap_seed))
    h_c_center, h_c_draws = _bootstrap_complex(
        campaign.global_h_c.values, generator=generator, draw_count=draw_count
    )
    e_center, e_draws = _bootstrap_complex(
        campaign.observed_e.values, generator=generator, draw_count=draw_count
    )
    source_uncertainties: list[SourceUncertainty] = [
        SourceUncertainty("global_h_c", _complex_covariance(h_c_draws)),
        SourceUncertainty("observed_e", _complex_covariance(e_draws)),
    ]
    arm_bootstraps: list[
        tuple[
            str,
            complex,
            npt.NDArray[np.complex128],
            complex,
            npt.NDArray[np.complex128],
            complex,
            npt.NDArray[np.complex128],
            _ValidatedCohort,
            _ValidatedCohort,
            _ValidatedCohort,
        ]
    ] = []
    for arm, c_i, d1_i, d2_i in campaign.arms:
        c_center, c_draws = _bootstrap_complex(
            c_i.values, generator=generator, draw_count=draw_count
        )
        d1_center, d1_draws = _bootstrap_complex(
            d1_i.values, generator=generator, draw_count=draw_count
        )
        d2_center, d2_draws = _bootstrap_complex(
            d2_i.values, generator=generator, draw_count=draw_count
        )
        source_uncertainties.extend(
            (
                SourceUncertainty(f"{arm}.c_i", _complex_covariance(c_draws)),
                SourceUncertainty(f"{arm}.d1_i", _complex_covariance(d1_draws)),
                SourceUncertainty(f"{arm}.d2_i", _complex_covariance(d2_draws)),
            )
        )
        arm_bootstraps.append(
            (
                arm,
                c_center,
                c_draws,
                d1_center,
                d1_draws,
                d2_center,
                d2_draws,
                c_i,
                d1_i,
                d2_i,
            )
        )

    weight_centers: npt.NDArray[np.complex128] | None = None
    weight_draws: npt.NDArray[np.complex128] | None = None
    weight_covariance: Matrix | None = None
    if campaign.weights is not None:
        weight_centers, weight_draws = _bootstrap_weight_vectors(
            campaign.weights.values,
            generator=generator,
            draw_count=draw_count,
        )
        weight_covariance = _complex_vector_covariance(weight_draws)

    primary_centers: list[complex] = []
    primary_draw_vectors: list[npt.NDArray[np.complex128]] = []
    d2_centers: list[complex] = []
    d2_draw_vectors: list[npt.NDArray[np.complex128]] = []
    diagnostics: list[ArmClosureDiagnostic] = []
    for arm_index, item in enumerate(arm_bootstraps):
        (
            arm,
            c_center,
            c_draws,
            d1_center,
            d1_draws,
            d2_center,
            d2_draws,
            c_i,
            d1_i,
            d2_i,
        ) = item
        d1_increment_center = d1_center - c_center
        d1_increment_draws = d1_draws - c_draws
        d2_increment_center = d2_center - c_center
        d2_increment_draws = d2_draws - c_draws
        d2_centers.append(d2_increment_center)
        d2_draw_vectors.append(d2_increment_draws)
        if campaign.plan.method == "arm_preserving":
            weight_center = None
            primary_center = d2_increment_center
            primary_draws = d2_increment_draws
            independent_center = d1_increment_center
            independent_draws = d1_increment_draws
        else:
            if weight_centers is None or weight_draws is None:  # pragma: no cover - validated.
                raise ClosureQualificationError("internal weighted bootstrap lacks weights")
            weight_center = complex(weight_centers[arm_index])
            primary_center = weight_center * d1_increment_center
            primary_draws = weight_draws[:, arm_index] * d1_increment_draws
            independent_center = d2_increment_center
            independent_draws = d2_increment_draws
        primary_centers.append(primary_center)
        primary_draw_vectors.append(primary_draws)
        increment_residual_center = independent_center - primary_center
        increment_residual_draws = independent_draws - primary_draws
        diagnostics.append(
            ArmClosureDiagnostic(
                arm=arm,
                c_i_center=c_center,
                d1_i_center=d1_center,
                d2_i_center=d2_center,
                weight_center=weight_center,
                primary_increment_center=primary_center,
                independently_observed_increment_center=independent_center,
                increment_residual_center=increment_residual_center,
                increment_residual_covariance_real_imag=_complex_covariance(
                    increment_residual_draws
                ),
                c_i_topology_sha256=c_i.topology_sha256,
                d1_i_topology_sha256=d1_i.topology_sha256,
                d2_i_topology_sha256=d2_i.topology_sha256,
            )
        )

    primary_draw_matrix = np.stack(primary_draw_vectors, axis=1)
    predicted_center = h_c_center + sum(primary_centers, 0.0 + 0.0j)
    predicted_draws = h_c_draws + np.sum(primary_draw_matrix, axis=1)
    residual_center = e_center - predicted_center
    residual_draws = e_draws - predicted_draws
    quality = _quality(
        predicted_center,
        predicted_draws,
        e_center,
        e_draws,
        thresholds=thresholds,
    )

    d2_draw_matrix = np.stack(d2_draw_vectors, axis=1)
    d2_predicted_center = h_c_center + sum(d2_centers, 0.0 + 0.0j)
    d2_predicted_draws = h_c_draws + np.sum(d2_draw_matrix, axis=1)
    d2_quality = _quality(
        d2_predicted_center,
        d2_predicted_draws,
        e_center,
        e_draws,
        thresholds=thresholds,
    )
    return ClosureQualificationResult(
        status="evaluated",
        method=campaign.plan.method,
        campaign_id=campaign.plan.campaign_id,
        plan_sha256=evidence.plan_identity.sha256,
        fixture_graph_sha256=campaign.plan.fixture_graph_sha256,
        reference_plane_sha256=campaign.plan.reference_plane_sha256,
        closure_authority=authority,
        splitter_multiport_characterized=campaign.plan.splitter_multiport_characterized,
        splitter_multiport_characterization_sha256=(
            campaign.plan.splitter_multiport_characterization_sha256
        ),
        source_disjointness_verified=True,
        joint_weight_covariance_preserved=campaign.plan.method == "weighted",
        predicted_e=_complex_summary(predicted_center, predicted_draws),
        observed_e=_complex_summary(e_center, e_draws),
        observed_minus_predicted=_complex_summary(residual_center, residual_draws),
        quality=quality,
        closure_claim_supported=(
            quality.full_complex_equivalent and authority == "closure_qualified"
        ),
        arm_diagnostics=tuple(diagnostics),
        source_uncertainties=tuple(source_uncertainties),
        weight_vector_covariance_real_imag=weight_covariance,
        primary_increment_vector_covariance_real_imag=_complex_vector_covariance(
            primary_draw_matrix
        ),
        d2_validation_quality=d2_quality,
        nondetections=(),
        phase_free_bound=None,
        topology_sha256s=campaign.topology_sha256s,
        bootstrap_draw_count=draw_count,
        statistical_method=(
            "full_joint_source_disjoint_nonparametric_mean_bootstrap;"
            "joint_weight_rows_resampled_as_vectors"
        ),
    )
