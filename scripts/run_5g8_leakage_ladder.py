#!/usr/bin/env python3
"""Run one manually confirmed 5.8 GHz coherent-leakage topology stage."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_SMATEWAY_SOURCE = Path(__file__).resolve().parents[1] / "src"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX or str(_SMATEWAY_SOURCE) not in sys.path
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
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

import numpy as np
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
from pluto_plus.hardware.iio import find_usb_sysfs_path, resolve_iio_uri
from pluto_plus.models import GainMode, RadioSettings

from smateway.bench import BenchManifest, OpenOcdBench
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.capture_continuity import validate_continuity_ledger
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.profile import load_profile
from smateway.rf_policy import EXPERIMENTAL_5G8_CENTER_HZ, classify_fast20_center_frequency

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
CENTER_FREQUENCY_HZ = EXPERIMENTAL_5G8_CENTER_HZ
TONE_OFFSET_HZ = 100_000
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 3
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
RECEIVER_GAIN_DB = 60
TX_HARDWARE_GAINS_DB = (-35.0, -30.0, -25.0, -20.0, -15.0, -10.0)
DDS_SCALE = 0.125
MINIMUM_PILOT_CONFIDENCE = 0.25
MINIMUM_PILOT_PHASE_STEP_COHERENCE = 0.995
MAXIMUM_PILOT_PHASE_RMS_DEG = 6.0
SOURCE_PEAK_OUTPUT_BOUND_DBM = 7.0
LOAD_INPUT_LIMIT_DBM = 0.0
REQUIRED_MARGIN_DB = 10.0
PATH_ATTENUATION_BEFORE_LOAD_DB = 0.0
CONDITION_RECORD_NAME = "5g8-leakage-condition.json"
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
USB_URI = re.compile(r"usb:[0-9]+(?:\.[0-9]+)+")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")

STAGE_CONTRACTS: dict[str, dict[str, Any]] = {
    "direct_rx2_termination": {
        "order": 0,
        "confirmation_token": "DIRECT_RX2_50OHM_AT_PLUTO",
        "rx2_topology": "5.8 GHz 50 ohm termination directly on Pluto RX2",
        "selector_topology": "selector and RX2 cable disconnected",
        "selector_state_contract": "selector physically disconnected; controller forbidden",
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "rx2_cable_terminated": {
        "order": 1,
        "confirmation_token": "RX2_CABLE_FAR_END_50OHM",
        "rx2_topology": "test cable on Pluto RX2 with a 5.8 GHz 50 ohm far-end termination",
        "selector_topology": "selector disconnected from the RX2 test cable",
        "selector_state_contract": "selector physically disconnected; controller forbidden",
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "powered_selector_all_inputs_terminated": {
        "order": 2,
        "confirmation_token": "POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM",
        "rx2_topology": "RX2 test cable connects Pluto RX2 to the selector common port",
        "selector_topology": (
            "selector is powered and all eight ANT input ports have 5.8 GHz 50 ohm terminations"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": (
            "TX1 feeds only a matched conducted two-way network; one attenuated branch feeds "
            "RX1 and every other branch is 50 ohm terminated"
        ),
    },
    "full_conducted_fixture": {
        "order": 3,
        "confirmation_token": "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
        "rx2_topology": "selector common connects through the fixed test cable to Pluto RX2",
        "selector_topology": (
            "TX1 two-way branch feeds the 2-8 GHz eight-way splitter and all eight selector "
            "ANT inputs; the other attenuated two-way branch feeds RX1"
        ),
        "selector_state_contract": (
            "reviewed static firmware; ALL_OFF commanded and mailbox-read back before and after RF"
        ),
        "tx1_reference_topology": "RX1 is the attenuated conducted reference branch",
    },
}
STAGES = tuple(STAGE_CONTRACTS)
SELECTOR_CONNECTED_STAGES = frozenset(
    {"powered_selector_all_inputs_terminated", "full_conducted_fixture"}
)


class LeakageLadderError(RuntimeError):
    """A frozen plan, live safety, continuity, or artifact invariant failed."""


class ConditionCaptureFailure(LeakageLadderError):
    """One condition failed after preserving an explicit quarantine."""

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


class CaptureBoundary(Protocol):
    """Injectable seam around the sole live RF helper."""

    def __call__(
        self,
        plan: SafeDdsTonePlan,
        *,
        samples_per_frame: int,
        frame_count: int,
        kernel_buffers: int,
        block_consumer: Callable[[SampleBlockV2], None],
    ) -> Any: ...


MuteBoundary = Callable[[str, str], dict[str, Any]]


class IdentityBoundary(Protocol):
    """Injectable read-only identity scan before any RF helper is called."""

    def __call__(self, serial: str, requested_uri: str) -> dict[str, Any]: ...


class SelectorBoundary(Protocol):
    """Injectable static ALL_OFF command plus mailbox-readback boundary."""

    def __call__(
        self,
        selector_control: Mapping[str, Any],
        purpose: str,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_document(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _validate_identifier(value: str, label: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _validate_serial(value: str) -> str:
    if not value or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("serial must be a non-empty exact device identifier")
    return value


def _validate_usb_uri(value: str) -> str:
    if USB_URI.fullmatch(value) is None:
        raise ValueError("URI must be an exact current usb: bus.address IIO URI")
    return value


def _validate_commit(value: str, label: str) -> str:
    if GIT_COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return value


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    """Create one durable read-only JSON file without any overwrite path."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LeakageLadderError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise LeakageLadderError(f"{label} root must be an object")
    return value


def _board_root(board_id: str) -> Path:
    return Path.home() / ".local/state/smateway/boards" / board_id


@contextmanager
def _board_lock(board_root: Path) -> Iterator[None]:
    board_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = board_root / ".bench.lock"
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LeakageLadderError(f"board bench lock is already held: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _repository_commit_and_require_clean(repository: Path, label: str) -> str:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _validate_commit(head, f"{label} commit")
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise LeakageLadderError(f"{label} source tree must be clean before freezing an RF plan")
    return head


def _validate_dependency_source_attestation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and freeze the complete source/import attestation used at runtime."""

    document = _json_safe(dict(value))
    if not isinstance(document, dict):
        raise ValueError("pluto-plus-utils source attestation must be an object")
    commit = str(document.get("commit", ""))
    _validate_commit(commit, "pluto-plus-utils attested commit")
    if (
        document.get("schema") != 1
        or document.get("dependency") != "pluto-plus-utils"
        or document.get("head") != commit
        or document.get("clean_worktree_verified") is not True
        or not isinstance(document.get("files"), list)
        or not document["files"]
        or not isinstance(document.get("imported_modules"), list)
    ):
        raise ValueError("pluto-plus-utils source attestation is incomplete")
    imported = {
        item.get("module") for item in document["imported_modules"] if isinstance(item, Mapping)
    }
    required = {
        "pluto_plus.artifacts",
        "pluto_plus.bootstrap_firmware",
        "pluto_plus.hardware",
        "pluto_plus.hardware.iio",
        "pluto_plus.models",
    }
    if not required.issubset(imported):
        raise ValueError("pluto-plus-utils imported-module attestation is incomplete")
    return document


def _selector_control_contract(
    *,
    bench_manifest_path: Path,
    openocd_config_path: Path,
    profile_path: Path,
) -> dict[str, Any]:
    """Freeze the exact static-selector mailbox and ALL_OFF control artifacts."""

    manifest_path = bench_manifest_path.expanduser().resolve(strict=True)
    config_path = openocd_config_path.expanduser().resolve(strict=True)
    exact_profile_path = profile_path.expanduser().resolve(strict=True)
    profile_header_path = exact_profile_path.with_name("control_profile.h").resolve(strict=True)
    manifest = BenchManifest.load(manifest_path)
    profile = load_profile(exact_profile_path)
    return {
        "schema": 1,
        "mode": "reviewed_static_selector_mailbox_all_off",
        "bench_manifest": {
            "path": str(manifest_path),
            "file_sha256": sha256_path(manifest_path),
            "elf_sha256": manifest.elf_sha256,
            "mailbox_address": manifest.address,
            "mailbox_size": manifest.size,
            "mailbox_magic": manifest.magic,
            "mailbox_version": manifest.version,
            "max_lease_ms": manifest.max_lease_ms,
            "mailbox_offsets": dict(manifest.offsets),
        },
        "openocd_config": {
            "path": str(config_path),
            "file_sha256": sha256_path(config_path),
        },
        "control_profile": {
            "path": str(exact_profile_path),
            "file_sha256": sha256_path(exact_profile_path),
            "header_path": str(profile_header_path),
            "header_file_sha256": sha256_path(profile_header_path),
            "profile_id": profile.profile_id,
            "revision": profile.revision,
            "contract_sha256": profile.contract_sha256,
            "all_off_code": profile.all_off_code,
        },
        "command": {
            "code": profile.all_off_code,
            "lease_ms": 0,
            "wait_until_applied": True,
            "readback_required": True,
        },
    }


def _validate_selector_control_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    document = _json_safe(dict(value))
    if not isinstance(document, dict):
        raise ValueError("selector-control contract must be an object")
    manifest = document.get("bench_manifest")
    config = document.get("openocd_config")
    profile = document.get("control_profile")
    command = document.get("command")
    if (
        document.get("schema") != 1
        or document.get("mode") != "reviewed_static_selector_mailbox_all_off"
        or not all(isinstance(item, Mapping) for item in (manifest, config, profile, command))
    ):
        raise ValueError("selector-control contract is malformed")
    assert isinstance(manifest, Mapping)
    assert isinstance(config, Mapping)
    assert isinstance(profile, Mapping)
    assert isinstance(command, Mapping)
    if (
        command.get("code") != profile.get("all_off_code")
        or command.get("lease_ms") != 0
        or command.get("wait_until_applied") is not True
        or command.get("readback_required") is not True
    ):
        raise ValueError("selector-control contract does not require static ALL_OFF readback")
    for artifact, key in (
        (manifest, "file_sha256"),
        (config, "file_sha256"),
        (profile, "file_sha256"),
        (profile, "header_file_sha256"),
    ):
        digest = str(artifact.get(key, ""))
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("selector-control artifact hash is malformed")
    return document


def _tone_plan(condition: Mapping[str, Any], contract: Mapping[str, Any]) -> SafeDdsTonePlan:
    configuration = contract["configuration"]
    if not isinstance(configuration, Mapping):
        raise LeakageLadderError("plan configuration is malformed")
    return SafeDdsTonePlan(
        uri=str(configuration["uri"]),
        serial=str(configuration["serial"]),
        center_frequency_hz=int(condition["center_frequency_hz"]),
        sample_rate_hz=int(condition["sample_rate_hz"]),
        bandwidth_hz=int(condition["bandwidth_hz"]),
        tone_frequency_hz=int(condition["tone_offset_hz"]),
        tx_channel=int(condition["tx_channel"]),
        tx_hardware_gain_db=float(condition["tx_hardware_gain_db"]),
        dds_scale=float(condition["dds_scale"]),
        receiver_gain_db=float(condition["receiver_gain_db"]),
        source_peak_output_bound_dbm=SOURCE_PEAK_OUTPUT_BOUND_DBM,
        load_input_limit_dbm=LOAD_INPUT_LIMIT_DBM,
        path_attenuation_before_load_db=PATH_ATTENUATION_BEFORE_LOAD_DB,
        required_margin_db=REQUIRED_MARGIN_DB,
        settle_ms=100,
    )


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    stage: str,
    source_commit: str,
    pluto_plus_utils_source_attestation: Mapping[str, Any],
    selector_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run = _validate_identifier(run_id, "run ID")
    board = _validate_identifier(board_id, "board ID")
    exact_serial = _validate_serial(serial)
    exact_uri = _validate_usb_uri(uri)
    if stage not in STAGE_CONTRACTS:
        raise ValueError(f"unsupported topology stage: {stage}")
    source = _validate_commit(source_commit, "smateway source commit")
    dependency = _validate_dependency_source_attestation(pluto_plus_utils_source_attestation)
    if stage in SELECTOR_CONNECTED_STAGES:
        if selector_control is None:
            raise ValueError("selector-connected stage requires frozen static ALL_OFF control")
        frozen_selector_control = _validate_selector_control_contract(selector_control)
    elif selector_control is not None:
        raise ValueError("selector-disconnected stage must not include selector control")
    else:
        frozen_selector_control = None
    policy = classify_fast20_center_frequency(
        CENTER_FREQUENCY_HZ,
        allow_experimental_5g8=True,
    )
    conditions = []
    for index, tx_gain_db in enumerate(TX_HARDWARE_GAINS_DB):
        condition = {
            "plan_index": index,
            "condition_id": f"{stage}-tx{tx_gain_db:g}db",
            "stage": stage,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "center_frequency_policy": policy,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "tone_offset_hz": TONE_OFFSET_HZ,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_required_exact_muted": True,
            "tx_hardware_gain_db": tx_gain_db,
            "dds_scale": DDS_SCALE,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": TOTAL_SAMPLES,
            "kernel_buffers": KERNEL_BUFFERS,
            "fresh_stream_required": True,
        }
        _tone_plan(
            condition,
            {"configuration": {"serial": exact_serial, "uri": exact_uri}},
        )
        conditions.append(condition)
    board_state_root = _board_root(board)
    return {
        "schema": 1,
        "plan_kind": "5g8_marker_independent_coherent_leakage_ladder",
        "run_id": run,
        "board_id": board,
        "topology_stage": stage,
        "stage_contract": dict(STAGE_CONTRACTS[stage]),
        "source": {
            "smateway_commit": source,
            "pluto_plus_utils_source_attestation": dependency,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
            "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
            "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
            "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
            "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
        },
        "selector_control": frozen_selector_control,
        "configuration": {
            "serial": exact_serial,
            "uri": exact_uri,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_required_exact_muted": True,
            "dds_scale": DDS_SCALE,
            "tx_hardware_gains_db": list(TX_HARDWARE_GAINS_DB),
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count_per_condition": TOTAL_SAMPLES,
            "duration_s_per_condition": TOTAL_SAMPLES / SAMPLE_RATE_HZ,
            "kernel_buffers": KERNEL_BUFFERS,
            "fresh_stream_per_condition": True,
            "metadata_abi": 2,
            "automatic_retry_count": 0,
            "pilot_frequency_refinement_required": True,
            "minimum_pilot_confidence": MINIMUM_PILOT_CONFIDENCE,
            "minimum_pilot_phase_step_coherence": MINIMUM_PILOT_PHASE_STEP_COHERENCE,
            "maximum_pilot_phase_rms_deg": MAXIMUM_PILOT_PHASE_RMS_DEG,
        },
        "operator_confirmations_required": {
            "no_antennas_anywhere": True,
            "tx1_matched_conducted_network": True,
            "tx2_muted_and_50ohm_terminated": True,
            "rx1_attenuated_conducted_reference": True,
            "exact_stage": stage,
            "topology_confirmation_token": STAGE_CONTRACTS[stage]["confirmation_token"],
        },
        "safety": {
            "exact_serial_and_current_usb_uri_required": True,
            "tx1_only": True,
            "tx2_gain_readback_required_db": -80.0,
            "inactive_dds_scales_required_zero": True,
            "exact_mute_before_stage": True,
            "exact_mute_after_every_condition": True,
            "exact_mute_in_stage_finally": True,
            "headroom_failure_stops_stronger_conditions": True,
            "failure_fragments_are_quarantined": True,
            "automatic_retry_count": 0,
            "read_only_usb_identity_scan_before_rf": True,
            "resolved_usb_uri_must_equal_requested_uri": True,
            "selector_static_all_off_readback_required": stage in SELECTOR_CONNECTED_STAGES,
        },
        "storage": {
            "medium": "raspberry_pi_local_filesystem",
            "board_state_root": str(board_state_root),
            "artifact_root": str(board_state_root / "pluto-usb-captures"),
            "pluto_onboard_storage_used": False,
            "estimated_raw_iq_bytes": (
                len(conditions) * TOTAL_SAMPLES * 2 * 2 * np.dtype("<i2").itemsize
            ),
        },
        "interpretation": {
            "purpose": "diagnose coherent TX1-to-RX2 leakage by physical topology stage",
            "marker_required": False,
            "selector_calibration_claim": False,
            "may_be_used_as_selector_calibration": False,
            "rx2_tone_absence_is_a_valid_low_leakage_result": True,
            "one_hot_path_diagnosis": {
                "implemented_by_this_runner": False,
                "required_future_runner": "run_5g8_one_hot_path_ladder.py",
                "reason": (
                    "per-port path response requires a separate immutable state/readback plan; "
                    "the present runner measures only static ALL_OFF topology leakage"
                ),
            },
        },
        "conditions": conditions,
    }


def _plan_envelope(contract: Mapping[str, Any]) -> dict[str, Any]:
    frozen = dict(contract)
    return {
        "schema": 1,
        "plan_contract": frozen,
        "plan_contract_sha256": canonical_json_sha256(frozen),
        "plan_contract_hash_provenance": (
            "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
        ),
        "immutable": True,
    }


def _validate_plan_envelope(
    document: Mapping[str, Any],
    *,
    expected_contract: Mapping[str, Any],
) -> dict[str, Any]:
    contract = document.get("plan_contract")
    if document.get("schema") != 1 or document.get("immutable") is not True:
        raise LeakageLadderError("immutable plan envelope schema is invalid")
    if not isinstance(contract, Mapping):
        raise LeakageLadderError("immutable plan contract is malformed")
    observed_sha = canonical_json_sha256(contract)
    if document.get("plan_contract_sha256") != observed_sha:
        raise LeakageLadderError("immutable plan canonical contract hash does not match")
    if document.get("plan_contract_hash_provenance") != (
        "UTF-8 json.dumps(sort_keys=True,separators=(',', ':'),allow_nan=False)"
    ):
        raise LeakageLadderError("immutable plan contract hash provenance is invalid")
    if dict(contract) != dict(expected_contract):
        raise LeakageLadderError("requested execution differs from the immutable local plan")
    return dict(document)


def _prepare_plan(path: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    expected = _plan_envelope(contract)
    if path.exists():
        return _validate_plan_envelope(
            _read_json(path, "immutable plan"),
            expected_contract=contract,
        )
    _write_immutable_json(path, expected)
    return expected


def _plan_file_evidence(path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "plan_contract_sha256": str(envelope["plan_contract_sha256"]),
        "plan_contract_hash_provenance": str(envelope["plan_contract_hash_provenance"]),
        "plan_file_sha256": sha256_path(path),
        "plan_file_hash_provenance": "SHA-256 over exact immutable plan.json file bytes",
    }


def _new_manifest(plan_path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    return {
        "schema": 1,
        "run_kind": "5g8_marker_independent_coherent_leakage_ladder",
        "run_id": contract["run_id"],
        "topology_stage": contract["topology_stage"],
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "immutable_plan": _plan_file_evidence(plan_path, envelope),
        "confirmations": [],
        "identity_preflight_attempts": [],
        "identity_preflight": None,
        "preflight_mute_attempts": [],
        "attempts": [],
        "recovery_mute_attempts": [],
        "final_mute_attempts": [],
        "final_mute": None,
        "error": None,
        "summary": {},
        "selector_calibration_claim": False,
    }


def _manifest_summary(manifest: Mapping[str, Any], condition_count: int) -> dict[str, Any]:
    attempts = [item for item in manifest.get("attempts", []) if isinstance(item, Mapping)]
    complete = [item for item in attempts if item.get("status") == "complete"]
    return {
        "planned_conditions": condition_count,
        "attempted_conditions": len(attempts),
        "completed_conditions": len(complete),
        "remaining_conditions": condition_count - len(complete),
        "measurement_quality_passed": sum(
            item.get("outcome") == "measurement_quality_passed" for item in complete
        ),
        "measurement_quality_rejected": sum(
            item.get("outcome") == "measurement_quality_rejected" for item in complete
        ),
        "failed_conditions": sum(item.get("status") == "failed" for item in attempts),
        "quarantine_count": sum(bool(item.get("quarantine")) for item in attempts),
        "selector_calibration_claim": False,
    }


def _persist_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    condition_count: int,
) -> None:
    manifest["updated_at"] = _now()
    manifest["summary"] = _manifest_summary(manifest, condition_count)
    write_json_atomic(path, manifest)


def _load_manifest(
    path: Path,
    *,
    plan_path: Path,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    document = _read_json(path, "leakage-ladder manifest")
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    if (
        document.get("schema") != 1
        or document.get("run_kind") != "5g8_marker_independent_coherent_leakage_ladder"
        or document.get("run_id") != contract.get("run_id")
        or document.get("topology_stage") != contract.get("topology_stage")
        or document.get("selector_calibration_claim") is not False
    ):
        raise LeakageLadderError("manifest identity differs from the immutable plan")
    if document.get("immutable_plan") != _plan_file_evidence(plan_path, envelope):
        raise LeakageLadderError("manifest immutable plan hashes differ from plan bytes/contract")
    list_fields = (
        "confirmations",
        "identity_preflight_attempts",
        "preflight_mute_attempts",
        "attempts",
        "recovery_mute_attempts",
        "final_mute_attempts",
    )
    if any(not isinstance(document.get(field), list) for field in list_fields):
        raise LeakageLadderError("manifest progress arrays are malformed")
    return document


def _validate_confirmations(
    *,
    stage: str,
    confirm_stage: str | None,
    topology_token: str | None,
    no_antennas: bool,
    tx1_matched: bool,
    tx2_terminated_muted: bool,
    rx1_conducted_reference: bool,
    selector_static_all_off: bool = False,
) -> dict[str, Any]:
    if stage not in STAGE_CONTRACTS:
        raise ValueError(f"unsupported topology stage: {stage}")
    if not no_antennas:
        raise LeakageLadderError("execution requires --confirm-no-antennas")
    if not tx1_matched:
        raise LeakageLadderError("execution requires --confirm-tx1-matched-conducted")
    if not tx2_terminated_muted:
        raise LeakageLadderError("execution requires --confirm-tx2-terminated-muted")
    if not rx1_conducted_reference:
        raise LeakageLadderError("execution requires --confirm-rx1-conducted-reference")
    if confirm_stage != stage:
        raise LeakageLadderError(f"execution requires --confirm-stage {stage}")
    expected_token = str(STAGE_CONTRACTS[stage]["confirmation_token"])
    if topology_token != expected_token:
        raise LeakageLadderError(f"execution requires --confirm-topology-token {expected_token}")
    if stage in SELECTOR_CONNECTED_STAGES and not selector_static_all_off:
        raise LeakageLadderError(
            "selector-connected execution requires --confirm-selector-static-all-off"
        )
    if stage not in SELECTOR_CONNECTED_STAGES and selector_static_all_off:
        raise LeakageLadderError(
            "selector-disconnected execution must not confirm selector ALL_OFF"
        )
    return {
        "confirmed_at": _now(),
        "stage": stage,
        "topology_confirmation_token": expected_token,
        "no_antennas_anywhere": True,
        "tx1_matched_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "selector_static_all_off_physically_expected": stage in SELECTOR_CONNECTED_STAGES,
        "confirmation_method": "explicit CLI flags after physical inspection",
    }


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
            "error": _error_document(error),
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


def _call_mute(boundary: MuteBoundary, serial: str, purpose: str) -> dict[str, Any]:
    try:
        result = boundary(serial, purpose)
    except BaseException as error:
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": None,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "purpose": purpose,
            "status": "failed",
            "serial": serial,
            "attestation": "mute_returned_radio_exact_serial_readback",
            "started_at": None,
            "completed_at": _now(),
            "error": {
                "type": "InvalidMuteAttestation",
                "message": "mute boundary did not return an object",
            },
        }
    return result


def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _live_identity_boundary(serial: str, requested_uri: str) -> dict[str, Any]:
    """Resolve the current USB context without opening a radio or changing RF state."""

    started_at = _now()
    resolved_uri = resolve_iio_uri(requested_uri, serial)
    return {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed" if resolved_uri == requested_uri else "failed",
        "serial": serial,
        "requested_uri": requested_uri,
        "resolved_uri": resolved_uri,
        "exact_uri_match": resolved_uri == requested_uri,
        "sysfs_path": find_usb_sysfs_path(serial),
        "scan_mutates_radio_state": False,
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_identity(
    boundary: IdentityBoundary,
    serial: str,
    requested_uri: str,
) -> dict[str, Any]:
    try:
        result = boundary(serial, requested_uri)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": None,
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": requested_uri,
            "resolved_uri": None,
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "completed_at": _now(),
            "error": {
                "type": "InvalidIdentityAttestation",
                "message": "identity boundary did not return an object",
            },
        }
    return result


def _identity_passed(value: object, *, serial: str, requested_uri: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "read_only_current_usb_uri_resolution"
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("requested_uri") == requested_uri
        and value.get("resolved_uri") == requested_uri
        and value.get("exact_uri_match") is True
        and value.get("scan_mutates_radio_state") is False
        and value.get("error") is None
    )


def _verify_selector_artifacts(selector_control: Mapping[str, Any]) -> None:
    for section_name, path_key, hash_key in (
        ("bench_manifest", "path", "file_sha256"),
        ("openocd_config", "path", "file_sha256"),
        ("control_profile", "path", "file_sha256"),
        ("control_profile", "header_path", "header_file_sha256"),
    ):
        section = selector_control.get(section_name)
        if not isinstance(section, Mapping):
            raise LeakageLadderError("selector-control artifact section is malformed")
        path = Path(str(section[path_key])).resolve(strict=True)
        if sha256_path(path) != section.get(hash_key):
            raise LeakageLadderError("selector-control artifact differs from immutable plan")


def _live_selector_all_off_boundary(
    selector_control: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    started_at = _now()
    _verify_selector_artifacts(selector_control)
    manifest_document = selector_control["bench_manifest"]
    config_document = selector_control["openocd_config"]
    profile_document = selector_control["control_profile"]
    command_document = selector_control["command"]
    assert isinstance(manifest_document, Mapping)
    assert isinstance(config_document, Mapping)
    assert isinstance(profile_document, Mapping)
    assert isinstance(command_document, Mapping)
    manifest = BenchManifest.load(Path(str(manifest_document["path"])))
    profile = load_profile(Path(str(profile_document["path"])))
    code = int(command_document["code"])
    if code != profile.all_off_code:
        raise LeakageLadderError("live profile ALL_OFF code differs from immutable plan")
    controller = OpenOcdBench(manifest, Path(str(config_document["path"])))
    before = controller.status()
    commanded = controller.request(code, 0, wait_until_applied=True)
    readback = controller.status()
    passed = (
        commanded.acknowledged_sequence == commanded.command_sequence
        and commanded.applied_code == code
        and readback.applied_code == code
        and readback.command_valid
        and not readback.lease_active
        and not readback.guard_active
        and not readback.invalid_command
    )
    return {
        "schema": 1,
        "evidence_kind": "static_selector_all_off_mailbox_readback",
        "purpose": purpose,
        "status": "passed" if passed else "failed",
        "all_off_code": code,
        "lease_ms": 0,
        "before": before.as_dict(),
        "commanded": commanded.as_dict(),
        "readback": readback.as_dict(),
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_selector(
    boundary: SelectorBoundary,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    try:
        result = boundary(selector_control, purpose)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "all_off_code": selector_control.get("command", {}).get("code")
            if isinstance(selector_control.get("command"), Mapping)
            else None,
            "completed_at": _now(),
            "error": _error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "static_selector_all_off_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "completed_at": _now(),
            "error": {
                "type": "InvalidSelectorAttestation",
                "message": "selector boundary did not return an object",
            },
        }
    return result


def _selector_passed(
    value: object,
    *,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> bool:
    command = selector_control.get("command")
    if not isinstance(command, Mapping):
        return False
    expected_code = command.get("code")
    if not (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "static_selector_all_off_mailbox_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("all_off_code") == expected_code
        and value.get("error") is None
    ):
        return False
    readback = value.get("readback")
    return (
        isinstance(readback, Mapping)
        and readback.get("applied_code") == expected_code
        and readback.get("command_valid") is True
        and readback.get("lease_active") is False
        and readback.get("guard_active") is False
        and readback.get("invalid_command") is False
    )


def _live_capture_boundary(
    plan: SafeDdsTonePlan,
    *,
    samples_per_frame: int,
    frame_count: int,
    kernel_buffers: int,
    block_consumer: Callable[[SampleBlockV2], None],
) -> Any:
    return capture_continuous_safe_dds_tone(
        plan,
        samples_per_frame=samples_per_frame,
        frame_count=frame_count,
        kernel_buffers=kernel_buffers,
        block_consumer=block_consumer,
    )


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
        record.update({key: int(value) for key, value in timing.items() if value is not None})
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


def _quarantine_inventory(root: Path, artifact_id: str) -> dict[str, Any]:
    files = [
        {
            "name": path.name,
            "path": str(path),
            "sha256": sha256_path(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(root.iterdir())
        if path.is_file()
    ]
    return {
        "artifact_id": artifact_id,
        "path": str(root),
        "accepted": False,
        "may_be_used_for_selector_calibration": False,
        "files": files,
    }


def _persist_memory_quarantine(
    capture_root: Path,
    *,
    blocks: list[SampleBlockV2],
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_id = uuid.uuid4().hex
    failed_root = capture_root / ".failed"
    temporary = failed_root / f".{artifact_id}.partial"
    destination = failed_root / f"{artifact_id}.failed"
    failed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary.mkdir(mode=0o700)
    data_file = temporary / f"{artifact_id}.sigmf-data"
    digest = hashlib.sha256()
    with data_file.open("xb") as stream:
        for block in blocks:
            wire = complex_to_ci16(block.samples).tobytes(order="C")
            stream.write(wire)
            digest.update(wire)
        stream.flush()
        os.fsync(stream.fileno())
    ledger = _block_ledger(blocks)
    metadata: dict[str, Any] = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": SAMPLE_RATE_HZ,
            "core:num_channels": 2,
            "pluto:artifact_id": artifact_id,
            "pluto:sha256": digest.hexdigest(),
        },
        "pluto:capture": {
            "sample_count": sum(block.sample_count for block in blocks),
            "receiver_count": 2,
            "incomplete": True,
        },
        "smateway:quarantine": {
            "accepted": False,
            "may_be_used_for_selector_calibration": False,
            "automatic_retry_attempted": False,
            "error": _error_document(error),
            "context": _json_safe(context),
        },
    }
    if ledger is not None:
        metadata["pluto:continuity"] = ledger
    write_json_atomic(temporary / f"{artifact_id}.sigmf-meta", metadata)
    failure = {
        "schema": 1,
        "failure_kind": "5g8_leakage_ladder_capture_quarantine",
        "artifact_id": artifact_id,
        "accepted": False,
        "automatic_retry_attempted": False,
        "may_be_used_for_selector_calibration": False,
        "error": _error_document(error),
        "retained_frame_count": len(blocks),
        "retained_sample_count": sum(block.sample_count for block in blocks),
        "context": _json_safe(context),
        "created_at": _now(),
    }
    write_json_atomic(temporary / "failure.json", failure)
    _fsync_directory(temporary)
    os.replace(temporary, destination)
    _fsync_directory(failed_root)
    return _quarantine_inventory(destination, artifact_id)


def _seal_failed_directory(
    root: Path,
    *,
    artifact_id: str,
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        root / "leakage-ladder-quarantine.json",
        {
            "schema": 1,
            "failure_kind": "5g8_leakage_ladder_post_capture_quarantine",
            "artifact_id": artifact_id,
            "accepted": False,
            "may_be_used_for_selector_calibration": False,
            "automatic_retry_attempted": False,
            "error": _error_document(error),
            "context": _json_safe(context),
            "created_at": _now(),
        },
    )
    return _quarantine_inventory(root, artifact_id)


def _rf_readback_evidence(capture: Any, plan: SafeDdsTonePlan) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [capture.tx_gain_readback_db, -80.0],
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
        planned_dds_scale=DDS_SCALE,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return evidence


def _active_tone_readback_hz(evidence: Mapping[str, Any]) -> float:
    raw = evidence.get("dds_frequency_readback_hz")
    if not isinstance(raw, list) or len(raw) != 8:
        raise LeakageLadderError("DDS frequency readback is malformed")
    active = (abs(float(raw[0])), abs(float(raw[2])))
    tolerance = math.ceil(SAMPLE_RATE_HZ / (1 << 16))
    if abs(active[0] - active[1]) > tolerance:
        raise LeakageLadderError("TX1 I/Q DDS frequency readbacks disagree")
    return sum(active) / 2.0


def _validate_capture_result(
    capture: Any,
    retained: list[SampleBlockV2],
    *,
    plan: SafeDdsTonePlan,
    settings: RadioSettings,
    forbidden_stream_ids: set[int],
) -> tuple[int, dict[str, Any], float]:
    if capture.identity.serial != plan.serial or capture.identity.uri != plan.uri:
        raise LeakageLadderError("capture identity differs from exact serial/current USB URI")
    if capture.settings != settings:
        raise LeakageLadderError("capture settings readback differs from the frozen condition")
    if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
        raise LeakageLadderError("capture sample/frame count differs from the frozen condition")
    if capture.kernel_buffers != KERNEL_BUFFERS:
        raise LeakageLadderError("kernel-buffer readback differs from exact value eight")
    if len(retained) != FRAME_COUNT:
        raise LeakageLadderError("not every metadata frame was retained")
    if any(block.samples.shape != (2, SAMPLES_PER_FRAME) for block in retained):
        raise LeakageLadderError("retained data is not canonical dual-RX 100k-sample frames")
    ledger = _block_ledger(retained)
    if ledger is None:
        raise LeakageLadderError("capture has no ABI2 continuity ledger")
    summary = validate_continuity_ledger(
        ledger,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    if summary.metadata_abi != 2 or summary.first_buffer_sequence != 0:
        raise LeakageLadderError("capture did not begin a fresh ABI2 stream")
    if summary.stream_id in forbidden_stream_ids:
        raise LeakageLadderError("fresh condition reused an earlier stream ID")
    for proof, block in zip(capture.frames, retained, strict=True):
        observed = (
            proof.stream_id,
            proof.buffer_sequence,
            proof.first_sample_sequence,
            proof.last_sample_sequence_exclusive,
            proof.metadata_abi,
        )
        expected = (
            block.stream_id,
            block.buffer_sequence,
            block.first_sample_sequence,
            block.last_sample_sequence_exclusive,
            block.metadata_abi,
        )
        if observed != expected:
            raise LeakageLadderError("capture frame proof differs from retained ABI2 block")
    evidence = _rf_readback_evidence(capture, plan)
    return summary.stream_id, evidence, _active_tone_readback_hz(evidence)


def _artifact_evidence(artifact: Any) -> dict[str, Any]:
    root = Path(artifact.path)
    data_file = data_path(artifact)
    metadata_file = root / f"{artifact.artifact_id}.sigmf-meta"
    return {
        "artifact_id": artifact.artifact_id,
        "path": str(root),
        "data_path": str(data_file),
        "data_sha256": sha256_path(data_file),
        "data_size_bytes": data_file.stat().st_size,
        "metadata_path": str(metadata_file),
        "metadata_sha256": sha256_path(metadata_file),
        "metadata_size_bytes": metadata_file.stat().st_size,
    }


def _capture_condition(
    condition: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
    forbidden_stream_ids: set[int],
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_mute,
    selector_boundary: SelectorBoundary = _live_selector_all_off_boundary,
) -> dict[str, Any]:
    plan = _tone_plan(condition, contract)
    settings = RadioSettings(
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    )
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    context: dict[str, Any] = {
        "condition": dict(condition),
        "topology_stage": contract["topology_stage"],
        "stage_contract": contract["stage_contract"],
        "immutable_plan": dict(plan_evidence),
        "selector_calibration_claim": False,
    }
    stage = str(contract["topology_stage"])
    raw_selector_control = contract.get("selector_control")
    selector_control = raw_selector_control if isinstance(raw_selector_control, Mapping) else None
    selector_before: dict[str, Any] | None = None
    selector_after: dict[str, Any] | None = None
    capture: Any | None = None
    capture_error: BaseException | None = None
    try:
        if stage in SELECTOR_CONNECTED_STAGES:
            if selector_control is None:
                raise LeakageLadderError(
                    "selector-connected condition lacks frozen static ALL_OFF control"
                )
            selector_before = _call_selector(
                selector_boundary,
                selector_control,
                "before_condition",
            )
            context["selector_static_all_off_before"] = selector_before
            if not _selector_passed(
                selector_before,
                selector_control=selector_control,
                purpose="before_condition",
            ):
                raise LeakageLadderError("selector static ALL_OFF pre-condition failed")
        elif selector_control is not None:
            raise LeakageLadderError("selector-disconnected condition included selector control")
        capture = capture_boundary(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=retain,
        )
    except BaseException as error:
        capture_error = error
    finally:
        post_mute = _call_mute(mute_boundary, plan.serial, "post_condition")
        context["post_condition_exact_serial_mute"] = post_mute
        if selector_control is not None:
            selector_after = _call_selector(
                selector_boundary,
                selector_control,
                "after_condition",
            )
            context["selector_static_all_off_after"] = selector_after

    post_mute_passed = _mute_passed(post_mute, serial=plan.serial, purpose="post_condition")
    selector_after_passed = selector_control is None or _selector_passed(
        selector_after,
        selector_control=selector_control,
        purpose="after_condition",
    )
    if capture_error is not None or not post_mute_passed or not selector_after_passed:
        if capture_error is not None:
            failure = capture_error
        elif not post_mute_passed:
            failure = LeakageLadderError("exact post-condition mute attestation failed")
        else:
            failure = LeakageLadderError("selector static ALL_OFF post-condition failed")
        quarantine = _persist_memory_quarantine(
            capture_root,
            blocks=retained,
            error=failure,
            context=context,
        )
        retained.clear()
        raise ConditionCaptureFailure(
            str(failure),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from capture_error
    assert capture is not None

    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        stream_id, rf_readback, tone_readback_hz = _validate_capture_result(
            capture,
            retained,
            plan=plan,
            settings=settings,
            forbidden_stream_ids=forbidden_stream_ids,
        )
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        context["adc_headroom_admission"] = asdict(headroom)
        if not headroom.passed:
            raise LeakageLadderError(
                "ADC headroom admission failed; stronger ladder conditions are forbidden"
            )

        rx1 = np.concatenate([block.samples[0] for block in retained])
        rx2 = np.concatenate([block.samples[1] for block in retained])
        pilot = estimate_coherent_pilot_offset(
            rx1,
            sample_rate_hz=SAMPLE_RATE_HZ,
            nominal_tone_offset_hz=tone_readback_hz,
        )
        pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
        pilot_rejection_reasons: list[str] = []
        if pilot.confidence < MINIMUM_PILOT_CONFIDENCE:
            pilot_rejection_reasons.append("rx1_pilot_confidence_below_minimum")
        if pilot.phase_step_coherence < MINIMUM_PILOT_PHASE_STEP_COHERENCE:
            pilot_rejection_reasons.append("rx1_pilot_phase_step_coherence_below_minimum")
        if pilot_phase_rms_deg > MAXIMUM_PILOT_PHASE_RMS_DEG:
            pilot_rejection_reasons.append("rx1_pilot_phase_rms_above_maximum")
        analysis = analyze_coherent_leakage(
            rx1,
            rx2,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=pilot.estimated_offset_hz,
        )
        measurement_rejection_reasons = [
            *pilot_rejection_reasons,
            *analysis.quality_rejection_reasons,
        ]
        measurement_quality_passed = not measurement_rejection_reasons
        del rx1, rx2

        writer = CaptureWriter(
            capture_root,
            radio=capture.identity,
            settings=settings,
            label=(
                "EXPLORATORY marker-independent 5.8 GHz leakage diagnostic "
                f"{contract['topology_stage']} TX1={plan.tx_hardware_gain_db:g}dB"
            ),
        )
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise LeakageLadderError("finalized SigMF data failed SHA-256 verification")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
        if continuity["stream_id"] != stream_id or continuity["metadata_abi"] != 2:
            raise LeakageLadderError("persisted continuity identity differs from live capture")

        evidence = _artifact_evidence(artifact)
        analysis_document = _json_safe(asdict(analysis))
        record = {
            "schema": 1,
            "record_kind": "5g8_marker_independent_coherent_leakage_condition",
            "created_at": _now(),
            "accepted_raw_artifact": True,
            "accepted_for_selector_calibration": False,
            "may_be_used_as_selector_calibration": False,
            "immutable_plan": {
                **dict(plan_evidence),
            },
            "condition": dict(condition),
            "topology": {
                "stage": contract["topology_stage"],
                "contract": contract["stage_contract"],
                "operator_confirmation_required_by_plan": contract[
                    "operator_confirmations_required"
                ],
            },
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": evidence,
            "capture": {
                "serial": plan.serial,
                "uri": plan.uri,
                "center_frequency_hz": CENTER_FREQUENCY_HZ,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "receiver_gain_db": RECEIVER_GAIN_DB,
                "tx_channel": 0,
                "tx_port": "TX1",
                "tx2_required_exact_muted": True,
                "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
                "dds_scale_requested": DDS_SCALE,
                "tone_offset_hz_requested": TONE_OFFSET_HZ,
                "tone_offset_hz_readback": tone_readback_hz,
                "tone_offset_hz_measured": pilot.estimated_offset_hz,
                "pilot_frequency_refinement": {
                    **_json_safe(asdict(pilot)),
                    "phase_residual_rms_deg": pilot_phase_rms_deg,
                    "minimum_confidence": MINIMUM_PILOT_CONFIDENCE,
                    "minimum_phase_step_coherence": MINIMUM_PILOT_PHASE_STEP_COHERENCE,
                    "maximum_phase_rms_deg": MAXIMUM_PILOT_PHASE_RMS_DEG,
                    "quality_passed": not pilot_rejection_reasons,
                    "quality_rejection_reasons": pilot_rejection_reasons,
                },
                "samples_per_frame": SAMPLES_PER_FRAME,
                "frame_count": FRAME_COUNT,
                "sample_count": TOTAL_SAMPLES,
                "kernel_buffers": KERNEL_BUFFERS,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
                "rf_readback_evidence": rf_readback,
                "adc_headroom_admission": asdict(headroom),
            },
            "continuity_audit": continuity,
            "marker_independent_analysis": analysis_document,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "safety": {
                "post_condition_exact_serial_mute": post_mute,
                "persistence_began_only_after_helper_returned_and_exact_mute_passed": True,
                "tx1_only_readback_validated": True,
                "tx2_minus80db_and_zero_dds_scales_validated": True,
                "fresh_stream_validated": True,
                "selector_static_all_off_before": selector_before,
                "selector_static_all_off_after": selector_after,
                "automatic_retry_count": 0,
                "selector_calibration_claim": False,
            },
            "interpretation": (
                "Topology leakage diagnostic only; this record does not measure or calibrate "
                "selector state paths."
            ),
        }
        record_path = Path(artifact.path) / CONDITION_RECORD_NAME
        write_json_atomic(record_path, record)
        return {
            "condition_id": condition["condition_id"],
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_data_sha256": evidence["data_sha256"],
            "artifact_metadata_sha256": evidence["metadata_sha256"],
            "condition_record_path": str(record_path),
            "condition_record_sha256": sha256_path(record_path),
            "stream_id": stream_id,
            "metadata_abi": 2,
            "headroom_passed": headroom.passed,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "tone_offset_hz_readback": tone_readback_hz,
            "tone_offset_hz_measured": pilot.estimated_offset_hz,
            "pilot_confidence": pilot.confidence,
            "pilot_phase_step_coherence": pilot.phase_step_coherence,
            "pilot_phase_residual_rms_deg": pilot_phase_rms_deg,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "rx2_over_rx1": analysis_document["rx2_over_rx1"],
            "post_condition_exact_serial_mute": post_mute,
            "selector_static_all_off_before": selector_before,
            "selector_static_all_off_after": selector_after,
            "selector_calibration_claim": False,
        }
    except BaseException as error:
        context["post_capture_error"] = _error_document(error)
        if artifact is not None:
            source = Path(artifact.path)
            failed_root = capture_root / ".failed"
            failed_root.mkdir(parents=True, exist_ok=True)
            destination = failed_root / f"{artifact.artifact_id}.failed"
            if source.exists():
                os.replace(source, destination)
            quarantine = _seal_failed_directory(
                destination,
                artifact_id=artifact.artifact_id,
                error=error,
                context=context,
            )
        elif writer is not None:
            finalized_candidate = capture_root / writer.artifact_id
            if finalized_candidate.exists():
                failed_root = capture_root / ".failed"
                failed_root.mkdir(parents=True, exist_ok=True)
                destination = failed_root / f"{writer.artifact_id}.failed"
                os.replace(finalized_candidate, destination)
            else:
                destination = writer.fail(error)
            quarantine = _seal_failed_directory(
                destination,
                artifact_id=writer.artifact_id,
                error=error,
                context=context,
            )
        else:
            quarantine = _persist_memory_quarantine(
                capture_root,
                blocks=retained,
                error=error,
                context=context,
            )
        raise ConditionCaptureFailure(
            str(error),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from error
    finally:
        retained.clear()


def _completed_condition_ids(
    manifest: Mapping[str, Any],
    *,
    planned_condition_ids: set[str] | None = None,
) -> set[str]:
    completed: set[str] = set()
    stream_ids: set[int] = set()
    for raw in manifest.get("attempts", []):
        if not isinstance(raw, Mapping):
            raise LeakageLadderError("manifest attempt is malformed")
        condition_id = raw.get("condition_id")
        if not isinstance(condition_id, str):
            raise LeakageLadderError("manifest condition ID is malformed")
        if planned_condition_ids is not None and condition_id not in planned_condition_ids:
            raise LeakageLadderError("manifest attempt is not bound to an immutable condition")
        if raw.get("status") == "complete":
            if condition_id in completed:
                raise LeakageLadderError("multiple completed attempts exist for one condition")
            result = raw.get("result")
            if (
                not isinstance(result, Mapping)
                or result.get("metadata_abi") != 2
                or result.get("selector_calibration_claim") is not False
                or isinstance(result.get("stream_id"), bool)
                or not isinstance(result.get("stream_id"), int)
            ):
                raise LeakageLadderError("completed attempt evidence is malformed")
            stream_id = int(result["stream_id"])
            if stream_id in stream_ids:
                raise LeakageLadderError("completed conditions reused an ABI2 stream ID")
            stream_ids.add(stream_id)
            completed.add(condition_id)
    return completed


def _execute_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_root: Path,
    capture_boundary: CaptureBoundary = _live_capture_boundary,
    mute_boundary: MuteBoundary = _strict_mute,
    identity_boundary: IdentityBoundary = _live_identity_boundary,
    selector_boundary: SelectorBoundary = _live_selector_all_off_boundary,
) -> None:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    conditions = contract["conditions"]
    assert isinstance(conditions, list)
    serial = str(contract["configuration"]["serial"])
    uri = str(contract["configuration"]["uri"])
    condition_count = len(conditions)
    identity = _call_identity(identity_boundary, serial, uri)
    manifest["identity_preflight_attempts"].append(identity)
    manifest["identity_preflight"] = identity
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not _identity_passed(identity, serial=serial, requested_uri=uri):
        error = LeakageLadderError(
            "read-only USB identity scan did not resolve the exact requested current URI"
        )
        manifest["status"] = "failed"
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    if manifest.get("status") == "failed" or any(
        isinstance(item, Mapping) and item.get("status") == "failed"
        for item in manifest["attempts"]
    ):
        recovery = _call_mute(mute_boundary, serial, "resume_recovery")
        manifest["recovery_mute_attempts"].append(recovery)
        error = LeakageLadderError("failed ladder runs cannot retry; prepare a new run ID")
        manifest["error"] = _error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    stale = [
        item
        for item in manifest["attempts"]
        if isinstance(item, dict) and item.get("status") == "running"
    ]
    pending_error: BaseException | None = None
    manifest["confirmations"].append(dict(confirmation))
    manifest["status"] = "running"
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    try:
        if stale:
            recovery = _call_mute(mute_boundary, serial, "resume_recovery")
            manifest["recovery_mute_attempts"].append(recovery)
            for stale_attempt in stale:
                stale_attempt["status"] = "failed"
                stale_attempt["outcome"] = "stale_process_failed"
                stale_attempt["failure_kind"] = "stale_process"
                stale_attempt["recovery_mute"] = recovery
                stale_attempt["completed_at"] = _now()
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            raise LeakageLadderError("stale live attempt recovered; use a new run ID")

        preflight = _call_mute(mute_boundary, serial, "preflight")
        manifest["preflight_mute_attempts"].append(preflight)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not _mute_passed(preflight, serial=serial, purpose="preflight"):
            raise LeakageLadderError("exact preflight mute attestation failed")

        planned_condition_ids = {
            str(condition["condition_id"])
            for condition in conditions
            if isinstance(condition, Mapping)
        }
        if len(planned_condition_ids) != len(conditions):
            raise LeakageLadderError("immutable plan condition IDs are malformed or duplicated")
        completed = _completed_condition_ids(
            manifest,
            planned_condition_ids=planned_condition_ids,
        )
        forbidden_stream_ids = {
            int(item["result"]["stream_id"])
            for item in manifest["attempts"]
            if isinstance(item, Mapping)
            and item.get("status") == "complete"
            and isinstance(item.get("result"), Mapping)
        }
        for raw_condition in conditions:
            if not isinstance(raw_condition, Mapping):
                raise LeakageLadderError("immutable plan condition is malformed")
            condition = dict(raw_condition)
            condition_id = str(condition["condition_id"])
            if condition_id in completed:
                continue
            attempt: dict[str, Any] = {
                "attempt_id": len(manifest["attempts"]) + 1,
                "condition_id": condition_id,
                "condition": condition,
                "started_at": _now(),
                "completed_at": None,
                "status": "running",
                "outcome": None,
                "failure_kind": None,
                "result": None,
                "quarantine": None,
                "post_condition_exact_serial_mute": None,
                "error": None,
                "automatic_retry_attempted": False,
            }
            manifest["attempts"].append(attempt)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            try:
                result = _capture_condition(
                    condition,
                    contract=contract,
                    plan_evidence=_plan_file_evidence(plan_path, envelope),
                    capture_root=capture_root,
                    forbidden_stream_ids=forbidden_stream_ids,
                    capture_boundary=capture_boundary,
                    mute_boundary=mute_boundary,
                    selector_boundary=selector_boundary,
                )
            except ConditionCaptureFailure as error:
                attempt["status"] = "failed"
                attempt["outcome"] = "condition_failed"
                attempt["failure_kind"] = "capture_or_validation"
                attempt["quarantine"] = error.quarantine
                attempt["post_condition_exact_serial_mute"] = error.post_mute
                attempt["error"] = _error_document(error)
                attempt["completed_at"] = _now()
                _persist_manifest(manifest_path, manifest, condition_count=condition_count)
                raise
            attempt["result"] = result
            attempt["post_condition_exact_serial_mute"] = result["post_condition_exact_serial_mute"]
            attempt["outcome"] = (
                "measurement_quality_passed"
                if result["measurement_quality_passed"]
                else "measurement_quality_rejected"
            )
            attempt["status"] = "complete"
            attempt["completed_at"] = _now()
            forbidden_stream_ids.add(int(result["stream_id"]))
            completed.add(condition_id)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        rejected = [
            item
            for item in manifest["attempts"]
            if isinstance(item, Mapping) and item.get("outcome") == "measurement_quality_rejected"
        ]
        if rejected:
            raise LeakageLadderError(
                f"{len(rejected)} condition(s) failed measurement quality; stage is not successful"
            )
        manifest["status"] = "conditions_complete"
    except BaseException as error:
        pending_error = error
        manifest["error"] = _error_document(error)
        manifest["status"] = "failed"
    finally:
        final_mute = _call_mute(mute_boundary, serial, "final")
        manifest["final_mute_attempts"].append(final_mute)
        manifest["final_mute"] = final_mute
        if not _mute_passed(final_mute, serial=serial, purpose="final"):
            pending_error = LeakageLadderError("exact final mute attestation failed")
            manifest["error"] = _error_document(pending_error)
            manifest["status"] = "failed"
        elif pending_error is None:
            manifest["status"] = "complete"
            manifest["completed_at"] = _now()
            manifest["error"] = None
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if pending_error is not None:
        raise pending_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="current exact usb: IIO URI")
    parser.add_argument("--stage", choices=STAGES, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-only", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-tx1-matched-conducted", action="store_true")
    parser.add_argument("--confirm-tx2-terminated-muted", action="store_true")
    parser.add_argument("--confirm-rx1-conducted-reference", action="store_true")
    parser.add_argument("--confirm-selector-static-all-off", action="store_true")
    parser.add_argument("--confirm-stage", choices=STAGES)
    parser.add_argument("--confirm-topology-token")
    parser.add_argument("--bench-manifest", type=Path)
    parser.add_argument("--openocd-config", type=Path)
    parser.add_argument("--profile", type=Path)
    return parser


def _signal_handler(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received {signal.Signals(signum).name}; entering fail-muted cleanup")


def _install_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    repository = Path(__file__).resolve().parents[1]
    try:
        source_commit = _repository_commit_and_require_clean(repository, "smateway")
        dependency_attestation = attest_pluto_plus_utils_source()
        selector_control = None
        if args.stage in SELECTOR_CONNECTED_STAGES:
            if not all((args.bench_manifest, args.openocd_config, args.profile)):
                raise LeakageLadderError(
                    "selector-connected stage requires --bench-manifest, --openocd-config, "
                    "and --profile"
                )
            selector_control = _selector_control_contract(
                bench_manifest_path=args.bench_manifest,
                openocd_config_path=args.openocd_config,
                profile_path=args.profile,
            )
        elif any((args.bench_manifest, args.openocd_config, args.profile)):
            raise LeakageLadderError(
                "selector-disconnected stage must not include selector-control files"
            )
        contract = _build_plan_contract(
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            stage=args.stage,
            source_commit=source_commit,
            pluto_plus_utils_source_attestation=dependency_attestation,
            selector_control=selector_control,
        )
    except (LeakageLadderError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    board_root = _board_root(str(contract["board_id"]))
    run_root = board_root / "5g8-leakage-ladder" / str(contract["run_id"])
    plan_path = run_root / PLAN_FILENAME
    manifest_path = run_root / MANIFEST_FILENAME
    with _board_lock(board_root):
        if args.plan_only:
            envelope = _prepare_plan(plan_path, contract)
            manifest = (
                _load_manifest(
                    manifest_path,
                    plan_path=plan_path,
                    envelope=envelope,
                )
                if manifest_path.exists()
                else _new_manifest(plan_path, envelope)
            )
            _persist_manifest(
                manifest_path,
                manifest,
                condition_count=len(contract["conditions"]),
            )
            print(
                json.dumps(
                    {
                        "run_id": contract["run_id"],
                        "stage": contract["topology_stage"],
                        "status": manifest["status"],
                        "immutable_plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "plan_file_sha256": sha256_path(plan_path),
                        "manifest": str(manifest_path),
                        "condition_count": len(contract["conditions"]),
                        "selector_calibration_claim": False,
                    },
                    sort_keys=True,
                )
            )
            return 0

        if not plan_path.is_file() or not manifest_path.is_file():
            raise SystemExit("execute requires a prior successful --plan-only invocation")
        try:
            envelope = _validate_plan_envelope(
                _read_json(plan_path, "immutable plan"),
                expected_contract=contract,
            )
            manifest = _load_manifest(
                manifest_path,
                plan_path=plan_path,
                envelope=envelope,
            )
            confirmation = _validate_confirmations(
                stage=args.stage,
                confirm_stage=args.confirm_stage,
                topology_token=args.confirm_topology_token,
                no_antennas=args.confirm_no_antennas,
                tx1_matched=args.confirm_tx1_matched_conducted,
                tx2_terminated_muted=args.confirm_tx2_terminated_muted,
                rx1_conducted_reference=args.confirm_rx1_conducted_reference,
                selector_static_all_off=args.confirm_selector_static_all_off,
            )
            _execute_stage(
                manifest,
                manifest_path,
                envelope=envelope,
                plan_path=plan_path,
                confirmation=confirmation,
                capture_root=board_root / "pluto-usb-captures",
            )
        except (LeakageLadderError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "run_id": contract["run_id"],
                    "stage": contract["topology_stage"],
                    "status": manifest["status"],
                    "manifest": str(manifest_path),
                    "summary": manifest["summary"],
                    "selector_calibration_claim": False,
                },
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
