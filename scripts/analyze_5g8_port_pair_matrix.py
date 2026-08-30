#!/usr/bin/env python3
"""Re-admit and aggregate the protected 20-run 5.8-GHz TX/RX matrix."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE = _REPOSITORY / "src"
_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.executable).absolute() != _PINNED_PYTHON
    or Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned analysis Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SOURCE) if not prior_pythonpath else f"{_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    loader_entries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(_REQUIRED_LIBIIO_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

import numpy as np

from smateway import global_ledger

from smateway.file_artifact_admission import (  # noqa: E402
    FileArtifactAdmissionError,
    admit_dual_rx_ci16_artifact,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
    read_json_file,
    verify_file_binding,
    verify_source_tree_binding,
)
from smateway.hexcal import (  # noqa: E402
    attest_pluto_plus_utils_source,
    canonical_json_sha256,
    sha256_path,
)
from smateway.leakage_ladder import analyze_coherent_leakage  # noqa: E402
from smateway.native_iio_attestation import (  # noqa: E402
    attestation_sha256,
    attest_runtime,
    validate_runtime_attestation,
)
from smateway.port_pair_matrix import (  # noqa: E402
    BANDWIDTH_HZ,
    CAPTURE_TX_GAIN_DB,
    CELL_IDS,
    CENTER_FREQUENCY_HZ,
    RECEIVER_GAIN_DB,
    REPEAT_COUNT,
    SAMPLE_RATE_HZ,
    TONE_OFFSET_HZ,
    HeadroomPreflight,
    PortPairMatrixError,
    PortPairRepeat,
    analyze_port_pair_matrix,
    canonical_sha256,
    evaluate_headroom_preflight,
    port_pair_repeat_from_observation,
    validate_calibration,
    validate_fixture,
)

OUTPUT_KIND = "smateway.5g8.verified-port-pair-matrix/v1"
_RUNNER: Any | None = None


class PortPairAnalysisError(RuntimeError):
    """A condition or campaign cannot support the protected matrix result."""


def _runner() -> Any:
    global _RUNNER
    if _RUNNER is None:
        path = Path(__file__).with_name("run_5g8_port_pair_matrix.py")
        spec = importlib.util.spec_from_file_location("smateway_port_pair_run_verifier", path)
        if spec is None or spec.loader is None:
            raise PortPairAnalysisError("cannot load the port-pair capture verifier")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except (ImportError, SyntaxError) as error:
            sys.modules.pop(spec.name, None)
            raise PortPairAnalysisError(
                f"cannot load port-pair capture verifier: {error}"
            ) from error
        _RUNNER = module
    return _RUNNER


def _parse_condition(value: str) -> tuple[Path, Path]:
    raw = value.split(",")
    if len(raw) != 2:
        raise PortPairAnalysisError("--condition must use /absolute/plan.json,/manifest.json")
    plan, manifest = (Path(item).expanduser() for item in raw)
    if not plan.is_absolute() or not manifest.is_absolute():
        raise PortPairAnalysisError("port-pair plan and manifest paths must be absolute")
    return plan, manifest


def _settings(metadata: Mapping[str, Any], contract: Mapping[str, Any], label: str) -> None:
    captures = metadata.get("captures")
    global_section = metadata.get("global")
    configuration = contract.get("configuration")
    if (
        not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
        or not isinstance(global_section, Mapping)
        or not isinstance(configuration, Mapping)
    ):
        raise PortPairAnalysisError(f"{label} SigMF identity/settings are malformed")
    observed = captures[0].get("settings")
    expected = {
        "bandwidth_hz": float(BANDWIDTH_HZ),
        "center_frequency_hz": float(CENTER_FREQUENCY_HZ),
        "channels": [0, 1],
        "gain_db": float(RECEIVER_GAIN_DB),
        "gain_mode": "manual",
        "sample_rate_hz": float(SAMPLE_RATE_HZ),
    }
    radio = global_section.get("pluto:radio")
    if (
        observed != expected
        or not isinstance(radio, Mapping)
        or radio.get("serial") != configuration.get("serial")
        or radio.get("uri") != configuration.get("uri")
    ):
        raise PortPairAnalysisError(f"{label} SigMF radio/settings differ from the plan")


def _headroom(samples: np.ndarray) -> tuple[tuple[float, float], tuple[int, int]]:
    peaks: list[float] = []
    clipped: list[int] = []
    for receiver in range(2):
        values = samples[receiver]
        peaks.append(max(float(np.max(np.abs(values.real))), float(np.max(np.abs(values.imag)))))
        clipped.append(
            int(
                np.count_nonzero(
                    (np.abs(values.real) >= 2_047.0) | (np.abs(values.imag) >= 2_047.0)
                )
            )
        )
    return (peaks[0], peaks[1]), (clipped[0], clipped[1])


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def _reanalyze_main(samples: np.ndarray, contract: Mapping[str, Any]) -> dict[str, Any]:
    condition = contract.get("condition")
    if not isinstance(condition, Mapping):
        raise PortPairAnalysisError("port-pair condition is malformed")
    test_index = 0 if condition.get("test_receiver") == "RX1" else 1
    reference_index = 0 if condition.get("reference_receiver") == "RX1" else 1
    analysis = analyze_coherent_leakage(
        samples[reference_index],
        samples[test_index],
        sample_rate_hz=SAMPLE_RATE_HZ,
        tone_offset_hz=TONE_OFFSET_HZ,
        block_duration_s=0.1,
        minimum_block_count=3,
    )
    if not analysis.quality_passed or analysis.rx1.tone_to_noise_snr_db < 20.0:
        raise PortPairAnalysisError("recomputed port-pair main capture quality failed")
    reference = analysis.rx1.phasor
    if reference is None:
        raise PortPairAnalysisError("recomputed conducted reference phasor is unavailable")
    transfer = analysis.rx2_over_rx1
    detected = transfer.phasor is not None and analysis.rx2.tone_detected
    if detected:
        test_upper_bound = None
    else:
        amplitude_upper_bound = transfer.amplitude_upper_bound_ratio
        if amplitude_upper_bound is None:
            raise PortPairAnalysisError("recomputed nondetection lacks a phase-free bound")
        test_upper_bound = float(amplitude_upper_bound) * abs(reference)
    return {
        "test_receiver_tone_detected": detected,
        "test_receiver_tone": _complex_document(analysis.rx2.phasor) if detected else None,
        "test_receiver_tone_magnitude_upper_bound": test_upper_bound,
        "reference_receiver_tone": _complex_document(reference),
        "reference_tone_snr_db": analysis.rx1.tone_to_noise_snr_db,
        "raw_channel_amplitudes_comparable": False,
        "normalization_required": True,
        "analysis": _runner()._json_safe(asdict(analysis)),
    }


CurrentExecutionBoundary = Callable[[], Mapping[str, Any]]


def _attest_current_execution() -> dict[str, Any]:
    """Freshly attest the interpreter, dependency imports, and native libiio."""

    executable = Path(sys.executable).absolute()
    prefix = Path(sys.prefix).resolve()
    loaders = tuple(
        Path(item).resolve()
        for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item
    )
    try:
        dependency = attest_pluto_plus_utils_source()
        native = validate_runtime_attestation(attest_runtime())
    except Exception as error:
        raise PortPairAnalysisError(
            f"cannot freshly attest analyzer dependency/native closure: {error}"
        ) from error
    return {
        "schema": 1,
        "evidence_kind": "5g8_port_pair_current_analysis_execution_v1",
        "python_executable": str(executable),
        "python_prefix": str(prefix),
        "loader_search_path_first": str(loaders[0]) if loaders else None,
        "pluto_plus_utils": dependency,
        "native_libiio": native,
    }


def _verify_current_execution(
    contract: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
) -> None:
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise PortPairAnalysisError("port-pair source contract is missing")
    dependency = source.get("pluto_plus_utils")
    native = source.get("native_libiio")
    if not isinstance(dependency, Mapping) or not isinstance(native, Mapping):
        raise PortPairAnalysisError("port-pair frozen dependency/native identity is malformed")
    if (
        current.get("schema") != 1
        or current.get("evidence_kind") != "5g8_port_pair_current_analysis_execution_v1"
        or current.get("python_executable") != str(_PINNED_PYTHON)
        or current.get("python_prefix") != str(_PINNED_PREFIX)
        or current.get("loader_search_path_first") != str(_REQUIRED_LIBIIO_DIRECTORY)
    ):
        raise PortPairAnalysisError(
            "analyzer is not running under the pinned pluto-plus-utils Python/libiio closure"
        )
    observed_dependency = current.get("pluto_plus_utils")
    observed_native = current.get("native_libiio")
    if not isinstance(observed_dependency, Mapping) or dict(observed_dependency) != dict(
        dependency
    ):
        raise PortPairAnalysisError(
            "current pluto-plus-utils source/import origins differ from the immutable plan"
        )
    try:
        normalized_native = validate_runtime_attestation(observed_native)
    except Exception as error:
        raise PortPairAnalysisError(
            f"current native libiio attestation is invalid: {error}"
        ) from error
    if normalized_native != validate_runtime_attestation(native):
        raise PortPairAnalysisError("current native libiio runtime differs from the immutable plan")


def _verify_source(
    contract: Mapping[str, Any],
    *,
    current_execution_boundary: CurrentExecutionBoundary,
) -> dict[str, Any]:
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise PortPairAnalysisError("port-pair source contract is missing")
    smateway = source.get("smateway")
    dependency = source.get("pluto_plus_utils")
    native = source.get("native_libiio")
    if not all(isinstance(item, Mapping) for item in (smateway, dependency, native)):
        raise PortPairAnalysisError("port-pair source/dependency/native identity is malformed")
    assert isinstance(smateway, Mapping) and isinstance(dependency, Mapping)
    assert isinstance(native, Mapping)
    smateway_files = smateway.get("files")
    dependency_files = dependency.get("files")
    if (
        not isinstance(smateway_files, list)
        or not isinstance(dependency_files, list)
        or not smateway_files
        or not dependency_files
        or smateway.get("source_files_sha256") != canonical_json_sha256(smateway_files)
        or source.get("native_libiio_sha256")
        != attestation_sha256(validate_runtime_attestation(native))
    ):
        raise PortPairAnalysisError("port-pair source/native hash identity is inconsistent")
    verify_source_tree_binding(
        smateway,
        label="port-pair Smateway",
        required_relative_paths=tuple(_runner().SOURCE_FILES),
    )
    verify_source_tree_binding(dependency, label="port-pair pluto-plus-utils")
    try:
        current = current_execution_boundary()
    except PortPairAnalysisError:
        raise
    except Exception as error:
        raise PortPairAnalysisError(
            f"cannot freshly attest current analysis execution: {error}"
        ) from error
    if not isinstance(current, Mapping):
        raise PortPairAnalysisError("current analysis execution attestation is malformed")
    _verify_current_execution(contract, current=current)
    return dict(current)


def _verify_execution_safety_bindings(
    contract: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    observation: Mapping[str, Any],
    result: Mapping[str, Any],
    reservation_receipt: Mapping[str, Any],
    burn_receipt: Mapping[str, Any],
    execution_marker_receipt: Mapping[str, Any],
    attempt_started: Mapping[str, Any],
) -> None:
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PortPairAnalysisError("port-pair configuration identity is malformed")
    identity = record.get("identity_preflight")
    initial_mute = record.get("initial_mute")
    final_mute = record.get("final_mute")
    capture_timeline = record.get("capture_timeline")
    try:
        safety = _runner()._validated_execution_safety(
            identity=identity,
            initial_mute=initial_mute,
            final_mute=final_mute,
            capture_timeline=capture_timeline,
            attempt_started=attempt_started,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_marker_receipt,
        )
    except Exception as error:
        raise PortPairAnalysisError(
            f"port-pair execution safety evidence failed: {error}"
        ) from error
    evidence = safety["evidence"]
    digest = safety["evidence_sha256"]
    if (
        record.get("execution_safety_sha256") != digest
        or record.get("identity_preflight_sha256") != safety["identity_preflight_sha256"]
        or record.get("initial_mute_sha256") != safety["initial_mute_sha256"]
        or record.get("capture_timeline_sha256") != safety["capture_timeline_sha256"]
        or record.get("final_mute_sha256") != safety["final_mute_sha256"]
        or record.get("execution_tombstone") != execution_marker_receipt
        or record.get("execution_tombstone_receipt_sha256")
        != safety["execution_tombstone_receipt_sha256"]
        or observation.get("identity_preflight") != evidence["identity_preflight"]
        or observation.get("initial_mute") != evidence["initial_mute"]
        or observation.get("capture_timeline") != evidence["capture_timeline"]
        or observation.get("final_mute") != evidence["final_mute"]
        or observation.get("execution_tombstone") != execution_marker_receipt
        or observation.get("execution_safety_sha256") != digest
        or observation.get("identity_preflight_sha256") != safety["identity_preflight_sha256"]
        or observation.get("initial_mute_sha256") != safety["initial_mute_sha256"]
        or observation.get("capture_timeline_sha256") != safety["capture_timeline_sha256"]
        or observation.get("final_mute_sha256") != safety["final_mute_sha256"]
        or observation.get("execution_tombstone_receipt_sha256")
        != safety["execution_tombstone_receipt_sha256"]
        or result.get("identity_preflight") != evidence["identity_preflight"]
        or result.get("initial_mute") != evidence["initial_mute"]
        or result.get("capture_timeline") != evidence["capture_timeline"]
        or result.get("final_mute") != evidence["final_mute"]
        or result.get("execution_tombstone") != execution_marker_receipt
        or result.get("execution_safety_sha256") != digest
        or result.get("identity_preflight_sha256") != safety["identity_preflight_sha256"]
        or result.get("initial_mute_sha256") != safety["initial_mute_sha256"]
        or result.get("capture_timeline_sha256") != safety["capture_timeline_sha256"]
        or result.get("final_mute_sha256") != safety["final_mute_sha256"]
        or result.get("execution_tombstone_receipt_sha256")
        != safety["execution_tombstone_receipt_sha256"]
        or record.get("permanent_run_reservation") != reservation_receipt
        or observation.get("permanent_run_reservation") != reservation_receipt
        or result.get("permanent_run_reservation") != reservation_receipt
        or record.get("irreversible_execution_burn") != burn_receipt
        or observation.get("irreversible_execution_burn") != burn_receipt
        or result.get("irreversible_execution_burn") != burn_receipt
    ):
        raise PortPairAnalysisError(
            "port-pair initial identity/mute/final-mute evidence is not recursively cross-bound"
        )


def load_verified_repeat(
    plan_path: Path,
    manifest_path: Path,
    *,
    current_execution_boundary: CurrentExecutionBoundary = _attest_current_execution,
    ledger_backend: global_ledger.LedgerBackend | None = None,
) -> tuple[PortPairRepeat, dict[str, Any]]:
    """Re-admit one two-stream condition entirely from immutable local files."""

    selected_ledger_backend = (
        global_ledger.SudoLedgerBackend() if ledger_backend is None else ledger_backend
    )
    plan_file = assert_no_symlink_chain(plan_path, label="port-pair plan")
    manifest_file = assert_no_symlink_chain(manifest_path, label="port-pair manifest")
    if plan_file.parent != manifest_file.parent or plan_file == manifest_file:
        raise PortPairAnalysisError("port-pair plan and manifest must be distinct siblings")
    plan = read_json_file(plan_file, label="port-pair immutable plan")
    manifest = read_json_file(manifest_file, label="port-pair manifest")
    contract = plan.get("plan_contract")
    if not isinstance(contract, Mapping):
        raise PortPairAnalysisError("port-pair immutable plan contract is missing")
    contract_sha = canonical_sha256(contract)
    if (
        plan.get("schema") != 1
        or plan.get("immutable") is not True
        or plan.get("plan_contract_sha256") != contract_sha
        or contract.get("run_kind") != _runner().RUN_KIND
    ):
        raise PortPairAnalysisError("port-pair immutable plan is inconsistent")
    current_execution = _verify_source(
        contract,
        current_execution_boundary=current_execution_boundary,
    )
    storage = contract.get("storage")
    if (
        not isinstance(storage, Mapping)
        or storage.get("local_rpi_only") is not True
        or storage.get("pluto_storage_forbidden") is not True
        or storage.get("condition_root") != str(plan_file.parent)
        or not isinstance(storage.get("capture_root"), str)
        or not Path(str(storage["capture_root"])).is_absolute()
    ):
        raise PortPairAnalysisError("port-pair local-storage contract is malformed")
    assert_local_rpi_storage(plan_file.parent, label="port-pair condition storage")
    capture_root = assert_local_rpi_storage(
        Path(str(storage["capture_root"])), label="port-pair capture storage"
    )
    assert_no_symlink_chain(capture_root, label="port-pair capture root")
    fixture_contract = contract.get("fixture")
    calibration_contract = contract.get("calibration")
    if not isinstance(fixture_contract, Mapping) or not isinstance(calibration_contract, Mapping):
        raise PortPairAnalysisError("port-pair fixture/calibration bindings are missing")
    fixture_path = verify_file_binding(fixture_contract.get("file"), label="port-pair fixture")
    calibration_path = verify_file_binding(
        calibration_contract.get("file"), label="port-pair calibration"
    )
    fixture_document = read_json_file(fixture_path, label="port-pair fixture")
    fixture = validate_fixture(fixture_document)
    calibration_document = read_json_file(calibration_path, label="port-pair calibration")
    calibration = validate_calibration(calibration_document, fixture)
    if (
        fixture_contract.get("identity_sha256") != fixture.fixture_sha256
        or calibration_contract.get("identity_sha256") != calibration.calibration_sha256
    ):
        raise PortPairAnalysisError("port-pair fixture/calibration identity is stale")
    campaign = contract.get("campaign_plan")
    condition = contract.get("condition")
    if not isinstance(campaign, Mapping) or not isinstance(condition, Mapping):
        raise PortPairAnalysisError("port-pair campaign/condition contract is missing")
    campaign_contract = campaign.get("contract")
    source = contract["source"]
    assert isinstance(source, Mapping)
    smateway_source = source["smateway"]
    dependency_source = source["pluto_plus_utils"]
    assert isinstance(smateway_source, Mapping) and isinstance(dependency_source, Mapping)
    if (
        not isinstance(campaign_contract, Mapping)
        or campaign.get("sha256") != canonical_sha256(campaign_contract)
        or campaign_contract.get("condition_count") != 20
        or campaign_contract.get("conditions")
        != [
            {
                "cell_id": cell.cell_id,
                "repeat_index": repeat,
                "topology_sha256": cell.topology_sha256,
                "topology_token": cell.topology_token,
            }
            for cell in fixture.cells
            for repeat in range(1, REPEAT_COUNT + 1)
        ]
        or campaign_contract.get("source_commit") != smateway_source.get("commit")
        or campaign_contract.get("source_files_sha256")
        != smateway_source.get("source_files_sha256")
        or campaign_contract.get("dependency_commit") != dependency_source.get("commit")
        or campaign_contract.get("dependency_attestation_sha256")
        != canonical_sha256(dependency_source)
        or campaign_contract.get("native_libiio_sha256") != source.get("native_libiio_sha256")
        or campaign_contract.get("local_rpi_only") is not True
    ):
        raise PortPairAnalysisError("port-pair campaign plan is not the exact 20-cell matrix")
    cell = fixture.cell(str(condition.get("cell_id")))
    if (
        condition.get("topology_sha256") != cell.topology_sha256
        or condition.get("topology_token") != cell.topology_token
        or condition.get("active_tx") != cell.active_tx
        or condition.get("inactive_tx") != cell.inactive_tx
        or condition.get("test_receiver") != cell.test_receiver
        or condition.get("reference_receiver") != cell.reference_receiver
    ):
        raise PortPairAnalysisError("port-pair condition differs from its fixture cell")
    attempts = manifest.get("attempts")
    result = manifest.get("result")
    plan_binding = manifest.get("plan")
    if (
        manifest.get("schema") != 1
        or manifest.get("run_kind") != _runner().RUN_KIND
        or manifest.get("status") != "complete"
        or manifest.get("accepted_stream_count") != 2
        or manifest.get("error") is not None
        or manifest.get("run_id") != contract.get("run_id")
        or manifest.get("campaign_id") != contract.get("campaign_id")
        or manifest.get("cell_id") != condition.get("cell_id")
        or manifest.get("repeat_index") != condition.get("repeat_index")
        or not isinstance(plan_binding, Mapping)
        or plan_binding.get("path") != str(plan_file)
        or plan_binding.get("sha256") != sha256_path(plan_file)
        or plan_binding.get("contract_sha256") != contract_sha
        or not isinstance(attempts, list)
        or len(attempts) != 1
        or not isinstance(attempts[0], Mapping)
        or attempts[0].get("status") != "complete"
        or attempts[0].get("error") is not None
        or not isinstance(result, Mapping)
        or attempts[0].get("result") != result
    ):
        raise PortPairAnalysisError("port-pair manifest is not one accepted two-stream run")
    attempt = attempts[0]
    assert isinstance(attempt, Mapping)
    expected_attempt_keys = {
        "started_at",
        "started_monotonic_ns",
        "started_clock_boot_id",
        "completed_at",
        "completed_monotonic_ns",
        "completed_clock_boot_id",
        "status",
        "confirmations",
        "permanent_run_reservation",
        "irreversible_execution_burn",
        "execution_tombstone",
        "result",
        "error",
    }
    if set(attempt) != expected_attempt_keys:
        raise PortPairAnalysisError("port-pair attempt fields are incomplete or unexpected")
    attempt_started = {
        "started_at": attempt.get("started_at"),
        "started_monotonic_ns": attempt.get("started_monotonic_ns"),
        "started_clock_boot_id": attempt.get("started_clock_boot_id"),
    }
    attempt_completed = {
        "completed_at": attempt.get("completed_at"),
        "completed_monotonic_ns": attempt.get("completed_monotonic_ns"),
        "completed_clock_boot_id": attempt.get("completed_clock_boot_id"),
    }
    try:
        _runner()._clock_point(attempt_started, prefix="started", label="attempt")
        _runner()._clock_point(attempt_completed, prefix="completed", label="attempt")
    except Exception as error:
        raise PortPairAnalysisError(f"port-pair attempt timing is invalid: {error}") from error
    reservation_value = attempt.get("permanent_run_reservation")
    burn_value = attempt.get("irreversible_execution_burn")
    if not isinstance(reservation_value, Mapping) or not isinstance(burn_value, Mapping):
        raise PortPairAnalysisError("port-pair external execution receipts are missing")
    try:
        reservation_receipt = _runner()._validate_reservation_receipt(
            contract,
            plan_path=plan_file,
            manifest_path=manifest_file,
            ledger_backend=selected_ledger_backend,
            receipt=reservation_value,
        )
        burn_receipt = _runner()._validate_execution_burn_receipt(
            contract,
            plan_path=plan_file,
            manifest_path=manifest_file,
            reservation_receipt=reservation_receipt,
            ledger_backend=selected_ledger_backend,
            receipt=burn_value,
        )
    except Exception as error:
        raise PortPairAnalysisError(
            f"port-pair external execution reservation/burn failed: {error}"
        ) from error
    confirmations = attempt.get("confirmations")
    required_confirmation_fields = (
        "no_antennas",
        "inactive_tx_physically_terminated",
        "test_receiver_terminated",
        "rx1_protection_unchanged",
        "separate_reference_attenuator",
        "reference_planes_match",
        "no_movement",
    )
    if (
        not isinstance(confirmations, Mapping)
        or confirmations.get("topology_token") != cell.topology_token
        or any(confirmations.get(name) is not True for name in required_confirmation_fields)
    ):
        raise PortPairAnalysisError("port-pair physical confirmations are incomplete")
    failure_path = manifest_file.parent / _runner().FAILURE_TOMBSTONE_FILENAME
    if failure_path.exists() or failure_path.is_symlink():
        raise PortPairAnalysisError("port-pair run has a failure tombstone")
    execution_binding = attempt.get("execution_tombstone")
    try:
        execution_receipt = _runner()._validate_execution_tombstone_receipt(
            contract,
            plan_path=plan_file,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            attempt_started=attempt_started,
            receipt=execution_binding,
        )
    except Exception as error:
        raise PortPairAnalysisError(
            f"port-pair execution tombstone is inconsistent: {error}"
        ) from error
    try:
        _runner()._validate_completed_attempt_timeline(
            attempt=attempt,
            result=result,
            contract=contract,
            reservation_receipt=reservation_receipt,
            burn_receipt=burn_receipt,
            execution_marker_receipt=execution_receipt,
        )
    except Exception as error:
        raise PortPairAnalysisError(
            f"port-pair complete execution timeline is invalid: {error}"
        ) from error
    observation_path = Path(str(result.get("observation_path", ""))).expanduser().absolute()
    record_path = Path(str(result.get("condition_record_path", ""))).expanduser().absolute()
    expected_observation_path = plan_file.parent / _runner().OBSERVATION_FILENAME
    expected_record_path = plan_file.parent / _runner().CONDITION_RECORD_FILENAME
    if observation_path != expected_observation_path or record_path != expected_record_path:
        raise PortPairAnalysisError("port-pair result paths escape the exact condition root")
    for path, label in (
        (observation_path, "port-pair normalized observation"),
        (record_path, "port-pair condition record"),
    ):
        assert_no_symlink_chain(path, label=label)
        assert_local_rpi_storage(path, label=f"{label} storage")
    observation = read_json_file(observation_path, label="port-pair normalized observation")
    record = read_json_file(record_path, label="port-pair condition record")
    if (
        result.get("observation_sha256") != sha256_path(observation_path)
        or result.get("condition_record_sha256") != sha256_path(record_path)
        or record.get("schema") != 1
        or record.get("record_kind") != "5g8_protected_port_pair_condition_record"
        or record.get("plan_contract_sha256") != contract_sha
        or record.get("campaign_plan_sha256") != campaign.get("sha256")
        or record.get("condition") != condition
        or record.get("fixture") != fixture_contract
        or record.get("calibration") != calibration_contract
    ):
        raise PortPairAnalysisError("port-pair record does not bind its immutable plan")
    if (
        observation.get("plan_contract_sha256") != contract_sha
        or observation.get("campaign_plan_sha256") != campaign.get("sha256")
        or observation.get("fixture_sha256") != fixture.fixture_sha256
        or observation.get("calibration_sha256") != calibration.calibration_sha256
        or observation.get("topology_sha256") != cell.topology_sha256
        or observation.get("source_commit") != smateway_source.get("commit")
        or observation.get("dependency_commit") != dependency_source.get("commit")
        or observation.get("native_attestation_sha256") != source.get("native_libiio_sha256")
    ):
        raise PortPairAnalysisError("port-pair normalized identity differs from plan")
    _verify_execution_safety_bindings(
        contract,
        record=record,
        observation=observation,
        result=result,
        reservation_receipt=reservation_receipt,
        burn_receipt=burn_receipt,
        execution_marker_receipt=execution_receipt,
        attempt_started=attempt_started,
    )
    preflight_record = record.get("preflight")
    main_record = record.get("main")
    preflight_observation = observation.get("preflight")
    main_observation = observation.get("main")
    if not all(
        isinstance(item, Mapping)
        for item in (preflight_record, main_record, preflight_observation, main_observation)
    ):
        raise PortPairAnalysisError("port-pair capture evidence is malformed")
    assert isinstance(preflight_record, Mapping) and isinstance(main_record, Mapping)
    assert isinstance(preflight_observation, Mapping) and isinstance(main_observation, Mapping)
    for name, record_capture, normalized_capture, samples_per_frame, frame_count in (
        (
            "preflight",
            preflight_record,
            preflight_observation,
            _runner().PREFLIGHT_SAMPLES_PER_FRAME,
            _runner().PREFLIGHT_FRAME_COUNT,
        ),
        (
            "main",
            main_record,
            main_observation,
            _runner().MAIN_SAMPLES_PER_FRAME,
            _runner().MAIN_FRAME_COUNT,
        ),
    ):
        evidence = record_capture.get("evidence")
        if (
            not isinstance(evidence, Mapping)
            or normalized_capture.get("artifact") != evidence
            or result.get(f"{name}_artifact") != evidence
        ):
            raise PortPairAnalysisError(f"port-pair {name} artifact bindings differ")
        samples, continuity, raw_path, metadata_path = admit_dual_rx_ci16_artifact(
            evidence,
            label=f"port-pair {name}",
            expected_sample_count=samples_per_frame * frame_count,
            expected_samples_per_block=samples_per_frame,
            expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
            expected_stream_id=normalized_capture.get("stream_id"),
            expected_artifact_id=str(evidence.get("artifact_id")),
        )
        expected_capture_root = capture_root / name
        if (
            raw_path.parent != expected_capture_root / str(evidence.get("artifact_id"))
            or metadata_path.parent != raw_path.parent
        ):
            raise PortPairAnalysisError(
                f"port-pair {name} artifact is outside its local capture root"
            )
        metadata = read_json_file(metadata_path, label=f"port-pair {name} SigMF metadata")
        _settings(metadata, contract, f"port-pair {name}")
        global_section = metadata.get("global")
        persisted_ledger = metadata.get("pluto:continuity")
        if persisted_ledger is None and isinstance(global_section, Mapping):
            persisted_ledger = global_section.get("pluto:continuity")
        if (
            record_capture.get("stream_id") != continuity.get("stream_id")
            or record_capture.get("continuity_ledger") != persisted_ledger
            or normalized_capture.get("continuity_passed") is not True
        ):
            raise PortPairAnalysisError(f"port-pair {name} live/persisted ABI2 ledgers differ")
        if name == "preflight":
            peaks, clipped = _headroom(samples)
            headroom = HeadroomPreflight(
                preflight_tx_gain_db=float(CAPTURE_TX_GAIN_DB - 20.0),
                capture_tx_gain_db=float(CAPTURE_TX_GAIN_DB),
                clip_threshold_abs_counts=2_047.0,
                peak_abs_counts_by_receiver=peaks,
                clipped_sample_count_by_receiver=clipped,
            )
            expected_headroom = {
                "input": _runner()._json_safe(asdict(headroom)),
                "admission": _runner()._json_safe(asdict(evaluate_headroom_preflight(headroom))),
            }
            if (
                record.get("headroom_preflight") != expected_headroom
                or normalized_capture.get("headroom") != expected_headroom
            ):
                raise PortPairAnalysisError("port-pair headroom result differs from raw IQ")
        else:
            recomputed = _reanalyze_main(samples, contract)
            if (
                main_record.get("analysis") != recomputed
                or main_observation.get("analysis") != recomputed
                or main_observation.get("clipped_sample_count_by_receiver")
                != list(_headroom(samples)[1])
            ):
                raise PortPairAnalysisError("port-pair main analysis differs from raw IQ")
        del samples
    if preflight_observation.get("condition_record_sha256") != sha256_path(record_path) or (
        main_observation.get("condition_record_sha256") != sha256_path(record_path)
    ):
        raise PortPairAnalysisError("port-pair normalized streams bind another condition record")
    repeat = port_pair_repeat_from_observation(observation)
    return repeat, {
        "campaign_plan": dict(campaign),
        "fixture_document": fixture_document,
        "calibration_document": calibration_document,
        "run_id": contract["run_id"],
        "cell_id": condition["cell_id"],
        "repeat_index": condition["repeat_index"],
        "plan_path": str(plan_file),
        "plan_sha256": sha256_path(plan_file),
        "manifest_path": str(manifest_file),
        "manifest_sha256": sha256_path(manifest_file),
        "analysis_execution_attestation": current_execution,
        "analysis_execution_attestation_sha256": canonical_sha256(current_execution),
        "permanent_run_reservation": reservation_receipt,
        "irreversible_execution_burn": burn_receipt,
        "execution_tombstone": execution_receipt,
        "attempt_started": attempt_started,
        "attempt_completed": attempt_completed,
    }


def analyze_conditions(
    paths: Sequence[tuple[Path, Path]],
    *,
    bootstrap_draw_count: int,
    bootstrap_seed: int,
    ledger_backend: global_ledger.LedgerBackend | None = None,
) -> tuple[Any, tuple[dict[str, Any], ...]]:
    if len(paths) != len(CELL_IDS) * REPEAT_COUNT:
        raise PortPairAnalysisError("port-pair matrix requires exactly 20 plan/manifest pairs")
    flattened = [path.absolute() for pair in paths for path in pair]
    if len(set(flattened)) != 40:
        raise PortPairAnalysisError("port-pair campaign reuses a plan or manifest path")
    current_execution = _attest_current_execution()
    loaded = [
        load_verified_repeat(
            plan,
            manifest,
            current_execution_boundary=lambda: current_execution,
            ledger_backend=ledger_backend,
        )
        for plan, manifest in paths
    ]
    repeats = [item[0] for item in loaded]
    evidence = tuple(item[1] for item in loaded)
    expected_order = tuple((cell, repeat) for cell in CELL_IDS for repeat in range(1, 6))
    observed_order = tuple((str(item["cell_id"]), int(item["repeat_index"])) for item in evidence)
    if observed_order != expected_order:
        raise PortPairAnalysisError("conditions must be supplied in exact cell then repeat order")
    campaign = evidence[0]["campaign_plan"]
    fixture_document = evidence[0]["fixture_document"]
    calibration_document = evidence[0]["calibration_document"]
    if any(
        item["campaign_plan"] != campaign
        or item["fixture_document"] != fixture_document
        or item["calibration_document"] != calibration_document
        or item["analysis_execution_attestation"] != evidence[0]["analysis_execution_attestation"]
        for item in evidence[1:]
    ):
        raise PortPairAnalysisError(
            "port-pair conditions do not share one campaign/fixture/calibration"
        )
    fixture = validate_fixture(fixture_document)
    calibration = validate_calibration(calibration_document, fixture)
    result = analyze_port_pair_matrix(
        fixture,
        calibration,
        repeats,
        plan_sha256=str(campaign["sha256"]),
        bootstrap_draw_count=bootstrap_draw_count,
        bootstrap_seed=bootstrap_seed,
    )
    return result, evidence


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    output = assert_no_symlink_chain(path.expanduser().absolute(), label="port-pair output")
    assert_local_rpi_storage(output, label="port-pair output storage")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_no_symlink_chain(output.parent, label="port-pair output parent")
    payload = (
        json.dumps(document, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n"
    ).encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(FileNotFoundError):
            output.unlink()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--condition",
        action="append",
        required=True,
        metavar="/absolute/plan.json,/absolute/manifest.json",
    )
    parser.add_argument("--bootstrap-draws", type=int, default=32_768)
    parser.add_argument("--bootstrap-seed", type=int, default=0x5A8_706)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result, evidence = analyze_conditions(
            tuple(_parse_condition(value) for value in args.condition),
            bootstrap_draw_count=args.bootstrap_draws,
            bootstrap_seed=args.bootstrap_seed,
        )
        output = {
            "schema": 1,
            "analysis_kind": OUTPUT_KIND,
            "condition_order": [
                f"{cell}.repeat-{repeat}" for cell in CELL_IDS for repeat in range(1, 6)
            ],
            "result": asdict(result),
            "analysis_execution_attestation": evidence[0]["analysis_execution_attestation"],
            "analysis_execution_attestation_sha256": evidence[0][
                "analysis_execution_attestation_sha256"
            ],
            "inputs": [
                {
                    key: item[key]
                    for key in (
                        "run_id",
                        "cell_id",
                        "repeat_index",
                        "plan_path",
                        "plan_sha256",
                        "manifest_path",
                        "manifest_sha256",
                        "permanent_run_reservation",
                        "irreversible_execution_burn",
                    )
                }
                for item in evidence
            ],
            "recursive_admission": {
                "plan_manifest_condition_record_raw_iq_metadata_reverified": True,
                "abi2_continuity_reaudited": True,
                "complex_transfer_recomputed_from_raw_iq": True,
                "nondetections_use_phase_free_magnitude_bounds": True,
                "source_native_fixture_calibration_identity_reverified": True,
                "failure_tombstone_accepted": False,
            },
            "analysis_hardware_activity": False,
        }
        _write_new(args.output, output)
    except (
        FileArtifactAdmissionError,
        OSError,
        PortPairAnalysisError,
        PortPairMatrixError,
        ValueError,
    ) as error:
        print(
            json.dumps({"status": "failed", "error": f"{type(error).__name__}: {error}"}),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"status": "complete", "output": str(args.output.absolute()), "repeat_count": 20},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
