from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_broadband_cohort_comparison.py"
SPEC = importlib.util.spec_from_file_location("broadband_cohort_comparison_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def _phasor(fit: dict[str, object]) -> np.ndarray:
    return np.asarray(fit["predicted_real"]) + 1j * np.asarray(fit["predicted_imag"])


def test_log_harmonic_fit_recovers_synthetic_one_ripple() -> None:
    frequency_ghz = np.asarray(analyzer.FREQUENCIES_HZ) / 1e9
    delta_frequency_ghz = frequency_ghz - np.mean(frequency_ghz)
    ripple = 0.23 - 0.17j
    measured_log = (
        0.12
        + 1j * (0.4 - 2.0 * np.pi * delta_frequency_ghz * 0.08)
        + ripple * np.exp(-2j * np.pi * delta_frequency_ghz * 0.64)
    )
    measured = np.exp(measured_log)

    fit = analyzer.fit_log_harmonic_model(measured, 1)

    assert fit["parameter_count"] == 6
    assert fit["ripple_delay_ns"] == pytest.approx(0.64, abs=0.001)
    assert np.max(np.abs(_phasor(fit) - measured)) < 1e-10
    assert fit["training_phase_rms_deg"] < 1e-9
    assert fit["training_gain_rms_db"] < 1e-9


def test_exact_single_echo_fit_recovers_synthetic_response() -> None:
    frequency_ghz = np.asarray(analyzer.FREQUENCIES_HZ) / 1e9
    delta_frequency_ghz = frequency_ghz - np.mean(frequency_ghz)
    base = 0.9 + 0.2j
    echo = 0.18 - 0.11j
    measured = base * np.exp(-2j * np.pi * delta_frequency_ghz * 0.12) + echo * np.exp(
        -2j * np.pi * delta_frequency_ghz * (0.12 + 0.64)
    )

    fit = analyzer.fit_exact_single_echo_model(measured)

    assert fit["parameter_count"] == 6
    assert fit["base_delay_ns"] == pytest.approx(0.12, abs=0.0011)
    assert fit["echo_delay_ns"] == pytest.approx(0.64, abs=0.0011)
    assert fit["echo_relative_magnitude"] == pytest.approx(abs(echo / base), abs=1e-6)
    assert np.max(np.abs(_phasor(fit) - measured)) < 1e-10


def _synthetic_run(run_id: str, coefficient_phase_deg: float) -> dict[str, object]:
    coefficient = np.exp(1j * math.radians(coefficient_phase_deg))
    ratio = 1.0 / coefficient
    transfer: dict[tuple[int, str], complex] = {}
    for frequency in analyzer.FREQUENCIES_HZ:
        transfer[(frequency, "ALL_OFF")] = 0.0j
        transfer[(frequency, "ANT8")] = 1.0 + 0.0j
        for state in analyzer.MODEL_STATES:
            transfer[(frequency, state)] = ratio
    return {"run_id": run_id, "transfer": transfer}


def test_cohort_shift_uses_circular_phase_difference() -> None:
    original = [_synthetic_run("old", 179.0)]
    future = [_synthetic_run("new", -179.0)]

    result = analyzer._cohort_mean_shift(original, future)

    assert result["absolute_phase_delta_deg"]["maximum"] == pytest.approx(2.0)
    assert result["absolute_gain_delta_db"]["maximum"] == pytest.approx(0.0, abs=1e-12)


def test_pinned_future_cohort_excludes_intervening_run() -> None:
    assert analyzer.EXCLUDED_INTERVENING_RUN_ID not in analyzer.FUTURE_RUN_SHA256
    assert tuple(sorted(analyzer.FUTURE_RUN_SHA256)) == (
        "20260830T233718.194719Z",
        "20260830T235102.223267Z",
        "20260831T000448.252130Z",
        "20260831T001833.322413Z",
        "20260831T003219.696398Z",
    )
