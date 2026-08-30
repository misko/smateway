"""Offline PSD analysis and cohort admission for true TX-muted controls.

The functions in this module deliberately analyze RX1 and RX2 independently.
There is no coherent pilot in a muted capture, so a transfer function or
RX2/RX1 phase is not a defined result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

EXPECTED_COHORT_SIZE = 5
TARGET_HALF_WIDTH_HZ = 2_000.0
DC_HALF_WIDTH_HZ = 5_000.0
FILTER_EDGE_START_HZ = 350_000.0
CONTROL_CENTER_DELTA_HZ = 15_000.0
CONTROL_HALF_WIDTH_HZ = 5_000.0
LOCAL_NOISE_HALF_WIDTH_HZ = 10_000.0
NARROWBAND_EXCESS_DB = 10.0
FLOOR_ELEVATION_DB = 3.0
DEFAULT_WELCH_SEGMENT_SAMPLES = 65_536
DEFAULT_WELCH_OVERLAP_SAMPLES = DEFAULT_WELCH_SEGMENT_SAMPLES // 2
TOP_FEATURE_COUNT = 12
EXPECTED_CENTER_FREQUENCY_HZ = 5_800_000_000
EXPECTED_SAMPLE_RATE_HZ = 1_000_000
EXPECTED_BANDWIDTH_HZ = 800_000
EXPECTED_RECEIVER_GAIN_DB = 40
EXPECTED_SAMPLE_COUNT = 10_000_000
EXPECTED_SAMPLES_PER_FRAME = 100_000
EXPECTED_FRAME_COUNT = 100
EXPECTED_KERNEL_BUFFERS = 8


class MutedControlAnalysisError(RuntimeError):
    """Muted-control evidence is malformed, incomplete, or non-comparable."""


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MutedControlAnalysisError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MutedControlAnalysisError(f"{label} must be finite")
    return result


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MutedControlAnalysisError(f"{label} must be an integer of at least {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MutedControlAnalysisError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise MutedControlAnalysisError(f"{label} must be an array")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MutedControlAnalysisError(f"{label} must be a nonempty string")
    return value


def _exact_mute_passed(
    value: object,
    *,
    serial: str,
    uri: str,
    purpose: str,
) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_serial_tx_mute_and_full_dds_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("uri") == uri
        and value.get("tx_hardware_gain_db_by_channel") == [-80.0, -80.0]
        and value.get("dds_raw_readback") == [0.0] * 8
        and value.get("dds_scale_readback") == [0.0] * 8
        and value.get("dds_enabled_readback") == [False] * 8
        and value.get("error") is None
    )


def _db(value: float) -> float:
    return 10.0 * math.log10(max(value, np.finfo(np.float64).tiny))


def _validate_window_geometry(sample_rate_hz: float, pilot_offset_hz: float) -> None:
    nyquist = sample_rate_hz / 2.0
    if not 0.0 < pilot_offset_hz < nyquist:
        raise MutedControlAnalysisError("positive P0 pilot readback is outside the sampled band")
    target_low = pilot_offset_hz - TARGET_HALF_WIDTH_HZ
    target_high = pilot_offset_hz + TARGET_HALF_WIDTH_HZ
    controls = (
        (
            pilot_offset_hz - CONTROL_CENTER_DELTA_HZ - CONTROL_HALF_WIDTH_HZ,
            pilot_offset_hz - CONTROL_CENTER_DELTA_HZ + CONTROL_HALF_WIDTH_HZ,
        ),
        (
            pilot_offset_hz + CONTROL_CENTER_DELTA_HZ - CONTROL_HALF_WIDTH_HZ,
            pilot_offset_hz + CONTROL_CENTER_DELTA_HZ + CONTROL_HALF_WIDTH_HZ,
        ),
    )
    if target_low <= DC_HALF_WIDTH_HZ or target_high >= FILTER_EDGE_START_HZ:
        raise MutedControlAnalysisError("target window overlaps the frozen DC or edge exclusion")
    if any(low <= DC_HALF_WIDTH_HZ or high >= FILTER_EDGE_START_HZ for low, high in controls):
        raise MutedControlAnalysisError("control window overlaps the frozen DC or edge exclusion")
    if nyquist <= FILTER_EDGE_START_HZ:
        raise MutedControlAnalysisError(
            "sample rate does not contain the frozen filter-edge region"
        )


def _welch_power(
    samples: npt.NDArray[np.complexfloating[Any, Any]],
    *,
    sample_rate_hz: float,
    segment_samples: int,
    overlap_samples: int,
) -> tuple[
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    int,
    float,
    float,
]:
    if segment_samples < 128 or segment_samples > samples.size:
        raise MutedControlAnalysisError("Welch segment length must be 128..sample_count")
    if not 0 <= overlap_samples < segment_samples:
        raise MutedControlAnalysisError("Welch overlap must be in 0..segment_samples-1")
    hop = segment_samples - overlap_samples
    starts = range(0, samples.size - segment_samples + 1, hop)
    window = np.hanning(segment_samples).astype(np.float64)
    window_energy = float(np.sum(np.square(window)))
    window_sum = float(np.sum(window))
    if window_energy <= 0.0:
        raise MutedControlAnalysisError("Welch window has no energy")
    accumulated = np.zeros(segment_samples, dtype=np.float64)
    segment_count = 0
    for start in starts:
        segment = np.asarray(samples[start : start + segment_samples], dtype=np.complex128)
        transformed = np.fft.fftshift(np.fft.fft(segment * window))
        accumulated += np.square(np.abs(transformed)) / (sample_rate_hz * window_energy)
        segment_count += 1
    if segment_count < 1:
        raise MutedControlAnalysisError("Welch estimator produced no complete segment")
    frequency_hz = np.fft.fftshift(np.fft.fftfreq(segment_samples, d=1.0 / sample_rate_hz))
    return (
        frequency_hz,
        accumulated / segment_count,
        segment_count,
        window_energy,
        window_sum,
    )


def classify_frequency(offset_hz: float, pilot_offset_hz: float) -> str:
    """Classify one FFT bin using the frozen P1 exclusion precedence."""

    if abs(offset_hz - pilot_offset_hz) <= TARGET_HALF_WIDTH_HZ:
        return "target_window"
    if abs(offset_hz) <= DC_HALF_WIDTH_HZ:
        return "dc_lo_excluded"
    if abs(offset_hz + pilot_offset_hz) <= TARGET_HALF_WIDTH_HZ:
        return "conjugate_image_excluded"
    if abs(offset_hz) >= FILTER_EDGE_START_HZ:
        return "filter_edge_excluded"
    return "other_admitted_passband_diagnostic"


def _mask_between(
    frequencies_hz: npt.NDArray[np.float64], low_hz: float, high_hz: float
) -> npt.NDArray[np.bool_]:
    return np.asarray((frequencies_hz >= low_hz) & (frequencies_hz <= high_hz))


def _robust_floor(power: npt.NDArray[np.float64], mask: npt.NDArray[np.bool_], label: str) -> float:
    selected = power[mask]
    if selected.size < 3:
        raise MutedControlAnalysisError(f"{label} contains fewer than three analyzer bins")
    return float(np.median(selected))


def _top_features(
    frequencies_hz: npt.NDArray[np.float64],
    power: npt.NDArray[np.float64],
    *,
    pilot_offset_hz: float,
) -> list[dict[str, Any]]:
    order = np.argsort(power)[::-1]
    chosen: list[int] = []
    for raw_index in order:
        index = int(raw_index)
        if any(abs(index - prior) <= 2 for prior in chosen):
            continue
        chosen.append(index)
        if len(chosen) == TOP_FEATURE_COUNT:
            break
    return [
        {
            "offset_hz": float(frequencies_hz[index]),
            "welch_psd_counts2_per_hz": float(power[index]),
            "welch_psd_db_counts2_per_hz": _db(float(power[index])),
            "classification": classify_frequency(float(frequencies_hz[index]), pilot_offset_hz),
            "blocking_by_classification_alone": False,
        }
        for index in chosen
    ]


def analyze_muted_stream(
    samples: npt.ArrayLike,
    *,
    sample_rate_hz: float,
    pilot_offset_hz: float,
    segment_samples: int = DEFAULT_WELCH_SEGMENT_SAMPLES,
    overlap_samples: int | None = None,
) -> dict[str, Any]:
    """Analyze one continuous, dual-RX muted stream without a transfer ratio."""

    rate = _finite_number(sample_rate_hz, "sample rate")
    pilot = _finite_number(pilot_offset_hz, "P0 pilot offset")
    _validate_window_geometry(rate, pilot)
    values = np.asarray(samples)
    if values.ndim != 2 or values.shape[0] != 2 or values.shape[1] < segment_samples:
        raise MutedControlAnalysisError("muted IQ must have shape (2, samples)")
    if not np.iscomplexobj(values):
        raise MutedControlAnalysisError("muted IQ must be complex")
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise MutedControlAnalysisError("muted IQ contains non-finite samples")
    overlap = segment_samples // 2 if overlap_samples is None else overlap_samples
    receivers: list[dict[str, Any]] = []
    for receiver in range(2):
        channel = np.asarray(values[receiver], dtype=np.complex64)
        frequencies, power, welch_segments, window_energy, window_sum = _welch_power(
            channel,
            sample_rate_hz=rate,
            segment_samples=segment_samples,
            overlap_samples=overlap,
        )
        target = _mask_between(
            frequencies,
            pilot - TARGET_HALF_WIDTH_HZ,
            pilot + TARGET_HALF_WIDTH_HZ,
        )
        local = (
            _mask_between(
                frequencies,
                pilot - LOCAL_NOISE_HALF_WIDTH_HZ,
                pilot + LOCAL_NOISE_HALF_WIDTH_HZ,
            )
            & ~target
        )
        lower_control = _mask_between(
            frequencies,
            pilot - CONTROL_CENTER_DELTA_HZ - CONTROL_HALF_WIDTH_HZ,
            pilot - CONTROL_CENTER_DELTA_HZ + CONTROL_HALF_WIDTH_HZ,
        )
        upper_control = _mask_between(
            frequencies,
            pilot + CONTROL_CENTER_DELTA_HZ - CONTROL_HALF_WIDTH_HZ,
            pilot + CONTROL_CENTER_DELTA_HZ + CONTROL_HALF_WIDTH_HZ,
        )
        target_indices = np.flatnonzero(target)
        if target_indices.size < 3:
            raise MutedControlAnalysisError("target window contains fewer than three analyzer bins")
        peak_index = int(target_indices[int(np.argmax(power[target]))])
        peak_power = float(power[peak_index])
        peak_bin_amplitude_counts = (
            math.sqrt(max(peak_power, 0.0) * rate * window_energy) / window_sum
        )
        local_noise = _robust_floor(power, local, "local-noise window")
        bin_width_hz = rate / segment_samples
        integrated_excess_counts2 = float(
            np.sum(np.maximum(power[target] - local_noise, 0.0)) * bin_width_hz
        )
        integrated_tone_amplitude_counts = math.sqrt(max(integrated_excess_counts2, 0.0))
        target_floor = _robust_floor(power, target, "target window")
        lower_floor = _robust_floor(power, lower_control, "lower control window")
        upper_floor = _robust_floor(power, upper_control, "upper control window")
        peak_excess_db = _db(peak_power) - _db(local_noise)
        elevations = (
            _db(target_floor) - _db(lower_floor),
            _db(target_floor) - _db(upper_floor),
        )
        receivers.append(
            {
                "receiver": receiver,
                "sample_count": int(channel.size),
                "rms_counts": float(np.sqrt(np.mean(np.square(np.abs(channel))))),
                "peak_component_abs_counts": float(
                    max(np.max(np.abs(channel.real)), np.max(np.abs(channel.imag)))
                ),
                "welch": {
                    "window": "symmetric Hann via numpy.hanning",
                    "segment_samples": segment_samples,
                    "overlap_samples": overlap,
                    "segment_count": welch_segments,
                    "bin_width_hz": bin_width_hz,
                    "per_segment_mean_removed": False,
                    "power_units": "ADC_counts_squared_per_hz",
                    "robust_floor_estimator": "median_of_in-window_Welch_bins",
                },
                "target": {
                    "center_hz": pilot,
                    "half_width_hz": TARGET_HALF_WIDTH_HZ,
                    "peak_offset_hz": float(frequencies[peak_index]),
                    "peak_welch_psd_counts2_per_hz": peak_power,
                    "peak_welch_psd_db_counts2_per_hz": _db(peak_power),
                    "peak_bin_equivalent_amplitude_counts": peak_bin_amplitude_counts,
                    "target_integrated_tone_amplitude_counts": (integrated_tone_amplitude_counts),
                    "local_noise_welch_psd_counts2_per_hz": local_noise,
                    "local_noise_welch_psd_db_counts2_per_hz": _db(local_noise),
                    "peak_excess_over_local_noise_db": peak_excess_db,
                    "narrowband_candidate": peak_excess_db >= NARROWBAND_EXCESS_DB,
                },
                "floors": {
                    "target_window_welch_psd_counts2_per_hz": target_floor,
                    "lower_control_welch_psd_counts2_per_hz": lower_floor,
                    "upper_control_welch_psd_counts2_per_hz": upper_floor,
                    "lower_control_center_hz": pilot - CONTROL_CENTER_DELTA_HZ,
                    "upper_control_center_hz": pilot + CONTROL_CENTER_DELTA_HZ,
                    "control_window_width_hz": 2.0 * CONTROL_HALF_WIDTH_HZ,
                    "target_minus_lower_control_db": elevations[0],
                    "target_minus_upper_control_db": elevations[1],
                    "elevated_at_least_3db_over_both_controls": (
                        min(elevations) >= FLOOR_ELEVATION_DB
                    ),
                },
                "classified_top_features": _top_features(
                    frequencies,
                    power,
                    pilot_offset_hz=pilot,
                ),
            }
        )
    return {
        "schema": 1,
        "analysis_kind": "true_tx_muted_dual_rx_psd",
        "sample_rate_hz": rate,
        "pilot_offset_source": "P0_post_cycle_actual_positive_pilot_readback",
        "pilot_offset_hz": pilot,
        "frozen_windows": {
            "target_half_width_hz": TARGET_HALF_WIDTH_HZ,
            "dc_lo_exclusion_abs_hz": DC_HALF_WIDTH_HZ,
            "conjugate_image_half_width_hz": TARGET_HALF_WIDTH_HZ,
            "filter_edge_exclusion_abs_hz": FILTER_EDGE_START_HZ,
            "control_center_delta_hz": CONTROL_CENTER_DELTA_HZ,
            "control_window_width_hz": 2.0 * CONTROL_HALF_WIDTH_HZ,
            "narrowband_excess_threshold_db": NARROWBAND_EXCESS_DB,
            "floor_elevation_threshold_db": FLOOR_ELEVATION_DB,
        },
        "receivers": receivers,
        "rx1_rx2_transfer_phasor": None,
        "transfer_phase_defined": False,
        "interpretation": (
            "RX1 and RX2 are absolute, independent PSD observations; a muted capture has no "
            "reference pilot and cannot define RX2/RX1 transfer phase."
        ),
    }


def _validate_member(record: Mapping[str, Any], index: int) -> dict[str, Any]:
    label = f"cohort member {index}"
    if (
        record.get("schema") != 1
        or record.get("record_kind") != "5g8_true_tx_muted_control"
        or record.get("accepted") is not True
    ):
        raise MutedControlAnalysisError(f"{label} is not an accepted muted-control record")
    run_id = _nonempty_string(record.get("run_id"), f"{label} run ID")
    campaign_id = _nonempty_string(record.get("campaign_id"), f"{label} campaign ID")
    artifact = _mapping(record.get("artifact_evidence"), f"{label} artifact evidence")
    data_sha256 = _nonempty_string(artifact.get("data_sha256"), f"{label} data hash")
    capture = _mapping(record.get("capture"), f"{label} capture")
    exact_capture = {
        "center_frequency_hz": EXPECTED_CENTER_FREQUENCY_HZ,
        "sample_rate_hz": EXPECTED_SAMPLE_RATE_HZ,
        "bandwidth_hz": EXPECTED_BANDWIDTH_HZ,
        "receiver_gain_db": EXPECTED_RECEIVER_GAIN_DB,
        "sample_count": EXPECTED_SAMPLE_COUNT,
        "duration_s": 10.0,
        "samples_per_frame": EXPECTED_SAMPLES_PER_FRAME,
        "frame_count": EXPECTED_FRAME_COUNT,
        "kernel_buffers": EXPECTED_KERNEL_BUFFERS,
        "metadata_abi": 2,
        "tx_source_active": False,
        "receive_only_api": True,
    }
    if any(capture.get(field) != expected for field, expected in exact_capture.items()):
        raise MutedControlAnalysisError(f"{label} is not one exact 10-second ABI2 muted stream")
    stream_id = _integer(capture.get("stream_id"), f"{label} stream ID", minimum=1)
    serial = _nonempty_string(capture.get("serial"), f"{label} serial")
    uri = _nonempty_string(capture.get("uri"), f"{label} URI")
    continuity = _mapping(record.get("continuity_audit"), f"{label} continuity audit")
    if (
        continuity.get("metadata_abi") != 2
        or continuity.get("stream_id") != stream_id
        or continuity.get("total_samples") != EXPECTED_SAMPLE_COUNT
        or continuity.get("block_count") != EXPECTED_FRAME_COUNT
        or continuity.get("samples_per_block") != EXPECTED_SAMPLES_PER_FRAME
        or continuity.get("first_buffer_sequence") != 0
        or continuity.get("abi2_flags_counters_order_and_rate_verified") is not True
    ):
        raise MutedControlAnalysisError(f"{label} lacks exact persisted ABI2 continuity")
    safety = _mapping(record.get("safety"), f"{label} safety")
    if (
        not _exact_mute_passed(
            safety.get("post_capture_exact_mute"),
            serial=serial,
            uri=uri,
            purpose="post_capture",
        )
        or not _exact_mute_passed(
            safety.get("final_exact_mute"),
            serial=serial,
            uri=uri,
            purpose="final",
        )
        or safety.get("headroom_passed") is not True
        or safety.get("raw_persisted_only_after_post_capture_exact_mute_passed") is not True
        or safety.get("automatic_retry_count") != 0
    ):
        raise MutedControlAnalysisError(f"{label} lacks final mute or ADC headroom admission")
    analysis = _mapping(record.get("analysis"), f"{label} analysis")
    pilot_offset_hz = _finite_number(analysis.get("pilot_offset_hz"), f"{label} pilot offset")
    if (
        analysis.get("analysis_kind") != "true_tx_muted_dual_rx_psd"
        or analysis.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or analysis.get("pilot_offset_source") != "P0_post_cycle_actual_positive_pilot_readback"
        or analysis.get("transfer_phase_defined") is not False
        or analysis.get("rx1_rx2_transfer_phasor") is not None
    ):
        raise MutedControlAnalysisError(f"{label} improperly reports a muted transfer phasor")
    receivers = _sequence(analysis.get("receivers"), f"{label} receivers")
    if len(receivers) != 2:
        raise MutedControlAnalysisError(f"{label} must report RX1 and RX2 independently")
    normalized_receivers: list[dict[str, Any]] = []
    for receiver_index, raw in enumerate(receivers):
        receiver = _mapping(raw, f"{label} receiver {receiver_index}")
        if receiver.get("receiver") != receiver_index:
            raise MutedControlAnalysisError(f"{label} receiver order differs")
        welch = _mapping(receiver.get("welch"), f"{label} receiver Welch")
        target = _mapping(receiver.get("target"), f"{label} receiver target")
        floors = _mapping(receiver.get("floors"), f"{label} receiver floors")
        normalized_receivers.append(
            {
                "bin_width_hz": _finite_number(welch.get("bin_width_hz"), "bin width"),
                "peak_offset_hz": _finite_number(target.get("peak_offset_hz"), "peak offset"),
                "peak_excess_db": _finite_number(
                    target.get("peak_excess_over_local_noise_db"), "peak excess"
                ),
                "narrowband_candidate": target.get("narrowband_candidate") is True,
                "floor_elevated": floors.get("elevated_at_least_3db_over_both_controls") is True,
            }
        )
    return {
        "run_id": run_id,
        "campaign_id": campaign_id,
        "serial": serial,
        "pilot_offset_hz": pilot_offset_hz,
        "stream_id": stream_id,
        "data_sha256": data_sha256,
        "metadata_sha256": _nonempty_string(
            artifact.get("metadata_sha256"), f"{label} metadata hash"
        ),
        "fixture_sha256": _nonempty_string(
            record.get("fixture_evidence_sha256"), f"{label} fixture hash"
        ),
        "cohort_fixture_identity_sha256": _nonempty_string(
            record.get("cohort_fixture_identity_sha256"),
            f"{label} cohort fixture identity hash",
        ),
        "p0_post_cycle_schedule_proof_sha256": _nonempty_string(
            record.get("p0_post_cycle_schedule_proof_sha256"),
            f"{label} P0 post-cycle schedule-proof hash",
        ),
        "source_commit": _nonempty_string(record.get("source_commit"), f"{label} source commit"),
        "dependency_sha256": _nonempty_string(
            record.get("dependency_source_attestation_sha256"),
            f"{label} dependency attestation hash",
        ),
        "native_sha256": _nonempty_string(
            record.get("native_libiio_runtime_attestation_sha256"),
            f"{label} native attestation hash",
        ),
        "receivers": normalized_receivers,
    }


def aggregate_muted_control_cohort(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Admit and classify exactly five source-distinct muted-control streams."""

    if len(records) != EXPECTED_COHORT_SIZE:
        raise MutedControlAnalysisError(
            f"P1 cohort requires exactly {EXPECTED_COHORT_SIZE} run records"
        )
    members = [_validate_member(record, index) for index, record in enumerate(records)]
    distinct_fields = ("run_id", "stream_id", "data_sha256")
    for field in distinct_fields:
        values = [member[field] for member in members]
        if len(set(values)) != EXPECTED_COHORT_SIZE:
            raise MutedControlAnalysisError(f"P1 cohort reuses {field}")
    common_fields = (
        "campaign_id",
        "serial",
        "cohort_fixture_identity_sha256",
        "p0_post_cycle_schedule_proof_sha256",
        "source_commit",
        "dependency_sha256",
        "native_sha256",
        "pilot_offset_hz",
    )
    for field in common_fields:
        if len({member[field] for member in members}) != 1:
            raise MutedControlAnalysisError(f"P1 cohort members differ in {field}")

    receiver_results: list[dict[str, Any]] = []
    for receiver in range(2):
        observations = [member["receivers"][receiver] for member in members]
        bin_widths = [float(item["bin_width_hz"]) for item in observations]
        if max(bin_widths) - min(bin_widths) > 1e-9:
            raise MutedControlAnalysisError("P1 cohort Welch bin widths differ")
        candidates = [item for item in observations if item["narrowband_candidate"]]
        candidate_peaks = [float(item["peak_offset_hz"]) for item in candidates]
        alignment_limit_hz = 2.0 * bin_widths[0]
        sorted_peaks = sorted(candidate_peaks)
        aligned_cluster: list[float] = []
        left = 0
        for right, peak in enumerate(sorted_peaks):
            while peak - sorted_peaks[left] > alignment_limit_hz:
                left += 1
            cluster = sorted_peaks[left : right + 1]
            if len(cluster) > len(aligned_cluster):
                aligned_cluster = cluster
        aligned = len(aligned_cluster) >= 4
        narrowband_blocking = aligned
        elevated_members = [item for item in observations if item["floor_elevated"]]
        # The frozen plan's floor clause intentionally has no four-of-five
        # qualifier: one admitted stream with a >=3 dB robust-floor rise is a
        # stop condition and is retained for investigation.
        floor_blocking = bool(elevated_members)
        receiver_results.append(
            {
                "receiver": receiver,
                "narrowband_candidate_count": len(candidates),
                "candidate_peak_offsets_hz": candidate_peaks,
                "candidate_peak_span_hz": (
                    max(candidate_peaks) - min(candidate_peaks) if candidate_peaks else None
                ),
                "two_bin_alignment_limit_hz": alignment_limit_hz,
                "candidate_peaks_aligned": aligned,
                "largest_aligned_candidate_count": len(aligned_cluster),
                "largest_aligned_candidate_peak_offsets_hz": aligned_cluster,
                "narrowband_blocking": narrowband_blocking,
                "target_floor_elevated_count": len(elevated_members),
                "target_floor_blocking": floor_blocking,
                "peak_excess_db": [float(item["peak_excess_db"]) for item in observations],
            }
        )
    blocked = any(
        result["narrowband_blocking"] or result["target_floor_blocking"]
        for result in receiver_results
    )
    return {
        "schema": 1,
        "analysis_kind": "true_tx_muted_five_run_cohort",
        "campaign_id": members[0]["campaign_id"],
        "serial": members[0]["serial"],
        "source_commit": members[0]["source_commit"],
        "dependency_source_attestation_sha256": members[0]["dependency_sha256"],
        "native_libiio_runtime_attestation_sha256": members[0]["native_sha256"],
        "run_bound_fixture_evidence_sha256s": [member["fixture_sha256"] for member in members],
        "cohort_fixture_identity_sha256": members[0]["cohort_fixture_identity_sha256"],
        "p0_post_cycle_schedule_proof_sha256": members[0]["p0_post_cycle_schedule_proof_sha256"],
        "pilot_offset_hz": members[0]["pilot_offset_hz"],
        "run_ids": [member["run_id"] for member in members],
        "stream_ids": [member["stream_id"] for member in members],
        "source_artifacts": [
            {
                "run_id": member["run_id"],
                "stream_id": member["stream_id"],
                "data_sha256": member["data_sha256"],
                "metadata_sha256": member["metadata_sha256"],
            }
            for member in members
        ],
        "source_distinct_stream_count": EXPECTED_COHORT_SIZE,
        "receivers": receiver_results,
        "capture_system_or_interference_investigation_required": blocked,
        "cohort_disposition": "blocked_for_investigation" if blocked else "admitted_no_p1_blocker",
        "rx1_rx2_transfer_phasor": None,
        "transfer_phase_defined": False,
    }


__all__ = [
    "DEFAULT_WELCH_OVERLAP_SAMPLES",
    "DEFAULT_WELCH_SEGMENT_SAMPLES",
    "EXPECTED_COHORT_SIZE",
    "MutedControlAnalysisError",
    "aggregate_muted_control_cohort",
    "analyze_muted_stream",
    "classify_frequency",
]
