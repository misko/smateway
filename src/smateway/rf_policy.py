"""Explicit RF-frequency policy for live Fast20 experiments."""

from __future__ import annotations

QUALIFIED_2G4_MINIMUM_HZ = 2_400_000_000
QUALIFIED_2G4_MAXIMUM_HZ = 2_483_500_000
EXPERIMENTAL_5G8_CENTER_HZ = 5_800_000_000
CONDUCTED_SWEEP_MINIMUM_HZ = 2_100_000_000
CONDUCTED_SWEEP_MAXIMUM_HZ = 5_800_000_000
CONDUCTED_SWEEP_STEP_HZ = 100_000_000


def classify_fast20_center_frequency(
    center_frequency_hz: int,
    *,
    allow_experimental_5g8: bool,
) -> str:
    """Return the narrow policy class or reject an unreviewed live center.

    The 2.4 GHz range is the existing qualified Fast20 range.  The separate
    5.8 GHz point is intentionally exact and opt-in: it records the user's
    requested experiment without widening the normal capture surface or
    claiming antenna/system calibration there.
    """

    if isinstance(center_frequency_hz, bool) or not isinstance(center_frequency_hz, int):
        raise ValueError("center frequency must be an integer number of hertz")
    if QUALIFIED_2G4_MINIMUM_HZ <= center_frequency_hz <= QUALIFIED_2G4_MAXIMUM_HZ:
        return "qualified_2g4_ism"
    if center_frequency_hz == EXPERIMENTAL_5G8_CENTER_HZ:
        if not allow_experimental_5g8:
            raise ValueError(
                "5.8 GHz requires the explicit --allow-experimental-5g8 opt-in"
            )
        return "experimental_5g8_user_requested"
    raise ValueError(
        "center frequency must be in 2.4000–2.4835 GHz or exactly 5.8000 GHz "
        "with the experimental opt-in"
    )


def classify_conducted_calibration_center_frequency(center_frequency_hz: int) -> str:
    """Admit only the predeclared 2.1–5.8 GHz/100 MHz conducted-sweep grid.

    This deliberately separate policy must only be selected by a runner that
    records a fully conducted, antenna-free fixture confirmation. It does not
    widen the normal Fast20 OTA frequency surface.
    """

    if isinstance(center_frequency_hz, bool) or not isinstance(center_frequency_hz, int):
        raise ValueError("center frequency must be an integer number of hertz")
    in_range = CONDUCTED_SWEEP_MINIMUM_HZ <= center_frequency_hz <= CONDUCTED_SWEEP_MAXIMUM_HZ
    on_grid = (center_frequency_hz - CONDUCTED_SWEEP_MINIMUM_HZ) % CONDUCTED_SWEEP_STEP_HZ == 0
    if in_range and on_grid:
        return "experimental_fully_conducted_2g1_to_5g8_100mhz_sweep"
    raise ValueError(
        "conducted calibration center must be on the 100 MHz grid from 2.100 through 5.800 GHz"
    )
