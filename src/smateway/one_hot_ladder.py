"""Pure contracts for a physical single-driven-input selector matrix.

One hardware run drives exactly one board input while the other seven inputs
are individually terminated.  Its static selector-state sweep supplies one row
of the physical matrix.  A complete matrix requires eight provenance-bound,
independently confirmed row bundles; bare result dictionaries are insufficient.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import atan2, cos, degrees, isclose, isfinite, log10, radians, sin, sqrt
from numbers import Real
from typing import Any

import numpy as np

ALL_OFF_STATE = "ALL_OFF"
ANTENNA_STATES = tuple(f"ANT{index}" for index in range(1, 9))
ANTENNA_GPIO_CODES = (0, 4, 2, 6, 1, 5, 3, 7)
ALL_OFF_GPIO_CODE = 8
ONE_HOT_STATE_ORDER = (ALL_OFF_STATE, *ANTENNA_STATES)
TOPOLOGY_IDENTITY = "one_hot_single_driven_input_other_seven_terminated"
CELL_ROLE_ALL_OFF = "all_off"
CELL_ROLE_INTENDED_THROUGH = "intended_through"
CELL_ROLE_WRONG_STATE = "wrong_state"
DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB = 6.0
DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB = 0.2
DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG = 2.0
STRICT_OPERATIONAL_CONTRAST_DB = 20.0


@dataclass(frozen=True, slots=True)
class OneHotStateSummary:
    """Sweep-observation summary for one physical matrix cell."""

    driven_input: str
    selector_state_name: str
    cell_role: str
    condition_count: int
    quality_passed_count: int
    detected_count: int
    detection_fraction: float
    median_transfer_amplitude_db: float | None
    transfer_amplitude_span_db: float | None
    circular_mean_transfer_phase_deg: float | None
    sweep_observation_phase_coherence: float | None
    median_absent_upper_bound_db: float | None
    intended_contrast_detected_count: int
    median_selected_minus_all_off_db: float | None
    minimum_contrast_over_all_off_db: float | None
    attribution_repeat_count: int
    attribution_quality_passed_count: int
    attribution_contrast_detected_count: int
    attribution_median_selected_minus_all_off_db: float | None
    attribution_minimum_contrast_over_all_off_db: float | None
    attribution_contrast_phase_coherence: float | None
    attribution_median_raw_selected_to_all_off_db: float | None
    attribution_minimum_raw_selected_to_all_off_db: float | None
    attribution_median_path_contrast_db: float | None
    attribution_minimum_path_contrast_db: float | None
    attribution_minimum_conservative_raw_contrast_db: float | None
    attribution_contrast_amplitude_span_db: float | None
    attribution_contrast_max_phase_residual_deg: float | None
    attribution_complex_increment_95pct_uncertainty_radius: float | None
    attribution_complex_increment_confidence_excludes_zero: bool
    strict_20db_raw_contrast_gate_passed: bool
    strict_20db_path_contrast_gate_passed: bool


@dataclass(frozen=True, slots=True)
class OneHotRunSummary:
    """Admission result for one immutable driven-input row."""

    topology_identity: str
    driven_input: str
    shared_fixture_identity: Mapping[str, str]
    setup_evidence_sha256: str
    planned_selector_state_count: int
    planned_gain_count: int
    attribution_gain_db: float
    attribution_repeat_count: int
    planned_condition_count: int
    observed_condition_count: int
    minimum_detected_attribution_repeats: int
    minimum_intended_through_contrast_over_all_off_db: float
    maximum_attribution_amplitude_span_db: float
    maximum_attribution_phase_residual_deg: float
    all_off_cell_count: int
    intended_through_cell_count: int
    wrong_state_cell_count: int
    states: tuple[OneHotStateSummary, ...]
    quality_passed: bool
    quality_rejection_reasons: tuple[str, ...]
    causal_attribution_claim: bool
    operational_switching_claim: bool


@dataclass(frozen=True, slots=True)
class OneHotMatrixSummary:
    """Admission result for eight provenance-bound physical input rows."""

    topology_identity: str
    matrix_identity: Mapping[str, Any]
    shared_fixture_identity: Mapping[str, str]
    setup_evidence_count: int
    independently_confirmed_row_count: int
    planned_driven_input_count: int
    planned_selector_state_count: int
    planned_gain_count: int
    attribution_gain_db: float
    attribution_repeat_count: int
    planned_condition_count: int
    observed_condition_count: int
    all_off_cell_count: int
    intended_through_cell_count: int
    wrong_state_cell_count: int
    all_off_condition_count: int
    intended_through_condition_count: int
    wrong_state_condition_count: int
    runs: tuple[OneHotRunSummary, ...]
    quality_passed: bool
    quality_rejection_reasons: tuple[str, ...]
    causal_attribution_claim: bool
    operational_switching_claim: bool


_VERIFIED_ROW_SEAL = object()


@dataclass(frozen=True, slots=True)
class VerifiedOneHotRowBundle:
    """Opaque in-process result of the file-reading row verifier."""

    _canonical_json: str
    canonical_sha256: str
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _VERIFIED_ROW_SEAL:
            raise ValueError("verified row bundles must come from the file-reading loader")
        if hashlib.sha256(self._canonical_json.encode("utf-8")).hexdigest() != (
            self.canonical_sha256
        ):
            raise ValueError("verified row canonical bytes are inconsistent")

    @property
    def document(self) -> Mapping[str, Any]:
        value = json.loads(self._canonical_json)
        if not isinstance(value, dict):
            raise ValueError("verified row canonical root is not an object")
        return value


def _seal_verified_one_hot_row_bundle(
    document: Mapping[str, Any],
) -> VerifiedOneHotRowBundle:
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return VerifiedOneHotRowBundle(
        _canonical_json=canonical,
        canonical_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        _seal=_VERIFIED_ROW_SEAL,
    )


def validate_antenna_name(value: object, label: str = "antenna") -> str:
    if not isinstance(value, str) or value not in ANTENNA_STATES:
        raise ValueError(f"{label} must be exactly ANT1..ANT8")
    return value


def physical_confirmation_token(driven_input: str) -> str:
    exact_input = validate_antenna_name(driven_input, "driven input")
    return (
        f"ONE_HOT_DRIVE_{exact_input}_OTHER_SEVEN_50OHM_"
        "NO_8WAY_SIMULTANEOUS_FEED_SELECTOR_COMMON_RX2"
    )


def one_hot_cell_role(driven_input: str, selector_state_name: str) -> str:
    exact_input = validate_antenna_name(driven_input, "driven input")
    if selector_state_name == ALL_OFF_STATE:
        return CELL_ROLE_ALL_OFF
    selected = validate_antenna_name(selector_state_name, "selector state")
    return CELL_ROLE_INTENDED_THROUGH if selected == exact_input else CELL_ROLE_WRONG_STATE


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_finite(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, label)


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def validate_one_hot_fixture_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("one-hot fixture identity must be an object")
    shared = value.get("shared_hardware")
    evidence = value.get("setup_evidence")
    required = {
        "feed_arm_id",
        "feed_cable_id",
        "termination_load_set_id",
        "rx1_reference_plane_id",
        "rx2_reference_plane_id",
    }
    if not isinstance(shared, Mapping) or set(shared) != required:
        raise ValueError("one-hot shared fixture identity is incomplete")
    normalized_shared: dict[str, str] = {}
    for key in sorted(required):
        identifier = shared.get(key)
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("one-hot shared fixture identifier is invalid")
        normalized_shared[key] = identifier
    if (
        not isinstance(evidence, Mapping)
        or not isinstance(evidence.get("path"), str)
        or not evidence["path"]
    ):
        raise ValueError("one-hot setup evidence is incomplete")
    if value.get("attribution_repeats_without_cable_movement_required") is not True:
        raise ValueError("one-hot repeats must freeze the no-cable-movement contract")
    return {
        "shared_hardware": normalized_shared,
        "setup_evidence": {
            "path": str(evidence["path"]),
            "file_sha256": _sha256(evidence.get("file_sha256"), "setup evidence"),
        },
        "attribution_repeats_without_cable_movement_required": True,
    }


def _canonical_sha256(value: object) -> str:
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("matrix identity must be canonical JSON") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_one_hot_matrix_identity(value: object) -> dict[str, Any]:
    """Validate the common DUT, control image, source, and acquisition identity."""

    required = {
        "board_id",
        "pluto_serial",
        "smateway_commit",
        "pluto_plus_utils_source_attestation_sha256",
        "bench_manifest_sha256",
        "bench_elf_sha256",
        "bench_bin_sha256",
        "bench_protocol_sha256",
        "bench_verifier_sha256",
        "openocd_config_sha256",
        "control_profile_contract_sha256",
        "control_profile_sha256",
        "control_profile_header_sha256",
        "control_profile_provenance_sha256",
        "acquisition_configuration",
        "acquisition_configuration_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("matrix identity is incomplete")
    for key in ("board_id", "pluto_serial"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"matrix identity {key} is invalid")
    commit = value.get("smateway_commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise ValueError("matrix identity smateway commit is invalid")
    for key in required - {
        "board_id",
        "pluto_serial",
        "smateway_commit",
        "acquisition_configuration",
    }:
        _sha256(value.get(key), f"matrix identity {key}")
    configuration = value.get("acquisition_configuration")
    if not isinstance(configuration, Mapping) or not configuration:
        raise ValueError("matrix acquisition configuration is invalid")
    normalized_configuration = json.loads(
        json.dumps(configuration, sort_keys=True, allow_nan=False)
    )
    if (
        _canonical_sha256(normalized_configuration)
        != value["acquisition_configuration_sha256"]
    ):
        raise ValueError("matrix acquisition configuration hash differs")
    return {
        **{key: value[key] for key in required if key != "acquisition_configuration"},
        "acquisition_configuration": normalized_configuration,
    }


def validate_one_hot_state_codes(
    states: Sequence[Mapping[str, Any]],
    *,
    all_off_code: int,
) -> tuple[dict[str, int | str], ...]:
    """Require the exact reviewed ANT and ALL_OFF GPIO-code mapping."""

    if all_off_code != ALL_OFF_GPIO_CODE:
        raise ValueError(f"ALL_OFF code must be exactly {ALL_OFF_GPIO_CODE}")
    normalized: list[dict[str, int | str]] = []
    for raw in states:
        name = raw.get("name")
        code = raw.get("gpio_code")
        if not isinstance(name, str):
            raise ValueError("selector state name must be a string")
        if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 0xFF:
            raise ValueError("selector state code must fit in one byte")
        normalized.append({"name": name, "gpio_code": code})
    names = tuple(str(item["name"]) for item in normalized)
    codes = tuple(int(item["gpio_code"]) for item in normalized)
    if names != ANTENNA_STATES:
        raise ValueError("selector profile must contain ANT1..ANT8 in exact sequential order")
    if codes != ANTENNA_GPIO_CODES:
        raise ValueError("selector profile GPIO codes differ from the reviewed exact mapping")
    return (
        {"name": ALL_OFF_STATE, "gpio_code": ALL_OFF_GPIO_CODE},
        *normalized,
    )


def _expected_keys(
    states: Sequence[str],
    gains: Sequence[float],
    *,
    attribution_gain_db: float,
    attribution_repeat_count: int,
) -> set[tuple[str, float, int]]:
    return {
        (state, gain, repeat_index)
        for state in states
        for gain in gains
        for repeat_index in range(
            attribution_repeat_count if gain == attribution_gain_db else 1
        )
    }


def _state_summary(
    driven_input: str,
    selector_state_name: str,
    results: Sequence[Mapping[str, Any]],
    *,
    attribution_gain_db: float,
) -> OneHotStateSummary:
    detected_amplitudes: list[float] = []
    detected_phases: list[float] = []
    absent_upper_bounds: list[float] = []
    quality_count = 0
    detected_count = 0
    attribution = [
        result
        for result in results
        if float(result["tx_hardware_gain_db"]) == attribution_gain_db
    ]
    for result in results:
        quality_passed = result.get("measurement_quality_passed") is True
        if quality_passed:
            quality_count += 1
        detected = result.get("rx2_tone_detected") is True
        transfer = result.get("rx2_over_rx1")
        if not isinstance(transfer, Mapping):
            raise ValueError("one-hot condition transfer evidence must be an object")
        if detected:
            amplitude = _optional_finite(
                transfer.get("amplitude_db"),
                f"{driven_input}/{selector_state_name} detected transfer amplitude",
            )
            phase = _optional_finite(
                transfer.get("phase_deg"),
                f"{driven_input}/{selector_state_name} detected transfer phase",
            )
            if amplitude is None or phase is None:
                raise ValueError("detected one-hot transfer lacks amplitude or phase evidence")
            if quality_passed:
                detected_count += 1
                detected_amplitudes.append(amplitude)
                detected_phases.append(phase)
        else:
            upper_bound = _optional_finite(
                transfer.get("amplitude_upper_bound_db"),
                f"{driven_input}/{selector_state_name} absent-tone upper bound",
            )
            if upper_bound is not None:
                absent_upper_bounds.append(upper_bound)

    median_amplitude = (
        float(np.median(detected_amplitudes)) if detected_amplitudes else None
    )
    amplitude_span = (
        max(detected_amplitudes) - min(detected_amplitudes)
        if len(detected_amplitudes) >= 2
        else None
    )
    if detected_phases:
        mean_cos = sum(cos(radians(value)) for value in detected_phases) / len(detected_phases)
        mean_sin = sum(sin(radians(value)) for value in detected_phases) / len(detected_phases)
        phase_coherence = sqrt(mean_cos**2 + mean_sin**2)
        circular_phase = float(np.degrees(np.arctan2(mean_sin, mean_cos)))
    else:
        phase_coherence = None
        circular_phase = None
    return OneHotStateSummary(
        driven_input=driven_input,
        selector_state_name=selector_state_name,
        cell_role=one_hot_cell_role(driven_input, selector_state_name),
        condition_count=len(results),
        quality_passed_count=quality_count,
        detected_count=detected_count,
        detection_fraction=detected_count / len(results),
        median_transfer_amplitude_db=median_amplitude,
        transfer_amplitude_span_db=amplitude_span,
        circular_mean_transfer_phase_deg=circular_phase,
        sweep_observation_phase_coherence=phase_coherence,
        median_absent_upper_bound_db=(
            float(np.median(absent_upper_bounds)) if absent_upper_bounds else None
        ),
        intended_contrast_detected_count=0,
        median_selected_minus_all_off_db=None,
        minimum_contrast_over_all_off_db=None,
        attribution_repeat_count=len(attribution),
        attribution_quality_passed_count=sum(
            result.get("measurement_quality_passed") is True for result in attribution
        ),
        attribution_contrast_detected_count=0,
        attribution_median_selected_minus_all_off_db=None,
        attribution_minimum_contrast_over_all_off_db=None,
        attribution_contrast_phase_coherence=None,
        attribution_median_raw_selected_to_all_off_db=None,
        attribution_minimum_raw_selected_to_all_off_db=None,
        attribution_median_path_contrast_db=None,
        attribution_minimum_path_contrast_db=None,
        attribution_minimum_conservative_raw_contrast_db=None,
        attribution_contrast_amplitude_span_db=None,
        attribution_contrast_max_phase_residual_deg=None,
        attribution_complex_increment_95pct_uncertainty_radius=None,
        attribution_complex_increment_confidence_excludes_zero=False,
        strict_20db_raw_contrast_gate_passed=False,
        strict_20db_path_contrast_gate_passed=False,
    )


def _transfer_phasor(result: Mapping[str, Any], label: str) -> complex:
    transfer = result.get("rx2_over_rx1")
    if not isinstance(transfer, Mapping):
        raise ValueError(f"{label} transfer evidence must be an object")
    raw = transfer.get("phasor")
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} transfer phasor is missing")
    real = _finite_float(raw.get("real"), f"{label} transfer phasor real")
    imag = _finite_float(raw.get("imag"), f"{label} transfer phasor imaginary")
    return complex(real, imag)


def _validate_result_measurement_evidence(
    result: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Cross-check every representation consumed by the pure row aggregator."""

    quality_passed = result.get("measurement_quality_passed")
    rejection_reasons = result.get("measurement_quality_rejection_reasons")
    if not isinstance(quality_passed, bool):
        raise ValueError(f"{label} measurement-quality flag must be an exact boolean")
    if not isinstance(rejection_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in rejection_reasons
    ):
        raise ValueError(f"{label} measurement-quality rejection reasons are malformed")
    if quality_passed != (not rejection_reasons):
        raise ValueError(f"{label} measurement-quality flag and reasons disagree")

    detected = result.get("rx2_tone_detected")
    if not isinstance(detected, bool):
        raise ValueError(f"{label} RX2 detection flag must be an exact boolean")
    transfer = result.get("rx2_over_rx1")
    if not isinstance(transfer, Mapping):
        raise ValueError(f"{label} transfer evidence must be an object")
    phasor = _transfer_phasor(result, label)
    amplitude = abs(phasor)
    amplitude_ratio = _optional_finite(
        transfer.get("amplitude_ratio"),
        f"{label} transfer amplitude ratio",
    )
    amplitude_db = _optional_finite(
        transfer.get("amplitude_db"),
        f"{label} transfer amplitude dB",
    )
    phase_deg = _optional_finite(
        transfer.get("phase_deg"),
        f"{label} transfer phase",
    )
    if amplitude_ratio is not None and (
        amplitude_ratio < 0.0
        or not isclose(amplitude_ratio, amplitude, rel_tol=1e-9, abs_tol=1e-12)
    ):
        raise ValueError(f"{label} transfer amplitude ratio contradicts its phasor")
    if amplitude > 0.0:
        expected_db = 20.0 * log10(amplitude)
        expected_phase = degrees(atan2(phasor.imag, phasor.real))
        if amplitude_db is not None and not isclose(
            amplitude_db,
            expected_db,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError(f"{label} transfer amplitude dB contradicts its phasor")
        if phase_deg is not None:
            phase_residual = (phase_deg - expected_phase + 180.0) % 360.0 - 180.0
            if abs(phase_residual) > 1e-9:
                raise ValueError(f"{label} transfer phase contradicts its phasor")
    if detected and (
        amplitude <= 0.0
        or amplitude_ratio is None
        or amplitude_db is None
        or phase_deg is None
    ):
        raise ValueError(f"{label} detected transfer lacks amplitude or phase evidence")
    if not detected:
        upper_ratio = _optional_finite(
            transfer.get("amplitude_upper_bound_ratio"),
            f"{label} absent-tone amplitude upper-bound ratio",
        )
        upper_db = _optional_finite(
            transfer.get("amplitude_upper_bound_db"),
            f"{label} absent-tone amplitude upper-bound dB",
        )
        if upper_ratio is None or upper_ratio <= 0.0 or upper_db is None:
            raise ValueError(f"{label} absent tone lacks a finite positive amplitude bound")
        if upper_ratio + 1e-12 < amplitude:
            raise ValueError(f"{label} absent-tone bound is below the measured phasor")
        if not isclose(
            upper_db,
            20.0 * log10(upper_ratio),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError(f"{label} absent-tone bound ratio and dB disagree")


def _all_off_reference_amplitude(
    result: Mapping[str, Any],
    phasor: complex,
    label: str,
) -> float:
    if result.get("rx2_tone_detected") is True:
        amplitude = abs(phasor)
        if amplitude <= 0.0:
            raise ValueError(f"{label} detected ALL_OFF tone has zero phasor amplitude")
        return amplitude
    transfer = result.get("rx2_over_rx1")
    assert isinstance(transfer, Mapping)
    upper_bound = _optional_finite(
        transfer.get("amplitude_upper_bound_ratio"),
        f"{label} ALL_OFF amplitude upper bound",
    )
    if upper_bound is None or upper_bound <= 0.0:
        raise ValueError("ALL_OFF contrast reference lacks a positive amplitude bound")
    return upper_bound


def _phase_coherence(values: Sequence[complex]) -> float | None:
    nonzero = [value / abs(value) for value in values if abs(value) > 0.0]
    if not nonzero:
        return None
    return float(abs(sum(nonzero) / len(nonzero)))


def _intended_contrast_summary(
    indexed: Mapping[tuple[str, float, int], Mapping[str, Any]],
    *,
    driven_input: str,
    gains: Sequence[float],
    attribution_gain_db: float,
    attribution_repeat_count: int,
    minimum_contrast_db: float,
) -> dict[str, Any]:
    contrasts_db: list[float] = []
    contrast_over_all_off_db: list[float] = []
    attribution_contrasts_db: list[float] = []
    attribution_over_db: list[float] = []
    attribution_raw_over_db: list[float] = []
    attribution_exact_raw_db: list[float] = []
    attribution_exact_path_db: list[float] = []
    attribution_phasors: list[complex] = []
    detected_count = 0
    attribution_detected_count = 0
    for gain in gains:
        repeats = attribution_repeat_count if gain == attribution_gain_db else 1
        for repeat_index in range(repeats):
            all_off = indexed[(ALL_OFF_STATE, gain, repeat_index)]
            intended = indexed[(driven_input, gain, repeat_index)]
            label = f"{driven_input} {gain:g} dB repeat {repeat_index + 1}"
            all_off_phasor = _transfer_phasor(all_off, f"{label} ALL_OFF")
            intended_phasor = _transfer_phasor(intended, f"{label} intended-through")
            contrast_phasor = intended_phasor - all_off_phasor
            contrast_amplitude = abs(contrast_phasor)
            reference_amplitude = _all_off_reference_amplitude(
                all_off,
                all_off_phasor,
                label,
            )
            contrast_db = (
                -300.0 if contrast_amplitude <= 0.0 else 20.0 * log10(contrast_amplitude)
            )
            over_db = 20.0 * log10(
                max(contrast_amplitude, np.finfo(np.float64).tiny) / reference_amplitude
            )
            raw_over_db = 20.0 * log10(
                max(abs(intended_phasor), np.finfo(np.float64).tiny)
                / reference_amplitude
            )
            exact_all_off_amplitude = max(
                abs(all_off_phasor),
                np.finfo(np.float64).tiny,
            )
            exact_raw_db = 20.0 * log10(
                max(abs(intended_phasor), np.finfo(np.float64).tiny)
                / exact_all_off_amplitude
            )
            exact_path_db = 20.0 * log10(
                max(contrast_amplitude, np.finfo(np.float64).tiny)
                / exact_all_off_amplitude
            )
            contrasts_db.append(contrast_db)
            contrast_over_all_off_db.append(over_db)
            passed = (
                intended.get("measurement_quality_passed") is True
                and all_off.get("measurement_quality_passed") is True
                and intended.get("rx2_tone_detected") is True
                and over_db >= minimum_contrast_db
            )
            if passed:
                detected_count += 1
            if gain == attribution_gain_db:
                attribution_contrasts_db.append(contrast_db)
                attribution_over_db.append(over_db)
                attribution_raw_over_db.append(raw_over_db)
                attribution_exact_raw_db.append(exact_raw_db)
                attribution_exact_path_db.append(exact_path_db)
                attribution_phasors.append(contrast_phasor)
                if passed:
                    attribution_detected_count += 1
    attribution_amplitude_span_db = (
        max(attribution_contrasts_db) - min(attribution_contrasts_db)
    )
    mean_increment = sum(attribution_phasors) / len(attribution_phasors)
    phase_residuals = [
        abs(float(np.degrees(np.angle(value * np.conj(mean_increment)))))
        for value in attribution_phasors
        if abs(value) > 0.0 and abs(mean_increment) > 0.0
    ]
    uncertainty_radius = 0.0
    if len(attribution_phasors) >= 2:
        radial_variance = sum(
            abs(value - mean_increment) ** 2 for value in attribution_phasors
        ) / (len(attribution_phasors) - 1)
        uncertainty_radius = 4.303 * sqrt(
            radial_variance / len(attribution_phasors)
        )
    return {
        "detected_count": detected_count,
        "median_contrast_db": float(np.median(contrasts_db)),
        "minimum_over_db": min(contrast_over_all_off_db),
        "attribution_detected_count": attribution_detected_count,
        "attribution_median_contrast_db": float(np.median(attribution_contrasts_db)),
        "attribution_minimum_over_db": min(attribution_over_db),
        "attribution_phase_coherence": _phase_coherence(attribution_phasors),
        "attribution_median_raw_over_db": float(np.median(attribution_exact_raw_db)),
        "attribution_minimum_raw_over_db": min(attribution_exact_raw_db),
        "attribution_median_path_over_db": float(np.median(attribution_exact_path_db)),
        "attribution_minimum_path_over_db": min(attribution_exact_path_db),
        "attribution_minimum_conservative_raw_over_db": min(attribution_raw_over_db),
        "attribution_amplitude_span_db": attribution_amplitude_span_db,
        "attribution_max_phase_residual_deg": (
            max(phase_residuals) if phase_residuals else None
        ),
        "attribution_uncertainty_radius": uncertainty_radius,
        "attribution_confidence_excludes_zero": (
            abs(mean_increment) > uncertainty_radius
        ),
    }


def summarize_one_hot_run(
    results: Sequence[Mapping[str, Any]],
    *,
    driven_input: str,
    fixture_identity: Mapping[str, Any],
    planned_states: Sequence[str] = ONE_HOT_STATE_ORDER,
    planned_gains_db: Sequence[float],
    attribution_gain_db: float,
    attribution_repeat_count: int = 3,
    minimum_detected_attribution_repeats: int = 3,
    minimum_intended_through_contrast_over_all_off_db: float = (
        DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB
    ),
    maximum_attribution_amplitude_span_db: float = (
        DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB
    ),
    maximum_attribution_phase_residual_deg: float = (
        DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG
    ),
) -> OneHotRunSummary:
    """Validate one driven-input row, including independent attribution repeats."""

    exact_input = validate_antenna_name(driven_input, "driven input")
    fixture = validate_one_hot_fixture_identity(fixture_identity)
    states = tuple(planned_states)
    if states != ONE_HOT_STATE_ORDER:
        raise ValueError("planned selector states must be ALL_OFF followed by ANT1..ANT8")
    gains = tuple(_finite_float(value, "planned TX gain") for value in planned_gains_db)
    if not gains or len(set(gains)) != len(gains):
        raise ValueError("planned TX gains must be a non-empty unique sequence")
    attribution_gain = _finite_float(attribution_gain_db, "attribution TX gain")
    if attribution_gain not in gains:
        raise ValueError("attribution TX gain must be in the planned gain ladder")
    repeat_count = _positive_int(attribution_repeat_count, "attribution repeat count")
    minimum_repeats = _positive_int(
        minimum_detected_attribution_repeats,
        "minimum detected attribution repeats",
    )
    if repeat_count < 3 or minimum_repeats < 3 or minimum_repeats > repeat_count:
        raise ValueError("attribution design requires at least three planned/detected repeats")
    minimum_contrast_db = _finite_float(
        minimum_intended_through_contrast_over_all_off_db,
        "minimum intended-through contrast over ALL_OFF",
    )
    if minimum_contrast_db < 0.0:
        raise ValueError("minimum intended-through contrast must be non-negative")
    maximum_amplitude_span_db = _finite_float(
        maximum_attribution_amplitude_span_db,
        "maximum attribution amplitude span",
    )
    maximum_phase_residual_deg = _finite_float(
        maximum_attribution_phase_residual_deg,
        "maximum attribution phase residual",
    )
    if maximum_amplitude_span_db <= 0.0 or maximum_phase_residual_deg <= 0.0:
        raise ValueError("attribution repeatability limits must be positive")

    expected = _expected_keys(
        states,
        gains,
        attribution_gain_db=attribution_gain,
        attribution_repeat_count=repeat_count,
    )
    indexed: dict[tuple[str, float, int], Mapping[str, Any]] = {}
    for result in results:
        if result.get("topology_identity") != TOPOLOGY_IDENTITY:
            raise ValueError("one-hot result has the wrong topology identity")
        if result.get("driven_input") != exact_input:
            raise ValueError("one-hot result is bound to a different driven input")
        if result.get("fixture_identity") != fixture:
            raise ValueError("one-hot result is bound to a different fixture identity")
        state = str(result.get("selector_state_name"))
        gain = _finite_float(result.get("tx_hardware_gain_db"), "observed TX gain")
        repeat_index = result.get("repeat_index")
        repeat_total = result.get("repeat_count_at_gain")
        if isinstance(repeat_index, bool) or not isinstance(repeat_index, int):
            raise ValueError("one-hot repeat index must be an integer")
        expected_repeat_total = repeat_count if gain == attribution_gain else 1
        if repeat_total != expected_repeat_total:
            raise ValueError("one-hot repeat count differs from the immutable design")
        key = (state, gain, repeat_index)
        if key not in expected:
            raise ValueError("one-hot result is not bound to an immutable state/gain/repeat")
        if key in indexed:
            raise ValueError("duplicate one-hot selector-state/gain/repeat result")
        _validate_result_measurement_evidence(
            result,
            label=f"{exact_input}/{state}/{gain:g} dB/repeat {repeat_index + 1}",
        )
        indexed[key] = result
    if expected - indexed.keys():
        raise ValueError("one-hot driven-input row is incomplete")

    raw_summaries = tuple(
        _state_summary(
            exact_input,
            state,
            [
                indexed[key]
                for key in sorted(
                    (candidate for candidate in expected if candidate[0] == state),
                    key=lambda item: (gains.index(item[1]), item[2]),
                )
            ],
            attribution_gain_db=attribution_gain,
        )
        for state in states
    )
    contrast = _intended_contrast_summary(
        indexed,
        driven_input=exact_input,
        gains=gains,
        attribution_gain_db=attribution_gain,
        attribution_repeat_count=repeat_count,
        minimum_contrast_db=minimum_contrast_db,
    )
    summaries = tuple(
        replace(
            summary,
            intended_contrast_detected_count=int(contrast["detected_count"]),
            median_selected_minus_all_off_db=float(contrast["median_contrast_db"]),
            minimum_contrast_over_all_off_db=float(contrast["minimum_over_db"]),
            attribution_contrast_detected_count=int(
                contrast["attribution_detected_count"]
            ),
            attribution_median_selected_minus_all_off_db=float(
                contrast["attribution_median_contrast_db"]
            ),
            attribution_minimum_contrast_over_all_off_db=float(
                contrast["attribution_minimum_over_db"]
            ),
            attribution_contrast_phase_coherence=(
                None
                if contrast["attribution_phase_coherence"] is None
                else float(contrast["attribution_phase_coherence"])
            ),
            attribution_median_raw_selected_to_all_off_db=float(
                contrast["attribution_median_raw_over_db"]
            ),
            attribution_minimum_raw_selected_to_all_off_db=float(
                contrast["attribution_minimum_raw_over_db"]
            ),
            attribution_median_path_contrast_db=float(
                contrast["attribution_median_path_over_db"]
            ),
            attribution_minimum_path_contrast_db=float(
                contrast["attribution_minimum_path_over_db"]
            ),
            attribution_minimum_conservative_raw_contrast_db=float(
                contrast["attribution_minimum_conservative_raw_over_db"]
            ),
            attribution_contrast_amplitude_span_db=float(
                contrast["attribution_amplitude_span_db"]
            ),
            attribution_contrast_max_phase_residual_deg=(
                None
                if contrast["attribution_max_phase_residual_deg"] is None
                else float(contrast["attribution_max_phase_residual_deg"])
            ),
            attribution_complex_increment_95pct_uncertainty_radius=float(
                contrast["attribution_uncertainty_radius"]
            ),
            attribution_complex_increment_confidence_excludes_zero=bool(
                contrast["attribution_confidence_excludes_zero"]
            ),
            strict_20db_raw_contrast_gate_passed=(
                float(contrast["attribution_minimum_conservative_raw_over_db"])
                >= STRICT_OPERATIONAL_CONTRAST_DB
            ),
            strict_20db_path_contrast_gate_passed=(
                float(contrast["attribution_minimum_over_db"])
                >= STRICT_OPERATIONAL_CONTRAST_DB
            ),
        )
        if summary.cell_role == CELL_ROLE_INTENDED_THROUGH
        else summary
        for summary in raw_summaries
    )
    rejection_reasons: list[str] = []
    for state_summary in summaries:
        if state_summary.quality_passed_count != state_summary.condition_count:
            rejection_reasons.append(
                f"{exact_input}_{state_summary.selector_state_name}_condition_quality_rejected"
            )
        if (
            state_summary.cell_role == CELL_ROLE_INTENDED_THROUGH
            and state_summary.attribution_contrast_detected_count < minimum_repeats
        ):
            rejection_reasons.append(
                f"{exact_input}_intended_through_insufficient_attribution_repeats"
            )
        if state_summary.cell_role == CELL_ROLE_INTENDED_THROUGH:
            if (
                state_summary.attribution_contrast_amplitude_span_db is None
                or state_summary.attribution_contrast_amplitude_span_db
                > maximum_amplitude_span_db
            ):
                rejection_reasons.append(
                    f"{exact_input}_attribution_amplitude_span_above_limit"
                )
            if (
                state_summary.attribution_contrast_max_phase_residual_deg is None
                or state_summary.attribution_contrast_max_phase_residual_deg
                > maximum_phase_residual_deg
            ):
                rejection_reasons.append(
                    f"{exact_input}_attribution_phase_residual_above_limit"
                )
            if not state_summary.attribution_complex_increment_confidence_excludes_zero:
                rejection_reasons.append(
                    f"{exact_input}_attribution_increment_confidence_includes_zero"
                )
    return OneHotRunSummary(
        topology_identity=TOPOLOGY_IDENTITY,
        driven_input=exact_input,
        shared_fixture_identity=fixture["shared_hardware"],
        setup_evidence_sha256=str(fixture["setup_evidence"]["file_sha256"]),
        planned_selector_state_count=len(states),
        planned_gain_count=len(gains),
        attribution_gain_db=attribution_gain,
        attribution_repeat_count=repeat_count,
        planned_condition_count=len(expected),
        observed_condition_count=len(indexed),
        minimum_detected_attribution_repeats=minimum_repeats,
        minimum_intended_through_contrast_over_all_off_db=minimum_contrast_db,
        maximum_attribution_amplitude_span_db=maximum_amplitude_span_db,
        maximum_attribution_phase_residual_deg=maximum_phase_residual_deg,
        all_off_cell_count=1,
        intended_through_cell_count=1,
        wrong_state_cell_count=7,
        states=summaries,
        quality_passed=not rejection_reasons,
        quality_rejection_reasons=tuple(rejection_reasons),
        causal_attribution_claim=False,
        operational_switching_claim=False,
    )


def _verified_row_results(
    row: Mapping[str, Any],
    *,
    expected_driven_input: str,
) -> tuple[Mapping[str, Any], ...]:
    if row.get("schema") != 1 or row.get("row_bundle_kind") != "verified_one_hot_row":
        raise ValueError("matrix row lacks the verified row-bundle contract")
    if (
        row.get("topology_identity") != TOPOLOGY_IDENTITY
        or row.get("driven_input") != expected_driven_input
        or row.get("manifest_status") != "complete"
        or row.get("physical_confirmation_verified") is not True
        or row.get("physical_confirmation_token")
        != physical_confirmation_token(expected_driven_input)
    ):
        raise ValueError("matrix row physical confirmation provenance is invalid")
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("matrix row run ID is invalid")
    plan_contract_sha = _sha256(row.get("plan_contract_sha256"), "row plan contract")
    plan_file_sha = _sha256(row.get("plan_file_sha256"), "row plan file")
    _sha256(row.get("manifest_sha256"), "row manifest")
    fixture = validate_one_hot_fixture_identity(row.get("fixture_identity"))
    validate_one_hot_matrix_identity(row.get("matrix_identity"))
    verification = row.get("verification_evidence")
    if (
        not isinstance(verification, Mapping)
        or verification.get("verification_kind")
        != "local_manifest_plan_artifact_byte_verification"
        or verification.get("manifest_file_sha256") != row.get("manifest_sha256")
        or verification.get("plan_contract_sha256") != plan_contract_sha
        or verification.get("plan_file_sha256") != plan_file_sha
        or verification.get("condition_artifacts_reverified") is not True
        or verification.get("abi2_continuity_reaudited") is not True
        or verification.get("physical_confirmation_reverified") is not True
    ):
        raise ValueError("matrix row lacks loader verification evidence")
    raw_results = row.get("results")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise ValueError("matrix row results must be a sequence")
    results: list[Mapping[str, Any]] = []
    for result in raw_results:
        if not isinstance(result, Mapping):
            raise ValueError("matrix row result is malformed")
        immutable_plan = result.get("immutable_plan")
        if (
            not isinstance(immutable_plan, Mapping)
            or immutable_plan.get("plan_contract_sha256") != plan_contract_sha
            or immutable_plan.get("plan_file_sha256") != plan_file_sha
            or result.get("fixture_identity") != fixture
        ):
            raise ValueError("matrix row result is not bound to its immutable plan")
        _sha256(result.get("artifact_data_sha256"), "artifact data")
        _sha256(result.get("artifact_metadata_sha256"), "artifact metadata")
        _sha256(result.get("condition_record_sha256"), "condition record")
        if not isinstance(result.get("artifact_id"), str) or not result["artifact_id"]:
            raise ValueError("matrix row result lacks an artifact identity")
        if isinstance(result.get("stream_id"), bool) or not isinstance(
            result.get("stream_id"), int
        ):
            raise ValueError("matrix row result lacks an ABI2 stream identity")
        results.append(result)
    return tuple(results)


def summarize_complete_one_hot_matrix(
    verified_rows: Sequence[VerifiedOneHotRowBundle],
    *,
    planned_gains_db: Sequence[float],
    attribution_gain_db: float,
    attribution_repeat_count: int = 3,
    minimum_detected_attribution_repeats: int = 3,
    minimum_intended_through_contrast_over_all_off_db: float = (
        DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB
    ),
    maximum_attribution_amplitude_span_db: float = (
        DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB
    ),
    maximum_attribution_phase_residual_deg: float = (
        DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG
    ),
) -> OneHotMatrixSummary:
    """Combine exactly eight independently verified physical row bundles."""

    if len(verified_rows) != len(ANTENNA_STATES):
        raise ValueError("complete matrix requires exactly eight verified row bundles")
    by_input: dict[str, Mapping[str, Any]] = {}
    run_ids: set[str] = set()
    manifest_hashes: set[str] = set()
    setup_evidence_hashes: set[str] = set()
    artifact_ids: set[str] = set()
    artifact_data_hashes: set[str] = set()
    stream_ids: set[int] = set()
    row_results: dict[str, tuple[Mapping[str, Any], ...]] = {}
    shared_fixture_identity: Mapping[str, str] | None = None
    shared_matrix_identity: Mapping[str, Any] | None = None
    for verified_row in verified_rows:
        if not isinstance(verified_row, VerifiedOneHotRowBundle):
            raise ValueError("matrix rows must come from the file-reading row loader")
        row = verified_row.document
        driven_input = validate_antenna_name(row.get("driven_input"), "matrix driven input")
        if driven_input in by_input:
            raise ValueError("matrix contains duplicate driven-input rows")
        results = _verified_row_results(row, expected_driven_input=driven_input)
        fixture = validate_one_hot_fixture_identity(row.get("fixture_identity"))
        matrix_identity = validate_one_hot_matrix_identity(row.get("matrix_identity"))
        shared = fixture["shared_hardware"]
        evidence_sha = str(fixture["setup_evidence"]["file_sha256"])
        if shared_fixture_identity is None:
            shared_fixture_identity = shared
        elif shared != shared_fixture_identity:
            raise ValueError("matrix rows do not share one characterized fixture identity")
        if shared_matrix_identity is None:
            shared_matrix_identity = matrix_identity
        elif matrix_identity != shared_matrix_identity:
            raise ValueError(
                "matrix rows do not share one DUT/control/acquisition identity"
            )
        if evidence_sha in setup_evidence_hashes:
            raise ValueError("matrix rows reuse setup evidence instead of recording each setup")
        setup_evidence_hashes.add(evidence_sha)
        run_id = str(row["run_id"])
        manifest_sha = str(row["manifest_sha256"])
        if run_id in run_ids or manifest_sha in manifest_hashes:
            raise ValueError("matrix rows do not have independent run/manifest provenance")
        run_ids.add(run_id)
        manifest_hashes.add(manifest_sha)
        for result in results:
            artifact_id = str(result["artifact_id"])
            data_sha = str(result["artifact_data_sha256"])
            stream_id = int(result["stream_id"])
            if (
                artifact_id in artifact_ids
                or data_sha in artifact_data_hashes
                or stream_id in stream_ids
            ):
                raise ValueError("matrix reuses a raw artifact or ABI2 stream identity")
            artifact_ids.add(artifact_id)
            artifact_data_hashes.add(data_sha)
            stream_ids.add(stream_id)
        by_input[driven_input] = row
        row_results[driven_input] = results
    if set(by_input) != set(ANTENNA_STATES):
        raise ValueError("matrix does not contain every driven input ANT1..ANT8")

    gains = tuple(_finite_float(value, "planned TX gain") for value in planned_gains_db)
    runs = tuple(
        summarize_one_hot_run(
            row_results[driven_input],
            driven_input=driven_input,
            fixture_identity=by_input[driven_input]["fixture_identity"],
            planned_gains_db=gains,
            attribution_gain_db=attribution_gain_db,
            attribution_repeat_count=attribution_repeat_count,
            minimum_detected_attribution_repeats=minimum_detected_attribution_repeats,
            minimum_intended_through_contrast_over_all_off_db=(
                minimum_intended_through_contrast_over_all_off_db
            ),
            maximum_attribution_amplitude_span_db=(
                maximum_attribution_amplitude_span_db
            ),
            maximum_attribution_phase_residual_deg=(
                maximum_attribution_phase_residual_deg
            ),
        )
        for driven_input in ANTENNA_STATES
    )
    rejection_reasons = tuple(
        f"{run.driven_input}:{reason}"
        for run in runs
        for reason in run.quality_rejection_reasons
    )
    conditions_per_cell = len(gains) + attribution_repeat_count - 1
    expected_count = 72 * conditions_per_cell
    observed_count = sum(len(results) for results in row_results.values())
    return OneHotMatrixSummary(
        topology_identity=TOPOLOGY_IDENTITY,
        matrix_identity={} if shared_matrix_identity is None else shared_matrix_identity,
        shared_fixture_identity=(
            {} if shared_fixture_identity is None else shared_fixture_identity
        ),
        setup_evidence_count=len(setup_evidence_hashes),
        independently_confirmed_row_count=8,
        planned_driven_input_count=8,
        planned_selector_state_count=9,
        planned_gain_count=len(gains),
        attribution_gain_db=float(attribution_gain_db),
        attribution_repeat_count=attribution_repeat_count,
        planned_condition_count=expected_count,
        observed_condition_count=observed_count,
        all_off_cell_count=8,
        intended_through_cell_count=8,
        wrong_state_cell_count=56,
        all_off_condition_count=8 * conditions_per_cell,
        intended_through_condition_count=8 * conditions_per_cell,
        wrong_state_condition_count=56 * conditions_per_cell,
        runs=runs,
        quality_passed=not rejection_reasons,
        quality_rejection_reasons=rejection_reasons,
        causal_attribution_claim=False,
        operational_switching_claim=False,
    )


__all__ = [
    "ALL_OFF_GPIO_CODE",
    "ALL_OFF_STATE",
    "ANTENNA_GPIO_CODES",
    "ANTENNA_STATES",
    "CELL_ROLE_ALL_OFF",
    "CELL_ROLE_INTENDED_THROUGH",
    "CELL_ROLE_WRONG_STATE",
    "DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB",
    "DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB",
    "DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG",
    "ONE_HOT_STATE_ORDER",
    "TOPOLOGY_IDENTITY",
    "STRICT_OPERATIONAL_CONTRAST_DB",
    "OneHotMatrixSummary",
    "OneHotRunSummary",
    "OneHotStateSummary",
    "VerifiedOneHotRowBundle",
    "one_hot_cell_role",
    "physical_confirmation_token",
    "summarize_complete_one_hot_matrix",
    "summarize_one_hot_run",
    "validate_antenna_name",
    "validate_one_hot_fixture_identity",
    "validate_one_hot_matrix_identity",
    "validate_one_hot_state_codes",
]
