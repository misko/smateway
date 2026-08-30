"""Protected, gain-normalized TX/RX port-pair matrix contracts.

The module is deliberately hardware-free.  It validates an immutable fixture,
derives the exact four TX1/TX2 by RX1/RX2 cell topologies, admits five
source-distinct repeats per cell, and normalizes every transfer at declared RF
reference planes.  Raw RX-channel amplitudes are never treated as port-pair
measurements.

RX1 protection is a permanent fixture invariant.  When RX1 is the terminated
test receiver, its protection remains installed and the termination is placed
at the protected test reference plane.  When RX2 is the conducted reference, a
second independently identified 5.8-GHz-rated attenuation chain is required;
the RX1 chain is never moved or bypassed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import atan2, degrees, isfinite, log10
from numbers import Complex, Integral, Real
from typing import Any

import numpy as np
import numpy.typing as npt

TX_PORTS = ("TX1", "TX2")
RX_PORTS = ("RX1", "RX2")
CELL_IDS = tuple(f"{tx}_{rx}" for tx in TX_PORTS for rx in RX_PORTS)
REPEAT_COUNT = 5

CENTER_FREQUENCY_HZ = 5_800_000_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
PREFLIGHT_TX_GAIN_DB = -40.0
CAPTURE_TX_GAIN_DB = -20.0
DDS_SCALE = 0.125
RECEIVER_GAIN_DB = 40.0
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
DEFAULT_REQUIRED_HEADROOM_DB = 6.0
DEFAULT_BOOTSTRAP_DRAWS = 32_768

FIXTURE_KIND = "5g8_protected_tx_rx_port_pair_matrix_fixture"
CALIBRATION_KIND = "5g8_port_pair_receiver_reference_calibration"
NORMALIZED_OBSERVATION_KIND = "5g8_port_pair_normalized_observation"

_EPSILON = 1e-15


class PortPairMatrixError(ValueError):
    """The fixture, capture evidence, or normalization contract is unsafe."""


def canonical_sha256(value: object) -> str:
    """Hash one finite JSON-compatible document using canonical bytes."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PortPairMatrixError("identity must contain only finite JSON values") from error
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortPairMatrixError(f"{label} must be a nonempty string")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PortPairMatrixError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_commit(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PortPairMatrixError(f"{label} must be a full lowercase Git commit")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PortPairMatrixError(f"{label} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise PortPairMatrixError(f"{label} must be finite")
    return result


def _complex(value: object, label: str) -> complex:
    if isinstance(value, bool) or not isinstance(value, Complex):
        raise PortPairMatrixError(f"{label} must be complex")
    result = complex(value)
    if not isfinite(result.real) or not isfinite(result.imag):
        raise PortPairMatrixError(f"{label} must be finite")
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PortPairMatrixError(
            f"{label} keys differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _port_other(port: str, values: tuple[str, str]) -> str:
    if port not in values:
        raise PortPairMatrixError(f"invalid port {port!r}; expected one of {values}")
    return values[1] if port == values[0] else values[0]


@dataclass(frozen=True, slots=True)
class AttenuationChain:
    chain_id: str
    identity_sha256: str
    assigned_receiver: str
    rated_min_hz: int
    rated_max_hz: int
    attenuation_db: float
    attenuation_tolerance_db: float
    maximum_input_dbm: float
    permanently_installed: bool
    removal_forbidden: bool
    independent_of_rx1_chain: bool

    @property
    def conservative_attenuation_db(self) -> float:
        return self.attenuation_db - self.attenuation_tolerance_db


@dataclass(frozen=True, slots=True)
class Termination:
    termination_id: str
    identity_sha256: str
    rated_min_hz: int
    rated_max_hz: int
    impedance_ohm: float
    maximum_input_dbm: float


@dataclass(frozen=True, slots=True)
class ReferenceDistribution:
    identity_sha256: str
    active_tx_reference_plane_sha256: str
    minimum_path_loss_db: float
    unused_output_termination: Termination


@dataclass(frozen=True, slots=True)
class CellContract:
    cell_id: str
    active_tx: str
    inactive_tx: str
    test_receiver: str
    reference_receiver: str
    inactive_tx_termination_sha256: str
    test_receiver_termination_sha256: str
    reference_chain_sha256: str
    rx1_protection_sha256: str
    active_tx_reference_plane_sha256: str
    test_receiver_reference_plane_sha256: str
    reference_receiver_reference_plane_sha256: str
    path_attenuation_before_reference_receiver_db: float
    topology_token: str
    topology_canonical_json: str
    topology_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedFixture:
    fixture_id: str
    fixture_sha256: str
    fixed_graph_sha256: str
    center_frequency_hz: int
    receiver_input_limit_dbm: float
    required_safety_margin_db: float
    rx1_protection: AttenuationChain
    rx2_reference_chain: AttenuationChain
    reference_distribution: ReferenceDistribution
    inactive_tx_terminations: tuple[Termination, Termination]
    test_receiver_terminations: tuple[Termination, Termination]
    test_reference_plane_sha256s: tuple[str, str]
    reference_plane_sha256s: tuple[str, str]
    cells: tuple[CellContract, ...]

    def cell(self, cell_id: str) -> CellContract:
        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        raise KeyError(cell_id)


@dataclass(frozen=True, slots=True)
class ReceiverCalibration:
    receiver: str
    test_receiver_response: complex
    test_response_evidence_sha256: str
    reference_chain_response: complex
    reference_response_evidence_sha256: str
    reference_chain_sha256: str
    test_reference_plane_sha256: str
    reference_plane_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedCalibration:
    calibration_id: str
    calibration_sha256: str
    fixture_sha256: str
    center_frequency_hz: int
    receivers: tuple[ReceiverCalibration, ReceiverCalibration]

    def receiver(self, name: str) -> ReceiverCalibration:
        for receiver in self.receivers:
            if receiver.receiver == name:
                return receiver
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class HeadroomPreflight:
    preflight_tx_gain_db: float
    capture_tx_gain_db: float
    clip_threshold_abs_counts: float
    peak_abs_counts_by_receiver: tuple[float, float]
    clipped_sample_count_by_receiver: tuple[int, int]
    required_projected_headroom_db: float = DEFAULT_REQUIRED_HEADROOM_DB


@dataclass(frozen=True, slots=True)
class HeadroomAdmission:
    passed: bool
    projected_peak_abs_counts_by_receiver: tuple[float, float]
    projected_headroom_db_by_receiver: tuple[float, float]
    rejection_reasons: tuple[str, ...]
    method: str


@dataclass(frozen=True, slots=True)
class CaptureIdentity:
    run_id: str
    stream_id: str
    artifact_sha256: str
    raw_iq_sha256: str
    metadata_sha256: str
    condition_record_sha256: str
    leaf_source_sha256s: tuple[str, ...]
    leaf_source_set_sha256: str


@dataclass(frozen=True, slots=True)
class ComplexDetection:
    detected: bool
    phasor: complex | None
    magnitude_upper_bound: float | None


@dataclass(frozen=True, slots=True)
class PortPairRepeat:
    cell_id: str
    repeat_index: int
    plan_sha256: str
    fixture_sha256: str
    calibration_sha256: str
    topology_sha256: str
    source_commit: str
    dependency_commit: str
    native_attestation_sha256: str
    preflight_capture: CaptureIdentity
    main_capture: CaptureIdentity
    headroom_preflight: HeadroomPreflight
    tx_gain_readback_db_by_channel: tuple[float, float]
    dds_scale_readback: tuple[float, ...]
    clipped_sample_count_by_receiver: tuple[int, int]
    inactive_tx_termination_sha256: str
    test_receiver_termination_sha256: str
    reference_chain_sha256: str
    rx1_protection_sha256: str
    test_receiver_tone: ComplexDetection
    reference_receiver_tone: complex
    reference_tone_snr_db: float
    continuity_passed: bool
    quality_passed: bool
    final_mute_passed: bool
    final_tx_gain_readback_db_by_channel: tuple[float, float]
    final_dds_scale_readback: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ComplexInterval:
    center: complex
    amplitude_db: float
    phase_deg: float
    amplitude_95_interval_db: tuple[float, float]
    phase_95_interval_deg: tuple[float, float]
    covariance_real_imag: tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class NormalizedCellResult:
    cell_id: str
    active_tx: str
    test_receiver: str
    reference_receiver: str
    repeat_count: int
    all_test_tones_detected: bool
    normalized_transfer: ComplexInterval | None
    normalized_magnitude_upper_bound: float | None
    normalized_magnitude_upper_bound_db: float | None
    phase_available: bool
    raw_channel_amplitudes_comparable: bool
    normalization_equation: str
    topology_sha256: str
    test_response_evidence_sha256: str
    reference_response_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class PortPairMatrixResult:
    fixture_sha256: str
    calibration_sha256: str
    plan_sha256: str
    exact_four_cells_verified: bool
    five_source_distinct_repeats_per_cell_verified: bool
    rx1_protection_never_removed_or_bypassed: bool
    second_rx2_reference_chain_verified: bool
    receiver_reference_gain_normalization_applied: bool
    raw_channel_comparison_forbidden: bool
    cells: tuple[NormalizedCellResult, ...]
    statistical_method: str

    def cell(self, cell_id: str) -> NormalizedCellResult:
        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        raise KeyError(cell_id)


def _attenuation_chain(value: object, *, receiver: str, label: str) -> AttenuationChain:
    if not isinstance(value, Mapping):
        raise PortPairMatrixError(f"{label} must be an object")
    _exact_keys(
        value,
        {
            "chain_id",
            "identity_sha256",
            "assigned_receiver",
            "rated_min_hz",
            "rated_max_hz",
            "attenuation_db",
            "attenuation_tolerance_db",
            "maximum_input_dbm",
            "permanently_installed",
            "removal_forbidden",
            "independent_of_rx1_chain",
        },
        label,
    )
    assigned = value["assigned_receiver"]
    if assigned != receiver:
        raise PortPairMatrixError(f"{label} must be assigned to {receiver}")
    minimum = value["rated_min_hz"]
    maximum = value["rated_max_hz"]
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, Integral)
        or isinstance(maximum, bool)
        or not isinstance(maximum, Integral)
        or not int(minimum) <= CENTER_FREQUENCY_HZ <= int(maximum)
    ):
        raise PortPairMatrixError(f"{label} is not rated through 5.8 GHz")
    attenuation = _finite(value["attenuation_db"], f"{label} attenuation")
    tolerance = _finite(value["attenuation_tolerance_db"], f"{label} tolerance")
    if attenuation <= 0.0 or tolerance < 0.0 or attenuation - tolerance <= 0.0:
        raise PortPairMatrixError(f"{label} conservative attenuation must be positive")
    for field in ("permanently_installed", "removal_forbidden", "independent_of_rx1_chain"):
        if not isinstance(value[field], bool):
            raise PortPairMatrixError(f"{label} {field} must be boolean")
    return AttenuationChain(
        chain_id=_identifier(value["chain_id"], f"{label} ID"),
        identity_sha256=_sha256(value["identity_sha256"], f"{label} identity"),
        assigned_receiver=receiver,
        rated_min_hz=int(minimum),
        rated_max_hz=int(maximum),
        attenuation_db=attenuation,
        attenuation_tolerance_db=tolerance,
        maximum_input_dbm=_finite(value["maximum_input_dbm"], f"{label} power limit"),
        permanently_installed=value["permanently_installed"],
        removal_forbidden=value["removal_forbidden"],
        independent_of_rx1_chain=value["independent_of_rx1_chain"],
    )


def _termination(value: object, label: str) -> Termination:
    if not isinstance(value, Mapping):
        raise PortPairMatrixError(f"{label} must be an object")
    _exact_keys(
        value,
        {
            "termination_id",
            "identity_sha256",
            "rated_min_hz",
            "rated_max_hz",
            "impedance_ohm",
            "maximum_input_dbm",
        },
        label,
    )
    minimum = value["rated_min_hz"]
    maximum = value["rated_max_hz"]
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, Integral)
        or isinstance(maximum, bool)
        or not isinstance(maximum, Integral)
        or not int(minimum) <= CENTER_FREQUENCY_HZ <= int(maximum)
    ):
        raise PortPairMatrixError(f"{label} is not rated through 5.8 GHz")
    impedance = _finite(value["impedance_ohm"], f"{label} impedance")
    if not 49.0 <= impedance <= 51.0:
        raise PortPairMatrixError(f"{label} must be a 50-ohm termination")
    return Termination(
        termination_id=_identifier(value["termination_id"], f"{label} ID"),
        identity_sha256=_sha256(value["identity_sha256"], f"{label} identity"),
        rated_min_hz=int(minimum),
        rated_max_hz=int(maximum),
        impedance_ohm=impedance,
        maximum_input_dbm=_finite(value["maximum_input_dbm"], f"{label} power limit"),
    )


def _port_mapping(value: object, ports: tuple[str, str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or tuple(value) != ports:
        raise PortPairMatrixError(f"{label} must bind exactly {ports[0]} and {ports[1]}")
    return value


def _topology_payload(
    *,
    fixture_id: str,
    fixed_graph_sha256: str,
    active_tx: str,
    inactive_tx: str,
    test_receiver: str,
    reference_receiver: str,
    inactive_termination: Termination,
    test_termination: Termination,
    reference_chain: AttenuationChain,
    rx1_chain: AttenuationChain,
    distribution: ReferenceDistribution,
    test_reference_plane_sha256: str,
    reference_plane_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "topology_kind": "protected_tx_rx_port_pair_cell",
        "fixture_id": fixture_id,
        "fixed_graph_sha256": fixed_graph_sha256,
        "active_tx": active_tx,
        "inactive_tx": {
            "port": inactive_tx,
            "physical_termination_sha256": inactive_termination.identity_sha256,
            "digital_gain_db": -80.0,
            "all_dds_scales_zero": True,
        },
        "test_receiver": {
            "port": test_receiver,
            "termination_sha256": test_termination.identity_sha256,
            "reference_plane_sha256": test_reference_plane_sha256,
            "has_conducted_stimulus": False,
        },
        "reference_receiver": {
            "port": reference_receiver,
            "attenuation_chain_sha256": reference_chain.identity_sha256,
            "reference_plane_sha256": reference_plane_sha256,
            "has_conducted_stimulus": True,
        },
        "rx1_protection": {
            "identity_sha256": rx1_chain.identity_sha256,
            "remains_installed": True,
            "bypass_forbidden": True,
        },
        "reference_distribution": {
            "identity_sha256": distribution.identity_sha256,
            "active_tx_reference_plane_sha256": (distribution.active_tx_reference_plane_sha256),
            "unused_output_termination_sha256": (
                distribution.unused_output_termination.identity_sha256
            ),
        },
    }


def validate_fixture(document: Mapping[str, Any]) -> ValidatedFixture:
    """Validate the immutable two-protection-chain fixture and derive four cells."""

    _exact_keys(
        document,
        {
            "schema",
            "fixture_kind",
            "fixture_id",
            "center_frequency_hz",
            "fixed_graph_sha256",
            "receiver_input_limit_dbm",
            "required_safety_margin_db",
            "rx1_protection",
            "rx2_reference_chain",
            "reference_distribution",
            "inactive_tx_terminations",
            "test_receiver_terminations",
            "test_reference_plane_sha256s",
            "reference_plane_sha256s",
        },
        "port-pair fixture",
    )
    if document["schema"] != 1 or document["fixture_kind"] != FIXTURE_KIND:
        raise PortPairMatrixError("fixture schema or kind is wrong")
    center = document["center_frequency_hz"]
    if (
        isinstance(center, bool)
        or not isinstance(center, Integral)
        or int(center) != CENTER_FREQUENCY_HZ
    ):
        raise PortPairMatrixError("fixture center frequency must be exactly 5.8 GHz")
    fixture_id = _identifier(document["fixture_id"], "fixture ID")
    fixed_graph = _sha256(document["fixed_graph_sha256"], "fixed graph hash")
    rx1_chain = _attenuation_chain(
        document["rx1_protection"], receiver="RX1", label="RX1 protection"
    )
    rx2_chain = _attenuation_chain(
        document["rx2_reference_chain"], receiver="RX2", label="RX2 reference chain"
    )
    if not rx1_chain.permanently_installed or not rx1_chain.removal_forbidden:
        raise PortPairMatrixError("RX1 protection must be permanent and removal-forbidden")
    if not rx2_chain.independent_of_rx1_chain:
        raise PortPairMatrixError("RX2 reference requires a second independent attenuation chain")
    if (
        rx1_chain.identity_sha256 == rx2_chain.identity_sha256
        or rx1_chain.chain_id == rx2_chain.chain_id
    ):
        raise PortPairMatrixError("RX1 and RX2 attenuation chains must be physically distinct")

    raw_distribution = document["reference_distribution"]
    if not isinstance(raw_distribution, Mapping):
        raise PortPairMatrixError("reference distribution must be an object")
    _exact_keys(
        raw_distribution,
        {
            "identity_sha256",
            "active_tx_reference_plane_sha256",
            "minimum_path_loss_db",
            "unused_output_termination",
        },
        "reference distribution",
    )
    distribution = ReferenceDistribution(
        identity_sha256=_sha256(raw_distribution["identity_sha256"], "distribution identity"),
        active_tx_reference_plane_sha256=_sha256(
            raw_distribution["active_tx_reference_plane_sha256"],
            "active-TX reference plane",
        ),
        minimum_path_loss_db=_finite(
            raw_distribution["minimum_path_loss_db"], "distribution minimum path loss"
        ),
        unused_output_termination=_termination(
            raw_distribution["unused_output_termination"], "distribution output termination"
        ),
    )
    if distribution.minimum_path_loss_db < 0.0:
        raise PortPairMatrixError("distribution minimum path loss cannot be negative")

    raw_tx_terms = _port_mapping(
        document["inactive_tx_terminations"], TX_PORTS, "inactive-TX terminations"
    )
    tx_terms = tuple(_termination(raw_tx_terms[port], f"{port} termination") for port in TX_PORTS)
    raw_rx_terms = _port_mapping(
        document["test_receiver_terminations"], RX_PORTS, "test-receiver terminations"
    )
    rx_terms = tuple(_termination(raw_rx_terms[port], f"{port} termination") for port in RX_PORTS)
    all_termination_hashes = {
        *(item.identity_sha256 for item in tx_terms),
        *(item.identity_sha256 for item in rx_terms),
        distribution.unused_output_termination.identity_sha256,
    }
    if len(all_termination_hashes) != 5:
        raise PortPairMatrixError("each fixture termination must have a distinct identity")

    raw_test_planes = _port_mapping(
        document["test_reference_plane_sha256s"], RX_PORTS, "test reference planes"
    )
    test_planes = tuple(
        _sha256(raw_test_planes[port], f"{port} test reference plane") for port in RX_PORTS
    )
    raw_reference_planes = _port_mapping(
        document["reference_plane_sha256s"], RX_PORTS, "reference receiver planes"
    )
    reference_planes = tuple(
        _sha256(raw_reference_planes[port], f"{port} reference plane") for port in RX_PORTS
    )
    receiver_limit = _finite(document["receiver_input_limit_dbm"], "receiver input limit")
    safety_margin = _finite(document["required_safety_margin_db"], "safety margin")
    if safety_margin < 0.0:
        raise PortPairMatrixError("safety margin cannot be negative")

    tx_term_by_port = dict(zip(TX_PORTS, tx_terms, strict=True))
    rx_term_by_port = dict(zip(RX_PORTS, rx_terms, strict=True))
    test_plane_by_port = dict(zip(RX_PORTS, test_planes, strict=True))
    reference_plane_by_port = dict(zip(RX_PORTS, reference_planes, strict=True))
    chain_by_receiver = {"RX1": rx1_chain, "RX2": rx2_chain}
    cells: list[CellContract] = []
    for active_tx in TX_PORTS:
        inactive_tx = _port_other(active_tx, TX_PORTS)
        for test_receiver in RX_PORTS:
            reference_receiver = _port_other(test_receiver, RX_PORTS)
            chain = chain_by_receiver[reference_receiver]
            topology = _topology_payload(
                fixture_id=fixture_id,
                fixed_graph_sha256=fixed_graph,
                active_tx=active_tx,
                inactive_tx=inactive_tx,
                test_receiver=test_receiver,
                reference_receiver=reference_receiver,
                inactive_termination=tx_term_by_port[inactive_tx],
                test_termination=rx_term_by_port[test_receiver],
                reference_chain=chain,
                rx1_chain=rx1_chain,
                distribution=distribution,
                test_reference_plane_sha256=test_plane_by_port[test_receiver],
                reference_plane_sha256=reference_plane_by_port[reference_receiver],
            )
            canonical = json.dumps(topology, sort_keys=True, separators=(",", ":"))
            cell_id = f"{active_tx}_{test_receiver}"
            cells.append(
                CellContract(
                    cell_id=cell_id,
                    active_tx=active_tx,
                    inactive_tx=inactive_tx,
                    test_receiver=test_receiver,
                    reference_receiver=reference_receiver,
                    inactive_tx_termination_sha256=tx_term_by_port[inactive_tx].identity_sha256,
                    test_receiver_termination_sha256=rx_term_by_port[test_receiver].identity_sha256,
                    reference_chain_sha256=chain.identity_sha256,
                    rx1_protection_sha256=rx1_chain.identity_sha256,
                    active_tx_reference_plane_sha256=distribution.active_tx_reference_plane_sha256,
                    test_receiver_reference_plane_sha256=test_plane_by_port[test_receiver],
                    reference_receiver_reference_plane_sha256=(
                        reference_plane_by_port[reference_receiver]
                    ),
                    path_attenuation_before_reference_receiver_db=(
                        distribution.minimum_path_loss_db + chain.conservative_attenuation_db
                    ),
                    topology_token=(
                        f"PROTECTED_{active_tx}_TEST_{test_receiver}_REFERENCE_{reference_receiver}"
                    ),
                    topology_canonical_json=canonical,
                    topology_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
                )
            )
    fixture_sha = canonical_sha256(document)
    fixture = ValidatedFixture(
        fixture_id=fixture_id,
        fixture_sha256=fixture_sha,
        fixed_graph_sha256=fixed_graph,
        center_frequency_hz=int(center),
        receiver_input_limit_dbm=receiver_limit,
        required_safety_margin_db=safety_margin,
        rx1_protection=rx1_chain,
        rx2_reference_chain=rx2_chain,
        reference_distribution=distribution,
        inactive_tx_terminations=(tx_terms[0], tx_terms[1]),
        test_receiver_terminations=(rx_terms[0], rx_terms[1]),
        test_reference_plane_sha256s=(test_planes[0], test_planes[1]),
        reference_plane_sha256s=(reference_planes[0], reference_planes[1]),
        cells=tuple(cells),
    )
    for cell in fixture.cells:
        validate_cell_rf_safety(fixture, cell)
    return fixture


def validate_cell_rf_safety(
    fixture: ValidatedFixture,
    cell: CellContract,
    *,
    tx_gain_db: float = CAPTURE_TX_GAIN_DB,
    dds_scale: float = DDS_SCALE,
    source_peak_output_bound_dbm: float = SOURCE_PEAK_OUTPUT_BOUND_DBM,
) -> float:
    """Return worst-case reference-input power after enforcing the margin."""

    gain = _finite(tx_gain_db, "TX gain")
    scale = _finite(dds_scale, "DDS scale")
    source = _finite(source_peak_output_bound_dbm, "source peak output bound")
    if not 0.0 < scale <= 1.0:
        raise PortPairMatrixError("DDS scale must lie in (0, 1]")
    worst_case = (
        source + gain + 20.0 * log10(scale) - cell.path_attenuation_before_reference_receiver_db
    )
    distribution_output = (
        source + gain + 20.0 * log10(scale) - fixture.reference_distribution.minimum_path_loss_db
    )
    reference_chain = (
        fixture.rx1_protection if cell.reference_receiver == "RX1" else fixture.rx2_reference_chain
    )
    chain_allowed = reference_chain.maximum_input_dbm - fixture.required_safety_margin_db
    if distribution_output > chain_allowed:
        raise PortPairMatrixError(
            f"{cell.cell_id} can overdrive reference attenuator input: "
            f"{distribution_output:.2f} dBm exceeds {chain_allowed:.2f} dBm"
        )
    termination_allowed = (
        fixture.reference_distribution.unused_output_termination.maximum_input_dbm
        - fixture.required_safety_margin_db
    )
    if distribution_output > termination_allowed:
        raise PortPairMatrixError(
            f"{cell.cell_id} can overdrive the distribution output termination"
        )
    allowed = fixture.receiver_input_limit_dbm - fixture.required_safety_margin_db
    if worst_case > allowed:
        raise PortPairMatrixError(
            f"{cell.cell_id} reference chain is unsafe: {worst_case:.2f} dBm exceeds "
            f"{allowed:.2f} dBm"
        )
    return worst_case


def _calibration_response(value: object, label: str) -> complex:
    if not isinstance(value, Mapping):
        raise PortPairMatrixError(f"{label} must be an object")
    _exact_keys(value, {"real", "imag"}, label)
    response = complex(
        _finite(value["real"], f"{label} real"),
        _finite(value["imag"], f"{label} imag"),
    )
    if abs(response) <= _EPSILON:
        raise PortPairMatrixError(f"{label} must be nonzero")
    return response


def validate_calibration(
    document: Mapping[str, Any], fixture: ValidatedFixture
) -> ValidatedCalibration:
    """Bind complex receiver/reference-chain response to the exact fixture planes."""

    _exact_keys(
        document,
        {
            "schema",
            "calibration_kind",
            "calibration_id",
            "fixture_sha256",
            "center_frequency_hz",
            "receiver_calibrations",
        },
        "port-pair calibration",
    )
    if document["schema"] != 1 or document["calibration_kind"] != CALIBRATION_KIND:
        raise PortPairMatrixError("calibration schema or kind is wrong")
    if document["fixture_sha256"] != fixture.fixture_sha256:
        raise PortPairMatrixError("calibration is stale for this fixture")
    if document["center_frequency_hz"] != fixture.center_frequency_hz:
        raise PortPairMatrixError("calibration frequency differs from the fixture")
    raw_receivers = _port_mapping(
        document["receiver_calibrations"], RX_PORTS, "receiver calibrations"
    )
    chains = {"RX1": fixture.rx1_protection, "RX2": fixture.rx2_reference_chain}
    test_planes = dict(zip(RX_PORTS, fixture.test_reference_plane_sha256s, strict=True))
    reference_planes = dict(zip(RX_PORTS, fixture.reference_plane_sha256s, strict=True))
    receivers: list[ReceiverCalibration] = []
    for receiver in RX_PORTS:
        raw = raw_receivers[receiver]
        if not isinstance(raw, Mapping):
            raise PortPairMatrixError(f"{receiver} calibration must be an object")
        _exact_keys(
            raw,
            {
                "test_receiver_response",
                "test_response_evidence_sha256",
                "reference_chain_response",
                "reference_response_evidence_sha256",
                "reference_chain_sha256",
                "test_reference_plane_sha256",
                "reference_plane_sha256",
            },
            f"{receiver} calibration",
        )
        if raw["reference_chain_sha256"] != chains[receiver].identity_sha256:
            raise PortPairMatrixError(f"{receiver} calibration uses the wrong attenuator")
        if raw["test_reference_plane_sha256"] != test_planes[receiver]:
            raise PortPairMatrixError(f"{receiver} test calibration plane is wrong")
        if raw["reference_plane_sha256"] != reference_planes[receiver]:
            raise PortPairMatrixError(f"{receiver} reference calibration plane is wrong")
        receivers.append(
            ReceiverCalibration(
                receiver=receiver,
                test_receiver_response=_calibration_response(
                    raw["test_receiver_response"], f"{receiver} test response"
                ),
                test_response_evidence_sha256=_sha256(
                    raw["test_response_evidence_sha256"],
                    f"{receiver} test-response evidence",
                ),
                reference_chain_response=_calibration_response(
                    raw["reference_chain_response"], f"{receiver} reference response"
                ),
                reference_response_evidence_sha256=_sha256(
                    raw["reference_response_evidence_sha256"],
                    f"{receiver} reference-response evidence",
                ),
                reference_chain_sha256=chains[receiver].identity_sha256,
                test_reference_plane_sha256=test_planes[receiver],
                reference_plane_sha256=reference_planes[receiver],
            )
        )
    return ValidatedCalibration(
        calibration_id=_identifier(document["calibration_id"], "calibration ID"),
        calibration_sha256=canonical_sha256(document),
        fixture_sha256=fixture.fixture_sha256,
        center_frequency_hz=fixture.center_frequency_hz,
        receivers=(receivers[0], receivers[1]),
    )


def evaluate_headroom_preflight(preflight: HeadroomPreflight) -> HeadroomAdmission:
    """Project the weak preflight peaks to the planned main TX gain."""

    if not isinstance(preflight, HeadroomPreflight):
        raise PortPairMatrixError("headroom preflight has the wrong type")
    weak_gain = _finite(preflight.preflight_tx_gain_db, "preflight TX gain")
    main_gain = _finite(preflight.capture_tx_gain_db, "capture TX gain")
    if main_gain < weak_gain:
        raise PortPairMatrixError("capture TX gain must not be weaker than the preflight")
    clip = _finite(preflight.clip_threshold_abs_counts, "ADC clip threshold")
    required = _finite(preflight.required_projected_headroom_db, "required headroom")
    if clip <= 0.0 or required < 0.0:
        raise PortPairMatrixError("clip threshold must be positive and headroom nonnegative")
    if (
        len(preflight.peak_abs_counts_by_receiver) != 2
        or len(preflight.clipped_sample_count_by_receiver) != 2
    ):
        raise PortPairMatrixError("preflight must describe exactly RX1 and RX2")
    scale = 10.0 ** ((main_gain - weak_gain) / 20.0)
    projected: list[float] = []
    headroom: list[float] = []
    reasons: list[str] = []
    for receiver_index, (raw_peak, raw_clipped) in enumerate(
        zip(
            preflight.peak_abs_counts_by_receiver,
            preflight.clipped_sample_count_by_receiver,
            strict=True,
        ),
        start=1,
    ):
        peak = _finite(raw_peak, f"RX{receiver_index} preflight peak")
        if peak < 0.0:
            raise PortPairMatrixError("preflight peak cannot be negative")
        if (
            isinstance(raw_clipped, bool)
            or not isinstance(raw_clipped, Integral)
            or raw_clipped < 0
        ):
            raise PortPairMatrixError("preflight clipped-sample count must be nonnegative integer")
        projected_peak = peak * scale
        projected.append(projected_peak)
        available_headroom = (
            float("inf") if projected_peak <= 0.0 else 20.0 * log10(clip / projected_peak)
        )
        headroom.append(available_headroom)
        if raw_clipped:
            reasons.append(f"RX{receiver_index}_preflight_clipped")
        if peak >= clip:
            reasons.append(f"RX{receiver_index}_preflight_reached_clip_threshold")
        if available_headroom < required:
            reasons.append(f"RX{receiver_index}_projected_headroom_below_minimum")
    return HeadroomAdmission(
        passed=not reasons,
        projected_peak_abs_counts_by_receiver=(projected[0], projected[1]),
        projected_headroom_db_by_receiver=(headroom[0], headroom[1]),
        rejection_reasons=tuple(reasons),
        method="weak_gain_peak_projection_to_main_gain_with_per_receiver_clip_gate",
    )


def _validate_detection(value: ComplexDetection, label: str) -> tuple[complex, float, bool]:
    if not isinstance(value, ComplexDetection) or not isinstance(value.detected, bool):
        raise PortPairMatrixError(f"{label} detection record is invalid")
    if value.detected:
        if value.phasor is None:
            raise PortPairMatrixError(f"{label} detected tone lacks a phasor")
        phasor = _complex(value.phasor, f"{label} phasor")
        if abs(phasor) <= _EPSILON:
            raise PortPairMatrixError(f"{label} detected phasor must be nonzero")
        if value.magnitude_upper_bound is not None:
            raise PortPairMatrixError(f"{label} detected tone must not carry an upper bound")
        return phasor, abs(phasor), True
    if value.phasor is not None:
        raise PortPairMatrixError(f"{label} nondetection must not synthesize phase")
    upper = _finite(value.magnitude_upper_bound, f"{label} magnitude upper bound")
    if upper <= 0.0:
        raise PortPairMatrixError(f"{label} upper bound must be positive")
    return 0.0 + 0.0j, upper, False


def _document_complex(value: object, label: str) -> complex:
    if not isinstance(value, Mapping):
        raise PortPairMatrixError(f"{label} must be a complex object")
    _exact_keys(value, {"real", "imag"}, label)
    return complex(
        _finite(value["real"], f"{label} real"),
        _finite(value["imag"], f"{label} imag"),
    )


def _capture_from_observation(
    value: object,
    *,
    condition_run_id: str,
    label: str,
) -> CaptureIdentity:
    if not isinstance(value, Mapping):
        raise PortPairMatrixError(f"{label} observation must be an object")
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping):
        raise PortPairMatrixError(f"{label} artifact evidence must be an object")
    artifact_id = _identifier(artifact.get("artifact_id"), f"{label} artifact ID")
    raw_sha256 = _sha256(artifact.get("raw_iq_sha256"), f"{label} raw IQ hash")
    metadata_sha256 = _sha256(artifact.get("metadata_sha256"), f"{label} metadata hash")
    stream_id = _identifier(str(value.get("stream_id", "")), f"{label} stream ID")
    condition_record = _sha256(
        value.get("condition_record_sha256"), f"{label} condition-record hash"
    )
    leaves = (raw_sha256,)
    return CaptureIdentity(
        run_id=condition_run_id,
        stream_id=stream_id,
        artifact_sha256=canonical_sha256(
            {
                "artifact_id": artifact_id,
                "raw_iq_sha256": raw_sha256,
                "metadata_sha256": metadata_sha256,
            }
        ),
        raw_iq_sha256=raw_sha256,
        metadata_sha256=metadata_sha256,
        condition_record_sha256=condition_record,
        leaf_source_sha256s=leaves,
        leaf_source_set_sha256=canonical_sha256(leaves),
    )


def port_pair_repeat_from_observation(document: Mapping[str, Any]) -> PortPairRepeat:
    """Normalize one source-verified runner observation into analyzer evidence.

    The caller remains responsible for verifying the referenced local files and
    their SHA-256 values before invoking this pure parser.
    """

    if document.get("schema") != 1 or document.get("observation_kind") != (
        NORMALIZED_OBSERVATION_KIND
    ):
        raise PortPairMatrixError("normalized observation schema or kind is wrong")
    cell_id = document.get("cell_id")
    if cell_id not in CELL_IDS:
        raise PortPairMatrixError("normalized observation has an invalid cell")
    repeat_index = document.get("repeat_index")
    if (
        isinstance(repeat_index, bool)
        or not isinstance(repeat_index, Integral)
        or int(repeat_index) not in range(1, REPEAT_COUNT + 1)
    ):
        raise PortPairMatrixError("normalized observation repeat index must be 1..5")
    condition_run_id = _identifier(document.get("run_id"), "condition run ID")
    preflight = document.get("preflight")
    main = document.get("main")
    if not isinstance(preflight, Mapping) or not isinstance(main, Mapping):
        raise PortPairMatrixError("observation must contain preflight and main evidence")
    headroom = preflight.get("headroom")
    if not isinstance(headroom, Mapping) or not isinstance(headroom.get("input"), Mapping):
        raise PortPairMatrixError("observation lacks raw headroom-preflight input")
    raw_headroom = headroom["input"]
    peaks = raw_headroom.get("peak_abs_counts_by_receiver")
    clipped_preflight = raw_headroom.get("clipped_sample_count_by_receiver")
    if (
        not isinstance(peaks, Sequence)
        or isinstance(peaks, (str, bytes))
        or len(peaks) != 2
        or not isinstance(clipped_preflight, Sequence)
        or isinstance(clipped_preflight, (str, bytes))
        or len(clipped_preflight) != 2
    ):
        raise PortPairMatrixError("headroom preflight must describe RX1 and RX2")
    preflight_input = HeadroomPreflight(
        preflight_tx_gain_db=_finite(raw_headroom.get("preflight_tx_gain_db"), "preflight TX gain"),
        capture_tx_gain_db=_finite(raw_headroom.get("capture_tx_gain_db"), "capture TX gain"),
        clip_threshold_abs_counts=_finite(
            raw_headroom.get("clip_threshold_abs_counts"), "clip threshold"
        ),
        peak_abs_counts_by_receiver=(
            _finite(peaks[0], "RX1 preflight peak"),
            _finite(peaks[1], "RX2 preflight peak"),
        ),
        clipped_sample_count_by_receiver=(int(clipped_preflight[0]), int(clipped_preflight[1])),
        required_projected_headroom_db=_finite(
            raw_headroom.get("required_projected_headroom_db"), "required headroom"
        ),
    )
    main_readback = main.get("rf_readback")
    if not isinstance(main_readback, Mapping):
        raise PortPairMatrixError("main observation lacks RF readback")
    gains = main_readback.get("tx_gain_readback_db_by_channel")
    scales = main_readback.get("dds_scale_readback")
    clipped_main = main.get("clipped_sample_count_by_receiver")
    if (
        not isinstance(gains, Sequence)
        or isinstance(gains, (str, bytes))
        or len(gains) != 2
        or not isinstance(scales, Sequence)
        or isinstance(scales, (str, bytes))
        or len(scales) != 8
        or not isinstance(clipped_main, Sequence)
        or isinstance(clipped_main, (str, bytes))
        or len(clipped_main) != 2
    ):
        raise PortPairMatrixError("main RF/headroom evidence has the wrong shape")
    physical = document.get("physical_safety")
    final_mute = document.get("final_mute")
    analysis = main.get("analysis")
    if not isinstance(physical, Mapping) or not isinstance(final_mute, Mapping):
        raise PortPairMatrixError("observation lacks physical safety or final mute")
    if not isinstance(analysis, Mapping):
        raise PortPairMatrixError("observation lacks coherent main analysis")
    detected = analysis.get("test_receiver_tone_detected")
    if not isinstance(detected, bool):
        raise PortPairMatrixError("test-tone detection flag must be boolean")
    test_tone = ComplexDetection(
        detected=detected,
        phasor=(
            _document_complex(analysis.get("test_receiver_tone"), "test receiver tone")
            if detected
            else None
        ),
        magnitude_upper_bound=(
            None
            if detected
            else _finite(
                analysis.get("test_receiver_tone_magnitude_upper_bound"),
                "test-tone upper bound",
            )
        ),
    )
    final_gains = final_mute.get("tx_gain_readback_db_by_channel")
    final_scales = final_mute.get("dds_scale_readback")
    if (
        not isinstance(final_gains, Sequence)
        or isinstance(final_gains, (str, bytes))
        or len(final_gains) != 2
        or not isinstance(final_scales, Sequence)
        or isinstance(final_scales, (str, bytes))
        or len(final_scales) != 8
    ):
        raise PortPairMatrixError("final mute readback has the wrong shape")
    return PortPairRepeat(
        cell_id=str(cell_id),
        repeat_index=int(repeat_index),
        plan_sha256=_sha256(document.get("campaign_plan_sha256"), "campaign plan hash"),
        fixture_sha256=_sha256(document.get("fixture_sha256"), "fixture hash"),
        calibration_sha256=_sha256(document.get("calibration_sha256"), "calibration hash"),
        topology_sha256=_sha256(document.get("topology_sha256"), "topology hash"),
        source_commit=_git_commit(document.get("source_commit"), "source commit"),
        dependency_commit=_git_commit(document.get("dependency_commit"), "dependency commit"),
        native_attestation_sha256=_sha256(
            document.get("native_attestation_sha256"), "native attestation hash"
        ),
        preflight_capture=_capture_from_observation(
            preflight, condition_run_id=condition_run_id, label="preflight"
        ),
        main_capture=_capture_from_observation(
            main, condition_run_id=condition_run_id, label="main"
        ),
        headroom_preflight=preflight_input,
        tx_gain_readback_db_by_channel=(
            _finite(gains[0], "TX1 gain readback"),
            _finite(gains[1], "TX2 gain readback"),
        ),
        dds_scale_readback=tuple(_finite(value, "DDS scale") for value in scales),
        clipped_sample_count_by_receiver=(int(clipped_main[0]), int(clipped_main[1])),
        inactive_tx_termination_sha256=_sha256(
            physical.get("inactive_tx_termination_sha256"), "inactive-TX termination"
        ),
        test_receiver_termination_sha256=_sha256(
            physical.get("test_receiver_termination_sha256"), "test-RX termination"
        ),
        reference_chain_sha256=_sha256(physical.get("reference_chain_sha256"), "reference chain"),
        rx1_protection_sha256=_sha256(physical.get("rx1_protection_sha256"), "RX1 protection"),
        test_receiver_tone=test_tone,
        reference_receiver_tone=_document_complex(
            analysis.get("reference_receiver_tone"), "reference receiver tone"
        ),
        reference_tone_snr_db=_finite(analysis.get("reference_tone_snr_db"), "reference tone SNR"),
        continuity_passed=(
            preflight.get("continuity_passed") is True and main.get("continuity_passed") is True
        ),
        quality_passed=document.get("quality_passed") is True,
        final_mute_passed=final_mute.get("status") == "passed",
        final_tx_gain_readback_db_by_channel=(
            _finite(final_gains[0], "final TX1 gain"),
            _finite(final_gains[1], "final TX2 gain"),
        ),
        final_dds_scale_readback=tuple(_finite(value, "final DDS scale") for value in final_scales),
    )


class _Sources:
    def __init__(self) -> None:
        self.values: dict[str, set[str]] = {
            "stream": set(),
            "artifact": set(),
            "raw": set(),
            "metadata": set(),
            "leaf": set(),
        }

    def add(self, capture: CaptureIdentity, label: str) -> None:
        if not isinstance(capture, CaptureIdentity):
            raise PortPairMatrixError(f"{label} capture identity has the wrong type")
        fields = (
            ("stream", capture.stream_id),
            ("artifact", _sha256(capture.artifact_sha256, f"{label} artifact")),
            ("raw", _sha256(capture.raw_iq_sha256, f"{label} raw IQ")),
            ("metadata", _sha256(capture.metadata_sha256, f"{label} metadata")),
        )
        for kind, raw_value in fields:
            value = _identifier(raw_value, f"{label} {kind} identity")
            if value in self.values[kind]:
                raise PortPairMatrixError(f"matrix captures are not source-distinct: reused {kind}")
            self.values[kind].add(value)
        if not isinstance(capture.leaf_source_sha256s, tuple) or not capture.leaf_source_sha256s:
            raise PortPairMatrixError(f"{label} leaf sources must be a nonempty immutable tuple")
        leaves = tuple(
            _sha256(value, f"{label} leaf source") for value in capture.leaf_source_sha256s
        )
        if leaves != tuple(sorted(set(leaves))):
            raise PortPairMatrixError(f"{label} leaf sources must be unique and sorted")
        if capture.leaf_source_set_sha256 != canonical_sha256(leaves):
            raise PortPairMatrixError(f"{label} leaf-source set hash mismatch")
        for leaf in leaves:
            if leaf in self.values["leaf"]:
                raise PortPairMatrixError(
                    "matrix captures are not source-distinct: reused raw leaf source"
                )
            self.values["leaf"].add(leaf)


def _validate_digital_readback(repeat: PortPairRepeat, cell: CellContract) -> None:
    if len(repeat.tx_gain_readback_db_by_channel) != 2:
        raise PortPairMatrixError("TX gain readback must contain TX1 and TX2")
    active_index = TX_PORTS.index(cell.active_tx)
    inactive_index = TX_PORTS.index(cell.inactive_tx)
    gains = tuple(
        _finite(value, "TX gain readback") for value in repeat.tx_gain_readback_db_by_channel
    )
    if abs(gains[active_index] - CAPTURE_TX_GAIN_DB) > 0.25:
        raise PortPairMatrixError("active TX gain readback differs from the plan")
    if abs(gains[inactive_index] - (-80.0)) > 0.25:
        raise PortPairMatrixError("inactive TX is not digitally muted at -80 dB")
    if len(repeat.dds_scale_readback) != 8:
        raise PortPairMatrixError("DDS readback is not the canonical eight-value layout")
    active_indices = {active_index * 4, active_index * 4 + 2}
    for index, raw_scale in enumerate(repeat.dds_scale_readback):
        scale = abs(_finite(raw_scale, "DDS scale readback"))
        expected = DDS_SCALE if index in active_indices else 0.0
        if abs(scale - expected) > 1e-6:
            if index // 4 == inactive_index:
                raise PortPairMatrixError("inactive TX DDS is active")
            raise PortPairMatrixError("DDS scale readback differs from one-port plan")
    if repeat.final_tx_gain_readback_db_by_channel != (-80.0, -80.0):
        raise PortPairMatrixError("final mute did not leave both TX gains at -80 dB")
    if len(repeat.final_dds_scale_readback) != 8 or any(
        abs(_finite(value, "final DDS scale")) > 1e-9 for value in repeat.final_dds_scale_readback
    ):
        raise PortPairMatrixError("final mute did not zero every DDS scale")


def _covariance(
    values: npt.NDArray[np.complex128],
) -> tuple[tuple[float, float], tuple[float, float]]:
    raw = np.cov(np.stack((values.real, values.imag), axis=1), rowvar=False, ddof=1)
    return (
        (float(raw[0, 0]), float(raw[0, 1])),
        (float(raw[1, 0]), float(raw[1, 1])),
    )


def _phase_interval(draws: npt.NDArray[np.complex128], center_phase: float) -> tuple[float, float]:
    raw = np.degrees(np.angle(draws))
    unwrapped = center_phase + ((raw - center_phase + 180.0) % 360.0 - 180.0)
    return (
        float(np.quantile(unwrapped, 0.025, method="lower")),
        float(np.quantile(unwrapped, 0.975, method="higher")),
    )


def analyze_port_pair_matrix(
    fixture: ValidatedFixture,
    calibration: ValidatedCalibration,
    repeats: Sequence[PortPairRepeat],
    *,
    plan_sha256: str,
    bootstrap_draw_count: int = DEFAULT_BOOTSTRAP_DRAWS,
    bootstrap_seed: int = 0x5A8_706,
) -> PortPairMatrixResult:
    """Normalize and aggregate exactly five protected repeats for all four cells."""

    plan_hash = _sha256(plan_sha256, "matrix plan hash")
    if calibration.fixture_sha256 != fixture.fixture_sha256:
        raise PortPairMatrixError("calibration and fixture differ")
    if (
        isinstance(bootstrap_draw_count, bool)
        or not isinstance(bootstrap_draw_count, Integral)
        or int(bootstrap_draw_count) < 256
    ):
        raise PortPairMatrixError("bootstrap draw count must be an integer of at least 256")
    if isinstance(repeats, (str, bytes)) or len(repeats) != len(CELL_IDS) * REPEAT_COUNT:
        raise PortPairMatrixError("matrix must contain exactly 20 accepted main repeats")
    sources = _Sources()
    grouped: dict[str, dict[int, tuple[PortPairRepeat, complex, float, bool]]] = {
        cell_id: {} for cell_id in CELL_IDS
    }
    source_commit: str | None = None
    dependency_commit: str | None = None
    native_hash: str | None = None
    condition_run_ids: set[str] = set()
    condition_record_hashes: set[str] = set()
    for repeat in repeats:
        if not isinstance(repeat, PortPairRepeat) or repeat.cell_id not in CELL_IDS:
            raise PortPairMatrixError("repeat has an invalid matrix cell")
        cell = fixture.cell(repeat.cell_id)
        if (
            isinstance(repeat.repeat_index, bool)
            or not isinstance(repeat.repeat_index, Integral)
            or int(repeat.repeat_index) not in range(1, REPEAT_COUNT + 1)
        ):
            raise PortPairMatrixError("cell repeat indices must be exactly 1..5")
        index = int(repeat.repeat_index)
        if index in grouped[repeat.cell_id]:
            raise PortPairMatrixError("cell has a duplicate repeat index")
        if repeat.plan_sha256 != plan_hash:
            raise PortPairMatrixError("repeat is stale for the immutable plan")
        if repeat.fixture_sha256 != fixture.fixture_sha256:
            raise PortPairMatrixError("repeat uses the wrong fixture")
        if repeat.calibration_sha256 != calibration.calibration_sha256:
            raise PortPairMatrixError("repeat uses the wrong calibration")
        if repeat.topology_sha256 != cell.topology_sha256:
            raise PortPairMatrixError("repeat topology differs from the exact cell")
        current_source = _git_commit(repeat.source_commit, "source commit")
        current_dependency = _git_commit(repeat.dependency_commit, "dependency commit")
        current_native = _sha256(repeat.native_attestation_sha256, "native attestation")
        source_commit = source_commit or current_source
        dependency_commit = dependency_commit or current_dependency
        native_hash = native_hash or current_native
        if (
            current_source != source_commit
            or current_dependency != dependency_commit
            or current_native != native_hash
        ):
            raise PortPairMatrixError("matrix repeats do not share one source/native revision")
        condition_run_id = _identifier(repeat.preflight_capture.run_id, "condition run ID")
        if repeat.main_capture.run_id != condition_run_id:
            raise PortPairMatrixError("preflight and main must bind one condition run ID")
        if condition_run_id in condition_run_ids:
            raise PortPairMatrixError("matrix condition run IDs are not source-distinct")
        condition_run_ids.add(condition_run_id)
        condition_record_hash = _sha256(
            repeat.preflight_capture.condition_record_sha256,
            "condition-record hash",
        )
        if repeat.main_capture.condition_record_sha256 != condition_record_hash:
            raise PortPairMatrixError("preflight and main must bind one condition record")
        if condition_record_hash in condition_record_hashes:
            raise PortPairMatrixError("matrix condition records are not source-distinct")
        condition_record_hashes.add(condition_record_hash)
        sources.add(repeat.preflight_capture, f"{repeat.cell_id} preflight {index}")
        sources.add(repeat.main_capture, f"{repeat.cell_id} main {index}")
        admission = evaluate_headroom_preflight(repeat.headroom_preflight)
        if not admission.passed:
            raise PortPairMatrixError("repeat failed projected ADC-headroom preflight")
        if tuple(repeat.clipped_sample_count_by_receiver) != (0, 0):
            raise PortPairMatrixError("main capture contains clipped samples")
        if not repeat.continuity_passed or not repeat.quality_passed:
            raise PortPairMatrixError("repeat failed continuity or capture quality")
        if not repeat.final_mute_passed:
            raise PortPairMatrixError("repeat lacks exact final mute")
        if repeat.inactive_tx_termination_sha256 != cell.inactive_tx_termination_sha256:
            raise PortPairMatrixError("inactive TX lacks the exact physical termination")
        if repeat.test_receiver_termination_sha256 != cell.test_receiver_termination_sha256:
            raise PortPairMatrixError("test receiver lacks the exact direct termination")
        if repeat.reference_chain_sha256 != cell.reference_chain_sha256:
            raise PortPairMatrixError("reference receiver uses the wrong attenuator")
        if repeat.rx1_protection_sha256 != fixture.rx1_protection.identity_sha256:
            raise PortPairMatrixError("RX1 protection was removed, moved, or bypassed")
        _validate_digital_readback(repeat, cell)
        reference = _complex(repeat.reference_receiver_tone, "reference receiver tone")
        if (
            abs(reference) <= _EPSILON
            or _finite(repeat.reference_tone_snr_db, "reference SNR") < 20.0
        ):
            raise PortPairMatrixError(
                "conducted reference tone is not a valid normalization anchor"
            )
        test_phasor, test_upper, detected = _validate_detection(
            repeat.test_receiver_tone, "test receiver tone"
        )
        test_calibration = calibration.receiver(cell.test_receiver)
        reference_calibration = calibration.receiver(cell.reference_receiver)
        denominator = reference / reference_calibration.reference_chain_response
        if abs(denominator) <= _EPSILON:
            raise PortPairMatrixError("de-embedded reference tone is zero")
        if detected:
            normalized = (test_phasor / test_calibration.test_receiver_response) / denominator
            upper = abs(normalized)
        else:
            normalized = 0.0 + 0.0j
            upper = test_upper / abs(test_calibration.test_receiver_response) / abs(denominator)
        grouped[repeat.cell_id][index] = repeat, normalized, upper, detected

    generator = np.random.default_rng(int(bootstrap_seed))
    cell_results: list[NormalizedCellResult] = []
    for cell_id in CELL_IDS:
        indexed = grouped[cell_id]
        if set(indexed) != set(range(1, REPEAT_COUNT + 1)):
            raise PortPairMatrixError(f"{cell_id} repeat indices must be exactly 1..5")
        cell = fixture.cell(cell_id)
        ordered = [indexed[index] for index in range(1, REPEAT_COUNT + 1)]
        detections = [item[3] for item in ordered]
        test_calibration = calibration.receiver(cell.test_receiver)
        reference_calibration = calibration.receiver(cell.reference_receiver)
        transfer: ComplexInterval | None = None
        cell_upper: float | None = None
        upper_db: float | None = None
        if all(detections):
            values = np.asarray([item[1] for item in ordered], dtype=np.complex128)
            center = complex(np.mean(values))
            if abs(center) <= _EPSILON:
                raise PortPairMatrixError("normalized detected transfer center is zero")
            indices = generator.integers(
                0, REPEAT_COUNT, size=(int(bootstrap_draw_count), REPEAT_COUNT)
            )
            draws = np.asarray(np.mean(values[indices], axis=1), dtype=np.complex128)
            amplitude_db = 20.0 * log10(abs(center))
            phase_deg = degrees(atan2(center.imag, center.real))
            amplitude_draws = 20.0 * np.log10(np.abs(draws))
            transfer = ComplexInterval(
                center=center,
                amplitude_db=amplitude_db,
                phase_deg=phase_deg,
                amplitude_95_interval_db=(
                    float(np.quantile(amplitude_draws, 0.025, method="lower")),
                    float(np.quantile(amplitude_draws, 0.975, method="higher")),
                ),
                phase_95_interval_deg=_phase_interval(draws, phase_deg),
                covariance_real_imag=_covariance(draws),
            )
        else:
            cell_upper = max(item[2] for item in ordered)
            upper_db = 20.0 * log10(cell_upper)
        cell_results.append(
            NormalizedCellResult(
                cell_id=cell_id,
                active_tx=cell.active_tx,
                test_receiver=cell.test_receiver,
                reference_receiver=cell.reference_receiver,
                repeat_count=REPEAT_COUNT,
                all_test_tones_detected=all(detections),
                normalized_transfer=transfer,
                normalized_magnitude_upper_bound=cell_upper,
                normalized_magnitude_upper_bound_db=upper_db,
                phase_available=all(detections),
                raw_channel_amplitudes_comparable=False,
                normalization_equation=(
                    "(Y_test/G_test_receiver)/(Y_reference/G_reference_chain_receiver)"
                ),
                topology_sha256=cell.topology_sha256,
                test_response_evidence_sha256=test_calibration.test_response_evidence_sha256,
                reference_response_evidence_sha256=(
                    reference_calibration.reference_response_evidence_sha256
                ),
            )
        )
    return PortPairMatrixResult(
        fixture_sha256=fixture.fixture_sha256,
        calibration_sha256=calibration.calibration_sha256,
        plan_sha256=plan_hash,
        exact_four_cells_verified=True,
        five_source_distinct_repeats_per_cell_verified=True,
        rx1_protection_never_removed_or_bypassed=True,
        second_rx2_reference_chain_verified=True,
        receiver_reference_gain_normalization_applied=True,
        raw_channel_comparison_forbidden=True,
        cells=tuple(cell_results),
        statistical_method=(
            "source_distinct_five_repeat_complex_mean_bootstrap_after_reference_plane_"
            "receiver_and_reference_chain_deembedding"
        ),
    )
