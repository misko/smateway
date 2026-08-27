import cmath
import math

import pytest

from smateway.permutation_calibration import (
    PermutationCalibrationError,
    PermutationObservation,
    coherent_leakage_phase_bound_deg,
    compare_rotation_zero_closure,
    fit_separable_paths,
    wrap_phase_deg,
)


def _phasor(gain_db: float, phase_deg: float) -> complex:
    return 10.0 ** (gain_db / 20.0) * cmath.exp(1j * math.radians(phase_deg))


def _synthetic_observations(interaction_phase_deg: float = 0.0) -> list[PermutationObservation]:
    feed_gain = (0.0, 0.2, -0.4, 0.8, -0.6, 0.1, 0.5, -0.3)
    feed_phase = (0.0, 145.0, -160.0, 73.0, -101.0, 32.0, -49.0, 179.0)
    board_gain = (0.4, -0.9, 0.1, 1.2, -1.5, 0.3, -0.2, 0.7)
    board_phase = (112.0, -174.0, 49.0, -92.0, 176.0, -38.0, 81.0, -151.0)
    round_gain = (0.0, -0.3, 0.6)
    # The conducted fixture keeps RX1 continuously connected, so capture-common
    # phase is a small drift rather than an arbitrary per-reconnect angle.
    round_phase = (0.0, 2.0, -3.0)
    observations = []
    for rotation in range(3):
        for feed_index in range(8):
            antenna_index = (feed_index + rotation) % 8
            interaction = interaction_phase_deg if (rotation, feed_index) == (2, 6) else 0.0
            observations.append(
                PermutationObservation(
                    rotation=rotation,
                    feed=f"F{feed_index + 1}",
                    antenna=f"ANT{antenna_index + 1}",
                    transfer=_phasor(
                        round_gain[rotation] + feed_gain[feed_index] + board_gain[antenna_index],
                        round_phase[rotation]
                        + feed_phase[feed_index]
                        + board_phase[antenna_index]
                        + interaction,
                    ),
                )
            )
    return observations


def test_separable_fit_recovers_wrapped_board_and_feed_terms() -> None:
    result = fit_separable_paths(_synthetic_observations())

    quality = result["fit_quality"]
    assert isinstance(quality, dict)
    assert quality["amplitude_residual_rms_db"] == pytest.approx(0.0, abs=1e-11)
    assert quality["phase_residual_rms_deg"] == pytest.approx(0.0, abs=1e-9)
    board = result["board_path_terms"]
    assert isinstance(board, list)
    expected_board_gain = (0.0, -1.3, -0.3, 0.8, -1.9, -0.1, -0.6, 0.3)
    expected_board_phase = (0.0, 74.0, -63.0, 156.0, 64.0, -150.0, -31.0, 97.0)
    for row, gain, phase in zip(board, expected_board_gain, expected_board_phase, strict=True):
        assert row["response_gain_db_relative_to_ant1"] == pytest.approx(gain, abs=1e-10)
        assert wrap_phase_deg(row["response_phase_deg_relative_to_ant1"] - phase) == pytest.approx(
            0.0, abs=1e-9
        )
        assert row["correction_gain_db"] == pytest.approx(-gain, abs=1e-10)
        assert wrap_phase_deg(row["correction_phase_deg"] + phase) == pytest.approx(
            0.0, abs=1e-9
        )


def test_nonseparable_interaction_appears_in_residual() -> None:
    result = fit_separable_paths(_synthetic_observations(interaction_phase_deg=12.0))

    quality = result["fit_quality"]
    assert isinstance(quality, dict)
    assert float(quality["phase_residual_rms_deg"]) > 1.0
    assert float(quality["phase_residual_max_abs_deg"]) > 3.0


def test_rank_and_rotation_coverage_are_enforced() -> None:
    with pytest.raises(PermutationCalibrationError, match="three unique rotations"):
        fit_separable_paths(_synthetic_observations()[:16])


def test_closure_removes_common_gain_and_phase() -> None:
    initial = [_phasor(index * 0.2, index * 17.0) for index in range(8)]
    closure = [value * _phasor(-0.7, 83.0) for value in initial]

    result = compare_rotation_zero_closure(initial, closure)

    assert result["common_gain_change_db"] == pytest.approx(-0.7, abs=1e-12)
    assert result["common_phase_change_deg"] == pytest.approx(83.0, abs=1e-12)
    assert result["relative_shape_gain_rms_db"] == pytest.approx(0.0, abs=1e-12)
    assert result["relative_shape_phase_rms_deg"] == pytest.approx(0.0, abs=1e-12)


def test_coherent_leakage_phase_bound_reports_unbounded_case() -> None:
    assert coherent_leakage_phase_bound_deg(40.0) == pytest.approx(0.572967, rel=1e-5)
    assert coherent_leakage_phase_bound_deg(0.0) is None
    assert coherent_leakage_phase_bound_deg(-2.0) is None
