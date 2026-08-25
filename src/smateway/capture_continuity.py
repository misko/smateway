"""Strict, offline validation of persisted Pluto SigMF continuity evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CaptureContinuitySummary:
    """Immutable proof summary for one validated continuity ledger."""

    schema_version: int
    metadata_abi: int
    block_count: int
    total_samples: int
    sample_sequence_span: int
    stream_id: int
    first_buffer_sequence: int
    last_buffer_sequence: int
    first_sample_sequence: int
    last_sample_sequence_exclusive: int
    samples_per_block: int | None

    def as_dict(self) -> dict[str, int | None]:
        """Return a JSON-serializable representation without mutable internals."""

        return {
            "schema_version": self.schema_version,
            "metadata_abi": self.metadata_abi,
            "block_count": self.block_count,
            "total_samples": self.total_samples,
            "sample_sequence_span": self.sample_sequence_span,
            "stream_id": self.stream_id,
            "first_buffer_sequence": self.first_buffer_sequence,
            "last_buffer_sequence": self.last_buffer_sequence,
            "first_sample_sequence": self.first_sample_sequence,
            "last_sample_sequence_exclusive": self.last_sample_sequence_exclusive,
            "samples_per_block": self.samples_per_block,
        }


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _expected_positive(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field, minimum=1)


def validate_continuity_ledger(
    ledger: Mapping[str, object],
    *,
    expected_total_samples: int | None = None,
    expected_samples_per_block: int | None = None,
) -> CaptureContinuitySummary:
    """Validate one ``pluto:continuity`` object without reading IQ or hardware."""

    expected_total = _expected_positive(expected_total_samples, "expected_total_samples")
    expected_block_samples = _expected_positive(
        expected_samples_per_block, "expected_samples_per_block"
    )
    schema_version = _integer(ledger.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ValueError("schema_version must be exactly 1")
    metadata_abi = _integer(ledger.get("metadata_abi"), "metadata_abi")
    if metadata_abi != 2:
        raise ValueError("metadata_abi must be exactly 2")
    block_count = _integer(ledger.get("block_count"), "block_count", minimum=1)
    total_samples = _integer(ledger.get("total_samples"), "total_samples", minimum=1)
    stream_id = _integer(ledger.get("stream_id"), "stream_id")
    aggregate_first = _integer(
        ledger.get("first_sample_sequence"), "first_sample_sequence"
    )
    aggregate_last = _integer(
        ledger.get("last_sample_sequence_exclusive"),
        "last_sample_sequence_exclusive",
        minimum=1,
    )
    aggregate_span = _integer(
        ledger.get("sample_sequence_span"), "sample_sequence_span", minimum=1
    )
    raw_blocks = ledger.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ValueError("blocks must be a nonempty array")
    if block_count != len(raw_blocks):
        raise ValueError("block_count does not match the number of blocks")
    if expected_total is not None and total_samples != expected_total:
        raise ValueError("total_samples does not match expected_total_samples")
    if aggregate_last <= aggregate_first:
        raise ValueError("aggregate FPGA sample sequence does not advance")
    if aggregate_last - aggregate_first != aggregate_span:
        raise ValueError("sample_sequence_span does not match aggregate first/last")
    if aggregate_span != total_samples:
        raise ValueError("sample_sequence_span does not match total_samples")

    next_sample_start = 0
    next_fpga_sample = aggregate_first
    observed_sample_counts: set[int] = set()
    for index, raw_block in enumerate(raw_blocks):
        label = f"blocks[{index}]"
        block = _mapping(raw_block, label)
        block_abi = _integer(block.get("metadata_abi"), f"{label}.metadata_abi")
        if block_abi != metadata_abi:
            raise ValueError(f"{label}.metadata_abi does not match the ledger")
        block_stream = _integer(block.get("stream_id"), f"{label}.stream_id")
        if block_stream != stream_id:
            raise ValueError(f"{label}.stream_id does not match the ledger")
        buffer_sequence = _integer(
            block.get("buffer_sequence"), f"{label}.buffer_sequence"
        )
        if buffer_sequence != index:
            raise ValueError(f"{label}.buffer_sequence is not zero-based and consecutive")
        sample_start = _integer(block.get("sample_start"), f"{label}.sample_start")
        if sample_start != next_sample_start:
            raise ValueError(f"{label}.sample_start is not contiguous")
        sample_count = _integer(
            block.get("sample_count"), f"{label}.sample_count", minimum=1
        )
        if expected_block_samples is not None and sample_count != expected_block_samples:
            raise ValueError(f"{label}.sample_count does not match expected_samples_per_block")
        observed_sample_counts.add(sample_count)
        missing_samples = _integer(
            block.get("missing_samples_before"), f"{label}.missing_samples_before"
        )
        if missing_samples != 0:
            raise ValueError(f"{label}.missing_samples_before must be zero")
        first_sample = _integer(
            block.get("first_sample_sequence"), f"{label}.first_sample_sequence"
        )
        if first_sample != next_fpga_sample:
            raise ValueError(f"{label}.first_sample_sequence is not contiguous")
        last_sample = _integer(
            block.get("last_sample_sequence_exclusive"),
            f"{label}.last_sample_sequence_exclusive",
            minimum=1,
        )
        if last_sample != first_sample + sample_count:
            raise ValueError(
                f"{label}.last_sample_sequence_exclusive does not match its sample_count"
            )
        next_sample_start += sample_count
        next_fpga_sample = last_sample

    if next_sample_start != total_samples:
        raise ValueError("sum of block sample_count values does not match total_samples")
    if next_fpga_sample != aggregate_last:
        raise ValueError("last block FPGA sample sequence does not match the aggregate")
    first_block = _mapping(raw_blocks[0], "blocks[0]")
    if _integer(first_block.get("first_sample_sequence"), "blocks[0].first_sample_sequence") != (
        aggregate_first
    ):
        raise ValueError("first block FPGA sample sequence does not match the aggregate")

    uniform_samples = (
        next(iter(observed_sample_counts)) if len(observed_sample_counts) == 1 else None
    )
    return CaptureContinuitySummary(
        schema_version=schema_version,
        metadata_abi=metadata_abi,
        block_count=block_count,
        total_samples=total_samples,
        sample_sequence_span=aggregate_span,
        stream_id=stream_id,
        first_buffer_sequence=0,
        last_buffer_sequence=block_count - 1,
        first_sample_sequence=aggregate_first,
        last_sample_sequence_exclusive=aggregate_last,
        samples_per_block=uniform_samples,
    )


def validate_sigmf_continuity(
    metadata: Mapping[str, object],
    *,
    expected_total_samples: int | None = None,
    expected_samples_per_block: int | None = None,
) -> CaptureContinuitySummary:
    """Extract and validate continuity from one complete SigMF metadata object."""

    capture = _mapping(metadata.get("pluto:capture"), "pluto:capture")
    capture_sample_count = _integer(
        capture.get("sample_count"), "pluto:capture.sample_count", minimum=1
    )
    if expected_total_samples is not None:
        expected_total = _expected_positive(expected_total_samples, "expected_total_samples")
        if capture_sample_count != expected_total:
            raise ValueError(
                "pluto:capture.sample_count does not match expected_total_samples"
            )
    else:
        expected_total = capture_sample_count
    ledger = _mapping(metadata.get("pluto:continuity"), "pluto:continuity")
    return validate_continuity_ledger(
        ledger,
        expected_total_samples=expected_total,
        expected_samples_per_block=expected_samples_per_block,
    )


__all__ = [
    "CaptureContinuitySummary",
    "validate_continuity_ledger",
    "validate_sigmf_continuity",
]
