from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_pinned_broadband_campaign.py"
SPEC = importlib.util.spec_from_file_location(
    "analyze_pinned_broadband_campaign_under_test", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0.0), (181.0, -179.0), (-181.0, 179.0), (540.0, 180.0)],
)
def test_wrap_phase_deg(value: float, expected: float) -> None:
    assert analyzer.wrap_phase_deg(value) == pytest.approx(expected)


def test_frequency_lattice_is_exact_and_inclusive() -> None:
    assert len(analyzer.FREQUENCIES_HZ) == 38
    assert analyzer.FREQUENCIES_HZ[0] == 2_100_000_000
    assert analyzer.FREQUENCIES_HZ[-1] == 5_800_000_000
    assert set(np.diff(analyzer.FREQUENCIES_HZ)) == {100_000_000}


def test_aggregate_phasors_handles_phase_wrap() -> None:
    values = [0.1 * np.exp(1j * np.deg2rad(179.0)), 0.1 * np.exp(1j * np.deg2rad(-179.0))]
    result = analyzer.aggregate_phasors(values)
    assert abs(result["phase_deg"]) == pytest.approx(180.0)
    assert result["mean_magnitude_db"] == pytest.approx(-20.0)
    assert result["phase_span_deg"] == pytest.approx(2.0)


def test_aggregate_phasors_rejects_zero_member() -> None:
    with pytest.raises(analyzer.CampaignError, match="zero phasor"):
        analyzer.aggregate_phasors([1 + 0j, 0j])


def test_delay_model_recovers_sign_and_scores_holdout() -> None:
    frequencies = np.asarray(analyzer.FREQUENCIES_HZ)
    center = float(np.mean(frequencies))
    delay_ns = 0.725
    gain_db = 2.4
    phase_center_deg = 37.0
    phase_deg = phase_center_deg - 360.0 * delay_ns * ((frequencies - center) / 1e9)
    coefficients = 10.0 ** (gain_db / 20.0) * np.exp(1j * np.deg2rad(phase_deg))
    result = analyzer.fit_delay_model(frequencies.tolist(), coefficients.tolist())
    assert result["delay_ns"] == pytest.approx(delay_ns, abs=0.0002)
    assert result["gain_db"] == pytest.approx(gain_db, abs=1e-12)
    assert result["phase_at_center_deg"] == pytest.approx(phase_center_deg, abs=1e-10)
    assert result["heldout_phase_rms_deg"] < 0.1
    assert result["heldout_gain_rms_db"] < 1e-10
    assert result["free_space_equivalent_length_mm"] == pytest.approx(
        analyzer.LIGHT_SPEED_M_S * delay_ns * 1e-6,
        abs=0.1,
    )


def test_delay_model_holdout_detects_frequency_structure() -> None:
    frequencies = np.asarray(analyzer.FREQUENCIES_HZ)
    center = float(np.mean(frequencies))
    linear = 15.0 - 360.0 * 0.4 * ((frequencies - center) / 1e9)
    ripple = 12.0 * np.sin(2.0 * math.pi * np.arange(len(frequencies)) / 9.0)
    coefficients = np.exp(1j * np.deg2rad(linear + ripple))
    result = analyzer.fit_delay_model(frequencies.tolist(), coefficients.tolist())
    assert result["heldout_phase_rms_deg"] > 5.0
    assert result["maximum_heldout_phase_error_deg"] > result["heldout_phase_rms_deg"]


def test_delay_model_rejects_invalid_input() -> None:
    with pytest.raises(analyzer.CampaignError, match="four points"):
        analyzer.fit_delay_model([1, 2, 3], [1 + 0j] * 3)
    with pytest.raises(analyzer.CampaignError, match="finite and non-zero"):
        analyzer.fit_delay_model([1, 2, 3, 4], [1 + 0j, 1 + 0j, 0j, 1 + 0j])


def test_delay_model_flags_search_boundary_alias() -> None:
    frequencies = np.asarray(analyzer.FREQUENCIES_HZ)
    center = float(np.mean(frequencies))
    phase = -360.0 * 2.4999 * ((frequencies - center) / 1e9)
    result = analyzer.fit_delay_model(frequencies.tolist(), np.exp(1j * np.deg2rad(phase)).tolist())
    assert result["full_fit"]["search_boundary"] or any(
        fold["search_boundary"] for fold in result["cross_validation"]
    )
    assert result["heldout_phase_rms_deg"] > 100.0


def test_log_ripple_model_recovers_repeatable_frequency_structure() -> None:
    frequencies = np.asarray(analyzer.FREQUENCIES_HZ)
    delta_frequency_ghz = (frequencies - np.mean(frequencies)) / 1e9
    base_delay_ns = 0.085
    ripple_delay_ns = 0.64
    ripple = 0.38 * np.exp(1j * np.deg2rad(47.0))
    log_response = (
        math.log(0.8)
        + 1j * np.deg2rad(-32.0)
        - 2j * np.pi * delta_frequency_ghz * base_delay_ns
        + ripple * np.exp(-2j * np.pi * delta_frequency_ghz * ripple_delay_ns)
    )
    coefficients = np.exp(log_response)
    delay_only = analyzer.fit_delay_model(frequencies.tolist(), coefficients.tolist())
    result = analyzer.fit_log_ripple_model(frequencies.tolist(), coefficients.tolist())
    assert result["base_delay_ns"] == pytest.approx(base_delay_ns, abs=0.001)
    assert result["ripple_delay_ns"] == pytest.approx(ripple_delay_ns, abs=0.002)
    assert result["ripple_log_amplitude"] == pytest.approx(abs(ripple), abs=0.002)
    assert result["heldout_phase_rms_deg"] < 0.1
    assert result["heldout_gain_rms_db"] < 0.01
    assert result["heldout_phase_rms_deg"] < delay_only["heldout_phase_rms_deg"]


def test_log_ripple_model_rejects_invalid_input() -> None:
    with pytest.raises(analyzer.CampaignError, match="eight points"):
        analyzer.fit_log_ripple_model([1, 2, 3, 4], [1 + 0j] * 4)
    with pytest.raises(analyzer.CampaignError, match="finite and non-zero"):
        analyzer.fit_log_ripple_model(list(range(8)), [1 + 0j] * 7 + [0j])


def test_selector_contract_checks_lease_guard_and_sequence() -> None:
    valid = {
        "applied_code": 0,
        "command_code": 0,
        "lease_active": True,
        "command_lease_ms": 5000,
        "remaining_lease_ms": 4700,
        "command_valid": True,
        "invalid_command": False,
        "guard_active": False,
        "status_flags": 3,
        "command_sequence": 42,
        "acknowledged_sequence": 42,
    }
    analyzer._require_selector(valid, code=0, lease_active=True, label="selected")
    for field, value in (
        ("guard_active", True),
        ("remaining_lease_ms", 0),
        ("acknowledged_sequence", 41),
    ):
        invalid = {**valid, field: value}
        with pytest.raises(analyzer.CampaignError):
            analyzer._require_selector(invalid, code=0, lease_active=True, label="selected")


def test_radio_readback_requires_active_source_and_utc_interval() -> None:
    mute = {"passed": True, "tx_gain_db": [-80.0, -80.0], "dds_scales": [0.0] * 8}
    valid = {
        "receiver_mute": mute,
        "source_tx_gain_db": [-40.0, -80.0],
        "source_dds_scales": [0.25, 0.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0],
        "started_utc": "2026-08-30T21:14:02+00:00",
        "completed_utc": "2026-08-30T21:14:03+00:00",
    }
    analyzer._require_radio_readback(valid, "capture")
    with pytest.raises(analyzer.CampaignError, match="source gain"):
        analyzer._require_radio_readback({**valid, "source_tx_gain_db": [-80.0, -80.0]}, "capture")


def test_pinned_identities_are_current_fixture() -> None:
    assert analyzer.RADIO_URI == "ip:192.168.1.15"
    assert analyzer.SOURCE_URI == "ip:192.168.1.173"
    assert analyzer.SOURCE_SERIAL == "104473b80a16000de6ff2000f8a6beca79"
    assert analyzer.SOURCE_COMMIT == "4a163644ab54c804680e2784da1f73dcb1c2167a"
