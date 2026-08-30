#!/usr/bin/env python3
"""Re-admit and aggregate the complete eight-row 5.8-GHz D1 matrix."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE = _REPOSITORY / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from smateway.file_artifact_admission import (  # noqa: E402
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
    read_json_file,
)
from smateway.one_hot_ladder import (  # noqa: E402
    ANTENNA_STATES,
    OneHotMatrixSummary,
    VerifiedOneHotRowBundle,
    summarize_complete_one_hot_matrix,
)

OUTPUT_KIND = "smateway.5g8.verified-one-hot-matrix/v1"
_RUNNER: Any | None = None


class OneHotMatrixAnalysisError(RuntimeError):
    """The supplied rows cannot support an authoritative D1 matrix."""


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _runner() -> Any:
    global _RUNNER
    if _RUNNER is None:
        path = Path(__file__).with_name("run_5g8_one_hot_path_ladder.py")
        spec = importlib.util.spec_from_file_location("smateway_d1_row_verifier", path)
        if spec is None or spec.loader is None:
            raise OneHotMatrixAnalysisError("cannot load the authoritative D1 row verifier")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except (ImportError, SyntaxError) as error:
            sys.modules.pop(spec.name, None)
            raise OneHotMatrixAnalysisError(f"cannot load D1 row verifier: {error}") from error
        _RUNNER = module
    return _RUNNER


def _parse_row(value: str) -> tuple[str, Path, Path]:
    """Parse ``ANTn=/absolute/plan.json,/absolute/manifest.json``."""

    if "=" not in value:
        raise OneHotMatrixAnalysisError("--row must use ANTn=/plan.json,/manifest.json")
    arm, paths = value.split("=", 1)
    raw = paths.split(",")
    if arm not in ANTENNA_STATES or len(raw) != 2:
        raise OneHotMatrixAnalysisError("--row must use ANT1..ANT8=/plan.json,/manifest.json")
    plan, manifest = (Path(item).expanduser() for item in raw)
    if not plan.is_absolute() or not manifest.is_absolute():
        raise OneHotMatrixAnalysisError("D1 plan and manifest paths must be absolute")
    return arm, plan, manifest


def _configuration(rows: Sequence[VerifiedOneHotRowBundle]) -> Mapping[str, Any]:
    if not rows:
        raise OneHotMatrixAnalysisError("D1 matrix has no verified rows")
    identity = rows[0].document.get("matrix_identity")
    if not isinstance(identity, Mapping):
        raise OneHotMatrixAnalysisError("D1 row lacks its matrix identity")
    configuration = identity.get("acquisition_configuration")
    if not isinstance(configuration, Mapping):
        raise OneHotMatrixAnalysisError("D1 row lacks acquisition configuration")
    return configuration


def _recompute_current_execution_identity(runner: Any) -> dict[str, Any]:
    """Rebuild the complete execution identity before aggregating any D1 row."""

    try:
        commit = runner.leakage._repository_commit_and_require_clean(
            _REPOSITORY,
            "smateway analyzer",
        )
        dependency = runner.leakage._validate_dependency_source_attestation(
            runner.attest_pluto_plus_utils_source()
        )
        native = runner.validate_runtime_attestation(runner.attest_runtime())
    except Exception as error:
        raise OneHotMatrixAnalysisError(
            f"cannot re-attest current analysis source/native closure: {error}"
        ) from error
    return {
        "smateway_commit": commit,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": _canonical_sha256(dependency),
        "native_libiio_runtime_attestation": native,
        "native_libiio_runtime_attestation_sha256": _canonical_sha256(native),
        "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
        "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
        "runner": "scripts/run_5g8_one_hot_path_ladder.py",
        "run_aggregator": "smateway.one_hot_ladder.summarize_one_hot_run",
        "matrix_aggregator": "smateway.one_hot_ladder.summarize_complete_one_hot_matrix",
    }


def _require_current_execution_identity(
    contract: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
) -> None:
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise OneHotMatrixAnalysisError("D1 row lacks frozen source identity")
    dependency = current.get("pluto_plus_utils_source_attestation")
    native = current.get("native_libiio_runtime_attestation")
    if not isinstance(dependency, Mapping) or not isinstance(native, Mapping):
        raise OneHotMatrixAnalysisError("current D1 execution identity is malformed")
    dependency_hash = _canonical_sha256(dependency)
    native_hash = _canonical_sha256(native)
    if current.get("smateway_commit") != source.get("smateway_commit"):
        raise OneHotMatrixAnalysisError(
            "current clean Smateway source closure differs from the D1 plan"
        )
    if dependency != source.get(
        "pluto_plus_utils_source_attestation"
    ) or dependency_hash != source.get("pluto_plus_utils_source_attestation_sha256"):
        raise OneHotMatrixAnalysisError(
            "current pluto-plus-utils source closure differs from the D1 plan"
        )
    if native != source.get("native_libiio_runtime_attestation") or native_hash != source.get(
        "native_libiio_runtime_attestation_sha256"
    ):
        raise OneHotMatrixAnalysisError("current native libiio identity differs from the D1 plan")
    if dict(current) != dict(source):
        raise OneHotMatrixAnalysisError(
            "current complete analysis source contract differs from the D1 plan"
        )


def analyze_rows(
    row_paths: Sequence[tuple[str, Path, Path]],
) -> tuple[OneHotMatrixSummary, tuple[VerifiedOneHotRowBundle, ...]]:
    """Load every row through the capture runner's recursive file verifier."""

    if len(row_paths) != len(ANTENNA_STATES):
        raise OneHotMatrixAnalysisError("D1 requires exactly eight row plan/manifest pairs")
    if tuple(item[0] for item in row_paths) != ANTENNA_STATES:
        raise OneHotMatrixAnalysisError("D1 rows must be supplied once in ANT1..ANT8 order")
    flattened = [path.absolute() for _, plan, manifest in row_paths for path in (plan, manifest)]
    if len(set(flattened)) != 16:
        raise OneHotMatrixAnalysisError("D1 row inputs reuse a plan or manifest path")
    authoritative_runner = _runner()
    current_identity = _recompute_current_execution_identity(authoritative_runner)
    loader = authoritative_runner.load_verified_one_hot_row_bundle
    verified: list[VerifiedOneHotRowBundle] = []
    for expected_arm, plan_path, manifest_path in row_paths:
        plan_document = read_json_file(plan_path, label=f"{expected_arm} D1 plan")
        contract = plan_document.get("plan_contract")
        if not isinstance(contract, Mapping):
            raise OneHotMatrixAnalysisError(f"{expected_arm} D1 plan contract is malformed")
        _require_current_execution_identity(contract, current=current_identity)
        storage = contract.get("storage") if isinstance(contract, Mapping) else None
        if (
            not isinstance(storage, Mapping)
            or storage.get("medium") != "raspberry_pi_local_filesystem"
            or storage.get("pluto_onboard_storage_used") is not False
            or not isinstance(storage.get("run_capture_root"), str)
            or not Path(str(storage["run_capture_root"])).is_absolute()
        ):
            raise OneHotMatrixAnalysisError(f"{expected_arm} lacks local-RPi storage evidence")
        assert_local_rpi_storage(plan_path, label=f"{expected_arm} D1 plan storage")
        assert_local_rpi_storage(
            Path(str(storage["run_capture_root"])),
            label=f"{expected_arm} D1 capture storage",
        )
        bundle = loader(plan_path=plan_path, manifest_path=manifest_path)
        if bundle.document.get("driven_input") != expected_arm:
            raise OneHotMatrixAnalysisError(f"{expected_arm} input paths contain another row")
        verified.append(bundle)
    configuration = _configuration(verified)
    try:
        gains = configuration["tx_hardware_gains_db"]
        attribution_gain = configuration["attribution_tx_hardware_gain_db"]
        repeat_count = configuration["attribution_repeat_count"]
        minimum_repeats = configuration["minimum_detected_attribution_repeats"]
        minimum_contrast = configuration["minimum_intended_through_contrast_over_all_off_db"]
        maximum_span = configuration["maximum_attribution_amplitude_span_db"]
        maximum_phase = configuration["maximum_attribution_phase_residual_deg"]
    except KeyError as error:
        raise OneHotMatrixAnalysisError(
            f"D1 acquisition configuration lacks {error.args[0]}"
        ) from error
    if not isinstance(gains, Sequence) or isinstance(gains, (str, bytes)):
        raise OneHotMatrixAnalysisError("D1 planned gain ladder is malformed")
    summary = summarize_complete_one_hot_matrix(
        verified,
        planned_gains_db=gains,
        attribution_gain_db=float(attribution_gain),
        attribution_repeat_count=int(repeat_count),
        minimum_detected_attribution_repeats=int(minimum_repeats),
        minimum_intended_through_contrast_over_all_off_db=float(minimum_contrast),
        maximum_attribution_amplitude_span_db=float(maximum_span),
        maximum_attribution_phase_residual_deg=float(maximum_phase),
    )
    return summary, tuple(verified)


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    output = assert_no_symlink_chain(path.expanduser().absolute(), label="D1 output")
    assert_local_rpi_storage(output, label="D1 output storage")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_no_symlink_chain(output.parent, label="D1 output parent")
    payload = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
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
        "--row",
        action="append",
        required=True,
        metavar="ANTn=/absolute/plan.json,/absolute/manifest.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        row_paths = tuple(_parse_row(value) for value in args.row)
        summary, rows = analyze_rows(row_paths)
        document = {
            "schema": 1,
            "analysis_kind": OUTPUT_KIND,
            "row_order": list(ANTENNA_STATES),
            "summary": asdict(summary),
            "input_rows": [
                {
                    "driven_input": row.document["driven_input"],
                    "verified_bundle_sha256": row.canonical_sha256,
                    "run_id": row.document["run_id"],
                    "plan_sha256": row.document["plan_file_sha256"],
                    "manifest_sha256": row.document["manifest_sha256"],
                }
                for row in rows
            ],
            "recursive_admission": {
                "plan_manifest_condition_record_raw_iq_metadata_reverified": True,
                "abi2_continuity_reaudited": True,
                "selector_fixture_source_native_identity_reverified": True,
                "failure_or_quarantine_artifacts_accepted": False,
            },
            "analysis_hardware_activity": False,
        }
        _write_new(args.output, document)
    except (FileArtifactAdmissionError, OneHotMatrixAnalysisError, OSError, ValueError) as error:
        print(
            json.dumps({"status": "failed", "error": f"{type(error).__name__}: {error}"}),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "complete",
                "output": str(args.output.expanduser().absolute()),
                "verified_row_count": 8,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
