import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "comprehensive", ROOT / "scripts/run_comprehensive_block.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_each_dwell_has_three_independent_rounds_and_separate_controls():
    rows = MODULE.schedule([200, 1000], 42)
    assert rows == MODULE.schedule([200, 1000], 42)
    assert len(rows) == 9
    for round_number in (1, 2, 3):
        group = [r for r in rows if r["round"] == round_number]
        assert len(group) == 3
        assert sum(r["control"] for r in group) == 1
    with pytest.raises(ValueError):
        MODULE.schedule([20000], 42)


@pytest.mark.parametrize("configuration", ["B", "D"])
def test_rate_screen_retains_independent_A_baseline_and_long_controls(configuration):
    rows = MODULE.schedule([25, 50, 100, 200, 1000], 42, configuration)
    assert len(rows) == 21
    for number in (1, 2, 3):
        group = [r for r in rows if r["round"] == number]
        controls = [r for r in group if r["control"]]
        assert {r["dwell_us"] for r in controls} == {200, 1000}
        assert {r["configuration"] for r in controls} == {"A"}
        assert {r["configuration"] for r in group if not r["control"]} == {configuration}


def test_unqualified_10MS_rate_is_not_admitted():
    with pytest.raises(ValueError, match="continuity"):
        MODULE.schedule([200], 42, "C")


@pytest.mark.parametrize("error", [RuntimeError("capture"), KeyboardInterrupt()])
def test_capture_failure_and_interrupt_restore_original_backup(error):
    restored = []

    def acquire(_row, _flash):
        raise error

    with pytest.raises(type(error)):
        MODULE.run_switched(
            [{"dwell_us": 200}],
            flash=lambda _d: "original",
            acquire=acquire,
            restore=restored.append,
        )
    assert restored == ["original"]


def test_multiple_profiles_restore_first_backup_not_last():
    restored = []
    MODULE.run_switched(
        [{"dwell_us": 200}, {"dwell_us": 1000}],
        flash=lambda d: str(d),
        acquire=lambda _r, _f: None,
        restore=restored.append,
    )
    assert restored == ["200"]


def test_explicit_protocol_is_admitted_and_forwarded_to_capture(monkeypatch, tmp_path):
    protocol = tmp_path / "single-frequency.json"
    admitted, commands = [], []

    def admit(path, frequency, **_kwargs):
        admitted.append((path, frequency))
        return {}

    def capture(command):
        commands.append(command)
        raise RuntimeError("test stops before hardware")

    monkeypatch.setattr(MODULE, "admit_capture", admit)
    monkeypatch.setattr(MODULE, "capture", capture)
    monkeypatch.setattr(MODULE, "schedule", lambda *_args: [])
    monkeypatch.setattr(sys, "argv", [
        "run_comprehensive_block.py", "--output-root", str(tmp_path / "captures"),
        "--fixture-json", str(tmp_path / "fixture.json"),
        "--protocol-json", str(protocol), "--frequency-hz", "915000000", "--gain-db", "50",
        "--acknowledge-ota-authorization", "--acknowledge-selector-flash",
    ])
    with pytest.raises(RuntimeError, match="before hardware"):
        MODULE.main()
    assert admitted == [(protocol, 915_000_000)]
    assert commands[0][commands[0].index("--protocol-json") + 1] == str(protocol)
