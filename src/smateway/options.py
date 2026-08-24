"""Pure planning for reviewed STM32C0 option-byte changes."""

from __future__ import annotations

from dataclasses import dataclass

RDP_MASK = 0x000000FF
RDP_LEVEL_0 = 0x000000AA
BOR_ENABLE = 0x00000100
BOR_RISING_LEVEL_MASK = 0x00000600
BOR_FALLING_LEVEL_MASK = 0x00001800
BOR_LEVEL_4_FIELDS = BOR_RISING_LEVEL_MASK | BOR_FALLING_LEVEL_MASK
BOR_FIELDS_MASK = BOR_ENABLE | BOR_LEVEL_4_FIELDS


@dataclass(frozen=True, slots=True)
class Bor4Plan:
    observed_optr: int
    expected_optr: int
    write_mask: int
    write_value: int
    changed_bits: int

    @property
    def already_configured(self) -> bool:
        return self.changed_bits == 0


def plan_bor4(observed_optr: int) -> Bor4Plan:
    if observed_optr < 0 or observed_optr > 0xFFFFFFFF:
        raise ValueError("FLASH_OPTR must be a 32-bit unsigned value")
    if observed_optr & RDP_MASK != RDP_LEVEL_0:
        raise ValueError("refusing BOR planning unless RDP is exactly level 0 (0xAA)")
    expected = (observed_optr & ~BOR_FIELDS_MASK) | BOR_ENABLE | BOR_LEVEL_4_FIELDS
    changed = observed_optr ^ expected
    if changed & ~BOR_FIELDS_MASK:
        raise AssertionError("BOR plan would modify an unrelated option bit")
    return Bor4Plan(
        observed_optr=observed_optr,
        expected_optr=expected,
        write_mask=BOR_FIELDS_MASK,
        write_value=BOR_ENABLE | BOR_LEVEL_4_FIELDS,
        changed_bits=changed,
    )
