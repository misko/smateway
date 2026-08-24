import pytest

from smateway.options import BOR_FIELDS_MASK, plan_bor4


def test_factory_option_word_plans_only_bor_enable() -> None:
    plan = plan_bor4(0xFFFFFEAA)

    assert plan.expected_optr == 0xFFFFFFAA
    assert plan.write_mask == BOR_FIELDS_MASK
    assert plan.write_value == BOR_FIELDS_MASK
    assert plan.changed_bits == 0x00000100
    assert not plan.already_configured


def test_bor4_plan_is_idempotent() -> None:
    plan = plan_bor4(0xFFFFFFAA)

    assert plan.expected_optr == plan.observed_optr
    assert plan.already_configured


def test_bor4_plan_refuses_nonzero_read_protection() -> None:
    with pytest.raises(ValueError, match="RDP is exactly level 0"):
        plan_bor4(0xFFFFFFBB)
