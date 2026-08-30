from __future__ import annotations

import json
import pwd
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import smateway.p0_normalized_evidence as p0_evidence
from smateway.file_artifact_admission import FileArtifactAdmissionError
from smateway.hexcal import sha256_path
from smateway.input_off_control import OBSERVATION_KIND, acquisition_contract, canonical_sha256
from smateway.p0_normalized_evidence import (
    P0NormalizedEvidenceError,
    admit_normalized_p0_evidence,
    build_normalized_p0_envelope,
    reconstruct_legacy_closed_loop_plan,
    write_sealed_normalized_p0,
)

COMMIT = "a" * 40
RUN_ID = "p0-run-a"
ARTIFACT_ID = "c" * 32
DEPENDENCY = {"schema": 1, "dependency": "synthetic-pluto-plus-utils"}
NATIVE = {"schema": 1, "evidence_kind": "synthetic-native-libiio"}


def _reviewed_runner_account(**overrides: object) -> SimpleNamespace:
    identity: dict[str, object] = {
        "pw_name": "smateway-rf",
        "pw_uid": 990,
        "pw_gid": 990,
        "pw_dir": "/var/lib/smateway-rf",
        "pw_shell": "/usr/sbin/nologin",
    }
    identity.update(overrides)
    return SimpleNamespace(**identity)


def test_production_legacy_root_is_derived_from_exact_reviewed_runner_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_names: list[str] = []

    def account(name: str) -> SimpleNamespace:
        observed_names.append(name)
        return _reviewed_runner_account()

    monkeypatch.setattr(pwd, "getpwnam", account)
    assert p0_evidence.LEGACY_RUNNER_USER == "smateway-rf"
    assert p0_evidence.LEGACY_RUNNER_UID == 990
    assert p0_evidence.LEGACY_RUNNER_GID == 990
    assert Path("/var/lib/smateway-rf") == p0_evidence.LEGACY_RUNNER_HOME
    assert Path("/usr/sbin/nologin") == p0_evidence.LEGACY_RUNNER_SHELL
    assert p0_evidence._legacy_authoritative_boards_root(test_only_legacy_boards_root=None) == Path(
        "/var/lib/smateway-rf/.local/state/smateway/boards"
    )
    assert observed_names == ["smateway-rf"]


def test_production_legacy_root_rejects_different_runner_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: _reviewed_runner_account(pw_uid=991),
    )
    with pytest.raises(P0NormalizedEvidenceError, match="account identity differs"):
        p0_evidence._legacy_authoritative_boards_root(test_only_legacy_boards_root=None)


def _observation(raw_path: Path) -> dict[str, Any]:
    return {
        "schema": 1,
        "observation_kind": OBSERVATION_KIND,
        "cohort": "P0",
        "run_id": RUN_ID,
        "artifact": {
            "artifact_id": ARTIFACT_ID,
            "stream_id": 123,
            "sha256": sha256_path(raw_path),
        },
        "acquisition": acquisition_contract(),
        "profile_contract_sha256": (
            "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
        ),
        "analysis": {
            "transfer_detected": True,
            "all_off_transfer": {"real": 0.01, "imag": 0.002},
            "all_off_transfer_upper_bound": None,
            "rx1_reference_amplitude": 100.0,
            "detected_pilot_snr_db": 35.0,
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
            "source_commit": COMMIT,
            "source_files_sha256": None,
            "native_attestation_sha256": None,
            "fixture_evidence_sha256": None,
            "fixture_fixed_graph_sha256": None,
            "comparable_fixture_group_id": None,
        },
    }


def _sources(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    board_id = "board-a"
    boards_root = tmp_path / "boards"
    board_state_root = boards_root / board_id
    storage_root = board_state_root / "pluto-usb-captures"
    artifact_root = storage_root / ARTIFACT_ID
    artifact_root.mkdir(parents=True)
    raw = artifact_root / f"{ARTIFACT_ID}.sigmf-data"
    metadata = artifact_root / f"{ARTIFACT_ID}.sigmf-meta"
    analysis = artifact_root / "fast20-reference-transfer-v2.json"
    raw.write_bytes(b"source-bound-raw-iq")
    metadata.write_text("{}\n", encoding="utf-8")
    analysis.write_text(
        json.dumps(
            {
                "artifact": {
                    "artifact_id": ARTIFACT_ID,
                    "path": str(artifact_root),
                    "sha256": sha256_path(raw),
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    repository = tmp_path / "smateway"
    source_path = repository / "scripts/analyze_5g8_input_off_cohort.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# exact normalizer source\n", encoding="utf-8")
    init_path = repository / "src/smateway/__init__.py"
    init_path.parent.mkdir(parents=True)
    init_path.write_text('"""Synthetic Smateway."""\n', encoding="utf-8")
    files = [
        {
            "path": "src/smateway/__init__.py",
            "sha256": sha256_path(init_path),
            "size_bytes": init_path.stat().st_size,
        },
        {
            "path": "scripts/analyze_5g8_input_off_cohort.py",
            "sha256": sha256_path(source_path),
            "size_bytes": source_path.stat().st_size,
        },
    ]
    configuration = {
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "frequencies_hz": [5_800_000_000],
        "closure_frequencies_hz": [5_800_000_000],
        "stages": ["rotation0", "rotation1", "rotation2", "closure0"],
        "mappings": {
            "rotation0": {f"F{index}": f"ANT{index}" for index in range(1, 9)},
            "rotation1": {f"F{index}": f"ANT{index % 8 + 1}" for index in range(1, 9)},
            "rotation2": {f"F{index}": f"ANT{(index + 1) % 8 + 1}" for index in range(1, 9)},
            "closure0": {f"F{index}": f"ANT{index}" for index in range(1, 9)},
        },
        "fixture_id": "tx1-2way-rx1-and-8way-board-rx2-v1",
        "fully_conducted_required": True,
        "tx_channel": 0,
        "stimulus": "qualification",
        "receiver_gain_db": 40,
        "sample_rate_hz": 1_000_000,
        "duration_s": 10.0,
        "kernel_buffers": 8,
        "planned_capture_count": 4,
        "estimated_raw_iq_bytes": 320_000_000,
        "profile_id": "fast20-v1",
        "profile_contract_sha256": (
            "25b2bd0769687cc255d5e6926312e7e827672dc4567d64aecd85e8078acb4258"
        ),
        "firmware_binary_sha256": (
            "aeaed9d2f892d2a59add1aba2a7477e349b750c99f81610632286d04d91326ac"
        ),
        "board_id": board_id,
        "serial": "serial-a",
        "uri": "usb:1.2.3",
        "python": "/home/pi/pluto-plus-utils/.venv/bin/python",
        "timeout_s": 180,
        "storage_medium": "raspberry_pi_local_filesystem",
        "board_state_root": str(board_state_root),
        "artifact_storage_root": str(storage_root),
        "pluto_onboard_storage_used": False,
    }
    plan = reconstruct_legacy_closed_loop_plan(
        configuration,
        expected_repository=repository,
        test_only_legacy_boards_root=boards_root,
    )
    condition = next(
        item
        for item in plan
        if item["stage"] == "rotation0" and item["center_frequency_hz"] == 5_800_000_000
    )
    reanalysis_command = [
        ARTIFACT_ID if item == "{artifact_id}" else item
        for item in condition["reference_reanalysis_command_template"]
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "experiment_kind": configuration["experiment_kind"],
                "run_id": RUN_ID,
                "status": "awaiting_rotation1",
                "runner_source_commit": COMMIT,
                "configuration": configuration,
                "plan": plan,
                "final_mute": {
                    "status": "passed",
                    "purpose": "final_rotation0",
                    "error": None,
                },
                "attempts": [
                    {
                        **condition,
                        "artifact_id": ARTIFACT_ID,
                        "status": "complete",
                        "outcome": "quality_passed",
                        "failure_kind": None,
                        "error": None,
                        "post_mute": {
                            "status": "passed",
                            "purpose": "post_attempt",
                            "error": None,
                        },
                        "capture": {
                            "status": "complete",
                            "accepted": True,
                            "timed_out": False,
                            "return_code": 0,
                            "command": condition["capture_command"],
                        },
                        "reanalysis": {
                            "status": "complete",
                            "accepted": True,
                            "timed_out": False,
                            "return_code": 0,
                            "command": reanalysis_command,
                            "parsed_output": {
                                "artifact_id": ARTIFACT_ID,
                                "quality_passed": True,
                            },
                        },
                        "artifact_identity": {
                            "artifact_id": ARTIFACT_ID,
                            "path": str(artifact_root),
                            "sha256": sha256_path(raw),
                        },
                        "quality_result": {
                            "status": "passed",
                            "quality_passed": True,
                            "analysis_path": str(analysis),
                            "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
                            "artifact_id": ARTIFACT_ID,
                            "artifact_path": str(artifact_root),
                            "artifact_sha256": sha256_path(raw),
                            "tx_channel": 0,
                            "center_frequency_hz": 5_800_000_000,
                            "receiver_gain_db": 40,
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = {
        "schema": 1,
        "python_executable": "/home/pi/pluto-plus-utils/.venv/bin/python",
        "python_prefix": "/home/pi/pluto-plus-utils/.venv",
        "sys_path": [str(repository / "src")],
        "smateway_source_first": True,
        "pythonpath": [str(repository / "src")],
        "ld_library_path": ["/usr/local/lib"],
    }
    imports = {
        "schema": 1,
        "repository": str(repository),
        "commit": COMMIT,
        "source_files_sha256": canonical_sha256(files),
        "modules": [
            {
                "module": "smateway",
                "relative_path": "src/smateway/__init__.py",
                "origin": str(init_path),
                "sha256": sha256_path(init_path),
                "size_bytes": init_path.stat().st_size,
            }
        ],
    }
    imports["modules_sha256"] = canonical_sha256(imports["modules"])
    source = {
        "schema": 1,
        "repository": str(repository),
        "commit": COMMIT,
        "clean_worktree_verified": True,
        "files": files,
        "source_files_sha256": canonical_sha256(files),
        "analyzer_runtime_attestation": runtime,
        "analyzer_runtime_attestation_sha256": canonical_sha256(runtime),
        "smateway_import_origin_attestation": imports,
        "smateway_import_origin_attestation_sha256": canonical_sha256(imports),
        "pluto_plus_utils_source_attestation": DEPENDENCY,
        "pluto_plus_utils_source_attestation_sha256": canonical_sha256(DEPENDENCY),
        "native_libiio_runtime_attestation": NATIVE,
        "native_libiio_runtime_attestation_sha256": canonical_sha256(NATIVE),
    }
    return source, {
        "repository": repository,
        "manifest": manifest,
        "analysis": analysis,
        "metadata": metadata,
        "raw": raw,
        "boards_root": boards_root,
        "storage": storage_root,
        "artifact_root": artifact_root,
    }


def _sealed(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    source, paths = _sources(tmp_path)
    envelope = build_normalized_p0_envelope(
        _observation(paths["raw"]),
        manifest_path=paths["manifest"],
        analysis_path=paths["analysis"],
        metadata_path=paths["metadata"],
        raw_iq_path=paths["raw"],
        normalizer_source=source,
        test_only_legacy_boards_root=paths["boards_root"],
    )
    output = write_sealed_normalized_p0(tmp_path / "normalized-p0.json", envelope)
    return output, paths


def test_production_reconstruction_rejects_self_consistent_non_authoritative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, paths = _sources(tmp_path)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: _reviewed_runner_account(),
    )

    with pytest.raises(P0NormalizedEvidenceError, match="authoritative runner root"):
        reconstruct_legacy_closed_loop_plan(
            manifest["configuration"],
            expected_repository=paths["repository"],
        )


def test_production_admission_rejects_test_root_sealed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, paths = _sealed(tmp_path)
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: _reviewed_runner_account(),
    )

    with pytest.raises(P0NormalizedEvidenceError, match="authoritative runner root"):
        admit_normalized_p0_evidence(
            output,
            expected_normalizer_repository=paths["repository"],
            expected_normalizer_commit=COMMIT,
            required_source_paths=("scripts/analyze_5g8_input_off_cohort.py",),
            expected_dependency_attestation=DEPENDENCY,
            expected_native_attestation=NATIVE,
        )


def test_test_only_legacy_root_must_be_explicitly_absolute() -> None:
    with pytest.raises(P0NormalizedEvidenceError, match="must be absolute"):
        p0_evidence._legacy_authoritative_boards_root(
            test_only_legacy_boards_root=Path("relative-test-root")
        )


def _admit(
    path: Path, repository: Path, boards_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    return admit_normalized_p0_evidence(
        path,
        expected_normalizer_repository=repository,
        expected_normalizer_commit=COMMIT,
        required_source_paths=("scripts/analyze_5g8_input_off_cohort.py",),
        expected_dependency_attestation=DEPENDENCY,
        expected_native_attestation=NATIVE,
        test_only_legacy_boards_root=boards_root,
    )


def test_sealed_p0_recursively_reopens_every_bound_source(tmp_path: Path) -> None:
    output, paths = _sealed(tmp_path)
    observation, binding = _admit(output, paths["repository"], paths["boards_root"])
    assert output.stat().st_mode & 0o222 == 0
    assert observation["artifact"]["artifact_id"] == ARTIFACT_ID
    assert binding["path"] == str(output)
    assert binding["normalizer_commit"] == COMMIT


def test_sealed_p0_rejects_source_mutation_and_writable_envelope(tmp_path: Path) -> None:
    output, paths = _sealed(tmp_path)
    paths["raw"].write_bytes(b"mutated")
    with pytest.raises(FileArtifactAdmissionError, match="SHA-256 binding is stale"):
        _admit(output, paths["repository"], paths["boards_root"])

    paths["raw"].write_bytes(b"source-bound-raw-iq")
    output.chmod(0o600)
    with pytest.raises(P0NormalizedEvidenceError, match="sealed read-only"):
        _admit(output, paths["repository"], paths["boards_root"])


def test_sealed_p0_rejects_forged_embedded_plan_binding(tmp_path: Path) -> None:
    source, paths = _sources(tmp_path)
    envelope = build_normalized_p0_envelope(
        _observation(paths["raw"]),
        manifest_path=paths["manifest"],
        analysis_path=paths["analysis"],
        metadata_path=paths["metadata"],
        raw_iq_path=paths["raw"],
        normalizer_source=source,
        test_only_legacy_boards_root=paths["boards_root"],
    )
    envelope["source_artifacts"]["legacy_plan"]["canonical_sha256"] = "0" * 64
    output = write_sealed_normalized_p0(tmp_path / "forged-plan.json", envelope)
    with pytest.raises(P0NormalizedEvidenceError, match="embedded plan binding"):
        _admit(output, paths["repository"], paths["boards_root"])


def test_sealed_p0_rejects_analysis_artifact_path_swap(tmp_path: Path) -> None:
    source, paths = _sources(tmp_path)
    paths["analysis"].write_text(
        json.dumps(
            {
                "artifact": {
                    "artifact_id": ARTIFACT_ID,
                    "path": str(tmp_path / "wrong-artifact"),
                    "sha256": sha256_path(paths["raw"]),
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(P0NormalizedEvidenceError, match="outside configuration"):
        build_normalized_p0_envelope(
            _observation(paths["raw"]),
            manifest_path=paths["manifest"],
            analysis_path=paths["analysis"],
            metadata_path=paths["metadata"],
            raw_iq_path=paths["raw"],
            normalizer_source=source,
            test_only_legacy_boards_root=paths["boards_root"],
        )


def _refresh_manifest_binding(envelope: dict[str, Any], manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    envelope["source_artifacts"]["legacy_manifest"] = {
        "path": str(manifest_path),
        "sha256": sha256_path(manifest_path),
        "size_bytes": manifest_path.stat().st_size,
    }
    envelope["source_artifacts"]["legacy_plan"]["canonical_sha256"] = canonical_sha256(
        manifest["plan"]
    )


@pytest.mark.parametrize("mutation", ("python", "capture_program", "reanalysis_program"))
def test_sealed_p0_rejects_self_consistent_legacy_command_forgery(
    tmp_path: Path, mutation: str
) -> None:
    source, paths = _sources(tmp_path)
    envelope = build_normalized_p0_envelope(
        _observation(paths["raw"]),
        manifest_path=paths["manifest"],
        analysis_path=paths["analysis"],
        metadata_path=paths["metadata"],
        raw_iq_path=paths["raw"],
        normalizer_source=source,
        test_only_legacy_boards_root=paths["boards_root"],
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    condition = next(
        item
        for item in manifest["plan"]
        if item["stage"] == "rotation0" and item["center_frequency_hz"] == 5_800_000_000
    )
    attempt = manifest["attempts"][0]
    if mutation == "python":
        manifest["configuration"]["python"] = "/usr/bin/python3"
        for row in manifest["plan"]:
            row["capture_command"][0] = "/usr/bin/python3"
            row["reference_reanalysis_command_template"][0] = "/usr/bin/python3"
        attempt["capture_command"][0] = "/usr/bin/python3"
        attempt["reference_reanalysis_command_template"][0] = "/usr/bin/python3"
        attempt["capture"]["command"][0] = "/usr/bin/python3"
        attempt["reanalysis"]["command"][0] = "/usr/bin/python3"
        expected = "configuration.python"
    elif mutation == "capture_program":
        condition["capture_command"][1] = "/tmp/forged-capture.py"
        attempt["capture_command"][1] = "/tmp/forged-capture.py"
        attempt["capture"]["command"][1] = "/tmp/forged-capture.py"
        expected = "reconstructed immutable"
    else:
        condition["reference_reanalysis_command_template"][1] = "/tmp/forged-analysis.py"
        attempt["reference_reanalysis_command_template"][1] = "/tmp/forged-analysis.py"
        attempt["reanalysis"]["command"][1] = "/tmp/forged-analysis.py"
        expected = "reconstructed immutable"
    paths["manifest"].write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    _refresh_manifest_binding(envelope, paths["manifest"])
    output = write_sealed_normalized_p0(tmp_path / f"forged-{mutation}.json", envelope)

    with pytest.raises(P0NormalizedEvidenceError, match=expected):
        _admit(output, paths["repository"], paths["boards_root"])


def test_sealed_p0_rejects_artifact_graph_outside_declared_storage_root(
    tmp_path: Path,
) -> None:
    source, paths = _sources(tmp_path)
    envelope = build_normalized_p0_envelope(
        _observation(paths["raw"]),
        manifest_path=paths["manifest"],
        analysis_path=paths["analysis"],
        metadata_path=paths["metadata"],
        raw_iq_path=paths["raw"],
        normalizer_source=source,
        test_only_legacy_boards_root=paths["boards_root"],
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    alternate_board_root = tmp_path / "alternate-boards" / "board-a"
    alternate_storage_root = alternate_board_root / "pluto-usb-captures"
    alternate_storage_root.mkdir(parents=True)
    manifest["configuration"]["board_state_root"] = str(alternate_board_root)
    manifest["configuration"]["artifact_storage_root"] = str(alternate_storage_root)
    paths["manifest"].write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    _refresh_manifest_binding(envelope, paths["manifest"])
    output = write_sealed_normalized_p0(tmp_path / "forged-storage.json", envelope)

    with pytest.raises(P0NormalizedEvidenceError, match="authoritative runner root"):
        _admit(output, paths["repository"], paths["boards_root"])


@pytest.mark.parametrize(
    "rejected_label",
    (
        "legacy P0 artifact root storage",
        "legacy P0 raw IQ storage",
        "legacy P0 SigMF metadata storage",
        "legacy P0 reference analysis storage",
    ),
)
def test_p0_build_checks_every_artifact_member_is_on_local_rpi_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected_label: str,
) -> None:
    source, paths = _sources(tmp_path)
    real_admission = p0_evidence.assert_local_rpi_storage  # type: ignore[attr-defined]

    def reject_selected(path: Path, *, label: str, **kwargs: Any) -> Path:
        if label == rejected_label:
            raise FileArtifactAdmissionError(f"{label} is not on the local RPi storage device")
        return real_admission(path, label=label, **kwargs)

    monkeypatch.setattr(p0_evidence, "assert_local_rpi_storage", reject_selected)
    with pytest.raises(FileArtifactAdmissionError, match="local RPi storage device"):
        build_normalized_p0_envelope(
            _observation(paths["raw"]),
            manifest_path=paths["manifest"],
            analysis_path=paths["analysis"],
            metadata_path=paths["metadata"],
            raw_iq_path=paths["raw"],
            normalizer_source=source,
            test_only_legacy_boards_root=paths["boards_root"],
        )


def test_p0_build_rejects_symlinked_artifact_ancestry(tmp_path: Path) -> None:
    source, paths = _sources(tmp_path)
    relocated = tmp_path / "relocated-artifact"
    paths["artifact_root"].rename(relocated)
    paths["artifact_root"].symlink_to(relocated, target_is_directory=True)

    with pytest.raises(FileArtifactAdmissionError, match="contains a symlink"):
        build_normalized_p0_envelope(
            _observation(paths["raw"]),
            manifest_path=paths["manifest"],
            analysis_path=paths["analysis"],
            metadata_path=paths["metadata"],
            raw_iq_path=paths["raw"],
            normalizer_source=source,
            test_only_legacy_boards_root=paths["boards_root"],
        )


def test_sealed_p0_rejects_another_normalizer_repository(tmp_path: Path) -> None:
    output, paths = _sealed(tmp_path)
    other = tmp_path / "another-smateway"
    other.mkdir()
    with pytest.raises(P0NormalizedEvidenceError, match="repository differs"):
        _admit(output, other, paths["boards_root"])


def test_sealed_p0_rejects_another_dependency_or_native_runtime(tmp_path: Path) -> None:
    output, paths = _sealed(tmp_path)
    with pytest.raises(P0NormalizedEvidenceError, match="pluto-plus-utils source differs"):
        admit_normalized_p0_evidence(
            output,
            expected_normalizer_repository=paths["repository"],
            expected_normalizer_commit=COMMIT,
            required_source_paths=("scripts/analyze_5g8_input_off_cohort.py",),
            expected_dependency_attestation={"schema": 1, "dependency": "ambient-wheel"},
            expected_native_attestation=NATIVE,
            test_only_legacy_boards_root=paths["boards_root"],
        )
    with pytest.raises(P0NormalizedEvidenceError, match="native libiio runtime differs"):
        admit_normalized_p0_evidence(
            output,
            expected_normalizer_repository=paths["repository"],
            expected_normalizer_commit=COMMIT,
            required_source_paths=("scripts/analyze_5g8_input_off_cohort.py",),
            expected_dependency_attestation=DEPENDENCY,
            expected_native_attestation={"schema": 1, "evidence_kind": "ambient-libiio"},
            test_only_legacy_boards_root=paths["boards_root"],
        )
