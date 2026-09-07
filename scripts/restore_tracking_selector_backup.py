#!/usr/bin/env python3
"""Restore the pre-campaign bench image from an experimental flash backup."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / ".venv"
PYTHON = PREFIX / "bin/python"
SOURCE = ROOT / "src"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != PREFIX.resolve() or str(SOURCE) not in sys.path
):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(SOURCE), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    os.execve(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], environment)

from flash_tracking_c6_firmware import (
    BOARD_ID,
    FLASH_BASE,
    FLASH_SIZE,
    RECEIVER_SERIAL,
    RECEIVER_URI,
    SOURCE_SERIAL,
    SOURCE_URI,
    UID_ADDRESS,
    UID_SIZE,
    _load_json,
    _lock,
    _mute,
    _openocd,
    _sha256,
    _tcl,
    _write_json_atomic,
)
from smateway.bench import BenchManifest, OpenOcdBench

BENCH_BINARY = Path("build/STM32C011F4P6/bench/pluto_bench.bin")
BENCH_MANIFEST = Path("build/STM32C011F4P6/bench/pluto_bench.manifest.json")
OPENOCD_CONFIG = Path("openocd/stlink-v3-stm32c011.cfg")
DEFAULT_OUTPUT_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v4")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flash-evidence", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    return parser


def _backup_from_evidence(path: Path) -> tuple[dict[str, Any], Path]:
    value = _load_json(path.resolve(strict=True))
    backup = value.get("pre_program_flash")
    if (
        value.get("status") != "passed"
        or value.get("board_id") != BOARD_ID
        or not isinstance(backup, dict)
        or backup.get("size_bytes") != FLASH_SIZE
    ):
        raise ValueError("flash evidence has no valid full pre-program backup")
    backup_path = Path(str(backup.get("path", ""))).resolve(strict=True)
    if _sha256(backup_path) != backup.get("sha256"):
        raise ValueError("pre-program backup hash differs from its evidence")
    bench = (ROOT / BENCH_BINARY).resolve(strict=True).read_bytes()
    if backup_path.read_bytes()[: len(bench)] != bench:
        raise ValueError("pre-program backup is not the current reviewed bench image")
    return value, backup_path


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_selector_flash:
        raise SystemExit("selector restoration requires --acknowledge-selector-flash")
    flash, backup = _backup_from_evidence(args.flash_evidence)
    config = (ROOT / OPENOCD_CONFIG).resolve(strict=True)
    run_id = datetime.now(UTC).strftime("restore-bench-%Y%m%dT%H%M%S.%fZ")
    run_directory = args.output_root / "restores" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    uid = run_directory / "uid.bin"
    readback = run_directory / "restored-flash.bin"
    record: dict[str, Any] = {
        "schema": 1,
        "evidence_kind": "tracking_campaign_selector_backup_restore",
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "source_flash_evidence": {
            "path": str(args.flash_evidence.resolve()),
            "sha256": _sha256(args.flash_evidence),
            "run_id": flash["run_id"],
        },
        "backup": {"path": str(backup), "sha256": _sha256(backup)},
        "error": None,
    }
    evidence_path = run_directory / "restore.json"
    try:
        with _lock(args.output_root / ".hardware.lock"):
            record["radio_mute"] = [
                _mute(RECEIVER_URI, RECEIVER_SERIAL),
                _mute(SOURCE_URI, SOURCE_SERIAL),
            ]
            program = _openocd(
                config,
                (
                    "init; reset halt; "
                    f"dump_image {_tcl(uid)} 0x{UID_ADDRESS:x} 0x{UID_SIZE:x}; "
                    f"program {_tcl(backup)} 0x{FLASH_BASE:x} verify; reset run; shutdown"
                ),
                run_directory / "openocd-program.log",
            )
            if program.returncode or uid.read_bytes().hex() != BOARD_ID.removeprefix("stm32c011-"):
                raise RuntimeError("bench backup program or UID validation failed")
            verify = _openocd(
                config,
                (
                    "init; reset halt; "
                    f"dump_image {_tcl(readback)} 0x{FLASH_BASE:x} 0x{FLASH_SIZE:x}; "
                    "reset run; shutdown"
                ),
                run_directory / "openocd-readback.log",
            )
            if verify.returncode or readback.read_bytes() != backup.read_bytes():
                raise RuntimeError("restored full-flash readback differs from the backup")
            controller = OpenOcdBench(
                BenchManifest.load((ROOT / BENCH_MANIFEST).resolve(strict=True)),
                config,
            )
            status = controller.request(8, 0, wait_until_applied=False)
            if status.applied_code != 8 or status.lease_active:
                raise RuntimeError("restored bench did not accept lease-free ALL_OFF")
            record["selector_status"] = status.as_dict()
            record["restored_flash"] = {
                "path": str(readback),
                "sha256": _sha256(readback),
                "size_bytes": readback.stat().st_size,
                "matches_backup": True,
            }
            record["final_radio_mute"] = [
                _mute(RECEIVER_URI, RECEIVER_SERIAL),
                _mute(SOURCE_URI, SOURCE_SERIAL),
            ]
            record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        try:
            recovery = _openocd(
                config,
                "init; reset run; shutdown",
                run_directory / "openocd-failure-reset.log",
            )
            record["failure_reset_returncode"] = recovery.returncode
        except BaseException as recovery_error:
            record["failure_reset_error"] = {
                "type": type(recovery_error).__name__,
                "message": str(recovery_error),
            }
        raise
    finally:
        record["completed_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(evidence_path, record)
    print(f"restore_evidence={evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
