from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from smateway.intervention_support import InterventionRepeat
from smateway.selected_state_qualification import canonical_sha256

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_intervention_support.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_intervention_support", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)

CONTRACT = "selector-shield-intervention-r01"
SOURCE_COMMIT = "a" * 40
DEPENDENCY_COMMIT = "b" * 40
SELECTOR_SHA = "c" * 64
ROLES = ("full_fixture_baseline", "full_fixture_intervention")


def _file(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def _repeat(role: str, index: int, ratio: float) -> InterventionRepeat:
    return InterventionRepeat(
        repeat_index=index,
        condition_id=f"{role}-condition-{index}",
        stream_id=f"{role}-stream-{index}",
        raw_iq_sha256=hashlib.sha256(f"{role}-raw-{index}".encode()).hexdigest(),
        quality_passed=True,
        rx1_amplitude_counts=1000.0,
        transfer_detected=True,
        transfer_amplitude_ratio=ratio,
        transfer_amplitude_upper_bound_ratio=None,
    )


def _source_identity(dependency: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    return {
        "smateway_commit": SOURCE_COMMIT,
        "dependency_commit": DEPENDENCY_COMMIT,
        "dependency_attestation_sha256": canonical_sha256(dependency),
        "native_attestation_sha256": canonical_sha256(native),
        "selector_evidence_sha256": SELECTOR_SHA,
    }


def _runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = tmp_path / "analysis-source"
    files: list[dict[str, Any]] = []
    for relative in analyzer.ANALYSIS_SOURCE_FILES:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source:{relative}\n", encoding="utf-8")
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    dependency = {"schema": 1, "commit": DEPENDENCY_COMMIT, "dependency": "test"}
    native = {"schema": 1, "native": "test"}
    monkeypatch.setattr(analyzer, "validate_runtime_attestation", lambda value: dict(value))
    monkeypatch.setattr(analyzer, "attestation_sha256", canonical_sha256)
    source = {
        "schema": 1,
        "repository": str(repository.absolute()),
        "commit": SOURCE_COMMIT,
        "clean_source_files_verified": True,
        "files": files,
        "source_files_sha256": canonical_sha256(files),
    }
    return (
        {
            "source": source,
            "dependency": dependency,
            "native": native,
            "source_commit": SOURCE_COMMIT,
            "dependency_commit": DEPENDENCY_COMMIT,
            "native_attestation_sha256": canonical_sha256(native),
        },
        _source_identity(dependency, native),
    )


def _inputs(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    x_plans: dict[str, Any] = {}
    manifests: dict[str, Path] = {}
    for role in ROLES:
        plan_path = tmp_path / f"{role}.plan.json"
        plan_path.write_text(json.dumps({"role": role}), encoding="utf-8")
        x_plans[role] = {"run_id": f"x-{role}", "plan_file": _file(plan_path)}
        manifest_path = tmp_path / f"{role}.manifest.json"
        manifest_path.write_text(json.dumps({"run_role": role}), encoding="utf-8")
        manifests[role] = manifest_path
    change_plan = tmp_path / "change-plan.json"
    change_plan.write_text(json.dumps({"x_run_plans": x_plans}), encoding="utf-8")
    return change_plan, manifests


def _install_plan_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analyzer,
        "validate_intervention_change_plan",
        lambda _value: SimpleNamespace(contract_id=CONTRACT, expected_x_roles=ROLES),
    )


def test_producer_derives_supported_result_and_seals_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_plan, manifests = _inputs(tmp_path)
    runtime, identity = _runtime(tmp_path, monkeypatch)
    _install_plan_validator(monkeypatch)

    def load_role(*, role: str, **_kwargs: Any) -> Any:
        ratio = 0.1 if role.endswith("baseline") else 0.04
        return analyzer.LoadedRole(
            source_identity=identity,
            repeats=tuple(_repeat(role, index, ratio) for index in range(1, 6)),
        )

    analysis_path, result_path = analyzer.produce_intervention_support_result(
        change_plan_path=change_plan,
        x_manifest_paths=manifests,
        analysis_output=tmp_path / "analysis.json",
        result_output=tmp_path / "result.json",
        runtime_bindings=runtime,
        role_loader=load_role,
        now=lambda: "2026-08-30T12:00:00+00:00",
    )
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["result_kind"] == "5g8_intervention_support_result_v1"
    assert result["simultaneous_improvement_gate_passed"] is True
    assert result["accepted"] is True
    assert result["decision"] == "supported_fix"
    assert analysis["x_run_source_identity"] == identity
    assert analysis["qualification"]["simultaneous_improvement_gate_passed"] is True
    assert result["analysis_file"] == _file(analysis_path)
    assert result_path.stat().st_mode & 0o222 == 0


def test_producer_cannot_accept_subthreshold_repeats_from_a_caller_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_plan, manifests = _inputs(tmp_path)
    runtime, identity = _runtime(tmp_path, monkeypatch)
    _install_plan_validator(monkeypatch)

    def load_role(*, role: str, **_kwargs: Any) -> Any:
        ratio = 0.1 if role.endswith("baseline") else 0.09
        return analyzer.LoadedRole(
            source_identity=identity,
            repeats=tuple(_repeat(role, index, ratio) for index in range(1, 6)),
        )

    _, result_path = analyzer.produce_intervention_support_result(
        change_plan_path=change_plan,
        x_manifest_paths=manifests,
        analysis_output=tmp_path / "analysis.json",
        result_output=tmp_path / "result.json",
        runtime_bindings=runtime,
        role_loader=load_role,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["accepted"] is False
    assert result["simultaneous_improvement_gate_passed"] is False
    assert "simultaneous_three_db_leakage_improvement_not_proven" in result["rejection_reasons"]


def test_producer_rejects_source_mismatch_before_writing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_plan, manifests = _inputs(tmp_path)
    runtime, identity = _runtime(tmp_path, monkeypatch)
    runtime["source_commit"] = "d" * 40
    runtime["source"]["commit"] = "d" * 40
    _install_plan_validator(monkeypatch)

    def load_role(*, role: str, **_kwargs: Any) -> Any:
        return analyzer.LoadedRole(
            source_identity=identity,
            repeats=tuple(_repeat(role, index, 0.1) for index in range(1, 6)),
        )

    with pytest.raises(
        analyzer.InterventionSupportAnalysisError, match="differs from the X acquisitions"
    ):
        analyzer.produce_intervention_support_result(
            change_plan_path=change_plan,
            x_manifest_paths=manifests,
            analysis_output=tmp_path / "analysis.json",
            result_output=tmp_path / "result.json",
            runtime_bindings=runtime,
            role_loader=load_role,
        )
    assert not (tmp_path / "analysis.json").exists()
    assert not (tmp_path / "result.json").exists()


def test_producer_rejects_manifest_tamper_during_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_plan, manifests = _inputs(tmp_path)
    runtime, identity = _runtime(tmp_path, monkeypatch)
    _install_plan_validator(monkeypatch)
    mutated = False

    def load_role(*, role: str, **_kwargs: Any) -> Any:
        nonlocal mutated
        if not mutated:
            manifests[role].write_text('{"tampered":true}\n', encoding="utf-8")
            mutated = True
        return analyzer.LoadedRole(
            source_identity=identity,
            repeats=tuple(_repeat(role, index, 0.1) for index in range(1, 6)),
        )

    with pytest.raises(analyzer.InterventionSupportAnalysisError, match="changed during analysis"):
        analyzer.produce_intervention_support_result(
            change_plan_path=change_plan,
            x_manifest_paths=manifests,
            analysis_output=tmp_path / "analysis.json",
            result_output=tmp_path / "result.json",
            runtime_bindings=runtime,
            role_loader=load_role,
        )


def test_default_role_loader_rejects_bound_raw_artifact_hash_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = {"commit": DEPENDENCY_COMMIT}
    native = {"native": "test"}
    source = {
        "smateway_commit": SOURCE_COMMIT,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": canonical_sha256(dependency),
        "native_libiio_runtime_attestation": native,
        "native_libiio_runtime_attestation_sha256": canonical_sha256(native),
    }
    conditions = [
        {"condition_id": f"condition-{index}", "attribution_repeat_index": index}
        for index in range(1, 6)
    ]
    contract = {"source": source, "configuration": {}, "conditions": conditions}
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_contract": contract}), encoding="utf-8")
    captures: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions, start=1):
        raw = tmp_path / f"raw-{index}.bin"
        raw.write_bytes(f"raw-{index}".encode())
        metadata = tmp_path / f"meta-{index}.json"
        metadata.write_text("{}", encoding="utf-8")
        record = tmp_path / f"record-{index}.json"
        record.write_text(json.dumps({"condition": condition}), encoding="utf-8")
        captures.append(
            {
                "stream_id": str(index),
                "raw_iq_file": _file(raw),
                "metadata_file": _file(metadata),
                "condition_record_file": _file(record),
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "run_role": "full_fixture_baseline",
                "selector_evidence_sha256": SELECTOR_SHA,
                "captures": captures,
            }
        ),
        encoding="utf-8",
    )
    Path(captures[0]["raw_iq_file"]["path"]).write_bytes(b"tampered")
    monkeypatch.setattr(analyzer.leakage_runner, "_validate_plan_envelope", lambda *_a, **_k: None)
    monkeypatch.setattr(
        analyzer.leakage_runner, "_validate_x_capture_manifest", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        analyzer,
        "_reanalyze_repeat",
        lambda **kwargs: _repeat(
            "full_fixture_baseline",
            int(kwargs["condition"]["attribution_repeat_index"]),
            0.1,
        ),
    )
    with pytest.raises(analyzer.InterventionSupportAnalysisError, match="SHA-256 binding is stale"):
        analyzer._load_role(
            role="full_fixture_baseline",
            manifest_path=manifest,
            expected_plan_file=_file(plan),
        )
