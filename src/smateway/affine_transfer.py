"""Experimental short-visit complex regression with a nuisance DC intercept."""

import numpy as np


def affine_interval_transfer(rx1, rx2, left, right):
    one, two = np.asarray(rx1, dtype=complex), np.asarray(rx2, dtype=complex)
    a, b = np.asarray(left, dtype=int), np.asarray(right, dtype=int)
    if (
        one.ndim != 1
        or two.shape != one.shape
        or a.shape != b.shape
        or np.any(a < 0)
        or np.any(b > len(one))
        or np.any(b - a < 3)
    ):
        raise ValueError("valid paired samples and intervals of at least three samples required")

    def sums(values):
        prefix = np.concatenate(([0], np.cumsum(values)))
        return prefix[b] - prefix[a]

    count = b - a
    first, second = sums(one), sums(two)
    power, cross = sums(abs(one) ** 2), sums(two * one.conj())
    denominator = power - abs(first) ** 2 / count
    fraction = denominator / np.maximum(power, 1e-300)
    if not np.all(np.isfinite(fraction)) or np.any(fraction <= 1e-8):
        raise ValueError("reference is not distinguishable from a constant offset")
    transfer = (cross - second * first.conj() / count) / denominator
    intercept = (second - transfer * first) / count
    return transfer, intercept, fraction
