#!/usr/bin/env python3
"""Read-only raw-IQ integrity audit for an existing bounded campaign."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from smateway.rate_timing import load, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for index, path in enumerate(sorted((args.campaign_root / "captures").glob("*/run.json"))):
        run = load(path)
        if index % 30 == 0:
            print(f"[audit] capture {index + 1}: {path.parent.name}", flush=True)
        raw_rows = []
        for raw in run.get("capture", {}).get("raw", []):
            raw_path = Path(raw["path"])
            actual = sha256(raw_path)
            expected_bytes = run["capture"]["samples_per_channel"] * 8
            raw_rows.append(
                {
                    "path": str(raw_path),
                    "sha256": actual,
                    "bytes": raw_path.stat().st_size,
                    "passed": actual == raw["sha256"] and raw_path.stat().st_size == expected_bytes,
                }
            )
        rows.append(
            {
                "run_json": str(path),
                "sha256": sha256(path),
                "status": run["status"],
                "mode": run["configuration"]["mode"],
                "raw": raw_rows,
                "final_source_mute": run.get("safety", {})
                .get("final_source_mute", {})
                .get("passed"),
            }
        )
    result = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "campaign_root": str(args.campaign_root),
        "source_sha256": sha256(Path(__file__)),
        "scope": "Every finalized capture manifest and its registered raw IQ; no RF writes. "
        "Failed or unsaved partial acquisitions remain failed; this is not algorithm replay.",
        "passed": all(raw["passed"] for r in rows for raw in r["raw"]),
        "raw_files": sum(len(r["raw"]) for r in rows),
        "raw_bytes": sum(raw["bytes"] for r in rows for raw in r["raw"]),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    if not result["passed"]:
        raise SystemExit("raw integrity audit failed")
    print(f"audit_evidence={args.output}")


if __name__ == "__main__":
    main()
