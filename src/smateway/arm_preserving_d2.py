"""Fail-closed contracts for the preferred 5.8-GHz arm-preserving D2 fixture.

This module has no hardware or filesystem side effects.  It freezes the physical
fixture, derives topology identities accepted by :mod:`smateway.closure_qualification`,
validates one normalized C_i or D2_i observation, and assembles the exact
eight-arm, five-repeat cohorts used by that model.

The current passive 8-way fixture deliberately remains diagnostic-only.  Driving
one splitter input while terminating its other ports does not characterize the
splitter's simultaneous multiport interaction.  The limitation is carried in
every fixture and cohort document; this module never upgrades it to a closure
claim silently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from numbers import Integral, Real
from typing import Any

from smateway.closure_qualification import (
    ARMS,
    PLAN_SCHEMA,
    REPEAT_COUNT,
    TOPOLOGY_SCHEMA,
    ArmClosureEvidence,
    CanonicalIdentity,
    ClosureCampaignEvidence,
    ClosureCohort,
    ClosureRepeat,
    ComplexDetection,
    leaf_source_set_sha256,
    make_canonical_identity,
)

FIXTURE_KIND = "smateway.5g8.arm-preserving-fixture/v2"
PHYSICAL_GRAPH_SCHEMA = "smateway.5g8.arm-preserving-physical-graph/v2"
REFERENCE_PLANE_SCHEMA = "smateway.5g8.arm-preserving-reference-planes/v2"
SETUP_KIND = "smateway.5g8.arm-preserving-setup-attestation/v1"
OBSERVATION_KIND = "smateway.5g8.arm-preserving-observation/v1"
COHORT_DOCUMENT_KIND = "smateway.5g8.closure-cohort/v1"
FRAGMENT_KIND = "smateway.5g8.arm-preserving-c-d2-fragment/v1"

ROLES = ("c_i", "d2_i")
CENTER_FREQUENCY_HZ = 5_800_000_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 3
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
RECEIVER_GAIN_DB = 40.0
TX_HARDWARE_GAIN_DB = -20.0
DDS_SCALE = 0.125
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
MINIMUM_REFERENCE_SNR_DB = 20.0
ADC_CLIP_THRESHOLD_COUNTS = 2_047.0

COMPONENT_ROLES = (
    "pluto",
    "two_way_splitter",
    "eight_way_splitter",
    "selector",
    "rx1_attenuation_chain",
    "tx2_termination",
)
FIXED_CONNECTION_ROLES = (
    "tx1_to_two_way",
    "two_way_reference_to_rx1_attenuation",
    "rx1_attenuation_to_rx1",
    "selector_common_to_rx2",
    "tx2_to_termination",
)
FIXED_REFERENCE_PLANE_ROLES = (
    "tx1_source",
    "two_way_stimulus_output",
    "eight_way_input",
    "rx1_protected_input",
    "selector_common",
    "rx2_input",
)

TOPOLOGY_LIMITATION_CODE = "UNCHARACTERIZED_8WAY_SPLITTER_MULTIPORT"
TOPOLOGY_LIMITATION_REASON = (
    "Single-arm terminated-port captures do not characterize simultaneous "
    "8-way splitter multiport interaction; arm-preserving closure is diagnostic only."
)
TOPOLOGY_AUTHORITY = "diagnostic_only_uncharacterized_splitter_multiport"

_HEX = frozenset("0123456789abcdef")


class ArmPreservingD2Error(ValueError):
    """The fixture or evidence violates the frozen arm-preserving contract."""


def canonical_json(value: object) -> str:
    """Return the exact finite canonical JSON representation used for identities."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ArmPreservingD2Error("identity must contain only finite JSON values") from error


def canonical_sha256(value: object) -> str:
    """Hash a JSON-compatible object deterministically."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArmPreservingD2Error(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    actual = set(value)
    wanted = set(expected)
    if actual != wanted:
        raise ArmPreservingD2Error(
            f"{label} fields differ; missing={sorted(wanted - actual)}, "
            f"extra={sorted(actual - wanted)}"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ArmPreservingD2Error(f"{label} must be a nonempty identifier")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-"
        for character in value
    ):
        raise ArmPreservingD2Error(f"{label} contains unsafe characters")
    return value


def _absolute_path(value: object, label: str) -> str:
    result = _identifier(value, label)
    if not result.startswith("/") or result.endswith("/"):
        raise ArmPreservingD2Error(f"{label} must name an absolute file")
    return result


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ArmPreservingD2Error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_commit(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in _HEX for character in value)
    ):
        raise ArmPreservingD2Error(f"{label} must be a full lowercase Git commit")
    return value


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ArmPreservingD2Error(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result) or (positive and result <= 0.0):
        suffix = " and positive" if positive else ""
        raise ArmPreservingD2Error(f"{label} must be finite{suffix}")
    return result


def _identity_document(identity: CanonicalIdentity) -> dict[str, str]:
    return {"canonical_json": identity.canonical_json, "sha256": identity.sha256}


def _identity(value: object, label: str) -> CanonicalIdentity:
    document = _mapping(value, label)
    _exact_keys(document, ("canonical_json", "sha256"), label)
    identity = CanonicalIdentity(
        canonical_json=str(document["canonical_json"]),
        sha256=_sha256(document["sha256"], f"{label}.sha256"),
    )
    try:
        identity.payload()
    except ValueError as error:
        raise ArmPreservingD2Error(f"{label} is invalid: {error}") from error
    return identity


def _normalized(value: object) -> Any:
    return json.loads(canonical_json(value))


def _normalized_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalized(value)
    if not isinstance(normalized, dict):  # pragma: no cover - input is a mapping.
        raise AssertionError("canonical mapping normalization returned a non-object")
    return normalized


def _component(value: object, role: str) -> dict[str, str]:
    component = _mapping(value, f"components.{role}")
    _exact_keys(component, ("component_id", "identity_sha256"), f"components.{role}")
    return {
        "component_id": _identifier(component["component_id"], f"components.{role}.id"),
        "identity_sha256": _sha256(component["identity_sha256"], f"components.{role}.identity"),
    }


def _termination(value: object, label: str) -> dict[str, Any]:
    termination = _mapping(value, label)
    _exact_keys(
        termination,
        (
            "termination_id",
            "identity_sha256",
            "impedance_ohm",
            "rated_min_frequency_hz",
            "rated_max_frequency_hz",
            "maximum_input_dbm",
        ),
        label,
    )
    impedance = _finite(termination["impedance_ohm"], f"{label}.impedance", positive=True)
    minimum = _finite(termination["rated_min_frequency_hz"], f"{label}.rated_min", positive=True)
    maximum = _finite(termination["rated_max_frequency_hz"], f"{label}.rated_max", positive=True)
    if abs(impedance - 50.0) > 0.5:
        raise ArmPreservingD2Error(f"{label} must be a 50-ohm termination")
    if not minimum <= CENTER_FREQUENCY_HZ <= maximum:
        raise ArmPreservingD2Error(f"{label} is not rated across 5.8 GHz")
    return {
        "termination_id": _identifier(termination["termination_id"], f"{label}.termination_id"),
        "identity_sha256": _sha256(termination["identity_sha256"], f"{label}.identity_sha256"),
        "impedance_ohm": impedance,
        "rated_min_frequency_hz": minimum,
        "rated_max_frequency_hz": maximum,
        "maximum_input_dbm": _finite(
            termination["maximum_input_dbm"], f"{label}.maximum_input_dbm"
        ),
    }


def _arm_path(value: object, arm: str, planes: Mapping[str, Any]) -> dict[str, str]:
    path = _mapping(value, f"arm_paths.{arm}")
    _exact_keys(
        path,
        (
            "path_id",
            "identity_sha256",
            "splitter_output_port",
            "selector_input_port",
            "splitter_output_reference_plane",
            "selector_input_reference_plane",
        ),
        f"arm_paths.{arm}",
    )
    index = int(arm.removeprefix("ANT"))
    arm_planes = _mapping(planes[arm], f"reference_planes.arms.{arm}")
    if path["splitter_output_port"] != f"F{index}" or path["selector_input_port"] != arm:
        raise ArmPreservingD2Error(f"{arm} must bind exact splitter F{index} to selector {arm}")
    if (
        path["splitter_output_reference_plane"] != arm_planes["splitter_output"]
        or path["selector_input_reference_plane"] != arm_planes["selector_input"]
    ):
        raise ArmPreservingD2Error(f"{arm} path reference planes differ from the fixture")
    return {
        "path_id": _identifier(path["path_id"], f"arm_paths.{arm}.path_id"),
        "identity_sha256": _sha256(path["identity_sha256"], f"arm_paths.{arm}.identity_sha256"),
        "splitter_output_port": f"F{index}",
        "selector_input_port": arm,
        "splitter_output_reference_plane": str(path["splitter_output_reference_plane"]),
        "selector_input_reference_plane": str(path["selector_input_reference_plane"]),
    }


def _file_binding(value: object, label: str) -> dict[str, Any]:
    binding = _mapping(value, label)
    _exact_keys(binding, ("path", "sha256", "size_bytes"), label)
    size = binding["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, Integral) or int(size) <= 0:
        raise ArmPreservingD2Error(f"{label}.size_bytes must be a positive integer")
    return {
        "path": _absolute_path(binding["path"], f"{label}.path"),
        "sha256": _sha256(binding["sha256"], f"{label}.sha256"),
        "size_bytes": int(size),
    }


def _selector_binding(value: object, *, campaign_id: str, board_id: str) -> dict[str, Any]:
    binding = _mapping(value, "selector_flash_attestation")
    _exact_keys(
        binding,
        ("file", "campaign_id", "run_id", "board_id", "image_role"),
        "selector_flash_attestation",
    )
    if (
        binding["campaign_id"] != campaign_id
        or binding["board_id"] != board_id
        or binding["image_role"] != "bench"
    ):
        raise ArmPreservingD2Error(
            "selector flash binding must be the exact campaign/board bench image"
        )
    return {
        "file": _file_binding(binding["file"], "selector_flash_attestation.file"),
        "campaign_id": campaign_id,
        "run_id": _identifier(binding["run_id"], "selector flash run ID"),
        "board_id": board_id,
        "image_role": "bench",
    }


def _set_sha256(values: Mapping[str, Mapping[str, Any]], field: str) -> str:
    return canonical_sha256([values[arm][field] for arm in ARMS])


def _topology_payload(
    *,
    campaign_id: str,
    role: str,
    arm: str | None,
    fixture_graph_sha256: str,
    reference_plane_sha256: str,
    source_configuration: str,
    details: Mapping[str, Any],
    upstream: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "schema": TOPOLOGY_SCHEMA,
        "campaign_id": campaign_id,
        "method": "arm_preserving",
        "role": role,
        "arm": arm,
        "fixture_graph_sha256": fixture_graph_sha256,
        "reference_plane_sha256": reference_plane_sha256,
        "source_configuration": source_configuration,
        "topology_details": _normalized(details),
        "upstream_sha256s": dict(sorted(upstream.items())),
    }


@dataclass(frozen=True, slots=True)
class ValidatedArmPreservingFixture:
    """Canonical fixture plus all closure-compatible identities."""

    document: dict[str, Any]
    fixture_sha256: str
    campaign_id: str
    board_id: str
    pluto_serial: str
    source_commit: str
    plan_identity: CanonicalIdentity
    fixture_graph_identity: CanonicalIdentity
    reference_plane_identity: CanonicalIdentity
    topology_identities: Mapping[str, CanonicalIdentity]
    arm_paths: Mapping[str, Mapping[str, Any]]
    splitter_output_terminations: Mapping[str, Mapping[str, Any]]
    selector_input_terminations: Mapping[str, Mapping[str, Any]]
    selector_flash_attestation: Mapping[str, Any]

    def topology(self, arm: str, role: str) -> CanonicalIdentity:
        if arm not in ARMS or role not in ROLES:
            raise KeyError(f"invalid arm-preserving topology {arm}.{role}")
        return self.topology_identities[f"{arm}.{role}"]


def build_fixture_v2(
    *,
    campaign_id: str,
    board_id: str,
    pluto_serial: str,
    source_commit: str,
    components: Mapping[str, object],
    fixed_connection_ids: Mapping[str, object],
    reference_planes: Mapping[str, object],
    arm_paths: Mapping[str, object],
    splitter_output_terminations: Mapping[str, object],
    selector_input_terminations: Mapping[str, object],
    selector_flash_attestation: Mapping[str, object],
    linearity_evidence_sha256s: Mapping[str, object],
    rf_safety: Mapping[str, object],
) -> dict[str, Any]:
    """Derive one complete deterministic fixture and arm-preserving closure plan."""

    campaign = _identifier(campaign_id, "campaign ID")
    board = _identifier(board_id, "board ID")
    serial = _identifier(pluto_serial, "Pluto serial")
    commit = _git_commit(source_commit, "source commit")

    _exact_keys(components, COMPONENT_ROLES, "components")
    normalized_components = {role: _component(components[role], role) for role in COMPONENT_ROLES}
    component_ids = [value["component_id"] for value in normalized_components.values()]
    if len(set(component_ids)) != len(component_ids):
        raise ArmPreservingD2Error("fixture component IDs must be globally unique")

    _exact_keys(fixed_connection_ids, FIXED_CONNECTION_ROLES, "fixed_connection_ids")
    normalized_connections = {
        role: _identifier(fixed_connection_ids[role], f"fixed_connection_ids.{role}")
        for role in FIXED_CONNECTION_ROLES
    }
    if len(set(normalized_connections.values())) != len(normalized_connections):
        raise ArmPreservingD2Error("fixed connection IDs must be unique")

    raw_planes = _mapping(reference_planes, "reference_planes")
    _exact_keys(raw_planes, (*FIXED_REFERENCE_PLANE_ROLES, "arms"), "reference_planes")
    normalized_planes: dict[str, Any] = {
        role: _identifier(raw_planes[role], f"reference_planes.{role}")
        for role in FIXED_REFERENCE_PLANE_ROLES
    }
    raw_arm_planes = _mapping(raw_planes["arms"], "reference_planes.arms")
    _exact_keys(raw_arm_planes, ARMS, "reference_planes.arms")
    normalized_arm_planes: dict[str, dict[str, str]] = {}
    for arm in ARMS:
        arm_plane = _mapping(raw_arm_planes[arm], f"reference_planes.arms.{arm}")
        _exact_keys(
            arm_plane,
            ("splitter_output", "selector_input"),
            f"reference_planes.arms.{arm}",
        )
        normalized_arm_planes[arm] = {
            "splitter_output": _identifier(
                arm_plane["splitter_output"],
                f"reference_planes.arms.{arm}.splitter_output",
            ),
            "selector_input": _identifier(
                arm_plane["selector_input"],
                f"reference_planes.arms.{arm}.selector_input",
            ),
        }
    normalized_planes["arms"] = normalized_arm_planes
    plane_ids = [str(normalized_planes[role]) for role in FIXED_REFERENCE_PLANE_ROLES]
    plane_ids.extend(plane for arm in ARMS for plane in normalized_arm_planes[arm].values())
    if len(set(plane_ids)) != len(plane_ids):
        raise ArmPreservingD2Error("every declared RF reference plane must have a unique ID")

    _exact_keys(arm_paths, ARMS, "arm_paths")
    normalized_paths = {arm: _arm_path(arm_paths[arm], arm, normalized_arm_planes) for arm in ARMS}
    if len({path["path_id"] for path in normalized_paths.values()}) != len(ARMS):
        raise ArmPreservingD2Error("E arm/cable path IDs must be unique")
    if len({path["identity_sha256"] for path in normalized_paths.values()}) != len(ARMS):
        raise ArmPreservingD2Error("E arm/cable identity hashes must be unique")

    _exact_keys(
        splitter_output_terminations,
        ARMS,
        "splitter_output_terminations",
    )
    _exact_keys(
        selector_input_terminations,
        ARMS,
        "selector_input_terminations",
    )
    splitter_loads = {
        arm: _termination(splitter_output_terminations[arm], f"splitter_output_terminations.{arm}")
        for arm in ARMS
    }
    selector_loads = {
        arm: _termination(selector_input_terminations[arm], f"selector_input_terminations.{arm}")
        for arm in ARMS
    }
    all_loads = [*splitter_loads.values(), *selector_loads.values()]
    load_ids = [str(item["termination_id"]) for item in all_loads]
    load_hashes = [str(item["identity_sha256"]) for item in all_loads]
    if len(set(load_ids)) != 16 or len(set(load_hashes)) != 16:
        raise ArmPreservingD2Error(
            "splitter-output and selector-input loads must be 16 independent identities"
        )

    selector_binding = _selector_binding(
        selector_flash_attestation,
        campaign_id=campaign,
        board_id=board,
    )
    _exact_keys(linearity_evidence_sha256s, ARMS, "linearity_evidence_sha256s")
    linearity = {
        arm: _sha256(linearity_evidence_sha256s[arm], f"linearity_evidence_sha256s.{arm}")
        for arm in ARMS
    }
    _exact_keys(
        rf_safety,
        (
            "source_peak_output_bound_dbm",
            "receiver_input_limit_dbm",
            "minimum_path_attenuation_before_rx1_db",
            "required_margin_db",
        ),
        "rf_safety",
    )
    normalized_safety = {
        "source_peak_output_bound_dbm": _finite(
            rf_safety["source_peak_output_bound_dbm"],
            "rf_safety.source_peak_output_bound_dbm",
        ),
        "receiver_input_limit_dbm": _finite(
            rf_safety["receiver_input_limit_dbm"],
            "rf_safety.receiver_input_limit_dbm",
        ),
        "minimum_path_attenuation_before_rx1_db": _finite(
            rf_safety["minimum_path_attenuation_before_rx1_db"],
            "rf_safety.minimum_path_attenuation_before_rx1_db",
            positive=True,
        ),
        "required_margin_db": _finite(
            rf_safety["required_margin_db"],
            "rf_safety.required_margin_db",
            positive=True,
        ),
    }
    if normalized_safety["source_peak_output_bound_dbm"] != SOURCE_PEAK_OUTPUT_BOUND_DBM:
        raise ArmPreservingD2Error(
            "fixture source peak bound must equal the frozen 7 dBm Pluto bound"
        )

    path_hashes = {arm: normalized_paths[arm]["identity_sha256"] for arm in ARMS}
    splitter_load_hashes = {arm: splitter_loads[arm]["identity_sha256"] for arm in ARMS}
    selector_load_hashes = {arm: selector_loads[arm]["identity_sha256"] for arm in ARMS}
    graph_payload = {
        "schema": PHYSICAL_GRAPH_SCHEMA,
        "campaign_id": campaign,
        "board_id": board,
        "pluto_serial": serial,
        "components": normalized_components,
        "fixed_connection_ids": normalized_connections,
        "reference_planes": normalized_planes,
        "e_arm_path_sha256s": path_hashes,
        "splitter_output_termination_sha256s": splitter_load_hashes,
        "selector_input_termination_sha256s": selector_load_hashes,
        "rf_safety": normalized_safety,
        "physical_contract": {
            "tx1_to_two_way_to_eight_way_input": True,
            "two_way_reference_to_protected_rx1": True,
            "selector_common_to_rx2": True,
            "tx2_physically_terminated_and_digitally_muted": True,
            "no_antennas": True,
            "sixteen_independently_identified_terminations": True,
        },
    }
    graph_identity = make_canonical_identity(graph_payload)
    plane_identity = make_canonical_identity(
        {
            "schema": REFERENCE_PLANE_SCHEMA,
            "campaign_id": campaign,
            "board_id": board,
            "reference_planes": normalized_planes,
        }
    )
    graph_hash = graph_identity.sha256
    plane_hash = plane_identity.sha256
    selector_flash_hash = str(selector_binding["file"]["sha256"])
    path_set_hash = canonical_sha256(path_hashes)
    splitter_load_set_hash = _set_sha256(splitter_loads, "identity_sha256")
    selector_load_set_hash = _set_sha256(selector_loads, "identity_sha256")

    global_h_c = make_canonical_identity(
        _topology_payload(
            campaign_id=campaign,
            role="global_h_c",
            arm=None,
            fixture_graph_sha256=graph_hash,
            reference_plane_sha256=plane_hash,
            source_configuration="all_selector_inputs_terminated_global",
            details={
                "all_input_load_set_sha256": canonical_sha256(
                    {
                        "splitter_outputs": splitter_load_hashes,
                        "selector_inputs": selector_load_hashes,
                    }
                ),
                "static_selector_state": "ALL_OFF",
            },
            upstream={
                "fixture_graph": graph_hash,
                "reference_planes": plane_hash,
                "selector_flash_attestation": selector_flash_hash,
            },
        )
    )
    observed_e = make_canonical_identity(
        _topology_payload(
            campaign_id=campaign,
            role="observed_e",
            arm=None,
            fixture_graph_sha256=graph_hash,
            reference_plane_sha256=plane_hash,
            source_configuration="simultaneous_8way_feed",
            details={
                "arm_cable_sha256s": path_hashes,
                "static_selector_state": "ALL_OFF",
            },
            upstream={
                "e_arm_path_set": path_set_hash,
                "fixture_graph": graph_hash,
                "reference_planes": plane_hash,
                "selector_flash_attestation": selector_flash_hash,
            },
        )
    )

    arm_identities: dict[str, dict[str, CanonicalIdentity]] = {}
    for arm in ARMS:
        c_identity = make_canonical_identity(
            _topology_payload(
                campaign_id=campaign,
                role="c_i",
                arm=arm,
                fixture_graph_sha256=graph_hash,
                reference_plane_sha256=plane_hash,
                source_configuration="all_selector_inputs_terminated_dedicated",
                details={
                    "all_selector_inputs_terminated": True,
                    "valid_comparator_roles": ["d1_i", "d2_i"],
                    "dedicated_comparator_arm": arm,
                    "eight_way_input_driven": True,
                    "splitter_outputs_terminated": 8,
                    "selector_inputs_terminated": 8,
                    "splitter_output_termination_ids": {
                        name: splitter_loads[name]["termination_id"] for name in ARMS
                    },
                    "selector_input_termination_ids": {
                        name: selector_loads[name]["termination_id"] for name in ARMS
                    },
                    "static_selector_state": "ALL_OFF",
                    "selector_command_mode": "lease_free",
                    "topology_limitation_code": TOPOLOGY_LIMITATION_CODE,
                },
                upstream={
                    "fixture_graph": graph_hash,
                    "reference_planes": plane_hash,
                    "selector_flash_attestation": selector_flash_hash,
                    "selector_input_termination_set": selector_load_set_hash,
                    "splitter_output_termination_set": splitter_load_set_hash,
                },
            )
        )
        d1_identity = make_canonical_identity(
            _topology_payload(
                campaign_id=campaign,
                role="d1_i",
                arm=arm,
                fixture_graph_sha256=graph_hash,
                reference_plane_sha256=plane_hash,
                source_configuration="direct_one_hot",
                details={
                    "reference_c_i_topology_sha256": c_identity.sha256,
                    "board_input_reference_plane_sha256": plane_hash,
                    "linearity_evidence_sha256": linearity[arm],
                    "static_selector_state": "ALL_OFF",
                },
                upstream={
                    "fixture_graph": graph_hash,
                    "linearity_evidence": linearity[arm],
                    "reference_planes": plane_hash,
                    "selector_flash_attestation": selector_flash_hash,
                },
            )
        )
        other_arms = [name for name in ARMS if name != arm]
        d2_identity = make_canonical_identity(
            _topology_payload(
                campaign_id=campaign,
                role="d2_i",
                arm=arm,
                fixture_graph_sha256=graph_hash,
                reference_plane_sha256=plane_hash,
                source_configuration="arm_preserving_exact_e_arm",
                details={
                    "reference_c_i_topology_sha256": c_identity.sha256,
                    "board_input_reference_plane_sha256": plane_hash,
                    "e_topology_sha256": observed_e.sha256,
                    "e_arm_cable_sha256": normalized_paths[arm]["identity_sha256"],
                    "exact_connected_path_id": normalized_paths[arm]["path_id"],
                    "exact_connected_splitter_output": normalized_paths[arm][
                        "splitter_output_port"
                    ],
                    "exact_connected_selector_input": arm,
                    "eight_way_input_driven": True,
                    "only_exact_e_arm_connected": True,
                    "other_splitter_outputs_terminated": 7,
                    "other_selector_inputs_terminated": 7,
                    "other_splitter_output_termination_ids": {
                        name: splitter_loads[name]["termination_id"] for name in other_arms
                    },
                    "other_selector_input_termination_ids": {
                        name: selector_loads[name]["termination_id"] for name in other_arms
                    },
                    "static_selector_state": "ALL_OFF",
                    "selector_command_mode": "lease_free",
                    "topology_limitation_code": TOPOLOGY_LIMITATION_CODE,
                },
                upstream={
                    "c_i_topology": c_identity.sha256,
                    "e_arm_cable": normalized_paths[arm]["identity_sha256"],
                    "e_topology": observed_e.sha256,
                    "fixture_graph": graph_hash,
                    "reference_planes": plane_hash,
                    "selector_flash_attestation": selector_flash_hash,
                    "other_selector_input_termination_set": canonical_sha256(
                        [selector_loads[name]["identity_sha256"] for name in other_arms]
                    ),
                    "other_splitter_output_termination_set": canonical_sha256(
                        [splitter_loads[name]["identity_sha256"] for name in other_arms]
                    ),
                },
            )
        )
        arm_identities[arm] = {"c_i": c_identity, "d1_i": d1_identity, "d2_i": d2_identity}

    plan_identity = make_canonical_identity(
        {
            "schema": PLAN_SCHEMA,
            "campaign_id": campaign,
            "method": "arm_preserving",
            "source_commit": commit,
            "fixture_graph_sha256": graph_hash,
            "reference_plane_sha256": plane_hash,
            "splitter_multiport_characterized": False,
            "splitter_multiport_characterization_sha256": None,
            "e_arm_cable_sha256s": path_hashes,
            "topology_sha256s": {
                "global_h_c": global_h_c.sha256,
                "observed_e": observed_e.sha256,
                "arms": {
                    arm: {
                        role: arm_identities[arm][role].sha256 for role in ("c_i", "d1_i", "d2_i")
                    }
                    for arm in ARMS
                },
                "joint_weights": None,
            },
        }
    )
    topology_documents = {
        "global_h_c": _identity_document(global_h_c),
        "observed_e": _identity_document(observed_e),
        "arms": {
            arm: {
                role: _identity_document(arm_identities[arm][role])
                for role in ("c_i", "d1_i", "d2_i")
            }
            for arm in ARMS
        },
        "joint_weights": None,
    }
    return {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND,
        "campaign_id": campaign,
        "board_id": board,
        "pluto_serial": serial,
        "source_commit": commit,
        "components": normalized_components,
        "fixed_connection_ids": normalized_connections,
        "reference_planes": normalized_planes,
        "arm_paths": normalized_paths,
        "splitter_output_terminations": splitter_loads,
        "selector_input_terminations": selector_loads,
        "selector_flash_attestation": selector_binding,
        "linearity_evidence_sha256s": linearity,
        "rf_safety": normalized_safety,
        "fixture_graph_identity": _identity_document(graph_identity),
        "reference_plane_identity": _identity_document(plane_identity),
        "closure_plan_identity": _identity_document(plan_identity),
        "topology_identities": topology_documents,
        "splitter_multiport_characterization": {
            "status": "uncharacterized",
            "evidence_sha256": None,
        },
        "topology_limitation": {
            "code": TOPOLOGY_LIMITATION_CODE,
            "reason": TOPOLOGY_LIMITATION_REASON,
            "closure_authority": TOPOLOGY_AUTHORITY,
            "diagnostic_only": True,
            "closure_claim_permitted": False,
        },
    }


def validate_fixture_v2(value: object) -> ValidatedArmPreservingFixture:
    """Validate a deterministic fixture by rebuilding every derived identity."""

    document = _mapping(value, "fixture")
    _exact_keys(
        document,
        (
            "schema",
            "fixture_kind",
            "campaign_id",
            "board_id",
            "pluto_serial",
            "source_commit",
            "components",
            "fixed_connection_ids",
            "reference_planes",
            "arm_paths",
            "splitter_output_terminations",
            "selector_input_terminations",
            "selector_flash_attestation",
            "linearity_evidence_sha256s",
            "rf_safety",
            "fixture_graph_identity",
            "reference_plane_identity",
            "closure_plan_identity",
            "topology_identities",
            "splitter_multiport_characterization",
            "topology_limitation",
        ),
        "fixture",
    )
    if document["schema"] != 2 or document["fixture_kind"] != FIXTURE_KIND:
        raise ArmPreservingD2Error("fixture schema/kind differs from arm-preserving v2")
    expected = build_fixture_v2(
        campaign_id=str(document["campaign_id"]),
        board_id=str(document["board_id"]),
        pluto_serial=str(document["pluto_serial"]),
        source_commit=str(document["source_commit"]),
        components=_mapping(document["components"], "components"),
        fixed_connection_ids=_mapping(document["fixed_connection_ids"], "fixed_connection_ids"),
        reference_planes=_mapping(document["reference_planes"], "reference_planes"),
        arm_paths=_mapping(document["arm_paths"], "arm_paths"),
        splitter_output_terminations=_mapping(
            document["splitter_output_terminations"], "splitter_output_terminations"
        ),
        selector_input_terminations=_mapping(
            document["selector_input_terminations"], "selector_input_terminations"
        ),
        selector_flash_attestation=_mapping(
            document["selector_flash_attestation"], "selector_flash_attestation"
        ),
        linearity_evidence_sha256s=_mapping(
            document["linearity_evidence_sha256s"], "linearity_evidence_sha256s"
        ),
        rf_safety=_mapping(document["rf_safety"], "rf_safety"),
    )
    normalized = _normalized(document)
    if normalized != expected:
        raise ArmPreservingD2Error(
            "fixture derived identities/topology limitation differ from canonical v2"
        )
    plan = _identity(expected["closure_plan_identity"], "closure_plan_identity")
    graph = _identity(expected["fixture_graph_identity"], "fixture_graph_identity")
    planes = _identity(expected["reference_plane_identity"], "reference_plane_identity")
    topologies: dict[str, CanonicalIdentity] = {
        "global_h_c": _identity(
            expected["topology_identities"]["global_h_c"], "global_h_c topology"
        ),
        "observed_e": _identity(
            expected["topology_identities"]["observed_e"], "observed_e topology"
        ),
    }
    for arm in ARMS:
        for role in ("c_i", "d1_i", "d2_i"):
            topologies[f"{arm}.{role}"] = _identity(
                expected["topology_identities"]["arms"][arm][role],
                f"{arm}.{role} topology",
            )
    return ValidatedArmPreservingFixture(
        document=expected,
        fixture_sha256=canonical_sha256(expected),
        campaign_id=str(expected["campaign_id"]),
        board_id=str(expected["board_id"]),
        pluto_serial=str(expected["pluto_serial"]),
        source_commit=str(expected["source_commit"]),
        plan_identity=plan,
        fixture_graph_identity=graph,
        reference_plane_identity=planes,
        topology_identities=topologies,
        arm_paths=_mapping(expected["arm_paths"], "arm_paths"),
        splitter_output_terminations=_mapping(
            expected["splitter_output_terminations"], "splitter_output_terminations"
        ),
        selector_input_terminations=_mapping(
            expected["selector_input_terminations"], "selector_input_terminations"
        ),
        selector_flash_attestation=_mapping(
            expected["selector_flash_attestation"], "selector_flash_attestation"
        ),
    )


def expected_setup_inventory(
    fixture: ValidatedArmPreservingFixture, *, role: str, arm: str
) -> dict[str, Any]:
    """Return exact connected paths and load IDs for one C_i or D2_i setup."""

    if role not in ROLES or arm not in ARMS:
        raise ArmPreservingD2Error("setup role/arm must be c_i|d2_i and ANT1..ANT8")
    if role == "c_i":
        return {
            "connected_e_arm_path_ids": [],
            "splitter_output_termination_ids": [
                fixture.splitter_output_terminations[name]["termination_id"] for name in ARMS
            ],
            "selector_input_termination_ids": [
                fixture.selector_input_terminations[name]["termination_id"] for name in ARMS
            ],
        }
    other = [name for name in ARMS if name != arm]
    return {
        "connected_e_arm_path_ids": [fixture.arm_paths[arm]["path_id"]],
        "splitter_output_termination_ids": [
            fixture.splitter_output_terminations[name]["termination_id"] for name in other
        ],
        "selector_input_termination_ids": [
            fixture.selector_input_terminations[name]["termination_id"] for name in other
        ],
    }


def validate_setup_attestation(
    value: object,
    *,
    fixture: ValidatedArmPreservingFixture,
    fixture_file_sha256: str,
    run_id: str,
    role: str,
    arm: str,
    repeat_index: int,
) -> dict[str, Any]:
    """Validate a run-specific human observation of the exact physical topology."""

    document = _mapping(value, "setup attestation")
    _exact_keys(
        document,
        (
            "schema",
            "attestation_kind",
            "attestation_id",
            "created_at",
            "operator_id",
            "campaign_id",
            "run_id",
            "role",
            "arm",
            "repeat_index",
            "fixture_file_sha256",
            "observed_inventory",
            "setup_evidence",
            "confirmations",
        ),
        "setup attestation",
    )
    if (
        document["schema"] != 1
        or document["attestation_kind"] != SETUP_KIND
        or document["campaign_id"] != fixture.campaign_id
        or document["run_id"] != run_id
        or document["role"] != role
        or document["arm"] != arm
        or document["repeat_index"] != repeat_index
        or document["fixture_file_sha256"] != _sha256(fixture_file_sha256, "fixture file SHA-256")
    ):
        raise ArmPreservingD2Error("setup attestation identity differs from this run")
    _identifier(document["attestation_id"], "setup attestation ID")
    _identifier(document["operator_id"], "operator ID")
    if not isinstance(document["created_at"], str) or not document["created_at"]:
        raise ArmPreservingD2Error("setup attestation timestamp is missing")
    if document["observed_inventory"] != expected_setup_inventory(fixture, role=role, arm=arm):
        raise ArmPreservingD2Error("observed fixture inventory differs from exact condition")
    _file_binding(document["setup_evidence"], "setup_evidence")
    confirmations = _mapping(document["confirmations"], "setup confirmations")
    required = (
        "no_antennas",
        "tx1_drives_two_way_then_eight_way_input",
        "two_way_reference_feeds_protected_rx1",
        "selector_common_feeds_rx2",
        "tx2_physically_terminated_and_digitally_muted",
        "static_bench_image_live",
        "selector_lease_free_all_off",
        "exact_connected_paths_only",
        "every_listed_load_is_independent_and_5g8_rated",
        "reference_planes_match_fixture_v2",
        "no_unlisted_connection_or_movement",
        "topology_limitation_understood",
    )
    _exact_keys(confirmations, required, "setup confirmations")
    if any(confirmations[name] is not True for name in required):
        raise ArmPreservingD2Error("every physical setup confirmation must be true")
    return _normalized_mapping(document)


@dataclass(frozen=True, slots=True)
class NormalizedArmObservation:
    role: str
    arm: str
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
    document: dict[str, Any]


def _complex_detection(value: object, label: str) -> ComplexDetection:
    document = _mapping(value, label)
    _exact_keys(
        document,
        ("detected", "phasor", "magnitude_upper_bound"),
        label,
    )
    detected = document["detected"]
    if not isinstance(detected, bool):
        raise ArmPreservingD2Error(f"{label}.detected must be boolean")
    if detected:
        phasor_document = _mapping(document["phasor"], f"{label}.phasor")
        _exact_keys(phasor_document, ("real", "imag"), f"{label}.phasor")
        phasor = complex(
            _finite(phasor_document["real"], f"{label}.phasor.real"),
            _finite(phasor_document["imag"], f"{label}.phasor.imag"),
        )
        if abs(phasor) == 0.0 or document["magnitude_upper_bound"] is not None:
            raise ArmPreservingD2Error(
                f"{label} detected value needs nonzero phasor and no upper bound"
            )
        return ComplexDetection(True, phasor, None)
    if document["phasor"] is not None:
        raise ArmPreservingD2Error(f"{label} nondetection must not synthesize phase")
    bound = _finite(
        document["magnitude_upper_bound"],
        f"{label}.magnitude_upper_bound",
        positive=True,
    )
    return ComplexDetection(False, None, bound)


def complex_detection_document(value: ComplexDetection) -> dict[str, Any]:
    """Serialize one detected phasor or one phase-free upper bound."""

    if value.detected:
        if value.phasor is None:
            raise ArmPreservingD2Error("detected value lacks a phasor")
        return {
            "detected": True,
            "phasor": {"real": float(value.phasor.real), "imag": float(value.phasor.imag)},
            "magnitude_upper_bound": None,
        }
    return {
        "detected": False,
        "phasor": None,
        "magnitude_upper_bound": value.magnitude_upper_bound,
    }


def validate_observation(
    value: object,
    *,
    fixture: ValidatedArmPreservingFixture,
) -> NormalizedArmObservation:
    """Validate one normalized, admitted, source-bound C_i or D2_i observation."""

    document = _mapping(value, "observation")
    _exact_keys(
        document,
        (
            "schema",
            "observation_kind",
            "campaign_id",
            "board_id",
            "pluto_serial",
            "role",
            "arm",
            "repeat_index",
            "run_id",
            "condition_id",
            "fixture_file",
            "fixture_sha256",
            "setup_attestation_file",
            "selector_flash_attestation_file",
            "closure_plan_sha256",
            "topology_sha256",
            "fixture_graph_sha256",
            "reference_plane_sha256",
            "source",
            "capture",
            "artifact",
            "condition_record_sha256",
            "leaf_source_sha256s",
            "leaf_source_set_sha256",
            "transfer",
            "quality",
            "safety",
            "topology_limitation",
        ),
        "observation",
    )
    role = document["role"]
    arm = document["arm"]
    index = document["repeat_index"]
    if role not in ROLES or arm not in ARMS:
        raise ArmPreservingD2Error("observation role/arm is invalid")
    if isinstance(index, bool) or not isinstance(index, Integral) or int(index) not in range(1, 6):
        raise ArmPreservingD2Error("observation repeat index must be exactly 1..5")
    if (
        document["schema"] != 1
        or document["observation_kind"] != OBSERVATION_KIND
        or document["campaign_id"] != fixture.campaign_id
        or document["board_id"] != fixture.board_id
        or document["pluto_serial"] != fixture.pluto_serial
        or document["fixture_sha256"] != fixture.fixture_sha256
        or document["closure_plan_sha256"] != fixture.plan_identity.sha256
        or document["topology_sha256"] != fixture.topology(str(arm), str(role)).sha256
        or document["fixture_graph_sha256"] != fixture.fixture_graph_identity.sha256
        or document["reference_plane_sha256"] != fixture.reference_plane_identity.sha256
        or document["topology_limitation"] != fixture.document["topology_limitation"]
    ):
        raise ArmPreservingD2Error("observation fixture/plan/topology binding is stale")
    run_id = _identifier(document["run_id"], "observation run ID")
    expected_condition = f"{fixture.campaign_id}.{role}.{arm}.repeat-{int(index)}.{run_id}"
    if document["condition_id"] != expected_condition:
        raise ArmPreservingD2Error("observation condition ID is not canonical or run-distinct")
    for field in (
        "fixture_file",
        "setup_attestation_file",
        "selector_flash_attestation_file",
    ):
        _file_binding(document[field], field)
    if document["fixture_file"]["sha256"] == document["setup_attestation_file"]["sha256"]:
        raise ArmPreservingD2Error("fixture and setup evidence identities were silently reused")
    if document["selector_flash_attestation_file"] != fixture.selector_flash_attestation["file"]:
        raise ArmPreservingD2Error("observation selector image evidence differs from fixture")

    source = _mapping(document["source"], "observation.source")
    _exact_keys(
        source,
        (
            "smateway_commit",
            "smateway_files_sha256",
            "dependency_commit",
            "dependency_files_sha256",
            "native_libiio_attestation_sha256",
        ),
        "observation.source",
    )
    source_commit = _git_commit(source["smateway_commit"], "Smateway source commit")
    if source_commit != fixture.source_commit:
        raise ArmPreservingD2Error("observation source commit differs from closure plan")
    for name in (
        "smateway_files_sha256",
        "dependency_files_sha256",
        "native_libiio_attestation_sha256",
    ):
        _sha256(source[name], f"observation.source.{name}")
    _git_commit(source["dependency_commit"], "dependency commit")

    capture = _mapping(document["capture"], "observation.capture")
    _exact_keys(
        capture,
        (
            "stream_id",
            "metadata_abi",
            "center_frequency_hz",
            "sample_rate_hz",
            "bandwidth_hz",
            "tone_offset_hz",
            "samples_per_frame",
            "frame_count",
            "sample_count",
            "kernel_buffers",
            "receiver_gain_db",
            "tx_hardware_gain_db",
            "dds_scale",
            "continuity_passed",
            "rf_readback_passed",
        ),
        "observation.capture",
    )
    expected_capture: dict[str, Any] = {
        "metadata_abi": 2,
        "center_frequency_hz": CENTER_FREQUENCY_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "tone_offset_hz": TONE_OFFSET_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "sample_count": TOTAL_SAMPLES,
        "kernel_buffers": KERNEL_BUFFERS,
        "receiver_gain_db": RECEIVER_GAIN_DB,
        "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
        "dds_scale": DDS_SCALE,
        "continuity_passed": True,
        "rf_readback_passed": True,
    }
    if any(capture.get(name) != expected for name, expected in expected_capture.items()):
        raise ArmPreservingD2Error("capture settings/readback differ from frozen acquisition")
    stream_id = _identifier(str(capture["stream_id"]), "capture stream ID")

    artifact = _mapping(document["artifact"], "observation.artifact")
    _exact_keys(
        artifact,
        (
            "artifact_id",
            "path",
            "artifact_sha256",
            "raw_iq_path",
            "raw_iq_sha256",
            "metadata_path",
            "metadata_sha256",
        ),
        "observation.artifact",
    )
    _identifier(artifact["artifact_id"], "artifact ID")
    for path_name in ("path", "raw_iq_path", "metadata_path"):
        _absolute_path(artifact[path_name], f"artifact.{path_name}")
    artifact_sha = _sha256(artifact["artifact_sha256"], "artifact descriptor SHA-256")
    raw_sha = _sha256(artifact["raw_iq_sha256"], "raw-IQ SHA-256")
    metadata_sha = _sha256(artifact["metadata_sha256"], "metadata SHA-256")
    descriptor = {name: artifact[name] for name in artifact if name != "artifact_sha256"}
    if canonical_sha256(descriptor) != artifact_sha:
        raise ArmPreservingD2Error("artifact descriptor SHA-256 is inconsistent")
    record_sha = _sha256(document["condition_record_sha256"], "condition record SHA-256")
    leaves_raw = document["leaf_source_sha256s"]
    if not isinstance(leaves_raw, list) or not leaves_raw:
        raise ArmPreservingD2Error("leaf source list must be nonempty")
    leaves = tuple(_sha256(item, "leaf source SHA-256") for item in leaves_raw)
    if leaves != tuple(sorted(leaves)) or len(set(leaves)) != len(leaves):
        raise ArmPreservingD2Error("leaf source hashes must be sorted and duplicate-free")
    if raw_sha not in leaves:
        raise ArmPreservingD2Error("raw-IQ hash must be a registered leaf source")
    leaf_set_hash = _sha256(document["leaf_source_set_sha256"], "leaf set SHA-256")
    if leaf_source_set_sha256(leaves) != leaf_set_hash:
        raise ArmPreservingD2Error("leaf source set hash mismatch")

    quality = _mapping(document["quality"], "observation.quality")
    _exact_keys(
        quality,
        (
            "passed",
            "rejection_reasons",
            "reference_tone_snr_db",
            "adc_headroom_passed",
            "clipped_sample_count_by_receiver",
        ),
        "observation.quality",
    )
    if quality["passed"] is not True or quality["rejection_reasons"] != []:
        raise ArmPreservingD2Error("observation did not pass acquisition quality")
    if _finite(quality["reference_tone_snr_db"], "reference SNR") < MINIMUM_REFERENCE_SNR_DB:
        raise ArmPreservingD2Error("observation reference SNR is below 20 dB")
    if quality["adc_headroom_passed"] is not True:
        raise ArmPreservingD2Error("observation ADC headroom failed")
    clipped = quality["clipped_sample_count_by_receiver"]
    if clipped != [0, 0]:
        raise ArmPreservingD2Error("observation contains clipped samples")

    safety = _mapping(document["safety"], "observation.safety")
    _exact_keys(
        safety,
        (
            "initial_exact_mute_passed",
            "selector_all_off_before_passed",
            "selector_all_off_after_passed",
            "selector_all_off_cleanup_passed",
            "final_exact_mute_passed",
            "persistence_after_final_mute_only",
            "automatic_retry_count",
            "accepted_from_quarantine",
        ),
        "observation.safety",
    )
    if (
        any(
            safety[name] is not True
            for name in (
                "initial_exact_mute_passed",
                "selector_all_off_before_passed",
                "selector_all_off_after_passed",
                "selector_all_off_cleanup_passed",
                "final_exact_mute_passed",
                "persistence_after_final_mute_only",
            )
        )
        or safety["automatic_retry_count"] != 0
        or safety["accepted_from_quarantine"] is not False
    ):
        raise ArmPreservingD2Error("observation safety admission is incomplete")
    detection = _complex_detection(document["transfer"], "observation.transfer")
    return NormalizedArmObservation(
        role=str(role),
        arm=str(arm),
        repeat_index=int(index),
        run_id=run_id,
        condition_id=expected_condition,
        stream_id=stream_id,
        artifact_sha256=artifact_sha,
        raw_iq_sha256=raw_sha,
        metadata_sha256=metadata_sha,
        condition_record_sha256=record_sha,
        leaf_source_sha256s=leaves,
        leaf_source_set_sha256=leaf_set_hash,
        plan_sha256=fixture.plan_identity.sha256,
        topology_sha256=fixture.topology(str(arm), str(role)).sha256,
        source_commit=source_commit,
        quality_passed=True,
        value=detection,
        document=_normalized_mapping(document),
    )


def observation_to_repeat(value: NormalizedArmObservation) -> ClosureRepeat:
    """Adapt one normalized observation to the shared closure model."""

    return ClosureRepeat(
        repeat_index=value.repeat_index,
        run_id=value.run_id,
        condition_id=value.condition_id,
        stream_id=value.stream_id,
        artifact_sha256=value.artifact_sha256,
        raw_iq_sha256=value.raw_iq_sha256,
        metadata_sha256=value.metadata_sha256,
        condition_record_sha256=value.condition_record_sha256,
        leaf_source_sha256s=value.leaf_source_sha256s,
        leaf_source_set_sha256=value.leaf_source_set_sha256,
        plan_sha256=value.plan_sha256,
        topology_sha256=value.topology_sha256,
        source_commit=value.source_commit,
        quality_passed=value.quality_passed,
        value=value.value,
    )


@dataclass(frozen=True, slots=True)
class ArmCAndD2Cohorts:
    arm: str
    c_i: ClosureCohort
    d2_i: ClosureCohort


@dataclass(frozen=True, slots=True)
class ArmPreservingCAndD2Fragment:
    fixture_sha256: str
    plan_identity: CanonicalIdentity
    arms: tuple[ArmCAndD2Cohorts, ...]
    topology_limitation_code: str
    closure_authority: str
    closure_claim_permitted: bool


def build_c_d2_fragment(
    observations: Sequence[NormalizedArmObservation],
    *,
    fixture: ValidatedArmPreservingFixture,
) -> ArmPreservingCAndD2Fragment:
    """Require exact 8 x (C_i,D2_i) x five source-distinct observations."""

    if isinstance(observations, (str, bytes)) or len(observations) != 80:
        raise ArmPreservingD2Error("arm-preserving C/D2 fragment requires exactly 80 captures")
    keys: set[tuple[str, str, int]] = set()
    identities: dict[str, set[str]] = {
        "run_id": set(),
        "condition_id": set(),
        "stream_id": set(),
        "artifact": set(),
        "raw_iq": set(),
        "metadata": set(),
        "condition_record": set(),
        "leaf_source": set(),
    }
    for item in observations:
        if not isinstance(item, NormalizedArmObservation):
            raise ArmPreservingD2Error("fragment members must be normalized observations")
        key = (item.role, item.arm, item.repeat_index)
        if key in keys:
            raise ArmPreservingD2Error(f"duplicate observation cell {key}")
        keys.add(key)
        singular = {
            "run_id": item.run_id,
            "condition_id": item.condition_id,
            "stream_id": item.stream_id,
            "artifact": item.artifact_sha256,
            "raw_iq": item.raw_iq_sha256,
            "metadata": item.metadata_sha256,
            "condition_record": item.condition_record_sha256,
        }
        for label, identity in singular.items():
            if identity in identities[label]:
                raise ArmPreservingD2Error(f"silently reused {label} identity")
            identities[label].add(identity)
        for leaf in item.leaf_source_sha256s:
            if leaf in identities["leaf_source"]:
                raise ArmPreservingD2Error("silently reused raw leaf-source identity")
            identities["leaf_source"].add(leaf)
    expected = {
        (role, arm, index) for arm in ARMS for role in ROLES for index in range(1, REPEAT_COUNT + 1)
    }
    if keys != expected:
        raise ArmPreservingD2Error(
            f"fragment cells differ; missing={sorted(expected - keys)}, "
            f"extra={sorted(keys - expected)}"
        )
    arms: list[ArmCAndD2Cohorts] = []
    for arm in ARMS:
        cohorts: dict[str, ClosureCohort] = {}
        for role in ROLES:
            members = sorted(
                (item for item in observations if item.arm == arm and item.role == role),
                key=lambda item: item.repeat_index,
            )
            cohorts[role] = ClosureCohort(
                role=role,
                arm=arm,
                plan_sha256=fixture.plan_identity.sha256,
                source_commit=fixture.source_commit,
                topology_identity=fixture.topology(arm, role),
                repeats=tuple(observation_to_repeat(item) for item in members),
            )
        arms.append(ArmCAndD2Cohorts(arm, cohorts["c_i"], cohorts["d2_i"]))
    return ArmPreservingCAndD2Fragment(
        fixture_sha256=fixture.fixture_sha256,
        plan_identity=fixture.plan_identity,
        arms=tuple(arms),
        topology_limitation_code=TOPOLOGY_LIMITATION_CODE,
        closure_authority=TOPOLOGY_AUTHORITY,
        closure_claim_permitted=False,
    )


def _repeat_document(repeat: ClosureRepeat) -> dict[str, Any]:
    return {
        "repeat_index": repeat.repeat_index,
        "run_id": repeat.run_id,
        "condition_id": repeat.condition_id,
        "stream_id": repeat.stream_id,
        "artifact_sha256": repeat.artifact_sha256,
        "raw_iq_sha256": repeat.raw_iq_sha256,
        "metadata_sha256": repeat.metadata_sha256,
        "condition_record_sha256": repeat.condition_record_sha256,
        "leaf_source_sha256s": list(repeat.leaf_source_sha256s),
        "leaf_source_set_sha256": repeat.leaf_source_set_sha256,
        "plan_sha256": repeat.plan_sha256,
        "topology_sha256": repeat.topology_sha256,
        "source_commit": repeat.source_commit,
        "quality_passed": repeat.quality_passed,
        "value": complex_detection_document(repeat.value),
    }


def cohort_document(cohort: ClosureCohort) -> dict[str, Any]:
    """Serialize a shared closure cohort without losing source identities."""

    return {
        "schema": 1,
        "document_kind": COHORT_DOCUMENT_KIND,
        "role": cohort.role,
        "arm": cohort.arm,
        "plan_sha256": cohort.plan_sha256,
        "source_commit": cohort.source_commit,
        "topology_identity": _identity_document(cohort.topology_identity),
        "repeats": [_repeat_document(repeat) for repeat in cohort.repeats],
    }


def cohort_from_document(value: object) -> ClosureCohort:
    """Load a normalized shared cohort for assembly into full closure evidence."""

    document = _mapping(value, "closure cohort")
    _exact_keys(
        document,
        (
            "schema",
            "document_kind",
            "role",
            "arm",
            "plan_sha256",
            "source_commit",
            "topology_identity",
            "repeats",
        ),
        "closure cohort",
    )
    if document["schema"] != 1 or document["document_kind"] != COHORT_DOCUMENT_KIND:
        raise ArmPreservingD2Error("closure cohort schema/kind differs")
    role = _identifier(document["role"], "cohort role")
    arm_raw = document["arm"]
    arm = None if arm_raw is None else _identifier(arm_raw, "cohort arm")
    plan_sha = _sha256(document["plan_sha256"], "cohort plan hash")
    commit = _git_commit(document["source_commit"], "cohort source commit")
    topology = _identity(document["topology_identity"], "cohort topology")
    repeats_raw = document["repeats"]
    if not isinstance(repeats_raw, list) or len(repeats_raw) != REPEAT_COUNT:
        raise ArmPreservingD2Error("closure cohort must have exactly five repeats")
    repeats: list[ClosureRepeat] = []
    for raw in repeats_raw:
        repeat = _mapping(raw, "closure repeat")
        _exact_keys(
            repeat,
            (
                "repeat_index",
                "run_id",
                "condition_id",
                "stream_id",
                "artifact_sha256",
                "raw_iq_sha256",
                "metadata_sha256",
                "condition_record_sha256",
                "leaf_source_sha256s",
                "leaf_source_set_sha256",
                "plan_sha256",
                "topology_sha256",
                "source_commit",
                "quality_passed",
                "value",
            ),
            "closure repeat",
        )
        leaves_raw = repeat["leaf_source_sha256s"]
        if not isinstance(leaves_raw, list):
            raise ArmPreservingD2Error("closure repeat leaf sources must be a list")
        leaves = tuple(_sha256(item, "repeat leaf source") for item in leaves_raw)
        repeats.append(
            ClosureRepeat(
                repeat_index=int(repeat["repeat_index"]),
                run_id=_identifier(repeat["run_id"], "repeat run ID"),
                condition_id=_identifier(repeat["condition_id"], "repeat condition ID"),
                stream_id=_identifier(str(repeat["stream_id"]), "repeat stream ID"),
                artifact_sha256=_sha256(repeat["artifact_sha256"], "artifact hash"),
                raw_iq_sha256=_sha256(repeat["raw_iq_sha256"], "raw-IQ hash"),
                metadata_sha256=_sha256(repeat["metadata_sha256"], "metadata hash"),
                condition_record_sha256=_sha256(
                    repeat["condition_record_sha256"], "condition-record hash"
                ),
                leaf_source_sha256s=leaves,
                leaf_source_set_sha256=_sha256(repeat["leaf_source_set_sha256"], "leaf-set hash"),
                plan_sha256=_sha256(repeat["plan_sha256"], "repeat plan hash"),
                topology_sha256=_sha256(repeat["topology_sha256"], "repeat topology hash"),
                source_commit=_git_commit(repeat["source_commit"], "repeat source commit"),
                quality_passed=repeat["quality_passed"] is True,
                value=_complex_detection(repeat["value"], "repeat value"),
            )
        )
    return ClosureCohort(
        role=role,
        arm=arm,
        plan_sha256=plan_sha,
        source_commit=commit,
        topology_identity=topology,
        repeats=tuple(repeats),
    )


def fragment_document(fragment: ArmPreservingCAndD2Fragment) -> dict[str, Any]:
    """Serialize the exact 80-capture C/D2 fragment and its diagnostic authority."""

    return {
        "schema": 1,
        "document_kind": FRAGMENT_KIND,
        "fixture_sha256": fragment.fixture_sha256,
        "plan_identity": _identity_document(fragment.plan_identity),
        "arms": {
            item.arm: {
                "c_i": cohort_document(item.c_i),
                "d2_i": cohort_document(item.d2_i),
            }
            for item in fragment.arms
        },
        "accepted_observation_count": 80,
        "source_disjointness_verified_within_fragment": True,
        "topology_limitation": {
            "code": fragment.topology_limitation_code,
            "reason": TOPOLOGY_LIMITATION_REASON,
            "closure_authority": fragment.closure_authority,
            "diagnostic_only": True,
            "closure_claim_permitted": fragment.closure_claim_permitted,
        },
    }


def assemble_closure_campaign(
    *,
    fixture: ValidatedArmPreservingFixture,
    fragment: ArmPreservingCAndD2Fragment,
    global_h_c: ClosureCohort,
    observed_e: ClosureCohort,
    d1_by_arm: Mapping[str, ClosureCohort],
) -> ClosureCampaignEvidence:
    """Assemble full evidence for ``qualify_closure`` without weakening its checks."""

    if fragment.plan_identity != fixture.plan_identity or fragment.fixture_sha256 != (
        fixture.fixture_sha256
    ):
        raise ArmPreservingD2Error("C/D2 fragment differs from the fixture plan")
    _exact_keys(d1_by_arm, ARMS, "D1 cohorts")
    by_arm = {item.arm: item for item in fragment.arms}
    if tuple(by_arm) != ARMS:
        raise ArmPreservingD2Error("C/D2 fragment arms must be ANT1..ANT8 in order")
    return ClosureCampaignEvidence(
        plan_identity=fixture.plan_identity,
        global_h_c=global_h_c,
        observed_e=observed_e,
        arms=tuple(
            ArmClosureEvidence(
                arm=arm,
                c_i=by_arm[arm].c_i,
                d1_i=d1_by_arm[arm],
                d2_i=by_arm[arm].d2_i,
            )
            for arm in ARMS
        ),
        joint_weights=None,
    )


__all__ = [
    "ADC_CLIP_THRESHOLD_COUNTS",
    "ARMS",
    "ArmCAndD2Cohorts",
    "ArmPreservingCAndD2Fragment",
    "ArmPreservingD2Error",
    "BANDWIDTH_HZ",
    "CENTER_FREQUENCY_HZ",
    "DDS_SCALE",
    "FIXTURE_KIND",
    "FRAME_COUNT",
    "KERNEL_BUFFERS",
    "MINIMUM_REFERENCE_SNR_DB",
    "NormalizedArmObservation",
    "OBSERVATION_KIND",
    "RECEIVER_GAIN_DB",
    "ROLES",
    "SAMPLE_RATE_HZ",
    "SAMPLES_PER_FRAME",
    "SETUP_KIND",
    "SOURCE_PEAK_OUTPUT_BOUND_DBM",
    "TOPOLOGY_AUTHORITY",
    "TOPOLOGY_LIMITATION_CODE",
    "TOPOLOGY_LIMITATION_REASON",
    "TONE_OFFSET_HZ",
    "TOTAL_SAMPLES",
    "TX_HARDWARE_GAIN_DB",
    "ValidatedArmPreservingFixture",
    "assemble_closure_campaign",
    "build_c_d2_fragment",
    "build_fixture_v2",
    "canonical_json",
    "canonical_sha256",
    "cohort_document",
    "cohort_from_document",
    "complex_detection_document",
    "expected_setup_inventory",
    "fragment_document",
    "observation_to_repeat",
    "validate_fixture_v2",
    "validate_observation",
    "validate_setup_attestation",
]
