#!/usr/bin/env python3
"""Verify and analyze one complete immutable T7 frequency-sweep campaign."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pluto_plus.artifacts import data_path, load_metadata, verify_artifact
from pluto_plus.models import ArtifactSummary, GainMode, RadioSettings

from smateway import global_ledger
from smateway.fine_frequency import (
    BANDWIDTH_HZ,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TOTAL_SAMPLES,
    FineFrequencyError,
    analyze_sweep,
    campaign_cross_binding_from_plan_contract,
    canonical_json_sha256,
    coherent_measurement_document,
    normalized_observation_from_evidence,
    select_coarse_refinements,
    validate_live_condition_evidence,
    validate_plan_envelope,
)
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import audit_continuity_metadata, load_ci16_channel, sha256_path
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import attest_runtime, validate_runtime_attestation
from smateway.ota_analysis import estimate_coherent_pilot_offset

_REPOSITORY = Path(__file__).resolve().parents[1]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))
import scripts.run_5g8_fine_frequency as runner
import scripts.run_5g8_leakage_ladder as leakage_runner

PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_RESULTS_FILENAME = "fine-frequency-results.json"
EXECUTION_TOMBSTONE_FILENAME = "execution-started.tombstone.json"
FAILURE_TOMBSTONE_FILENAME = "failed-run.tombstone.json"
CONDITION_RECORD_FILENAME = "5g8-fine-frequency-condition.json"
COMPLETE_ATTEMPT_KEYS = frozenset(
    {
        "started_at",
        "status",
        "confirmations",
        "execution_tombstone",
        "external_run_burn",
        "external_run_burn_sha256",
        "completed_condition_count",
        "campaign_preflight_exact_mute",
        "campaign_preflight_exact_mute_sha256",
        "selector_connected_preflight",
        "selector_connected_preflight_sha256",
        "campaign_final_cleanup",
        "campaign_final_cleanup_sha256",
        "error",
        "completed_at",
    }
)
FAILURE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "marker_kind",
        "run_id",
        "board_id",
        "failed_at",
        "failure_phase",
        "plan_path",
        "plan_contract_sha256",
        "global_ledger_authority",
        "shared_global_ledger_authority",
        "execution_nonce",
        "reservation_receipt",
        "run_consumption_receipt",
        "original_error",
        "failure_cleanup_evidence",
        "cleanup_errors",
        "persistence_attempts",
        "persistence_errors",
        "campaign_accepted",
        "automatic_retry_forbidden",
        "partial_artifacts_are_forensic_only",
    }
)
BURN_ACQUISITION_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "evidence_kind",
        "burn_classification",
        "authoritative_state",
        "authoritative_inspection",
        "authoritative_inspection_sha256",
        "global_ledger_authority",
        "global_ledger_authority_sha256",
        "run_state_ledger",
        "reservation",
        "live_access_began",
        "live_cleanup_call_count",
        "automatic_retry_forbidden",
    }
)
NO_LIVE_CLEANUP_KEYS = frozenset(
    {
        "schema",
        "evidence_kind",
        "burn_classification",
        "authoritative_ledger_state",
        "authoritative_inspection_sha256",
        "exact_pluto_mute",
        "selector_image_admission",
        "selector_all_off",
        "live_cleanup_call_count",
        "live_cleanup_prohibited",
        "cleanup_validation_passed",
    }
)
CAMPAIGN_CLEANUP_KEYS = frozenset(
    {
        "schema",
        "evidence_kind",
        "exact_pluto_mute",
        "exact_pluto_mute_validation_passed",
        "selector_image_admission",
        "selector_image_admission_validation_passed",
        "selector_all_off",
        "selector_all_off_purpose",
        "selector_all_off_validation_passed",
        "selector_write_permitted_by_image_admission",
        "no_unauthorized_selector_write",
        "cleanup_validation_passed",
    }
)
UNVALIDATED_CAMPAIGN_CLEANUP_KEYS = frozenset(
    {
        "schema",
        "evidence_kind",
        "exact_pluto_mute",
        "selector_image_admission",
        "selector_all_off",
        "cleanup_validation_passed",
    }
)
COMPLETE_RESULT_KEYS = frozenset(
    {
        "plan_index",
        "condition_id",
        "evidence",
        "evidence_sha256",
        "normalized_observation",
        "boundary_result",
        "campaign_acceptance_pending",
        "campaign_accepted",
    }
)


class FineFrequencyAnalysisError(RuntimeError):
    """The persisted campaign cannot support a T7 analysis."""


def _is_exact_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise FineFrequencyAnalysisError(f"{label} is not an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FineFrequencyAnalysisError(f"{label} is not an exact UTC timestamp") from error
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.isoformat() != value
    ):
        raise FineFrequencyAnalysisError(f"{label} is not an exact UTC timestamp")
    return parsed


def _validate_error_document(value: object, label: str) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"type", "message"}
        or not isinstance(value.get("type"), str)
        or not value.get("type")
        or not isinstance(value.get("message"), str)
    ):
        raise FineFrequencyAnalysisError(f"{label} error document is malformed")
    return {"type": str(value["type"]), "message": str(value["message"])}


def _validate_operation_records(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise FineFrequencyAnalysisError(f"{label} is not a list")
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"operation", "error"}:
            raise FineFrequencyAnalysisError(f"{label} entry fields differ")
        if not isinstance(item.get("operation"), str) or not item.get("operation"):
            raise FineFrequencyAnalysisError(f"{label} operation is malformed")
        error = item.get("error")
        if error is not None:
            _validate_error_document(error, label)


def _validate_persistence_attempts(value: object) -> None:
    if not isinstance(value, list):
        raise FineFrequencyAnalysisError("failure persistence attempts are not a list")
    for item in value:
        if not isinstance(item, Mapping) or item.get("status") not in {"passed", "failed"}:
            raise FineFrequencyAnalysisError("failure persistence attempt is malformed")
        expected = {"operation", "status"}
        if item.get("status") == "failed":
            expected.add("error")
        if (
            set(item) != expected
            or not isinstance(item.get("operation"), str)
            or not item.get("operation")
        ):
            raise FineFrequencyAnalysisError("failure persistence attempt fields differ")
        if "error" in item:
            _validate_error_document(item["error"], "failure persistence attempt")


def _validate_failure_burn_receipt(
    value: object,
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    ledger_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FineFrequencyAnalysisError("failure run-consumption receipt is malformed")
    burn = dict(value)
    authority = ledger_binding["global_ledger_authority"]
    observed_reservation = runner._validate_reservation(
        contract=contract,
        plan_path=plan_path,
        manifest_path=plan_path.parent / MANIFEST_FILENAME,
        ledger_binding=ledger_binding,
        prepared_manifest_sha256=None,
        prepared_manifest_size_bytes=None,
    )
    if burn.get("evidence_kind") == "5g8_fine_frequency_burn_acquisition_emergency_v2":
        inspection = burn.get("authoritative_inspection")
        classification = burn.get("burn_classification")
        state = burn.get("authoritative_state")
        expected_classification = {
            "prepared": "pristine",
            "burn_committed_guard_pending": "partial",
            "burn_complete": "full",
        }.get(state)
        if (
            set(burn) != BURN_ACQUISITION_RECEIPT_KEYS
            or burn.get("schema") != 2
            or classification != expected_classification
            or not isinstance(inspection, Mapping)
            or inspection.get("classification") != state
            or burn.get("authoritative_inspection_sha256") != canonical_json_sha256(inspection)
            or burn.get("global_ledger_authority") != authority
            or burn.get("global_ledger_authority_sha256") != canonical_json_sha256(authority)
            or burn.get("run_state_ledger") != ledger_binding
            or burn.get("reservation") != observed_reservation
            or burn.get("live_access_began") is not False
            or burn.get("live_cleanup_call_count") != 0
            or burn.get("automatic_retry_forbidden") is not True
        ):
            raise FineFrequencyAnalysisError(
                "burn-acquisition emergency receipt fields or binding differ"
            )
        try:
            global_ledger.validate_inspection_evidence(authority, inspection)
        except (global_ledger.GlobalLedgerError, OSError, TypeError, ValueError) as error:
            raise FineFrequencyAnalysisError(
                f"burn-acquisition inspection evidence failed: {error}"
            ) from error
        return burn

    expected_keys = {
        "schema",
        "evidence_kind",
        "global_ledger_authority",
        "global_ledger_authority_sha256",
        "run_state_ledger",
        "reservation",
        "burn_guard",
        "burn_marker",
        "burn_completed_before_source_dependency_fixture_or_hardware_access",
    }
    guard = burn.get("burn_guard")
    marker = burn.get("burn_marker")
    if (
        set(burn) != expected_keys
        or burn.get("schema") != 3
        or burn.get("evidence_kind") != "5g8_fine_frequency_global_run_burn_v3"
        or burn.get("global_ledger_authority") != authority
        or burn.get("global_ledger_authority_sha256") != canonical_json_sha256(authority)
        or burn.get("run_state_ledger") != ledger_binding
        or burn.get("reservation") != observed_reservation
        or not isinstance(guard, Mapping)
        or not isinstance(marker, Mapping)
        or burn.get("burn_completed_before_source_dependency_fixture_or_hardware_access")
        is not True
    ):
        raise FineFrequencyAnalysisError("completed burn receipt fields or binding differ")
    guard_path = Path(str(ledger_binding["burn_guard"]["path"]))
    observed_guard = runner._file_evidence(
        guard_path,
        "consumed T7 failure-receipt burn guard",
        expected_nlink=2,
    )
    if (
        dict(guard) != observed_guard
        or observed_guard["size_bytes"] != 1
        or guard_path.stat().st_mode & 0o222
    ):
        raise FineFrequencyAnalysisError("completed burn guard evidence differs")
    marker_path = Path(str(ledger_binding["burn_marker_path"]))
    marker_document = marker.get("document")
    if not isinstance(marker_document, Mapping):
        raise FineFrequencyAnalysisError("completed burn marker document is malformed")
    observed_marker = {
        **runner._file_evidence(
            marker_path,
            "T7 failure-receipt execution burn marker",
        ),
        "document": runner._read_json(
            marker_path,
            "T7 failure-receipt execution burn marker",
        ),
    }
    expected_marker_document = runner._burn_marker_document(
        contract=contract,
        plan_path=plan_path,
        manifest_path=plan_path.parent / MANIFEST_FILENAME,
        ledger_binding=ledger_binding,
        reservation=observed_reservation,
        burn_guard=observed_guard,
        execution_nonce=marker_document.get("execution_nonce"),
        consumed_at=marker_document.get("consumed_at"),
    )
    _exact_utc_timestamp(marker_document.get("consumed_at"), "burn marker consumed_at")
    if (
        dict(marker) != observed_marker
        or dict(marker_document) != expected_marker_document
        or marker_path.stat().st_mode & 0o222
    ):
        raise FineFrequencyAnalysisError("completed burn marker evidence differs")
    return burn


def _validate_failure_cleanup(
    value: object,
    *,
    contract: Mapping[str, Any],
    phase: str,
    burn: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FineFrequencyAnalysisError("failure cleanup evidence is malformed")
    cleanup = dict(value)
    if phase == "external_burn_acquisition":
        if set(cleanup) != NO_LIVE_CLEANUP_KEYS:
            raise FineFrequencyAnalysisError("burn-acquisition cleanup fields differ")
        try:
            expected = runner._validate_burn_acquisition_no_live_cleanup(
                cleanup,
                burn_receipt=burn,
            )
        except (runner.FineFrequencyRunError, TypeError, ValueError) as error:
            raise FineFrequencyAnalysisError(
                f"burn-acquisition no-live cleanup failed: {error}"
            ) from error
        if cleanup != expected:
            raise FineFrequencyAnalysisError("burn-acquisition no-live cleanup differs")
        return cleanup
    fixture = contract.get("fixture_identity")
    selector_control = (
        fixture.get("selector_control")
        if isinstance(fixture, Mapping) and fixture.get("selector_connected") is True
        else None
    )
    if cleanup.get("evidence_kind") == "5g8_fine_frequency_campaign_cleanup_v1":
        if set(cleanup) != CAMPAIGN_CLEANUP_KEYS:
            raise FineFrequencyAnalysisError("campaign failure cleanup fields differ")
        expected = runner._validated_campaign_cleanup(
            exact_mute=cleanup.get("exact_pluto_mute"),
            serial=str(contract["device_identity"]["serial"]),
            exact_mute_purpose="campaign_failure",
            selector_image_admission=cleanup.get("selector_image_admission"),
            selector_all_off=cleanup.get("selector_all_off"),
            selector_control=selector_control,
            selector_purpose="exception_cleanup_all_off",
        )
        if cleanup != expected:
            raise FineFrequencyAnalysisError("campaign failure cleanup evidence differs")
        return cleanup
    if (
        set(cleanup) != UNVALIDATED_CAMPAIGN_CLEANUP_KEYS
        or cleanup.get("schema") != 1
        or cleanup.get("evidence_kind") != "5g8_fine_frequency_unvalidated_campaign_cleanup_v1"
        or cleanup.get("cleanup_validation_passed") is not False
    ):
        raise FineFrequencyAnalysisError("unvalidated campaign cleanup evidence differs")
    return cleanup


def _validate_external_failure_receipt_document(
    value: object,
    *,
    contract: Mapping[str, Any],
    plan_path: Path,
    ledger_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FAILURE_RECEIPT_KEYS:
        raise FineFrequencyAnalysisError("v2 external failure receipt fields differ")
    document = dict(value)
    failed_at = _exact_utc_timestamp(document.get("failed_at"), "failure receipt failed_at")
    original_error = document.get("original_error")
    authority = ledger_binding.get("global_ledger_authority")
    if (
        document.get("schema") != 2
        or document.get("marker_kind") != "5g8_fine_frequency_external_failure_receipt_v2"
        or document.get("run_id") != contract.get("run_id")
        or document.get("board_id") != contract.get("board_id")
        or not isinstance(document.get("failure_phase"), str)
        or not document.get("failure_phase")
        or document.get("plan_path") != str(plan_path)
        or document.get("plan_contract_sha256") != canonical_json_sha256(contract)
        or document.get("global_ledger_authority") != authority
        or not isinstance(authority, Mapping)
        or document.get("shared_global_ledger_authority")
        != global_ledger.authority_receipt_binding(authority)
        or not isinstance(original_error, Mapping)
        or set(original_error) != {"phase", "error"}
        or original_error.get("phase") != document.get("failure_phase")
        or document.get("campaign_accepted") is not False
        or document.get("automatic_retry_forbidden") is not True
        or document.get("partial_artifacts_are_forensic_only") is not True
    ):
        raise FineFrequencyAnalysisError("v2 external failure receipt binding differs")
    try:
        burn = _validate_failure_burn_receipt(
            document.get("run_consumption_receipt"),
            contract=contract,
            plan_path=plan_path,
            ledger_binding=ledger_binding,
        )
    except (runner.FineFrequencyRunError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(
            f"v2 external failure burn receipt failed: {error}"
        ) from error
    reservation = burn["reservation"]
    reservation_document = reservation.get("document")
    if not isinstance(reservation_document, Mapping):
        raise FineFrequencyAnalysisError("failure reservation document is malformed")
    reserved_at = _exact_utc_timestamp(
        reservation_document.get("reserved_at"),
        "failure reservation reserved_at",
    )
    marker = burn.get("burn_marker")
    marker_document = marker.get("document") if isinstance(marker, Mapping) else None
    consumed_at = (
        _exact_utc_timestamp(marker_document.get("consumed_at"), "failure burn consumed_at")
        if isinstance(marker_document, Mapping)
        else None
    )
    if (
        document.get("reservation_receipt") != reservation
        or document.get("execution_nonce")
        != (
            burn["authoritative_inspection"].get("execution_nonce")
            if burn.get("evidence_kind") == "5g8_fine_frequency_burn_acquisition_emergency_v2"
            else marker_document.get("execution_nonce")
        )
        or reserved_at > failed_at
        or (consumed_at is not None and consumed_at > failed_at)
    ):
        raise FineFrequencyAnalysisError("failure receipt timestamp/reservation binding differs")
    _validate_failure_cleanup(
        document.get("failure_cleanup_evidence"),
        contract=contract,
        phase=str(document["failure_phase"]),
        burn=burn,
    )
    _validate_error_document(original_error.get("error"), "failure receipt original")
    _validate_operation_records(document.get("cleanup_errors"), "failure cleanup errors")
    _validate_persistence_attempts(document.get("persistence_attempts"))
    _validate_operation_records(
        document.get("persistence_errors"),
        "failure persistence errors",
    )
    return document


def _read_json(path: Path, label: str) -> dict[str, Any]:
    exact = path.expanduser().absolute()
    _assert_path_chain_no_symlink(exact, label)
    if exact.is_symlink() or not exact.is_file():
        raise FineFrequencyAnalysisError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FineFrequencyAnalysisError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FineFrequencyAnalysisError(f"{label} must contain one JSON object")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    _assert_path_chain_no_symlink(path.parent, "analysis output parent")
    try:
        runner._assert_local_rpi_storage(path.parent)
    except Exception as error:
        raise FineFrequencyAnalysisError(
            f"analysis output is not Raspberry Pi local storage: {error}"
        ) from error
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_path_chain_no_symlink(path.parent, "analysis output parent")
    wire = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(wire)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    path.chmod(0o400)
    _fsync_directory(path.parent)


def _assert_path_chain_no_symlink(path: Path, label: str) -> None:
    runner._assert_no_symlink_chain(path, label)


def _assert_tree_no_symlink(path: Path, label: str) -> None:
    _assert_path_chain_no_symlink(path, label)
    if path.is_symlink() or not path.is_dir():
        raise FineFrequencyAnalysisError(f"{label} must be a regular non-symlink directory")
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *files):
            candidate = root_path / name
            if candidate.is_symlink():
                raise FineFrequencyAnalysisError(f"{label} contains a symlink: {candidate}")


def _verified_file(
    value: Mapping[str, Any],
    *,
    path_key: str,
    size_key: str,
    hash_key: str,
    label: str,
) -> Path:
    path = Path(str(value.get(path_key, ""))).expanduser().absolute()
    _assert_path_chain_no_symlink(path, label)
    if path.is_symlink() or not path.is_file():
        raise FineFrequencyAnalysisError(f"{label} is missing or a symlink")
    if path.stat().st_size != value.get(size_key) or sha256_path(path) != value.get(hash_key):
        raise FineFrequencyAnalysisError(f"{label} bytes differ from immutable evidence")
    return path


def _verify_analyzer_source(contract: Mapping[str, Any]) -> None:
    try:
        observed = runner._repository_source_identity()
    except Exception as error:
        raise FineFrequencyAnalysisError(
            f"cannot re-attest source dependency closure: {error}"
        ) from error
    if observed != contract.get("source_identity"):
        raise FineFrequencyAnalysisError("current source/dependency closure differs from the plan")


def _verify_global_ledger_authority(
    contract: Mapping[str, Any],
    *,
    plan_path: Path,
) -> None:
    try:
        runner._validate_global_ledger_authority(contract, plan_path=plan_path)
    except (runner.FineFrequencyRunError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(
            f"shared global run-ledger authority failed: {error}"
        ) from error


def _reject_external_failure_receipt(
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    plan_path: Path,
) -> None:
    try:
        binding = runner._validate_run_ledger_binding(
            contract,
            run_root=plan_path.parent,
            value=manifest.get("run_state_ledger"),
        )
    except (runner.FineFrequencyRunError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(
            f"shared global run-ledger binding failed: {error}"
        ) from error
    receipt_path = Path(str(binding["failure_receipt_slot"]["path"]))
    _assert_path_chain_no_symlink(receipt_path, "external failure receipt")
    if receipt_path.stat().st_size == 0:
        return
    receipt = _read_json(receipt_path, "external failure receipt")
    _validate_external_failure_receipt_document(
        receipt,
        contract=contract,
        plan_path=plan_path,
        ledger_binding=binding,
    )
    raise FineFrequencyAnalysisError("valid external failure receipt forbids analysis")


def _verify_native_identity(contract: Mapping[str, Any]) -> None:
    try:
        observed = validate_runtime_attestation(attest_runtime())
    except Exception as error:
        raise FineFrequencyAnalysisError(
            f"cannot re-attest native libiio runtime: {error}"
        ) from error
    if observed != contract.get("native_identity"):
        raise FineFrequencyAnalysisError("current native libiio runtime differs from the plan")


def _verify_fixture_and_storage(contract: Mapping[str, Any], run_root: Path) -> None:
    try:
        runner._assert_local_rpi_storage(run_root)
        runner._verify_fixture_identity(contract.get("fixture_identity"))
        coarse = contract.get("coarse_results_binding")
        if coarse is not None:
            runner._verify_file_identity(coarse, "coarse results")
    except Exception as error:
        raise FineFrequencyAnalysisError(
            f"fixture/local-storage binding differs from immutable plan: {error}"
        ) from error


def _verify_campaign_safety_evidence(
    contract: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> None:
    device = contract.get("device_identity")
    fixture = contract.get("fixture_identity")
    if not isinstance(device, Mapping) or not isinstance(fixture, Mapping):
        raise FineFrequencyAnalysisError("campaign safety identity is malformed")
    serial = str(device.get("serial"))
    preflight_mute = attempt.get("campaign_preflight_exact_mute")
    if (
        not isinstance(preflight_mute, Mapping)
        or attempt.get("campaign_preflight_exact_mute_sha256")
        != canonical_json_sha256(preflight_mute)
        or not leakage_runner._mute_passed(
            preflight_mute,
            serial=serial,
            purpose="campaign_preflight",
        )
    ):
        raise FineFrequencyAnalysisError("campaign preflight exact Pluto mute evidence is invalid")

    selector_connected = fixture.get("selector_connected") is True
    selector_control = fixture.get("selector_control")
    target_image: object = None
    if selector_connected:
        if not isinstance(selector_control, Mapping):
            raise FineFrequencyAnalysisError(
                "selector-connected campaign lacks frozen selector control"
            )
        selector_preflight = attempt.get("selector_connected_preflight")
        expected_order = [
            "exact_pluto_mute",
            "target_full_bin_uid_admission",
            "first_mailbox_operation",
        ]
        expected_keys = {
            "exact_pluto_mute",
            "target_full_bin_uid_admission",
            "first_mailbox_operation",
            "required_order",
            "observed_order",
            "passed",
        }
        if (
            not isinstance(selector_preflight, Mapping)
            or set(selector_preflight) != expected_keys
            or selector_preflight.get("exact_pluto_mute") != preflight_mute
            or selector_preflight.get("required_order") != expected_order
            or selector_preflight.get("observed_order") != expected_order
            or selector_preflight.get("passed") is not True
            or attempt.get("selector_connected_preflight_sha256")
            != canonical_json_sha256(selector_preflight)
        ):
            raise FineFrequencyAnalysisError(
                "selector campaign mute/image/mailbox order evidence is invalid"
            )
        target_image = selector_preflight.get("target_full_bin_uid_admission")
        first_mailbox = selector_preflight.get("first_mailbox_operation")
        try:
            image_passed = leakage_runner._selector_image_admission_passed(
                target_image,
                selector_control=selector_control,
            )
            mailbox_passed = leakage_runner._selector_passed(
                first_mailbox,
                selector_control=selector_control,
                purpose="initial_state_before_command",
            )
        except (TypeError, ValueError):
            image_passed = False
            mailbox_passed = False
        if not image_passed or not mailbox_passed:
            raise FineFrequencyAnalysisError(
                "selector campaign BIN/UID or first mailbox evidence is invalid"
            )
    elif (
        selector_control is not None
        or attempt.get("selector_connected_preflight") is not None
        or attempt.get("selector_connected_preflight_sha256") is not None
    ):
        raise FineFrequencyAnalysisError(
            "selector-disconnected campaign contains selector preflight evidence"
        )

    final_cleanup = attempt.get("campaign_final_cleanup")
    if (
        not isinstance(final_cleanup, Mapping)
        or attempt.get("campaign_final_cleanup_sha256") != canonical_json_sha256(final_cleanup)
        or final_cleanup.get("selector_image_admission") != target_image
    ):
        raise FineFrequencyAnalysisError("campaign final cleanup binding is invalid")
    expected_cleanup = runner._validated_campaign_cleanup(
        exact_mute=final_cleanup.get("exact_pluto_mute"),
        serial=serial,
        exact_mute_purpose="campaign_final",
        selector_image_admission=target_image,
        selector_all_off=final_cleanup.get("selector_all_off"),
        selector_control=selector_control,
        selector_purpose="final_cleanup_all_off",
    )
    if (
        final_cleanup != expected_cleanup
        or expected_cleanup.get("cleanup_validation_passed") is not True
    ):
        raise FineFrequencyAnalysisError(
            "campaign final exact mute/selector ALL_OFF evidence is invalid"
        )


def _validate_complete_manifest_schema(
    manifest: object,
    *,
    contract: Mapping[str, Any],
    envelope: Mapping[str, Any],
    plan_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(manifest, Mapping) or set(manifest) != runner.PREPARED_MANIFEST_KEYS:
        raise FineFrequencyAnalysisError("complete manifest fields are missing or unexpected")
    document = dict(manifest)
    attempts = document.get("attempts")
    results = document.get("condition_results")
    expected_count = int(contract["storage"]["condition_count"])
    if (
        document.get("schema") != 1
        or document.get("manifest_kind") != runner.RUN_KIND
        or document.get("run_id") != contract["run_id"]
        or document.get("status") != "complete"
        or document.get("plan_path") != str(plan_path)
        or document.get("plan_sha256") != sha256_path(plan_path)
        or document.get("plan_contract_sha256") != envelope["plan_contract_sha256"]
        or not _is_exact_int(document.get("accepted_condition_count"))
        or document.get("accepted_condition_count") != expected_count
        or document.get("campaign_accepted") is not True
        or document.get("error") is not None
        or not isinstance(attempts, list)
        or len(attempts) != 1
        or not isinstance(results, list)
        or len(results) != expected_count
        or any(
            not isinstance(item, Mapping) or set(item) != COMPLETE_RESULT_KEYS for item in results
        )
    ):
        raise FineFrequencyAnalysisError("campaign is not complete and atomically accepted")
    attempt_value = attempts[0]
    if not isinstance(attempt_value, Mapping) or set(attempt_value) != COMPLETE_ATTEMPT_KEYS:
        raise FineFrequencyAnalysisError("complete attempt fields are missing or unexpected")
    attempt = dict(attempt_value)
    created_at = _exact_utc_timestamp(document.get("created_at"), "manifest created_at")
    updated_at = _exact_utc_timestamp(document.get("updated_at"), "manifest updated_at")
    started_at = _exact_utc_timestamp(attempt.get("started_at"), "attempt started_at")
    completed_at = _exact_utc_timestamp(attempt.get("completed_at"), "attempt completed_at")
    external_burn = attempt.get("external_run_burn")
    preflight_mute = attempt.get("campaign_preflight_exact_mute")
    final_cleanup = attempt.get("campaign_final_cleanup")
    selector_preflight = attempt.get("selector_connected_preflight")
    fixture = contract.get("fixture_identity")
    selector_connected = isinstance(fixture, Mapping) and fixture.get("selector_connected") is True
    if (
        not created_at <= started_at <= completed_at <= updated_at
        or attempt.get("status") != "complete"
        or attempt.get("error") is not None
        or not _is_exact_int(attempt.get("completed_condition_count"))
        or attempt.get("completed_condition_count") != expected_count
        or not isinstance(attempt.get("confirmations"), Mapping)
        or not isinstance(attempt.get("execution_tombstone"), Mapping)
        or not isinstance(external_burn, Mapping)
        or attempt.get("external_run_burn_sha256") != canonical_json_sha256(external_burn)
        or not isinstance(preflight_mute, Mapping)
        or attempt.get("campaign_preflight_exact_mute_sha256")
        != canonical_json_sha256(preflight_mute)
        or not isinstance(final_cleanup, Mapping)
        or attempt.get("campaign_final_cleanup_sha256") != canonical_json_sha256(final_cleanup)
        or (selector_connected and not isinstance(selector_preflight, Mapping))
        or (
            selector_connected
            and attempt.get("selector_connected_preflight_sha256")
            != canonical_json_sha256(selector_preflight)
        )
        or (not selector_connected and selector_preflight is not None)
        or (
            not selector_connected
            and attempt.get("selector_connected_preflight_sha256") is not None
        )
    ):
        raise FineFrequencyAnalysisError("complete attempt schema or hash binding is invalid")
    for result in results:
        assert isinstance(result, Mapping)
        if (
            not _is_exact_int(result.get("plan_index"))
            or not isinstance(result.get("condition_id"), str)
            or not result.get("condition_id")
            or not isinstance(result.get("evidence"), Mapping)
            or not isinstance(result.get("evidence_sha256"), str)
            or not isinstance(result.get("normalized_observation"), Mapping)
            or not isinstance(result.get("boundary_result"), Mapping)
            or result.get("campaign_acceptance_pending") is not False
            or result.get("campaign_accepted") is not True
        ):
            raise FineFrequencyAnalysisError("complete condition-result schema is invalid")
    return attempt, [dict(item) for item in results]


def _verify_prepared_manifest_reconstruction(
    *,
    contract: Mapping[str, Any],
    envelope: Mapping[str, Any],
    plan_path: Path,
    manifest: Mapping[str, Any],
    external_burn: Mapping[str, Any],
) -> None:
    ledger_binding = manifest.get("run_state_ledger")
    created_at = manifest.get("created_at")
    if not isinstance(ledger_binding, Mapping) or not isinstance(created_at, str):
        raise FineFrequencyAnalysisError("prepared-manifest reconstruction inputs are malformed")
    prepared = runner._prepared_manifest_document(
        plan_path,
        envelope,
        ledger_binding=ledger_binding,
        created_at=created_at,
    )
    wire = runner._manifest_wire_bytes(prepared)
    expected_sha256 = hashlib.sha256(wire).hexdigest()
    expected_size = len(wire)
    reservation = external_burn.get("reservation")
    marker = external_burn.get("burn_marker")
    if not isinstance(reservation, Mapping) or not isinstance(marker, Mapping):
        raise FineFrequencyAnalysisError("external burn lacks reservation/marker evidence")
    reservation_document = reservation.get("document")
    marker_document = marker.get("document")
    if not isinstance(reservation_document, Mapping) or not isinstance(marker_document, Mapping):
        raise FineFrequencyAnalysisError("external burn documents are malformed")
    reserved_at = _exact_utc_timestamp(
        reservation_document.get("reserved_at"),
        "reservation reserved_at",
    )
    consumed_at = _exact_utc_timestamp(
        marker_document.get("consumed_at"),
        "burn marker consumed_at",
    )
    created_timestamp = _exact_utc_timestamp(created_at, "prepared manifest created_at")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1 or not isinstance(attempts[0], Mapping):
        raise FineFrequencyAnalysisError("prepared reconstruction lacks one complete attempt")
    started_at = _exact_utc_timestamp(
        attempts[0].get("started_at"),
        "prepared reconstruction attempt started_at",
    )
    if (
        reservation_document.get("schema") != 3
        or reservation_document.get("marker_kind")
        != "5g8_fine_frequency_global_run_id_reservation_v3"
        or marker_document.get("schema") != 3
        or marker_document.get("marker_kind") != "5g8_fine_frequency_global_execution_consumed_v3"
        or not created_timestamp <= reserved_at <= consumed_at <= started_at
        or reservation_document.get("prepared_manifest_sha256") != expected_sha256
        or reservation_document.get("prepared_manifest_size_bytes") != expected_size
        or marker_document.get("prepared_manifest_sha256") != expected_sha256
        or marker_document.get("prepared_manifest_size_bytes") != expected_size
    ):
        raise FineFrequencyAnalysisError(
            "reconstructed prepared manifest bytes differ from v3 reservation/burn"
        )


def _verify_execution_tombstone(
    run_root: Path,
    *,
    plan_path: Path,
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    failure_path = run_root / FAILURE_TOMBSTONE_FILENAME
    if (
        failure_path.exists()
        or failure_path.is_symlink()
        or manifest.get("failure_tombstone") is not None
    ):
        raise FineFrequencyAnalysisError("failed campaign tombstone forbids analysis")
    execution_path = run_root / EXECUTION_TOMBSTONE_FILENAME
    execution = _read_json(execution_path, "execution tombstone")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise FineFrequencyAnalysisError("complete campaign must contain exactly one attempt")
    attempt = attempts[0]
    if not isinstance(attempt, Mapping):
        raise FineFrequencyAnalysisError("complete campaign attempt is malformed")
    external_burn = attempt.get("external_run_burn")
    try:
        validated_external_burn = runner._validate_consumed_run_ledger(
            contract=contract,
            plan_path=plan_path,
            manifest_path=run_root / MANIFEST_FILENAME,
            ledger_binding=manifest.get("run_state_ledger"),
            burn_evidence=external_burn,
        )
    except (runner.FineFrequencyRunError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(
            f"external run reservation/burn evidence failed: {error}"
        ) from error
    expected = {
        "schema": 1,
        "marker_kind": "5g8_fine_frequency_execution_started",
        "run_id": contract["run_id"],
        "created_at": execution.get("created_at"),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": canonical_json_sha256(contract),
        "run_state_ledger_sha256": canonical_json_sha256(
            validated_external_burn["run_state_ledger"]
        ),
        "external_run_burn_sha256": canonical_json_sha256(validated_external_burn),
        "external_burn_marker_sha256": validated_external_burn["burn_marker"]["sha256"],
        "run_id_burned": True,
        "resume_or_splice_forbidden": True,
    }
    execution_created_at = _exact_utc_timestamp(
        execution.get("created_at"),
        "execution tombstone created_at",
    )
    if execution != expected:
        raise FineFrequencyAnalysisError("execution tombstone differs from immutable plan")
    tombstone_binding = attempt.get("execution_tombstone")
    confirmations = attempt.get("confirmations")
    fixture = contract.get("fixture_identity")
    if not isinstance(fixture, Mapping):
        raise FineFrequencyAnalysisError("complete attempt fixture identity is malformed")
    stage = str(fixture.get("topology_stage"))
    connected = fixture.get("selector_connected") is True
    stage_fields = {f"stage_{name}": name == stage for name in runner.TOPOLOGIES}
    confirmation_fields = {
        "confirmed_at",
        "topology_stage",
        "topology_token",
        "selector_static_all_off",
        "experimental_policy_reviewed",
        "no_antennas",
        "tx2_terminated_muted",
        "rx1_protected_reference",
        "no_movement",
        *stage_fields,
    }
    attempt_started_at = _exact_utc_timestamp(
        attempt.get("started_at"),
        "execution-bound attempt started_at",
    )
    _exact_utc_timestamp(
        attempt.get("completed_at"),
        "execution-bound attempt completed_at",
    )
    if execution_created_at > attempt_started_at:
        raise FineFrequencyAnalysisError("execution tombstone timestamp follows attempt start")
    if (
        attempt.get("status") != "complete"
        or attempt.get("error") is not None
        or attempt.get("completed_condition_count") != contract["storage"]["condition_count"]
        or attempt.get("external_run_burn_sha256") != canonical_json_sha256(validated_external_burn)
        or not isinstance(confirmations, Mapping)
        or set(confirmations) != confirmation_fields
        or confirmations.get("topology_stage") != stage
        or confirmations.get("topology_token") != fixture.get("topology_token")
        or confirmations.get("selector_static_all_off") is not connected
        or any(
            confirmations.get(name) is not True
            for name in (
                "experimental_policy_reviewed",
                "no_antennas",
                "tx2_terminated_muted",
                "rx1_protected_reference",
                "no_movement",
            )
        )
        or any(confirmations.get(name) is not expected for name, expected in stage_fields.items())
        or not isinstance(tombstone_binding, Mapping)
        or tombstone_binding.get("path") != str(execution_path)
        or tombstone_binding.get("sha256") != sha256_path(execution_path)
        or tombstone_binding.get("document") != execution
    ):
        raise FineFrequencyAnalysisError("complete attempt/execution tombstone binding is invalid")
    _exact_utc_timestamp(confirmations.get("confirmed_at"), "execution confirmation confirmed_at")
    _verify_campaign_safety_evidence(contract, attempt)
    return validated_external_burn


def _reanalyze_condition(
    contract: Mapping[str, Any],
    condition: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    capture_root: Path,
    prior_stream_ids: set[int],
    prior_artifact_sha256s: set[str],
) -> dict[str, Any]:
    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise FineFrequencyAnalysisError("manifest condition evidence is malformed")
    if canonical_json_sha256(evidence) != result.get("evidence_sha256"):
        raise FineFrequencyAnalysisError("manifest condition evidence hash differs")
    try:
        admitted = validate_live_condition_evidence(
            contract,
            evidence,
            prior_stream_ids=prior_stream_ids,
            prior_artifact_sha256s=prior_artifact_sha256s,
        )
    except (FineFrequencyError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(f"condition evidence admission failed: {error}") from error
    artifact_evidence = admitted["artifact"]
    assert isinstance(artifact_evidence, Mapping)
    artifact_root = Path(str(artifact_evidence["path"])).expanduser().absolute()
    data_file = _verified_file(
        artifact_evidence,
        path_key="data_path",
        size_key="data_size_bytes",
        hash_key="data_sha256",
        label="condition raw CI16 artifact",
    )
    metadata_file = _verified_file(
        artifact_evidence,
        path_key="metadata_path",
        size_key="metadata_size_bytes",
        hash_key="metadata_sha256",
        label="condition SigMF metadata",
    )
    record_file = _verified_file(
        artifact_evidence,
        path_key="condition_record_path",
        size_key="condition_record_size_bytes",
        hash_key="condition_record_sha256",
        label="condition immutable record",
    )
    _assert_tree_no_symlink(artifact_root, "condition artifact tree")
    expected_condition_root = capture_root / str(condition["condition_id"])
    expected_artifact_root = expected_condition_root / str(artifact_evidence["artifact_id"])
    if (
        artifact_root != expected_artifact_root
        or data_file.parent != artifact_root
        or metadata_file.parent != artifact_root
        or record_file.parent != artifact_root
        or data_file.name != f"{artifact_evidence['artifact_id']}.sigmf-data"
        or metadata_file.name != f"{artifact_evidence['artifact_id']}.sigmf-meta"
        or record_file.name != CONDITION_RECORD_FILENAME
    ):
        raise FineFrequencyAnalysisError("condition artifact layout escaped its planned root")
    boundary = result.get("boundary_result")
    if not isinstance(boundary, Mapping) or set(boundary) != {"artifact", "condition_record"}:
        raise FineFrequencyAnalysisError("condition boundary result is incomplete or unexpected")
    artifact_document = boundary.get("artifact")
    if not isinstance(artifact_document, Mapping):
        raise FineFrequencyAnalysisError("condition ArtifactSummary is missing")
    try:
        artifact = ArtifactSummary.model_validate(artifact_document)
    except (TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError("condition ArtifactSummary is malformed") from error
    if (
        artifact.artifact_id != artifact_evidence["artifact_id"]
        or Path(artifact.path).expanduser().absolute() != artifact_root
        or data_path(artifact).absolute() != data_file
        or artifact.sha256 != artifact_evidence["data_sha256"]
        or artifact.sample_count != TOTAL_SAMPLES
        or artifact.receiver_count != 2
        or artifact.sample_rate_hz != SAMPLE_RATE_HZ
        or artifact.center_frequency_hz != condition["frequency_hz"]
        or not verify_artifact(artifact)
    ):
        raise FineFrequencyAnalysisError("condition raw ArtifactSummary verification failed")
    record = _read_json(record_file, "condition immutable record")
    record_keys = {
        "schema",
        "record_kind",
        "run_id",
        "board_id",
        "plan_contract_sha256",
        "condition",
        "device",
        "rf_readback",
        "capture",
        "artifact",
        "artifact_evidence_without_condition_record",
        "analysis",
        "safety",
        "selector_static_all_off",
        "standalone_record_is_not_campaign_acceptance",
    }
    base_artifact_evidence = {
        key: value
        for key, value in artifact_evidence.items()
        if not key.startswith("condition_record_")
    }
    if (
        set(record) != record_keys
        or record.get("schema") != 1
        or record.get("record_kind") != "5g8_fine_frequency_raw_condition_v1"
        or record.get("run_id") != contract["run_id"]
        or record.get("board_id") != contract["board_id"]
        or record.get("plan_contract_sha256") != canonical_json_sha256(contract)
        or record.get("condition") != dict(condition)
        or record.get("artifact") != dict(artifact_document)
        or record.get("artifact_evidence_without_condition_record") != base_artifact_evidence
        or record.get("device") != admitted["device"]
        or record.get("rf_readback") != admitted["rf_readback"]
        or record.get("capture") != admitted["capture"]
        or record.get("analysis") != admitted["analysis"]
        or record.get("safety") != admitted["safety"]
        or record.get("selector_static_all_off") != admitted["selector_static_all_off"]
        or record.get("standalone_record_is_not_campaign_acceptance") is not True
        or boundary.get("condition_record") != record
    ):
        raise FineFrequencyAnalysisError("condition record differs from raw/evidence bindings")
    try:
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=TOTAL_SAMPLES,
            expected_samples_per_block=SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        )
    except (OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(f"condition SigMF/ABI2 audit failed: {error}") from error
    global_metadata = metadata.get("global")
    capture_metadata = metadata.get("pluto:capture")
    captures = metadata.get("captures")
    expected_settings = RadioSettings(
        center_frequency_hz=int(condition["frequency_hz"]),
        sample_rate_hz=SAMPLE_RATE_HZ,
        bandwidth_hz=BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=RECEIVER_GAIN_DB,
        channels=(0, 1),
    ).model_dump(mode="json")
    if (
        not isinstance(global_metadata, Mapping)
        or global_metadata.get("core:datatype") != "ci16_le"
        or global_metadata.get("core:sample_rate") != SAMPLE_RATE_HZ
        or global_metadata.get("core:num_channels") != 2
        or global_metadata.get("pluto:artifact_id") != artifact.artifact_id
        or global_metadata.get("pluto:sha256") != artifact.sha256
        or global_metadata.get("pluto:radio") != admitted["device"]["radio_identity"]
        or not isinstance(capture_metadata, Mapping)
        or capture_metadata.get("sample_count") != TOTAL_SAMPLES
        or capture_metadata.get("receiver_count") != 2
        or capture_metadata.get("initial_settings") != expected_settings
        or not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
        or captures[0].get("sample_start") != 0
        or captures[0].get("configuration_revision") != 1
        or captures[0].get("settings") != expected_settings
        or metadata.get("pluto:continuity") != admitted["capture"]["live_ledger"]
        or continuity != admitted["capture"]["persisted_continuity"]
    ):
        raise FineFrequencyAnalysisError("condition SigMF identity/settings differ from evidence")
    try:
        rx1 = load_ci16_channel(
            data_file,
            sample_count=TOTAL_SAMPLES,
            receiver_count=2,
            channel=0,
        )
        rx2 = load_ci16_channel(
            data_file,
            sample_count=TOTAL_SAMPLES,
            receiver_count=2,
            channel=1,
        )
        monitor = AdcHeadroomMonitor(receiver_count=2)
        monitor.observe(np.stack((rx1, rx2), axis=0))
        headroom = monitor.result()
        clipped = sum(item.clipped_sample_count for item in headroom.receivers)
        frequencies = admitted["rf_readback"]["dds_frequency_readback_hz"]
        tone_readback_hz = (abs(float(frequencies[0])) + abs(float(frequencies[2]))) / 2.0
        pilot = estimate_coherent_pilot_offset(
            rx1,
            sample_rate_hz=SAMPLE_RATE_HZ,
            nominal_tone_offset_hz=tone_readback_hz,
        )
        coherent = analyze_coherent_leakage(
            rx1,
            rx2,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=pilot.estimated_offset_hz,
        )
        recomputed_analysis = coherent_measurement_document(pilot, coherent)
    except (FineFrequencyError, OSError, TypeError, ValueError) as error:
        raise FineFrequencyAnalysisError(f"raw CI16 reanalysis failed: {error}") from error
    if (
        not headroom.passed
        or clipped != 0
        or admitted["capture"]["headroom_passed"] is not True
        or admitted["capture"]["clipped_sample_count"] != clipped
        or recomputed_analysis != admitted["analysis"]
        or recomputed_analysis != record["analysis"]
    ):
        raise FineFrequencyAnalysisError(
            "recomputed IQ headroom/phasor/magnitude differs from persisted evidence"
        )
    recomputed_evidence = dict(admitted)
    recomputed_evidence["analysis"] = recomputed_analysis
    normalized = normalized_observation_from_evidence(condition, recomputed_evidence)
    if result.get("normalized_observation") != normalized:
        raise FineFrequencyAnalysisError("normalized observation differs from raw-IQ recomputation")
    return normalized


def analyze_campaign(run_root: Path) -> dict[str, Any]:
    """Re-admit every condition, then perform the frozen statistical analysis."""

    exact_root = run_root.expanduser().absolute()
    _assert_path_chain_no_symlink(exact_root, "run root")
    if exact_root.is_symlink() or not exact_root.is_dir():
        raise FineFrequencyAnalysisError("run root must be a regular non-symlink directory")
    plan_path = exact_root / PLAN_FILENAME
    manifest_path = exact_root / MANIFEST_FILENAME
    envelope = _read_json(plan_path, "immutable plan")
    contract = validate_plan_envelope(envelope)
    _verify_global_ledger_authority(contract, plan_path=plan_path)
    manifest = _read_json(manifest_path, "complete manifest")
    _reject_external_failure_receipt(contract, manifest, plan_path=plan_path)
    _attempt, results = _validate_complete_manifest_schema(
        manifest,
        contract=contract,
        envelope=envelope,
        plan_path=plan_path,
    )
    expected_count = int(contract["storage"]["condition_count"])
    if Path(str(contract["execution_storage"]["run_root"])).absolute() != exact_root:
        raise FineFrequencyAnalysisError("campaign run root differs from immutable plan")
    external_burn = _verify_execution_tombstone(
        exact_root,
        plan_path=plan_path,
        contract=contract,
        manifest=manifest,
    )
    _verify_prepared_manifest_reconstruction(
        contract=contract,
        envelope=envelope,
        plan_path=plan_path,
        manifest=manifest,
        external_burn=external_burn,
    )
    _verify_analyzer_source(contract)
    _verify_native_identity(contract)
    _verify_fixture_and_storage(contract, exact_root)
    capture_root = Path(str(contract["execution_storage"]["capture_root"])).absolute()
    if capture_root != exact_root / "captures":
        raise FineFrequencyAnalysisError("planned capture root differs from run root")
    _assert_path_chain_no_symlink(capture_root, "capture root")
    if capture_root.is_symlink() or not capture_root.is_dir():
        raise FineFrequencyAnalysisError("planned capture root is missing or a symlink")
    planned = contract["schedule"]["conditions"]
    stream_ids: set[int] = set()
    artifact_hashes: set[str] = set()
    observations: list[dict[str, Any]] = []
    for index, (condition, result) in enumerate(zip(planned, results, strict=True)):
        if (
            not isinstance(result, Mapping)
            or result.get("plan_index") != index
            or result.get("condition_id") != condition["condition_id"]
            or result.get("campaign_accepted") is not True
            or result.get("campaign_acceptance_pending") is not False
        ):
            raise FineFrequencyAnalysisError(
                "manifest condition order/acceptance differs from plan"
            )
        normalized = _reanalyze_condition(
            contract,
            condition,
            result,
            capture_root=capture_root,
            prior_stream_ids=stream_ids,
            prior_artifact_sha256s=artifact_hashes,
        )
        stream_ids.add(int(normalized["stream_id"]))
        artifact_hashes.add(str(normalized["artifact_sha256"]))
        observations.append(normalized)
    analysis = analyze_sweep(contract, observations)
    refinement_selection = (
        select_coarse_refinements(contract, observations) if contract["mode"] == "coarse" else None
    )
    campaign_binding = campaign_cross_binding_from_plan_contract(contract)
    return {
        "schema": 1,
        "results_kind": "5g8_bidirectional_frequency_results",
        "mode": contract["mode"],
        "run_id": contract["run_id"],
        "board_id": contract["board_id"],
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "plan_contract_sha256": envelope["plan_contract_sha256"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "run_state_ledger": manifest["run_state_ledger"],
        "external_run_burn": external_burn,
        "external_run_burn_sha256": canonical_json_sha256(external_burn),
        "condition_count": expected_count,
        "campaign_binding": campaign_binding,
        "campaign_binding_sha256": canonical_json_sha256(campaign_binding),
        "coarse_results_binding": contract["coarse_results_binding"],
        "observations_sha256": canonical_json_sha256(observations),
        "analysis": analysis,
        "refinement_selection": refinement_selection,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        results = analyze_campaign(args.run_root)
        output = (
            args.output.expanduser().absolute()
            if args.output is not None
            else args.run_root.expanduser().absolute() / DEFAULT_RESULTS_FILENAME
        )
        _write_immutable_json(output, results)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "mode": results["mode"],
                    "condition_count": results["condition_count"],
                    "pooling_performed": results["analysis"]["pooling_performed"],
                    "results": str(output),
                    "results_sha256": sha256_path(output),
                },
                sort_keys=True,
            )
        )
        return 0
    except (FineFrequencyAnalysisError, FineFrequencyError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
