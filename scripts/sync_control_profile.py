#!/usr/bin/env python3
"""Import and verify the circuits control profile with exact provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PROJECT = Path("projects/pluto-rx2-8way-v5")
CONTRACT = PROJECT / "03_src/rules/control_protocol.yaml"
HEADER = PROJECT / "05_firmware/include/control_profile.h"
DECODER = PROJECT / "05_firmware/host/control_profile.json"
DESTINATION = Path("profiles/fast20-v1")
HEADER_DIGEST = re.compile(
    r'^#define CONTROL_PROFILE_CONTRACT_SHA256 "([0-9a-f]{64})"$', re.MULTILINE
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def expected(circuits_root: Path) -> dict[Path, bytes]:
    contract = circuits_root / CONTRACT
    header = circuits_root / HEADER
    decoder = circuits_root / DECODER
    for path in (contract, header, decoder):
        if not path.is_file():
            raise FileNotFoundError(path)

    contract_digest = sha256(contract)
    header_text = header.read_text(encoding="utf-8")
    match = HEADER_DIGEST.search(header_text)
    if match is None or match.group(1) != contract_digest:
        raise ValueError("firmware header does not identify the authoritative contract hash")
    decoder_document = json.loads(decoder.read_text(encoding="utf-8"))
    if decoder_document.get("contract_sha256") != contract_digest:
        raise ValueError("decoder JSON does not identify the authoritative contract hash")

    provenance = {
        "schema": 1,
        "source_repository": "https://github.com/misko/circuits.git",
        "source_commit": git_head(circuits_root),
        "source_contract": CONTRACT.as_posix(),
        "contract_sha256": contract_digest,
        "artifacts": {
            "control_profile.h": sha256(header),
            "control_profile.json": sha256(decoder),
        },
    }
    return {
        DESTINATION / "control_protocol.yaml": contract.read_bytes(),
        DESTINATION / "control_profile.h": header.read_bytes(),
        DESTINATION / "control_profile.json": decoder.read_bytes(),
        DESTINATION / "provenance.json": (
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        ).encode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--circuits-root", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()

    root = args.circuits_root.expanduser().resolve(strict=True)
    wanted = expected(root)
    if args.write:
        for destination, content in wanted.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        print("PROFILE WRITE: fast20-v1 and provenance imported")
        return 0

    stale = [
        str(path)
        for path, content in wanted.items()
        if not path.is_file() or path.read_bytes() != content
    ]
    if stale:
        print("PROFILE STALE: " + ", ".join(stale))
        return 1
    print("PROFILE PASS: fast20-v1 artifacts and provenance exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
