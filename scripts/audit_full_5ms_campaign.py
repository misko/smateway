#!/usr/bin/env python3
"""Independent offline completion audit; count exact trials, settings and raw bytes."""

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from analyze_jitter_comparison import save, verified_run
from run_comprehensive_block import schedule
from screen_comprehensive_headroom import retryable_metadata_error

from smateway.rate_timing import CONFIGURATIONS, PORTS, RECEIVER_SERIAL, SOURCE_SERIAL, load, sha256


def snapshot(path):
    digest = sha256(path)
    value = load(path)
    if sha256(path) != digest:
        raise ValueError("Manifest changed during snapshot; retry offline audit")
    return value, digest


def transport_attempts(item):
    binding = item.get("transport_attempts")
    if binding is None:
        return 1
    evidence, digest = snapshot(Path(binding["path"]))
    if digest != binding["sha256"]:
        raise ValueError("Transport attempt evidence hash differs")
    attempts = evidence["attempts"]
    if (
        not 1 <= len(attempts) <= evidence["maximum_attempts"] <= 3
        or evidence["selected_run_json"] != item["run_json"]
        or attempts[-1]["run_json"] != item["run_json"]
        or evidence["status"] != "passed"
    ):
        raise ValueError("Transport attempt selection/bound differs")
    if len({Path(r["run_json"]).resolve() for r in attempts}) != len(attempts):
        raise ValueError("A transport attempt was reused")
    for index, attempt in enumerate(attempts):
        path = Path(attempt["run_json"])
        if sha256(path) != attempt["sha256"]:
            raise ValueError("Transport attempt run hash differs")
        run = load(path)
        if index < len(attempts) - 1 and not retryable_metadata_error(run):
            raise ValueError("A non-transport failure was retried")
        mute = run["safety"]["final_source_mute"]
        if (
            not mute["passed"]
            or mute["dds_scales"] != [0.0] * 8
            or mute["tx_gain_db"] != [-80.0, -80.0]
            or any(k.endswith("cleanup_error") or k == "mute_error" for k in run["safety"])
        ):
            raise ValueError("Transport attempt cleanup differs")
        for raw in run.get("partial_raw", []):
            if sha256(Path(raw["path"])) != raw["sha256"]:
                raise ValueError("Failed-attempt partial IQ differs")
    return len(attempts)


def check_run(run, *, frequency, configuration, duration, gain, mode, tx=0):
    identities = run["identities"]
    if (
        identities["receiver_serial"] != RECEIVER_SERIAL
        or identities["source_serial"] != SOURCE_SERIAL
        or run["source_identity"]["hw_serial"] != SOURCE_SERIAL
    ):
        raise ValueError("Pinned radio identity differs")
    cfg, data, actual = run["configuration"], run["capture"], run["receiver_settings"]
    expected = CONFIGURATIONS[configuration]
    if (
        cfg["frequency_hz"] != frequency
        or cfg["name"] != configuration
        or cfg["sample_rate_hz"] != expected.sample_rate_hz
        or cfg["bandwidth_hz"] != expected.bandwidth_hz
        or cfg["receiver_gain_db"] != gain
        or cfg["mode"] != mode
        or cfg["tx_channel"] != tx
        or cfg["duration_s"] != duration
    ):
        raise ValueError("Capture configuration differs from planned role")
    if (
        actual["sample_rate_hz"] != expected.sample_rate_hz
        or actual["bandwidth_hz"] != expected.bandwidth_hz
        or abs(actual["center_frequency_hz"] - frequency) > 5
        or actual["gain_db"] != gain
        or actual["channels"] != [0, 1]
        or actual["gain_mode"] != "manual"
    ):
        raise ValueError("Actual receiver readback differs")
    count = expected.sample_rate_hz * duration
    if data["samples_per_channel"] != count or data["clipped_samples"] != [0, 0]:
        raise ValueError("Sample count or ADC clipping failure")
    timeline = data["timeline"]
    frame = cfg["frame_samples"]
    if len(timeline) != cfg["frames"] or len(timeline) * frame != count:
        raise ValueError("Incomplete frame timeline")
    for i, item in enumerate(timeline):
        if (
            item["buffer_sequence"] != i
            or item["missing_samples_before"]
            or item["overflow_observed"]
            or item["clipped_samples"] != [0, 0]
            or item["last_sample_sequence_exclusive"] - item["first_sample_sequence"] != frame
        ):
            raise ValueError("Frame discontinuity/loss/clipping")
        if i and (
            item["stream_id"] != timeline[i - 1]["stream_id"]
            or item["first_sample_sequence"] != timeline[i - 1]["last_sample_sequence_exclusive"]
        ):
            raise ValueError("Sample/stream continuity differs")
    if len(data["raw"]) != 2 or len({r["path"] for r in data["raw"]}) != 2:
        raise ValueError("Two distinct IQ channels required")
    mute = run["safety"]["final_source_mute"]
    if (
        not mute["passed"]
        or mute["tx_gain_db"] != [-80.0, -80.0]
        or mute["dds_scales"] != [0.0] * 8
        or any(k.endswith("cleanup_error") or k == "mute_error" for k in run["safety"])
    ):
        raise ValueError("Capture cleanup was not verified")
    source = run["source_settings"]
    if (
        source["tx_channel"] != tx
        or any(g > -35 for g in source["tx_gain_db"])
        or max(source["dds_scale"]) > 0.25
        or abs(source["tx_lo_readback_hz"] - frequency) > 5
    ):
        raise ValueError("Source power/frequency differs from bounded settings")
    return {
        "samples_per_channel": count,
        "frames": len(timeline),
        "raw_bytes": sum(Path(r["path"]).stat().st_size for r in data["raw"]),
        "peak_counts": data["peak_component_counts"],
        "conservative_headroom_pass": max(data["peak_component_counts"]) < 1600,
        "transfer_wall_s": data["wall_time_s"],
    }


def block_grid(block, spec):
    if (
        block["status"] != "diagnostic-complete"
        or block["frequency_hz"] != spec["frequency_hz"]
        or block["configuration"] != spec["configuration"]
        or block["gain_db"] != spec["gain_db"]
    ):
        raise ValueError("Block status or configured identity differs")
    expected = schedule(
        spec["dwells_us"], 202609081536 + spec["frequency_hz"], spec["configuration"]
    )
    fields = ("round", "configuration", "dwell_us", "control")

    def signature(row):
        return tuple(row[key] for key in fields)

    if Counter(map(signature, block["captures"])) != Counter(map(signature, expected)):
        raise ValueError("Planned main/control grid differs")
    wanted_refs = {
        (position, cfg, port)
        for position in ("before", "after")
        for cfg in ("A", spec["configuration"])
        for port in PORTS
    }
    actual_refs = [(r["position"], r["configuration"], r["port"]) for r in block["references"]]
    if len(actual_refs) != len(wanted_refs) or set(actual_refs) != wanted_refs:
        raise ValueError("Reference bracket grid differs")


def retained_attempts(plan):
    """Index interrupted attempts separately, including completed but unindexed runs."""
    continuation = plan.get("continuation")
    if not continuation:
        return []
    parent = Path(continuation["parent"])
    for filename, key in (
        ("plan.json", "parent_plan_sha256"),
        ("blocks.json", "parent_blocks_sha256"),
    ):
        if sha256(parent / filename) != continuation[key]:
            raise ValueError("Retained parent manifest changed")
    attempts = []
    for item in continuation["retained_incomplete_blocks"]:
        path = Path(item["block_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("Retained interrupted block changed")
        block = load(path)
        indexed = {
            Path(r["run_json"]).resolve(): r["sha256"]
            for r in block["captures"] + block["references"]
            if r.get("run_json")
        }
        runs = []
        for run_path in sorted((path.parent / "captures").glob(f"{path.stem}-*/run.json")):
            digest = sha256(run_path)
            expected = indexed.get(run_path.resolve())
            if expected is not None and expected != digest:
                raise ValueError("Retained interrupted run changed")
            run = load(run_path)
            # These records are never substituted for qualified full-block trials.
            declared_raw = run.get("capture", {}).get("raw", []) + run.get("partial_raw", [])
            for raw in declared_raw:
                if sha256(Path(raw["path"])) != raw["sha256"]:
                    raise ValueError("Retained interrupted IQ changed")
            runs.append(
                {
                    "run_json": str(run_path),
                    "sha256": digest,
                    "indexed_in_block": expected is not None,
                    "status": run["status"],
                    "mode": run["configuration"]["mode"],
                    "partial_frames": len(run.get("partial_timeline", [])),
                    "raw_evidence": [
                        {"path": str(p), "sha256": sha256(p), "bytes": p.stat().st_size}
                        for p in sorted(run_path.parent.glob("rx*.cf32"))
                    ],
                }
            )
        if not indexed.keys() <= {Path(r["run_json"]).resolve() for r in runs}:
            raise ValueError("Retained indexed run missing")
        attempts.append(
            {
                "block_json": str(path),
                "sha256": item["sha256"],
                "status": block["status"],
                "runs": runs,
            }
        )
    return attempts


def audit(root, *, allow_partial=False):
    plan, plan_hash = snapshot(root / "plan.json")
    manifest, blocks_hash = snapshot(root / "blocks.json")
    specs = {(r["frequency_hz"], r["configuration"]): r for r in plan["blocks"]}
    expected_blocks = {
        (f, c) for f in (915000000, 2475000000, 5800000000, 5811000000) for c in ("B", "D")
    }
    if set(specs) != expected_blocks or len(plan["blocks"]) != 8:
        raise ValueError("Campaign scope must include all eight planned blocks")
    for (frequency, _configuration), spec in specs.items():
        expected_dwells = [200, 1000] if frequency == 915000000 else [25, 50, 100, 200, 1000]
        if sorted(spec["dwells_us"]) != expected_dwells:
            raise ValueError("Planned dwell coverage differs")
    records, seen_blocks, seen_runs = [], set(), set()

    def inspect(item, **kwargs):
        identity = Path(item["run_json"]).resolve()
        if identity in seen_runs:
            raise ValueError("A capture has been reused as an independent trial/reference")
        run = verified_run(item)
        if (
            plan.get("continuation", {}).get("transport_retry_policy")
            and identity.is_relative_to(root.resolve())
            and "transport_attempts" not in item
        ):
            raise ValueError("New capture omits its required attempt ledger")
        row = {
            "run_json": item["run_json"],
            "sha256": item["sha256"],
            **kwargs,
            **check_run(run, **kwargs),
            "transport_attempt_count": transport_attempts(item),
        }
        seen_runs.add(identity)
        records.append(row)
        return run

    for item in manifest["blocks"]:
        path = Path(item["block_json"])
        if sha256(path) != item["sha256"]:
            raise ValueError("Block hash differs")
        block = load(path)
        key = (block["frequency_hz"], block["configuration"])
        if key in seen_blocks or key not in specs:
            raise ValueError("Duplicate or unplanned block")
        block_grid(block, specs[key])
        seen_blocks.add(key)
        for r in block["references"]:
            run = inspect(
                r,
                frequency=key[0],
                configuration=r["configuration"],
                duration=2,
                gain=block["gain_db"],
                mode="static",
            )
            if run["configuration"]["port"] != r["port"]:
                raise ValueError("Reference port identity differs")
        for r in block["captures"]:
            run = inspect(
                r,
                frequency=key[0],
                configuration=r["configuration"],
                duration=4,
                gain=block["gain_db"],
                mode="fast",
            )
            if run["configuration"]["dwell_us"] != r["dwell_us"]:
                raise ValueError("Dwell identity differs")
        print(f"audited {path.name}", flush=True)
    dense_path = root / "dense.json"
    dense, dense_hash = (
        snapshot(dense_path)
        if dense_path.exists()
        else ({"status": "not-started", "captures": []}, None)
    )
    grid = set()
    for item in dense["captures"]:
        key = (item["frequency_hz"], item["tx_channel"])
        if key in grid:
            raise ValueError("Duplicate dense frequency/source")
        grid.add(key)
        run = inspect(
            item,
            frequency=key[0],
            configuration="D",
            duration=4,
            gain=plan["dense"].get("receiver_gain_db", 60),
            mode="fast",
            tx=key[1],
        )
        if run["configuration"]["dwell_us"] != 200:
            raise ValueError("Dense dwell differs")
    expected_grid = {(f, tx) for f in range(5726000000, 5874000001, 1000000) for tx in (0, 1)}
    if not grid <= expected_grid:
        raise ValueError("Unexpected dense frequency/source")
    complete = (
        manifest["status"] == dense["status"] == "complete"
        and seen_blocks == set(specs)
        and grid == expected_grid
    )
    if not complete and not allow_partial:
        raise ValueError("Campaign is incomplete; no completion certificate issued")
    result = {
        "schema": 1,
        "status": "complete" if complete else "partial",
        "scope": (
            "Planned raw acquisitions only; analysis and final restoration "
            "require separate verification"
        ),
        "at": datetime.now(UTC).isoformat(),
        "plan_sha256": plan_hash,
        "blocks_manifest_sha256": blocks_hash,
        "dense_manifest_sha256": dense_hash,
        "source_sha256": sha256(Path(__file__)),
        "blocks": len(seen_blocks),
        "dense_records": len(grid),
        "unique_planned_runs": len(seen_runs),
        "transport_attempts_for_planned_runs": sum(r["transport_attempt_count"] for r in records),
        "raw_bytes_verified": sum(r["raw_bytes"] for r in records),
        "retained_incomplete_attempts": retained_attempts(plan),
        "headroom_warnings": [
            r["run_json"] for r in records if not r["conservative_headroom_pass"]
        ],
        "records": records,
    }
    save(root / "analysis/completion-audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    result = audit(args.campaign_root, allow_partial=args.allow_partial)
    print(result["status"], result["unique_planned_runs"], "unique planned runs", flush=True)
