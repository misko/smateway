"""Bounded receive-only capture composed from pluto-plus-utils primitives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pluto_plus.analysis import SignalQualityAnalyzer, SpectrumAnalyzer
from pluto_plus.artifacts import CaptureWriter, data_path, verify_artifact
from pluto_plus.hardware import RadioDevice
from pluto_plus.models import ArtifactSummary, RadioSettings

from .decoder import ObservedInterval, intervals_from_presence

MAX_CAPTURE_SAMPLES = 16_777_216
DEFAULT_BLOCK_SAMPLES = 65_536


@dataclass(frozen=True, slots=True)
class EnvelopeDetection:
    intervals: tuple[ObservedInterval, ...]
    baseline_db: float
    threshold_db: float
    bin_duration_ms: float


def capture_receive_only(
    device: RadioDevice,
    *,
    requested_settings: RadioSettings,
    sample_count: int,
    artifact_root: Path,
    label: str,
    block_samples: int = DEFAULT_BLOCK_SAMPLES,
) -> ArtifactSummary:
    """Capture bounded IQ after the reused adapter mutes every TX path on open."""

    if sample_count < 1 or sample_count > MAX_CAPTURE_SAMPLES:
        raise ValueError(f"sample_count must be 1..{MAX_CAPTURE_SAMPLES}")
    if block_samples < 1 or block_samples > 1_048_576:
        raise ValueError("block_samples must be 1..1048576")

    writer: CaptureWriter | None = None
    device.open()
    try:
        actual_settings = device.apply_settings(requested_settings)
        writer = CaptureWriter(
            artifact_root,
            radio=device.identity,
            settings=actual_settings,
            label=label,
        )
        remaining = sample_count
        while remaining:
            count = min(remaining, block_samples)
            writer.append(device.read_block(count), actual_settings, revision=1)
            remaining -= count
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise RuntimeError("pluto-plus-utils artifact digest verification failed")
        return artifact
    except Exception as error:
        if writer is not None:
            writer.fail(error)
        raise
    finally:
        device.close()


def analyze_receive_capture(
    artifact: ArtifactSummary, *, fft_size: int = 4096
) -> dict[str, dict[str, Any]]:
    """Run the reusable spectrum and signal-health analyzers."""

    return {
        "spectrum": SpectrumAnalyzer().run(artifact, {"fft_size": fft_size}),
        "quality": SignalQualityAnalyzer().run(artifact, {}),
    }


def detect_envelope_intervals(
    artifact: ArtifactSummary,
    *,
    receiver: int = 1,
    bin_ms: float = 1.0,
    threshold_db_above_baseline: float = 6.0,
) -> EnvelopeDetection:
    """Convert one receiver's immutable CI16 envelope into timed present/absent runs."""

    if receiver < 0 or receiver >= artifact.receiver_count:
        raise ValueError("receiver is outside the capture")
    if bin_ms <= 0:
        raise ValueError("bin_ms must be positive")
    if threshold_db_above_baseline <= 0:
        raise ValueError("threshold margin must be positive")
    samples_per_bin = round(artifact.sample_rate_hz * bin_ms / 1000.0)
    if samples_per_bin < 1:
        raise ValueError("bin duration is shorter than one sample")
    complete_bins = artifact.sample_count // samples_per_bin
    if complete_bins < 1:
        raise ValueError("capture is shorter than one envelope bin")

    raw = np.memmap(data_path(artifact), dtype="<i2", mode="r")
    expected_components = artifact.sample_count * artifact.receiver_count * 2
    if raw.size != expected_components:
        raise ValueError("IQ component count does not match artifact metadata")
    components = raw.reshape(artifact.sample_count, artifact.receiver_count, 2)
    selected = components[: complete_bins * samples_per_bin, receiver]
    shaped = selected.reshape(complete_bins, samples_per_bin, 2).astype(np.float64)
    power = np.mean(shaped[:, :, 0] ** 2 + shaped[:, :, 1] ** 2, axis=1)
    power_db = 10.0 * np.log10(power + np.finfo(np.float64).tiny)
    baseline_db = float(np.percentile(power_db, 20.0))
    threshold_db = baseline_db + threshold_db_above_baseline
    presence = tuple(bool(value >= threshold_db) for value in power_db)
    actual_bin_ms = samples_per_bin * 1000.0 / artifact.sample_rate_hz
    return EnvelopeDetection(
        intervals=intervals_from_presence(presence, bin_duration_ms=actual_bin_ms),
        baseline_db=baseline_db,
        threshold_db=threshold_db,
        bin_duration_ms=actual_bin_ms,
    )
