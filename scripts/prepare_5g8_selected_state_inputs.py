#!/usr/bin/env python3
"""Produce source-bound T8 device, intervention-plan, and intervention-seal inputs.

The device action is read-only: it resolves one exact USB context, opens only a
libiio context description, reads bounded sysfs attributes, and writes a sealed
observation.  Intervention planning and sealing are hardware-inert.  They
require X runner plans/results that were pre-bound to the contract before RF.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO = Path("/usr/local/lib")
_LOCAL_RPI_REFERENCE = Path("/home/pi")
_NONLOCAL_OUTPUT_ROOTS = (Path("/media"), Path("/mnt"), Path("/run/media"))
_LOADER_DIRECTORIES = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)

if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SOURCE) not in sys.path
    or not _LOADER_DIRECTORIES
    or _LOADER_DIRECTORIES[0] != _REQUIRED_LIBIIO
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned qualification Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(_SOURCE), environment.get("PYTHONPATH", "")) if item
    )
    libraries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join((str(_REQUIRED_LIBIIO), *libraries))
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from smateway.native_iio_attestation import attestation_sha256, attest_runtime
from smateway.selected_state_qualification import (
    DEVICE_IDENTITY_KIND,
    INTERVENTION_KIND,
    INTERVENTION_PLAN_KIND,
    X_RUN_BINDING_KIND,
    SelectedStateQualificationError,
    canonical_sha256,
    full_simultaneous_fixture_binding_from_manifest,
    reject_replace_placeholders,
    sha256_path,
    validate_device_identity_evidence,
    validate_intervention_change_plan,
    validate_intervention_contract,
)

from scripts import run_5g8_leakage_ladder as leakage_runner

IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
X_RUN_ROLES = (
    "boundary_baseline",
    "boundary_intervention",
    "full_fixture_baseline",
    "full_fixture_intervention",
)
X_BOUNDARY_ROLES = ("boundary_baseline", "boundary_intervention")
X_FULL_FIXTURE_ROLES = ("full_fixture_baseline", "full_fixture_intervention")
X_BOUNDARY_STAGES = (
    "direct_rx2_termination",
    "rx2_cable_terminated",
    "powered_selector_all_inputs_terminated",
)
X_FULL_FIXTURE_STAGE = "full_conducted_fixture"
X_PREBINDING_KIND = "5g8_x_intervention_prebinding_v1"
X_MANIFEST_KIND = "5g8_x_intervention_capture_v1"


class SelectedStateInputError(RuntimeError):
    """A T8 input cannot be safely produced from the supplied evidence."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 192
        or any(character not in IDENTIFIER_CHARS for character in value)
    ):
        raise SelectedStateInputError(f"{label} is not a safe identifier")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SelectedStateInputError(f"{label} is not a lowercase SHA-256")
    return value


def _assert_no_symlink_chain(path: Path, label: str, *, allow_missing: bool = False) -> None:
    exact = path.expanduser().absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise SelectedStateInputError(f"{label} does not exist: {current}") from None
        if current.is_symlink():
            raise SelectedStateInputError(f"{label} path contains a symlink: {current}")


def _assert_local_rpi_path(path: Path, label: str) -> None:
    """Reject removable/network-style roots and a filesystem unlike local RPi storage."""

    exact = path.expanduser().absolute()
    if any(exact == root or root in exact.parents for root in _NONLOCAL_OUTPUT_ROOTS):
        raise SelectedStateInputError(f"{label} is not on local Raspberry Pi storage")
    probe = exact
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise SelectedStateInputError(f"cannot resolve an existing parent for {label}")
        probe = parent
    try:
        if os.stat(probe).st_dev != os.stat(_LOCAL_RPI_REFERENCE).st_dev:
            raise SelectedStateInputError(f"{label} is not on the Raspberry Pi local filesystem")
    except OSError as error:
        raise SelectedStateInputError(
            f"cannot attest local storage for {label}: {error}"
        ) from error


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if not exact.is_file():
        raise SelectedStateInputError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelectedStateInputError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise SelectedStateInputError(f"{label} must contain one JSON object")
    return value


def _file(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, label)
    if not exact.is_file():
        raise SelectedStateInputError(f"{label} must be a regular non-symlink file")
    return {"path": str(exact), "sha256": sha256_path(exact), "size_bytes": exact.stat().st_size}


def _write_new(path: Path, document: Mapping[str, Any]) -> Path:
    exact = path.expanduser().absolute()
    _assert_no_symlink_chain(exact, "output", allow_missing=True)
    _assert_local_rpi_path(exact, "output")
    if exact.exists() or exact.is_symlink():
        raise SelectedStateInputError(f"output already exists: {exact}")
    exact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(exact.parent, "output parent")
    _assert_local_rpi_path(exact.parent, "output parent")
    payload = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor = os.open(
        exact,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            exact.unlink()
        raise
    return exact


def _pointer_value(document: object, pointer: str) -> object:
    if not pointer.startswith("/"):
        raise SelectedStateInputError("property path must be a JSON pointer")
    current = document
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise SelectedStateInputError("property path does not resolve in fixture manifest")
        current = current[part]
    return current


def _sysfs_attributes(path: Path) -> dict[str, str]:
    exact = path.expanduser().absolute()
    if not exact.is_dir():
        raise SelectedStateInputError("resolved Pluto sysfs directory is unavailable")
    values: dict[str, str] = {"path": str(exact)}
    for name in ("serial", "idVendor", "idProduct", "manufacturer", "product"):
        try:
            value = (exact / name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise SelectedStateInputError(f"cannot read Pluto sysfs {name}: {error}") from error
        if not value:
            raise SelectedStateInputError(f"Pluto sysfs {name} is empty")
        values[name] = value
    return values


def _live_iio_context_facts(uri: str) -> dict[str, Any]:
    """Open a context description only; do not open buffers or write radio attributes."""

    try:
        import iio  # type: ignore[import-untyped]
        from pluto_plus.hardware.iio import context_facts

        context = iio.Context(uri)
        observed = context_facts(context)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        raise SelectedStateInputError(f"cannot inspect read-only IIO context: {error}") from error
    raw_channels = observed.get("rx_scan_channels")
    channels = (
        list(raw_channels)
        if isinstance(raw_channels, Sequence) and not isinstance(raw_channels, (str, bytes))
        else []
    )
    return {
        "serial": observed.get("serial"),
        "model": observed.get("model"),
        "firmware_version": observed.get("firmware_version"),
        "kernel_version": observed.get("kernel_version"),
        "context_uri": uri,
        "phy_model": observed.get("phy_model"),
        "buffer_metadata_abi": observed.get("buffer_metadata_abi"),
        "rx_scan_channels": channels,
    }


def produce_device_identity(
    *,
    serial: str,
    uri: str,
    output: Path,
    identity_boundary: Callable[[str, str], dict[str, Any]] = (
        leakage_runner._live_identity_boundary
    ),
    context_boundary: Callable[[str], dict[str, Any]] = _live_iio_context_facts,
    native_boundary: Callable[[], Mapping[str, Any]] = attest_runtime,
    now: Callable[[], str] = _now,
) -> Path:
    """Create one fresh, read-only, recomputably accepted Pluto identity file."""

    exact_serial = _identifier(serial, "Pluto serial")
    if not uri.startswith("usb:"):
        raise SelectedStateInputError("device observation requires an explicit USB URI")
    resolution = identity_boundary(exact_serial, uri)
    sysfs_path = resolution.get("sysfs_path") if isinstance(resolution, Mapping) else None
    if not isinstance(sysfs_path, str) or not sysfs_path:
        raise SelectedStateInputError("USB resolution did not return one exact sysfs path")
    facts = context_boundary(uri)
    native = dict(native_boundary())
    native_sha = attestation_sha256(native)
    observed_at = now()
    observation = {
        "observed_at": observed_at,
        "serial": exact_serial,
        "usb_uri": uri,
        "read_only_usb_resolution": resolution,
        "iio_context_facts": facts,
        "sysfs_attributes": _sysfs_attributes(Path(sysfs_path)),
        "native_libiio_runtime_attestation": native,
        "native_libiio_runtime_attestation_sha256": native_sha,
    }
    document = {
        "schema": 2,
        "evidence_kind": DEVICE_IDENTITY_KIND,
        **observation,
        "observation_sha256": canonical_sha256(observation),
        "accepted": True,
    }
    try:
        validate_device_identity_evidence(document)
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    return _write_new(output, document)


def _x_roles_for_stage(stage: str) -> tuple[str, ...]:
    if stage in X_BOUNDARY_STAGES:
        return X_RUN_ROLES
    if stage == X_FULL_FIXTURE_STAGE:
        return X_FULL_FIXTURE_ROLES
    raise SelectedStateInputError("X implicated boundary stage is unsupported")


def _prebound_x_plan(path: Path, *, contract_id: str, role: str) -> tuple[str, dict[str, Any], str]:
    document = _read_json(path, f"X {role} immutable plan")
    try:
        reject_replace_placeholders(document, f"X {role} immutable plan")
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    contract = document.get("plan_contract")
    if not isinstance(contract, Mapping):
        raise SelectedStateInputError(f"X {role} plan has no plan contract")
    try:
        leakage_runner._validate_plan_envelope(document, expected_contract=contract)
    except (ValueError, RuntimeError) as error:
        raise SelectedStateInputError(f"X {role} immutable plan is invalid: {error}") from error
    binding = contract.get("x_intervention_prebinding")
    if not isinstance(binding, Mapping) or set(binding) != {
        "schema",
        "binding_kind",
        "contract_id",
        "run_role",
        "installed_fixture_revision_sha256",
    }:
        raise SelectedStateInputError(
            f"X {role} plan lacks the mandatory x_intervention_prebinding contract"
        )
    if (
        binding.get("schema") != 1
        or binding.get("binding_kind") != X_PREBINDING_KIND
        or binding.get("contract_id") != contract_id
        or binding.get("run_role") != role
    ):
        raise SelectedStateInputError(f"X {role} plan prebinding differs from this contract")
    _sha256(binding.get("installed_fixture_revision_sha256"), "installed fixture revision")
    context = contract.get("x_intervention_capture_context")
    if not isinstance(context, Mapping) or set(context) != {
        "schema",
        "binding_kind",
        "implicated_boundary_stage",
        "acquisition_index",
        "freshness_epoch_id",
        "capture_state_fixture",
        "installed_after_fixture",
        "selector_flash_evidence",
    }:
        raise SelectedStateInputError(
            f"X {role} plan lacks the mandatory x_intervention_capture_context contract"
        )
    stage = context.get("implicated_boundary_stage")
    if (
        context.get("schema") != 1
        or context.get("binding_kind") != leakage_runner.X_CAPTURE_CONTEXT_KIND
        or not isinstance(stage, str)
    ):
        raise SelectedStateInputError(f"X {role} capture context is malformed")
    return (
        _identifier(contract.get("run_id"), f"X {role} run ID"),
        _file(path, f"X {role} plan"),
        stage,
    )


def produce_intervention_plan(
    *,
    contract_id: str,
    campaign_id: str,
    board_id: str,
    before_fixture_manifest: Path,
    after_fixture_manifest: Path,
    component_id: str,
    property_path: str,
    restore_instruction: str,
    x_plan_paths: Mapping[str, Path],
    output: Path,
    now: Callable[[], str] = _now,
) -> Path:
    """Freeze the one-leaf change and every future X run before any X capture."""

    exact_contract = _identifier(contract_id, "intervention contract ID")
    before_raw = _read_json(before_fixture_manifest, "before fixture manifest")
    after_raw = _read_json(after_fixture_manifest, "after fixture manifest")
    try:
        reject_replace_placeholders(
            {
                "contract_id": contract_id,
                "campaign_id": campaign_id,
                "board_id": board_id,
                "component_id": component_id,
                "property_path": property_path,
                "restore_instruction": restore_instruction,
                "before_fixture_manifest": before_raw,
                "after_fixture_manifest": after_raw,
            },
            "intervention-plan inputs",
        )
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    before = full_simultaneous_fixture_binding_from_manifest(before_fixture_manifest)
    after = full_simultaneous_fixture_binding_from_manifest(after_fixture_manifest)
    expected_after_revision = str(after["fixture_revision_sha256"])
    x_plans: dict[str, Any] = {}
    if not x_plan_paths or not set(x_plan_paths).issubset(X_RUN_ROLES):
        raise SelectedStateInputError("intervention plan has unsupported or missing X run plans")
    implicated_stages: set[str] = set()
    for role in X_RUN_ROLES:
        if role not in x_plan_paths:
            continue
        run_id, plan_file, implicated_stage = _prebound_x_plan(
            x_plan_paths[role], contract_id=exact_contract, role=role
        )
        implicated_stages.add(implicated_stage)
        plan_document = _read_json(Path(plan_file["path"]), f"X {role} plan")
        binding = plan_document["plan_contract"]["x_intervention_prebinding"]
        if binding["installed_fixture_revision_sha256"] != expected_after_revision:
            raise SelectedStateInputError(f"X {role} prebinding names the wrong installed revision")
        x_plans[role] = {"run_id": run_id, "plan_file": plan_file}
    if len(implicated_stages) != 1:
        raise SelectedStateInputError("all X plans must name one implicated boundary stage")
    implicated_stage = next(iter(implicated_stages))
    expected_roles = _x_roles_for_stage(implicated_stage)
    if set(x_plan_paths) != set(expected_roles):
        raise SelectedStateInputError(
            "X plan roles differ from the implicated-boundary execution branch"
        )
    document = {
        "schema": 2,
        "plan_kind": INTERVENTION_PLAN_KIND,
        "contract_id": exact_contract,
        "campaign_id": _identifier(campaign_id, "campaign ID"),
        "board_id": _identifier(board_id, "board ID"),
        "created_at": now(),
        "before_fixture": before,
        "installed_after_fixture": after,
        "change": {
            "component_id": _identifier(component_id, "changed component ID"),
            "property_path": property_path,
            "before": _pointer_value(before_raw, property_path),
            "after": _pointer_value(after_raw, property_path),
            "reversible": True,
            "restore_instruction": restore_instruction,
        },
        "implicated_boundary_stage": implicated_stage,
        "x_run_plans": x_plans,
        "diagnostic_restoration_policy": (
            "restoration_is_diagnostic_only_and_requires_source_bound_reapplication_before_q"
        ),
    }
    try:
        validate_intervention_change_plan(document)
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    return _write_new(output, document)


def _accepted_x_manifest(
    path: Path,
    *,
    role: str,
    contract_id: str,
    change_plan_sha256: str,
    expected_plan: Mapping[str, Any],
) -> dict[str, Any]:
    document = _read_json(path, f"X {role} accepted manifest")
    try:
        reject_replace_placeholders(document, f"X {role} accepted manifest")
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    expected_fields = {
        "schema",
        "run_kind",
        "contract_id",
        "run_role",
        "run_id",
        "status",
        "captured_at",
        "acquisition_index",
        "freshness_epoch_id",
        "intervention_state_fixture_revision_sha256",
        "topology_stage",
        "topology_fixture_sha256",
        "source_commit",
        "dependency_commit",
        "selector_evidence_sha256",
        "immutable_plan_file",
        "captures",
        "measurement_quality_rejection_reasons",
        "final_mute_verified",
        "final_selector_safe_state",
    }
    if set(document) != expected_fields:
        raise SelectedStateInputError(f"X {role} manifest fields are incomplete or unexpected")
    if (
        document.get("schema") != 1
        or document.get("run_kind") != X_MANIFEST_KIND
        or document.get("contract_id") != contract_id
        or document.get("run_role") != role
        or document.get("run_id") != expected_plan.get("run_id")
        or document.get("status") != "accepted"
        or document.get("measurement_quality_rejection_reasons") != []
        or document.get("final_mute_verified") is not True
    ):
        raise SelectedStateInputError(f"X {role} manifest was not accepted fail-closed")
    plan_file = document.get("immutable_plan_file")
    if not isinstance(plan_file, Mapping) or dict(plan_file) != expected_plan.get("plan_file"):
        raise SelectedStateInputError(f"X {role} manifest does not bind its predeclared plan")
    plan_document = _read_json(Path(str(plan_file["path"])), f"X {role} immutable plan")
    plan_contract = plan_document["plan_contract"]
    context = plan_contract["x_intervention_capture_context"]
    capture_fixture = context["capture_state_fixture"]
    selector_flash = context["selector_flash_evidence"]
    if (
        document.get("acquisition_index") != context["acquisition_index"]
        or document.get("freshness_epoch_id") != context["freshness_epoch_id"]
        or document.get("intervention_state_fixture_revision_sha256")
        != capture_fixture["fixture_revision_sha256"]
        or document.get("topology_stage") != plan_contract.get("topology_stage")
        or document.get("topology_fixture_sha256") != plan_contract.get("fixture_evidence_sha256")
        or document.get("selector_evidence_sha256") != selector_flash.get("sha256")
    ):
        raise SelectedStateInputError(f"X {role} manifest differs from its capture context")
    topology_stage = str(document["topology_stage"])
    safe_state = document.get("final_selector_safe_state")
    if topology_stage in X_BOUNDARY_STAGES[:2]:
        expected_safe_state: dict[str, Any] = {
            "status": "physical_disconnect_verified",
            "topology_stage": topology_stage,
            "selector_rf_state": "rf_disconnected",
            "selector_power_state": "bench_power_off",
            "selector_control_harness_state": "disconnected",
        }
    else:
        expected_safe_state = {
            "status": "mailbox_all_off_verified",
            "topology_stage": topology_stage,
            "mailbox_all_off_verified": True,
        }
    if safe_state != expected_safe_state:
        raise SelectedStateInputError(f"X {role} final selector safe state is not truthful")
    captures = document.get("captures")
    if not isinstance(captures, list) or not captures:
        raise SelectedStateInputError(f"X {role} manifest has no accepted captures")
    streams: list[str] = []
    raw_files: list[dict[str, Any]] = []
    for index, capture in enumerate(captures, start=1):
        if not isinstance(capture, Mapping) or set(capture) != {
            "stream_id",
            "raw_iq_file",
            "metadata_file",
            "condition_record_file",
            "abi2_continuity_verified",
            "measurement_quality_passed",
        }:
            raise SelectedStateInputError(f"X {role} capture {index} is malformed")
        if (
            capture.get("abi2_continuity_verified") is not True
            or capture.get("measurement_quality_passed") is not True
        ):
            raise SelectedStateInputError(f"X {role} capture {index} was not accepted")
        streams.append(_identifier(capture.get("stream_id"), f"X {role} stream ID"))
        for name in ("raw_iq_file", "metadata_file", "condition_record_file"):
            binding = capture.get(name)
            if not isinstance(binding, Mapping):
                raise SelectedStateInputError(f"X {role} capture lacks {name}")
            actual = _file(Path(str(binding.get("path", ""))), f"X {role} {name}")
            if actual != dict(binding):
                raise SelectedStateInputError(f"X {role} {name} bytes differ from manifest")
            if name == "raw_iq_file":
                raw_files.append(actual)
    if len(streams) != len(set(streams)) or len({item["sha256"] for item in raw_files}) != len(
        raw_files
    ):
        raise SelectedStateInputError(f"X {role} reuses stream or raw IQ identity")
    return {
        "schema": 1,
        "binding_kind": X_RUN_BINDING_KIND,
        "contract_id": contract_id,
        "change_plan_sha256": change_plan_sha256,
        "run_role": role,
        "run_id": document["run_id"],
        "captured_at": document["captured_at"],
        "acquisition_index": document["acquisition_index"],
        "freshness_epoch_id": document["freshness_epoch_id"],
        "intervention_state_fixture_revision_sha256": document[
            "intervention_state_fixture_revision_sha256"
        ],
        "topology_stage": document["topology_stage"],
        "topology_fixture_sha256": document["topology_fixture_sha256"],
        "source_commit": document["source_commit"],
        "dependency_commit": document["dependency_commit"],
        "selector_evidence_sha256": document["selector_evidence_sha256"],
        "plan_file": dict(plan_file),
        "manifest_file": _file(path, f"X {role} manifest"),
        "stream_ids": streams,
        "raw_iq_files": raw_files,
        "acceptance_revalidated": True,
    }


def produce_intervention_seal(
    *,
    change_plan_path: Path,
    x_manifest_paths: Mapping[str, Path],
    installation_attestation_path: Path,
    support_result_path: Path,
    output: Path,
    restoration_evidence_path: Path | None = None,
    reapplication_evidence_path: Path | None = None,
    now: Callable[[], str] = _now,
) -> Path:
    """Seal admitted X sources and the explicitly installed supported after-state."""

    plan = _read_json(change_plan_path, "intervention change plan")
    try:
        validated = validate_intervention_change_plan(plan)
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    plan_file = _file(change_plan_path, "intervention change plan")
    expected_roles = validated.expected_x_roles
    if set(x_manifest_paths) != set(expected_roles):
        raise SelectedStateInputError(
            "intervention seal manifests differ from the implicated-boundary execution branch"
        )
    x_runs = {
        role: _accepted_x_manifest(
            x_manifest_paths[role],
            role=role,
            contract_id=validated.contract_id,
            change_plan_sha256=plan_file["sha256"],
            expected_plan=plan["x_run_plans"][role],
        )
        for role in expected_roles
    }
    restoration_supplied = restoration_evidence_path is not None
    if restoration_supplied != (reapplication_evidence_path is not None):
        raise SelectedStateInputError(
            "diagnostic restoration and after-state reapplication evidence are an inseparable pair"
        )
    if validated.implicated_boundary_stage in X_BOUNDARY_STAGES and not restoration_supplied:
        raise SelectedStateInputError(
            "four-role X sealing requires after-to-before restoration and before-to-after "
            "reapplication evidence"
        )
    document = {
        "schema": 2,
        "contract_kind": INTERVENTION_KIND,
        "contract_id": validated.contract_id,
        "sealed_at": now(),
        "change_plan_file": plan_file,
        "change_plan_sha256": plan_file["sha256"],
        "x_runs": x_runs,
        "diagnostic_restoration": {
            "status": "restored_then_reapplied" if restoration_supplied else "not_performed",
            "restoration_evidence_file": (
                _file(restoration_evidence_path, "restoration evidence")
                if restoration_evidence_path is not None
                else None
            ),
            "reapplication_evidence_file": (
                _file(reapplication_evidence_path, "reapplication evidence")
                if reapplication_evidence_path is not None
                else None
            ),
        },
        "adoption": {
            "decision": "adopt_supported_fix",
            "installed_state": "after",
            "installed_fixture_revision_sha256": (
                validated.installed_after_fixture_revision_sha256
            ),
            "installation_attestation_file": _file(
                installation_attestation_path, "installation attestation"
            ),
        },
        "support_evidence_file": _file(support_result_path, "support result"),
    }
    try:
        validate_intervention_contract(document)
    except SelectedStateQualificationError as error:
        raise SelectedStateInputError(str(error)) from error
    return _write_new(output, document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    device = commands.add_parser("device-identity")
    device.add_argument("--serial", required=True)
    device.add_argument("--uri", required=True)
    device.add_argument("--output", type=Path, required=True)

    plan = commands.add_parser("intervention-plan")
    plan.add_argument("--contract-id", required=True)
    plan.add_argument("--campaign-id", required=True)
    plan.add_argument("--board-id", required=True)
    plan.add_argument("--before-fixture-manifest", type=Path, required=True)
    plan.add_argument("--after-fixture-manifest", type=Path, required=True)
    plan.add_argument("--component-id", required=True)
    plan.add_argument("--property-path", required=True)
    plan.add_argument("--restore-instruction", required=True)
    for role in X_RUN_ROLES:
        plan.add_argument(f"--{role.replace('_', '-')}-plan", type=Path)
    plan.add_argument("--output", type=Path, required=True)

    seal = commands.add_parser("intervention-seal")
    seal.add_argument("--change-plan", type=Path, required=True)
    for role in X_RUN_ROLES:
        seal.add_argument(f"--{role.replace('_', '-')}-manifest", type=Path)
    seal.add_argument("--installation-attestation", type=Path, required=True)
    seal.add_argument("--support-result", type=Path, required=True)
    seal.add_argument("--restoration-evidence", type=Path)
    seal.add_argument("--reapplication-evidence", type=Path)
    seal.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "device-identity":
            output = produce_device_identity(serial=args.serial, uri=args.uri, output=args.output)
        elif args.command == "intervention-plan":
            output = produce_intervention_plan(
                contract_id=args.contract_id,
                campaign_id=args.campaign_id,
                board_id=args.board_id,
                before_fixture_manifest=args.before_fixture_manifest,
                after_fixture_manifest=args.after_fixture_manifest,
                component_id=args.component_id,
                property_path=args.property_path,
                restore_instruction=args.restore_instruction,
                x_plan_paths={
                    role: path
                    for role in X_RUN_ROLES
                    if (path := getattr(args, f"{role}_plan")) is not None
                },
                output=args.output,
            )
        else:
            output = produce_intervention_seal(
                change_plan_path=args.change_plan,
                x_manifest_paths={
                    role: path
                    for role in X_RUN_ROLES
                    if (path := getattr(args, f"{role}_manifest")) is not None
                },
                installation_attestation_path=args.installation_attestation,
                support_result_path=args.support_result,
                restoration_evidence_path=args.restoration_evidence,
                reapplication_evidence_path=args.reapplication_evidence,
                output=args.output,
            )
        print(output)
        return 0
    except (OSError, SelectedStateInputError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
