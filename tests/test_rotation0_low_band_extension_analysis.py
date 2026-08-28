import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"

BASE_SPEC = importlib.util.spec_from_file_location(
    "analyze_rotation0_repeatability",
    SCRIPT_DIRECTORY / "analyze_rotation0_repeatability.py",
)
assert BASE_SPEC is not None and BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = base
BASE_SPEC.loader.exec_module(base)

SPEC = importlib.util.spec_from_file_location(
    "rotation0_low_band_extension_under_test",
    SCRIPT_DIRECTORY / "analyze_rotation0_low_band_extension.py",
)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)


def _state(antenna_index: int, run_offset_deg: float) -> dict[str, object]:
    return {
        "name": f"ANT{antenna_index}",
        "quality_passed": True,
        "quality_rejection_reasons": [],
        "transfer_detection_snr_db": 50.0,
        "all_off_subtracted_rx2_over_rx1": {
            "phase_deg": antenna_index * 10.0 + run_offset_deg,
            "amplitude": 0.1 * antenna_index,
            "cycle_phase_std_deg": 0.2,
            "cycle_coherence": 0.99,
        },
        "raw_rx2_over_rx1": {"amplitude": 0.1 * antenna_index + 0.01},
    }


def _manifest(
    tmp_path: Path,
    run_id: str,
    frequencies_hz: tuple[int, ...],
    run_offset_deg: float,
) -> Path:
    attempts = []
    for plan_index, frequency_hz in enumerate(frequencies_hz):
        artifact_id = f"{run_id}-{plan_index}"
        artifact_dir = tmp_path / run_id / str(plan_index)
        artifact_dir.mkdir(parents=True)
        analysis_path = artifact_dir / "fast20-reference-transfer.json"
        document = {
            "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
            "aggregation_key": {"center_frequency_hz": frequency_hz},
            "artifact": {"artifact_id": artifact_id, "sha256": f"{plan_index + 1:064x}"},
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
                "mapping": base.EXPECTED_MAPPING,
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
            "frequencies_hz": list(frequencies_hz),
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


def test_combines_full_historical_and_focused_low_band_runs(tmp_path: Path) -> None:
    historical = [
        base._load_run(
            f"historical-{index}",
            _manifest(tmp_path, f"historical-{index}", base.FREQUENCIES_HZ, index * 17.0),
        )
        for index in range(2)
    ]
    focused = [
        base._load_run(
            f"focused-{index}",
            _manifest(
                tmp_path,
                f"focused-{index}",
                analysis.LOW_BAND_FREQUENCIES_HZ,
                index * 23.0,
            ),
            analysis.LOW_BAND_FREQUENCIES_HZ,
        )
        for index in range(2)
    ]

    result = analysis.analyze(historical, focused)

    assert result["scope"]["combined_pass_count"] == 4
    assert result["acquisition_integrity"]["combined_low_band_capture_count"] == 20
    assert result["acquisition_integrity"]["focused_capture_count"] == 10
    assert result["acquisition_integrity"]["focused_failed_attempt_count"] == 0
    assert result["aggregate"]["combined_unambiguous_capture_count"] == 20
    assert result["aggregate"]["fully_unambiguous_frequencies_hz"] == list(
        analysis.LOW_BAND_FREQUENCIES_HZ
    )
    first_path = result["frequency_results"][0]["paths"][0]
    assert first_path["combined"]["relative_phase_circular_std_deg"] < 1e-6
