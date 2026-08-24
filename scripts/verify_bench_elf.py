#!/usr/bin/env python3
"""Fail closed on unsafe content in the leased static-selector image."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

FLASH_LIMIT = 16 * 1024
RAM_LIMIT = 6 * 1024
FORBIDDEN_ADDRESS_FRAGMENTS = {
    "400220": "flash controller",
    "40002c": "window watchdog",
    "400030": "independent watchdog",
}


def output(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    args = parser.parse_args()
    elf = args.elf.resolve(strict=True)

    size_text = output("arm-none-eabi-size", str(elf))
    match = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+", size_text, re.MULTILINE)
    if match is None:
        raise SystemExit("BENCH VERIFY FAIL: cannot parse size output")
    text_size, data_size, bss_size = (int(value) for value in match.groups())
    if text_size + data_size > FLASH_LIMIT or data_size + bss_size > RAM_LIMIT:
        raise SystemExit("BENCH VERIFY FAIL: device memory region exceeded")

    undefined = output("arm-none-eabi-nm", "-u", str(elf)).strip()
    if undefined:
        raise SystemExit("BENCH VERIFY FAIL: undefined symbols present")
    disassembly = output("arm-none-eabi-objdump", "-d", str(elf)).lower()
    forbidden = [
        label for fragment, label in FORBIDDEN_ADDRESS_FRAGMENTS.items() if fragment in disassembly
    ]
    if forbidden:
        raise SystemExit("BENCH VERIFY FAIL: forbidden MMIO " + ", ".join(forbidden))
    if re.search(r"\bcpsie\b", disassembly):
        raise SystemExit("BENCH VERIFY FAIL: interrupt enable instruction present")

    symbols = output("arm-none-eabi-nm", "-g", "--defined-only", str(elf))
    if not re.search(r"\bsmateway_bench_mailbox$", symbols, re.MULTILINE):
        raise SystemExit("BENCH VERIFY FAIL: debugger mailbox missing")

    print(
        "BENCH VERIFY PASS: "
        f"flash={text_size + data_size}/{FLASH_LIMIT} "
        f"ram={data_size + bss_size}/{RAM_LIMIT} interrupts=disabled"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
