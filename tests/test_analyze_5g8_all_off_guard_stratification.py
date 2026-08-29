from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/analyze_5g8_all_off_guard_stratification.py"
SPEC = importlib.util.spec_from_file_location("guard_stratification_script_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

REPOSITORY_ROOT = Path(__file__).parents[1]
COMMITTED_ARTIFACT = (
    REPOSITORY_ROOT
    / "docs/5g8_root_cause_analysis/data"
    / "selector-synchronous-all-off-guard-stratification.json"
)


def _repeatability() -> dict[str, object]:
    return json.loads(MODULE.DEFAULT_REPEATABILITY.read_text(encoding="utf-8"))


def test_source_selection_is_exact_twenty_pass_cohort_without_baseline() -> None:
    cohort = MODULE._select_repeatability_cohort(_repeatability())

    assert len(cohort) == 20
    assert [item["label"] for item in cohort] == [f"repeat-{index}" for index in range(1, 21)]
    assert cohort[0]["run_id"].endswith("r0-repeat6")
    assert cohort[-1]["run_id"].endswith("r0-repeat25")
    assert cohort[0]["artifact_id"] == "8f57e06dbd464effbf838791179e86f2"
    assert cohort[-1]["artifact_id"] == "debc81e91fa14a7e97f9f46345e655ee"
    assert all("retry" not in item["run_id"] for item in cohort)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "baseline"])
def test_source_selection_fails_closed(mutation: str) -> None:
    document = deepcopy(_repeatability())
    if mutation == "missing":
        document["source_runs"].pop()
    elif mutation == "duplicate":
        document["source_runs"][2]["source_analyses"] = deepcopy(
            document["source_runs"][1]["source_analyses"]
        )
    else:
        document["source_runs"][0]["label"] = "not-baseline"

    with pytest.raises(MODULE.GuardArtifactError):
        MODULE._select_repeatability_cohort(document)


def test_inventory_and_repeatability_bind_same_raw_and_sidecar_hashes() -> None:
    cohort = MODULE._select_repeatability_cohort(_repeatability())
    inventory_document = json.loads(MODULE.DEFAULT_INVENTORY.read_text(encoding="utf-8"))
    inventory = MODULE._inventory_by_artifact(inventory_document)

    bound = MODULE._bind_cohort_to_inventory(cohort, inventory)

    assert len(bound) == 20
    assert all(
        item["source"]["raw_data_sha256"] == item["inventory"]["raw_data_sha256"] for item in bound
    )
    assert all(
        item["source"]["reference_sidecar_sha256"] == item["reference"]["sha256"] for item in bound
    )


def test_real_data_compact_artifact_reproduces_null_decision() -> None:
    document = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    captures = document["captures"]

    recomputed = MODULE.aggregate_from_capture_summaries(captures)

    assert recomputed == document["aggregate"]
    assert recomputed["persistent_selector_synchronous_signature_detected"] is False
    assert recomputed["detected_strata"] == []
    assert document["interpretation"]["physical_root_cause_identified"] is False
    assert "cannot distinguish" in document["interpretation"]["null_result_limit"]


def test_committed_artifact_binds_exact_source_hashes() -> None:
    document = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    source = document["source"]

    assert source["evidence_inventory"]["sha256"] == MODULE.EXPECTED_INVENTORY_SHA256
    assert source["repeatability_result"]["sha256"] == MODULE.EXPECTED_REPEATABILITY_SHA256
    assert source["control_profile"]["sha256"] == MODULE.EXPECTED_PROFILE_SHA256
    assert len(document["captures"]) == 20
    assert len({capture["raw_data_sha256"] for capture in document["captures"]}) == 20
    assert all(capture["continuity"]["metadata_abi"] == 2 for capture in document["captures"])


def test_committed_artifact_binds_exact_generation_environment() -> None:
    document = json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))
    observed = MODULE._require_generation_environment()

    assert observed == MODULE._expected_generation_environment()
    assert document["generation_environment"] == observed
    assert document["generation_environment"]["project_inputs"] == {
        "pyproject": {
            "path": "pyproject.toml",
            "sha256": MODULE.EXPECTED_PYPROJECT_SHA256,
        },
        "uv_lock": {"path": "uv.lock", "sha256": MODULE.EXPECTED_UV_LOCK_SHA256},
    }
    fonts = document["generation_environment"]["rendering"]["font_files"]
    assert fonts == [dict(item) for item in MODULE.EXPECTED_FONT_FILES]


@pytest.mark.parametrize(
    "mutation",
    [
        "pyproject",
        "uv_lock",
        "python",
        "numpy",
        "matplotlib",
        "backend",
        "renderer",
        "font",
    ],
)
def test_replay_fails_closed_on_generation_environment_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = deepcopy(MODULE._generation_environment())
    if mutation in {"pyproject", "uv_lock"}:
        drifted["project_inputs"][mutation]["sha256"] = "0" * 64
    elif mutation == "python":
        drifted["python"]["version"] = "0.0.0"
    elif mutation in {"numpy", "matplotlib"}:
        drifted["packages"][f"{mutation}_version"] = "0.0.0"
    elif mutation == "backend":
        drifted["rendering"]["backend"] = "drifted"
    elif mutation == "renderer":
        drifted["rendering"]["renderer"] = "drifted.Renderer"
    else:
        drifted["rendering"]["font_files"][0]["sha256"] = "0" * 64
    monkeypatch.setattr(MODULE, "_generation_environment", lambda: drifted)

    with pytest.raises(MODULE.GuardArtifactError, match="generation environment drifted"):
        MODULE._require_generation_environment()


def test_cli_rejects_external_figure_before_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_figure = tmp_path / "external.png"
    external_output = tmp_path / "external.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--figure",
            str(external_figure),
            "--output",
            str(external_output),
        ],
    )

    with pytest.raises(MODULE.GuardArtifactError, match="--figure"):
        MODULE.main()

    assert external_figure.exists() is False
    assert external_output.exists() is False
