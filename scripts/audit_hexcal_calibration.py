#!/usr/bin/env python3
"""Independently audit a completed hexcal run and every accepted artifact."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import importlib.util
import json
import math
import os
import re
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

import numpy as np

from smateway.capture_admission import (
    ADC_CLIP_THRESHOLD_ABS,
    ADC_NEAR_FULL_SCALE_THRESHOLD_ABS,
    MAXIMUM_NEAR_FULL_SCALE_SAMPLE_FRACTION,
)
from smateway.hexcal import (
    HEXCAL_AGGREGATION_SOURCE_FILES,
    HEXCAL_ANALYSIS_SOURCE_FILES,
    analyze_hexcal_samples,
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    audit_continuity_metadata,
    canonical_json_sha256,
    evaluate_hexcal_quality,
    load_ci16_channel,
    load_hexcal_firmware_evidence,
    load_hexcal_profile,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.hexcal_gain import (
    QUALIFICATION_SOURCE_FILES,
    STIMULUS_PROTOCOL_ID,
    load_hexcal_gain_qualification,
    load_hexcal_stimulus_qualification,
)

DEFAULT_BOARD_ID = "stm32c011-4c0055000950313950363920"
CAPTURE_FILENAME = "hexcal-capture.json"
ANALYSIS_FILENAME = "hexcal-analysis.json"
CALIBRATION_FILENAME = "hexcal-calibration.json"
AUDIT_FILENAME = "hexcal-audit.json"
EXPECTED_SAMPLE_COUNT = 1_000_000
EXPECTED_SAMPLES_PER_BLOCK = 100_000
EXPECTED_DATA_SIZE = EXPECTED_SAMPLE_COUNT * 2 * 2 * 2
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
AUDIT_SOURCE_FILES = tuple(
    dict.fromkeys(
        (
            "scripts/audit_hexcal_calibration.py",
            *HEXCAL_AGGREGATION_SOURCE_FILES,
            *HEXCAL_ANALYSIS_SOURCE_FILES,
            *QUALIFICATION_SOURCE_FILES,
        )
    )
)
SCIENTIFIC_CALIBRATION_FIELDS = (
    "frequency_results",
    "leave_one_frequency_out_2g4",
    "missing_passing_frequencies_hz",
    "quality_gate",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _load_aggregation_module() -> Any:
    path = Path(__file__).resolve().with_name("aggregate_hexcal_calibration.py")
    spec = importlib.util.spec_from_file_location("hexcal_aggregation_for_audit", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the attested aggregation implementation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "build_calibration_scientific_payload", None)):
        raise ValueError("aggregation implementation lacks deterministic payload builder")
    return module


def _headroom_from_data(data_file: Path) -> dict[str, Any]:
    raw = np.memmap(data_file, dtype="<i2", mode="r")
    if raw.size != EXPECTED_SAMPLE_COUNT * 4:
        raise ValueError("raw CI16 component count differs from exact dual-RX capture")
    components = raw.reshape(EXPECTED_SAMPLE_COUNT, 2, 2)
    receivers = []
    for receiver in (0, 1):
        real = np.abs(components[:, receiver, 0].astype(np.int32))
        imag = np.abs(components[:, receiver, 1].astype(np.int32))
        clipped = int(
            np.count_nonzero((real >= ADC_CLIP_THRESHOLD_ABS) | (imag >= ADC_CLIP_THRESHOLD_ABS))
        )
        near = int(
            np.count_nonzero(
                (real >= ADC_NEAR_FULL_SCALE_THRESHOLD_ABS)
                | (imag >= ADC_NEAR_FULL_SCALE_THRESHOLD_ABS)
            )
        )
        near_fraction = near / EXPECTED_SAMPLE_COUNT
        receivers.append(
            {
                "receiver": receiver,
                "sample_count": EXPECTED_SAMPLE_COUNT,
                "peak_abs_component_counts": int(max(np.max(real), np.max(imag))),
                "clipped_sample_count": clipped,
                "near_full_scale_sample_count": near,
                "near_full_scale_fraction": near_fraction,
                "passed": clipped == 0 and near_fraction <= MAXIMUM_NEAR_FULL_SCALE_SAMPLE_FRACTION,
            }
        )
    return {"passed": all(item["passed"] for item in receivers), "receivers": receivers}


def _compare_headroom(observed: Mapping[str, Any], recorded: Mapping[str, Any]) -> list[str]:
    issues = []
    if observed.get("passed") is not recorded.get("passed"):
        issues.append("headroom overall decision differs from raw CI16")
    raw_recorded = recorded.get("receivers")
    if not isinstance(raw_recorded, list) or len(raw_recorded) != 2:
        return issues + ["recorded headroom receivers are malformed"]
    for index, expected in enumerate(raw_recorded):
        if not isinstance(expected, Mapping):
            issues.append(f"recorded RX{index + 1} headroom is malformed")
            continue
        actual = observed["receivers"][index]
        for key in (
            "receiver",
            "sample_count",
            "peak_abs_component_counts",
            "clipped_sample_count",
            "near_full_scale_sample_count",
            "passed",
        ):
            if float(actual[key]) != float(expected.get(key, float("nan"))):
                issues.append(f"recorded RX{index + 1} {key} differs from raw CI16")
        expected_fraction = expected.get("near_full_scale_fraction")
        if not isinstance(expected_fraction, (int, float)) or not math.isclose(
            float(expected_fraction), float(actual["near_full_scale_fraction"]), abs_tol=1e-15
        ):
            issues.append(f"recorded RX{index + 1} near-full-scale fraction differs")
    return issues


def _passed_exact_mute(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("purpose") == purpose
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _audit_quarantined_failure(
    value: object, *, accepted_artifact_ids: set[str]
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        evidence = _mapping(value, "quarantined failure")
    except ValueError as error:
        return {"passed": False, "error": str(error)}, [str(error)]
    artifact_id = evidence.get("artifact_id")
    root = Path(str(evidence.get("path")))
    if not isinstance(artifact_id, str) or ARTIFACT_ID.fullmatch(artifact_id) is None:
        issues.append("quarantined failure artifact ID is malformed")
    elif artifact_id in accepted_artifact_ids:
        issues.append("quarantined failure is also accepted")
    if root.name != artifact_id or root.parent.name != ".failed":
        issues.append("quarantined failure path does not bind its .failed artifact ID")
    if evidence.get("accepted") is not False:
        issues.append("quarantined failure is not explicitly marked unaccepted")
    raw_files = evidence.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        issues.append("quarantined failure has no hashed file evidence")
        raw_files = []
    observed_files: list[dict[str, Any]] = []
    for index, raw_file in enumerate(raw_files):
        try:
            file_evidence = _mapping(raw_file, f"quarantined failure files[{index}]")
        except ValueError as error:
            issues.append(str(error))
            continue
        path = Path(str(file_evidence.get("path")))
        if path.parent != root or path.name != file_evidence.get("name"):
            issues.append("quarantined failure file path differs from its bound root/name")
            continue
        if not path.is_file():
            issues.append(f"quarantined failure file disappeared: {path.name}")
            continue
        actual = {
            "name": path.name,
            "path": str(path),
            "sha256": sha256_path(path),
            "size_bytes": path.stat().st_size,
        }
        observed_files.append(actual)
        if dict(file_evidence) != actual:
            issues.append(f"quarantined failure file evidence changed: {path.name}")
    failure_path = root / "failure.json"
    failure_record = evidence.get("failure_record")
    if not isinstance(failure_record, Mapping):
        issues.append("quarantined failure lacks its parsed failure record")
    elif not failure_path.is_file():
        issues.append("quarantined failure lacks failure.json")
    else:
        try:
            actual_failure = json.loads(failure_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            issues.append(f"cannot re-read quarantined failure.json: {error}")
        else:
            if actual_failure != dict(failure_record):
                issues.append("quarantined failure record changed")
    return {
        "artifact_id": artifact_id,
        "path": str(root),
        "passed": not issues,
        "issues": issues,
        "files": observed_files,
    }, issues


def _audit_artifact(
    attempt: Mapping[str, Any],
    *,
    board_id: str,
    serial: str,
    uri: str,
    profile_sha256: str,
    profile: Any,
    source_commit: str,
    expected_firmware_evidence: Mapping[str, Any],
    expected_dependency_attestation: Mapping[str, Any],
    expected_dependency_sha256: str,
) -> dict[str, Any]:
    issues: list[str] = []
    identity = _mapping(attempt.get("artifact_identity"), "artifact identity")
    artifact_id = str(identity.get("artifact_id"))
    artifact_root = Path(str(identity.get("path")))
    data_file = artifact_root / f"{artifact_id}.sigmf-data"
    meta_file = artifact_root / f"{artifact_id}.sigmf-meta"
    capture_file = artifact_root / CAPTURE_FILENAME
    analysis_file = artifact_root / ANALYSIS_FILENAME
    files = (data_file, meta_file, capture_file, analysis_file)
    for path in files:
        if not path.is_file():
            issues.append(f"missing finalized file: {path.name}")
    if issues:
        return {"artifact_id": artifact_id, "passed": False, "issues": issues}
    hashes = {
        "data_sha256": sha256_path(data_file),
        "metadata_sha256": sha256_path(meta_file),
        "capture_record_sha256": sha256_path(capture_file),
        "analysis_sha256": sha256_path(analysis_file),
    }
    sizes = {
        "data_size_bytes": data_file.stat().st_size,
        "metadata_size_bytes": meta_file.stat().st_size,
        "capture_record_size_bytes": capture_file.stat().st_size,
        "analysis_size_bytes": analysis_file.stat().st_size,
    }
    for key, value in {**hashes, **sizes}.items():
        if identity.get(key) != value:
            issues.append(f"manifest {key} differs from finalized file")
    if sizes["data_size_bytes"] != EXPECTED_DATA_SIZE:
        issues.append("data size is not exact 1.0 s dual-RX CI16")
    try:
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        continuity = audit_continuity_metadata(
            _mapping(metadata, "SigMF metadata"),
            expected_total_samples=EXPECTED_SAMPLE_COUNT,
            expected_samples_per_block=EXPECTED_SAMPLES_PER_BLOCK,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        continuity = {"passed": False, "error": f"{type(error).__name__}: {error}"}
        issues.append(f"ABI2 continuity audit failed: {error}")
    else:
        if continuity.get("stream_id") != identity.get("stream_id"):
            issues.append("manifest stream ID differs from ABI2 ledger")
        if continuity.get("metadata_abi") != identity.get("metadata_abi"):
            issues.append("manifest metadata ABI differs from ABI2 ledger")
    try:
        capture = json.loads(capture_file.read_text(encoding="utf-8"))
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        capture_root = _mapping(capture, "capture record")
        analysis_root = _mapping(analysis, "analysis record")
        artifact = _mapping(capture_root.get("artifact"), "capture artifact")
        capture_evidence = _mapping(capture_root.get("artifact_evidence"), "capture evidence")
        source_profile = _mapping(capture_root.get("source_profile"), "source profile")
        firmware_evidence = _mapping(capture_root.get("firmware_evidence"), "firmware evidence")
        capture_dependency = _mapping(
            capture_root.get("pluto_plus_utils_source_attestation"),
            "capture pluto-plus-utils source attestation",
        )
        analysis_dependency = _mapping(
            analysis_root.get("pluto_plus_utils_source_attestation"),
            "analysis pluto-plus-utils source attestation",
        )
        capture_settings = _mapping(capture_root.get("capture"), "capture settings")
        analysis_evidence = _mapping(analysis_root.get("artifact_evidence"), "analysis evidence")
        recorded_source_attestation = _mapping(
            analysis_root.get("analysis_source_attestation"),
            "analysis source attestation",
        )
        aggregation = _mapping(analysis_root.get("aggregation_key"), "aggregation key")
        quality = _mapping(analysis_root.get("quality_gate"), "quality gate")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        issues.append(f"capture/analysis record audit failed: {error}")
    else:
        if (
            artifact.get("artifact_id") != artifact_id
            or artifact.get("sha256") != hashes["data_sha256"]
        ):
            issues.append("capture artifact identity or data SHA differs")
        if (
            capture_evidence.get("data_sha256") != hashes["data_sha256"]
            or capture_evidence.get("metadata_sha256") != hashes["metadata_sha256"]
            or capture_evidence.get("metadata_size_bytes") != sizes["metadata_size_bytes"]
        ):
            issues.append("capture record does not bind finalized data and metadata")
        if (
            analysis_evidence.get("data_sha256") != hashes["data_sha256"]
            or analysis_evidence.get("metadata_sha256") != hashes["metadata_sha256"]
            or analysis_evidence.get("capture_record_sha256") != hashes["capture_record_sha256"]
        ):
            issues.append("analysis does not bind finalized artifact and capture record")
        try:
            replayed_source_attestation = attest_source_files_at_commit(
                Path(__file__).resolve().parents[1],
                expected_commit=source_commit,
                relative_paths=HEXCAL_ANALYSIS_SOURCE_FILES,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            replayed_source_attestation = None
            issues.append(f"analysis source attestation replay failed: {error}")
        else:
            if (
                analysis_root.get("source_commit") != source_commit
                or dict(recorded_source_attestation) != replayed_source_attestation
            ):
                issues.append("analysis scientific source attestation differs on replay")
        if source_profile.get("file_sha256") != profile_sha256:
            issues.append("capture source profile SHA differs from run profile")
        if capture_root.get("source_commit") != source_commit or dict(firmware_evidence) != dict(
            expected_firmware_evidence
        ):
            issues.append("capture source or flashed firmware evidence differs from plan")
        if (
            dict(capture_dependency) != dict(expected_dependency_attestation)
            or dict(analysis_dependency) != dict(expected_dependency_attestation)
            or capture_root.get("pluto_plus_utils_source_attestation_sha256")
            != expected_dependency_sha256
            or analysis_root.get("pluto_plus_utils_source_attestation_sha256")
            != expected_dependency_sha256
            or aggregation.get("pluto_plus_utils_source_attestation_sha256")
            != expected_dependency_sha256
            or attempt.get("pluto_plus_utils_source_attestation_sha256")
            != expected_dependency_sha256
            or identity.get("pluto_plus_utils_source_attestation_sha256")
            != expected_dependency_sha256
        ):
            issues.append("pluto-plus-utils source evidence differs across the artifact chain")
        if (
            capture_settings.get("board_id") != board_id
            or capture_settings.get("serial") != serial
            or capture_settings.get("uri") != uri
            or capture_settings.get("tx_channel") != 0
            or capture_settings.get("tx_port") != "TX1"
            or capture_settings.get("sample_rate_hz") != 1_000_000
            or capture_settings.get("sample_count") != EXPECTED_SAMPLE_COUNT
            or capture_settings.get("samples_per_frame") != EXPECTED_SAMPLES_PER_BLOCK
            or capture_settings.get("frame_count") != 10
            or capture_settings.get("kernel_buffers") != 8
        ):
            issues.append("capture settings differ from exact USB TX1 plan")
        if (
            aggregation.get("artifact_id") != artifact_id
            or aggregation.get("serial") != serial
            or aggregation.get("uri") != uri
            or aggregation.get("profile_file_sha256") != profile_sha256
            or aggregation.get("center_frequency_hz") != attempt.get("center_frequency_hz")
            or aggregation.get("implementation_source_commit")
            != attempt.get("implementation_source_commit")
            or aggregation.get("firmware_evidence_sha256")
            != attempt.get("firmware_evidence_sha256")
            or aggregation.get("firmware_bin_sha256") != attempt.get("firmware_bin_sha256")
            or aggregation.get("full_flash_readback_sha256")
            != attempt.get("full_flash_readback_sha256")
        ):
            issues.append("analysis aggregation identity differs from run condition")
        try:
            rf_readback = _mapping(
                capture_settings.get("rf_readback_evidence"),
                "capture RF readback evidence",
            )
            rf_readback_sha256 = canonical_json_sha256(rf_readback)
            normalized_rf_readback = validate_tx1_rf_readback_evidence(
                rf_readback,
                planned_kernel_buffers=8,
                planned_tx_gain_db=float(attempt["planned_tx_hardware_gain_db"]),
                planned_dds_scale=float(attempt["planned_dds_scale"]),
                planned_tone_hz=100_000.0,
                sample_rate_hz=1_000_000.0,
            )
        except (KeyError, TypeError, ValueError) as error:
            rf_readback_sha256 = None
            normalized_rf_readback = None
            issues.append(f"RF readback evidence failed validation: {error}")
        else:
            assert normalized_rf_readback is not None
            if (
                capture_settings.get("rf_readback_evidence_sha256") != rf_readback_sha256
                or aggregation.get("rf_readback_evidence_sha256") != rf_readback_sha256
                or identity.get("rf_readback_evidence_sha256") != rf_readback_sha256
                or capture_settings.get("kernel_buffers")
                != normalized_rf_readback["kernel_buffers"]
                or capture_settings.get("tx_gain_readback_db")
                != normalized_rf_readback["tx1_gain_readback_db"]
                or capture_settings.get("dds_scale_readback")
                != normalized_rf_readback["dds_scale_readback"]
                or capture_settings.get("dds_enabled_readback")
                != normalized_rf_readback["dds_enabled_readback"]
                or capture_settings.get("dds_frequency_readback_hz")
                != normalized_rf_readback["dds_frequency_readback_hz"]
            ):
                issues.append("RF readback evidence is not hash-bound across the artifact chain")
        readback = (
            normalized_rf_readback["dds_frequency_readback_hz"]
            if normalized_rf_readback is not None
            else capture_settings.get("dds_frequency_readback_hz")
        )
        if not isinstance(readback, list) or len(readback) != 8:
            issues.append("capture DDS frequency readback is malformed")
        else:
            try:
                dds_readback = [float(value) for value in readback]
            except (TypeError, ValueError):
                dds_readback = []
            if len(dds_readback) != 8 or not all(math.isfinite(value) for value in dds_readback):
                issues.append("capture DDS frequency readback is non-numeric or non-finite")
            else:
                tone_offsets = [abs(dds_readback[index]) for index in (0, 2)]
                tone_offset = sum(tone_offsets) / 2.0
                emitted = float(capture_settings["center_frequency_hz"]) + tone_offset
                if (
                    abs(tone_offsets[0] - tone_offsets[1])
                    > math.ceil(float(capture_settings["sample_rate_hz"]) / (1 << 16))
                    or aggregation.get("dds_frequency_readback_hz") != readback
                    or identity.get("dds_frequency_readback_hz") != readback
                    or aggregation.get("dds_tone_offset_hz") != tone_offset
                    or aggregation.get("emitted_carrier_frequency_hz") != emitted
                    or identity.get("dds_tone_offset_hz") != tone_offset
                    or identity.get("emitted_carrier_frequency_hz") != emitted
                ):
                    issues.append("DDS readback or emitted carrier binding differs")
        expected_quality = attempt.get("outcome") == "quality_passed"
        if quality.get("passed") is not expected_quality:
            issues.append("analysis quality differs from runner outcome")
        try:
            raw_headroom = _headroom_from_data(data_file)
            recorded_headroom = _mapping(
                capture_settings.get("adc_headroom_admission"), "recorded headroom"
            )
            issues.extend(_compare_headroom(raw_headroom, recorded_headroom))
        except (OSError, ValueError) as error:
            raw_headroom = {"passed": False, "error": str(error)}
            issues.append(f"raw headroom audit failed: {error}")
        try:
            if not isinstance(continuity, Mapping) or continuity.get("metadata_abi") != 2:
                raise ValueError("continuity prerequisites did not pass")
            if "tone_offset" not in locals():
                raise ValueError("DDS tone-offset prerequisites did not pass")
            rx2 = load_ci16_channel(
                data_file,
                sample_count=EXPECTED_SAMPLE_COUNT,
                receiver_count=2,
                channel=1,
            )
            replayed_hexcal = analyze_hexcal_samples(
                rx2,
                sample_rate_hz=1_000_000,
                tone_offset_hz=float(tone_offset),
                profile=profile,
                continuity_verified=True,
            )
            replayed_quality = evaluate_hexcal_quality(
                replayed_hexcal,
                headroom_passed=bool(raw_headroom.get("passed")),
            )
        except (OSError, ValueError) as error:
            issues.append(f"scientific analysis replay failed: {error}")
        else:
            if analysis_root.get("hexcal") != replayed_hexcal:
                issues.append("persisted complex analysis differs from deterministic replay")
            if dict(quality) != replayed_quality:
                issues.append("persisted quality gate differs from deterministic replay")
    return {
        "artifact_id": artifact_id,
        "center_frequency_hz": attempt.get("center_frequency_hz"),
        "round_index": attempt.get("round_index"),
        "outcome": attempt.get("outcome"),
        "passed": not issues,
        "issues": issues,
        "hashes": hashes,
        "sizes": sizes,
        "continuity": continuity,
        "raw_adc_headroom": raw_headroom if "raw_headroom" in locals() else None,
    }


def audit_manifest(
    manifest: Mapping[str, Any],
    *,
    board_id: str,
    serial: str,
    uri: str,
    manifest_path: Path,
) -> dict[str, Any]:
    issues: list[str] = []
    supported_experiment_kinds = {
        "hexcal_v1_tx1_center_calibration",
        "hexcal_v2_2_2g4_tx1_center_calibration",
    }
    if manifest.get("schema") != 1 or manifest.get("experiment_kind") not in (
        supported_experiment_kinds
    ):
        issues.append("manifest schema or experiment kind is unsupported")
    configuration = _mapping(manifest.get("configuration"), "configuration")
    protocol_id = configuration.get("protocol_id", "hexcal-v1")
    expected_experiment_kind = (
        "hexcal_v2_2_2g4_tx1_center_calibration"
        if protocol_id == STIMULUS_PROTOCOL_ID
        else "hexcal_v1_tx1_center_calibration"
    )
    if manifest.get("experiment_kind") != expected_experiment_kind:
        issues.append("manifest experiment kind differs from its protocol ID")
    if (
        configuration.get("board_id") != board_id
        or configuration.get("serial") != serial
        or configuration.get("uri") != uri
    ):
        issues.append("explicit board/serial/URI differ from manifest")
    if manifest.get("status") != "complete":
        issues.append("manifest status is not complete")
    profile_path = Path(str(configuration.get("profile")))
    profile_sha = str(configuration.get("profile_file_sha256"))
    source_commit = configuration.get("implementation_source_commit")
    if not isinstance(source_commit, str) or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        issues.append("implementation source commit is malformed")
    audit_source_attestation: dict[str, Any] = {}
    try:
        if not isinstance(source_commit, str):
            raise ValueError("implementation source commit is unavailable")
        audit_source_attestation = attest_source_files_at_commit(
            Path(__file__).resolve().parents[1],
            expected_commit=source_commit,
            relative_paths=AUDIT_SOURCE_FILES,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        issues.append(f"independent audit source attestation failed: {error}")
    try:
        profile = load_hexcal_profile(profile_path)
        if profile.file_sha256 != profile_sha or profile.contract_sha256 != configuration.get(
            "profile_contract_sha256"
        ):
            issues.append("current frozen source profile differs from persisted plan")
    except (OSError, ValueError) as error:
        profile = None
        issues.append(f"cannot validate current frozen source profile: {error}")
    try:
        firmware_configuration = _mapping(
            configuration.get("firmware_evidence"), "configured firmware evidence"
        )
        if profile is None or not isinstance(source_commit, str):
            raise ValueError("profile/source prerequisites are unavailable")
        firmware_evidence = load_hexcal_firmware_evidence(
            Path(str(firmware_configuration.get("path"))),
            expected_board_id=board_id,
            expected_source_commit=source_commit,
            expected_profile=profile,
        )
        if firmware_evidence.as_dict() != dict(firmware_configuration):
            issues.append("configured firmware evidence differs from revalidated evidence")
    except (OSError, ValueError) as error:
        firmware_evidence = None
        issues.append(f"cannot revalidate flashed firmware evidence: {error}")
    try:
        dependency_configuration = _mapping(
            configuration.get("pluto_plus_utils_source_attestation"),
            "configured pluto-plus-utils source attestation",
        )
        dependency_configuration_sha256 = str(
            configuration.get("pluto_plus_utils_source_attestation_sha256")
        )
        dependency_attestation = attest_pluto_plus_utils_source()
        dependency_sha256 = canonical_json_sha256(dependency_attestation)
        if (
            dependency_attestation != dict(dependency_configuration)
            or dependency_sha256 != dependency_configuration_sha256
        ):
            raise ValueError("configured pluto-plus-utils source evidence differs on replay")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        dependency_attestation = {}
        dependency_sha256 = ""
        issues.append(f"cannot revalidate pluto-plus-utils source evidence: {error}")
    qualification_issues: list[str] = []
    qualification_source_attestation: dict[str, Any] = {}
    qualification_evidence: dict[str, Any] = {}
    try:
        qualification_kind = "stimulus" if protocol_id == STIMULUS_PROTOCOL_ID else "gain"
        qualification_configuration = _mapping(
            configuration.get(f"{qualification_kind}_qualification"),
            f"configured {qualification_kind} qualification",
        )
        center_frequencies = configuration.get("center_frequencies_hz")
        if (
            not isinstance(center_frequencies, list)
            or not center_frequencies
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in center_frequencies
            )
        ):
            raise ValueError("configured calibration frequencies are malformed")
        receiver_gain = configuration.get("receiver_gain_db")
        tx_gain = configuration.get("tx_hardware_gain_db")
        dds_scale = configuration.get("dds_scale")
        if (
            isinstance(receiver_gain, bool)
            or not isinstance(receiver_gain, int)
            or isinstance(tx_gain, bool)
            or not isinstance(tx_gain, (int, float))
            or isinstance(dds_scale, bool)
            or not isinstance(dds_scale, (int, float))
            or not math.isfinite(float(tx_gain))
            or not math.isfinite(float(dds_scale))
        ):
            raise ValueError("configured calibration RF gain/scale plan is malformed")
        if (
            profile is None
            or firmware_evidence is None
            or not dependency_attestation
            or not isinstance(source_commit, str)
        ):
            raise ValueError("qualification replay prerequisites are unavailable")
        qualification_source_attestation = attest_source_files_at_commit(
            Path(__file__).resolve().parents[1],
            expected_commit=source_commit,
            relative_paths=QUALIFICATION_SOURCE_FILES,
        )
        if qualification_kind == "stimulus":
            stimulus_qualification = load_hexcal_stimulus_qualification(
                Path(str(qualification_configuration.get("path"))),
                expected_board_id=board_id,
                expected_serial=serial,
                expected_uri=uri,
                expected_source_commit=source_commit,
                expected_source_attestation=qualification_source_attestation,
                expected_profile=profile,
                expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
                expected_pluto_plus_utils_source_attestation_sha256=dependency_sha256,
                expected_center_frequencies_hz=center_frequencies,
                expected_receiver_gain_db=int(receiver_gain),
                expected_dds_scale=float(dds_scale),
            )
            qualification_evidence = stimulus_qualification.as_dict()
            if (
                stimulus_qualification.fixed_receiver_gain_db != receiver_gain
                or stimulus_qualification.selected_tx_hardware_gain_db != float(tx_gain)
                or stimulus_qualification.dds_scale != float(dds_scale)
            ):
                qualification_issues.append(
                    "calibration RF settings differ from the frozen stimulus qualification"
                )
        else:
            gain_qualification = load_hexcal_gain_qualification(
                Path(str(qualification_configuration.get("path"))),
                expected_board_id=board_id,
                expected_serial=serial,
                expected_uri=uri,
                expected_source_commit=source_commit,
                expected_source_attestation=qualification_source_attestation,
                expected_profile=profile,
                expected_firmware_evidence_sha256=firmware_evidence.file_sha256,
                expected_pluto_plus_utils_source_attestation_sha256=dependency_sha256,
                expected_center_frequencies_hz=center_frequencies,
                expected_tx_hardware_gain_db=float(tx_gain),
                expected_dds_scale=float(dds_scale),
            )
            qualification_evidence = gain_qualification.as_dict()
            if gain_qualification.selected_receiver_gain_db != receiver_gain:
                qualification_issues.append(
                    "calibration receiver gain differs from the qualified selected gain"
                )
        if qualification_evidence != dict(qualification_configuration):
            qualification_issues.append(
                f"configured {qualification_kind} qualification differs from independently "
                "replayed evidence"
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        qualification_issues.append(
            f"cannot independently replay {qualification_kind} qualification: {error}"
        )
    issues.extend(qualification_issues)
    plan = manifest.get("plan")
    attempts = manifest.get("attempts")
    if not isinstance(plan, list) or not isinstance(attempts, list):
        raise ValueError("manifest plan or attempts are malformed")
    planned_qualification_configuration = configuration.get(f"{qualification_kind}_qualification")
    expected_qualification_id = (
        planned_qualification_configuration.get("qualification_id")
        if isinstance(planned_qualification_configuration, Mapping)
        else None
    )
    expected_qualification_sha = (
        planned_qualification_configuration.get("file_sha256")
        if isinstance(planned_qualification_configuration, Mapping)
        else None
    )
    for index, raw_condition in enumerate(plan):
        condition = _mapping(raw_condition, f"plan[{index}]")
        qualification_binding_mismatch = (
            condition.get("stimulus_qualification_id") != expected_qualification_id
            or condition.get("stimulus_qualification_sha256") != expected_qualification_sha
            or condition.get("qualification_kind") != qualification_kind
            or condition.get("qualification_id") != expected_qualification_id
            or condition.get("qualification_sha256") != expected_qualification_sha
            if qualification_kind == "stimulus"
            else condition.get("gain_qualification_id") != expected_qualification_id
            or condition.get("gain_qualification_sha256") != expected_qualification_sha
        )
        if qualification_binding_mismatch or condition.get("receiver_gain_db") != configuration.get(
            "receiver_gain_db"
        ):
            qualifier_label = (
                "stimulus qualification"
                if qualification_kind == "stimulus"
                else "RX-gain qualification"
            )
            issues.append(f"plan condition {index} differs from the fixed {qualifier_label}")
    completed: dict[int, Mapping[str, Any]] = {}
    failed_attempts: list[dict[str, Any]] = []
    failed_attempt_records: list[Mapping[str, Any]] = []
    for raw in attempts:
        attempt = _mapping(raw, "attempt")
        plan_index = attempt.get("plan_index")
        if not isinstance(plan_index, int) or not 0 <= plan_index < len(plan):
            issues.append("attempt references an unknown plan condition")
            continue
        condition = _mapping(plan[plan_index], f"plan[{plan_index}]")
        if condition.get("plan_index") != plan_index:
            issues.append(f"plan condition {plan_index} has a malformed index")
        if any(attempt.get(key) != value for key, value in condition.items()):
            issues.append(f"attempt {attempt.get('attempt_id')} differs from its plan condition")
        if attempt.get("status") == "complete" and attempt.get("outcome") in {
            "quality_passed",
            "quality_rejected",
        }:
            if plan_index in completed:
                issues.append(f"plan condition {plan_index} completed more than once")
            completed[plan_index] = attempt
            post_mute = attempt.get("post_mute")
            if not _passed_exact_mute(post_mute, serial=serial, purpose="post_attempt"):
                issues.append(f"condition {plan_index} lacks exact-serial post mute")
        elif attempt.get("status") == "failed":
            if attempt.get("artifact_identity") is not None:
                issues.append("failed attempt improperly accepts an artifact identity")
            post_mute = attempt.get("post_mute")
            failure_kind = attempt.get("failure_kind")
            recovered_stale = attempt.get("recovered_stale_process") is True
            if failure_kind == "execution":
                ordinary_mute_passed = _passed_exact_mute(
                    post_mute, serial=serial, purpose="post_attempt"
                )
                recovery_mute_passed = _passed_exact_mute(
                    attempt.get("recovery_mute"),
                    serial=serial,
                    purpose="resume_recovery",
                )
                if recovered_stale:
                    if not recovery_mute_passed or post_mute is not None:
                        issues.append(
                            "stale execution attempt lacks its exact resume recovery mute"
                        )
                elif not ordinary_mute_passed:
                    issues.append("failed execution attempt lacks a passed exact-serial mute")
            if failure_kind == "post_attempt_mute" and not _passed_exact_mute(
                attempt.get("recovery_mute"), serial=serial, purpose="resume_recovery"
            ):
                issues.append("failed post-attempt mute lacks a passed resume recovery mute")
            failed_attempt_records.append(attempt)
            failed_attempts.append(
                {
                    "attempt_id": attempt.get("attempt_id"),
                    "failure_kind": failure_kind,
                    "unaccepted_artifact_id": attempt.get("artifact_id"),
                    "post_mute_passed": _passed_exact_mute(
                        post_mute, serial=serial, purpose="post_attempt"
                    ),
                    "recovered_stale_process": recovered_stale,
                    "recovery_mute_passed": _passed_exact_mute(
                        attempt.get("recovery_mute"),
                        serial=serial,
                        purpose="resume_recovery",
                    ),
                }
            )
        else:
            issues.append("attempt is neither completed quality nor failed execution")
    if set(completed) != set(range(len(plan))):
        issues.append("not every persisted plan condition completed exactly once")
    artifact_audits = [
        _audit_artifact(
            completed[index],
            board_id=board_id,
            serial=serial,
            uri=uri,
            profile_sha256=profile_sha,
            profile=profile,
            source_commit=str(source_commit),
            expected_firmware_evidence=_mapping(
                configuration.get("firmware_evidence"), "configured firmware evidence"
            ),
            expected_dependency_attestation=dependency_attestation,
            expected_dependency_sha256=dependency_sha256,
        )
        for index in sorted(completed)
    ]
    stream_ids = [
        int(_mapping(completed[index]["artifact_identity"], "identity")["stream_id"])
        for index in sorted(completed)
    ]
    if len(stream_ids) != len(set(stream_ids)):
        issues.append("accepted captures do not have unique fresh ABI2 stream IDs")
    if any(not item["passed"] for item in artifact_audits):
        issues.append("one or more accepted artifacts failed independent audit")
    accepted_artifact_ids = {
        str(_mapping(item["artifact_identity"], "accepted identity")["artifact_id"])
        for item in completed.values()
    }
    for attempt, report in zip(failed_attempt_records, failed_attempts, strict=True):
        raw_quarantines = attempt.get("quarantined_failures")
        if not isinstance(raw_quarantines, list):
            issues.append("failed attempt quarantine evidence is malformed")
            raw_quarantines = []
        attempt_quarantines: list[dict[str, Any]] = []
        for raw_quarantine in raw_quarantines:
            quarantine, quarantine_issues = _audit_quarantined_failure(
                raw_quarantine,
                accepted_artifact_ids=accepted_artifact_ids,
            )
            attempt_quarantines.append(quarantine)
            issues.extend(quarantine_issues)
        capture_text = json.dumps(attempt.get("capture"), default=str).lower()
        enodata = any(
            marker in capture_text for marker in ("enodata", "no data available", "errno 61")
        )
        if enodata and not attempt_quarantines:
            issues.append("ENODATA attempt lacks persisted .failed quarantine evidence")
        report["enodata_detected"] = enodata
        report["quarantined_failures"] = attempt_quarantines
    recovery_attempts = manifest.get("recovery_mute_attempts")
    if not isinstance(recovery_attempts, list):
        issues.append("recovery mute attempt ledger is malformed")
        recovery_attempts = []
    for attempt in failed_attempt_records:
        if attempt.get("failure_kind") != "post_attempt_mute" and not (
            attempt.get("failure_kind") == "execution"
            and attempt.get("recovered_stale_process") is True
        ):
            continue
        recovery = attempt.get("recovery_mute")
        if not any(candidate == recovery for candidate in recovery_attempts):
            issues.append("resume recovery evidence is absent from recovery ledger")
    final = manifest.get("final_mute")
    if not _passed_exact_mute(final, serial=serial, purpose="final"):
        issues.append("run lacks a passed exact-serial final mute")
    calibration_path = manifest_path.parent / CALIBRATION_FILENAME
    calibration: dict[str, Any] | None = None
    if not calibration_path.is_file():
        issues.append("required calibration artifact is missing")
    else:
        try:
            calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
            calibration_root = _mapping(calibration_document, "calibration")
            aggregation_source_attestation = attest_source_files_at_commit(
                Path(__file__).resolve().parents[1],
                expected_commit=str(source_commit),
                relative_paths=HEXCAL_AGGREGATION_SOURCE_FILES,
            )
            expected_calibration_kind = (
                "hexcal_v2_2_2g4_tx1_center_end_to_end_complex_correction"
                if protocol_id == STIMULUS_PROTOCOL_ID
                else "hexcal_v1_tx1_center_end_to_end_complex_correction"
            )
            v2_binding_mismatch = protocol_id == STIMULUS_PROTOCOL_ID and (
                calibration_root.get("protocol_id") != protocol_id
                or calibration_root.get("qualification_kind") != qualification_kind
                or calibration_root.get("qualification")
                != configuration.get("stimulus_qualification")
                or calibration_root.get("stimulus_qualification")
                != configuration.get("stimulus_qualification")
            )
            if (
                calibration_root.get("schema") != 1
                or calibration_root.get("calibration_kind") != expected_calibration_kind
                or v2_binding_mismatch
                or calibration_root.get("run_id") != manifest.get("run_id")
                or calibration_root.get("manifest_path") != str(manifest_path)
                or calibration_root.get("source_commit") != source_commit
                or calibration_root.get("aggregation_source_attestation")
                != aggregation_source_attestation
                or calibration_root.get("manifest_sha256") != sha256_path(manifest_path)
                or calibration_root.get("serial") != serial
                or calibration_root.get("uri") != uri
                or calibration_root.get("profile_file_sha256") != profile_sha
                or calibration_root.get("profile_contract_sha256")
                != configuration.get("profile_contract_sha256")
                or calibration_root.get("capture_implementation_source_commit") != source_commit
                or calibration_root.get("receiver_gain_db") != configuration.get("receiver_gain_db")
                or calibration_root.get("gain_qualification")
                != configuration.get("gain_qualification")
                or calibration_root.get("firmware_evidence")
                != configuration.get("firmware_evidence")
                or calibration_root.get("pluto_plus_utils_source_attestation")
                != configuration.get("pluto_plus_utils_source_attestation")
                or calibration_root.get("pluto_plus_utils_source_attestation_sha256")
                != configuration.get("pluto_plus_utils_source_attestation_sha256")
                or calibration_root.get("aggregation_python_runtime")
                != {
                    "executable": dependency_attestation.get("python_executable"),
                    "prefix": dependency_attestation.get("python_prefix"),
                }
            ):
                issues.append("calibration does not bind the audited run manifest")
            expected_geometry = {
                "element_count": 6,
                "diameter_mm": 51.0,
                "order": [f"ANT{index}" for index in range(1, 7)],
                "direction": "clockwise",
                "forward_reference": "ANT1",
                "clockwise_bearings_from_forward_deg": [0, 60, 120, 180, 240, 300],
                "source": "TX1 at nominal array center",
            }
            if calibration_root.get("array_geometry") != expected_geometry:
                issues.append("calibration array geometry differs from ANT1-forward clockwise C6")
            aggregation_module = _load_aggregation_module()
            replayed_scientific_payload = aggregation_module.build_calibration_scientific_payload(
                manifest
            )
            scientific_mismatches = [
                field
                for field in SCIENTIFIC_CALIBRATION_FIELDS
                if calibration_root.get(field) != replayed_scientific_payload.get(field)
            ]
            if scientific_mismatches:
                issues.append(
                    "calibration scientific payload differs from deterministic replay: "
                    + ", ".join(scientific_mismatches)
                )
            calibration_quality = _mapping(
                calibration_root.get("quality_gate"), "calibration quality"
            ).get("passed")
            if calibration_quality is not True:
                issues.append("calibration scientific quality gate did not pass")
            calibration = {
                "path": str(calibration_path),
                "sha256": sha256_path(calibration_path),
                "size_bytes": calibration_path.stat().st_size,
                "quality_passed": calibration_quality,
                "scientific_payload_replayed": True,
                "scientific_mismatches": scientific_mismatches,
                "aggregation_source_attestation": aggregation_source_attestation,
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as error:
            issues.append(f"calibration artifact audit failed: {error}")
    return {
        "schema": 1,
        "audit_kind": (
            "hexcal_v2_2_2g4_independent_calibration_and_artifact_audit"
            if protocol_id == STIMULUS_PROTOCOL_ID
            else "hexcal_v1_independent_calibration_and_artifact_audit"
        ),
        "protocol_id": protocol_id,
        "run_id": manifest.get("run_id"),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "serial": serial,
        "uri": uri,
        "passed": not issues,
        "issues": issues,
        "planned_condition_count": len(plan),
        "completed_condition_count": len(completed),
        "failed_execution_attempts": failed_attempts,
        "unique_stream_ids": stream_ids,
        "artifact_audits": artifact_audits,
        "calibration_artifact": calibration,
        "audit_source_attestation": audit_source_attestation or None,
        "qualification_audit": {
            "kind": qualification_kind,
            "passed": not qualification_issues,
            "issues": qualification_issues,
            "qualification": qualification_evidence or None,
            "source_attestation": qualification_source_attestation or None,
            "source_attestation_sha256": (
                canonical_json_sha256(qualification_source_attestation)
                if qualification_source_attestation
                else None
            ),
            "raw_artifacts_and_selection_replayed": not qualification_issues,
        },
        "gain_qualification_audit": (
            {
                "passed": not qualification_issues,
                "issues": qualification_issues,
                "qualification": qualification_evidence or None,
                "source_attestation": qualification_source_attestation or None,
                "source_attestation_sha256": (
                    canonical_json_sha256(qualification_source_attestation)
                    if qualification_source_attestation
                    else None
                ),
                "raw_artifacts_and_selection_replayed": not qualification_issues,
            }
            if qualification_kind == "gain"
            else None
        ),
        "attestations": {
            "data_sha256_recomputed": True,
            "finalized_metadata_sha256_and_size_recomputed": True,
            "abi2_flags_counters_order_and_rate_revalidated": True,
            "raw_ci16_adc_headroom_recomputed": True,
            "analysis_sha256_recomputed": True,
            "scientific_analysis_replayed_from_raw_ci16": True,
            "calibration_scientific_payload_rebuilt_from_accepted_analyses": (
                calibration is not None
                and calibration.get("scientific_payload_replayed") is True
                and not calibration.get("scientific_mismatches")
            ),
            "audit_and_aggregation_sources_attested_to_implementation_commit": bool(
                audit_source_attestation
            ),
            "full_flash_readback_and_target_uid_revalidated": firmware_evidence is not None,
            "pluto_plus_utils_clean_source_and_import_paths_revalidated": bool(
                dependency_attestation
            ),
            "qualification_source_raw_artifacts_and_selection_revalidated": (
                not qualification_issues
            ),
            "failed_partial_quarantine_hashes_recomputed": True,
            "post_mute_resume_recovery_checked": True,
            "post_and_final_exact_serial_mutes_checked": True,
        },
    }


def main() -> int:
    args = _parser().parse_args()
    run_root = (
        Path.home()
        / ".local/state/smateway/boards"
        / args.board_id
        / "hexcal-distributions"
        / args.run_id
    )
    manifest_path = run_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest root is not an object")
        audit = audit_manifest(
            manifest,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            manifest_path=manifest_path,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"cannot audit run: {error}") from error
    output_path = run_root / AUDIT_FILENAME
    write_json_atomic(output_path, audit)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "audit": str(output_path),
                "audit_sha256": sha256_path(output_path),
                "passed": audit["passed"],
                "issue_count": len(audit["issues"]),
            },
            sort_keys=True,
        )
    )
    return 0 if audit["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
