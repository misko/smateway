"""Ideal far-field and near-field steering-vector models."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from .schedule import ArrayGeometry

SPEED_OF_LIGHT_M_S = 299_792_458.0


def _frequencies(value: float | npt.ArrayLike) -> npt.NDArray[np.float64]:
    result = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(result)) or np.any(result <= 0.0):
        raise ValueError("steering frequency must be positive and finite")
    return result


def far_field_steering(
    geometry: ArrayGeometry,
    frequency_hz: float,
    bearings_deg_clockwise_from_forward: npt.ArrayLike,
) -> npt.NDArray[np.complex128]:
    """Return rows of ideal plane-wave steering vectors.

    Coordinates use +x right and +y forward. Bearing zero is forward and
    increases clockwise when viewed from above.
    """

    frequency = float(_frequencies(frequency_hz))
    bearings = np.asarray(bearings_deg_clockwise_from_forward, dtype=np.float64)
    if bearings.ndim != 1 or not np.all(np.isfinite(bearings)):
        raise ValueError("bearing grid must be a finite one-dimensional vector")
    radians = np.deg2rad(bearings)
    directions = np.column_stack((np.sin(radians), np.cos(radians)))
    phase = 2.0 * np.pi * frequency / SPEED_OF_LIGHT_M_S * (
        directions @ geometry.positions_m.T
    )
    result = np.exp(1j * phase).astype(np.complex128, copy=False)
    return np.asarray(result, dtype=np.complex128)


def near_field_steering(
    geometry: ArrayGeometry,
    frequency_hz: float,
    source_positions_m: npt.ArrayLike,
) -> npt.NDArray[np.complex128]:
    """Return spherical-wave steering rows referenced to the array centre."""

    frequency = float(_frequencies(frequency_hz))
    positions = np.asarray(source_positions_m, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 2 or not np.all(np.isfinite(positions)):
        raise ValueError("source positions must be a finite N by 2 matrix")
    ranges = np.linalg.norm(positions[:, None, :] - geometry.positions_m[None, :, :], axis=2)
    center_ranges = np.linalg.norm(positions, axis=1, keepdims=True)
    if np.any(ranges <= 0.0) or np.any(center_ranges <= 0.0):
        raise ValueError("source cannot coincide with an antenna or array centre")
    phase = -2.0 * np.pi * frequency / SPEED_OF_LIGHT_M_S * (ranges - center_ranges)
    # Include ideal spherical spreading. The snapshot-common centre range is a
    # harmless gauge, but relative 1/r amplitude can aid a near-field solve.
    magnitude = center_ranges / ranges
    result = (magnitude * np.exp(1j * phase)).astype(np.complex128, copy=False)
    return np.asarray(result, dtype=np.complex128)
