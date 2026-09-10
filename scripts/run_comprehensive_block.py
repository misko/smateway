#!/usr/bin/env python3
"""One bounded TX1 diagnostic block with bracketing references and exact restore."""

from __future__ import annotations

# ruff: noqa: E402, I001
import argparse
import os
import random
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / ".venv"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != PREFIX.resolve()
    or os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)[0] != str(PREFIX / "lib")
    or str(ROOT / "src") not in sys.path
):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(PREFIX / "lib")
    env["PYTHONPATH"] = str(ROOT / "src")
    os.execve(
        str(PREFIX / "bin/python"),
        [str(PREFIX / "bin/python"), str(Path(__file__).resolve()), *sys.argv[1:]],
        env,
    )

from capture_fast_tracking_timing import _write_json_atomic
from capture_rate_timing import record_interrupt
from run_fast_tracking_timing_campaign import _flash, _lock, _restore
from screen_comprehensive_headroom import capture
from smateway.campaign_protocol import admit_capture
from smateway.rate_timing import PORTS, analyze_rate_capture, sha256

PROTOCOL = ROOT / "docs/comprehensive_fast_switching/data/protocol-v1.json"


def schedule(dwells, seed, configuration="A"):
    if (
        not dwells
        or len(set(dwells)) != len(dwells)
        or any(d not in (25, 50, 100, 200, 1000) for d in dwells)
    ):
        raise ValueError("distinct reviewed dwell values required")
    if configuration not in ("A", "B", "D"):
        raise ValueError("only continuity-qualified rates are admitted")
    rng = random.Random(seed)
    rows = []
    for round_number in (1, 2, 3):
        order = [
            {"round": round_number, "dwell_us": d, "control": False, "configuration": configuration}
            for d in dwells
        ]
        order.append(
            {"round": round_number, "dwell_us": 200, "control": True, "configuration": "A"}
        )
        if configuration != "A":
            order.append(
                {"round": round_number, "dwell_us": 1000, "control": True, "configuration": "A"}
            )
        rng.shuffle(order)
        rows.extend(order)
    return rows


def run_switched(rows, *, flash, acquire, restore):
    """Always restore the initial full backup, including failed/interrupt captures."""
    original = active = None
    dwell = None
    try:
        for row in rows:
            if row["dwell_us"] != dwell:
                active = flash(row["dwell_us"])
                original = original or active
                dwell = row["dwell_us"]
            acquire(row, active)
    finally:
        if original is not None:
            restore(original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--fixture-json", type=Path, required=True)
    parser.add_argument("--protocol-json", type=Path, default=PROTOCOL)
    parser.add_argument("--frequency-hz", type=int, required=True)
    parser.add_argument("--gain-db", type=int, choices=range(61), required=True)
    parser.add_argument("--configuration", choices=("A", "B", "D"), default="A")
    parser.add_argument("--dwells-us", type=int, nargs="+", default=[200, 1000])
    parser.add_argument("--timing-recipe", type=Path)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization or not args.acknowledge_selector_flash:
        raise SystemExit("bounded RF and selector programming acknowledgments are required")
    binding = admit_capture(
        args.protocol_json, args.frequency_hz, muted=False, fixture_path=args.fixture_json
    )
    rows = schedule(args.dwells_us, 202609081536 + args.frequency_hz, args.configuration)
    reference_configurations = list(dict.fromkeys(("A", args.configuration)))
    for dwell in {r["dwell_us"] for r in rows}:
        if not (
            ROOT / f"build/STM32C011F4P6/tracking-c6-{dwell}us/pluto_tracking_c6.build.json"
        ).is_file():
            raise SystemExit(f"missing verified build for {dwell}us")
    args.output_root.mkdir(parents=True, exist_ok=True)
    identifier = datetime.now(UTC).strftime("block-%Y%m%dT%H%M%S%fZ")
    path = args.output_root / f"{identifier}.json"
    record = {
        "schema": 1,
        "status": "running",
        "scope": "fresh TX1 phase/gain diagnostic block",
        "frequency_hz": args.frequency_hz,
        "gain_db": args.gain_db,
        "configuration": args.configuration,
        "reference_configurations": reference_configurations,
        "weight_reference_configuration": "A",
        "binding": binding,
        "references": [],
        "captures": [],
        "flashes": [],
        "restores": [],
        "planned_rows": rows,
        "source_sha256": sha256(Path(__file__)),
        "started_at": datetime.now(UTC).isoformat(),
        "surveyed_bearing_accuracy_available": False,
        "interrupt_events": [],
        "timing_replay_recipe": None
        if args.timing_recipe is None
        else {
            "path": str(args.timing_recipe.resolve(strict=True)),
            "sha256": sha256(args.timing_recipe),
            "scope": "additional frozen offline timing policy; RF settings unchanged",
        },
    }

    def save():
        record["updated_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(path, record)

    def take(tag, mode, *, configuration, port=None, row=None, flash=None):
        command = [
            sys.executable,
            str(ROOT / "scripts/capture_rate_timing.py"),
            "--configuration",
            configuration,
            "--mode",
            mode,
            "--duration-s",
            "2" if mode == "static" else "4",
            "--frequency-hz",
            str(args.frequency_hz),
            "--gain-db",
            str(args.gain_db),
            "--protocol-json",
            str(args.protocol_json),
            "--fixture-json",
            str(args.fixture_json),
            "--output-root",
            str(args.output_root),
            "--tag",
            f"{identifier}-{tag}",
            "--acknowledge-ota-authorization",
        ]
        if port:
            command.extend(("--port", port))
        if row:
            command.extend(
                (
                    "--profile",
                    str(ROOT / f"profiles/tracking-c6-{row['dwell_us']}us-v1/control_profile.json"),
                    "--flash-evidence",
                    str(flash),
                )
            )
        print(f"[capture] {tag}", flush=True)
        result_path, run = capture(command)
        result = {
            "run_json": str(result_path),
            "sha256": sha256(result_path),
            "status": run["status"],
            "error": run.get("error"),
        }
        return result

    def references(position):
        configurations = (
            reference_configurations if position == "before" else reversed(reference_configurations)
        )
        for configuration in configurations:
            for port in PORTS if position == "before" else reversed(PORTS):
                result = take(
                    f"ref-{position}-{configuration}-{port}",
                    "static",
                    configuration=configuration,
                    port=port,
                )
                record["references"].append(
                    {"position": position, "port": port, "configuration": configuration, **result}
                )
                save()
                if result["status"] != "passed":
                    raise RuntimeError(
                        "independent reference acquisition failed; no silent replacement"
                    )

    def flash(dwell):
        print(f"[flash] {dwell}us", flush=True)
        result = _flash(args.output_root, dwell)
        record["flashes"].append({"dwell_us": dwell, "path": str(result), "sha256": sha256(result)})
        save()
        return result

    def switched(row, flash_path):
        tag = f"r{row['round']}-{row['configuration']}-{row['dwell_us']}us" + (
            "-control" if row["control"] else ""
        )
        result = take(tag, "fast", configuration=row["configuration"], row=row, flash=flash_path)
        record["captures"].append({**row, **result})
        save()
        if result["status"] != "passed":
            raise RuntimeError("switched acquisition failed; preserve holdout without replacement")

    def restore(original):
        print("[restore] exact original bench image", flush=True)
        result = _restore(args.output_root, original)
        record["restores"].append({"path": str(result), "sha256": sha256(result)})
        save()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            signum, lambda received, _frame: record_interrupt(received, record["interrupt_events"])
        )
    save()
    try:
        with _lock(args.output_root / ".block.lock"):
            references("before")
            run_switched(rows, flash=flash, acquire=switched, restore=restore)
            references("after")
        record["status"] = "acquired"
        save()
        refs = {
            configuration: {
                r["port"]: Path(r["run_json"])
                for r in record["references"]
                if r["position"] == "before" and r["configuration"] == configuration
            }
            for configuration in reference_configurations
        }
        for row in record["captures"]:
            print(f"[analyze] {Path(row['run_json']).parent.name}", flush=True)
            try:
                analysis = analyze_rate_capture(
                    Path(row["run_json"]), refs[row["configuration"]], refs["A"]
                )
            except ValueError as error:
                analysis = {"status": "analysis-failed", "error": str(error), "variants": []}
            analysis_path = Path(row["run_json"]).parent / "independent-analysis.json"
            _write_json_atomic(analysis_path, analysis)
            row["analysis_json"] = str(analysis_path)
            row["analysis_sha256"] = sha256(analysis_path)
            save()
        record["status"] = "diagnostic-complete"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save()
        print(f"block_evidence={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
