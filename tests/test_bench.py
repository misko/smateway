import json
import struct
from pathlib import Path

import pytest

from smateway.bench import (
    STATUS_COMMAND_VALID,
    STATUS_GUARD_ACTIVE,
    STATUS_LEASE_ACTIVE,
    BenchManifest,
    decode_mailbox,
    next_sequence,
)


def write_manifest(path: Path) -> BenchManifest:
    document = {
        "schema": 1,
        "elf_sha256": "a" * 64,
        "mailbox": {
            "address": 0x20000004,
            "size": 36,
            "magic": 0x534D4757,
            "version": 1,
            "max_lease_ms": 5000,
            "offsets": {
                "magic": 0,
                "version": 4,
                "command_sequence": 8,
                "command_code": 12,
                "command_lease_ms": 16,
                "acknowledged_sequence": 20,
                "applied_code": 24,
                "remaining_lease_ms": 28,
                "status_flags": 32,
            },
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return BenchManifest.load(path)


def test_decode_mailbox_and_flags(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json")
    data = struct.pack(
        "<9I",
        manifest.magic,
        manifest.version,
        7,
        6,
        1000,
        7,
        8,
        995,
        STATUS_COMMAND_VALID | STATUS_LEASE_ACTIVE | STATUS_GUARD_ACTIVE,
    )

    status = decode_mailbox(data, manifest)

    assert status.command_valid
    assert status.lease_active
    assert status.guard_active
    assert not status.invalid_command
    assert status.applied_code == 8


def test_decode_rejects_wrong_image(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json")
    data = struct.pack("<9I", 0, manifest.version, 0, 0, 0, 0, 0, 0, 0)

    with pytest.raises(ValueError, match="magic mismatch"):
        decode_mailbox(data, manifest)


def test_sequence_wrap_skips_reserved_zero() -> None:
    assert next_sequence(41) == 42
    assert next_sequence(0xFFFFFFFF) == 1
