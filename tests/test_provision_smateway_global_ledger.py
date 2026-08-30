from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from smateway import global_ledger  # type: ignore[import-untyped]

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/provision_smateway_global_ledger.py"
SPEC = importlib.util.spec_from_file_location("provision_smateway_global_ledger_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
provisioner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provisioner
SPEC.loader.exec_module(provisioner)

VERIFY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_smateway_global_ledger.py"
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_smateway_global_ledger_under_test", VERIFY_SCRIPT
)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
verifier = importlib.util.module_from_spec(VERIFY_SPEC)
sys.modules[VERIFY_SPEC.name] = verifier
VERIFY_SPEC.loader.exec_module(verifier)


def test_provision_requires_root_before_any_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 1000)
    invoked = False

    def install(*_args: Any, **_kwargs: Any) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(provisioner, "_install_create_once", install)
    with pytest.raises(global_ledger.GlobalLedgerError, match="must run as root"):
        provisioner.provision(global_ledger.RUNNER_USER)
    assert invoked is False


def test_provision_uses_only_source_fixed_paths_and_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories: list[tuple[Path, int]] = []
    files: list[tuple[Path, bytes, int]] = []
    sudoers: list[bytes] = []
    account = SimpleNamespace(pw_uid=global_ledger.RUNNER_UID, pw_gid=global_ledger.RUNNER_GID)
    seal = {
        "schema": 1,
        "seal_kind": "smateway_global_run_ledger_root_seal_v1",
    }
    observed = {
        "runner": {
            "user": global_ledger.RUNNER_USER,
            "uid": global_ledger.RUNNER_UID,
            "gid": global_ledger.RUNNER_GID,
        },
        "global_root": {"path": str(global_ledger.GLOBAL_ROOT)},
        "global_root_seal": {"path": str(global_ledger.GLOBAL_SEAL)},
        "privileged_helper": {"path": str(global_ledger.HELPER_PATH)},
        "sudoers_policy": {"path": str(global_ledger.SUDOERS_PATH)},
    }

    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(provisioner.pwd, "getpwnam", lambda _user: account)
    monkeypatch.setattr(
        provisioner,
        "_ensure_root_directory",
        lambda path, mode: directories.append((path, mode)),
    )
    monkeypatch.setattr(
        provisioner,
        "_install_create_once",
        lambda path, wire, mode: files.append((path, wire, mode)),
    )
    monkeypatch.setattr(provisioner, "_validate_sudoers", sudoers.append)
    monkeypatch.setattr(
        global_ledger,
        "build_root_seal_document",
        lambda **_kwargs: seal,
    )
    monkeypatch.setattr(
        global_ledger,
        "attest_fixed_storage",
        lambda **_kwargs: observed,
    )

    result = provisioner.provision(global_ledger.RUNNER_USER)

    assert directories == [
        (global_ledger.GLOBAL_ROOT.parent, global_ledger.DIRECTORY_MODE),
        (global_ledger.GLOBAL_ROOT, global_ledger.DIRECTORY_MODE),
        (
            global_ledger.GLOBAL_ROOT / global_ledger.ENTRIES_DIRECTORY,
            global_ledger.DIRECTORY_MODE,
        ),
        (
            global_ledger.GLOBAL_ROOT / global_ledger.ANCHORS_DIRECTORY,
            global_ledger.DIRECTORY_MODE,
        ),
        (global_ledger.GLOBAL_SEAL.parent, global_ledger.DIRECTORY_MODE),
        (global_ledger.HELPER_PATH.parent, global_ledger.DIRECTORY_MODE),
    ]
    assert [item[0] for item in files] == [
        global_ledger.HELPER_PATH,
        global_ledger.SUDOERS_PATH,
        global_ledger.GLOBAL_SEAL,
    ]
    assert [item[2] for item in files] == [
        global_ledger.HELPER_MODE,
        global_ledger.SUDOERS_MODE,
        global_ledger.SEALED_FILE_MODE,
    ]
    assert files[0][1] == Path(global_ledger.__file__).resolve().read_bytes()
    assert sudoers == [global_ledger.sudoers_text(global_ledger.RUNNER_USER).encode()]
    assert (
        files[2][1] == (json.dumps(seal, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    )
    assert result["status"] == "provisioned_and_verified"


def test_create_once_rejects_existing_non_authoritative_file(tmp_path: Path) -> None:
    path = tmp_path / "fixed-helper"
    path.write_bytes(b"old bytes")
    with pytest.raises(global_ledger.GlobalLedgerError, match="refusing replacement"):
        provisioner._install_create_once(path, b"new bytes", global_ledger.HELPER_MODE)


def test_sudoers_rule_is_narrow_noninteractive_helper_allowlist() -> None:
    assert global_ledger.sudoers_text(global_ledger.RUNNER_USER) == (
        "Defaults!/usr/local/libexec/smateway-ledger-helper env_reset\n"
        "smateway-rf ALL=(root) NOPASSWD: "
        "/usr/local/libexec/smateway-ledger-helper attest, "
        "/usr/local/libexec/smateway-ledger-helper reserve_run, "
        "/usr/local/libexec/smateway-ledger-helper seal_slot, "
        "/usr/local/libexec/smateway-ledger-helper burn_run\n"
    )


def test_read_only_verifier_reports_exact_attested_authority(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = {
        "runner": {
            "user": global_ledger.RUNNER_USER,
            "uid": global_ledger.RUNNER_UID,
            "gid": global_ledger.RUNNER_GID,
        },
        "global_root": {"path": str(global_ledger.GLOBAL_ROOT)},
        "global_root_seal": {"path": str(global_ledger.GLOBAL_SEAL)},
        "privileged_helper": {"path": str(global_ledger.HELPER_PATH)},
        "sudo_binary": {"path": str(global_ledger.SUDO_PATH)},
        "sudoers_policy": {"path": str(global_ledger.SUDOERS_PATH)},
    }
    calls: list[bool] = []

    def attest(*, require_runner_identity: bool) -> dict[str, Any]:
        calls.append(require_runner_identity)
        return storage

    monkeypatch.setattr(global_ledger, "attest_fixed_storage", attest)
    assert verifier.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [True]
    assert output["status"] == "verified"
    assert output["runner"] == storage["runner"]
    assert output["policies"] == sorted(global_ledger.POLICIES)


def test_read_only_verifier_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject(*, require_runner_identity: bool) -> dict[str, Any]:
        assert require_runner_identity is True
        raise global_ledger.GlobalLedgerError("authority absent")

    monkeypatch.setattr(global_ledger, "attest_fixed_storage", reject)
    assert verifier.main() == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "failed"
    assert error["error"]["message"] == "authority absent"
