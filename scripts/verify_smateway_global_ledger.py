#!/usr/bin/env python3
"""Read-only verification of the shared privileged Smateway run ledger."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY / "src"
sys.path[:] = [entry for entry in sys.path if entry != str(_SOURCE_ROOT)]
sys.path.insert(0, str(_SOURCE_ROOT))

from smateway import global_ledger


def main() -> int:
    try:
        storage = global_ledger.attest_fixed_storage(require_runner_identity=True)
    except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema": 1,
                "status": "verified",
                "api": global_ledger.API,
                "runner": storage["runner"],
                "global_root": storage["global_root"],
                "seal": storage["global_root_seal"],
                "helper": storage["privileged_helper"],
                "sudo_binary": storage["sudo_binary"],
                "sudoers": storage["sudoers_policy"],
                "policies": sorted(global_ledger.POLICIES),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
