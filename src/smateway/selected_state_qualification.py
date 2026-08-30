"""Fail-closed contracts for post-intervention 5.8-GHz selector re-entry.

The module is deliberately hardware-free.  It validates a fixture-v2-bound,
single-variable reversible intervention and three source-distinct evidence
sets: a static bench state matrix, two Fast20 timing captures, and five fresh
Fast20 complex-matrix streams.  Only complex phasors are combined; magnitude
and phase are derived after coherent arithmetic.

The final gates are intentionally simultaneous and conservative.  Bootstrap
tails use Bonferroni family-wise 95% coverage across all eight selected states.
No nondetection, reused source, incomplete state set, or cleanup failure can
produce a coefficient-release result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Literal

import numpy as np

from smateway.bench import BenchManifest
from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    verify_source_tree_binding,
)
from smateway.fixture_v2 import (
    FixtureV2Error,
    validate_fixture_manifest,
    validate_x_capture_linkage,
)
from smateway.intervention_support import (
    INTERVENTION_SUPPORT_ANALYSIS_KIND,
    INTERVENTION_SUPPORT_RESULT_KIND,
    InterventionSupportError,
    intervention_repeat_from_document,
    qualify_intervention_support,
)
from smateway.native_iio_attestation import validate_runtime_attestation
from smateway.profile import load_profile

ALL_OFF = "ALL_OFF"
ANTENNA_STATES = tuple(f"ANT{index}" for index in range(1, 9))
EXPECTED_STATES = (ALL_OFF, *ANTENNA_STATES)
IMAGE_ROLES = ("bench", "fast20")

FIXTURE_KIND_V2 = "5g8_general_topology_stage_fixture"
FIXTURE_BINDING_KIND = "5g8_full_simultaneous_fixture_binding_v1"
FULL_SIMULTANEOUS_TOPOLOGY = "full_simultaneous_fixture"
FULL_CONDUCTED_STAGE = "full_conducted_fixture"
DEVICE_IDENTITY_KIND = "pluto_device_identity_v2"
INTERVENTION_PLAN_KIND = "5g8_intervention_change_plan_v2"
INTERVENTION_KIND = "5g8_supported_intervention_evidence_v2"
X_RUN_BINDING_KIND = "5g8_accepted_x_run_binding_v1"
X_PREBINDING_KIND = "5g8_x_intervention_prebinding_v1"
X_CAPTURE_CONTEXT_KIND = "5g8_x_intervention_capture_context_v1"
X_CAPTURE_MANIFEST_KIND = "5g8_x_intervention_capture_v1"
STATIC_KIND = "5g8_static_selected_state_matrix_v1"
TIMING_KIND = "5g8_fast20_timing_qualification_v1"
MATRIX_KIND = "5g8_fast20_complex_matrix_v1"

STATIC_RESULT_KIND = "5g8_static_selected_state_result_v1"
TIMING_RESULT_KIND = "5g8_fast20_timing_result_v1"
MATRIX_RESULT_KIND = "5g8_fast20_complex_matrix_result_v1"
RELEASE_RESULT_KIND = "5g8_selected_state_release_result_v1"

TIMING_RUN_COUNT = 2
MATRIX_REPEAT_COUNT = 5
STATIC_SELECTED_STATE_LEASE_MS = 60_000
MINIMUM_REFERENCE_SNR_DB = 20.0
MINIMUM_COHERENCE = 0.995
MAXIMUM_PHASE_RMS_DEG = 6.0
MINIMUM_ADC_HEADROOM_DB = 6.0
MAXIMUM_REPEATABILITY_DB = 0.2
MAXIMUM_REPEATABILITY_PHASE_DEG = 2.0
OPERATIONAL_RAW_CONTRAST_DB = 20.0
OPERATIONAL_PATH_CONTRAST_DB = 20.0
ONE_DEGREE_PATH_CONTRAST_DB = 35.1629
DEFAULT_BOOTSTRAP_DRAWS = 32_768
_EPSILON = 1e-15

ImageRole = Literal["bench", "fast20"]


class SelectedStateQualificationError(ValueError):
    """Evidence cannot satisfy the selected-state qualification contract."""


def canonical_sha256(value: object) -> str:
    """Return a stable SHA-256 for finite JSON-compatible evidence."""

    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SelectedStateQualificationError(
            "identity must contain only finite JSON-compatible values"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    """Hash one regular, non-symlink file."""

    exact = path.expanduser().absolute()
    if exact.is_symlink() or not exact.is_file():
        raise SelectedStateQualificationError(f"evidence path is not a regular file: {exact}")
    digest = hashlib.sha256()
    with exact.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SelectedStateQualificationError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SelectedStateQualificationError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SelectedStateQualificationError(
            f"{label} keys differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def reject_replace_placeholders(value: object, label: str = "X evidence") -> None:
    """Recursively reject unresolved uppercase REPLACE sentinels."""

    if isinstance(value, str):
        if "REPLACE" in value:
            raise SelectedStateQualificationError(
                f"{label} contains an unresolved REPLACE placeholder"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            reject_replace_placeholders(key, f"{label} key")
            reject_replace_placeholders(item, f"{label}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            reject_replace_placeholders(item, f"{label}[{index}]")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 192:
        raise SelectedStateQualificationError(f"{label} must be a bounded nonempty string")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(character not in allowed for character in value):
        raise SelectedStateQualificationError(f"{label} contains unsafe characters")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SelectedStateQualificationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _commit(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SelectedStateQualificationError(f"{label} must be a full lowercase Git commit")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < minimum:
        raise SelectedStateQualificationError(f"{label} must be an integer >= {minimum}")
    return int(value)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise SelectedStateQualificationError(f"{label} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise SelectedStateQualificationError(f"{label} must be finite")
    return result


def _true(value: object, label: str) -> None:
    if value is not True:
        raise SelectedStateQualificationError(f"{label} must be true")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SelectedStateQualificationError(f"{label} must be an ISO-8601 timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise SelectedStateQualificationError(f"{label} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise SelectedStateQualificationError(f"{label} must include a UTC offset")
    return result


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _complex(value: object, label: str) -> complex:
    document = _mapping(value, label)
    _exact_keys(document, {"real", "imag"}, label)
    result = complex(
        _finite(document["real"], f"{label}.real"),
        _finite(document["imag"], f"{label}.imag"),
    )
    if abs(result) <= _EPSILON:
        raise SelectedStateQualificationError(f"{label} must have nonzero magnitude")
    return result


def _state_order(value: object, label: str) -> tuple[str, ...]:
    result = tuple(str(item) for item in _sequence(value, label))
    if result != EXPECTED_STATES:
        raise SelectedStateQualificationError(f"{label} must be exactly ALL_OFF, ANT1, ..., ANT8")
    return result


@dataclass(frozen=True, slots=True)
class DeviceIdentityEvidence:
    """One fresh read-only observation of the exact Pluto and host IIO runtime."""

    serial: str
    usb_uri: str
    observed_at: datetime
    model: str
    firmware_version: str
    kernel_version: str
    phy_model: str
    metadata_abi: int
    rx_scan_channels: tuple[str, ...]
    native_attestation_sha256: str
    observation_sha256: str


_DEVICE_IDENTITY_FIELDS = {
    "schema",
    "evidence_kind",
    "observed_at",
    "serial",
    "usb_uri",
    "read_only_usb_resolution",
    "iio_context_facts",
    "sysfs_attributes",
    "native_libiio_runtime_attestation",
    "native_libiio_runtime_attestation_sha256",
    "observation_sha256",
    "accepted",
}


def validate_device_identity_evidence(value: Mapping[str, Any]) -> DeviceIdentityEvidence:
    """Recompute acceptance for a producer-generated, read-only Pluto observation."""

    document = _mapping(value, "device identity")
    _exact_keys(document, _DEVICE_IDENTITY_FIELDS, "device identity")
    if document.get("schema") != 2 or document.get("evidence_kind") != DEVICE_IDENTITY_KIND:
        raise SelectedStateQualificationError("device identity schema or kind is invalid")
    if document.get("accepted") is not True:
        raise SelectedStateQualificationError("device identity was not accepted by its producer")
    serial = _identifier(document.get("serial"), "device serial")
    uri = document.get("usb_uri")
    if not isinstance(uri, str) or not uri.startswith("usb:"):
        raise SelectedStateQualificationError("device identity requires an explicit USB URI")
    resolution = _mapping(document.get("read_only_usb_resolution"), "USB resolution")
    if (
        resolution.get("status") != "passed"
        or resolution.get("serial") != serial
        or resolution.get("requested_uri") != uri
        or resolution.get("resolved_uri") != uri
        or resolution.get("exact_uri_match") is not True
        or resolution.get("scan_mutates_radio_state") is not False
        or resolution.get("error") is not None
    ):
        raise SelectedStateQualificationError(
            "device identity lacks an exact read-only serial/USB resolution"
        )
    facts = _mapping(document.get("iio_context_facts"), "IIO context facts")
    expected_fact_fields = {
        "serial",
        "model",
        "firmware_version",
        "kernel_version",
        "context_uri",
        "phy_model",
        "buffer_metadata_abi",
        "rx_scan_channels",
    }
    _exact_keys(facts, expected_fact_fields, "IIO context facts")
    if facts.get("serial") != serial or facts.get("context_uri") != uri:
        raise SelectedStateQualificationError("IIO context identity differs from USB resolution")
    model = _identifier(facts.get("model"), "Pluto model")
    firmware = _identifier(facts.get("firmware_version"), "Pluto firmware version")
    kernel = _identifier(facts.get("kernel_version"), "Pluto kernel version")
    phy = _identifier(facts.get("phy_model"), "Pluto PHY model")
    metadata_abi = _integer(facts.get("buffer_metadata_abi"), "metadata ABI", minimum=1)
    if metadata_abi != 2:
        raise SelectedStateQualificationError(
            "selected-state qualification requires metadata ABI 2"
        )
    channels = tuple(str(item) for item in _sequence(facts.get("rx_scan_channels"), "RX channels"))
    if channels != ("voltage0", "voltage1", "voltage2", "voltage3"):
        raise SelectedStateQualificationError("device identity requires the exact paired-RX layout")
    sysfs = _mapping(document.get("sysfs_attributes"), "USB sysfs attributes")
    _exact_keys(
        sysfs,
        {"path", "serial", "idVendor", "idProduct", "manufacturer", "product"},
        "USB sysfs attributes",
    )
    if (
        sysfs.get("serial") != serial
        or str(sysfs.get("idVendor", "")).lower() != "0456"
        or str(sysfs.get("idProduct", "")).lower() != "b673"
    ):
        raise SelectedStateQualificationError(
            "USB sysfs identity is not the requested runtime Pluto"
        )
    native = _mapping(
        document.get("native_libiio_runtime_attestation"), "native libiio attestation"
    )
    native_sha = _sha256(
        document.get("native_libiio_runtime_attestation_sha256"),
        "native libiio attestation SHA-256",
    )
    if canonical_sha256(native) != native_sha:
        raise SelectedStateQualificationError("native libiio attestation hash is inconsistent")
    observation = {
        "observed_at": document.get("observed_at"),
        "serial": serial,
        "usb_uri": uri,
        "read_only_usb_resolution": dict(resolution),
        "iio_context_facts": dict(facts),
        "sysfs_attributes": dict(sysfs),
        "native_libiio_runtime_attestation": dict(native),
        "native_libiio_runtime_attestation_sha256": native_sha,
    }
    observation_sha = _sha256(document.get("observation_sha256"), "device observation SHA-256")
    if canonical_sha256(observation) != observation_sha:
        raise SelectedStateQualificationError("device observation hash is inconsistent")
    return DeviceIdentityEvidence(
        serial=serial,
        usb_uri=uri,
        observed_at=_timestamp(document.get("observed_at"), "device observed_at"),
        model=model,
        firmware_version=firmware,
        kernel_version=kernel,
        phy_model=phy,
        metadata_abi=metadata_abi,
        rx_scan_channels=channels,
        native_attestation_sha256=native_sha,
        observation_sha256=observation_sha,
    )


@dataclass(frozen=True, slots=True)
class DeviceIdentitySnapshot:
    """Release-relevant identity projected from one fresh device observation.

    ``usb_uri`` is retained for traceability but is deliberately excluded from
    the stable identity key: the bus address may change when the same
    serial-numbered Pluto re-enumerates.  Every hardware, firmware, ABI, scan
    layout, and native-runtime field remains release-stable.
    """

    serial: str
    usb_uri: str
    model: str
    firmware_version: str
    kernel_version: str
    phy_model: str
    metadata_abi: int
    rx_scan_channels: tuple[str, ...]
    native_attestation_sha256: str

    def stable_key(self) -> tuple[object, ...]:
        """Return the exact cross-observation identity, excluding USB address."""

        return (
            self.serial,
            self.model,
            self.firmware_version,
            self.kernel_version,
            self.phy_model,
            self.metadata_abi,
            self.rx_scan_channels,
            self.native_attestation_sha256,
        )


_DEVICE_IDENTITY_SNAPSHOT_FIELDS = set(DeviceIdentitySnapshot.__dataclass_fields__)


def device_identity_snapshot_from_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project one validated observation into its qualification-context snapshot."""

    evidence = validate_device_identity_evidence(value)
    return {
        "serial": evidence.serial,
        "usb_uri": evidence.usb_uri,
        "model": evidence.model,
        "firmware_version": evidence.firmware_version,
        "kernel_version": evidence.kernel_version,
        "phy_model": evidence.phy_model,
        "metadata_abi": evidence.metadata_abi,
        "rx_scan_channels": list(evidence.rx_scan_channels),
        "native_attestation_sha256": evidence.native_attestation_sha256,
    }


def _device_identity_snapshot(value: object, label: str) -> DeviceIdentitySnapshot:
    document = _mapping(value, label)
    _exact_keys(document, _DEVICE_IDENTITY_SNAPSHOT_FIELDS, label)
    uri = document.get("usb_uri")
    if not isinstance(uri, str) or not uri.startswith("usb:"):
        raise SelectedStateQualificationError(f"{label} requires an explicit USB URI")
    metadata_abi = _integer(document.get("metadata_abi"), f"{label} metadata ABI", minimum=1)
    if metadata_abi != 2:
        raise SelectedStateQualificationError(f"{label} requires metadata ABI 2")
    channels = tuple(
        str(item)
        for item in _sequence(document.get("rx_scan_channels"), f"{label} RX scan channels")
    )
    if channels != ("voltage0", "voltage1", "voltage2", "voltage3"):
        raise SelectedStateQualificationError(f"{label} requires the exact paired-RX layout")
    return DeviceIdentitySnapshot(
        serial=_identifier(document.get("serial"), f"{label} serial"),
        usb_uri=uri,
        model=_identifier(document.get("model"), f"{label} model"),
        firmware_version=_identifier(document.get("firmware_version"), f"{label} firmware version"),
        kernel_version=_identifier(document.get("kernel_version"), f"{label} kernel version"),
        phy_model=_identifier(document.get("phy_model"), f"{label} PHY model"),
        metadata_abi=metadata_abi,
        rx_scan_channels=channels,
        native_attestation_sha256=_sha256(
            document.get("native_attestation_sha256"), f"{label} native attestation"
        ),
    )


@dataclass(frozen=True, slots=True)
class FullSimultaneousFixture:
    fixture_id: str
    fixture_manifest_path: str
    fixture_manifest_sha256: str
    fixture_revision_sha256: str
    board_id: str
    hardware_revision: str
    pluto_serial: str
    component_ids: Mapping[str, str]
    connection_ids: Mapping[str, Any]


_COMPONENT_ROLES = {
    "pluto",
    "two_way_splitter",
    "eight_way_splitter",
    "rx1_attenuator",
    "selector",
    "tx2_termination",
}
_CONNECTION_ROLES = {
    "tx1_to_two_way",
    "two_way_to_rx1_chain",
    "two_way_to_eight_way",
    "eight_way_to_selector",
    "selector_common_to_rx2",
    "tx2_to_termination",
}


def _manifest_identity(value: object, label: str) -> str:
    document = _mapping(value, label)
    return _identifier(document.get("id"), f"{label} ID")


def _full_fixture_manifest_facts(path: Path) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    try:
        raw = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectedStateQualificationError("fixture manifest is not valid JSON") from error
    document = _mapping(raw, "fixture manifest")
    preliminary_shared = _mapping(document.get("shared_fixture"), "fixture shared graph")
    preliminary_pluto = _mapping(preliminary_shared.get("pluto"), "fixture Pluto")
    board_id = _identifier(document.get("board_id"), "fixture board ID")
    pluto_serial = _identifier(preliminary_pluto.get("serial"), "fixture Pluto serial")
    try:
        validated = validate_fixture_manifest(
            exact,
            expected_stage=FULL_CONDUCTED_STAGE,
            expected_board_id=board_id,
            expected_serial=pluto_serial,
        )
    except FixtureV2Error as error:
        raise SelectedStateQualificationError(
            f"fixture manifest is not a valid production fixture-v2 chain: {error}"
        ) from error
    shared = _mapping(validated.shared_fixture, "fixture shared graph")
    delta = _mapping(validated.stage_delta, "fixture full-conducted delta")
    components = _mapping(delta.get("components"), "fixture stage components")
    connections = _mapping(delta.get("connections"), "fixture stage connections")
    shared_connections = _mapping(shared.get("connections"), "fixture shared connections")
    pluto = _mapping(shared.get("pluto"), "fixture Pluto")
    selector = _mapping(components.get("selector"), "fixture selector")
    two_way_rx1_roles = ("splitter_to_rx1_attenuator", "rx1_attenuator_to_rx1")
    antenna_roles = tuple(
        f"eight_way_{state.lower()}_to_selector_{state.lower()}" for state in ANTENNA_STATES
    )
    return {
        "fixture_id": _identifier(
            validated.comparable_fixture_group_id, "fixture comparison group ID"
        ),
        "fixture_manifest_path": str(exact),
        "fixture_manifest_sha256": validated.file_sha256,
        "board_id": validated.board_id,
        "hardware_revision": _identifier(
            selector.get("hardware_revision"), "selector hardware revision"
        ),
        "pluto_serial": validated.serial,
        "component_ids": {
            "pluto": _manifest_identity(pluto, "fixture Pluto"),
            "two_way_splitter": _manifest_identity(
                shared.get("tx1_reference_splitter"), "fixture two-way splitter"
            ),
            "eight_way_splitter": _manifest_identity(
                components.get("eight_way_splitter"), "fixture eight-way splitter"
            ),
            "rx1_attenuator": _manifest_identity(
                shared.get("rx1_attenuator"), "fixture RX1 attenuator"
            ),
            "selector": _manifest_identity(selector, "fixture selector"),
            "tx2_termination": _manifest_identity(
                shared.get("tx2_termination"), "fixture TX2 termination"
            ),
        },
        "connection_ids": {
            "tx1_to_two_way": _manifest_identity(
                shared_connections.get("tx1_to_splitter"), "fixture TX1 connection"
            ),
            "two_way_to_rx1_chain": [
                _manifest_identity(shared_connections.get(role), f"fixture RX1-chain {role}")
                for role in two_way_rx1_roles
            ],
            "two_way_to_eight_way": _manifest_identity(
                connections.get("splitter_stimulus_to_eight_way"),
                "fixture two-way-to-eight-way connection",
            ),
            "eight_way_to_selector": [
                _manifest_identity(connections.get(role), f"fixture splitter branch {role}")
                for role in antenna_roles
            ],
            "selector_common_to_rx2": _manifest_identity(
                connections.get("rx2_to_selector_common"),
                "fixture selector-common connection",
            ),
            "tx2_to_termination": _manifest_identity(
                shared_connections.get("tx2_to_termination"),
                "fixture TX2 termination connection",
            ),
        },
    }


def full_simultaneous_fixture_binding_from_manifest(path: Path) -> dict[str, Any]:
    """Derive the T8 wrapper directly from one full-conducted fixture-v2 manifest."""

    facts = _full_fixture_manifest_facts(path)
    document: dict[str, Any] = {
        "schema": 1,
        "binding_kind": FIXTURE_BINDING_KIND,
        **facts,
        "fixture_revision_sha256": "0" * 64,
        "fixture_schema": 2,
        "fixture_kind": FIXTURE_KIND_V2,
        "fixture_stage": FULL_CONDUCTED_STAGE,
        "topology_label": FULL_SIMULTANEOUS_TOPOLOGY,
        "direct_one_hot": False,
        "simultaneous_distribution_confirmed": True,
        "antenna_port_order": list(EXPECTED_STATES),
    }
    document["fixture_revision_sha256"] = fixture_revision_sha256(document)
    return document


def fixture_revision_sha256(document: Mapping[str, Any]) -> str:
    """Derive the revision identity from all fixed simultaneous-fixture facts."""

    return canonical_sha256(
        {
            "fixture_manifest_sha256": document.get("fixture_manifest_sha256"),
            "board_id": document.get("board_id"),
            "hardware_revision": document.get("hardware_revision"),
            "pluto_serial": document.get("pluto_serial"),
            "component_ids": document.get("component_ids"),
            "connection_ids": document.get("connection_ids"),
            "antenna_port_order": document.get("antenna_port_order"),
            "topology_label": document.get("topology_label"),
        }
    )


def validate_full_simultaneous_fixture(
    value: Mapping[str, Any], *, verify_manifest: bool = True
) -> FullSimultaneousFixture:
    """Validate a wrapper around an immutable full-conducted fixture-v2 manifest."""

    document = _mapping(value, "full simultaneous fixture binding")
    _exact_keys(
        document,
        {
            "schema",
            "binding_kind",
            "fixture_id",
            "fixture_manifest_path",
            "fixture_manifest_sha256",
            "fixture_revision_sha256",
            "fixture_schema",
            "fixture_kind",
            "fixture_stage",
            "topology_label",
            "direct_one_hot",
            "simultaneous_distribution_confirmed",
            "board_id",
            "hardware_revision",
            "pluto_serial",
            "component_ids",
            "connection_ids",
            "antenna_port_order",
        },
        "full simultaneous fixture binding",
    )
    if (
        document["schema"] != 1
        or document["binding_kind"] != FIXTURE_BINDING_KIND
        or document["fixture_schema"] != 2
        or document["fixture_kind"] != FIXTURE_KIND_V2
        or document["fixture_stage"] != FULL_CONDUCTED_STAGE
        or document["topology_label"] != FULL_SIMULTANEOUS_TOPOLOGY
        or document["direct_one_hot"] is not False
        or document["simultaneous_distribution_confirmed"] is not True
    ):
        raise SelectedStateQualificationError(
            "fixture must be fixture-v2 full_conducted_fixture under the distinct "
            "full_simultaneous_fixture label, never direct-one-hot"
        )
    _state_order(document["antenna_port_order"], "fixture antenna port order")
    components = _mapping(document["component_ids"], "fixture component IDs")
    _exact_keys(components, _COMPONENT_ROLES, "fixture component IDs")
    normalized_components = {
        role: _identifier(components[role], f"fixture component {role}")
        for role in sorted(_COMPONENT_ROLES)
    }
    if len(set(normalized_components.values())) != len(normalized_components):
        raise SelectedStateQualificationError("fixture component IDs must be unique")
    connections = _mapping(document["connection_ids"], "fixture connection IDs")
    _exact_keys(connections, _CONNECTION_ROLES, "fixture connection IDs")
    normalized_connections: dict[str, Any] = {}
    array_roles = {"two_way_to_rx1_chain", "eight_way_to_selector"}
    for role in sorted(_CONNECTION_ROLES - array_roles):
        normalized_connections[role] = _identifier(connections[role], f"fixture connection {role}")
    rx1_chain = tuple(
        _identifier(item, "two-way-to-RX1 chain connection ID")
        for item in _sequence(
            connections["two_way_to_rx1_chain"], "two-way-to-RX1 chain connections"
        )
    )
    if len(rx1_chain) != 2 or len(set(rx1_chain)) != 2:
        raise SelectedStateQualificationError(
            "full simultaneous fixture requires two unique RX1-chain connections"
        )
    normalized_connections["two_way_to_rx1_chain"] = rx1_chain
    branches = tuple(
        _identifier(item, "eight-way-to-selector branch ID")
        for item in _sequence(
            connections["eight_way_to_selector"], "eight-way-to-selector branches"
        )
    )
    if len(branches) != 8 or len(set(branches)) != 8:
        raise SelectedStateQualificationError(
            "full simultaneous fixture requires eight unique splitter-to-selector branches"
        )
    normalized_connections["eight_way_to_selector"] = branches
    all_connection_ids = [
        value
        for role, value in normalized_connections.items()
        for value in (value if role in array_roles else (value,))
    ]
    if len(set(all_connection_ids)) != len(all_connection_ids):
        raise SelectedStateQualificationError("fixture connection IDs must be unique")

    path_text = document["fixture_manifest_path"]
    if not isinstance(path_text, str) or not Path(path_text).is_absolute():
        raise SelectedStateQualificationError("fixture manifest path must be absolute")
    path = Path(path_text)
    manifest_sha = _sha256(document["fixture_manifest_sha256"], "fixture manifest SHA-256")
    if verify_manifest:
        if sha256_path(path) != manifest_sha:
            raise SelectedStateQualificationError("fixture manifest bytes differ from binding")
        manifest_facts = _full_fixture_manifest_facts(path)
        bound_facts = {
            field: document[field]
            for field in (
                "fixture_id",
                "fixture_manifest_path",
                "fixture_manifest_sha256",
                "board_id",
                "hardware_revision",
                "pluto_serial",
                "component_ids",
                "connection_ids",
            )
        }
        if canonical_sha256(manifest_facts) != canonical_sha256(bound_facts):
            raise SelectedStateQualificationError(
                "full simultaneous binding facts differ from the fixture-v2 graph"
            )
    expected_revision = fixture_revision_sha256(document)
    revision = _sha256(document["fixture_revision_sha256"], "fixture revision SHA-256")
    if revision != expected_revision:
        raise SelectedStateQualificationError("fixture revision hash is inconsistent")
    return FullSimultaneousFixture(
        fixture_id=_identifier(document["fixture_id"], "fixture ID"),
        fixture_manifest_path=str(path),
        fixture_manifest_sha256=manifest_sha,
        fixture_revision_sha256=revision,
        board_id=_identifier(document["board_id"], "board ID"),
        hardware_revision=_identifier(document["hardware_revision"], "hardware revision"),
        pluto_serial=_identifier(document["pluto_serial"], "Pluto serial"),
        component_ids=normalized_components,
        connection_ids=normalized_connections,
    )


@dataclass(frozen=True, slots=True)
class SelectorEvidenceBinding:
    path: str
    sha256: str
    campaign_id: str
    run_id: str
    board_id: str
    image_role: ImageRole
    firmware_bin_sha256: str
    profile_contract_sha256: str
    startup_evidence_sha256: str


def selector_binding_from_sealed(
    path: Path,
    *,
    expected_sha256: str,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: ImageRole,
) -> SelectorEvidenceBinding:
    """Validate the retained attestor graph and extract its exact downstream identity."""

    if image_role not in IMAGE_ROLES:
        raise SelectedStateQualificationError("selector image role must be bench or fast20")
    from smateway.selector_flash_attestation import (  # local to keep this module pure to import
        SelectorFlashError,
        validate_sealed_selector_evidence,
    )

    exact = path.expanduser().absolute()
    try:
        document = validate_sealed_selector_evidence(
            exact,
            expected_sha256=expected_sha256,
            expected_campaign_id=campaign_id,
            expected_run_id=run_id,
            expected_board_id=board_id,
            expected_image_role=image_role,
        )
    except SelectorFlashError as error:
        raise SelectedStateQualificationError(str(error)) from error
    frozen = _mapping(document.get("frozen_inputs"), "sealed selector frozen inputs")
    files = _mapping(frozen.get("files"), "sealed selector frozen files")
    firmware = _mapping(files.get("firmware_bin"), "sealed selector firmware BIN")
    profile = _mapping(frozen.get("control_profile"), "sealed selector profile")
    startup = _mapping(document.get("startup"), "sealed selector startup")
    if image_role == "bench":
        _true(startup.get("mailbox_all_off_passed"), "bench startup mailbox ALL_OFF")
        _true(startup.get("gpio_latch_all_off_passed"), "bench startup GPIO ALL_OFF")
    else:
        _true(startup.get("exact_bin_extent_readback_passed"), "Fast20 exact BIN readback")
        _true(startup.get("explicit_reset_run_succeeded"), "Fast20 reset run")
    return SelectorEvidenceBinding(
        path=str(exact),
        sha256=_sha256(expected_sha256, "selector evidence SHA-256"),
        campaign_id=_identifier(campaign_id, "campaign ID"),
        run_id=_identifier(run_id, "selector run ID"),
        board_id=_identifier(board_id, "board ID"),
        image_role=image_role,
        firmware_bin_sha256=_sha256(firmware.get("sha256"), "selector firmware SHA-256"),
        profile_contract_sha256=_sha256(
            profile.get("contract_sha256"), "selector profile contract SHA-256"
        ),
        startup_evidence_sha256=canonical_sha256(startup),
    )


def validate_selector_binding_snapshot(
    value: Mapping[str, Any], *, expected_role: ImageRole
) -> SelectorEvidenceBinding:
    """Validate a previously extracted selector binding stored in an immutable plan."""

    document = _mapping(value, "selector binding")
    _exact_keys(document, set(SelectorEvidenceBinding.__dataclass_fields__), "selector binding")
    if document.get("image_role") != expected_role:
        raise SelectedStateQualificationError(
            f"selected-state mode requires exact {expected_role} selector evidence"
        )
    path_value = document.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateQualificationError("selector evidence path must be absolute")
    return SelectorEvidenceBinding(
        path=path_value,
        sha256=_sha256(document.get("sha256"), "selector evidence SHA-256"),
        campaign_id=_identifier(document.get("campaign_id"), "selector campaign ID"),
        run_id=_identifier(document.get("run_id"), "selector run ID"),
        board_id=_identifier(document.get("board_id"), "selector board ID"),
        image_role=expected_role,
        firmware_bin_sha256=_sha256(
            document.get("firmware_bin_sha256"), "selector firmware SHA-256"
        ),
        profile_contract_sha256=_sha256(
            document.get("profile_contract_sha256"), "selector profile SHA-256"
        ),
        startup_evidence_sha256=_sha256(
            document.get("startup_evidence_sha256"), "selector startup SHA-256"
        ),
    )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_differences(before: object, after: object, path: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        output: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{_escape_pointer(str(key))}"
            if key not in before or key not in after:
                output.append(child)
            else:
                output.extend(_leaf_differences(before[key], after[key], child))
        return output
    if before != after:
        return [path or "/"]
    return []


def _pointer_value(document: object, pointer: str, label: str) -> object:
    if not pointer.startswith("/"):
        raise SelectedStateQualificationError(f"{label} is not a JSON pointer")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise SelectedStateQualificationError(f"{label} does not resolve in controlled fixture")
        current = current[part]
    return current


@dataclass(frozen=True, slots=True)
class InterventionChangePlan:
    contract_id: str
    campaign_id: str
    board_id: str
    before_fixture_revision_sha256: str
    installed_after_fixture_revision_sha256: str
    changed_component_id: str
    changed_property_path: str
    before: Any
    after: Any
    implicated_boundary_stage: str
    expected_x_roles: tuple[str, ...]
    expected_x_run_ids: Mapping[str, str]
    expected_x_plan_sha256s: Mapping[str, str]
    expected_x_topology_stages: Mapping[str, str]
    expected_x_topology_fixture_sha256s: Mapping[str, str]
    expected_x_acquisition_indices: Mapping[str, int]
    expected_x_freshness_epoch_id: str
    expected_x_selector_evidence_sha256: str
    expected_x_source_commit: str
    expected_x_dependency_commit: str
    expected_x_dependency_attestation_sha256: str
    expected_x_native_attestation_sha256: str
    expected_x_source_identity_sha256: str


@dataclass(frozen=True, slots=True)
class XRunBinding:
    run_role: str
    run_id: str
    captured_at: datetime
    acquisition_index: int
    freshness_epoch_id: str
    intervention_state_fixture_revision_sha256: str
    topology_stage: str
    topology_fixture_sha256: str
    source_commit: str
    dependency_commit: str
    selector_evidence_sha256: str
    stream_ids: tuple[str, ...]
    raw_iq_sha256s: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InterventionContract:
    contract_id: str
    campaign_id: str
    board_id: str
    baseline_fixture_revision_sha256: str
    installed_after_fixture_revision_sha256: str
    changed_component_id: str
    changed_property_path: str
    implicated_boundary_stage: str
    before: Any
    after: Any
    source_commit: str
    dependency_commit: str
    selector_evidence_sha256: str
    baseline_stream_ids: tuple[str, ...]
    intervention_stream_ids: tuple[str, ...]
    baseline_raw_iq_sha256s: tuple[str, ...]
    intervention_raw_iq_sha256s: tuple[str, ...]
    support_evidence_sha256: str
    adoption_attestation_sha256: str
    diagnostic_restoration_status: str


_CHANGE_PLAN_FIELDS = {
    "schema",
    "plan_kind",
    "contract_id",
    "campaign_id",
    "board_id",
    "created_at",
    "before_fixture",
    "installed_after_fixture",
    "change",
    "implicated_boundary_stage",
    "x_run_plans",
    "diagnostic_restoration_policy",
}
_X_ALL_RUN_ROLES = (
    "boundary_baseline",
    "boundary_intervention",
    "full_fixture_baseline",
    "full_fixture_intervention",
)
_X_BOUNDARY_ROLES = ("boundary_baseline", "boundary_intervention")
_X_FULL_FIXTURE_ROLES = ("full_fixture_baseline", "full_fixture_intervention")
_X_BOUNDARY_STAGES = (
    "direct_rx2_termination",
    "rx2_cable_terminated",
    "powered_selector_all_inputs_terminated",
)
_X_FULL_FIXTURE_STAGE = "full_conducted_fixture"
_PRODUCTION_X_PLAN_KIND = "5g8_marker_independent_coherent_leakage_ladder"
_PRODUCTION_X_PLAN_FIELDS = {
    "schema",
    "plan_kind",
    "run_id",
    "board_id",
    "topology_stage",
    "stage_contract",
    "source",
    "fixture_evidence",
    "fixture_evidence_sha256",
    "selector_control",
    "configuration",
    "operator_confirmations_required",
    "safety",
    "storage",
    "interpretation",
    "conditions",
    "x_intervention_prebinding",
    "x_intervention_capture_context",
}
_PRODUCTION_X_SOURCE_FIELDS = {
    "smateway_commit",
    "pluto_plus_utils_source_attestation",
    "pluto_plus_utils_source_attestation_sha256",
    "native_libiio_runtime_attestation",
    "native_libiio_runtime_attestation_sha256",
    "analyzer",
    "pilot_estimator",
    "capture_helper",
    "identity_resolver",
}
_PRODUCTION_DEPENDENCY_ATTESTATION_FIELDS = {
    "schema",
    "dependency",
    "repository_path",
    "commit",
    "head",
    "python_executable",
    "python_prefix",
    "clean_worktree_verified",
    "lock_metadata_files",
    "files",
    "imported_modules",
}
_PRODUCTION_DEPENDENCY_MODULE_PATHS = {
    "pluto_plus": "src/pluto_plus/__init__.py",
    "pluto_plus.artifacts": "src/pluto_plus/artifacts.py",
    "pluto_plus.bootstrap_firmware": "src/pluto_plus/bootstrap_firmware.py",
    "pluto_plus.hardware": "src/pluto_plus/hardware/__init__.py",
    "pluto_plus.hardware.iio": "src/pluto_plus/hardware/iio.py",
    "pluto_plus.hardware.iio_metadata": "src/pluto_plus/hardware/iio_metadata.py",
    "pluto_plus.hardware.preflight": "src/pluto_plus/hardware/preflight.py",
    "pluto_plus.hardware.stimulus": "src/pluto_plus/hardware/stimulus.py",
    "pluto_plus.models": "src/pluto_plus/models.py",
}
_PRODUCTION_X_CONFIGURATION_FIELDS = {
    "serial",
    "uri",
    "center_frequency_hz",
    "tone_offset_hz_requested",
    "sample_rate_hz",
    "bandwidth_hz",
    "receiver_gain_db",
    "tx_channel",
    "tx_port",
    "tx2_required_exact_muted",
    "dds_scale",
    "tx_hardware_gains_db",
    "samples_per_frame",
    "frame_count",
    "sample_count_per_condition",
    "duration_s_per_condition",
    "kernel_buffers",
    "fresh_stream_per_condition",
    "metadata_abi",
    "automatic_retry_count",
    "attribution_gain_db",
    "attribution_repeat_count",
    "attribution_repeats_require_unique_fresh_streams",
    "pilot_frequency_refinement_required",
    "minimum_pilot_confidence",
    "minimum_pilot_phase_step_coherence",
    "maximum_pilot_phase_rms_deg",
}
_PRODUCTION_X_CONDITION_FIELDS = {
    "plan_index",
    "condition_id",
    "stage",
    "center_frequency_hz",
    "center_frequency_policy",
    "sample_rate_hz",
    "bandwidth_hz",
    "tone_offset_hz",
    "tx_channel",
    "tx_port",
    "tx2_required_exact_muted",
    "tx_hardware_gain_db",
    "dds_scale",
    "receiver_gain_db",
    "samples_per_frame",
    "frame_count",
    "sample_count",
    "kernel_buffers",
    "fresh_stream_required",
    "condition_role",
    "attribution_repeat_index",
    "attribution_repeat_count",
}
_PRODUCTION_X_STAGE_ORDER_AND_TOKEN = {
    "direct_rx2_termination": (0, "DIRECT_RX2_50OHM_AT_PLUTO"),
    "rx2_cable_terminated": (1, "RX2_CABLE_FAR_END_50OHM"),
    "powered_selector_all_inputs_terminated": (
        2,
        "POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM",
    ),
    FULL_CONDUCTED_STAGE: (
        3,
        "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
    ),
}
_PRODUCTION_X_STAGE_CONTRACTS = {
    "direct_rx2_termination": {
        "order": 0,
        "confirmation_token": "DIRECT_RX2_50OHM_AT_PLUTO",
        "rx2_topology": "5.8 GHz 50 ohm termination directly on Pluto RX2",
        "selector_topology": "selector and RX2 cable disconnected",
        "selector_state_contract": (
            "selector RF disconnected, bench power off, and control/ground harness disconnected; "
            "controller forbidden"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "rx2_cable_terminated": {
        "order": 1,
        "confirmation_token": "RX2_CABLE_FAR_END_50OHM",
        "rx2_topology": "test cable on Pluto RX2 with a 5.8 GHz 50 ohm far-end termination",
        "selector_topology": "selector disconnected from the RX2 test cable",
        "selector_state_contract": (
            "selector RF disconnected, bench power off, and control/ground harness disconnected; "
            "controller forbidden"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "powered_selector_all_inputs_terminated": {
        "order": 2,
        "confirmation_token": "POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM",
        "rx2_topology": "RX2 test cable connects Pluto RX2 to the selector common port",
        "selector_topology": (
            "selector is powered and all eight ANT input ports have 5.8 GHz 50 ohm terminations"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    FULL_CONDUCTED_STAGE: {
        "order": 3,
        "confirmation_token": "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
        "rx2_topology": "selector common connects through the fixed test cable to Pluto RX2",
        "selector_topology": (
            "TX1 two-way branch feeds the 2-8 GHz eight-way splitter and all eight selector "
            "ANT inputs; the other attenuated two-way branch feeds RX1"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": "RX1 is the attenuated conducted reference branch",
    },
}
_PRODUCTION_X_SELECTOR_CONNECTED_STAGES = {
    "powered_selector_all_inputs_terminated",
    FULL_CONDUCTED_STAGE,
}
_FIXTURE_COMPONENT_POINTER_PREFIX = {
    "pluto": "/shared_fixture/pluto",
    "two_way_splitter": "/shared_fixture/tx1_reference_splitter",
    "rx1_attenuator": "/shared_fixture/rx1_attenuator",
    "tx2_termination": "/shared_fixture/tx2_termination",
    "selector": "/stage_delta/components/selector",
    "eight_way_splitter": "/stage_delta/components/eight_way_splitter",
}
_STAGE_COMPONENT_ROLES = {
    "direct_rx2_termination": frozenset(
        {"pluto", "two_way_splitter", "rx1_attenuator", "tx2_termination"}
    ),
    "rx2_cable_terminated": frozenset(
        {"pluto", "two_way_splitter", "rx1_attenuator", "tx2_termination"}
    ),
    "powered_selector_all_inputs_terminated": frozenset(
        {
            "pluto",
            "two_way_splitter",
            "rx1_attenuator",
            "tx2_termination",
            "selector",
        }
    ),
    "full_conducted_fixture": frozenset(_FIXTURE_COMPONENT_POINTER_PREFIX),
}
_X_RUN_BINDING_FIELDS = {
    "schema",
    "binding_kind",
    "contract_id",
    "change_plan_sha256",
    "run_role",
    "run_id",
    "captured_at",
    "acquisition_index",
    "freshness_epoch_id",
    "intervention_state_fixture_revision_sha256",
    "topology_stage",
    "topology_fixture_sha256",
    "source_commit",
    "dependency_commit",
    "selector_evidence_sha256",
    "plan_file",
    "manifest_file",
    "stream_ids",
    "raw_iq_files",
    "acceptance_revalidated",
}
_INTERVENTION_SUPPORT_SOURCE_FILES = (
    "src/smateway/capture_admission.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/hexcal.py",
    "src/smateway/intervention_support.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/ota_analysis.py",
    "src/smateway/selected_state_qualification.py",
    "scripts/run_5g8_leakage_ladder.py",
    "scripts/analyze_5g8_intervention_support.py",
)
_SETUP_ATTESTATION_KIND = "5g8_general_topology_run_setup"
_SETUP_ATTESTATION_SOURCE_FIELDS = {
    "schema",
    "attestation_kind",
    "attestation_id",
    "created_at",
    "run_id",
    "campaign_id",
    "comparable_fixture_group_id",
    "stage",
    "fixture_manifest_sha256",
    "shared_fixture_sha256",
    "stage_delta_sha256",
    "observed_component_ids",
    "observed_connection_ids",
    "selector_flash_evidence",
    "setup_evidence_path",
    "setup_evidence_sha256",
}
_NORMALIZED_SETUP_ATTESTATION_FIELDS = {
    "schema",
    "attestation_kind",
    "attestation_id",
    "created_at",
    "created_at_wall_clock_freshness_enforced",
    "run_id",
    "campaign_id",
    "comparable_fixture_group_id",
    "stage",
    "fixture_manifest_sha256",
    "shared_fixture_sha256",
    "stage_delta_sha256",
    "observed_component_ids",
    "observed_connection_ids",
    "selector_flash_evidence",
    "setup_evidence",
    "setup_attestation_file",
}


def _evidence_file(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    require_local_rpi: bool = False,
) -> dict[str, Any]:
    document = _mapping(value, label)
    _exact_keys(document, {"path", "sha256", "size_bytes"}, label)
    path_value = document.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateQualificationError(f"{label} path must be absolute")
    path = Path(path_value).expanduser().absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        if current.is_symlink():
            raise SelectedStateQualificationError(f"{label} path contains a symlink")
    if not path.is_file():
        raise SelectedStateQualificationError(f"{label} must be a regular non-symlink file")
    if require_local_rpi:
        forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
        if any(path == root or root in path.parents for root in forbidden):
            raise SelectedStateQualificationError(f"{label} is not on local Raspberry Pi storage")
        try:
            if os.stat(path).st_dev != os.stat(Path("/home/pi")).st_dev:
                raise SelectedStateQualificationError(
                    f"{label} is not on the Raspberry Pi local filesystem"
                )
        except OSError as error:
            raise SelectedStateQualificationError(
                f"cannot attest local storage for {label}: {error}"
            ) from error
    size = _integer(document.get("size_bytes"), f"{label} size", minimum=0 if allow_empty else 1)
    if path.stat().st_size != size:
        raise SelectedStateQualificationError(f"{label} size differs from its binding")
    digest = _sha256(document.get("sha256"), f"{label} SHA-256")
    if sha256_path(path) != digest:
        raise SelectedStateQualificationError(f"{label} bytes differ from its binding")
    return {"path": str(path), "sha256": digest, "size_bytes": size}


def _json_file(value: object, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _evidence_file(value, label)
    try:
        document = json.loads(Path(evidence["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectedStateQualificationError(f"{label} is not valid JSON") from error
    if not isinstance(document, dict):
        raise SelectedStateQualificationError(f"{label} must contain one JSON object")
    reject_replace_placeholders(document, label)
    return document, evidence


def _fixture_graph_inventory(
    shared_fixture: Mapping[str, Any], stage_delta: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Derive the authoritative component/connection inventory from one fixture graph."""

    component_ids: list[str] = []
    connection_ids: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            identity = value.get("id")
            if {"id", "from", "to", "interconnect"}.issubset(value):
                connection_ids.append(_identifier(identity, "fixture connection ID"))
            elif isinstance(identity, str):
                component_ids.append(_identifier(identity, "fixture component ID"))
            for item in value.values():
                visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item)

    visit(shared_fixture)
    visit(stage_delta)
    if len(component_ids) != len(set(component_ids)) or len(connection_ids) != len(
        set(connection_ids)
    ):
        raise SelectedStateQualificationError(
            "installed fixture component/connection IDs are not globally unique"
        )
    return sorted(component_ids), sorted(connection_ids)


def _full_fixture_intervention_setup_authority(
    change_plan_document: Mapping[str, Any],
    *,
    plan: InterventionChangePlan,
    expected_selector_evidence_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reopen and validate the accepted Stage-E intervention setup authority."""

    role = "full_fixture_intervention"
    x_plans = _mapping(change_plan_document.get("x_run_plans"), "predeclared X run plans")
    role_plan = _mapping(x_plans.get(role), f"X plan {role}")
    x_plan_document, _ = _json_file(role_plan.get("plan_file"), f"X plan {role} file")
    contract = _mapping(x_plan_document.get("plan_contract"), f"X plan {role} contract")
    fixture_evidence = _mapping(contract.get("fixture_evidence"), f"X plan {role} fixture evidence")
    if (
        contract.get("run_id") != plan.expected_x_run_ids[role]
        or contract.get("topology_stage") != FULL_CONDUCTED_STAGE
        or fixture_evidence.get("schema") != 2
        or fixture_evidence.get("fixture_kind") != FIXTURE_KIND_V2
        or fixture_evidence.get("campaign_id") != plan.campaign_id
        or fixture_evidence.get("comparable_fixture_group_id")
        != _mapping(
            change_plan_document.get("installed_after_fixture"), "installed after fixture"
        ).get("fixture_id")
        or fixture_evidence.get("stage") != FULL_CONDUCTED_STAGE
        or fixture_evidence.get("run_id") != plan.expected_x_run_ids[role]
        or fixture_evidence.get("board_id") != plan.board_id
    ):
        raise SelectedStateQualificationError(
            "full-fixture intervention setup authority has the wrong campaign/board/stage/run"
        )

    after_binding = _mapping(
        change_plan_document.get("installed_after_fixture"), "installed after fixture"
    )
    after_manifest_path = Path(str(after_binding.get("fixture_manifest_path", "")))
    expected_manifest_file = {
        "path": str(after_manifest_path),
        "sha256": after_binding.get("fixture_manifest_sha256"),
        "size_bytes": after_manifest_path.stat().st_size if after_manifest_path.is_file() else -1,
    }
    source_files = _mapping(
        fixture_evidence.get("source_files"), f"X plan {role} fixture source files"
    )
    _exact_keys(
        source_files,
        {"fixture_manifest", "setup_attestation"},
        f"X plan {role} fixture source files",
    )
    observed_manifest_file = _evidence_file(
        source_files.get("fixture_manifest"), f"X plan {role} fixture manifest"
    )
    if observed_manifest_file != expected_manifest_file:
        raise SelectedStateQualificationError(
            "full-fixture intervention setup does not bind the installed-after fixture manifest"
        )

    shared = _mapping(fixture_evidence.get("shared_fixture"), "installed shared fixture")
    delta = _mapping(fixture_evidence.get("stage_delta"), "installed Stage-E delta")
    shared_sha = _sha256(
        fixture_evidence.get("shared_fixture_sha256"), "installed shared fixture SHA-256"
    )
    delta_sha = _sha256(
        fixture_evidence.get("stage_delta_sha256"), "installed Stage-E delta SHA-256"
    )
    component_ids, connection_ids = _fixture_graph_inventory(shared, delta)
    if (
        canonical_sha256(shared) != shared_sha
        or canonical_sha256(delta) != delta_sha
        or fixture_evidence.get("component_ids") != component_ids
        or fixture_evidence.get("connection_ids") != connection_ids
    ):
        raise SelectedStateQualificationError(
            "full-fixture intervention setup inventory differs from its normalized graph"
        )

    context = _mapping(
        contract.get("x_intervention_capture_context"), f"X plan {role} capture context"
    )
    selector = _mapping(context.get("selector_flash_evidence"), f"X plan {role} selector evidence")
    if selector.get("sha256") != expected_selector_evidence_sha256 or fixture_evidence.get(
        "selector_flash_evidence"
    ) != dict(selector):
        raise SelectedStateQualificationError(
            "installed Stage-E setup selector differs from accepted X bench evidence"
        )
    selector_summary = {
        "path": selector.get("path"),
        "sha256": selector.get("sha256"),
        "run_id": selector.get("run_id"),
    }

    setup_file = _evidence_file(
        source_files.get("setup_attestation"), f"X plan {role} setup attestation"
    )
    source_setup, observed_setup_file = _json_file(setup_file, f"X plan {role} setup attestation")
    _exact_keys(
        source_setup,
        _SETUP_ATTESTATION_SOURCE_FIELDS,
        f"X plan {role} setup attestation",
    )
    created_at = _timestamp(source_setup.get("created_at"), "installed setup created_at")
    normalized_setup = _mapping(
        fixture_evidence.get("setup_attestation"), "normalized installed setup attestation"
    )
    _exact_keys(
        normalized_setup,
        _NORMALIZED_SETUP_ATTESTATION_FIELDS,
        "normalized installed setup attestation",
    )
    setup_evidence = _evidence_file(
        normalized_setup.get("setup_evidence"), "installed setup evidence"
    )
    raw_evidence_path = Path(str(source_setup.get("setup_evidence_path", ""))).expanduser()
    if not raw_evidence_path.is_absolute():
        raw_evidence_path = Path(observed_setup_file["path"]).parent / raw_evidence_path
    raw_evidence_path = raw_evidence_path.absolute()
    expected_common = {
        "schema": 1,
        "attestation_kind": _SETUP_ATTESTATION_KIND,
        "attestation_id": _identifier(
            source_setup.get("attestation_id"), "installed setup attestation ID"
        ),
        "run_id": plan.expected_x_run_ids[role],
        "campaign_id": plan.campaign_id,
        "comparable_fixture_group_id": after_binding.get("fixture_id"),
        "stage": FULL_CONDUCTED_STAGE,
        "fixture_manifest_sha256": after_binding.get("fixture_manifest_sha256"),
        "shared_fixture_sha256": shared_sha,
        "stage_delta_sha256": delta_sha,
        "observed_component_ids": component_ids,
        "observed_connection_ids": connection_ids,
        "selector_flash_evidence": selector_summary,
    }
    if (
        any(source_setup.get(field) != expected for field, expected in expected_common.items())
        or str(raw_evidence_path) != setup_evidence["path"]
        or source_setup.get("setup_evidence_sha256") != setup_evidence["sha256"]
        or normalized_setup.get("schema") != 1
        or normalized_setup.get("attestation_kind") != _SETUP_ATTESTATION_KIND
        or normalized_setup.get("attestation_id") != expected_common["attestation_id"]
        or normalized_setup.get("created_at") != created_at.isoformat()
        or normalized_setup.get("created_at_wall_clock_freshness_enforced") is not False
        or any(
            normalized_setup.get(field) != expected
            for field, expected in expected_common.items()
            if field
            not in {
                "schema",
                "attestation_kind",
                "attestation_id",
                "selector_flash_evidence",
            }
        )
        or normalized_setup.get("selector_flash_evidence") != dict(selector)
        or normalized_setup.get("setup_attestation_file") != observed_setup_file
        or normalized_setup.get("setup_evidence") != setup_evidence
    ):
        raise SelectedStateQualificationError(
            "installed setup attestation is not the normalized accepted Stage-E authority"
        )
    return observed_setup_file, setup_evidence


def _component_owns_pointer(document: Mapping[str, Any], pointer: str, component_id: str) -> bool:
    current: object = document
    owners: list[Mapping[str, Any]] = []
    for raw_part in pointer[1:].split("/"):
        if isinstance(current, Mapping):
            owners.append(current)
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return any(owner.get("id") == component_id for owner in owners)


def _expected_x_roles(implicated_stage: str) -> tuple[str, ...]:
    if implicated_stage in _X_BOUNDARY_STAGES:
        return _X_ALL_RUN_ROLES
    if implicated_stage == _X_FULL_FIXTURE_STAGE:
        return _X_FULL_FIXTURE_ROLES
    raise SelectedStateQualificationError("implicated X boundary stage is unsupported")


def _expected_x_topology_stage(role: str, implicated_stage: str) -> str:
    if role in _X_BOUNDARY_ROLES:
        return implicated_stage
    if role in _X_FULL_FIXTURE_ROLES:
        return _X_FULL_FIXTURE_STAGE
    raise SelectedStateQualificationError(f"unsupported X run role: {role}")


def _component_role_for_id(
    *,
    component_id: str,
    before: FullSimultaneousFixture,
    after: FullSimultaneousFixture,
) -> str:
    before_roles = [
        role for role, identity in before.component_ids.items() if identity == component_id
    ]
    after_roles = [
        role for role, identity in after.component_ids.items() if identity == component_id
    ]
    if len(before_roles) != 1 or before_roles != after_roles:
        raise SelectedStateQualificationError(
            "changed component must retain one exact supported fixture role"
        )
    return before_roles[0]


def _validate_change_stage_compatibility(
    *, component_role: str, property_path: str, implicated_stage: str
) -> None:
    prefix = _FIXTURE_COMPONENT_POINTER_PREFIX.get(component_role)
    allowed = _STAGE_COMPONENT_ROLES.get(implicated_stage)
    if prefix is None or allowed is None:
        raise SelectedStateQualificationError(
            "changed component role or implicated stage is unsupported"
        )
    if not property_path.startswith(prefix + "/"):
        raise SelectedStateQualificationError(
            "changed property path is outside the changed component's exact fixture role"
        )
    if component_role not in allowed:
        raise SelectedStateQualificationError(
            f"changed component role {component_role} is absent from implicated stage "
            f"{implicated_stage}"
        )


def _validate_x_topology_change_binding(
    *,
    role: str,
    fixture_evidence: Mapping[str, Any],
    topology_manifest_file: Mapping[str, Any],
    topology_stage: str,
    run_id: str,
    campaign_id: str,
    fixture_id: str,
    board_id: str,
    component_role: str,
    component_id: str,
    property_path: str,
    expected_value: object,
    expected_capture_fixture: Mapping[str, Any],
) -> None:
    """Prove the declared changed leaf is real in this role's topology graph."""

    if component_role not in _STAGE_COMPONENT_ROLES[topology_stage]:
        raise SelectedStateQualificationError(
            f"X plan {role} topology omits changed component role {component_role}"
        )
    if (
        fixture_evidence.get("schema") != 2
        or fixture_evidence.get("fixture_kind") != FIXTURE_KIND_V2
        or fixture_evidence.get("campaign_id") != campaign_id
        or fixture_evidence.get("comparable_fixture_group_id") != fixture_id
        or fixture_evidence.get("stage") != topology_stage
        or fixture_evidence.get("run_id") != run_id
        or fixture_evidence.get("board_id") != board_id
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} topology graph identity differs from the intervention"
        )
    shared = _mapping(fixture_evidence.get("shared_fixture"), f"X plan {role} shared graph")
    delta = _mapping(fixture_evidence.get("stage_delta"), f"X plan {role} stage graph")
    shared_sha = _sha256(
        fixture_evidence.get("shared_fixture_sha256"), f"X plan {role} shared graph SHA-256"
    )
    delta_sha = _sha256(
        fixture_evidence.get("stage_delta_sha256"), f"X plan {role} stage graph SHA-256"
    )
    component_ids, connection_ids = _fixture_graph_inventory(shared, delta)
    if (
        canonical_sha256(shared) != shared_sha
        or canonical_sha256(delta) != delta_sha
        or fixture_evidence.get("component_ids") != component_ids
        or fixture_evidence.get("connection_ids") != connection_ids
        or component_id not in component_ids
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} topology inventory is not derived from its graph"
        )
    topology_document = {"shared_fixture": shared, "stage_delta": delta}
    if not _component_owns_pointer(
        topology_document, property_path, component_id
    ) or canonical_sha256(
        _pointer_value(topology_document, property_path, f"X plan {role} changed property")
    ) != canonical_sha256(expected_value):
        raise SelectedStateQualificationError(
            f"X plan {role} topology does not carry the declared component/property state"
        )

    source_document, observed_manifest_file = _json_file(
        topology_manifest_file, f"X plan {role} topology fixture manifest"
    )
    if (
        source_document.get("schema") != 2
        or source_document.get("fixture_kind") != FIXTURE_KIND_V2
        or source_document.get("campaign_id") != campaign_id
        or source_document.get("comparable_fixture_group_id") != fixture_id
        or source_document.get("stage") != topology_stage
        or source_document.get("board_id") != board_id
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} topology source manifest identity differs"
        )
    source_shared = _mapping(
        source_document.get("shared_fixture"), f"X plan {role} source shared graph"
    )
    source_delta = _mapping(source_document.get("stage_delta"), f"X plan {role} source stage graph")
    source_components, _ = _fixture_graph_inventory(source_shared, source_delta)
    if source_shared != shared or source_delta != delta:
        raise SelectedStateQualificationError(
            f"X plan {role} topology source graph differs from its embedded evidence"
        )
    if (
        source_components != component_ids
        or component_id not in source_components
        or not _component_owns_pointer(source_document, property_path, component_id)
        or canonical_sha256(
            _pointer_value(
                source_document,
                property_path,
                f"X plan {role} source changed property",
            )
        )
        != canonical_sha256(expected_value)
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} source manifest does not carry the declared component/property state"
        )
    if topology_stage == FULL_CONDUCTED_STAGE:
        capture_manifest_path = Path(str(expected_capture_fixture.get("fixture_manifest_path", "")))
        expected_full_file = {
            "path": str(capture_manifest_path),
            "sha256": expected_capture_fixture.get("fixture_manifest_sha256"),
            "size_bytes": (
                capture_manifest_path.stat().st_size if capture_manifest_path.is_file() else -1
            ),
        }
        if observed_manifest_file != expected_full_file:
            raise SelectedStateQualificationError(
                f"X plan {role} full-fixture topology source differs from its capture state"
            )


def _selector_flash_sha256(value: object, *, campaign_id: str, board_id: str) -> str:
    binding = _mapping(value, "X selector-flash evidence")
    _exact_keys(
        binding,
        {
            "schema",
            "binding_kind",
            "path",
            "sha256",
            "campaign_id",
            "run_id",
            "board_id",
            "image_role",
        },
        "X selector-flash evidence",
    )
    path_value = binding.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateQualificationError("X selector-flash evidence path must be absolute")
    path = Path(path_value).expanduser().absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        if current.is_symlink():
            raise SelectedStateQualificationError(
                "X selector-flash evidence path contains a symlink"
            )
    digest = _sha256(binding.get("sha256"), "X selector-flash evidence SHA-256")
    if (
        binding.get("schema") != 1
        or binding.get("binding_kind") != "sealed_selector_flash_evidence_v1"
        or binding.get("campaign_id") != campaign_id
        or binding.get("board_id") != board_id
        or binding.get("image_role") != "bench"
        or not path.is_file()
        or sha256_path(path) != digest
    ):
        raise SelectedStateQualificationError(
            "X selector-flash evidence is not the exact source-bound bench image"
        )
    _identifier(binding.get("run_id"), "X selector-flash run ID")
    return digest


def _control_file_path(
    section: Mapping[str, Any],
    *,
    path_key: str,
    hash_key: str,
    label: str,
) -> Path:
    """Reopen one selector-control file without accepting path indirection."""

    path_value = section.get(path_key)
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateQualificationError(f"{label} path must be absolute")
    path = Path(path_value).expanduser().absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise SelectedStateQualificationError(f"{label} path contains a symlink")
    digest = _sha256(section.get(hash_key), f"{label} SHA-256")
    if not path.is_file() or sha256_path(path) != digest:
        raise SelectedStateQualificationError(f"{label} differs from its production X plan")
    return path


def _sealed_x_selector(
    value: object,
    *,
    campaign_id: str,
    board_id: str,
) -> tuple[SelectorEvidenceBinding, Mapping[str, Any]]:
    """Recursively validate X's sealed selector image and return frozen inputs."""

    digest = _selector_flash_sha256(value, campaign_id=campaign_id, board_id=board_id)
    binding = _mapping(value, "X selector-flash evidence")
    run_id = _identifier(binding.get("run_id"), "X selector-flash run ID")
    path = Path(str(binding["path"]))
    selected = selector_binding_from_sealed(
        path,
        expected_sha256=digest,
        campaign_id=campaign_id,
        run_id=run_id,
        board_id=board_id,
        image_role="bench",
    )
    try:
        sealed_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectedStateQualificationError(
            "cannot reopen recursively validated X selector evidence"
        ) from error
    sealed = _mapping(sealed_value, "sealed X selector evidence")
    if sha256_path(path) != digest:
        raise SelectedStateQualificationError(
            "sealed X selector evidence changed during production-plan validation"
        )
    frozen = _mapping(sealed.get("frozen_inputs"), "sealed X selector frozen inputs")
    return selected, frozen


def _require_control_matches_frozen_file(
    path: Path,
    digest: str,
    frozen_value: object,
    *,
    label: str,
) -> None:
    frozen = _mapping(frozen_value, f"sealed {label}")
    _exact_keys(frozen, {"path", "sha256", "size_bytes"}, f"sealed {label}")
    frozen_path_value = frozen.get("path")
    if not isinstance(frozen_path_value, str) or not Path(frozen_path_value).is_absolute():
        raise SelectedStateQualificationError(f"sealed {label} path must be absolute")
    frozen_path = Path(frozen_path_value).expanduser().absolute()
    if (
        path != frozen_path
        or digest != _sha256(frozen.get("sha256"), f"sealed {label} SHA-256")
        or path.stat().st_size
        != _integer(frozen.get("size_bytes"), f"sealed {label} size", minimum=1)
    ):
        raise SelectedStateQualificationError(
            f"{label} differs from the recursively sealed selector image"
        )


def _validate_production_x_selector_control(
    contract: Mapping[str, Any],
    *,
    stage: str,
    campaign_id: str,
    board_id: str,
    fixture_evidence: Mapping[str, Any],
    context_selector_flash_evidence: object,
) -> str:
    """Require the runner's full static-ALL_OFF control and sealed image tuple."""

    selected, frozen = _sealed_x_selector(
        context_selector_flash_evidence,
        campaign_id=campaign_id,
        board_id=board_id,
    )
    raw_control = contract.get("selector_control")
    if stage not in _PRODUCTION_X_SELECTOR_CONNECTED_STAGES:
        if raw_control is not None:
            raise SelectedStateQualificationError(
                "selector-disconnected production X plan must not include selector control"
            )
        return selected.sha256

    control = _mapping(raw_control, "production X selector control")
    _exact_keys(
        control,
        {
            "schema",
            "mode",
            "bench_manifest",
            "openocd_config",
            "control_profile",
            "command",
            "selector_flash_evidence",
            "target_image_admission_contract",
        },
        "production X selector control",
    )
    if (
        control.get("schema") != 1
        or control.get("mode") != "reviewed_static_selector_mailbox_all_off"
        or control.get("selector_flash_evidence") != context_selector_flash_evidence
        or fixture_evidence.get("selector_flash_evidence") != context_selector_flash_evidence
    ):
        raise SelectedStateQualificationError(
            "production X selector control is not the fixture's exact sealed bench image"
        )

    frozen_files = _mapping(frozen.get("files"), "sealed X selector files")
    required_frozen_files = {
        "build_manifest",
        "firmware_bin",
        "openocd_config",
        "profile",
        "profile_header",
    }
    if not required_frozen_files.issubset(frozen_files):
        raise SelectedStateQualificationError(
            "sealed X selector evidence lacks the complete production control tuple"
        )

    bench = _mapping(control.get("bench_manifest"), "production X bench manifest")
    _exact_keys(
        bench,
        {
            "path",
            "file_sha256",
            "elf_sha256",
            "mailbox_address",
            "mailbox_size",
            "mailbox_magic",
            "mailbox_version",
            "max_lease_ms",
            "mailbox_offsets",
        },
        "production X bench manifest",
    )
    config = _mapping(control.get("openocd_config"), "production X OpenOCD config")
    _exact_keys(config, {"path", "file_sha256"}, "production X OpenOCD config")
    profile = _mapping(control.get("control_profile"), "production X control profile")
    _exact_keys(
        profile,
        {
            "path",
            "file_sha256",
            "header_path",
            "header_file_sha256",
            "profile_id",
            "revision",
            "contract_sha256",
            "all_off_code",
        },
        "production X control profile",
    )
    bench_path = _control_file_path(
        bench,
        path_key="path",
        hash_key="file_sha256",
        label="production X bench manifest",
    )
    config_path = _control_file_path(
        config,
        path_key="path",
        hash_key="file_sha256",
        label="production X OpenOCD config",
    )
    profile_path = _control_file_path(
        profile,
        path_key="path",
        hash_key="file_sha256",
        label="production X control profile",
    )
    header_path = _control_file_path(
        profile,
        path_key="header_path",
        hash_key="header_file_sha256",
        label="production X control profile header",
    )
    _require_control_matches_frozen_file(
        bench_path,
        str(bench["file_sha256"]),
        frozen_files["build_manifest"],
        label="bench manifest",
    )
    _require_control_matches_frozen_file(
        config_path,
        str(config["file_sha256"]),
        frozen_files["openocd_config"],
        label="OpenOCD config",
    )
    _require_control_matches_frozen_file(
        profile_path,
        str(profile["file_sha256"]),
        frozen_files["profile"],
        label="control profile",
    )
    _require_control_matches_frozen_file(
        header_path,
        str(profile["header_file_sha256"]),
        frozen_files["profile_header"],
        label="control profile header",
    )
    try:
        loaded_bench = BenchManifest.load(bench_path)
        loaded_profile = load_profile(profile_path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SelectedStateQualificationError(
            "cannot reopen production X selector control artifacts"
        ) from error
    expected_bench = {
        "path": str(bench_path),
        "file_sha256": sha256_path(bench_path),
        "elf_sha256": loaded_bench.elf_sha256,
        "mailbox_address": loaded_bench.address,
        "mailbox_size": loaded_bench.size,
        "mailbox_magic": loaded_bench.magic,
        "mailbox_version": loaded_bench.version,
        "max_lease_ms": loaded_bench.max_lease_ms,
        "mailbox_offsets": dict(loaded_bench.offsets),
    }
    expected_profile = {
        "path": str(profile_path),
        "file_sha256": sha256_path(profile_path),
        "header_path": str(header_path),
        "header_file_sha256": sha256_path(header_path),
        "profile_id": loaded_profile.profile_id,
        "revision": loaded_profile.revision,
        "contract_sha256": loaded_profile.contract_sha256,
        "all_off_code": loaded_profile.all_off_code,
    }
    if dict(bench) != expected_bench or dict(profile) != expected_profile:
        raise SelectedStateQualificationError(
            "production X selector control facts differ from the sealed control files"
        )

    command = _mapping(control.get("command"), "production X selector command")
    _exact_keys(
        command,
        {"code", "lease_ms", "wait_until_applied", "readback_required"},
        "production X selector command",
    )
    if dict(command) != {
        "code": loaded_profile.all_off_code,
        "lease_ms": 0,
        "wait_until_applied": True,
        "readback_required": True,
    }:
        raise SelectedStateQualificationError(
            "production X selector command is not static lease-free ALL_OFF with readback"
        )

    target = _mapping(
        control.get("target_image_admission_contract"),
        "production X target-image admission",
    )
    _exact_keys(
        target,
        {
            "schema",
            "flash_base_address",
            "firmware_bin_path",
            "firmware_bin_sha256",
            "firmware_bin_size_bytes",
            "board_id",
            "selector_flash_evidence_sha256",
            "full_bin_extent_and_uid_required_before_mailbox",
        },
        "production X target-image admission",
    )
    frozen_firmware = _mapping(frozen_files["firmware_bin"], "sealed selector firmware BIN")
    _exact_keys(
        frozen_firmware,
        {"path", "sha256", "size_bytes"},
        "sealed selector firmware BIN",
    )
    expected_target = {
        "schema": 1,
        "flash_base_address": 0x08000000,
        "firmware_bin_path": frozen_firmware["path"],
        "firmware_bin_sha256": selected.firmware_bin_sha256,
        "firmware_bin_size_bytes": frozen_firmware["size_bytes"],
        "board_id": board_id,
        "selector_flash_evidence_sha256": selected.sha256,
        "full_bin_extent_and_uid_required_before_mailbox": True,
    }
    if dict(target) != expected_target:
        raise SelectedStateQualificationError(
            "production X target-image admission differs from the sealed firmware BIN"
        )
    firmware_path = _control_file_path(
        {
            "path": target["firmware_bin_path"],
            "sha256": target["firmware_bin_sha256"],
        },
        path_key="path",
        hash_key="sha256",
        label="production X target firmware BIN",
    )
    _require_control_matches_frozen_file(
        firmware_path,
        str(target["firmware_bin_sha256"]),
        frozen_firmware,
        label="firmware BIN",
    )
    if stage == FULL_CONDUCTED_STAGE:
        prior = _mapping(
            fixture_evidence.get("prior_stage_binding"),
            "production X full-fixture prior-stage binding",
        )
        if prior.get("prior_selector_control_sha256") != canonical_sha256(control):
            raise SelectedStateQualificationError(
                "production X Stage E selector control differs from immediately prior Stage C"
            )
    return selected.sha256


def _validate_production_x_conditions(value: object, *, stage: str) -> None:
    """Require the runner's exact six-gain ladder plus five fresh attribution views."""

    observed = _sequence(value, "production X conditions")
    gains = (-35.0, -30.0, -25.0, -20.0, -15.0, -10.0)
    expected: list[dict[str, Any]] = []
    for index, gain in enumerate(gains):
        is_attribution = gain == -20.0
        expected.append(
            {
                "plan_index": index,
                "condition_id": f"{stage}-tx{gain:g}db",
                "stage": stage,
                "center_frequency_hz": 5_800_000_000,
                "center_frequency_policy": "experimental_5g8_user_requested",
                "sample_rate_hz": 1_000_000,
                "bandwidth_hz": 800_000,
                "tone_offset_hz": 100_000,
                "tx_channel": 0,
                "tx_port": "TX1",
                "tx2_required_exact_muted": True,
                "tx_hardware_gain_db": gain,
                "dds_scale": 0.125,
                "receiver_gain_db": 60,
                "samples_per_frame": 100_000,
                "frame_count": 3,
                "sample_count": 300_000,
                "kernel_buffers": 8,
                "fresh_stream_required": True,
                "condition_role": (
                    "linearity_ladder_and_attribution_repeat"
                    if is_attribution
                    else "linearity_ladder"
                ),
                "attribution_repeat_index": 1 if is_attribution else None,
                "attribution_repeat_count": 5 if is_attribution else None,
            }
        )
    attribution = expected[3]
    for repeat_index in range(2, 6):
        expected.append(
            {
                **attribution,
                "plan_index": len(expected),
                "condition_id": f"{stage}-tx-20db-attribution-repeat{repeat_index}",
                "condition_role": "attribution_repeat",
                "attribution_repeat_index": repeat_index,
            }
        )
    for index, condition_value in enumerate(observed):
        condition = _mapping(condition_value, f"production X condition {index}")
        _exact_keys(
            condition,
            _PRODUCTION_X_CONDITION_FIELDS,
            f"production X condition {index}",
        )
    if list(observed) != expected:
        raise SelectedStateQualificationError(
            "production X conditions differ from the fixed leakage-runner schedule"
        )


def _validate_production_dependency_attestation(
    value: object,
    *,
    label: str,
) -> tuple[Mapping[str, Any], str, str]:
    dependency = _mapping(value, label)
    _exact_keys(dependency, _PRODUCTION_DEPENDENCY_ATTESTATION_FIELDS, label)
    dependency_commit = _commit(dependency.get("commit"), f"{label} commit")
    repository_value = dependency.get("repository_path")
    if not isinstance(repository_value, str) or not Path(repository_value).is_absolute():
        raise SelectedStateQualificationError(f"{label} repository path must be absolute")
    repository = Path(repository_value).expanduser().absolute()
    expected_lock_files = ["pyproject.toml", "uv.lock"]
    if (
        dependency.get("schema") != 1
        or dependency.get("dependency") != "pluto-plus-utils"
        or dependency.get("head") != dependency_commit
        or dependency.get("clean_worktree_verified") is not True
        or dependency.get("lock_metadata_files") != expected_lock_files
        or dependency.get("python_executable") != str(repository / ".venv/bin/python")
        or dependency.get("python_prefix") != str(repository / ".venv")
    ):
        raise SelectedStateQualificationError(f"{label} identity is incomplete")
    files = _sequence(dependency.get("files"), f"{label} files")
    for index, file_value in enumerate(files):
        _exact_keys(
            _mapping(file_value, f"{label} file {index}"),
            {"path", "sha256", "size_bytes"},
            f"{label} file {index}",
        )
    required_paths = (
        *expected_lock_files,
        *_PRODUCTION_DEPENDENCY_MODULE_PATHS.values(),
    )
    try:
        verify_source_tree_binding(
            dependency,
            label=label,
            required_relative_paths=required_paths,
        )
    except FileArtifactAdmissionError as error:
        raise SelectedStateQualificationError(str(error)) from error

    imported = _sequence(dependency.get("imported_modules"), f"{label} imported modules")
    if len(imported) != len(_PRODUCTION_DEPENDENCY_MODULE_PATHS):
        raise SelectedStateQualificationError(f"{label} imported-module closure is incomplete")
    observed_modules: dict[str, str] = {}
    for index, module_value in enumerate(imported):
        module = _mapping(module_value, f"{label} imported module {index}")
        _exact_keys(
            module,
            {"module", "path", "relative_path", "sha256", "size_bytes"},
            f"{label} imported module {index}",
        )
        name = module.get("module")
        relative = module.get("relative_path")
        if not isinstance(name, str) or not isinstance(relative, str):
            raise SelectedStateQualificationError(
                f"{label} imported module {index} identity is malformed"
            )
        expected_relative = _PRODUCTION_DEPENDENCY_MODULE_PATHS.get(name)
        if expected_relative is None or relative != expected_relative or name in observed_modules:
            raise SelectedStateQualificationError(
                f"{label} imported module {index} differs from the reviewed closure"
            )
        module_file = _evidence_file(
            {
                "path": module.get("path"),
                "sha256": module.get("sha256"),
                "size_bytes": module.get("size_bytes"),
            },
            f"{label} imported module {name}",
        )
        if module_file["path"] != str(repository / expected_relative):
            raise SelectedStateQualificationError(
                f"{label} imported module {name} escaped its attested repository"
            )
        observed_modules[name] = relative
    if observed_modules != _PRODUCTION_DEPENDENCY_MODULE_PATHS:
        raise SelectedStateQualificationError(f"{label} imported-module closure differs")
    return dependency, dependency_commit, canonical_sha256(dependency)


def _validate_production_x_source(value: object) -> tuple[str, str, str, str, str]:
    source = _mapping(value, "production X source")
    _exact_keys(source, _PRODUCTION_X_SOURCE_FIELDS, "production X source")
    source_commit = _commit(source.get("smateway_commit"), "production X source commit")
    dependency, dependency_commit, dependency_sha = _validate_production_dependency_attestation(
        source.get("pluto_plus_utils_source_attestation"),
        label="production X pluto-plus-utils source attestation",
    )
    if source.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha:
        raise SelectedStateQualificationError(
            "production X dependency source attestation is incomplete or self-inconsistent"
        )
    native = source.get("native_libiio_runtime_attestation")
    try:
        validated_native = validate_runtime_attestation(native)
    except ValueError as error:
        raise SelectedStateQualificationError(
            "production X native-libiio runtime attestation is invalid"
        ) from error
    native_sha = canonical_sha256(validated_native)
    if source.get("native_libiio_runtime_attestation_sha256") != native_sha:
        raise SelectedStateQualificationError(
            "production X native-libiio runtime hash is inconsistent"
        )
    expected_tools = {
        "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
        "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
    }
    if any(source.get(key) != value for key, value in expected_tools.items()):
        raise SelectedStateQualificationError(
            "production X source does not name the reviewed leakage-runner implementation"
        )
    return (
        source_commit,
        dependency_commit,
        dependency_sha,
        native_sha,
        canonical_sha256(source),
    )


def _validate_production_x_runner_contract(
    contract: Mapping[str, Any],
    *,
    run_id: str,
    board_id: str,
    stage: str,
    expected_serial: str,
    campaign_id: str,
    fixture_evidence: Mapping[str, Any],
    topology_fixture_sha256: str,
    context_selector_flash_evidence: object,
) -> tuple[str, str, str, str, str, str]:
    """Reopen the exact immutable contract emitted by the production X runner."""

    _exact_keys(contract, _PRODUCTION_X_PLAN_FIELDS, "production X leakage-runner contract")
    if (
        contract.get("schema") != 1
        or contract.get("plan_kind") != _PRODUCTION_X_PLAN_KIND
        or contract.get("run_id") != run_id
        or contract.get("board_id") != board_id
        or contract.get("topology_stage") != stage
        or contract.get("fixture_evidence") != fixture_evidence
        or contract.get("fixture_evidence_sha256") != topology_fixture_sha256
    ):
        raise SelectedStateQualificationError(
            "production X leakage-runner contract identity is invalid"
        )
    expected_stage_contract = _PRODUCTION_X_STAGE_CONTRACTS.get(stage)
    if contract.get("stage_contract") != expected_stage_contract:
        raise SelectedStateQualificationError(
            "production X stage contract differs from the reviewed leakage runner"
        )

    (
        source_commit,
        dependency_commit,
        dependency_attestation_sha,
        native_attestation_sha,
        source_identity_sha,
    ) = _validate_production_x_source(contract.get("source"))
    configuration = _mapping(contract.get("configuration"), "production X configuration")
    _exact_keys(configuration, _PRODUCTION_X_CONFIGURATION_FIELDS, "production X configuration")
    uri = configuration.get("uri")
    uri_suffix = uri.removeprefix("usb:") if isinstance(uri, str) else ""
    uri_parts = uri_suffix.split(".")
    if len(uri_parts) < 2 or any(not part.isdigit() for part in uri_parts):
        raise SelectedStateQualificationError(
            "production X configuration requires an exact current USB bus address"
        )
    expected_configuration = {
        "serial": expected_serial,
        "uri": uri,
        "center_frequency_hz": 5_800_000_000,
        "tone_offset_hz_requested": 100_000,
        "sample_rate_hz": 1_000_000,
        "bandwidth_hz": 800_000,
        "receiver_gain_db": 60,
        "tx_channel": 0,
        "tx_port": "TX1",
        "tx2_required_exact_muted": True,
        "dds_scale": 0.125,
        "tx_hardware_gains_db": [-35.0, -30.0, -25.0, -20.0, -15.0, -10.0],
        "samples_per_frame": 100_000,
        "frame_count": 3,
        "sample_count_per_condition": 300_000,
        "duration_s_per_condition": 0.3,
        "kernel_buffers": 8,
        "fresh_stream_per_condition": True,
        "metadata_abi": 2,
        "automatic_retry_count": 0,
        "attribution_gain_db": -20.0,
        "attribution_repeat_count": 5,
        "attribution_repeats_require_unique_fresh_streams": True,
        "pilot_frequency_refinement_required": True,
        "minimum_pilot_confidence": 0.25,
        "minimum_pilot_phase_step_coherence": 0.995,
        "maximum_pilot_phase_rms_deg": 6.0,
    }
    if dict(configuration) != expected_configuration:
        raise SelectedStateQualificationError(
            "production X configuration differs from the fixed leakage-runner acquisition"
        )

    selector_sha = _validate_production_x_selector_control(
        contract,
        stage=stage,
        campaign_id=campaign_id,
        board_id=board_id,
        fixture_evidence=fixture_evidence,
        context_selector_flash_evidence=context_selector_flash_evidence,
    )
    confirmations = _mapping(
        contract.get("operator_confirmations_required"),
        "production X operator confirmations",
    )
    expected_confirmations = {
        "no_antennas_anywhere": True,
        "tx1_matched_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "no_component_or_connection_movement_since_setup_attestation": True,
        "fixture_evidence_sha256": topology_fixture_sha256,
        "exact_stage": stage,
        "topology_confirmation_token": _PRODUCTION_X_STAGE_ORDER_AND_TOKEN[stage][1],
    }
    if dict(confirmations) != expected_confirmations:
        raise SelectedStateQualificationError(
            "production X operator confirmations differ from the fixed topology contract"
        )

    safety = _mapping(contract.get("safety"), "production X safety contract")
    expected_safety = {
        "exact_serial_and_current_usb_uri_required": True,
        "tx1_only": True,
        "tx2_gain_readback_required_db": -80.0,
        "inactive_dds_scales_required_zero": True,
        "exact_mute_before_stage": True,
        "exact_mute_after_every_condition": True,
        "exact_mute_in_stage_finally": True,
        "headroom_failure_stops_stronger_conditions": True,
        "failure_fragments_are_quarantined": True,
        "automatic_retry_count": 0,
        "read_only_usb_identity_scan_before_rf": True,
        "resolved_usb_uri_must_equal_requested_uri": True,
        "native_libiio_exact_path_version_hash_required": True,
        "fixture_v2_files_and_prior_plan_rehashed_before_rf": True,
        "selector_static_all_off_readback_required": (
            stage in _PRODUCTION_X_SELECTOR_CONNECTED_STAGES
        ),
    }
    if dict(safety) != expected_safety:
        raise SelectedStateQualificationError(
            "production X safety contract differs from the reviewed leakage runner"
        )

    board_root = Path("/home/pi/.local/state/smateway/boards") / board_id
    artifact_root = board_root / "pluto-usb-captures"
    run_root = artifact_root / "leakage-runs" / run_id
    storage = _mapping(contract.get("storage"), "production X storage contract")
    expected_storage = {
        "medium": "raspberry_pi_local_filesystem",
        "board_state_root": str(board_root),
        "artifact_root": str(artifact_root),
        "run_capture_root": str(run_root),
        "pluto_onboard_storage_used": False,
        "estimated_raw_iq_bytes": 24_000_000,
    }
    if dict(storage) != expected_storage:
        raise SelectedStateQualificationError(
            "production X storage is not the fixed local-Raspberry-Pi artifact root"
        )

    characterization = fixture_evidence.get("characterization_summary")
    eligible = bool(
        isinstance(characterization, Mapping)
        and characterization.get("causal_attribution_fixture_eligible") is True
    )
    interpretation = _mapping(contract.get("interpretation"), "production X interpretation")
    expected_interpretation = {
        "purpose": "diagnose coherent TX1-to-RX2 leakage by physical topology stage",
        "marker_required": False,
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
        "causal_attribution_fixture_eligible": eligible,
        "uncharacterized_fixture_is_screening_only": not eligible,
        "may_be_used_as_selector_calibration": False,
        "rx2_tone_absence_is_a_valid_low_leakage_result": True,
        "one_hot_path_diagnosis": {
            "implemented_by_this_runner": False,
            "required_future_runner": "run_5g8_one_hot_path_ladder.py",
            "reason": (
                "per-port path response requires a separate immutable state/readback plan; "
                "the present runner measures only static ALL_OFF topology leakage"
            ),
        },
    }
    if dict(interpretation) != expected_interpretation:
        raise SelectedStateQualificationError(
            "production X interpretation differs from the runner's non-calibration scope"
        )
    _validate_production_x_conditions(contract.get("conditions"), stage=stage)
    return (
        source_commit,
        dependency_commit,
        dependency_attestation_sha,
        native_attestation_sha,
        source_identity_sha,
        selector_sha,
    )


def _x_plan_facts(
    plan_file: Mapping[str, Any],
    *,
    role: str,
    contract_id: str,
    campaign_id: str,
    board_id: str,
    implicated_stage: str,
    before_fixture: Mapping[str, Any],
    installed_after_fixture: Mapping[str, Any],
    changed_component_role: str,
    changed_component_id: str,
    changed_property_path: str,
    expected_change_value: object,
) -> dict[str, Any]:
    """Reopen one immutable general-runner plan and derive its X authority."""

    plan_document, observed_file = _json_file(plan_file, f"X plan {role} file")
    reject_replace_placeholders(plan_document, f"X plan {role}")
    _exact_keys(
        plan_document,
        {
            "schema",
            "plan_contract",
            "plan_contract_sha256",
            "plan_contract_hash_provenance",
            "immutable",
        },
        f"X plan {role} envelope",
    )
    contract = _mapping(plan_document.get("plan_contract"), f"X plan {role} contract")
    if (
        plan_document.get("schema") != 1
        or plan_document.get("immutable") is not True
        or plan_document.get("plan_contract_sha256") != canonical_sha256(contract)
        or plan_document.get("plan_contract_hash_provenance")
        != "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
    ):
        raise SelectedStateQualificationError(f"X plan {role} immutable envelope is invalid")
    run_id = _identifier(contract.get("run_id"), f"X plan {role} run ID")
    stage = _identifier(contract.get("topology_stage"), f"X plan {role} topology stage")
    if stage != _expected_x_topology_stage(role, implicated_stage):
        raise SelectedStateQualificationError(f"X plan {role} qualifies the wrong topology stage")
    fixture_evidence = _mapping(
        contract.get("fixture_evidence"), f"X plan {role} topology fixture evidence"
    )
    topology_fixture_sha = _sha256(
        contract.get("fixture_evidence_sha256"), f"X plan {role} topology fixture SHA-256"
    )
    source_files = _mapping(
        fixture_evidence.get("source_files"), f"X plan {role} fixture source files"
    )
    _exact_keys(
        source_files,
        {"fixture_manifest", "setup_attestation"},
        f"X plan {role} fixture source files",
    )
    topology_manifest_file = _evidence_file(
        source_files.get("fixture_manifest"), f"X plan {role} fixture_manifest"
    )
    _evidence_file(source_files.get("setup_attestation"), f"X plan {role} setup_attestation")
    if (
        canonical_sha256(fixture_evidence) != topology_fixture_sha
        or fixture_evidence.get("stage") != stage
        or fixture_evidence.get("run_id") != run_id
        or fixture_evidence.get("board_id") != board_id
        or fixture_evidence.get("campaign_id") != campaign_id
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} topology fixture is not source-bound to this run"
        )
    prebinding = _mapping(contract.get("x_intervention_prebinding"), f"X plan {role} prebinding")
    _exact_keys(
        prebinding,
        {
            "schema",
            "binding_kind",
            "contract_id",
            "run_role",
            "installed_fixture_revision_sha256",
        },
        f"X plan {role} prebinding",
    )
    installed_revision = _sha256(
        installed_after_fixture.get("fixture_revision_sha256"),
        "installed after fixture revision",
    )
    if (
        prebinding.get("schema") != 1
        or prebinding.get("binding_kind") != X_PREBINDING_KIND
        or prebinding.get("contract_id") != contract_id
        or prebinding.get("run_role") != role
        or prebinding.get("installed_fixture_revision_sha256") != installed_revision
    ):
        raise SelectedStateQualificationError(f"X plan {role} prebinding differs from phase 1")
    context = _mapping(
        contract.get("x_intervention_capture_context"), f"X plan {role} capture context"
    )
    _exact_keys(
        context,
        {
            "schema",
            "binding_kind",
            "implicated_boundary_stage",
            "acquisition_index",
            "freshness_epoch_id",
            "capture_state_fixture",
            "installed_after_fixture",
            "selector_flash_evidence",
        },
        f"X plan {role} capture context",
    )
    capture_fixture = _mapping(
        context.get("capture_state_fixture"), f"X plan {role} capture-state fixture"
    )
    context_after = _mapping(
        context.get("installed_after_fixture"), f"X plan {role} installed-after fixture"
    )
    validate_full_simultaneous_fixture(capture_fixture)
    validate_full_simultaneous_fixture(context_after)
    expected_capture = before_fixture if role.endswith("_baseline") else installed_after_fixture
    if (
        context.get("schema") != 1
        or context.get("binding_kind") != X_CAPTURE_CONTEXT_KIND
        or context.get("implicated_boundary_stage") != implicated_stage
        or dict(capture_fixture) != dict(expected_capture)
        or dict(context_after) != dict(installed_after_fixture)
    ):
        raise SelectedStateQualificationError(
            f"X plan {role} capture context binds the wrong intervention state"
        )
    acquisition_index = _integer(
        context.get("acquisition_index"), f"X plan {role} acquisition index", minimum=1
    )
    freshness_epoch_id = _identifier(
        context.get("freshness_epoch_id"), f"X plan {role} freshness epoch"
    )
    try:
        normalized_fixture = validate_x_capture_linkage(
            fixture_evidence,
            capture_fixture_binding=expected_capture,
            context_selector_flash_evidence=context.get("selector_flash_evidence"),
            verify_selector_file=True,
        )
    except FixtureV2Error as error:
        raise SelectedStateQualificationError(
            f"X plan {role} production fixture/capture linkage is invalid: {error}"
        ) from error
    (
        source_commit,
        dependency_commit,
        dependency_attestation_sha,
        native_attestation_sha,
        source_identity_sha,
        selector_sha,
    ) = _validate_production_x_runner_contract(
        contract,
        run_id=run_id,
        board_id=board_id,
        stage=stage,
        expected_serial=_identifier(
            expected_capture.get("pluto_serial"), f"X plan {role} Pluto serial"
        ),
        campaign_id=campaign_id,
        fixture_evidence=fixture_evidence,
        topology_fixture_sha256=topology_fixture_sha,
        context_selector_flash_evidence=context.get("selector_flash_evidence"),
    )
    _validate_x_topology_change_binding(
        role=role,
        fixture_evidence=normalized_fixture,
        topology_manifest_file=topology_manifest_file,
        topology_stage=stage,
        run_id=run_id,
        campaign_id=campaign_id,
        fixture_id=str(before_fixture.get("fixture_id")),
        board_id=board_id,
        component_role=changed_component_role,
        component_id=changed_component_id,
        property_path=changed_property_path,
        expected_value=expected_change_value,
        expected_capture_fixture=expected_capture,
    )
    return {
        "run_id": run_id,
        "plan_file": observed_file,
        "topology_stage": stage,
        "topology_fixture_sha256": topology_fixture_sha,
        "acquisition_index": acquisition_index,
        "freshness_epoch_id": freshness_epoch_id,
        "selector_evidence_sha256": selector_sha,
        "source_commit": source_commit,
        "dependency_commit": dependency_commit,
        "dependency_attestation_sha256": dependency_attestation_sha,
        "native_attestation_sha256": native_attestation_sha,
        "source_identity_sha256": source_identity_sha,
        "physical_fixture": {
            "shared_fixture": normalized_fixture["shared_fixture"],
            "stage_delta": normalized_fixture["stage_delta"],
        },
    }


def validate_intervention_change_plan(value: Mapping[str, Any]) -> InterventionChangePlan:
    """Validate a pre-X immutable plan whose one changed leaf comes from fixture-v2 bytes."""

    document = _mapping(value, "intervention change plan")
    reject_replace_placeholders(document, "intervention change plan")
    _exact_keys(document, _CHANGE_PLAN_FIELDS, "intervention change plan")
    if document.get("schema") != 2 or document.get("plan_kind") != INTERVENTION_PLAN_KIND:
        raise SelectedStateQualificationError("intervention change plan schema or kind is invalid")
    contract_id = _identifier(document.get("contract_id"), "intervention contract ID")
    before_binding = _mapping(document.get("before_fixture"), "before fixture")
    after_binding = _mapping(document.get("installed_after_fixture"), "installed after fixture")
    before = validate_full_simultaneous_fixture(before_binding)
    after = validate_full_simultaneous_fixture(after_binding)
    board_id = _identifier(document.get("board_id"), "intervention board ID")
    if (
        before.board_id != board_id
        or after.board_id != board_id
        or before.fixture_id != after.fixture_id
        or before.hardware_revision != after.hardware_revision
        or before.pluto_serial != after.pluto_serial
        or before.fixture_revision_sha256 == after.fixture_revision_sha256
    ):
        raise SelectedStateQualificationError(
            "before/after fixtures must be distinct revisions of one exact physical fixture"
        )
    campaign_id = _identifier(document.get("campaign_id"), "intervention campaign ID")
    try:
        before_manifest = validate_fixture_manifest(
            Path(before.fixture_manifest_path),
            expected_stage=FULL_CONDUCTED_STAGE,
            expected_board_id=before.board_id,
            expected_serial=before.pluto_serial,
        )
        after_manifest = validate_fixture_manifest(
            Path(after.fixture_manifest_path),
            expected_stage=FULL_CONDUCTED_STAGE,
            expected_board_id=after.board_id,
            expected_serial=after.pluto_serial,
        )
    except FixtureV2Error as error:
        raise SelectedStateQualificationError(
            f"cannot validate production intervention fixture manifests: {error}"
        ) from error
    if (
        before_manifest.file_sha256 != before.fixture_manifest_sha256
        or after_manifest.file_sha256 != after.fixture_manifest_sha256
        or before_manifest.campaign_id != campaign_id
        or after_manifest.campaign_id != campaign_id
        or before_manifest.comparable_fixture_group_id != before.fixture_id
        or after_manifest.comparable_fixture_group_id != after.fixture_id
        or before_manifest.component_ids != after_manifest.component_ids
        or before_manifest.connection_ids != after_manifest.connection_ids
    ):
        raise SelectedStateQualificationError(
            "before/after fixture manifests differ from the production intervention identity"
        )
    before_physical = {
        "shared_fixture": before_manifest.shared_fixture,
        "stage_delta": before_manifest.stage_delta,
    }
    after_physical = {
        "shared_fixture": after_manifest.shared_fixture,
        "stage_delta": after_manifest.stage_delta,
    }
    change = _mapping(document.get("change"), "intervention change")
    _exact_keys(
        change,
        {"component_id", "property_path", "before", "after", "reversible", "restore_instruction"},
        "intervention change",
    )
    component_id = _identifier(change.get("component_id"), "changed component ID")
    property_path = change.get("property_path")
    if not isinstance(property_path, str) or not property_path.startswith("/"):
        raise SelectedStateQualificationError("changed property path must be a JSON pointer")
    differences = _leaf_differences(before_physical, after_physical)
    if differences != [property_path]:
        raise SelectedStateQualificationError(
            f"fixture manifests must differ at exactly the predeclared leaf; observed={differences}"
        )
    if (
        _pointer_value(before_physical, property_path, "before fixture property")
        != change.get("before")
        or _pointer_value(after_physical, property_path, "after fixture property")
        != change.get("after")
        or not _component_owns_pointer(before_physical, property_path, component_id)
        or not _component_owns_pointer(after_physical, property_path, component_id)
    ):
        raise SelectedStateQualificationError(
            "predeclared change does not match the source-bound component fixture leaf"
        )
    instruction = change.get("restore_instruction")
    if (
        change.get("before") == change.get("after")
        or change.get("reversible") is not True
        or not isinstance(instruction, str)
        or not instruction.strip()
    ):
        raise SelectedStateQualificationError("intervention must be nontrivial and reversible")
    if component_id not in before.component_ids.values():
        raise SelectedStateQualificationError("changed component is outside the bound fixture")
    component_role = _component_role_for_id(
        component_id=component_id,
        before=before,
        after=after,
    )
    implicated_stage = _identifier(
        document.get("implicated_boundary_stage"), "implicated X boundary stage"
    )
    _validate_change_stage_compatibility(
        component_role=component_role,
        property_path=property_path,
        implicated_stage=implicated_stage,
    )
    expected_roles = _expected_x_roles(implicated_stage)
    x_plans = _mapping(document.get("x_run_plans"), "predeclared X run plans")
    _exact_keys(x_plans, set(expected_roles), "predeclared X run plans")
    run_ids: dict[str, str] = {}
    plan_sha256s: dict[str, str] = {}
    topology_stages: dict[str, str] = {}
    topology_fixture_sha256s: dict[str, str] = {}
    physical_fixtures: dict[str, Mapping[str, Any]] = {}
    acquisition_indices: dict[str, int] = {}
    freshness_epochs: set[str] = set()
    selector_sha256s: set[str] = set()
    source_commits: set[str] = set()
    dependency_commits: set[str] = set()
    dependency_attestation_sha256s: set[str] = set()
    native_attestation_sha256s: set[str] = set()
    source_identity_sha256s: set[str] = set()
    plan_hashes: set[str] = set()
    for role in expected_roles:
        binding = _mapping(x_plans.get(role), f"X plan {role}")
        _exact_keys(binding, {"run_id", "plan_file"}, f"X plan {role}")
        plan_file = _evidence_file(binding.get("plan_file"), f"X plan {role} file")
        if plan_file["sha256"] in plan_hashes:
            raise SelectedStateQualificationError("predeclared X runs must use distinct plan bytes")
        plan_hashes.add(plan_file["sha256"])
        plan_sha256s[role] = plan_file["sha256"]
        facts = _x_plan_facts(
            plan_file,
            role=role,
            contract_id=contract_id,
            campaign_id=campaign_id,
            board_id=board_id,
            implicated_stage=implicated_stage,
            before_fixture=before_binding,
            installed_after_fixture=after_binding,
            changed_component_role=component_role,
            changed_component_id=component_id,
            changed_property_path=property_path,
            expected_change_value=(
                change.get("before") if role.endswith("baseline") else change.get("after")
            ),
        )
        declared_run_id = _identifier(binding.get("run_id"), f"X plan {role} run ID")
        if declared_run_id != facts["run_id"]:
            raise SelectedStateQualificationError(
                f"X plan {role} run ID differs from its immutable plan"
            )
        run_ids[role] = declared_run_id
        topology_stages[role] = str(facts["topology_stage"])
        topology_fixture_sha256s[role] = str(facts["topology_fixture_sha256"])
        physical_fixtures[role] = _mapping(
            facts["physical_fixture"], f"X plan {role} physical fixture"
        )
        acquisition_indices[role] = int(facts["acquisition_index"])
        freshness_epochs.add(str(facts["freshness_epoch_id"]))
        selector_sha256s.add(str(facts["selector_evidence_sha256"]))
        source_commits.add(str(facts["source_commit"]))
        dependency_commits.add(str(facts["dependency_commit"]))
        dependency_attestation_sha256s.add(str(facts["dependency_attestation_sha256"]))
        native_attestation_sha256s.add(str(facts["native_attestation_sha256"]))
        source_identity_sha256s.add(str(facts["source_identity_sha256"]))
    if len(set(run_ids.values())) != len(run_ids):
        raise SelectedStateQualificationError("predeclared X run IDs must be distinct")
    for baseline_role, intervention_role in (
        ("boundary_baseline", "boundary_intervention"),
        ("full_fixture_baseline", "full_fixture_intervention"),
    ):
        if baseline_role not in physical_fixtures:
            continue
        physical_differences = _leaf_differences(
            physical_fixtures[baseline_role],
            physical_fixtures[intervention_role],
        )
        if physical_differences != [property_path]:
            raise SelectedStateQualificationError(
                f"X {baseline_role}/{intervention_role} physical fixtures must differ only at "
                f"the declared leaf; observed={physical_differences}"
            )
    indices = tuple(acquisition_indices[role] for role in expected_roles)
    if indices != tuple(range(indices[0], indices[0] + len(indices))):
        raise SelectedStateQualificationError(
            "predeclared X runs must be one consecutive acquisition sequence"
        )
    if (
        len(freshness_epochs) != 1
        or len(selector_sha256s) != 1
        or len(source_commits) != 1
        or len(dependency_commits) != 1
        or len(dependency_attestation_sha256s) != 1
        or len(native_attestation_sha256s) != 1
        or len(source_identity_sha256s) != 1
    ):
        raise SelectedStateQualificationError(
            "predeclared X runs must share one freshness epoch, selector, and source identity"
        )
    if document.get("diagnostic_restoration_policy") != (
        "restoration_is_diagnostic_only_and_requires_source_bound_reapplication_before_q"
    ):
        raise SelectedStateQualificationError("intervention restoration policy is unsafe")
    _timestamp(document.get("created_at"), "intervention plan created_at")
    return InterventionChangePlan(
        contract_id=contract_id,
        campaign_id=campaign_id,
        board_id=board_id,
        before_fixture_revision_sha256=before.fixture_revision_sha256,
        installed_after_fixture_revision_sha256=after.fixture_revision_sha256,
        changed_component_id=component_id,
        changed_property_path=property_path,
        before=change.get("before"),
        after=change.get("after"),
        implicated_boundary_stage=implicated_stage,
        expected_x_roles=expected_roles,
        expected_x_run_ids=run_ids,
        expected_x_plan_sha256s=plan_sha256s,
        expected_x_topology_stages=topology_stages,
        expected_x_topology_fixture_sha256s=topology_fixture_sha256s,
        expected_x_acquisition_indices=acquisition_indices,
        expected_x_freshness_epoch_id=next(iter(freshness_epochs)),
        expected_x_selector_evidence_sha256=next(iter(selector_sha256s)),
        expected_x_source_commit=next(iter(source_commits)),
        expected_x_dependency_commit=next(iter(dependency_commits)),
        expected_x_dependency_attestation_sha256=next(iter(dependency_attestation_sha256s)),
        expected_x_native_attestation_sha256=next(iter(native_attestation_sha256s)),
        expected_x_source_identity_sha256=next(iter(source_identity_sha256s)),
    )


def _x_run_binding(
    value: object,
    *,
    role: str,
    contract_id: str,
    change_plan_sha256: str,
    plan: InterventionChangePlan,
) -> XRunBinding:
    document = _mapping(value, f"X run {role}")
    reject_replace_placeholders(document, f"X run {role} binding")
    _exact_keys(document, _X_RUN_BINDING_FIELDS, f"X run {role}")
    if (
        document.get("schema") != 1
        or document.get("binding_kind") != X_RUN_BINDING_KIND
        or document.get("contract_id") != contract_id
        or document.get("change_plan_sha256") != change_plan_sha256
        or document.get("run_role") != role
        or document.get("run_id") != plan.expected_x_run_ids[role]
        or document.get("acceptance_revalidated") is not True
    ):
        raise SelectedStateQualificationError(f"X run {role} is not bound to its predeclared plan")
    plan_file = _evidence_file(document.get("plan_file"), f"X run {role} plan")
    if plan_file["sha256"] != plan.expected_x_plan_sha256s[role]:
        raise SelectedStateQualificationError(f"X run {role} changed its predeclared plan bytes")
    manifest, manifest_file = _json_file(document.get("manifest_file"), f"X run {role} manifest")
    reject_replace_placeholders(manifest, f"X run {role} manifest")
    _exact_keys(
        manifest,
        {
            "schema",
            "run_kind",
            "contract_id",
            "run_role",
            "run_id",
            "status",
            "captured_at",
            "acquisition_index",
            "freshness_epoch_id",
            "intervention_state_fixture_revision_sha256",
            "topology_stage",
            "topology_fixture_sha256",
            "source_commit",
            "dependency_commit",
            "selector_evidence_sha256",
            "immutable_plan_file",
            "captures",
            "measurement_quality_rejection_reasons",
            "final_mute_verified",
            "final_selector_safe_state",
        },
        f"X run {role} accepted manifest",
    )
    expected_revision = (
        plan.before_fixture_revision_sha256
        if role.endswith("baseline")
        else plan.installed_after_fixture_revision_sha256
    )
    if (
        manifest.get("schema") != 1
        or manifest.get("run_kind") != X_CAPTURE_MANIFEST_KIND
        or manifest.get("contract_id") != contract_id
        or manifest.get("run_role") != role
        or manifest.get("run_id") != plan.expected_x_run_ids[role]
        or manifest.get("status") != "accepted"
        or manifest.get("acquisition_index") != plan.expected_x_acquisition_indices[role]
        or manifest.get("freshness_epoch_id") != plan.expected_x_freshness_epoch_id
        or manifest.get("intervention_state_fixture_revision_sha256") != expected_revision
        or manifest.get("topology_stage") != plan.expected_x_topology_stages[role]
        or manifest.get("topology_fixture_sha256") != plan.expected_x_topology_fixture_sha256s[role]
        or manifest.get("selector_evidence_sha256") != plan.expected_x_selector_evidence_sha256
        or manifest.get("immutable_plan_file") != plan_file
        or manifest.get("measurement_quality_rejection_reasons") != []
        or manifest.get("final_mute_verified") is not True
    ):
        raise SelectedStateQualificationError(
            f"X run {role} manifest is not accepted against its immutable plan"
        )
    topology_stage = str(manifest["topology_stage"])
    safe_state = _mapping(
        manifest.get("final_selector_safe_state"), f"X run {role} final selector safe state"
    )
    if topology_stage in _X_BOUNDARY_STAGES[:2]:
        _exact_keys(
            safe_state,
            {
                "status",
                "topology_stage",
                "selector_rf_state",
                "selector_power_state",
                "selector_control_harness_state",
            },
            f"X run {role} disconnected selector safe state",
        )
        if dict(safe_state) != {
            "status": "physical_disconnect_verified",
            "topology_stage": topology_stage,
            "selector_rf_state": "rf_disconnected",
            "selector_power_state": "bench_power_off",
            "selector_control_harness_state": "disconnected",
        }:
            raise SelectedStateQualificationError(
                f"X run {role} does not prove the disconnected selector safe state"
            )
    else:
        _exact_keys(
            safe_state,
            {"status", "topology_stage", "mailbox_all_off_verified"},
            f"X run {role} mailbox selector safe state",
        )
        if dict(safe_state) != {
            "status": "mailbox_all_off_verified",
            "topology_stage": topology_stage,
            "mailbox_all_off_verified": True,
        }:
            raise SelectedStateQualificationError(
                f"X run {role} does not prove final mailbox ALL_OFF"
            )
    captures = _sequence(manifest.get("captures"), f"X run {role} accepted captures")
    if not captures:
        raise SelectedStateQualificationError(f"X run {role} manifest has no accepted captures")
    manifest_streams: list[str] = []
    manifest_raw_files: list[dict[str, Any]] = []
    for index, raw_capture in enumerate(captures, start=1):
        capture = _mapping(raw_capture, f"X run {role} capture {index}")
        _exact_keys(
            capture,
            {
                "stream_id",
                "raw_iq_file",
                "metadata_file",
                "condition_record_file",
                "abi2_continuity_verified",
                "measurement_quality_passed",
            },
            f"X run {role} capture {index}",
        )
        if (
            capture.get("abi2_continuity_verified") is not True
            or capture.get("measurement_quality_passed") is not True
        ):
            raise SelectedStateQualificationError(f"X run {role} contains a rejected capture")
        manifest_streams.append(
            _identifier(capture.get("stream_id"), f"X run {role} capture stream ID")
        )
        manifest_raw_files.append(
            _evidence_file(
                capture.get("raw_iq_file"),
                f"X run {role} raw IQ",
                require_local_rpi=True,
            )
        )
        _evidence_file(capture.get("metadata_file"), f"X run {role} metadata")
        _evidence_file(capture.get("condition_record_file"), f"X run {role} condition record")
    streams = tuple(
        _identifier(item, f"X run {role} stream ID")
        for item in _sequence(document.get("stream_ids"), f"X run {role} stream IDs")
    )
    raw_files = tuple(
        _evidence_file(item, f"X run {role} bound raw IQ", require_local_rpi=True)
        for item in _sequence(document.get("raw_iq_files"), f"X run {role} raw IQ files")
    )
    if streams != tuple(manifest_streams) or raw_files != tuple(manifest_raw_files):
        raise SelectedStateQualificationError(
            f"X run {role} sealed streams differ from its accepted manifest"
        )
    raw_hashes = tuple(item["sha256"] for item in raw_files)
    if (
        not streams
        or len(streams) != len(set(streams))
        or len(raw_hashes) != len(streams)
        or len(raw_hashes) != len(set(raw_hashes))
    ):
        raise SelectedStateQualificationError(f"X run {role} lacks unique source-bound captures")
    revision = _sha256(
        document.get("intervention_state_fixture_revision_sha256"),
        f"X run {role} intervention-state fixture",
    )
    if revision != expected_revision:
        raise SelectedStateQualificationError(f"X run {role} qualifies the wrong fixture revision")
    if (
        document.get("topology_stage") != plan.expected_x_topology_stages[role]
        or document.get("topology_fixture_sha256") != plan.expected_x_topology_fixture_sha256s[role]
        or document.get("captured_at") != manifest.get("captured_at")
        or document.get("acquisition_index") != plan.expected_x_acquisition_indices[role]
        or document.get("freshness_epoch_id") != plan.expected_x_freshness_epoch_id
        or document.get("source_commit") != plan.expected_x_source_commit
        or document.get("dependency_commit") != plan.expected_x_dependency_commit
        or document.get("source_commit") != manifest.get("source_commit")
        or document.get("dependency_commit") != manifest.get("dependency_commit")
        or document.get("selector_evidence_sha256") != plan.expected_x_selector_evidence_sha256
        or document.get("manifest_file") != manifest_file
    ):
        raise SelectedStateQualificationError(f"X run {role} binding differs from source evidence")
    return XRunBinding(
        run_role=role,
        run_id=str(document["run_id"]),
        captured_at=_timestamp(document.get("captured_at"), f"X run {role} captured_at"),
        acquisition_index=_integer(
            document.get("acquisition_index"), f"X run {role} acquisition index", minimum=1
        ),
        freshness_epoch_id=_identifier(
            document.get("freshness_epoch_id"), f"X run {role} freshness epoch"
        ),
        intervention_state_fixture_revision_sha256=revision,
        topology_stage=topology_stage,
        topology_fixture_sha256=str(document["topology_fixture_sha256"]),
        source_commit=_commit(document.get("source_commit"), f"X run {role} source commit"),
        dependency_commit=_commit(
            document.get("dependency_commit"), f"X run {role} dependency commit"
        ),
        selector_evidence_sha256=_sha256(
            document.get("selector_evidence_sha256"), f"X run {role} selector evidence"
        ),
        stream_ids=streams,
        raw_iq_sha256s=raw_hashes,
    )


def _validate_intervention_support_analysis(
    *,
    support_document: Mapping[str, Any],
    contract_id: str,
    plan_file: Mapping[str, Any],
    expected_roles: Sequence[str],
    x_runs_document: Mapping[str, Any],
    expected_manifest_sha256s: Mapping[str, str],
    source_commit: str,
    dependency_commit: str,
    expected_dependency_attestation_sha256: str,
    expected_native_attestation_sha256: str,
    selector_evidence_sha256: str,
) -> None:
    """Recompute the fixed support gate from a source-bound analyzer document."""

    analysis_document, analysis_file = _json_file(
        support_document.get("analysis_file"), "intervention support analysis"
    )
    _exact_keys(
        analysis_document,
        {
            "schema",
            "analysis_kind",
            "created_at",
            "contract_id",
            "change_plan_file",
            "x_run_manifest_files",
            "x_run_manifest_sha256s",
            "x_run_source_identity",
            "analysis_runtime",
            "normalized_repeats",
            "qualification",
            "input_identity_sha256",
        },
        "intervention support analysis",
    )
    if (
        analysis_document.get("schema") != 1
        or analysis_document.get("analysis_kind") != INTERVENTION_SUPPORT_ANALYSIS_KIND
        or analysis_document.get("contract_id") != contract_id
        or analysis_document.get("change_plan_file") != dict(plan_file)
        or support_document.get("analysis_file") != analysis_file
    ):
        raise SelectedStateQualificationError(
            "support analysis is not bound to the intervention change plan"
        )
    _timestamp(analysis_document.get("created_at"), "support analysis created_at")
    manifest_files = _mapping(
        analysis_document.get("x_run_manifest_files"), "support X manifest files"
    )
    manifest_hashes = _mapping(
        analysis_document.get("x_run_manifest_sha256s"), "support X manifest hashes"
    )
    _exact_keys(manifest_files, set(expected_roles), "support X manifest files")
    _exact_keys(manifest_hashes, set(expected_roles), "support X manifest hashes")
    for role in expected_roles:
        observed = _evidence_file(manifest_files[role], f"support X {role} manifest")
        expected_binding = _mapping(x_runs_document[role], role).get("manifest_file")
        if (
            observed != expected_binding
            or observed["sha256"] != expected_manifest_sha256s[role]
            or manifest_hashes[role] != expected_manifest_sha256s[role]
        ):
            raise SelectedStateQualificationError(
                f"support analysis X {role} manifest binding differs"
            )
    source_identity = _mapping(
        analysis_document.get("x_run_source_identity"), "support X source identity"
    )
    _exact_keys(
        source_identity,
        {
            "smateway_commit",
            "dependency_commit",
            "dependency_attestation_sha256",
            "native_attestation_sha256",
            "selector_evidence_sha256",
        },
        "support X source identity",
    )
    expected_source_identity = {
        "smateway_commit": source_commit,
        "dependency_commit": dependency_commit,
        "dependency_attestation_sha256": expected_dependency_attestation_sha256,
        "native_attestation_sha256": expected_native_attestation_sha256,
        "selector_evidence_sha256": selector_evidence_sha256,
    }
    if dict(source_identity) != expected_source_identity:
        raise SelectedStateQualificationError(
            "support analysis source identity differs from immutable production X plans"
        )
    dependency_attestation_sha = _sha256(
        source_identity.get("dependency_attestation_sha256"),
        "support dependency attestation SHA-256",
    )
    native_attestation_sha = _sha256(
        source_identity.get("native_attestation_sha256"),
        "support native attestation SHA-256",
    )
    runtime = _mapping(analysis_document.get("analysis_runtime"), "support analysis runtime")
    _exact_keys(
        runtime,
        {
            "source",
            "dependency",
            "native",
            "source_commit",
            "dependency_commit",
            "native_attestation_sha256",
        },
        "support analysis runtime",
    )
    source = _mapping(runtime.get("source"), "support analysis Smateway source")
    _exact_keys(
        source,
        {
            "schema",
            "repository",
            "commit",
            "clean_source_files_verified",
            "files",
            "source_files_sha256",
        },
        "support analysis Smateway source",
    )
    if (
        source.get("schema") != 1
        or source.get("clean_source_files_verified") is not True
        or source.get("commit") != source_commit
        or runtime.get("source_commit") != source_commit
        or source.get("source_files_sha256") != canonical_sha256(source.get("files"))
    ):
        raise SelectedStateQualificationError(
            "support analysis Smateway source attestation is inconsistent"
        )
    try:
        verify_source_tree_binding(
            source,
            label="intervention support analyzer",
            required_relative_paths=_INTERVENTION_SUPPORT_SOURCE_FILES,
        )
    except FileArtifactAdmissionError as error:
        raise SelectedStateQualificationError(str(error)) from error
    dependency = _mapping(runtime.get("dependency"), "support dependency runtime")
    native = _mapping(runtime.get("native"), "support native runtime")
    _, validated_dependency_commit, validated_dependency_sha = (
        _validate_production_dependency_attestation(
            dependency,
            label="support pluto-plus-utils runtime attestation",
        )
    )
    try:
        validated_native = validate_runtime_attestation(native)
    except ValueError as error:
        raise SelectedStateQualificationError(
            "support native-libiio runtime attestation is invalid"
        ) from error
    validated_native_sha = canonical_sha256(validated_native)
    if (
        runtime.get("dependency_commit") != dependency_commit
        or validated_dependency_commit != dependency_commit
        or validated_dependency_sha != dependency_attestation_sha
        or validated_dependency_sha != expected_dependency_attestation_sha256
        or runtime.get("native_attestation_sha256") != native_attestation_sha
        or validated_native_sha != native_attestation_sha
        or validated_native_sha != expected_native_attestation_sha256
    ):
        raise SelectedStateQualificationError(
            "support analysis dependency/native runtime differs from immutable X source identity"
        )
    repeat_documents = _mapping(
        analysis_document.get("normalized_repeats"), "support normalized repeats"
    )
    _exact_keys(repeat_documents, set(expected_roles), "support normalized repeats")
    try:
        cohorts = {
            role: tuple(
                intervention_repeat_from_document(item, role=role)
                for item in _sequence(repeat_documents[role], f"support {role} repeats")
            )
            for role in expected_roles
        }
        for role in expected_roles:
            admitted = _mapping(x_runs_document[role], f"X run {role}")
            admitted_streams = set(
                _sequence(admitted.get("stream_ids"), f"X run {role} stream IDs")
            )
            admitted_raw = {
                _mapping(item, f"X run {role} raw IQ").get("sha256")
                for item in _sequence(admitted.get("raw_iq_files"), f"X run {role} raw IQ")
            }
            observed_streams = {item.stream_id for item in cohorts[role]}
            observed_raw = {item.raw_iq_sha256 for item in cohorts[role]}
            if (
                len(observed_streams) != len(cohorts[role])
                or len(observed_raw) != len(cohorts[role])
                or not observed_streams.issubset(admitted_streams)
                or not observed_raw.issubset(admitted_raw)
            ):
                raise SelectedStateQualificationError(
                    f"support {role} repeats are not source-bound to admitted X captures"
                )
        recomputed = qualify_intervention_support(cohorts)
    except InterventionSupportError as error:
        raise SelectedStateQualificationError(str(error)) from error
    recomputed_document = json.loads(
        json.dumps(asdict(recomputed), sort_keys=True, allow_nan=False, default=str)
    )
    if analysis_document.get("qualification") != recomputed_document:
        raise SelectedStateQualificationError(
            "support qualification differs from independently recomputed fixed gate"
        )
    input_identity = {
        "change_plan_file": dict(plan_file),
        "x_run_manifest_files": dict(manifest_files),
        "x_run_source_identity": dict(source_identity),
        "analysis_runtime_identity": {
            "source_commit": source_commit,
            "source_files_sha256": source["source_files_sha256"],
            "dependency_commit": dependency_commit,
            "dependency_attestation_sha256": dependency_attestation_sha,
            "native_attestation_sha256": native_attestation_sha,
        },
    }
    if analysis_document.get("input_identity_sha256") != canonical_sha256(input_identity):
        raise SelectedStateQualificationError("support analysis input identity hash differs")
    passed = recomputed.simultaneous_improvement_gate_passed
    if (
        support_document.get("simultaneous_improvement_gate_passed") is not passed
        or support_document.get("rejection_reasons") != list(recomputed.rejection_reasons)
        or support_document.get("accepted") is not passed
        or support_document.get("decision") != ("supported_fix" if passed else "unsupported")
    ):
        raise SelectedStateQualificationError(
            "support result projection differs from recomputed analysis"
        )


def validate_intervention_contract(
    value: Mapping[str, Any], *, fixture: FullSimultaneousFixture | None = None
) -> InterventionContract:
    """Admit source-bound X evidence and an installed, explicitly adopted after-state."""

    document = _mapping(value, "intervention contract")
    reject_replace_placeholders(document, "intervention contract")
    _exact_keys(
        document,
        {
            "schema",
            "contract_kind",
            "contract_id",
            "sealed_at",
            "change_plan_file",
            "change_plan_sha256",
            "x_runs",
            "diagnostic_restoration",
            "adoption",
            "support_evidence_file",
        },
        "intervention contract",
    )
    if document.get("schema") != 2 or document.get("contract_kind") != INTERVENTION_KIND:
        raise SelectedStateQualificationError("intervention contract schema or kind is invalid")
    contract_id = _identifier(document.get("contract_id"), "intervention contract ID")
    plan_document, plan_file = _json_file(document.get("change_plan_file"), "change plan file")
    plan_sha = _sha256(document.get("change_plan_sha256"), "change plan SHA-256")
    if plan_file["sha256"] != plan_sha:
        raise SelectedStateQualificationError(
            "intervention seal does not bind exact change-plan bytes"
        )
    plan = validate_intervention_change_plan(plan_document)
    if plan.contract_id != contract_id:
        raise SelectedStateQualificationError("intervention contract ID differs from change plan")
    x_runs_document = _mapping(document.get("x_runs"), "sealed X runs")
    _exact_keys(x_runs_document, set(plan.expected_x_roles), "sealed X runs")
    runs = {
        role: _x_run_binding(
            x_runs_document[role],
            role=role,
            contract_id=contract_id,
            change_plan_sha256=plan_sha,
            plan=plan,
        )
        for role in plan.expected_x_roles
    }
    baseline_runs = tuple(
        runs[role] for role in plan.expected_x_roles if role.endswith("_baseline")
    )
    intervention_runs = tuple(
        runs[role] for role in plan.expected_x_roles if role.endswith("_intervention")
    )
    all_runs = tuple(runs[role] for role in plan.expected_x_roles)
    if len({item.run_id for item in all_runs}) != len(plan.expected_x_roles):
        raise SelectedStateQualificationError("X comparison run IDs are not distinct")
    if len({item.freshness_epoch_id for item in all_runs}) != 1:
        raise SelectedStateQualificationError("X runs do not share one fresh comparison epoch")
    acquisition_indices = tuple(item.acquisition_index for item in all_runs)
    if acquisition_indices != tuple(
        range(acquisition_indices[0], acquisition_indices[0] + len(all_runs))
    ):
        raise SelectedStateQualificationError(
            "X boundary and full-fixture runs must be one consecutive acquisition sequence"
        )
    capture_times = tuple(item.captured_at for item in all_runs)
    if capture_times != tuple(sorted(capture_times)) or len(set(capture_times)) != len(all_runs):
        raise SelectedStateQualificationError(
            "X boundary and full-fixture capture times must be strictly increasing"
        )
    identity_fields = ("source_commit", "dependency_commit", "selector_evidence_sha256")
    if any(
        getattr(item, field) != getattr(all_runs[0], field)
        for item in all_runs[1:]
        for field in identity_fields
    ):
        raise SelectedStateQualificationError("X runs differ in source or selector identity")
    installed_setup_file, installed_setup_evidence = _full_fixture_intervention_setup_authority(
        plan_document,
        plan=plan,
        expected_selector_evidence_sha256=all_runs[0].selector_evidence_sha256,
    )
    for baseline, intervention in zip(baseline_runs, intervention_runs, strict=True):
        if intervention.acquisition_index != baseline.acquisition_index + 1:
            raise SelectedStateQualificationError(
                "each X baseline/intervention pair must be consecutive acquisitions"
            )
        if intervention.captured_at <= baseline.captured_at:
            raise SelectedStateQualificationError("each X intervention must follow its baseline")
    all_streams = [stream for item in all_runs for stream in item.stream_ids]
    all_raw = [digest for item in all_runs for digest in item.raw_iq_sha256s]
    if len(all_streams) != len(set(all_streams)) or len(all_raw) != len(set(all_raw)):
        raise SelectedStateQualificationError("X runs reuse streams or raw IQ bytes")

    restoration = _mapping(document.get("diagnostic_restoration"), "diagnostic restoration")
    _exact_keys(
        restoration,
        {"status", "restoration_evidence_file", "reapplication_evidence_file"},
        "diagnostic restoration",
    )
    restoration_status = restoration.get("status")
    requires_state_transitions = plan.implicated_boundary_stage in _X_BOUNDARY_STAGES
    reapplication_at: datetime | None = None
    restored_at: datetime | None = None
    if restoration_status == "not_performed":
        if (
            restoration.get("restoration_evidence_file") is not None
            or restoration.get("reapplication_evidence_file") is not None
        ):
            raise SelectedStateQualificationError("unperformed restoration cannot include evidence")
        if requires_state_transitions:
            raise SelectedStateQualificationError(
                "four-role X evidence requires source-bound after-to-before restoration and "
                "before-to-after reapplication transitions"
            )
    elif restoration_status == "restored_then_reapplied":
        restoration_document, _ = _json_file(
            restoration.get("restoration_evidence_file"), "restoration evidence"
        )
        reapplication_document, _ = _json_file(
            restoration.get("reapplication_evidence_file"), "reapplication evidence"
        )
        transition_fields = {
            "schema",
            "evidence_kind",
            "contract_id",
            "observed_at",
            "from_fixture_revision_sha256",
            "to_fixture_revision_sha256",
            "setup_evidence_file",
            "accepted",
        }
        _exact_keys(restoration_document, transition_fields, "restoration evidence")
        _exact_keys(reapplication_document, transition_fields, "reapplication evidence")
        for transition, label, source_revision, target_revision in (
            (
                restoration_document,
                "restoration",
                plan.installed_after_fixture_revision_sha256,
                plan.before_fixture_revision_sha256,
            ),
            (
                reapplication_document,
                "reapplication",
                plan.before_fixture_revision_sha256,
                plan.installed_after_fixture_revision_sha256,
            ),
        ):
            _evidence_file(transition.get("setup_evidence_file"), f"{label} setup evidence")
            if (
                transition.get("schema") != 1
                or transition.get("evidence_kind") != "5g8_fixture_state_transition_attestation_v1"
                or transition.get("contract_id") != contract_id
                or transition.get("from_fixture_revision_sha256") != source_revision
                or transition.get("to_fixture_revision_sha256") != target_revision
                or transition.get("accepted") is not True
            ):
                raise SelectedStateQualificationError(
                    f"diagnostic {label} transition is inconsistent"
                )
        restored_at = _timestamp(restoration_document.get("observed_at"), "restoration observed_at")
        reapplication_at = _timestamp(
            reapplication_document.get("observed_at"), "reapplication observed_at"
        )
        if reapplication_at <= restored_at:
            raise SelectedStateQualificationError(
                "after-state reapplication must follow diagnostic restoration"
            )
    else:
        raise SelectedStateQualificationError("diagnostic restoration status is invalid")

    if requires_state_transitions:
        assert restored_at is not None and reapplication_at is not None
        boundary_intervention = runs["boundary_intervention"]
        full_fixture_baseline = runs["full_fixture_baseline"]
        full_fixture_intervention = runs["full_fixture_intervention"]
        if not (
            boundary_intervention.captured_at < restored_at < full_fixture_baseline.captured_at
        ):
            raise SelectedStateQualificationError(
                "after-to-before restoration must occur between the boundary intervention "
                "and full-fixture baseline captures"
            )
        if not (
            full_fixture_baseline.captured_at
            < reapplication_at
            < full_fixture_intervention.captured_at
        ):
            raise SelectedStateQualificationError(
                "before-to-after reapplication must occur between the full-fixture baseline "
                "and intervention captures"
            )

    adoption = _mapping(document.get("adoption"), "supported-fix adoption")
    _exact_keys(
        adoption,
        {
            "decision",
            "installed_state",
            "installed_fixture_revision_sha256",
            "installation_attestation_file",
        },
        "supported-fix adoption",
    )
    installed_revision = _sha256(
        adoption.get("installed_fixture_revision_sha256"), "installed fixture revision"
    )
    expected_manifest_sha256s = {
        role: _mapping(x_runs_document[role], role)["manifest_file"]["sha256"]
        for role in plan.expected_x_roles
    }
    if (
        adoption.get("decision") != "adopt_supported_fix"
        or adoption.get("installed_state") != "after"
        or installed_revision != plan.installed_after_fixture_revision_sha256
    ):
        raise SelectedStateQualificationError(
            "Q requires adoption of the installed after-state, never a restored baseline"
        )
    installation_document, installation_file = _json_file(
        adoption.get("installation_attestation_file"), "installation attestation"
    )
    _exact_keys(
        installation_document,
        {
            "schema",
            "evidence_kind",
            "contract_id",
            "observed_at",
            "installed_state",
            "fixture_revision_sha256",
            "fixture_manifest_file",
            "setup_attestation_file",
            "setup_evidence_file",
            "accepted",
        },
        "installation attestation",
    )
    installed_manifest = _evidence_file(
        installation_document.get("fixture_manifest_file"), "installed fixture manifest"
    )
    after_fixture = _mapping(plan_document.get("installed_after_fixture"), "planned after fixture")
    observed_installed_setup = _evidence_file(
        installation_document.get("setup_attestation_file"), "installed setup attestation"
    )
    observed_installed_setup_evidence = _evidence_file(
        installation_document.get("setup_evidence_file"), "installed setup evidence"
    )
    if (
        installation_document.get("schema") != 1
        or installation_document.get("evidence_kind") != "5g8_installed_after_state_attestation_v1"
        or installation_document.get("contract_id") != contract_id
        or installation_document.get("installed_state") != "after"
        or installation_document.get("fixture_revision_sha256") != installed_revision
        or installed_manifest["sha256"] != after_fixture.get("fixture_manifest_sha256")
        or observed_installed_setup != installed_setup_file
        or observed_installed_setup_evidence != installed_setup_evidence
        or installation_document.get("accepted") is not True
    ):
        raise SelectedStateQualificationError("installed after-state attestation is inconsistent")
    installed_at = _timestamp(
        installation_document.get("observed_at"), "installed state observed_at"
    )
    if installed_at <= max(item.captured_at for item in intervention_runs):
        raise SelectedStateQualificationError(
            "installed after-state attestation must follow accepted X intervention captures"
        )
    if reapplication_at is not None and installed_at <= reapplication_at:
        raise SelectedStateQualificationError(
            "installed after-state attestation must follow diagnostic reapplication"
        )
    support_document, support_file = _json_file(
        document.get("support_evidence_file"), "support evidence"
    )
    _exact_keys(
        support_document,
        {
            "schema",
            "result_kind",
            "contract_id",
            "decision",
            "accepted",
            "x_run_manifest_sha256s",
            "analysis_file",
            "simultaneous_improvement_gate_passed",
            "rejection_reasons",
        },
        "support evidence",
    )
    _validate_intervention_support_analysis(
        support_document=support_document,
        contract_id=contract_id,
        plan_file=plan_file,
        expected_roles=plan.expected_x_roles,
        x_runs_document=x_runs_document,
        expected_manifest_sha256s=expected_manifest_sha256s,
        source_commit=all_runs[0].source_commit,
        dependency_commit=all_runs[0].dependency_commit,
        expected_dependency_attestation_sha256=(plan.expected_x_dependency_attestation_sha256),
        expected_native_attestation_sha256=plan.expected_x_native_attestation_sha256,
        selector_evidence_sha256=all_runs[0].selector_evidence_sha256,
    )
    if (
        support_document.get("schema") != 1
        or support_document.get("result_kind") != INTERVENTION_SUPPORT_RESULT_KIND
        or support_document.get("contract_id") != contract_id
        or support_document.get("decision") != "supported_fix"
        or support_document.get("accepted") is not True
        or support_document.get("simultaneous_improvement_gate_passed") is not True
        or support_document.get("rejection_reasons") != []
        or support_document.get("x_run_manifest_sha256s") != expected_manifest_sha256s
    ):
        raise SelectedStateQualificationError("support result is not bound to all admitted X runs")
    _timestamp(document.get("sealed_at"), "intervention sealed_at")
    if fixture is not None and (
        fixture.board_id != plan.board_id
        or fixture.fixture_revision_sha256 != installed_revision
        or plan.changed_component_id not in fixture.component_ids.values()
    ):
        raise SelectedStateQualificationError(
            "selected-state fixture is not the adopted installed after-state"
        )
    return InterventionContract(
        contract_id=contract_id,
        campaign_id=plan.campaign_id,
        board_id=plan.board_id,
        baseline_fixture_revision_sha256=plan.before_fixture_revision_sha256,
        installed_after_fixture_revision_sha256=installed_revision,
        changed_component_id=plan.changed_component_id,
        changed_property_path=plan.changed_property_path,
        implicated_boundary_stage=plan.implicated_boundary_stage,
        before=plan.before,
        after=plan.after,
        source_commit=all_runs[0].source_commit,
        dependency_commit=all_runs[0].dependency_commit,
        selector_evidence_sha256=all_runs[0].selector_evidence_sha256,
        baseline_stream_ids=tuple(stream for item in baseline_runs for stream in item.stream_ids),
        intervention_stream_ids=tuple(
            stream for item in intervention_runs for stream in item.stream_ids
        ),
        baseline_raw_iq_sha256s=tuple(
            digest for item in baseline_runs for digest in item.raw_iq_sha256s
        ),
        intervention_raw_iq_sha256s=tuple(
            digest for item in intervention_runs for digest in item.raw_iq_sha256s
        ),
        support_evidence_sha256=support_file["sha256"],
        adoption_attestation_sha256=installation_file["sha256"],
        diagnostic_restoration_status=str(restoration_status),
    )


@dataclass(frozen=True, slots=True)
class QualificationContext:
    campaign_id: str
    board_id: str
    fixture_revision_sha256: str
    selector_evidence_sha256: str
    selector_image_role: ImageRole
    source_commit: str
    dependency_commit: str
    native_attestation_sha256: str
    device_identity_sha256: str
    device_identity_snapshot: DeviceIdentitySnapshot
    plan_sha256: str


_CONTEXT_FIELDS = set(QualificationContext.__dataclass_fields__)


def _context(
    value: object,
    *,
    fixture: FullSimultaneousFixture,
    selector: SelectorEvidenceBinding,
    expected_role: ImageRole,
) -> QualificationContext:
    document = _mapping(value, "qualification context")
    _exact_keys(document, _CONTEXT_FIELDS, "qualification context")
    role = document.get("selector_image_role")
    if role != expected_role or selector.image_role != expected_role:
        raise SelectedStateQualificationError(
            f"qualification requires exact {expected_role} live-image evidence"
        )
    identity = _device_identity_snapshot(
        document.get("device_identity_snapshot"), "qualification device identity snapshot"
    )
    result = QualificationContext(
        campaign_id=_identifier(document.get("campaign_id"), "campaign ID"),
        board_id=_identifier(document.get("board_id"), "board ID"),
        fixture_revision_sha256=_sha256(
            document.get("fixture_revision_sha256"), "fixture revision SHA-256"
        ),
        selector_evidence_sha256=_sha256(
            document.get("selector_evidence_sha256"), "selector evidence SHA-256"
        ),
        selector_image_role=expected_role,
        source_commit=_commit(document.get("source_commit"), "source commit"),
        dependency_commit=_commit(document.get("dependency_commit"), "dependency commit"),
        native_attestation_sha256=_sha256(
            document.get("native_attestation_sha256"), "native attestation SHA-256"
        ),
        device_identity_sha256=_sha256(
            document.get("device_identity_sha256"), "device identity SHA-256"
        ),
        device_identity_snapshot=identity,
        plan_sha256=_sha256(document.get("plan_sha256"), "plan SHA-256"),
    )
    if (
        result.campaign_id != selector.campaign_id
        or result.board_id != fixture.board_id
        or result.board_id != selector.board_id
        or result.fixture_revision_sha256 != fixture.fixture_revision_sha256
        or result.selector_evidence_sha256 != selector.sha256
        or identity.serial != fixture.pluto_serial
        or identity.native_attestation_sha256 != result.native_attestation_sha256
    ):
        raise SelectedStateQualificationError(
            "qualification context differs from fixture, selector, or device evidence"
        )
    return result


@dataclass(frozen=True, slots=True)
class CaptureBinding:
    run_id: str
    stream_id: str
    artifact_id: str
    raw_iq_path: str
    raw_iq_sha256: str
    raw_iq_size_bytes: int
    metadata_path: str
    metadata_sha256: str
    metadata_size_bytes: int
    condition_record_path: str
    condition_record_sha256: str
    condition_record_size_bytes: int
    leaf_source_sha256s: tuple[str, ...]


_CAPTURE_FIELDS = {
    "run_id",
    "stream_id",
    "artifact_id",
    "raw_iq_path",
    "raw_iq_sha256",
    "raw_iq_size_bytes",
    "metadata_path",
    "metadata_sha256",
    "metadata_size_bytes",
    "condition_record_path",
    "condition_record_sha256",
    "condition_record_size_bytes",
    "leaf_source_sha256s",
    "plan_sha256",
    "fixture_revision_sha256",
    "selector_evidence_sha256",
    "source_commit",
    "dependency_commit",
    "native_attestation_sha256",
    "device_identity_sha256",
}


def _capture_file(
    path_value: object,
    sha_value: object,
    size_value: object,
    label: str,
) -> tuple[str, str, int]:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise SelectedStateQualificationError(f"{label} path must be absolute")
    path = Path(path_value).expanduser().absolute()
    forbidden_roots = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(path == root or root in path.parents for root in forbidden_roots):
        raise SelectedStateQualificationError(
            f"{label} must use local RPi storage, never removable or Pluto storage"
        )
    if path.is_symlink() or not path.is_file():
        raise SelectedStateQualificationError(f"{label} must exist as a regular non-symlink file")
    declared_size = _integer(size_value, f"{label} size", minimum=1)
    if path.stat().st_size != declared_size:
        raise SelectedStateQualificationError(f"{label} size differs from capture binding")
    declared_sha = _sha256(sha_value, f"{label} SHA-256")
    if sha256_path(path) != declared_sha:
        raise SelectedStateQualificationError(f"{label} bytes differ from capture binding")
    return str(path), declared_sha, declared_size


def _capture_binding(value: object, label: str, context: QualificationContext) -> CaptureBinding:
    document = _mapping(value, label)
    _exact_keys(document, _CAPTURE_FIELDS, label)
    equality = {
        "plan_sha256": context.plan_sha256,
        "fixture_revision_sha256": context.fixture_revision_sha256,
        "selector_evidence_sha256": context.selector_evidence_sha256,
        "source_commit": context.source_commit,
        "dependency_commit": context.dependency_commit,
        "native_attestation_sha256": context.native_attestation_sha256,
        "device_identity_sha256": context.device_identity_sha256,
    }
    if any(document.get(field) != expected for field, expected in equality.items()):
        raise SelectedStateQualificationError(f"{label} identity differs from its plan context")
    leaves = tuple(
        _sha256(item, f"{label} leaf source SHA-256")
        for item in _sequence(document.get("leaf_source_sha256s"), f"{label} leaf sources")
    )
    if not leaves or len(set(leaves)) != len(leaves):
        raise SelectedStateQualificationError(f"{label} leaf sources must be nonempty and unique")
    raw_path, raw_sha, raw_size = _capture_file(
        document.get("raw_iq_path"),
        document.get("raw_iq_sha256"),
        document.get("raw_iq_size_bytes"),
        f"{label} raw IQ",
    )
    metadata_path, metadata_sha, metadata_size = _capture_file(
        document.get("metadata_path"),
        document.get("metadata_sha256"),
        document.get("metadata_size_bytes"),
        f"{label} metadata",
    )
    record_path, record_sha, record_size = _capture_file(
        document.get("condition_record_path"),
        document.get("condition_record_sha256"),
        document.get("condition_record_size_bytes"),
        f"{label} condition record",
    )
    return CaptureBinding(
        run_id=_identifier(document.get("run_id"), f"{label} run ID"),
        stream_id=_identifier(document.get("stream_id"), f"{label} stream ID"),
        artifact_id=_identifier(document.get("artifact_id"), f"{label} artifact ID"),
        raw_iq_path=raw_path,
        raw_iq_sha256=raw_sha,
        raw_iq_size_bytes=raw_size,
        metadata_path=metadata_path,
        metadata_sha256=metadata_sha,
        metadata_size_bytes=metadata_size,
        condition_record_path=record_path,
        condition_record_sha256=record_sha,
        condition_record_size_bytes=record_size,
        leaf_source_sha256s=leaves,
    )


_QUALITY_FIELDS = {
    "metadata_abi",
    "expected_sample_count",
    "observed_sample_count",
    "raw_sample_count",
    "continuity_verified",
    "missing_sample_count",
    "clipped_sample_count",
    "adc_headroom_db",
    "reference_detected",
    "reference_snr_db",
    "final_mute_verified",
    "final_selector_control_verified",
}


def _quality_reasons(value: object, label: str, *, expected_samples: int) -> list[str]:
    document = _mapping(value, label)
    _exact_keys(document, _QUALITY_FIELDS, label)
    reasons: list[str] = []
    if document.get("metadata_abi") != 2:
        reasons.append("metadata_abi_not_2")
    declared = _integer(
        document.get("expected_sample_count"), f"{label} expected samples", minimum=1
    )
    observed = _integer(document.get("observed_sample_count"), f"{label} observed samples")
    raw = _integer(document.get("raw_sample_count"), f"{label} raw samples")
    if declared != expected_samples or observed != declared or raw != declared:
        reasons.append("sample_count_not_exact")
    if document.get("continuity_verified") is not True:
        reasons.append("abi2_continuity_not_verified")
    if _integer(document.get("missing_sample_count"), f"{label} missing samples") != 0:
        reasons.append("missing_samples_nonzero")
    if _integer(document.get("clipped_sample_count"), f"{label} clipped samples") != 0:
        reasons.append("clipped_samples_nonzero")
    if _finite(document.get("adc_headroom_db"), f"{label} ADC headroom") < MINIMUM_ADC_HEADROOM_DB:
        reasons.append("adc_headroom_below_6db")
    if document.get("reference_detected") is not True:
        reasons.append("reference_not_detected")
    if (
        _finite(document.get("reference_snr_db"), f"{label} reference SNR")
        < MINIMUM_REFERENCE_SNR_DB
    ):
        reasons.append("reference_snr_below_20db")
    if document.get("final_mute_verified") is not True:
        reasons.append("final_mute_not_verified")
    if document.get("final_selector_control_verified") is not True:
        reasons.append("final_selector_control_not_verified")
    return reasons


def _state_transfer(value: object, label: str) -> tuple[complex | None, list[str]]:
    document = _mapping(value, label)
    _exact_keys(
        document,
        {"detected", "h", "magnitude_upper_bound", "coherence", "phase_rms_deg"},
        label,
    )
    reasons: list[str] = []
    if document.get("detected") is not True:
        if document.get("h") is not None:
            raise SelectedStateQualificationError(
                f"{label} nondetection must not invent a complex phasor"
            )
        if document.get("coherence") is not None or document.get("phase_rms_deg") is not None:
            raise SelectedStateQualificationError(
                f"{label} nondetection must not report phase/coherence"
            )
        upper = _finite(document.get("magnitude_upper_bound"), f"{label} magnitude upper bound")
        if upper <= 0.0:
            raise SelectedStateQualificationError(
                f"{label} nondetection requires a positive phase-free magnitude upper bound"
            )
        reasons.append("pilot_not_detected")
        return None, reasons
    if document.get("magnitude_upper_bound") is not None:
        raise SelectedStateQualificationError(
            f"{label} detection must not replace its complex phasor with an upper bound"
        )
    result = _complex(document.get("h"), f"{label}.h")
    if _finite(document.get("coherence"), f"{label} coherence") < MINIMUM_COHERENCE:
        reasons.append("coherence_below_0p995")
    phase_rms = _finite(document.get("phase_rms_deg"), f"{label} phase RMS")
    if phase_rms < 0.0 or phase_rms > MAXIMUM_PHASE_RMS_DEG:
        reasons.append("phase_rms_above_6deg")
    return result, reasons


def _source_sets(captures: Sequence[CaptureBinding], label: str) -> dict[str, tuple[str, ...]]:
    streams = tuple(item.stream_id for item in captures)
    raw = tuple(item.raw_iq_sha256 for item in captures)
    metadata = tuple(item.metadata_sha256 for item in captures)
    leaves = tuple(source for item in captures for source in item.leaf_source_sha256s)
    for values, source_label in (
        (streams, "stream IDs"),
        (raw, "raw IQ sources"),
        (metadata, "metadata sources"),
        (leaves, "leaf sources"),
    ):
        if len(values) != len(set(values)):
            raise SelectedStateQualificationError(f"{label} reuses {source_label}")
    return {
        "stream_ids": streams,
        "raw_iq_sha256s": raw,
        "metadata_sha256s": metadata,
        "leaf_source_sha256s": leaves,
    }


def _base_result(
    kind: str,
    context: QualificationContext,
    accepted: bool,
    reasons: Sequence[str],
    sources: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "result_kind": kind,
        "accepted": bool(accepted),
        "rejection_reasons": sorted(set(reasons)),
        "context": asdict(context),
        "source_stream_ids": list(sources["stream_ids"]),
        "raw_iq_sha256s": list(sources["raw_iq_sha256s"]),
        "metadata_sha256s": list(sources["metadata_sha256s"]),
        "leaf_source_sha256s": list(sources["leaf_source_sha256s"]),
    }


def qualify_static_bench(
    value: Mapping[str, Any],
    *,
    fixture: FullSimultaneousFixture,
    selector: SelectorEvidenceBinding,
) -> dict[str, Any]:
    """Qualify one static capture for ALL_OFF and every selected state."""

    document = _mapping(value, "static bench evidence")
    _exact_keys(
        document,
        {
            "schema",
            "evidence_kind",
            "context",
            "state_order",
            "state_codes",
            "observations",
            "final_mute_verified",
            "final_all_off_readback",
        },
        "static bench evidence",
    )
    if document.get("schema") != 1 or document.get("evidence_kind") != STATIC_KIND:
        raise SelectedStateQualificationError("static bench evidence schema or kind is invalid")
    context = _context(
        document.get("context"), fixture=fixture, selector=selector, expected_role="bench"
    )
    _state_order(document.get("state_order"), "static state order")
    codes = _mapping(document.get("state_codes"), "static state codes")
    _exact_keys(codes, set(EXPECTED_STATES), "static state codes")
    normalized_codes = {
        state: _integer(codes[state], f"{state} GPIO code") for state in EXPECTED_STATES
    }
    if len(set(normalized_codes.values())) != len(normalized_codes):
        raise SelectedStateQualificationError("static state GPIO codes must be unique")

    observations = _sequence(document.get("observations"), "static observations")
    if len(observations) != len(EXPECTED_STATES):
        raise SelectedStateQualificationError("static matrix requires exactly nine observations")
    captures: list[CaptureBinding] = []
    reasons: list[str] = []
    observed_states: list[str] = []
    for index, raw_observation in enumerate(observations):
        label = f"static observations[{index}]"
        observation = _mapping(raw_observation, label)
        _exact_keys(observation, {"state", "capture", "command", "quality", "transfer"}, label)
        state = str(observation.get("state"))
        observed_states.append(state)
        if state not in EXPECTED_STATES:
            raise SelectedStateQualificationError(f"{label} has unknown state {state!r}")
        capture = _capture_binding(observation.get("capture"), f"{label}.capture", context)
        captures.append(capture)
        command = _mapping(observation.get("command"), f"{label}.command")
        _exact_keys(
            command,
            {
                "commanded_state",
                "commanded_code",
                "command_sequence",
                "acknowledged_sequence",
                "applied_state",
                "applied_code",
                "gpio_latch_code",
                "lease_ms",
                "command_valid",
                "readback_passed",
            },
            f"{label}.command",
        )
        sequence = _integer(command.get("command_sequence"), f"{label} command sequence", minimum=1)
        acknowledged = _integer(
            command.get("acknowledged_sequence"), f"{label} acknowledged sequence", minimum=1
        )
        lease_ms = _integer(command.get("lease_ms"), f"{label} lease")
        expected_code = normalized_codes[state]
        if (
            command.get("commanded_state") != state
            or command.get("applied_state") != state
            or command.get("commanded_code") != expected_code
            or command.get("applied_code") != expected_code
            or command.get("gpio_latch_code") != expected_code
            or sequence != acknowledged
            or lease_ms != (0 if state == ALL_OFF else STATIC_SELECTED_STATE_LEASE_MS)
            or command.get("command_valid") is not True
            or command.get("readback_passed") is not True
        ):
            reasons.append(f"{state.lower()}_selected_state_readback_failed")
        reasons.extend(
            f"{state.lower()}_{reason}"
            for reason in _quality_reasons(
                observation.get("quality"), f"{label}.quality", expected_samples=300_000
            )
        )
        _, transfer_reasons = _state_transfer(observation.get("transfer"), f"{label}.transfer")
        reasons.extend(f"{state.lower()}_{reason}" for reason in transfer_reasons)
    if tuple(observed_states) != EXPECTED_STATES:
        raise SelectedStateQualificationError(
            "static observations must contain the exact canonical state order"
        )
    sources = _source_sets(captures, "static matrix")
    if document.get("final_mute_verified") is not True:
        reasons.append("static_final_mute_not_verified")
    final = _mapping(document.get("final_all_off_readback"), "static final ALL_OFF readback")
    _exact_keys(
        final,
        {"state", "mailbox_code", "gpio_latch_code", "passed"},
        "static final ALL_OFF readback",
    )
    if (
        final.get("state") != ALL_OFF
        or final.get("mailbox_code") != normalized_codes[ALL_OFF]
        or final.get("gpio_latch_code") != normalized_codes[ALL_OFF]
        or final.get("passed") is not True
    ):
        reasons.append("static_final_all_off_not_verified")
    result = _base_result(STATIC_RESULT_KIND, context, not reasons, reasons, sources)
    result.update(
        {
            "state_order": list(EXPECTED_STATES),
            "state_codes": normalized_codes,
            "observation_count": len(observations),
            "final_mute_verified": document.get("final_mute_verified") is True,
            "final_all_off_verified": "static_final_all_off_not_verified" not in reasons,
        }
    )
    return result


def _positive_window(value: object, label: str) -> tuple[float, float]:
    raw = _sequence(value, label)
    if len(raw) != 2:
        raise SelectedStateQualificationError(f"{label} must contain [minimum, maximum]")
    minimum = _finite(raw[0], f"{label} minimum")
    maximum = _finite(raw[1], f"{label} maximum")
    if minimum <= 0.0 or maximum < minimum:
        raise SelectedStateQualificationError(f"{label} is not a positive ordered window")
    return minimum, maximum


def qualify_fast20_timing(
    value: Mapping[str, Any],
    *,
    fixture: FullSimultaneousFixture,
    selector: SelectorEvidenceBinding,
) -> dict[str, Any]:
    """Require two independently complete and aligned Fast20 timing streams."""

    document = _mapping(value, "Fast20 timing evidence")
    _exact_keys(
        document,
        {
            "schema",
            "evidence_kind",
            "context",
            "profile",
            "runs",
            "final_mute_verified",
            "final_fast20_schedule_verified",
        },
        "Fast20 timing evidence",
    )
    if document.get("schema") != 1 or document.get("evidence_kind") != TIMING_KIND:
        raise SelectedStateQualificationError("Fast20 timing schema or kind is invalid")
    context = _context(
        document.get("context"), fixture=fixture, selector=selector, expected_role="fast20"
    )
    profile = _mapping(document.get("profile"), "Fast20 timing profile")
    _exact_keys(
        profile,
        {
            "profile_id",
            "profile_contract_sha256",
            "state_order",
            "sample_rate_hz",
            "expected_sample_count",
            "samples_per_frame",
            "frame_count",
            "minimum_complete_cycles",
            "dwell_window_ms_by_state",
        },
        "Fast20 timing profile",
    )
    if profile.get("profile_id") != "fast20-v1":
        raise SelectedStateQualificationError("timing profile must be the reviewed fast20-v1")
    if profile.get("profile_contract_sha256") != selector.profile_contract_sha256:
        raise SelectedStateQualificationError(
            "timing profile hash differs from the exact live Fast20 image"
        )
    _state_order(profile.get("state_order"), "Fast20 timing state order")
    sample_rate = _integer(profile.get("sample_rate_hz"), "timing sample rate", minimum=1)
    if sample_rate != 1_000_000:
        raise SelectedStateQualificationError("selected-state timing requires exactly 1 MS/s")
    expected_samples = _integer(
        profile.get("expected_sample_count"), "timing expected sample count", minimum=1
    )
    samples_per_frame = _integer(
        profile.get("samples_per_frame"), "timing samples per frame", minimum=1
    )
    frame_count = _integer(profile.get("frame_count"), "timing frame count", minimum=1)
    if expected_samples != samples_per_frame * frame_count:
        raise SelectedStateQualificationError("timing frame shape does not equal sample count")
    minimum_cycles = _integer(
        profile.get("minimum_complete_cycles"), "minimum complete cycles", minimum=5
    )
    dwell_windows = _mapping(profile.get("dwell_window_ms_by_state"), "dwell windows")
    _exact_keys(dwell_windows, set(ANTENNA_STATES), "dwell windows")
    normalized_dwell_windows = {
        state: _positive_window(dwell_windows[state], f"{state} dwell window")
        for state in ANTENNA_STATES
    }

    runs = _sequence(document.get("runs"), "Fast20 timing runs")
    if len(runs) != TIMING_RUN_COUNT:
        raise SelectedStateQualificationError("Fast20 timing requires exactly two runs")
    captures: list[CaptureBinding] = []
    reasons: list[str] = []
    per_run: list[dict[str, Any]] = []
    for index, raw_run in enumerate(runs):
        label = f"Fast20 timing runs[{index}]"
        run = _mapping(raw_run, label)
        _exact_keys(run, {"capture", "quality", "timing"}, label)
        capture = _capture_binding(run.get("capture"), f"{label}.capture", context)
        captures.append(capture)
        run_reasons = _quality_reasons(
            run.get("quality"), f"{label}.quality", expected_samples=expected_samples
        )
        timing = _mapping(run.get("timing"), f"{label}.timing")
        _exact_keys(
            timing,
            {
                "state_order",
                "isolation_verified",
                "continuity_verified",
                "complete_cycle_count",
                "rejected_marker_count",
                "threshold_stable",
                "dwell_by_state",
            },
            f"{label}.timing",
        )
        _state_order(timing.get("state_order"), f"{label} state order")
        complete_cycles = _integer(
            timing.get("complete_cycle_count"), f"{label} complete cycle count"
        )
        if (
            timing.get("isolation_verified") is not True
            or timing.get("continuity_verified") is not True
            or complete_cycles < minimum_cycles
            or _integer(timing.get("rejected_marker_count"), f"{label} rejected markers") != 0
            or timing.get("threshold_stable") is not True
        ):
            run_reasons.append("fast20_schedule_timing_not_verified")
        observed_dwells = _mapping(timing.get("dwell_by_state"), f"{label} dwell evidence")
        _exact_keys(observed_dwells, set(ANTENNA_STATES), f"{label} dwell evidence")
        for state in ANTENNA_STATES:
            dwell = _mapping(observed_dwells[state], f"{label} {state} dwell")
            _exact_keys(
                dwell,
                {"observed_count", "duration_min_ms", "duration_max_ms"},
                f"{label} {state} dwell",
            )
            count = _integer(dwell.get("observed_count"), f"{label} {state} dwell count")
            minimum = _finite(dwell.get("duration_min_ms"), f"{label} {state} dwell minimum")
            maximum = _finite(dwell.get("duration_max_ms"), f"{label} {state} dwell maximum")
            window = normalized_dwell_windows[state]
            if (
                count != complete_cycles
                or minimum < window[0]
                or maximum > window[1]
                or maximum < minimum
            ):
                run_reasons.append(f"{state.lower()}_dwell_duration_or_count_failed")
        reasons.extend(f"run{index + 1}_{reason}" for reason in run_reasons)
        per_run.append(
            {
                "run_id": capture.run_id,
                "stream_id": capture.stream_id,
                "accepted": not run_reasons,
                "rejection_reasons": sorted(set(run_reasons)),
            }
        )
    sources = _source_sets(captures, "Fast20 timing")
    if len({capture.run_id for capture in captures}) != TIMING_RUN_COUNT:
        raise SelectedStateQualificationError("Fast20 timing requires two distinct run IDs")
    if document.get("final_mute_verified") is not True:
        reasons.append("timing_final_mute_not_verified")
    final = _mapping(document.get("final_fast20_schedule_verified"), "timing final Fast20 schedule")
    _exact_keys(
        final,
        {"image_role", "profile_contract_sha256", "passed"},
        "timing final Fast20 schedule",
    )
    if (
        final.get("image_role") != "fast20"
        or final.get("profile_contract_sha256") != selector.profile_contract_sha256
        or final.get("passed") is not True
    ):
        reasons.append("timing_final_fast20_schedule_not_verified")
    result = _base_result(TIMING_RESULT_KIND, context, not reasons, reasons, sources)
    result.update(
        {
            "timing_run_count": len(runs),
            "runs": per_run,
            "profile_contract_sha256": selector.profile_contract_sha256,
            "expected_sample_count_per_run": expected_samples,
            "final_mute_verified": document.get("final_mute_verified") is True,
            "final_fast20_schedule_verified": (
                "timing_final_fast20_schedule_not_verified" not in reasons
            ),
        }
    )
    return result


def _wrap_phase_deg(values: np.ndarray) -> np.ndarray:
    return (values + 180.0) % 360.0 - 180.0


def _bootstrap_matrix_metrics(
    values: Mapping[str, Sequence[complex]], *, draws: int, seed: int
) -> dict[str, Any]:
    if draws < 2_000:
        raise SelectedStateQualificationError("bootstrap requires at least 2,000 draws")
    rng = np.random.default_rng(seed)
    arrays = {
        state: np.asarray(tuple(state_values), dtype=np.complex128)
        for state, state_values in values.items()
    }
    if any(array.shape != (MATRIX_REPEAT_COUNT,) for array in arrays.values()):
        raise SelectedStateQualificationError("matrix bootstrap requires five values per state")
    point_means = {state: complex(np.mean(array)) for state, array in arrays.items()}
    # One matrix repeat contains every state.  Resample whole streams so selected/off
    # covariance and cross-state acquisition drift are retained rather than fabricated away.
    indices = rng.integers(0, MATRIX_REPEAT_COUNT, size=(draws, MATRIX_REPEAT_COUNT))
    boot_means = {state: np.mean(array[indices], axis=1) for state, array in arrays.items()}

    contrast_alpha = 0.05 / (2 * len(ANTENNA_STATES))
    repeatability_tail = 0.05 / (2 * 2 * len(ANTENNA_STATES))
    off_boot = boot_means[ALL_OFF]
    state_results: dict[str, Any] = {}
    for state in ANTENNA_STATES:
        selected = point_means[state]
        off = point_means[ALL_OFF]
        path = selected - off
        raw_point = 20.0 * math.log10(abs(selected) / abs(off))
        path_point = 20.0 * math.log10(abs(path) / abs(off))
        raw_draws = 20.0 * np.log10(
            np.maximum(np.abs(boot_means[state]), _EPSILON) / np.maximum(np.abs(off_boot), _EPSILON)
        )
        path_draws = 20.0 * np.log10(
            np.maximum(np.abs(boot_means[state] - off_boot), _EPSILON)
            / np.maximum(np.abs(off_boot), _EPSILON)
        )
        amplitude_delta = 20.0 * np.log10(
            np.maximum(np.abs(boot_means[state]), _EPSILON) / max(abs(selected), _EPSILON)
        )
        phase_delta = _wrap_phase_deg(
            np.degrees(np.angle(boot_means[state] * np.conjugate(selected)))
        )
        amp_bounds = np.quantile(amplitude_delta, [repeatability_tail, 1.0 - repeatability_tail])
        phase_bounds = np.quantile(phase_delta, [repeatability_tail, 1.0 - repeatability_tail])
        state_results[state] = {
            "h_selected": _complex_document(selected),
            "h_off": _complex_document(off),
            "h_path": _complex_document(path),
            "c_raw_db": raw_point,
            "c_raw_simultaneous_lower_95_db": float(np.quantile(raw_draws, contrast_alpha)),
            "c_path_db": path_point,
            "c_path_simultaneous_lower_95_db": float(np.quantile(path_draws, contrast_alpha)),
            "repeatability_amplitude_simultaneous_95_db": [
                float(amp_bounds[0]),
                float(amp_bounds[1]),
            ],
            "repeatability_amplitude_half_width_db": float(np.max(np.abs(amp_bounds))),
            "repeatability_phase_simultaneous_95_deg": [
                float(phase_bounds[0]),
                float(phase_bounds[1]),
            ],
            "repeatability_phase_half_width_deg": float(np.max(np.abs(phase_bounds))),
        }
    return {
        "method": (
            "paired full-stream nonparametric complex bootstrap; Bonferroni family-wise 95% "
            "one-sided contrast bounds across C_raw/C_path x ANT1..ANT8 and two-sided "
            "repeatability bounds across amplitude/phase x ANT1..ANT8"
        ),
        "draws": draws,
        "seed": seed,
        "states": state_results,
    }


def qualify_fast20_matrix(
    value: Mapping[str, Any],
    *,
    fixture: FullSimultaneousFixture,
    selector: SelectorEvidenceBinding,
    forbidden_stream_ids: Sequence[str] = (),
    forbidden_raw_iq_sha256s: Sequence[str] = (),
    bootstrap_draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    bootstrap_seed: int = 0x5F8,
) -> dict[str, Any]:
    """Qualify five fresh full-state Fast20 streams and simultaneous contrast gates."""

    document = _mapping(value, "Fast20 matrix evidence")
    _exact_keys(
        document,
        {
            "schema",
            "evidence_kind",
            "context",
            "state_order",
            "repeat_count",
            "streams",
            "final_mute_verified",
            "final_fast20_schedule_verified",
        },
        "Fast20 matrix evidence",
    )
    if document.get("schema") != 1 or document.get("evidence_kind") != MATRIX_KIND:
        raise SelectedStateQualificationError("Fast20 matrix schema or kind is invalid")
    context = _context(
        document.get("context"), fixture=fixture, selector=selector, expected_role="fast20"
    )
    _state_order(document.get("state_order"), "Fast20 matrix state order")
    if document.get("repeat_count") != MATRIX_REPEAT_COUNT:
        raise SelectedStateQualificationError("Fast20 matrix requires exactly five repeats")
    streams = _sequence(document.get("streams"), "Fast20 matrix streams")
    if len(streams) != MATRIX_REPEAT_COUNT:
        raise SelectedStateQualificationError("Fast20 matrix requires exactly five streams")
    captures: list[CaptureBinding] = []
    reasons: list[str] = []
    state_values: dict[str, list[complex]] = {state: [] for state in EXPECTED_STATES}
    nondetection_bounds: dict[str, list[float]] = {state: [] for state in EXPECTED_STATES}
    per_stream: list[dict[str, Any]] = []
    for index, raw_stream in enumerate(streams):
        label = f"Fast20 matrix streams[{index}]"
        stream = _mapping(raw_stream, label)
        _exact_keys(stream, {"repeat_index", "capture", "quality", "state_order", "states"}, label)
        if stream.get("repeat_index") != index + 1:
            raise SelectedStateQualificationError("Fast20 matrix repeat indices must be 1..5")
        _state_order(stream.get("state_order"), f"{label} state order")
        capture = _capture_binding(stream.get("capture"), f"{label}.capture", context)
        captures.append(capture)
        stream_reasons = _quality_reasons(
            stream.get("quality"), f"{label}.quality", expected_samples=10_000_000
        )
        states = _mapping(stream.get("states"), f"{label}.states")
        _exact_keys(states, set(EXPECTED_STATES), f"{label}.states")
        for state in EXPECTED_STATES:
            transfer, transfer_reasons = _state_transfer(states[state], f"{label}.states.{state}")
            if transfer is None:
                state_document = _mapping(states[state], f"{label}.states.{state}")
                nondetection_bounds[state].append(
                    _finite(
                        state_document.get("magnitude_upper_bound"),
                        f"{label}.states.{state} magnitude upper bound",
                    )
                )
            else:
                state_values[state].append(transfer)
            stream_reasons.extend(f"{state.lower()}_{reason}" for reason in transfer_reasons)
        reasons.extend(f"repeat{index + 1}_{reason}" for reason in stream_reasons)
        per_stream.append(
            {
                "repeat_index": index + 1,
                "run_id": capture.run_id,
                "stream_id": capture.stream_id,
                "accepted": not stream_reasons,
                "rejection_reasons": sorted(set(stream_reasons)),
            }
        )
    sources = _source_sets(captures, "Fast20 matrix")
    if set(sources["stream_ids"]) & set(forbidden_stream_ids):
        raise SelectedStateQualificationError("Fast20 matrix reuses a prior stream ID")
    if set(sources["raw_iq_sha256s"]) & set(forbidden_raw_iq_sha256s):
        raise SelectedStateQualificationError("Fast20 matrix reuses prior raw IQ")
    if len({capture.run_id for capture in captures}) != MATRIX_REPEAT_COUNT:
        raise SelectedStateQualificationError("Fast20 matrix requires five distinct run IDs")
    if document.get("final_mute_verified") is not True:
        reasons.append("matrix_final_mute_not_verified")
    final = _mapping(document.get("final_fast20_schedule_verified"), "matrix final Fast20 schedule")
    _exact_keys(
        final,
        {"image_role", "profile_contract_sha256", "passed"},
        "matrix final Fast20 schedule",
    )
    if (
        final.get("image_role") != "fast20"
        or final.get("profile_contract_sha256") != selector.profile_contract_sha256
        or final.get("passed") is not True
    ):
        reasons.append("matrix_final_fast20_schedule_not_verified")

    if any(nondetection_bounds[state] for state in EXPECTED_STATES):
        reasons.append("matrix_contains_phase_free_nondetection")
        result = _base_result(MATRIX_RESULT_KIND, context, False, reasons, sources)
        result.update(
            {
                "state_order": list(EXPECTED_STATES),
                "repeat_count": MATRIX_REPEAT_COUNT,
                "streams": per_stream,
                "bootstrap": None,
                "nondetections": {
                    state: {
                        "count": len(nondetection_bounds[state]),
                        "phase_free_magnitude_upper_bounds": nondetection_bounds[state],
                    }
                    for state in EXPECTED_STATES
                    if nondetection_bounds[state]
                },
                "simultaneous_gates": {
                    "minimum_c_raw_lower_95_db": None,
                    "minimum_c_path_lower_95_db": None,
                    "maximum_amplitude_repeatability_half_width_db": None,
                    "maximum_phase_repeatability_half_width_deg": None,
                    "c_raw_at_least_20db": False,
                    "c_path_at_least_20db": False,
                    "one_degree_c_path_at_least_35p1629db": False,
                    "amplitude_repeatability_at_most_0p2db": False,
                    "phase_repeatability_at_most_2deg": False,
                },
                "operational_matrix_accepted": False,
                "one_degree_matrix_accepted": False,
                "final_mute_verified": document.get("final_mute_verified") is True,
                "final_fast20_schedule_verified": (
                    "matrix_final_fast20_schedule_not_verified" not in reasons
                ),
            }
        )
        return result

    bootstrap = _bootstrap_matrix_metrics(state_values, draws=bootstrap_draws, seed=bootstrap_seed)
    state_results = _mapping(bootstrap["states"], "matrix bootstrap states")
    raw_lowers = [
        float(_mapping(state_results[state], state)["c_raw_simultaneous_lower_95_db"])
        for state in ANTENNA_STATES
    ]
    path_lowers = [
        float(_mapping(state_results[state], state)["c_path_simultaneous_lower_95_db"])
        for state in ANTENNA_STATES
    ]
    amplitude_widths = [
        float(_mapping(state_results[state], state)["repeatability_amplitude_half_width_db"])
        for state in ANTENNA_STATES
    ]
    phase_widths = [
        float(_mapping(state_results[state], state)["repeatability_phase_half_width_deg"])
        for state in ANTENNA_STATES
    ]
    minimum_raw = min(raw_lowers)
    minimum_path = min(path_lowers)
    maximum_amplitude_width = max(amplitude_widths)
    maximum_phase_width = max(phase_widths)
    if maximum_amplitude_width > MAXIMUM_REPEATABILITY_DB:
        reasons.append("simultaneous_amplitude_repeatability_above_0p2db")
    if maximum_phase_width > MAXIMUM_REPEATABILITY_PHASE_DEG:
        reasons.append("simultaneous_phase_repeatability_above_2deg")
    if minimum_raw < OPERATIONAL_RAW_CONTRAST_DB:
        reasons.append("simultaneous_c_raw_lower_bound_below_20db")
    if minimum_path < OPERATIONAL_PATH_CONTRAST_DB:
        reasons.append("simultaneous_c_path_lower_bound_below_20db")
    matrix_accepted = not reasons
    result = _base_result(MATRIX_RESULT_KIND, context, matrix_accepted, reasons, sources)
    result.update(
        {
            "state_order": list(EXPECTED_STATES),
            "repeat_count": MATRIX_REPEAT_COUNT,
            "streams": per_stream,
            "bootstrap": bootstrap,
            "simultaneous_gates": {
                "minimum_c_raw_lower_95_db": minimum_raw,
                "minimum_c_path_lower_95_db": minimum_path,
                "maximum_amplitude_repeatability_half_width_db": maximum_amplitude_width,
                "maximum_phase_repeatability_half_width_deg": maximum_phase_width,
                "c_raw_at_least_20db": minimum_raw >= OPERATIONAL_RAW_CONTRAST_DB,
                "c_path_at_least_20db": minimum_path >= OPERATIONAL_PATH_CONTRAST_DB,
                "one_degree_c_path_at_least_35p1629db": (
                    minimum_path >= ONE_DEGREE_PATH_CONTRAST_DB
                ),
                "amplitude_repeatability_at_most_0p2db": (
                    maximum_amplitude_width <= MAXIMUM_REPEATABILITY_DB
                ),
                "phase_repeatability_at_most_2deg": (
                    maximum_phase_width <= MAXIMUM_REPEATABILITY_PHASE_DEG
                ),
            },
            "operational_matrix_accepted": matrix_accepted,
            "one_degree_matrix_accepted": (
                matrix_accepted and minimum_path >= ONE_DEGREE_PATH_CONTRAST_DB
            ),
            "final_mute_verified": document.get("final_mute_verified") is True,
            "final_fast20_schedule_verified": (
                "matrix_final_fast20_schedule_not_verified" not in reasons
            ),
        }
    )
    return result


def _result_context(value: Mapping[str, Any], label: str) -> QualificationContext:
    document = _mapping(value.get("context"), f"{label} context")
    _exact_keys(document, _CONTEXT_FIELDS, f"{label} context")
    role = document.get("selector_image_role")
    if role not in IMAGE_ROLES:
        raise SelectedStateQualificationError(f"{label} has invalid selector role")
    identity = _device_identity_snapshot(
        document.get("device_identity_snapshot"), f"{label} device identity snapshot"
    )
    context = QualificationContext(
        campaign_id=_identifier(document.get("campaign_id"), f"{label} campaign ID"),
        board_id=_identifier(document.get("board_id"), f"{label} board ID"),
        fixture_revision_sha256=_sha256(
            document.get("fixture_revision_sha256"), f"{label} fixture revision"
        ),
        selector_evidence_sha256=_sha256(
            document.get("selector_evidence_sha256"), f"{label} selector evidence"
        ),
        selector_image_role=role,
        source_commit=_commit(document.get("source_commit"), f"{label} source commit"),
        dependency_commit=_commit(document.get("dependency_commit"), f"{label} dependency commit"),
        native_attestation_sha256=_sha256(
            document.get("native_attestation_sha256"), f"{label} native attestation"
        ),
        device_identity_sha256=_sha256(
            document.get("device_identity_sha256"), f"{label} device identity"
        ),
        device_identity_snapshot=identity,
        plan_sha256=_sha256(document.get("plan_sha256"), f"{label} plan SHA-256"),
    )
    if identity.native_attestation_sha256 != context.native_attestation_sha256:
        raise SelectedStateQualificationError(
            f"{label} device snapshot differs from its native runtime identity"
        )
    return context


def _result_sources(value: Mapping[str, Any], label: str) -> tuple[set[str], set[str]]:
    streams = {
        _identifier(item, f"{label} stream ID")
        for item in _sequence(value.get("source_stream_ids"), f"{label} stream IDs")
    }
    raw = {
        _sha256(item, f"{label} raw IQ SHA-256")
        for item in _sequence(value.get("raw_iq_sha256s"), f"{label} raw IQ hashes")
    }
    if not streams or not raw:
        raise SelectedStateQualificationError(f"{label} source identity is empty")
    return streams, raw


def qualify_selected_state_release(
    *,
    intervention: InterventionContract,
    static_result: Mapping[str, Any],
    timing_result: Mapping[str, Any],
    matrix_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine the four independently admitted prerequisites into release evidence."""

    expected_kinds = (
        (static_result, STATIC_RESULT_KIND, "static"),
        (timing_result, TIMING_RESULT_KIND, "timing"),
        (matrix_result, MATRIX_RESULT_KIND, "matrix"),
    )
    contexts: dict[str, QualificationContext] = {}
    sources: dict[str, tuple[set[str], set[str]]] = {}
    reasons: list[str] = []
    for result, kind, label in expected_kinds:
        if result.get("schema") != 1 or result.get("result_kind") != kind:
            raise SelectedStateQualificationError(f"{label} result kind is invalid")
        contexts[label] = _result_context(result, label)
        sources[label] = _result_sources(result, label)
        if result.get("accepted") is not True:
            reasons.append(f"{label}_qualification_not_accepted")
    static_context = contexts["static"]
    timing_context = contexts["timing"]
    matrix_context = contexts["matrix"]
    if static_context.selector_image_role != "bench":
        raise SelectedStateQualificationError("static result is not bound to bench image evidence")
    if (
        timing_context.selector_image_role != "fast20"
        or matrix_context.selector_image_role != "fast20"
    ):
        raise SelectedStateQualificationError("timing/matrix results require Fast20 evidence")
    common_fields = (
        "campaign_id",
        "board_id",
        "fixture_revision_sha256",
        "source_commit",
        "dependency_commit",
        "native_attestation_sha256",
    )
    if any(
        getattr(static_context, field) != getattr(timing_context, field)
        or getattr(static_context, field) != getattr(matrix_context, field)
        for field in common_fields
    ):
        raise SelectedStateQualificationError(
            "static, timing, and matrix results do not share one exact revision/runtime identity"
        )
    if (
        static_context.device_identity_snapshot.stable_key()
        != timing_context.device_identity_snapshot.stable_key()
        or static_context.device_identity_snapshot.stable_key()
        != matrix_context.device_identity_snapshot.stable_key()
    ):
        raise SelectedStateQualificationError(
            "static, timing, and matrix results do not share one exact stable device identity"
        )
    if timing_context.selector_evidence_sha256 != matrix_context.selector_evidence_sha256:
        raise SelectedStateQualificationError(
            "timing and matrix do not bind the same exact live Fast20 image evidence"
        )
    if intervention.selector_evidence_sha256 != static_context.selector_evidence_sha256:
        raise SelectedStateQualificationError(
            "X intervention and static qualification do not bind the same exact bench "
            "selector evidence"
        )
    timing_runs = timing_result.get("runs")
    if (
        timing_result.get("timing_run_count") != TIMING_RUN_COUNT
        or not isinstance(timing_runs, list)
        or len(timing_runs) != TIMING_RUN_COUNT
        or len(sources["timing"][0]) != TIMING_RUN_COUNT
        or any(
            not isinstance(item, Mapping) or item.get("accepted") is not True
            for item in timing_runs
        )
    ):
        raise SelectedStateQualificationError(
            "matrix release requires one timing parent result with exactly two accepted captures"
        )
    if (
        intervention.campaign_id != matrix_context.campaign_id
        or intervention.board_id != matrix_context.board_id
        or intervention.installed_after_fixture_revision_sha256
        != matrix_context.fixture_revision_sha256
        or intervention.source_commit != matrix_context.source_commit
        or intervention.dependency_commit != matrix_context.dependency_commit
    ):
        raise SelectedStateQualificationError(
            "supported intervention differs from selected-state qualification identity"
        )
    labels = ("static", "timing", "matrix")
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            if sources[left][0] & sources[right][0] or sources[left][1] & sources[right][1]:
                raise SelectedStateQualificationError(
                    f"{left} and {right} qualifications reuse capture sources"
                )
    intervention_streams = set(
        (*intervention.baseline_stream_ids, *intervention.intervention_stream_ids)
    )
    intervention_raw = set(
        (*intervention.baseline_raw_iq_sha256s, *intervention.intervention_raw_iq_sha256s)
    )
    for label in labels:
        if sources[label][0] & intervention_streams or sources[label][1] & intervention_raw:
            raise SelectedStateQualificationError(
                f"{label} qualification reuses an intervention-comparison capture source"
            )
    matrix_gates = _mapping(matrix_result.get("simultaneous_gates"), "matrix gates")
    operational = (
        not reasons
        and matrix_gates.get("c_raw_at_least_20db") is True
        and matrix_gates.get("c_path_at_least_20db") is True
        and matrix_gates.get("amplitude_repeatability_at_most_0p2db") is True
        and matrix_gates.get("phase_repeatability_at_most_2deg") is True
    )
    one_degree = operational and matrix_gates.get("one_degree_c_path_at_least_35p1629db") is True
    return {
        "schema": 1,
        "result_kind": RELEASE_RESULT_KIND,
        "campaign_id": matrix_context.campaign_id,
        "board_id": matrix_context.board_id,
        "fixture_revision_sha256": matrix_context.fixture_revision_sha256,
        "intervention": asdict(intervention),
        "bench_selector_evidence_sha256": static_context.selector_evidence_sha256,
        "fast20_selector_evidence_sha256": timing_context.selector_evidence_sha256,
        "prerequisite_plan_sha256s": {
            "static": static_context.plan_sha256,
            "timing": timing_context.plan_sha256,
            "matrix": matrix_context.plan_sha256,
        },
        "source_disjointness_verified": True,
        "operational_coefficient_release_allowed": operational,
        "one_degree_coefficient_release_allowed": one_degree,
        "rejection_reasons": sorted(set(reasons)),
        "limitations": {
            "frequency_hz": 5_800_000_000,
            "experimental_operation_above_qualified_ad9363_range": True,
            "release_applies_only_to_bound_fixture_revision_and_exact_fast20_image": True,
        },
    }


__all__ = [
    "ALL_OFF",
    "ANTENNA_STATES",
    "DEFAULT_BOOTSTRAP_DRAWS",
    "DEVICE_IDENTITY_KIND",
    "EXPECTED_STATES",
    "FIXTURE_BINDING_KIND",
    "FIXTURE_KIND_V2",
    "FULL_CONDUCTED_STAGE",
    "FULL_SIMULTANEOUS_TOPOLOGY",
    "INTERVENTION_KIND",
    "INTERVENTION_PLAN_KIND",
    "MATRIX_KIND",
    "MATRIX_REPEAT_COUNT",
    "ONE_DEGREE_PATH_CONTRAST_DB",
    "STATIC_KIND",
    "TIMING_KIND",
    "X_RUN_BINDING_KIND",
    "FullSimultaneousFixture",
    "DeviceIdentityEvidence",
    "DeviceIdentitySnapshot",
    "InterventionChangePlan",
    "InterventionContract",
    "XRunBinding",
    "QualificationContext",
    "SelectedStateQualificationError",
    "SelectorEvidenceBinding",
    "canonical_sha256",
    "fixture_revision_sha256",
    "full_simultaneous_fixture_binding_from_manifest",
    "qualify_fast20_matrix",
    "qualify_fast20_timing",
    "qualify_selected_state_release",
    "qualify_static_bench",
    "selector_binding_from_sealed",
    "sha256_path",
    "validate_full_simultaneous_fixture",
    "validate_device_identity_evidence",
    "device_identity_snapshot_from_evidence",
    "validate_intervention_change_plan",
    "reject_replace_placeholders",
    "validate_intervention_contract",
    "validate_selector_binding_snapshot",
]
