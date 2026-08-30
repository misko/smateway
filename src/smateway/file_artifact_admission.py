"""Hardware-free admission helpers for persisted dual-RX CI16 artifacts.

These helpers deliberately operate on ordinary files only.  They reject
symlinked path components, re-hash the raw IQ and SigMF metadata, re-audit the
ABI-2 continuity ledger, and decode the exact dual-receiver sample layout used
by :mod:`pluto_plus.artifacts`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from smateway.hexcal import audit_continuity_metadata, sha256_path


class FileArtifactAdmissionError(ValueError):
    """A persisted artifact is missing, mutable-by-indirection, or inconsistent."""


LOCAL_RPI_STORAGE_REFERENCE = Path("/home/pi")


def assert_no_symlink_chain(path: Path, *, label: str) -> Path:
    """Return an absolute path after rejecting every symlink in its ancestry."""

    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise FileArtifactAdmissionError(f"{label} contains parent traversal")
    exact = expanded.absolute()
    current = Path(exact.anchor)
    for part in exact.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise FileArtifactAdmissionError(f"{label} contains a symlink: {current}")
    return exact


def _nearest_existing_ancestor(path: Path, *, label: str) -> Path:
    """Return the nearest existing ancestor of an already absolute path."""

    current = assert_no_symlink_chain(path, label=label)
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise FileArtifactAdmissionError(f"{label} has no existing filesystem ancestor")
        current = parent
    return current


def assert_local_rpi_storage(
    path: Path,
    *,
    label: str,
    reference: Path = LOCAL_RPI_STORAGE_REFERENCE,
) -> Path:
    """Require ``path`` to reside on the same device as local Raspberry Pi storage.

    Planned capture/output paths commonly do not exist when their contract is
    admitted.  Comparing the nearest existing ancestor's ``st_dev`` avoids both
    that race and brittle mount-name blacklists (for example, merely rejecting
    ``/mnt`` or ``/media``).
    """

    exact = assert_no_symlink_chain(path, label=label)
    nearest = _nearest_existing_ancestor(exact, label=label)
    local_reference = _nearest_existing_ancestor(
        reference.expanduser().absolute(), label="local RPi storage reference"
    )
    try:
        observed_device = nearest.stat().st_dev
        local_device = local_reference.stat().st_dev
    except OSError as error:
        raise FileArtifactAdmissionError(
            f"cannot inspect {label} filesystem device: {error}"
        ) from error
    if observed_device != local_device:
        raise FileArtifactAdmissionError(f"{label} is not on the local RPi storage device")
    return exact


def read_json_file(path: Path, *, label: str) -> dict[str, Any]:
    """Read one regular, non-symlink JSON object."""

    exact = assert_no_symlink_chain(path, label=label)
    if exact.is_symlink() or not exact.is_file():
        raise FileArtifactAdmissionError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(exact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FileArtifactAdmissionError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FileArtifactAdmissionError(f"{label} must contain one JSON object")
    return value


def verify_file_binding(
    value: object,
    *,
    label: str,
    path_field: str = "path",
    sha256_field: str = "sha256",
    size_field: str | None = "size_bytes",
) -> Path:
    """Verify a path/hash/(optional) size binding and return its absolute path."""

    if not isinstance(value, Mapping):
        raise FileArtifactAdmissionError(f"{label} binding must be an object")
    raw_path = value.get(path_field)
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise FileArtifactAdmissionError(f"{label} path must be absolute")
    path = assert_no_symlink_chain(Path(raw_path), label=label)
    if path.is_symlink() or not path.is_file():
        raise FileArtifactAdmissionError(f"{label} must be a regular non-symlink file")
    digest = value.get(sha256_field)
    if not isinstance(digest, str) or sha256_path(path) != digest:
        raise FileArtifactAdmissionError(f"{label} SHA-256 binding is stale")
    if size_field is not None:
        size = value.get(size_field)
        if isinstance(size, bool) or not isinstance(size, int) or path.stat().st_size != size:
            raise FileArtifactAdmissionError(f"{label} size binding is stale")
    return path


def verify_source_tree_binding(
    value: object,
    *,
    label: str,
    required_relative_paths: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Re-hash every source file named by a repository attestation.

    The attestation's aggregate hash is validated by each campaign-specific
    analyzer because historical schemas use different field names.  This
    helper verifies the authoritative repository-relative file bindings and
    rejects empty, duplicate, absolute, escaping, or symlinked entries.
    """

    if not isinstance(value, Mapping):
        raise FileArtifactAdmissionError(f"{label} source attestation must be an object")
    repository_value = value.get("repository")
    repository_path_value = value.get("repository_path")
    if repository_value is None:
        repository_value = repository_path_value
    elif repository_path_value is not None and repository_path_value != repository_value:
        raise FileArtifactAdmissionError(f"{label} repository fields disagree")
    files = value.get("files")
    if (
        not isinstance(repository_value, str)
        or not Path(repository_value).is_absolute()
        or not isinstance(files, list)
        or not files
    ):
        raise FileArtifactAdmissionError(
            f"{label} source attestation requires an absolute repository and files"
        )
    repository = assert_no_symlink_chain(Path(repository_value), label=f"{label} repository")
    if repository.is_symlink() or not repository.is_dir():
        raise FileArtifactAdmissionError(f"{label} repository is not a regular directory")
    observed_relative_paths: set[str] = set()
    verified: list[Path] = []
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise FileArtifactAdmissionError(f"{label} source file {index} is malformed")
        relative_value = item.get("path")
        if not isinstance(relative_value, str):
            raise FileArtifactAdmissionError(f"{label} source file {index} path is missing")
        relative = Path(relative_value)
        normalized = relative.as_posix()
        if (
            relative.is_absolute()
            or relative_value in {"", "."}
            or ".." in relative.parts
            or normalized in observed_relative_paths
        ):
            raise FileArtifactAdmissionError(f"{label} source file path is unsafe or duplicate")
        observed_relative_paths.add(normalized)
        binding = {
            "path": str(repository / relative),
            "sha256": item.get("sha256"),
            "size_bytes": item.get("size_bytes"),
        }
        verified.append(verify_file_binding(binding, label=f"{label} source file {normalized}"))
    missing = set(required_relative_paths) - observed_relative_paths
    if missing:
        raise FileArtifactAdmissionError(
            f"{label} source attestation omits required files: {', '.join(sorted(missing))}"
        )
    return tuple(verified)


def _artifact_member(
    evidence: Mapping[str, Any],
    *,
    label: str,
    path_fields: tuple[str, ...],
    hash_fields: tuple[str, ...],
    size_fields: tuple[str, ...],
) -> Path:
    path_field = next((name for name in path_fields if name in evidence), None)
    hash_field = next((name for name in hash_fields if name in evidence), None)
    if path_field is None or hash_field is None:
        raise FileArtifactAdmissionError(f"{label} lacks a path/hash binding")
    size_field = next((name for name in size_fields if name in evidence), None)
    return verify_file_binding(
        evidence,
        label=label,
        path_field=path_field,
        sha256_field=hash_field,
        size_field=size_field,
    )


def admit_dual_rx_ci16_artifact(
    evidence: object,
    *,
    label: str,
    expected_sample_count: int,
    expected_samples_per_block: int,
    expected_sample_rate_hz: float,
    expected_stream_id: int | str | None = None,
    expected_artifact_id: str | None = None,
) -> tuple[npt.NDArray[np.complex64], dict[str, Any], Path, Path]:
    """Re-admit, audit, and decode one exact dual-RX ``ci16_le`` artifact.

    The returned array has shape ``(2, expected_sample_count)``.  No hardware,
    libiio context, subprocess, or network operation is performed.
    """

    if not isinstance(evidence, Mapping):
        raise FileArtifactAdmissionError(f"{label} evidence must be an object")
    artifact_id = evidence.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise FileArtifactAdmissionError(f"{label} artifact ID is missing")
    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        raise FileArtifactAdmissionError(f"{label} artifact ID differs")
    raw_path = _artifact_member(
        evidence,
        label=f"{label} raw IQ",
        path_fields=("raw_iq_path", "data_path"),
        hash_fields=("raw_iq_sha256", "data_sha256"),
        size_fields=("raw_iq_size_bytes", "data_size_bytes"),
    )
    metadata_path = _artifact_member(
        evidence,
        label=f"{label} SigMF metadata",
        path_fields=("metadata_path",),
        hash_fields=("metadata_sha256",),
        size_fields=("metadata_size_bytes",),
    )
    metadata = read_json_file(metadata_path, label=f"{label} SigMF metadata")
    global_section = metadata.get("global")
    if not isinstance(global_section, Mapping):
        raise FileArtifactAdmissionError(f"{label} SigMF global section is missing")
    if (
        global_section.get("core:datatype") != "ci16_le"
        or global_section.get("core:num_channels") != 2
        or global_section.get("pluto:artifact_id") != artifact_id
    ):
        raise FileArtifactAdmissionError(
            f"{label} SigMF datatype/channel/artifact identity differs"
        )
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=expected_sample_count,
        expected_samples_per_block=expected_samples_per_block,
        expected_sample_rate_hz=expected_sample_rate_hz,
    )
    if continuity.get("metadata_abi") != 2:
        raise FileArtifactAdmissionError(f"{label} is not ABI-2 metadata")
    stream_id = continuity.get("stream_id")
    if expected_stream_id is not None and str(stream_id) != str(expected_stream_id):
        raise FileArtifactAdmissionError(f"{label} stream ID differs from accepted evidence")
    raw = np.memmap(raw_path, dtype="<i2", mode="r")
    expected_components = expected_sample_count * 2 * 2
    if raw.size != expected_components:
        raise FileArtifactAdmissionError(f"{label} raw byte count is not exact dual-RX CI16")
    components = raw.reshape(expected_sample_count, 2, 2)
    samples = np.empty((2, expected_sample_count), dtype=np.complex64)
    for receiver in range(2):
        samples[receiver].real = components[:, receiver, 0]
        samples[receiver].imag = components[:, receiver, 1]
    return samples, continuity, raw_path, metadata_path


__all__ = [
    "FileArtifactAdmissionError",
    "LOCAL_RPI_STORAGE_REFERENCE",
    "admit_dual_rx_ci16_artifact",
    "assert_local_rpi_storage",
    "assert_no_symlink_chain",
    "read_json_file",
    "verify_file_binding",
    "verify_source_tree_binding",
]
