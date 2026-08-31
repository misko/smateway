from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_broadband_midpoint_campaign.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("broadband_midpoint_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def test_midpoint_lattice_is_exactly_interstitial() -> None:
    assert len(analyzer.MIDPOINT_FREQUENCIES_HZ) == 37
    assert analyzer.MIDPOINT_FREQUENCIES_HZ[0] == 2_150_000_000
    assert analyzer.MIDPOINT_FREQUENCIES_HZ[-1] == 5_750_000_000
    assert set(analyzer.MIDPOINT_FREQUENCIES_HZ).isdisjoint(analyzer.cohort.FREQUENCIES_HZ)
    assert all(
        right - left == 100_000_000
        for left, right in zip(
            analyzer.MIDPOINT_FREQUENCIES_HZ,
            analyzer.MIDPOINT_FREQUENCIES_HZ[1:],
            strict=False,
        )
    )


def test_harmonic_model_predicts_unseen_synthetic_midpoints() -> None:
    training_frequency_ghz = np.asarray(analyzer.cohort.FREQUENCIES_HZ) / 1e9
    midpoint_frequency_ghz = np.asarray(analyzer.MIDPOINT_FREQUENCIES_HZ) / 1e9
    center_ghz = np.mean(training_frequency_ghz)
    ripple = 0.21 - 0.14j

    def response(frequency_ghz: np.ndarray) -> np.ndarray:
        delta = frequency_ghz - center_ghz
        logged = (
            0.08
            + 1j * (0.3 - 2.0 * np.pi * delta * 0.09)
            + ripple * np.exp(-2j * np.pi * delta * 0.64)
        )
        return np.exp(logged)

    prediction, fit = analyzer.fit_harmonic_predict(
        response(training_frequency_ghz), analyzer.MIDPOINT_FREQUENCIES_HZ, 1
    )

    assert fit["parameter_count"] == 6
    assert fit["ripple_delay_ns"] == pytest.approx(0.64, abs=0.001)
    assert np.max(np.abs(prediction - response(midpoint_frequency_ghz))) < 1e-10


def test_exact_echo_model_predicts_unseen_synthetic_midpoints() -> None:
    training_frequency_ghz = np.asarray(analyzer.cohort.FREQUENCIES_HZ) / 1e9
    midpoint_frequency_ghz = np.asarray(analyzer.MIDPOINT_FREQUENCIES_HZ) / 1e9
    center_ghz = np.mean(training_frequency_ghz)
    base = 0.9 + 0.15j
    echo = 0.19 - 0.08j

    def response(frequency_ghz: np.ndarray) -> np.ndarray:
        delta = frequency_ghz - center_ghz
        return base * np.exp(-2j * np.pi * delta * 0.12) + echo * np.exp(
            -2j * np.pi * delta * (0.12 + 0.64)
        )

    prediction, fit = analyzer.fit_exact_echo_predict(
        response(training_frequency_ghz), analyzer.MIDPOINT_FREQUENCIES_HZ
    )

    assert fit["parameter_count"] == 6
    assert fit["base_delay_ns"] == pytest.approx(0.12, abs=0.0011)
    assert fit["echo_delay_ns"] == pytest.approx(0.64, abs=0.0011)
    assert np.max(np.abs(prediction - response(midpoint_frequency_ghz))) < 1e-10


def _synthetic_midpoint_run(
    prediction: dict[str, np.ndarray], *, phase_error_deg: float, gain_error_db: float
) -> dict[str, object]:
    error = 10.0 ** (gain_error_db / 20.0) * np.exp(1j * math.radians(phase_error_deg))
    transfer: dict[tuple[int, str], complex] = {}
    for frequency_index, frequency in enumerate(analyzer.MIDPOINT_FREQUENCIES_HZ):
        transfer[(frequency, "ALL_OFF")] = 0.0j
        transfer[(frequency, "ANT8")] = 1.0 + 0.0j
        for state in analyzer.cohort.MODEL_STATES:
            transfer[(frequency, state)] = prediction[state][frequency_index] * error
    return {"run_id": "synthetic", "transfer": transfer}


def test_prediction_score_preserves_phase_and_gain_sign_convention() -> None:
    prediction = {
        state: np.ones(len(analyzer.MIDPOINT_FREQUENCIES_HZ), dtype=np.complex128)
        for state in analyzer.cohort.MODEL_STATES
    }
    run = _synthetic_midpoint_run(prediction, phase_error_deg=2.0, gain_error_db=0.1)

    score = analyzer._score_predictions(prediction, [run])

    assert score["phase_rms_deg"] == pytest.approx(2.0)
    assert score["gain_rms_db"] == pytest.approx(0.1)
    assert score["maximum_absolute_phase_error_deg"] == pytest.approx(2.0)
    assert score["maximum_absolute_gain_error_db"] == pytest.approx(0.1)


def test_exact_five_run_ids_are_pinned() -> None:
    assert tuple(sorted(analyzer.MIDPOINT_RUN_SHA256)) == (
        "20260831T012101.150426Z",
        "20260831T013428.712215Z",
        "20260831T014754.522922Z",
        "20260831T020127.934844Z",
        "20260831T021459.216318Z",
    )
