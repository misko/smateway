#!/usr/bin/env python3
"""Normalize legacy P0 evidence or compare five P0 with five P2 runs."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import stat
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_PINNED_DEPENDENCY_SOURCE = Path("/home/pi/pluto-plus-utils/src")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SMATEWAY_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")


class CohortAnalysisError(RuntimeError):
    """Legacy or hardened evidence fails the frozen cohort contract."""


_pythonpath_directories = tuple(
    Path(item).resolve() for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item
)
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(os.path.abspath(sys.executable)) != _PINNED_PYTHON
    or Path(os.path.abspath(sys.prefix)) != _PINNED_PREFIX
    or _pythonpath_directories != (_SMATEWAY_SOURCE.resolve(),)
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned analysis Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_SMATEWAY_SOURCE)
    environment["LD_LIBRARY_PATH"] = str(_REQUIRED_LIBIIO_DIRECTORY)
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )


def _reject_preloaded_smateway_modules() -> None:
    preloaded = sorted(
        name for name in sys.modules if name == "smateway" or name.startswith("smateway.")
    )
    if preloaded:
        raise CohortAnalysisError(
            "Smateway modules were preloaded before analyzer path sanitization: "
            + ", ".join(preloaded)
        )


def _sanitized_analysis_sys_path() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    stdlib = Path(paths["stdlib"]).resolve()
    purelib = Path(paths["purelib"]).resolve()
    candidates = (
        _SMATEWAY_SOURCE.resolve(),
        _PINNED_DEPENDENCY_SOURCE.resolve(),
        stdlib.parent / "python311.zip",
        stdlib,
        stdlib / "lib-dynload",
        purelib,
    )
    output: list[str] = []
    for candidate in candidates:
        rendered = str(candidate)
        if rendered not in output:
            output.append(rendered)
    return tuple(output)


_EXPECTED_ANALYSIS_SYS_PATH = _sanitized_analysis_sys_path()
_ANALYZER_PATH_SANITIZED = False
if __name__ == "__main__":
    # This check occurs before the first Smateway import.  A sitecustomize or
    # other ambient preload is therefore a fatal provenance violation.
    _reject_preloaded_smateway_modules()
    sys.path[:] = list(_EXPECTED_ANALYSIS_SYS_PATH)
    _ANALYZER_PATH_SANITIZED = True
elif str(_SMATEWAY_SOURCE) not in sys.path:
    # Unit tests import this module as a library.  They do not perform live
    # analysis, but still resolve repository modules deterministically.
    sys.path.insert(0, str(_SMATEWAY_SOURCE))

import numpy as np
import numpy.typing as npt

from smateway import global_ledger
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
    PLUTO_PLUS_UTILS_IMPORTED_MODULES,
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    sha256_path,
    write_json_atomic,
)
from smateway.input_off_control import (
    BANDWIDTH_HZ,
    CENTER_FREQUENCY_HZ,
    DDS_SCALE,
    DURATION_S,
    EDGE_EXCLUSION_BINS,
    FRAME_COUNT,
    KERNEL_BUFFERS,
    MINIMUM_COMPLETE_FAST20_FRAMES,
    OBSERVATION_KIND,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    TONE_OFFSET_HZ,
    TOTAL_SAMPLES,
    TX_HARDWARE_GAIN_DB,
    InputOffContractError,
    acquisition_contract,
    canonical_sha256,
    compare_p0_p2_cohorts,
    validate_observation,
)
from smateway.native_iio_attestation import (
    attestation_sha256,
    attest_runtime,
    validate_runtime_attestation,
)
from smateway.p0_normalized_evidence import (
    LEGACY_REFERENCE_ANALYSIS_FILENAME,
    P0NormalizedEvidenceError,
    admit_normalized_p0_evidence,
    build_normalized_p0_envelope,
    validate_legacy_p0_execution_identity,
    validate_legacy_p0_plan,
    write_sealed_normalized_p0,
)

MINIMUM_ALIGNMENT_SCORE = 0.75
MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT = 0.75
MINIMUM_REFERENCE_VALID_BIN_FRACTION = 0.95
MINIMUM_RX1_CYCLE_COHERENCE = 0.90
REPOSITORY = _REPOSITORY
FAST20_PROFILE = REPOSITORY / "profiles/fast20-v1/control_profile.json"


_P2_RUNNER: Any | None = None


def _p2_runner() -> Any:
    """Load capture-side pure reanalysis code without invoking its CLI or hardware."""

    global _P2_RUNNER
    if _P2_RUNNER is None:
        path = Path(__file__).with_name("run_5g8_input_off_control.py")
        spec = importlib.util.spec_from_file_location("smateway_p2_capture_verifier", path)
        if spec is None or spec.loader is None:
            raise CohortAnalysisError("cannot load the authoritative P2 verifier")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except (ImportError, SyntaxError) as error:
            sys.modules.pop(spec.name, None)
            raise CohortAnalysisError(f"cannot load P2 verifier: {error}") from error
        _P2_RUNNER = module
    return _P2_RUNNER


def _analysis_runtime_attestation() -> dict[str, Any]:
    """Fail closed unless this is the exact sanitized production interpreter."""

    executable = Path(os.path.abspath(sys.executable))
    prefix = Path(os.path.abspath(sys.prefix))
    observed_path = tuple(sys.path)
    pythonpath = tuple(
        str(Path(item).resolve())
        for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if item
    )
    loader = tuple(
        str(Path(item).resolve())
        for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item
    )
    if (
        not _ANALYZER_PATH_SANITIZED
        or executable != _PINNED_PYTHON
        or prefix != _PINNED_PREFIX
        or observed_path != _EXPECTED_ANALYSIS_SYS_PATH
        or not observed_path
        or Path(observed_path[0]).resolve() != _SMATEWAY_SOURCE.resolve()
        or pythonpath != (str(_SMATEWAY_SOURCE.resolve()),)
        or not loader
        or Path(loader[0]).resolve() != _REQUIRED_LIBIIO_DIRECTORY.resolve()
    ):
        raise CohortAnalysisError(
            "analyzer is not running in the exact pinned executable/path/loader environment"
        )
    return {
        "schema": 1,
        "python_executable": str(executable),
        "python_prefix": str(prefix),
        "sys_path": list(observed_path),
        "smateway_source_first": True,
        "pythonpath": list(pythonpath),
        "ld_library_path": list(loader),
    }


def _attest_smateway_import_origins(source: Mapping[str, Any]) -> dict[str, Any]:
    """Bind every loaded Smateway module to its frozen repository file bytes."""

    repository_value = source.get("repository")
    raw_files = source.get("files")
    if not isinstance(repository_value, str) or not isinstance(raw_files, list):
        raise CohortAnalysisError("Smateway source attestation is malformed")
    repository = Path(repository_value).expanduser().absolute()
    try:
        verify_source_tree_binding(
            source,
            label="current analyzer Smateway",
            required_relative_paths=tuple(_p2_runner().SOURCE_FILES),
        )
    except FileArtifactAdmissionError as error:
        raise CohortAnalysisError(str(error)) from error
    bindings: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_files):
        binding = _mapping(raw, f"Smateway source file {index}")
        relative = binding.get("path")
        if not isinstance(relative, str):
            raise CohortAnalysisError("Smateway source file path is malformed")
        if relative == "src/smateway/__init__.py":
            module_name = "smateway"
        elif relative.startswith("src/smateway/") and relative.endswith(".py"):
            module_name = "smateway." + Path(relative).stem
        else:
            continue
        if module_name in bindings:
            raise CohortAnalysisError(f"duplicate Smateway module binding: {module_name}")
        bindings[module_name] = binding
    loaded = {
        name: module
        for name, module in sys.modules.items()
        if name == "smateway" or name.startswith("smateway.")
    }
    if set(loaded) != set(bindings):
        missing = sorted(set(bindings) - set(loaded))
        unexpected = sorted(set(loaded) - set(bindings))
        raise CohortAnalysisError(
            "loaded Smateway modules differ from frozen source bindings "
            f"(missing={missing}, unexpected={unexpected})"
        )
    modules: list[dict[str, Any]] = []
    for name in sorted(bindings):
        binding = bindings[name]
        relative = str(binding["path"])
        expected_path = (repository / relative).absolute()
        module = loaded[name]
        spec = getattr(module, "__spec__", None)
        file_value = getattr(module, "__file__", None)
        origin_value = getattr(spec, "origin", None)
        if (
            not isinstance(file_value, str)
            or not isinstance(origin_value, str)
            or Path(file_value).absolute() != expected_path
            or Path(origin_value).absolute() != expected_path
            or expected_path.is_symlink()
            or not expected_path.is_file()
            or expected_path.stat().st_size != binding.get("size_bytes")
            or sha256_path(expected_path) != binding.get("sha256")
        ):
            raise CohortAnalysisError(
                f"loaded Smateway module {name} does not originate from its frozen binding"
            )
        modules.append(
            {
                "module": name,
                "relative_path": relative,
                "origin": str(expected_path),
                "sha256": binding["sha256"],
                "size_bytes": binding["size_bytes"],
            }
        )
    return {
        "schema": 1,
        "repository": str(repository),
        "commit": source.get("commit"),
        "source_files_sha256": source.get("source_files_sha256"),
        "modules": modules,
        "modules_sha256": canonical_json_sha256(modules),
    }


def _current_analysis_attestations() -> tuple[dict[str, Any], dict[str, Any]]:
    """Attest the exact dependency imports and native library mapped by this analyzer."""

    _analysis_runtime_attestation()
    # Loading the capture verifier first imports the same pluto-plus-utils surface
    # whose origins are then checked against the pinned clean checkout.
    _p2_runner()
    dependency = attest_pluto_plus_utils_source(imported_modules=PLUTO_PLUS_UTILS_IMPORTED_MODULES)
    native = validate_runtime_attestation(attest_runtime())
    return dependency, native


def _normalizer_source_with_runtime(
    dependency: Mapping[str, Any], native: Mapping[str, Any]
) -> dict[str, Any]:
    source = dict(_p2_runner()._repository_source_attestation())
    runtime = _analysis_runtime_attestation()
    imports = _attest_smateway_import_origins(source)
    source.update(
        {
            "analyzer_runtime_attestation": runtime,
            "analyzer_runtime_attestation_sha256": canonical_json_sha256(runtime),
            "smateway_import_origin_attestation": imports,
            "smateway_import_origin_attestation_sha256": canonical_json_sha256(imports),
            "pluto_plus_utils_source_attestation": dict(dependency),
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
            "native_libiio_runtime_attestation": dict(native),
            "native_libiio_runtime_attestation_sha256": attestation_sha256(native),
        }
    )
    return source


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        return read_json_file(path, label=label)
    except FileArtifactAdmissionError as error:
        raise CohortAnalysisError(str(error)) from error


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CohortAnalysisError(f"{label} must be an object")
    return value


def _passed_mute(value: object, purpose: str | None = None) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "passed"
        and value.get("error") is None
        and (purpose is None or value.get("purpose") == purpose)
    )


def _legacy_attempt(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    artifact_id: str,
    test_only_legacy_boards_root: Path | None = None,
) -> Mapping[str, Any]:
    try:
        _configuration, _condition, attempt = validate_legacy_p0_execution_identity(
            manifest,
            run_id=run_id,
            artifact_id=artifact_id,
            expected_repository=REPOSITORY,
            test_only_legacy_boards_root=test_only_legacy_boards_root,
        )
    except (FileArtifactAdmissionError, P0NormalizedEvidenceError) as error:
        raise CohortAnalysisError(str(error)) from error
    return attempt


def _meta_and_paths(
    analysis: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    analysis_path: Path,
    attempt: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, Path]:
    artifact = _mapping(analysis.get("artifact"), "legacy P0 artifact")
    root_value = artifact.get("path")
    artifact_id = artifact.get("artifact_id")
    configuration = _mapping(manifest.get("configuration"), "legacy P0 configuration")
    storage_value = configuration.get("artifact_storage_root")
    if (
        not isinstance(root_value, str)
        or not Path(root_value).is_absolute()
        or not isinstance(artifact_id, str)
        or not isinstance(storage_value, str)
        or not Path(storage_value).is_absolute()
    ):
        raise CohortAnalysisError("legacy P0 artifact root/ID is invalid")
    try:
        storage_root = assert_no_symlink_chain(
            Path(storage_value), label="legacy P0 artifact-storage root"
        )
        root = assert_no_symlink_chain(Path(root_value), label="legacy P0 artifact root")
        exact_analysis = assert_no_symlink_chain(
            analysis_path.expanduser().absolute(), label="legacy P0 reference analysis"
        )
        assert_local_rpi_storage(storage_root, label="legacy P0 artifact storage")
        assert_local_rpi_storage(root, label="legacy P0 artifact-root storage")
        assert_local_rpi_storage(exact_analysis, label="legacy P0 analysis storage")
    except FileArtifactAdmissionError as error:
        raise CohortAnalysisError(str(error)) from error
    if (
        not storage_root.is_dir()
        or root != storage_root / artifact_id
        or not root.is_dir()
        or exact_analysis != root / LEGACY_REFERENCE_ANALYSIS_FILENAME
    ):
        raise CohortAnalysisError(
            "legacy P0 artifact root/analysis is outside its exact local storage identity"
        )
    quality = _mapping(attempt.get("quality_result"), "legacy P0 quality result")
    artifact_identity = _mapping(attempt.get("artifact_identity"), "legacy P0 artifact identity")
    if (
        quality.get("analysis_path") != str(exact_analysis)
        or quality.get("artifact_path") != str(root)
        or artifact_identity.get("artifact_id") != artifact_id
        or artifact_identity.get("path") != str(root)
    ):
        raise CohortAnalysisError(
            "legacy P0 attempt does not bind the exact artifact root/analysis identity"
        )
    data_path = root / f"{artifact_id}.sigmf-data"
    metadata_path = root / f"{artifact_id}.sigmf-meta"
    try:
        data_path = assert_no_symlink_chain(data_path, label="legacy P0 raw IQ")
        metadata_path = assert_no_symlink_chain(metadata_path, label="legacy P0 SigMF metadata")
        assert_local_rpi_storage(data_path, label="legacy P0 raw-IQ storage")
        assert_local_rpi_storage(metadata_path, label="legacy P0 metadata storage")
    except FileArtifactAdmissionError as error:
        raise CohortAnalysisError(str(error)) from error
    if any(not path.is_file() for path in (data_path, metadata_path)):
        raise CohortAnalysisError("legacy P0 SigMF files are missing or symlinks")
    metadata = _read_json(metadata_path, "legacy P0 SigMF metadata")
    return metadata, data_path, metadata_path


def _load_dual_ci16(data_path: Path, *, sample_count: int) -> npt.NDArray[np.complex64]:
    raw: npt.NDArray[np.int16] = np.memmap(data_path, dtype="<i2", mode="r")
    expected_components = sample_count * 2 * 2
    if raw.size != expected_components:
        raise CohortAnalysisError("legacy P0 data size is not dual-RX CI16")
    components = raw.reshape(sample_count, 2, 2)
    samples: npt.NDArray[np.complex64] = np.empty((2, sample_count), dtype=np.complex64)
    for start in range(0, sample_count, 1_000_000):
        stop = min(sample_count, start + 1_000_000)
        samples[:, start:stop].real = components[start:stop, :, 0].T
        samples[:, start:stop].imag = components[start:stop, :, 1].T
    return samples


def _legacy_rotation0_plan_row(
    manifest: Mapping[str, Any], *, test_only_legacy_boards_root: Path | None = None
) -> Mapping[str, Any]:
    try:
        _configuration, row = validate_legacy_p0_plan(
            manifest,
            expected_repository=REPOSITORY,
            test_only_legacy_boards_root=test_only_legacy_boards_root,
        )
    except (FileArtifactAdmissionError, P0NormalizedEvidenceError) as error:
        raise CohortAnalysisError(str(error)) from error
    return row


def _finite_float(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise CohortAnalysisError(f"{label} must be finite")
    return float(value)


def _recompute_legacy_all_off_from_raw(
    data_path: Path,
    *,
    metadata: Mapping[str, Any],
    manifest: Mapping[str, Any],
    test_only_legacy_boards_root: Path | None = None,
) -> dict[str, Any]:
    """Derive every P0 collapse input from IQ, timing metadata, plan, and profile."""

    _legacy_rotation0_plan_row(
        manifest,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    configuration = _mapping(manifest.get("configuration"), "legacy P0 configuration")
    if configuration.get("profile_id") != "fast20-v1":
        raise CohortAnalysisError("legacy P0 manifest does not bind Fast20-v1")
    profile_document = _read_json(FAST20_PROFILE, "current Fast20-v1 profile")
    profile_sha256 = profile_document.get("contract_sha256")
    if profile_sha256 != configuration.get("profile_contract_sha256"):
        raise CohortAnalysisError("current Fast20 profile differs from the legacy P0 binding")
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
    )
    global_section = metadata.get("global")
    persisted = metadata.get("pluto:continuity")
    if persisted is None and isinstance(global_section, Mapping):
        persisted = global_section.get("pluto:continuity")
    ledger = _mapping(persisted, "legacy P0 persisted continuity ledger")
    blocks_document = ledger.get("blocks")
    if (
        ledger.get("stream_id") != continuity.get("stream_id")
        or not isinstance(blocks_document, list)
        or len(blocks_document) != FRAME_COUNT
    ):
        raise CohortAnalysisError("legacy P0 persisted ABI2 timing ledger is malformed")
    samples = _load_dual_ci16(data_path, sample_count=TOTAL_SAMPLES)
    blocks: list[SimpleNamespace] = []
    expected_start = 0
    try:
        for index, raw_block in enumerate(blocks_document):
            block = _mapping(raw_block, f"legacy P0 persisted ABI2 block {index}")
            start = block.get("sample_start")
            count = block.get("sample_count")
            utc_ns = block.get("utc_ns")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or start != expected_start
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != SAMPLES_PER_FRAME
                or isinstance(utc_ns, bool)
                or not isinstance(utc_ns, int)
                or utc_ns < 0
            ):
                raise CohortAnalysisError(
                    "legacy P0 persisted ABI2 block timing differs from the exact plan"
                )
            blocks.append(
                SimpleNamespace(
                    samples=samples[:, start : start + count],
                    sample_count=count,
                    utc_ns=utc_ns,
                )
            )
            expected_start += count
        if expected_start != TOTAL_SAMPLES:
            raise CohortAnalysisError("legacy P0 persisted ABI2 timing does not cover raw IQ")
        # The legacy capture program quantized its fixed 100-kHz plan to the
        # AD936x 16-bit DDS accumulator.  This comes from the frozen plan/code,
        # never from the mutable analysis or DDS read-back fields.
        nominal_tone_hz = (
            round(TONE_OFFSET_HZ * (1 << 16) / SAMPLE_RATE_HZ) * SAMPLE_RATE_HZ / (1 << 16)
        )
        recomputed = _p2_runner()._analyze_blocks(
            blocks,
            profile=_p2_runner().load_profile(FAST20_PROFILE),
            tone_readback_hz=float(nominal_tone_hz),
        )
    except (InputOffContractError, ValueError, RuntimeError) as error:
        raise CohortAnalysisError(f"legacy P0 raw-IQ reanalysis failed: {error}") from error
    finally:
        del samples
    if not isinstance(recomputed, dict):
        raise CohortAnalysisError("legacy P0 raw-IQ reanalysis returned a malformed document")
    return recomputed


def _complex_value(value: object, label: str) -> complex:
    if isinstance(value, str):
        try:
            result = complex(value)
        except ValueError as error:
            raise CohortAnalysisError(f"{label} is not a complex value") from error
    elif isinstance(value, complex):
        result = value
    else:
        raise CohortAnalysisError(f"{label} is not a complex value")
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise CohortAnalysisError(f"{label} is not finite")
    return result


def _legacy_phasor_document(
    value: object,
    *,
    label: str,
    minimum_cycle_coherence: float,
) -> dict[str, Any]:
    summary = _mapping(value, label)
    phasor = _complex_value(summary.get("phasor"), f"{label} phasor")
    raw_cycles = summary.get("cycle_phasors")
    if not isinstance(raw_cycles, list) or not raw_cycles:
        raise CohortAnalysisError(f"{label} cycle phasors are malformed")
    cycles = [_complex_value(item, f"{label} cycle phasor") for item in raw_cycles]
    cycle_coherence = _finite_float(summary.get("cycle_coherence"), f"{label} coherence")
    phase_std = _finite_float(summary.get("cycle_phase_std_deg"), f"{label} phase std")
    even_odd = _finite_float(summary.get("even_odd_phase_agreement"), f"{label} even/odd agreement")
    reasons: list[str] = []
    if cycle_coherence < minimum_cycle_coherence:
        reasons.append("cycle_coherence_below_minimum")
    if even_odd < MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT:
        reasons.append("even_odd_agreement_below_minimum")
    if phase_std > 30.0:
        reasons.append("cycle_phase_std_above_maximum")
    return {
        "phasor": {"real": phasor.real, "imag": phasor.imag},
        "amplitude": _finite_float(summary.get("amplitude"), f"{label} amplitude"),
        "phase_deg": _finite_float(summary.get("phase_deg"), f"{label} phase"),
        "cycle_coherence": cycle_coherence,
        "cycle_phase_std_deg": phase_std,
        "even_odd_phase_agreement": even_odd,
        "cycle_phasors": [{"real": item.real, "imag": item.imag} for item in cycles],
        "repeat_quality_passed": not reasons,
        "repeat_quality_rejection_reasons": reasons,
    }


def _legacy_schedule_alignment_document(value: object) -> dict[str, Any]:
    alignment = _mapping(value, "raw-derived legacy P0 schedule alignment")
    provenance = _mapping(alignment.get("provenance"), "raw-derived alignment provenance")

    def candidate(value: object, label: str) -> dict[str, Any] | None:
        if value is None:
            return None
        item = _mapping(value, label)
        return {
            "cycle_ms": item.get("cycle_ms"),
            "marker_phase_ms": item.get("marker_phase_ms"),
            "complete_cycle_count": item.get("complete_cycle_count"),
            "fit": item.get("quality"),
        }

    return {
        "method": provenance.get("method_version"),
        "selected": candidate(alignment.get("selected"), "raw-derived selected alignment"),
        "distinct_runner_up": candidate(
            alignment.get("distinct_runner_up"), "raw-derived runner-up alignment"
        ),
        "score_margin": alignment.get("score_margin"),
        "search": dict(provenance),
        "decoder_agreement": alignment.get("decoded_timing_agreement"),
        "decoded_timing": alignment.get("decoded_timing"),
    }


def _require_raw_equivalent_legacy_collapse_fields(
    analysis: Mapping[str, Any], recomputed: Mapping[str, Any]
) -> None:
    transfer = _mapping(analysis.get("transfer"), "legacy P0 transfer")
    reference = _mapping(
        recomputed.get("reference_transfer"), "raw-derived legacy P0 reference transfer"
    )
    pilot = _mapping(recomputed.get("pilot"), "raw-derived legacy P0 pilot")
    expected_pilot = {name: value for name, value in pilot.items() if name != "snr_db"}
    expected_scalars = {
        name: reference.get(name)
        for name in (
            "cycle_ms",
            "marker_phase_ms",
            "bin_duration_ms",
            "bin_count",
            "complete_cycle_count",
            "edge_exclusion_ms",
            "alignment_score",
            "alignment_even_odd_agreement",
            "reference_valid_bin_fraction",
            "continuity_verified",
            "continuity_block_count",
            "all_off_anchor_count",
        )
    }
    expected_schedule = _legacy_schedule_alignment_document(reference.get("schedule_alignment"))
    expected_all_off = {
        "rx1": _legacy_phasor_document(
            reference.get("all_off_rx1"),
            label="raw-derived legacy P0 ALL_OFF RX1",
            minimum_cycle_coherence=MINIMUM_RX1_CYCLE_COHERENCE,
        ),
        "raw_rx2_over_rx1": _legacy_phasor_document(
            reference.get("all_off_raw_rx2_over_rx1"),
            label="raw-derived legacy P0 ALL_OFF transfer",
            minimum_cycle_coherence=0.75,
        ),
        "used_as_global_admission_gate": False,
    }
    observed_scalars = {name: transfer.get(name) for name in expected_scalars}
    comparisons = (
        (analysis.get("pilot"), expected_pilot, "pilot"),
        (observed_scalars, expected_scalars, "timing/alignment scalars"),
        (transfer.get("schedule_alignment"), expected_schedule, "schedule alignment"),
        (transfer.get("all_off"), expected_all_off, "ALL_OFF phasor evidence"),
    )
    for observed, expected, label in comparisons:
        if canonical_sha256(observed) != canonical_sha256(expected):
            raise CohortAnalysisError(
                f"legacy P0 stored {label} differs from canonical raw-IQ recomputation"
            )


def _normalize_legacy_documents(
    analysis: Mapping[str, Any],
    manifest: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    run_id: str,
    data_sha256: str,
    pilot_snr_db: float,
    raw_all_off: Mapping[str, Any],
    test_only_legacy_boards_root: Path | None = None,
) -> dict[str, Any]:
    if analysis.get("schema") != 1 or analysis.get("analysis_kind") != (
        "fast20_dual_rx_ota_reference_transfer"
    ):
        raise CohortAnalysisError("legacy P0 analysis kind is unsupported")
    artifact = _mapping(analysis.get("artifact"), "legacy P0 artifact")
    capture = _mapping(analysis.get("capture"), "legacy P0 capture")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise CohortAnalysisError("legacy P0 artifact ID is missing")
    _legacy_attempt(
        manifest,
        run_id=run_id,
        artifact_id=artifact_id,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    quality = _mapping(analysis.get("quality_gate"), "legacy P0 quality gate")
    transfer = _mapping(analysis.get("transfer"), "legacy P0 transfer")
    all_off = _mapping(transfer.get("all_off"), "legacy P0 all-off")
    raw_transfer = _mapping(all_off.get("raw_rx2_over_rx1"), "legacy P0 all-off transfer")
    rx1 = _mapping(all_off.get("rx1"), "legacy P0 all-off RX1")
    pilot = _mapping(analysis.get("pilot"), "legacy P0 pilot")
    if quality.get("passed") is not True:
        raise CohortAnalysisError("legacy P0 reference-transfer quality gate failed")
    headroom = _mapping(capture.get("adc_headroom_admission"), "legacy P0 headroom")
    if headroom.get("passed") is not True:
        raise CohortAnalysisError("legacy P0 ADC headroom admission failed")
    exact_capture = {
        "center_frequency_hz": CENTER_FREQUENCY_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "receiver_gain_db": int(RECEIVER_GAIN_DB),
        "samples_per_frame": SAMPLES_PER_FRAME,
        "frame_count": FRAME_COUNT,
        "sample_count": TOTAL_SAMPLES,
        "duration_s": DURATION_S,
        "kernel_buffers": KERNEL_BUFFERS,
        "metadata_abi": 2,
        "tx_channel": 0,
        "tx_gain_readback_db": TX_HARDWARE_GAIN_DB,
    }
    for field, wanted in exact_capture.items():
        observed = capture.get(field)
        if isinstance(wanted, float):
            if not isinstance(observed, (int, float)) or not math.isclose(
                float(observed), wanted, abs_tol=1e-9
            ):
                raise CohortAnalysisError(f"legacy P0 capture.{field} differs")
        elif observed != wanted:
            raise CohortAnalysisError(f"legacy P0 capture.{field} differs")
    scales = capture.get("dds_scale_readback")
    if not isinstance(scales, list) or len(scales) != 8:
        raise CohortAnalysisError("legacy P0 DDS scale readback is malformed")
    if any(float(scales[index]) != DDS_SCALE for index in (0, 2)) or any(
        float(scales[index]) != 0.0 for index in (1, 3, 4, 5, 6, 7)
    ):
        raise CohortAnalysisError("legacy P0 DDS scale readback differs from 0.25 TX1-only")
    captures = metadata.get("captures")
    if not isinstance(captures, list) or len(captures) != 1:
        raise CohortAnalysisError("legacy P0 SigMF must contain one settings capture")
    settings = _mapping(captures[0].get("settings"), "legacy P0 SigMF settings")
    if (
        float(settings.get("bandwidth_hz", 0.0)) != BANDWIDTH_HZ
        or float(settings.get("center_frequency_hz", 0.0)) != CENTER_FREQUENCY_HZ
        or float(settings.get("sample_rate_hz", 0.0)) != SAMPLE_RATE_HZ
        or float(settings.get("gain_db", -1.0)) != RECEIVER_GAIN_DB
        or settings.get("channels") != [0, 1]
    ):
        raise CohortAnalysisError("legacy P0 SigMF settings do not match P2")
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
    )
    if continuity["stream_id"] != capture.get("stream_id"):
        raise CohortAnalysisError("legacy P0 live/persisted stream IDs differ")
    if (
        transfer.get("continuity_verified") is not True
        or transfer.get("complete_cycle_count", 0) < MINIMUM_COMPLETE_FAST20_FRAMES
        or transfer.get("alignment_score", 0.0) < MINIMUM_ALIGNMENT_SCORE
        or transfer.get("alignment_even_odd_agreement", 0.0) < MINIMUM_ALIGNMENT_EVEN_ODD_AGREEMENT
        or transfer.get("reference_valid_bin_fraction", 0.0) < MINIMUM_REFERENCE_VALID_BIN_FRACTION
        or rx1.get("cycle_coherence", 0.0) < MINIMUM_RX1_CYCLE_COHERENCE
        or not math.isclose(
            float(transfer.get("edge_exclusion_ms", -1.0)),
            float(EDGE_EXCLUSION_BINS),
            abs_tol=1e-9,
        )
        or not math.isclose(float(transfer.get("bin_duration_ms", -1.0)), 1.0, abs_tol=1e-9)
    ):
        raise CohortAnalysisError("legacy P0 Fast20 alignment/central-window evidence failed")
    _mapping(raw_transfer.get("phasor"), "legacy P0 all-off phasor")
    _require_raw_equivalent_legacy_collapse_fields(analysis, raw_all_off)
    recomputed_phasor = raw_all_off.get("all_off_transfer")
    recomputed_rx1_amplitude = raw_all_off.get("rx1_reference_amplitude")
    if not isinstance(recomputed_phasor, complex) or not isinstance(
        recomputed_rx1_amplitude, (int, float)
    ):
        raise CohortAnalysisError("raw-derived P0 collapse fields are malformed")
    source_commit = capture.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise CohortAnalysisError("legacy P0 capture source commit is malformed")
    profile_sha256 = capture.get("profile_contract_sha256")
    if not isinstance(profile_sha256, str) or len(profile_sha256) != 64:
        raise CohortAnalysisError("legacy P0 Fast20 profile hash is malformed")
    configuration = _mapping(manifest.get("configuration"), "legacy P0 configuration")
    if configuration.get("profile_contract_sha256") != profile_sha256:
        raise CohortAnalysisError("legacy P0 manifest/capture Fast20 profile hashes differ")
    if not isinstance(pilot_snr_db, (int, float)) or not math.isfinite(pilot_snr_db):
        raise CohortAnalysisError("legacy P0 pilot SNR is not finite")
    if not isinstance(pilot.get("estimated_offset_hz"), (int, float)):
        raise CohortAnalysisError("legacy P0 pilot estimator evidence is missing")
    document = {
        "schema": 1,
        "observation_kind": OBSERVATION_KIND,
        "cohort": "P0",
        "run_id": run_id,
        "artifact": {
            "artifact_id": artifact_id,
            "stream_id": int(capture["stream_id"]),
            "sha256": data_sha256,
        },
        "acquisition": acquisition_contract(),
        "profile_contract_sha256": profile_sha256,
        "analysis": {
            "transfer_detected": True,
            "all_off_transfer": {
                "real": float(recomputed_phasor.real),
                "imag": float(recomputed_phasor.imag),
            },
            "all_off_transfer_upper_bound": None,
            "rx1_reference_amplitude": float(recomputed_rx1_amplitude),
            "detected_pilot_snr_db": float(pilot_snr_db),
        },
        "quality": {
            "passed": True,
            "continuity_verified": True,
            "metadata_abi": 2,
            "headroom_passed": True,
            "final_mute_passed": True,
            "fast20_schedule_verified": True,
            "central_all_off_windows_used": True,
        },
        "provenance": {
            "source_commit": source_commit,
            "source_files_sha256": None,
            "native_attestation_sha256": None,
            "fixture_evidence_sha256": None,
            "fixture_fixed_graph_sha256": None,
            "comparable_fixture_group_id": None,
        },
    }
    validate_observation(document, expected_cohort="P0")
    return document


def normalize_legacy_p0(
    *,
    run_id: str,
    analysis_path: Path,
    manifest_path: Path,
    normalizer_source: Mapping[str, Any],
    test_only_legacy_boards_root: Path | None = None,
) -> dict[str, Any]:
    """Load and seal one accepted legacy P0 exact-5.8-GHz observation."""

    analysis = _read_json(analysis_path, "legacy P0 reference-transfer analysis")
    manifest = _read_json(manifest_path, "legacy P0 sweep manifest")
    artifact = _mapping(analysis["artifact"], "legacy P0 artifact")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise CohortAnalysisError("legacy P0 artifact ID is missing")
    attempt = _legacy_attempt(
        manifest,
        run_id=run_id,
        artifact_id=artifact_id,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    metadata, data_path, metadata_path = _meta_and_paths(
        analysis, manifest, analysis_path=analysis_path, attempt=attempt
    )
    data_sha256 = sha256_path(data_path)
    quality = _mapping(attempt.get("quality_result"), "legacy P0 quality result")
    artifact_identity = _mapping(attempt.get("artifact_identity"), "legacy P0 artifact identity")
    if (
        data_sha256 != artifact.get("sha256")
        or quality.get("artifact_sha256") != data_sha256
        or artifact_identity.get("sha256") != data_sha256
    ):
        raise CohortAnalysisError("legacy P0 raw IQ SHA-256 differs from artifact evidence")
    raw_analysis = _recompute_legacy_all_off_from_raw(
        data_path,
        metadata=metadata,
        manifest=manifest,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    pilot_snr = raw_analysis.get("detected_pilot_snr_db")
    if isinstance(pilot_snr, bool) or not isinstance(pilot_snr, (int, float)):
        raise CohortAnalysisError("raw-derived legacy P0 pilot SNR is malformed")
    observation = _normalize_legacy_documents(
        analysis,
        manifest,
        metadata,
        run_id=run_id,
        data_sha256=data_sha256,
        pilot_snr_db=float(pilot_snr),
        raw_all_off=raw_analysis,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    if observation["provenance"]["source_commit"] != normalizer_source.get("commit"):
        raise CohortAnalysisError("legacy P0 and normalizer must use one frozen source commit")
    return build_normalized_p0_envelope(
        observation,
        manifest_path=manifest_path,
        analysis_path=analysis_path,
        metadata_path=metadata_path,
        raw_iq_path=data_path,
        normalizer_source=normalizer_source,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )


def _admit_and_recompute_normalized_p0(
    path: Path,
    *,
    normalizer_source: Mapping[str, Any],
    dependency: Mapping[str, Any],
    native: Mapping[str, Any],
    test_only_legacy_boards_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Admit a sealed P0, then independently repeat its raw-IQ normalization."""

    observation, binding = admit_normalized_p0_evidence(
        path,
        expected_normalizer_repository=REPOSITORY,
        expected_normalizer_commit=str(normalizer_source["commit"]),
        required_source_paths=tuple(_p2_runner().SOURCE_FILES),
        expected_dependency_attestation=dependency,
        expected_native_attestation=native,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    envelope = _read_json(path.expanduser().absolute(), "normalized P0 evidence")
    source_artifacts = _mapping(envelope.get("source_artifacts"), "P0 source artifacts")
    manifest_binding = _mapping(
        source_artifacts.get("legacy_manifest"), "P0 legacy manifest binding"
    )
    analysis_binding = _mapping(
        source_artifacts.get("reference_transfer_analysis"), "P0 analysis binding"
    )
    manifest_path = verify_file_binding(manifest_binding, label="P0 legacy manifest")
    analysis_path = verify_file_binding(analysis_binding, label="P0 legacy analysis")
    recomputed = normalize_legacy_p0(
        run_id=str(observation.get("run_id", "")),
        analysis_path=analysis_path,
        manifest_path=manifest_path,
        normalizer_source=normalizer_source,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    if recomputed.get("observation") != observation:
        raise CohortAnalysisError(
            "sealed P0 observation differs from fresh bound raw-IQ normalization"
        )
    return observation, binding


def _verify_p2_fixture_files(fixture: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    """Re-hash every directly referenced fixture/setup/profile evidence file."""

    verify_file_binding(profile, label="P2 Fast20 profile")
    sources = _mapping(fixture.get("source_files"), "P2 fixture source files")
    for name in ("fixture_manifest", "setup_attestation"):
        verify_file_binding(sources.get(name), label=f"P2 {name}")
    fixture_document = _mapping(fixture.get("fixture"), "P2 fixture graph")
    setup = _mapping(fixture.get("setup_attestation"), "P2 setup attestation")
    fixture_profile = _mapping(
        _mapping(fixture_document.get("fast20_control"), "P2 Fast20 control").get("profile"),
        "P2 fixture Fast20 profile",
    )
    planned_profile_file = {name: profile.get(name) for name in ("path", "sha256", "size_bytes")}
    if fixture_profile != planned_profile_file:
        raise CohortAnalysisError("P2 plan and fixture bind different Fast20 profiles")
    for value, label in (
        (fixture_document.get("baseline_topology_evidence"), "P2 baseline topology evidence"),
        (
            _mapping(fixture_document.get("fast20_control"), "P2 Fast20 control").get(
                "live_image_evidence"
            ),
            "P2 Fast20 live-image evidence",
        ),
        (setup.get("setup_evidence"), "P2 setup evidence"),
    ):
        verify_file_binding(value, label=label)
    fast20_control = _mapping(fixture_document.get("fast20_control"), "P2 Fast20 control")
    verifier = _p2_runner()
    try:
        observed_live_image = verifier._validate_fast20_live_image(
            _mapping(fast20_control.get("live_image_evidence"), "P2 Fast20 live image"),
            campaign_id=str(fixture_document.get("campaign_id", "")),
            board_id=str(fixture_document.get("board_id", "")),
        )
    except verifier.InputOffRunError as error:
        raise CohortAnalysisError("P2 sealed Fast20 image failed recursive re-admission") from error
    if observed_live_image != fixture.get("sealed_fast20_live_image"):
        raise CohortAnalysisError("P2 sealed Fast20 image differs from immutable fixture evidence")
    components = fixture_document.get("components")
    connections = fixture_document.get("connections")
    if not isinstance(components, Mapping) or not isinstance(connections, Mapping):
        raise CohortAnalysisError("P2 fixture component/connection graph is malformed")
    characterizations = [
        item.get("characterization") for item in components.values() if isinstance(item, Mapping)
    ] + [
        _mapping(item.get("interconnect"), "P2 interconnect").get("characterization")
        for item in connections.values()
        if isinstance(item, Mapping)
    ]
    rx2_attenuator = _mapping(fixture_document.get("rx2_attenuator"), "P2 optional RX2 attenuator")
    if rx2_attenuator.get("state") == "present":
        component = _mapping(
            rx2_attenuator.get("component"), "P2 optional RX2 attenuator component"
        )
        pluto_connection = _mapping(
            rx2_attenuator.get("pluto_connection"),
            "P2 optional RX2 attenuator Pluto connection",
        )
        interconnect = _mapping(
            pluto_connection.get("interconnect"),
            "P2 optional RX2 attenuator Pluto interconnect",
        )
        characterizations.extend(
            (component.get("characterization"), interconnect.get("characterization"))
        )
    elif rx2_attenuator.get("state") != "absent":
        raise CohortAnalysisError("P2 optional RX2 attenuator state is not explicit")
    for index, raw in enumerate(characterizations):
        characterization = _mapping(raw, f"P2 characterization {index}")
        if characterization.get("status") != "characterized":
            continue
        binding = {
            "path": characterization.get("evidence_path"),
            "sha256": characterization.get("evidence_sha256"),
        }
        verify_file_binding(
            binding,
            label=f"P2 characterization {index}",
            size_field=None,
        )


def _reverify_p2_raw(
    *,
    observation: Mapping[str, Any],
    result: Mapping[str, Any],
    condition: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> None:
    artifact = _mapping(result.get("artifact_evidence"), "P2 artifact evidence")
    normalized_artifact = _mapping(observation.get("artifact"), "P2 normalized artifact")
    samples, continuity, raw_path, metadata_path = admit_dual_rx_ci16_artifact(
        artifact,
        label="P2 artifact",
        expected_sample_count=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
        expected_sample_rate_hz=float(SAMPLE_RATE_HZ),
        expected_stream_id=normalized_artifact.get("stream_id"),
        expected_artifact_id=str(normalized_artifact.get("artifact_id")),
    )
    storage = _mapping(contract.get("storage"), "P2 local storage contract")
    capture_root = Path(str(storage.get("capture_root", ""))).expanduser().absolute()
    expected_artifact_root = capture_root / str(normalized_artifact.get("artifact_id"))
    if raw_path.parent != expected_artifact_root or metadata_path.parent != expected_artifact_root:
        raise CohortAnalysisError("P2 artifact is outside its local capture root")
    metadata = read_json_file(metadata_path, label="P2 SigMF metadata")
    global_section = metadata.get("global")
    captures = metadata.get("captures")
    configuration = _mapping(contract.get("configuration"), "P2 configuration")
    if (
        not isinstance(global_section, Mapping)
        or not isinstance(captures, list)
        or len(captures) != 1
        or not isinstance(captures[0], Mapping)
    ):
        raise CohortAnalysisError("P2 SigMF identity/settings are malformed")
    radio = global_section.get("pluto:radio")
    settings = captures[0].get("settings")
    expected_settings = {
        "bandwidth_hz": float(BANDWIDTH_HZ),
        "center_frequency_hz": float(CENTER_FREQUENCY_HZ),
        "channels": [0, 1],
        "gain_db": float(RECEIVER_GAIN_DB),
        "gain_mode": "manual",
        "sample_rate_hz": float(SAMPLE_RATE_HZ),
    }
    if (
        settings != expected_settings
        or not isinstance(radio, Mapping)
        or radio.get("serial") != configuration.get("serial")
        or radio.get("uri") != configuration.get("uri")
    ):
        raise CohortAnalysisError("P2 SigMF radio/settings differ from immutable plan")
    capture = _mapping(condition.get("capture"), "P2 condition capture")
    persisted_ledger = metadata.get("pluto:continuity")
    if persisted_ledger is None:
        persisted_ledger = global_section.get("pluto:continuity")
    if (
        capture.get("persisted_continuity_audit") != continuity
        or capture.get("live_continuity_ledger") != persisted_ledger
        or capture.get("stream_id") != continuity.get("stream_id")
    ):
        raise CohortAnalysisError("P2 live and persisted ABI2 ledgers differ")
    monitor = AdcHeadroomMonitor(receiver_count=2)
    monitor.observe(samples)
    if condition.get("capture", {}).get("adc_headroom_admission") != _p2_runner()._json_safe(
        asdict(monitor.result())
    ):
        raise CohortAnalysisError("P2 stored ADC headroom differs from raw IQ")
    blocks_document = (
        persisted_ledger.get("blocks") if isinstance(persisted_ledger, Mapping) else None
    )
    if not isinstance(blocks_document, list) or len(blocks_document) != FRAME_COUNT:
        raise CohortAnalysisError("P2 persisted ABI2 block ledger is malformed")
    blocks: list[SimpleNamespace] = []
    for block in blocks_document:
        if not isinstance(block, Mapping):
            raise CohortAnalysisError("P2 persisted ABI2 block is malformed")
        start = int(block["sample_start"])
        count = int(block["sample_count"])
        blocks.append(
            SimpleNamespace(
                samples=samples[:, start : start + count],
                sample_count=count,
                utc_ns=int(block["utc_ns"]),
            )
        )
    profile_contract = _mapping(contract.get("profile"), "P2 profile")
    profile = _p2_runner().load_profile(Path(str(profile_contract["path"])))
    tone_readback = capture.get("tone_offset_hz_readback")
    if isinstance(tone_readback, bool) or not isinstance(tone_readback, (int, float)):
        raise CohortAnalysisError("P2 tone readback is malformed")
    recomputed = _p2_runner()._analyze_blocks(
        blocks,
        profile=profile,
        tone_readback_hz=float(tone_readback),
    )
    del samples
    if condition.get("analysis") != _p2_runner()._json_safe(recomputed):
        raise CohortAnalysisError("P2 stored Fast20 analysis differs from raw IQ")
    detected = bool(recomputed["all_off_transfer_detected"])
    expected_analysis = {
        "transfer_detected": detected,
        "all_off_transfer": (
            _p2_runner().complex_document(recomputed["all_off_transfer"]) if detected else None
        ),
        "all_off_transfer_upper_bound": recomputed["all_off_transfer_upper_bound"],
        "rx1_reference_amplitude": recomputed["rx1_reference_amplitude"],
        "detected_pilot_snr_db": recomputed["detected_pilot_snr_db"],
    }
    if observation.get("analysis") != expected_analysis:
        raise CohortAnalysisError("P2 normalized transfer differs from raw-IQ recomputation")
    if not detected and (
        expected_analysis["all_off_transfer"] is not None
        or not isinstance(expected_analysis["all_off_transfer_upper_bound"], (int, float))
        or float(expected_analysis["all_off_transfer_upper_bound"]) <= 0.0
    ):
        raise CohortAnalysisError("P2 nondetection lacks a phase-free magnitude bound")


def _require_ordered_p0_bindings(
    contract: Mapping[str, Any], expected: Sequence[Mapping[str, Any]]
) -> None:
    observed = contract.get("p0_baseline_bindings")
    if not isinstance(observed, list) or observed != [dict(item) for item in expected]:
        raise CohortAnalysisError(
            "P2 plan P0 baseline bindings differ from the exact ordered caller cohort"
        )


def _accepted_p2_observation(
    observation_path: Path,
    manifest_path: Path,
    *,
    expected_p0_bindings: Sequence[Mapping[str, Any]],
    ledger_backend: global_ledger.LedgerBackend,
    current_dependency_attestation: Mapping[str, Any] | None = None,
    current_native_attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observation_file = observation_path.expanduser().absolute()
    manifest_file = manifest_path.expanduser().absolute()
    observation = _read_json(observation_file, "P2 observation")
    normalized = validate_observation(observation, expected_cohort="P2")
    manifest = _read_json(manifest_file, "P2 run manifest")
    attempts = manifest.get("attempts")
    result = manifest.get("result")
    if (
        manifest.get("schema") != 1
        or manifest.get("run_kind") != "5g8_input_drive_off_fast20_one_stream"
        or manifest.get("run_id") != normalized.run_id
        or manifest.get("status") != "complete"
        or manifest.get("accepted_stream_count") != 1
        or not isinstance(attempts, list)
        or len(attempts) != 1
        or not isinstance(attempts[0], Mapping)
        or attempts[0].get("status") != "complete"
        or not isinstance(result, Mapping)
        or attempts[0].get("result") != result
        or result.get("observation_path") != str(observation_file)
        or result.get("observation_sha256") != sha256_path(observation_file)
        or result.get("stream_id") != normalized.stream_id
    ):
        raise CohortAnalysisError("P2 observation lacks one complete manifest acceptance")
    artifact = result.get("artifact")
    if not isinstance(artifact, Mapping) or artifact.get("artifact_id") != normalized.artifact_id:
        raise CohortAnalysisError("P2 manifest artifact identity differs from observation")
    plan_evidence = _mapping(manifest.get("plan"), "P2 manifest plan evidence")
    expected_plan_path = manifest_file.parent / "plan.json"
    if (
        plan_evidence.get("path") != str(expected_plan_path)
        or expected_plan_path.is_symlink()
        or not expected_plan_path.is_file()
        or expected_plan_path.stat().st_mode & stat.S_IWUSR
        or plan_evidence.get("sha256") != sha256_path(expected_plan_path)
    ):
        raise CohortAnalysisError("P2 immutable plan path/hash/mode differs from manifest")
    envelope = _read_json(expected_plan_path, "P2 immutable plan")
    contract = _mapping(envelope.get("plan_contract"), "P2 immutable plan contract")
    contract_sha256 = canonical_sha256(contract)
    if (
        envelope.get("schema") != 1
        or envelope.get("immutable") is not True
        or envelope.get("plan_contract_sha256") != contract_sha256
        or plan_evidence.get("contract_sha256") != contract_sha256
        or contract.get("run_id") != normalized.run_id
        or contract.get("run_kind") != "5g8_input_drive_off_fast20_one_stream"
        or contract.get("acquisition") != acquisition_contract()
        or _mapping(contract.get("profile"), "P2 plan profile").get("contract_sha256")
        != normalized.profile_contract_sha256
    ):
        raise CohortAnalysisError("P2 immutable plan contract is inconsistent")
    _require_ordered_p0_bindings(contract, expected_p0_bindings)
    source = _mapping(contract.get("source"), "P2 plan source")
    smateway_source = _mapping(source.get("smateway"), "P2 Smateway source")
    dependency_source = _mapping(source.get("pluto_plus_utils"), "P2 dependency source")
    fixture = _mapping(contract.get("fixture_evidence"), "P2 fixture evidence")
    fixture_document = _mapping(fixture.get("fixture"), "P2 fixture graph")
    profile_contract = _mapping(contract.get("profile"), "P2 plan profile")
    storage = _mapping(contract.get("storage"), "P2 local storage contract")
    smateway_files = smateway_source.get("files")
    dependency_files = dependency_source.get("files")
    if (
        storage.get("local_rpi_only") is not True
        or storage.get("pluto_storage_forbidden") is not True
        or storage.get("local_storage_device") != Path("/home/pi").stat().st_dev
        or storage.get("run_root") != str(manifest_file.parent)
        or not all(
            isinstance(storage.get(name), str) and Path(str(storage[name])).is_absolute()
            for name in ("state_root", "run_root", "capture_root")
        )
        or not isinstance(smateway_files, list)
        or not isinstance(dependency_files, list)
        or not smateway_files
        or not dependency_files
        or smateway_source.get("source_files_sha256") != canonical_sha256(smateway_files)
        or source.get("native_libiio_sha256")
        != attestation_sha256(validate_runtime_attestation(source.get("native_libiio")))
        or smateway_source.get("commit") != normalized.source_commit
        or smateway_source.get("source_files_sha256") != normalized.source_files_sha256
        or source.get("native_libiio_sha256") != normalized.native_attestation_sha256
        or contract.get("fixture_evidence_sha256") != normalized.fixture_evidence_sha256
        or fixture_document.get("fixed_graph_sha256") != normalized.fixture_fixed_graph_sha256
        or fixture_document.get("comparable_fixture_group_id")
        != normalized.comparable_fixture_group_id
    ):
        raise CohortAnalysisError("P2 normalized source/native/fixture identities differ from plan")
    runner = _p2_runner()
    try:
        runner._require_local_storage_contract(contract, run_root=manifest_file.parent)
        global_reservation = _mapping(
            manifest.get("global_run_reservation"), "P2 global run reservation"
        )
        global_burn = runner._validate_global_burn(
            contract=contract,
            plan_path=expected_plan_path,
            manifest_path=manifest_file,
            reservation=global_reservation,
            value=manifest.get("global_execution_burn"),
            ledger_backend=ledger_backend,
        )
    except Exception as error:
        raise CohortAnalysisError(
            f"P2 fixed global reserve/burn authority is invalid: {error}"
        ) from error
    if (
        manifest.get("global_execution_burn") != global_burn
        or attempts[0].get("global_execution_burn") != global_burn
        or result.get("global_execution_burn") != global_burn
        or manifest.get("global_failure_receipt") not in (None,)
        or manifest.get("quarantine") not in (None,)
    ):
        raise CohortAnalysisError("P2 accepted evidence does not share one global burn receipt")
    if current_dependency_attestation is not None and dependency_source != dict(
        current_dependency_attestation
    ):
        raise CohortAnalysisError(
            "P2 frozen pluto-plus-utils source differs from the current pinned analyzer imports"
        )
    if current_native_attestation is not None and source.get("native_libiio") != dict(
        current_native_attestation
    ):
        raise CohortAnalysisError(
            "P2 frozen native libiio runtime differs from the current analyzer process"
        )
    assert_local_rpi_storage(manifest_file.parent, label="P2 run storage")
    assert_local_rpi_storage(Path(str(storage["capture_root"])), label="P2 capture storage")
    verify_source_tree_binding(
        smateway_source,
        label="P2 Smateway",
        required_relative_paths=tuple(_p2_runner().SOURCE_FILES),
    )
    verify_source_tree_binding(dependency_source, label="P2 pluto-plus-utils")
    _verify_p2_fixture_files(fixture, profile_contract)
    if not _passed_mute(result.get("final_mute"), "final_acceptance_exact_mute"):
        raise CohortAnalysisError("P2 accepted result lacks exact final-mute evidence")
    if result.get("native_runtime_preflight") != source.get("native_libiio"):
        raise CohortAnalysisError("P2 runtime native identity differs from plan")
    identity = _mapping(result.get("identity_preflight"), "P2 identity preflight")
    configuration = _mapping(contract.get("configuration"), "P2 radio configuration")
    if (
        identity.get("status") != "passed"
        or identity.get("serial") != configuration.get("serial")
        or identity.get("resolved_uri") != configuration.get("uri")
        or identity.get("exact_uri_match") is not True
    ):
        raise CohortAnalysisError("P2 live serial/current-URI identity differs from plan")

    capture_binding = _mapping(
        result.get("capture_root_binding"), "P2 capture-root directory-FD binding"
    )
    capture_root = Path(str(storage["capture_root"])).expanduser().absolute()
    artifact_root = capture_root / normalized.artifact_id
    expected_data_path = artifact_root / f"{normalized.artifact_id}.sigmf-data"
    expected_metadata_path = artifact_root / f"{normalized.artifact_id}.sigmf-meta"
    expected_observation_path = artifact_root / runner.OBSERVATION_FILENAME
    expected_condition_path = artifact_root / "5g8-input-off-condition.json"
    if (
        capture_binding.get("path") != str(capture_root)
        or result.get("artifact_id") != normalized.artifact_id
        or artifact.get("path") != str(artifact_root)
        or observation_file != expected_observation_path
        or result.get("condition_record_path") != str(expected_condition_path)
    ):
        raise CohortAnalysisError("P2 accepted paths escape the bound capture root")
    try:
        capture_fd, validated_capture_binding = runner._validate_capture_root_binding(
            capture_binding
        )
        try:
            artifact_identity = runner._validate_accepted_capture_inventory(
                capture_fd, artifact_id=normalized.artifact_id
            )
        finally:
            os.close(capture_fd)
        exact_artifact_root = assert_no_symlink_chain(
            artifact_root, label="P2 accepted artifact root"
        )
        exact_members = tuple(
            assert_no_symlink_chain(path, label=label)
            for path, label in (
                (expected_data_path, "P2 raw IQ"),
                (expected_metadata_path, "P2 SigMF metadata"),
                (expected_observation_path, "P2 normalized observation"),
                (expected_condition_path, "P2 condition record"),
            )
        )
    except (FileArtifactAdmissionError, OSError, runner.InputOffRunError) as error:
        raise CohortAnalysisError(f"P2 bound capture inventory is invalid: {error}") from error
    if (
        validated_capture_binding != capture_binding
        or result.get("artifact_directory_identity") != artifact_identity
        or stat.S_IMODE(exact_artifact_root.stat().st_mode) != 0o500
        or any(stat.S_IMODE(path.stat().st_mode) != 0o400 for path in exact_members)
    ):
        raise CohortAnalysisError("P2 artifact tree is not the sealed bound capture inventory")

    artifact_evidence = _mapping(result.get("artifact_evidence"), "P2 artifact evidence")
    if (
        artifact_evidence.get("artifact_id") != normalized.artifact_id
        or artifact_evidence.get("data_sha256") != normalized.artifact_sha256
        or artifact_evidence.get("path") != str(artifact_root)
        or artifact_evidence.get("data_path") != str(expected_data_path)
        or artifact_evidence.get("metadata_path") != str(expected_metadata_path)
        or artifact_evidence.get("data_size_bytes") != expected_data_path.stat().st_size
        or artifact_evidence.get("metadata_size_bytes") != expected_metadata_path.stat().st_size
    ):
        raise CohortAnalysisError("P2 artifact evidence differs from observation")
    for label, path_key, hash_key in (
        ("P2 raw IQ", "data_path", "data_sha256"),
        ("P2 SigMF metadata", "metadata_path", "metadata_sha256"),
    ):
        evidence_path = Path(str(artifact_evidence.get(path_key, ""))).absolute()
        if (
            evidence_path.is_symlink()
            or not evidence_path.is_file()
            or sha256_path(evidence_path) != artifact_evidence.get(hash_key)
        ):
            raise CohortAnalysisError(f"{label} path/hash differs from manifest")
    condition_path = Path(str(result.get("condition_record_path", ""))).absolute()
    if (
        condition_path.is_symlink()
        or not condition_path.is_file()
        or sha256_path(condition_path) != result.get("condition_record_sha256")
    ):
        raise CohortAnalysisError("P2 condition-record path/hash differs from manifest")
    condition = _read_json(condition_path, "P2 condition record")
    if (
        condition.get("normalized_observation") != observation
        or condition.get("artifact_evidence") != artifact_evidence
        or condition.get("immutable_plan_contract_sha256") != contract_sha256
        or condition.get("global_execution_burn") != global_burn
        or condition.get("capture_root_binding") != capture_binding
        or _mapping(condition.get("safety"), "P2 condition safety").get(
            "persistence_began_only_after_final_mute_passed"
        )
        is not True
    ):
        raise CohortAnalysisError("P2 condition record does not bind accepted evidence")
    _reverify_p2_raw(
        observation=observation,
        result=result,
        condition=condition,
        contract=contract,
    )
    execution = _mapping(attempts[0].get("execution_tombstone"), "P2 execution tombstone")
    execution_path = manifest_file.parent / "execution-started.tombstone.json"
    if (
        execution.get("path") != str(execution_path)
        or execution_path.is_symlink()
        or not execution_path.is_file()
        or execution_path.stat().st_mode & stat.S_IWUSR
        or execution.get("sha256") != sha256_path(execution_path)
        or execution.get("document") != _read_json(execution_path, "P2 execution tombstone")
        or _mapping(execution.get("document"), "P2 execution tombstone document").get("run_id")
        != normalized.run_id
        or execution["document"].get("plan_path") != str(expected_plan_path)
        or execution["document"].get("plan_sha256") != sha256_path(expected_plan_path)
        or execution["document"].get("plan_contract_sha256") != contract_sha256
        or execution["document"].get("global_execution_burn") != global_burn
        or execution["document"].get("run_id_burned") is not True
        or execution["document"].get("automatic_retry_forbidden") is not True
    ):
        raise CohortAnalysisError("P2 execution tombstone identity is invalid")
    failure = manifest_file.parent / "failed-run.tombstone.json"
    if failure.exists() or failure.is_symlink():
        raise CohortAnalysisError("P2 run has a failure tombstone and cannot be accepted")
    return observation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--normalize-p0", action="store_true")
    mode.add_argument("--compare", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--legacy-analysis", type=Path)
    parser.add_argument("--legacy-manifest", type=Path)
    parser.add_argument("--p0-observation", type=Path, action="append", default=[])
    parser.add_argument("--p2-observation", type=Path, action="append", default=[])
    parser.add_argument("--p2-manifest", type=Path, action="append", default=[])
    parser.add_argument("--bootstrap-replicates", type=int, default=32_768)
    parser.add_argument("--seed", type=int, default=0x5A8_2026)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        dependency, native = _current_analysis_attestations()
        output = args.output.expanduser().absolute()
        assert_local_rpi_storage(output, label="P2 analysis output")
        if output.exists() or output.is_symlink():
            raise CohortAnalysisError("output already exists; evidence is never overwritten")
        if args.normalize_p0:
            if args.run_id is None or args.legacy_analysis is None or args.legacy_manifest is None:
                raise CohortAnalysisError(
                    "--normalize-p0 requires --run-id, --legacy-analysis, and --legacy-manifest"
                )
            if args.p0_observation or args.p2_observation or args.p2_manifest:
                raise CohortAnalysisError("normalization does not accept cohort observations")
            normalizer_source = _normalizer_source_with_runtime(dependency, native)
            document = normalize_legacy_p0(
                run_id=args.run_id,
                analysis_path=args.legacy_analysis,
                manifest_path=args.legacy_manifest,
                normalizer_source=normalizer_source,
            )
            write_sealed_normalized_p0(output, document)
        else:
            if (
                len(args.p0_observation) != 5
                or len(args.p2_observation) != 5
                or len(args.p2_manifest) != 5
            ):
                raise CohortAnalysisError(
                    "comparison requires five P0 observations and five P2 observation/manifests"
                )
            if any((args.run_id, args.legacy_analysis, args.legacy_manifest)):
                raise CohortAnalysisError("comparison does not accept legacy normalization options")
            normalizer_source = _normalizer_source_with_runtime(dependency, native)
            admitted_p0 = [
                _admit_and_recompute_normalized_p0(
                    path,
                    normalizer_source=normalizer_source,
                    dependency=dependency,
                    native=native,
                )
                for path in args.p0_observation
            ]
            p0 = [observation for observation, _binding in admitted_p0]
            p0_bindings = [binding for _observation, binding in admitted_p0]
            ledger_backend: global_ledger.LedgerBackend = global_ledger.SudoLedgerBackend()
            p2 = [
                _accepted_p2_observation(
                    observation,
                    manifest,
                    expected_p0_bindings=p0_bindings,
                    ledger_backend=ledger_backend,
                    current_dependency_attestation=dependency,
                    current_native_attestation=native,
                )
                for observation, manifest in zip(args.p2_observation, args.p2_manifest, strict=True)
            ]
            document = compare_p0_p2_cohorts(
                p0,
                p2,
                bootstrap_replicates=args.bootstrap_replicates,
                seed=args.seed,
            )
            document["analysis_runtime"] = {
                "analyzer_runtime_attestation": normalizer_source["analyzer_runtime_attestation"],
                "analyzer_runtime_attestation_sha256": normalizer_source[
                    "analyzer_runtime_attestation_sha256"
                ],
                "smateway_import_origin_attestation": normalizer_source[
                    "smateway_import_origin_attestation"
                ],
                "smateway_import_origin_attestation_sha256": normalizer_source[
                    "smateway_import_origin_attestation_sha256"
                ],
                "pluto_plus_utils_source_attestation": dependency,
                "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
                "native_libiio_runtime_attestation": native,
                "native_libiio_runtime_attestation_sha256": attestation_sha256(native),
            }
            write_json_atomic(output, document)
        print(json.dumps({"status": "complete", "output": str(output)}))
        return 0
    except (
        CohortAnalysisError,
        FileArtifactAdmissionError,
        InputOffContractError,
        P0NormalizedEvidenceError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        print(
            json.dumps({"status": "failed", "error": f"{type(error).__name__}: {error}"}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
