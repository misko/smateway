from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from smateway.fixture_v2 import (
    FIXTURE_KIND_V2,
    FULL_CONDUCTED_STAGE,
    SETUP_ATTESTATION_KIND,
    FixtureV2Error,
    canonical_sha256,
    fixture_identity_sets,
    sha256_path,
    validate_fixture_evidence,
    validate_fixture_manifest,
    validate_setup_attestation,
    validate_x_capture_linkage,
)

CAMPAIGN = "5p8-debug-r1"
GROUP = "full-simultaneous-r0"
BOARD = "stm32c011-4c0055000950313950363920"
SERIAL = "104000b29905000e17000800065934759d"


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path.absolute()


def _file(path: Path) -> dict[str, Any]:
    exact = path.absolute()
    return {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": exact.stat().st_size,
    }


def _uncharacterized() -> dict[str, Any]:
    return {
        "status": "uncharacterized",
        "evidence_path": None,
        "evidence_sha256": None,
        "s_parameter_sha256": None,
        "return_loss_db_at_5g8": None,
    }


def _asset(
    identity: str,
    ports: tuple[str, ...],
    **extra: float,
) -> dict[str, Any]:
    return {
        "id": identity,
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_power_dbm": 20.0,
        "port_map": {role: f"{identity}-{role}" for role in ports},
        "characterization": _uncharacterized(),
        **extra,
    }


def _interconnect(identity: str, kind: str = "coaxial_cable") -> dict[str, Any]:
    return {
        "id": identity,
        "kind": kind,
        "rated_min_frequency_hz": 2_000_000_000,
        "rated_max_frequency_hz": 8_000_000_000,
        "maximum_input_power_dbm": 20.0,
        "characterization": _uncharacterized(),
    }


def _connection(
    identity: str,
    source: tuple[str, str],
    destination: tuple[str, str],
    *,
    interconnect_id: str,
    kind: str = "coaxial_cable",
) -> dict[str, Any]:
    return {
        "id": identity,
        "from": {"component_id": source[0], "port_id": source[1]},
        "to": {"component_id": destination[0], "port_id": destination[1]},
        "interconnect": _interconnect(interconnect_id, kind),
    }


def _selector(
    *,
    control_ground: str = "selector-control-ground",
    supply_current_limit_a: float = 0.5,
) -> dict[str, Any]:
    return {
        **_asset("selector-v5-01", ("common", *(f"ANT{i}" for i in range(1, 9)))),
        "supply_voltage_v": 5.0,
        "supply_current_limit_a": supply_current_limit_a,
        "physical_board_id": BOARD,
        "hardware_revision": "pluto-rx2-8way-v5",
        "bench_supply_id": "bench-supply-01",
        "bench_supply_output_id": "bench-output-01",
        "power_positive_reference_id": "selector-positive-reference",
        "power_ground_reference_id": "selector-power-ground",
        "control_ground_reference_id": control_ground,
    }


def _shared(*, splitter_power_dbm: float = 20.0) -> dict[str, Any]:
    pluto = {
        "id": "pluto-plus-01",
        "serial": SERIAL,
        "port_map": {"tx1": "TX1", "tx2": "TX2", "rx1": "RX1", "rx2": "RX2"},
    }
    splitter = _asset("splitter-2way-01", ("input", "rx1_branch", "stimulus_branch"))
    splitter["maximum_input_power_dbm"] = splitter_power_dbm
    attenuator = _asset("rx1-attenuator-01", ("input", "output"), attenuation_db=30.0)
    tx2_load = _asset("tx2-load-01", ("load",), impedance_ohm=50.0)
    return {
        "pluto": pluto,
        "reference_planes": {
            "tx1": "tx1-reference-plane",
            "rx1": "rx1-reference-plane",
            "rx2": "rx2-reference-plane",
        },
        "tx1_reference_splitter": splitter,
        "rx1_attenuator": attenuator,
        "rx2_attenuator": {
            "state": "absent",
            "asset": None,
            "orientation": None,
            "pluto_connection": None,
        },
        "tx2_termination": tx2_load,
        "connections": {
            "tx1_to_splitter": _connection(
                "connection-tx1-to-splitter",
                ("pluto-plus-01", "TX1"),
                ("splitter-2way-01", "splitter-2way-01-input"),
                interconnect_id="cable-tx1-to-splitter",
            ),
            "splitter_to_rx1_attenuator": _connection(
                "connection-splitter-to-rx1-attenuator",
                ("splitter-2way-01", "splitter-2way-01-rx1_branch"),
                ("rx1-attenuator-01", "rx1-attenuator-01-input"),
                interconnect_id="cable-splitter-to-rx1-attenuator",
            ),
            "rx1_attenuator_to_rx1": _connection(
                "connection-rx1-attenuator-to-rx1",
                ("rx1-attenuator-01", "rx1-attenuator-01-output"),
                ("pluto-plus-01", "RX1"),
                interconnect_id="cable-rx1-attenuator-to-rx1",
            ),
            "tx2_to_termination": _connection(
                "connection-tx2-to-load",
                ("pluto-plus-01", "TX2"),
                ("tx2-load-01", "tx2-load-01-load"),
                interconnect_id="adapter-tx2-to-load",
                kind="direct_adapter",
            ),
        },
    }


def _stage_delta(stage: str, *, selector: dict[str, Any]) -> dict[str, Any]:
    connected = stage in {"powered_selector_all_inputs_terminated", FULL_CONDUCTED_STAGE}
    common_connection = _connection(
        "connection-rx2-common",
        ("pluto-plus-01", "RX2"),
        ("selector-v5-01", "selector-v5-01-common"),
        interconnect_id="cable-rx2-common",
    )
    stimulus_load = _asset("stimulus-load-01", ("load",), impedance_ohm=50.0)
    stimulus_connection = _connection(
        "connection-stimulus-to-load",
        ("splitter-2way-01", "splitter-2way-01-stimulus_branch"),
        ("stimulus-load-01", "stimulus-load-01-load"),
        interconnect_id="adapter-stimulus-to-load",
        kind="direct_adapter",
    )
    rx2_load = _asset("rx2-load-01", ("load",), impedance_ohm=50.0)
    if stage == "direct_rx2_termination":
        components = {
            "tx1_stimulus_termination": stimulus_load,
            "rx2_termination": rx2_load,
        }
        connections = {
            "splitter_stimulus_to_termination": stimulus_connection,
            "rx2_to_direct_termination": _connection(
                "connection-rx2-direct",
                ("pluto-plus-01", "RX2"),
                ("rx2-load-01", "rx2-load-01-load"),
                interconnect_id="adapter-rx2-direct",
                kind="direct_adapter",
            ),
        }
    elif stage == "rx2_cable_terminated":
        components = {
            "tx1_stimulus_termination": stimulus_load,
            "rx2_termination": rx2_load,
        }
        far = copy.deepcopy(common_connection)
        far["to"] = {"component_id": "rx2-load-01", "port_id": "rx2-load-01-load"}
        connections = {
            "splitter_stimulus_to_termination": stimulus_connection,
            "rx2_to_far_end_termination": far,
        }
    elif stage == "powered_selector_all_inputs_terminated":
        loads = {
            f"ANT{i}": _asset(f"selector-ant{i}-load", ("load",), impedance_ohm=50.0)
            for i in range(1, 9)
        }
        components = {
            "tx1_stimulus_termination": stimulus_load,
            "selector": copy.deepcopy(selector),
            "selector_input_terminations": loads,
        }
        connections = {
            "splitter_stimulus_to_termination": stimulus_connection,
            "rx2_to_selector_common": common_connection,
            **{
                f"selector_ant{i}_to_termination": _connection(
                    f"connection-selector-ant{i}-load",
                    ("selector-v5-01", f"selector-v5-01-ANT{i}"),
                    (f"selector-ant{i}-load", f"selector-ant{i}-load-load"),
                    interconnect_id=f"adapter-selector-ant{i}-load",
                    kind="direct_adapter",
                )
                for i in range(1, 9)
            },
        }
    else:
        eight_way = _asset("splitter-8way-01", ("input", *(f"ANT{i}" for i in range(1, 9))))
        components = {
            "eight_way_splitter": eight_way,
            "selector": copy.deepcopy(selector),
        }
        connections = {
            "splitter_stimulus_to_eight_way": _connection(
                "connection-stimulus-to-eight-way",
                ("splitter-2way-01", "splitter-2way-01-stimulus_branch"),
                ("splitter-8way-01", "splitter-8way-01-input"),
                interconnect_id="cable-stimulus-to-eight-way",
            ),
            "rx2_to_selector_common": common_connection,
            **{
                f"eight_way_ant{i}_to_selector_ant{i}": _connection(
                    f"connection-eight-way-ant{i}-selector-ant{i}",
                    ("splitter-8way-01", f"splitter-8way-01-ANT{i}"),
                    ("selector-v5-01", f"selector-v5-01-ANT{i}"),
                    interconnect_id=f"cable-eight-way-ant{i}-selector-ant{i}",
                )
                for i in range(1, 9)
            },
        }
    return {
        "schema": 1,
        "delta_id": f"delta-{stage}",
        "selector_rf_state": "rf_connected" if connected else "rf_disconnected",
        "selector_power_state": "bench_power_on" if connected else "bench_power_off",
        "selector_control_harness_state": (
            "connected_static_all_off" if connected else "disconnected"
        ),
        "components": components,
        "connections": connections,
    }


def _prior_source_binding(stage: str, plan_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage,
        "run_id": plan["plan_contract"]["run_id"],
        "plan_path": str(plan_path.absolute()),
        "plan_file_sha256": sha256_path(plan_path),
        "plan_contract_sha256": plan["plan_contract_sha256"],
        "fixture_evidence_sha256": plan["plan_contract"]["fixture_evidence_sha256"],
    }


def _selector_binding(directory: Path) -> dict[str, Any]:
    path = _write_json(directory / "selector-bench-evidence.json", {"sealed": True})
    return {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": str(path),
        "sha256": sha256_path(path),
        "campaign_id": CAMPAIGN,
        "run_id": "selector-bench-r01",
        "board_id": BOARD,
        "image_role": "bench",
    }


def _evidence_from_manifest(
    directory: Path,
    *,
    manifest_path: Path,
    stage: str,
    run_id: str,
    selector_binding: dict[str, Any],
) -> dict[str, Any]:
    manifest = validate_fixture_manifest(
        manifest_path,
        expected_stage=stage,
        expected_board_id=BOARD,
        expected_serial=SERIAL,
        verify_selector_file=False,
    )
    setup_evidence_path = directory / f"{run_id}.setup-evidence.txt"
    setup_evidence_path.write_text(f"observed {stage}\n", encoding="utf-8")
    fixture_selector = (
        selector_binding
        if stage
        in {
            "powered_selector_all_inputs_terminated",
            FULL_CONDUCTED_STAGE,
        }
        else None
    )
    selector_summary = (
        None
        if fixture_selector is None
        else {
            "path": fixture_selector["path"],
            "sha256": fixture_selector["sha256"],
            "run_id": fixture_selector["run_id"],
        }
    )
    setup_path = _write_json(
        directory / f"{run_id}.setup.json",
        {
            "schema": 1,
            "attestation_kind": SETUP_ATTESTATION_KIND,
            "attestation_id": f"setup-{run_id}",
            "created_at": "2026-08-30T09:00:00+00:00",
            "run_id": run_id,
            "campaign_id": CAMPAIGN,
            "comparable_fixture_group_id": GROUP,
            "stage": stage,
            "fixture_manifest_sha256": manifest.file_sha256,
            "shared_fixture_sha256": manifest.shared_fixture_sha256,
            "stage_delta_sha256": manifest.stage_delta_sha256,
            "observed_component_ids": list(manifest.component_ids),
            "observed_connection_ids": list(manifest.connection_ids),
            "selector_flash_evidence": selector_summary,
            "setup_evidence_path": str(setup_evidence_path.absolute()),
            "setup_evidence_sha256": sha256_path(setup_evidence_path),
        },
    )
    setup = validate_setup_attestation(
        setup_path,
        run_id=run_id,
        campaign_id=CAMPAIGN,
        comparable_fixture_group_id=GROUP,
        stage=stage,
        fixture_manifest_sha256=manifest.file_sha256,
        shared_fixture_sha256=manifest.shared_fixture_sha256,
        stage_delta_sha256=manifest.stage_delta_sha256,
        component_ids=manifest.component_ids,
        connection_ids=manifest.connection_ids,
        selector_flash_evidence=fixture_selector,
    )
    evidence = {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND_V2,
        "campaign_id": CAMPAIGN,
        "comparable_fixture_group_id": GROUP,
        "stage": stage,
        "run_id": run_id,
        "board_id": BOARD,
        "source_files": {
            "fixture_manifest": _file(manifest_path),
            "setup_attestation": _file(setup_path),
        },
        "shared_fixture": manifest.shared_fixture,
        "shared_fixture_sha256": manifest.shared_fixture_sha256,
        "stage_delta": manifest.stage_delta,
        "stage_delta_sha256": manifest.stage_delta_sha256,
        "prior_stage_binding": manifest.prior_stage_binding,
        "setup_attestation": setup,
        "selector_flash_evidence": fixture_selector,
        "component_ids": list(manifest.component_ids),
        "connection_ids": list(manifest.connection_ids),
        "characterization_summary": manifest.characterization_summary,
    }
    return validate_fixture_evidence(
        evidence,
        expected_stage=stage,
        expected_run_id=run_id,
        expected_board_id=BOARD,
        expected_serial=SERIAL,
    )


def _plan(
    directory: Path,
    *,
    stage: str,
    run_id: str,
    evidence: dict[str, Any],
    selector_binding: dict[str, Any],
    selector_control: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    frozen_selector_control = (
        copy.deepcopy(
            selector_control
            if selector_control is not None
            else {"selector_flash_evidence": selector_binding}
        )
        if stage in {"powered_selector_all_inputs_terminated", FULL_CONDUCTED_STAGE}
        else None
    )
    contract = {
        "run_id": run_id,
        "topology_stage": stage,
        "board_id": BOARD,
        "configuration": {"serial": SERIAL},
        "fixture_evidence": evidence,
        "fixture_evidence_sha256": canonical_sha256(evidence),
        "selector_control": frozen_selector_control,
    }
    envelope = {
        "schema": 1,
        "plan_contract": contract,
        "plan_contract_sha256": canonical_sha256(contract),
        "plan_contract_hash_provenance": (
            "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
        ),
        "immutable": True,
    }
    path = _write_json(directory / f"{run_id}.plan.json", envelope)
    return path, envelope


def _chain(
    directory: Path,
    *,
    splitter_power_dbm: float = 20.0,
    control_ground: str = "selector-control-ground",
    supply_current_limit_a: float = 0.5,
    run_prefix: str = "chain",
    selector_binding: dict[str, Any] | None = None,
    selector_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    flash = copy.deepcopy(
        selector_binding if selector_binding is not None else _selector_binding(directory)
    )
    if selector_control is not None:
        control_flash = selector_control.get("selector_flash_evidence")
        if control_flash != flash:
            raise ValueError("injected selector control must bind the injected selector evidence")
    shared = _shared(splitter_power_dbm=splitter_power_dbm)
    selector = _selector(
        control_ground=control_ground,
        supply_current_limit_a=supply_current_limit_a,
    )
    result: dict[str, Any] = {
        "selector": flash,
        "selector_control": copy.deepcopy(selector_control),
    }
    prior_plan_path: Path | None = None
    prior_plan: dict[str, Any] | None = None
    prior_stage: str | None = None
    for index, stage in enumerate(
        (
            "direct_rx2_termination",
            "rx2_cable_terminated",
            "powered_selector_all_inputs_terminated",
            FULL_CONDUCTED_STAGE,
        ),
        start=1,
    ):
        prior_binding = (
            None
            if prior_plan_path is None or prior_plan is None or prior_stage is None
            else _prior_source_binding(prior_stage, prior_plan_path, prior_plan)
        )
        manifest_path = _write_json(
            directory / f"{stage}.fixture.json",
            {
                "schema": 2,
                "fixture_kind": FIXTURE_KIND_V2,
                "campaign_id": CAMPAIGN,
                "comparable_fixture_group_id": GROUP,
                "stage": stage,
                "board_id": BOARD,
                "shared_fixture": copy.deepcopy(shared),
                "stage_delta": _stage_delta(stage, selector=selector),
                "prior_stage_binding": prior_binding,
            },
        )
        run_id = f"{run_prefix}-{index}-{stage}"
        evidence = _evidence_from_manifest(
            directory,
            manifest_path=manifest_path,
            stage=stage,
            run_id=run_id,
            selector_binding=flash,
        )
        plan_path, plan = _plan(
            directory,
            stage=stage,
            run_id=run_id,
            evidence=evidence,
            selector_binding=flash,
            selector_control=selector_control,
        )
        result[stage] = {
            "manifest": manifest_path,
            "evidence": evidence,
            "plan": plan_path,
        }
        prior_plan_path = plan_path
        prior_plan = plan
        prior_stage = stage
    return result


def _capture_binding(chain: dict[str, Any]) -> dict[str, Any]:
    path = Path(chain[FULL_CONDUCTED_STAGE]["manifest"])
    return {
        "fixture_id": GROUP,
        "fixture_manifest_path": str(path),
        "fixture_manifest_sha256": sha256_path(path),
        "board_id": BOARD,
        "pluto_serial": SERIAL,
    }


def test_complete_production_a_b_c_e_chain_and_evidence_are_green(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")

    for stage in (
        "direct_rx2_termination",
        "rx2_cable_terminated",
        "powered_selector_all_inputs_terminated",
        FULL_CONDUCTED_STAGE,
    ):
        manifest = validate_fixture_manifest(
            Path(chain[stage]["manifest"]),
            expected_stage=stage,
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )
        evidence = validate_fixture_evidence(
            chain[stage]["evidence"],
            expected_stage=stage,
            expected_run_id=chain[stage]["evidence"]["run_id"],
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )
        components, connections = fixture_identity_sets(
            manifest.shared_fixture, manifest.stage_delta
        )
        assert components == evidence["component_ids"]
        assert connections == evidence["connection_ids"]


def test_raw_manifest_rejects_nonproduction_selector_leaf(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    source = Path(chain[FULL_CONDUCTED_STAGE]["manifest"])
    document = json.loads(source.read_text(encoding="utf-8"))
    document["stage_delta"]["components"]["selector"]["shield_lid"] = "installed"
    invalid = _write_json(tmp_path / "invalid-shield.fixture.json", document)

    with pytest.raises(FixtureV2Error, match="selector fields are incomplete or unexpected"):
        validate_fixture_manifest(
            invalid,
            expected_stage=FULL_CONDUCTED_STAGE,
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )


def test_stage_c_rejects_incomplete_synthetic_topology(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    source = Path(chain["powered_selector_all_inputs_terminated"]["manifest"])
    document = json.loads(source.read_text(encoding="utf-8"))
    del document["stage_delta"]["components"]["selector_input_terminations"]
    invalid = _write_json(tmp_path / "incomplete-c.fixture.json", document)

    with pytest.raises(FixtureV2Error, match="Stage C components are incomplete"):
        validate_fixture_manifest(
            invalid,
            expected_stage="powered_selector_all_inputs_terminated",
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )


def test_fixture_evidence_rejects_opaque_setup_and_missing_projection_fields(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "chain")
    stage = "direct_rx2_termination"
    evidence = copy.deepcopy(chain[stage]["evidence"])
    setup_path = Path(evidence["source_files"]["setup_attestation"]["path"])
    setup_path.write_text("{}\n", encoding="utf-8")
    evidence["source_files"]["setup_attestation"] = _file(setup_path)
    evidence["setup_attestation"] = {}

    with pytest.raises(FixtureV2Error, match="per-run setup attestation fields"):
        validate_fixture_evidence(
            evidence,
            expected_stage=stage,
            expected_run_id=evidence["run_id"],
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )


def test_raw_manifest_rejects_tampered_immediate_prior_plan_binding(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    stage = "rx2_cable_terminated"
    source = Path(chain[stage]["manifest"])
    document = json.loads(source.read_text(encoding="utf-8"))
    document["prior_stage_binding"]["plan_file_sha256"] = "0" * 64
    invalid = _write_json(tmp_path / "invalid-prior.fixture.json", document)

    with pytest.raises(FixtureV2Error, match="prior-stage plan file differs"):
        validate_fixture_manifest(
            invalid,
            expected_stage=stage,
            expected_board_id=BOARD,
            expected_serial=SERIAL,
        )


def test_x_capture_linkage_accepts_exact_boundary_and_full_graphs(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    capture = _capture_binding(chain)

    for stage in (
        "direct_rx2_termination",
        "powered_selector_all_inputs_terminated",
        FULL_CONDUCTED_STAGE,
    ):
        normalized = validate_x_capture_linkage(
            chain[stage]["evidence"],
            capture_fixture_binding=capture,
            context_selector_flash_evidence=chain["selector"],
        )
        assert normalized["stage"] == stage


def test_x_boundary_rejects_different_shared_physical_graph(tmp_path: Path) -> None:
    capture_chain = _chain(tmp_path / "capture")
    substituted_chain = _chain(tmp_path / "substituted", splitter_power_dbm=21.0)

    with pytest.raises(FixtureV2Error, match="exact capture-state physical graph"):
        validate_x_capture_linkage(
            substituted_chain["powered_selector_all_inputs_terminated"]["evidence"],
            capture_fixture_binding=_capture_binding(capture_chain),
            context_selector_flash_evidence=substituted_chain["selector"],
        )


def test_x_stage_c_rejects_selector_different_from_capture_e(tmp_path: Path) -> None:
    capture_chain = _chain(tmp_path / "capture")
    changed_chain = _chain(tmp_path / "changed", control_ground="different-control-ground")

    with pytest.raises(FixtureV2Error, match="selector boundary"):
        validate_x_capture_linkage(
            changed_chain["powered_selector_all_inputs_terminated"]["evidence"],
            capture_fixture_binding=_capture_binding(capture_chain),
            context_selector_flash_evidence=changed_chain["selector"],
        )


def test_x_full_role_rejects_byte_identical_relocated_fixture_source(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    stage = FULL_CONDUCTED_STAGE
    evidence = copy.deepcopy(chain[stage]["evidence"])
    original = Path(evidence["source_files"]["fixture_manifest"]["path"])
    copied = tmp_path / "relocated-full.fixture.json"
    copied.write_bytes(original.read_bytes())
    evidence["source_files"]["fixture_manifest"] = _file(copied)

    with pytest.raises(FixtureV2Error, match="capture-revision manifest"):
        validate_x_capture_linkage(
            evidence,
            capture_fixture_binding=_capture_binding(chain),
            context_selector_flash_evidence=chain["selector"],
        )


def test_full_e_rejects_selector_substitution_after_bound_stage_c(tmp_path: Path) -> None:
    chain = _chain(tmp_path / "chain")
    alternate_path = _write_json(tmp_path / "chain" / "selector-alternate.json", {"sealed": "alt"})
    alternate = copy.deepcopy(chain["selector"])
    alternate.update(
        path=str(alternate_path),
        sha256=sha256_path(alternate_path),
        run_id="selector-alternate-r01",
    )
    stage = FULL_CONDUCTED_STAGE
    run_id = chain[stage]["evidence"]["run_id"]

    with pytest.raises(
        FixtureV2Error,
        match="differs from the immediately prior Stage-C plan",
    ):
        _evidence_from_manifest(
            tmp_path / "chain",
            manifest_path=Path(chain[stage]["manifest"]),
            stage=stage,
            run_id=run_id,
            selector_binding=alternate,
        )
