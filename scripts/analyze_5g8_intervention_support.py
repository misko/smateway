#!/usr/bin/env python3
"""Reanalyze exact X-run IQ and produce the T8 intervention-support result.

This command is hardware-inert.  It reopens immutable X plans/manifests and
their bound CI16 artifacts, independently recomputes the known-tone transfer,
then applies the fixed simultaneous 3 dB / RX1-reference gate.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO = Path("/usr/local/lib")
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

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    admit_dual_rx_ci16_artifact,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
    read_json_file,
    verify_file_binding,
    verify_source_tree_binding,
)
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    attest_source_files_at_commit,
    sha256_path,
)
from smateway.intervention_support import (
    ATTRIBUTION_REPEAT_COUNT,
    INTERVENTION_SUPPORT_ANALYSIS_KIND,
    INTERVENTION_SUPPORT_RESULT_KIND,
    InterventionRepeat,
    InterventionSupportError,
    qualify_intervention_support,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import (
    attest_runtime,
    attestation_sha256,
    validate_runtime_attestation,
)
from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.selected_state_qualification import (
    SelectedStateQualificationError,
    canonical_sha256,
    reject_replace_placeholders,
    validate_intervention_change_plan,
)

from scripts import run_5g8_leakage_ladder as leakage_runner

ANALYSIS_SOURCE_FILES = (
    "src/smateway/capture_admission.py",
    "src/smateway/file_artifact_admission.py",
    "src/smateway/hexcal.py",
    "src/smateway/intervention_support.py",
    "src/smateway/leakage_ladder.py",
    "src/smateway/native_iio_attestation.py",
    "src/smateway/ota_analysis.py",
    "src/smateway/selected_state_qualification.py",
    "scripts/run_5g8_leakage_ladder.py",
    "scripts/analyze_5g8_intervention_support.py",
)
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")


class InterventionSupportAnalysisError(RuntimeError):
    """The exact X sources cannot support a trustworthy intervention result."""


@dataclass(frozen=True, slots=True)
class LoadedRole:
    source_identity: Mapping[str, Any]
    repeats: tuple[InterventionRepeat, ...]


RoleLoader = Callable[..., LoadedRole]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_safe(value: object) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, default=str))


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _file_evidence(path: Path, label: str, *, require_local: bool = True) -> dict[str, Any]:
    exact = assert_no_symlink_chain(path.expanduser().absolute(), label=label)
    if exact.is_symlink() or not exact.is_file():
        raise InterventionSupportAnalysisError(f"{label} must be a regular non-symlink file")
    if require_local:
        assert_local_rpi_storage(exact, label=label)
    return {
        "path": str(exact),
        "sha256": sha256_path(exact),
        "size_bytes": exact.stat().st_size,
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = read_json_file(path.expanduser().absolute(), label=label)
        reject_replace_placeholders(document, label)
    except (FileArtifactAdmissionError, SelectedStateQualificationError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    return document


def _ensure_output_path(path: Path, label: str) -> Path:
    exact = assert_no_symlink_chain(path.expanduser().absolute(), label=label)
    assert_local_rpi_storage(exact, label=label)
    if exact.exists() or exact.is_symlink():
        raise InterventionSupportAnalysisError(f"{label} already exists")
    parent = exact.parent
    assert_no_symlink_chain(parent, label=f"{label} parent")
    assert_local_rpi_storage(parent, label=f"{label} parent")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_no_symlink_chain(parent, label=f"{label} parent")
    assert_local_rpi_storage(parent, label=f"{label} parent")
    return exact


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(document)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            path.unlink()
        raise


def _repository_source_attestation(repository: Path = _REPOSITORY) -> dict[str, Any]:
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if GIT_COMMIT.fullmatch(head) is None:
            raise InterventionSupportAnalysisError("Smateway HEAD is not a full Git commit")
        observed = attest_source_files_at_commit(
            repository,
            expected_commit=head,
            relative_paths=ANALYSIS_SOURCE_FILES,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": head,
        "clean_source_files_verified": True,
        "files": observed["files"],
        "source_files_sha256": canonical_sha256(observed["files"]),
    }


def _local_runtime_bindings() -> dict[str, Any]:
    try:
        dependency = attest_pluto_plus_utils_source()
        native = attest_runtime()
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    source = _repository_source_attestation()
    return {
        "source": source,
        "dependency": dependency,
        "native": native,
        "source_commit": source["commit"],
        "dependency_commit": dependency["commit"],
        "native_attestation_sha256": attestation_sha256(native),
    }


def _validate_runtime_bindings(
    value: Mapping[str, Any], *, expected_source_identity: Mapping[str, Any]
) -> dict[str, Any]:
    if set(value) != {
        "source",
        "dependency",
        "native",
        "source_commit",
        "dependency_commit",
        "native_attestation_sha256",
    }:
        raise InterventionSupportAnalysisError("analysis runtime binding fields are incomplete")
    source = value.get("source")
    dependency = value.get("dependency")
    native = value.get("native")
    if not all(isinstance(item, Mapping) for item in (source, dependency, native)):
        raise InterventionSupportAnalysisError("analysis runtime source closure is malformed")
    assert isinstance(source, Mapping)
    assert isinstance(dependency, Mapping)
    assert isinstance(native, Mapping)
    if (
        set(source)
        != {
            "schema",
            "repository",
            "commit",
            "clean_source_files_verified",
            "files",
            "source_files_sha256",
        }
        or source.get("schema") != 1
        or source.get("clean_source_files_verified") is not True
        or source.get("commit") != value.get("source_commit")
        or source.get("source_files_sha256") != canonical_sha256(source.get("files"))
    ):
        raise InterventionSupportAnalysisError("analysis Smateway source attestation is malformed")
    try:
        verify_source_tree_binding(
            source,
            label="intervention-support analysis source",
            required_relative_paths=ANALYSIS_SOURCE_FILES,
        )
        frozen_native = validate_runtime_attestation(native)
    except (FileArtifactAdmissionError, ValueError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    observed = {
        "smateway_commit": source["commit"],
        "dependency_commit": dependency.get("commit"),
        "dependency_attestation_sha256": canonical_sha256(dependency),
        "native_attestation_sha256": attestation_sha256(frozen_native),
        "selector_evidence_sha256": expected_source_identity.get("selector_evidence_sha256"),
    }
    if (
        value.get("dependency_commit") != dependency.get("commit")
        or value.get("native_attestation_sha256") != observed["native_attestation_sha256"]
        or observed != dict(expected_source_identity)
    ):
        raise InterventionSupportAnalysisError(
            "current source/dependency/native identity differs from the X acquisitions"
        )
    return cast(dict[str, Any], _json_safe(dict(value)))


def _source_identity(contract: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise InterventionSupportAnalysisError("X plan lacks frozen source identity")
    dependency = source.get("pluto_plus_utils_source_attestation")
    native = source.get("native_libiio_runtime_attestation")
    if not isinstance(dependency, Mapping) or not isinstance(native, Mapping):
        raise InterventionSupportAnalysisError("X plan lacks dependency/native source identity")
    identity = {
        "smateway_commit": source.get("smateway_commit"),
        "dependency_commit": dependency.get("commit"),
        "dependency_attestation_sha256": canonical_sha256(dependency),
        "native_attestation_sha256": canonical_sha256(native),
        "selector_evidence_sha256": manifest.get("selector_evidence_sha256"),
    }
    if (
        GIT_COMMIT.fullmatch(str(identity["smateway_commit"])) is None
        or GIT_COMMIT.fullmatch(str(identity["dependency_commit"])) is None
        or source.get("pluto_plus_utils_source_attestation_sha256")
        != identity["dependency_attestation_sha256"]
        or source.get("native_libiio_runtime_attestation_sha256")
        != identity["native_attestation_sha256"]
    ):
        raise InterventionSupportAnalysisError("X plan source hashes are inconsistent")
    return identity


def _reanalyze_repeat(
    *,
    role: str,
    capture_binding: Mapping[str, Any],
    record: Mapping[str, Any],
    condition: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> InterventionRepeat:
    raw_binding = capture_binding.get("raw_iq_file")
    metadata_binding = capture_binding.get("metadata_file")
    artifact = record.get("artifact_evidence")
    capture = record.get("capture")
    if not all(
        isinstance(item, Mapping) for item in (raw_binding, metadata_binding, artifact, capture)
    ):
        raise InterventionSupportAnalysisError(f"{role} attribution record is malformed")
    assert isinstance(raw_binding, Mapping)
    assert isinstance(metadata_binding, Mapping)
    assert isinstance(artifact, Mapping)
    assert isinstance(capture, Mapping)
    if (
        artifact.get("data_path") != raw_binding.get("path")
        or artifact.get("data_sha256") != raw_binding.get("sha256")
        or artifact.get("data_size_bytes") != raw_binding.get("size_bytes")
        or artifact.get("metadata_path") != metadata_binding.get("path")
        or artifact.get("metadata_sha256") != metadata_binding.get("sha256")
        or artifact.get("metadata_size_bytes") != metadata_binding.get("size_bytes")
    ):
        raise InterventionSupportAnalysisError(
            f"{role} condition record differs from accepted artifact bindings"
        )
    stream_id = str(capture_binding.get("stream_id", ""))
    try:
        samples, continuity, _, _ = admit_dual_rx_ci16_artifact(
            artifact,
            label=f"{role} attribution repeat",
            expected_sample_count=int(configuration["sample_count_per_condition"]),
            expected_samples_per_block=int(configuration["samples_per_frame"]),
            expected_sample_rate_hz=float(configuration["sample_rate_hz"]),
            expected_stream_id=stream_id,
            expected_artifact_id=str(artifact.get("artifact_id", "")),
        )
    except (FileArtifactAdmissionError, KeyError, TypeError, ValueError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    if str(continuity.get("stream_id")) != stream_id or str(capture.get("stream_id")) != stream_id:
        raise InterventionSupportAnalysisError(f"{role} ABI-2 stream identity differs")
    monitor = AdcHeadroomMonitor(receiver_count=2)
    monitor.observe(samples)
    expected_headroom = _json_safe(asdict(monitor.result()))
    tone_readback = capture.get("tone_offset_hz_readback")
    if isinstance(tone_readback, bool) or not isinstance(tone_readback, (int, float)):
        raise InterventionSupportAnalysisError(f"{role} tone readback is malformed")
    pilot = estimate_coherent_pilot_offset(
        samples[0],
        sample_rate_hz=float(configuration["sample_rate_hz"]),
        nominal_tone_offset_hz=float(tone_readback),
    )
    pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
    pilot_reasons: list[str] = []
    if pilot.confidence < leakage_runner.MINIMUM_PILOT_CONFIDENCE:
        pilot_reasons.append("rx1_pilot_confidence_below_minimum")
    if pilot.phase_step_coherence < leakage_runner.MINIMUM_PILOT_PHASE_STEP_COHERENCE:
        pilot_reasons.append("rx1_pilot_phase_step_coherence_below_minimum")
    if pilot_phase_rms_deg > leakage_runner.MAXIMUM_PILOT_PHASE_RMS_DEG:
        pilot_reasons.append("rx1_pilot_phase_rms_above_maximum")
    analysis = analyze_coherent_leakage(
        samples[0],
        samples[1],
        sample_rate_hz=float(configuration["sample_rate_hz"]),
        tone_offset_hz=pilot.estimated_offset_hz,
    )
    del samples
    analysis_document = leakage_runner._json_safe(asdict(analysis))
    rejection_reasons = [*pilot_reasons, *analysis.quality_rejection_reasons]
    stored_pilot = capture.get("pilot_frequency_refinement")
    expected_pilot = {
        **leakage_runner._json_safe(asdict(pilot)),
        "phase_residual_rms_deg": pilot_phase_rms_deg,
        "minimum_confidence": leakage_runner.MINIMUM_PILOT_CONFIDENCE,
        "minimum_phase_step_coherence": leakage_runner.MINIMUM_PILOT_PHASE_STEP_COHERENCE,
        "maximum_phase_rms_deg": leakage_runner.MAXIMUM_PILOT_PHASE_RMS_DEG,
        "quality_passed": not pilot_reasons,
        "quality_rejection_reasons": pilot_reasons,
    }
    if (
        capture.get("adc_headroom_admission") != expected_headroom
        or stored_pilot != expected_pilot
        or record.get("marker_independent_analysis") != analysis_document
        or record.get("measurement_quality_passed") is not (not rejection_reasons)
        or record.get("measurement_quality_rejection_reasons") != rejection_reasons
        or rejection_reasons
    ):
        raise InterventionSupportAnalysisError(
            f"{role} stored quality/analysis differs from independently recomputed raw IQ"
        )
    transfer = analysis.rx2_over_rx1
    detected = analysis.rx2.tone_detected
    if detected:
        ratio = transfer.amplitude_ratio
        upper = None
        if ratio is None or ratio <= 0.0:
            raise InterventionSupportAnalysisError(f"{role} detected transfer is unavailable")
    else:
        ratio = None
        upper = transfer.amplitude_upper_bound_ratio
        if upper is None or upper <= 0.0 or transfer.phasor is not None:
            raise InterventionSupportAnalysisError(
                f"{role} nondetection lacks a phase-free upper bound"
            )
    repeat_index = condition.get("attribution_repeat_index")
    condition_id = condition.get("condition_id")
    if (
        isinstance(repeat_index, bool)
        or not isinstance(repeat_index, int)
        or not isinstance(condition_id, str)
    ):
        raise InterventionSupportAnalysisError(f"{role} attribution identity is malformed")
    return InterventionRepeat(
        repeat_index=repeat_index,
        condition_id=condition_id,
        stream_id=stream_id,
        raw_iq_sha256=str(raw_binding["sha256"]),
        quality_passed=True,
        rx1_amplitude_counts=analysis.rx1.amplitude_counts,
        transfer_detected=detected,
        transfer_amplitude_ratio=ratio,
        transfer_amplitude_upper_bound_ratio=upper,
    )


def _load_role(
    *, role: str, manifest_path: Path, expected_plan_file: Mapping[str, Any]
) -> LoadedRole:
    manifest_file = _file_evidence(manifest_path, f"X {role} manifest")
    bound_capture_files: list[tuple[Mapping[str, Any], str]] = []
    try:
        plan_path = verify_file_binding(expected_plan_file, label=f"X {role} plan")
        plan_document = _read_json(plan_path, f"X {role} plan")
        contract = plan_document.get("plan_contract")
        if not isinstance(contract, Mapping):
            raise InterventionSupportAnalysisError(f"X {role} plan contract is missing")
        leakage_runner._validate_plan_envelope(plan_document, expected_contract=contract)
        manifest = _read_json(manifest_path, f"X {role} manifest")
        leakage_runner._validate_x_capture_manifest(
            manifest,
            contract=contract,
            plan_path=plan_path,
        )
    except (FileArtifactAdmissionError, OSError, RuntimeError, ValueError) as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    if manifest.get("run_role") != role:
        raise InterventionSupportAnalysisError(f"X manifest role differs: expected {role}")
    configuration = contract.get("configuration")
    conditions = contract.get("conditions")
    if not isinstance(configuration, Mapping) or not isinstance(conditions, list):
        raise InterventionSupportAnalysisError(f"X {role} plan lacks acquisition configuration")
    planned: dict[str, Mapping[str, Any]] = {}
    for condition in conditions:
        if not isinstance(condition, Mapping) or not isinstance(condition.get("condition_id"), str):
            raise InterventionSupportAnalysisError(f"X {role} plan condition is malformed")
        planned[str(condition["condition_id"])] = condition
    repeats: list[InterventionRepeat] = []
    captures = manifest.get("captures")
    assert isinstance(captures, list)
    for capture_binding in captures:
        assert isinstance(capture_binding, Mapping)
        for field, label in (
            ("raw_iq_file", "raw IQ"),
            ("metadata_file", "metadata"),
            ("condition_record_file", "condition record"),
        ):
            binding = capture_binding.get(field)
            if not isinstance(binding, Mapping):
                raise InterventionSupportAnalysisError(f"X {role} lacks {label} binding")
            bound_capture_files.append((binding, f"X {role} {label}"))
        record_binding = capture_binding.get("condition_record_file")
        try:
            record_path = verify_file_binding(
                record_binding,
                label=f"X {role} condition record",
            )
        except FileArtifactAdmissionError as error:
            raise InterventionSupportAnalysisError(str(error)) from error
        record = _read_json(record_path, f"X {role} condition record")
        condition = record.get("condition")
        if not isinstance(condition, Mapping):
            raise InterventionSupportAnalysisError(f"X {role} condition binding is missing")
        condition_id = condition.get("condition_id")
        expected = planned.get(str(condition_id))
        if expected is None or dict(condition) != dict(expected):
            raise InterventionSupportAnalysisError(
                f"X {role} condition record differs from its immutable plan"
            )
        if condition.get("attribution_repeat_index") is not None:
            repeats.append(
                _reanalyze_repeat(
                    role=role,
                    capture_binding=capture_binding,
                    record=record,
                    condition=condition,
                    configuration=configuration,
                )
            )
    repeats.sort(key=lambda item: item.repeat_index)
    if len(repeats) != ATTRIBUTION_REPEAT_COUNT or [item.repeat_index for item in repeats] != list(
        range(1, ATTRIBUTION_REPEAT_COUNT + 1)
    ):
        raise InterventionSupportAnalysisError(
            f"X {role} lacks exactly five independently reanalyzed attribution repeats"
        )
    try:
        if _file_evidence(manifest_path, f"X {role} manifest") != manifest_file:
            raise InterventionSupportAnalysisError(f"X {role} manifest changed during analysis")
        verify_file_binding(expected_plan_file, label=f"X {role} plan final revalidation")
        for binding, label in bound_capture_files:
            verify_file_binding(binding, label=f"{label} final revalidation")
    except FileArtifactAdmissionError as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    return LoadedRole(
        source_identity=_source_identity(contract, manifest),
        repeats=tuple(repeats),
    )


def produce_intervention_support_result(
    *,
    change_plan_path: Path,
    x_manifest_paths: Mapping[str, Path],
    analysis_output: Path,
    result_output: Path,
    runtime_bindings: Mapping[str, Any] | None = None,
    role_loader: RoleLoader = _load_role,
    now: Callable[[], str] = _now,
) -> tuple[Path, Path]:
    """Produce immutable analysis and its fixed-schema support projection."""

    exact_analysis = _ensure_output_path(analysis_output, "support analysis output")
    exact_result = _ensure_output_path(result_output, "support result output")
    if exact_analysis == exact_result:
        raise InterventionSupportAnalysisError("analysis and result outputs must be distinct")
    change_plan_file = _file_evidence(change_plan_path, "intervention change plan")
    change_plan = _read_json(change_plan_path, "intervention change plan")
    try:
        plan = validate_intervention_change_plan(change_plan)
    except SelectedStateQualificationError as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    expected_roles = plan.expected_x_roles
    if set(x_manifest_paths) != set(expected_roles):
        raise InterventionSupportAnalysisError(
            "support manifests differ from the predeclared X execution branch"
        )
    manifest_files = {
        role: _file_evidence(x_manifest_paths[role], f"X {role} manifest")
        for role in expected_roles
    }
    loaded = {
        role: role_loader(
            role=role,
            manifest_path=Path(manifest_files[role]["path"]),
            expected_plan_file=change_plan["x_run_plans"][role]["plan_file"],
        )
        for role in expected_roles
    }
    source_identities = [dict(loaded[role].source_identity) for role in expected_roles]
    if not source_identities or any(item != source_identities[0] for item in source_identities[1:]):
        raise InterventionSupportAnalysisError(
            "X roles differ in Smateway/dependency/native/selector source identity"
        )
    current_runtime = _validate_runtime_bindings(
        runtime_bindings if runtime_bindings is not None else _local_runtime_bindings(),
        expected_source_identity=source_identities[0],
    )
    cohorts = {role: loaded[role].repeats for role in expected_roles}
    try:
        qualification = qualify_intervention_support(cohorts)
    except InterventionSupportError as error:
        raise InterventionSupportAnalysisError(str(error)) from error
    if _file_evidence(change_plan_path, "intervention change plan") != change_plan_file:
        raise InterventionSupportAnalysisError("intervention change plan changed during analysis")
    for role in expected_roles:
        if _file_evidence(x_manifest_paths[role], f"X {role} manifest") != manifest_files[role]:
            raise InterventionSupportAnalysisError(f"X {role} manifest changed during analysis")
    manifest_hashes = {role: manifest_files[role]["sha256"] for role in expected_roles}
    normalized_repeats = {
        role: [_json_safe(asdict(item)) for item in cohorts[role]] for role in expected_roles
    }
    qualification_document = _json_safe(asdict(qualification))
    input_identity = {
        "change_plan_file": change_plan_file,
        "x_run_manifest_files": manifest_files,
        "x_run_source_identity": source_identities[0],
        "analysis_runtime_identity": {
            "source_commit": current_runtime["source_commit"],
            "source_files_sha256": current_runtime["source"]["source_files_sha256"],
            "dependency_commit": current_runtime["dependency_commit"],
            "dependency_attestation_sha256": canonical_sha256(current_runtime["dependency"]),
            "native_attestation_sha256": current_runtime["native_attestation_sha256"],
        },
    }
    analysis_document = {
        "schema": 1,
        "analysis_kind": INTERVENTION_SUPPORT_ANALYSIS_KIND,
        "created_at": now(),
        "contract_id": plan.contract_id,
        "change_plan_file": change_plan_file,
        "x_run_manifest_files": manifest_files,
        "x_run_manifest_sha256s": manifest_hashes,
        "x_run_source_identity": source_identities[0],
        "analysis_runtime": current_runtime,
        "normalized_repeats": normalized_repeats,
        "qualification": qualification_document,
        "input_identity_sha256": canonical_sha256(input_identity),
    }
    _write_new_json(exact_analysis, analysis_document)
    passed = qualification.simultaneous_improvement_gate_passed
    result_document = {
        "schema": 1,
        "result_kind": INTERVENTION_SUPPORT_RESULT_KIND,
        "contract_id": plan.contract_id,
        "decision": "supported_fix" if passed else "unsupported",
        "accepted": passed,
        "x_run_manifest_sha256s": manifest_hashes,
        "analysis_file": _file_evidence(exact_analysis, "intervention support analysis"),
        "simultaneous_improvement_gate_passed": passed,
        "rejection_reasons": list(qualification.rejection_reasons),
    }
    _write_new_json(exact_result, result_document)
    return exact_analysis, exact_result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--change-plan", type=Path, required=True)
    parser.add_argument("--boundary-baseline-manifest", type=Path)
    parser.add_argument("--boundary-intervention-manifest", type=Path)
    parser.add_argument("--full-fixture-baseline-manifest", type=Path, required=True)
    parser.add_argument("--full-fixture-intervention-manifest", type=Path, required=True)
    parser.add_argument("--analysis-output", type=Path, required=True)
    parser.add_argument("--result-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifests = {
        role: path
        for role, path in (
            ("boundary_baseline", args.boundary_baseline_manifest),
            ("boundary_intervention", args.boundary_intervention_manifest),
            ("full_fixture_baseline", args.full_fixture_baseline_manifest),
            ("full_fixture_intervention", args.full_fixture_intervention_manifest),
        )
        if path is not None
    }
    try:
        analysis, result = produce_intervention_support_result(
            change_plan_path=args.change_plan,
            x_manifest_paths=manifests,
            analysis_output=args.analysis_output,
            result_output=args.result_output,
        )
    except (
        InterventionSupportAnalysisError,
        FileArtifactAdmissionError,
        OSError,
        ValueError,
    ) as error:
        print(f"intervention-support analysis failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "complete",
                "analysis_path": str(analysis),
                "analysis_sha256": sha256_path(analysis),
                "result_path": str(result),
                "result_sha256": sha256_path(result),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
