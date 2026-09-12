import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_full_5ms_campaign as audit  # noqa: E402


@pytest.fixture
def run(tmp_path):
    raw = []
    for name in ("rx1", "rx2"):
        path = tmp_path / name
        path.touch()
        raw.append({"path": str(path)})
    return {
        "identities": {
            "receiver_serial": audit.RECEIVER_SERIAL,
            "source_serial": audit.SOURCE_SERIAL,
        },
        "source_identity": {"hw_serial": audit.SOURCE_SERIAL},
        "configuration": {
            "name": "D",
            "frequency_hz": 5800000000,
            "sample_rate_hz": 5000000,
            "bandwidth_hz": 4000000,
            "receiver_gain_db": 50,
            "mode": "fast",
            "tx_channel": 0,
            "duration_s": 0.1,
            "frame_samples": 250000,
            "frames": 2,
        },
        "receiver_settings": {
            "sample_rate_hz": 5000000,
            "bandwidth_hz": 4000000,
            "center_frequency_hz": 5800000000,
            "gain_db": 50,
            "channels": [0, 1],
            "gain_mode": "manual",
        },
        "source_settings": {
            "tx_channel": 0,
            "tx_gain_db": [-35, -80],
            "dds_scale": [0.25, 0],
            "tx_lo_readback_hz": 5800000000,
        },
        "safety": {
            "final_source_mute": {
                "passed": True,
                "tx_gain_db": [-80, -80],
                "dds_scales": [0] * 8,
            }
        },
        "capture": {
            "samples_per_channel": 500000,
            "clipped_samples": [0, 0],
            "peak_component_counts": [100, 200],
            "raw": raw,
            "wall_time_s": 0.2,
            "timeline": [
                {
                    "buffer_sequence": i,
                    "missing_samples_before": 0,
                    "overflow_observed": False,
                    "clipped_samples": [0, 0],
                    "stream_id": 1,
                    "first_sample_sequence": 1000 + i * 250000,
                    "last_sample_sequence_exclusive": 1000 + (i + 1) * 250000,
                }
                for i in range(2)
            ],
        },
    }


def check(run):
    return audit.check_run(
        run, frequency=5800000000, configuration="D", duration=0.1, gain=50, mode="fast"
    )


def test_continuous_native_frames_pass(run):
    assert check(run)["samples_per_channel"] == 500000


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("receiver_settings", "sample_rate_hz", 2000000),
        ("receiver_settings", "gain_mode", "slow_attack"),
        ("configuration", "tx_channel", 1),
        ("capture", "samples_per_channel", 250000),
        ("capture", "clipped_samples", [0, 1]),
        ("source_settings", "tx_gain_db", [-20, -80]),
        ("source_settings", "tx_channel", 1),
        ("identities", "receiver_serial", "another-receiver"),
        ("identities", "source_serial", "another-source"),
        ("source_identity", "hw_serial", "another-source"),
    ],
)
def test_rejects_wrong_readback_or_capture(run, section, key, value):
    run[section][key] = value
    with pytest.raises(ValueError):
        check(run)


@pytest.mark.parametrize(
    "key,value",
    [
        ("buffer_sequence", 3),
        ("stream_id", 2),
        ("missing_samples_before", 1),
        ("overflow_observed", True),
        ("first_sample_sequence", 250999),
    ],
)
def test_rejects_frame_discontinuity(run, key, value):
    run["capture"]["timeline"][1][key] = value
    with pytest.raises(ValueError):
        check(run)


def test_headroom_warning_is_not_silently_removed(run):
    run["capture"]["peak_component_counts"][1] = 1878
    assert check(run)["conservative_headroom_pass"] is False


def test_cleanup_failure_rejected(run):
    run["safety"]["final_source_mute"]["passed"] = False
    with pytest.raises(ValueError, match="cleanup"):
        check(run)


@pytest.fixture
def block_and_spec():
    spec = {
        "frequency_hz": 5800000000,
        "configuration": "D",
        "gain_db": 50,
        "dwells_us": [25, 50, 100, 200, 1000],
    }
    block = {
        **spec,
        "status": "diagnostic-complete",
        "captures": audit.schedule(spec["dwells_us"], 202609081536 + 5800000000, "D"),
        "references": [
            {"position": pos, "configuration": c, "port": p}
            for pos in ("before", "after")
            for c in ("A", "D")
            for p in audit.PORTS
        ],
    }
    return block, spec


def test_exact_grid_passes(block_and_spec):
    audit.block_grid(*block_and_spec)


@pytest.mark.parametrize("collection", ["captures", "references"])
def test_missing_or_replaced_trial_rejected(block_and_spec, collection):
    block, spec = block_and_spec
    block[collection][-1] = copy.deepcopy(block[collection][0])
    with pytest.raises(ValueError, match="grid"):
        audit.block_grid(block, spec)


@pytest.mark.parametrize("failure_type,accepted", [("OSError", True), ("RuntimeError", False)])
def test_transport_ledger_never_accepts_quality_based_replacement(tmp_path, failure_type, accepted):
    attempts = []
    for index in range(2):
        path = tmp_path / f"attempt-{index}.json"
        run = {
            "status": "failed" if index == 0 else "passed",
            "error": {"type": failure_type, "errno": 61} if index == 0 else None,
            "error_traceback": "iio_metadata.py\nself._buffer.refill()",
            "safety": {
                "final_source_mute": {
                    "passed": True,
                    "dds_scales": [0.0] * 8,
                    "tx_gain_db": [-80.0, -80.0],
                }
            },
        }
        audit.save(path, run)
        attempts.append({"run_json": str(path), "sha256": audit.sha256(path)})
    evidence = tmp_path / "capture-attempts.json"
    audit.save(
        evidence,
        {
            "attempts": attempts,
            "status": "passed",
            "maximum_attempts": 3,
            "selected_run_json": attempts[-1]["run_json"],
        },
    )
    item = {
        **attempts[-1],
        "transport_attempts": {
            "path": str(evidence),
            "sha256": audit.sha256(evidence),
        },
    }
    if accepted:
        assert audit.transport_attempts(item) == 2
    else:
        with pytest.raises(ValueError, match="non-transport"):
            audit.transport_attempts(item)
