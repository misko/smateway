from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DOCS = Path(__file__).resolve().parents[1] / "docs/5g8_root_cause_analysis"
FIXTURE = DOCS / "p1_muted_fixture_v1.template.json"
SETUP = DOCS / "p1_muted_setup_attestation_v1.template.json"
NOTE = DOCS / "p1_muted_operator_templates.md"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _placeholder_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if "REPLACE_" in value else []
    if isinstance(value, dict):
        return [
            placeholder
            for key, item in value.items()
            for placeholder in (*_placeholder_strings(str(key)), *_placeholder_strings(item))
        ]
    if isinstance(value, list):
        return [placeholder for item in value for placeholder in _placeholder_strings(item)]
    return []


def test_p1_fixture_template_is_one_shareable_exact_runner_schema() -> None:
    fixture = _load(FIXTURE)

    assert set(fixture) == {
        "schema",
        "fixture_kind",
        "campaign_id",
        "fixture_id",
        "p0_legacy_fixture_id",
        "board_id",
        "pluto_serial",
        "topology_token",
        "no_antennas",
        "tx1_path",
        "tx2_state",
        "rx1_state",
        "rx2_state",
        "selector_mode",
        "component_ids",
        "connection_ids",
    }
    assert fixture["schema"] == 1
    assert fixture["fixture_kind"] == "5g8_p1_untouched_fixture"
    assert fixture["topology_token"] == "UNTOUCHED_ROTATION0_FULL_CONDUCTED_FIXTURE"
    assert fixture["no_antennas"] is True
    assert fixture["tx1_path"] == "matched_conducted_full_fixture"
    assert fixture["tx2_state"] == "50ohm_terminated"
    assert fixture["rx1_state"] == "protected_conducted_reference"
    assert fixture["rx2_state"] == "selector_common_full_fixture"
    assert fixture["selector_mode"] == "fast20"
    assert "run_id" not in fixture
    for field in ("component_ids", "connection_ids"):
        values = fixture[field]
        assert isinstance(values, list) and values
        assert len(values) == len(set(values))
        assert all(isinstance(value, str) and value.startswith("REPLACE_") for value in values)


def test_p1_setup_template_is_run_specific_and_binds_ordered_p0_cohort() -> None:
    setup = _load(SETUP)

    assert set(setup) == {
        "schema",
        "attestation_kind",
        "attestation_id",
        "run_id",
        "campaign_id",
        "board_id",
        "pluto_serial",
        "fixture_manifest_sha256",
        "setup_evidence",
        "no_component_or_connection_movement",
        "selector_flash_evidence_sha256",
        "p0_source_manifest_sha256s",
    }
    assert setup["schema"] == 1
    assert setup["attestation_kind"] == "5g8_p1_muted_control_setup"
    assert setup["attestation_id"].startswith("REPLACE_UNIQUE_")
    assert setup["run_id"] == "REPLACE_EXACT_P1_RUN_ID"
    assert setup["no_component_or_connection_movement"] is True
    assert set(setup["setup_evidence"]) == {"path", "sha256", "size_bytes"}
    assert setup["p0_source_manifest_sha256s"] == [
        f"REPLACE_P0_R{index:02d}_MANIFEST_SHA256" for index in range(1, 6)
    ]


def test_p1_templates_remain_unresolved_drafts_and_note_explains_copy_policy() -> None:
    assert _placeholder_strings(_load(FIXTURE))
    assert _placeholder_strings(_load(SETUP))
    note = NOTE.read_text(encoding="utf-8")
    assert "identical bytes and SHA-256" in note
    assert "separate completed setup attestation for every P1 run" in note
    assert "ordered identically" in note
    assert "--p0-manifest" in note
    assert "any nesting depth" in note
