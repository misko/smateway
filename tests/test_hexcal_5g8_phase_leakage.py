from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_hexcal_5g8_phase_leakage.py"
SPEC = importlib.util.spec_from_file_location("hexcal_5g8_phase_leakage_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def test_phase_wrap_and_circular_summary_cross_the_branch_cut() -> None:
    assert analysis._wrap_phase_deg(181.0) == -179.0
    summary = analysis._circular_summary((179.0, -179.0))
    assert abs(abs(summary["mean_deg"]) - 180.0) < 1e-9
    assert summary["circular_std_deg"] == pytest.approx(1.0, abs=0.01)
    assert abs(summary["maximum_pair_delta_deg"]) == pytest.approx(2.0)


def test_phase_only_alignment_recovers_leakage_subtracted_state_phases() -> None:
    folded = np.full(1_500, 100.0 + 20.0j, dtype=np.complex128)
    phases = np.deg2rad((0.0, 60.0, 120.0, 180.0, -120.0, -60.0))
    for (start, stop), phase in zip(analysis.STATE_WINDOWS, phases, strict=True):
        folded[start:stop] += 10.0 * np.exp(1j * phase)
    result = analysis.phase_only_alignment(folded)

    assert result["cycle_start_offset_nominal_us"] == 0
    assert result["guard_alignment_score"] > 1e10
    assert result["leakage_subtracted_phase_deg_relative_to_ant1"] == pytest.approx(
        (0.0, 60.0, 120.0, -180.0, -120.0, -60.0),
        abs=1e-9,
    )
    assert all(math.isclose(value, 1.0) for value in result["leakage_subtracted_normalized_magnitude"])
