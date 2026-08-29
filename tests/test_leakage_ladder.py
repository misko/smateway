import numpy as np
import pytest

from smateway.leakage_ladder import LeakageAnalysisThresholds, analyze_coherent_leakage

SAMPLE_RATE_HZ = 100_000
TONE_OFFSET_HZ = 10_000
SAMPLE_COUNT = 40_000


def _capture(
    *,
    rx1_phasor: complex = 400.0 * np.exp(0.4j),
    rx2_phasor: complex = 80.0 * np.exp(-0.7j),
    noise_sigma: float = 2.0,
    seed: int = 20260829,
) -> tuple[np.ndarray, np.ndarray]:
    indices = np.arange(SAMPLE_COUNT, dtype=np.float64)
    carrier = np.exp(2j * np.pi * TONE_OFFSET_HZ * indices / SAMPLE_RATE_HZ)
    rng = np.random.default_rng(seed)
    rx1_noise = noise_sigma * (
        rng.standard_normal(SAMPLE_COUNT) + 1j * rng.standard_normal(SAMPLE_COUNT)
    )
    rx2_noise = noise_sigma * (
        rng.standard_normal(SAMPLE_COUNT) + 1j * rng.standard_normal(SAMPLE_COUNT)
    )
    return (
        np.asarray(rx1_phasor * carrier + rx1_noise, dtype=np.complex64),
        np.asarray(rx2_phasor * carrier + rx2_noise, dtype=np.complex64),
    )


def test_recovers_robust_dual_rx_phasors_and_complex_transfer() -> None:
    expected_rx1 = 400.0 * np.exp(0.4j)
    expected_rx2 = 80.0 * np.exp(-0.7j)
    rx1, rx2 = _capture(rx1_phasor=expected_rx1, rx2_phasor=expected_rx2)

    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
    )

    assert analysis.quality_passed
    assert analysis.quality_rejection_reasons == ()
    assert analysis.sample_count == SAMPLE_COUNT
    assert analysis.block_count == 40
    assert analysis.rx1.phasor == pytest.approx(expected_rx1, rel=2e-3)
    assert analysis.rx2.phasor == pytest.approx(expected_rx2, rel=2e-3)
    assert analysis.rx1.tone_to_noise_snr_db == pytest.approx(43.0, abs=0.4)
    assert analysis.rx2.tone_to_noise_snr_db == pytest.approx(29.0, abs=0.4)
    assert analysis.rx1.block_phase_coherence >= 0.995
    assert analysis.rx1.block_phase_rms_deg is not None
    assert analysis.rx1.block_phase_rms_deg <= 6.0
    assert analysis.rx2.block_phase_coherence > 0.999
    assert analysis.rx1.adc_headroom_passed
    assert analysis.rx2.adc_headroom_passed

    transfer = analysis.rx2_over_rx1
    assert transfer.phasor == pytest.approx(expected_rx2 / expected_rx1, rel=3e-3)
    assert transfer.amplitude_ratio == pytest.approx(0.2, rel=3e-3)
    assert transfer.amplitude_db == pytest.approx(-13.9794, abs=0.02)
    assert transfer.phase_deg == pytest.approx(np.rad2deg(-1.1), abs=0.1)
    assert transfer.block_phase_coherence >= 0.995
    assert transfer.block_phase_rms_deg is not None
    assert transfer.block_phase_rms_deg <= 6.0
    assert transfer.valid_block_count == analysis.block_count
    assert transfer.valid_block_fraction == 1.0


def test_absent_rx2_tone_is_a_valid_low_leakage_outcome() -> None:
    rx1, rx2 = _capture(rx2_phasor=0j, noise_sigma=4.0)

    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
    )

    assert analysis.quality_passed
    assert analysis.rx1.tone_detected
    assert not analysis.rx2.tone_detected
    assert analysis.rx2.tone_to_noise_snr_db < 0.0
    assert analysis.rx2_over_rx1.phasor is not None
    assert analysis.rx2_over_rx1.valid_block_fraction == 1.0
    assert analysis.rx2_over_rx1.amplitude_upper_bound_ratio is not None
    assert analysis.rx2_over_rx1.amplitude_upper_bound_ratio > 0.0
    assert analysis.rx2_over_rx1.amplitude_upper_bound_db is not None
    assert "three scaled MAD" in analysis.rx2_over_rx1.amplitude_upper_bound_method


def test_robust_center_survives_outlier_but_strict_reference_quality_rejects_it() -> None:
    expected_rx1 = 400.0 * np.exp(0.4j)
    rx1, rx2 = _capture(rx1_phasor=expected_rx1)
    indices = np.arange(5_000, 6_000, dtype=np.float64)
    carrier = np.exp(2j * np.pi * TONE_OFFSET_HZ * indices / SAMPLE_RATE_HZ)
    rx1[5_000:6_000] += np.asarray(1_200.0j * carrier, dtype=np.complex64)

    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
    )

    assert analysis.rx1.phasor == pytest.approx(expected_rx1, rel=2e-3)
    assert not analysis.quality_passed
    assert "rx1_reference_coherence_below_minimum" in analysis.quality_rejection_reasons
    assert "rx1_reference_phase_rms_above_maximum" in analysis.quality_rejection_reasons


def test_detected_rx2_requires_strict_transfer_phase_quality() -> None:
    rx1, rx2 = _capture()
    block_samples = round(SAMPLE_RATE_HZ * 0.010)
    for block_index, start in enumerate(range(0, SAMPLE_COUNT, block_samples)):
        phase_deg = -12.0 if block_index % 2 == 0 else 12.0
        rx2[start : start + block_samples] *= np.exp(1j * np.deg2rad(phase_deg))

    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
    )

    assert analysis.rx2.tone_detected
    assert not analysis.quality_passed
    assert "detected_rx2_transfer_coherence_below_minimum" in analysis.quality_rejection_reasons
    assert "detected_rx2_transfer_phase_rms_above_maximum" in analysis.quality_rejection_reasons


def test_quality_gate_fails_closed_for_weak_reference_and_adc_clipping() -> None:
    rx1, rx2 = _capture(rx1_phasor=0j, noise_sigma=4.0)
    rx2[0] = 2_047.0 + 0j

    analysis = analyze_coherent_leakage(
        rx1,
        rx2,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
    )

    assert not analysis.quality_passed
    assert "rx1_reference_snr_below_minimum" in analysis.quality_rejection_reasons
    assert "rx1_reference_coherence_below_minimum" in analysis.quality_rejection_reasons
    assert "rx1_reference_phase_rms_above_maximum" in analysis.quality_rejection_reasons
    assert "rx2_adc_headroom_failed" in analysis.quality_rejection_reasons
    assert "reference_valid_block_fraction_below_minimum" in analysis.quality_rejection_reasons
    assert not analysis.rx2.adc_headroom_passed
    assert analysis.rx2.peak_abs_component_counts == 2_047.0
    assert analysis.rx2.adc_headroom_counts == 0.0
    assert analysis.rx2.adc_headroom_db == pytest.approx(0.0)
    assert analysis.rx2_over_rx1.phasor is None


@pytest.mark.parametrize(
    ("rx1", "rx2", "message"),
    [
        (np.ones(20, dtype=np.float32), np.ones(20, dtype=np.complex64), "RX1.*complex"),
        (np.ones((2, 20), dtype=np.complex64), np.ones(20, dtype=np.complex64), "RX1.*one"),
        (np.ones(20, dtype=np.complex64), np.ones(19, dtype=np.complex64), "equal length"),
        (
            np.asarray([1 + 0j] * 19 + [complex(np.nan, 0.0)]),
            np.ones(20, dtype=np.complex64),
            "RX1.*finite",
        ),
    ],
)
def test_rejects_invalid_sample_vectors(
    rx1: np.ndarray,
    rx2: np.ndarray,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        analyze_coherent_leakage(
            rx1,
            rx2,
            sample_rate_hz=1_000,
            tone_offset_hz=100,
            block_duration_s=0.002,
            minimum_block_count=3,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_rate_hz": 0.0, "tone_offset_hz": 0.0}, "sample rate must be positive"),
        ({"sample_rate_hz": 1_000.0, "tone_offset_hz": 500.0}, "inside Nyquist"),
        (
            {
                "sample_rate_hz": 1_000.0,
                "tone_offset_hz": 100.0,
                "block_duration_s": 0.0001,
            },
            "at least two samples",
        ),
        (
            {
                "sample_rate_hz": 1_000.0,
                "tone_offset_hz": 100.0,
                "minimum_block_count": 2,
            },
            "at least three",
        ),
    ],
)
def test_rejects_invalid_analysis_parameters(kwargs: dict[str, float], message: str) -> None:
    samples = np.ones(100, dtype=np.complex64)
    with pytest.raises(ValueError, match=message):
        analyze_coherent_leakage(samples, samples, **kwargs)


def test_rejects_invalid_thresholds_and_short_capture() -> None:
    samples = np.ones(100, dtype=np.complex64)
    with pytest.raises(ValueError, match="coherence.*0..1"):
        analyze_coherent_leakage(
            samples,
            samples,
            sample_rate_hz=1_000,
            tone_offset_hz=100,
            thresholds=LeakageAnalysisThresholds(minimum_rx1_coherence=1.1),
        )
    with pytest.raises(ValueError, match="at least 8 complete"):
        analyze_coherent_leakage(
            samples,
            samples,
            sample_rate_hz=1_000,
            tone_offset_hz=100,
            block_duration_s=0.02,
        )
