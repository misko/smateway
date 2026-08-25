from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import pi
from pathlib import Path

import numpy as np
import pytest

from smateway.reference_locus import (
    SPEED_OF_LIGHT_M_S,
    ReferenceLocusError,
    ReferenceTransferCapture,
    aggregate_reference_transfers,
    analyze_reference_locus,
    collapse_frequency_profiles,
    fit_range_difference,
    sample_hyperbola_locus,
    signed_range_difference_mm,
)

STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 9))
ANTENNAS_MM = np.asarray(
    (
        (-15.0, -62.5),
        (-30.0, -62.5),
        (-75.0, -4.5),
        (-75.0, 13.5),
        (75.0, 13.5),
        (75.0, -4.5),
        (30.0, -62.5),
        (15.0, -62.5),
    ),
    dtype=np.float64,
)
TX1_MM = (-250.0, 0.0)
TX2_MM = (250.0, 0.0)
RX1_MM = (80.0, 230.0)
FREQUENCIES_HZ = (2_400_100_000.0, 2_409_100_000.0, 2_423_100_000.0, 2_440_100_000.0)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/analyze_reference_locus.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "reference_locus_script_under_test", SCRIPT_PATH
)
assert SCRIPT_SPEC is not None
assert SCRIPT_SPEC.loader is not None
reference_locus_script = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = reference_locus_script
SCRIPT_SPEC.loader.exec_module(reference_locus_script)


def test_default_geometry_loader_uses_board_centred_whip_axes() -> None:
    geometry = Path(__file__).resolve().parents[1] / "profiles/phase20-v1/array_geometry.json"

    names, positions, provenance = reference_locus_script._load_geometry(geometry)

    assert names == STATE_NAMES
    np.testing.assert_allclose(positions, ANTENNAS_MM)
    assert provenance["analysis_coordinate_origin"] == "board outline center"


def _synthetic_captures(
    *,
    common_rotation_deg: float = 0.0,
    tx_source_rotation_deg: float = 0.0,
    incoherent_pair_phase: bool = False,
) -> tuple[ReferenceTransferCapture, ...]:
    tx1 = np.asarray(TX1_MM)
    tx2 = np.asarray(TX2_MM)
    rx1 = np.asarray(RX1_MM)
    range1 = float(np.linalg.norm(rx1 - tx1))
    range2 = float(np.linalg.norm(rx1 - tx2))
    array_range1 = np.linalg.norm(ANTENNAS_MM - tx1, axis=1)
    array_range2 = np.linalg.norm(ANTENNAS_MM - tx2, axis=1)
    captures: list[ReferenceTransferCapture] = []
    for frequency_index, frequency_hz in enumerate(FREQUENCIES_HZ):
        wave_number_per_mm = 2.0 * pi * frequency_hz / (SPEED_OF_LIGHT_M_S * 1000.0)
        for repeat in range(3):
            state_gain = (
                np.linspace(0.8, 1.2, len(STATE_NAMES))
                * np.exp(
                    1j
                    * np.deg2rad(
                        np.arange(len(STATE_NAMES)) * 23.0
                        + repeat * 37.0
                        + frequency_index * 11.0
                        + common_rotation_deg
                    )
                )
            )
            source1 = np.exp(
                1j
                * np.deg2rad(
                    tx_source_rotation_deg + repeat * 71.0 + frequency_index * 19.0
                )
            )
            source2 = np.exp(
                1j
                * np.deg2rad(
                    -2.0 * tx_source_rotation_deg + repeat * 43.0 - frequency_index * 31.0
                )
            )
            tx1_rx1 = source1 / range1 * np.exp(-1j * wave_number_per_mm * range1)
            tx1_rx2 = (
                source1
                * state_gain
                / array_range1
                * np.exp(-1j * wave_number_per_mm * array_range1)
            )
            tx2_rx1 = source2 / range2 * np.exp(-1j * wave_number_per_mm * range2)
            tx2_rx2 = (
                source2
                * state_gain
                / array_range2
                * np.exp(-1j * wave_number_per_mm * array_range2)
            )
            tx1_transfer = tx1_rx2 / tx1_rx1
            tx2_transfer = tx2_rx2 / tx2_rx1
            if incoherent_pair_phase:
                tx2_transfer *= np.exp(1j * 2.0 * pi * repeat / 3.0)
            for tx_channel, phasor in ((0, tx1_transfer), (1, tx2_transfer)):
                captures.append(
                    ReferenceTransferCapture(
                        artifact_id=f"f{frequency_index}-r{repeat}-tx{tx_channel}",
                        pair_id=f"round-{repeat}",
                        tx_channel=tx_channel,
                        carrier_frequency_hz=frequency_hz,
                        state_names=STATE_NAMES,
                        transfer_phasor=phasor,
                        phase_standard_error_deg=np.full(len(STATE_NAMES), 0.5),
                        valid_mask=np.ones(len(STATE_NAMES), dtype=np.bool_),
                    )
                )
    return tuple(captures)


def test_recovers_signed_range_difference_and_reports_only_a_hyperbola() -> None:
    expected_delta = signed_range_difference_mm(RX1_MM, TX1_MM, TX2_MM)

    result = analyze_reference_locus(
        _synthetic_captures(),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
        bounds_mm=(-600.0, 600.0, -500.0, 500.0),
        systematic_phase_standard_error_deg=1.0,
    )

    assert result.fit.map_range_difference_mm == pytest.approx(expected_delta, abs=0.11)
    assert result.identifiability_rank == 1
    assert result.profiles.frequency_count == len(FREQUENCIES_HZ)
    assert np.all(result.measurements.pair_count == 3)
    assert len(result.leave_one_frequency_out) == len(FREQUENCIES_HZ)
    assert len(result.leave_one_state_out) == len(STATE_NAMES)
    assert result.hyperbola_points_mm.shape[0] > 20
    sampled_delta = np.asarray(
        [
            signed_range_difference_mm(point, TX1_MM, TX2_MM)
            for point in result.hyperbola_points_mm[::20]
        ]
    )
    assert sampled_delta == pytest.approx(result.fit.map_range_difference_mm, abs=1e-8)
    assert result.weak_amplitude.usable is True
    assert any(
        np.linalg.norm(np.asarray(candidate) - np.asarray(RX1_MM)) < 0.5
        for candidate in result.weak_amplitude.candidate_positions_mm
    )


def test_ratio_is_invariant_to_common_receiver_gain_and_independent_tx_phase() -> None:
    baseline = aggregate_reference_transfers(
        _synthetic_captures(),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
    )
    rotated = aggregate_reference_transfers(
        _synthetic_captures(common_rotation_deg=137.0, tx_source_rotation_deg=223.0),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
    )

    assert rotated.geometry_corrected_phasor == pytest.approx(
        baseline.geometry_corrected_phasor,
        abs=1e-12,
    )
    assert rotated.corrected_amplitude_ratio == pytest.approx(
        baseline.corrected_amplitude_ratio,
        abs=1e-12,
    )


def test_pair_phase_incoherence_fails_closed() -> None:
    measurements = aggregate_reference_transfers(
        _synthetic_captures(incoherent_pair_phase=True),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
        minimum_pair_coherence=0.7,
    )

    assert not np.any(measurements.valid_mask)
    with pytest.raises(ReferenceLocusError, match="no frequency"):
        collapse_frequency_profiles(measurements)


def test_frequency_fit_rejects_insufficient_unique_frequencies() -> None:
    measurements = aggregate_reference_transfers(
        _synthetic_captures(),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
    )
    profiles = collapse_frequency_profiles(measurements)
    two_frequency_profiles = replace(
        profiles,
        carrier_frequency_hz=profiles.carrier_frequency_hz[:2],
        phasor=profiles.phasor[:2],
        phase_standard_error_deg=profiles.phase_standard_error_deg[:2],
        state_coherence=profiles.state_coherence[:2],
        state_phase_rms_deg=profiles.state_phase_rms_deg[:2],
        valid_state_count=profiles.valid_state_count[:2],
        source_frequency_index=profiles.source_frequency_index[:2],
    )

    with pytest.raises(ReferenceLocusError, match="at least 3"):
        fit_range_difference(two_frequency_profiles, anchor_separation_mm=500.0)


def test_hyperbola_samples_have_the_requested_sign_and_rank_one_family() -> None:
    positive_delta = 120.0
    points = sample_hyperbola_locus(
        TX1_MM,
        TX2_MM,
        positive_delta,
        bounds_mm=(-1000.0, 1000.0, -1000.0, 1000.0),
    )

    assert points.shape[0] > 100
    assert np.ptp(points[:, 1]) > 100.0
    actual = np.asarray(
        [signed_range_difference_mm(point, TX1_MM, TX2_MM) for point in points]
    )
    assert actual == pytest.approx(positive_delta, abs=1e-8)
    # Positive d(TX2)-d(TX1) must select the TX1/left branch for these anchors.
    assert np.all(points[:, 0] < 0.0)


def _write_schema_one_document(
    path: Path,
    capture: ReferenceTransferCapture,
    *,
    created_at: datetime,
) -> None:
    states = []
    for index, name in enumerate(capture.state_names):
        phasor = capture.transfer_phasor[index]
        states.append(
            {
                "name": name,
                "quality_passed": True,
                "transfer_approximate_phase_standard_error_deg": float(
                    capture.phase_standard_error_deg[index]
                ),
                "all_off_subtracted_rx2_over_rx1": {
                    "phasor": {"real": float(phasor.real), "imag": float(phasor.imag)},
                    "repeat_quality_passed": True,
                },
            }
        )
    document = {
        "schema": 1,
        "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
        "artifact": {
            "artifact_id": capture.artifact_id,
            "created_at": created_at.isoformat(),
        },
        "aggregation_key": {
            "artifact_id": capture.artifact_id,
            "tx_channel": capture.tx_channel,
            "carrier_frequency_hz": capture.carrier_frequency_hz,
        },
        "quality_gate": {"passed": True},
        "transfer": {"states": states},
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_schema_one_script_adapter_preserves_identifiability_warning(tmp_path: Path) -> None:
    loaded = []
    start = datetime(2026, 8, 25, tzinfo=UTC)
    for index, capture in enumerate(_synthetic_captures()):
        path = tmp_path / f"{capture.artifact_id}.json"
        _write_schema_one_document(path, capture, created_at=start + timedelta(seconds=index))
        loaded.append(reference_locus_script._load_capture(path))
    paired, pairing = reference_locus_script._assign_pair_ids(tuple(loaded), manifest=None)
    analysis = analyze_reference_locus(
        tuple(item.capture for item in paired),
        antenna_positions_mm=ANTENNAS_MM,
        tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
        bounds_mm=(-600.0, 600.0, -500.0, 500.0),
        systematic_phase_standard_error_deg=1.0,
    )

    report = reference_locus_script._analysis_document(
        analysis,
        loaded=paired,
        pairing=pairing,
        geometry_provenance={"path": "synthetic", "sha256": "0" * 64},
        antenna_positions_mm=ANTENNAS_MM,
        thresholds={"systematic_phase_standard_error_deg": 1.0},
        source_commit="1" * 40,
    )

    assert report["analysis_kind"] == "paired_tx_rx1_range_difference_locus"
    assert report["status"] == "passed"
    assert report["identifiability"]["geometric_rank"] == 1
    assert report["identifiability"]["unique_planar_position_identified"] is False
    assert report["weak_amplitude_diagnostic"]["primary_fit_uses_amplitude"] is False
    assert "cancels" in report["observable"]["pcb_path_handling"]
    assert len(report["measurements"]["frequency_rows"]) == len(FREQUENCIES_HZ)
    json.dumps(report, allow_nan=False)
