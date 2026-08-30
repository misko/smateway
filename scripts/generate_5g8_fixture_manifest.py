#!/usr/bin/env python3
"""Generate fixture-v2 manifests and run-bound setup-attestation drafts.

The stage-specific physical graph remains explicit operator-authored evidence.
For B/C/E this tool derives the only accepted ``prior_stage_binding`` from the
immediately prior immutable plan; callers never hand-copy plan hashes.

Once a fixture manifest exists, setup-draft mode revalidates that canonical
manifest and derives every fixture hash and sorted identity inventory.  The
result contains placeholders only for evidence that must be observed for the
specific physical setup.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPOSITORY = Path(__file__).resolve().parents[1]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

leakage: Any = importlib.import_module("scripts.run_5g8_leakage_ladder")

from smateway.file_artifact_admission import (  # noqa: E402
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
)


class FixtureManifestGenerationError(RuntimeError):
    """A draft cannot become a source-bound fixture manifest."""


_PLACEHOLDER = re.compile(r"REPLACE_[A-Za-z0-9_]+")
_STAGE_LABELS = {
    "direct_rx2_termination": "A",
    "rx2_cable_terminated": "B",
    "powered_selector_all_inputs_terminated": "C",
    "full_conducted_fixture": "E",
}


@dataclass(frozen=True)
class ValidatedFixtureManifest:
    """Canonical fixture bytes and identities safe to copy into a setup draft."""

    path: Path
    file_sha256: str
    document: dict[str, Any]
    shared_fixture_sha256: str
    stage_delta_sha256: str
    component_ids: list[str]
    connection_ids: list[str]


def _assert_no_placeholders(value: object, *, location: str = "$") -> None:
    """Reject every unresolved template token before any normalization occurs."""

    if isinstance(value, str):
        match = _PLACEHOLDER.search(value)
        if match is not None:
            raise FixtureManifestGenerationError(
                f"fixture draft contains unresolved placeholder at {location}: {match.group(0)}"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _PLACEHOLDER.search(key_text) is not None:
                raise FixtureManifestGenerationError(
                    f"fixture draft contains unresolved placeholder key at {location}"
                )
            _assert_no_placeholders(item, location=f"{location}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_placeholders(item, location=f"{location}[{index}]")


def _assert_declared_fixture_paths_are_local_files(
    value: object,
    *,
    base_directory: Path,
    location: str = "$.generated_fixture",
) -> None:
    """Reject symlinked/non-local characterization and prior-plan paths."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_location = f"{location}.{key}"
            if key in {"evidence_path", "plan_path"} and item is not None:
                if not isinstance(item, str) or not item:
                    raise FixtureManifestGenerationError(
                        f"generated fixture declared path is malformed at {child_location}"
                    )
                path = Path(item).expanduser()
                if not path.is_absolute():
                    path = base_directory / path
                try:
                    exact = assert_no_symlink_chain(path, label=child_location)
                    assert_local_rpi_storage(exact, label=f"{child_location} storage")
                except FileArtifactAdmissionError as error:
                    raise FixtureManifestGenerationError(str(error)) from error
                if not exact.is_file():
                    raise FixtureManifestGenerationError(
                        f"generated fixture declared path is not a file at {child_location}"
                    )
            else:
                _assert_declared_fixture_paths_are_local_files(
                    item,
                    base_directory=base_directory,
                    location=child_location,
                )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_declared_fixture_paths_are_local_files(
                item,
                base_directory=base_directory,
                location=f"{location}[{index}]",
            )


def _assert_no_symlink_chain(path: Path, label: str) -> None:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            current.lstat()
        except FileNotFoundError:
            raise FixtureManifestGenerationError(f"{label} does not exist: {current}") from None
        if current.is_symlink():
            raise FixtureManifestGenerationError(f"{label} must not contain symlinks: {current}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    candidate = path.expanduser().absolute()
    _assert_no_symlink_chain(candidate, label)
    exact = candidate.resolve(strict=True)
    if not exact.is_file():
        raise FixtureManifestGenerationError(f"{label} must be a real regular file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureManifestGenerationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise FixtureManifestGenerationError(f"{label} root must be an object")
    return value


def _canonical(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FixtureManifestGenerationError(f"{label} is not a lowercase SHA-256")
    return value


def _prior_binding(
    prior_plan_path: Path,
    *,
    stage: str,
    board_id: str,
    serial: str,
) -> dict[str, Any]:
    expected_prior = leakage.PRIOR_STAGE[stage]
    if expected_prior is None:
        raise FixtureManifestGenerationError("Stage A must not bind a prior plan")
    candidate_path = prior_plan_path.expanduser().absolute()
    envelope = _read_json(candidate_path, "prior immutable plan")
    _assert_no_placeholders(envelope, location="$.prior_plan")
    exact_path = candidate_path.resolve(strict=True)
    contract = envelope.get("plan_contract")
    contract_sha = envelope.get("plan_contract_sha256")
    if (
        envelope.get("schema") != 1
        or envelope.get("immutable") is not True
        or not isinstance(contract, Mapping)
        or contract_sha != _canonical_sha256(contract)
    ):
        raise FixtureManifestGenerationError("prior immutable plan envelope is invalid")
    configuration = contract.get("configuration")
    fixture = contract.get("fixture_evidence")
    if (
        contract.get("topology_stage") != expected_prior
        or contract.get("board_id") != board_id
        or not isinstance(configuration, Mapping)
        or configuration.get("serial") != serial
        or not isinstance(fixture, Mapping)
    ):
        raise FixtureManifestGenerationError(
            "prior plan is not the immediate board/serial topology predecessor"
        )
    fixture_sha = _validate_sha256(
        contract.get("fixture_evidence_sha256"),
        "prior fixture-evidence hash",
    )
    if _canonical_sha256(fixture) != fixture_sha:
        raise FixtureManifestGenerationError("prior fixture evidence hash is inconsistent")
    run_id = contract.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise FixtureManifestGenerationError("prior plan run ID is missing")
    return {
        "stage": expected_prior,
        "run_id": run_id,
        "plan_path": str(exact_path),
        "plan_file_sha256": _sha256(exact_path),
        "plan_contract_sha256": _validate_sha256(contract_sha, "prior plan-contract hash"),
        "fixture_evidence_sha256": fixture_sha,
    }


def generate_fixture_manifest(
    draft: Mapping[str, Any],
    *,
    draft_directory: Path,
    stage: str,
    board_id: str,
    serial: str,
    prior_plan_path: Path | None,
    verify_characterization_files: bool = True,
) -> dict[str, Any]:
    """Normalize a draft and derive its immutable prior-stage binding."""

    _assert_no_placeholders(draft)
    if stage not in leakage.STAGES:
        raise FixtureManifestGenerationError("stage must be one of A/B/C/E runner enums")
    expected_fields = {
        "schema",
        "fixture_kind",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "board_id",
        "shared_fixture",
        "stage_delta",
        "prior_stage_binding",
    }
    if set(draft) != expected_fields:
        raise FixtureManifestGenerationError("fixture draft fields are incomplete or unexpected")
    if (
        draft.get("schema") != 2
        or draft.get("fixture_kind") != leakage.FIXTURE_KIND_V2
        or draft.get("stage") != stage
        or draft.get("board_id") != board_id
    ):
        raise FixtureManifestGenerationError("fixture draft identity differs from CLI")
    try:
        campaign_id = leakage._validate_identifier(str(draft["campaign_id"]), "campaign ID")
        group_id = leakage._validate_identifier(
            str(draft["comparable_fixture_group_id"]),
            "comparable fixture group ID",
        )
        shared = leakage._normalize_shared_fixture(
            draft["shared_fixture"],
            expected_serial=serial,
            base_directory=draft_directory,
            verify_files=verify_characterization_files,
        )
        delta = leakage._normalize_stage_delta(
            draft["stage_delta"],
            stage=stage,
            shared=shared,
            base_directory=draft_directory,
            verify_files=verify_characterization_files,
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise FixtureManifestGenerationError(str(error)) from error
    _assert_no_placeholders(shared, location="$.normalized.shared_fixture")
    _assert_no_placeholders(delta, location="$.normalized.stage_delta")
    shared_sha = _canonical_sha256(shared)
    if leakage.PRIOR_STAGE[stage] is None:
        if prior_plan_path is not None or draft["prior_stage_binding"] is not None:
            raise FixtureManifestGenerationError("Stage A prior-stage binding must be null")
        prior: dict[str, Any] | None = None
    else:
        if prior_plan_path is None:
            raise FixtureManifestGenerationError("B/C/E require --prior-plan")
        if draft["prior_stage_binding"] is not None:
            raise FixtureManifestGenerationError(
                "B/C/E draft prior_stage_binding must be null; the generator derives it"
            )
        prior = _prior_binding(
            prior_plan_path,
            stage=stage,
            board_id=board_id,
            serial=serial,
        )
        try:
            leakage._prior_stage_binding_from_plan(
                prior,
                stage=stage,
                campaign_id=campaign_id,
                comparable_fixture_group_id=group_id,
                shared_fixture_sha256=shared_sha,
                current_stage_delta=delta,
                board_id=board_id,
                serial=serial,
                base_directory=draft_directory,
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise FixtureManifestGenerationError(str(error)) from error
    return {
        "schema": 2,
        "fixture_kind": leakage.FIXTURE_KIND_V2,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": group_id,
        "stage": stage,
        "board_id": board_id,
        "shared_fixture": shared,
        "stage_delta": delta,
        "prior_stage_binding": prior,
    }


def validate_generated_fixture_manifest(
    fixture_manifest_path: Path,
    *,
    stage: str,
    board_id: str,
    serial: str,
) -> ValidatedFixtureManifest:
    """Reopen one canonical generator output and derive its trusted identities."""

    candidate = fixture_manifest_path.expanduser().absolute()
    try:
        assert_no_symlink_chain(candidate, label="generated fixture manifest")
        assert_local_rpi_storage(candidate, label="generated fixture manifest storage")
    except FileArtifactAdmissionError as error:
        raise FixtureManifestGenerationError(str(error)) from error
    raw = _read_json(candidate, "generated fixture manifest")
    exact = candidate.resolve(strict=True)
    _assert_no_placeholders(raw, location="$.generated_fixture")
    _assert_declared_fixture_paths_are_local_files(raw, base_directory=exact.parent)
    expected_fields = {
        "schema",
        "fixture_kind",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "board_id",
        "shared_fixture",
        "stage_delta",
        "prior_stage_binding",
    }
    if set(raw) != expected_fields:
        raise FixtureManifestGenerationError(
            "generated fixture manifest fields are incomplete or unexpected"
        )
    if (
        stage not in leakage.STAGES
        or raw.get("schema") != 2
        or raw.get("fixture_kind") != leakage.FIXTURE_KIND_V2
        or raw.get("stage") != stage
        or raw.get("board_id") != board_id
    ):
        raise FixtureManifestGenerationError(
            "generated fixture manifest identity differs from the requested setup"
        )
    try:
        campaign_id = leakage._validate_identifier(str(raw["campaign_id"]), "campaign ID")
        group_id = leakage._validate_identifier(
            str(raw["comparable_fixture_group_id"]),
            "comparable fixture group ID",
        )
        shared = leakage._normalize_shared_fixture(
            raw["shared_fixture"],
            expected_serial=serial,
            base_directory=exact.parent,
            verify_files=True,
        )
        delta = leakage._normalize_stage_delta(
            raw["stage_delta"],
            stage=stage,
            shared=shared,
            base_directory=exact.parent,
            verify_files=True,
        )
        shared_sha = _canonical_sha256(shared)
        prior = leakage._prior_stage_binding_from_plan(
            raw["prior_stage_binding"],
            stage=stage,
            campaign_id=campaign_id,
            comparable_fixture_group_id=group_id,
            shared_fixture_sha256=shared_sha,
            current_stage_delta=delta,
            board_id=board_id,
            serial=serial,
            base_directory=exact.parent,
        )
        component_ids, connection_ids = leakage._fixture_identity_sets(shared, delta)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        raise FixtureManifestGenerationError(
            f"generated fixture manifest validation failed: {error}"
        ) from error
    normalized = {
        "schema": 2,
        "fixture_kind": leakage.FIXTURE_KIND_V2,
        "campaign_id": campaign_id,
        "comparable_fixture_group_id": group_id,
        "stage": stage,
        "board_id": board_id,
        "shared_fixture": shared,
        "stage_delta": delta,
        # The manifest intentionally stores the minimal prior-plan binding;
        # the runner expands it into ``prior`` when constructing a plan.
        "prior_stage_binding": raw["prior_stage_binding"],
    }
    if normalized != raw:
        raise FixtureManifestGenerationError(
            "generated fixture manifest is not the canonical normalized fixture"
        )
    if (prior is None) != (leakage.PRIOR_STAGE[stage] is None):
        raise FixtureManifestGenerationError(
            "generated fixture manifest prior-stage binding is inconsistent"
        )
    if exact.read_bytes() != _canonical(raw):
        raise FixtureManifestGenerationError(
            "fixture manifest bytes are not canonical generator output"
        )
    return ValidatedFixtureManifest(
        path=exact,
        file_sha256=_sha256(exact),
        document=normalized,
        shared_fixture_sha256=shared_sha,
        stage_delta_sha256=_canonical_sha256(delta),
        component_ids=component_ids,
        connection_ids=connection_ids,
    )


def generate_setup_attestation_draft(
    fixture: ValidatedFixtureManifest,
    *,
    run_id: str,
) -> dict[str, Any]:
    """Derive a run-bound setup draft with only physical-observation placeholders."""

    try:
        exact_run_id = leakage._validate_identifier(run_id, "run ID")
    except ValueError as error:
        raise FixtureManifestGenerationError(str(error)) from error
    stage = str(fixture.document["stage"])
    stage_label = _STAGE_LABELS.get(stage)
    if stage_label is None:
        raise FixtureManifestGenerationError("validated fixture has an unsupported setup stage")
    selector_flash: dict[str, str] | None
    if stage in leakage.SELECTOR_CONNECTED_STAGES:
        selector_flash = {
            "path": "REPLACE_ABSOLUTE_SEALED_SELECTOR_FLASH_EVIDENCE_PATH",
            "sha256": "REPLACE_SELECTOR_FLASH_EVIDENCE_SHA256",
            "run_id": "REPLACE_SELECTOR_FLASH_RUN_ID",
        }
    else:
        selector_flash = None
    return {
        "schema": 1,
        "attestation_kind": leakage.SETUP_ATTESTATION_KIND,
        "attestation_id": f"REPLACE_UNIQUE_STAGE_{stage_label}_SETUP_ATTESTATION_ID",
        "created_at": "REPLACE_TIMEZONE_QUALIFIED_ISO_8601_TIMESTAMP",
        "run_id": exact_run_id,
        "campaign_id": fixture.document["campaign_id"],
        "comparable_fixture_group_id": fixture.document["comparable_fixture_group_id"],
        "stage": stage,
        "fixture_manifest_sha256": fixture.file_sha256,
        "shared_fixture_sha256": fixture.shared_fixture_sha256,
        "stage_delta_sha256": fixture.stage_delta_sha256,
        "observed_component_ids": list(fixture.component_ids),
        "observed_connection_ids": list(fixture.connection_ids),
        "selector_flash_evidence": selector_flash,
        "setup_evidence_path": "REPLACE_SETUP_PHOTO_OR_DIAGRAM_PATH",
        "setup_evidence_sha256": "REPLACE_SETUP_EVIDENCE_SHA256",
    }


def _write_new(
    path: Path,
    document: Mapping[str, Any],
    *,
    mode: int = 0o444,
    label: str = "fixture manifest output",
) -> None:
    if mode not in {0o444, 0o600}:
        raise FixtureManifestGenerationError("unsupported create-only output mode")
    raw_output = path.expanduser().absolute()
    if raw_output.exists() or raw_output.is_symlink():
        raise FixtureManifestGenerationError("output already exists; fixture evidence is immutable")
    try:
        output = assert_no_symlink_chain(raw_output, label=label)
        assert_local_rpi_storage(output, label=f"{label} storage")
    except FileArtifactAdmissionError as error:
        raise FixtureManifestGenerationError(str(error)) from error
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert_no_symlink_chain(output.parent, label=f"{label} parent")
        assert_local_rpi_storage(output.parent, label=f"{label} parent storage")
    except FileArtifactAdmissionError as error:
        raise FixtureManifestGenerationError(str(error)) from error
    payload = _canonical(document)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.link(temporary, output, follow_symlinks=False)
        directory_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise FixtureManifestGenerationError(
            "could not atomically create fixture manifest"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--stage", choices=leakage.STAGES, required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--prior-plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--setup-from-fixture",
        type=Path,
        help="canonical generated fixture manifest from which to derive a setup draft",
    )
    parser.add_argument("--setup-run-id")
    parser.add_argument("--setup-draft-output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    fixture_mode = args.draft is not None
    setup_mode = args.setup_from_fixture is not None
    if fixture_mode == setup_mode:
        raise SystemExit("choose exactly one of --draft or --setup-from-fixture")
    try:
        if fixture_mode:
            if any((args.setup_run_id, args.setup_draft_output)):
                raise FixtureManifestGenerationError("fixture mode forbids setup-draft arguments")
            if args.validate_only == (args.output is not None):
                raise FixtureManifestGenerationError(
                    "choose exactly one of --validate-only or --output"
                )
            draft_input = args.draft.expanduser().absolute()
            draft = _read_json(draft_input, "fixture draft")
            draft_path = draft_input.resolve(strict=True)
            generated = generate_fixture_manifest(
                draft,
                draft_directory=draft_path.parent,
                stage=args.stage,
                board_id=args.board_id,
                serial=args.serial,
                prior_plan_path=args.prior_plan,
            )
            if args.output is not None:
                _write_new(args.output, generated)
            result = {
                "artifact_kind": "fixture_manifest_v2",
                "stage": args.stage,
                "board_id": args.board_id,
                "serial": args.serial,
                "prior_stage": leakage.PRIOR_STAGE[args.stage],
                "prior_binding_derived": generated["prior_stage_binding"] is not None,
                "canonical_manifest_sha256": hashlib.sha256(_canonical(generated)).hexdigest(),
                "output": (
                    None if args.output is None else str(args.output.expanduser().absolute())
                ),
                "rf_activity": False,
            }
        else:
            if (
                args.prior_plan is not None
                or args.output is not None
                or args.validate_only
                or not args.setup_run_id
                or args.setup_draft_output is None
            ):
                raise FixtureManifestGenerationError(
                    "setup mode requires --setup-run-id and --setup-draft-output and forbids "
                    "--prior-plan, --output, and --validate-only"
                )
            fixture = validate_generated_fixture_manifest(
                args.setup_from_fixture,
                stage=args.stage,
                board_id=args.board_id,
                serial=args.serial,
            )
            generated = generate_setup_attestation_draft(
                fixture,
                run_id=args.setup_run_id,
            )
            _write_new(
                args.setup_draft_output,
                generated,
                mode=0o600,
                label="setup-attestation draft output",
            )
            result = {
                "artifact_kind": "run_bound_setup_attestation_draft",
                "stage": args.stage,
                "board_id": args.board_id,
                "serial": args.serial,
                "run_id": args.setup_run_id,
                "fixture_manifest": str(fixture.path),
                "fixture_manifest_sha256": fixture.file_sha256,
                "shared_fixture_sha256": fixture.shared_fixture_sha256,
                "stage_delta_sha256": fixture.stage_delta_sha256,
                "component_count": len(fixture.component_ids),
                "connection_count": len(fixture.connection_ids),
                "output": str(args.setup_draft_output.expanduser().absolute()),
                "operator_observation_placeholders_retained": True,
                "rf_activity": False,
            }
    except FixtureManifestGenerationError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
