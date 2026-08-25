import importlib
import json
import sys
from pathlib import Path
from typing import cast

import pytest

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
try:
    runner = importlib.import_module("run_fast20_phase_distribution")
finally:
    sys.path.pop(0)


FREQUENCIES = (2_400_000_000, 2_420_000_000, 5_800_000_000)


def _condition_pairs(plan: list[dict[str, int | str]]) -> list[tuple[int, int]]:
    return [
        (int(condition["center_frequency_hz"]), int(condition["tx_channel"])) for condition in plan
    ]


def _configuration(
    center_frequencies_hz: tuple[int, ...] = FREQUENCIES,
) -> dict[str, object]:
    return cast(
        dict[str, object],
        runner._configuration(
            rounds=4,
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            python=Path("/opt/capture-python"),
            timeout_s=180,
            center_frequencies_hz=center_frequencies_hz,
        ),
    )


def test_repeatable_frequency_option_preserves_dual_band_default() -> None:
    parser = runner._parser()

    default_args = parser.parse_args([])
    supplied_args = parser.parse_args(
        [
            "--center-frequency-hz",
            "2400000000",
            "--center-frequency-hz",
            "2420000000",
        ]
    )

    assert runner._validate_center_frequencies(default_args.center_frequencies_hz) == (
        2_400_000_000,
        5_800_000_000,
    )
    assert _condition_pairs(runner._plan(1)) == list(runner.CONDITION_ORDER)
    assert runner._validate_center_frequencies(supplied_args.center_frequencies_hz) == (
        2_400_000_000,
        2_420_000_000,
    )


def test_frequency_validation_is_unique_integer_and_uses_exact_5g8_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classifications: list[tuple[int, bool]] = []

    def record_classification(center_frequency_hz: int, *, allow_experimental_5g8: bool) -> str:
        classifications.append((center_frequency_hz, allow_experimental_5g8))
        return "test_policy"

    original_classifier = runner.classify_fast20_center_frequency
    monkeypatch.setattr(runner, "classify_fast20_center_frequency", record_classification)
    assert runner._validate_center_frequencies(FREQUENCIES) == FREQUENCIES
    assert classifications == [
        (2_400_000_000, False),
        (2_420_000_000, False),
        (5_800_000_000, True),
    ]
    monkeypatch.setattr(runner, "classify_fast20_center_frequency", original_classifier)

    with pytest.raises(ValueError, match="unique"):
        runner._validate_center_frequencies((2_420_000_000, 2_420_000_000))
    with pytest.raises(ValueError, match="integer"):
        runner._validate_center_frequencies((True,))
    with pytest.raises(ValueError, match="exactly 5.8000 GHz"):
        runner._validate_center_frequencies((5_800_000_001,))
    with pytest.raises(ValueError, match="at least one"):
        runner._validate_center_frequencies(())


def test_round_order_pairs_transmitters_and_cycles_drift_detection_pattern() -> None:
    plan = runner._plan(4, FREQUENCIES)
    conditions_per_round = len(FREQUENCIES) * 2
    round_orders = [
        _condition_pairs(plan[offset : offset + conditions_per_round])
        for offset in range(0, len(plan), conditions_per_round)
    ]

    assert round_orders[0] == [
        (2_400_000_000, 0),
        (2_400_000_000, 1),
        (2_420_000_000, 0),
        (2_420_000_000, 1),
        (5_800_000_000, 0),
        (5_800_000_000, 1),
    ]
    assert round_orders[1] == [
        (5_800_000_000, 1),
        (5_800_000_000, 0),
        (2_420_000_000, 1),
        (2_420_000_000, 0),
        (2_400_000_000, 1),
        (2_400_000_000, 0),
    ]
    assert round_orders[2] == [
        (2_420_000_000, 0),
        (2_420_000_000, 1),
        (5_800_000_000, 1),
        (5_800_000_000, 0),
        (2_400_000_000, 0),
        (2_400_000_000, 1),
    ]
    assert round_orders[3] == round_orders[0]
    assert [int(condition["plan_index"]) for condition in plan] == list(range(len(plan)))

    for round_order in round_orders:
        for offset in range(0, len(round_order), 2):
            first, second = round_order[offset : offset + 2]
            assert first[0] == second[0]
            assert {first[1], second[1]} == {0, 1}


def test_configuration_records_frequency_and_complete_order_cycle() -> None:
    configuration = _configuration()

    assert configuration["center_frequencies_hz"] == list(FREQUENCIES)
    policies = configuration["round_order_policy"]
    assert isinstance(policies, list)
    assert [policy["pattern"] for policy in policies] == [1, 2, 3]
    assert [policy["name"] for policy in policies] == list(runner.ROUND_ORDER_POLICY)
    assert [
        (condition["center_frequency_hz"], condition["tx_channel"])
        for condition in policies[1]["conditions"]
    ] == _condition_pairs(runner._plan(2, FREQUENCIES)[6:])


def test_manifest_resume_reconstructs_the_same_persisted_plan(tmp_path: Path) -> None:
    configuration = _configuration()
    manifest = runner._new_manifest("run-a", configuration)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed = runner._load_manifest(path, configuration)

    assert resumed["plan"] == runner._plan(4, FREQUENCIES)
    assert resumed["resume_count"] == 1

    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["plan"][0]["tx_channel"] = 1
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(runner.ExperimentError, match="condition order changed"):
        runner._load_manifest(path, configuration)


def test_capture_command_opts_into_5g8_only_at_the_exact_center() -> None:
    common = {
        "python": Path("/opt/capture-python"),
        "repository": Path("/repo"),
        "board_id": "board-a",
        "serial": "serial-a",
        "uri": "usb:1.2.3",
    }
    qualified = runner._capture_command(
        common["python"],
        common["repository"],
        {"center_frequency_hz": 2_483_500_000, "tx_channel": 0},
        board_id=common["board_id"],
        serial=common["serial"],
        uri=common["uri"],
    )
    exact_5g8 = runner._capture_command(
        common["python"],
        common["repository"],
        {"center_frequency_hz": 5_800_000_000, "tx_channel": 1},
        board_id=common["board_id"],
        serial=common["serial"],
        uri=common["uri"],
    )

    assert "--allow-experimental-5g8" not in qualified
    assert exact_5g8[-1] == "--allow-experimental-5g8"
