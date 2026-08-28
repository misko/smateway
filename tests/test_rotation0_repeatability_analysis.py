import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_rotation0_repeatability.py"
SPEC = importlib.util.spec_from_file_location("rotation0_repeatability_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def _state(antenna_index: int, run_offset_deg: float = 0.0) -> dict[str, object]:
    phase = antenna_index * 10.0 + run_offset_deg
    return {
        "name": f"ANT{antenna_index}",
        "quality_passed": True,
        "quality_rejection_reasons": [],
        "transfer_detection_snr_db": 50.0,
        "all_off_subtracted_rx2_over_rx1": {
            "phase_deg": phase,
            "amplitude": 0.1 * antenna_index,
            "cycle_phase_std_deg": 0.2,
            "cycle_coherence": 0.99,
        },
        "raw_rx2_over_rx1": {"amplitude": 0.1 * antenna_index + 0.01},
    }


def _manifest(tmp_path: Path, run_id: str, run_offset_deg: float = 0.0) -> Path:
    attempts = []
    for plan_index, frequency_hz in enumerate(analysis.FREQUENCIES_HZ):
        artifact_id = f"{plan_index + 1:032x}"
        artifact_dir = tmp_path / run_id / artifact_id
        artifact_dir.mkdir(parents=True)
        artifact_sha256 = f"{plan_index + 1:064x}"
        analysis_path = artifact_dir / "fast20-reference-transfer.json"
        document = {
            "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
            "aggregation_key": {"center_frequency_hz": frequency_hz},
            "artifact": {"artifact_id": artifact_id, "sha256": artifact_sha256},
            "capture": {"adc_headroom_admission": {"passed": True}},
            "transfer": {
                "continuity_verified": True,
                "complete_cycle_count": 25,
                "reference_valid_bin_fraction": 1.0,
                "alignment_score": 1.0,
                "alignment_even_odd_agreement": 1.0,
                "marker_phase_ms": 10.0,
                "all_off": {"raw_rx2_over_rx1": {"amplitude": 0.01}},
                "states": [_state(index, run_offset_deg) for index in range(1, 9)],
            },
        }
        analysis_path.write_text(json.dumps(document), encoding="utf-8")
        attempts.append(
            {
                "plan_index": plan_index,
                "stage": "rotation0",
                "status": "complete",
                "center_frequency_hz": frequency_hz,
                "mapping": analysis.EXPECTED_MAPPING,
                "artifact_id": artifact_id,
                "quality_result": {"analysis_path": str(analysis_path)},
                "post_mute": {"status": "passed"},
            }
        )
    manifest = {
        "schema": 1,
        "experiment_kind": "fast20_fully_conducted_broadband_board_calibration",
        "run_id": run_id,
        "configuration": {
            "frequencies_hz": list(analysis.FREQUENCIES_HZ),
            "storage_medium": "raspberry_pi_local_filesystem",
            "pluto_onboard_storage_used": False,
            "board_id": "board-a",
            "serial": "pluto-a",
            "profile_id": "fast20-v1",
            "profile_contract_sha256": "a" * 64,
            "firmware_binary_sha256": "b" * 64,
        },
        "attempts": attempts,
        "final_mute": {"status": "passed"},
    }
    path = tmp_path / run_id / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_relative_phase_rejects_capture_wide_phase_rotation() -> None:
    states_a = {
        f"ANT{index}": {"phase_deg": index * 10.0, "amplitude": index * 0.1}
        for index in range(1, 9)
    }
    states_b = {
        f"ANT{index}": {"phase_deg": index * 10.0 + 137.0, "amplitude": index * 0.1}
        for index in range(1, 9)
    }

    phase_a, amplitude_a = analysis._relative_measurement(states_a, "ANT3")
    phase_b, amplitude_b = analysis._relative_measurement(states_b, "ANT3")

    assert phase_a == pytest.approx(-50.0)
    assert phase_b == pytest.approx(phase_a)
    assert amplitude_b == pytest.approx(amplitude_a)


def test_analysis_accepts_baseline_plus_five_complete_repeats(tmp_path: Path) -> None:
    baseline = analysis._load_run("baseline", _manifest(tmp_path, "baseline"))
    repeats = [
        analysis._load_run(
            f"repeat-{index}",
            _manifest(tmp_path, f"repeat-{index}", run_offset_deg=index * 23.0),
        )
        for index in range(1, 6)
    ]

    result = analysis.analyze(baseline, repeats)

    assert result["acquisition_integrity"]["repeat_capture_count"] == 190
    assert result["pass_summary"]["repeat_condition_pass_counts"] == [38] * 5
    assert result["high_band_repeatability"]["all_repeat_conditions_passed"] is True
    assert result["high_band_repeatability"]["relative_phase_std_max_deg"] < 1e-6
    assert len(result["raw_isolation"]["repeatable_and_operational_frequencies_hz"]) == 38
    assert result["alignment_diagnostics"]["unambiguous_alignment_capture_count"] == 190
    assert len(result["frequency_results"]) == 38
    assert len(result["relative_delay_fits"]) == 7


def test_analysis_accepts_ten_complete_repeats(tmp_path: Path) -> None:
    baseline = analysis._load_run("baseline", _manifest(tmp_path, "baseline"))
    repeats = [
        analysis._load_run(
            f"repeat-{index}",
            _manifest(tmp_path, f"repeat-{index}", run_offset_deg=index * 17.0),
        )
        for index in range(1, 11)
    ]

    result = analysis.analyze(baseline, repeats)

    assert result["acquisition_integrity"]["repeat_capture_count"] == 380
    assert result["pass_summary"]["repeat_condition_pass_counts"] == [38] * 10
    assert len(result["pass_summary"]["repeat_run_results"]) == 10
    assert result["temporal_drift"]["pass_count"] == 10
    assert result["temporal_drift"]["path_frequency_count"] == 23 * 7
    assert result["temporal_drift"]["phase_absolute_first_to_last_deg"]["maximum"] < 1e-6


def test_analysis_quarantines_indeterminate_alignment_from_rf_statistics(tmp_path: Path) -> None:
    baseline = analysis._load_run("baseline", _manifest(tmp_path, "baseline"))
    repeat_paths = [_manifest(tmp_path, f"repeat-{index}") for index in range(1, 3)]
    manifest = json.loads(repeat_paths[0].read_text(encoding="utf-8"))
    analysis_path = Path(manifest["attempts"][0]["quality_result"]["analysis_path"])
    document = json.loads(analysis_path.read_text(encoding="utf-8"))
    document["transfer"]["alignment_score"] = 0.90
    analysis_path.write_text(json.dumps(document), encoding="utf-8")
    repeats = [
        analysis._load_run(f"repeat-{index}", path)
        for index, path in enumerate(repeat_paths, start=1)
    ]

    result = analysis.analyze(baseline, repeats)

    diagnostics = result["alignment_diagnostics"]
    assert diagnostics["indeterminate_alignment_capture_count"] == 1
    assert diagnostics["indeterminate_alignment_quality_pass_count"] == 1
    assert result["frequency_results"][0]["unambiguous_alignment_capture_count"] == 1
    assert result["frequency_results"][0]["indeterminate_alignment_capture_count"] == 1


def test_loader_rejects_non_rotation0_mapping(tmp_path: Path) -> None:
    path = _manifest(tmp_path, "bad-mapping")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["attempts"][0]["mapping"]["F1"] = "ANT2"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(analysis.RepeatabilityAnalysisError, match="mapping"):
        analysis._load_run("bad-mapping", path)
