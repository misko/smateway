from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_hexcal_calibration.py"
SPEC = importlib.util.spec_from_file_location("hexcal_runner_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

CAPTURE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/capture_hexcal.py"
CAPTURE_SPEC = importlib.util.spec_from_file_location("hexcal_capture_under_test", CAPTURE_SCRIPT)
assert CAPTURE_SPEC is not None and CAPTURE_SPEC.loader is not None
capture_program = importlib.util.module_from_spec(CAPTURE_SPEC)
sys.modules[CAPTURE_SPEC.name] = capture_program
CAPTURE_SPEC.loader.exec_module(capture_program)

REPOSITORY = Path("/repo")
PYTHON = Path("/opt/hexcal-python")
PROFILE = Path("/repo/profiles/hexcal-v1/control_profile.json")
PROFILE_SHA = "a" * 64
SOURCE_COMMIT = "1" * 40
DEPENDENCY_ATTESTATION = {
    "schema": 1,
    "dependency": "pluto-plus-utils",
    "commit": "2" * 40,
    "files": [],
}


def _firmware_evidence() -> Any:
    return runner.HexcalFirmwareEvidence(
        path=Path("/evidence/hexcal-firmware-evidence.json"),
        file_sha256="c" * 64,
        board_id="board-a",
        target_uid="uid-a",
        target_uid_readback_path=Path("/evidence/target-uid.bin"),
        target_uid_readback_sha256="9" * 64,
        target_uid_readback_size_bytes=12,
        source_commit=SOURCE_COMMIT,
        profile_file_sha256=PROFILE_SHA,
        profile_contract_sha256="b" * 64,
        firmware_elf_path=Path("/evidence/hexcal.elf"),
        firmware_elf_sha256="a" * 64,
        firmware_elf_size_bytes=304428,
        firmware_bin_path=Path("/evidence/hexcal.bin"),
        firmware_bin_sha256="d" * 64,
        firmware_bin_size_bytes=4096,
        full_flash_readback_path=Path("/evidence/full-flash.bin"),
        full_flash_readback_sha256="f" * 64,
        full_flash_readback_size_bytes=16 * 1024,
        verified_at="2026-08-26T12:00:00+00:00",
        verification_method="synthetic full flash readback",
    )


def _gain_qualification(frequencies: tuple[int, ...]) -> Any:
    return runner.HexcalGainQualification(
        path=Path("/evidence/gain-qualification.json"),
        file_sha256="8" * 64,
        qualification_id="gain-a",
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        source_commit=SOURCE_COMMIT,
        profile_file_sha256=PROFILE_SHA,
        profile_contract_sha256="b" * 64,
        firmware_evidence_sha256="c" * 64,
        pluto_plus_utils_source_attestation_sha256=runner.canonical_json_sha256(
            DEPENDENCY_ATTESTATION
        ),
        center_frequencies_hz=frequencies,
        candidate_gains_db=(0,),
        tested_gains_db=(0,),
        selected_receiver_gain_db=0,
        completed_at="2026-08-26T11:59:00+00:00",
    )


def _stimulus_qualification() -> Any:
    return runner.HexcalStimulusQualification(
        path=Path("/evidence/stimulus-qualification.json"),
        file_sha256="7" * 64,
        qualification_id="stimulus-a",
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        source_commit=SOURCE_COMMIT,
        profile_file_sha256=PROFILE_SHA,
        profile_contract_sha256="b" * 64,
        firmware_evidence_sha256="c" * 64,
        pluto_plus_utils_source_attestation_sha256=runner.canonical_json_sha256(
            DEPENDENCY_ATTESTATION
        ),
        center_frequencies_hz=runner.STIMULUS_CENTER_FREQUENCIES_HZ,
        fixed_receiver_gain_db=20,
        candidate_tx_hardware_gains_db=(-35.0, -30.0),
        tested_tx_hardware_gains_db=(-35.0, -30.0),
        selected_tx_hardware_gain_db=-30.0,
        dds_scale=0.125,
        completed_at="2026-08-26T11:59:00+00:00",
    )


def _configuration(
    *,
    rounds: int = 3,
    frequencies: tuple[int, ...] = runner.DEFAULT_FREQUENCIES_HZ,
    max_attempts: int = 3,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runner._configuration(
            rounds=rounds,
            max_attempts=max_attempts,
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            python=PYTHON,
            profile=PROFILE,
            profile_file_sha256=PROFILE_SHA,
            profile_contract_sha256="b" * 64,
            timeout_s=180,
            receiver_gain_db=0,
            tx_hardware_gain_db=-40.0,
            dds_scale=0.125,
            center_frequencies_hz=frequencies,
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=DEPENDENCY_ATTESTATION,
            firmware_evidence=_firmware_evidence(),
            gain_qualification=_gain_qualification(frequencies),
            allow_experimental_5g8=5_800_000_000 in frequencies,
        ),
    )


def _v2_configuration() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runner._configuration(
            rounds=3,
            max_attempts=3,
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            python=PYTHON,
            profile=PROFILE,
            profile_file_sha256=PROFILE_SHA,
            profile_contract_sha256="b" * 64,
            timeout_s=180,
            receiver_gain_db=20,
            tx_hardware_gain_db=-30.0,
            dds_scale=0.125,
            center_frequencies_hz=runner.STIMULUS_CENTER_FREQUENCIES_HZ,
            source_commit=SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=DEPENDENCY_ATTESTATION,
            firmware_evidence=_firmware_evidence(),
            gain_qualification=None,
            stimulus_qualification=_stimulus_qualification(),
            allow_experimental_5g8=False,
        ),
    )


def _command_result(
    command: list[str], return_code: int, stdout: str, stderr: str = ""
) -> dict[str, Any]:
    return {
        "command": command,
        "started_at": "2026-08-26T12:00:00+00:00",
        "completed_at": "2026-08-26T12:00:01+00:00",
        "return_code": return_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "error": None,
    }


def _identity(artifact_id: str, artifact_root: Path, analysis_sha: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "path": str(artifact_root),
        "data_sha256": "c" * 64,
        "data_size_bytes": 8_000_000,
        "metadata_sha256": "d" * 64,
        "metadata_size_bytes": 1000,
        "capture_record_sha256": "e" * 64,
        "capture_record_size_bytes": 2000,
        "analysis_sha256": analysis_sha,
        "analysis_size_bytes": 3000,
        "stream_id": 991,
        "metadata_abi": 2,
        "implementation_source_commit": SOURCE_COMMIT,
        "pluto_plus_utils_source_attestation_sha256": runner.canonical_json_sha256(
            DEPENDENCY_ATTESTATION
        ),
        "firmware_evidence_sha256": "c" * 64,
        "firmware_bin_sha256": "d" * 64,
        "full_flash_readback_sha256": "f" * 64,
        "rf_readback_evidence_sha256": "0" * 64,
        "dds_frequency_readback_hz": [100_000.0, 0.0, -100_000.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "dds_tone_offset_hz": 100_000.0,
        "emitted_carrier_frequency_hz": 2_400_100_000.0,
    }


def test_default_plan_is_three_rounds_six_frequencies_tx1_only() -> None:
    configuration = _configuration()
    plan = runner._execution_plan(configuration, REPOSITORY)

    assert len(plan) == 18
    assert [item["center_frequency_hz"] for item in plan[:6]] == [
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
        2_458_000_000,
        2_483_000_000,
        5_800_000_000,
    ]
    assert [item["center_frequency_hz"] for item in plan[6:12]] == [
        5_800_000_000,
        2_483_000_000,
        2_458_000_000,
        2_440_000_000,
        2_423_000_000,
        2_400_000_000,
    ]
    assert [item["center_frequency_hz"] for item in plan[12:]] == [
        2_440_000_000,
        2_458_000_000,
        2_483_000_000,
        5_800_000_000,
        2_400_000_000,
        2_423_000_000,
    ]
    assert {item["tx_channel"] for item in plan} == {0}
    assert {item["tx_port"] for item in plan} == {"TX1"}
    assert {item["gain_qualification_id"] for item in plan} == {"gain-a"}
    assert {item["gain_qualification_sha256"] for item in plan} == {"8" * 64}
    assert all(item["profile_file_sha256"] == PROFILE_SHA for item in plan)
    five_g8 = next(item for item in plan if item["center_frequency_hz"] == 5_800_000_000)
    assert "--allow-experimental-5g8" in five_g8["capture_command"]
    assert "--serial" in five_g8["capture_command"]
    assert "--uri" in five_g8["capture_command"]
    parsed_capture = capture_program._parser().parse_args(five_g8["capture_command"][2:])
    assert parsed_capture.board_id == "board-a"
    assert parsed_capture.serial == "serial-a"
    assert parsed_capture.uri == "usb:1.2.3"
    assert parsed_capture.center_frequency_hz == 5_800_000_000
    assert parsed_capture.receiver_gain_db == 0
    assert runner.ARTIFACT_TOKEN in five_g8["reanalysis_command_template"]


def test_v2_plan_is_exact_fifteen_artifact_five_frequency_matrix() -> None:
    configuration = _v2_configuration()
    plan = runner._execution_plan(configuration, REPOSITORY)

    assert configuration["protocol_id"] == runner.STIMULUS_PROTOCOL_ID
    assert len(plan) == 15
    assert [item["center_frequency_hz"] for item in plan] == [
        *runner.STIMULUS_CENTER_FREQUENCIES_HZ,
        *reversed(runner.STIMULUS_CENTER_FREQUENCIES_HZ),
        2_440_000_000,
        2_472_000_000,
        2_483_000_000,
        2_400_000_000,
        2_423_000_000,
    ]
    assert {item["receiver_gain_db"] for item in plan} == {20}
    assert {item["planned_tx_hardware_gain_db"] for item in plan} == {-30.0}
    assert {item["qualification_kind"] for item in plan} == {"stimulus"}
    assert {item["stimulus_qualification_id"] for item in plan} == {"stimulus-a"}
    assert {item["stimulus_qualification_sha256"] for item in plan} == {"7" * 64}
    assert all("--allow-experimental-5g8" not in item["capture_command"] for item in plan)
    manifest = runner._new_manifest("v2-run", configuration, REPOSITORY)
    assert manifest["experiment_kind"] == "hexcal_v2_1_2g4_tx1_center_calibration"


def test_parser_requires_explicit_serial_and_uri() -> None:
    with pytest.raises(SystemExit):
        runner._parser().parse_args([])

    args = runner._parser().parse_args(
        (
            "--serial",
            "serial-a",
            "--uri",
            "usb:1.2.3",
            "--firmware-evidence",
            "/evidence.json",
            "--gain-qualification",
            "/gain-qualification.json",
        )
    )
    assert args.serial == "serial-a"
    assert args.uri == "usb:1.2.3"
    assert args.rounds == 3
    assert args.max_attempts_per_condition == 3
    assert args.gain_qualification == Path("/gain-qualification.json")
    assert args.allow_experimental_5g8 is False

    with pytest.raises(SystemExit):
        runner._parser().parse_args(
            (
                "--serial",
                "serial-a",
                "--uri",
                "usb:1.2.3",
                "--firmware-evidence",
                "/evidence.json",
                "--gain-qualification",
                "/gain-qualification.json",
                "--receiver-gain-db",
                "20",
            )
        )

    with pytest.raises(ValueError, match="experimental"):
        runner._validate_frequencies(None, allow_experimental_5g8=False)
    assert (
        runner._validate_frequencies(None, allow_experimental_5g8=True)
        == runner.DEFAULT_FREQUENCIES_HZ
    )

    capture_args = capture_program._parser().parse_args(
        (
            "--serial",
            "serial-a",
            "--uri",
            "usb:1.2.3",
            "--center-frequency-hz",
            "2400000000",
            "--source-commit",
            SOURCE_COMMIT,
            "--pluto-plus-utils-attestation-sha256",
            "2" * 64,
            "--firmware-evidence",
            "/evidence.json",
            "--firmware-evidence-sha256",
            "c" * 64,
        )
    )
    assert capture_args.receiver_gain_db == 0
    assert capture_program.SAMPLE_RATE_HZ == 1_000_000
    assert capture_program.SAMPLES_PER_FRAME == 100_000
    assert capture_program.FRAME_COUNT == 10


def test_main_capture_binds_exact_eight_buffers_and_all_tx_dds_readbacks() -> None:
    capture = SimpleNamespace(
        kernel_buffers=8,
        tx_gain_readback_db=-40.0,
        dds_scale_readback=(0.125, 0.0, -0.125, 0.0, 0.0, 0.0, 0.0, 0.0),
        dds_enabled_readback=(True, False, True, False, False, False, False, False),
        dds_frequency_readback_hz=(100_000, 0, -100_000, 0, 0, 0, 0, 0),
    )
    args = SimpleNamespace(tx_hardware_gain_db=-40.0, dds_scale=0.125)

    evidence = capture_program._rf_readback_evidence(capture, args)
    assert evidence["kernel_buffers"] == 8
    assert evidence["tx_hardware_gain_readback_db_by_channel"] == [-40.0, -80.0]
    assert evidence["dds_enabled_readback"] == [
        True,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    ]

    capture.dds_enabled_readback = (True,) * 8
    capture_program._rf_readback_evidence(capture, args)

    capture.dds_enabled_readback = (True, True, True, True, True, 1, True, True)
    with pytest.raises(ValueError, match="exact booleans"):
        capture_program._rf_readback_evidence(capture, args)


def test_resume_binds_configuration_plan_attempt_order_and_profile_hash(tmp_path: Path) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed = runner._load_manifest(path, configuration, REPOSITORY)
    assert resumed["resume_count"] == 1
    assert resumed["plan"] == manifest["plan"]

    changed = _configuration(rounds=1, frequencies=(2_400_000_000,))
    changed["profile_file_sha256"] = "f" * 64
    with pytest.raises(runner.ExperimentError, match="arguments differ"):
        runner._load_manifest(path, changed, REPOSITORY)

    damaged = json.loads(path.read_text(encoding="utf-8"))
    damaged["plan"][0]["capture_command"].append("--unplanned")
    path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="execution plan differs"):
        runner._load_manifest(path, configuration, REPOSITORY)


def test_enodata_execution_failure_gets_exact_mute_then_fresh_stream_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(
        rounds=1,
        frequencies=(2_400_000_000,),
        max_attempts=3,
    )
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    manifest_path = tmp_path / "run" / "manifest.json"
    board_root = tmp_path / "board"
    capture_root = board_root / "pluto-usb-captures"
    artifact_id = "1" * 32
    failed_artifact_id = "f" * 32
    artifact_root = capture_root / artifact_id
    analysis_sha = "9" * 64
    calls: list[str] = []
    mutes: list[str] = []

    monkeypatch.setattr(runner, "_board_root", lambda _board_id: board_root)
    monkeypatch.setattr(runner, "_reattest_completed_artifacts", lambda *args, **kwargs: None)

    def fake_command(
        command: list[str], *, cwd: Path, environment: dict[str, str], timeout_s: int
    ) -> dict[str, Any]:
        del cwd, environment, timeout_s
        if "capture_hexcal.py" in command[1]:
            capture_number = sum(value == "capture" for value in calls)
            calls.append("capture")
            if capture_number == 0:
                failed_root = capture_root / ".failed" / failed_artifact_id
                failed_root.mkdir(parents=True)
                (failed_root / "partial.sigmf-data").write_bytes(b"partial")
                (failed_root / "failure.json").write_text(
                    json.dumps(
                        {
                            "artifact_id": failed_artifact_id,
                            "error": "OSError: [Errno 61] No data available",
                        }
                    ),
                    encoding="utf-8",
                )
                return _command_result(command, 1, "", "OSError: [Errno 61] No data available")
            artifact_root.mkdir(parents=True)
            return _command_result(command, 0, json.dumps({"artifact_id": artifact_id}))
        calls.append("reanalyze")
        return _command_result(
            command,
            0,
            json.dumps(
                {
                    "artifact_id": artifact_id,
                    "quality_passed": True,
                    "analysis_sha256": analysis_sha,
                }
            ),
        )

    def fake_identity(
        root: Path, condition: dict[str, Any], *, serial: str, uri: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del serial, uri
        return _identity(artifact_id, root, analysis_sha), {
            "artifact_id": artifact_id,
            "quality_passed": True,
            "status": "passed",
            "global_rejection_reasons": [],
            "analysis_path": str(root / runner.ANALYSIS_FILENAME),
            "center_frequency_hz": condition["center_frequency_hz"],
            "round_index": condition["round_index"],
            "round_order": condition["round_order"],
        }

    def fake_mute(serial: str, purpose: str) -> dict[str, Any]:
        mutes.append(purpose)
        return {
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        }

    monkeypatch.setattr(runner, "_run_command", fake_command)
    monkeypatch.setattr(runner, "_analysis_identity", fake_identity)
    monkeypatch.setattr(runner, "_strict_mute", fake_mute)

    result = runner._run_experiment(
        manifest,
        manifest_path,
        repository=REPOSITORY,
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        timeout_s=180,
        max_attempts=3,
    )

    assert result == 0
    assert calls == ["capture", "capture", "reanalyze"]
    assert mutes == ["post_attempt", "post_attempt", "final"]
    assert len(manifest["attempts"]) == 2
    failed, passed = manifest["attempts"]
    assert failed["retry"] == 0
    assert failed["status"] == "failed"
    assert failed["failure_kind"] == "execution"
    assert failed["artifact_identity"] is None
    assert "Errno 61" in failed["capture"]["stderr"]
    assert len(failed["quarantined_failures"]) == 1
    quarantine = failed["quarantined_failures"][0]
    assert quarantine["artifact_id"] == failed_artifact_id
    assert quarantine["accepted"] is False
    assert {item["name"] for item in quarantine["files"]} == {
        "failure.json",
        "partial.sigmf-data",
    }
    assert all(len(item["sha256"]) == 64 for item in quarantine["files"])
    assert passed["retry"] == 1
    assert passed["status"] == "complete"
    assert passed["artifact_identity"]["stream_id"] == 991
    assert manifest["final_mute"]["serial"] == "serial-a"


def test_quality_rejection_completes_condition_without_execution_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    condition = manifest["plan"][0]
    board_root = tmp_path / "board"
    artifact_id = "2" * 32
    artifact_root = board_root / "pluto-usb-captures" / artifact_id
    analysis_sha = "8" * 64
    command_count = 0

    monkeypatch.setattr(runner, "_board_root", lambda _board_id: board_root)

    def fake_command(
        command: list[str], *, cwd: Path, environment: dict[str, str], timeout_s: int
    ) -> dict[str, Any]:
        nonlocal command_count
        del cwd, environment, timeout_s
        command_count += 1
        if command_count == 1:
            artifact_root.mkdir(parents=True)
            return _command_result(command, 0, json.dumps({"artifact_id": artifact_id}))
        return _command_result(
            command,
            2,
            json.dumps(
                {
                    "artifact_id": artifact_id,
                    "quality_passed": False,
                    "analysis_sha256": analysis_sha,
                }
            ),
        )

    monkeypatch.setattr(runner, "_run_command", fake_command)
    monkeypatch.setattr(
        runner,
        "_analysis_identity",
        lambda root, condition, serial, uri: (
            _identity(artifact_id, root, analysis_sha),
            {
                "artifact_id": artifact_id,
                "quality_passed": False,
                "status": "rejected",
                "global_rejection_reasons": ["synthetic"],
                "analysis_path": str(root / runner.ANALYSIS_FILENAME),
                "center_frequency_hz": condition["center_frequency_hz"],
                "round_index": condition["round_index"],
                "round_order": condition["round_order"],
            },
        ),
    )
    monkeypatch.setattr(
        runner,
        "_strict_mute",
        lambda serial, purpose: {
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "error": None,
        },
    )

    succeeded, failure = runner._run_attempt(
        manifest,
        tmp_path / "manifest.json",
        condition,
        repository=REPOSITORY,
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        timeout_s=180,
    )

    assert succeeded is True
    assert failure is None
    assert len(manifest["attempts"]) == 1
    assert manifest["attempts"][0]["outcome"] == "quality_rejected"
    assert manifest["attempts"][0]["status"] == "complete"


def test_final_mute_failure_is_fatal_even_without_pending_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "run_id": "run-a",
        "configuration": {"serial": "serial-a"},
        "plan": [],
        "attempts": [],
        "final_mute_attempts": [],
        "summary": {},
    }
    monkeypatch.setattr(runner, "_reattest_completed_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_strict_mute",
        lambda serial, purpose: {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "error": "readback unavailable",
        },
    )

    with pytest.raises(runner.ExperimentError, match="strict final mute failed"):
        runner._run_experiment(
            manifest,
            tmp_path / "manifest.json",
            repository=REPOSITORY,
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            timeout_s=180,
            max_attempts=3,
        )


def test_clean_source_commit_is_bound_and_dirty_tree_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirty = False

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[1:] == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(command, 0, SOURCE_COMMIT + "\n", "")
        assert command[1:3] == ("status", "--porcelain")
        stdout = " M src/smateway/hexcal.py\n" if dirty else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner._repository_commit_and_require_clean(REPOSITORY) == SOURCE_COMMIT

    dirty = True
    with pytest.raises(runner.ExperimentError, match="dirty implementation tree"):
        runner._repository_commit_and_require_clean(REPOSITORY)


def test_resume_after_post_mute_failure_requires_persisted_recovery_mute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = runner._new_manifest(
        "run-a",
        _configuration(rounds=1, frequencies=(2_400_000_000,)),
        REPOSITORY,
    )
    condition = manifest["plan"][0]
    attempt = {
        "attempt_id": 1,
        "retry": 0,
        **condition,
        "status": "failed",
        "outcome": "post_mute_failed",
        "failure_kind": "post_attempt_mute",
        "artifact_identity": None,
        "post_mute": {"status": "failed", "serial": "serial-a"},
    }
    manifest["attempts"].append(attempt)
    manifest_path = tmp_path / "manifest.json"
    recovery = {
        "purpose": "resume_recovery",
        "status": "passed",
        "serial": "serial-a",
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }
    monkeypatch.setattr(runner, "_strict_mute", lambda serial, purpose: dict(recovery))

    runner._recover_stale_attempts(manifest, manifest_path, serial="serial-a")

    assert attempt["recovery_mute"] == recovery
    assert manifest["recovery_mute_attempts"] == [recovery]


def test_resume_marks_stale_running_attempt_with_recovery_not_fake_post_mute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = runner._new_manifest(
        "run-a",
        _configuration(rounds=1, frequencies=(2_400_000_000,)),
        REPOSITORY,
    )
    condition = manifest["plan"][0]
    attempt = {
        "attempt_id": 1,
        "retry": 0,
        **condition,
        "status": "running",
        "outcome": None,
        "failure_kind": None,
        "artifact_identity": None,
        "post_mute": None,
    }
    manifest["attempts"].append(attempt)
    recovery = {
        "purpose": "resume_recovery",
        "status": "passed",
        "serial": "serial-a",
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }
    monkeypatch.setattr(runner, "_strict_mute", lambda serial, purpose: dict(recovery))

    runner._recover_stale_attempts(manifest, tmp_path / "manifest.json", serial="serial-a")

    assert attempt["status"] == "failed"
    assert attempt["post_mute"] is None
    assert attempt["recovered_stale_process"] is True
    assert attempt["recovery_mute"] == recovery
    assert manifest["recovery_mute_attempts"] == [recovery]
