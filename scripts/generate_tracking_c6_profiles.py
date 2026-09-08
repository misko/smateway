#!/usr/bin/env python3
"""Generate the bounded autonomous C6 tracking timing profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SPEC_PATH = Path("profiles/tracking-c6-timing-v1/profile_spec.json")
EXPECTED_SOURCE = Path("profiles/fast20-v1/control_profile.json")
EXPECTED_ORDER = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
EXPECTED_DWELLS_US = (25, 50, 100, 200)
LONG_SPEC_PATH = Path("profiles/tracking-c6-long-control-v1/profile_spec.json")
ALL_OFF_DEFINE = re.compile(r"^#define CONTROL_ALL_OFF_CODE (0x[0-9A-Fa-f]+)u$", re.MULTILINE)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _inputs(
    spec_path: Path = SPEC_PATH,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    long_control = spec_path == LONG_SPEC_PATH
    if spec_path not in (SPEC_PATH, LONG_SPEC_PATH):
        raise ValueError("unreviewed profile specification")
    spec = _object(json.loads(spec_path.read_text(encoding="utf-8")), "spec")
    family = "tracking-c6-long-control-v1" if long_control else "tracking-c6-timing-v1"
    if spec.get("schema") != 1 or spec.get("family_id") != family:
        raise ValueError("tracking C6 profile identity changed")
    source_path = Path(str(spec.get("source_profile", "")))
    if source_path != EXPECTED_SOURCE:
        raise ValueError("tracking C6 profiles must derive from fast20-v1")
    source = _object(json.loads(source_path.read_text(encoding="utf-8")), "source")
    match = ALL_OFF_DEFINE.search(
        source_path.with_name("control_profile.h").read_text(encoding="utf-8")
    )
    if match is None:
        raise ValueError("source profile does not define ALL_OFF")
    if tuple(spec.get("state_order", ())) != EXPECTED_ORDER:
        raise ValueError("tracking C6 physical order changed")
    if tuple(spec.get("antenna_dwell_us", ())) != ((1000,) if long_control else EXPECTED_DWELLS_US):
        raise ValueError("tracking C6 dwell grid changed")
    if long_control and spec.get("watchdog_refresh_opportunities") != 2:
        raise ValueError("long control watchdog proof differs")
    if (
        spec.get("revision") != 1
        or spec.get("timer_hz") != 1_000_000
        or spec.get("marker_body_us") != 180
        or spec.get("all_off_guard_us") != 20
        or spec.get("released_transition_guard_us") != 5_000
        or spec.get("released_contract_conformant") is not False
        or spec.get("maximum_lateness_us") != 5
        or spec.get("physical_order") != "clockwise"
        or spec.get("forward_reference") != "ANT1"
    ):
        raise ValueError("tracking C6 timing contract changed without a revision")
    return spec, source, int(match.group(1), 16)


def _profile(
    spec: dict[str, Any],
    source: dict[str, Any],
    all_off_code: int,
    dwell_us: int,
    *,
    spec_path: Path = SPEC_PATH,
) -> dict[str, Any]:
    source_states = source.get("states")
    if not isinstance(source_states, list):
        raise ValueError("source states must be a list")
    by_name = {
        str(_object(item, "source state")["name"]): _object(item, "source state")
        for item in source_states
    }
    states: list[dict[str, object]] = []
    seen_codes: set[str] = set()
    for name in EXPECTED_ORDER:
        source_state = by_name[name]
        code = str(source_state["gpio_code_pa3_pa0"])
        if not re.fullmatch(r"[01]{4}", code):
            raise ValueError(f"source state {name} has an invalid GPIO code")
        if code in seen_codes or int(code, 2) == all_off_code:
            raise ValueError("C6 state codes are not unique from ALL_OFF")
        seen_codes.add(code)
        states.append(
            {
                "dwell_us": dwell_us,
                "gpio_code_pa3_pa0": code,
                "name": name,
                "window_us": [0.95 * dwell_us, 1.05 * dwell_us],
            }
        )
    guard_us = int(spec["all_off_guard_us"])
    marker_us = int(spec["marker_body_us"])
    cycle_us = marker_us + len(states) * (guard_us + dwell_us)
    identity = f"tracking-c6-{dwell_us}us-v1"
    contract_sha256 = _sha256_bytes(spec_path.read_bytes() + b"\0" + str(dwell_us).encode("ascii"))
    return {
        "array_order": {
            "direction": "clockwise",
            "forward_reference": "ANT1",
        },
        "clock": {
            **_object(source["clock"], "source clock"),
            "timer_nominal_hz": int(spec["timer_hz"]),
            "timer_resolution_us": 1,
        },
        "contract_sha256": contract_sha256,
        "decoder": {
            "accept": (
                "bounded ALL_OFF marker followed by six clockwise C6 slots with "
                "an ALL_OFF guard before every slot"
            ),
            "sync": (
                f"{marker_us + guard_us} us nominal continuous ALL_OFF marker, then "
                f"{dwell_us} us ANT1, ANT2, ANT4, ANT8, ANT7, ANT5 slots"
            ),
        },
        "frame": {
            "all_off_guard_us": guard_us,
            "guard_window_us": [0.95 * guard_us, 1.05 * guard_us],
            "guards_per_cycle": len(states),
            "marker": {
                "body_nominal_us": marker_us,
                "contiguous_pre_ANT1_guard_us": guard_us,
                "observable_nominal_us": marker_us + guard_us,
                "state": "ALL_OFF",
            },
            "minimum_capture_for_guaranteed_complete_frame_us": 2 * cycle_us,
            "nominal_cycle_us": cycle_us,
            "order": list(EXPECTED_ORDER),
            "recommended_capture_us": 2_000_000,
        },
        "generated_from": str(spec_path),
        "profile": {"id": identity, "revision": int(spec["revision"])},
        "protocol": "framed_guarded_equal_dwell_tracking_c6_timing_v1",
        "release_contract": {
            "conformant": False,
            "profile_transition_guard_us": guard_us,
            "released_transition_guard_us": int(spec["released_transition_guard_us"]),
            "status": str(spec["protocol_status"]),
        },
        "safety": {
            "all_off_code": f"{all_off_code:04b}",
            "maximum_deadline_lateness_us": int(spec["maximum_lateness_us"]),
            "on_excessive_lateness": "apply ALL_OFF and restart the marker",
            "unused_states": ["ANT3", "ANT6"],
            **({"watchdog_refresh_opportunities": 2} if spec_path == LONG_SPEC_PATH else {}),
        },
        "schema": 1,
        "states": states,
        "time_unit": "microseconds",
    }


def _header(profile: dict[str, Any], all_off_code: int) -> str:
    identity = _object(profile["profile"], "profile identity")
    frame = _object(profile["frame"], "frame")
    marker = _object(frame["marker"], "marker")
    safety = _object(profile["safety"], "safety")
    states = profile["states"]
    if not isinstance(states, list):
        raise ValueError("profile states must be a list")
    lines = [
        "/* GENERATED by generate_tracking_c6_profiles.py; do not edit. */",
        "#ifndef PLUTO_TRACKING_C6_CONTROL_PROFILE_H",
        "#define PLUTO_TRACKING_C6_CONTROL_PROFILE_H",
        "#include <stdint.h>",
        "",
        f'#define CONTROL_PROFILE_ID "{identity["id"]}"',
        f"#define CONTROL_PROFILE_REVISION {identity['revision']}u",
        f'#define CONTROL_PROFILE_CONTRACT_SHA256 "{profile["contract_sha256"]}"',
        "#define CONTROL_EXPERIMENTAL_GUARD_WAIVER 1u",
        "#define CONTROL_RELEASED_GUARD_US 5000u",
        f"#define CONTROL_ALL_OFF_CODE 0x{all_off_code:X}u",
        "#define CONTROL_TIMER_HZ 1000000u",
        f"#define CONTROL_GUARD_US {frame['all_off_guard_us']}u",
        f"#define CONTROL_MARKER_BODY_US {marker['body_nominal_us']}u",
        f"#define CONTROL_MAX_LATENESS_US {safety['maximum_deadline_lateness_us']}u",
        f"#define CONTROL_STATE_COUNT {len(states)}u",
        f"#define CONTROL_NOMINAL_CYCLE_US {frame['nominal_cycle_us']}u",
        "",
        "typedef struct { uint8_t gpio_code_pa3_pa0; uint16_t dwell_us; } control_step_us_t;",
        "static const control_step_us_t CONTROL_SCHEDULE[CONTROL_STATE_COUNT] = {",
    ]
    for value in states:
        state = _object(value, "state")
        lines.append(
            f"  {{ 0x{int(str(state['gpio_code_pa3_pa0']), 2):X}u, "
            f"{state['dwell_us']}u }}, /* {state['name']} */"
        )
    lines.extend(("};", "", "#endif", ""))
    if "watchdog_refresh_opportunities" in safety:
        lines.insert(-2, "#define CONTROL_LONG_CONTROL_REFRESH_OPPORTUNITIES 2u")
    return "\n".join(lines)


def _family_outputs(spec_path: Path) -> dict[Path, str]:
    spec, source, all_off_code = _inputs(spec_path)
    outputs: dict[Path, str] = {}
    for dwell_us in spec["antenna_dwell_us"]:
        profile = _profile(spec, source, all_off_code, dwell_us, spec_path=spec_path)
        directory = Path(f"profiles/tracking-c6-{dwell_us}us-v1")
        profile_text = json.dumps(profile, indent=2, sort_keys=True) + "\n"
        header_text = _header(profile, all_off_code)
        provenance = {
            "artifacts": {
                "control_profile.h": _sha256_bytes(header_text.encode()),
                "control_profile.json": _sha256_bytes(profile_text.encode()),
            },
            "schema": 1,
            "sources": {
                str(spec_path): _sha256(spec_path),
                str(EXPECTED_SOURCE): _sha256(EXPECTED_SOURCE),
                str(EXPECTED_SOURCE.with_name("control_profile.h")): _sha256(
                    EXPECTED_SOURCE.with_name("control_profile.h")
                ),
            },
        }
        outputs[directory / "control_profile.h"] = header_text
        outputs[directory / "control_profile.json"] = profile_text
        outputs[directory / "provenance.json"] = (
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
    return outputs


def _outputs() -> dict[Path, str]:
    return {**_family_outputs(SPEC_PATH), **_family_outputs(LONG_SPEC_PATH)}


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
        print("TRACKING C6 PROFILE WRITE: generated 25/50/100/200/1000 us profiles")
        return 0
    stale = [
        str(path)
        for path, content in outputs.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if stale:
        raise SystemExit("TRACKING C6 PROFILE STALE: " + ", ".join(stale))
    print("TRACKING C6 PROFILE PASS: all generated artifacts are exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
