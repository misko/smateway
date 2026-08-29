from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/analyze_5g8_paired_tx_ota_control.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_paired_tx_ota_control", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write_analyzable_artifact(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    artifact_id = "abcdef0123456789abcdef0123456789"
    sample_rate_hz = 1_000
    sample_count = 10_000
    tone_hz = 100.0
    monkeypatch.setattr(MODULE, "EXPECTED_SAMPLE_RATE_HZ", sample_rate_hz)
    monkeypatch.setattr(MODULE, "EXPECTED_SAMPLE_COUNT", sample_count)
    monkeypatch.setattr(MODULE, "SAMPLES_PER_BIN", 1)

    artifact_root = root / artifact_id
    artifact_root.mkdir(parents=True)
    indices = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(2j * np.pi * tone_hz * indices / sample_rate_hz)
    transfer = 0.25 * np.exp(1j * np.radians(30.0))
    raw = np.empty((sample_count, 2, 2), dtype="<i2")
    for receiver, values in enumerate((500.0 * carrier, 500.0 * transfer * carrier)):
        raw[:, receiver, 0] = np.rint(values.real).astype("<i2")
        raw[:, receiver, 1] = np.rint(values.imag).astype("<i2")
    data_path = artifact_root / f"{artifact_id}.sigmf-data"
    raw.tofile(data_path)
    raw_sha256 = hashlib.sha256(data_path.read_bytes()).hexdigest()
    first_sequence = 100
    last_sequence = first_sequence + sample_count
    metadata = {
        "annotations": [],
        "captures": [
            {
                "sample_start": 0,
                "settings": {
                    "bandwidth_hz": 800,
                    "center_frequency_hz": MODULE.EXACT_CENTER_FREQUENCY_HZ,
                    "channels": [0, 1],
                    "gain_db": 60.0,
                    "gain_mode": "manual",
                    "sample_rate_hz": sample_rate_hz,
                },
            }
        ],
        "global": {
            "core:datatype": "ci16_le",
            "core:num_channels": 2,
            "pluto:artifact_id": artifact_id,
            "pluto:created_at": "2026-08-25T00:00:00+00:00",
            "pluto:sha256": raw_sha256,
        },
        "pluto:capture": {"sample_count": sample_count, "receiver_count": 2},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "block_count": 1,
            "total_samples": sample_count,
            "sample_sequence_span": sample_count,
            "stream_id": 7,
            "first_sample_sequence": first_sequence,
            "last_sample_sequence_exclusive": last_sequence,
            "blocks": [
                {
                    "buffer_sequence": 0,
                    "sample_start": 0,
                    "sample_count": sample_count,
                    "first_sample_sequence": first_sequence,
                    "last_sample_sequence_exclusive": last_sequence,
                    "stream_id": 7,
                    "metadata_abi": 2,
                    "missing_samples_before": 0,
                }
            ],
        },
    }
    metadata_path = artifact_root / f"{artifact_id}.sigmf-meta"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    profile_path = Path(__file__).parents[1] / "profiles/fast20-v1/control_profile.json"
    profile = MODULE.load_profile(profile_path)
    capture = {
        "center_frequency_hz": MODULE.EXACT_CENTER_FREQUENCY_HZ,
        "first_sample_sequence": first_sequence,
        "last_sample_sequence_exclusive": last_sequence,
        "metadata_abi": 2,
        "profile_contract_sha256": profile.contract_sha256,
        "sample_count": sample_count,
        "sample_rate_hz": sample_rate_hz,
        "stimulus": "phase",
        "stream_id": 7,
        "tx_channel": 0,
        "tx_gain_readback_db": -12.0,
    }
    artifact = {
        "artifact_id": artifact_id,
        "center_frequency_hz": MODULE.EXACT_CENTER_FREQUENCY_HZ,
        "receiver_count": 2,
        "sample_count": sample_count,
        "sample_rate_hz": sample_rate_hz,
        "sha256": raw_sha256,
    }
    pilot = {
        "confidence": 1.0,
        "estimated_offset_hz": tone_hz,
        "phase_residual_rms_rad": 0.0,
        "phase_step_coherence": 1.0,
    }
    times_ms = np.arange(sample_count, dtype=np.float64) + 0.5
    _, complete_ids = MODULE.complete_cycle_ids(
        times_ms,
        duration_ms=float(sample_count),
        cycle_ms=386.0,
        marker_phase_ms=0.0,
    )
    dwell_path = artifact_root / "fast20-dwell-isolation.json"
    phase_path = artifact_root / "fast20-relative-phase.json"
    dwell_path.write_text(
        json.dumps({"schema": 1, "artifact": artifact, "capture": capture, "pilot": pilot}),
        encoding="utf-8",
    )
    phase_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "analysis_kind": "fast20_rx1_referenced_relative_phase",
                "artifact": artifact,
                "capture": capture,
                "pilot": pilot,
                "phase": {
                    "alignment_score": 0.95,
                    "complete_cycle_count": len(complete_ids),
                    "confidence": 0.96,
                    "continuity_verified": True,
                    "cycle_ms": 386.0,
                    "even_odd_cycle_agreement": 0.999,
                    "jackknife_stability": 0.999,
                    "marker_phase_ms": 0.0,
                },
                "quality_gate": {"passed": False},
            }
        ),
        encoding="utf-8",
    )
    return artifact_root, dwell_path, phase_path


def _manifest_attempts() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(5):
        for tx_channel in (0, 1):
            rows.append(
                {
                    "artifact_id": f"{2 * index + tx_channel:032x}",
                    "center_frequency_hz": MODULE.EXACT_CENTER_FREQUENCY_HZ,
                    "status": "complete",
                    "tx_channel": tx_channel,
                }
            )
    return rows


def test_manifest_selects_five_unique_repeats_per_tx() -> None:
    selected = MODULE._manifest_exact_5g8_attempts({"attempts": _manifest_attempts()})

    assert len(selected) == 10
    assert sum(row["tx_channel"] == 0 for row in selected) == 5
    assert sum(row["tx_channel"] == 1 for row in selected) == 5


@pytest.mark.parametrize("mutation", ["missing", "failed", "duplicate", "unbalanced"])
def test_manifest_fails_closed(mutation: str) -> None:
    rows = _manifest_attempts()
    if mutation == "missing":
        rows.pop()
    elif mutation == "failed":
        rows[0]["status"] = "failed"
    elif mutation == "duplicate":
        rows[1]["artifact_id"] = rows[0]["artifact_id"]
    else:
        rows[1]["tx_channel"] = 0

    with pytest.raises(ValueError):
        MODULE._manifest_exact_5g8_attempts({"attempts": rows})


def test_continuity_requires_gap_free_single_abi2_stream() -> None:
    metadata = {
        "pluto:capture": {"sample_count": 8},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "block_count": 2,
            "total_samples": 8,
            "sample_sequence_span": 8,
            "stream_id": 7,
            "first_sample_sequence": 100,
            "last_sample_sequence_exclusive": 108,
            "blocks": [
                {
                    "buffer_sequence": 0,
                    "sample_start": 0,
                    "sample_count": 4,
                    "first_sample_sequence": 100,
                    "last_sample_sequence_exclusive": 104,
                    "stream_id": 7,
                    "metadata_abi": 2,
                    "missing_samples_before": 0,
                },
                {
                    "buffer_sequence": 1,
                    "sample_start": 4,
                    "sample_count": 4,
                    "first_sample_sequence": 104,
                    "last_sample_sequence_exclusive": 108,
                    "stream_id": 7,
                    "metadata_abi": 2,
                    "missing_samples_before": 0,
                },
            ]
        }
    }

    result = MODULE._validate_continuity(metadata, sample_count=8)

    assert result["total_samples"] == 8
    assert result["stream_id"] == 7
    metadata["pluto:continuity"]["blocks"][1]["first_sample_sequence"] = 105
    with pytest.raises(ValueError):
        MODULE._validate_continuity(metadata, sample_count=8)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("metadata_abi", 1), ("block_count", 3)],
)
def test_continuity_rejects_noncanonical_aggregate(field: str, value: int) -> None:
    metadata = {
        "pluto:capture": {"sample_count": 1},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "block_count": 1,
            "total_samples": 1,
            "sample_sequence_span": 1,
            "stream_id": 7,
            "first_sample_sequence": 100,
            "last_sample_sequence_exclusive": 101,
            "blocks": [
                {
                    "buffer_sequence": 0,
                    "sample_start": 0,
                    "sample_count": 1,
                    "first_sample_sequence": 100,
                    "last_sample_sequence_exclusive": 101,
                    "stream_id": 7,
                    "metadata_abi": 2,
                    "missing_samples_before": 0,
                }
            ],
        },
    }
    metadata["pluto:continuity"][field] = value

    with pytest.raises(ValueError):
        MODULE._validate_continuity(metadata, sample_count=1)


def test_coherent_bins_preserve_dual_channel_transfer(tmp_path: Path) -> None:
    sample_count = 4_000
    sample_rate_hz = 1_000.0
    tone_hz = 100.0
    indices = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(2j * np.pi * tone_hz * indices / sample_rate_hz)
    rx1 = np.rint(500.0 * np.column_stack((carrier.real, carrier.imag))).astype("<i2")
    transfer = 0.25 * np.exp(1j * np.radians(30.0))
    rx2_carrier = transfer * carrier
    rx2 = np.rint(500.0 * np.column_stack((rx2_carrier.real, rx2_carrier.imag))).astype(
        "<i2"
    )
    raw = np.empty((sample_count, 2, 2), dtype="<i2")
    raw[:, 0] = rx1
    raw[:, 1] = rx2
    path = tmp_path / "capture.sigmf-data"
    raw.tofile(path)

    prior = MODULE.SAMPLES_PER_BIN
    MODULE.SAMPLES_PER_BIN = 100
    try:
        bins = MODULE._coherent_bins(
            path,
            sample_count=sample_count,
            sample_rate_hz=sample_rate_hz,
            tone_offset_hz=tone_hz,
        )
    finally:
        MODULE.SAMPLES_PER_BIN = prior

    observed = np.median(bins[1] / bins[0])
    assert abs(observed) == pytest.approx(0.25, abs=0.002)
    assert np.degrees(np.angle(observed)) == pytest.approx(30.0, abs=0.3)


@pytest.mark.parametrize("tamper", ["dwell_sha", "phase_profile", "phase_pilot"])
def test_analyze_artifact_binds_sidecars_and_admits_timing_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    artifact_root, dwell_path, phase_path = _write_analyzable_artifact(tmp_path, monkeypatch)
    profile_path = Path(__file__).parents[1] / "profiles/fast20-v1/control_profile.json"

    estimate, continuity = MODULE._analyze_artifact(
        artifact_root, expected_tx_channel=0, profile_path=profile_path
    )

    assert estimate.raw_rx2_over_rx1_amplitude == pytest.approx(0.25, abs=0.002)
    assert estimate.timing_robustness_passed is True
    assert estimate.retained_phase_quality_passed is False
    assert continuity["metadata_abi"] == 2

    target = dwell_path if tamper == "dwell_sha" else phase_path
    document = json.loads(target.read_text(encoding="utf-8"))
    if tamper == "dwell_sha":
        document["artifact"]["sha256"] = "0" * 64
    elif tamper == "phase_profile":
        document["capture"]["profile_contract_sha256"] = "0" * 64
    else:
        document["pilot"]["estimated_offset_hz"] = 101.0
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        MODULE._analyze_artifact(
            artifact_root, expected_tx_channel=0, profile_path=profile_path
        )


def test_group_summary_uses_only_requested_tx() -> None:
    estimates = []
    for tx_channel in (0, 1):
        for index in range(5):
            estimates.append(
                MODULE.AllOffControlEstimate(
                    artifact_id=f"{tx_channel * 5 + index:032x}",
                    tx_channel=tx_channel,
                    tx_name=f"TX{tx_channel + 1}",
                    created_at="2026-08-25T00:00:00Z",
                    receiver_gain_db=60.0,
                    tx_gain_db=-12.0,
                    refined_pilot_offset_hz=100_000.0,
                    cycle_ms=384.6,
                    marker_phase_ms=10.0,
                    retained_alignment_score=0.95,
                    retained_phase_confidence=0.96,
                    retained_even_odd_cycle_agreement=0.999,
                    retained_jackknife_stability=0.999,
                    retained_phase_quality_passed=index != 0,
                    timing_perturbation_ms=2.0,
                    timing_sensitivity_amplitude_span_db=0.02,
                    timing_sensitivity_phase_span_deg=0.05,
                    timing_robustness_passed=True,
                    complete_cycle_count=25,
                    all_off_bin_count=2_000,
                    rx1_amplitude_counts=100.0 + tx_channel,
                    rx2_amplitude_counts=10.0 + tx_channel,
                    raw_rx2_over_rx1_real=0.1,
                    raw_rx2_over_rx1_imag=0.0,
                    raw_rx2_over_rx1_amplitude=0.1,
                    raw_rx2_over_rx1_amplitude_db=-20.0 + tx_channel + index,
                    raw_rx2_over_rx1_phase_deg=10.0 + index,
                    cycle_phase_coherence=1.0,
                    cycle_phase_rms_deg=0.1,
                    metadata_sha256="a" * 64,
                    raw_data_sha256="b" * 64,
                    raw_data_bytes=1,
                    dwell_sidecar_sha256="c" * 64,
                    phase_sidecar_sha256="d" * 64,
                )
            )

    summary = MODULE._group_summary(estimates, tx_channel=1)

    assert summary["repeat_count"] == 5
    assert summary["raw_rx2_over_rx1_amplitude_db_median"] == -17.0
    assert summary["retained_phase_quality_pass_count"] == 4
    assert summary["timing_robustness_pass_count"] == 5
