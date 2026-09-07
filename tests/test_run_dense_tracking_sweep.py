from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_dense_tracking_sweep.py"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location("run_dense_tracking_sweep", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dense_grid_is_inclusive_and_reverses_between_passes() -> None:
    module = _load_script()
    module._validate_grid(5_726_000_000, 5_728_000_000, 1_000_000, 2)

    assert module._frequency_order(5_726_000_000, 5_728_000_000, 1_000_000, 0) == (
        5_726_000_000,
        5_727_000_000,
        5_728_000_000,
    )
    assert module._frequency_order(5_726_000_000, 5_728_000_000, 1_000_000, 1) == (
        5_728_000_000,
        5_727_000_000,
        5_726_000_000,
    )


@pytest.mark.parametrize(
    ("start_hz", "stop_hz", "step_hz"),
    [
        (5_725_000_000, 5_874_000_000, 1_000_000),
        (5_726_000_000, 5_875_000_000, 1_000_000),
        (5_726_050_000, 5_874_000_000, 1_000_000),
        (5_726_000_000, 5_874_000_000, 50_000),
        (5_726_000_000, 5_874_000_000, 1_100_000),
    ],
)
def test_dense_grid_rejects_edges_and_non_lattice_values(
    start_hz: int,
    stop_hz: int,
    step_hz: int,
) -> None:
    module = _load_script()

    with pytest.raises(ValueError, match="100 kHz-aligned"):
        module._validate_grid(start_hz, stop_hz, step_hz, 1)


def test_condition_keys_bind_pass_frequency_and_source() -> None:
    module = _load_script()

    assert module._condition_key(1, 5_800_000_000, 1) == "pass2:f5800000000:tx2"
