#!/usr/bin/env python3
"""Fail closed when a pinned static-screen scan shard is incomplete or unsafe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

STATES = ("ALL_OFF", *(f"ANT{index}" for index in range(1, 9)))
EXPECTED_FIXTURE = {
    "mode": "external",
    "radio_uri": "ip:192.168.1.15",
    "radio_serial": "104000b29905000e17000800065934759d",
    "source_radio_uri": "ip:192.168.1.173",
    "source_radio_serial": "104473b80a16000de6ff2000f8a6beca79",
    "board_id": "stm32c011-4c0055000950313950363920",
    "stlink_serial": "002D003A3335511035383531",
}
EXPECTED_CONFIGURATION = {
    "states": list(STATES),
    "repeats": 1,
    "sample_rate_hz": 2_000_000,
    "bandwidth_hz": 1_600_000,
    "sample_count": 262_144,
    "tone_offset_hz": 100_000,
    "rx_gain_db": 60,
    "tx_gain_db": -40.0,
    "dds_scale": 0.25,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json", type=Path)
    parser.add_argument("--first-hz", type=int, required=True)
    parser.add_argument("--last-hz", type=int, required=True)
    parser.add_argument("--step-hz", type=int, default=1_000_000)
    return parser


def _load(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file() or resolved.name != "run.json":
        raise ValueError("input must be a regular run.json")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run.json is not an object")
    return value


def _exact_mute(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("passed") is True
        and value.get("tx_gain_db") == [-80.0, -80.0]
        and value.get("dds_scales") == [0.0] * 8
    )


def validate(path: Path, first_hz: int, last_hz: int, step_hz: int) -> dict[str, Any]:
    if step_hz <= 0 or first_hz > last_hz or (last_hz - first_hz) % step_hz:
        raise ValueError("invalid frequency interval")
    frequencies = list(range(first_hz, last_hz + 1, step_hz))
    run = _load(path)
    for key, expected in EXPECTED_FIXTURE.items():
        if run.get(key) != expected:
            raise ValueError(f"fixture mismatch at {key}")
    if run.get("schema") != 1 or run.get("error") is not None:
        raise ValueError("run is failed or has an unknown schema")
    configuration = run.get("configuration")
    expected_configuration = {**EXPECTED_CONFIGURATION, "frequencies_hz": frequencies}
    if configuration != expected_configuration:
        raise ValueError("capture configuration is not exact")
    observations = run.get("observations")
    if not isinstance(observations, list):
        raise ValueError("observations are absent")
    expected_order = [(frequency, state, 1) for frequency in frequencies for state in STATES]
    actual_order = [
        (row.get("frequency_hz"), row.get("state"), row.get("repeat"))
        for row in observations
        if isinstance(row, dict)
    ]
    if actual_order != expected_order:
        raise ValueError("observation order or completeness differs")
    for row in observations:
        if (
            row.get("analysis") is None
            or row.get("analysis_error") is not None
            or not _exact_mute(row.get("post_capture_mute"))
            or not _exact_mute(row.get("post_capture_source_mute"))
        ):
            raise ValueError("observation analysis or post-capture safety failed")
    if not _exact_mute(run.get("final_radio_mute")) or not _exact_mute(
        run.get("final_source_radio_mute")
    ):
        raise ValueError("final radio safety failed")
    selector = run.get("final_selector")
    if not isinstance(selector, dict) or (
        selector.get("applied_code") != 8
        or selector.get("remaining_lease_ms") != 0
        or selector.get("lease_active") is not False
        or selector.get("guard_active") is not False
        or selector.get("invalid_command") is not False
    ):
        raise ValueError("final selector safety failed")
    iq_files = sorted(path.resolve().parent.glob("*.npz"))
    if len(iq_files) != len(expected_order):
        raise ValueError("raw IQ file count differs")
    maximum_adc = max(
        max(float(value) for value in row["analysis"]["peak_component_counts"])
        for row in observations
    )
    return {
        "run_id": run["run_id"],
        "run_json": str(path.resolve()),
        "run_json_sha256": hashlib.sha256(path.resolve().read_bytes()).hexdigest(),
        "first_hz": first_hz,
        "last_hz": last_hz,
        "frequency_count": len(frequencies),
        "capture_count": len(expected_order),
        "maximum_peak_component_counts": maximum_adc,
        "analysis_error_count": 0,
        "final_safety_passed": True,
    }


def main() -> int:
    args = _parser().parse_args()
    print(
        json.dumps(
            validate(args.run_json, args.first_hz, args.last_hz, args.step_hz),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
