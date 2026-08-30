from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from smateway import global_ledger


def _authority(tmp_path: Path) -> tuple[global_ledger.LocalLedgerBackend, dict[str, Any]]:
    state_root = tmp_path / "state"
    state_root.mkdir()
    storage = global_ledger.provision_local_test_storage(tmp_path.parent / f"{tmp_path.name}-root")
    backend = global_ledger.LocalLedgerBackend(storage=storage)
    namespace = {
        "schema": 1,
        "policy_id": "p2-5g8-input-off-v1",
        "namespace_kind": "5g8_input_off_board_run_id_v1",
        "board_id": "board-a",
        "run_id": "run-a",
    }
    identity = {
        "schema": 1,
        "board_id": "board-a",
        "run_id": "run-a",
        "run_root": str(state_root / "runs" / "run-a"),
        "capture_root": str(state_root / "captures" / "run-a"),
        "plan_path": str(state_root / "runs" / "run-a" / "plan.json"),
    }
    authority = global_ledger.authority_from_storage(
        policy_id="p2-5g8-input-off-v1",
        namespace=namespace,
        canonical_identity=identity,
        state_root=state_root,
        backend=backend,
    )
    return backend, authority


def _document(
    authority: dict[str, Any], role: str, *, execution_nonce: str | None = None
) -> dict[str, Any]:
    kinds = {
        "reservation": "5g8_input_off_global_run_id_reservation_v1",
        "execution": "5g8_input_off_global_execution_consumed_v1",
        "failure": "5g8_input_off_global_failure_receipt_v1",
    }
    document: dict[str, Any] = {
        "schema": 1,
        "marker_kind": kinds[role],
        "board_id": authority["global_run_namespace"]["board_id"],
        "run_id": authority["global_run_namespace"]["run_id"],
        "shared_global_ledger_authority": global_ledger.authority_receipt_binding(authority),
    }
    if role == "execution":
        assert execution_nonce is not None
        document["execution_nonce"] = execution_nonce
    if role == "failure":
        document["automatic_retry_forbidden"] = True
        if execution_nonce is not None:
            document["execution_nonce"] = execution_nonce
    return document


def _reserve(
    backend: global_ledger.LocalLedgerBackend, authority: dict[str, Any]
) -> dict[str, Any]:
    request = global_ledger.mutation_request(
        authority=authority,
        operation="reserve_run",
        payload={"reservation_id": "1" * 32},
    )
    return dict(backend.mutate(request))


def _prepare(
    tmp_path: Path,
) -> tuple[global_ledger.LocalLedgerBackend, dict[str, Any], dict[str, Any]]:
    backend, authority = _authority(tmp_path)
    reserve = _reserve(backend, authority)
    slots = reserve["evidence"]["slots"]
    backend.mutate(
        global_ledger.mutation_request(
            authority=authority,
            operation="seal_slot",
            payload={
                "slot": "reservation",
                "expected_identity": slots["reservation"],
                "document": _document(authority, "reservation"),
            },
        )
    )
    return backend, authority, dict(slots)


def _burn_request(authority: dict[str, Any], slots: dict[str, Any], nonce: str) -> dict[str, Any]:
    return global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": nonce,
            "expected_guard_identity": slots["burn-guard"],
            "document": _document(authority, "execution", execution_nonce=nonce),
        },
    )


def _competing_burn_worker(
    storage: dict[str, Any],
    request: dict[str, Any],
    start: Any,
    output: Any,
) -> None:
    backend = global_ledger.LocalLedgerBackend(storage=storage)
    start.wait()
    try:
        response = backend.mutate(request)
        output.put(("complete", response["evidence"]["execution_nonce"]))
    except BaseException as error:
        output.put(("failed", type(error).__name__))


def test_local_backend_runs_exact_monotonic_reserve_burn_sequence(tmp_path: Path) -> None:
    backend, authority = _authority(tmp_path)
    reserve = _reserve(backend, authority)
    slots = reserve["evidence"]["slots"]
    anchors = reserve["evidence"]["anchors"]
    assert set(slots) == {"reservation", "burn-guard", "failure-receipt"}
    assert all(
        (slots[name]["st_dev"], slots[name]["st_ino"])
        == (anchors[name]["st_dev"], anchors[name]["st_ino"])
        for name in slots
    )

    seal_request = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "expected_identity": slots["reservation"],
            "document": _document(authority, "reservation"),
        },
    )
    reservation = backend.mutate(seal_request)["evidence"]
    assert reservation["slot"] == "reservation"
    assert reservation["file"]["mode"] == global_ledger.SEALED_FILE_MODE
    assert backend.inspect(authority)["classification"] == "prepared"

    execution_nonce = "2" * 32
    burn_request = global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": slots["burn-guard"],
            "document": _document(
                authority,
                "execution",
                execution_nonce=execution_nonce,
            ),
        },
    )
    burn = backend.mutate(burn_request)["evidence"]
    guard = burn["guard"]
    assert Path(guard["path"]).read_bytes() == b"\x01"
    assert guard["mode"] == global_ledger.SEALED_FILE_MODE
    marker = burn["marker"]
    assert (
        json.loads(Path(marker["path"]).read_text(encoding="utf-8"))["execution_nonce"]
        == execution_nonce
    )
    assert marker["mode"] == global_ledger.SEALED_FILE_MODE
    assert backend.inspect(authority)["classification"] == "burn_complete"
    assert backend.mutate(burn_request)["evidence"] == burn

    with pytest.raises(global_ledger.GlobalLedgerError, match="already has a ledger"):
        _reserve(backend, authority)


def test_emergency_failure_receipt_seals_after_marker_guard_crash_gap(tmp_path: Path) -> None:
    clean_backend, authority = _authority(tmp_path)
    storage = clean_backend.storage()
    slots = _reserve(clean_backend, authority)["evidence"]["slots"]
    clean_backend.mutate(
        global_ledger.mutation_request(
            authority=authority,
            operation="seal_slot",
            payload={
                "slot": "reservation",
                "expected_identity": slots["reservation"],
                "document": _document(authority, "reservation"),
            },
        )
    )
    execution_nonce = "3" * 32
    burn_request = global_ledger.mutation_request(
        authority=authority,
        operation="burn_run",
        payload={
            "execution_nonce": execution_nonce,
            "expected_guard_identity": slots["burn-guard"],
            "document": _document(
                authority,
                "execution",
                execution_nonce=execution_nonce,
            ),
        },
    )

    class InjectedCrash(BaseException):
        pass

    def fault(stage: str) -> None:
        if stage == "after_marker_commit":
            raise InjectedCrash

    fault_backend = global_ledger.LocalLedgerBackend(
        storage=storage,
        test_only_burn_fault=fault,
    )
    with pytest.raises(InjectedCrash):
        fault_backend.mutate(burn_request)
    assert clean_backend.inspect(authority)["classification"] == "burn_committed_guard_pending"
    failure_request = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "failure",
            "expected_identity": slots["failure-receipt"],
            "document": _document(
                authority,
                "failure",
                execution_nonce=execution_nonce,
            ),
        },
    )
    failure = clean_backend.mutate(failure_request)["evidence"]
    assert failure["file"]["mode"] == global_ledger.SEALED_FILE_MODE
    assert failure["file"]["size_bytes"] > 0
    assert clean_backend.inspect(authority)["classification"] == "failed_postburn"


def test_response_loss_after_full_commit_is_exactly_observable_and_idempotent(
    tmp_path: Path,
) -> None:
    clean_backend, authority, slots = _prepare(tmp_path)
    request = _burn_request(authority, slots, "4" * 32)

    class ResponseLoss(BaseException):
        pass

    def fault(stage: str) -> None:
        if stage == "after_burn_commit":
            raise ResponseLoss

    fault_backend = global_ledger.LocalLedgerBackend(
        storage=clean_backend.storage(),
        test_only_burn_fault=fault,
    )
    with pytest.raises(ResponseLoss):
        fault_backend.mutate(request)
    inspection = clean_backend.inspect(authority)
    assert inspection["classification"] == "burn_complete"
    assert inspection["burn_guard"]["content_hex"] == "01"
    assert inspection["burn_guard"]["evidence"]["size_bytes"] == 1
    assert clean_backend.mutate(request)["evidence"]["state"] == "burn_complete"


def test_competing_processes_commit_exactly_one_execution_nonce(tmp_path: Path) -> None:
    backend, authority, slots = _prepare(tmp_path)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    output = context.Queue()
    requests = [
        _burn_request(authority, slots, "5" * 32),
        _burn_request(authority, slots, "6" * 32),
    ]
    processes = [
        context.Process(
            target=_competing_burn_worker,
            args=(dict(backend.storage()), request, start, output),
        )
        for request in requests
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    results = [output.get(timeout=2) for _ in processes]
    assert sorted(result[0] for result in results) == ["complete", "failed"]
    winning_nonce = next(result[1] for result in results if result[0] == "complete")
    inspection = backend.inspect(authority)
    assert inspection["classification"] == "burn_complete"
    assert inspection["execution_nonce"] == winning_nonce
    assert inspection["burn_guard"]["content_hex"] == "01"
    assert inspection["burn_guard"]["evidence"]["size_bytes"] == 1


def test_stale_locked_ledger_descriptor_is_rejected(tmp_path: Path) -> None:
    backend, authority, _slots = _prepare(tmp_path)
    storage = backend.storage()
    entries_fd, anchors_fd, ledger_fd = global_ledger._open_authority_directories(
        authority,
        storage,
    )
    assert ledger_fd is not None
    ledger_path = Path(str(authority["ledger_directory_path"]))
    held_path = ledger_path.with_name(f"{ledger_path.name}.held")
    try:
        ledger_path.rename(held_path)
        ledger_path.mkdir(mode=global_ledger.DIRECTORY_MODE)
        with pytest.raises(global_ledger.GlobalLedgerError, match="stale or replaced"):
            global_ledger._validate_locked_ledger_fd(
                ledger_fd,
                entries_fd=entries_fd,
                authority=authority,
            )
    finally:
        ledger_path.rmdir()
        held_path.rename(ledger_path)
        os.close(ledger_fd)
        os.close(entries_fd)
        os.close(anchors_fd)


def test_helper_rejects_authority_and_document_forgeries_before_mutation(
    tmp_path: Path,
) -> None:
    backend, authority = _authority(tmp_path)
    forged_authority = json.loads(json.dumps(authority))
    forged_authority["global_run_namespace"]["run_id"] = "forged"
    forged = global_ledger.mutation_request(
        authority=forged_authority,
        operation="reserve_run",
        payload={"reservation_id": "2" * 32},
    )
    with pytest.raises(global_ledger.GlobalLedgerError, match="differs"):
        backend.mutate(forged)

    slots = _reserve(backend, authority)["evidence"]["slots"]
    missing_binding = global_ledger.mutation_request(
        authority=authority,
        operation="seal_slot",
        payload={
            "slot": "reservation",
            "expected_identity": slots["reservation"],
            "document": {"schema": 1},
        },
    )
    with pytest.raises(global_ledger.GlobalLedgerError, match="campaign-specific schema"):
        backend.mutate(missing_binding)


def test_helper_rejects_symlink_rebound_global_root(tmp_path: Path) -> None:
    backend, authority = _authority(tmp_path)
    root = Path(str(backend.storage()["global_root"]["path"]))
    held = root.parent / "held-ledger-root"
    replacement = root.parent / "replacement-ledger-root"
    root.rename(held)
    replacement.mkdir()
    root.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(global_ledger.GlobalLedgerError, match="symlink"):
        _reserve(backend, authority)


def test_policy_namespace_and_identity_are_exact(tmp_path: Path) -> None:
    backend, authority = _authority(tmp_path)
    namespace = dict(authority["global_run_namespace"])
    namespace["extra"] = "forged"
    with pytest.raises(global_ledger.GlobalLedgerError, match="fixed campaign policy"):
        global_ledger.authority_from_storage(
            policy_id="p2-5g8-input-off-v1",
            namespace=namespace,
            canonical_identity=authority["canonical_run_identity"],
            state_root=Path(authority["state_root"]),
            backend=backend,
        )


def test_storage_document_validation_is_pure_exact_and_test_opt_in(
    tmp_path: Path,
) -> None:
    storage = global_ledger.provision_local_test_storage(tmp_path / "private-authority")
    with pytest.raises(global_ledger.GlobalLedgerError, match="private test.*forbidden"):
        global_ledger.validate_storage_document(storage)
    assert global_ledger.validate_storage_document(storage, allow_private_test=True) == storage

    forged = dict(storage)
    forged["unexpected"] = True
    with pytest.raises(global_ledger.GlobalLedgerError, match="fields differ"):
        global_ledger.validate_storage_document(forged, allow_private_test=True)


def test_policy_documents_survive_canonical_json_roundtrip() -> None:
    document = global_ledger.policy_document(global_ledger.POLICIES["p2-5g8-input-off-v1"])
    assert json.loads(json.dumps(document)) == document
    assert isinstance(document["namespace_fields"], list)
    assert isinstance(document["identity_fields"], list)


def test_mutation_request_rejects_missing_extra_or_header_overwrite_payloads(
    tmp_path: Path,
) -> None:
    _backend, authority = _authority(tmp_path)
    for payload in (
        {},
        {"reservation_id": "1" * 32, "extra": True},
        {"reservation_id": "1" * 32, "authority": {}},
    ):
        with pytest.raises(global_ledger.GlobalLedgerError, match="payload fields differ"):
            global_ledger.mutation_request(
                authority=authority,
                operation="reserve_run",
                payload=payload,
            )


def test_installed_helper_entrypoint_rejects_in_process_test_use() -> None:
    assert Path(global_ledger.__file__).absolute() != global_ledger.HELPER_PATH
    with pytest.raises(global_ledger.GlobalLedgerError, match="fixed installed path"):
        global_ledger._helper_storage("reserve_run")


def test_sudo_backend_invocation_is_fixed_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {"operation": "reserve_run"}
    captured: dict[str, Any] = {}

    def run(command: tuple[str, ...], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stderr": "denied",
                "stdout": "",
            },
        )()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(global_ledger, "attest_runner_runtime", lambda: {})
    with pytest.raises(global_ledger.GlobalLedgerError, match="rejected request"):
        global_ledger.SudoLedgerBackend().mutate(request)
    assert captured["command"] == (
        "/usr/bin/sudo",
        "-n",
        "--",
        "/usr/local/libexec/smateway-ledger-helper",
        "reserve_run",
    )
    assert captured["kwargs"]["env"] == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }


def test_sudo_backend_normalizes_timeout_to_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("ledger-helper", 30)

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(global_ledger, "attest_runner_runtime", lambda: {})
    with pytest.raises(global_ledger.GlobalLedgerError, match="cannot invoke"):
        global_ledger.SudoLedgerBackend().mutate({"operation": "reserve_run"})
