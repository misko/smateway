import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from smateway.capture_continuity import (
    CaptureContinuitySummary,
    validate_continuity_ledger,
    validate_sigmf_continuity,
)


def _ledger(sample_counts: tuple[int, ...] = (4, 4, 4)) -> dict[str, Any]:
    stream_id = 91
    first_sample = 10_000
    sample_start = 0
    blocks = []
    for buffer_sequence, sample_count in enumerate(sample_counts):
        blocks.append(
            {
                "sample_start": sample_start,
                "sample_count": sample_count,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "buffer_sequence": buffer_sequence,
                "first_sample_sequence": first_sample + sample_start,
                "last_sample_sequence_exclusive": first_sample
                + sample_start
                + sample_count,
                "missing_samples_before": 0,
            }
        )
        sample_start += sample_count
    return {
        "schema_version": 1,
        "metadata_abi": 2,
        "stream_id": stream_id,
        "block_count": len(blocks),
        "total_samples": sample_start,
        "first_sample_sequence": first_sample,
        "last_sample_sequence_exclusive": first_sample + sample_start,
        "sample_sequence_span": sample_start,
        "blocks": blocks,
    }


def _metadata(sample_counts: tuple[int, ...] = (4, 4, 4)) -> dict[str, Any]:
    ledger = _ledger(sample_counts)
    return {
        "pluto:capture": {"sample_count": ledger["total_samples"], "receiver_count": 2},
        "pluto:continuity": ledger,
    }


def test_valid_sigmf_continuity_returns_immutable_json_summary() -> None:
    summary = validate_sigmf_continuity(
        _metadata(),
        expected_total_samples=12,
        expected_samples_per_block=4,
    )

    assert summary == CaptureContinuitySummary(
        schema_version=1,
        metadata_abi=2,
        block_count=3,
        total_samples=12,
        sample_sequence_span=12,
        stream_id=91,
        first_buffer_sequence=0,
        last_buffer_sequence=2,
        first_sample_sequence=10_000,
        last_sample_sequence_exclusive=10_012,
        samples_per_block=4,
    )
    assert json.loads(json.dumps(summary.as_dict())) == summary.as_dict()
    with pytest.raises(FrozenInstanceError):
        summary.total_samples = 13  # type: ignore[misc]


def test_nonuniform_positive_blocks_are_valid_without_an_expected_size() -> None:
    summary = validate_continuity_ledger(_ledger((3, 4, 5)), expected_total_samples=12)

    assert summary.total_samples == 12
    assert summary.samples_per_block is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", 2),
        ("metadata_abi", 1),
        ("block_count", 4),
        ("total_samples", 13),
        ("sample_sequence_span", 13),
        ("first_sample_sequence", 9_999),
        ("last_sample_sequence_exclusive", 10_013),
    ),
)
def test_rejects_inconsistent_aggregate_fields(field: str, value: int) -> None:
    ledger = _ledger()
    ledger[field] = value

    with pytest.raises(ValueError):
        validate_continuity_ledger(ledger)


def test_rejects_empty_or_miscounted_blocks() -> None:
    empty = _ledger()
    empty["blocks"] = []
    empty["block_count"] = 0
    with pytest.raises(ValueError, match="block_count|blocks"):
        validate_continuity_ledger(empty)

    miscounted = _ledger()
    miscounted["block_count"] = 2
    with pytest.raises(ValueError, match="block_count"):
        validate_continuity_ledger(miscounted)


@pytest.mark.parametrize(
    ("block_index", "field", "value", "message"),
    (
        (1, "metadata_abi", 1, "metadata_abi"),
        (1, "stream_id", 92, "stream_id"),
        (1, "buffer_sequence", 2, "buffer_sequence"),
        (1, "sample_start", 5, "sample_start"),
        (1, "sample_count", 0, "sample_count"),
        (1, "missing_samples_before", 1, "missing_samples_before"),
        (1, "first_sample_sequence", 10_005, "first_sample_sequence"),
        (1, "last_sample_sequence_exclusive", 10_009, "last_sample_sequence"),
    ),
)
def test_rejects_invalid_per_block_evidence(
    block_index: int, field: str, value: int, message: str
) -> None:
    ledger = _ledger()
    blocks = ledger["blocks"]
    assert isinstance(blocks, list)
    blocks[block_index][field] = value

    with pytest.raises(ValueError, match=message):
        validate_continuity_ledger(ledger)


def test_rejects_sum_of_blocks_that_differs_from_total() -> None:
    ledger = _ledger()
    blocks = ledger["blocks"]
    assert isinstance(blocks, list)
    blocks[-1]["sample_count"] = 5
    blocks[-1]["last_sample_sequence_exclusive"] = 10_013

    with pytest.raises(ValueError, match="sum of block sample_count"):
        validate_continuity_ledger(ledger)


def test_rejects_unexpected_total_or_per_block_size() -> None:
    ledger = _ledger()
    with pytest.raises(ValueError, match="expected_total_samples"):
        validate_continuity_ledger(ledger, expected_total_samples=13)
    with pytest.raises(ValueError, match="expected_samples_per_block"):
        validate_continuity_ledger(ledger, expected_samples_per_block=5)


def test_sigmf_wrapper_requires_matching_capture_sample_count() -> None:
    metadata = _metadata()
    capture = metadata["pluto:capture"]
    assert isinstance(capture, dict)
    capture["sample_count"] = 13

    with pytest.raises(ValueError, match="total_samples"):
        validate_sigmf_continuity(metadata)
    with pytest.raises(ValueError, match="expected_total_samples"):
        validate_sigmf_continuity(metadata, expected_total_samples=12)


@pytest.mark.parametrize(
    ("path", "message"),
    (
        ("pluto:capture", "pluto:capture"),
        ("pluto:continuity", "pluto:continuity"),
    ),
)
def test_sigmf_wrapper_requires_both_metadata_objects(path: str, message: str) -> None:
    metadata = _metadata()
    del metadata[path]

    with pytest.raises(ValueError, match=message):
        validate_sigmf_continuity(metadata)


@pytest.mark.parametrize("value", (True, "2", 2.0, None))
def test_integer_fields_are_strict(value: object) -> None:
    ledger = deepcopy(_ledger())
    ledger["metadata_abi"] = value

    with pytest.raises(ValueError, match="integer"):
        validate_continuity_ledger(ledger)
