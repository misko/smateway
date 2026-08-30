from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from test_selector_flash_attestation import (
    BOARD_ID,
    CAMPAIGN_ID,
    RUN_ID,
    _phase2,
)

from smateway.hexcal import PLUTO_PLUS_UTILS_IMPORTED_MODULES
from smateway.selector_flash_attestation import validate_sealed_selector_evidence

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/run_5g8_leakage_ladder.py"
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "run_5g8_leakage_ladder_for_t8_production_tests",
    RUNNER_PATH,
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
leakage_runner: ModuleType = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = leakage_runner
RUNNER_SPEC.loader.exec_module(leakage_runner)

SOURCE_COMMIT = "a" * 40
DEPENDENCY_COMMIT = "b" * 40
USB_URI = "usb:1.2.3"


def dependency_attestation(repository: Path) -> dict[str, Any]:
    root = repository.absolute()
    root.mkdir(parents=True, exist_ok=True)
    relative_sources = sorted({relative for _module, relative in PLUTO_PLUS_UTILS_IMPORTED_MODULES})
    contents = {
        relative: f'"""Synthetic pinned source for {relative}."""\n'.encode()
        for relative in relative_sources
    }
    contents.update(
        {
            "pyproject.toml": b"[project]\nname = 'pluto-plus-utils'\nversion = '0.0.0'\n",
            "uv.lock": b"version = 1\nrevision = 1\n",
        }
    )
    for relative, payload in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    python_prefix = root / ".venv"
    python_executable = python_prefix / "bin/python"
    python_executable.parent.mkdir(parents=True, exist_ok=True)
    python_executable.write_bytes(b"synthetic-python\n")

    def source_file(relative: str) -> dict[str, Any]:
        payload = contents[relative]
        return {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }

    imported_modules = []
    for module, relative in PLUTO_PLUS_UTILS_IMPORTED_MODULES:
        path = (root / relative).absolute()
        imported_modules.append(
            {
                "module": module,
                "path": str(path),
                "relative_path": relative,
                "sha256": hashlib.sha256(contents[relative]).hexdigest(),
                "size_bytes": len(contents[relative]),
            }
        )
    return {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "repository_path": str(root),
        "commit": DEPENDENCY_COMMIT,
        "head": DEPENDENCY_COMMIT,
        "python_executable": str(python_executable),
        "python_prefix": str(python_prefix),
        "clean_worktree_verified": True,
        "lock_metadata_files": ["pyproject.toml", "uv.lock"],
        "files": [
            *(source_file(relative) for relative in relative_sources),
            source_file("pyproject.toml"),
            source_file("uv.lock"),
        ],
        "imported_modules": imported_modules,
    }


def native_libiio_attestation() -> dict[str, Any]:
    return {
        "schema": 1,
        "evidence_kind": "native_libiio_process_mapping",
        "library_path": str(leakage_runner.REQUIRED_LIBIIO_PATH),
        "library_path_from_proc_maps": True,
        "library_sha256": leakage_runner.REQUIRED_LIBIIO_SHA256,
        "library_size_bytes": 158_416,
        "requested_soname": "libiio.so.0",
        "version": {"major": 0, "minor": 25, "git_tag": "synthetic"},
        "required_symbols": {symbol: True for symbol in leakage_runner.REQUIRED_LIBIIO_SYMBOLS},
        "loader_search_path_first": "/usr/local/lib",
    }


def sealed_bench_selector(
    directory: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create fully sealed selector evidence using only the fake hardware boundary."""

    paths, _boundary, _run_directory, result = _phase2(directory, role="bench")
    binding = {
        "schema": 1,
        "binding_kind": "sealed_selector_flash_evidence_v1",
        "path": str(result.path.absolute()),
        "sha256": result.sha256,
        "campaign_id": CAMPAIGN_ID,
        "run_id": RUN_ID,
        "board_id": BOARD_ID,
        "image_role": "bench",
    }
    validate_sealed_selector_evidence(
        result.path,
        expected_sha256=result.sha256,
        expected_campaign_id=CAMPAIGN_ID,
        expected_run_id=RUN_ID,
        expected_board_id=BOARD_ID,
        expected_image_role="bench",
    )
    control = leakage_runner._selector_control_contract(
        bench_manifest_path=paths["build_manifest"],
        openocd_config_path=paths["openocd_config"],
        profile_path=paths["profile"],
        selector_flash_evidence=binding,
    )
    leakage_runner._validate_target_image_admission_contract(control)
    return binding, control


def build_x_plan_contract(
    *,
    role: str,
    contract_id: str,
    implicated_stage: str,
    acquisition_index: int,
    freshness_epoch_id: str,
    fixture_evidence: Mapping[str, Any],
    capture_fixture: Mapping[str, Any],
    installed_after_fixture: Mapping[str, Any],
    selector_binding: Mapping[str, Any],
    selector_control: Mapping[str, Any] | None,
    serial: str,
) -> dict[str, Any]:
    """Build the same complete immutable contract used by the leakage runner."""

    stage = str(fixture_evidence["stage"])
    prebinding = {
        "schema": 1,
        "binding_kind": leakage_runner.X_PREBINDING_KIND,
        "contract_id": contract_id,
        "run_role": role,
        "installed_fixture_revision_sha256": installed_after_fixture["fixture_revision_sha256"],
    }
    capture_context = {
        "schema": 1,
        "binding_kind": leakage_runner.X_CAPTURE_CONTEXT_KIND,
        "implicated_boundary_stage": implicated_stage,
        "acquisition_index": acquisition_index,
        "freshness_epoch_id": freshness_epoch_id,
        "capture_state_fixture": copy.deepcopy(dict(capture_fixture)),
        "installed_after_fixture": copy.deepcopy(dict(installed_after_fixture)),
        "selector_flash_evidence": copy.deepcopy(dict(selector_binding)),
    }
    return leakage_runner._build_plan_contract(
        run_id=str(fixture_evidence["run_id"]),
        board_id=str(fixture_evidence["board_id"]),
        serial=serial,
        uri=USB_URI,
        stage=stage,
        source_commit=SOURCE_COMMIT,
        pluto_plus_utils_source_attestation=dependency_attestation(
            Path(str(selector_binding["path"])).parent / "synthetic-pluto-plus-utils"
        ),
        selector_control=(
            copy.deepcopy(dict(selector_control)) if selector_control is not None else None
        ),
        native_libiio_runtime_attestation=native_libiio_attestation(),
        fixture_evidence=copy.deepcopy(dict(fixture_evidence)),
        x_intervention_prebinding=prebinding,
        x_intervention_capture_context=capture_context,
    )
