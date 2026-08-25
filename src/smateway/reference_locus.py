"""Range-difference localization for an OTA antenna connected to Pluto RX1.

For one transmitter ``j`` and selector state ``i``, the simultaneous dual-RX
transfer is approximately

``T[j, i] = G[i] exp(-1j k (d(array[i], tx[j]) - d(rx1, tx[j])))``.

The frequency-matched ratio of ratios ``Q[i] = T[TX2, i] / T[TX1, i]``
cancels the fixed receiver, selector, and PCB path term ``G[i]``.  Correcting
the known array-to-transmitter geometry leaves

``Q[i] exp(+1j k (d(array[i], TX2) - d(array[i], TX1)))``
``= exp(+1j k (d(RX1, TX2) - d(RX1, TX1)))``.

All selector states therefore repeat *one* scalar range-difference observable;
they are not eight independent baselines.  Two known transmitter positions
identify a signed range-difference hyperbola, never a unique planar point.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, pi, sqrt

import numpy as np
import numpy.typing as npt

SPEED_OF_LIGHT_M_S = 299_792_458.0


class ReferenceLocusError(ValueError):
    """The paired reference-transfer inputs cannot support a defensible locus."""


def _readonly(values: npt.ArrayLike, *, dtype: npt.DTypeLike) -> npt.NDArray[np.generic]:
    result = np.asarray(values, dtype=dtype).copy()
    result.setflags(write=False)
    return result


def _unit(values: npt.ArrayLike) -> npt.NDArray[np.complex128]:
    phasors = np.asarray(values, dtype=np.complex128)
    amplitudes = np.abs(phasors)
    if np.any(~np.isfinite(phasors.real)) or np.any(~np.isfinite(phasors.imag)):
        raise ReferenceLocusError("phasors must be finite")
    if np.any(amplitudes <= np.finfo(np.float64).tiny):
        raise ReferenceLocusError("phasors must be non-zero")
    return (phasors / amplitudes).astype(np.complex128)


def wrap_phase_deg(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Wrap phase in degrees into ``[-180, 180)``."""

    phase = np.asarray(values, dtype=np.float64)
    return (phase + 180.0) % 360.0 - 180.0


def signed_range_difference_mm(
    position_mm: npt.ArrayLike,
    tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
) -> float:
    """Return ``distance(position, TX2) - distance(position, TX1)`` in mm."""

    position = np.asarray(position_mm, dtype=np.float64)
    tx1 = np.asarray(tx1_position_mm, dtype=np.float64)
    tx2 = np.asarray(tx2_position_mm, dtype=np.float64)
    if position.shape != (2,) or tx1.shape != (2,) or tx2.shape != (2,):
        raise ValueError("position and transmitter coordinates must be planar points")
    if not np.all(np.isfinite(position)) or not np.all(np.isfinite(tx1)) or not np.all(
        np.isfinite(tx2)
    ):
        raise ValueError("position and transmitter coordinates must be finite")
    return float(np.linalg.norm(position - tx2) - np.linalg.norm(position - tx1))


@dataclass(frozen=True, slots=True)
class ReferenceTransferCapture:
    """One schema-1 transfer artifact reduced to the fields used for pairing."""

    artifact_id: str
    pair_id: str
    tx_channel: int
    carrier_frequency_hz: float
    state_names: tuple[str, ...]
    transfer_phasor: npt.NDArray[np.complex128]
    phase_standard_error_deg: npt.NDArray[np.float64]
    valid_mask: npt.NDArray[np.bool_]
    global_quality_passed: bool = True

    def __post_init__(self) -> None:
        count = len(self.state_names)
        phasors = np.asarray(self.transfer_phasor, dtype=np.complex128)
        uncertainty = np.asarray(self.phase_standard_error_deg, dtype=np.float64)
        valid = np.asarray(self.valid_mask, dtype=np.bool_)
        if not self.artifact_id or not self.pair_id:
            raise ValueError("artifact and pair identifiers must be non-empty")
        if self.tx_channel not in (0, 1):
            raise ValueError("TX channel must be zero or one")
        if not np.isfinite(self.carrier_frequency_hz) or self.carrier_frequency_hz <= 0.0:
            raise ValueError("carrier frequency must be positive and finite")
        if count < 3 or len(set(self.state_names)) != count:
            raise ValueError("at least three uniquely named selector states are required")
        if phasors.shape != (count,) or uncertainty.shape != (count,) or valid.shape != (count,):
            raise ValueError("capture state arrays must match the state-name count")
        effective_valid = valid & self.global_quality_passed
        if np.any(~np.isfinite(phasors[effective_valid].real)) or np.any(
            ~np.isfinite(phasors[effective_valid].imag)
        ):
            raise ValueError("valid transfer phasors must be finite")
        if np.any(np.abs(phasors[effective_valid]) <= np.finfo(np.float64).tiny):
            raise ValueError("valid transfer phasors must be non-zero")
        if np.any(~np.isfinite(uncertainty[effective_valid])) or np.any(
            uncertainty[effective_valid] <= 0.0
        ):
            raise ValueError("valid transfer uncertainty must be positive and finite")
        object.__setattr__(
            self,
            "transfer_phasor",
            _readonly(phasors, dtype=np.complex128),
        )
        object.__setattr__(
            self,
            "phase_standard_error_deg",
            _readonly(uncertainty, dtype=np.float64),
        )
        object.__setattr__(
            self,
            "valid_mask",
            _readonly(effective_valid, dtype=np.bool_),
        )


@dataclass(frozen=True, slots=True)
class PairedReferenceMeasurements:
    """Frequency/state ratio-of-ratios after repeat and geometry aggregation."""

    carrier_frequency_hz: npt.NDArray[np.float64]
    state_names: tuple[str, ...]
    ratio_of_ratios: npt.NDArray[np.complex128]
    geometry_corrected_phasor: npt.NDArray[np.complex128]
    phase_standard_error_deg: npt.NDArray[np.float64]
    pair_count: npt.NDArray[np.int64]
    pair_coherence: npt.NDArray[np.float64]
    corrected_amplitude_ratio: npt.NDArray[np.float64]
    corrected_log_amplitude_std: npt.NDArray[np.float64]
    valid_mask: npt.NDArray[np.bool_]
    array_path_difference_mm: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        frequencies = np.asarray(self.carrier_frequency_hz, dtype=np.float64)
        shape = (frequencies.size, len(self.state_names))
        if frequencies.ndim != 1 or frequencies.size < 1:
            raise ValueError("measurements need at least one carrier frequency")
        if np.unique(frequencies).size != frequencies.size:
            raise ValueError("measurement carrier frequencies must be unique")
        if not np.all(np.isfinite(frequencies)) or np.any(frequencies <= 0.0):
            raise ValueError("measurement carrier frequencies must be positive and finite")
        fields: tuple[tuple[str, npt.DTypeLike], ...] = (
            ("ratio_of_ratios", np.complex128),
            ("geometry_corrected_phasor", np.complex128),
            ("phase_standard_error_deg", np.float64),
            ("pair_count", np.int64),
            ("pair_coherence", np.float64),
            ("corrected_amplitude_ratio", np.float64),
            ("corrected_log_amplitude_std", np.float64),
            ("valid_mask", np.bool_),
        )
        for name, dtype in fields:
            values = np.asarray(getattr(self, name), dtype=dtype)
            if values.shape != shape:
                raise ValueError(f"{name} must have frequency by state shape")
            object.__setattr__(self, name, _readonly(values, dtype=dtype))
        path_difference = np.asarray(self.array_path_difference_mm, dtype=np.float64)
        if path_difference.shape != (len(self.state_names),) or not np.all(
            np.isfinite(path_difference)
        ):
            raise ValueError("array path difference must have one finite value per state")
        object.__setattr__(
            self,
            "carrier_frequency_hz",
            _readonly(frequencies, dtype=np.float64),
        )
        object.__setattr__(
            self,
            "array_path_difference_mm",
            _readonly(path_difference, dtype=np.float64),
        )

    @property
    def frequency_count(self) -> int:
        return int(self.carrier_frequency_hz.size)

    @property
    def state_count(self) -> int:
        return len(self.state_names)


@dataclass(frozen=True, slots=True)
class FrequencyLocusProfiles:
    """Exactly one phase and uncertainty observation per accepted frequency."""

    carrier_frequency_hz: npt.NDArray[np.float64]
    phasor: npt.NDArray[np.complex128]
    phase_standard_error_deg: npt.NDArray[np.float64]
    state_coherence: npt.NDArray[np.float64]
    state_phase_rms_deg: npt.NDArray[np.float64]
    valid_state_count: npt.NDArray[np.int64]
    source_frequency_index: npt.NDArray[np.int64]

    def __post_init__(self) -> None:
        count = np.asarray(self.carrier_frequency_hz).size
        fields: tuple[tuple[str, npt.DTypeLike], ...] = (
            ("carrier_frequency_hz", np.float64),
            ("phasor", np.complex128),
            ("phase_standard_error_deg", np.float64),
            ("state_coherence", np.float64),
            ("state_phase_rms_deg", np.float64),
            ("valid_state_count", np.int64),
            ("source_frequency_index", np.int64),
        )
        if count < 1:
            raise ValueError("at least one accepted frequency profile is required")
        for name, dtype in fields:
            values = np.asarray(getattr(self, name), dtype=dtype)
            if values.shape != (count,):
                raise ValueError(f"{name} must have one value per accepted frequency")
            object.__setattr__(self, name, _readonly(values, dtype=dtype))

    @property
    def frequency_count(self) -> int:
        return int(self.carrier_frequency_hz.size)


@dataclass(frozen=True, slots=True)
class RangeDifferenceFit:
    """Wrapped-frequency likelihood for the signed RX1 range difference."""

    grid_mm: npt.NDArray[np.float64]
    relative_log_likelihood: npt.NDArray[np.float64]
    normalized_weight: npt.NDArray[np.float64]
    map_range_difference_mm: float
    median_range_difference_mm: float
    interval_50_mm: tuple[float, float]
    interval_90_mm: tuple[float, float]
    map_wrapped_rms_deg: float
    effective_grid_sample_count: float
    competing_modes: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        grid = np.asarray(self.grid_mm, dtype=np.float64)
        likelihood = np.asarray(self.relative_log_likelihood, dtype=np.float64)
        weight = np.asarray(self.normalized_weight, dtype=np.float64)
        if (
            grid.ndim != 1
            or grid.size < 3
            or likelihood.shape != grid.shape
            or weight.shape != grid.shape
        ):
            raise ValueError("range-difference fit arrays must have one common grid shape")
        if not np.all(np.isfinite(grid)) or not np.all(np.isfinite(likelihood)):
            raise ValueError("range-difference grid and likelihood must be finite")
        if not np.all(np.isfinite(weight)) or np.any(weight < 0.0) or not np.isclose(
            np.sum(weight), 1.0, atol=1e-10
        ):
            raise ValueError("range-difference weights must be finite and sum to one")
        object.__setattr__(self, "grid_mm", _readonly(grid, dtype=np.float64))
        object.__setattr__(
            self,
            "relative_log_likelihood",
            _readonly(likelihood, dtype=np.float64),
        )
        object.__setattr__(
            self,
            "normalized_weight",
            _readonly(weight, dtype=np.float64),
        )


@dataclass(frozen=True, slots=True)
class LocusSensitivity:
    """One leave-one-frequency/state range-difference refit."""

    omitted_kind: str
    omitted_value: str
    map_range_difference_mm: float
    shift_from_primary_mm: float
    accepted_frequency_count: int
    map_wrapped_rms_deg: float


@dataclass(frozen=True, slots=True)
class WeakAmplitudeResult:
    """Explicitly non-primary free-space amplitude diagnostic."""

    usable: bool
    distance_ratio_tx2_over_tx1: float | None
    log_ratio_scatter: float | None
    inferred_distance_to_tx1_mm: float | None
    inferred_distance_to_tx2_mm: float | None
    candidate_positions_mm: tuple[tuple[float, float], ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ReferenceLocusAnalysis:
    """Primary hyperbola result, diagnostics, and explicit identifiability statement."""

    measurements: PairedReferenceMeasurements
    profiles: FrequencyLocusProfiles
    fit: RangeDifferenceFit
    leave_one_frequency_out: tuple[LocusSensitivity, ...]
    leave_one_state_out: tuple[LocusSensitivity, ...]
    hyperbola_points_mm: npt.NDArray[np.float64]
    weak_amplitude: WeakAmplitudeResult
    tx1_position_mm: tuple[float, float]
    tx2_position_mm: tuple[float, float]
    anchor_separation_mm: float
    identifiability_rank: int = 1

    def __post_init__(self) -> None:
        points = np.asarray(self.hyperbola_points_mm, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or not np.all(np.isfinite(points)):
            raise ValueError("hyperbola points must have finite planar coordinates")
        object.__setattr__(
            self,
            "hyperbola_points_mm",
            _readonly(points, dtype=np.float64),
        )
        if self.identifiability_rank != 1:
            raise ValueError("a paired two-anchor reference analysis has geometric rank one")


def _circular_center_and_rms(
    values: npt.NDArray[np.complex128],
) -> tuple[complex, float, float]:
    units = _unit(values)
    mean = complex(np.mean(units))
    coherence = abs(mean)
    if coherence <= np.finfo(np.float64).tiny:
        return 1.0 + 0.0j, 0.0, 180.0
    center = mean / coherence
    residual_deg = np.angle(units * np.conj(center)) * 180.0 / pi
    rms_deg = sqrt(float(np.mean(residual_deg**2)))
    return complex(center), float(np.clip(coherence, 0.0, 1.0)), rms_deg


def aggregate_reference_transfers(
    captures: tuple[ReferenceTransferCapture, ...],
    *,
    antenna_positions_mm: npt.ArrayLike,
    tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
    minimum_pair_repeats: int = 2,
    minimum_pair_coherence: float = 0.70,
    minimum_phase_standard_error_deg: float = 0.5,
) -> PairedReferenceMeasurements:
    """Pair TX captures and aggregate repeats without creating extra likelihood rows."""

    if not captures:
        raise ReferenceLocusError("no reference-transfer captures were supplied")
    if minimum_pair_repeats < 1:
        raise ValueError("minimum pair repeats must be positive")
    if not 0.0 <= minimum_pair_coherence <= 1.0:
        raise ValueError("minimum pair coherence must lie in [0, 1]")
    if minimum_phase_standard_error_deg <= 0.0:
        raise ValueError("minimum phase standard error must be positive")
    state_names = captures[0].state_names
    if any(capture.state_names != state_names for capture in captures):
        raise ReferenceLocusError("all captures must use the same ordered selector states")
    antennas = np.asarray(antenna_positions_mm, dtype=np.float64)
    tx1 = np.asarray(tx1_position_mm, dtype=np.float64)
    tx2 = np.asarray(tx2_position_mm, dtype=np.float64)
    if antennas.shape != (len(state_names), 2) or tx1.shape != (2,) or tx2.shape != (2,):
        raise ValueError("antenna and transmitter geometry does not match the selector states")
    if not np.all(np.isfinite(antennas)) or not np.all(np.isfinite(tx1)) or not np.all(
        np.isfinite(tx2)
    ):
        raise ValueError("antenna and transmitter geometry must be finite")
    if np.linalg.norm(tx2 - tx1) <= np.finfo(np.float64).tiny:
        raise ValueError("the two transmitter anchors must be distinct")
    array_to_tx1 = np.linalg.norm(antennas - tx1, axis=1)
    array_to_tx2 = np.linalg.norm(antennas - tx2, axis=1)
    if np.any(array_to_tx1 <= 0.0) or np.any(array_to_tx2 <= 0.0):
        raise ValueError("transmitter anchors must not coincide with an array phase center")
    array_path_difference = array_to_tx2 - array_to_tx1

    keyed: dict[tuple[float, str], dict[int, ReferenceTransferCapture]] = {}
    for capture in captures:
        key = (float(capture.carrier_frequency_hz), capture.pair_id)
        by_tx = keyed.setdefault(key, {})
        if capture.tx_channel in by_tx:
            raise ReferenceLocusError(
                f"duplicate TX{capture.tx_channel + 1} capture for pair {capture.pair_id}"
            )
        by_tx[capture.tx_channel] = capture
    incomplete = [key for key, by_tx in keyed.items() if set(by_tx) != {0, 1}]
    if incomplete:
        frequency, pair_id = incomplete[0]
        raise ReferenceLocusError(
            f"incomplete TX pair {pair_id} at {frequency:.3f} Hz"
        )

    frequencies = np.asarray(sorted({key[0] for key in keyed}), dtype=np.float64)
    shape = (frequencies.size, len(state_names))
    ratio = np.zeros(shape, dtype=np.complex128)
    corrected = np.zeros(shape, dtype=np.complex128)
    uncertainty = np.full(shape, np.inf, dtype=np.float64)
    pair_count = np.zeros(shape, dtype=np.int64)
    pair_coherence = np.zeros(shape, dtype=np.float64)
    corrected_amplitude = np.full(shape, np.nan, dtype=np.float64)
    corrected_log_amplitude_std = np.full(shape, np.inf, dtype=np.float64)
    valid = np.zeros(shape, dtype=np.bool_)

    for frequency_index, frequency_hz in enumerate(frequencies):
        frequency_pairs = [
            by_tx for (frequency, _pair_id), by_tx in keyed.items() if frequency == frequency_hz
        ]
        wave_number_per_mm = 2.0 * pi * frequency_hz / (SPEED_OF_LIGHT_M_S * 1000.0)
        geometry_rotation = np.exp(1j * wave_number_per_mm * array_path_difference)
        for state_index in range(len(state_names)):
            pair_values: list[complex] = []
            pair_uncertainty: list[float] = []
            for by_tx in frequency_pairs:
                tx1_capture = by_tx[0]
                tx2_capture = by_tx[1]
                if not (
                    tx1_capture.valid_mask[state_index]
                    and tx2_capture.valid_mask[state_index]
                ):
                    continue
                value = (
                    tx2_capture.transfer_phasor[state_index]
                    / tx1_capture.transfer_phasor[state_index]
                )
                if not np.isfinite(value.real) or not np.isfinite(value.imag) or abs(value) <= 0.0:
                    continue
                pair_values.append(complex(value))
                pair_uncertainty.append(
                    sqrt(
                        tx1_capture.phase_standard_error_deg[state_index] ** 2
                        + tx2_capture.phase_standard_error_deg[state_index] ** 2
                    )
                )
            count = len(pair_values)
            pair_count[frequency_index, state_index] = count
            if count < minimum_pair_repeats:
                continue
            values = np.asarray(pair_values, dtype=np.complex128)
            center, coherence, phase_rms_deg = _circular_center_and_rms(values)
            pair_coherence[frequency_index, state_index] = coherence
            if coherence < minimum_pair_coherence:
                continue
            log_amplitudes = np.log(np.abs(values))
            median_log_amplitude = float(np.median(log_amplitudes))
            log_amplitude_std = sqrt(
                float(np.mean((log_amplitudes - median_log_amplitude) ** 2))
            )
            aggregate_se = max(
                minimum_phase_standard_error_deg,
                phase_rms_deg / sqrt(count),
                sqrt(float(np.mean(np.square(pair_uncertainty)))) / sqrt(count),
            )
            aggregate_amplitude = float(np.exp(median_log_amplitude))
            ratio[frequency_index, state_index] = aggregate_amplitude * center
            corrected[frequency_index, state_index] = center * geometry_rotation[state_index]
            uncertainty[frequency_index, state_index] = aggregate_se
            corrected_amplitude[frequency_index, state_index] = (
                aggregate_amplitude * array_to_tx2[state_index] / array_to_tx1[state_index]
            )
            corrected_log_amplitude_std[frequency_index, state_index] = log_amplitude_std
            valid[frequency_index, state_index] = True

    return PairedReferenceMeasurements(
        carrier_frequency_hz=frequencies,
        state_names=state_names,
        ratio_of_ratios=ratio,
        geometry_corrected_phasor=corrected,
        phase_standard_error_deg=uncertainty,
        pair_count=pair_count,
        pair_coherence=pair_coherence,
        corrected_amplitude_ratio=corrected_amplitude,
        corrected_log_amplitude_std=corrected_log_amplitude_std,
        valid_mask=valid,
        array_path_difference_mm=array_path_difference,
    )


def collapse_frequency_profiles(
    measurements: PairedReferenceMeasurements,
    *,
    omitted_state_index: int | None = None,
    minimum_valid_states: int = 4,
    minimum_state_coherence: float = 0.50,
    minimum_phase_standard_error_deg: float = 0.5,
) -> FrequencyLocusProfiles:
    """Collapse switch states once per frequency without state pseudoreplication."""

    if minimum_valid_states < 2:
        raise ValueError("at least two valid states must be required")
    if not 0.0 <= minimum_state_coherence <= 1.0:
        raise ValueError("minimum state coherence must lie in [0, 1]")
    if omitted_state_index is not None and not 0 <= omitted_state_index < measurements.state_count:
        raise ValueError("omitted state index is outside the selector state set")
    accepted_frequency: list[float] = []
    accepted_phasor: list[complex] = []
    accepted_uncertainty: list[float] = []
    accepted_coherence: list[float] = []
    accepted_rms: list[float] = []
    accepted_state_count: list[int] = []
    source_indices: list[int] = []
    for frequency_index, frequency_hz in enumerate(measurements.carrier_frequency_hz):
        state_mask = measurements.valid_mask[frequency_index].copy()
        if omitted_state_index is not None:
            state_mask[omitted_state_index] = False
        state_indices = np.flatnonzero(state_mask)
        if state_indices.size < minimum_valid_states:
            continue
        values = measurements.geometry_corrected_phasor[frequency_index, state_indices]
        center, coherence, phase_rms_deg = _circular_center_and_rms(values)
        if coherence < minimum_state_coherence:
            continue
        state_uncertainty = measurements.phase_standard_error_deg[
            frequency_index, state_indices
        ]
        # States are systematic replicas of one scalar, not independent baselines.
        # Deliberately do not divide either state scatter term by sqrt(state count).
        aggregate_uncertainty = max(
            minimum_phase_standard_error_deg,
            phase_rms_deg,
            sqrt(float(np.mean(np.square(state_uncertainty)))),
        )
        accepted_frequency.append(float(frequency_hz))
        accepted_phasor.append(center)
        accepted_uncertainty.append(aggregate_uncertainty)
        accepted_coherence.append(coherence)
        accepted_rms.append(phase_rms_deg)
        accepted_state_count.append(int(state_indices.size))
        source_indices.append(frequency_index)
    if not accepted_frequency:
        raise ReferenceLocusError(
            "no frequency has enough coherent, quality-passed selector states"
        )
    return FrequencyLocusProfiles(
        carrier_frequency_hz=np.asarray(accepted_frequency),
        phasor=np.asarray(accepted_phasor),
        phase_standard_error_deg=np.asarray(accepted_uncertainty),
        state_coherence=np.asarray(accepted_coherence),
        state_phase_rms_deg=np.asarray(accepted_rms),
        valid_state_count=np.asarray(accepted_state_count),
        source_frequency_index=np.asarray(source_indices),
    )


def _weighted_quantile(
    grid: npt.NDArray[np.float64],
    weight: npt.NDArray[np.float64],
    probability: float,
) -> float:
    cumulative = np.cumsum(weight)
    index = int(np.searchsorted(cumulative, probability, side="left"))
    return float(grid[min(index, grid.size - 1)])


def fit_range_difference(
    profiles: FrequencyLocusProfiles,
    *,
    anchor_separation_mm: float,
    grid_step_mm: float = 0.1,
    systematic_phase_standard_error_deg: float = 10.0,
    minimum_frequency_count: int = 3,
) -> RangeDifferenceFit:
    """Fit a wrapped signed distance difference on its physical finite interval."""

    if profiles.frequency_count < minimum_frequency_count:
        raise ReferenceLocusError(
            f"need at least {minimum_frequency_count} accepted unique frequencies; "
            f"got {profiles.frequency_count}"
        )
    if not np.isfinite(anchor_separation_mm) or anchor_separation_mm <= 0.0:
        raise ValueError("anchor separation must be positive and finite")
    if not np.isfinite(grid_step_mm) or grid_step_mm <= 0.0:
        raise ValueError("range-difference grid step must be positive and finite")
    if (
        not np.isfinite(systematic_phase_standard_error_deg)
        or systematic_phase_standard_error_deg < 0.0
    ):
        raise ValueError("systematic phase uncertainty must be finite and non-negative")
    grid_count = max(3, ceil(2.0 * anchor_separation_mm / grid_step_mm) + 1)
    grid = np.linspace(-anchor_separation_mm, anchor_separation_mm, grid_count)
    wave_number_per_mm = (
        2.0 * pi * profiles.carrier_frequency_hz / (SPEED_OF_LIGHT_M_S * 1000.0)
    )
    predicted = np.exp(1j * wave_number_per_mm[:, None] * grid[None, :])
    residual_rad = np.angle(profiles.phasor[:, None] * np.conj(predicted))
    sigma_deg = np.sqrt(
        profiles.phase_standard_error_deg**2 + systematic_phase_standard_error_deg**2
    )
    sigma_rad = np.deg2rad(sigma_deg)
    log_likelihood = -0.5 * np.sum((residual_rad / sigma_rad[:, None]) ** 2, axis=0)
    relative = log_likelihood - float(np.max(log_likelihood))
    weight = np.exp(np.maximum(relative, -745.0))
    weight /= np.sum(weight)
    map_index = int(np.argmax(log_likelihood))
    map_delta = float(grid[map_index])
    map_rms = sqrt(float(np.mean(np.rad2deg(residual_rad[:, map_index]) ** 2)))
    local_maximum = np.ones(grid.size, dtype=np.bool_)
    local_maximum[1:] &= log_likelihood[1:] >= log_likelihood[:-1]
    local_maximum[:-1] &= log_likelihood[:-1] >= log_likelihood[1:]
    mode_indices = np.flatnonzero(local_maximum)
    mode_indices = mode_indices[np.argsort(log_likelihood[mode_indices])[::-1]][:8]
    modes = tuple(
        (float(grid[index]), float(log_likelihood[index] - log_likelihood[map_index]))
        for index in mode_indices
    )
    effective_count = float(1.0 / np.sum(weight**2))
    return RangeDifferenceFit(
        grid_mm=grid,
        relative_log_likelihood=relative,
        normalized_weight=weight,
        map_range_difference_mm=map_delta,
        median_range_difference_mm=_weighted_quantile(grid, weight, 0.5),
        interval_50_mm=(
            _weighted_quantile(grid, weight, 0.25),
            _weighted_quantile(grid, weight, 0.75),
        ),
        interval_90_mm=(
            _weighted_quantile(grid, weight, 0.05),
            _weighted_quantile(grid, weight, 0.95),
        ),
        map_wrapped_rms_deg=map_rms,
        effective_grid_sample_count=effective_count,
        competing_modes=modes,
    )


def sample_hyperbola_locus(
    tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
    range_difference_mm: float,
    *,
    bounds_mm: tuple[float, float, float, float],
    sample_count: int = 1001,
) -> npt.NDArray[np.float64]:
    """Sample the signed range-difference hyperbola within planar plot bounds."""

    tx1 = np.asarray(tx1_position_mm, dtype=np.float64)
    tx2 = np.asarray(tx2_position_mm, dtype=np.float64)
    if tx1.shape != (2,) or tx2.shape != (2,) or not np.all(np.isfinite(tx1)) or not np.all(
        np.isfinite(tx2)
    ):
        raise ValueError("transmitter anchors must be finite planar points")
    x0, x1, y0, y1 = bounds_mm
    if not all(np.isfinite(bounds_mm)) or x1 <= x0 or y1 <= y0 or sample_count < 3:
        raise ValueError("locus bounds and sample count are invalid")
    vector = tx2 - tx1
    separation = float(np.linalg.norm(vector))
    if separation <= 0.0:
        raise ValueError("transmitter anchors must be distinct")
    if not np.isfinite(range_difference_mm) or abs(range_difference_mm) > separation + 1e-9:
        raise ValueError("signed range difference exceeds the anchor separation")
    midpoint = 0.5 * (tx1 + tx2)
    axis = vector / separation
    perpendicular = np.asarray((-axis[1], axis[0]))
    diagonal = float(np.hypot(x1 - x0, y1 - y0))
    if abs(range_difference_mm) < 1e-12:
        local_y = np.linspace(-2.0 * diagonal, 2.0 * diagonal, sample_count)
        points = midpoint[None, :] + local_y[:, None] * perpendicular[None, :]
    elif abs(range_difference_mm) >= separation - 1e-9:
        direction = -np.sign(range_difference_mm) * axis
        distance = np.linspace(separation / 2.0, 2.0 * diagonal, sample_count)
        points = midpoint[None, :] + distance[:, None] * direction[None, :]
    else:
        focal_half_distance = separation / 2.0
        semi_major = abs(range_difference_mm) / 2.0
        semi_minor = sqrt(focal_half_distance**2 - semi_major**2)
        maximum_u = np.arcsinh(2.0 * diagonal / semi_minor)
        parameter = np.linspace(-maximum_u, maximum_u, sample_count)
        local_x = -np.sign(range_difference_mm) * semi_major * np.cosh(parameter)
        local_y = semi_minor * np.sinh(parameter)
        points = (
            midpoint[None, :]
            + local_x[:, None] * axis[None, :]
            + local_y[:, None] * perpendicular[None, :]
        )
    inside = (
        (points[:, 0] >= x0)
        & (points[:, 0] <= x1)
        & (points[:, 1] >= y0)
        & (points[:, 1] <= y1)
    )
    result: npt.NDArray[np.float64] = np.asarray(points[inside], dtype=np.float64)
    return result


def _circle_intersections(
    tx1: npt.NDArray[np.float64],
    tx2: npt.NDArray[np.float64],
    radius1: float,
    radius2: float,
) -> tuple[tuple[float, float], ...]:
    vector = tx2 - tx1
    separation = float(np.linalg.norm(vector))
    if radius1 + radius2 < separation or abs(radius1 - radius2) > separation:
        return ()
    along = (radius1**2 - radius2**2 + separation**2) / (2.0 * separation)
    height_squared = radius1**2 - along**2
    if height_squared < -1e-6:
        return ()
    height = sqrt(max(0.0, height_squared))
    axis = vector / separation
    base = tx1 + along * axis
    if height <= 1e-9:
        return ((float(base[0]), float(base[1])),)
    perpendicular = np.asarray((-axis[1], axis[0]))
    return tuple(
        (float(point[0]), float(point[1]))
        for point in (base + height * perpendicular, base - height * perpendicular)
    )


def weak_amplitude_diagnostic(
    measurements: PairedReferenceMeasurements,
    fit: RangeDifferenceFit,
    *,
    tx1_position_mm: npt.ArrayLike,
    tx2_position_mm: npt.ArrayLike,
) -> WeakAmplitudeResult:
    """Compute a non-primary ideal-free-space amplitude ratio diagnostic."""

    values = measurements.corrected_amplitude_ratio[measurements.valid_mask]
    values = values[np.isfinite(values) & (values > 0.0)]
    if values.size < 4:
        return WeakAmplitudeResult(
            usable=False,
            distance_ratio_tx2_over_tx1=None,
            log_ratio_scatter=None,
            inferred_distance_to_tx1_mm=None,
            inferred_distance_to_tx2_mm=None,
            candidate_positions_mm=(),
            reason="fewer than four quality-passed amplitude ratios",
        )
    log_values = np.log(values)
    center = float(np.median(log_values))
    scatter = sqrt(float(np.mean((log_values - center) ** 2)))
    distance_ratio = float(np.exp(center))
    delta = fit.map_range_difference_mm
    denominator = distance_ratio - 1.0
    if abs(denominator) < 0.02 or delta / denominator <= 0.0:
        return WeakAmplitudeResult(
            usable=False,
            distance_ratio_tx2_over_tx1=distance_ratio,
            log_ratio_scatter=scatter,
            inferred_distance_to_tx1_mm=None,
            inferred_distance_to_tx2_mm=None,
            candidate_positions_mm=(),
            reason=(
                "ideal free-space amplitude ratio is degenerate or has a sign inconsistent "
                "with the phase range difference"
            ),
        )
    radius1 = delta / denominator
    radius2 = distance_ratio * radius1
    tx1 = np.asarray(tx1_position_mm, dtype=np.float64)
    tx2 = np.asarray(tx2_position_mm, dtype=np.float64)
    candidates = _circle_intersections(tx1, tx2, radius1, radius2)
    if not candidates:
        return WeakAmplitudeResult(
            usable=False,
            distance_ratio_tx2_over_tx1=distance_ratio,
            log_ratio_scatter=scatter,
            inferred_distance_to_tx1_mm=radius1,
            inferred_distance_to_tx2_mm=radius2,
            candidate_positions_mm=(),
            reason="phase and ideal free-space amplitude ranges do not form intersecting circles",
        )
    return WeakAmplitudeResult(
        usable=True,
        distance_ratio_tx2_over_tx1=distance_ratio,
        log_ratio_scatter=scatter,
        inferred_distance_to_tx1_mm=radius1,
        inferred_distance_to_tx2_mm=radius2,
        candidate_positions_mm=candidates,
        reason=(
            "weak diagnostic only: assumes inverse-distance free-space amplitude, stable "
            "antenna patterns, and negligible multipath; it is not used by the primary fit"
        ),
    )


def analyze_reference_locus(
    captures: tuple[ReferenceTransferCapture, ...],
    *,
    antenna_positions_mm: npt.ArrayLike,
    tx1_position_mm: tuple[float, float],
    tx2_position_mm: tuple[float, float],
    bounds_mm: tuple[float, float, float, float],
    minimum_pair_repeats: int = 2,
    minimum_pair_coherence: float = 0.70,
    minimum_valid_states: int = 4,
    minimum_state_coherence: float = 0.50,
    grid_step_mm: float = 0.1,
    systematic_phase_standard_error_deg: float = 10.0,
) -> ReferenceLocusAnalysis:
    """Run paired aggregation, scalar fit, sensitivity, and locus sampling."""

    measurements = aggregate_reference_transfers(
        captures,
        antenna_positions_mm=antenna_positions_mm,
        tx1_position_mm=tx1_position_mm,
        tx2_position_mm=tx2_position_mm,
        minimum_pair_repeats=minimum_pair_repeats,
        minimum_pair_coherence=minimum_pair_coherence,
    )
    profiles = collapse_frequency_profiles(
        measurements,
        minimum_valid_states=minimum_valid_states,
        minimum_state_coherence=minimum_state_coherence,
    )
    tx1 = np.asarray(tx1_position_mm, dtype=np.float64)
    tx2 = np.asarray(tx2_position_mm, dtype=np.float64)
    separation = float(np.linalg.norm(tx2 - tx1))
    fit = fit_range_difference(
        profiles,
        anchor_separation_mm=separation,
        grid_step_mm=grid_step_mm,
        systematic_phase_standard_error_deg=systematic_phase_standard_error_deg,
    )

    frequency_sensitivity: list[LocusSensitivity] = []
    for omitted_index, frequency_hz in enumerate(profiles.carrier_frequency_hz):
        keep = np.arange(profiles.frequency_count) != omitted_index
        if int(np.sum(keep)) < 2:
            continue
        reduced = FrequencyLocusProfiles(
            carrier_frequency_hz=profiles.carrier_frequency_hz[keep],
            phasor=profiles.phasor[keep],
            phase_standard_error_deg=profiles.phase_standard_error_deg[keep],
            state_coherence=profiles.state_coherence[keep],
            state_phase_rms_deg=profiles.state_phase_rms_deg[keep],
            valid_state_count=profiles.valid_state_count[keep],
            source_frequency_index=profiles.source_frequency_index[keep],
        )
        reduced_fit = fit_range_difference(
            reduced,
            anchor_separation_mm=separation,
            grid_step_mm=grid_step_mm,
            systematic_phase_standard_error_deg=systematic_phase_standard_error_deg,
            minimum_frequency_count=2,
        )
        frequency_sensitivity.append(
            LocusSensitivity(
                omitted_kind="frequency",
                omitted_value=f"{frequency_hz:.6f}",
                map_range_difference_mm=reduced_fit.map_range_difference_mm,
                shift_from_primary_mm=reduced_fit.map_range_difference_mm
                - fit.map_range_difference_mm,
                accepted_frequency_count=reduced.frequency_count,
                map_wrapped_rms_deg=reduced_fit.map_wrapped_rms_deg,
            )
        )

    state_sensitivity: list[LocusSensitivity] = []
    for state_index, state_name in enumerate(measurements.state_names):
        try:
            reduced_profiles = collapse_frequency_profiles(
                measurements,
                omitted_state_index=state_index,
                minimum_valid_states=max(2, minimum_valid_states - 1),
                minimum_state_coherence=minimum_state_coherence,
            )
            reduced_fit = fit_range_difference(
                reduced_profiles,
                anchor_separation_mm=separation,
                grid_step_mm=grid_step_mm,
                systematic_phase_standard_error_deg=systematic_phase_standard_error_deg,
            )
        except ReferenceLocusError:
            continue
        state_sensitivity.append(
            LocusSensitivity(
                omitted_kind="state",
                omitted_value=state_name,
                map_range_difference_mm=reduced_fit.map_range_difference_mm,
                shift_from_primary_mm=reduced_fit.map_range_difference_mm
                - fit.map_range_difference_mm,
                accepted_frequency_count=reduced_profiles.frequency_count,
                map_wrapped_rms_deg=reduced_fit.map_wrapped_rms_deg,
            )
        )

    locus_points = sample_hyperbola_locus(
        tx1,
        tx2,
        fit.map_range_difference_mm,
        bounds_mm=bounds_mm,
    )
    weak_amplitude = weak_amplitude_diagnostic(
        measurements,
        fit,
        tx1_position_mm=tx1,
        tx2_position_mm=tx2,
    )
    return ReferenceLocusAnalysis(
        measurements=measurements,
        profiles=profiles,
        fit=fit,
        leave_one_frequency_out=tuple(frequency_sensitivity),
        leave_one_state_out=tuple(state_sensitivity),
        hyperbola_points_mm=locus_points,
        weak_amplitude=weak_amplitude,
        tx1_position_mm=(float(tx1[0]), float(tx1[1])),
        tx2_position_mm=(float(tx2[0]), float(tx2[1])),
        anchor_separation_mm=separation,
    )
