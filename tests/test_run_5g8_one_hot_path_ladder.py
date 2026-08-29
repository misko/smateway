from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pluto_plus.direct_radio.usb import MetadataFlags
from pluto_plus.hardware import SampleBlockV2
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_one_hot_path_ladder.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_one_hot_path_ladder_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

SOURCE_COMMIT = "1" * 40
DEPENDENCY_COMMIT = "2" * 40
SERIAL = "serial-a"
URI = "usb:1.2.3"
ANTENNA_CODES = (0, 4, 2, 6, 1, 5, 3, 7)


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
    states = [
        {"name": runner.ALL_OFF_STATE, "gpio_code": 8},
        *[
            {"name": f"ANT{index}", "gpio_code": code}
            for index, code in enumerate(ANTENNA_CODES, start=1)
        ],
    ]
    return {
        "schema": 1,
        "mode": "reviewed_static_selector_mailbox_all_off",
        "bench_manifest": {
            "path": "/synthetic/pluto_bench.manifest.json",
            "file_sha256": "4" * 64,
            "max_lease_ms": 5_000,
            "elf_sha256": "8" * 64,
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
            "all_off_code": 8,
            "contract_sha256": "9" * 64,
        },
        "command": {
            "code": 8,
            "lease_ms": 0,
            "wait_until_applied": True,
            "readback_required": True,
        },
        "one_hot_static_states": states,
        "selected_state_lease_ms": runner.SELECTED_STATE_LEASE_MS,
        "state_hold_contract": {
            "marker_used": False,
            "one_fresh_stream_per_static_state_gain_condition": True,
            "selected_state_readback_before_capture": True,
            "selected_state_readback_after_pluto_mute": True,
            "all_off_readback_after_every_condition": True,
            "gpioa_odr_latch_readback_at_every_boundary": True,
            "physical_rf_state_proven_by_digital_readback": False,
        },
        "bench_profile_binding": {
            "schema": 1,
            "binding_kind": "profile_json_header_provenance_to_bench_elf_schedule",
            "bench_elf": {
                "path": "/synthetic/pluto_bench.elf",
                "file_sha256": "8" * 64,
                "manifest_declared_elf_sha256": "8" * 64,
            },
            "bench_bin": {
                "path": "/synthetic/pluto_bench.bin",
                "file_sha256": "a" * 64,
                "size_bytes": 1_024,
                "flash_base_address": runner.BENCH_FLASH_BASE_ADDRESS,
                "derived_from_elf_with": "arm-none-eabi-objcopy -O binary",
            },
            "reproducible_source_build": {
                "source_repository": "/synthetic/smateway",
                "source_commit": SOURCE_COMMIT,
                "fresh_build_directory_used": True,
                "rebuilt_bin_sha256": "a" * 64,
                "exact_bin_match": True,
                "tracked_bench_protocol_path": "/synthetic/bench_protocol.h",
                "tracked_bench_protocol_sha256": "b" * 64,
                "manifest_protocol_sha256": "b" * 64,
                "verifier_path": "/synthetic/verify_bench_elf.py",
                "verifier_sha256": "c" * 64,
                "verify_bench_elf_passed": True,
                "toolchain": "synthetic arm-none-eabi-gcc",
            },
            "profile_provenance": {
                "path": "/synthetic/provenance.json",
                "file_sha256": "d" * 64,
                "contract_sha256": "9" * 64,
                "profile_file_sha256": "6" * 64,
                "header_file_sha256": "7" * 64,
            },
            "control_schedule": {
                "symbol": "CONTROL_SCHEDULE",
                "address": 0x0800035E,
                "size_bytes": 32,
                "bytes_hex": "000014000400170002001a0006001e00010022000500270003002c0007003200",
                "expected_bytes_hex": (
                    "000014000400170002001a0006001e00"
                    "010022000500270003002c0007003200"
                ),
                "state_names": list(runner.ANTENNA_STATES),
                "gpio_codes": list(ANTENNA_CODES),
                "dwell_ms": [20, 23, 26, 30, 34, 39, 44, 50],
                "extraction": "synthetic",
            },
        },
    }


def _fixture(evidence_sha: str = "e" * 64) -> dict[str, Any]:
    return {
        "shared_hardware": {
            "feed_arm_id": "feed-arm-a",
            "feed_cable_id": "feed-cable-a",
            "termination_load_set_id": "loads-a",
            "rx1_reference_plane_id": "rx1-plane-a",
            "rx2_reference_plane_id": "rx2-plane-a",
        },
        "setup_evidence": {
            "path": "/synthetic/setup-evidence.json",
            "file_sha256": evidence_sha,
        },
        "attribution_repeats_without_cable_movement_required": True,
    }


def _contract(driven_input: str = "ANT3") -> dict[str, Any]:
    return runner._build_plan_contract(
        run_id=f"run-{driven_input.lower()}",
        board_id="board-a",
        serial=SERIAL,
        uri=URI,
        driven_input=driven_input,
        source_commit=SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=_dependency_attestation(),
        selector_control=_selector_control(),
        fixture_identity=_fixture(),
    )


def _plan_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "plan_contract_sha256": "a" * 64,
        "plan_contract_hash_provenance": "synthetic canonical JSON",
        "plan_file_sha256": "b" * 64,
        "plan_file_hash_provenance": "synthetic file bytes",
    }


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


def _selector_boundary(
    calls: list[tuple[str, str]] | None = None,
    *,
    fail_purpose: str | None = None,
    expire_after_capture: bool = False,
    freeze_countdown: bool = False,
) -> Any:
    def selector(
        control: dict[str, Any],
        state_name: str,
        state_code: int,
        purpose: str,
    ) -> dict[str, Any]:
        del control
        if calls is not None:
            calls.append((state_name, purpose))
        cleanup = purpose in {
            "cleanup_all_off",
            "final_cleanup_all_off",
            "resume_cleanup_all_off",
        }
        expected_name = runner.ALL_OFF_STATE if cleanup else state_name
        expected_code = 8 if cleanup else state_code
        selected = expected_name != runner.ALL_OFF_STATE
        expired = expire_after_capture and purpose == "after_pluto_mute"
        passed = purpose != fail_purpose and not expired
        lease_ms = runner.SELECTED_STATE_LEASE_MS if selected else 0
        mailbox = {
            "command_sequence": 12,
            "command_code": expected_code,
            "command_lease_ms": lease_ms,
            "acknowledged_sequence": 12,
            "applied_code": expected_code,
            "remaining_lease_ms": (
                (
                    3_500
                    if freeze_countdown or purpose != "after_pluto_mute"
                    else 3_100
                )
                if selected and not expired
                else 0
            ),
            "status_flags": 3 if selected and not expired else 1,
            "command_valid": True,
            "lease_active": selected and not expired,
            "guard_active": False,
            "invalid_command": False,
        }
        return {
            "schema": 1,
            "evidence_kind": "static_one_hot_selector_mailbox_readback",
            "purpose": purpose,
            "status": "passed" if passed else "failed",
            "condition_state_name": state_name,
            "condition_state_code": state_code,
            "expected_applied_state_name": expected_name,
            "expected_applied_code": expected_code,
            "command_lease_ms": lease_ms,
            "commanded": None if purpose == "after_pluto_mute" else dict(mailbox),
            "readback": mailbox,
            "gpio_output_latch_readback": {
                "register": "GPIOA_ODR",
                "address": runner.GPIOA_ODR_ADDRESS,
                "selector_mask": runner.SELECTOR_GPIO_MASK,
                "raw_value": expected_code,
                "masked_selector_code": expected_code,
                "expected_selector_code": expected_code,
                "passed": True,
                "physical_rf_state_proven": False,
            },
            "error": None,
        }

    return selector


def test_frozen_selected_state_lease_countdown_is_rejected() -> None:
    boundary = _selector_boundary(freeze_countdown=True)
    before = boundary(_selector_control(), "ANT3", 2, "before_condition")
    after = boundary(_selector_control(), "ANT3", 2, "after_pluto_mute")

    assert not runner._selector_hold_command_unchanged(before, after)


def _passing_target_image() -> Any:
    def target(control: dict[str, Any]) -> dict[str, Any]:
        bench_bin = control["bench_profile_binding"]["bench_bin"]
        return {
            "schema": 1,
            "evidence_kind": (
                "exact_target_flash_readback_against_elf_bound_bench_bin"
            ),
            "status": "passed",
            "flash_base_address": bench_bin["flash_base_address"],
            "byte_count": bench_bin["size_bytes"],
            "expected_bin_sha256": bench_bin["file_sha256"],
            "observed_target_sha256": bench_bin["file_sha256"],
            "exact_byte_match": True,
            "reviewed_image_started_only_after_exact_match": True,
            "target_kept_halted_on_failure": False,
            "error": None,
        }

    return target


def test_target_image_mismatch_never_releases_unknown_flash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"reviewed-bench-image"
    expected_path = tmp_path / "pluto_bench.bin"
    expected_path.write_bytes(expected)
    config_path = tmp_path / "openocd.cfg"
    config_path.write_text("# synthetic\n", encoding="utf-8")
    control = _selector_control()
    digest = hashlib.sha256(expected).hexdigest()
    control["openocd_config"]["path"] = str(config_path)
    control["bench_profile_binding"]["bench_bin"].update(
        {
            "path": str(expected_path),
            "file_sha256": digest,
            "size_bytes": len(expected),
        }
    )
    control["bench_profile_binding"]["reproducible_source_build"][
        "rebuilt_bin_sha256"
    ] = digest
    commands: list[str] = []

    def openocd(command: Any, **_kwargs: Any) -> Any:
        text = str(command[-1])
        commands.append(text)
        if "dump_image" in text:
            target = Path(text.split("dump_image ", 1)[1].split(" ", 1)[0])
            target.write_bytes(b"X" * len(expected))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_verify_one_hot_artifacts", lambda _control: None)
    monkeypatch.setattr(runner.subprocess, "run", openocd)

    result = runner._live_target_image_attestation(control)

    assert result["status"] == "failed"
    assert result["exact_byte_match"] is False
    assert result["target_kept_halted_on_failure"] is True
    assert not any("reset run" in command for command in commands)
    assert commands[-1] == "init; halt; shutdown"


def _settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=runner.leakage.CENTER_FREQUENCY_HZ,
        sample_rate_hz=runner.leakage.SAMPLE_RATE_HZ,
        bandwidth_hz=runner.leakage.BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=runner.leakage.RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _blocks(
    *,
    stream_id: int = 12345,
    tone_offset_hz: float = 100_037.0,
) -> list[SampleBlockV2]:
    flags = int(MetadataFlags.SAMPLE_SEQUENCE_VALID | MetadataFlags.HARDWARE_SAMPLE_COUNTER_VALID)
    rng = np.random.default_rng(20260829)
    blocks = []
    first_sample_sequence = 8_000_000
    realtime_base = 1_800_000_000_000_000_000
    monotonic_base = 2_000_000_000_000
    duration_ns = round(runner.leakage.SAMPLES_PER_FRAME / runner.leakage.SAMPLE_RATE_HZ * 1e9)
    for index in range(runner.leakage.FRAME_COUNT):
        start = index * runner.leakage.SAMPLES_PER_FRAME
        indices = np.arange(
            start,
            start + runner.leakage.SAMPLES_PER_FRAME,
            dtype=np.float64,
        )
        carrier = np.exp(2j * np.pi * tone_offset_hz * indices / runner.leakage.SAMPLE_RATE_HZ)
        noise1 = 2.0 * (
            rng.standard_normal(runner.leakage.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.leakage.SAMPLES_PER_FRAME)
        )
        noise2 = 2.0 * (
            rng.standard_normal(runner.leakage.SAMPLES_PER_FRAME)
            + 1j * rng.standard_normal(runner.leakage.SAMPLES_PER_FRAME)
        )
        samples = np.asarray(
            [400.0 * np.exp(0.4j) * carrier + noise1, 40.0 * np.exp(-0.8j) * carrier + noise2],
            dtype=np.complex64,
        )
        realtime_start = realtime_base + index * duration_ns
        monotonic_start = monotonic_base + index * duration_ns
        blocks.append(
            SampleBlockV2(
                utc_ns=realtime_start + duration_ns // 2,
                samples=samples,
                stream_id=stream_id,
                buffer_sequence=index,
                first_sample_sequence=(
                    first_sample_sequence + index * runner.leakage.SAMPLES_PER_FRAME
                ),
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
        scales[0] = runner.leakage.DDS_SCALE
        scales[2] = runner.leakage.DDS_SCALE
        enabled = [False] * 8
        enabled[0] = True
        enabled[2] = True
        frequencies = [0] * 8
        frequencies[0] = runner.leakage.TONE_OFFSET_HZ
        frequencies[2] = -runner.leakage.TONE_OFFSET_HZ
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


def test_plan_is_one_physical_row_with_three_independent_attribution_repeats() -> None:
    contract = _contract("ANT3")
    conditions = contract["conditions"]

    assert contract["plan_kind"] == "5g8_marker_independent_one_hot_selector_path_ladder"
    assert contract["topology_identity"] == runner.TOPOLOGY_IDENTITY
    assert contract["driven_input"] == "ANT3"
    assert contract["stage_contract"]["driven_board_input"] == "ANT3"
    assert contract["stage_contract"]["terminated_board_inputs"] == [
        "ANT1",
        "ANT2",
        "ANT4",
        "ANT5",
        "ANT6",
        "ANT7",
        "ANT8",
    ]
    assert contract["stage_contract"]["simultaneous_eight_way_feed_present"] is False
    assert contract["fixture_identity"] == _fixture()
    assert contract["interpretation"]["one_run_represents_exactly_one_driven_input"] is True
    assert contract["interpretation"]["cross_gain_observations_are_not_repeatability_claims"]
    assert contract["safety"][
        "selector_mailbox_and_gpio_latch_readback_before_every_capture"
    ]
    assert len(conditions) == 72
    assert [item["selector_state_name"] for item in conditions[:9]] == list(
        runner.ONE_HOT_STATE_ORDER
    )
    assert [item["selector_gpio_code"] for item in conditions[:9]] == [8, *ANTENNA_CODES]
    attribution = [
        item
        for item in conditions
        if item["tx_hardware_gain_db"] == runner.ATTRIBUTION_TX_HARDWARE_GAIN_DB
    ]
    assert len(attribution) == 27
    assert {item["repeat_index"] for item in attribution} == {0, 1, 2}
    assert all(item["repeat_count_at_gain"] == 3 for item in attribution)
    assert all(item["driven_input"] == "ANT3" for item in conditions)
    assert all(item["fixture_identity"] == _fixture() for item in conditions)
    matrix_identity = runner._matrix_identity_from_contract(contract)
    assert matrix_identity["board_id"] == "board-a"
    assert matrix_identity["pluto_serial"] == SERIAL
    assert matrix_identity["bench_bin_sha256"] == "a" * 64
    moved_usb_uri = json.loads(json.dumps(contract))
    moved_usb_uri["configuration"]["uri"] = "usb:9.8.7"
    assert runner._matrix_identity_from_contract(moved_usb_uri) == matrix_identity
    serialized = json.dumps(contract)
    assert "full_conducted_fixture" not in serialized
    assert "powered_selector_all_inputs_terminated" not in serialized


def test_plan_rejects_noncanonical_state_map() -> None:
    control = _selector_control()
    control["one_hot_static_states"][1], control["one_hot_static_states"][2] = (
        control["one_hot_static_states"][2],
        control["one_hot_static_states"][1],
    )
    with pytest.raises(ValueError, match="sequential order"):
        runner._build_plan_contract(
            run_id="run-bad-map",
            board_id="board-a",
            serial=SERIAL,
            uri=URI,
            driven_input="ANT3",
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=_dependency_attestation(),
            selector_control=control,
            fixture_identity=_fixture(),
        )


def test_plan_only_preparation_is_rf_and_selector_control_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        runner.leakage,
        "_live_capture_boundary",
        lambda *args, **kwargs: calls.append("capture"),
    )
    monkeypatch.setattr(
        runner,
        "_live_selector_boundary",
        lambda *args, **kwargs: calls.append("selector"),
    )
    monkeypatch.setattr(
        runner.leakage,
        "_strict_mute",
        lambda *args, **kwargs: calls.append("mute"),
    )
    contract = _contract()
    plan_path = tmp_path / "run" / runner.PLAN_FILENAME

    envelope = runner.leakage._prepare_plan(plan_path, contract)
    manifest = runner._new_manifest(plan_path, envelope)
    runner._persist_manifest(
        tmp_path / "run" / runner.MANIFEST_FILENAME,
        manifest,
        condition_count=len(contract["conditions"]),
    )

    assert calls == []
    assert envelope["plan_contract_sha256"] == runner.leakage.canonical_json_sha256(contract)
    assert manifest["status"] == "prepared"
    assert manifest["immutable_plan"]["plan_file_sha256"] == runner.sha256_path(plan_path)


def test_full_main_plan_only_never_touches_hardware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "ant3-setup-evidence.json"
    evidence.write_text('{"driven_input":"ANT3"}\n', encoding="utf-8")
    openocd_config = tmp_path / "rpi4-swd.cfg"
    openocd_config.write_text("# synthetic plan-only config\n", encoding="utf-8")
    hardware_calls: list[str] = []

    def forbidden(name: str) -> Any:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            hardware_calls.append(name)
            raise AssertionError(f"plan-only touched {name}")

        return fail

    original_run = runner.subprocess.run

    def guarded_run(command: Any, *args: Any, **kwargs: Any) -> Any:
        executable = str(command[0]) if isinstance(command, (list, tuple)) else str(command)
        if executable == "openocd":
            return forbidden("openocd")(command, *args, **kwargs)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(
        runner.leakage,
        "_repository_commit_and_require_clean",
        lambda *_args: SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        runner,
        "attest_pluto_plus_utils_source",
        _dependency_attestation,
    )
    monkeypatch.setattr(runner.leakage, "_board_root", lambda _board: tmp_path / "board")
    monkeypatch.setattr(runner.leakage, "_board_lock", lambda _root: nullcontext())
    monkeypatch.setattr(runner.leakage, "_live_capture_boundary", forbidden("capture"))
    monkeypatch.setattr(runner.leakage, "_strict_mute", forbidden("mute"))
    monkeypatch.setattr(runner.leakage, "_live_identity_boundary", forbidden("identity"))
    monkeypatch.setattr(runner, "_live_selector_boundary", forbidden("selector"))
    monkeypatch.setattr(runner, "_live_target_image_attestation", forbidden("target-image"))
    monkeypatch.setattr(runner.subprocess, "run", guarded_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--run-id",
            "plan-only-ant3",
            "--board-id",
            "board-a",
            "--serial",
            SERIAL,
            "--uri",
            URI,
            "--driven-input",
            "ANT3",
            "--plan-only",
            "--bench-manifest",
            str(
                SCRIPT.parents[1]
                / "build/STM32C011F4P6/bench/pluto_bench.manifest.json"
            ),
            "--openocd-config",
            str(openocd_config),
            "--profile",
            str(SCRIPT.parents[1] / "profiles/fast20-v1/control_profile.json"),
            "--feed-arm-id",
            "feed-arm-a",
            "--feed-cable-id",
            "feed-cable-a",
            "--termination-load-set-id",
            "loads-a",
            "--rx1-reference-plane-id",
            "rx1-plane-a",
            "--rx2-reference-plane-id",
            "rx2-plane-a",
            "--setup-evidence-file",
            str(evidence),
        ],
    )

    assert runner.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rf_activity"] is False
    assert output["driven_input"] == "ANT3"
    assert output["condition_count"] == 72
    assert hardware_calls == []


def test_execution_confirmations_require_exact_input_token_fixture_and_no_movement() -> None:
    token = runner.physical_confirmation_token("ANT3")
    confirmation = runner._validate_confirmations(
        driven_input="ANT3",
        fixture_identity=_fixture(),
        topology_token=token,
        no_antennas=True,
        tx1_matched=True,
        tx2_terminated_muted=True,
        rx1_conducted_reference=True,
        one_hot_static_control=True,
        single_driven_input=True,
        other_seven_terminated=True,
        no_simultaneous_eight_way_feed=True,
        attribution_repeats_no_cable_movement=True,
    )
    assert confirmation["reviewed_static_one_hot_mailbox_control"] is True
    assert confirmation["fixture_identity"] == _fixture()

    with pytest.raises(runner.OneHotLadderError, match="no-cable-movement"):
        runner._validate_confirmations(
            driven_input="ANT3",
            fixture_identity=_fixture(),
            topology_token=token,
            no_antennas=True,
            tx1_matched=True,
            tx2_terminated_muted=True,
            rx1_conducted_reference=True,
            one_hot_static_control=True,
            single_driven_input=True,
            other_seven_terminated=True,
            no_simultaneous_eight_way_feed=True,
            attribution_repeats_no_cable_movement=False,
        )


def test_condition_capture_attests_selected_hold_and_all_off_cleanup(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = next(
        item
        for item in contract["conditions"]
        if item["selector_state_name"] == "ANT3" and item["tx_hardware_gain_db"] == -35.0
    )
    selector_calls: list[tuple[str, str]] = []
    mute_calls: list[str] = []
    capture_calls: list[dict[str, Any]] = []
    plan_evidence = _plan_evidence(tmp_path / "plan.json")

    result = runner._capture_condition(
        condition,
        contract=contract,
        plan_evidence=plan_evidence,
        capture_root=tmp_path / "captures",
        forbidden_stream_ids=set(),
        capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
        mute_boundary=_passing_mute(mute_calls),
        selector_boundary=_selector_boundary(selector_calls),
    )

    assert selector_calls == [
        ("ANT3", "before_condition"),
        ("ANT3", "after_pluto_mute"),
        ("ANT3", "cleanup_all_off"),
    ]
    assert mute_calls == ["post_condition"]
    assert len(capture_calls) == 1
    assert result["selector_state_name"] == "ANT3"
    assert result["selector_gpio_code"] == 2
    assert result["measurement_quality_passed"] is True
    assert result["tone_offset_hz_requested"] == 100_000
    assert result["tone_offset_hz_readback"] == 100_000
    assert result["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert result["pilot_confidence"] >= runner.leakage.MINIMUM_PILOT_CONFIDENCE
    assert result["selector_after_pluto_mute"]["readback"]["lease_active"] is True
    assert result["selector_cleanup_all_off"]["readback"]["applied_code"] == 8

    record = json.loads((Path(result["artifact_path"]) / runner.CONDITION_RECORD_NAME).read_text())
    assert record["condition"]["selector_state_name"] == "ANT3"
    assert record["capture"]["tone_offset_hz_measured"] == pytest.approx(100_037.0, abs=0.2)
    assert record["selector_state_attestation"]["after_pluto_mute"]["status"] == "passed"
    assert record["selector_state_attestation"]["cleanup_all_off"]["status"] == "passed"
    assert record["accepted_for_selector_calibration"] is False
    resume_manifest = {
        "attempts": [
            {
                "attempt_id": 1,
                "condition_id": condition["condition_id"],
                "condition": dict(condition),
                "status": "complete",
                "outcome": "measurement_quality_passed",
                "failure_kind": None,
                "quarantine": None,
                "post_condition_exact_serial_mute": result[
                    "post_condition_exact_serial_mute"
                ],
                "error": None,
                "automatic_retry_attempted": False,
                "result": result,
            }
        ]
    }
    assert runner._completed_condition_ids(
        resume_manifest,
        planned_conditions={str(condition["condition_id"]): condition},
        selector_control=contract["selector_control"],
        configuration=contract["configuration"],
        serial=SERIAL,
        plan_evidence=plan_evidence,
        capture_root=tmp_path / "captures",
    ) == {condition["condition_id"]}

    external_root = tmp_path / "external-root"
    external_root.mkdir()
    with pytest.raises(runner.OneHotLadderError, match="artifact layout is invalid"):
        runner._completed_condition_ids(
            resume_manifest,
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=external_root,
            downgrade_invalid=False,
        )

    result["rx2_over_rx1"]["phasor"]["real"] += 0.5
    with pytest.raises(runner.OneHotLadderError, match="condition record is inconsistent"):
        runner._completed_condition_ids(
            resume_manifest,
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=tmp_path / "captures",
        )
    assert resume_manifest["attempts"][0]["status"] == "failed"
    assert resume_manifest["attempts"][0]["quarantine"] is not None
    assert not Path(result["artifact_path"]).exists()


def test_resume_revalidates_claim_safety_rf_and_attempt_state(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = next(
        item
        for item in contract["conditions"]
        if item["selector_state_name"] == "ANT3"
        and item["tx_hardware_gain_db"] == -35.0
    )
    plan_evidence = _plan_evidence(tmp_path / "plan.json")
    capture_root = tmp_path / "captures"
    result = runner._capture_condition(
        condition,
        contract=contract,
        plan_evidence=plan_evidence,
        capture_root=capture_root,
        forbidden_stream_ids=set(),
        capture_boundary=_capture_boundary(_blocks(stream_id=54321)),
        mute_boundary=_passing_mute(),
        selector_boundary=_selector_boundary(),
    )
    record_path = Path(result["condition_record_path"])
    original_record = json.loads(record_path.read_text(encoding="utf-8"))

    def attempt() -> dict[str, Any]:
        return {
            "attempt_id": 1,
            "condition_id": condition["condition_id"],
            "condition": dict(condition),
            "status": "complete",
            "outcome": "measurement_quality_passed",
            "failure_kind": None,
            "quarantine": None,
            "post_condition_exact_serial_mute": result[
                "post_condition_exact_serial_mute"
            ],
            "error": None,
            "automatic_retry_attempted": False,
            "result": result,
        }

    def persist_record(record: dict[str, Any]) -> None:
        runner.write_json_atomic(record_path, record)
        result["condition_record_sha256"] = runner.sha256_path(record_path)

    weakened_safety = json.loads(json.dumps(original_record))
    weakened_safety["safety"]["fresh_stream_validated"] = False
    persist_record(weakened_safety)
    with pytest.raises(runner.OneHotLadderError, match="condition record is inconsistent"):
        runner._completed_condition_ids(
            {"attempts": [attempt()]},
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
            downgrade_invalid=False,
        )

    wrong_tx2_readback = json.loads(json.dumps(original_record))
    wrong_tx2_readback["capture"]["rf_readback_evidence"][
        "tx_hardware_gain_readback_db_by_channel"
    ][1] = -60.0
    persist_record(wrong_tx2_readback)
    with pytest.raises(runner.OneHotLadderError, match="live RF readback is invalid"):
        runner._completed_condition_ids(
            {"attempts": [attempt()]},
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
            downgrade_invalid=False,
        )

    persist_record(original_record)
    result["causal_attribution_claim"] = True
    with pytest.raises(runner.OneHotLadderError, match="condition record is inconsistent"):
        runner._completed_condition_ids(
            {"attempts": [attempt()]},
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
            downgrade_invalid=False,
        )
    result["causal_attribution_claim"] = False

    with pytest.raises(runner.OneHotLadderError, match="non-complete or unknown-status"):
        runner._completed_condition_ids(
            {
                "attempts": [
                    attempt(),
                    {"condition_id": condition["condition_id"], "status": "failed"},
                ]
            },
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=contract["selector_control"],
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
            downgrade_invalid=False,
        )


def test_selector_precondition_failure_blocks_capture_and_is_quarantined(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = contract["conditions"][1]
    capture_calls: list[dict[str, Any]] = []
    selector_calls: list[tuple[str, str]] = []

    with pytest.raises(runner.leakage.ConditionCaptureFailure, match="pre-condition") as caught:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(),
            selector_boundary=_selector_boundary(
                selector_calls,
                fail_purpose="before_condition",
            ),
        )

    assert capture_calls == []
    assert selector_calls == [
        ("ANT1", "before_condition"),
        ("ANT1", "after_pluto_mute"),
        ("ANT1", "cleanup_all_off"),
    ]
    assert caught.value.quarantine["accepted"] is False


def test_selected_state_lease_expiry_after_capture_quarantines_retained_iq(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = contract["conditions"][1]

    with pytest.raises(runner.leakage.ConditionCaptureFailure, match="remain applied") as caught:
        runner._capture_condition(
            condition,
            contract=contract,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
            forbidden_stream_ids=set(),
            capture_boundary=_capture_boundary(_blocks(stream_id=23456)),
            mute_boundary=_passing_mute(),
            selector_boundary=_selector_boundary(expire_after_capture=True),
        )

    assert caught.value.quarantine["accepted"] is False
    assert any(item["name"].endswith(".sigmf-data") for item in caught.value.quarantine["files"])


def test_stale_usb_uri_blocks_mute_selector_and_capture_boundaries(tmp_path: Path) -> None:
    contract = _contract()
    envelope = runner.leakage._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner.leakage._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []
    selector_calls: list[tuple[str, str]] = []
    capture_calls: list[dict[str, Any]] = []

    def mismatch(serial: str, requested_uri: str) -> dict[str, Any]:
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

    with pytest.raises(runner.OneHotLadderError, match="read-only USB identity"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=mismatch,
            selector_boundary=_selector_boundary(selector_calls),
            target_image_boundary=_passing_target_image(),
            fixture_evidence_boundary=lambda _fixture: None,
        )

    assert capture_calls == []
    assert mute_calls == ["identity_preflight_recovery"]
    assert selector_calls == [
        (runner.ALL_OFF_STATE, "identity_failure_cleanup_all_off")
    ]
    assert manifest["status"] == "failed"


def test_preflight_mute_failure_never_captures_and_stage_finally_forces_all_off(
    tmp_path: Path,
) -> None:
    contract = _contract()
    envelope = runner.leakage._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner.leakage._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest_path = tmp_path / "manifest.json"
    capture_calls: list[dict[str, Any]] = []
    mute_calls: list[str] = []
    selector_calls: list[tuple[str, str]] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        result = _passing_mute(mute_calls)(serial, purpose)
        if purpose == "preflight":
            result["status"] = "failed"
            result["error"] = {"type": "SyntheticMuteFailure", "message": "failed"}
        return result

    with pytest.raises(runner.OneHotLadderError, match="preflight mute"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks(), calls=capture_calls),
            mute_boundary=mute,
            identity_boundary=_passing_identity(),
            selector_boundary=_selector_boundary(selector_calls),
            target_image_boundary=_passing_target_image(),
            fixture_evidence_boundary=lambda _fixture: None,
        )

    assert capture_calls == []
    assert mute_calls == ["preflight", "final"]
    assert selector_calls == [(runner.ALL_OFF_STATE, "final_cleanup_all_off")]
    assert manifest["final_selector_cleanup"]["status"] == "passed"
    assert manifest["status"] == "failed"


def test_failed_invocation_attempts_pluto_mute_and_selector_all_off_recovery(
    tmp_path: Path,
) -> None:
    contract = _contract()
    envelope = runner.leakage._plan_envelope(contract)
    plan_path = tmp_path / "plan.json"
    runner.leakage._write_immutable_json(plan_path, envelope)
    manifest = runner._new_manifest(plan_path, envelope)
    manifest["status"] = "failed"
    manifest_path = tmp_path / "manifest.json"
    mute_calls: list[str] = []
    selector_calls: list[tuple[str, str]] = []

    with pytest.raises(runner.OneHotLadderError, match="cannot retry"):
        runner._execute_stage(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation={"stage": contract["topology_stage"]},
            capture_root=tmp_path / "captures",
            capture_boundary=_capture_boundary(_blocks()),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            selector_boundary=_selector_boundary(selector_calls),
            target_image_boundary=_passing_target_image(),
            fixture_evidence_boundary=lambda _fixture: None,
        )

    assert mute_calls == ["resume_recovery"]
    assert selector_calls == [(runner.ALL_OFF_STATE, "resume_cleanup_all_off")]
    assert manifest["recovery_selector_cleanup_attempts"][0]["status"] == "passed"


def test_resume_rejects_completed_attempt_without_exact_selector_attestations(
    tmp_path: Path,
) -> None:
    contract = _contract()
    condition = contract["conditions"][0]
    manifest = {
        "attempts": [
            {
                "condition_id": condition["condition_id"],
                "status": "complete",
                "result": {
                    "condition_id": condition["condition_id"],
                    "selector_state_name": condition["selector_state_name"],
                    "selector_gpio_code": condition["selector_gpio_code"],
                    "tx_hardware_gain_db": condition["tx_hardware_gain_db"],
                    "metadata_abi": 2,
                    "stream_id": 1,
                    "post_condition_exact_serial_mute": _passing_mute()(SERIAL, "post_condition"),
                    "selector_before_condition": None,
                    "selector_after_pluto_mute": None,
                    "selector_cleanup_all_off": None,
                },
            }
        ]
    }

    with pytest.raises(runner.OneHotLadderError, match="evidence is malformed"):
        runner._completed_condition_ids(
            manifest,
            planned_conditions={str(condition["condition_id"]): condition},
            selector_control=_selector_control(),
            configuration=contract["configuration"],
            serial=SERIAL,
            plan_evidence=_plan_evidence(tmp_path / "plan.json"),
            capture_root=tmp_path / "captures",
        )
    assert manifest["attempts"][0]["status"] == "failed"


def test_run_specific_orphan_scan_quarantines_truncated_and_partial_artifacts(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "one-hot-run"
    malformed = capture_root / "artifact-malformed"
    malformed.mkdir(parents=True)
    (malformed / runner.CONDITION_RECORD_NAME).write_text("{truncated", encoding="utf-8")
    partial = capture_root / ".partial" / "artifact-partial"
    partial.mkdir(parents=True)
    (partial / "partial.sigmf-data").write_bytes(b"partial")

    quarantines = runner._quarantine_orphaned_current_plan_artifacts(
        capture_root,
        manifest={"attempts": []},
        plan_evidence=_plan_evidence(tmp_path / "plan.json"),
    )

    assert len(quarantines) == 2
    assert not malformed.exists()
    assert not partial.exists()
    assert (capture_root / ".failed/artifact-malformed.orphaned").is_dir()
    assert (capture_root / ".failed/artifact-partial.orphaned").is_dir()
