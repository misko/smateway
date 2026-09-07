"""ISM frequency profiles and immutable circular-array geometry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt


def _readonly_positions(values: npt.ArrayLike) -> npt.NDArray[np.float64]:
    result = np.asarray(values, dtype=np.float64).copy()
    result.setflags(write=False)
    return result


@dataclass(frozen=True, slots=True)
class ArrayGeometry:
    """Ordered physical ports and phase-centre positions in the board plane."""

    geometry_id: str
    ports: tuple[str, ...]
    positions_m: npt.NDArray[np.float64]
    bearings_deg_clockwise_from_forward: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        if not self.geometry_id or not self.ports or len(set(self.ports)) != len(self.ports):
            raise ValueError("geometry identity and unique ports are required")
        positions = _readonly_positions(self.positions_m)
        bearings = _readonly_positions(self.bearings_deg_clockwise_from_forward)
        if positions.shape != (len(self.ports), 2) or bearings.shape != (len(self.ports),):
            raise ValueError("geometry vectors disagree with the port count")
        if not np.all(np.isfinite(positions)) or not np.all(np.isfinite(bearings)):
            raise ValueError("geometry must be finite")
        object.__setattr__(self, "positions_m", positions)
        object.__setattr__(self, "bearings_deg_clockwise_from_forward", bearings)

    @classmethod
    def circular(
        cls,
        geometry_id: str,
        ports: tuple[str, ...],
        *,
        radius_mm: float,
    ) -> ArrayGeometry:
        if not np.isfinite(radius_mm) or radius_mm <= 0.0:
            raise ValueError("array radius must be positive and finite")
        bearings = np.arange(len(ports), dtype=np.float64) * (360.0 / len(ports))
        radians = np.deg2rad(bearings)
        radius_m = radius_mm / 1000.0
        positions = np.column_stack((radius_m * np.sin(radians), radius_m * np.cos(radians)))
        return cls(geometry_id, ports, positions, bearings)


@dataclass(frozen=True, slots=True)
class IsmBandProfile:
    """One array/frequency qualification profile."""

    profile_id: str
    allocation_hz: tuple[int, int]
    itu_region: str
    primary_centres_hz: tuple[int, ...]
    blind_frequency_holdouts_hz: tuple[int, ...]
    geometry: ArrayGeometry
    pcb_lut_status: str
    current_fixture_ready: bool
    current_fixture_blocker: str | None

    def includes(self, frequency_hz: float) -> bool:
        return self.allocation_hz[0] <= frequency_hz <= self.allocation_hz[1]


def load_ism_band_profiles(path: Path) -> dict[str, IsmBandProfile]:
    """Load the machine-readable tracking plan and generate its nominal C6 geometries."""

    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("ISM frequency plan has an unsupported schema")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("ISM frequency plan has no profiles")
    interpretation = document.get("interpretation")
    if not isinstance(interpretation, dict):
        raise ValueError("ISM frequency plan has no frequency interpretation")
    nominal_spacing_hz = interpretation.get("nominal_spacing_hz")
    if not isinstance(nominal_spacing_hz, int) or nominal_spacing_hz <= 0:
        raise ValueError("ISM frequency plan has an invalid nominal spacing")
    profiles: dict[str, IsmBandProfile] = {}
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            raise ValueError("ISM profile entry is invalid")
        profile_id = raw.get("id")
        allocation = raw.get("allocation_hz")
        array = raw.get("recommended_array")
        if (
            not isinstance(profile_id, str)
            or not isinstance(allocation, dict)
            or not isinstance(array, dict)
        ):
            raise ValueError("ISM profile identity, allocation, or array is invalid")
        lower = allocation.get("min")
        upper = allocation.get("max")
        if not isinstance(lower, int) or not isinstance(upper, int) or lower >= upper:
            raise ValueError(f"{profile_id} allocation is invalid")
        centres = tuple(raw.get("primary_centres_hz", ()))
        holdouts = tuple(raw.get("blind_frequency_holdouts_hz", ()))
        if (
            not centres
            or not all(isinstance(value, int) and lower <= value <= upper for value in centres)
            or not all(isinstance(value, int) and lower <= value <= upper for value in holdouts)
        ):
            raise ValueError(f"{profile_id} frequencies are invalid")
        if tuple(sorted(set(centres))) != centres or tuple(sorted(set(holdouts))) != holdouts:
            raise ValueError(f"{profile_id} frequencies must be strictly increasing")
        if set(centres) & set(holdouts):
            raise ValueError(f"{profile_id} training centres and holdouts overlap")
        if len(centres) > 1 and any(
            right - left != nominal_spacing_hz
            for left, right in pairwise(centres)
        ):
            raise ValueError(f"{profile_id} centres violate the nominal spacing")
        ports = tuple(array.get("ports_clockwise", ()))
        radius_mm = array.get("radius_mm")
        if not all(isinstance(port, str) for port in ports) or not isinstance(
            radius_mm, (int, float)
        ):
            raise ValueError(f"{profile_id} array is invalid")
        geometry = ArrayGeometry.circular(profile_id, ports, radius_mm=float(radius_mm))
        lut_status = raw.get("pcb_lut_status")
        if not isinstance(lut_status, str):
            raise ValueError(f"{profile_id} PCB LUT status is absent")
        fixture_ready = raw.get("current_fixture_ready")
        fixture_blocker = raw.get("current_fixture_blocker")
        if not isinstance(fixture_ready, bool) or (
            fixture_ready and fixture_blocker is not None
        ) or (not fixture_ready and not isinstance(fixture_blocker, str)):
            raise ValueError(f"{profile_id} current-fixture readiness is invalid")
        region = allocation.get("itu_region")
        profiles[profile_id] = IsmBandProfile(
            profile_id=profile_id,
            allocation_hz=(lower, upper),
            itu_region=str(region),
            primary_centres_hz=centres,
            blind_frequency_holdouts_hz=holdouts,
            geometry=geometry,
            pcb_lut_status=lut_status,
            current_fixture_ready=fixture_ready,
            current_fixture_blocker=fixture_blocker,
        )
    if len(profiles) != len(raw_profiles):
        raise ValueError("ISM frequency plan repeats a profile ID")
    return profiles
