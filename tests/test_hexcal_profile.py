from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_hexcal_profile.py"
SPEC = importlib.util.spec_from_file_location("hexcal_profile_generator_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GENERATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


def test_generated_profile_is_fresh_and_exact() -> None:
    outputs = GENERATOR._outputs()

    for path, content in outputs.items():
        assert path.read_text(encoding="utf-8") == content
    document = json.loads(outputs[Path("profiles/hexcal-v1/control_profile.json")])
    assert document["profile"] == {"id": "hexcal-v1", "revision": 1}
    assert document["array_order"] == {
        "direction": "clockwise",
        "forward_reference": "ANT1",
    }
    assert document["time_unit"] == "microseconds"
    assert document["frame"]["all_off_guard_us"] == 20
    assert document["frame"]["marker"]["body_nominal_us"] == 180
    assert document["frame"]["marker"]["observable_nominal_us"] == 200
    assert document["frame"]["nominal_cycle_us"] == 1500
    assert document["release_contract"] == {
        "released_transition_guard_us": 5000,
        "profile_transition_guard_us": 20,
        "conformant": False,
        "status": (
            "experimental calibration-only guard waiver; does not supersede "
            "qualified Fast20"
        ),
    }
    assert document["safety"]["maximum_deadline_lateness_us"] == 5
    assert document["safety"]["unused_states"] == ["ANT7", "ANT8"]
    assert [state["name"] for state in document["states"]] == [
        "ANT1",
        "ANT2",
        "ANT3",
        "ANT4",
        "ANT5",
        "ANT6",
    ]
    assert [state["gpio_code_pa3_pa0"] for state in document["states"]] == [
        "0000",
        "0100",
        "0010",
        "0110",
        "0001",
        "0101",
    ]
    assert {state["dwell_us"] for state in document["states"]} == {200}


def test_generated_profile_provenance_hashes_exact_bytes() -> None:
    provenance = json.loads(Path("profiles/hexcal-v1/provenance.json").read_text())

    for filename, expected in provenance["artifacts"].items():
        path = Path("profiles/hexcal-v1") / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    for filename, expected in provenance["sources"].items():
        assert hashlib.sha256(Path(filename).read_bytes()).hexdigest() == expected


def test_profile_generator_rejects_changed_order_or_timing() -> None:
    spec, source, _, all_off_code = GENERATOR._load_inputs()
    wrong_order = {**spec, "state_order": list(reversed(spec["state_order"]))}
    wrong_timing = {**spec, "all_off_guard_us": 19}
    missing_waiver = {**spec, "released_contract_conformant": True}

    with pytest.raises(ValueError, match="ANT1 through ANT6"):
        GENERATOR._generated_profile(wrong_order, source, all_off_code)
    with pytest.raises(ValueError, match="timing contract changed"):
        GENERATOR._generated_profile(wrong_timing, source, all_off_code)
    with pytest.raises(ValueError, match="released-contract waiver"):
        GENERATOR._generated_profile(missing_waiver, source, all_off_code)
