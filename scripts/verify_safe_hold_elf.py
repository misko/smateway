#!/usr/bin/env python3
"""Fail closed on unexpected memory or symbol content in pluto_safe_hold."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

FLASH_LIMIT = 16 * 1024
RAM_LIMIT = 6 * 1024
FORBIDDEN_APPLICATION_SYMBOLS = (
    "HAL_IWDG_Init",
    "HAL_WWDG_Init",
    "HAL_FLASH_OB_Launch",
    "HAL_FLASHEx_OBProgram",
    "NVIC_EnableIRQ",
    "SysTick_Config",
)


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
        raise SystemExit("SAFE-HOLD VERIFY FAIL: cannot parse size output")
    text_size, data_size, bss_size = (int(value) for value in match.groups())
    if text_size + data_size > FLASH_LIMIT:
        raise SystemExit("SAFE-HOLD VERIFY FAIL: flash region exceeded")
    if data_size + bss_size > RAM_LIMIT:
        raise SystemExit("SAFE-HOLD VERIFY FAIL: SRAM region exceeded")

    symbols = output("arm-none-eabi-nm", "-a", str(elf))
    required = ("Reset_Handler", "SystemInit", "main")
    missing = [name for name in required if not re.search(rf"\b{name}$", symbols, re.MULTILINE)]
    if missing:
        raise SystemExit("SAFE-HOLD VERIFY FAIL: missing symbols " + ", ".join(missing))
    forbidden = [
        name
        for name in FORBIDDEN_APPLICATION_SYMBOLS
        if re.search(rf"\b{re.escape(name)}$", symbols, re.MULTILINE)
    ]
    if forbidden:
        raise SystemExit("SAFE-HOLD VERIFY FAIL: forbidden symbols " + ", ".join(forbidden))

    main_disassembly = output("arm-none-eabi-objdump", "-d", "--disassemble=main", str(elf))
    if re.search(r"\bblx?(?:\.n)?\s+", main_disassembly):
        raise SystemExit("SAFE-HOLD VERIFY FAIL: main contains an external call")

    print(
        "SAFE-HOLD VERIFY PASS: "
        f"flash={text_size + data_size}/{FLASH_LIMIT} "
        f"ram={data_size + bss_size}/{RAM_LIMIT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
