#!/usr/bin/env python3
"""Root-only create-once provisioner for the shared Smateway run ledger."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY / "src"
sys.path[:] = [entry for entry in sys.path if entry != str(_SOURCE_ROOT)]
sys.path.insert(0, str(_SOURCE_ROOT))

from smateway import global_ledger


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_root_directory(path: Path, mode: int) -> None:
    global_ledger._assert_no_symlink_chain(path.parent, "provisioning parent")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if path.is_symlink() or not path.is_dir():
        raise global_ledger.GlobalLedgerError(f"provisioning target is not a directory: {path}")
    os.chown(path, 0, 0)
    path.chmod(mode)
    _fsync_parent(path)


def _install_create_once(path: Path, wire: bytes, mode: int) -> None:
    global_ledger._assert_no_symlink_chain(path.parent, "installation parent")
    if path.exists() or path.is_symlink():
        if (
            path.is_symlink()
            or not path.is_file()
            or path.read_bytes() != wire
            or path.stat().st_uid != 0
            or path.stat().st_gid != 0
            or (path.stat().st_mode & 0o7777) != mode
        ):
            raise global_ledger.GlobalLedgerError(
                f"existing fixed authority file differs; refusing replacement: {path}"
            )
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(wire):
            written = os.write(descriptor, wire[offset:])
            if written <= 0:
                raise global_ledger.GlobalLedgerError(f"incomplete provisioning write: {path}")
            offset += written
        os.fsync(descriptor)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent(path)


def _validate_sudoers(wire: bytes) -> None:
    visudo = shutil.which("visudo", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if visudo is None:
        raise global_ledger.GlobalLedgerError("visudo is required before provisioning")
    descriptor, temporary = tempfile.mkstemp(prefix="smateway-ledger-sudoers-", dir="/run")
    try:
        offset = 0
        while offset < len(wire):
            written = os.write(descriptor, wire[offset:])
            if written <= 0:
                raise global_ledger.GlobalLedgerError(
                    "generated sudoers policy validation write was incomplete"
                )
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        completed = subprocess.run(
            (visudo, "-cf", temporary),
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        if completed.returncode != 0:
            raise global_ledger.GlobalLedgerError(
                "generated sudoers policy failed visudo: " + completed.stderr.strip()
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def provision(runner_user: str) -> dict[str, object]:
    if os.geteuid() != 0:
        raise global_ledger.GlobalLedgerError("provisioning must run as root")
    try:
        account = pwd.getpwnam(runner_user)
    except KeyError as error:
        raise global_ledger.GlobalLedgerError("runner user does not exist") from error
    if account.pw_uid == 0:
        raise global_ledger.GlobalLedgerError("ledger runner must be unprivileged")
    helper_source = Path(global_ledger.__file__).resolve()
    expected_helper_source = _SOURCE_ROOT / "smateway/global_ledger.py"
    if (
        helper_source != expected_helper_source
        or expected_helper_source.is_symlink()
        or not expected_helper_source.is_file()
    ):
        raise global_ledger.GlobalLedgerError(
            "provisioner did not import the reviewed repository ledger helper source"
        )
    helper_wire = helper_source.read_bytes()
    sudoers_wire = global_ledger.sudoers_text(runner_user).encode()
    _validate_sudoers(sudoers_wire)
    for path in (
        global_ledger.GLOBAL_ROOT.parent,
        global_ledger.GLOBAL_ROOT,
        global_ledger.GLOBAL_ROOT / global_ledger.ENTRIES_DIRECTORY,
        global_ledger.GLOBAL_ROOT / global_ledger.ANCHORS_DIRECTORY,
    ):
        _ensure_root_directory(path, global_ledger.DIRECTORY_MODE)
    for path in (global_ledger.GLOBAL_SEAL.parent, global_ledger.HELPER_PATH.parent):
        _ensure_root_directory(path, global_ledger.DIRECTORY_MODE)
    _install_create_once(global_ledger.HELPER_PATH, helper_wire, global_ledger.HELPER_MODE)
    _install_create_once(global_ledger.SUDOERS_PATH, sudoers_wire, global_ledger.SUDOERS_MODE)
    seal = global_ledger.build_root_seal_document(
        runner_user=runner_user,
        runner_uid=account.pw_uid,
        runner_gid=account.pw_gid,
    )
    seal_wire = (json.dumps(seal, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    _install_create_once(global_ledger.GLOBAL_SEAL, seal_wire, global_ledger.SEALED_FILE_MODE)
    observed = global_ledger.attest_fixed_storage(require_runner_identity=False)
    return {
        "schema": 1,
        "status": "provisioned_and_verified",
        "runner": observed["runner"],
        "global_root": observed["global_root"],
        "seal": observed["global_root_seal"],
        "helper": observed["privileged_helper"],
        "sudoers": observed["sudoers_policy"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-user", required=True)
    args = parser.parse_args()
    try:
        result = provision(args.runner_user)
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": {"type": type(error).__name__, "message": str(error)}}
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
