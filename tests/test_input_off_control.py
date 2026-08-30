from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from smateway.input_off_control import (
    COMPONENT_ROLES,
    FIXED_CONNECTION_ROLES,
    OBSERVATION_KIND,
    P2_CONNECTION_ROLES,
    REFERENCE_PLANE_ROLES,
    TOPOLOGY_TOKEN,
    InputOffContractError,
    acquisition_contract,
    coherent_tone_snr_db,
    compare_p0_p2_cohorts,
    phase_free_complex_upper_bound,
    validate_fixture_v2,
    validate_observation,
    validate_setup_attestation,
)

RUN_ID = "5p8-debug-r1-p2-input-off-r01-20260829"
BOARD_ID = "board-a"
SERIAL = "serial-a"
PROFILE_SHA = "a" * 64
FIXED_GRAPH_SHA = "b" * 64
GROUP_ID = "fixture-group-a"


def _uncharacterized() -> dict[str, Any]:
    return {
        "status": "uncharacterized",
        "evidence_path": None,
        "evidence_sha256": None,
        "s_parameter_sha256": None,
        "return_loss_db_at_5g8": None,
    }


def _component(role: str, ports: dict[str, str]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": f"{role}-id",
        "kind": role,
        "manufacturer": "synthetic",
        "model": f"model-{role}",
        "ports": ports,
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_power_dbm": 10.0,
        "characterization": _uncharacterized(),
    }
    if role == "pluto":
        value["serial"] = SERIAL
    elif role.endswith("termination"):
        value["impedance_ohm"] = 50.0
    elif role == "rx1_attenuator":
        value.update({"attenuation_db": 30.0, "orientation": "input_to_output"})
    elif role == "rx2_attenuator":
        value.update({"attenuation_db": 20.0, "orientation": "input_toward_fixture"})
    elif role == "selector":
        value.update({"board_id": BOARD_ID, "hardware_revision": "v5"})
    return value


def _interconnect(role: str) -> dict[str, Any]:
    return {
        "id": f"{role}-interconnect",
        "kind": "coaxial_cable",
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_power_dbm": 10.0,
        "characterization": _uncharacterized(),
    }


def _connection(
    role: str,
    source: tuple[str, str],
    destination: tuple[str, str],
) -> dict[str, Any]:
    return {
        "id": f"{role}-connection",
        "from": {"component_role": source[0], "port_role": source[1]},
        "to": {"component_role": destination[0], "port_role": destination[1]},
        "interconnect": _interconnect(role),
    }


def _file(path: str, digest: str) -> dict[str, Any]:
    return {"path": path, "sha256": digest, "size_bytes": 1}


def fixture_document() -> dict[str, Any]:
    component_ports = {
        "pluto": {"tx1": "TX1", "tx2": "TX2", "rx1": "RX1", "rx2": "RX2"},
        "two_way_splitter": {
            "input": "IN",
            "reference_output": "OUT1",
            "stimulus_output": "OUT2",
        },
        "rx1_attenuator": {"input": "IN", "output": "OUT"},
        "tx1_stimulus_termination": {"load": "LOAD"},
        "eight_way_input_termination": {"load": "LOAD"},
        "tx2_termination": {"load": "LOAD"},
        "eight_way_splitter": {
            "input": "IN",
            **{f"f{index}": f"F{index}" for index in range(1, 9)},
        },
        "selector": {
            "common": "COMMON",
            **{f"ant{index}": f"ANT{index}" for index in range(1, 9)},
        },
    }
    components = {role: _component(role, component_ports[role]) for role in COMPONENT_ROLES}
    connections = {
        "tx1_to_two_way": _connection(
            "tx1_to_two_way", ("pluto", "tx1"), ("two_way_splitter", "input")
        ),
        "two_way_reference_to_rx1_attenuator": _connection(
            "two_way_reference_to_rx1_attenuator",
            ("two_way_splitter", "reference_output"),
            ("rx1_attenuator", "input"),
        ),
        "rx1_attenuator_to_rx1": _connection(
            "rx1_attenuator_to_rx1", ("rx1_attenuator", "output"), ("pluto", "rx1")
        ),
        "tx2_to_termination": _connection(
            "tx2_to_termination", ("pluto", "tx2"), ("tx2_termination", "load")
        ),
        "selector_common_to_rx2": _connection(
            "selector_common_to_rx2", ("selector", "common"), ("pluto", "rx2")
        ),
        "two_way_stimulus_to_termination": _connection(
            "two_way_stimulus_to_termination",
            ("two_way_splitter", "stimulus_output"),
            ("tx1_stimulus_termination", "load"),
        ),
        "eight_way_input_to_termination": _connection(
            "eight_way_input_to_termination",
            ("eight_way_splitter", "input"),
            ("eight_way_input_termination", "load"),
        ),
        **{
            f"eight_way_f{index}_to_selector_ant{index}": _connection(
                f"eight_way_f{index}_to_selector_ant{index}",
                ("eight_way_splitter", f"f{index}"),
                ("selector", f"ant{index}"),
            )
            for index in range(1, 9)
        },
    }
    assert set(connections) == set(P2_CONNECTION_ROLES)
    return {
        "schema": 2,
        "fixture_kind": "5g8_input_drive_off_fixture_v2",
        "campaign_id": "5p8-debug-r1",
        "comparable_fixture_group_id": GROUP_ID,
        "topology_stage": "input_drive_off",
        "topology_token": TOPOLOGY_TOKEN,
        "run_id": RUN_ID,
        "board_id": BOARD_ID,
        "pluto_serial": SERIAL,
        "reference_planes": {role: f"{role}-plane" for role in REFERENCE_PLANE_ROLES},
        "components": components,
        "rx2_attenuator": {
            "state": "absent",
            "component": None,
            "pluto_connection": None,
        },
        "connections": connections,
        "declared_p0_to_p2_delta": {
            "removed_connection": {
                "id": "p0-stimulus-to-eight-way",
                "from": {
                    "component_role": "two_way_splitter",
                    "port_role": "stimulus_output",
                },
                "to": {"component_role": "eight_way_splitter", "port_role": "input"},
            },
            "added_connection_roles": [
                "two_way_stimulus_to_termination",
                "eight_way_input_to_termination",
            ],
            "unchanged_connection_roles": list(FIXED_CONNECTION_ROLES),
            "no_other_component_or_connection_moved": True,
        },
        "baseline_topology_evidence": _file("/evidence/p0.png", "1" * 64),
        "fast20_control": {
            "mode": "autonomous_fast20_schedule",
            "profile": _file("/evidence/profile.json", "2" * 64),
            "live_image_evidence": _file("/evidence/fast20.json", "3" * 64),
        },
    }


def setup_document(fixture: dict[str, Any], *, fixture_sha: str = "4" * 64) -> dict[str, Any]:
    normalized = validate_fixture_v2(
        fixture,
        run_id=RUN_ID,
        board_id=BOARD_ID,
        serial=SERIAL,
    )
    confirmations = {
        "no_antennas": True,
        "tx1_matched_two_way_still_feeds_protected_rx1": True,
        "tx1_stimulus_branch_has_own_rated_50ohm_load": True,
        "eight_way_input_has_separate_rated_50ohm_load": True,
        "two_loads_and_reference_planes_are_distinct": True,
        "all_eight_way_outputs_unchanged": True,
        "selector_and_rx2_common_cable_unchanged": True,
        "rx1_chain_unchanged": True,
        "tx2_terminated_and_muted": True,
        "fast20_live_and_unchanged": True,
        "no_other_component_or_connection_moved_since_p0_evidence": True,
    }
    return {
        "schema": 1,
        "attestation_kind": "5g8_input_drive_off_run_setup_v1",
        "attestation_id": "setup-a",
        "created_at": "2026-08-29T12:00:00+00:00",
        "operator_id": "operator-a",
        "run_id": RUN_ID,
        "campaign_id": "5p8-debug-r1",
        "topology_stage": "input_drive_off",
        "fixture_manifest_sha256": fixture_sha,
        "observed_component_ids": normalized["component_ids"],
        "observed_connection_ids": normalized["connection_ids"],
        "setup_evidence": _file("/evidence/setup.png", "5" * 64),
        "confirmations": confirmations,
    }


def observation(
    cohort: str,
    index: int,
    transfer: complex,
    *,
    reference: float = 100.0,
    fixture_hash: str | None = None,
) -> dict[str, Any]:
    hardened = cohort == "P2"
    return {
        "schema": 1,
        "observation_kind": OBSERVATION_KIND,
        "cohort": cohort,
        "run_id": f"{cohort.lower()}-run-{index}",
        "artifact": {
            "artifact_id": f"{cohort.lower()}-artifact-{index}",
            "stream_id": (1000 if hardened else 100) + index,
            "sha256": f"{index + (20 if hardened else 10):064x}",
        },
        "acquisition": acquisition_contract(),
        "profile_contract_sha256": PROFILE_SHA,
        "analysis": {
            "transfer_detected": True,
            "all_off_transfer": {"real": transfer.real, "imag": transfer.imag},
            "all_off_transfer_upper_bound": None,
            "rx1_reference_amplitude": reference,
            "detected_pilot_snr_db": 35.0,
        },
        "quality": {
            "passed": True,
            "continuity_verified": True,
            "metadata_abi": 2,
            "headroom_passed": True,
            "final_mute_passed": True,
            "fast20_schedule_verified": True,
            "central_all_off_windows_used": True,
        },
        "provenance": {
            "source_commit": "6" * 40,
            "source_files_sha256": "7" * 64 if hardened else None,
            "native_attestation_sha256": "8" * 64 if hardened else None,
            "fixture_evidence_sha256": (fixture_hash or f"{index + 40:064x}" if hardened else None),
            "fixture_fixed_graph_sha256": FIXED_GRAPH_SHA if hardened else None,
            "comparable_fixture_group_id": GROUP_ID if hardened else None,
        },
    }


def test_exact_two_load_fixture_and_setup_are_admitted() -> None:
    raw = fixture_document()
    normalized = validate_fixture_v2(raw, run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL)
    setup = setup_document(raw)
    observed = validate_setup_attestation(
        setup,
        fixture=normalized,
        fixture_file_sha256="4" * 64,
        run_id=RUN_ID,
    )
    assert normalized["topology_token"] == TOPOLOGY_TOKEN
    assert (
        normalized["components"]["tx1_stimulus_termination"]["id"]
        != normalized["components"]["eight_way_input_termination"]["id"]
    )
    assert observed["confirmations"]["all_eight_way_outputs_unchanged"] is True


def test_present_rx2_attenuator_is_part_of_fixed_graph_and_inventory() -> None:
    raw = fixture_document()
    component = _component("rx2_attenuator", {"input": "IN", "output": "OUT"})
    raw["rx2_attenuator"] = {
        "state": "present",
        "component": component,
        "pluto_connection": _connection(
            "rx2_attenuator_to_pluto",
            ("rx2_attenuator", "output"),
            ("pluto", "rx2"),
        ),
    }
    raw["connections"]["selector_common_to_rx2"]["to"] = {
        "component_role": "rx2_attenuator",
        "port_role": "input",
    }

    normalized = validate_fixture_v2(
        raw,
        run_id=RUN_ID,
        board_id=BOARD_ID,
        serial=SERIAL,
    )

    assert normalized["rx2_attenuator"]["state"] == "present"
    assert component["id"] in normalized["component_ids"]
    assert "rx2_attenuator_to_pluto-connection" in normalized["connection_ids"]
    absent_hash = validate_fixture_v2(
        fixture_document(), run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL
    )["fixed_graph_sha256"]
    assert normalized["fixed_graph_sha256"] != absent_hash


@pytest.mark.parametrize(
    "bad_state",
    (
        {"state": "present", "component": None, "pluto_connection": None},
        {"state": "absent", "component": {"id": "forged"}, "pluto_connection": None},
        {
            "state": "REPLACE_RX2_ATTENUATOR_STATE_PRESENT_OR_ABSENT",
            "component": None,
            "pluto_connection": None,
        },
    ),
)
def test_rx2_attenuator_state_fails_closed(bad_state: dict[str, Any]) -> None:
    raw = fixture_document()
    raw["rx2_attenuator"] = bad_state
    with pytest.raises(InputOffContractError):
        validate_fixture_v2(raw, run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL)


@pytest.mark.parametrize("mutation", ("same_load", "same_plane", "moved_edge", "wrong_rating"))
def test_fixture_rejects_false_input_off_claims(mutation: str) -> None:
    raw = fixture_document()
    if mutation == "same_load":
        raw["components"]["eight_way_input_termination"]["id"] = raw["components"][
            "tx1_stimulus_termination"
        ]["id"]
    elif mutation == "same_plane":
        raw["reference_planes"]["eight_way_input_termination_load"] = raw["reference_planes"][
            "tx1_stimulus_termination_load"
        ]
    elif mutation == "moved_edge":
        raw["declared_p0_to_p2_delta"]["unchanged_connection_roles"] = list(
            FIXED_CONNECTION_ROLES[:-1]
        )
    else:
        raw["components"]["eight_way_input_termination"]["rated_max_frequency_hz"] = 5_000_000_000
    with pytest.raises(InputOffContractError):
        validate_fixture_v2(raw, run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL)


def test_setup_rejects_unconfirmed_downstream_stability() -> None:
    raw = fixture_document()
    normalized = validate_fixture_v2(raw, run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL)
    setup = setup_document(raw)
    setup["confirmations"]["selector_and_rx2_common_cable_unchanged"] = False
    with pytest.raises(InputOffContractError, match="every physical P2"):
        validate_setup_attestation(
            setup,
            fixture=normalized,
            fixture_file_sha256="4" * 64,
            run_id=RUN_ID,
        )


@pytest.mark.parametrize(
    "created_at",
    (
        "2026-08-29T12:00:00",
        "2026-08-29 12:00:00",
        "not-a-time",
        "",
    ),
)
def test_setup_requires_timezone_aware_iso_timestamp(created_at: str) -> None:
    raw = fixture_document()
    normalized = validate_fixture_v2(raw, run_id=RUN_ID, board_id=BOARD_ID, serial=SERIAL)
    setup = setup_document(raw)
    setup["created_at"] = created_at
    with pytest.raises(InputOffContractError, match="UTC offset|timezone-aware"):
        validate_setup_attestation(
            setup,
            fixture=normalized,
            fixture_file_sha256="4" * 64,
            run_id=RUN_ID,
        )


def test_observation_rejects_nonmatching_p0_acquisition_and_missing_native() -> None:
    p0 = observation("P0", 1, 0.05 + 0.01j)
    p0["acquisition"]["receiver_gain_db"] = 60.0
    with pytest.raises(InputOffContractError, match="acquisition differs"):
        validate_observation(p0)
    p2 = observation("P2", 1, 0.01 + 0.002j)
    p2["provenance"]["native_attestation_sha256"] = None
    with pytest.raises(InputOffContractError, match="hardened"):
        validate_observation(p2)


def _cohort(
    scale: float, *, reference_db: float = 0.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base = 0.05 + 0.02j
    perturbations = (0.99 - 0.004j, 1.01 + 0.002j, 1.0 + 0j, 0.995 + 0.003j, 1.005 - 0.002j)
    p0 = [observation("P0", index, base * value) for index, value in enumerate(perturbations, 1)]
    reference = 100.0 * 10.0 ** (reference_db / 20.0)
    p2 = [
        observation("P2", index, base * scale * value, reference=reference)
        for index, value in enumerate(perturbations, 1)
    ]
    return p0, p2


def test_independent_bootstrap_classifies_collapse_and_not_collapsed() -> None:
    p0, p2 = _cohort(0.20)
    collapsed = compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=4096, seed=4)
    assert collapsed["disposition"] == "input_drive_required"
    assert collapsed["transfer_magnitude_ratio"]["confidence_interval_95"][1] <= 0.31623
    assert collapsed["rx1_reference_difference_db"]["stable"] is True

    p0, p2 = _cohort(0.90)
    retained = compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=4096, seed=4)
    assert retained["disposition"] == "not_collapsed"
    assert retained["transfer_magnitude_ratio"]["confidence_interval_95"][0] >= 0.70795


def test_reference_interval_not_point_estimate_controls_disposition() -> None:
    p0, p2 = _cohort(0.20)
    for index, item in enumerate(p2):
        item["analysis"]["rx1_reference_amplitude"] = 100.0 * 10 ** (
            (-1.4 if index < 2 else 0.6) / 20.0
        )
    result = compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=8192, seed=9)
    assert abs(result["rx1_reference_difference_db"]["point_estimate"]) < 1.0
    assert result["rx1_reference_difference_db"]["stable"] is False
    assert result["disposition"] == "inconclusive"


def test_cohort_rejects_reused_stream_and_noncomparable_graph() -> None:
    p0, p2 = _cohort(0.2)
    p2[1]["artifact"]["stream_id"] = p2[0]["artifact"]["stream_id"]
    with pytest.raises(InputOffContractError, match="stream IDs"):
        compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=1000)
    p0, p2 = _cohort(0.2)
    p2[1]["provenance"]["fixture_fixed_graph_sha256"] = "c" * 64
    with pytest.raises(InputOffContractError, match="fixed downstream graph"):
        compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=1000)


def test_p2_nondetection_retains_phase_free_bound_without_fake_complex_phase() -> None:
    p0, p2 = _cohort(0.2)
    p2[0]["analysis"].update(
        {
            "transfer_detected": False,
            "all_off_transfer": None,
            "all_off_transfer_upper_bound": 0.012,
        }
    )
    observed = validate_observation(p2[0], expected_cohort="P2")
    assert observed.all_off_transfer is None
    assert observed.all_off_transfer_upper_bound == 0.012
    result = compare_p0_p2_cohorts(p0, p2, bootstrap_replicates=4096, seed=18)
    assert result["disposition"] == "inconclusive"
    assert result["p2"]["phase_free_nondetection_count"] == 1
    assert result["transfer_magnitude_ratio"]["confidence_interval_95"] is None


def test_phase_free_upper_bound_is_positive_and_above_observed_center() -> None:
    rng = np.random.default_rng(21)
    values = 0.002 + 0.001j + (rng.normal(0.0, 0.003, 25) + 1j * rng.normal(0.0, 0.003, 25))
    bound = phase_free_complex_upper_bound(values, bootstrap_replicates=4096, seed=2)
    observed_center = complex(float(np.median(values.real)), float(np.median(values.imag)))
    assert bound > abs(observed_center)


def test_shared_coherent_pilot_snr_estimator_detects_exact_tone() -> None:
    sample_rate = 1_000_000.0
    tone_hz = 100_000.0
    count = 100_000
    rng = np.random.default_rng(12)
    phase = 2.0 * np.pi * tone_hz * np.arange(count) / sample_rate
    samples = 10.0 * np.exp(1j * phase) + (
        rng.normal(0.0, 0.1, count) + 1j * rng.normal(0.0, 0.1, count)
    )
    snr = coherent_tone_snr_db(samples, sample_rate_hz=sample_rate, tone_hz=tone_hz)
    assert snr == pytest.approx(36.99, abs=0.15)
    assert math.isfinite(snr)
