from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts/run_5g8_leakage_ladder.py"
SPEC = importlib.util.spec_from_file_location("fixture_template_leakage_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
leakage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = leakage
SPEC.loader.exec_module(leakage)
GENERATOR_SCRIPT = REPOSITORY / "scripts/generate_5g8_fixture_manifest.py"
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "fixture_template_generator_under_test",
    GENERATOR_SCRIPT,
)
assert GENERATOR_SPEC is not None and GENERATOR_SPEC.loader is not None
generator = importlib.util.module_from_spec(GENERATOR_SPEC)
sys.modules[GENERATOR_SPEC.name] = generator
GENERATOR_SPEC.loader.exec_module(generator)
DOCS = REPOSITORY / "docs/5g8_root_cause_analysis"
STAGES = {
    "a": "direct_rx2_termination",
    "b": "rx2_cable_terminated",
    "c": "powered_selector_all_inputs_terminated",
    "e": "full_conducted_fixture",
}
EXPECTED_FIXTURE_FIELDS = {
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
EXPECTED_SETUP_FIELDS = {
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
EXPECTED_GRAPH_ROLES = {
    "a": (
        {"tx1_stimulus_termination", "rx2_termination"},
        {"splitter_stimulus_to_termination", "rx2_to_direct_termination"},
    ),
    "b": (
        {"tx1_stimulus_termination", "rx2_termination"},
        {"splitter_stimulus_to_termination", "rx2_to_far_end_termination"},
    ),
    "c": (
        {"tx1_stimulus_termination", "selector", "selector_input_terminations"},
        {
            "splitter_stimulus_to_termination",
            "rx2_to_selector_common",
            *(f"selector_ant{index}_to_termination" for index in range(1, 9)),
        },
    ),
    "e": (
        {"eight_way_splitter", "selector"},
        {
            "splitter_stimulus_to_eight_way",
            "rx2_to_selector_common",
            *(f"eight_way_ant{index}_to_selector_ant{index}" for index in range(1, 9)),
        },
    ),
}
NUMERIC_PLACEHOLDERS: dict[str, float] = {
    "REPLACE_OPERATOR_OBSERVED_RATED_MIN_FREQUENCY_HZ": 2_000_000_000.0,
    "REPLACE_OPERATOR_OBSERVED_RATED_MAX_FREQUENCY_HZ": 8_000_000_000.0,
    "REPLACE_OPERATOR_OBSERVED_MAXIMUM_INPUT_POWER_DBM": 20.0,
    "REPLACE_OPERATOR_OBSERVED_RX1_ATTENUATION_DB": 30.0,
    "REPLACE_OPERATOR_OBSERVED_IMPEDANCE_OHM": 50.0,
    "REPLACE_OPERATOR_OBSERVED_SUPPLY_VOLTAGE_V": 5.0,
    "REPLACE_OPERATOR_OBSERVED_SUPPLY_CURRENT_LIMIT_A": 0.5,
}
SENSITIVE_NUMERIC_FIELDS = frozenset(
    {
        "rated_min_frequency_hz",
        "rated_max_frequency_hz",
        "maximum_input_power_dbm",
        "attenuation_db",
        "impedance_ohm",
        "supply_voltage_v",
        "supply_current_limit_a",
    }
)


def _load(name: str) -> dict[str, Any]:
    value = json.loads((DOCS / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _replace_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item) for item in value]
    if isinstance(value, str) and value in NUMERIC_PLACEHOLDERS:
        return NUMERIC_PLACEHOLDERS[value]
    if value == "REPLACE_RX2_ATTENUATOR_STATE_PRESENT_OR_ABSENT":
        return "absent"
    if isinstance(value, str) and value.startswith("REPLACE_"):
        # This monotonic substitution preserves the template inventories' lexical sort.
        return f"example_{value.removeprefix('REPLACE_').lower()}"
    return value


def _assert_sensitive_values_are_operator_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SENSITIVE_NUMERIC_FIELDS:
                assert isinstance(item, str)
                assert item.startswith("REPLACE_OPERATOR_OBSERVED_")
            _assert_sensitive_values_are_operator_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            _assert_sensitive_values_are_operator_placeholders(item)


def _normalized_fixture(label: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    stage = STAGES[label]
    raw = _replace_placeholders(_load(f"fixture_manifest_v2.stage-{label}.template.json"))
    assert set(raw) == EXPECTED_FIXTURE_FIELDS
    assert raw["schema"] == 2
    assert raw["fixture_kind"] == leakage.FIXTURE_KIND_V2
    assert raw["stage"] == stage
    assert raw["prior_stage_binding"] is None
    shared = leakage._normalize_shared_fixture(
        raw["shared_fixture"],
        expected_serial=raw["shared_fixture"]["pluto"]["serial"],
        base_directory=DOCS,
        verify_files=False,
    )
    delta = leakage._normalize_stage_delta(
        raw["stage_delta"],
        stage=stage,
        shared=shared,
        base_directory=DOCS,
        verify_files=False,
    )
    return raw, shared, delta


@pytest.mark.parametrize("label", STAGES)
def test_fixture_draft_template_normalizes_with_exact_stage_graph(label: str) -> None:
    _raw, shared, delta = _normalized_fixture(label)
    expected_components, expected_connections = EXPECTED_GRAPH_ROLES[label]

    assert set(delta["components"]) == expected_components
    assert set(delta["connections"]) == expected_connections
    component_ids, connection_ids = leakage._fixture_identity_sets(shared, delta)
    assert component_ids == sorted(component_ids)
    assert connection_ids == sorted(connection_ids)


@pytest.mark.parametrize("label", STAGES)
def test_fixture_drafts_do_not_invent_numeric_observations(label: str) -> None:
    raw = _load(f"fixture_manifest_v2.stage-{label}.template.json")
    _assert_sensitive_values_are_operator_placeholders(raw)


def test_fixture_drafts_preserve_the_comparison_chain() -> None:
    normalized = {label: _normalized_fixture(label) for label in STAGES}
    shared_values = [item[1] for item in normalized.values()]
    assert all(shared == shared_values[0] for shared in shared_values[1:])

    for previous, current in (("a", "b"), ("b", "c"), ("c", "e")):
        prior_delta = normalized[previous][2]
        current_delta = normalized[current][2]
        assert leakage._prior_comparison_invariants(STAGES[current], prior_delta) == (
            leakage._current_comparison_invariants(STAGES[current], current_delta)
        )


@pytest.mark.parametrize("label", STAGES)
def test_setup_template_normalizes_against_its_exact_fixture_inventory(
    label: str,
    tmp_path: Path,
) -> None:
    raw_fixture, shared, delta = _normalized_fixture(label)
    component_ids, connection_ids = leakage._fixture_identity_sets(shared, delta)
    setup = _replace_placeholders(_load(f"setup_attestation_v1.stage-{label}.template.json"))
    assert set(setup) == EXPECTED_SETUP_FIELDS
    assert setup["stage"] == STAGES[label]
    assert setup["observed_component_ids"] == component_ids
    assert setup["observed_connection_ids"] == connection_ids

    evidence = tmp_path / f"stage-{label}-setup-evidence.txt"
    evidence.write_text("offline template smoke-test evidence\n", encoding="utf-8")
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    manifest_sha = "1" * 64
    shared_sha = leakage.canonical_json_sha256(shared)
    delta_sha = leakage.canonical_json_sha256(delta)
    setup.update(
        {
            "created_at": "2026-08-30T12:00:00+00:00",
            "fixture_manifest_sha256": manifest_sha,
            "shared_fixture_sha256": shared_sha,
            "stage_delta_sha256": delta_sha,
            "setup_evidence_path": str(evidence),
            "setup_evidence_sha256": evidence_sha,
        }
    )
    flash_binding: dict[str, Any] | None
    if label in {"c", "e"}:
        flash_path = tmp_path / "sealed-selector-flash.json"
        flash_sha = "2" * 64
        assert isinstance(setup["selector_flash_evidence"], dict)
        setup["selector_flash_evidence"].update({"path": str(flash_path), "sha256": flash_sha})
        flash_binding = {
            "schema": 1,
            "binding_kind": "sealed_selector_flash_evidence_v1",
            "path": str(flash_path),
            "sha256": flash_sha,
            "campaign_id": raw_fixture["campaign_id"],
            "run_id": setup["selector_flash_evidence"]["run_id"],
            "board_id": raw_fixture["board_id"],
            "image_role": "bench",
        }
    else:
        assert setup["selector_flash_evidence"] is None
        flash_binding = None

    setup_path = tmp_path / f"stage-{label}-setup-attestation.json"
    setup_path.write_text(json.dumps(setup, sort_keys=True), encoding="utf-8")
    normalized = leakage._normalize_setup_attestation(
        setup_path,
        run_id=setup["run_id"],
        campaign_id=raw_fixture["campaign_id"],
        comparable_fixture_group_id=raw_fixture["comparable_fixture_group_id"],
        stage=STAGES[label],
        fixture_manifest_sha256=manifest_sha,
        shared_fixture_sha256=shared_sha,
        stage_delta_sha256=delta_sha,
        component_ids=component_ids,
        connection_ids=connection_ids,
        selector_flash_evidence=flash_binding,
    )

    assert normalized["stage"] == STAGES[label]
    assert normalized["selector_flash_evidence"] == flash_binding


def test_generated_setup_draft_is_accepted_after_only_observations_are_filled(
    tmp_path: Path,
) -> None:
    raw_fixture, shared, delta = _normalized_fixture("a")
    fixture_document = {
        "schema": 2,
        "fixture_kind": leakage.FIXTURE_KIND_V2,
        "campaign_id": raw_fixture["campaign_id"],
        "comparable_fixture_group_id": raw_fixture["comparable_fixture_group_id"],
        "stage": STAGES["a"],
        "board_id": raw_fixture["board_id"],
        "shared_fixture": shared,
        "stage_delta": delta,
        "prior_stage_binding": None,
    }
    fixture_path = tmp_path / "stage-a.fixture.json"
    generator._write_new(fixture_path, fixture_document)
    validated = generator.validate_generated_fixture_manifest(
        fixture_path,
        stage=STAGES["a"],
        board_id=raw_fixture["board_id"],
        serial=shared["pluto"]["serial"],
    )
    run_id = "stage-a-run-001"
    setup = generator.generate_setup_attestation_draft(validated, run_id=run_id)
    evidence = tmp_path / "stage-a-setup-photo.txt"
    evidence.write_text("operator-observed stage A setup\n", encoding="utf-8")
    setup.update(
        {
            "attestation_id": "stage-a-setup-observation-001",
            "created_at": "2026-08-30T12:00:00+00:00",
            "setup_evidence_path": str(evidence),
            "setup_evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }
    )
    setup_path = tmp_path / "stage-a.setup.json"
    setup_path.write_text(json.dumps(setup, sort_keys=True), encoding="utf-8")

    normalized = leakage._normalize_setup_attestation(
        setup_path,
        run_id=run_id,
        campaign_id=raw_fixture["campaign_id"],
        comparable_fixture_group_id=raw_fixture["comparable_fixture_group_id"],
        stage=STAGES["a"],
        fixture_manifest_sha256=validated.file_sha256,
        shared_fixture_sha256=validated.shared_fixture_sha256,
        stage_delta_sha256=validated.stage_delta_sha256,
        component_ids=validated.component_ids,
        connection_ids=validated.connection_ids,
        selector_flash_evidence=None,
    )

    assert normalized["run_id"] == run_id
    assert normalized["fixture_manifest_sha256"] == validated.file_sha256
