#!/usr/bin/env python3
"""Capture two independent, muted-between, 2 MS/s Hexcal timing artifacts."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_SMATEWAY_SOURCE = Path(__file__).resolve().parents[1] / "src"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX or str(_SMATEWAY_SOURCE) not in sys.path
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned hexcal Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

from pluto_plus.artifacts import (
    CaptureWriter,
    complex_to_ci16,
    data_path,
    load_metadata,
    verify_artifact,
)
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import (
    SafeDdsTonePlan,
    SampleBlockV2,
    capture_continuous_safe_dds_tone,
)
from pluto_plus.models import GainMode, RadioSettings

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    canonical_json_sha256,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.hexcal_gain import (
    EXPERIMENTAL_5G8_HIGH_RX_STIMULUS_PROTOCOL_ID,
    EXPERIMENTAL_5G8_STIMULUS_PROTOCOL_ID,
    QUALIFICATION_SOURCE_FILES,
    STIMULUS_PROTOCOL_ID,
    HexcalStimulusProtocol,
    HexcalStimulusQualification,
    load_hexcal_stimulus_qualification,
    stimulus_protocol,
)
from smateway.hexcal_timing import BANDWIDTH_HZ, SAMPLE_RATE_HZ
from smateway.rf_policy import classify_fast20_center_frequency

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
TONE_OFFSET_HZ = 100_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 9
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
REPLICATE_COUNT = 2
TX_CHANNEL = 0
CAPTURE_RECORD_NAME = "hexcal-timing-capture.json"
SOURCE_FILES = (
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.1-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.2-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.3-experimental-5g8-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.4-experimental-5g8-high-rx-stimulus.json",
    "scripts/qualify_hexcal_rx_gain.py",
    "src/smateway/hexcal_timing.py",
    "src/smateway/hexcal_gain.py",
    "scripts/capture_hexcal_timing.py",
    "scripts/analyze_hexcal_timing.py",
    "profiles/hexcal-v1/control_profile.json",
    "pyproject.toml",
    "uv.lock",
)


class TimingCaptureFailure(RuntimeError):
    """Capture failure carrying the immutable quarantine summary."""

    def __init__(self, message: str, quarantine: dict[str, Any]) -> None:
        super().__init__(message)
        self.quarantine = quarantine


class CooperativeTermination(RuntimeError):
    """SIGINT/SIGTERM/SIGHUP converted into an exception so RF cleanup runs."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _signal_handler(signum: int, _frame: object) -> None:
    name = signal.Signals(signum).name
    raise CooperativeTermination(f"received {name}; entering fail-muted cleanup")


def _install_cooperative_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)


@contextmanager
def _exclusive_bench_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"board bench lock is already held: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _exact_serial_mute(serial: str, purpose: str) -> dict[str, Any]:
    started = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started,
            "completed_at": _now(),
            "error": {"type": type(error).__name__, "message": str(error)},
        }
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": started,
        "completed_at": _now(),
        "error": None,
    }


def _mute_passed(attestation: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(attestation, dict)
        and attestation.get("purpose") == purpose
        and attestation.get("status") == "passed"
        and attestation.get("serial") == serial
        and attestation.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and attestation.get("error") is None
    )


def _canonical_sha256(document: object) -> str:
    wire = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(wire).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="exact usb: IIO URI")
    parser.add_argument("--center-frequency-hz", type=int, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--firmware-evidence", type=Path, required=True)
    parser.add_argument("--firmware-evidence-sha256", required=True)
    parser.add_argument("--protocol-v2", "--protocol-v21", "--protocol-v22", action="store_true")
    parser.add_argument("--protocol-v23-5g8", action="store_true")
    parser.add_argument("--protocol-v24-5g8", action="store_true")
    parser.add_argument("--stimulus-qualification", type=Path)
    parser.add_argument("--stimulus-qualification-sha256")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--allow-experimental-5g8", action="store_true")
    parser.add_argument(
        "--receiver-gain-db",
        type=int,
        choices=range(63),
        help="legacy v1 common RX1/RX2 gain; v2 derives this from its qualification",
    )
    parser.add_argument(
        "--tx-hardware-gain-db",
        type=float,
        help="legacy v1 TX1 gain; v2 derives this from its qualification",
    )
    parser.add_argument("--dds-scale", type=float, default=0.125)
    parser.add_argument(
        "--capture-root",
        type=Path,
        help="override the board-bound state directory (normally omitted)",
    )
    return parser


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_manifest(repository: Path, expected_commit: str) -> dict[str, Any]:
    head = _git(repository, "rev-parse", "HEAD")
    if head != expected_commit:
        raise RuntimeError("implementation HEAD differs from --source-commit")
    status = _git(repository, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError("timing capture refuses a dirty implementation source tree")
    attestation = attest_source_files_at_commit(
        repository,
        expected_commit=expected_commit,
        relative_paths=SOURCE_FILES,
    )
    return {
        "commit": expected_commit,
        "clean_worktree_verified": True,
        "files": attestation["files"],
    }


def _pair_plan_contract(
    *,
    run_id: str,
    board_id: str,
    source: dict[str, Any],
    pluto_plus_utils_source_attestation: dict[str, Any],
    pluto_plus_utils_source_attestation_sha256: str,
    profile: dict[str, Any],
    firmware: dict[str, Any],
    center_frequency_policy: str,
    plan: SafeDdsTonePlan,
    bench_lock_path: Path,
    stimulus_qualification: HexcalStimulusQualification | None = None,
    stimulus_protocol_contract: HexcalStimulusProtocol | None = None,
) -> dict[str, Any]:
    protocol_v2 = stimulus_protocol_contract is not None
    if protocol_v2 != (stimulus_qualification is not None):
        raise ValueError("stimulus qualification and protocol contract must be paired")
    contract = {
        "schema": 1,
        "plan_kind": (
            stimulus_protocol_contract.timing_plan_kind
            if protocol_v2
            else "hexcal_v1_rf_timing_two_replicates"
        ),
        "protocol_id": (
            stimulus_protocol_contract.protocol_id if protocol_v2 else "hexcal-v1"
        ),
        "run_id": run_id,
        "board_id": board_id,
        "serial": plan.serial,
        "uri": plan.uri,
        "source": source,
        "pluto_plus_utils_source_attestation": (pluto_plus_utils_source_attestation),
        "pluto_plus_utils_source_attestation_sha256": (pluto_plus_utils_source_attestation_sha256),
        "profile": profile,
        "firmware": firmware,
        "center_frequency_policy": center_frequency_policy,
        "capture": {
            "replicate_count": REPLICATE_COUNT,
            "fresh_stream_per_replicate": True,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "total_samples": TOTAL_SAMPLES,
            "kernel_buffers": KERNEL_BUFFERS,
            "receiver_channels": [0, 1],
            "metadata_abi": 2,
            "receiver_gain_scope": ("timing_qualification_only" if protocol_v2 else "legacy_v1"),
        },
        "stimulus": {
            "tx_channel": TX_CHANNEL,
            "tx_port": "TX1",
            "tx2_required_muted": True,
            "center_frequency_hz": plan.center_frequency_hz,
            "tone_offset_hz_requested": plan.tone_frequency_hz,
            "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
            "dds_scale_requested": plan.dds_scale,
            "receiver_gain_db": plan.receiver_gain_db,
            "calibration_receiver_gain_db": (
                stimulus_qualification.fixed_receiver_gain_db
                if stimulus_qualification is not None
                else plan.receiver_gain_db
            ),
            "source_peak_output_bound_dbm": plan.source_peak_output_bound_dbm,
            "load_input_limit_dbm": plan.load_input_limit_dbm,
            "path_attenuation_before_load_db": plan.path_attenuation_before_load_db,
            "required_margin_db": plan.required_margin_db,
            "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
        },
        "safety": {
            "bench_lock_path": str(bench_lock_path),
            "ram_only_until_exact_serial_mute_passes": True,
            "per_replicate_exact_serial_mute_required": True,
            "final_exact_serial_mute_required": True,
            "automatic_retry_count": 0,
            "cooperative_signals": ["SIGINT", "SIGTERM", "SIGHUP"],
            "sigkill_cannot_be_intercepted": True,
        },
    }
    if stimulus_qualification is not None:
        contract["stimulus_qualification"] = stimulus_qualification.as_dict()
    return contract


def _metadata_path(artifact_root: Path, artifact_id: str) -> Path:
    return artifact_root / f"{artifact_id}.sigmf-meta"


def _block_ledger(blocks: list[SampleBlockV2]) -> dict[str, Any] | None:
    if not blocks:
        return None
    records: list[dict[str, int]] = []
    sample_start = 0
    for block in blocks:
        record = {
            "sample_start": sample_start,
            "sample_count": block.sample_count,
            "utc_ns": block.utc_ns,
            "metadata_abi": block.metadata_abi,
            "stream_id": block.stream_id,
            "buffer_sequence": block.buffer_sequence,
            "first_sample_sequence": block.first_sample_sequence,
            "last_sample_sequence_exclusive": block.last_sample_sequence_exclusive,
            "metadata_flags": block.metadata_flags,
            "missing_samples_before": block.missing_samples_before,
        }
        timing = {
            "sample_time_realtime_start_ns": block.sample_time_realtime_start_ns,
            "sample_time_realtime_end_ns": block.sample_time_realtime_end_ns,
            "sample_time_monotonic_start_ns": block.sample_time_monotonic_start_ns,
            "sample_time_monotonic_end_ns": block.sample_time_monotonic_end_ns,
            "sample_time_uncertainty_ns": block.sample_time_uncertainty_ns,
        }
        record.update({key: value for key, value in timing.items() if value is not None})
        records.append(record)
        sample_start += block.sample_count
    return {
        "schema_version": 1,
        "metadata_abi": blocks[0].metadata_abi,
        "stream_id": blocks[0].stream_id,
        "block_count": len(blocks),
        "total_samples": sample_start,
        "first_sample_sequence": blocks[0].first_sample_sequence,
        "last_sample_sequence_exclusive": blocks[-1].last_sample_sequence_exclusive,
        "sample_sequence_span": (
            blocks[-1].last_sample_sequence_exclusive - blocks[0].first_sample_sequence
        ),
        "blocks": records,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _persist_memory_quarantine(
    capture_root: Path,
    *,
    blocks: list[SampleBlockV2],
    error: BaseException,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Persist a failed in-memory fragment only after the RF helper has returned."""

    quarantine_id = f"hexcal-timing-{uuid.uuid4().hex}.failed"
    failed_root = capture_root / ".failed"
    temporary = failed_root / f".{quarantine_id}.partial"
    destination = failed_root / quarantine_id
    failed_root.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    data_file = temporary / f"{quarantine_id}.sigmf-data"
    digest = hashlib.sha256()
    with data_file.open("xb") as stream:
        for block in blocks:
            wire = complex_to_ci16(block.samples).tobytes(order="C")
            stream.write(wire)
            digest.update(wire)
        stream.flush()
        os.fsync(stream.fileno())
    ledger = _block_ledger(blocks)
    sample_count = sum(block.sample_count for block in blocks)
    metadata: dict[str, Any] = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": SAMPLE_RATE_HZ,
            "core:num_channels": 2,
            "pluto:artifact_id": quarantine_id,
            "pluto:sha256": digest.hexdigest(),
        },
        "pluto:capture": {
            "sample_count": sample_count,
            "receiver_count": 2,
            "incomplete": True,
        },
        "smateway:quarantine": {
            "accepted": False,
            "may_be_used_for_qualification": False,
            "automatic_retry_fragment": False,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context,
        },
    }
    if ledger is not None:
        metadata["pluto:continuity"] = ledger
    metadata_file = temporary / f"{quarantine_id}.sigmf-meta"
    write_json_atomic(metadata_file, metadata)
    failure = {
        "schema": 1,
        "failure_kind": "hexcal_v1_timing_capture_quarantine",
        "artifact_id": quarantine_id,
        "accepted": False,
        "automatic_retry_attempted": False,
        "error": {"type": type(error).__name__, "message": str(error)},
        "retained_frame_count": len(blocks),
        "retained_sample_count": sample_count,
        "files": [
            {
                "name": data_file.name,
                "sha256": sha256_path(data_file),
                "size_bytes": data_file.stat().st_size,
            },
            {
                "name": metadata_file.name,
                "sha256": sha256_path(metadata_file),
                "size_bytes": metadata_file.stat().st_size,
            },
        ],
        "created_at": datetime.now(UTC).isoformat(),
    }
    failure_file = temporary / "failure.json"
    write_json_atomic(failure_file, failure)
    _fsync_directory(temporary)
    os.replace(temporary, destination)
    _fsync_directory(failed_root)
    return {
        "artifact_id": quarantine_id,
        "path": str(destination),
        "accepted": False,
        "failure_record": str(destination / failure_file.name),
        "failure_record_sha256": sha256_path(destination / failure_file.name),
        "files": failure["files"],
    }


def _seal_existing_failed_directory(destination: Path, *, error: BaseException) -> dict[str, Any]:
    files = []
    for path in sorted(destination.iterdir()):
        if path.is_file() and path.name != "timing-quarantine.json":
            files.append(
                {
                    "name": path.name,
                    "sha256": sha256_path(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    record = {
        "schema": 1,
        "failure_kind": "hexcal_v1_timing_post_capture_quarantine",
        "artifact_id": destination.name,
        "accepted": False,
        "automatic_retry_attempted": False,
        "error": {"type": type(error).__name__, "message": str(error)},
        "files": files,
        "created_at": datetime.now(UTC).isoformat(),
    }
    record_path = destination / "timing-quarantine.json"
    write_json_atomic(record_path, record)
    return {
        "artifact_id": destination.name,
        "path": str(destination),
        "accepted": False,
        "failure_record": str(record_path),
        "failure_record_sha256": sha256_path(record_path),
        "files": files,
    }


def _active_tone_readback_hz(capture: Any) -> float:
    values = tuple(float(value) for value in capture.dds_frequency_readback_hz)
    if len(values) < 3:
        raise RuntimeError("DDS frequency readback does not contain TX1 I/Q channels")
    active = (abs(values[0]), abs(values[2]))
    frequency_tolerance_hz = math.ceil(SAMPLE_RATE_HZ / (1 << 16))
    if abs(active[0] - active[1]) > frequency_tolerance_hz:
        raise RuntimeError("TX1 I/Q DDS frequency readbacks disagree")
    return float(sum(active) / 2.0)


def _rf_readback_evidence(capture: Any, *, plan: SafeDdsTonePlan) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": TX_CHANNEL,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [
            capture.tx_gain_readback_db,
            -80.0,
        ],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_scale_requested": plan.dds_scale,
        "dds_scale_readback": list(capture.dds_scale_readback),
        "dds_enabled_readback": list(capture.dds_enabled_readback),
        "tone_frequency_hz_requested": plan.tone_frequency_hz,
        "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
        "active_dds_indices": [0, 2],
        "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
        "inactive_dds_rf_activity_contract": (
            "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
        ),
    }
    validate_tx1_rf_readback_evidence(
        evidence,
        planned_kernel_buffers=KERNEL_BUFFERS,
        planned_tx_gain_db=plan.tx_hardware_gain_db,
        planned_dds_scale=plan.dds_scale,
        planned_tone_hz=plan.tone_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return evidence


def _capture_one(
    *,
    replicate_index: int,
    run_id: str,
    capture_root: Path,
    plan: SafeDdsTonePlan,
    settings: RadioSettings,
    board_id: str,
    source_manifest: dict[str, Any],
    dependency_attestation: dict[str, Any],
    dependency_attestation_sha256: str,
    profile_document: dict[str, Any],
    firmware_document: dict[str, Any],
    center_frequency_policy: str,
    pair_plan_contract: dict[str, Any],
    pair_plan_contract_sha256: str,
) -> dict[str, Any]:
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        # This is the only refill callback action.  Conversion, validation, and
        # every disk write happen after the helper's finally block has muted TX.
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    context = {
        "run_id": run_id,
        "replicate_index": replicate_index,
        "board_id": board_id,
        "source": source_manifest,
        "pluto_plus_utils_source_attestation": dependency_attestation,
        "pluto_plus_utils_source_attestation_sha256": (dependency_attestation_sha256),
        "profile": profile_document,
        "firmware": firmware_document,
        "pair_plan_contract": pair_plan_contract,
        "pair_plan_contract_sha256": pair_plan_contract_sha256,
        "plan": {
            "uri": plan.uri,
            "serial": plan.serial,
            "center_frequency_hz": plan.center_frequency_hz,
            "sample_rate_hz": plan.sample_rate_hz,
            "bandwidth_hz": plan.bandwidth_hz,
            "tone_frequency_hz": plan.tone_frequency_hz,
            "tx_channel": plan.tx_channel,
            "tx_hardware_gain_db": plan.tx_hardware_gain_db,
            "dds_scale": plan.dds_scale,
            "receiver_gain_db": plan.receiver_gain_db,
            "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
        },
    }
    try:
        capture = capture_continuous_safe_dds_tone(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        emergency_mute = _exact_serial_mute(plan.serial, "emergency_after_capture_failure")
        context["emergency_exact_serial_mute"] = emergency_mute
        quarantine = _persist_memory_quarantine(
            capture_root, blocks=retained, error=error, context=context
        )
        retained.clear()
        raise TimingCaptureFailure(str(error), quarantine) from error

    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        post_helper_mute = _exact_serial_mute(plan.serial, f"post_replicate_{replicate_index}")
        context["post_helper_exact_serial_mute"] = post_helper_mute
        if not _mute_passed(
            post_helper_mute,
            serial=plan.serial,
            purpose=f"post_replicate_{replicate_index}",
        ):
            raise RuntimeError("post-helper exact-serial mute attestation failed")
        if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
            raise RuntimeError("capture identity differs from the exact selected USB target")
        if capture.settings != settings:
            raise RuntimeError("capture setting readback differs from the exact timing plan")
        if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
            raise RuntimeError("capture is not exactly nine 100k-sample frames")
        if capture.kernel_buffers != KERNEL_BUFFERS:
            raise RuntimeError("kernel buffer readback differs from the required value eight")
        if len(retained) != FRAME_COUNT:
            raise RuntimeError("not every validated frame was retained in memory")
        if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in retained):
            raise RuntimeError("retained capture is not canonical dual-RX 100k-frame data")
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        if not headroom.passed:
            raise RuntimeError("ADC headroom admission failed before artifact acceptance")
        tone_readback_hz = _active_tone_readback_hz(capture)
        rf_readback_evidence = _rf_readback_evidence(capture, plan=plan)
        rf_readback_evidence_sha256 = canonical_json_sha256(rf_readback_evidence)

        writer = CaptureWriter(
            capture_root,
            radio=capture.identity,
            settings=settings,
            label=(
                f"{pair_plan_contract['protocol_id']} RF timing replicate {replicate_index}/2 "
                f"TX1 {plan.center_frequency_hz}Hz 2MS/s 450ms"
            ),
        )
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise RuntimeError("finalized timing SigMF data failed SHA-256 verification")
        artifact_root = Path(artifact.path)
        sigmf_data = data_path(artifact)
        sigmf_meta = _metadata_path(artifact_root, artifact.artifact_id)
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        document = {
            "schema": 1,
            "capture_kind": (
                stimulus_protocol(str(pair_plan_contract["protocol_id"])).timing_capture_kind
                if pair_plan_contract["protocol_id"] != "hexcal-v1"
                else "hexcal_v1_rf_timing_2msps_tx1"
            ),
            "run_id": run_id,
            "replicate_index": replicate_index,
            "required_replicate_count": REPLICATE_COUNT,
            "accepted": True,
            "automatic_retry_count": 0,
            "accepted_retry_fragment": False,
            "pair_plan_contract": pair_plan_contract,
            "pair_plan_contract_sha256": pair_plan_contract_sha256,
            "source": source_manifest,
            "pluto_plus_utils_source_attestation": dependency_attestation,
            "pluto_plus_utils_source_attestation_sha256": (dependency_attestation_sha256),
            "source_profile": profile_document,
            "firmware_evidence": firmware_document,
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": {
                "data_path": str(sigmf_data),
                "data_sha256": sha256_path(sigmf_data),
                "data_size_bytes": sigmf_data.stat().st_size,
                "metadata_path": str(sigmf_meta),
                "metadata_sha256": sha256_path(sigmf_meta),
                "metadata_size_bytes": sigmf_meta.stat().st_size,
            },
            "capture": {
                "board_id": board_id,
                "serial": plan.serial,
                "uri": plan.uri,
                "tx_channel": TX_CHANNEL,
                "tx_port": "TX1",
                "center_frequency_hz": plan.center_frequency_hz,
                "center_frequency_policy": center_frequency_policy,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FRAME_COUNT,
                "sample_count": capture.sample_count,
                "duration_s": capture.duration_s,
                "kernel_buffers": capture.kernel_buffers,
                "receiver_gain_db": plan.receiver_gain_db,
                "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
                "tx_gain_readback_db": capture.tx_gain_readback_db,
                "dds_scale_requested": plan.dds_scale,
                "dds_scale_readback": list(capture.dds_scale_readback),
                "dds_enabled_readback": list(capture.dds_enabled_readback),
                "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
                "rf_readback_evidence": rf_readback_evidence,
                "rf_readback_evidence_sha256": rf_readback_evidence_sha256,
                "tone_offset_hz_requested": TONE_OFFSET_HZ,
                "tone_offset_readback_hz": tone_readback_hz,
                "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
                "metadata_abi": capture.frames[0].metadata_abi,
                "stream_id": capture.frames[0].stream_id,
                "first_buffer_sequence": capture.frames[0].buffer_sequence,
                "last_buffer_sequence": capture.frames[-1].buffer_sequence,
                "first_sample_sequence": capture.frames[0].first_sample_sequence,
                "last_sample_sequence_exclusive": (
                    capture.frames[-1].last_sample_sequence_exclusive
                ),
                "adc_headroom_admission": asdict(headroom),
                "tx_readback_contract": {
                    "selected_tx_gain_readback_db": capture.tx_gain_readback_db,
                    "unselected_tx2_gain_readback_db_attested_by_helper": -80.0,
                    "active_dds_indices": [0, 2],
                    "inactive_dds_scales_required_zero": True,
                    "tx2_never_enabled": True,
                },
            },
            "continuity_audit": continuity,
            "analysis_status": "immutable_artifact_verified_unanalyzed",
            "capture_safety": {
                "refill_callback_action": "copy_to_ram_only",
                "disk_persistence_began_after_helper_returned_and_tx_was_muted": True,
                "post_helper_exact_serial_mute": post_helper_mute,
                "tx2_never_enabled": True,
                "no_automatic_retry": True,
                "sigint_sigterm_sighup_are_cooperative_exceptions": True,
                "sigkill_cannot_be_intercepted": True,
            },
        }
        record_path = artifact_root / CAPTURE_RECORD_NAME
        write_json_atomic(record_path, document)
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_path": str(artifact_root),
            "capture_record": str(record_path),
            "capture_record_sha256": sha256_path(record_path),
            "data_sha256": sha256_path(sigmf_data),
            "metadata_sha256": sha256_path(sigmf_meta),
            "stream_id": capture.frames[0].stream_id,
            "tone_offset_readback_hz": tone_readback_hz,
            "headroom_passed": headroom.passed,
            "post_helper_exact_serial_mute": post_helper_mute,
        }
    except BaseException as error:
        if artifact is not None:
            source = Path(artifact.path)
            failed_root = capture_root / ".failed"
            failed_root.mkdir(parents=True, exist_ok=True)
            destination = failed_root / f"{artifact.artifact_id}.failed"
            if source.exists():
                os.replace(source, destination)
            quarantine = _seal_existing_failed_directory(destination, error=error)
        elif writer is not None:
            destination = writer.fail(error)
            quarantine = _seal_existing_failed_directory(destination, error=error)
        else:
            quarantine = _persist_memory_quarantine(
                capture_root, blocks=retained, error=error, context=context
            )
        raise TimingCaptureFailure(str(error), quarantine) from error
    finally:
        retained.clear()


def main() -> int:
    args = _parser().parse_args()
    _install_cooperative_signal_handlers()
    if not args.serial.strip():
        raise SystemExit("--serial must be non-empty")
    if not args.uri.removeprefix("pluto://").startswith("usb:"):
        raise SystemExit("--uri must be an exact USB IIO URI")
    repository = Path(__file__).resolve().parents[1]
    if sum((args.protocol_v2, args.protocol_v23_5g8, args.protocol_v24_5g8)) > 1:
        raise SystemExit("select exactly one timing protocol")
    selected_protocol = (
        stimulus_protocol(EXPERIMENTAL_5G8_HIGH_RX_STIMULUS_PROTOCOL_ID)
        if args.protocol_v24_5g8
        else stimulus_protocol(EXPERIMENTAL_5G8_STIMULUS_PROTOCOL_ID)
        if args.protocol_v23_5g8
        else stimulus_protocol(STIMULUS_PROTOCOL_ID)
    )
    stimulus_mode = args.protocol_v2 or args.protocol_v23_5g8 or args.protocol_v24_5g8
    if stimulus_mode:
        if args.stimulus_qualification is None or args.stimulus_qualification_sha256 is None:
            raise SystemExit(
                "stimulus timing requires --stimulus-qualification and its reviewed SHA-256"
            )
        if args.center_frequency_hz != selected_protocol.center_frequencies_hz[0]:
            raise SystemExit("stimulus timing is frozen at its protocol reference frequency")
        if args.allow_experimental_5g8 != selected_protocol.allow_experimental_5g8:
            raise SystemExit("timing experimental-5.8 opt-in differs from its protocol")
        if args.receiver_gain_db is not None or args.tx_hardware_gain_db is not None:
            raise SystemExit("stimulus timing derives RF gains from its protocol and ledger")
    elif args.stimulus_qualification is not None or args.stimulus_qualification_sha256 is not None:
        raise SystemExit("stimulus qualification arguments require a stimulus protocol")
    try:
        policy = classify_fast20_center_frequency(
            args.center_frequency_hz,
            allow_experimental_5g8=args.allow_experimental_5g8,
        )
        profile = load_hexcal_profile(args.profile)
        source = _source_manifest(repository, args.source_commit)
        dependency_attestation = attest_pluto_plus_utils_source()
        dependency_attestation_sha256 = canonical_json_sha256(dependency_attestation)
        firmware = load_hexcal_firmware_evidence(
            args.firmware_evidence,
            expected_board_id=args.board_id,
            expected_source_commit=args.source_commit,
            expected_profile=profile,
        )
        stimulus_qualification: HexcalStimulusQualification | None = None
        if stimulus_mode:
            assert isinstance(args.stimulus_qualification, Path)
            qualification_source_attestation = attest_source_files_at_commit(
                repository,
                expected_commit=args.source_commit,
                relative_paths=QUALIFICATION_SOURCE_FILES,
            )
            stimulus_qualification = load_hexcal_stimulus_qualification(
                args.stimulus_qualification,
                expected_board_id=args.board_id,
                expected_serial=args.serial,
                expected_uri=args.uri,
                expected_source_commit=args.source_commit,
                expected_source_attestation=qualification_source_attestation,
                expected_profile=profile,
                expected_firmware_evidence_sha256=firmware.file_sha256,
                expected_pluto_plus_utils_source_attestation_sha256=(dependency_attestation_sha256),
                expected_protocol_id=selected_protocol.protocol_id,
                expected_qualification_kind=selected_protocol.qualification_kind,
                expected_center_frequencies_hz=selected_protocol.center_frequencies_hz,
                expected_receiver_gain_db=selected_protocol.fixed_receiver_gain_db,
            )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    if firmware.file_sha256 != args.firmware_evidence_sha256:
        raise SystemExit("firmware evidence SHA-256 differs from the reviewed plan")
    if (
        stimulus_qualification is not None
        and stimulus_qualification.file_sha256 != args.stimulus_qualification_sha256
    ):
        raise SystemExit("stimulus qualification SHA-256 differs from the reviewed plan")
    receiver_gain_db = (
        selected_protocol.timing_receiver_gain_db
        if stimulus_qualification is not None
        else (0 if args.receiver_gain_db is None else args.receiver_gain_db)
    )
    tx_hardware_gain_db = (
        stimulus_qualification.selected_tx_hardware_gain_db
        if stimulus_qualification is not None
        else (-40.0 if args.tx_hardware_gain_db is None else args.tx_hardware_gain_db)
    )
    dds_scale = (
        stimulus_qualification.dds_scale if stimulus_qualification is not None else args.dds_scale
    )
    if stimulus_qualification is not None and args.dds_scale != stimulus_qualification.dds_scale:
        raise SystemExit("stimulus DDS scale differs from the qualification ledger")
    capture_root = args.capture_root or (
        Path.home() / ".local/state/smateway/boards" / args.board_id / "pluto-usb-captures"
    )
    settings = RadioSettings(
        center_frequency_hz=args.center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=receiver_gain_db,
        channels=(0, 1),
    )
    plan = SafeDdsTonePlan(
        uri=args.uri,
        serial=args.serial,
        center_frequency_hz=args.center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=TX_CHANNEL,
        tx_hardware_gain_db=tx_hardware_gain_db,
        dds_scale=dds_scale,
        receiver_gain_db=float(receiver_gain_db),
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    run_id = f"hexcal-timing-{uuid.uuid4().hex}"
    board_state = Path.home() / ".local/state/smateway/boards" / args.board_id
    bench_lock_path = board_state / ".bench.lock"
    profile_document = profile.as_dict()
    firmware_document = firmware.as_dict()
    pair_contract = _pair_plan_contract(
        run_id=run_id,
        board_id=args.board_id,
        source=source,
        pluto_plus_utils_source_attestation=dependency_attestation,
        pluto_plus_utils_source_attestation_sha256=(dependency_attestation_sha256),
        profile=profile_document,
        firmware=firmware_document,
        center_frequency_policy=policy,
        plan=plan,
        bench_lock_path=bench_lock_path,
        stimulus_qualification=stimulus_qualification,
        stimulus_protocol_contract=(selected_protocol if stimulus_mode else None),
    )
    pair_contract_sha256 = _canonical_sha256(pair_contract)
    manifest_path = capture_root / "timing-runs" / f"{run_id}.json"
    results: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema": 1,
        "run_kind": pair_contract["plan_kind"],
        "run_id": run_id,
        "status": "planned_no_rf_enabled",
        "accepted": False,
        "created_at": _now(),
        "required_replicate_count": REPLICATE_COUNT,
        "automatic_retry_count": 0,
        "pair_plan_contract": pair_contract,
        "pair_plan_contract_sha256": pair_contract_sha256,
        "attempts": [],
        "artifacts": results,
        "mute_attestations": [],
        "error": None,
        "quarantine": None,
    }
    with _exclusive_bench_lock(bench_lock_path):
        # This durable exact plan and empty attempt ledger exist before any RF
        # helper can enable TX.  SIGKILL can still prevent later status updates.
        write_json_atomic(manifest_path, manifest)
        final_mute: dict[str, Any] | None = None
        try:
            pre_mute = _exact_serial_mute(args.serial, "pre_run")
            manifest["mute_attestations"].append(pre_mute)
            write_json_atomic(manifest_path, manifest)
            if not _mute_passed(pre_mute, serial=args.serial, purpose="pre_run"):
                raise RuntimeError("pre-run exact-serial mute attestation failed")
            for replicate in range(1, REPLICATE_COUNT + 1):
                attempt = {
                    "replicate_index": replicate,
                    "status": "in_progress",
                    "started_at": _now(),
                    "completed_at": None,
                    "artifact": None,
                    "error": None,
                }
                manifest["attempts"].append(attempt)
                manifest["status"] = f"replicate_{replicate}_in_progress"
                write_json_atomic(manifest_path, manifest)
                result = _capture_one(
                    replicate_index=replicate,
                    run_id=run_id,
                    capture_root=capture_root,
                    plan=plan,
                    settings=settings,
                    board_id=args.board_id,
                    source_manifest=source,
                    dependency_attestation=dependency_attestation,
                    dependency_attestation_sha256=dependency_attestation_sha256,
                    profile_document=profile_document,
                    firmware_document=firmware_document,
                    center_frequency_policy=policy,
                    pair_plan_contract=pair_contract,
                    pair_plan_contract_sha256=pair_contract_sha256,
                )
                results.append(result)
                attempt["status"] = "complete_full_artifact"
                attempt["completed_at"] = _now()
                attempt["artifact"] = result
                manifest["mute_attestations"].append(result["post_helper_exact_serial_mute"])
                manifest["status"] = f"replicate_{replicate}_complete"
                write_json_atomic(manifest_path, manifest)
            if len({str(item["artifact_id"]) for item in results}) != REPLICATE_COUNT:
                raise RuntimeError("timing artifacts are not independently identified")
            if len({int(item["stream_id"]) for item in results}) != REPLICATE_COUNT:
                raise RuntimeError("timing artifacts did not use independent stream generations")
            if len({str(item["data_sha256"]) for item in results}) != REPLICATE_COUNT:
                raise RuntimeError("timing artifact data hashes are unexpectedly identical")
            final_mute = _exact_serial_mute(args.serial, "final")
            manifest["mute_attestations"].append(final_mute)
            if not _mute_passed(final_mute, serial=args.serial, purpose="final"):
                raise RuntimeError("final exact-serial mute attestation failed")
        except BaseException as error:
            if final_mute is None:
                final_mute = _exact_serial_mute(args.serial, "final_after_failure")
                manifest["mute_attestations"].append(final_mute)
            attempts = manifest["attempts"]
            if attempts and attempts[-1].get("status") == "in_progress":
                attempts[-1]["status"] = "failed"
                attempts[-1]["completed_at"] = _now()
                attempts[-1]["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
            manifest["status"] = "failed_incomplete_not_qualification_eligible"
            manifest["accepted"] = False
            manifest["completed_at"] = _now()
            manifest["error"] = {"type": type(error).__name__, "message": str(error)}
            manifest["quarantine"] = (
                error.quarantine if isinstance(error, TimingCaptureFailure) else None
            )
            write_json_atomic(manifest_path, manifest)
            raise
        manifest["status"] = "two_independent_artifacts_verified_unanalyzed"
        manifest["accepted"] = True
        manifest["completed_at"] = _now()
        write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "manifest": str(manifest_path),
                "manifest_sha256": sha256_path(manifest_path),
                "artifacts": results,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
