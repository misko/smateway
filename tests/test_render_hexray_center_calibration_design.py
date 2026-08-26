from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/render_hexray_center_calibration_design.py"
)
SPEC = importlib.util.spec_from_file_location("hexray_design_renderer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_snapshot_locks_geometry_schedule_and_evidence_boundary() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)

    assert document["evidence_status"].startswith("pre-execution")
    assert document["geometry"]["phase_center_circle_diameter_mm"] == 51.0
    assert [row["name"] for row in document["geometry"]["antennas"]] == [
        "ANT1",
        "ANT2",
        "ANT3",
        "ANT4",
        "ANT5",
        "ANT6",
    ]
    assert document["schedule"]["profile_id"] == "hexcal-v1"
    assert document["schedule"]["cycle_us"] == 1500
    assert document["schedule"]["pre_state_all_off_us"] == 20
    assert document["capture_plan"]["planned_artifacts"] == 18

    gain = document["capture_plan"]["rx_gain_qualification"]
    assert gain["mode"] == "manual"
    assert gain["selected_gain_db"] is None
    assert gain["agc_allowed"] is False
    assert "lowest conservative" in gain["initial_setting"]

    fallback = document["timing_and_gpio_qualification"]["fallback_path"]
    assert "timing only" in fallback["independently_observed"]
    assert "not independently" in fallback["not_independently_observed"]


def test_snapshot_retains_gauge_ambiguity_and_experimental_5g8() -> None:
    document = MODULE.load_snapshot(MODULE.DEFAULT_SNAPSHOT)

    assert document["calibration_model"]["measurement_equation"] == (
        "H_i(f) = C_i(f) * A_i(f)"
    )
    assert "only the product" in document["calibration_model"]["identifiability"]
    assert document["frequency_plan"]["experimental_exact_center_hz"] == 5_800_000_000


def test_renderer_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, first)
    second_hashes = MODULE.render_report(MODULE.DEFAULT_SNAPSHOT, second)

    assert first_hashes == second_hashes
    assert tuple(first_hashes) == MODULE.FIGURE_NAMES
    for filename in MODULE.FIGURE_NAMES:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_snapshot_rejects_an_inconsistent_cycle(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["schedule"]["cycle_us"] = 1499
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.DesignReportError, match="do not add"):
        MODULE.load_snapshot(malformed)


def test_snapshot_rejects_a_claimed_measurement(tmp_path: Path) -> None:
    document = json.loads(MODULE.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
    document["evidence_status"] = "calibration passed"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MODULE.DesignReportError, match="pre-execution"):
        MODULE.load_snapshot(malformed)
