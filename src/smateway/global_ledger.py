#!/usr/bin/python3 -I -B
"""Shared privileged, monotonic run-ledger authority.

The installed copy of this exact file is invoked only through a narrowly scoped
``sudo -n`` rule.  Production paths and campaign policies are source-fixed.
Unit tests use :class:`LocalLedgerBackend` by explicit dependency injection;
the production CLI has no path or backend override.
"""

from __future__ import annotations

import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

API = "smateway_global_run_ledger_v1"
RUNNER_USER = "smateway-rf"
RUNNER_UID = 990
RUNNER_GID = 990
RUNNER_HOME = Path("/var/lib/smateway-rf")
RUNNER_SHELL = "/usr/sbin/nologin"
GLOBAL_ROOT = Path("/var/lib/smateway/global-run-ledger-v1")
GLOBAL_SEAL = Path("/etc/smateway/global-run-ledger-root-v1.json")
HELPER_PATH = Path("/usr/local/libexec/smateway-ledger-helper")
SUDO_PATH = Path("/usr/bin/sudo")
SUDOERS_PATH = Path("/etc/sudoers.d/smateway-ledger-helper")
SUDOERS_ROOT = Path("/etc/sudoers")
CVTSUDOERS_PATH = Path("/usr/bin/cvtsudoers")
ENTRIES_DIRECTORY = "run-ledgers"
ANCHORS_DIRECTORY = "inode-anchors"
RESERVATION_FILENAME = "reservation.json"
BURN_GUARD_FILENAME = "burn.guard"
BURN_MARKER_FILENAME = "execution-consumed.json"
FAILURE_RECEIPT_FILENAME = "failure-receipt.slot"
DIRECTORY_MODE = 0o755
PREPARED_SLOT_MODE = 0o644
SEALED_FILE_MODE = 0o444
HELPER_MODE = 0o555
SUDOERS_MODE = 0o440
OPERATIONS = ("attest", "reserve_run", "seal_slot", "burn_run")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RESERVATION_ID = re.compile(r"[0-9a-f]{32}")
EXECUTION_NONCE = re.compile(r"[0-9a-f]{32}")
DANGEROUS_SOCKET_PATHS = (
    Path("/run/docker.sock"),
    Path("/var/run/docker.sock"),
    Path("/run/containerd/containerd.sock"),
    Path("/run/podman/podman.sock"),
    Path("/var/run/libvirt/libvirt-sock"),
    Path("/run/lxd/unix.socket"),
    Path("/var/snap/lxd/common/lxd/unix.socket"),
)

sys.dont_write_bytecode = True


class GlobalLedgerError(RuntimeError):
    """The shared ledger failed closed."""


@dataclass(frozen=True)
class CampaignPolicy:
    policy_id: str
    namespace_kind: str
    namespace_fields: tuple[str, ...]
    identity_fields: tuple[str, ...]


POLICIES = {
    "p2-5g8-input-off-v1": CampaignPolicy(
        policy_id="p2-5g8-input-off-v1",
        namespace_kind="5g8_input_off_board_run_id_v1",
        namespace_fields=("board_id", "run_id"),
        identity_fields=("board_id", "run_id", "run_root", "capture_root", "plan_path"),
    ),
    "t7-5g8-fine-frequency-v1": CampaignPolicy(
        policy_id="t7-5g8-fine-frequency-v1",
        namespace_kind="5g8_fine_frequency_board_run_id_v1",
        namespace_fields=("board_id", "run_id"),
        identity_fields=("board_id", "run_id", "plan_path"),
    ),
    "t6-5g8-port-pair-matrix-v1": CampaignPolicy(
        policy_id="t6-5g8-port-pair-matrix-v1",
        namespace_kind="5g8_port_pair_board_campaign_cell_repeat_run_id_v1",
        namespace_fields=(
            "board_id",
            "campaign_id",
            "cell_id",
            "repeat_index",
            "run_id",
        ),
        identity_fields=(
            "board_id",
            "campaign_id",
            "cell_id",
            "repeat_index",
            "run_id",
            "plan_contract_sha256",
            "run_root",
            "plan_path",
        ),
    ),
}


def canonical_sha256(value: object) -> str:
    wire = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return hashlib.sha256(wire).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while chunk := os.read(descriptor, 1 << 20):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_directory_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = _open_absolute_directory(path)
    try:
        _fsync_directory_fd(descriptor)
    finally:
        os.close(descriptor)


def _assert_no_symlink_chain(path: Path, label: str) -> Path:
    exact = path.expanduser().absolute()
    if not exact.is_absolute() or ".." in exact.parts:
        raise GlobalLedgerError(f"{label} is not an absolute normalized path")
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current /= part
        if current.is_symlink():
            raise GlobalLedgerError(f"{label} contains a symlink: {current}")
    return exact


def _open_absolute_directory(path: Path) -> int:
    exact = _assert_no_symlink_chain(path, "ledger directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(exact.anchor, flags)
    try:
        for part in exact.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def _identity_from_stat(path: Path, observed: os.stat_result) -> dict[str, Any]:
    return {"path": str(path), "st_dev": int(observed.st_dev), "st_ino": int(observed.st_ino)}


def inode_identity(
    path: Path,
    *,
    directory: bool,
    label: str,
    expected_nlink: int | None = None,
) -> dict[str, Any]:
    exact = _assert_no_symlink_chain(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(exact, flags)
    try:
        observed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if directory != stat.S_ISDIR(observed.st_mode) or (
        not directory and not stat.S_ISREG(observed.st_mode)
    ):
        raise GlobalLedgerError(f"{label} has the wrong file type")
    if expected_nlink is not None and observed.st_nlink != expected_nlink:
        raise GlobalLedgerError(f"{label} hard-link count differs")
    return _identity_from_stat(exact, observed)


def file_evidence(path: Path, *, label: str, expected_nlink: int = 1) -> dict[str, Any]:
    identity = inode_identity(path, directory=False, label=label, expected_nlink=expected_nlink)
    observed = path.stat(follow_symlinks=False)
    return {
        **identity,
        "size_bytes": int(observed.st_size),
        "mode": stat.S_IMODE(observed.st_mode),
        "nlink": int(observed.st_nlink),
        "sha256": sha256_path(path),
    }


def _require_root_owned_mode(path: Path, mode: int, label: str) -> None:
    observed = path.stat(follow_symlinks=False)
    if observed.st_uid != 0 or observed.st_gid != 0 or stat.S_IMODE(observed.st_mode) != mode:
        raise GlobalLedgerError(f"{label} is not root-owned mode {mode:04o}")


def _require_root_controlled_ancestry(path: Path, label: str) -> None:
    current = path.parent
    while True:
        observed = current.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_gid != 0
            or observed.st_mode & 0o022
        ):
            raise GlobalLedgerError(f"{label} has runner-writable or non-root ancestry: {current}")
        if current.parent == current:
            return
        current = current.parent


def policy_document(policy: CampaignPolicy) -> dict[str, Any]:
    document = asdict(policy)
    document["namespace_fields"] = list(policy.namespace_fields)
    document["identity_fields"] = list(policy.identity_fields)
    return {"schema": 1, **document}


def policies_document() -> list[dict[str, Any]]:
    return [policy_document(POLICIES[key]) for key in sorted(POLICIES)]


def sudoers_text(runner_user: str = RUNNER_USER) -> str:
    if runner_user != RUNNER_USER:
        raise GlobalLedgerError("ledger runner must be the source-fixed service account")
    commands = ", ".join(f"{HELPER_PATH} {operation}" for operation in OPERATIONS)
    return f"Defaults!{HELPER_PATH} env_reset\n{runner_user} ALL=(root) NOPASSWD: {commands}\n"


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _assert_no_symlink_chain(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GlobalLedgerError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise GlobalLedgerError(f"{label} is not one JSON object")
    return value


def _require_no_acl(path: Path, label: str) -> None:
    try:
        attributes = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise GlobalLedgerError(f"cannot inspect {label} ACLs: {error}") from error
    if any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in attributes):
        raise GlobalLedgerError(f"{label} has an ACL outside the fixed authority model")


def _runner_account_document(
    *, require_home: bool = True, require_password_lock: bool = True
) -> dict[str, Any]:
    try:
        account = pwd.getpwnam(RUNNER_USER)
    except KeyError as error:
        raise GlobalLedgerError("source-fixed ledger runner account does not exist") from error
    if (
        account.pw_uid != RUNNER_UID
        or account.pw_gid != RUNNER_GID
        or account.pw_dir != str(RUNNER_HOME)
        or account.pw_shell != RUNNER_SHELL
    ):
        raise GlobalLedgerError("source-fixed ledger runner passwd identity differs")
    try:
        primary = grp.getgrgid(RUNNER_GID)
    except KeyError as error:
        raise GlobalLedgerError("source-fixed ledger runner group does not exist") from error
    supplementary = sorted(
        group.gr_gid
        for group in grp.getgrall()
        if RUNNER_USER in group.gr_mem and group.gr_gid != RUNNER_GID
    )
    if primary.gr_name != RUNNER_USER or primary.gr_mem or supplementary:
        raise GlobalLedgerError("ledger runner has a non-fixed or supplementary group grant")
    if require_password_lock:
        try:
            import spwd  # type: ignore[deprecated]

            password = spwd.getspnam(RUNNER_USER).sp_pwdp
        except (ImportError, KeyError, PermissionError) as error:
            raise GlobalLedgerError("cannot attest the ledger runner password lock") from error
        if not isinstance(password, str) or not password.startswith(("!", "*")):
            raise GlobalLedgerError("ledger runner password is not locked")
    if require_home:
        _assert_no_symlink_chain(RUNNER_HOME, "ledger runner home")
        observed = RUNNER_HOME.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != RUNNER_UID
            or observed.st_gid != RUNNER_GID
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise GlobalLedgerError("ledger runner home ownership or mode differs")
        _require_no_acl(RUNNER_HOME, "ledger runner home")
    document = {
        "user": RUNNER_USER,
        "uid": RUNNER_UID,
        "gid": RUNNER_GID,
        "home": str(RUNNER_HOME),
        "shell": RUNNER_SHELL,
        "supplementary_groups": [],
    }
    if require_password_lock:
        document["password_locked"] = True
    return document


def _socket_accessible_to_runner(path: Path) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(observed.st_mode):
        raise GlobalLedgerError(f"privileged socket path has the wrong type: {path}")
    _require_no_acl(path, f"privileged socket {path}")
    mode = stat.S_IMODE(observed.st_mode)
    if observed.st_uid == RUNNER_UID:
        return bool(mode & 0o600)
    if observed.st_gid == RUNNER_GID:
        return bool(mode & 0o060)
    return bool(mode & 0o006)


def attest_runner_runtime() -> dict[str, Any]:
    if (
        os.getuid() != RUNNER_UID
        or os.geteuid() != RUNNER_UID
        or os.getgid() != RUNNER_GID
        or os.getegid() != RUNNER_GID
        or any(group != RUNNER_GID for group in os.getgroups())
    ):
        raise GlobalLedgerError("current process is not the isolated ledger service account")
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError as error:
        raise GlobalLedgerError("cannot attest ledger runner process capabilities") from error
    capabilities: dict[str, int] = {}
    for field in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        match = re.search(rf"^{field}:\s*([0-9A-Fa-f]+)$", status, re.MULTILINE)
        if match is None:
            raise GlobalLedgerError("ledger runner capability evidence is missing")
        capabilities[field] = int(match.group(1), 16)
    if any(capabilities.values()):
        raise GlobalLedgerError("ledger runner process has runtime capabilities")
    accessible = [
        str(path) for path in DANGEROUS_SOCKET_PATHS if _socket_accessible_to_runner(path)
    ]
    if accessible:
        raise GlobalLedgerError("ledger runner can access a privileged control socket")
    account = _runner_account_document(require_password_lock=False)
    return {
        "schema": 1,
        "runtime_kind": "smateway_isolated_ledger_runner_runtime_v1",
        "runner": account,
        "password_lock_revalidated_by_privileged_attest": True,
        "capabilities": {key: "0" for key in sorted(capabilities)},
        "privileged_socket_access": [],
    }


def effective_sudo_policy_document(*, sudoers_file: Path = SUDOERS_ROOT) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise GlobalLedgerError("effective sudo-policy attestation requires root")
    if not CVTSUDOERS_PATH.is_file():
        raise GlobalLedgerError("cvtsudoers is required for effective sudo-policy attestation")
    try:
        completed = subprocess.run(
            (
                str(CVTSUDOERS_PATH),
                "-f",
                "JSON",
                "-e",
                "-M",
                "-m",
                f"user={RUNNER_USER}",
                str(sudoers_file),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GlobalLedgerError(f"cannot inspect effective sudo policy: {error}") from error
    if completed.returncode != 0:
        raise GlobalLedgerError(
            "effective sudo-policy inspection failed: " + completed.stderr.strip()
        )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise GlobalLedgerError("cvtsudoers returned malformed JSON") from error
    specs = parsed.get("User_Specs") if isinstance(parsed, Mapping) else None
    if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], Mapping):
        raise GlobalLedgerError("ledger runner effective sudo grants are not exactly one rule")
    spec = specs[0]
    if spec.get("User_List") != [{"username": RUNNER_USER}] or spec.get("Host_List") != [
        {"hostname": "ALL"}
    ]:
        raise GlobalLedgerError("ledger runner effective sudo principal or host differs")
    command_specs = spec.get("Cmnd_Specs")
    if not isinstance(command_specs, list) or len(command_specs) != 1:
        raise GlobalLedgerError("ledger runner effective sudo command set is ambiguous")
    command_spec = command_specs[0]
    if not isinstance(command_spec, Mapping):
        raise GlobalLedgerError("ledger runner effective sudo command set is malformed")
    if command_spec.get("runasusers") != [{"username": "root"}] or command_spec.get(
        "runasgroups", []
    ) not in ([], None):
        raise GlobalLedgerError("ledger runner sudo run-as grant is broader than root-only")
    if command_spec.get("Options") != [{"authenticate": False}]:
        raise GlobalLedgerError("ledger runner sudo options differ from NOPASSWD without SETENV")
    expected_commands = [{"command": f"{HELPER_PATH} {operation}"} for operation in OPERATIONS]
    if command_spec.get("Commands") != expected_commands:
        raise GlobalLedgerError("ledger runner effective sudo commands differ from the allowlist")
    if set(command_spec) != {"runasusers", "Options", "Commands"}:
        raise GlobalLedgerError("ledger runner effective sudo command attributes are broader")
    return {
        "schema": 1,
        "policy_kind": "smateway_ledger_effective_sudo_policy_v1",
        "runner_user": RUNNER_USER,
        "runas_user": "root",
        "authenticate": False,
        "setenv": False,
        "commands": [item["command"] for item in expected_commands],
        "additional_grants": False,
    }


def build_root_seal_document() -> dict[str, Any]:
    root_identity = inode_identity(GLOBAL_ROOT, directory=True, label="global ledger root")
    entries_identity = inode_identity(
        GLOBAL_ROOT / ENTRIES_DIRECTORY,
        directory=True,
        label="global run-ledgers directory",
    )
    anchors_identity = inode_identity(
        GLOBAL_ROOT / ANCHORS_DIRECTORY,
        directory=True,
        label="global inode-anchors directory",
    )
    document = {
        "schema": 1,
        "seal_kind": "smateway_global_run_ledger_root_seal_v1",
        "api": API,
        "runner": _runner_account_document(),
        "policy_registry": policies_document(),
        "global_root": root_identity,
        "run_ledgers_directory": entries_identity,
        "inode_anchors_directory": anchors_identity,
        "privileged_helper": file_evidence(HELPER_PATH, label="shared ledger helper"),
        "sudo_binary": file_evidence(SUDO_PATH, label="sudo binary"),
        "sudoers_policy": file_evidence(SUDOERS_PATH, label="shared ledger sudoers policy"),
        "effective_sudo_policy": effective_sudo_policy_document(),
        "local_storage_device": int(GLOBAL_ROOT.stat().st_dev),
        "mutation_command_prefix": [str(SUDO_PATH), "-n", "--", str(HELPER_PATH)],
        "root_owned_nonwritable_authority": True,
        "administrative_pi_account_outside_trust_boundary": True,
    }
    return document


def attest_fixed_storage(*, require_runner_identity: bool) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise GlobalLedgerError("fixed storage attestation must run in the privileged helper")
    for path in (GLOBAL_ROOT, GLOBAL_SEAL, HELPER_PATH, SUDO_PATH, SUDOERS_PATH):
        _assert_no_symlink_chain(path, "fixed shared ledger authority")
        _require_root_controlled_ancestry(path, "fixed shared ledger authority")
    entries = GLOBAL_ROOT / ENTRIES_DIRECTORY
    anchors = GLOBAL_ROOT / ANCHORS_DIRECTORY
    for path, label in (
        (GLOBAL_ROOT, "global ledger root"),
        (entries, "global run-ledgers directory"),
        (anchors, "global inode-anchors directory"),
    ):
        inode_identity(path, directory=True, label=label)
        _require_root_owned_mode(path, DIRECTORY_MODE, label)
        _require_no_acl(path, label)
    _require_root_owned_mode(GLOBAL_SEAL, SEALED_FILE_MODE, "global ledger root seal")
    _require_root_owned_mode(HELPER_PATH, HELPER_MODE, "shared ledger helper")
    _require_root_owned_mode(SUDOERS_PATH, SUDOERS_MODE, "shared ledger sudoers policy")
    for path, label in (
        (GLOBAL_SEAL, "global ledger root seal"),
        (HELPER_PATH, "shared ledger helper"),
        (SUDOERS_PATH, "shared ledger sudoers policy"),
    ):
        _require_no_acl(path, label)
    sudo_stat = SUDO_PATH.stat(follow_symlinks=False)
    if (
        sudo_stat.st_uid != 0
        or sudo_stat.st_gid != 0
        or not sudo_stat.st_mode & stat.S_ISUID
        or sudo_stat.st_mode & 0o022
        or not os.access(SUDO_PATH, os.X_OK)
    ):
        raise GlobalLedgerError("sudo binary is not the root-owned setuid trust boundary")
    seal = _read_json(GLOBAL_SEAL, "global ledger root seal")
    runner = seal.get("runner")
    if not isinstance(runner, Mapping):
        raise GlobalLedgerError("global ledger seal lacks runner identity")
    expected = build_root_seal_document()
    if seal != expected:
        raise GlobalLedgerError("global ledger authority differs from its root seal")
    if SUDOERS_PATH.read_text(encoding="utf-8") != sudoers_text():
        raise GlobalLedgerError("sudoers policy content differs from the sealed narrow rule")
    if seal.get("effective_sudo_policy") != effective_sudo_policy_document():
        raise GlobalLedgerError("effective sudo grants differ from the root seal")
    if require_runner_identity:
        raise GlobalLedgerError("runner identity must be attested before entering sudo")
    device = int(seal["local_storage_device"])
    for path in (
        GLOBAL_ROOT,
        entries,
        anchors,
        GLOBAL_SEAL,
        HELPER_PATH,
        SUDO_PATH,
        SUDOERS_PATH,
    ):
        if path.stat().st_dev != device:
            raise GlobalLedgerError("shared ledger authority escaped the sealed local device")
    document = {
        "schema": 1,
        "storage_kind": "smateway_fixed_global_run_ledger_storage_v1",
        "api": API,
        "global_root": seal["global_root"],
        "run_ledgers_directory": seal["run_ledgers_directory"],
        "anchor_directory": seal["inode_anchors_directory"],
        "global_root_seal": file_evidence(GLOBAL_SEAL, label="global ledger root seal"),
        "global_root_seal_document_sha256": canonical_sha256(seal),
        "privileged_helper": seal["privileged_helper"],
        "sudo_binary": seal["sudo_binary"],
        "sudoers_policy": seal["sudoers_policy"],
        "effective_sudo_policy_sha256": canonical_sha256(seal["effective_sudo_policy"]),
        "runner": dict(runner),
        "policy_registry_sha256": canonical_sha256(seal["policy_registry"]),
        "mutation_command_prefix": seal["mutation_command_prefix"],
        "local_storage_device": device,
        "os_enforced_trust_boundary": True,
        "private_test_storage": False,
    }
    return validate_storage_document(document)


def _validate_structural_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise GlobalLedgerError(f"{label} path is malformed")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise GlobalLedgerError(f"{label} path is not absolute and normalized")
    return value


def _validate_structural_inode(
    value: object, *, label: str, expected_path: Path | None
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "st_dev", "st_ino"}:
        raise GlobalLedgerError(f"{label} inode evidence is malformed")
    normalized = dict(value)
    path = _validate_structural_path(normalized["path"], label=label)
    if expected_path is not None and path != str(expected_path):
        raise GlobalLedgerError(f"{label} path differs from the fixed authority")
    for field in ("st_dev", "st_ino"):
        item = normalized[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise GlobalLedgerError(f"{label} {field} is malformed")
    return normalized


def _validate_structural_file_evidence(
    value: object, *, label: str, expected_path: Path | None
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "st_dev",
        "st_ino",
        "size_bytes",
        "mode",
        "nlink",
        "sha256",
    }:
        raise GlobalLedgerError(f"{label} file evidence is malformed")
    normalized = dict(value)
    _validate_structural_inode(
        {field: normalized[field] for field in ("path", "st_dev", "st_ino")},
        label=label,
        expected_path=expected_path,
    )
    if not isinstance(normalized["sha256"], str) or SHA256.fullmatch(normalized["sha256"]) is None:
        raise GlobalLedgerError(f"{label} SHA-256 is malformed")
    size = normalized["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise GlobalLedgerError(f"{label} size is malformed")
    mode = normalized["mode"]
    nlink = normalized["nlink"]
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise GlobalLedgerError(f"{label} mode is malformed")
    if isinstance(nlink, bool) or not isinstance(nlink, int) or nlink <= 0:
        raise GlobalLedgerError(f"{label} link count is malformed")
    return normalized


def validate_storage_document(value: object, *, allow_private_test: bool = False) -> dict[str, Any]:
    """Purely validate an embedded storage attestation's exact structure.

    This intentionally does not attest the live filesystem.  Execution paths must
    still call :func:`attest_fixed_storage` through ``SudoLedgerBackend`` and compare
    the complete document.  ``allow_private_test`` is only for explicitly injected
    offline test adapters.
    """

    expected_keys = {
        "schema",
        "storage_kind",
        "api",
        "global_root",
        "run_ledgers_directory",
        "anchor_directory",
        "global_root_seal",
        "global_root_seal_document_sha256",
        "privileged_helper",
        "sudo_binary",
        "sudoers_policy",
        "effective_sudo_policy_sha256",
        "runner",
        "policy_registry_sha256",
        "mutation_command_prefix",
        "local_storage_device",
        "os_enforced_trust_boundary",
        "private_test_storage",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise GlobalLedgerError("global-ledger storage document fields differ")
    storage = dict(value)
    if not isinstance(storage.get("private_test_storage"), bool) or not isinstance(
        storage.get("os_enforced_trust_boundary"), bool
    ):
        raise GlobalLedgerError("global-ledger storage trust flags are malformed")
    private = storage.get("private_test_storage") is True
    if private and not allow_private_test:
        raise GlobalLedgerError("private test ledger storage is forbidden")
    expected_kind = (
        "smateway_private_test_global_run_ledger_storage_v1"
        if private
        else "smateway_fixed_global_run_ledger_storage_v1"
    )
    if (
        storage.get("schema") != 1
        or storage.get("api") != API
        or storage.get("storage_kind") != expected_kind
        or storage.get("os_enforced_trust_boundary") is not (not private)
    ):
        raise GlobalLedgerError("global-ledger storage trust flags differ")

    root_path = None if private else GLOBAL_ROOT
    entries_path = None if private else GLOBAL_ROOT / ENTRIES_DIRECTORY
    anchors_path = None if private else GLOBAL_ROOT / ANCHORS_DIRECTORY
    root = _validate_structural_inode(
        storage["global_root"], label="global ledger root", expected_path=root_path
    )
    entries = _validate_structural_inode(
        storage["run_ledgers_directory"],
        label="global run-ledgers directory",
        expected_path=entries_path,
    )
    anchors = _validate_structural_inode(
        storage["anchor_directory"],
        label="global inode-anchors directory",
        expected_path=anchors_path,
    )
    production_files: list[dict[str, Any]] = []
    if private:
        root_value = Path(str(root["path"]))
        if (
            Path(str(entries["path"])) != root_value / ENTRIES_DIRECTORY
            or Path(str(anchors["path"])) != root_value / ANCHORS_DIRECTORY
        ):
            raise GlobalLedgerError("private test ledger directories are not nested exactly")
        seal = storage["global_root_seal"]
        if not isinstance(seal, Mapping) or set(seal) != {"path"}:
            raise GlobalLedgerError("private test ledger seal placeholder is malformed")
        _validate_structural_path(seal["path"], label="private test ledger seal")
        if (
            any(
                storage[field] is not None
                for field in ("privileged_helper", "sudo_binary", "sudoers_policy")
            )
            or storage["mutation_command_prefix"] is not None
        ):
            raise GlobalLedgerError("private test storage claims privileged production files")
    else:
        production_files = [
            _validate_structural_file_evidence(
                storage["global_root_seal"],
                label="global ledger root seal",
                expected_path=GLOBAL_SEAL,
            )
        ]
        for field, label, path in (
            ("privileged_helper", "shared ledger helper", HELPER_PATH),
            ("sudo_binary", "sudo binary", SUDO_PATH),
            ("sudoers_policy", "shared ledger sudoers policy", SUDOERS_PATH),
        ):
            production_files.append(
                _validate_structural_file_evidence(storage[field], label=label, expected_path=path)
            )
        if storage["mutation_command_prefix"] != [
            str(SUDO_PATH),
            "-n",
            "--",
            str(HELPER_PATH),
        ]:
            raise GlobalLedgerError("shared ledger mutation command prefix differs")

    for field in (
        "global_root_seal_document_sha256",
        "policy_registry_sha256",
        "effective_sudo_policy_sha256",
    ):
        item = storage[field]
        if not isinstance(item, str) or SHA256.fullmatch(item) is None:
            raise GlobalLedgerError(f"global-ledger {field} is malformed")
    runner = storage["runner"]
    if not isinstance(runner, Mapping) or set(runner) != {
        "user",
        "uid",
        "gid",
        "home",
        "shell",
        "password_locked",
        "supplementary_groups",
    }:
        raise GlobalLedgerError("global-ledger runner identity is malformed")
    if (
        (not private and runner.get("user") != RUNNER_USER)
        or (not private and runner.get("uid") != RUNNER_UID)
        or (not private and runner.get("gid") != RUNNER_GID)
        or (not private and runner.get("home") != str(RUNNER_HOME))
        or (not private and runner.get("shell") != RUNNER_SHELL)
        or runner.get("password_locked") is not True
        or runner.get("supplementary_groups") != []
    ):
        raise GlobalLedgerError("global-ledger runner identity differs from fixed policy")
    _require_identifier(runner["user"], "ledger runner user")
    for field in ("uid", "gid"):
        item = runner[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise GlobalLedgerError(f"ledger runner {field} is malformed")
    device = storage["local_storage_device"]
    if isinstance(device, bool) or not isinstance(device, int) or device <= 0:
        raise GlobalLedgerError("global-ledger local storage device is malformed")
    if any(int(value["st_dev"]) != device for value in (root, entries, anchors)) or (
        not private and any(int(value["st_dev"]) != device for value in production_files)
    ):
        raise GlobalLedgerError("global-ledger storage evidence spans multiple devices")
    if storage["policy_registry_sha256"] != canonical_sha256(policies_document()):
        raise GlobalLedgerError("global-ledger policy registry differs from source")
    if not private and int(runner["uid"]) == 0:
        raise GlobalLedgerError("production ledger runner must be unprivileged")
    return storage


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise GlobalLedgerError(f"{label} is malformed")
    return value


def validate_namespace(policy: CampaignPolicy, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GlobalLedgerError("global run namespace is missing")
    namespace = dict(value)
    expected_keys = {"schema", "policy_id", "namespace_kind", *policy.namespace_fields}
    if (
        set(namespace) != expected_keys
        or namespace.get("schema") != 1
        or namespace.get("policy_id") != policy.policy_id
        or namespace.get("namespace_kind") != policy.namespace_kind
    ):
        raise GlobalLedgerError("global run namespace differs from fixed campaign policy")
    for field in policy.namespace_fields:
        item = namespace[field]
        if field == "repeat_index":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise GlobalLedgerError("namespace repeat_index is malformed")
        else:
            _require_identifier(item, f"namespace {field}")
    return namespace


def validate_canonical_identity(
    policy: CampaignPolicy,
    value: object,
    *,
    namespace: Mapping[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GlobalLedgerError("canonical run identity is missing")
    identity = dict(value)
    if set(identity) != {"schema", *policy.identity_fields} or identity.get("schema") != 1:
        raise GlobalLedgerError("canonical run identity fields differ from campaign policy")
    for field in set(policy.identity_fields).intersection(policy.namespace_fields):
        if identity[field] != namespace[field]:
            raise GlobalLedgerError(f"canonical identity {field} differs from namespace")
    for field in policy.identity_fields:
        item = identity[field]
        if field == "repeat_index":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise GlobalLedgerError("canonical repeat_index is malformed")
        elif field.endswith("_sha256"):
            if not isinstance(item, str) or SHA256.fullmatch(item) is None:
                raise GlobalLedgerError(f"canonical {field} is not SHA-256")
        elif field.endswith("_path") or field.endswith("_root"):
            if not isinstance(item, str):
                raise GlobalLedgerError(f"canonical {field} is not a path string")
            path = Path(item)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or str(path) != item
                or not path.is_relative_to(state_root)
            ):
                raise GlobalLedgerError(f"canonical {field} escapes the state root")
        else:
            _require_identifier(item, f"canonical {field}")
    return identity


def build_authority(
    *,
    policy: CampaignPolicy,
    namespace: Mapping[str, Any],
    canonical_identity: Mapping[str, Any],
    state_root: Path,
    storage: Mapping[str, Any],
) -> dict[str, Any]:
    exact_state = state_root.expanduser().absolute()
    normalized_storage = validate_storage_document(
        storage, allow_private_test=storage.get("private_test_storage") is True
    )
    normalized_namespace = validate_namespace(policy, namespace)
    normalized_identity = validate_canonical_identity(
        policy, canonical_identity, namespace=normalized_namespace, state_root=exact_state
    )
    root = Path(str(normalized_storage["global_root"]["path"]))
    seal = Path(str(normalized_storage["global_root_seal"]["path"]))
    if (
        root == exact_state
        or root.is_relative_to(exact_state)
        or exact_state.is_relative_to(root)
        or seal == exact_state
        or seal.is_relative_to(exact_state)
    ):
        raise GlobalLedgerError("global ledger authority overlaps caller state storage")
    ledger_key = canonical_sha256(normalized_namespace)
    entries = Path(str(normalized_storage["run_ledgers_directory"]["path"]))
    return {
        "schema": 1,
        "authority_kind": "smateway_fixed_global_run_ledger_authority_v1",
        "policy": policy_document(policy),
        "storage": normalized_storage,
        "global_run_namespace": normalized_namespace,
        "canonical_run_identity": normalized_identity,
        "canonical_run_identity_sha256": canonical_sha256(normalized_identity),
        "state_root": str(exact_state),
        "ledger_key": ledger_key,
        "ledger_directory_path": str(entries / ledger_key),
        "authority_is_source_fixed_and_external": True,
    }


def production_authority(
    *,
    policy_id: str,
    namespace: Mapping[str, Any],
    canonical_identity: Mapping[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    policy = POLICIES.get(policy_id)
    if policy is None:
        raise GlobalLedgerError("unknown global-ledger campaign policy")
    return build_authority(
        policy=policy,
        namespace=namespace,
        canonical_identity=canonical_identity,
        state_root=state_root,
        storage=SudoLedgerBackend().storage(),
    )


def authority_from_storage(
    *,
    policy_id: str,
    namespace: Mapping[str, Any],
    canonical_identity: Mapping[str, Any],
    state_root: Path,
    backend: LedgerBackend,
) -> dict[str, Any]:
    policy = POLICIES.get(policy_id)
    if policy is None:
        raise GlobalLedgerError("unknown global-ledger campaign policy")
    return build_authority(
        policy=policy,
        namespace=namespace,
        canonical_identity=canonical_identity,
        state_root=state_root,
        storage=backend.storage(),
    )


def validate_authority(
    value: object,
    *,
    policy_id: str,
    namespace: Mapping[str, Any],
    canonical_identity: Mapping[str, Any],
    state_root: Path,
    backend: LedgerBackend,
) -> dict[str, Any]:
    expected = authority_from_storage(
        policy_id=policy_id,
        namespace=namespace,
        canonical_identity=canonical_identity,
        state_root=state_root,
        backend=backend,
    )
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise GlobalLedgerError("global-ledger authority differs from current fixed storage")
    return expected


RECEIPT_SCHEMAS: dict[str, dict[str, tuple[int, str, str]]] = {
    "p2-5g8-input-off-v1": {
        "reservation": (1, "marker_kind", "5g8_input_off_global_run_id_reservation_v1"),
        "execution": (1, "marker_kind", "5g8_input_off_global_execution_consumed_v1"),
        "failure": (1, "marker_kind", "5g8_input_off_global_failure_receipt_v1"),
    },
    "t7-5g8-fine-frequency-v1": {
        "reservation": (3, "marker_kind", "5g8_fine_frequency_global_run_id_reservation_v3"),
        "execution": (3, "marker_kind", "5g8_fine_frequency_global_execution_consumed_v3"),
        "failure": (2, "marker_kind", "5g8_fine_frequency_external_failure_receipt_v2"),
    },
    "t6-5g8-port-pair-matrix-v1": {
        "reservation": (1, "marker_kind", "5g8_port_pair_permanent_run_reservation_v1"),
        "execution": (1, "marker_kind", "5g8_port_pair_irreversible_execution_burn_v1"),
        "failure": (1, "receipt_kind", "5g8_port_pair_emergency_failure_receipt_v1"),
    },
}


def validate_receipt_document(
    document: object,
    *,
    authority: Mapping[str, Any],
    role: str,
    execution_nonce: str | None = None,
) -> dict[str, Any]:
    """Validate the source-fixed campaign receipt schema and authority binding."""

    policy_id = str(authority["policy"]["policy_id"])
    descriptor = RECEIPT_SCHEMAS.get(policy_id, {}).get(role)
    if descriptor is None or not isinstance(document, Mapping):
        raise GlobalLedgerError("ledger receipt role is not registered for this campaign")
    normalized = dict(document)
    schema, discriminator, expected_kind = descriptor
    if (
        normalized.get("schema") != schema
        or normalized.get(discriminator) != expected_kind
        or normalized.get("shared_global_ledger_authority") != authority_receipt_binding(authority)
    ):
        raise GlobalLedgerError("ledger receipt differs from its campaign-specific schema")
    namespace = authority["global_run_namespace"]
    for field in authority["policy"]["namespace_fields"]:
        if field in normalized and normalized[field] != namespace[field]:
            raise GlobalLedgerError(f"ledger receipt {field} differs from its namespace")
    if role == "execution":
        if (
            execution_nonce is None
            or EXECUTION_NONCE.fullmatch(execution_nonce) is None
            or normalized.get("execution_nonce") != execution_nonce
        ):
            raise GlobalLedgerError("execution receipt lacks the exact execution nonce")
        if normalized.get("automatic_retry_forbidden", True) is not True:
            raise GlobalLedgerError("execution receipt permits automatic retry")
    elif role == "reservation" and "execution_nonce" in normalized:
        raise GlobalLedgerError("non-execution receipt unexpectedly carries an execution nonce")
    if role == "failure":
        if normalized.get("automatic_retry_forbidden") is not True:
            raise GlobalLedgerError("failure receipt permits automatic retry")
        failure_nonce = normalized.get("execution_nonce")
        if execution_nonce is not None and failure_nonce != execution_nonce:
            raise GlobalLedgerError("failure receipt differs from the committed execution nonce")
        if failure_nonce is not None and (
            not isinstance(failure_nonce, str) or EXECUTION_NONCE.fullmatch(failure_nonce) is None
        ):
            raise GlobalLedgerError("failure receipt execution nonce is malformed")
    return normalized


def mutation_request(
    *,
    authority: Mapping[str, Any],
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if operation not in OPERATIONS or operation == "attest":
        raise GlobalLedgerError("unknown shared-ledger operation")
    expected_payload = {
        "reserve_run": {"reservation_id"},
        "seal_slot": {"slot", "expected_identity", "document"},
        "burn_run": {"execution_nonce", "expected_guard_identity", "document"},
    }[operation]
    if set(payload) != expected_payload:
        raise GlobalLedgerError("shared-ledger mutation payload fields differ")
    return {
        "schema": 1,
        "api": API,
        "operation": operation,
        "policy_id": authority["policy"]["policy_id"],
        "authority": dict(authority),
        "authority_sha256": canonical_sha256(authority),
        **dict(payload),
    }


def _validate_identity_shape(value: object, *, file_evidence_required: bool) -> None:
    if not isinstance(value, Mapping):
        raise GlobalLedgerError("privileged response inode evidence is missing")
    expected = {"path", "st_dev", "st_ino"}
    if file_evidence_required:
        expected |= {"size_bytes", "mode", "nlink", "sha256"}
    if (
        set(value) != expected
        or not isinstance(value.get("path"), str)
        or not Path(str(value["path"])).is_absolute()
        or isinstance(value.get("st_dev"), bool)
        or not isinstance(value.get("st_dev"), int)
        or isinstance(value.get("st_ino"), bool)
        or not isinstance(value.get("st_ino"), int)
    ):
        raise GlobalLedgerError("privileged response inode evidence is malformed")
    if file_evidence_required and (
        not isinstance(value.get("sha256"), str)
        or SHA256.fullmatch(str(value["sha256"])) is None
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or isinstance(value.get("mode"), bool)
        or not isinstance(value.get("mode"), int)
        or isinstance(value.get("nlink"), bool)
        or not isinstance(value.get("nlink"), int)
    ):
        raise GlobalLedgerError("privileged response file evidence is malformed")


def _document_wire(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _require_exact_file_response(
    value: object,
    *,
    path: Path,
    size_bytes: int,
    mode: int,
    nlink: int,
    sha256: str,
    label: str,
) -> dict[str, Any]:
    _validate_identity_shape(value, file_evidence_required=True)
    normalized = dict(value)  # type: ignore[arg-type]
    if (
        normalized["path"] != str(path)
        or normalized["size_bytes"] != size_bytes
        or normalized["mode"] != mode
        or normalized["nlink"] != nlink
        or normalized["sha256"] != sha256
    ):
        raise GlobalLedgerError(f"privileged {label} response evidence differs")
    return normalized


def validate_response(request: Mapping[str, Any], response: object) -> dict[str, Any]:
    authority = request.get("authority")
    if not isinstance(authority, Mapping) or not isinstance(response, Mapping):
        raise GlobalLedgerError("privileged shared-ledger response is missing")
    normalized = dict(response)
    if (
        set(normalized)
        != {"schema", "api", "operation", "policy_id", "ledger_key", "status", "evidence"}
        or normalized.get("schema") != 1
        or normalized.get("api") != API
        or normalized.get("operation") != request.get("operation")
        or normalized.get("policy_id") != authority["policy"]["policy_id"]
        or normalized.get("ledger_key") != authority["ledger_key"]
        or normalized.get("status") != "complete"
        or not isinstance(normalized.get("evidence"), Mapping)
    ):
        raise GlobalLedgerError("privileged shared-ledger response header differs")
    evidence = normalized["evidence"]
    operation = request["operation"]
    if operation == "reserve_run":
        if set(evidence) != {
            "reservation_id",
            "ledger_directory",
            "slots",
            "anchors",
            "slot_files",
            "anchor_files",
        }:
            raise GlobalLedgerError("privileged reserve response evidence differs")
        if evidence["reservation_id"] != request["reservation_id"]:
            raise GlobalLedgerError("privileged reserve response reservation differs")
        _validate_identity_shape(evidence["ledger_directory"], file_evidence_required=False)
        for collection in ("slots", "anchors"):
            values = evidence[collection]
            if not isinstance(values, Mapping) or set(values) != {
                "reservation",
                "burn-guard",
                "failure-receipt",
            }:
                raise GlobalLedgerError("privileged reserve response slot set differs")
            for value in values.values():
                _validate_identity_shape(value, file_evidence_required=False)
        for collection in ("slot_files", "anchor_files"):
            values = evidence[collection]
            if not isinstance(values, Mapping) or set(values) != {
                "reservation",
                "burn-guard",
                "failure-receipt",
            }:
                raise GlobalLedgerError("privileged reserve response file set differs")
            for value in values.values():
                _validate_identity_shape(value, file_evidence_required=True)
        ledger, reservation, guard, _marker, failure = _ledger_paths(authority)
        if evidence["ledger_directory"]["path"] != str(ledger):
            raise GlobalLedgerError("privileged reserve response ledger path differs")
        expected_slots = {
            "reservation": reservation,
            "burn-guard": guard,
            "failure-receipt": failure,
        }
        anchor_root = Path(str(authority["storage"]["anchor_directory"]["path"]))
        prefix = f".anchor.{authority['ledger_key']}.{request['reservation_id']}"
        for name, path in expected_slots.items():
            slot = evidence["slots"][name]
            anchor = evidence["anchors"][name]
            slot_file = evidence["slot_files"][name]
            anchor_file = evidence["anchor_files"][name]
            suffix = name
            if (
                slot["path"] != str(path)
                or anchor["path"] != str(anchor_root / f"{prefix}.{suffix}")
                or (slot["st_dev"], slot["st_ino"]) != (anchor["st_dev"], anchor["st_ino"])
                or {key: slot_file[key] for key in ("path", "st_dev", "st_ino")} != slot
                or {key: anchor_file[key] for key in ("path", "st_dev", "st_ino")} != anchor
                or any(
                    slot_file[field] != anchor_file[field]
                    for field in ("size_bytes", "mode", "nlink", "sha256")
                )
                or slot_file["size_bytes"] != 0
                or slot_file["mode"] != PREPARED_SLOT_MODE
                or slot_file["nlink"] != 2
                or slot_file["sha256"] != hashlib.sha256(b"").hexdigest()
            ):
                raise GlobalLedgerError("privileged reserve response inode binding differs")
    elif operation == "seal_slot":
        if set(evidence) != {"slot", "file", "document_sha256"}:
            raise GlobalLedgerError("privileged seal response evidence differs")
        slot = request.get("slot")
        if evidence["slot"] != slot or slot not in {"reservation", "failure"}:
            raise GlobalLedgerError("privileged seal response slot differs")
        document = request.get("document")
        if not isinstance(document, Mapping):
            raise GlobalLedgerError("privileged seal request document is missing")
        wire = _document_wire(document)
        _ledger, reservation, _guard, _marker, failure = _ledger_paths(authority)
        _require_exact_file_response(
            evidence["file"],
            path=reservation if slot == "reservation" else failure,
            size_bytes=len(wire),
            mode=SEALED_FILE_MODE,
            nlink=2,
            sha256=hashlib.sha256(wire).hexdigest(),
            label="seal",
        )
        if evidence["document_sha256"] != canonical_sha256(document):
            raise GlobalLedgerError("privileged seal response document hash differs")
    elif operation == "burn_run":
        if set(evidence) != {
            "state",
            "execution_nonce",
            "document_sha256",
            "guard",
            "marker",
        }:
            raise GlobalLedgerError("privileged burn response evidence differs")
        document = request.get("document")
        nonce = request.get("execution_nonce")
        if not isinstance(document, Mapping) or evidence.get("execution_nonce") != nonce:
            raise GlobalLedgerError("privileged burn response request binding differs")
        _ledger, _reservation, guard, marker, _failure = _ledger_paths(authority)
        _require_exact_file_response(
            evidence["guard"],
            path=guard,
            size_bytes=1,
            mode=SEALED_FILE_MODE,
            nlink=2,
            sha256=hashlib.sha256(b"\x01").hexdigest(),
            label="burn guard",
        )
        wire = _document_wire(document)
        _require_exact_file_response(
            evidence["marker"],
            path=marker,
            size_bytes=len(wire),
            mode=SEALED_FILE_MODE,
            nlink=1,
            sha256=hashlib.sha256(wire).hexdigest(),
            label="burn marker",
        )
        if evidence["state"] != "burn_complete" or evidence["document_sha256"] != canonical_sha256(
            document
        ):
            raise GlobalLedgerError("privileged burn completion evidence differs")
    else:
        raise GlobalLedgerError("privileged response operation is unsupported")
    return normalized


def authority_receipt_binding(authority: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "api": API,
        "policy_id": authority["policy"]["policy_id"],
        "ledger_key": authority["ledger_key"],
        "authority_sha256": canonical_sha256(authority),
        "global_run_namespace": authority["global_run_namespace"],
        "canonical_run_identity_sha256": authority["canonical_run_identity_sha256"],
    }


def validate_receipt_document_binding(
    document: object, *, authority: Mapping[str, Any]
) -> Mapping[str, Any]:
    if not isinstance(document, Mapping) or document.get(
        "shared_global_ledger_authority"
    ) != authority_receipt_binding(authority):
        raise GlobalLedgerError("ledger receipt document lacks the exact shared authority")
    return document


class LedgerBackend(Protocol):
    def storage(self) -> Mapping[str, Any]: ...

    def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def inspect(self, authority: Mapping[str, Any]) -> Mapping[str, Any]: ...


def attestation_request(
    *, query: str, authority: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if query == "storage":
        if authority is not None:
            raise GlobalLedgerError("storage attestation cannot carry a ledger authority")
        return {"schema": 1, "api": API, "operation": "attest", "query": "storage"}
    if query != "ledger_state" or authority is None:
        raise GlobalLedgerError("attestation query is invalid")
    return {
        "schema": 1,
        "api": API,
        "operation": "attest",
        "query": "ledger_state",
        "policy_id": authority["policy"]["policy_id"],
        "authority": dict(authority),
        "authority_sha256": canonical_sha256(authority),
    }


def validate_attestation_response(request: Mapping[str, Any], response: object) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise GlobalLedgerError("privileged attestation response is missing")
    normalized = dict(response)
    base = {"schema", "api", "operation", "query", "status", "evidence"}
    if (
        normalized.get("schema") != 1
        or normalized.get("api") != API
        or normalized.get("operation") != "attest"
        or normalized.get("query") != request.get("query")
        or normalized.get("status") != "complete"
    ):
        raise GlobalLedgerError("privileged attestation response header differs")
    if request.get("query") == "storage":
        if set(normalized) != base:
            raise GlobalLedgerError("privileged storage attestation fields differ")
        validate_storage_document(normalized.get("evidence"))
    else:
        authority = request.get("authority")
        if not isinstance(authority, Mapping) or set(normalized) != base | {
            "policy_id",
            "ledger_key",
            "authority_sha256",
        }:
            raise GlobalLedgerError("privileged ledger inspection fields differ")
        if (
            normalized.get("policy_id") != authority["policy"]["policy_id"]
            or normalized.get("ledger_key") != authority["ledger_key"]
            or normalized.get("authority_sha256") != canonical_sha256(authority)
        ):
            raise GlobalLedgerError("privileged ledger inspection binding differs")
        validate_inspection_evidence(authority, normalized.get("evidence"))
    return normalized


class SudoLedgerBackend:
    @staticmethod
    def _invoke(operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in OPERATIONS:
            raise GlobalLedgerError("production ledger operation is invalid")
        try:
            wire = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
            completed = subprocess.run(
                (str(SUDO_PATH), "-n", "--", str(HELPER_PATH), operation),
                input=wire,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
            raise GlobalLedgerError(
                f"cannot invoke privileged shared-ledger helper: {error}"
            ) from error
        if completed.returncode != 0:
            raise GlobalLedgerError(
                "privileged shared-ledger helper rejected request: " + completed.stderr.strip()
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise GlobalLedgerError("privileged helper returned malformed JSON") from error
        if not isinstance(value, Mapping):
            raise GlobalLedgerError("privileged helper returned a non-object response")
        return value

    def storage(self) -> Mapping[str, Any]:
        attest_runner_runtime()
        request = attestation_request(query="storage")
        return validate_attestation_response(request, self._invoke("attest", request))["evidence"]

    def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        operation = str(request.get("operation", ""))
        if operation == "attest":
            raise GlobalLedgerError("mutation cannot use the read-only attestation operation")
        attest_runner_runtime()
        return validate_response(request, self._invoke(operation, request))

    def inspect(self, authority: Mapping[str, Any]) -> Mapping[str, Any]:
        attest_runner_runtime()
        request = attestation_request(query="ledger_state", authority=authority)
        return validate_attestation_response(request, self._invoke("attest", request))["evidence"]


def _validated_request(
    operation: str,
    request: Mapping[str, Any],
    *,
    storage: Mapping[str, Any],
) -> tuple[CampaignPolicy, dict[str, Any], dict[str, Any]]:
    policy_id = request.get("policy_id")
    policy = POLICIES.get(str(policy_id))
    authority = request.get("authority")
    if (
        policy is None
        or request.get("schema") != 1
        or request.get("api") != API
        or request.get("operation") != operation
        or not isinstance(authority, Mapping)
        or request.get("authority_sha256") != canonical_sha256(authority)
    ):
        raise GlobalLedgerError("shared-ledger request header/authority is invalid")
    expected = build_authority(
        policy=policy,
        namespace=authority.get("global_run_namespace", {}),
        canonical_identity=authority.get("canonical_run_identity", {}),
        state_root=Path(str(authority.get("state_root", ""))),
        storage=storage,
    )
    if dict(authority) != expected:
        raise GlobalLedgerError("shared-ledger request authority differs from fixed policy")
    base_keys = {"schema", "api", "operation", "policy_id", "authority", "authority_sha256"}
    operation_keys = {
        "reserve_run": {"reservation_id"},
        "seal_slot": {"slot", "expected_identity", "document"},
        "burn_run": {"execution_nonce", "expected_guard_identity", "document"},
    }[operation]
    if set(request) != base_keys | operation_keys:
        raise GlobalLedgerError("shared-ledger request has unexpected or missing fields")
    return policy, expected, dict(request)


def _ledger_paths(authority: Mapping[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    ledger = Path(str(authority["ledger_directory_path"]))
    return (
        ledger,
        ledger / RESERVATION_FILENAME,
        ledger / BURN_GUARD_FILENAME,
        ledger / BURN_MARKER_FILENAME,
        ledger / FAILURE_RECEIPT_FILENAME,
    )


def _open_authority_directories(
    authority: Mapping[str, Any], storage: Mapping[str, Any]
) -> tuple[int, int, int | None]:
    root_fd = _open_absolute_directory(Path(str(storage["global_root"]["path"])))
    try:
        entries_fd = _open_child_directory(root_fd, ENTRIES_DIRECTORY)
        anchors_fd = _open_child_directory(root_fd, ANCHORS_DIRECTORY)
    except BaseException:
        os.close(root_fd)
        raise
    os.close(root_fd)
    _validate_directory_fd(
        entries_fd,
        identity=storage["run_ledgers_directory"],
        storage=storage,
        label="global run-ledgers directory",
    )
    _validate_directory_fd(
        anchors_fd,
        identity=storage["anchor_directory"],
        storage=storage,
        label="global inode-anchors directory",
    )
    ledger_fd: int | None = None
    ledger_key = str(authority["ledger_key"])
    with suppress(FileNotFoundError):
        ledger_fd = _open_child_directory(entries_fd, ledger_key)
    return entries_fd, anchors_fd, ledger_fd


def _bound_regular_identity(
    parent_fd: int,
    name: str,
    *,
    absolute_path: Path,
    expected_nlink: int,
) -> tuple[int, dict[str, Any], os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    observed = os.fstat(descriptor)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != expected_nlink:
        os.close(descriptor)
        raise GlobalLedgerError("shared-ledger slot type/link count differs")
    return descriptor, _identity_from_stat(absolute_path, observed), observed


def _file_evidence_at(
    parent_fd: int,
    name: str,
    *,
    absolute_path: Path,
    expected_nlink: int,
) -> tuple[dict[str, Any], bytes]:
    descriptor, identity, before = _bound_regular_identity(
        parent_fd,
        name,
        absolute_path=absolute_path,
        expected_nlink=expected_nlink,
    )
    digest = hashlib.sha256()
    content = bytearray()
    try:
        while chunk := os.read(descriptor, 1 << 20):
            digest.update(chunk)
            content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mode, before.st_nlink) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mode,
        after.st_nlink,
    ):
        raise GlobalLedgerError("shared-ledger file changed while it was inspected")
    return (
        {
            **identity,
            "size_bytes": int(after.st_size),
            "mode": stat.S_IMODE(after.st_mode),
            "nlink": int(after.st_nlink),
            "sha256": digest.hexdigest(),
        },
        bytes(content),
    )


def _directory_evidence(path: Path, observed: os.stat_result) -> dict[str, Any]:
    return {
        **_identity_from_stat(path, observed),
        "mode": stat.S_IMODE(observed.st_mode),
        "nlink": int(observed.st_nlink),
    }


def _expected_owner(storage: Mapping[str, Any]) -> int:
    return 0 if storage.get("os_enforced_trust_boundary") is True else os.geteuid()


def _expected_group(storage: Mapping[str, Any]) -> int:
    return 0 if storage.get("os_enforced_trust_boundary") is True else os.getegid()


def _validate_directory_fd(
    descriptor: int,
    *,
    identity: Mapping[str, Any],
    storage: Mapping[str, Any],
    label: str,
) -> None:
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != _expected_owner(storage)
        or observed.st_gid != _expected_group(storage)
        or observed.st_dev != storage["local_storage_device"]
        or stat.S_IMODE(observed.st_mode) != DIRECTORY_MODE
        or observed.st_dev != identity.get("st_dev")
        or observed.st_ino != identity.get("st_ino")
    ):
        raise GlobalLedgerError(f"{label} changed after authority attestation")


def _ledger_inventory(
    *,
    ledger_fd: int,
    anchors_fd: int,
    authority: Mapping[str, Any],
    storage: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], os.stat_result]]:
    names = set(os.listdir(ledger_fd))
    required = {RESERVATION_FILENAME, BURN_GUARD_FILENAME, FAILURE_RECEIPT_FILENAME}
    if names not in (required, required | {BURN_MARKER_FILENAME}):
        raise GlobalLedgerError("global run ledger inventory is incomplete or unexpected")
    ledger_path, reservation_path, guard_path, _marker_path, failure_path = _ledger_paths(authority)
    ledger_observed = os.fstat(ledger_fd)
    if (
        ledger_observed.st_uid != _expected_owner(storage)
        or ledger_observed.st_gid != _expected_group(storage)
        or ledger_observed.st_dev != storage["local_storage_device"]
        or stat.S_IMODE(ledger_observed.st_mode) != DIRECTORY_MODE
    ):
        raise GlobalLedgerError("global run ledger directory ownership/device/mode differs")
    slots: dict[str, tuple[dict[str, Any], os.stat_result]] = {}
    for filename, absolute, slot_name in (
        (RESERVATION_FILENAME, reservation_path, "reservation"),
        (BURN_GUARD_FILENAME, guard_path, "burn-guard"),
        (FAILURE_RECEIPT_FILENAME, failure_path, "failure-receipt"),
    ):
        descriptor, identity, observed = _bound_regular_identity(
            ledger_fd, filename, absolute_path=absolute, expected_nlink=2
        )
        os.close(descriptor)
        if (
            observed.st_uid != _expected_owner(storage)
            or observed.st_gid != _expected_group(storage)
            or observed.st_dev != storage["local_storage_device"]
        ):
            raise GlobalLedgerError("global run slot ownership/device differs")
        slots[slot_name] = (identity, observed)
    if BURN_MARKER_FILENAME in names:
        marker_path = ledger_path / BURN_MARKER_FILENAME
        descriptor, identity, observed = _bound_regular_identity(
            ledger_fd,
            BURN_MARKER_FILENAME,
            absolute_path=marker_path,
            expected_nlink=1,
        )
        os.close(descriptor)
        if (
            observed.st_uid != _expected_owner(storage)
            or observed.st_gid != _expected_group(storage)
            or observed.st_dev != storage["local_storage_device"]
            or observed.st_size == 0
            or stat.S_IMODE(observed.st_mode) != SEALED_FILE_MODE
        ):
            raise GlobalLedgerError("global execution marker ownership/device/mode differs")
        slots["marker"] = (identity, observed)
    prefix = f".anchor.{authority['ledger_key']}."
    anchor_names = sorted(name for name in os.listdir(anchors_fd) if name.startswith(prefix))
    if len(anchor_names) != 3:
        raise GlobalLedgerError("global run inode-anchor history is incomplete or unexpected")
    parsed: dict[str, str] = {}
    reservation_ids: set[str] = set()
    anchored_slot_names = {"reservation", "burn-guard", "failure-receipt"}
    for name in anchor_names:
        remainder = name.removeprefix(prefix)
        reservation_id, separator, suffix = remainder.partition(".")
        if (
            not separator
            or RESERVATION_ID.fullmatch(reservation_id) is None
            or suffix not in anchored_slot_names
        ):
            raise GlobalLedgerError("global run inode-anchor name is malformed")
        reservation_ids.add(reservation_id)
        parsed[suffix] = name
    if len(reservation_ids) != 1 or set(parsed) != anchored_slot_names:
        raise GlobalLedgerError("global run inode anchors do not share one reservation")
    anchor_root = Path(str(storage["anchor_directory"]["path"]))
    for slot_name, name in parsed.items():
        descriptor, identity, observed = _bound_regular_identity(
            anchors_fd,
            name,
            absolute_path=anchor_root / name,
            expected_nlink=2,
        )
        os.close(descriptor)
        if (
            observed.st_uid != _expected_owner(storage)
            or observed.st_gid != _expected_group(storage)
            or observed.st_dev != storage["local_storage_device"]
            or (identity["st_dev"], identity["st_ino"])
            != (slots[slot_name][0]["st_dev"], slots[slot_name][0]["st_ino"])
        ):
            raise GlobalLedgerError("global run inode anchor differs from its slot")
    slots["ledger"] = (_identity_from_stat(ledger_path, ledger_observed), ledger_observed)
    return slots


def _response(
    *, operation: str, authority: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema": 1,
        "api": API,
        "operation": operation,
        "policy_id": authority["policy"]["policy_id"],
        "ledger_key": authority["ledger_key"],
        "status": "complete",
        "evidence": dict(evidence),
    }


def _validate_locked_ledger_fd(
    ledger_fd: int, *, entries_fd: int, authority: Mapping[str, Any]
) -> None:
    observed = os.fstat(ledger_fd)
    try:
        named = os.stat(
            str(authority["ledger_key"]),
            dir_fd=entries_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise GlobalLedgerError("locked ledger directory was unlinked") from error
    if (
        not stat.S_ISDIR(observed.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or observed.st_nlink < 2
        or (observed.st_dev, observed.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise GlobalLedgerError("locked ledger descriptor is stale or replaced")


def _decode_document(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GlobalLedgerError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict) or content != _document_wire(value):
        raise GlobalLedgerError(f"{label} is not one canonical sealed JSON document")
    return value


def _slot_record(
    *,
    ledger_fd: int,
    filename: str,
    path: Path,
    expected_nlink: int,
    role: str | None,
    authority: Mapping[str, Any],
    execution_nonce: str | None = None,
) -> dict[str, Any]:
    evidence, content = _file_evidence_at(
        ledger_fd,
        filename,
        absolute_path=path,
        expected_nlink=expected_nlink,
    )
    if evidence["size_bytes"] == 0 and evidence["mode"] == PREPARED_SLOT_MODE:
        if role == "execution":
            raise GlobalLedgerError("execution marker cannot be a prepared empty file")
        return {"state": "prepared", "evidence": evidence, "document": None}
    if evidence["size_bytes"] <= 0 or evidence["mode"] != SEALED_FILE_MODE:
        raise GlobalLedgerError("shared-ledger slot is neither prepared nor sealed")
    document = _decode_document(content, f"sealed {role or 'ledger'} receipt")
    if role is not None:
        validate_receipt_document(
            document,
            authority=authority,
            role=role,
            execution_nonce=execution_nonce,
        )
    return {"state": "sealed", "evidence": evidence, "document": document}


def inspect_ledger(authority: Mapping[str, Any], *, storage: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect one ledger under its interprocess lock without mutating it."""

    policy = POLICIES.get(str(authority.get("policy", {}).get("policy_id", "")))
    if policy is None:
        raise GlobalLedgerError("cannot inspect an unknown ledger campaign")
    expected = build_authority(
        policy=policy,
        namespace=authority.get("global_run_namespace", {}),
        canonical_identity=authority.get("canonical_run_identity", {}),
        state_root=Path(str(authority.get("state_root", ""))),
        storage=storage,
    )
    if dict(authority) != expected:
        raise GlobalLedgerError("ledger inspection authority differs from fixed policy")
    entries_fd, anchors_fd, ledger_fd = _open_authority_directories(authority, storage)
    try:
        if ledger_fd is None:
            raise GlobalLedgerError("global run ledger does not exist")
        fcntl.flock(ledger_fd, fcntl.LOCK_EX)
        _validate_locked_ledger_fd(ledger_fd, entries_fd=entries_fd, authority=authority)
        inventory = _ledger_inventory(
            ledger_fd=ledger_fd,
            anchors_fd=anchors_fd,
            authority=authority,
            storage=storage,
        )
        ledger_path, reservation_path, guard_path, marker_path, failure_path = _ledger_paths(
            authority
        )
        reservation = _slot_record(
            ledger_fd=ledger_fd,
            filename=RESERVATION_FILENAME,
            path=reservation_path,
            expected_nlink=2,
            role="reservation",
            authority=authority,
        )
        guard_evidence, guard_content = _file_evidence_at(
            ledger_fd,
            BURN_GUARD_FILENAME,
            absolute_path=guard_path,
            expected_nlink=2,
        )
        if guard_content == b"" and guard_evidence["mode"] == PREPARED_SLOT_MODE:
            guard_state = "prepared"
        elif guard_content == b"\x01" and guard_evidence["mode"] == SEALED_FILE_MODE:
            guard_state = "consumed"
        else:
            raise GlobalLedgerError("burn guard is not exactly prepared or one-byte consumed")
        failure = _slot_record(
            ledger_fd=ledger_fd,
            filename=FAILURE_RECEIPT_FILENAME,
            path=failure_path,
            expected_nlink=2,
            role="failure",
            authority=authority,
        )
        marker: dict[str, Any] | None = None
        execution_nonce: str | None = None
        document_sha256: str | None = None
        if "marker" in inventory:
            marker_evidence, marker_content = _file_evidence_at(
                ledger_fd,
                BURN_MARKER_FILENAME,
                absolute_path=marker_path,
                expected_nlink=1,
            )
            marker_document = _decode_document(marker_content, "sealed execution marker")
            candidate_nonce = marker_document.get("execution_nonce")
            if (
                not isinstance(candidate_nonce, str)
                or EXECUTION_NONCE.fullmatch(candidate_nonce) is None
            ):
                raise GlobalLedgerError("sealed execution marker has no valid nonce")
            validate_receipt_document(
                marker_document,
                authority=authority,
                role="execution",
                execution_nonce=candidate_nonce,
            )
            marker = {
                "state": "sealed",
                "evidence": marker_evidence,
                "document": marker_document,
            }
            execution_nonce = candidate_nonce
            document_sha256 = canonical_sha256(marker_document)
        combination = (
            reservation["state"],
            guard_state,
            marker is not None,
            failure["state"],
        )
        classifications = {
            ("prepared", "prepared", False, "prepared"): "reserved_unsealed",
            ("sealed", "prepared", False, "prepared"): "prepared",
            ("sealed", "prepared", True, "prepared"): "burn_committed_guard_pending",
            ("sealed", "consumed", True, "prepared"): "burn_complete",
            ("sealed", "prepared", False, "sealed"): "failed_preburn",
            ("sealed", "prepared", True, "sealed"): "failed_postburn",
            ("sealed", "consumed", True, "sealed"): "failed_postburn",
        }
        classification = classifications.get(combination)
        if classification is None:
            raise GlobalLedgerError("global run ledger has an impossible state combination")
        return {
            "schema": 1,
            "inspection_kind": "smateway_global_run_ledger_state_v1",
            "classification": classification,
            "ledger_directory": _directory_evidence(ledger_path, os.fstat(ledger_fd)),
            "reservation": reservation,
            "burn_guard": {
                "state": guard_state,
                "evidence": guard_evidence,
                "content_hex": guard_content.hex(),
            },
            "execution_marker": marker,
            "failure_receipt": failure,
            "execution_nonce": execution_nonce,
            "execution_document_sha256": document_sha256,
        }
    finally:
        if ledger_fd is not None:
            os.close(ledger_fd)
        os.close(entries_fd)
        os.close(anchors_fd)


def validate_inspection_evidence(authority: Mapping[str, Any], value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "inspection_kind",
        "classification",
        "ledger_directory",
        "reservation",
        "burn_guard",
        "execution_marker",
        "failure_receipt",
        "execution_nonce",
        "execution_document_sha256",
    }:
        raise GlobalLedgerError("ledger inspection evidence fields differ")
    evidence = dict(value)
    if (
        evidence.get("schema") != 1
        or evidence.get("inspection_kind") != "smateway_global_run_ledger_state_v1"
    ):
        raise GlobalLedgerError("ledger inspection evidence header differs")
    ledger_path, reservation_path, guard_path, marker_path, failure_path = _ledger_paths(authority)
    directory = evidence["ledger_directory"]
    if (
        not isinstance(directory, Mapping)
        or set(directory)
        != {
            "path",
            "st_dev",
            "st_ino",
            "mode",
            "nlink",
        }
        or directory.get("path") != str(ledger_path)
        or directory.get("mode") != DIRECTORY_MODE
    ):
        raise GlobalLedgerError("ledger inspection directory evidence differs")

    def receipt_record(
        item: object, *, path: Path, nlink: int, role: str
    ) -> tuple[str, Mapping[str, Any] | None]:
        if not isinstance(item, Mapping) or set(item) != {"state", "evidence", "document"}:
            raise GlobalLedgerError("ledger inspection receipt record differs")
        state = item.get("state")
        document = item.get("document")
        if state == "prepared":
            _require_exact_file_response(
                item.get("evidence"),
                path=path,
                size_bytes=0,
                mode=PREPARED_SLOT_MODE,
                nlink=nlink,
                sha256=hashlib.sha256(b"").hexdigest(),
                label="inspection prepared slot",
            )
            if document is not None:
                raise GlobalLedgerError("prepared ledger slot carries a document")
            return state, None
        if state != "sealed" or not isinstance(document, Mapping):
            raise GlobalLedgerError("sealed ledger slot lacks a document")
        wire = _document_wire(document)
        _require_exact_file_response(
            item.get("evidence"),
            path=path,
            size_bytes=len(wire),
            mode=SEALED_FILE_MODE,
            nlink=nlink,
            sha256=hashlib.sha256(wire).hexdigest(),
            label="inspection sealed slot",
        )
        validate_receipt_document(
            document,
            authority=authority,
            role=role,
            execution_nonce=(str(evidence["execution_nonce"]) if role == "execution" else None),
        )
        return state, document

    reservation_state, _ = receipt_record(
        evidence["reservation"], path=reservation_path, nlink=2, role="reservation"
    )
    failure_state, _ = receipt_record(
        evidence["failure_receipt"], path=failure_path, nlink=2, role="failure"
    )
    guard = evidence["burn_guard"]
    if not isinstance(guard, Mapping) or set(guard) != {"state", "evidence", "content_hex"}:
        raise GlobalLedgerError("ledger inspection guard record differs")
    guard_state = guard.get("state")
    guard_content = b"" if guard_state == "prepared" else b"\x01"
    if (
        guard_state not in {"prepared", "consumed"}
        or guard.get("content_hex") != guard_content.hex()
    ):
        raise GlobalLedgerError("ledger inspection guard state differs")
    _require_exact_file_response(
        guard.get("evidence"),
        path=guard_path,
        size_bytes=len(guard_content),
        mode=PREPARED_SLOT_MODE if guard_state == "prepared" else SEALED_FILE_MODE,
        nlink=2,
        sha256=hashlib.sha256(guard_content).hexdigest(),
        label="inspection guard",
    )
    marker_present = evidence["execution_marker"] is not None
    if marker_present:
        _state, marker_document = receipt_record(
            evidence["execution_marker"], path=marker_path, nlink=1, role="execution"
        )
        nonce = evidence["execution_nonce"]
        if (
            not isinstance(nonce, str)
            or EXECUTION_NONCE.fullmatch(nonce) is None
            or marker_document is None
            or evidence["execution_document_sha256"] != canonical_sha256(marker_document)
        ):
            raise GlobalLedgerError("ledger inspection marker binding differs")
    elif (
        evidence["execution_nonce"] is not None or evidence["execution_document_sha256"] is not None
    ):
        raise GlobalLedgerError("ledger inspection absent marker has execution binding")
    combination = (reservation_state, guard_state, marker_present, failure_state)
    classifications = {
        ("prepared", "prepared", False, "prepared"): "reserved_unsealed",
        ("sealed", "prepared", False, "prepared"): "prepared",
        ("sealed", "prepared", True, "prepared"): "burn_committed_guard_pending",
        ("sealed", "consumed", True, "prepared"): "burn_complete",
        ("sealed", "prepared", False, "sealed"): "failed_preburn",
        ("sealed", "prepared", True, "sealed"): "failed_postburn",
        ("sealed", "consumed", True, "sealed"): "failed_postburn",
    }
    if evidence["classification"] != classifications.get(combination):
        raise GlobalLedgerError("ledger inspection classification differs from components")
    return evidence


def apply_request(
    operation: str,
    request: Mapping[str, Any],
    *,
    storage: Mapping[str, Any],
    _test_only_burn_fault: Any = None,
) -> dict[str, Any]:
    """Apply one already-privileged request using only fixed, validated dirfds."""

    _policy, authority, normalized = _validated_request(operation, request, storage=storage)
    entries_fd, anchors_fd, ledger_fd = _open_authority_directories(authority, storage)
    ledger_key = str(authority["ledger_key"])
    ledger_path, reservation_path, guard_path, marker_path, failure_path = _ledger_paths(authority)
    try:
        if operation == "reserve_run":
            fcntl.flock(entries_fd, fcntl.LOCK_EX)
            if ledger_fd is None:
                with suppress(FileNotFoundError):
                    ledger_fd = _open_child_directory(entries_fd, ledger_key)
            if ledger_fd is not None:
                raise GlobalLedgerError("global run key already has a ledger directory")
            reservation_id = normalized["reservation_id"]
            if (
                not isinstance(reservation_id, str)
                or RESERVATION_ID.fullmatch(reservation_id) is None
            ):
                raise GlobalLedgerError("reservation ID is malformed")
            prefix = f".anchor.{ledger_key}."
            if any(name.startswith(prefix) for name in os.listdir(anchors_fd)):
                raise GlobalLedgerError("global run key already has durable anchor history")
            os.mkdir(ledger_key, mode=DIRECTORY_MODE, dir_fd=entries_fd)
            ledger_fd = _open_child_directory(entries_fd, ledger_key)
            os.fchmod(ledger_fd, DIRECTORY_MODE)
            os.fsync(ledger_fd)
            _fsync_directory_fd(entries_fd)
            fcntl.flock(ledger_fd, fcntl.LOCK_EX)
            _validate_locked_ledger_fd(ledger_fd, entries_fd=entries_fd, authority=authority)
            slots = (
                (RESERVATION_FILENAME, reservation_path, "reservation"),
                (BURN_GUARD_FILENAME, guard_path, "burn-guard"),
                (FAILURE_RECEIPT_FILENAME, failure_path, "failure-receipt"),
            )
            result_slots: dict[str, Any] = {}
            result_anchors: dict[str, Any] = {}
            result_slot_files: dict[str, Any] = {}
            result_anchor_files: dict[str, Any] = {}
            for filename, absolute, suffix in slots:
                descriptor = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    PREPARED_SLOT_MODE,
                    dir_fd=ledger_fd,
                )
                try:
                    os.fchmod(descriptor, PREPARED_SLOT_MODE)
                    os.fsync(descriptor)
                    observed = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                anchor_name = f"{prefix}{reservation_id}.{suffix}"
                os.link(
                    filename,
                    anchor_name,
                    src_dir_fd=ledger_fd,
                    dst_dir_fd=anchors_fd,
                    follow_symlinks=False,
                )
                slot_file, _content = _file_evidence_at(
                    ledger_fd,
                    filename,
                    absolute_path=absolute,
                    expected_nlink=2,
                )
                result_slot_files[suffix] = slot_file
                result_slots[suffix] = {key: slot_file[key] for key in ("path", "st_dev", "st_ino")}
                anchor_path = Path(str(storage["anchor_directory"]["path"])) / anchor_name
                anchor_file, _content = _file_evidence_at(
                    anchors_fd,
                    anchor_name,
                    absolute_path=anchor_path,
                    expected_nlink=2,
                )
                result_anchor_files[suffix] = anchor_file
                result_anchors[suffix] = {
                    key: anchor_file[key] for key in ("path", "st_dev", "st_ino")
                }
            _fsync_directory_fd(ledger_fd)
            _fsync_directory_fd(anchors_fd)
            return _response(
                operation=operation,
                authority=authority,
                evidence={
                    "reservation_id": reservation_id,
                    "ledger_directory": _identity_from_stat(ledger_path, os.fstat(ledger_fd)),
                    "slots": result_slots,
                    "anchors": result_anchors,
                    "slot_files": result_slot_files,
                    "anchor_files": result_anchor_files,
                },
            )
        if ledger_fd is None:
            raise GlobalLedgerError("global run ledger does not exist")
        fcntl.flock(ledger_fd, fcntl.LOCK_EX)
        _validate_locked_ledger_fd(ledger_fd, entries_fd=entries_fd, authority=authority)
        inventory = _ledger_inventory(
            ledger_fd=ledger_fd,
            anchors_fd=anchors_fd,
            authority=authority,
            storage=storage,
        )
        reservation_stat = inventory["reservation"][1]
        guard_stat = inventory["burn-guard"][1]
        failure_stat = inventory["failure-receipt"][1]
        reservation_is_sealed = (
            reservation_stat.st_size > 0
            and stat.S_IMODE(reservation_stat.st_mode) == SEALED_FILE_MODE
        )
        guard_is_prepared = (
            guard_stat.st_size == 0 and stat.S_IMODE(guard_stat.st_mode) == PREPARED_SLOT_MODE
        )
        failure_is_prepared = (
            failure_stat.st_size == 0 and stat.S_IMODE(failure_stat.st_mode) == PREPARED_SLOT_MODE
        )
        if operation == "seal_slot":
            slot = normalized["slot"]
            slot_map = {
                "reservation": (RESERVATION_FILENAME, reservation_path),
                "failure": (FAILURE_RECEIPT_FILENAME, failure_path),
            }
            if slot not in slot_map:
                raise GlobalLedgerError("seal-slot request is malformed")
            marker_nonce: str | None = None
            if "marker" in inventory:
                _marker_evidence, marker_content = _file_evidence_at(
                    ledger_fd,
                    BURN_MARKER_FILENAME,
                    absolute_path=marker_path,
                    expected_nlink=1,
                )
                marker_document = _decode_document(marker_content, "sealed execution marker")
                candidate = marker_document.get("execution_nonce")
                if isinstance(candidate, str) and EXECUTION_NONCE.fullmatch(candidate):
                    marker_nonce = candidate
            validate_receipt_document(
                normalized["document"],
                authority=authority,
                role="reservation" if slot == "reservation" else "failure",
                execution_nonce=marker_nonce,
            )
            if slot == "reservation" and (
                reservation_stat.st_size != 0
                or stat.S_IMODE(reservation_stat.st_mode) != PREPARED_SLOT_MODE
                or not guard_is_prepared
                or not failure_is_prepared
                or "marker" in inventory
            ):
                raise GlobalLedgerError("reservation cannot transition from current ledger state")
            if slot == "failure" and (not reservation_is_sealed or not failure_is_prepared):
                raise GlobalLedgerError(
                    "failure receipt cannot transition from current ledger state"
                )
            filename, absolute = slot_map[str(slot)]
            descriptor = os.open(
                filename,
                os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=ledger_fd,
            )
            try:
                observed = os.fstat(descriptor)
                identity = _identity_from_stat(absolute, observed)
                if (
                    identity != normalized["expected_identity"]
                    or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 2
                    or observed.st_size != 0
                    or stat.S_IMODE(observed.st_mode) != PREPARED_SLOT_MODE
                ):
                    raise GlobalLedgerError("slot cannot transition from prepared")
                wire = _document_wire(normalized["document"])
                offset = 0
                while offset < len(wire):
                    written = os.write(descriptor, wire[offset:])
                    if written <= 0:
                        raise GlobalLedgerError("slot write was incomplete")
                    offset += written
                os.fsync(descriptor)
                os.fchmod(descriptor, SEALED_FILE_MODE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory_fd(ledger_fd)
            sealed_evidence, _content = _file_evidence_at(
                ledger_fd,
                filename,
                absolute_path=absolute,
                expected_nlink=2,
            )
            return _response(
                operation=operation,
                authority=authority,
                evidence={
                    "slot": slot,
                    "file": sealed_evidence,
                    "document_sha256": canonical_sha256(normalized["document"]),
                },
            )
        if operation != "burn_run":
            raise GlobalLedgerError("shared-ledger mutation operation is unsupported")
        nonce = normalized.get("execution_nonce")
        if not isinstance(nonce, str) or EXECUTION_NONCE.fullmatch(nonce) is None:
            raise GlobalLedgerError("execution nonce is malformed")
        validate_receipt_document(
            normalized["document"],
            authority=authority,
            role="execution",
            execution_nonce=nonce,
        )
        expected_guard = normalized.get("expected_guard_identity")
        if expected_guard != inventory["burn-guard"][0]:
            raise GlobalLedgerError("burn request guard identity differs")
        if not reservation_is_sealed or not failure_is_prepared:
            raise GlobalLedgerError("execution burn cannot transition from current ledger state")
        wire = _document_wire(normalized["document"])
        marker_present = "marker" in inventory
        if marker_present:
            marker_evidence, marker_content = _file_evidence_at(
                ledger_fd,
                BURN_MARKER_FILENAME,
                absolute_path=marker_path,
                expected_nlink=1,
            )
            if marker_content != wire:
                raise GlobalLedgerError("execution burn is already committed to another request")
            marker_document = _decode_document(marker_content, "sealed execution marker")
            if marker_document.get("execution_nonce") != nonce:
                raise GlobalLedgerError("execution burn nonce differs from committed marker")
        else:
            if not guard_is_prepared:
                raise GlobalLedgerError("execution burn cannot transition from current guard state")
            descriptor = os.open(
                BURN_MARKER_FILENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                PREPARED_SLOT_MODE,
                dir_fd=ledger_fd,
            )
            try:
                offset = 0
                while offset < len(wire):
                    written = os.write(descriptor, wire[offset:])
                    if written <= 0:
                        raise GlobalLedgerError("immutable marker write was incomplete")
                    offset += written
                os.fsync(descriptor)
                os.fchmod(descriptor, SEALED_FILE_MODE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory_fd(ledger_fd)
            marker_evidence, marker_content = _file_evidence_at(
                ledger_fd,
                BURN_MARKER_FILENAME,
                absolute_path=marker_path,
                expected_nlink=1,
            )
            if marker_content != wire:
                raise GlobalLedgerError("sealed execution marker differs after commit")
            if _test_only_burn_fault is not None:
                _test_only_burn_fault("after_marker_commit")
        guard_evidence, guard_content = _file_evidence_at(
            ledger_fd,
            BURN_GUARD_FILENAME,
            absolute_path=guard_path,
            expected_nlink=2,
        )
        if guard_content == b"":
            if guard_evidence["mode"] != PREPARED_SLOT_MODE:
                raise GlobalLedgerError("burn guard is not in the prepared mode")
            descriptor = os.open(
                BURN_GUARD_FILENAME,
                os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=ledger_fd,
            )
            try:
                observed = os.fstat(descriptor)
                if (
                    _identity_from_stat(guard_path, observed) != expected_guard
                    or observed.st_nlink != 2
                    or observed.st_size != 0
                    or stat.S_IMODE(observed.st_mode) != PREPARED_SLOT_MODE
                ):
                    raise GlobalLedgerError("burn guard cannot transition from prepared")
                if os.write(descriptor, b"\x01") != 1:
                    raise GlobalLedgerError("burn guard write was incomplete")
                os.fsync(descriptor)
                os.fchmod(descriptor, SEALED_FILE_MODE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory_fd(ledger_fd)
        elif guard_content != b"\x01" or guard_evidence["mode"] != SEALED_FILE_MODE:
            raise GlobalLedgerError("burn guard differs from exact one-byte consumed state")
        guard_evidence, guard_content = _file_evidence_at(
            ledger_fd,
            BURN_GUARD_FILENAME,
            absolute_path=guard_path,
            expected_nlink=2,
        )
        if guard_content != b"\x01" or guard_evidence["mode"] != SEALED_FILE_MODE:
            raise GlobalLedgerError("burn guard commit did not persist exactly")
        if _test_only_burn_fault is not None:
            _test_only_burn_fault("after_burn_commit")
        return _response(
            operation=operation,
            authority=authority,
            evidence={
                "state": "burn_complete",
                "execution_nonce": nonce,
                "document_sha256": canonical_sha256(normalized["document"]),
                "guard": guard_evidence,
                "marker": marker_evidence,
            },
        )
    finally:
        if ledger_fd is not None:
            os.close(ledger_fd)
        os.close(entries_fd)
        os.close(anchors_fd)


class LocalLedgerBackend:
    """Explicit offline adapter; never selected by the installed helper or runner CLI."""

    def __init__(
        self,
        *,
        storage: Mapping[str, Any],
        test_only_burn_fault: Callable[[str], None] | None = None,
    ) -> None:
        if storage.get("private_test_storage") is not True:
            raise GlobalLedgerError("local ledger backend requires explicit test storage")
        self._storage = validate_storage_document(storage, allow_private_test=True)
        self._test_only_burn_fault = test_only_burn_fault

    def storage(self) -> Mapping[str, Any]:
        return dict(self._storage)

    def mutate(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = apply_request(
            str(request.get("operation", "")),
            request,
            storage=self._storage,
            _test_only_burn_fault=self._test_only_burn_fault,
        )
        return validate_response(request, response)

    def inspect(self, authority: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_inspection_evidence(
            authority,
            inspect_ledger(authority, storage=self._storage),
        )


def provision_local_test_storage(base: Path) -> dict[str, Any]:
    """Create non-authoritative private storage for offline dependency-injected tests."""

    exact_base = base.expanduser().absolute()
    root = exact_base / "global-run-ledger-v1"
    entries = root / ENTRIES_DIRECTORY
    anchors = root / ANCHORS_DIRECTORY
    entries.mkdir(parents=True, mode=DIRECTORY_MODE)
    anchors.mkdir(mode=DIRECTORY_MODE)
    for path in (root, entries, anchors):
        path.chmod(DIRECTORY_MODE)
    return {
        "schema": 1,
        "storage_kind": "smateway_private_test_global_run_ledger_storage_v1",
        "api": API,
        "global_root": inode_identity(root, directory=True, label="test ledger root"),
        "run_ledgers_directory": inode_identity(
            entries, directory=True, label="test ledger entries"
        ),
        "anchor_directory": inode_identity(anchors, directory=True, label="test ledger anchors"),
        "global_root_seal": {"path": str(exact_base / "non-authoritative-test-seal")},
        "global_root_seal_document_sha256": "0" * 64,
        "privileged_helper": None,
        "sudo_binary": None,
        "sudoers_policy": None,
        "effective_sudo_policy_sha256": "0" * 64,
        "runner": {
            "user": "pytest",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "home": str(exact_base),
            "shell": "/usr/sbin/nologin",
            "password_locked": True,
            "supplementary_groups": [],
        },
        "policy_registry_sha256": canonical_sha256(policies_document()),
        "mutation_command_prefix": None,
        "local_storage_device": int(root.stat().st_dev),
        "os_enforced_trust_boundary": False,
        "private_test_storage": True,
    }


def _helper_storage(operation: str) -> dict[str, Any]:
    if Path(__file__).absolute() != HELPER_PATH or Path(__file__).is_symlink():
        raise GlobalLedgerError("privileged helper is not executing from its fixed installed path")
    if os.geteuid() != 0:
        raise GlobalLedgerError("privileged helper is not running as root")
    storage = attest_fixed_storage(require_runner_identity=False)
    runner = storage["runner"]
    if (
        os.environ.get("SUDO_UID") != str(runner["uid"])
        or os.environ.get("SUDO_USER") != str(runner["user"])
        or os.environ.get("SUDO_COMMAND") != f"{HELPER_PATH} {operation}"
    ):
        raise GlobalLedgerError("privileged helper lacks the sealed sudo caller identity")
    return storage


def helper_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 1 or arguments[0] not in OPERATIONS:
            raise GlobalLedgerError("helper requires exactly one fixed operation argument")
        if sys.stdin.isatty():
            raise GlobalLedgerError("helper request must arrive on standard input")
        wire = sys.stdin.buffer.read(1 << 20)
        if not wire or len(wire) >= 1 << 20:
            raise GlobalLedgerError("helper request size is invalid")
        request = json.loads(wire)
        if not isinstance(request, Mapping):
            raise GlobalLedgerError("helper request must be one JSON object")
        operation = arguments[0]
        storage = _helper_storage(operation)
        if operation == "attest":
            query = request.get("query")
            if query == "storage":
                if dict(request) != attestation_request(query="storage"):
                    raise GlobalLedgerError("storage attestation request differs")
                result = {
                    "schema": 1,
                    "api": API,
                    "operation": "attest",
                    "query": "storage",
                    "status": "complete",
                    "evidence": storage,
                }
            elif query == "ledger_state":
                authority = request.get("authority")
                if not isinstance(authority, Mapping) or dict(request) != attestation_request(
                    query="ledger_state", authority=authority
                ):
                    raise GlobalLedgerError("ledger-state attestation request differs")
                result = {
                    "schema": 1,
                    "api": API,
                    "operation": "attest",
                    "query": "ledger_state",
                    "policy_id": authority["policy"]["policy_id"],
                    "ledger_key": authority["ledger_key"],
                    "authority_sha256": canonical_sha256(authority),
                    "status": "complete",
                    "evidence": inspect_ledger(authority, storage=storage),
                }
            else:
                raise GlobalLedgerError("unknown read-only attestation query")
            validate_attestation_response(request, result)
        else:
            result = apply_request(operation, request, storage=storage)
        sys.stdout.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
        return 0
    except (GlobalLedgerError, OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(helper_main())
