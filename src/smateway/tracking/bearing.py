"""Noise-weighted bearing likelihood, quality gates, and frequency fusion."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


def wrap_degrees(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    result = np.asarray(values, dtype=np.float64)
    return (result + 180.0) % 360.0 - 180.0


@dataclass(frozen=True, slots=True)
class BearingEstimate:
    """One full circular likelihood and its admission decision."""

    bearing_deg: float
    score: float
    second_bearing_deg: float
    ambiguity_margin_db: float
    residual_phase_rms_deg: float
    valid: bool
    reasons: tuple[str, ...]
    bearings_deg: npt.NDArray[np.float64]
    likelihood: npt.NDArray[np.float64]


def _readonly(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    result = np.asarray(values, dtype=np.float64).copy()
    result.setflags(write=False)
    return result


def solve_bearing(
    calibrated_snapshot: npt.ArrayLike,
    steering: npt.ArrayLike,
    bearings_deg: npt.ArrayLike,
    *,
    weights: npt.ArrayLike | None = None,
    minimum_score: float = 0.5,
    minimum_ambiguity_margin_db: float = 1.0,
    maximum_residual_phase_rms_deg: float = 45.0,
    second_peak_separation_deg: float = 20.0,
) -> BearingEstimate:
    """Solve a normalized matched-manifold likelihood and apply quality gates."""

    snapshot = np.asarray(calibrated_snapshot, dtype=np.complex128)
    manifold = np.asarray(steering, dtype=np.complex128)
    grid = np.asarray(bearings_deg, dtype=np.float64)
    if snapshot.ndim != 1 or snapshot.size < 2:
        raise ValueError("bearing solve requires a complex port vector")
    if manifold.shape != (grid.size, snapshot.size) or grid.ndim != 1 or grid.size < 2:
        raise ValueError("steering matrix, bearing grid, and snapshot disagree")
    if not (
        np.all(np.isfinite(snapshot.real))
        and np.all(np.isfinite(snapshot.imag))
        and np.all(np.isfinite(manifold.real))
        and np.all(np.isfinite(manifold.imag))
        and np.all(np.isfinite(grid))
    ):
        raise ValueError("bearing inputs must be finite")
    if weights is None:
        port_weights = np.ones(snapshot.size, dtype=np.float64)
    else:
        port_weights = np.asarray(weights, dtype=np.float64)
        if port_weights.shape != snapshot.shape or not np.all(np.isfinite(port_weights)):
            raise ValueError("bearing weights disagree with the snapshot")
        if np.any(port_weights < 0.0) or np.count_nonzero(port_weights > 0.0) < 2:
            raise ValueError("bearing solve requires two positive port weights")
    if not 0.0 <= minimum_score <= 1.0:
        raise ValueError("minimum score must lie in [0, 1]")
    if minimum_ambiguity_margin_db < 0.0 or maximum_residual_phase_rms_deg <= 0.0:
        raise ValueError("bearing quality gates are invalid")
    if not 0.0 < second_peak_separation_deg <= 180.0:
        raise ValueError("second-peak separation must lie in (0, 180]")

    weighted_snapshot_power = float(np.sum(port_weights * np.abs(snapshot) ** 2))
    if weighted_snapshot_power <= np.finfo(float).tiny:
        raise ValueError("bearing snapshot has zero admitted power")
    cross = np.sum(np.conjugate(manifold) * (port_weights * snapshot)[None, :], axis=1)
    manifold_power = np.sum(port_weights[None, :] * np.abs(manifold) ** 2, axis=1)
    likelihood = np.abs(cross) ** 2 / (manifold_power * weighted_snapshot_power)
    likelihood = np.clip(likelihood.real, 0.0, 1.0)
    best_index = int(np.argmax(likelihood))
    best_bearing = float(grid[best_index] % 360.0)
    best_score = float(likelihood[best_index])

    separation = np.abs(wrap_degrees(grid - best_bearing))
    candidates = np.flatnonzero(separation >= second_peak_separation_deg)
    if candidates.size:
        second_index = int(candidates[np.argmax(likelihood[candidates])])
    else:
        second_index = int(np.argsort(likelihood)[-2])
    second_score = float(likelihood[second_index])
    ambiguity_margin_db = float(
        10.0
        * np.log10(
            max(best_score, np.finfo(float).tiny)
            / max(second_score, np.finfo(float).tiny)
        )
    )

    best_vector = manifold[best_index]
    scale = complex(
        np.sum(port_weights * np.conjugate(best_vector) * snapshot)
        / np.sum(port_weights * np.abs(best_vector) ** 2)
    )
    admitted = port_weights > 0.0
    residual_phase = np.angle(snapshot[admitted] / (scale * best_vector[admitted]), deg=True)
    residual_phase_rms = float(
        np.sqrt(np.average(residual_phase**2, weights=port_weights[admitted]))
    )
    reasons: list[str] = []
    if best_score < minimum_score:
        reasons.append("score_below_gate")
    if ambiguity_margin_db < minimum_ambiguity_margin_db:
        reasons.append("ambiguous_peak")
    if residual_phase_rms > maximum_residual_phase_rms_deg:
        reasons.append("phase_residual_above_gate")
    return BearingEstimate(
        bearing_deg=best_bearing,
        score=best_score,
        second_bearing_deg=float(grid[second_index] % 360.0),
        ambiguity_margin_db=ambiguity_margin_db,
        residual_phase_rms_deg=residual_phase_rms,
        valid=not reasons,
        reasons=tuple(reasons),
        bearings_deg=_readonly(grid % 360.0),
        likelihood=_readonly(likelihood),
    )


def fuse_log_likelihoods(
    likelihoods: npt.ArrayLike,
    *,
    frequency_weights: npt.ArrayLike | None = None,
    epsilon: float = 1e-12,
) -> npt.NDArray[np.float64]:
    """Fuse per-frequency circular likelihoods without cross-band absolute phase."""

    matrix = np.asarray(likelihoods, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] < 1 or matrix.shape[1] < 2:
        raise ValueError("likelihood fusion requires a frequency by bearing matrix")
    if not np.all(np.isfinite(matrix)) or np.any(matrix < 0.0):
        raise ValueError("likelihoods must be finite and non-negative")
    if not np.isfinite(epsilon) or epsilon <= 0.0:
        raise ValueError("fusion epsilon must be positive and finite")
    if frequency_weights is None:
        weights = np.ones(matrix.shape[0], dtype=np.float64)
    else:
        weights = np.asarray(frequency_weights, dtype=np.float64)
        if weights.shape != (matrix.shape[0],) or not np.all(np.isfinite(weights)):
            raise ValueError("frequency weights disagree with likelihood rows")
        if np.any(weights < 0.0) or not np.any(weights > 0.0):
            raise ValueError("frequency weights must contain a positive value")
    log_score = np.sum(weights[:, None] * np.log(matrix + epsilon), axis=0) / np.sum(weights)
    log_score -= float(np.max(log_score))
    fused = np.exp(log_score)
    maximum = float(np.max(fused))
    result = fused / maximum if maximum > 0.0 else fused
    return np.asarray(result, dtype=np.float64)
