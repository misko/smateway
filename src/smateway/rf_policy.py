"""Explicit RF-frequency policy for live Fast20 experiments."""

from __future__ import annotations

QUALIFIED_2G4_MINIMUM_HZ = 2_400_000_000
QUALIFIED_2G4_MAXIMUM_HZ = 2_483_500_000
EXPERIMENTAL_5G8_CENTER_HZ = 5_800_000_000


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
