#!/usr/bin/env python3
"""Fail closed on unexpected high-rate calibration-image content."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

FLASH_LIMIT = 16 * 1024
RAM_LIMIT = 6 * 1024


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
        raise SystemExit("HEXCAL VERIFY FAIL: cannot parse size output")
    text_size, data_size, bss_size = (int(value) for value in match.groups())
    if text_size + data_size > FLASH_LIMIT or data_size + bss_size > RAM_LIMIT:
        raise SystemExit("HEXCAL VERIFY FAIL: device memory region exceeded")

    if output("arm-none-eabi-nm", "-u", str(elf)).strip():
        raise SystemExit("HEXCAL VERIFY FAIL: undefined symbols present")
    symbols = output("arm-none-eabi-nm", "-a", str(elf))
    required_symbols = (
        "SystemCoreClockUpdate",
        "high_rate_deadline_action",
        "high_rate_frame_advance",
        "main",
    )
    missing = [
        name
        for name in required_symbols
        if not re.search(rf"\b{re.escape(name)}$", symbols, re.MULTILINE)
    ]
    if missing:
        raise SystemExit("HEXCAL VERIFY FAIL: missing symbols " + ", ".join(missing))

    disassembly = output("arm-none-eabi-objdump", "-d", str(elf)).lower()
    if "400220" in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: image accesses flash/option registers")
    if "40002c" in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: unexpected window-watchdog access")
    if "400030" not in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: independent-watchdog access missing")
    if "400004" not in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: TIM3 access missing")
    if "e000e010" in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: unexpected SysTick access")
    if not re.search(r"\bcpsid\s+i\b", disassembly):
        raise SystemExit("HEXCAL VERIFY FAIL: explicit interrupt disable missing")
    if re.search(r"\bcpsie\b", disassembly):
        raise SystemExit("HEXCAL VERIFY FAIL: interrupt enable instruction present")

    print(
        "HEXCAL VERIFY PASS: "
        f"flash={text_size + data_size}/{FLASH_LIMIT} "
        f"ram={data_size + bss_size}/{RAM_LIMIT} "
        "TIM3=present IWDG=present "
        "maskable_interrupts=explicitly-disabled-and-never-reenabled "
        "flash_option_base_literal=absent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
