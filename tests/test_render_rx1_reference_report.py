from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_rx1_reference_report.py"
SPEC = importlib.util.spec_from_file_location("rx1_report_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_snapshot_retains_negative_result_contract() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)

    assert document["integrity_audit"]["passed"] is True
    assert document["strict_model_gate"]["passed"] is False
    assert document["identifiability"]["unique_planar_position_identified"] is False
    assert document["identifiability"]["accepted_range_difference_identified"] is False


def test_renderer_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, first)
    second_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, second)

    assert first_hashes == second_hashes
    assert tuple(first_hashes) == MODULE.FIGURE_NAMES
    for filename in MODULE.FIGURE_NAMES:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_snapshot_rejects_a_claimed_position(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["identifiability"]["unique_planar_position_identified"] = True
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.ReportError, match="must not claim"):
        MODULE.load_snapshot(malformed)
