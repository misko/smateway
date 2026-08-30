from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_5g8_input_off_control.py"
SPEC = importlib.util.spec_from_file_location("run_5g8_input_off_control_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


@pytest.fixture
def ledger_backend(tmp_path: Path) -> Any:
    storage = runner.global_ledger.provision_local_test_storage(
        tmp_path.parent / f"{tmp_path.name}-shared-ledger-authority"
    )
    return runner.global_ledger.LocalLedgerBackend(storage=storage)


def _native_attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(runner._REQUIRED_LIBIIO_DIRECTORY / "libiio.so.0.25"),
        "library_path_from_proc_maps": True,
        "library_sha256": ("d0a18bddcb54d182262acb2a9e31a88c81618cb43789320b8381c149777bef89"),
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {"iio_device_get_kernel_buffers_count": True},
        "loader_search_path_first": "/usr/local/lib",
    }


def _minimal_contract(
    tmp_path: Path, ledger_backend: Any, run_id: str = "p2-run-a"
) -> dict[str, Any]:
    run_root = tmp_path / "runs" / run_id
    capture_root = tmp_path / "captures" / run_id
    contract: dict[str, Any] = {
        "schema": 1,
        "run_id": run_id,
        "board_id": "board-a",
        "configuration": {"serial": "serial-a", "uri": "usb:1.2.3"},
        "source": {"native_libiio": _native_attestation()},
        "storage": {
            "state_root": str(tmp_path),
            "capture_root": str(capture_root),
            "run_root": str(run_root),
            "local_storage_device": os.stat("/home/pi").st_dev,
            "local_rpi_only": True,
            "pluto_storage_forbidden": True,
        },
        "execution": {},
    }
    contract["execution"]["global_run_ledger_authority"] = (
        runner._shared_new_global_ledger_authority(
            board_id="board-a",
            run_id=run_id,
            run_root=run_root,
            capture_root=capture_root,
            state_root=tmp_path,
            ledger_backend=ledger_backend,
        )
    )
    return contract


def _prepared(
    tmp_path: Path, ledger_backend: Any, *, run_id: str = "p2-run-a"
) -> tuple[dict[str, Any], Path, Path]:
    contract = _minimal_contract(tmp_path, ledger_backend, run_id)
    run_root = Path(contract["storage"]["run_root"])
    plan_path = run_root / runner.PLAN_FILENAME
    manifest_path = run_root / runner.MANIFEST_FILENAME
    runner._prepare_plan(plan_path, manifest_path, contract, ledger_backend=ledger_backend)
    return contract, plan_path, manifest_path


def _passed_mute(serial: str, purpose: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "purpose": purpose,
        "serial": serial,
        "attestation": "mute_returned_radio_exact_serial_readback",
        "error": None,
    }


def _identity(serial: str, uri: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "serial": serial,
        "requested_uri": uri,
        "resolved_uri": uri,
        "exact_uri_match": True,
        "scan_mutates_radio_state": False,
    }


def _write_finalized_test_artifact(
    contract: dict[str, Any], artifact_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture_fd, binding = runner._create_bound_capture_root(
        Path(contract["storage"]["capture_root"]),
        expected_device=int(contract["storage"]["local_storage_device"]),
    )
    try:
        os.mkdir(artifact_id, mode=0o700, dir_fd=capture_fd)
        artifact_fd = runner._open_child_directory(capture_fd, artifact_id)
        try:
            for name, payload in (
                (f"{artifact_id}.sigmf-data", b"raw"),
                (f"{artifact_id}.sigmf-meta", b"{}"),
            ):
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=artifact_fd,
                )
                try:
                    os.write(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            observed = os.fstat(artifact_fd)
            identity = {
                "path": artifact_id,
                "st_dev": int(observed.st_dev),
                "st_ino": int(observed.st_ino),
            }
            runner._seal_directory_tree(artifact_fd)
        finally:
            os.close(artifact_fd)
        os.fsync(capture_fd)
    finally:
        os.close(capture_fd)
    return binding, identity


def test_runner_has_no_legacy_fully_conducted_confirmation() -> None:
    option_strings = {
        option for action in runner._parser()._actions for option in action.option_strings
    }
    assert "--confirm-fully-conducted" not in option_strings
    assert "--confirm-two-distinct-terminations" in option_strings
    assert runner.FRAME_COUNT == 100
    assert runner.TOTAL_SAMPLES == 10_000_000
    assert runner.RECEIVER_GAIN_DB == 40.0
    assert runner.DDS_SCALE == 0.25


def test_confirmation_gate_requires_exact_p2_token_and_every_fact() -> None:
    base = {
        "confirm_no_antennas": True,
        "confirm_two_distinct_terminations": True,
        "confirm_downstream_unchanged": True,
        "confirm_rx1_protected_reference": True,
        "confirm_tx2_terminated_muted": True,
        "confirm_fast20_live": True,
        "confirm_no_movement": True,
        "confirm_topology_token": runner.TOPOLOGY_TOKEN,
    }
    confirmation = runner._validate_execution_confirmations(SimpleNamespace(**base))
    assert confirmation["two_distinct_terminations"] is True
    base["confirm_downstream_unchanged"] = False
    with pytest.raises(runner.InputOffRunError, match="downstream_unchanged"):
        runner._validate_execution_confirmations(SimpleNamespace(**base))
    base["confirm_downstream_unchanged"] = True
    base["confirm_topology_token"] = "WRONG"
    with pytest.raises(runner.InputOffRunError, match="confirm-topology-token"):
        runner._validate_execution_confirmations(SimpleNamespace(**base))


def test_one_successful_execution_burns_run_and_accepts_exactly_one_stream(
    tmp_path: Path,
    ledger_backend: Any,
) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path, ledger_backend)

    calls = 0

    def execute(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"stream_id": 123, "artifact": {"artifact_id": "artifact-a"}}

    manifest = runner._execute_prepared(
        plan_path=plan_path,
        manifest_path=manifest_path,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=execute,
    )
    assert manifest["status"] == "complete"
    assert manifest["accepted_stream_count"] == 1
    assert calls == 1
    assert (
        manifest["global_execution_burn"][
            "burn_completed_before_source_dependency_fixture_or_hardware_access"
        ]
        is True
    )
    tombstone = manifest_path.parent / runner.EXECUTION_TOMBSTONE_FILENAME
    assert tombstone.is_file()
    assert tombstone.stat().st_mode & stat.S_IWUSR == 0
    with pytest.raises(runner.InputOffRunError, match="never-attempted|consumed|failed"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )


def test_failed_execution_writes_immutable_tombstone_and_never_accepts_raw(
    tmp_path: Path,
    ledger_backend: Any,
) -> None:
    contract, plan_path, manifest_path = _prepared(tmp_path, ledger_backend, run_id="p2-run-failed")

    def fail(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise OSError("ENODATA")

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_prepared(
            plan_path=plan_path,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=fail,
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failure_path = manifest_path.parent / runner.FAILURE_TOMBSTONE_FILENAME
    assert manifest["status"] == "failed"
    assert manifest["accepted_stream_count"] == 0
    assert failure_path.is_file()
    assert failure_path.stat().st_mode & stat.S_IWUSR == 0
    assert json.loads(failure_path.read_text(encoding="utf-8"))["accepted_artifact"] is False


def test_enodata_path_always_attempts_final_mute_and_persists_nothing(
    tmp_path: Path, ledger_backend: Any
) -> None:
    contract = _minimal_contract(tmp_path, ledger_backend, "p2-enodata")
    calls: list[str] = []

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("ENODATA")

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        return _passed_mute(serial, purpose)

    with pytest.raises(OSError, match="ENODATA"):
        runner._execute_one_stream(
            contract,
            execution_context={},
            capture_boundary=capture,
            mute_boundary=mute,
            identity_boundary=_identity,
            native_boundary=_native_attestation,
        )
    assert calls == ["pre_capture_exact_mute", "final_acceptance_exact_mute"]
    assert not Path(contract["storage"]["capture_root"]).exists()


@pytest.mark.parametrize("failure", ("identity", "native"))
def test_live_preflight_mutes_first_and_final_mutes_on_failure(
    tmp_path: Path, ledger_backend: Any, failure: str
) -> None:
    contract = _minimal_contract(tmp_path, ledger_backend, f"p2-{failure}-failure")
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        return _passed_mute(serial, purpose)

    def identity(serial: str, uri: str) -> dict[str, Any]:
        calls.append("identity")
        result = _identity(serial, uri)
        if failure == "identity":
            result["status"] = "failed"
        return result

    def native() -> dict[str, Any]:
        calls.append("native")
        if failure == "native":
            raise RuntimeError("native readback failed")
        return _native_attestation()

    with pytest.raises((runner.InputOffRunError, RuntimeError)):
        runner._execute_one_stream(
            contract,
            execution_context={},
            mute_boundary=mute,
            identity_boundary=identity,
            native_boundary=native,
        )

    expected = ["pre_capture_exact_mute", "identity"]
    if failure == "native":
        expected.append("native")
    expected.append("final_acceptance_exact_mute")
    assert calls == expected
    assert not Path(contract["storage"]["capture_root"]).exists()


def test_failed_initial_mute_still_attempts_final_mute_before_any_identity(
    tmp_path: Path, ledger_backend: Any
) -> None:
    contract = _minimal_contract(tmp_path, ledger_backend, "p2-initial-mute-failure")
    calls: list[str] = []

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        calls.append(purpose)
        result = _passed_mute(serial, purpose)
        if purpose == "pre_capture_exact_mute":
            result["status"] = "failed"
        return result

    def identity(_serial: str, _uri: str) -> dict[str, Any]:
        calls.append("identity")
        raise AssertionError("identity must not run after failed first mute")

    with pytest.raises(runner.InputOffRunError, match="initial exact-radio mute"):
        runner._execute_one_stream(
            contract,
            execution_context={},
            mute_boundary=mute,
            identity_boundary=identity,
            native_boundary=_native_attestation,
        )
    assert calls == ["pre_capture_exact_mute", "final_acceptance_exact_mute"]


def test_final_mute_failure_overrides_capture_failure_and_accepts_nothing(
    tmp_path: Path, ledger_backend: Any
) -> None:
    contract = _minimal_contract(tmp_path, ledger_backend, "p2-final-mute-failed")

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("ENODATA")

    def mute(serial: str, purpose: str) -> dict[str, Any]:
        evidence = _passed_mute(serial, purpose)
        if purpose == "final_acceptance_exact_mute":
            evidence["status"] = "failed"
            evidence["error"] = {"type": "ReadbackError", "message": "not muted"}
        return evidence

    with pytest.raises(runner.InputOffRunError, match="final exact-radio mute"):
        runner._execute_one_stream(
            contract,
            execution_context={},
            capture_boundary=capture,
            mute_boundary=mute,
            identity_boundary=_identity,
            native_boundary=_native_attestation,
        )
    assert not Path(contract["storage"]["capture_root"]).exists()


def test_plan_contract_freezes_p0_matched_acquisition_and_local_storage(
    tmp_path: Path, ledger_backend: Any
) -> None:
    profile_path = Path(__file__).resolve().parents[1] / "profiles/fast20-v1/control_profile.json"
    profile_file = runner._file_evidence(profile_path)
    fixture = {
        "fixture": {
            "fast20_control": {"profile": profile_file},
            "fixed_graph_sha256": "a" * 64,
            "comparable_fixture_group_id": "group-a",
        },
        "fixture_evidence_sha256": "b" * 64,
    }
    source = {
        "commit": "c" * 40,
        "source_files_sha256": "d" * 64,
    }
    contract = runner._build_plan_contract(
        run_id="p2-plan-a",
        board_id="board-a",
        serial="serial-a",
        uri="usb:1.2.3",
        profile_path=profile_path,
        fixture_evidence=fixture,
        p0_bindings=[{"run_id": f"p0-{index}"} for index in range(5)],
        source_attestation=source,
        dependency_attestation={"commit": "e" * 40},
        native_attestation=_native_attestation(),
        state_root=tmp_path,
        ledger_backend=ledger_backend,
    )
    assert contract["acquisition"] == runner.acquisition_contract()
    assert contract["execution"]["one_stream_per_run"] is True
    assert contract["execution"]["legacy_fully_conducted_label_forbidden"] is True
    assert contract["storage"]["local_rpi_only"] is True
    assert "pluto-usb-captures/input-off-runs/p2-plan-a" in contract["storage"]["capture_root"]


def test_local_state_compares_nearest_existing_filesystem_device(tmp_path: Path) -> None:
    planned = tmp_path / "not-created" / "state"
    assert runner._safe_local_state_root(planned) == planned.absolute()
    with pytest.raises(runner.InputOffRunError, match="local RPi storage device"):
        runner._safe_local_state_root(Path("/proc/smateway-p2"))


def test_fast20_live_image_is_recursively_validated_and_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "control_profile.json"
    firmware = tmp_path / "pluto_fast20.bin"
    readback = tmp_path / "pluto_fast20.readback.bin"
    profile.write_text("{}", encoding="utf-8")
    firmware.write_bytes(b"fast20-image")
    readback.write_bytes(firmware.read_bytes())
    document = {
        "schema": 1,
        "evidence_kind": runner.SEALED_SELECTOR_EVIDENCE_KIND,
        "campaign_id": runner.CAMPAIGN_ID,
        "run_id": "fast20-flash-r01",
        "board_id": "board-a",
        "image_role": "fast20",
        "frozen_inputs": {
            "files": {
                "profile": runner._file_evidence(profile),
                "firmware_bin": runner._file_evidence(firmware),
            },
            "control_profile": {"id": "fast20-v1", "revision": 1},
        },
        "target_flash_readback": runner._file_evidence(readback),
        "startup": {"evidence_kind": "fast20_exact_image_reset_run_identity_v1"},
    }
    evidence_path = tmp_path / "selector-flash-evidence.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")
    evidence = runner._file_evidence(evidence_path)
    calls: list[dict[str, Any]] = []

    def validate(path: Path, **kwargs: Any) -> dict[str, Any]:
        assert path == evidence_path.absolute()
        calls.append(kwargs)
        return document

    monkeypatch.setattr(runner, "validate_sealed_selector_evidence", validate)
    result = runner._validate_fast20_live_image(
        evidence,
        campaign_id=runner.CAMPAIGN_ID,
        board_id="board-a",
    )

    assert result["binding_kind"] == "sealed_fast20_live_image_v1"
    assert result["profile"] == runner._file_evidence(profile)
    assert result["firmware_bin"] == runner._file_evidence(firmware)
    assert calls == [
        {
            "expected_sha256": runner.sha256_path(evidence_path),
            "expected_campaign_id": runner.CAMPAIGN_ID,
            "expected_run_id": "fast20-flash-r01",
            "expected_board_id": "board-a",
            "expected_image_role": "fast20",
        }
    ]


def test_fast20_live_image_rejects_arbitrary_json(tmp_path: Path) -> None:
    evidence_path = tmp_path / "not-sealed.json"
    evidence_path.write_text("{}", encoding="utf-8")

    with pytest.raises(runner.InputOffRunError, match="recursively sealed"):
        runner._validate_fast20_live_image(
            runner._file_evidence(evidence_path),
            campaign_id=runner.CAMPAIGN_ID,
            board_id="board-a",
        )


def test_execute_rechecks_local_storage_before_burning_run(
    tmp_path: Path, ledger_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan, manifest = _prepared(tmp_path, ledger_backend)
    invoked = False

    def reject(_path: Path, *, label: str) -> Path:
        raise runner.FileArtifactAdmissionError(f"{label} is not on local RPi storage device")

    def execute(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"stream_id": 123}

    monkeypatch.setattr(runner, "assert_local_rpi_storage", reject)
    with pytest.raises(runner.InputOffRunError, match="local RPi storage device"):
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


def test_global_ledger_rejects_replayed_prepared_snapshot_after_success(
    tmp_path: Path,
    ledger_backend: Any,
) -> None:
    contract, plan, manifest = _prepared(tmp_path, ledger_backend, run_id="p2-prepared-replay")
    snapshot = tmp_path / "prepared-snapshot"
    shutil.copytree(plan.parent, snapshot)
    calls = 0

    def execute(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"stream_id": 123, "artifact": {"artifact_id": "artifact-a"}}

    runner._execute_prepared(
        plan_path=plan,
        manifest_path=manifest,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=execute,
    )
    completed = tmp_path / "completed-run"
    plan.parent.rename(completed)
    shutil.copytree(snapshot, plan.parent)

    with pytest.raises(runner.InputOffRunError, match="consumed or failed"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert calls == 1


def test_global_ledger_rejects_deleted_local_tombstone_and_restored_manifest(
    tmp_path: Path,
    ledger_backend: Any,
) -> None:
    contract, plan, manifest = _prepared(tmp_path, ledger_backend, run_id="p2-local-rollback")
    prepared_manifest = manifest.read_bytes()
    calls = 0

    def execute(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"stream_id": 123, "artifact": {"artifact_id": "artifact-a"}}

    runner._execute_prepared(
        plan_path=plan,
        manifest_path=manifest,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=execute,
    )
    (plan.parent / runner.EXECUTION_TOMBSTONE_FILENAME).unlink()
    manifest.write_bytes(prepared_manifest)

    with pytest.raises(runner.InputOffRunError, match="consumed or failed"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )
    assert calls == 1


def test_global_anchor_history_rejects_replan_after_run_directory_removal(
    tmp_path: Path,
    ledger_backend: Any,
) -> None:
    contract, plan, manifest = _prepared(tmp_path, ledger_backend, run_id="p2-replan-after-removal")

    runner._execute_prepared(
        plan_path=plan,
        manifest_path=manifest,
        expected_contract=contract,
        confirmations={"confirmed": True},
        ledger_backend=ledger_backend,
        execute_boundary=lambda _contract, _burn, _execution_context: {
            "stream_id": 123,
            "artifact": {"artifact_id": "artifact-a"},
        },
    )
    plan.parent.rename(tmp_path / "removed-completed-run")

    with pytest.raises(runner.InputOffRunError, match="durable global ledger history"):
        runner._prepare_plan(plan, manifest, contract, ledger_backend=ledger_backend)


def test_shared_global_ledger_backend_rejects_path_escape_before_mutation(
    tmp_path: Path, ledger_backend: Any
) -> None:
    contract = _minimal_contract(tmp_path, ledger_backend, "p2-helper-path-escape")
    authority = dict(contract["execution"]["global_run_ledger_authority"])
    authority["ledger_directory_path"] = str(tmp_path / "escaped-ledger")
    request = runner.global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": "0" * 32,
            "expected_guard_identity": {},
            "document": {},
        },
    )
    with pytest.raises(runner.global_ledger.GlobalLedgerError, match="differs from fixed policy"):
        ledger_backend.mutate(request)
    assert not (tmp_path / "escaped-ledger").exists()


def test_default_execution_revalidates_dependencies_only_after_global_burn(
    tmp_path: Path, ledger_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan, manifest = _prepared(
        tmp_path, ledger_backend, run_id="p2-post-burn-revalidation"
    )
    observed: dict[str, Any] = {}

    def revalidate(_contract: dict[str, Any]) -> None:
        current = json.loads(manifest.read_text(encoding="utf-8"))
        burn = current["global_execution_burn"]
        guard = Path(burn["burn_guard"]["path"])
        observed["manifest_status"] = current["status"]
        observed["guard"] = guard.read_bytes()
        observed["guard_mode"] = stat.S_IMODE(guard.stat().st_mode)
        raise OSError("frozen source changed after planning")

    def hardware(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("hardware must not run after evidence revalidation fails")

    monkeypatch.setattr(runner, "_revalidate_frozen_execution_evidence", revalidate)
    monkeypatch.setattr(runner, "_execute_one_stream", hardware)
    with pytest.raises(OSError, match="frozen source changed"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=runner._default_execute_boundary,
        )
    assert observed == {
        "manifest_status": "running",
        "guard": b"\x01",
        "guard_mode": runner.GLOBAL_LEDGER_SEALED_FILE_MODE,
    }


def test_bound_capture_fd_does_not_follow_rebound_canonical_path(tmp_path: Path) -> None:
    capture_root = tmp_path / "captures" / "p2-rebound"
    capture_fd, binding = runner._create_bound_capture_root(
        capture_root, expected_device=os.stat("/home/pi").st_dev
    )
    held_root = tmp_path / "held-original-capture-root"
    replacement = tmp_path / "replacement-capture-root"
    replacement.mkdir()
    capture_root.rename(held_root)
    capture_root.symlink_to(replacement, target_is_directory=True)
    try:
        marker_fd = os.open(
            "held-fd-marker",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=capture_fd,
        )
        try:
            os.write(marker_fd, b"held inode")
        finally:
            os.close(marker_fd)
        assert (held_root / "held-fd-marker").read_bytes() == b"held inode"
        assert not (replacement / "held-fd-marker").exists()
        with pytest.raises(runner.InputOffRunError, match="without following links"):
            runner._validate_capture_root_binding(binding)
    finally:
        os.close(capture_fd)


def test_final_manifest_failure_quarantines_and_seals_uncommitted_artifact(
    tmp_path: Path, ledger_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, plan, manifest = _prepared(
        tmp_path, ledger_backend, run_id="p2-final-manifest-failure"
    )
    artifact_id = "uncommitted-artifact"

    def execute(
        execution_contract: dict[str, Any],
        burn: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        binding, artifact_identity = _write_finalized_test_artifact(execution_contract, artifact_id)
        runner._publish_finalized_artifact_binding(
            execution_context,
            capture_root_binding=binding,
            artifact_id=artifact_id,
            artifact_directory_identity=artifact_identity,
        )
        return {
            "stream_id": 123,
            "artifact": {"artifact_id": artifact_id},
            "artifact_id": artifact_id,
            "capture_root_binding": binding,
            "artifact_directory_identity": artifact_identity,
            "global_execution_burn": burn,
        }

    original_write = runner.write_json_atomic

    def fail_complete_manifest(path: Path, document: dict[str, Any]) -> None:
        if path == manifest and document.get("status") == "complete":
            raise OSError("final manifest durability failure")
        original_write(path, document)

    monkeypatch.setattr(runner, "write_json_atomic", fail_complete_manifest)
    with pytest.raises(OSError, match="final manifest durability failure"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )

    failed_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    quarantine = failed_manifest["quarantine"]
    quarantined_root = Path(quarantine["path"])
    assert failed_manifest["status"] == "failed"
    assert failed_manifest["accepted_stream_count"] == 0
    assert quarantine["readonly_tree_verified"] is True
    assert quarantine["files_sha256"] == runner.canonical_sha256(quarantine["files"])
    assert quarantined_root.is_dir()
    assert stat.S_IMODE(quarantined_root.stat().st_mode) == 0o500
    assert stat.S_IMODE(quarantined_root.parent.stat().st_mode) == 0o500
    assert stat.S_IMODE(quarantined_root.parent.parent.stat().st_mode) == 0o500
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in quarantined_root.iterdir())
    failure_receipt = Path(
        failed_manifest["global_run_reservation"]["binding"]["failure_receipt_slot"]["path"]
    )
    assert failure_receipt.stat().st_size > 0
    assert stat.S_IMODE(failure_receipt.stat().st_mode) == runner.GLOBAL_LEDGER_SEALED_FILE_MODE


@pytest.mark.parametrize("interruption_kind", ("sigint", "base_exception"))
def test_post_boundary_interrupt_quarantines_context_bound_finalized_artifact(
    tmp_path: Path,
    ledger_backend: Any,
    interruption_kind: str,
) -> None:
    run_id = f"p2-post-boundary-{interruption_kind}"
    contract, plan, manifest_path = _prepared(tmp_path, ledger_backend, run_id=run_id)
    artifact_id = f"artifact-{interruption_kind}"
    observed: dict[str, Any] = {}

    class SyntheticFatalAbort(BaseException):
        pass

    expected_error: type[BaseException]
    if interruption_kind == "sigint":
        expected_error = KeyboardInterrupt
        expected_message = "received SIGINT"
    else:
        expected_error = SyntheticFatalAbort
        expected_message = "fatal abort after boundary return"

    class InterruptBeforeResultAttachment(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            assert key == "global_execution_burn"
            observed["result_inspection_started"] = True
            if interruption_kind == "sigint":
                runner._stop_on_signal(signal.SIGINT, None)
            raise SyntheticFatalAbort(expected_message)

    def execute(
        execution_contract: dict[str, Any],
        burn: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        binding, artifact_identity = _write_finalized_test_artifact(execution_contract, artifact_id)
        finalized = runner._publish_finalized_artifact_binding(
            execution_context,
            capture_root_binding=binding,
            artifact_id=artifact_id,
            artifact_directory_identity=artifact_identity,
        )
        observed["execution_context"] = execution_context
        observed["finalized"] = finalized
        return InterruptBeforeResultAttachment(
            {
                "stream_id": 123,
                "artifact_id": artifact_id,
                "capture_root_binding": binding,
                "artifact_directory_identity": artifact_identity,
                "global_execution_burn": burn,
            }
        )

    with pytest.raises(expected_error, match=expected_message):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest_path,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=ledger_backend,
            execute_boundary=execute,
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    finalized = observed["finalized"]
    quarantine = manifest["quarantine"]
    assert observed["result_inspection_started"] is True
    assert observed["execution_context"][runner._FINALIZED_ARTIFACT_CONTEXT_KEY] == finalized
    assert manifest["status"] == "failed"
    assert manifest["accepted_stream_count"] == 0
    assert manifest["attempts"][0]["uncommitted_result"] is None
    assert quarantine["accepted"] is False
    assert quarantine["artifact_id"] == artifact_id
    assert quarantine["capture_root_binding"] == finalized["capture_root_binding"]
    assert (quarantine["st_dev"], quarantine["st_ino"]) == (
        finalized["artifact_directory_identity"]["st_dev"],
        finalized["artifact_directory_identity"]["st_ino"],
    )
    quarantined_root = Path(quarantine["path"])
    assert quarantined_root.is_dir()
    assert not (Path(contract["storage"]["capture_root"]) / artifact_id).exists()
    assert stat.S_IMODE(quarantined_root.stat().st_mode) == 0o500
    assert (
        json.loads((quarantined_root / "failure.json").read_text(encoding="utf-8"))["error"]["type"]
        == expected_error.__name__
    )


def test_atomic_burn_response_loss_seals_failure_receipt_and_blocks_retry(
    tmp_path: Path, ledger_backend: Any
) -> None:
    contract, plan, manifest = _prepared(tmp_path, ledger_backend, run_id="p2-response-loss")
    execute_calls = 0

    class ResponseLossBackend:
        def storage(self) -> dict[str, Any]:
            return dict(ledger_backend.storage())

        def inspect(self, authority: dict[str, Any]) -> dict[str, Any]:
            return dict(ledger_backend.inspect(authority))

        def mutate(self, request: dict[str, Any]) -> dict[str, Any]:
            if request.get("operation") == "burn_run":
                ledger_backend.mutate(request)
                raise runner.global_ledger.GlobalLedgerError(
                    "atomic burn response lost after commit"
                )
            return dict(ledger_backend.mutate(request))

    response_loss_backend = ResponseLossBackend()

    def execute(
        _contract: dict[str, Any],
        _burn: dict[str, Any],
        _execution_context: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal execute_calls
        execute_calls += 1
        return {"stream_id": 123}

    with pytest.raises(runner.InputOffRunError, match="atomic burn response lost after commit"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=response_loss_backend,
            execute_boundary=execute,
        )
    failed_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    binding = failed_manifest["global_run_reservation"]["binding"]
    guard = Path(binding["burn_guard"]["path"])
    marker = Path(binding["burn_marker_path"])
    failure_receipt = Path(binding["failure_receipt_slot"]["path"])
    assert guard.read_bytes() == b"\x01"
    assert stat.S_IMODE(guard.stat().st_mode) == runner.GLOBAL_LEDGER_SEALED_FILE_MODE
    assert marker.is_file()
    assert stat.S_IMODE(marker.stat().st_mode) == runner.GLOBAL_LEDGER_SEALED_FILE_MODE
    assert failure_receipt.stat().st_size > 0
    assert stat.S_IMODE(failure_receipt.stat().st_mode) == runner.GLOBAL_LEDGER_SEALED_FILE_MODE
    assert execute_calls == 0

    with pytest.raises(runner.InputOffRunError, match="never-attempted|consumed|failed"):
        runner._execute_prepared(
            plan_path=plan,
            manifest_path=manifest,
            expected_contract=contract,
            confirmations={"confirmed": True},
            ledger_backend=response_loss_backend,
            execute_boundary=execute,
        )
    assert execute_calls == 0
