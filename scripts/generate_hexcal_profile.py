#!/usr/bin/env python3
"""Generate the six-element microsecond calibration profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SPEC_PATH = Path("profiles/hexcal-v1/profile_spec.json")
OUTPUT_DIRECTORY = SPEC_PATH.parent
ALL_OFF_DEFINE = re.compile(
    r"^#define CONTROL_ALL_OFF_CODE (0x[0-9A-Fa-f]+)u$", re.MULTILINE
)
EXPECTED_ORDER = [f"ANT{index}" for index in range(1, 7)]
EXPECTED_SOURCE = Path("profiles/fast20-v1/control_profile.json")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _positive_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_inputs() -> tuple[dict[str, Any], dict[str, Any], Path, int]:
    spec = _object(json.loads(SPEC_PATH.read_text(encoding="utf-8")), "spec")
    if spec.get("schema") != 1:
        raise ValueError("hexcal profile spec schema must be 1")
    source_path = Path(str(spec.get("source_profile", "")))
    if source_path != EXPECTED_SOURCE:
        raise ValueError("hexcal-v1 must derive from the qualified fast20-v1 profile")
    source = _object(json.loads(source_path.read_text(encoding="utf-8")), "source profile")
    source_identity = _object(source.get("profile"), "source profile identity")
    if source_identity.get("id") != "fast20-v1" or source_identity.get("revision") != 1:
        raise ValueError("hexcal-v1 source profile identity changed")
    source_header = source_path.with_name("control_profile.h")
    match = ALL_OFF_DEFINE.search(source_header.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("source profile header does not define ALL_OFF")
    return spec, source, source_path, int(match.group(1), 16)


def _generated_profile(
    spec: dict[str, Any], source: dict[str, Any], all_off_code: int
) -> dict[str, Any]:
    if spec.get("profile_id") != "hexcal-v1" or spec.get("revision") != 1:
        raise ValueError("hexcal-v1 identity changed without a new generator")
    if spec.get("physical_order") != "clockwise" or spec.get("forward_reference") != "ANT1":
        raise ValueError("hexcal-v1 requires clockwise order with ANT1 forward")
    order = spec.get("state_order")
    if not isinstance(order, list) or order != EXPECTED_ORDER:
        raise ValueError("hexcal state order must be ANT1 through ANT6")
    source_states = source.get("states")
    if not isinstance(source_states, list):
        raise ValueError("source profile states must be a list")
    by_name = {_object(item, "source state").get("name"): item for item in source_states}
    if set(by_name) != {f"ANT{index}" for index in range(1, 9)}:
        raise ValueError("source profile state set is not exactly ANT1 through ANT8")

    timer_hz = _positive_int(spec, "timer_hz")
    marker_us = _positive_int(spec, "marker_body_us")
    guard_us = _positive_int(spec, "all_off_guard_us")
    released_guard_us = _positive_int(spec, "released_transition_guard_us")
    dwell_us = _positive_int(spec, "antenna_dwell_us")
    maximum_lateness_us = _positive_int(spec, "maximum_lateness_us")
    if timer_hz != 1_000_000:
        raise ValueError("hexcal-v1 requires an exact nominal 1 MHz timer")
    if (marker_us, guard_us, dwell_us, maximum_lateness_us) != (180, 20, 200, 5):
        raise ValueError("hexcal-v1 timing contract changed without a profile revision")
    if (
        released_guard_us != 5_000
        or spec.get("released_contract_conformant") is not False
        or spec.get("protocol_status")
        != "experimental calibration-only guard waiver; does not supersede qualified Fast20"
    ):
        raise ValueError("hexcal-v1 must retain its explicit released-contract waiver")
    if maximum_lateness_us >= guard_us:
        raise ValueError("maximum lateness must be shorter than the ALL_OFF guard")

    states: list[dict[str, object]] = []
    selected_codes: set[str] = set()
    for name in order:
        source_state = _object(by_name[name], f"source state {name}")
        code = str(source_state["gpio_code_pa3_pa0"])
        if not re.fullmatch(r"[01]{4}", code):
            raise ValueError(f"source state {name} has an invalid GPIO code")
        if code in selected_codes or int(code, 2) == all_off_code:
            raise ValueError("source state codes are not unique from ALL_OFF")
        selected_codes.add(code)
        states.append(
            {
                "dwell_us": dwell_us,
                "gpio_code_pa3_pa0": code,
                "name": name,
                "window_us": [dwell_us * 0.95, dwell_us * 1.05],
            }
        )

    cycle_us = marker_us + len(states) * (guard_us + dwell_us)
    marker_observable_us = marker_us + guard_us
    if cycle_us != 1_500 or marker_observable_us != 200:
        raise ValueError("hexcal-v1 derived timing contract changed")
    source_clock = _object(source.get("clock"), "source clock")
    return {
        "array_order": {
            "direction": "clockwise",
            "forward_reference": "ANT1",
        },
        "clock": {
            **source_clock,
            "timer_nominal_hz": timer_hz,
            "timer_resolution_us": 1,
        },
        "contract_sha256": _sha256(SPEC_PATH),
        "decoder": {
            "accept": (
                "bounded ALL_OFF marker followed by six ordered equal-duration slots "
                "with a bounded ALL_OFF guard before every slot"
            ),
            "fundamental_limit": (
                "RF-only alignment is unavailable when neither the marker nor the "
                "guard transitions have usable contrast"
            ),
            "reject_to_unknown": [
                "no_observable_signal",
                "truncated_capture",
                "missed_or_extra_transition",
                "invalid_order",
                "no_valid_marker",
                "reset_or_watchdog_recovery",
            ],
            "sync": (
                "190..210us continuous ALL_OFF marker followed by ordered "
                "ANT1 through ANT6 slots"
            ),
        },
        "frame": {
            "all_off_guard_us": guard_us,
            "guard_window_us": [guard_us * 0.95, guard_us * 1.05],
            "guards_per_cycle": len(states),
            "marker": {
                "body_nominal_us": marker_us,
                "body_window_us": [marker_us * 0.95, marker_us * 1.05],
                "contiguous_pre_ANT1_guard_us": guard_us,
                "observable_nominal_us": marker_observable_us,
                "observable_window_us": [
                    marker_observable_us * 0.95,
                    marker_observable_us * 1.05,
                ],
                "state": "ALL_OFF",
            },
            "minimum_capture_for_guaranteed_complete_frame_us": cycle_us * 2,
            "nominal_cycle_us": cycle_us,
            "order": order,
            "recommended_capture_us": 2_000_000,
        },
        "generated_from": str(SPEC_PATH),
        "profile": {
            "id": str(spec.get("profile_id")),
            "revision": _positive_int(spec, "revision"),
        },
        "protocol": "framed_guarded_equal_dwell_hexcal_v1",
        "release_contract": {
            "released_transition_guard_us": released_guard_us,
            "profile_transition_guard_us": guard_us,
            "conformant": False,
            "status": str(spec["protocol_status"]),
        },
        "safety": {
            "all_off_code": f"{all_off_code:04b}",
            "maximum_deadline_lateness_us": maximum_lateness_us,
            "on_excessive_lateness": "apply ALL_OFF and restart the marker",
            "unused_states": ["ANT7", "ANT8"],
        },
        "schema": 1,
        "states": states,
        "time_unit": "microseconds",
    }


def _generated_header(profile: dict[str, Any], all_off_code: int) -> str:
    frame = _object(profile["frame"], "frame")
    marker = _object(frame["marker"], "marker")
    identity = _object(profile["profile"], "profile")
    safety = _object(profile["safety"], "safety")
    release_contract = _object(profile["release_contract"], "release contract")
    clock = _object(profile["clock"], "clock")
    states = profile["states"]
    if not isinstance(states, list):
        raise ValueError("generated states must be a list")
    lines = [
        "/* GENERATED by generate_hexcal_profile.py; do not edit. */",
        "#ifndef PLUTO_HEXCAL_CONTROL_PROFILE_H",
        "#define PLUTO_HEXCAL_CONTROL_PROFILE_H",
        "#include <stdint.h>",
        "",
        f'#define CONTROL_PROFILE_ID "{identity["id"]}"',
        f'#define CONTROL_PROFILE_REVISION {identity["revision"]}u',
        f'#define CONTROL_PROFILE_CONTRACT_SHA256 "{profile["contract_sha256"]}"',
        "#define CONTROL_EXPERIMENTAL_GUARD_WAIVER 1u",
        f'#define CONTROL_RELEASED_GUARD_US {release_contract["released_transition_guard_us"]}u',
        f"#define CONTROL_ALL_OFF_CODE 0x{all_off_code:X}u",
        f'#define CONTROL_TIMER_HZ {clock["timer_nominal_hz"]}u',
        f'#define CONTROL_GUARD_US {frame["all_off_guard_us"]}u',
        f'#define CONTROL_MARKER_BODY_US {marker["body_nominal_us"]}u',
        f'#define CONTROL_MAX_LATENESS_US {safety["maximum_deadline_lateness_us"]}u',
        f"#define CONTROL_STATE_COUNT {len(states)}u",
        f'#define CONTROL_NOMINAL_CYCLE_US {frame["nominal_cycle_us"]}u',
        "",
        "typedef struct { uint8_t gpio_code_pa3_pa0; uint16_t dwell_us; } "
        "control_step_us_t;",
        "static const control_step_us_t CONTROL_SCHEDULE[CONTROL_STATE_COUNT] = {",
    ]
    for state_value in states:
        state = _object(state_value, "state")
        lines.append(
            f'  {{ 0x{int(str(state["gpio_code_pa3_pa0"]), 2):X}u, '
            f'{state["dwell_us"]}u }}, /* {state["name"]} */'
        )
    lines.extend(("};", "", "#endif", ""))
    return "\n".join(lines)


def _outputs() -> dict[Path, str]:
    spec, source, source_path, all_off_code = _load_inputs()
    profile = _generated_profile(spec, source, all_off_code)
    profile_text = json.dumps(profile, indent=2, sort_keys=True) + "\n"
    header_text = _generated_header(profile, all_off_code)
    provenance = {
        "artifacts": {
            "control_profile.h": hashlib.sha256(header_text.encode()).hexdigest(),
            "control_profile.json": hashlib.sha256(profile_text.encode()).hexdigest(),
        },
        "schema": 1,
        "sources": {
            str(SPEC_PATH): _sha256(SPEC_PATH),
            str(source_path): _sha256(source_path),
            str(source_path.with_name("control_profile.h")): _sha256(
                source_path.with_name("control_profile.h")
            ),
        },
    }
    return {
        OUTPUT_DIRECTORY / "control_profile.h": header_text,
        OUTPUT_DIRECTORY / "control_profile.json": profile_text,
        OUTPUT_DIRECTORY / "provenance.json": json.dumps(provenance, indent=2, sort_keys=True)
        + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = _outputs()
    if args.write:
        for path, content in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        print("HEXCAL PROFILE WRITE: generated from qualified ANT1..ANT6 truth table")
        return 0
    stale = [
        str(path)
        for path, content in outputs.items()
        if not path.exists() or path.read_text() != content
    ]
    if stale:
        raise SystemExit("HEXCAL PROFILE STALE: " + ", ".join(stale))
    print("HEXCAL PROFILE PASS: generated artifacts and provenance exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
