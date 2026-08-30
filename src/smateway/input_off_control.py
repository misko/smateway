"""Fail-closed contracts for the 5.8 GHz Fast20 input-drive-off control.

This module is deliberately free of radio and filesystem side effects.  It
defines the exact P2 fixture graph, normalizes one-run observations, and makes
the independent P0/P2 bootstrap decision.  Hardware orchestration lives in
``scripts/run_5g8_input_off_control.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

CAMPAIGN_ID = "5p8-debug-r1"
FIXTURE_KIND = "5g8_input_drive_off_fixture_v2"
SETUP_KIND = "5g8_input_drive_off_run_setup_v1"
OBSERVATION_KIND = "5g8_fast20_all_off_transfer_observation_v1"
TOPOLOGY_STAGE = "input_drive_off"
TOPOLOGY_TOKEN = "TX1_STIMULUS_AND_8WAY_INPUT_SEPARATELY_TERMINATED"

CENTER_FREQUENCY_HZ = 5_800_000_000
TONE_OFFSET_HZ = 100_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 100
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
DURATION_S = 10.0
KERNEL_BUFFERS = 8
RECEIVER_GAIN_DB = 40.0
TX_HARDWARE_GAIN_DB = -20.0
DDS_SCALE = 0.25
TX_CHANNEL = 0
EDGE_EXCLUSION_BINS = 2
MINIMUM_COMPLETE_FAST20_FRAMES = 20
COHORT_SIZE = 5
DEFAULT_BOOTSTRAP_REPLICATES = 32_768
DEFAULT_BOOTSTRAP_SEED = 0x5A8_2026
COLLAPSE_UPPER_RATIO = 0.31623
NOT_COLLAPSED_LOWER_RATIO = 0.70795
REFERENCE_STABILITY_DB = 1.0
MINIMUM_PILOT_SNR_DB = 20.0
MAXIMUM_P0_AMPLITUDE_CV = 0.10
MAXIMUM_P0_PHASE_STD_DEG = 10.0

SHA256 = frozenset("0123456789abcdef")
PLACEHOLDER = re.compile(r"REPLACE_[A-Za-z0-9_]+")
COMPONENT_ROLES = (
    "pluto",
    "two_way_splitter",
    "rx1_attenuator",
    "tx1_stimulus_termination",
    "eight_way_input_termination",
    "tx2_termination",
    "eight_way_splitter",
    "selector",
)
FIXED_CONNECTION_ROLES = (
    "tx1_to_two_way",
    "two_way_reference_to_rx1_attenuator",
    "rx1_attenuator_to_rx1",
    "tx2_to_termination",
    *(f"eight_way_f{index}_to_selector_ant{index}" for index in range(1, 9)),
    "selector_common_to_rx2",
)
P2_CONNECTION_ROLES = (
    *FIXED_CONNECTION_ROLES,
    "two_way_stimulus_to_termination",
    "eight_way_input_to_termination",
)
REFERENCE_PLANE_ROLES = (
    "tx1",
    "two_way_reference_output",
    "two_way_stimulus_output",
    "rx1_protected_input",
    "tx1_stimulus_termination_load",
    "eight_way_input",
    "eight_way_input_termination_load",
    "selector_common",
    "rx2",
)


class InputOffContractError(ValueError):
    """Evidence cannot support the P2 input-drive-off claim."""


@dataclass(frozen=True, slots=True)
class InputOffObservation:
    """One admitted, source-distinct Fast20 ``ALL_OFF`` observation."""

    cohort: Literal["P0", "P2"]
    run_id: str
    artifact_id: str
    stream_id: int
    artifact_sha256: str
    profile_contract_sha256: str
    transfer_detected: bool
    all_off_transfer: complex | None
    all_off_transfer_upper_bound: float | None
    rx1_reference_amplitude: float
    detected_pilot_snr_db: float
    source_commit: str
    source_files_sha256: str | None
    native_attestation_sha256: str | None
    fixture_evidence_sha256: str | None
    fixture_fixed_graph_sha256: str | None
    comparable_fixture_group_id: str | None


def canonical_json(document: object) -> str:
    """Return the one canonical JSON representation used for evidence hashes."""

    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(document: object) -> str:
    """Hash a JSON-compatible evidence object deterministically."""

    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _normalized_mapping(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(canonical_json(document))
    if not isinstance(normalized, dict):  # pragma: no cover - input is already a mapping
        raise AssertionError("canonical mapping normalization did not return an object")
    return normalized


def _assert_no_placeholders(value: object, *, location: str = "fixture") -> None:
    """Reject unresolved operator-template values before schema normalization."""

    if isinstance(value, str):
        match = PLACEHOLDER.search(value)
        if match is not None:
            raise InputOffContractError(
                f"{location} contains unresolved placeholder {match.group(0)}"
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_placeholders(item, location=f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_placeholders(item, location=f"{location}[{index}]")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputOffContractError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    observed = set(value)
    wanted = set(expected)
    if observed != wanted:
        missing = ", ".join(sorted(wanted - observed)) or "none"
        unexpected = ", ".join(sorted(observed - wanted)) or "none"
        raise InputOffContractError(
            f"{label} fields differ (missing: {missing}; unexpected: {unexpected})"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        raise InputOffContractError(f"{label} is missing or too long")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    ):
        raise InputOffContractError(f"{label} contains unsafe characters")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in SHA256 for c in value):
        raise InputOffContractError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputOffContractError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise InputOffContractError(f"{label} must be finite{' and positive' if positive else ''}")
    return result


def _timezone_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InputOffContractError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InputOffContractError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise InputOffContractError(f"{label} must include an explicit UTC offset")
    return value


def _file_evidence(value: object, label: str) -> dict[str, Any]:
    evidence = _mapping(value, label)
    _exact_keys(evidence, ("path", "sha256", "size_bytes"), label)
    path = evidence["path"]
    size = evidence["size_bytes"]
    if not isinstance(path, str) or not path.startswith("/"):
        raise InputOffContractError(f"{label}.path must be absolute")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise InputOffContractError(f"{label}.size_bytes must be positive")
    return {"path": path, "sha256": _sha256(evidence["sha256"], label), "size_bytes": size}


def _characterization(value: object, label: str) -> dict[str, Any]:
    document = _mapping(value, label)
    _exact_keys(
        document,
        (
            "status",
            "evidence_path",
            "evidence_sha256",
            "s_parameter_sha256",
            "return_loss_db_at_5g8",
        ),
        label,
    )
    status = document["status"]
    if status not in {"characterized", "uncharacterized"}:
        raise InputOffContractError(f"{label}.status must be characterized or uncharacterized")
    if status == "uncharacterized":
        if any(document[key] is not None for key in document if key != "status"):
            raise InputOffContractError(f"{label} uncharacterized evidence fields must be null")
    else:
        evidence_path = document["evidence_path"]
        if (
            not isinstance(evidence_path, str)
            or not evidence_path.startswith("/")
            or evidence_path.endswith("/")
        ):
            raise InputOffContractError(
                f"{label} characterized evidence path must be an absolute file"
            )
        _sha256(document["evidence_sha256"], f"{label}.evidence_sha256")
        _sha256(document["s_parameter_sha256"], f"{label}.s_parameter_sha256")
        _finite(document["return_loss_db_at_5g8"], f"{label}.return_loss_db_at_5g8")
    return _normalized_mapping(document)


def _component(value: object, role: str, *, serial: str) -> dict[str, Any]:
    component = _mapping(value, f"components.{role}")
    common = (
        "id",
        "kind",
        "manufacturer",
        "model",
        "ports",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "characterization",
    )
    extra: tuple[str, ...] = ()
    if role == "pluto":
        extra = ("serial",)
    elif role.endswith("termination"):
        extra = ("impedance_ohm",)
    elif role in {"rx1_attenuator", "rx2_attenuator"}:
        extra = ("attenuation_db", "orientation")
    elif role == "selector":
        extra = ("board_id", "hardware_revision")
    _exact_keys(component, (*common, *extra), f"components.{role}")
    normalized = _normalized_mapping(component)
    normalized["id"] = _identifier(component["id"], f"components.{role}.id")
    if not isinstance(component["kind"], str) or not component["kind"]:
        raise InputOffContractError(f"components.{role}.kind is missing")
    for text_field in ("manufacturer", "model"):
        if not isinstance(component[text_field], str) or not component[text_field]:
            raise InputOffContractError(f"components.{role}.{text_field} is missing")
    ports = _mapping(component["ports"], f"components.{role}.ports")
    if not ports or any(not isinstance(port, str) or not port for port in ports.values()):
        raise InputOffContractError(f"components.{role}.ports must name every physical port")
    minimum = _finite(component["rated_min_frequency_hz"], f"components.{role}.rated_min")
    maximum = _finite(component["rated_max_frequency_hz"], f"components.{role}.rated_max")
    if minimum > CENTER_FREQUENCY_HZ or maximum < CENTER_FREQUENCY_HZ:
        raise InputOffContractError(f"components.{role} is not rated at 5.8 GHz")
    maximum_input_dbm = _finite(
        component["maximum_input_power_dbm"], f"components.{role}.maximum_input_power_dbm"
    )
    _characterization(component["characterization"], f"components.{role}.characterization")
    if role == "pluto" and component["serial"] != serial:
        raise InputOffContractError("fixture Pluto serial differs from the planned radio")
    if role.endswith("termination") and not math.isclose(
        _finite(component["impedance_ohm"], f"components.{role}.impedance_ohm"),
        50.0,
        abs_tol=0.1,
    ):
        raise InputOffContractError(f"components.{role} is not a 50-ohm termination")
    if role.endswith("termination") and maximum_input_dbm < 0.0:
        raise InputOffContractError(
            f"components.{role} is not rated for the frozen 0 dBm load limit"
        )
    if role == "rx1_attenuator":
        _finite(
            component["attenuation_db"], "components.rx1_attenuator.attenuation_db", positive=True
        )
        if component["orientation"] not in {"input_to_output", "bidirectional"}:
            raise InputOffContractError("RX1 attenuator orientation is not explicit")
    if role == "rx2_attenuator":
        _finite(
            component["attenuation_db"], "components.rx2_attenuator.attenuation_db", positive=True
        )
        if component["orientation"] not in {"input_toward_fixture", "output_toward_fixture"}:
            raise InputOffContractError(
                "RX2 attenuator orientation must identify which labelled port faces the fixture"
            )
        if set(ports) != {"input", "output"}:
            raise InputOffContractError("RX2 attenuator ports must name input and output")
    return normalized


def _optional_rx2_attenuator(
    value: object,
    *,
    components: Mapping[str, Mapping[str, Any]],
    serial: str,
) -> dict[str, Any]:
    """Normalize an explicit absent state or a fully identified RX2 attenuator graph."""

    document = _mapping(value, "rx2_attenuator")
    _exact_keys(document, ("state", "component", "pluto_connection"), "rx2_attenuator")
    if document["state"] == "absent":
        if document["component"] is not None or document["pluto_connection"] is not None:
            raise InputOffContractError(
                "absent RX2 attenuator must use null component and Pluto connection"
            )
        return {"state": "absent", "component": None, "pluto_connection": None}
    if document["state"] != "present":
        raise InputOffContractError("RX2 attenuator state must be explicitly present or absent")
    component = _component(document["component"], "rx2_attenuator", serial=serial)
    expanded_components = {**components, "rx2_attenuator": component}
    connection = _connection(
        document["pluto_connection"],
        "rx2_attenuator_to_pluto",
        components=expanded_components,
    )
    fixture_port = "input" if component["orientation"] == "input_toward_fixture" else "output"
    pluto_port = "output" if fixture_port == "input" else "input"
    observed_source = (
        connection["from"]["component_role"],
        connection["from"]["port_role"],
    )
    observed_destination = (
        connection["to"]["component_role"],
        connection["to"]["port_role"],
    )
    if observed_source != ("rx2_attenuator", pluto_port) or observed_destination != (
        "pluto",
        "rx2",
    ):
        raise InputOffContractError(
            "RX2 attenuator Pluto connection differs from its declared physical orientation"
        )
    return {
        "state": "present",
        "component": component,
        "pluto_connection": connection,
    }


def _endpoint(
    value: object,
    label: str,
    *,
    components: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    endpoint = _mapping(value, label)
    _exact_keys(endpoint, ("component_role", "port_role"), label)
    component_role = endpoint["component_role"]
    port_role = endpoint["port_role"]
    if component_role not in components or not isinstance(port_role, str):
        raise InputOffContractError(f"{label} names an unknown component or port")
    ports = _mapping(components[str(component_role)]["ports"], f"{label}.component.ports")
    if port_role not in ports:
        raise InputOffContractError(f"{label} port role does not exist on its component")
    return {"component_role": str(component_role), "port_role": port_role}


def _connection(
    value: object,
    role: str,
    *,
    components: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    connection = _mapping(value, f"connections.{role}")
    _exact_keys(connection, ("id", "from", "to", "interconnect"), f"connections.{role}")
    interconnect = _mapping(connection["interconnect"], f"connections.{role}.interconnect")
    _exact_keys(
        interconnect,
        (
            "id",
            "kind",
            "rated_min_frequency_hz",
            "rated_max_frequency_hz",
            "maximum_input_power_dbm",
            "characterization",
        ),
        f"connections.{role}.interconnect",
    )
    minimum = _finite(interconnect["rated_min_frequency_hz"], f"connections.{role}.rated_min")
    maximum = _finite(interconnect["rated_max_frequency_hz"], f"connections.{role}.rated_max")
    if minimum > CENTER_FREQUENCY_HZ or maximum < CENTER_FREQUENCY_HZ:
        raise InputOffContractError(f"connections.{role} is not rated at 5.8 GHz")
    _finite(interconnect["maximum_input_power_dbm"], f"connections.{role}.power")
    _characterization(interconnect["characterization"], f"connections.{role}.characterization")
    return {
        "id": _identifier(connection["id"], f"connections.{role}.id"),
        "from": _endpoint(connection["from"], f"connections.{role}.from", components=components),
        "to": _endpoint(connection["to"], f"connections.{role}.to", components=components),
        "interconnect": {
            **_normalized_mapping(interconnect),
            "id": _identifier(interconnect["id"], f"connections.{role}.interconnect.id"),
        },
    }


def _assert_connection_endpoints(
    connections: Mapping[str, Mapping[str, Any]],
    *,
    rx2_attenuator: Mapping[str, Any],
) -> None:
    expected: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
        "tx1_to_two_way": (("pluto", "tx1"), ("two_way_splitter", "input")),
        "two_way_reference_to_rx1_attenuator": (
            ("two_way_splitter", "reference_output"),
            ("rx1_attenuator", "input"),
        ),
        "rx1_attenuator_to_rx1": (("rx1_attenuator", "output"), ("pluto", "rx1")),
        "tx2_to_termination": (("pluto", "tx2"), ("tx2_termination", "load")),
        "two_way_stimulus_to_termination": (
            ("two_way_splitter", "stimulus_output"),
            ("tx1_stimulus_termination", "load"),
        ),
        "eight_way_input_to_termination": (
            ("eight_way_splitter", "input"),
            ("eight_way_input_termination", "load"),
        ),
        "selector_common_to_rx2": (
            ("selector", "common"),
            (
                ("pluto", "rx2")
                if rx2_attenuator["state"] == "absent"
                else (
                    "rx2_attenuator",
                    "input"
                    if rx2_attenuator["component"]["orientation"] == "input_toward_fixture"
                    else "output",
                )
            ),
        ),
    }
    expected.update(
        {
            f"eight_way_f{index}_to_selector_ant{index}": (
                ("eight_way_splitter", f"f{index}"),
                ("selector", f"ant{index}"),
            )
            for index in range(1, 9)
        }
    )
    for role, (source, destination) in expected.items():
        connection = connections[role]
        observed_source = (
            connection["from"]["component_role"],
            connection["from"]["port_role"],
        )
        observed_destination = (
            connection["to"]["component_role"],
            connection["to"]["port_role"],
        )
        if observed_source != source or observed_destination != destination:
            raise InputOffContractError(f"connections.{role} endpoints differ from P2 topology")


def validate_fixture_v2(
    value: object,
    *,
    run_id: str,
    board_id: str,
    serial: str,
) -> dict[str, Any]:
    """Validate and normalize the exact two-load P2 graph.

    The graph represents one and only one change from P0: the splitter stimulus
    connection is opened, and its two newly exposed reference planes receive
    two distinct rated 50-ohm loads.  All downstream and RX1 connections remain
    named as unchanged operator-attested edges.
    """

    document = _mapping(value, "fixture")
    _assert_no_placeholders(document)
    _exact_keys(
        document,
        (
            "schema",
            "fixture_kind",
            "campaign_id",
            "comparable_fixture_group_id",
            "topology_stage",
            "topology_token",
            "run_id",
            "board_id",
            "pluto_serial",
            "reference_planes",
            "components",
            "rx2_attenuator",
            "connections",
            "declared_p0_to_p2_delta",
            "baseline_topology_evidence",
            "fast20_control",
        ),
        "fixture",
    )
    if (
        document["schema"] != 2
        or document["fixture_kind"] != FIXTURE_KIND
        or document["campaign_id"] != CAMPAIGN_ID
        or document["topology_stage"] != TOPOLOGY_STAGE
        or document["topology_token"] != TOPOLOGY_TOKEN
        or document["run_id"] != run_id
        or document["board_id"] != board_id
        or document["pluto_serial"] != serial
    ):
        raise InputOffContractError("fixture identity differs from the P2 run plan")
    _identifier(document["comparable_fixture_group_id"], "comparable fixture group ID")

    planes = _mapping(document["reference_planes"], "reference_planes")
    _exact_keys(planes, REFERENCE_PLANE_ROLES, "reference_planes")
    normalized_planes = {
        role: _identifier(planes[role], f"reference_planes.{role}")
        for role in REFERENCE_PLANE_ROLES
    }
    if len(set(normalized_planes.values())) != len(normalized_planes):
        raise InputOffContractError("every fixture reference plane must have a distinct ID")

    raw_components = _mapping(document["components"], "components")
    _exact_keys(raw_components, COMPONENT_ROLES, "components")
    components = {
        role: _component(raw_components[role], role, serial=serial) for role in COMPONENT_ROLES
    }
    rx2_attenuator = _optional_rx2_attenuator(
        document["rx2_attenuator"],
        components=components,
        serial=serial,
    )
    component_ids = [str(component["id"]) for component in components.values()]
    if rx2_attenuator["state"] == "present":
        component_ids.append(str(rx2_attenuator["component"]["id"]))
    if len(set(component_ids)) != len(component_ids):
        raise InputOffContractError("fixture component IDs must be globally unique")
    load_ids = {
        components["tx1_stimulus_termination"]["id"],
        components["eight_way_input_termination"]["id"],
    }
    if len(load_ids) != 2:
        raise InputOffContractError("P2 requires two separately identified 50-ohm loads")

    raw_connections = _mapping(document["connections"], "connections")
    _exact_keys(raw_connections, P2_CONNECTION_ROLES, "connections")
    connection_components = dict(components)
    if rx2_attenuator["state"] == "present":
        connection_components["rx2_attenuator"] = rx2_attenuator["component"]
    connections = {
        role: _connection(raw_connections[role], role, components=connection_components)
        for role in P2_CONNECTION_ROLES
    }
    connection_ids = [str(connection["id"]) for connection in connections.values()]
    interconnect_ids = [
        str(connection["interconnect"]["id"]) for connection in connections.values()
    ]
    if rx2_attenuator["state"] == "present":
        optional_connection = rx2_attenuator["pluto_connection"]
        connection_ids.append(str(optional_connection["id"]))
        interconnect_ids.append(str(optional_connection["interconnect"]["id"]))
    if len(set(connection_ids)) != len(connection_ids) or len(set(interconnect_ids)) != len(
        interconnect_ids
    ):
        raise InputOffContractError("connection and interconnect IDs must each be unique")
    _assert_connection_endpoints(connections, rx2_attenuator=rx2_attenuator)

    delta = _mapping(document["declared_p0_to_p2_delta"], "declared_p0_to_p2_delta")
    _exact_keys(
        delta,
        (
            "removed_connection",
            "added_connection_roles",
            "unchanged_connection_roles",
            "no_other_component_or_connection_moved",
        ),
        "declared_p0_to_p2_delta",
    )
    removed = _mapping(delta["removed_connection"], "declared_p0_to_p2_delta.removed_connection")
    _exact_keys(removed, ("id", "from", "to"), "declared_p0_to_p2_delta.removed_connection")
    if removed["from"] != {
        "component_role": "two_way_splitter",
        "port_role": "stimulus_output",
    } or removed["to"] != {"component_role": "eight_way_splitter", "port_role": "input"}:
        raise InputOffContractError("declared P0 removal is not the stimulus-to-8-way connection")
    _identifier(removed["id"], "declared removed connection ID")
    if delta["added_connection_roles"] != [
        "two_way_stimulus_to_termination",
        "eight_way_input_to_termination",
    ]:
        raise InputOffContractError("declared P2 additions must be the two termination edges")
    if delta["unchanged_connection_roles"] != list(FIXED_CONNECTION_ROLES):
        raise InputOffContractError("declared unchanged graph does not cover every fixed P2 edge")
    if delta["no_other_component_or_connection_moved"] is not True:
        raise InputOffContractError("fixture must attest that no other graph edge moved")

    baseline = _file_evidence(document["baseline_topology_evidence"], "baseline_topology_evidence")
    fast20 = _mapping(document["fast20_control"], "fast20_control")
    _exact_keys(fast20, ("mode", "profile", "live_image_evidence"), "fast20_control")
    if fast20["mode"] != "autonomous_fast20_schedule":
        raise InputOffContractError("P2 must retain the autonomous Fast20 image")
    normalized_fast20 = {
        "mode": fast20["mode"],
        "profile": _file_evidence(fast20["profile"], "fast20_control.profile"),
        "live_image_evidence": _file_evidence(
            fast20["live_image_evidence"], "fast20_control.live_image_evidence"
        ),
    }
    return {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND,
        "campaign_id": CAMPAIGN_ID,
        "comparable_fixture_group_id": document["comparable_fixture_group_id"],
        "topology_stage": TOPOLOGY_STAGE,
        "topology_token": TOPOLOGY_TOKEN,
        "run_id": run_id,
        "board_id": board_id,
        "pluto_serial": serial,
        "reference_planes": normalized_planes,
        "components": components,
        "rx2_attenuator": rx2_attenuator,
        "connections": connections,
        "declared_p0_to_p2_delta": json.loads(canonical_json(delta)),
        "baseline_topology_evidence": baseline,
        "fast20_control": normalized_fast20,
        "component_ids": sorted(component_ids),
        "connection_ids": sorted(connection_ids),
        "fixed_graph_sha256": canonical_sha256(
            {
                "components": {
                    role: components[role]
                    for role in COMPONENT_ROLES
                    if role not in {"tx1_stimulus_termination", "eight_way_input_termination"}
                },
                "rx2_attenuator": rx2_attenuator,
                "connections": {role: connections[role] for role in FIXED_CONNECTION_ROLES},
                "reference_planes": {
                    role: normalized_planes[role]
                    for role in REFERENCE_PLANE_ROLES
                    if role
                    not in {
                        "tx1_stimulus_termination_load",
                        "eight_way_input_termination_load",
                    }
                },
            }
        ),
    }


def validate_setup_attestation(
    value: object,
    *,
    fixture: Mapping[str, Any],
    fixture_file_sha256: str,
    run_id: str,
) -> dict[str, Any]:
    """Validate the run-bound human observation of the exact P2 topology."""

    document = _mapping(value, "setup attestation")
    _assert_no_placeholders(document, location="setup attestation")
    _exact_keys(
        document,
        (
            "schema",
            "attestation_kind",
            "attestation_id",
            "created_at",
            "operator_id",
            "run_id",
            "campaign_id",
            "topology_stage",
            "fixture_manifest_sha256",
            "observed_component_ids",
            "observed_connection_ids",
            "setup_evidence",
            "confirmations",
        ),
        "setup attestation",
    )
    if (
        document["schema"] != 1
        or document["attestation_kind"] != SETUP_KIND
        or document["run_id"] != run_id
        or document["campaign_id"] != CAMPAIGN_ID
        or document["topology_stage"] != TOPOLOGY_STAGE
        or document["fixture_manifest_sha256"] != fixture_file_sha256
    ):
        raise InputOffContractError("setup attestation is not bound to this P2 fixture/run")
    _identifier(document["attestation_id"], "setup attestation ID")
    _identifier(document["operator_id"], "operator ID")
    _timezone_timestamp(document["created_at"], "setup attestation created_at")
    if document["observed_component_ids"] != fixture["component_ids"]:
        raise InputOffContractError("observed setup component inventory differs from fixture")
    if document["observed_connection_ids"] != fixture["connection_ids"]:
        raise InputOffContractError("observed setup connection inventory differs from fixture")
    confirmations = _mapping(document["confirmations"], "setup confirmations")
    required = (
        "no_antennas",
        "tx1_matched_two_way_still_feeds_protected_rx1",
        "tx1_stimulus_branch_has_own_rated_50ohm_load",
        "eight_way_input_has_separate_rated_50ohm_load",
        "two_loads_and_reference_planes_are_distinct",
        "all_eight_way_outputs_unchanged",
        "selector_and_rx2_common_cable_unchanged",
        "rx1_chain_unchanged",
        "tx2_terminated_and_muted",
        "fast20_live_and_unchanged",
        "no_other_component_or_connection_moved_since_p0_evidence",
    )
    _exact_keys(confirmations, required, "setup confirmations")
    if any(confirmations[field] is not True for field in required):
        raise InputOffContractError("every physical P2 setup confirmation must be true")
    return {
        **json.loads(canonical_json(document)),
        "setup_evidence": _file_evidence(document["setup_evidence"], "setup_evidence"),
    }


def acquisition_contract() -> dict[str, Any]:
    """Return the exact P0-matched P2 acquisition contract."""

    return {
        "center_frequency_hz": CENTER_FREQUENCY_HZ,
        "tone_offset_hz_requested": TONE_OFFSET_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "duration_s": DURATION_S,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "sample_count": TOTAL_SAMPLES,
        "kernel_buffers": KERNEL_BUFFERS,
        "receiver_gain_db": RECEIVER_GAIN_DB,
        "tx_channel": TX_CHANNEL,
        "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
        "dds_scale": DDS_SCALE,
        "selector_schedule": "Fast20",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "all_off_window_estimator": "Fast20 central interior windows",
        "edge_exclusion_bins": EDGE_EXCLUSION_BINS,
        "alignment_search_mode": "transition_seeded",
    }


def _complex(value: object, label: str) -> complex:
    document = _mapping(value, label)
    _exact_keys(document, ("real", "imag"), label)
    return complex(
        _finite(document["real"], f"{label}.real"), _finite(document["imag"], f"{label}.imag")
    )


def complex_document(value: complex) -> dict[str, float]:
    """Serialize one finite complex number."""

    if not math.isfinite(value.real) or not math.isfinite(value.imag):
        raise InputOffContractError("complex evidence must be finite")
    return {"real": float(value.real), "imag": float(value.imag)}


def coherent_tone_snr_db(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    tone_hz: float,
) -> float:
    """Estimate coherent-tone power relative to the residual sample power.

    This intentionally simple estimator is shared by legacy-P0 normalization
    and live P2 analysis.  Both cohorts therefore apply the same 20 dB pilot
    gate rather than borrowing selected-state transfer SNR from one analyzer.
    """

    rate = _finite(sample_rate_hz, "sample rate", positive=True)
    tone = _finite(tone_hz, "tone frequency")
    values = np.asarray(samples)
    if values.ndim != 1 or values.size < 2 or not np.iscomplexobj(values):
        raise InputOffContractError("pilot SNR input must be a complex one-dimensional stream")
    if abs(tone) >= rate / 2.0:
        raise InputOffContractError("pilot SNR tone must be strictly inside Nyquist")
    chunk_size = 1_000_000
    coherent_sum = 0j
    for start in range(0, values.size, chunk_size):
        stop = min(values.size, start + chunk_size)
        sample_indices = np.arange(start, stop, dtype=np.float64)
        carrier = np.exp(-2j * np.pi * tone * sample_indices / rate)
        coherent_sum += complex(
            np.sum(values[start:stop].astype(np.complex128, copy=False) * carrier)
        )
    phasor = coherent_sum / values.size
    residual_sum = 0.0
    for start in range(0, values.size, chunk_size):
        stop = min(values.size, start + chunk_size)
        sample_indices = np.arange(start, stop, dtype=np.float64)
        carrier = np.exp(-2j * np.pi * tone * sample_indices / rate)
        baseband = values[start:stop].astype(np.complex128, copy=False) * carrier
        residual_sum += float(np.sum(np.abs(baseband - phasor) ** 2))
    residual_power = residual_sum / values.size
    if residual_power <= np.finfo(np.float64).tiny:
        return float("inf")
    return 10.0 * math.log10(abs(phasor) ** 2 / residual_power)


def phase_free_complex_upper_bound(
    cycle_phasors: npt.ArrayLike,
    *,
    bootstrap_replicates: int = 8_192,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> float:
    """Return a conservative phase-free 95% amplitude bound.

    The observed componentwise center is retained only as amplitude.  Centered
    cycle residuals are independently resampled to estimate the 95th
    percentile of complex-center noise, which is then added to the observed
    amplitude.  No arbitrary phase or zero complex phasor is invented.
    """

    values = np.asarray(cycle_phasors, dtype=np.complex128)
    if values.ndim != 1 or values.size < 2 or not np.all(np.isfinite(values)):
        raise InputOffContractError(
            "phase-free upper bound requires at least two finite cycle phasors"
        )
    if isinstance(bootstrap_replicates, bool) or bootstrap_replicates < 1_000:
        raise InputOffContractError("phase-free upper bound requires at least 1000 replicates")
    center = _componentwise_median(values)
    residuals = values - center
    generator = np.random.default_rng(seed)
    indices = generator.integers(0, values.size, size=(bootstrap_replicates, values.size))
    samples = residuals[indices]
    residual_centers = np.median(samples.real, axis=1) + 1j * np.median(samples.imag, axis=1)
    uncertainty = float(np.percentile(np.abs(residual_centers), 95.0, method="linear"))
    return abs(center) + uncertainty


def validate_observation(
    value: object, *, expected_cohort: str | None = None
) -> InputOffObservation:
    """Validate one normalized P0 or P2 observation."""

    document = _mapping(value, "observation")
    _exact_keys(
        document,
        (
            "schema",
            "observation_kind",
            "cohort",
            "run_id",
            "artifact",
            "acquisition",
            "profile_contract_sha256",
            "analysis",
            "quality",
            "provenance",
        ),
        "observation",
    )
    cohort = document["cohort"]
    if (
        document["schema"] != 1
        or document["observation_kind"] != OBSERVATION_KIND
        or cohort not in {"P0", "P2"}
        or (expected_cohort is not None and cohort != expected_cohort)
    ):
        raise InputOffContractError("observation schema/cohort is invalid")
    run_id = _identifier(document["run_id"], "observation run ID")
    artifact = _mapping(document["artifact"], "observation artifact")
    _exact_keys(artifact, ("artifact_id", "stream_id", "sha256"), "observation artifact")
    stream_id = artifact["stream_id"]
    if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
        raise InputOffContractError("observation stream ID is invalid")
    acquisition = _mapping(document["acquisition"], "observation acquisition")
    if dict(acquisition) != acquisition_contract():
        raise InputOffContractError("observation acquisition differs from exact P0/P2 contract")
    analysis = _mapping(document["analysis"], "observation analysis")
    _exact_keys(
        analysis,
        (
            "transfer_detected",
            "all_off_transfer",
            "all_off_transfer_upper_bound",
            "rx1_reference_amplitude",
            "detected_pilot_snr_db",
        ),
        "observation analysis",
    )
    transfer_detected = analysis["transfer_detected"]
    if not isinstance(transfer_detected, bool):
        raise InputOffContractError("transfer detection status must be Boolean")
    raw_transfer = analysis["all_off_transfer"]
    raw_upper_bound = analysis["all_off_transfer_upper_bound"]
    if transfer_detected:
        if raw_transfer is None or raw_upper_bound is not None:
            raise InputOffContractError(
                "detected transfer requires one complex phasor and no phase-free bound"
            )
        transfer = _complex(raw_transfer, "all-off transfer")
        upper_bound = None
    else:
        if raw_transfer is not None or raw_upper_bound is None:
            raise InputOffContractError(
                "nondetection requires a phase-free upper bound and no complex phasor"
            )
        transfer = None
        upper_bound = _finite(raw_upper_bound, "all-off transfer upper bound", positive=True)
        if cohort == "P0":
            raise InputOffContractError("P0 screening cohort requires a detected transfer")
    reference_amplitude = _finite(
        analysis["rx1_reference_amplitude"], "RX1 reference amplitude", positive=True
    )
    pilot_snr = _finite(analysis["detected_pilot_snr_db"], "detected pilot SNR")
    quality = _mapping(document["quality"], "observation quality")
    required_quality = {
        "passed": True,
        "continuity_verified": True,
        "metadata_abi": 2,
        "headroom_passed": True,
        "final_mute_passed": True,
        "fast20_schedule_verified": True,
        "central_all_off_windows_used": True,
    }
    if any(quality.get(field) != expected for field, expected in required_quality.items()):
        raise InputOffContractError("observation did not pass every capture/analysis gate")
    if pilot_snr < MINIMUM_PILOT_SNR_DB:
        raise InputOffContractError("detected pilot SNR is below 20 dB")
    provenance = _mapping(document["provenance"], "observation provenance")
    _exact_keys(
        provenance,
        (
            "source_commit",
            "source_files_sha256",
            "native_attestation_sha256",
            "fixture_evidence_sha256",
            "fixture_fixed_graph_sha256",
            "comparable_fixture_group_id",
        ),
        "observation provenance",
    )
    source_commit = provenance["source_commit"]
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(c not in SHA256 for c in source_commit)
    ):
        raise InputOffContractError("observation source commit is malformed")
    optional_hashes: dict[str, str | None] = {}
    for field in (
        "source_files_sha256",
        "native_attestation_sha256",
        "fixture_evidence_sha256",
        "fixture_fixed_graph_sha256",
    ):
        raw = provenance[field]
        optional_hashes[field] = None if raw is None else _sha256(raw, f"provenance.{field}")
    group_id = provenance["comparable_fixture_group_id"]
    if group_id is not None:
        group_id = _identifier(group_id, "provenance.comparable_fixture_group_id")
    if cohort == "P2" and (
        any(optional_hashes[field] is None for field in optional_hashes) or group_id is None
    ):
        raise InputOffContractError("P2 observation lacks hardened source/native/fixture binding")
    if cohort == "P0" and group_id is not None:
        raise InputOffContractError("legacy P0 must not claim a fixture-v2 comparable group")
    return InputOffObservation(
        cohort=cohort,
        run_id=run_id,
        artifact_id=_identifier(artifact["artifact_id"], "artifact ID"),
        stream_id=stream_id,
        artifact_sha256=_sha256(artifact["sha256"], "artifact SHA-256"),
        profile_contract_sha256=_sha256(
            document["profile_contract_sha256"], "profile contract SHA-256"
        ),
        transfer_detected=transfer_detected,
        all_off_transfer=transfer,
        all_off_transfer_upper_bound=upper_bound,
        rx1_reference_amplitude=reference_amplitude,
        detected_pilot_snr_db=pilot_snr,
        source_commit=source_commit,
        source_files_sha256=optional_hashes["source_files_sha256"],
        native_attestation_sha256=optional_hashes["native_attestation_sha256"],
        fixture_evidence_sha256=optional_hashes["fixture_evidence_sha256"],
        fixture_fixed_graph_sha256=optional_hashes["fixture_fixed_graph_sha256"],
        comparable_fixture_group_id=group_id,
    )


def _componentwise_median(values: npt.NDArray[np.complex128]) -> complex:
    return complex(float(np.median(values.real)), float(np.median(values.imag)))


def _percentile_interval(values: npt.NDArray[np.float64]) -> tuple[float, float]:
    low, high = np.percentile(values, (2.5, 97.5), method="linear")
    return float(low), float(high)


def _circular_std_deg(values: npt.NDArray[np.complex128]) -> float:
    amplitudes = np.abs(values)
    unit = values / np.maximum(amplitudes, np.finfo(np.float64).tiny)
    resultant = float(np.clip(abs(np.mean(unit)), np.finfo(np.float64).tiny, 1.0))
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(resultant))))


def validate_p0_cohort(
    values: Sequence[InputOffObservation | Mapping[str, Any]],
) -> dict[str, Any]:
    """Require the frozen five-run P0 screening repeatability gate."""

    observations = tuple(
        item
        if isinstance(item, InputOffObservation)
        else validate_observation(item, expected_cohort="P0")
        for item in values
    )
    if len(observations) != COHORT_SIZE:
        raise InputOffContractError("P0 requires exactly five observations")
    for field, identities in (
        ("run IDs", [item.run_id for item in observations]),
        ("artifact IDs", [item.artifact_id for item in observations]),
        ("stream IDs", [item.stream_id for item in observations]),
        ("artifact hashes", [item.artifact_sha256 for item in observations]),
    ):
        if len(set(identities)) != COHORT_SIZE:
            raise InputOffContractError(f"P0 {field} are not source-distinct")
    if len({item.profile_contract_sha256 for item in observations}) != 1:
        raise InputOffContractError("P0 runs do not share one Fast20 profile")
    if len({item.source_commit for item in observations}) != 1:
        raise InputOffContractError("P0 runs do not share one frozen source commit")
    detected_transfers: list[complex] = []
    for item in observations:
        if not item.transfer_detected or item.all_off_transfer is None:
            raise InputOffContractError("P0 repeatability requires five detected complex transfers")
        detected_transfers.append(item.all_off_transfer)
    transfers = np.asarray(detected_transfers, dtype=np.complex128)
    amplitudes = np.abs(transfers)
    amplitude_cv = float(np.std(amplitudes, ddof=1) / np.mean(amplitudes))
    phase_std_deg = _circular_std_deg(transfers)
    minimum_snr = min(item.detected_pilot_snr_db for item in observations)
    passed = (
        amplitude_cv <= MAXIMUM_P0_AMPLITUDE_CV
        and phase_std_deg <= MAXIMUM_P0_PHASE_STD_DEG
        and minimum_snr >= MINIMUM_PILOT_SNR_DB
    )
    if not passed:
        raise InputOffContractError("P0 cohort fails the frozen repeatability gate")
    return {
        "passed": True,
        "amplitude_coefficient_of_variation": amplitude_cv,
        "maximum_amplitude_coefficient_of_variation": MAXIMUM_P0_AMPLITUDE_CV,
        "circular_phase_standard_deviation_deg": phase_std_deg,
        "maximum_circular_phase_standard_deviation_deg": MAXIMUM_P0_PHASE_STD_DEG,
        "minimum_detected_pilot_snr_db": minimum_snr,
    }


def compare_p0_p2_cohorts(
    p0_values: Sequence[InputOffObservation | Mapping[str, Any]],
    p2_values: Sequence[InputOffObservation | Mapping[str, Any]],
    *,
    bootstrap_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Apply the predeclared independent complex-bootstrap P2 gate.

    Repeat numbers are intentionally not paired.  Each bootstrap replicate
    independently resamples five P0 runs and five P2 runs, takes a
    componentwise complex median, and evaluates the magnitude ratio.  RX1
    stability is bootstrapped independently in dB from cohort medians.
    """

    if isinstance(bootstrap_replicates, bool) or bootstrap_replicates < 1_000:
        raise InputOffContractError("bootstrap requires at least 1000 replicates")
    p0 = tuple(
        item
        if isinstance(item, InputOffObservation)
        else validate_observation(item, expected_cohort="P0")
        for item in p0_values
    )
    p2 = tuple(
        item
        if isinstance(item, InputOffObservation)
        else validate_observation(item, expected_cohort="P2")
        for item in p2_values
    )
    if len(p0) != COHORT_SIZE or len(p2) != COHORT_SIZE:
        raise InputOffContractError("P2 gate requires exactly five P0 and five P2 runs")
    p0_repeatability = validate_p0_cohort(p0)
    for label, cohort in (("P0", p0), ("P2", p2)):
        for field, values in (
            ("run IDs", [item.run_id for item in cohort]),
            ("artifact IDs", [item.artifact_id for item in cohort]),
            ("stream IDs", [item.stream_id for item in cohort]),
            ("artifact hashes", [item.artifact_sha256 for item in cohort]),
        ):
            if len(set(values)) != COHORT_SIZE:
                raise InputOffContractError(f"{label} {field} are not source-distinct")
    if set(item.artifact_id for item in p0) & set(item.artifact_id for item in p2):
        raise InputOffContractError("P0 and P2 reuse an artifact")
    if set(item.stream_id for item in p0) & set(item.stream_id for item in p2):
        raise InputOffContractError("P0 and P2 reuse a stream")
    profiles = {item.profile_contract_sha256 for item in (*p0, *p2)}
    if len(profiles) != 1:
        raise InputOffContractError("P0 and P2 do not use the same Fast20 profile")
    source_commits = {item.source_commit for item in (*p0, *p2)}
    if len(source_commits) != 1:
        raise InputOffContractError("P0 and P2 were not captured at one frozen source commit")
    source_file_hashes = {item.source_files_sha256 for item in p2}
    native_hashes = {item.native_attestation_sha256 for item in p2}
    if len(source_file_hashes) != 1 or None in source_file_hashes:
        raise InputOffContractError("P2 source-file identities differ across runs")
    if len(native_hashes) != 1 or None in native_hashes:
        raise InputOffContractError("P2 native-libiio identities differ across runs")
    fixture_hashes = {item.fixture_evidence_sha256 for item in p2}
    if None in fixture_hashes or len(fixture_hashes) != COHORT_SIZE:
        raise InputOffContractError("P2 run-bound fixture evidence is missing or reused")
    fixed_graph_hashes = {item.fixture_fixed_graph_sha256 for item in p2}
    fixture_groups = {item.comparable_fixture_group_id for item in p2}
    if len(fixed_graph_hashes) != 1 or None in fixed_graph_hashes:
        raise InputOffContractError("P2 runs do not share one exact fixed downstream graph")
    if len(fixture_groups) != 1 or None in fixture_groups:
        raise InputOffContractError("P2 runs do not share one comparable fixture group")

    p0_detected = [item.all_off_transfer for item in p0 if item.all_off_transfer is not None]
    if len(p0_detected) != COHORT_SIZE:  # already enforced by validate_p0_cohort
        raise InputOffContractError("P0 complex transfers are incomplete")
    p0_transfer = np.asarray(p0_detected, dtype=np.complex128)
    p0_reference = np.asarray([item.rx1_reference_amplitude for item in p0], dtype=np.float64)
    p2_reference = np.asarray([item.rx1_reference_amplitude for item in p2], dtype=np.float64)
    generator = np.random.default_rng(seed)
    p0_indices = generator.integers(0, COHORT_SIZE, size=(bootstrap_replicates, COHORT_SIZE))
    p2_indices = generator.integers(0, COHORT_SIZE, size=(bootstrap_replicates, COHORT_SIZE))
    reference_difference_db = (
        20.0
        * np.log10(
            np.median(p2_reference[p2_indices], axis=1)
            / np.median(p0_reference[p0_indices], axis=1)
        )
    ).astype(np.float64)
    reference_interval = _percentile_interval(reference_difference_db)
    reference_stable = (
        reference_interval[0] >= -REFERENCE_STABILITY_DB
        and reference_interval[1] <= REFERENCE_STABILITY_DB
    )
    p0_center = _componentwise_median(p0_transfer)
    nondetection_count = sum(not item.transfer_detected for item in p2)
    if nondetection_count:
        conservative_p2_bounds: list[float] = []
        for item in p2:
            if item.all_off_transfer_upper_bound is not None:
                conservative_p2_bounds.append(item.all_off_transfer_upper_bound)
            elif item.all_off_transfer is not None:
                conservative_p2_bounds.append(abs(item.all_off_transfer))
            else:  # rejected by observation validation
                raise InputOffContractError("P2 nondetection lacks a phase-free upper bound")
        return {
            "schema": 1,
            "analysis_kind": "5g8_p0_p2_independent_complex_bootstrap",
            "cohort_size_each": COHORT_SIZE,
            "bootstrap": {
                "independent_two_sample": True,
                "paired_repeat_indices": False,
                "replicates": bootstrap_replicates,
                "seed": seed,
                "complex_gate_evaluated": False,
                "reason": "one_or_more_p2_phase_free_nondetections",
            },
            "p0_repeatability": p0_repeatability,
            "p0": {
                "run_ids": [item.run_id for item in p0],
                "artifact_ids": [item.artifact_id for item in p0],
                "stream_ids": [item.stream_id for item in p0],
                "complex_center": complex_document(p0_center),
                "amplitude_center": abs(p0_center),
            },
            "p2": {
                "run_ids": [item.run_id for item in p2],
                "artifact_ids": [item.artifact_id for item in p2],
                "stream_ids": [item.stream_id for item in p2],
                "complex_center": None,
                "amplitude_center": None,
                "phase_free_nondetection_count": nondetection_count,
                "per_run_amplitude_upper_bounds": conservative_p2_bounds,
            },
            "transfer_magnitude_ratio": {
                "point_estimate": None,
                "confidence_interval_95": None,
                "conservative_median_bound_over_p0_point_center": (
                    float(np.median(conservative_p2_bounds)) / abs(p0_center)
                ),
                "collapse_upper_threshold": COLLAPSE_UPPER_RATIO,
                "not_collapsed_lower_threshold": NOT_COLLAPSED_LOWER_RATIO,
            },
            "rx1_reference_difference_db": {
                "point_estimate": 20.0
                * math.log10(float(np.median(p2_reference)) / float(np.median(p0_reference))),
                "confidence_interval_95": list(reference_interval),
                "required_interval_db": [-REFERENCE_STABILITY_DB, REFERENCE_STABILITY_DB],
                "stable": reference_stable,
            },
            "disposition": "inconclusive",
            "input_drive_required": False,
            "physical_branch_selected": False,
            "limitations": (
                "At least one P2 result is a phase-free nondetection. It is retained with an "
                "upper bound, but the predeclared complex-bootstrap branch gate is not guessed."
            ),
        }

    p2_detected = [item.all_off_transfer for item in p2 if item.all_off_transfer is not None]
    if len(p2_detected) != COHORT_SIZE:
        raise InputOffContractError("P2 complex transfers are incomplete")
    p2_transfer = np.asarray(p2_detected, dtype=np.complex128)
    p0_samples = p0_transfer[p0_indices]
    p2_samples = p2_transfer[p2_indices]
    p0_centers = np.median(p0_samples.real, axis=1) + 1j * np.median(p0_samples.imag, axis=1)
    p2_centers = np.median(p2_samples.real, axis=1) + 1j * np.median(p2_samples.imag, axis=1)
    denominator = np.abs(p0_centers)
    if np.any(denominator <= np.finfo(np.float64).tiny):
        raise InputOffContractError("P0 bootstrap contains a zero complex center")
    ratios = (np.abs(p2_centers) / denominator).astype(np.float64)
    ratio_interval = _percentile_interval(ratios)
    if reference_stable and ratio_interval[1] <= COLLAPSE_UPPER_RATIO:
        disposition = "input_drive_required"
    elif reference_stable and ratio_interval[0] >= NOT_COLLAPSED_LOWER_RATIO:
        disposition = "not_collapsed"
    else:
        disposition = "inconclusive"

    p2_center = _componentwise_median(p2_transfer)
    return {
        "schema": 1,
        "analysis_kind": "5g8_p0_p2_independent_complex_bootstrap",
        "cohort_size_each": COHORT_SIZE,
        "bootstrap": {
            "independent_two_sample": True,
            "paired_repeat_indices": False,
            "replicates": bootstrap_replicates,
            "seed": seed,
            "complex_center": "componentwise median(real), median(imag)",
            "interval": "95% equal-tailed percentile, numpy linear quantiles",
        },
        "p0_repeatability": p0_repeatability,
        "p0": {
            "run_ids": [item.run_id for item in p0],
            "artifact_ids": [item.artifact_id for item in p0],
            "stream_ids": [item.stream_id for item in p0],
            "complex_center": complex_document(p0_center),
            "amplitude_center": abs(p0_center),
        },
        "p2": {
            "run_ids": [item.run_id for item in p2],
            "artifact_ids": [item.artifact_id for item in p2],
            "stream_ids": [item.stream_id for item in p2],
            "complex_center": complex_document(p2_center),
            "amplitude_center": abs(p2_center),
        },
        "transfer_magnitude_ratio": {
            "point_estimate": abs(p2_center) / abs(p0_center),
            "confidence_interval_95": list(ratio_interval),
            "collapse_upper_threshold": COLLAPSE_UPPER_RATIO,
            "not_collapsed_lower_threshold": NOT_COLLAPSED_LOWER_RATIO,
        },
        "rx1_reference_difference_db": {
            "point_estimate": 20.0
            * math.log10(float(np.median(p2_reference)) / float(np.median(p0_reference))),
            "confidence_interval_95": list(reference_interval),
            "required_interval_db": [-REFERENCE_STABILITY_DB, REFERENCE_STABILITY_DB],
            "stable": reference_stable,
        },
        "disposition": disposition,
        "input_drive_required": disposition == "input_drive_required",
        "physical_branch_selected": disposition == "input_drive_required",
        "limitations": (
            "P2 is an input-drive requirement screen only; it does not attribute A/B/C or a "
            "specific board path."
        ),
    }
