"""Calibration-free joint localization from paired TX phase measurements.

The observation for one capture pair is the phase of TX2 minus TX1 at every
receive antenna.  A stable receive-path phase is therefore common to the two
transmissions and cancels.  The two independently started transmissions still
have one arbitrary circular phase difference, which is marginalized exactly
for every capture pair.

This module deliberately models only a planar, direct propagation path.  Any
posterior it produces remains conditional on the supplied phase uncertainty;
static, transmitter-dependent antenna phase and multipath are not learned by
repeating an unchanged capture and must be included in that uncertainty by the
caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import pi

import numpy as np
import numpy.typing as npt

SPEED_OF_LIGHT_M_S = 299_792_458.0


def wrap_phase_deg(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Wrap degrees into ``[-180, 180)``."""

    array = np.asarray(values, dtype=np.float64)
    return (array + 180.0) % 360.0 - 180.0


def _readonly(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64).copy()
    array.setflags(write=False)
    return array


def _readonly_bool(values: npt.ArrayLike) -> npt.NDArray[np.bool_]:
    array = np.asarray(values, dtype=np.bool_).copy()
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class PairedPhaseMeasurements:
    """Neutral phase input with one row per paired TX1/TX2 capture.

    ``tx2_minus_tx1_phase_deg`` and ``phase_standard_deviation_deg`` have shape
    ``(capture_pair_count, antenna_count)``.  A scalar or broadcast-compatible
    uncertainty is accepted.  Each row can have a different carrier frequency,
    so repeated measurements at several frequencies use the same structure.
    ``valid_mask`` is broadcast to the same matrix, defaults to all true, and
    permits non-finite placeholders only where false.  At least three antennas
    must remain valid in every row.
    """

    carrier_frequency_hz: npt.NDArray[np.float64]
    tx2_minus_tx1_phase_deg: npt.NDArray[np.float64]
    phase_standard_deviation_deg: npt.NDArray[np.float64]
    valid_mask: npt.NDArray[np.bool_] | None = None

    def __post_init__(self) -> None:
        frequencies = np.asarray(self.carrier_frequency_hz, dtype=np.float64)
        phases = np.asarray(self.tx2_minus_tx1_phase_deg, dtype=np.float64)
        uncertainty = np.asarray(self.phase_standard_deviation_deg, dtype=np.float64)
        if frequencies.ndim != 1 or frequencies.size < 1:
            raise ValueError("carrier frequencies must be a non-empty vector")
        if phases.ndim != 2 or phases.shape[0] != frequencies.size or phases.shape[1] < 3:
            raise ValueError(
                "paired phase measurements must have one row per frequency and at least "
                "three antennas"
            )
        try:
            uncertainty = np.broadcast_to(uncertainty, phases.shape)
        except ValueError as error:
            raise ValueError("phase uncertainty is not broadcast-compatible with phases") from error
        if self.valid_mask is None:
            valid = np.ones(phases.shape, dtype=np.bool_)
        else:
            try:
                valid = np.broadcast_to(
                    np.asarray(self.valid_mask, dtype=np.bool_),
                    phases.shape,
                )
            except ValueError as error:
                raise ValueError("valid mask is not broadcast-compatible with phases") from error
        if not np.all(np.isfinite(frequencies)) or np.any(frequencies <= 0.0):
            raise ValueError("carrier frequencies must be positive and finite")
        if not np.all(np.isfinite(phases[valid])):
            raise ValueError("valid paired phases must be finite")
        if not np.all(np.isfinite(uncertainty[valid])) or np.any(uncertainty[valid] <= 0.0):
            raise ValueError("valid phase uncertainties must be positive and finite")
        if np.any(np.count_nonzero(valid, axis=1) < 3):
            raise ValueError("every capture pair requires at least three valid antennas")
        phases = np.where(valid, phases, 0.0)
        uncertainty = np.where(valid, uncertainty, 1.0)
        object.__setattr__(self, "carrier_frequency_hz", _readonly(frequencies))
        object.__setattr__(
            self,
            "tx2_minus_tx1_phase_deg",
            _readonly(wrap_phase_deg(phases)),
        )
        object.__setattr__(
            self,
            "phase_standard_deviation_deg",
            _readonly(uncertainty),
        )
        object.__setattr__(self, "valid_mask", _readonly_bool(valid))

    @property
    def capture_pair_count(self) -> int:
        return int(self.tx2_minus_tx1_phase_deg.shape[0])

    @property
    def antenna_count(self) -> int:
        return int(self.tx2_minus_tx1_phase_deg.shape[1])


@dataclass(frozen=True, slots=True)
class PlanarArrayGeometry:
    """Planar antenna phase-center coordinates and the radial-prior origin."""

    antenna_positions_mm: npt.NDArray[np.float64]
    center_mm: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        antennas = np.asarray(self.antenna_positions_mm, dtype=np.float64)
        center = np.asarray(self.center_mm, dtype=np.float64)
        if antennas.ndim != 2 or antennas.shape[1] != 2 or antennas.shape[0] < 3:
            raise ValueError("array geometry requires at least three planar antenna points")
        if center.shape != (2,):
            raise ValueError("array center must contain x and y")
        if not np.all(np.isfinite(antennas)) or not np.all(np.isfinite(center)):
            raise ValueError("array geometry must be finite")
        if np.linalg.matrix_rank(antennas - np.mean(antennas, axis=0)) < 2:
            raise ValueError("antenna phase centers must not all be collinear")
        object.__setattr__(self, "antenna_positions_mm", _readonly(antennas))
        object.__setattr__(self, "center_mm", _readonly(center))


@dataclass(frozen=True, slots=True)
class RadialPositionPrior:
    """Independent radius priors for TX1 and TX2; azimuth is uniform."""

    mean_mm: float = 304.8
    standard_deviation_mm: float = 50.0
    minimum_mm: float = 1.0
    maximum_mm: float = 700.0

    def __post_init__(self) -> None:
        values = np.asarray(
            (self.mean_mm, self.standard_deviation_mm, self.minimum_mm, self.maximum_mm),
            dtype=np.float64,
        )
        if not np.all(np.isfinite(values)):
            raise ValueError("radial prior values must be finite")
        if self.standard_deviation_mm <= 0.0:
            raise ValueError("radial prior standard deviation must be positive")
        if self.minimum_mm < 0.0 or self.maximum_mm <= self.minimum_mm:
            raise ValueError("radial prior bounds are invalid")
        if not self.minimum_mm <= self.mean_mm <= self.maximum_mm:
            raise ValueError("radial prior mean must lie inside its bounds")

    def log_density(self, radius_mm: npt.ArrayLike) -> npt.NDArray[np.float64]:
        """Return log density up to the constant truncation normalization."""

        radius = np.asarray(radius_mm, dtype=np.float64)
        standardized = (radius - self.mean_mm) / self.standard_deviation_mm
        result = -0.5 * standardized**2 - np.log(self.standard_deviation_mm)
        return np.where(
            (radius >= self.minimum_mm) & (radius <= self.maximum_mm),
            result,
            -np.inf,
        )


@dataclass(frozen=True, slots=True)
class CircularLikelihood:
    """Controls for the marginalized circular phase likelihood."""

    systematic_phase_std_deg: float = 0.0
    minimum_phase_std_deg: float = 0.1

    def __post_init__(self) -> None:
        if (
            not np.isfinite(self.systematic_phase_std_deg)
            or self.systematic_phase_std_deg < 0.0
        ):
            raise ValueError("systematic phase uncertainty must be finite and non-negative")
        if not np.isfinite(self.minimum_phase_std_deg) or self.minimum_phase_std_deg <= 0.0:
            raise ValueError("minimum phase uncertainty must be positive and finite")


@dataclass(frozen=True, slots=True)
class WeightedPosteriorSamples:
    """Weighted joint posterior particles or grid cells."""

    tx1_radius_mm: npt.NDArray[np.float64]
    tx1_angle_deg: npt.NDArray[np.float64]
    tx2_radius_mm: npt.NDArray[np.float64]
    tx2_angle_deg: npt.NDArray[np.float64]
    tx1_position_mm: npt.NDArray[np.float64]
    tx2_position_mm: npt.NDArray[np.float64]
    weight: npt.NDArray[np.float64]
    log_likelihood: npt.NDArray[np.float64]
    log_posterior_density: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        count = np.asarray(self.weight).size
        vectors = (
            self.tx1_radius_mm,
            self.tx1_angle_deg,
            self.tx2_radius_mm,
            self.tx2_angle_deg,
            self.weight,
            self.log_likelihood,
            self.log_posterior_density,
        )
        if count < 1 or any(np.asarray(values).shape != (count,) for values in vectors):
            raise ValueError("posterior vector fields must have one common non-empty shape")
        if np.asarray(self.tx1_position_mm).shape != (count, 2) or np.asarray(
            self.tx2_position_mm
        ).shape != (count, 2):
            raise ValueError("posterior position fields must have shape (sample_count, 2)")
        weights = np.asarray(self.weight, dtype=np.float64)
        if not np.all(np.isfinite(weights)) or np.any(weights < 0.0):
            raise ValueError("posterior weights must be finite and non-negative")
        if not np.isclose(float(np.sum(weights)), 1.0, rtol=1e-10, atol=1e-12):
            raise ValueError("posterior weights must sum to one")
        for field in (
            "tx1_radius_mm",
            "tx1_angle_deg",
            "tx2_radius_mm",
            "tx2_angle_deg",
            "tx1_position_mm",
            "tx2_position_mm",
            "weight",
            "log_likelihood",
            "log_posterior_density",
        ):
            object.__setattr__(self, field, _readonly(getattr(self, field)))

    @property
    def sample_count(self) -> int:
        return int(self.weight.size)


@dataclass(frozen=True, slots=True)
class PositionPosteriorSummary:
    """Marginal position summary; angle concentration exposes multimodality."""

    mean_position_mm: tuple[float, float]
    mean_radius_mm: float
    median_radius_mm: float
    radius_interval_50_mm: tuple[float, float]
    radius_interval_90_mm: tuple[float, float]
    radius_interval_95_mm: tuple[float, float]
    circular_mean_angle_deg: float
    angle_resultant_length: float


@dataclass(frozen=True, slots=True)
class JointCredibleRegionSummary:
    """Projection bounds of one joint highest-posterior-density sample set."""

    probability: float
    achieved_probability: float
    log_density_threshold: float
    sample_count: int
    tx1_x_bounds_mm: tuple[float, float]
    tx1_y_bounds_mm: tuple[float, float]
    tx2_x_bounds_mm: tuple[float, float]
    tx2_y_bounds_mm: tuple[float, float]


@dataclass(frozen=True, slots=True)
class PosteriorMode:
    """One separated joint mode and the posterior mass assigned to it."""

    probability_mass: float
    map_tx1_position_mm: tuple[float, float]
    map_tx2_position_mm: tuple[float, float]
    mean_tx1_position_mm: tuple[float, float]
    mean_tx2_position_mm: tuple[float, float]
    map_tx1_radius_mm: float
    map_tx1_angle_deg: float
    map_tx2_radius_mm: float
    map_tx2_angle_deg: float
    map_log_posterior_density: float


@dataclass(frozen=True, slots=True)
class PhaseResidualDiagnostics:
    """Profiled nuisance phase and direct-path residuals at one hypothesis."""

    nuisance_offset_deg: npt.NDArray[np.float64]
    residual_phase_deg: npt.NDArray[np.float64]
    capture_pair_rms_deg: npt.NDArray[np.float64]
    overall_weighted_rms_deg: float
    maximum_absolute_residual_deg: float
    valid_mask: npt.NDArray[np.bool_] | None = None

    def __post_init__(self) -> None:
        offsets = np.asarray(self.nuisance_offset_deg)
        residuals = np.asarray(self.residual_phase_deg)
        rms = np.asarray(self.capture_pair_rms_deg)
        if offsets.ndim != 1 or residuals.shape[0] != offsets.size or rms.shape != offsets.shape:
            raise ValueError("residual diagnostic shapes are inconsistent")
        if residuals.ndim != 2:
            raise ValueError("residual phases must be a matrix")
        if self.valid_mask is None:
            valid = np.ones(residuals.shape, dtype=np.bool_)
        else:
            try:
                valid = np.broadcast_to(
                    np.asarray(self.valid_mask, dtype=np.bool_),
                    residuals.shape,
                )
            except ValueError as error:
                raise ValueError(
                    "residual valid mask is not broadcast-compatible with residual phases"
                ) from error
        for field in ("nuisance_offset_deg", "residual_phase_deg", "capture_pair_rms_deg"):
            object.__setattr__(self, field, _readonly(getattr(self, field)))
        object.__setattr__(self, "valid_mask", _readonly_bool(valid))


@dataclass(frozen=True, slots=True)
class DualTxPosterior:
    """Complete weighted posterior and summaries without mode collapse."""

    method: str
    samples: WeightedPosteriorSamples
    modes: tuple[PosteriorMode, ...]
    tx1: PositionPosteriorSummary
    tx2: PositionPosteriorSummary
    credible_regions: tuple[JointCredibleRegionSummary, ...]
    map_residuals: PhaseResidualDiagnostics
    effective_sample_size: float


def _polar_positions(
    geometry: PlanarArrayGeometry,
    radius_mm: npt.ArrayLike,
    angle_deg: npt.ArrayLike,
) -> npt.NDArray[np.float64]:
    radius, angle = np.broadcast_arrays(
        np.asarray(radius_mm, dtype=np.float64),
        np.asarray(angle_deg, dtype=np.float64),
    )
    radians = np.deg2rad(angle)
    return np.stack(
        (
            geometry.center_mm[0] + radius * np.cos(radians),
            geometry.center_mm[1] + radius * np.sin(radians),
        ),
        axis=-1,
    )


def predict_tx2_minus_tx1_phase_deg(
    geometry: PlanarArrayGeometry,
    carrier_frequency_hz: npt.ArrayLike,
    *,
    tx1_radius_mm: float,
    tx1_angle_deg: float,
    tx2_radius_mm: float,
    tx2_angle_deg: float,
) -> npt.NDArray[np.float64]:
    """Predict direct-path TX2-minus-TX1 phases before a common row offset."""

    frequencies = np.atleast_1d(np.asarray(carrier_frequency_hz, dtype=np.float64))
    if frequencies.ndim != 1 or not np.all(np.isfinite(frequencies)) or np.any(
        frequencies <= 0.0
    ):
        raise ValueError("carrier frequencies must be a positive finite vector")
    tx1 = _polar_positions(geometry, tx1_radius_mm, tx1_angle_deg)
    tx2 = _polar_positions(geometry, tx2_radius_mm, tx2_angle_deg)
    distance1_mm = np.linalg.norm(geometry.antenna_positions_mm - tx1, axis=1)
    distance2_mm = np.linalg.norm(geometry.antenna_positions_mm - tx2, axis=1)
    distance_difference_m = (distance2_mm - distance1_mm) * 1e-3
    phase = (
        -360.0
        * frequencies[:, None]
        * distance_difference_m[None, :]
        / SPEED_OF_LIGHT_M_S
    )
    return wrap_phase_deg(phase)


def _measurement_valid_mask(
    measurements: PairedPhaseMeasurements,
) -> npt.NDArray[np.bool_]:
    valid = measurements.valid_mask
    if valid is None:  # Normalized by PairedPhaseMeasurements.__post_init__.
        raise RuntimeError("paired phase validity mask was not normalized")
    return valid


def _kappa(measurements: PairedPhaseMeasurements, model: CircularLikelihood) -> np.ndarray:
    sigma_deg = np.sqrt(
        measurements.phase_standard_deviation_deg**2 + model.systematic_phase_std_deg**2
    )
    sigma_rad = np.deg2rad(np.maximum(sigma_deg, model.minimum_phase_std_deg))
    return np.where(_measurement_valid_mask(measurements), 1.0 / sigma_rad**2, 0.0)


def _log_i0(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Stable log of the modified Bessel I0 using NumPy and its asymptote."""

    x = np.abs(np.asarray(values, dtype=np.float64))
    result = np.empty_like(x)
    small = x <= 50.0
    result[small] = np.log(np.i0(x[small]))
    large_x = x[~small]
    if large_x.size:
        inverse = 1.0 / large_x
        series = (
            1.0
            + inverse / 8.0
            + 9.0 * inverse**2 / 128.0
            + 225.0 * inverse**3 / 3072.0
            + 11025.0 * inverse**4 / 98304.0
        )
        result[~small] = (
            large_x - 0.5 * np.log(2.0 * pi * large_x) + np.log(series)
        )
    return result


def _candidate_log_likelihood(
    measurements: PairedPhaseMeasurements,
    geometry: PlanarArrayGeometry,
    tx1_radius_mm: npt.NDArray[np.float64],
    tx1_angle_deg: npt.NDArray[np.float64],
    tx2_radius_mm: npt.NDArray[np.float64],
    tx2_angle_deg: npt.NDArray[np.float64],
    *,
    model: CircularLikelihood,
    chunk_size: int,
) -> npt.NDArray[np.float64]:
    if geometry.antenna_positions_mm.shape[0] != measurements.antenna_count:
        raise ValueError("measurement and geometry antenna counts differ")
    if chunk_size < 1:
        raise ValueError("likelihood chunk size must be positive")
    count = tx1_radius_mm.size
    result = np.empty(count, dtype=np.float64)
    observed = np.deg2rad(measurements.tx2_minus_tx1_phase_deg)
    concentration = _kappa(measurements, model)
    angular_scale = -2.0 * pi * measurements.carrier_frequency_hz / SPEED_OF_LIGHT_M_S
    for first in range(0, count, chunk_size):
        last = min(first + chunk_size, count)
        tx1 = _polar_positions(
            geometry,
            tx1_radius_mm[first:last],
            tx1_angle_deg[first:last],
        )
        tx2 = _polar_positions(
            geometry,
            tx2_radius_mm[first:last],
            tx2_angle_deg[first:last],
        )
        distance1_mm = np.linalg.norm(
            tx1[:, None, :] - geometry.antenna_positions_mm[None, :, :],
            axis=2,
        )
        distance2_mm = np.linalg.norm(
            tx2[:, None, :] - geometry.antenna_positions_mm[None, :, :],
            axis=2,
        )
        distance_difference_m = (distance2_mm - distance1_mm) * 1e-3
        predicted = angular_scale[:, None, None] * distance_difference_m[None, :, :]
        raw_residual = observed[:, None, :] - predicted
        resultant = np.sum(
            concentration[:, None, :] * np.exp(1j * raw_residual),
            axis=2,
        )
        # Integrating one uniform nuisance offset per row gives I0(|resultant|).
        # Terms involving only the supplied concentration are constant over
        # position candidates and are intentionally omitted.
        result[first:last] = np.sum(_log_i0(np.abs(resultant)), axis=0)
    return result


def phase_residual_diagnostics(
    measurements: PairedPhaseMeasurements,
    geometry: PlanarArrayGeometry,
    *,
    tx1_radius_mm: float,
    tx1_angle_deg: float,
    tx2_radius_mm: float,
    tx2_angle_deg: float,
    likelihood: CircularLikelihood | None = None,
) -> PhaseResidualDiagnostics:
    """Profile each capture-pair nuisance phase and report wrapped residuals."""

    model = likelihood or CircularLikelihood()
    predicted = predict_tx2_minus_tx1_phase_deg(
        geometry,
        measurements.carrier_frequency_hz,
        tx1_radius_mm=tx1_radius_mm,
        tx1_angle_deg=tx1_angle_deg,
        tx2_radius_mm=tx2_radius_mm,
        tx2_angle_deg=tx2_angle_deg,
    )
    concentration = _kappa(measurements, model)
    raw_residual_rad = np.deg2rad(measurements.tx2_minus_tx1_phase_deg - predicted)
    resultant = np.sum(concentration * np.exp(1j * raw_residual_rad), axis=1)
    nuisance_deg = np.rad2deg(np.angle(resultant))
    valid_mask = _measurement_valid_mask(measurements)
    residual_deg = wrap_phase_deg(
        measurements.tx2_minus_tx1_phase_deg - predicted - nuisance_deg[:, None]
    )
    residual_deg = np.where(valid_mask, residual_deg, 0.0)
    row_weight = concentration / np.sum(concentration, axis=1, keepdims=True)
    row_rms = np.sqrt(np.sum(row_weight * residual_deg**2, axis=1))
    total_weight = concentration / np.sum(concentration)
    overall_rms = float(np.sqrt(np.sum(total_weight * residual_deg**2)))
    return PhaseResidualDiagnostics(
        nuisance_offset_deg=nuisance_deg,
        residual_phase_deg=residual_deg,
        capture_pair_rms_deg=row_rms,
        overall_weighted_rms_deg=overall_rms,
        maximum_absolute_residual_deg=float(
            np.max(np.abs(residual_deg[valid_mask]))
        ),
        valid_mask=valid_mask,
    )


def _normalize_log_weights(log_weight: np.ndarray) -> np.ndarray:
    finite = np.isfinite(log_weight)
    if not np.any(finite):
        raise ValueError("no posterior candidate has finite weight")
    maximum = float(np.max(log_weight[finite]))
    weight = np.zeros_like(log_weight)
    weight[finite] = np.exp(log_weight[finite] - maximum)
    total = float(np.sum(weight))
    if not np.isfinite(total) or total <= 0.0:
        raise ValueError("posterior weights cannot be normalized")
    return weight / total


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, probability: float) -> float:
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    index = min(int(np.searchsorted(cumulative, probability, side="left")), order.size - 1)
    return float(values[order[index]])


def _position_summary(
    radius_mm: np.ndarray,
    angle_deg: np.ndarray,
    position_mm: np.ndarray,
    weight: np.ndarray,
) -> PositionPosteriorSummary:
    angle_vector = np.sum(weight * np.exp(1j * np.deg2rad(angle_deg)))

    def interval(probability: float) -> tuple[float, float]:
        tail = (1.0 - probability) / 2.0
        return (
            _weighted_quantile(radius_mm, weight, tail),
            _weighted_quantile(radius_mm, weight, 1.0 - tail),
        )

    mean_position = np.sum(weight[:, None] * position_mm, axis=0)
    return PositionPosteriorSummary(
        mean_position_mm=(float(mean_position[0]), float(mean_position[1])),
        mean_radius_mm=float(np.sum(weight * radius_mm)),
        median_radius_mm=_weighted_quantile(radius_mm, weight, 0.5),
        radius_interval_50_mm=interval(0.5),
        radius_interval_90_mm=interval(0.9),
        radius_interval_95_mm=interval(0.95),
        circular_mean_angle_deg=float(wrap_phase_deg(np.rad2deg(np.angle(angle_vector)))),
        angle_resultant_length=float(abs(angle_vector)),
    )


def _credible_regions(
    samples: WeightedPosteriorSamples,
    levels: tuple[float, ...],
) -> tuple[JointCredibleRegionSummary, ...]:
    order = np.argsort(-samples.log_posterior_density, kind="stable")
    cumulative = np.cumsum(samples.weight[order])
    result = []
    for level in levels:
        if not np.isfinite(level) or not 0.0 < level <= 1.0:
            raise ValueError("credible probabilities must lie in (0, 1]")
        boundary = min(int(np.searchsorted(cumulative, level, side="left")), order.size - 1)
        threshold = float(samples.log_posterior_density[order[boundary]])
        selected = samples.log_posterior_density >= threshold - 1e-12
        tx1 = samples.tx1_position_mm[selected]
        tx2 = samples.tx2_position_mm[selected]
        result.append(
            JointCredibleRegionSummary(
                probability=level,
                achieved_probability=float(np.sum(samples.weight[selected])),
                log_density_threshold=threshold,
                sample_count=int(np.count_nonzero(selected)),
                tx1_x_bounds_mm=(float(np.min(tx1[:, 0])), float(np.max(tx1[:, 0]))),
                tx1_y_bounds_mm=(float(np.min(tx1[:, 1])), float(np.max(tx1[:, 1]))),
                tx2_x_bounds_mm=(float(np.min(tx2[:, 0])), float(np.max(tx2[:, 0]))),
                tx2_y_bounds_mm=(float(np.min(tx2[:, 1])), float(np.max(tx2[:, 1]))),
            )
        )
    return tuple(result)


def _posterior_modes(
    samples: WeightedPosteriorSamples,
    *,
    maximum_modes: int,
    separation_mm: float,
) -> tuple[PosteriorMode, ...]:
    if maximum_modes < 1 or not np.isfinite(separation_mm) or separation_mm <= 0.0:
        raise ValueError("mode count and separation must be positive")
    order = np.argsort(-samples.log_posterior_density, kind="stable")
    cumulative = np.cumsum(samples.weight[order])
    boundary = min(int(np.searchsorted(cumulative, 0.95, side="left")), order.size - 1)
    threshold = float(samples.log_posterior_density[order[boundary]])
    candidates = order[samples.log_posterior_density[order] >= threshold - 1e-12]
    seed_indices: list[int] = []
    for index in candidates:
        if all(
            np.hypot(
                np.linalg.norm(samples.tx1_position_mm[index] - samples.tx1_position_mm[seed]),
                np.linalg.norm(samples.tx2_position_mm[index] - samples.tx2_position_mm[seed]),
            )
            >= separation_mm
            for seed in seed_indices
        ):
            seed_indices.append(int(index))
        if len(seed_indices) == maximum_modes:
            break
    if not seed_indices:
        seed_indices.append(int(order[0]))
    seeds1 = samples.tx1_position_mm[seed_indices]
    seeds2 = samples.tx2_position_mm[seed_indices]
    distance_squared = (
        np.sum((samples.tx1_position_mm[:, None, :] - seeds1[None, :, :]) ** 2, axis=2)
        + np.sum((samples.tx2_position_mm[:, None, :] - seeds2[None, :, :]) ** 2, axis=2)
    )
    assignment = np.argmin(distance_squared, axis=1)
    modes = []
    for cluster_index in range(len(seed_indices)):
        members = np.flatnonzero(assignment == cluster_index)
        if members.size == 0:
            continue
        member_weight = samples.weight[members]
        mass = float(np.sum(member_weight))
        normalized = member_weight / mass
        map_index = int(members[np.argmax(samples.log_posterior_density[members])])
        mean1 = np.sum(normalized[:, None] * samples.tx1_position_mm[members], axis=0)
        mean2 = np.sum(normalized[:, None] * samples.tx2_position_mm[members], axis=0)
        modes.append(
            PosteriorMode(
                probability_mass=mass,
                map_tx1_position_mm=(
                    float(samples.tx1_position_mm[map_index, 0]),
                    float(samples.tx1_position_mm[map_index, 1]),
                ),
                map_tx2_position_mm=(
                    float(samples.tx2_position_mm[map_index, 0]),
                    float(samples.tx2_position_mm[map_index, 1]),
                ),
                mean_tx1_position_mm=(float(mean1[0]), float(mean1[1])),
                mean_tx2_position_mm=(float(mean2[0]), float(mean2[1])),
                map_tx1_radius_mm=float(samples.tx1_radius_mm[map_index]),
                map_tx1_angle_deg=float(samples.tx1_angle_deg[map_index]),
                map_tx2_radius_mm=float(samples.tx2_radius_mm[map_index]),
                map_tx2_angle_deg=float(samples.tx2_angle_deg[map_index]),
                map_log_posterior_density=float(samples.log_posterior_density[map_index]),
            )
        )
    return tuple(sorted(modes, key=lambda mode: mode.probability_mass, reverse=True))


def _build_posterior(
    *,
    method: str,
    measurements: PairedPhaseMeasurements,
    geometry: PlanarArrayGeometry,
    prior: RadialPositionPrior,
    likelihood: CircularLikelihood,
    tx1_radius_mm: np.ndarray,
    tx1_angle_deg: np.ndarray,
    tx2_radius_mm: np.ndarray,
    tx2_angle_deg: np.ndarray,
    log_likelihood: np.ndarray,
    log_weight: np.ndarray,
    credible_levels: tuple[float, ...],
    maximum_modes: int,
    mode_separation_mm: float,
) -> DualTxPosterior:
    log_prior = prior.log_density(tx1_radius_mm) + prior.log_density(tx2_radius_mm)
    weight = _normalize_log_weights(log_weight)
    tx1_position = _polar_positions(geometry, tx1_radius_mm, tx1_angle_deg)
    tx2_position = _polar_positions(geometry, tx2_radius_mm, tx2_angle_deg)
    samples = WeightedPosteriorSamples(
        tx1_radius_mm=tx1_radius_mm,
        tx1_angle_deg=wrap_phase_deg(tx1_angle_deg),
        tx2_radius_mm=tx2_radius_mm,
        tx2_angle_deg=wrap_phase_deg(tx2_angle_deg),
        tx1_position_mm=tx1_position,
        tx2_position_mm=tx2_position,
        weight=weight,
        log_likelihood=log_likelihood,
        log_posterior_density=log_likelihood + log_prior,
    )
    map_index = int(np.argmax(samples.log_posterior_density))
    diagnostics = phase_residual_diagnostics(
        measurements,
        geometry,
        tx1_radius_mm=float(samples.tx1_radius_mm[map_index]),
        tx1_angle_deg=float(samples.tx1_angle_deg[map_index]),
        tx2_radius_mm=float(samples.tx2_radius_mm[map_index]),
        tx2_angle_deg=float(samples.tx2_angle_deg[map_index]),
        likelihood=likelihood,
    )
    return DualTxPosterior(
        method=method,
        samples=samples,
        modes=_posterior_modes(
            samples,
            maximum_modes=maximum_modes,
            separation_mm=mode_separation_mm,
        ),
        tx1=_position_summary(
            samples.tx1_radius_mm,
            samples.tx1_angle_deg,
            samples.tx1_position_mm,
            samples.weight,
        ),
        tx2=_position_summary(
            samples.tx2_radius_mm,
            samples.tx2_angle_deg,
            samples.tx2_position_mm,
            samples.weight,
        ),
        credible_regions=_credible_regions(samples, credible_levels),
        map_residuals=diagnostics,
        effective_sample_size=float(1.0 / np.sum(samples.weight**2)),
    )


def _sample_truncated_radius(
    rng: np.random.Generator,
    count: int,
    prior: RadialPositionPrior,
) -> np.ndarray:
    accepted: list[np.ndarray] = []
    accepted_count = 0
    for _ in range(100):
        needed = count - accepted_count
        if needed <= 0:
            break
        draw = rng.normal(prior.mean_mm, prior.standard_deviation_mm, max(needed * 2, 256))
        valid = draw[(draw >= prior.minimum_mm) & (draw <= prior.maximum_mm)]
        if valid.size:
            retained = valid[:needed]
            accepted.append(retained)
            accepted_count += retained.size
    if accepted_count != count:
        raise ValueError("could not sample the configured truncated radial prior")
    return np.concatenate(accepted)


def infer_dual_tx_importance(
    measurements: PairedPhaseMeasurements,
    geometry: PlanarArrayGeometry,
    *,
    sample_count: int = 100_000,
    seed: int = 0,
    prior: RadialPositionPrior | None = None,
    likelihood: CircularLikelihood | None = None,
    chunk_size: int = 4096,
    credible_levels: tuple[float, ...] = (0.5, 0.9, 0.95),
    maximum_modes: int = 8,
    mode_separation_mm: float = 75.0,
) -> DualTxPosterior:
    """Draw deterministic seeded particles from the prior and importance-weight them."""

    if sample_count < 1:
        raise ValueError("importance sample count must be positive")
    radial_prior = prior or RadialPositionPrior()
    phase_model = likelihood or CircularLikelihood()
    rng = np.random.default_rng(seed)
    tx1_radius = _sample_truncated_radius(rng, sample_count, radial_prior)
    tx1_angle = rng.uniform(-180.0, 180.0, sample_count)
    tx2_radius = _sample_truncated_radius(rng, sample_count, radial_prior)
    tx2_angle = rng.uniform(-180.0, 180.0, sample_count)
    log_likelihood = _candidate_log_likelihood(
        measurements,
        geometry,
        tx1_radius,
        tx1_angle,
        tx2_radius,
        tx2_angle,
        model=phase_model,
        chunk_size=chunk_size,
    )
    # The proposal is the normalized truncated radial prior and uniform azimuth,
    # so the importance ratio is the likelihood alone.
    return _build_posterior(
        method="seeded-prior-importance",
        measurements=measurements,
        geometry=geometry,
        prior=radial_prior,
        likelihood=phase_model,
        tx1_radius_mm=tx1_radius,
        tx1_angle_deg=tx1_angle,
        tx2_radius_mm=tx2_radius,
        tx2_angle_deg=tx2_angle,
        log_likelihood=log_likelihood,
        log_weight=log_likelihood,
        credible_levels=credible_levels,
        maximum_modes=maximum_modes,
        mode_separation_mm=mode_separation_mm,
    )


def _linear_cell_widths(values: np.ndarray) -> np.ndarray:
    if values.ndim != 1 or values.size < 1 or not np.all(np.isfinite(values)):
        raise ValueError("grid coordinates must be a non-empty finite vector")
    if values.size == 1:
        return np.ones(1, dtype=np.float64)
    differences = np.diff(values)
    if np.any(differences <= 0.0):
        raise ValueError("grid coordinates must be strictly increasing")
    boundaries = (values[:-1] + values[1:]) / 2.0
    result = np.empty(values.size, dtype=np.float64)
    result[1:-1] = boundaries[1:] - boundaries[:-1]
    result[0] = differences[0] / 2.0
    result[-1] = differences[-1] / 2.0
    return result


def _circular_cell_widths_deg(values: np.ndarray) -> np.ndarray:
    if values.ndim != 1 or values.size < 2 or not np.all(np.isfinite(values)):
        raise ValueError("angle grid requires at least two finite values")
    differences = np.diff(values)
    if np.any(differences <= 0.0) or values[-1] - values[0] >= 360.0:
        raise ValueError("angle grid must be strictly increasing over less than 360 degrees")
    closing_width = 360.0 - (values[-1] - values[0])
    forward = np.concatenate((differences, np.asarray((closing_width,))))
    if np.any(forward <= 0.0):
        raise ValueError("angle grid must not contain duplicate directions")
    return np.asarray((np.roll(forward, 1) + forward) / 2.0, dtype=np.float64)


def infer_dual_tx_grid(
    measurements: PairedPhaseMeasurements,
    geometry: PlanarArrayGeometry,
    *,
    radius_values_mm: npt.ArrayLike,
    angle_values_deg: npt.ArrayLike,
    prior: RadialPositionPrior | None = None,
    likelihood: CircularLikelihood | None = None,
    chunk_size: int = 4096,
    maximum_grid_points: int = 2_000_000,
    credible_levels: tuple[float, ...] = (0.5, 0.9, 0.95),
    maximum_modes: int = 8,
    mode_separation_mm: float = 75.0,
) -> DualTxPosterior:
    """Evaluate a deterministic polar product grid for both transmitters."""

    radial_prior = prior or RadialPositionPrior()
    phase_model = likelihood or CircularLikelihood()
    radii = np.asarray(radius_values_mm, dtype=np.float64)
    angles = np.asarray(angle_values_deg, dtype=np.float64)
    radial_width = _linear_cell_widths(radii)
    angular_width = _circular_cell_widths_deg(angles)
    single_radius = np.repeat(radii, angles.size)
    single_angle = np.tile(angles, radii.size)
    single_cell = np.repeat(radial_width, angles.size) * np.tile(
        angular_width,
        radii.size,
    )
    single_count = single_radius.size
    grid_count = single_count**2
    if maximum_grid_points < 1 or grid_count > maximum_grid_points:
        raise ValueError(
            f"joint grid has {grid_count} points, above limit {maximum_grid_points}"
        )
    tx1_radius = np.repeat(single_radius, single_count)
    tx1_angle = np.repeat(single_angle, single_count)
    tx2_radius = np.tile(single_radius, single_count)
    tx2_angle = np.tile(single_angle, single_count)
    log_likelihood = _candidate_log_likelihood(
        measurements,
        geometry,
        tx1_radius,
        tx1_angle,
        tx2_radius,
        tx2_angle,
        model=phase_model,
        chunk_size=chunk_size,
    )
    log_prior = radial_prior.log_density(tx1_radius) + radial_prior.log_density(tx2_radius)
    log_cell_measure = np.log(np.repeat(single_cell, single_count)) + np.log(
        np.tile(single_cell, single_count)
    )
    return _build_posterior(
        method="deterministic-polar-grid",
        measurements=measurements,
        geometry=geometry,
        prior=radial_prior,
        likelihood=phase_model,
        tx1_radius_mm=tx1_radius,
        tx1_angle_deg=tx1_angle,
        tx2_radius_mm=tx2_radius,
        tx2_angle_deg=tx2_angle,
        log_likelihood=log_likelihood,
        log_weight=log_likelihood + log_prior + log_cell_measure,
        credible_levels=credible_levels,
        maximum_modes=maximum_modes,
        mode_separation_mm=mode_separation_mm,
    )
