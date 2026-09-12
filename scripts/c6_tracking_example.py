#!/usr/bin/env python3
"""Offline C6 tutorial: synthetic IQ and a bounded replay of one retained TX1 record.

No radio, network, flashing or capture APIs. All bearings are diagnostic, never a
production qualification. The real replay retains the known-emitter timing caveat.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from smateway.fast_tracking import FastTrackingProfile
from smateway.rate_timing import PORTS, complex_json, complex_value, load, sha256
from smateway.reference_timing import rolling_reference_windows
from smateway.tracking.bearing import solve_bearing
from smateway.tracking.calibration import BoardCalibrationLut
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]
LUT = ROOT / "docs/pcb_direct_injection_calibration/data/calibration-lut.json"
PROFILE = ROOT / "profiles/tracking-c6-200us-v1/control_profile.json"


def transfer(rx1, rx2, weights=None):
    """Weighted least-squares RX2/RX1 coefficient, without a per-visit FFT.

    Requires an isolated common narrowband signal and already settled/labeled
    samples. Positive reference energy is mandatory; epsilon cannot create it.
    """
    x = np.asarray(rx1, dtype=np.complex128)
    y = np.asarray(rx2, dtype=np.complex128)
    w = np.ones(x.shape) if weights is None else np.asarray(weights, dtype=float)
    if (
        x.ndim != 1
        or x.size == 0
        or x.shape != y.shape
        or x.shape != w.shape
        or not np.isfinite(x).all()
        or not np.isfinite(y).all()
        or not np.isfinite(w).all()
        or np.any(w < 0)
    ):
        raise ValueError("Finite paired IQ and nonnegative aligned weights required")
    energy = float(np.sum(w * np.abs(x) ** 2))
    if energy <= np.finfo(float).tiny:
        raise ValueError("No reference signal energy")
    return complex(np.sum(w * y * x.conj()) / energy)


def solve_vector(h, frequency_hz, geom, lut, weights=None):
    """Apply the PCB correction exactly once; preserve the legacy gate result."""
    grid = np.arange(0, 360, 0.25)
    correction = lut.evaluate(frequency_hz, PORTS)
    calibrated = np.asarray(h) * correction.coefficients
    steering = far_field_steering(geom, frequency_hz, grid)
    fit = solve_bearing(calibrated, steering, grid, weights=weights)
    return {
        "raw_transfer": [complex_json(v) for v in h],
        "calibrated_transfer": [complex_json(v) for v in calibrated],
        "bearing_deg": fit.bearing_deg,
        "legacy_gate_valid": fit.valid,
        "legacy_gate_reasons": list(fit.reasons),
        "spatial_phase_residual_deg": fit.residual_phase_rms_deg,
        "score": fit.score,
        "ambiguity_margin_db": fit.ambiguity_margin_db,
        "board_exact_knot": correction.exact_knot,
        "board_interpolation_supported": correction.interpolation_validated,
        "board_status": correction.calibration_status,
        "production_valid": False,
        "bearing_grid_deg": grid.tolist(),
        "likelihood": fit.likelihood.tolist(),
    }


def simulate(frequency_hz, *, fs=5_000_000, cycles=80, seed=12, bearing_deg=65.0, noise_rms=0.08):
    """Generate an ideal fixed source with measured PCB response and exact timing.

    The measured LUT is deliberately both the forward model and its inverse:
    this is a software identity demonstration, NOT independent calibration proof.
    Raw IQ stays in memory. A decimated first-cycle excerpt is retained for figures.
    """
    if fs not in (2_000_000, 5_000_000) or cycles < 1 or noise_rms < 0:
        raise ValueError("Use 2 or 5 MS/s, positive cycles and nonnegative noise")
    if not np.isfinite([frequency_hz, bearing_deg, noise_rms]).all():
        raise ValueError("Finite simulation settings required")
    p = FastTrackingProfile.load(PROFILE)
    lut = BoardCalibrationLut.load(LUT)
    geom = ArrayGeometry.circular("nominal-51mm-C6", PORTS, radius_mm=25.5)
    expected = far_field_steering(geom, frequency_hz, [bearing_deg])[0]
    calibration = lut.evaluate(frequency_hz, PORTS)
    physical = expected / calibration.coefficients
    cycle_samples = round(p.cycle_us * fs / 1e6)
    count = cycles * cycle_samples
    sample = np.arange(count)
    # A common drifting transmitter phase cancels in RX2 * conjugate(RX1).
    signal = np.exp(
        1j * (2 * np.pi * 100_000 * sample / fs + 0.5 * np.sin(2 * np.pi * 11 * sample / fs))
    )
    label = np.full(count, -1, dtype=int)
    settled = np.zeros(count, dtype=bool)
    offsets = sample % cycle_samples
    for port in range(6):
        start = round((p.observable_marker_us + port * (p.dwell_us + p.guard_us)) * fs / 1e6)
        end = start + round(p.dwell_us * fs / 1e6)
        label[(offsets >= start) & (offsets < end)] = port
        settled |= (offsets >= start + round(5 * fs / 1e6)) & (offsets < end - round(5 * fs / 1e6))
    rng = np.random.default_rng(seed)

    def noise():
        return noise_rms / np.sqrt(2) * (rng.normal(size=count) + 1j * rng.normal(size=count))

    x1 = signal + noise()
    x2 = noise()
    for i in range(6):
        x2[label == i] += signal[label == i] * physical[i]
    h = np.array(
        [transfer(x1[(label == i) & settled], x2[(label == i) & settled]) for i in range(6)]
    )
    result = solve_vector(h, frequency_hz, geom, lut)
    raw_fit = solve_bearing(
        h, far_field_steering(geom, frequency_hz, np.arange(0, 360, 0.25)), np.arange(0, 360, 0.25)
    )
    stride = max(1, fs // 1_000_000)
    example = slice(0, cycle_samples, stride)
    result.update(
        {
            "schema": 1,
            "evidence_kind": "synthetic_IQ_tutorial_not_hardware_validation",
            "frequency_hz": frequency_hz,
            "rx_lo_hz": frequency_hz - 100_000,
            "tone_offset_hz": 100_000,
            "sample_rate_hz": fs,
            "cycles": cycles,
            "duration_s": count / fs,
            "ports": list(PORTS),
            "positions_m": geom.positions_m.tolist(),
            "true_bearing_deg": bearing_deg,
            "seed": seed,
            "noise_rms_per_channel": noise_rms,
            "raw_model_bearing_deg": raw_fit.bearing_deg,
            "raw_model_likelihood": raw_fit.likelihood.tolist(),
            "expected_corrected_transfer": [complex_json(v) for v in expected],
            "time_us": (sample[example] / fs * 1e6).tolist(),
            "rx1_real_excerpt": x1[example].real.tolist(),
            "rx2_real_excerpt": x2[example].real.tolist(),
            "cross_real_excerpt": (x2[example] * x1[example].conj()).real.tolist(),
            "cross_imag_excerpt": (x2[example] * x1[example].conj()).imag.tolist(),
            "timing_source": "exact synthetic sample labels; no timing recovery tested",
            "calibration_caveat": "same LUT used for synthetic forward response and correction",
            "sources": {str(p.relative_to(ROOT)): sha256(p) for p in (LUT, PROFILE)},
        }
    )
    return result


def validate_timeline(run):
    """Reject missing, clipped, mixed-stream or discontinuous retained samples."""
    if run["status"] != "passed":
        raise ValueError("Acquisition did not pass")
    cfg, capture = run["configuration"], run["capture"]
    timeline = capture["timeline"]
    if (
        len(timeline) != cfg["frames"]
        or not timeline
        or capture["samples_per_channel"] != cfg["frames"] * cfg["frame_samples"]
        or len(capture["clipped_samples"]) != 2
        or any(capture["clipped_samples"])
    ):
        raise ValueError("Incomplete or clipped capture")
    previous = None
    stream = timeline[0]["stream_id"]
    for index, frame in enumerate(timeline):
        if (
            frame["buffer_sequence"] != index
            or frame["stream_id"] != stream
            or frame["missing_samples_before"] != 0
            or frame["overflow_observed"]
            or any(frame["clipped_samples"])
            or frame["last_sample_sequence_exclusive"] - frame["first_sample_sequence"]
            != cfg["frame_samples"]
            or (previous is not None and frame["first_sample_sequence"] != previous)
        ):
            raise ValueError("Discontinuous, mixed-stream or clipped frame")
        previous = frame["last_sample_sequence_exclusive"]


def replay(block_path, *, configuration="B", dwell_us=200, round_number=1, rf_offset_hz=0):
    """One archived TX1 record, 1 s past-only template training / 50 ms output.

    Default RF offset zero reproduces the historical nominal-center analysis.
    Set actual signal offset for a separately labeled RF-frequency refinement.
    Does not overwrite the original campaign's analyses or infer surveyed truth.
    """
    # Import this read-only reference loader only for real replay; synthetic use
    # depends solely on the small tracking modules above.
    from analyze_comprehensive_bearings import block_references

    block = load(block_path)
    if block["status"] not in ("acquired", "diagnostic-complete"):
        raise ValueError("Complete block required")
    refs, frozen, drift = block_references(block)
    selected = []
    for item in block["captures"]:
        path = Path(item["run_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("Run hash changed")
        run = load(path)
        cfg = run["configuration"]
        if (
            cfg["name"] == configuration
            and cfg["dwell_us"] == dwell_us
            and item["round"] == round_number
            and item["control"] is False
        ):
            selected.append((path, run))
    if len(selected) != 1:
        raise ValueError("Expected exactly one non-control capture")
    path, run = selected[0]
    cfg = run["configuration"]
    if cfg["mode"] != "fast" or cfg["tx_channel"] != 0:
        raise ValueError("Only TX1 same-emitter fast records are supported")
    validate_timeline(run)
    raw = run["capture"]["raw"]
    if len(raw) != 2:
        raise ValueError("Two raw channel files required")
    channels = []
    for artifact in raw:
        raw_path = Path(artifact["path"])
        expected_bytes = run["capture"]["samples_per_channel"] * 8
        if (
            raw_path.stat().st_size != expected_bytes
            or artifact["bytes"] != expected_bytes
            or sha256(raw_path) != artifact["sha256"]
        ):
            raise ValueError("Raw IQ hash or size changed")
        channels.append(np.memmap(raw_path, dtype="<c8", mode="r"))
    fixture_binding = block["binding"]["fixture"]
    fixture_path = Path(fixture_binding["path"])
    if sha256(fixture_path) != fixture_binding["sha256"]:
        raise ValueError("Fixture hash changed")
    fixture = load(fixture_path)
    if fixture["ports"] != list(PORTS):
        raise ValueError("Fixture port order differs")
    geom = ArrayGeometry(
        fixture["fixture_id"], PORTS, np.asarray(fixture["positions_m"]), np.arange(6) * 60
    )
    profile_path = ROOT / f"profiles/tracking-c6-{dwell_us}us-v1/control_profile.json"
    profile = FastTrackingProfile.load(profile_path)
    flash_path = Path(run["flash"]["path"])
    if sha256(flash_path) != run["flash"]["sha256"]:
        raise ValueError("Flash evidence hash changed")
    flash = load(flash_path)
    if (
        flash["status"] != "passed"
        or flash["build"]["dwell_us"] != dwell_us
        or flash["build"]["profile"]["sha256"] != sha256(profile_path)
    ):
        raise ValueError("Selected firmware/profile binding differs")
    expected = np.array(
        [complex_value(refs[configuration]["before"][p]["reference"]["transfer"]) for p in PORTS]
    )
    fs = cfg["sample_rate_hz"]
    rows = rolling_reference_windows(*channels, profile, expected, fs=fs)
    lut = BoardCalibrationLut.load(LUT)
    for row in rows:
        if row["status"] == "analyzed":
            h = np.array([complex_value(v) for v in row["mean_transfer"]])
            row["direction"] = solve_vector(
                h, cfg["frequency_hz"] + rf_offset_hz, geom, lut, frozen["weights"]
            )
            # Keep the long likelihood once, not 60 copies in the tutorial output.
            row["direction"].pop("bearing_grid_deg")
            row["direction"].pop("likelihood")
    return {
        "schema": 1,
        "evidence_kind": "stored_IQ_known_emitter_template_replay",
        "production_valid": False,
        "surveyed_truth_available": False,
        "scope": "One retained record; not a replacement for full block/control qualification",
        "configuration": cfg,
        "rf_analysis_frequency_hz": cfg["frequency_hz"] + rf_offset_hz,
        "rf_offset_hz": rf_offset_hz,
        "ports": list(PORTS),
        "reference_brackets": drift,
        "weights": frozen["weights"],
        "observable_mask": frozen["observable"],
        "windows": rows,
        "expected_windows": round((cfg["duration_s"] - 1) / 0.05),
        "source_block": {"path": str(block_path.resolve()), "sha256": sha256(block_path)},
        "source_run": {"path": str(path.resolve()), "sha256": sha256(path)},
        "raw_inputs": raw,
        "fixture": fixture_binding,
        "sources": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                LUT,
                profile_path,
                Path(__file__).resolve(),
                ROOT / "src/smateway/reference_timing.py",
                ROOT / "src/smateway/causal_timing.py",
                ROOT / "src/smateway/rate_timing.py",
                ROOT / "src/smateway/fast_tracking.py",
                ROOT / "src/smateway/tracking/bearing.py",
                ROOT / "src/smateway/tracking/calibration.py",
                ROOT / "src/smateway/tracking/manifold.py",
                ROOT / "scripts/analyze_comprehensive_bearings.py",
            )
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Synthetic IQ, no hardware or raw-data files")
    demo.add_argument("--frequency-hz", type=float, required=True)
    demo.add_argument(
        "--sample-rate-hz", type=int, choices=(2_000_000, 5_000_000), default=5_000_000
    )
    demo.add_argument("--bearing-deg", type=float, default=65)
    demo.add_argument("--seed", type=int, default=12)
    demo.add_argument("--output", type=Path, required=True)
    real = commands.add_parser("replay", help="Replay one existing TX1 run, no capture")
    real.add_argument("--block", type=Path, required=True)
    real.add_argument("--configuration", choices=("A", "B", "D"), default="B")
    real.add_argument("--dwell-us", type=int, choices=(25, 50, 100, 200, 1000), default=200)
    real.add_argument("--round", type=int, choices=(1, 2, 3), default=1)
    real.add_argument("--rf-offset-hz", type=float, default=0)
    real.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; choose a new path to preserve previous evidence")
    if args.command == "demo":
        result = simulate(
            args.frequency_hz, fs=args.sample_rate_hz, seed=args.seed, bearing_deg=args.bearing_deg
        )
    else:
        result = replay(
            args.block,
            configuration=args.configuration,
            dwell_us=args.dwell_us,
            round_number=args.round,
            rf_offset_hz=args.rf_offset_hz,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "evidence_kind": result["evidence_kind"],
                "production_valid": False,
            }
        )
    )


if __name__ == "__main__":
    main()
