"""Anchored TX2 localization from phase slope across carrier frequency.

The observations are double-relative phases::

    psi_a(f) = (TX2 - TX1)_a(f) - (TX2 - TX1)_reference(f)

TX1 is fixed at a supplied planar position.  For every non-reference receive
antenna, the likelihood integrates one frequency-independent circular phase
intercept.  Consequently, fixed antenna, cable, and switch phase is absorbed;
only the change of phase with exact RF frequency informs TX2 position.

Each carrier frequency may occur only once.  Repeated captures at the same
frequency must be circularly aggregated, with their uncertainty propagated,
before constructing :class:`FrequencySlopeMeasurements`.  This prevents
unchanged frequency profiles from being pseudoreplicated as independent
geometric evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import pi

import numpy as np
import numpy.typing as npt

SPEED_OF_LIGHT_M_S = 299_792_458.0


def wrap_phase_deg(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Wrap phase in degrees into ``[-180, 180)``."""

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


def _readonly_int(values: npt.ArrayLike) -> npt.NDArray[np.int64]:
    array = np.asarray(values, dtype=np.int64).copy()
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class FrequencySlopeMeasurements:
    """One aggregated double-relative phase profile per exact RF frequency.

    Phase, uncertainty, and validity have shape ``(frequency_count,
    antenna_count)`` after broadcasting.  The antenna columns include the
    reference antenna, although its phase values are ignored by inference and
    may be masked.  Invalid cells may contain non-finite placeholders.
    Duplicate frequencies are rejected: aggregate capture repeats first.
    """

    carrier_frequency_hz: npt.NDArray[np.float64]
    tx2_minus_tx1_relative_phase_deg: npt.NDArray[np.float64]
    phase_standard_deviation_deg: npt.NDArray[np.float64]
    valid_mask: npt.NDArray[np.bool_] | None = None

    def __post_init__(self) -> None:
        frequencies = np.asarray(self.carrier_frequency_hz, dtype=np.float64)
        phases = np.asarray(self.tx2_minus_tx1_relative_phase_deg, dtype=np.float64)
        uncertainty = np.asarray(self.phase_standard_deviation_deg, dtype=np.float64)
        if frequencies.ndim != 1 or frequencies.size < 3:
            raise ValueError("frequency-slope localization requires at least three frequencies")
        if phases.ndim != 2 or phases.shape[0] != frequencies.size or phases.shape[1] < 3:
            raise ValueError(
                "phase profiles require one row per frequency and at least three antennas"
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
        if np.unique(frequencies).size != frequencies.size:
            raise ValueError(
                "carrier frequencies must be unique; circularly aggregate repeated captures first"
            )
        if not np.all(np.isfinite(phases[valid])):
            raise ValueError("valid phase measurements must be finite")
        if not np.all(np.isfinite(uncertainty[valid])) or np.any(uncertainty[valid] <= 0.0):
            raise ValueError("valid phase uncertainties must be positive and finite")
        phases = np.where(valid, phases, 0.0)
        uncertainty = np.where(valid, uncertainty, 1.0)
        object.__setattr__(self, "carrier_frequency_hz", _readonly(frequencies))
        object.__setattr__(
            self,
            "tx2_minus_tx1_relative_phase_deg",
            _readonly(wrap_phase_deg(phases)),
        )
        object.__setattr__(self, "phase_standard_deviation_deg", _readonly(uncertainty))
        object.__setattr__(self, "valid_mask", _readonly_bool(valid))

    @property
    def frequency_count(self) -> int:
        return int(self.carrier_frequency_hz.size)

    @property
    def antenna_count(self) -> int:
        return int(self.tx2_minus_tx1_relative_phase_deg.shape[1])


@dataclass(frozen=True, slots=True)
class AnchoredArrayGeometry:
    """Planar receive phase centers and the origin of the TX2 radial prior."""

    antenna_positions_mm: npt.NDArray[np.float64]
    center_mm: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        antennas = np.asarray(self.antenna_positions_mm, dtype=np.float64)
        center = np.asarray(self.center_mm, dtype=np.float64)
        if antennas.ndim != 2 or antennas.shape[1] != 2 or antennas.shape[0] < 4:
            raise ValueError(
                "anchored slope geometry requires a reference and at least three antennas"
            )
        if center.shape != (2,):
            raise ValueError("array center must contain x and y")
        if not np.all(np.isfinite(antennas)) or not np.all(np.isfinite(center)):
            raise ValueError("array geometry must be finite")
        if np.linalg.matrix_rank(antennas - np.mean(antennas, axis=0)) < 2:
            raise ValueError("antenna phase centers must not all be collinear")
        object.__setattr__(self, "antenna_positions_mm", _readonly(antennas))
        object.__setattr__(self, "center_mm", _readonly(center))

    @property
    def antenna_count(self) -> int:
        return int(self.antenna_positions_mm.shape[0])


@dataclass(frozen=True, slots=True)
class Tx2RadialPrior:
    """Truncated Gaussian prior for TX2 radius with uniform azimuth."""

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
        """Return radial log density up to its constant truncation normalization."""

        radius = np.asarray(radius_mm, dtype=np.float64)
        standardized = (radius - self.mean_mm) / self.standard_deviation_mm
        result = -0.5 * standardized**2 - np.log(self.standard_deviation_mm)
        return np.where(
            (radius >= self.minimum_mm) & (radius <= self.maximum_mm),
            result,
            -np.inf,
        )


@dataclass(frozen=True, slots=True)
class FrequencySlopeLikelihood:
    """Circular likelihood controls applied once to each frequency profile."""

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
class FrequencySlopePosteriorSamples:
    """Deterministically seeded prior particles and normalized weights."""

    tx2_radius_mm: npt.NDArray[np.float64]
    tx2_direction_deg: npt.NDArray[np.float64]
    tx2_position_mm: npt.NDArray[np.float64]
    weight: npt.NDArray[np.float64]
    log_likelihood: npt.NDArray[np.float64]
    log_posterior_density: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        count = np.asarray(self.weight).size
        vectors = (
            self.tx2_radius_mm,
            self.tx2_direction_deg,
            self.weight,
            self.log_likelihood,
            self.log_posterior_density,
        )
        if count < 1 or any(np.asarray(values).shape != (count,) for values in vectors):
            raise ValueError("posterior vector fields must have one common non-empty shape")
        if np.asarray(self.tx2_position_mm).shape != (count, 2):
            raise ValueError("posterior TX2 positions must have shape (sample_count, 2)")
        weights = np.asarray(self.weight, dtype=np.float64)
        if not np.all(np.isfinite(weights)) or np.any(weights < 0.0):
            raise ValueError("posterior weights must be finite and non-negative")
        if not np.isclose(float(np.sum(weights)), 1.0, rtol=1e-10, atol=1e-12):
            raise ValueError("posterior weights must sum to one")
        for field in (
            "tx2_radius_mm",
            "tx2_direction_deg",
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
class Tx2PosteriorSummary:
    """MAP and weighted marginal summaries for anchored TX2 position."""

    map_position_mm: tuple[float, float]
    mean_position_mm: tuple[float, float]
    map_radius_mm: float
    mean_radius_mm: float
    median_radius_mm: float
    radius_interval_50_mm: tuple[float, float]
    radius_interval_90_mm: tuple[float, float]
    radius_interval_95_mm: tuple[float, float]
    map_direction_deg: float
    circular_mean_direction_deg: float
    direction_resultant_length: float
    map_log_posterior_density: float


@dataclass(frozen=True, slots=True)
class FrequencySlopeResidualDiagnostics:
    """Profiled antenna intercepts and wrapped residuals at one TX2 position."""

    antenna_indices: npt.NDArray[np.int64]
    nuisance_intercept_deg: npt.NDArray[np.float64]
    residual_phase_deg: npt.NDArray[np.float64]
    antenna_weighted_rms_deg: npt.NDArray[np.float64]
    frequency_weighted_rms_deg: npt.NDArray[np.float64]
    overall_weighted_rms_deg: float
    maximum_absolute_residual_deg: float
    valid_mask: npt.NDArray[np.bool_]

    def __post_init__(self) -> None:
        indices = np.asarray(self.antenna_indices)
        intercepts = np.asarray(self.nuisance_intercept_deg)
        residuals = np.asarray(self.residual_phase_deg)
        antenna_rms = np.asarray(self.antenna_weighted_rms_deg)
        frequency_rms = np.asarray(self.frequency_weighted_rms_deg)
        valid = np.asarray(self.valid_mask)
        if (
            indices.ndim != 1
            or intercepts.shape != indices.shape
            or antenna_rms.shape != indices.shape
        ):
            raise ValueError("antenna residual diagnostic shapes are inconsistent")
        if residuals.ndim != 2 or residuals.shape[1] != indices.size:
            raise ValueError("residual phases must have one column per diagnostic antenna")
        if frequency_rms.shape != (residuals.shape[0],) or valid.shape != residuals.shape:
            raise ValueError("frequency residual diagnostic shapes are inconsistent")
        object.__setattr__(self, "antenna_indices", _readonly_int(indices))
        for field in (
            "nuisance_intercept_deg",
            "residual_phase_deg",
            "antenna_weighted_rms_deg",
            "frequency_weighted_rms_deg",
        ):
            object.__setattr__(self, field, _readonly(getattr(self, field)))
        object.__setattr__(self, "valid_mask", _readonly_bool(valid))


@dataclass(frozen=True, slots=True)
class AnchoredFrequencySlopePosterior:
    """Complete anchored multi-frequency posterior and diagnostics."""

    method: str
    reference_index: int
    fixed_tx1_position_mm: tuple[float, float]
    samples: FrequencySlopePosteriorSamples
    tx2: Tx2PosteriorSummary
    map_residuals: FrequencySlopeResidualDiagnostics
    effective_sample_size: float


def _position(values: npt.ArrayLike, *, name: str) -> npt.NDArray[np.float64]:
    result = np.asarray(values, dtype=np.float64)
    if result.shape != (2,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain finite planar x and y coordinates")
    return result


def _validate_reference(reference_index: int, antenna_count: int) -> None:
    if not 0 <= reference_index < antenna_count:
        raise ValueError("reference antenna index is outside the array geometry")


def predict_double_relative_phase_deg(
    geometry: AnchoredArrayGeometry,
    carrier_frequency_hz: npt.ArrayLike,
    *,
    fixed_tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
    reference_index: int = 0,
) -> npt.NDArray[np.float64]:
    """Predict direct-path double-relative phase for all receive antennas."""

    frequencies = np.asarray(carrier_frequency_hz, dtype=np.float64)
    if frequencies.ndim != 1 or frequencies.size < 1 or not np.all(np.isfinite(frequencies)):
        raise ValueError("carrier frequencies must be a non-empty finite vector")
    if np.any(frequencies <= 0.0):
        raise ValueError("carrier frequencies must be positive")
    _validate_reference(reference_index, geometry.antenna_count)
    tx1 = _position(fixed_tx1_position_mm, name="fixed TX1 position")
    tx2 = _position(tx2_position_mm, name="TX2 position")
    distance1_mm = np.linalg.norm(geometry.antenna_positions_mm - tx1, axis=1)
    distance2_mm = np.linalg.norm(geometry.antenna_positions_mm - tx2, axis=1)
    difference_m = (distance2_mm - distance1_mm) * 1e-3
    relative_difference_m = difference_m - difference_m[reference_index]
    phase = (
        -360.0
        * frequencies[:, None]
        * relative_difference_m[None, :]
        / SPEED_OF_LIGHT_M_S
    )
    return wrap_phase_deg(phase)


def _valid_mask(measurements: FrequencySlopeMeasurements) -> npt.NDArray[np.bool_]:
    valid = measurements.valid_mask
    if valid is None:  # Normalized by FrequencySlopeMeasurements.__post_init__.
        raise RuntimeError("frequency-slope validity mask was not normalized")
    return valid


def _selected_antenna_indices(
    measurements: FrequencySlopeMeasurements,
    *,
    reference_index: int,
) -> npt.NDArray[np.int64]:
    valid = _valid_mask(measurements).copy()
    valid[:, reference_index] = False
    selected = np.flatnonzero(np.count_nonzero(valid, axis=0) >= 3)
    if selected.size < 3:
        raise ValueError(
            "frequency-slope localization requires at least three non-reference antennas "
            "with three valid frequencies each"
        )
    if np.any(np.count_nonzero(valid[:, selected], axis=1) < 3):
        raise ValueError("every frequency profile requires at least three valid antennas")
    return np.asarray(selected, dtype=np.int64)


def _concentration(
    measurements: FrequencySlopeMeasurements,
    model: FrequencySlopeLikelihood,
) -> npt.NDArray[np.float64]:
    sigma_deg = np.sqrt(
        measurements.phase_standard_deviation_deg**2 + model.systematic_phase_std_deg**2
    )
    sigma_rad = np.deg2rad(np.maximum(sigma_deg, model.minimum_phase_std_deg))
    return np.where(_valid_mask(measurements), 1.0 / sigma_rad**2, 0.0)


def _log_i0(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Stable log of modified Bessel I0 using NumPy and its asymptote."""

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
        result[~small] = large_x - 0.5 * np.log(2.0 * pi * large_x) + np.log(series)
    return result


def _polar_positions(
    geometry: AnchoredArrayGeometry,
    radius_mm: npt.ArrayLike,
    direction_deg: npt.ArrayLike,
) -> npt.NDArray[np.float64]:
    radius, direction = np.broadcast_arrays(
        np.asarray(radius_mm, dtype=np.float64),
        np.asarray(direction_deg, dtype=np.float64),
    )
    radians = np.deg2rad(direction)
    return np.stack(
        (
            geometry.center_mm[0] + radius * np.cos(radians),
            geometry.center_mm[1] + radius * np.sin(radians),
        ),
        axis=-1,
    )


def _candidate_log_likelihood(
    measurements: FrequencySlopeMeasurements,
    geometry: AnchoredArrayGeometry,
    fixed_tx1_position_mm: npt.NDArray[np.float64],
    tx2_position_mm: npt.NDArray[np.float64],
    antenna_indices: npt.NDArray[np.int64],
    *,
    reference_index: int,
    model: FrequencySlopeLikelihood,
    chunk_size: int,
) -> npt.NDArray[np.float64]:
    if chunk_size < 1:
        raise ValueError("likelihood chunk size must be positive")
    observed = np.deg2rad(
        measurements.tx2_minus_tx1_relative_phase_deg[:, antenna_indices]
    )
    concentration = _concentration(measurements, model)[:, antenna_indices]
    maximum_resultant = np.sum(concentration, axis=0)
    normalization = _log_i0(maximum_resultant)
    angular_scale = -2.0 * pi * measurements.carrier_frequency_hz / SPEED_OF_LIGHT_M_S
    distance1_mm = np.linalg.norm(
        geometry.antenna_positions_mm - fixed_tx1_position_mm,
        axis=1,
    )
    count = tx2_position_mm.shape[0]
    result = np.empty(count, dtype=np.float64)
    for first in range(0, count, chunk_size):
        last = min(first + chunk_size, count)
        distance2_mm = np.linalg.norm(
            tx2_position_mm[first:last, None, :]
            - geometry.antenna_positions_mm[None, :, :],
            axis=2,
        )
        difference_m = (distance2_mm - distance1_mm[None, :]) * 1e-3
        relative_m = difference_m - difference_m[:, reference_index, None]
        predicted = (
            angular_scale[:, None, None]
            * relative_m[None, :, antenna_indices]
        )
        raw_residual = observed[:, None, :] - predicted
        resultant = np.sum(
            concentration[:, None, :] * np.exp(1j * raw_residual),
            axis=0,
        )
        # One uniform, frequency-independent intercept is integrated for each
        # antenna.  The subtracted term is candidate-independent and places the
        # perfect-fit log likelihood at zero.
        result[first:last] = np.sum(_log_i0(np.abs(resultant)) - normalization, axis=1)
    return result


def frequency_slope_residual_diagnostics(
    measurements: FrequencySlopeMeasurements,
    geometry: AnchoredArrayGeometry,
    *,
    fixed_tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
    reference_index: int = 0,
    likelihood: FrequencySlopeLikelihood | None = None,
) -> FrequencySlopeResidualDiagnostics:
    """Profile fixed antenna intercepts and report wrapped residual phases."""

    if geometry.antenna_count != measurements.antenna_count:
        raise ValueError("measurement and geometry antenna counts differ")
    _validate_reference(reference_index, geometry.antenna_count)
    tx1 = _position(fixed_tx1_position_mm, name="fixed TX1 position")
    tx2 = _position(tx2_position_mm, name="TX2 position")
    antenna_indices = _selected_antenna_indices(
        measurements,
        reference_index=reference_index,
    )
    model = likelihood or FrequencySlopeLikelihood()
    predicted = predict_double_relative_phase_deg(
        geometry,
        measurements.carrier_frequency_hz,
        fixed_tx1_position_mm=tx1,
        tx2_position_mm=tx2,
        reference_index=reference_index,
    )[:, antenna_indices]
    observed = measurements.tx2_minus_tx1_relative_phase_deg[:, antenna_indices]
    concentration = _concentration(measurements, model)[:, antenna_indices]
    valid = _valid_mask(measurements)[:, antenna_indices]
    raw_residual_rad = np.deg2rad(observed - predicted)
    resultant = np.sum(concentration * np.exp(1j * raw_residual_rad), axis=0)
    intercept_deg = np.rad2deg(np.angle(resultant))
    residual_deg = wrap_phase_deg(observed - predicted - intercept_deg[None, :])
    residual_deg = np.where(valid, residual_deg, 0.0)
    antenna_rms = np.sqrt(
        np.sum(concentration * residual_deg**2, axis=0)
        / np.sum(concentration, axis=0)
    )
    frequency_rms = np.sqrt(
        np.sum(concentration * residual_deg**2, axis=1)
        / np.sum(concentration, axis=1)
    )
    overall_rms = float(
        np.sqrt(np.sum(concentration * residual_deg**2) / np.sum(concentration))
    )
    return FrequencySlopeResidualDiagnostics(
        antenna_indices=antenna_indices,
        nuisance_intercept_deg=wrap_phase_deg(intercept_deg),
        residual_phase_deg=residual_deg,
        antenna_weighted_rms_deg=antenna_rms,
        frequency_weighted_rms_deg=frequency_rms,
        overall_weighted_rms_deg=overall_rms,
        maximum_absolute_residual_deg=float(np.max(np.abs(residual_deg[valid]))),
        valid_mask=valid,
    )


def _sample_truncated_radius(
    rng: np.random.Generator,
    count: int,
    prior: Tx2RadialPrior,
) -> npt.NDArray[np.float64]:
    accepted: list[npt.NDArray[np.float64]] = []
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


def _normalize_log_weights(log_weight: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    finite = np.isfinite(log_weight)
    if not np.any(finite):
        raise ValueError("no posterior particle has finite weight")
    maximum = float(np.max(log_weight[finite]))
    weight = np.zeros_like(log_weight)
    weight[finite] = np.exp(log_weight[finite] - maximum)
    total = float(np.sum(weight))
    if not np.isfinite(total) or total <= 0.0:
        raise ValueError("posterior weights cannot be normalized")
    return weight / total


def _weighted_quantile(
    values: npt.NDArray[np.float64],
    weights: npt.NDArray[np.float64],
    probability: float,
) -> float:
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    index = min(int(np.searchsorted(cumulative, probability, side="left")), order.size - 1)
    return float(values[order[index]])


def _posterior_summary(
    samples: FrequencySlopePosteriorSamples,
) -> Tx2PosteriorSummary:
    map_index = int(np.argmax(samples.log_posterior_density))
    mean_position = np.sum(samples.weight[:, None] * samples.tx2_position_mm, axis=0)
    direction_vector = np.sum(
        samples.weight * np.exp(1j * np.deg2rad(samples.tx2_direction_deg))
    )

    def interval(probability: float) -> tuple[float, float]:
        tail = (1.0 - probability) / 2.0
        return (
            _weighted_quantile(samples.tx2_radius_mm, samples.weight, tail),
            _weighted_quantile(samples.tx2_radius_mm, samples.weight, 1.0 - tail),
        )

    return Tx2PosteriorSummary(
        map_position_mm=(
            float(samples.tx2_position_mm[map_index, 0]),
            float(samples.tx2_position_mm[map_index, 1]),
        ),
        mean_position_mm=(float(mean_position[0]), float(mean_position[1])),
        map_radius_mm=float(samples.tx2_radius_mm[map_index]),
        mean_radius_mm=float(np.sum(samples.weight * samples.tx2_radius_mm)),
        median_radius_mm=_weighted_quantile(samples.tx2_radius_mm, samples.weight, 0.5),
        radius_interval_50_mm=interval(0.5),
        radius_interval_90_mm=interval(0.9),
        radius_interval_95_mm=interval(0.95),
        map_direction_deg=float(samples.tx2_direction_deg[map_index]),
        circular_mean_direction_deg=float(
            wrap_phase_deg(np.rad2deg(np.angle(direction_vector)))
        ),
        direction_resultant_length=float(abs(direction_vector)),
        map_log_posterior_density=float(samples.log_posterior_density[map_index]),
    )


def infer_anchored_tx2_frequency_slope(
    measurements: FrequencySlopeMeasurements,
    geometry: AnchoredArrayGeometry,
    *,
    fixed_tx1_position_mm: npt.ArrayLike,
    reference_index: int = 0,
    sample_count: int = 100_000,
    seed: int = 0,
    prior: Tx2RadialPrior | None = None,
    likelihood: FrequencySlopeLikelihood | None = None,
    chunk_size: int = 4096,
) -> AnchoredFrequencySlopePosterior:
    """Weight deterministic seeded TX2 prior particles by phase-slope fit."""

    if geometry.antenna_count != measurements.antenna_count:
        raise ValueError("measurement and geometry antenna counts differ")
    _validate_reference(reference_index, geometry.antenna_count)
    tx1 = _position(fixed_tx1_position_mm, name="fixed TX1 position")
    if sample_count < 1:
        raise ValueError("Monte Carlo sample count must be positive")
    if chunk_size < 1:
        raise ValueError("likelihood chunk size must be positive")
    antenna_indices = _selected_antenna_indices(
        measurements,
        reference_index=reference_index,
    )
    radial_prior = prior or Tx2RadialPrior()
    phase_model = likelihood or FrequencySlopeLikelihood()
    rng = np.random.default_rng(seed)
    radius = _sample_truncated_radius(rng, sample_count, radial_prior)
    direction = rng.uniform(-180.0, 180.0, sample_count)
    position = _polar_positions(geometry, radius, direction)
    log_likelihood = _candidate_log_likelihood(
        measurements,
        geometry,
        tx1,
        position,
        antenna_indices,
        reference_index=reference_index,
        model=phase_model,
        chunk_size=chunk_size,
    )
    # Particles are drawn from the normalized radial prior and uniform angle,
    # so the importance ratio is the likelihood.  The explicit prior is added
    # only to rank sampled points by posterior density for the MAP estimate.
    log_posterior_density = log_likelihood + radial_prior.log_density(radius)
    weight = _normalize_log_weights(log_likelihood)
    samples = FrequencySlopePosteriorSamples(
        tx2_radius_mm=radius,
        tx2_direction_deg=wrap_phase_deg(direction),
        tx2_position_mm=position,
        weight=weight,
        log_likelihood=log_likelihood,
        log_posterior_density=log_posterior_density,
    )
    summary = _posterior_summary(samples)
    diagnostics = frequency_slope_residual_diagnostics(
        measurements,
        geometry,
        fixed_tx1_position_mm=tx1,
        tx2_position_mm=np.asarray(summary.map_position_mm),
        reference_index=reference_index,
        likelihood=phase_model,
    )
    return AnchoredFrequencySlopePosterior(
        method="seeded-prior-frequency-slope-importance",
        reference_index=reference_index,
        fixed_tx1_position_mm=(float(tx1[0]), float(tx1[1])),
        samples=samples,
        tx2=summary,
        map_residuals=diagnostics,
        effective_sample_size=float(1.0 / np.sum(weight**2)),
    )
