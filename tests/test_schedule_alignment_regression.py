import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from smateway.decoder import DecodedScheduleTiming
from smateway.profile import load_profile
from smateway.schedule_alignment import (
    AlignmentSearchConfig,
    AlignmentSearchMode,
    evaluate_schedule_alignment,
    search_schedule_alignment,
)

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "schedule_alignment"
FIXTURE_STEM = "false_lock_be64aa4b22f9436c8ff25547a3589b98"
PROVENANCE_PATH = FIXTURE_DIRECTORY / f"{FIXTURE_STEM}.json"
DATA_PATH = FIXTURE_DIRECTORY / f"{FIXTURE_STEM}.npz"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture() -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    with np.load(DATA_PATH, allow_pickle=False) as archive:
        assert set(archive.files) == {"transfer_bins", "reference_valid"}
        transfer = archive["transfer_bins"]
        reference_valid = archive["reference_valid"]
    return provenance, transfer, reference_valid


def _decoded_timing(document: dict[str, Any]) -> DecodedScheduleTiming:
    timing = document["decoded_timing"]
    marker_starts = tuple(float(value) for value in timing["marker_start_times_ms"])
    cycle_durations = tuple(float(value) for value in timing["cycle_durations_ms"])
    return DecodedScheduleTiming(
        marker_indices=tuple(range(len(marker_starts))),
        marker_start_times_ms=marker_starts,
        cycle_durations_ms=cycle_durations,
        median_cycle_ms=float(timing["median_cycle_ms"]),
        cycle_jitter_ms=float(timing["cycle_jitter_ms"]),
        marker_phase_ms=float(timing["marker_phase_ms"]),
        marker_count=int(timing["marker_count"]),
        complete_frame_count=int(timing["complete_frame_count"]),
        strict_frame_count=int(timing["strict_frame_count"]),
        edge_truncated_marker_count=int(timing["edge_truncated_marker_count"]),
        rejected_marker_count=int(timing["rejected_marker_count"]),
    )


def _circular_distance(first: float, second: float, period: float) -> float:
    separation = abs((first - second) % period)
    return min(separation, period - separation)


def test_false_lock_fixture_is_a_compact_verified_reduction() -> None:
    provenance, transfer, reference_valid = _fixture()
    reduction = provenance["reduction"]
    arrays = reduction["arrays"]

    assert provenance["source"]["artifact_id"] == "be64aa4b22f9436c8ff25547a3589b98"
    assert provenance["source"]["sigmf_data_size_bytes"] == 80_000_000
    assert reduction["full_iq_is_present"] is False
    assert DATA_PATH.stat().st_size == reduction["fixture_size_bytes"]
    assert DATA_PATH.stat().st_size < provenance["source"]["sigmf_data_size_bytes"] / 500
    assert _sha256_file(DATA_PATH) == reduction["fixture_sha256"]

    assert transfer.shape == tuple(arrays["transfer_bins"]["shape"])
    assert transfer.dtype == np.dtype(arrays["transfer_bins"]["dtype"])
    assert transfer.nbytes == arrays["transfer_bins"]["uncompressed_size_bytes"]
    assert _sha256_bytes(transfer.tobytes(order="C")) == arrays["transfer_bins"]["data_sha256"]
    assert reference_valid.shape == tuple(arrays["reference_valid"]["shape"])
    assert reference_valid.dtype == np.dtype(arrays["reference_valid"]["dtype"])
    assert reference_valid.nbytes == arrays["reference_valid"]["uncompressed_size_bytes"]
    assert (
        _sha256_bytes(reference_valid.tobytes(order="C"))
        == arrays["reference_valid"]["data_sha256"]
    )
    assert float(np.mean(reference_valid)) == arrays["reference_valid"]["valid_fraction"]


def test_false_lock_fixture_distinguishes_wrong_and_correct_fit_quality() -> None:
    provenance, transfer, reference_valid = _fixture()
    profile = load_profile(Path(provenance["profile"]["path"]))
    duration_ms = float(provenance["reduction"]["duration_ms"])
    bin_duration_ms = float(provenance["reduction"]["bin_duration_ms"])
    edge_exclusion_ms = float(provenance["reduction"]["edge_exclusion_ms"])
    times_ms = (np.arange(transfer.size, dtype=np.float64) + 0.5) * bin_duration_ms
    legacy = provenance["known_failure"]["legacy_greedy_result"]
    expected = provenance["known_failure"]["transition_aligned_candidate"]

    wrong = evaluate_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=float(legacy["cycle_ms"]),
        marker_phase_ms=float(legacy["marker_phase_ms"]),
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )
    correct = evaluate_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=duration_ms,
        cycle_ms=float(expected["cycle_ms"]),
        marker_phase_ms=float(expected["marker_phase_ms"]),
        edge_exclusion_ms=edge_exclusion_ms,
        profile=profile,
    )

    assert wrong is not None
    assert correct is not None
    assert wrong.score == pytest.approx(legacy["combined_score"], abs=1e-12)
    assert wrong.quality.explained_fraction == pytest.approx(
        legacy["explained_fraction"], abs=1e-12
    )
    assert correct.score == pytest.approx(expected["combined_score"], abs=1e-12)
    assert correct.quality.explained_fraction == pytest.approx(
        expected["explained_fraction"], abs=1e-12
    )
    assert correct.quality.residual_fraction == pytest.approx(
        expected["residual_fraction"], abs=1e-15
    )
    assert correct.complete_cycle_count == expected["complete_cycle_count"]
    assert correct.score - wrong.score > 0.20


def test_transition_seeded_search_recovers_real_false_lock_capture() -> None:
    provenance, transfer, reference_valid = _fixture()
    profile = load_profile(Path(provenance["profile"]["path"]))
    reduction = provenance["reduction"]
    acceptance = provenance["acceptance"]
    timing = _decoded_timing(provenance)
    bin_duration_ms = float(reduction["bin_duration_ms"])
    times_ms = (np.arange(transfer.size, dtype=np.float64) + 0.5) * bin_duration_ms

    result = search_schedule_alignment(
        transfer,
        reference_valid,
        times_ms,
        duration_ms=float(reduction["duration_ms"]),
        profile=profile,
        config=AlignmentSearchConfig(
            cycle_range_ms=tuple(provenance["profile"]["cycle_search_range_ms"]),
            bin_duration_ms=bin_duration_ms,
            edge_exclusion_ms=float(reduction["edge_exclusion_ms"]),
            mode=AlignmentSearchMode.TRANSITION_SEEDED,
        ),
        decoded_timing=timing,
    )

    assert timing.median_cycle_ms is not None
    assert timing.marker_phase_ms is not None
    assert abs(result.cycle_ms - timing.median_cycle_ms) <= acceptance["maximum_cycle_error_ms"]
    assert (
        _circular_distance(
            result.marker_phase_ms,
            timing.marker_phase_ms,
            timing.median_cycle_ms,
        )
        <= acceptance["maximum_marker_error_from_decoder_ms"]
    )
    assert result.score >= acceptance["minimum_combined_score"]
    assert result.quality.explained_fraction >= acceptance["minimum_explained_fraction"]
    assert result.complete_cycle_count >= acceptance["minimum_complete_cycle_count"]
    assert result.provenance.mode is AlignmentSearchMode.TRANSITION_SEEDED
    assert result.provenance.transition_seed_used
    assert result.provenance.candidate_count > 0
    assert result.decoded_timing_agreement is not None
    assert result.decoded_timing_agreement.agrees
    assert result.distinct_runner_up is not None
    assert result.score_margin is not None and result.score_margin > 0.0
