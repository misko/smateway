#!/usr/bin/env python3
"""Generate the deterministic exact-5.8-GHz local raw-evidence inventory."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import smateway.capture_continuity as continuity_library
import smateway.evidence_inventory as inventory_library
from smateway.evidence_inventory import (
    CURRENT_CORPUS_FAMILY_COUNTS,
    EvidenceInventoryError,
    build_evidence_inventory,
    canonical_json_bytes,
    sha256_file,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT / "docs/5g8_root_cause_analysis/data/evidence-inventory.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board-state-root",
        type=Path,
        required=True,
        help="one local board directory to scan; no other evidence root is consulted",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if OUTPUT is absent or differs; do not write",
    )
    parser.add_argument(
        "--allow-corpus-drift",
        action="store_true",
        help="inventory changed family counts instead of enforcing the frozen 130-capture corpus",
    )
    return parser


def _source_binding(path: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    try:
        reported = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise EvidenceInventoryError(
            f"generator source is outside the repository: {path}"
        ) from error
    return {"path": reported, "sha256": sha256_file(resolved)}


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    library_path = Path(inventory_library.__file__).resolve()
    continuity_path = Path(continuity_library.__file__).resolve()
    bindings = [
        _source_binding(Path(__file__)),
        _source_binding(library_path),
        _source_binding(continuity_path),
    ]
    inventory = build_evidence_inventory(
        args.board_state_root,
        generator_bindings=bindings,
        expected_family_counts=(
            None if args.allow_corpus_drift else CURRENT_CORPUS_FAMILY_COUNTS
        ),
    )
    content = canonical_json_bytes(inventory)
    if args.check:
        try:
            existing = args.output.read_bytes()
        except OSError as error:
            raise EvidenceInventoryError(f"cannot read inventory for --check: {error}") from error
        if existing != content:
            raise EvidenceInventoryError(f"inventory is stale: {args.output}")
        print(f"verified {args.output} ({len(content)} bytes)")
        return 0
    _write_atomic(args.output, content)
    aggregate = inventory["aggregate_invariants"]
    assert isinstance(aggregate, dict)
    print(
        f"wrote {args.output}: "
        f"{aggregate['unique_raw_capture_count']} unique captures, "
        f"{aggregate['total_unique_raw_data_bytes']} raw bytes"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceInventoryError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
