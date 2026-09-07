#!/usr/bin/env python3
"""Verify and bind an experimental autonomous C6 timing image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FLASH_BASE = 0x0800_0000
FLASH_LIMIT = 16 * 1024
RAM_LIMIT = 6 * 1024
EXPECTED_PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
EXPECTED_CODES = (0x0, 0x4, 0x6, 0x7, 0x3, 0x1)
EXPECTED_DWELLS_US = (25, 50, 100, 200)
REQUIRED_SYMBOLS = (
    "Reset_Handler",
    "main",
    "high_rate_frame_init",
    "high_rate_frame_advance",
    "high_rate_next_deadline",
    "CONTROL_SCHEDULE",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _profile(path: Path) -> tuple[dict[str, Any], int, bytes]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("profile must be an object")
    identity = value.get("profile")
    frame = value.get("frame")
    safety = value.get("safety")
    states = value.get("states")
    if not all(isinstance(item, dict) for item in (identity, frame, safety)):
        raise ValueError("profile identity/frame/safety is malformed")
    if not isinstance(states, list) or len(states) != 6:
        raise ValueError("profile must contain six states")
    assert isinstance(identity, dict)
    assert isinstance(frame, dict)
    assert isinstance(safety, dict)
    dwell_us = states[0].get("dwell_us") if isinstance(states[0], dict) else None
    if not isinstance(dwell_us, int) or dwell_us not in EXPECTED_DWELLS_US:
        raise ValueError("profile dwell is outside the reviewed campaign grid")
    if (
        identity.get("id") != f"tracking-c6-{dwell_us}us-v1"
        or identity.get("revision") != 1
        or frame.get("order") != list(EXPECTED_PORTS)
        or frame.get("all_off_guard_us") != 20
        or frame.get("nominal_cycle_us") != 180 + 6 * (20 + dwell_us)
        or safety.get("all_off_code") != "1000"
        or safety.get("maximum_deadline_lateness_us") != 5
        or value.get("release_contract", {}).get("conformant") is not False
    ):
        raise ValueError("profile contract is not the reviewed experimental C6 grammar")
    schedule = bytearray()
    for item, name, code in zip(states, EXPECTED_PORTS, EXPECTED_CODES, strict=True):
        if not isinstance(item, dict):
            raise ValueError("profile state is malformed")
        if (
            item.get("name") != name
            or int(str(item.get("gpio_code_pa3_pa0", "")), 2) != code
            or item.get("dwell_us") != dwell_us
        ):
            raise ValueError("profile schedule differs from the physical C6 order")
        schedule.extend(struct.pack("<BxH", code, dwell_us))
    return value, dwell_us, bytes(schedule)


def _symbols(elf: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    pattern = re.compile(
        r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([A-Za-z])\s+(.+)$"
    )
    for line in _run("arm-none-eabi-nm", "-S", "--defined-only", str(elf)).splitlines():
        match = pattern.match(line.strip())
        if match:
            result[match.group(4)] = (
                int(match.group(1), 16),
                int(match.group(2), 16),
                match.group(3),
            )
    return result


def _text_section(elf: Path) -> tuple[int, bytes]:
    header = _run("arm-none-eabi-objdump", "-h", str(elf))
    match = re.search(
        r"^\s*\d+\s+\.text\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)",
        header,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("ELF has no .text section")
    size = int(match.group(1), 16)
    address = int(match.group(2), 16)
    raw = subprocess.run(
        ("arm-none-eabi-objcopy", "-O", "binary", "--only-section=.text", str(elf), "/dev/stdout"),
        check=True,
        capture_output=True,
    ).stdout
    if len(raw) != size:
        raise ValueError("extracted .text size differs from the section table")
    return address, raw


def _size(elf: Path) -> tuple[int, int, int]:
    rows = _run("arm-none-eabi-size", str(elf)).splitlines()
    if len(rows) < 2:
        raise ValueError("ELF size output is malformed")
    fields = rows[-1].split()
    text_size, data_size, bss_size = (int(fields[index]) for index in range(3))
    if text_size + data_size > FLASH_LIMIT or data_size + bss_size > RAM_LIMIT:
        raise ValueError("firmware exceeds the STM32C011F4P6 memory limits")
    return text_size, data_size, bss_size


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main() -> int:
    args = _parser().parse_args()
    for path in (args.elf, args.profile, args.binary):
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"input is absent, non-regular, or a symlink: {path}")
    try:
        profile, dwell_us, expected_schedule = _profile(args.profile)
        symbols = _symbols(args.elf)
        missing = [name for name in REQUIRED_SYMBOLS if name not in symbols]
        if missing:
            raise ValueError("firmware is missing symbols: " + ", ".join(missing))
        schedule_address, schedule_size, schedule_kind = symbols["CONTROL_SCHEDULE"]
        if schedule_size != len(expected_schedule) or schedule_kind.lower() not in {"r", "t"}:
            raise ValueError("CONTROL_SCHEDULE symbol size/type differs")
        text_address, text = _text_section(args.elf)
        offset = schedule_address - text_address
        if offset < 0 or text[offset : offset + schedule_size] != expected_schedule:
            raise ValueError("compiled CONTROL_SCHEDULE bytes differ from the profile")
        undefined = _run("arm-none-eabi-nm", "-u", str(args.elf)).strip()
        if undefined:
            raise ValueError("firmware has undefined symbols")
        disassembly = _run("arm-none-eabi-objdump", "-d", str(args.elf)).lower()
        if "cpsie" in disassembly or "\tsvc" in disassembly:
            raise ValueError("firmware enables interrupts or issues a supervisor call")
        if "cpsid\ti" not in disassembly and "cpsid i" not in disassembly:
            raise ValueError("firmware does not explicitly disable interrupts")
        text_size, data_size, bss_size = _size(args.elf)
        binary = args.binary.read_bytes()
        if not binary or len(binary) > FLASH_LIMIT:
            raise ValueError("flash binary length is invalid")
        if symbols["Reset_Handler"][0] < FLASH_BASE:
            raise ValueError("reset handler is outside flash")
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise SystemExit(f"TRACKING C6 VERIFY FAIL: {error}") from error

    sources = (
        "firmware/stm32c011/apps/hexcal/main.c",
        "firmware/stm32c011/apps/hexcal/startup_stm32c011xx.S",
        "firmware/stm32c011/core/high_rate_autonomous_core.c",
        "firmware/stm32c011/core/high_rate_autonomous_core.h",
        "firmware/stm32c011/apps/safe_hold/safe_runtime.c",
        "firmware/stm32c011/linker/stm32c011f4p6.ld",
    )
    record = {
        "schema": 1,
        "evidence_kind": "experimental_tracking_c6_build_verification",
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "passed",
        "scope": (
            "profile/schedule/memory/symbol/static-safety verification; not the released "
            "exact-ELF or 5 ms transition-guard qualification"
        ),
        "dwell_us": dwell_us,
        "guard_us": 20,
        "cycle_us": profile["frame"]["nominal_cycle_us"],
        "ports": list(EXPECTED_PORTS),
        "profile": {"path": str(args.profile.resolve()), "sha256": _sha256(args.profile)},
        "elf": {"path": str(args.elf.resolve()), "sha256": _sha256(args.elf)},
        "binary": {
            "path": str(args.binary.resolve()),
            "sha256": _sha256(args.binary),
            "size_bytes": len(binary),
        },
        "memory": {
            "text_bytes": text_size,
            "data_bytes": data_size,
            "bss_bytes": bss_size,
            "flash_limit_bytes": FLASH_LIMIT,
            "ram_limit_bytes": RAM_LIMIT,
        },
        "control_schedule": {
            "address": schedule_address,
            "size_bytes": schedule_size,
            "bytes_hex": expected_schedule.hex(),
        },
        "source_sha256": {
            relative: _sha256(ROOT / relative) for relative in sources
        },
    }
    output = args.output or args.elf.with_name("pluto_tracking_c6.build.json")
    _write_json_atomic(output, record)
    print(
        "TRACKING C6 VERIFY PASS: "
        f"dwell={dwell_us}us cycle={record['cycle_us']}us "
        f"flash={text_size + data_size}/{FLASH_LIMIT} "
        f"ram={data_size + bss_size}/{RAM_LIMIT} manifest={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
