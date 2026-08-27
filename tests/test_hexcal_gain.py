from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pluto_plus.artifacts import complex_to_ci16

from smateway.hexcal import (
    REQUIRED_METADATA_FLAGS,
    canonical_json_sha256,
    load_hexcal_profile,
    sha256_path,
)
from smateway.hexcal_gain import (
    BANDWIDTH_HZ,
    CONDITION_TIMEOUT_S,
    DEFAULT_STIMULUS_TX_GAINS_DB,
    FRAME_COUNT,
    KERNEL_BUFFERS,
    QUALIFICATION_KIND,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    STIMULUS_FIXED_RECEIVER_GAIN_DB,
    STIMULUS_PROTOCOL_ID,
    STIMULUS_QUALIFICATION_KIND,
    TOTAL_SAMPLES,
    load_hexcal_gain_qualification,
    load_hexcal_stimulus_qualification,
    qualification_thresholds,
    replay_hexcal_gain_artifact,
)

REPOSITORY = Path(__file__).resolve().parents[1]
PROFILE = load_hexcal_profile(REPOSITORY / "profiles/hexcal-v1/control_profile.json")
SOURCE_COMMIT = "1" * 40
FIRMWARE_SHA = "2" * 64
DEPENDENCY = {"schema": 1, "dependency": "pluto-plus-utils", "commit": "3" * 40}
SOURCE_ATTESTATION = {"commit": SOURCE_COMMIT, "files": []}
FREQUENCIES = (2_400_000_000,)


def test_qualification_thresholds_freeze_circular_phase_gauge() -> None:
    assert qualification_thresholds()["minimum_phase_gauge_resultant"] == 0.25


def _mute(purpose: str) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": "serial-a",
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }


def _metadata(*, artifact_id: str, gain: int, frequency: int) -> dict[str, Any]:
    first_sample = 5_000_000
    blocks = []
    for index in range(FRAME_COUNT):
        sample_start = index * SAMPLES_PER_FRAME
        time_start = 2_000_000_000 + index * 100_000_000
        blocks.append(
            {
                "sample_start": sample_start,
                "sample_count": SAMPLES_PER_FRAME,
                "utc_ns": 1_000_000_000 + index * 100_000_000,
                "metadata_abi": 2,
                "stream_id": 44,
                "buffer_sequence": index,
                "first_sample_sequence": first_sample + sample_start,
                "last_sample_sequence_exclusive": first_sample + sample_start + SAMPLES_PER_FRAME,
                "metadata_flags": REQUIRED_METADATA_FLAGS,
                "missing_samples_before": 0,
                "sample_time_realtime_start_ns": time_start,
                "sample_time_realtime_end_ns": time_start + 100_000_000,
                "sample_time_monotonic_start_ns": time_start + 1_000_000_000,
                "sample_time_monotonic_end_ns": time_start + 1_100_000_000,
                "sample_time_uncertainty_ns": 50_000,
            }
        )
    return {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": SAMPLE_RATE_HZ,
            "core:num_channels": 2,
            "pluto:artifact_id": artifact_id,
            "pluto:radio": {"serial": "serial-a", "uri": "usb:1.2.3"},
        },
        "pluto:capture": {
            "sample_count": TOTAL_SAMPLES,
            "receiver_count": 2,
            "initial_settings": {
                "center_frequency_hz": frequency,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "gain_mode": "manual",
                "gain_db": gain,
                "channels": [0, 1],
            },
        },
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "stream_id": 44,
            "block_count": FRAME_COUNT,
            "total_samples": TOTAL_SAMPLES,
            "first_sample_sequence": first_sample,
            "last_sample_sequence_exclusive": first_sample + TOTAL_SAMPLES,
            "sample_sequence_span": TOTAL_SAMPLES,
            "blocks": blocks,
        },
    }


def _samples(*, sufficient: bool, seed: int) -> np.ndarray:
    sample_numbers = np.arange(TOTAL_SAMPLES, dtype=np.float64)
    phase_us = np.mod(sample_numbers, 1_500.0)
    envelope = np.zeros(TOTAL_SAMPLES, dtype=np.complex128)
    amplitude = 520.0 if sufficient else 45.0
    for state_index in range(6):
        active_start = 200.0 + state_index * 220.0
        mask = (phase_us >= active_start) & (phase_us < active_start + 200.0)
        envelope[mask] = amplitude * np.exp(1j * math.radians(state_index * 17.0))
    carrier = np.exp(2j * np.pi * 100_000.0 / SAMPLE_RATE_HZ * sample_numbers)
    rng = np.random.default_rng(seed)
    noise_sigma = 4.0 if sufficient else 15.0
    noise = noise_sigma * (rng.normal(size=TOTAL_SAMPLES) + 1j * rng.normal(size=TOTAL_SAMPLES))
    rx2 = envelope * carrier + noise
    rx1 = 3.0 * (rng.normal(size=TOTAL_SAMPLES) + 1j * rng.normal(size=TOTAL_SAMPLES))
    return np.stack((rx1, rx2))


def _artifact(
    root: Path, label: str, *, gain: int, frequency: int, sufficient: bool
) -> dict[str, Any]:
    artifact_id = hashlib.sha256(label.encode()).hexdigest()[:32]
    artifact_root = root / "exploratory-artifacts" / artifact_id
    artifact_root.mkdir(parents=True)
    data = artifact_root / f"{artifact_id}.sigmf-data"
    metadata = artifact_root / f"{artifact_id}.sigmf-meta"
    seed = sum(label.encode())
    data.write_bytes(complex_to_ci16(_samples(sufficient=sufficient, seed=seed)).tobytes())
    metadata.write_text(
        json.dumps(_metadata(artifact_id=artifact_id, gain=gain, frequency=frequency)) + "\n",
        encoding="utf-8",
    )
    return {
        "artifact_id": artifact_id,
        "path": str(artifact_root),
        "data_path": str(data),
        "data_sha256": sha256_path(data),
        "data_size_bytes": data.stat().st_size,
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
        "metadata_size_bytes": metadata.stat().st_size,
    }


def _record(
    root: Path,
    gain: int,
    frequency: int,
    *,
    passed: bool,
    tx_gain: float = -40.0,
) -> dict[str, Any]:
    label = f"g{gain}-tx{tx_gain}-f{frequency}"
    artifact = _artifact(
        root,
        label,
        gain=gain,
        frequency=frequency,
        sufficient=passed,
    )
    replayed = replay_hexcal_gain_artifact(
        artifact,
        ledger_root=root,
        profile=PROFILE,
        expected_serial="serial-a",
        expected_uri="usb:1.2.3",
        expected_center_frequency_hz=frequency,
        expected_receiver_gain_db=gain,
        tone_offset_hz=99_991.0,
    )
    assert replayed["passed"] is passed
    return {
        "receiver_gain_db": gain,
        "tx_hardware_gain_db": tx_gain,
        "center_frequency_hz": frequency,
        "status": "complete",
        "passed": passed,
        "artifact_evidence": artifact,
        "rf_readback_evidence": {
            "schema": 1,
            "evidence_kind": "pluto_tx1_dds_live_readback",
            "tx_channel": 0,
            "tx_port": "TX1",
            "kernel_buffers": KERNEL_BUFFERS,
            "tx_hardware_gain_db_requested": tx_gain,
            "tx_hardware_gain_readback_db_by_channel": [tx_gain, -80.0],
            "tx2_gain_readback_provenance": (
                "pluto_plus_utils_capture_helper_internal_exact_readback"
            ),
            "dds_scale_requested": 0.125,
            "dds_scale_readback": [0.125, 0.0, 0.125, 0.0, 0.0, 0.0, 0.0, 0.0],
            "dds_enabled_readback": [True, False, True, False, False, False, False, False],
            "tone_frequency_hz_requested": 100_000,
            "dds_frequency_readback_hz": [
                99_991,
                0,
                -99_991,
                0,
                0,
                0,
                0,
                0,
            ],
            "active_dds_indices": [0, 2],
            "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
            "inactive_dds_rf_activity_contract": (
                "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
            ),
        },
        "rx_hold_evidence": {
            "schema": 1,
            "mode": "tandem_hold",
            "channels": [0, 1],
            "requested_gain_db": gain,
            "verified_tolerance_db": 0.25,
            "provenance": ("pinned_helper_verified_each_channel_within_requested_gain_tolerance"),
        },
        "live_adc_headroom_admission": deepcopy(replayed["adc_headroom_admission"]),
        "replayed_artifact_analysis": replayed,
        "post_mute": _mute("post_condition"),
    }


def _document(root: Path, *, lower_gain_passes: bool = False) -> dict[str, Any]:
    conditions = [
        _record(root, gain, frequency, passed=gain == 1 or lower_gain_passes)
        for gain in (0, 1)
        for frequency in FREQUENCIES
    ]
    return {
        "schema": 1,
        "qualification_kind": QUALIFICATION_KIND,
        "qualification_id": "gain-a",
        "status": "passed",
        "completed_at": "2026-08-26T12:00:00+00:00",
        "configuration": {
            "board_id": "board-a",
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "source_commit": SOURCE_COMMIT,
            "source_attestation": SOURCE_ATTESTATION,
            "profile_file_sha256": PROFILE.file_sha256,
            "profile_contract_sha256": PROFILE.contract_sha256,
            "firmware_evidence": {
                "file_sha256": FIRMWARE_SHA,
                "board_id": "board-a",
                "source_commit": SOURCE_COMMIT,
                "profile_file_sha256": PROFILE.file_sha256,
                "profile_contract_sha256": PROFILE.contract_sha256,
            },
            "firmware_evidence_sha256": FIRMWARE_SHA,
            "pluto_plus_utils_source_attestation": DEPENDENCY,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(DEPENDENCY),
            "python_runtime": {
                "requested_executable": "/home/pi/pluto-plus-utils/.venv/bin/python",
                "sys_executable": "/home/pi/pluto-plus-utils/.venv/bin/python",
                "sys_prefix": "/home/pi/pluto-plus-utils/.venv",
                "smateway_source_root": "/home/pi/smateway/src",
                "hexcal_gain_module_path": "/home/pi/smateway/src/smateway/hexcal_gain.py",
                "auto_reexec_before_pluto_import": True,
            },
            "center_frequencies_hz": list(FREQUENCIES),
            "candidate_gains_db": [0, 1, 2],
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "kernel_buffers": KERNEL_BUFFERS,
            "condition_timeout_s": CONDITION_TIMEOUT_S,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_policy": "muted_-80dB_and_zero_DDS",
            "tx_hardware_gain_db": -40.0,
            "dds_scale": 0.125,
            "thresholds": qualification_thresholds(),
        },
        "plan": [
            {
                "gain_index": gain_index,
                "frequency_index": frequency_index,
                "receiver_gain_db": gain,
                "center_frequency_hz": frequency,
                "tx_channel": 0,
                "tx_port": "TX1",
            }
            for gain_index, gain in enumerate((0, 1, 2))
            for frequency_index, frequency in enumerate(FREQUENCIES)
        ],
        "conditions": conditions,
        "tested_gains_db": [0, 1],
        "selected_receiver_gain_db": 1,
        "selection_policy": "lowest_ascending_gain_passing_every_frequency_and_state",
        "calibration_gain_is_fixed": True,
        "preflight_mute": _mute("preflight"),
        "final_mute": _mute("final"),
    }


def _load(path: Path) -> Any:
    return load_hexcal_gain_qualification(
        path,
        expected_board_id="board-a",
        expected_serial="serial-a",
        expected_uri="usb:1.2.3",
        expected_source_commit=SOURCE_COMMIT,
        expected_source_attestation=SOURCE_ATTESTATION,
        expected_profile=PROFILE,
        expected_firmware_evidence_sha256=FIRMWARE_SHA,
        expected_pluto_plus_utils_source_attestation_sha256=canonical_json_sha256(DEPENDENCY),
        expected_center_frequencies_hz=FREQUENCIES,
        expected_tx_hardware_gain_db=-40.0,
        expected_dds_scale=0.125,
    )


def _stimulus_document(root: Path, *, lower_tx_passes: bool = False) -> dict[str, Any]:
    candidates = (-35.0, -30.0)
    conditions = [
        _record(
            root,
            STIMULUS_FIXED_RECEIVER_GAIN_DB,
            frequency,
            passed=tx_gain == -30.0 or lower_tx_passes,
            tx_gain=tx_gain,
        )
        for tx_gain in candidates
        for frequency in FREQUENCIES
    ]
    return {
        "schema": 1,
        "protocol_id": STIMULUS_PROTOCOL_ID,
        "qualification_kind": STIMULUS_QUALIFICATION_KIND,
        "qualification_id": "stimulus-a",
        "status": "passed",
        "completed_at": "2026-08-27T12:00:00+00:00",
        "configuration": {
            "board_id": "board-a",
            "serial": "serial-a",
            "uri": "usb:1.2.3",
            "source_commit": SOURCE_COMMIT,
            "source_attestation": SOURCE_ATTESTATION,
            "profile_file_sha256": PROFILE.file_sha256,
            "profile_contract_sha256": PROFILE.contract_sha256,
            "firmware_evidence": {
                "file_sha256": FIRMWARE_SHA,
                "board_id": "board-a",
                "source_commit": SOURCE_COMMIT,
                "profile_file_sha256": PROFILE.file_sha256,
                "profile_contract_sha256": PROFILE.contract_sha256,
            },
            "firmware_evidence_sha256": FIRMWARE_SHA,
            "pluto_plus_utils_source_attestation": DEPENDENCY,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(DEPENDENCY),
            "python_runtime": {
                "requested_executable": "/home/pi/pluto-plus-utils/.venv/bin/python",
                "sys_executable": "/home/pi/pluto-plus-utils/.venv/bin/python",
                "sys_prefix": "/home/pi/pluto-plus-utils/.venv",
                "smateway_source_root": "/home/pi/smateway/src",
                "hexcal_gain_module_path": "/home/pi/smateway/src/smateway/hexcal_gain.py",
                "auto_reexec_before_pluto_import": True,
            },
            "center_frequencies_hz": list(FREQUENCIES),
            "candidate_tx_hardware_gains_db": list(candidates),
            "fixed_receiver_gain_db": STIMULUS_FIXED_RECEIVER_GAIN_DB,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "kernel_buffers": KERNEL_BUFFERS,
            "condition_timeout_s": CONDITION_TIMEOUT_S,
            "tone_offset_hz": 100_000,
            "tx_channel": 0,
            "tx_port": "TX1",
            "tx2_policy": "muted_-80dB_and_zero_DDS",
            "dds_scale": 0.125,
            "thresholds": qualification_thresholds(),
        },
        "plan": [
            {
                "tx_gain_index": gain_index,
                "frequency_index": frequency_index,
                "receiver_gain_db": STIMULUS_FIXED_RECEIVER_GAIN_DB,
                "tx_hardware_gain_db": gain,
                "center_frequency_hz": frequency,
                "tx_channel": 0,
                "tx_port": "TX1",
            }
            for gain_index, gain in enumerate(candidates)
            for frequency_index, frequency in enumerate(FREQUENCIES)
        ],
        "conditions": conditions,
        "tested_tx_hardware_gains_db": list(candidates),
        "selected_tx_hardware_gain_db": -30.0,
        "selection_policy": ("lowest_power_ascending_tx_gain_passing_every_frequency_and_state"),
        "receiver_gain_is_fixed": True,
        "selected_stimulus_is_frozen": True,
        "preflight_mute": _mute("preflight"),
        "final_mute": _mute("final"),
    }


def _load_stimulus(path: Path) -> Any:
    return load_hexcal_stimulus_qualification(
        path,
        expected_board_id="board-a",
        expected_serial="serial-a",
        expected_uri="usb:1.2.3",
        expected_source_commit=SOURCE_COMMIT,
        expected_source_attestation=SOURCE_ATTESTATION,
        expected_profile=PROFILE,
        expected_firmware_evidence_sha256=FIRMWARE_SHA,
        expected_pluto_plus_utils_source_attestation_sha256=canonical_json_sha256(DEPENDENCY),
        expected_center_frequencies_hz=FREQUENCIES,
        expected_receiver_gain_db=STIMULUS_FIXED_RECEIVER_GAIN_DB,
        expected_candidate_tx_hardware_gains_db=(-35.0, -30.0),
        expected_dds_scale=0.125,
    )


def test_stimulus_protocol_defaults_are_frozen() -> None:
    assert DEFAULT_STIMULUS_TX_GAINS_DB == (
        -35.0,
        -30.0,
        -25.0,
        -20.0,
        -15.0,
        -10.0,
    )
    assert STIMULUS_FIXED_RECEIVER_GAIN_DB == 20


def test_machine_readable_stimulus_protocol_matches_implementation() -> None:
    document = json.loads(
        (
            REPOSITORY / "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.1-2g4-stimulus.json"
        ).read_text(encoding="utf-8")
    )
    screen = document["stimulus_screen"]
    timing = document["timing_qualification"]
    matrix = document["calibration_matrix"]

    assert document["protocol_id"] == STIMULUS_PROTOCOL_ID
    assert tuple(document["center_frequencies_hz"]) == (
        2_400_000_000,
        2_423_000_000,
        2_440_000_000,
        2_472_000_000,
        2_483_000_000,
    )
    assert tuple(screen["candidate_tx_hardware_gains_db"]) == (DEFAULT_STIMULUS_TX_GAINS_DB)
    assert screen["receiver_gain_db"] == STIMULUS_FIXED_RECEIVER_GAIN_DB
    assert screen["gates"] == {
        **qualification_thresholds(),
        "clipped_samples": 0,
    }
    assert timing["replicate_count"] == 2
    assert timing["sample_rate_hz"] == 5_000_000
    assert timing["must_use_new_artifacts_after_stimulus_selection"] is True
    assert matrix["artifact_count"] == 15
    assert sum(len(order) for order in matrix["orders_hz"]) == 15
    assert len({tuple(order) for order in matrix["orders_hz"]}) == 3


def test_stimulus_loader_reproduces_lowest_all_band_tx_level(tmp_path: Path) -> None:
    path = tmp_path / "stimulus-qualification.json"
    path.write_text(json.dumps(_stimulus_document(tmp_path)), encoding="utf-8")

    evidence = _load_stimulus(path)

    assert evidence.selected_tx_hardware_gain_db == -30.0
    assert evidence.tested_tx_hardware_gains_db == (-35.0, -30.0)
    assert evidence.fixed_receiver_gain_db == 20
    assert evidence.file_sha256 == sha256_path(path)


def test_stimulus_loader_rejects_skipped_lower_power_pass(tmp_path: Path) -> None:
    path = tmp_path / "stimulus-qualification.json"
    path.write_text(
        json.dumps(_stimulus_document(tmp_path, lower_tx_passes=True)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="lowest sufficient"):
        _load_stimulus(path)


def test_stimulus_loader_rejects_crossing_headroom_stop(tmp_path: Path) -> None:
    document = _stimulus_document(tmp_path)
    document["conditions"][0]["live_adc_headroom_admission"]["receivers"][1][
        "peak_abs_component_counts"
    ] = 1_301.0
    path = tmp_path / "stimulus-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="headroom stop boundary"):
        _load_stimulus(path)


def test_loader_reproduces_lowest_sufficient_gain_and_hashes_raw_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(_document(tmp_path)), encoding="utf-8")

    evidence = _load(path)

    assert evidence.selected_receiver_gain_db == 1
    assert evidence.tested_gains_db == (0, 1)
    assert evidence.center_frequencies_hz == FREQUENCIES
    assert evidence.file_sha256 == sha256_path(path)


def test_loader_rejects_claim_that_skips_a_lower_passing_decision(tmp_path: Path) -> None:
    document = _document(tmp_path, lower_gain_passes=True)
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="lowest sufficient"):
        _load(path)


def test_loader_rejects_incomplete_frequency_matrix(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["conditions"].pop()
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="matrix is incomplete"):
        _load(path)


def test_loader_rejects_raw_artifact_tamper(tmp_path: Path) -> None:
    document = _document(tmp_path)
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(deepcopy(document)), encoding="utf-8")
    data_path_value = Path(document["conditions"][-1]["artifact_evidence"]["data_path"])
    data_path_value.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="data SHA-256 changed"):
        _load(path)


def test_loader_rejects_tampered_derived_analysis(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["conditions"][-1]["replayed_artifact_analysis"]["state_metrics"][0][
        "pilot_snr_db"
    ] += 1.0
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="derived evidence differs"):
        _load(path)


def test_loader_rejects_rehashed_metadata_with_wrong_condition_identity(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    evidence = document["conditions"][-1]["artifact_evidence"]
    metadata_path = Path(evidence["metadata_path"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["pluto:capture"]["initial_settings"]["center_frequency_hz"] += 1
    metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    evidence["metadata_sha256"] = sha256_path(metadata_path)
    evidence["metadata_size_bytes"] = metadata_path.stat().st_size
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="identity/settings differ"):
        _load(path)


def test_loader_rejects_live_headroom_claim_that_exceeds_conservative_cap(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    document["conditions"][-1]["live_adc_headroom_admission"]["receivers"][1][
        "peak_abs_component_counts"
    ] = 1_301.0
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="persisted pass result"):
        _load(path)


def test_loader_requires_exact_preflight_mute(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["preflight_mute"] = None
    path = tmp_path / "gain-qualification.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="preflight mute"):
        _load(path)


def test_system_python_command_reexecs_pinned_runtime_before_imports() -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        (
            "/usr/bin/python3",
            str(REPOSITORY / "scripts/qualify_hexcal_rx_gain.py"),
            "--print-routed-runtime-evidence",
        ),
        cwd=REPOSITORY,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    runtime = json.loads(result.stdout)
    assert runtime["sys_executable"] == "/home/pi/pluto-plus-utils/.venv/bin/python"
    assert runtime["sys_prefix"] == "/home/pi/pluto-plus-utils/.venv"
    assert runtime["smateway_source_root"] == "/home/pi/smateway/src"
    assert runtime["auto_reexec_before_pluto_import"] is True
