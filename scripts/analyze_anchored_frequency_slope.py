#!/usr/bin/env python3
"""Localize TX2 from multi-frequency phase slope with TX1 fixed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.frequency_slope_localization import (
    AnchoredArrayGeometry,
    AnchoredFrequencySlopePosterior,
    FrequencySlopeLikelihood,
    FrequencySlopeMeasurements,
    Tx2RadialPrior,
    infer_anchored_tx2_frequency_slope,
    wrap_phase_deg,
)

REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_ANALYSIS_KIND = "fast20_dualband_phase_distribution_and_joint_localization"
REFERENCE_STATE = "ANT1"
DEFAULT_SAMPLE_COUNT = 100_000
DEFAULT_SEED = 20260825
DEFAULT_SYSTEMATIC_PHASE_STD_DEG = 10.0
MAXIMUM_OUTPUT_PARTICLES = 5_000
MINIMUM_STATISTICAL_STD_DEG = 0.1


class AnalysisDocumentError(RuntimeError):
    """The source analysis document violates an anchored-slope invariant."""


@dataclass(frozen=True, slots=True)
class ExtractedSlopeInputs:
    """Strictly validated arrays extracted from one aggregate analysis document."""

    state_names: tuple[str, ...]
    reference_index: int
    source_center_frequency_hz: npt.NDArray[np.int64]
    source_carrier_frequency_hz: npt.NDArray[np.float64]
    excluded_center_frequency_hz: tuple[int, ...]
    center_frequency_hz: npt.NDArray[np.int64]
    carrier_frequency_hz: npt.NDArray[np.float64]
    mean_phase_deg: npt.NDArray[np.float64]
    repeat_standard_deviation_deg: npt.NDArray[np.float64]
    analyzer_standard_error_deg: npt.NDArray[np.float64]
    statistical_standard_deviation_deg: npt.NDArray[np.float64]
    valid_mask: npt.NDArray[np.bool_]
    geometry: AnchoredArrayGeometry
    measurements: FrequencySlopeMeasurements


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--analysis",
        "--input",
        dest="analysis",
        type=Path,
        required=True,
        help="aggregate analysis JSON produced by analyze_dualband_phase_distribution.py",
    )
    parser.add_argument("--tx1-anchor-x-mm", type=float, required=True)
    parser.add_argument("--tx1-anchor-y-mm", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-count", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--exclude-center-frequency-hz",
        type=int,
        action="append",
        help="omit one center-frequency profile; repeat for leave-many-frequency-out checks",
    )
    parser.add_argument(
        "--systematic-phase-std-deg",
        type=float,
        default=DEFAULT_SYSTEMATIC_PHASE_STD_DEG,
        help="frequency-slope systematic uncertainty added once by the likelihood",
    )
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AnalysisDocumentError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AnalysisDocumentError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisDocumentError(f"{label} must be a non-empty string")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisDocumentError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise AnalysisDocumentError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisDocumentError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AnalysisDocumentError(f"{label} must be boolean")
    return value


def _numeric_vector(
    value: object,
    label: str,
    *,
    expected_count: int,
    non_negative: bool = False,
) -> npt.NDArray[np.float64]:
    raw = _sequence(value, label)
    if len(raw) != expected_count:
        raise AnalysisDocumentError(
            f"{label} has {len(raw)} values; expected {expected_count}"
        )
    result = np.asarray(
        [_number(item, f"{label}[{index}]") for index, item in enumerate(raw)],
        dtype=np.float64,
    )
    if non_negative and np.any(result < 0.0):
        raise AnalysisDocumentError(f"{label} values must be non-negative")
    return result


def _valid_vector(
    value: object,
    label: str,
    *,
    expected_count: int,
) -> npt.NDArray[np.bool_]:
    raw = _sequence(value, label)
    if len(raw) != expected_count:
        raise AnalysisDocumentError(
            f"{label} has {len(raw)} values; expected {expected_count}"
        )
    return np.asarray(
        [_boolean(item, f"{label}[{index}]") for index, item in enumerate(raw)],
        dtype=np.bool_,
    )


def _state_names(value: object, label: str) -> tuple[str, ...]:
    raw = _sequence(value, label)
    names = tuple(_string(item, f"{label}[{index}]") for index, item in enumerate(raw))
    if len(names) < 4:
        raise AnalysisDocumentError(
            "selected states must include ANT1 and at least three localization antennas"
        )
    if len(set(names)) != len(names):
        raise AnalysisDocumentError("selected state names must be unique")
    if REFERENCE_STATE not in names:
        raise AnalysisDocumentError("selected state names must contain ANT1 as the reference")
    return names


def _geometry(
    localization: Mapping[str, Any],
    state_names: tuple[str, ...],
) -> AnchoredArrayGeometry:
    raw_geometry = _mapping(localization.get("geometry"), "localization.geometry")
    raw_positions = _mapping(
        raw_geometry.get("selected_antenna_positions_mm"),
        "localization.geometry.selected_antenna_positions_mm",
    )
    if set(raw_positions) != set(state_names):
        raise AnalysisDocumentError(
            "selected antenna geometry keys differ from selected state names"
        )
    positions = np.asarray(
        [
            _numeric_vector(
                raw_positions[name],
                f"selected_antenna_positions_mm.{name}",
                expected_count=2,
            )
            for name in state_names
        ],
        dtype=np.float64,
    )
    center = _numeric_vector(
        raw_geometry.get("inference_center_mm"),
        "localization.geometry.inference_center_mm",
        expected_count=2,
    )
    try:
        return AnchoredArrayGeometry(antenna_positions_mm=positions, center_mm=center)
    except ValueError as error:
        raise AnalysisDocumentError(f"invalid selected antenna geometry: {error}") from error


def _extract_inputs(
    document: Mapping[str, Any],
    excluded_center_frequency_hz: Sequence[int] = (),
) -> ExtractedSlopeInputs:
    if document.get("schema") != 1:
        raise AnalysisDocumentError("source analysis schema must be 1")
    if document.get("analysis_kind") != EXPECTED_ANALYSIS_KIND:
        raise AnalysisDocumentError(
            "input is not an analyze_dualband_phase_distribution.py output document"
        )
    localization = _mapping(document.get("localization"), "localization")
    names = _state_names(
        localization.get("selected_state_names"),
        "localization.selected_state_names",
    )
    reference_index = names.index(REFERENCE_STATE)
    geometry = _geometry(localization, names)
    rows = _sequence(
        localization.get("frequency_profile_rows"),
        "localization.frequency_profile_rows",
    )
    if len(rows) < 3:
        raise AnalysisDocumentError("anchored slope analysis requires at least three profiles")

    center_frequencies = []
    frequencies = []
    means = []
    repeat_std = []
    analyzer_se = []
    valid_masks = []
    for row_index, raw_row in enumerate(rows):
        label = f"localization.frequency_profile_rows[{row_index}]"
        row = _mapping(raw_row, label)
        row_names = _state_names(row.get("state_names"), f"{label}.state_names")
        if row_names != names:
            raise AnalysisDocumentError(f"{label}.state_names differs from selected state order")
        center_frequency_hz = _integer(
            row.get("center_frequency_hz"),
            f"{label}.center_frequency_hz",
        )
        if center_frequency_hz <= 0:
            raise AnalysisDocumentError(f"{label}.center_frequency_hz must be positive")
        center_frequencies.append(center_frequency_hz)
        frequency_hz = _number(row.get("carrier_frequency_hz"), f"{label}.carrier_frequency_hz")
        if frequency_hz <= 0.0:
            raise AnalysisDocumentError(f"{label}.carrier_frequency_hz must be positive")
        frequencies.append(frequency_hz)
        means.append(
            _numeric_vector(
                row.get("circular_mean_double_relative_phase_deg"),
                f"{label}.circular_mean_double_relative_phase_deg",
                expected_count=len(names),
            )
        )
        repeat_std.append(
            _numeric_vector(
                row.get("circular_repeat_standard_deviation_deg"),
                f"{label}.circular_repeat_standard_deviation_deg",
                expected_count=len(names),
                non_negative=True,
            )
        )
        analyzer_se.append(
            _numeric_vector(
                row.get("aggregate_analyzer_standard_error_deg"),
                f"{label}.aggregate_analyzer_standard_error_deg",
                expected_count=len(names),
                non_negative=True,
            )
        )
        valid_masks.append(
            _valid_vector(
                row.get("valid_mask"),
                f"{label}.valid_mask",
                expected_count=len(names),
            )
        )

    source_center_array = np.asarray(center_frequencies, dtype=np.int64)
    if np.unique(source_center_array).size != source_center_array.size:
        raise AnalysisDocumentError("frequency profile center frequencies must be unique")
    exclusions = tuple(
        _integer(value, f"excluded_center_frequency_hz[{index}]")
        for index, value in enumerate(excluded_center_frequency_hz)
    )
    if len(set(exclusions)) != len(exclusions):
        raise AnalysisDocumentError("excluded center frequencies must not contain duplicates")
    unknown_exclusions = sorted(set(exclusions) - set(center_frequencies))
    if unknown_exclusions:
        raise AnalysisDocumentError(
            "excluded center frequencies are absent from the source profiles: "
            + ", ".join(str(value) for value in unknown_exclusions)
        )
    source_frequency_array = np.asarray(frequencies, dtype=np.float64)
    if np.unique(source_frequency_array).size != source_frequency_array.size:
        raise AnalysisDocumentError(
            "frequency profile carrier frequencies must be unique; repeated captures must "
            "already be circularly aggregated"
        )
    source_mean_array = np.asarray(means, dtype=np.float64)
    source_repeat_array = np.asarray(repeat_std, dtype=np.float64)
    source_analyzer_array = np.asarray(analyzer_se, dtype=np.float64)
    source_valid_array = np.asarray(valid_masks, dtype=np.bool_)
    valid_reference = source_valid_array[:, reference_index]
    reference_phase = wrap_phase_deg(source_mean_array[:, reference_index])
    if np.any(np.abs(reference_phase[valid_reference]) > 1e-6):
        raise AnalysisDocumentError("valid ANT1 double-relative phases must be zero")
    source_statistical_std = np.sqrt(source_repeat_array**2 + source_analyzer_array**2)
    # A zero statistical estimate is possible for a noiseless synthetic cell
    # or ANT1.  This numerical lower bound is not a systematic phase floor.
    source_statistical_std = np.maximum(
        source_statistical_std,
        MINIMUM_STATISTICAL_STD_DEG,
    )
    exclusion_set = set(exclusions)
    used = np.asarray([value not in exclusion_set for value in center_frequencies], dtype=np.bool_)
    if np.count_nonzero(used) < 3:
        raise AnalysisDocumentError(
            "at least three frequency profiles must remain after exclusions"
        )
    center_array = source_center_array[used]
    frequency_array = source_frequency_array[used]
    mean_array = source_mean_array[used]
    repeat_array = source_repeat_array[used]
    analyzer_array = source_analyzer_array[used]
    statistical_std = source_statistical_std[used]
    valid_array = source_valid_array[used]
    try:
        measurements = FrequencySlopeMeasurements(
            carrier_frequency_hz=frequency_array,
            tx2_minus_tx1_relative_phase_deg=mean_array,
            phase_standard_deviation_deg=statistical_std,
            valid_mask=valid_array,
        )
    except ValueError as error:
        raise AnalysisDocumentError(f"invalid frequency-slope measurements: {error}") from error
    return ExtractedSlopeInputs(
        state_names=names,
        reference_index=reference_index,
        source_center_frequency_hz=source_center_array,
        source_carrier_frequency_hz=source_frequency_array,
        excluded_center_frequency_hz=exclusions,
        center_frequency_hz=center_array,
        carrier_frequency_hz=frequency_array,
        mean_phase_deg=mean_array,
        repeat_standard_deviation_deg=repeat_array,
        analyzer_standard_error_deg=analyzer_array,
        statistical_standard_deviation_deg=statistical_std,
        valid_mask=valid_array,
        geometry=geometry,
        measurements=measurements,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_commit() -> str | None:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _particle_downsample(
    posterior: AnchoredFrequencySlopePosterior,
    *,
    seed: int,
    maximum_count: int = MAXIMUM_OUTPUT_PARTICLES,
) -> dict[str, Any]:
    samples = posterior.samples
    source_count = samples.sample_count
    if source_count <= maximum_count:
        indices = np.arange(source_count)
        display_weights = samples.weight.copy()
        method = "all-weighted-particles"
    else:
        rng = np.random.default_rng(seed)
        targets = (np.arange(maximum_count, dtype=np.float64) + rng.random()) / maximum_count
        selected = np.searchsorted(np.cumsum(samples.weight), targets, side="left")
        indices, counts = np.unique(np.minimum(selected, source_count - 1), return_counts=True)
        display_weights = counts.astype(np.float64) / maximum_count
        method = "seeded-systematic-resample"
    particles = []
    for index, display_weight in zip(indices, display_weights, strict=True):
        particles.append(
            {
                "source_index": int(index),
                "display_weight": float(display_weight),
                "source_weight": float(samples.weight[index]),
                "tx2_radius_mm": float(samples.tx2_radius_mm[index]),
                "tx2_direction_deg": float(samples.tx2_direction_deg[index]),
                "tx2_position_mm": samples.tx2_position_mm[index].tolist(),
                "log_likelihood": float(samples.log_likelihood[index]),
                "log_posterior_density": float(samples.log_posterior_density[index]),
            }
        )
    return {
        "method": method,
        "source_particle_count": source_count,
        "maximum_output_count": maximum_count,
        "output_particle_count": len(particles),
        "display_weight_sum": float(np.sum(display_weights)),
        "particles": particles,
    }


def _posterior_document(
    posterior: AnchoredFrequencySlopePosterior,
    inputs: ExtractedSlopeInputs,
    *,
    particle_seed: int,
) -> dict[str, Any]:
    samples = posterior.samples
    map_index = int(np.argmax(samples.log_posterior_density))
    diagnostics = posterior.map_residuals
    diagnostic_names = [inputs.state_names[index] for index in diagnostics.antenna_indices]
    return {
        "method": posterior.method,
        "sample_count": samples.sample_count,
        "effective_sample_size": posterior.effective_sample_size,
        "effective_sample_fraction": posterior.effective_sample_size / samples.sample_count,
        "low_effective_sample_size_warning": posterior.effective_sample_size
        < max(100.0, samples.sample_count * 0.001),
        "map": {
            "tx2_position_mm": samples.tx2_position_mm[map_index].tolist(),
            "tx2_radius_mm": float(samples.tx2_radius_mm[map_index]),
            "tx2_direction_deg": float(samples.tx2_direction_deg[map_index]),
            "log_likelihood": float(samples.log_likelihood[map_index]),
            "log_posterior_density": float(samples.log_posterior_density[map_index]),
            "posterior_weight": float(samples.weight[map_index]),
        },
        "tx2": asdict(posterior.tx2),
        "map_residuals": {
            "state_names": diagnostic_names,
            "state_indices": diagnostics.antenna_indices.tolist(),
            "nuisance_intercept_deg": diagnostics.nuisance_intercept_deg.tolist(),
            "residual_phase_deg": diagnostics.residual_phase_deg.tolist(),
            "antenna_weighted_rms_deg": diagnostics.antenna_weighted_rms_deg.tolist(),
            "frequency_weighted_rms_deg": diagnostics.frequency_weighted_rms_deg.tolist(),
            "overall_weighted_rms_deg": diagnostics.overall_weighted_rms_deg,
            "maximum_absolute_residual_deg": diagnostics.maximum_absolute_residual_deg,
            "valid_mask": diagnostics.valid_mask.tolist(),
        },
        "output_particles": _particle_downsample(
            posterior,
            seed=particle_seed,
        ),
    }


def _profile_input_rows(inputs: ExtractedSlopeInputs) -> list[dict[str, Any]]:
    rows = []
    for index, (center_frequency_hz, carrier_frequency_hz) in enumerate(
        zip(inputs.center_frequency_hz, inputs.carrier_frequency_hz, strict=True)
    ):
        rows.append(
            {
                "center_frequency_hz": int(center_frequency_hz),
                "carrier_frequency_hz": float(carrier_frequency_hz),
                "state_names": list(inputs.state_names),
                "valid_mask": inputs.valid_mask[index].tolist(),
                "circular_mean_double_relative_phase_deg": inputs.mean_phase_deg[index].tolist(),
                "circular_repeat_standard_deviation_deg": (
                    inputs.repeat_standard_deviation_deg[index].tolist()
                ),
                "aggregate_analyzer_standard_error_deg": (
                    inputs.analyzer_standard_error_deg[index].tolist()
                ),
                "statistical_phase_standard_deviation_deg": (
                    inputs.statistical_standard_deviation_deg[index].tolist()
                ),
            }
        )
    return rows


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1_000 <= args.sample_count <= 2_000_000:
        raise AnalysisDocumentError("sample count must lie within 1000..2000000")
    if (
        not isfinite(args.systematic_phase_std_deg)
        or not 0.0 <= args.systematic_phase_std_deg <= 180.0
    ):
        raise AnalysisDocumentError("systematic phase standard deviation must lie within [0, 180]")
    anchor = np.asarray((args.tx1_anchor_x_mm, args.tx1_anchor_y_mm), dtype=np.float64)
    if not np.all(np.isfinite(anchor)):
        raise AnalysisDocumentError("TX1 anchor coordinates must be finite")
    analysis_path = args.analysis.expanduser().resolve(strict=True)
    output_path = args.output.expanduser().resolve()
    if output_path == analysis_path:
        raise AnalysisDocumentError("output must not overwrite the source analysis")
    try:
        document = _mapping(
            json.loads(analysis_path.read_text(encoding="utf-8")),
            str(analysis_path),
        )
    except json.JSONDecodeError as error:
        raise AnalysisDocumentError(f"source analysis is not valid JSON: {error}") from error
    excluded_center_frequency_hz = tuple(args.exclude_center_frequency_hz or ())
    inputs = _extract_inputs(
        document,
        excluded_center_frequency_hz=excluded_center_frequency_hz,
    )
    prior = Tx2RadialPrior(mean_mm=304.8, standard_deviation_mm=50.0)
    likelihood = FrequencySlopeLikelihood(
        systematic_phase_std_deg=args.systematic_phase_std_deg,
        minimum_phase_std_deg=MINIMUM_STATISTICAL_STD_DEG,
    )
    try:
        posterior = infer_anchored_tx2_frequency_slope(
            inputs.measurements,
            inputs.geometry,
            fixed_tx1_position_mm=anchor,
            reference_index=inputs.reference_index,
            sample_count=args.sample_count,
            seed=args.seed,
            prior=prior,
            likelihood=likelihood,
        )
    except ValueError as error:
        raise AnalysisDocumentError(f"anchored slope inference failed: {error}") from error
    output_document = {
        "schema": 1,
        "analysis_kind": "anchored_multifrequency_tx2_phase_slope_localization",
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "repository": str(REPOSITORY),
            "git_commit": _source_commit(),
            "analysis_path": str(analysis_path),
            "analysis_sha256": _sha256(analysis_path),
            "analysis_schema": document.get("schema"),
            "analysis_kind": document.get("analysis_kind"),
            "analysis_created_at": document.get("created_at"),
            "upstream_provenance": document.get("source"),
            "frequency_selection": {
                "source_center_frequencies_hz": inputs.source_center_frequency_hz.tolist(),
                "excluded_center_frequencies_hz": list(inputs.excluded_center_frequency_hz),
                "used_center_frequencies_hz": inputs.center_frequency_hz.tolist(),
            },
        },
        "analysis_configuration": {
            "sample_count": args.sample_count,
            "seed": args.seed,
            "systematic_phase_standard_deviation_deg": args.systematic_phase_std_deg,
            "systematic_floor_application_count": 1,
            "statistical_uncertainty_formula": (
                "sqrt(circular_repeat_standard_deviation_deg^2 + "
                "aggregate_analyzer_standard_error_deg^2)"
            ),
            "minimum_statistical_standard_deviation_deg": MINIMUM_STATISTICAL_STD_DEG,
            "upstream_direct_path_systematic_floors_used": False,
            "tx1_anchor_position_mm": anchor.tolist(),
            "reference_state": REFERENCE_STATE,
            "reference_index": inputs.reference_index,
            "radial_prior": asdict(prior),
            "maximum_output_particle_count": MAXIMUM_OUTPUT_PARTICLES,
            "source_center_frequencies_hz": inputs.source_center_frequency_hz.tolist(),
            "excluded_center_frequencies_hz": list(inputs.excluded_center_frequency_hz),
            "used_center_frequencies_hz": inputs.center_frequency_hz.tolist(),
        },
        "inputs": {
            "source_frequency_profile_count": int(inputs.source_center_frequency_hz.size),
            "frequency_profile_count": inputs.measurements.frequency_count,
            "state_names": list(inputs.state_names),
            "source_center_frequencies_hz": inputs.source_center_frequency_hz.tolist(),
            "source_carrier_frequencies_hz": inputs.source_carrier_frequency_hz.tolist(),
            "excluded_center_frequencies_hz": list(inputs.excluded_center_frequency_hz),
            "used_center_frequencies_hz": inputs.center_frequency_hz.tolist(),
            "carrier_frequencies_hz": inputs.carrier_frequency_hz.tolist(),
            "geometry": {
                "inference_center_mm": inputs.geometry.center_mm.tolist(),
                "selected_antenna_positions_mm": {
                    name: inputs.geometry.antenna_positions_mm[index].tolist()
                    for index, name in enumerate(inputs.state_names)
                },
            },
            "frequency_profile_rows": _profile_input_rows(inputs),
        },
        "localization": {
            "model": (
                "planar direct-path double-relative TX2-minus-TX1 phase slope with fixed TX1; "
                "one frequency-independent circular phase intercept is marginalized for each "
                "non-reference receive antenna"
            ),
            "repeat_handling": (
                "each unique carrier contributes one circularly aggregated profile; capture "
                "repeats are not independent posterior rows"
            ),
            "posterior": _posterior_document(
                posterior,
                inputs,
                particle_seed=args.seed ^ 0x51_0F_EE,
            ),
        },
    }
    _atomic_write(output_path, output_document)
    return {
        "output": str(output_path),
        "source_analysis": str(analysis_path),
        "frequency_profiles": inputs.measurements.frequency_count,
        "excluded_center_frequencies_hz": list(inputs.excluded_center_frequency_hz),
        "used_center_frequencies_hz": inputs.center_frequency_hz.tolist(),
        "selected_antennas": list(inputs.state_names),
        "effective_sample_size": posterior.effective_sample_size,
        "tx2_map_position_mm": list(posterior.tx2.map_position_mm),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except (AnalysisDocumentError, FileNotFoundError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
