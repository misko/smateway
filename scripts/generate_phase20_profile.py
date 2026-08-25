#!/usr/bin/env python3
"""Generate the equal-dwell phase profile from the qualified truth table."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SPEC_PATH = Path("profiles/phase20-v1/profile_spec.json")
OUTPUT_DIRECTORY = SPEC_PATH.parent
ALL_OFF_DEFINE = re.compile(
    r"^#define CONTROL_ALL_OFF_CODE (0x[0-9A-Fa-f]+)u$", re.MULTILINE
)


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
        raise ValueError("phase profile spec schema must be 1")
    source_path = Path(str(spec.get("source_profile", "")))
    source = _object(json.loads(source_path.read_text(encoding="utf-8")), "source profile")
    source_header = source_path.with_name("control_profile.h")
    match = ALL_OFF_DEFINE.search(source_header.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("source profile header does not define ALL_OFF")
    all_off_code = int(match.group(1), 16)
    return spec, source, source_path, all_off_code


def _generated_profile(
    spec: dict[str, Any], source: dict[str, Any], all_off_code: int
) -> dict[str, Any]:
    order = spec.get("state_order")
    if not isinstance(order, list) or order != [f"ANT{index}" for index in range(1, 9)]:
        raise ValueError("phase profile state order must be ANT1 through ANT8")
    source_states = source.get("states")
    if not isinstance(source_states, list):
        raise ValueError("source profile states must be a list")
    by_name = {_object(item, "source state").get("name"): item for item in source_states}
    if set(by_name) != set(order):
        raise ValueError("source profile state set is not exactly ANT1 through ANT8")

    marker_ms = _positive_int(spec, "marker_body_ms")
    guard_ms = _positive_int(spec, "all_off_guard_ms")
    dwell_ms = _positive_int(spec, "antenna_dwell_ms")
    if guard_ms != int(_object(source.get("frame"), "source frame")["all_off_guard_ms"]):
        raise ValueError("phase profile must preserve the qualified ALL_OFF guard")
    states = []
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
                "dwell_ms": dwell_ms,
                "gpio_code_pa3_pa0": code,
                "name": name,
                "window_ms": [dwell_ms * 0.95, dwell_ms * 1.05],
            }
        )

    cycle_ms = marker_ms + len(states) * (guard_ms + dwell_ms)
    marker_observable_ms = marker_ms + guard_ms
    return {
        "clock": _object(source.get("clock"), "source clock"),
        "contract_sha256": _sha256(SPEC_PATH),
        "decoder": {
            "accept": "ordered equal dwells aligned by complex state fingerprints",
            "fundamental_limit": (
                "equal RF states cannot be aligned when their complex fingerprints "
                "are statistically indistinguishable"
            ),
            "sync": (
                f"{marker_observable_ms}ms nominal ALL_OFF marker followed by "
                "ordered ANT1 through ANT8"
            ),
        },
        "frame": {
            "all_off_guard_ms": guard_ms,
            "guards_per_cycle": len(states),
            "marker": {
                "body_nominal_ms": marker_ms,
                "contiguous_pre_ANT1_guard_ms": guard_ms,
                "decoder_min_ms": marker_observable_ms * 0.9,
                "observable_nominal_ms": marker_observable_ms,
                "state": "ALL_OFF",
            },
            "minimum_capture_for_guaranteed_complete_frame_ms": cycle_ms * 2,
            "nominal_cycle_ms": cycle_ms,
            "order": order,
            "recommended_capture_ms": cycle_ms * 3,
        },
        "generated_from": str(SPEC_PATH),
        "profile": {
            "id": str(spec.get("profile_id")),
            "revision": _positive_int(spec, "revision"),
        },
        "protocol": "framed_guarded_equal_dwell_phase_v1",
        "schema": 1,
        "states": states,
    }


def _generated_header(profile: dict[str, Any], all_off_code: int) -> str:
    frame = _object(profile["frame"], "frame")
    marker = _object(frame["marker"], "marker")
    identity = _object(profile["profile"], "profile")
    states = profile["states"]
    if not isinstance(states, list):
        raise ValueError("generated states must be a list")
    lines = [
        "/* GENERATED by generate_phase20_profile.py; do not edit. */",
        "#ifndef PLUTO_PHASE20_CONTROL_PROFILE_H",
        "#define PLUTO_PHASE20_CONTROL_PROFILE_H",
        "#include <stdint.h>",
        "",
        f'#define CONTROL_PROFILE_ID "{identity["id"]}"',
        f'#define CONTROL_PROFILE_REVISION {identity["revision"]}u',
        f'#define CONTROL_PROFILE_CONTRACT_SHA256 "{profile["contract_sha256"]}"',
        f"#define CONTROL_ALL_OFF_CODE 0x{all_off_code:X}u",
        f'#define CONTROL_GUARD_MS {frame["all_off_guard_ms"]}u',
        f'#define CONTROL_MARKER_BODY_MS {marker["body_nominal_ms"]}u',
        f"#define CONTROL_STATE_COUNT {len(states)}u",
        f'#define CONTROL_NOMINAL_CYCLE_MS {frame["nominal_cycle_ms"]}u',
        "",
        "typedef struct { uint8_t gpio_code_pa3_pa0; uint16_t dwell_ms; } control_step_t;",
        "static const control_step_t CONTROL_SCHEDULE[CONTROL_STATE_COUNT] = {",
    ]
    for state_value in states:
        state = _object(state_value, "state")
        lines.append(
            f'  {{ 0x{int(str(state["gpio_code_pa3_pa0"]), 2):X}u, '
            f'{state["dwell_ms"]}u }}, /* {state["name"]} */'
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
        print("PHASE20 PROFILE WRITE: generated from qualified truth table")
        return 0
    stale = [
        str(path)
        for path, content in outputs.items()
        if not path.exists() or path.read_text() != content
    ]
    if stale:
        raise SystemExit("PHASE20 PROFILE STALE: " + ", ".join(stale))
    print("PHASE20 PROFILE PASS: generated artifacts and provenance exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
