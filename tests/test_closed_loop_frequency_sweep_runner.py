import importlib.util
import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"

BASE_SPEC = importlib.util.spec_from_file_location(
    "run_fast20_reference_distribution",
    SCRIPT_DIRECTORY / "run_fast20_reference_distribution.py",
)
assert BASE_SPEC is not None and BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = base
BASE_SPEC.loader.exec_module(base)

SPEC = importlib.util.spec_from_file_location(
    "closed_loop_frequency_sweep_under_test",
    SCRIPT_DIRECTORY / "run_closed_loop_frequency_sweep.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _configuration() -> dict[str, object]:
    return runner._configuration(
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        python=Path("/opt/pluto-python"),
        receiver_gain_db=40,
        timeout_s=180,
    )


def test_frequency_grid_and_stage_plan_are_exact() -> None:
    assert len(runner.FREQUENCIES_HZ) == 38
    assert runner.FREQUENCIES_HZ[0] == 2_100_000_000
    assert runner.FREQUENCIES_HZ[-1] == 5_800_000_000
    assert all(
        right - left == 100_000_000
        for left, right in zip(runner.FREQUENCIES_HZ, runner.FREQUENCIES_HZ[1:], strict=False)
    )

    plan = runner._execution_plan(_configuration(), Path("/repo"))

    assert len(plan) == 3 * 38 + 6
    assert [item["stage"] for item in plan].count("rotation0") == 38
    assert [item["stage"] for item in plan].count("rotation1") == 38
    assert [item["stage"] for item in plan].count("rotation2") == 38
    assert [item["stage"] for item in plan].count("closure0") == 6
    assert [item["plan_index"] for item in plan] == list(range(120))


def test_configuration_records_pi_local_storage_and_excludes_pluto_storage() -> None:
    configuration = _configuration()

    assert configuration["storage_medium"] == "raspberry_pi_local_filesystem"
    assert configuration["board_state_root"] == ("/home/pi/.local/state/smateway/boards/board-a")
    assert configuration["artifact_storage_root"] == (
        "/home/pi/.local/state/smateway/boards/board-a/pluto-usb-captures"
    )
    assert configuration["pluto_onboard_storage_used"] is False
    assert configuration["profile_id"] == "fast20-v1"
    assert len(configuration["profile_contract_sha256"]) == 64
    assert len(configuration["firmware_binary_sha256"]) == 64
    assert configuration["planned_capture_count"] == 120
    assert configuration["estimated_raw_iq_bytes"] == 9_600_000_000


def test_capture_commands_are_tx1_only_and_explicitly_conducted() -> None:
    plan = runner._execution_plan(_configuration(), Path("/repo"))

    for condition in plan:
        command = condition["capture_command"]
        assert command[command.index("--tx-channel") + 1] == "0"
        assert "--allow-conducted-calibration-sweep" in command
        assert "--confirm-fully-conducted" in command
        fixture_index = command.index("--conducted-fixture-id")
        assert command[fixture_index + 1] == runner.CONDUCTED_FIXTURE_ID


def test_cyclic_mappings_and_normal_closure_are_explicit() -> None:
    assert runner._mapping(0) == {f"F{i}": f"ANT{i}" for i in range(1, 9)}
    assert runner._mapping(1)["F8"] == "ANT1"
    assert runner._mapping(2)["F7"] == "ANT1"

    plan = runner._execution_plan(_configuration(), Path("/repo"))
    closure = [item for item in plan if item["stage"] == "closure0"]
    assert all(item["rotation"] == 0 for item in closure)
    assert all(item["mapping"] == runner._mapping(0) for item in closure)


def test_stage_prerequisites_reject_skipping_physical_mapping() -> None:
    manifest = {
        "plan": runner._execution_plan(_configuration(), Path("/repo")),
        "attempts": [],
    }

    try:
        runner._check_stage_prerequisites(manifest, "rotation1")
    except runner.ConductedSweepError as error:
        assert "rotation0" in str(error)
    else:
        raise AssertionError("rotation1 was allowed before rotation0")
