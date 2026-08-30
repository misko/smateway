"""Pure, fail-closed validation for the 5.8 GHz fixture-v2 evidence graph.

The RF runner, fixture generator, and selected-state release workflow all need
the same physical-fixture authority.  This module deliberately contains no RF,
IIO, GPIO, OpenOCD, or subprocess boundary.  It validates only immutable JSON
and files already named by that JSON.

The public entry points are:

``validate_fixture_manifest``
    Reopen and normalize one source A/B/C/E fixture manifest, including its
    immediately-prior plan chain.

``validate_setup_attestation``
    Reopen one strict per-run setup attestation and return the normalized form
    embedded in runner fixture evidence.

``validate_fixture_evidence``
    Independently reproduce the complete normalized fixture-evidence object
    from its two source files and reject any projection mismatch.

``validate_x_capture_linkage``
    Bind an A/B/C/E topology fixture to the exact full-E capture-state fixture
    and selector context used by an X intervention plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CENTER_FREQUENCY_HZ = 5_800_000_000
LOAD_INPUT_LIMIT_DBM = 0.0
FIXTURE_KIND_V2 = "5g8_general_topology_stage_fixture"
SETUP_ATTESTATION_KIND = "5g8_general_topology_run_setup"
FULL_CONDUCTED_STAGE = "full_conducted_fixture"
ANTENNA_PORTS = tuple(f"ANT{index}" for index in range(1, 9))
SHARED_CONNECTION_ROLES = (
    "tx1_to_splitter",
    "splitter_to_rx1_attenuator",
    "rx1_attenuator_to_rx1",
    "tx2_to_termination",
)
PRIOR_STAGE: dict[str, str | None] = {
    "direct_rx2_termination": None,
    "rx2_cable_terminated": "direct_rx2_termination",
    "powered_selector_all_inputs_terminated": "rx2_cable_terminated",
    FULL_CONDUCTED_STAGE: "powered_selector_all_inputs_terminated",
}
STAGES = tuple(PRIOR_STAGE)
SELECTOR_CONNECTED_STAGES = frozenset(
    {"powered_selector_all_inputs_terminated", FULL_CONDUCTED_STAGE}
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RAW_MANIFEST_FIELDS = {
    "schema",
    "fixture_kind",
    "campaign_id",
    "comparable_fixture_group_id",
    "stage",
    "board_id",
    "shared_fixture",
    "stage_delta",
    "prior_stage_binding",
}
_FIXTURE_EVIDENCE_FIELDS = {
    "schema",
    "fixture_kind",
    "campaign_id",
    "comparable_fixture_group_id",
    "stage",
    "run_id",
    "board_id",
    "source_files",
    "shared_fixture",
    "shared_fixture_sha256",
    "stage_delta",
    "stage_delta_sha256",
    "prior_stage_binding",
    "setup_attestation",
    "selector_flash_evidence",
    "component_ids",
    "connection_ids",
    "characterization_summary",
}
_RAW_SETUP_FIELDS = {
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
_NORMALIZED_SETUP_FIELDS = {
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


class FixtureV2Error(ValueError):
    """Fixture bytes do not satisfy the production fixture-v2 contract."""


@dataclass(frozen=True, slots=True)
class ValidatedFixtureManifest:
    """Canonical facts independently derived from one raw fixture manifest."""

    path: Path
    file_sha256: str
    size_bytes: int
    campaign_id: str
    comparable_fixture_group_id: str
    stage: str
    board_id: str
    serial: str
    shared_fixture: dict[str, Any]
    shared_fixture_sha256: str
    stage_delta: dict[str, Any]
    stage_delta_sha256: str
    source_prior_stage_binding: dict[str, Any] | None
    prior_stage_binding: dict[str, Any] | None
    prior_selector_flash_evidence: dict[str, Any] | None
    component_ids: tuple[str, ...]
    connection_ids: tuple[str, ...]
    characterization_summary: dict[str, Any]


def canonical_sha256(value: object) -> str:
    """Hash one JSON value using the runner's canonical representation."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FixtureV2Error("value is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def sha256_path(path: Path) -> str:
    """Hash one regular file after rejecting every symlink in its path."""

    exact = _regular_file(path, "hash input")
    digest = hashlib.sha256()
    with exact.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unexpected = sorted(set(value) - expected)
        raise FixtureV2Error(
            f"{label} fields are incomplete or unexpected; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise FixtureV2Error(f"{label} contains unsupported characters")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FixtureV2Error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FixtureV2Error(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FixtureV2Error(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise FixtureV2Error(f"{label} must include a timezone")
    return parsed.isoformat()


def _assert_no_symlink_chain(path: Path, label: str) -> Path:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            raise FixtureV2Error(f"{label} does not exist: {current}") from None
        if current.is_symlink():
            raise FixtureV2Error(f"{label} contains a symlink: {current}")
    return absolute


def _regular_file(path: Path, label: str) -> Path:
    exact = _assert_no_symlink_chain(path, label)
    if not exact.is_file():
        raise FixtureV2Error(f"{label} must be a regular non-symlink file")
    return exact


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], Path]:
    exact = _regular_file(path, label)
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FixtureV2Error(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise FixtureV2Error(f"{label} root must be an object")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise FixtureV2Error(f"{label} is not finite canonical JSON") from error
    return value, exact


def _file_evidence(value: object, label: str, *, verify_file: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error(f"{label} evidence must be an object")
    document = dict(value)
    _exact_keys(document, {"path", "sha256", "size_bytes"}, f"{label} evidence")
    path_value = document["path"]
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise FixtureV2Error(f"{label} evidence path must be absolute")
    size = document["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise FixtureV2Error(f"{label} evidence size must be positive")
    digest = _digest(document["sha256"], f"{label} evidence hash")
    path = Path(path_value).expanduser().absolute()
    if verify_file:
        exact = _regular_file(path, label)
        if exact.stat().st_size != size or sha256_path(exact) != digest:
            raise FixtureV2Error(f"{label} evidence differs from its path/size/hash binding")
        path = exact
    return {"path": str(path), "sha256": digest, "size_bytes": size}


def validate_selector_flash_binding(
    value: object,
    *,
    expected_campaign_id: str | None = None,
    expected_board_id: str | None = None,
    expected_image_role: str = "bench",
    verify_file: bool = False,
) -> dict[str, Any]:
    """Validate the immutable path/hash identity of one sealed selector image."""

    if not isinstance(value, Mapping):
        raise FixtureV2Error("selector-flash evidence binding must be an object")
    document = dict(value)
    _exact_keys(
        document,
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
        "selector-flash evidence binding",
    )
    path_value = document["path"]
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise FixtureV2Error("selector-flash evidence path must be absolute")
    campaign_id = _identifier(document["campaign_id"], "flash campaign ID")
    run_id = _identifier(document["run_id"], "flash run ID")
    board_id = _identifier(document["board_id"], "flash board ID")
    image_role = document["image_role"]
    if (
        document["schema"] != 1
        or document["binding_kind"] != "sealed_selector_flash_evidence_v1"
        or image_role != expected_image_role
        or (expected_campaign_id is not None and campaign_id != expected_campaign_id)
        or (expected_board_id is not None and board_id != expected_board_id)
    ):
        raise FixtureV2Error("selector-flash evidence identity differs from the capture")
    path = Path(path_value).expanduser().absolute()
    digest = _digest(document["sha256"], "selector-flash evidence hash")
    if verify_file and sha256_path(path) != digest:
        raise FixtureV2Error("selector-flash evidence bytes differ from the binding")
    return {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": str(path),
        "sha256": digest,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "board_id": board_id,
        "image_role": str(image_role),
    }


def _normalize_characterization(
    value: object,
    *,
    label: str,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error(f"{label} characterization must be an object")
    document = dict(value)
    _exact_keys(
        document,
        {
            "status",
            "evidence_path",
            "evidence_sha256",
            "s_parameter_sha256",
            "return_loss_db_at_5g8",
        },
        f"{label} characterization",
    )
    status = document["status"]
    if status not in {"characterized", "uncharacterized"}:
        raise FixtureV2Error(f"{label} characterization status is invalid")
    s_parameter = document["s_parameter_sha256"]
    if s_parameter is not None:
        document["s_parameter_sha256"] = _digest(s_parameter, f"{label} S-parameter hash")
    return_loss = document["return_loss_db_at_5g8"]
    if return_loss is not None and (
        isinstance(return_loss, bool)
        or not isinstance(return_loss, (int, float))
        or not math.isfinite(float(return_loss))
        or float(return_loss) < 0
    ):
        raise FixtureV2Error(f"{label} return loss must be a non-negative finite dB value")
    evidence_path = document["evidence_path"]
    evidence_hash = document["evidence_sha256"]
    if status == "uncharacterized":
        if any(
            item is not None for item in (evidence_path, evidence_hash, s_parameter, return_loss)
        ):
            raise FixtureV2Error(
                f"{label} must use explicit null characterization fields when uncharacterized"
            )
        return document
    if s_parameter is None or return_loss is None:
        raise FixtureV2Error(
            f"{label} characterized evidence requires an S-parameter hash and 5.8-GHz return loss"
        )
    if not isinstance(evidence_path, str) or not evidence_path:
        raise FixtureV2Error(f"{label} characterized evidence requires a file path")
    path = Path(evidence_path).expanduser()
    if not path.is_absolute():
        if base_directory is None:
            raise FixtureV2Error(f"{label} characterization evidence path must be absolute")
        path = base_directory / path
    path = path.absolute()
    digest = _digest(evidence_hash, f"{label} characterization evidence hash")
    if verify_files:
        exact = _regular_file(path, f"{label} characterization evidence")
        if sha256_path(exact) != digest:
            raise FixtureV2Error(f"{label} characterization evidence hash differs")
        path = exact
    document["evidence_path"] = str(path)
    document["evidence_sha256"] = digest
    return document


def _normalize_rated_asset(
    value: object,
    *,
    label: str,
    port_names: tuple[str, ...],
    extra_numeric_fields: tuple[str, ...] = (),
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error(f"{label} must be an object")
    document = dict(value)
    required = {
        "id",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "port_map",
        "characterization",
        *extra_numeric_fields,
    }
    _exact_keys(document, required, label)
    document["id"] = _identifier(document["id"], f"{label} ID")
    for field in {
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        *extra_numeric_fields,
    }:
        item = document[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise FixtureV2Error(f"{label} {field} must be a finite number")
    minimum = float(document["rated_min_frequency_hz"])
    maximum = float(document["rated_max_frequency_hz"])
    if minimum < 0 or minimum > CENTER_FREQUENCY_HZ or maximum < CENTER_FREQUENCY_HZ:
        raise FixtureV2Error(f"{label} frequency rating must contain 5.8 GHz")
    if maximum <= minimum:
        raise FixtureV2Error(f"{label} frequency rating is inverted")
    if float(document["maximum_input_power_dbm"]) < LOAD_INPUT_LIMIT_DBM:
        raise FixtureV2Error(f"{label} maximum input rating is below the frozen load limit")
    if "attenuation_db" in extra_numeric_fields and float(document["attenuation_db"]) <= 0:
        raise FixtureV2Error(f"{label} attenuation must be positive")
    if "impedance_ohm" in extra_numeric_fields and not math.isclose(
        float(document["impedance_ohm"]), 50.0, rel_tol=0.0, abs_tol=0.01
    ):
        raise FixtureV2Error(f"{label} must be rated 50 ohm")
    port_map = document["port_map"]
    if not isinstance(port_map, Mapping) or set(port_map) != set(port_names):
        raise FixtureV2Error(f"{label} port map is incomplete or unexpected")
    document["port_map"] = {
        name: _identifier(port_map[name], f"{label} {name} port ID") for name in port_names
    }
    if len(set(document["port_map"].values())) != len(document["port_map"]):
        raise FixtureV2Error(f"{label} physical port IDs must be unique within its port map")
    document["characterization"] = _normalize_characterization(
        document["characterization"],
        label=label,
        base_directory=base_directory,
        verify_files=verify_files,
    )
    return document


def _normalize_interconnect(
    value: object,
    *,
    label: str,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error(f"{label} interconnect must be an object")
    document = dict(value)
    _exact_keys(
        document,
        {
            "id",
            "kind",
            "rated_min_frequency_hz",
            "rated_max_frequency_hz",
            "maximum_input_power_dbm",
            "characterization",
        },
        f"{label} interconnect",
    )
    if document["kind"] not in {
        "coaxial_cable",
        "direct_adapter",
        "sma_barrel",
        "integrated_launch",
    }:
        raise FixtureV2Error(f"{label} interconnect kind is invalid")
    rated = _normalize_rated_asset(
        {key: item for key, item in document.items() if key != "kind"} | {"port_map": {}},
        label=f"{label} interconnect",
        port_names=(),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    rated["kind"] = document["kind"]
    rated.pop("port_map")
    return rated


def _normalize_endpoint(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"component_id", "port_id"}:
        raise FixtureV2Error(f"{label} endpoint is malformed")
    return {
        "component_id": _identifier(value["component_id"], f"{label} component"),
        "port_id": _identifier(value["port_id"], f"{label} port"),
    }


def _normalize_connection(
    value: object,
    *,
    label: str,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error(f"{label} connection must be an object")
    document = dict(value)
    _exact_keys(document, {"id", "from", "to", "interconnect"}, f"{label} connection")
    document["id"] = _identifier(document["id"], f"{label} connection ID")
    document["from"] = _normalize_endpoint(document["from"], f"{label} source")
    document["to"] = _normalize_endpoint(document["to"], f"{label} destination")
    document["interconnect"] = _normalize_interconnect(
        document["interconnect"],
        label=label,
        base_directory=base_directory,
        verify_files=verify_files,
    )
    return document


def _require_connection(
    connection: Mapping[str, Any],
    *,
    source: tuple[str, str],
    destination: tuple[str, str],
    label: str,
    required_kind: str | None = None,
) -> None:
    if connection.get("from") != {"component_id": source[0], "port_id": source[1]} or (
        connection.get("to") != {"component_id": destination[0], "port_id": destination[1]}
    ):
        raise FixtureV2Error(f"{label} endpoints differ from the frozen port-level graph")
    interconnect = connection.get("interconnect")
    if required_kind is not None and (
        not isinstance(interconnect, Mapping) or interconnect.get("kind") != required_kind
    ):
        raise FixtureV2Error(f"{label} requires a {required_kind} interconnect")


def _normalize_pluto(value: object, *, expected_serial: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"id", "serial", "port_map"}:
        raise FixtureV2Error("shared Pluto identity is malformed")
    serial = _identifier(value["serial"], "Pluto serial")
    if serial != expected_serial:
        raise FixtureV2Error("shared fixture Pluto serial differs from the exact plan serial")
    port_map = value["port_map"]
    if not isinstance(port_map, Mapping) or set(port_map) != {"tx1", "tx2", "rx1", "rx2"}:
        raise FixtureV2Error("shared Pluto port map is incomplete or unexpected")
    ports = {
        name: _identifier(port_map[name], f"Pluto {name} port ID")
        for name in ("tx1", "tx2", "rx1", "rx2")
    }
    if len(set(ports.values())) != len(ports):
        raise FixtureV2Error("Pluto physical port IDs must be unique within its port map")
    return {"id": _identifier(value["id"], "Pluto fixture ID"), "serial": serial, "port_map": ports}


def _normalize_optional_rx2_attenuator(
    value: object,
    *,
    pluto: Mapping[str, Any],
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "state",
        "asset",
        "orientation",
        "pluto_connection",
    }:
        raise FixtureV2Error("RX2 attenuator state fields are incomplete or unexpected")
    state = value["state"]
    if state == "absent":
        if any(value[field] is not None for field in ("asset", "orientation", "pluto_connection")):
            raise FixtureV2Error("absent RX2 attenuator must use null asset/orientation/connection")
        return {
            "state": "absent",
            "asset": None,
            "orientation": None,
            "pluto_connection": None,
        }
    if state != "present":
        raise FixtureV2Error("RX2 attenuator state must be explicitly present or absent")
    asset = _normalize_rated_asset(
        value["asset"],
        label="RX2 attenuator",
        port_names=("input", "output"),
        extra_numeric_fields=("attenuation_db",),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    orientation = value["orientation"]
    if not isinstance(orientation, Mapping) or set(orientation) != {
        "fixture_side_port_role",
        "pluto_side_port_role",
    }:
        raise FixtureV2Error("RX2 attenuator orientation is incomplete or unexpected")
    fixture_side = orientation["fixture_side_port_role"]
    pluto_side = orientation["pluto_side_port_role"]
    if {fixture_side, pluto_side} != {"input", "output"}:
        raise FixtureV2Error(
            "RX2 attenuator orientation must assign input/output to opposite sides"
        )
    connection = _normalize_connection(
        value["pluto_connection"],
        label="Pluto-RX2-to-attenuator",
        base_directory=base_directory,
        verify_files=verify_files,
    )
    _require_connection(
        connection,
        source=(str(pluto["id"]), str(pluto["port_map"]["rx2"])),
        destination=(str(asset["id"]), str(asset["port_map"][str(pluto_side)])),
        label="Pluto-RX2-to-attenuator connection",
    )
    return {
        "state": "present",
        "asset": asset,
        "orientation": {
            "fixture_side_port_role": str(fixture_side),
            "pluto_side_port_role": str(pluto_side),
        },
        "pluto_connection": connection,
    }


def normalize_shared_fixture(
    value: object,
    *,
    expected_serial: str,
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    """Normalize the exact A/B/C/E shared physical graph."""

    if not isinstance(value, Mapping):
        raise FixtureV2Error("shared fixture must be an object")
    _exact_keys(
        value,
        {
            "pluto",
            "reference_planes",
            "tx1_reference_splitter",
            "rx1_attenuator",
            "rx2_attenuator",
            "tx2_termination",
            "connections",
        },
        "shared fixture",
    )
    pluto = _normalize_pluto(value["pluto"], expected_serial=expected_serial)
    planes = value["reference_planes"]
    if not isinstance(planes, Mapping) or set(planes) != {"tx1", "rx1", "rx2"}:
        raise FixtureV2Error("TX1/RX1/RX2 reference planes must be separate explicit IDs")
    reference_planes = {
        name: _identifier(planes[name], f"{name} reference-plane ID")
        for name in ("tx1", "rx1", "rx2")
    }
    if len(set(reference_planes.values())) != 3:
        raise FixtureV2Error("TX1, RX1, and RX2 reference-plane IDs must be distinct")
    splitter = _normalize_rated_asset(
        value["tx1_reference_splitter"],
        label="TX1 two-way reference splitter",
        port_names=("input", "rx1_branch", "stimulus_branch"),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    attenuator = _normalize_rated_asset(
        value["rx1_attenuator"],
        label="RX1 attenuator",
        port_names=("input", "output"),
        extra_numeric_fields=("attenuation_db",),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    rx2_attenuator = _normalize_optional_rx2_attenuator(
        value["rx2_attenuator"],
        pluto=pluto,
        base_directory=base_directory,
        verify_files=verify_files,
    )
    tx2_load = _normalize_rated_asset(
        value["tx2_termination"],
        label="TX2 termination load",
        port_names=("load",),
        extra_numeric_fields=("impedance_ohm",),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    raw_connections = value["connections"]
    if not isinstance(raw_connections, Mapping) or set(raw_connections) != set(
        SHARED_CONNECTION_ROLES
    ):
        raise FixtureV2Error("shared fixture connection graph is incomplete or unexpected")
    connections = {
        role: _normalize_connection(
            raw_connections[role],
            label=f"shared {role}",
            base_directory=base_directory,
            verify_files=verify_files,
        )
        for role in SHARED_CONNECTION_ROLES
    }
    pluto_id = str(pluto["id"])
    pluto_ports = pluto["port_map"]
    splitter_id = str(splitter["id"])
    splitter_ports = splitter["port_map"]
    attenuator_id = str(attenuator["id"])
    attenuator_ports = attenuator["port_map"]
    _require_connection(
        connections["tx1_to_splitter"],
        source=(pluto_id, str(pluto_ports["tx1"])),
        destination=(splitter_id, str(splitter_ports["input"])),
        label="TX1-to-splitter connection",
    )
    _require_connection(
        connections["splitter_to_rx1_attenuator"],
        source=(splitter_id, str(splitter_ports["rx1_branch"])),
        destination=(attenuator_id, str(attenuator_ports["input"])),
        label="splitter-to-RX1-attenuator connection",
    )
    _require_connection(
        connections["rx1_attenuator_to_rx1"],
        source=(attenuator_id, str(attenuator_ports["output"])),
        destination=(pluto_id, str(pluto_ports["rx1"])),
        label="RX1-attenuator-to-receiver connection",
    )
    _require_connection(
        connections["tx2_to_termination"],
        source=(pluto_id, str(pluto_ports["tx2"])),
        destination=(str(tx2_load["id"]), str(tx2_load["port_map"]["load"])),
        label="TX2 termination connection",
    )
    return {
        "pluto": pluto,
        "reference_planes": reference_planes,
        "tx1_reference_splitter": splitter,
        "rx1_attenuator": attenuator,
        "rx2_attenuator": rx2_attenuator,
        "tx2_termination": tx2_load,
        "connections": connections,
    }


def _rx2_fixture_endpoint(shared: Mapping[str, Any]) -> tuple[str, str]:
    optional = shared["rx2_attenuator"]
    if optional["state"] == "absent":
        pluto = shared["pluto"]
        return str(pluto["id"]), str(pluto["port_map"]["rx2"])
    asset = optional["asset"]
    orientation = optional["orientation"]
    return (
        str(asset["id"]),
        str(asset["port_map"][str(orientation["fixture_side_port_role"])]),
    )


def _normalize_load(
    value: object,
    *,
    label: str,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    return _normalize_rated_asset(
        value,
        label=label,
        port_names=("load",),
        extra_numeric_fields=("impedance_ohm",),
        base_directory=base_directory,
        verify_files=verify_files,
    )


def _normalize_selector(
    value: object,
    *,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error("selector must be an object")
    identity_fields = {
        "physical_board_id",
        "hardware_revision",
        "bench_supply_id",
        "bench_supply_output_id",
        "power_positive_reference_id",
        "power_ground_reference_id",
        "control_ground_reference_id",
    }
    numeric_fields = {"supply_voltage_v", "supply_current_limit_a"}
    generic_fields = {
        "id",
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "port_map",
        "characterization",
        *numeric_fields,
    }
    _exact_keys(value, generic_fields | identity_fields, "selector")
    normalized = _normalize_rated_asset(
        {field: value[field] for field in generic_fields},
        label="selector",
        port_names=("common", *ANTENNA_PORTS),
        extra_numeric_fields=tuple(sorted(numeric_fields)),
        base_directory=base_directory,
        verify_files=verify_files,
    )
    if normalized["supply_voltage_v"] <= 0 or normalized["supply_current_limit_a"] <= 0:
        raise FixtureV2Error("selector bench-supply voltage/current limit must be positive")
    normalized.update(
        {field: _identifier(value[field], f"selector {field}") for field in sorted(identity_fields)}
    )
    return normalized


def _normalize_eight_way_splitter(
    value: object,
    *,
    base_directory: Path | None,
    verify_files: bool,
) -> dict[str, Any]:
    return _normalize_rated_asset(
        value,
        label="eight-way splitter",
        port_names=("input", *ANTENNA_PORTS),
        base_directory=base_directory,
        verify_files=verify_files,
    )


def normalize_stage_delta(
    value: object,
    *,
    stage: str,
    shared_fixture: Mapping[str, Any],
    base_directory: Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    """Normalize the exact stage-specific A/B/C/E graph."""

    if stage not in PRIOR_STAGE:
        raise FixtureV2Error(f"unsupported topology stage: {stage}")
    if not isinstance(value, Mapping):
        raise FixtureV2Error("stage-specific fixture delta must be an object")
    _exact_keys(
        value,
        {
            "schema",
            "delta_id",
            "selector_rf_state",
            "selector_power_state",
            "selector_control_harness_state",
            "components",
            "connections",
        },
        "stage-specific fixture delta",
    )
    if value["schema"] != 1:
        raise FixtureV2Error("stage-specific fixture delta schema is invalid")
    selector_connected = stage in SELECTOR_CONNECTED_STAGES
    expected_rf = "rf_connected" if selector_connected else "rf_disconnected"
    expected_power = "bench_power_on" if selector_connected else "bench_power_off"
    expected_harness = "connected_static_all_off" if selector_connected else "disconnected"
    if (
        value["selector_rf_state"] != expected_rf
        or value["selector_power_state"] != expected_power
        or value["selector_control_harness_state"] != expected_harness
    ):
        raise FixtureV2Error(
            "stage selector RF, bench-power, or control-harness state differs from the "
            "topology contract"
        )
    components = value["components"]
    connections = value["connections"]
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise FixtureV2Error("stage components and connections must be objects")
    splitter = shared_fixture["tx1_reference_splitter"]
    splitter_id = str(splitter["id"])
    stimulus_port = str(splitter["port_map"]["stimulus_branch"])
    rx2_endpoint = _rx2_fixture_endpoint(shared_fixture)

    if stage in {"direct_rx2_termination", "rx2_cable_terminated"}:
        if set(components) != {"tx1_stimulus_termination", "rx2_termination"}:
            raise FixtureV2Error("Stage A/B components are incomplete or unexpected")
        rx2_role = (
            "rx2_to_direct_termination"
            if stage == "direct_rx2_termination"
            else "rx2_to_far_end_termination"
        )
        expected_connections = {"splitter_stimulus_to_termination", rx2_role}
        if set(connections) != expected_connections:
            raise FixtureV2Error("Stage A/B connection graph is incomplete or unexpected")
        normalized_components = {
            "tx1_stimulus_termination": _normalize_load(
                components["tx1_stimulus_termination"],
                label="TX1 stimulus-branch termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "rx2_termination": _normalize_load(
                components["rx2_termination"],
                label="RX2 termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
        }
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        stimulus_load = normalized_components["tx1_stimulus_termination"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_termination"],
            source=(splitter_id, stimulus_port),
            destination=(
                str(stimulus_load["id"]),
                str(stimulus_load["port_map"]["load"]),
            ),
            label="TX1 stimulus-branch termination connection",
        )
        rx2_load = normalized_components["rx2_termination"]
        _require_connection(
            normalized_connections[rx2_role],
            source=rx2_endpoint,
            destination=(str(rx2_load["id"]), str(rx2_load["port_map"]["load"])),
            label="RX2 termination connection",
            required_kind=(
                "direct_adapter" if stage == "direct_rx2_termination" else "coaxial_cable"
            ),
        )
    elif stage == "powered_selector_all_inputs_terminated":
        if set(components) != {
            "tx1_stimulus_termination",
            "selector",
            "selector_input_terminations",
        }:
            raise FixtureV2Error("Stage C components are incomplete or unexpected")
        loads = components["selector_input_terminations"]
        if not isinstance(loads, Mapping) or set(loads) != set(ANTENNA_PORTS):
            raise FixtureV2Error("Stage C requires eight individually identified selector loads")
        normalized_loads = {
            ant: _normalize_load(
                loads[ant],
                label=f"selector {ant} termination",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for ant in ANTENNA_PORTS
        }
        normalized_components = {
            "tx1_stimulus_termination": _normalize_load(
                components["tx1_stimulus_termination"],
                label="TX1 stimulus-branch termination",
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector": _normalize_selector(
                components["selector"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector_input_terminations": normalized_loads,
        }
        expected_connections = {
            "splitter_stimulus_to_termination",
            "rx2_to_selector_common",
            *(f"selector_{ant.lower()}_to_termination" for ant in ANTENNA_PORTS),
        }
        if set(connections) != expected_connections:
            raise FixtureV2Error("Stage C connection graph is incomplete or unexpected")
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        stimulus_load = normalized_components["tx1_stimulus_termination"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_termination"],
            source=(splitter_id, stimulus_port),
            destination=(
                str(stimulus_load["id"]),
                str(stimulus_load["port_map"]["load"]),
            ),
            label="TX1 stimulus termination connection",
        )
        selector = normalized_components["selector"]
        _require_connection(
            normalized_connections["rx2_to_selector_common"],
            source=rx2_endpoint,
            destination=(str(selector["id"]), str(selector["port_map"]["common"])),
            label="RX2-to-selector-common connection",
            required_kind="coaxial_cable",
        )
        for ant in ANTENNA_PORTS:
            load = normalized_loads[ant]
            _require_connection(
                normalized_connections[f"selector_{ant.lower()}_to_termination"],
                source=(str(selector["id"]), str(selector["port_map"][ant])),
                destination=(str(load["id"]), str(load["port_map"]["load"])),
                label=f"selector {ant} termination connection",
            )
    else:
        if set(components) != {"eight_way_splitter", "selector"}:
            raise FixtureV2Error("Stage E components are incomplete or unexpected")
        normalized_components = {
            "eight_way_splitter": _normalize_eight_way_splitter(
                components["eight_way_splitter"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
            "selector": _normalize_selector(
                components["selector"],
                base_directory=base_directory,
                verify_files=verify_files,
            ),
        }
        expected_connections = {
            "splitter_stimulus_to_eight_way",
            "rx2_to_selector_common",
            *(f"eight_way_{ant.lower()}_to_selector_{ant.lower()}" for ant in ANTENNA_PORTS),
        }
        if set(connections) != expected_connections:
            raise FixtureV2Error("Stage E connection graph is incomplete or unexpected")
        normalized_connections = {
            role: _normalize_connection(
                connections[role],
                label=f"stage {role}",
                base_directory=base_directory,
                verify_files=verify_files,
            )
            for role in expected_connections
        }
        eight_way = normalized_components["eight_way_splitter"]
        selector = normalized_components["selector"]
        _require_connection(
            normalized_connections["splitter_stimulus_to_eight_way"],
            source=(splitter_id, stimulus_port),
            destination=(str(eight_way["id"]), str(eight_way["port_map"]["input"])),
            label="TX1 stimulus-to-eight-way connection",
        )
        _require_connection(
            normalized_connections["rx2_to_selector_common"],
            source=rx2_endpoint,
            destination=(str(selector["id"]), str(selector["port_map"]["common"])),
            label="RX2-to-selector-common connection",
            required_kind="coaxial_cable",
        )
        for ant in ANTENNA_PORTS:
            _require_connection(
                normalized_connections[f"eight_way_{ant.lower()}_to_selector_{ant.lower()}"],
                source=(str(eight_way["id"]), str(eight_way["port_map"][ant])),
                destination=(str(selector["id"]), str(selector["port_map"][ant])),
                label=f"eight-way {ant} feed connection",
                required_kind="coaxial_cable",
            )
    return {
        "schema": 1,
        "delta_id": _identifier(value["delta_id"], "stage delta ID"),
        "selector_rf_state": expected_rf,
        "selector_power_state": expected_power,
        "selector_control_harness_state": expected_harness,
        "components": normalized_components,
        "connections": normalized_connections,
    }


def fixture_identity_sets(
    shared_fixture: Mapping[str, Any], stage_delta: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Derive the globally unique component/interconnect and connection inventories."""

    component_ids: list[str] = []
    connection_ids: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            identity = item.get("id")
            if {"id", "from", "to", "interconnect"}.issubset(item):
                connection_ids.append(_identifier(identity, "fixture connection ID"))
            elif isinstance(identity, str):
                component_ids.append(_identifier(identity, "fixture component/interconnect ID"))
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(shared_fixture)
    visit(stage_delta)
    if len(component_ids) != len(set(component_ids)):
        raise FixtureV2Error("fixture component/interconnect IDs must be globally unique")
    if len(connection_ids) != len(set(connection_ids)):
        raise FixtureV2Error("fixture connection IDs must be globally unique")
    return sorted(component_ids), sorted(connection_ids)


def characterization_summary(
    shared_fixture: Mapping[str, Any],
    stage_delta: Mapping[str, Any],
    *,
    prior_characterized: bool,
) -> dict[str, Any]:
    """Project the runner's fixed characterization eligibility summary."""

    characterized: list[str] = []
    uncharacterized: list[str] = []

    def visit(item: object, identity: str | None = None) -> None:
        if isinstance(item, Mapping):
            current = str(item.get("id")) if isinstance(item.get("id"), str) else identity
            characterization = item.get("characterization")
            if isinstance(characterization, Mapping) and current is not None:
                target = (
                    characterized
                    if characterization.get("status") == "characterized"
                    else uncharacterized
                )
                target.append(current)
            for nested in item.values():
                visit(nested, current)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested, identity)

    visit(shared_fixture)
    visit(stage_delta)
    all_current = not uncharacterized and bool(characterized)
    return {
        "characterized_asset_ids": sorted(set(characterized)),
        "uncharacterized_asset_ids": sorted(set(uncharacterized)),
        "all_current_stage_assets_characterized": all_current,
        "prior_stage_fixture_characterized": prior_characterized,
        "causal_attribution_fixture_eligible": all_current and prior_characterized,
        "screening_capture_allowed_when_uncharacterized": True,
        "causal_attribution_claim": False,
    }


def _rx2_connection_without_far_endpoint(connection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": connection["id"],
        "from": connection["from"],
        "interconnect": connection["interconnect"],
    }


def _current_comparison_invariants(stage: str, stage_delta: Mapping[str, Any]) -> dict[str, Any]:
    components = stage_delta.get("components")
    connections = stage_delta.get("connections")
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise FixtureV2Error("comparison-anchor stage delta is malformed")
    if stage == "rx2_cable_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_termination": components["rx2_termination"],
        }
    if stage == "powered_selector_all_inputs_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_common_cable_without_far_endpoint": _rx2_connection_without_far_endpoint(
                connections["rx2_to_selector_common"]
            ),
        }
    if stage == FULL_CONDUCTED_STAGE:
        return {
            "selector": components["selector"],
            "rx2_to_selector_common": connections["rx2_to_selector_common"],
        }
    raise FixtureV2Error("Stage A has no prior-stage comparison anchor")


def _prior_comparison_invariants(stage: str, prior_delta: Mapping[str, Any]) -> dict[str, Any]:
    components = prior_delta.get("components")
    connections = prior_delta.get("connections")
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise FixtureV2Error("prior-stage delta is malformed")
    if stage == "rx2_cable_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_termination": components["rx2_termination"],
        }
    if stage == "powered_selector_all_inputs_terminated":
        return {
            "tx1_stimulus_termination": components["tx1_stimulus_termination"],
            "splitter_stimulus_to_termination": connections["splitter_stimulus_to_termination"],
            "rx2_common_cable_without_far_endpoint": _rx2_connection_without_far_endpoint(
                connections["rx2_to_far_end_termination"]
            ),
        }
    if stage == FULL_CONDUCTED_STAGE:
        return {
            "selector": components["selector"],
            "rx2_to_selector_common": connections["rx2_to_selector_common"],
        }
    raise FixtureV2Error("Stage A has no prior-stage comparison anchor")


def _comparison_anchor_from_fixture_chain(
    *,
    stage: str,
    prior_fixture: Mapping[str, Any],
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any]:
    expected_prior = PRIOR_STAGE[stage]
    prior_delta = prior_fixture.get("stage_delta")
    if expected_prior is None or prior_fixture.get("stage") != expected_prior:
        raise FixtureV2Error("comparison anchor does not use the immediate prior stage")
    if not isinstance(prior_delta, Mapping):
        raise FixtureV2Error("prior-stage fixture lacks its stage delta")
    prior_delta_sha = _digest(prior_fixture.get("stage_delta_sha256"), "prior stage-delta hash")
    if canonical_sha256(prior_delta) != prior_delta_sha:
        raise FixtureV2Error("prior-stage delta differs from its frozen hash")
    current = _current_comparison_invariants(stage, current_stage_delta)
    prior = _prior_comparison_invariants(stage, prior_delta)
    if current != prior:
        raise FixtureV2Error(
            "current stage substituted a comparison-anchor load, cable, selector, or power identity"
        )
    return {
        "schema": 1,
        "from_stage": expected_prior,
        "to_stage": stage,
        "prior_stage_delta_sha256": prior_delta_sha,
        "preserved_assets": current,
    }


def _validate_frozen_comparison_anchor(
    value: object,
    *,
    stage: str,
    prior_stage_delta_sha256: str,
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureV2Error("prior-stage comparison anchor must be an object")
    document = dict(value)
    _exact_keys(
        document,
        {
            "schema",
            "from_stage",
            "to_stage",
            "prior_stage_delta_sha256",
            "preserved_assets",
        },
        "prior-stage comparison anchor",
    )
    if (
        document["schema"] != 1
        or document["from_stage"] != PRIOR_STAGE[stage]
        or document["to_stage"] != stage
        or document["prior_stage_delta_sha256"] != prior_stage_delta_sha256
        or document["preserved_assets"]
        != _current_comparison_invariants(stage, current_stage_delta)
    ):
        raise FixtureV2Error("prior-stage comparison anchor differs from the current fixture")
    return document


def _validate_frozen_prior_stage_binding(
    value: object,
    *,
    stage: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    shared_fixture_sha256: str,
    current_stage_delta: Mapping[str, Any],
) -> dict[str, Any] | None:
    expected_stage = PRIOR_STAGE[stage]
    if expected_stage is None:
        if value is not None:
            raise FixtureV2Error("Stage A must not bind a prior-stage plan")
        return None
    if not isinstance(value, Mapping):
        raise FixtureV2Error("prior-stage binding must be an object")
    document = dict(value)
    _exact_keys(
        document,
        {
            "stage",
            "run_id",
            "plan_path",
            "plan_file_sha256",
            "plan_contract_sha256",
            "fixture_evidence_sha256",
            "shared_fixture_sha256",
            "prior_stage_delta_sha256",
            "comparison_anchor",
            "comparison_anchor_sha256",
            "prior_selector_control_sha256",
            "campaign_id",
            "comparable_fixture_group_id",
            "prior_fixture_characterized",
        },
        "prior-stage binding",
    )
    if (
        document["stage"] != expected_stage
        or document["campaign_id"] != campaign_id
        or document["comparable_fixture_group_id"] != comparable_fixture_group_id
        or document["shared_fixture_sha256"] != shared_fixture_sha256
        or not isinstance(document["prior_fixture_characterized"], bool)
    ):
        raise FixtureV2Error("prior-stage binding differs from the comparable fixture chain")
    document["run_id"] = _identifier(document["run_id"], "prior run ID")
    plan_path = document["plan_path"]
    if not isinstance(plan_path, str) or not Path(plan_path).is_absolute():
        raise FixtureV2Error("prior-stage plan path must be absolute")
    document["plan_path"] = str(Path(plan_path).expanduser().absolute())
    for field in (
        "plan_file_sha256",
        "plan_contract_sha256",
        "fixture_evidence_sha256",
        "shared_fixture_sha256",
        "prior_stage_delta_sha256",
        "comparison_anchor_sha256",
    ):
        document[field] = _digest(document[field], f"prior-stage {field}")
    prior_selector_sha = document["prior_selector_control_sha256"]
    if expected_stage in SELECTOR_CONNECTED_STAGES:
        document["prior_selector_control_sha256"] = _digest(
            prior_selector_sha, "prior-stage selector-control hash"
        )
    elif prior_selector_sha is not None:
        raise FixtureV2Error("selector-disconnected prior stage must not bind selector control")
    document["comparison_anchor"] = _validate_frozen_comparison_anchor(
        document["comparison_anchor"],
        stage=stage,
        prior_stage_delta_sha256=document["prior_stage_delta_sha256"],
        current_stage_delta=current_stage_delta,
    )
    if canonical_sha256(document["comparison_anchor"]) != document["comparison_anchor_sha256"]:
        raise FixtureV2Error("prior-stage comparison-anchor hash is inconsistent")
    return document


def _source_prior_stage_binding_from_plan(
    value: object,
    *,
    stage: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    shared_fixture_sha256: str,
    current_stage_delta: Mapping[str, Any],
    board_id: str,
    serial: str,
    base_directory: Path,
    verify_files: bool,
    verify_selector_file: bool,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    expected_stage = PRIOR_STAGE[stage]
    if expected_stage is None:
        if value is not None:
            raise FixtureV2Error("Stage A fixture manifest must use null prior_stage_binding")
        return None, None, None
    if not isinstance(value, Mapping):
        raise FixtureV2Error("prior-stage fixture binding must be an object")
    raw_binding = dict(value)
    _exact_keys(
        raw_binding,
        {
            "stage",
            "run_id",
            "plan_path",
            "plan_file_sha256",
            "plan_contract_sha256",
            "fixture_evidence_sha256",
        },
        "prior-stage fixture binding",
    )
    if raw_binding["stage"] != expected_stage:
        raise FixtureV2Error(f"{stage} must bind the immediately prior {expected_stage} plan")
    raw_path = Path(str(raw_binding["plan_path"])).expanduser()
    plan_path = raw_path if raw_path.is_absolute() else base_directory / raw_path
    plan_document, plan_path = _read_json(plan_path, "prior-stage immutable plan")
    declared_file_sha = _digest(raw_binding["plan_file_sha256"], "prior plan file hash")
    if sha256_path(plan_path) != declared_file_sha:
        raise FixtureV2Error("prior-stage plan file differs from its declared hash")
    _exact_keys(
        plan_document,
        {
            "schema",
            "plan_contract",
            "plan_contract_sha256",
            "plan_contract_hash_provenance",
            "immutable",
        },
        "prior-stage immutable plan envelope",
    )
    contract = plan_document["plan_contract"]
    if not isinstance(contract, Mapping):
        raise FixtureV2Error("prior-stage immutable plan contract is malformed")
    if (
        plan_document["schema"] != 1
        or plan_document["immutable"] is not True
        or plan_document["plan_contract_sha256"] != canonical_sha256(contract)
        or plan_document["plan_contract_hash_provenance"]
        != "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
    ):
        raise FixtureV2Error("prior-stage immutable plan envelope is invalid")
    declared_contract_sha = _digest(raw_binding["plan_contract_sha256"], "prior plan contract hash")
    if declared_contract_sha != plan_document["plan_contract_sha256"]:
        raise FixtureV2Error("prior-stage contract hash differs from its immutable plan")
    prior_fixture = contract.get("fixture_evidence")
    declared_fixture_sha = _digest(
        raw_binding["fixture_evidence_sha256"], "prior fixture evidence hash"
    )
    configuration = contract.get("configuration")
    if not isinstance(prior_fixture, Mapping) or not isinstance(configuration, Mapping):
        raise FixtureV2Error("prior-stage plan is missing fixture/configuration evidence")
    if (
        contract.get("topology_stage") != expected_stage
        or contract.get("run_id") != raw_binding["run_id"]
        or contract.get("board_id") != board_id
        or configuration.get("serial") != serial
        or contract.get("fixture_evidence_sha256") != declared_fixture_sha
        or canonical_sha256(prior_fixture) != declared_fixture_sha
    ):
        raise FixtureV2Error("prior-stage plan is not comparable with this fixture")
    validated_prior = validate_fixture_evidence(
        prior_fixture,
        expected_stage=expected_stage,
        expected_run_id=_identifier(raw_binding["run_id"], "prior run ID"),
        expected_board_id=board_id,
        expected_serial=serial,
        verify_files=verify_files,
        verify_selector_file=verify_selector_file,
    )
    if (
        validated_prior["campaign_id"] != campaign_id
        or validated_prior["comparable_fixture_group_id"] != comparable_fixture_group_id
        or validated_prior["shared_fixture_sha256"] != shared_fixture_sha256
    ):
        raise FixtureV2Error("prior-stage plan is not comparable with this fixture")
    comparison_anchor = _comparison_anchor_from_fixture_chain(
        stage=stage,
        prior_fixture=validated_prior,
        current_stage_delta=current_stage_delta,
    )
    prior_selector_control = contract.get("selector_control")
    if expected_stage in SELECTOR_CONNECTED_STAGES:
        if not isinstance(prior_selector_control, Mapping):
            raise FixtureV2Error("prior selector-connected plan lacks selector control")
        prior_selector_flash = validate_selector_flash_binding(
            prior_selector_control.get("selector_flash_evidence"),
            expected_campaign_id=campaign_id,
            expected_board_id=board_id,
            expected_image_role="bench",
            verify_file=verify_selector_file,
        )
        if prior_selector_flash != validated_prior["selector_flash_evidence"]:
            raise FixtureV2Error("prior selector control differs from its exact fixture evidence")
        prior_selector_sha: str | None = canonical_sha256(prior_selector_control)
    else:
        if prior_selector_control is not None:
            raise FixtureV2Error("prior selector-disconnected plan contains selector control")
        prior_selector_flash = None
        prior_selector_sha = None
    normalized = {
        "stage": expected_stage,
        "run_id": _identifier(raw_binding["run_id"], "prior run ID"),
        "plan_path": str(plan_path),
        "plan_file_sha256": declared_file_sha,
        "plan_contract_sha256": declared_contract_sha,
        "fixture_evidence_sha256": declared_fixture_sha,
        "shared_fixture_sha256": shared_fixture_sha256,
        "prior_stage_delta_sha256": comparison_anchor["prior_stage_delta_sha256"],
        "comparison_anchor": comparison_anchor,
        "comparison_anchor_sha256": canonical_sha256(comparison_anchor),
        "prior_selector_control_sha256": prior_selector_sha,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": comparable_fixture_group_id,
        "prior_fixture_characterized": bool(
            validated_prior["characterization_summary"].get("causal_attribution_fixture_eligible")
        ),
    }
    checked = _validate_frozen_prior_stage_binding(
        normalized,
        stage=stage,
        campaign_id=campaign_id,
        comparable_fixture_group_id=comparable_fixture_group_id,
        shared_fixture_sha256=shared_fixture_sha256,
        current_stage_delta=current_stage_delta,
    )
    if checked is None:  # pragma: no cover - non-A stages always return a binding
        raise FixtureV2Error("non-A prior-stage binding unexpectedly normalized to null")
    return raw_binding, checked, prior_selector_flash


def validate_fixture_manifest(
    path: Path,
    *,
    expected_stage: str,
    expected_board_id: str,
    expected_serial: str,
    verify_files: bool = True,
    verify_selector_file: bool = True,
) -> ValidatedFixtureManifest:
    """Reopen and fully validate one production fixture-v2 source manifest."""

    if expected_stage not in PRIOR_STAGE:
        raise FixtureV2Error(f"unsupported topology stage: {expected_stage}")
    document, exact_path = _read_json(path, "fixture manifest v2")
    _exact_keys(document, _RAW_MANIFEST_FIELDS, "fixture manifest v2")
    if (
        document["schema"] != 2
        or document["fixture_kind"] != FIXTURE_KIND_V2
        or document["stage"] != expected_stage
        or document["board_id"] != expected_board_id
    ):
        raise FixtureV2Error("fixture manifest v2 identity differs from the requested fixture")
    campaign_id = _identifier(document["campaign_id"], "campaign ID")
    group_id = _identifier(document["comparable_fixture_group_id"], "comparison group ID")
    board_id = _identifier(document["board_id"], "board ID")
    serial = _identifier(expected_serial, "Pluto serial")
    shared = normalize_shared_fixture(
        document["shared_fixture"],
        expected_serial=serial,
        base_directory=exact_path.parent,
        verify_files=verify_files,
    )
    delta = normalize_stage_delta(
        document["stage_delta"],
        stage=expected_stage,
        shared_fixture=shared,
        base_directory=exact_path.parent,
        verify_files=verify_files,
    )
    shared_sha = canonical_sha256(shared)
    delta_sha = canonical_sha256(delta)
    source_prior, prior, prior_selector_flash = _source_prior_stage_binding_from_plan(
        document["prior_stage_binding"],
        stage=expected_stage,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        shared_fixture_sha256=shared_sha,
        current_stage_delta=delta,
        board_id=board_id,
        serial=serial,
        base_directory=exact_path.parent,
        verify_files=verify_files,
        verify_selector_file=verify_selector_file,
    )
    component_ids, connection_ids = fixture_identity_sets(shared, delta)
    prior_characterized = prior is None or bool(prior["prior_fixture_characterized"])
    return ValidatedFixtureManifest(
        path=exact_path,
        file_sha256=sha256_path(exact_path),
        size_bytes=exact_path.stat().st_size,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        stage=expected_stage,
        board_id=board_id,
        serial=serial,
        shared_fixture=shared,
        shared_fixture_sha256=shared_sha,
        stage_delta=delta,
        stage_delta_sha256=delta_sha,
        source_prior_stage_binding=source_prior,
        prior_stage_binding=prior,
        prior_selector_flash_evidence=prior_selector_flash,
        component_ids=tuple(component_ids),
        connection_ids=tuple(connection_ids),
        characterization_summary=characterization_summary(
            shared,
            delta,
            prior_characterized=prior_characterized,
        ),
    )


def validate_setup_attestation(
    path: Path,
    *,
    run_id: str,
    campaign_id: str,
    comparable_fixture_group_id: str,
    stage: str,
    fixture_manifest_sha256: str,
    shared_fixture_sha256: str,
    stage_delta_sha256: str,
    component_ids: Sequence[str],
    connection_ids: Sequence[str],
    selector_flash_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reopen a raw setup attestation and derive its normalized runner projection."""

    document, exact_path = _read_json(path, "per-run setup attestation")
    _exact_keys(document, _RAW_SETUP_FIELDS, "per-run setup attestation")
    created_at = _timestamp(document["created_at"], "setup attestation created_at")
    expected_selector_summary = (
        None
        if selector_flash_evidence is None
        else {
            "path": selector_flash_evidence["path"],
            "sha256": selector_flash_evidence["sha256"],
            "run_id": selector_flash_evidence["run_id"],
        }
    )
    expected_components = list(component_ids)
    expected_connections = list(connection_ids)
    if (
        document["schema"] != 1
        or document["attestation_kind"] != SETUP_ATTESTATION_KIND
        or document["run_id"] != run_id
        or document["campaign_id"] != campaign_id
        or document["comparable_fixture_group_id"] != comparable_fixture_group_id
        or document["stage"] != stage
        or document["fixture_manifest_sha256"] != fixture_manifest_sha256
        or document["shared_fixture_sha256"] != shared_fixture_sha256
        or document["stage_delta_sha256"] != stage_delta_sha256
        or document["observed_component_ids"] != expected_components
        or document["observed_connection_ids"] != expected_connections
        or document["selector_flash_evidence"] != expected_selector_summary
    ):
        raise FixtureV2Error("per-run setup attestation is not bound to this exact fixture")
    attestation_id = _identifier(document["attestation_id"], "setup attestation ID")
    evidence_path = Path(str(document["setup_evidence_path"])).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = exact_path.parent / evidence_path
    evidence_path = evidence_path.absolute()
    evidence_sha = _digest(document["setup_evidence_sha256"], "setup evidence hash")
    evidence_path = _regular_file(evidence_path, "setup evidence")
    if sha256_path(evidence_path) != evidence_sha:
        raise FixtureV2Error("setup evidence differs from its declared hash")
    return {
        "schema": 1,
        "attestation_kind": SETUP_ATTESTATION_KIND,
        "attestation_id": attestation_id,
        "created_at": created_at,
        "created_at_wall_clock_freshness_enforced": False,
        "run_id": _identifier(run_id, "setup run ID"),
        "campaign_id": _identifier(campaign_id, "setup campaign ID"),
        "comparable_fixture_group_id": _identifier(
            comparable_fixture_group_id, "setup comparison group ID"
        ),
        "stage": stage,
        "fixture_manifest_sha256": _digest(fixture_manifest_sha256, "setup fixture manifest hash"),
        "shared_fixture_sha256": _digest(shared_fixture_sha256, "setup shared fixture hash"),
        "stage_delta_sha256": _digest(stage_delta_sha256, "setup stage delta hash"),
        "observed_component_ids": expected_components,
        "observed_connection_ids": expected_connections,
        "selector_flash_evidence": (
            None if selector_flash_evidence is None else dict(selector_flash_evidence)
        ),
        "setup_evidence": {
            "path": str(evidence_path),
            "sha256": evidence_sha,
            "size_bytes": evidence_path.stat().st_size,
        },
        "setup_attestation_file": {
            "path": str(exact_path),
            "sha256": sha256_path(exact_path),
            "size_bytes": exact_path.stat().st_size,
        },
    }


def validate_fixture_evidence(
    value: object,
    *,
    expected_stage: str,
    expected_run_id: str,
    expected_board_id: str,
    expected_serial: str,
    verify_files: bool = True,
    verify_selector_file: bool = True,
) -> dict[str, Any]:
    """Reproduce and validate the complete normalized fixture-evidence authority."""

    if not isinstance(value, Mapping):
        raise FixtureV2Error("fixture-evidence v2 must be an object")
    document = dict(value)
    _exact_keys(document, _FIXTURE_EVIDENCE_FIELDS, "fixture-evidence v2")
    if (
        document["schema"] != 2
        or document["fixture_kind"] != FIXTURE_KIND_V2
        or document["stage"] != expected_stage
        or document["run_id"] != expected_run_id
        or document["board_id"] != expected_board_id
    ):
        raise FixtureV2Error("fixture-evidence v2 identity differs from the plan")
    campaign_id = _identifier(document["campaign_id"], "campaign ID")
    group_id = _identifier(document["comparable_fixture_group_id"], "comparison group ID")
    run_id = _identifier(document["run_id"], "fixture run ID")
    board_id = _identifier(document["board_id"], "fixture board ID")
    source_files = document["source_files"]
    if not isinstance(source_files, Mapping):
        raise FixtureV2Error("fixture source-file evidence must be an object")
    _exact_keys(
        source_files,
        {"fixture_manifest", "setup_attestation"},
        "fixture source-file evidence",
    )
    manifest_file = _file_evidence(
        source_files["fixture_manifest"], "fixture manifest", verify_file=True
    )
    setup_file = _file_evidence(
        source_files["setup_attestation"], "setup attestation", verify_file=True
    )
    manifest = validate_fixture_manifest(
        Path(manifest_file["path"]),
        expected_stage=expected_stage,
        expected_board_id=board_id,
        expected_serial=expected_serial,
        verify_files=verify_files,
        verify_selector_file=verify_selector_file,
    )
    expected_manifest_file = {
        "path": str(manifest.path),
        "sha256": manifest.file_sha256,
        "size_bytes": manifest.size_bytes,
    }
    if manifest_file != expected_manifest_file:
        raise FixtureV2Error("fixture manifest source binding differs from the reopened file")
    if manifest.campaign_id != campaign_id or manifest.comparable_fixture_group_id != group_id:
        raise FixtureV2Error("fixture manifest campaign/group differs from normalized evidence")
    if expected_stage in SELECTOR_CONNECTED_STAGES:
        selector_flash = validate_selector_flash_binding(
            document["selector_flash_evidence"],
            expected_campaign_id=campaign_id,
            expected_board_id=board_id,
            expected_image_role="bench",
            verify_file=verify_selector_file,
        )
    elif document["selector_flash_evidence"] is not None:
        raise FixtureV2Error("selector-disconnected fixture must not bind selector-flash evidence")
    else:
        selector_flash = None
    if (
        expected_stage == FULL_CONDUCTED_STAGE
        and selector_flash != manifest.prior_selector_flash_evidence
    ):
        raise FixtureV2Error(
            "full fixture selector evidence differs from the immediately prior Stage-C plan"
        )
    setup = validate_setup_attestation(
        Path(setup_file["path"]),
        run_id=run_id,
        campaign_id=campaign_id,
        comparable_fixture_group_id=group_id,
        stage=expected_stage,
        fixture_manifest_sha256=manifest.file_sha256,
        shared_fixture_sha256=manifest.shared_fixture_sha256,
        stage_delta_sha256=manifest.stage_delta_sha256,
        component_ids=manifest.component_ids,
        connection_ids=manifest.connection_ids,
        selector_flash_evidence=selector_flash,
    )
    if setup["setup_attestation_file"] != setup_file:
        raise FixtureV2Error("setup-attestation file evidence differs from source binding")
    expected = {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND_V2,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": group_id,
        "stage": expected_stage,
        "run_id": run_id,
        "board_id": board_id,
        "source_files": {
            "fixture_manifest": expected_manifest_file,
            "setup_attestation": setup_file,
        },
        "shared_fixture": manifest.shared_fixture,
        "shared_fixture_sha256": manifest.shared_fixture_sha256,
        "stage_delta": manifest.stage_delta,
        "stage_delta_sha256": manifest.stage_delta_sha256,
        "prior_stage_binding": manifest.prior_stage_binding,
        "setup_attestation": setup,
        "selector_flash_evidence": selector_flash,
        "component_ids": list(manifest.component_ids),
        "connection_ids": list(manifest.connection_ids),
        "characterization_summary": manifest.characterization_summary,
    }
    if document != expected:
        raise FixtureV2Error(
            "fixture-evidence v2 differs from its exact source-derived production projection"
        )
    normalized_setup = document["setup_attestation"]
    if not isinstance(normalized_setup, Mapping):
        raise FixtureV2Error("normalized setup attestation must be an object")
    _exact_keys(normalized_setup, _NORMALIZED_SETUP_FIELDS, "normalized setup attestation")
    return expected


def validate_x_capture_linkage(
    fixture_evidence: object,
    *,
    capture_fixture_binding: Mapping[str, Any],
    context_selector_flash_evidence: object,
    verify_files: bool = True,
    verify_selector_file: bool = True,
) -> dict[str, Any]:
    """Bind one X topology fixture to its exact full-E capture-state fixture.

    The capture binding is the selected-state full-fixture wrapper (or any
    mapping carrying the same five authoritative fields).  The function
    independently reopens both fixture sources; it never trusts a topology
    graph merely because it carries the expected component ID or changed leaf.
    """

    if not isinstance(fixture_evidence, Mapping):
        raise FixtureV2Error("X topology fixture evidence must be an object")
    stage = fixture_evidence.get("stage")
    if not isinstance(stage, str) or stage not in PRIOR_STAGE:
        raise FixtureV2Error("X topology stage is unsupported")
    run_id = _identifier(fixture_evidence.get("run_id"), "X topology run ID")
    board_id = _identifier(capture_fixture_binding.get("board_id"), "capture fixture board ID")
    serial = _identifier(
        capture_fixture_binding.get("pluto_serial"), "capture fixture Pluto serial"
    )
    group_id = _identifier(capture_fixture_binding.get("fixture_id"), "capture fixture group ID")
    capture_path_value = capture_fixture_binding.get("fixture_manifest_path")
    if not isinstance(capture_path_value, str) or not Path(capture_path_value).is_absolute():
        raise FixtureV2Error("capture fixture manifest path must be absolute")
    capture_manifest = validate_fixture_manifest(
        Path(capture_path_value),
        expected_stage=FULL_CONDUCTED_STAGE,
        expected_board_id=board_id,
        expected_serial=serial,
        verify_files=verify_files,
        verify_selector_file=verify_selector_file,
    )
    capture_hash = _digest(
        capture_fixture_binding.get("fixture_manifest_sha256"),
        "capture fixture manifest hash",
    )
    if (
        capture_hash != capture_manifest.file_sha256
        or capture_manifest.comparable_fixture_group_id != group_id
    ):
        raise FixtureV2Error("capture fixture binding differs from its full-E manifest")
    normalized = validate_fixture_evidence(
        fixture_evidence,
        expected_stage=stage,
        expected_run_id=run_id,
        expected_board_id=board_id,
        expected_serial=serial,
        verify_files=verify_files,
        verify_selector_file=verify_selector_file,
    )
    if (
        normalized["campaign_id"] != capture_manifest.campaign_id
        or normalized["comparable_fixture_group_id"] != group_id
    ):
        raise FixtureV2Error("X topology fixture campaign/group differs from capture state")
    topology_pluto = normalized["shared_fixture"]["pluto"]
    if (
        topology_pluto["serial"] != serial
        or normalized["shared_fixture"] != capture_manifest.shared_fixture
    ):
        raise FixtureV2Error(
            "X topology fixture does not share the exact capture-state physical graph"
        )
    context_selector = validate_selector_flash_binding(
        context_selector_flash_evidence,
        expected_campaign_id=normalized["campaign_id"],
        expected_board_id=board_id,
        expected_image_role="bench",
        verify_file=verify_selector_file,
    )
    fixture_selector = normalized["selector_flash_evidence"]
    if stage in SELECTOR_CONNECTED_STAGES:
        if fixture_selector != context_selector:
            raise FixtureV2Error(
                "X connected topology selector evidence differs from capture context"
            )
    elif fixture_selector is not None:
        raise FixtureV2Error("X disconnected topology unexpectedly binds live selector control")
    components = normalized["stage_delta"]["components"]
    connections = normalized["stage_delta"]["connections"]
    capture_components = capture_manifest.stage_delta["components"]
    capture_connections = capture_manifest.stage_delta["connections"]
    if stage == "powered_selector_all_inputs_terminated" and (
        components["selector"] != capture_components["selector"]
        or connections["rx2_to_selector_common"] != capture_connections["rx2_to_selector_common"]
    ):
        raise FixtureV2Error(
            "Stage-C X fixture does not match its associated full-fixture selector boundary"
        )
    if stage == FULL_CONDUCTED_STAGE:
        expected_source = {
            "path": str(capture_manifest.path),
            "sha256": capture_manifest.file_sha256,
            "size_bytes": capture_manifest.size_bytes,
        }
        if normalized["source_files"]["fixture_manifest"] != expected_source:
            raise FixtureV2Error(
                "full-fixture X role must use its capture-revision manifest as fixture source"
            )
    return normalized
