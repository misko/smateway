from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from smateway.causal_timing import FrozenTiming, bearing_study, predict_intervals, train_timing
from smateway.fast_tracking import FastTrackingProfile
from smateway.rate_timing import PORTS
from smateway.tracking.manifold import far_field_steering
from smateway.tracking.schedule import ArrayGeometry

ROOT = Path(__file__).resolve().parents[1]
PROFILE = FastTrackingProfile.load(ROOT / "profiles/tracking-c6-200us-v1/control_profile.json")


def test_prediction_uses_fractional_clock_and_only_complete_unseen_frames():
    model = FrozenTiming(2_000_000, 2_000_000, 231.2, 2989.4, 0, None, 0.2)
    left, right = predict_intervals(model, PROFILE, 3_000_000)
    assert left.min() >= model.training_samples
    assert right.max() <= 3_000_000
    assert np.all(left < right)
    assert left.shape[1] == 6
    short, _ = predict_intervals(model, PROFILE, 2_500_000)
    assert np.array_equal(short, left[: len(short)])
    with pytest.raises(ValueError, match="discard"):
        predict_intervals(model, PROFILE, 3_000_000, leading_us=200)


def test_training_cannot_observe_modified_future_samples(monkeypatch):
    seen = []

    def decoder(product, **_kwargs):
        seen.append(product.copy())
        return type("Decode", (), {"marker_end_bins": np.arange(20) * 1494.7 + 111.3})()

    monkeypatch.setattr("smateway.causal_timing.decode_fast_schedule", decoder)
    first = np.ones(100_000, dtype=np.complex64)
    second = first * 0.4j
    before = train_timing(first, second, PROFILE, fs=2_000_000, training_s=0.02)
    second[40_000:] = 1000 - 32j
    after = train_timing(first, second, PROFILE, fs=2_000_000, training_s=0.02)
    assert before == after
    assert all(len(p) == 40_000 for p in seen)
    assert np.array_equal(*seen)
    assert before.cycle_samples == pytest.approx(2989.4)


def test_perfect_bearing_reports_no_surveyed_accuracy_without_truth():
    geometry = ArrayGeometry.circular("synthetic", PORTS, radius_mm=25)
    grid = np.arange(360)
    steering = far_field_steering(geometry, 5_800_000_000, grid)
    values = np.tile(steering[42], (128, 1))
    result = bearing_study(
        values,
        cycle_ms=1.5,
        coefficients=np.ones(6),
        steering=steering,
        bearing_grid=grid,
        grouping_cycles=(1, 4, 16),
    )
    assert result[0]["model_valid_percent"] == 100
    assert result[0]["all_group_repeatability_rms_deg"] < 1e-8
    assert result[0]["surveyed_angle_error_rms_deg"] is None
    assert result[0]["repeatable_bearing_pass"]
    assert not result[-1]["repeatable_bearing_pass"]  # Too few independent groups.


def test_changed_clock_changes_predictions_not_the_frozen_model():
    original = FrozenTiming(2_000_000, 2_000_000, 0, 3000.1, 0, None, 0.2)
    drifted = replace(original, cycle_samples=3000.9)
    before, _ = predict_intervals(original, PROFILE, 3_000_000)
    after, _ = predict_intervals(drifted, PROFILE, 3_000_000)
    assert original.cycle_samples == 3000.1
    assert not np.array_equal(before, after)
