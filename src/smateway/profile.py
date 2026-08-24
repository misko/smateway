"""Validated access to the generated control profile."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALL_OFF_DEFINE = re.compile(r"^#define CONTROL_ALL_OFF_CODE (0x[0-9A-Fa-f]+)u$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ControlState:
    name: str
    gpio_code: int
    dwell_ms: int
    window_ms: tuple[float, float]


@dataclass(frozen=True, slots=True)
class ControlProfile:
    profile_id: str
    revision: int
    contract_sha256: str
    all_off_code: int
    guard_ms: int
    marker_body_ms: int
    nominal_cycle_ms: int
    recommended_capture_ms: int
    states: tuple[ControlState, ...]


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _state(value: object) -> ControlState:
    item = _mapping(value, "state")
    raw_window = item.get("window_ms")
    if not isinstance(raw_window, list) or len(raw_window) != 2:
        raise ValueError("every state window must have two bounds")
    return ControlState(
        name=str(item["name"]),
        gpio_code=int(str(item["gpio_code_pa3_pa0"]), 2),
        dwell_ms=int(item["dwell_ms"]),
        window_ms=(float(raw_window[0]), float(raw_window[1])),
    )


def load_profile(path: Path) -> ControlProfile:
    document = _mapping(json.loads(path.read_text(encoding="utf-8")), "profile")
    profile = _mapping(document.get("profile"), "profile.profile")
    frame = _mapping(document.get("frame"), "profile.frame")
    marker = _mapping(frame.get("marker"), "profile.frame.marker")
    raw_states = document.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        raise ValueError("profile.states must be a non-empty array")

    states = tuple(_state(item) for item in raw_states)
    if len({state.name for state in states}) != len(states):
        raise ValueError("profile state names must be unique")
    if len({state.gpio_code for state in states}) != len(states):
        raise ValueError("profile state codes must be unique")
    guard_ms = int(frame["all_off_guard_ms"])
    marker_body_ms = int(marker["body_nominal_ms"])
    nominal_cycle_ms = int(frame["nominal_cycle_ms"])
    derived_cycle = marker_body_ms + guard_ms * len(states) + sum(
        state.dwell_ms for state in states
    )
    if derived_cycle != nominal_cycle_ms:
        raise ValueError(
            f"nominal cycle {nominal_cycle_ms} does not match derived {derived_cycle}"
        )

    digest = str(document["contract_sha256"])
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("invalid contract SHA-256")
    header_path = path.with_name("control_profile.h")
    header_match = ALL_OFF_DEFINE.search(header_path.read_text(encoding="utf-8"))
    if header_match is None:
        raise ValueError("generated header does not define CONTROL_ALL_OFF_CODE")
    all_off_code = int(header_match.group(1), 16)
    if all_off_code in {state.gpio_code for state in states}:
        raise ValueError("ALL_OFF code overlaps a selected state")
    return ControlProfile(
        profile_id=str(profile["id"]),
        revision=int(profile["revision"]),
        contract_sha256=digest,
        all_off_code=all_off_code,
        guard_ms=guard_ms,
        marker_body_ms=marker_body_ms,
        nominal_cycle_ms=nominal_cycle_ms,
        recommended_capture_ms=int(frame["recommended_capture_ms"]),
        states=states,
    )


def verify_provenance(profile_path: Path, provenance_path: Path) -> bool:
    provenance = _mapping(
        json.loads(provenance_path.read_text(encoding="utf-8")), "provenance"
    )
    artifacts = _mapping(provenance.get("artifacts"), "provenance.artifacts")
    digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    return artifacts.get("control_profile.json") == digest
