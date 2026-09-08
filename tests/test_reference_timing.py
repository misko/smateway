from pathlib import Path

import numpy as np
import pytest

from smateway import reference_timing as timing
from smateway.causal_timing import FrozenTiming
from smateway.fast_tracking import FastTrackingProfile

ROOT = Path(__file__).resolve().parents[1]


def signal(dwell=200):
    profile = FastTrackingProfile.load(
        ROOT / f"profiles/tracking-c6-{dwell}us-v1/control_profile.json"
    )
    refs = np.array([1 + 2j, -2 + 1j, 0.3 - 0.8j, 0.7 + 1.2j, -1 - 0.3j, 0.2 + 0.2j])
    fold = np.zeros(profile.cycle_us, dtype=complex)
    for i, ref in enumerate(refs):
        start = i * (dwell + 20)
        fold[start : start + dwell] = ref
    return profile, refs, fold


@pytest.mark.parametrize("dwell", [25, 50, 100, 200, 1000])
def test_known_labels_recover_slot_shift_despite_common_rotation(dwell):
    profile, refs, fold = signal(dwell)
    shift = dwell + 20
    result = timing.align_reference_fold(np.roll(fold, shift) * (0.8 + 0.6j), refs, profile)
    # Discarded edges leave a few samples of intentionally equivalent origins.
    assert abs(result["shift_bins"] - shift) <= 5
    assert result["score"] > 0.999
    assert result["unshifted_score"] < 0.8


def test_prefix_only_fold_and_training(monkeypatch):
    profile, refs, fold = signal()
    base = FrozenTiming(100, 1000, 5, 150, 0, None, 0)
    monkeypatch.setattr(timing, "train_timing", lambda *a, **k: base)
    seen = []

    def fake_fold(one, two, **kwargs):
        seen.append((one.copy(), two.copy()))
        return fold, np.ones(len(fold))

    monkeypatch.setattr(timing, "native_fold", fake_fold)
    one = np.ones(400, dtype=complex)
    two = one.copy()
    first, _ = timing.train_reference_timing(one, two, profile, refs, fs=1000)
    two[100:] = 1e9j
    second, _ = timing.train_reference_timing(one, two, profile, refs, fs=1000)
    assert first == second
    assert all(len(pair[0]) == 100 and len(pair[1]) == 100 for pair in seen)


def test_empty_reference_rejected():
    profile, _, fold = signal()
    with pytest.raises(ValueError):
        timing.align_reference_fold(fold, np.zeros(6), profile)


def test_rolling_never_scores_failed_windows_as_contiguous(monkeypatch):
    profile, refs, _ = signal()
    starts = []

    def train(one, two, profile, refs, **kwargs):
        starts.append(float(one[0]))
        raise ValueError("retained training failure")

    monkeypatch.setattr(timing, "train_reference_timing", train)
    data = np.arange(1200, dtype=float)
    rows = timing.rolling_reference_windows(data, data, profile, refs, fs=1000)
    assert len(rows) == 4
    assert starts == [0, 50, 100, 150]
    assert all(r["status"] == "analysis-failed" for r in rows)
    assert [r["prediction_start_sample"] for r in rows] == [1000, 1050, 1100, 1150]
    assert all(r["training_stop_sample"] == r["prediction_start_sample"] for r in rows)


def test_rolling_scores_only_complete_future_visits(monkeypatch):
    profile, refs, pattern = signal()
    data = np.arange(1, 1100001, dtype=float)
    two = data * np.resize(pattern, len(data))

    def train(one, _two, _profile, _refs, **kwargs):
        start = int(one[0] - 1)
        return FrozenTiming(1000000, 1000000, -(start % 1500), 1500, 0, None, 0), {}

    monkeypatch.setattr(timing, "train_reference_timing", train)
    rows = timing.rolling_reference_windows(data, two, profile, refs, fs=1000000)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "analyzed"
        measured = [v["real"] + 1j * v["imag"] for v in row["mean_transfer"]]
        np.testing.assert_allclose(measured, refs, atol=1e-12)
        assert row["observation_ms"] == 50
        assert 0 < min(row["usable_per_port_ms"]) < 50
