#!/usr/bin/env python3
"""Verify and aggregate one source-bound A/B/C/E 5.8 GHz campaign."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from typing import Any

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.file_artifact_admission import (
    admit_dual_rx_ci16_artifact,
    assert_local_rpi_storage,
    read_json_file,
)
from smateway.leakage_attribution import (
    AttributionRepeat,
    StageAttributionEvidence,
    StagedAttributionSummary,
    summarize_staged_attribution,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.ota_analysis import estimate_coherent_pilot_offset

STAGE_ARGUMENTS = {
    "A": "direct_rx2_termination",
    "B": "rx2_cable_terminated",
    "C": "powered_selector_all_inputs_terminated",
    "E": "full_conducted_fixture",
}
STAGE_ORDER = tuple(STAGE_ARGUMENTS)

_LEAKAGE_RUNNER: Any | None = None


class CampaignAnalysisError(RuntimeError):
    """The on-disk campaign cannot support attribution."""


def _runner() -> Any:
    """Load the authoritative verifier only when an input must be admitted."""

    global _LEAKAGE_RUNNER
    if _LEAKAGE_RUNNER is None:
        try:
            _LEAKAGE_RUNNER = importlib.import_module("scripts.run_5g8_leakage_ladder")
        except ModuleNotFoundError as error:
            if error.name != "scripts":
                raise CampaignAnalysisError(
                    f"cannot load the 5.8-GHz leakage-ladder verifier: {error}"
                ) from error
            module_name = "smateway_5g8_leakage_ladder_verifier"
            spec = importlib.util.spec_from_file_location(
                module_name,
                Path(__file__).with_name("run_5g8_leakage_ladder.py"),
            )
            if spec is None or spec.loader is None:
                raise CampaignAnalysisError(
                    "cannot locate the 5.8-GHz leakage-ladder verifier"
                ) from error
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except (ImportError, SyntaxError) as load_error:
                sys.modules.pop(module_name, None)
                raise CampaignAnalysisError(
                    f"cannot load the 5.8-GHz leakage-ladder verifier: {load_error}"
                ) from load_error
            _LEAKAGE_RUNNER = module
        except (ImportError, SyntaxError) as error:
            raise CampaignAnalysisError(
                f"cannot load the 5.8-GHz leakage-ladder verifier: {error}"
            ) from error
    return _LEAKAGE_RUNNER


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _exact_input_file(path: Path, label: str, *, runner: Any) -> Path:
    candidate = path.expanduser().absolute()
    runner._assert_path_chain_has_no_symlink(candidate, label=label)
    exact = candidate.resolve(strict=True)
    if not exact.is_file():
        raise CampaignAnalysisError(f"{label} must be a regular file")
    return exact


def _read_json(path: Path, label: str, *, runner: Any) -> dict[str, Any]:
    exact = _exact_input_file(path, label, runner=runner)
    document = runner._read_json(exact, label)
    if not isinstance(document, dict):
        raise CampaignAnalysisError(f"{label} root must be an object")
    return document


def _assert_declared_paths_have_no_symlink(
    value: object,
    *,
    label: str,
    runner: Any,
) -> None:
    """Reject symlinks before the runner resolves and rehashes frozen fixture files."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            if (
                key in {"path", "evidence_path", "header_path", "plan_path"}
                and isinstance(item, str)
                and item
            ):
                candidate = Path(item).expanduser()
                if candidate.is_absolute():
                    runner._assert_path_chain_has_no_symlink(
                        candidate,
                        label=f"{label} {key}",
                    )
            else:
                _assert_declared_paths_have_no_symlink(item, label=label, runner=runner)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_declared_paths_have_no_symlink(item, label=label, runner=runner)


def _rebuild_expected_contract(
    contract: Mapping[str, Any],
    *,
    expected_stage: str,
    runner: Any,
) -> dict[str, Any]:
    source = contract.get("source")
    configuration = contract.get("configuration")
    fixture = contract.get("fixture_evidence")
    dependency = (
        source.get("pluto_plus_utils_source_attestation") if isinstance(source, Mapping) else None
    )
    runtime = (
        source.get("native_libiio_runtime_attestation") if isinstance(source, Mapping) else None
    )
    if (
        not isinstance(source, Mapping)
        or not isinstance(configuration, Mapping)
        or not isinstance(fixture, Mapping)
        or not isinstance(dependency, Mapping)
        or not isinstance(runtime, Mapping)
    ):
        raise CampaignAnalysisError("stage source/configuration/fixture evidence is malformed")
    selector = contract.get("selector_control")
    if selector is not None and not isinstance(selector, Mapping):
        raise CampaignAnalysisError("stage selector-control contract is malformed")
    rebuilt = runner._build_plan_contract(
        run_id=str(contract.get("run_id", "")),
        board_id=str(contract.get("board_id", "")),
        serial=str(configuration.get("serial", "")),
        uri=str(configuration.get("uri", "")),
        stage=expected_stage,
        source_commit=str(source.get("smateway_commit", "")),
        pluto_plus_utils_source_attestation=dependency,
        selector_control=selector,
        native_libiio_runtime_attestation=runtime,
        fixture_evidence=fixture,
        freeze_attribution_repeats=True,
    )
    if not isinstance(rebuilt, dict):
        raise CampaignAnalysisError("runner did not rebuild an immutable plan contract")
    return rebuilt


def _verify_current_execution_identity(
    contract: Mapping[str, Any],
    *,
    runner: Any,
) -> dict[str, Any]:
    """Re-attest the code, dependency, and native runtime used by this analysis.

    A frozen plan proves what produced the captures.  It does not, by itself,
    prove that the process deriving a result is still running those bytes.  A
    clean repository at the exact frozen commit closes the complete Smateway
    source tree (including this analyzer); the dependency and native identities
    are independently rebuilt and compared as complete normalized documents.
    """

    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise CampaignAnalysisError("stage plan lacks frozen source identity")
    expected_dependency = source.get("pluto_plus_utils_source_attestation")
    expected_native = source.get("native_libiio_runtime_attestation")
    if not isinstance(expected_dependency, Mapping) or not isinstance(expected_native, Mapping):
        raise CampaignAnalysisError("stage plan lacks dependency or native-runtime identity")
    try:
        current_commit = runner._repository_commit_and_require_clean(
            Path(__file__).resolve().parents[1],
            "smateway analyzer",
        )
        current_dependency = runner._validate_dependency_source_attestation(
            runner.attest_pluto_plus_utils_source()
        )
        current_native = runner._validate_native_libiio_runtime_attestation(
            runner._native_libiio_runtime_attestation()
        )
    except Exception as error:
        raise CampaignAnalysisError(
            f"cannot re-attest current analysis source/native closure: {error}"
        ) from error
    if current_commit != source.get("smateway_commit"):
        raise CampaignAnalysisError(
            "current clean Smateway source closure differs from the immutable plan"
        )
    if current_dependency != dict(expected_dependency) or _canonical_sha256(
        current_dependency
    ) != source.get("pluto_plus_utils_source_attestation_sha256"):
        raise CampaignAnalysisError(
            "current pluto-plus-utils source closure differs from the immutable plan"
        )
    if current_native != dict(expected_native) or _canonical_sha256(current_native) != source.get(
        "native_libiio_runtime_attestation_sha256"
    ):
        raise CampaignAnalysisError(
            "current native libiio identity differs from the immutable plan"
        )
    current_source = {
        "smateway_commit": current_commit,
        "pluto_plus_utils_source_attestation": current_dependency,
        "pluto_plus_utils_source_attestation_sha256": _canonical_sha256(current_dependency),
        "native_libiio_runtime_attestation": current_native,
        "native_libiio_runtime_attestation_sha256": _canonical_sha256(current_native),
        "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
        "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
    }
    if current_source != dict(source):
        raise CampaignAnalysisError(
            "current complete analysis source contract differs from the immutable plan"
        )
    return current_source


def _history_passed(
    manifest: Mapping[str, Any],
    *,
    attempts_field: str,
    current_field: str | None,
    validator: Callable[[object], bool],
    label: str,
) -> None:
    attempts = manifest.get(attempts_field)
    if not isinstance(attempts, list) or not attempts:
        raise CampaignAnalysisError(f"stage manifest lacks {label}")
    if any(not validator(value) for value in attempts):
        raise CampaignAnalysisError(f"stage manifest contains invalid {label}")
    if current_field is not None and manifest.get(current_field) != attempts[-1]:
        raise CampaignAnalysisError(f"stage manifest latest {label} binding is invalid")


def _confirmation_passed(
    value: object,
    *,
    contract: Mapping[str, Any],
    fixture: Mapping[str, Any],
    runner: Any,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = contract.get("operator_confirmations_required")
    stage = contract.get("topology_stage")
    if not isinstance(required, Mapping) or not isinstance(stage, str):
        return False
    exact_fields = {
        "confirmed_at",
        "stage",
        "topology_confirmation_token",
        "no_antennas_anywhere",
        "tx1_matched_conducted_network",
        "tx2_muted_and_50ohm_terminated",
        "rx1_attenuated_conducted_reference",
        "no_component_or_connection_movement_since_setup_attestation",
        "fixture_evidence_sha256",
        "shared_fixture_sha256",
        "stage_delta_sha256",
        "setup_attestation_sha256",
        "setup_evidence_sha256",
        "observed_component_ids",
        "observed_connection_ids",
        "campaign_id",
        "comparable_fixture_group_id",
        "prior_stage_binding",
        "selector_static_all_off_physically_expected",
        "confirmation_method",
    }
    return (
        set(value) == exact_fields
        and isinstance(value.get("confirmed_at"), str)
        and bool(value.get("confirmed_at"))
        and value.get("stage") == stage
        and value.get("topology_confirmation_token") == required.get("topology_confirmation_token")
        and value.get("no_antennas_anywhere") is True
        and value.get("tx1_matched_conducted_network") is True
        and value.get("tx2_muted_and_50ohm_terminated") is True
        and value.get("rx1_attenuated_conducted_reference") is True
        and value.get("no_component_or_connection_movement_since_setup_attestation") is True
        and value.get("selector_static_all_off_physically_expected")
        is (stage in runner.SELECTOR_CONNECTED_STAGES)
        and value.get("confirmation_method") == "explicit CLI flags after physical inspection"
        and runner._confirmation_fixture_binding_passed(value, fixture)
    )


def _passing_manifest_completion(
    manifest: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    condition_count: int,
    runner: Any,
) -> None:
    configuration = contract.get("configuration")
    source = contract.get("source")
    fixture = contract.get("fixture_evidence")
    if (
        not isinstance(configuration, Mapping)
        or not isinstance(source, Mapping)
        or not isinstance(fixture, Mapping)
    ):
        raise CampaignAnalysisError("stage completion inputs are malformed")
    serial = str(configuration.get("serial", ""))
    uri = str(configuration.get("uri", ""))
    runtime = source.get("native_libiio_runtime_attestation")
    if not isinstance(runtime, Mapping):
        raise CampaignAnalysisError("stage lacks frozen native-runtime evidence")
    if (
        manifest.get("status") != "complete"
        or not isinstance(manifest.get("completed_at"), str)
        or not manifest.get("completed_at")
        or manifest.get("error") is not None
        or manifest.get("selector_calibration_claim") is not False
        or manifest.get("causal_attribution_claim") is not False
        or manifest.get("summary") != runner._manifest_summary(manifest, condition_count)
    ):
        raise CampaignAnalysisError("stage manifest is not an exact successful completion")
    summary = manifest.get("summary")
    if not isinstance(summary, Mapping) or (
        summary.get("planned_conditions") != condition_count
        or summary.get("completed_conditions") != condition_count
        or summary.get("remaining_conditions") != 0
        or summary.get("measurement_quality_passed") != condition_count
        or summary.get("measurement_quality_rejected") != 0
        or summary.get("failed_conditions") != 0
        or summary.get("quarantine_count") != 0
    ):
        raise CampaignAnalysisError("stage manifest summary does not admit every quality result")
    for field in (
        "recovery_mute_attempts",
        "recovery_selector_cleanup_attempts",
        "orphan_quarantine_attempts",
    ):
        if manifest.get(field) != []:
            raise CampaignAnalysisError(f"stage manifest contains disallowed {field}")

    confirmations = manifest.get("confirmations")
    if (
        not isinstance(confirmations, list)
        or not confirmations
        or any(
            not _confirmation_passed(value, contract=contract, fixture=fixture, runner=runner)
            for value in confirmations
        )
    ):
        raise CampaignAnalysisError("stage manifest lacks exact topology/setup confirmation")

    _history_passed(
        manifest,
        attempts_field="native_runtime_preflight_attempts",
        current_field="native_runtime_preflight",
        validator=lambda value: runner._runtime_attestation_passed(value, expected=runtime),
        label="native-runtime preflight",
    )
    _history_passed(
        manifest,
        attempts_field="fixture_evidence_preflight_attempts",
        current_field="fixture_evidence_preflight",
        validator=lambda value: runner._fixture_evidence_passed(value, expected=fixture),
        label="fixture-evidence preflight",
    )
    _history_passed(
        manifest,
        attempts_field="identity_preflight_attempts",
        current_field="identity_preflight",
        validator=lambda value: runner._identity_passed(
            value,
            serial=serial,
            requested_uri=uri,
        ),
        label="exact USB-identity preflight",
    )
    _history_passed(
        manifest,
        attempts_field="preflight_mute_attempts",
        current_field=None,
        validator=lambda value: runner._mute_passed(
            value,
            serial=serial,
            purpose="preflight",
        ),
        label="preflight mute",
    )
    _history_passed(
        manifest,
        attempts_field="final_mute_attempts",
        current_field="final_mute",
        validator=lambda value: runner._mute_passed(
            value,
            serial=serial,
            purpose="final",
        ),
        label="final mute",
    )

    selector_control = contract.get("selector_control")
    stage = contract.get("topology_stage")
    if stage in runner.SELECTOR_CONNECTED_STAGES:
        if not isinstance(selector_control, Mapping):
            raise CampaignAnalysisError("selector-connected stage lacks selector control")
        _history_passed(
            manifest,
            attempts_field="selector_initial_state_attempts",
            current_field="selector_initial_state",
            validator=lambda value: runner._selector_passed(
                value,
                selector_control=selector_control,
                purpose="initial_state_before_command",
            ),
            label="initial static ALL_OFF selector attestation",
        )
        _history_passed(
            manifest,
            attempts_field="final_selector_cleanup_attempts",
            current_field="final_selector_cleanup",
            validator=lambda value: runner._selector_passed(
                value,
                selector_control=selector_control,
                purpose="final_cleanup_all_off",
            ),
            label="final static ALL_OFF selector cleanup",
        )
    elif (
        selector_control is not None
        or manifest.get("selector_initial_state_attempts") != []
        or manifest.get("selector_initial_state") is not None
        or manifest.get("final_selector_cleanup_attempts") != []
        or manifest.get("final_selector_cleanup") is not None
    ):
        raise CampaignAnalysisError("selector-disconnected stage contains selector evidence")


def _complex_repeat(result: Mapping[str, Any]) -> AttributionRepeat:
    raw_index = result.get("attribution_repeat_index")
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        raise CampaignAnalysisError("attribution repeat index is malformed")
    transfer = result.get("rx2_over_rx1")
    if not isinstance(transfer, Mapping):
        raise CampaignAnalysisError("attribution result lacks RX2/RX1 transfer")
    detected = result.get("rx2_tone_detected") is True
    if detected:
        phasor_document = transfer.get("phasor")
        if not isinstance(phasor_document, Mapping):
            raise CampaignAnalysisError("detected attribution result lacks complex phasor")
        try:
            phasor = complex(
                float(phasor_document["real"]),
                float(phasor_document["imag"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CampaignAnalysisError("detected attribution phasor is malformed") from error
        if not isfinite(phasor.real) or not isfinite(phasor.imag):
            raise CampaignAnalysisError("detected attribution phasor must be finite")
        upper = None
    else:
        # A diagnostic phasor may be present below the detection threshold.  It is
        # deliberately discarded: nondetection contributes magnitude bounds only.
        phasor = None
        raw_upper = transfer.get("amplitude_upper_bound_ratio")
        if isinstance(raw_upper, bool) or not isinstance(raw_upper, (int, float)):
            raise CampaignAnalysisError("nondetection lacks phase-free magnitude bound")
        upper = float(raw_upper)
        if not isfinite(upper) or upper <= 0.0:
            raise CampaignAnalysisError("nondetection magnitude bound must be finite and positive")
    stream_id = result.get("stream_id")
    if isinstance(stream_id, bool) or not isinstance(stream_id, int):
        raise CampaignAnalysisError("attribution stream ID is malformed")
    condition_id = result.get("condition_id")
    if not isinstance(condition_id, str) or not condition_id:
        raise CampaignAnalysisError("attribution condition ID is malformed")
    artifact_sha = result.get("artifact_data_sha256")
    if not isinstance(artifact_sha, str):
        raise CampaignAnalysisError("attribution artifact SHA-256 is missing")
    return AttributionRepeat(
        repeat_index=raw_index,
        condition_id=condition_id,
        stream_id=stream_id,
        artifact_sha256=artifact_sha,
        quality_passed=result.get("measurement_quality_passed") is True,
        detected=detected,
        phasor=phasor,
        amplitude_upper_bound_ratio=upper,
    )


def _provenance_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    source = contract.get("source")
    configuration = contract.get("configuration")
    fixture = contract.get("fixture_evidence")
    if not all(isinstance(value, Mapping) for value in (source, configuration, fixture)):
        raise CampaignAnalysisError("stage source/configuration/fixture is malformed")
    assert isinstance(source, Mapping)
    assert isinstance(configuration, Mapping)
    assert isinstance(fixture, Mapping)
    fields = (
        "center_frequency_hz",
        "tone_offset_hz_requested",
        "sample_rate_hz",
        "bandwidth_hz",
        "receiver_gain_db",
        "dds_scale",
        "attribution_gain_db",
        "metadata_abi",
        "kernel_buffers",
    )
    return {
        "campaign_id": fixture.get("campaign_id"),
        "comparable_fixture_group_id": fixture.get("comparable_fixture_group_id"),
        "board_id": contract.get("board_id"),
        "pluto_serial": configuration.get("serial"),
        "source": dict(source),
        "acquisition": {field: configuration.get(field) for field in fields},
    }


def _planned_conditions(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = contract.get("conditions")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise CampaignAnalysisError("stage plan conditions are malformed")
    planned: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise CampaignAnalysisError("stage plan condition is malformed")
        condition_id = value.get("condition_id")
        if not isinstance(condition_id, str) or not condition_id or condition_id in planned:
            raise CampaignAnalysisError("stage plan condition IDs are malformed or duplicated")
        planned[condition_id] = value
    if not planned:
        raise CampaignAnalysisError("stage plan contains no conditions")
    return planned


def _attribution_repeats(
    manifest: Mapping[str, Any],
    *,
    planned: Mapping[str, Mapping[str, Any]],
    repeat_count: int,
) -> tuple[AttributionRepeat, ...]:
    if repeat_count != 5:
        raise CampaignAnalysisError("attribution repeat count must be frozen at five")
    attribution_conditions = sorted(
        (
            condition
            for condition in planned.values()
            if condition.get("attribution_repeat_index") is not None
        ),
        key=lambda condition: int(condition["attribution_repeat_index"]),
    )
    indices = [condition.get("attribution_repeat_index") for condition in attribution_conditions]
    if (
        len(attribution_conditions) != repeat_count
        or indices != list(range(1, repeat_count + 1))
        or any(
            condition.get("attribution_repeat_count") != repeat_count
            for condition in attribution_conditions
        )
    ):
        raise CampaignAnalysisError("stage plan lacks exactly five indexed attribution sources")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        raise CampaignAnalysisError("stage manifest attempts are malformed")
    results: dict[str, Mapping[str, Any]] = {}
    for attempt in attempts:
        if not isinstance(attempt, Mapping) or not isinstance(attempt.get("result"), Mapping):
            raise CampaignAnalysisError("stage manifest contains a malformed completed attempt")
        result = attempt["result"]
        assert isinstance(result, Mapping)
        condition_id = result.get("condition_id")
        if not isinstance(condition_id, str) or condition_id in results:
            raise CampaignAnalysisError("stage manifest reuses a result condition ID")
        results[condition_id] = result
    repeats = tuple(
        _complex_repeat(results[str(condition["condition_id"])])
        for condition in attribution_conditions
    )
    identities = (
        [repeat.condition_id for repeat in repeats],
        [repeat.stream_id for repeat in repeats],
        [repeat.artifact_sha256 for repeat in repeats],
    )
    if any(len(set(values)) != repeat_count for values in identities):
        raise CampaignAnalysisError("stage attribution sources are not five unique acquisitions")
    if any(not repeat.quality_passed for repeat in repeats):
        raise CampaignAnalysisError("stage attribution repeat failed measurement quality")
    return repeats


def _reverify_raw_result(
    result: Mapping[str, Any],
    *,
    condition: Mapping[str, Any],
    contract: Mapping[str, Any],
    runner: Any,
) -> None:
    """Recompute headroom, pilot, and complex transfer from persisted CI16."""

    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise CampaignAnalysisError("stage configuration is malformed")
    evidence = {
        "artifact_id": result.get("artifact_id"),
        "data_path": result.get("artifact_data_path"),
        "data_sha256": result.get("artifact_data_sha256"),
        "metadata_path": result.get("artifact_metadata_path"),
        "metadata_sha256": result.get("artifact_metadata_sha256"),
    }
    samples, continuity, _, metadata_path = admit_dual_rx_ci16_artifact(
        evidence,
        label=f"stage condition {result.get('condition_id')}",
        expected_sample_count=int(configuration["sample_count_per_condition"]),
        expected_samples_per_block=int(configuration["samples_per_frame"]),
        expected_sample_rate_hz=float(configuration["sample_rate_hz"]),
        expected_stream_id=result.get("stream_id"),
        expected_artifact_id=str(result.get("artifact_id")),
    )
    metadata = read_json_file(metadata_path, label="stage SigMF metadata")
    captures = metadata.get("captures")
    global_section = metadata.get("global")
    if (
        not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
        or not isinstance(global_section, Mapping)
    ):
        raise CampaignAnalysisError("stage SigMF settings/identity are malformed")
    settings = captures[0].get("settings")
    radio = global_section.get("pluto:radio")
    expected_settings = {
        "bandwidth_hz": float(configuration["bandwidth_hz"]),
        "center_frequency_hz": float(configuration["center_frequency_hz"]),
        "channels": [0, 1],
        "gain_db": float(configuration["receiver_gain_db"]),
        "gain_mode": "manual",
        "sample_rate_hz": float(configuration["sample_rate_hz"]),
    }
    if (
        settings != expected_settings
        or not isinstance(radio, Mapping)
        or radio.get("serial") != configuration.get("serial")
        or radio.get("uri") != configuration.get("uri")
    ):
        raise CampaignAnalysisError("stage SigMF radio/settings differ from immutable plan")
    record_path = Path(str(result.get("condition_record_path", ""))).absolute()
    record = read_json_file(record_path, label="stage condition record")
    capture = record.get("capture")
    if not isinstance(capture, Mapping):
        raise CampaignAnalysisError("stage condition record capture is malformed")
    monitor = AdcHeadroomMonitor(receiver_count=2)
    monitor.observe(samples)
    headroom = json.loads(
        json.dumps(asdict(monitor.result()), sort_keys=True, allow_nan=False, default=str)
    )
    if capture.get("adc_headroom_admission") != headroom:
        raise CampaignAnalysisError("stage stored headroom differs from raw IQ")
    tone_readback = result.get("tone_offset_hz_readback")
    if isinstance(tone_readback, bool) or not isinstance(tone_readback, (int, float)):
        raise CampaignAnalysisError("stage tone readback is malformed")
    pilot = estimate_coherent_pilot_offset(
        samples[0],
        sample_rate_hz=float(configuration["sample_rate_hz"]),
        nominal_tone_offset_hz=float(tone_readback),
    )
    analysis = analyze_coherent_leakage(
        samples[0],
        samples[1],
        sample_rate_hz=float(configuration["sample_rate_hz"]),
        tone_offset_hz=pilot.estimated_offset_hz,
    )
    del samples
    analysis_document = runner._json_safe(asdict(analysis))
    stored_analysis = record.get("marker_independent_analysis")
    stored_pilot = capture.get("pilot_frequency_refinement")
    pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
    pilot_rejections: list[str] = []
    if pilot.confidence < runner.MINIMUM_PILOT_CONFIDENCE:
        pilot_rejections.append("rx1_pilot_confidence_below_minimum")
    if pilot.phase_step_coherence < runner.MINIMUM_PILOT_PHASE_STEP_COHERENCE:
        pilot_rejections.append("rx1_pilot_phase_step_coherence_below_minimum")
    if pilot_phase_rms_deg > runner.MAXIMUM_PILOT_PHASE_RMS_DEG:
        pilot_rejections.append("rx1_pilot_phase_rms_above_maximum")
    expected_pilot = {
        **runner._json_safe(asdict(pilot)),
        "phase_residual_rms_deg": pilot_phase_rms_deg,
        "minimum_confidence": runner.MINIMUM_PILOT_CONFIDENCE,
        "minimum_phase_step_coherence": runner.MINIMUM_PILOT_PHASE_STEP_COHERENCE,
        "maximum_phase_rms_deg": runner.MAXIMUM_PILOT_PHASE_RMS_DEG,
        "quality_passed": not pilot_rejections,
        "quality_rejection_reasons": pilot_rejections,
    }
    if (
        stored_analysis != analysis_document
        or not isinstance(stored_pilot, Mapping)
        or stored_pilot != expected_pilot
        or result.get("tone_offset_hz_measured") != pilot.estimated_offset_hz
        or result.get("pilot_confidence") != pilot.confidence
        or result.get("rx2_tone_detected") is not analysis.rx2.tone_detected
        or result.get("rx2_over_rx1") != analysis_document.get("rx2_over_rx1")
        or continuity.get("stream_id") != result.get("stream_id")
    ):
        raise CampaignAnalysisError("stage stored complex analysis differs from raw IQ")
    # A nondetection is admitted only through the analyzer's finite, phase-free
    # transfer bound; it never gains a synthesized complex phase.
    transfer = result.get("rx2_over_rx1")
    if not analysis.rx2.tone_detected and (
        not isinstance(transfer, Mapping)
        or transfer.get("phasor") is not None
        or not isinstance(transfer.get("amplitude_upper_bound_ratio"), (int, float))
        or float(transfer["amplitude_upper_bound_ratio"]) <= 0.0
    ):
        raise CampaignAnalysisError("stage nondetection lacks a phase-free magnitude bound")


def load_verified_stage(
    plan_path: Path,
    manifest_path: Path,
    *,
    stage_name: str,
) -> StageAttributionEvidence:
    """Reverify every plan, manifest, IQ, metadata, record, and hash without mutation."""

    if stage_name not in STAGE_ARGUMENTS:
        raise CampaignAnalysisError("stage name must be exactly A, B, C, or E")
    expected_stage = STAGE_ARGUMENTS[stage_name]
    runner = _runner()
    try:
        exact_plan = _exact_input_file(plan_path, f"Stage {stage_name} plan", runner=runner)
        exact_manifest = _exact_input_file(
            manifest_path,
            f"Stage {stage_name} manifest",
            runner=runner,
        )
        if exact_plan == exact_manifest or os.path.samefile(exact_plan, exact_manifest):
            raise CampaignAnalysisError("stage plan and manifest must be distinct files")
        plan_sha_before = _sha256_path(exact_plan)
        manifest_sha_before = _sha256_path(exact_manifest)

        document = _read_json(exact_plan, f"Stage {stage_name} plan", runner=runner)
        raw_contract = document.get("plan_contract")
        if not isinstance(raw_contract, Mapping):
            raise CampaignAnalysisError(f"Stage {stage_name} immutable plan is malformed")
        expected_contract = _rebuild_expected_contract(
            raw_contract,
            expected_stage=expected_stage,
            runner=runner,
        )
        envelope = runner._validate_plan_envelope(
            document,
            expected_contract=expected_contract,
        )
        contract = envelope.get("plan_contract")
        if not isinstance(contract, Mapping) or contract.get("topology_stage") != expected_stage:
            raise CampaignAnalysisError(f"Stage {stage_name} immutable plan is invalid")
        _verify_current_execution_identity(contract, runner=runner)
        planned = _planned_conditions(contract)

        manifest = runner._load_manifest(
            exact_manifest,
            plan_path=exact_plan,
            envelope=envelope,
        )
        if not isinstance(manifest, Mapping):
            raise CampaignAnalysisError("runner returned a malformed stage manifest")
        _passing_manifest_completion(
            manifest,
            contract=contract,
            condition_count=len(planned),
            runner=runner,
        )

        configuration = contract.get("configuration")
        storage = contract.get("storage")
        fixture = contract.get("fixture_evidence")
        if (
            not isinstance(configuration, Mapping)
            or not isinstance(storage, Mapping)
            or not isinstance(fixture, Mapping)
        ):
            raise CampaignAnalysisError("stage plan evidence is incomplete")
        _assert_declared_paths_have_no_symlink(
            fixture,
            label=f"Stage {stage_name} fixture evidence",
            runner=runner,
        )
        fresh_fixture = runner._live_fixture_evidence_boundary(fixture)
        if not runner._fixture_evidence_passed(fresh_fixture, expected=fixture):
            raise CampaignAnalysisError(
                f"Stage {stage_name} fixture files no longer match the immutable plan"
            )
        selector_control = contract.get("selector_control")
        if isinstance(selector_control, Mapping):
            _assert_declared_paths_have_no_symlink(
                selector_control,
                label=f"Stage {stage_name} selector control",
                runner=runner,
            )
            runner._verify_selector_artifacts(selector_control)

        capture_root_value = storage.get("run_capture_root")
        if (
            storage.get("medium") != "raspberry_pi_local_filesystem"
            or storage.get("pluto_onboard_storage_used") is not False
            or not isinstance(capture_root_value, str)
            or not capture_root_value
            or not Path(capture_root_value).is_absolute()
        ):
            raise CampaignAnalysisError("stage capture root is malformed")
        for storage_field in ("board_state_root", "artifact_root", "run_capture_root"):
            raw_storage_path = storage.get(storage_field)
            if not isinstance(raw_storage_path, str) or not Path(raw_storage_path).is_absolute():
                raise CampaignAnalysisError(f"stage {storage_field} is malformed")
            assert_local_rpi_storage(
                Path(raw_storage_path), label=f"Stage {stage_name} {storage_field}"
            )
        raw_capture_root = Path(capture_root_value).expanduser()
        runner._assert_path_chain_has_no_symlink(
            raw_capture_root,
            label=f"Stage {stage_name} capture root",
        )
        capture_root = raw_capture_root.resolve(strict=True)
        if not capture_root.is_dir():
            raise CampaignAnalysisError("stage capture root is not a directory")
        completed = runner._completed_condition_ids(
            manifest,
            planned_conditions=planned,
            contract=contract,
            serial=str(configuration["serial"]),
            plan_evidence=runner._plan_file_evidence(exact_plan, envelope),
            capture_root=capture_root,
            downgrade_invalid=False,
        )
        if completed != set(planned):
            raise CampaignAnalysisError(f"Stage {stage_name} is missing immutable conditions")
        failure_tombstone = exact_manifest.parent / "failed-run.tombstone.json"
        if failure_tombstone.exists() or failure_tombstone.is_symlink():
            raise CampaignAnalysisError(
                f"Stage {stage_name} has a failure tombstone and cannot be accepted"
            )
        attempts = manifest.get("attempts")
        if not isinstance(attempts, list):
            raise CampaignAnalysisError("stage attempts are malformed")
        for attempt in attempts:
            if not isinstance(attempt, Mapping) or not isinstance(attempt.get("result"), Mapping):
                raise CampaignAnalysisError("stage contains a malformed completed result")
            result = attempt["result"]
            assert isinstance(result, Mapping)
            condition_id = result.get("condition_id")
            if not isinstance(condition_id, str) or condition_id not in planned:
                raise CampaignAnalysisError("stage result condition is not in the immutable plan")
            raw_verifier = getattr(runner, "_offline_reverify_raw_result", _reverify_raw_result)
            raw_verifier(
                result,
                condition=planned[condition_id],
                contract=contract,
                runner=runner,
            )

        repeat_count = configuration.get("attribution_repeat_count")
        if isinstance(repeat_count, bool) or not isinstance(repeat_count, int):
            raise CampaignAnalysisError("stage attribution repeat count is malformed")
        repeats = _attribution_repeats(
            manifest,
            planned=planned,
            repeat_count=repeat_count,
        )
        if (
            _sha256_path(exact_plan) != plan_sha_before
            or _sha256_path(exact_manifest) != manifest_sha_before
        ):
            raise CampaignAnalysisError("stage plan or manifest changed during verification")

        stage_delta = fixture.get("stage_delta")
        shared = fixture.get("shared_fixture")
        source_files = fixture.get("source_files")
        characterization = fixture.get("characterization_summary")
        if (
            not isinstance(stage_delta, Mapping)
            or not isinstance(shared, Mapping)
            or not isinstance(source_files, Mapping)
            or not isinstance(characterization, Mapping)
        ):
            raise CampaignAnalysisError("fixture identity is malformed")
        setup_source = source_files.get("setup_attestation")
        if not isinstance(setup_source, Mapping) or not isinstance(setup_source.get("sha256"), str):
            raise CampaignAnalysisError("fixture setup-attestation identity is malformed")
        selector_sha = (
            _canonical_sha256(selector_control) if isinstance(selector_control, Mapping) else None
        )
        return StageAttributionEvidence(
            stage=stage_name,
            run_id=str(contract["run_id"]),
            contemporaneous_group_id=str(fixture["comparable_fixture_group_id"]),
            shared_fixture_identity=dict(shared),
            provenance_identity=_provenance_identity(contract),
            stage_fixture_identity={
                "topology_stage": expected_stage,
                "plan_path": str(exact_plan),
                "plan_file_sha256": plan_sha_before,
                "plan_contract_sha256": envelope["plan_contract_sha256"],
                "manifest_path": str(exact_manifest),
                "manifest_file_sha256": manifest_sha_before,
                "fixture_evidence_sha256": contract["fixture_evidence_sha256"],
                "shared_fixture_sha256": fixture["shared_fixture_sha256"],
                "stage_delta": dict(stage_delta),
                "stage_delta_sha256": fixture["stage_delta_sha256"],
                "prior_stage_binding": fixture.get("prior_stage_binding"),
                "setup_attestation_sha256": setup_source["sha256"],
                "selector_control_sha256": selector_sha,
                "selector_flash_evidence": fixture.get("selector_flash_evidence"),
                "fixture_characterized": characterization.get("causal_attribution_fixture_eligible")
                is True,
            },
            repeats=repeats,
        )
    except CampaignAnalysisError:
        raise
    except Exception as error:
        raise CampaignAnalysisError(f"Stage {stage_name} verification failed: {error}") from error


def _require_stage_identity(
    evidence: StageAttributionEvidence,
) -> Mapping[str, Any]:
    identity = evidence.stage_fixture_identity
    if not isinstance(identity, Mapping):
        raise CampaignAnalysisError(f"Stage {evidence.stage} fixture identity is malformed")
    return identity


def _verify_immediate_adjacency(
    previous: StageAttributionEvidence,
    current: StageAttributionEvidence,
) -> None:
    previous_identity = _require_stage_identity(previous)
    current_identity = _require_stage_identity(current)
    binding = current_identity.get("prior_stage_binding")
    if not isinstance(binding, Mapping):
        raise CampaignAnalysisError(
            f"Stage {current.stage} lacks an immutable binding to Stage {previous.stage}"
        )
    comparison_anchor = binding.get("comparison_anchor")
    if not isinstance(comparison_anchor, Mapping):
        raise CampaignAnalysisError("prior-stage comparison anchor is malformed")
    expected = {
        "stage": STAGE_ARGUMENTS[previous.stage],
        "run_id": previous.run_id,
        "plan_path": previous_identity.get("plan_path"),
        "plan_file_sha256": previous_identity.get("plan_file_sha256"),
        "plan_contract_sha256": previous_identity.get("plan_contract_sha256"),
        "fixture_evidence_sha256": previous_identity.get("fixture_evidence_sha256"),
        "shared_fixture_sha256": previous_identity.get("shared_fixture_sha256"),
        "prior_stage_delta_sha256": previous_identity.get("stage_delta_sha256"),
        "prior_selector_control_sha256": previous_identity.get("selector_control_sha256"),
        "campaign_id": current.provenance_identity.get("campaign_id"),
        "comparable_fixture_group_id": current.contemporaneous_group_id,
        "prior_fixture_characterized": previous_identity.get("fixture_characterized"),
    }
    if any(binding.get(key) != value for key, value in expected.items()):
        raise CampaignAnalysisError(
            f"Stage {current.stage} does not bind the exact immediately prior "
            f"Stage {previous.stage} evidence"
        )
    if (
        comparison_anchor.get("from_stage") != STAGE_ARGUMENTS[previous.stage]
        or comparison_anchor.get("to_stage") != STAGE_ARGUMENTS[current.stage]
        or comparison_anchor.get("prior_stage_delta_sha256")
        != previous_identity.get("stage_delta_sha256")
        or binding.get("comparison_anchor_sha256") != _canonical_sha256(comparison_anchor)
    ):
        raise CampaignAnalysisError(
            f"Stage {current.stage} comparison anchor is not adjacent to Stage {previous.stage}"
        )


def summarize_verified_campaign(
    stages: Sequence[StageAttributionEvidence],
) -> StagedAttributionSummary:
    """Enforce ordered physical adjacency, then delegate identities/statistics to pure code."""

    if isinstance(stages, (str, bytes)) or tuple(stage.stage for stage in stages) != STAGE_ORDER:
        raise CampaignAnalysisError("campaign stages must be supplied in exact A/B/C/E order")
    first_identity = _require_stage_identity(stages[0])
    if first_identity.get("prior_stage_binding") is not None:
        raise CampaignAnalysisError("Stage A must not bind a prior-stage plan")
    for previous, current in zip(stages[:-1], stages[1:], strict=True):
        _verify_immediate_adjacency(previous, current)
    # This pure authority rejects unequal source/shared identities, duplicate source
    # streams/hashes/conditions, non-1..5 repeat sets, and phase-bearing nondetections.
    return summarize_staged_attribution(stages)


def _json_safe(value: object) -> Any:
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _assert_output_parent(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise CampaignAnalysisError(f"output parent contains a symlink: {current}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    """Durably publish one read-only result without replacing any directory entry."""

    output = path.expanduser().absolute()
    _assert_output_parent(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_output_parent(output.parent)
    if output.exists() or output.is_symlink():
        raise CampaignAnalysisError("output already exists")
    assert_local_rpi_storage(output, label="topology analysis output")
    payload = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o400)
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError as error:
            raise CampaignAnalysisError("output already exists") from error
        _fsync_directory(output.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
        _fsync_directory(output.parent)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for stage in STAGE_ORDER:
        parser.add_argument(f"--stage-{stage.lower()}-plan", type=Path, required=True)
        parser.add_argument(f"--stage-{stage.lower()}-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _input_hashes(
    stages: Sequence[StageAttributionEvidence],
    key: str,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for stage in stages:
        value = _require_stage_identity(stage).get(key)
        if not isinstance(value, str):
            raise CampaignAnalysisError(f"Stage {stage.stage} lacks {key}")
        hashes[stage.stage] = value
    return hashes


def main() -> int:
    args = _parser().parse_args()
    try:
        stages = tuple(
            load_verified_stage(
                getattr(args, f"stage_{stage.lower()}_plan"),
                getattr(args, f"stage_{stage.lower()}_manifest"),
                stage_name=stage,
            )
            for stage in STAGE_ORDER
        )
        summary = summarize_verified_campaign(stages)
        output = {
            "schema": 1,
            "analysis_kind": "5g8_source_bound_topology_campaign",
            "stage_order": list(STAGE_ORDER),
            "stages": [_json_safe(asdict(stage)) for stage in stages],
            "summary": _json_safe(asdict(summary)),
            "input_plan_sha256s": _input_hashes(stages, "plan_file_sha256"),
            "input_manifest_sha256s": _input_hashes(stages, "manifest_file_sha256"),
            "runner_artifact_revalidation": {
                "all_conditions_reverified": True,
                "invalid_completed_attempt_downgrade_enabled": False,
                "quarantine_or_mutation_performed": False,
            },
            "current_analysis_identity_reverified": {
                "clean_smateway_repository_at_frozen_commit": True,
                "pluto_plus_utils_source_attestation_exact_match": True,
                "native_libiio_runtime_attestation_exact_match": True,
            },
            "raw_iq_committed": False,
        }
        _write_new(args.output, output)
    except (CampaignAnalysisError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({"output": str(args.output.absolute()), "stage_count": 4}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
