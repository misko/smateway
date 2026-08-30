from __future__ import annotations

import importlib.util
import json
import stat
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_arm_preserving_d2.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_arm_preserving_d2_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _contract(tmp_path: Path, run_id: str = "arm-run-a") -> dict[str, Any]:
    condition_root = tmp_path / "runs" / run_id
    return {
        "schema": 1,
        "run_kind": runner.RUN_KIND,
        "run_id": run_id,
        "condition_id": f"campaign.c_i.ANT1.repeat-1.{run_id}",
        "storage": {
            "condition_root": str(condition_root),
            "capture_root": str(tmp_path / "captures" / run_id),
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
        },
    }


def _prepared(tmp_path: Path, run_id: str = "arm-run-a") -> tuple[dict[str, Any], Path, Path]:
    contract = _contract(tmp_path, run_id)
    root = Path(contract["storage"]["condition_root"])
    plan = root / runner.PLAN_FILENAME
    manifest = root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan, manifest, contract)
    return contract, plan, manifest


def _selector_control(tmp_path: Path) -> dict[str, Any]:
    source_manifest = (
        Path(__file__).resolve().parents[1] / "build/STM32C011F4P6/bench/pluto_bench.manifest.json"
    )
    manifest_path = tmp_path / "pluto_bench.manifest.json"
    manifest_path.write_bytes(source_manifest.read_bytes())
    config_path = tmp_path / "openocd.cfg"
    config_path.write_text("# synthetic\n", encoding="utf-8")
    selector_path = tmp_path / "selector-flash.json"
    selector_path.write_text('{"sealed":true}\n', encoding="utf-8")
    firmware_path = tmp_path / "pluto_bench.bin"
    firmware_path.write_bytes(b"reviewed bench image")
    manifest = runner.BenchManifest.load(manifest_path)
    return {
        "schema": 1,
        "control_kind": "sealed_bench_static_all_off",
        "selector_flash_attestation": runner._file_evidence(selector_path, "selector"),
        "build_manifest": runner._file_evidence(manifest_path, "manifest"),
        "openocd_config": runner._file_evidence(config_path, "OpenOCD config"),
        "target_image_admission": {
            "schema": 1,
            "flash_base_address": runner.FLASH_BASE_ADDRESS,
            "firmware_bin": runner._file_evidence(firmware_path, "firmware"),
            "board_id": runner.DEFAULT_BOARD_ID,
            "expected_uid": runner.DEFAULT_BOARD_ID.removeprefix("stm32c011-"),
            "selector_flash_attestation_sha256": runner.sha256_path(selector_path),
            "full_bin_extent_and_uid_required_before_mailbox": True,
            "mismatch_must_remain_halted": True,
        },
        "all_off_code": 8,
        "mailbox": {
            "address": manifest.address,
            "size": manifest.size,
            "magic": manifest.magic,
            "version": manifest.version,
            "max_lease_ms": manifest.max_lease_ms,
            "offsets": manifest.offsets,
        },
        "gpioa_odr_address": runner.GPIOA_ODR_ADDRESS,
        "selector_gpio_mask": runner.SELECTOR_GPIO_MASK,
        "required_lease_ms": 0,
        "live_raw_mailbox_and_gpio_readback_required": True,
    }


def _selector_evidence(
    tmp_path: Path, control: dict[str, Any], purpose: str = "before_capture"
) -> dict[str, Any]:
    root = tmp_path / purpose
    root.mkdir()
    manifest = runner.BenchManifest.load(Path(control["build_manifest"]["path"]))
    words = [0] * 9
    values = {
        "magic": manifest.magic,
        "version": manifest.version,
        "command_sequence": 7,
        "command_code": 8,
        "command_lease_ms": 0,
        "acknowledged_sequence": 7,
        "applied_code": 8,
        "remaining_lease_ms": 0,
        "status_flags": 1,
    }
    for name, value in values.items():
        words[manifest.offsets[name] // 4] = value
    mailbox_path = root / "mailbox.bin"
    mailbox_path.write_bytes(struct.pack("<9I", *words))
    gpio_path = root / "gpio.bin"
    gpio_path.write_bytes((8).to_bytes(4, "little"))
    log_path = root / "openocd.json"
    log_path.write_text('{"returncode":0}\n', encoding="utf-8")
    status = runner.decode_mailbox(mailbox_path.read_bytes(), manifest)
    return {
        "status": "passed",
        "purpose": purpose,
        "control_sha256": runner.canonical_sha256(control),
        "all_off_code": 8,
        "lease_ms": 0,
        "mailbox": status.as_dict(),
        "gpioa_odr_raw_value": 8,
        "gpioa_odr_masked_selector_code": 8,
        "mailbox_readback": runner._file_evidence(mailbox_path, "mailbox"),
        "gpioa_odr_readback": runner._file_evidence(gpio_path, "gpio"),
        "openocd_log": runner._file_evidence(log_path, "log"),
        "command_valid": True,
        "raw_mailbox_and_gpio_readback_passed": True,
        "error": None,
    }


def _mute(serial: str, purpose: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": purpose,
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": [-80.0, -80.0],
        "dds_scale_readback": [0.0] * 8,
        "error": None,
    }


def _sequence_contract(tmp_path: Path, control: dict[str, Any]) -> dict[str, Any]:
    return {
        "configuration": {"serial": runner.DEFAULT_SERIAL, "uri": "usb:1.2.3"},
        "fixture": {"document": {}, "file": {}},
        "setup_attestation": {"file": {}},
        "selector_control": control,
        "source": {"native_libiio": {}},
        "storage": {
            "condition_root": str(tmp_path / "condition"),
            "capture_root": str(tmp_path / "captures"),
        },
    }


def _patch_sequence_preflight(monkeypatch: pytest.MonkeyPatch, control: dict[str, Any]) -> None:
    monkeypatch.setattr(runner, "validate_fixture_v2", lambda _value: object())
    monkeypatch.setattr(runner, "_verify_file_evidence", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner, "_validate_sealed_selector_for_fixture", lambda _fixture: ({}, control)
    )
    monkeypatch.setattr(runner, "_validate_selector_control", lambda value: dict(value))
    monkeypatch.setattr(runner, "validate_runtime_attestation", lambda value: dict(value))
    monkeypatch.setattr(runner, "_tone_plan", lambda _contract: object())
    monkeypatch.setattr(
        runner,
        "_live_source_binding",
        lambda *_a, **_k: {"schema": 1, "source": "sealed"},
    )
    monkeypatch.setattr(
        runner,
        "_target_image_passed",
        lambda value, **_kwargs: value.get("status") == "passed",
    )
    monkeypatch.setattr(
        runner,
        "_target_halt_passed",
        lambda value, **_kwargs: value.get("status") == "passed",
    )
    monkeypatch.setattr(
        runner,
        "_selector_passed",
        lambda value, **_kwargs: isinstance(value, dict) and value.get("status") == "passed",
    )
    monkeypatch.setattr(
        runner,
        "_quarantine_blocks",
        lambda *_a, **_k: {"path": "/local/failed", "accepted": False},
    )


def test_parser_freezes_one_role_arm_repeat_and_local_state() -> None:
    options = {option for action in runner._parser()._actions for option in action.option_strings}
    assert {"--role", "--arm", "--repeat-index", "--fixture", "--setup-attestation"} <= (options)
    assert "--state-root" in options
    assert runner.FRAME_COUNT == 3
    assert runner.KERNEL_BUFFERS == 8
    assert runner.TX_HARDWARE_GAIN_DB == -20.0
    assert runner.ROLES == ("c_i", "d2_i")


def test_success_burns_run_and_accepts_exactly_one_stream(tmp_path: Path) -> None:
    contract, plan, manifest = _prepared(tmp_path)

    def execute(_contract: dict[str, Any]) -> dict[str, Any]:
        return {"accepted_stream_count": 1, "stream_id": "123"}

    result = runner._execute_prepared(
        plan_path=plan,
        manifest_path=manifest,
        expected_contract=contract,
        execute_boundary=execute,
    )
    assert result["status"] == "complete"
    assert result["accepted_stream_count"] == 1
    tombstone = plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    assert tombstone.is_file()
    assert tombstone.stat().st_mode & stat.S_IWUSR == 0
    with pytest.raises(runner.ArmPreservingRunError, match="never-attempted"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            execute_boundary=execute,
        )


def test_failure_writes_immutable_tombstone_and_accepts_no_stream(tmp_path: Path) -> None:
    contract, plan, manifest = _prepared(tmp_path, "arm-run-failed")

    def fail(_contract: dict[str, Any]) -> dict[str, Any]:
        raise OSError("ENODATA")

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            execute_boundary=fail,
        )
    observed = json.loads(manifest.read_text(encoding="utf-8"))
    failure = plan.parent / runner.FAILURE_TOMBSTONE_FILENAME
    assert observed["status"] == "failed"
    assert observed["accepted_stream_count"] == 0
    assert failure.is_file()
    assert failure.stat().st_mode & stat.S_IWUSR == 0
    assert json.loads(failure.read_text(encoding="utf-8"))["accepted_artifact"] is False


def test_selector_admission_requires_raw_mailbox_gpio_and_lease_free_all_off(
    tmp_path: Path,
) -> None:
    control = _selector_control(tmp_path)
    evidence = _selector_evidence(tmp_path, control)
    assert runner._selector_passed(evidence, control=control, purpose="before_capture")
    evidence["raw_mailbox_and_gpio_readback_passed"] = False
    assert not runner._selector_passed(evidence, control=control, purpose="before_capture")
    evidence["raw_mailbox_and_gpio_readback_passed"] = True
    evidence["lease_ms"] = 100
    assert not runner._selector_passed(evidence, control=control, purpose="before_capture")


def test_failed_memory_capture_is_quarantined_and_never_accepted(tmp_path: Path) -> None:
    contract = _contract(tmp_path, "arm-run-quarantine")
    evidence = runner._quarantine_blocks(contract, [], OSError("ENODATA"))
    assert evidence["accepted"] is False
    assert evidence["may_be_used_for_closure"] is False
    failure = Path(evidence["path"]) / "failure.json"
    document = json.loads(failure.read_text(encoding="utf-8"))
    assert document["automatic_retry_attempted"] is False
    assert document["retained_frame_count"] == 0


def test_local_state_rejects_known_removable_mounts() -> None:
    with pytest.raises(runner.ArmPreservingRunError, match="local Raspberry Pi"):
        runner._safe_local_state_root(Path("/media/pi/Pluto"))
    with pytest.raises(runner.ArmPreservingRunError, match="local Raspberry Pi"):
        runner._safe_local_state_root(Path("/mnt/capture"))


def test_local_state_compares_nearest_existing_filesystem_device(tmp_path: Path) -> None:
    planned = tmp_path / "not-created" / "state"
    assert runner._safe_local_state_root(planned) == planned.absolute()
    with pytest.raises(runner.ArmPreservingRunError, match="local RPi storage device"):
        runner._safe_local_state_root(Path("/proc/smateway-arm-preserving"))


def test_execute_rechecks_local_storage_before_burning_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan, manifest = _prepared(tmp_path)
    invoked = False

    def reject(_path: Path, *, label: str) -> Path:
        raise runner.FileArtifactAdmissionError(f"{label} is not on local RPi storage device")

    def execute(_contract: dict[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 1}

    monkeypatch.setattr(runner, "assert_local_rpi_storage", reject)
    with pytest.raises(runner.ArmPreservingRunError, match="local RPi storage device"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            execute_boundary=execute,
        )
    assert not invoked
    assert not (plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME).exists()


def test_exact_mute_then_identity_then_image_precedes_any_mailbox_and_mismatch_stays_halted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _selector_control(tmp_path)
    contract = _sequence_contract(tmp_path, control)
    _patch_sequence_preflight(monkeypatch, control)
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(f"mute:{purpose}")
        return _mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        calls.append("identity")
        return {
            "status": "passed",
            "serial": serial,
            "requested_uri": uri,
            "resolved_uri": uri,
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
        }

    def target(*_args: object) -> dict[str, Any]:
        calls.append("target-image")
        return {
            "status": "failed",
            "target_kept_halted_on_failure": True,
            "mailbox_access_performed": False,
        }

    def forbidden_selector(*_args: object) -> dict[str, Any]:
        calls.append("MAILBOX")
        raise AssertionError("mailbox must not be touched after image mismatch")

    def halt(*_args: object) -> dict[str, Any]:
        calls.append("target-halt")
        return {"status": "passed"}

    with pytest.raises(runner.ArmPreservingRunError, match="full-BIN extent/UID"):
        runner._execute_one_stream(
            contract,
            capture_boundary=lambda *_a, **_k: pytest.fail("capture must not start"),
            mute_boundary=mute,
            identity_boundary=identity,
            target_image_boundary=target,
            target_halt_boundary=halt,
            selector_boundary=forbidden_selector,
            native_boundary=lambda: {},
        )
    assert calls == [
        "mute:pre_capture_exact_mute",
        "identity",
        "target-image",
        "target-halt",
        "mute:final_acceptance_exact_mute",
    ]


def test_early_mailbox_failure_still_final_mutes_and_runs_verified_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _selector_control(tmp_path)
    contract = _sequence_contract(tmp_path, control)
    _patch_sequence_preflight(monkeypatch, control)
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(f"mute:{purpose}")
        return _mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        calls.append("identity")
        return {
            "status": "passed",
            "serial": serial,
            "requested_uri": uri,
            "resolved_uri": uri,
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
        }

    def target(*_args: object) -> dict[str, Any]:
        calls.append("target-image")
        return {"status": "passed"}

    def selector(_control: dict[str, Any], purpose: str, _root: Path) -> dict[str, Any]:
        calls.append(f"selector:{purpose}")
        return {"status": "failed" if purpose == "before_capture" else "passed"}

    with pytest.raises(runner.ArmPreservingRunError, match="pre-capture"):
        runner._execute_one_stream(
            contract,
            capture_boundary=lambda *_a, **_k: pytest.fail("capture must not start"),
            mute_boundary=mute,
            identity_boundary=identity,
            target_image_boundary=target,
            selector_boundary=selector,
            native_boundary=lambda: {},
        )
    assert calls == [
        "mute:pre_capture_exact_mute",
        "identity",
        "target-image",
        "selector:before_capture",
        "mute:final_acceptance_exact_mute",
        "selector:after_capture",
        "selector:cleanup_all_off",
    ]


def test_late_target_evidence_rejection_halts_before_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _selector_control(tmp_path)
    contract = _sequence_contract(tmp_path, control)
    _patch_sequence_preflight(monkeypatch, control)
    monkeypatch.setattr(runner, "_target_image_passed", lambda *_a, **_k: False)
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(f"mute:{purpose}")
        return _mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        calls.append("identity")
        return {
            "status": "passed",
            "serial": serial,
            "requested_uri": uri,
            "resolved_uri": uri,
            "exact_uri_match": True,
            "scan_mutates_radio_state": False,
        }

    def target(*_args: object) -> dict[str, Any]:
        calls.append("target-returned-after-reset-run")
        return {"status": "passed", "claimed_target_running": True}

    def halt(*_args: object) -> dict[str, Any]:
        calls.append("independent-halt")
        return {"status": "passed"}

    with pytest.raises(runner.ArmPreservingRunError, match="independent halt proven"):
        runner._execute_one_stream(
            contract,
            capture_boundary=lambda *_a, **_k: pytest.fail("capture must not start"),
            mute_boundary=mute,
            identity_boundary=identity,
            target_image_boundary=target,
            target_halt_boundary=halt,
            selector_boundary=lambda *_a, **_k: pytest.fail("mailbox must not be touched"),
            native_boundary=lambda: {},
        )
    assert calls == [
        "mute:pre_capture_exact_mute",
        "identity",
        "target-returned-after-reset-run",
        "independent-halt",
        "mute:final_acceptance_exact_mute",
    ]
    rejection = json.loads(
        (
            Path(contract["storage"]["condition_root"])
            / "selector-live-evidence/target-admission-rejection.json"
        ).read_text(encoding="utf-8")
    )
    assert rejection["independent_rejection_halt_passed"] is True
    assert rejection["mailbox_access_performed"] is False


def test_live_target_mismatch_explicitly_halts_and_never_resets_or_uses_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _selector_control(tmp_path)
    target = control["target_image_admission"]
    firmware = Path(target["firmware_bin"]["path"])
    calls: list[str] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> Any:
        command = argv[-1]
        calls.append(command)
        if "dump_image" in command:
            root = tmp_path / "live" / "target-image-admission"
            (root / "target-flash.bin").write_bytes(b"X" * firmware.stat().st_size)
            (root / "target-uid.bin").write_bytes(
                bytes.fromhex(runner.DEFAULT_BOARD_ID.removeprefix("stm32c011-"))
            )
        return runner.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    source = {"schema": 1, "source": "sealed"}
    evidence = runner._live_target_image_admission(control, tmp_path / "live", source)
    assert evidence["status"] == "failed"
    assert evidence["target_kept_halted_on_failure"] is True
    assert evidence["mailbox_access_performed"] is False
    assert calls[-1] == "init; halt; shutdown"
    assert all("reset run" not in command for command in calls)
    assert runner._target_image_passed(evidence, control=control, source_binding=source) is False


def test_exact_readback_reset_run_failure_issues_separate_attested_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _selector_control(tmp_path)
    target = control["target_image_admission"]
    firmware = Path(target["firmware_bin"]["path"])
    calls: list[str] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> Any:
        command = argv[-1]
        calls.append(command)
        if "dump_image" in command:
            root = tmp_path / "live" / "target-image-admission"
            (root / "target-flash.bin").write_bytes(firmware.read_bytes())
            (root / "target-uid.bin").write_bytes(
                bytes.fromhex(runner.DEFAULT_BOARD_ID.removeprefix("stm32c011-"))
            )
            return runner.subprocess.CompletedProcess(argv, 0, "", "")
        if command == "init; reset run; shutdown":
            return runner.subprocess.CompletedProcess(argv, 1, "", "reset failed")
        assert command == "init; halt; shutdown"
        return runner.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    source = {"schema": 1, "source": "sealed"}
    evidence = runner._live_target_image_admission(control, tmp_path / "live", source)
    assert calls[-2:] == ["init; reset run; shutdown", "init; halt; shutdown"]
    assert evidence["status"] == "failed"
    assert evidence["reviewed_image_started_only_after_exact_match"] is False
    assert evidence["target_may_have_started_before_failure_halt"] is True
    assert evidence["failure_halt_required"] is True
    assert evidence["target_kept_halted_on_failure"] is True
    assert evidence["failure_halt"]["evidence_kind"] == "selector_target_best_effort_halt_v1"
    assert evidence["failure_halt"]["target_halted"] is True
    assert evidence["mailbox_access_performed"] is False


def test_selector_and_initial_memory_quarantine_reject_symlink_ancestry(
    tmp_path: Path,
) -> None:
    control = _selector_control(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(runner.ArmPreservingRunError, match="symlink"):
        runner._live_target_image_admission(control, linked, {"source": "sealed"})

    contract = _contract(tmp_path)
    contract["storage"]["capture_root"] = str(linked / "captures" / "run")
    with pytest.raises(runner.ArmPreservingRunError, match="symlink"):
        runner._quarantine_blocks(contract, [], OSError("ENODATA"))


def test_quarantines_reject_nonlocal_device_and_post_persistence_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(tmp_path)
    control = _selector_control(tmp_path)

    def reject(_path: Path, *, label: str) -> Path:
        raise runner.FileArtifactAdmissionError(f"{label} is not on local RPi storage device")

    monkeypatch.setattr(runner, "assert_local_rpi_storage", reject)
    with pytest.raises(runner.ArmPreservingRunError, match="local RPi storage device"):
        runner._live_target_image_admission(
            control, tmp_path / "selector-live", {"source": "sealed"}
        )
    with pytest.raises(runner.ArmPreservingRunError, match="local RPi storage device"):
        runner._quarantine_blocks(contract, [], OSError("ENODATA"))

    monkeypatch.undo()
    real = tmp_path / "real-staging"
    real.mkdir()
    linked = tmp_path / "linked-staging"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(runner.ArmPreservingRunError, match="symlink"):
        runner._quarantine_post_persistence_staging(contract, linked)
