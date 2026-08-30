from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from smateway import global_ledger
from smateway.port_pair_matrix import CALIBRATION_KIND, CELL_IDS, FIXTURE_KIND, canonical_sha256

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_port_pair_matrix.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_port_pair_matrix_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _termination(label: str) -> dict[str, object]:
    return {
        "termination_id": label,
        "identity_sha256": _hash(label),
        "rated_min_hz": 2_000_000_000,
        "rated_max_hz": 8_000_000_000,
        "impedance_ohm": 50.0,
        "maximum_input_dbm": 20.0,
    }


def _chain(label: str, receiver: str, *, permanent: bool, independent: bool) -> dict[str, object]:
    return {
        "chain_id": label,
        "identity_sha256": _hash(label),
        "assigned_receiver": receiver,
        "rated_min_hz": 2_000_000_000,
        "rated_max_hz": 8_000_000_000,
        "attenuation_db": 30.0,
        "attenuation_tolerance_db": 0.5,
        "maximum_input_dbm": 20.0,
        "permanently_installed": permanent,
        "removal_forbidden": permanent,
        "independent_of_rx1_chain": independent,
    }


def _fixture() -> dict[str, object]:
    return {
        "schema": 1,
        "fixture_kind": FIXTURE_KIND,
        "fixture_id": "runner-fixture",
        "center_frequency_hz": 5_800_000_000,
        "fixed_graph_sha256": _hash("fixed-graph"),
        "receiver_input_limit_dbm": -10.0,
        "required_safety_margin_db": 10.0,
        "rx1_protection": _chain("rx1-chain", "RX1", permanent=True, independent=False),
        "rx2_reference_chain": _chain("rx2-chain", "RX2", permanent=False, independent=True),
        "reference_distribution": {
            "identity_sha256": _hash("distribution"),
            "active_tx_reference_plane_sha256": _hash("tx-plane"),
            "minimum_path_loss_db": 3.0,
            "unused_output_termination": _termination("distribution-load"),
        },
        "inactive_tx_terminations": {
            "TX1": _termination("tx1-load"),
            "TX2": _termination("tx2-load"),
        },
        "test_receiver_terminations": {
            "RX1": _termination("rx1-load"),
            "RX2": _termination("rx2-load"),
        },
        "test_reference_plane_sha256s": {
            "RX1": _hash("rx1-test-plane"),
            "RX2": _hash("rx2-test-plane"),
        },
        "reference_plane_sha256s": {
            "RX1": _hash("rx1-reference-plane"),
            "RX2": _hash("rx2-reference-plane"),
        },
    }


def _complex(value: complex) -> dict[str, float]:
    return {"real": value.real, "imag": value.imag}


def _calibration(fixture_sha256: str) -> dict[str, object]:
    return {
        "schema": 1,
        "calibration_kind": CALIBRATION_KIND,
        "calibration_id": "runner-calibration",
        "fixture_sha256": fixture_sha256,
        "center_frequency_hz": 5_800_000_000,
        "receiver_calibrations": {
            "RX1": {
                "test_receiver_response": _complex(2.0 + 0.1j),
                "test_response_evidence_sha256": _hash("rx1-test-response"),
                "reference_chain_response": _complex(0.08 - 0.02j),
                "reference_response_evidence_sha256": _hash("rx1-reference-response"),
                "reference_chain_sha256": _hash("rx1-chain"),
                "test_reference_plane_sha256": _hash("rx1-test-plane"),
                "reference_plane_sha256": _hash("rx1-reference-plane"),
            },
            "RX2": {
                "test_receiver_response": _complex(3.0 - 0.2j),
                "test_response_evidence_sha256": _hash("rx2-test-response"),
                "reference_chain_response": _complex(0.04 + 0.01j),
                "reference_response_evidence_sha256": _hash("rx2-reference-response"),
                "reference_chain_sha256": _hash("rx2-chain"),
                "test_reference_plane_sha256": _hash("rx2-test-plane"),
                "reference_plane_sha256": _hash("rx2-reference-plane"),
            },
        },
    }


def _native_attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(runner._REQUIRED_LIBIIO_DIRECTORY / "libiio.so.0.25"),
        "library_path_from_proc_maps": True,
        "library_sha256": "d0a18bddcb54d182262acb2a9e31a88c81618cb43789320b8381c149777bef89",
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {"iio_device_get_kernel_buffers_count": True},
        "loader_search_path_first": "/usr/local/lib",
    }


def _contract(
    tmp_path: Path,
    *,
    cell_id: str = "TX1_RX1",
    repeat_index: int = 1,
    run_id: str = "matrix-run-1",
) -> dict[str, Any]:
    fixture = _fixture()
    calibration = _calibration(canonical_sha256(fixture))
    return runner._build_plan_contract(
        run_id=run_id,
        campaign_id="matrix-campaign",
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        cell_id=cell_id,
        repeat_index=repeat_index,
        fixture_document=fixture,
        fixture_file={"path": "/fixture.json", "sha256": _hash("fixture-file"), "size_bytes": 1},
        calibration_document=calibration,
        calibration_file={
            "path": "/calibration.json",
            "sha256": _hash("calibration-file"),
            "size_bytes": 1,
        },
        source_attestation={"commit": "a" * 40, "files": [], "source_files_sha256": _hash("src")},
        dependency_attestation={"commit": "b" * 40, "files": []},
        native_attestation=_native_attestation(),
        state_root=tmp_path / "state",
    )


def _prepared(
    tmp_path: Path, *, run_id: str = "matrix-run-1"
) -> tuple[dict[str, Any], Path, Path, global_ledger.LocalLedgerBackend]:
    contract = _contract(tmp_path, run_id=run_id)
    root = Path(contract["storage"]["condition_root"])
    plan_path = root / runner.PLAN_FILENAME
    manifest_path = root / runner.MANIFEST_FILENAME
    storage = global_ledger.provision_local_test_storage(tmp_path / "ledger-authority")
    backend = global_ledger.LocalLedgerBackend(storage=storage)
    runner._prepare_plan(
        plan_path,
        manifest_path,
        contract,
        ledger_backend=backend,
    )
    return contract, plan_path, manifest_path, backend


def _execution_receipts(
    contract: Mapping[str, Any],
    plan_path: Path,
    manifest_path: Path,
    ledger_backend: global_ledger.LedgerBackend,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    reservation = runner._validate_reservation_receipt(
        contract,
        plan_path=plan_path,
        manifest_path=manifest_path,
        ledger_backend=ledger_backend,
    )
    _reservation_path, _guard_path, burn_path, _failure_path = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    if burn_path.exists():
        burn = runner._validate_execution_burn_receipt(
            contract,
            plan_path=plan_path,
            manifest_path=manifest_path,
            reservation_receipt=reservation,
            ledger_backend=ledger_backend,
        )
        burn_document = burn["document"]
        attempt_started = {
            "started_at": burn_document["attempt_started_at"],
            "started_monotonic_ns": burn_document["attempt_started_monotonic_ns"],
            "started_clock_boot_id": burn_document["attempt_started_clock_boot_id"],
        }
    else:
        attempt_started = runner._stamp_fields(runner._clock_stamp(), "started")
        burn = runner._acquire_execution_burn(
            contract,
            plan_path=plan_path,
            manifest_path=manifest_path,
            reservation_receipt=reservation,
            attempt_started=attempt_started,
            ledger_backend=ledger_backend,
        )
    execution_path = plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    if not execution_path.exists():
        runner._execution_tombstone(
            execution_path,
            contract,
            plan_path,
            reservation_receipt=reservation,
            burn_receipt=burn,
            attempt_started=attempt_started,
        )
    marker = runner._validate_execution_tombstone_receipt(
        contract,
        plan_path=plan_path,
        reservation_receipt=reservation,
        burn_receipt=burn,
        attempt_started=attempt_started,
    )
    return reservation, burn, marker, attempt_started


def _passed_mute(serial: str, purpose: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": purpose,
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "tx_gain_readback_db_by_channel": [-80.0, -80.0],
        "dds_scale_readback": [0.0] * 8,
        "error": None,
    }


def _rewrite_sealed_json(path: Path, document: Mapping[str, Any]) -> None:
    path.chmod(0o600)
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o400)


def _identity(serial: str, uri: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": runner.IDENTITY_PURPOSE,
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": uri,
        "exact_uri_match": True,
        "sysfs_path": "/sys/bus/usb/devices/1-2.3",
        "attestation": runner.IDENTITY_ATTESTATION,
        "scan_mutates_radio_state": False,
        "error": None,
    }


def _accepted_boundary_result(
    contract: Mapping[str, Any], execution_context: Mapping[str, Any]
) -> dict[str, Any]:
    reservation = execution_context["permanent_run_reservation"]
    burn = execution_context["irreversible_execution_burn"]
    marker = execution_context["execution_tombstone"]
    attempt_started = execution_context["attempt_started"]
    assert isinstance(reservation, Mapping)
    assert isinstance(burn, Mapping)
    assert isinstance(marker, Mapping)
    assert isinstance(attempt_started, Mapping)
    identity = runner._call_identity(
        _identity,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
    )
    initial = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="pre_preflight_exact_mute",
    )
    preflight_started = runner._clock_stamp()
    preflight_completed = runner._clock_stamp()
    post_preflight = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="post_preflight_exact_mute",
    )
    main_started = runner._clock_stamp()
    main_completed = runner._clock_stamp()
    post_main = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="post_main_exact_mute",
    )
    final = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose=runner.FINAL_ACCEPTANCE_MUTE_PURPOSE,
    )
    capture_timeline = {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_capture_mute_timeline_v1",
        "preflight_capture": runner._capture_timing(
            purpose="preflight_capture",
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            started=preflight_started,
            completed=preflight_completed,
        ),
        "post_preflight_mute": post_preflight,
        "main_capture": runner._capture_timing(
            purpose="main_capture",
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            started=main_started,
            completed=main_completed,
        ),
        "post_main_mute": post_main,
    }
    safety = runner._validated_execution_safety(
        identity=identity,
        initial_mute=initial,
        final_mute=final,
        capture_timeline=capture_timeline,
        attempt_started=attempt_started,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
    )
    return {
        "accepted_stream_count": 2,
        "preflight_artifact": {"artifact_id": "preflight"},
        "main_artifact": {"artifact_id": "main"},
        "identity_preflight": identity,
        "initial_mute": initial,
        "capture_timeline": capture_timeline,
        "final_mute": final,
        "execution_safety_sha256": safety["evidence_sha256"],
        "identity_preflight_sha256": safety["identity_preflight_sha256"],
        "initial_mute_sha256": safety["initial_mute_sha256"],
        "capture_timeline_sha256": safety["capture_timeline_sha256"],
        "final_mute_sha256": safety["final_mute_sha256"],
        "execution_tombstone_receipt_sha256": safety["execution_tombstone_receipt_sha256"],
        "permanent_run_reservation": dict(reservation),
        "irreversible_execution_burn": dict(burn),
        "execution_tombstone": dict(marker),
    }


@pytest.mark.parametrize("cell_id", CELL_IDS)
def test_plan_binds_each_exact_protected_cell_and_two_local_streams(
    tmp_path: Path, cell_id: str
) -> None:
    contract = _contract(tmp_path, cell_id=cell_id)
    condition = contract["condition"]

    assert condition["cell_id"] == cell_id
    assert condition["active_tx"] in {"TX1", "TX2"}
    assert condition["inactive_tx"] != condition["active_tx"]
    assert condition["test_receiver"] in {"RX1", "RX2"}
    assert condition["reference_receiver"] != condition["test_receiver"]
    assert contract["fixture"]["rx1_protection_sha256"] == _hash("rx1-chain")
    assert contract["fixture"]["reference_chain_sha256"] in {
        _hash("rx1-chain"),
        _hash("rx2-chain"),
    }
    assert contract["execution"]["two_streams_per_repeat"] is True
    assert contract["execution"]["raw_channel_amplitude_comparison_forbidden"] is True
    assert contract["storage"]["local_rpi_only"] is True
    assert contract["storage"]["pluto_storage_forbidden"] is True
    assert "/pluto-usb-captures/" in contract["storage"]["capture_root"]


def test_tone_plan_selects_only_active_tx_and_uses_exact_reference_attenuation(
    tmp_path: Path,
) -> None:
    tx1 = _contract(tmp_path, cell_id="TX1_RX1")
    tx2 = _contract(tmp_path, cell_id="TX2_RX2", run_id="matrix-run-2")

    plan1 = runner._tone_plan(tx1, runner.CAPTURE_TX_GAIN_DB)
    plan2 = runner._tone_plan(tx2, runner.CAPTURE_TX_GAIN_DB)

    assert plan1.tx_channel == 0
    assert plan2.tx_channel == 1
    assert plan1.tx_hardware_gain_db == -20.0
    assert plan1.path_attenuation_before_load_db == pytest.approx(
        tx1["fixture"]["path_attenuation_before_reference_receiver_db"]
    )
    assert plan1.worst_case_load_input_dbm <= plan1.load_input_limit_dbm - plan1.required_margin_db


def test_every_condition_binds_one_common_exact_twenty_condition_campaign_plan(
    tmp_path: Path,
) -> None:
    first = _contract(tmp_path, cell_id="TX1_RX1", repeat_index=1)
    last = _contract(
        tmp_path,
        cell_id="TX2_RX2",
        repeat_index=5,
        run_id="matrix-run-last",
    )

    assert first["campaign_plan"]["sha256"] == last["campaign_plan"]["sha256"]
    campaign = first["campaign_plan"]["contract"]
    assert campaign["condition_count"] == 20
    assert len(campaign["conditions"]) == 20
    assert len({(item["cell_id"], item["repeat_index"]) for item in campaign["conditions"]}) == 20
    assert campaign["accepted_main_repeats_per_cell"] == 5


def test_confirmation_gate_requires_every_physical_fact_and_exact_cell_token(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    values = {
        "confirm_no_antennas": True,
        "confirm_inactive_tx_physically_terminated": True,
        "confirm_test_receiver_terminated": True,
        "confirm_rx1_protection_unchanged": True,
        "confirm_separate_reference_attenuator": True,
        "confirm_reference_planes_match": True,
        "confirm_no_movement": True,
        "confirm_topology_token": contract["condition"]["topology_token"],
    }

    confirmed = runner._validate_execution_confirmations(SimpleNamespace(**values), contract)
    assert confirmed["rx1_protection_unchanged"] is True
    values["confirm_test_receiver_terminated"] = False
    with pytest.raises(runner.PortPairRunError, match="test_receiver_terminated"):
        runner._validate_execution_confirmations(SimpleNamespace(**values), contract)
    values["confirm_test_receiver_terminated"] = True
    values["confirm_topology_token"] = "WRONG"
    with pytest.raises(runner.PortPairRunError, match="confirm-topology-token"):
        runner._validate_execution_confirmations(SimpleNamespace(**values), contract)


def test_rejects_pluto_or_removable_storage_root() -> None:
    fixture = _fixture()
    calibration = _calibration(canonical_sha256(fixture))

    with pytest.raises(runner.PortPairRunError, match="local RPi storage"):
        runner._build_plan_contract(
            run_id="bad-storage",
            campaign_id="matrix-campaign",
            board_id="board-a",
            serial="serial-a",
            uri="usb:1.2.3",
            cell_id="TX1_RX1",
            repeat_index=1,
            fixture_document=fixture,
            fixture_file={"sha256": _hash("fixture")},
            calibration_document=calibration,
            calibration_file={"sha256": _hash("calibration")},
            source_attestation={"commit": "a" * 40},
            dependency_attestation={"commit": "b" * 40},
            native_attestation=_native_attestation(),
            state_root=Path("/media/pluto/state"),
        )


def test_success_burns_run_and_accepts_exactly_preflight_plus_main(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(tmp_path)

    def execute(selected_contract: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return _accepted_boundary_result(selected_contract, context)

    manifest = runner._execute_prepared(
        plan_path=plan_path,
        manifest_path=manifest_path,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=execute,
    )

    assert manifest["status"] == "complete"
    assert manifest["accepted_stream_count"] == 2
    execution = manifest_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    assert execution.is_file()
    assert execution.stat().st_mode & stat.S_IWUSR == 0
    _reservation, _guard, _burn, failure_slot = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    failure_state = failure_slot.stat(follow_symlinks=False)
    assert stat.S_ISREG(failure_state.st_mode)
    assert stat.S_IMODE(failure_state.st_mode) == global_ledger.PREPARED_SLOT_MODE
    assert failure_state.st_nlink == 2
    assert failure_state.st_size == 0
    with pytest.raises(runner.PortPairRunError, match="never-attempted"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )


def test_failure_writes_immutable_tombstone_and_accepts_nothing(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(tmp_path, run_id="matrix-failed")

    def fail(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        raise OSError("ENODATA")

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=fail,
            mute_boundary=_passed_mute,
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failure = manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME
    assert manifest["status"] == "failed"
    assert manifest["accepted_stream_count"] == 0
    assert failure.is_file()
    assert failure.stat().st_mode & stat.S_IWUSR == 0
    assert json.loads(failure.read_text(encoding="utf-8"))["accepted_artifacts"] is False


def test_wrong_stream_count_fails_closed_and_writes_failure_tombstone(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="one-stream-only"
    )

    with pytest.raises(runner.PortPairRunError, match="exact streams"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=lambda _contract, _context: {"accepted_stream_count": 1},
            mute_boundary=_passed_mute,
        )
    assert (manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).is_file()


def test_capture_error_attempts_final_mute_and_persists_nothing(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="capture-enodata"
    )
    reservation, burn, marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    calls: list[str] = []

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("ENODATA")

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_condition(
            contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            attempt_started=attempt_started,
            capture_boundary=capture,
            mute_boundary=mute,
            identity_boundary=_identity,
            native_boundary=_native_attestation,
        )

    assert calls == ["pre_preflight_exact_mute", "final_acceptance_exact_mute"]
    assert not Path(contract["storage"]["capture_root"]).exists()


def test_mute_evidence_must_include_exact_both_gain_and_all_dds_readback() -> None:
    # The strict predicate is exercised with a complete run-bound artifact below.
    contract = {"run_id": "unit", "configuration": {"serial": "serial-a", "uri": "usb:1.2"}}
    reservation = {"receipt": "reservation"}
    burn = {"receipt": "burn"}
    marker = {"sha256": "c" * 64}
    passed = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="final_acceptance_exact_mute",
    )
    assert runner._mute_passed(
        passed,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="final_acceptance_exact_mute",
    )
    missing = dict(passed)
    missing.pop("dds_scale_readback")
    assert not runner._mute_passed(
        missing,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose="final_acceptance_exact_mute",
    )


def test_runner_binds_source_dependency_native_and_has_no_raw_compare_flag(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    options = {option for action in runner._parser()._actions for option in action.option_strings}

    assert contract["source"]["smateway"]["commit"] == "a" * 40
    assert contract["source"]["pluto_plus_utils"]["commit"] == "b" * 40
    assert len(contract["source"]["native_libiio_sha256"]) == 64
    assert "src/smateway/port_pair_matrix.py" in runner.SOURCE_FILES
    assert "scripts/run_5g8_port_pair_matrix.py" in runner.SOURCE_FILES
    assert "--confirm-rx1-protection-unchanged" in options
    assert "--confirm-separate-reference-attenuator" in options
    assert "--compare-raw-channels" not in options


@pytest.mark.parametrize("repeat_index", [0, 6])
def test_plan_rejects_repeat_outside_exact_five(tmp_path: Path, repeat_index: int) -> None:
    with pytest.raises(runner.PortPairRunError, match="exactly 1..5"):
        _contract(tmp_path, repeat_index=repeat_index)


def test_local_state_compares_nearest_existing_filesystem_device(tmp_path: Path) -> None:
    planned = tmp_path / "not-created" / "state"
    assert runner._safe_local_state_root(planned) == planned.absolute()
    with pytest.raises(runner.PortPairRunError, match="local RPi storage device"):
        runner._safe_local_state_root(Path("/proc/smateway-port-pair"))


def test_execute_rechecks_local_storage_before_burning_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan, manifest, ledger_backend = _prepared(tmp_path)
    invoked = False

    def reject(_path: Path, *, label: str) -> Path:
        raise runner.FileArtifactAdmissionError(f"{label} is not on local RPi storage device")

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    monkeypatch.setattr(runner, "assert_local_rpi_storage", reject)
    with pytest.raises(runner.PortPairRunError, match="local RPi storage device"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert not invoked
    assert not (plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME).exists()


@pytest.mark.parametrize("failure_stage", ("identity", "native", "initial_mute"))
def test_every_pre_capture_failure_attempts_mandatory_final_exact_mute(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"pre-capture-{failure_stage}"
    )
    reservation, burn, marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    calls: list[str] = []

    def identity(serial: str, uri: str) -> dict[str, Any]:
        if failure_stage == "identity":
            value = _identity(serial, uri)
            value["status"] = "failed"
            value["error"] = {"type": "IdentityMismatch", "message": "wrong device"}
            return value
        return _identity(serial, uri)

    def native() -> dict[str, Any]:
        if failure_stage == "native":
            value = _native_attestation()
            value["library_sha256"] = "f" * 64
            return value
        return _native_attestation()

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        value = _passed_mute(serial, purpose)
        if failure_stage == "initial_mute" and purpose == "pre_preflight_exact_mute":
            value["status"] = "failed"
            value["error"] = {"type": "ReadbackError", "message": "not muted"}
        return value

    with pytest.raises((runner.PortPairRunError, ValueError)):
        runner._execute_condition(
            contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            attempt_started=attempt_started,
            capture_boundary=lambda *_args, **_kwargs: pytest.fail("capture was reached"),
            mute_boundary=mute,
            identity_boundary=identity,
            native_boundary=native,
        )

    assert calls[-1] == runner.FINAL_ACCEPTANCE_MUTE_PURPOSE
    if failure_stage == "initial_mute":
        assert calls == ["pre_preflight_exact_mute", runner.FINAL_ACCEPTANCE_MUTE_PURPOSE]
    else:
        assert calls == [runner.FINAL_ACCEPTANCE_MUTE_PURPOSE]


def test_failed_transaction_seals_recomputed_cleanup_evidence(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="sealed-cleanup"
    )
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=lambda _contract, _context: (_ for _ in ()).throw(OSError("ENODATA")),
            mute_boundary=mute,
        )

    tombstone = json.loads(
        (manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    cleanup = tombstone["failure_cleanup_evidence"]
    reservation = tombstone["permanent_run_reservation"]
    burn = tombstone["irreversible_execution_burn"]
    marker = tombstone["execution_tombstone_evidence"]
    assert calls == [runner.FAILURE_CLEANUP_MUTE_PURPOSE]
    assert cleanup == runner._validated_failure_cleanup(
        cleanup["exact_mute"],
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
    )
    assert tombstone["failure_cleanup_evidence_sha256"] == canonical_sha256(cleanup)
    assert tombstone["final_failure_cleanup_passed"] is True


def test_failed_cleanup_is_sealed_but_cannot_be_claimed_as_safe(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="failed-cleanup"
    )

    def failed_mute(serial: str, purpose: str) -> dict[str, Any]:
        value = _passed_mute(serial, purpose)
        value["status"] = "failed"
        value["error"] = {"type": "ReadbackError", "message": "mute not proven"}
        return value

    with pytest.raises(runner.PortPairRunError, match="final exact mute was not proven"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=lambda _contract, _context: (_ for _ in ()).throw(OSError("ENODATA")),
            mute_boundary=failed_mute,
        )

    tombstone = json.loads(
        (manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert tombstone["final_failure_cleanup_passed"] is False
    assert tombstone["failure_cleanup_evidence"]["exact_mute_passed"] is False


def test_failure_tombstone_rejects_caller_asserted_cleanup_boolean(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="forged-cleanup"
    )
    reservation, burn, marker, _attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    exact_mute = runner._call_mute(
        _passed_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
        purpose=runner.FAILURE_CLEANUP_MUTE_PURPOSE,
    )
    cleanup = runner._validated_failure_cleanup(
        exact_mute,
        contract=contract,
        reservation_receipt=reservation,
        burn_receipt=burn,
        execution_marker_receipt=marker,
    )
    cleanup["exact_mute_passed"] = False
    failure_path = plan_path.parent / runner.FAILURE_TOMBSTONE_FILENAME

    with pytest.raises(runner.PortPairRunError, match="differs from recomputed"):
        runner._failure_tombstone(
            failure_path,
            contract,
            plan_path,
            OSError("ENODATA"),
            failure_cleanup=cleanup,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            execution_tombstone_evidence=None,
        )
    assert not failure_path.exists()


@pytest.mark.parametrize(
    "history_kind",
    (
        "condition-result",
        "execution-tombstone",
        "failure-tombstone",
        "capture-root",
        "staging-root",
        "quarantine-root",
    ),
)
def test_surviving_run_history_blocks_rolled_back_manifest_before_hardware(
    tmp_path: Path,
    history_kind: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"rollback-{history_kind}"
    )
    capture_root = Path(contract["storage"]["capture_root"])
    if history_kind == "condition-result":
        (plan_path.parent / runner.OBSERVATION_FILENAME).write_text("{}", encoding="utf-8")
    elif history_kind == "execution-tombstone":
        (plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME).write_text("{}", encoding="utf-8")
    elif history_kind == "failure-tombstone":
        (plan_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).write_text("{}", encoding="utf-8")
    elif history_kind == "capture-root":
        capture_root.mkdir(parents=True)
    elif history_kind == "staging-root":
        (capture_root.parent / f".{capture_root.name}.staging").mkdir(parents=True)
    else:
        (capture_root.parent / ".failed" / capture_root.name).mkdir(parents=True)
    invoked = False

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    with pytest.raises(runner.PortPairRunError, match="surviving run-derived history"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert not invoked
    execution_path = plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    if history_kind == "execution-tombstone":
        assert execution_path.read_text(encoding="utf-8") == "{}"
    else:
        assert not execution_path.exists()


def test_capture_storage_symlink_ancestry_is_rejected_before_hardware(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="capture-symlink"
    )
    capture_root = Path(contract["storage"]["capture_root"])
    capture_branch = next(
        parent for parent in capture_root.parents if parent.name == "pluto-usb-captures"
    )
    capture_branch.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "redirected-captures"
    target.mkdir()
    capture_branch.symlink_to(target, target_is_directory=True)
    invoked = False

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    with pytest.raises(
        (runner.PortPairRunError, runner.FileArtifactAdmissionError), match="symlink"
    ):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert not invoked


@pytest.mark.parametrize(
    ("artifact", "mutation"),
    (
        ("identity", "extra-key"),
        ("identity", "missing-key"),
        ("identity", "integer-boolean"),
        ("identity", "reversed-time"),
        ("mute", "extra-key"),
        ("mute", "missing-key"),
        ("mute", "integer-readback"),
        ("mute", "reversed-time"),
    ),
)
def test_safety_artifacts_are_schema_closed_and_semantically_typed(
    tmp_path: Path,
    artifact: str,
    mutation: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"strict-{artifact}-{mutation}"
    )
    reservation, burn, marker, _attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    if artifact == "identity":
        value = runner._call_identity(
            _identity,
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
        )
        if mutation == "extra-key":
            value["forged"] = True
        elif mutation == "missing-key":
            value.pop("attestation")
        elif mutation == "integer-boolean":
            value["exact_uri_match"] = 1
        else:
            value["started_at"], value["completed_at"] = (
                "2026-08-30T00:00:01+00:00",
                "2026-08-30T00:00:00+00:00",
            )
        with pytest.raises(runner.PortPairRunError):
            runner._validate_identity_evidence(
                value,
                contract=contract,
                reservation_receipt=reservation,
                burn_receipt=burn,
                execution_marker_receipt=marker,
            )
    else:
        value = runner._call_mute(
            _passed_mute,
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
            purpose="pre_preflight_exact_mute",
        )
        if mutation == "extra-key":
            value["forged"] = True
        elif mutation == "missing-key":
            value.pop("attestation")
        elif mutation == "integer-readback":
            value["tx_gain_readback_db_by_channel"] = [-80, -80]
        else:
            value["started_at"], value["completed_at"] = (
                "2026-08-30T00:00:01+00:00",
                "2026-08-30T00:00:00+00:00",
            )
        with pytest.raises(runner.PortPairRunError):
            runner._validate_mute_evidence(
                value,
                contract=contract,
                reservation_receipt=reservation,
                burn_receipt=burn,
                execution_marker_receipt=marker,
                purpose="pre_preflight_exact_mute",
            )


@pytest.mark.parametrize("clock_axis", ("utc", "monotonic"))
@pytest.mark.parametrize(
    ("phase", "expected_label"),
    (
        ("reservation-attempt", "reservation→attempt start→execution burn"),
        ("attempt-burn", "reservation→attempt start→execution burn"),
        ("burn-marker", "reservation→attempt start→burn→execution tombstone"),
    ),
)
def test_pre_hardware_authorization_rejects_impossible_runner_clock_order(
    tmp_path: Path,
    phase: str,
    expected_label: str,
    clock_axis: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"clock-{phase}-{clock_axis}"
    )
    reservation, burn, _marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    _reservation_path, _guard_path, burn_path, _failure_path = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    if phase in {"reservation-attempt", "attempt-burn"}:
        document = copy.deepcopy(burn["document"])
        if phase == "reservation-attempt":
            if clock_axis == "utc":
                document["attempt_started_at"] = "2000-01-01T00:00:00+00:00"
            else:
                document["attempt_started_monotonic_ns"] = (
                    reservation["document"]["reserved_monotonic_ns"] - 1
                )
        elif clock_axis == "utc":
            document["burned_at"] = "2000-01-01T00:00:00+00:00"
        else:
            document["burned_monotonic_ns"] = document["attempt_started_monotonic_ns"] - 1
        _rewrite_sealed_json(burn_path, document)
        with pytest.raises(runner.PortPairRunError, match=expected_label):
            runner._validate_execution_burn_receipt(
                contract,
                plan_path=plan_path,
                manifest_path=manifest_path,
                reservation_receipt=reservation,
                ledger_backend=ledger_backend,
            )
        return

    execution_path = plan_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    document = json.loads(execution_path.read_text(encoding="utf-8"))
    if clock_axis == "utc":
        document["created_at"] = "2000-01-01T00:00:00+00:00"
    else:
        document["created_monotonic_ns"] = burn["document"]["burned_monotonic_ns"] - 1
    _rewrite_sealed_json(execution_path, document)
    with pytest.raises(runner.PortPairRunError, match=expected_label):
        runner._validate_execution_tombstone_receipt(
            contract,
            plan_path=plan_path,
            reservation_receipt=reservation,
            burn_receipt=burn,
            attempt_started=attempt_started,
        )


@pytest.mark.parametrize("clock_axis", ("utc", "monotonic"))
@pytest.mark.parametrize(
    ("prior_name", "prior_prefix", "target_name", "target_prefix"),
    (
        ("marker", "created", "identity", "started"),
        ("identity", "completed", "initial", "started"),
        ("initial", "completed", "preflight", "started"),
        ("preflight", "completed", "post_preflight", "started"),
        ("post_preflight", "completed", "main", "started"),
        ("main", "completed", "post_main", "started"),
        ("post_main", "completed", "final", "started"),
    ),
)
def test_execution_safety_rejects_each_impossible_adjacent_clock_order(
    tmp_path: Path,
    prior_name: str,
    prior_prefix: str,
    target_name: str,
    target_prefix: str,
    clock_axis: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"safety-clock-{prior_name}-{target_name}-{clock_axis}"
    )
    reservation, burn, marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    result = copy.deepcopy(
        _accepted_boundary_result(
            contract,
            {
                "attempt_started": attempt_started,
                "permanent_run_reservation": reservation,
                "irreversible_execution_burn": burn,
                "execution_tombstone": marker,
            },
        )
    )
    timeline = result["capture_timeline"]
    events = {
        "marker": marker["document"],
        "identity": result["identity_preflight"],
        "initial": result["initial_mute"],
        "preflight": timeline["preflight_capture"],
        "post_preflight": timeline["post_preflight_mute"],
        "main": timeline["main_capture"],
        "post_main": timeline["post_main_mute"],
        "final": result["final_mute"],
    }
    prior = events[prior_name]
    target = events[target_name]
    if clock_axis == "utc":
        target[f"{target_prefix}_at"] = "2000-01-01T00:00:00+00:00"
    else:
        target[f"{target_prefix}_monotonic_ns"] = prior[f"{prior_prefix}_monotonic_ns"] - 1

    with pytest.raises(runner.PortPairRunError, match="required order"):
        runner._validated_execution_safety(
            identity=result["identity_preflight"],
            initial_mute=result["initial_mute"],
            final_mute=result["final_mute"],
            capture_timeline=timeline,
            attempt_started=attempt_started,
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
        )


def test_execution_safety_rejects_cross_boot_timeline(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="safety-cross-boot"
    )
    reservation, burn, marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    result = copy.deepcopy(
        _accepted_boundary_result(
            contract,
            {
                "attempt_started": attempt_started,
                "permanent_run_reservation": reservation,
                "irreversible_execution_burn": burn,
                "execution_tombstone": marker,
            },
        )
    )
    identity = result["identity_preflight"]
    identity["started_clock_boot_id"] = "00000000-0000-0000-0000-000000000001"
    identity["completed_clock_boot_id"] = "00000000-0000-0000-0000-000000000001"

    with pytest.raises(runner.PortPairRunError, match="crosses a kernel boot"):
        runner._validated_execution_safety(
            identity=identity,
            initial_mute=result["initial_mute"],
            final_mute=result["final_mute"],
            capture_timeline=result["capture_timeline"],
            attempt_started=attempt_started,
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
        )


@pytest.mark.parametrize("clock_axis", ("utc", "monotonic"))
def test_completed_attempt_rejects_completion_before_final_mute(
    tmp_path: Path, clock_axis: str
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"completion-clock-{clock_axis}"
    )
    reservation, burn, marker, attempt_started = _execution_receipts(
        contract, plan_path, manifest_path, ledger_backend
    )
    result = _accepted_boundary_result(
        contract,
        {
            "attempt_started": attempt_started,
            "permanent_run_reservation": reservation,
            "irreversible_execution_burn": burn,
            "execution_tombstone": marker,
        },
    )
    attempt = {
        **attempt_started,
        **runner._stamp_fields(runner._clock_stamp(), "completed"),
    }
    if clock_axis == "utc":
        attempt["completed_at"] = "2000-01-01T00:00:00+00:00"
    else:
        attempt["completed_monotonic_ns"] = result["final_mute"]["completed_monotonic_ns"] - 1

    with pytest.raises(runner.PortPairRunError, match="final mute→attempt completion"):
        runner._validate_completed_attempt_timeline(
            attempt=attempt,
            result=result,
            contract=contract,
            reservation_receipt=reservation,
            burn_receipt=burn,
            execution_marker_receipt=marker,
        )


def test_external_burn_survives_complete_local_run_rollback(tmp_path: Path) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(tmp_path, run_id="full-rollback")
    prepared_snapshot = tmp_path / "prepared-snapshot"
    shutil.copytree(plan_path.parent, prepared_snapshot)

    def execute(selected_contract: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return _accepted_boundary_result(selected_contract, context)

    runner._execute_prepared(
        plan_path=plan_path,
        manifest_path=manifest_path,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=execute,
    )
    completed_root = tmp_path / "completed-forensic-root"
    plan_path.parent.rename(completed_root)
    shutil.copytree(prepared_snapshot, plan_path.parent)
    invoked = False

    def replay(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    with pytest.raises(runner.PortPairRunError, match="already burned|reservation identity"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=replay,
        )
    assert not invoked


def test_permanent_reservation_rejects_moved_and_recreated_prepared_root(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(tmp_path, run_id="moved-root")
    original_root = plan_path.parent
    preserved = tmp_path / "preserved-prepared-root"
    original_root.rename(preserved)
    shutil.copytree(preserved, original_root)
    invoked = False

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    with pytest.raises(runner.PortPairRunError, match="reservation identity"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert not invoked


@pytest.mark.parametrize("failure_point", ("burn-receipt-hash", "marker-write", "marker-hash"))
def test_every_post_burn_marker_gap_mutes_and_seals_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"post-burn-{failure_point}"
    )
    original_hash = runner.sha256_path
    original_write = runner._write_immutable_json
    injected = False

    def hash_boundary(path: Path) -> str:
        nonlocal injected
        if failure_point == "marker-hash" and path.name == runner.EXECUTION_TOMBSTONE_FILENAME:
            injected = True
            raise OSError(f"injected {failure_point}")
        return original_hash(path)

    def write_boundary(path: Path, document: Mapping[str, Any]) -> None:
        nonlocal injected
        if (
            not injected
            and failure_point == "marker-write"
            and path.name == runner.EXECUTION_TOMBSTONE_FILENAME
        ):
            injected = True
            raise OSError("injected marker-write")
        original_write(path, document)

    monkeypatch.setattr(runner, "sha256_path", hash_boundary)
    monkeypatch.setattr(runner, "_write_immutable_json", write_boundary)
    selected_backend: global_ledger.LedgerBackend = ledger_backend
    if failure_point == "burn-receipt-hash":

        class WriteThenFailBackend:
            def storage(self) -> Mapping[str, Any]:
                return ledger_backend.storage()

            def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
                nonlocal injected
                response = ledger_backend.mutate(request)
                if request.get("operation") == "burn_run":
                    injected = True
                    raise OSError("injected burn-receipt-hash")
                return response

        selected_backend = WriteThenFailBackend()
    elif failure_point == "marker-hash":

        class AuthorityUnavailableAfterHashBackend:
            def storage(self) -> Mapping[str, Any]:
                if injected:
                    raise OSError("authority unavailable during rescue")
                return ledger_backend.storage()

            def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
                return ledger_backend.mutate(request)

        selected_backend = AuthorityUnavailableAfterHashBackend()
    mute_calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        mute_calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="injected"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=selected_backend,
            execute_boundary=lambda _contract, _context: pytest.fail(
                "execution boundary was reached"
            ),
            mute_boundary=mute,
        )

    failure_path = plan_path.parent / runner.FAILURE_TOMBSTONE_FILENAME
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert injected
    assert mute_calls == [runner.FAILURE_CLEANUP_MUTE_PURPOSE]
    assert failure["final_failure_cleanup_passed"] is True
    assert failure["error"]["message"] == f"injected {failure_point}"
    degraded = failure["degraded_execution_authorization"]
    assert degraded["burn_marker_raw_state"]["state"] == "regular_file_present"
    assert degraded["receipt_or_hash_error"]["message"] == f"injected {failure_point}"
    assert failure["normal_burn_or_marker_receipt_not_claimed"] is True
    assert manifest["status"] == "failed"


def test_partial_global_burn_consumption_uses_degraded_exact_mute_and_records_failure(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="partial-global-burn"
    )

    def partial_burn_fault(phase: str) -> None:
        if phase == "after_marker_commit":
            raise OSError("injected partial burn after marker commit")

    partial_backend = global_ledger.LocalLedgerBackend(
        storage=ledger_backend.storage(),
        test_only_burn_fault=partial_burn_fault,
    )

    mute_calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        mute_calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="partial burn"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=partial_backend,
            execute_boundary=lambda _contract, _context: pytest.fail(
                "execution boundary was reached"
            ),
            mute_boundary=mute,
        )

    _reservation, guard_path, burn_path, _failure_slot = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    assert guard_path.read_bytes() == b""
    assert burn_path.exists()
    failure = json.loads(
        (plan_path.parent / runner.FAILURE_TOMBSTONE_FILENAME).read_text(encoding="utf-8")
    )
    assert mute_calls == [runner.FAILURE_CLEANUP_MUTE_PURPOSE]
    assert failure["degraded_execution_authorization"]["burn_guard_raw_state"]["size_bytes"] == 0
    assert (
        failure["degraded_execution_authorization"]["burn_marker_raw_state"]["state"]
        == "regular_file_present"
    )
    assert failure["final_failure_cleanup_passed"] is True


@pytest.mark.parametrize(
    "persistence_failure",
    ("failure-tombstone-write", "failure-tombstone-hash", "failed-manifest-write"),
)
def test_independent_global_failure_slot_survives_each_ordinary_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence_failure: str,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id=f"emergency-{persistence_failure}"
    )
    original_immutable_write = runner._write_immutable_json
    original_hash = runner.sha256_path
    original_manifest_write = runner.write_json_atomic

    def immutable_write(path: Path, document: Mapping[str, Any]) -> None:
        if (
            persistence_failure == "failure-tombstone-write"
            and path.name == runner.FAILURE_TOMBSTONE_FILENAME
        ):
            raise OSError("injected failure tombstone write")
        original_immutable_write(path, document)

    def hash_path(path: Path) -> str:
        if (
            persistence_failure == "failure-tombstone-hash"
            and path.name == runner.FAILURE_TOMBSTONE_FILENAME
        ):
            raise OSError("injected failure tombstone hash")
        return original_hash(path)

    def manifest_write(path: Path, document: Mapping[str, Any]) -> None:
        if persistence_failure == "failed-manifest-write" and document.get("status") == "failed":
            raise OSError("injected failed manifest write")
        original_manifest_write(path, document)

    monkeypatch.setattr(runner, "_write_immutable_json", immutable_write)
    monkeypatch.setattr(runner, "sha256_path", hash_path)
    monkeypatch.setattr(runner, "write_json_atomic", manifest_write)
    mute_calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        mute_calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=lambda _contract, _context: (_ for _ in ()).throw(OSError("ENODATA")),
            mute_boundary=mute,
        )

    _reservation, _guard, _burn, failure_slot = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    emergency = json.loads(failure_slot.read_text(encoding="utf-8"))
    state = failure_slot.stat(follow_symlinks=False)
    assert mute_calls == [runner.FAILURE_CLEANUP_MUTE_PURPOSE]
    assert emergency["receipt_kind"] == runner.EMERGENCY_FAILURE_KIND
    assert emergency["original_error"] == {"type": "OSError", "message": "ENODATA"}
    assert persistence_failure.split("-")[0] in emergency["ordinary_persistence_error"]["message"]
    assert emergency["failure_cleanup"]["exact_mute_passed"] is True
    assert stat.S_IMODE(state.st_mode) == global_ledger.SEALED_FILE_MODE
    assert state.st_nlink == 2


def test_global_failure_slot_survives_complete_local_condition_root_loss(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="local-root-loss"
    )
    condition_root = plan_path.parent
    mute_calls: list[str] = []

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        shutil.rmtree(condition_root)
        raise OSError("local condition root vanished after burn")

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        mute_calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="condition root vanished"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
            mute_boundary=mute,
        )

    _reservation, _guard, _burn, failure_slot = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    emergency = json.loads(failure_slot.read_text(encoding="utf-8"))
    assert not condition_root.exists()
    assert mute_calls == [runner.FAILURE_CLEANUP_MUTE_PURPOSE]
    assert emergency["original_error"]["message"] == ("local condition root vanished after burn")
    assert emergency["ordinary_persistence_error"]["type"] == "FileNotFoundError"


def test_compounded_original_ordinary_and_emergency_failures_are_all_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="compounded-failure"
    )
    original_manifest_write = runner.write_json_atomic

    def manifest_write(path: Path, document: Mapping[str, Any]) -> None:
        if document.get("status") == "failed":
            raise OSError("ordinary persistence unavailable")
        original_manifest_write(path, document)

    class EmergencyFailBackend:
        def storage(self) -> Mapping[str, Any]:
            return ledger_backend.storage()

        def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            if request.get("operation") == "seal_slot" and request.get("slot") == "failure":
                raise OSError("emergency persistence unavailable")
            return ledger_backend.mutate(request)

    monkeypatch.setattr(runner, "write_json_atomic", manifest_write)
    with pytest.raises(runner.PortPairRunError) as raised:
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=EmergencyFailBackend(),
            execute_boundary=lambda _contract, _context: (_ for _ in ()).throw(
                OSError("original ENODATA")
            ),
            mute_boundary=_passed_mute,
        )
    message = str(raised.value)
    assert "original ENODATA" in message
    assert "ordinary persistence unavailable" in message
    assert "emergency persistence unavailable" in message


def test_global_failure_slot_inode_substitution_is_rejected_before_hardware(
    tmp_path: Path,
) -> None:
    contract, plan_path, manifest_path, ledger_backend = _prepared(
        tmp_path, run_id="failure-slot-substitution"
    )
    _reservation, _guard, _burn, failure_slot = runner._ledger_paths(
        contract, plan_path=plan_path, backend=ledger_backend
    )
    failure_slot.unlink()
    replacement = tmp_path / "replacement-failure-slot"
    replacement.write_bytes(b"")
    replacement.chmod(global_ledger.PREPARED_SLOT_MODE)
    failure_slot.hardlink_to(replacement)
    invoked = False

    def execute(_contract: Mapping[str, Any], _context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"accepted_stream_count": 2}

    with pytest.raises(runner.PortPairRunError, match="reservation identity"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert not invoked


def test_same_global_run_namespace_rejects_replay_from_second_state_root(
    tmp_path: Path,
) -> None:
    storage = global_ledger.provision_local_test_storage(tmp_path / "ledger-authority")
    ledger_backend = global_ledger.LocalLedgerBackend(storage=storage)
    first = _contract(tmp_path / "first-state", run_id="cross-state-replay")
    first_root = Path(first["storage"]["condition_root"])
    runner._prepare_plan(
        first_root / runner.PLAN_FILENAME,
        first_root / runner.MANIFEST_FILENAME,
        first,
        ledger_backend=ledger_backend,
    )
    second = _contract(tmp_path / "second-state", run_id="cross-state-replay")
    second_root = Path(second["storage"]["condition_root"])

    with pytest.raises(runner.PortPairRunError, match="external reservation"):
        runner._prepare_plan(
            second_root / runner.PLAN_FILENAME,
            second_root / runner.MANIFEST_FILENAME,
            second,
            ledger_backend=ledger_backend,
        )
