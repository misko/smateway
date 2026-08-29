from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from smateway import native_iio_attestation as native


def _attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(native.REQUIRED_LIBIIO_PATH),
        "library_path_from_proc_maps": True,
        "library_sha256": native.REQUIRED_LIBIIO_SHA256,
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {
            "major": native.REQUIRED_LIBIIO_VERSION[0],
            "minor": native.REQUIRED_LIBIIO_VERSION[1],
            "git_tag": "synthetic",
        },
        "required_symbols": {symbol: True for symbol in native.REQUIRED_LIBIIO_SYMBOLS},
        "loader_search_path_first": str(native.REQUIRED_LIBIIO_DIRECTORY),
    }


def test_runtime_attestation_validation_is_exact_and_deterministic() -> None:
    expected = _attestation()
    assert native.validate_runtime_attestation(expected) == expected
    assert native.attestation_sha256(expected) == native.attestation_sha256(
        dict(reversed(list(expected.items())))
    )

    extra = _attestation()
    extra["unreviewed"] = True
    with pytest.raises(ValueError, match="incomplete"):
        native.validate_runtime_attestation(extra)

    wrong_symbol_set = _attestation()
    wrong_symbol_set["required_symbols"]["unreviewed_symbol"] = True
    with pytest.raises(ValueError, match="incomplete"):
        native.validate_runtime_attestation(wrong_symbol_set)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("library_path", "/usr/lib/libiio.so.0.25", "mapped from"),
        ("library_sha256", "0" * 64, "reviewed binary"),
        ("loader_search_path_first", "/usr/lib", "not pinned"),
        ("requested_soname", "", "soname"),
    ],
)
def test_runtime_attestation_rejects_wrong_reviewed_identity(
    field: str,
    value: object,
    message: str,
) -> None:
    document = _attestation()
    document[field] = value
    with pytest.raises(ValueError, match=message):
        native.validate_runtime_attestation(document)


def test_runtime_preflight_freezes_success_and_failure() -> None:
    clock = iter(("start", "finish", "start-2", "finish-2"))

    def now() -> str:
        return next(clock)

    def error_document(error: BaseException) -> dict[str, str]:
        return {"type": type(error).__name__, "message": str(error)}

    passed = native.call_runtime_preflight(
        _attestation,
        now=now,
        error_document=error_document,
    )
    assert passed["started_at"] == "start"
    assert passed["completed_at"] == "finish"
    assert native.runtime_preflight_passed(passed, expected=_attestation())

    failed = native.call_runtime_preflight(
        lambda: {**_attestation(), "library_sha256": "0" * 64},
        now=now,
        error_document=error_document,
    )
    assert failed["status"] == "failed"
    assert failed["attestation"] is None
    assert failed["error"]["type"] == "ValueError"
    assert not native.runtime_preflight_passed(failed, expected=_attestation())


def test_attest_runtime_binds_mapped_binary_loader_version_and_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_library = SimpleNamespace(
        _name="libiio.so.0",
        iio_device_get_kernel_buffers_count=object(),
    )
    fake_iio = SimpleNamespace(
        version=(0, 25, "synthetic"),
        _lib=native_library,
    )
    monkeypatch.setattr(native.importlib, "import_module", lambda name: fake_iio)
    monkeypatch.setattr(
        native,
        "_mapped_libiio_paths",
        lambda _path: {native.REQUIRED_LIBIIO_PATH},
    )
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        f"{native.REQUIRED_LIBIIO_DIRECTORY}:/synthetic/fallback",
    )

    observed = native.attest_runtime()

    assert observed["library_sha256"] == native.REQUIRED_LIBIIO_SHA256
    assert observed["library_size_bytes"] == 158_416
    assert observed["version"] == {
        "major": 0,
        "minor": 25,
        "git_tag": "synthetic",
    }
    assert all(observed["required_symbols"].values())


def test_attest_runtime_rejects_any_other_process_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native.importlib, "import_module", lambda name: SimpleNamespace())
    monkeypatch.setattr(
        native,
        "_mapped_libiio_paths",
        lambda _path: {native.REQUIRED_LIBIIO_PATH, native.REQUIRED_LIBIIO_PATH.parent / "other"},
    )
    with pytest.raises(RuntimeError, match="not uniquely mapped"):
        native.attest_runtime()
