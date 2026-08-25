"""Calibrated two-dimensional phase localization for the eight-way array."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

SPEED_OF_LIGHT_M_S = 299_792_458.0


@dataclass(frozen=True, slots=True)
class PhaseCalibration:
    """Per-antenna phase offsets measured from one known emitter position."""

    frequency_hz: float
    antenna_positions_mm: npt.NDArray[np.float64]
    calibration_position_mm: tuple[float, float]
    channel_offset_deg: npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class PositionCandidate:
    """One wrapped-phase position solution."""

    x_mm: float
    y_mm: float
    rms_phase_error_deg: float


def wrap_phase_deg(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Wrap degrees into [-180, 180)."""

    array = np.asarray(values, dtype=np.float64)
    return (array + 180.0) % 360.0 - 180.0


def load_antenna_positions(path: Path) -> npt.NDArray[np.float64]:
    """Load ANT1..ANT8 nominal vertical-axis coordinates from a profile file."""

    document = json.loads(path.read_text(encoding="utf-8"))
    antennas = document.get("antennas")
    if not isinstance(antennas, list) or len(antennas) != 8:
        raise ValueError("array geometry must contain exactly eight antennas")
    expected_names = [f"ANT{index}" for index in range(1, 9)]
    names = [item.get("name") for item in antennas if isinstance(item, dict)]
    if names != expected_names:
        raise ValueError("array geometry must be ordered ANT1 through ANT8")
    positions = np.asarray([item["vertical_axis_mm"] for item in antennas], dtype=np.float64)
    if positions.shape != (8, 2) or not np.all(np.isfinite(positions)):
        raise ValueError("antenna coordinates must be a finite 8 by 2 matrix")
    return positions


def _phases(
    position_mm: npt.NDArray[np.float64],
    antenna_positions_mm: npt.NDArray[np.float64],
    *,
    frequency_hz: float,
    channel_offset_deg: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    distances_mm = np.linalg.norm(antenna_positions_mm - position_mm, axis=1)
    distance_difference_mm = distances_mm - distances_mm[0]
    wavelength_mm = SPEED_OF_LIGHT_M_S * 1000.0 / frequency_hz
    return wrap_phase_deg(
        channel_offset_deg - 360.0 * distance_difference_mm / wavelength_mm
    )


def calibrate_channel_phases(
    measured_phase_deg: npt.ArrayLike,
    *,
    frequency_hz: float,
    antenna_positions_mm: npt.ArrayLike,
    calibration_position_mm: tuple[float, float],
) -> PhaseCalibration:
    """Derive fixed ANT-relative phase offsets from a known source position."""

    measured = np.asarray(measured_phase_deg, dtype=np.float64)
    antennas = np.asarray(antenna_positions_mm, dtype=np.float64)
    position = np.asarray(calibration_position_mm, dtype=np.float64)
    if measured.shape != (8,) or antennas.shape != (8, 2) or position.shape != (2,):
        raise ValueError("calibration requires eight phases, eight 2-D antennas and one point")
    if not np.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("calibration frequency must be positive and finite")
    if not np.all(np.isfinite(measured)) or not np.all(np.isfinite(antennas)):
        raise ValueError("calibration inputs must be finite")
    distances_mm = np.linalg.norm(antennas - position, axis=1)
    wavelength_mm = SPEED_OF_LIGHT_M_S * 1000.0 / frequency_hz
    offsets = wrap_phase_deg(
        measured + 360.0 * (distances_mm - distances_mm[0]) / wavelength_mm
    )
    return PhaseCalibration(
        frequency_hz=float(frequency_hz),
        antenna_positions_mm=antennas.copy(),
        calibration_position_mm=(float(position[0]), float(position[1])),
        channel_offset_deg=offsets,
    )


def _cost(
    position_mm: npt.NDArray[np.float64],
    measured_phase_deg: npt.NDArray[np.float64],
    calibration: PhaseCalibration,
) -> float:
    predicted = _phases(
        position_mm,
        calibration.antenna_positions_mm,
        frequency_hz=calibration.frequency_hz,
        channel_offset_deg=calibration.channel_offset_deg,
    )
    residual = wrap_phase_deg(predicted[1:] - measured_phase_deg[1:])
    return float(np.sqrt(np.mean(residual**2)))


def _refine(
    seed: npt.NDArray[np.float64],
    measured: npt.NDArray[np.float64],
    calibration: PhaseCalibration,
    bounds_mm: tuple[float, float, float, float],
    initial_step_mm: float,
) -> PositionCandidate:
    x0, x1, y0, y1 = bounds_mm
    point = seed.copy()
    value = _cost(point, measured, calibration)
    step = initial_step_mm
    directions = np.asarray(
        ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)),
        dtype=np.float64,
    )
    while step >= 0.05:
        improved = False
        for direction in directions:
            candidate = point + direction * step
            if not x0 <= candidate[0] <= x1 or not y0 <= candidate[1] <= y1:
                continue
            candidate_value = _cost(candidate, measured, calibration)
            if candidate_value < value:
                point = candidate
                value = candidate_value
                improved = True
        if not improved:
            step /= 2.0
    return PositionCandidate(float(point[0]), float(point[1]), value)


def estimate_phase_position(
    measured_phase_deg: npt.ArrayLike,
    calibration: PhaseCalibration,
    *,
    bounds_mm: tuple[float, float, float, float],
    grid_step_mm: float = 5.0,
    maximum_candidates: int = 8,
    candidate_separation_mm: float = 20.0,
) -> tuple[PositionCandidate, ...]:
    """Search and refine distinct wrapped-phase position hypotheses."""

    measured = np.asarray(measured_phase_deg, dtype=np.float64)
    if measured.shape != (8,) or not np.all(np.isfinite(measured)):
        raise ValueError("position solving requires eight finite phases")
    x0, x1, y0, y1 = bounds_mm
    if x1 <= x0 or y1 <= y0 or grid_step_mm <= 0 or maximum_candidates < 1:
        raise ValueError("position bounds and search settings are invalid")
    xs = np.arange(x0, x1 + grid_step_mm * 0.25, grid_step_mm)
    ys = np.arange(y0, y1 + grid_step_mm * 0.25, grid_step_mm)
    coarse: list[tuple[float, float, float]] = []
    for y in ys:
        points = np.column_stack((xs, np.full(xs.size, y)))
        distances = np.linalg.norm(
            points[:, None, :] - calibration.antenna_positions_mm[None, :, :], axis=2
        )
        wavelength_mm = SPEED_OF_LIGHT_M_S * 1000.0 / calibration.frequency_hz
        predicted = wrap_phase_deg(
            calibration.channel_offset_deg[None, :]
            - 360.0 * (distances - distances[:, [0]]) / wavelength_mm
        )
        residual = wrap_phase_deg(predicted[:, 1:] - measured[None, 1:])
        costs = np.sqrt(np.mean(residual**2, axis=1))
        keep = min(maximum_candidates * 8, costs.size)
        indices = np.argpartition(costs, keep - 1)[:keep]
        coarse.extend((float(costs[index]), float(xs[index]), float(y)) for index in indices)
    refined = [
        _refine(
            np.asarray((x, y), dtype=np.float64),
            measured,
            calibration,
            bounds_mm,
            grid_step_mm,
        )
        for _, x, y in sorted(coarse)[: maximum_candidates * 16]
    ]
    distinct: list[PositionCandidate] = []
    for candidate in sorted(refined, key=lambda item: item.rms_phase_error_deg):
        if all(
            np.hypot(candidate.x_mm - prior.x_mm, candidate.y_mm - prior.y_mm)
            >= candidate_separation_mm
            for prior in distinct
        ):
            distinct.append(candidate)
        if len(distinct) == maximum_candidates:
            break
    return tuple(distinct)
