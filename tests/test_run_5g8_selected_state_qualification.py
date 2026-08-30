from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_fixture_v2 import _chain as _production_fixture_chain

from smateway.selected_state_qualification import (
    ANTENNA_STATES,
    EXPECTED_STATES,
    FIXTURE_KIND_V2,
    FULL_CONDUCTED_STAGE,
    STATIC_KIND,
    SelectorEvidenceBinding,
)

FIXTURE_GENERATOR_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/generate_5g8_fixture_manifest.py"
)
FIXTURE_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_5g8_fixture_manifest_for_selected_state_tests",
    FIXTURE_GENERATOR_SCRIPT,
)
assert FIXTURE_GENERATOR_SPEC is not None and FIXTURE_GENERATOR_SPEC.loader is not None
fixture_generator = importlib.util.module_from_spec(FIXTURE_GENERATOR_SPEC)
sys.modules[FIXTURE_GENERATOR_SPEC.name] = fixture_generator
FIXTURE_GENERATOR_SPEC.loader.exec_module(fixture_generator)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_selected_state_qualification.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_selected_state_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

CAMPAIGN = "5p8-debug-r1"
BOARD = "stm32c011-4c0055000950313950363920"
SERIAL = "104000b29905000e17000800065934759d"
URI = "usb:1.2.3"
SOURCE = "a" * 40
DEPENDENCY = "b" * 40
NATIVE_DOCUMENT = {"schema": 1, "library": "/usr/local/lib/libiio.so.0"}
NATIVE = hashlib.sha256(
    json.dumps(NATIVE_DOCUMENT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _write_json(path: Path, document: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _file(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def _production_fixture_inputs(tmp_path: Path) -> tuple[Path, Path]:
    chain = _production_fixture_chain(
        tmp_path / "production-fixture-chain",
        run_prefix="selected-state-fixture",
    )
    stage_e = chain[FULL_CONDUCTED_STAGE]
    fixture_path = Path(stage_e["manifest"])
    setup_path = Path(stage_e["evidence"]["source_files"]["setup_attestation"]["path"])
    return fixture_path, setup_path


def _fixture_manifest(tmp_path: Path) -> Path:
    fixture_path, _ = _production_fixture_inputs(tmp_path)
    return fixture_path


def _selector_evidence(tmp_path: Path, role: str) -> tuple[Path, str]:
    path = tmp_path / f"selector-{role}-evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{role} sealed evidence\n", encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _selector_loader(
    path: Path,
    *,
    expected_sha256: str,
    campaign_id: str,
    run_id: str,
    board_id: str,
    image_role: str,
) -> SelectorEvidenceBinding:
    return SelectorEvidenceBinding(
        path=str(path.expanduser().absolute()),
        sha256=expected_sha256,
        campaign_id=campaign_id,
        run_id=run_id,
        board_id=board_id,
        image_role=image_role,  # type: ignore[arg-type]
        firmware_bin_sha256=_hash(f"firmware-{image_role}"),
        profile_contract_sha256=_hash("fast20-profile"),
        startup_evidence_sha256=_hash(f"startup-{image_role}"),
    )


def _runtime() -> dict[str, Any]:
    return {
        "source": {"schema": 1, "commit": SOURCE, "files": []},
        "dependency": {"schema": 1, "commit": DEPENDENCY, "files": []},
        "native": NATIVE_DOCUMENT,
        "source_commit": SOURCE,
        "dependency_commit": DEPENDENCY,
        "native_attestation_sha256": NATIVE,
    }


@pytest.mark.parametrize("image_role", ("bench", "fast20"))
def test_generated_stage_e_setup_draft_is_accepted_by_selected_state_validator(
    tmp_path: Path, image_role: str
) -> None:
    fixture_path = _fixture_manifest(tmp_path)
    raw_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    run_id = f"q-{image_role}-setup-r01"
    fixture = fixture_generator.ValidatedFixtureManifest(
        path=fixture_path,
        file_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        document=raw_fixture,
        shared_fixture_sha256=_hash("shared-fixture"),
        stage_delta_sha256=_hash("stage-e-delta"),
        component_ids=["component-a", "component-b"],
        connection_ids=["connection-a"],
    )
    setup = fixture_generator.generate_setup_attestation_draft(fixture, run_id=run_id)
    selector_path, selector_sha = _selector_evidence(tmp_path, image_role)
    selector = _selector_loader(
        selector_path,
        expected_sha256=selector_sha,
        campaign_id=CAMPAIGN,
        run_id=f"selector-{image_role}-r01",
        board_id=BOARD,
        image_role=image_role,
    )
    setup_evidence = tmp_path / f"q-{image_role}-setup.png"
    setup_evidence.write_bytes(f"observed-{image_role}-setup".encode())
    setup.update(
        {
            "attestation_id": f"q-{image_role}-setup-observation-r01",
            "created_at": "2026-08-30T12:00:00+00:00",
            "selector_flash_evidence": {
                "path": selector.path,
                "sha256": selector.sha256,
                "run_id": selector.run_id,
            },
            "setup_evidence_path": str(setup_evidence),
            "setup_evidence_sha256": hashlib.sha256(setup_evidence.read_bytes()).hexdigest(),
        }
    )
    setup_path = _write_json(tmp_path / f"q-{image_role}-setup.json", setup)
    normalized = runner.leakage_runner._normalize_setup_attestation(
        setup_path,
        run_id=run_id,
        campaign_id=CAMPAIGN,
        comparable_fixture_group_id="full-simultaneous-r0",
        stage=FULL_CONDUCTED_STAGE,
        fixture_manifest_sha256=fixture.file_sha256,
        shared_fixture_sha256=fixture.shared_fixture_sha256,
        stage_delta_sha256=fixture.stage_delta_sha256,
        component_ids=fixture.component_ids,
        connection_ids=fixture.connection_ids,
        selector_flash_evidence=runner._selector_flash_binding(selector),
    )
    assert normalized["run_id"] == run_id
    assert normalized["selector_flash_evidence"]["image_role"] == image_role


def _device_identity(tmp_path: Path) -> Path:
    sysfs_path = tmp_path / "sysfs" / "1-2.3"
    sysfs_path.mkdir(parents=True, exist_ok=True)
    sysfs_values = {
        "serial": SERIAL,
        "idVendor": "0456",
        "idProduct": "b673",
        "manufacturer": "Analog Devices Inc.",
        "product": "PlutoSDR",
    }
    for name, value in sysfs_values.items():
        (sysfs_path / name).write_text(value + "\n", encoding="utf-8")
    resolution = {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed",
        "serial": SERIAL,
        "requested_uri": URI,
        "resolved_uri": URI,
        "exact_uri_match": True,
        "sysfs_path": str(sysfs_path),
        "scan_mutates_radio_state": False,
        "started_at": "2026-08-29T11:59:58+00:00",
        "completed_at": "2026-08-29T11:59:59+00:00",
        "error": None,
    }
    facts = {
        "serial": SERIAL,
        "model": "PlutoSDR",
        "firmware_version": "v0.40-plutoplus-spf-tandem-agc-v7",
        "kernel_version": "6.1.0",
        "context_uri": URI,
        "phy_model": "ad9361",
        "buffer_metadata_abi": 2,
        "rx_scan_channels": ["voltage0", "voltage1", "voltage2", "voltage3"],
    }
    sysfs = {
        "path": str(sysfs_path),
        **sysfs_values,
    }
    observation = {
        "observed_at": "2026-08-29T11:59:59+00:00",
        "serial": SERIAL,
        "usb_uri": URI,
        "read_only_usb_resolution": resolution,
        "iio_context_facts": facts,
        "sysfs_attributes": sysfs,
        "native_libiio_runtime_attestation": NATIVE_DOCUMENT,
        "native_libiio_runtime_attestation_sha256": NATIVE,
    }
    return _write_json(
        tmp_path / "device-identity.json",
        {
            "schema": 2,
            "evidence_kind": runner.DEVICE_IDENTITY_KIND,
            **observation,
            "observation_sha256": runner.canonical_sha256(observation),
            "accepted": True,
        },
    )


def _fixture_loader(
    manifest: Path,
    setup: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
    selector: SelectorEvidenceBinding,
) -> dict[str, Any]:
    return {
        "schema": 2,
        "fixture_kind": FIXTURE_KIND_V2,
        "stage": FULL_CONDUCTED_STAGE,
        "run_id": run_id,
        "board_id": board_id,
        "serial": serial,
        "selector": {
            "path": selector.path,
            "sha256": selector.sha256,
            "image_role": selector.image_role,
        },
        "source_files": {
            "fixture_manifest": _file(manifest),
            "setup_attestation": _file(setup),
        },
    }


def _selector_control_builder(
    *,
    selector: SelectorEvidenceBinding,
    bench_manifest_path: Path | None,
    openocd_config_path: Path,
    profile_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    common = {
        "schema": 1,
        "source_commit": source_commit,
        "profile_contract_sha256": selector.profile_contract_sha256,
        "profile": _file(profile_path),
    }
    if selector.image_role == "fast20":
        return {
            **common,
            "control_kind": "sealed_fast20_autonomous_schedule",
            "state_order": list(EXPECTED_STATES),
            "state_codes": {"ALL_OFF": 8, **{state: i for i, state in enumerate(ANTENNA_STATES)}},
        }
    assert bench_manifest_path is not None
    return {
        **common,
        "control_kind": "synthetic_test_bench_control",
        "bench_manifest": _file(bench_manifest_path),
        "openocd_config": _file(openocd_config_path),
        "control_profile": _file(profile_path),
    }


def _inputs(tmp_path: Path, role: str) -> dict[str, Path]:
    fixture, setup = _production_fixture_inputs(tmp_path)
    profile = _write_json(tmp_path / "profile.json", {"profile": "fast20-v1"})
    openocd = tmp_path / "rpi4-swd.cfg"
    openocd.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    bench = _write_json(tmp_path / "bench.manifest.json", {"bench": True})
    selector, _ = _selector_evidence(tmp_path, role)
    return {
        "fixture": fixture,
        "setup": setup,
        "profile": profile,
        "openocd": openocd,
        "bench": bench,
        "selector": selector,
    }


def _build(
    tmp_path: Path,
    *,
    mode: str = "static-bench",
    run_id: str = "selected-state-r01",
    require_one_degree: bool = False,
    priors: dict[str, Path] | None = None,
) -> Path:
    role = "bench" if mode == "static-bench" else "fast20"
    inputs = _inputs(tmp_path, role)
    selector_sha = hashlib.sha256(inputs["selector"].read_bytes()).hexdigest()
    prior_kwargs: dict[str, Any] = {}
    if priors is not None:
        prior_kwargs = {
            "intervention_contract_path": priors["intervention"],
            "static_result_path": priors["static"],
            "timing_result_path": priors["timing"],
        }
    return runner.build_plan(
        mode=mode,
        run_id=run_id,
        campaign_id=CAMPAIGN,
        board_id=BOARD,
        serial=SERIAL,
        uri=URI,
        fixture_manifest_path=inputs["fixture"],
        setup_attestation_path=inputs["setup"],
        selector_evidence_path=inputs["selector"],
        selector_evidence_sha256=selector_sha,
        selector_run_id=f"selector-{mode}-r01",
        device_identity_path=_device_identity(tmp_path),
        state_root=tmp_path / "state",
        profile_path=inputs["profile"],
        openocd_config_path=inputs["openocd"],
        bench_manifest_path=inputs["bench"] if role == "bench" else None,
        require_one_degree=require_one_degree,
        runtime_bindings=_runtime(),
        selector_loader=_selector_loader,
        fixture_evidence_loader=_fixture_loader,
        selector_control_builder=_selector_control_builder,
        now=lambda: "2026-08-29T12:00:00+00:00",
        **prior_kwargs,
    )


def _load_plan(path: Path) -> tuple[dict[str, Any], str]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return envelope["plan_contract"], envelope["plan_contract_sha256"]


def _passed_mute(serial: str, purpose: str) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "status": "passed",
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }


def _passed_identity(serial: str, requested_uri: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "read_only_current_usb_uri_resolution",
        "status": "passed",
        "serial": serial,
        "requested_uri": requested_uri,
        "resolved_uri": requested_uri,
        "exact_uri_match": True,
        "scan_mutates_radio_state": False,
        "error": None,
    }


def _target_image_result(
    contract: dict[str, Any], *, exact: bool, reset_run_succeeded: bool
) -> dict[str, Any]:
    selector = contract["selector"]
    passed = exact and reset_run_succeeded
    return {
        "schema": 1,
        "evidence_kind": "exact_live_selector_image_readback_v1",
        "status": "passed" if passed else "failed",
        "image_role": contract["image_role"],
        "selector_evidence_sha256": selector["sha256"],
        "firmware_bin_sha256": selector["firmware_bin_sha256"],
        "profile_contract_sha256": selector["profile_contract_sha256"],
        "exact_byte_match": exact,
        "uid_exact_match": exact,
        "expected_board_id": contract["board_id"],
        "observed_uid": (contract["board_id"].removeprefix("stm32c011-") if exact else "0" * 24),
        "expected_target_sha256": selector["firmware_bin_sha256"],
        "observed_target_sha256": (
            selector["firmware_bin_sha256"] if exact else _hash("wrong-target")
        ),
        "full_bin_and_uid_compared_before_reset_run": True,
        "reviewed_image_started_only_after_exact_match": exact,
        "mailbox_access_performed": False,
        "reset_run_succeeded": reset_run_succeeded,
        "error": None if passed else {"type": "SyntheticImageFailure", "message": "test"},
    }


def _passed_fast20_cleanup(evidence_root: Path) -> dict[str, Any]:
    evidence_root.mkdir(parents=True)
    odr = evidence_root / "odr.bin"
    moder = evidence_root / "moder.bin"
    moder_before = evidence_root / "moder-before.bin"
    read_log = evidence_root / "read.log"
    action_log = evidence_root / "action.log"
    odr.write_bytes((8).to_bytes(4, "little"))
    moder.write_bytes((0xFFFFFF55).to_bytes(4, "little"))
    moder_before.write_bytes((0xFFFFFFFF).to_bytes(4, "little"))
    read_log.write_text("read\n", encoding="utf-8")
    action_log.write_text("action\n", encoding="utf-8")
    return {
        "schema": 1,
        "evidence_kind": runner.FAST20_FAILURE_CLEANUP_KIND,
        "status": "passed",
        "selector_write_authorized_by_exact_image_and_uid_admission": True,
        "mailbox_access_performed": False,
        "target_resume_command_issued": False,
        "target_left_halted": True,
        "electrical_selector_all_off_proven": True,
        "gpio_output_latch_readback": {
            "register": "GPIOA_ODR",
            "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS,
            "selector_mask": runner.one_hot_runner.SELECTOR_GPIO_MASK,
            "raw_value": 8,
            "masked_selector_code": 8,
            "expected_selector_code": 8,
            "passed": True,
            "physical_rf_state_proven": False,
        },
        "gpio_output_mode_readback": {
            "register": "GPIOA_MODER",
            "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS - 0x14,
            "selector_mode_mask": 0xFF,
            "raw_value": 0xFFFFFF55,
            "masked_selector_modes": 0x55,
            "expected_selector_output_modes": 0x55,
            "passed": True,
        },
        "gpio_output_mode_before": {
            "register": "GPIOA_MODER",
            "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS - 0x14,
            "raw_value": 0xFFFFFFFF,
            "preserved_non_selector_mask": 0xFFFFFF00,
            "desired_value": 0xFFFFFF55,
        },
        "openocd_returncodes": {
            "moder_read": 0,
            "all_off_and_mode_write": 0,
        },
        "gpio_odr_readback": _file(odr),
        "gpio_moder_readback": _file(moder),
        "gpio_moder_before_readback": _file(moder_before),
        "moder_read_openocd_log": _file(read_log),
        "all_off_openocd_log": _file(action_log),
        "error": None,
    }


def _tiny_binding(tmp_path: Path, context: dict[str, Any], label: str) -> dict[str, Any]:
    root = tmp_path / "tiny" / label
    root.mkdir(parents=True, exist_ok=True)
    raw = root / f"{label}.sigmf-data"
    metadata = root / f"{label}.sigmf-meta"
    record = root / runner.CONDITION_RECORD_FILENAME
    raw.write_bytes(f"raw-{label}".encode())
    metadata.write_text(json.dumps({"label": label}), encoding="utf-8")
    record.write_text(json.dumps({"label": label}), encoding="utf-8")
    return {
        "run_id": f"run-{label}",
        "stream_id": f"stream-{label}",
        "artifact_id": f"artifact-{label}",
        "raw_iq_path": str(raw.absolute()),
        "raw_iq_sha256": _file(raw)["sha256"],
        "raw_iq_size_bytes": raw.stat().st_size,
        "metadata_path": str(metadata.absolute()),
        "metadata_sha256": _file(metadata)["sha256"],
        "metadata_size_bytes": metadata.stat().st_size,
        "condition_record_path": str(record.absolute()),
        "condition_record_sha256": _file(record)["sha256"],
        "condition_record_size_bytes": record.stat().st_size,
        "leaf_source_sha256s": [_file(raw)["sha256"]],
        "plan_sha256": context["plan_sha256"],
        "fixture_revision_sha256": context["fixture_revision_sha256"],
        "selector_evidence_sha256": context["selector_evidence_sha256"],
        "source_commit": context["source_commit"],
        "dependency_commit": context["dependency_commit"],
        "native_attestation_sha256": context["native_attestation_sha256"],
        "device_identity_sha256": context["device_identity_sha256"],
    }


def _static_scientific_evidence(plan: Path, tmp_path: Path) -> dict[str, Any]:
    contract, plan_sha = _load_plan(plan)
    context = runner._qualification_context(contract, plan_sha)
    codes = {"ALL_OFF": 8, **{state: i for i, state in enumerate(ANTENNA_STATES)}}
    observations = []
    for index, state in enumerate(EXPECTED_STATES):
        code = codes[state]
        observations.append(
            {
                "state": state,
                "capture": _tiny_binding(tmp_path, context, state.lower()),
                "command": {
                    "commanded_state": state,
                    "commanded_code": code,
                    "command_sequence": index + 1,
                    "acknowledged_sequence": index + 1,
                    "applied_state": state,
                    "applied_code": code,
                    "gpio_latch_code": code,
                    "lease_ms": 0 if state == "ALL_OFF" else 60_000,
                    "command_valid": True,
                    "readback_passed": True,
                },
                "quality": {
                    "metadata_abi": 2,
                    "expected_sample_count": runner.STATIC_SAMPLE_COUNT,
                    "observed_sample_count": runner.STATIC_SAMPLE_COUNT,
                    "raw_sample_count": runner.STATIC_SAMPLE_COUNT,
                    "continuity_verified": True,
                    "missing_sample_count": 0,
                    "clipped_sample_count": 0,
                    "adc_headroom_db": 12.0,
                    "reference_detected": True,
                    "reference_snr_db": 30.0,
                    "final_mute_verified": True,
                    "final_selector_control_verified": True,
                },
                "transfer": {
                    "detected": True,
                    "h": {"real": 0.001 if state == "ALL_OFF" else 0.1, "imag": 0.001},
                    "magnitude_upper_bound": None,
                    "coherence": 0.999,
                    "phase_rms_deg": 0.5,
                },
            }
        )
    return {
        "schema": 1,
        "evidence_kind": STATIC_KIND,
        "context": context,
        "state_order": list(EXPECTED_STATES),
        "state_codes": codes,
        "observations": observations,
        "final_mute_verified": True,
        "final_all_off_readback": {
            "state": "ALL_OFF",
            "mailbox_code": 8,
            "gpio_latch_code": 8,
            "passed": True,
        },
    }


def test_parser_exposes_prepare_capture_analyze_for_all_modes() -> None:
    parser = runner._parser()
    for mode in ("static-bench", "fast20-timing", "fast20-matrix"):
        for action in ("capture", "analyze"):
            parsed = parser.parse_args([mode, action, "--plan", "/x"])
            assert parsed.mode == mode
            assert parsed.action == action


def test_prepare_is_create_only_local_role_and_hardware_policy_bound(tmp_path: Path) -> None:
    plan = _build(tmp_path)
    contract, digest = _load_plan(plan)
    assert contract["mode"] == "static-bench"
    assert contract["uri"] == URI
    assert contract["image_role"] == "bench"
    assert contract["selector"]["image_role"] == "bench"
    assert contract["local_rpi_storage_only"] is True
    assert contract["hardware_access_policy"] == {
        "prepare": False,
        "analyze": False,
        "capture": True,
    }
    assert contract["fixture_evidence"]["run_id"] == "selected-state-r01"
    assert len(digest) == 64
    assert os.stat(plan).st_mode & 0o777 == 0o400
    with pytest.raises(runner.SelectedStateRunError, match="burned"):
        _build(tmp_path)


def test_prepare_rejects_removable_or_pluto_storage(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, "bench")
    selector_sha = hashlib.sha256(inputs["selector"].read_bytes()).hexdigest()
    with pytest.raises(runner.SelectedStateRunError, match="local RPi storage"):
        runner.build_plan(
            mode="static-bench",
            run_id="bad-storage-r01",
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            serial=SERIAL,
            uri=URI,
            fixture_manifest_path=inputs["fixture"],
            setup_attestation_path=inputs["setup"],
            selector_evidence_path=inputs["selector"],
            selector_evidence_sha256=selector_sha,
            selector_run_id="selector-bench-r01",
            device_identity_path=_device_identity(tmp_path),
            state_root=Path("/mnt/pluto"),
            profile_path=inputs["profile"],
            openocd_config_path=inputs["openocd"],
            bench_manifest_path=inputs["bench"],
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
        )


def test_prepare_rejects_wrong_selector_role_and_uri(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, "bench")
    selector_sha = hashlib.sha256(inputs["selector"].read_bytes()).hexdigest()

    def wrong_loader(*args: Any, **kwargs: Any) -> SelectorEvidenceBinding:
        selected = _selector_loader(*args, **kwargs)
        values = {field: getattr(selected, field) for field in selected.__dataclass_fields__}
        values["image_role"] = "fast20"
        return SelectorEvidenceBinding(**values)

    with pytest.raises(runner.SelectedStateRunError, match="outside the exact"):
        runner.build_plan(
            mode="static-bench",
            run_id="wrong-role-r01",
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            serial=SERIAL,
            uri=URI,
            fixture_manifest_path=inputs["fixture"],
            setup_attestation_path=inputs["setup"],
            selector_evidence_path=inputs["selector"],
            selector_evidence_sha256=selector_sha,
            selector_run_id="selector-bench-r01",
            device_identity_path=_device_identity(tmp_path),
            state_root=tmp_path / "state",
            profile_path=inputs["profile"],
            openocd_config_path=inputs["openocd"],
            bench_manifest_path=inputs["bench"],
            runtime_bindings=_runtime(),
            selector_loader=wrong_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
        )


def test_fast20_profile_must_match_sealed_path_hash_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector_path = tmp_path / "sealed-selector.json"
    selector_path.write_text("sealed\n", encoding="utf-8")
    firmware_path = tmp_path / "fast20.bin"
    firmware_path.write_bytes(b"firmware")
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    other_config_path = tmp_path / "other-rpi4-swd.cfg"
    other_config_path.write_text("adapter driver cmsis-dap\n", encoding="utf-8")
    sealed_profile_path = _write_json(tmp_path / "sealed-profile.json", {"profile": 1})
    other_profile_path = _write_json(tmp_path / "other-profile.json", {"profile": 1})
    profile_sha = _hash("profile-contract")
    selector = SelectorEvidenceBinding(
        path=str(selector_path.absolute()),
        sha256=_file(selector_path)["sha256"],
        campaign_id=CAMPAIGN,
        run_id="selector-fast20-r01",
        board_id=BOARD,
        image_role="fast20",
        firmware_bin_sha256=_file(firmware_path)["sha256"],
        profile_contract_sha256=profile_sha,
        startup_evidence_sha256=_hash("startup"),
    )
    sealed = {
        "frozen_inputs": {
            "files": {
                "firmware_bin": _file(firmware_path),
                "openocd_config": _file(config_path),
                "profile": _file(sealed_profile_path),
            }
        }
    }
    monkeypatch.setattr(runner, "validate_sealed_selector_evidence", lambda *_a, **_k: sealed)
    monkeypatch.setattr(
        runner,
        "load_profile",
        lambda _path: SimpleNamespace(
            profile_id="fast20-v1",
            contract_sha256=profile_sha,
            all_off_code=8,
            states=[
                SimpleNamespace(name=state, gpio_code=index)
                for index, state in enumerate(ANTENNA_STATES)
            ],
        ),
    )

    with pytest.raises(runner.SelectedStateRunError, match="path/hash/size"):
        runner._selector_control_from_files(
            selector=selector,
            bench_manifest_path=None,
            openocd_config_path=config_path,
            profile_path=other_profile_path,
            source_commit=SOURCE,
        )
    with pytest.raises(runner.SelectedStateRunError, match="OpenOCD path/hash/size"):
        runner._selector_control_from_files(
            selector=selector,
            bench_manifest_path=None,
            openocd_config_path=other_config_path,
            profile_path=sealed_profile_path,
            source_commit=SOURCE,
        )
    control = runner._selector_control_from_files(
        selector=selector,
        bench_manifest_path=None,
        openocd_config_path=config_path,
        profile_path=sealed_profile_path,
        source_commit=SOURCE,
    )
    assert control is not None
    assert control["profile"] == _file(sealed_profile_path)


def test_prepare_rejects_stale_or_legacy_self_asserted_device_identity(tmp_path: Path) -> None:
    current = _device_identity(tmp_path)
    with pytest.raises(runner.SelectedStateRunError, match="stale"):
        runner._device_identity(
            current,
            serial=SERIAL,
            uri=URI,
            reference_time="2026-08-29T12:10:00+00:00",
        )
    legacy = _write_json(
        tmp_path / "legacy-device.json",
        {
            "schema": 1,
            "evidence_kind": "pluto_device_identity_v1",
            "serial": SERIAL,
            "usb_uri": URI,
            "device_attributes_sha256": _hash("self-asserted"),
            "accepted": True,
        },
    )
    with pytest.raises(runner.SelectedStateRunError, match="keys differ"):
        runner._device_identity(legacy, serial=SERIAL, uri=URI)
    inputs = _inputs(tmp_path / "bad-uri", "bench")
    selector_sha = hashlib.sha256(inputs["selector"].read_bytes()).hexdigest()
    with pytest.raises(runner.SelectedStateRunError, match="explicit current usb"):
        runner.build_plan(
            mode="static-bench",
            run_id="bad-uri-r01",
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            serial=SERIAL,
            uri="ip:pluto.local",
            fixture_manifest_path=inputs["fixture"],
            setup_attestation_path=inputs["setup"],
            selector_evidence_path=inputs["selector"],
            selector_evidence_sha256=selector_sha,
            selector_run_id="selector-bench-r01",
            device_identity_path=_device_identity(tmp_path),
            state_root=tmp_path / "state2",
            profile_path=inputs["profile"],
            openocd_config_path=inputs["openocd"],
            bench_manifest_path=inputs["bench"],
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
        )


def test_matrix_prepare_requires_and_binds_all_prior_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(runner.SelectedStateRunError, match="requires intervention"):
        _build(tmp_path / "missing", mode="fast20-matrix", run_id="matrix-no-priors")
    prior_root = tmp_path / "prior"
    priors = {
        "intervention": _write_json(prior_root / "intervention.json", {"placeholder": 1}),
        "static": _write_json(prior_root / "static.json", {"placeholder": 2}),
        "timing": _write_json(prior_root / "timing.json", {"placeholder": 3}),
    }
    monkeypatch.setattr(
        runner,
        "validate_intervention_contract",
        lambda *_args, **_kwargs: SimpleNamespace(
            campaign_id=CAMPAIGN,
            board_id=BOARD,
            source_commit=SOURCE,
            dependency_commit=DEPENDENCY,
        ),
    )
    plan = _build(
        tmp_path / "matrix",
        mode="fast20-matrix",
        run_id="matrix-r01",
        require_one_degree=True,
        priors=priors,
    )
    contract, _ = _load_plan(plan)
    assert contract["image_role"] == "fast20"
    assert contract["require_one_degree"] is True
    assert set(contract["prior_qualification_files"]) == {
        "intervention_contract",
        "static_result",
        "timing_result",
    }


def test_capture_action_produces_exact_stream_lattice_and_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)
    seen: list[tuple[int, str | None]] = []

    def fake_capture_one_stream(**kwargs: Any) -> dict[str, Any]:
        index = int(kwargs["capture_index"])
        state = kwargs["state"]
        seen.append((index, state))
        return {"stream_id": str(10_000 + index), "synthetic": True}

    monkeypatch.setattr(runner, "_capture_one_stream", fake_capture_one_stream)
    monkeypatch.setattr(runner, "_reanalyze_capture_evidence", lambda *args, **kwargs: {})
    path = runner.execute_capture(
        plan_path=plan,
        runtime_bindings=_runtime(),
        selector_loader=_selector_loader,
        fixture_evidence_loader=_fixture_loader,
        selector_control_builder=_selector_control_builder,
        now=lambda: "2026-08-29T12:00:00+00:00",
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert seen == list(enumerate(EXPECTED_STATES, start=1))
    assert len(document["captures"]) == 9
    tombstone = json.loads(
        (plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert tombstone["run_id_burned"] is True
    assert tombstone["expected_capture_set_path"] == str(path)
    with pytest.raises(runner.SelectedStateRunError, match="already burned"):
        runner.execute_capture(
            plan_path=plan,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
            now=lambda: "2026-08-29T12:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("artifact_name", "is_directory"),
    (
        (runner.EXECUTION_TOMBSTONE_FILENAME, False),
        (runner.FAILURE_TOMBSTONE_FILENAME, False),
        (runner.ANALYSIS_TOMBSTONE_FILENAME, False),
        (runner.RESULT_FILENAME, False),
        (runner.CAPTURE_EVIDENCE_FILENAME, False),
        ("capture-01.failure-safety.json", False),
        ("session.failure-safety.json", False),
        ("unexpected-run-output.tmp", False),
        ("captures", True),
        ("selector-live-evidence", True),
        ("failure-safety-live-evidence", True),
        (".failed", True),
    ),
)
def test_capture_rejects_any_run_derived_artifact_before_hardware_or_revalidation(
    tmp_path: Path, artifact_name: str, is_directory: bool
) -> None:
    plan = _build(tmp_path)
    artifact = plan.parent / artifact_name
    if is_directory:
        artifact.mkdir()
    else:
        artifact.write_text("prior run artifact\n", encoding="utf-8")
    touched: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        touched.append("called")
        raise AssertionError("hardware/revalidation boundary called")

    with pytest.raises(runner.SelectedStateRunError, match="already burned"):
        runner.execute_capture(
            plan_path=plan,
            runtime_bindings=_runtime(),
            selector_loader=forbidden,
            fixture_evidence_loader=forbidden,
            selector_control_builder=forbidden,
            capture_boundary=forbidden,
            mute_boundary=forbidden,
            identity_boundary=forbidden,
            selector_boundary=forbidden,
            target_image_boundary=forbidden,
            fast20_cleanup_boundary=forbidden,
        )
    assert touched == []


@pytest.mark.parametrize("boundary", ("target-image", "all-off-cleanup"))
def test_selector_live_evidence_rejects_preexisting_symlink_ancestor_before_openocd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    openocd_calls: list[str] = []

    def forbidden_openocd(*_args: Any, **_kwargs: Any) -> Any:
        openocd_calls.append("called")
        raise AssertionError("OpenOCD called through a symlinked evidence path")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_openocd)
    with pytest.raises(runner.SelectedStateRunError, match="symlink"):
        if boundary == "target-image":
            firmware_path = tmp_path / "fast20.bin"
            firmware_path.write_bytes(b"sealed-fast20-image")
            runner._live_target_image_preflight(
                _fast20_preflight_contract(firmware_path, config_path),
                evidence_root=linked_parent / "capture-01",
            )
        else:
            runner._live_fast20_all_off_cleanup(
                {
                    "openocd_config": _file(config_path),
                    "state_codes": {"ALL_OFF": 8},
                },
                evidence_root=linked_parent / "cleanup",
            )
    assert openocd_calls == []


def test_evidence_and_quarantine_directories_reject_nonlocal_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_nearest_existing", lambda _path: tmp_path)
    monkeypatch.setattr(
        runner,
        "_filesystem_device",
        lambda path: 1 if Path(path) == Path("/home/pi") else 2,
    )
    with pytest.raises(runner.SelectedStateRunError, match="local filesystem"):
        runner._ensure_local_directory(tmp_path / ".failed", "failed artifact quarantine")


def test_capture_rejects_device_observation_that_became_stale_after_prepare(
    tmp_path: Path,
) -> None:
    plan = _build(tmp_path)
    hardware_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        hardware_calls.append("called")
        raise AssertionError("capture hardware called")

    with pytest.raises(runner.SelectedStateRunError, match="stale"):
        runner.execute_capture(
            plan_path=plan,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
            capture_boundary=forbidden,
            mute_boundary=_passed_mute,
            identity_boundary=forbidden,
            selector_boundary=forbidden,
            target_image_boundary=forbidden,
            fast20_cleanup_boundary=forbidden,
            now=lambda: "2026-08-29T12:10:00+00:00",
        )
    assert hardware_calls == []
    failure = json.loads(
        (plan.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert failure["final_failure_cleanup_passed"] is True


def test_capture_mutes_before_identity_and_retries_mute_on_identity_failure(
    tmp_path: Path,
) -> None:
    plan = _build(tmp_path)
    contract, plan_sha = _load_plan(plan)
    events: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        assert serial == SERIAL
        events.append(f"mute:{purpose}")
        return _passed_mute(serial, purpose)

    def identity(_serial: str, _uri: str) -> dict[str, Any]:
        events.append("identity")
        raise RuntimeError("identity unavailable")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("post-identity boundary called")

    with pytest.raises(runner.SelectedStateRunError, match="USB URI preflight"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state="ALL_OFF",
            forbidden_stream_ids=set(),
            capture_boundary=forbidden,
            mute_boundary=mute,
            identity_boundary=identity,
            selector_boundary=forbidden,
            target_image_boundary=forbidden,
            fast20_cleanup_boundary=forbidden,
        )

    assert events == ["mute:pre_capture", "identity", "mute:failure_cleanup"]
    cleanup = json.loads(
        (plan.parent / "capture-01.failure-safety.json").read_text(encoding="utf-8")
    )
    assert cleanup["selector_image_and_uid_admitted"] is False
    assert cleanup["selector_cleanup_attempted"] is False
    assert cleanup["cleanup_passed"] is True


def test_unproven_failure_mute_is_an_explicit_no_go(tmp_path: Path) -> None:
    plan = _build(tmp_path)
    contract, plan_sha = _load_plan(plan)

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        result = _passed_mute(serial, purpose)
        if purpose == "failure_cleanup":
            result["status"] = "failed"
            result["error"] = {"type": "SyntheticMuteFailure", "message": "test"}
        return result

    with pytest.raises(runner.SelectedStateRunError, match="cleanup was not proven"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state="ALL_OFF",
            forbidden_stream_ids=set(),
            capture_boundary=lambda *_a, **_k: None,
            mute_boundary=mute,
            identity_boundary=lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("identity unavailable")
            ),
            selector_boundary=lambda *_a, **_k: {},
            target_image_boundary=lambda *_a, **_k: {},
        )
    cleanup = json.loads(
        (plan.parent / "capture-01.failure-safety.json").read_text(encoding="utf-8")
    )
    assert cleanup["exact_mute_passed"] is False
    assert cleanup["cleanup_passed"] is False


def test_static_failure_after_image_admission_mutes_then_forces_all_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)
    contract, plan_sha = _load_plan(plan)
    events: list[str] = []
    codes = {"ALL_OFF": 8, **{state: i for i, state in enumerate(ANTENNA_STATES)}}
    monkeypatch.setattr(runner.one_hot_runner, "_state_map", lambda _control: codes)
    monkeypatch.setattr(
        runner.one_hot_runner,
        "_selector_passed",
        lambda _value, **kwargs: kwargs["purpose"] == "final_cleanup_all_off",
    )

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        events.append(f"mute:{purpose}")
        return _passed_mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        events.append("identity")
        return _passed_identity(serial, uri)

    def target(_contract: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        events.append("target")
        return _target_image_result(contract, exact=True, reset_run_succeeded=True)

    def selector(_control: dict[str, Any], _state: str, _code: int, purpose: str) -> dict[str, Any]:
        events.append(f"selector:{purpose}")
        return {"purpose": purpose, "status": "passed", "error": None}

    with pytest.raises(runner.SelectedStateRunError, match="pre-capture state readback"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state="ALL_OFF",
            forbidden_stream_ids=set(),
            capture_boundary=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("capture called")
            ),
            mute_boundary=mute,
            identity_boundary=identity,
            selector_boundary=selector,
            target_image_boundary=target,
        )

    assert events == [
        "mute:pre_capture",
        "identity",
        "target",
        "selector:before_condition",
        "mute:failure_cleanup",
        "selector:final_cleanup_all_off",
    ]
    cleanup = json.loads(
        (plan.parent / "capture-01.failure-safety.json").read_text(encoding="utf-8")
    )
    assert cleanup["selector_image_and_uid_admitted"] is True
    assert cleanup["selector_cleanup_attempted"] is True
    assert cleanup["selector_cleanup_passed"] is True
    assert cleanup["cleanup_passed"] is True


def test_fast20_image_mismatch_never_authorizes_selector_cleanup(tmp_path: Path) -> None:
    plan = _build(tmp_path, mode="fast20-timing", run_id="fast20-mismatch-r01")
    contract, plan_sha = _load_plan(plan)
    cleanup_calls: list[str] = []

    def forbidden_cleanup(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        cleanup_calls.append("called")
        raise AssertionError("selector cleanup touched an unadmitted target")

    with pytest.raises(runner.SelectedStateRunError, match="selector-image preflight"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state=None,
            forbidden_stream_ids=set(),
            capture_boundary=lambda *_a, **_k: None,
            mute_boundary=_passed_mute,
            identity_boundary=_passed_identity,
            selector_boundary=forbidden_cleanup,
            target_image_boundary=lambda *_a, **_k: _target_image_result(
                contract, exact=False, reset_run_succeeded=False
            ),
            fast20_cleanup_boundary=forbidden_cleanup,
        )

    assert cleanup_calls == []
    cleanup = json.loads(
        (plan.parent / "capture-01.failure-safety.json").read_text(encoding="utf-8")
    )
    assert cleanup["selector_cleanup_attempted"] is False
    assert cleanup["cleanup_passed"] is True


def test_image_admission_rejects_true_booleans_without_concrete_uid_and_bin_proof(
    tmp_path: Path,
) -> None:
    plan = _build(tmp_path, mode="fast20-timing", run_id="fast20-self-claim-r01")
    contract, _ = _load_plan(plan)
    claim = _target_image_result(contract, exact=True, reset_run_succeeded=False)
    del claim["observed_uid"]
    del claim["observed_target_sha256"]

    assert runner._target_image_write_admitted(claim, contract=contract) is False
    assert runner._target_image_admission_binding(claim, contract=contract) is None
    assert runner._target_image_admission_binding_passed({}, contract=contract) is False


def test_fast20_exact_image_uid_admission_authorizes_halted_all_off_cleanup(
    tmp_path: Path,
) -> None:
    plan = _build(tmp_path, mode="fast20-timing", run_id="fast20-cleanup-r01")
    contract, plan_sha = _load_plan(plan)
    cleanup_calls: list[str] = []

    def fast20_cleanup(_control: dict[str, Any], *, evidence_root: Path) -> dict[str, Any]:
        cleanup_calls.append(str(evidence_root))
        evidence_root.mkdir(parents=True)
        odr = evidence_root / "odr.bin"
        moder = evidence_root / "moder.bin"
        moder_before = evidence_root / "moder-before.bin"
        read_log = evidence_root / "read.log"
        action_log = evidence_root / "action.log"
        odr.write_bytes((8).to_bytes(4, "little"))
        moder.write_bytes((0xFFFFFF55).to_bytes(4, "little"))
        moder_before.write_bytes((0xFFFFFFFF).to_bytes(4, "little"))
        read_log.write_text("read\n", encoding="utf-8")
        action_log.write_text("action\n", encoding="utf-8")
        return {
            "schema": 1,
            "evidence_kind": runner.FAST20_FAILURE_CLEANUP_KIND,
            "status": "passed",
            "selector_write_authorized_by_exact_image_and_uid_admission": True,
            "mailbox_access_performed": False,
            "target_resume_command_issued": False,
            "target_left_halted": True,
            "electrical_selector_all_off_proven": True,
            "gpio_output_latch_readback": {
                "register": "GPIOA_ODR",
                "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS,
                "selector_mask": runner.one_hot_runner.SELECTOR_GPIO_MASK,
                "raw_value": 8,
                "masked_selector_code": 8,
                "expected_selector_code": 8,
                "passed": True,
                "physical_rf_state_proven": False,
            },
            "gpio_output_mode_readback": {
                "register": "GPIOA_MODER",
                "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS - 0x14,
                "selector_mode_mask": 0xFF,
                "raw_value": 0xFFFFFF55,
                "masked_selector_modes": 0x55,
                "expected_selector_output_modes": 0x55,
                "passed": True,
            },
            "gpio_output_mode_before": {
                "register": "GPIOA_MODER",
                "address": runner.one_hot_runner.GPIOA_ODR_ADDRESS - 0x14,
                "raw_value": 0xFFFFFFFF,
                "preserved_non_selector_mask": 0xFFFFFF00,
                "desired_value": 0xFFFFFF55,
            },
            "openocd_returncodes": {
                "moder_read": 0,
                "all_off_and_mode_write": 0,
            },
            "gpio_odr_readback": _file(odr),
            "gpio_moder_readback": _file(moder),
            "gpio_moder_before_readback": _file(moder_before),
            "moder_read_openocd_log": _file(read_log),
            "all_off_openocd_log": _file(action_log),
            "error": None,
        }

    with pytest.raises(runner.SelectedStateRunError, match="selector-image preflight"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state=None,
            forbidden_stream_ids=set(),
            capture_boundary=lambda *_a, **_k: None,
            mute_boundary=_passed_mute,
            identity_boundary=_passed_identity,
            selector_boundary=lambda *_a, **_k: {},
            target_image_boundary=lambda *_a, **_k: _target_image_result(
                contract, exact=True, reset_run_succeeded=False
            ),
            fast20_cleanup_boundary=fast20_cleanup,
        )

    assert len(cleanup_calls) == 1
    cleanup = json.loads(
        (plan.parent / "capture-01.failure-safety.json").read_text(encoding="utf-8")
    )
    assert cleanup["selector_image_and_uid_admitted"] is True
    assert cleanup["selector_image_admission"]["observed_uid"] == (BOARD.removeprefix("stm32c011-"))
    assert cleanup["selector_image_admission_sha256"] == runner.canonical_sha256(
        cleanup["selector_image_admission"]
    )
    assert cleanup["selector_cleanup_attempted"] is True
    assert cleanup["selector_cleanup_passed"] is True
    assert cleanup["cleanup_passed"] is True


def test_fast20_admission_remains_durable_for_next_capture_early_failure(
    tmp_path: Path,
) -> None:
    plan = _build(tmp_path, mode="fast20-timing", run_id="fast20-durable-r01")
    contract, plan_sha = _load_plan(plan)
    admission = runner._target_image_admission_binding(
        _target_image_result(contract, exact=True, reset_run_succeeded=True),
        contract=contract,
    )
    assert admission is not None
    safety_state: dict[str, Any] = {
        "selector_image_admitted": True,
        "selector_image_admission": admission,
    }
    cleanup_calls: list[Path] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        result = _passed_mute(serial, purpose)
        if purpose == "pre_capture":
            result["status"] = "failed"
            result["error"] = {"type": "SyntheticMuteFailure", "message": "test"}
        return result

    def cleanup(_control: dict[str, Any], *, evidence_root: Path) -> dict[str, Any]:
        cleanup_calls.append(evidence_root)
        return _passed_fast20_cleanup(evidence_root)

    with pytest.raises(runner.SelectedStateRunError, match="pre-capture exact-radio mute"):
        runner._capture_one_stream(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=2,
            state=None,
            forbidden_stream_ids=set(),
            capture_boundary=lambda *_a, **_k: None,
            mute_boundary=mute,
            identity_boundary=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("identity called after failed mute")
            ),
            selector_boundary=lambda *_a, **_k: {},
            target_image_boundary=lambda *_a, **_k: {},
            safety_state=safety_state,
            fast20_cleanup_boundary=cleanup,
        )

    assert len(cleanup_calls) == 1
    evidence = json.loads(
        (plan.parent / "capture-02.failure-safety.json").read_text(encoding="utf-8")
    )
    assert evidence["selector_image_and_uid_admitted"] is True
    assert evidence["selector_cleanup_passed"] is True


def test_fast20_capture_path_mutes_then_electrically_cleans_all_off_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path, mode="fast20-timing", run_id="fast20-success-cleanup-r01")
    contract, plan_sha = _load_plan(plan)
    events: list[str] = []
    cleanup_roots: list[Path] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        events.append(f"mute:{purpose}")
        return _passed_mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        events.append("identity")
        return _passed_identity(serial, uri)

    def target(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("target-image")
        return _target_image_result(contract, exact=True, reset_run_succeeded=True)

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        events.append("capture")
        return SimpleNamespace()

    def cleanup(_control: dict[str, Any], *, evidence_root: Path) -> dict[str, Any]:
        events.append("all-off")
        cleanup_roots.append(evidence_root)
        return _passed_fast20_cleanup(evidence_root)

    def stop_after_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        events.append("validate")
        raise runner.SelectedStateRunError("synthetic stop after cleanup")

    monkeypatch.setattr(runner, "_validate_live_capture", stop_after_cleanup)
    with pytest.raises(runner.SelectedStateRunError, match="stop after cleanup"):
        runner._capture_one_stream_impl(
            contract=contract,
            plan_path=plan,
            plan_sha=plan_sha,
            capture_index=1,
            state=None,
            forbidden_stream_ids=set(),
            capture_boundary=capture,
            mute_boundary=mute,
            identity_boundary=identity,
            selector_boundary=lambda *_a, **_k: {},
            target_image_boundary=target,
            fast20_cleanup_boundary=cleanup,
            safety_state={},
        )

    assert events == [
        "mute:pre_capture",
        "identity",
        "target-image",
        "capture",
        "mute:post_capture",
        "all-off",
        "validate",
    ]
    assert cleanup_roots == [
        plan.parent / "selector-live-evidence" / "capture-01" / "success-all-off"
    ]


def test_capture_failure_is_fail_closed_and_burns_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise runner.SelectedStateRunError("synthetic ENODATA")

    monkeypatch.setattr(runner, "_capture_one_stream", fail)
    mute_calls: list[tuple[str, str]] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        mute_calls.append((serial, purpose))
        return _passed_mute(serial, purpose)

    with pytest.raises(runner.SelectedStateRunError, match="ENODATA"):
        runner.execute_capture(
            plan_path=plan,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
            mute_boundary=mute,
            now=lambda: "2026-08-29T12:00:00+00:00",
        )
    failure = json.loads(
        (plan.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert failure["artifacts_accepted"] is False
    assert failure["automatic_retry_attempted"] is False
    assert failure["final_failure_cleanup_passed"] is True
    assert failure["failure_safety_evidence"]["session"] is not None
    assert mute_calls == [(SERIAL, "failure_cleanup")]
    session = json.loads((plan.parent / "session.failure-safety.json").read_text(encoding="utf-8"))
    assert session["selector_cleanup_attempted"] is False
    assert session["cleanup_passed"] is True
    assert not (plan.parent / runner.CAPTURE_EVIDENCE_FILENAME).exists()


def test_failure_tombstone_never_claims_cleanup_without_sealed_session_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)
    monkeypatch.setattr(
        runner,
        "_capture_one_stream",
        lambda **_kwargs: (_ for _ in ()).throw(runner.SelectedStateRunError("synthetic failure")),
    )
    original_write = runner._write_new_json

    def fail_session_write(path: Path, document: dict[str, Any]) -> None:
        if path.name == "session.failure-safety.json":
            raise OSError("synthetic fsync failure")
        original_write(path, document)

    monkeypatch.setattr(runner, "_write_new_json", fail_session_write)
    with pytest.raises(runner.SelectedStateRunError, match="cleanup was not proven"):
        runner.execute_capture(
            plan_path=plan,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
            mute_boundary=_passed_mute,
            now=lambda: "2026-08-29T12:00:00+00:00",
        )
    failure = json.loads(
        (plan.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert failure["final_failure_cleanup_passed"] is False
    assert failure["failure_safety_evidence"]["session"] is None


def test_analyze_requires_capture_tombstone_and_exact_capture_set(tmp_path: Path) -> None:
    plan = _build(tmp_path)
    arbitrary = _write_json(tmp_path / "arbitrary.json", {"quality": "self-reported"})
    with pytest.raises(runner.SelectedStateRunError, match="capture execution tombstone"):
        runner.execute_qualification(
            plan_path=plan,
            evidence_path=arbitrary,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
        )


def test_reanalysis_rejects_nonexistent_raw_and_self_reported_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)
    contract, plan_sha = _load_plan(plan)
    context = runner._qualification_context(contract, plan_sha)
    missing = _tiny_binding(tmp_path, context, "missing")
    Path(missing["raw_iq_path"]).unlink()
    capture_set = {
        "schema": 1,
        "evidence_kind": "5g8_selected_state_raw_capture_set_v1",
        "mode": "static-bench",
        "context": context,
        "captures": [missing] * 9,
    }
    monkeypatch.setattr(
        runner.one_hot_runner, "_validate_one_hot_selector_control", lambda value: value
    )
    monkeypatch.setattr(
        runner.one_hot_runner,
        "_state_map",
        lambda _value: {"ALL_OFF": 8, **{state: i for i, state in enumerate(ANTENNA_STATES)}},
    )
    with pytest.raises(runner.SelectedStateRunError, match="raw IQ"):
        runner._reanalyze_capture_evidence(capture_set, contract=contract, plan_sha=plan_sha)
    capture_set["quality"] = {"accepted": True}
    with pytest.raises(runner.SelectedStateRunError, match="not bound"):
        runner._reanalyze_capture_evidence(capture_set, contract=contract, plan_sha=plan_sha)


def test_analysis_is_rf_inert_and_writes_immutable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _build(tmp_path)
    contract, plan_sha = _load_plan(plan)
    capture_set = {
        "schema": 1,
        "evidence_kind": "5g8_selected_state_raw_capture_set_v1",
        "mode": "static-bench",
        "context": runner._qualification_context(contract, plan_sha),
        "captures": [],
    }
    evidence_path = plan.parent / runner.CAPTURE_EVIDENCE_FILENAME
    runner._write_new_json(evidence_path, capture_set)
    runner._write_new_json(
        plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME,
        {
            "schema": 1,
            "evidence_kind": "5g8_selected_state_capture_started_v1",
            "started_at": "2026-08-29T12:01:00+00:00",
            "run_id": contract["run_id"],
            "mode": contract["mode"],
            "plan_path": str(plan),
            "plan_file_sha256": runner.sha256_path(plan),
            "plan_contract_sha256": plan_sha,
            "expected_capture_set_path": str(evidence_path),
            "run_id_burned": True,
            "hardware_access_authorized_only_for_this_action": True,
        },
    )
    scientific = _static_scientific_evidence(plan, tmp_path)
    monkeypatch.setattr(
        runner,
        "_reanalyze_capture_evidence",
        lambda *args, **kwargs: copy.deepcopy(scientific),
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("analysis touched hardware")

    monkeypatch.setattr(runner, "capture_continuous_safe_dds_tone", forbidden)
    monkeypatch.setattr(runner.leakage_runner, "_live_identity_boundary", forbidden)
    monkeypatch.setattr(runner.one_hot_runner, "_live_selector_boundary", forbidden)
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    result_path = runner.execute_qualification(
        plan_path=plan,
        evidence_path=evidence_path,
        runtime_bindings=_runtime(),
        selector_loader=_selector_loader,
        fixture_evidence_loader=_fixture_loader,
        selector_control_builder=_selector_control_builder,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["accepted"] is True
    assert result["result_accepted"] is True
    assert result["observation_count"] == 9
    assert os.stat(result_path).st_mode & 0o777 == 0o400
    assert (plan.parent / runner.ANALYSIS_TOMBSTONE_FILENAME).is_file()
    fixture, _ = runner._revalidate_plan_inputs(
        contract,
        runtime_bindings=_runtime(),
        selector_loader=_selector_loader,
        fixture_evidence_loader=_fixture_loader,
        selector_control_builder=_selector_control_builder,
    )
    recomputed = runner._reanalyze_prior_result(
        _file(result_path),
        expected_mode="static-bench",
        expected_kind=runner.STATIC_RESULT_KIND,
        label="static result",
        current_contract=contract,
        current_fixture=fixture,
        runtime_bindings=_runtime(),
        selector_loader=_selector_loader,
        fixture_evidence_loader=_fixture_loader,
        selector_control_builder=_selector_control_builder,
    )
    assert recomputed["accepted"] is True

    os.chmod(result_path, 0o600)
    forged = json.loads(result_path.read_text(encoding="utf-8"))
    forged["source_stream_ids"] = ["forged-stream"]
    result_path.write_bytes(runner._canonical_bytes(forged))
    os.chmod(result_path, 0o400)
    with pytest.raises(runner.SelectedStateRunError, match="independent reanalysis"):
        runner._reanalyze_prior_result(
            _file(result_path),
            expected_mode="static-bench",
            expected_kind=runner.STATIC_RESULT_KIND,
            label="static result",
            current_contract=contract,
            current_fixture=fixture,
            runtime_bindings=_runtime(),
            selector_loader=_selector_loader,
            fixture_evidence_loader=_fixture_loader,
            selector_control_builder=_selector_control_builder,
        )


def _fast20_preflight_contract(firmware_path: Path, config_path: Path) -> dict[str, Any]:
    return {
        "board_id": BOARD,
        "image_role": "fast20",
        "selector": {
            "sha256": _hash("sealed-selector"),
            "firmware_bin_sha256": _file(firmware_path)["sha256"],
            "profile_contract_sha256": _hash("profile"),
        },
        "selector_control": {
            "firmware_bin": _file(firmware_path),
            "openocd_config": _file(config_path),
        },
    }


def test_failed_fast20_image_readback_never_resets_or_touches_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_path = tmp_path / "fast20.bin"
    firmware_path.write_bytes(b"sealed-fast20-image")
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    contract = _fast20_preflight_contract(firmware_path, config_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        calls.append(command)
        return runner.subprocess.CompletedProcess(command, 1, "readback-out", "readback-err")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._live_target_image_preflight(
        contract,
        evidence_root=tmp_path / "live-evidence",
    )

    assert len(calls) == 1
    assert "reset run" not in calls[0][-1]
    assert calls[0][-1].count("dump_image") == 2
    assert f"0x{runner.leakage_runner.STM32C011_UID_ADDRESS:x}" in calls[0][-1]
    assert result["status"] == "failed"
    assert result["exact_byte_match"] is False
    assert result["preflight_command_succeeded"] is False
    assert result["reset_run_attempted"] is False
    assert result["reset_run_succeeded"] is False
    assert result["mailbox_access_performed"] is False
    assert result["target_kept_halted_or_unknown_on_failure"] is True


def test_mismatched_fast20_flash_or_uid_never_resets_unknown_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_path = tmp_path / "fast20.bin"
    firmware_path.write_bytes(b"sealed-fast20-image")
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    evidence_root = tmp_path / "live evidence"
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        calls.append(command)
        (evidence_root / "live-fast20-flash.bin").write_bytes(b"wrong-image")
        (evidence_root / "live-fast20-uid.bin").write_bytes(
            bytes.fromhex(BOARD.removeprefix("stm32c011-"))
        )
        return runner.subprocess.CompletedProcess(command, 0, "readback", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._live_target_image_preflight(
        _fast20_preflight_contract(firmware_path, config_path),
        evidence_root=evidence_root,
    )

    assert len(calls) == 1
    assert "reset run" not in calls[0][-1]
    assert result["exact_byte_match"] is False
    assert result["uid_exact_match"] is True
    assert result["reset_run_attempted"] is False


def test_fast20_exact_full_bin_and_uid_are_compared_before_single_reset_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_path = tmp_path / "fast20.bin"
    firmware = b"sealed-fast20-image"
    firmware_path.write_bytes(firmware)
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    evidence_root = tmp_path / "live-evidence"
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        calls.append(command)
        if len(calls) == 1:
            assert "reset run" not in command[-1]
            assert command[-1].count("dump_image") == 2
            (evidence_root / "live-fast20-flash.bin").write_bytes(firmware)
            (evidence_root / "live-fast20-uid.bin").write_bytes(
                bytes.fromhex(BOARD.removeprefix("stm32c011-"))
            )
            return runner.subprocess.CompletedProcess(command, 0, "readback", "")
        assert (evidence_root / "live-fast20-flash.bin").read_bytes() == firmware
        assert (evidence_root / "live-fast20-uid.bin").read_bytes().hex() == (
            BOARD.removeprefix("stm32c011-")
        )
        return runner.subprocess.CompletedProcess(command, 0, "reset", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._live_target_image_preflight(
        _fast20_preflight_contract(firmware_path, config_path),
        evidence_root=evidence_root,
    )

    assert len(calls) == 2
    assert "dump_image {" in calls[0][-1]
    assert calls[1][-1] == "init; reset run; shutdown"
    assert result["status"] == "passed"
    assert result["exact_byte_match"] is True
    assert result["uid_exact_match"] is True
    assert result["full_bin_and_uid_compared_before_reset_run"] is True
    assert result["reviewed_image_started_only_after_exact_match"] is True
    assert result["mailbox_access_performed"] is False


def test_fast20_admission_survives_exception_after_exact_bin_and_uid_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_path = tmp_path / "fast20.bin"
    firmware = b"sealed-fast20-image"
    firmware_path.write_bytes(firmware)
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    evidence_root = tmp_path / "live-evidence"
    admission_state: dict[str, Any] = {}
    calls = 0

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            (evidence_root / "live-fast20-flash.bin").write_bytes(firmware)
            (evidence_root / "live-fast20-uid.bin").write_bytes(
                bytes.fromhex(BOARD.removeprefix("stm32c011-"))
            )
            return runner.subprocess.CompletedProcess(command, 0, "readback", "")
        raise OSError("synthetic reset-run launch failure")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(OSError, match="reset-run"):
        runner._live_target_image_preflight(
            _fast20_preflight_contract(firmware_path, config_path),
            evidence_root=evidence_root,
            admission_state=admission_state,
        )

    assert admission_state["selector_image_admitted"] is True
    assert admission_state["selector_image_admission"]["observed_uid"] == (
        BOARD.removeprefix("stm32c011-")
    )
    assert (
        admission_state["selector_image_admission"]["observed_target_sha256"]
        == (_file(firmware_path)["sha256"])
    )


def test_fast20_failure_cleanup_halts_and_proves_direct_all_off_gpio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "rpi4-swd.cfg"
    config_path.write_text("adapter driver bcm2835gpio\n", encoding="utf-8")
    evidence_root = tmp_path / "cleanup-live-evidence"
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        calls.append(command)
        if len(calls) == 1:
            (evidence_root / "gpioa-moder-before.bin").write_bytes(
                (0xFFFFFFFF).to_bytes(4, "little")
            )
            return runner.subprocess.CompletedProcess(command, 0, "moder", "")
        (evidence_root / "gpioa-odr.bin").write_bytes((8).to_bytes(4, "little"))
        (evidence_root / "gpioa-moder.bin").write_bytes((0xFFFFFF55).to_bytes(4, "little"))
        return runner.subprocess.CompletedProcess(command, 0, "all-off", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner._live_fast20_all_off_cleanup(
        {
            "openocd_config": _file(config_path),
            "state_codes": {"ALL_OFF": 8},
        },
        evidence_root=evidence_root,
    )

    assert len(calls) == 2
    assert "mww" not in calls[0][-1]
    command = calls[1][-1]
    assert "init; halt" in command
    assert "reset run" not in command
    assert "mww 0x50000018 0x00070008" in command
    assert "mww 0x50000000 0xffffff55" in command
    assert command.count("dump_image") == 2
    assert result["status"] == "passed"
    assert result["mailbox_access_performed"] is False
    assert result["target_resume_command_issued"] is False
    assert result["gpio_output_latch_readback"]["masked_selector_code"] == 8
    assert result["gpio_output_mode_readback"]["masked_selector_modes"] == 0x55
    assert result["electrical_selector_all_off_proven"] is True


def test_sparse_fast20_cleanup_claim_cannot_pass_acceptance() -> None:
    assert (
        runner._fast20_failure_cleanup_passed(
            {
                "schema": 1,
                "evidence_kind": runner.FAST20_FAILURE_CLEANUP_KIND,
                "status": "passed",
                "selector_write_authorized_by_exact_image_and_uid_admission": True,
                "mailbox_access_performed": False,
                "target_resume_command_issued": False,
                "error": None,
            },
            selector_control={"state_codes": {"ALL_OFF": 8}},
        )
        is False
    )


def test_prepare_and_analyze_cli_paths_do_not_call_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = runner._parser()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("RF/OpenOCD boundary called")

    monkeypatch.setattr(runner, "capture_continuous_safe_dds_tone", forbidden)
    monkeypatch.setattr(runner, "_live_target_image_preflight", forbidden)
    parsed = parser.parse_args(["static-bench", "analyze", "--plan", str(tmp_path / "x")])
    assert parsed.action == "analyze"
    # Parsing/plan construction contains no implicit radio or selector access.
    plan = _build(tmp_path / "prepared")
    assert plan.is_file()
