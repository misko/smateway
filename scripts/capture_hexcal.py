#!/usr/bin/env python3
"""Capture one exact, continuous 1.0 s TX1 ``hexcal-v1`` calibration stream."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import json
import os
import subprocess
import sys
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

from pluto_plus.artifacts import CaptureWriter, data_path, load_metadata, verify_artifact
from pluto_plus.hardware import SafeDdsTonePlan, SampleBlockV2, capture_continuous_safe_dds_tone
from pluto_plus.hardware.iio import find_usb_sysfs_path
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.rf_policy import classify_fast20_center_frequency

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
DEFAULT_PROFILE = Path("profiles/hexcal-v1/control_profile.json")
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 10
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
TX_CHANNEL = 0
CAPTURE_FILENAME = "hexcal-capture.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="exact usb: IIO URI")
    parser.add_argument("--center-frequency-hz", type=int, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pluto-plus-utils-attestation-sha256", required=True)
    parser.add_argument("--firmware-evidence", type=Path, required=True)
    parser.add_argument("--firmware-evidence-sha256", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--allow-experimental-5g8", action="store_true")
    parser.add_argument(
        "--receiver-gain-db",
        type=int,
        choices=range(63),
        default=0,
        help="common RX1/RX2 tandem-HOLD gain (conservative default: 0 dB)",
    )
    parser.add_argument(
        "--tx-hardware-gain-db",
        type=float,
        default=-40.0,
        help="conservative TX1 hardware gain in -80..0 dB",
    )
    parser.add_argument(
        "--dds-scale",
        type=float,
        default=0.125,
        help="bounded TX1 DDS scale in (0,1]",
    )
    return parser


def _git_commit(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _require_clean_source(repository: Path, expected_commit: str) -> None:
    if _git_commit(repository) != expected_commit:
        raise RuntimeError("implementation HEAD differs from the persisted source commit")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("hexcal capture refuses a dirty implementation source tree")


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


def _metadata_path(artifact_path: Path, artifact_id: str) -> Path:
    return artifact_path / f"{artifact_id}.sigmf-meta"


def _rf_readback_evidence(capture: Any, args: argparse.Namespace) -> dict[str, Any]:
    evidence = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": TX_CHANNEL,
        "tx_port": "TX1",
        "kernel_buffers": capture.kernel_buffers,
        "tx_hardware_gain_db_requested": args.tx_hardware_gain_db,
        "tx_hardware_gain_readback_db_by_channel": [
            capture.tx_gain_readback_db,
            -80.0,
        ],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_scale_requested": args.dds_scale,
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
        planned_tx_gain_db=args.tx_hardware_gain_db,
        planned_dds_scale=args.dds_scale,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    return evidence


def main() -> int:
    args = _parser().parse_args()
    if not args.serial.strip():
        raise SystemExit("--serial must be non-empty")
    if not args.uri.removeprefix("pluto://").startswith("usb:"):
        raise SystemExit("--uri must be an exact USB IIO URI")
    try:
        policy = classify_fast20_center_frequency(
            args.center_frequency_hz,
            allow_experimental_5g8=args.allow_experimental_5g8,
        )
        profile = load_hexcal_profile(args.profile)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    repository = Path(__file__).resolve().parents[1]
    _require_clean_source(repository, args.source_commit)
    dependency_attestation = attest_pluto_plus_utils_source()
    dependency_attestation_sha256 = canonical_json_sha256(dependency_attestation)
    if dependency_attestation_sha256 != args.pluto_plus_utils_attestation_sha256:
        raise SystemExit("pluto-plus-utils source attestation differs from the persisted plan")
    firmware_evidence = load_hexcal_firmware_evidence(
        args.firmware_evidence,
        expected_board_id=args.board_id,
        expected_source_commit=args.source_commit,
        expected_profile=profile,
    )
    if firmware_evidence.file_sha256 != args.firmware_evidence_sha256:
        raise SystemExit("firmware evidence SHA-256 differs from the persisted plan")
    settings = RadioSettings(
        center_frequency_hz=args.center_frequency_hz,
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=args.receiver_gain_db,
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
        tx_hardware_gain_db=args.tx_hardware_gain_db,
        dds_scale=args.dds_scale,
        receiver_gain_db=float(args.receiver_gain_db),
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    capture_root = (
        Path.home() / ".local/state/smateway/boards" / args.board_id / "pluto-usb-captures"
    )
    writer = CaptureWriter(
        capture_root,
        radio=_radio_identity(args.uri, args.serial),
        settings=settings,
        label=f"hexcal-v1 TX1 {args.center_frequency_hz}Hz exact 1.0s",
    )
    headroom = AdcHeadroomMonitor(receiver_count=2)

    def persist(block: SampleBlockV2) -> None:
        headroom.observe(block.samples)
        writer.append(block, settings, revision=1)

    try:
        capture = capture_continuous_safe_dds_tone(
            plan,
            samples_per_frame=SAMPLES_PER_FRAME,
            frame_count=FRAME_COUNT,
            kernel_buffers=KERNEL_BUFFERS,
            block_consumer=persist,
        )
        if capture.settings != settings:
            raise RuntimeError("capture settings read-back differs from the exact plan")
        if capture.identity.serial != args.serial or capture.identity.uri != args.uri:
            raise RuntimeError("capture identity read-back differs from explicit USB target")
        if capture.sample_count != TOTAL_SAMPLES or len(capture.frames) != FRAME_COUNT:
            raise RuntimeError("capture did not return exactly ten 100k-sample frames")
        rf_readback_evidence = _rf_readback_evidence(capture, args)
        headroom_result = headroom.result()
        artifact = writer.finalize()
    except BaseException as error:
        writer.fail(error)
        raise

    if not verify_artifact(artifact):
        raise RuntimeError("finalized SigMF data failed its SHA-256 verification")
    artifact_root = Path(artifact.path)
    sigmf_data = data_path(artifact)
    sigmf_meta = _metadata_path(artifact_root, artifact.artifact_id)
    metadata = load_metadata(artifact)
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    metadata_sha256 = sha256_path(sigmf_meta)
    document: dict[str, Any] = {
        "schema": 1,
        "capture_kind": "hexcal_v1_exact_usb_tx1",
        "source_commit": args.source_commit,
        "pluto_plus_utils_source_attestation": dependency_attestation,
        "pluto_plus_utils_source_attestation_sha256": (dependency_attestation_sha256),
        "artifact": artifact.model_dump(mode="json"),
        "artifact_evidence": {
            "data_path": str(sigmf_data),
            "data_sha256": sha256_path(sigmf_data),
            "data_size_bytes": sigmf_data.stat().st_size,
            "metadata_path": str(sigmf_meta),
            "metadata_sha256": metadata_sha256,
            "metadata_size_bytes": sigmf_meta.stat().st_size,
        },
        "source_profile": profile.as_dict(),
        "firmware_evidence": firmware_evidence.as_dict(),
        "capture": {
            "board_id": args.board_id,
            "serial": args.serial,
            "uri": args.uri,
            "tx_channel": TX_CHANNEL,
            "tx_port": "TX1",
            "center_frequency_hz": args.center_frequency_hz,
            "center_frequency_policy": policy,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "sample_count": capture.sample_count,
            "duration_s": capture.duration_s,
            "kernel_buffers": capture.kernel_buffers,
            "receiver_gain_db": args.receiver_gain_db,
            "tx_hardware_gain_db_requested": args.tx_hardware_gain_db,
            "tx_gain_readback_db": capture.tx_gain_readback_db,
            "dds_scale_requested": args.dds_scale,
            "dds_scale_readback": list(capture.dds_scale_readback),
            "dds_enabled_readback": list(capture.dds_enabled_readback),
            "dds_frequency_readback_hz": list(capture.dds_frequency_readback_hz),
            "rf_readback_evidence": rf_readback_evidence,
            "rf_readback_evidence_sha256": canonical_json_sha256(rf_readback_evidence),
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "worst_case_load_input_dbm": plan.worst_case_load_input_dbm,
            "metadata_abi": capture.frames[0].metadata_abi,
            "stream_id": capture.frames[0].stream_id,
            "first_buffer_sequence": capture.frames[0].buffer_sequence,
            "last_buffer_sequence": capture.frames[-1].buffer_sequence,
            "first_sample_sequence": capture.frames[0].first_sample_sequence,
            "last_sample_sequence_exclusive": capture.frames[-1].last_sample_sequence_exclusive,
            "adc_headroom_admission": asdict(headroom_result),
        },
        "continuity_audit": continuity,
        "analysis_status": "artifact_verified_unanalyzed",
    }
    capture_path = artifact_root / CAPTURE_FILENAME
    write_json_atomic(capture_path, document)
    print(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "artifact_path": str(artifact_root),
                "capture_record": str(capture_path),
                "capture_record_sha256": sha256_path(capture_path),
                "data_sha256": artifact.sha256,
                "metadata_sha256": metadata_sha256,
                "metadata_size_bytes": sigmf_meta.stat().st_size,
                "stream_id": capture.frames[0].stream_id,
                "headroom_passed": headroom_result.passed,
                "source_commit": args.source_commit,
                "pluto_plus_utils_source_attestation_sha256": (dependency_attestation_sha256),
                "rf_readback_evidence_sha256": canonical_json_sha256(rf_readback_evidence),
                "firmware_evidence_sha256": firmware_evidence.file_sha256,
                "firmware_bin_sha256": firmware_evidence.firmware_bin_sha256,
                "full_flash_readback_sha256": (firmware_evidence.full_flash_readback_sha256),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
