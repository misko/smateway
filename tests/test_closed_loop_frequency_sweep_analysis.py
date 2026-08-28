import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"

BASE_SPEC = importlib.util.spec_from_file_location(
    "analyze_closed_loop_permutation",
    SCRIPT_DIRECTORY / "analyze_closed_loop_permutation.py",
)
assert BASE_SPEC is not None and BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = base
BASE_SPEC.loader.exec_module(base)

SPEC = importlib.util.spec_from_file_location(
    "closed_loop_frequency_sweep_analysis_under_test",
    SCRIPT_DIRECTORY / "analyze_closed_loop_frequency_sweep.py",
)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def _mapping(rotation: int) -> dict[str, str]:
    return {f"F{index + 1}": f"ANT{(index + rotation) % 8 + 1}" for index in range(8)}


def _manifest() -> dict[str, object]:
    configuration = {
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "frequencies_hz": list(analysis.FREQUENCIES_HZ),
        "closure_frequencies_hz": list(analysis.CLOSURE_FREQUENCIES_HZ),
        "storage_medium": "raspberry_pi_local_filesystem",
        "pluto_onboard_storage_used": False,
        "board_id": "board-a",
        "serial": "serial-a",
        "profile_id": "fast20-v1",
        "profile_contract_sha256": "a" * 64,
        "firmware_binary_sha256": "b" * 64,
    }
    plan = []
    attempts = []
    stages = (
        ("rotation0", 0, analysis.FREQUENCIES_HZ),
        ("rotation1", 1, analysis.FREQUENCIES_HZ),
        ("rotation2", 2, analysis.FREQUENCIES_HZ),
        ("closure0", 0, analysis.CLOSURE_FREQUENCIES_HZ),
    )
    for stage, rotation, frequencies in stages:
        for frequency_hz in frequencies:
            condition = {
                "plan_index": len(plan),
                "stage": stage,
                "rotation": rotation,
                "center_frequency_hz": frequency_hz,
                "mapping": _mapping(rotation),
            }
            plan.append(condition)
            attempts.append(
                {
                    **condition,
                    "status": "complete",
                    "outcome": "quality_passed",
                    "artifact_id": f"{condition['plan_index'] + 1:032x}",
                    "quality_result": {"quality_passed": True},
                }
            )
    return {
        "schema": 1,
        "experiment_kind": configuration["experiment_kind"],
        "run_id": "synthetic-broadband",
        "runner_source_commit": "c" * 40,
        "configuration": configuration,
        "plan": plan,
        "attempts": attempts,
    }


def test_canonical_manifest_preserves_all_38_frequencies_and_six_closures() -> None:
    canonical, coverage = analysis._canonical_manifest(_manifest())

    assert canonical["frequencies_hz"] == list(analysis.FREQUENCIES_HZ)
    assert len(canonical["rounds"]) == 3
    assert all(len(row["artifacts_by_frequency_hz"]) == 38 for row in canonical["rounds"])
    assert len(canonical["closure"]["artifacts_by_frequency_hz"]) == 6
    assert coverage["all_requested_frequencies_usable"] is True
    assert coverage["excluded_frequencies"] == []


def test_canonical_manifest_retains_a_quality_rejection_as_explicit_coverage_gap() -> None:
    manifest = _manifest()
    attempts = manifest["attempts"]
    assert isinstance(attempts, list)
    rejected = next(
        item
        for item in attempts
        if item["stage"] == "rotation1" and item["center_frequency_hz"] == 3_100_000_000
    )
    rejected["outcome"] = "quality_rejected"
    rejected["quality_result"] = {"quality_passed": False}

    canonical, coverage = analysis._canonical_manifest(manifest)

    assert 3_100_000_000 not in canonical["frequencies_hz"]
    assert coverage["usable_frequency_count"] == 37
    gap = next(
        row
        for row in coverage["excluded_frequencies"]
        if row["center_frequency_hz"] == 3_100_000_000
    )
    assert gap["stages"]["rotation1"] == "capture_quality_rejected"


def test_frequency_continuity_removes_relative_cyclic_branch_jumps() -> None:
    frequencies = (2_300_000_000, 2_400_000_000, 2_500_000_000)
    antenna_index = np.arange(8, dtype=np.float64)
    true_phase = np.asarray([antenna_index * value for value in (2.0, 3.0, 4.0)])
    raw = true_phase.copy()
    raw[0] += 90.0 * antenna_index
    raw[2] -= 45.0 * antenna_index

    adjusted, branches = analysis._continuity_branches(raw, frequencies)

    assert branches == [6, 0, 1]
    assert adjusted == pytest.approx(true_phase)


def test_single_delay_fit_recovers_synthetic_phase_slope() -> None:
    frequencies = np.asarray(analysis.FREQUENCIES_HZ, dtype=np.float64)
    delay_ps = 137.5
    phases = np.asarray(
        [analysis._wrap_phase_deg(23.0 + 360.0 * value * delay_ps * 1e-12) for value in frequencies]
    )

    fitted = analysis._fit_single_delay(frequencies, phases)

    assert fitted["correction_equivalent_delay_ps"] == pytest.approx(delay_ps, abs=1e-9)
    assert fitted["phase_residual_rms_deg"] < 1e-9
    assert math.isfinite(fitted["correction_equivalent_free_space_path_mm"])
