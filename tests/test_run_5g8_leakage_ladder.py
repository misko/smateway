from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pluto_plus.direct_radio.usb import MetadataFlags
from pluto_plus.hardware import SampleBlockV2
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_leakage_ladder.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_leakage_ladder_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

SOURCE_COMMIT = "1" * 40
DEPENDENCY_COMMIT = "2" * 40
SERIAL = "serial-a"
URI = "usb:1.2.3"


def _dependency_attestation() -> dict[str, Any]:
    modules = (
        "pluto_plus.artifacts",
        "pluto_plus.bootstrap_firmware",
        "pluto_plus.hardware",
        "pluto_plus.hardware.iio",
        "pluto_plus.models",
    )
    return {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "repository_path": "/synthetic/pluto-plus-utils",
        "commit": DEPENDENCY_COMMIT,
        "head": DEPENDENCY_COMMIT,
        "clean_worktree_verified": True,
        "files": [{"relative_path": "src/pluto_plus/__init__.py", "sha256": "3" * 64}],
        "imported_modules": [{"module": module} for module in modules],
    }


def _selector_control() -> dict[str, Any]:
    return {
        "schema": 1,
        "mode": "reviewed_static_selector_mailbox_all_off",
        "bench_manifest": {
            "path": "/synthetic/pluto_bench.manifest.json",
            "file_sha256": "4" * 64,
        },
        "openocd_config": {
            "path": "/synthetic/rpi4-swd.cfg",
            "file_sha256": "5" * 64,
        },
        "control_profile": {
            "path": "/synthetic/control_profile.json",
            "file_sha256": "6" * 64,
            "header_path": "/synthetic/control_profile.h",
            "header_file_sha256": "7" * 64,
            "all_off_code": 15,
        },
        "command": {
            "code": 15,
            "lease_ms": 0,
            "wait_until_applied": True,
            "readback_required": True,
        },
    }


def _contract(stage: str = "direct_rx2_termination") -> dict[str, Any]:
    return runner._build_plan_contract(
        run_id=f"run-{stage}",
        board_id="board-a",
        serial=SERIAL,
        uri=URI,
        stage=stage,
        source_commit=SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=_dependency_attestation(),
        selector_control=_selector_control() if stage in runner.SELECTOR_CONNECTED_STAGES else None,
    )


def _plan_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "plan_contract_sha256": "a" * 64,
        "plan_contract_hash_provenance": "synthetic canonical JSON",
        "plan_file_sha256": "b" * 64,
        "plan_file_hash_provenance": "synthetic file bytes",
    }


def _passing_identity(calls: list[tuple[str, str]] | None = None) -> Any:
    def identity(serial: str, requested_uri: str) -> dict[str, Any]:
        if calls is not None:
            calls.append((serial, requested_uri))
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "passed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": requested_uri,
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
            "error": None,
        }

    return identity


def _passing_selector(calls: list[str] | None = None) -> Any:
    def selector(control: dict[str, Any], purpose: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(purpose)
        code = control["command"]["code"]
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "passed",
            "all_off_code": code,
            "readback": {
                "applied_code": code,
                "command_valid": True,
                "lease_active": False,
                "guard_active": False,
                "invalid_command": False,
            },
            "error": None,
        }

    return selector


def _passing_mute(calls: list[str] | None = None) -> Any:
    def mute(serial: str, purpose: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(purpose)
        return {
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        }

    return mute


def _settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=runner.CENTER_FREQUENCY_HZ,
        sample_rate_hz=runner.SAMPLE_RATE_HZ,
        bandwidth_hz=runner.BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=runner.RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _blocks(
    *,
    stream_id: int = 12345,
    rx1_phasor: complex = 400.0 * np.exp(0.4j),
    rx2_phasor: complex = 40.0 * np.exp(-0.8j),
    clipped: bool = False,
    tone_offset_hz: float = runner.TONE_OFFSET_HZ,
    rx2_alternating_phase: bool = False,
) -> list[SampleBlockV2]:
    flags = int(MetadataFlags.SAMPLE_SEQUENCE_VALID | MetadataFlags.HARDWARE_SAMPLE_COUNTER_VALID)
    rng = np.random.default_rng(20260829)
    blocks = []
    first_sample_sequence = 8_000_000
    realtime_base = 1_800_000_000_000_000_000
    monotonic_base = 2_000_000_000_000
    duration_ns = round(runner.SAMPLES_PER_FRAME / runner.SAMPLE_RATE_HZ * 1e9)
    for index in range(runner.FRAME_COUNT):
        start = index * runner.SAMPLES_PER_FRAME
        indices = np.arange(start, start + runner.SAMPLES_PER_FRAME, dtype=np.float64)
        carrier = np.exp(2j * np.pi * tone_offset_hz * indices / runner.SAMPLE_RATE_HZ)
        noise1 = 2.0 * (
            rng.standard_normal(runner.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.SAMPLES_PER_FRAME)
        )
        noise2 = 2.0 * (
            rng.standard_normal(runner.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.SAMPLES_PER_FRAME)
        )
        samples = np.asarray(
            [rx1_phasor * carrier + noise1, rx2_phasor * carrier + noise2],
            dtype=np.complex64,
        )
        if rx2_alternating_phase:
            analysis_block_samples = round(0.010 * runner.SAMPLE_RATE_HZ)
            for local_start in range(0, runner.SAMPLES_PER_FRAME, analysis_block_samples):
                local_stop = min(
                    local_start + analysis_block_samples,
                    runner.SAMPLES_PER_FRAME,
                )
                analysis_index = (start + local_start) // analysis_block_samples
                samples[1, local_start:local_stop] *= np.exp(
                    1j * np.deg2rad(35.0 if analysis_index % 2 else -35.0)
                )
        if clipped and index == 0:
            samples[1, 0] = 2_047.0 + 0j
        realtime_start = realtime_base + index * duration_ns
        monotonic_start = monotonic_base + index * duration_ns
        blocks.append(
            SampleBlockV2(
                utc_ns=realtime_start + duration_ns // 2,
                samples=samples,
                stream_id=stream_id,
                buffer_sequence=index,
                first_sample_sequence=(first_sample_sequence + index * runner.SAMPLES_PER_FRAME),
                metadata_flags=flags,
                metadata_abi=2,
                missing_samples_before=0,
                sample_time_realtime_start_ns=realtime_start,
                sample_time_realtime_end_ns=realtime_start + duration_ns,
                sample_time_monotonic_start_ns=monotonic_start,
                sample_time_monotonic_end_ns=monotonic_start + duration_ns,
                sample_time_uncertainty_ns=1_000,
            )
        )
    return blocks


def _capture_boundary(
    blocks: list[SampleBlockV2],
    *,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    def capture(
        plan: Any,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Any,
    ) -> Any:
        if calls is not None:
            calls.append(
                {
                    "plan": plan,
                    "samples_per_frame": samples_per_frame,
                    "frame_count": frame_count,
                    "kernel_buffers": kernel_buffers,
                }
            )
        for block in blocks:
            block_consumer(block)
        scales = [0.0] * 8
        scales[0] = runner.DDS_SCALE
        scales[2] = runner.DDS_SCALE
        enabled = [False] * 8
        enabled[0] = True
        enabled[2] = True
        frequencies = [0] * 8
        frequencies[0] = runner.TONE_OFFSET_HZ
        frequencies[2] = -runner.TONE_OFFSET_HZ
        identity = RadioIdentity(
            radio_id=SERIAL,
            serial=SERIAL,
            uri=URI,
            transport=Transport.IIO_USB,
            model="synthetic Pluto",
            firmware_version="v0.39-v7",
            usb_path="/synthetic/usb",
        )
        return SimpleNamespace(
            identity=identity,
            settings=_settings(),
            frames=tuple(blocks),
            sample_count=sum(block.sample_count for block in blocks),
            kernel_buffers=kernel_buffers,
            tx_gain_readback_db=plan.tx_hardware_gain_db,
            dds_scale_readback=tuple(scales),
            dds_enabled_readback=tuple(enabled),
            dds_frequency_readback_hz=tuple(frequencies),
        )

    return capture


def test_plan_contract_freezes_exact_stages_and_bounded_tx1_ladder() -> None:
    assert tuple(runner.STAGE_CONTRACTS) == (
        "direct_rx2_termination",
        "rx2_cable_terminated",
        "powered_selector_all_inputs_terminated",
        "full_conducted_fixture",
    )
    for stage in runner.STAGES:
        contract = _contract(stage)
        conditions = contract["conditions"]
        assert contract["topology_stage"] == stage
        assert contract["stage_contract"]["confirmation_token"]
        assert contract["interpretation"]["selector_calibration_claim"] is False
        assert contract["interpretation"]["may_be_used_as_selector_calibration"] is False
        assert contract["interpretation"]["one_hot_path_diagnosis"] == {
            "implemented_by_this_runner": False,
            "required_future_runner": "run_5g8_one_hot_path_ladder.py",
            "reason": (
                "per-port path response requires a separate immutable state/readback plan; "
                "the present runner measures only static ALL_OFF topology leakage"
            ),
        }
        assert (
            contract["source"]["pluto_plus_utils_source_attestation"]["commit"] == DEPENDENCY_COMMIT
        )
        assert len(contract["source"]["pluto_plus_utils_source_attestation_sha256"]) == 64
        assert (contract["selector_control"] is not None) == (
            stage in runner.SELECTOR_CONNECTED_STAGES
        )
        assert contract["storage"]["medium"] == "raspberry_pi_local_filesystem"
        assert contract["storage"]["pluto_onboard_storage_used"] is False
        assert [item["tx_hardware_gain_db"] for item in conditions] == list(
            runner.TX_HARDWARE_GAINS_DB
        )
        assert all(item["center_frequency_hz"] == 5_800_000_000 for item in conditions)
        assert all(item["tone_offset_hz"] == 100_000 for item in conditions)
        assert all(item["tx_channel"] == 0 for item in conditions)
        assert all(item["tx2_required_exact_muted"] is True for item in conditions)
        assert all(item["kernel_buffers"] == 8 for item in conditions)
        assert all(item["fresh_stream_required"] is True for item in conditions)
        assert contract["configuration"]["automatic_retry_count"] == 0


@pytest.mark.parametrize(
    ("serial", "uri", "message"),
    [
        ("", URI, "serial"),
        (SERIAL, "ip:192.168.2.1", "exact current"),
        (SERIAL, "pluto://usb:1.2.3", "exact current"),
        (SERIAL, "usb:any", "exact current"),
    ],
)
def test_plan_rejects_nonexact_device_identity(serial: str, uri: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        runner._build_plan_contract(
            run_id="run-a",
            board_id="board-a",
            serial=serial,
            uri=uri,
            stage="direct_rx2_termination",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
        )


def test_selector_connected_plan_requires_static_all_off_control_contract() -> None:
    with pytest.raises(ValueError, match="static ALL_OFF"):
        runner._build_plan_contract(
            run_id="run-selector",
            board_id="board-a",
            serial=SERIAL,
            uri=URI,
            stage="powered_selector_all_inputs_terminated",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
        )


def test_immutable_plan_is_create_only_hash_bound_and_idempotent(tmp_path: Path) -> None:
    contract = _contract()
    path = tmp_path / "run" / runner.PLAN_FILENAME

    first = runner._prepare_plan(path, contract)
    second = runner._prepare_plan(path, contract)

    assert first == second
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert first["plan_contract_sha256"] == runner.canonical_json_sha256(contract)
    evidence = runner._plan_file_evidence(path, first)
    assert evidence["plan_file_sha256"] == runner.sha256_path(path)
    assert evidence["plan_contract_sha256"] == first["plan_contract_sha256"]
    assert evidence["plan_file_hash_provenance"] != evidence["plan_contract_hash_provenance"]

    os.chmod(path, 0o600)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["plan_contract"]["conditions"][0]["tx_channel"] = 1
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(runner.LeakageLadderError, match="hash"):
        runner._prepare_plan(path, contract)


def test_execution_confirmations_are_exact_and_stage_specific() -> None:
    stage = "powered_selector_all_inputs_terminated"
    token = runner.STAGE_CONTRACTS[stage]["confirmation_token"]
    confirmation = runner._validate_confirmations(
        stage=stage,
        confirm_stage=stage,
        topology_token=token,
        no_antennas=True,
        tx1_matched=True,
        tx2_terminated_muted=True,
        rx1_conducted_reference=True,
        selector_static_all_off=True,
    )
    assert confirmation["stage"] == stage
    assert confirmation["topology_confirmation_token"] == token

    with pytest.raises(runner.LeakageLadderError, match="topology-token"):
        runner._validate_confirmations(
            stage=stage,
            confirm_stage=stage,
            topology_token="DIRECT_RX2_50OHM_AT_PLUTO",
            no_antennas=True,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            selector_static_all_off=True,
        )
    with pytest.raises(runner.LeakageLadderError, match="no-antennas"):
        runner._validate_confirmations(
            stage=stage,
            confirm_stage=stage,
            topology_token=token,
            no_antennas=False,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            selector_static_all_off=True,
        )


def test_condition_capture_persists_raw_abi2_artifact_and_markerless_analysis(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    calls: list[dict[str, Any]] = []
    mute_calls: list[str] = []
    capture_root = tmp_path / "captures"

    result = runner._capture_condition(
        condition,
        contract=contract,
        plan_evidence=_plan_evidence(tmp_path / "plan.json"),
        capture_root=capture_root,
        forbidden_stream_ids=set(),
        capture_boundary=_capture_boundary(_blocks(tone_offset_hz=100_037.0), calls=calls),
        mute_boundary=_passing_mute(mute_calls),
    )

    assert mute_calls == ["post_condition"]
    assert len(calls) == 1
    live = calls[0]
    assert live["samples_per_frame"] == runner.SAMPLES_PER_FRAME
    assert live["frame_count"] == runner.FRAME_COUNT
    assert live["kernel_buffers"] == 8
    assert live["plan"].tx_channel == 0
    assert live["plan"].tx_hardware_gain_db == -35.0
    assert live["plan"].tone_frequency_hz == 100_000
    assert result["metadata_abi"] == 2
    assert result["stream_id"] == 12345
    assert result["headroom_passed"] is True
    assert result["measurement_quality_passed"] is True
    assert result["tone_offset_hz_requested"] == 100_000
    assert result["tone_offset_hz_readback"] == 100_000
    assert result["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert result["pilot_confidence"] >= runner.MINIMUM_PILOT_CONFIDENCE
    assert result["selector_calibration_claim"] is False

    artifact_root = Path(result["artifact_path"])
    record = json.loads((artifact_root / runner.CONDITION_RECORD_NAME).read_text())
    assert record["continuity_audit"]["metadata_abi"] == 2
    assert record["continuity_audit"]["abi2_flags_counters_order_and_rate_verified"] is True
    assert record["capture"]["kernel_buffers"] == 8
    assert record["capture"]["tx_channel"] == 0
    assert record["capture"]["tx2_required_exact_muted"] is True
    assert record["capture"]["tone_offset_hz_requested"] == 100_000
    assert record["capture"]["tone_offset_hz_readback"] == 100_000
    assert record["capture"]["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert record["capture"]["pilot_frequency_refinement"]["quality_passed"] is True
    assert record["capture"]["rf_readback_evidence"]["tx_hardware_gain_readback_db_by_channel"] == [
        -35.0,
        -80.0,
    ]
    assert record["marker_independent_analysis"]["rx2_over_rx1"]["amplitude_ratio"] == (
        pytest.approx(0.1, rel=0.01)
    )
    assert record["accepted_for_selector_calibration"] is False
    assert record["may_be_used_as_selector_calibration"] is False


def test_reused_stream_id_is_rejected_and_quarantined(tmp_path: Path) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    with pytest.raises(runner.ConditionCaptureFailure) as captured:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids={12345},
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=_passing_mute(),
        )

    quarantine = captured.value.quarantine
    assert quarantine["accepted"] is False
    assert quarantine["may_be_used_for_selector_calibration"] is False
    assert Path(quarantine["path"]).is_dir()
    assert any(item["name"].endswith(".sigmf-data") for item in quarantine["files"])
    assert not [
        path
        for path in (tmp_path / "captures").iterdir()
        if path.is_dir() and path.name != ".failed"
    ]


def test_enodata_fragment_and_headroom_failure_are_quarantined(tmp_path: Path) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    fragment = _blocks()

    def enodata_capture(plan: Any, **kwargs: Any) -> Any:
        del plan
        kwargs["block_consumer"](fragment[0])
        raise OSError(61, "No data available")

    with pytest.raises(runner.ConditionCaptureFailure, match="No data") as enodata:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "enodata",
            forbidden_stream_ids=set(),
            capture_boundary=enodata_capture,
            mute_boundary=_passing_mute(),
        )
    assert enodata.value.quarantine["accepted"] is False
    assert any(item["name"] == "failure.json" for item in enodata.value.quarantine["files"])

    with pytest.raises(runner.ConditionCaptureFailure, match="headroom") as headroom:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "headroom",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(stream_id=23456, clipped=True)),
            mute_boundary=_passing_mute(),
        )
    assert headroom.value.quarantine["accepted"] is False
    assert not [
        path
        for path in (tmp_path / "headroom").iterdir()
        if path.is_dir() and path.name != ".failed"
    ]


def test_stage_execution_uses_preflight_condition_and_final_exact_mutes(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    confirmation = {
        "stage": contract["topology_stage"],
        "topology_confirmation_token": contract["stage_contract"]["confirmation_token"],
    }
    mute_calls: list[str] = []

    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=confirmation,
        capture_root=tmp_path / "captures",
        capture_boundary=_capture_boundary(_blocks(stream_id=34567)),
        mute_boundary=_passing_mute(mute_calls),
        identity_boundary=_passing_identity(),
    )

    assert mute_calls == ["preflight", "post_condition", "final"]
    assert manifest["status"] == "complete"
    assert manifest["final_mute"]["status"] == "passed"
    assert manifest["attempts"][0]["status"] == "complete"
    assert manifest["attempts"][0]["automatic_retry_attempted"] is False
    assert manifest["summary"]["completed_conditions"] == 1
    assert manifest["summary"]["selector_calibration_claim"] is False


def test_read_only_identity_mismatch_blocks_all_mute_and_capture_boundaries(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []
    capture_calls: list[dict[str, Any]] = []

    def mismatched_identity(serial: str, requested_uri: str) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": "usb:9.9.9",
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "error": None,
        }

    with pytest.raises(runner.LeakageLadderError, match="read-only USB identity"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=mismatched_identity,
        )

    assert capture_calls == []
    assert mute_calls == []
    assert manifest["status"] == "failed"
    assert manifest["identity_preflight"]["resolved_uri"] == "usb:9.9.9"


def test_selector_connected_condition_attests_static_all_off_before_and_after(
    tmp_path: Path,
) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    selector_calls: list[str] = []

    runner._execute_stage(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation={"stage": contract["topology_stage"]},
        capture_root=tmp_path / "captures",
        capture_boundary=_capture_boundary(_blocks(stream_id=45678)),
        mute_boundary=_passing_mute(),
        identity_boundary=_passing_identity(),
        selector_boundary=_passing_selector(selector_calls),
    )

    assert selector_calls == ["before_condition", "after_condition"]
    result = manifest["attempts"][0]["result"]
    assert result["selector_static_all_off_before"]["status"] == "passed"
    assert result["selector_static_all_off_after"]["status"] == "passed"


def test_selector_all_off_readback_failure_blocks_rf_capture(tmp_path: Path) -> None:
    contract = _contract("powered_selector_all_inputs_terminated")
    capture_calls: list[dict[str, Any]] = []

    def failed_selector(control: dict[str, Any], purpose: str) -> dict[str, Any]:
        code = control["command"]["code"]
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "all_off_code": code,
            "readback": {
                "applied_code": code ^ 1,
                "command_valid": True,
                "lease_active": False,
                "guard_active": False,
                "invalid_command": False,
            },
            "error": None,
        }

    with pytest.raises(runner.ConditionCaptureFailure, match="pre-condition"):
        runner._capture_condition(
            contract["conditions"][0],
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(),
            selector_boundary=failed_selector,
        )

    assert capture_calls == []


def test_measurement_quality_rejection_makes_stage_unsuccessful_but_keeps_raw_artifact(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"

    with pytest.raises(runner.LeakageLadderError, match="measurement quality"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(
                _blocks(stream_id=56789, rx2_alternating_phase=True)
            ),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
        )

    assert manifest["status"] == "failed"
    attempt = manifest["attempts"][0]
    assert attempt["status"] == "complete"
    assert attempt["outcome"] == "measurement_quality_rejected"
    assert attempt["quarantine"] is None
    assert Path(attempt["result"]["artifact_path"]).is_dir()
    assert (
        "detected_rx2_transfer_coherence_below_minimum"
        in attempt["result"]["measurement_quality_rejection_reasons"]
    )


def test_capture_failure_still_runs_final_mute_and_persists_failed_manifest(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []

    def failed_capture(plan: Any, **kwargs: Any) -> Any:
        del plan, kwargs
        raise OSError(61, "No data available")

    with pytest.raises(runner.ConditionCaptureFailure, match="No data"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=failed_capture,
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
        )

    assert mute_calls == ["preflight", "post_condition", "final"]
    persisted = json.loads(manifest_path.read_text())
    assert persisted["status"] == "failed"
    assert persisted["attempts"][0]["status"] == "failed"
    assert persisted["attempts"][0]["quarantine"]["accepted"] is False
    assert persisted["final_mute"]["status"] == "passed"


def test_malformed_post_condition_mute_is_failed_and_quarantined(tmp_path: Path) -> None:
    contract = _contract()

    def malformed_mute(serial: str, purpose: str) -> Any:
        del serial, purpose
        return None

    with pytest.raises(runner.ConditionCaptureFailure) as captured:
        runner._capture_condition(
            contract["conditions"][0],
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=malformed_mute,
        )

    assert captured.value.post_mute["status"] == "failed"
    assert captured.value.post_mute["error"]["type"] == "InvalidMuteAttestation"
    assert captured.value.quarantine["accepted"] is False


def test_failed_attempt_cannot_be_retried_if_top_level_status_is_damaged(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["conditions"] = contract["conditions"][:1]
    envelope = runner._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest["status"] = "prepared"
    manifest["attempts"].append(
        {
            "condition_id": contract["conditions"][0]["condition_id"],
            "status": "failed",
        }
    )
    mute_calls: list[str] = []

    with pytest.raises(runner.LeakageLadderError, match="cannot retry"):
        runner._execute_stage(
            manifest,
            tmp_path / "manifest.json",
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
        )

    assert mute_calls == ["resume_recovery"]
    assert len(manifest["attempts"]) == 1


def test_json_serialization_is_deterministic_for_complex_and_nonfinite_values() -> None:
    value = runner._json_safe(
        {
            "phasor": 1.5 - 2.5j,
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
        }
    )
    assert value == {
        "phasor": {"real": 1.5, "imag": -2.5},
        "positive_infinity": None,
        "negative_infinity": None,
    }
    json.dumps(value, allow_nan=False)
