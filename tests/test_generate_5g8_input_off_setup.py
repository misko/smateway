from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from smateway.input_off_control import (
    InputOffContractError,
    validate_fixture_v2,
    validate_setup_attestation,
)

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TEST = REPOSITORY / "tests/test_input_off_control.py"
SOURCE_SPEC = importlib.util.spec_from_file_location("p2_setup_fixture_source", SOURCE_TEST)
assert SOURCE_SPEC is not None and SOURCE_SPEC.loader is not None
source = importlib.util.module_from_spec(SOURCE_SPEC)
sys.modules[SOURCE_SPEC.name] = source
SOURCE_SPEC.loader.exec_module(source)

SCRIPT = REPOSITORY / "scripts/generate_5g8_input_off_setup.py"
SPEC = importlib.util.spec_from_file_location("generate_5g8_input_off_setup_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

DOCS = REPOSITORY / "docs/5g8_root_cause_analysis"


def _write_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    fixture = source.fixture_document()
    path = tmp_path / "p2.fixture.json"
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, fixture


def test_checked_p2_templates_are_structurally_complete_and_fail_closed() -> None:
    fixture = json.loads(
        (DOCS / "p2_input_off_fixture_v2.template.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (DOCS / "p2_input_off_setup_attestation_v1.template.json").read_text(encoding="utf-8")
    )
    assert fixture["fixture_kind"] == "5g8_input_drive_off_fixture_v2"
    assert fixture["rx2_attenuator"] == {
        "state": "REPLACE_RX2_ATTENUATOR_STATE_PRESENT_OR_ABSENT",
        "component": None,
        "pluto_connection": None,
    }
    assert set(fixture["components"]) == set(source.COMPONENT_ROLES)
    assert set(fixture["connections"]) == set(source.P2_CONNECTION_ROLES)
    assert setup["attestation_kind"] == "5g8_input_drive_off_run_setup_v1"
    with pytest.raises(InputOffContractError, match="unresolved placeholder"):
        validate_fixture_v2(
            fixture,
            run_id="p2-run-a",
            board_id=source.BOARD_ID,
            serial=source.SERIAL,
        )


def test_setup_generator_derives_hash_and_exact_sorted_inventories(tmp_path: Path) -> None:
    fixture_path, fixture = _write_fixture(tmp_path)
    normalized = validate_fixture_v2(
        fixture,
        run_id=source.RUN_ID,
        board_id=source.BOARD_ID,
        serial=source.SERIAL,
    )
    draft = generator.generate_setup_draft(
        fixture_path,
        run_id=source.RUN_ID,
        board_id=source.BOARD_ID,
        serial=source.SERIAL,
    )
    assert draft["observed_component_ids"] == normalized["component_ids"]
    assert draft["observed_connection_ids"] == normalized["connection_ids"]
    assert len(draft["fixture_manifest_sha256"]) == 64
    assert all(value is False for value in draft["confirmations"].values())
    with pytest.raises(InputOffContractError, match="unresolved placeholder"):
        validate_setup_attestation(
            draft,
            fixture=normalized,
            fixture_file_sha256=draft["fixture_manifest_sha256"],
            run_id=source.RUN_ID,
        )


def test_setup_generator_cli_is_create_only(tmp_path: Path, capsys: Any) -> None:
    fixture_path, _ = _write_fixture(tmp_path)
    output = tmp_path / "p2.setup-draft.json"
    arguments = [
        "--fixture-manifest",
        str(fixture_path),
        "--run-id",
        source.RUN_ID,
        "--board-id",
        source.BOARD_ID,
        "--serial",
        source.SERIAL,
        "--output",
        str(output),
    ]
    assert generator.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hardware_access"] is False
    assert result["rf_activity"] is False
    assert output.stat().st_mode & 0o077 == 0
    assert generator.main(arguments) == 2
    assert "refusing overwrite" in capsys.readouterr().err
