"""Small circular alpha-beta tracker that preserves measurement validity."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _wrap(value: float) -> float:
    return float((value + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True, slots=True)
class TrackState:
    timestamp_s: float
    bearing_deg: float
    angular_velocity_deg_s: float
    measurement_used: bool
    coast_count: int


class CircularAlphaBetaTracker:
    """Constant-angular-velocity filter with explicit invalid-measurement coast."""

    def __init__(self, *, alpha: float = 0.55, beta: float = 0.12, maximum_coasts: int = 5):
        if not 0.0 < alpha <= 1.0 or not 0.0 <= beta <= 1.0:
            raise ValueError("alpha and beta must lie in their stable unit intervals")
        if maximum_coasts < 0:
            raise ValueError("maximum coasts must not be negative")
        self._alpha = float(alpha)
        self._beta = float(beta)
        self._maximum_coasts = int(maximum_coasts)
        self._state: TrackState | None = None

    @property
    def state(self) -> TrackState | None:
        return self._state

    def update(
        self,
        timestamp_s: float,
        bearing_deg: float | None,
        *,
        valid: bool,
    ) -> TrackState | None:
        if not np.isfinite(timestamp_s):
            raise ValueError("tracking timestamp must be finite")
        if bearing_deg is not None and not np.isfinite(bearing_deg):
            raise ValueError("tracking bearing must be finite when provided")
        if self._state is None:
            if not valid or bearing_deg is None:
                return None
            self._state = TrackState(float(timestamp_s), bearing_deg % 360.0, 0.0, True, 0)
            return self._state
        elapsed = float(timestamp_s - self._state.timestamp_s)
        if elapsed <= 0.0:
            raise ValueError("tracking timestamps must increase")
        predicted = (self._state.bearing_deg + self._state.angular_velocity_deg_s * elapsed) % 360.0
        if not valid or bearing_deg is None:
            coast_count = self._state.coast_count + 1
            if coast_count > self._maximum_coasts:
                self._state = None
                return None
            self._state = TrackState(
                float(timestamp_s),
                predicted,
                self._state.angular_velocity_deg_s,
                False,
                coast_count,
            )
            return self._state
        innovation = _wrap(float(bearing_deg) - predicted)
        corrected = (predicted + self._alpha * innovation) % 360.0
        velocity = self._state.angular_velocity_deg_s + self._beta * innovation / elapsed
        self._state = TrackState(float(timestamp_s), corrected, velocity, True, 0)
        return self._state
