#!/usr/bin/env python3
"""One bounded, source-muted/static 915 MHz screen on the existing six antennas."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from run_fast_tracking_timing_campaign import _lock
from screen_comprehensive_headroom import capture, headroom_pass, save

from smateway.campaign_protocol import admit_capture
from smateway.rate_timing import PORTS, sha256

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs/subghz_915_diagnostic/data/protocol-v1.json"
FIXTURE = ROOT / "docs/subghz_915_diagnostic/data/fixture-v1.json"


def static_usable(run):
    reference = run.get("reference", {})
    return (
        headroom_pass(run)
        and reference.get("coherence", 0) >= 0.9
        and reference.get("phase_rms_10ms_deg", float("inf")) <= 5
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--gain-db", type=int, choices=(30, 40, 50), default=30)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization:
        raise SystemExit("bounded RF acknowledgment required")
    binding = admit_capture(PROTOCOL, 915_000_000, muted=False, fixture_path=FIXTURE)
    args.output_root.mkdir(parents=True, exist_ok=True)
    path = args.output_root / datetime.now(UTC).strftime("screen-%Y%m%dT%H%M%S%fZ.json")
    record = {
        "schema": 1, "status": "running", "binding": binding,
        "frequency_hz": 915_000_000, "gain_db": args.gain_db, "configuration": "A",
        "captures": [], "source_sha256": sha256(Path(__file__)),
    }
    save(path, record)
    try:
        with _lock(args.output_root / ".block.lock"):
            for mode in ("ambient", "static"):
                for port in PORTS:
                    print(
                        f"[capture] {mode} {port} 915 MHz, RX gain {args.gain_db} dB", flush=True,
                    )
                    run_path, run = capture([
                        sys.executable, str(ROOT / "scripts/capture_rate_timing.py"),
                        "--configuration", "A", "--mode", mode, "--port", port,
                        "--frequency-hz", "915000000", "--duration-s", "2",
                        "--gain-db", str(args.gain_db), "--protocol-json", str(PROTOCOL),
                        "--fixture-json", str(FIXTURE), "--output-root", str(args.output_root),
                        "--tag", f"screen-g{args.gain_db}-{mode}-{port}",
                        "--acknowledge-ota-authorization",
                    ])
                    row = {
                        "mode": mode, "port": port, "run_json": str(run_path),
                        "sha256": sha256(run_path), "status": run["status"],
                        "headroom_pass": headroom_pass(run),
                        "static_usable": static_usable(run) if mode == "static" else None,
                        "reference": run.get("reference"),
                    }
                    record["captures"].append(row)
                    save(path, record)
                    peaks = run.get("capture", {}).get("peak_component_counts")
                    print(f"  {run['status']}, peaks={peaks}", flush=True)
                    if mode == "static":
                        ref = run.get("reference", {})
                        print(
                            f"  coherence={ref.get('coherence')}, "
                            f"phase_rms_10ms_deg={ref.get('phase_rms_10ms_deg')}", flush=True,
                        )
                    if not headroom_pass(run):
                        raise RuntimeError(f"acquisition/headroom gate failed: {run_path}")
        record["usable_static_ports"] = [
            r["port"] for r in record["captures"] if r["static_usable"]
        ]
        record["switching_screen_admitted"] = len(record["usable_static_ports"]) >= 4
        record["status"] = "complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save(path, record)
        print(f"screen_json={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
