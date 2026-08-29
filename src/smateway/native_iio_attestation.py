"""Exact native-libiio loader and runtime identity attestation.

Importing this module has no loader or IIO side effects.  Capture CLIs must pin
``LD_LIBRARY_PATH`` before importing any project dependency that could load
libiio, then call :func:`attest_runtime` before any hardware action.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

REQUIRED_LIBIIO_DIRECTORY = Path("/usr/local/lib")
REQUIRED_LIBIIO_PATH = REQUIRED_LIBIIO_DIRECTORY / "libiio.so.0.25"
REQUIRED_LIBIIO_SHA256 = "d0a18bddcb54d182262acb2a9e31a88c81618cb43789320b8381c149777bef89"
REQUIRED_LIBIIO_VERSION = (0, 25)
REQUIRED_LIBIIO_SYMBOLS = ("iio_device_get_kernel_buffers_count",)

_ATTESTATION_FIELDS = {
    "schema",
    "evidence_kind",
    "library_path",
    "library_path_from_proc_maps",
    "library_sha256",
    "library_size_bytes",
    "requested_soname",
    "version",
    "required_symbols",
    "loader_search_path_first",
}
_VERSION_FIELDS = {"major", "minor", "git_tag"}


class RuntimeAttestationBoundary(Protocol):
    """Injectable boundary used for a pre-hardware runtime recheck."""

    def __call__(self) -> dict[str, Any]: ...


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalize_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("native libiio runtime attestation must be an object")
    try:
        normalized = json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as error:
        raise ValueError("native libiio runtime attestation must be canonical JSON") from error
    if not isinstance(normalized, dict):
        raise ValueError("native libiio runtime attestation must be an object")
    return normalized


def _validate_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("native libiio hash is malformed")
    return value


def validate_runtime_attestation(value: object) -> dict[str, Any]:
    """Validate and freeze the exact reviewed native library identity."""

    document = _normalize_object(value)
    version = document.get("version")
    symbols = document.get("required_symbols")
    if (
        set(document) != _ATTESTATION_FIELDS
        or document.get("schema") != 1
        or document.get("evidence_kind") != "native_libiio_process_mapping"
        or document.get("library_path_from_proc_maps") is not True
        or not isinstance(version, Mapping)
        or set(version) != _VERSION_FIELDS
        or not isinstance(symbols, Mapping)
        or set(symbols) != set(REQUIRED_LIBIIO_SYMBOLS)
    ):
        raise ValueError("native libiio runtime attestation is incomplete")
    path = Path(str(document.get("library_path", "")))
    if not path.is_absolute() or path != REQUIRED_LIBIIO_PATH:
        raise ValueError(f"native libiio must be mapped from {REQUIRED_LIBIIO_PATH}")
    if _validate_sha256(document.get("library_sha256")) != REQUIRED_LIBIIO_SHA256:
        raise ValueError("native libiio hash differs from the reviewed binary")
    if (
        version.get("major") != REQUIRED_LIBIIO_VERSION[0]
        or version.get("minor") != REQUIRED_LIBIIO_VERSION[1]
        or not isinstance(version.get("git_tag"), str)
        or not version.get("git_tag")
    ):
        raise ValueError("native libiio version differs from the required 0.25 ABI")
    if document.get("loader_search_path_first") != str(REQUIRED_LIBIIO_DIRECTORY):
        raise ValueError("native loader search path is not pinned to /usr/local/lib first")
    if any(symbols.get(symbol) is not True for symbol in REQUIRED_LIBIIO_SYMBOLS):
        raise ValueError("native libiio does not export every required capture symbol")
    size = document.get("library_size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("native libiio binary size is invalid")
    soname = document.get("requested_soname")
    if not isinstance(soname, str) or not soname:
        raise ValueError("native libiio requested soname is missing")
    return document


def attestation_sha256(value: object) -> str:
    """Hash one validated attestation using deterministic JSON bytes."""

    document = validate_runtime_attestation(value)
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _mapped_libiio_paths(maps_path: Path) -> set[Path]:
    mapped_paths: set[Path] = set()
    try:
        lines = maps_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            fields = line.split()
            if fields and fields[-1].startswith("/") and "libiio.so" in fields[-1]:
                mapped_paths.add(Path(fields[-1]).resolve(strict=True))
    except OSError as error:
        raise RuntimeError(f"cannot inspect native process mappings: {error}") from error
    return mapped_paths


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attest_runtime() -> dict[str, Any]:
    """Attest the exact libiio binary already mapped into this process."""

    # Lazy by design: loader pinning must happen in the CLI before this import.
    iio = importlib.import_module("iio")
    mapped_paths = _mapped_libiio_paths(Path("/proc/self/maps"))
    if mapped_paths != {REQUIRED_LIBIIO_PATH}:
        rendered = ", ".join(sorted(str(path) for path in mapped_paths)) or "none"
        raise RuntimeError(
            f"exact reviewed native libiio is not uniquely mapped; observed: {rendered}"
        )
    loader_entries = [
        item for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
    ]
    if not loader_entries or Path(loader_entries[0]).resolve() != REQUIRED_LIBIIO_DIRECTORY:
        raise RuntimeError("LD_LIBRARY_PATH must place /usr/local/lib first")
    try:
        raw_version = iio.version
        major = int(raw_version[0])
        minor = int(raw_version[1])
        git_tag = str(raw_version[2])
        native_library = iio._lib
    except (AttributeError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("cannot read native libiio version identity") from error
    document = {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(REQUIRED_LIBIIO_PATH),
        "library_path_from_proc_maps": True,
        "library_sha256": _sha256_path(REQUIRED_LIBIIO_PATH),
        "library_size_bytes": REQUIRED_LIBIIO_PATH.stat().st_size,
        "requested_soname": str(getattr(native_library, "_name", "")),
        "version": {"major": major, "minor": minor, "git_tag": git_tag},
        "required_symbols": {
            symbol: getattr(native_library, symbol, None) is not None
            for symbol in REQUIRED_LIBIIO_SYMBOLS
        },
        "loader_search_path_first": str(REQUIRED_LIBIIO_DIRECTORY),
    }
    return validate_runtime_attestation(document)


def call_runtime_preflight(
    boundary: RuntimeAttestationBoundary,
    *,
    now: Callable[[], str],
    error_document: Callable[[BaseException], Mapping[str, Any]],
) -> dict[str, Any]:
    """Call and freeze a runtime boundary as auditable preflight evidence."""

    started_at = now()
    try:
        frozen = validate_runtime_attestation(boundary())
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "native_libiio_runtime_preflight",
            "status": "failed",
            "attestation": None,
            "attestation_sha256": None,
            "started_at": started_at,
            "completed_at": now(),
            "error": dict(error_document(error)),
        }
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_runtime_preflight",
        "status": "passed",
        "attestation": frozen,
        "attestation_sha256": attestation_sha256(frozen),
        "started_at": started_at,
        "completed_at": now(),
        "error": None,
    }


def runtime_preflight_passed(value: object, *, expected: object) -> bool:
    """Return whether a preflight is the exact immutable planned attestation."""

    try:
        frozen_expected = validate_runtime_attestation(expected)
        expected_sha256 = attestation_sha256(frozen_expected)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "native_libiio_runtime_preflight"
        and value.get("status") == "passed"
        and value.get("attestation") == frozen_expected
        and value.get("attestation_sha256") == expected_sha256
        and value.get("error") is None
    )
