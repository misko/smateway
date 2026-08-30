#!/usr/bin/env python3
"""Capture one source-distinct, true-TX-muted 5.8 GHz dual-RX P1 stream."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SMATEWAY_SOURCE = _REPOSITORY / "src"
_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SMATEWAY_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned capture Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    loader_entries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(_REQUIRED_LIBIIO_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

for source_directory in (_SMATEWAY_SOURCE, _SCRIPT_DIRECTORY):
    if str(source_directory) not in sys.path:
        sys.path.insert(0, str(source_directory))

import numpy as np
import numpy.typing as npt
from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.hardware import IioRadioDevice, SampleBlockV2
from pluto_plus.hardware.iio import (
    _mute_transmit,
    _release_device,
    context_facts,
    resolve_iio_uri,
)
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION
from pluto_plus.models import ArtifactSummary, GainMode, RadioIdentity, RadioSettings
from pluto_plus.tandem import TandemMode, TandemSessionRequestV1

import run_5g8_leakage_ladder as foundation
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.capture_continuity import validate_continuity_ledger
from smateway.hexcal import (
    PLUTO_PLUS_UTILS_IMPORTED_MODULES,
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    sha256_path,
    write_json_atomic,
)
from smateway.muted_control import _validate_window_geometry, analyze_muted_stream
from smateway.native_iio_attestation import (
    RuntimeAttestationBoundary,
    attest_runtime as _native_libiio_runtime_attestation,
    call_runtime_preflight,
    runtime_preflight_passed,
    validate_runtime_attestation,
)
from smateway.ota_analysis import ContinuityBlock, analyze_fast20_dwell_isolation
from smateway.profile import load_profile
from smateway.selector_flash_attestation import (
    EVIDENCE_KIND as SEALED_SELECTOR_EVIDENCE_KIND,
    SelectorFlashError,
    validate_sealed_selector_evidence,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
CENTER_FREQUENCY_HZ = 5_800_000_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
RECEIVER_GAIN_DB = 40
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 100
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
RECORD_FILENAME = "5g8-muted-control-record.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
RUN_RESERVATION_DIRECTORY = ".muted-control-run-id-ledger"
EXECUTION_BURN_SUFFIX = ".execution-started.json"
FIXTURE_KIND = "5g8_p1_untouched_fixture"
SETUP_KIND = "5g8_p1_muted_control_setup"
P0_POST_CYCLE_SCHEDULE_PROOF_KIND = "5g8_p0_post_cycle_fast20_schedule_proof"
TOPOLOGY_TOKEN = "UNTOUCHED_ROTATION0_FULL_CONDUCTED_FIXTURE"
SHA256_LENGTH = 64
PLACEHOLDER = re.compile(r"REPLACE_[A-Za-z0-9_]+")
P0_MINIMUM_PILOT_SNR_DB = 20.0
P0_MAXIMUM_AMPLITUDE_CV = 0.10
P0_MAXIMUM_CIRCULAR_PHASE_STD_DEG = 10.0
P0_MINIMUM_COMPLETE_FAST20_CYCLES = 20


class MutedControlError(RuntimeError):
    """A frozen T1 plan, safety gate, capture, or evidence invariant failed."""


class MutedCaptureFailure(MutedControlError):
    """One capture failed after its partial evidence was quarantined."""

    def __init__(
        self,
        message: str,
        *,
        quarantine: Mapping[str, Any],
        post_mute: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.quarantine = dict(quarantine)
        self.post_mute = dict(post_mute)


@dataclass(frozen=True, slots=True)
class MutedFrameProof:
    stream_id: int
    buffer_sequence: int
    first_sample_sequence: int
    last_sample_sequence_exclusive: int
    sample_count: int
    metadata_abi: int
    metadata_flags: int


@dataclass(frozen=True, slots=True)
class MutedContinuousCapture:
    identity: RadioIdentity
    settings: RadioSettings
    frames: tuple[MutedFrameProof, ...]
    kernel_buffers: int
    receive_only_api: bool
    tx_source_active: bool

    @property
    def sample_count(self) -> int:
        return sum(frame.sample_count for frame in self.frames)


class CaptureBoundary(Protocol):
    def __call__(
        self,
        contract: Mapping[str, Any],
        *,
        block_consumer: Callable[[SampleBlockV2], None],
    ) -> MutedContinuousCapture: ...


MuteBoundary = Callable[[str, str, str], dict[str, Any]]
EvidenceBoundary = Callable[[Mapping[str, Any]], dict[str, Any]]


class IdentityBoundary(Protocol):
    def __call__(self, serial: str, requested_uri: str) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: object) -> Any:
    return foundation._json_safe(value)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = _assert_safe_local_path(path, label=label)
    if exact.is_symlink() or not exact.is_file():
        raise MutedControlError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MutedControlError(f"cannot load {label}: {error}") from error
    _assert_safe_local_path(exact, label=label)
    if exact.is_symlink() or not exact.is_file():
        raise MutedControlError(f"{label} changed while it was being read")
    if not isinstance(value, dict):
        raise MutedControlError(f"{label} root must be an object")
    return value


def _assert_no_unresolved_placeholders(
    value: object,
    *,
    label: str,
    location: str = "$",
) -> None:
    """Reject template tokens recursively before interpreting fixture/setup evidence."""

    if isinstance(value, str):
        match = PLACEHOLDER.search(value)
        if match is not None:
            raise MutedControlError(
                f"{label} contains unresolved placeholder at {location}: {match.group(0)}"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            match = PLACEHOLDER.search(key_text)
            if match is not None:
                raise MutedControlError(
                    f"{label} contains unresolved placeholder key at {location}: {match.group(0)}"
                )
            _assert_no_unresolved_placeholders(
                item,
                label=label,
                location=f"{location}.{key_text}",
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_unresolved_placeholders(
                item,
                label=label,
                location=f"{location}[{index}]",
            )


def _validate_sha256(value: object, label: str) -> str:
    digest = str(value)
    if len(digest) != SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise MutedControlError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _validate_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MutedControlError(f"{label} must be a nonempty string")
    return value


def _parse_timestamp(value: object, label: str) -> tuple[str, datetime]:
    exact = _validate_string(value, label)
    try:
        parsed = datetime.fromisoformat(exact)
    except ValueError as error:
        raise MutedControlError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise MutedControlError(f"{label} must include an explicit UTC offset")
    return exact, parsed.astimezone(UTC)


def _regular_file_evidence(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise MutedControlError(f"{label} path must be absolute")
    exact = _assert_safe_local_path(path, label=label)
    if exact.is_symlink() or not exact.is_file():
        raise MutedControlError(f"{label} must be a regular non-symlink file")
    before = exact.stat()
    evidence = {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": before.st_size,
    }
    _assert_safe_local_path(exact, label=label)
    after = exact.stat()
    if (after.st_dev, after.st_ino, after.st_size) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
    ):
        raise MutedControlError(f"{label} changed while its file identity was frozen")
    return evidence


def _nearest_existing(path: Path) -> Path:
    current = path.expanduser().absolute()
    while not current.exists():
        if current.parent == current:
            raise MutedControlError("local-storage path has no existing ancestor")
        current = current.parent
    return current


def _assert_local_rpi_storage(path: Path) -> Path:
    exact = path.expanduser().absolute()
    forbidden = (Path("/media"), Path("/mnt"), Path("/run/media"))
    if any(exact == root or root in exact.parents for root in forbidden):
        raise MutedControlError("raw storage must be local Raspberry Pi storage")
    try:
        home_device = os.stat(Path("/home/pi")).st_dev
        storage_device = os.stat(_nearest_existing(exact)).st_dev
    except OSError as error:
        raise MutedControlError("cannot attest local Raspberry Pi storage device") from error
    if storage_device != home_device:
        raise MutedControlError("raw storage device differs from the Raspberry Pi local disk")
    return exact


def _assert_safe_local_path(path: Path, *, label: str) -> Path:
    """Reject symlink ancestry and non-local storage without resolving indirection."""

    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise MutedControlError(f"{label} contains parent traversal")
    exact = expanded.absolute()
    try:
        foundation._assert_path_chain_has_no_symlink(exact, label=label)
        _assert_local_rpi_storage(exact)
    except (OSError, RuntimeError) as error:
        raise MutedControlError(f"{label} failed local no-symlink admission: {error}") from error
    return exact


def _directory_identity(path: Path, *, label: str) -> dict[str, Any]:
    exact = _assert_safe_local_path(path, label=label)
    if exact.is_symlink() or not exact.is_dir():
        raise MutedControlError(f"{label} must be a regular non-symlink directory")
    observed = exact.stat()
    return {
        "path": str(exact),
        "device": int(observed.st_dev),
        "inode": int(observed.st_ino),
    }


def _validate_embedded_file_evidence(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size_bytes"}:
        raise MutedControlError(f"{label} file evidence is incomplete")
    observed = _regular_file_evidence(Path(str(value["path"])), label)
    if dict(value) != observed:
        raise MutedControlError(f"{label} file bytes differ from their declared identity")
    return observed


def _legacy_mute_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("purpose") == purpose
        and value.get("serial") == serial
        and value.get("status") == "passed"
        and value.get("error") is None
    )


def _coherent_rx1_snr_db(
    data_file: Path,
    *,
    sample_count: int,
    sample_rate_hz: float,
    tone_hz: float,
) -> float:
    raw: npt.NDArray[np.int16] = np.memmap(data_file, dtype="<i2", mode="r")
    if raw.size != sample_count * 2 * 2:
        raise MutedControlError("P0 raw artifact is not exact dual-RX CI16")
    components = raw.reshape(sample_count, 2, 2)
    coherent_sum = 0j
    chunk_size = 1_000_000
    for start in range(0, sample_count, chunk_size):
        stop = min(sample_count, start + chunk_size)
        indices: npt.NDArray[np.float64] = np.arange(start, stop, dtype=np.float64)
        values = components[start:stop, 0, 0].astype(np.float64) + 1j * components[
            start:stop, 0, 1
        ].astype(np.float64)
        coherent_sum += complex(
            np.sum(values * np.exp(-2j * np.pi * tone_hz * indices / sample_rate_hz))
        )
    phasor = coherent_sum / sample_count
    residual_sum = 0.0
    for start in range(0, sample_count, chunk_size):
        stop = min(sample_count, start + chunk_size)
        indices = np.arange(start, stop, dtype=np.float64)
        values = components[start:stop, 0, 0].astype(np.float64) + 1j * components[
            start:stop, 0, 1
        ].astype(np.float64)
        baseband = values * np.exp(-2j * np.pi * tone_hz * indices / sample_rate_hz)
        residual_sum += float(np.sum(np.square(np.abs(baseband - phasor))))
    residual_power = residual_sum / sample_count
    if residual_power <= np.finfo(np.float64).tiny:
        raise MutedControlError("P0 pilot SNR residual is zero and cannot be serialized")
    result = 10.0 * math.log10(abs(phasor) ** 2 / residual_power)
    if not math.isfinite(result):
        raise MutedControlError("P0 pilot SNR is not finite")
    return result


def _recompute_p0_schedule_timing(
    data_file: Path,
    metadata: Mapping[str, Any],
    *,
    sample_count: int,
    sample_rate_hz: float,
    tone_hz: float,
    profile_path: Path,
) -> dict[str, Any]:
    """Reopen P0 RX2 IQ and independently decode the Fast20 dwell schedule."""

    raw: npt.NDArray[np.int16] = np.memmap(data_file, dtype="<i2", mode="r")
    if raw.size != sample_count * 2 * 2:
        raise MutedControlError("P0 raw artifact is not exact dual-RX CI16")
    components = raw.reshape(sample_count, 2, 2)
    rx2: npt.NDArray[np.complex64] = np.empty(sample_count, dtype=np.complex64)
    for start in range(0, sample_count, 1_000_000):
        stop = min(sample_count, start + 1_000_000)
        rx2[start:stop].real = components[start:stop, 1, 0]
        rx2[start:stop].imag = components[start:stop, 1, 1]
    continuity = metadata.get("pluto:continuity")
    if not isinstance(continuity, Mapping):
        global_section = metadata.get("global")
        continuity = (
            global_section.get("pluto:continuity") if isinstance(global_section, Mapping) else None
        )
    blocks = continuity.get("blocks") if isinstance(continuity, Mapping) else None
    if not isinstance(blocks, list) or not blocks:
        raise MutedControlError("P0 metadata lacks its raw ABI2 continuity blocks")
    ledger = tuple(
        ContinuityBlock(
            sample_start=int(block["sample_start"]),
            sample_count=int(block["sample_count"]),
            utc_ns=int(block["utc_ns"]),
        )
        for block in blocks
        if isinstance(block, Mapping)
    )
    try:
        dwell = analyze_fast20_dwell_isolation(
            rx2,
            sample_rate_hz=sample_rate_hz,
            tone_offset_hz=tone_hz,
            profile=load_profile(profile_path),
            continuity_ledger=ledger,
            minimum_complete_frames=P0_MINIMUM_COMPLETE_FAST20_CYCLES,
        )
    except (OSError, TypeError, ValueError) as error:
        raise MutedControlError("P0 raw-IQ Fast20 timing reanalysis failed") from error
    finally:
        del rx2
    if (
        not dwell.isolation_verified
        or not dwell.continuity_verified
        or dwell.complete_frame_count < P0_MINIMUM_COMPLETE_FAST20_CYCLES
        or dwell.rejected_marker_count != 0
        or dwell.schedule_timing is None
        or [state.name for state in dwell.states] != [f"ANT{state}" for state in range(1, 9)]
    ):
        raise MutedControlError("P0 raw IQ does not independently prove the Fast20 schedule")
    timing = _json_safe(asdict(dwell.schedule_timing))
    return {
        "schema": 1,
        "evidence_kind": "p0_raw_iq_fast20_schedule_reanalysis_v1",
        "schedule_timing": timing,
        "schedule_timing_sha256": canonical_json_sha256(timing),
        "complete_frame_count": dwell.complete_frame_count,
        "rejected_marker_count": dwell.rejected_marker_count,
        "continuity_verified": dwell.continuity_verified,
        "state_order": [state.name for state in dwell.states],
    }


def _derive_p0_post_cycle_schedule_proof(
    manifest_paths: Sequence[Path],
    *,
    selector_file: Mapping[str, Any],
    selector: Mapping[str, Any],
    profile_path: Path,
    campaign_id: str,
    board_id: str,
    serial: str,
    derivation_source_commit: str,
) -> dict[str, Any]:
    """Recursively admit five post-cycle P0 RF streams and derive their schedule proof."""

    if len(manifest_paths) != 5:
        raise MutedControlError("P0 post-cycle schedule proof requires exactly five manifests")
    commit = foundation._validate_commit(
        derivation_source_commit, "P0 evidence derivation source commit"
    )
    selector_sealed_at, selector_sealed_time = _parse_timestamp(
        selector.get("sealed_at"), "sealed Fast20 completion time"
    )
    source_rows: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    seen_manifest_hashes: set[str] = set()
    seen_artifact_ids: set[str] = set()
    seen_stream_ids: set[int] = set()
    seen_data_hashes: set[str] = set()
    profile_hashes: set[str] = set()
    firmware_hashes: set[str] = set()
    fixture_ids: set[str] = set()
    for index, manifest_path in enumerate(manifest_paths):
        _assert_local_rpi_storage(manifest_path)
        manifest_file = _regular_file_evidence(manifest_path, f"P0 source manifest {index}")
        if manifest_file["sha256"] in seen_manifest_hashes:
            raise MutedControlError("P0 source manifest bytes are reused")
        seen_manifest_hashes.add(str(manifest_file["sha256"]))
        manifest = _read_json(manifest_path, f"P0 source manifest {index}")
        manifest_created_at, manifest_created_time = _parse_timestamp(
            manifest.get("created_at"), f"P0 source manifest creation time {index}"
        )
        if manifest_created_time <= selector_sealed_time:
            raise MutedControlError(
                "P0 manifest was not created after the bound selector power-cycle seal"
            )
        run_id = _validate_string(manifest.get("run_id"), f"P0 source run ID {index}")
        if run_id in seen_run_ids or not run_id.startswith(f"{campaign_id}-p0-paired-r"):
            raise MutedControlError("P0 source run IDs are reused or outside this campaign")
        seen_run_ids.add(run_id)
        configuration = manifest.get("configuration")
        attempts = manifest.get("attempts")
        if (
            manifest.get("schema") != 1
            or manifest.get("experiment_kind")
            != "fast20_fully_conducted_broadband_board_calibration"
            or manifest.get("status") != "awaiting_rotation1"
            or not isinstance(configuration, Mapping)
            or not isinstance(attempts, list)
            or configuration.get("board_id") != board_id
            or configuration.get("serial") != serial
            or configuration.get("frequencies_hz") != [5_700_000_000, 5_800_000_000]
            or configuration.get("sample_rate_hz") != SAMPLE_RATE_HZ
            or configuration.get("receiver_gain_db") != RECEIVER_GAIN_DB
            or configuration.get("duration_s") != TOTAL_SAMPLES / SAMPLE_RATE_HZ
            or configuration.get("kernel_buffers") != KERNEL_BUFFERS
            or configuration.get("profile_id") != "fast20-v1"
            or configuration.get("fully_conducted_required") is not True
            or configuration.get("storage_medium") != "raspberry_pi_local_filesystem"
            or configuration.get("pluto_onboard_storage_used") is not False
            or manifest.get("runner_source_commit") != commit
            or not _legacy_mute_passed(
                manifest.get("final_mute"), serial=serial, purpose="final_rotation0"
            )
        ):
            raise MutedControlError("P0 source manifest failed its exact legacy admission contract")
        matches = [
            attempt
            for attempt in attempts
            if isinstance(attempt, Mapping)
            and attempt.get("center_frequency_hz") == CENTER_FREQUENCY_HZ
            and attempt.get("rotation") == 0
        ]
        if len(matches) != 1:
            raise MutedControlError("P0 manifest lacks one unique Rotation-0 5.8-GHz attempt")
        attempt = matches[0]
        capture_attempt = attempt.get("capture")
        reanalysis = attempt.get("reanalysis")
        quality = attempt.get("quality_result")
        if (
            attempt.get("status") != "complete"
            or attempt.get("outcome") != "quality_passed"
            or attempt.get("error") is not None
            or not _legacy_mute_passed(
                attempt.get("post_mute"), serial=serial, purpose="post_attempt"
            )
            or not isinstance(capture_attempt, Mapping)
            or capture_attempt.get("accepted") is not True
            or not isinstance(reanalysis, Mapping)
            or reanalysis.get("accepted") is not True
            or not isinstance(quality, Mapping)
            or quality.get("quality_passed") is not True
            or quality.get("status") != "passed"
        ):
            raise MutedControlError("P0 5.8-GHz attempt was not quality-admitted and fail-muted")
        analysis_path = Path(str(quality.get("analysis_path", "")))
        _assert_local_rpi_storage(analysis_path)
        analysis_file = _regular_file_evidence(analysis_path, f"P0 source analysis {index}")
        analysis = _read_json(analysis_path, f"P0 source analysis {index}")
        artifact_document = analysis.get("artifact")
        capture = analysis.get("capture")
        pilot = analysis.get("pilot")
        transfer = analysis.get("transfer")
        quality_gate = analysis.get("quality_gate")
        if not all(
            isinstance(value, Mapping)
            for value in (artifact_document, capture, pilot, transfer, quality_gate)
        ):
            raise MutedControlError("P0 normalized analysis sections are malformed")
        assert isinstance(artifact_document, Mapping)
        assert isinstance(capture, Mapping)
        assert isinstance(pilot, Mapping)
        assert isinstance(transfer, Mapping)
        assert isinstance(quality_gate, Mapping)
        artifact = ArtifactSummary.model_validate(artifact_document)
        artifact_root = foundation._assert_tree_has_no_symlink(
            Path(artifact.path), label=f"P0 source artifact {index}"
        )
        data_file = artifact_root / f"{artifact.artifact_id}.sigmf-data"
        metadata_file = artifact_root / f"{artifact.artifact_id}.sigmf-meta"
        _assert_local_rpi_storage(data_file)
        _assert_local_rpi_storage(metadata_file)
        data_evidence = _regular_file_evidence(data_file, f"P0 source raw IQ {index}")
        metadata_evidence = _regular_file_evidence(
            metadata_file, f"P0 source SigMF metadata {index}"
        )
        if artifact.sha256 != data_evidence["sha256"] or not verify_artifact(artifact):
            raise MutedControlError("P0 raw artifact differs from its ArtifactSummary")
        metadata = _read_json(metadata_file, f"P0 source SigMF metadata {index}")
        global_section = metadata.get("global")
        artifact_created_at, artifact_created_time = _parse_timestamp(
            global_section.get("pluto:created_at") if isinstance(global_section, Mapping) else None,
            f"P0 source artifact creation time {index}",
        )
        if artifact_created_time <= selector_sealed_time:
            raise MutedControlError(
                "P0 RF artifact was not created after the bound selector power-cycle seal"
            )
        try:
            continuity = audit_continuity_metadata(
                metadata,
                expected_total_samples=TOTAL_SAMPLES,
                expected_samples_per_block=SAMPLES_PER_FRAME,
                expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
            )
        except ValueError as error:
            raise MutedControlError("P0 source ABI2 continuity failed") from error
        headroom = capture.get("adc_headroom_admission")
        pilot_offset = pilot.get("estimated_offset_hz")
        scales = capture.get("dds_scale_readback")
        aggregation = analysis.get("aggregation_key")
        all_off = transfer.get("all_off")
        states = transfer.get("states")
        schedule_alignment = transfer.get("schedule_alignment")
        selected_alignment = (
            schedule_alignment.get("selected") if isinstance(schedule_alignment, Mapping) else None
        )
        decoded_timing = (
            schedule_alignment.get("decoded_timing")
            if isinstance(schedule_alignment, Mapping)
            else None
        )
        if (
            analysis.get("schema") != 1
            or analysis.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer"
            or analysis.get("source_commit") != commit
            or quality_gate.get("passed") is not True
            or artifact.radio_id != serial
            or artifact.sample_count != TOTAL_SAMPLES
            or artifact.receiver_count != 2
            or artifact.sample_rate_hz != SAMPLE_RATE_HZ
            or capture.get("center_frequency_hz") != CENTER_FREQUENCY_HZ
            or capture.get("sample_rate_hz") != SAMPLE_RATE_HZ
            or capture.get("receiver_gain_db") != RECEIVER_GAIN_DB
            or capture.get("sample_count") != TOTAL_SAMPLES
            or capture.get("samples_per_frame") != SAMPLES_PER_FRAME
            or capture.get("frame_count") != FRAME_COUNT
            or capture.get("kernel_buffers") != KERNEL_BUFFERS
            or capture.get("metadata_abi") != 2
            or capture.get("tx_channel") != 0
            or capture.get("tx_gain_readback_db") != -20.0
            or not isinstance(scales, list)
            or len(scales) != 8
            or scales != [0.25, 0.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0]
            or not isinstance(headroom, Mapping)
            or headroom.get("passed") is not True
            or not isinstance(aggregation, Mapping)
            or aggregation.get("stream_id") != continuity["stream_id"]
            or transfer.get("continuity_verified") is not True
            or transfer.get("complete_cycle_count", 0) < P0_MINIMUM_COMPLETE_FAST20_CYCLES
            or transfer.get("alignment_score", 0.0) < 0.75
            or transfer.get("alignment_even_odd_agreement", 0.0) < 0.75
            or transfer.get("reference_valid_bin_fraction", 0.0) < 0.95
            or not isinstance(schedule_alignment, Mapping)
            or not isinstance(selected_alignment, Mapping)
            or not isinstance(decoded_timing, Mapping)
            or selected_alignment.get("complete_cycle_count")
            != transfer.get("complete_cycle_count")
            or decoded_timing.get("strict_frame_count", 0) < P0_MINIMUM_COMPLETE_FAST20_CYCLES
            or decoded_timing.get("rejected_marker_count") != 0
            or not isinstance(states, list)
            or [item.get("name") for item in states if isinstance(item, Mapping)]
            != [f"ANT{state}" for state in range(1, 9)]
            or isinstance(pilot_offset, bool)
            or not isinstance(pilot_offset, (int, float))
            or not math.isfinite(float(pilot_offset))
            or float(pilot_offset) <= 0.0
        ):
            raise MutedControlError("P0 analysis does not prove the exact Fast20 5.8-GHz stream")
        raw_schedule = _recompute_p0_schedule_timing(
            data_file,
            metadata,
            sample_count=TOTAL_SAMPLES,
            sample_rate_hz=float(SAMPLE_RATE_HZ),
            tone_hz=float(pilot_offset),
            profile_path=profile_path,
        )
        if raw_schedule["schedule_timing"] != dict(decoded_timing):
            raise MutedControlError(
                "stored P0 schedule timing differs from independent raw-IQ reanalysis"
            )
        raw_transfer = all_off.get("raw_rx2_over_rx1") if isinstance(all_off, Mapping) else None
        phasor = raw_transfer.get("phasor") if isinstance(raw_transfer, Mapping) else None
        if (
            not isinstance(raw_transfer, Mapping)
            or not isinstance(phasor, Mapping)
            or not isinstance(raw_transfer.get("amplitude"), (int, float))
            or not isinstance(phasor.get("real"), (int, float))
            or not isinstance(phasor.get("imag"), (int, float))
        ):
            raise MutedControlError("P0 ALL_OFF complex baseline is malformed")
        pilot_snr_db = _coherent_rx1_snr_db(
            data_file,
            sample_count=TOTAL_SAMPLES,
            sample_rate_hz=float(SAMPLE_RATE_HZ),
            tone_hz=float(pilot_offset),
        )
        if pilot_snr_db < P0_MINIMUM_PILOT_SNR_DB:
            raise MutedControlError("P0 source pilot SNR is below 20 dB")
        artifact_id = artifact.artifact_id
        stream_id = int(continuity["stream_id"])
        if (
            artifact_id in seen_artifact_ids
            or stream_id in seen_stream_ids
            or data_evidence["sha256"] in seen_data_hashes
        ):
            raise MutedControlError("P0 sources reuse an artifact, stream, or raw IQ bytes")
        seen_artifact_ids.add(artifact_id)
        seen_stream_ids.add(stream_id)
        seen_data_hashes.add(str(data_evidence["sha256"]))
        profile_hash = _validate_sha256(
            capture.get("profile_contract_sha256"), "P0 Fast20 profile hash"
        )
        firmware_hash = _validate_sha256(
            configuration.get("firmware_binary_sha256"), "P0 Fast20 firmware hash"
        )
        fixture_id = _validate_string(configuration.get("fixture_id"), "P0 fixture ID")
        profile_hashes.add(profile_hash)
        firmware_hashes.add(firmware_hash)
        fixture_ids.add(fixture_id)
        source_rows.append(
            {
                "run_id": run_id,
                "manifest": manifest_file,
                "manifest_created_at": manifest_created_at,
                "analysis": analysis_file,
                "artifact_id": artifact_id,
                "stream_id": stream_id,
                "data": data_evidence,
                "metadata": metadata_evidence,
                "artifact_created_at": artifact_created_at,
                "runner_source_commit": manifest["runner_source_commit"],
                "analysis_source_commit": analysis["source_commit"],
                "schedule_alignment_sha256": canonical_json_sha256(schedule_alignment),
                "raw_schedule_reanalysis": raw_schedule,
                "strict_frame_count": int(decoded_timing["strict_frame_count"]),
                "rejected_marker_count": int(decoded_timing["rejected_marker_count"]),
                "pilot_offset_hz": float(pilot_offset),
                "pilot_snr_db": pilot_snr_db,
                "all_off_amplitude": float(raw_transfer["amplitude"]),
                "all_off_phasor": {
                    "real": float(phasor["real"]),
                    "imag": float(phasor["imag"]),
                },
            }
        )
    if any(len(values) != 1 for values in (profile_hashes, firmware_hashes, fixture_ids)):
        raise MutedControlError("P0 sources differ in profile, firmware, or fixture identity")
    pilot_offsets = np.asarray([row["pilot_offset_hz"] for row in source_rows], dtype=float)
    amplitudes = np.asarray([row["all_off_amplitude"] for row in source_rows], dtype=float)
    amplitude_cv = float(np.std(amplitudes) / np.mean(amplitudes))
    phases = np.angle(
        np.asarray(
            [
                complex(row["all_off_phasor"]["real"], row["all_off_phasor"]["imag"])
                for row in source_rows
            ],
            dtype=np.complex128,
        )
    )
    resultant: float = min(1.0, float(abs(np.mean(np.exp(1j * phases)))))
    circular_tiny = float(np.finfo(np.float64).tiny)
    circular_phase_std_deg = math.degrees(
        math.sqrt(max(0.0, -2.0 * math.log(max(resultant, circular_tiny))))
    )
    if (
        amplitude_cv > P0_MAXIMUM_AMPLITUDE_CV
        or circular_phase_std_deg > P0_MAXIMUM_CIRCULAR_PHASE_STD_DEG
    ):
        raise MutedControlError("P0 cohort fails amplitude or circular-phase repeatability")
    selector_run_id = _validate_string(selector.get("run_id"), "Fast20 flash run ID")
    selector_startup = selector.get("startup")
    selector_operators = selector.get("operator_attestations")
    if (
        not isinstance(selector_startup, Mapping)
        or selector_startup.get("autonomous_schedule_timing_proven") is not False
        or selector_startup.get("runtime_gpio_sequence_proven") is not False
        or not isinstance(selector_operators, Mapping)
    ):
        raise MutedControlError("sealed Fast20 evidence overclaims autonomous schedule timing")
    power_cycle = _validate_embedded_file_evidence(
        selector_operators.get("power_cycle_snapshot"),
        "sealed Fast20 power-cycle attestation",
    )
    return {
        "schema": 1,
        "evidence_kind": P0_POST_CYCLE_SCHEDULE_PROOF_KIND,
        "campaign_id": campaign_id,
        "board_id": board_id,
        "pluto_serial": serial,
        "derivation_source_commit": commit,
        "selector_flash_evidence": dict(selector_file),
        "selector_flash_run_id": selector_run_id,
        "selector_sealed_at": selector_sealed_at,
        "selector_power_cycle_attestation": power_cycle,
        "source_count": 5,
        "source_run_ids": [row["run_id"] for row in source_rows],
        "source_manifest_sha256s": [row["manifest"]["sha256"] for row in source_rows],
        "source_manifest_created_at": [row["manifest_created_at"] for row in source_rows],
        "source_manifests": [row["manifest"] for row in source_rows],
        "source_analyses": [row["analysis"] for row in source_rows],
        "source_artifact_ids": [row["artifact_id"] for row in source_rows],
        "source_stream_ids": [row["stream_id"] for row in source_rows],
        "source_data_sha256s": [row["data"]["sha256"] for row in source_rows],
        "source_metadata_sha256s": [row["metadata"]["sha256"] for row in source_rows],
        "source_artifact_created_at": [row["artifact_created_at"] for row in source_rows],
        "all_source_artifacts_created_after_selector_seal": True,
        "source_runner_commits": [row["runner_source_commit"] for row in source_rows],
        "source_analysis_commits": [row["analysis_source_commit"] for row in source_rows],
        "source_schedule_alignment_sha256s": [
            row["schedule_alignment_sha256"] for row in source_rows
        ],
        "source_raw_schedule_reanalyses": [row["raw_schedule_reanalysis"] for row in source_rows],
        "source_strict_frame_counts": [row["strict_frame_count"] for row in source_rows],
        "source_rejected_marker_counts": [row["rejected_marker_count"] for row in source_rows],
        "source_pilot_offsets_hz": pilot_offsets.tolist(),
        "source_pilot_snr_db": [row["pilot_snr_db"] for row in source_rows],
        "pilot_offset_hz": float(np.median(pilot_offsets)),
        "aggregation": "median_of_actual_positive_pilot_readbacks",
        "minimum_pilot_snr_db": float(min(row["pilot_snr_db"] for row in source_rows)),
        "all_off_amplitude_cv": amplitude_cv,
        "all_off_circular_phase_std_deg": circular_phase_std_deg,
        "cohort_quality_passed": True,
        "fast20_schedule_verified_in_all_sources": True,
        "schedule_timing_proven_by": (
            "five_recursively_admitted_and_independently_reanalyzed_post_cycle_p0_rf_artifacts"
        ),
        "selector_flash_attestation_proves_schedule_timing": False,
        "profile_contract_sha256": next(iter(profile_hashes)),
        "firmware_binary_sha256": next(iter(firmware_hashes)),
        "legacy_fixture_id": next(iter(fixture_ids)),
    }


def _fixture_evidence_from_files(
    fixture_path: Path,
    setup_path: Path,
    selector_path: Path,
    p0_manifest_paths: Sequence[Path],
    *,
    run_id: str,
    board_id: str,
    serial: str,
    derivation_source_commit: str,
) -> dict[str, Any]:
    fixture_file = _regular_file_evidence(fixture_path, "fixture manifest")
    setup_file = _regular_file_evidence(setup_path, "setup attestation")
    selector_file = _regular_file_evidence(selector_path, "Fast20 selector evidence")
    fixture = _read_json(fixture_path, "fixture manifest")
    setup = _read_json(setup_path, "setup attestation")
    selector = _read_json(selector_path, "Fast20 selector evidence")
    _assert_no_unresolved_placeholders(fixture, label="fixture manifest")
    _assert_no_unresolved_placeholders(setup, label="setup attestation")
    if len(p0_manifest_paths) != 5:
        raise MutedControlError("P1 requires exactly five post-cycle P0 manifests")
    p0_manifest_files = [
        _regular_file_evidence(path, f"post-cycle P0 manifest {index}")
        for index, path in enumerate(p0_manifest_paths)
    ]

    required_fixture = {
        "schema",
        "fixture_kind",
        "campaign_id",
        "fixture_id",
        "p0_legacy_fixture_id",
        "board_id",
        "pluto_serial",
        "topology_token",
        "no_antennas",
        "tx1_path",
        "tx2_state",
        "rx1_state",
        "rx2_state",
        "selector_mode",
        "component_ids",
        "connection_ids",
    }
    if (
        set(fixture) != required_fixture
        or fixture.get("schema") != 1
        or fixture.get("fixture_kind") != FIXTURE_KIND
        or fixture.get("board_id") != board_id
        or fixture.get("pluto_serial") != serial
        or fixture.get("topology_token") != TOPOLOGY_TOKEN
        or fixture.get("no_antennas") is not True
        or fixture.get("tx1_path") != "matched_conducted_full_fixture"
        or fixture.get("tx2_state") != "50ohm_terminated"
        or fixture.get("rx1_state") != "protected_conducted_reference"
        or fixture.get("rx2_state") != "selector_common_full_fixture"
        or fixture.get("selector_mode") != "fast20"
    ):
        raise MutedControlError("fixture manifest does not describe the exact untouched P1 fixture")
    for field in ("component_ids", "connection_ids"):
        values = fixture.get(field)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item for item in values)
            or len(values) != len(set(values))
        ):
            raise MutedControlError(f"fixture {field} must be a nonempty unique string array")

    setup_required = {
        "schema",
        "attestation_kind",
        "attestation_id",
        "run_id",
        "campaign_id",
        "board_id",
        "pluto_serial",
        "fixture_manifest_sha256",
        "setup_evidence",
        "no_component_or_connection_movement",
        "selector_flash_evidence_sha256",
        "p0_source_manifest_sha256s",
    }
    if (
        set(setup) != setup_required
        or setup.get("schema") != 1
        or setup.get("attestation_kind") != SETUP_KIND
        or setup.get("run_id") != run_id
        or setup.get("campaign_id") != fixture.get("campaign_id")
        or setup.get("board_id") != board_id
        or setup.get("pluto_serial") != serial
        or setup.get("fixture_manifest_sha256") != fixture_file["sha256"]
        or setup.get("selector_flash_evidence_sha256") != selector_file["sha256"]
        or setup.get("p0_source_manifest_sha256s") != [item["sha256"] for item in p0_manifest_files]
        or setup.get("no_component_or_connection_movement") is not True
    ):
        raise MutedControlError("setup attestation is not bound to this exact P1 run/fixture")
    setup_evidence = _validate_embedded_file_evidence(
        setup.get("setup_evidence"), "setup photograph/evidence"
    )

    if selector.get("evidence_kind") != SEALED_SELECTOR_EVIDENCE_KIND:
        raise MutedControlError("P1 requires recursively sealed Fast20 selector evidence")
    flash_run_id = _validate_string(selector.get("run_id"), "Fast20 flash run ID")
    try:
        selector = validate_sealed_selector_evidence(
            Path(str(selector_file["path"])),
            expected_sha256=str(selector_file["sha256"]),
            expected_campaign_id=str(fixture["campaign_id"]),
            expected_run_id=flash_run_id,
            expected_board_id=board_id,
            expected_image_role="fast20",
        )
    except SelectorFlashError as error:
        raise MutedControlError(f"sealed Fast20 selector evidence failed: {error}") from error
    frozen_inputs = selector.get("frozen_inputs")
    if not isinstance(frozen_inputs, Mapping):
        raise MutedControlError("sealed Fast20 evidence lacks frozen inputs")
    frozen_files = frozen_inputs.get("files")
    control_profile = frozen_inputs.get("control_profile")
    target_readback = selector.get("target_flash_readback")
    startup = selector.get("startup")
    if (
        not isinstance(frozen_files, Mapping)
        or not isinstance(control_profile, Mapping)
        or not isinstance(target_readback, Mapping)
        or not isinstance(startup, Mapping)
        or control_profile.get("id") != "fast20-v1"
        or startup.get("evidence_kind") != "fast20_exact_image_reset_run_identity_v1"
        or startup.get("autonomous_schedule_timing_proven") is not False
        or startup.get("runtime_gpio_sequence_proven") is not False
    ):
        raise MutedControlError("sealed evidence does not identify the reviewed Fast20 image")
    profile_evidence = _validate_embedded_file_evidence(
        frozen_files.get("profile"), "sealed Fast20 profile"
    )
    firmware_evidence = _validate_embedded_file_evidence(
        frozen_files.get("firmware_bin"), "sealed Fast20 firmware BIN"
    )
    readback_evidence = _validate_embedded_file_evidence(
        {key: target_readback.get(key) for key in ("path", "sha256", "size_bytes")},
        "sealed Fast20 target readback",
    )
    if (
        firmware_evidence["sha256"] != readback_evidence["sha256"]
        or firmware_evidence["size_bytes"] != readback_evidence["size_bytes"]
    ):
        raise MutedControlError("Fast20 target readback differs from the complete firmware BIN")
    profile_document = _read_json(Path(str(profile_evidence["path"])), "Fast20 control profile")
    profile_identity = profile_document.get("profile")
    profile_contract_sha256 = profile_document.get("contract_sha256")
    if (
        profile_document.get("schema") != 1
        or not isinstance(profile_identity, Mapping)
        or profile_identity.get("id") != "fast20-v1"
        or _validate_sha256(profile_contract_sha256, "Fast20 profile contract hash")
        != profile_contract_sha256
    ):
        raise MutedControlError("Fast20 profile file lacks its reviewed contract identity")

    schedule_proof = _derive_p0_post_cycle_schedule_proof(
        p0_manifest_paths,
        selector_file=selector_file,
        selector=selector,
        profile_path=Path(str(profile_evidence["path"])),
        campaign_id=str(fixture["campaign_id"]),
        board_id=board_id,
        serial=serial,
        derivation_source_commit=derivation_source_commit,
    )
    if (
        fixture.get("p0_legacy_fixture_id") != schedule_proof["legacy_fixture_id"]
        or schedule_proof["profile_contract_sha256"] != profile_contract_sha256
        or schedule_proof["firmware_binary_sha256"] != firmware_evidence["sha256"]
    ):
        raise MutedControlError("P0 fixture/profile/firmware identity differs from current P1")
    pilot_offset = schedule_proof["pilot_offset_hz"]
    # This proves the target and both 10-kHz controls avoid DC and edge exclusions.
    _validate_window_geometry(SAMPLE_RATE_HZ, float(pilot_offset))
    cohort_fixture_identity = {
        "schema": 1,
        "evidence_kind": "5g8_p1_cohort_fixture_identity",
        "campaign_id": fixture["campaign_id"],
        "board_id": board_id,
        "serial": serial,
        "fixture_manifest_file": fixture_file,
        "sealed_selector_flash_evidence_file": selector_file,
        "p0_post_cycle_schedule_proof_sha256": canonical_json_sha256(schedule_proof),
    }
    return {
        "schema": 1,
        "evidence_kind": "5g8_p1_fixture_setup_selector_and_p0_binding",
        "campaign_id": fixture["campaign_id"],
        "run_id": run_id,
        "board_id": board_id,
        "serial": serial,
        "fixture_manifest": fixture,
        "fixture_manifest_file": fixture_file,
        "setup_attestation": setup,
        "setup_attestation_file": setup_file,
        "setup_evidence": setup_evidence,
        "sealed_selector_flash_evidence": selector,
        "sealed_selector_flash_evidence_file": selector_file,
        "selector_evidence_format": "sealed_selector_flash_attestation_v1",
        "fast20_profile": profile_evidence,
        "fast20_firmware_bin": firmware_evidence,
        "fast20_target_readback": readback_evidence,
        "p0_post_cycle_schedule_proof": schedule_proof,
        "p0_post_cycle_schedule_proof_sha256": canonical_json_sha256(schedule_proof),
        "p0_source_manifest_files": p0_manifest_files,
        "pilot_offset_hz": float(pilot_offset),
        "cohort_fixture_identity": cohort_fixture_identity,
        "cohort_fixture_identity_sha256": canonical_json_sha256(cohort_fixture_identity),
    }


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    source_commit: str,
    dependency_attestation: Mapping[str, Any],
    native_attestation: Mapping[str, Any],
    fixture_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    run = foundation._validate_identifier(run_id, "run ID")
    board = foundation._validate_identifier(board_id, "board ID")
    exact_serial = foundation._validate_serial(serial)
    exact_uri = foundation._validate_usb_uri(uri)
    commit = foundation._validate_commit(source_commit, "Smateway source commit")
    dependency = foundation._validate_dependency_source_attestation(dependency_attestation)
    imported_dependency_modules = {
        item.get("module") for item in dependency["imported_modules"] if isinstance(item, Mapping)
    }
    if "pluto_plus.tandem" not in imported_dependency_modules:
        raise MutedControlError("pluto-plus-utils tandem import origin is not attested")
    native = validate_runtime_attestation(native_attestation)
    evidence = _json_safe(dict(fixture_evidence))
    if (
        not isinstance(evidence, dict)
        or evidence.get("run_id") != run
        or evidence.get("board_id") != board
        or evidence.get("serial") != exact_serial
    ):
        raise MutedControlError("fixture evidence differs from the requested run identity")
    p0_proof = evidence.get("p0_post_cycle_schedule_proof")
    if not isinstance(p0_proof, Mapping) or p0_proof.get("derivation_source_commit") != commit:
        raise MutedControlError(
            "P0 post-cycle schedule proof was not derived by this Smateway revision"
        )
    pilot_offset_hz = float(evidence["pilot_offset_hz"])
    board_root = _assert_local_rpi_storage(foundation._board_root(board))
    return {
        "schema": 1,
        "plan_kind": "5g8_true_tx_muted_dual_rx_control",
        "run_id": run,
        "campaign_id": evidence["campaign_id"],
        "board_id": board,
        "source": {
            "smateway_commit": commit,
            "pluto_plus_utils_source_attestation": dependency,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
            "native_libiio_runtime_attestation": native,
            "native_libiio_runtime_attestation_sha256": canonical_json_sha256(native),
            "capture_api": "pluto_plus.hardware.IioRadioDevice.begin_metadata_capture",
            "analysis": "smateway.muted_control.analyze_muted_stream",
        },
        "fixture_evidence": evidence,
        "fixture_evidence_sha256": canonical_json_sha256(evidence),
        "configuration": {
            "serial": exact_serial,
            "uri": exact_uri,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "channels": [0, 1],
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": TOTAL_SAMPLES,
            "duration_s": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
            "kernel_buffers": KERNEL_BUFFERS,
            "metadata_abi": 2,
            "pilot_offset_hz_from_p0": pilot_offset_hz,
            "tx_hardware_gain_db_required": [-80.0, -80.0],
            "dds_raw_required": [0.0] * 8,
            "dds_scale_required": [0.0] * 8,
            "one_stream_per_run_id": True,
            "cohort_run_count": 5,
            "automatic_retry_count": 0,
        },
        "operator_confirmations_required": {
            "topology_token": TOPOLOGY_TOKEN,
            "no_antennas": True,
            "tx1_untouched_conducted_fixture": True,
            "tx2_physically_50ohm_terminated": True,
            "rx1_protection_present": True,
            "no_movement_since_setup_evidence": True,
            "sealed_fast20_image_unchanged_since_p0": True,
            "post_cycle_p0_rf_schedule_proof_bound": True,
            "fixture_evidence_sha256": canonical_json_sha256(evidence),
        },
        "safety": {
            "both_tx_gains_exact_minus80db": True,
            "all_eight_dds_raw_and_scale_exact_zero": True,
            "receive_only_capture_api": True,
            "pre_capture_exact_mute_required": True,
            "post_capture_exact_mute_required": True,
            "final_exact_mute_required": True,
            "ABI2_continuity_required": True,
            "clipping_forbidden": True,
            "failed_run_id_burned": True,
            "raw_storage_local_rpi_only": True,
        },
        "storage": {
            "medium": "raspberry_pi_local_filesystem",
            "run_root": str(board_root / "5g8-muted-control" / run),
            "run_capture_root": str(board_root / "pluto-usb-captures" / "muted-control-runs" / run),
            "estimated_raw_iq_bytes": TOTAL_SAMPLES * 2 * 2 * np.dtype("<i2").itemsize,
            "pluto_onboard_storage_used": False,
        },
        "analysis_contract": {
            "rx_channels_analyzed_independently": True,
            "transfer_phasor_forbidden": True,
            "p0_post_cycle_schedule_proof_sha256": evidence["p0_post_cycle_schedule_proof_sha256"],
            "target_window_hz": [pilot_offset_hz - 2_000.0, pilot_offset_hz + 2_000.0],
            "dc_lo_exclusion_abs_hz": 5_000.0,
            "conjugate_image_window_hz": [
                -pilot_offset_hz - 2_000.0,
                -pilot_offset_hz + 2_000.0,
            ],
            "filter_edge_exclusion_abs_hz": 350_000.0,
            "control_window_width_hz": 10_000.0,
            "control_window_centers_hz": [
                pilot_offset_hz - 15_000.0,
                pilot_offset_hz + 15_000.0,
            ],
            "narrowband_threshold_db": 10.0,
            "cohort_vote_required": 4,
            "peak_alignment_bins": 2,
            "target_floor_elevation_threshold_db": 3.0,
        },
        "conditions": [
            {
                "condition_id": f"{run}-muted-stream",
                "tx_source_active": False,
                "sample_count": TOTAL_SAMPLES,
                "fresh_stream_required": True,
            }
        ],
    }


def _plan_envelope(contract: Mapping[str, Any], *, run_root: Path) -> dict[str, Any]:
    frozen = _json_safe(dict(contract))
    return {
        "schema": 1,
        "plan_contract": frozen,
        "plan_contract_sha256": canonical_json_sha256(frozen),
        "plan_contract_hash_provenance": (
            "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
        ),
        "run_directory_identity": _directory_identity(
            run_root, label="muted-control run directory"
        ),
        "immutable": True,
    }


def _validate_plan_envelope(
    document: Mapping[str, Any], *, expected_contract: Mapping[str, Any]
) -> dict[str, Any]:
    contract = document.get("plan_contract")
    storage = expected_contract.get("storage")
    run_root = Path(str(storage.get("run_root", ""))) if isinstance(storage, Mapping) else Path("")
    if (
        document.get("schema") != 1
        or document.get("immutable") is not True
        or not isinstance(contract, Mapping)
        or document.get("plan_contract_sha256") != canonical_json_sha256(contract)
        or document.get("plan_contract_hash_provenance")
        != "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
        or dict(contract) != dict(expected_contract)
        or document.get("run_directory_identity")
        != _directory_identity(run_root, label="muted-control run directory")
    ):
        raise MutedControlError("requested execution differs from the immutable muted-control plan")
    return dict(document)


def _plan_evidence(path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    exact = _assert_safe_local_path(path, label="immutable muted-control plan")
    if exact.is_symlink() or not exact.is_file():
        raise MutedControlError("immutable muted-control plan must be a regular file")
    return {
        "path": str(exact),
        "plan_contract_sha256": envelope["plan_contract_sha256"],
        "plan_file_sha256": sha256_path(exact),
    }


def _reservation_path(manifest_path: Path, *, run_id: str) -> Path:
    return manifest_path.parent.parent / RUN_RESERVATION_DIRECTORY / f"{run_id}.json"


def _execution_burn_path(manifest_path: Path, *, run_id: str) -> Path:
    return (
        manifest_path.parent.parent / RUN_RESERVATION_DIRECTORY / f"{run_id}{EXECUTION_BURN_SUFFIX}"
    )


def _reservation_document(
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    contract = envelope.get("plan_contract")
    if not isinstance(contract, Mapping):
        raise MutedControlError("immutable plan contract is malformed")
    run_id = _validate_string(contract.get("run_id"), "muted-control run ID")
    plan_path = manifest_path.parent / PLAN_FILENAME
    return {
        "schema": 1,
        "marker_kind": "5g8_muted_control_run_id_reservation",
        "run_id": run_id,
        "run_directory_identity": _directory_identity(
            manifest_path.parent, label="muted-control run directory"
        ),
        "plan_path": str(_assert_safe_local_path(plan_path, label="immutable plan")),
        "plan_file_sha256": sha256_path(plan_path),
        "plan_contract_sha256": envelope.get("plan_contract_sha256"),
        "manifest_path": str(
            _assert_safe_local_path(manifest_path, label="muted-control manifest")
        ),
        "reserved_at": _now(),
        "run_id_reserved_outside_run_directory": True,
        "replacement_or_replay_forbidden": True,
    }


def _validate_run_reservation(
    manifest_path: Path,
    *,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = _validate_string(manifest.get("run_id"), "muted-control run ID")
    path = _reservation_path(manifest_path, run_id=run_id)
    _assert_safe_local_path(path, label="muted-control run reservation")
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise MutedControlError(
            "muted-control run reservation must be regular, non-symlink, and read-only"
        )
    document = _read_json(path, "muted-control run reservation")
    immutable_plan = manifest.get("immutable_plan")
    expected_identity = _directory_identity(
        manifest_path.parent, label="muted-control run directory"
    )
    if (
        document.get("schema") != 1
        or document.get("marker_kind") != "5g8_muted_control_run_id_reservation"
        or document.get("run_id") != run_id
        or document.get("run_directory_identity") != expected_identity
        or not isinstance(immutable_plan, Mapping)
        or document.get("plan_path") != immutable_plan.get("path")
        or document.get("plan_file_sha256") != immutable_plan.get("plan_file_sha256")
        or document.get("plan_contract_sha256") != immutable_plan.get("plan_contract_sha256")
        or document.get("manifest_path") != str(manifest_path.expanduser().absolute())
        or document.get("run_id_reserved_outside_run_directory") is not True
        or document.get("replacement_or_replay_forbidden") is not True
    ):
        raise MutedControlError("muted-control run reservation identity differs")
    evidence = {
        "path": str(path),
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
    }
    if manifest.get("run_state_reservation") != evidence:
        raise MutedControlError("manifest run reservation binding differs")
    return evidence


def _validate_execution_burn(
    manifest_path: Path,
    *,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = _validate_string(manifest.get("run_id"), "muted-control run ID")
    path = _execution_burn_path(manifest_path, run_id=run_id)
    _assert_safe_local_path(path, label="muted-control execution burn")
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise MutedControlError(
            "muted-control execution burn must be regular, non-symlink, and read-only"
        )
    document = _read_json(path, "muted-control execution burn")
    reservation = manifest.get("run_state_reservation")
    immutable_plan = manifest.get("immutable_plan")
    if (
        document.get("schema") != 1
        or document.get("marker_kind") != "5g8_muted_control_execution_started_burn"
        or document.get("run_id") != run_id
        or document.get("run_directory_identity")
        != _directory_identity(manifest_path.parent, label="muted-control run directory")
        or document.get("run_state_reservation") != reservation
        or document.get("immutable_plan") != immutable_plan
        or document.get("manifest_path") != str(manifest_path.expanduser().absolute())
        or document.get("run_id_burned") is not True
        or document.get("automatic_retry_forbidden") is not True
        or document.get("manifest_rollback_cannot_restore_execution") is not True
    ):
        raise MutedControlError("muted-control execution burn identity differs")
    receipt = {
        "path": str(path),
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
        "document": document,
    }
    if manifest.get("execution_started") != receipt:
        raise MutedControlError("manifest execution-burn receipt differs")
    return receipt


def _burn_execution_run_id(
    manifest_path: Path,
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Atomically and durably burn one reserved run ID exactly once."""

    _validate_run_reservation(manifest_path, manifest=manifest)
    run_id = _validate_string(manifest.get("run_id"), "muted-control run ID")
    path = _execution_burn_path(manifest_path, run_id=run_id)
    _assert_safe_local_path(path, label="muted-control execution burn")
    if path.exists() or path.is_symlink():
        raise MutedControlError("muted-control run ID was already executed and cannot re-enter")
    document = {
        "schema": 1,
        "marker_kind": "5g8_muted_control_execution_started_burn",
        "run_id": run_id,
        "run_directory_identity": _directory_identity(
            manifest_path.parent, label="muted-control run directory"
        ),
        "run_state_reservation": manifest.get("run_state_reservation"),
        "immutable_plan": manifest.get("immutable_plan"),
        "manifest_path": str(manifest_path.expanduser().absolute()),
        "started_at": _now(),
        "run_id_burned": True,
        "automatic_retry_forbidden": True,
        "manifest_rollback_cannot_restore_execution": True,
    }
    foundation._write_immutable_json(path, document)
    _assert_safe_local_path(path, label="muted-control execution burn")
    receipt = {
        "path": str(path),
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
        "document": _read_json(path, "muted-control execution burn"),
    }
    manifest["execution_started"] = receipt
    _validate_execution_burn(manifest_path, manifest=manifest)
    return receipt


def _new_manifest(
    plan_path: Path,
    envelope: Mapping[str, Any],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    return {
        "schema": 1,
        "run_kind": "5g8_true_tx_muted_dual_rx_control",
        "run_id": contract["run_id"],
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "immutable_plan": _plan_evidence(plan_path, envelope),
        "run_state_reservation": dict(reservation),
        "execution_started": None,
        "confirmation": None,
        "native_runtime_preflight": None,
        "fixture_preflight": None,
        "identity_preflight": None,
        "pre_capture_mute": None,
        "attempt": None,
        "final_mute_attempts": [],
        "final_mute": None,
        "error": None,
    }


def _tombstone_path(manifest_path: Path) -> Path:
    return manifest_path.parent / FAILURE_TOMBSTONE_FILENAME


def _validate_tombstone(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    exact = _assert_safe_local_path(path, label="muted-control failure tombstone")
    if exact.is_symlink() or not exact.is_file() or exact.stat().st_mode & 0o222:
        raise MutedControlError(
            "muted-control tombstone must be regular, non-symlink, and read-only"
        )
    document = _read_json(exact, "muted-control failure tombstone")
    if (
        document.get("schema") != 1
        or document.get("marker_kind") != "5g8_muted_control_failed_run_tombstone"
        or document.get("run_id") != manifest.get("run_id")
        or document.get("immutable_plan") != manifest.get("immutable_plan")
        or document.get("run_state_reservation") != manifest.get("run_state_reservation")
        or document.get("execution_started") != manifest.get("execution_started")
        or document.get("retry_forbidden") is not True
    ):
        raise MutedControlError("muted-control failure tombstone identity differs")
    return document


def _ensure_tombstone(manifest_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    path = _tombstone_path(manifest_path)
    _assert_safe_local_path(path, label="muted-control failure tombstone")
    if path.exists() or path.is_symlink():
        return _validate_tombstone(path, manifest)
    document = {
        "schema": 1,
        "marker_kind": "5g8_muted_control_failed_run_tombstone",
        "run_id": manifest.get("run_id"),
        "immutable_plan": manifest.get("immutable_plan"),
        "run_state_reservation": manifest.get("run_state_reservation"),
        "execution_started": manifest.get("execution_started"),
        "first_failed_at": _now(),
        "manifest_path": str(manifest_path),
        "first_failure_error": _json_safe(manifest.get("error")),
        "retry_forbidden": True,
    }
    foundation._write_immutable_json(path, document)
    _assert_safe_local_path(path, label="muted-control failure tombstone")
    return _validate_tombstone(path, manifest)


def _persist_manifest(path: Path, manifest: dict[str, Any]) -> None:
    exact = _assert_safe_local_path(path, label="muted-control manifest")
    if exact.parent != path.expanduser().absolute().parent:
        raise MutedControlError("muted-control manifest path changed by indirection")
    _validate_run_reservation(exact, manifest=manifest)
    run_id = _validate_string(manifest.get("run_id"), "muted-control run ID")
    burn = _execution_burn_path(exact, run_id=run_id)
    if manifest.get("execution_started") is None:
        if burn.exists() or burn.is_symlink():
            raise MutedControlError("external execution burn forbids manifest rollback")
    else:
        _validate_execution_burn(exact, manifest=manifest)
    manifest["updated_at"] = _now()
    tombstone = _tombstone_path(exact)
    if tombstone.exists() or tombstone.is_symlink():
        _validate_tombstone(tombstone, manifest)
        if manifest.get("status") != "failed":
            raise MutedControlError("failure tombstone forbids manifest rollback")
    elif manifest.get("status") == "failed":
        _ensure_tombstone(exact, manifest)
    write_json_atomic(exact, manifest)
    _assert_safe_local_path(exact, label="muted-control manifest")
    if exact.is_symlink() or not exact.is_file():
        raise MutedControlError("muted-control manifest persistence escaped its run directory")
    _validate_run_reservation(exact, manifest=manifest)
    if manifest.get("execution_started") is not None:
        _validate_execution_burn(exact, manifest=manifest)


def _prepare_plan_only(
    *, plan_path: Path, manifest_path: Path, contract: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture_root = Path(str(contract["storage"]["run_capture_root"]))
    plan_path = _assert_safe_local_path(plan_path, label="muted-control immutable plan")
    manifest_path = _assert_safe_local_path(manifest_path, label="muted-control manifest")
    capture_root = _assert_safe_local_path(capture_root, label="muted-control capture root")
    run_id = _validate_string(contract.get("run_id"), "muted-control run ID")
    reservation_path = _reservation_path(manifest_path, run_id=run_id)
    burn_path = _execution_burn_path(manifest_path, run_id=run_id)
    _assert_safe_local_path(reservation_path, label="muted-control run reservation")
    _assert_safe_local_path(burn_path, label="muted-control execution burn")
    if _tombstone_path(manifest_path).exists() or _tombstone_path(manifest_path).is_symlink():
        raise MutedControlError("failed run ID cannot be planned again")
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file() or not plan_path.is_file():
            raise MutedControlError("existing muted-control run state is incomplete")
        envelope = _validate_plan_envelope(
            _read_json(plan_path, "immutable plan"), expected_contract=contract
        )
        manifest = _read_json(manifest_path, "run manifest")
        if manifest.get("immutable_plan") != _plan_evidence(plan_path, envelope):
            raise MutedControlError("manifest no longer binds the immutable plan")
        _validate_run_reservation(manifest_path, manifest=manifest)
        return envelope, manifest
    if (
        plan_path.parent.exists()
        or plan_path.parent.is_symlink()
        or capture_root.exists()
        or capture_root.is_symlink()
        or reservation_path.exists()
        or reservation_path.is_symlink()
        or burn_path.exists()
        or burn_path.is_symlink()
    ):
        raise MutedControlError("run ID already has plan, state, or capture history")
    plan_path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    _assert_safe_local_path(plan_path.parent, label="muted-control run directory")
    envelope = _plan_envelope(contract, run_root=plan_path.parent)
    foundation._write_immutable_json(plan_path, envelope)
    _assert_safe_local_path(plan_path, label="muted-control immutable plan")
    if plan_path.is_symlink() or not plan_path.is_file() or plan_path.stat().st_mode & 0o222:
        raise MutedControlError("muted-control plan was not sealed as a regular read-only file")
    reservation_document = _reservation_document(manifest_path, envelope=envelope)
    foundation._write_immutable_json(reservation_path, reservation_document)
    _assert_safe_local_path(reservation_path, label="muted-control run reservation")
    reservation = {
        "path": str(reservation_path),
        "sha256": sha256_path(reservation_path),
        "size_bytes": reservation_path.stat().st_size,
    }
    manifest = _new_manifest(plan_path, envelope, reservation=reservation)
    _validate_run_reservation(manifest_path, manifest=manifest)
    return envelope, manifest


def _exact_tx_state_from_device(device: Any) -> dict[str, Any]:
    gains = [float(getattr(device, f"tx_hardwaregain_chan{channel}")) for channel in (0, 1)]
    scales = [float(value) for value in device.dds_scales]
    enabled = [str(value).strip().lower() not in {"0", "false"} for value in device.dds_enabled]
    dds_device = getattr(device, "_dds", None)
    channels = getattr(dds_device, "channels", ())
    raw_by_index: dict[int, float] = {}
    for channel in channels:
        identifier = str(getattr(channel, "id", ""))
        if not identifier.startswith("altvoltage"):
            continue
        suffix = identifier.removeprefix("altvoltage")
        if not suffix.isdigit():
            continue
        raw_attribute = getattr(channel, "attrs", {}).get("raw")
        if raw_attribute is not None:
            raw_by_index[int(suffix)] = float(raw_attribute.value)
    raws = [raw_by_index[index] for index in range(8)] if set(raw_by_index) == set(range(8)) else []
    return {
        "tx_hardware_gain_db_by_channel": gains,
        "dds_raw_readback": raws,
        "dds_scale_readback": scales,
        "dds_enabled_readback": enabled,
    }


def _zero_dds_raw(device: Any) -> None:
    dds_device = getattr(device, "_dds", None)
    channels = getattr(dds_device, "channels", ())
    found: set[int] = set()
    for channel in channels:
        identifier = str(getattr(channel, "id", ""))
        suffix = identifier.removeprefix("altvoltage")
        if not identifier.startswith("altvoltage") or not suffix.isdigit():
            continue
        index = int(suffix)
        raw_attribute = getattr(channel, "attrs", {}).get("raw")
        scale_attribute = getattr(channel, "attrs", {}).get("scale")
        if raw_attribute is None or scale_attribute is None:
            continue
        raw_attribute.value = "0"
        scale_attribute.value = "0"
        found.add(index)
    if found != set(range(8)):
        raise MutedControlError("cannot address all eight DDS raw/scale channel attributes")


def _strict_exact_mute(serial: str, uri: str, purpose: str) -> dict[str, Any]:
    started_at = _now()
    device: Any | None = None
    try:
        iio_runtime = importlib.import_module("iio")
        adi_runtime = importlib.import_module("adi")
        resolved = resolve_iio_uri(uri, serial, contexts=iio_runtime.scan_contexts())
        if resolved != uri:
            raise MutedControlError("exact mute resolved a different current USB URI")
        device = adi_runtime.ad9361(uri=resolved)
        facts = context_facts(device.ctx)
        if facts.get("serial") != serial:
            raise MutedControlError("exact mute opened a different Pluto serial")
        _mute_transmit(device)
        _zero_dds_raw(device)
        _mute_transmit(device)
        state = _exact_tx_state_from_device(device)
        if not _exact_mute_state_passed(state):
            raise MutedControlError(
                "TX gains or DDS raw/scale/enables did not reach exact zero state"
            )
        return {
            "schema": 1,
            "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "uri": uri,
            **state,
            "started_at": started_at,
            "completed_at": _now(),
            "error": None,
        }
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "uri": uri,
            "tx_hardware_gain_db_by_channel": None,
            "dds_raw_readback": None,
            "dds_scale_readback": None,
            "dds_enabled_readback": None,
            "started_at": started_at,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    finally:
        if device is not None:
            _release_device(device)


def _exact_mute_state_passed(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    gains = value.get("tx_hardware_gain_db_by_channel")
    raws = value.get("dds_raw_readback")
    scales = value.get("dds_scale_readback")
    enabled = value.get("dds_enabled_readback")
    return (
        isinstance(gains, list)
        and gains == [-80.0, -80.0]
        and isinstance(raws, list)
        and raws == [0.0] * 8
        and isinstance(scales, list)
        and scales == [0.0] * 8
        and isinstance(enabled, list)
        and len(enabled) == 8
        and not any(enabled)
    )


def _mute_passed(value: object, *, serial: str, uri: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_serial_tx_mute_and_full_dds_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("uri") == uri
        and value.get("error") is None
        and _exact_mute_state_passed(value)
    )


def _call_mute(boundary: MuteBoundary, serial: str, uri: str, purpose: str) -> dict[str, Any]:
    try:
        result = boundary(serial, uri, purpose)
    except BaseException as error:
        result = {
            "schema": 1,
            "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "uri": uri,
            "error": _error_document(error),
        }
    return dict(result) if isinstance(result, Mapping) else {"status": "failed"}


def _frame_proof(block: SampleBlockV2) -> MutedFrameProof:
    return MutedFrameProof(
        stream_id=block.stream_id,
        buffer_sequence=block.buffer_sequence,
        first_sample_sequence=block.first_sample_sequence,
        last_sample_sequence_exclusive=block.last_sample_sequence_exclusive,
        sample_count=block.sample_count,
        metadata_abi=block.metadata_abi,
        metadata_flags=block.metadata_flags,
    )


def _live_capture_boundary(
    contract: Mapping[str, Any], *, block_consumer: Callable[[SampleBlockV2], None]
) -> MutedContinuousCapture:
    configuration = contract["configuration"]
    assert isinstance(configuration, Mapping)
    settings = RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )
    radio = IioRadioDevice(
        str(configuration["uri"]),
        serial=str(configuration["serial"]),
        expected_metadata_abi=2,
        expected_firmware_version=V7_FIRMWARE_VERSION,
    )
    session: Any | None = None
    proofs: list[MutedFrameProof] = []
    try:
        radio.open()
        if radio.apply_settings(settings) != settings:
            raise MutedControlError("receive-only radio settings readback differs from the plan")
        session = radio.begin_metadata_capture(
            SAMPLES_PER_FRAME,
            kernel_buffers=KERNEL_BUFFERS,
            tandem_request=TandemSessionRequestV1(
                mode=TandemMode.HOLD,
                initial_gain_db=RECEIVER_GAIN_DB,
            ),
        )
        for _ in range(FRAME_COUNT):
            block = session.read_block()
            block_consumer(block)
            proofs.append(_frame_proof(block))
        return MutedContinuousCapture(
            identity=radio.identity,
            settings=settings,
            frames=tuple(proofs),
            kernel_buffers=session.kernel_buffers,
            receive_only_api=True,
            tx_source_active=False,
        )
    finally:
        if session is not None:
            session.close()
        radio.close()


def _block_ledger(blocks: list[SampleBlockV2]) -> dict[str, Any]:
    ledger = foundation._block_ledger(blocks)
    if ledger is None:
        raise MutedControlError("capture returned no ABI2 blocks")
    return ledger


def _expected_settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _validate_capture(
    capture: MutedContinuousCapture,
    blocks: list[SampleBlockV2],
    *,
    serial: str,
    uri: str,
) -> tuple[int, dict[str, Any]]:
    if capture.identity.serial != serial or capture.identity.uri != uri:
        raise MutedControlError("capture identity differs from exact serial/current USB URI")
    if capture.settings != _expected_settings():
        raise MutedControlError("capture settings differ from the frozen P1 settings")
    if (
        capture.sample_count != TOTAL_SAMPLES
        or len(capture.frames) != FRAME_COUNT
        or len(blocks) != FRAME_COUNT
        or capture.kernel_buffers != KERNEL_BUFFERS
        or capture.receive_only_api is not True
        or capture.tx_source_active is not False
    ):
        raise MutedControlError("capture is not one exact receive-only 10-second stream")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in blocks):
        raise MutedControlError("capture contains a partial or non-dual-RX frame")
    ledger = _block_ledger(blocks)
    summary = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if summary.metadata_abi != 2 or summary.first_buffer_sequence != 0:
        raise MutedControlError("capture did not begin a fresh ABI2 stream")
    for proof, block in zip(capture.frames, blocks, strict=True):
        if asdict(proof) != asdict(_frame_proof(block)):
            raise MutedControlError("capture proof differs from retained ABI2 frame")
    return summary.stream_id, ledger


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    root = _assert_safe_local_path(Path(artifact.path), label="muted-control artifact directory")
    if root.is_symlink() or not root.is_dir():
        raise MutedControlError("muted-control artifact directory is not a regular directory")
    data_file = _assert_safe_local_path(data_path(artifact), label="muted-control raw IQ")
    metadata_file = _assert_safe_local_path(
        root / f"{artifact.artifact_id}.sigmf-meta", label="muted-control SigMF metadata"
    )
    if any(path.is_symlink() or not path.is_file() for path in (data_file, metadata_file)):
        raise MutedControlError("muted-control artifact files are missing or symlinked")
    evidence = {
        "artifact_id": artifact.artifact_id,
        "path": str(root),
        "data_path": str(data_file),
        "data_sha256": sha256_path(data_file),
        "data_size_bytes": data_file.stat().st_size,
        "metadata_path": str(metadata_file),
        "metadata_sha256": sha256_path(metadata_file),
        "metadata_size_bytes": metadata_file.stat().st_size,
    }
    for path, label in (
        (root, "muted-control artifact directory"),
        (data_file, "muted-control raw IQ"),
        (metadata_file, "muted-control SigMF metadata"),
    ):
        _assert_safe_local_path(path, label=label)
    return evidence


def _validate_one_stream_capture_inventory(capture_root: Path, *, artifact_id: str) -> None:
    root = _assert_safe_local_path(capture_root, label="muted-control capture root")
    if root.is_symlink() or not root.is_dir():
        raise MutedControlError("muted-control capture root is not a regular directory")
    allowed = {artifact_id, ".failed", ".partial"}
    children = list(root.iterdir())
    if any(child.name not in allowed for child in children):
        raise MutedControlError("one-stream capture root contains an extra sibling artifact")
    artifact = root / artifact_id
    if artifact.is_symlink() or not artifact.is_dir():
        raise MutedControlError("one-stream capture root lacks its exact artifact directory")
    failed = root / ".failed"
    if (failed.exists() or failed.is_symlink()) and (
        failed.is_symlink() or not failed.is_dir() or any(failed.iterdir())
    ):
        raise MutedControlError("successful one-stream run has failed or extra artifacts")
    partial = root / ".partial"
    if (partial.exists() or partial.is_symlink()) and (
        partial.is_symlink() or not partial.is_dir() or any(partial.iterdir())
    ):
        raise MutedControlError("successful one-stream run has partial or extra artifacts")
    foundation._assert_tree_has_no_symlink(root, label="one-stream muted-control capture")
    _assert_safe_local_path(root, label="one-stream muted-control capture")


def _open_absolute_directory_nofollow(path: Path) -> int:
    exact = path.expanduser().absolute()
    if not exact.is_absolute() or ".." in exact.parts:
        raise MutedControlError("directory-fd path is not an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(exact.anchor, flags)
    try:
        for part in exact.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_fd_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_fd_bytes(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise MutedControlError("short write while finalizing muted-control record")
        view = view[written:]


def _validate_record_artifact_files(
    record: Mapping[str, Any],
    *,
    capture_root: Path,
    artifact_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = record.get("artifact_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("artifact_id") != artifact_id:
        raise MutedControlError("muted-control record artifact evidence is malformed")
    exact_capture = _assert_safe_local_path(capture_root, label="muted-control capture root")
    artifact_root = _assert_safe_local_path(
        Path(str(evidence.get("path", ""))), label="muted-control artifact directory"
    )
    if artifact_root.parent != exact_capture or artifact_root.name != artifact_id:
        raise MutedControlError("muted-control artifact escaped its capture root")
    artifact_identity = _directory_identity(artifact_root, label="muted-control artifact directory")
    capture_identity = _directory_identity(exact_capture, label="muted-control capture root")
    for label, path_key, hash_key, size_key in (
        ("muted-control raw IQ", "data_path", "data_sha256", "data_size_bytes"),
        (
            "muted-control SigMF metadata",
            "metadata_path",
            "metadata_sha256",
            "metadata_size_bytes",
        ),
    ):
        path = _assert_safe_local_path(Path(str(evidence.get(path_key, ""))), label=label)
        if (
            path.parent != artifact_root
            or path.is_symlink()
            or not path.is_file()
            or sha256_path(path) != evidence.get(hash_key)
            or path.stat().st_size != evidence.get(size_key)
        ):
            raise MutedControlError(f"{label} differs from the accepted artifact identity")
    foundation._assert_tree_has_no_symlink(artifact_root, label="muted-control artifact directory")
    return capture_identity, artifact_identity


def _finalize_record_safely(
    record_path: Path,
    record: Mapping[str, Any],
    *,
    capture_root: Path,
    artifact_id: str,
    expected_sha256: str,
) -> str:
    """Replace the final record through no-follow directory FDs and reattest its tree."""

    before_capture, before_artifact = _validate_record_artifact_files(
        record, capture_root=capture_root, artifact_id=artifact_id
    )
    exact_record = _assert_safe_local_path(record_path, label="muted-control record")
    artifact_root = Path(str(record["artifact_evidence"]["path"]))
    if exact_record.parent != artifact_root or exact_record.name != RECORD_FILENAME:
        raise MutedControlError("muted-control record path escaped its artifact directory")
    before_record = exact_record.stat()
    if exact_record.is_symlink() or not exact_record.is_file():
        raise MutedControlError("muted-control record is not a regular file")
    capture_fd = _open_absolute_directory_nofollow(capture_root)
    artifact_fd = -1
    record_fd = -1
    temporary_fd = -1
    temporary_name = f".{RECORD_FILENAME}.finalizing-{os.getpid()}"
    try:
        capture_stat = os.fstat(capture_fd)
        if (capture_stat.st_dev, capture_stat.st_ino) != (
            before_capture["device"],
            before_capture["inode"],
        ):
            raise MutedControlError("capture root rebound before final record rewrite")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        artifact_fd = os.open(artifact_id, directory_flags, dir_fd=capture_fd)
        artifact_stat = os.fstat(artifact_fd)
        if (artifact_stat.st_dev, artifact_stat.st_ino) != (
            before_artifact["device"],
            before_artifact["inode"],
        ):
            raise MutedControlError("artifact directory rebound before final record rewrite")
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        record_fd = os.open(RECORD_FILENAME, file_flags, dir_fd=artifact_fd)
        opened_record = os.fstat(record_fd)
        if not stat.S_ISREG(opened_record.st_mode) or (
            opened_record.st_dev,
            opened_record.st_ino,
        ) != (before_record.st_dev, before_record.st_ino):
            raise MutedControlError("record file rebound before final rewrite")
        if hashlib.sha256(_read_fd_bytes(record_fd)).hexdigest() != expected_sha256:
            raise MutedControlError("record bytes changed before final rewrite")
        payload = (json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
            "utf-8"
        )
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
            dir_fd=artifact_fd,
        )
        _write_fd_bytes(temporary_fd, payload)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(
            temporary_name,
            RECORD_FILENAME,
            src_dir_fd=artifact_fd,
            dst_dir_fd=artifact_fd,
        )
        os.fsync(artifact_fd)
        os.close(record_fd)
        record_fd = os.open(RECORD_FILENAME, file_flags, dir_fd=artifact_fd)
        completed = os.fstat(record_fd)
        completed_bytes = _read_fd_bytes(record_fd)
        if not stat.S_ISREG(completed.st_mode) or completed_bytes != payload:
            raise MutedControlError("final record bytes differ after atomic replacement")
        digest = hashlib.sha256(completed_bytes).hexdigest()
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if artifact_fd >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=artifact_fd)
        if record_fd >= 0:
            os.close(record_fd)
        if artifact_fd >= 0:
            os.close(artifact_fd)
        os.close(capture_fd)
    after_capture, after_artifact = _validate_record_artifact_files(
        record, capture_root=capture_root, artifact_id=artifact_id
    )
    if after_capture != before_capture or after_artifact != before_artifact:
        raise MutedControlError("artifact or capture directory rebound during finalization")
    if sha256_path(exact_record) != digest:
        raise MutedControlError("final record path/hash differs after no-follow rewrite")
    _validate_one_stream_capture_inventory(capture_root, artifact_id=artifact_id)
    return digest


def _quarantine_partial(
    capture_root: Path,
    *,
    blocks: list[SampleBlockV2],
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    result = foundation._persist_memory_quarantine(
        capture_root,
        blocks=blocks,
        error=error,
        context=context,
    )
    return _validate_local_quarantine(result)


def _validate_local_quarantine(value: Mapping[str, Any]) -> dict[str, Any]:
    path_value = value.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise MutedControlError("muted-control quarantine lacks an absolute path")
    root = _assert_safe_local_path(Path(path_value), label="muted-control quarantine")
    try:
        exact = foundation._assert_tree_has_no_symlink(root, label="muted-control quarantine")
    except (OSError, RuntimeError) as error:
        raise MutedControlError("muted-control quarantine tree is unsafe") from error
    if exact != root or exact.parent.name != ".failed":
        raise MutedControlError("muted-control quarantine escaped its failed-artifact root")
    return dict(value)


def _seal_local_failed_directory(
    destination: Path,
    *,
    artifact_id: str,
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    _assert_safe_local_path(destination, label="muted-control failed artifact")
    result = foundation._seal_failed_directory(
        destination,
        artifact_id=artifact_id,
        error=error,
        context=context,
    )
    return _validate_local_quarantine(result)


def _capture_one(
    contract: Mapping[str, Any],
    *,
    plan_evidence: Mapping[str, Any],
    execution_started: Mapping[str, Any],
    capture_root: Path,
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_exact_mute,
) -> dict[str, Any]:
    configuration = contract["configuration"]
    assert isinstance(configuration, Mapping)
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    retained: list[SampleBlockV2] = []
    try:
        capture_root = _assert_safe_local_path(capture_root, label="muted-control capture root")
        exact_capture_root, _failed_root = foundation._safe_quarantine_parent(capture_root)
        _assert_safe_local_path(exact_capture_root, label="muted-control capture root")
        _assert_safe_local_path(_failed_root, label="muted-control quarantine root")
    except (OSError, RuntimeError) as error:
        raise MutedControlError("capture root failed symlink/layout validation") from error

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    context: dict[str, Any] = {
        "immutable_plan": dict(plan_evidence),
        "execution_started": dict(execution_started),
        "fixture_evidence_sha256": contract["fixture_evidence_sha256"],
        "native_libiio_runtime_attestation_sha256": contract["source"][
            "native_libiio_runtime_attestation_sha256"
        ],
        "tx_source_active": False,
    }
    capture: MutedContinuousCapture | None = None
    capture_error: BaseException | None = None
    try:
        capture = capture_boundary(contract, block_consumer=retain)
    except BaseException as error:
        capture_error = error
    finally:
        post_mute = _call_mute(mute_boundary, serial, uri, "post_capture")
        context["post_capture_exact_mute"] = post_mute
    if capture_error is not None or not _mute_passed(
        post_mute, serial=serial, uri=uri, purpose="post_capture"
    ):
        failure = capture_error or MutedControlError("post-capture exact mute attestation failed")
        quarantine = _quarantine_partial(
            capture_root, blocks=retained, error=failure, context=context
        )
        retained.clear()
        raise MutedCaptureFailure(
            str(failure), quarantine=quarantine, post_mute=post_mute
        ) from capture_error
    assert capture is not None

    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        stream_id, _ledger = _validate_capture(capture, retained, serial=serial, uri=uri)
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        if not headroom.passed:
            raise MutedControlError("ADC clipping/headroom admission failed")
        samples = np.concatenate([block.samples for block in retained], axis=1)
        analysis = analyze_muted_stream(
            samples,
            sample_rate_hz=SAMPLE_RATE_HZ,
            pilot_offset_hz=float(configuration["pilot_offset_hz_from_p0"]),
        )
        writer = CaptureWriter(
            exact_capture_root,
            radio=capture.identity,
            settings=capture.settings,
            label="P1 true-TX-muted dual-RX 5.8 GHz control",
        )
        for block in retained:
            writer.append(block, capture.settings, revision=1)
        artifact = writer.finalize()
        artifact_root = foundation._assert_tree_has_no_symlink(
            Path(artifact.path), label="completed muted-control artifact"
        )
        _assert_safe_local_path(artifact_root, label="completed muted-control artifact")
        if artifact_root.parent != exact_capture_root:
            raise MutedControlError("completed artifact escaped the immutable capture root")
        if not verify_artifact(artifact):
            raise MutedControlError("finalized muted-control SigMF hash verification failed")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        if continuity["stream_id"] != stream_id or continuity["metadata_abi"] != 2:
            raise MutedControlError("persisted continuity identity differs from live capture")
        evidence = _artifact_evidence(artifact)
        fixture = contract["fixture_evidence"]
        assert isinstance(fixture, Mapping)
        source = contract["source"]
        assert isinstance(source, Mapping)
        record = {
            "schema": 1,
            "record_kind": "5g8_true_tx_muted_control",
            "created_at": _now(),
            "accepted": False,
            "standalone_record_is_not_acceptance": True,
            "acceptance_authority": "complete plan-bound run manifest plus artifact revalidation",
            "run_id": contract["run_id"],
            "campaign_id": contract["campaign_id"],
            "source_commit": source["smateway_commit"],
            "dependency_source_attestation_sha256": source[
                "pluto_plus_utils_source_attestation_sha256"
            ],
            "native_libiio_runtime_attestation_sha256": source[
                "native_libiio_runtime_attestation_sha256"
            ],
            "fixture_evidence_sha256": contract["fixture_evidence_sha256"],
            "cohort_fixture_identity_sha256": fixture["cohort_fixture_identity_sha256"],
            "p0_post_cycle_schedule_proof_sha256": fixture["p0_post_cycle_schedule_proof_sha256"],
            "immutable_plan": dict(plan_evidence),
            "execution_started": dict(execution_started),
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": evidence,
            "capture": {
                "serial": serial,
                "uri": uri,
                "center_frequency_hz": CENTER_FREQUENCY_HZ,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "receiver_gain_db": RECEIVER_GAIN_DB,
                "sample_count": TOTAL_SAMPLES,
                "duration_s": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FRAME_COUNT,
                "kernel_buffers": KERNEL_BUFFERS,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "tx_source_active": False,
                "receive_only_api": True,
            },
            "continuity_audit": continuity,
            "analysis": analysis,
            "safety": {
                "post_capture_exact_mute": post_mute,
                "final_exact_mute": None,
                "headroom_passed": headroom.passed,
                "adc_headroom_admission": _json_safe(asdict(headroom)),
                "raw_persisted_only_after_post_capture_exact_mute_passed": True,
                "automatic_retry_count": 0,
            },
        }
        record_path = Path(artifact.path) / RECORD_FILENAME
        write_json_atomic(record_path, record)
        _assert_safe_local_path(record_path, label="muted-control record")
        if record_path.is_symlink() or not record_path.is_file():
            raise MutedControlError("muted-control record persistence escaped its artifact")
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_data_sha256": evidence["data_sha256"],
            "artifact_metadata_sha256": evidence["metadata_sha256"],
            "record_path": str(record_path),
            "record_sha256": sha256_path(record_path),
            "stream_id": stream_id,
            "metadata_abi": 2,
            "sample_count": TOTAL_SAMPLES,
            "headroom_passed": True,
            "post_capture_exact_mute": post_mute,
            "transfer_phase_defined": False,
            "execution_started": dict(execution_started),
        }
    except BaseException as error:
        context["post_capture_validation_error"] = _error_document(error)
        if artifact is not None:
            source_path = Path(artifact.path)
            exact_root, failed_root = foundation._safe_quarantine_parent(capture_root)
            exact_source = foundation._assert_tree_has_no_symlink(
                source_path, label="failed muted-control artifact"
            )
            if exact_source.parent != exact_root:
                raise MutedControlError("failed artifact escaped capture root") from error
            destination = failed_root / f"{artifact.artifact_id}.failed"
            os.replace(exact_source, destination)
            quarantine = _seal_local_failed_directory(
                destination,
                artifact_id=artifact.artifact_id,
                error=error,
                context=context,
            )
        elif writer is not None:
            destination = writer.fail(error)
            quarantine = _seal_local_failed_directory(
                destination,
                artifact_id=writer.artifact_id,
                error=error,
                context=context,
            )
        else:
            quarantine = _quarantine_partial(
                capture_root, blocks=retained, error=error, context=context
            )
        raise MutedCaptureFailure(str(error), quarantine=quarantine, post_mute=post_mute) from error
    finally:
        retained.clear()


def _quarantine_completed_result(
    result: Mapping[str, Any],
    *,
    capture_root: Path,
    error: BaseException,
    reason: str,
) -> dict[str, Any]:
    raw_source = result.get("artifact_path")
    artifact_id = result.get("artifact_id")
    if not isinstance(raw_source, str) or not isinstance(artifact_id, str):
        raise MutedControlError("completed result lacks an artifact path/identity to quarantine")
    foundation._assert_path_chain_has_no_symlink(
        Path(raw_source), label="completed muted-control artifact"
    )
    _assert_safe_local_path(Path(raw_source), label="completed muted-control artifact")
    exact_root, failed_root = foundation._safe_quarantine_parent(capture_root)
    source = foundation._assert_tree_has_no_symlink(
        Path(raw_source), label="completed muted-control artifact"
    )
    if source.parent != exact_root or source.name != artifact_id:
        raise MutedControlError("completed muted-control artifact escaped its capture root")
    destination = failed_root / f"{artifact_id}.{reason}.failed"
    if destination.exists() or destination.is_symlink():
        raise MutedControlError("completed-result quarantine destination already exists")
    os.replace(source, destination)
    _assert_safe_local_path(destination, label="muted-control completed-result quarantine")
    return _seal_local_failed_directory(
        destination,
        artifact_id=artifact_id,
        error=error,
        context={
            "muted_control_result": _json_safe(result),
            "quarantine_reason": reason,
        },
    )


def _confirmation(
    contract: Mapping[str, Any],
    *,
    no_antennas: bool,
    tx1_untouched: bool,
    tx2_terminated: bool,
    rx1_protected: bool,
    no_movement: bool,
    sealed_fast20_unchanged: bool,
    topology_token: str | None,
) -> dict[str, Any]:
    evidence_sha = contract["fixture_evidence_sha256"]
    passed = all(
        (
            no_antennas,
            tx1_untouched,
            tx2_terminated,
            rx1_protected,
            no_movement,
            sealed_fast20_unchanged,
            topology_token == TOPOLOGY_TOKEN,
        )
    )
    document = {
        "schema": 1,
        "evidence_kind": "5g8_p1_operator_confirmation",
        "status": "passed" if passed else "failed",
        "topology_token": topology_token,
        "no_antennas": no_antennas,
        "tx1_untouched_conducted_fixture": tx1_untouched,
        "tx2_physically_50ohm_terminated": tx2_terminated,
        "rx1_protection_present": rx1_protected,
        "no_movement_since_setup_evidence": no_movement,
        "sealed_fast20_image_unchanged_since_p0": sealed_fast20_unchanged,
        "schedule_timing_claim_source": "bound_post_cycle_p0_rf_artifacts",
        "fixture_evidence_sha256": evidence_sha,
    }
    if not passed:
        raise MutedControlError("all exact P1 operator confirmations are required")
    return document


def _live_evidence_preflight(expected: Mapping[str, Any]) -> dict[str, Any]:
    started_at = _now()
    source_manifests = expected.get("p0_source_manifest_files")
    if not isinstance(source_manifests, list) or len(source_manifests) != 5:
        raise MutedControlError("fixture evidence lacks five post-cycle P0 manifest bindings")
    observed = _fixture_evidence_from_files(
        Path(str(expected["fixture_manifest_file"]["path"])),
        Path(str(expected["setup_attestation_file"]["path"])),
        Path(str(expected["sealed_selector_flash_evidence_file"]["path"])),
        [Path(str(item["path"])) for item in source_manifests if isinstance(item, Mapping)],
        run_id=str(expected["run_id"]),
        board_id=str(expected["board_id"]),
        serial=str(expected["serial"]),
        derivation_source_commit=str(
            expected["p0_post_cycle_schedule_proof"]["derivation_source_commit"]
        ),
    )
    return {
        "schema": 1,
        "evidence_kind": "5g8_p1_fixture_preflight",
        "status": "passed" if observed == dict(expected) else "failed",
        "fixture_evidence": observed,
        "fixture_evidence_sha256": canonical_json_sha256(observed),
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _execute_run_under_cleanup(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_exact_mute,
    identity_boundary: IdentityBoundary = foundation._live_identity_boundary,
    runtime_boundary: RuntimeAttestationBoundary = _native_libiio_runtime_attestation,
    evidence_boundary: EvidenceBoundary = _live_evidence_preflight,
) -> None:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    configuration = contract["configuration"]
    assert isinstance(configuration, Mapping)
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    capture_root = Path(str(contract["storage"]["run_capture_root"]))
    manifest_path = _assert_safe_local_path(manifest_path, label="muted-control manifest")
    capture_root = _assert_safe_local_path(capture_root, label="muted-control capture root")
    _validate_run_reservation(manifest_path, manifest=manifest)
    _validate_execution_burn(manifest_path, manifest=manifest)
    if _tombstone_path(manifest_path).exists() or manifest.get("status") == "failed":
        raise MutedControlError("failed muted-control run IDs cannot retry")
    if manifest.get("status") != "prepared" or manifest.get("attempt") is not None:
        raise MutedControlError("muted-control run already executed or is incomplete")
    pending: BaseException | None = None
    manifest["confirmation"] = dict(confirmation)
    manifest["status"] = "running"
    _persist_manifest(manifest_path, manifest)
    try:
        source = contract["source"]
        assert isinstance(source, Mapping)
        runtime = call_runtime_preflight(runtime_boundary, now=_now, error_document=_error_document)
        manifest["native_runtime_preflight"] = runtime
        _persist_manifest(manifest_path, manifest)
        if not runtime_preflight_passed(
            runtime, expected=source["native_libiio_runtime_attestation"]
        ):
            raise MutedControlError("native libiio runtime differs from the immutable plan")

        evidence = evidence_boundary(contract["fixture_evidence"])
        manifest["fixture_preflight"] = evidence
        _persist_manifest(manifest_path, manifest)
        if (
            evidence.get("status") != "passed"
            or evidence.get("fixture_evidence") != contract["fixture_evidence"]
            or evidence.get("fixture_evidence_sha256") != contract["fixture_evidence_sha256"]
        ):
            raise MutedControlError("fixture/setup/Fast20/P0 evidence differs from the plan")

        identity = foundation._call_identity(identity_boundary, serial, uri)
        manifest["identity_preflight"] = identity
        _persist_manifest(manifest_path, manifest)
        if not foundation._identity_passed(identity, serial=serial, requested_uri=uri):
            raise MutedControlError("USB identity did not resolve the exact serial/current URI")

        pre_mute = _call_mute(mute_boundary, serial, uri, "pre_capture")
        manifest["pre_capture_mute"] = pre_mute
        _persist_manifest(manifest_path, manifest)
        if not _mute_passed(pre_mute, serial=serial, uri=uri, purpose="pre_capture"):
            raise MutedControlError("pre-capture exact TX/DDS mute attestation failed")

        attempt: dict[str, Any] = {
            "attempt_id": 1,
            "started_at": _now(),
            "completed_at": None,
            "status": "running",
            "result": None,
            "quarantine": None,
            "error": None,
            "automatic_retry_attempted": False,
        }
        manifest["attempt"] = attempt
        _persist_manifest(manifest_path, manifest)
        try:
            result = _capture_one(
                contract,
                plan_evidence=_plan_evidence(plan_path, envelope),
                execution_started=manifest["execution_started"],
                capture_root=capture_root,
                capture_boundary=capture_boundary,
                mute_boundary=mute_boundary,
            )
        except MutedCaptureFailure as error:
            attempt["status"] = "failed"
            attempt["completed_at"] = _now()
            attempt["quarantine"] = error.quarantine
            attempt["error"] = _error_document(error)
            _persist_manifest(manifest_path, manifest)
            raise
        attempt["status"] = "complete"
        attempt["completed_at"] = _now()
        attempt["result"] = result
        _persist_manifest(manifest_path, manifest)
    except BaseException as error:
        pending = error
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
    finally:
        final_mute = _call_mute(mute_boundary, serial, uri, "final")
        manifest["final_mute_attempts"].append(final_mute)
        manifest["final_mute"] = final_mute
        if not _mute_passed(final_mute, serial=serial, uri=uri, purpose="final"):
            pending = MutedControlError("final exact TX/DDS mute attestation failed")
            manifest["status"] = "failed"
            manifest["error"] = _error_document(pending)
            completed_attempt = manifest.get("attempt")
            completed_result = (
                completed_attempt.get("result") if isinstance(completed_attempt, Mapping) else None
            )
            if isinstance(completed_attempt, dict) and isinstance(completed_result, Mapping):
                completed_attempt["quarantine"] = _quarantine_completed_result(
                    completed_result,
                    capture_root=capture_root,
                    error=pending,
                    reason="final-mute",
                )
                completed_attempt["status"] = "failed"
                completed_attempt["error"] = _error_document(pending)
                completed_attempt["completed_at"] = _now()
        elif pending is None:
            completed_attempt = manifest.get("attempt")
            if (
                not isinstance(completed_attempt, Mapping)
                or completed_attempt.get("status") != "complete"
            ):
                pending = MutedControlError("muted-control capture did not complete")
                manifest["status"] = "failed"
                manifest["error"] = _error_document(pending)
            else:
                completed_result = completed_attempt.get("result")
                assert isinstance(completed_result, dict)
                record_path = Path(str(completed_result["record_path"]))
                record = _read_json(record_path, "muted-control record")
                safety = record.get("safety")
                if (
                    not isinstance(safety, dict)
                    or record.get("execution_started") != manifest.get("execution_started")
                    or completed_result.get("execution_started")
                    != manifest.get("execution_started")
                ):
                    pending = MutedControlError("muted-control record safety is malformed")
                    manifest["status"] = "failed"
                    manifest["error"] = _error_document(pending)
                else:
                    record["accepted"] = True
                    safety["final_exact_mute"] = final_mute
                    completed_result["record_sha256"] = _finalize_record_safely(
                        record_path,
                        record,
                        capture_root=capture_root,
                        artifact_id=str(completed_result["artifact_id"]),
                        expected_sha256=str(completed_result["record_sha256"]),
                    )
                    manifest["status"] = "complete"
                    manifest["completed_at"] = _now()
                    manifest["error"] = None
        _persist_manifest(manifest_path, manifest)
    if pending is not None:
        raise pending


def _execute_run(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_exact_mute,
    identity_boundary: IdentityBoundary = foundation._live_identity_boundary,
    runtime_boundary: RuntimeAttestationBoundary = _native_libiio_runtime_attestation,
    evidence_boundary: EvidenceBoundary = _live_evidence_preflight,
) -> None:
    """Execute one run with an outer fail-muted recovery boundary.

    The outer boundary also covers stale ``running`` manifests and exceptions
    raised before the inner try/finally is entered.
    """

    contract = envelope.get("plan_contract")
    if not isinstance(contract, Mapping):
        raise MutedControlError("immutable muted-control contract is malformed")
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise MutedControlError("immutable muted-control configuration is malformed")
    serial = str(configuration.get("serial", ""))
    uri = str(configuration.get("uri", ""))
    capture_root = Path(str(contract.get("storage", {}).get("run_capture_root", "")))
    manifest_path = _assert_safe_local_path(manifest_path, label="muted-control manifest")
    capture_root = _assert_safe_local_path(capture_root, label="muted-control capture root")
    _validate_run_reservation(manifest_path, manifest=manifest)
    run_id = _validate_string(manifest.get("run_id"), "muted-control run ID")
    burn_path = _execution_burn_path(manifest_path, run_id=run_id)
    _assert_safe_local_path(burn_path, label="muted-control execution burn")
    if burn_path.exists() or burn_path.is_symlink():
        raise MutedControlError("muted-control run ID was already executed and cannot re-enter")
    _burn_execution_run_id(manifest_path, manifest=manifest)
    _persist_manifest(manifest_path, manifest)
    if manifest.get("status") != "prepared" or manifest.get("attempt") is not None:
        raise MutedControlError("muted-control run is not one untouched prepared execution")
    initial_status = manifest.get("status")
    initial_final_count = len(manifest.get("final_mute_attempts", []))
    try:
        _execute_run_under_cleanup(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=confirmation,
            capture_boundary=capture_boundary,
            mute_boundary=mute_boundary,
            identity_boundary=identity_boundary,
            runtime_boundary=runtime_boundary,
            evidence_boundary=evidence_boundary,
        )
    except BaseException as error:
        cleanup_error: BaseException = error
        if len(manifest.get("final_mute_attempts", [])) == initial_final_count:
            final_mute = _call_mute(mute_boundary, serial, uri, "final")
            manifest.setdefault("final_mute_attempts", []).append(final_mute)
            manifest["final_mute"] = final_mute
            if not _mute_passed(final_mute, serial=serial, uri=uri, purpose="final"):
                cleanup_error = MutedControlError(
                    "exception-path final exact TX/DDS mute attestation failed"
                )
        # An accidental second invocation of a completed run must not destroy
        # already accepted evidence. It still receives the best-effort final
        # mute above, but its immutable manifest is left untouched.
        if initial_status != "complete":
            attempt = manifest.get("attempt")
            result = attempt.get("result") if isinstance(attempt, Mapping) else None
            if (
                isinstance(attempt, dict)
                and isinstance(result, Mapping)
                and attempt.get("quarantine") is None
            ):
                try:
                    attempt["quarantine"] = _quarantine_completed_result(
                        result,
                        capture_root=capture_root,
                        error=cleanup_error,
                        reason="exception-cleanup",
                    )
                except (OSError, RuntimeError) as quarantine_error:
                    cleanup_error = MutedControlError(
                        f"failed to quarantine rejected completed artifact: {quarantine_error}"
                    )
                attempt["status"] = "failed"
                attempt["error"] = _error_document(cleanup_error)
                attempt["completed_at"] = _now()
            manifest["status"] = "failed"
            manifest["error"] = _error_document(cleanup_error)
            _persist_manifest(manifest_path, manifest)
        if cleanup_error is not error:
            raise cleanup_error from error
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--uri", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-only", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--setup-attestation", type=Path, required=True)
    parser.add_argument("--selector-evidence", type=Path, required=True)
    parser.add_argument(
        "--p0-manifest",
        type=Path,
        action="append",
        required=True,
        help="accepted post-cycle P0 manifest; specify exactly five times",
    )
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-tx1-untouched", action="store_true")
    parser.add_argument("--confirm-tx2-terminated", action="store_true")
    parser.add_argument("--confirm-rx1-protected", action="store_true")
    parser.add_argument("--confirm-no-movement", action="store_true")
    parser.add_argument("--confirm-sealed-fast20-unchanged", action="store_true")
    parser.add_argument("--confirm-topology-token")
    return parser


def _signal_handler(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received {signal.Signals(signum).name}; entering fail-muted cleanup")


def _install_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    try:
        source_commit = foundation._repository_commit_and_require_clean(_REPOSITORY, "smateway")
        dependency = attest_pluto_plus_utils_source(
            imported_modules=(
                *PLUTO_PLUS_UTILS_IMPORTED_MODULES,
                ("pluto_plus.tandem", "src/pluto_plus/tandem.py"),
            )
        )
        native = _native_libiio_runtime_attestation()
        fixture = _fixture_evidence_from_files(
            args.fixture_manifest,
            args.setup_attestation,
            args.selector_evidence,
            args.p0_manifest,
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            derivation_source_commit=source_commit,
        )
        contract = _build_plan_contract(
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            source_commit=source_commit,
            dependency_attestation=dependency,
            native_attestation=native,
            fixture_evidence=fixture,
        )
    except (MutedControlError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    board_root = foundation._board_root(str(contract["board_id"]))
    run_root = board_root / "5g8-muted-control" / str(contract["run_id"])
    plan_path = run_root / PLAN_FILENAME
    manifest_path = run_root / MANIFEST_FILENAME
    _assert_safe_local_path(board_root, label="muted-control board state root")
    _assert_safe_local_path(run_root, label="muted-control run directory")
    with foundation._board_lock(board_root):
        _assert_safe_local_path(board_root, label="muted-control board state root")
        _assert_safe_local_path(run_root, label="muted-control run directory")
        if args.plan_only:
            try:
                envelope, manifest = _prepare_plan_only(
                    plan_path=plan_path,
                    manifest_path=manifest_path,
                    contract=contract,
                )
                _persist_manifest(manifest_path, manifest)
            except MutedControlError as error:
                raise SystemExit(str(error)) from error
            print(
                json.dumps(
                    {
                        "run_id": contract["run_id"],
                        "status": manifest["status"],
                        "immutable_plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "manifest": str(manifest_path),
                        "planned_stream_count": 1,
                        "cohort_run_count": 5,
                    },
                    sort_keys=True,
                )
            )
            return 0

        if _tombstone_path(manifest_path).exists() or _tombstone_path(manifest_path).is_symlink():
            raise SystemExit("failed-run tombstone forbids execution or retry")
        if (
            plan_path.is_symlink()
            or manifest_path.is_symlink()
            or not plan_path.is_file()
            or not manifest_path.is_file()
        ):
            raise SystemExit("execute requires a prior successful --plan-only invocation")
        try:
            envelope = _validate_plan_envelope(
                _read_json(plan_path, "immutable plan"), expected_contract=contract
            )
            manifest = _read_json(manifest_path, "run manifest")
            if manifest.get("immutable_plan") != _plan_evidence(plan_path, envelope):
                raise MutedControlError("manifest no longer binds the immutable plan")
            _validate_run_reservation(manifest_path, manifest=manifest)
            confirmation = _confirmation(
                contract,
                no_antennas=args.confirm_no_antennas,
                tx1_untouched=args.confirm_tx1_untouched,
                tx2_terminated=args.confirm_tx2_terminated,
                rx1_protected=args.confirm_rx1_protected,
                no_movement=args.confirm_no_movement,
                sealed_fast20_unchanged=args.confirm_sealed_fast20_unchanged,
                topology_token=args.confirm_topology_token,
            )
            _execute_run(
                manifest,
                manifest_path,
                envelope=envelope,
                plan_path=plan_path,
                confirmation=confirmation,
            )
        except (MutedControlError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "run_id": contract["run_id"],
                    "status": manifest["status"],
                    "manifest": str(manifest_path),
                    "stream_id": manifest["attempt"]["result"]["stream_id"],
                    "record": manifest["attempt"]["result"]["record_path"],
                    "transfer_phase_defined": False,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
