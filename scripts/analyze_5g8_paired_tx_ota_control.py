#!/usr/bin/env python3
"""Reproduce the retained paired-TX 5.8 GHz OTA ALL_OFF control.

This is a diagnostic control, not a closed-loop calibration.  It streams the
ten exact-5.8-GHz raw captures named by the dual-band phase-distribution
manifest, projects both receivers at the retained refined pilot frequency, and
uses a separately admitted, perturbation-tested Fast20 cycle/marker alignment
to measure the raw RX2/RX1 transfer during selector ALL_OFF intervals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import atan2, isfinite, log10, pi, sqrt
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

import smateway.capture_continuity as capture_continuity_library
import smateway.hexcal as hexcal_library
import smateway.profile as profile_library
import smateway.schedule_alignment as schedule_alignment_library
from smateway.capture_continuity import validate_sigmf_continuity
from smateway.hexcal import sha256_path, write_json_atomic
from smateway.profile import ControlProfile, load_profile
from smateway.schedule_alignment import complete_cycle_ids, labels_and_interior

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
EXACT_CENTER_FREQUENCY_HZ = 5_800_000_000
EXPECTED_SAMPLE_RATE_HZ = 1_000_000
EXPECTED_SAMPLE_COUNT = 10_000_000
EXPECTED_RECEIVER_COUNT = 2
EXPECTED_REPEATS_PER_TX = 5
SAMPLES_PER_BIN = 1_000
EDGE_EXCLUSION_MS = 2.0
TIMING_PERTURBATION_MS = 2.0
MINIMUM_TIMING_ALIGNMENT_SCORE = 0.80
MINIMUM_TIMING_CONFIDENCE = 0.90
MINIMUM_TIMING_EVEN_ODD_AGREEMENT = 0.99
MINIMUM_TIMING_JACKKNIFE_STABILITY = 0.99
MAXIMUM_TIMING_AMPLITUDE_SPAN_DB = 0.10
MAXIMUM_TIMING_PHASE_SPAN_DEG = 0.50
MINIMUM_PILOT_CONFIDENCE = 0.90
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.995
MAXIMUM_PILOT_PHASE_RESIDUAL_RMS_RAD = 0.10
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class AllOffControlEstimate:
    """One artifact's cycle-robust raw ALL_OFF transfer estimate."""

    artifact_id: str
    tx_channel: int
    tx_name: str
    created_at: str
    receiver_gain_db: float
    tx_gain_db: float
    refined_pilot_offset_hz: float
    cycle_ms: float
    marker_phase_ms: float
    retained_alignment_score: float
    retained_phase_confidence: float
    retained_even_odd_cycle_agreement: float
    retained_jackknife_stability: float
    retained_phase_quality_passed: bool
    timing_perturbation_ms: float
    timing_sensitivity_amplitude_span_db: float
    timing_sensitivity_phase_span_deg: float
    timing_robustness_passed: bool
    complete_cycle_count: int
    all_off_bin_count: int
    rx1_amplitude_counts: float
    rx2_amplitude_counts: float
    raw_rx2_over_rx1_real: float
    raw_rx2_over_rx1_imag: float
    raw_rx2_over_rx1_amplitude: float
    raw_rx2_over_rx1_amplitude_db: float
    raw_rx2_over_rx1_phase_deg: float
    cycle_phase_coherence: float
    cycle_phase_rms_deg: float
    metadata_sha256: str
    raw_data_sha256: str
    raw_data_bytes: int
    dwell_sidecar_sha256: str
    phase_sidecar_sha256: str


@dataclass(frozen=True, slots=True)
class AllOffWindowEstimate:
    """One timing hypothesis's robust ALL_OFF estimate."""

    center: complex
    rx1_center: complex
    rx2_center: complex
    cycle_transfer: npt.NDArray[np.complex128]
    complete_cycle_count: int
    all_off_bin_count: int


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _manifest_exact_5g8_attempts(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("phase-distribution manifest has no attempts array")
    selected: list[dict[str, Any]] = []
    for raw in attempts:
        if not isinstance(raw, Mapping):
            raise ValueError("phase-distribution attempt must be an object")
        if raw.get("center_frequency_hz") != EXACT_CENTER_FREQUENCY_HZ:
            continue
        if raw.get("status") != "complete":
            raise ValueError("exact-5.8-GHz manifest attempt is not complete")
        artifact_id = raw.get("artifact_id")
        tx_channel = raw.get("tx_channel")
        if not isinstance(artifact_id, str) or len(artifact_id) != 32:
            raise ValueError("exact-5.8-GHz attempt artifact ID is malformed")
        if isinstance(tx_channel, bool) or tx_channel not in (0, 1):
            raise ValueError("exact-5.8-GHz attempt TX channel is malformed")
        selected.append({"artifact_id": artifact_id, "tx_channel": int(tx_channel)})
    if len(selected) != 2 * EXPECTED_REPEATS_PER_TX:
        raise ValueError("manifest must contain exactly five exact-5.8-GHz repeats per TX")
    for tx_channel in (0, 1):
        if sum(item["tx_channel"] == tx_channel for item in selected) != EXPECTED_REPEATS_PER_TX:
            raise ValueError("manifest exact-5.8-GHz TX repeat counts are unbalanced")
    if len({item["artifact_id"] for item in selected}) != len(selected):
        raise ValueError("manifest reuses an exact-5.8-GHz artifact")
    return tuple(selected)


def _validate_continuity(
    metadata: Mapping[str, Any], *, sample_count: int
) -> dict[str, int | None]:
    try:
        return validate_sigmf_continuity(
            metadata, expected_total_samples=sample_count
        ).as_dict()
    except ValueError as error:
        raise ValueError(f"strict ABI-2 continuity validation failed: {error}") from error


def _sha256_stream(path: Path, *, chunk_bytes: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _coherent_bins(
    data_path: Path,
    *,
    sample_count: int,
    sample_rate_hz: float,
    tone_offset_hz: float,
) -> npt.NDArray[np.complex128]:
    if sample_count % SAMPLES_PER_BIN:
        raise ValueError("sample count is not divisible by the coherent bin size")
    expected_components = sample_count * EXPECTED_RECEIVER_COUNT * 2
    raw = np.memmap(data_path, dtype="<i2", mode="r")
    if raw.size != expected_components:
        raise ValueError("raw CI16 size disagrees with metadata")
    components = raw.reshape(sample_count, EXPECTED_RECEIVER_COUNT, 2)
    bin_count = sample_count // SAMPLES_PER_BIN
    output = np.empty((EXPECTED_RECEIVER_COUNT, bin_count), dtype=np.complex128)
    bins_per_chunk = 200
    for bin_start in range(0, bin_count, bins_per_chunk):
        bin_stop = min(bin_count, bin_start + bins_per_chunk)
        sample_start = bin_start * SAMPLES_PER_BIN
        sample_stop = bin_stop * SAMPLES_PER_BIN
        indices = np.arange(sample_start, sample_stop, dtype=np.float64)
        oscillator = np.exp(-2j * pi * tone_offset_hz * indices / sample_rate_hz)
        for receiver in range(EXPECTED_RECEIVER_COUNT):
            samples = components[sample_start:sample_stop, receiver]
            complex_samples = samples[:, 0].astype(np.float64) + 1j * samples[:, 1].astype(
                np.float64
            )
            output[receiver, bin_start:bin_stop] = (
                complex_samples * oscillator
            ).reshape(bin_stop - bin_start, SAMPLES_PER_BIN).mean(axis=1)
    return output


def _robust_complex_center(values: npt.NDArray[np.complex128]) -> complex:
    if values.ndim != 1 or not values.size:
        raise ValueError("phasor vector must be non-empty")
    return complex(float(np.median(values.real)), float(np.median(values.imag)))


def _validate_sidecar_binding(
    sidecar: Mapping[str, Any],
    *,
    label: str,
    artifact_id: str,
    raw_sha256: str,
    sample_count: int,
    sample_rate_hz: float,
    expected_tx_channel: int,
    profile: ControlProfile,
    continuity: Mapping[str, int | None],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if sidecar.get("schema") != 1:
        raise ValueError(f"{label} schema is not exactly 1")
    artifact = sidecar.get("artifact")
    capture = sidecar.get("capture")
    if not isinstance(artifact, Mapping) or not isinstance(capture, Mapping):
        raise ValueError(f"{label} artifact/capture binding is malformed")
    if (
        artifact.get("artifact_id") != artifact_id
        or artifact.get("sha256") != raw_sha256
        or _integer(artifact.get("sample_count"), f"{label} artifact sample count")
        != sample_count
        or _number(artifact.get("sample_rate_hz"), f"{label} artifact sample rate")
        != sample_rate_hz
        or _number(artifact.get("center_frequency_hz"), f"{label} artifact center")
        != EXACT_CENTER_FREQUENCY_HZ
        or _integer(artifact.get("receiver_count"), f"{label} receiver count")
        != EXPECTED_RECEIVER_COUNT
    ):
        raise ValueError(f"{label} artifact identity disagrees with raw SigMF evidence")
    if (
        _integer(capture.get("sample_count"), f"{label} capture sample count")
        != sample_count
        or _number(capture.get("sample_rate_hz"), f"{label} capture sample rate")
        != sample_rate_hz
        or _number(capture.get("center_frequency_hz"), f"{label} capture center")
        != EXACT_CENTER_FREQUENCY_HZ
        or _integer(capture.get("metadata_abi"), f"{label} metadata ABI") != 2
        or _integer(capture.get("tx_channel"), f"{label} TX channel")
        != expected_tx_channel
        or capture.get("stimulus") != "phase"
        or capture.get("profile_contract_sha256") != profile.contract_sha256
        or _integer(capture.get("stream_id"), f"{label} stream ID")
        != continuity["stream_id"]
        or _integer(
            capture.get("first_sample_sequence"), f"{label} first sample sequence"
        )
        != continuity["first_sample_sequence"]
        or _integer(
            capture.get("last_sample_sequence_exclusive"),
            f"{label} last sample sequence",
        )
        != continuity["last_sample_sequence_exclusive"]
    ):
        raise ValueError(f"{label} capture contract disagrees with raw SigMF evidence")
    return artifact, capture


def _all_off_window_estimate(
    *,
    bins: npt.NDArray[np.complex128],
    transfer: npt.NDArray[np.complex128],
    reference_valid: npt.NDArray[np.bool_],
    times_ms: npt.NDArray[np.float64],
    duration_ms: float,
    cycle_ms: float,
    marker_phase_ms: float,
    profile: ControlProfile,
) -> AllOffWindowEstimate:
    labels, interior = labels_and_interior(
        times_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        edge_exclusion_ms=EDGE_EXCLUSION_MS,
        profile=profile,
    )
    cycle_ids, complete_ids = complete_cycle_ids(
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
    )
    all_off = (
        reference_valid
        & interior
        & np.isin(cycle_ids, complete_ids)
        & (labels == len(profile.states))
    )
    cycle_transfer: list[complex] = []
    cycle_rx1: list[complex] = []
    cycle_rx2: list[complex] = []
    for cycle_id in complete_ids:
        selected = all_off & (cycle_ids == cycle_id)
        if not np.any(selected):
            raise ValueError("complete cycle has no admitted ALL_OFF bins")
        cycle_transfer.append(complex(np.mean(transfer[selected])))
        cycle_rx1.append(complex(np.mean(bins[0, selected])))
        cycle_rx2.append(complex(np.mean(bins[1, selected])))
    transfer_values = np.asarray(cycle_transfer, dtype=np.complex128)
    center = _robust_complex_center(transfer_values)
    if abs(center) <= np.finfo(np.float64).tiny:
        raise ValueError("ALL_OFF transfer center is zero")
    return AllOffWindowEstimate(
        center=center,
        rx1_center=_robust_complex_center(np.asarray(cycle_rx1, dtype=np.complex128)),
        rx2_center=_robust_complex_center(np.asarray(cycle_rx2, dtype=np.complex128)),
        cycle_transfer=transfer_values,
        complete_cycle_count=len(complete_ids),
        all_off_bin_count=int(np.sum(all_off)),
    )


def _analyze_artifact(
    artifact_root: Path,
    *,
    expected_tx_channel: int,
    profile_path: Path,
) -> tuple[AllOffControlEstimate, dict[str, int | None]]:
    artifact_id = artifact_root.name
    metadata_path = artifact_root / f"{artifact_id}.sigmf-meta"
    data_path = artifact_root / f"{artifact_id}.sigmf-data"
    dwell_path = artifact_root / "fast20-dwell-isolation.json"
    phase_path = artifact_root / "fast20-relative-phase.json"
    for path in (metadata_path, data_path, dwell_path, phase_path):
        if not path.is_file():
            raise ValueError(f"artifact is missing required source: {path.name}")

    metadata = _read_json(metadata_path, "SigMF metadata")
    global_metadata = metadata.get("global")
    captures = metadata.get("captures")
    capture_metadata = metadata.get("pluto:capture")
    if (
        not isinstance(global_metadata, Mapping)
        or not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
        or not isinstance(capture_metadata, Mapping)
    ):
        raise ValueError("SigMF metadata structure is not canonical")
    settings = captures[0].get("settings")
    if not isinstance(settings, Mapping):
        raise ValueError("SigMF capture settings are malformed")
    sample_count = _integer(capture_metadata.get("sample_count"), "sample count")
    sample_rate_hz = _number(settings.get("sample_rate_hz"), "sample rate")
    if (
        global_metadata.get("pluto:artifact_id") != artifact_id
        or global_metadata.get("core:datatype") != "ci16_le"
        or global_metadata.get("core:num_channels") != EXPECTED_RECEIVER_COUNT
        or settings.get("center_frequency_hz") != EXACT_CENTER_FREQUENCY_HZ
        or tuple(settings.get("channels", ())) != (0, 1)
        or sample_count != EXPECTED_SAMPLE_COUNT
        or sample_rate_hz != EXPECTED_SAMPLE_RATE_HZ
    ):
        raise ValueError("artifact is not the canonical exact-5.8-GHz dual-RX capture")
    expected_bytes = sample_count * EXPECTED_RECEIVER_COUNT * 2 * np.dtype("<i2").itemsize
    if data_path.stat().st_size != expected_bytes:
        raise ValueError("raw data byte length disagrees with canonical CI16 layout")
    raw_sha256 = _sha256_stream(data_path)
    if global_metadata.get("pluto:sha256") != raw_sha256:
        raise ValueError("raw data SHA-256 disagrees with SigMF metadata")
    continuity = _validate_continuity(metadata, sample_count=sample_count)
    profile = load_profile(profile_path)

    dwell = _read_json(dwell_path, "dwell sidecar")
    phase = _read_json(phase_path, "relative-phase sidecar")
    _, capture = _validate_sidecar_binding(
        dwell,
        label="dwell sidecar",
        artifact_id=artifact_id,
        raw_sha256=raw_sha256,
        sample_count=sample_count,
        sample_rate_hz=sample_rate_hz,
        expected_tx_channel=expected_tx_channel,
        profile=profile,
        continuity=continuity,
    )
    _, phase_capture = _validate_sidecar_binding(
        phase,
        label="relative-phase sidecar",
        artifact_id=artifact_id,
        raw_sha256=raw_sha256,
        sample_count=sample_count,
        sample_rate_hz=sample_rate_hz,
        expected_tx_channel=expected_tx_channel,
        profile=profile,
        continuity=continuity,
    )
    if capture != phase_capture:
        raise ValueError("dwell and relative-phase capture contracts disagree")
    if phase.get("analysis_kind") != "fast20_rx1_referenced_relative_phase":
        raise ValueError("relative-phase sidecar analysis kind is not canonical")
    pilot = dwell.get("pilot")
    phase_pilot = phase.get("pilot")
    phase_result = phase.get("phase")
    phase_gate = phase.get("quality_gate")
    if (
        not isinstance(pilot, Mapping)
        or not isinstance(phase_pilot, Mapping)
        or not isinstance(phase_result, Mapping)
        or not isinstance(phase_gate, Mapping)
    ):
        raise ValueError("retained pilot/alignment sidecars are malformed")
    if pilot != phase_pilot:
        raise ValueError("dwell and relative-phase pilot estimates disagree")
    retained_phase_quality_passed = phase_gate.get("passed")
    if not isinstance(retained_phase_quality_passed, bool):
        raise ValueError("retained phase quality decision is malformed")
    if phase_result.get("continuity_verified") is not True:
        raise ValueError("retained phase alignment was not continuity-verified")
    tx_channel = _integer(capture.get("tx_channel"), "capture TX channel")
    if tx_channel != expected_tx_channel:
        raise ValueError("manifest and capture TX channel disagree")
    refined_pilot_hz = _number(pilot.get("estimated_offset_hz"), "refined pilot")
    if (
        _number(pilot.get("confidence"), "pilot confidence") < MINIMUM_PILOT_CONFIDENCE
        or _number(pilot.get("phase_step_coherence"), "pilot phase-step coherence")
        < MINIMUM_PILOT_PHASE_STEP_COHERENCE
        or _number(pilot.get("phase_residual_rms_rad"), "pilot phase residual RMS")
        > MAXIMUM_PILOT_PHASE_RESIDUAL_RMS_RAD
    ):
        raise ValueError("retained pilot fails the diagnostic quality contract")
    cycle_ms = _number(phase_result.get("cycle_ms"), "cycle_ms")
    marker_phase_ms = _number(phase_result.get("marker_phase_ms"), "marker_phase_ms")
    retained_alignment_score = _number(
        phase_result.get("alignment_score"), "retained alignment score"
    )
    retained_phase_confidence = _number(
        phase_result.get("confidence"), "retained phase confidence"
    )
    retained_even_odd_cycle_agreement = _number(
        phase_result.get("even_odd_cycle_agreement"), "retained even/odd agreement"
    )
    retained_jackknife_stability = _number(
        phase_result.get("jackknife_stability"), "retained jackknife stability"
    )
    retained_complete_cycle_count = _integer(
        phase_result.get("complete_cycle_count"), "retained complete-cycle count"
    )
    if (
        retained_alignment_score < MINIMUM_TIMING_ALIGNMENT_SCORE
        or retained_phase_confidence < MINIMUM_TIMING_CONFIDENCE
        or retained_even_odd_cycle_agreement < MINIMUM_TIMING_EVEN_ODD_AGREEMENT
        or retained_jackknife_stability < MINIMUM_TIMING_JACKKNIFE_STABILITY
        or retained_complete_cycle_count < 20
    ):
        raise ValueError("retained timing fails the independent diagnostic timing gate")

    bins = _coherent_bins(
        data_path,
        sample_count=sample_count,
        sample_rate_hz=sample_rate_hz,
        tone_offset_hz=refined_pilot_hz,
    )
    rx1_magnitude = np.abs(bins[0])
    reference_threshold = 0.2 * float(np.median(rx1_magnitude))
    reference_valid = rx1_magnitude >= reference_threshold
    if float(np.mean(reference_valid)) < 0.95:
        raise ValueError("RX1 reference is not continuously usable")
    transfer = np.zeros(bins.shape[1], dtype=np.complex128)
    transfer[reference_valid] = bins[1, reference_valid] / bins[0, reference_valid]

    bin_duration_ms = SAMPLES_PER_BIN * 1_000.0 / sample_rate_hz
    times_ms = (np.arange(bins.shape[1], dtype=np.float64) + 0.5) * bin_duration_ms
    duration_ms = sample_count * 1_000.0 / sample_rate_hz
    baseline = _all_off_window_estimate(
        bins=bins,
        transfer=transfer,
        reference_valid=reference_valid,
        times_ms=times_ms,
        duration_ms=duration_ms,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        profile=profile,
    )
    if baseline.complete_cycle_count != retained_complete_cycle_count:
        raise ValueError("recomputed and retained complete-cycle counts disagree")
    perturbed = [
        _all_off_window_estimate(
            bins=bins,
            transfer=transfer,
            reference_valid=reference_valid,
            times_ms=times_ms,
            duration_ms=duration_ms,
            cycle_ms=cycle_ms,
            marker_phase_ms=marker_phase_ms + offset_ms,
            profile=profile,
        )
        for offset_ms in (-TIMING_PERTURBATION_MS, 0.0, TIMING_PERTURBATION_MS)
    ]
    perturbed_centers = np.asarray([item.center for item in perturbed], dtype=np.complex128)
    perturbed_amplitude_db = 20.0 * np.log10(np.abs(perturbed_centers))
    perturbed_phase_deg = np.unwrap(np.angle(perturbed_centers)) * 180.0 / pi
    timing_amplitude_span_db = float(np.ptp(perturbed_amplitude_db))
    timing_phase_span_deg = float(np.ptp(perturbed_phase_deg))
    if (
        timing_amplitude_span_db > MAXIMUM_TIMING_AMPLITUDE_SPAN_DB
        or timing_phase_span_deg > MAXIMUM_TIMING_PHASE_SPAN_DEG
    ):
        raise ValueError("ALL_OFF result is not robust to the timing perturbation contract")

    center = baseline.center
    transfer_values = baseline.cycle_transfer
    units = transfer_values / np.maximum(np.abs(transfer_values), np.finfo(np.float64).tiny)
    phase_residuals = np.angle(transfer_values * np.conj(center))
    created_at = global_metadata.get("pluto:created_at")
    if not isinstance(created_at, str):
        raise ValueError("artifact creation time is malformed")
    estimate = AllOffControlEstimate(
        artifact_id=artifact_id,
        tx_channel=tx_channel,
        tx_name=f"TX{tx_channel + 1}",
        created_at=created_at,
        receiver_gain_db=_number(settings.get("gain_db"), "receiver gain"),
        tx_gain_db=_number(capture.get("tx_gain_readback_db"), "TX gain"),
        refined_pilot_offset_hz=refined_pilot_hz,
        cycle_ms=cycle_ms,
        marker_phase_ms=marker_phase_ms,
        retained_alignment_score=retained_alignment_score,
        retained_phase_confidence=retained_phase_confidence,
        retained_even_odd_cycle_agreement=retained_even_odd_cycle_agreement,
        retained_jackknife_stability=retained_jackknife_stability,
        retained_phase_quality_passed=retained_phase_quality_passed,
        timing_perturbation_ms=TIMING_PERTURBATION_MS,
        timing_sensitivity_amplitude_span_db=timing_amplitude_span_db,
        timing_sensitivity_phase_span_deg=timing_phase_span_deg,
        timing_robustness_passed=True,
        complete_cycle_count=baseline.complete_cycle_count,
        all_off_bin_count=baseline.all_off_bin_count,
        rx1_amplitude_counts=abs(baseline.rx1_center),
        rx2_amplitude_counts=abs(baseline.rx2_center),
        raw_rx2_over_rx1_real=center.real,
        raw_rx2_over_rx1_imag=center.imag,
        raw_rx2_over_rx1_amplitude=abs(center),
        raw_rx2_over_rx1_amplitude_db=20.0 * log10(abs(center)),
        raw_rx2_over_rx1_phase_deg=atan2(center.imag, center.real) * 180.0 / pi,
        cycle_phase_coherence=float(abs(np.mean(units))),
        cycle_phase_rms_deg=sqrt(float(np.mean(phase_residuals**2))) * 180.0 / pi,
        metadata_sha256=sha256_path(metadata_path),
        raw_data_sha256=raw_sha256,
        raw_data_bytes=expected_bytes,
        dwell_sidecar_sha256=sha256_path(dwell_path),
        phase_sidecar_sha256=sha256_path(phase_path),
    )
    return estimate, continuity


def _group_summary(
    estimates: Sequence[AllOffControlEstimate], *, tx_channel: int
) -> dict[str, Any]:
    selected = [item for item in estimates if item.tx_channel == tx_channel]
    if len(selected) != EXPECTED_REPEATS_PER_TX:
        raise ValueError("paired-TX group does not have exactly five repeats")
    amplitudes_db = np.asarray(
        [item.raw_rx2_over_rx1_amplitude_db for item in selected], dtype=np.float64
    )
    phases = np.radians([item.raw_rx2_over_rx1_phase_deg for item in selected])
    mean_unit = complex(float(np.mean(np.cos(phases))), float(np.mean(np.sin(phases))))
    return {
        "tx_channel": tx_channel,
        "tx_name": f"TX{tx_channel + 1}",
        "repeat_count": len(selected),
        "artifact_ids": [item.artifact_id for item in selected],
        "raw_rx2_over_rx1_amplitude_db_median": float(np.median(amplitudes_db)),
        "raw_rx2_over_rx1_amplitude_db_minimum": float(np.min(amplitudes_db)),
        "raw_rx2_over_rx1_amplitude_db_maximum": float(np.max(amplitudes_db)),
        "raw_rx2_over_rx1_phase_circular_mean_deg": atan2(mean_unit.imag, mean_unit.real)
        * 180.0
        / pi,
        "cross_repeat_phase_coherence": abs(mean_unit),
        "retained_phase_quality_pass_count": sum(
            item.retained_phase_quality_passed for item in selected
        ),
        "timing_robustness_pass_count": sum(item.timing_robustness_passed for item in selected),
        "timing_sensitivity_amplitude_span_db_maximum": max(
            item.timing_sensitivity_amplitude_span_db for item in selected
        ),
        "timing_sensitivity_phase_span_deg_maximum": max(
            item.timing_sensitivity_phase_span_deg for item in selected
        ),
        "rx1_amplitude_counts_median": float(
            np.median([item.rx1_amplitude_counts for item in selected])
        ),
        "rx2_amplitude_counts_median": float(
            np.median([item.rx2_amplitude_counts for item in selected])
        ),
    }


def _repository_source_binding(path: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise ValueError(f"implementation source is outside repository: {path}") from error
    if not resolved.is_file():
        raise ValueError(f"implementation source is not a file: {path}")
    return {"path": relative, "sha256": sha256_path(resolved)}


def _resolved_relative_file(path: Path, root: Path, label: str) -> tuple[Path, str]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"{label} escapes its required root: {path}") from error
    if not resolved.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return resolved, relative


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument(
        "--board-state-root",
        type=Path,
        default=Path.home() / ".local/state/smateway/boards",
    )
    parser.add_argument(
        "--manifest-relative-path",
        type=Path,
        default=Path("phase-distributions/dualband-phase-20260825-a/manifest.json"),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/5g8_root_cause_analysis/data/paired-tx-ota-all-off-control.json"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if Path(args.board_id).name != args.board_id or args.board_id in {"", ".", ".."}:
        raise ValueError("board ID must be one nonempty path component")
    board_state_root = args.board_state_root.expanduser().resolve(strict=True)
    if not board_state_root.is_dir():
        raise ValueError("board-state root must be a directory")
    board_root = (board_state_root / args.board_id).resolve(strict=True)
    if board_root.parent != board_state_root or not board_root.is_dir():
        raise ValueError("board ID does not identify one board-state directory")
    if args.manifest_relative_path.is_absolute():
        raise ValueError("manifest path must be relative to the board-state directory")
    manifest_path, manifest_relative_path = _resolved_relative_file(
        board_root / args.manifest_relative_path,
        board_root,
        "phase-distribution manifest",
    )
    profile_path, profile_relative_path = _resolved_relative_file(
        args.profile.expanduser(), REPOSITORY_ROOT, "control profile"
    )
    profile_header_path, profile_header_relative_path = _resolved_relative_file(
        profile_path.with_name("control_profile.h"),
        REPOSITORY_ROOT,
        "control profile header",
    )
    manifest = _read_json(manifest_path, "phase-distribution manifest")
    attempts = _manifest_exact_5g8_attempts(manifest)
    estimates: list[AllOffControlEstimate] = []
    continuity: dict[str, dict[str, int | None]] = {}
    for attempt in attempts:
        artifact_id = str(attempt["artifact_id"])
        estimate, ledger = _analyze_artifact(
            board_root / "pluto-usb-captures" / artifact_id,
            expected_tx_channel=int(attempt["tx_channel"]),
            profile_path=profile_path,
        )
        estimates.append(estimate)
        continuity[artifact_id] = ledger

    implementation_sources = [
        _repository_source_binding(Path(__file__)),
        _repository_source_binding(Path(capture_continuity_library.__file__)),
        _repository_source_binding(Path(profile_library.__file__)),
        _repository_source_binding(Path(schedule_alignment_library.__file__)),
        _repository_source_binding(Path(hexcal_library.__file__)),
    ]
    groups = [_group_summary(estimates, tx_channel=index) for index in (0, 1)]
    document: dict[str, Any] = {
        "schema": 1,
        "analysis_kind": "paired_tx_5g8_ota_all_off_control",
        "status": "diagnostic_only_not_closed_loop_attribution",
        "source": {
            "implementation_sources": implementation_sources,
            "manifest_relative_path": manifest_relative_path,
            "manifest_sha256": sha256_path(manifest_path),
            "profile": profile_relative_path,
            "profile_sha256": sha256_path(profile_path),
            "profile_header": profile_header_relative_path,
            "profile_header_sha256": sha256_path(profile_header_path),
            "board_id": board_root.name,
            "raw_storage_root_embedded": False,
        },
        "method": {
            "center_frequency_hz": EXACT_CENTER_FREQUENCY_HZ,
            "sample_rate_hz": EXPECTED_SAMPLE_RATE_HZ,
            "samples_per_coherent_bin": SAMPLES_PER_BIN,
            "coherent_bin_duration_ms": SAMPLES_PER_BIN
            * 1_000.0
            / EXPECTED_SAMPLE_RATE_HZ,
            "edge_exclusion_ms": EDGE_EXCLUSION_MS,
            "pilot_source": "retained refined RX1 pilot estimate",
            "alignment_source": (
                "retained best-fit Fast20 cycle and marker phase, admitted independently for "
                "this ALL_OFF-only diagnostic by continuity, timing confidence/stability, "
                "recomputed cycle count, and +/-2 ms perturbation checks; the original "
                "full-state phase decision is reported separately"
            ),
            "timing_admission": {
                "minimum_alignment_score": MINIMUM_TIMING_ALIGNMENT_SCORE,
                "minimum_confidence": MINIMUM_TIMING_CONFIDENCE,
                "minimum_even_odd_cycle_agreement": MINIMUM_TIMING_EVEN_ODD_AGREEMENT,
                "minimum_jackknife_stability": MINIMUM_TIMING_JACKKNIFE_STABILITY,
                "marker_phase_perturbation_ms": TIMING_PERTURBATION_MS,
                "maximum_amplitude_span_db": MAXIMUM_TIMING_AMPLITUDE_SPAN_DB,
                "maximum_phase_span_deg": MAXIMUM_TIMING_PHASE_SPAN_DEG,
            },
            "pilot_admission": {
                "minimum_confidence": MINIMUM_PILOT_CONFIDENCE,
                "minimum_phase_step_coherence": MINIMUM_PILOT_PHASE_STEP_COHERENCE,
                "maximum_phase_residual_rms_rad": MAXIMUM_PILOT_PHASE_RESIDUAL_RMS_RAD,
            },
            "transfer": "raw coherent RX2 / RX1 during ALL_OFF bins",
            "cycle_center": "component-wise median of complete-cycle complex means",
        },
        "groups": groups,
        "artifacts": [asdict(item) for item in estimates],
        "continuity": continuity,
        "interpretation": {
            "proven": (
                "Both TX configurations produced a highly coherent ALL_OFF component in the "
                "retained OTA localization geometry."
            ),
            "not_proven": (
                "The TX1 and TX2 antennas occupied different source positions and illuminated "
                "RX1 differently; their amplitudes cannot localize the later conducted "
                "closed-loop 5.8 GHz leakage path."
            ),
            "calibration_admissible": False,
        },
    }
    output = args.output.expanduser().resolve()
    write_json_atomic(output, document)
    print(
        json.dumps(
            {
                "output": str(output),
                "output_sha256": sha256_path(output),
                "artifact_count": len(estimates),
                "tx1_median_db": groups[0]["raw_rx2_over_rx1_amplitude_db_median"],
                "tx2_median_db": groups[1]["raw_rx2_over_rx1_amplitude_db_median"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
