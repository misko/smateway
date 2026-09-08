import json
from types import SimpleNamespace

import numpy as np
import pytest

from smateway.rate_timing import (
    CONFIGURATIONS,
    PORTS,
    analyze_rate_capture,
    closure,
    coarse_product,
    complex_json,
    interval_moments,
    reference_summary,
    sha256,
    validate_block,
)


@pytest.mark.parametrize(
    "name,frame,total",
    [("A", 100_000, 8_000_000), ("B", 250_000, 20_000_000), ("C", 250_000, 40_000_000)],
)
def test_equal_wall_observation_duration(name, frame, total):
    cfg = CONFIGURATIONS[name]
    assert cfg.frame_plan(4) == (frame, total // frame)
    assert cfg.samples(4) == total
    assert cfg.frame_plan(30)[1] == round(30 * cfg.sample_rate_hz / frame)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), 0.031])
def test_bad_capture_duration_rejected(duration):
    with pytest.raises(ValueError):
        CONFIGURATIONS["A"].frame_plan(duration)


def test_sample_counter_gap_rejected_at_high_rate():
    def block(sequence, first, last):
        return SimpleNamespace(
            metadata_abi=2,
            samples=np.zeros((2, 500_000)),
            missing_samples_before=0,
            overflow_observed=False,
            buffer_sequence=sequence,
            first_sample_sequence=first,
            last_sample_sequence_exclusive=last,
            stream_id=12,
        )

    first = block(0, 100, 500_100)
    validate_block(first, None, 500_000)
    validate_block(block(1, 500_100, 1_000_100), first, 500_000)
    with pytest.raises(ValueError, match="discontinuous"):
        validate_block(block(1, 500_101, 1_000_101), first, 500_000)
    with pytest.raises(ValueError, match="counter span"):
        validate_block(block(0, 100, 500_101), None, 500_000)


def test_static_reference_recovers_known_transfer():
    x = np.exp(2j * np.pi * 0.15 * np.arange(100_000)) * 100
    h = 0.7 * np.exp(0.4j)
    result = reference_summary(x, h * x, 2_000_000)
    value = complex(**result["transfer"])
    assert value == pytest.approx(h)
    assert result["phase_rms_10ms_deg"] < 1e-10


def test_common_rotation_allowed_but_wrong_port_phase_fails():
    refs = np.exp(1j * np.arange(6))
    weights = np.ones(6)
    mask = np.ones(6, dtype=bool)
    assert closure(refs * np.exp(0.7j), refs, weights, mask)["passed"]
    wrong = refs * np.exp(0.7j)
    wrong[3] *= np.exp(0.4j)
    assert not closure(wrong, refs, weights, mask)["passed"]


def test_stable_but_wrong_gain_fails_independent_closure():
    result = closure(np.full(6, 2 + 0j), np.ones(6), np.ones(6), np.ones(6, dtype=bool))
    assert result["weighted_phase_bias_deg"] == 0
    assert not result["passed"]


def test_chunked_interval_sums_match_direct_computation():
    rng = np.random.default_rng(5)
    one = rng.normal(size=500_030) + 1j * rng.normal(size=500_030)
    two = 0.2j * one + rng.normal(size=one.size)
    left = np.array([0, 17, 249_997, 250_000, 499_900])
    right = np.array([5, 250_001, 250_009, 500_001, 500_030])
    sums, powers = interval_moments(one, two, left, right, fs=10_000_000)
    for i, (a, b) in enumerate(zip(left, right, strict=True)):
        assert sums[i] == pytest.approx(np.vdot(one[a:b], two[a:b]), abs=1e-8)
        assert powers[i] == pytest.approx(np.sum(np.abs(one[a:b]) ** 2), rel=1e-12, abs=1e-8)


def test_downsample_cross_product_before_coarsening_preserves_pilot():
    one = np.exp(2j * np.pi * 0.2 * np.arange(100))
    result = coarse_product(one, 0.7j * one, 5_000_000)
    assert np.allclose(result, 0.7j)


@pytest.mark.parametrize("name,dwell", [("A", 25), ("B", 50), ("C", 100)])
def test_rate_analysis_closes_against_independent_reference(tmp_path, name, dwell):
    cfg = CONFIGURATIONS[name]
    fs = cfg.sample_rate_hz
    n = round(fs * 0.12)
    time_us = np.arange(n) / fs * 1e6 + 113
    cycle = 180 + 6 * (20 + dwell)
    phase = np.mod(time_us, cycle)
    gains = np.array(
        [
            0.8 * np.exp(0.2j),
            0.9 * np.exp(-0.7j),
            0.6 * np.exp(1.1j),
            1.05 * np.exp(-1.9j),
            0.82 * np.exp(2.4j),
            0.54 * np.exp(-2.8j),
        ]
    )
    gain = np.full(n, 0.02 + 0j)
    for i, g in enumerate(gains):
        start = 200 + i * (dwell + 20)
        gain[(phase >= start) & (phase < start + dwell)] = g
    one = (100 * np.exp(2j * np.pi * 100_000 * np.arange(n) / fs)).astype(np.complex64)
    two = (one * gain).astype(np.complex64)
    raw = []
    for label, values in (("rx1", one), ("rx2", two)):
        p = tmp_path / f"{label}.cf32"
        values.tofile(p)
        raw.append({"path": str(p), "sha256": sha256(p)})
    record = {
        "status": "passed",
        "configuration": {
            "name": name,
            "mode": "fast",
            "tx_channel": 0,
            "sample_rate_hz": fs,
            "dwell_us": dwell,
            "frequency_hz": 5_800_000_000,
        },
        "capture": {"samples_per_channel": n, "raw": raw},
    }
    p = tmp_path / "run.json"
    p.write_text(json.dumps(record))
    refs, weight_refs = {}, {}
    for i, port in enumerate(PORTS):
        path = tmp_path / f"{port}.json"
        path.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "configuration": {
                        "mode": "static",
                        "name": name,
                        "frequency_hz": 5_800_000_000,
                        "port": port,
                    },
                    "reference": {"transfer": complex_json(gains[i])},
                }
            )
        )
        refs[port] = path
        weight_document = json.loads(path.read_text())
        weight_document["configuration"]["name"] = "A"
        weight_path = tmp_path / f"weight-{port}.json"
        weight_path.write_text(json.dumps(weight_document))
        weight_refs[port] = weight_path
    result = analyze_rate_capture(p, refs, weight_refs)
    variant = next(
        v for v in result["variants"] if v["method"] == "legacy" and v["leading_discard_us"] == 5
    )
    assert variant["metrics"]["closure"]["weighted_phase_bias_deg"] < 0.1
    assert variant["metrics"]["passed"]
    weight_document = json.loads(weight_refs[PORTS[0]].read_text())
    weight_document["configuration"]["frequency_hz"] += 1_000_000
    weight_refs[PORTS[0]].write_text(json.dumps(weight_document))
    with pytest.raises(ValueError, match="weight reference"):
        analyze_rate_capture(p, refs, weight_refs)
    weight_document["configuration"]["frequency_hz"] -= 1_000_000
    weight_refs[PORTS[0]].write_text(json.dumps(weight_document))
    # Refusing a modified reference identity prevents wrong-frequency calibration reuse.
    wrong = json.loads(refs[PORTS[0]].read_text())
    wrong["configuration"]["frequency_hz"] += 1_000_000
    refs[PORTS[0]].write_text(json.dumps(wrong))
    with pytest.raises(ValueError, match="mismatch"):
        analyze_rate_capture(p, refs, weight_refs)
