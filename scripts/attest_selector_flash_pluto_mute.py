#!/usr/bin/env python3
"""Create immutable exact-Pluto-mute evidence for one selector-flash checkpoint."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SMATEWAY_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SMATEWAY_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned capture Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    loader_entries = [
        item
        for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(_REQUIRED_LIBIIO_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

from smateway.file_artifact_admission import (  # noqa: E402
    FileArtifactAdmissionError,
    assert_local_rpi_storage,
    assert_no_symlink_chain,
)
from smateway.selector_flash_attestation import (  # noqa: E402
    PLUTO_MUTE_EVIDENCE_KIND,
    SelectorFlashError,
    _source_identity,
    _write_new_json,
    subprocess_boundary,
    validate_pluto_mute_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        choices=("phase1_pre_openocd", "phase2_pre_openocd"),
        required=True,
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = _source_identity(_REPOSITORY, subprocess_boundary)
        muted: Any = importlib.import_module("scripts.run_5g8_muted_control")
        readback = muted._strict_exact_mute(args.serial, args.uri, args.checkpoint)
        if not muted._mute_passed(
            readback,
            serial=args.serial,
            uri=args.uri,
            purpose=args.checkpoint,
        ):
            raise SelectorFlashError("exact Pluto mute/readback failed; no evidence was published")
        document = {
            "schema": 1,
            "evidence_kind": PLUTO_MUTE_EVIDENCE_KIND,
            "checkpoint": args.checkpoint,
            "status": "passed",
            "serial": args.serial,
            "uri": args.uri,
            "tx_hardware_gain_db_by_channel": readback["tx_hardware_gain_db_by_channel"],
            "dds_raw_readback": readback["dds_raw_readback"],
            "dds_scale_readback": readback["dds_scale_readback"],
            "dds_enabled_readback": readback["dds_enabled_readback"],
            "started_at": readback["started_at"],
            "completed_at": readback["completed_at"],
            "source": source,
            "error": None,
        }
        normalized = validate_pluto_mute_evidence(
            document,
            checkpoint=args.checkpoint,
            serial=args.serial,
            uri=args.uri,
            source=source,
            validated_at=str(document["completed_at"]),
        )
        output = args.output.expanduser().absolute()
        assert_no_symlink_chain(output, label="Pluto mute evidence output")
        assert_local_rpi_storage(output, label="Pluto mute evidence output storage")
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_no_symlink_chain(output.parent, label="Pluto mute evidence parent")
        assert_local_rpi_storage(output.parent, label="Pluto mute evidence parent storage")
        _write_new_json(output, normalized)
        print(
            json.dumps(
                {
                    "status": "passed",
                    "checkpoint": args.checkpoint,
                    "serial": args.serial,
                    "uri": args.uri,
                    "output": str(output),
                    "owner_writable": bool(output.stat().st_mode & 0o200),
                    "openocd_access": False,
                    "rf_transmission": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (FileArtifactAdmissionError, OSError, SelectorFlashError, ValueError) as error:
        print(f"selector-flash Pluto mute attestation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
