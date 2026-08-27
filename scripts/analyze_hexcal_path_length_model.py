#!/usr/bin/env python3
"""Test whether one fixed relative delay explains each HexRay antenna phase response."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

REPOSITORY = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = REPOSITORY / "docs/hexray_tx_in_middle_calibration/data"
DEFAULT_V22_RESULT = DATA_DIRECTORY / "hexcal-v2.2-results.json"
DEFAULT_5G8_PHASE = DATA_DIRECTORY / "hexcal-v2.4-5g8-phase-leakage-results.json"
DEFAULT_DESIGN = DATA_DIRECTORY / "design-snapshot.json"
DEFAULT_OUTPUT = DATA_DIRECTORY / "hexcal-path-length-inverse-analysis.json"
DEFAULT_FIGURE = (
    REPOSITORY
    / "docs/hexray_tx_in_middle_calibration/png/fig12_path_length_inverse_analysis.png"
)
DEFAULT_MANIFEST = DATA_DIRECTORY / "hexcal-path-length-inverse-analysis-manifest.json"
ANTENNAS = tuple(f"ANT{index}" for index in range(1, 7))
EXPECTED_2G4_FREQUENCIES_HZ = (
    2_400_000_000,
    2_423_000_000,
    2_440_000_000,
    2_472_000_000,
    2_483_000_000,
)
EXPERIMENTAL_5G8_HZ = 5_800_000_000
SPEED_OF_LIGHT_M_PER_S = 299_792_458.0
DIAGNOSTIC_PHASE_RMS_TOLERANCE_DEG = 5.0


class PathLengthAnalysisError(RuntimeError):
    """An input, invariant, or committed output is inconsistent."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PathLengthAnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise PathLengthAnalysisError(f"{label} must be an array")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PathLengthAnalysisError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PathLengthAnalysisError(f"{label} must be finite")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY).as_posix()
    except ValueError:
        return path.name


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PathLengthAnalysisError(f"cannot load {label} {path}: {error}") from error


def wrap_phase_deg(value: float) -> float:
    """Wrap a phase to [-180, 180)."""

    return (float(value) + 180.0) % 360.0 - 180.0


def circular_summary_deg(values: Sequence[float]) -> dict[str, float]:
    """Return circular mean and standard deviation in degrees."""

    phases = np.deg2rad(np.asarray(values, dtype=float))
    if phases.size == 0 or not np.all(np.isfinite(phases)):
        raise PathLengthAnalysisError("circular summary requires finite phase values")
    mean = complex(np.mean(np.exp(1j * phases)))
    coherence = abs(mean)
    if coherence <= 0.0:
        raise PathLengthAnalysisError("circular mean is undefined")
    return {
        "mean_deg": wrap_phase_deg(math.degrees(math.atan2(mean.imag, mean.real))),
        "circular_std_deg": math.degrees(math.sqrt(max(0.0, -2.0 * math.log(coherence)))),
        "coherence": coherence,
    }


def infer_delay_near_prior_ps(phase_deg: float, frequency_hz: float, prior_ps: float) -> float:
    """Resolve integer phase cycles to the delay nearest a released PCB prior."""

    candidates = (
        (-phase_deg + 360.0 * cycle) / (360.0 * frequency_hz) * 1e12
        for cycle in range(-10, 11)
    )
    return min(candidates, key=lambda value: abs(value - prior_ps))


def fit_fixed_delay(
    phases_deg: Sequence[float], frequencies_hz: Sequence[float], prior_ps: float
) -> dict[str, Any]:
    """Fit phase(f) = wrap(-360 f tau) after prior-based cycle resolution."""

    phases = np.asarray(phases_deg, dtype=float)
    frequencies = np.asarray(frequencies_hz, dtype=float)
    if phases.shape != frequencies.shape or phases.ndim != 1 or phases.size < 2:
        raise PathLengthAnalysisError("fixed-delay fit inputs must have equal nontrivial shape")
    inferred_ps = np.asarray(
        [
            infer_delay_near_prior_ps(float(phase), float(frequency), prior_ps)
            for phase, frequency in zip(phases, frequencies, strict=True)
        ]
    )
    resolved_cycles = inferred_ps * 1e-12 * frequencies
    fitted_delay_s = float(np.dot(frequencies, resolved_cycles) / np.dot(frequencies, frequencies))
    predicted = np.asarray(
        [wrap_phase_deg(-360.0 * frequency * fitted_delay_s) for frequency in frequencies]
    )
    residual = np.asarray(
        [
            wrap_phase_deg(observed - expected)
            for observed, expected in zip(phases, predicted, strict=True)
        ]
    )
    fitted_delay_ps = fitted_delay_s * 1e12
    return {
        "inferred_delay_ps": inferred_ps.tolist(),
        "fitted_delay_ps": fitted_delay_ps,
        "free_space_equivalent_length_mm": fitted_delay_s * SPEED_OF_LIGHT_M_PER_S * 1e3,
        "predicted_phase_deg_relative_to_ant1": predicted.tolist(),
        "phase_residual_deg": residual.tolist(),
        "phase_residual_rms_deg": float(math.sqrt(float(np.mean(residual**2)))),
        "inferred_delay_range_ps": float(np.ptp(inferred_ps)),
    }


def _observed_2g4_phase(v22: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    rows = tuple(
        _mapping(item, "frequency result")
        for item in _sequence(v22.get("frequency_results"), "frequency results")
    )
    frequencies = np.asarray([row.get("center_frequency_hz") for row in rows], dtype=float)
    if tuple(int(value) for value in frequencies) != EXPECTED_2G4_FREQUENCIES_HZ:
        raise PathLengthAnalysisError("v2.2 frequency matrix differs from the accepted result")
    observed = np.zeros((len(ANTENNAS), len(rows)), dtype=float)
    for frequency_index, row in enumerate(rows):
        correction = np.asarray(
            _sequence(row.get("correction_phase_deg"), "correction phase"), dtype=float
        )
        if correction.shape != (len(ANTENNAS),):
            raise PathLengthAnalysisError("correction phase must contain six antennas")
        observed[:, frequency_index] = [
            wrap_phase_deg(float(correction[0] - value)) for value in correction
        ]
    return frequencies, observed


def _observed_5g8_phase(phase: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = tuple(
        _mapping(item, "5.8 GHz gain row")
        for item in _sequence(phase.get("per_gain_reproducibility"), "5.8 GHz gain rows")
    )
    if len(rows) != 6 or phase.get("trial_count") != 12:
        raise PathLengthAnalysisError("5.8 GHz report must retain six gains and 12 trials")
    means = []
    deviations = []
    counts = []
    for antenna_index, name in enumerate(ANTENNAS):
        values = []
        for row in rows:
            states = tuple(
                _mapping(item, "5.8 GHz state")
                for item in _sequence(row.get("phase_by_state_relative_to_ant1"), "states")
            )
            if tuple(state.get("name") for state in states) != ANTENNAS:
                raise PathLengthAnalysisError("5.8 GHz state order differs from ANT1 through ANT6")
            values.extend(_sequence(states[antenna_index].get("values_deg"), f"{name} values"))
        summary = circular_summary_deg([_number(value, f"{name} phase") for value in values])
        means.append(summary["mean_deg"])
        deviations.append(summary["circular_std_deg"])
        counts.append(len(values))
    return np.asarray(means), np.asarray(deviations), np.asarray(counts, dtype=int)


def build_report(v22_path: Path, phase_path: Path, design_path: Path) -> Mapping[str, Any]:
    """Build the deterministic inverse-delay result from committed evidence snapshots."""

    v22 = _read_json(v22_path, "v2.2 result")
    phase = _read_json(phase_path, "5.8 GHz phase report")
    design = _read_json(design_path, "design snapshot")
    if v22.get("status") != "passed":
        raise PathLengthAnalysisError("v2.2 input must remain an accepted calibration")
    phase_conclusions = _mapping(phase.get("conclusions"), "5.8 GHz conclusions")
    if phase_conclusions.get("may_be_used_as_array_calibration") is not False:
        raise PathLengthAnalysisError("5.8 GHz input must remain rejected as calibration")

    priors = _mapping(design.get("pcb_route_priors"), "PCB route priors")
    prior_rows = tuple(
        _mapping(item, "PCB route row")
        for item in _sequence(priors.get("rows"), "PCB route rows")
    )
    if tuple(row.get("name") for row in prior_rows) != ANTENNAS:
        raise PathLengthAnalysisError("PCB route prior order differs from ANT1 through ANT6")
    prior_delay_ps = np.asarray(
        [_number(row.get("relative_delay_ps"), "PCB delay") for row in prior_rows]
    )
    frequencies, observed_2g4 = _observed_2g4_phase(v22)
    observed_5g8, std_5g8, count_5g8 = _observed_5g8_phase(phase)

    antenna_rows = []
    for index, name in enumerate(ANTENNAS):
        fit = fit_fixed_delay(observed_2g4[index], frequencies, float(prior_delay_ps[index]))
        inferred_5g8 = infer_delay_near_prior_ps(
            float(observed_5g8[index]), EXPERIMENTAL_5G8_HZ, float(prior_delay_ps[index])
        )
        predicted_5g8 = wrap_phase_deg(
            -360.0 * EXPERIMENTAL_5G8_HZ * float(fit["fitted_delay_ps"]) * 1e-12
        )
        row = {
            "name": name,
            "pcb_prior_delay_ps": float(prior_delay_ps[index]),
            "observed_phase_deg_relative_to_ant1_2g4": observed_2g4[index].tolist(),
            **fit,
            "diagnostic_5g8": {
                "admissible_calibration": False,
                "measurement_count": int(count_5g8[index]),
                "observed_phase_mean_deg_relative_to_ant1": float(observed_5g8[index]),
                "observed_phase_circular_std_deg": float(std_5g8[index]),
                "inferred_delay_ps": inferred_5g8,
                "predicted_phase_from_2g4_fit_deg": predicted_5g8,
                "phase_residual_from_2g4_fit_deg": wrap_phase_deg(
                    float(observed_5g8[index]) - predicted_5g8
                ),
            },
            "fixed_delay_model_within_diagnostic_tolerance": (
                float(fit["phase_residual_rms_deg"])
                <= DIAGNOSTIC_PHASE_RMS_TOLERANCE_DEG
            ),
        }
        antenna_rows.append(row)

    nonreference = antenna_rows[1:]
    residual_at_2440 = [float(row["phase_residual_deg"][2]) for row in nonreference]
    return {
        "schema": 1,
        "analysis_kind": "hexcal_single_fixed_relative_path_delay_test",
        "status": "single_fixed_path_delay_rejected_for_ant2_through_ant6",
        "analysis_script_sha256": _sha256(Path(__file__)),
        "inputs": {
            "v2_2_result": {"path": _relative(v22_path), "sha256": _sha256(v22_path)},
            "experimental_5g8_phase": {
                "path": _relative(phase_path),
                "sha256": _sha256(phase_path),
                "admissible_calibration": False,
            },
            "design_snapshot": {"path": _relative(design_path), "sha256": _sha256(design_path)},
        },
        "reference_antenna": "ANT1",
        "phase_convention": (
            "measured response phase of ANTn relative to ANT1; negative means ANTn lags ANT1; "
            "wrapped to [-180, 180)"
        ),
        "model": {
            "equation": "phase_i(f) = wrap(-360 * f * tau_i)",
            "inverse_equation": "tau_i = (-phase_i + 360*k) / (360*f)",
            "integer_cycle_policy": (
                "choose k independently at each frequency so inferred delay is nearest the "
                "released PCB-route delay prior"
            ),
            "diagnostic_phase_rms_tolerance_deg": DIAGNOSTIC_PHASE_RMS_TOLERANCE_DEG,
            "tolerance_provenance": (
                "diagnostic yardstick borrowed from the existing maximum 5 degree calibration "
                "phase-residual gate; not a predeclared model-selection gate"
            ),
            "path_length_interpretation": (
                "reported c*tau is a free-space-equivalent electrical length; physical length "
                "equals propagation velocity times delay and depends on medium"
            ),
        },
        "verified_2g4_frequencies_hz": [int(value) for value in frequencies],
        "diagnostic_5g8_frequency_hz": EXPERIMENTAL_5G8_HZ,
        "antennas": antenna_rows,
        "common_frequency_structure": {
            "frequency_hz": 2_440_000_000,
            "nonreference_phase_residual_deg": residual_at_2440,
            "median_phase_residual_deg": float(np.median(residual_at_2440)),
            "interpretation": (
                "all five nonreference channels share their largest negative residual at 2.440 "
                "GHz; a static delay cannot create this common curved frequency response"
            ),
        },
        "conclusions": [
            "No nonreference antenna meets the 5 degree diagnostic fixed-delay tolerance.",
            (
                "The best nonreference RMS residual exceeds 12 degrees despite sub-degree "
                "repeatability."
            ),
            (
                "The common 2.440 GHz excursion points to frequency-dependent end-to-end "
                "response, not only path length."
            ),
            (
                "The 5.8 GHz point is retained only as a rejected-artifact diagnostic and "
                "cannot promote or rescue the model."
            ),
        ],
    }


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.labelsize": 11,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "axes.facecolor": "#fbfcfe",
        }
    )


def render_figure(report: Mapping[str, Any], path: Path) -> None:
    """Render the inverse-delay evidence and residual diagnostics."""

    rows = tuple(_mapping(item, "antenna result") for item in report["antennas"])[1:]
    frequencies_ghz = np.asarray(report["verified_2g4_frequencies_hz"], dtype=float) / 1e9
    colors = ("#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7")
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), constrained_layout=True)

    for row, color in zip(rows, colors, strict=True):
        inferred = np.asarray(row["inferred_delay_ps"], dtype=float)
        fitted = _number(row["fitted_delay_ps"], "fitted delay")
        axes[0, 0].plot(frequencies_ghz, inferred, "o-", color=color, label=row["name"])
        axes[0, 0].axhline(fitted, color=color, linewidth=1, linestyle="--", alpha=0.7)
        residual = np.asarray(row["phase_residual_deg"], dtype=float)
        axes[1, 0].plot(frequencies_ghz, residual, "o-", color=color, label=row["name"])
    axes[0, 0].set_title("Inverted delay is not constant with frequency")
    axes[0, 0].set_ylabel("Equivalent relative delay (ps)")
    axes[0, 0].legend(ncol=3)
    axes[1, 0].axhline(5.0, color="#555555", linestyle="--", linewidth=1)
    axes[1, 0].axhline(-5.0, color="#555555", linestyle="--", linewidth=1)
    axes[1, 0].axvline(2.440, color="#8B1A1A", linewidth=1.5, alpha=0.7)
    axes[1, 0].set_title("All paths share a negative 2.440 GHz residual")
    axes[1, 0].set_xlabel("RF center frequency (GHz)")
    axes[1, 0].set_ylabel("Observed − fixed-delay prediction (degrees)")

    x = np.arange(len(rows), dtype=float)
    rms = np.asarray([row["phase_residual_rms_deg"] for row in rows], dtype=float)
    axes[0, 1].bar(x, rms, color=colors)
    axes[0, 1].axhline(5.0, color="#8B1A1A", linestyle="--", label="5° diagnostic tolerance")
    axes[0, 1].set_xticks(x, [row["name"] for row in rows])
    axes[0, 1].set_ylabel("2.4 GHz phase residual RMS (degrees)")
    axes[0, 1].set_title("Every nonreference antenna rejects the model")
    axes[0, 1].legend()

    fitted = np.asarray([row["fitted_delay_ps"] for row in rows], dtype=float)
    diagnostic = np.asarray(
        [row["diagnostic_5g8"]["inferred_delay_ps"] for row in rows], dtype=float
    )
    width = 0.36
    axes[1, 1].bar(x - width / 2, fitted, width, color="#0072B2", label="2.4 GHz fixed fit")
    axes[1, 1].bar(
        x + width / 2,
        diagnostic,
        width,
        color="#D55E00",
        label="5.8 GHz diagnostic inversion",
    )
    axes[1, 1].set_xticks(x, [row["name"] for row in rows])
    axes[1, 1].set_ylabel("Equivalent relative delay (ps)")
    axes[1, 1].set_title("Rejected 5.8 GHz data do not restore consistency")
    axes[1, 1].legend()

    fig.suptitle(
        "HexRay inverse path-length test — one static delay per antenna is insufficient",
        fontsize=17,
        fontweight="bold",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        path,
        dpi=160,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": "smateway deterministic inverse path-length analyzer"},
    )
    plt.close(fig)


def write_report(
    report: Mapping[str, Any], output_path: Path, figure_path: Path, manifest_path: Path
) -> Mapping[str, Any]:
    """Write deterministic JSON, PNG, and provenance manifest."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with plt.rc_context():
        plt.rcdefaults()
        _style()
        render_figure(report, figure_path)
    manifest = {
        "schema": 1,
        "analysis_script": _relative(Path(__file__)),
        "analysis_script_sha256": _sha256(Path(__file__)),
        "analysis": {"path": _relative(output_path), "sha256": _sha256(output_path)},
        "figure": {
            "path": _relative(figure_path),
            "sha256": _sha256(figure_path),
            "byte_size": figure_path.stat().st_size,
        },
        "matplotlib_version": matplotlib.__version__,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def generate(
    v22_path: Path,
    phase_path: Path,
    design_path: Path,
    output_path: Path,
    figure_path: Path,
    manifest_path: Path,
) -> Mapping[str, Any]:
    report = build_report(v22_path, phase_path, design_path)
    return write_report(report, output_path, figure_path, manifest_path)


def check(
    v22_path: Path,
    phase_path: Path,
    design_path: Path,
    output_path: Path,
    figure_path: Path,
    manifest_path: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="hexcal-path-length-") as temporary:
        root = Path(temporary)
        expected = generate(
            v22_path,
            phase_path,
            design_path,
            root / output_path.name,
            root / figure_path.name,
            root / manifest_path.name,
        )
        expected["analysis"]["path"] = _relative(output_path)
        expected["figure"]["path"] = _relative(figure_path)
        actual = _read_json(manifest_path, "committed inverse-delay manifest")
        if actual != expected:
            raise PathLengthAnalysisError("committed inverse-delay manifest is stale")
        if (root / output_path.name).read_bytes() != output_path.read_bytes():
            raise PathLengthAnalysisError("committed inverse-delay JSON is stale")
        if (root / figure_path.name).read_bytes() != figure_path.read_bytes():
            raise PathLengthAnalysisError("committed inverse-delay PNG is stale")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v22-result", type=Path, default=DEFAULT_V22_RESULT)
    parser.add_argument("--phase-5g8", type=Path, default=DEFAULT_5G8_PHASE)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--figure", type=Path, default=DEFAULT_FIGURE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.check:
            check(
                args.v22_result,
                args.phase_5g8,
                args.design,
                args.output,
                args.figure,
                args.manifest,
            )
            print(json.dumps({"status": "passed", "outputs_checked": 3}, sort_keys=True))
        else:
            manifest = generate(
                args.v22_result,
                args.phase_5g8,
                args.design,
                args.output,
                args.figure,
                args.manifest,
            )
            print(json.dumps({"status": "generated", "manifest": manifest}, sort_keys=True))
    except PathLengthAnalysisError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
