#!/usr/bin/env python3
"""Run the C6 timing ladder and a dense sweep at the fastest robust schedule."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v6")
DWELLS_US = (200, 100, 50, 25)
SENTINEL_FREQUENCIES_HZ = (
    5_726_000_000,
    5_750_000_000,
    5_775_000_000,
    5_800_000_000,
    5_825_000_000,
    5_850_000_000,
    5_874_000_000,
)
RUN_DIRECTORY = re.compile(r"^run_dir=(/.+)$", re.MULTILINE)
FLASH_EVIDENCE = re.compile(r"^flash_evidence=(/.+)$", re.MULTILINE)
RESTORE_EVIDENCE = re.compile(r"^restore_evidence=(/.+)$", re.MULTILINE)
SOURCE_CONTRACT = (
    "scripts/run_fast_tracking_timing_campaign.py",
    "scripts/capture_fast_tracking_timing.py",
    "scripts/flash_tracking_c6_firmware.py",
    "scripts/restore_tracking_selector_backup.py",
    "scripts/verify_tracking_c6_firmware.py",
    "src/smateway/fast_tracking.py",
    "src/smateway/tracking/bearing.py",
    "src/smateway/tracking/calibration.py",
    "src/smateway/tracking/manifold.py",
    "src/smateway/tracking/schedule.py",
    "docs/tracking_development_plan/data/ism-frequency-plan.json",
    "docs/pcb_direct_injection_calibration/data/calibration-lut.json",
    "build/STM32C011F4P6/bench/pluto_bench.bin",
    "build/STM32C011F4P6/bench/pluto_bench.manifest.json",
    "openocd/stlink-v3-stm32c011.cfg",
    *tuple(
        f"profiles/tracking-c6-{dwell}us-v1/control_profile.json"
        for dwell in DWELLS_US
    ),
    *tuple(
        f"build/STM32C011F4P6/tracking-c6-{dwell}us/pluto_tracking_c6.bin"
        for dwell in DWELLS_US
    ),
    *tuple(
        f"build/STM32C011F4P6/tracking-c6-{dwell}us/pluto_tracking_c6.build.json"
        for dwell in DWELLS_US
    ),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--ladder-only", action="store_true")
    parser.add_argument("--selected-dwell-us", type=int, choices=DWELLS_US)
    parser.add_argument(
        "--capture-attempts",
        type=_positive_int,
        default=3,
        help="Maximum attempts for an isolated capture-quality failure (default: 3)",
    )
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _source_contract() -> dict[str, str]:
    result = {}
    for relative in SOURCE_CONTRACT:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"campaign source is absent, non-regular, or a symlink: {relative}")
        result[relative] = _sha256(path)
    return result


def _write(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("fast timing campaign is already running") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _run(command: tuple[str, ...]) -> str:
    process = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(
            f"child command failed with exit {process.returncode}: {' '.join(command)}\n"
            + process.stdout
        )
    return process.stdout


def _paths(dwell_us: int) -> tuple[Path, Path]:
    profile = ROOT / f"profiles/tracking-c6-{dwell_us}us-v1/control_profile.json"
    build = (
        ROOT
        / f"build/STM32C011F4P6/tracking-c6-{dwell_us}us/pluto_tracking_c6.build.json"
    )
    return profile, build


def _flash(output_root: Path, dwell_us: int) -> Path:
    _profile, build = _paths(dwell_us)
    output = _run(
        (
            sys.executable,
            str(ROOT / "scripts/flash_tracking_c6_firmware.py"),
            "--build-manifest",
            str(build),
            "--output-root",
            str(output_root),
            "--acknowledge-selector-flash",
        )
    )
    match = FLASH_EVIDENCE.search(output)
    if match is None:
        raise RuntimeError("flash child did not print its evidence path")
    path = Path(match.group(1))
    if _load(path).get("status") != "passed":
        raise RuntimeError("flash child evidence is not passed")
    return path


def _capture(
    output_root: Path,
    *,
    dwell_us: int,
    flash_evidence: Path,
    frequency_hz: int,
    tx_channel: int,
    attempts: int,
    failed_attempts: list[dict[str, Any]],
) -> Path:
    profile, _build = _paths(dwell_us)
    command = (
        sys.executable,
        str(ROOT / "scripts/capture_fast_tracking_timing.py"),
        "--profile",
        str(profile),
        "--flash-evidence",
        str(flash_evidence),
        "--frequency-hz",
        str(frequency_hz),
        "--tx-channel",
        str(tx_channel),
        "--output-root",
        str(output_root),
        "--acknowledge-ota-authorization",
    )
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            output = _run(command)
        except RuntimeError as error:
            last_error = error
            match = RUN_DIRECTORY.search(str(error))
            evidence: dict[str, Any] = {
                "attempt": attempt,
                "dwell_us": dwell_us,
                "frequency_hz": frequency_hz,
                "tx_channel": tx_channel,
                "error": str(error),
            }
            if match is not None:
                run_json = Path(match.group(1)) / "run.json"
                if run_json.is_file():
                    evidence["run_json"] = str(run_json)
                    evidence["run_sha256"] = _sha256(run_json)
                    record = _load(run_json)
                    evidence["capture_error"] = record.get("error")
            failed_attempts.append(evidence)
            if attempt < attempts:
                print(
                    f"[retry {attempt + 1}/{attempts}] dwell={dwell_us}us "
                    f"frequency={frequency_hz / 1e6:.3f}MHz TX{tx_channel + 1}",
                    flush=True,
                )
                continue
            break
        match = RUN_DIRECTORY.search(output)
        if match is None:
            raise RuntimeError("capture child did not print its run directory")
        path = Path(match.group(1)) / "run.json"
        if _load(path).get("status") != "passed":
            raise RuntimeError("capture child evidence is not passed")
        return path
    assert last_error is not None
    raise RuntimeError(
        f"capture failed after {attempts} attempt(s): dwell={dwell_us}us "
        f"frequency={frequency_hz}Hz TX{tx_channel + 1}\n{last_error}"
    )


def _restore(output_root: Path, flash_evidence: Path) -> Path:
    output = _run(
        (
            sys.executable,
            str(ROOT / "scripts/restore_tracking_selector_backup.py"),
            "--flash-evidence",
            str(flash_evidence),
            "--output-root",
            str(output_root),
            "--acknowledge-selector-flash",
        )
    )
    match = RESTORE_EVIDENCE.search(output)
    if match is None:
        raise RuntimeError("restore child did not print its evidence path")
    path = Path(match.group(1))
    if _load(path).get("status") != "passed":
        raise RuntimeError("restore child evidence is not passed")
    return path


def _first_passing_latency(run: dict[str, Any]) -> float | None:
    analysis = run.get("analysis")
    if not isinstance(analysis, dict):
        return None
    frequency_acceptance = analysis.get("frequency_difference_acceptance")
    if isinstance(frequency_acceptance, dict) and not frequency_acceptance.get(
        "qualified", False
    ):
        return None
    study = analysis.get("integration_study")
    if not isinstance(study, list):
        return None
    for row in study:
        if (
            isinstance(row, dict)
            and isinstance(row.get("groups_per_port"), int)
            and row["groups_per_port"] >= 8
            and isinstance(
                row.get("phase_rms_deg_power_weighted_ports"), (int, float)
            )
            and row["phase_rms_deg_power_weighted_ports"] <= 10.0
        ):
            return float(row["measured_wall_latency_ms"])
    return None


def _select_dwell(conditions: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    summary: dict[str, Any] = {}
    qualified: list[tuple[float, int]] = []
    for dwell_us in DWELLS_US:
        selected = [item for item in conditions if item["dwell_us"] == dwell_us]
        latencies = []
        for condition in selected:
            latency = _first_passing_latency(_load(Path(condition["run_json"])))
            condition["first_phase_rms_10deg_wall_latency_ms"] = latency
            latencies.append(latency)
        complete = len(selected) == 2 * len(SENTINEL_FREQUENCIES_HZ)
        passes = complete and all(value is not None for value in latencies)
        worst = max(value for value in latencies if value is not None) if passes else None
        summary[str(dwell_us)] = {
            "condition_count": len(selected),
            "all_conditions_reach_timing_quality_gate": passes,
            "worst_condition_wall_latency_ms": worst,
            "theoretical_c6_scans_per_second": 1e6 / (180 + 6 * (20 + dwell_us)),
        }
        if worst is not None:
            qualified.append((worst, dwell_us))
    if not qualified:
        raise RuntimeError("no autonomous dwell reaches 10 degree power-weighted phase RMS")
    worst_latency, dwell_us = min(qualified, key=lambda item: (item[0], item[1]))
    return dwell_us, {
        "criterion": (
            "minimize the worst sentinel-frequency wall latency at which both TX modes reach "
            "10 degree received-power-weighted phase RMS with at least eight independent "
            "groups; null-port, observable-port maximum, settling, and ideal-manifold "
            "bearing metrics remain separately reported diagnostics; "
            "tie-break toward shorter dwell"
        ),
        "selected_dwell_us": dwell_us,
        "selected_worst_wall_latency_ms": worst_latency,
        "by_dwell_us": summary,
    }


def _new_manifest(source_contract: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": 1,
        "campaign_id": "tracking-fast-timing-20260903-v6",
        "evidence_kind": "autonomous_c6_timing_ladder_and_dense_frequency_sweep",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "source_contract_sha256": source_contract,
        "ladder_frequencies_hz": list(SENTINEL_FREQUENCIES_HZ),
        "dwell_grid_us": list(DWELLS_US),
        "ladder_conditions": [],
        "selection": None,
        "dense_conditions": [],
        "capture_failures": [],
        "flash_evidence": [],
        "restore_evidence": None,
        "error": None,
    }


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_ota_authorization or not args.acknowledge_selector_flash:
        raise SystemExit("campaign requires both RF and selector-flash acknowledgements")
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_root / "campaign.json"
    source_contract = _source_contract()
    with _lock(args.output_root / ".campaign.lock"):
        manifest = (
            _load(manifest_path)
            if manifest_path.exists()
            else _new_manifest(source_contract)
        )
        if manifest.get("source_contract_sha256") != source_contract:
            raise RuntimeError("fast timing source contract differs from the campaign manifest")
        try:
            completed_ladder = {
                (item["dwell_us"], item["frequency_hz"], item["tx_channel"])
                for item in manifest["ladder_conditions"]
            }
            for dwell_us in DWELLS_US:
                missing = [
                    (frequency_hz, tx_channel)
                    for frequency_hz in SENTINEL_FREQUENCIES_HZ
                    for tx_channel in (0, 1)
                    if (dwell_us, frequency_hz, tx_channel) not in completed_ladder
                ]
                if not missing:
                    continue
                if _source_contract() != source_contract:
                    raise RuntimeError("fast timing source changed before a ladder flash")
                flash = _flash(args.output_root, dwell_us)
                manifest["flash_evidence"].append(
                    {"dwell_us": dwell_us, "path": str(flash), "sha256": _sha256(flash)}
                )
                _write(manifest_path, manifest)
                for frequency_hz, tx_channel in missing:
                    if _source_contract() != source_contract:
                        raise RuntimeError("fast timing source changed during the ladder")
                    ordinal = len(manifest["ladder_conditions"]) + 1
                    print(
                        f"[ladder {ordinal}/{len(DWELLS_US) * 2 * len(SENTINEL_FREQUENCIES_HZ)}] "
                        f"dwell={dwell_us}us frequency={frequency_hz / 1e6:.3f}MHz "
                        f"TX{tx_channel + 1}",
                        flush=True,
                    )
                    run = _capture(
                        args.output_root,
                        dwell_us=dwell_us,
                        flash_evidence=flash,
                        frequency_hz=frequency_hz,
                        tx_channel=tx_channel,
                        attempts=args.capture_attempts,
                        failed_attempts=manifest["capture_failures"],
                    )
                    manifest["ladder_conditions"].append(
                        {
                            "dwell_us": dwell_us,
                            "frequency_hz": frequency_hz,
                            "tx_channel": tx_channel,
                            "run_json": str(run),
                            "run_sha256": _sha256(run),
                        }
                    )
                    _write(manifest_path, manifest)
            selected, selection = _select_dwell(manifest["ladder_conditions"])
            if args.selected_dwell_us is not None:
                selected = args.selected_dwell_us
                selection["automatic_selected_dwell_us"] = selection["selected_dwell_us"]
                selection["selected_dwell_us"] = selected
                selection["operator_override"] = True
            manifest["selection"] = selection
            _write(manifest_path, manifest)
            if args.ladder_only:
                manifest["status"] = "ladder_complete"
                _write(manifest_path, manifest)
                print(f"campaign={manifest_path}")
                return 0

            dense_complete = {
                (item["frequency_hz"], item["tx_channel"])
                for item in manifest["dense_conditions"]
            }
            missing_dense = [
                (frequency_hz, tx_channel)
                for frequency_hz in range(5_726_000_000, 5_874_000_001, 1_000_000)
                for tx_channel in (0, 1)
                if (frequency_hz, tx_channel) not in dense_complete
            ]
            if missing_dense:
                if _source_contract() != source_contract:
                    raise RuntimeError("fast timing source changed before the dense flash")
                flash = _flash(args.output_root, selected)
                manifest["flash_evidence"].append(
                    {"dwell_us": selected, "path": str(flash), "sha256": _sha256(flash)}
                )
                _write(manifest_path, manifest)
                for frequency_hz, tx_channel in missing_dense:
                    if _source_contract() != source_contract:
                        raise RuntimeError("fast timing source changed during the dense sweep")
                    ordinal = len(manifest["dense_conditions"]) + 1
                    print(
                        f"[dense {ordinal}/298] dwell={selected}us "
                        f"frequency={frequency_hz / 1e6:.3f}MHz TX{tx_channel + 1}",
                        flush=True,
                    )
                    run = _capture(
                        args.output_root,
                        dwell_us=selected,
                        flash_evidence=flash,
                        frequency_hz=frequency_hz,
                        tx_channel=tx_channel,
                        attempts=args.capture_attempts,
                        failed_attempts=manifest["capture_failures"],
                    )
                    manifest["dense_conditions"].append(
                        {
                            "dwell_us": selected,
                            "frequency_hz": frequency_hz,
                            "tx_channel": tx_channel,
                            "run_json": str(run),
                            "run_sha256": _sha256(run),
                        }
                    )
                    _write(manifest_path, manifest)
            manifest["status"] = "complete"
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            _write(manifest_path, manifest)
        except BaseException as error:
            manifest["status"] = "failed"
            manifest["error"] = {"type": type(error).__name__, "message": str(error)}
            _write(manifest_path, manifest)
            raise
        finally:
            flashes = manifest.get("flash_evidence")
            if isinstance(flashes, list) and flashes:
                try:
                    first_flash = Path(str(flashes[0]["path"]))
                    restore = _restore(args.output_root, first_flash)
                    manifest["restore_evidence"] = {
                        "path": str(restore),
                        "sha256": _sha256(restore),
                    }
                except BaseException as restore_error:
                    manifest["status"] = "failed"
                    manifest["restore_error"] = {
                        "type": type(restore_error).__name__,
                        "message": str(restore_error),
                    }
                    _write(manifest_path, manifest)
                    raise
                _write(manifest_path, manifest)
    print(f"campaign={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
