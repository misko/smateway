from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from smateway.hexcal import (
    FAILURE_METADATA_FLAGS,
    REQUIRED_METADATA_FLAGS,
    HexcalAnalysisError,
    analyze_hexcal_samples,
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    correction_coefficients,
    evaluate_hexcal_quality,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    six_point_dft,
    validate_tx1_rf_readback_evidence,
    wrapped_phase_deg,
)

PROFILE_PATH = Path("profiles/hexcal-v1/control_profile.json")
SAMPLE_RATE_HZ = 1_000_000
TRUE_GAINS_DB = np.asarray((0.0, 1.0, -1.0, 0.5, -0.5, 0.2))
TRUE_PHASES_DEG = np.asarray((0.0, 25.0, -40.0, 80.0, -100.0, 150.0))


def _synthetic_capture(
    *,
    sample_count: int = 1_000_000,
    start_offset_samples: int = 317,
    residual_hz: float = 3.0,
    noise_sigma: float = 0.0015,
    null_noise_sigma: float = 0.0,
    leakage_start: complex = 0.012 * np.exp(0.4j),
    leakage_drift_per_second: complex = 0.0j,
    gains_db: np.ndarray = TRUE_GAINS_DB,
    phases_deg: np.ndarray = TRUE_PHASES_DEG,
    seed: int = 4,
) -> np.ndarray:
    samples = np.arange(sample_count, dtype=np.int64)
    phase_us = (samples + start_offset_samples) % 1500
    gains = 10.0 ** (gains_db / 20.0) * np.exp(1j * np.deg2rad(phases_deg))
    time_s = samples / SAMPLE_RATE_HZ
    envelope = leakage_start + leakage_drift_per_second * time_s
    selected = np.zeros(sample_count, dtype=bool)
    for index, value in enumerate(gains):
        active_start = 200 + index * 220
        mask = (phase_us >= active_start) & (phase_us < active_start + 200)
        selected |= mask
        envelope[mask] += value
    carrier = np.exp(2j * np.pi * (100_000.0 + residual_hz) / SAMPLE_RATE_HZ * samples)
    rng = np.random.default_rng(seed)
    noise = noise_sigma * (rng.normal(size=sample_count) + 1j * rng.normal(size=sample_count))
    captured = envelope * carrier + noise
    if null_noise_sigma > 0.0:
        null_noise = null_noise_sigma * (
            rng.normal(size=sample_count) + 1j * rng.normal(size=sample_count)
        )
        null_noise[selected] = 0.0
        captured += null_noise * carrier
    return captured


def _metadata(*, flags: int = REQUIRED_METADATA_FLAGS) -> dict[str, Any]:
    blocks = []
    first_sample = 9_000_000
    for index in range(10):
        sample_start = index * 100_000
        blocks.append(
            {
                "sample_start": sample_start,
                "sample_count": 100_000,
                "utc_ns": 1_000_000_000 + index * 100_000_000,
                "metadata_abi": 2,
                "stream_id": 1234,
                "buffer_sequence": index,
                "first_sample_sequence": first_sample + sample_start,
                "last_sample_sequence_exclusive": first_sample + sample_start + 100_000,
                "metadata_flags": flags,
                "missing_samples_before": 0,
                "sample_time_realtime_start_ns": 2_000_000_000 + index * 100_000_000,
                "sample_time_realtime_end_ns": 2_100_000_000 + index * 100_000_000,
                "sample_time_monotonic_start_ns": 3_000_000_000 + index * 100_000_000,
                "sample_time_monotonic_end_ns": 3_100_000_000 + index * 100_000_000,
                "sample_time_uncertainty_ns": 50_000,
            }
        )
    return {
        "pluto:capture": {"sample_count": 1_000_000, "receiver_count": 2},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "stream_id": 1234,
            "block_count": 10,
            "total_samples": 1_000_000,
            "first_sample_sequence": first_sample,
            "last_sample_sequence_exclusive": first_sample + 1_000_000,
            "sample_sequence_span": 1_000_000,
            "blocks": blocks,
        },
    }


@pytest.fixture(scope="module")
def synthetic_analysis() -> dict[str, Any]:
    profile = load_hexcal_profile(PROFILE_PATH)
    return analyze_hexcal_samples(
        _synthetic_capture(),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )


def test_profile_is_exact_microsecond_clockwise_contract() -> None:
    profile = load_hexcal_profile(PROFILE_PATH)

    assert profile.state_names == tuple(f"ANT{index}" for index in range(1, 7))
    assert profile.order_direction == "clockwise"
    assert profile.forward_reference == "ANT1"
    assert profile.marker_body_us == 180
    assert profile.marker_observable_us == 200
    assert profile.guard_us == 20
    assert profile.cycle_us == 1500
    assert profile.file_sha256
    assert profile.contract_sha256


def test_profile_rejects_gpio_or_all_off_map_drift(tmp_path: Path) -> None:
    document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    document["states"][1]["gpio_code_pa3_pa0"] = "1111"
    changed = tmp_path / "changed-gpio.json"
    changed.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen ANT1..ANT6 code map"):
        load_hexcal_profile(changed)

    document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    document["safety"]["all_off_code"] = "1111"
    changed.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="ALL_OFF GPIO code"):
        load_hexcal_profile(changed)


def test_decoder_recovers_timing_gain_phase_and_residual_tone(
    synthetic_analysis: dict[str, Any],
) -> None:
    analysis = synthetic_analysis

    assert analysis["continuity_verified"] is True
    assert analysis["valid_cycle_count"] >= 660
    assert analysis["decoded_cycle_fraction"] >= 0.99
    assert analysis["alignment"]["contrast_db"] > 30.0
    assert analysis["timing"]["cycle_us"]["median"] == pytest.approx(1500.0, abs=5.0)
    assert analysis["timing"]["marker_observable_us"]["median"] == pytest.approx(200.0, abs=10.0)
    assert analysis["residual_common_tone_offset_hz"] == pytest.approx(3.0, abs=0.5)
    states = analysis["states"]
    expected_gain = TRUE_GAINS_DB - np.mean(TRUE_GAINS_DB)
    phase_center = math.degrees(
        math.atan2(
            float(np.mean(np.sin(np.deg2rad(TRUE_PHASES_DEG)))),
            float(np.mean(np.cos(np.deg2rad(TRUE_PHASES_DEG)))),
        )
    )
    for index, state in enumerate(states):
        assert state["name"] == f"ANT{index + 1}"
        assert state["relative_gain_db"] == pytest.approx(expected_gain[index], abs=0.25)
        assert state["normalized_gain_db"] == pytest.approx(expected_gain[index], abs=0.25)
        assert wrapped_phase_deg(
            state["phase_circular_centered_deg"]
            - wrapped_phase_deg(float(TRUE_PHASES_DEG[index]) - phase_center)
        ) == pytest.approx(0.0, abs=2.0)
        assert wrapped_phase_deg(
            state["phase_relative_to_ant1_deg"] - TRUE_PHASES_DEG[index]
        ) == pytest.approx(0.0, abs=2.0)
        assert state["cycle_coherence"] > 0.995
        assert state["null_isolation_db"] > 30.0
    assert analysis["phase_reference"].startswith("six-element circular phase centre")
    assert analysis["normalization_gauge"]["phase_reference_element"] == "none"
    assert analysis["normalization_gauge"]["phase_gauge_resultant"]["minimum"] > 0.25
    assert len(analysis["common_tone_fit"]["per_state_residual_hz"]) == 6


def test_quality_gate_accepts_good_capture_and_keeps_rejection_diagnostics(
    synthetic_analysis: dict[str, Any],
) -> None:
    accepted = evaluate_hexcal_quality(synthetic_analysis, headroom_passed=True)
    corrupted = deepcopy(synthetic_analysis)
    corrupted["states"][2]["cycle_coherence"] = 0.2
    corrupted["states"][2]["cycle_phase_std_deg"] = 80.0
    corrupted["alignment"]["contrast_db"] = 5.0
    rejected = evaluate_hexcal_quality(corrupted, headroom_passed=False)

    assert accepted["passed"] is True
    assert rejected["passed"] is False
    assert "adc_headroom_admission_failed" in rejected["global_rejection_reasons"]
    assert "marker_contrast_below_minimum" in rejected["global_rejection_reasons"]
    assert rejected["states"][2]["passed"] is False
    assert "cycle_coherence_below_minimum" in rejected["states"][2]["rejection_reasons"]

    ill_conditioned = deepcopy(synthetic_analysis)
    ill_conditioned["normalization_gauge"]["phase_gauge_resultant"]["minimum"] = 0.24
    gauge_rejected = evaluate_hexcal_quality(ill_conditioned, headroom_passed=True)
    assert gauge_rejected["passed"] is False
    assert "circular_phase_gauge_ill_conditioned" in gauge_rejected["global_rejection_reasons"]


def test_analyzer_is_invariant_to_one_global_capture_phase_rotation() -> None:
    profile = load_hexcal_profile(PROFILE_PATH)
    samples = _synthetic_capture(sample_count=300_000)
    baseline = analyze_hexcal_samples(
        samples,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )
    rotation = np.exp(1j * np.deg2rad(73.0))
    rotated = analyze_hexcal_samples(
        samples * rotation,
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )

    for baseline_state, rotated_state in zip(baseline["states"], rotated["states"], strict=True):
        assert rotated_state["normalized_gain_db"] == pytest.approx(
            baseline_state["normalized_gain_db"], abs=1e-10
        )
        assert wrapped_phase_deg(
            rotated_state["phase_circular_centered_deg"]
            - baseline_state["phase_circular_centered_deg"]
        ) == pytest.approx(0.0, abs=1e-10)
        assert rotated_state["cycle_coherence"] == pytest.approx(
            baseline_state["cycle_coherence"], abs=1e-12
        )


def test_isolated_ant1_phase_noise_is_not_used_as_every_state_reference() -> None:
    sample_count = 300_000
    indices = np.arange(sample_count, dtype=np.int64)
    phase_us = (indices + 317) % 1500
    cycle_index = (indices + 317) // 1500
    rng = np.random.default_rng(991)
    cycle_rotations = rng.normal(0.0, 18.0, int(cycle_index[-1]) + 1)

    def noisy_analysis(
        gains_db: np.ndarray, phases_deg: np.ndarray, noisy_state: int
    ) -> dict[str, Any]:
        samples = _synthetic_capture(
            sample_count=sample_count,
            noise_sigma=0.0005,
            gains_db=gains_db,
            phases_deg=phases_deg,
        )
        active_start = 200 + noisy_state * 220
        active = (phase_us >= active_start) & (phase_us < active_start + 200)
        samples[active] *= np.exp(1j * np.deg2rad(cycle_rotations[cycle_index[active]]))
        return analyze_hexcal_samples(
            samples,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=100_000.0,
            profile=load_hexcal_profile(PROFILE_PATH),
            continuity_verified=True,
        )

    analysis = noisy_analysis(TRUE_GAINS_DB, TRUE_PHASES_DEG, 0)
    rotated = noisy_analysis(np.roll(TRUE_GAINS_DB, 1), np.roll(TRUE_PHASES_DEG, 1), 1)
    phase_stds = np.asarray([float(state["cycle_phase_std_deg"]) for state in analysis["states"]])
    rotated_phase_stds = np.asarray(
        [float(state["cycle_phase_std_deg"]) for state in rotated["states"]]
    )

    assert np.all(np.isfinite(phase_stds))
    assert np.all(phase_stds > 1.0)
    assert analysis["states"][0]["cycle_coherence"] < 0.995
    assert all(state["cycle_coherence"] < 1.0 for state in analysis["states"])
    assert rotated_phase_stds == pytest.approx(np.roll(phase_stds, 1), abs=0.2)


def test_ill_conditioned_circular_gauge_is_explicitly_rejected() -> None:
    analysis = analyze_hexcal_samples(
        _synthetic_capture(sample_count=300_000),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )
    analysis["normalization_gauge"]["phase_gauge_resultant"]["minimum"] = 0.249

    quality = evaluate_hexcal_quality(
        analysis,
        headroom_passed=True,
        minimum_complete_cycles=150,
    )

    assert quality["passed"] is False
    assert "circular_phase_gauge_ill_conditioned" in quality["global_rejection_reasons"]


def test_cyclic_element_permutation_preserves_symmetric_manifold_metrics() -> None:
    profile = load_hexcal_profile(PROFILE_PATH)
    baseline = analyze_hexcal_samples(
        _synthetic_capture(sample_count=300_000),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )
    rotated = analyze_hexcal_samples(
        _synthetic_capture(
            sample_count=300_000,
            gains_db=np.roll(TRUE_GAINS_DB, 1),
            phases_deg=np.roll(TRUE_PHASES_DEG, 1),
        ),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )
    baseline_gains = np.asarray([state["normalized_gain_db"] for state in baseline["states"]])
    rotated_gains = np.asarray([state["normalized_gain_db"] for state in rotated["states"]])
    baseline_phases = np.asarray(
        [state["phase_circular_centered_deg"] for state in baseline["states"]]
    )
    rotated_phases = np.asarray(
        [state["phase_circular_centered_deg"] for state in rotated["states"]]
    )
    baseline_mode_amplitudes = np.asarray(
        [mode["amplitude"] for mode in baseline["six_point_dft"]["modes"]]
    )
    rotated_mode_amplitudes = np.asarray(
        [mode["amplitude"] for mode in rotated["six_point_dft"]["modes"]]
    )

    assert rotated_gains == pytest.approx(np.roll(baseline_gains, 1), abs=0.05)
    assert [
        wrapped_phase_deg(float(observed - expected))
        for observed, expected in zip(rotated_phases, np.roll(baseline_phases, 1), strict=True)
    ] == pytest.approx([0.0] * 6, abs=0.1)
    assert rotated_mode_amplitudes == pytest.approx(baseline_mode_amplitudes, abs=0.01)


def test_decoder_rejects_capture_without_an_observable_null_marker() -> None:
    samples = np.exp(2j * np.pi * 100_000.0 / SAMPLE_RATE_HZ * np.arange(20_000))

    with pytest.raises(HexcalAnalysisError, match="no usable RF amplitude contrast"):
        analyze_hexcal_samples(
            samples,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=100_000.0,
            profile=load_hexcal_profile(PROFILE_PATH),
            continuity_verified=True,
        )


def test_independent_continuity_audit_requires_flags_counters_and_times() -> None:
    result = audit_continuity_metadata(
        _metadata(),
        expected_total_samples=1_000_000,
        expected_samples_per_block=100_000,
    )

    assert result["metadata_abi"] == 2
    assert result["block_count"] == 10
    assert result["stream_id"] == 1234
    assert result["abi2_flags_counters_order_and_rate_verified"] is True

    missing_flag = _metadata(flags=1 << 4)
    with pytest.raises(ValueError, match="required ABI2 validity flags"):
        audit_continuity_metadata(
            missing_flag,
            expected_total_samples=1_000_000,
            expected_samples_per_block=100_000,
        )
    failed = _metadata(flags=REQUIRED_METADATA_FLAGS | FAILURE_METADATA_FLAGS)
    with pytest.raises(ValueError, match="failure flags"):
        audit_continuity_metadata(
            failed,
            expected_total_samples=1_000_000,
            expected_samples_per_block=100_000,
        )

    unordered = _metadata()
    unordered["pluto:continuity"]["blocks"][4]["sample_time_monotonic_start_ns"] = 3_050_000_000
    unordered["pluto:continuity"]["blocks"][4]["sample_time_monotonic_end_ns"] = 3_150_000_000
    with pytest.raises(ValueError, match="monotonic mapping is not ordered"):
        audit_continuity_metadata(
            unordered,
            expected_total_samples=1_000_000,
            expected_samples_per_block=100_000,
        )

    wrong_duration = _metadata()
    wrong_duration["pluto:continuity"]["blocks"][7]["sample_time_realtime_end_ns"] += 1_000_000
    with pytest.raises(ValueError, match="duration disagrees with sample rate"):
        audit_continuity_metadata(
            wrong_duration,
            expected_total_samples=1_000_000,
            expected_samples_per_block=100_000,
        )

    boundary_gap = _metadata()
    for clock in ("realtime", "monotonic"):
        boundary_gap["pluto:continuity"]["blocks"][6][f"sample_time_{clock}_start_ns"] += 5_000_000
        boundary_gap["pluto:continuity"]["blocks"][6][f"sample_time_{clock}_end_ns"] += 5_000_000
    with pytest.raises(ValueError, match="cross-block time boundary is not contiguous"):
        audit_continuity_metadata(
            boundary_gap,
            expected_total_samples=1_000_000,
            expected_samples_per_block=100_000,
        )


def test_two_sided_complex_null_interpolation_cancels_linear_leakage_drift() -> None:
    profile = load_hexcal_profile(PROFILE_PATH)
    analysis = analyze_hexcal_samples(
        _synthetic_capture(
            sample_count=300_000,
            leakage_start=0.35 * np.exp(0.8j),
            leakage_drift_per_second=0.8 * np.exp(-0.5j),
        ),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=profile,
        continuity_verified=True,
    )

    assert "complex linear interpolation" in analysis["null_estimator"]
    assert "ANT6 uses the following frame marker" in analysis["null_estimator"]
    for index, state in enumerate(analysis["states"]):
        assert wrapped_phase_deg(
            state["phase_relative_to_ant1_deg"] - TRUE_PHASES_DEG[index]
        ) == pytest.approx(0.0, abs=2.0)


def test_pilot_snr_is_per_sample_not_coherent_mean_inflated() -> None:
    analysis = analyze_hexcal_samples(
        _synthetic_capture(sample_count=300_000, noise_sigma=0.01),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )

    observed = [float(state["pilot_snr_db"]) for state in analysis["states"]]
    assert min(observed) > 28.0
    assert max(observed) < 42.0
    assert "no sqrt(N) coherent-mean gain" in analysis["pilot_snr_estimator"]


def test_noisy_null_windows_contribute_to_snr_and_fail_the_20db_gate() -> None:
    analysis = analyze_hexcal_samples(
        _synthetic_capture(
            sample_count=300_000,
            noise_sigma=0.001,
            null_noise_sigma=0.18,
            seed=18,
        ),
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=100_000.0,
        profile=load_hexcal_profile(PROFILE_PATH),
        continuity_verified=True,
    )
    quality = evaluate_hexcal_quality(
        analysis,
        headroom_passed=True,
        minimum_complete_cycles=100,
    )

    assert min(float(state["pilot_snr_db"]) for state in analysis["states"]) < 20.0
    assert any(
        "pilot_snr_below_minimum" in state["rejection_reasons"] for state in quality["states"]
    )
    assert quality["passed"] is False


def test_quality_rejects_missing_or_out_of_window_timing_and_nan_state_metrics(
    synthetic_analysis: dict[str, Any],
) -> None:
    corrupted = deepcopy(synthetic_analysis)
    corrupted["timing"]["guard_observable_us"]["count"] -= 1
    corrupted["timing"]["cycle_us"]["maximum"] = 1600.0
    corrupted["timing"]["active_observable_us_by_state"]["ANT4"]["minimum"] = None
    corrupted["states"][4]["pilot_snr_db"] = float("nan")

    result = evaluate_hexcal_quality(corrupted, headroom_passed=True)

    assert result["passed"] is False
    assert "ordinary_guard_timing_below_minimum_or_unobserved" in result["global_rejection_reasons"]
    assert "cycle_timing_outside_profile_window" in result["global_rejection_reasons"]
    assert "ant4_dwell_timing_outside_window_or_unobserved" in result["global_rejection_reasons"]
    assert "pilot_snr_below_minimum" in result["states"][4]["rejection_reasons"]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _firmware_evidence_document(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    profile = load_hexcal_profile(PROFILE_PATH)
    firmware_elf = b"synthetic-reviewed-hexcal-elf"
    firmware = bytes(range(251)) * 7
    readback = firmware + b"\xff" * (16 * 1024 - len(firmware))
    uid_readback = bytes.fromhex("4c0055000950313950363920")
    uid_readback_path = tmp_path / "target-uid.bin"
    firmware_elf_path = tmp_path / "hexcal.elf"
    firmware_path = tmp_path / "hexcal.bin"
    readback_path = tmp_path / "full-flash.bin"
    uid_readback_path.write_bytes(uid_readback)
    firmware_elf_path.write_bytes(firmware_elf)
    firmware_path.write_bytes(firmware)
    readback_path.write_bytes(readback)
    document = {
        "schema": 1,
        "evidence_kind": "hexcal_v1_full_flash_readback",
        "board_id": "stm32c011-4c0055000950313950363920",
        "target_uid": "4c0055000950313950363920",
        "target_uid_readback": {
            "path": uid_readback_path.name,
            "sha256": _sha256_bytes(uid_readback),
            "size_bytes": len(uid_readback),
        },
        "source_commit": "1" * 40,
        "profile_file_sha256": profile.file_sha256,
        "profile_contract_sha256": profile.contract_sha256,
        "firmware_elf": {
            "path": firmware_elf_path.name,
            "sha256": _sha256_bytes(firmware_elf),
            "size_bytes": len(firmware_elf),
        },
        "firmware_bin": {
            "path": firmware_path.name,
            "sha256": _sha256_bytes(firmware),
            "size_bytes": len(firmware),
        },
        "full_flash_readback": {
            "path": readback_path.name,
            "sha256": _sha256_bytes(readback),
            "size_bytes": len(readback),
        },
        "verification": {
            "target_identity_verified": True,
            "full_flash_readback_verified": True,
            "image_prefix_matches": True,
            "erased_tail_verified": True,
            "verified_at": "2026-08-26T12:00:00+00:00",
            "method": "st-link full-flash readback",
        },
    }
    evidence_path = tmp_path / "firmware-evidence.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")
    return evidence_path, document


def test_firmware_evidence_binds_exact_board_uid_and_complete_16k_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path, document = _firmware_evidence_document(tmp_path)
    profile = load_hexcal_profile(PROFILE_PATH)
    monkeypatch.setattr(
        "smateway.hexcal.EXPECTED_HEXCAL_ELF_SHA256",
        document["firmware_elf"]["sha256"],
    )
    monkeypatch.setattr(
        "smateway.hexcal.EXPECTED_HEXCAL_BIN_SHA256",
        document["firmware_bin"]["sha256"],
    )
    monkeypatch.setattr(
        "smateway.hexcal.EXPECTED_HEXCAL_BIN_SIZE_BYTES",
        document["firmware_bin"]["size_bytes"],
    )
    monkeypatch.setattr(
        "smateway.hexcal.EXPECTED_HEXCAL_FULL_FLASH_SHA256",
        document["full_flash_readback"]["sha256"],
    )

    evidence = load_hexcal_firmware_evidence(
        evidence_path,
        expected_board_id="stm32c011-4c0055000950313950363920",
        expected_source_commit="1" * 40,
        expected_profile=profile,
    )

    assert evidence.target_uid == "4c0055000950313950363920"
    assert evidence.full_flash_readback_size_bytes == 16 * 1024

    document["target_uid"] = "0" * 24
    evidence_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly match the selected board ID suffix"):
        load_hexcal_firmware_evidence(
            evidence_path,
            expected_board_id="stm32c011-4c0055000950313950363920",
            expected_source_commit="1" * 40,
            expected_profile=profile,
        )

    document["target_uid"] = "4c0055000950313950363920"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / document["target_uid_readback"]["path"]).write_bytes(b"\x00" * 12)
    with pytest.raises(ValueError, match="raw target UID readback"):
        load_hexcal_firmware_evidence(
            evidence_path,
            expected_board_id="stm32c011-4c0055000950313950363920",
            expected_source_commit="1" * 40,
            expected_profile=profile,
        )


def test_firmware_evidence_rejects_arbitrary_self_consistent_image(tmp_path: Path) -> None:
    evidence_path, _ = _firmware_evidence_document(tmp_path)

    with pytest.raises(ValueError, match="exact reviewed hexcal-v1 image"):
        load_hexcal_firmware_evidence(
            evidence_path,
            expected_board_id="stm32c011-4c0055000950313950363920",
            expected_source_commit="1" * 40,
            expected_profile=load_hexcal_profile(PROFILE_PATH),
        )


def test_dependency_attestation_requires_exact_clean_checkout_and_import_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "pluto-plus-utils"
    module_path = repository / "src/pluto_plus/__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text('"""fixture package"""\n', encoding="utf-8")
    (repository / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Hexcal Test",
            "-c",
            "user.email=hexcal@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ),
        cwd=repository,
        check=True,
    )
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        "smateway.hexcal.importlib.util.find_spec",
        lambda _name: SimpleNamespace(origin=str(module_path)),
    )

    evidence = attest_pluto_plus_utils_source(
        repository=repository,
        expected_commit=commit,
        imported_modules=(("pluto_plus", "src/pluto_plus/__init__.py"),),
        require_repository_runtime=False,
    )
    assert evidence["commit"] == commit
    assert {item["path"] for item in evidence["files"]} == {
        "pyproject.toml",
        "src/pluto_plus/__init__.py",
        "uv.lock",
    }

    escaped = tmp_path / "ambient_pluto_plus.py"
    escaped.write_text("# ambient\n", encoding="utf-8")
    monkeypatch.setattr(
        "smateway.hexcal.importlib.util.find_spec",
        lambda _name: SimpleNamespace(origin=str(escaped)),
    )
    with pytest.raises(ValueError, match="escaped the pinned checkout"):
        attest_pluto_plus_utils_source(
            repository=repository,
            expected_commit=commit,
            imported_modules=(("pluto_plus", "src/pluto_plus/__init__.py"),),
            require_repository_runtime=False,
        )

    monkeypatch.setattr(
        "smateway.hexcal.importlib.util.find_spec",
        lambda _name: SimpleNamespace(origin=str(module_path)),
    )
    module_path.write_text("# modified\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source tree is dirty"):
        attest_pluto_plus_utils_source(
            repository=repository,
            expected_commit=commit,
            imported_modules=(("pluto_plus", "src/pluto_plus/__init__.py"),),
            require_repository_runtime=False,
        )


@pytest.mark.skipif(
    not Path("/home/pi/pluto-plus-utils/.venv/bin/python").is_file(),
    reason="pinned local pluto-plus-utils runtime is unavailable",
)
def test_local_dependency_attestation_runs_under_exact_pinned_interpreter() -> None:
    repository = Path(__file__).resolve().parents[1]
    python = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository / "src")
    completed = subprocess.run(
        (
            str(python),
            "-c",
            (
                "import json; "
                "from smateway.hexcal import attest_pluto_plus_utils_source; "
                "print(json.dumps(attest_pluto_plus_utils_source(), sort_keys=True))"
            ),
        ),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["commit"] == "dd48f2a76d4b01152ca13ad0612d4b21f0bfd15a"
    assert evidence["clean_worktree_verified"] is True
    assert evidence["python_executable"] == str(python)
    assert evidence["python_prefix"] == "/home/pi/pluto-plus-utils/.venv"
    assert all(
        item["path"].startswith("/home/pi/pluto-plus-utils/src/pluto_plus/")
        for item in evidence["imported_modules"]
    )


def _rf_readback_evidence() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": 8,
        "tx_hardware_gain_db_requested": -40.0,
        "tx_hardware_gain_readback_db_by_channel": [-40.0, -80.0],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_scale_requested": 0.125,
        "dds_scale_readback": [0.125, 0.0, -0.125, 0.0, 0.0, 0.0, 0.0, 0.0],
        "dds_enabled_readback": [True, False, True, False, False, False, False, False],
        "tone_frequency_hz_requested": 100_000.0,
        "dds_frequency_readback_hz": [100_000, 0, -100_000, 0, 0, 0, 0, 0],
        "active_dds_indices": [0, 2],
        "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
        "inactive_dds_rf_activity_contract": (
            "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
        ),
    }


def test_rf_readback_evidence_rejects_every_unplanned_tx_or_dds_path() -> None:
    def validate(evidence: dict[str, Any]) -> None:
        validate_tx1_rf_readback_evidence(
            evidence,
            planned_kernel_buffers=8,
            planned_tx_gain_db=-40.0,
            planned_dds_scale=0.125,
            planned_tone_hz=100_000.0,
            sample_rate_hz=1_000_000.0,
        )

    validate(_rf_readback_evidence())
    global_enable_readback = _rf_readback_evidence()
    global_enable_readback["dds_enabled_readback"] = [True] * 8
    global_enable_readback["dds_frequency_readback_hz"][2] = 100_000
    global_enable_readback["dds_frequency_readback_hz"][5] = 321_000
    validate(global_enable_readback)

    mutations = (
        ("kernel-buffer", lambda item: item.update(kernel_buffers=7)),
        (
            "above the planned gain",
            lambda item: item["tx_hardware_gain_readback_db_by_channel"].__setitem__(0, -39.0),
        ),
        (
            "TX2 gain readback",
            lambda item: item["tx_hardware_gain_readback_db_by_channel"].__setitem__(1, -79.0),
        ),
        (
            "inactive DDS scale",
            lambda item: item["dds_scale_readback"].__setitem__(5, 0.01),
        ),
        (
            "exact booleans",
            lambda item: item["dds_enabled_readback"].__setitem__(5, 1),
        ),
        (
            "DDS frequency readback differs",
            lambda item: item["dds_frequency_readback_hz"].__setitem__(0, 80_000),
        ),
    )
    for message, mutate in mutations:
        corrupted = _rf_readback_evidence()
        mutate(corrupted)
        with pytest.raises(ValueError, match=message):
            validate(corrupted)


def test_complex_corrections_flatten_a_manifold_and_dft_uses_six_modes() -> None:
    states = [
        {
            "normalized_gain_db": float(gain),
            "phase_circular_centered_deg": float(phase),
        }
        for gain, phase in zip(TRUE_GAINS_DB - np.mean(TRUE_GAINS_DB), TRUE_PHASES_DEG, strict=True)
    ]
    correction = correction_coefficients(states)
    measured = 10.0 ** ((TRUE_GAINS_DB - np.mean(TRUE_GAINS_DB)) / 20.0) * np.exp(
        1j * np.deg2rad(TRUE_PHASES_DEG)
    )
    flattened = measured * np.asarray(correction)
    modes = six_point_dft(tuple(complex(value) for value in flattened))

    assert np.max(np.abs(flattened - flattened[0])) < 1e-12
    assert len(modes) == 6
    assert abs(modes[0]) == pytest.approx(1.0)
    assert max(abs(value) for value in modes[1:]) < 1e-12
