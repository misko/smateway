import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pluto_plus.models import ArtifactSummary

from smateway.reference_transfer import (
    CyclePhasorSummary,
    Fast20ReferenceTransferAnalysis,
    ReferenceTransferStateEstimate,
)
from smateway.schedule_alignment import (
    AlignmentCandidate,
    AlignmentFitQuality,
    AlignmentSearchMode,
    AlignmentSearchProvenance,
    DecodedTimingAgreement,
    ScheduleAlignmentResult,
)

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"

CAPTURE_SPEC = importlib.util.spec_from_file_location(
    "capture_fast20_dwell",
    SCRIPT_DIRECTORY / "capture_fast20_dwell.py",
)
assert CAPTURE_SPEC is not None
assert CAPTURE_SPEC.loader is not None
capture_script = importlib.util.module_from_spec(CAPTURE_SPEC)
sys.modules[CAPTURE_SPEC.name] = capture_script
CAPTURE_SPEC.loader.exec_module(capture_script)

ANALYZER_SPEC = importlib.util.spec_from_file_location(
    "reference_transfer_script_under_test",
    SCRIPT_DIRECTORY / "reanalyze_fast20_reference_transfer_artifact.py",
)
assert ANALYZER_SPEC is not None
assert ANALYZER_SPEC.loader is not None
analyzer_script = importlib.util.module_from_spec(ANALYZER_SPEC)
sys.modules[ANALYZER_SPEC.name] = analyzer_script
ANALYZER_SPEC.loader.exec_module(analyzer_script)


def _summary(value: complex, *, cycle_count: int = 20) -> CyclePhasorSummary:
    return CyclePhasorSummary(
        phasor=value,
        amplitude=abs(value),
        phase_deg=0.0,
        cycle_coherence=1.0,
        cycle_phase_std_deg=0.0,
        even_odd_phase_agreement=1.0,
        cycle_phasors=(value,) * cycle_count,
    )


def _alignment() -> ScheduleAlignmentResult:
    quality = AlignmentFitQuality(
        explained_fraction=0.999,
        residual_fraction=0.001,
        residual_energy=0.01,
        null_energy=10.0,
        coherent_energy=8.0,
        cycle_deviation_energy=0.01,
        detection_ratio=800.0,
        detection_strength=1.0,
        even_odd_agreement=0.99,
        cycle_coherence=0.995,
        combined_score=0.99,
        selected_bin_count=8_000,
    )
    selected = AlignmentCandidate(
        cycle_ms=386.0,
        marker_phase_ms=117.0,
        quality=quality,
        complete_cycle_count=20,
    )
    runner_up = AlignmentCandidate(
        cycle_ms=386.0,
        marker_phase_ms=47.0,
        quality=AlignmentFitQuality(
            explained_fraction=0.80,
            residual_fraction=0.20,
            residual_energy=2.0,
            null_energy=10.0,
            coherent_energy=6.0,
            cycle_deviation_energy=0.5,
            detection_ratio=12.0,
            detection_strength=0.78,
            even_odd_agreement=0.95,
            cycle_coherence=0.90,
            combined_score=0.60,
            selected_bin_count=8_000,
        ),
        complete_cycle_count=20,
    )
    return ScheduleAlignmentResult(
        selected=selected,
        distinct_runner_up=runner_up,
        score_margin=0.39,
        provenance=AlignmentSearchProvenance(
            method_version="schedule_alignment_v1",
            mode=AlignmentSearchMode.TRANSITION_SEEDED,
            cycle_range_ms=(382.0, 390.0),
            fine_cycle_step_ms=0.2,
            fine_phase_step_ms=0.2,
            coarse_phase_step_ms=2.0,
            candidate_count=231,
            valid_candidate_count=231,
            coarse_candidate_count=0,
            fine_candidate_count=231,
            refinement_basin_count=8,
            transition_seed_used=True,
        ),
        decoded_timing_agreement=DecodedTimingAgreement(
            cycle_error_ms=0.0,
            marker_error_ms=0.1,
            cycle_tolerance_ms=0.2,
            marker_tolerance_ms=1.2,
            agrees=True,
        ),
    )


def _analysis() -> Fast20ReferenceTransferAnalysis:
    states = tuple(
        ReferenceTransferStateEstimate(
            name=f"ANT{index}",
            rx1=_summary(0.9 + 0.1j),
            raw_rx2_over_rx1=_summary(0.2 + index * 0.01 + 0.1j),
            all_off_subtracted_rx2_over_rx1=_summary(0.1 + index * 0.01 + 0.02j),
            transfer_detection_snr_db=30.0,
            transfer_approximate_phase_standard_error_deg=2.0,
            bin_count=200,
        )
        for index in range(1, 9)
    )
    return Fast20ReferenceTransferAnalysis(
        cycle_ms=386.0,
        marker_phase_ms=117.0,
        bin_duration_ms=1.0,
        bin_count=10_000,
        complete_cycle_count=20,
        edge_exclusion_ms=2.0,
        alignment_score=0.99,
        alignment_even_odd_agreement=0.99,
        reference_valid_bin_fraction=1.0,
        continuity_verified=True,
        continuity_block_count=100,
        all_off_anchor_count=500,
        all_off_rx1=_summary(0.9 + 0.1j),
        all_off_raw_rx2_over_rx1=_summary(0.04 + 0.02j),
        states=states,
        schedule_alignment=_alignment(),
    )


def _artifact() -> ArtifactSummary:
    return ArtifactSummary(
        artifact_id="a" * 32,
        radio_id="radio",
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        path="/immutable/artifact",
        sample_count=10_000_000,
        receiver_count=2,
        sample_rate_hz=1_000_000,
        center_frequency_hz=2_400_000_000,
        sha256="b" * 64,
        label="synthetic",
    )


def _capture() -> dict[str, object]:
    return {
        "tx_channel": 1,
        "center_frequency_hz": 2_400_000_000,
        "sample_rate_hz": 1_000_000,
        "sample_count": 10_000_000,
        "stream_id": 1234,
        "receiver_gain_db": 20,
        "dds_frequency_readback_hz": [0, 0, 0, 0, 100_000, 0, -100_000, 0],
        "adc_headroom_admission": {
            "passed": True,
            "receivers": [
                {
                    "receiver": 0,
                    "sample_count": 10_000_000,
                    "clipped_sample_count": 0,
                    "near_full_scale_fraction": 0.0,
                    "maximum_near_full_scale_fraction": 0.0001,
                    "passed": True,
                },
                {
                    "receiver": 1,
                    "sample_count": 10_000_000,
                    "clipped_sample_count": 0,
                    "near_full_scale_fraction": 0.0,
                    "maximum_near_full_scale_fraction": 0.0001,
                    "passed": True,
                },
            ],
        },
    }


def test_document_retains_reference_transfer_cycles_and_aggregation_identity() -> None:
    document = analyzer_script._analysis_document(
        artifact=_artifact(),
        capture=_capture(),
        pilot={"estimated_offset_hz": 100_006.103515625},
        analysis=_analysis(),
        source_commit="c" * 40,
    )

    assert document["analysis_kind"] == "fast20_dual_rx_ota_reference_transfer"
    assert document["reference_model"]["terminated_rx1_leakage_interpretation"] is False
    assert document["quality_gate"]["passed"] is True
    assert document["quality_gate"]["capture_headroom_admission_passed"] is True
    schedule = document["transfer"]["schedule_alignment"]
    assert schedule["method"] == "schedule_alignment_v1"
    assert schedule["selected"]["fit"]["explained_fraction"] == 0.999
    assert schedule["search"]["mode"] == "transition_seeded"
    assert schedule["decoder_agreement"]["agrees"] is True
    assert schedule["distinct_runner_up"]["marker_phase_ms"] == 47.0
    assert schedule["score_margin"] == 0.39
    assert document["aggregation_key"] == {
        "artifact_id": "a" * 32,
        "stream_id": 1234,
        "tx_channel": 1,
        "center_frequency_hz": 2_400_000_000,
        "carrier_frequency_hz": 2_400_100_000.0,
        "configured_dds_tone_offset_hz": 100_000.0,
        "estimated_received_carrier_frequency_hz": 2_400_100_006.1035156,
        "sample_rate_hz": 1_000_000,
        "receiver_gain_db": 20,
    }
    all_off = document["transfer"]["all_off"]
    assert all_off["raw_rx2_over_rx1"]["phasor"] == {"real": 0.04, "imag": 0.02}
    assert all_off["raw_rx2_over_rx1"]["repeat_quality_passed"] is True
    assert len(all_off["raw_rx2_over_rx1"]["cycle_phasors"]) == 20
    assert len(document["transfer"]["states"]) == 8
    for state in document["transfer"]["states"]:
        assert state["quality_passed"] is True
        assert state["raw_rx2_over_rx1"]["repeat_quality_passed"] is True
        assert len(state["rx1"]["cycle_phasors"]) == 20
        assert len(state["raw_rx2_over_rx1"]["cycle_phasors"]) == 20
        assert len(state["all_off_subtracted_rx2_over_rx1"]["cycle_phasors"]) == 20
    json.dumps(document)


def test_document_rejects_missing_capture_headroom_admission() -> None:
    capture = _capture()
    del capture["adc_headroom_admission"]

    document = analyzer_script._analysis_document(
        artifact=_artifact(),
        capture=capture,
        pilot={"estimated_offset_hz": 100_000.0},
        analysis=_analysis(),
        source_commit="c" * 40,
    )

    assert document["quality_gate"]["passed"] is False
    assert document["quality_gate"]["global_rejection_reasons"] == [
        "capture_headroom_admission_missing_or_failed"
    ]


def test_document_checks_each_receiver_headroom_record() -> None:
    capture = _capture()
    admission = capture["adc_headroom_admission"]
    assert isinstance(admission, dict)
    receivers = admission["receivers"]
    assert isinstance(receivers, list)
    assert isinstance(receivers[1], dict)
    receivers[1]["clipped_sample_count"] = 1

    document = analyzer_script._analysis_document(
        artifact=_artifact(),
        capture=capture,
        pilot={"estimated_offset_hz": 100_000.0},
        analysis=_analysis(),
        source_commit="c" * 40,
    )

    assert document["quality_gate"]["capture_headroom_admission_passed"] is False
    assert document["quality_gate"]["passed"] is False


def test_document_fails_closed_without_alignment_diagnostics() -> None:
    document = analyzer_script._analysis_document(
        artifact=_artifact(),
        capture=_capture(),
        pilot={"estimated_offset_hz": 100_000.0},
        analysis=replace(_analysis(), schedule_alignment=None),
        source_commit="c" * 40,
    )

    assert document["quality_gate"]["passed"] is False
    assert "schedule_alignment_diagnostics_missing" in document["quality_gate"][
        "global_rejection_reasons"
    ]
    assert document["transfer"]["schedule_alignment"] is None


def test_document_rejects_weak_fit_and_decoder_disagreement() -> None:
    analysis = _analysis()
    assert analysis.schedule_alignment is not None
    alignment = analysis.schedule_alignment
    weak_quality = replace(alignment.quality, explained_fraction=0.80)
    disagreement = replace(alignment.decoded_timing_agreement, agrees=False)
    weak_alignment = replace(
        alignment,
        selected=replace(alignment.selected, quality=weak_quality),
        decoded_timing_agreement=disagreement,
    )
    document = analyzer_script._analysis_document(
        artifact=_artifact(),
        capture=_capture(),
        pilot={"estimated_offset_hz": 100_000.0},
        analysis=replace(analysis, schedule_alignment=weak_alignment),
        source_commit="c" * 40,
    )

    assert document["quality_gate"]["passed"] is False
    assert document["quality_gate"]["global_rejection_reasons"] == [
        "schedule_explained_fraction_below_minimum",
        "schedule_phase_and_transition_decoders_disagree",
    ]


def test_reference_reanalysis_defaults_to_transition_seed_and_versioned_output() -> None:
    args = analyzer_script._parser().parse_args(("a" * 32,))

    assert args.alignment_search_mode is AlignmentSearchMode.TRANSITION_SEEDED
    assert args.output_filename == analyzer_script.VERSIONED_OUTPUT_FILENAME
    assert args.promote_canonical is False


def test_capture_parser_accepts_and_bounds_common_receiver_gain() -> None:
    default = capture_script._parser().parse_args(("--tx-channel", "0"))
    selected = capture_script._parser().parse_args(
        ("--tx-channel", "1", "--receiver-gain-db", "20")
    )

    assert default.receiver_gain_db == 60
    assert selected.receiver_gain_db == 20
    with pytest.raises(SystemExit):
        capture_script._parser().parse_args(("--tx-channel", "0", "--receiver-gain-db", "63"))


def test_capture_parser_exposes_separate_conducted_sweep_confirmation() -> None:
    args = capture_script._parser().parse_args(
        (
            "--tx-channel",
            "0",
            "--center-frequency-hz",
            "2100000000",
            "--allow-conducted-calibration-sweep",
            "--conducted-fixture-id",
            capture_script.CONDUCTED_FIXTURE_ID,
            "--confirm-fully-conducted",
        )
    )

    assert args.allow_conducted_calibration_sweep is True
    assert args.conducted_fixture_id == capture_script.CONDUCTED_FIXTURE_ID
    assert args.confirm_fully_conducted is True
