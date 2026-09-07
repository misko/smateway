#!/usr/bin/env python3
"""Capture one sample-timestamped TX1 or TX2 switched-array screen."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import fcntl
import hashlib
import importlib
import json
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPOSITORY = Path(__file__).resolve().parents[1]
_PINNED_PREFIX = _REPOSITORY / ".venv"
_PINNED_PYTHON = _PINNED_PREFIX / "bin/python"
_PINNED_LIB = _PINNED_PREFIX / "lib"
_SOURCE_ROOT = _REPOSITORY / "src"
_loader_entries = tuple(
    Path(item).resolve()
    for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
    if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX.resolve()
    or str(_SOURCE_ROOT) not in sys.path
    or not _loader_entries
    or _loader_entries[0] != _PINNED_LIB.resolve()
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned tracking Python is unavailable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SOURCE_ROOT)
        if not prior_pythonpath
        else f"{_SOURCE_ROOT}{os.pathsep}{prior_pythonpath}"
    )
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (
            str(_PINNED_LIB),
            *(
                item
                for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
                if item and Path(item).resolve() != _PINNED_LIB.resolve()
            ),
        )
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

import numpy as np
from pluto_plus.hardware import IioRadioDevice, SampleBlockV2
from pluto_plus.hardware.iio import _mute_transmit, _release_device
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION, verify_metadata_runtime
from pluto_plus.models import GainMode, RadioSettings
from pluto_plus.tandem import TandemMode, TandemSessionRequestV1

from smateway.bench import BenchManifest, OpenOcdBench
from smateway.profile import load_profile
from smateway.tracking import (
    BoardCalibrationLut,
    SampleTimeBlock,
    SelectorEvent,
    estimate_continuous_tone_frequency,
    estimate_cross_frequency_transfers,
    estimate_same_emitter_transfer,
    far_field_steering,
    load_ism_band_profiles,
    selector_dwell_intervals,
    solve_bearing,
)

RECEIVER_URI = "ip:192.168.1.15"
RECEIVER_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.179"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
BOARD_ID = "stm32c011-4c0055000950313950363920"
STLINK_SERIAL = "002D003A3335511035383531"
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
TX2_REFERENCE_TONE_OFFSET_HZ = -100_000
FRAME_SAMPLES = 100_000
KERNEL_BUFFERS = 64
RX_GAIN_DB = 60
MAX_TX_GAIN_DB = -35.0
DDS_SCALE = 0.25
SELECTOR_LEASE_MS = 5000
SELECTOR_COMMAND_SETTLE_MS = 50
DWELL_MS = 250
EDGE_GUARD_MS = 10
TAIL_MS = 150
MAX_FRAMES = 500
DENSE_5G8_MIN_CENTER_HZ = 5_726_000_000
DENSE_5G8_MAX_CENTER_HZ = 5_874_000_000
DENSE_5G8_LATTICE_HZ = 100_000


class CooperativeTermination(RuntimeError):
    """A cooperative signal converted into fail-muted unwinding."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _signal_handler(signum: int, _frame: object) -> None:
    raise CooperativeTermination(f"received {signal.Signals(signum).name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--frequency-hz", type=int, required=True)
    parser.add_argument("--tx-channel", type=int, choices=(0, 1), required=True)
    parser.add_argument("--scan-pairs", type=int, default=1)
    parser.add_argument("--dwell-ms", type=int, default=DWELL_MS)
    parser.add_argument("--tx-gain-db", type=float, default=MAX_TX_GAIN_DB)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument(
        "--dense-5g8-campaign",
        action="store_true",
        help=(
            "admit a 100 kHz-lattice centre from 5.726 through 5.874 GHz; "
            "the edge guard keeps both TX2 tones inside the ISM allocation"
        ),
    )
    parser.add_argument("--receiver-uri", default=RECEIVER_URI)
    parser.add_argument("--source-uri", default=SOURCE_URI)
    parser.add_argument(
        "--frequency-plan",
        type=Path,
        default=Path("docs/tracking_development_plan/data/ism-frequency-plan.json"),
    )
    parser.add_argument(
        "--board-lut",
        type=Path,
        default=Path("docs/pcb_direct_injection_calibration/data/calibration-lut.json"),
    )
    parser.add_argument(
        "--selector-profile",
        type=Path,
        default=Path("profiles/fast20-v1/control_profile.json"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("build/STM32C011F4P6/bench/pluto_bench.manifest.json"),
    )
    parser.add_argument(
        "--openocd-config",
        type=Path,
        default=Path("openocd/stlink-v3-stm32c011.cfg"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("/srv/bulk/samteway/lab-data/tracking-continuous"),
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"tracking hardware lock is held: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _mute_readback(device: Any) -> dict[str, Any]:
    _mute_transmit(device)
    gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
    scales = [float(value) for value in device.dds_scales]
    passed = gains == [-80.0, -80.0] and len(scales) == 8 and not any(scales)
    if not passed:
        raise RuntimeError("exact dual-channel transmit mute readback failed")
    return {"passed": True, "tx_gain_db": gains, "dds_scales": scales}


def _source_facts(device: Any, *, uri: str) -> dict[str, str]:
    facts = {str(key): str(value) for key, value in device.ctx.attrs.items()}
    if facts.get("hw_serial") != SOURCE_SERIAL:
        raise RuntimeError("source serial does not match the pinned source")
    if facts.get("uri") != uri:
        raise RuntimeError("source URI readback does not match the requested endpoint")
    return facts


def _enable_source(
    device: Any,
    *,
    frequency_hz: int,
    tx_channel: int,
    tx_gain_db: float,
) -> dict[str, Any]:
    _mute_readback(device)
    device.sample_rate = SAMPLE_RATE_HZ
    device.tx_rf_bandwidth = BANDWIDTH_HZ
    device.tx_lo = frequency_hz
    if abs(float(device.tx_lo) - frequency_hz) > 5.0:
        raise RuntimeError("source LO readback is outside the 5 Hz tolerance")
    if tx_channel == 0:
        device.tx_hardwaregain_chan0 = tx_gain_db
        device.dds_single_tone(TONE_OFFSET_HZ, DDS_SCALE, channel=0)
        source_mode = "tx1_same_frequency_conducted_reference"
        reference_tone_hz: int | None = TONE_OFFSET_HZ
    else:
        # TX1 remains the conducted RX1 pilot while TX2 is the blind OTA
        # target.  Opposite-frequency tones share the source LO and sample
        # clock, but are separable even though both antennas illuminate RX2.
        # The explicit array assignment avoids dds_single_tone() clearing the
        # other channel when the second tone is configured.
        device.tx_hardwaregain_chan0 = tx_gain_db
        device.tx_hardwaregain_chan1 = tx_gain_db
        device.dds_single_tone(TONE_OFFSET_HZ, 0.0, channel=0)
        frequencies = [
            abs(TX2_REFERENCE_TONE_OFFSET_HZ),
            abs(TX2_REFERENCE_TONE_OFFSET_HZ),
            abs(TX2_REFERENCE_TONE_OFFSET_HZ),
            abs(TX2_REFERENCE_TONE_OFFSET_HZ),
            TONE_OFFSET_HZ,
            TONE_OFFSET_HZ,
            TONE_OFFSET_HZ,
            TONE_OFFSET_HZ,
        ]
        phases = [0, 0, 90_000, 0, 90_000, 0, 0, 0]
        scales = [DDS_SCALE, 0.0, DDS_SCALE, 0.0, DDS_SCALE, 0.0, DDS_SCALE, 0.0]
        device.dds_frequencies = frequencies
        device.dds_phases = phases
        device.dds_scales = scales
        device.dds_enabled = [1] * len(scales)
        source_mode = "tx2_with_frequency_separated_coherent_tx1_pilot"
        reference_tone_hz = TX2_REFERENCE_TONE_OFFSET_HZ
    gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
    scales = [float(value) for value in device.dds_scales]
    frequencies = [int(value) for value in device.dds_frequencies]
    phases = [int(value) for value in device.dds_phases]
    enabled = [int(value) for value in device.dds_enabled]
    expected_gains = [tx_gain_db, -80.0] if tx_channel == 0 else [tx_gain_db, tx_gain_db]
    if any(
        actual > expected + 0.25 or actual < expected - 0.25
        for actual, expected in zip(gains, expected_gains, strict=True)
    ):
        raise RuntimeError("source transmit-gain readback violates the bounded plan")
    if max(abs(value) for value in scales) > DDS_SCALE + 1e-6:
        raise RuntimeError("source DDS scale exceeds the bounded plan")
    active_indices = {0, 2} if tx_channel == 0 else {0, 2, 4, 6}
    if any(
        (index in active_indices) != (abs(scale - DDS_SCALE) <= 1e-6)
        for index, scale in enumerate(scales)
    ):
        raise RuntimeError("source DDS active-channel readback violates the plan")
    expected_phases = (
        [90_000, 0, 0, 0, 0, 0, 0, 0]
        if tx_channel == 0
        else [0, 0, 90_000, 0, 90_000, 0, 0, 0]
    )
    if phases != expected_phases or enabled != [1] * 8:
        raise RuntimeError("source DDS phase or enable readback violates the plan")
    target_tone_readback_hz = frequencies[0 if tx_channel == 0 else 4]
    reference_tone_readback_hz = (
        target_tone_readback_hz if tx_channel == 0 else -frequencies[0]
    )
    if (
        abs(target_tone_readback_hz - TONE_OFFSET_HZ) > 20
        or reference_tone_hz is None
        or abs(reference_tone_readback_hz - reference_tone_hz) > 20
    ):
        raise RuntimeError("source DDS frequency readback violates the plan")
    return {
        "source_mode": source_mode,
        "tx_channel": tx_channel,
        "tx_gain_db": gains,
        "dds_scales": scales,
        "dds_frequencies_hz": frequencies,
        "dds_phases_millidegrees": phases,
        "dds_enabled": enabled,
        "tx_lo_readback_hz": int(device.tx_lo),
        "target_tone_requested_hz": TONE_OFFSET_HZ,
        "target_tone_readback_hz": target_tone_readback_hz,
        "reference_tone_requested_hz": reference_tone_hz,
        "reference_tone_readback_hz": reference_tone_readback_hz,
        "frequency_difference_readback_hz": (
            target_tone_readback_hz - reference_tone_readback_hz
        ),
    }


def _time_block(block: SampleBlockV2) -> SampleTimeBlock:
    if (
        block.sample_time_realtime_start_ns is None
        or block.sample_time_realtime_end_ns is None
        or block.sample_time_uncertainty_ns is None
    ):
        raise RuntimeError("ABI-2 block lacks realtime sample anchors")
    return SampleTimeBlock(
        first_sample_sequence=block.first_sample_sequence,
        sample_count=block.sample_count,
        realtime_start_ns=block.sample_time_realtime_start_ns,
        realtime_end_ns=block.sample_time_realtime_end_ns,
        uncertainty_ns=block.sample_time_uncertainty_ns,
    )


def _validate_block(block: SampleBlockV2, previous: SampleBlockV2 | None) -> None:
    if block.metadata_abi != 2 or block.samples.shape != (2, FRAME_SAMPLES):
        raise RuntimeError("capture returned a non-ABI-2 or partial dual-RX block")
    if block.missing_samples_before != 0 or block.overflow_observed:
        raise RuntimeError(
            "capture metadata reports loss: "
            f"buffer={block.buffer_sequence}, missing={block.missing_samples_before}, "
            f"flags=0x{block.metadata_flags:x}, overflow={block.overflow_observed}"
        )
    if previous is None:
        if block.buffer_sequence != 0:
            raise RuntimeError("capture did not start at buffer sequence zero")
        return
    if (
        block.stream_id != previous.stream_id
        or block.buffer_sequence != previous.buffer_sequence + 1
        or block.first_sample_sequence != previous.last_sample_sequence_exclusive
    ):
        raise RuntimeError("capture lost buffer or FPGA sample-sequence continuity")


def _selector_worker(
    *,
    controller: OpenOcdBench,
    state_codes: dict[str, int],
    ports: tuple[str, ...],
    scan_pairs: int,
    dwell_ms: int,
    initial_acknowledged_sequence: int,
    events: list[SelectorEvent],
    result: dict[str, Any],
    completed: threading.Event,
    cancel: threading.Event,
) -> None:
    acknowledged = initial_acknowledged_sequence

    def apply(state: str) -> None:
        nonlocal acknowledged
        timed = controller.request_from_known_sequence_timed(
            state_codes[state],
            0 if state == "ALL_OFF" else SELECTOR_LEASE_MS,
            acknowledged_sequence=acknowledged,
            settle_ms=SELECTOR_COMMAND_SETTLE_MS,
        )
        acknowledged = timed.status.acknowledged_sequence
        events.append(
            SelectorEvent(
                state,
                timed.command_write_realtime_start_ns,
                timed.command_write_realtime_end_ns,
            )
        )

    try:
        apply("ALL_OFF")
        if cancel.wait(TAIL_MS / 1000.0):
            return
        for _ in range(scan_pairs):
            for state in (*ports, *reversed(ports)):
                if cancel.is_set():
                    return
                apply(state)
                if cancel.wait(dwell_ms / 1000.0):
                    return
        apply("ALL_OFF")
        result["completed_realtime_ns"] = time.time_ns()
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        result["exception"] = error
    finally:
        completed.set()


def _aggregate_complex(
    values: list[tuple[complex, float, int]],
) -> tuple[complex, float, float]:
    if not values:
        raise ValueError("cannot aggregate an empty port")
    weights = np.asarray(
        [max(coherence, 1e-6) ** 2 * count for _, coherence, count in values],
        dtype=np.float64,
    )
    vector = np.asarray([value for value, _, _ in values], dtype=np.complex128)
    mean = complex(np.sum(weights * vector) / np.sum(weights))
    residual = np.angle(vector / mean, deg=True)
    scatter = float(np.sqrt(np.average(residual**2, weights=weights)))
    return mean, scatter, float(min(coherence for _, coherence, _ in values))


def _coherent_estimator_snr_db(coherence: float, sample_count: int) -> float:
    fraction = min(max(coherence**2, np.finfo(float).eps), 1.0 - np.finfo(float).eps)
    integrated_snr = sample_count * fraction / (1.0 - fraction)
    return float(10.0 * np.log10(integrated_snr))


def _analyze(
    *,
    rx1_path: Path,
    rx2_path: Path,
    sample_count: int,
    blocks: tuple[SampleTimeBlock, ...],
    events: tuple[SelectorEvent, ...],
    ports: tuple[str, ...],
    tx_channel: int,
    frequency_hz: int,
    profile: Any,
    board_lut: BoardCalibrationLut,
    cross_frequency_difference_hz: float | None = None,
) -> dict[str, Any]:
    rx1 = np.memmap(rx1_path, mode="r", dtype=np.complex64, shape=(sample_count,))
    rx2 = np.memmap(rx2_path, mode="r", dtype=np.complex64, shape=(sample_count,))
    intervals = selector_dwell_intervals(
        blocks,
        events,
        admitted_ports=ports,
        settle_guard_ns=EDGE_GUARD_MS * 1_000_000,
    )
    admitted_peaks = [0.0, 0.0]
    admitted_full_scale_counts = [0, 0]
    for interval in intervals:
        for channel, stream in enumerate((rx1, rx2)):
            selected = stream[interval.start : interval.stop]
            admitted_peaks[channel] = max(
                admitted_peaks[channel],
                float(max(np.max(np.abs(selected.real)), np.max(np.abs(selected.imag)))),
            )
            admitted_full_scale_counts[channel] += int(
                np.count_nonzero(
                    (np.abs(selected.real) >= 2047.0) | (np.abs(selected.imag) >= 2047.0)
                )
            )
    by_port: dict[str, list[tuple[complex, float, int]]] = {port: [] for port in ports}
    dwell_records: list[dict[str, Any]] = []
    coherent_snrs_db: list[float] = []
    if tx_channel == 0:
        tone = estimate_continuous_tone_frequency(
            rx1,
            intervals,
            sample_rate_hz=SAMPLE_RATE_HZ,
            nominal_tone_offset_hz=TONE_OFFSET_HZ,
        )
        for interval in intervals:
            estimate = estimate_same_emitter_transfer(
                rx1[interval.start : interval.stop],
                rx2[interval.start : interval.stop],
            )
            by_port[interval.port].append(
                (estimate.value, estimate.coherence, estimate.sample_count)
            )
            coherent_snr_db = estimate.residual_snr_db + 10.0 * math.log10(
                estimate.sample_count
            )
            coherent_snrs_db.append(coherent_snr_db)
            dwell_records.append(
                {
                    "port": interval.port,
                    "start": interval.start,
                    "stop": interval.stop,
                    "value_real": estimate.value.real,
                    "value_imag": estimate.value.imag,
                    "coherence": estimate.coherence,
                    "residual_snr_db": estimate.residual_snr_db,
                    "coherent_estimator_snr_db": coherent_snr_db,
                    "sample_count": estimate.sample_count,
                }
            )
        estimator = "simultaneous_rx2_over_rx1_least_squares"
        same_emitter_reference_valid = True
        coherent_pilot_reference_valid = True
        frequency_record: dict[str, Any] = {"kind": "single_tone", **asdict(tone)}
    else:
        if cross_frequency_difference_hz is None:
            raise ValueError("TX2 analysis requires the coherent-pilot frequency difference")
        cross_frequency = estimate_cross_frequency_transfers(
            rx1,
            rx2,
            intervals,
            first_sample_sequence=blocks[0].first_sample_sequence,
            sample_rate_hz=SAMPLE_RATE_HZ,
            nominal_frequency_difference_hz=cross_frequency_difference_hz,
            edge_discard_samples=EDGE_GUARD_MS * SAMPLE_RATE_HZ // 1000,
        )
        for interval, estimate in zip(intervals, cross_frequency.phasors, strict=True):
            by_port[interval.port].append(
                (estimate.value, estimate.coherence, estimate.sample_count)
            )
            coherent_snr_db = _coherent_estimator_snr_db(
                estimate.coherence, estimate.sample_count
            )
            coherent_snrs_db.append(coherent_snr_db)
            dwell_records.append(
                {
                    "port": interval.port,
                    "start": interval.start,
                    "stop": interval.stop,
                    "center_sample_sequence": estimate.center_sample_sequence,
                    "value_real": estimate.value.real,
                    "value_imag": estimate.value.imag,
                    "coherence": estimate.coherence,
                    "coherent_estimator_snr_db": coherent_snr_db,
                    "sample_count": estimate.sample_count,
                }
            )
        estimator = "tx2_over_frequency_separated_coherent_tx1_pilot"
        same_emitter_reference_valid = False
        coherent_pilot_reference_valid = True
        frequency_record = {
            "kind": "cross_frequency",
            "frequency_difference_hz": cross_frequency.frequency_difference_hz,
            "frequency_error_hz": cross_frequency.frequency_error_hz,
            "search_objective": cross_frequency.search_objective,
            "bin_count": cross_frequency.bin_count,
        }

    port_values: list[complex] = []
    summaries: list[dict[str, Any]] = []
    minimum_coherence = 1.0
    maximum_repeat_scatter_deg = 0.0
    for port in ports:
        mean, scatter, coherence = _aggregate_complex(by_port[port])
        port_values.append(mean)
        minimum_coherence = min(minimum_coherence, coherence)
        maximum_repeat_scatter_deg = max(maximum_repeat_scatter_deg, scatter)
        summaries.append(
            {
                "port": port,
                "value_real": mean.real,
                "value_imag": mean.imag,
                "magnitude": abs(mean),
                "phase_deg": math.degrees(math.atan2(mean.imag, mean.real)),
                "minimum_dwell_coherence": coherence,
                "repeat_phase_rms_deg": scatter,
                "dwell_count": len(by_port[port]),
            }
        )

    result: dict[str, Any] = {
        "phase_estimator": estimator,
        "same_emitter_reference_valid": same_emitter_reference_valid,
        "coherent_pilot_reference_valid": coherent_pilot_reference_valid,
        "continuous_phase_valid": True,
        "tone_frequency": frequency_record,
        "intervals": [asdict(interval) for interval in intervals],
        "dwells": dwell_records,
        "ports": summaries,
        "minimum_dwell_coherence": minimum_coherence,
        "minimum_coherent_estimator_snr_db": min(coherent_snrs_db),
        "maximum_repeat_phase_rms_deg": maximum_repeat_scatter_deg,
        "admitted_peak_component_counts": admitted_peaks,
        "admitted_full_scale_sample_counts": admitted_full_scale_counts,
        "board_calibration": None,
        "ideal_far_field_bearing": None,
    }
    try:
        calibration = board_lut.evaluate(frequency_hz, ports)
    except ValueError as error:
        result["board_calibration"] = {
            "accepted": False,
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        return result

    raw_vector = np.asarray(port_values, dtype=np.complex128)
    corrected = raw_vector * calibration.coefficients
    bearings = np.arange(0.0, 360.0, 0.25)
    steering = far_field_steering(profile.geometry, frequency_hz, bearings)
    bearing = solve_bearing(
        corrected,
        steering,
        bearings,
        minimum_score=0.5,
        minimum_ambiguity_margin_db=1.0,
        maximum_residual_phase_rms_deg=45.0,
    )
    reasons = list(bearing.reasons)
    valid = bearing.valid
    if not calibration.interpolation_validated:
        reasons.append("board_interpolation_not_independently_validated")
        valid = False
    if tx_channel == 0 and tone.adjacent_product_coherence < 0.9:
        reasons.append("continuous_tone_coherence_below_gate")
        valid = False
    if tx_channel == 1 and cross_frequency.search_objective < 0.25:
        reasons.append("cross_frequency_objective_below_gate")
        valid = False
    if min(coherent_snrs_db) < 5.0:
        reasons.append("coherent_estimator_snr_below_gate")
        valid = False
    if maximum_repeat_scatter_deg > 20.0:
        reasons.append("repeat_phase_scatter_above_gate")
        valid = False
    if any(admitted_full_scale_counts):
        reasons.append("admitted_dwell_contains_full_scale_samples")
        valid = False
    result["board_calibration"] = {
        "accepted": True,
        "source": str(board_lut.source),
        "status": calibration.calibration_status,
        "exact_knot": calibration.exact_knot,
        "interpolation_validated": calibration.interpolation_validated,
        "corrected_real": corrected.real.tolist(),
        "corrected_imag": corrected.imag.tolist(),
    }
    result["ideal_far_field_bearing"] = {
        "valid": valid,
        "reasons": reasons,
        "bearing_deg_clockwise_from_forward": bearing.bearing_deg,
        "score": bearing.score,
        "second_bearing_deg_clockwise_from_forward": bearing.second_bearing_deg,
        "ambiguity_margin_db": bearing.ambiguity_margin_db,
        "residual_phase_rms_deg": bearing.residual_phase_rms_deg,
        "geometry_id": profile.geometry.geometry_id,
        "model_scope": "diagnostic_only_until_surveyed_or_empirical_manifold_closes",
        "bearing_grid_deg": bearing.bearings_deg.tolist(),
        "likelihood": bearing.likelihood.tolist(),
    }
    return result


def _assert_arguments(
    args: argparse.Namespace,
    profile: Any,
    board_lut: BoardCalibrationLut,
) -> None:
    if not args.acknowledge_ota_authorization:
        raise SystemExit("RF execution requires --acknowledge-ota-authorization")
    if args.scan_pairs < 1 or args.scan_pairs > 5:
        raise SystemExit("scan pairs must be 1..5")
    if args.dwell_ms < 100 or args.dwell_ms > 1000:
        raise SystemExit("dwell must be 100..1000 ms for the host-controlled screen")
    if args.tx_gain_db < -80.0 or args.tx_gain_db > MAX_TX_GAIN_DB:
        raise SystemExit(f"TX gain must be -80..{MAX_TX_GAIN_DB:g} dB")
    if not profile.current_fixture_ready:
        raise SystemExit(
            "tracking acquisition is blocked for the current fixture: "
            f"{profile.current_fixture_blocker}"
        )
    dense_campaign = bool(getattr(args, "dense_5g8_campaign", False))
    allowed = {*profile.primary_centres_hz, *profile.blind_frequency_holdouts_hz}
    if dense_campaign:
        if (
            profile.profile_id != "ism5800-c6-v1"
            or not DENSE_5G8_MIN_CENTER_HZ
            <= args.frequency_hz
            <= DENSE_5G8_MAX_CENTER_HZ
            or args.frequency_hz % DENSE_5G8_LATTICE_HZ != 0
        ):
            raise SystemExit(
                "dense 5.8 GHz centre must lie on the 100 kHz lattice from "
                "5.726 through 5.874 GHz"
            )
    elif args.frequency_hz not in allowed:
        raise SystemExit("frequency is not a declared primary centre or blind holdout")
    try:
        board_lut.evaluate(args.frequency_hz, profile.geometry.ports)
    except ValueError as error:
        raise SystemExit(f"tracking acquisition is blocked by PCB calibration: {error}") from error


def main() -> int:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)
    args = _parser().parse_args()
    profiles = load_ism_band_profiles(args.frequency_plan)
    if args.profile_id not in profiles:
        raise SystemExit(f"unknown ISM profile: {args.profile_id}")
    profile = profiles[args.profile_id]
    board_lut = BoardCalibrationLut.load(args.board_lut)
    _assert_arguments(args, profile, board_lut)
    verify_metadata_runtime(2, expected_firmware_version=V7_FIRMWARE_VERSION)

    selector_profile = load_profile(args.selector_profile)
    state_codes = {state.name: state.gpio_code for state in selector_profile.states}
    state_codes["ALL_OFF"] = selector_profile.all_off_code
    if any(port not in state_codes for port in profile.geometry.ports):
        raise SystemExit("selector profile does not define every array port")
    manifest = BenchManifest.load(args.manifest)
    controller = OpenOcdBench(manifest, args.openocd_config)
    run_id = datetime.now(UTC).strftime("tracking-%Y%m%dT%H%M%S.%fZ")
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    lock_path = args.output_root / ".hardware.lock"
    rx1_path = run_dir / "rx1.cf32"
    rx2_path = run_dir / "rx2.cf32"
    record: dict[str, Any] = {
        "schema": 1,
        "evidence_kind": "continuous_sample_timestamped_switched_array_screen",
        "status": "running",
        "run_id": run_id,
        "started_at": _now(),
        "completed_at": None,
        "configuration": {
            "profile_id": args.profile_id,
            "frequency_hz": args.frequency_hz,
            "tx_channel": args.tx_channel,
            "tx_port": f"TX{args.tx_channel + 1}",
            "scan_pairs": args.scan_pairs,
            "dwell_ms": args.dwell_ms,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "frame_samples": FRAME_SAMPLES,
            "receiver_gain_db": RX_GAIN_DB,
            "tx_gain_db": args.tx_gain_db,
            "dds_scale": DDS_SCALE,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "tx2_reference_tone_offset_hz": (
                TX2_REFERENCE_TONE_OFFSET_HZ if args.tx_channel == 1 else None
            ),
            "ports": list(profile.geometry.ports),
            "dense_5g8_campaign": args.dense_5g8_campaign,
        },
        "identities": {
            "receiver_serial": RECEIVER_SERIAL,
            "receiver_uri": args.receiver_uri,
            "source_serial": SOURCE_SERIAL,
            "source_uri": args.source_uri,
            "board_id": BOARD_ID,
            "stlink_serial": STLINK_SERIAL,
        },
        "safety": {
            "ota_authorization_acknowledged": True,
            "maximum_tx_gain_db": MAX_TX_GAIN_DB,
            "source_final_mute": None,
            "selector_final_all_off": None,
        },
        "capture": None,
        "analysis": None,
        "error": None,
    }
    radio: IioRadioDevice | None = None
    session: Any | None = None
    source: Any | None = None
    worker: threading.Thread | None = None
    events: list[SelectorEvent] = []
    worker_result: dict[str, Any] = {}
    completed = threading.Event()
    cancel = threading.Event()
    time_blocks: list[SampleTimeBlock] = []
    previous: SampleBlockV2 | None = None
    total_samples = 0
    peak_components = [0.0, 0.0]
    try:
        with _exclusive_lock(lock_path):
            initial_status = controller.status()
            if initial_status.applied_code != state_codes["ALL_OFF"]:
                initial_status = controller.request(
                    state_codes["ALL_OFF"], 0, wait_until_applied=False
                )
            radio = IioRadioDevice(
                args.receiver_uri,
                serial=RECEIVER_SERIAL,
                expected_metadata_abi=2,
                expected_firmware_version=V7_FIRMWARE_VERSION,
            )
            radio.open()
            settings = RadioSettings(
                center_frequency_hz=args.frequency_hz,
                sample_rate_hz=SAMPLE_RATE_HZ,
                bandwidth_hz=BANDWIDTH_HZ,
                gain_mode=GainMode.MANUAL,
                gain_db=RX_GAIN_DB,
                channels=(0, 1),
            )
            actual_settings = radio.apply_settings(settings)
            if (
                abs(actual_settings.center_frequency_hz - args.frequency_hz) > 5.0
                or actual_settings.sample_rate_hz != SAMPLE_RATE_HZ
                or actual_settings.channels != (0, 1)
            ):
                raise RuntimeError("receiver settings readback differs from the plan")
            adi = importlib.import_module("adi")
            source = adi.ad9361(uri=args.source_uri)
            source_facts = _source_facts(source, uri=args.source_uri)
            _mute_readback(source)
            session = radio.begin_metadata_capture(
                FRAME_SAMPLES,
                kernel_buffers=KERNEL_BUFFERS,
                tandem_request=TandemSessionRequestV1(
                    mode=TandemMode.HOLD,
                    initial_gain_db=RX_GAIN_DB,
                ),
            )
            source_readback = _enable_source(
                source,
                frequency_hz=args.frequency_hz,
                tx_channel=args.tx_channel,
                tx_gain_db=args.tx_gain_db,
            )
            worker = threading.Thread(
                target=_selector_worker,
                kwargs={
                    "controller": controller,
                    "state_codes": state_codes,
                    "ports": profile.geometry.ports,
                    "scan_pairs": args.scan_pairs,
                    "dwell_ms": args.dwell_ms,
                    "initial_acknowledged_sequence": initial_status.acknowledged_sequence,
                    "events": events,
                    "result": worker_result,
                    "completed": completed,
                    "cancel": cancel,
                },
                name="selector-schedule",
                daemon=True,
            )
            worker.start()
            with rx1_path.open("xb") as rx1_stream, rx2_path.open("xb") as rx2_stream:
                while True:
                    block = session.read_block()
                    _validate_block(block, previous)
                    np.asarray(block.samples[0], dtype=np.complex64).tofile(rx1_stream)
                    np.asarray(block.samples[1], dtype=np.complex64).tofile(rx2_stream)
                    for channel in (0, 1):
                        values = block.samples[channel]
                        peak_components[channel] = max(
                            peak_components[channel],
                            float(max(np.max(np.abs(values.real)), np.max(np.abs(values.imag)))),
                        )
                    time_blocks.append(_time_block(block))
                    total_samples += block.sample_count
                    previous = block
                    completed_ns = worker_result.get("completed_realtime_ns")
                    if completed.is_set() and "exception" in worker_result:
                        break
                    if (
                        completed.is_set()
                        and isinstance(completed_ns, int)
                        and block.sample_time_realtime_end_ns is not None
                        and block.sample_time_realtime_end_ns
                        >= completed_ns + TAIL_MS * 1_000_000
                    ):
                        break
                    if len(time_blocks) >= MAX_FRAMES:
                        raise RuntimeError("selector schedule exceeded the bounded capture")
                rx1_stream.flush()
                rx2_stream.flush()
                os.fsync(rx1_stream.fileno())
                os.fsync(rx2_stream.fileno())
            worker.join(timeout=5.0)
            if worker.is_alive():
                raise RuntimeError("selector worker did not terminate")
            if "exception" in worker_result:
                raise RuntimeError(f"selector worker failed: {worker_result['error']}")
            analysis = _analyze(
                rx1_path=rx1_path,
                rx2_path=rx2_path,
                sample_count=total_samples,
                blocks=tuple(time_blocks),
                events=tuple(events),
                ports=profile.geometry.ports,
                tx_channel=args.tx_channel,
                frequency_hz=args.frequency_hz,
                profile=profile,
                board_lut=board_lut,
                cross_frequency_difference_hz=float(
                    source_readback["frequency_difference_readback_hz"]
                ),
            )
            record["capture"] = {
                "receiver_identity": radio.identity.model_dump(mode="json"),
                "receiver_settings": actual_settings.model_dump(mode="json"),
                "source_facts": source_facts,
                "source_readback": source_readback,
                "frame_count": len(time_blocks),
                "total_samples_per_channel": total_samples,
                "peak_component_counts": peak_components,
                "stream_id": previous.stream_id if previous is not None else None,
                "first_sample_sequence": time_blocks[0].first_sample_sequence,
                "last_sample_sequence_exclusive": (
                    time_blocks[-1].last_sample_sequence_exclusive
                ),
                "timeline": [asdict(block) for block in time_blocks],
                "selector_events": [asdict(event) for event in events],
                "raw": {
                    "dtype": "complex64",
                    "rx1_path": str(rx1_path),
                    "rx1_sha256": _sha256(rx1_path),
                    "rx2_path": str(rx2_path),
                    "rx2_sha256": _sha256(rx2_path),
                },
            }
            record["analysis"] = analysis
            record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        cancel.set()
        if source is not None:
            try:
                record["safety"]["source_final_mute"] = _mute_readback(source)
            except BaseException as cleanup_error:
                record["safety"]["source_final_mute"] = {
                    "passed": False,
                    "error": {
                        "type": type(cleanup_error).__name__,
                        "message": str(cleanup_error),
                    },
                }
                record["status"] = "failed"
            try:
                _release_device(source)
            except BaseException as cleanup_error:
                record["safety"]["source_release_error"] = {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                }
                record["status"] = "failed"
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)
            if worker.is_alive():
                record["safety"]["selector_worker_shutdown"] = "timed_out"
                record["status"] = "failed"
        if radio is not None:
            try:
                if session is not None:
                    session.close()
            except BaseException as cleanup_error:
                record["safety"]["receiver_session_close_error"] = {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                }
                record["status"] = "failed"
            try:
                radio.close()
            except BaseException as cleanup_error:
                record["safety"]["receiver_close_error"] = {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                }
                record["status"] = "failed"
        try:
            final_status = controller.request(
                state_codes["ALL_OFF"], 0, wait_until_applied=False
            )
            record["safety"]["selector_final_all_off"] = final_status.as_dict()
        except BaseException as cleanup_error:
            record["safety"]["selector_final_all_off"] = {
                "error": {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                }
            }
        if record["capture"] is None and time_blocks:
            record["partial_capture"] = {
                "frame_count": len(time_blocks),
                "total_samples_per_channel": total_samples,
                "peak_component_counts": peak_components,
                "timeline": [asdict(block) for block in time_blocks],
                "selector_events": [asdict(event) for event in events],
                "raw": {
                    "dtype": "complex64",
                    "rx1_path": str(rx1_path),
                    "rx1_sha256": _sha256(rx1_path) if rx1_path.is_file() else None,
                    "rx2_path": str(rx2_path),
                    "rx2_sha256": _sha256(rx2_path) if rx2_path.is_file() else None,
                },
            }
        record["completed_at"] = _now()
        _write_json_atomic(run_dir / "run.json", record)
        print(f"run_dir={run_dir}", flush=True)
        analysis_summary: dict[str, Any] | None = None
        if isinstance(record["analysis"], dict):
            analysis_summary = {
                "minimum_coherent_estimator_snr_db": record["analysis"].get(
                    "minimum_coherent_estimator_snr_db"
                ),
                "maximum_repeat_phase_rms_deg": record["analysis"].get(
                    "maximum_repeat_phase_rms_deg"
                ),
                "ideal_far_field_bearing": (
                    dict(record["analysis"]["ideal_far_field_bearing"])
                    if isinstance(record["analysis"].get("ideal_far_field_bearing"), dict)
                    else None
                ),
            }
            if isinstance(analysis_summary["ideal_far_field_bearing"], dict):
                analysis_summary["ideal_far_field_bearing"].pop("bearing_grid_deg", None)
                analysis_summary["ideal_far_field_bearing"].pop("likelihood", None)
        print(json.dumps({"status": record["status"], "analysis": analysis_summary}))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
