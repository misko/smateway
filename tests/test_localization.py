import numpy as np
import pytest

from smateway.localization import (
    SPEED_OF_LIGHT_M_S,
    calibrate_channel_phases,
    estimate_phase_position,
    wrap_phase_deg,
)


def _measurement(
    position: tuple[float, float],
    antennas: np.ndarray,
    offsets: np.ndarray,
    frequency_hz: float,
) -> np.ndarray:
    distances = np.linalg.norm(antennas - np.asarray(position), axis=1)
    wavelength_mm = SPEED_OF_LIGHT_M_S * 1000.0 / frequency_hz
    return wrap_phase_deg(offsets - 360.0 * (distances - distances[0]) / wavelength_mm)


def test_known_phase_calibration_recovers_target_position() -> None:
    antennas = np.asarray(
        (
            (0.0, 0.0),
            (40.0, 0.0),
            (80.0, 0.0),
            (0.0, 50.0),
            (80.0, 50.0),
            (0.0, 100.0),
            (40.0, 100.0),
            (80.0, 100.0),
        )
    )
    frequency_hz = 2_400_100_000.0
    offsets = np.asarray((0.0, 31.0, -72.0, 114.0, -151.0, 88.0, 47.0, -29.0))
    calibration_position = (210.0, 170.0)
    target_position = (-95.0, 145.0)
    calibration = calibrate_channel_phases(
        _measurement(calibration_position, antennas, offsets, frequency_hz),
        frequency_hz=frequency_hz,
        antenna_positions_mm=antennas,
        calibration_position_mm=calibration_position,
    )

    candidates = estimate_phase_position(
        _measurement(target_position, antennas, offsets, frequency_hz),
        calibration,
        bounds_mm=(-250.0, 300.0, -200.0, 300.0),
        grid_step_mm=5.0,
    )

    assert candidates
    assert candidates[0].x_mm == pytest.approx(target_position[0], abs=0.2)
    assert candidates[0].y_mm == pytest.approx(target_position[1], abs=0.2)
    assert candidates[0].rms_phase_error_deg < 0.2
