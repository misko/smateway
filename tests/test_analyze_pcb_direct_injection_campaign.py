from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_pcb_direct_injection_campaign.py"
REPORT_DIR = SCRIPT.parents[1] / "docs/pcb_direct_injection_calibration"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("pcb_direct_campaign_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def test_pinned_lattices_are_exact_and_interstitial() -> None:
    assert len(analyzer.FULL_FREQUENCIES_HZ) == 441
    assert analyzer.FULL_FREQUENCIES_HZ[0] == 500_000_000
    assert analyzer.FULL_FREQUENCIES_HZ[-1] == 6_000_000_000
    assert len(analyzer.HOLDOUT_FREQUENCIES_HZ) == 80
    assert set(analyzer.FULL_FREQUENCIES_HZ).isdisjoint(analyzer.HOLDOUT_FREQUENCIES_HZ)
    assert all(
        frequency == (left + right) // 2
        for frequency, left, right in zip(
            analyzer.HOLDOUT_FREQUENCIES_HZ,
            analyzer.FULL_FREQUENCIES_HZ[360:440],
            analyzer.FULL_FREQUENCIES_HZ[361:441],
            strict=True,
        )
    )


def test_pchip_is_exact_for_linear_values_and_rejects_extrapolation() -> None:
    x = np.asarray([0.0, 1.0, 2.0, 4.0])
    values = 1.25 + 2.5 * x
    query = np.asarray([0.25, 1.5, 3.0])

    assert np.allclose(analyzer.pchip_interpolate(x, values, query), 1.25 + 2.5 * query)
    with pytest.raises(ValueError, match="extrapolation"):
        analyzer.pchip_interpolate(x, values, np.asarray([-0.1]))


def test_logphase_pchip_closes_a_synthetic_linear_complex_log() -> None:
    frequency = np.asarray(analyzer.FULL_FREQUENCIES_HZ, dtype=np.float64)
    holdout_frequency = np.asarray(analyzer.HOLDOUT_FREQUENCIES_HZ, dtype=np.float64)

    def response(value: np.ndarray) -> np.ndarray:
        centered_ghz = (value - np.mean(frequency)) / 1e9
        magnitude_db = -3.0 + 0.7 * centered_ghz
        phase_rad = 0.4 - 2.0 * math.pi * 0.23 * centered_ghz
        return 10.0 ** (magnitude_db / 20.0) * np.exp(1j * phase_rad)

    metrics, _ = analyzer._interpolation_analysis(
        frequency,
        response(frequency),
        holdout_frequency,
        response(holdout_frequency),
    )

    assert metrics["logphase_pchip"]["phase_rms_deg"] < 1e-10
    assert metrics["logphase_pchip"]["magnitude_rms_db"] < 1e-10


def test_delay_fit_recovers_known_delay() -> None:
    frequency = np.asarray(analyzer.FULL_FREQUENCIES_HZ, dtype=np.float64)
    delay_ns = 0.187
    transfer = np.exp(-2j * np.pi * frequency * delay_ns * 1e-9)

    metrics, _, residual = analyzer._fit_delay(frequency, transfer)

    assert metrics["delay_ns"] == pytest.approx(delay_ns, abs=1e-12)
    assert np.max(np.abs(residual)) < 1e-9


def test_identity_leakage_matrix_has_zero_integer_grid_bias() -> None:
    wavelength = analyzer.LIGHT_SPEED_M_S / analyzer.QUALIFICATION_FREQUENCY_HZ
    radius = wavelength / (4.0 * math.sin(math.pi / 8.0))
    angles = np.deg2rad(np.arange(8) * 45.0 + 90.0)
    positions = np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))
    bearings = np.arange(0.0, 360.0, 15.0)

    bias = analyzer._uca8_leakage_bias_deg(
        np.eye(8, dtype=np.complex128),
        positions,
        frequency_hz=float(analyzer.QUALIFICATION_FREQUENCY_HZ),
        bearings_deg=bearings,
    )

    assert np.array_equal(bias, np.zeros_like(bearings))


def test_generated_lut_uses_geometric_mean_gauge() -> None:
    lut = json.loads((REPORT_DIR / "data/calibration-lut.json").read_text(encoding="utf-8"))

    assert lut["support_hz"] == [500_000_000, 6_000_000_000]
    assert lut["reference_gauge"] == "per_frequency_geometric_mean"
    assert lut["interpolation"]["extrapolation"] == "reject"
    assert len(lut["frequency_hz"]) == 441

    gains = np.asarray([lut["ports"][port]["correction_gain_db"] for port in analyzer.PORTS])
    phases = np.asarray(
        [lut["ports"][port]["correction_phase_unwrapped_deg"] for port in analyzer.PORTS]
    )
    assert np.max(np.abs(np.sum(gains, axis=0))) < 1e-10
    assert np.max(np.abs(np.sum(phases, axis=0))) < 1e-9


def test_generated_manifest_is_full_raw_replay() -> None:
    manifest = json.loads((REPORT_DIR / "data/campaign-manifest.json").read_text(encoding="utf-8"))

    assert manifest["raw_hashing_completed"] is True
    assert manifest["raw_replay_completed"] is True
    assert len(manifest["runs"]) == 25
    assert sum(run["observation_count"] for run in manifest["runs"]) == 4_528
    assert all(run["raw_sha256_included"] for run in manifest["runs"])
    assert all(run["raw_replay_completed"] for run in manifest["runs"])


def test_recommended_circular_layouts_are_alias_safe_and_pair_opposites() -> None:
    c6 = analyzer._circular_layout(
        analyzer.C6_RECOMMENDED_PORTS, analyzer.ARRAY_DESIGN_MAX_FREQUENCY_HZ
    )
    c8 = analyzer._circular_layout(
        analyzer.C8_RECOMMENDED_PORTS, analyzer.ARRAY_DESIGN_MAX_FREQUENCY_HZ
    )

    assert set(analyzer.C6_RECOMMENDED_PORTS).isdisjoint(analyzer.C6_OMITTED_PORTS)
    assert set(analyzer.C6_RECOMMENDED_PORTS) | set(analyzer.C6_OMITTED_PORTS) == set(
        analyzer.PORTS
    )
    assert set(analyzer.C8_RECOMMENDED_PORTS) == set(analyzer.PORTS)
    assert c6["adjacent_spacing_mm"] == pytest.approx(
        analyzer.LIGHT_SPEED_M_S / analyzer.ARRAY_DESIGN_MAX_FREQUENCY_HZ * 500.0
    )
    assert c8["adjacent_spacing_mm"] == pytest.approx(c6["adjacent_spacing_mm"])

    c6_ports = [item["port"] for item in c6["elements_clockwise_from_forward"]]
    c8_ports = [item["port"] for item in c8["elements_clockwise_from_forward"]]
    for first, second in (("ANT1", "ANT8"), ("ANT2", "ANT7"), ("ANT4", "ANT5")):
        assert (c6_ports.index(first) - c6_ports.index(second)) % 6 == 3
    for first, second in (
        ("ANT1", "ANT8"),
        ("ANT2", "ANT7"),
        ("ANT3", "ANT6"),
        ("ANT4", "ANT5"),
    ):
        assert (c8_ports.index(first) - c8_ports.index(second)) % 8 == 4


def test_generated_array_recommendation_and_figure_are_present() -> None:
    recommendation = json.loads(
        (REPORT_DIR / "data/array-layout-recommendations.json").read_text(encoding="utf-8")
    )
    figures = json.loads((REPORT_DIR / "data/figures-manifest.json").read_text(encoding="utf-8"))

    assert recommendation["c6"]["port_order_clockwise"] == list(analyzer.C6_RECOMMENDED_PORTS)
    assert recommendation["c8"]["port_order_clockwise"] == list(analyzer.C8_RECOMMENDED_PORTS)
    assert recommendation["mechanical_notes"]["phase_error_per_mm_at_6_ghz_deg"] == pytest.approx(
        7.2049844563
    )
    assert [item["filename"] for item in figures["figures"]] == list(analyzer.FIGURES)
