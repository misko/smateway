"""Separate fixture-feed and board-path terms from conducted permutation captures."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


class PermutationCalibrationError(ValueError):
    """The permutation experiment is incomplete or mathematically unidentifiable."""


@dataclass(frozen=True, slots=True)
class PermutationObservation:
    """One ALL_OFF-subtracted complex path observation."""

    rotation: int
    feed: str
    antenna: str
    transfer: complex
    artifact_id: str = ""


def wrap_phase_deg(value: float) -> float:
    """Wrap a phase to the half-open interval [-180, 180)."""

    return (float(value) + 180.0) % 360.0 - 180.0


def _rms(values: npt.NDArray[np.float64]) -> float:
    return float(math.sqrt(float(np.mean(np.square(values)))))


def _canonical_names(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}{index}" for index in range(1, 9))


def _validate_observations(
    observations: Sequence[PermutationObservation],
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
    feeds = _canonical_names("F")
    antennas = _canonical_names("ANT")
    rotations = tuple(sorted({item.rotation for item in observations}))
    if len(rotations) < 3:
        raise PermutationCalibrationError("at least three unique rotations are required")
    if len(observations) != len(rotations) * 8:
        raise PermutationCalibrationError("each rotation must contain exactly eight observations")

    for item in observations:
        if item.feed not in feeds or item.antenna not in antennas:
            raise PermutationCalibrationError("observations must use F1..F8 and ANT1..ANT8")
        if not math.isfinite(item.transfer.real) or not math.isfinite(item.transfer.imag):
            raise PermutationCalibrationError("transfer values must be finite")
        if abs(item.transfer) <= 0.0:
            raise PermutationCalibrationError("transfer values must be nonzero")

    for rotation in rotations:
        rows = tuple(item for item in observations if item.rotation == rotation)
        if {item.feed for item in rows} != set(feeds):
            raise PermutationCalibrationError(f"rotation {rotation} does not cover F1..F8")
        if {item.antenna for item in rows} != set(antennas):
            raise PermutationCalibrationError(f"rotation {rotation} does not cover ANT1..ANT8")
        if len({(item.feed, item.antenna) for item in rows}) != 8:
            raise PermutationCalibrationError(f"rotation {rotation} repeats a mapping")
    return rotations, feeds, antennas


def _design_matrix(
    observations: Sequence[PermutationObservation],
    rotations: tuple[int, ...],
    feeds: tuple[str, ...],
    antennas: tuple[str, ...],
) -> npt.NDArray[np.float64]:
    """Build a full-rank design with first-round and F1 gauges fixed to zero."""

    round_columns = {value: index for index, value in enumerate(rotations[1:])}
    feed_offset = len(round_columns)
    feed_columns = {value: feed_offset + index for index, value in enumerate(feeds[1:])}
    antenna_offset = feed_offset + len(feed_columns)
    antenna_columns = {
        value: antenna_offset + index for index, value in enumerate(antennas)
    }
    column_count = len(round_columns) + len(feed_columns) + len(antenna_columns)
    design = np.zeros((len(observations), column_count), dtype=np.float64)
    for row, item in enumerate(observations):
        if item.rotation in round_columns:
            design[row, round_columns[item.rotation]] = 1.0
        if item.feed in feed_columns:
            design[row, feed_columns[item.feed]] = 1.0
        design[row, antenna_columns[item.antenna]] = 1.0
    if int(np.linalg.matrix_rank(design)) != column_count:
        raise PermutationCalibrationError("permutation design is rank deficient")
    return design


def _unit(value: complex) -> complex:
    magnitude = abs(value)
    return value / magnitude if magnitude > 0.0 else complex(1.0, 0.0)


def _fit_wrapped_phase(
    observations: Sequence[PermutationObservation],
    rotations: tuple[int, ...],
    feeds: tuple[str, ...],
    antennas: tuple[str, ...],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], dict[str, object]]:
    """Fit unit phasors by deterministic alternating circular least squares."""

    rotation_index = {value: index for index, value in enumerate(rotations)}
    feed_index = {value: index for index, value in enumerate(feeds)}
    antenna_index = {value: index for index, value in enumerate(antennas)}
    measured = np.asarray([_unit(item.transfer) for item in observations], dtype=np.complex128)

    starts: list[
        tuple[
            npt.NDArray[np.complex128],
            npt.NDArray[np.complex128],
            npt.NDArray[np.complex128],
        ]
    ] = []
    first_board = np.ones(8, dtype=np.complex128)
    for value, item in zip(measured[:8], observations[:8], strict=True):
        first_board[antenna_index[item.antenna]] = value
    starts.append(
        (
            np.ones(len(rotations), dtype=np.complex128),
            np.ones(8, dtype=np.complex128),
            first_board,
        )
    )
    starts.append(
        (
            np.ones(len(rotations), dtype=np.complex128),
            np.ones(8, dtype=np.complex128),
            np.ones(8, dtype=np.complex128),
        )
    )
    generator = np.random.default_rng(20260827)
    for _ in range(32):
        starts.append(
            tuple(
                np.exp(1j * generator.uniform(-math.pi, math.pi, size=count)).astype(
                    np.complex128
                )
                for count in (len(rotations), 8, 8)
            )
        )

    best_terms: tuple[
        npt.NDArray[np.complex128],
        npt.NDArray[np.complex128],
        npt.NDArray[np.complex128],
    ] | None = None
    best_residual: npt.NDArray[np.float64] | None = None
    best_score = math.inf
    for round_terms, feed_terms, board_terms in starts:
        round_terms = round_terms.copy()
        feed_terms = feed_terms.copy()
        board_terms = board_terms.copy()
        previous_score = math.inf
        for _ in range(1000):
            for rotation in rotations:
                index = rotation_index[rotation]
                estimates = [
                    measured[row]
                    / (
                        feed_terms[feed_index[item.feed]]
                        * board_terms[antenna_index[item.antenna]]
                    )
                    for row, item in enumerate(observations)
                    if item.rotation == rotation
                ]
                round_terms[index] = _unit(sum(estimates))
            for feed in feeds:
                index = feed_index[feed]
                estimates = [
                    measured[row]
                    / (
                        round_terms[rotation_index[item.rotation]]
                        * board_terms[antenna_index[item.antenna]]
                    )
                    for row, item in enumerate(observations)
                    if item.feed == feed
                ]
                feed_terms[index] = _unit(sum(estimates))
            for antenna in antennas:
                index = antenna_index[antenna]
                estimates = [
                    measured[row]
                    / (
                        round_terms[rotation_index[item.rotation]]
                        * feed_terms[feed_index[item.feed]]
                    )
                    for row, item in enumerate(observations)
                    if item.antenna == antenna
                ]
                board_terms[index] = _unit(sum(estimates))

            # Preserve the model while fixing the same gauges as the amplitude fit.
            round_gauge = round_terms[0]
            round_terms /= round_gauge
            board_terms *= round_gauge
            feed_gauge = feed_terms[0]
            feed_terms /= feed_gauge
            board_terms *= feed_gauge

            prediction = np.asarray(
                [
                    round_terms[rotation_index[item.rotation]]
                    * feed_terms[feed_index[item.feed]]
                    * board_terms[antenna_index[item.antenna]]
                    for item in observations
                ],
                dtype=np.complex128,
            )
            residual = np.angle(measured / prediction, deg=True).astype(np.float64)
            score = float(np.dot(residual, residual))
            if abs(previous_score - score) < 1e-20:
                break
            previous_score = score
        if score < best_score:
            best_score = score
            best_terms = (round_terms, feed_terms, board_terms)
            best_residual = residual

    assert best_terms is not None and best_residual is not None
    round_terms, feed_terms, board_terms = best_terms

    shifts: dict[int, int] = {}
    cyclic = True
    for rotation in rotations:
        observed_shifts = {
            (
                antenna_index[item.antenna] - feed_index[item.feed]
            )
            % 8
            for item in observations
            if item.rotation == rotation
        }
        if len(observed_shifts) != 1:
            cyclic = False
            break
        shifts[rotation] = observed_shifts.pop()

    if cyclic:
        # Pure cyclic permutations admit eight exactly equivalent phase-ramp branches:
        # q^feed * q^-antenna * q^rotation = 1 for q^8 = 1. The continuously
        # illuminated RX1 reference and rotation-0 closure justify selecting the branch
        # with the smallest capture-common phase changes.
        candidates = []
        for branch in range(8):
            q = cmath_exp_deg(45.0 * branch)
            adjusted_round = np.asarray(
                [
                    value * q ** shifts[rotation]
                    for rotation, value in zip(rotations, round_terms, strict=True)
                ],
                dtype=np.complex128,
            )
            adjusted_feed = np.asarray(
                [value * q**index for index, value in enumerate(feed_terms)],
                dtype=np.complex128,
            )
            adjusted_board = np.asarray(
                [value * q ** (-index) for index, value in enumerate(board_terms)],
                dtype=np.complex128,
            )
            round_phase = np.angle(adjusted_round, deg=True).astype(np.float64)
            score = float(np.dot(round_phase, round_phase))
            candidates.append(
                (score, branch, adjusted_round, adjusted_feed, adjusted_board, round_phase)
            )
        _, branch, round_terms, feed_terms, board_terms, round_phase = min(
            candidates, key=lambda item: item[0]
        )
        branch_document: dict[str, object] = {
            "ambiguity": (
                "pure cyclic mappings admit eight equivalent 45-degree spatial phase ramps"
            ),
            "resolution": (
                "choose the branch minimizing rotation-common phase, using the continuous "
                "RX1 reference and measured rotation-0 closure as the prior"
            ),
            "chosen_branch_index": branch,
            "chosen_spatial_ramp_step_deg": wrap_phase_deg(-45.0 * branch),
            "rotation_common_phase_rms_deg": _rms(round_phase),
            "independent_noncyclic_mapping_used": False,
        }
    else:
        branch_document = {
            "ambiguity": "removed by at least one noncyclic mapping",
            "resolution": "directly identifiable from the mapping matrix",
            "chosen_branch_index": 0,
            "chosen_spatial_ramp_step_deg": 0.0,
            "rotation_common_phase_rms_deg": _rms(
                np.angle(round_terms, deg=True).astype(np.float64)
            ),
            "independent_noncyclic_mapping_used": True,
        }

    parameters = np.asarray(
        [
            *np.angle(round_terms[1:], deg=True),
            *np.angle(feed_terms[1:], deg=True),
            *np.angle(board_terms, deg=True),
        ],
        dtype=np.float64,
    )
    return parameters, best_residual, branch_document


def cmath_exp_deg(phase_deg: float) -> complex:
    """Return a unit phasor without requiring callers to manipulate radians."""

    angle = math.radians(phase_deg)
    return complex(math.cos(angle), math.sin(angle))


def _complex_document(gain_db: float, phase_deg: float) -> dict[str, float]:
    amplitude = 10.0 ** (gain_db / 20.0)
    angle = math.radians(phase_deg)
    return {
        "real": amplitude * math.cos(angle),
        "imag": amplitude * math.sin(angle),
    }


def fit_separable_paths(
    observations: Sequence[PermutationObservation],
) -> dict[str, object]:
    """Fit ``round_common * feed_arm * board_path`` to complex observations.

    The first rotation and F1 are fixed gauges. Board responses and correction
    coefficients are then normalized to ANT1, which removes the remaining common
    complex scale and makes the exported board correction directly usable.
    """

    ordered = tuple(sorted(observations, key=lambda item: (item.rotation, item.feed)))
    rotations, feeds, antennas = _validate_observations(ordered)
    design = _design_matrix(ordered, rotations, feeds, antennas)
    round_parameter_count = len(rotations) - 1
    feed_parameter_count = len(feeds) - 1
    board_offset = round_parameter_count + feed_parameter_count

    amplitude_db = np.asarray(
        [20.0 * math.log10(abs(item.transfer)) for item in ordered], dtype=np.float64
    )
    amplitude_parameters = np.linalg.lstsq(design, amplitude_db, rcond=None)[0]
    amplitude_residual = amplitude_db - design @ amplitude_parameters

    phase_parameters, phase_residual, phase_branch = _fit_wrapped_phase(
        ordered,
        rotations,
        feeds,
        antennas,
    )

    def parameter_pair(index: int) -> tuple[float, float]:
        return float(amplitude_parameters[index]), wrap_phase_deg(phase_parameters[index])

    round_terms = []
    for index, rotation in enumerate(rotations):
        gain_db, phase = (0.0, 0.0) if index == 0 else parameter_pair(index - 1)
        round_terms.append(
            {
                "rotation": rotation,
                "gain_db": gain_db,
                "phase_deg": phase,
                "complex": _complex_document(gain_db, phase),
            }
        )

    feed_terms = []
    for index, feed in enumerate(feeds):
        parameter_index = round_parameter_count + index - 1
        gain_db, phase = (0.0, 0.0) if index == 0 else parameter_pair(parameter_index)
        feed_terms.append(
            {
                "name": feed,
                "gain_db_relative_to_f1": gain_db,
                "phase_deg_relative_to_f1": phase,
                "complex_relative_to_f1": _complex_document(gain_db, phase),
            }
        )

    raw_board = [parameter_pair(board_offset + index) for index in range(8)]
    ant1_gain, ant1_phase = raw_board[0]
    board_terms = []
    for antenna, (gain_db, phase) in zip(antennas, raw_board, strict=True):
        relative_gain = gain_db - ant1_gain
        relative_phase = wrap_phase_deg(phase - ant1_phase)
        correction_gain = -relative_gain
        correction_phase = wrap_phase_deg(-relative_phase)
        board_terms.append(
            {
                "name": antenna,
                "response_gain_db_relative_to_ant1": relative_gain,
                "response_phase_deg_relative_to_ant1": relative_phase,
                "response_complex_relative_to_ant1": _complex_document(
                    relative_gain, relative_phase
                ),
                "correction_gain_db": correction_gain,
                "correction_phase_deg": correction_phase,
                "correction_complex": _complex_document(correction_gain, correction_phase),
            }
        )

    residual_rows = []
    for index, item in enumerate(ordered):
        residual_rows.append(
            {
                "rotation": item.rotation,
                "feed": item.feed,
                "antenna": item.antenna,
                "artifact_id": item.artifact_id,
                "amplitude_residual_db": float(amplitude_residual[index]),
                "phase_residual_deg": float(phase_residual[index]),
            }
        )

    parameter_count = design.shape[1]
    return {
        "model": "measurement = round_common * feed_arm * board_path",
        "observation_count": len(ordered),
        "parameter_count_per_scalar": parameter_count,
        "residual_degrees_of_freedom_per_scalar": len(ordered) - parameter_count,
        "gauge": {
            "round_common": f"rotation {rotations[0]} fixed to 1∠0",
            "feed_arm": "F1 fixed to 1∠0",
            "exported_board_response": "normalized to ANT1",
        },
        "phase_branch_resolution": phase_branch,
        "fit_quality": {
            "amplitude_residual_rms_db": _rms(amplitude_residual),
            "amplitude_residual_max_abs_db": float(np.max(np.abs(amplitude_residual))),
            "phase_residual_rms_deg": _rms(phase_residual),
            "phase_residual_max_abs_deg": float(np.max(np.abs(phase_residual))),
        },
        "round_common_terms": round_terms,
        "feed_arm_terms": feed_terms,
        "board_path_terms": board_terms,
        "residuals": residual_rows,
    }


def compare_rotation_zero_closure(
    initial: Sequence[complex], closure: Sequence[complex]
) -> dict[str, object]:
    """Compare two normal-wiring captures after removing their common complex change."""

    if len(initial) != 8 or len(closure) != 8:
        raise PermutationCalibrationError("closure comparison requires eight paths")
    if any(abs(value) <= 0.0 for value in (*initial, *closure)):
        raise PermutationCalibrationError("closure transfer values must be nonzero")
    delta = np.asarray(
        [new / old for old, new in zip(initial, closure, strict=True)], dtype=np.complex128
    )
    amplitude_delta = 20.0 * np.log10(np.abs(delta))
    phase_delta = np.angle(delta, deg=True)
    common_gain = float(np.mean(amplitude_delta))
    common_phase_phasor = complex(np.mean(np.exp(1j * np.deg2rad(phase_delta))))
    common_phase = math.degrees(math.atan2(common_phase_phasor.imag, common_phase_phasor.real))
    shape_gain = amplitude_delta - common_gain
    shape_phase = np.asarray(
        [wrap_phase_deg(value - common_phase) for value in phase_delta], dtype=np.float64
    )
    return {
        "common_gain_change_db": common_gain,
        "common_phase_change_deg": wrap_phase_deg(common_phase),
        "relative_shape_gain_rms_db": _rms(shape_gain),
        "relative_shape_gain_max_abs_db": float(np.max(np.abs(shape_gain))),
        "relative_shape_phase_rms_deg": _rms(shape_phase),
        "relative_shape_phase_max_abs_deg": float(np.max(np.abs(shape_phase))),
        "per_antenna": [
            {
                "name": f"ANT{index + 1}",
                "gain_change_db": float(amplitude_delta[index]),
                "phase_change_deg": wrap_phase_deg(float(phase_delta[index])),
                "common_removed_gain_change_db": float(shape_gain[index]),
                "common_removed_phase_change_deg": float(shape_phase[index]),
            }
            for index in range(8)
        ],
    }


def coherent_leakage_phase_bound_deg(contrast_db: float) -> float | None:
    """Return the worst additive phase error bound, or None when leakage can dominate."""

    ratio = 10.0 ** (-float(contrast_db) / 20.0)
    if ratio >= 1.0:
        return None
    return math.degrees(math.asin(ratio))
