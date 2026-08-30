"""Pure contracts for the source-bound 5.60--5.95 GHz fine sweep.

This module deliberately has no radio, IIO, filesystem, or wall-clock side
effects.  The runner freezes schedules and identities with these helpers; the
analyzer consumes only already admitted observations.  Ascending and
descending observations remain separate unless a simultaneous direction test
proves them equivalent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import product
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from smateway import global_ledger

SweepDirection = Literal["ascending", "descending"]
SweepMode = Literal["coarse", "fine"]
VisitRole = Literal["primary", "interleaved_anchor"]

COARSE_MINIMUM_HZ = 5_600_000_000
COARSE_MAXIMUM_HZ = 5_950_000_000
COARSE_STEP_HZ = 10_000_000
FINE_STEP_HZ = 1_000_000
FINE_HALF_SPAN_HZ = 10_000_000
ANCHOR_FREQUENCY_HZ = 5_800_000_000
ANCHOR_CADENCE_NON_ANCHOR_VISITS = 5
REPEATS_PER_VISIT = 5
DIRECTIONS: tuple[SweepDirection, SweepDirection] = ("ascending", "descending")

SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
TOTAL_SAMPLES = 300_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 3
KERNEL_BUFFERS = 8
RECEIVER_COUNT = 2
CI16_BYTES_PER_COMPLEX_SAMPLE = 4
BYTES_PER_CAPTURE = TOTAL_SAMPLES * RECEIVER_COUNT * CI16_BYTES_PER_COMPLEX_SAMPLE
STORAGE_HEADROOM_MULTIPLIER = 2

TX_CHANNEL = 0
TX_HARDWARE_GAIN_DB = -20.0
DDS_SCALE = 0.125
RECEIVER_GAIN_DB = 60.0
DDS_READBACK_TOLERANCE_HZ = math.ceil(SAMPLE_RATE_HZ / (1 << 16))
DIRECTION_EQUIVALENCE_DB = 0.2
DIRECTION_EQUIVALENCE_PHASE_DEG = 2.0
SIMULTANEOUS_CONFIDENCE = 0.95
EXPERIMENTAL_POLICY = "experimental_conducted_5g6_to_5g95_policy_reviewed"
EXPERIMENTAL_WARNING = (
    "5.60--5.95 GHz operation is experimental and may exceed the qualified range of "
    "the fitted Pluto RF silicon; results do not establish production qualification"
)

TOPOLOGY_TOKENS: dict[str, str] = {
    "direct_rx2_termination": "DIRECT_RX2_50OHM_AT_PLUTO",
    "rx2_cable_terminated": "RX2_CABLE_FAR_END_50OHM",
    "powered_selector_all_inputs_terminated": ("POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM"),
    "full_conducted_fixture": "FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2",
}
SELECTOR_CONNECTED_TOPOLOGIES = frozenset(
    {"powered_selector_all_inputs_terminated", "full_conducted_fixture"}
)
CAMPAIGN_BINDING_KIND = "5g8_frequency_campaign_cross_binding_v1"
DEVICE_URI_REOBSERVATION_POLICY = "fresh_usb_uri_allowed_only_for_same_serial"


class FineFrequencyError(ValueError):
    """Evidence cannot satisfy the frozen fine-frequency contract."""


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise FineFrequencyError(f"{label} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise FineFrequencyError(f"{label} must be finite")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) <= 0:
        raise FineFrequencyError(f"{label} must be a positive integer")
    return int(value)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160 or not value[0].isalnum():
        raise FineFrequencyError(f"{label} must be a nonempty bounded string")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    ):
        raise FineFrequencyError(f"{label} contains unsafe characters")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FineFrequencyError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _complex_document(value: complex) -> dict[str, float]:
    real = float(value.real)
    imag = float(value.imag)
    if not math.isfinite(real) or not math.isfinite(imag):
        raise FineFrequencyError("complex value must be finite")
    return {"real": real, "imag": imag}


def _json_normalize(value: object) -> Any:
    if isinstance(value, complex):
        return _complex_document(value)
    if isinstance(value, Mapping):
        return {str(key): _json_normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_normalize(item) for item in value]
    return value


def _json_safe(value: object, label: str = "value") -> Any:
    try:
        wire = json.dumps(
            _json_normalize(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(wire)
    except (TypeError, ValueError) as error:
        raise FineFrequencyError(f"{label} must be finite canonical JSON") from error


def canonical_json_sha256(value: object) -> str:
    """Return the stable SHA-256 used by immutable plans and result bindings."""

    normalized = _json_safe(value)
    wire = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(wire).hexdigest()


def classify_center_frequency(frequency_hz: object, *, grid_step_hz: int) -> str:
    """Admit one reviewed experimental center on the requested integer grid."""

    frequency = _positive_int(frequency_hz, "center frequency")
    if grid_step_hz not in (COARSE_STEP_HZ, FINE_STEP_HZ):
        raise FineFrequencyError("grid step must be exactly 10 MHz or 1 MHz")
    if not COARSE_MINIMUM_HZ <= frequency <= COARSE_MAXIMUM_HZ:
        raise FineFrequencyError("center frequency is outside 5.60--5.95 GHz")
    if (frequency - COARSE_MINIMUM_HZ) % grid_step_hz:
        raise FineFrequencyError("center frequency is not on the frozen sweep grid")
    return EXPERIMENTAL_POLICY


@dataclass(frozen=True, slots=True)
class SweepVisit:
    """One frequency visit; five source-distinct captures belong to each visit."""

    refinement_id: str
    refinement_center_hz: int | None
    direction: SweepDirection
    visit_index: int
    frequency_hz: int
    role: VisitRole
    primary_index: int | None
    anchor_group_index: int | None


@dataclass(frozen=True, slots=True)
class SweepCondition:
    """One immutable fresh-stream capture condition."""

    plan_index: int
    condition_id: str
    refinement_id: str
    refinement_center_hz: int | None
    direction: SweepDirection
    visit_index: int
    frequency_hz: int
    role: VisitRole
    primary_index: int | None
    anchor_group_index: int | None
    repeat_index: int


@dataclass(frozen=True, slots=True)
class SweepSchedule:
    """Complete coarse plan or one immutable collection of fine refinements."""

    mode: SweepMode
    refinement_centers_hz: tuple[int, ...]
    primary_frequencies_by_refinement: Mapping[str, tuple[int, ...]]
    visits: tuple[SweepVisit, ...]
    conditions: tuple[SweepCondition, ...]


def _inclusive_grid(minimum_hz: int, maximum_hz: int, step_hz: int) -> tuple[int, ...]:
    if minimum_hz > maximum_hz or (maximum_hz - minimum_hz) % step_hz:
        raise FineFrequencyError("frequency endpoints do not define an inclusive grid")
    return tuple(range(minimum_hz, maximum_hz + step_hz, step_hz))


def _interleaved_visits(
    primary: Sequence[int],
    *,
    refinement_id: str,
    refinement_center_hz: int | None,
    direction: SweepDirection,
) -> tuple[SweepVisit, ...]:
    ordered = tuple(primary if direction == "ascending" else reversed(primary))
    if len(set(ordered)) != len(ordered):
        raise FineFrequencyError("primary frequency grid contains duplicates")
    output: list[SweepVisit] = []
    non_anchor_since_marker = 0
    anchor_group_index = 0
    for primary_index, frequency_hz in enumerate(ordered):
        output.append(
            SweepVisit(
                refinement_id=refinement_id,
                refinement_center_hz=refinement_center_hz,
                direction=direction,
                visit_index=len(output),
                frequency_hz=frequency_hz,
                role="primary",
                primary_index=primary_index,
                anchor_group_index=None,
            )
        )
        if frequency_hz == ANCHOR_FREQUENCY_HZ:
            continue
        non_anchor_since_marker += 1
        if non_anchor_since_marker == ANCHOR_CADENCE_NON_ANCHOR_VISITS:
            anchor_group_index += 1
            output.append(
                SweepVisit(
                    refinement_id=refinement_id,
                    refinement_center_hz=refinement_center_hz,
                    direction=direction,
                    visit_index=len(output),
                    frequency_hz=ANCHOR_FREQUENCY_HZ,
                    role="interleaved_anchor",
                    primary_index=None,
                    anchor_group_index=anchor_group_index,
                )
            )
            non_anchor_since_marker = 0
    return tuple(output)


def _conditions(
    visits: Sequence[SweepVisit], *, start_index: int = 0
) -> tuple[SweepCondition, ...]:
    output: list[SweepCondition] = []
    for visit in visits:
        for repeat_index in range(1, REPEATS_PER_VISIT + 1):
            role = "primary" if visit.role == "primary" else f"anchor{visit.anchor_group_index}"
            condition_id = (
                f"{visit.refinement_id}-{visit.direction}-v{visit.visit_index:03d}-"
                f"{role}-{visit.frequency_hz}-r{repeat_index}"
            )
            output.append(
                SweepCondition(
                    plan_index=start_index + len(output),
                    condition_id=condition_id,
                    refinement_id=visit.refinement_id,
                    refinement_center_hz=visit.refinement_center_hz,
                    direction=visit.direction,
                    visit_index=visit.visit_index,
                    frequency_hz=visit.frequency_hz,
                    role=visit.role,
                    primary_index=visit.primary_index,
                    anchor_group_index=visit.anchor_group_index,
                    repeat_index=repeat_index,
                )
            )
    return tuple(output)


def build_coarse_schedule() -> SweepSchedule:
    """Build 5.60--5.95 GHz/10 MHz ascending and descending schedules."""

    primary = _inclusive_grid(COARSE_MINIMUM_HZ, COARSE_MAXIMUM_HZ, COARSE_STEP_HZ)
    visits: list[SweepVisit] = []
    for direction in DIRECTIONS:
        visits.extend(
            _interleaved_visits(
                primary,
                refinement_id="coarse",
                refinement_center_hz=None,
                direction=direction,
            )
        )
    schedule = SweepSchedule(
        mode="coarse",
        refinement_centers_hz=(),
        primary_frequencies_by_refinement={"coarse": primary},
        visits=tuple(visits),
        conditions=_conditions(visits),
    )
    validate_schedule(schedule)
    return schedule


def build_fine_schedule(refinement_centers_hz: Sequence[int]) -> SweepSchedule:
    """Build 1 MHz/plus-or-minus-10 MHz strata for selected coarse extrema."""

    centers = tuple(
        _positive_int(value, "fine refinement center") for value in refinement_centers_hz
    )
    if not centers or len(centers) > 2 or len(set(centers)) != len(centers):
        raise FineFrequencyError("fine refinement centers must contain one or two unique integers")
    primary_by_refinement: dict[str, tuple[int, ...]] = {}
    visits: list[SweepVisit] = []
    conditions: list[SweepCondition] = []
    for center in centers:
        classify_center_frequency(center, grid_step_hz=COARSE_STEP_HZ)
        minimum = center - FINE_HALF_SPAN_HZ
        maximum = center + FINE_HALF_SPAN_HZ
        if minimum < COARSE_MINIMUM_HZ or maximum > COARSE_MAXIMUM_HZ:
            raise FineFrequencyError("fine refinement interval leaves 5.60--5.95 GHz policy")
        refinement_id = f"fine-{center}"
        primary = _inclusive_grid(minimum, maximum, FINE_STEP_HZ)
        primary_by_refinement[refinement_id] = primary
        refinement_visits: list[SweepVisit] = []
        for direction in DIRECTIONS:
            refinement_visits.extend(
                _interleaved_visits(
                    primary,
                    refinement_id=refinement_id,
                    refinement_center_hz=center,
                    direction=direction,
                )
            )
        visits.extend(refinement_visits)
        conditions.extend(_conditions(refinement_visits, start_index=len(conditions)))
    schedule = SweepSchedule(
        mode="fine",
        refinement_centers_hz=centers,
        primary_frequencies_by_refinement=primary_by_refinement,
        visits=tuple(visits),
        conditions=tuple(conditions),
    )
    validate_schedule(schedule)
    return schedule


def validate_schedule(schedule: SweepSchedule) -> None:
    """Reject endpoint, cadence, ordering, duplication, or condition-count drift."""

    if schedule.mode not in ("coarse", "fine"):
        raise FineFrequencyError("unsupported sweep mode")
    expected_step = COARSE_STEP_HZ if schedule.mode == "coarse" else FINE_STEP_HZ
    if schedule.mode == "coarse":
        expected_primary_by_refinement = {
            "coarse": _inclusive_grid(
                COARSE_MINIMUM_HZ,
                COARSE_MAXIMUM_HZ,
                COARSE_STEP_HZ,
            )
        }
        if (
            schedule.refinement_centers_hz
            or dict(schedule.primary_frequencies_by_refinement) != expected_primary_by_refinement
        ):
            raise FineFrequencyError("coarse endpoints/grid differ from 5.60--5.95 GHz")
    else:
        centers = schedule.refinement_centers_hz
        if not centers or len(centers) > 2 or len(set(centers)) != len(centers):
            raise FineFrequencyError("fine schedule has invalid refinement centers")
        expected_primary_by_refinement = {}
        for center in centers:
            classify_center_frequency(center, grid_step_hz=COARSE_STEP_HZ)
            minimum = center - FINE_HALF_SPAN_HZ
            maximum = center + FINE_HALF_SPAN_HZ
            if minimum < COARSE_MINIMUM_HZ or maximum > COARSE_MAXIMUM_HZ:
                raise FineFrequencyError("fine refinement interval leaves reviewed policy")
            expected_primary_by_refinement[f"fine-{center}"] = _inclusive_grid(
                minimum,
                maximum,
                FINE_STEP_HZ,
            )
        if dict(schedule.primary_frequencies_by_refinement) != expected_primary_by_refinement:
            raise FineFrequencyError("fine endpoints/grid differ from the frozen intervals")
    condition_ids: set[str] = set()
    condition_indices: set[int] = set()
    visits_by_key: dict[tuple[str, SweepDirection], list[SweepVisit]] = defaultdict(list)
    for visit in schedule.visits:
        classify_center_frequency(visit.frequency_hz, grid_step_hz=FINE_STEP_HZ)
        visits_by_key[(visit.refinement_id, visit.direction)].append(visit)
    expected_visit_keys = {
        (refinement_id, direction)
        for refinement_id in schedule.primary_frequencies_by_refinement
        for direction in DIRECTIONS
    }
    if set(visits_by_key) != expected_visit_keys:
        raise FineFrequencyError("schedule is missing a direction/refinement stratum")
    for (refinement_id, direction), visits in visits_by_key.items():
        primary = schedule.primary_frequencies_by_refinement[refinement_id]
        expected_primary = tuple(primary if direction == "ascending" else reversed(primary))
        observed_primary = tuple(visit.frequency_hz for visit in visits if visit.role == "primary")
        if observed_primary != expected_primary:
            raise FineFrequencyError("primary endpoints/order differ from the frozen grid")
        expected_center = (
            None if schedule.mode == "coarse" else int(refinement_id.removeprefix("fine-"))
        )
        expected_visits = _interleaved_visits(
            primary,
            refinement_id=refinement_id,
            refinement_center_hz=expected_center,
            direction=direction,
        )
        if tuple(visits) != expected_visits:
            raise FineFrequencyError("visit order/anchor cadence differs from the frozen schedule")
        if any((frequency - COARSE_MINIMUM_HZ) % expected_step for frequency in primary):
            raise FineFrequencyError("primary frequency is off the mode grid")
        if [visit.visit_index for visit in visits] != list(range(len(visits))):
            raise FineFrequencyError("visit indices are not contiguous within a stratum")
        non_anchor_count = 0
        expected_anchor_group = 0
        for visit in visits:
            if visit.role == "interleaved_anchor":
                if (
                    visit.frequency_hz != ANCHOR_FREQUENCY_HZ
                    or non_anchor_count != ANCHOR_CADENCE_NON_ANCHOR_VISITS
                ):
                    raise FineFrequencyError("interleaved anchor cadence/frequency is invalid")
                expected_anchor_group += 1
                if visit.anchor_group_index != expected_anchor_group:
                    raise FineFrequencyError("interleaved anchor group numbering is invalid")
                non_anchor_count = 0
            elif visit.frequency_hz != ANCHOR_FREQUENCY_HZ:
                non_anchor_count += 1
                if non_anchor_count > ANCHOR_CADENCE_NON_ANCHOR_VISITS:
                    raise FineFrequencyError("missing interleaved anchor")
    condition_groups: dict[tuple[str, SweepDirection, int], list[SweepCondition]] = defaultdict(
        list
    )
    for condition in schedule.conditions:
        if condition.condition_id in condition_ids:
            raise FineFrequencyError("condition IDs are duplicated")
        if condition.plan_index in condition_indices:
            raise FineFrequencyError("condition plan indices are duplicated")
        condition_ids.add(condition.condition_id)
        condition_indices.add(condition.plan_index)
        condition_groups[
            (condition.refinement_id, condition.direction, condition.visit_index)
        ].append(condition)
    if condition_indices != set(range(len(schedule.conditions))):
        raise FineFrequencyError("condition indices are not exactly contiguous")
    visit_identity = {
        (visit.refinement_id, visit.direction, visit.visit_index): visit
        for visit in schedule.visits
    }
    if set(condition_groups) != set(visit_identity):
        raise FineFrequencyError("conditions do not cover every visit exactly")
    for key, grouped in condition_groups.items():
        visit = visit_identity[key]
        if sorted(condition.repeat_index for condition in grouped) != list(
            range(1, REPEATS_PER_VISIT + 1)
        ):
            raise FineFrequencyError("visit does not contain exactly five distinct repeats")
        for condition in grouped:
            comparable = (
                condition.refinement_center_hz,
                condition.frequency_hz,
                condition.role,
                condition.primary_index,
                condition.anchor_group_index,
            )
            expected = (
                visit.refinement_center_hz,
                visit.frequency_hz,
                visit.role,
                visit.primary_index,
                visit.anchor_group_index,
            )
            if comparable != expected:
                raise FineFrequencyError("condition differs from its frozen visit")
    if schedule.conditions != _conditions(schedule.visits):
        raise FineFrequencyError("condition IDs/order differ from the frozen schedule")


def schedule_document(schedule: SweepSchedule) -> dict[str, Any]:
    """Return the canonical JSON representation frozen in a plan."""

    validate_schedule(schedule)
    return {
        "schema": 1,
        "mode": schedule.mode,
        "refinement_centers_hz": list(schedule.refinement_centers_hz),
        "primary_frequencies_by_refinement": {
            key: list(value)
            for key, value in sorted(schedule.primary_frequencies_by_refinement.items())
        },
        "visits": [asdict(visit) for visit in schedule.visits],
        "conditions": [asdict(condition) for condition in schedule.conditions],
    }


def schedule_from_document(value: object) -> SweepSchedule:
    """Rebuild only a byte-for-byte frozen schedule; reject handcrafted variants."""

    if not isinstance(value, Mapping):
        raise FineFrequencyError("plan schedule must be an object")
    normalized = _json_safe(dict(value), "plan schedule")
    if not isinstance(normalized, dict) or normalized.get("schema") != 1:
        raise FineFrequencyError("plan schedule schema is invalid")
    mode = normalized.get("mode")
    raw_centers = normalized.get("refinement_centers_hz")
    if not isinstance(raw_centers, list):
        raise FineFrequencyError("plan refinement centers are malformed")
    centers = tuple(_positive_int(item, "refinement center") for item in raw_centers)
    if mode == "coarse":
        if centers:
            raise FineFrequencyError("coarse schedule cannot contain refinement centers")
        schedule = build_coarse_schedule()
    elif mode == "fine":
        schedule = build_fine_schedule(centers)
    else:
        raise FineFrequencyError("plan schedule mode is invalid")
    if normalized != schedule_document(schedule):
        raise FineFrequencyError("plan schedule differs from the generated frozen schedule")
    return schedule


def storage_contract(schedule: SweepSchedule, *, free_bytes: object) -> dict[str, Any]:
    """Freeze exact raw bytes/time and require at least twice that capacity locally."""

    free = _positive_int(free_bytes, "free local bytes")
    raw = len(schedule.conditions) * BYTES_PER_CAPTURE
    required = raw * STORAGE_HEADROOM_MULTIPLIER
    capture_seconds = len(schedule.conditions) * TOTAL_SAMPLES / SAMPLE_RATE_HZ
    minimum_settle_seconds = len(schedule.conditions) * 0.100
    if free < required:
        raise FineFrequencyError(
            f"local free space {free} is below the frozen two-times requirement {required}"
        )
    return {
        "medium": "raspberry_pi_local_filesystem",
        "pluto_onboard_storage_used": False,
        "condition_count": len(schedule.conditions),
        "sample_count_per_condition": TOTAL_SAMPLES,
        "receiver_count": RECEIVER_COUNT,
        "wire_format": "ci16_le",
        "bytes_per_condition": BYTES_PER_CAPTURE,
        "estimated_raw_iq_bytes": raw,
        "required_free_bytes": required,
        "observed_free_bytes_at_plan": free,
        "free_space_multiplier": STORAGE_HEADROOM_MULTIPLIER,
        "capture_duration_s": capture_seconds,
        "minimum_settle_duration_s": minimum_settle_seconds,
        "minimum_rf_runtime_s": capture_seconds + minimum_settle_seconds,
        "runtime_excludes_usb_and_filesystem_overhead": True,
    }


def _identity_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise FineFrequencyError(f"{label} must be a nonempty object")
    normalized = _json_safe(dict(value), label)
    if not isinstance(normalized, dict):
        raise FineFrequencyError(f"{label} must be an object")
    return normalized


def _validate_file_identity_document(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "size_bytes"}:
        raise FineFrequencyError(f"{label} file identity is incomplete or unexpected")
    path = value.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise FineFrequencyError(f"{label} file identity path must be absolute")
    _sha256(value.get("sha256"), f"{label} file hash")
    _positive_int(value.get("size_bytes"), f"{label} file size")
    return dict(value)


def _validate_selector_flash_binding(
    value: object,
    *,
    campaign_id: str,
    board_id: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "binding_kind",
        "path",
        "sha256",
        "campaign_id",
        "run_id",
        "board_id",
        "image_role",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FineFrequencyError("selector flash binding is incomplete or unexpected")
    path = value.get("path")
    if (
        value.get("schema") != 1
        or value.get("binding_kind") != "sealed_selector_flash_evidence_v1"
        or not isinstance(path, str)
        or not Path(path).is_absolute()
        or value.get("campaign_id") != campaign_id
        or value.get("board_id") != board_id
        or value.get("image_role") != "bench"
    ):
        raise FineFrequencyError("selector flash binding is not the sealed bench image")
    _identifier(value.get("run_id"), "selector flash run ID")
    _sha256(value.get("sha256"), "selector flash evidence hash")
    return dict(value)


def _validate_selector_control_identity(
    value: object,
    *,
    flash: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "mode",
        "bench_manifest",
        "openocd_config",
        "control_profile",
        "command",
        "selector_flash_evidence",
        "target_image_admission_contract",
    }:
        raise FineFrequencyError("selector-control identity is incomplete or unexpected")
    bench = value.get("bench_manifest")
    config = value.get("openocd_config")
    profile = value.get("control_profile")
    command = value.get("command")
    target = value.get("target_image_admission_contract")
    if (
        value.get("schema") != 1
        or value.get("mode") != "reviewed_static_selector_mailbox_all_off"
        or not isinstance(bench, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(profile, Mapping)
        or not isinstance(command, Mapping)
        or not isinstance(target, Mapping)
        or value.get("selector_flash_evidence") != flash
    ):
        raise FineFrequencyError("selector-control identity is not reviewed static ALL_OFF")
    if (
        set(bench)
        != {
            "path",
            "file_sha256",
            "elf_sha256",
            "mailbox_address",
            "mailbox_size",
            "mailbox_magic",
            "mailbox_version",
            "max_lease_ms",
            "mailbox_offsets",
        }
        or set(config) != {"path", "file_sha256"}
        or set(profile)
        != {
            "path",
            "file_sha256",
            "header_path",
            "header_file_sha256",
            "profile_id",
            "revision",
            "contract_sha256",
            "all_off_code",
        }
    ):
        raise FineFrequencyError("selector-control artifact identities are malformed")
    for section, path_name, hash_name, label in (
        (bench, "path", "file_sha256", "bench manifest"),
        (config, "path", "file_sha256", "OpenOCD config"),
        (profile, "path", "file_sha256", "control profile"),
        (profile, "header_path", "header_file_sha256", "control profile header"),
    ):
        path = section.get(path_name)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise FineFrequencyError(f"{label} path must be absolute")
        _sha256(section.get(hash_name), f"{label} hash")
    _sha256(bench.get("elf_sha256"), "bench ELF hash")
    _sha256(profile.get("contract_sha256"), "control profile contract hash")
    if (
        set(command) != {"code", "lease_ms", "wait_until_applied", "readback_required"}
        or command.get("code") != profile.get("all_off_code")
        or command.get("lease_ms") != 0
        or command.get("wait_until_applied") is not True
        or command.get("readback_required") is not True
    ):
        raise FineFrequencyError("selector control does not require static ALL_OFF readback")
    target_path = target.get("firmware_bin_path")
    target_size = target.get("firmware_bin_size_bytes")
    if (
        set(target)
        != {
            "schema",
            "flash_base_address",
            "firmware_bin_path",
            "firmware_bin_sha256",
            "firmware_bin_size_bytes",
            "board_id",
            "selector_flash_evidence_sha256",
            "full_bin_extent_and_uid_required_before_mailbox",
        }
        or target.get("schema") != 1
        or target.get("flash_base_address") != 0x08000000
        or not isinstance(target_path, str)
        or not Path(target_path).is_absolute()
        or isinstance(target_size, bool)
        or not isinstance(target_size, int)
        or target_size <= 0
        or target.get("board_id") != flash.get("board_id")
        or target.get("selector_flash_evidence_sha256") != flash.get("sha256")
        or target.get("full_bin_extent_and_uid_required_before_mailbox") is not True
    ):
        raise FineFrequencyError("selector target-image admission contract is malformed")
    _sha256(target.get("firmware_bin_sha256"), "selector firmware BIN hash")
    return dict(value)


def _validate_fixture_plan_identity(
    value: object,
    *,
    run_id: str,
    board_id: str,
) -> dict[str, Any]:
    """Validate the immutable T7 wrapper around normalized fixture-v2 evidence."""

    document = _identity_object(value, "fixture identity")
    if set(document) != {
        "schema",
        "identity_kind",
        "topology_stage",
        "topology_token",
        "selector_connected",
        "fixture_evidence_v2",
        "selector_control",
    }:
        raise FineFrequencyError("fixture identity fields are incomplete or unexpected")
    stage = document.get("topology_stage")
    if (
        document.get("schema") != 1
        or document.get("identity_kind") != "5g8_t7_fixture_v2_binding"
        or stage not in TOPOLOGY_TOKENS
        or document.get("topology_token") != TOPOLOGY_TOKENS[str(stage)]
        or document.get("selector_connected") is not (stage in SELECTOR_CONNECTED_TOPOLOGIES)
    ):
        raise FineFrequencyError("fixture topology identity differs from the reviewed ladder")
    fixture = document.get("fixture_evidence_v2")
    if not isinstance(fixture, Mapping):
        raise FineFrequencyError("normalized fixture-v2 evidence is missing")
    if set(fixture) != {
        "schema",
        "fixture_kind",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "run_id",
        "board_id",
        "source_files",
        "shared_fixture",
        "shared_fixture_sha256",
        "stage_delta",
        "stage_delta_sha256",
        "prior_stage_binding",
        "setup_attestation",
        "selector_flash_evidence",
        "component_ids",
        "connection_ids",
        "characterization_summary",
    }:
        raise FineFrequencyError("fixture-v2 fields are incomplete or unexpected")
    if (
        fixture.get("schema") != 2
        or fixture.get("fixture_kind") != "5g8_general_topology_stage_fixture"
        or fixture.get("stage") != stage
        or fixture.get("run_id") != run_id
        or fixture.get("board_id") != board_id
    ):
        raise FineFrequencyError("fixture-v2 identity differs from this exact T7 plan")
    campaign_id = _identifier(fixture.get("campaign_id"), "fixture campaign ID")
    group_id = _identifier(
        fixture.get("comparable_fixture_group_id"), "comparable fixture group ID"
    )
    shared_fixture = fixture.get("shared_fixture")
    stage_delta = fixture.get("stage_delta")
    if not isinstance(shared_fixture, Mapping) or not isinstance(stage_delta, Mapping):
        raise FineFrequencyError("fixture-v2 shared fixture/stage delta is malformed")
    if (
        _sha256(fixture.get("shared_fixture_sha256"), "shared fixture hash")
        != canonical_json_sha256(shared_fixture)
        or _sha256(fixture.get("stage_delta_sha256"), "stage delta hash")
        != canonical_json_sha256(stage_delta)
        or not isinstance(fixture.get("component_ids"), list)
        or not isinstance(fixture.get("connection_ids"), list)
        or not isinstance(fixture.get("characterization_summary"), Mapping)
    ):
        raise FineFrequencyError("fixture-v2 normalized hashes/inventories are inconsistent")
    source_files = fixture.get("source_files")
    setup = fixture.get("setup_attestation")
    if not isinstance(source_files, Mapping) or not isinstance(setup, Mapping):
        raise FineFrequencyError("fixture-v2 source/setup binding is missing")
    fixture_source = source_files.get("fixture_manifest")
    setup_source = source_files.get("setup_attestation")
    fixture_source = _validate_file_identity_document(fixture_source, "fixture manifest")
    setup_source = _validate_file_identity_document(setup_source, "setup attestation")
    setup_evidence = setup.get("setup_evidence")
    if set(setup) != {
        "schema",
        "attestation_kind",
        "attestation_id",
        "created_at",
        "created_at_wall_clock_freshness_enforced",
        "run_id",
        "campaign_id",
        "comparable_fixture_group_id",
        "stage",
        "fixture_manifest_sha256",
        "shared_fixture_sha256",
        "stage_delta_sha256",
        "observed_component_ids",
        "observed_connection_ids",
        "selector_flash_evidence",
        "setup_evidence",
        "setup_attestation_file",
    }:
        raise FineFrequencyError("run-specific setup attestation fields are incomplete")
    _validate_file_identity_document(setup_evidence, "setup evidence")
    if (
        setup.get("schema") != 1
        or setup.get("attestation_kind") != "5g8_general_topology_run_setup"
        or not isinstance(setup.get("attestation_id"), str)
        or not isinstance(setup.get("created_at"), str)
        or setup.get("created_at_wall_clock_freshness_enforced") is not False
        or setup.get("run_id") != run_id
        or setup.get("campaign_id") != campaign_id
        or setup.get("comparable_fixture_group_id") != group_id
        or setup.get("stage") != stage
        or setup.get("fixture_manifest_sha256") != fixture_source.get("sha256")
        or setup.get("shared_fixture_sha256") != fixture.get("shared_fixture_sha256")
        or setup.get("stage_delta_sha256") != fixture.get("stage_delta_sha256")
        or setup.get("observed_component_ids") != fixture.get("component_ids")
        or setup.get("observed_connection_ids") != fixture.get("connection_ids")
        or setup.get("setup_attestation_file") != setup_source
    ):
        raise FineFrequencyError("run-specific setup attestation is not bound to fixture-v2")
    selector = document.get("selector_control")
    fixture_flash = fixture.get("selector_flash_evidence")
    if stage in SELECTOR_CONNECTED_TOPOLOGIES:
        flash = _validate_selector_flash_binding(
            fixture_flash,
            campaign_id=campaign_id,
            board_id=board_id,
        )
        if setup.get("selector_flash_evidence") != flash:
            raise FineFrequencyError(
                "selector control is not exact static ALL_OFF fixture evidence"
            )
        _validate_selector_control_identity(selector, flash=flash)
    elif (
        selector is not None
        or fixture_flash is not None
        or setup.get("selector_flash_evidence") is not None
    ):
        raise FineFrequencyError("selector-disconnected fixture contains selector evidence")
    return document


def _validate_device_identity(value: object) -> dict[str, str]:
    device = _identity_object(value, "device identity")
    serial = device.get("serial")
    uri = device.get("uri")
    if (
        set(device) != {"serial", "uri"}
        or not isinstance(serial, str)
        or not serial
        or not isinstance(uri, str)
        or re.fullmatch(r"usb:[0-9]+(?:\.[0-9]+)+", uri) is None
    ):
        raise FineFrequencyError("device identity requires exact serial and usb: URI")
    return {"serial": serial, "uri": uri}


def _campaign_cross_binding(
    *,
    board_id: str,
    source_identity: Mapping[str, Any],
    native_identity: Mapping[str, Any],
    fixture_identity: Mapping[str, Any],
    device_identity: Mapping[str, str],
) -> dict[str, Any]:
    """Freeze the run-independent identities that coarse and fine must share."""

    fixture = fixture_identity["fixture_evidence_v2"]
    assert isinstance(fixture, Mapping)
    source_files = fixture["source_files"]
    assert isinstance(source_files, Mapping)
    reference_planes = {
        "fixture_manifest": source_files["fixture_manifest"],
        "shared_fixture": fixture["shared_fixture"],
        "shared_fixture_sha256": fixture["shared_fixture_sha256"],
        "stage_delta": fixture["stage_delta"],
        "stage_delta_sha256": fixture["stage_delta_sha256"],
        "prior_stage_binding": fixture["prior_stage_binding"],
        "selector_flash_evidence": fixture["selector_flash_evidence"],
        "selector_control": fixture_identity["selector_control"],
        "component_ids": fixture["component_ids"],
        "connection_ids": fixture["connection_ids"],
        "characterization_summary": fixture["characterization_summary"],
    }
    stable_device = {"serial": device_identity["serial"]}
    return {
        "schema": 1,
        "binding_kind": CAMPAIGN_BINDING_KIND,
        "board_id": board_id,
        "fixture_campaign_id": fixture["campaign_id"],
        "comparable_fixture_group_id": fixture["comparable_fixture_group_id"],
        "topology_stage": fixture_identity["topology_stage"],
        "topology_token": fixture_identity["topology_token"],
        "selector_connected": fixture_identity["selector_connected"],
        "fixture_reference_planes": reference_planes,
        "fixture_reference_planes_sha256": canonical_json_sha256(reference_planes),
        "source_identity": dict(source_identity),
        "source_identity_sha256": canonical_json_sha256(source_identity),
        "native_identity": dict(native_identity),
        "native_identity_sha256": canonical_json_sha256(native_identity),
        "device_stable_identity": stable_device,
        "device_stable_identity_sha256": canonical_json_sha256(stable_device),
        "device_uri_observation": device_identity["uri"],
        "device_uri_reobservation_policy": DEVICE_URI_REOBSERVATION_POLICY,
        "run_specific_setup_attestation_excluded": True,
    }


def validate_campaign_cross_binding(value: object) -> dict[str, Any]:
    """Validate the exact coarse/fine campaign identity carried by results."""

    binding = _identity_object(value, "campaign cross-binding")
    fields = {
        "schema",
        "binding_kind",
        "board_id",
        "fixture_campaign_id",
        "comparable_fixture_group_id",
        "topology_stage",
        "topology_token",
        "selector_connected",
        "fixture_reference_planes",
        "fixture_reference_planes_sha256",
        "source_identity",
        "source_identity_sha256",
        "native_identity",
        "native_identity_sha256",
        "device_stable_identity",
        "device_stable_identity_sha256",
        "device_uri_observation",
        "device_uri_reobservation_policy",
        "run_specific_setup_attestation_excluded",
    }
    if set(binding) != fields:
        raise FineFrequencyError("campaign cross-binding fields are incomplete or unexpected")
    board_id = _identifier(binding.get("board_id"), "campaign board ID")
    _identifier(binding.get("fixture_campaign_id"), "fixture campaign ID")
    _identifier(binding.get("comparable_fixture_group_id"), "fixture group ID")
    topology_stage = binding.get("topology_stage")
    reference_planes = _identity_object(
        binding.get("fixture_reference_planes"),
        "fixture reference planes",
    )
    expected_reference_fields = {
        "fixture_manifest",
        "shared_fixture",
        "shared_fixture_sha256",
        "stage_delta",
        "stage_delta_sha256",
        "prior_stage_binding",
        "selector_flash_evidence",
        "selector_control",
        "component_ids",
        "connection_ids",
        "characterization_summary",
    }
    source = _identity_object(binding.get("source_identity"), "bound source identity")
    native = _identity_object(binding.get("native_identity"), "bound native identity")
    stable_device = _identity_object(
        binding.get("device_stable_identity"),
        "stable device identity",
    )
    uri = binding.get("device_uri_observation")
    if (
        binding.get("schema") != 1
        or binding.get("binding_kind") != CAMPAIGN_BINDING_KIND
        or topology_stage not in TOPOLOGY_TOKENS
        or binding.get("topology_token") != TOPOLOGY_TOKENS[str(topology_stage)]
        or binding.get("selector_connected")
        is not (topology_stage in SELECTOR_CONNECTED_TOPOLOGIES)
        or set(reference_planes) != expected_reference_fields
        or binding.get("fixture_reference_planes_sha256") != canonical_json_sha256(reference_planes)
        or binding.get("source_identity_sha256") != canonical_json_sha256(source)
        or binding.get("native_identity_sha256") != canonical_json_sha256(native)
        or set(stable_device) != {"serial"}
        or not isinstance(stable_device.get("serial"), str)
        or not stable_device.get("serial")
        or binding.get("device_stable_identity_sha256") != canonical_json_sha256(stable_device)
        or not isinstance(uri, str)
        or re.fullmatch(r"usb:[0-9]+(?:\.[0-9]+)+", uri) is None
        or binding.get("device_uri_reobservation_policy") != DEVICE_URI_REOBSERVATION_POLICY
        or binding.get("run_specific_setup_attestation_excluded") is not True
    ):
        raise FineFrequencyError("campaign cross-binding is internally inconsistent")
    fixture_manifest = reference_planes.get("fixture_manifest")
    _validate_file_identity_document(fixture_manifest, "bound fixture manifest")
    for name in ("shared_fixture", "stage_delta", "characterization_summary"):
        if not isinstance(reference_planes.get(name), Mapping):
            raise FineFrequencyError(f"bound {name} reference plane is malformed")
    for name in ("component_ids", "connection_ids"):
        if not isinstance(reference_planes.get(name), list):
            raise FineFrequencyError(f"bound {name} reference plane is malformed")
    if _sha256(
        reference_planes.get("shared_fixture_sha256"), "bound shared fixture hash"
    ) != canonical_json_sha256(reference_planes["shared_fixture"]) or _sha256(
        reference_planes.get("stage_delta_sha256"), "bound stage delta hash"
    ) != canonical_json_sha256(reference_planes["stage_delta"]):
        raise FineFrequencyError("fixture reference-plane hash is inconsistent")
    binding["board_id"] = board_id
    return binding


def campaign_cross_binding_from_plan_contract(value: object) -> dict[str, Any]:
    """Derive the exact cross-run binding from one validated-shape plan contract."""

    contract = _identity_object(value, "plan contract for campaign binding")
    run_id = _identifier(contract.get("run_id"), "run ID")
    board_id = _identifier(contract.get("board_id"), "board ID")
    source = _identity_object(contract.get("source_identity"), "source identity")
    native = _identity_object(contract.get("native_identity"), "native identity")
    fixture = _validate_fixture_plan_identity(
        contract.get("fixture_identity"),
        run_id=run_id,
        board_id=board_id,
    )
    device = _validate_device_identity(contract.get("device_identity"))
    for identity, hash_name, label in (
        (source, "source_identity_sha256", "source identity"),
        (native, "native_identity_sha256", "native identity"),
        (fixture, "fixture_identity_sha256", "fixture identity"),
        (device, "device_identity_sha256", "device identity"),
    ):
        if contract.get(hash_name) != canonical_json_sha256(identity):
            raise FineFrequencyError(f"{label} differs from its plan hash")
    return validate_campaign_cross_binding(
        _campaign_cross_binding(
            board_id=board_id,
            source_identity=source,
            native_identity=native,
            fixture_identity=fixture,
            device_identity=device,
        )
    )


def _campaign_binding_without_uri(value: Mapping[str, Any]) -> dict[str, Any]:
    comparable = dict(value)
    comparable.pop("device_uri_observation", None)
    return comparable


def _validate_coarse_results_binding(value: object) -> dict[str, Any]:
    coarse = _identity_object(value, "coarse results binding")
    if set(coarse) != {
        "path",
        "sha256",
        "size_bytes",
        "coarse_plan_contract_sha256",
        "campaign_binding",
        "campaign_binding_sha256",
    }:
        raise FineFrequencyError("coarse results binding is incomplete or unexpected")
    path = coarse.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise FineFrequencyError("coarse results path must be absolute")
    _sha256(coarse.get("sha256"), "coarse results hash")
    _positive_int(coarse.get("size_bytes"), "coarse results size")
    _sha256(coarse.get("coarse_plan_contract_sha256"), "coarse plan contract hash")
    campaign = validate_campaign_cross_binding(coarse.get("campaign_binding"))
    if coarse.get("campaign_binding_sha256") != canonical_json_sha256(campaign):
        raise FineFrequencyError("coarse campaign cross-binding hash is inconsistent")
    coarse["campaign_binding"] = campaign
    return coarse


def build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    schedule: SweepSchedule,
    source_identity: Mapping[str, Any],
    native_identity: Mapping[str, Any],
    fixture_identity: Mapping[str, Any],
    device_identity: Mapping[str, Any],
    free_bytes: int,
    coarse_results_binding: Mapping[str, Any] | None = None,
    refinement_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze a complete offline-verifiable coarse or fine plan contract."""

    run = _identifier(run_id, "run ID")
    board = _identifier(board_id, "board ID")
    validate_schedule(schedule)
    source = _identity_object(source_identity, "source identity")
    native = _identity_object(native_identity, "native identity")
    fixture = _validate_fixture_plan_identity(
        fixture_identity,
        run_id=run,
        board_id=board,
    )
    device = _validate_device_identity(device_identity)
    serial = device["serial"]
    fixture_v2 = fixture["fixture_evidence_v2"]
    assert isinstance(fixture_v2, Mapping)
    shared_fixture = fixture_v2.get("shared_fixture")
    pluto = shared_fixture.get("pluto") if isinstance(shared_fixture, Mapping) else None
    if not isinstance(pluto, Mapping) or pluto.get("serial") != serial:
        raise FineFrequencyError("fixture-v2 Pluto serial differs from the exact device plan")
    if schedule.mode == "fine":
        if coarse_results_binding is None or refinement_selection is None:
            raise FineFrequencyError("fine plan requires bound coarse results and selection")
        coarse = _validate_coarse_results_binding(coarse_results_binding)
        selection = _identity_object(refinement_selection, "refinement selection")
        raw_selected = selection.get("selected_centers_hz")
        if not isinstance(raw_selected, list):
            raise FineFrequencyError("fine selection centers must be an array")
        selected = tuple(_positive_int(item, "fine selection center") for item in raw_selected)
        current_binding = validate_campaign_cross_binding(
            _campaign_cross_binding(
                board_id=board,
                source_identity=source,
                native_identity=native,
                fixture_identity=fixture,
                device_identity=device,
            )
        )
        coarse_campaign = coarse["campaign_binding"]
        assert isinstance(coarse_campaign, Mapping)
        if (
            selection.get("schema") != 1
            or selection.get("selection_kind") != "multiplicity_corrected_local_extrema_v1"
            or selection.get("coarse_plan_contract_sha256")
            != coarse.get("coarse_plan_contract_sha256")
        ):
            raise FineFrequencyError("fine selection does not bind the exact coarse plan")
        if selected != schedule.refinement_centers_hz:
            raise FineFrequencyError("fine schedule centers differ from bound coarse selection")
        if _campaign_binding_without_uri(coarse_campaign) != _campaign_binding_without_uri(
            current_binding
        ):
            raise FineFrequencyError(
                "fine plan board/fixture/source/native/device identity differs from coarse"
            )
    elif coarse_results_binding is not None or refinement_selection is not None:
        raise FineFrequencyError("coarse plan cannot bind a fine selection")
    else:
        coarse = None
        selection = None
    topology_stage = str(fixture["topology_stage"])
    schedule_json = schedule_document(schedule)
    storage = storage_contract(schedule, free_bytes=free_bytes)
    return {
        "schema": 1,
        "plan_kind": "5g8_bidirectional_fine_frequency_sweep",
        "run_id": run,
        "board_id": board,
        "mode": schedule.mode,
        "experimental_policy": {
            "id": EXPERIMENTAL_POLICY,
            "minimum_hz": COARSE_MINIMUM_HZ,
            "maximum_hz": COARSE_MAXIMUM_HZ,
            "warning": EXPERIMENTAL_WARNING,
            "explicit_operator_opt_in_required": True,
        },
        "source_identity": source,
        "source_identity_sha256": canonical_json_sha256(source),
        "native_identity": native,
        "native_identity_sha256": canonical_json_sha256(native),
        "fixture_identity": fixture,
        "fixture_identity_sha256": canonical_json_sha256(fixture),
        "device_identity": device,
        "device_identity_sha256": canonical_json_sha256(device),
        "coarse_results_binding": coarse,
        "refinement_selection": selection,
        "acquisition": {
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "tone_offset_hz_requested": TONE_OFFSET_HZ,
            "sample_count": TOTAL_SAMPLES,
            "samples_per_frame": SAMPLES_PER_FRAME,
            "frame_count": FRAME_COUNT,
            "kernel_buffers": KERNEL_BUFFERS,
            "receiver_count": RECEIVER_COUNT,
            "receiver_gain_db": RECEIVER_GAIN_DB,
            "tx_channel": TX_CHANNEL,
            "tx_hardware_gain_db": TX_HARDWARE_GAIN_DB,
            "dds_scale": DDS_SCALE,
            "metadata_abi": 2,
            "fresh_stream_per_condition": True,
            "exact_lo_readback_required": True,
            "exact_dds_readback_required": True,
            "dds_readback_tolerance_hz": DDS_READBACK_TOLERANCE_HZ,
        },
        "schedule": schedule_json,
        "schedule_sha256": canonical_json_sha256(schedule_json),
        "storage": storage,
        "safety": {
            "no_antennas": True,
            "topology_stage": topology_stage,
            "topology_token": TOPOLOGY_TOKENS[topology_stage],
            "fully_conducted_fixture": topology_stage == "full_conducted_fixture",
            "selector_static_all_off_readback_required": (
                topology_stage in SELECTOR_CONNECTED_TOPOLOGIES
            ),
            "tx2_exact_muted_and_terminated": True,
            "rx1_protected_reference": True,
            "no_movement": True,
            "automatic_retry_count": 0,
            "failed_run_id_burned": True,
            "interrupted_captures_never_spliced": True,
        },
    }


def plan_envelope(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Seal a canonical immutable plan envelope."""

    normalized = validate_plan_contract(_identity_object(contract, "plan contract"))
    return {
        "schema": 1,
        "immutable": True,
        "plan_contract": normalized,
        "plan_contract_sha256": canonical_json_sha256(normalized),
    }


def validate_plan_contract(value: object) -> dict[str, Any]:
    """Recompute a plan from its inputs so a self-hashed unsafe plan cannot pass."""

    contract = _identity_object(value, "plan contract")
    execution_storage = contract.pop("execution_storage", None)
    required_keys = {
        "schema",
        "plan_kind",
        "run_id",
        "board_id",
        "mode",
        "experimental_policy",
        "source_identity",
        "source_identity_sha256",
        "native_identity",
        "native_identity_sha256",
        "fixture_identity",
        "fixture_identity_sha256",
        "device_identity",
        "device_identity_sha256",
        "coarse_results_binding",
        "refinement_selection",
        "acquisition",
        "schedule",
        "schedule_sha256",
        "storage",
        "safety",
    }
    if set(contract) != required_keys:
        raise FineFrequencyError("plan contract fields are incomplete or unexpected")
    schedule = schedule_from_document(contract["schedule"])
    storage = contract.get("storage")
    if not isinstance(storage, Mapping):
        raise FineFrequencyError("plan storage contract is missing")
    free_bytes = _positive_int(
        storage.get("observed_free_bytes_at_plan"),
        "planned free local bytes",
    )
    for name in (
        "source_identity",
        "native_identity",
        "fixture_identity",
        "device_identity",
    ):
        if not isinstance(contract[name], Mapping):
            raise FineFrequencyError(f"{name} is not an object")
    coarse = contract.get("coarse_results_binding")
    selection = contract.get("refinement_selection")
    if coarse is not None and not isinstance(coarse, Mapping):
        raise FineFrequencyError("coarse results binding is not an object")
    if selection is not None and not isinstance(selection, Mapping):
        raise FineFrequencyError("refinement selection is not an object")
    rebuilt = build_plan_contract(
        run_id=str(contract["run_id"]),
        board_id=str(contract["board_id"]),
        schedule=schedule,
        source_identity=contract["source_identity"],
        native_identity=contract["native_identity"],
        fixture_identity=contract["fixture_identity"],
        device_identity=contract["device_identity"],
        free_bytes=free_bytes,
        coarse_results_binding=coarse,
        refinement_selection=selection,
    )
    if rebuilt != contract:
        raise FineFrequencyError("plan contract differs from its regenerated policy contract")
    if execution_storage is not None:
        if not isinstance(execution_storage, Mapping) or set(execution_storage) != {
            "state_root",
            "run_root",
            "capture_root",
            "medium",
            "pluto_onboard_storage_used",
            "global_run_ledger_authority",
        }:
            raise FineFrequencyError("execution storage identity is malformed")
        state_root = Path(str(execution_storage.get("state_root", "")))
        run_root = Path(str(execution_storage.get("run_root", "")))
        capture_root = Path(str(execution_storage.get("capture_root", "")))
        authority = execution_storage.get("global_run_ledger_authority")
        if (
            not state_root.is_absolute()
            or not run_root.is_absolute()
            or run_root
            != state_root
            / "boards"
            / str(contract["board_id"])
            / "5g8-fine-frequency"
            / str(contract["run_id"])
            or capture_root != run_root / "captures"
            or execution_storage.get("medium") != "raspberry_pi_local_filesystem"
            or execution_storage.get("pluto_onboard_storage_used") is not False
        ):
            raise FineFrequencyError("execution storage is outside the exact local contract")
        expected_namespace = {
            "schema": 1,
            "policy_id": "t7-5g8-fine-frequency-v1",
            "namespace_kind": "5g8_fine_frequency_board_run_id_v1",
            "board_id": contract["board_id"],
            "run_id": contract["run_id"],
        }
        expected_identity = {
            "schema": 1,
            "board_id": contract["board_id"],
            "run_id": contract["run_id"],
            "plan_path": str(run_root / "plan.json"),
        }
        try:
            if not isinstance(authority, Mapping):
                raise global_ledger.GlobalLedgerError("shared authority is not an object")
            shared_storage = global_ledger.validate_storage_document(
                authority.get("storage"),
                allow_private_test=True,
            )
            expected_authority = global_ledger.build_authority(
                policy=global_ledger.POLICIES["t7-5g8-fine-frequency-v1"],
                namespace=expected_namespace,
                canonical_identity=expected_identity,
                state_root=state_root,
                storage=shared_storage,
            )
        except (global_ledger.GlobalLedgerError, OSError, ValueError) as error:
            raise FineFrequencyError(
                f"global run-ledger authority is malformed: {error}"
            ) from error
        if dict(authority) != expected_authority:
            raise FineFrequencyError("global run-ledger authority differs from the plan")
        rebuilt["execution_storage"] = dict(execution_storage)
    return rebuilt


def validate_plan_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "immutable",
        "plan_contract",
        "plan_contract_sha256",
    }:
        raise FineFrequencyError("immutable plan envelope is incomplete")
    contract = _identity_object(value["plan_contract"], "plan contract")
    if (
        value.get("schema") != 1
        or value.get("immutable") is not True
        or _sha256(value.get("plan_contract_sha256"), "plan contract hash")
        != canonical_json_sha256(contract)
    ):
        raise FineFrequencyError("immutable plan envelope hash/identity is invalid")
    return validate_plan_contract(contract)


def _condition_map(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    schedule = contract.get("schedule")
    conditions = schedule.get("conditions") if isinstance(schedule, Mapping) else None
    if not isinstance(conditions, list) or not conditions:
        raise FineFrequencyError("plan has no conditions")
    output: dict[str, dict[str, Any]] = {}
    for raw in conditions:
        if not isinstance(raw, Mapping):
            raise FineFrequencyError("plan condition is malformed")
        item = dict(raw)
        condition_id = item.get("condition_id")
        if not isinstance(condition_id, str) or condition_id in output:
            raise FineFrequencyError("plan condition IDs are missing or duplicated")
        output[condition_id] = item
    return output


def coherent_measurement_document(pilot: Any, analysis: Any) -> dict[str, Any]:
    """Serialize one recomputable transfer without assigning phase to nondetections."""

    try:
        transfer = analysis.rx2_over_rx1
        detected = bool(analysis.rx2.tone_detected)
        quality_passed = bool(analysis.quality_passed)
        rx1_detected = bool(analysis.rx1.tone_detected)
        rejection_reasons = tuple(analysis.quality_rejection_reasons)
    except AttributeError as error:
        raise FineFrequencyError("coherent measurement object is malformed") from error
    phasor = transfer.phasor if detected else None
    magnitude = transfer.amplitude_ratio if detected else None
    phase_deg = transfer.phase_deg if detected else None
    upper_bound = None if detected else transfer.amplitude_upper_bound_ratio
    coherent_document = _json_safe(asdict(analysis), "coherent transfer")
    if not detected:
        raw_transfer = coherent_document.get("rx2_over_rx1")
        raw_rx2 = coherent_document.get("rx2")
        if not isinstance(raw_transfer, dict) or not isinstance(raw_rx2, dict):
            raise FineFrequencyError("coherent nondetection diagnostics are malformed")
        for field in ("phasor", "amplitude_ratio", "amplitude_db", "phase_deg"):
            raw_transfer[field] = None
        for field in ("phasor", "phase_deg"):
            raw_rx2[field] = None
    return {
        "schema": 1,
        "analysis_kind": "raw_ci16_coherent_rx2_over_rx1_v1",
        "pilot": _json_safe(asdict(pilot), "pilot evidence"),
        "coherent_transfer": coherent_document,
        "quality_passed": quality_passed,
        "quality_rejection_reasons": list(rejection_reasons),
        "rx1_reference_tone_detected": rx1_detected,
        "rx2_tone_detected": detected,
        "phasor": None if phasor is None else _complex_document(complex(phasor)),
        "magnitude": None if magnitude is None else float(magnitude),
        "phase_deg": None if phase_deg is None else float(phase_deg),
        "amplitude_upper_bound_ratio": (None if upper_bound is None else float(upper_bound)),
        "nondetection_is_phase_free": not detected,
    }


def _complex_from_document(value: object, label: str) -> complex:
    if not isinstance(value, Mapping) or set(value) != {"real", "imag"}:
        raise FineFrequencyError(f"{label} must be a finite complex object")
    return complex(
        _finite_float(value.get("real"), f"{label} real component"),
        _finite_float(value.get("imag"), f"{label} imaginary component"),
    )


def _selector_snapshot_passed(value: object, *, expected_code: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    integer_fields = (
        "applied_code",
        "command_code",
        "command_lease_ms",
        "command_sequence",
        "acknowledged_sequence",
        "remaining_lease_ms",
    )
    boolean_fields = ("command_valid", "lease_active", "guard_active", "invalid_command")
    if not all(
        not isinstance(value.get(field), bool)
        and isinstance(value.get(field), Integral)
        and int(value[field]) >= 0
        for field in integer_fields
    ) or not all(isinstance(value.get(field), bool) for field in boolean_fields):
        return False
    return (
        value.get("applied_code") == expected_code
        and value.get("command_code") == expected_code
        and value.get("command_lease_ms") == 0
        and value.get("command_sequence") == value.get("acknowledged_sequence")
        and value.get("command_valid") is True
        and value.get("lease_active") is False
        and value.get("remaining_lease_ms") == 0
        and value.get("guard_active") is False
        and value.get("invalid_command") is False
    )


def _selector_attestation_passed(
    value: object,
    *,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> bool:
    command = selector_control.get("command")
    if not isinstance(command, Mapping):
        return False
    code = command.get("code")
    if not (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "static_selector_all_off_mailbox_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("all_off_code") == code
        and value.get("lease_ms") == 0
        and value.get("error") is None
        and _selector_snapshot_passed(value.get("readback"), expected_code=code)
    ):
        return False
    if purpose == "before_condition":
        return (
            value.get("operation") == "command_all_off"
            and value.get("command_was_issued") is True
            and value.get("pre_command_was_all_off") is True
            and _selector_snapshot_passed(value.get("pre_command"), expected_code=code)
            and _selector_snapshot_passed(value.get("commanded"), expected_code=code)
        )
    if purpose == "after_condition":
        return (
            value.get("operation") == "read_only"
            and value.get("command_was_issued") is False
            and value.get("pre_command") is None
            and value.get("commanded") is None
            and value.get("pre_command_was_all_off") is None
        )
    if purpose == "condition_cleanup_all_off":
        pre = value.get("pre_command")
        observed = _selector_snapshot_passed(pre, expected_code=code)
        return (
            value.get("operation") == "command_all_off"
            and value.get("command_was_issued") is True
            and isinstance(value.get("pre_command_was_all_off"), bool)
            and value.get("pre_command_was_all_off") is observed
            and _selector_snapshot_passed(value.get("commanded"), expected_code=code)
        )
    return False


def normalized_observation_from_evidence(
    condition: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Create the only admitted sweep observation shape from validated evidence."""

    analysis = evidence.get("analysis")
    capture = evidence.get("capture")
    artifact = evidence.get("artifact")
    if not all(isinstance(value, Mapping) for value in (analysis, capture, artifact)):
        raise FineFrequencyError("condition evidence cannot form a normalized observation")
    assert isinstance(analysis, Mapping)
    assert isinstance(capture, Mapping)
    assert isinstance(artifact, Mapping)
    return {
        "condition_id": condition["condition_id"],
        "accepted": True,
        "refinement_id": condition["refinement_id"],
        "direction": condition["direction"],
        "frequency_hz": condition["frequency_hz"],
        "role": condition["role"],
        "repeat_index": condition["repeat_index"],
        "stream_id": capture["stream_id"],
        "artifact_sha256": artifact["data_sha256"],
        "detected": analysis["rx2_tone_detected"],
        "phasor": analysis["phasor"],
        "magnitude": analysis["magnitude"],
        "phase_deg": analysis["phase_deg"],
        "amplitude_upper_bound_ratio": analysis["amplitude_upper_bound_ratio"],
        "nondetection_is_phase_free": analysis["nondetection_is_phase_free"],
    }


def validate_live_condition_evidence(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    prior_stream_ids: set[int],
    prior_artifact_sha256s: set[str],
) -> dict[str, Any]:
    """Fail closed on exact device/LO/DDS/ABI2/storage readback evidence."""

    document = _identity_object(evidence, "condition evidence")
    if set(document) != {
        "schema",
        "evidence_kind",
        "condition_id",
        "status",
        "device",
        "rf_readback",
        "capture",
        "artifact",
        "analysis",
        "safety",
        "selector_static_all_off",
    } or (
        document.get("schema") != 1
        or document.get("evidence_kind") != "5g8_fine_frequency_condition_v1"
    ):
        raise FineFrequencyError("condition evidence schema is incomplete or unexpected")
    conditions = _condition_map(contract)
    condition_id = document.get("condition_id")
    if not isinstance(condition_id, str) or condition_id not in conditions:
        raise FineFrequencyError("condition evidence is not in the immutable plan")
    condition = conditions[condition_id]
    if document.get("status") != "passed":
        raise FineFrequencyError("condition evidence is not quality-passed")
    device = document.get("device")
    expected_device = contract.get("device_identity")
    if not isinstance(device, Mapping) or not isinstance(expected_device, Mapping):
        raise FineFrequencyError("condition device identity is missing")
    if set(device) != {"serial", "uri", "usb_sysfs_path", "radio_identity"} or not isinstance(
        device.get("usb_sysfs_path"), str
    ):
        raise FineFrequencyError("condition full device evidence is incomplete or unexpected")
    if device.get("serial") != expected_device.get("serial") or device.get(
        "uri"
    ) != expected_device.get("uri"):
        raise FineFrequencyError("condition device identity differs from the plan")
    radio_identity = device.get("radio_identity")
    if (
        not isinstance(radio_identity, Mapping)
        or radio_identity.get("serial") != expected_device.get("serial")
        or radio_identity.get("uri") != expected_device.get("uri")
    ):
        raise FineFrequencyError("full live Pluto identity differs from the plan")
    readback = document.get("rf_readback")
    if not isinstance(readback, Mapping):
        raise FineFrequencyError("condition RF readback is missing")
    if (
        set(readback)
        != {
            "rx_lo_hz",
            "tx_lo_hz",
            "lo_readback_provenance",
            "sample_rate_hz",
            "bandwidth_hz",
            "tx1_gain_db",
            "tx2_gain_db",
            "tx2_gain_readback_provenance",
            "dds_enabled_readback",
            "dds_scale_readback",
            "dds_frequency_readback_hz",
        }
        or readback.get("lo_readback_provenance")
        != "pluto_plus_utils_continuous_exact_condition_preflight"
    ):
        raise FineFrequencyError("condition RF readback fields/provenance are not exact")
    if readback.get("tx2_gain_readback_provenance") != (
        "pluto_plus_utils_capture_helper_internal_exact_readback"
    ):
        raise FineFrequencyError("TX2 mute readback provenance is not the pinned helper")
    center = int(condition["frequency_hz"])
    if (
        readback.get("rx_lo_hz") != center
        or readback.get("tx_lo_hz") != center
        or readback.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or readback.get("bandwidth_hz") != BANDWIDTH_HZ
        or _finite_float(readback.get("tx1_gain_db"), "TX1 gain readback") != TX_HARDWARE_GAIN_DB
        or _finite_float(readback.get("tx2_gain_db"), "TX2 gain readback") != -80.0
    ):
        raise FineFrequencyError("exact LO/radio settings readback differs from the condition")
    scales = readback.get("dds_scale_readback")
    frequencies = readback.get("dds_frequency_readback_hz")
    enabled = readback.get("dds_enabled_readback")
    if (
        not isinstance(scales, list)
        or len(scales) != 8
        or not isinstance(frequencies, list)
        or len(frequencies) != 8
        or not isinstance(enabled, list)
        or len(enabled) != 8
    ):
        raise FineFrequencyError("DDS readback arrays are malformed")
    if enabled[0] is not True or enabled[2] is not True:
        raise FineFrequencyError("active DDS I/Q sources were not both enabled")
    for index, scale in enumerate(scales):
        expected_scale = DDS_SCALE if index in (0, 2) else 0.0
        if _finite_float(scale, "DDS scale readback") != expected_scale:
            raise FineFrequencyError("DDS scale readback differs from the exact plan")
    active = (
        abs(_finite_float(frequencies[0], "DDS frequency")),
        abs(_finite_float(frequencies[2], "DDS frequency")),
    )
    if abs(active[0] - active[1]) > DDS_READBACK_TOLERANCE_HZ or any(
        abs(value - TONE_OFFSET_HZ) > DDS_READBACK_TOLERANCE_HZ for value in active
    ):
        raise FineFrequencyError("active DDS frequency readback differs from the exact tone")
    capture = document.get("capture")
    if not isinstance(capture, Mapping):
        raise FineFrequencyError("capture evidence is missing")
    if set(capture) != {
        "stream_id",
        "metadata_abi",
        "first_buffer_sequence",
        "sample_count",
        "frame_count",
        "kernel_buffers",
        "continuity_passed",
        "headroom_passed",
        "clipped_sample_count",
        "final_mute_passed",
        "live_ledger",
        "persisted_continuity",
    }:
        raise FineFrequencyError("capture evidence fields are incomplete or unexpected")
    stream_id = _positive_int(capture.get("stream_id"), "stream ID")
    if stream_id in prior_stream_ids:
        raise FineFrequencyError("condition reused an earlier stream ID")
    if (
        capture.get("metadata_abi") != 2
        or capture.get("first_buffer_sequence") != 0
        or capture.get("sample_count") != TOTAL_SAMPLES
        or capture.get("frame_count") != FRAME_COUNT
        or capture.get("kernel_buffers") != KERNEL_BUFFERS
        or capture.get("continuity_passed") is not True
        or capture.get("headroom_passed") is not True
        or capture.get("clipped_sample_count") != 0
        or capture.get("final_mute_passed") is not True
    ):
        raise FineFrequencyError("capture continuity/headroom/final-mute evidence failed")
    live_ledger = capture.get("live_ledger")
    persisted_continuity = capture.get("persisted_continuity")
    if not isinstance(live_ledger, Mapping) or not isinstance(persisted_continuity, Mapping):
        raise FineFrequencyError("live and persisted ABI2 continuity evidence is required")
    for continuity in (live_ledger, persisted_continuity):
        if (
            continuity.get("metadata_abi") != 2
            or continuity.get("stream_id") != stream_id
            or continuity.get("block_count") != FRAME_COUNT
            or continuity.get("total_samples") != TOTAL_SAMPLES
            or continuity.get("first_buffer_sequence", 0) != 0
        ):
            raise FineFrequencyError("ABI2 continuity identity differs from the capture")
    artifact = document.get("artifact")
    if not isinstance(artifact, Mapping):
        raise FineFrequencyError("artifact evidence is missing")
    if set(artifact) != {
        "artifact_id",
        "path",
        "data_path",
        "data_sha256",
        "data_size_bytes",
        "metadata_path",
        "metadata_sha256",
        "metadata_size_bytes",
        "condition_record_path",
        "condition_record_sha256",
        "condition_record_size_bytes",
        "local_rpi_storage",
        "pluto_storage_used",
    }:
        raise FineFrequencyError("artifact evidence fields are incomplete or unexpected")
    artifact_sha = _sha256(artifact.get("data_sha256"), "artifact data hash")
    if artifact_sha in prior_artifact_sha256s:
        raise FineFrequencyError("condition reused an earlier artifact hash")
    if (
        artifact.get("data_size_bytes") != BYTES_PER_CAPTURE
        or not isinstance(artifact.get("artifact_id"), str)
        or not artifact.get("artifact_id")
        or not all(
            isinstance(artifact.get(name), str) and Path(str(artifact[name])).is_absolute()
            for name in ("path", "data_path", "metadata_path", "condition_record_path")
        )
        or _sha256(artifact.get("metadata_sha256"), "artifact metadata hash")
        != artifact.get("metadata_sha256")
        or _sha256(artifact.get("condition_record_sha256"), "condition record hash")
        != artifact.get("condition_record_sha256")
        or not isinstance(artifact.get("metadata_size_bytes"), Integral)
        or int(artifact["metadata_size_bytes"]) <= 0
        or not isinstance(artifact.get("condition_record_size_bytes"), Integral)
        or int(artifact["condition_record_size_bytes"]) <= 0
        or artifact.get("local_rpi_storage") is not True
        or artifact.get("pluto_storage_used") is not False
    ):
        raise FineFrequencyError("artifact size/local Raspberry Pi storage proof failed")
    analysis = document.get("analysis")
    if not isinstance(analysis, Mapping) or set(analysis) != {
        "schema",
        "analysis_kind",
        "pilot",
        "coherent_transfer",
        "quality_passed",
        "quality_rejection_reasons",
        "rx1_reference_tone_detected",
        "rx2_tone_detected",
        "phasor",
        "magnitude",
        "phase_deg",
        "amplitude_upper_bound_ratio",
        "nondetection_is_phase_free",
    }:
        raise FineFrequencyError("exact-tone analysis evidence is missing")
    if (
        analysis.get("schema") != 1
        or analysis.get("analysis_kind") != "raw_ci16_coherent_rx2_over_rx1_v1"
        or analysis.get("quality_passed") is not True
        or analysis.get("quality_rejection_reasons") != []
        or analysis.get("rx1_reference_tone_detected") is not True
    ):
        raise FineFrequencyError("coherent transfer lacks a quality-passed RX1 reference")
    detected = analysis.get("rx2_tone_detected")
    if not isinstance(detected, bool):
        raise FineFrequencyError("RX2 tone detection state is malformed")
    if detected:
        phasor = _complex_from_document(analysis.get("phasor"), "condition phasor")
        magnitude = _finite_float(analysis.get("magnitude"), "condition magnitude")
        phase = _finite_float(analysis.get("phase_deg"), "condition phase")
        expected_phase = math.degrees(math.atan2(phasor.imag, phasor.real))
        phase_error = (phase - expected_phase + 180.0) % 360.0 - 180.0
        if (
            magnitude <= 0.0
            or not math.isclose(abs(phasor), magnitude, rel_tol=1e-9, abs_tol=1e-12)
            or abs(phase_error) > 1e-9
            or analysis.get("amplitude_upper_bound_ratio") is not None
            or analysis.get("nondetection_is_phase_free") is not False
        ):
            raise FineFrequencyError("detected transfer phasor/magnitude/phase is inconsistent")
    else:
        upper = _finite_float(
            analysis.get("amplitude_upper_bound_ratio"),
            "nondetection amplitude upper bound",
        )
        if (
            upper <= 0.0
            or analysis.get("phasor") is not None
            or analysis.get("magnitude") is not None
            or analysis.get("phase_deg") is not None
            or analysis.get("nondetection_is_phase_free") is not True
        ):
            raise FineFrequencyError("nondetection must retain only a phase-free upper bound")
    safety = document.get("safety")
    if not isinstance(safety, Mapping):
        raise FineFrequencyError("condition exact-mute safety evidence is missing")
    if set(safety) != {
        "initial_mute",
        "final_mute",
        "persistence_began_only_after_final_mute_passed",
        "selector_all_off_passed_before_persistence",
    }:
        raise FineFrequencyError("condition safety evidence fields are incomplete or unexpected")
    for name, purpose in (
        ("initial_mute", "pre_condition_exact_mute"),
        ("final_mute", "final_condition_exact_mute"),
    ):
        mute = safety.get(name)
        if not isinstance(mute, Mapping) or (
            mute.get("status") != "passed"
            or mute.get("serial") != expected_device.get("serial")
            or mute.get("purpose") != purpose
            or mute.get("attestation") != "mute_returned_radio_exact_serial_readback"
            or mute.get("error") is not None
        ):
            raise FineFrequencyError(f"condition {name} evidence failed")
    if safety.get("persistence_began_only_after_final_mute_passed") is not True:
        raise FineFrequencyError("artifact persistence was not gated by exact final mute")
    fixture_identity = contract.get("fixture_identity")
    selector_control = (
        fixture_identity.get("selector_control") if isinstance(fixture_identity, Mapping) else None
    )
    selector_evidence = document.get("selector_static_all_off")
    selector_connected = (
        isinstance(fixture_identity, Mapping) and fixture_identity.get("selector_connected") is True
    )
    if safety.get("selector_all_off_passed_before_persistence") is not (
        True if selector_connected else None
    ):
        raise FineFrequencyError("safety selector-persistence gate differs from topology")
    if selector_connected:
        if not isinstance(selector_control, Mapping) or not isinstance(selector_evidence, Mapping):
            raise FineFrequencyError("selector-connected condition lacks ALL_OFF readback")
        for field, purpose in (
            ("before", "before_condition"),
            ("after", "after_condition"),
            ("cleanup", "condition_cleanup_all_off"),
        ):
            if not _selector_attestation_passed(
                selector_evidence.get(field),
                selector_control=selector_control,
                purpose=purpose,
            ):
                raise FineFrequencyError(f"selector static ALL_OFF {field} evidence failed")
    elif selector_control is not None or selector_evidence is not None:
        raise FineFrequencyError("selector-disconnected condition contains selector evidence")
    return document


def _validated_observations(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    condition_map = _condition_map(contract)
    by_condition: dict[str, dict[str, Any]] = {}
    streams: set[int] = set()
    hashes: set[str] = set()
    for raw in observations:
        document = _identity_object(raw, "sweep observation")
        if set(document) != {
            "condition_id",
            "accepted",
            "refinement_id",
            "direction",
            "frequency_hz",
            "role",
            "repeat_index",
            "stream_id",
            "artifact_sha256",
            "detected",
            "phasor",
            "magnitude",
            "phase_deg",
            "amplitude_upper_bound_ratio",
            "nondetection_is_phase_free",
        }:
            raise FineFrequencyError("sweep observation fields are incomplete or unexpected")
        condition_id = document.get("condition_id")
        if not isinstance(condition_id, str) or condition_id not in condition_map:
            raise FineFrequencyError("observation does not bind a planned condition")
        if condition_id in by_condition:
            raise FineFrequencyError("observation duplicates a planned condition")
        if document.get("accepted") is not True:
            raise FineFrequencyError("unaccepted observation cannot enter analysis")
        condition = condition_map[condition_id]
        for field in (
            "direction",
            "frequency_hz",
            "role",
            "repeat_index",
            "refinement_id",
        ):
            if document.get(field) != condition.get(field):
                raise FineFrequencyError(f"observation {field} differs from its condition")
        stream_id = _positive_int(document.get("stream_id"), "observation stream ID")
        artifact_sha = _sha256(document.get("artifact_sha256"), "observation artifact hash")
        detected = document.get("detected")
        if not isinstance(detected, bool):
            raise FineFrequencyError("observation detection state is malformed")
        if detected:
            phasor = _complex_from_document(document.get("phasor"), "observation phasor")
            magnitude = _finite_float(document.get("magnitude"), "observation magnitude")
            phase = _finite_float(document.get("phase_deg"), "observation phase")
            expected_phase = math.degrees(math.atan2(phasor.imag, phasor.real))
            phase_error = (phase - expected_phase + 180.0) % 360.0 - 180.0
            if (
                magnitude <= 0.0
                or not math.isclose(abs(phasor), magnitude, rel_tol=1e-9, abs_tol=1e-12)
                or abs(phase_error) > 1e-9
                or document.get("amplitude_upper_bound_ratio") is not None
                or document.get("nondetection_is_phase_free") is not False
            ):
                raise FineFrequencyError("detected observation complex transfer is inconsistent")
            document["phasor"] = _complex_document(phasor)
            document["magnitude"] = magnitude
            document["phase_deg"] = phase
        else:
            upper = _finite_float(
                document.get("amplitude_upper_bound_ratio"),
                "observation nondetection upper bound",
            )
            if (
                upper <= 0.0
                or document.get("phasor") is not None
                or document.get("magnitude") is not None
                or document.get("phase_deg") is not None
                or document.get("nondetection_is_phase_free") is not True
            ):
                raise FineFrequencyError("nondetection observation is not phase-free")
            document["amplitude_upper_bound_ratio"] = upper
        if stream_id in streams or artifact_sha in hashes:
            raise FineFrequencyError("observations reuse a stream or artifact")
        streams.add(stream_id)
        hashes.add(artifact_sha)
        by_condition[condition_id] = document
    if set(by_condition) != set(condition_map):
        missing = sorted(set(condition_map) - set(by_condition))
        raise FineFrequencyError(f"sweep observations are incomplete; missing {len(missing)}")
    return tuple(by_condition[condition_id] for condition_id in condition_map)


def _exact_bootstrap_means(values: Sequence[float]) -> npt.NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    count = len(array)
    if count < 2:
        raise FineFrequencyError("bootstrap group needs at least two values")
    if count <= 5:
        exact_indices = np.asarray(tuple(product(range(count), repeat=count)), dtype=np.int16)
        return np.asarray(np.mean(array[exact_indices], axis=1), dtype=np.float64)
    seed = int(hashlib.sha256(array.tobytes()).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed)
    random_indices = rng.integers(0, count, size=(65_536, count))
    return np.asarray(np.mean(array[random_indices], axis=1), dtype=np.float64)


def _simultaneous_interval(values: Sequence[float], *, family_count: int) -> tuple[float, float]:
    if family_count < 1:
        raise FineFrequencyError("simultaneous interval family count must be positive")
    draws = _exact_bootstrap_means(values)
    alpha = 1.0 - SIMULTANEOUS_CONFIDENCE
    lower_q = alpha / (2.0 * family_count)
    upper_q = 1.0 - lower_q
    return float(np.quantile(draws, lower_q)), float(np.quantile(draws, upper_q))


def _observation_amplitude(item: Mapping[str, Any]) -> float:
    """Return a detected magnitude or a conservative nondetection upper bound."""

    field = "magnitude" if item.get("detected") is True else "amplitude_upper_bound_ratio"
    value = _finite_float(item.get(field), f"observation {field}")
    if value <= 0.0:
        raise FineFrequencyError(f"observation {field} must be positive")
    return value


def _primary_observation_groups(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str, int], tuple[dict[str, Any], ...]]:
    validated = _validated_observations(contract, observations)
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in validated:
        if item["role"] != "primary":
            continue
        key = (str(item["refinement_id"]), str(item["direction"]), int(item["frequency_hz"]))
        grouped[key].append(item)
    if any(len(values) != REPEATS_PER_VISIT for values in grouped.values()):
        raise FineFrequencyError("primary frequency does not contain exactly five repeats")
    return {key: tuple(values) for key, values in grouped.items()}


def _primary_groups(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str, int], tuple[float, ...]]:
    return {
        key: tuple(_observation_amplitude(item) for item in items)
        for key, items in _primary_observation_groups(contract, observations).items()
    }


def _wrap_phase_deg(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _circular_mean_deg(values: npt.ArrayLike) -> float:
    radians = np.deg2rad(np.asarray(values, dtype=np.float64))
    vector = np.mean(np.exp(1j * radians))
    if abs(vector) <= np.finfo(np.float64).eps:
        raise FineFrequencyError("phase observations have no defined circular mean")
    return _wrap_phase_deg(float(np.rad2deg(np.angle(vector))))


def _complex_group_summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    detected = [item for item in items if item.get("detected") is True]
    nondetected = [item for item in items if item.get("detected") is False]
    amplitudes = tuple(_observation_amplitude(item) for item in items)
    phasors = (
        tuple(_complex_from_document(item.get("phasor"), "summary phasor") for item in detected)
        if detected
        else ()
    )
    mean_phasor = sum(phasors, 0j) / len(phasors) if phasors else None
    upper_bounds = tuple(_observation_amplitude(item) for item in nondetected)
    return {
        "repeat_count": len(items),
        "detected_count": len(detected),
        "nondetection_count": len(nondetected),
        "all_repeats_detected": not nondetected,
        "mean_amplitude_or_upper_bound": float(np.mean(amplitudes)),
        "mean_detected_phasor": (
            None if mean_phasor is None else _complex_document(complex(mean_phasor))
        ),
        "mean_detected_magnitude": (
            None
            if not detected
            else float(np.mean([_observation_amplitude(item) for item in detected]))
        ),
        "circular_mean_detected_phase_deg": (
            None
            if not detected
            else _circular_mean_deg([float(item["phase_deg"]) for item in detected])
        ),
        "maximum_phase_free_amplitude_upper_bound": (
            None if not upper_bounds else float(max(upper_bounds))
        ),
    }


def direction_strata_test(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Test all ascending/descending differences before allowing any pooling."""

    observation_groups = _primary_observation_groups(contract, observations)
    groups = {
        key: tuple(_observation_amplitude(item) for item in items)
        for key, items in observation_groups.items()
    }
    refinement_ids = sorted({key[0] for key in groups})
    family_count = len(groups) // len(DIRECTIONS)
    rows: list[dict[str, Any]] = []
    for refinement_id in refinement_ids:
        frequencies = sorted({key[2] for key in groups if key[0] == refinement_id})
        for frequency_hz in frequencies:
            ascending = np.asarray(groups[(refinement_id, "ascending", frequency_hz)])
            descending = np.asarray(groups[(refinement_id, "descending", frequency_hz)])
            ascending_db = 20.0 * np.log10(ascending)
            descending_db = 20.0 * np.log10(descending)
            seed = frequency_hz % (1 << 32)
            rng = np.random.default_rng(seed)
            draws = 65_536
            asc_indices = rng.integers(0, len(ascending_db), size=(draws, len(ascending_db)))
            desc_indices = rng.integers(0, len(descending_db), size=(draws, len(descending_db)))
            differences = np.mean(ascending_db[asc_indices], axis=1) - np.mean(
                descending_db[desc_indices], axis=1
            )
            alpha = 1.0 - SIMULTANEOUS_CONFIDENCE
            lower_q = alpha / (2.0 * family_count)
            lower, upper = np.quantile(differences, (lower_q, 1.0 - lower_q))
            amplitude_passed = (
                lower >= -DIRECTION_EQUIVALENCE_DB and upper <= DIRECTION_EQUIVALENCE_DB
            )
            ascending_items = observation_groups[(refinement_id, "ascending", frequency_hz)]
            descending_items = observation_groups[(refinement_id, "descending", frequency_hz)]
            phase_available = all(
                item.get("detected") is True for item in (*ascending_items, *descending_items)
            )
            phase_interval: list[float] | None = None
            phase_difference: float | None = None
            phase_passed = False
            if phase_available:
                ascending_phase = np.asarray(
                    [float(item["phase_deg"]) for item in ascending_items], dtype=np.float64
                )
                descending_phase = np.asarray(
                    [float(item["phase_deg"]) for item in descending_items], dtype=np.float64
                )
                phase_difference = _wrap_phase_deg(
                    _circular_mean_deg(ascending_phase) - _circular_mean_deg(descending_phase)
                )
                asc_phase_indices = rng.integers(
                    0, len(ascending_phase), size=(draws, len(ascending_phase))
                )
                desc_phase_indices = rng.integers(
                    0, len(descending_phase), size=(draws, len(descending_phase))
                )
                asc_vectors = np.mean(
                    np.exp(1j * np.deg2rad(ascending_phase[asc_phase_indices])), axis=1
                )
                desc_vectors = np.mean(
                    np.exp(1j * np.deg2rad(descending_phase[desc_phase_indices])), axis=1
                )
                raw_phase_draws = np.rad2deg(np.angle(asc_vectors / desc_vectors))
                unwrapped_phase_draws = phase_difference + (
                    (raw_phase_draws - phase_difference + 180.0) % 360.0 - 180.0
                )
                phase_lower, phase_upper = np.quantile(
                    unwrapped_phase_draws, (lower_q, 1.0 - lower_q)
                )
                phase_interval = [float(phase_lower), float(phase_upper)]
                phase_passed = (
                    phase_lower >= -DIRECTION_EQUIVALENCE_PHASE_DEG
                    and phase_upper <= DIRECTION_EQUIVALENCE_PHASE_DEG
                )
            passed = amplitude_passed and phase_passed
            rows.append(
                {
                    "refinement_id": refinement_id,
                    "frequency_hz": frequency_hz,
                    "ascending_minus_descending_db": float(
                        np.mean(ascending_db) - np.mean(descending_db)
                    ),
                    "simultaneous_95_interval_db": [float(lower), float(upper)],
                    "equivalence_limit_db": DIRECTION_EQUIVALENCE_DB,
                    "amplitude_equivalence_passed": bool(amplitude_passed),
                    "ascending_minus_descending_phase_deg": phase_difference,
                    "simultaneous_95_phase_interval_deg": phase_interval,
                    "phase_equivalence_limit_deg": DIRECTION_EQUIVALENCE_PHASE_DEG,
                    "phase_equivalence_available": phase_available,
                    "phase_equivalence_passed": bool(phase_passed),
                    "contains_phase_free_nondetection": not phase_available,
                    "passed": bool(passed),
                }
            )
    return {
        "schema": 1,
        "test": "multiplicity_corrected_complex_direction_bootstrap",
        "confidence": SIMULTANEOUS_CONFIDENCE,
        "pooling_allowed": all(row["passed"] for row in rows),
        "rows": rows,
    }


def anchor_drift_summary(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Summarize the interleaved 5.8 GHz markers without treating them as grid points."""

    validated = _validated_observations(contract, observations)
    condition_map = _condition_map(contract)
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in validated:
        condition = condition_map[str(item["condition_id"])]
        if condition["role"] != "interleaved_anchor":
            continue
        anchor_group = condition.get("anchor_group_index")
        if isinstance(anchor_group, bool) or not isinstance(anchor_group, Integral):
            raise FineFrequencyError("interleaved observation has no anchor group")
        key = (
            str(condition["refinement_id"]),
            str(condition["direction"]),
            int(anchor_group),
        )
        grouped[key].append(item)
    if not grouped or any(len(values) != REPEATS_PER_VISIT for values in grouped.values()):
        raise FineFrequencyError("interleaved anchor does not contain exactly five repeats")
    family_count = max(1, len(grouped) - len({(key[0], key[1]) for key in grouped}))
    rows: list[dict[str, Any]] = []
    for refinement_id, direction in sorted({(key[0], key[1]) for key in grouped}):
        keys = sorted(key for key in grouped if key[:2] == (refinement_id, direction))
        baseline_values = tuple(_observation_amplitude(item) for item in grouped[keys[0]])
        baseline_db = 20.0 * np.log10(np.asarray(baseline_values, dtype=np.float64))
        for key in keys:
            values = tuple(_observation_amplitude(item) for item in grouped[key])
            values_db = 20.0 * np.log10(np.asarray(values, dtype=np.float64))
            mean_difference = float(np.mean(values_db) - np.mean(baseline_db))
            if key == keys[0]:
                interval = (0.0, 0.0)
            else:
                seed_material = f"{refinement_id}:{direction}:{key[2]}".encode()
                seed = int(hashlib.sha256(seed_material).hexdigest()[:16], 16)
                rng = np.random.default_rng(seed)
                draw_count = 65_536
                current_indices = rng.integers(0, len(values_db), size=(draw_count, len(values_db)))
                baseline_indices = rng.integers(
                    0, len(baseline_db), size=(draw_count, len(baseline_db))
                )
                differences = np.mean(values_db[current_indices], axis=1) - np.mean(
                    baseline_db[baseline_indices], axis=1
                )
                alpha = 1.0 - SIMULTANEOUS_CONFIDENCE
                lower_q = alpha / (2.0 * family_count)
                lower, upper = np.quantile(differences, (lower_q, 1.0 - lower_q))
                interval = (float(lower), float(upper))
            rows.append(
                {
                    "refinement_id": refinement_id,
                    "direction": direction,
                    "anchor_group_index": key[2],
                    "frequency_hz": ANCHOR_FREQUENCY_HZ,
                    "repeat_count": len(values_db),
                    "mean_magnitude_or_upper_bound": float(np.mean(values)),
                    "complex_transfer": _complex_group_summary(grouped[key]),
                    "drift_from_first_anchor_db": mean_difference,
                    "simultaneous_95_interval_db": [interval[0], interval[1]],
                }
            )
    return {
        "schema": 1,
        "reference": "first_interleaved_anchor_within_each_direction_and_refinement",
        "confidence": SIMULTANEOUS_CONFIDENCE,
        "rows": rows,
        "maximum_absolute_mean_drift_db": max(
            abs(float(row["drift_from_first_anchor_db"])) for row in rows
        ),
    }


def select_coarse_refinements(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Select deterministic significant local extrema, tying to lower frequency."""

    if contract.get("mode") != "coarse":
        raise FineFrequencyError("refinement selection requires a coarse plan")
    primary_observations = _primary_observation_groups(contract, observations)
    groups = _primary_groups(contract, observations)
    direction_test = direction_strata_test(contract, observations)
    pooling = bool(direction_test["pooling_allowed"])
    frequencies = sorted({key[2] for key in groups if key[0] == "coarse"})
    if frequencies != list(_inclusive_grid(COARSE_MINIMUM_HZ, COARSE_MAXIMUM_HZ, COARSE_STEP_HZ)):
        raise FineFrequencyError("coarse primary frequency coverage is incomplete")
    nondetection_count = sum(
        item.get("detected") is False for items in primary_observations.values() for item in items
    )
    strata: dict[str, dict[int, tuple[float, ...]]] = {}
    if pooling:
        strata["pooled"] = {
            frequency: (
                groups[("coarse", "ascending", frequency)]
                + groups[("coarse", "descending", frequency)]
            )
            for frequency in frequencies
        }
    else:
        for direction in DIRECTIONS:
            strata[direction] = {
                frequency: groups[("coarse", direction, frequency)] for frequency in frequencies
            }
    family_count = len(strata) * len(frequencies)
    summaries: dict[str, dict[int, dict[str, Any]]] = {}
    maxima: list[dict[str, Any]] = []
    minima: list[dict[str, Any]] = []
    for stratum, values_by_frequency in strata.items():
        summaries[stratum] = {}
        for frequency, values in values_by_frequency.items():
            interval = _simultaneous_interval(values, family_count=family_count)
            summaries[stratum][frequency] = {
                "center": float(np.mean(values)),
                "simultaneous_95_interval": [interval[0], interval[1]],
            }
        for index in range(1, len(frequencies) - 1):
            frequency = frequencies[index]
            before = summaries[stratum][frequencies[index - 1]]
            current = summaries[stratum][frequency]
            after = summaries[stratum][frequencies[index + 1]]
            current_lower, current_upper = current["simultaneous_95_interval"]
            before_lower, before_upper = before["simultaneous_95_interval"]
            after_lower, after_upper = after["simultaneous_95_interval"]
            if nondetection_count == 0 and current_lower > max(before_upper, after_upper):
                maxima.append(
                    {
                        "kind": "local_maximum",
                        "stratum": stratum,
                        "frequency_hz": frequency,
                        **current,
                    }
                )
            if nondetection_count == 0 and current_upper < min(before_lower, after_lower):
                minima.append(
                    {
                        "kind": "local_minimum",
                        "stratum": stratum,
                        "frequency_hz": frequency,
                        **current,
                    }
                )
    selected: list[dict[str, Any]] = []
    if maxima:
        selected.append(sorted(maxima, key=lambda item: (-item["center"], item["frequency_hz"]))[0])
    if minima:
        candidate = sorted(minima, key=lambda item: (item["center"], item["frequency_hz"]))[0]
        if not selected or candidate["frequency_hz"] != selected[0]["frequency_hz"]:
            selected.append(candidate)
    if not selected:
        selected = [
            {
                "kind": "fallback_anchor",
                "stratum": "none",
                "frequency_hz": ANCHOR_FREQUENCY_HZ,
                "center": None,
                "simultaneous_95_interval": None,
            }
        ]
    return {
        "schema": 1,
        "selection_kind": "multiplicity_corrected_local_extrema_v1",
        "coarse_plan_contract_sha256": canonical_json_sha256(contract),
        "direction_test": direction_test,
        "directions_pooled": pooling,
        "phase_free_primary_nondetection_count": nondetection_count,
        "extrema_qualification_suppressed_by_nondetection": nondetection_count > 0,
        "tie_break": "lower_frequency",
        "selected_centers_hz": [int(item["frequency_hz"]) for item in selected],
        "selected": selected,
        "qualified_local_maxima": sorted(maxima, key=lambda item: item["frequency_hz"]),
        "qualified_local_minima": sorted(minima, key=lambda item: item["frequency_hz"]),
    }


def analyze_sweep(
    contract: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Produce direction-stratified summaries and pool only after equivalence."""

    observation_groups = _primary_observation_groups(contract, observations)
    groups = {
        key: tuple(_observation_amplitude(item) for item in items)
        for key, items in observation_groups.items()
    }
    direction_test = direction_strata_test(contract, observations)
    rows = []
    for (refinement_id, direction, frequency_hz), values in sorted(groups.items()):
        interval = _simultaneous_interval(values, family_count=len(groups))
        rows.append(
            {
                "refinement_id": refinement_id,
                "direction": direction,
                "frequency_hz": frequency_hz,
                "repeat_count": len(values),
                "mean_magnitude_or_upper_bound": float(np.mean(values)),
                "simultaneous_95_interval": [interval[0], interval[1]],
                "complex_transfer": _complex_group_summary(
                    observation_groups[(refinement_id, direction, frequency_hz)]
                ),
            }
        )
    pooled_rows: list[dict[str, Any]] = []
    if direction_test["pooling_allowed"]:
        keys = sorted({(key[0], key[2]) for key in groups})
        for refinement_id, frequency_hz in keys:
            values = (
                groups[(refinement_id, "ascending", frequency_hz)]
                + groups[(refinement_id, "descending", frequency_hz)]
            )
            pooled_items = (
                observation_groups[(refinement_id, "ascending", frequency_hz)]
                + observation_groups[(refinement_id, "descending", frequency_hz)]
            )
            interval = _simultaneous_interval(values, family_count=len(keys))
            pooled_rows.append(
                {
                    "refinement_id": refinement_id,
                    "frequency_hz": frequency_hz,
                    "repeat_count": len(values),
                    "mean_magnitude_or_upper_bound": float(np.mean(values)),
                    "simultaneous_95_interval": [interval[0], interval[1]],
                    "complex_transfer": _complex_group_summary(pooled_items),
                }
            )
    return {
        "schema": 1,
        "analysis_kind": "5g8_bidirectional_frequency_strata",
        "plan_contract_sha256": canonical_json_sha256(contract),
        "direction_test": direction_test,
        "anchor_drift": anchor_drift_summary(contract, observations),
        "direction_rows": rows,
        "pooled_rows": pooled_rows,
        "pooling_performed": bool(pooled_rows),
    }
