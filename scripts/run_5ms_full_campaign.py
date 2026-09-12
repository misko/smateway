#!/usr/bin/env python3
"""Fresh 5 MS/s B/D campaign using existing pinned, bounded capture and restore paths."""

import argparse
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import run_jitter_reacquisition as prior
from resume_jitter_campaign import preflight

from smateway.rate_timing import PORTS, load, sha256

ROOT = prior.ROOT
OUTPUT = Path("/srv/bulk/samteway/lab-data/tracking-5ms-full-20260911-v1")
PARENT = Path("/srv/bulk/samteway/lab-data/tracking-jitter-20260911-resume-v1")
CENTRES = ((915000000, 50), (2475000000, 40), (5800000000, 60), (5811000000, 60))
BLOCKS = tuple(
    (f, c, g, (200, 1000) if f == 915000000 else (25, 50, 100, 200, 1000))
    for f, g in CENTRES
    for c in ("D", "B")
)


def setup(root, confirmed_at):
    root.mkdir(parents=True, exist_ok=False)
    (root / "analysis").mkdir()
    shutil.copyfile(PARENT / "initial-selector-flash.bin", root / "initial-selector-flash.bin")
    for name in ("fixture-dual-band.json", "fixture-915.json"):
        original = PARENT / name
        fixture = load(original)
        fixture.update(
            {
                "fixture_id": f"5ms-full-{name.removesuffix('.json')}-20260911",
                "confirmed_at": confirmed_at,
                "operator_confirmation": (
                    "User confirmed yes please! to fresh 5 MS/s captures "
                    "on the unchanged fixture and pinned radios."
                ),
                "authorization_description": (
                    "5 MS/s / 4 MHz primary; 5 MS/s / 1.6 MHz comparisons; "
                    "existing A references/controls retained. Same bands, TX power, wiring and "
                    "PCB LUT; no new surveyed geometry."
                ),
                "parent_fixture": {"path": str(original), "sha256": sha256(original)},
            }
        )
        prior.save(root / name, fixture)
    plan = {
        "schema": 1,
        "campaign_id": "full-5ms-20260911-v1",
        "confirmed_at": confirmed_at,
        "source_sha256": sha256(Path(__file__)),
        "blocks": [
            {"frequency_hz": f, "configuration": c, "gain_db": g, "dwells_us": list(d)}
            for f, c, g, d in BLOCKS
        ],
        "expected_switched_records": 150,
        "expected_blocks": 8,
        "dense": {
            "configuration": "D",
            "sample_rate_hz": 5000000,
            "bandwidth_hz": 4000000,
            "frequency_start_hz": 5726000000,
            "frequency_stop_hz": 5874000000,
            "step_hz": 1000000,
            "tx_channels": [0, 1],
            "dwell_us": 200,
            "duration_s": 4,
            "expected_records": 298,
        },
        "scope": (
            "Fresh acquisition, not upsampled IQ. Separate B/D blocks at every centre; "
            "A references/controls preserve the frozen timing recipe. All failures retained."
        ),
        "parent_915_protocol_note": (
            "Existing protocol supplies bounded RF admission; this "
            "campaign adds B/D receiver configurations at the same single 915 MHz centre."
        ),
        "source_contract": {name: sha256(ROOT / name) for name in prior.CONTRACT_FILES},
    }
    prior.save(root / "plan.json", plan)


def command(root, frequency, gain, config, mode, tag, *, port=None, flash=None, tx=0):
    args = prior.capture_args(root, frequency, gain, mode, tag=tag, port=port, flash=flash, tx=tx)
    args[args.index("--configuration") + 1] = config
    return args


def screens(root):
    path = root / "screens.json"
    record = {"status": "running", "captures": [], "expected_captures": 96}
    try:
        for frequency, gain in CENTRES:
            for config in ("D", "B"):
                for mode in ("ambient", "static"):
                    for port in PORTS:
                        print(
                            f"[screen {len(record['captures']) + 1}/96] "
                            f"{frequency / 1e6:g} {config} {mode} {port}",
                            flush=True,
                        )
                        p, run = prior.capture(
                            command(
                                root,
                                frequency,
                                gain,
                                config,
                                mode,
                                f"screen-{frequency}-{config}-{mode}-{port}",
                                port=port,
                            )
                        )
                        record["captures"].append(
                            prior.record_capture(
                                p,
                                run,
                                frequency_hz=frequency,
                                configuration=config,
                                mode=mode,
                                port=port,
                            )
                        )
                        prior.save(path, record)
                        if not prior.headroom_pass(run):
                            raise RuntimeError(f"Headroom/acquisition failure retained: {p}")
        record["status"] = "complete"
    except BaseException as error:
        record.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        prior.save(path, record)


def acquire_blocks(root, record, *, settings=BLOCKS):
    for index, (frequency, config, gain, dwells) in enumerate(settings):
        protocol, fixture, _ = prior.binding(root, frequency)
        log = root / f"block-{index + 1}-{frequency}-{config}.log"
        args = [
            sys.executable,
            str(ROOT / "scripts/run_comprehensive_block.py"),
            "--output-root",
            str(root),
            "--protocol-json",
            str(protocol),
            "--fixture-json",
            str(fixture),
            "--frequency-hz",
            str(frequency),
            "--gain-db",
            str(gain),
            "--configuration",
            config,
            "--dwells-us",
            *map(str, dwells),
            "--timing-recipe",
            str(prior.RECIPE),
            "--acknowledge-ota-authorization",
            "--acknowledge-selector-flash",
        ]
        print(f"[block {index + 1}/8] {frequency / 1e6:g} MHz {config}", flush=True)
        with log.open("x") as stream:
            process = subprocess.Popen(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            try:
                code = process.wait()
            except BaseException:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=90)
                raise
        candidates = [
            line.split("=", 1)[1]
            for line in log.read_text().splitlines()
            if line.startswith("block_evidence=")
        ]
        item = {
            "frequency_hz": frequency,
            "configuration": config,
            "log": str(log),
            "returncode": code,
        }
        if candidates:
            p = Path(candidates[-1])
            item.update(block_json=str(p), sha256=sha256(p), status=load(p)["status"])
        record["blocks"].append(item)
        prior.save(root / "blocks.json", record)
        if code or item.get("status") != "diagnostic-complete":
            raise RuntimeError(f"Block stopped; no silent replacement: {log}")


def dense(root, *, gain_db=60):
    path = root / "dense.json"
    record = {
        "status": "running",
        "captures": [],
        "started_at": datetime.now(UTC).isoformat(),
        "configuration": "D",
        "plan_sha256": sha256(root / "plan.json"),
    }
    flash = None
    try:
        prior.save(path, record)
        flash = prior._flash(root, 200)
        record["flash"] = {"path": str(flash), "sha256": sha256(flash)}
        for frequency in range(5726000000, 5874000001, 1000000):
            for tx in (0, 1):
                print(
                    f"[dense {len(record['captures']) + 1}/298] {frequency / 1e6:g} MHz TX{tx + 1}",
                    flush=True,
                )
                p, run = prior.capture(
                    command(
                        root,
                        frequency,
                        gain_db,
                        "D",
                        "fast",
                        f"dense5ms-{frequency}-tx{tx + 1}",
                        flash=flash,
                        tx=tx,
                    )
                )
                record["captures"].append(
                    prior.record_capture(
                        p, run, frequency_hz=frequency, tx_channel=tx, dwell_us=200
                    )
                )
                prior.save(path, record)
                if not prior.headroom_pass(run):
                    raise RuntimeError(f"Dense acquisition/headroom failure: {p}")
        record["status"] = "complete"
    except BaseException as error:
        record.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        try:
            if flash:
                restored = prior._restore(root, flash)
                record["restore"] = {"path": str(restored), "sha256": sha256(restored)}
        except BaseException as error:
            record.update(status="failed", restore_error=str(error))
            raise
        finally:
            prior.save(path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    parser.add_argument("--confirmed-at", required=True)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization or not args.acknowledge_selector_flash:
        raise SystemExit("Fresh bounded RF and selector acknowledgments required")
    root = args.output_root.resolve()
    setup(root, args.confirmed_at)
    record = {
        "status": "running",
        "blocks": [],
        "started_at": datetime.now(UTC).isoformat(),
        "plan_sha256": sha256(root / "plan.json"),
    }

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupt)
    prior.save(root / "blocks.json", record)
    with prior._lock(root / ".5ms-hardware.lock"):
        try:
            preflight(root)
            screens(root)
            acquire_blocks(root, record)
            record["status"] = "complete"
        except BaseException as error:
            record.update(
                status="failed", error={"type": type(error).__name__, "message": str(error)}
            )
            raise
        finally:
            prior.save(root / "blocks.json", record)
        dense(root)


if __name__ == "__main__":
    main()
