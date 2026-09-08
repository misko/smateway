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
