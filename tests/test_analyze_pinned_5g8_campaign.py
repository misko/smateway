from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_pinned_5g8_campaign.py"
SPEC = importlib.util.spec_from_file_location("analyze_pinned_5g8_campaign_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)


def _row(magnitude: float, phase_deg: float) -> dict[str, object]:
    import math

    phase = math.radians(phase_deg)
    return {
        "analysis": {
            "transfer_rx2_over_rx1": {
                "real": magnitude * math.cos(phase),
                "imag": magnitude * math.sin(phase),
            }
        }
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0.0), (181.0, -179.0), (-181.0, 179.0), (540.0, 180.0)],
)
def test_wrap_phase_deg(value: float, expected: float) -> None:
    assert analyzer.wrap_phase_deg(value) == pytest.approx(expected)


def test_aggregate_transfer_handles_phase_wrap() -> None:
    result = analyzer.aggregate_transfer([_row(0.1, 179.0), _row(0.1, -179.0)])
    assert abs(result["phase_deg"]) == pytest.approx(180.0)
    assert result["mean_magnitude_db"] == pytest.approx(-20.0)
    assert result["phase_span_deg"] == pytest.approx(2.0)


def test_calibration_coefficient_maps_path_to_reference() -> None:
    reference = analyzer.aggregate_transfer([_row(0.1, 20.0)])
    path = analyzer.aggregate_transfer([_row(0.05, -10.0)])
    coefficient = analyzer.calibration_coefficient(reference, path)
    assert coefficient["gain_db"] == pytest.approx(6.0205999)
    assert coefficient["phase_deg"] == pytest.approx(30.0)


def test_empty_transfer_cohort_is_rejected() -> None:
    with pytest.raises(analyzer.CampaignError, match="empty phasor cohort"):
        analyzer.aggregate_transfer([])


def test_pinned_source_identity_is_current_fixture() -> None:
    assert analyzer.SOURCE_SERIAL == "104473b80a16000de6ff2000f8a6beca79"


def test_power_cohort_selects_only_center_frequency() -> None:
    rows = [
        {"tx_gain_db": -40.0, "frequency_hz": 5_775_000_000, "state": "ANT1"},
        {"tx_gain_db": -40.0, "frequency_hz": 5_800_000_000, "state": "ANT1"},
        {"tx_gain_db": -40.0, "frequency_hz": 5_800_000_000, "state": "ANT2"},
    ]
    assert analyzer._power_cohort(rows, gain_db=-40.0, state="ANT1") == [rows[1]]
