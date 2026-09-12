#!/usr/bin/env python3
"""Resume the exact unfinished 5 MS/s scope with provenance and fresh gain references."""

import argparse
import os
import shutil
import signal
from pathlib import Path

import run_5ms_full_campaign as campaign
from resume_jitter_campaign import preflight

from smateway.rate_timing import PORTS, load, sha256

PARENT = campaign.OUTPUT
OUTPUT = Path("/srv/bulk/samteway/lab-data/tracking-5ms-full-20260911-resume-v1")


def initialize(parent, root):
    previous = load(parent / "blocks.json")
    if previous["status"] != "failed" or (parent / "dense.json").exists():
        raise ValueError("Requires a failed block campaign with no dense attempts")
    good = [r for r in previous["blocks"] if r.get("status") == "diagnostic-complete"]
    required = {(f, c) for f, c, _g, _d in campaign.BLOCKS[:4]}
    expected = {(f, c) for f, c, _g, _d in campaign.BLOCKS}
    actual = {(r["frequency_hz"], r["configuration"]) for r in good}
    if not required <= actual <= expected or len(actual) != len(good):
        raise ValueError("Completed blocks must be unique planned conditions including low bands")
    for r in good:
        if sha256(Path(r["block_json"])) != r["sha256"]:
            raise ValueError("Inherited block hash differs")
    if load(parent / "screens.json")["status"] != "complete":
        raise ValueError("Original completed screen required")
    root.mkdir(parents=True, exist_ok=False)
    (root / "analysis").mkdir()
    for name in (
        "fixture-dual-band.json",
        "fixture-915.json",
        "initial-selector-flash.bin",
        "screens.json",
    ):
        shutil.copyfile(parent / name, root / name)
    for frequency, _gain in campaign.CENTRES:
        campaign.prior.binding(root, frequency)  # Original user readiness must still be fresh.
    plan = load(parent / "plan.json")
    previous_continuation = plan.get("continuation")
    for b in plan["blocks"]:
        if b["frequency_hz"] >= 5000000000:
            b["gain_db"] = 50
    plan["dense"]["receiver_gain_db"] = 50
    plan["continuation"] = {
        "parent": str(parent),
        "parent_blocks_sha256": sha256(parent / "blocks.json"),
        "parent_plan_sha256": sha256(parent / "plan.json"),
        "inherited_complete_blocks": good,
        "retained_incomplete_blocks": (
            previous_continuation.get("retained_incomplete_blocks", [])
            if previous_continuation
            else []
        )
        + [r for r in previous["blocks"] if r not in good],
        "receiver_gain_change_db": -10,
        "reason": (
            "Previous peak 1878 exceeded 1600-count headroom; "
            "fresh g50 high-band brackets required."
        ),
        "supervisor_timeout_s": 90,
        "timeout_reason": (
            "Four-second recording took 23.92 s to transfer; 45 s process limit "
            "interrupted supervisor. No frame/gate thresholds relaxed."
        ),
        "static_figures_scope": "Original screens inherited; fresh g50 recovery screens separate.",
    }
    if os.environ.get("SMATEWAY_STAGE_IQ_IN_RAM") == "1":
        plan["continuation"]["host_iq_staging"] = {
            "enabled": True,
            "maximum_bytes_per_capture": 512 * 1024 * 1024,
            "reason": (
                "Metadata refill returned ENODATA after 15/80 frames. "
                "Transport diagnostics passed afterward; root cause remains unproven. "
                "Remove live-loop disk writes as a possible source of backpressure; "
                "mute before persistence, retain partial IQ, keep all continuity gates."
            ),
        }
    if previous_continuation:
        plan["continuation"]["previous_continuation"] = previous_continuation
    retries = int(os.environ.get("SMATEWAY_METADATA_RETRIES", "0"))
    if retries:
        if retries not in (1, 2):
            raise ValueError("Metadata retry limit must be 1 or 2")
        plan["continuation"]["transport_retry_policy"] = {
            "maximum_attempts_per_scheduled_capture": retries + 1,
            "retry_only": "OSError errno 61 from metadata refill after verified cleanup",
            "scope": "New whole contiguous records; never splice or accept missing samples",
            "not_retried": (
                "Phase/gain/bearing failures, clipping, timeouts, cleanup or other errors"
            ),
            "evidence": "Every attempt remains hash-linked; all original failed blocks retained",
            "reason": "ENODATA recurred with RAM staging after 39/40 frames in a static reference",
        }
    plan["continuation_source_contract"] = {
        str(p.relative_to(campaign.ROOT)): sha256(p)
        for p in (
            Path(__file__),
            campaign.ROOT / "scripts/run_5ms_full_campaign.py",
            campaign.ROOT / "scripts/screen_comprehensive_headroom.py",
            campaign.ROOT / "scripts/resume_jitter_campaign.py",
            campaign.ROOT / "scripts/capture_rate_timing.py",
            campaign.ROOT / "scripts/run_comprehensive_block.py",
            campaign.ROOT / "scripts/run_jitter_reacquisition.py",
        )
    }
    campaign.prior.save(root / "plan.json", plan)
    return good


def screen_remaining(root):
    path = root / "remaining-headroom.json"
    record = {"status": "running", "captures": [], "gain_db": 50, "duration_s": 4}
    try:
        for frequency in (5800000000, 5811000000):
            for config in ("D", "B"):
                for mode in ("ambient", "static"):
                    for port in PORTS:
                        args = campaign.command(
                            root,
                            frequency,
                            50,
                            config,
                            mode,
                            f"remaining-{frequency}-{config}-{mode}-{port}",
                            port=port,
                        )
                        args[args.index("--duration-s") + 1] = "4"
                        print(
                            f"[g50 screen {len(record['captures']) + 1}/48] "
                            f"{frequency} {config} {mode} {port}",
                            flush=True,
                        )
                        p, run = campaign.prior.capture(args)
                        record["captures"].append(
                            campaign.prior.record_capture(
                                p,
                                run,
                                frequency_hz=frequency,
                                configuration=config,
                                mode=mode,
                                port=port,
                            )
                        )
                        campaign.prior.save(path, record)
                        if not campaign.prior.headroom_pass(run):
                            raise RuntimeError(f"Remaining headroom/continuity failed: {p}")
        record["status"] = "complete"
    except BaseException as error:
        record.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        campaign.prior.save(path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=PARENT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_ota_authorization or not args.acknowledge_selector_flash:
        raise SystemExit("Explicit bounded campaign completion acknowledgments required")
    os.environ["SMATEWAY_CAPTURE_TIMEOUT_S"] = "90"
    root = args.output_root.resolve()
    completed = initialize(args.parent.resolve(strict=True), root)
    record = {"status": "running", "blocks": completed, "plan_sha256": sha256(root / "plan.json")}
    path = root / "blocks.json"
    campaign.prior.save(path, record)

    def interrupt(_sig, _frame):
        raise KeyboardInterrupt()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupt)
    with campaign.prior._lock(root / ".5ms-hardware.lock"):
        try:
            preflight(root, gain_db=50)
            screen_remaining(root)
            inherited = {(r["frequency_hz"], r["configuration"]) for r in completed}
            remaining = tuple(
                (f, c, 50, d) for f, c, _g, d in campaign.BLOCKS if (f, c) not in inherited
            )
            campaign.acquire_blocks(root, record, settings=remaining)
            record["status"] = "complete"
        except BaseException as error:
            record.update(
                status="failed", error={"type": type(error).__name__, "message": str(error)}
            )
            raise
        finally:
            campaign.prior.save(path, record)
        campaign.dense(root, gain_db=50)


if __name__ == "__main__":
    main()
