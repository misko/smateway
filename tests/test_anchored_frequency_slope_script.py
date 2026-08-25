import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from smateway.frequency_slope_localization import (
    AnchoredArrayGeometry,
    predict_double_relative_phase_deg,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_anchored_frequency_slope.py"
SPEC = importlib.util.spec_from_file_location("anchored_frequency_slope_script_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 9))
POSITIONS_MM = np.asarray(
    (
        (-15.0, -62.5),
        (-30.0, -62.5),
        (-75.0, -4.5),
        (-75.0, 13.5),
        (75.0, 13.5),
        (75.0, -4.5),
        (30.0, -62.5),
        (15.0, -62.5),
    )
)
FREQUENCIES_HZ = np.asarray(
    (2.31e9, 2.47e9, 2.68e9, 2.94e9, 3.27e9, 3.69e9, 4.18e9, 4.74e9, 5.37e9, 5.8e9)
)
TX1_MM = np.asarray((-26.5, 315.7))
TX2_MM = np.asarray((161.2, -262.7))


def _source_document() -> dict[str, object]:
    geometry = AnchoredArrayGeometry(POSITIONS_MM, np.asarray((0.0, 0.0)))
    phase = predict_double_relative_phase_deg(
        geometry,
        FREQUENCIES_HZ,
        fixed_tx1_position_mm=TX1_MM,
        tx2_position_mm=TX2_MM,
    )
    phase += np.asarray((0.0, 47.0, -83.0, 129.0, -151.0, 31.0, 174.0, -66.0))[None, :]
    rows = []
    for frequency_hz, values in zip(FREQUENCIES_HZ, phase, strict=True):
        rows.append(
            {
                "center_frequency_hz": int(frequency_hz),
                "carrier_frequency_hz": float(frequency_hz),
                "state_names": list(STATE_NAMES),
                "valid_mask": [True] * 8,
                "circular_mean_double_relative_phase_deg": values.tolist(),
                "circular_repeat_standard_deviation_deg": [0.0] + [3.0] * 7,
                "aggregate_analyzer_standard_error_deg": [0.0] + [4.0] * 7,
                # The anchored wrapper must not reuse this old direct-path floor.
                "combined_phase_standard_deviation_deg": [99.0] * 8,
            }
        )
    return {
        "schema": 1,
        "analysis_kind": script.EXPECTED_ANALYSIS_KIND,
        "created_at": "2026-08-25T12:00:00+00:00",
        "source": {
            "manifest_path": "/synthetic/manifest.json",
            "manifest_sha256": "a" * 64,
            "geometry_sha256": "b" * 64,
            "board_id": "synthetic-board",
            "radio_serial": "synthetic-radio",
        },
        "localization": {
            "selected_state_names": list(STATE_NAMES),
            "geometry": {
                "inference_center_mm": [0.0, 0.0],
                "selected_antenna_positions_mm": {
                    name: position.tolist()
                    for name, position in zip(STATE_NAMES, POSITIONS_MM, strict=True)
                },
            },
            "frequency_profile_rows": rows,
        },
    }


def test_cli_extracts_statistical_slope_inputs_and_writes_bounded_posterior(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "aggregate.json"
    output = tmp_path / "anchored.json"
    source.write_text(json.dumps(_source_document()), encoding="utf-8")

    assert (
        script.main(
            (
                "--analysis",
                str(source),
                "--tx1-anchor-x-mm",
                str(TX1_MM[0]),
                "--tx1-anchor-y-mm",
                str(TX1_MM[1]),
                "--output",
                str(output),
                "--sample-count",
                "6000",
                "--seed",
                "17",
                "--systematic-phase-std-deg",
                "2.0",
            )
        )
        == 0
    )

    status = json.loads(capsys.readouterr().out)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert status["output"] == str(output)
    assert document["analysis_kind"] == "anchored_multifrequency_tx2_phase_slope_localization"
    assert document["source"]["analysis_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert document["source"]["upstream_provenance"]["board_id"] == "synthetic-board"
    configuration = document["analysis_configuration"]
    assert configuration["systematic_phase_standard_deviation_deg"] == 2.0
    assert configuration["systematic_floor_application_count"] == 1
    assert not configuration["upstream_direct_path_systematic_floors_used"]
    expected_frequencies = FREQUENCIES_HZ.astype(np.int64).tolist()
    assert configuration["source_center_frequencies_hz"] == expected_frequencies
    assert configuration["excluded_center_frequencies_hz"] == []
    assert configuration["used_center_frequencies_hz"] == expected_frequencies
    assert document["source"]["frequency_selection"] == {
        "source_center_frequencies_hz": expected_frequencies,
        "excluded_center_frequencies_hz": [],
        "used_center_frequencies_hz": expected_frequencies,
    }
    extracted_row = document["inputs"]["frequency_profile_rows"][0]
    assert extracted_row["statistical_phase_standard_deviation_deg"][0] == 0.1
    assert extracted_row["statistical_phase_standard_deviation_deg"][1:] == [5.0] * 7
    posterior = document["localization"]["posterior"]
    assert posterior["sample_count"] == 6000
    assert posterior["output_particles"]["source_particle_count"] == 6000
    assert posterior["output_particles"]["maximum_output_count"] == 5000
    assert posterior["output_particles"]["output_particle_count"] <= 5000
    assert posterior["map_residuals"]["state_names"] == list(STATE_NAMES[1:])
    map_position = np.asarray(posterior["map"]["tx2_position_mm"])
    assert np.linalg.norm(map_position - TX2_MM) < 20.0
    assert posterior["map_residuals"]["overall_weighted_rms_deg"] < 1.0


def test_repeatable_frequency_exclusion_filters_measurements_and_provenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "aggregate.json"
    output = tmp_path / "lofo.json"
    source.write_text(json.dumps(_source_document()), encoding="utf-8")
    excluded = (int(FREQUENCIES_HZ[1]), int(FREQUENCIES_HZ[7]))

    assert (
        script.main(
            (
                "--analysis",
                str(source),
                "--tx1-anchor-x-mm",
                str(TX1_MM[0]),
                "--tx1-anchor-y-mm",
                str(TX1_MM[1]),
                "--output",
                str(output),
                "--sample-count",
                "1000",
                "--exclude-center-frequency-hz",
                str(excluded[0]),
                "--exclude-center-frequency-hz",
                str(excluded[1]),
            )
        )
        == 0
    )

    status = json.loads(capsys.readouterr().out)
    document = json.loads(output.read_text(encoding="utf-8"))
    source_frequencies = FREQUENCIES_HZ.astype(np.int64).tolist()
    used = [value for value in source_frequencies if value not in excluded]
    assert status["excluded_center_frequencies_hz"] == list(excluded)
    assert status["used_center_frequencies_hz"] == used
    for selection in (
        document["source"]["frequency_selection"],
        document["analysis_configuration"],
        document["inputs"],
    ):
        assert selection["source_center_frequencies_hz"] == source_frequencies
        assert selection["excluded_center_frequencies_hz"] == list(excluded)
        assert selection["used_center_frequencies_hz"] == used
    assert document["inputs"]["source_frequency_profile_count"] == 10
    assert document["inputs"]["frequency_profile_count"] == 8
    assert [
        row["center_frequency_hz"] for row in document["inputs"]["frequency_profile_rows"]
    ] == used
    residuals = document["localization"]["posterior"]["map_residuals"]
    assert len(residuals["residual_phase_deg"]) == len(used)


def test_extraction_rejects_duplicate_profile_frequency_and_changed_state_order() -> None:
    duplicate = _source_document()
    localization = duplicate["localization"]
    assert isinstance(localization, dict)
    rows = localization["frequency_profile_rows"]
    assert isinstance(rows, list)
    assert isinstance(rows[1], dict)
    assert isinstance(rows[0], dict)
    rows[1]["carrier_frequency_hz"] = rows[0]["carrier_frequency_hz"]
    with pytest.raises(script.AnalysisDocumentError, match="must be unique"):
        script._extract_inputs(duplicate)

    changed_order = _source_document()
    changed_localization = changed_order["localization"]
    assert isinstance(changed_localization, dict)
    changed_rows = changed_localization["frequency_profile_rows"]
    assert isinstance(changed_rows, list)
    assert isinstance(changed_rows[2], dict)
    changed_rows[2]["state_names"] = list(reversed(STATE_NAMES))
    with pytest.raises(script.AnalysisDocumentError, match="differs from selected state order"):
        script._extract_inputs(changed_order)


def test_exclusions_reject_duplicates_unknown_values_and_fewer_than_three_profiles() -> None:
    first = int(FREQUENCIES_HZ[0])
    with pytest.raises(script.AnalysisDocumentError, match="must not contain duplicates"):
        script._extract_inputs(_source_document(), (first, first))
    with pytest.raises(script.AnalysisDocumentError, match="absent from the source"):
        script._extract_inputs(_source_document(), (123_456_789,))
    with pytest.raises(script.AnalysisDocumentError, match="at least three.*remain"):
        script._extract_inputs(
            _source_document(),
            tuple(int(value) for value in FREQUENCIES_HZ[:-2]),
        )

    non_integer_center = _source_document()
    localization = non_integer_center["localization"]
    assert isinstance(localization, dict)
    rows = localization["frequency_profile_rows"]
    assert isinstance(rows, list)
    assert isinstance(rows[0], dict)
    rows[0]["center_frequency_hz"] = float(FREQUENCIES_HZ[0])
    with pytest.raises(script.AnalysisDocumentError, match="must be an integer"):
        script._extract_inputs(non_integer_center)
