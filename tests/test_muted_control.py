from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from smateway.muted_control import (
    MutedControlAnalysisError,
    _welch_power,
    aggregate_muted_control_cohort,
    analyze_muted_stream,
    classify_frequency,
)


def _signal(
    *,
    sample_count: int = 262_144,
    target_amplitude: float = 0.0,
    target_offset_hz: float = 100_000.0,
) -> np.ndarray:
    rng = np.random.default_rng(20260829)
    samples = (
        rng.standard_normal((2, sample_count)) + 1j * rng.standard_normal((2, sample_count))
    ).astype(np.complex64)
    indices = np.arange(sample_count, dtype=np.float64)
    carrier = np.exp(2j * np.pi * target_offset_hz * indices / 1_000_000.0)
    samples[0] += np.asarray(target_amplitude * carrier, dtype=np.complex64)
    return samples


@pytest.mark.parametrize(
    ("offset_hz", "expected"),
    (
        (100_000.0, "target_window"),
        (0.0, "dc_lo_excluded"),
        (-100_000.0, "conjugate_image_excluded"),
        (400_000.0, "filter_edge_excluded"),
        (75_000.0, "other_admitted_passband_diagnostic"),
    ),
)
def test_frozen_frequency_classification(offset_hz: float, expected: str) -> None:
    assert classify_frequency(offset_hz, 100_000.0) == expected


def test_welch_psd_integrates_to_complex_white_noise_power() -> None:
    rng = np.random.default_rng(7)
    samples = (rng.standard_normal(524_288) + 1j * rng.standard_normal(524_288)).astype(
        np.complex64
    )

    frequencies, psd, count, _window_energy, _window_sum = _welch_power(
        samples,
        sample_rate_hz=1_000_000.0,
        segment_samples=65_536,
        overlap_samples=32_768,
    )

    integrated = float(np.sum(psd) * (frequencies[1] - frequencies[0]))
    observed = float(np.mean(np.square(np.abs(samples))))
    assert count == 15
    assert integrated == pytest.approx(observed, rel=0.04)


def test_exact_target_tone_is_reported_in_counts_and_never_as_transfer_phase() -> None:
    result = analyze_muted_stream(
        _signal(target_amplitude=30.0),
        sample_rate_hz=1_000_000.0,
        pilot_offset_hz=100_000.0,
    )

    rx1 = result["receivers"][0]
    target = rx1["target"]
    assert target["narrowband_candidate"] is True
    assert target["peak_offset_hz"] == pytest.approx(100_000.0, abs=16.0)
    assert target["target_integrated_tone_amplitude_counts"] == pytest.approx(30.0, rel=0.03)
    assert target["peak_excess_over_local_noise_db"] > 30.0
    assert result["transfer_phase_defined"] is False
    assert result["rx1_rx2_transfer_phasor"] is None
    assert all("phase" not in receiver["target"] for receiver in result["receivers"])


def test_expected_dc_image_and_edge_features_remain_diagnostics() -> None:
    samples = _signal()
    indices = np.arange(samples.shape[1], dtype=np.float64)
    samples[0] += 80.0
    samples[0] += np.asarray(
        60.0 * np.exp(-2j * np.pi * 100_000.0 * indices / 1_000_000.0),
        dtype=np.complex64,
    )
    samples[0] += np.asarray(
        40.0 * np.exp(2j * np.pi * 400_000.0 * indices / 1_000_000.0),
        dtype=np.complex64,
    )

    result = analyze_muted_stream(
        samples,
        sample_rate_hz=1_000_000.0,
        pilot_offset_hz=100_000.0,
    )

    classes = {
        feature["classification"] for feature in result["receivers"][0]["classified_top_features"]
    }
    assert "dc_lo_excluded" in classes
    assert "conjugate_image_excluded" in classes
    assert "filter_edge_excluded" in classes
    assert result["receivers"][0]["target"]["narrowband_candidate"] is False


@pytest.mark.parametrize("pilot", (4_000.0, 340_000.0, -100_000.0))
def test_analysis_rejects_windows_overlapping_exclusions(pilot: float) -> None:
    with pytest.raises(MutedControlAnalysisError, match="window|positive"):
        analyze_muted_stream(
            _signal(sample_count=65_536),
            sample_rate_hz=1_000_000.0,
            pilot_offset_hz=pilot,
        )


def _record(
    index: int,
    *,
    rx0_candidate: bool = False,
    rx0_peak_hz: float = 100_000.0,
    rx0_floor: bool = False,
    stream_id: int | None = None,
) -> dict[str, Any]:
    def exact_mute(purpose: str) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
            "purpose": purpose,
            "status": "passed",
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
            "dds_raw_readback": [0.0] * 8,
            "dds_scale_readback": [0.0] * 8,
            "dds_enabled_readback": [False] * 8,
            "error": None,
        }

    receiver_rows = []
    for receiver in range(2):
        candidate = rx0_candidate if receiver == 0 else False
        floor = rx0_floor if receiver == 0 else False
        receiver_rows.append(
            {
                "receiver": receiver,
                "welch": {"bin_width_hz": 1_000_000.0 / 65_536},
                "target": {
                    "peak_offset_hz": rx0_peak_hz if receiver == 0 else 101_000.0,
                    "peak_excess_over_local_noise_db": 12.0 if candidate else 2.0,
                    "narrowband_candidate": candidate,
                },
                "floors": {"elevated_at_least_3db_over_both_controls": floor},
            }
        )
    return {
        "schema": 1,
        "record_kind": "5g8_true_tx_muted_control",
        "accepted": True,
        "run_id": f"p1-r{index + 1:02d}",
        "campaign_id": "campaign-a",
        "source_commit": "1" * 40,
        "dependency_source_attestation_sha256": "2" * 64,
        "native_libiio_runtime_attestation_sha256": "3" * 64,
        "fixture_evidence_sha256": "4" * 64,
        "cohort_fixture_identity_sha256": "6" * 64,
        "p0_post_cycle_schedule_proof_sha256": "5" * 64,
        "artifact_evidence": {
            "data_sha256": f"{index + 10:064x}",
            "metadata_sha256": f"{index + 20:064x}",
        },
        "capture": {
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "center_frequency_hz": 5_800_000_000,
            "sample_rate_hz": 1_000_000,
            "bandwidth_hz": 800_000,
            "receiver_gain_db": 40,
            "sample_count": 10_000_000,
            "duration_s": 10.0,
            "samples_per_frame": 100_000,
            "frame_count": 100,
            "kernel_buffers": 8,
            "metadata_abi": 2,
            "stream_id": stream_id if stream_id is not None else 100 + index,
            "tx_source_active": False,
            "receive_only_api": True,
        },
        "continuity_audit": {
            "metadata_abi": 2,
            "stream_id": stream_id if stream_id is not None else 100 + index,
            "total_samples": 10_000_000,
            "block_count": 100,
            "samples_per_block": 100_000,
            "first_buffer_sequence": 0,
            "abi2_flags_counters_order_and_rate_verified": True,
        },
        "safety": {
            "post_capture_exact_mute": exact_mute("post_capture"),
            "final_exact_mute": exact_mute("final"),
            "headroom_passed": True,
            "raw_persisted_only_after_post_capture_exact_mute_passed": True,
            "automatic_retry_count": 0,
        },
        "analysis": {
            "analysis_kind": "true_tx_muted_dual_rx_psd",
            "sample_rate_hz": 1_000_000,
            "pilot_offset_source": "P0_post_cycle_actual_positive_pilot_readback",
            "pilot_offset_hz": 100_000.0,
            "transfer_phase_defined": False,
            "rx1_rx2_transfer_phasor": None,
            "receivers": receiver_rows,
        },
    }


def test_cohort_requires_four_aligned_narrowband_observations() -> None:
    records = [
        _record(index, rx0_candidate=index < 4, rx0_peak_hz=100_000.0 + index * 2.0)
        for index in range(5)
    ]

    result = aggregate_muted_control_cohort(records)

    rx1 = result["receivers"][0]
    assert rx1["narrowband_candidate_count"] == 4
    assert rx1["candidate_peaks_aligned"] is True
    assert rx1["narrowband_blocking"] is True
    assert result["cohort_disposition"] == "blocked_for_investigation"
    assert result["transfer_phase_defined"] is False


def test_three_candidates_or_four_misaligned_candidates_do_not_trigger_narrowband_gate() -> None:
    only_three = [_record(index, rx0_candidate=index < 3) for index in range(5)]
    misaligned = [
        _record(index, rx0_candidate=index < 4, rx0_peak_hz=100_000.0 + index * 20.0)
        for index in range(5)
    ]

    assert (
        aggregate_muted_control_cohort(only_three)["receivers"][0]["narrowband_blocking"] is False
    )
    assert (
        aggregate_muted_control_cohort(misaligned)["receivers"][0]["narrowband_blocking"] is False
    )


def test_fifth_outlier_cannot_hide_four_aligned_candidates() -> None:
    peaks = [100_000.0, 100_005.0, 100_010.0, 100_020.0, 101_000.0]
    records = [_record(index, rx0_candidate=True, rx0_peak_hz=peaks[index]) for index in range(5)]

    result = aggregate_muted_control_cohort(records)["receivers"][0]

    assert result["narrowband_candidate_count"] == 5
    assert result["largest_aligned_candidate_count"] == 4
    assert result["candidate_peaks_aligned"] is True
    assert result["narrowband_blocking"] is True


def test_one_admitted_three_db_floor_elevation_is_a_stop_condition() -> None:
    records = [_record(index, rx0_floor=index == 2) for index in range(5)]

    result = aggregate_muted_control_cohort(records)

    assert result["receivers"][0]["target_floor_elevated_count"] == 1
    assert result["receivers"][0]["target_floor_blocking"] is True
    assert result["capture_system_or_interference_investigation_required"] is True


@pytest.mark.parametrize("count", (4, 6))
def test_cohort_rejects_wrong_run_count(count: int) -> None:
    with pytest.raises(MutedControlAnalysisError, match="exactly 5"):
        aggregate_muted_control_cohort([_record(index) for index in range(count)])


@pytest.mark.parametrize("duplicate", ("run", "stream", "data"))
def test_cohort_rejects_reused_source_identity(duplicate: str) -> None:
    records = [_record(index) for index in range(5)]
    if duplicate == "run":
        records[4]["run_id"] = records[0]["run_id"]
    elif duplicate == "stream":
        records[4]["capture"]["stream_id"] = records[0]["capture"]["stream_id"]
        records[4]["continuity_audit"]["stream_id"] = records[0]["continuity_audit"]["stream_id"]
    else:
        records[4]["artifact_evidence"]["data_sha256"] = records[0]["artifact_evidence"][
            "data_sha256"
        ]

    with pytest.raises(MutedControlAnalysisError, match="reuses"):
        aggregate_muted_control_cohort(records)


def test_cohort_allows_distinct_run_bound_setup_attestations_for_one_fixture() -> None:
    records = [_record(index) for index in range(5)]
    for index, record in enumerate(records):
        record["fixture_evidence_sha256"] = f"{index + 30:064x}"

    result = aggregate_muted_control_cohort(records)

    assert len(set(result["run_bound_fixture_evidence_sha256s"])) == 5
    assert result["cohort_fixture_identity_sha256"] == "6" * 64


def test_cohort_rejects_a_different_physical_fixture_identity() -> None:
    records = [_record(index) for index in range(5)]
    records[-1]["cohort_fixture_identity_sha256"] = "f" * 64

    with pytest.raises(MutedControlAnalysisError, match="cohort_fixture_identity"):
        aggregate_muted_control_cohort(records)


def test_cohort_rejects_any_cross_channel_transfer_claim() -> None:
    records = [_record(index) for index in range(5)]
    records[0]["analysis"]["transfer_phase_defined"] = True

    with pytest.raises(MutedControlAnalysisError, match="transfer phasor"):
        aggregate_muted_control_cohort(records)
