from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DWELLS_US = (25, 50, 100, 200)
PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
CODES = ("0000", "0100", "0110", "0111", "0011", "0001")


def test_generated_tracking_c6_profiles_are_exact_and_safe() -> None:
    for dwell_us in DWELLS_US:
        directory = ROOT / f"profiles/tracking-c6-{dwell_us}us-v1"
        profile_path = directory / "control_profile.json"
        header_path = directory / "control_profile.h"
        provenance = json.loads((directory / "provenance.json").read_text())
        profile = json.loads(profile_path.read_text())

        assert profile["profile"]["id"] == f"tracking-c6-{dwell_us}us-v1"
        assert profile["frame"]["order"] == list(PORTS)
        assert profile["frame"]["all_off_guard_us"] == 20
        assert profile["frame"]["nominal_cycle_us"] == 180 + 6 * (20 + dwell_us)
        assert [item["name"] for item in profile["states"]] == list(PORTS)
        assert [item["gpio_code_pa3_pa0"] for item in profile["states"]] == list(CODES)
        assert all(item["dwell_us"] == dwell_us for item in profile["states"])
        assert profile["safety"]["all_off_code"] == "1000"
        assert profile["release_contract"]["conformant"] is False
        assert profile["safety"]["unused_states"] == ["ANT3", "ANT6"]

        for filename, path in (
            ("control_profile.json", profile_path),
            ("control_profile.h", header_path),
        ):
            assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance["artifacts"][
                filename
            ]
