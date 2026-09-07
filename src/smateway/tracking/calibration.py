"""Immutable board-LUT loading and log-magnitude/unwrapped-phase interpolation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt


def _readonly(values: npt.ArrayLike, *, dtype: npt.DTypeLike) -> np.ndarray:
    result = np.asarray(values, dtype=dtype).copy()
    result.setflags(write=False)
    return result


def _pchip_derivatives(
    x: npt.NDArray[np.float64],
    values: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    if x.ndim != 1 or values.ndim != 1 or x.size != values.size or x.size < 3:
        raise ValueError("PCHIP requires equal one-dimensional arrays with at least three points")
    spacing = np.diff(x)
    if np.any(spacing <= 0.0):
        raise ValueError("PCHIP knots must be strictly increasing")
    slopes = np.diff(values) / spacing
    derivatives = np.zeros_like(values)
    for index in range(1, values.size - 1):
        left = slopes[index - 1]
        right = slopes[index]
        if left * right > 0.0:
            weight_one = 2.0 * spacing[index] + spacing[index - 1]
            weight_two = spacing[index] + 2.0 * spacing[index - 1]
            derivatives[index] = (weight_one + weight_two) / (
                weight_one / left + weight_two / right
            )

    def endpoint(h0: float, h1: float, slope0: float, slope1: float) -> float:
        derivative = ((2.0 * h0 + h1) * slope0 - h0 * slope1) / (h0 + h1)
        if derivative * slope0 <= 0.0:
            return 0.0
        if slope0 * slope1 < 0.0 and abs(derivative) > 3.0 * abs(slope0):
            return 3.0 * slope0
        return derivative

    derivatives[0] = endpoint(spacing[0], spacing[1], slopes[0], slopes[1])
    derivatives[-1] = endpoint(spacing[-1], spacing[-2], slopes[-1], slopes[-2])
    return derivatives


def pchip_interpolate(
    x: npt.ArrayLike,
    values: npt.ArrayLike,
    query: npt.ArrayLike,
) -> npt.NDArray[np.float64]:
    """Evaluate a shape-preserving cubic Hermite interpolator without extrapolation."""

    knots = np.asarray(x, dtype=np.float64)
    ordinates = np.asarray(values, dtype=np.float64)
    requested = np.asarray(query, dtype=np.float64)
    if not np.all(np.isfinite(knots)) or not np.all(np.isfinite(ordinates)):
        raise ValueError("PCHIP inputs must be finite")
    if not np.all(np.isfinite(requested)):
        raise ValueError("PCHIP query must be finite")
    if np.any(requested < knots[0]) or np.any(requested > knots[-1]):
        raise ValueError("PCHIP extrapolation is forbidden")

    # Normalize the abscissa so GHz-scale values do not unnecessarily reduce
    # derivative precision.
    span = knots[-1] - knots[0]
    if not np.isfinite(span) or span <= 0.0:
        raise ValueError("PCHIP support is invalid")
    normalized = (knots - knots[0]) / span
    normalized_query = (requested - knots[0]) / span
    derivatives = _pchip_derivatives(normalized, ordinates)
    indices = np.searchsorted(normalized, normalized_query, side="right") - 1
    indices = np.clip(indices, 0, normalized.size - 2)
    spacing = normalized[indices + 1] - normalized[indices]
    t = (normalized_query - normalized[indices]) / spacing
    h00 = 2.0 * t**3 - 3.0 * t**2 + 1.0
    h10 = t**3 - 2.0 * t**2 + t
    h01 = -2.0 * t**3 + 3.0 * t**2
    h11 = t**3 - t**2
    result = (
        h00 * ordinates[indices]
        + h10 * spacing * derivatives[indices]
        + h01 * ordinates[indices + 1]
        + h11 * spacing * derivatives[indices + 1]
    )
    return np.asarray(result, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class CalibrationEvaluation:
    """One frequency's ordered complex board correction."""

    frequency_hz: float
    ports: tuple[str, ...]
    coefficients: npt.NDArray[np.complex128]
    exact_knot: bool
    interpolation_validated: bool
    calibration_status: str


@dataclass(frozen=True, slots=True)
class BoardCalibrationLut:
    """Released PCB correction with an explicit interpolation evidence boundary."""

    source: Path
    status: str
    frequencies_hz: npt.NDArray[np.float64]
    correction_gain_db: dict[str, npt.NDArray[np.float64]]
    correction_phase_unwrapped_deg: dict[str, npt.NDArray[np.float64]]
    validated_midpoint_band_hz: tuple[float, float] | None

    @classmethod
    def load(cls, path: Path) -> BoardCalibrationLut:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != 1:
            raise ValueError("board calibration LUT has an unsupported schema")
        frequencies = _readonly(document["frequency_hz"], dtype=np.float64)
        if frequencies.ndim != 1 or frequencies.size < 3 or np.any(np.diff(frequencies) <= 0):
            raise ValueError("board calibration frequencies are invalid")
        support = document.get("support_hz")
        if support != [int(frequencies[0]), int(frequencies[-1])]:
            raise ValueError("board calibration support disagrees with its knots")
        interpolation = document.get("interpolation")
        if not isinstance(interpolation, dict) or interpolation.get("method") != (
            "shape_preserving_cubic_hermite_pchip"
        ):
            raise ValueError("board calibration interpolation method is unsupported")
        if interpolation.get("extrapolation") != "reject":
            raise ValueError("board calibration must reject extrapolation")
        validated = interpolation.get("validated_midpoint_band_hz")
        validated_band = None
        if validated is not None:
            if (
                not isinstance(validated, list)
                or len(validated) != 2
                or not all(isinstance(value, (int, float)) for value in validated)
            ):
                raise ValueError("board calibration validation band is invalid")
            validated_band = (float(validated[0]), float(validated[1]))

        raw_ports = document.get("ports")
        if not isinstance(raw_ports, dict) or tuple(raw_ports) != tuple(
            f"ANT{index}" for index in range(1, 9)
        ):
            raise ValueError("board calibration must contain ANT1 through ANT8 in order")
        gains: dict[str, npt.NDArray[np.float64]] = {}
        phases: dict[str, npt.NDArray[np.float64]] = {}
        for port, values in raw_ports.items():
            if not isinstance(values, dict):
                raise ValueError(f"board calibration {port} entry is invalid")
            gain = _readonly(values["correction_gain_db"], dtype=np.float64)
            phase = _readonly(values["correction_phase_unwrapped_deg"], dtype=np.float64)
            if (
                gain.shape != frequencies.shape
                or phase.shape != frequencies.shape
                or not np.all(np.isfinite(gain))
                or not np.all(np.isfinite(phase))
            ):
                raise ValueError(f"board calibration {port} vectors are invalid")
            gains[port] = gain
            phases[port] = phase
        status = document.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("board calibration status is absent")
        return cls(
            source=path.resolve(),
            status=status,
            frequencies_hz=frequencies,
            correction_gain_db=gains,
            correction_phase_unwrapped_deg=phases,
            validated_midpoint_band_hz=validated_band,
        )

    def evaluate(self, frequency_hz: float, ports: tuple[str, ...]) -> CalibrationEvaluation:
        if not np.isfinite(frequency_hz):
            raise ValueError("calibration frequency must be finite")
        if not ports or len(set(ports)) != len(ports):
            raise ValueError("calibration ports must be a non-empty unique sequence")
        missing = [port for port in ports if port not in self.correction_gain_db]
        if missing:
            raise ValueError(f"calibration has no coefficients for {missing}")
        query = np.asarray([frequency_hz], dtype=np.float64)
        coefficients: list[complex] = []
        for port in ports:
            gain_db = float(
                pchip_interpolate(self.frequencies_hz, self.correction_gain_db[port], query)[0]
            )
            phase_deg = float(
                pchip_interpolate(
                    self.frequencies_hz,
                    self.correction_phase_unwrapped_deg[port],
                    query,
                )[0]
            )
            coefficients.append(10.0 ** (gain_db / 20.0) * np.exp(1j * np.deg2rad(phase_deg)))
        exact_knot = bool(np.any(self.frequencies_hz == frequency_hz))
        interpolation_validated = exact_knot or bool(
            self.validated_midpoint_band_hz is not None
            and self.validated_midpoint_band_hz[0]
            <= frequency_hz
            <= self.validated_midpoint_band_hz[1]
        )
        return CalibrationEvaluation(
            frequency_hz=float(frequency_hz),
            ports=ports,
            coefficients=_readonly(coefficients, dtype=np.complex128),
            exact_knot=exact_knot,
            interpolation_validated=interpolation_validated,
            calibration_status=self.status,
        )
