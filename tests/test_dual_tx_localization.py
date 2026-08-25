import numpy as np
import pytest

from smateway.dual_tx_localization import (
    CircularLikelihood,
    PairedPhaseMeasurements,
    PlanarArrayGeometry,
    infer_dual_tx_grid,
    infer_dual_tx_importance,
    phase_residual_diagnostics,
    predict_tx2_minus_tx1_phase_deg,
    wrap_phase_deg,
)


def _geometry() -> PlanarArrayGeometry:
    return PlanarArrayGeometry(
        antenna_positions_mm=np.asarray(
            (
                (-15.0, 62.5),
                (-30.0, 62.5),
                (-75.0, 4.5),
                (-75.0, -13.5),
                (75.0, -13.5),
                (75.0, 4.5),
                (30.0, 62.5),
                (15.0, 62.5),
            )
        ),
        center_mm=np.asarray((0.0, 0.0)),
    )


def _measurements(
    *,
    phase_std_deg: float = 2.0,
    extra_offsets_deg: np.ndarray | None = None,
) -> PairedPhaseMeasurements:
    frequencies = np.asarray((2.4e9, 2.4e9, 5.8e9, 5.8e9))
    predicted = predict_tx2_minus_tx1_phase_deg(
        _geometry(),
        frequencies,
        tx1_radius_mm=300.0,
        tx1_angle_deg=25.0,
        tx2_radius_mm=325.0,
        tx2_angle_deg=140.0,
    )
    offsets = np.asarray((31.0, -78.0, 125.0, -11.0))
    if extra_offsets_deg is not None:
        offsets = offsets + extra_offsets_deg
    return PairedPhaseMeasurements(
        carrier_frequency_hz=frequencies,
        tx2_minus_tx1_phase_deg=predicted + offsets[:, None],
        phase_standard_deviation_deg=phase_std_deg,
    )


def test_profiled_capture_offsets_leave_zero_residual_at_truth() -> None:
    measurements = _measurements()
    assert measurements.valid_mask is not None
    np.testing.assert_array_equal(measurements.valid_mask, np.ones((4, 8), dtype=np.bool_))

    diagnostics = phase_residual_diagnostics(
        measurements,
        _geometry(),
        tx1_radius_mm=300.0,
        tx1_angle_deg=25.0,
        tx2_radius_mm=325.0,
        tx2_angle_deg=140.0,
    )

    np.testing.assert_allclose(
        wrap_phase_deg(diagnostics.nuisance_offset_deg),
        np.asarray((31.0, -78.0, 125.0, -11.0)),
        atol=1e-10,
    )
    np.testing.assert_allclose(diagnostics.residual_phase_deg, 0.0, atol=1e-10)
    assert diagnostics.overall_weighted_rms_deg < 1e-10
    assert diagnostics.maximum_absolute_residual_deg < 1e-10
    np.testing.assert_array_equal(diagnostics.valid_mask, measurements.valid_mask)


def test_sparse_valid_mask_zeroes_invalid_likelihood_and_residual_cells() -> None:
    clean = _measurements(phase_std_deg=4.0)
    valid = np.ones((clean.capture_pair_count, clean.antenna_count), dtype=np.bool_)
    valid[(0, 1, 2, 3), (0, 3, 4, 7)] = False
    phases = clean.tx2_minus_tx1_phase_deg.copy()
    uncertainty = clean.phase_standard_deviation_deg.copy()
    phases[~valid] = np.nan
    uncertainty[~valid] = np.nan
    sparse = PairedPhaseMeasurements(
        carrier_frequency_hz=clean.carrier_frequency_hz,
        tx2_minus_tx1_phase_deg=phases,
        phase_standard_deviation_deg=uncertainty,
        valid_mask=valid,
    )
    clean_masked = PairedPhaseMeasurements(
        carrier_frequency_hz=clean.carrier_frequency_hz,
        tx2_minus_tx1_phase_deg=clean.tx2_minus_tx1_phase_deg,
        phase_standard_deviation_deg=clean.phase_standard_deviation_deg,
        valid_mask=valid,
    )
    arguments = {
        "geometry": _geometry(),
        "radius_values_mm": np.asarray((300.0, 325.0)),
        "angle_values_deg": np.arange(-180.0, 180.0, 30.0),
        "maximum_modes": 3,
    }

    sparse_posterior = infer_dual_tx_grid(sparse, **arguments)
    clean_posterior = infer_dual_tx_grid(clean_masked, **arguments)
    diagnostics = phase_residual_diagnostics(
        sparse,
        _geometry(),
        tx1_radius_mm=300.0,
        tx1_angle_deg=25.0,
        tx2_radius_mm=325.0,
        tx2_angle_deg=140.0,
    )

    np.testing.assert_array_equal(sparse.valid_mask, valid)
    np.testing.assert_allclose(
        sparse_posterior.samples.log_likelihood,
        clean_posterior.samples.log_likelihood,
    )
    np.testing.assert_allclose(sparse_posterior.samples.weight, clean_posterior.samples.weight)
    np.testing.assert_array_equal(diagnostics.valid_mask, valid)
    np.testing.assert_array_equal(diagnostics.residual_phase_deg[~valid], 0.0)
    np.testing.assert_allclose(diagnostics.residual_phase_deg[valid], 0.0, atol=1e-10)
    assert diagnostics.overall_weighted_rms_deg < 1e-10
    assert diagnostics.maximum_absolute_residual_deg < 1e-10


def test_two_frequency_grid_recovers_truth_and_retains_weighted_summaries() -> None:
    posterior = infer_dual_tx_grid(
        _measurements(),
        _geometry(),
        radius_values_mm=np.asarray((275.0, 300.0, 325.0)),
        angle_values_deg=np.arange(-180.0, 180.0, 5.0),
        maximum_grid_points=100_000,
        maximum_modes=4,
    )

    map_index = int(np.argmax(posterior.samples.log_posterior_density))
    assert posterior.method == "deterministic-polar-grid"
    assert posterior.samples.tx1_radius_mm[map_index] == pytest.approx(300.0)
    assert posterior.samples.tx1_angle_deg[map_index] == pytest.approx(25.0)
    assert posterior.samples.tx2_radius_mm[map_index] == pytest.approx(325.0)
    assert posterior.samples.tx2_angle_deg[map_index] == pytest.approx(140.0)
    assert posterior.map_residuals.overall_weighted_rms_deg < 1e-10
    assert posterior.modes[0].map_tx1_angle_deg == pytest.approx(25.0)
    assert posterior.modes[0].map_tx2_angle_deg == pytest.approx(140.0)
    assert sum(mode.probability_mass for mode in posterior.modes) == pytest.approx(1.0)
    assert tuple(region.probability for region in posterior.credible_regions) == (
        0.5,
        0.9,
        0.95,
    )
    assert all(
        region.achieved_probability + 1e-12 >= region.probability
        for region in posterior.credible_regions
    )
    assert posterior.tx1.radius_interval_95_mm[0] <= posterior.tx1.median_radius_mm
    assert posterior.tx1.radius_interval_95_mm[1] >= posterior.tx1.median_radius_mm


def test_grid_posterior_is_invariant_to_arbitrary_capture_pair_phase_offsets() -> None:
    arguments = {
        "geometry": _geometry(),
        "radius_values_mm": np.asarray((280.0, 300.0, 325.0)),
        "angle_values_deg": np.arange(-180.0, 180.0, 10.0),
        "maximum_grid_points": 20_000,
        "maximum_modes": 3,
    }
    baseline = infer_dual_tx_grid(_measurements(phase_std_deg=8.0), **arguments)
    shifted = infer_dual_tx_grid(
        _measurements(
            phase_std_deg=8.0,
            extra_offsets_deg=np.asarray((143.0, -219.0, 77.0, 358.0)),
        ),
        **arguments,
    )

    np.testing.assert_allclose(baseline.samples.log_likelihood, shifted.samples.log_likelihood)
    np.testing.assert_allclose(baseline.samples.weight, shifted.samples.weight)
    assert baseline.effective_sample_size == pytest.approx(shifted.effective_sample_size)


def test_seeded_importance_inference_is_reproducible_and_keeps_particles() -> None:
    measurements = _measurements(phase_std_deg=25.0)
    likelihood = CircularLikelihood(systematic_phase_std_deg=20.0)
    first = infer_dual_tx_importance(
        measurements,
        _geometry(),
        sample_count=4_000,
        seed=8677588,
        likelihood=likelihood,
        maximum_modes=4,
    )
    second = infer_dual_tx_importance(
        measurements,
        _geometry(),
        sample_count=4_000,
        seed=8677588,
        likelihood=likelihood,
        maximum_modes=4,
    )

    assert first.method == "seeded-prior-importance"
    assert first.samples.sample_count == 4_000
    np.testing.assert_array_equal(first.samples.tx1_radius_mm, second.samples.tx1_radius_mm)
    np.testing.assert_array_equal(first.samples.tx1_angle_deg, second.samples.tx1_angle_deg)
    np.testing.assert_array_equal(first.samples.tx2_radius_mm, second.samples.tx2_radius_mm)
    np.testing.assert_array_equal(first.samples.tx2_angle_deg, second.samples.tx2_angle_deg)
    np.testing.assert_array_equal(first.samples.weight, second.samples.weight)
    assert 1.0 <= first.effective_sample_size <= first.samples.sample_count
    assert len(first.modes) >= 1
    assert sum(mode.probability_mass for mode in first.modes) == pytest.approx(1.0)


def test_ambiguous_single_frequency_result_retains_separated_modes() -> None:
    frequency = np.asarray((2.4e9,))
    predicted = predict_tx2_minus_tx1_phase_deg(
        _geometry(),
        frequency,
        tx1_radius_mm=300.0,
        tx1_angle_deg=25.0,
        tx2_radius_mm=300.0,
        tx2_angle_deg=140.0,
    )
    measurements = PairedPhaseMeasurements(
        carrier_frequency_hz=frequency,
        tx2_minus_tx1_phase_deg=predicted + 33.0,
        phase_standard_deviation_deg=30.0,
    )

    posterior = infer_dual_tx_grid(
        measurements,
        _geometry(),
        radius_values_mm=np.asarray((300.0,)),
        angle_values_deg=np.arange(-180.0, 180.0, 10.0),
        maximum_modes=4,
        mode_separation_mm=100.0,
    )

    assert len(posterior.modes) >= 2
    assert posterior.modes[0].probability_mass < 0.9
    assert sum(mode.probability_mass for mode in posterior.modes) == pytest.approx(1.0)


def test_inputs_reject_mismatched_or_degenerate_geometry() -> None:
    with pytest.raises(ValueError, match="collinear"):
        PlanarArrayGeometry(
            antenna_positions_mm=np.asarray(((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))),
            center_mm=np.asarray((0.0, 0.0)),
        )
    with pytest.raises(ValueError, match="broadcast-compatible"):
        PairedPhaseMeasurements(
            carrier_frequency_hz=np.asarray((2.4e9, 5.8e9)),
            tx2_minus_tx1_phase_deg=np.zeros((2, 8)),
            phase_standard_deviation_deg=np.ones((3, 2)),
        )
    with pytest.raises(ValueError, match="at least three valid"):
        PairedPhaseMeasurements(
            carrier_frequency_hz=np.asarray((2.4e9, 5.8e9)),
            tx2_minus_tx1_phase_deg=np.zeros((2, 8)),
            phase_standard_deviation_deg=1.0,
            valid_mask=np.asarray(
                (
                    (True, True, False, False, False, False, False, False),
                    (True, True, True, False, False, False, False, False),
                )
            ),
        )
