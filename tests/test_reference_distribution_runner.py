import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_fast20_reference_distribution.py"
SPEC = importlib.util.spec_from_file_location("reference_distribution_runner_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

REPOSITORY = Path("/repo")
PYTHON = Path("/opt/capture-python")
FREQUENCIES = (2_400_000_000, 2_420_000_000, 5_800_000_000)
ARTIFACT_ID = "a" * 32


def _configuration(
    *,
    rounds: int = 3,
    frequencies: tuple[int, ...] = FREQUENCIES,
    receiver_gain_db: int = 20,
    stimulus: str = "phase",
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runner._configuration(
            rounds=rounds,
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            python=PYTHON,
            timeout_s=180,
            receiver_gain_db=receiver_gain_db,
            stimulus=stimulus,
            center_frequencies_hz=frequencies,
        ),
    )


def _condition_pairs(plan: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [
        (int(condition["center_frequency_hz"]), int(condition["tx_channel"])) for condition in plan
    ]


def _completed_attempt(
    condition: dict[str, Any],
    *,
    attempt_id: int,
    retry: int = 0,
    quality_passed: bool,
) -> dict[str, Any]:
    artifact_id = f"{attempt_id:032x}"
    template = condition["reference_reanalysis_command_template"]
    reanalysis_command = [
        artifact_id if item == runner.ARTIFACT_TOKEN else item for item in template
    ]
    outcome = "quality_passed" if quality_passed else "quality_rejected"
    return {
        "attempt_id": attempt_id,
        "retry": retry,
        **condition,
        "started_at": "2026-08-25T12:00:00+00:00",
        "completed_at": "2026-08-25T12:00:10+00:00",
        "status": "complete",
        "outcome": outcome,
        "failure_kind": None,
        "artifact_id": artifact_id,
        "artifact_identity": {
            "artifact_id": artifact_id,
            "path": f"/artifacts/{artifact_id}",
            "sha256": "b" * 64,
        },
        "capture": {
            "status": "complete",
            "command": condition["capture_command"],
            "return_code": 0,
            "stdout": json.dumps({"artifact_id": artifact_id}),
            "stderr": "",
            "timed_out": False,
            "accepted": True,
            "parsed_output": {"artifact_id": artifact_id},
        },
        "reanalysis": {
            "status": "complete",
            "command": reanalysis_command,
            "return_code": 0 if quality_passed else 2,
            "stdout": json.dumps({"artifact_id": artifact_id, "quality_passed": quality_passed}),
            "stderr": "",
            "timed_out": False,
            "accepted": True,
            "parsed_output": {
                "artifact_id": artifact_id,
                "quality_passed": quality_passed,
            },
        },
        "quality_result": {
            "status": "passed" if quality_passed else "rejected",
            "quality_passed": quality_passed,
            "artifact_id": artifact_id,
            "artifact_path": f"/artifacts/{artifact_id}",
            "artifact_sha256": "b" * 64,
            "tx_channel": condition["tx_channel"],
            "center_frequency_hz": condition["center_frequency_hz"],
            "receiver_gain_db": condition["receiver_gain_db"],
        },
        "post_mute": {
            "purpose": "post_attempt",
            "status": "passed",
            "error": None,
        },
        "error": None,
    }


def _analysis_document(
    artifact_root: Path,
    condition: dict[str, Any],
    *,
    quality_passed: bool,
) -> dict[str, Any]:
    states = [
        {
            "name": f"ANT{index}",
            "quality_passed": quality_passed,
        }
        for index in range(1, 9)
    ]
    return {
        "schema": 1,
        "analysis_kind": runner.REFERENCE_ANALYSIS_KIND,
        "artifact": {
            "artifact_id": artifact_root.name,
            "path": str(artifact_root),
            "sha256": "c" * 64,
        },
        "aggregation_key": {
            "artifact_id": artifact_root.name,
            "tx_channel": condition["tx_channel"],
            "center_frequency_hz": condition["center_frequency_hz"],
            "carrier_frequency_hz": condition["center_frequency_hz"] + 100_000,
            "receiver_gain_db": condition["receiver_gain_db"],
            "sample_rate_hz": condition["sample_rate_hz"],
        },
        "quality_gate": {
            "passed": quality_passed,
            "global_rejection_reasons": [] if quality_passed else ["synthetic_rejection"],
        },
        "transfer": {"states": states},
    }


def _command_result(
    command: list[str],
    *,
    return_code: int,
    stdout: str,
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "command": command,
        "started_at": "2026-08-25T12:00:00+00:00",
        "completed_at": "2026-08-25T12:00:01+00:00",
        "return_code": return_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "error": None,
    }


def test_parser_and_configuration_persist_safe_reference_controls() -> None:
    defaults = runner._parser().parse_args([])
    selected = runner._parser().parse_args(
        (
            "--rounds",
            "2",
            "--receiver-gain-db",
            "31",
            "--stimulus",
            "qualification",
            "--timeout-s",
            "240",
            "--center-frequency-hz",
            "2400000000",
            "--center-frequency-hz",
            "2420000000",
        )
    )

    assert defaults.receiver_gain_db == 20
    assert defaults.stimulus == "phase"
    assert selected.receiver_gain_db == 31
    assert selected.stimulus == "qualification"
    assert selected.timeout_s == 240
    configuration = _configuration(receiver_gain_db=31, stimulus="qualification")
    assert configuration["receiver_gain_db"] == 31
    assert configuration["stimulus"] == "qualification"
    assert configuration["sample_rate_hz"] == 1_000_000
    assert configuration["rounds"] == 3
    assert configuration["board_id"] == "board-a"
    assert configuration["serial"] == "serial-a"
    assert configuration["uri"] == "usb:1.2.3"
    assert configuration["python"] == "/opt/capture-python"
    assert configuration["timeout_s"] == 180
    assert configuration["reference_analysis_kind"] == runner.REFERENCE_ANALYSIS_KIND
    with pytest.raises(SystemExit):
        runner._parser().parse_args(("--receiver-gain-db", "63"))
    with pytest.raises(ValueError, match="within 0..62"):
        _configuration(receiver_gain_db=-1)


def test_frequency_validation_and_reversed_rotated_adjacent_order() -> None:
    assert runner._validate_center_frequencies(FREQUENCIES) == FREQUENCIES
    with pytest.raises(ValueError, match="unique"):
        runner._validate_center_frequencies((2_400_000_000, 2_400_000_000))
    with pytest.raises(ValueError, match="integers"):
        runner._validate_center_frequencies((True,))

    plan = runner._execution_plan(_configuration(), REPOSITORY)
    conditions_per_round = 2 * len(FREQUENCIES)
    orders = [
        _condition_pairs(plan[offset : offset + conditions_per_round])
        for offset in range(0, len(plan), conditions_per_round)
    ]
    assert orders[0] == [
        (2_400_000_000, 0),
        (2_400_000_000, 1),
        (2_420_000_000, 0),
        (2_420_000_000, 1),
        (5_800_000_000, 0),
        (5_800_000_000, 1),
    ]
    assert orders[1] == [
        (5_800_000_000, 1),
        (5_800_000_000, 0),
        (2_420_000_000, 1),
        (2_420_000_000, 0),
        (2_400_000_000, 1),
        (2_400_000_000, 0),
    ]
    assert orders[2][:2] == [(2_420_000_000, 0), (2_420_000_000, 1)]
    for order in orders:
        for offset in range(0, len(order), 2):
            pair = order[offset : offset + 2]
            assert pair[0][0] == pair[1][0]
            assert {pair[0][1], pair[1][1]} == {0, 1}


def test_exact_commands_propagate_tx_frequency_gain_and_reference_analyzer() -> None:
    condition = {"center_frequency_hz": 5_800_000_000, "tx_channel": 1}
    capture = runner._capture_command(
        PYTHON,
        REPOSITORY,
        condition,
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        receiver_gain_db=27,
        stimulus="qualification",
    )
    reanalysis = runner._reanalyze_command(PYTHON, REPOSITORY, ARTIFACT_ID, "board-a")

    assert capture == [
        "/opt/capture-python",
        "/repo/scripts/capture_fast20_dwell.py",
        "--tx-channel",
        "1",
        "--stimulus",
        "qualification",
        "--receiver-gain-db",
        "27",
        "--sample-rate-hz",
        "1000000",
        "--center-frequency-hz",
        "5800000000",
        "--board-id",
        "board-a",
        "--serial",
        "serial-a",
        "--uri",
        "usb:1.2.3",
        "--allow-experimental-5g8",
    ]
    assert reanalysis == [
        "/opt/capture-python",
        "/repo/scripts/reanalyze_fast20_reference_transfer_artifact.py",
        ARTIFACT_ID,
        "--board-id",
        "board-a",
    ]
    source = SCRIPT.read_text(encoding="utf-8")
    assert "reanalyze_fast20_phase_artifact.py" not in source
    assert "analyze_fast20_phase_sensitive" not in source


def test_manifest_persists_full_plan_and_rejects_configuration_or_plan_change(
    tmp_path: Path,
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed = runner._load_manifest(path, configuration, REPOSITORY)
    assert resumed["resume_count"] == 1
    assert len(resumed["plan"]) == 2
    for condition in resumed["plan"]:
        assert condition["receiver_gain_db"] == 20
        assert condition["stimulus"] == "phase"
        assert condition["capture_command"]
        assert runner.ARTIFACT_TOKEN in condition["reference_reanalysis_command_template"]

    changed_gain = _configuration(
        rounds=1,
        frequencies=(2_400_000_000,),
        receiver_gain_db=21,
    )
    with pytest.raises(runner.ExperimentError, match="arguments do not match"):
        runner._load_manifest(path, changed_gain, REPOSITORY)

    changed_plan = json.loads(path.read_text(encoding="utf-8"))
    changed_plan["plan"][0]["capture_command"][3] = "1"
    path.write_text(json.dumps(changed_plan), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="execution plan changed"):
        runner._load_manifest(path, configuration, REPOSITORY)


def test_resume_rejects_unattested_or_duplicate_completed_attempts(tmp_path: Path) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    condition = manifest["plan"][0]
    manifest["attempts"] = [_completed_attempt(condition, attempt_id=1, quality_passed=True)]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    runner._load_manifest(path, configuration, REPOSITORY)

    malformed = json.loads(path.read_text(encoding="utf-8"))
    malformed["attempts"][0]["reanalysis"]["command"][1] = "/wrong-analyzer.py"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="not fully attested"):
        runner._load_manifest(path, configuration, REPOSITORY)

    duplicate = json.loads(json.dumps(manifest))
    second = _completed_attempt(condition, attempt_id=2, retry=1, quality_passed=True)
    duplicate["attempts"].append(second)
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="duplicate completed"):
        runner._load_manifest(path, configuration, REPOSITORY)


def test_manifest_summary_distinguishes_quality_and_execution_failures() -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000, 2_420_000_000))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    plan = manifest["plan"]
    passed = _completed_attempt(plan[0], attempt_id=1, quality_passed=True)
    rejected = _completed_attempt(plan[1], attempt_id=2, quality_passed=False)
    failed = {
        "attempt_id": 3,
        "retry": 0,
        **plan[2],
        "plan_index": 2,
        "status": "failed",
        "outcome": "execution_failed",
        "failure_kind": "execution",
    }
    manifest["attempts"] = [passed, rejected, failed]
    manifest["final_mute_attempts"] = [{"status": "passed"}]

    summary = runner._summary(manifest)

    assert summary == {
        "planned_conditions": 4,
        "execution_attempts": 3,
        "completed_conditions": 2,
        "remaining_conditions": 2,
        "quality_passed": 1,
        "quality_rejected": 1,
        "failed_attempts": 1,
        "execution_failures": 1,
        "post_mute_failures": 0,
        "final_mute_attempts": 1,
        "final_mute_passed": True,
        "distribution_quality_passed": False,
    }


def test_artifact_discovery_requires_a_fresh_identity(tmp_path: Path) -> None:
    stale = "1" * 32
    fresh = "2" * 32
    (tmp_path / stale).mkdir()
    stale_result = {"parsed_output": {"artifact_id": stale}}
    assert runner._artifact_from_capture(stale_result, {stale}, tmp_path) == (None, None)

    (tmp_path / fresh).mkdir()
    assert runner._artifact_from_capture(stale_result, {stale}, tmp_path) == (None, None)
    fresh_result = {"parsed_output": {"artifact_id": fresh}}
    assert runner._artifact_from_capture(fresh_result, {stale}, tmp_path) == (fresh, "stdout")


def test_reference_result_binds_artifact_tx_frequency_gain_and_quality(tmp_path: Path) -> None:
    condition = runner._execution_plan(
        _configuration(rounds=1, frequencies=(2_400_000_000,)), REPOSITORY
    )[0]
    artifact_root = tmp_path / ARTIFACT_ID
    artifact_root.mkdir()
    path = artifact_root / runner.REFERENCE_ANALYSIS_FILENAME
    path.write_text(
        json.dumps(_analysis_document(artifact_root, condition, quality_passed=False)),
        encoding="utf-8",
    )

    result = runner._reference_quality_result(
        artifact_root,
        condition,
        receiver_gain_db=20,
    )
    assert result["status"] == "rejected"
    assert result["artifact_id"] == ARTIFACT_ID
    assert result["tx_channel"] == 0
    assert result["center_frequency_hz"] == 2_400_000_000
    assert result["receiver_gain_db"] == 20

    changed = _analysis_document(artifact_root, condition, quality_passed=True)
    changed["aggregation_key"]["receiver_gain_db"] = 60
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="receiver gain differs"):
        runner._reference_quality_result(
            artifact_root,
            condition,
            receiver_gain_db=20,
        )


@pytest.mark.parametrize("quality_passed", [True, False])
def test_attempt_persists_stdout_stderr_artifact_quality_and_post_mute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quality_passed: bool,
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    condition = manifest["plan"][0]
    manifest_path = tmp_path / "manifest.json"
    board_root = tmp_path / "board"
    capture_root = board_root / "pluto-usb-captures"
    calls: list[list[str]] = []
    durable_plan_seen_before_capture: list[bool] = []

    monkeypatch.setattr(runner, "_board_root", lambda _board_id: board_root)

    def fake_run_command(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_s: int,
    ) -> dict[str, Any]:
        del cwd, environment, timeout_s
        calls.append(command)
        if command[1].endswith("capture_fast20_dwell.py"):
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            durable_plan_seen_before_capture.append(
                len(persisted["plan"]) == 2
                and persisted["attempts"][0]["capture"]["command"] == command
            )
            artifact_root = capture_root / ARTIFACT_ID
            artifact_root.mkdir(parents=True)
            return _command_result(
                command,
                return_code=0,
                stdout=f"capture log\n{json.dumps({'artifact_id': ARTIFACT_ID})}\n",
                stderr="capture warning\n",
            )
        artifact_root = capture_root / ARTIFACT_ID
        (artifact_root / runner.REFERENCE_ANALYSIS_FILENAME).write_text(
            json.dumps(
                _analysis_document(
                    artifact_root,
                    condition,
                    quality_passed=quality_passed,
                )
            ),
            encoding="utf-8",
        )
        return _command_result(
            command,
            return_code=0 if quality_passed else 2,
            stdout=json.dumps({"artifact_id": ARTIFACT_ID, "quality_passed": quality_passed}),
            stderr="reanalyzer diagnostic\n",
        )

    monkeypatch.setattr(runner, "_run_command", fake_run_command)
    monkeypatch.setattr(
        runner,
        "_strict_mute",
        lambda _serial, purpose: {"purpose": purpose, "status": "passed", "error": None},
    )

    runner._run_attempt(
        manifest,
        manifest_path,
        condition,
        repository=REPOSITORY,
        board_id="board-a",
        serial="serial-a",
        timeout_s=180,
        receiver_gain_db=20,
    )

    attempt = manifest["attempts"][0]
    assert attempt["status"] == "complete"
    assert attempt["outcome"] == ("quality_passed" if quality_passed else "quality_rejected")
    assert attempt["artifact_identity"]["artifact_id"] == ARTIFACT_ID
    assert attempt["capture"]["stderr"] == "capture warning\n"
    assert attempt["reanalysis"]["stderr"] == "reanalyzer diagnostic\n"
    assert attempt["quality_result"]["quality_passed"] is quality_passed
    assert attempt["post_mute"]["status"] == "passed"
    assert durable_plan_seen_before_capture == [True]
    assert "reanalyze_fast20_reference_transfer_artifact.py" in calls[1][1]
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["attempts"][0]["outcome"] == attempt["outcome"]


def test_post_attempt_mute_failure_overrides_quality_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    condition = manifest["plan"][0]
    manifest_path = tmp_path / "manifest.json"
    board_root = tmp_path / "board"
    capture_root = board_root / "pluto-usb-captures"
    monkeypatch.setattr(runner, "_board_root", lambda _board_id: board_root)

    def fake_run_command(
        command: list[str],
        **_kwargs: object,
    ) -> dict[str, Any]:
        artifact_root = capture_root / ARTIFACT_ID
        if command[1].endswith("capture_fast20_dwell.py"):
            artifact_root.mkdir(parents=True)
            return _command_result(
                command,
                return_code=0,
                stdout=json.dumps({"artifact_id": ARTIFACT_ID}),
            )
        (artifact_root / runner.REFERENCE_ANALYSIS_FILENAME).write_text(
            json.dumps(_analysis_document(artifact_root, condition, quality_passed=True)),
            encoding="utf-8",
        )
        return _command_result(
            command,
            return_code=0,
            stdout=json.dumps({"artifact_id": ARTIFACT_ID, "quality_passed": True}),
        )

    monkeypatch.setattr(runner, "_run_command", fake_run_command)
    monkeypatch.setattr(
        runner,
        "_strict_mute",
        lambda _serial, purpose: {
            "purpose": purpose,
            "status": "failed",
            "error": "readback failed",
        },
    )

    with pytest.raises(runner.ExperimentError, match="post-attempt mute failed"):
        runner._run_attempt(
            manifest,
            manifest_path,
            condition,
            repository=REPOSITORY,
            board_id="board-a",
            serial="serial-a",
            timeout_s=180,
            receiver_gain_db=20,
        )
    attempt = manifest["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["outcome"] == "post_mute_failed"
    assert attempt["failure_kind"] == "post_attempt_mute"
    assert runner._completed_plan_indices(manifest) == set()


def test_execution_failure_is_distinct_and_still_runs_post_mute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    condition = manifest["plan"][0]
    mute_purposes: list[str] = []
    monkeypatch.setattr(runner, "_board_root", lambda _board_id: tmp_path / "board")
    monkeypatch.setattr(
        runner,
        "_run_command",
        lambda command, **_kwargs: _command_result(
            command,
            return_code=1,
            stdout="",
            stderr="capture failed",
        ),
    )

    def pass_mute(_serial: str, purpose: str) -> dict[str, object]:
        mute_purposes.append(purpose)
        return {"purpose": purpose, "status": "passed", "error": None}

    monkeypatch.setattr(runner, "_strict_mute", pass_mute)
    with pytest.raises(runner.ExperimentError, match="capture returned 1"):
        runner._run_attempt(
            manifest,
            tmp_path / "manifest.json",
            condition,
            repository=REPOSITORY,
            board_id="board-a",
            serial="serial-a",
            timeout_s=180,
            receiver_gain_db=20,
        )
    attempt = manifest["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["outcome"] == "execution_failed"
    assert attempt["failure_kind"] == "execution"
    assert mute_purposes == ["post_attempt"]


def test_strict_mute_records_exact_serial_readback_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(runner, "mute_returned_radio", called.append)

    passed = runner._strict_mute("serial-a", "final")

    assert called == ["serial-a"]
    assert passed["status"] == "passed"
    assert passed["serial"] == "serial-a"
    assert passed["attestation"] == "mute_returned_radio_exact_serial_readback"

    def fail_readback(_serial: str) -> None:
        raise RuntimeError("synthetic readback failure")

    monkeypatch.setattr(runner, "mute_returned_radio", fail_readback)
    failed = runner._strict_mute("serial-a", "post_attempt")
    assert failed["status"] == "failed"
    assert "synthetic readback failure" in failed["error"]


def test_final_mute_failure_blocks_completion_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration(rounds=1, frequencies=(2_400_000_000,))
    manifest = runner._new_manifest("run-a", configuration, REPOSITORY)
    manifest["attempts"] = [
        _completed_attempt(condition, attempt_id=index + 1, quality_passed=True)
        for index, condition in enumerate(manifest["plan"])
    ]
    manifest_path = tmp_path / "manifest.json"
    results = iter(
        (
            {"purpose": "final", "status": "failed", "error": "readback failed"},
            {"purpose": "final", "status": "passed", "error": None},
        )
    )
    monkeypatch.setattr(runner, "_strict_mute", lambda _serial, _purpose: next(results))

    with pytest.raises(runner.ExperimentError, match="strict final mute failed"):
        runner._run_experiment(
            manifest,
            manifest_path,
            repository=REPOSITORY,
            board_id="board-a",
            serial="serial-a",
            timeout_s=180,
            receiver_gain_db=20,
        )
    assert manifest["status"] != "complete"
    assert manifest["summary"]["final_mute_passed"] is False

    assert (
        runner._run_experiment(
            manifest,
            manifest_path,
            repository=REPOSITORY,
            board_id="board-a",
            serial="serial-a",
            timeout_s=180,
            receiver_gain_db=20,
        )
        == 0
    )
    assert manifest["status"] == "complete"
    assert manifest["summary"]["final_mute_passed"] is True
    assert manifest["summary"]["distribution_quality_passed"] is True
