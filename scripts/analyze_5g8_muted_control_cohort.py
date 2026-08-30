#!/usr/bin/env python3
"""Verify and aggregate exactly five completed T1 muted-control run manifests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != PINNED_PREFIX
    or str(REPOSITORY / "src") not in sys.path
    or not loader_directories
    or loader_directories[0] != REQUIRED_LIBIIO_DIRECTORY
):
    if not PINNED_PYTHON.is_file() or not os.access(PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned analysis Python is not executable: {PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(REPOSITORY / "src")
        if not prior_pythonpath
        else f"{REPOSITORY / 'src'}{os.pathsep}{prior_pythonpath}"
    )
    loader_entries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != REQUIRED_LIBIIO_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(REQUIRED_LIBIIO_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(PINNED_PYTHON),
        [str(PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

for source in (REPOSITORY / "src", Path(__file__).resolve().parent):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import numpy as np  # noqa: E402
import numpy.typing as npt  # noqa: E402
import run_5g8_leakage_ladder as foundation  # noqa: E402
import run_5g8_muted_control as runner  # noqa: E402
from pluto_plus.artifacts import verify_artifact  # noqa: E402
from pluto_plus.models import ArtifactSummary  # noqa: E402

from smateway.hexcal import (  # noqa: E402
    PLUTO_PLUS_UTILS_IMPORTED_MODULES,
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    canonical_json_sha256,
    sha256_path,
)
from smateway.muted_control import (  # noqa: E402
    MutedControlAnalysisError,
    aggregate_muted_control_cohort,
    analyze_muted_stream,
)
from smateway.native_iio_attestation import (  # noqa: E402
    attest_runtime as native_libiio_runtime_attestation,
)
from smateway.native_iio_attestation import (  # noqa: E402
    validate_runtime_attestation,
)


class MutedCohortArtifactError(MutedControlAnalysisError):
    """A manifest, plan, record, or raw artifact fails offline verification."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise MutedCohortArtifactError(f"{label} must be an absolute regular non-symlink file")
    try:
        exact = runner._assert_safe_local_path(path, label=label)
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError(
            f"{label} is not a local no-symlink Raspberry Pi path"
        ) from error
    if exact.is_symlink() or not exact.is_file():
        raise MutedCohortArtifactError(f"{label} must be an absolute regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MutedCohortArtifactError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise MutedCohortArtifactError(f"{label} root must be an object")
    try:
        runner._assert_safe_local_path(exact, label=label)
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError(f"{label} path changed while being read") from error
    return value


def _evidence_path(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise MutedCohortArtifactError(f"{label} path is missing")
    path = Path(value)
    if not path.is_absolute():
        raise MutedCohortArtifactError(f"{label} must be an absolute regular file")
    try:
        exact = runner._assert_safe_local_path(path, label=label)
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError(
            f"{label} is not a local no-symlink Raspberry Pi path"
        ) from error
    if exact.is_symlink() or not exact.is_file():
        raise MutedCohortArtifactError(f"{label} must be an absolute regular file")
    return exact


def _verify_hash(path: Path, expected: object, label: str) -> None:
    if not isinstance(expected, str) or sha256_path(path) != expected:
        raise MutedCohortArtifactError(f"{label} SHA-256 differs")


def _verify_size(path: Path, expected: object, label: str) -> None:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise MutedCohortArtifactError(f"{label} size evidence is malformed")
    if path.stat().st_size != expected:
        raise MutedCohortArtifactError(f"{label} size differs")


def _admit_new_output(path: Path) -> Path:
    if not path.is_absolute():
        raise MutedCohortArtifactError("output path must be absolute")
    try:
        output = runner._assert_safe_local_path(path, label="muted-control cohort output")
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError("output must have local no-symlink ancestry") from error
    if output.exists() or output.is_symlink():
        raise MutedCohortArtifactError("output path already exists")
    return output


def _load_dual_ci16(path: Path, *, sample_count: int) -> npt.NDArray[np.complex64]:
    raw: npt.NDArray[np.int16] = np.memmap(path, dtype="<i2", mode="r")
    if raw.size != sample_count * 2 * 2:
        raise MutedCohortArtifactError("raw data is not exact dual-RX CI16")
    components = raw.reshape(sample_count, 2, 2)
    output: npt.NDArray[np.complex64] = np.empty((2, sample_count), dtype=np.complex64)
    for start in range(0, sample_count, 250_000):
        stop = min(sample_count, start + 250_000)
        output[:, start:stop].real = components[start:stop, :, 0].T
        output[:, start:stop].imag = components[start:stop, :, 1].T
    return output


def _headroom_passed(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("passed") is not True:
        return False
    receivers = value.get("receivers")
    return (
        isinstance(receivers, list)
        and len(receivers) == 2
        and all(
            isinstance(receiver, Mapping)
            and receiver.get("receiver") == index
            and receiver.get("passed") is True
            and receiver.get("clipped_sample_count") == 0
            for index, receiver in enumerate(receivers)
        )
    )


def load_completed_record(
    manifest_path: Path,
    *,
    current_dependency_attestation: Mapping[str, Any] | None = None,
    current_native_attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load one record only through its complete authoritative run manifest."""

    manifest = _read_json(manifest_path, "muted-control run manifest")
    tombstone = runner._tombstone_path(manifest_path)
    if tombstone.exists() or tombstone.is_symlink():
        raise MutedCohortArtifactError(
            "failure tombstone overrides an apparently complete run manifest"
        )
    if (
        manifest.get("schema") != 1
        or manifest.get("run_kind") != "5g8_true_tx_muted_dual_rx_control"
        or manifest.get("status") != "complete"
        or manifest.get("error") is not None
    ):
        raise MutedCohortArtifactError("muted-control run manifest is not complete")
    try:
        runner._validate_run_reservation(manifest_path, manifest=manifest)
        execution_started = runner._validate_execution_burn(manifest_path, manifest=manifest)
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError(
            "muted-control reservation/execution replay barrier differs"
        ) from error
    plan_evidence = manifest.get("immutable_plan")
    if not isinstance(plan_evidence, Mapping):
        raise MutedCohortArtifactError("manifest immutable-plan evidence is malformed")
    plan_path = _evidence_path(plan_evidence.get("path"), "immutable plan")
    _verify_hash(plan_path, plan_evidence.get("plan_file_sha256"), "immutable plan")
    envelope = _read_json(plan_path, "immutable plan")
    contract = envelope.get("plan_contract")
    if (
        envelope.get("schema") != 1
        or envelope.get("immutable") is not True
        or not isinstance(contract, Mapping)
        or envelope.get("plan_contract_sha256") != canonical_json_sha256(contract)
        or envelope.get("plan_contract_sha256") != plan_evidence.get("plan_contract_sha256")
        or contract.get("run_id") != manifest.get("run_id")
    ):
        raise MutedCohortArtifactError("immutable plan envelope or manifest binding differs")
    configuration = contract.get("configuration")
    source = contract.get("source")
    fixture = contract.get("fixture_evidence")
    storage = contract.get("storage")
    if not all(isinstance(value, Mapping) for value in (configuration, source, fixture, storage)):
        raise MutedCohortArtifactError("immutable plan source/fixture/configuration is malformed")
    assert isinstance(configuration, Mapping)
    assert isinstance(source, Mapping)
    assert isinstance(fixture, Mapping)
    assert isinstance(storage, Mapping)
    fixture_sources = fixture.get("fixture_manifest_file")
    setup_sources = fixture.get("setup_attestation_file")
    selector_sources = fixture.get("sealed_selector_flash_evidence_file")
    p0_sources = fixture.get("p0_source_manifest_files")
    if not all(
        isinstance(value, Mapping) for value in (fixture_sources, setup_sources, selector_sources)
    ):
        raise MutedCohortArtifactError("fixture source-file bindings are malformed")
    assert isinstance(fixture_sources, Mapping)
    assert isinstance(setup_sources, Mapping)
    assert isinstance(selector_sources, Mapping)
    if (
        not isinstance(p0_sources, list)
        or len(p0_sources) != 5
        or not all(isinstance(value, Mapping) for value in p0_sources)
    ):
        raise MutedCohortArtifactError("fixture lacks five P0 source-manifest bindings")
    if current_dependency_attestation is not None:
        frozen_dependency = source.get("pluto_plus_utils_source_attestation")
        if frozen_dependency != dict(current_dependency_attestation):
            raise MutedCohortArtifactError(
                "capture pluto-plus-utils source differs from the current clean analyzer source"
            )
    if current_native_attestation is not None:
        frozen_native = source.get("native_libiio_runtime_attestation")
        if frozen_native != dict(current_native_attestation):
            raise MutedCohortArtifactError(
                "capture native libiio runtime differs from the current analyzer runtime"
            )
    try:
        rebuilt_fixture = runner._fixture_evidence_from_files(
            _evidence_path(fixture_sources.get("path"), "fixture manifest"),
            _evidence_path(setup_sources.get("path"), "setup attestation"),
            _evidence_path(selector_sources.get("path"), "Fast20 selector evidence"),
            [
                _evidence_path(value.get("path"), f"P0 source manifest {index}")
                for index, value in enumerate(p0_sources)
            ],
            run_id=str(contract["run_id"]),
            board_id=str(contract["board_id"]),
            serial=str(configuration["serial"]),
            derivation_source_commit=str(source["smateway_commit"]),
        )
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError("fixture evidence failed recursive re-admission") from error
    if rebuilt_fixture != dict(fixture):
        raise MutedCohortArtifactError("fixture evidence differs from recursive re-admission")
    final_mute = manifest.get("final_mute")
    if not runner._mute_passed(
        final_mute,
        serial=str(configuration.get("serial", "")),
        uri=str(configuration.get("uri", "")),
        purpose="final",
    ):
        raise MutedCohortArtifactError("manifest lacks the exact final TX/DDS mute")
    attempt = manifest.get("attempt")
    if not isinstance(attempt, Mapping) or attempt.get("status") != "complete":
        raise MutedCohortArtifactError("manifest lacks one complete capture attempt")
    result = attempt.get("result")
    if not isinstance(result, Mapping):
        raise MutedCohortArtifactError("complete attempt result is malformed")
    record_path = _evidence_path(result.get("record_path"), "muted-control record")
    _verify_hash(record_path, result.get("record_sha256"), "muted-control record")
    record = _read_json(record_path, "muted-control record")
    if (
        record.get("schema") != 1
        or record.get("record_kind") != "5g8_true_tx_muted_control"
        or record.get("run_id") != manifest.get("run_id")
        or record.get("accepted") is not True
        or record.get("immutable_plan") != dict(plan_evidence)
        or record.get("execution_started") != execution_started
        or result.get("execution_started") != execution_started
    ):
        raise MutedCohortArtifactError("record is not bound to the complete run/plan")
    safety = record.get("safety")
    if (
        not isinstance(safety, Mapping)
        or safety.get("final_exact_mute") != final_mute
        or not runner._mute_passed(
            safety.get("post_capture_exact_mute"),
            serial=str(configuration["serial"]),
            uri=str(configuration["uri"]),
            purpose="post_capture",
        )
        or safety.get("headroom_passed") is not True
        or not _headroom_passed(safety.get("adc_headroom_admission"))
        or safety.get("raw_persisted_only_after_post_capture_exact_mute_passed") is not True
        or safety.get("automatic_retry_count") != 0
    ):
        raise MutedCohortArtifactError("record safety, headroom, or exact mutes differ")
    artifact = record.get("artifact_evidence")
    if not isinstance(artifact, Mapping):
        raise MutedCohortArtifactError("record artifact evidence is malformed")
    capture = record.get("capture")
    if not isinstance(capture, Mapping):
        raise MutedCohortArtifactError("record capture identity is malformed")
    capture_bindings = {
        "serial": configuration.get("serial"),
        "uri": configuration.get("uri"),
        "center_frequency_hz": configuration.get("center_frequency_hz"),
        "sample_rate_hz": configuration.get("sample_rate_hz"),
        "bandwidth_hz": configuration.get("bandwidth_hz"),
        "receiver_gain_db": configuration.get("receiver_gain_db"),
        "sample_count": configuration.get("sample_count"),
        "duration_s": configuration.get("duration_s"),
        "samples_per_frame": configuration.get("samples_per_frame"),
        "frame_count": configuration.get("frame_count"),
        "kernel_buffers": configuration.get("kernel_buffers"),
        "metadata_abi": configuration.get("metadata_abi"),
        "tx_source_active": False,
        "receive_only_api": True,
    }
    if any(capture.get(field) != expected for field, expected in capture_bindings.items()):
        raise MutedCohortArtifactError("record capture differs from the immutable plan")
    expected_p0_hash = fixture.get("p0_post_cycle_schedule_proof_sha256")
    if (
        record.get("campaign_id") != contract.get("campaign_id")
        or record.get("source_commit") != source.get("smateway_commit")
        or record.get("dependency_source_attestation_sha256")
        != source.get("pluto_plus_utils_source_attestation_sha256")
        or record.get("native_libiio_runtime_attestation_sha256")
        != source.get("native_libiio_runtime_attestation_sha256")
        or record.get("fixture_evidence_sha256") != contract.get("fixture_evidence_sha256")
        or record.get("cohort_fixture_identity_sha256")
        != fixture.get("cohort_fixture_identity_sha256")
        or record.get("p0_post_cycle_schedule_proof_sha256") != expected_p0_hash
    ):
        raise MutedCohortArtifactError("record provenance differs from the immutable plan")
    sample_count = capture.get("sample_count")
    samples_per_frame = capture.get("samples_per_frame")
    sample_rate_hz = capture.get("sample_rate_hz")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 1
        or isinstance(samples_per_frame, bool)
        or not isinstance(samples_per_frame, int)
        or samples_per_frame < 1
        or isinstance(sample_rate_hz, bool)
        or not isinstance(sample_rate_hz, (int, float))
        or float(sample_rate_hz) <= 0.0
    ):
        raise MutedCohortArtifactError("record capture geometry is malformed")
    artifact_paths: dict[str, Path] = {}
    for name, path_field, hash_field, size_field in (
        ("raw data", "data_path", "data_sha256", "data_size_bytes"),
        (
            "SigMF metadata",
            "metadata_path",
            "metadata_sha256",
            "metadata_size_bytes",
        ),
    ):
        path = _evidence_path(artifact.get(path_field), name)
        _verify_hash(path, artifact.get(hash_field), name)
        _verify_size(path, artifact.get(size_field), name)
        artifact_paths[path_field] = path
    capture_root = Path(str(storage.get("run_capture_root", "")))
    artifact_root = Path(str(artifact.get("path", "")))
    try:
        foundation._assert_path_chain_has_no_symlink(capture_root, label="capture root")
        exact_capture_root = capture_root.resolve(strict=True)
        exact_artifact_root = foundation._assert_tree_has_no_symlink(
            artifact_root, label="muted-control artifact"
        )
    except (OSError, RuntimeError) as error:
        raise MutedCohortArtifactError("artifact path/layout validation failed") from error
    if (
        exact_artifact_root.parent != exact_capture_root
        or any(
            path.resolve(strict=True).parent != exact_artifact_root
            for path in artifact_paths.values()
        )
        or record_path.resolve(strict=True).parent != exact_artifact_root
    ):
        raise MutedCohortArtifactError(
            "record or artifact files escaped the immutable capture root"
        )
    try:
        runner._validate_one_stream_capture_inventory(
            exact_capture_root, artifact_id=str(artifact.get("artifact_id", ""))
        )
    except runner.MutedControlError as error:
        raise MutedCohortArtifactError(
            "one-stream run contains extra sibling or failed artifacts"
        ) from error
    if artifact_paths["data_path"].stat().st_size != sample_count * 2 * 2 * 2:
        raise MutedCohortArtifactError("raw data size is not exact dual-RX CI16")
    metadata = _read_json(artifact_paths["metadata_path"], "SigMF metadata")
    try:
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=sample_count,
            expected_samples_per_block=samples_per_frame,
            expected_sample_rate_hz=float(sample_rate_hz),
        )
    except ValueError as error:
        raise MutedCohortArtifactError(
            f"persisted SigMF ABI2 continuity failed revalidation: {error}"
        ) from error
    if continuity != record.get("continuity_audit"):
        raise MutedCohortArtifactError("record continuity audit differs from persisted SigMF")
    try:
        summary = ArtifactSummary.model_validate(record.get("artifact"))
    except (TypeError, ValueError) as error:
        raise MutedCohortArtifactError("record ArtifactSummary is malformed") from error
    if (
        summary.artifact_id != artifact.get("artifact_id")
        or Path(summary.path).resolve(strict=True) != exact_artifact_root
        or summary.sha256 != artifact.get("data_sha256")
        or not verify_artifact(summary)
    ):
        raise MutedCohortArtifactError("pluto-plus-utils artifact verification failed")
    analysis = record.get("analysis")
    if not isinstance(analysis, Mapping):
        raise MutedCohortArtifactError("record muted-control analysis is malformed")
    try:
        samples = _load_dual_ci16(artifact_paths["data_path"], sample_count=sample_count)
        recomputed_analysis = analyze_muted_stream(
            samples,
            sample_rate_hz=float(sample_rate_hz),
            pilot_offset_hz=float(configuration["pilot_offset_hz_from_p0"]),
        )
    except (OSError, TypeError, ValueError, MutedControlAnalysisError) as error:
        raise MutedCohortArtifactError("raw muted-control PSD reanalysis failed") from error
    finally:
        if "samples" in locals():
            del samples
    if canonical_json_sha256(recomputed_analysis) != canonical_json_sha256(analysis):
        raise MutedCohortArtifactError("stored muted-control PSD differs from raw recomputation")
    if (
        result.get("artifact_data_sha256") != artifact.get("data_sha256")
        or result.get("artifact_metadata_sha256") != artifact.get("metadata_sha256")
        or result.get("stream_id") != capture.get("stream_id")
    ):
        raise MutedCohortArtifactError("manifest result differs from record artifact identity")
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        required=True,
        help="absolute path to one complete T1 manifest; specify exactly five times",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if len(args.manifest) != 5:
            raise MutedCohortArtifactError("exactly five --manifest arguments are required")
        source_commit = foundation._repository_commit_and_require_clean(REPOSITORY, "smateway")
        dependency = attest_pluto_plus_utils_source(
            imported_modules=(
                *PLUTO_PLUS_UTILS_IMPORTED_MODULES,
                ("pluto_plus.tandem", "src/pluto_plus/tandem.py"),
            )
        )
        native = validate_runtime_attestation(native_libiio_runtime_attestation())
        records = [
            load_completed_record(
                path,
                current_dependency_attestation=dependency,
                current_native_attestation=native,
            )
            for path in args.manifest
        ]
        result = aggregate_muted_control_cohort(records)
        if result["source_commit"] != source_commit:
            raise MutedCohortArtifactError(
                "cohort capture source differs from the clean analyzer revision"
            )
        result["analysis_source"] = {
            "smateway_commit": source_commit,
            "clean_worktree_verified": True,
            "pluto_plus_utils_source_attestation": dependency,
            "pluto_plus_utils_source_attestation_sha256": canonical_json_sha256(dependency),
            "native_libiio_runtime_attestation": native,
            "native_libiio_runtime_attestation_sha256": canonical_json_sha256(native),
            "files": [
                {
                    "path": str(path),
                    "sha256": sha256_path(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in (
                    Path(__file__).resolve(),
                    Path(runner.__file__).resolve(),
                    REPOSITORY / "src/smateway/muted_control.py",
                )
            ],
        }
        result["input_manifests"] = [
            {
                "path": str(path.resolve(strict=True)),
                "sha256": sha256_path(path),
                "size_bytes": path.stat().st_size,
                "run_id": record["run_id"],
                "stream_id": record["capture"]["stream_id"],
                "record_sha256": sha256_path(
                    Path(record["artifact_evidence"]["path"]) / runner.RECORD_FILENAME
                ),
                "data_sha256": record["artifact_evidence"]["data_sha256"],
                "metadata_sha256": record["artifact_evidence"]["metadata_sha256"],
                "execution_started": record["execution_started"],
            }
            for path, record in zip(args.manifest, records, strict=True)
        ]
        output = _admit_new_output(args.output)
        foundation._write_immutable_json(output, result)
        try:
            runner._assert_safe_local_path(output, label="muted-control cohort output")
        except runner.MutedControlError as error:
            raise MutedCohortArtifactError("output escaped local storage after write") from error
        if output.is_symlink() or not output.is_file() or output.stat().st_mode & 0o222:
            raise MutedCohortArtifactError("output was not sealed as a regular read-only file")
    except (MutedControlAnalysisError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(
        json.dumps(
            {
                "status": result["cohort_disposition"],
                "output": str(args.output),
                "run_count": 5,
                "stream_count": result["source_distinct_stream_count"],
                "transfer_phase_defined": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
