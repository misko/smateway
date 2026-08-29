import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_5g8_evidence_inventory.py"
SPEC = importlib.util.spec_from_file_location("generate_5g8_inventory_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def test_cli_writes_and_checks_empty_drift_corpus(tmp_path: Path) -> None:
    board_root = tmp_path / "board-a"
    board_root.mkdir()
    output = tmp_path / "inventory.json"
    arguments = [
        "--board-state-root",
        str(board_root),
        "--output",
        str(output),
        "--allow-corpus-drift",
    ]

    assert generator.main(arguments) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["aggregate_invariants"]["unique_raw_capture_count"] == 0
    assert generator.main([*arguments, "--check"]) == 0


def test_cli_check_rejects_stale_output(tmp_path: Path) -> None:
    board_root = tmp_path / "board-a"
    board_root.mkdir()
    output = tmp_path / "inventory.json"
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(generator.EvidenceInventoryError, match="stale"):
        generator.main(
            [
                "--board-state-root",
                str(board_root),
                "--output",
                str(output),
                "--allow-corpus-drift",
                "--check",
            ]
        )
