#!/usr/bin/env python3
"""Mute both Plutos, program one verified C6 timing image, and read it back."""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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

from pluto_plus.hardware.iio import _mute_transmit, _release_device

BOARD_ID = "stm32c011-4c0055000950313950363920"
RECEIVER_URI = "ip:192.168.1.15"
RECEIVER_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_URI = "ip:192.168.1.179"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"
OPENOCD_CONFIG = Path("openocd/stlink-v3-stm32c011.cfg")
OPENOCD_BINARY = Path(
    "/srv/bulk/samteway/tooling/openocd-stm32c0-install/bin/openocd"
)
OPENOCD_SOURCE_COMMIT = "43648fedd39440b06662ec16a0643b3081b1de53"
FLASH_BANK_COMMAND = (
    "flash bank smateway.flash stm32l4x 0x08000000 0 0 0 smateway.cpu"
)
DEFAULT_OUTPUT_ROOT = Path("/srv/bulk/samteway/lab-data/tracking-fast-timing-20260903-v4")
FLASH_BASE = 0x0800_0000
FLASH_SIZE = 16 * 1024
UID_ADDRESS = 0x1FFF_7550
UID_SIZE = 12


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--openocd-config", type=Path, default=OPENOCD_CONFIG)
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _write_json_atomic(path: Path, document: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _tcl(path: Path) -> str:
    value = str(path.resolve())
    if any(character in value for character in "{}\n\r"):
        raise ValueError("OpenOCD path contains an unsupported character")
    return "{" + value + "}"


def _openocd(config: Path, command: str, log_path: Path) -> subprocess.CompletedProcess[str]:
    executable = OPENOCD_BINARY.resolve(strict=True)
    process = subprocess.run(
        (
            str(executable),
            "-f",
            str(config.resolve()),
            "-c",
            f"{FLASH_BANK_COMMAND}; {command}",
        ),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(process.stdout, encoding="utf-8")
    return process


def _mute(uri: str, serial: str) -> dict[str, Any]:
    adi = importlib.import_module("adi")
    device = adi.ad9361(uri=uri)
    try:
        facts = {str(key): str(value) for key, value in device.ctx.attrs.items()}
        if facts.get("hw_serial") != serial or facts.get("uri") != uri:
            raise RuntimeError("Pluto identity differs from the exact flash safety target")
        _mute_transmit(device)
        _mute_transmit(device)
        gains = [float(device.tx_hardwaregain_chan0), float(device.tx_hardwaregain_chan1)]
        scales = [float(value) for value in device.dds_scales]
        if gains != [-80.0, -80.0] or len(scales) != 8 or any(scales):
            raise RuntimeError("exact transmit mute readback failed")
        return {"uri": uri, "serial": serial, "tx_gain_db": gains, "dds_scales": scales}
    finally:
        _release_device(device)


def _validated_build(path: Path) -> tuple[dict[str, Any], Path, int]:
    document = _load_json(path)
    binary = document.get("binary")
    profile = document.get("profile")
    if (
        document.get("status") != "passed"
        or document.get("evidence_kind") != "experimental_tracking_c6_build_verification"
        or not isinstance(binary, dict)
        or not isinstance(profile, dict)
        or document.get("guard_us") != 20
        or document.get("dwell_us") not in (25, 50, 100, 200, 1000)
    ):
        raise ValueError("build manifest is not a passed C6 timing verification")
    binary_path = Path(str(binary.get("path", ""))).resolve(strict=True)
    profile_path = Path(str(profile.get("path", ""))).resolve(strict=True)
    if (
        _sha256(binary_path) != binary.get("sha256")
        or binary_path.stat().st_size != binary.get("size_bytes")
        or _sha256(profile_path) != profile.get("sha256")
    ):
        raise ValueError("build manifest file binding differs")
    return document, binary_path, int(document["dwell_us"])


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_selector_flash:
        raise SystemExit("selector programming requires --acknowledge-selector-flash")
    build, binary, dwell_us = _validated_build(args.build_manifest.resolve(strict=True))
    config = args.openocd_config.resolve(strict=True)
    run_id = datetime.now(UTC).strftime(f"flash-c6-{dwell_us}us-%Y%m%dT%H%M%S.%fZ")
    run_directory = args.output_root / "flashes" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    backup = run_directory / "pre-program-flash.bin"
    uid_before = run_directory / "uid-before.bin"
    readback = run_directory / "post-program-readback.bin"
    uid_after = run_directory / "uid-after.bin"
    record: dict[str, Any] = {
        "schema": 1,
        "evidence_kind": "experimental_tracking_c6_flash_readback",
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "board_id": BOARD_ID,
        "build_manifest": {
            "path": str(args.build_manifest.resolve()),
            "sha256": _sha256(args.build_manifest),
        },
        "build": build,
        "radio_mute": None,
        "target_uid_hex": None,
        "openocd": {
            "path": str(OPENOCD_BINARY.resolve(strict=True)),
            "sha256": _sha256(OPENOCD_BINARY.resolve(strict=True)),
            "source_commit": OPENOCD_SOURCE_COMMIT,
        },
        "pre_program_flash": None,
        "post_program_readback": None,
        "error": None,
    }
    evidence_path = run_directory / "flash.json"
    try:
        with _lock(args.output_root / ".hardware.lock"):
            record["radio_mute"] = [
                _mute(RECEIVER_URI, RECEIVER_SERIAL),
                _mute(SOURCE_URI, SOURCE_SERIAL),
            ]
            command = (
                "init; reset halt; "
                f"dump_image {_tcl(backup)} 0x{FLASH_BASE:x} 0x{FLASH_SIZE:x}; "
                f"dump_image {_tcl(uid_before)} 0x{UID_ADDRESS:x} 0x{UID_SIZE:x}; shutdown"
            )
            before = _openocd(config, command, run_directory / "openocd-before.log")
            if before.returncode != 0 or backup.stat().st_size != FLASH_SIZE:
                raise RuntimeError("pre-program flash/UID readback failed")
            expected_uid = BOARD_ID.removeprefix("stm32c011-")
            if uid_before.read_bytes().hex() != expected_uid:
                raise RuntimeError("live selector UID differs from the pinned board")
            record["target_uid_hex"] = expected_uid
            record["pre_program_flash"] = {
                "path": str(backup),
                "sha256": _sha256(backup),
                "size_bytes": backup.stat().st_size,
            }
            program = _openocd(
                config,
                (
                    "init; reset halt; "
                    f"program {_tcl(binary)} 0x{FLASH_BASE:x} verify; reset run; shutdown"
                ),
                run_directory / "openocd-program.log",
            )
            if program.returncode != 0:
                raise RuntimeError("OpenOCD program-with-verify failed")
            after = _openocd(
                config,
                (
                    "init; reset halt; "
                    f"dump_image {_tcl(readback)} 0x{FLASH_BASE:x} 0x{binary.stat().st_size:x}; "
                    f"dump_image {_tcl(uid_after)} 0x{UID_ADDRESS:x} 0x{UID_SIZE:x}; "
                    "reset run; shutdown"
                ),
                run_directory / "openocd-after.log",
            )
            if (
                after.returncode != 0
                or readback.read_bytes() != binary.read_bytes()
                or uid_after.read_bytes() != uid_before.read_bytes()
            ):
                raise RuntimeError("post-program byte-for-byte flash/UID readback failed")
            record["post_program_readback"] = {
                "path": str(readback),
                "sha256": _sha256(readback),
                "size_bytes": readback.stat().st_size,
                "matches_binary": True,
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
            if backup.is_file() and backup.stat().st_size == FLASH_SIZE:
                recovery = _openocd(
                    config,
                    (
                        "init; reset halt; "
                        f"program {_tcl(backup)} 0x{FLASH_BASE:x} verify; "
                        "reset run; shutdown"
                    ),
                    run_directory / "openocd-failure-restore.log",
                )
                record["failure_recovery"] = {
                    "attempted": True,
                    "restored_pre_program_flash": recovery.returncode == 0,
                    "returncode": recovery.returncode,
                }
            else:
                recovery = _openocd(
                    config,
                    "init; reset run; shutdown",
                    run_directory / "openocd-failure-reset.log",
                )
                record["failure_recovery"] = {
                    "attempted": True,
                    "restored_pre_program_flash": False,
                    "returncode": recovery.returncode,
                }
        except BaseException as recovery_error:
            record["failure_recovery"] = {
                "attempted": True,
                "restored_pre_program_flash": False,
                "error": {
                    "type": type(recovery_error).__name__,
                    "message": str(recovery_error),
                },
            }
        raise
    finally:
        record["completed_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(evidence_path, record)
    print(f"flash_evidence={evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
