"""Offline checks for tutorial estimators, evidence and guide assets."""

import copy
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from c6_tracking_example import (  # noqa: E402
    PORTS,
    PROFILE,
    ROOT,
    simulate,
    transfer,
    validate_timeline,
)

from smateway.rate_timing import complex_value, sha256  # noqa: E402

OUT = ROOT / "docs/c6_dual_band_tracking_guide"


def test_perfect_transfer_cancels_arbitrary_common_signal_phase():
    rng = np.random.default_rng(53)
    x = rng.normal(size=128) + 1j * rng.normal(size=128)
    h = 1.7 * np.exp(1j * 0.83)
    assert transfer(x, h * x) == pytest.approx(h, abs=1e-14)
    assert transfer(x, h * x, np.linspace(0.1, 2, len(x))) == pytest.approx(h, abs=1e-14)


@pytest.mark.parametrize(
    "one,two,w",
    [
        ([], [], None),
        ([0, 0], [1, 1], None),
        ([1], [1, 2], None),
        ([np.nan], [1], None),
        ([1], [np.inf], None),
        ([1], [1], [-1]),
        ([1], [1], [np.nan]),
        ([1], [1], [0]),
    ],
)
def test_bad_samples_and_absent_reference_are_rejected(one, two, w):
    with pytest.raises(ValueError):
        transfer(one, two, w)


@pytest.mark.parametrize(
    "frequency,fs",
    [
        (2475e6, 2_000_000),
        (2475e6, 5_000_000),
        (5800e6, 2_000_000),
        (5800e6, 5_000_000),
    ],
)
def test_noiseless_demo_applies_lut_once_and_preserves_qualification_boundary(frequency, fs):
    result = simulate(frequency, fs=fs, cycles=3, noise_rms=0)
    expected = np.array([complex_value(v) for v in result["expected_corrected_transfer"]])
    actual = np.array([complex_value(v) for v in result["calibrated_transfer"]])
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    assert result["bearing_deg"] == 65
    assert result["legacy_gate_valid"] == (frequency == 5800e6)
    assert result["production_valid"] is False
    assert result["rx_lo_hz"] + result["tone_offset_hz"] == frequency
    assert result["duration_s"] == pytest.approx(0.0045)
    assert "no timing recovery tested" in result["timing_source"]


def timeline_example():
    return {
        "status": "passed",
        "configuration": {"frames": 2, "frame_samples": 10},
        "capture": {
            "samples_per_channel": 20,
            "clipped_samples": [0, 0],
            "timeline": [
                {
                    "buffer_sequence": i,
                    "first_sample_sequence": 100 + i * 10,
                    "last_sample_sequence_exclusive": 110 + i * 10,
                    "stream_id": 71,
                    "missing_samples_before": 0,
                    "overflow_observed": False,
                    "clipped_samples": [0, 0],
                }
                for i in range(2)
            ],
        },
    }


def test_continuity_accepts_arbitrary_nonzero_first_sample_counter():
    validate_timeline(timeline_example())


@pytest.mark.parametrize(
    "key,value",
    [
        ("buffer_sequence", 7),
        ("first_sample_sequence", 111),
        ("stream_id", 99),
        ("last_sample_sequence_exclusive", 121),
        ("missing_samples_before", 1),
        ("overflow_observed", True),
        ("clipped_samples", [0, 1]),
    ],
)
def test_continuity_rejects_gap_clip_or_wrong_stream(key, value):
    run = copy.deepcopy(timeline_example())
    run["capture"]["timeline"][1][key] = value
    with pytest.raises(ValueError):
        validate_timeline(run)


def test_firmware_verification_binds_correct_profile_and_binary():
    d = json.loads((OUT / "data/firmware-200us-verification.json").read_text())
    profile = json.loads(PROFILE.read_text())
    assert d["status"] == "passed"
    assert d["cycle_us"] == 1500 and d["dwell_us"] == 200 and d["guard_us"] == 20
    assert d["ports"] == list(PORTS)
    assert d["binary"]["size_bytes"] == 1156
    assert (
        d["binary"]["sha256"] == "505ddd97abee775f65dd5766a3c124787a26d379122a22740df9a468b6a1ddbb"
    )
    assert sha256(PROFILE) == d["profile"]["sha256"]
    assert profile["release_contract"]["conformant"] is False
    assert profile["frame"]["marker"]["observable_nominal_us"] == 200
    assert [int(r["gpio_code_pa3_pa0"], 2) for r in profile["states"]] == [0, 4, 6, 7, 3, 1]
    for path, digest in d["source_sha256"].items():
        assert sha256(ROOT / path) == digest
    # Build artifacts are local and may be absent in a fresh clone; verify if retained.
    for kind in ("elf", "binary"):
        p = Path(d[kind]["path"])
        if p.exists():
            assert sha256(p) == d[kind]["sha256"]


def test_frozen_real_replay_keeps_all_windows_and_rejections():
    d = json.loads((OUT / "data/replay-5800-B-200us.json").read_text())
    assert d["expected_windows"] == len(d["windows"]) == 60
    assert sum(r["status"] == "analyzed" for r in d["windows"]) == 60
    assert sum(r["direction"]["legacy_gate_valid"] for r in d["windows"]) == 0
    assert d["production_valid"] is d["surveyed_truth_available"] is False
    for r in d["windows"]:
        assert r["training_stop_sample"] == r["prediction_start_sample"]
        assert r["prediction_stop_sample"] - r["prediction_start_sample"] == 250_000
        assert r["direction"]["production_valid"] is False
    assert np.median(
        [r["direction"]["spatial_phase_residual_deg"] for r in d["windows"]]
    ) == pytest.approx(42.38, abs=0.01)
    for path, digest in d["sources"].items():
        assert sha256(ROOT / path) == digest


def test_manifest_links_and_all_ten_pngs():
    manifest = json.loads((OUT / "data/manifest.json").read_text())
    assert manifest["production_qualified"] is False
    assert len(manifest["figures"]) == 10
    assert sha256(OUT / "README.md") == manifest["report_sha256"]
    for path, digest in manifest["sources"].items():
        assert sha256(ROOT / path) == digest
    for group in ("data", "figures"):
        for path, digest in manifest[group].items():
            assert sha256(OUT / path) == digest
    for path in manifest["figures"]:
        with Image.open(OUT / path) as image:
            assert image.format == "PNG" and image.width > 2000
            image.verify()
    report = (OUT / "README.md").read_text()
    embedded = re.findall(r"!\[[^\]]*\]\((png/[^)]+)\)", report)
    assert len(embedded) == len(set(embedded)) == 10
    for target in re.findall(r"\]\(([^)]+)\)", report):
        if not target.startswith(("https://", "#")):
            assert (OUT / target).is_file(), target
