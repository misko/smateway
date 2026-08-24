#!/usr/bin/env python3
"""Print, but never execute, a masked STM32C0 BOR-level-4 option plan."""

from __future__ import annotations

import argparse
import json

from smateway.options import plan_bor4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed-optr", type=lambda value: int(value, 0), required=True)
    args = parser.parse_args()
    plan = plan_bor4(args.observed_optr)
    print(
        json.dumps(
            {
                "observed_optr": f"0x{plan.observed_optr:08x}",
                "expected_optr": f"0x{plan.expected_optr:08x}",
                "write_mask": f"0x{plan.write_mask:08x}",
                "write_value": f"0x{plan.write_value:08x}",
                "changed_bits": f"0x{plan.changed_bits:08x}",
                "already_configured": plan.already_configured,
                "executed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
