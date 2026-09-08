"""Explicit frequency intent and fresh fixture admission for the two-band campaign."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from smateway.rate_timing import PORTS, RECEIVER_SERIAL, SOURCE_SERIAL, load, sha256


def read_protocol(path: Path) -> dict:
    protocol = load(path)
    if (
        protocol.get("schema") != 1
        or protocol.get("receiver_serial") != RECEIVER_SERIAL
        or protocol.get("source_serial") != SOURCE_SERIAL
        or tuple(protocol.get("ports", [])) != PORTS
        or protocol.get("intent_grid_step_hz") != 1_000_000
        or protocol.get("band_allocations_hz")
        != [[2_400_000_000, 2_500_000_000], [5_725_000_000, 5_875_000_000]]
    ):
        raise ValueError("protocol identity/frequency scope differs")
    return protocol


def coverage_grid(protocol: dict, approved_intervals: list | None = None) -> list[dict]:
    """Include allocation edges as explicit excluded rows, not missing measurements."""
    rows = []
    guard = protocol["provisional_edge_guard_hz"]
    for low, high in protocol["band_allocations_hz"]:
        for frequency in range(low, high + 1, protocol["intent_grid_step_hz"]):
            status = "awaiting_fixture_and_authorization"
            if not low + guard <= frequency <= high - guard:
                status = "excluded_occupied_signal_margin"
            elif approved_intervals is not None:
                status = (
                    "pending_measurement"
                    if any(a + guard <= frequency <= b - guard for a, b in approved_intervals)
                    else "excluded_authorization"
                )
            rows.append({"frequency_hz": frequency, "allocation_hz": [low, high], "status": status})
    return rows


def admit_capture(
    protocol_path: Path,
    frequency_hz: int,
    *,
    muted: bool,
    fixture_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    protocol = read_protocol(protocol_path)
    if not any(lo <= frequency_hz <= hi for lo, hi in protocol["band_allocations_hz"]):
        raise ValueError("frequency outside the two-band campaign")
    binding = {"path": str(protocol_path.resolve()), "sha256": sha256(protocol_path)}
    if muted:
        return {"protocol": binding, "scope": "muted acquisition; no OTA readiness claim"}
    if fixture_path is None:
        raise ValueError("new OTA/fixture capture requires fresh fixture attestation")
    fixture = load(fixture_path)
    now = now or datetime.now(UTC)
    confirmed = datetime.fromisoformat(fixture["confirmed_at"])
    if confirmed.tzinfo is None or not 0 <= (now - confirmed).total_seconds() <= 12 * 3600:
        raise ValueError("fixture readiness is stale or has an invalid timestamp")
    if (
        fixture.get("ready") is not True
        or fixture.get("protocol_sha256") != binding["sha256"]
        or fixture.get("receiver_serial") != RECEIVER_SERIAL
        or fixture.get("source_serial") != SOURCE_SERIAL
        or tuple(fixture.get("ports", [])) != PORTS
        or fixture.get("kind") not in ("ota", "conducted", "shielded")
        or not fixture.get("operator_confirmation")
        or not fixture.get("authorization_description")
    ):
        raise ValueError("fixture identity/readiness/authorization differs")
    coordinates = np.asarray(fixture.get("positions_m"), dtype=float)
    if coordinates.shape != (6, 2) or not np.all(np.isfinite(coordinates)):
        raise ValueError("fixture must bind the actual installed six-port geometry")
    guard = protocol["provisional_edge_guard_hz"]
    intervals = fixture.get("approved_intervals_hz", [])
    if not intervals or not any(lo + guard <= frequency_hz <= hi - guard for lo, hi in intervals):
        raise ValueError("occupied-signal margin or authorized frequency limit violated")
    if not any(
        lo + guard <= frequency_hz <= hi - guard for lo, hi in protocol["band_allocations_hz"]
    ):
        raise ValueError("occupied-signal margin at allocation boundary violated")
    return {
        "protocol": binding,
        "fixture": {"path": str(fixture_path.resolve()), "sha256": sha256(fixture_path)},
        "fixture_id": fixture["fixture_id"],
        "kind": fixture["kind"],
        "positions_m": coordinates.tolist(),
    }
