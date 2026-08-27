#!/usr/bin/env python3
"""Explore RX gains safely, then persist the lowest sufficient Hexcal gain."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
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

from pluto_plus.artifacts import CaptureWriter, data_path, verify_artifact
from pluto_plus.bootstrap_firmware import mute_returned_radio
from pluto_plus.hardware import SafeDdsTonePlan, SampleBlockV2, capture_continuous_safe_dds_tone
from pluto_plus.hardware.iio import find_usb_sysfs_path
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    canonical_json_sha256,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.hexcal_gain import (
    BANDWIDTH_HZ,
    CONDITION_TIMEOUT_S,
    DEFAULT_GAIN_CANDIDATES_DB,
    DEFAULT_STIMULUS_TX_GAINS_DB,
    FRAME_COUNT,
    KERNEL_BUFFERS,
    QUALIFICATION_KIND,
    QUALIFICATION_SOURCE_FILES,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    STIMULUS_CENTER_FREQUENCIES_HZ,
    STIMULUS_FIXED_RECEIVER_GAIN_DB,
    STIMULUS_QUALIFICATION_KIND,
    TONE_OFFSET_HZ,
    TOTAL_SAMPLES,
    gain_headroom_passes,
    load_hexcal_gain_qualification,
    load_hexcal_stimulus_qualification,
    qualification_thresholds,
    replay_hexcal_gain_artifact,
)
from smateway.rf_policy import EXPERIMENTAL_5G8_CENTER_HZ, classify_fast20_center_frequency

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
DEFAULT_FREQUENCIES_HZ = (
    2_400_000_000,
    2_423_000_000,
    2_440_000_000,
    2_458_000_000,
    2_483_000_000,
    EXPERIMENTAL_5G8_CENTER_HZ,
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class QualificationError(RuntimeError):
    """A safety, identity, continuity, or persisted-evidence invariant failed."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="exact usb: IIO URI")
    parser.add_argument("--firmware-evidence", type=Path, required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--qualification-id")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--allow-experimental-5g8", action="store_true")
    parser.add_argument(
        "--center-frequency-hz",
        action="append",
        type=int,
        dest="center_frequencies_hz",
        metavar="HZ",
        help="repeat to replace the six-frequency default plan",
    )
    parser.add_argument(
        "--gain-db",
        action="append",
        type=int,
        dest="candidate_gains_db",
        metavar="DB",
        help="repeat only to truncate the exhaustive ascending 0..62 dB search",
    )
    parser.add_argument("--tx-hardware-gain-db", type=float, default=-40.0)
    parser.add_argument(
        "--tx-stimulus-v2",
        action="store_true",
        help="run the frozen five-frequency TX1 stimulus ladder at fixed 20 dB RX gain",
    )
    parser.add_argument(
        "--tx-gain-db",
        action="append",
        type=float,
        dest="candidate_tx_gains_db",
        metavar="DB",
        help="repeat only to replace the frozen v2 TX ladder",
    )
    parser.add_argument(
        "--fixed-receiver-gain-db",
        type=int,
        default=STIMULUS_FIXED_RECEIVER_GAIN_DB,
        help="v2 only; must remain the frozen 20 dB",
    )
    parser.add_argument("--dds-scale", type=float, default=0.125)
    return parser


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _cooperative_termination(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def _condition_timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("gain-qualification condition exceeded its finite deadline")


@contextmanager
def _condition_deadline() -> Iterator[None]:
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    previous_handler = signal.signal(signal.SIGALRM, _condition_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, CONDITION_TIMEOUT_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _repository_commit_and_require_clean(repository: Path) -> str:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise QualificationError("gain qualification requires a clean implementation tree")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise QualificationError("implementation source commit is malformed")
    return commit


def _board_root(board_id: str) -> Path:
    return Path.home() / ".local/state/smateway/boards" / board_id


@contextmanager
def _board_lock(board_root: Path) -> Iterator[None]:
    board_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (board_root / ".bench.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _validate_frequencies(
    values: Sequence[int] | None, *, allow_experimental_5g8: bool
) -> tuple[int, ...]:
    frequencies = DEFAULT_FREQUENCIES_HZ if values is None else tuple(values)
    if not frequencies or len(set(frequencies)) != len(frequencies):
        raise ValueError("center frequencies must be non-empty and unique")
    for frequency_hz in frequencies:
        classify_fast20_center_frequency(
            frequency_hz,
            allow_experimental_5g8=allow_experimental_5g8,
        )
    return tuple(frequencies)


def _validate_candidates(values: Sequence[int] | None) -> tuple[int, ...]:
    candidates = DEFAULT_GAIN_CANDIDATES_DB if values is None else tuple(values)
    if (
        not candidates
        or candidates[0] != 0
        or any(
            second != first + 1 for first, second in zip(candidates, candidates[1:], strict=False)
        )
        or any(not 0 <= value <= 62 for value in candidates)
    ):
        raise ValueError("gain candidates must be a contiguous ascending prefix from 0 dB")
    return tuple(candidates)


def _validate_tx_stimulus_candidates(
    values: Sequence[float] | None,
) -> tuple[float, ...]:
    candidates = (
        DEFAULT_STIMULUS_TX_GAINS_DB if values is None else tuple(float(value) for value in values)
    )
    if (
        not candidates
        or len(set(candidates)) != len(candidates)
        or any(not math.isfinite(value) or not -80.0 <= value <= 0.0 for value in candidates)
        or any(second <= first for first, second in zip(candidates, candidates[1:], strict=False))
    ):
        raise ValueError(
            "TX-stimulus candidates must be unique finite gains in strictly ascending power"
        )
    if candidates != DEFAULT_STIMULUS_TX_GAINS_DB:
        raise ValueError("hexcal-v2 requires the exact frozen TX-stimulus ladder")
    return tuple(candidates)


def _strict_mute(serial: str, purpose: str) -> dict[str, Any]:
    started_at = _now()
    try:
        mute_returned_radio(serial)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_text(error),
        }
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _mute_passed(value: Mapping[str, Any], *, serial: str, purpose: str) -> bool:
    return (
        value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _radio_identity(uri: str, serial: str) -> RadioIdentity:
    return RadioIdentity(
        radio_id=serial,
        serial=serial,
        uri=uri,
        transport=Transport.IIO_USB,
        model="Analog Devices PlutoSDR Rev.C (Z7010-AD9361)",
        firmware_version=V7_FIRMWARE_VERSION,
        usb_path=find_usb_sysfs_path(serial),
    )


def _runtime_evidence(repository: Path) -> dict[str, Any]:
    requested = str(_PINNED_PYTHON)
    executable = str(Path(sys.executable).absolute())
    prefix = str(Path(sys.prefix).resolve())
    source_root = str((repository / "src").resolve())
    module_path = str(Path(replay_hexcal_gain_artifact.__code__.co_filename).resolve())
    if (
        executable != requested
        or prefix != str(_PINNED_PREFIX)
        or source_root != str(_SMATEWAY_SOURCE)
        or module_path != str(_SMATEWAY_SOURCE / "smateway/hexcal_gain.py")
    ):
        raise QualificationError("gain qualification did not route through pinned Python/source")
    return {
        "requested_executable": requested,
        "sys_executable": executable,
        "sys_prefix": prefix,
        "smateway_source_root": source_root,
        "hexcal_gain_module_path": module_path,
        "auto_reexec_before_pluto_import": True,
    }


def _rf_readback_evidence(
    capture: Any, *, tx_hardware_gain_db: float, dds_scale: float
) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [
            capture.tx_gain_readback_db,
            -80.0,
        ],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_scale_requested": dds_scale,
        "dds_scale_readback": list(capture.dds_scale_readback),
        "dds_enabled_readback": list(capture.dds_enabled_readback),
        "tone_frequency_hz_requested": TONE_OFFSET_HZ,
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
        planned_tx_gain_db=tx_hardware_gain_db,
        planned_dds_scale=dds_scale,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return evidence


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    artifact_root = Path(artifact.path)
    data_file = data_path(artifact)
    metadata_file = artifact_root / f"{artifact.artifact_id}.sigmf-meta"
    return {
        "artifact_id": artifact.artifact_id,
        "path": str(artifact_root),
        "data_path": str(data_file),
        "data_sha256": sha256_path(data_file),
        "data_size_bytes": data_file.stat().st_size,
        "metadata_path": str(metadata_file),
        "metadata_sha256": sha256_path(metadata_file),
        "metadata_size_bytes": metadata_file.stat().st_size,
    }


def _capture_condition(
    *,
    artifact_root: Path,
    profile: Any,
    serial: str,
    uri: str,
    center_frequency_hz: int,
    receiver_gain_db: int,
    tx_hardware_gain_db: float,
    dds_scale: float,
    allow_experimental_5g8: bool,
) -> dict[str, Any]:
    started_at = _now()
    policy = classify_fast20_center_frequency(
        center_frequency_hz,
        allow_experimental_5g8=allow_experimental_5g8,
    )
    settings = RadioSettings(
        center_frequency_hz=center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=receiver_gain_db,
        channels=(0, 1),
    )
    plan = SafeDdsTonePlan(
        uri=uri,
        serial=serial,
        center_frequency_hz=center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        tone_frequency_hz=TONE_OFFSET_HZ,
        tx_channel=0,
        tx_hardware_gain_db=tx_hardware_gain_db,
        dds_scale=dds_scale,
        receiver_gain_db=float(receiver_gain_db),
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    writer = CaptureWriter(
        artifact_root,
        radio=_radio_identity(uri, serial),
        settings=settings,
        label=(
            "EXPLORATORY hexcal qualification "
            f"RX={receiver_gain_db}dB TX={tx_hardware_gain_db}dB "
            f"frequency={center_frequency_hz}Hz"
        ),
    )
    headroom = AdcHeadroomMonitor(receiver_count=2)

    def persist(block: SampleBlockV2) -> None:
        headroom.observe(block.samples)
        writer.append(block, settings, revision=1)

    try:
        with _condition_deadline():
            capture = capture_continuous_safe_dds_tone(
                plan,
                samples_per_frame=SAMPLES_PER_FRAME,
                frame_count=FRAME_COUNT,
                kernel_buffers=KERNEL_BUFFERS,
                block_consumer=persist,
            )
        if capture.settings != settings:
            raise QualificationError("qualification settings readback differs from the plan")
        if capture.identity.serial != serial or capture.identity.uri != uri:
            raise QualificationError("qualification opened a different Pluto identity")
        if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
            raise QualificationError("qualification capture shape differs from the plan")
        rf_readback = _rf_readback_evidence(
            capture,
            tx_hardware_gain_db=tx_hardware_gain_db,
            dds_scale=dds_scale,
        )
        live_headroom_document = asdict(headroom.result())
    except BaseException as error:
        writer.fail(error)
        post_mute = _strict_mute(serial, "post_condition")
        if not _mute_passed(post_mute, serial=serial, purpose="post_condition"):
            raise QualificationError(
                f"capture failed and exact post-condition mute failed: {post_mute['error']}"
            ) from error
        raise

    post_mute = _strict_mute(serial, "post_condition")
    if not _mute_passed(post_mute, serial=serial, purpose="post_condition"):
        mute_failure = QualificationError(f"exact post-condition mute failed: {post_mute['error']}")
        writer.fail(mute_failure)
        raise mute_failure
    artifact = writer.finalize()
    if not verify_artifact(artifact):
        raise QualificationError("exploratory artifact SHA-256 verification failed")
    evidence = _artifact_evidence(artifact)
    dds_frequencies = rf_readback["dds_frequency_readback_hz"]
    assert isinstance(dds_frequencies, list)
    tone_offset_hz = (abs(float(dds_frequencies[0])) + abs(float(dds_frequencies[2]))) / 2.0
    replayed = replay_hexcal_gain_artifact(
        evidence,
        ledger_root=artifact_root.parent,
        profile=profile,
        expected_serial=serial,
        expected_uri=uri,
        expected_center_frequency_hz=center_frequency_hz,
        expected_receiver_gain_db=receiver_gain_db,
        tone_offset_hz=tone_offset_hz,
    )
    passed = bool(replayed["passed"]) and gain_headroom_passes(live_headroom_document)
    rejection_reasons = list(replayed["rejection_reasons"])
    if not gain_headroom_passes(live_headroom_document):
        rejection_reasons.append("live_conservative_dual_rx_headroom_failed")
    return {
        "receiver_gain_db": receiver_gain_db,
        "tx_hardware_gain_db": tx_hardware_gain_db,
        "center_frequency_hz": center_frequency_hz,
        "status": "complete",
        "passed": passed,
        "rejection_reasons": rejection_reasons,
        "started_at": started_at,
        "completed_at": _now(),
        "capture_policy": policy,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "kernel_buffers": KERNEL_BUFFERS,
        "tx_channel": 0,
        "tx_port": "TX1",
        "rx_channel_analyzed": 1,
        "rx_port_analyzed": "RX2",
        "artifact_evidence": evidence,
        "rf_readback_evidence": rf_readback,
        "rx_hold_evidence": {
            "schema": 1,
            "mode": "tandem_hold",
            "channels": [0, 1],
            "requested_gain_db": receiver_gain_db,
            "verified_tolerance_db": 0.25,
            "provenance": ("pinned_helper_verified_each_channel_within_requested_gain_tolerance"),
        },
        "live_adc_headroom_admission": live_headroom_document,
        "replayed_artifact_analysis": replayed,
        "post_mute": post_mute,
        "exploratory_only": True,
        "accepted_calibration_artifact": False,
    }


def _configuration(
    *,
    board_id: str,
    serial: str,
    uri: str,
    source_commit: str,
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    python_runtime: Mapping[str, Any],
    profile: Any,
    firmware_evidence: Any,
    frequencies: Sequence[int],
    candidates: Sequence[int],
    tx_hardware_gain_db: float,
    dds_scale: float,
    allow_experimental_5g8: bool,
) -> dict[str, Any]:
    dependency = dict(dependency_attestation)
    return {
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "source_commit": source_commit,
        "source_attestation": dict(source_attestation),
        "profile": str(profile.path),
        "profile_file_sha256": profile.file_sha256,
        "profile_contract_sha256": profile.contract_sha256,
        "firmware_evidence": firmware_evidence.as_dict(),
        "firmware_evidence_sha256": firmware_evidence.file_sha256,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
        "python_runtime": dict(python_runtime),
        "center_frequencies_hz": list(frequencies),
        "candidate_gains_db": list(candidates),
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "kernel_buffers": KERNEL_BUFFERS,
        "condition_timeout_s": CONDITION_TIMEOUT_S,
        "duration_s_per_condition": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
        "tone_offset_hz": TONE_OFFSET_HZ,
        "tx_channel": 0,
        "tx_port": "TX1",
        "tx2_policy": "muted_-80dB_and_zero_DDS",
        "tx_hardware_gain_db": tx_hardware_gain_db,
        "dds_scale": dds_scale,
        "allow_experimental_5g8": allow_experimental_5g8,
        "thresholds": qualification_thresholds(),
        "selection_policy": "first ascending gain passing every frequency and all six states",
        "adaptation_policy": "explore between finite captures; never adapt within a capture",
    }


def _stimulus_configuration(
    *,
    board_id: str,
    serial: str,
    uri: str,
    source_commit: str,
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    python_runtime: Mapping[str, Any],
    profile: Any,
    firmware_evidence: Any,
    frequencies: Sequence[int],
    candidates: Sequence[float],
    receiver_gain_db: int,
    dds_scale: float,
) -> dict[str, Any]:
    dependency = dict(dependency_attestation)
    return {
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "source_commit": source_commit,
        "source_attestation": dict(source_attestation),
        "profile": str(profile.path),
        "profile_file_sha256": profile.file_sha256,
        "profile_contract_sha256": profile.contract_sha256,
        "firmware_evidence": firmware_evidence.as_dict(),
        "firmware_evidence_sha256": firmware_evidence.file_sha256,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
        "python_runtime": dict(python_runtime),
        "center_frequencies_hz": list(frequencies),
        "candidate_tx_hardware_gains_db": list(candidates),
        "fixed_receiver_gain_db": receiver_gain_db,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "kernel_buffers": KERNEL_BUFFERS,
        "condition_timeout_s": CONDITION_TIMEOUT_S,
        "duration_s_per_condition": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
        "tone_offset_hz": TONE_OFFSET_HZ,
        "tx_channel": 0,
        "tx_port": "TX1",
        "tx2_policy": "muted_-80dB_and_zero_DDS",
        "dds_scale": dds_scale,
        "allow_experimental_5g8": False,
        "thresholds": qualification_thresholds(),
        "selection_policy": (
            "lowest power ascending TX gain passing every frequency and all six states"
        ),
        "headroom_stop_policy": (
            "stop before any stronger candidate after one failed condition headroom gate"
        ),
        "adaptation_policy": "explore between finite captures; never adapt within a capture",
    }


def _run_tx_stimulus_qualification(
    *,
    repository: Path,
    args: argparse.Namespace,
    board_id: str,
    qualification_id: str,
    source_commit: str,
    source_attestation: Mapping[str, Any],
    dependency_attestation: Mapping[str, Any],
    python_runtime: Mapping[str, Any],
    profile: Any,
    firmware_evidence: Any,
    frequencies: tuple[int, ...],
    candidates: tuple[float, ...],
) -> int:
    receiver_gain_db = int(args.fixed_receiver_gain_db)
    if receiver_gain_db != STIMULUS_FIXED_RECEIVER_GAIN_DB:
        raise SystemExit("hexcal-v2 fixes the common RX gain at exactly 20 dB")
    board_root = _board_root(board_id)
    run_root = board_root / "hexcal-stimulus-qualifications" / qualification_id
    ledger_path = run_root / "stimulus-qualification.json"
    if run_root.exists():
        raise SystemExit(f"qualification output already exists: {run_root}")
    configuration = _stimulus_configuration(
        board_id=board_id,
        serial=args.serial,
        uri=args.uri,
        source_commit=source_commit,
        source_attestation=source_attestation,
        dependency_attestation=dependency_attestation,
        python_runtime=python_runtime,
        profile=profile,
        firmware_evidence=firmware_evidence,
        frequencies=frequencies,
        candidates=candidates,
        receiver_gain_db=receiver_gain_db,
        dds_scale=args.dds_scale,
    )
    document: dict[str, Any] = {
        "schema": 1,
        "qualification_kind": STIMULUS_QUALIFICATION_KIND,
        "qualification_id": qualification_id,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "status": "running",
        "configuration": configuration,
        "plan": [
            {
                "tx_gain_index": gain_index,
                "frequency_index": frequency_index,
                "receiver_gain_db": receiver_gain_db,
                "tx_hardware_gain_db": gain,
                "center_frequency_hz": frequency,
                "tx_channel": 0,
                "tx_port": "TX1",
            }
            for gain_index, gain in enumerate(candidates)
            for frequency_index, frequency in enumerate(frequencies)
        ],
        "conditions": [],
        "tested_tx_hardware_gains_db": [],
        "selected_tx_hardware_gain_db": None,
        "selection_policy": ("lowest_power_ascending_tx_gain_passing_every_frequency_and_state"),
        "receiver_gain_is_fixed": True,
        "selected_stimulus_is_frozen": True,
        "preflight_mute": None,
        "final_mute": None,
        "error": None,
    }
    pending_error: BaseException | None = None
    interrupted = False
    selected: float | None = None

    with _board_lock(board_root):
        # The complete ladder and immutable evidence identities are durable
        # before the first finite TX1 tone can be enabled.
        write_json_atomic(ledger_path, document)
        try:
            preflight_mute = _strict_mute(args.serial, "preflight")
            document["preflight_mute"] = preflight_mute
            document["updated_at"] = _now()
            write_json_atomic(ledger_path, document)
            if not _mute_passed(preflight_mute, serial=args.serial, purpose="preflight"):
                raise QualificationError(f"exact preflight mute failed: {preflight_mute['error']}")
            conditions = document["conditions"]
            tested_gains = document["tested_tx_hardware_gains_db"]
            assert isinstance(conditions, list) and isinstance(tested_gains, list)
            for gain in candidates:
                gain_records: list[dict[str, Any]] = []
                headroom_failed = False
                for frequency in frequencies:
                    record = _capture_condition(
                        artifact_root=run_root / "exploratory-artifacts",
                        profile=profile,
                        serial=args.serial,
                        uri=args.uri,
                        center_frequency_hz=frequency,
                        receiver_gain_db=receiver_gain_db,
                        tx_hardware_gain_db=gain,
                        dds_scale=args.dds_scale,
                        allow_experimental_5g8=False,
                    )
                    gain_records.append(record)
                    conditions.append(record)
                    headroom_failed = headroom_failed or not gain_headroom_passes(
                        record.get("live_adc_headroom_admission")
                    )
                    document["updated_at"] = _now()
                    write_json_atomic(ledger_path, document)
                tested_gains.append(gain)
                document["updated_at"] = _now()
                write_json_atomic(ledger_path, document)
                if headroom_failed:
                    raise QualificationError(
                        "TX-stimulus headroom failed; stronger candidates are forbidden"
                    )
                if all(record["passed"] is True for record in gain_records):
                    selected = gain
                    break
            if selected is None:
                raise QualificationError(
                    "no tested TX1 stimulus passed every 2.4 GHz frequency and state"
                )
            document["selected_tx_hardware_gain_db"] = selected
        except KeyboardInterrupt as error:
            interrupted = True
            pending_error = error
        except BaseException as error:
            pending_error = error
        finally:
            final_mute = _strict_mute(args.serial, "final")
            document["final_mute"] = final_mute
            if not _mute_passed(final_mute, serial=args.serial, purpose="final"):
                mute_error = QualificationError(f"exact final mute failed: {final_mute['error']}")
                if pending_error is not None:
                    final_mute["prior_error"] = _error_text(pending_error)
                pending_error = mute_error
            document["completed_at"] = _now()
            document["updated_at"] = document["completed_at"]
            document["status"] = "failed" if pending_error is not None else "passed"
            document["error"] = None if pending_error is None else _error_text(pending_error)
            write_json_atomic(ledger_path, document)

    if pending_error is not None:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "qualification": str(ledger_path),
                    "error": _error_text(pending_error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130 if interrupted else 1

    try:
        final_source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        final_dependency_attestation = attest_pluto_plus_utils_source()
        if (
            final_source_attestation != source_attestation
            or final_dependency_attestation != dependency_attestation
        ):
            raise QualificationError("scientific source changed during qualification")
        load_hexcal_stimulus_qualification(
            ledger_path,
            expected_board_id=board_id,
            expected_serial=args.serial,
            expected_uri=args.uri,
            expected_source_commit=source_commit,
            expected_source_attestation=source_attestation,
            expected_profile=profile,
            expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
            expected_pluto_plus_utils_source_attestation_sha256=canonical_json_sha256(
                dependency_attestation
            ),
            expected_center_frequencies_hz=frequencies,
            expected_receiver_gain_db=receiver_gain_db,
            expected_candidate_tx_hardware_gains_db=candidates,
            expected_dds_scale=args.dds_scale,
        )
    except (OSError, ValueError, QualificationError, subprocess.CalledProcessError) as error:
        document["status"] = "failed"
        document["error"] = _error_text(error)
        document["updated_at"] = _now()
        write_json_atomic(ledger_path, document)
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "passed",
                "qualification_id": qualification_id,
                "qualification": str(ledger_path),
                "qualification_sha256": sha256_path(ledger_path),
                "fixed_receiver_gain_db": receiver_gain_db,
                "selected_tx_hardware_gain_db": selected,
                "tested_tx_hardware_gains_db": document["tested_tx_hardware_gains_db"],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    if sys.argv[1:] == ["--print-routed-runtime-evidence"]:
        print(json.dumps(_runtime_evidence(repository), sort_keys=True))
        return 0
    args = _parser().parse_args()
    signal.signal(signal.SIGTERM, _cooperative_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _cooperative_termination)
    if not args.serial.strip() or not args.uri.startswith("usb:"):
        raise SystemExit("explicit non-empty --serial and exact usb: --uri are required")
    try:
        board_id = _validate_identifier(args.board_id, "board ID")
        qualification_id = _validate_identifier(
            args.qualification_id or _new_id(), "qualification ID"
        )
        if args.tx_stimulus_v2:
            if (
                args.candidate_gains_db is not None
                or args.allow_experimental_5g8
                or args.tx_hardware_gain_db != -40.0
                or args.dds_scale != 0.125
            ):
                raise ValueError(
                    "v2 forbids RX-ladder/5.8 GHz/fixed-TX overrides and fixes DDS scale at 0.125"
                )
            frequencies = (
                STIMULUS_CENTER_FREQUENCIES_HZ
                if args.center_frequencies_hz is None
                else _validate_frequencies(
                    args.center_frequencies_hz,
                    allow_experimental_5g8=False,
                )
            )
            if frequencies != STIMULUS_CENTER_FREQUENCIES_HZ:
                raise ValueError("v2 requires the exact frozen five-frequency 2.4 GHz plan")
            stimulus_candidates = _validate_tx_stimulus_candidates(args.candidate_tx_gains_db)
            candidates = DEFAULT_GAIN_CANDIDATES_DB
        else:
            if (
                args.candidate_tx_gains_db is not None
                or args.fixed_receiver_gain_db != STIMULUS_FIXED_RECEIVER_GAIN_DB
            ):
                raise ValueError("TX-stimulus options require --tx-stimulus-v2")
            frequencies = _validate_frequencies(
                args.center_frequencies_hz,
                allow_experimental_5g8=args.allow_experimental_5g8,
            )
            candidates = _validate_candidates(args.candidate_gains_db)
            stimulus_candidates = DEFAULT_STIMULUS_TX_GAINS_DB
        source_commit = _repository_commit_and_require_clean(repository)
        profile = load_hexcal_profile(args.profile)
        firmware_evidence = load_hexcal_firmware_evidence(
            args.firmware_evidence,
            expected_board_id=board_id,
            expected_source_commit=source_commit,
            expected_profile=profile,
        )
        source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        dependency_attestation = attest_pluto_plus_utils_source()
        python_runtime = _runtime_evidence(repository)
    except (OSError, ValueError, QualificationError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    if args.tx_stimulus_v2:
        return _run_tx_stimulus_qualification(
            repository=repository,
            args=args,
            board_id=board_id,
            qualification_id=qualification_id,
            source_commit=source_commit,
            source_attestation=source_attestation,
            dependency_attestation=dependency_attestation,
            python_runtime=python_runtime,
            profile=profile,
            firmware_evidence=firmware_evidence,
            frequencies=frequencies,
            candidates=stimulus_candidates,
        )

    board_root = _board_root(board_id)
    run_root = board_root / "hexcal-gain-qualifications" / qualification_id
    ledger_path = run_root / "gain-qualification.json"
    if run_root.exists():
        raise SystemExit(f"qualification output already exists: {run_root}")
    configuration = _configuration(
        board_id=board_id,
        serial=args.serial,
        uri=args.uri,
        source_commit=source_commit,
        source_attestation=source_attestation,
        dependency_attestation=dependency_attestation,
        python_runtime=python_runtime,
        profile=profile,
        firmware_evidence=firmware_evidence,
        frequencies=frequencies,
        candidates=candidates,
        tx_hardware_gain_db=args.tx_hardware_gain_db,
        dds_scale=args.dds_scale,
        allow_experimental_5g8=args.allow_experimental_5g8,
    )
    document: dict[str, Any] = {
        "schema": 1,
        "qualification_kind": QUALIFICATION_KIND,
        "qualification_id": qualification_id,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "status": "running",
        "configuration": configuration,
        "plan": [
            {
                "gain_index": gain_index,
                "frequency_index": frequency_index,
                "receiver_gain_db": gain,
                "center_frequency_hz": frequency,
                "tx_channel": 0,
                "tx_port": "TX1",
            }
            for gain_index, gain in enumerate(candidates)
            for frequency_index, frequency in enumerate(frequencies)
        ],
        "conditions": [],
        "tested_gains_db": [],
        "selected_receiver_gain_db": None,
        "selection_policy": "lowest_ascending_gain_passing_every_frequency_and_state",
        "calibration_gain_is_fixed": True,
        "preflight_mute": None,
        "final_mute": None,
        "error": None,
    }
    pending_error: BaseException | None = None
    interrupted = False
    selected: int | None = None

    with _board_lock(board_root):
        # The complete exploratory matrix, RF limits, identities, hashes and source
        # attestations are durable before the first finite TX1 tone is enabled.
        write_json_atomic(ledger_path, document)
        try:
            preflight_mute = _strict_mute(args.serial, "preflight")
            document["preflight_mute"] = preflight_mute
            document["updated_at"] = _now()
            write_json_atomic(ledger_path, document)
            if not _mute_passed(preflight_mute, serial=args.serial, purpose="preflight"):
                raise QualificationError(f"exact preflight mute failed: {preflight_mute['error']}")
            conditions = document["conditions"]
            tested_gains = document["tested_gains_db"]
            assert isinstance(conditions, list) and isinstance(tested_gains, list)
            for gain in candidates:
                gain_records: list[dict[str, Any]] = []
                for frequency in frequencies:
                    record = _capture_condition(
                        artifact_root=run_root / "exploratory-artifacts",
                        profile=profile,
                        serial=args.serial,
                        uri=args.uri,
                        center_frequency_hz=frequency,
                        receiver_gain_db=gain,
                        tx_hardware_gain_db=args.tx_hardware_gain_db,
                        dds_scale=args.dds_scale,
                        allow_experimental_5g8=args.allow_experimental_5g8,
                    )
                    gain_records.append(record)
                    conditions.append(record)
                    document["updated_at"] = _now()
                    write_json_atomic(ledger_path, document)
                tested_gains.append(gain)
                document["updated_at"] = _now()
                write_json_atomic(ledger_path, document)
                if all(record["passed"] is True for record in gain_records):
                    selected = gain
                    break
            if selected is None:
                raise QualificationError(
                    "no tested manual RX gain passed every frequency and all six states"
                )
            document["selected_receiver_gain_db"] = selected
        except KeyboardInterrupt as error:
            interrupted = True
            pending_error = error
        except BaseException as error:
            pending_error = error
        finally:
            final_mute = _strict_mute(args.serial, "final")
            document["final_mute"] = final_mute
            if not _mute_passed(final_mute, serial=args.serial, purpose="final"):
                mute_error = QualificationError(f"exact final mute failed: {final_mute['error']}")
                if pending_error is not None:
                    final_mute["prior_error"] = _error_text(pending_error)
                pending_error = mute_error
            document["completed_at"] = _now()
            document["updated_at"] = document["completed_at"]
            document["status"] = "failed" if pending_error is not None else "passed"
            document["error"] = None if pending_error is None else _error_text(pending_error)
            write_json_atomic(ledger_path, document)

    if pending_error is not None:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "qualification": str(ledger_path),
                    "error": _error_text(pending_error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130 if interrupted else 1

    try:
        final_source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        final_dependency_attestation = attest_pluto_plus_utils_source()
        if (
            final_source_attestation != source_attestation
            or final_dependency_attestation != dependency_attestation
        ):
            raise QualificationError("scientific source changed during gain qualification")
        final_profile = load_hexcal_profile(profile.path)
        final_firmware_evidence = load_hexcal_firmware_evidence(
            firmware_evidence.path,
            expected_board_id=board_id,
            expected_source_commit=source_commit,
            expected_profile=final_profile,
        )
        if (
            final_profile.file_sha256 != profile.file_sha256
            or final_profile.contract_sha256 != profile.contract_sha256
            or final_firmware_evidence.file_sha256 != firmware_evidence.file_sha256
        ):
            raise QualificationError("profile or firmware evidence changed during qualification")
        load_hexcal_gain_qualification(
            ledger_path,
            expected_board_id=board_id,
            expected_serial=args.serial,
            expected_uri=args.uri,
            expected_source_commit=source_commit,
            expected_source_attestation=source_attestation,
            expected_profile=profile,
            expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
            expected_pluto_plus_utils_source_attestation_sha256=canonical_json_sha256(
                dependency_attestation
            ),
            expected_center_frequencies_hz=frequencies,
            expected_tx_hardware_gain_db=args.tx_hardware_gain_db,
            expected_dds_scale=args.dds_scale,
        )
    except (OSError, ValueError, QualificationError, subprocess.CalledProcessError) as error:
        document["status"] = "failed"
        document["error"] = _error_text(error)
        document["updated_at"] = _now()
        write_json_atomic(ledger_path, document)
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "passed",
                "qualification_id": qualification_id,
                "qualification": str(ledger_path),
                "qualification_sha256": sha256_path(ledger_path),
                "selected_receiver_gain_db": selected,
                "tested_gains_db": document["tested_gains_db"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
