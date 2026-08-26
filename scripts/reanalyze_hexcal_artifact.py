#!/usr/bin/env python3
"""Offline-only coherent reanalysis of one immutable ``hexcal-v1`` artifact."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping
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

from pluto_plus.artifacts import data_path, load_metadata, verify_artifact
from pluto_plus.models import ArtifactSummary

from smateway.hexcal import (
    HEXCAL_ANALYSIS_SOURCE_FILES,
    analyze_hexcal_samples,
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    canonical_json_sha256,
    evaluate_hexcal_quality,
    load_ci16_channel,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
CAPTURE_FILENAME = "hexcal-capture.json"
ANALYSIS_FILENAME = "hexcal-analysis.json"
EXPECTED_SAMPLE_RATE_HZ = 1_000_000
EXPECTED_SAMPLES_PER_FRAME = 100_000
EXPECTED_SAMPLE_COUNT = 1_000_000
TONE_OFFSET_HZ = 100_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_id")
    parser.add_argument("--serial", required=True, help="expected immutable capture serial")
    parser.add_argument("--uri", required=True, help="expected immutable capture USB URI")
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _git_commit(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _canonical_dds_readback(capture: Mapping[str, Any]) -> tuple[float, ...]:
    if capture.get("tx_channel") != 0 or capture.get("tx_port") != "TX1":
        raise ValueError("hexcal calibration permits TX1 only")
    readback = capture.get("dds_frequency_readback_hz")
    if not isinstance(readback, list) or len(readback) != 8:
        raise ValueError("DDS frequency read-back is not canonical 2T2R")
    values = tuple(float(value) for value in readback)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("DDS frequency read-back contains non-finite values")
    return values


def _configured_tone_offset(capture: Mapping[str, Any], *, sample_rate_hz: float) -> float:
    readback = _canonical_dds_readback(capture)
    frequencies = [abs(readback[index]) for index in (0, 2)]
    if not all(math.isfinite(value) and value > 0.0 for value in frequencies):
        raise ValueError("active TX1 DDS frequency read-backs are invalid")
    frequency_tolerance_hz = math.ceil(sample_rate_hz / (1 << 16))
    if abs(frequencies[0] - frequencies[1]) > frequency_tolerance_hz:
        raise ValueError("active TX1 I/Q DDS frequency read-backs disagree")
    return sum(frequencies) / 2.0


def _headroom_passed(capture: Mapping[str, Any]) -> bool:
    admission = capture.get("adc_headroom_admission")
    if not isinstance(admission, Mapping) or admission.get("passed") is not True:
        return False
    receivers = admission.get("receivers")
    if not isinstance(receivers, list) or len(receivers) != 2:
        return False
    observed: set[int] = set()
    for raw in receivers:
        if not isinstance(raw, Mapping) or raw.get("passed") is not True:
            return False
        receiver = raw.get("receiver")
        if receiver not in (0, 1) or receiver in observed:
            return False
        if raw.get("sample_count") != EXPECTED_SAMPLE_COUNT:
            return False
        if raw.get("clipped_sample_count") != 0:
            return False
        near = raw.get("near_full_scale_fraction")
        maximum = raw.get("maximum_near_full_scale_fraction")
        if not isinstance(near, (int, float)) or not isinstance(maximum, (int, float)):
            return False
        if float(near) > float(maximum):
            return False
        observed.add(int(receiver))
    return observed == {0, 1}


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    artifact_root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "pluto-usb-captures"
        / args.artifact_id
    )
    capture_path = artifact_root / CAPTURE_FILENAME
    try:
        capture_document = json.loads(capture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load immutable capture record: {error}") from error
    if not isinstance(capture_document, dict) or capture_document.get("schema") != 1:
        raise SystemExit("capture record schema is unsupported")
    artifact = ArtifactSummary.model_validate(capture_document.get("artifact"))
    if artifact.artifact_id != args.artifact_id or Path(artifact.path) != artifact_root:
        raise SystemExit("capture record artifact identity differs from requested artifact")
    if artifact.sample_count != EXPECTED_SAMPLE_COUNT or artifact.receiver_count != 2:
        raise SystemExit("hexcal artifact must contain exact 1.0 s dual-RX CI16")
    if artifact.sample_rate_hz != EXPECTED_SAMPLE_RATE_HZ:
        raise SystemExit("hexcal artifact sample rate must be exactly 1 MS/s")
    if not verify_artifact(artifact):
        raise SystemExit("immutable artifact data SHA-256 verification failed")

    evidence = _mapping(capture_document.get("artifact_evidence"), "artifact_evidence")
    sigmf_data = data_path(artifact)
    sigmf_meta = artifact_root / f"{artifact.artifact_id}.sigmf-meta"
    actual_data_sha256 = sha256_path(sigmf_data)
    actual_meta_sha256 = sha256_path(sigmf_meta)
    if evidence.get("data_sha256") != actual_data_sha256 or artifact.sha256 != actual_data_sha256:
        raise SystemExit("capture record data SHA-256 differs from finalized data")
    if evidence.get("metadata_sha256") != actual_meta_sha256:
        raise SystemExit("capture record metadata SHA-256 differs from finalized metadata")
    if evidence.get("metadata_size_bytes") != sigmf_meta.stat().st_size:
        raise SystemExit("capture record metadata size differs from finalized metadata")

    profile = load_hexcal_profile(args.profile)
    source_profile = _mapping(capture_document.get("source_profile"), "source_profile")
    if (
        source_profile.get("file_sha256") != profile.file_sha256
        or source_profile.get("contract_sha256") != profile.contract_sha256
    ):
        raise SystemExit("analysis profile bytes differ from the source capture profile")
    capture = _mapping(capture_document.get("capture"), "capture")
    capture_source_commit = capture_document.get("source_commit")
    if not isinstance(capture_source_commit, str) or len(capture_source_commit) != 40:
        raise SystemExit("capture implementation source commit is malformed")
    try:
        source_attestation = attest_source_files_at_commit(
            repository,
            expected_commit=capture_source_commit,
            relative_paths=HEXCAL_ANALYSIS_SOURCE_FILES,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"analysis source attestation failed: {error}") from error
    recorded_dependency = _mapping(
        capture_document.get("pluto_plus_utils_source_attestation"),
        "pluto_plus_utils_source_attestation",
    )
    try:
        dependency_attestation = attest_pluto_plus_utils_source()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"pluto-plus-utils source attestation failed: {error}") from error
    dependency_sha256 = canonical_json_sha256(dependency_attestation)
    if (
        dict(recorded_dependency) != dependency_attestation
        or capture_document.get("pluto_plus_utils_source_attestation_sha256") != dependency_sha256
    ):
        raise SystemExit("capture pluto-plus-utils source attestation differs on replay")
    firmware_evidence = _mapping(capture_document.get("firmware_evidence"), "firmware_evidence")
    if capture.get("serial") != args.serial or capture.get("uri") != args.uri:
        raise SystemExit("explicit serial/URI differ from the immutable capture identity")
    if (
        capture.get("sample_rate_hz") != EXPECTED_SAMPLE_RATE_HZ
        or capture.get("samples_per_frame") != EXPECTED_SAMPLES_PER_FRAME
        or capture.get("sample_count") != EXPECTED_SAMPLE_COUNT
        or capture.get("frame_count") != 10
        or capture.get("kernel_buffers") != 8
    ):
        raise SystemExit("capture shape differs from the reviewed exact-stream plan")
    rf_readback = _mapping(capture.get("rf_readback_evidence"), "capture.rf_readback_evidence")
    rf_readback_sha256 = canonical_json_sha256(rf_readback)
    try:
        normalized_rf_readback = validate_tx1_rf_readback_evidence(
            rf_readback,
            planned_kernel_buffers=8,
            planned_tx_gain_db=float(capture["tx_hardware_gain_db_requested"]),
            planned_dds_scale=float(capture["dds_scale_requested"]),
            planned_tone_hz=TONE_OFFSET_HZ,
            sample_rate_hz=EXPECTED_SAMPLE_RATE_HZ,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"capture RF readback evidence failed validation: {error}") from error
    if (
        capture.get("rf_readback_evidence_sha256") != rf_readback_sha256
        or capture.get("kernel_buffers") != normalized_rf_readback["kernel_buffers"]
        or capture.get("tx_gain_readback_db") != normalized_rf_readback["tx1_gain_readback_db"]
        or capture.get("dds_scale_readback") != normalized_rf_readback["dds_scale_readback"]
        or capture.get("dds_enabled_readback") != normalized_rf_readback["dds_enabled_readback"]
        or capture.get("dds_frequency_readback_hz")
        != normalized_rf_readback["dds_frequency_readback_hz"]
    ):
        raise SystemExit("capture RF readback fields differ from their hash-bound evidence")
    metadata = load_metadata(artifact)
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=EXPECTED_SAMPLE_COUNT,
        expected_samples_per_block=EXPECTED_SAMPLES_PER_FRAME,
    )
    if continuity.get("stream_id") != capture.get("stream_id"):
        raise SystemExit("capture record stream ID differs from SigMF continuity evidence")
    tone_offset_hz = _configured_tone_offset(capture, sample_rate_hz=EXPECTED_SAMPLE_RATE_HZ)
    dds_frequency_readback_hz = _canonical_dds_readback(capture)
    emitted_carrier_frequency_hz = float(capture["center_frequency_hz"]) + tone_offset_hz
    rx2 = load_ci16_channel(
        sigmf_data,
        sample_count=EXPECTED_SAMPLE_COUNT,
        receiver_count=2,
        channel=1,
    )
    analysis = analyze_hexcal_samples(
        rx2,
        sample_rate_hz=EXPECTED_SAMPLE_RATE_HZ,
        tone_offset_hz=tone_offset_hz,
        profile=profile,
        continuity_verified=True,
    )
    quality = evaluate_hexcal_quality(
        analysis,
        headroom_passed=_headroom_passed(capture),
    )
    document = {
        "schema": 1,
        "analysis_kind": "hexcal_v1_offline_calibration",
        "source_commit": capture_source_commit,
        "analysis_runtime_head": _git_commit(repository),
        "analysis_source_attestation": source_attestation,
        "pluto_plus_utils_source_attestation": dependency_attestation,
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "artifact": artifact.model_dump(mode="json"),
        "artifact_evidence": {
            "data_sha256": actual_data_sha256,
            "data_size_bytes": sigmf_data.stat().st_size,
            "metadata_sha256": actual_meta_sha256,
            "metadata_size_bytes": sigmf_meta.stat().st_size,
            "capture_record_sha256": sha256_path(capture_path),
            "capture_record_size_bytes": capture_path.stat().st_size,
            "stream_id": continuity["stream_id"],
            "metadata_abi": continuity["metadata_abi"],
        },
        "aggregation_key": {
            "artifact_id": artifact.artifact_id,
            "serial": args.serial,
            "uri": args.uri,
            "tx_channel": 0,
            "tx_port": "TX1",
            "center_frequency_hz": int(capture["center_frequency_hz"]),
            "sample_rate_hz": EXPECTED_SAMPLE_RATE_HZ,
            "receiver_gain_db": int(capture["receiver_gain_db"]),
            "profile_file_sha256": profile.file_sha256,
            "profile_contract_sha256": profile.contract_sha256,
            "implementation_source_commit": capture_source_commit,
            "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
            "firmware_evidence_sha256": firmware_evidence["file_sha256"],
            "firmware_bin_sha256": firmware_evidence["firmware_bin_sha256"],
            "full_flash_readback_sha256": firmware_evidence["full_flash_readback_sha256"],
            "rf_readback_evidence_sha256": rf_readback_sha256,
            "dds_frequency_readback_hz": list(dds_frequency_readback_hz),
            "dds_tone_offset_hz": tone_offset_hz,
            "emitted_carrier_frequency_hz": emitted_carrier_frequency_hz,
        },
        "continuity_audit": continuity,
        "quality_gate": quality,
        "hexcal": analysis,
        "immutable_reanalysis": (
            "Only finalized SigMF data/metadata and the capture record were read; "
            "no radio, selector, or firmware operation occurs in this program."
        ),
    }
    output_path = artifact_root / ANALYSIS_FILENAME
    write_json_atomic(output_path, document)
    output_sha256 = sha256_path(output_path)
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "analysis": str(output_path),
                "analysis_sha256": output_sha256,
                "analysis_size_bytes": output_path.stat().st_size,
                "quality_passed": quality["passed"],
                "valid_cycle_count": analysis["valid_cycle_count"],
                "stream_id": continuity["stream_id"],
            },
            sort_keys=True,
        )
    )
    return 0 if quality["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
