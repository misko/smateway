"""Bounded receive-only capture composed from pluto-plus-utils primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pluto_plus.analysis import SignalQualityAnalyzer, SpectrumAnalyzer
from pluto_plus.artifacts import CaptureWriter, verify_artifact
from pluto_plus.hardware import RadioDevice
from pluto_plus.models import ArtifactSummary, RadioSettings

MAX_CAPTURE_SAMPLES = 16_777_216
DEFAULT_BLOCK_SAMPLES = 65_536


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
