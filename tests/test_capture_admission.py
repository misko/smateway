import numpy as np
import pytest

from smateway.capture_admission import AdcHeadroomMonitor


def test_dual_receiver_headroom_admits_bounded_samples() -> None:
    monitor = AdcHeadroomMonitor()
    samples = np.asarray(
        [
            [100 + 200j, -300 - 400j, 500 + 600j],
            [700 - 800j, -900 + 1_000j, 1_100 - 1_200j],
        ],
        dtype=np.complex64,
    )

    monitor.observe(samples)
    result = monitor.result()

    assert result.passed
    assert [receiver.sample_count for receiver in result.receivers] == [3, 3]
    assert [receiver.peak_abs_component_counts for receiver in result.receivers] == [600, 1_200]
    assert all(receiver.clipped_sample_count == 0 for receiver in result.receivers)
    assert all(receiver.near_full_scale_sample_count == 0 for receiver in result.receivers)


def test_clipping_rejects_only_observed_receiver() -> None:
    monitor = AdcHeadroomMonitor()
    monitor.observe(
        np.asarray(
            [
                [2_047 + 0j, 10 + 20j],
                [100 + 200j, 300 + 400j],
            ],
            dtype=np.complex64,
        )
    )

    result = monitor.result()

    assert not result.passed
    assert not result.receivers[0].passed
    assert result.receivers[0].rejection_reasons == (
        "clipping_detected",
        "near_full_scale_fraction_exceeded",
    )
    assert result.receivers[1].passed


def test_near_full_scale_fraction_is_accumulated_across_blocks() -> None:
    monitor = AdcHeadroomMonitor(maximum_near_full_scale_fraction=0.25)
    monitor.observe(
        np.asarray(
            [
                [1_900 + 0j, 0j],
                [0j, 0j],
            ],
            dtype=np.complex64,
        )
    )
    monitor.observe(np.zeros((2, 2), dtype=np.complex64))

    result = monitor.result()

    assert result.receivers[0].near_full_scale_fraction == pytest.approx(0.25)
    assert result.receivers[0].passed
    assert result.receivers[1].passed

    rejected = AdcHeadroomMonitor(maximum_near_full_scale_fraction=0.24)
    rejected.observe(
        np.asarray(
            [[1_900 + 0j, 0j, 0j, 0j], [0j, 0j, 0j, 0j]],
            dtype=np.complex64,
        )
    )
    assert not rejected.result().receivers[0].passed


def test_headroom_monitor_rejects_bad_blocks_and_empty_result() -> None:
    monitor = AdcHeadroomMonitor()
    with pytest.raises(RuntimeError, match="no samples"):
        monitor.result()
    with pytest.raises(ValueError, match="shape"):
        monitor.observe(np.ones((1, 10), dtype=np.complex64))
    with pytest.raises(ValueError, match="complex"):
        monitor.observe(np.ones((2, 10), dtype=np.float32))
