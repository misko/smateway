#!/usr/bin/env python3
"""Reproduce the committed, offline 5.8 GHz frequency-domain RCA evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPEATABILITY = (
    REPOSITORY_ROOT
    / "docs/closed_loop_frequency_sweep_repeatability/data"
    / "rotation0-repeatability-20pass-results.json"
)
DEFAULT_PERMUTATION = (
    REPOSITORY_ROOT
    / "docs/closed_loop_permutation_calibration/data/closed-loop-calibration-results.json"
)
DEFAULT_SNAPSHOT = (
    REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/data/frequency-domain-observations.json"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/data/frequency-domain-analysis.json"
)
DEFAULT_FIGURE_DIRECTORY = REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/png"

HIGH_BAND_FREQUENCIES_HZ = tuple(range(3_600_000_000, 5_800_000_001, 100_000_000))
HIGH_BAND_MIN_HZ = HIGH_BAND_FREQUENCIES_HZ[0]
HIGH_BAND_MAX_HZ = HIGH_BAND_FREQUENCIES_HZ[-1]
FREQUENCY_STEP_HZ = 100_000_000
DELAY_ALIAS_PERIOD_S = 1.0 / FREQUENCY_STEP_HZ
EXPECTED_REPEAT_COUNT = 20
EXPECTED_PERMUTATION_ARTIFACTS = {
    "rotation_1": "bcd1bcc3d46742c289b77487598ebdd6",
    "rotation_2": "3f9d9dbb249041e18906273c88c7b879",
    "rotation_0_restored": "089c96119d6b4daeb3a0004de7796c62",
}

# PE42482 DOC-75785-4, table 3.  These are deliberately a
# datasheet-conditioned planning bound, not an assembled-board measurement.
PE42482_TWO_TO_FOUR_MINIMUM_ISOLATION_DB = (
    34.0,
    38.0,
    38.0,
    45.0,
    45.0,
    38.0,
    38.0,
    34.0,
)
PE42482_TWO_TO_FOUR_MAXIMUM_INSERTION_LOSS_DB = (
    1.5,
    1.6,
    1.7,
    1.9,
    1.9,
    1.7,
    1.6,
    1.5,
)
PE42482_MINIMUM_ISOLATION_DB = (29.0, 30.0, 33.0, 38.0, 38.0, 33.0, 30.0, 29.0)
PE42482_MAXIMUM_INSERTION_LOSS_DB = (1.9, 2.3, 2.2, 2.2, 2.2, 2.2, 2.3, 1.9)
PE42482_DATASHEET_ID = "DOC-75785-4 (09/2023)"
PE42482_DATASHEET_PAGE = 5

MINIMUM_ALIGNMENT_SCORE = 0.95
T_CRITICAL_95_N20 = 2.093024054


class FrequencyDomainAnalysisError(RuntimeError):
    """Committed evidence is absent, inconsistent, or insufficient."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeatability", type=Path, default=DEFAULT_REPEATABILITY)
    parser.add_argument("--permutation", type=Path, default=DEFAULT_PERMUTATION)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--figure-directory", type=Path, default=DEFAULT_FIGURE_DIRECTORY)
    parser.add_argument(
        "--refresh-observation-snapshot-from-capture-root",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "explicit maintainer-only refresh from local sidecars; normal reproduction reads "
            "only the committed compact snapshot"
        ),
    )
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FrequencyDomainAnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FrequencyDomainAnalysisError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrequencyDomainAnalysisError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FrequencyDomainAnalysisError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrequencyDomainAnalysisError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FrequencyDomainAnalysisError(f"{label} must be a nonempty string")
    return value


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrequencyDomainAnalysisError(f"cannot load {label} {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FrequencyDomainAnalysisError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _reported_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(resolved)


def _complex(value: object, label: str) -> complex:
    document = _mapping(value, label)
    return complex(
        _number(document.get("real"), f"{label} real"),
        _number(document.get("imag"), f"{label} imag"),
    )


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _sample_std(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def paired_summary(values: Sequence[float]) -> dict[str, float | int | list[float]]:
    """Summarize the fixed twenty-run paired design used by this RCA."""

    if len(values) != EXPECTED_REPEAT_COUNT:
        raise FrequencyDomainAnalysisError(
            f"paired statistic requires exactly {EXPECTED_REPEAT_COUNT} observations"
        )
    finite_values = [_number(value, "paired value") for value in values]
    mean = statistics.fmean(finite_values)
    sample_std = statistics.stdev(finite_values)
    half_width = T_CRITICAL_95_N20 * sample_std / math.sqrt(EXPECTED_REPEAT_COUNT)
    return {
        "count": EXPECTED_REPEAT_COUNT,
        "mean": mean,
        "sample_standard_deviation": sample_std,
        "minimum": min(finite_values),
        "maximum": max(finite_values),
        "mean_95_percent_confidence_interval": [mean - half_width, mean + half_width],
    }


def fit_single_delay(
    frequencies_hz: Sequence[int],
    response: Sequence[complex],
    *,
    grid_step_ps: float = 0.2,
) -> dict[str, Any]:
    """Fit a constant complex amplitude and one delay over its sampling alias interval."""

    frequencies = np.asarray(frequencies_hz, dtype=np.float64)
    values = np.asarray(response, dtype=np.complex128)
    if frequencies.ndim != 1 or values.ndim != 1 or len(frequencies) != len(values):
        raise FrequencyDomainAnalysisError(
            "single-delay inputs must be equal one-dimensional arrays"
        )
    if len(values) < 3 or np.any(~np.isfinite(frequencies)) or np.any(~np.isfinite(values)):
        raise FrequencyDomainAnalysisError("single-delay fit requires at least three finite values")
    spacing = np.diff(frequencies)
    if np.any(spacing <= 0.0) or not np.allclose(spacing, spacing[0], rtol=0.0, atol=0.5):
        raise FrequencyDomainAnalysisError("single-delay fit requires a uniform increasing grid")
    if grid_step_ps <= 0.0 or not math.isfinite(grid_step_ps):
        raise FrequencyDomainAnalysisError("delay grid step must be positive and finite")
    power = float(np.vdot(values, values).real)
    if power <= 0.0:
        raise FrequencyDomainAnalysisError("single-delay response must contain nonzero energy")

    alias_period_s = 1.0 / float(spacing[0])
    step_s = grid_step_ps * 1e-12
    delays = np.arange(0.0, alias_period_s, step_s, dtype=np.float64)
    offsets = frequencies - frequencies[0]
    basis = np.exp(-2j * np.pi * offsets[:, np.newaxis] * delays[np.newaxis, :])
    correlations = basis.conj().T @ values
    best_index = int(np.argmax(np.abs(correlations)))
    amplitude = correlations[best_index] / len(values)
    prediction = basis[:, best_index] * amplitude
    residual = values - prediction
    residual_power = float(np.vdot(residual, residual).real)
    magnitude_error_db = 20.0 * np.log10(np.abs(prediction) / np.abs(values))
    phase_error_deg = np.angle(prediction / values, deg=True)
    return {
        "model": "constant_complex_amplitude_times_single_delay",
        "delay_ns_modulo_alias_period": float(delays[best_index] * 1e9),
        "delay_alias_period_ns": float(alias_period_s * 1e9),
        "delay_grid_step_ps": grid_step_ps,
        "complex_amplitude": _complex_document(complex(amplitude)),
        "complex_amplitude_magnitude": float(abs(amplitude)),
        "complex_amplitude_phase_deg": float(np.angle(amplitude, deg=True)),
        "complex_nrmse": math.sqrt(residual_power / power),
        "magnitude_error_rms_db": float(np.sqrt(np.mean(np.square(magnitude_error_db)))),
        "phase_error_rms_deg": float(np.sqrt(np.mean(np.square(phase_error_deg)))),
        "prediction": [_complex_document(complex(value)) for value in prediction],
    }


def hankel_diagnostics(response: Sequence[complex]) -> dict[str, Any]:
    """Return model-order diagnostics for a uniformly sampled complex response."""

    values = np.asarray(response, dtype=np.complex128)
    if values.ndim != 1 or len(values) < 7 or np.any(~np.isfinite(values)):
        raise FrequencyDomainAnalysisError("Hankel analysis requires at least seven finite values")
    row_count = (len(values) + 1) // 2
    column_count = len(values) - row_count + 1
    matrix = np.asarray(
        [[values[row + column] for column in range(column_count)] for row in range(row_count)]
    )
    singular_values = np.linalg.svd(matrix, compute_uv=False)
    energy = np.square(singular_values)
    energy_fraction = energy / np.sum(energy)
    return {
        "shape": [row_count, column_count],
        "singular_values": [float(value) for value in singular_values],
        "singular_values_relative_to_first": [
            float(value / singular_values[0]) for value in singular_values
        ],
        "energy_fractions": [float(value) for value in energy_fraction],
        "rank_one_explained_energy_fraction": float(energy_fraction[0]),
        "rank_one_relative_frobenius_residual": float(
            math.sqrt(float(np.sum(energy[1:]) / np.sum(energy)))
        ),
    }


def selector_coherent_bound(
    selected_amplitudes: Sequence[float],
    minimum_isolation_db: Sequence[float] = PE42482_MINIMUM_ISOLATION_DB,
    maximum_insertion_loss_db: Sequence[float] = PE42482_MAXIMUM_INSERTION_LOSS_DB,
) -> float:
    """Return the perfect-phase-sum voltage bound under PE42482 datasheet limits."""

    selected = np.asarray(selected_amplitudes, dtype=np.float64)
    isolation = np.asarray(minimum_isolation_db, dtype=np.float64)
    insertion_loss = np.asarray(maximum_insertion_loss_db, dtype=np.float64)
    if selected.shape != (8,) or isolation.shape != (8,) or insertion_loss.shape != (8,):
        raise FrequencyDomainAnalysisError("selector bound requires exactly eight paths")
    if (
        np.any(~np.isfinite(selected))
        or np.any(~np.isfinite(isolation))
        or np.any(~np.isfinite(insertion_loss))
        or np.any(selected < 0.0)
    ):
        raise FrequencyDomainAnalysisError("selector bound inputs must be finite and nonnegative")
    return float(np.sum(selected * np.power(10.0, -(isolation - insertion_loss) / 20.0)))


def _phasor_from_transfer(value: object, label: str) -> complex:
    transfer = _mapping(value, label)
    return _complex(transfer.get("phasor"), f"{label} phasor")


def _validate_repeatability(
    document: Mapping[str, Any],
) -> dict[str, dict[int, Mapping[str, Any]]]:
    if document.get("schema") != 1:
        raise FrequencyDomainAnalysisError("repeatability schema must be 1")
    if document.get("analysis_kind") != "rotation0_broadband_repeatability":
        raise FrequencyDomainAnalysisError("unexpected repeatability analysis kind")
    scope = _mapping(document.get("scope"), "repeatability scope")
    if (
        _integer(scope.get("frequency_min_hz"), "repeatability minimum frequency") != 2_100_000_000
        or _integer(scope.get("frequency_max_hz"), "repeatability maximum frequency")
        != HIGH_BAND_MAX_HZ
        or _integer(scope.get("frequency_step_hz"), "repeatability frequency step")
        != FREQUENCY_STEP_HZ
    ):
        raise FrequencyDomainAnalysisError("repeatability sweep grid differs from the RCA contract")
    source_runs = _sequence(document.get("source_runs"), "repeatability source runs")
    if len(source_runs) != EXPECTED_REPEAT_COUNT + 1:
        raise FrequencyDomainAnalysisError(
            "repeatability input must contain baseline plus 20 repeats"
        )
    by_label: dict[str, dict[int, Mapping[str, Any]]] = {}
    for raw_run in source_runs[1:]:
        run = _mapping(raw_run, "repeatability source run")
        label = _string(run.get("label"), "repeatability run label")
        if label in by_label:
            raise FrequencyDomainAnalysisError(f"duplicate repeatability run label {label}")
        sources = _sequence(run.get("source_analyses"), f"{label} source analyses")
        by_frequency = {
            _integer(
                _mapping(item, "source analysis").get("center_frequency_hz"), "frequency"
            ): _mapping(item, "source analysis")
            for item in sources
        }
        if not set(HIGH_BAND_FREQUENCIES_HZ).issubset(by_frequency):
            raise FrequencyDomainAnalysisError(f"{label} lacks high-band source identities")
        by_label[label] = by_frequency
    return by_label


def _validate_permutation(document: Mapping[str, Any]) -> set[str]:
    if document.get("schema") != 1:
        raise FrequencyDomainAnalysisError("permutation schema must be 1")
    if document.get("analysis_kind") != "fast20_closed_loop_permutation_calibration":
        raise FrequencyDomainAnalysisError("unexpected permutation analysis kind")
    frequencies = _sequence(document.get("frequency_results"), "permutation frequencies")
    exact = [
        _mapping(item, "permutation frequency")
        for item in frequencies
        if _mapping(item, "permutation frequency").get("center_frequency_hz") == HIGH_BAND_MAX_HZ
    ]
    if len(exact) != 1:
        raise FrequencyDomainAnalysisError("permutation input must contain one 5.8 GHz result")
    ids = set(
        _string(value, "permutation artifact ID")
        for value in _mapping(
            exact[0].get("fit_artifact_ids_by_rotation"), "fit artifacts"
        ).values()
    )
    if ids != set(EXPECTED_PERMUTATION_ARTIFACTS.values()):
        raise FrequencyDomainAnalysisError("5.8 GHz permutation artifact identities changed")
    return ids


def _build_snapshot(
    repeatability_path: Path,
    repeatability: Mapping[str, Any],
    permutation_path: Path,
    permutation: Mapping[str, Any],
    capture_root: Path,
) -> dict[str, Any]:
    """Maintainer-only derivation of a compact snapshot from retained local sidecars."""

    repeat_sources = _validate_repeatability(repeatability)
    _validate_permutation(permutation)
    repeat_runs = []
    frequency_selected: dict[int, list[list[float]]] = {
        frequency: [[] for _ in range(8)] for frequency in HIGH_BAND_FREQUENCIES_HZ
    }
    for label, by_frequency in repeat_sources.items():
        observations = []
        for frequency in HIGH_BAND_FREQUENCIES_HZ:
            identity = by_frequency[frequency]
            artifact_id = _string(identity.get("artifact_id"), "artifact ID")
            sidecar = capture_root / artifact_id / "fast20-reference-transfer.json"
            if _sha256(sidecar) != _string(identity.get("analysis_sha256"), "analysis SHA-256"):
                raise FrequencyDomainAnalysisError(f"sidecar hash differs for {artifact_id}")
            analysis = _read_json(sidecar, f"sidecar {artifact_id}")
            if analysis.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer":
                raise FrequencyDomainAnalysisError(f"unsupported sidecar kind for {artifact_id}")
            transfer = _mapping(analysis.get("transfer"), "transfer")
            if (
                _number(transfer.get("alignment_score"), "alignment score")
                < MINIMUM_ALIGNMENT_SCORE
            ):
                raise FrequencyDomainAnalysisError(
                    f"high-band sidecar {artifact_id} is not admitted"
                )
            all_off = _mapping(transfer.get("all_off"), "ALL_OFF")
            all_off_raw = _mapping(all_off.get("raw_rx2_over_rx1"), "ALL_OFF raw transfer")
            all_off_phasor = _phasor_from_transfer(all_off_raw, "ALL_OFF raw transfer")
            selected_amplitudes = []
            raw_contrasts = []
            states = _sequence(transfer.get("states"), "transfer states")
            if len(states) != 8:
                raise FrequencyDomainAnalysisError(f"{artifact_id} does not contain eight states")
            for index, raw_state in enumerate(states):
                state = _mapping(raw_state, "transfer state")
                if state.get("name") != f"ANT{index + 1}":
                    raise FrequencyDomainAnalysisError(f"{artifact_id} state order changed")
                selected = _mapping(
                    state.get("all_off_subtracted_rx2_over_rx1"), "selected transfer"
                )
                amplitude = _number(selected.get("amplitude"), "selected amplitude")
                if amplitude <= 0.0:
                    raise FrequencyDomainAnalysisError("selected amplitude must be positive")
                selected_amplitudes.append(amplitude)
                frequency_selected[frequency][index].append(amplitude)
                raw = _mapping(state.get("raw_rx2_over_rx1"), "raw selected transfer")
                raw_amplitude = _number(raw.get("amplitude"), "raw selected amplitude")
                raw_contrasts.append(20.0 * math.log10(raw_amplitude / abs(all_off_phasor)))
            observations.append(
                {
                    "center_frequency_hz": frequency,
                    "artifact_id": artifact_id,
                    "analysis_sha256": _string(identity.get("analysis_sha256"), "analysis SHA"),
                    "all_off": _complex_document(all_off_phasor),
                    "selected_subtracted_median_amplitude": float(
                        statistics.median(selected_amplitudes)
                    ),
                    "raw_selected_to_all_off_median_contrast_db": float(
                        statistics.median(raw_contrasts)
                    ),
                }
            )
        repeat_runs.append({"label": label, "observations": observations})

    source_documents = {
        _string(
            _mapping(item, "source document").get("artifact_id"), "source artifact ID"
        ): _mapping(item, "source document")
        for item in _sequence(permutation.get("source_documents"), "permutation sources")
    }
    permutation_rows = []
    labels_by_artifact = {value: key for key, value in EXPECTED_PERMUTATION_ARTIFACTS.items()}
    for artifact_id in EXPECTED_PERMUTATION_ARTIFACTS.values():
        permutation_identity = source_documents.get(artifact_id)
        if permutation_identity is None:
            raise FrequencyDomainAnalysisError(f"permutation source lacks {artifact_id}")
        sidecar = capture_root / artifact_id / "fast20-reference-transfer.json"
        if _sha256(sidecar) != _string(
            permutation_identity.get("analysis_sha256"), "analysis SHA-256"
        ):
            raise FrequencyDomainAnalysisError(
                f"permutation sidecar hash differs for {artifact_id}"
            )
        analysis = _read_json(sidecar, f"permutation sidecar {artifact_id}")
        transfer = _mapping(analysis.get("transfer"), "permutation transfer")
        all_off = _mapping(transfer.get("all_off"), "permutation ALL_OFF")
        all_off_phasor = _phasor_from_transfer(
            _mapping(all_off.get("raw_rx2_over_rx1"), "permutation ALL_OFF raw"),
            "permutation ALL_OFF raw",
        )
        selected_phasors = []
        for raw_state in _sequence(transfer.get("states"), "permutation states"):
            state = _mapping(raw_state, "permutation state")
            selected_phasors.append(
                _phasor_from_transfer(
                    state.get("all_off_subtracted_rx2_over_rx1"), "permutation selected"
                )
            )
        artifact = _mapping(analysis.get("artifact"), "permutation artifact")
        permutation_rows.append(
            {
                "label": labels_by_artifact[artifact_id],
                "artifact_id": artifact_id,
                "analysis_sha256": _string(
                    permutation_identity.get("analysis_sha256"), "analysis SHA"
                ),
                "created_at": _string(artifact.get("created_at"), "artifact creation time"),
                "all_off": _complex_document(all_off_phasor),
                "selected_coherent_sum": _complex_document(sum(selected_phasors)),
                "selected_subtracted_amplitudes": [float(abs(value)) for value in selected_phasors],
            }
        )

    return {
        "schema": 1,
        "evidence_kind": "5g8_frequency_domain_compact_observation_snapshot",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "frequency_min_hz": HIGH_BAND_MIN_HZ,
            "frequency_max_hz": HIGH_BAND_MAX_HZ,
            "frequency_step_hz": FREQUENCY_STEP_HZ,
            "repeat_count": EXPECTED_REPEAT_COUNT,
            "note": "all 20 repeats are unambiguously aligned at every retained center",
        },
        "sources": {
            "repeatability": {
                "path": str(repeatability_path.relative_to(REPOSITORY_ROOT)),
                "sha256": _sha256(repeatability_path),
            },
            "permutation": {
                "path": str(permutation_path.relative_to(REPOSITORY_ROOT)),
                "sha256": _sha256(permutation_path),
            },
        },
        "repeat_runs": repeat_runs,
        "frequency_selected_subtracted_mean_amplitudes": [
            {
                "center_frequency_hz": frequency,
                "amplitudes": [
                    statistics.fmean(values) for values in frequency_selected[frequency]
                ],
            }
            for frequency in HIGH_BAND_FREQUENCIES_HZ
        ],
        "permutation_5g8": permutation_rows,
    }


def _load_snapshot(
    snapshot_path: Path,
    repeatability_path: Path,
    repeatability: Mapping[str, Any],
    permutation_path: Path,
    permutation: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = _read_json(snapshot_path, "frequency-domain observation snapshot")
    if snapshot.get("schema") != 1:
        raise FrequencyDomainAnalysisError("observation snapshot schema must be 1")
    if snapshot.get("evidence_kind") != "5g8_frequency_domain_compact_observation_snapshot":
        raise FrequencyDomainAnalysisError("unexpected observation snapshot kind")
    sources = _mapping(snapshot.get("sources"), "snapshot sources")
    for name, path in (("repeatability", repeatability_path), ("permutation", permutation_path)):
        source = _mapping(sources.get(name), f"snapshot {name} source")
        if source.get("sha256") != _sha256(path):
            raise FrequencyDomainAnalysisError(f"snapshot {name} source hash differs")

    repeat_sources = _validate_repeatability(repeatability)
    permutation_ids = _validate_permutation(permutation)
    raw_runs = _sequence(snapshot.get("repeat_runs"), "snapshot repeat runs")
    if len(raw_runs) != EXPECTED_REPEAT_COUNT:
        raise FrequencyDomainAnalysisError("snapshot must contain exactly 20 repeat runs")
    runs = []
    for raw_run in raw_runs:
        run = _mapping(raw_run, "snapshot repeat run")
        label = _string(run.get("label"), "snapshot run label")
        if label not in repeat_sources:
            raise FrequencyDomainAnalysisError(
                f"snapshot run {label} is not in repeatability input"
            )
        observations = []
        seen_frequencies = set()
        for raw_observation in _sequence(run.get("observations"), f"{label} observations"):
            observation = _mapping(raw_observation, "snapshot observation")
            frequency = _integer(observation.get("center_frequency_hz"), "snapshot frequency")
            if frequency in seen_frequencies:
                raise FrequencyDomainAnalysisError(f"snapshot {label} duplicates {frequency} Hz")
            seen_frequencies.add(frequency)
            identity = repeat_sources[label].get(frequency)
            if identity is None:
                raise FrequencyDomainAnalysisError(f"snapshot {label} frequency is not a source")
            if observation.get("artifact_id") != identity.get("artifact_id"):
                raise FrequencyDomainAnalysisError(f"snapshot {label} artifact identity differs")
            if observation.get("analysis_sha256") != identity.get("analysis_sha256"):
                raise FrequencyDomainAnalysisError(f"snapshot {label} analysis hash differs")
            all_off = _complex(observation.get("all_off"), "snapshot ALL_OFF")
            if abs(all_off) <= 0.0:
                raise FrequencyDomainAnalysisError("snapshot ALL_OFF amplitude must be positive")
            observations.append(
                {
                    "center_frequency_hz": frequency,
                    "artifact_id": observation.get("artifact_id"),
                    "all_off": all_off,
                    "selected_median_amplitude": _number(
                        observation.get("selected_subtracted_median_amplitude"),
                        "selected median amplitude",
                    ),
                    "median_raw_contrast_db": _number(
                        observation.get("raw_selected_to_all_off_median_contrast_db"),
                        "median raw contrast",
                    ),
                }
            )
        if seen_frequencies != set(HIGH_BAND_FREQUENCIES_HZ):
            raise FrequencyDomainAnalysisError(f"snapshot {label} frequency grid differs")
        runs.append({"label": label, "observations": observations})

    selected_rows = _sequence(
        snapshot.get("frequency_selected_subtracted_mean_amplitudes"),
        "selected mean amplitudes",
    )
    selected_by_frequency: dict[int, list[float]] = {}
    for raw_row in selected_rows:
        row = _mapping(raw_row, "selected mean row")
        frequency = _integer(row.get("center_frequency_hz"), "selected mean frequency")
        amplitudes = [
            _number(value, "selected mean amplitude")
            for value in _sequence(row.get("amplitudes"), "selected amplitudes")
        ]
        if len(amplitudes) != 8 or any(value <= 0.0 for value in amplitudes):
            raise FrequencyDomainAnalysisError(
                "selected mean row must contain eight positive amplitudes"
            )
        selected_by_frequency[frequency] = amplitudes
    if set(selected_by_frequency) != set(HIGH_BAND_FREQUENCIES_HZ):
        raise FrequencyDomainAnalysisError("selected mean frequency grid differs")

    permutation_rows = []
    seen_permutation_ids = set()
    for raw_row in _sequence(snapshot.get("permutation_5g8"), "snapshot permutations"):
        row = _mapping(raw_row, "snapshot permutation")
        artifact_id = _string(row.get("artifact_id"), "snapshot permutation artifact ID")
        if artifact_id not in permutation_ids or artifact_id in seen_permutation_ids:
            raise FrequencyDomainAnalysisError("snapshot permutation artifact identity differs")
        seen_permutation_ids.add(artifact_id)
        amplitudes = [
            _number(value, "permutation selected amplitude")
            for value in _sequence(
                row.get("selected_subtracted_amplitudes"), "permutation amplitudes"
            )
        ]
        if len(amplitudes) != 8 or any(value <= 0.0 for value in amplitudes):
            raise FrequencyDomainAnalysisError("permutation must contain eight positive amplitudes")
        permutation_rows.append(
            {
                "label": _string(row.get("label"), "permutation label"),
                "artifact_id": artifact_id,
                "created_at": _string(row.get("created_at"), "permutation creation time"),
                "all_off": _complex(row.get("all_off"), "permutation ALL_OFF"),
                "selected_sum": _complex(
                    row.get("selected_coherent_sum"), "permutation selected sum"
                ),
                "selected_amplitudes": amplitudes,
            }
        )
    if seen_permutation_ids != permutation_ids:
        raise FrequencyDomainAnalysisError("snapshot does not contain all permutation artifacts")
    return {
        "runs": runs,
        "selected_by_frequency": selected_by_frequency,
        "permutation_rows": permutation_rows,
        "snapshot_sha256": _sha256(snapshot_path),
    }


def analyze(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    runs = list(_sequence(snapshot.get("runs"), "loaded runs"))
    if len(runs) != EXPECTED_REPEAT_COUNT:
        raise FrequencyDomainAnalysisError("analysis requires exactly twenty runs")
    per_frequency: dict[int, dict[str, Any]] = {}
    all_off_matrix = np.empty(
        (EXPECTED_REPEAT_COUNT, len(HIGH_BAND_FREQUENCIES_HZ)), dtype=np.complex128
    )
    selected_matrix = np.empty_like(all_off_matrix, dtype=np.float64)
    contrast_matrix = np.empty_like(all_off_matrix, dtype=np.float64)
    for run_index, raw_run in enumerate(runs):
        run = _mapping(raw_run, "loaded run")
        by_frequency = {
            _integer(
                _mapping(item, "loaded observation").get("center_frequency_hz"), "frequency"
            ): _mapping(item, "loaded observation")
            for item in _sequence(run.get("observations"), "loaded observations")
        }
        for frequency_index, frequency in enumerate(HIGH_BAND_FREQUENCIES_HZ):
            observation = by_frequency[frequency]
            all_off_matrix[run_index, frequency_index] = complex(observation["all_off"])
            selected_matrix[run_index, frequency_index] = _number(
                observation.get("selected_median_amplitude"), "loaded selected median"
            )
            contrast_matrix[run_index, frequency_index] = _number(
                observation.get("median_raw_contrast_db"), "loaded raw contrast"
            )

    all_off_mean = np.mean(all_off_matrix, axis=0)
    selected_means = np.mean(selected_matrix, axis=0)
    for index, frequency in enumerate(HIGH_BAND_FREQUENCIES_HZ):
        magnitudes = np.abs(all_off_matrix[:, index])
        relative_phase = np.angle(all_off_matrix[:, index] / all_off_mean[index], deg=True)
        per_frequency[frequency] = {
            "center_frequency_hz": frequency,
            "all_off_mean": _complex_document(complex(all_off_mean[index])),
            "all_off_mean_amplitude": float(abs(all_off_mean[index])),
            "all_off_mean_amplitude_db": float(20.0 * math.log10(abs(all_off_mean[index]))),
            "all_off_amplitude_sample_standard_deviation": _sample_std(magnitudes.tolist()),
            "all_off_phase_deg": float(np.angle(all_off_mean[index], deg=True)),
            "all_off_phase_sample_standard_deviation_deg": _sample_std(relative_phase.tolist()),
            "selected_subtracted_median_amplitude_mean": float(selected_means[index]),
            "selected_subtracted_median_amplitude_mean_db": float(
                20.0 * math.log10(selected_means[index])
            ),
            "raw_selected_to_all_off_median_contrast_mean_db": float(
                np.mean(contrast_matrix[:, index])
            ),
        }

    index_57 = HIGH_BAND_FREQUENCIES_HZ.index(5_700_000_000)
    index_58 = HIGH_BAND_FREQUENCIES_HZ.index(5_800_000_000)
    all_off_delta = 20.0 * np.log10(
        np.abs(all_off_matrix[:, index_58]) / np.abs(all_off_matrix[:, index_57])
    )
    selected_delta = 20.0 * np.log10(selected_matrix[:, index_58] / selected_matrix[:, index_57])
    contrast_delta = contrast_matrix[:, index_58] - contrast_matrix[:, index_57]

    single_delay = fit_single_delay(HIGH_BAND_FREQUENCIES_HZ, all_off_mean.tolist())
    prediction = np.asarray(
        [
            complex(item["real"], item["imag"])
            for item in _sequence(single_delay.get("prediction"), "single-delay prediction")
        ]
    )
    single_delay["five_point_eight_prediction_error_db"] = float(
        20.0 * math.log10(abs(prediction[-1]) / abs(all_off_mean[-1]))
    )
    per_run_single_delay = [
        fit_single_delay(HIGH_BAND_FREQUENCIES_HZ, row.tolist(), grid_step_ps=1.0)
        for row in all_off_matrix
    ]
    single_delay["run_to_run"] = {
        "delay_ns_modulo_alias_period_mean": statistics.fmean(
            _number(item["delay_ns_modulo_alias_period"], "delay") for item in per_run_single_delay
        ),
        "delay_ns_modulo_alias_period_sample_standard_deviation": _sample_std(
            [
                _number(item["delay_ns_modulo_alias_period"], "delay")
                for item in per_run_single_delay
            ]
        ),
        "complex_nrmse_mean": statistics.fmean(
            _number(item["complex_nrmse"], "NRMSE") for item in per_run_single_delay
        ),
        "complex_nrmse_sample_standard_deviation": _sample_std(
            [_number(item["complex_nrmse"], "NRMSE") for item in per_run_single_delay]
        ),
    }

    hankel = hankel_diagnostics(all_off_mean.tolist())
    per_run_hankel = [hankel_diagnostics(row.tolist()) for row in all_off_matrix]
    for index in range(1, 6):
        values = [
            _number(
                _sequence(item["singular_values_relative_to_first"], "singular values")[index],
                "singular-value ratio",
            )
            for item in per_run_hankel
        ]
        hankel.setdefault("run_to_run_singular_value_ratios", []).append(
            {
                "singular_value_index_one_based": index + 1,
                "mean": statistics.fmean(values),
                "sample_standard_deviation": _sample_std(values),
                "minimum": min(values),
                "maximum": max(values),
            }
        )
    run_deviation = np.linalg.norm(all_off_matrix - all_off_mean, axis=1) / np.linalg.norm(
        all_off_mean
    )
    hankel["run_complex_vector_relative_l2_deviation"] = {
        "mean": float(np.mean(run_deviation)),
        "sample_standard_deviation": float(np.std(run_deviation, ddof=1)),
        "minimum": float(np.min(run_deviation)),
        "maximum": float(np.max(run_deviation)),
    }

    step_rows = []
    for index in range(len(HIGH_BAND_FREQUENCIES_HZ) - 1):
        ratios = all_off_matrix[:, index + 1] / all_off_matrix[:, index]
        gain_db = 20.0 * np.log10(np.abs(ratios))
        delay_ns = -np.angle(ratios) / (2.0 * np.pi * FREQUENCY_STEP_HZ) * 1e9
        step_rows.append(
            {
                "from_hz": HIGH_BAND_FREQUENCIES_HZ[index],
                "to_hz": HIGH_BAND_FREQUENCIES_HZ[index + 1],
                "gain_change_db": {
                    "mean": float(np.mean(gain_db)),
                    "sample_standard_deviation": float(np.std(gain_db, ddof=1)),
                },
                "apparent_group_delay_ns_modulo_alias_period": {
                    "mean": float(np.mean(delay_ns)),
                    "sample_standard_deviation": float(np.std(delay_ns, ddof=1)),
                },
            }
        )

    raw_selected_by_frequency = snapshot.get("selected_by_frequency")
    if not isinstance(raw_selected_by_frequency, dict):
        raise FrequencyDomainAnalysisError("selected means by frequency must be an object")
    selected_by_frequency: dict[int, object] = {}
    for raw_frequency, values in raw_selected_by_frequency.items():
        frequency = _integer(raw_frequency, "selected means frequency")
        if frequency in selected_by_frequency:
            raise FrequencyDomainAnalysisError("selected means frequency is duplicated")
        selected_by_frequency[frequency] = values
    selector_rows = []
    for frequency in HIGH_BAND_FREQUENCIES_HZ:
        selected = [
            _number(value, "selected bound amplitude")
            for value in _sequence(selected_by_frequency[frequency], "selected bound amplitudes")
        ]
        if frequency < 4_000_000_000:
            specification_band = "2-4 GHz"
            minimum_isolation_db = PE42482_TWO_TO_FOUR_MINIMUM_ISOLATION_DB
            maximum_insertion_loss_db = PE42482_TWO_TO_FOUR_MAXIMUM_INSERTION_LOSS_DB
        else:
            specification_band = "4-6 GHz"
            minimum_isolation_db = PE42482_MINIMUM_ISOLATION_DB
            maximum_insertion_loss_db = PE42482_MAXIMUM_INSERTION_LOSS_DB
        bound = selector_coherent_bound(
            selected,
            minimum_isolation_db,
            maximum_insertion_loss_db,
        )
        observed = abs(all_off_mean[HIGH_BAND_FREQUENCIES_HZ.index(frequency)])
        residual_lower_bound = max(0.0, observed - bound)
        selector_rows.append(
            {
                "center_frequency_hz": frequency,
                "datasheet_specification_band": specification_band,
                "observed_all_off_amplitude": float(observed),
                "datasheet_conditioned_perfect_coherent_sum_bound": bound,
                "observed_over_bound_db": float(20.0 * math.log10(observed / bound)),
                "additional_common_path_amplitude_lower_bound": residual_lower_bound,
                "additional_common_path_lower_bound_fraction_of_observed_voltage": (
                    residual_lower_bound / observed
                ),
            }
        )

    permutation_rows = []
    raw_permutations = _sequence(snapshot.get("permutation_rows"), "loaded permutations")
    for raw_row in sorted(
        raw_permutations, key=lambda value: str(_mapping(value, "permutation")["created_at"])
    ):
        row = _mapping(raw_row, "loaded permutation")
        all_off = complex(row["all_off"])
        selected_sum = complex(row["selected_sum"])
        selected_amplitudes = [
            _number(value, "permutation amplitude")
            for value in _sequence(row.get("selected_amplitudes"), "permutation amplitudes")
        ]
        bound = selector_coherent_bound(selected_amplitudes)
        permutation_rows.append(
            {
                "label": row["label"],
                "artifact_id": row["artifact_id"],
                "created_at": row["created_at"],
                "all_off": _complex_document(all_off),
                "all_off_amplitude": abs(all_off),
                "all_off_phase_deg": float(np.angle(all_off, deg=True)),
                "selected_coherent_sum": _complex_document(selected_sum),
                "selected_coherent_sum_amplitude": abs(selected_sum),
                "selected_coherent_sum_phase_deg": float(np.angle(selected_sum, deg=True)),
                "datasheet_conditioned_perfect_coherent_sum_bound": bound,
                "observed_over_bound_db": 20.0 * math.log10(abs(all_off) / bound),
            }
        )

    exact_bound = selector_rows[-1]
    return {
        "schema": 1,
        "analysis_kind": "five_point_eight_ghz_frequency_domain_root_cause_analysis",
        "scope": {
            "frequency_min_hz": HIGH_BAND_MIN_HZ,
            "frequency_max_hz": HIGH_BAND_MAX_HZ,
            "frequency_step_hz": FREQUENCY_STEP_HZ,
            "frequency_count": len(HIGH_BAND_FREQUENCIES_HZ),
            "repeat_count": EXPECTED_REPEAT_COUNT,
            "delay_alias_period_ns": DELAY_ALIAS_PERIOD_S * 1e9,
            "delay_interpretation": (
                "all delays are modulo 10 ns because the retained grid spacing is 100 MHz"
            ),
        },
        "frequency_results": list(per_frequency.values()),
        "paired_five_point_seven_to_five_point_eight": {
            "all_off_amplitude_change_db": paired_summary(all_off_delta.tolist()),
            "selected_subtracted_median_amplitude_change_db": paired_summary(
                selected_delta.tolist()
            ),
            "raw_median_contrast_change_db": paired_summary(contrast_delta.tolist()),
        },
        "single_delay_rejection": single_delay,
        "hankel_model_order_diagnostics": hankel,
        "adjacent_frequency_diagnostics": step_rows,
        "selector_bound": {
            "status": "datasheet_conditioned_planning_bound_not_board_measurement",
            "datasheet": PE42482_DATASHEET_ID,
            "datasheet_page": PE42482_DATASHEET_PAGE,
            "conditions": (
                "table-3 band-specific minimum isolation and maximum insertion loss; "
                "50-ohm datasheet conditions; eight leakage voltages assumed perfectly "
                "phase aligned; for the sweep boundary, the 4-6 GHz row applies from "
                "exactly 4.0 GHz through 5.8 GHz"
            ),
            "specifications": {
                "2-4 GHz": {
                    "minimum_isolation_db_by_path": list(PE42482_TWO_TO_FOUR_MINIMUM_ISOLATION_DB),
                    "maximum_insertion_loss_db_by_path": list(
                        PE42482_TWO_TO_FOUR_MAXIMUM_INSERTION_LOSS_DB
                    ),
                },
                "4-6 GHz": {
                    "minimum_isolation_db_by_path": list(PE42482_MINIMUM_ISOLATION_DB),
                    "maximum_insertion_loss_db_by_path": list(PE42482_MAXIMUM_INSERTION_LOSS_DB),
                },
            },
            "frequency_results": selector_rows,
            "five_point_eight": exact_bound,
            "limitation": (
                "a fabricated selector with degraded RF grounding, mismatch, launch coupling, "
                "or performance near the weaker project-level 25 dB target can exceed this bound"
            ),
        },
        "permutation_invariance": {
            "rows": permutation_rows,
            "finding": (
                "the selected coherent-sum phase changes much more than ALL_OFF phase; this "
                "rejects a uniform frequency-independent scaled-sum model but does not identify "
                "arbitrary per-port leakage coefficients"
            ),
        },
        "conclusions": {
            "single_constant_gain_delay_rejected": True,
            "rank_one_frequency_response_rejected": True,
            "coherent_deterministic_frequency_selective_baseline": True,
            "physical_root_cause_uniquely_identified": False,
            "remaining_physical_candidates": [
                "Pluto internal TX1-to-RX2 coupling in the extended 5.8 GHz profile",
                "RX2 cable or common-launch coupling",
                "degraded selector or PCB common-output isolation",
                "coherent combination of a common path and finite selector leakage",
            ],
            "required_disambiguation": (
                "termination/cable/selector topology ladder and a board VNA isolation matrix"
            ),
        },
    }


def _render_figures(result: Mapping[str, Any], figure_directory: Path) -> list[dict[str, str]]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise FrequencyDomainAnalysisError(
            "figure rendering requires the report dependency group"
        ) from error

    figure_directory.mkdir(parents=True, exist_ok=True)
    rows = [
        _mapping(item, "frequency result")
        for item in _sequence(result["frequency_results"], "frequency results")
    ]
    frequencies = np.asarray(
        [_number(row["center_frequency_hz"], "frequency") / 1e9 for row in rows]
    )
    off_db = np.asarray([_number(row["all_off_mean_amplitude_db"], "ALL_OFF dB") for row in rows])
    selected_db = np.asarray(
        [
            _number(row["selected_subtracted_median_amplitude_mean_db"], "selected dB")
            for row in rows
        ]
    )
    contrast_db = np.asarray(
        [
            _number(row["raw_selected_to_all_off_median_contrast_mean_db"], "contrast")
            for row in rows
        ]
    )
    off = np.asarray([_complex(row["all_off_mean"], "mean ALL_OFF") for row in rows])
    created: list[dict[str, str]] = []

    def save(fig: Any, name: str) -> None:
        path = figure_directory / name
        fig.savefig(path, dpi=180, bbox_inches="tight", metadata={"Software": "smateway"})
        plt.close(fig)
        created.append({"path": str(path.relative_to(REPOSITORY_ROOT)), "sha256": _sha256(path)})

    fig, axis = plt.subplots(figsize=(8.4, 4.8))
    axis.plot(frequencies, off_db, "o-", label="ALL_OFF RX2/RX1")
    axis.plot(frequencies, selected_db, "s-", label="Median selected, ALL_OFF-subtracted")
    axis.set(xlabel="Center frequency (GHz)", ylabel="Transfer amplitude (dB)")
    axis.grid(True, alpha=0.25)
    axis.legend(loc="best")
    twin = axis.twinx()
    twin.plot(frequencies, contrast_db, color="#2ca02c", alpha=0.65, label="Raw contrast")
    twin.set_ylabel("Median raw contrast (dB)", color="#2ca02c")
    axis.set_title("Deterministic ALL_OFF rise dominates the 5.8 GHz contrast collapse")
    save(fig, "fig01_all_off_selected_contrast.png")

    fit = _mapping(result["single_delay_rejection"], "single-delay result")
    prediction = np.asarray(
        [
            _complex(item, "single-delay prediction")
            for item in _sequence(fit["prediction"], "prediction")
        ]
    )
    fig, axis = plt.subplots(figsize=(7.2, 5.6))
    axis.plot(off.real, off.imag, "o-", label="Measured mean")
    axis.plot(prediction.real, prediction.imag, "x--", label="Best one-delay model")
    for frequency, value in zip(frequencies, off, strict=True):
        axis.annotate(f"{frequency:.1f}", (value.real, value.imag), fontsize=7)
    axis.axhline(0.0, color="black", linewidth=0.6)
    axis.axvline(0.0, color="black", linewidth=0.6)
    axis.set(xlabel="Real RX2/RX1", ylabel="Imaginary RX2/RX1", aspect="equal")
    axis.grid(True, alpha=0.25)
    axis.legend(loc="best")
    axis.set_title(f"Single-delay model rejected: NRMSE {fit['complex_nrmse']:.3f}")
    save(fig, "fig02_complex_locus_single_delay_fit.png")

    steps = [
        _mapping(item, "adjacent-frequency row")
        for item in _sequence(result["adjacent_frequency_diagnostics"], "step rows")
    ]
    step_frequency = np.asarray([_number(row["to_hz"], "step frequency") / 1e9 for row in steps])
    gain_mean = np.asarray(
        [_number(_mapping(row["gain_change_db"], "gain")["mean"], "gain mean") for row in steps]
    )
    gain_std = np.asarray(
        [
            _number(
                _mapping(row["gain_change_db"], "gain")["sample_standard_deviation"], "gain std"
            )
            for row in steps
        ]
    )
    delay_mean = np.asarray(
        [
            _number(
                _mapping(row["apparent_group_delay_ns_modulo_alias_period"], "delay")["mean"],
                "delay mean",
            )
            for row in steps
        ]
    )
    delay_std = np.asarray(
        [
            _number(
                _mapping(row["apparent_group_delay_ns_modulo_alias_period"], "delay")[
                    "sample_standard_deviation"
                ],
                "delay std",
            )
            for row in steps
        ]
    )
    fig, axes = plt.subplots(2, 1, figsize=(8.4, 6.4), sharex=True)
    axes[0].errorbar(step_frequency, gain_mean, yerr=gain_std, fmt="o-", capsize=2)
    axes[0].set_ylabel("Step gain (dB)")
    axes[1].errorbar(step_frequency, delay_mean, yerr=delay_std, fmt="o-", capsize=2)
    axes[1].set(
        xlabel="Upper center of 100 MHz step (GHz)", ylabel="Apparent delay (ns, modulo 10 ns)"
    )
    for axis in axes:
        axis.grid(True, alpha=0.25)
    axes[0].set_title("Gain and apparent delay both change sharply near 5.8 GHz")
    save(fig, "fig03_step_gain_and_group_delay.png")

    hankel = _mapping(result["hankel_model_order_diagnostics"], "Hankel result")
    ratios = np.asarray(
        [
            _number(value, "singular ratio")
            for value in _sequence(hankel["singular_values_relative_to_first"], "singular ratios")
        ]
    )
    delay_grid = np.linspace(0.0, DELAY_ALIAS_PERIOD_S, 5000, endpoint=False)
    offsets = (np.asarray(HIGH_BAND_FREQUENCIES_HZ) - HIGH_BAND_MIN_HZ).astype(float)
    matched = np.abs(np.exp(2j * np.pi * delay_grid[:, None] * offsets[None, :]) @ off) / len(off)
    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.4))
    axes[0].bar(np.arange(1, len(ratios) + 1), ratios)
    axes[0].set(xlabel="Singular-value index", ylabel="Relative to first", title="Hankel spectrum")
    axes[1].plot(delay_grid * 1e9, matched)
    axes[1].set(
        xlabel="Delay (ns, modulo 10 ns)", ylabel="Matched amplitude", title="Delay-domain locus"
    )
    for axis in axes:
        axis.grid(True, alpha=0.25)
    fig.suptitle("The frequency response is not rank one")
    save(fig, "fig04_hankel_rank_delay_spectrum.png")

    bound_result = _mapping(result["selector_bound"], "selector bound")
    bounds = [
        _mapping(item, "selector bound row")
        for item in _sequence(bound_result["frequency_results"], "selector rows")
    ]
    bound_db = np.asarray(
        [
            20.0
            * math.log10(_number(row["datasheet_conditioned_perfect_coherent_sum_bound"], "bound"))
            for row in bounds
        ]
    )
    fig, axis = plt.subplots(figsize=(8.4, 4.8))
    axis.plot(frequencies, off_db, "o-", label="Observed ALL_OFF")
    axis.plot(frequencies, bound_db, "s--", label="PE42482 datasheet-conditioned upper bound")
    axis.fill_between(
        frequencies,
        bound_db,
        off_db,
        where=off_db > bound_db,
        color="#d62728",
        alpha=0.22,
        label="Observed exceeds bound",
    )
    axis.set(xlabel="Center frequency (GHz)", ylabel="Voltage transfer (dB)")
    axis.grid(True, alpha=0.25)
    axis.legend(loc="best")
    axis.set_title("5.8 GHz exceeds the datasheet-conditioned selector-only bound")
    save(fig, "fig05_selector_datasheet_conditioned_bound.png")

    permutation = _mapping(result["permutation_invariance"], "permutation result")
    permutation_rows = [
        _mapping(item, "permutation row")
        for item in _sequence(permutation["rows"], "permutation rows")
    ]
    permutation_off = np.asarray(
        [_complex(row["all_off"], "permutation ALL_OFF") for row in permutation_rows]
    )
    permutation_sum = np.asarray(
        [
            _complex(row["selected_coherent_sum"], "permutation selected sum")
            for row in permutation_rows
        ]
    )
    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.6))
    axes[0].plot(permutation_off.real, permutation_off.imag, "o-")
    axes[1].plot(permutation_sum.real, permutation_sum.imag, "s-")
    for axis, values, title in zip(
        axes,
        (permutation_off, permutation_sum),
        ("ALL_OFF", "Sum of selected contributions"),
        strict=True,
    ):
        for row, value in zip(permutation_rows, values, strict=True):
            axis.annotate(
                str(row["label"]).replace("_", " "),
                (value.real, value.imag),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
        axis.axhline(0.0, color="black", linewidth=0.6)
        axis.axvline(0.0, color="black", linewidth=0.6)
        axis.set(xlabel="Real RX2/RX1", ylabel="Imaginary RX2/RX1", title=title)
        axis.margins(0.18)
        axis.grid(True, alpha=0.25)
    fig.suptitle("Permutation changes the selected sum far more than the ALL_OFF locus")
    save(fig, "fig06_permutation_invariance.png")
    return created


def main() -> int:
    args = _parser().parse_args()
    repeatability = _read_json(args.repeatability, "repeatability analysis")
    permutation = _read_json(args.permutation, "permutation analysis")
    if args.refresh_observation_snapshot_from_capture_root is not None:
        snapshot = _build_snapshot(
            args.repeatability,
            repeatability,
            args.permutation,
            permutation,
            args.refresh_observation_snapshot_from_capture_root,
        )
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    loaded = _load_snapshot(
        args.snapshot,
        args.repeatability,
        repeatability,
        args.permutation,
        permutation,
    )
    result = analyze(loaded)
    result["sources"] = {
        "analysis_script": {
            "path": _reported_path(Path(__file__)),
            "sha256": _sha256(Path(__file__)),
        },
        "repeatability": {
            "path": _reported_path(args.repeatability),
            "sha256": _sha256(args.repeatability),
        },
        "permutation": {
            "path": _reported_path(args.permutation),
            "sha256": _sha256(args.permutation),
        },
        "compact_observation_snapshot": {
            "path": _reported_path(args.snapshot),
            "sha256": loaded["snapshot_sha256"],
        },
    }
    figures = _render_figures(result, args.figure_directory)
    result["figures"] = figures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"analysis": str(args.output), "figures": len(figures)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
