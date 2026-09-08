import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "headroom", Path(__file__).resolve().parents[1] / "scripts/screen_comprehensive_headroom.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "peaks,clips,status,mute,expected",
    [
        ([1599, 42], [0, 0], "passed", True, True),
        ([1600, 42], [0, 0], "passed", True, False),
        ([42, 2048], [0, 1], "failed", True, False),
        ([42, 43], [0, 0], "passed", False, False),
        ([], [], "failed", True, False),
        ([42, float("nan")], [0, 0], "passed", True, False),
    ],
)
def test_headroom_requires_both_channels_and_exact_cleanup(peaks, clips, status, mute, expected):
    run = {
        "status": status,
        "capture": {"peak_component_counts": peaks, "clipped_samples": clips},
        "safety": {"final_source_mute": {"passed": mute}},
    }
    assert MODULE.headroom_pass(run) is expected
