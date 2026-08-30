from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from smateway.one_hot_ladder import (
    validate_one_hot_fixture_identity,
    validate_one_hot_matrix_identity,
)

REPOSITORY = Path(__file__).resolve().parents[1]


def _load_test_helpers(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, REPOSITORY / "tests" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


GENERAL_HELPERS = _load_test_helpers(
    "selector_flash_general_test_helpers",
    "test_run_5g8_leakage_ladder.py",
)
ONE_HOT_HELPERS = _load_test_helpers(
    "selector_flash_one_hot_test_helpers",
    "test_run_5g8_one_hot_path_ladder.py",
)
MATRIX_HELPERS = _load_test_helpers(
    "selector_flash_matrix_test_helpers",
    "test_one_hot_ladder.py",
)

general = GENERAL_HELPERS.runner
one_hot = ONE_HOT_HELPERS.runner


def _build_general_contract(
    stage: str,
    *,
    fixture: dict[str, Any] | None,
    selector_control: dict[str, Any] | None,
) -> dict[str, Any]:
    return general._build_plan_contract(
        run_id=f"run-{stage}",
        board_id="board-a",
        serial=GENERAL_HELPERS.SERIAL,
        uri=GENERAL_HELPERS.URI,
        stage=stage,
        source_commit=GENERAL_HELPERS.SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=GENERAL_HELPERS._dependency_attestation(),
        selector_control=selector_control,
        native_libiio_runtime_attestation=GENERAL_HELPERS._native_attestation(),
        fixture_evidence=fixture,
    )


@pytest.mark.parametrize("stage", sorted(general.SELECTOR_CONNECTED_STAGES))
def test_connected_general_contract_freezes_one_exact_flash_binding(stage: str) -> None:
    fixture = GENERAL_HELPERS._fixture_evidence(stage)
    control = GENERAL_HELPERS._selector_control()

    contract = _build_general_contract(stage, fixture=fixture, selector_control=control)

    expected = contract["fixture_evidence"]["selector_flash_evidence"]
    assert expected == contract["fixture_evidence"]["setup_attestation"]["selector_flash_evidence"]
    assert expected == contract["selector_control"]["selector_flash_evidence"]
    assert {key: expected[key] for key in ("path", "sha256", "run_id")} == {
        "path": "/synthetic/selector-flash-evidence.json",
        "sha256": "b" * 64,
        "run_id": "bench-flash-r01",
    }


@pytest.mark.parametrize("stage", sorted(general.SELECTOR_CONNECTED_STAGES))
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "/synthetic/different-selector-flash-evidence.json"),
        ("sha256", "c" * 64),
        ("run_id", "bench-flash-r02"),
    ],
)
@pytest.mark.parametrize("location", ["setup", "control"])
def test_connected_general_contract_rejects_any_flash_tuple_mismatch(
    stage: str,
    field: str,
    replacement: str,
    location: str,
) -> None:
    fixture = copy.deepcopy(GENERAL_HELPERS._fixture_evidence(stage))
    control = copy.deepcopy(GENERAL_HELPERS._selector_control())
    if location == "setup":
        fixture["setup_attestation"]["selector_flash_evidence"] = copy.deepcopy(
            fixture["setup_attestation"]["selector_flash_evidence"]
        )
        fixture["setup_attestation"]["selector_flash_evidence"][field] = replacement
        message = "per-run setup attestation binding"
    else:
        control["selector_flash_evidence"][field] = replacement
        message = "same sealed live-image evidence"

    with pytest.raises(ValueError, match=message):
        _build_general_contract(stage, fixture=fixture, selector_control=control)


@pytest.mark.parametrize("stage", sorted(general.SELECTOR_CONNECTED_STAGES))
def test_connected_general_contract_cannot_omit_fixture_and_setup_binding(stage: str) -> None:
    with pytest.raises(ValueError, match="fixture"):
        _build_general_contract(
            stage,
            fixture=None,
            selector_control=GENERAL_HELPERS._selector_control(),
        )


@pytest.mark.parametrize(
    "stage",
    ["direct_rx2_termination", "rx2_cable_terminated"],
)
def test_disconnected_general_fixture_forbids_flash_binding(stage: str) -> None:
    fixture = copy.deepcopy(GENERAL_HELPERS._fixture_evidence(stage))
    fixture["selector_flash_evidence"] = GENERAL_HELPERS._selector_flash_binding()

    with pytest.raises(ValueError, match="selector-disconnected fixture"):
        general._validate_fixture_evidence_v2(
            fixture,
            expected_stage=stage,
            expected_run_id=f"run-{stage}",
            expected_board_id="board-a",
            expected_serial=GENERAL_HELPERS.SERIAL,
        )


@pytest.mark.parametrize("stage", sorted(general.SELECTOR_CONNECTED_STAGES))
def test_live_fixture_preflight_forwards_frozen_flash_tuple(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = GENERAL_HELPERS._fixture_evidence(stage)
    calls: list[dict[str, Any]] = []

    def rebuild(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return copy.deepcopy(fixture)

    monkeypatch.setattr(general, "_fixture_evidence_from_manifests", rebuild)

    evidence = general._live_fixture_evidence_boundary(fixture)

    flash = fixture["selector_flash_evidence"]
    assert evidence["status"] == "passed"
    assert calls == [
        {
            "run_id": fixture["run_id"],
            "board_id": fixture["board_id"],
            "serial": fixture["shared_fixture"]["pluto"]["serial"],
            "stage": fixture["stage"],
            "selector_flash_evidence_path": Path(flash["path"]),
            "selector_flash_evidence_sha256": flash["sha256"],
            "selector_flash_run_id": flash["run_id"],
        }
    ]


def _selector_control_with_real_artifacts(tmp_path: Path) -> dict[str, Any]:
    control = copy.deepcopy(GENERAL_HELPERS._selector_control())
    control.pop("target_image_admission_contract")
    locations = {
        ("bench_manifest", "path", "file_sha256"): tmp_path / "bench-manifest.json",
        ("openocd_config", "path", "file_sha256"): tmp_path / "openocd.cfg",
        ("control_profile", "path", "file_sha256"): tmp_path / "profile.json",
        (
            "control_profile",
            "header_path",
            "header_file_sha256",
        ): tmp_path / "profile.h",
    }
    for index, ((section, path_key, hash_key), path) in enumerate(locations.items()):
        path.write_text(f"artifact-{index}\n", encoding="utf-8")
        control[section][path_key] = str(path.resolve())
        control[section][hash_key] = general.sha256_path(path)
    return control


def test_execution_artifact_check_recursively_revalidates_exact_flash_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _selector_control_with_real_artifacts(tmp_path)
    calls: list[tuple[Path, dict[str, Any]]] = []
    elf = tmp_path / "bench.elf"
    firmware_bin = tmp_path / "bench.bin"
    elf.write_bytes(b"elf")
    firmware_bin.write_bytes(b"bin")

    def identity(path: Path) -> dict[str, Any]:
        return {
            "path": str(path.resolve()),
            "sha256": general.sha256_path(path),
            "size_bytes": path.stat().st_size,
        }

    def revalidate(path: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append((path, kwargs))
        return {
            "frozen_inputs": {
                "files": {
                    "build_manifest": identity(Path(control["bench_manifest"]["path"])),
                    "elf": identity(elf),
                    "firmware_bin": identity(firmware_bin),
                    "openocd_config": identity(Path(control["openocd_config"]["path"])),
                    "profile": identity(Path(control["control_profile"]["path"])),
                    "profile_header": identity(Path(control["control_profile"]["header_path"])),
                },
                "control_profile": {
                    "id": control["control_profile"].get("profile_id"),
                    "revision": control["control_profile"].get("revision"),
                    "contract_sha256": control["control_profile"].get("contract_sha256"),
                    "all_off_code": control["control_profile"]["all_off_code"],
                },
            }
        }

    monkeypatch.setattr(general, "validate_sealed_selector_evidence", revalidate)

    general._verify_selector_artifacts(control)

    flash = control["selector_flash_evidence"]
    assert calls == [
        (
            Path(flash["path"]),
            {
                "expected_sha256": flash["sha256"],
                "expected_campaign_id": flash["campaign_id"],
                "expected_run_id": flash["run_id"],
                "expected_board_id": flash["board_id"],
                "expected_image_role": "bench",
            },
        )
    ]


def test_execution_artifact_check_rejects_stale_recursive_flash_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _selector_control_with_real_artifacts(tmp_path)

    def stale(*_args: Any, **_kwargs: Any) -> None:
        raise general.SelectorFlashError("retained leaf hash changed")

    monkeypatch.setattr(general, "validate_sealed_selector_evidence", stale)

    with pytest.raises(general.LeakageLadderError, match="retained leaf hash changed"):
        general._verify_selector_artifacts(control)


def test_flash_binding_rejects_symlinked_ancestor_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    evidence = real_directory / "selector-flash-evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    linked_evidence = (linked_directory / evidence.name).absolute()

    def reject_symlink_chain(path: Path, **_kwargs: Any) -> dict[str, Any]:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            if current.is_symlink():
                raise general.SelectorFlashError("evidence path must not contain symlinks")
        raise AssertionError("recursive validator received an already-resolved path")

    monkeypatch.setattr(general, "validate_sealed_selector_evidence", reject_symlink_chain)

    with pytest.raises(general.LeakageLadderError, match="must not contain symlinks"):
        general._selector_flash_evidence_binding_from_file(
            linked_evidence,
            expected_sha256="b" * 64,
            campaign_id="campaign-a",
            flash_run_id="bench-flash-r01",
            board_id="board-a",
        )


def _one_hot_parser_arguments() -> list[str]:
    return [
        "--run-id",
        "plan-only-ant1",
        "--board-id",
        "board-a",
        "--serial",
        ONE_HOT_HELPERS.SERIAL,
        "--uri",
        ONE_HOT_HELPERS.URI,
        "--driven-input",
        "ANT1",
        "--plan-only",
        "--bench-manifest",
        "/synthetic/bench-manifest.json",
        "--openocd-config",
        "/synthetic/openocd.cfg",
        "--profile",
        "/synthetic/profile.json",
        "--campaign-id",
        "campaign-a",
        "--selector-flash-evidence",
        "/synthetic/selector-flash-evidence.json",
        "--selector-flash-evidence-sha256",
        "f" * 64,
        "--selector-flash-run-id",
        "bench-flash-r01",
        "--feed-arm-id",
        "feed-arm-a",
        "--feed-cable-id",
        "feed-cable-a",
        "--termination-load-set-id",
        "loads-a",
        "--rx1-reference-plane-id",
        "rx1-plane-a",
        "--rx2-reference-plane-id",
        "rx2-plane-a",
        "--setup-evidence-file",
        "/synthetic/setup-evidence.json",
    ]


@pytest.mark.parametrize(
    "option",
    [
        "--campaign-id",
        "--selector-flash-evidence",
        "--selector-flash-evidence-sha256",
        "--selector-flash-run-id",
    ],
)
def test_one_hot_plan_only_parser_cannot_omit_flash_identity(option: str) -> None:
    arguments = _one_hot_parser_arguments()
    index = arguments.index(option)
    del arguments[index : index + 2]

    with pytest.raises(SystemExit):
        one_hot._parser().parse_args(arguments)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "/synthetic/different-selector-flash-evidence.json"),
        ("sha256", "e" * 64),
        ("campaign_id", "campaign-b"),
        ("run_id", "bench-flash-r02"),
        ("board_id", "board-b"),
        ("image_role", "release"),
    ],
)
def test_one_hot_plan_rejects_fixture_control_flash_mismatch(
    field: str,
    replacement: str,
) -> None:
    fixture = copy.deepcopy(ONE_HOT_HELPERS._fixture())
    control = copy.deepcopy(ONE_HOT_HELPERS._selector_control())
    fixture["selector_flash_evidence"][field] = replacement

    with pytest.raises(
        ValueError,
        match="different live-image evidence|selector-flash evidence identity",
    ):
        one_hot._build_plan_contract(
            run_id="run-ant1",
            board_id="board-a",
            serial=ONE_HOT_HELPERS.SERIAL,
            uri=ONE_HOT_HELPERS.URI,
            driven_input="ANT1",
            source_commit=ONE_HOT_HELPERS.SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=(ONE_HOT_HELPERS._dependency_attestation()),
            native_libiio_runtime_attestation=ONE_HOT_HELPERS._native_attestation(),
            selector_control=control,
            fixture_identity=fixture,
        )


def test_one_hot_plan_rejects_flash_for_different_board() -> None:
    fixture = copy.deepcopy(ONE_HOT_HELPERS._fixture())
    control = copy.deepcopy(ONE_HOT_HELPERS._selector_control())
    fixture["selector_flash_evidence"]["board_id"] = "board-b"
    control["selector_flash_evidence"]["board_id"] = "board-b"

    with pytest.raises(ValueError, match="board"):
        one_hot._build_plan_contract(
            run_id="run-ant1",
            board_id="board-a",
            serial=ONE_HOT_HELPERS.SERIAL,
            uri=ONE_HOT_HELPERS.URI,
            driven_input="ANT1",
            source_commit=ONE_HOT_HELPERS.SOURCE_COMMIT,
            pluto_plus_utils_source_attestation=(ONE_HOT_HELPERS._dependency_attestation()),
            native_libiio_runtime_attestation=ONE_HOT_HELPERS._native_attestation(),
            selector_control=control,
            fixture_identity=fixture,
        )


def test_one_hot_execution_verifier_delegates_to_recursive_flash_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stale(_selector_control: dict[str, Any]) -> None:
        raise one_hot.leakage.LeakageLadderError("sealed selector leaf changed")

    monkeypatch.setattr(one_hot.leakage, "_verify_selector_artifacts", stale)

    with pytest.raises(
        one_hot.leakage.LeakageLadderError,
        match="sealed selector leaf changed",
    ):
        one_hot._verify_one_hot_artifacts(ONE_HOT_HELPERS._selector_control())


def test_verified_row_loader_revalidates_recursive_selector_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    manifest_path = tmp_path / "manifest.json"
    plan_path.write_text("{}\n", encoding="utf-8")
    manifest_path.write_text("{}\n", encoding="utf-8")
    contract = {
        "run_id": "run-ant1",
        "topology_identity": one_hot.TOPOLOGY_IDENTITY,
        "driven_input": "ANT1",
        "selector_control": ONE_HOT_HELPERS._selector_control(),
        "fixture_identity": ONE_HOT_HELPERS._fixture(),
        "configuration": {
            "serial": ONE_HOT_HELPERS.SERIAL,
            "selector_state_order": [],
            "tx_hardware_gains_db": [],
            "attribution_tx_hardware_gain_db": -20.0,
            "attribution_repeat_count": 5,
            "minimum_detected_attribution_repeats": 3,
            "minimum_intended_through_contrast_over_all_off_db": 6.0,
            "maximum_attribution_amplitude_span_db": 3.0,
            "maximum_attribution_phase_residual_deg": 15.0,
        },
        "conditions": [],
        "storage": {"run_capture_root": str(tmp_path)},
    }
    envelope = {"plan_contract": contract, "plan_contract_sha256": "a" * 64}
    manifest = {
        "status": "complete",
        "error": None,
        "causal_attribution_claim": False,
        "operational_switching_claim": False,
        "native_runtime_preflight": {},
        "confirmations": [{}],
        "target_image_preflight": {},
        "preflight_mute": {},
        "preflight_selector_cleanup": {},
        "final_mute": {},
        "final_selector_cleanup": {},
        "attempts": [],
        "one_hot_run_summary": {},
    }

    monkeypatch.setattr(one_hot.leakage, "_read_json", lambda *_args: envelope)
    monkeypatch.setattr(
        one_hot.leakage,
        "_validate_plan_envelope",
        lambda *_args, **_kwargs: envelope,
    )
    monkeypatch.setattr(one_hot, "_load_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(
        one_hot,
        "_native_runtime_from_contract",
        lambda _contract: ({}, "b" * 64),
    )
    monkeypatch.setattr(one_hot, "runtime_preflight_passed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(one_hot, "_verify_fixture_evidence", lambda _fixture: None)
    monkeypatch.setattr(
        one_hot,
        "_physical_confirmation_reverified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(one_hot, "_target_image_passed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(one_hot.leakage, "_mute_passed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(one_hot, "_selector_passed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(one_hot, "_state_map", lambda _control: {one_hot.ALL_OFF_STATE: 8})
    monkeypatch.setattr(one_hot.leakage, "_plan_file_evidence", lambda *_args: {})
    monkeypatch.setattr(
        one_hot,
        "_completed_condition_ids",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        one_hot,
        "summarize_one_hot_run",
        lambda *_args, **_kwargs: SimpleNamespace(quality_passed=True),
    )
    monkeypatch.setattr(one_hot, "asdict", lambda _summary: {})
    monkeypatch.setattr(one_hot, "_matrix_identity_from_contract", lambda _contract: {})

    def reject_stale(_selector_control: dict[str, Any]) -> None:
        raise one_hot.OneHotLadderError("stale recursive selector seal")

    monkeypatch.setattr(one_hot, "_verify_one_hot_artifacts", reject_stale)

    with pytest.raises(one_hot.OneHotLadderError, match="stale recursive selector seal"):
        one_hot.load_verified_one_hot_row_bundle(
            plan_path=plan_path,
            manifest_path=manifest_path,
        )


def test_matrix_rejects_fixture_flash_digest_different_from_matrix_identity() -> None:
    rows = [
        MATRIX_HELPERS._verified_bundle(driven_input, index)
        for index, driven_input in enumerate(MATRIX_HELPERS.ANTENNA_STATES, start=1)
    ]
    for row in rows:
        row["matrix_identity"]["selector_flash_evidence"]["sha256"] = "d" * 64

    with pytest.raises(ValueError, match="selector|flash|DUT/control/acquisition"):
        MATRIX_HELPERS.summarize_complete_one_hot_matrix(
            [MATRIX_HELPERS._seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=MATRIX_HELPERS.GAINS,
            attribution_gain_db=MATRIX_HELPERS.ATTRIBUTION_GAIN,
        )


def test_matrix_rejects_different_exact_flash_binding_across_rows() -> None:
    rows = [
        MATRIX_HELPERS._verified_bundle(driven_input, index)
        for index, driven_input in enumerate(MATRIX_HELPERS.ANTENNA_STATES, start=1)
    ]
    changed = rows[1]["fixture_identity"]
    changed["selector_flash_evidence"]["path"] = "/evidence/stale-copy.json"
    changed["selector_flash_evidence"]["run_id"] = "bench-flash-r02"
    for result in rows[1]["results"]:
        result["fixture_identity"] = copy.deepcopy(changed)

    with pytest.raises(ValueError, match="selector|flash|fixture|DUT/control/acquisition"):
        MATRIX_HELPERS.summarize_complete_one_hot_matrix(
            [MATRIX_HELPERS._seal_verified_one_hot_row_bundle(row) for row in rows],
            planned_gains_db=MATRIX_HELPERS.GAINS,
            attribution_gain_db=MATRIX_HELPERS.ATTRIBUTION_GAIN,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "relative/evidence.json"),
        ("sha256", "not-a-sha256"),
        ("image_role", "release"),
    ],
)
def test_one_hot_fixture_rejects_malformed_flash_identity(
    field: str,
    replacement: str,
) -> None:
    fixture = copy.deepcopy(MATRIX_HELPERS._fixture())
    fixture["selector_flash_evidence"][field] = replacement

    with pytest.raises(ValueError, match="selector-flash"):
        validate_one_hot_fixture_identity(fixture)


def test_one_hot_matrix_identity_requires_flash_binding() -> None:
    identity = MATRIX_HELPERS._matrix_identity()
    del identity["selector_flash_evidence"]

    with pytest.raises(ValueError, match="matrix identity is incomplete"):
        validate_one_hot_matrix_identity(identity)


def test_disconnected_manifest_loader_forbids_each_flash_argument(tmp_path: Path) -> None:
    stage = "direct_rx2_termination"
    fixture_manifest = tmp_path / "fixture.json"
    fixture_manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "fixture_kind": general.FIXTURE_KIND_V2,
                "campaign_id": "campaign-a",
                "comparable_fixture_group_id": "fixture-group-a",
                "stage": stage,
                "board_id": "board-a",
                "shared_fixture": {},
                "stage_delta": {},
                "prior_stage_binding": None,
            }
        ),
        encoding="utf-8",
    )
    setup = tmp_path / "setup.json"
    setup.write_text("{}\n", encoding="utf-8")

    for flash_kwargs in (
        {"selector_flash_evidence_path": tmp_path / "flash.json"},
        {"selector_flash_evidence_sha256": "a" * 64},
        {"selector_flash_run_id": "bench-flash-r01"},
    ):
        with pytest.raises(general.LeakageLadderError, match="selector-disconnected"):
            general._fixture_evidence_from_manifests(
                fixture_manifest,
                setup,
                run_id="run-a",
                board_id="board-a",
                serial=GENERAL_HELPERS.SERIAL,
                stage=stage,
                **flash_kwargs,
            )


@pytest.mark.parametrize(
    "missing_key",
    [
        "selector_flash_evidence_path",
        "selector_flash_evidence_sha256",
        "selector_flash_run_id",
    ],
)
def test_connected_manifest_loader_requires_complete_flash_tuple(
    tmp_path: Path,
    missing_key: str,
) -> None:
    stage = "powered_selector_all_inputs_terminated"
    fixture_manifest = tmp_path / "fixture.json"
    fixture_manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "fixture_kind": general.FIXTURE_KIND_V2,
                "campaign_id": "campaign-a",
                "comparable_fixture_group_id": "fixture-group-a",
                "stage": stage,
                "board_id": "board-a",
                "shared_fixture": {},
                "stage_delta": {},
                "prior_stage_binding": None,
            }
        ),
        encoding="utf-8",
    )
    setup = tmp_path / "setup.json"
    setup.write_text("{}\n", encoding="utf-8")
    flash_kwargs: dict[str, Any] = {
        "selector_flash_evidence_path": tmp_path / "flash.json",
        "selector_flash_evidence_sha256": "a" * 64,
        "selector_flash_run_id": "bench-flash-r01",
    }
    flash_kwargs[missing_key] = None

    with pytest.raises(general.LeakageLadderError, match="requires sealed selector-flash"):
        general._fixture_evidence_from_manifests(
            fixture_manifest,
            setup,
            run_id="run-c",
            board_id="board-a",
            serial=GENERAL_HELPERS.SERIAL,
            stage=stage,
            **flash_kwargs,
        )
