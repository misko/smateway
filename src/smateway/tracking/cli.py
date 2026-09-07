"""Replay one complex switched-array snapshot through the production estimator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .bearing import solve_bearing
from .calibration import BoardCalibrationLut
from .manifold import far_field_steering
from .schedule import load_ism_band_profiles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smateway-track")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--frequency-plan", type=Path, required=True)
    parser.add_argument("--board-lut", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--bearing-step-deg", type=float, default=0.25)
    parser.add_argument("--minimum-score", type=float, default=0.5)
    parser.add_argument("--minimum-ambiguity-db", type=float, default=1.0)
    parser.add_argument("--maximum-residual-phase-deg", type=float, default=45.0)
    parser.add_argument("--output", type=Path)
    return parser


def _complex_vector(document: dict[str, Any], ports: tuple[str, ...]) -> np.ndarray:
    real = np.asarray(document.get("transfer_real"), dtype=np.float64)
    imaginary = np.asarray(document.get("transfer_imag"), dtype=np.float64)
    if real.shape != (len(ports),) or imaginary.shape != real.shape:
        raise ValueError("snapshot complex transfer vectors disagree with the profile ports")
    if not np.all(np.isfinite(real)) or not np.all(np.isfinite(imaginary)):
        raise ValueError("snapshot transfer must be finite")
    return real + 1j * imaginary


def solve_snapshot(
    snapshot_path: Path,
    *,
    frequency_plan_path: Path,
    board_lut_path: Path,
    profile_id: str,
    bearing_step_deg: float,
    minimum_score: float,
    minimum_ambiguity_db: float,
    maximum_residual_phase_deg: float,
) -> dict[str, Any]:
    """Load, calibrate, and solve one JSON snapshot with strict identities."""

    raw: Any = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ValueError("tracking snapshot has an unsupported schema")
    profiles = load_ism_band_profiles(frequency_plan_path)
    if profile_id not in profiles:
        raise ValueError(f"unknown ISM profile {profile_id!r}")
    profile = profiles[profile_id]
    declared_profile = raw.get("profile_id")
    if declared_profile != profile_id:
        raise ValueError(
            f"snapshot profile {declared_profile!r} does not match requested {profile_id!r}"
        )
    declared_ports = tuple(raw.get("ports", ()))
    if declared_ports != profile.geometry.ports:
        raise ValueError("snapshot ports do not match the immutable profile order")
    frequency_hz = raw.get("frequency_hz")
    if not isinstance(frequency_hz, (int, float)) or not profile.includes(float(frequency_hz)):
        raise ValueError("snapshot frequency is outside its ISM profile")
    reference_valid = raw.get("same_emitter_reference_valid")
    continuous_phase_valid = raw.get("continuous_phase_valid")
    if reference_valid is not True and continuous_phase_valid is not True:
        raise ValueError("snapshot has neither a same-emitter reference nor continuous phase")
    transfer = _complex_vector(raw, declared_ports)
    weights = np.asarray(raw.get("weights", np.ones(transfer.size)), dtype=np.float64)
    if weights.shape != transfer.shape:
        raise ValueError("snapshot weights disagree with the transfer vector")

    lut = BoardCalibrationLut.load(board_lut_path)
    calibration = lut.evaluate(float(frequency_hz), declared_ports)
    calibrated = transfer * calibration.coefficients
    if not np.isfinite(bearing_step_deg) or not 0.01 <= bearing_step_deg <= 10.0:
        raise ValueError("bearing step must lie in [0.01, 10] degrees")
    bearings = np.arange(0.0, 360.0, bearing_step_deg, dtype=np.float64)
    steering = far_field_steering(profile.geometry, float(frequency_hz), bearings)
    estimate = solve_bearing(
        calibrated,
        steering,
        bearings,
        weights=weights,
        minimum_score=minimum_score,
        minimum_ambiguity_margin_db=minimum_ambiguity_db,
        maximum_residual_phase_rms_deg=maximum_residual_phase_deg,
    )
    result: dict[str, Any] = {
        "schema": 1,
        "analysis_kind": "calibrated_ism_far_field_bearing",
        "source_snapshot": str(snapshot_path.resolve()),
        "profile_id": profile.profile_id,
        "frequency_hz": float(frequency_hz),
        "ports": list(declared_ports),
        "phase_observability": {
            "same_emitter_reference_valid": reference_valid is True,
            "continuous_phase_valid": continuous_phase_valid is True,
        },
        "board_calibration": {
            "source": str(lut.source),
            "status": calibration.calibration_status,
            "exact_knot": calibration.exact_knot,
            "interpolation_validated": calibration.interpolation_validated,
        },
        "bearing": {
            "valid": estimate.valid,
            "reasons": list(estimate.reasons),
            "bearing_deg_clockwise_from_forward": estimate.bearing_deg,
            "score": estimate.score,
            "second_bearing_deg_clockwise_from_forward": estimate.second_bearing_deg,
            "ambiguity_margin_db": estimate.ambiguity_margin_db,
            "residual_phase_rms_deg": estimate.residual_phase_rms_deg,
        },
        "likelihood": {
            "bearings_deg_clockwise_from_forward": estimate.bearings_deg.tolist(),
            "normalized_score": estimate.likelihood.tolist(),
        },
    }
    # Do not claim a fully calibrated low-band result merely because the
    # mathematical solver found a sharp ideal-manifold peak.
    if not calibration.interpolation_validated:
        bearing_document = result["bearing"]
        if not isinstance(bearing_document, dict):
            raise AssertionError("internal bearing result is not a mapping")
        bearing_document["valid"] = False
        reasons = bearing_document["reasons"]
        if not isinstance(reasons, list):
            raise AssertionError("internal bearing reasons are not a list")
        reasons.append("board_interpolation_not_independently_validated")
    return result


def main() -> int:
    args = _parser().parse_args()
    document = solve_snapshot(
        args.snapshot,
        frequency_plan_path=args.frequency_plan,
        board_lut_path=args.board_lut,
        profile_id=args.profile_id,
        bearing_step_deg=args.bearing_step_deg,
        minimum_score=args.minimum_score,
        minimum_ambiguity_db=args.minimum_ambiguity_db,
        maximum_residual_phase_deg=args.maximum_residual_phase_deg,
    )
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
