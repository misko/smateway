"""Sealed, recursively reopenable evidence for legacy P0 normalization.

The legacy frequency-sweep manifest stores its acquisition plan inline.  A P0
normalization therefore binds that exact ``/plan`` value together with the
manifest, reference-transfer analysis, SigMF metadata, and raw IQ files.  The
normalized envelope is create-only and read-only; admission reopens and hashes
every source rather than trusting copied identity fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from smateway.file_artifact_admission import (
    assert_local_rpi_storage,
    assert_no_symlink_chain,
    read_json_file,
    verify_file_binding,
    verify_source_tree_binding,
)
from smateway.hexcal import sha256_path
from smateway.input_off_control import (
    CENTER_FREQUENCY_HZ,
    DURATION_S,
    KERNEL_BUFFERS,
    RECEIVER_GAIN_DB,
    SAMPLE_RATE_HZ,
    TOTAL_SAMPLES,
    InputOffObservation,
    validate_observation,
)

SCHEMA = 2
EVIDENCE_KIND = "5g8_legacy_p0_normalized_evidence_v2"
COMMIT_LENGTH = 40
HEX = frozenset("0123456789abcdef")
LEGACY_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
LEGACY_RUNNER_USER = "smateway-rf"
LEGACY_RUNNER_UID = 990
LEGACY_RUNNER_GID = 990
LEGACY_RUNNER_HOME = Path("/var/lib/smateway-rf")
LEGACY_RUNNER_SHELL = Path("/usr/sbin/nologin")
LEGACY_AUTHORITATIVE_BOARDS_ROOT = LEGACY_RUNNER_HOME / ".local/state/smateway/boards"
LEGACY_CAPTURE_PROGRAM = "scripts/capture_fast20_dwell.py"
LEGACY_REANALYSIS_PROGRAM = "scripts/reanalyze_fast20_reference_transfer_artifact.py"
LEGACY_REFERENCE_ANALYSIS_FILENAME = "fast20-reference-transfer-v2.json"
LEGACY_ARTIFACT_TOKEN = "{artifact_id}"
LEGACY_FIXTURE_ID = "tx1-2way-rx1-and-8way-board-rx2-v1"
LEGACY_PROFILE_ID = "fast20-v1"
LEGACY_PROFILE_CONTRACT_SHA256 = "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
LEGACY_FIRMWARE_BINARY_SHA256 = "aeaed9d2f892d2a59add1aba2a7477e349b750c99f81610632286d04d91326ac"
LEGACY_FREQUENCIES_HZ = tuple(range(2_100_000_000, 5_800_000_001, 100_000_000))
LEGACY_CLOSURE_FREQUENCIES_HZ = (
    2_100_000_000,
    2_400_000_000,
    3_000_000_000,
    4_000_000_000,
    5_000_000_000,
    5_800_000_000,
)
LEGACY_STAGES = ("rotation0", "rotation1", "rotation2", "closure0")
LEGACY_ROTATION_BY_STAGE = {"rotation0": 0, "rotation1": 1, "rotation2": 2, "closure0": 0}


class P0NormalizedEvidenceError(ValueError):
    """A normalized P0 envelope or one of its bound sources is inadmissible."""


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise P0NormalizedEvidenceError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    if set(value) != set(expected):
        raise P0NormalizedEvidenceError(f"{label} fields are incomplete or unexpected")


def _legacy_mapping(rotation: int) -> dict[str, str]:
    return {f"F{index + 1}": f"ANT{(index + rotation) % 8 + 1}" for index in range(8)}


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise P0NormalizedEvidenceError(f"{label} must be a nonempty string")
    return value


def _required_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise P0NormalizedEvidenceError(f"{label} must be an integer")
    return value


def _legacy_authoritative_boards_root(*, test_only_legacy_boards_root: Path | None) -> Path:
    """Return the one board-state base admitted by this process.

    Production callers cannot select a root: the fixed service-account record
    must exist and exactly match the reviewed identity.  Offline unit tests may
    inject an explicit local root through Python APIs; the production CLI never
    exposes or populates that keyword.
    """

    if test_only_legacy_boards_root is not None:
        if not test_only_legacy_boards_root.is_absolute():
            raise P0NormalizedEvidenceError("test-only legacy P0 boards root must be absolute")
        boards_root = assert_no_symlink_chain(
            test_only_legacy_boards_root,
            label="test-only legacy P0 authoritative boards root",
        )
        assert_local_rpi_storage(
            boards_root,
            label="test-only legacy P0 authoritative boards storage",
        )
        return boards_root

    try:
        account = pwd.getpwnam(LEGACY_RUNNER_USER)
    except KeyError as error:
        raise P0NormalizedEvidenceError(
            f"fixed legacy P0 runner account is unavailable: {LEGACY_RUNNER_USER}"
        ) from error
    expected_identity = (
        LEGACY_RUNNER_USER,
        LEGACY_RUNNER_UID,
        LEGACY_RUNNER_GID,
        str(LEGACY_RUNNER_HOME),
        str(LEGACY_RUNNER_SHELL),
    )
    observed_identity = (
        account.pw_name,
        account.pw_uid,
        account.pw_gid,
        account.pw_dir,
        account.pw_shell,
    )
    if observed_identity != expected_identity:
        raise P0NormalizedEvidenceError(
            "fixed legacy P0 runner account identity differs from the reviewed contract"
        )
    boards_root = assert_no_symlink_chain(
        LEGACY_AUTHORITATIVE_BOARDS_ROOT,
        label="legacy P0 authoritative boards root",
    )
    assert_local_rpi_storage(
        boards_root,
        label="legacy P0 authoritative boards storage",
    )
    return boards_root


def _validate_legacy_configuration(
    value: object, *, test_only_legacy_boards_root: Path | None = None
) -> dict[str, Any]:
    configuration = _mapping(value, "legacy P0 configuration")
    frequencies_document = configuration.get("frequencies_hz")
    if not isinstance(frequencies_document, list) or not frequencies_document:
        raise P0NormalizedEvidenceError("legacy P0 configuration.frequencies_hz is malformed")
    frequencies = tuple(
        _required_integer(item, "legacy P0 configured frequency") for item in frequencies_document
    )
    if (
        any(frequency not in LEGACY_FREQUENCIES_HZ for frequency in frequencies)
        or frequencies
        != LEGACY_FREQUENCIES_HZ[
            LEGACY_FREQUENCIES_HZ.index(frequencies[0]) : LEGACY_FREQUENCIES_HZ.index(
                frequencies[-1]
            )
            + 1
        ]
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 configuration.frequencies_hz is not one contiguous 100-MHz grid"
        )
    board_id = _required_string(configuration.get("board_id"), "legacy P0 board ID")
    if board_id in {".", ".."} or Path(board_id).name != board_id:
        raise P0NormalizedEvidenceError("legacy P0 board ID is unsafe")
    serial = _required_string(configuration.get("serial"), "legacy P0 Pluto serial")
    uri = _required_string(configuration.get("uri"), "legacy P0 Pluto URI")
    timeout_s = _required_integer(configuration.get("timeout_s"), "legacy P0 timeout")
    if not 30 <= timeout_s <= 600:
        raise P0NormalizedEvidenceError("legacy P0 configuration.timeout_s differs")
    board_state_value = _required_string(
        configuration.get("board_state_root"), "legacy P0 board-state root"
    )
    artifact_storage_value = _required_string(
        configuration.get("artifact_storage_root"), "legacy P0 artifact-storage root"
    )
    if not Path(board_state_value).is_absolute() or not Path(artifact_storage_value).is_absolute():
        raise P0NormalizedEvidenceError("legacy P0 storage roots must be absolute")
    board_state_root = assert_no_symlink_chain(
        Path(board_state_value), label="legacy P0 board-state root"
    )
    artifact_storage_root = assert_no_symlink_chain(
        Path(artifact_storage_value), label="legacy P0 artifact-storage root"
    )
    assert_local_rpi_storage(board_state_root, label="legacy P0 board-state storage")
    assert_local_rpi_storage(artifact_storage_root, label="legacy P0 artifact storage")
    authoritative_boards_root = _legacy_authoritative_boards_root(
        test_only_legacy_boards_root=test_only_legacy_boards_root
    )
    expected_board_state_root = authoritative_boards_root / board_id
    if (
        str(board_state_root) != board_state_value
        or board_state_root != expected_board_state_root
        or artifact_storage_root != board_state_root / "pluto-usb-captures"
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 configured storage roots differ from the authoritative runner root"
        )
    closure_frequencies = [
        frequency for frequency in LEGACY_CLOSURE_FREQUENCIES_HZ if frequency in frequencies
    ]
    planned_capture_count = 3 * len(frequencies) + len(closure_frequencies)
    expected = {
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "frequencies_hz": list(frequencies),
        "closure_frequencies_hz": closure_frequencies,
        "stages": list(LEGACY_STAGES),
        "mappings": {
            stage: _legacy_mapping(LEGACY_ROTATION_BY_STAGE[stage]) for stage in LEGACY_STAGES
        },
        "fixture_id": LEGACY_FIXTURE_ID,
        "fully_conducted_required": True,
        "tx_channel": 0,
        "stimulus": "qualification",
        "receiver_gain_db": int(RECEIVER_GAIN_DB),
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "duration_s": DURATION_S,
        "kernel_buffers": KERNEL_BUFFERS,
        "planned_capture_count": planned_capture_count,
        "estimated_raw_iq_bytes": planned_capture_count * TOTAL_SAMPLES * 2 * 4,
        "profile_id": LEGACY_PROFILE_ID,
        "profile_contract_sha256": LEGACY_PROFILE_CONTRACT_SHA256,
        "firmware_binary_sha256": LEGACY_FIRMWARE_BINARY_SHA256,
        "board_id": board_id,
        "serial": serial,
        "uri": uri,
        "python": str(LEGACY_PINNED_PYTHON),
        "timeout_s": timeout_s,
        "storage_medium": "raspberry_pi_local_filesystem",
        "board_state_root": str(board_state_root),
        "artifact_storage_root": str(artifact_storage_root),
        "pluto_onboard_storage_used": False,
    }
    if set(configuration) != set(expected):
        raise P0NormalizedEvidenceError(
            "legacy P0 configuration fields are incomplete or unexpected"
        )
    for field, expected_value in expected.items():
        if configuration.get(field) != expected_value:
            raise P0NormalizedEvidenceError(f"legacy P0 configuration.{field} differs")
    return cast(dict[str, Any], json.loads(json.dumps(expected, allow_nan=False)))


def reconstruct_legacy_closed_loop_plan(
    configuration: Mapping[str, Any],
    *,
    expected_repository: Path,
    test_only_legacy_boards_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct the exact immutable plan emitted by the legacy sweep runner."""

    validated = _validate_legacy_configuration(
        configuration,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    repository = assert_no_symlink_chain(
        expected_repository.expanduser().absolute(), label="legacy P0 repository"
    )
    python = str(LEGACY_PINNED_PYTHON)
    plan: list[dict[str, Any]] = []
    for stage in LEGACY_STAGES:
        frequencies = (
            validated["closure_frequencies_hz"]
            if stage == "closure0"
            else validated["frequencies_hz"]
        )
        rotation = LEGACY_ROTATION_BY_STAGE[stage]
        for order_index, frequency_hz in enumerate(frequencies):
            condition: dict[str, Any] = {
                "plan_index": len(plan),
                "stage": stage,
                "rotation": rotation,
                "stage_order_index": order_index,
                "center_frequency_hz": frequency_hz,
                "tx_channel": 0,
                "mapping": _legacy_mapping(rotation),
                "receiver_gain_db": int(RECEIVER_GAIN_DB),
                "stimulus": "qualification",
                "sample_rate_hz": SAMPLE_RATE_HZ,
            }
            condition["capture_command"] = [
                python,
                str(repository / LEGACY_CAPTURE_PROGRAM),
                "--tx-channel",
                "0",
                "--stimulus",
                "qualification",
                "--receiver-gain-db",
                str(int(RECEIVER_GAIN_DB)),
                "--sample-rate-hz",
                str(SAMPLE_RATE_HZ),
                "--center-frequency-hz",
                str(frequency_hz),
                "--board-id",
                str(validated["board_id"]),
                "--serial",
                str(validated["serial"]),
                "--uri",
                str(validated["uri"]),
                "--allow-conducted-calibration-sweep",
                "--conducted-fixture-id",
                LEGACY_FIXTURE_ID,
                "--confirm-fully-conducted",
            ]
            condition["reference_reanalysis_command_template"] = [
                python,
                str(repository / LEGACY_REANALYSIS_PROGRAM),
                LEGACY_ARTIFACT_TOKEN,
                "--board-id",
                str(validated["board_id"]),
            ]
            plan.append(condition)
    return plan


def validate_legacy_p0_plan(
    manifest: Mapping[str, Any],
    *,
    expected_repository: Path,
    test_only_legacy_boards_root: Path | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Require the embedded plan to equal a fresh closed-loop reconstruction."""

    configuration = _validate_legacy_configuration(
        manifest.get("configuration"),
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    expected_plan = reconstruct_legacy_closed_loop_plan(
        configuration,
        expected_repository=expected_repository,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    if manifest.get("plan") != expected_plan:
        raise P0NormalizedEvidenceError(
            "legacy P0 plan differs from the reconstructed immutable closed-loop plan"
        )
    matching_rows = [
        row
        for row in expected_plan
        if row.get("stage") == "rotation0"
        and row.get("rotation") == 0
        and row.get("center_frequency_hz") == CENTER_FREQUENCY_HZ
    ]
    if len(matching_rows) != 1:
        raise P0NormalizedEvidenceError("legacy P0 plan lacks one exact Rotation-0 5.8-GHz row")
    return configuration, matching_rows[0]


def validate_legacy_p0_execution_identity(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    artifact_id: str,
    expected_repository: Path,
    test_only_legacy_boards_root: Path | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Bind the accepted attempt and recorded commands to the immutable plan."""

    configuration, condition = validate_legacy_p0_plan(
        manifest,
        expected_repository=expected_repository,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    final_mute = manifest.get("final_mute")
    if (
        manifest.get("schema") != 1
        or manifest.get("experiment_kind") != configuration["experiment_kind"]
        or manifest.get("run_id") != run_id
        or manifest.get("status") != "awaiting_rotation1"
        or not isinstance(final_mute, Mapping)
        or final_mute.get("status") != "passed"
        or final_mute.get("purpose") != "final_rotation0"
        or final_mute.get("error") is not None
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 manifest did not finish Rotation 0 and final mute"
        )
    if len(artifact_id) != 32 or any(character not in HEX for character in artifact_id):
        raise P0NormalizedEvidenceError("legacy P0 artifact ID is malformed")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        raise P0NormalizedEvidenceError("legacy P0 attempts are missing")
    matches = [
        item
        for item in attempts
        if isinstance(item, Mapping)
        and item.get("artifact_id") == artifact_id
        and item.get("center_frequency_hz") == CENTER_FREQUENCY_HZ
        and item.get("rotation") == 0
        and item.get("stage") == "rotation0"
    ]
    if len(matches) != 1:
        raise P0NormalizedEvidenceError("legacy P0 manifest has no unique exact-5.8-GHz attempt")
    attempt = matches[0]
    if any(attempt.get(field) != value for field, value in condition.items()):
        raise P0NormalizedEvidenceError(
            "legacy P0 attempt differs from its immutable closed-loop plan row"
        )
    capture = _mapping(attempt.get("capture"), "legacy P0 capture attempt")
    reanalysis = _mapping(attempt.get("reanalysis"), "legacy P0 reanalysis attempt")
    quality = _mapping(attempt.get("quality_result"), "legacy P0 quality result")
    expected_reanalysis = [
        artifact_id if item == LEGACY_ARTIFACT_TOKEN else item
        for item in condition["reference_reanalysis_command_template"]
    ]
    parsed_reanalysis = reanalysis.get("parsed_output")
    if (
        attempt.get("status") != "complete"
        or attempt.get("outcome") != "quality_passed"
        or attempt.get("failure_kind") is not None
        or attempt.get("error") is not None
        or capture.get("status") != "complete"
        or capture.get("accepted") is not True
        or capture.get("timed_out") is not False
        or capture.get("return_code") not in {0, 2, 3}
        or capture.get("command") != condition["capture_command"]
        or reanalysis.get("status") != "complete"
        or reanalysis.get("accepted") is not True
        or reanalysis.get("timed_out") is not False
        or reanalysis.get("return_code") != 0
        or reanalysis.get("command") != expected_reanalysis
        or not isinstance(parsed_reanalysis, Mapping)
        or parsed_reanalysis.get("artifact_id") != artifact_id
        or parsed_reanalysis.get("quality_passed") is not True
        or quality.get("status") != "passed"
        or quality.get("quality_passed") is not True
        or quality.get("analysis_kind") != "fast20_dual_rx_ota_reference_transfer"
        or quality.get("artifact_id") != artifact_id
        or quality.get("tx_channel") != 0
        or quality.get("center_frequency_hz") != CENTER_FREQUENCY_HZ
        or quality.get("receiver_gain_db") != int(RECEIVER_GAIN_DB)
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 5.8-GHz attempt commands/results are not exact and accepted"
        )
    post_mute = attempt.get("post_mute")
    if (
        not isinstance(post_mute, Mapping)
        or post_mute.get("status") != "passed"
        or post_mute.get("purpose") != "post_attempt"
        or post_mute.get("error") is not None
    ):
        raise P0NormalizedEvidenceError("legacy P0 attempt did not finish fail-muted")
    return configuration, condition, attempt


def _file_identity(path: Path, *, label: str) -> dict[str, Any]:
    exact = assert_no_symlink_chain(path.expanduser().absolute(), label=label)
    assert_local_rpi_storage(exact, label=f"{label} storage")
    if exact.is_symlink() or not exact.is_file():
        raise P0NormalizedEvidenceError(f"{label} must be a regular non-symlink file")
    size = exact.stat().st_size
    if size <= 0:
        raise P0NormalizedEvidenceError(f"{label} must not be empty")
    return {"path": str(exact), "sha256": sha256_path(exact), "size_bytes": size}


def _normalizer_source(
    value: object,
    *,
    expected_repository: Path | None,
    expected_commit: str | None,
    required_source_paths: tuple[str, ...],
) -> dict[str, Any]:
    source = _mapping(value, "P0 normalizer source")
    required = {
        "schema",
        "repository",
        "commit",
        "clean_worktree_verified",
        "files",
        "source_files_sha256",
        "analyzer_runtime_attestation",
        "analyzer_runtime_attestation_sha256",
        "smateway_import_origin_attestation",
        "smateway_import_origin_attestation_sha256",
        "pluto_plus_utils_source_attestation",
        "pluto_plus_utils_source_attestation_sha256",
        "native_libiio_runtime_attestation",
        "native_libiio_runtime_attestation_sha256",
    }
    if not required <= set(source):
        raise P0NormalizedEvidenceError("P0 normalizer source fields are incomplete")
    repository_value = source.get("repository")
    commit = source.get("commit")
    files = source.get("files")
    runtime = source.get("analyzer_runtime_attestation")
    imports = source.get("smateway_import_origin_attestation")
    dependency = source.get("pluto_plus_utils_source_attestation")
    native = source.get("native_libiio_runtime_attestation")
    if (
        source.get("schema") != 1
        or source.get("clean_worktree_verified") is not True
        or not isinstance(repository_value, str)
        or not Path(repository_value).is_absolute()
        or not isinstance(commit, str)
        or len(commit) != COMMIT_LENGTH
        or any(character not in HEX for character in commit)
        or not isinstance(files, list)
        or not files
        or source.get("source_files_sha256") != _canonical_sha256(files)
        or not isinstance(runtime, Mapping)
        or source.get("analyzer_runtime_attestation_sha256") != _canonical_sha256(runtime)
        or not isinstance(imports, Mapping)
        or source.get("smateway_import_origin_attestation_sha256") != _canonical_sha256(imports)
        or not isinstance(dependency, Mapping)
        or source.get("pluto_plus_utils_source_attestation_sha256") != _canonical_sha256(dependency)
        or not isinstance(native, Mapping)
        or source.get("native_libiio_runtime_attestation_sha256") != _canonical_sha256(native)
    ):
        raise P0NormalizedEvidenceError("P0 normalizer source identity is invalid")
    repository = assert_no_symlink_chain(Path(repository_value), label="P0 normalizer repository")
    if (
        expected_repository is not None
        and repository != expected_repository.expanduser().absolute()
    ):
        raise P0NormalizedEvidenceError("P0 normalizer repository differs from current Smateway")
    if expected_commit is not None and commit != expected_commit:
        raise P0NormalizedEvidenceError("P0 normalizer commit differs from frozen Smateway")
    runtime_path = runtime.get("sys_path")
    runtime_pythonpath = runtime.get("pythonpath")
    executable = runtime.get("python_executable")
    prefix = runtime.get("python_prefix")
    if (
        runtime.get("schema") != 1
        or runtime.get("smateway_source_first") is not True
        or not isinstance(runtime_path, list)
        or not runtime_path
        or runtime_path[0] != str(repository / "src")
        or runtime_pythonpath != [str(repository / "src")]
        or not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not isinstance(prefix, str)
        or not Path(prefix).is_absolute()
        or Path(prefix) not in Path(executable).parents
    ):
        raise P0NormalizedEvidenceError("P0 analyzer runtime attestation is invalid")
    import_rows = imports.get("modules")
    if (
        imports.get("schema") != 1
        or imports.get("repository") != str(repository)
        or imports.get("commit") != commit
        or imports.get("source_files_sha256") != source.get("source_files_sha256")
        or not isinstance(import_rows, list)
        or imports.get("modules_sha256") != _canonical_sha256(import_rows)
    ):
        raise P0NormalizedEvidenceError("P0 Smateway import-origin attestation is invalid")
    file_bindings = {
        item.get("path"): item
        for item in files
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    expected_modules: dict[str, Mapping[str, Any]] = {}
    for raw_relative, binding in file_bindings.items():
        if not isinstance(raw_relative, str):
            continue
        relative = raw_relative
        if relative == "src/smateway/__init__.py":
            expected_modules["smateway"] = binding
        elif relative.startswith("src/smateway/") and relative.endswith(".py"):
            expected_modules[f"smateway.{Path(relative).stem}"] = binding
    observed_modules: dict[str, Mapping[str, Any]] = {}
    for raw_row in import_rows:
        if not isinstance(raw_row, Mapping) or not isinstance(raw_row.get("module"), str):
            raise P0NormalizedEvidenceError("P0 Smateway import-origin row is malformed")
        name = str(raw_row["module"])
        if name in observed_modules:
            raise P0NormalizedEvidenceError("P0 Smateway import-origin modules are duplicated")
        observed_modules[name] = raw_row
    if set(observed_modules) != set(expected_modules):
        raise P0NormalizedEvidenceError(
            "P0 imported Smateway modules differ from frozen implementation bindings"
        )
    for name, binding in expected_modules.items():
        relative = str(binding["path"])
        expected_row = {
            "module": name,
            "relative_path": relative,
            "origin": str(repository / relative),
            "sha256": binding.get("sha256"),
            "size_bytes": binding.get("size_bytes"),
        }
        if dict(observed_modules[name]) != expected_row:
            raise P0NormalizedEvidenceError(
                f"P0 Smateway import origin differs from frozen binding: {name}"
            )
    verify_source_tree_binding(
        source,
        label="P0 normalizer",
        required_relative_paths=required_source_paths,
    )
    return cast(dict[str, Any], json.loads(json.dumps(source, sort_keys=True, allow_nan=False)))


def build_normalized_p0_envelope(
    observation: Mapping[str, Any],
    *,
    manifest_path: Path,
    analysis_path: Path,
    metadata_path: Path,
    raw_iq_path: Path,
    normalizer_source: Mapping[str, Any],
    test_only_legacy_boards_root: Path | None = None,
) -> dict[str, Any]:
    """Build a source-bound envelope after domain-specific P0 normalization."""

    normalized = validate_observation(observation, expected_cohort="P0")
    manifest_identity = _file_identity(manifest_path, label="legacy P0 manifest")
    analysis_identity = _file_identity(analysis_path, label="legacy P0 analysis")
    metadata_identity = _file_identity(metadata_path, label="legacy P0 SigMF metadata")
    raw_identity = _file_identity(raw_iq_path, label="legacy P0 raw IQ")
    manifest = read_json_file(Path(manifest_identity["path"]), label="legacy P0 manifest")
    plan = manifest.get("plan")
    if not isinstance(plan, (list, dict)) or not plan:
        raise P0NormalizedEvidenceError("legacy P0 manifest has no exact embedded plan")
    source = _normalizer_source(
        normalizer_source,
        expected_repository=Path(str(normalizer_source.get("repository", ""))),
        expected_commit=str(normalizer_source.get("commit", "")),
        required_source_paths=(),
    )
    envelope = {
        "schema": SCHEMA,
        "evidence_kind": EVIDENCE_KIND,
        "immutable": True,
        "observation": json.loads(json.dumps(observation, sort_keys=True, allow_nan=False)),
        "source_artifacts": {
            "legacy_plan": {
                "source": "legacy_manifest",
                "json_pointer": "/plan",
                "canonical_sha256": _canonical_sha256(plan),
            },
            "legacy_manifest": manifest_identity,
            "reference_transfer_analysis": analysis_identity,
            "sigmf_metadata": metadata_identity,
            "raw_iq": raw_identity,
        },
        "normalizer_source": source,
    }
    if normalized.artifact_sha256 != raw_identity["sha256"]:
        raise P0NormalizedEvidenceError("normalized P0 raw-IQ hash differs from bound source")
    read_json_file(Path(metadata_identity["path"]), label="legacy P0 SigMF metadata")
    _verify_artifact_graph(
        observation=normalized,
        manifest=manifest,
        analysis=read_json_file(Path(analysis_identity["path"]), label="legacy P0 analysis"),
        source_artifacts=envelope["source_artifacts"],
        expected_repository=Path(str(source["repository"])),
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    return envelope


def _verify_artifact_graph(
    *,
    observation: InputOffObservation,
    manifest: Mapping[str, Any],
    analysis: Mapping[str, Any],
    source_artifacts: Mapping[str, Any],
    expected_repository: Path,
    test_only_legacy_boards_root: Path | None = None,
) -> None:
    configuration, _condition, attempt = validate_legacy_p0_execution_identity(
        manifest,
        run_id=observation.run_id,
        artifact_id=observation.artifact_id,
        expected_repository=expected_repository,
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    if manifest.get("runner_source_commit") != observation.source_commit:
        raise P0NormalizedEvidenceError(
            "legacy P0 runner commit differs from the normalized observation"
        )
    if configuration.get("profile_contract_sha256") != observation.profile_contract_sha256:
        raise P0NormalizedEvidenceError(
            "legacy P0 profile binding differs from the normalized observation"
        )
    artifact = _mapping(analysis.get("artifact"), "legacy P0 analysis artifact")
    artifact_root_value = artifact.get("path")
    artifact_id = artifact.get("artifact_id")
    if (
        not isinstance(artifact_root_value, str)
        or not Path(artifact_root_value).is_absolute()
        or artifact_id != observation.artifact_id
    ):
        raise P0NormalizedEvidenceError("legacy P0 analysis artifact identity differs")
    artifact_root = assert_no_symlink_chain(
        Path(artifact_root_value), label="legacy P0 artifact root"
    )
    storage_root = assert_no_symlink_chain(
        Path(str(configuration["artifact_storage_root"])),
        label="legacy P0 artifact-storage root",
    )
    expected_raw = artifact_root / f"{artifact_id}.sigmf-data"
    expected_metadata = artifact_root / f"{artifact_id}.sigmf-meta"
    expected_analysis = artifact_root / LEGACY_REFERENCE_ANALYSIS_FILENAME
    raw_binding = _mapping(source_artifacts.get("raw_iq"), "legacy P0 raw-IQ binding")
    metadata_binding = _mapping(
        source_artifacts.get("sigmf_metadata"), "legacy P0 metadata binding"
    )
    analysis_binding = _mapping(
        source_artifacts.get("reference_transfer_analysis"),
        "legacy P0 analysis binding",
    )
    exact_paths = {
        "artifact root": artifact_root,
        "raw IQ": Path(str(raw_binding.get("path"))),
        "SigMF metadata": Path(str(metadata_binding.get("path"))),
        "reference analysis": Path(str(analysis_binding.get("path"))),
    }
    for label, path in exact_paths.items():
        exact = assert_no_symlink_chain(path, label=f"legacy P0 {label}")
        assert_local_rpi_storage(exact, label=f"legacy P0 {label} storage")
        try:
            exact.relative_to(storage_root)
        except ValueError as error:
            raise P0NormalizedEvidenceError(
                f"legacy P0 {label} is outside configuration.artifact_storage_root"
            ) from error
    if (
        not storage_root.is_dir()
        or artifact_root != storage_root / str(artifact_id)
        or not artifact_root.is_dir()
        or Path(str(raw_binding.get("path"))) != expected_raw
        or Path(str(metadata_binding.get("path"))) != expected_metadata
        or Path(str(analysis_binding.get("path"))) != expected_analysis
        or artifact.get("sha256") != raw_binding.get("sha256")
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 artifact root/data/meta/analysis identity differs from local storage plan"
        )
    quality = _mapping(attempt.get("quality_result"), "legacy P0 quality result")
    artifact_identity = _mapping(attempt.get("artifact_identity"), "legacy P0 artifact identity")
    if (
        quality.get("analysis_path") != analysis_binding.get("path")
        or quality.get("artifact_id") != observation.artifact_id
        or quality.get("artifact_path") != str(artifact_root)
        or quality.get("artifact_sha256") != observation.artifact_sha256
        or artifact_identity.get("artifact_id") != observation.artifact_id
        or artifact_identity.get("path") != str(artifact_root)
        or artifact_identity.get("sha256") != observation.artifact_sha256
    ):
        raise P0NormalizedEvidenceError(
            "legacy P0 manifest does not bind the exact analysis/artifact sources"
        )


def admit_normalized_p0_evidence(
    path: Path,
    *,
    expected_normalizer_repository: Path,
    expected_normalizer_commit: str,
    required_source_paths: tuple[str, ...],
    expected_dependency_attestation: Mapping[str, Any] | None = None,
    expected_native_attestation: Mapping[str, Any] | None = None,
    test_only_legacy_boards_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recursively re-admit one sealed normalized P0 envelope and its sources."""

    exact = assert_no_symlink_chain(path.expanduser().absolute(), label="normalized P0 evidence")
    assert_local_rpi_storage(exact, label="normalized P0 evidence storage")
    if exact.is_symlink() or not exact.is_file() or exact.stat().st_mode & 0o222:
        raise P0NormalizedEvidenceError("normalized P0 evidence must be a sealed read-only file")
    envelope = read_json_file(exact, label="normalized P0 evidence")
    if exact.read_bytes() != _canonical_bytes(envelope):
        raise P0NormalizedEvidenceError("normalized P0 evidence is not canonical sealed JSON")
    _exact_keys(
        envelope,
        (
            "schema",
            "evidence_kind",
            "immutable",
            "observation",
            "source_artifacts",
            "normalizer_source",
        ),
        "normalized P0 evidence",
    )
    if (
        envelope.get("schema") != SCHEMA
        or envelope.get("evidence_kind") != EVIDENCE_KIND
        or envelope.get("immutable") is not True
    ):
        raise P0NormalizedEvidenceError("normalized P0 evidence schema/status is invalid")
    observation_document = _mapping(envelope.get("observation"), "normalized P0 observation")
    observation = validate_observation(observation_document, expected_cohort="P0")
    source_artifacts = _mapping(envelope.get("source_artifacts"), "P0 source artifacts")
    _exact_keys(
        source_artifacts,
        (
            "legacy_plan",
            "legacy_manifest",
            "reference_transfer_analysis",
            "sigmf_metadata",
            "raw_iq",
        ),
        "P0 source artifacts",
    )
    verified_paths: dict[str, Path] = {}
    for name in (
        "legacy_manifest",
        "reference_transfer_analysis",
        "sigmf_metadata",
        "raw_iq",
    ):
        verified_paths[name] = verify_file_binding(
            source_artifacts[name], label=f"P0 {name.replace('_', ' ')}"
        )
        assert_local_rpi_storage(verified_paths[name], label=f"P0 {name} storage")
    plan_binding = _mapping(source_artifacts.get("legacy_plan"), "legacy P0 plan binding")
    _exact_keys(
        plan_binding,
        ("source", "json_pointer", "canonical_sha256"),
        "legacy P0 plan binding",
    )
    manifest = read_json_file(verified_paths["legacy_manifest"], label="legacy P0 manifest")
    plan = manifest.get("plan")
    if (
        plan_binding.get("source") != "legacy_manifest"
        or plan_binding.get("json_pointer") != "/plan"
        or not isinstance(plan, (list, dict))
        or not plan
        or plan_binding.get("canonical_sha256") != _canonical_sha256(plan)
    ):
        raise P0NormalizedEvidenceError("legacy P0 embedded plan binding is stale")
    source = _normalizer_source(
        envelope.get("normalizer_source"),
        expected_repository=expected_normalizer_repository,
        expected_commit=expected_normalizer_commit,
        required_source_paths=required_source_paths,
    )
    analysis = read_json_file(
        verified_paths["reference_transfer_analysis"], label="legacy P0 analysis"
    )
    read_json_file(verified_paths["sigmf_metadata"], label="legacy P0 SigMF metadata")
    _verify_artifact_graph(
        observation=observation,
        manifest=manifest,
        analysis=analysis,
        source_artifacts=source_artifacts,
        expected_repository=Path(str(source["repository"])),
        test_only_legacy_boards_root=test_only_legacy_boards_root,
    )
    if expected_dependency_attestation is not None and source.get(
        "pluto_plus_utils_source_attestation"
    ) != dict(expected_dependency_attestation):
        raise P0NormalizedEvidenceError(
            "P0 normalizer pluto-plus-utils source differs from the current pinned analyzer"
        )
    if expected_native_attestation is not None and source.get(
        "native_libiio_runtime_attestation"
    ) != dict(expected_native_attestation):
        raise P0NormalizedEvidenceError(
            "P0 normalizer native libiio runtime differs from the current analyzer process"
        )
    binding = {
        **_file_identity(exact, label="normalized P0 evidence"),
        "run_id": observation.run_id,
        "artifact_id": observation.artifact_id,
        "stream_id": observation.stream_id,
        "artifact_sha256": observation.artifact_sha256,
        "source_commit": observation.source_commit,
        "profile_contract_sha256": observation.profile_contract_sha256,
        "normalizer_commit": source["commit"],
        "source_artifacts_sha256": _canonical_sha256(source_artifacts),
    }
    return dict(observation_document), binding


def write_sealed_normalized_p0(path: Path, document: Mapping[str, Any]) -> Path:
    """Publish one canonical create-only normalized P0 envelope as mode 0400."""

    output = path.expanduser().absolute()
    assert_no_symlink_chain(output, label="normalized P0 output")
    assert_local_rpi_storage(output, label="normalized P0 output storage")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() or output.is_symlink():
        raise P0NormalizedEvidenceError("normalized P0 output exists; refusing overwrite")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_canonical_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return output


__all__ = [
    "EVIDENCE_KIND",
    "LEGACY_AUTHORITATIVE_BOARDS_ROOT",
    "LEGACY_RUNNER_GID",
    "LEGACY_RUNNER_HOME",
    "LEGACY_RUNNER_SHELL",
    "LEGACY_RUNNER_UID",
    "LEGACY_RUNNER_USER",
    "P0NormalizedEvidenceError",
    "admit_normalized_p0_evidence",
    "build_normalized_p0_envelope",
    "reconstruct_legacy_closed_loop_plan",
    "validate_legacy_p0_execution_identity",
    "validate_legacy_p0_plan",
    "write_sealed_normalized_p0",
]
