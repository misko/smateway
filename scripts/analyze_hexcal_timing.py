#!/usr/bin/env python3
"""Verify and analyze exactly two independent 2 MS/s hexcal timing artifacts."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict
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

import numpy as np

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    canonical_json_sha256,
    load_ci16_channel,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.hexcal_timing import (
    BANDWIDTH_HZ,
    SAMPLE_RATE_HZ,
    analyze_hexcal_timing_samples,
)
from smateway.hexcal_gain import (
    EXPERIMENTAL_5G8_STIMULUS_PROTOCOL_ID,
    QUALIFICATION_SOURCE_FILES,
    STIMULUS_PROTOCOL_ID,
    STIMULUS_PROTOCOLS,
    load_hexcal_stimulus_qualification,
    stimulus_protocol,
)

__all__ = (
    "EXPERIMENTAL_5G8_STIMULUS_PROTOCOL_ID",
    "STIMULUS_PROTOCOL_ID",
)

CAPTURE_RECORD_NAME = "hexcal-timing-capture.json"
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 9
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
EXPECTED_DATA_SIZE_BYTES = TOTAL_SAMPLES * 2 * 2 * 2
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
ANALYSIS_SOURCE_FILES = (
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.1-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.2-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.3-experimental-5g8-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.4-experimental-5g8-high-rx-stimulus.json",
    "scripts/qualify_hexcal_rx_gain.py",
    "src/smateway/hexcal_gain.py",
    "src/smateway/hexcal_timing.py",
    "scripts/capture_hexcal_timing.py",
    "scripts/analyze_hexcal_timing.py",
)
MAXIMUM_REPLICATE_SLOT_MEDIAN_DELTA_US = 1.0
MAXIMUM_REPLICATE_CYCLE_MEDIAN_DELTA_US = 2.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture-record",
        type=Path,
        action="append",
        required=True,
        help="repeat exactly twice, once for each independent full artifact",
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _integer(value: object, label: str, expected: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if expected is not None and value != expected:
        raise ValueError(f"{label} must be exactly {expected}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _sha256(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return digest


def _canonical_sha256(document: object) -> str:
    wire = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(wire).hexdigest()


def _mute_attestation_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _exact_file(path_value: object, label: str) -> Path:
    path = Path(_string(path_value, f"{label}.path")).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file")
    return path


def _verify_file_evidence(
    evidence: Mapping[str, Any], *, label: str, expected_path: Path | None = None
) -> Path:
    path = _exact_file(evidence.get("path"), label)
    if expected_path is not None and path != expected_path.resolve():
        raise ValueError(f"{label} path differs from its artifact-bound path")
    expected_size = _integer(evidence.get("size_bytes"), f"{label}.size_bytes")
    expected_sha = _sha256(evidence.get("sha256"), f"{label}.sha256")
    if path.stat().st_size != expected_size or sha256_path(path) != expected_sha:
        raise ValueError(f"{label} bytes differ from their immutable evidence")
    return path


def _verify_source_commit(repository: Path, source: Mapping[str, Any]) -> str:
    commit = _string(source.get("commit"), "source.commit")
    subprocess.run(
        ("git", "cat-file", "-e", f"{commit}^{{commit}}"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    raw_files = source.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("source.files must be a nonempty array")
    observed_paths: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        file_document = _mapping(raw_file, f"source.files[{index}]")
        relative = _string(file_document.get("path"), f"source.files[{index}].path")
        if relative.startswith("/") or ".." in Path(relative).parts or relative in observed_paths:
            raise ValueError("source file paths must be unique repository-relative paths")
        expected_sha = _sha256(file_document.get("sha256"), f"source.files[{index}].sha256")
        committed = subprocess.run(
            ("git", "show", f"{commit}:{relative}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        if hashlib.sha256(committed).hexdigest() != expected_sha:
            raise ValueError(f"source hash differs from commit object: {relative}")
        observed_paths.add(relative)
    required = {
        "src/smateway/hexcal_timing.py",
        "scripts/capture_hexcal_timing.py",
        "scripts/analyze_hexcal_timing.py",
        "profiles/hexcal-v1/control_profile.json",
    }
    if not required.issubset(observed_paths):
        raise ValueError("source manifest omits timing implementation files")
    return commit


def _normalized_json(value: object) -> object:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _recompute_headroom(data_file: Path) -> dict[str, Any]:
    raw = np.memmap(data_file, dtype="<i2", mode="r")
    expected_components = TOTAL_SAMPLES * 2 * 2
    if raw.size != expected_components:
        raise ValueError("timing data is not canonical dual-RX CI16")
    components = raw.reshape(TOTAL_SAMPLES, 2, 2)
    monitor = AdcHeadroomMonitor(receiver_count=2)
    for start in range(0, TOTAL_SAMPLES, SAMPLES_PER_FRAME):
        stop = start + SAMPLES_PER_FRAME
        block = np.empty((2, SAMPLES_PER_FRAME), dtype=np.complex64)
        block.real = components[start:stop, :, 0].T
        block.imag = components[start:stop, :, 1].T
        monitor.observe(block)
    return asdict(monitor.result())


def _artifact_bound_paths(record_path: Path, artifact_id: str) -> tuple[Path, Path]:
    artifact_root = record_path.parent.resolve()
    if artifact_root.name != artifact_id or record_path.name != CAPTURE_RECORD_NAME:
        raise ValueError("capture record is not inside its artifact-ID directory")
    return (
        artifact_root / f"{artifact_id}.sigmf-data",
        artifact_root / f"{artifact_id}.sigmf-meta",
    )


def _validate_pair_plan_binding(
    root: Mapping[str, Any], capture: Mapping[str, Any]
) -> Mapping[str, Any]:
    plan = _mapping(root.get("pair_plan_contract"), "pair_plan_contract")
    expected_sha = _sha256(root.get("pair_plan_contract_sha256"), "pair_plan_contract_sha256")
    if _canonical_sha256(plan) != expected_sha:
        raise ValueError("pair plan contract SHA-256 does not match its canonical bytes")
    plan_kind = plan.get("plan_kind")
    supported_plan_kinds = {
        "hexcal_v1_rf_timing_two_replicates": "hexcal-v1",
        **{
            contract.timing_plan_kind: contract.protocol_id
            for contract in STIMULUS_PROTOCOLS.values()
        },
    }
    if plan.get("schema") != 1 or plan_kind not in supported_plan_kinds:
        raise ValueError("pair plan schema or kind is unsupported")
    if plan.get("protocol_id") != supported_plan_kinds[plan_kind]:
        raise ValueError("pair plan protocol ID differs from its plan kind")
    if plan_kind != "hexcal_v1_rf_timing_two_replicates":
        contract = stimulus_protocol(str(plan.get("protocol_id")))
        qualification = _mapping(
            plan.get("stimulus_qualification"),
            "pair_plan_contract.stimulus_qualification",
        )
        if (
            qualification.get("fixed_receiver_gain_db")
            != _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus").get(
                "calibration_receiver_gain_db"
            )
            or qualification.get("fixed_receiver_gain_db") != contract.fixed_receiver_gain_db
            or _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus").get("receiver_gain_db")
            != contract.timing_receiver_gain_db
            or qualification.get("selected_tx_hardware_gain_db")
            != _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus").get(
                "tx_hardware_gain_db_requested"
            )
            or qualification.get("dds_scale")
            != _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus").get(
                "dds_scale_requested"
            )
            or _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus").get(
                "center_frequency_hz"
            )
            != contract.center_frequencies_hz[0]
        ):
            raise ValueError("v2 timing stimulus differs from its frozen qualification")
    elif "stimulus_qualification" in plan:
        raise ValueError("legacy timing plan unexpectedly contains a v2 qualification")
    if plan.get("run_id") != root.get("run_id"):
        raise ValueError("capture run ID differs from pair plan")
    if plan.get("source") != root.get("source"):
        raise ValueError("capture source evidence differs from pair plan")
    dependency = _mapping(
        root.get("pluto_plus_utils_source_attestation"),
        "pluto_plus_utils_source_attestation",
    )
    dependency_sha256 = _sha256(
        root.get("pluto_plus_utils_source_attestation_sha256"),
        "pluto_plus_utils_source_attestation_sha256",
    )
    if (
        plan.get("pluto_plus_utils_source_attestation") != dependency
        or plan.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
        or canonical_json_sha256(dependency) != dependency_sha256
    ):
        raise ValueError("capture dependency evidence differs from pair plan")
    if plan.get("profile") != root.get("source_profile"):
        raise ValueError("capture profile evidence differs from pair plan")
    if plan.get("firmware") != root.get("firmware_evidence"):
        raise ValueError("capture firmware evidence differs from pair plan")
    for field in ("board_id", "serial", "uri"):
        if plan.get(field) != capture.get(field):
            raise ValueError(f"capture {field} differs from pair plan")
    plan_capture = _mapping(plan.get("capture"), "pair_plan_contract.capture")
    exact_capture_fields = {
        "replicate_count": 2,
        "fresh_stream_per_replicate": True,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "total_samples": TOTAL_SAMPLES,
        "kernel_buffers": KERNEL_BUFFERS,
        "receiver_channels": [0, 1],
        "metadata_abi": 2,
    }
    if any(plan_capture.get(key) != value for key, value in exact_capture_fields.items()):
        raise ValueError("pair plan capture shape differs from frozen timing contract")
    plan_stimulus = _mapping(plan.get("stimulus"), "pair_plan_contract.stimulus")
    if plan_stimulus.get("tx_channel") != 0 or plan_stimulus.get("tx_port") != "TX1":
        raise ValueError("pair plan stimulus is not TX1-only")
    if plan_stimulus.get("tx2_required_muted") is not True:
        raise ValueError("pair plan does not require TX2 muted")
    stimulus_bindings = {
        "center_frequency_hz": "center_frequency_hz",
        "tone_offset_hz_requested": "tone_offset_hz_requested",
        "tx_hardware_gain_db_requested": "tx_hardware_gain_db_requested",
        "dds_scale_requested": "dds_scale_requested",
        "receiver_gain_db": "receiver_gain_db",
        "worst_case_load_input_dbm": "worst_case_load_input_dbm",
    }
    for plan_field, capture_field in stimulus_bindings.items():
        if plan_stimulus.get(plan_field) != capture.get(capture_field):
            raise ValueError(f"capture {capture_field} differs from pair plan stimulus")
    if plan.get("center_frequency_policy") != capture.get("center_frequency_policy"):
        raise ValueError("capture frequency policy differs from pair plan")

    requested_scale = _number(plan_stimulus.get("dds_scale_requested"), "dds scale")
    requested_gain = _number(plan_stimulus.get("tx_hardware_gain_db_requested"), "TX gain")
    rf_readback = _mapping(capture.get("rf_readback_evidence"), "capture.rf_readback_evidence")
    rf_readback_sha256 = canonical_json_sha256(rf_readback)
    normalized_rf_readback = validate_tx1_rf_readback_evidence(
        rf_readback,
        planned_kernel_buffers=KERNEL_BUFFERS,
        planned_tx_gain_db=requested_gain,
        planned_dds_scale=requested_scale,
        planned_tone_hz=_number(plan_stimulus.get("tone_offset_hz_requested"), "requested tone"),
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    if (
        capture.get("rf_readback_evidence_sha256") != rf_readback_sha256
        or capture.get("kernel_buffers") != normalized_rf_readback["kernel_buffers"]
        or capture.get("tx_gain_readback_db") != normalized_rf_readback["tx1_gain_readback_db"]
        or capture.get("dds_scale_readback") != normalized_rf_readback["dds_scale_readback"]
        or capture.get("dds_enabled_readback") != normalized_rf_readback["dds_enabled_readback"]
        or capture.get("dds_frequency_readback_hz")
        != normalized_rf_readback["dds_frequency_readback_hz"]
    ):
        raise ValueError("RF readback evidence is not hash-bound to capture fields")
    readback_gain = _number(capture.get("tx_gain_readback_db"), "TX gain readback")
    if readback_gain > requested_gain + 0.25:
        raise ValueError("TX gain readback is above the pair plan")
    scales_raw = capture.get("dds_scale_readback")
    enabled_raw = capture.get("dds_enabled_readback")
    frequencies_raw = capture.get("dds_frequency_readback_hz")
    if (
        not isinstance(scales_raw, list)
        or not isinstance(enabled_raw, list)
        or not isinstance(frequencies_raw, list)
        or len(scales_raw) != 8
        or len(enabled_raw) != 8
        or len(frequencies_raw) != 8
    ):
        raise ValueError("DDS readbacks are not the canonical eight-source layout")
    active = {0, 2}
    for index in range(8):
        scale = _number(scales_raw[index], f"DDS scale[{index}]")
        expected_scale = requested_scale if index in active else 0.0
        if abs(abs(scale) - expected_scale) > 1e-6:
            raise ValueError("DDS scales do not select only the TX1 I/Q pair")
        if not isinstance(enabled_raw[index], bool):
            raise ValueError("DDS enabled readback values must be booleans")
        if index in active and enabled_raw[index] is not True:
            raise ValueError("TX1 I/Q DDS sources were not both enabled")
        if isinstance(frequencies_raw[index], bool) or not isinstance(frequencies_raw[index], int):
            raise ValueError("DDS frequency readbacks must be integers")
    tolerance_hz = math.ceil(SAMPLE_RATE_HZ / (1 << 16))
    requested_tone = _number(plan_stimulus.get("tone_offset_hz_requested"), "requested tone")
    if any(
        abs(abs(int(frequencies_raw[index])) - abs(requested_tone)) > tolerance_hz
        for index in active
    ):
        raise ValueError("active DDS frequency readbacks differ from the pair plan")
    tone_readback = _number(capture.get("tone_offset_readback_hz"), "tone readback")
    active_readback = sum(abs(int(frequencies_raw[index])) for index in active) / 2.0
    if tone_readback != active_readback:
        raise ValueError("derived tone readback differs from full DDS readbacks")
    tx_contract = _mapping(capture.get("tx_readback_contract"), "tx_readback_contract")
    if (
        tx_contract.get("selected_tx_gain_readback_db") != capture.get("tx_gain_readback_db")
        or tx_contract.get("unselected_tx2_gain_readback_db_attested_by_helper") != -80.0
        or tx_contract.get("active_dds_indices") != [0, 2]
        or tx_contract.get("inactive_dds_scales_required_zero") is not True
        or tx_contract.get("tx2_never_enabled") is not True
    ):
        raise ValueError("TX1-only helper readback contract is incomplete")
    safety = _mapping(root.get("capture_safety"), "capture_safety")
    replicate = _integer(root.get("replicate_index"), "replicate_index")
    expected_purpose = f"post_replicate_{replicate}"
    if (
        safety.get("refill_callback_action") != "copy_to_ram_only"
        or safety.get("disk_persistence_began_after_helper_returned_and_tx_was_muted") is not True
        or safety.get("tx2_never_enabled") is not True
        or safety.get("no_automatic_retry") is not True
        or safety.get("sigint_sigterm_sighup_are_cooperative_exceptions") is not True
        or safety.get("sigkill_cannot_be_intercepted") is not True
        or not _mute_attestation_passed(
            safety.get("post_helper_exact_serial_mute"),
            serial=_string(capture.get("serial"), "capture.serial"),
            purpose=expected_purpose,
        )
    ):
        raise ValueError("capture safety or post-helper mute attestation is incomplete")
    plan_safety = _mapping(plan.get("safety"), "pair_plan_contract.safety")
    if (
        plan_safety.get("ram_only_until_exact_serial_mute_passes") is not True
        or plan_safety.get("per_replicate_exact_serial_mute_required") is not True
        or plan_safety.get("final_exact_serial_mute_required") is not True
        or plan_safety.get("automatic_retry_count") != 0
        or plan_safety.get("sigkill_cannot_be_intercepted") is not True
    ):
        raise ValueError("pair plan safety contract is incomplete")
    return plan


def _bind_capture_record_to_continuity(
    capture: Mapping[str, Any], continuity: Mapping[str, Any]
) -> None:
    exact = {
        "stream_id": "stream_id",
        "first_buffer_sequence": "first_buffer_sequence",
        "last_buffer_sequence": "last_buffer_sequence",
        "first_sample_sequence": "first_sample_sequence",
        "last_sample_sequence_exclusive": "last_sample_sequence_exclusive",
    }
    for capture_field, continuity_field in exact.items():
        if capture.get(capture_field) != continuity.get(continuity_field):
            raise ValueError(f"capture {capture_field} differs from replayed ABI2 continuity")
    if capture.get("metadata_abi") != continuity.get("metadata_abi"):
        raise ValueError("capture metadata ABI differs from replayed continuity")
    if (
        continuity.get("first_buffer_sequence") != 0
        or continuity.get("last_buffer_sequence") != FRAME_COUNT - 1
    ):
        raise ValueError("replayed buffer sequence endpoints are not exactly 0..8")
    first_sample = _integer(continuity.get("first_sample_sequence"), "continuity first sample")
    last_sample = _integer(
        continuity.get("last_sample_sequence_exclusive"), "continuity last sample"
    )
    if last_sample - first_sample != TOTAL_SAMPLES:
        raise ValueError("replayed sample-sequence endpoints do not span 900,000 samples")


def _load_and_verify_record(
    record_path: Path,
    *,
    repository: Path,
    profile: Any,
) -> tuple[dict[str, Any], np.ndarray, dict[str, Any]]:
    resolved_record = record_path.expanduser().resolve(strict=True)
    try:
        document = json.loads(resolved_record.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load timing capture record: {error}") from error
    root = dict(_mapping(document, "capture record"))
    capture_kind = root.get("capture_kind")
    supported_capture_kinds = {
        "hexcal_v1_rf_timing_2msps_tx1",
        *(contract.timing_capture_kind for contract in STIMULUS_PROTOCOLS.values()),
    }
    if root.get("schema") != 1 or capture_kind not in supported_capture_kinds:
        raise ValueError("capture record schema or kind is unsupported")
    if root.get("accepted") is not True or root.get("accepted_retry_fragment") is not False:
        raise ValueError("capture record is not an accepted full non-retry artifact")
    if root.get("automatic_retry_count") != 0 or root.get("required_replicate_count") != 2:
        raise ValueError("capture retry/replicate contract differs from timing-v1")
    replicate_index = _integer(root.get("replicate_index"), "replicate_index")
    if replicate_index not in (1, 2):
        raise ValueError("replicate_index must be one or two")

    artifact = _mapping(root.get("artifact"), "artifact")
    artifact_id = _string(artifact.get("artifact_id"), "artifact.artifact_id")
    artifact_root = resolved_record.parent.resolve()
    if Path(_string(artifact.get("path"), "artifact.path")).resolve() != artifact_root:
        raise ValueError("artifact summary path differs from its capture record directory")
    if _integer(artifact.get("sample_count"), "artifact.sample_count") != TOTAL_SAMPLES:
        raise ValueError("artifact sample count differs from exact 450 ms shape")
    if _integer(artifact.get("receiver_count"), "artifact.receiver_count") != 2:
        raise ValueError("artifact receiver count differs from dual RX")
    expected_data, expected_meta = _artifact_bound_paths(resolved_record, artifact_id)
    evidence = _mapping(root.get("artifact_evidence"), "artifact_evidence")
    data_evidence = {
        "path": evidence.get("data_path"),
        "sha256": evidence.get("data_sha256"),
        "size_bytes": evidence.get("data_size_bytes"),
    }
    metadata_evidence = {
        "path": evidence.get("metadata_path"),
        "sha256": evidence.get("metadata_sha256"),
        "size_bytes": evidence.get("metadata_size_bytes"),
    }
    data_file = _verify_file_evidence(
        data_evidence, label="SigMF data", expected_path=expected_data
    )
    metadata_file = _verify_file_evidence(
        metadata_evidence, label="SigMF metadata", expected_path=expected_meta
    )
    if data_file.stat().st_size != EXPECTED_DATA_SIZE_BYTES:
        raise ValueError("SigMF data byte count differs from exact dual-RX shape")
    if artifact.get("sha256") != data_evidence["sha256"]:
        raise ValueError("artifact summary data SHA-256 differs from immutable evidence")
    try:
        metadata = _mapping(json.loads(metadata_file.read_bytes()), "SigMF metadata")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load SigMF metadata: {error}") from error
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
    )
    if continuity != root.get("continuity_audit"):
        raise ValueError("persisted continuity audit differs from independent replay")

    capture = _mapping(root.get("capture"), "capture")
    _validate_pair_plan_binding(root, capture)
    _bind_capture_record_to_continuity(capture, continuity)
    _integer(capture.get("sample_rate_hz"), "capture.sample_rate_hz", SAMPLE_RATE_HZ)
    _integer(capture.get("bandwidth_hz"), "capture.bandwidth_hz", BANDWIDTH_HZ)
    _integer(
        capture.get("samples_per_frame"),
        "capture.samples_per_frame",
        SAMPLES_PER_FRAME,
    )
    _integer(capture.get("frame_count"), "capture.frame_count", FRAME_COUNT)
    _integer(capture.get("sample_count"), "capture.sample_count", TOTAL_SAMPLES)
    _integer(capture.get("kernel_buffers"), "capture.kernel_buffers", KERNEL_BUFFERS)
    if _number(capture.get("duration_s"), "capture.duration_s") != 0.45:
        raise ValueError("capture duration is not exactly 450 ms")
    _integer(capture.get("tx_channel"), "capture.tx_channel", 0)
    if capture.get("tx_port") != "TX1":
        raise ValueError("timing capture did not use TX1")
    center_frequency_hz = _integer(
        capture.get("center_frequency_hz"), "capture.center_frequency_hz"
    )
    if _number(artifact.get("sample_rate_hz"), "artifact.sample_rate_hz") != SAMPLE_RATE_HZ:
        raise ValueError("artifact summary sample rate differs from exact plan")
    if (
        _number(artifact.get("center_frequency_hz"), "artifact.center_frequency_hz")
        != center_frequency_hz
    ):
        raise ValueError("artifact summary center frequency differs from capture plan")
    serial = _string(capture.get("serial"), "capture.serial")
    uri = _string(capture.get("uri"), "capture.uri")
    if not uri.removeprefix("pluto://").startswith("usb:"):
        raise ValueError("timing capture URI is not an exact USB context")
    tone_readback = _number(
        capture.get("tone_offset_readback_hz"), "capture.tone_offset_readback_hz"
    )
    if abs(tone_readback) >= SAMPLE_RATE_HZ / 2:
        raise ValueError("DDS readback is outside Nyquist")
    if capture.get("metadata_abi") != 2:
        raise ValueError("timing capture metadata ABI is not exactly two")
    if capture.get("adc_headroom_admission", {}).get("passed") is not True:
        raise ValueError("persisted ADC headroom admission did not pass")
    recomputed_headroom = _recompute_headroom(data_file)
    if _normalized_json(recomputed_headroom) != _normalized_json(
        capture.get("adc_headroom_admission")
    ):
        raise ValueError("ADC headroom evidence differs from raw CI16 replay")

    global_metadata = _mapping(metadata.get("global"), "SigMF global")
    if (
        _number(global_metadata.get("core:sample_rate"), "core:sample_rate") != SAMPLE_RATE_HZ
        or _integer(global_metadata.get("core:num_channels"), "core:num_channels") != 2
        or global_metadata.get("pluto:artifact_id") != artifact_id
        or global_metadata.get("pluto:sha256") != data_evidence["sha256"]
    ):
        raise ValueError("SigMF global identity or shape differs from capture evidence")
    sigmf_capture = _mapping(metadata.get("pluto:capture"), "pluto:capture")
    initial_settings = _mapping(
        sigmf_capture.get("initial_settings"), "pluto:capture.initial_settings"
    )
    expected_settings = {
        "center_frequency_hz": center_frequency_hz,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bandwidth_hz": BANDWIDTH_HZ,
        "gain_mode": "manual",
        "gain_db": capture.get("receiver_gain_db"),
        "channels": [0, 1],
    }
    if any(initial_settings.get(key) != value for key, value in expected_settings.items()):
        raise ValueError("SigMF initial radio settings differ from the pair plan")
    epochs = metadata.get("captures")
    if not isinstance(epochs, list) or len(epochs) != 1:
        raise ValueError("SigMF must contain exactly one immutable settings epoch")
    epoch = _mapping(epochs[0], "captures[0]")
    if epoch.get("sample_start") != 0 or epoch.get("settings") != initial_settings:
        raise ValueError("SigMF settings epoch differs from initial settings")

    source = _mapping(root.get("source"), "source")
    source_commit = _verify_source_commit(repository, source)
    recorded_dependency = _mapping(
        root.get("pluto_plus_utils_source_attestation"),
        "pluto_plus_utils_source_attestation",
    )
    dependency_attestation = attest_pluto_plus_utils_source()
    dependency_sha256 = canonical_json_sha256(dependency_attestation)
    if (
        dependency_attestation != dict(recorded_dependency)
        or root.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
    ):
        raise ValueError("pluto-plus-utils source evidence differs on replay")
    source_profile = _mapping(root.get("source_profile"), "source_profile")
    if (
        source_profile.get("file_sha256") != profile.file_sha256
        or source_profile.get("contract_sha256") != profile.contract_sha256
    ):
        raise ValueError("capture profile hashes differ from the selected exact profile")
    firmware_raw = _mapping(root.get("firmware_evidence"), "firmware_evidence")
    board_id = _string(capture.get("board_id"), "capture.board_id")
    firmware_path = Path(_string(firmware_raw.get("path"), "firmware_evidence.path"))
    firmware = load_hexcal_firmware_evidence(
        firmware_path,
        expected_board_id=board_id,
        expected_source_commit=source_commit,
        expected_profile=profile,
    )
    if firmware.as_dict() != firmware_raw:
        raise ValueError("firmware evidence differs from its independently replayed form")

    plan = _mapping(root.get("pair_plan_contract"), "pair_plan_contract")
    protocol_id = str(plan.get("protocol_id"))
    contract = None if protocol_id == "hexcal-v1" else stimulus_protocol(protocol_id)
    expected_capture_kind = (
        contract.timing_capture_kind if contract is not None else "hexcal_v1_rf_timing_2msps_tx1"
    )
    if capture_kind != expected_capture_kind:
        raise ValueError("capture kind differs from its pair-plan protocol")
    stimulus_qualification_sha256: str | None = None
    if contract is not None:
        qualification_summary = _mapping(
            plan.get("stimulus_qualification"),
            "pair_plan_contract.stimulus_qualification",
        )
        qualification_path = _exact_file(
            qualification_summary.get("path"),
            "pair_plan_contract.stimulus_qualification",
        )
        qualification_source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        qualification = load_hexcal_stimulus_qualification(
            qualification_path,
            expected_board_id=board_id,
            expected_serial=serial,
            expected_uri=uri,
            expected_source_commit=source_commit,
            expected_source_attestation=qualification_source_attestation,
            expected_profile=profile,
            expected_firmware_evidence_sha256=firmware.file_sha256,
            expected_pluto_plus_utils_source_attestation_sha256=dependency_sha256,
            expected_protocol_id=contract.protocol_id,
            expected_qualification_kind=contract.qualification_kind,
            expected_center_frequencies_hz=contract.center_frequencies_hz,
            expected_receiver_gain_db=contract.fixed_receiver_gain_db,
        )
        if qualification.as_dict() != dict(qualification_summary):
            raise ValueError("v2 stimulus qualification differs on independent raw replay")
        stimulus_qualification_sha256 = qualification.file_sha256

    radio = _mapping(_mapping(metadata.get("global"), "global").get("pluto:radio"), "radio")
    if radio.get("serial") != serial or radio.get("uri") != uri:
        raise ValueError("SigMF radio identity differs from capture record")
    rx2 = load_ci16_channel(
        data_file,
        sample_count=TOTAL_SAMPLES,
        receiver_count=2,
        channel=1,
    )
    verified = {
        "capture_record_path": str(resolved_record),
        "capture_record_sha256": sha256_path(resolved_record),
        "artifact_id": artifact_id,
        "data_path": str(data_file),
        "data_sha256": sha256_path(data_file),
        "metadata_path": str(metadata_file),
        "metadata_sha256": sha256_path(metadata_file),
        "continuity_audit": continuity,
        "replayed_stream_id": continuity["stream_id"],
        "headroom_recomputed": recomputed_headroom,
        "source_commit": source_commit,
        "profile_file_sha256": profile.file_sha256,
        "profile_contract_sha256": profile.contract_sha256,
        "firmware_evidence_sha256": firmware.file_sha256,
        "target_uid_readback_sha256": firmware.target_uid_readback_sha256,
        "firmware_elf_sha256": firmware.firmware_elf_sha256,
        "firmware_bin_sha256": firmware.firmware_bin_sha256,
        "full_flash_readback_sha256": firmware.full_flash_readback_sha256,
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "rf_readback_evidence_sha256": str(capture.get("rf_readback_evidence_sha256")),
        "stimulus_qualification_sha256": stimulus_qualification_sha256,
    }
    return root, rx2, verified


def _verify_run_manifest(
    records: list[dict[str, Any]], verified_records: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(records) != 2 or len(verified_records) != 2:
        raise ValueError("run manifest verification requires exactly two artifacts")
    run_id = _string(records[0].get("run_id"), "run_id")
    record_paths = [Path(str(item["capture_record_path"])) for item in verified_records]
    capture_roots = {path.parent.parent.resolve() for path in record_paths}
    if len(capture_roots) != 1:
        raise ValueError("timing artifacts do not share one persisted run root")
    manifest_path = next(iter(capture_roots)) / "timing-runs" / f"{run_id}.json"
    try:
        manifest = _mapping(json.loads(manifest_path.read_bytes()), "timing run manifest")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load durable timing run manifest: {error}") from error
    plan = records[0].get("pair_plan_contract")
    plan_mapping = _mapping(plan, "pair plan")
    expected_run_kind = plan_mapping.get("plan_kind")
    if (
        manifest.get("schema") != 1
        or manifest.get("run_kind") != expected_run_kind
        or manifest.get("run_id") != run_id
        or manifest.get("status") != "two_independent_artifacts_verified_unanalyzed"
        or manifest.get("accepted") is not True
        or manifest.get("automatic_retry_count") != 0
    ):
        raise ValueError("durable timing run manifest is not a completed accepted pair")
    plan_sha = records[0].get("pair_plan_contract_sha256")
    if (
        manifest.get("pair_plan_contract") != plan
        or manifest.get("pair_plan_contract_sha256") != plan_sha
    ):
        raise ValueError("durable pre-RF pair plan differs from capture records")
    board_id = _string(plan_mapping.get("board_id"), "pair plan board_id")
    expected_lock = Path.home() / ".local/state/smateway/boards" / board_id / ".bench.lock"
    safety = _mapping(plan_mapping.get("safety"), "pair plan safety")
    if Path(_string(safety.get("bench_lock_path"), "bench_lock_path")) != expected_lock:
        raise ValueError("pair plan bench lock path is not board-bound")

    artifacts = manifest.get("artifacts")
    attempts = manifest.get("attempts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValueError("run manifest does not contain exactly two full artifacts")
    if not isinstance(attempts, list) or len(attempts) != 2:
        raise ValueError("run manifest does not contain exactly two attempt ledgers")
    expected_artifacts = {
        (
            str(item["artifact_id"]),
            str(item["capture_record_sha256"]),
            str(item["data_sha256"]),
        )
        for item in verified_records
    }
    manifest_artifacts = {
        (
            _string(item.get("artifact_id"), "manifest artifact_id"),
            _sha256(item.get("capture_record_sha256"), "manifest capture record SHA"),
            _sha256(item.get("data_sha256"), "manifest data SHA"),
        )
        for item in artifacts
        if isinstance(item, Mapping)
    }
    if manifest_artifacts != expected_artifacts:
        raise ValueError("run manifest artifact identities differ from input artifacts")
    for index, attempt in enumerate(attempts, start=1):
        attempt_mapping = _mapping(attempt, f"attempts[{index - 1}]")
        if (
            attempt_mapping.get("replicate_index") != index
            or attempt_mapping.get("status") != "complete_full_artifact"
            or attempt_mapping.get("artifact") != artifacts[index - 1]
            or attempt_mapping.get("error") is not None
        ):
            raise ValueError("run manifest attempt ledger is incomplete or reordered")
    serial = _string(_mapping(records[0].get("capture"), "capture").get("serial"), "serial")
    mute_attestations = manifest.get("mute_attestations")
    if not isinstance(mute_attestations, list) or len(mute_attestations) != 4:
        raise ValueError("run manifest lacks pre, per-replicate, and final mute attestations")
    purposes = ("pre_run", "post_replicate_1", "post_replicate_2", "final")
    for attestation, purpose in zip(mute_attestations, purposes, strict=True):
        if not _mute_attestation_passed(attestation, serial=serial, purpose=purpose):
            raise ValueError(f"run manifest exact-serial mute failed: {purpose}")
    return {
        "path": str(manifest_path.resolve()),
        "sha256": sha256_path(manifest_path),
        "pair_plan_contract_sha256": plan_sha,
        "pre_rf_plan_was_durable": True,
        "board_bench_lock_path": str(expected_lock),
        "exact_serial_mutes_verified": list(purposes),
    }


def _median(document: Mapping[str, Any], *path: str) -> float:
    current: object = document
    for component in path:
        current = _mapping(current, ".".join(path)).get(component)
    return _number(current, ".".join(path))


def _replicate_agreement(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    first, second = analyses
    comparisons: list[dict[str, Any]] = []

    def compare(name: str, left: float, right: float, limit: float) -> None:
        delta = abs(left - right)
        comparisons.append(
            {
                "metric": name,
                "replicate_1": left,
                "replicate_2": right,
                "absolute_delta_us": delta,
                "maximum_delta_us": limit,
                "passed": delta <= limit,
            }
        )

    compare(
        "combined_rf_marker_median",
        _median(first, "timing", "combined_rf_marker_us", "median"),
        _median(second, "timing", "combined_rf_marker_us", "median"),
        MAXIMUM_REPLICATE_SLOT_MEDIAN_DELTA_US,
    )
    for state in range(1, 7):
        name = f"ANT{state}"
        compare(
            f"{name}_dwell_median",
            _median(first, "timing", "dwells_us", name, "median"),
            _median(second, "timing", "dwells_us", name, "median"),
            MAXIMUM_REPLICATE_SLOT_MEDIAN_DELTA_US,
        )
    for state in range(1, 6):
        name = f"ANT{state}_TO_ANT{state + 1}"
        compare(
            f"{name}_guard_median",
            _median(first, "timing", "ordinary_guards_us", name, "q50_us", "median"),
            _median(second, "timing", "ordinary_guards_us", name, "q50_us", "median"),
            MAXIMUM_REPLICATE_SLOT_MEDIAN_DELTA_US,
        )
    compare(
        "cycle_median",
        _median(first, "timing", "cycle_us", "median"),
        _median(second, "timing", "cycle_us", "median"),
        MAXIMUM_REPLICATE_CYCLE_MEDIAN_DELTA_US,
    )
    failed = [str(item["metric"]) for item in comparisons if item["passed"] is not True]
    return {
        "passed": not failed,
        "failed_metrics": failed,
        "frozen_gates": {
            "maximum_marker_dwell_guard_median_delta_us": (MAXIMUM_REPLICATE_SLOT_MEDIAN_DELTA_US),
            "maximum_cycle_median_delta_us": (MAXIMUM_REPLICATE_CYCLE_MEDIAN_DELTA_US),
        },
        "comparisons": comparisons,
    }


def main() -> int:
    args = _parser().parse_args()
    if len(args.capture_record) != 2:
        raise SystemExit("--capture-record must be supplied exactly twice")
    repository = Path(__file__).resolve().parents[1]
    try:
        profile = load_hexcal_profile(args.profile)
        records: list[dict[str, Any]] = []
        verified_records: list[dict[str, Any]] = []
        analyses: list[dict[str, Any]] = []
        for record_path in args.capture_record:
            record, rx2, verified = _load_and_verify_record(
                record_path,
                repository=repository,
                profile=profile,
            )
            capture = _mapping(record.get("capture"), "capture")
            analysis = analyze_hexcal_timing_samples(
                rx2,
                sample_rate_hz=float(SAMPLE_RATE_HZ),
                dds_readback_hz=_number(
                    capture.get("tone_offset_readback_hz"),
                    "capture.tone_offset_readback_hz",
                ),
                profile=profile,
                continuity_verified=True,
            )
            records.append(record)
            verified_records.append(verified)
            analyses.append(analysis)
        run_manifest_evidence = _verify_run_manifest(records, verified_records)
        captured_source = _mapping(records[0].get("source"), "captured source")
        captured_commit = _string(captured_source.get("commit"), "captured source commit")
        analysis_source = attest_source_files_at_commit(
            repository,
            expected_commit=captured_commit,
            relative_paths=ANALYSIS_SOURCE_FILES,
        )
        captured_files_raw = captured_source.get("files")
        if not isinstance(captured_files_raw, list):
            raise ValueError("captured source file evidence is malformed")
        captured_files = {
            _string(_mapping(item, "captured source file").get("path"), "source path"): _sha256(
                _mapping(item, "captured source file").get("sha256"), "source SHA"
            )
            for item in captured_files_raw
        }
        for item in analysis_source["files"]:
            if captured_files.get(str(item["path"])) != item["sha256"]:
                raise ValueError("current analyzer bytes differ from captured source evidence")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    run_ids = {_string(record.get("run_id"), "run_id") for record in records}
    replicate_indices = {
        _integer(record.get("replicate_index"), "replicate_index") for record in records
    }
    artifacts = {
        _string(_mapping(record.get("artifact"), "artifact").get("artifact_id"), "artifact_id")
        for record in records
    }
    stream_ids = {
        _integer(item.get("replayed_stream_id"), "replayed_stream_id") for item in verified_records
    }
    data_hashes = {str(item["data_sha256"]) for item in verified_records}
    common_fields = (
        "board_id",
        "serial",
        "uri",
        "center_frequency_hz",
        "sample_rate_hz",
        "bandwidth_hz",
        "receiver_gain_db",
        "tx_hardware_gain_db_requested",
        "dds_scale_requested",
    )
    first_capture = _mapping(records[0].get("capture"), "capture")
    second_capture = _mapping(records[1].get("capture"), "capture")
    reasons: list[str] = []
    if len(run_ids) != 1:
        reasons.append("capture_records_have_different_run_ids")
    if replicate_indices != {1, 2}:
        reasons.append("capture_records_are_not_replicates_one_and_two")
    if len(artifacts) != 2:
        reasons.append("artifact_ids_are_not_independent")
    if len(stream_ids) != 2:
        reasons.append("stream_generations_are_not_independent")
    if len(data_hashes) != 2:
        reasons.append("artifact_data_hashes_are_not_independent")
    if any(first_capture.get(field) != second_capture.get(field) for field in common_fields):
        reasons.append("replicate_capture_settings_or_target_differ")
    if records[0].get("source") != records[1].get("source"):
        reasons.append("replicate_source_evidence_differs")
    if records[0].get("pluto_plus_utils_source_attestation") != records[1].get(
        "pluto_plus_utils_source_attestation"
    ):
        reasons.append("replicate_pluto_plus_utils_source_evidence_differs")
    if records[0].get("source_profile") != records[1].get("source_profile"):
        reasons.append("replicate_profile_evidence_differs")
    if records[0].get("firmware_evidence") != records[1].get("firmware_evidence"):
        reasons.append("replicate_firmware_evidence_differs")
    for index, analysis in enumerate(analyses, start=1):
        quality = _mapping(analysis.get("quality"), f"analysis[{index}].quality")
        if quality.get("passed") is not True:
            reasons.append(f"replicate_{index}_timing_quality_failed")
    agreement = _replicate_agreement(analyses)
    if agreement["passed"] is not True:
        reasons.append("replicate_timing_medians_disagree")

    output = {
        "schema": 1,
        "analysis_kind": "hexcal_v1_rf_timing_two_replicate_qualification",
        "run_id": next(iter(run_ids)) if len(run_ids) == 1 else None,
        "passed": not reasons,
        "rejection_reasons": reasons,
        "analysis_source": analysis_source,
        "pluto_plus_utils_source_attestation": records[0].get(
            "pluto_plus_utils_source_attestation"
        ),
        "pluto_plus_utils_source_attestation_sha256": records[0].get(
            "pluto_plus_utils_source_attestation_sha256"
        ),
        "source_profile": profile.as_dict(),
        "durable_run_manifest_evidence": run_manifest_evidence,
        "verified_capture_evidence": verified_records,
        "replicate_analyses": analyses,
        "replicate_agreement": agreement,
        "limitations": [
            "RF sees the combined approximately 200 us marker, not the 180 us body and "
            "contiguous pre-ANT1 guard separately.",
            "Slot identity and position are source-backed; this is not independent GPIO or "
            "physical-port proof.",
            "Timing is measured relative to the Pluto sample clock and is not an independent "
            "calibrated timebase.",
        ],
    }
    write_json_atomic(args.output.expanduser().resolve(), output)
    print(
        json.dumps(
            {
                "output": str(args.output.expanduser().resolve()),
                "output_sha256": sha256_path(args.output.expanduser().resolve()),
                "passed": output["passed"],
                "rejection_reasons": reasons,
                "complete_cycles": [
                    analysis["decode"]["complete_cycle_count"] for analysis in analyses
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if output["passed"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
