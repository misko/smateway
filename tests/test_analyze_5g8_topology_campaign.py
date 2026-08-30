from __future__ import annotations

import copy
import importlib.util
import json
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from smateway.leakage_attribution import (
    AttributionRepeat,
    LeakageAttributionError,
    StageAttributionEvidence,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_topology_campaign.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_topology_campaign_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _MODULE
SPEC.loader.exec_module(_MODULE)
analyzer: Any = _MODULE

HASH_PROVENANCE = "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
TOPOLOGY = {
    "A": "direct_rx2_termination",
    "B": "rx2_cable_terminated",
    "C": "powered_selector_all_inputs_terminated",
    "E": "full_conducted_fixture",
}


def _sha256(path: Path) -> str:
    value: object = analyzer._sha256_path(path)
    assert isinstance(value, str)
    return value


class FakeRunner:
    SELECTOR_CONNECTED_STAGES = frozenset(
        {"powered_selector_all_inputs_terminated", "full_conducted_fixture"}
    )

    def __init__(self, expected_contract: dict[str, Any]) -> None:
        self.expected_contract = copy.deepcopy(expected_contract)
        self.completed_calls: list[dict[str, Any]] = []
        self.fixture_refresh_count = 0
        self.selector_hash_count = 0
        source = expected_contract["source"]
        self.current_commit = source["smateway_commit"]
        self.current_dependency = copy.deepcopy(source["pluto_plus_utils_source_attestation"])
        self.current_native = copy.deepcopy(source["native_libiio_runtime_attestation"])

    def _repository_commit_and_require_clean(self, _repository: Path, _label: str) -> str:
        return str(self.current_commit)

    def attest_pluto_plus_utils_source(self) -> dict[str, Any]:
        return copy.deepcopy(self.current_dependency)

    @staticmethod
    def _validate_dependency_source_attestation(value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    def _native_libiio_runtime_attestation(self) -> dict[str, Any]:
        return copy.deepcopy(self.current_native)

    @staticmethod
    def _validate_native_libiio_runtime_attestation(
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        return dict(value)

    @staticmethod
    def _assert_path_chain_has_no_symlink(path: Path, *, label: str) -> None:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"{label} contains a symlink: {current}")

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} root must be an object")
        return value

    def _build_plan_contract(self, **values: Any) -> dict[str, Any]:
        assert values["stage"] == self.expected_contract["topology_stage"]
        return copy.deepcopy(self.expected_contract)

    @staticmethod
    def _validate_plan_envelope(
        document: Mapping[str, Any],
        *,
        expected_contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        contract = document.get("plan_contract")
        if (
            document.get("schema") != 1
            or document.get("immutable") is not True
            or not isinstance(contract, Mapping)
            or document.get("plan_contract_sha256") != analyzer._canonical_sha256(contract)
            or document.get("plan_contract_hash_provenance") != HASH_PROVENANCE
            or dict(contract) != dict(expected_contract)
        ):
            raise RuntimeError("immutable plan envelope differs")
        return dict(document)

    def _load_manifest(
        self,
        path: Path,
        *,
        plan_path: Path,
        envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        assert plan_path.is_file()
        assert envelope["plan_contract"] == self.expected_contract
        return self._read_json(path, "manifest")

    @staticmethod
    def _manifest_summary(
        manifest: Mapping[str, Any],
        condition_count: int,
    ) -> dict[str, Any]:
        attempts = [value for value in manifest.get("attempts", []) if isinstance(value, Mapping)]
        complete = [value for value in attempts if value.get("status") == "complete"]
        return {
            "planned_conditions": condition_count,
            "attempted_conditions": len(attempts),
            "completed_conditions": len(complete),
            "remaining_conditions": condition_count - len(complete),
            "measurement_quality_passed": sum(
                value.get("outcome") == "measurement_quality_passed" for value in complete
            ),
            "measurement_quality_rejected": sum(
                value.get("outcome") == "measurement_quality_rejected" for value in complete
            ),
            "failed_conditions": sum(value.get("status") == "failed" for value in attempts),
            "quarantine_count": sum(bool(value.get("quarantine")) for value in attempts),
            "selector_calibration_claim": False,
            "causal_attribution_claim": False,
        }

    @staticmethod
    def _confirmation_fixture_binding_passed(
        value: Mapping[str, Any],
        fixture: Mapping[str, Any],
    ) -> bool:
        return (
            value.get("fixture_evidence_sha256") == analyzer._canonical_sha256(fixture)
            and value.get("shared_fixture_sha256") == fixture.get("shared_fixture_sha256")
            and value.get("stage_delta_sha256") == fixture.get("stage_delta_sha256")
            and value.get("prior_stage_binding") == fixture.get("prior_stage_binding")
        )

    @staticmethod
    def _runtime_attestation_passed(
        value: object,
        *,
        expected: Mapping[str, Any],
    ) -> bool:
        return isinstance(value, Mapping) and value == {"kind": "runtime", "expected": expected}

    @staticmethod
    def _fixture_evidence_passed(
        value: object,
        *,
        expected: Mapping[str, Any],
    ) -> bool:
        return isinstance(value, Mapping) and value == {"kind": "fixture", "expected": expected}

    @staticmethod
    def _identity_passed(value: object, *, serial: str, requested_uri: str) -> bool:
        return isinstance(value, Mapping) and value == {
            "kind": "identity",
            "serial": serial,
            "uri": requested_uri,
        }

    @staticmethod
    def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
        return isinstance(value, Mapping) and value == {
            "kind": "mute",
            "serial": serial,
            "purpose": purpose,
        }

    @staticmethod
    def _selector_passed(
        value: object,
        *,
        selector_control: Mapping[str, Any],
        purpose: str,
    ) -> bool:
        return isinstance(value, Mapping) and value == {
            "kind": "selector",
            "selector": selector_control,
            "purpose": purpose,
        }

    def _live_fixture_evidence_boundary(
        self,
        fixture: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.fixture_refresh_count += 1
        return {"kind": "fixture", "expected": fixture}

    def _verify_selector_artifacts(self, selector: Mapping[str, Any]) -> None:
        assert selector
        self.selector_hash_count += 1

    @staticmethod
    def _plan_file_evidence(
        path: Path,
        envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "path": str(path),
            "plan_file_sha256": _sha256(path),
            "plan_contract_sha256": envelope["plan_contract_sha256"],
        }

    def _completed_condition_ids(
        self,
        manifest: Mapping[str, Any],
        *,
        planned_conditions: Mapping[str, Mapping[str, Any]],
        contract: Mapping[str, Any],
        serial: str,
        plan_evidence: Mapping[str, Any],
        capture_root: Path,
        downgrade_invalid: bool,
    ) -> set[str]:
        self.completed_calls.append(
            {
                "planned": set(planned_conditions),
                "contract": contract,
                "serial": serial,
                "plan_evidence": plan_evidence,
                "capture_root": capture_root,
                "downgrade_invalid": downgrade_invalid,
            }
        )
        assert downgrade_invalid is False
        for attempt in manifest["attempts"]:
            result = attempt["result"]
            for path_key, hash_key in (
                ("artifact_data_path", "artifact_data_sha256"),
                ("artifact_metadata_path", "artifact_metadata_sha256"),
                ("condition_record_path", "condition_record_sha256"),
            ):
                path = Path(result[path_key])
                self._assert_path_chain_has_no_symlink(path, label=path_key)
                if _sha256(path.resolve(strict=True)) != result[hash_key]:
                    raise RuntimeError(f"{path_key} hash differs")
        return set(planned_conditions)

    @staticmethod
    def _offline_reverify_raw_result(*_args: Any, **_kwargs: Any) -> None:
        """The synthetic fixture exercises orchestration; raw admission has separate tests."""


def _mute(serial: str, purpose: str) -> dict[str, Any]:
    return {"kind": "mute", "serial": serial, "purpose": purpose}


def _confirmation(contract: Mapping[str, Any], fixture: Mapping[str, Any]) -> dict[str, Any]:
    stage = str(contract["topology_stage"])
    setup = fixture["setup_attestation"]
    return {
        "confirmed_at": "2026-08-29T12:00:00+00:00",
        "stage": stage,
        "topology_confirmation_token": contract["operator_confirmations_required"][
            "topology_confirmation_token"
        ],
        "no_antennas_anywhere": True,
        "tx1_matched_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "no_component_or_connection_movement_since_setup_attestation": True,
        "fixture_evidence_sha256": analyzer._canonical_sha256(fixture),
        "shared_fixture_sha256": fixture["shared_fixture_sha256"],
        "stage_delta_sha256": fixture["stage_delta_sha256"],
        "setup_attestation_sha256": setup["setup_attestation_file"]["sha256"],
        "setup_evidence_sha256": setup["setup_evidence"]["sha256"],
        "observed_component_ids": fixture["component_ids"],
        "observed_connection_ids": fixture["connection_ids"],
        "campaign_id": fixture["campaign_id"],
        "comparable_fixture_group_id": fixture["comparable_fixture_group_id"],
        "prior_stage_binding": fixture["prior_stage_binding"],
        "selector_static_all_off_physically_expected": (
            stage in FakeRunner.SELECTOR_CONNECTED_STAGES
        ),
        "confirmation_method": "explicit CLI flags after physical inspection",
    }


def _write_case(
    tmp_path: Path,
    *,
    stage_name: str = "A",
    detected: bool = False,
) -> tuple[FakeRunner, Path, Path]:
    topology_stage = TOPOLOGY[stage_name]
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    fixture_manifest = tmp_path / "fixture.json"
    setup_file = tmp_path / "setup.json"
    fixture_manifest.write_text("{}", encoding="utf-8")
    setup_file.write_text("{}", encoding="utf-8")
    shared = {"fixture": "shared-a"}
    stage_delta = {"delta": stage_name}
    fixture: dict[str, Any] = {
        "campaign_id": "campaign-a",
        "comparable_fixture_group_id": "group-a",
        "shared_fixture": shared,
        "shared_fixture_sha256": analyzer._canonical_sha256(shared),
        "stage_delta": stage_delta,
        "stage_delta_sha256": analyzer._canonical_sha256(stage_delta),
        "prior_stage_binding": None,
        "source_files": {
            "fixture_manifest": {
                "path": str(fixture_manifest),
                "sha256": _sha256(fixture_manifest),
                "size_bytes": fixture_manifest.stat().st_size,
            },
            "setup_attestation": {
                "path": str(setup_file),
                "sha256": _sha256(setup_file),
                "size_bytes": setup_file.stat().st_size,
            },
        },
        "setup_attestation": {
            "setup_attestation_file": {"sha256": _sha256(setup_file)},
            "setup_evidence": {"sha256": "9" * 64},
        },
        "component_ids": ["component-a"],
        "connection_ids": ["connection-a"],
        "characterization_summary": {"causal_attribution_fixture_eligible": True},
    }
    runtime = {"runtime": "native-a"}
    dependency = {"dependency": "pluto-plus-utils-a"}
    conditions = [
        {
            "condition_id": f"{stage_name}-repeat-{index}",
            "attribution_repeat_index": index,
            "attribution_repeat_count": 5,
        }
        for index in range(1, 6)
    ]
    source = {
        "smateway_commit": "1" * 40,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": analyzer._canonical_sha256(dependency),
        "native_libiio_runtime_attestation": runtime,
        "native_libiio_runtime_attestation_sha256": analyzer._canonical_sha256(runtime),
        "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
        "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
    }
    configuration = {
        "serial": "serial-a",
        "uri": "usb:1.2.3",
        "center_frequency_hz": 5_800_000_000,
        "tone_offset_hz_requested": 125_000,
        "sample_rate_hz": 5_000_000,
        "bandwidth_hz": 4_000_000,
        "receiver_gain_db": 20.0,
        "dds_scale": 0.1,
        "attribution_gain_db": -20.0,
        "attribution_repeat_count": 5,
        "metadata_abi": 2,
        "kernel_buffers": 4,
    }
    contract: dict[str, Any] = {
        "run_id": f"run-{stage_name}",
        "board_id": "board-a",
        "topology_stage": topology_stage,
        "source": source,
        "configuration": configuration,
        "fixture_evidence": fixture,
        "fixture_evidence_sha256": analyzer._canonical_sha256(fixture),
        "selector_control": None,
        "storage": {
            "medium": "raspberry_pi_local_filesystem",
            "board_state_root": str(tmp_path),
            "artifact_root": str(tmp_path / "captures"),
            "run_capture_root": str(capture_root),
            "pluto_onboard_storage_used": False,
        },
        "conditions": conditions,
        "operator_confirmations_required": {
            "topology_confirmation_token": f"CONFIRM-{stage_name}",
        },
    }
    envelope = {
        "schema": 1,
        "immutable": True,
        "plan_contract": contract,
        "plan_contract_sha256": analyzer._canonical_sha256(contract),
        "plan_contract_hash_provenance": HASH_PROVENANCE,
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(envelope), encoding="utf-8")

    attempts: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions, start=1):
        artifact = capture_root / f"artifact-{index}"
        artifact.mkdir()
        data = artifact / "capture.sigmf-data"
        metadata = artifact / "capture.sigmf-meta"
        record = artifact / "condition-record.json"
        data.write_bytes(f"data-{index}".encode())
        metadata.write_bytes(f"metadata-{index}".encode())
        record.write_bytes(f"record-{index}".encode())
        transfer: dict[str, Any] = {
            "phasor": {"real": 0.25 + index / 100.0, "imag": index / 1000.0},
        }
        if not detected:
            transfer["amplitude_upper_bound_ratio"] = 0.01 + index / 1000.0
        result = {
            "condition_id": condition["condition_id"],
            "attribution_repeat_index": index,
            "attribution_repeat_count": 5,
            "stream_id": 10_000 + index,
            "artifact_data_path": str(data),
            "artifact_data_sha256": _sha256(data),
            "artifact_metadata_path": str(metadata),
            "artifact_metadata_sha256": _sha256(metadata),
            "condition_record_path": str(record),
            "condition_record_sha256": _sha256(record),
            "measurement_quality_passed": True,
            "rx2_tone_detected": detected,
            "rx2_over_rx1": transfer,
        }
        attempts.append(
            {
                "status": "complete",
                "outcome": "measurement_quality_passed",
                "quarantine": None,
                "result": result,
            }
        )
    runtime_preflight = {"kind": "runtime", "expected": runtime}
    fixture_preflight = {"kind": "fixture", "expected": fixture}
    identity = {"kind": "identity", "serial": "serial-a", "uri": "usb:1.2.3"}
    preflight_mute = _mute("serial-a", "preflight")
    final_mute = _mute("serial-a", "final")
    manifest: dict[str, Any] = {
        "status": "complete",
        "completed_at": "2026-08-29T12:01:00+00:00",
        "error": None,
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
        "confirmations": [_confirmation(contract, fixture)],
        "native_runtime_preflight_attempts": [runtime_preflight],
        "native_runtime_preflight": runtime_preflight,
        "fixture_evidence_preflight_attempts": [fixture_preflight],
        "fixture_evidence_preflight": fixture_preflight,
        "identity_preflight_attempts": [identity],
        "identity_preflight": identity,
        "preflight_mute_attempts": [preflight_mute],
        "final_mute_attempts": [final_mute],
        "final_mute": final_mute,
        "selector_initial_state_attempts": [],
        "selector_initial_state": None,
        "final_selector_cleanup_attempts": [],
        "final_selector_cleanup": None,
        "recovery_mute_attempts": [],
        "recovery_selector_cleanup_attempts": [],
        "orphan_quarantine_attempts": [],
        "attempts": attempts,
    }
    fake = FakeRunner(contract)
    manifest["summary"] = fake._manifest_summary(manifest, len(conditions))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return fake, plan_path, manifest_path


def _set_manifest_value(path: Path, mutator: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutator(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_real_runner_imports_without_touching_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", None)
    runner = analyzer._runner()
    assert runner.__name__ in {
        "scripts.run_5g8_leakage_ladder",
        "smateway_5g8_leakage_ladder_verifier",
    }
    assert callable(runner._completed_condition_ids)


def test_stage_admission_revalidates_all_sources_without_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)

    stage = analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")

    assert [repeat.repeat_index for repeat in stage.repeats] == [1, 2, 3, 4, 5]
    assert all(repeat.phasor is None for repeat in stage.repeats)
    assert all(repeat.amplitude_upper_bound_ratio is not None for repeat in stage.repeats)
    assert fake.fixture_refresh_count == 1
    assert len(fake.completed_calls) == 1
    assert fake.completed_calls[0]["downgrade_invalid"] is False
    assert fake.completed_calls[0]["planned"] == {
        "A-repeat-1",
        "A-repeat-2",
        "A-repeat-3",
        "A-repeat-4",
        "A-repeat-5",
    }


@pytest.mark.parametrize(
    ("identity", "message"),
    (
        ("smateway", "Smateway source closure differs"),
        ("dependency", "pluto-plus-utils source closure differs"),
        ("native", "native libiio identity differs"),
    ),
)
def test_stage_admission_rejects_changed_current_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    message: str,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    if identity == "smateway":
        fake.current_commit = "2" * 40
    elif identity == "dependency":
        fake.current_dependency = {"dependency": "changed"}
    else:
        fake.current_native = {"runtime": "changed"}
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)

    with pytest.raises(analyzer.CampaignAnalysisError, match=message):
        analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")

    assert fake.completed_calls == []


@pytest.mark.parametrize("input_name", ("plan", "manifest"))
def test_stage_admission_rejects_symlinked_plan_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_name: str,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)
    original = plan_path if input_name == "plan" else manifest_path
    link = tmp_path / f"linked-{input_name}.json"
    link.symlink_to(original)

    with pytest.raises(analyzer.CampaignAnalysisError, match="symlink"):
        analyzer.load_verified_stage(
            link if input_name == "plan" else plan_path,
            link if input_name == "manifest" else manifest_path,
            stage_name="A",
        )
    assert fake.completed_calls == []


@pytest.mark.parametrize("tamper_kind", ("contract_hash", "semantic_contract"))
def test_stage_admission_rejects_tampered_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if tamper_kind == "contract_hash":
        plan["plan_contract_sha256"] = "0" * 64
    else:
        plan["plan_contract"]["configuration"]["center_frequency_hz"] = 5_700_000_000
        plan["plan_contract_sha256"] = analyzer._canonical_sha256(plan["plan_contract"])
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(analyzer.CampaignAnalysisError, match="immutable plan envelope differs"):
        analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")
    assert fake.completed_calls == []


@pytest.mark.parametrize(
    "path_key",
    ("artifact_data_path", "artifact_metadata_path", "condition_record_path"),
)
def test_runner_path_revalidation_rejects_symlinked_raw_metadata_or_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_key: str,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original = Path(manifest["attempts"][0]["result"][path_key])
    link = tmp_path / f"linked-{original.name}"
    link.symlink_to(original)
    manifest["attempts"][0]["result"][path_key] = str(link)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)

    with pytest.raises(analyzer.CampaignAnalysisError, match="symlink"):
        analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")
    assert fake.completed_calls[-1]["downgrade_invalid"] is False
    assert not (tmp_path / "captures" / ".failed").exists()


@pytest.mark.parametrize(
    "path_key",
    ("artifact_data_path", "artifact_metadata_path", "condition_record_path"),
)
def test_runner_hash_revalidation_rejects_tampered_raw_metadata_or_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_key: str,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = Path(manifest["attempts"][0]["result"][path_key])
    path.write_bytes(b"tampered after manifest acceptance")
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)

    with pytest.raises(analyzer.CampaignAnalysisError, match="hash differs"):
        analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")
    assert fake.completed_calls[-1]["downgrade_invalid"] is False
    assert path.read_bytes() == b"tampered after manifest acceptance"
    assert not (tmp_path / "captures" / ".failed").exists()


def test_duplicate_repeat_source_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, plan_path, manifest_path = _write_case(tmp_path, detected=True)

    def duplicate_source(manifest: dict[str, Any]) -> None:
        first = manifest["attempts"][0]["result"]
        second = manifest["attempts"][1]["result"]
        second["stream_id"] = first["stream_id"]

    _set_manifest_value(manifest_path, duplicate_source)
    monkeypatch.setattr(analyzer, "_LEAKAGE_RUNNER", fake)

    with pytest.raises(analyzer.CampaignAnalysisError, match="five unique acquisitions"):
        analyzer.load_verified_stage(plan_path, manifest_path, stage_name="A")


def test_nondetection_rejects_missing_or_nonpositive_bound() -> None:
    result = {
        "attribution_repeat_index": 1,
        "condition_id": "condition-a",
        "stream_id": 1,
        "artifact_data_sha256": "1" * 64,
        "measurement_quality_passed": True,
        "rx2_tone_detected": False,
        "rx2_over_rx1": {
            "phasor": {"real": 1.0, "imag": 0.0},
            "amplitude_upper_bound_ratio": 0.0,
        },
    }
    with pytest.raises(analyzer.CampaignAnalysisError, match="finite and positive"):
        analyzer._complex_repeat(result)


def _hash(number: int) -> str:
    return f"{number:064x}"


def _campaign_stages() -> list[StageAttributionEvidence]:
    shared = {"fixture": "same-physical-fixture"}
    provenance = {
        "campaign_id": "campaign-a",
        "comparable_fixture_group_id": "group-a",
        "source": {"commit": "1" * 40},
        "acquisition": {"frequency_hz": 5_800_000_000},
    }
    stages: list[StageAttributionEvidence] = []
    for stage_number, stage_name in enumerate(("A", "B", "C", "E"), start=1):
        identity: dict[str, Any] = {
            "topology_stage": TOPOLOGY[stage_name],
            "plan_path": f"/campaign/{stage_name}/plan.json",
            "plan_file_sha256": _hash(100 + stage_number),
            "plan_contract_sha256": _hash(200 + stage_number),
            "manifest_path": f"/campaign/{stage_name}/manifest.json",
            "manifest_file_sha256": _hash(300 + stage_number),
            "fixture_evidence_sha256": _hash(400 + stage_number),
            "shared_fixture_sha256": _hash(500),
            "stage_delta": {"stage": stage_name},
            "stage_delta_sha256": _hash(500 + stage_number),
            "prior_stage_binding": None,
            "setup_attestation_sha256": _hash(600 + stage_number),
            "selector_control_sha256": (_hash(700) if stage_name in {"C", "E"} else None),
            "selector_flash_evidence": None,
            "fixture_characterized": True,
        }
        if stages:
            previous = stages[-1]
            previous_identity = previous.stage_fixture_identity
            comparison_anchor = {
                "from_stage": TOPOLOGY[previous.stage],
                "to_stage": TOPOLOGY[stage_name],
                "prior_stage_delta_sha256": previous_identity["stage_delta_sha256"],
                "preserved_assets": {"fixture": "same"},
            }
            identity["prior_stage_binding"] = {
                "stage": TOPOLOGY[previous.stage],
                "run_id": previous.run_id,
                "plan_path": previous_identity["plan_path"],
                "plan_file_sha256": previous_identity["plan_file_sha256"],
                "plan_contract_sha256": previous_identity["plan_contract_sha256"],
                "fixture_evidence_sha256": previous_identity["fixture_evidence_sha256"],
                "shared_fixture_sha256": previous_identity["shared_fixture_sha256"],
                "prior_stage_delta_sha256": previous_identity["stage_delta_sha256"],
                "comparison_anchor": comparison_anchor,
                "comparison_anchor_sha256": analyzer._canonical_sha256(comparison_anchor),
                "prior_selector_control_sha256": previous_identity["selector_control_sha256"],
                "campaign_id": "campaign-a",
                "comparable_fixture_group_id": "group-a",
                "prior_fixture_characterized": previous_identity["fixture_characterized"],
            }
        repeats = tuple(
            AttributionRepeat(
                repeat_index=repeat,
                condition_id=f"{stage_name}-condition-{repeat}",
                stream_id=stage_number * 100 + repeat,
                artifact_sha256=_hash(stage_number * 100 + repeat),
                quality_passed=True,
                detected=True,
                phasor=complex(0.02 * stage_number + repeat / 10_000, repeat / 100_000),
                amplitude_upper_bound_ratio=None,
            )
            for repeat in range(1, 6)
        )
        stages.append(
            StageAttributionEvidence(
                stage=stage_name,
                run_id=f"run-{stage_name}",
                contemporaneous_group_id="group-a",
                shared_fixture_identity=shared,
                provenance_identity=provenance,
                stage_fixture_identity=identity,
                repeats=repeats,
            )
        )
    return stages


def test_campaign_summary_accepts_exact_order_adjacency_and_source_identity() -> None:
    summary = analyzer.summarize_verified_campaign(_campaign_stages())
    assert summary.stage_e_run_id == "run-E"
    assert [boundary.name for boundary in summary.boundaries] == [
        "B_MINUS_A",
        "C_MINUS_B",
        "E_MINUS_C",
    ]


def test_campaign_summary_rejects_noncanonical_order() -> None:
    stages = _campaign_stages()
    stages[1], stages[2] = stages[2], stages[1]
    with pytest.raises(analyzer.CampaignAnalysisError, match="exact A/B/C/E order"):
        analyzer.summarize_verified_campaign(stages)


def test_campaign_summary_rejects_nonadjacent_plan_binding() -> None:
    stages = _campaign_stages()
    identity = copy.deepcopy(stages[2].stage_fixture_identity)
    identity["prior_stage_binding"]["run_id"] = "run-A"
    stages[2] = StageAttributionEvidence(
        stage=stages[2].stage,
        run_id=stages[2].run_id,
        contemporaneous_group_id=stages[2].contemporaneous_group_id,
        shared_fixture_identity=stages[2].shared_fixture_identity,
        provenance_identity=stages[2].provenance_identity,
        stage_fixture_identity=identity,
        repeats=stages[2].repeats,
    )
    with pytest.raises(analyzer.CampaignAnalysisError, match="exact immediately prior"):
        analyzer.summarize_verified_campaign(stages)


@pytest.mark.parametrize("identity_kind", ("source", "shared"))
def test_pure_summary_rejects_cross_stage_identity_substitution(identity_kind: str) -> None:
    stages = _campaign_stages()
    replacement = stages[1]
    stages[1] = StageAttributionEvidence(
        stage=replacement.stage,
        run_id=replacement.run_id,
        contemporaneous_group_id=replacement.contemporaneous_group_id,
        shared_fixture_identity=(
            {"fixture": "substituted"}
            if identity_kind == "shared"
            else replacement.shared_fixture_identity
        ),
        provenance_identity=(
            {**replacement.provenance_identity, "source": {"commit": "2" * 40}}
            if identity_kind == "source"
            else replacement.provenance_identity
        ),
        stage_fixture_identity=replacement.stage_fixture_identity,
        repeats=replacement.repeats,
    )
    with pytest.raises(LeakageAttributionError, match="identity differs|provenance differs"):
        analyzer.summarize_verified_campaign(stages)


def test_pure_summary_rejects_duplicate_repeat_source_across_stages() -> None:
    stages = _campaign_stages()
    replacement = stages[1]
    duplicate = replacement.repeats[0]
    first = stages[0].repeats[0]
    repeats = (
        AttributionRepeat(
            repeat_index=duplicate.repeat_index,
            condition_id=duplicate.condition_id,
            stream_id=first.stream_id,
            artifact_sha256=first.artifact_sha256,
            quality_passed=duplicate.quality_passed,
            detected=duplicate.detected,
            phasor=duplicate.phasor,
            amplitude_upper_bound_ratio=duplicate.amplitude_upper_bound_ratio,
        ),
        *replacement.repeats[1:],
    )
    stages[1] = StageAttributionEvidence(
        stage=replacement.stage,
        run_id=replacement.run_id,
        contemporaneous_group_id=replacement.contemporaneous_group_id,
        shared_fixture_identity=replacement.shared_fixture_identity,
        provenance_identity=replacement.provenance_identity,
        stage_fixture_identity=replacement.stage_fixture_identity,
        repeats=repeats,
    )
    with pytest.raises(LeakageAttributionError, match="stream IDs must be globally unique"):
        analyzer.summarize_verified_campaign(stages)


def test_output_is_create_only_read_only_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    output = tmp_path / "results" / "campaign.json"
    analyzer._write_new(output, {"schema": 1, "value": "first"})
    original = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    with pytest.raises(analyzer.CampaignAnalysisError, match="already exists"):
        analyzer._write_new(output, {"schema": 1, "value": "replacement"})
    assert output.read_bytes() == original

    target = tmp_path / "target.json"
    target.write_text("do not change", encoding="utf-8")
    linked_output = tmp_path / "linked-output.json"
    linked_output.symlink_to(target)
    with pytest.raises(analyzer.CampaignAnalysisError, match="already exists"):
        analyzer._write_new(linked_output, {"schema": 1})
    assert target.read_text(encoding="utf-8") == "do not change"


def test_output_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(analyzer.CampaignAnalysisError, match="parent contains a symlink"):
        analyzer._write_new(linked_parent / "campaign.json", {"schema": 1})
    assert list(real_parent.iterdir()) == []
