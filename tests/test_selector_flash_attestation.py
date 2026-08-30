from __future__ import annotations

import hashlib
import json
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from smateway.selector_flash_attestation import (
    EVIDENCE_KIND,
    FAILURE_FILENAME,
    FINAL_DIGEST_FILENAME,
    FINAL_EVIDENCE_FILENAME,
    PHASE1_FILENAME,
    PLUTO_MUTE_EVIDENCE_KIND,
    POWER_CYCLE_ATTESTATION_KIND,
    POWER_CYCLE_SEALED_FILENAME,
    POWER_CYCLE_TEMPLATE_FILENAME,
    PRE_PROGRAM_ATTESTATION_KIND,
    CommandResult,
    SelectorFlashError,
    prepare_and_program,
    seal_power_cycle_attestation,
    validate_sealed_selector_evidence,
    verify_after_power_cycle,
    write_pre_program_attestation_template,
)

BOARD_ID = "stm32c011-4c0055000950313950363920"
UID = bytes.fromhex("4c0055000950313950363920")
CAMPAIGN_ID = "5p8-debug-r1"
RUN_ID = "5p8-debug-r1-flash-r01"
COMMIT = "a" * 40
PLUTO_SERIAL = "104000b29905000e17000800065934759d"
PLUTO_URI = "usb:1.2.3"
MAILBOX_ADDRESS = 0x20000004
GPIO_ADDRESS = 0x50000014
FLASH_ADDRESS = 0x08000000
DUMP_PATTERN = re.compile(r"dump_image \{([^}]+)\} (0x[0-9a-f]+) (0x[0-9a-f]+)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _profile() -> dict[str, Any]:
    return {
        "schema": 1,
        "contract_sha256": "b" * 64,
        "profile": {"id": "fast20-v1", "revision": 1},
        "clock": {"decoder_window_pct": 5},
        "frame": {
            "all_off_guard_ms": 5,
            "nominal_cycle_ms": 105,
            "recommended_capture_ms": 200,
            "minimum_capture_for_guaranteed_complete_frame_ms": 200,
            "marker": {"body_nominal_ms": 80, "decoder_min_ms": 76},
        },
        "states": [
            {
                "name": "ANT1",
                "gpio_code_pa3_pa0": "0000",
                "dwell_ms": 20,
                "window_ms": [19, 21],
            }
        ],
    }


def _repository(tmp_path: Path, role: str) -> dict[str, Path]:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "openocd").mkdir()
    (repository / "profiles/fast20-v1").mkdir(parents=True)
    (repository / "firmware/stm32c011/apps/bench").mkdir(parents=True)
    (repository / "Makefile").write_text("bench:\nfast20:\n", encoding="utf-8")
    (repository / "scripts/verify_bench_elf.py").write_text("# verifier\n", encoding="utf-8")
    (repository / "scripts/verify_fast20_elf.py").write_text("# verifier\n", encoding="utf-8")
    (repository / "openocd/rpi4-swd.cfg").write_text("transport select swd\n", encoding="utf-8")
    profile_path = repository / "profiles/fast20-v1/control_profile.json"
    _write_json(profile_path, _profile())
    (repository / "profiles/fast20-v1/control_profile.h").write_text(
        "#define CONTROL_ALL_OFF_CODE 0x8u\n", encoding="utf-8"
    )
    protocol_path = repository / "firmware/stm32c011/apps/bench/bench_protocol.h"
    protocol_path.write_text("#define BENCH_PROTOCOL 1\n", encoding="utf-8")

    build = repository / "build/STM32C011F4P6" / role
    build.mkdir(parents=True)
    stem = "pluto_bench" if role == "bench" else "pluto_fast20"
    elf = build / f"{stem}.elf"
    firmware_bin = build / f"{stem}.bin"
    elf.write_bytes(b"ELF-selector-image-v1")
    firmware_bin.write_bytes(b"BIN-selector-image-v1")
    manifest = build / "pluto_bench.manifest.json"
    if role == "bench":
        _write_json(
            manifest,
            {
                "schema": 1,
                "elf_sha256": _sha256(elf),
                "protocol_sha256": _sha256(protocol_path),
                "mailbox": {
                    "address": MAILBOX_ADDRESS,
                    "size": 36,
                    "magic": 0x534D4757,
                    "version": 1,
                    "max_lease_ms": 5000,
                    "offsets": {
                        "magic": 0,
                        "version": 4,
                        "command_sequence": 8,
                        "command_code": 12,
                        "command_lease_ms": 16,
                        "acknowledged_sequence": 20,
                        "applied_code": 24,
                        "remaining_lease_ms": 28,
                        "status_flags": 32,
                    },
                },
            },
        )
    return {
        "repository": repository,
        "elf": elf,
        "firmware_bin": firmware_bin,
        "build_manifest": manifest,
        "profile": profile_path,
        "openocd_config": repository / "openocd/rpi4-swd.cfg",
    }


def _operator_attestation(
    kind: str,
    *,
    role: str = "bench",
    phase1_path: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": 1,
        "evidence_kind": kind,
        "campaign_id": CAMPAIGN_ID,
        "run_id": RUN_ID,
        "board_id": BOARD_ID,
        "image_role": role,
        "operator_id": "operator-1",
        "observed_at": "2026-08-29T12:00:00+00:00",
        "power_source": "J12_bench_5V",
        "supply_output_id": "bench-output-1",
        "positive_lead_id": "j12-positive-lead",
        "power_ground_id": "j12-ground-lead",
        "control_ground_id": "pi-target-common-ground",
        "supply_set_voltage_v": 5.0,
        "supply_current_limit_a": 0.5,
        "supply_displayed_current_a": 0.08,
        "j11_pin1_voltage_v": 3.3,
        "j1_usb_c_disconnected": True,
        "pi_power_rails_disconnected": True,
        "j11_pin1_vtref_only": True,
        "common_ground_confirmed": True,
        "nrst_continuity_confirmed": True,
        "swd_wiring_confirmed": True,
        "openocd_stopped_confirmed": True,
        "target_powered": True,
        "unexpected_heat_observed": False,
    }
    if kind == POWER_CYCLE_ATTESTATION_KIND:
        assert phase1_path is not None
        result["power_removed_duration_s"] = 5.2
        result["no_rf_or_wiring_connection_changed"] = True
        result["phase1_path"] = str(phase1_path)
        result["phase1_sha256"] = _sha256(phase1_path)
    return result


def _power_attestation(run_directory: Path, *, role: str = "bench") -> dict[str, Any]:
    return _operator_attestation(
        POWER_CYCLE_ATTESTATION_KIND,
        role=role,
        phase1_path=run_directory / PHASE1_FILENAME,
    )


def _seal_power_attestation(
    run_directory: Path,
    evidence_root: Path,
    *,
    role: str = "bench",
    document: dict[str, Any] | None = None,
) -> Path:
    draft = run_directory / POWER_CYCLE_TEMPLATE_FILENAME
    _write_json(draft, document or _power_attestation(run_directory, role=role))
    return seal_power_cycle_attestation(
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        board_id=BOARD_ID,
        image_role=role,
        evidence_root=evidence_root,
        power_cycle_draft=draft,
    )


def _publish_forged_sealed_power(run_directory: Path, document: dict[str, Any]) -> Path:
    path = run_directory / POWER_CYCLE_SEALED_FILENAME
    _write_json(path, document)
    path.chmod(0o400)
    return path


class FakeBoundary:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self.commands: list[tuple[str, ...]] = []
        self.dirty = False
        self.fail_build = False
        self.fail_verify = False
        self.fail_program = False
        self.fail_reset_after_program = False
        self.fail_phase2_readback = False
        self.fail_bench_probe = False
        self.bad_uid = False
        self.bad_readback = False
        self.bad_mailbox = False
        self.bad_gpio = False
        self.phase2_started = False
        self.all_off_commanded = False

    def _mailbox(self, *, commanded: bool) -> bytes:
        sequence = 1 if commanded else 0
        applied = 7 if self.bad_mailbox else 8
        flags = 1 if commanded else 0
        return struct.pack(
            "<9I",
            0x534D4757,
            1,
            sequence,
            8,
            0,
            sequence,
            applied,
            0,
            flags,
        )

    def __call__(self, argv: tuple[str, ...], *, cwd: Path) -> CommandResult:
        del cwd
        self.commands.append(argv)
        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, COMMIT + "\n", "")
        if argv[:3] == ("git", "status", "--porcelain"):
            return CommandResult(0, " M dirty.py\n" if self.dirty else "", "")
        if argv[:2] == ("make", "bench") or argv[:2] == ("make", "fast20"):
            return CommandResult(2 if self.fail_build else 0, "build\n", "")
        if len(argv) >= 2 and "verify_" in argv[1]:
            return CommandResult(2 if self.fail_verify else 0, "verify\n", "")
        if argv and argv[0] == "arm-none-eabi-objcopy":
            Path(argv[-1]).write_bytes(self.paths["firmware_bin"].read_bytes())
            return CommandResult(0, "objcopy\n", "")
        if not argv or argv[0] != "openocd":
            return CommandResult(0, "", "")
        command = argv[-1]
        if "program " in command:
            return CommandResult(2 if self.fail_program else 0, "program verify\n", "")
        if command == "init; reset run; shutdown":
            if self.fail_reset_after_program and any(
                "program " in prior[-1]
                for prior in self.commands
                if prior and prior[0] == "openocd"
            ):
                return CommandResult(2, "", "reset failed")
            return CommandResult(0, "target running\n", "")
        if command == "init; halt; shutdown":
            return CommandResult(0, "target halted\n", "")
        if "mww " in command:
            self.all_off_commanded = True
            return CommandResult(0, "command written\n", "")
        if "phase2-target-flash.bin" in command:
            self.phase2_started = True
            if self.fail_phase2_readback:
                return CommandResult(2, "", "readback failed")
        stdout = ""
        for raw_path, raw_address, raw_size in DUMP_PATTERN.findall(command):
            path = Path(raw_path)
            address = int(raw_address, 16)
            size = int(raw_size, 16)
            if address == FLASH_ADDRESS:
                value = self.paths["firmware_bin"].read_bytes()
                if self.bad_readback:
                    value = b"X" + value[1:]
            elif address == 0x1FFF7550:
                value = (b"\x00" * 12) if self.bad_uid else UID
            elif address == MAILBOX_ADDRESS:
                value = self._mailbox(commanded=self.all_off_commanded)
            elif address == GPIO_ADDRESS:
                gpio_value = 7 if self.bad_gpio else 8
                value = gpio_value.to_bytes(4, "little")
            else:
                raise AssertionError(f"unexpected dump address {address:#x}")
            path.write_bytes(value[:size])
        if "mdw 0x50000014" in command:
            if self.fail_bench_probe:
                return CommandResult(2, "", "probe failed")
            gpio_value = 7 if self.bad_gpio else 8
            stdout = f"0x50000014: {gpio_value:08x}\n"
        return CommandResult(0, stdout, "")


def _common(paths: dict[str, Path], evidence_root: Path, role: str) -> dict[str, Any]:
    phase1_exists = (evidence_root / RUN_ID / PHASE1_FILENAME).is_file()
    checkpoint = "phase2_pre_openocd" if phase1_exists else "phase1_pre_openocd"
    timestamp = datetime.now(UTC).isoformat()
    mute_path = paths["repository"].parent / f"{checkpoint}-pluto-mute-{uuid4().hex}.json"
    _write_json(
        mute_path,
        {
            "schema": 1,
            "evidence_kind": PLUTO_MUTE_EVIDENCE_KIND,
            "checkpoint": checkpoint,
            "status": "passed",
            "serial": PLUTO_SERIAL,
            "uri": PLUTO_URI,
            "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
            "dds_raw_readback": [0.0] * 8,
            "dds_scale_readback": [0.0] * 8,
            "dds_enabled_readback": [False] * 8,
            "started_at": timestamp,
            "completed_at": timestamp,
            "source": {
                "repository": str(paths["repository"]),
                "commit": COMMIT,
                "clean_worktree_verified": True,
            },
            "error": None,
        },
    )
    mute_path.chmod(0o400)
    return {
        "campaign_id": CAMPAIGN_ID,
        "run_id": RUN_ID,
        "board_id": BOARD_ID,
        "image_role": role,
        "elf": paths["elf"],
        "firmware_bin": paths["firmware_bin"],
        "build_manifest": paths["build_manifest"] if role == "bench" else None,
        "profile": paths["profile"] if role == "fast20" else None,
        "openocd_config": paths["openocd_config"],
        "evidence_root": evidence_root,
        "pluto_serial": PLUTO_SERIAL,
        "pluto_uri": PLUTO_URI,
        "pluto_mute_evidence": mute_path,
        "repository": paths["repository"],
    }


def _phase1(
    tmp_path: Path, role: str = "bench"
) -> tuple[dict[str, Path], Path, FakeBoundary, Path]:
    paths = _repository(tmp_path, role)
    evidence_root = tmp_path / "evidence"
    attestation = tmp_path / "pre-program.json"
    _write_json(
        attestation,
        _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND, role=role),
    )
    boundary = FakeBoundary(paths)
    result = prepare_and_program(
        **_common(paths, evidence_root, role),
        pre_program_attestation=attestation,
        python_executable=Path("/test/python"),
        boundary=boundary,
    )
    return paths, evidence_root, boundary, result.run_directory


def _phase2(tmp_path: Path, role: str = "bench") -> tuple[dict[str, Path], FakeBoundary, Path, Any]:
    paths, evidence_root, boundary, run_directory = _phase1(tmp_path, role)
    power = _seal_power_attestation(run_directory, evidence_root, role=role)
    result = verify_after_power_cycle(
        **_common(paths, evidence_root, role),
        power_cycle_attestation=power,
        boundary=boundary,
        sleep=lambda _: None,
    )
    return paths, boundary, run_directory, result


def test_bench_two_phase_success_seals_downstream_path_hash(tmp_path: Path) -> None:
    _, boundary, run_directory, result = _phase2(tmp_path)
    assert result.path == run_directory / FINAL_EVIDENCE_FILENAME
    assert (run_directory / FINAL_DIGEST_FILENAME).read_text(encoding="ascii") == (
        f"{result.sha256}  {FINAL_EVIDENCE_FILENAME}\n"
    )
    document = validate_sealed_selector_evidence(
        result.path,
        expected_sha256=result.sha256,
        expected_campaign_id=CAMPAIGN_ID,
        expected_run_id=RUN_ID,
        expected_board_id=BOARD_ID,
        expected_image_role="bench",
    )
    assert document["evidence_kind"] == EVIDENCE_KIND
    assert document["startup"]["mailbox"]["command_valid"] is True
    assert document["startup"]["gpio_output_latch"]["masked_selector_code"] == 8
    assert document["startup"]["physical_rf_state_proven"] is False
    assert boundary.all_off_commanded is True

    phase1 = json.loads((run_directory / PHASE1_FILENAME).read_text(encoding="utf-8"))
    phase1_mute_path = Path(phase1["pluto_pre_openocd_mute"]["source"]["path"])
    phase2_mute_path = Path(document["operator_attestations"]["phase2_pluto_mute_source"]["path"])
    assert phase1_mute_path != phase2_mute_path
    for mute_path, checkpoint in (
        (phase1_mute_path, "phase1_pre_openocd"),
        (phase2_mute_path, "phase2_pre_openocd"),
    ):
        mute = json.loads(mute_path.read_text(encoding="utf-8"))
        assert mute["checkpoint"] == checkpoint
        completed_at = datetime.fromisoformat(mute["completed_at"])
        assert abs((datetime.now(UTC) - completed_at).total_seconds()) < 60.0


def test_fast20_success_does_not_claim_timing(tmp_path: Path) -> None:
    _, _, _, result = _phase2(tmp_path, "fast20")
    document = validate_sealed_selector_evidence(
        result.path,
        expected_sha256=result.sha256,
        expected_campaign_id=CAMPAIGN_ID,
        expected_run_id=RUN_ID,
        expected_board_id=BOARD_ID,
        expected_image_role="fast20",
    )
    assert document["startup"]["autonomous_schedule_timing_proven"] is False
    assert document["startup"]["runtime_gpio_sequence_proven"] is False


def test_phase1_requires_valid_pre_program_electrical_attestation(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND)
    attestation["j11_pin1_voltage_v"] = 3.2
    path = tmp_path / "invalid-pre-program.json"
    _write_json(path, attestation)
    boundary = FakeBoundary(paths)
    with pytest.raises(SelectorFlashError, match="J11.1"):
        prepare_and_program(
            **_common(paths, tmp_path / "evidence", "bench"),
            pre_program_attestation=path,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    assert boundary.commands == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.update(serial="wrong-serial"), "identity"),
        (
            lambda value: value["tx_hardware_gain_db_by_channel"].__setitem__(1, -79.0),
            "TX1/TX2",
        ),
        (lambda value: value["dds_scale_readback"].__setitem__(7, 0.1), "DDS scales"),
    ),
)
def test_phase1_rejects_invalid_pluto_mute_before_any_openocd(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    values = _common(paths, tmp_path / "evidence", "bench")
    mute_path = values["pluto_mute_evidence"]
    mute_path.chmod(0o600)
    mute = json.loads(mute_path.read_text(encoding="utf-8"))
    mutation(mute)
    _write_json(mute_path, mute)
    mute_path.chmod(0o400)

    with pytest.raises(SelectorFlashError, match=message):
        prepare_and_program(
            **values,
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )

    assert not any(command and command[0] == "openocd" for command in boundary.commands)
    assert not (tmp_path / "evidence" / RUN_ID).exists()


def test_phase1_rejects_stale_pluto_mute_before_any_openocd(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    values = _common(paths, tmp_path / "evidence", "bench")
    mute_path = values["pluto_mute_evidence"]
    mute_path.chmod(0o600)
    mute = json.loads(mute_path.read_text(encoding="utf-8"))
    mute.update(
        started_at="2020-01-01T11:00:00+00:00",
        completed_at="2020-01-01T11:00:00+00:00",
    )
    _write_json(mute_path, mute)
    mute_path.chmod(0o400)

    with pytest.raises(SelectorFlashError, match="contemporaneous"):
        prepare_and_program(
            **values,
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )

    assert not any(command and command[0] == "openocd" for command in boundary.commands)
    assert not (tmp_path / "evidence" / RUN_ID).exists()


def test_phase1_binds_exact_pluto_mute_source_and_immutable_snapshot(tmp_path: Path) -> None:
    paths, _, _, run_directory = _phase1(tmp_path)
    phase1 = json.loads((run_directory / PHASE1_FILENAME).read_text(encoding="utf-8"))
    binding = phase1["pluto_pre_openocd_mute"]
    assert binding["serial"] == PLUTO_SERIAL
    assert binding["uri"] == PLUTO_URI
    assert binding["checkpoint"] == "phase1_pre_openocd"
    assert binding["validated_immediately_before_openocd"] is True
    assert Path(binding["source"]["path"]).stat().st_mode & 0o200 == 0
    assert Path(binding["snapshot"]["path"]).stat().st_mode & 0o200 == 0
    assert phase1["source"]["repository"] == str(paths["repository"])


def test_power_cycle_draft_is_editable_then_sealed_create_only(tmp_path: Path) -> None:
    _, root, _, run = _phase1(tmp_path)
    draft = run / POWER_CYCLE_TEMPLATE_FILENAME
    assert draft.stat().st_mode & 0o200
    sealed = _seal_power_attestation(run, root)
    assert sealed == run / POWER_CYCLE_SEALED_FILENAME
    assert sealed.stat().st_mode & 0o222 == 0
    assert json.loads(sealed.read_text(encoding="utf-8"))["power_removed_duration_s"] == 5.2

    with pytest.raises(SelectorFlashError, match="refusing to overwrite"):
        seal_power_cycle_attestation(
            campaign_id=CAMPAIGN_ID,
            run_id=RUN_ID,
            board_id=BOARD_ID,
            image_role="bench",
            evidence_root=root,
            power_cycle_draft=draft,
        )


def test_phase2_rejects_editable_draft_before_openocd(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    draft = run / POWER_CYCLE_TEMPLATE_FILENAME
    _write_json(draft, _power_attestation(run))
    before = len(boundary.commands)

    with pytest.raises(SelectorFlashError, match="run-bound sealed"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=draft,
            boundary=boundary,
            sleep=lambda _: None,
        )

    assert not any(command and command[0] == "openocd" for command in boundary.commands[before:])


def test_pre_program_template_writer_is_create_only_and_run_bound(tmp_path: Path) -> None:
    output = tmp_path / "pre-program-attestation.json"
    result = write_pre_program_attestation_template(
        output,
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        board_id=BOARD_ID,
        image_role="bench",
    )
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["evidence_kind"] == PRE_PROGRAM_ATTESTATION_KIND
    assert document["campaign_id"] == CAMPAIGN_ID
    assert document["run_id"] == RUN_ID
    assert document["board_id"] == BOARD_ID
    assert document["image_role"] == "bench"
    assert document["target_powered"] is False
    assert result.stat().st_mode & 0o200

    with pytest.raises(SelectorFlashError, match="refusing to overwrite"):
        write_pre_program_attestation_template(
            output,
            campaign_id=CAMPAIGN_ID,
            run_id=RUN_ID,
            board_id=BOARD_ID,
            image_role="bench",
        )


def test_phase1_rejects_dirty_source_before_creating_run(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    boundary.dirty = True
    evidence_root = tmp_path / "evidence"
    with pytest.raises(SelectorFlashError, match="source tree must be clean"):
        prepare_and_program(
            **_common(paths, evidence_root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    assert not (evidence_root / RUN_ID).exists()


def test_phase1_can_require_current_image_exact_match_before_programming(
    tmp_path: Path,
) -> None:
    paths = _repository(tmp_path, "fast20")
    attestation = tmp_path / "pre-program.json"
    _write_json(
        attestation,
        _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND, role="fast20"),
    )
    boundary = FakeBoundary(paths)
    result = prepare_and_program(
        **_common(paths, tmp_path / "evidence", "fast20"),
        pre_program_attestation=attestation,
        python_executable=Path("/test/python"),
        require_current_image_match=True,
        boundary=boundary,
    )

    phase1 = json.loads(result.phase1_path.read_text(encoding="utf-8"))
    programming = phase1["programming"]
    assert programming["pre_program_current_image_match_required"] is True
    assert programming["pre_program_current_image_exact_match"] is True
    assert programming["pre_program_current_image_readback"]["sha256"] == _sha256(
        paths["firmware_bin"]
    )
    assert any("phase1-pre-program-flash.bin" in command[-1] for command in boundary.commands)


def test_phase1_current_image_mismatch_refuses_programming(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "fast20")
    attestation = tmp_path / "pre-program.json"
    _write_json(
        attestation,
        _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND, role="fast20"),
    )
    boundary = FakeBoundary(paths)
    boundary.bad_readback = True

    with pytest.raises(SelectorFlashError, match="programming refused"):
        prepare_and_program(
            **_common(paths, tmp_path / "evidence", "fast20"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            require_current_image_match=True,
            boundary=boundary,
        )

    assert not any("program " in command[-1] for command in boundary.commands)


def test_phase1_refuses_preexisting_or_symlink_run_directory(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    root = tmp_path / "evidence"
    root.mkdir()
    (root / RUN_ID).mkdir()
    with pytest.raises(SelectorFlashError, match="already exists"):
        prepare_and_program(
            **_common(paths, root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=FakeBoundary(paths),
        )


def test_phase1_rejects_symlinked_evidence_root(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    real_root = tmp_path / "real-evidence"
    real_root.mkdir()
    linked_root = tmp_path / "linked-evidence"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(SelectorFlashError, match="symlinks"):
        prepare_and_program(
            **_common(paths, linked_root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=FakeBoundary(paths),
        )


@pytest.mark.parametrize("failure", ["fail_build", "fail_verify"])
def test_phase1_build_or_verifier_failure_is_tombstoned(tmp_path: Path, failure: str) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    setattr(boundary, failure, True)
    root = tmp_path / "evidence"
    with pytest.raises(SelectorFlashError):
        prepare_and_program(
            **_common(paths, root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    failure_document = json.loads((root / RUN_ID / FAILURE_FILENAME).read_text())
    assert failure_document["downstream_use_permitted"] is False
    assert not any(command and command[0] == "openocd" for command in boundary.commands)


def test_wrong_live_uid_stops_before_program_and_resumes_old_image(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    boundary.bad_uid = True
    root = tmp_path / "evidence"
    with pytest.raises(SelectorFlashError, match="UID differs"):
        prepare_and_program(
            **_common(paths, root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    openocd_commands = [command[-1] for command in boundary.commands if command[0] == "openocd"]
    assert "init; reset run; shutdown" in openocd_commands
    assert not any("program " in command for command in openocd_commands)


def test_program_failure_is_halted_and_never_awaiting_power_cycle(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    boundary.fail_program = True
    root = tmp_path / "evidence"
    with pytest.raises(SelectorFlashError, match="program-with-verify"):
        prepare_and_program(
            **_common(paths, root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    run = root / RUN_ID
    failure = json.loads((run / FAILURE_FILENAME).read_text())
    assert failure["target_state_after_failure"] == "halted"
    assert not (run / PHASE1_FILENAME).exists()
    program_index = next(
        index for index, command in enumerate(boundary.commands) if "program " in command[-1]
    )
    later = [command[-1] for command in boundary.commands[program_index + 1 :]]
    assert later == ["init; halt; shutdown"]


def test_phase1_final_resume_failure_records_target_halted(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    boundary = FakeBoundary(paths)
    boundary.fail_reset_after_program = True
    root = tmp_path / "evidence"
    with pytest.raises(SelectorFlashError, match="could not be returned to reset run"):
        prepare_and_program(
            **_common(paths, root, "bench"),
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=boundary,
        )
    failure = json.loads((root / RUN_ID / FAILURE_FILENAME).read_text())
    assert failure["target_state_after_failure"] == "halted"
    assert failure["reset_run_attempted"] is True
    assert failure["reset_run_succeeded"] is False


def test_phase2_requires_five_second_power_cycle_before_hardware(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    power_document = _power_attestation(run)
    power_document["power_removed_duration_s"] = 4.9
    power = _publish_forged_sealed_power(run, power_document)
    command_count = len(boundary.commands)
    with pytest.raises(SelectorFlashError, match="at least five seconds"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    assert len(boundary.commands) == command_count + 2  # clean-source git checks only
    assert not boundary.phase2_started


def test_phase2_requires_fresh_post_cycle_pluto_mute_before_openocd(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    power = _seal_power_attestation(run, root)
    values = _common(paths, root, "bench")
    mute_path = values["pluto_mute_evidence"]
    mute_path.chmod(0o600)
    mute = json.loads(mute_path.read_text(encoding="utf-8"))
    mute["checkpoint"] = "phase1_pre_openocd"
    _write_json(mute_path, mute)
    mute_path.chmod(0o400)
    before = len(boundary.commands)

    with pytest.raises(SelectorFlashError, match="checkpoint"):
        verify_after_power_cycle(
            **values,
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )

    phase2_commands = boundary.commands[before:]
    assert not any(command and command[0] == "openocd" for command in phase2_commands)
    assert not boundary.phase2_started


def test_phase2_power_cycle_must_bind_exact_phase1_hash(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    power_document = _power_attestation(run)
    power_document["phase1_sha256"] = "0" * 64
    power = _publish_forged_sealed_power(run, power_document)
    with pytest.raises(SelectorFlashError, match="phase-1 SHA-256"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    assert not boundary.phase2_started


def test_phase2_rejects_changed_phase1_input_before_hardware(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    power = _seal_power_attestation(run, root)
    paths["firmware_bin"].chmod(0o600)
    paths["firmware_bin"].write_bytes(b"changed firmware")
    with pytest.raises(SelectorFlashError, match="frozen identity"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    assert not boundary.phase2_started


def test_phase2_refuses_preexisting_partial_output(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    power = _seal_power_attestation(run, root)
    (run / "phase2-target-flash.bin").write_bytes(b"partial")
    with pytest.raises(SelectorFlashError, match="partial-log overwrite"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    assert not boundary.phase2_started


@pytest.mark.parametrize("fault", ["bad_uid", "bad_readback", "fail_phase2_readback"])
def test_unverified_phase2_target_is_left_halted_and_not_reset(tmp_path: Path, fault: str) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    setattr(boundary, fault, True)
    power = _seal_power_attestation(run, root)
    before = len(boundary.commands)
    with pytest.raises(SelectorFlashError):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    phase2_commands = [
        command[-1] for command in boundary.commands[before:] if command and command[0] == "openocd"
    ]
    assert phase2_commands[-1] == "init; halt; shutdown"
    assert "init; reset run; shutdown" not in phase2_commands
    failure = json.loads((run / FAILURE_FILENAME).read_text())
    assert failure["target_state_after_failure"] == "halted"
    assert not (run / FINAL_EVIDENCE_FILENAME).exists()


def test_phase2_resume_failure_after_exact_match_records_halted(tmp_path: Path) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    boundary.fail_reset_after_program = True
    power = _seal_power_attestation(run, root)
    with pytest.raises(SelectorFlashError, match="could not be returned to reset run"):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    failure = json.loads((run / FAILURE_FILENAME).read_text())
    assert failure["target_state_after_failure"] == "halted"
    assert failure["reset_run_succeeded"] is False
    assert not (run / FINAL_EVIDENCE_FILENAME).exists()


@pytest.mark.parametrize("fault", ["bad_mailbox", "bad_gpio", "fail_bench_probe"])
def test_bench_startup_failure_never_seals_and_recovers_known_image(
    tmp_path: Path, fault: str
) -> None:
    paths, root, boundary, run = _phase1(tmp_path)
    setattr(boundary, fault, True)
    power = _seal_power_attestation(run, root)
    with pytest.raises(SelectorFlashError):
        verify_after_power_cycle(
            **_common(paths, root, "bench"),
            power_cycle_attestation=power,
            boundary=boundary,
            sleep=lambda _: None,
        )
    failure = json.loads((run / FAILURE_FILENAME).read_text())
    assert failure["target_state_after_failure"] == "running"
    assert failure["reset_run_succeeded"] is True
    assert not (run / FINAL_EVIDENCE_FILENAME).exists()


def test_loader_rejects_wrong_hash_and_changed_leaf(tmp_path: Path) -> None:
    _, _, _, result = _phase2(tmp_path, "fast20")
    with pytest.raises(SelectorFlashError, match="SHA-256 differs"):
        validate_sealed_selector_evidence(
            result.path,
            expected_sha256="0" * 64,
            expected_campaign_id=CAMPAIGN_ID,
            expected_run_id=RUN_ID,
            expected_board_id=BOARD_ID,
            expected_image_role="fast20",
        )
    manifest = json.loads(result.path.read_text())
    readback = Path(manifest["target_flash_readback"]["path"])
    readback.chmod(0o600)
    readback.write_bytes(b"tampered")
    with pytest.raises(SelectorFlashError, match="frozen identity"):
        validate_sealed_selector_evidence(
            result.path,
            expected_sha256=result.sha256,
            expected_campaign_id=CAMPAIGN_ID,
            expected_run_id=RUN_ID,
            expected_board_id=BOARD_ID,
            expected_image_role="fast20",
        )


def test_loader_rejects_fast20_timing_overclaim_even_with_new_outer_hash(
    tmp_path: Path,
) -> None:
    _, _, _, result = _phase2(tmp_path, "fast20")
    document = json.loads(result.path.read_text())
    document["startup"]["autonomous_schedule_timing_proven"] = True
    result.path.chmod(0o600)
    _write_json(result.path.with_name("forged.json"), document)
    forged = result.path.with_name("forged.json")
    with pytest.raises(SelectorFlashError, match="overclaims"):
        validate_sealed_selector_evidence(
            forged,
            expected_sha256=_sha256(forged),
            expected_campaign_id=CAMPAIGN_ID,
            expected_run_id=RUN_ID,
            expected_board_id=BOARD_ID,
            expected_image_role="fast20",
        )


def test_role_arguments_are_exact_and_mutually_exclusive(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    attestation = tmp_path / "pre-program.json"
    _write_json(attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    values = _common(paths, tmp_path / "evidence", "bench")
    values["profile"] = paths["profile"]
    with pytest.raises(SelectorFlashError, match="forbids --profile"):
        prepare_and_program(
            **values,
            pre_program_attestation=attestation,
            python_executable=Path("/test/python"),
            boundary=FakeBoundary(paths),
        )


def test_symlinked_input_is_rejected(tmp_path: Path) -> None:
    paths = _repository(tmp_path, "bench")
    real_attestation = tmp_path / "pre-program-real.json"
    _write_json(real_attestation, _operator_attestation(PRE_PROGRAM_ATTESTATION_KIND))
    link = tmp_path / "pre-program-link.json"
    link.symlink_to(real_attestation)
    with pytest.raises(SelectorFlashError, match="symlinks"):
        prepare_and_program(
            **_common(paths, tmp_path / "evidence", "bench"),
            pre_program_attestation=link,
            python_executable=Path("/test/python"),
            boundary=FakeBoundary(paths),
        )
