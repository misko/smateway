from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from smateway.file_artifact_admission import (
    FileArtifactAdmissionError,
    admit_dual_rx_ci16_artifact,
    assert_local_rpi_storage,
    verify_source_tree_binding,
)
from smateway.hexcal import sha256_path


def _artifact(tmp_path: Path) -> tuple[dict[str, object], np.ndarray]:
    artifact_id = "artifact-a"
    sample_count = 8
    root = tmp_path / artifact_id
    root.mkdir(parents=True)
    values = np.arange(sample_count * 2 * 2, dtype="<i2").reshape(sample_count, 2, 2)
    raw = root / f"{artifact_id}.sigmf-data"
    raw.write_bytes(values.tobytes())
    block = {
        "sample_start": 0,
        "sample_count": sample_count,
        "utc_ns": 1_000,
        "metadata_abi": 2,
        "stream_id": 123,
        "buffer_sequence": 0,
        "first_sample_sequence": 500,
        "last_sample_sequence_exclusive": 508,
        "metadata_flags": 2_982_931,
        "missing_samples_before": 0,
        "sample_time_realtime_start_ns": 1_000_000_000,
        "sample_time_realtime_end_ns": 2_000_000_000,
        "sample_time_monotonic_start_ns": 3_000_000_000,
        "sample_time_monotonic_end_ns": 4_000_000_000,
        "sample_time_uncertainty_ns": 1,
    }
    metadata_document = {
        "global": {
            "core:datatype": "ci16_le",
            "core:num_channels": 2,
            "core:sample_rate": 8.0,
            "pluto:artifact_id": artifact_id,
        },
        "captures": [
            {
                "sample_start": 0,
                "settings": {"sample_rate_hz": 8.0},
            }
        ],
        "pluto:capture": {"sample_count": sample_count},
        "pluto:continuity": {
            "schema_version": 1,
            "metadata_abi": 2,
            "stream_id": 123,
            "block_count": 1,
            "total_samples": sample_count,
            "first_sample_sequence": 500,
            "last_sample_sequence_exclusive": 508,
            "sample_sequence_span": sample_count,
            "blocks": [block],
        },
    }
    metadata = root / f"{artifact_id}.sigmf-meta"
    metadata.write_text(json.dumps(metadata_document), encoding="utf-8")
    evidence: dict[str, object] = {
        "artifact_id": artifact_id,
        "raw_iq_path": str(raw),
        "raw_iq_sha256": sha256_path(raw),
        "metadata_path": str(metadata),
        "metadata_sha256": sha256_path(metadata),
    }
    return evidence, values


def test_reopens_hashes_audits_and_decodes_exact_dual_rx_ci16(tmp_path: Path) -> None:
    evidence, values = _artifact(tmp_path)

    samples, continuity, _, _ = admit_dual_rx_ci16_artifact(
        evidence,
        label="test",
        expected_sample_count=8,
        expected_samples_per_block=8,
        expected_sample_rate_hz=8.0,
        expected_stream_id=123,
        expected_artifact_id="artifact-a",
    )

    assert samples.shape == (2, 8)
    assert samples[1, 3] == complex(values[3, 1, 0], values[3, 1, 1])
    assert continuity["metadata_abi"] == 2
    assert continuity["stream_id"] == 123


def test_raw_or_metadata_tamper_is_rejected(tmp_path: Path) -> None:
    evidence, _ = _artifact(tmp_path)
    raw = Path(str(evidence["raw_iq_path"]))
    raw.write_bytes(raw.read_bytes() + b"\0\0")
    with pytest.raises(FileArtifactAdmissionError, match="SHA-256"):
        admit_dual_rx_ci16_artifact(
            evidence,
            label="test",
            expected_sample_count=8,
            expected_samples_per_block=8,
            expected_sample_rate_hz=8.0,
        )

    evidence, _ = _artifact(tmp_path / "second")
    metadata = Path(str(evidence["metadata_path"]))
    metadata.write_text("{}", encoding="utf-8")
    with pytest.raises(FileArtifactAdmissionError, match="SHA-256"):
        admit_dual_rx_ci16_artifact(
            evidence,
            label="test",
            expected_sample_count=8,
            expected_samples_per_block=8,
            expected_sample_rate_hz=8.0,
        )


def test_stream_mismatch_and_symlinked_ancestor_are_rejected(tmp_path: Path) -> None:
    evidence, _ = _artifact(tmp_path / "real")
    with pytest.raises(FileArtifactAdmissionError, match="stream ID"):
        admit_dual_rx_ci16_artifact(
            evidence,
            label="test",
            expected_sample_count=8,
            expected_samples_per_block=8,
            expected_sample_rate_hz=8.0,
            expected_stream_id=456,
        )

    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    linked = dict(evidence)
    linked["raw_iq_path"] = str(link / "artifact-a" / "artifact-a.sigmf-data")
    with pytest.raises(FileArtifactAdmissionError, match="symlink"):
        admit_dual_rx_ci16_artifact(
            linked,
            label="test",
            expected_sample_count=8,
            expected_samples_per_block=8,
            expected_sample_rate_hz=8.0,
        )


def test_local_storage_uses_nearest_existing_ancestor_device(tmp_path: Path) -> None:
    planned = tmp_path / "not-created" / "capture" / "raw.sigmf-data"

    assert (
        assert_local_rpi_storage(
            planned,
            label="planned capture",
            reference=tmp_path,
        )
        == planned.absolute()
    )

    assert Path("/proc").stat().st_dev != tmp_path.stat().st_dev
    with pytest.raises(FileArtifactAdmissionError, match="local RPi storage device"):
        assert_local_rpi_storage(
            Path("/proc/smateway-never-created"),
            label="planned capture",
            reference=tmp_path,
        )
    with pytest.raises(FileArtifactAdmissionError, match="parent traversal"):
        assert_local_rpi_storage(
            tmp_path / "nested" / ".." / "capture",
            label="planned capture",
            reference=tmp_path,
        )


def test_source_tree_binding_rehashes_transitive_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "src" / "selector.py"
    source.parent.mkdir(parents=True)
    source.write_text("SELECTOR = 1\n", encoding="utf-8")
    attestation = {
        "repository": str(repository),
        "files": [
            {
                "path": "src/selector.py",
                "sha256": sha256_path(source),
                "size_bytes": source.stat().st_size,
            }
        ],
    }

    assert verify_source_tree_binding(attestation, label="test") == (source,)

    source.write_text("SELECTOR = 2\n", encoding="utf-8")
    with pytest.raises(FileArtifactAdmissionError, match="SHA-256"):
        verify_source_tree_binding(attestation, label="test")

    source.write_text("SELECTOR = 1\n", encoding="utf-8")
    attestation["files"] = [attestation["files"][0], attestation["files"][0]]
    with pytest.raises(FileArtifactAdmissionError, match="duplicate"):
        verify_source_tree_binding(attestation, label="test")
