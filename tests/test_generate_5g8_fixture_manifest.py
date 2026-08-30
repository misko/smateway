from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_5g8_fixture_manifest.py"
SPEC = importlib.util.spec_from_file_location("generate_5g8_fixture_manifest_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

BOARD = "board-a"
SERIAL = "serial-a"
CAMPAIGN = "campaign-a"
GROUP = "fixture-group-a"
STAGES = (
    "direct_rx2_termination",
    "rx2_cable_terminated",
    "powered_selector_all_inputs_terminated",
    "full_conducted_fixture",
)
TEMPLATE_DIRECTORY = SCRIPT.parents[1] / "docs/5g8_root_cause_analysis"


def _draft(stage: str) -> dict[str, Any]:
    return {
        "schema": 2,
        "fixture_kind": generator.leakage.FIXTURE_KIND_V2,
        "campaign_id": CAMPAIGN,
        "comparable_fixture_group_id": GROUP,
        "stage": stage,
        "board_id": BOARD,
        "shared_fixture": {"serial": SERIAL, "identity": "shared"},
        "stage_delta": {"stage": stage, "identity": f"delta-{stage}"},
        "prior_stage_binding": None,
    }


def _install_normalizers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generator.leakage,
        "_normalize_shared_fixture",
        lambda value, **_kwargs: dict(value),
    )
    monkeypatch.setattr(
        generator.leakage,
        "_normalize_stage_delta",
        lambda value, **_kwargs: dict(value),
    )


def _prior_plan(tmp_path: Path, stage: str) -> Path:
    fixture = {
        "campaign_id": CAMPAIGN,
        "comparable_fixture_group_id": GROUP,
        "shared_fixture": {"serial": SERIAL, "identity": "shared"},
        "stage_delta": {"stage": stage, "identity": f"delta-{stage}"},
    }
    contract = {
        "run_id": f"run-{stage}",
        "board_id": BOARD,
        "topology_stage": stage,
        "configuration": {"serial": SERIAL},
        "fixture_evidence": fixture,
        "fixture_evidence_sha256": generator._canonical_sha256(fixture),
    }
    envelope = {
        "schema": 1,
        "immutable": True,
        "plan_contract": contract,
        "plan_contract_sha256": generator._canonical_sha256(contract),
    }
    path = tmp_path / f"{stage}-plan.json"
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    return path


def _validated_stage_a_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> generator.ValidatedFixtureManifest:
    _install_normalizers(monkeypatch)
    monkeypatch.setattr(
        generator.leakage,
        "_fixture_identity_sets",
        lambda _shared, _delta: (["component-a", "component-b"], ["connection-a"]),
    )
    document = generator.generate_fixture_manifest(
        _draft(STAGES[0]),
        draft_directory=tmp_path,
        stage=STAGES[0],
        board_id=BOARD,
        serial=SERIAL,
        prior_plan_path=None,
        verify_characterization_files=False,
    )
    path = tmp_path / "stage-a.fixture.json"
    generator._write_new(path, document)
    return generator.validate_generated_fixture_manifest(
        path,
        stage=STAGES[0],
        board_id=BOARD,
        serial=SERIAL,
    )


def _placeholder_values(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            values.update(_placeholder_values(item))
    elif isinstance(value, list):
        for item in value:
            values.update(_placeholder_values(item))
    elif isinstance(value, str) and value.startswith("REPLACE_"):
        values.add(value)
    return values


def test_stage_a_normalizes_without_prior_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_normalizers(monkeypatch)
    result = generator.generate_fixture_manifest(
        _draft(STAGES[0]),
        draft_directory=Path("/evidence"),
        stage=STAGES[0],
        board_id=BOARD,
        serial=SERIAL,
        prior_plan_path=None,
        verify_characterization_files=False,
    )

    assert result["prior_stage_binding"] is None
    assert result["shared_fixture"]["serial"] == SERIAL


def test_unchanged_stage_a_template_is_rejected_before_normalization() -> None:
    draft = json.loads(
        (TEMPLATE_DIRECTORY / "fixture_manifest_v2.stage-a.template.json").read_text(
            encoding="utf-8"
        )
    )
    with pytest.raises(
        generator.FixtureManifestGenerationError,
        match="unresolved placeholder",
    ):
        generator.generate_fixture_manifest(
            draft,
            draft_directory=TEMPLATE_DIRECTORY,
            stage=STAGES[0],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=None,
            verify_characterization_files=False,
        )


def test_nested_placeholder_is_rejected_even_with_stub_normalizers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    draft = _draft(STAGES[0])
    draft["stage_delta"] = {
        "stage": STAGES[0],
        "nested": {"rating": "prefix-REPLACE_INVENTED_RATING-suffix"},
    }
    with pytest.raises(
        generator.FixtureManifestGenerationError,
        match=r"\$\.stage_delta\.nested\.rating",
    ):
        generator.generate_fixture_manifest(
            draft,
            draft_directory=Path("/evidence"),
            stage=STAGES[0],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=None,
            verify_characterization_files=False,
        )


@pytest.mark.parametrize(
    ("stage", "prior_stage"),
    list(zip(STAGES[1:], STAGES[:-1], strict=True)),
)
def test_later_stage_derives_exact_immediate_prior_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    prior_stage: str,
) -> None:
    _install_normalizers(monkeypatch)
    prior_path = _prior_plan(tmp_path, prior_stage)
    checked: list[dict[str, Any]] = []

    def validate_prior(value: object, **_kwargs: object) -> dict[str, Any]:
        assert isinstance(value, dict)
        checked.append(value)
        return value

    monkeypatch.setattr(generator.leakage, "_prior_stage_binding_from_plan", validate_prior)
    result = generator.generate_fixture_manifest(
        _draft(stage),
        draft_directory=tmp_path,
        stage=stage,
        board_id=BOARD,
        serial=SERIAL,
        prior_plan_path=prior_path,
        verify_characterization_files=False,
    )

    binding = result["prior_stage_binding"]
    assert binding == checked[0]
    assert binding["stage"] == prior_stage
    assert binding["plan_path"] == str(prior_path.resolve())
    assert binding["plan_file_sha256"] == hashlib.sha256(prior_path.read_bytes()).hexdigest()


def test_rejects_nonadjacent_prior_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    with pytest.raises(
        generator.FixtureManifestGenerationError,
        match="immediate board/serial topology predecessor",
    ):
        generator.generate_fixture_manifest(
            _draft(STAGES[3]),
            draft_directory=tmp_path,
            stage=STAGES[3],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=_prior_plan(tmp_path, STAGES[0]),
            verify_characterization_files=False,
        )


def test_rejects_hand_copied_prior_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    draft = _draft(STAGES[1])
    draft["prior_stage_binding"] = {"plan_file_sha256": "0" * 64}
    with pytest.raises(generator.FixtureManifestGenerationError, match="must be null"):
        generator.generate_fixture_manifest(
            draft,
            draft_directory=tmp_path,
            stage=STAGES[1],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=_prior_plan(tmp_path, STAGES[0]),
            verify_characterization_files=False,
        )


def test_rejects_prior_plan_hash_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    prior = _prior_plan(tmp_path, STAGES[0])
    document = json.loads(prior.read_text(encoding="utf-8"))
    document["plan_contract"]["run_id"] = "tampered"
    prior.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(generator.FixtureManifestGenerationError, match="envelope is invalid"):
        generator.generate_fixture_manifest(
            _draft(STAGES[1]),
            draft_directory=tmp_path,
            stage=STAGES[1],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=prior,
            verify_characterization_files=False,
        )


def test_rejects_placeholder_recursively_inside_prior_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    prior = _prior_plan(tmp_path, STAGES[0])
    document = json.loads(prior.read_text(encoding="utf-8"))
    document["plan_contract"]["fixture_evidence"]["operator_note"] = "REPLACE_NOTE"
    document["plan_contract"]["fixture_evidence_sha256"] = generator._canonical_sha256(
        document["plan_contract"]["fixture_evidence"]
    )
    document["plan_contract_sha256"] = generator._canonical_sha256(document["plan_contract"])
    prior.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        generator.FixtureManifestGenerationError,
        match=r"\$\.prior_plan.*REPLACE_NOTE",
    ):
        generator.generate_fixture_manifest(
            _draft(STAGES[1]),
            draft_directory=tmp_path,
            stage=STAGES[1],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=prior,
            verify_characterization_files=False,
        )


def test_setup_draft_derives_hashes_and_sorted_inventories_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _validated_stage_a_fixture(tmp_path, monkeypatch)
    draft = generator.generate_setup_attestation_draft(fixture, run_id="stage-a-run-001")

    assert draft["run_id"] == "stage-a-run-001"
    assert draft["campaign_id"] == CAMPAIGN
    assert draft["comparable_fixture_group_id"] == GROUP
    assert draft["stage"] == STAGES[0]
    assert draft["fixture_manifest_sha256"] == hashlib.sha256(fixture.path.read_bytes()).hexdigest()
    assert draft["shared_fixture_sha256"] == generator._canonical_sha256(
        fixture.document["shared_fixture"]
    )
    assert draft["stage_delta_sha256"] == generator._canonical_sha256(
        fixture.document["stage_delta"]
    )
    assert draft["observed_component_ids"] == ["component-a", "component-b"]
    assert draft["observed_connection_ids"] == ["connection-a"]
    assert draft["selector_flash_evidence"] is None
    assert _placeholder_values(draft) == {
        "REPLACE_UNIQUE_STAGE_A_SETUP_ATTESTATION_ID",
        "REPLACE_TIMEZONE_QUALIFIED_ISO_8601_TIMESTAMP",
        "REPLACE_SETUP_PHOTO_OR_DIAGRAM_PATH",
        "REPLACE_SETUP_EVIDENCE_SHA256",
    }


@pytest.mark.parametrize(
    ("stage", "selector_expected"),
    ((STAGES[0], False), (STAGES[1], False), (STAGES[2], True), (STAGES[3], True)),
)
def test_setup_draft_retains_selector_observation_only_for_connected_stages(
    stage: str,
    selector_expected: bool,
) -> None:
    fixture = generator.ValidatedFixtureManifest(
        path=Path("/home/pi/stage.fixture.json"),
        file_sha256="1" * 64,
        document={
            "stage": stage,
            "campaign_id": CAMPAIGN,
            "comparable_fixture_group_id": GROUP,
        },
        shared_fixture_sha256="2" * 64,
        stage_delta_sha256="3" * 64,
        component_ids=["component-a"],
        connection_ids=["connection-a"],
    )
    draft = generator.generate_setup_attestation_draft(fixture, run_id="run-001")

    selector = draft["selector_flash_evidence"]
    assert (selector is not None) is selector_expected
    if selector_expected:
        assert selector == {
            "path": "REPLACE_ABSOLUTE_SEALED_SELECTOR_FLASH_EVIDENCE_PATH",
            "sha256": "REPLACE_SELECTOR_FLASH_EVIDENCE_SHA256",
            "run_id": "REPLACE_SELECTOR_FLASH_RUN_ID",
        }


def test_setup_fixture_validation_rejects_noncanonical_or_placeholder_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    monkeypatch.setattr(
        generator.leakage,
        "_fixture_identity_sets",
        lambda _shared, _delta: ([], []),
    )
    document = generator.generate_fixture_manifest(
        _draft(STAGES[0]),
        draft_directory=tmp_path,
        stage=STAGES[0],
        board_id=BOARD,
        serial=SERIAL,
        prior_plan_path=None,
        verify_characterization_files=False,
    )
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(generator.FixtureManifestGenerationError, match="not canonical"):
        generator.validate_generated_fixture_manifest(
            noncanonical,
            stage=STAGES[0],
            board_id=BOARD,
            serial=SERIAL,
        )

    document["campaign_id"] = "REPLACE_CAMPAIGN_ID"
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_bytes(generator._canonical(document))
    with pytest.raises(generator.FixtureManifestGenerationError, match="unresolved placeholder"):
        generator.validate_generated_fixture_manifest(
            placeholder,
            stage=STAGES[0],
            board_id=BOARD,
            serial=SERIAL,
        )


def test_declared_fixture_evidence_rejects_symlinked_file(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.s2p"
    evidence.write_text("exact evidence", encoding="utf-8")
    linked = tmp_path / "linked.s2p"
    linked.symlink_to(evidence)

    with pytest.raises(generator.FixtureManifestGenerationError, match="contains a symlink"):
        generator._assert_declared_fixture_paths_are_local_files(
            {"characterization": {"evidence_path": str(linked)}},
            base_directory=tmp_path,
        )


def test_output_is_new_read_only_and_nonoverwriting(tmp_path: Path) -> None:
    output = tmp_path / "fixture.json"
    generator._write_new(output, {"schema": 2, "fixture": "a"})

    assert json.loads(output.read_text(encoding="utf-8"))["fixture"] == "a"
    assert output.stat().st_mode & 0o222 == 0
    with pytest.raises(generator.FixtureManifestGenerationError, match="already exists"):
        generator._write_new(output, {"schema": 2, "fixture": "b"})


def test_setup_draft_output_is_create_only_private_and_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "setup-draft.json"
    generator._write_new(
        output,
        {"schema": 1, "attestation": "draft"},
        mode=0o600,
        label="setup draft",
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(generator.FixtureManifestGenerationError, match="already exists"):
        generator._write_new(
            output,
            {"schema": 1, "attestation": "replacement"},
            mode=0o600,
            label="setup draft",
        )

    rejected = tmp_path / "rejected.json"
    monkeypatch.setattr(
        generator,
        "assert_local_rpi_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            generator.FileArtifactAdmissionError("not local storage")
        ),
    )
    with pytest.raises(generator.FixtureManifestGenerationError, match="not local storage"):
        generator._write_new(rejected, {"schema": 1}, mode=0o600, label="setup draft")


def test_setup_draft_cli_creates_run_bound_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _validated_stage_a_fixture(tmp_path, monkeypatch)
    output = tmp_path / "run-001.setup-draft.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--stage",
            STAGES[0],
            "--board-id",
            BOARD,
            "--serial",
            SERIAL,
            "--setup-from-fixture",
            str(fixture.path),
            "--setup-run-id",
            "run-001",
            "--setup-draft-output",
            str(output),
        ],
    )

    assert generator.main() == 0
    draft = json.loads(output.read_text(encoding="utf-8"))
    status = json.loads(capsys.readouterr().out)
    assert draft["run_id"] == "run-001"
    assert draft["fixture_manifest_sha256"] == fixture.file_sha256
    assert status["artifact_kind"] == "run_bound_setup_attestation_draft"
    assert status["rf_activity"] is False


def test_output_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(generator.FixtureManifestGenerationError, match="contains a symlink"):
        generator._write_new(linked_parent / "draft.json", {"schema": 1}, mode=0o600)
    assert list(real_parent.iterdir()) == []


def test_output_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    output = tmp_path / "fixture.json"
    output.symlink_to(target)

    with pytest.raises(generator.FixtureManifestGenerationError, match="already exists"):
        generator._write_new(output, {"schema": 2})


def test_input_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    source = tmp_path / "source.json"
    source.symlink_to(target)

    with pytest.raises(generator.FixtureManifestGenerationError, match="must not contain symlinks"):
        generator._read_json(source, "fixture draft")


def test_input_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    source = real_directory / "source.json"
    source.write_text("{}", encoding="utf-8")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(generator.FixtureManifestGenerationError, match="must not contain symlinks"):
        generator._read_json(linked_directory / "source.json", "fixture draft")


def test_prior_binding_rejects_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_normalizers(monkeypatch)
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    prior = _prior_plan(real_directory, STAGES[0])
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(generator.FixtureManifestGenerationError, match="must not contain symlinks"):
        generator.generate_fixture_manifest(
            _draft(STAGES[1]),
            draft_directory=tmp_path,
            stage=STAGES[1],
            board_id=BOARD,
            serial=SERIAL,
            prior_plan_path=linked_directory / prior.name,
            verify_characterization_files=False,
        )
