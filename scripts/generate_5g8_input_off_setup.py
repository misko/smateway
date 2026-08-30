#!/usr/bin/env python3
"""Derive one run-bound P2 setup-attestation draft from an exact fixture-v2 file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from smateway.file_artifact_admission import (  # noqa: E402
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
    read_json_file,
)
from smateway.input_off_control import (  # noqa: E402
    CAMPAIGN_ID,
    SETUP_KIND,
    TOPOLOGY_STAGE,
    InputOffContractError,
    validate_fixture_v2,
)


class SetupDraftError(RuntimeError):
    """The fixture cannot produce a safe run-bound P2 setup draft."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def generate_setup_draft(
    fixture_path: Path,
    *,
    run_id: str,
    board_id: str,
    serial: str,
) -> dict[str, Any]:
    exact = assert_no_symlink_chain(fixture_path.expanduser().absolute(), label="P2 fixture")
    assert_local_rpi_storage(exact, label="P2 fixture storage")
    fixture = validate_fixture_v2(
        read_json_file(exact, label="P2 fixture"),
        run_id=run_id,
        board_id=board_id,
        serial=serial,
    )
    return {
        "schema": 1,
        "attestation_kind": SETUP_KIND,
        "attestation_id": "REPLACE_UNIQUE_P2_SETUP_ATTESTATION_ID",
        "created_at": "REPLACE_TIMEZONE_QUALIFIED_ISO_8601_TIMESTAMP",
        "operator_id": "REPLACE_OPERATOR_ID",
        "run_id": run_id,
        "campaign_id": CAMPAIGN_ID,
        "topology_stage": TOPOLOGY_STAGE,
        "fixture_manifest_sha256": _sha256(exact),
        "observed_component_ids": list(fixture["component_ids"]),
        "observed_connection_ids": list(fixture["connection_ids"]),
        "setup_evidence": {
            "path": "REPLACE_ABSOLUTE_SETUP_PHOTO_OR_DIAGRAM_PATH",
            "sha256": "REPLACE_SETUP_EVIDENCE_SHA256",
            "size_bytes": "REPLACE_SETUP_EVIDENCE_SIZE_BYTES",
        },
        "confirmations": {
            "no_antennas": False,
            "tx1_matched_two_way_still_feeds_protected_rx1": False,
            "tx1_stimulus_branch_has_own_rated_50ohm_load": False,
            "eight_way_input_has_separate_rated_50ohm_load": False,
            "two_loads_and_reference_planes_are_distinct": False,
            "all_eight_way_outputs_unchanged": False,
            "selector_and_rx2_common_cable_unchanged": False,
            "rx1_chain_unchanged": False,
            "tx2_terminated_and_muted": False,
            "fast20_live_and_unchanged": False,
            "no_other_component_or_connection_moved_since_p0_evidence": False,
        },
    }


def _write_new(path: Path, document: dict[str, Any]) -> Path:
    output = path.expanduser().absolute()
    assert_no_symlink_chain(output, label="P2 setup draft output")
    assert_local_rpi_storage(output, label="P2 setup draft output storage")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() or output.is_symlink():
        raise SetupDraftError("P2 setup draft output already exists; refusing overwrite")
    payload = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = generate_setup_draft(
            args.fixture_manifest,
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
        )
        output = _write_new(args.output, document)
        print(
            json.dumps(
                {
                    "status": "draft_created",
                    "output": str(output),
                    "fixture_manifest_sha256": document["fixture_manifest_sha256"],
                    "component_count": len(document["observed_component_ids"]),
                    "connection_count": len(document["observed_connection_ids"]),
                    "hardware_access": False,
                    "rf_activity": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (FileArtifactAdmissionError, InputOffContractError, OSError, SetupDraftError) as error:
        print(f"P2 setup draft generation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
