#!/usr/bin/env python3
"""Generate and validate the debugger mailbox manifest from a bench ELF."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

RAM_START = 0x20000000
RAM_END = RAM_START + (6 * 1024)
DEFINE = re.compile(
    r"^#define (BENCH_[A-Z0-9_]+) UINT32_C\((0x[0-9A-Fa-f]+|[0-9]+)\)$",
    re.MULTILINE,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    elf = args.elf.resolve(strict=True)
    protocol = args.protocol.resolve(strict=True)
    symbols = subprocess.run(
        ["arm-none-eabi-nm", "-g", "--defined-only", str(elf)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    symbol_match = re.search(
        r"^([0-9A-Fa-f]+)\s+[BbDd]\s+smateway_bench_mailbox$", symbols, re.MULTILINE
    )
    if symbol_match is None:
        raise SystemExit("BENCH MANIFEST FAIL: mailbox symbol missing")
    address = int(symbol_match.group(1), 16)

    constants = {
        name: int(raw_value, 0)
        for name, raw_value in DEFINE.findall(protocol.read_text(encoding="utf-8"))
    }
    required = {
        "BENCH_MAILBOX_MAGIC",
        "BENCH_MAILBOX_VERSION",
        "BENCH_MAX_LEASE_MS",
        "BENCH_MAILBOX_SIZE",
        "BENCH_OFFSET_MAGIC",
        "BENCH_OFFSET_VERSION",
        "BENCH_OFFSET_COMMAND_SEQUENCE",
        "BENCH_OFFSET_COMMAND_CODE",
        "BENCH_OFFSET_COMMAND_LEASE_MS",
        "BENCH_OFFSET_ACKNOWLEDGED_SEQUENCE",
        "BENCH_OFFSET_APPLIED_CODE",
        "BENCH_OFFSET_REMAINING_LEASE_MS",
        "BENCH_OFFSET_STATUS_FLAGS",
    }
    missing = required - constants.keys()
    if missing:
        raise SystemExit("BENCH MANIFEST FAIL: missing constants " + ", ".join(sorted(missing)))
    size = constants["BENCH_MAILBOX_SIZE"]
    if address < RAM_START or address + size > RAM_END:
        raise SystemExit("BENCH MANIFEST FAIL: mailbox outside reviewed SRAM")

    offsets = {
        name.removeprefix("BENCH_OFFSET_").lower(): constants[name]
        for name in sorted(required)
        if name.startswith("BENCH_OFFSET_")
    }
    manifest = {
        "schema": 1,
        "elf_sha256": sha256(elf),
        "protocol_sha256": sha256(protocol),
        "mailbox": {
            "address": address,
            "size": size,
            "magic": constants["BENCH_MAILBOX_MAGIC"],
            "version": constants["BENCH_MAILBOX_VERSION"],
            "max_lease_ms": constants["BENCH_MAX_LEASE_MS"],
            "offsets": offsets,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"BENCH MANIFEST PASS: mailbox=0x{address:08x} size={size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
