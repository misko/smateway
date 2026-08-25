import numpy as np
import pytest

from smateway.frequency_slope_localization import (
    AnchoredArrayGeometry,
    FrequencySlopeMeasurements,
    Tx2RadialPrior,
    frequency_slope_residual_diagnostics,
    infer_anchored_tx2_frequency_slope,
    predict_double_relative_phase_deg,
    wrap_phase_deg,
)

FREQUENCIES_HZ = np.asarray(
    (2.31e9, 2.47e9, 2.68e9, 2.94e9, 3.27e9, 3.69e9, 4.18e9, 4.74e9, 5.37e9, 5.8e9)
)
FIXED_TX1_MM = np.asarray((-26.5, 315.7))
TRUE_TX2_MM = np.asarray((161.2, -262.7))
ANTENNA_INTERCEPT_DEG = np.asarray((0.0, 47.0, -83.0, 129.0, -151.0, 31.0, 174.0, -66.0))


def _geometry() -> AnchoredArrayGeometry:
    return AnchoredArrayGeometry(
        antenna_positions_mm=np.asarray(
            (
                (-15.0, -62.5),
                (-30.0, -62.5),
                (-75.0, -4.5),
                (-75.0, 13.5),
                (75.0, 13.5),
                (75.0, -4.5),
                (30.0, -62.5),
                (15.0, -62.5),
            )
        ),
        center_mm=np.asarray((0.0, 0.0)),
    )


def _measurements(
    *,
    extra_antenna_intercept_deg: np.ndarray | None = None,
    sparse: bool = False,
) -> FrequencySlopeMeasurements:
    predicted = predict_double_relative_phase_deg(
        _geometry(),
        FREQUENCIES_HZ,
        fixed_tx1_position_mm=FIXED_TX1_MM,
        tx2_position_mm=TRUE_TX2_MM,
    )
    intercept = ANTENNA_INTERCEPT_DEG.copy()
    if extra_antenna_intercept_deg is not None:
        intercept += extra_antenna_intercept_deg
    phase = predicted + intercept[None, :]
    uncertainty = np.full(phase.shape, 10.0)
    valid = np.ones(phase.shape, dtype=np.bool_)
    # ANT1 is the double-relative reference.  Its zero-valued profile carries
    # no slope information and may be absent from the observations.
    valid[:, 0] = False
    if sparse:
        valid[(0, 2, 4, 6, 8), (2, 4, 6, 1, 7)] = False
    phase[~valid] = np.nan
    uncertainty[~valid] = np.nan
    return FrequencySlopeMeasurements(
        carrier_frequency_hz=FREQUENCIES_HZ,
        tx2_minus_tx1_relative_phase_deg=phase,
        phase_standard_deviation_deg=uncertainty,
        valid_mask=valid,
    )


def test_truth_profiles_fixed_antenna_intercepts_and_preserves_sparse_mask() -> None:
    measurements = _measurements(sparse=True)
    diagnostics = frequency_slope_residual_diagnostics(
        measurements,
        _geometry(),
        fixed_tx1_position_mm=FIXED_TX1_MM,
        tx2_position_mm=TRUE_TX2_MM,
    )

    np.testing.assert_array_equal(diagnostics.antenna_indices, np.arange(1, 8))
    np.testing.assert_allclose(
        wrap_phase_deg(diagnostics.nuisance_intercept_deg),
        wrap_phase_deg(ANTENNA_INTERCEPT_DEG[1:]),
        atol=1e-10,
    )
    np.testing.assert_array_equal(
        diagnostics.valid_mask,
        measurements.valid_mask[:, 1:],
    )
    np.testing.assert_array_equal(
        diagnostics.residual_phase_deg[~diagnostics.valid_mask],
        0.0,
    )
    np.testing.assert_allclose(
        diagnostics.residual_phase_deg[diagnostics.valid_mask],
        0.0,
        atol=1e-10,
    )
    assert diagnostics.overall_weighted_rms_deg < 1e-10
    assert diagnostics.maximum_absolute_residual_deg < 1e-10


def test_seeded_chunked_posterior_recovers_synthetic_tx2() -> None:
    posterior = infer_anchored_tx2_frequency_slope(
        _measurements(),
        _geometry(),
        fixed_tx1_position_mm=FIXED_TX1_MM,
        sample_count=40_000,
        seed=17,
        prior=Tx2RadialPrior(mean_mm=304.8, standard_deviation_mm=50.0),
        chunk_size=713,
    )

    assert posterior.method == "seeded-prior-frequency-slope-importance"
    assert posterior.reference_index == 0
    assert posterior.fixed_tx1_position_mm == pytest.approx(tuple(FIXED_TX1_MM))
    assert np.linalg.norm(np.asarray(posterior.tx2.map_position_mm) - TRUE_TX2_MM) < 5.0
    assert np.linalg.norm(np.asarray(posterior.tx2.mean_position_mm) - TRUE_TX2_MM) < 10.0
    assert posterior.tx2.map_radius_mm == pytest.approx(np.linalg.norm(TRUE_TX2_MM), abs=5.0)
    assert posterior.tx2.map_direction_deg == pytest.approx(-58.47, abs=1.0)
    assert posterior.tx2.direction_resultant_length > 0.99
    assert 1.0 < posterior.effective_sample_size < posterior.samples.sample_count
    assert posterior.map_residuals.overall_weighted_rms_deg < 0.2
    with pytest.raises(ValueError, match="read-only"):
        posterior.samples.weight[0] = 1.0


def test_posterior_is_invariant_to_arbitrary_per_antenna_intercepts() -> None:
    arguments = {
        "geometry": _geometry(),
        "fixed_tx1_position_mm": FIXED_TX1_MM,
        "sample_count": 20_000,
        "seed": 8677588,
        "chunk_size": 509,
    }
    baseline = infer_anchored_tx2_frequency_slope(_measurements(), **arguments)
    shifted = infer_anchored_tx2_frequency_slope(
        _measurements(
            extra_antenna_intercept_deg=np.asarray(
                (812.0, -219.0, 77.0, 358.0, -541.0, 133.0, 999.0, -305.0)
            )
        ),
        **arguments,
    )

    np.testing.assert_array_equal(
        baseline.samples.tx2_radius_mm,
        shifted.samples.tx2_radius_mm,
    )
    np.testing.assert_array_equal(
        baseline.samples.tx2_direction_deg,
        shifted.samples.tx2_direction_deg,
    )
    np.testing.assert_allclose(
        baseline.samples.log_likelihood,
        shifted.samples.log_likelihood,
        rtol=0.0,
        atol=2e-10,
    )
    np.testing.assert_allclose(baseline.samples.weight, shifted.samples.weight, atol=2e-13)
    assert baseline.tx2.map_position_mm == pytest.approx(shifted.tx2.map_position_mm)
    assert baseline.tx2.mean_position_mm == pytest.approx(shifted.tx2.mean_position_mm)
    assert baseline.effective_sample_size == pytest.approx(shifted.effective_sample_size)


def test_inputs_reject_pseudoreplicated_frequencies_and_insufficient_data() -> None:
    phase = np.zeros((3, 4))
    with pytest.raises(ValueError, match="aggregate repeated captures"):
        FrequencySlopeMeasurements(
            carrier_frequency_hz=np.asarray((2.4e9, 2.4e9, 5.8e9)),
            tx2_minus_tx1_relative_phase_deg=phase,
            phase_standard_deviation_deg=5.0,
        )
    with pytest.raises(ValueError, match="at least three frequencies"):
        FrequencySlopeMeasurements(
            carrier_frequency_hz=np.asarray((2.4e9, 5.8e9)),
            tx2_minus_tx1_relative_phase_deg=np.zeros((2, 4)),
            phase_standard_deviation_deg=5.0,
        )

    valid = np.zeros((FREQUENCIES_HZ.size, 8), dtype=np.bool_)
    valid[:, 1:3] = True
    sparse = FrequencySlopeMeasurements(
        carrier_frequency_hz=FREQUENCIES_HZ,
        tx2_minus_tx1_relative_phase_deg=np.zeros(valid.shape),
        phase_standard_deviation_deg=5.0,
        valid_mask=valid,
    )
    with pytest.raises(ValueError, match="at least three non-reference antennas"):
        infer_anchored_tx2_frequency_slope(
            sparse,
            _geometry(),
            fixed_tx1_position_mm=FIXED_TX1_MM,
            sample_count=10,
        )

    with pytest.raises(ValueError, match="antenna counts differ"):
        frequency_slope_residual_diagnostics(
            FrequencySlopeMeasurements(
                carrier_frequency_hz=FREQUENCIES_HZ,
                tx2_minus_tx1_relative_phase_deg=np.zeros((FREQUENCIES_HZ.size, 4)),
                phase_standard_deviation_deg=5.0,
            ),
            _geometry(),
            fixed_tx1_position_mm=FIXED_TX1_MM,
            tx2_position_mm=TRUE_TX2_MM,
        )
