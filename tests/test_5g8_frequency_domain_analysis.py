import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_5g8_frequency_domain.py"
SPEC = importlib.util.spec_from_file_location("frequency_domain_rca_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def _synthetic_response(delay_ns: float = 3.2) -> tuple[list[int], list[complex]]:
    frequencies = list(range(3_600_000_000, 4_300_000_000, 100_000_000))
    offsets = np.asarray(frequencies, dtype=float) - frequencies[0]
    amplitude = 0.02 * np.exp(1j * 0.7)
    response = amplitude * np.exp(-2j * np.pi * offsets * delay_ns * 1e-9)
    return frequencies, response.tolist()


def _repeatability_document() -> dict[str, object]:
    source_runs: list[dict[str, object]] = [{"label": "baseline", "source_analyses": []}]
    for run_index in range(1, 21):
        source_runs.append(
            {
                "label": f"repeat-{run_index}",
                "source_analyses": [
                    {
                        "center_frequency_hz": frequency,
                        "artifact_id": f"{run_index:02d}{frequency:030d}"[-32:],
                        "analysis_sha256": f"{run_index + frequency:064x}"[-64:],
                    }
                    for frequency in analysis.HIGH_BAND_FREQUENCIES_HZ
                ],
            }
        )
    return {
        "schema": 1,
        "analysis_kind": "rotation0_broadband_repeatability",
        "scope": {
            "frequency_min_hz": 2_100_000_000,
            "frequency_max_hz": 5_800_000_000,
            "frequency_step_hz": 100_000_000,
        },
        "source_runs": source_runs,
    }


def _permutation_document() -> dict[str, object]:
    return {
        "schema": 1,
        "analysis_kind": "fast20_closed_loop_permutation_calibration",
        "frequency_results": [
            {
                "center_frequency_hz": 5_800_000_000,
                "fit_artifact_ids_by_rotation": {
                    "0": analysis.EXPECTED_PERMUTATION_ARTIFACTS["rotation_0_restored"],
                    "1": analysis.EXPECTED_PERMUTATION_ARTIFACTS["rotation_1"],
                    "2": analysis.EXPECTED_PERMUTATION_ARTIFACTS["rotation_2"],
                },
            }
        ],
    }


def _snapshot_document(
    repeatability: dict[str, object],
    repeatability_sha256: str,
    permutation_sha256: str,
) -> dict[str, object]:
    source_runs = repeatability["source_runs"]
    assert isinstance(source_runs, list)
    repeat_runs = []
    for run_index, source_run in enumerate(source_runs[1:], start=1):
        assert isinstance(source_run, dict)
        identities = source_run["source_analyses"]
        assert isinstance(identities, list)
        observations = []
        for frequency_index, identity in enumerate(identities):
            assert isinstance(identity, dict)
            angle = run_index * 0.001 + frequency_index * 0.4
            observations.append(
                {
                    "center_frequency_hz": identity["center_frequency_hz"],
                    "artifact_id": identity["artifact_id"],
                    "analysis_sha256": identity["analysis_sha256"],
                    "all_off": {
                        "real": 0.01 * math.cos(angle),
                        "imag": 0.01 * math.sin(angle),
                    },
                    "selected_subtracted_median_amplitude": 0.2,
                    "raw_selected_to_all_off_median_contrast_db": 25.0,
                }
            )
        repeat_runs.append({"label": source_run["label"], "observations": observations})
    permutation_rows = []
    for index, (label, artifact_id) in enumerate(
        analysis.EXPECTED_PERMUTATION_ARTIFACTS.items(), start=1
    ):
        permutation_rows.append(
            {
                "label": label,
                "artifact_id": artifact_id,
                "analysis_sha256": "a" * 64,
                "created_at": f"2026-08-27T20:0{index}:00Z",
                "all_off": {"real": 0.05, "imag": 0.01 * index},
                "selected_coherent_sum": {"real": 0.1 - index * 0.02, "imag": 0.03},
                "selected_subtracted_amplitudes": [0.1] * 8,
            }
        )
    return {
        "schema": 1,
        "evidence_kind": "5g8_frequency_domain_compact_observation_snapshot",
        "sources": {
            "repeatability": {"sha256": repeatability_sha256},
            "permutation": {"sha256": permutation_sha256},
        },
        "repeat_runs": repeat_runs,
        "frequency_selected_subtracted_mean_amplitudes": [
            {"center_frequency_hz": frequency, "amplitudes": [0.1] * 8}
            for frequency in analysis.HIGH_BAND_FREQUENCIES_HZ
        ],
        "permutation_5g8": permutation_rows,
    }


def test_single_delay_recovers_synthetic_delay_modulo_alias_period() -> None:
    frequencies, response = _synthetic_response(delay_ns=13.2)

    result = analysis.fit_single_delay(frequencies, response, grid_step_ps=1.0)

    assert result["delay_alias_period_ns"] == pytest.approx(10.0)
    assert result["delay_ns_modulo_alias_period"] == pytest.approx(3.2, abs=0.001)
    assert result["complex_nrmse"] < 1e-10
    assert result["phase_error_rms_deg"] < 1e-8


def test_single_delay_rejects_nonuniform_frequency_grid() -> None:
    frequencies, response = _synthetic_response()
    frequencies[3] += 1_000_000

    with pytest.raises(analysis.FrequencyDomainAnalysisError, match="uniform"):
        analysis.fit_single_delay(frequencies, response)


def test_single_delay_reports_large_error_for_frequency_dependent_magnitude() -> None:
    frequencies, response = _synthetic_response()
    response = [
        value * scale for value, scale in zip(response, np.linspace(0.2, 2.0, 7), strict=True)
    ]

    result = analysis.fit_single_delay(frequencies, response, grid_step_ps=1.0)

    assert result["complex_nrmse"] > 0.4
    assert result["magnitude_error_rms_db"] > 5.0


def test_hankel_rank_diagnostic_separates_one_and_two_paths() -> None:
    frequencies, one_path = _synthetic_response()
    offsets = np.asarray(frequencies, dtype=float) - frequencies[0]
    second_path = 0.013 * np.exp(-2j * np.pi * offsets * 5.1e-9 + 0.3j)
    two_path = np.asarray(one_path) + second_path

    rank_one = analysis.hankel_diagnostics(one_path)
    rank_two = analysis.hankel_diagnostics(two_path.tolist())

    assert rank_one["rank_one_explained_energy_fraction"] > 1.0 - 1e-12
    assert rank_one["rank_one_relative_frobenius_residual"] < 1e-6
    assert rank_two["rank_one_relative_frobenius_residual"] > 0.05


def test_selector_bound_matches_datasheet_conditioned_voltage_sum() -> None:
    selected = [0.1] * 8
    expected = sum(
        amplitude * 10 ** (-(isolation - insertion_loss) / 20.0)
        for amplitude, isolation, insertion_loss in zip(
            selected,
            analysis.PE42482_MINIMUM_ISOLATION_DB,
            analysis.PE42482_MAXIMUM_INSERTION_LOSS_DB,
            strict=True,
        )
    )

    assert analysis.selector_coherent_bound(selected) == pytest.approx(expected)


@pytest.mark.parametrize("selected", ([0.1] * 7, [0.1] * 7 + [-0.1]))
def test_selector_bound_fails_closed_on_invalid_paths(selected: list[float]) -> None:
    with pytest.raises(analysis.FrequencyDomainAnalysisError, match="selector bound"):
        analysis.selector_coherent_bound(selected)


def test_paired_summary_requires_the_frozen_twenty_run_design() -> None:
    result = analysis.paired_summary([float(index) for index in range(20)])

    assert result["count"] == 20
    assert result["mean"] == pytest.approx(9.5)
    interval = result["mean_95_percent_confidence_interval"]
    assert isinstance(interval, list)
    assert interval[0] < 9.5 < interval[1]
    with pytest.raises(analysis.FrequencyDomainAnalysisError, match="exactly 20"):
        analysis.paired_summary([1.0] * 19)


def test_committed_snapshot_loader_validates_source_hashes_and_identities(
    tmp_path: Path,
) -> None:
    repeatability = _repeatability_document()
    permutation = _permutation_document()
    repeatability_path = tmp_path / "repeatability.json"
    permutation_path = tmp_path / "permutation.json"
    repeatability_path.write_text(json.dumps(repeatability), encoding="utf-8")
    permutation_path.write_text(json.dumps(permutation), encoding="utf-8")
    snapshot = _snapshot_document(
        repeatability,
        analysis._sha256(repeatability_path),
        analysis._sha256(permutation_path),
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    loaded = analysis._load_snapshot(
        snapshot_path,
        repeatability_path,
        repeatability,
        permutation_path,
        permutation,
    )

    assert len(loaded["runs"]) == 20
    assert len(loaded["selected_by_frequency"]) == 23
    assert len(loaded["permutation_rows"]) == 3

    permutation_path.write_text(json.dumps({**permutation, "schema": 2}), encoding="utf-8")
    with pytest.raises(analysis.FrequencyDomainAnalysisError, match="source hash differs"):
        analysis._load_snapshot(
            snapshot_path,
            repeatability_path,
            repeatability,
            permutation_path,
            permutation,
        )


def test_repeatability_validator_rejects_an_incomplete_high_band() -> None:
    repeatability = _repeatability_document()
    source_runs = repeatability["source_runs"]
    assert isinstance(source_runs, list)
    first_repeat = source_runs[1]
    assert isinstance(first_repeat, dict)
    analyses = first_repeat["source_analyses"]
    assert isinstance(analyses, list)
    analyses.pop()

    with pytest.raises(analysis.FrequencyDomainAnalysisError, match="lacks high-band"):
        analysis._validate_repeatability(repeatability)


def test_analysis_preserves_alias_and_bound_caveats() -> None:
    repeatability = _repeatability_document()
    snapshot_document = _snapshot_document(repeatability, "a" * 64, "b" * 64)
    loaded_runs = []
    for raw_run in snapshot_document["repeat_runs"]:
        assert isinstance(raw_run, dict)
        observations = []
        for raw_observation in raw_run["observations"]:
            assert isinstance(raw_observation, dict)
            all_off = raw_observation["all_off"]
            assert isinstance(all_off, dict)
            observations.append(
                {
                    "center_frequency_hz": raw_observation["center_frequency_hz"],
                    "all_off": complex(all_off["real"], all_off["imag"]),
                    "selected_median_amplitude": raw_observation[
                        "selected_subtracted_median_amplitude"
                    ],
                    "median_raw_contrast_db": raw_observation[
                        "raw_selected_to_all_off_median_contrast_db"
                    ],
                }
            )
        loaded_runs.append({"label": raw_run["label"], "observations": observations})
    selected = {
        row["center_frequency_hz"]: row["amplitudes"]
        for row in snapshot_document["frequency_selected_subtracted_mean_amplitudes"]
    }
    permutations = []
    for raw_row in snapshot_document["permutation_5g8"]:
        assert isinstance(raw_row, dict)
        all_off = raw_row["all_off"]
        selected_sum = raw_row["selected_coherent_sum"]
        assert isinstance(all_off, dict) and isinstance(selected_sum, dict)
        permutations.append(
            {
                "label": raw_row["label"],
                "artifact_id": raw_row["artifact_id"],
                "created_at": raw_row["created_at"],
                "all_off": complex(all_off["real"], all_off["imag"]),
                "selected_sum": complex(selected_sum["real"], selected_sum["imag"]),
                "selected_amplitudes": raw_row["selected_subtracted_amplitudes"],
            }
        )

    result = analysis.analyze(
        {
            "runs": loaded_runs,
            "selected_by_frequency": selected,
            "permutation_rows": permutations,
        }
    )

    assert result["scope"]["delay_alias_period_ns"] == pytest.approx(10.0)
    assert "modulo 10 ns" in result["scope"]["delay_interpretation"]
    assert (
        result["selector_bound"]["status"]
        == "datasheet_conditioned_planning_bound_not_board_measurement"
    )
    selector_rows = result["selector_bound"]["frequency_results"]
    assert selector_rows[0]["datasheet_specification_band"] == "2-4 GHz"
    assert selector_rows[-1]["datasheet_specification_band"] == "4-6 GHz"
    assert result["conclusions"]["physical_root_cause_uniquely_identified"] is False
