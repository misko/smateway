import pytest

from smateway.rf_policy import (
    EXPERIMENTAL_5G8_CENTER_HZ,
    QUALIFIED_2G4_MAXIMUM_HZ,
    QUALIFIED_2G4_MINIMUM_HZ,
    classify_fast20_center_frequency,
)


@pytest.mark.parametrize(
    "frequency_hz",
    (QUALIFIED_2G4_MINIMUM_HZ, 2_450_000_000, QUALIFIED_2G4_MAXIMUM_HZ),
)
def test_qualified_2g4_centers_need_no_exception(frequency_hz: int) -> None:
    assert (
        classify_fast20_center_frequency(
            frequency_hz,
            allow_experimental_5g8=False,
        )
        == "qualified_2g4_ism"
    )


def test_exact_5g8_center_requires_and_records_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="explicit --allow-experimental-5g8"):
        classify_fast20_center_frequency(
            EXPERIMENTAL_5G8_CENTER_HZ,
            allow_experimental_5g8=False,
        )

    assert (
        classify_fast20_center_frequency(
            EXPERIMENTAL_5G8_CENTER_HZ,
            allow_experimental_5g8=True,
        )
        == "experimental_5g8_user_requested"
    )


@pytest.mark.parametrize(
    "frequency_hz",
    (2_399_999_999, 2_483_500_001, 5_799_999_999, 5_800_000_001),
)
def test_unreviewed_frequency_gap_and_nearby_5g8_values_are_rejected(
    frequency_hz: int,
) -> None:
    with pytest.raises(ValueError, match="exactly 5.8000 GHz"):
        classify_fast20_center_frequency(
            frequency_hz,
            allow_experimental_5g8=True,
        )


def test_non_integer_center_is_rejected() -> None:
    with pytest.raises(ValueError, match="integer"):
        classify_fast20_center_frequency(2_400_000_000.0, allow_experimental_5g8=False)  # type: ignore[arg-type]
