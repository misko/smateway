from pathlib import Path

from pluto_plus.artifacts import verify_artifact
from pluto_plus.hardware import FakeRadioDevice
from pluto_plus.models import GainMode, RadioSettings

from smateway.pluto_capture import analyze_receive_capture, capture_receive_only


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
