import numpy as np
import pytest

from smateway.affine_transfer import affine_interval_transfer


def test_noninteger_carrier_visit_recovers_complex_gain_despite_large_offset():
    one = np.exp(2j * np.pi * 0.10077 * np.arange(100))
    truth = 0.03 - 0.02j
    two = truth * one + 2 + 3j
    h, offset, fraction = affine_interval_transfer(one, two, [5, 26, 62], [20, 45, 96])
    np.testing.assert_allclose(h, truth, atol=1e-13)
    np.testing.assert_allclose(offset, 2 + 3j, atol=1e-13)
    assert min(fraction) > 0.9


def test_constant_reference_cannot_distinguish_gain_from_offset():
    with pytest.raises(ValueError, match="constant offset"):
        affine_interval_transfer(np.ones(20), np.ones(20) * 3j, [0], [20])


def test_each_port_gets_its_own_intercept_not_a_calibration_offset():
    one = np.exp(2j * np.pi * 0.15 * np.arange(60))
    truth = np.array([0.02 + 0.03j, -0.08 + 0.04j])
    offsets = np.array([1 + 2j, -3 - 1j])
    two = np.repeat(truth, 30) * one + np.repeat(offsets, 30)
    h, b, _ = affine_interval_transfer(one, two, [0, 30], [30, 60])
    np.testing.assert_allclose(h, truth, atol=1e-13)
    np.testing.assert_allclose(b, offsets, atol=1e-13)
