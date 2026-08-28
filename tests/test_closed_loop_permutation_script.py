import cmath
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_closed_loop_permutation.py"
SPEC = importlib.util.spec_from_file_location("closed_loop_permutation_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)

FREQUENCY_HZ = 2_400_000_000
ARTIFACT_IDS = {
    "initial": "0" * 32,
    "closure": "c" * 32,
    "rotation1": "1" * 32,
    "rotation2": "2" * 32,
}


def _phasor(gain_db: float, phase_deg: float) -> complex:
    return 10.0 ** (gain_db / 20.0) * cmath.exp(1j * math.radians(phase_deg))


def _mapping(rotation: int) -> dict[str, str]:
    return {
        f"F{index + 1}": f"ANT{(index + rotation) % 8 + 1}" for index in range(8)
    }


def _transfers(rotation: int, *, common_gain_db: float = 0.0) -> list[complex]:
    feed_gain = (0.0, 0.2, -0.4, 0.8, -0.6, 0.1, 0.5, -0.3)
    feed_phase = (0.0, 145.0, -160.0, 73.0, -101.0, 32.0, -49.0, 179.0)
    board_gain = (0.4, -0.9, 0.1, 1.2, -1.5, 0.3, -0.2, 0.7)
    board_phase = (112.0, -174.0, 49.0, -92.0, 176.0, -38.0, 81.0, -151.0)
    values = [0j] * 8
    for feed_index in range(8):
        antenna_index = (feed_index + rotation) % 8
        values[antenna_index] = _phasor(
            common_gain_db + feed_gain[feed_index] + board_gain[antenna_index],
            feed_phase[feed_index] + board_phase[antenna_index] + rotation,
        )
    return values


def _document(
    artifact_id: str, transfers: list[complex], *, quality: bool = True
) -> dict[str, Any]:
    states = []
    for index, value in enumerate(transfers):
        states.append(
            {
                "name": f"ANT{index + 1}",
                "quality_passed": quality,
                "raw_rx2_over_rx1": {"amplitude": abs(value) + 0.001},
                "all_off_subtracted_rx2_over_rx1": {
                    "phasor": {"real": value.real, "imag": value.imag}
                },
            }
        )
    return {
        "schema": 1,
        "analysis_kind": "fast20_dual_rx_ota_reference_transfer",
        "source_commit": "b" * 40,
        "quality_gate": {"passed": quality},
        "artifact": {
            "artifact_id": artifact_id,
            "center_frequency_hz": FREQUENCY_HZ,
            "sha256": "a" * 64,
        },
        "aggregation_key": {
            "center_frequency_hz": FREQUENCY_HZ,
            "tx_channel": 0,
            "receiver_gain_db": 40,
            "stream_id": int(artifact_id[0], 16) + 10,
        },
        "capture": {"profile_contract_sha256": "d" * 64},
        "transfer": {
            "all_off": {"raw_rx2_over_rx1": {"amplitude": 0.001}},
            "states": states,
        },
    }


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "captures"
    documents = {
        "initial": _document(ARTIFACT_IDS["initial"], _transfers(0, common_gain_db=0.01)),
        "closure": _document(ARTIFACT_IDS["closure"], _transfers(0)),
        "rotation1": _document(ARTIFACT_IDS["rotation1"], _transfers(1)),
        "rotation2": _document(ARTIFACT_IDS["rotation2"], _transfers(2)),
    }
    for name, document in documents.items():
        artifact_id = ARTIFACT_IDS[name]
        directory = root / artifact_id
        directory.mkdir(parents=True)
        (directory / "fast20-reference-transfer.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    manifest = {
        "schema": 1,
        "run_id": "synthetic",
        "board_id": "board",
        "pluto_serial": "radio",
        "profile_id": "fast20-v1",
        "profile_contract_sha256": "d" * 64,
        "firmware_binary_sha256": "e" * 64,
        "frequencies_hz": [FREQUENCY_HZ],
        "rounds": [
            {
                "rotation": rotation,
                "mapping": _mapping(rotation),
                "artifacts_by_frequency_hz": {
                    str(FREQUENCY_HZ): ARTIFACT_IDS[
                        "initial" if rotation == 0 else f"rotation{rotation}"
                    ]
                },
            }
            for rotation in range(3)
        ],
        "closure": {
            "rotation": 0,
            "mapping": _mapping(0),
            "artifacts_by_frequency_hz": {str(FREQUENCY_HZ): ARTIFACT_IDS["closure"]},
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, root


def test_build_analysis_uses_closure_for_fit_and_initial_for_repeatability(tmp_path: Path) -> None:
    manifest, root = _write_fixture(tmp_path)

    result = analysis.build_analysis(manifest, root)

    assert result["conclusions"]["additional_cyclic_rotations_required"] is False
    assert result["conclusions"]["qualified_board_calibration_frequencies_hz"] == [
        FREQUENCY_HZ
    ]
    row = result["frequency_results"][0]
    assert row["fit_artifact_ids_by_rotation"] == {
        "0": ARTIFACT_IDS["closure"],
        "1": ARTIFACT_IDS["rotation1"],
        "2": ARTIFACT_IDS["rotation2"],
    }
    assert row["closure"]["initial_artifact_id"] == ARTIFACT_IDS["initial"]
    assert row["closure"]["relative_shape_phase_rms_deg"] == pytest.approx(0.0, abs=1e-10)
    assert row["separable_model"]["fit_quality"]["phase_residual_rms_deg"] == pytest.approx(
        0.0, abs=1e-9
    )
    assert len(result["source_documents"]) == 4


def test_failed_reference_transfer_document_is_rejected(tmp_path: Path) -> None:
    manifest, root = _write_fixture(tmp_path)
    failed_path = (
        root / ARTIFACT_IDS["rotation1"] / "fast20-reference-transfer.json"
    )
    failed = json.loads(failed_path.read_text(encoding="utf-8"))
    failed["quality_gate"]["passed"] = False
    failed_path.write_text(json.dumps(failed), encoding="utf-8")

    with pytest.raises(analysis.AnalysisInputError, match="did not pass"):
        analysis.build_analysis(manifest, root)


def test_initial_rotation_zero_is_used_when_frequency_has_no_closure(tmp_path: Path) -> None:
    manifest_path, root = _write_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["closure"]["artifacts_by_frequency_hz"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = analysis.build_analysis(manifest_path, root)

    row = result["frequency_results"][0]
    assert row["fit_artifact_ids_by_rotation"]["0"] == ARTIFACT_IDS["initial"]
    assert row["closure"] is None
