"""OpenOCD transport for the fail-safe static selector mailbox."""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_COMMAND_VALID = 0x00000001
STATUS_LEASE_ACTIVE = 0x00000002
STATUS_GUARD_ACTIVE = 0x00000004
STATUS_INVALID_COMMAND = 0x80000000


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


@dataclass(frozen=True, slots=True)
class BenchManifest:
    elf_sha256: str
    address: int
    size: int
    magic: int
    version: int
    max_lease_ms: int
    offsets: dict[str, int]

    @classmethod
    def load(cls, path: Path) -> BenchManifest:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), "manifest")
        mailbox = _mapping(document.get("mailbox"), "manifest.mailbox")
        raw_offsets = _mapping(mailbox.get("offsets"), "manifest.mailbox.offsets")
        offsets = {str(name): int(value) for name, value in raw_offsets.items()}
        required = {
            "magic",
            "version",
            "command_sequence",
            "command_code",
            "command_lease_ms",
            "acknowledged_sequence",
            "applied_code",
            "remaining_lease_ms",
            "status_flags",
        }
        if offsets.keys() != required:
            raise ValueError("manifest mailbox offsets do not match protocol")
        size = int(mailbox["size"])
        if size != 36 or sorted(offsets.values()) != list(range(0, size, 4)):
            raise ValueError("manifest mailbox layout is not nine contiguous words")
        digest = str(document["elf_sha256"])
        if len(digest) != 64:
            raise ValueError("manifest ELF SHA-256 is invalid")
        return cls(
            elf_sha256=digest,
            address=int(mailbox["address"]),
            size=size,
            magic=int(mailbox["magic"]),
            version=int(mailbox["version"]),
            max_lease_ms=int(mailbox["max_lease_ms"]),
            offsets=offsets,
        )

    def field_address(self, name: str) -> int:
        return self.address + self.offsets[name]


@dataclass(frozen=True, slots=True)
class BenchStatus:
    command_sequence: int
    command_code: int
    command_lease_ms: int
    acknowledged_sequence: int
    applied_code: int
    remaining_lease_ms: int
    status_flags: int

    @property
    def command_valid(self) -> bool:
        return bool(self.status_flags & STATUS_COMMAND_VALID)

    @property
    def lease_active(self) -> bool:
        return bool(self.status_flags & STATUS_LEASE_ACTIVE)

    @property
    def guard_active(self) -> bool:
        return bool(self.status_flags & STATUS_GUARD_ACTIVE)

    @property
    def invalid_command(self) -> bool:
        return bool(self.status_flags & STATUS_INVALID_COMMAND)

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "command_sequence": self.command_sequence,
            "command_code": self.command_code,
            "command_lease_ms": self.command_lease_ms,
            "acknowledged_sequence": self.acknowledged_sequence,
            "applied_code": self.applied_code,
            "remaining_lease_ms": self.remaining_lease_ms,
            "status_flags": self.status_flags,
            "command_valid": self.command_valid,
            "lease_active": self.lease_active,
            "guard_active": self.guard_active,
            "invalid_command": self.invalid_command,
        }


def decode_mailbox(data: bytes, manifest: BenchManifest) -> BenchStatus:
    if len(data) != manifest.size:
        raise ValueError(f"expected {manifest.size} mailbox bytes, received {len(data)}")
    words = struct.unpack("<9I", data)
    values = {
        name: words[offset // 4]
        for name, offset in manifest.offsets.items()
    }
    if values["magic"] != manifest.magic:
        raise ValueError("bench mailbox magic mismatch; reviewed bench image is not running")
    if values["version"] != manifest.version:
        raise ValueError("bench mailbox version mismatch")
    return BenchStatus(
        command_sequence=values["command_sequence"],
        command_code=values["command_code"],
        command_lease_ms=values["command_lease_ms"],
        acknowledged_sequence=values["acknowledged_sequence"],
        applied_code=values["applied_code"],
        remaining_lease_ms=values["remaining_lease_ms"],
        status_flags=values["status_flags"],
    )


def next_sequence(acknowledged_sequence: int) -> int:
    candidate = (acknowledged_sequence + 1) & 0xFFFFFFFF
    return candidate if candidate != 0 else 1


class OpenOcdBench:
    def __init__(self, manifest: BenchManifest, openocd_config: Path) -> None:
        self.manifest = manifest
        self.openocd_config = openocd_config.resolve(strict=True)

    def _run(self, commands: str) -> str:
        result = subprocess.run(
            ["openocd", "-f", str(self.openocd_config), "-c", commands],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    def status(self) -> BenchStatus:
        with tempfile.TemporaryDirectory(prefix="smateway-mailbox-") as temporary:
            dump_path = Path(temporary) / "mailbox.bin"
            self._run(
                "init; "
                f"dump_image {{{dump_path}}} 0x{self.manifest.address:08x} "
                f"0x{self.manifest.size:x}; shutdown"
            )
            return decode_mailbox(dump_path.read_bytes(), self.manifest)

    def request(self, code: int, lease_ms: int) -> BenchStatus:
        if code < 0 or code > 0xFF:
            raise ValueError("code must fit in one byte")
        if lease_ms < 0 or lease_ms > self.manifest.max_lease_ms:
            raise ValueError(f"lease must be 0..{self.manifest.max_lease_ms} ms")
        current = self.status()
        sequence = next_sequence(current.acknowledged_sequence)
        command_address = self.manifest.field_address("command_code")
        lease_address = self.manifest.field_address("command_lease_ms")
        sequence_address = self.manifest.field_address("command_sequence")
        self._run(
            "init; "
            f"mww 0x{command_address:08x} 0x{code:08x}; "
            f"mww 0x{lease_address:08x} 0x{lease_ms:08x}; "
            f"mww 0x{sequence_address:08x} 0x{sequence:08x}; shutdown"
        )
        deadline = time.monotonic() + 2.0
        while True:
            observed = self.status()
            if observed.acknowledged_sequence == sequence:
                return observed
            if time.monotonic() >= deadline:
                raise TimeoutError("bench firmware did not acknowledge the command")
            time.sleep(0.01)
