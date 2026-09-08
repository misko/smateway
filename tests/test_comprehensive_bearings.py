import importlib.util
from pathlib import Path

import pytest

from smateway.rate_timing import PORTS, complex_json

SPEC = importlib.util.spec_from_file_location(
    "comprehensive_bearings",
    Path(__file__).resolve().parents[1] / "scripts/analyze_comprehensive_bearings.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(monkeypatch):
    records = {}
    block = {
        "configuration": "B",
        "reference_configurations": ["A", "B"],
        "frequency_hz": 5800000000,
        "gain_db": 60,
        "references": [],
    }
    for name in ("A", "B"):
        for position in ("before", "after"):
            for index, port in enumerate(PORTS):
                key = f"{name}-{position}-{port}"
                # B has deliberately opposite power order: weights must still come from A.
                value = index + 1 if name == "A" else 6 - index
                records[key] = {
                    "status": "passed",
                    "configuration": {
                        "mode": "static",
                        "name": name,
                        "frequency_hz": 5800000000,
                        "receiver_gain_db": 60,
                        "port": port,
                    },
                    "reference": {"transfer": complex_json(value)},
                }
                block["references"].append(
                    {
                        "configuration": name,
                        "position": position,
                        "port": port,
                        "run_json": key,
                        "sha256": key,
                    }
                )
    monkeypatch.setattr(MODULE, "sha256", lambda p: str(p))
    monkeypatch.setattr(MODULE, "load", lambda p: records[str(p)])
    return block, records


def test_rate_specific_references_with_A_weights_and_after_drift(monkeypatch):
    block, _ = fixture(monkeypatch)
    refs, frozen, drift = MODULE.block_references(block)
    assert len(refs["B"]["after"]) == 6
    assert frozen["weights"][0] < frozen["weights"][-1]
    assert drift["A"]["passed"] and drift["B"]["passed"]


def test_after_reference_failure_cannot_be_promoted(monkeypatch):
    block, records = fixture(monkeypatch)
    records["B-after-ANT1"]["reference"]["transfer"] = complex_json(60)
    _, _, drift = MODULE.block_references(block)
    assert drift["B"]["available"] and not drift["B"]["passed"]


def test_missing_after_is_unavailable_not_pass(monkeypatch):
    block, _ = fixture(monkeypatch)
    block["references"] = [r for r in block["references"] if r["position"] == "before"]
    _, _, drift = MODULE.block_references(block)
    assert not drift["A"]["available"] and not drift["A"]["passed"]


def test_reference_configuration_mismatch_fails(monkeypatch):
    block, records = fixture(monkeypatch)
    records["B-before-ANT1"]["configuration"]["name"] = "A"
    with pytest.raises(ValueError, match="configuration"):
        MODULE.block_references(block)
