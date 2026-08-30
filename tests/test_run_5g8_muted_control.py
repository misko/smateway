from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from pluto_plus.artifacts import CaptureWriter
from pluto_plus.direct_radio.usb import MetadataFlags
from pluto_plus.hardware import SampleBlockV2
from pluto_plus.models import GainMode, RadioIdentity, RadioSettings, Transport

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_muted_control.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_muted_control_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

COHORT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_muted_control_cohort.py"
COHORT_SPEC = importlib.util.spec_from_file_location(
    "analyze_5g8_muted_control_cohort_under_test", COHORT_SCRIPT
)
assert COHORT_SPEC is not None and COHORT_SPEC.loader is not None
cohort_analyzer = importlib.util.module_from_spec(COHORT_SPEC)
sys.modules[COHORT_SPEC.name] = cohort_analyzer
COHORT_SPEC.loader.exec_module(cohort_analyzer)

SOURCE_COMMIT = "1" * 40
DEPENDENCY_COMMIT = "2" * 40
SERIAL = "serial-a"
URI = "usb:1.2.3"
BOARD = "board-a"
RUN = "5p8-debug-r1-p1-muted-r01-20260829"


@pytest.fixture(autouse=True)
def _small_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "SAMPLES_PER_FRAME", 1024)
    monkeypatch.setattr(runner, "FRAME_COUNT", 3)
    monkeypatch.setattr(runner, "TOTAL_SAMPLES", 3072)
    monkeypatch.setattr(cohort_analyzer.runner, "SAMPLES_PER_FRAME", 1024)
    monkeypatch.setattr(cohort_analyzer.runner, "FRAME_COUNT", 3)
    monkeypatch.setattr(cohort_analyzer.runner, "TOTAL_SAMPLES", 3072)

    def fake_analysis(samples: np.ndarray, **_kwargs: Any) -> dict[str, Any]:
        return {
            "schema": 1,
            "analysis_kind": "true_tx_muted_dual_rx_psd",
            "sample_count": int(samples.shape[1]),
            "receivers": [{"receiver": 0}, {"receiver": 1}],
            "rx1_rx2_transfer_phasor": None,
            "transfer_phase_defined": False,
        }

    monkeypatch.setattr(runner, "analyze_muted_stream", fake_analysis)
    monkeypatch.setattr(cohort_analyzer, "analyze_muted_stream", fake_analysis)

    def validate_synthetic_selector(path: Path, **expected: Any) -> dict[str, Any]:
        document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        bindings = {
            "expected_sha256": runner.sha256_path(path),
            "expected_campaign_id": document.get("campaign_id"),
            "expected_run_id": document.get("run_id"),
            "expected_board_id": document.get("board_id"),
            "expected_image_role": document.get("image_role"),
        }
        if (
            expected != bindings
            or document.get("evidence_kind") != runner.SEALED_SELECTOR_EVIDENCE_KIND
        ):
            raise runner.SelectorFlashError("synthetic sealed-selector binding differs")
        return document

    monkeypatch.setattr(runner, "validate_sealed_selector_evidence", validate_synthetic_selector)
    monkeypatch.setattr(
        cohort_analyzer.runner,
        "validate_sealed_selector_evidence",
        validate_synthetic_selector,
    )

    synthetic_timing = {"strict_frame_count": 25, "rejected_marker_count": 0}

    def recompute_synthetic_schedule(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "p0_raw_iq_fast20_schedule_reanalysis_v1",
            "schedule_timing": synthetic_timing,
            "schedule_timing_sha256": runner.canonical_json_sha256(synthetic_timing),
            "complete_frame_count": 25,
            "rejected_marker_count": 0,
            "continuity_verified": True,
            "state_order": [f"ANT{state}" for state in range(1, 9)],
        }

    monkeypatch.setattr(runner, "_recompute_p0_schedule_timing", recompute_synthetic_schedule)
    monkeypatch.setattr(
        cohort_analyzer.runner,
        "_recompute_p0_schedule_timing",
        recompute_synthetic_schedule,
    )


def _dependency_attestation() -> dict[str, Any]:
    modules = (
        "pluto_plus.artifacts",
        "pluto_plus.bootstrap_firmware",
        "pluto_plus.hardware",
        "pluto_plus.hardware.iio",
        "pluto_plus.models",
        "pluto_plus.tandem",
    )
    return {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "repository_path": "/synthetic/pluto-plus-utils",
        "commit": DEPENDENCY_COMMIT,
        "head": DEPENDENCY_COMMIT,
        "clean_worktree_verified": True,
        "files": [{"relative_path": "src/pluto_plus/__init__.py", "sha256": "3" * 64}],
        "imported_modules": [{"module": module} for module in modules],
    }


def _native_attestation() -> dict[str, Any]:
    foundation = runner.foundation
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(foundation.REQUIRED_LIBIIO_PATH),
        "library_path_from_proc_maps": True,
        "library_sha256": foundation.REQUIRED_LIBIIO_SHA256,
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {symbol: True for symbol in foundation.REQUIRED_LIBIIO_SYMBOLS},
        "loader_search_path_first": "/usr/local/lib",
    }


def _file_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": runner.sha256_path(path),
        "size_bytes": path.stat().st_size,
    }


def _write_p0_source_manifests(
    tmp_path: Path,
    *,
    profile_contract_sha256: str,
    firmware_sha256: str,
) -> list[Path]:
    manifests: list[Path] = []
    capture_root = tmp_path / "p0-captures"
    radio = RadioIdentity(
        radio_id=SERIAL,
        serial=SERIAL,
        uri=URI,
        transport=Transport.IIO_USB,
        model="Pluto+",
        firmware_version=runner.V7_FIRMWARE_VERSION,
    )
    for source_index in range(5):
        stream_id = 50_000 + source_index
        blocks = _blocks(
            stream_id=stream_id,
            seed=20260829 + source_index,
            tone_amplitude=100.0,
        )
        writer = CaptureWriter(
            capture_root,
            radio=radio,
            settings=_settings(),
            label=f"synthetic P0 source {source_index}",
        )
        for block in blocks:
            writer.append(block, _settings(), revision=1)
        artifact = writer.finalize()
        artifact_root = Path(artifact.path)
        analysis_path = artifact_root / "fast20-reference-transfer.json"
        angle = np.deg2rad(float(source_index) * 0.1)
        phasor = 0.05 * np.exp(1j * angle)
        analysis = {
            "schema": 1,
            "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
            "source_commit": SOURCE_COMMIT,
            "artifact": artifact.model_dump(mode="json"),
            "aggregation_key": {"stream_id": stream_id},
            "capture": {
                "adc_headroom_admission": {"passed": True},
                "center_frequency_hz": runner.CENTER_FREQUENCY_HZ,
                "sample_rate_hz": runner.SAMPLE_RATE_HZ,
                "receiver_gain_db": runner.RECEIVER_GAIN_DB,
                "sample_count": runner.TOTAL_SAMPLES,
                "samples_per_frame": runner.SAMPLES_PER_FRAME,
                "frame_count": runner.FRAME_COUNT,
                "kernel_buffers": runner.KERNEL_BUFFERS,
                "metadata_abi": 2,
                "tx_channel": 0,
                "tx_gain_readback_db": -20.0,
                "dds_scale_readback": [0.25, 0.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0],
                "profile_contract_sha256": profile_contract_sha256,
            },
            "pilot": {"estimated_offset_hz": 100_000.0},
            "quality_gate": {"passed": True},
            "transfer": {
                "continuity_verified": True,
                "complete_cycle_count": 25,
                "alignment_score": 0.99,
                "alignment_even_odd_agreement": 0.99,
                "reference_valid_bin_fraction": 1.0,
                "schedule_alignment": {
                    "selected": {"complete_cycle_count": 25},
                    "decoded_timing": {
                        "strict_frame_count": 25,
                        "rejected_marker_count": 0,
                    },
                },
                "states": [{"name": f"ANT{state}"} for state in range(1, 9)],
                "all_off": {
                    "raw_rx2_over_rx1": {
                        "amplitude": 0.05,
                        "phasor": {"real": float(phasor.real), "imag": float(phasor.imag)},
                    }
                },
            },
        }
        analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
        run_id = f"campaign-a-p0-paired-r{source_index + 1:02d}-20260829"
        manifest_path = tmp_path / "p0-runs" / run_id / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        legacy_mute = {
            "attestation": "mute_returned_radio_exact_serial_readback",
            "serial": SERIAL,
            "status": "passed",
            "error": None,
        }
        manifest = {
            "schema": 1,
            "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
            "run_id": run_id,
            "status": "awaiting_rotation1",
            "created_at": "2026-08-30T00:00:00+00:00",
            "runner_source_commit": SOURCE_COMMIT,
            "configuration": {
                "board_id": BOARD,
                "serial": SERIAL,
                "frequencies_hz": [5_700_000_000, 5_800_000_000],
                "sample_rate_hz": runner.SAMPLE_RATE_HZ,
                "receiver_gain_db": runner.RECEIVER_GAIN_DB,
                "duration_s": runner.TOTAL_SAMPLES / runner.SAMPLE_RATE_HZ,
                "kernel_buffers": runner.KERNEL_BUFFERS,
                "profile_id": "fast20-v1",
                "profile_contract_sha256": profile_contract_sha256,
                "firmware_binary_sha256": firmware_sha256,
                "fixture_id": "legacy-fixture-a",
                "fully_conducted_required": True,
                "storage_medium": "raspberry_pi_local_filesystem",
                "pluto_onboard_storage_used": False,
            },
            "final_mute": {**legacy_mute, "purpose": "final_rotation0"},
            "attempts": [
                {
                    "center_frequency_hz": runner.CENTER_FREQUENCY_HZ,
                    "rotation": 0,
                    "status": "complete",
                    "outcome": "quality_passed",
                    "error": None,
                    "post_mute": {**legacy_mute, "purpose": "post_attempt"},
                    "capture": {"accepted": True},
                    "reanalysis": {"accepted": True},
                    "quality_result": {
                        "analysis_path": str(analysis_path),
                        "quality_passed": True,
                        "status": "passed",
                    },
                }
            ],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifests.append(manifest_path)
    return manifests


def _write_bound_evidence(tmp_path: Path, run_id: str = RUN) -> tuple[Path, Path, Path, list[Path]]:
    photograph = tmp_path / "setup.png"
    photograph.write_bytes(b"synthetic setup photograph")
    profile = tmp_path / "control_profile.json"
    profile_contract_sha256 = "6" * 64
    profile.write_text(
        json.dumps(
            {
                "schema": 1,
                "profile": {"id": "fast20-v1", "revision": 1},
                "contract_sha256": profile_contract_sha256,
            }
        ),
        encoding="utf-8",
    )
    firmware = tmp_path / "pluto_fast20.bin"
    firmware.write_bytes(bytes(range(64)))
    readback = tmp_path / "pluto_fast20.readback.bin"
    readback.write_bytes(firmware.read_bytes())
    power_cycle = tmp_path / "power-cycle-attestation.json"
    power_cycle.write_text('{"synthetic":"operator-observed power cycle"}', encoding="utf-8")
    selector_path = tmp_path / "selector.json"
    selector = {
        "schema": 1,
        "evidence_kind": runner.SEALED_SELECTOR_EVIDENCE_KIND,
        "campaign_id": "campaign-a",
        "run_id": "campaign-a-fast20-pre-p0-r01",
        "board_id": BOARD,
        "image_role": "fast20",
        "sealed_at": "2026-08-29T00:00:00+00:00",
        "frozen_inputs": {
            "files": {
                "profile": _file_evidence(profile),
                "firmware_bin": _file_evidence(firmware),
            },
            "control_profile": {"id": "fast20-v1", "revision": 1},
        },
        "target_flash_readback": _file_evidence(readback),
        "startup": {
            "evidence_kind": "fast20_exact_image_reset_run_identity_v1",
            "autonomous_schedule_timing_proven": False,
            "runtime_gpio_sequence_proven": False,
        },
        "operator_attestations": {
            "power_cycle_snapshot": _file_evidence(power_cycle),
        },
    }
    selector_path.write_text(json.dumps(selector), encoding="utf-8")

    p0_manifests = _write_p0_source_manifests(
        tmp_path,
        profile_contract_sha256=profile_contract_sha256,
        firmware_sha256=runner.sha256_path(firmware),
    )

    fixture_path = tmp_path / "fixture.json"
    fixture = {
        "schema": 1,
        "fixture_kind": runner.FIXTURE_KIND,
        "campaign_id": "campaign-a",
        "fixture_id": "fixture-a",
        "p0_legacy_fixture_id": "legacy-fixture-a",
        "board_id": BOARD,
        "pluto_serial": SERIAL,
        "topology_token": runner.TOPOLOGY_TOKEN,
        "no_antennas": True,
        "tx1_path": "matched_conducted_full_fixture",
        "tx2_state": "50ohm_terminated",
        "rx1_state": "protected_conducted_reference",
        "rx2_state": "selector_common_full_fixture",
        "selector_mode": "fast20",
        "component_ids": ["pluto", "two-way", "eight-way", "selector"],
        "connection_ids": ["tx1-two-way", "two-way-eight-way", "selector-rx2"],
    }
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    setup_path = tmp_path / "setup.json"
    setup = {
        "schema": 1,
        "attestation_kind": runner.SETUP_KIND,
        "attestation_id": f"setup-{run_id}",
        "run_id": run_id,
        "campaign_id": "campaign-a",
        "board_id": BOARD,
        "pluto_serial": SERIAL,
        "fixture_manifest_sha256": runner.sha256_path(fixture_path),
        "setup_evidence": _file_evidence(photograph),
        "no_component_or_connection_movement": True,
        "selector_flash_evidence_sha256": runner.sha256_path(selector_path),
        "p0_source_manifest_sha256s": [runner.sha256_path(path) for path in p0_manifests],
    }
    setup_path.write_text(json.dumps(setup), encoding="utf-8")

    return fixture_path, setup_path, selector_path, p0_manifests


def _contract(tmp_path: Path, run_id: str = RUN) -> dict[str, Any]:
    files = _write_bound_evidence(tmp_path, run_id)
    fixture = runner._fixture_evidence_from_files(
        *files,
        run_id=run_id,
        board_id=BOARD,
        serial=SERIAL,
        derivation_source_commit=SOURCE_COMMIT,
    )
    contract = cast(
        dict[str, Any],
        runner._build_plan_contract(
            run_id=run_id,
            board_id=BOARD,
            serial=SERIAL,
            uri=URI,
            source_commit=SOURCE_COMMIT,
            dependency_attestation=_dependency_attestation(),
            native_attestation=_native_attestation(),
            fixture_evidence=fixture,
        ),
    )
    run_root = tmp_path / "run-state" / run_id
    capture_root = tmp_path / "captures" / run_id
    contract["storage"] = {
        **contract["storage"],
        "run_root": str(run_root),
        "run_capture_root": str(capture_root),
        "estimated_raw_iq_bytes": runner.TOTAL_SAMPLES * 8,
    }
    return contract


def _settings() -> RadioSettings:
    return RadioSettings(
        center_frequency_hz=runner.CENTER_FREQUENCY_HZ,
        sample_rate_hz=runner.SAMPLE_RATE_HZ,
        bandwidth_hz=runner.BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=runner.RECEIVER_GAIN_DB,
        channels=(0, 1),
    )


def _blocks(
    *,
    stream_id: int = 12345,
    sequence_gap: bool = False,
    clipped: bool = False,
    count: int | None = None,
    seed: int = 20260829,
    tone_amplitude: float = 0.0,
) -> list[SampleBlockV2]:
    flags = int(MetadataFlags.SAMPLE_SEQUENCE_VALID | MetadataFlags.HARDWARE_SAMPLE_COUNTER_VALID)
    rng = np.random.default_rng(seed)
    blocks = []
    first = 8_000_000
    duration_ns = round(runner.SAMPLES_PER_FRAME / runner.SAMPLE_RATE_HZ * 1e9)
    block_count = runner.FRAME_COUNT if count is None else count
    for index in range(block_count):
        samples = (
            rng.standard_normal((2, runner.SAMPLES_PER_FRAME))
            + 1j * rng.standard_normal((2, runner.SAMPLES_PER_FRAME))
        ).astype(np.complex64)
        if tone_amplitude:
            sample_indices = index * runner.SAMPLES_PER_FRAME + np.arange(
                runner.SAMPLES_PER_FRAME, dtype=np.float64
            )
            samples[0] += np.asarray(
                tone_amplitude
                * np.exp(2j * np.pi * 100_000.0 * sample_indices / runner.SAMPLE_RATE_HZ),
                dtype=np.complex64,
            )
        if clipped and index == 1:
            samples[0, 0] = 2047.0 + 0.0j
        buffer_sequence = index + (1 if sequence_gap and index >= 1 else 0)
        realtime_start = 1_800_000_000_000_000_000 + index * duration_ns
        monotonic_start = 2_000_000_000_000 + index * duration_ns
        blocks.append(
            SampleBlockV2(
                utc_ns=realtime_start + duration_ns // 2,
                samples=samples,
                stream_id=stream_id,
                buffer_sequence=buffer_sequence,
                first_sample_sequence=first + index * runner.SAMPLES_PER_FRAME,
                metadata_flags=flags,
                metadata_abi=2,
                sample_time_realtime_start_ns=realtime_start,
                sample_time_realtime_end_ns=realtime_start + duration_ns,
                sample_time_monotonic_start_ns=monotonic_start,
                sample_time_monotonic_end_ns=monotonic_start + duration_ns,
                sample_time_uncertainty_ns=1_000,
            )
        )
    return blocks


def _capture_boundary(blocks: list[SampleBlockV2], calls: list[str] | None = None) -> Any:
    def capture(contract: dict[str, Any], *, block_consumer: Any) -> Any:
        if calls is not None:
            calls.append("capture")
        for block in blocks:
            block_consumer(block)
        return runner.MutedContinuousCapture(
            identity=RadioIdentity(
                radio_id=SERIAL,
                serial=SERIAL,
                uri=URI,
                transport=Transport.IIO_USB,
                model="Pluto+",
                firmware_version=runner.V7_FIRMWARE_VERSION,
            ),
            settings=_settings(),
            frames=tuple(runner._frame_proof(block) for block in blocks),
            kernel_buffers=runner.KERNEL_BUFFERS,
            receive_only_api=True,
            tx_source_active=False,
        )

    return capture


def _passing_mute(calls: list[str] | None = None) -> Any:
    def mute(serial: str, uri: str, purpose: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(purpose)
        return {
            "schema": 1,
            "evidence_kind": "exact_serial_tx_mute_and_full_dds_readback",
            "purpose": purpose,
            "status": "passed",
            "serial": serial,
            "uri": uri,
            "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
            "dds_raw_readback": [0.0] * 8,
            "dds_scale_readback": [0.0] * 8,
            "dds_enabled_readback": [False] * 8,
            "error": None,
        }

    return mute


def _passing_identity() -> Any:
    return lambda serial, uri: {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed",
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": uri,
        "exact_uri_match": True,
        "scan_mutates_radio_state": False,
        "error": None,
    }


def _passing_evidence() -> Any:
    return lambda expected: {
        "schema": 1,
        "evidence_kind": "5g8_p1_fixture_preflight",
        "status": "passed",
        "fixture_evidence": dict(expected),
        "fixture_evidence_sha256": runner.canonical_json_sha256(expected),
        "error": None,
    }


def _confirmation(contract: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runner._confirmation(
            contract,
            no_antennas=True,
            tx1_untouched=True,
            tx2_terminated=True,
            rx1_protected=True,
            no_movement=True,
            sealed_fast20_unchanged=True,
            topology_token=runner.TOPOLOGY_TOKEN,
        ),
    )


def _prepared(tmp_path: Path) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    contract = _contract(tmp_path)
    plan_path = Path(contract["storage"]["run_root"]) / runner.PLAN_FILENAME
    manifest_path = plan_path.parent / runner.MANIFEST_FILENAME
    envelope, manifest = runner._prepare_plan_only(
        plan_path=plan_path,
        manifest_path=manifest_path,
        contract=contract,
    )
    runner._persist_manifest(manifest_path, manifest)
    return contract, plan_path, envelope, manifest_path


def _execute(
    tmp_path: Path,
    *,
    blocks: list[SampleBlockV2] | None = None,
    mute_boundary: Any | None = None,
    identity_boundary: Any | None = None,
    runtime_boundary: Any | None = None,
    evidence_boundary: Any | None = None,
    capture_calls: list[str] | None = None,
) -> tuple[dict[str, Any], Path]:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    manifest = runner._read_json(manifest_path, "manifest")
    runner._execute_run(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=_confirmation(contract),
        capture_boundary=_capture_boundary(blocks or _blocks(), capture_calls),
        mute_boundary=mute_boundary or _passing_mute(),
        identity_boundary=identity_boundary or _passing_identity(),
        runtime_boundary=runtime_boundary or (lambda: _native_attestation()),
        evidence_boundary=evidence_boundary or _passing_evidence(),
    )
    return manifest, manifest_path


def test_plan_freezes_exact_one_stream_contract_and_all_identities(tmp_path: Path) -> None:
    contract = _contract(tmp_path)

    assert len(contract["conditions"]) == 1
    assert contract["configuration"]["one_stream_per_run_id"] is True
    assert contract["configuration"]["cohort_run_count"] == 5
    assert contract["configuration"]["duration_s"] == pytest.approx(0.003072)
    assert contract["configuration"]["tx_hardware_gain_db_required"] == [-80.0, -80.0]
    assert contract["configuration"]["dds_raw_required"] == [0.0] * 8
    assert contract["configuration"]["dds_scale_required"] == [0.0] * 8
    assert contract["source"]["smateway_commit"] == SOURCE_COMMIT
    assert len(contract["source"]["native_libiio_runtime_attestation_sha256"]) == 64
    assert len(contract["fixture_evidence_sha256"]) == 64
    assert contract["analysis_contract"]["transfer_phasor_forbidden"] is True
    assert contract["storage"]["pluto_onboard_storage_used"] is False
    confirmations = contract["operator_confirmations_required"]
    assert confirmations["sealed_fast20_image_unchanged_since_p0"] is True
    assert confirmations["post_cycle_p0_rf_schedule_proof_bound"] is True
    assert "fast20_schedule_active" not in confirmations


def test_fixture_binding_rejects_changed_setup_evidence(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(setup.read_text(encoding="utf-8"))
    photograph = Path(document["setup_evidence"]["path"])
    photograph.write_bytes(b"changed after attestation")

    with pytest.raises(runner.MutedControlError, match="file bytes differ"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_rejects_unresolved_placeholder_recursively(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["component_ids"][0] = "asset-REPLACE_NESTED_COMPONENT_ID-suffix"
    fixture.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match=r"fixture manifest.*\$\.component_ids\[0\]"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_setup_rejects_unresolved_placeholder_recursively(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(setup.read_text(encoding="utf-8"))
    document["setup_evidence"]["path"] = "/local/REPLACE_SETUP_PHOTO.png"
    setup.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match=r"setup attestation.*setup_evidence.path"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_setup_rejects_unresolved_placeholder_key(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(setup.read_text(encoding="utf-8"))
    document["setup_evidence"]["REPLACE_UNKNOWN_KEY"] = "value"
    setup.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match="unresolved placeholder key"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_accepts_recursively_validated_sealed_fast20_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    sealed = cast(dict[str, Any], json.loads(selector.read_text(encoding="utf-8")))
    calls: list[dict[str, Any]] = []

    def validate(path: Path, **kwargs: Any) -> dict[str, Any]:
        assert path == selector.resolve()
        calls.append(kwargs)
        return sealed

    monkeypatch.setattr(runner, "validate_sealed_selector_evidence", validate)
    evidence = runner._fixture_evidence_from_files(
        fixture,
        setup,
        selector,
        manifests,
        run_id=RUN,
        board_id=BOARD,
        serial=SERIAL,
        derivation_source_commit=SOURCE_COMMIT,
    )

    assert evidence["selector_evidence_format"] == "sealed_selector_flash_attestation_v1"
    assert evidence["fast20_firmware_bin"] == sealed["frozen_inputs"]["files"]["firmware_bin"]
    proof = evidence["p0_post_cycle_schedule_proof"]
    assert proof["selector_flash_attestation_proves_schedule_timing"] is False
    assert proof["schedule_timing_proven_by"].endswith("p0_rf_artifacts")
    assert calls == [
        {
            "expected_sha256": runner.sha256_path(selector),
            "expected_campaign_id": "campaign-a",
            "expected_run_id": "campaign-a-fast20-pre-p0-r01",
            "expected_board_id": BOARD,
            "expected_image_role": "fast20",
        }
    ]


def test_fixture_rejects_failed_recursive_fast20_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(selector.read_text(encoding="utf-8"))
    document.update(
        {
            "evidence_kind": runner.SEALED_SELECTOR_EVIDENCE_KIND,
            "run_id": "fast20-flash-r01",
        }
    )
    selector.write_text(json.dumps(document), encoding="utf-8")
    setup_document = json.loads(setup.read_text(encoding="utf-8"))
    setup_document["selector_flash_evidence_sha256"] = runner.sha256_path(selector)
    setup.write_text(json.dumps(setup_document), encoding="utf-8")

    def reject(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise runner.SelectorFlashError("target readback changed")

    monkeypatch.setattr(runner, "validate_sealed_selector_evidence", reject)
    with pytest.raises(runner.MutedControlError, match="target readback changed"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_rejects_legacy_unsealed_fast20_claim_before_recursive_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    document = json.loads(selector.read_text(encoding="utf-8"))
    document["evidence_kind"] = "fast20_live_selector_schedule"
    selector.write_text(json.dumps(document), encoding="utf-8")
    setup_document = json.loads(setup.read_text(encoding="utf-8"))
    setup_document["selector_flash_evidence_sha256"] = runner.sha256_path(selector)
    setup.write_text(json.dumps(setup_document), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "validate_sealed_selector_evidence",
        lambda *_args, **_kwargs: calls.append("called"),
    )

    with pytest.raises(runner.MutedControlError, match="recursively sealed"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )

    assert calls == []


def test_fixture_rejects_p0_without_decoded_post_cycle_rf_timing(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    analysis_path = Path(manifest["attempts"][0]["quality_result"]["analysis_path"])
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis["transfer"]["schedule_alignment"]["decoded_timing"]["strict_frame_count"] = 0
    analysis_path.write_text(json.dumps(analysis), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match="exact Fast20 5.8-GHz stream"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_rejects_stored_timing_that_differs_from_raw_iq(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    mismatched_timing = {"strict_frame_count": 24, "rejected_marker_count": 0}
    monkeypatch.setattr(
        runner,
        "_recompute_p0_schedule_timing",
        lambda *_args, **_kwargs: {
            "schema": 1,
            "evidence_kind": "p0_raw_iq_fast20_schedule_reanalysis_v1",
            "schedule_timing": mismatched_timing,
            "schedule_timing_sha256": runner.canonical_json_sha256(mismatched_timing),
            "complete_frame_count": 24,
            "rejected_marker_count": 0,
            "continuity_verified": True,
            "state_order": [f"ANT{state}" for state in range(1, 9)],
        },
    )

    with pytest.raises(runner.MutedControlError, match="independent raw-IQ reanalysis"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_rejects_p0_from_a_different_frozen_source_revision(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    manifest["runner_source_commit"] = "f" * 40
    manifests[0].write_text(json.dumps(manifest), encoding="utf-8")
    setup_document = json.loads(setup.read_text(encoding="utf-8"))
    setup_document["p0_source_manifest_sha256s"] = [runner.sha256_path(path) for path in manifests]
    setup.write_text(json.dumps(setup_document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match="legacy admission contract"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_setup_rejects_the_old_no_power_cycle_claim(tmp_path: Path) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    setup_document = json.loads(setup.read_text(encoding="utf-8"))
    setup_document["selector_not_power_cycled_since_p0"] = True
    setup.write_text(json.dumps(setup_document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match="exact P1 run/fixture"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_fixture_rejects_p0_artifacts_created_before_selector_power_cycle_seal(
    tmp_path: Path,
) -> None:
    fixture, setup, selector, manifests = _write_bound_evidence(tmp_path)
    selector_document = json.loads(selector.read_text(encoding="utf-8"))
    selector_document["sealed_at"] = "2099-01-01T00:00:00+00:00"
    selector.write_text(json.dumps(selector_document), encoding="utf-8")
    setup_document = json.loads(setup.read_text(encoding="utf-8"))
    setup_document["selector_flash_evidence_sha256"] = runner.sha256_path(selector)
    setup.write_text(json.dumps(setup_document), encoding="utf-8")

    with pytest.raises(runner.MutedControlError, match="not created after"):
        runner._fixture_evidence_from_files(
            fixture,
            setup,
            selector,
            manifests,
            run_id=RUN,
            board_id=BOARD,
            serial=SERIAL,
            derivation_source_commit=SOURCE_COMMIT,
        )


def test_local_storage_rejects_device_different_from_rpi_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_stat = os.stat

    def different_device(path: os.PathLike[str] | str, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == Path("/home/pi"):
            return SimpleNamespace(st_dev=100)
        result = real_stat(path, *args, **kwargs)
        if Path(path) == tmp_path:
            fields = list(result)
            fields[2] = 200
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(runner.os, "stat", different_device)
    with pytest.raises(runner.MutedControlError, match="device differs"):
        runner._assert_local_rpi_storage(tmp_path / "captures")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tx_hardware_gain_db_by_channel", [-79.0, -80.0]),
        ("dds_raw_readback", [0.0] * 7 + [1.0]),
        ("dds_scale_readback", [0.0] * 7 + [0.1]),
        ("dds_enabled_readback", [False] * 7 + [True]),
    ),
)
def test_exact_mute_rejects_any_nonzero_or_nonminus80_readback(
    field: str, value: list[Any]
) -> None:
    state = {
        "tx_hardware_gain_db_by_channel": [-80.0, -80.0],
        "dds_raw_readback": [0.0] * 8,
        "dds_scale_readback": [0.0] * 8,
        "dds_enabled_readback": [False] * 8,
    }
    state[field] = value
    assert runner._exact_mute_state_passed(state) is False


def test_success_persists_one_abi2_stream_and_three_exact_mutes(tmp_path: Path) -> None:
    mute_calls: list[str] = []
    manifest, manifest_path = _execute(
        tmp_path,
        mute_boundary=_passing_mute(mute_calls),
    )

    assert mute_calls == ["pre_capture", "post_capture", "final"]
    assert manifest["status"] == "complete"
    result = manifest["attempt"]["result"]
    assert result["stream_id"] == 12345
    assert result["metadata_abi"] == 2
    assert result["sample_count"] == runner.TOTAL_SAMPLES
    record = runner._read_json(Path(result["record_path"]), "record")
    assert record["accepted"] is True
    assert record["capture"]["tx_source_active"] is False
    assert record["analysis"]["transfer_phase_defined"] is False
    assert record["safety"]["final_exact_mute"]["status"] == "passed"
    assert runner.sha256_path(Path(result["record_path"])) == result["record_sha256"]
    assert not runner._tombstone_path(manifest_path).exists()


def test_cohort_loader_requires_complete_untampered_manifest_bound_artifact(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = _execute(tmp_path)

    record = cohort_analyzer.load_completed_record(
        manifest_path,
        current_dependency_attestation=_dependency_attestation(),
        current_native_attestation=_native_attestation(),
    )
    assert record["run_id"] == RUN
    assert record["accepted"] is True

    changed_dependency = _dependency_attestation()
    changed_dependency["commit"] = "f" * 40
    changed_dependency["head"] = "f" * 40
    with pytest.raises(
        cohort_analyzer.MutedCohortArtifactError,
        match="current clean analyzer source",
    ):
        cohort_analyzer.load_completed_record(
            manifest_path,
            current_dependency_attestation=changed_dependency,
            current_native_attestation=_native_attestation(),
        )

    changed_native = _native_attestation()
    changed_native["version"] = {"major": 0, "minor": 26, "git_tag": "different"}
    with pytest.raises(
        cohort_analyzer.MutedCohortArtifactError,
        match="current analyzer runtime",
    ):
        cohort_analyzer.load_completed_record(
            manifest_path,
            current_dependency_attestation=_dependency_attestation(),
            current_native_attestation=changed_native,
        )

    data_path = Path(record["artifact_evidence"]["data_path"])
    data_path.write_bytes(data_path.read_bytes() + b"tampered")
    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="SHA-256 differs"):
        cohort_analyzer.load_completed_record(manifest_path)

    assert manifest["status"] == "complete"


def test_cohort_loader_rejects_failure_tombstone_even_with_complete_manifest(
    tmp_path: Path,
) -> None:
    _manifest, manifest_path = _execute(tmp_path)
    runner.foundation._write_immutable_json(
        runner._tombstone_path(manifest_path),
        {"synthetic": "failure marker"},
    )

    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="tombstone"):
        cohort_analyzer.load_completed_record(manifest_path)


def test_cohort_loader_recomputes_psd_instead_of_trusting_self_consistent_record(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = _execute(tmp_path)
    result = manifest["attempt"]["result"]
    record_path = Path(result["record_path"])
    record = runner._read_json(record_path, "record")
    record["analysis"]["sample_count"] += 1
    runner.write_json_atomic(record_path, record)
    result["record_sha256"] = runner.sha256_path(record_path)
    runner._persist_manifest(manifest_path, manifest)

    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="raw recomputation"):
        cohort_analyzer.load_completed_record(manifest_path)


def test_capture_root_symlink_is_rejected_before_capture(tmp_path: Path) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    capture_root = Path(contract["storage"]["run_capture_root"])
    capture_root.parent.mkdir(parents=True)
    redirect = tmp_path / "redirected-capture-root"
    redirect.mkdir()
    capture_root.symlink_to(redirect, target_is_directory=True)
    manifest = runner._read_json(manifest_path, "manifest")
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="capture root"):
        runner._execute_run(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    assert capture_calls == []
    assert list(redirect.iterdir()) == []


def test_prepared_run_cannot_be_replayed_through_a_cloned_symlink_ancestor(
    tmp_path: Path,
) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    manifest = runner._read_json(manifest_path, "manifest")
    original_run_root = manifest_path.parent
    preserved = tmp_path / "preserved-prepared-run"
    clone = tmp_path / "cloned-prepared-run"
    original_run_root.rename(preserved)
    shutil.copytree(preserved, clone)
    original_run_root.symlink_to(clone, target_is_directory=True)
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="symlink|no-symlink"):
        runner._execute_run(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    assert capture_calls == []


def test_prepared_run_clone_on_a_real_directory_fails_external_inode_reservation(
    tmp_path: Path,
) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    manifest = runner._read_json(manifest_path, "manifest")
    original_run_root = manifest_path.parent
    preserved = tmp_path / "preserved-prepared-run"
    original_run_root.rename(preserved)
    shutil.copytree(preserved, original_run_root)
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="reservation identity"):
        runner._execute_run(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    assert capture_calls == []


def test_cohort_input_and_output_reject_symlink_ancestors(tmp_path: Path) -> None:
    _manifest, manifest_path = _execute(tmp_path)
    real_parent = manifest_path.parent.parent
    redirected = tmp_path / "redirected-state"
    real_parent.rename(redirected)
    real_parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="no-symlink"):
        cohort_analyzer.load_completed_record(manifest_path)

    output_target = tmp_path / "real-output"
    output_target.mkdir()
    output_parent = tmp_path / "output-link"
    output_parent.symlink_to(output_target, target_is_directory=True)
    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="no-symlink"):
        cohort_analyzer._admit_new_output(output_parent / "cohort.json")


def test_cohort_output_rejects_a_nonlocal_filesystem_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stat = os.stat

    def different_device(path: os.PathLike[str] | str, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == Path("/home/pi"):
            return SimpleNamespace(st_dev=100)
        result = real_stat(path, *args, **kwargs)
        if Path(path) == tmp_path:
            fields = list(result)
            fields[2] = 200
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(runner.os, "stat", different_device)
    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="local no-symlink"):
        cohort_analyzer._admit_new_output(tmp_path / "cohort.json")


def test_wrong_usb_identity_burns_run_without_capture_and_still_final_mutes(
    tmp_path: Path,
) -> None:
    mute_calls: list[str] = []
    capture_calls: list[str] = []

    def wrong_identity(serial: str, uri: str) -> dict[str, Any]:
        return {
            "schema": 1,
            "evidence_kind": "read_only_current_usb_uri_resolution",
            "status": "failed",
            "serial": serial,
            "requested_uri": uri,
            "resolved_uri": "usb:9.9.9",
            "exact_uri_match": False,
            "scan_mutates_radio_state": False,
            "error": None,
        }

    with pytest.raises(runner.MutedControlError, match="USB identity"):
        _execute(
            tmp_path,
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=wrong_identity,
            capture_calls=capture_calls,
        )

    manifest_path = tmp_path / "run-state" / RUN / runner.MANIFEST_FILENAME
    manifest = runner._read_json(manifest_path, "manifest")
    tombstone = runner._tombstone_path(manifest_path)
    assert manifest["status"] == "failed"
    assert capture_calls == []
    assert mute_calls == ["final"]
    assert tombstone.is_file()
    assert stat.S_IMODE(tombstone.stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("failure_kind", "message"),
    (
        ("sequence_gap", "buffer_sequence"),
        ("partial", "10-second"),
        ("clipping", "headroom"),
    ),
)
def test_continuity_partial_and_clipping_failures_quarantine_and_burn_run(
    tmp_path: Path, failure_kind: str, message: str
) -> None:
    blocks = {
        "sequence_gap": lambda: _blocks(sequence_gap=True),
        "partial": lambda: _blocks(count=2),
        "clipping": lambda: _blocks(clipped=True),
    }[failure_kind]()
    with pytest.raises(runner.MutedControlError, match=message):
        _execute(tmp_path, blocks=blocks)

    manifest_path = tmp_path / "run-state" / RUN / runner.MANIFEST_FILENAME
    manifest = runner._read_json(manifest_path, "manifest")
    assert manifest["status"] == "failed"
    assert manifest["attempt"]["quarantine"] is not None
    assert runner._tombstone_path(manifest_path).is_file()


def test_nonzero_pre_capture_dds_readback_prevents_capture(tmp_path: Path) -> None:
    capture_calls: list[str] = []

    def mute(serial: str, uri: str, purpose: str) -> dict[str, Any]:
        result = cast(dict[str, Any], _passing_mute()(serial, uri, purpose))
        if purpose == "pre_capture":
            result["dds_raw_readback"][-1] = 1.0
        return result

    with pytest.raises(runner.MutedControlError, match="pre-capture"):
        _execute(tmp_path, mute_boundary=mute, capture_calls=capture_calls)

    assert capture_calls == []


def test_final_mute_failure_rejects_artifact_and_burns_run(tmp_path: Path) -> None:
    def mute(serial: str, uri: str, purpose: str) -> dict[str, Any]:
        result = cast(dict[str, Any], _passing_mute()(serial, uri, purpose))
        if purpose == "final":
            result["status"] = "failed"
            result["error"] = {"type": "Synthetic", "message": "final mute failed"}
        return result

    with pytest.raises(runner.MutedControlError, match="final exact"):
        _execute(tmp_path, mute_boundary=mute)

    manifest_path = tmp_path / "run-state" / RUN / runner.MANIFEST_FILENAME
    manifest = runner._read_json(manifest_path, "manifest")
    assert manifest["status"] == "failed"
    assert manifest["attempt"]["status"] == "failed"
    quarantine = manifest["attempt"]["quarantine"]
    assert quarantine["accepted"] is False
    quarantine_root = Path(quarantine["path"])
    assert quarantine_root.parent.name == ".failed"
    record_path = quarantine_root / runner.RECORD_FILENAME
    record = runner._read_json(record_path, "record")
    assert record["accepted"] is False
    assert record["safety"]["final_exact_mute"] is None
    assert runner._tombstone_path(manifest_path).is_file()


def test_stale_running_manifest_is_externally_burned_without_hardware_api_access(
    tmp_path: Path,
) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    manifest = runner._read_json(manifest_path, "manifest")
    manifest["status"] = "running"
    manifest["attempt"] = {
        "attempt_id": 1,
        "status": "running",
        "result": None,
        "quarantine": None,
    }
    runner._persist_manifest(manifest_path, manifest)
    mute_calls: list[str] = []
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="untouched prepared execution"):
        runner._execute_run(
            manifest,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    persisted = runner._read_json(manifest_path, "manifest")
    assert capture_calls == []
    assert mute_calls == []
    assert persisted["status"] == "running"
    assert persisted["execution_started"] is not None
    burn = runner._execution_burn_path(manifest_path, run_id=RUN)
    assert burn.is_file()
    assert burn.stat().st_mode & 0o222 == 0


def test_failed_tombstone_survives_manifest_deletion_and_forbids_plan_reuse(
    tmp_path: Path,
) -> None:
    with pytest.raises(runner.MutedControlError):
        _execute(tmp_path, blocks=_blocks(count=2))
    manifest_path = tmp_path / "run-state" / RUN / runner.MANIFEST_FILENAME
    tombstone = runner._tombstone_path(manifest_path)
    os.chmod(manifest_path, 0o600)
    manifest_path.unlink()
    evidence_root = tmp_path / "new-evidence"
    evidence_root.mkdir()
    contract = _contract(evidence_root)
    contract["storage"]["run_root"] = str(manifest_path.parent)
    contract["storage"]["run_capture_root"] = str(tmp_path / "unused-captures")

    with pytest.raises(runner.MutedControlError, match="cannot be planned"):
        runner._prepare_plan_only(
            plan_path=manifest_path.parent / runner.PLAN_FILENAME,
            manifest_path=manifest_path,
            contract=contract,
        )
    assert tombstone.is_file()


def test_success_manifest_rollback_cannot_bypass_external_execution_burn(
    tmp_path: Path,
) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    prepared = runner._read_json(manifest_path, "prepared manifest")
    manifest = json.loads(json.dumps(prepared))
    runner._execute_run(
        manifest,
        manifest_path,
        envelope=envelope,
        plan_path=plan_path,
        confirmation=_confirmation(contract),
        capture_boundary=_capture_boundary(_blocks()),
        mute_boundary=_passing_mute(),
        identity_boundary=_passing_identity(),
        runtime_boundary=lambda: _native_attestation(),
        evidence_boundary=_passing_evidence(),
    )
    runner.write_json_atomic(manifest_path, prepared)
    rolled_back = runner._read_json(manifest_path, "rolled-back manifest")
    mute_calls: list[str] = []
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="already executed"):
        runner._execute_run(
            rolled_back,
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    assert mute_calls == []
    assert capture_calls == []
    assert runner._execution_burn_path(manifest_path, run_id=RUN).is_file()


def test_deleted_failure_tombstone_and_manifest_rollback_cannot_retry(
    tmp_path: Path,
) -> None:
    contract, plan_path, envelope, manifest_path = _prepared(tmp_path)
    prepared = runner._read_json(manifest_path, "prepared manifest")
    with pytest.raises(runner.MutedControlError):
        runner._execute_run(
            json.loads(json.dumps(prepared)),
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(count=2)),
            mute_boundary=_passing_mute(),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )
    tombstone = runner._tombstone_path(manifest_path)
    tombstone.chmod(0o600)
    tombstone.unlink()
    runner.write_json_atomic(manifest_path, prepared)
    mute_calls: list[str] = []
    capture_calls: list[str] = []

    with pytest.raises(runner.MutedControlError, match="already executed"):
        runner._execute_run(
            runner._read_json(manifest_path, "rolled-back failed manifest"),
            manifest_path,
            envelope=envelope,
            plan_path=plan_path,
            confirmation=_confirmation(contract),
            capture_boundary=_capture_boundary(_blocks(), capture_calls),
            mute_boundary=_passing_mute(mute_calls),
            identity_boundary=_passing_identity(),
            runtime_boundary=lambda: _native_attestation(),
            evidence_boundary=_passing_evidence(),
        )

    assert mute_calls == []
    assert capture_calls == []
    assert runner._execution_burn_path(manifest_path, run_id=RUN).is_file()


def test_cohort_rejects_extra_sibling_artifact_in_one_stream_capture(tmp_path: Path) -> None:
    manifest, manifest_path = _execute(tmp_path)
    capture_root = Path(manifest["immutable_plan"]["path"])
    plan = runner._read_json(capture_root, "plan")
    run_capture_root = Path(plan["plan_contract"]["storage"]["run_capture_root"])
    (run_capture_root / "extra-artifact").mkdir()

    with pytest.raises(cohort_analyzer.MutedCohortArtifactError, match="extra sibling"):
        cohort_analyzer.load_completed_record(
            manifest_path,
            current_dependency_attestation=_dependency_attestation(),
            current_native_attestation=_native_attestation(),
        )


def test_final_record_rewrite_rejects_concurrent_artifact_parent_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _manifest_path = _execute(tmp_path)
    result = manifest["attempt"]["result"]
    record_path = Path(result["record_path"])
    record = runner._read_json(record_path, "accepted record")
    artifact_root = record_path.parent
    capture_root = artifact_root.parent
    moved = tmp_path / "artifact-moved-during-finalize"
    redirect = tmp_path / "artifact-rebind-target"
    redirect.mkdir()
    real_replace = os.replace
    attacked = False

    def rebind_after_replace(*args: Any, **kwargs: Any) -> None:
        nonlocal attacked
        real_replace(*args, **kwargs)
        if not attacked:
            attacked = True
            artifact_root.rename(moved)
            artifact_root.symlink_to(redirect, target_is_directory=True)

    monkeypatch.setattr(runner.os, "replace", rebind_after_replace)
    with pytest.raises(runner.MutedControlError, match="rebound|no-symlink|unsafe"):
        runner._finalize_record_safely(
            record_path,
            record,
            capture_root=capture_root,
            artifact_id=str(result["artifact_id"]),
            expected_sha256=str(result["record_sha256"]),
        )

    assert attacked is True


def test_native_or_fixture_preflight_failure_never_calls_capture(tmp_path: Path) -> None:
    capture_calls: list[str] = []
    bad_native = _native_attestation()
    bad_native["library_sha256"] = "f" * 64

    with pytest.raises(runner.MutedControlError, match="native libiio"):
        _execute(
            tmp_path,
            runtime_boundary=lambda: bad_native,
            capture_calls=capture_calls,
        )
    assert capture_calls == []
