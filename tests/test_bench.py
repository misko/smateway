import json
import re
import struct
from pathlib import Path

import pytest

from smateway.bench import (
    STATUS_COMMAND_VALID,
    STATUS_GUARD_ACTIVE,
    STATUS_LEASE_ACTIVE,
    BenchManifest,
    BenchStatus,
    OpenOcdBench,
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


def test_status_reads_running_target_without_halting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = write_manifest(tmp_path / "manifest.json")
    config = tmp_path / "openocd.cfg"
    config.write_text("# test\n", encoding="utf-8")
    controller = OpenOcdBench(manifest, config)
    commands_seen: list[str] = []

    def fake_run(commands: str) -> str:
        commands_seen.append(commands)
        match = re.search(r"dump_image \{([^}]+)\}", commands)
        assert match is not None
        Path(match.group(1)).write_bytes(
            struct.pack("<9I", manifest.magic, manifest.version, 0, 8, 0, 0, 8, 0, 0)
        )
        return ""

    monkeypatch.setattr(controller, "_run", fake_run)

    assert controller.status().applied_code == 8
    assert all("halt" not in command and "resume" not in command for command in commands_seen)


def test_request_can_wait_for_guard_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = write_manifest(tmp_path / "manifest.json")
    config = tmp_path / "openocd.cfg"
    config.write_text("# test\n", encoding="utf-8")
    controller = OpenOcdBench(manifest, config)

    def status(sequence: int, applied: int, flags: int) -> BenchStatus:
        return BenchStatus(
            command_sequence=sequence,
            command_code=6,
            command_lease_ms=5000,
            acknowledged_sequence=sequence,
            applied_code=applied,
            remaining_lease_ms=4990,
            status_flags=flags,
        )

    observations = iter(
        (
            status(4, 8, 0),
            status(5, 8, STATUS_COMMAND_VALID | STATUS_LEASE_ACTIVE | STATUS_GUARD_ACTIVE),
            status(5, 6, STATUS_COMMAND_VALID | STATUS_LEASE_ACTIVE),
        )
    )
    monkeypatch.setattr(controller, "status", lambda: next(observations))
    monkeypatch.setattr(controller, "_run", lambda commands: "")
    monkeypatch.setattr("smateway.bench.time.sleep", lambda delay: None)

    observed = controller.request(6, 5000, wait_until_applied=True)

    assert observed.applied_code == 6
    assert not observed.guard_active
