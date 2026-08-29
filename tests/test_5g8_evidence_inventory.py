import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from smateway.evidence_inventory import (
    EXACT_CENTER_FREQUENCY_HZ,
    FAMILY_EARLY_PHASE,
    FAMILY_RX_GAIN,
    EvidenceInventoryError,
    build_evidence_inventory,
    canonical_json_bytes,
)

GENERATOR_BINDINGS = [{"path": "scripts/test-generator.py", "sha256": "a" * 64}]


def _metadata(
    artifact_id: str,
    raw: bytes,
    *,
    center_frequency_hz: int = EXACT_CENTER_FREQUENCY_HZ,
    gain_db: float = 40.0,
) -> dict[str, Any]:
    sample_count = len(raw) // 8
    first_sample = 10_000
    return {
        "annotations": [],
        "captures": [
            {
                "sample_start": 0,
                "settings": {
                    "bandwidth_hz": 800_000.0,
                    "center_frequency_hz": float(center_frequency_hz),
                    "channels": [0, 1],
                    "gain_db": gain_db,
                    "gain_mode": "manual",
                    "sample_rate_hz": 1_000_000.0,
                },
                "utc_ns": 1,
            }
        ],
        "global": {
            "core:datatype": "ci16_le",
            "core:description": (
                f"fast20 phase 1000000S/s 10s TX1 {center_frequency_hz}Hz"
            ),
            "core:num_channels": 2,
            "core:sample_rate": 1_000_000.0,
            "pluto:artifact_id": artifact_id,
            "pluto:created_at": "2026-08-25T17:22:21+00:00",
            "pluto:sha256": hashlib.sha256(raw).hexdigest(),
        },
        "pluto:capture": {
            "initial_settings": {
                "bandwidth_hz": 800_000.0,
                "center_frequency_hz": float(center_frequency_hz),
                "channels": [0, 1],
                "gain_db": gain_db,
                "gain_mode": "manual",
                "sample_rate_hz": 1_000_000.0,
            },
            "receiver_count": 2,
            "sample_count": sample_count,
        },
        "pluto:continuity": {
            "block_count": 1,
            "blocks": [
                {
                    "buffer_sequence": 0,
                    "first_sample_sequence": first_sample,
                    "last_sample_sequence_exclusive": first_sample + sample_count,
                    "metadata_abi": 2,
                    "missing_samples_before": 0,
                    "sample_count": sample_count,
                    "sample_start": 0,
                    "stream_id": 91,
                }
            ],
            "first_sample_sequence": first_sample,
            "last_sample_sequence_exclusive": first_sample + sample_count,
            "metadata_abi": 2,
            "sample_sequence_span": sample_count,
            "schema_version": 1,
            "stream_id": 91,
            "total_samples": sample_count,
        },
    }


def _write_capture(
    root: Path,
    artifact_id: str,
    *,
    raw: bytes = bytes(range(24)),
    metadata: dict[str, Any] | None = None,
    relative_parent: Path = Path("pluto-usb-captures"),
) -> tuple[Path, Path, dict[str, Any]]:
    directory = root / relative_parent / artifact_id
    directory.mkdir(parents=True)
    data_path = directory / f"{artifact_id}.sigmf-data"
    metadata_path = directory / f"{artifact_id}.sigmf-meta"
    data_path.write_bytes(raw)
    document = metadata if metadata is not None else _metadata(artifact_id, raw)
    metadata_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return metadata_path, data_path, document


def _inventory(root: Path, expected: dict[str, int] | None = None) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        build_evidence_inventory(
            root,
            generator_bindings=GENERATOR_BINDINGS,
            expected_family_counts=expected,
        ),
    )


def _add_rx_gain_manifest(
    root: Path,
    artifact_id: str,
    metadata_path: Path,
    data_path: Path,
    *,
    expected_data_sha: str | None = None,
) -> Path:
    manifest = root / "hexcal-gain-qualifications" / "qualification-a" / "gain-qualification.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    condition = {
        "artifact_evidence": {
            "artifact_id": artifact_id,
            "data_path": str(data_path),
            "data_sha256": expected_data_sha or hashlib.sha256(data_path.read_bytes()).hexdigest(),
            "data_size_bytes": data_path.stat().st_size,
            "metadata_path": str(metadata_path),
            "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
            "metadata_size_bytes": metadata_path.stat().st_size,
            "path": str(metadata_path.parent),
        },
        "center_frequency_hz": EXACT_CENTER_FREQUENCY_HZ,
        "receiver_gain_db": 40,
        "rf_readback_evidence": {"tx_hardware_gain_db_requested": -40.0},
        "tx_channel": 0,
    }
    manifest.write_text(json.dumps({"conditions": [condition]}), encoding="utf-8")
    return manifest


def test_minimal_inventory_validates_and_is_deterministic(tmp_path: Path) -> None:
    _write_capture(tmp_path, "00000000000000000000000000000001")

    first = _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})
    second = _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    aggregate = first["aggregate_invariants"]
    assert isinstance(aggregate, dict)
    assert aggregate["unique_raw_capture_count"] == 1
    assert aggregate["total_unique_raw_data_bytes"] == 24
    captures = first["captures"]
    assert isinstance(captures, list)
    capture = captures[0]
    assert capture["family"] == FAMILY_EARLY_PHASE
    assert capture["continuity"]["validated"] is True
    assert capture["metadata_path"].startswith("pluto-usb-captures/")
    assert str(tmp_path) not in canonical_json_bytes(first).decode()


def test_manifest_artifact_identity_classifies_rx_gain_without_timestamp_inference(
    tmp_path: Path,
) -> None:
    artifact_id = "10000000000000000000000000000001"
    relative = Path("hexcal-gain-qualifications/qualification-a/exploratory-artifacts")
    metadata_path, data_path, _ = _write_capture(
        tmp_path, artifact_id, relative_parent=relative
    )
    manifest = _add_rx_gain_manifest(tmp_path, artifact_id, metadata_path, data_path)

    result = _inventory(tmp_path, {FAMILY_RX_GAIN: 1})

    capture = result["captures"][0]
    assert capture["family"] == FAMILY_RX_GAIN
    assert capture["receiver_gain_db"] == 40.0
    assert capture["tx_hardware_gain_db"] == -40.0
    source = capture["source_references"][0]
    assert source["path"] == manifest.relative_to(tmp_path).as_posix()
    assert source["json_pointer"] == "/conditions/0"


def test_raw_sha_is_computed_and_must_match_metadata(tmp_path: Path) -> None:
    artifact_id = "20000000000000000000000000000001"
    _, data_path, _ = _write_capture(tmp_path, artifact_id)
    data_path.write_bytes(b"x" * 24)

    with pytest.raises(EvidenceInventoryError, match="data SHA differs"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_raw_size_must_match_dual_ci16_sample_count(tmp_path: Path) -> None:
    artifact_id = "30000000000000000000000000000001"
    metadata = _metadata(artifact_id, bytes(range(24)))
    metadata["pluto:capture"]["sample_count"] = 4
    _write_capture(tmp_path, artifact_id, metadata=metadata)

    with pytest.raises(EvidenceInventoryError, match="raw size"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_artifact_id_must_agree_with_parent_and_filename(tmp_path: Path) -> None:
    actual_id = "40000000000000000000000000000001"
    claimed_id = "40000000000000000000000000000002"
    metadata = _metadata(claimed_id, bytes(range(24)))
    _write_capture(tmp_path, actual_id, metadata=metadata)

    with pytest.raises(EvidenceInventoryError, match="artifact ID/path"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_present_continuity_ledger_is_strictly_validated(tmp_path: Path) -> None:
    artifact_id = "50000000000000000000000000000001"
    metadata = _metadata(artifact_id, bytes(range(24)))
    metadata["pluto:continuity"]["blocks"][0]["missing_samples_before"] = 1
    _write_capture(tmp_path, artifact_id, metadata=metadata)

    with pytest.raises(EvidenceInventoryError, match="continuity validation failed"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_continuity_ledger_is_required(tmp_path: Path) -> None:
    artifact_id = "50000000000000000000000000000002"
    metadata = _metadata(artifact_id, bytes(range(24)))
    del metadata["pluto:continuity"]
    _write_capture(tmp_path, artifact_id, metadata=metadata)

    with pytest.raises(EvidenceInventoryError, match="lacks required ABI-2 continuity"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_raw_sha_deduplication_retains_alias_and_does_not_double_count(tmp_path: Path) -> None:
    raw = bytes(range(24))
    _write_capture(tmp_path, "60000000000000000000000000000001", raw=raw)
    _write_capture(tmp_path, "60000000000000000000000000000002", raw=raw)

    result = _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})

    aggregate = result["aggregate_invariants"]
    assert aggregate["sigmf_metadata_record_count"] == 2
    assert aggregate["unique_artifact_id_count"] == 2
    assert aggregate["unique_raw_capture_count"] == 1
    capture = result["captures"][0]
    aliases = capture["artifact_aliases_after_raw_sha_deduplication"]
    assert [item["artifact_id"] for item in aliases] == [
        "60000000000000000000000000000002"
    ]
    groups = result["overlap_and_deduplication"]["duplicate_raw_sha_groups"]
    assert groups[0]["metadata_record_count"] == 2


def test_raw_sha_deduplication_rejects_conflicting_scientific_metadata(
    tmp_path: Path,
) -> None:
    raw = bytes(range(24))
    first_id = "60000000000000000000000000000003"
    second_id = "60000000000000000000000000000004"
    _write_capture(tmp_path, first_id, raw=raw)
    conflicting = _metadata(second_id, raw, gain_db=41.0)
    _write_capture(tmp_path, second_id, raw=raw, metadata=conflicting)

    with pytest.raises(EvidenceInventoryError, match="conflicting scientific metadata"):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})


def test_cross_family_duplicate_raw_data_fails_closed(tmp_path: Path) -> None:
    raw = bytes(range(24))
    rx_id = "70000000000000000000000000000001"
    phase_id = "70000000000000000000000000000002"
    relative = Path("hexcal-gain-qualifications/qualification-a/exploratory-artifacts")
    metadata_path, data_path, _ = _write_capture(
        tmp_path, rx_id, raw=raw, relative_parent=relative
    )
    _add_rx_gain_manifest(tmp_path, rx_id, metadata_path, data_path)
    _write_capture(tmp_path, phase_id, raw=raw)

    with pytest.raises(EvidenceInventoryError, match="more than one family"):
        _inventory(tmp_path, None)


def test_qualification_source_hash_and_size_evidence_is_enforced(tmp_path: Path) -> None:
    artifact_id = "80000000000000000000000000000001"
    relative = Path("hexcal-gain-qualifications/qualification-a/exploratory-artifacts")
    metadata_path, data_path, _ = _write_capture(
        tmp_path, artifact_id, relative_parent=relative
    )
    _add_rx_gain_manifest(
        tmp_path,
        artifact_id,
        metadata_path,
        data_path,
        expected_data_sha="f" * 64,
    )

    with pytest.raises(EvidenceInventoryError, match="source raw SHA disagrees"):
        _inventory(tmp_path, {FAMILY_RX_GAIN: 1})


def test_non_5g8_metadata_is_parsed_but_excluded(tmp_path: Path) -> None:
    artifact_id = "90000000000000000000000000000001"
    raw = bytes(range(24))
    _write_capture(
        tmp_path,
        artifact_id,
        raw=raw,
        metadata=_metadata(artifact_id, raw, center_frequency_hz=2_400_000_000),
    )

    result = _inventory(tmp_path, {})

    assert result["aggregate_invariants"]["unique_raw_capture_count"] == 0
    assert result["captures"] == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("core:datatype", "cf32_le", "ci16_le"),
        ("core:num_channels", 1, "dual-receiver"),
        ("core:sample_rate", 2_000_000.0, "1 MS/s"),
    ),
)
def test_exact_capture_contract_rejects_wrong_global_format(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    artifact_id = "a0000000000000000000000000000001"
    metadata = deepcopy(_metadata(artifact_id, bytes(range(24))))
    metadata["global"][field] = value
    _write_capture(tmp_path, artifact_id, metadata=metadata)

    with pytest.raises(EvidenceInventoryError, match=message):
        _inventory(tmp_path, {FAMILY_EARLY_PHASE: 1})
