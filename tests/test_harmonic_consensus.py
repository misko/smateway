import numpy as np
import pytest

from smateway.harmonic_consensus import consensus_mask


def test_consistent_weak_family_survives_loud_inconsistent_outliers():
    values = [2229.16, 2229.21, 2229.20, 2116.75, 2138.36, 2153.21]
    weights = [0.185, 0.019, 0.014, 0.407, 1.0, 0.189]
    np.testing.assert_array_equal(consensus_mask(values, weights), [True] * 3 + [False] * 3)


def test_no_consensus_stays_failed():
    with pytest.raises(ValueError, match="fewer than three"):
        consensus_mask([1, 2, 3, 4, 5, 6], np.ones(6))


def test_equal_disjoint_families_are_ambiguous():
    with pytest.raises(ValueError, match="equal support"):
        consensus_mask([100, 100.1, 100.2, 120, 120.1, 120.2], [1, 1, 1, 10, 10, 10])
