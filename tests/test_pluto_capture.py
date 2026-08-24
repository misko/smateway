from pathlib import Path
from time import time_ns

import numpy as np
from pluto_plus.artifacts import CaptureWriter, verify_artifact
from pluto_plus.hardware import FakeRadioDevice, SampleBlock
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

from smateway.decoder import decode_intervals
from smateway.pluto_capture import (
    analyze_receive_capture,
    capture_receive_only,
    detect_envelope_intervals,
)
from smateway.profile import load_profile


def test_bounded_capture_reuses_pluto_artifact_and_analysis(tmp_path: Path) -> None:
    settings = RadioSettings(
        center_frequency_hz=915_000_000,
        sample_rate_hz=1_000_000,
        bandwidth_hz=1_000_000,
        gain_mode=GainMode.MANUAL,
        gain_db=20,
        channels=(0, 1),
    )
    artifact = capture_receive_only(
        FakeRadioDevice(seed=7),
        requested_settings=settings,
        sample_count=8192,
        block_samples=4096,
        artifact_root=tmp_path,
        label="offline selector smoke test",
    )

    assert artifact.sample_count == 8192
    assert artifact.receiver_count == 2
    assert verify_artifact(artifact)
    analysis = analyze_receive_capture(artifact, fft_size=1024)
    assert len(analysis["spectrum"]["peaks"]) == 2
    assert len(analysis["quality"]["receivers"]) == 2


def test_sigmf_envelope_decodes_generated_complete_frame(tmp_path: Path) -> None:
    profile = load_profile(Path("profiles/fast20-v1/control_profile.json"))
    presence = [False] * (profile.marker_body_ms + profile.guard_ms)
    for index, state in enumerate(profile.states):
        presence.extend([True] * state.dwell_ms)
        if index + 1 < len(profile.states):
            presence.extend([False] * profile.guard_ms)

    sample_rate_hz = 100_000
    samples_per_bin = sample_rate_hz // 1000
    expanded = np.repeat(np.asarray(presence, dtype=bool), samples_per_bin)
    samples = np.zeros((2, expanded.size), dtype=np.complex64)
    samples[1, expanded] = 1000 + 1000j
    settings = RadioSettings(
        center_frequency_hz=915_000_000,
        sample_rate_hz=sample_rate_hz,
        bandwidth_hz=sample_rate_hz,
        gain_mode=GainMode.MANUAL,
        gain_db=20,
        channels=(0, 1),
    )
    writer = CaptureWriter(
        tmp_path,
        radio=RadioIdentity(
            radio_id="synthetic",
            serial="synthetic",
            uri="fake:synthetic",
            transport=Transport.FAKE,
        ),
        settings=settings,
        label="generated frame envelope",
    )
    writer.append(SampleBlock(utc_ns=time_ns(), samples=samples), settings, revision=1)
    artifact = writer.finalize()

    detection = detect_envelope_intervals(artifact, receiver=1, bin_ms=1.0)
    result = decode_intervals(detection.intervals, profile)

    assert detection.bin_duration_ms == 1.0
    assert result.status == "decoded"
    assert result.states == tuple(state.name for state in profile.states)
