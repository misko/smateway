#!/usr/bin/env python3
"""Run a marker-independent static one-hot 5.8 GHz selector-path ladder."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Protocol

_PINNED_PYTHON = Path("/home/pi/pluto-plus-utils/.venv/bin/python")
_PINNED_PREFIX = Path("/home/pi/pluto-plus-utils/.venv")
_REPOSITORY = Path(__file__).resolve().parents[1]
_SMATEWAY_SOURCE = _REPOSITORY / "src"
_REQUIRED_LIBIIO_BOOTSTRAP_DIRECTORY = Path("/usr/local/lib")
_loader_directories = tuple(
    Path(item).resolve() for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
)
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != _PINNED_PREFIX
    or str(_SMATEWAY_SOURCE) not in sys.path
    or not _loader_directories
    or _loader_directories[0] != _REQUIRED_LIBIIO_BOOTSTRAP_DIRECTORY
):
    if not _PINNED_PYTHON.is_file() or not os.access(_PINNED_PYTHON, os.X_OK):
        raise SystemExit(f"pinned capture Python is not executable: {_PINNED_PYTHON}")
    environment = dict(os.environ)
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_SMATEWAY_SOURCE)
        if not prior_pythonpath
        else f"{_SMATEWAY_SOURCE}{os.pathsep}{prior_pythonpath}"
    )
    prior_loader_path = environment.get("LD_LIBRARY_PATH", "")
    loader_entries = [
        item
        for item in prior_loader_path.split(os.pathsep)
        if item and Path(item).resolve() != _REQUIRED_LIBIIO_BOOTSTRAP_DIRECTORY
    ]
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (str(_REQUIRED_LIBIIO_BOOTSTRAP_DIRECTORY), *loader_entries)
    )
    os.execve(
        str(_PINNED_PYTHON),
        [str(_PINNED_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )

if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

import numpy as np
from pluto_plus.artifacts import CaptureWriter, load_metadata, verify_artifact
from pluto_plus.hardware import SampleBlockV2
from pluto_plus.models import ArtifactSummary, GainMode, RadioSettings

from scripts import run_5g8_leakage_ladder as leakage
from smateway.bench import BenchManifest, OpenOcdBench
from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    attest_pluto_plus_utils_source,
    audit_continuity_metadata,
    sha256_path,
    validate_tx1_rf_readback_evidence,
    write_json_atomic,
)
from smateway.leakage_ladder import analyze_coherent_leakage
from smateway.native_iio_attestation import (
    REQUIRED_LIBIIO_DIRECTORY,
    REQUIRED_LIBIIO_PATH as REQUIRED_LIBIIO_PATH,
    REQUIRED_LIBIIO_SHA256 as REQUIRED_LIBIIO_SHA256,
    REQUIRED_LIBIIO_SYMBOLS as REQUIRED_LIBIIO_SYMBOLS,
    REQUIRED_LIBIIO_VERSION as REQUIRED_LIBIIO_VERSION,
    RuntimeAttestationBoundary,
    attest_runtime,
    attestation_sha256,
    call_runtime_preflight,
    runtime_preflight_passed,
    validate_runtime_attestation,
)
from smateway.one_hot_ladder import (
    ALL_OFF_STATE,
    ANTENNA_STATES,
    DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB,
    DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG,
    DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB,
    ONE_HOT_STATE_ORDER,
    TOPOLOGY_IDENTITY,
    VerifiedOneHotRowBundle,
    _seal_verified_one_hot_row_bundle,
    one_hot_cell_role,
    physical_confirmation_token,
    summarize_one_hot_run,
    validate_antenna_name,
    validate_one_hot_state_codes,
    validate_one_hot_matrix_identity,
)
from smateway.ota_analysis import estimate_coherent_pilot_offset
from smateway.profile import load_profile

if REQUIRED_LIBIIO_DIRECTORY != _REQUIRED_LIBIIO_BOOTSTRAP_DIRECTORY:
    raise RuntimeError("native libiio bootstrap and attestation paths differ")

DEFAULT_BOARD_ID = leakage.DEFAULT_BOARD_ID
SELECTED_STATE_LEASE_MS = 5_000
ATTRIBUTION_TX_HARDWARE_GAIN_DB = -20.0
ATTRIBUTION_REPEAT_COUNT = 3
MINIMUM_DETECTED_ATTRIBUTION_REPEATS = 3
PLAN_FILENAME = "plan.json"
MANIFEST_FILENAME = "manifest.json"
RUN_STATE_DIRECTORY = ".run-state"
RUN_STATE_EVENT_KIND = "5g8_one_hot_run_state_event"
RUN_STATE_EVENT_PATTERN = re.compile(r"(?P<revision>[0-9]{20})\.json")
RUN_STARTED_TOMBSTONE = "execution-started.json"
RUN_FAILED_TOMBSTONE = "failed.json"
CONDITION_RECORD_NAME = "5g8-one-hot-path-condition.json"
BASE_TEMPLATE_STAGE = "powered_selector_all_inputs_terminated"
BENCH_FLASH_BASE_ADDRESS = 0x08000000
GPIOA_ODR_ADDRESS = 0x50000014
SELECTOR_GPIO_MASK = 0xF
ALL_OFF_CLEANUP_PURPOSES = frozenset(
    {
        "cleanup_all_off",
        "final_cleanup_all_off",
        "preflight_cleanup_all_off",
        "resume_cleanup_all_off",
        "identity_failure_cleanup_all_off",
    }
)
MINIMUM_SELECTED_HOLD_LEASE_DECREMENT_MS = int(
    0.5 * 1_000 * leakage.TOTAL_SAMPLES / leakage.SAMPLE_RATE_HZ
)


class OneHotLadderError(leakage.LeakageLadderError):
    """An immutable plan, selector state, RF safety, or artifact invariant failed."""


class OneHotSelectorBoundary(Protocol):
    """Injectable mailbox seam for selected-state holds and ALL_OFF cleanup."""

    def __call__(
        self,
        selector_control: Mapping[str, Any],
        state_name: str,
        state_code: int,
        purpose: str,
    ) -> dict[str, Any]: ...


class TargetImageBoundary(Protocol):
    """Injectable exact target-flash attestation seam."""

    def __call__(self, selector_control: Mapping[str, Any]) -> dict[str, Any]: ...


def _now() -> str:
    return leakage._now()


def _native_runtime_from_contract(
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    source = contract.get("source")
    if not isinstance(source, Mapping):
        raise OneHotLadderError("immutable one-hot plan lacks source identity")
    try:
        runtime = validate_runtime_attestation(source.get("native_libiio_runtime_attestation"))
        runtime_sha256 = attestation_sha256(runtime)
    except (TypeError, ValueError) as error:
        raise OneHotLadderError(
            "immutable one-hot plan lacks exact native libiio attestation"
        ) from error
    if source.get("native_libiio_runtime_attestation_sha256") != runtime_sha256:
        raise OneHotLadderError("immutable one-hot native libiio attestation hash differs")
    return runtime, runtime_sha256


def _fixture_identity_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    shared = value.get("shared_hardware")
    evidence = value.get("setup_evidence")
    if not isinstance(shared, Mapping) or not isinstance(evidence, Mapping):
        raise ValueError("one-hot fixture identity is malformed")
    required_shared = {
        "feed_arm_id",
        "feed_cable_id",
        "termination_load_set_id",
        "rx1_reference_plane_id",
        "rx2_reference_plane_id",
    }
    if set(shared) != required_shared:
        raise ValueError("one-hot shared fixture identifiers are incomplete")
    normalized_shared = {
        key: leakage._validate_identifier(str(shared[key]), key) for key in sorted(required_shared)
    }
    evidence_path = evidence.get("path")
    if not isinstance(evidence_path, str) or not evidence_path:
        raise ValueError("one-hot setup evidence path is missing")
    evidence_sha = _sha256_contract(evidence.get("file_sha256"), "setup evidence")
    if value.get("attribution_repeats_without_cable_movement_required") is not True:
        raise ValueError("one-hot fixture must freeze no-movement attribution repeats")
    return {
        "shared_hardware": normalized_shared,
        "setup_evidence": {"path": evidence_path, "file_sha256": evidence_sha},
        "attribution_repeats_without_cable_movement_required": True,
    }


def _fixture_identity_from_cli(
    *,
    feed_arm_id: str,
    feed_cable_id: str,
    termination_load_set_id: str,
    rx1_reference_plane_id: str,
    rx2_reference_plane_id: str,
    setup_evidence_path: Path,
) -> dict[str, Any]:
    evidence_path = setup_evidence_path.expanduser().resolve(strict=True)
    return _fixture_identity_contract(
        {
            "shared_hardware": {
                "feed_arm_id": feed_arm_id,
                "feed_cable_id": feed_cable_id,
                "termination_load_set_id": termination_load_set_id,
                "rx1_reference_plane_id": rx1_reference_plane_id,
                "rx2_reference_plane_id": rx2_reference_plane_id,
            },
            "setup_evidence": {
                "path": str(evidence_path),
                "file_sha256": sha256_path(evidence_path),
            },
            "attribution_repeats_without_cable_movement_required": True,
        }
    )


def _matrix_identity_from_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the row-invariant DUT, control, source, and acquisition contract."""

    configuration = contract.get("configuration")
    source = contract.get("source")
    selector = contract.get("selector_control")
    if not all(isinstance(item, Mapping) for item in (configuration, source, selector)):
        raise ValueError("one-hot plan lacks a common matrix identity")
    assert isinstance(configuration, Mapping)
    assert isinstance(source, Mapping)
    assert isinstance(selector, Mapping)
    binding = selector.get("bench_profile_binding")
    bench_manifest = selector.get("bench_manifest")
    openocd = selector.get("openocd_config")
    profile = selector.get("control_profile")
    if not all(isinstance(item, Mapping) for item in (binding, bench_manifest, openocd, profile)):
        raise ValueError("one-hot selector identity is incomplete")
    assert isinstance(binding, Mapping)
    assert isinstance(bench_manifest, Mapping)
    assert isinstance(openocd, Mapping)
    assert isinstance(profile, Mapping)
    bench_elf = binding.get("bench_elf")
    bench_bin = binding.get("bench_bin")
    reproducible = binding.get("reproducible_source_build")
    provenance = binding.get("profile_provenance")
    if not all(
        isinstance(item, Mapping) for item in (bench_elf, bench_bin, reproducible, provenance)
    ):
        raise ValueError("one-hot bench/profile identity is incomplete")
    assert isinstance(bench_elf, Mapping)
    assert isinstance(bench_bin, Mapping)
    assert isinstance(reproducible, Mapping)
    assert isinstance(provenance, Mapping)
    _, native_runtime_sha256 = _native_runtime_from_contract(contract)
    acquisition = {
        key: value
        for key, value in configuration.items()
        if key
        not in {
            "uri",
            "driven_input",
            "fixture_identity",
            "other_seven_inputs_individually_terminated",
        }
    }
    acquisition_sha256 = hashlib.sha256(
        json.dumps(
            acquisition,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return validate_one_hot_matrix_identity(
        {
            "board_id": contract.get("board_id"),
            "pluto_serial": configuration.get("serial"),
            "smateway_commit": source.get("smateway_commit"),
            "pluto_plus_utils_source_attestation_sha256": source.get(
                "pluto_plus_utils_source_attestation_sha256"
            ),
            "native_libiio_runtime_attestation_sha256": native_runtime_sha256,
            "bench_manifest_sha256": bench_manifest.get("file_sha256"),
            "bench_elf_sha256": bench_elf.get("file_sha256"),
            "bench_bin_sha256": bench_bin.get("file_sha256"),
            "bench_protocol_sha256": reproducible.get("tracked_bench_protocol_sha256"),
            "bench_verifier_sha256": reproducible.get("verifier_sha256"),
            "openocd_config_sha256": openocd.get("file_sha256"),
            "control_profile_contract_sha256": profile.get("contract_sha256"),
            "control_profile_sha256": profile.get("file_sha256"),
            "control_profile_header_sha256": profile.get("header_file_sha256"),
            "control_profile_provenance_sha256": provenance.get("file_sha256"),
            "acquisition_configuration": acquisition,
            "acquisition_configuration_sha256": acquisition_sha256,
        }
    )


def _selector_states_from_control(
    selector_control: Mapping[str, Any],
) -> tuple[dict[str, int | str], ...]:
    raw = selector_control.get("one_hot_static_states")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError("selector control lacks one-hot static states")
    profile = selector_control.get("control_profile")
    if not isinstance(profile, Mapping):
        raise ValueError("selector control profile is malformed")
    all_off_code = profile.get("all_off_code")
    if isinstance(all_off_code, bool) or not isinstance(all_off_code, int):
        raise ValueError("selector control ALL_OFF code is malformed")
    antenna_states = [
        {"name": item.get("name"), "gpio_code": item.get("gpio_code")}
        for item in raw
        if item.get("name") != ALL_OFF_STATE
    ]
    normalized = validate_one_hot_state_codes(
        antenna_states,
        all_off_code=all_off_code,
    )
    if [dict(item) for item in normalized] != [dict(item) for item in raw]:
        raise ValueError("selector one-hot state map is not canonical")
    return normalized


def _sha256_contract(value: object, label: str) -> str:
    digest = str(value)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} SHA-256 is malformed")
    return digest


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _extract_control_schedule(elf_path: Path) -> dict[str, Any]:
    """Read the exact CONTROL_SCHEDULE bytes from a reviewed bench ELF."""

    nm = subprocess.run(
        ("arm-none-eabi-nm", "-S", "-a", str(elf_path)),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    matches = re.findall(
        r"^([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s+\w\s+CONTROL_SCHEDULE$",
        nm,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("bench ELF must contain exactly one sized CONTROL_SCHEDULE")
    address = int(matches[0][0], 16)
    size = int(matches[0][1], 16)
    dump = subprocess.run(
        (
            "arm-none-eabi-objdump",
            "-s",
            f"--start-address=0x{address:x}",
            f"--stop-address=0x{address + size:x}",
            str(elf_path),
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    payload = bytearray()
    for line in dump.splitlines():
        fields = line.split()
        if not fields or re.fullmatch(r"[0-9A-Fa-f]+", fields[0]) is None:
            continue
        for field in fields[1:]:
            if re.fullmatch(r"[0-9A-Fa-f]{2,8}", field) is None or len(field) % 2:
                break
            payload.extend(bytes.fromhex(field))
    if len(payload) != size:
        raise ValueError("bench ELF CONTROL_SCHEDULE bytes could not be read exactly")
    return {
        "symbol": "CONTROL_SCHEDULE",
        "address": address,
        "size_bytes": size,
        "bytes_hex": bytes(payload).hex(),
        "extraction": "arm-none-eabi-nm sized symbol plus bounded objdump bytes",
    }


def _bench_profile_binding_contract(
    *,
    bench_manifest_path: Path,
    profile_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    """Mutually bind profile JSON/header/provenance to the exact bench ELF bytes."""

    manifest_path = bench_manifest_path.expanduser().resolve(strict=True)
    exact_profile_path = profile_path.expanduser().resolve(strict=True)
    header_path = exact_profile_path.with_name("control_profile.h").resolve(strict=True)
    provenance_path = exact_profile_path.with_name("provenance.json").resolve(strict=True)
    elf_path = manifest_path.with_name("pluto_bench.elf").resolve(strict=True)
    bin_path = manifest_path.with_name("pluto_bench.bin").resolve(strict=True)
    manifest = BenchManifest.load(manifest_path)
    manifest_json = _read_json_object(manifest_path, "bench manifest")
    profile = load_profile(exact_profile_path)
    provenance = _read_json_object(provenance_path, "control-profile provenance")
    artifacts = provenance.get("artifacts")
    if (
        provenance.get("schema") != 1
        or provenance.get("contract_sha256") != profile.contract_sha256
        or not isinstance(artifacts, Mapping)
        or artifacts.get("control_profile.json") != sha256_path(exact_profile_path)
        or artifacts.get("control_profile.h") != sha256_path(header_path)
    ):
        raise ValueError("profile JSON/header do not match their provenance contract")
    elf_sha256 = sha256_path(elf_path)
    if elf_sha256 != manifest.elf_sha256:
        raise ValueError("bench ELF does not match the bench manifest ELF SHA-256")
    protocol_path = (_REPOSITORY / "firmware/stm32c011/apps/bench/bench_protocol.h").resolve(
        strict=True
    )
    protocol_sha256 = sha256_path(protocol_path)
    if manifest_json.get("protocol_sha256") != protocol_sha256:
        raise ValueError("bench manifest protocol hash differs from tracked source")
    verifier_path = (_REPOSITORY / "scripts/verify_bench_elf.py").resolve(strict=True)
    subprocess.run(
        (sys.executable, str(verifier_path), str(elf_path)),
        check=True,
        capture_output=True,
        text=True,
    )
    with tempfile.TemporaryDirectory(prefix="one-hot-bench-elf-") as temporary:
        reconstructed_bin = Path(temporary) / "pluto_bench.bin"
        subprocess.run(
            (
                "arm-none-eabi-objcopy",
                "-O",
                "binary",
                str(elf_path),
                str(reconstructed_bin),
            ),
            check=True,
            capture_output=True,
        )
        if reconstructed_bin.read_bytes() != bin_path.read_bytes():
            raise ValueError("bench BIN is not the exact binary projection of the ELF")
    with tempfile.TemporaryDirectory(prefix="one-hot-reproducible-bench-") as temporary:
        rebuild_root = Path(temporary) / "build"
        rebuild_bin = rebuild_root / "STM32C011F4P6/bench/pluto_bench.bin"
        rebuild_manifest = rebuild_root / "STM32C011F4P6/bench/pluto_bench.manifest.json"
        subprocess.run(
            (
                "make",
                "-C",
                str(_REPOSITORY),
                f"BUILD_DIR={rebuild_root}",
                str(rebuild_bin),
                str(rebuild_manifest),
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        rebuilt_bin_sha256 = sha256_path(rebuild_bin)
        if rebuild_bin.read_bytes() != bin_path.read_bytes():
            raise ValueError("supplied bench BIN differs from an independent clean-source rebuild")
    expected_schedule = b"".join(
        bytes((state.gpio_code, 0)) + struct.pack("<H", state.dwell_ms) for state in profile.states
    )
    observed_schedule = _extract_control_schedule(elf_path)
    if (
        observed_schedule["size_bytes"] != len(expected_schedule)
        or observed_schedule["bytes_hex"] != expected_schedule.hex()
    ):
        raise ValueError("bench ELF CONTROL_SCHEDULE differs from the generated profile")
    return {
        "schema": 1,
        "binding_kind": "profile_json_header_provenance_to_bench_elf_schedule",
        "bench_elf": {
            "path": str(elf_path),
            "file_sha256": elf_sha256,
            "manifest_declared_elf_sha256": manifest.elf_sha256,
        },
        "bench_bin": {
            "path": str(bin_path),
            "file_sha256": sha256_path(bin_path),
            "size_bytes": bin_path.stat().st_size,
            "flash_base_address": BENCH_FLASH_BASE_ADDRESS,
            "derived_from_elf_with": "arm-none-eabi-objcopy -O binary",
        },
        "reproducible_source_build": {
            "source_repository": str(_REPOSITORY),
            "source_commit": source_commit,
            "fresh_build_directory_used": True,
            "rebuilt_bin_sha256": rebuilt_bin_sha256,
            "exact_bin_match": True,
            "tracked_bench_protocol_path": str(protocol_path),
            "tracked_bench_protocol_sha256": protocol_sha256,
            "manifest_protocol_sha256": manifest_json["protocol_sha256"],
            "verifier_path": str(verifier_path),
            "verifier_sha256": sha256_path(verifier_path),
            "verify_bench_elf_passed": True,
            "toolchain": subprocess.run(
                ("arm-none-eabi-gcc", "--version"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()[0],
        },
        "profile_provenance": {
            "path": str(provenance_path),
            "file_sha256": sha256_path(provenance_path),
            "contract_sha256": profile.contract_sha256,
            "profile_file_sha256": sha256_path(exact_profile_path),
            "header_file_sha256": sha256_path(header_path),
        },
        "control_schedule": {
            **observed_schedule,
            "expected_bytes_hex": expected_schedule.hex(),
            "state_names": [state.name for state in profile.states],
            "gpio_codes": [state.gpio_code for state in profile.states],
            "dwell_ms": [state.dwell_ms for state in profile.states],
        },
    }


def _one_hot_selector_control_contract(
    *,
    bench_manifest_path: Path,
    openocd_config_path: Path,
    profile_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    """Freeze the exact bench/profile artifacts and static ANT state codes."""

    control = leakage._selector_control_contract(
        bench_manifest_path=bench_manifest_path,
        openocd_config_path=openocd_config_path,
        profile_path=profile_path,
    )
    profile = load_profile(profile_path.expanduser().resolve(strict=True))
    states = validate_one_hot_state_codes(
        [{"name": state.name, "gpio_code": state.gpio_code} for state in profile.states],
        all_off_code=profile.all_off_code,
    )
    manifest_document = control["bench_manifest"]
    assert isinstance(manifest_document, Mapping)
    maximum_lease_ms = int(manifest_document["max_lease_ms"])
    if maximum_lease_ms < SELECTED_STATE_LEASE_MS:
        raise ValueError("selected-state lease exceeds the reviewed bench maximum")
    control["one_hot_static_states"] = [dict(item) for item in states]
    control["bench_profile_binding"] = _bench_profile_binding_contract(
        bench_manifest_path=bench_manifest_path,
        profile_path=profile_path,
        source_commit=source_commit,
    )
    control["selected_state_lease_ms"] = SELECTED_STATE_LEASE_MS
    control["state_hold_contract"] = {
        "marker_used": False,
        "one_fresh_stream_per_static_state_gain_condition": True,
        "selected_state_readback_before_capture": True,
        "selected_state_readback_after_pluto_mute": True,
        "all_off_readback_after_every_condition": True,
        "gpioa_odr_latch_readback_at_every_boundary": True,
        "physical_rf_state_proven_by_digital_readback": False,
    }
    return _validate_one_hot_selector_control(control)


def _validate_one_hot_selector_control(value: Mapping[str, Any]) -> dict[str, Any]:
    control = leakage._validate_selector_control_contract(value)
    states = _selector_states_from_control(control)
    lease_ms = control.get("selected_state_lease_ms")
    manifest = control.get("bench_manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("selector bench manifest contract is malformed")
    maximum_lease_ms = manifest.get("max_lease_ms")
    if (
        isinstance(lease_ms, bool)
        or not isinstance(lease_ms, int)
        or lease_ms < 1
        or isinstance(maximum_lease_ms, bool)
        or not isinstance(maximum_lease_ms, int)
        or lease_ms > maximum_lease_ms
    ):
        raise ValueError("selector selected-state lease contract is invalid")
    hold = control.get("state_hold_contract")
    if not isinstance(hold, Mapping) or hold != {
        "marker_used": False,
        "one_fresh_stream_per_static_state_gain_condition": True,
        "selected_state_readback_before_capture": True,
        "selected_state_readback_after_pluto_mute": True,
        "all_off_readback_after_every_condition": True,
        "gpioa_odr_latch_readback_at_every_boundary": True,
        "physical_rf_state_proven_by_digital_readback": False,
    }:
        raise ValueError("selector static-state hold contract is invalid")
    binding = control.get("bench_profile_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("selector bench/profile binding is missing")
    elf = binding.get("bench_elf")
    bench_bin = binding.get("bench_bin")
    provenance = binding.get("profile_provenance")
    reproducible = binding.get("reproducible_source_build")
    schedule = binding.get("control_schedule")
    if (
        binding.get("schema") != 1
        or binding.get("binding_kind") != "profile_json_header_provenance_to_bench_elf_schedule"
        or not all(
            isinstance(item, Mapping)
            for item in (elf, bench_bin, provenance, reproducible, schedule)
        )
    ):
        raise ValueError("selector bench/profile binding is malformed")
    assert isinstance(elf, Mapping)
    assert isinstance(bench_bin, Mapping)
    assert isinstance(provenance, Mapping)
    assert isinstance(reproducible, Mapping)
    assert isinstance(schedule, Mapping)
    manifest_elf_sha = manifest.get("elf_sha256")
    profile_contract = control.get("control_profile", {}).get("contract_sha256")
    reproducible_source_commit = str(reproducible.get("source_commit", ""))
    leakage._validate_commit(reproducible_source_commit, "bench reproducible-build source commit")
    if (
        _sha256_contract(elf.get("file_sha256"), "bench ELF") != manifest_elf_sha
        or elf.get("manifest_declared_elf_sha256") != manifest_elf_sha
        or _sha256_contract(bench_bin.get("file_sha256"), "bench BIN") == ""
        or bench_bin.get("flash_base_address") != BENCH_FLASH_BASE_ADDRESS
        or isinstance(bench_bin.get("size_bytes"), bool)
        or not isinstance(bench_bin.get("size_bytes"), int)
        or int(bench_bin["size_bytes"]) < 1
        or reproducible.get("exact_bin_match") is not True
        or reproducible.get("fresh_build_directory_used") is not True
        or reproducible.get("rebuilt_bin_sha256") != bench_bin.get("file_sha256")
        or reproducible.get("manifest_protocol_sha256")
        != reproducible.get("tracked_bench_protocol_sha256")
        or reproducible.get("verify_bench_elf_passed") is not True
        or _sha256_contract(provenance.get("file_sha256"), "profile provenance") == ""
        or provenance.get("contract_sha256") != profile_contract
        or provenance.get("profile_file_sha256")
        != control.get("control_profile", {}).get("file_sha256")
        or provenance.get("header_file_sha256")
        != control.get("control_profile", {}).get("header_file_sha256")
        or schedule.get("symbol") != "CONTROL_SCHEDULE"
        or schedule.get("size_bytes") != 32
        or schedule.get("bytes_hex") != schedule.get("expected_bytes_hex")
        or schedule.get("state_names") != list(ANTENNA_STATES)
        or schedule.get("gpio_codes") != [int(state["gpio_code"]) for state in states[1:]]
        or not isinstance(schedule.get("dwell_ms"), list)
        or len(schedule["dwell_ms"]) != 8
    ):
        raise ValueError("selector bench/profile binding is inconsistent")
    control["one_hot_static_states"] = [dict(item) for item in states]
    return control


def _build_plan_contract(
    *,
    run_id: str,
    board_id: str,
    serial: str,
    uri: str,
    driven_input: str,
    source_commit: str,
    pluto_plus_utils_source_attestation: Mapping[str, Any],
    native_libiio_runtime_attestation: Mapping[str, Any],
    selector_control: Mapping[str, Any],
    fixture_identity: Mapping[str, Any],
) -> dict[str, Any]:
    exact_driven_input = validate_antenna_name(driven_input, "driven input")
    control = _validate_one_hot_selector_control(selector_control)
    reproducible = control["bench_profile_binding"]["reproducible_source_build"]
    if not isinstance(reproducible, Mapping) or reproducible.get("source_commit") != source_commit:
        raise ValueError("bench reproducible build is not bound to the plan source commit")
    fixture = _fixture_identity_contract(fixture_identity)
    native_runtime = validate_runtime_attestation(native_libiio_runtime_attestation)
    base = leakage._build_plan_contract(
        run_id=run_id,
        board_id=board_id,
        serial=serial,
        uri=uri,
        stage=BASE_TEMPLATE_STAGE,
        source_commit=source_commit,
        pluto_plus_utils_source_attestation=pluto_plus_utils_source_attestation,
        native_libiio_runtime_attestation=native_runtime,
        selector_control=control,
        freeze_attribution_repeats=False,
    )
    templates = {
        float(condition["tx_hardware_gain_db"]): condition for condition in base["conditions"]
    }
    states = _selector_states_from_control(control)
    conditions: list[dict[str, Any]] = []
    plan_index = 0
    for gain_db in leakage.TX_HARDWARE_GAINS_DB:
        template = templates[gain_db]
        for state_index, state in enumerate(states):
            state_name = str(state["name"])
            state_code = int(state["gpio_code"])
            repeat_count = (
                ATTRIBUTION_REPEAT_COUNT if gain_db == ATTRIBUTION_TX_HARDWARE_GAIN_DB else 1
            )
            for repeat_index in range(repeat_count):
                condition = dict(template)
                condition.update(
                    {
                        "plan_index": plan_index,
                        "condition_id": (
                            f"drive-{exact_driven_input.lower()}-"
                            f"select-{state_name.lower()}-tx{gain_db:g}db-"
                            f"r{repeat_index + 1}of{repeat_count}"
                        ),
                        "stage": TOPOLOGY_IDENTITY,
                        "topology_identity": TOPOLOGY_IDENTITY,
                        "driven_input": exact_driven_input,
                        "fixture_identity": fixture,
                        "physical_cell_role": one_hot_cell_role(
                            exact_driven_input,
                            state_name,
                        ),
                        "selector_state_index": state_index,
                        "selector_state_name": state_name,
                        "selector_gpio_code": state_code,
                        "repeat_index": repeat_index,
                        "repeat_count_at_gain": repeat_count,
                        "independent_fresh_stream_repeat": True,
                        "attribution_gain_condition": (gain_db == ATTRIBUTION_TX_HARDWARE_GAIN_DB),
                        "selector_state_hold_mode": "static_mailbox_lease",
                        "selector_selected_state_lease_ms": (
                            0 if state_name == ALL_OFF_STATE else SELECTED_STATE_LEASE_MS
                        ),
                        "selector_readback_before_capture_required": True,
                        "selector_readback_after_pluto_mute_required": True,
                        "selector_all_off_cleanup_required": True,
                        "marker_required": False,
                    }
                )
                conditions.append(condition)
                plan_index += 1

    base["plan_kind"] = "5g8_marker_independent_one_hot_selector_path_ladder"
    base["topology_stage"] = TOPOLOGY_IDENTITY
    base["topology_identity"] = TOPOLOGY_IDENTITY
    base["driven_input"] = exact_driven_input
    base["fixture_identity"] = fixture
    other_inputs = [name for name in ANTENNA_STATES if name != exact_driven_input]
    base["stage_contract"] = {
        "topology_identity": TOPOLOGY_IDENTITY,
        "confirmation_token": physical_confirmation_token(exact_driven_input),
        "driven_board_input": exact_driven_input,
        "shared_fixture_identity": fixture["shared_hardware"],
        "setup_evidence": fixture["setup_evidence"],
        "attribution_repeats_without_cable_movement_required": True,
        "driven_input_topology": (
            f"TX1 conducted stimulus branch connects only to board input {exact_driven_input}"
        ),
        "terminated_board_inputs": other_inputs,
        "terminated_board_inputs_contract": (
            "each of the other seven board inputs has its own 5.8 GHz 50 ohm termination"
        ),
        "simultaneous_eight_way_feed_present": False,
        "eight_way_splitter_in_board_input_path": False,
        "rx2_topology": "selector common connects through the fixed test cable to Pluto RX2",
        "tx1_reference_topology": (
            "TX1 feeds a matched two-way conducted network: one attenuated branch feeds RX1; "
            f"the other branch feeds only {exact_driven_input}"
        ),
        "selector_state_contract": (
            "one static mailbox plus GPIOA ODR-latch-read-back state per fresh capture; selected "
            "state re-read after Pluto mute; ALL_OFF commanded and digitally read back after "
            "every condition; digital evidence does not prove the physical RF state"
        ),
    }
    base["source"] = {
        **dict(base["source"]),
        "runner": "scripts/run_5g8_one_hot_path_ladder.py",
        "run_aggregator": "smateway.one_hot_ladder.summarize_one_hot_run",
        "matrix_aggregator": "smateway.one_hot_ladder.summarize_complete_one_hot_matrix",
    }
    base["selector_control"] = control
    configuration = dict(base["configuration"])
    configuration.update(
        {
            "selector_state_order": list(ONE_HOT_STATE_ORDER),
            "selector_state_count": len(ONE_HOT_STATE_ORDER),
            "selected_state_lease_ms": SELECTED_STATE_LEASE_MS,
            "minimum_selected_hold_lease_decrement_ms": (MINIMUM_SELECTED_HOLD_LEASE_DECREMENT_MS),
            "conditions_per_non_attribution_gain": len(ONE_HOT_STATE_ORDER),
            "conditions_at_attribution_gain": (len(ONE_HOT_STATE_ORDER) * ATTRIBUTION_REPEAT_COUNT),
            "condition_count": len(conditions),
            "attribution_tx_hardware_gain_db": ATTRIBUTION_TX_HARDWARE_GAIN_DB,
            "attribution_repeat_count": ATTRIBUTION_REPEAT_COUNT,
            "minimum_detected_attribution_repeats": (MINIMUM_DETECTED_ATTRIBUTION_REPEATS),
            "minimum_intended_through_contrast_over_all_off_db": (
                DEFAULT_MINIMUM_INTENDED_THROUGH_CONTRAST_OVER_ALL_OFF_DB
            ),
            "maximum_attribution_amplitude_span_db": (
                DEFAULT_MAXIMUM_ATTRIBUTION_AMPLITUDE_SPAN_DB
            ),
            "maximum_attribution_phase_residual_deg": (
                DEFAULT_MAXIMUM_ATTRIBUTION_PHASE_RESIDUAL_DEG
            ),
            "topology_identity": TOPOLOGY_IDENTITY,
            "driven_input": exact_driven_input,
            "fixture_identity": fixture,
            "other_seven_inputs_individually_terminated": other_inputs,
            "simultaneous_eight_way_feed_present": False,
        }
    )
    base["configuration"] = configuration
    base["operator_confirmations_required"] = {
        "no_antennas_anywhere": True,
        "tx1_matched_single_input_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "topology_identity": TOPOLOGY_IDENTITY,
        "driven_input": exact_driven_input,
        "other_seven_inputs_individually_50ohm_terminated": True,
        "simultaneous_eight_way_feed_absent": True,
        "reviewed_static_one_hot_mailbox_control": True,
        "physical_confirmation_token": physical_confirmation_token(exact_driven_input),
        "fixture_identity": fixture,
        "attribution_repeats_without_cable_movement": True,
    }
    safety = dict(base["safety"])
    safety.pop("selector_static_all_off_readback_required", None)
    safety.update(
        {
            "selector_mailbox_and_gpio_latch_readback_before_every_capture": True,
            "selector_mailbox_and_gpio_latch_readback_after_pluto_mute": True,
            "selector_all_off_digital_cleanup_after_every_condition": True,
            "selector_all_off_cleanup_in_stage_finally": True,
            "digital_selector_readback_does_not_prove_physical_rf_state": True,
            "selected_state_lease_expiry_is_failure": True,
            "exactly_one_board_input_driven": True,
            "other_seven_board_inputs_individually_terminated": True,
            "simultaneous_eight_way_feed_forbidden": True,
            "serial_independent_global_selector_lock_required": True,
        }
    )
    base["safety"] = safety
    storage = dict(base["storage"])
    storage["estimated_raw_iq_bytes"] = (
        len(conditions) * leakage.TOTAL_SAMPLES * 2 * 2 * np.dtype("<i2").itemsize
    )
    storage["capture_root_scope"] = "local_RPi_run_specific_one_hot_staging_root"
    storage["unreferenced_run_root_entries_quarantined_on_resume"] = True
    storage["run_capture_root"] = str(
        leakage._board_root(str(base["board_id"]))
        / "pluto-usb-captures"
        / "one-hot-runs"
        / str(base["run_id"])
    )
    base["storage"] = storage
    base["interpretation"] = {
        "purpose": (
            f"measure the {exact_driven_input} physical matrix row across static ALL_OFF and "
            "ANT1..ANT8 RX2/RX1 transfer under a bounded TX1 gain ladder"
        ),
        "marker_required": False,
        "static_selector_state_readback_required": True,
        "one_run_represents_exactly_one_driven_input": True,
        "driven_input": exact_driven_input,
        "fixture_identity": fixture,
        "intended_through_selector_state": exact_driven_input,
        "all_off_cell_count_in_this_run": 1,
        "intended_through_cell_count_in_this_run": 1,
        "wrong_state_cell_count_in_this_run": 7,
        "independent_attribution_repeats": ATTRIBUTION_REPEAT_COUNT,
        "attribution_gain_db": ATTRIBUTION_TX_HARDWARE_GAIN_DB,
        "cross_gain_observations_are_not_repeatability_claims": True,
        "eight_independently_confirmed_manifests_required_for_complete_matrix": True,
        "simultaneous_feed_fixture_claim": False,
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
        "operational_switching_claim": False,
    }
    base["conditions"] = conditions
    return base


def _validate_confirmations(
    *,
    driven_input: str,
    fixture_identity: Mapping[str, Any],
    topology_token: str | None,
    no_antennas: bool,
    tx1_matched: bool,
    tx2_terminated_muted: bool,
    rx1_conducted_reference: bool,
    one_hot_static_control: bool,
    single_driven_input: bool,
    other_seven_terminated: bool,
    no_simultaneous_eight_way_feed: bool,
    attribution_repeats_no_cable_movement: bool,
) -> dict[str, Any]:
    exact_driven_input = validate_antenna_name(driven_input, "driven input")
    fixture = _fixture_identity_contract(fixture_identity)
    required = {
        "--confirm-no-antennas": no_antennas,
        "--confirm-tx1-matched-conducted": tx1_matched,
        "--confirm-tx2-terminated-muted": tx2_terminated_muted,
        "--confirm-rx1-conducted-reference": rx1_conducted_reference,
        "--confirm-one-hot-static-control": one_hot_static_control,
        "--confirm-single-driven-input": single_driven_input,
        "--confirm-other-seven-terminated": other_seven_terminated,
        "--confirm-no-simultaneous-eight-way-feed": no_simultaneous_eight_way_feed,
        "--confirm-attribution-repeats-no-cable-movement": (attribution_repeats_no_cable_movement),
    }
    missing = [flag for flag, passed in required.items() if not passed]
    if missing:
        raise OneHotLadderError(f"execution requires {missing[0]}")
    expected_token = physical_confirmation_token(exact_driven_input)
    if topology_token != expected_token:
        raise OneHotLadderError(f"execution requires --confirm-topology-token {expected_token}")
    return {
        "confirmed_at": _now(),
        "topology_identity": TOPOLOGY_IDENTITY,
        "driven_input": exact_driven_input,
        "fixture_identity": fixture,
        "topology_confirmation_token": expected_token,
        "no_antennas_anywhere": True,
        "tx1_matched_conducted_network": True,
        "tx2_muted_and_50ohm_terminated": True,
        "rx1_attenuated_conducted_reference": True,
        "reviewed_static_one_hot_mailbox_control": True,
        "exactly_one_board_input_driven": True,
        "other_seven_board_inputs_individually_50ohm_terminated": True,
        "simultaneous_eight_way_feed_absent": True,
        "attribution_repeats_without_cable_movement": True,
        "confirmation_method": "explicit CLI flags after physical inspection",
    }


def _state_map(selector_control: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(state["name"]): int(state["gpio_code"])
        for state in _selector_states_from_control(selector_control)
    }


def _verify_one_hot_artifacts(selector_control: Mapping[str, Any]) -> None:
    """Re-read and mutually verify every selector artifact before OpenOCD use."""

    leakage._verify_selector_artifacts(selector_control)
    control = _validate_one_hot_selector_control(selector_control)
    binding = control["bench_profile_binding"]
    assert isinstance(binding, Mapping)
    for section_name in ("bench_elf", "bench_bin", "profile_provenance"):
        section = binding[section_name]
        assert isinstance(section, Mapping)
        path = Path(str(section["path"])).resolve(strict=True)
        if sha256_path(path) != section.get("file_sha256"):
            raise OneHotLadderError(
                "live bench/profile artifact differs from the immutable one-hot plan"
            )
    reproducible = binding["reproducible_source_build"]
    schedule = binding["control_schedule"]
    bench_elf = binding["bench_elf"]
    assert isinstance(reproducible, Mapping)
    assert isinstance(schedule, Mapping)
    assert isinstance(bench_elf, Mapping)
    for path_key, hash_key in (
        ("tracked_bench_protocol_path", "tracked_bench_protocol_sha256"),
        ("verifier_path", "verifier_sha256"),
    ):
        path = Path(str(reproducible[path_key])).resolve(strict=True)
        if sha256_path(path) != reproducible.get(hash_key):
            raise OneHotLadderError("tracked bench verifier/source input changed")
    observed_schedule = _extract_control_schedule(Path(str(bench_elf["path"])))
    if any(
        observed_schedule.get(key) != schedule.get(key)
        for key in ("symbol", "address", "size_bytes", "bytes_hex")
    ):
        raise OneHotLadderError("live bench ELF schedule differs from immutable binding")


def _verify_fixture_evidence(fixture_identity: Mapping[str, Any]) -> None:
    fixture = _fixture_identity_contract(fixture_identity)
    evidence = fixture["setup_evidence"]
    assert isinstance(evidence, Mapping)
    path = Path(str(evidence["path"])).resolve(strict=True)
    if sha256_path(path) != evidence["file_sha256"]:
        raise OneHotLadderError("setup evidence file differs from the immutable plan")


def _live_target_image_attestation(
    selector_control: Mapping[str, Any],
) -> dict[str, Any]:
    """Read target flash and require an exact byte match to the ELF-bound BIN."""

    started_at = _now()
    _verify_one_hot_artifacts(selector_control)
    control = _validate_one_hot_selector_control(selector_control)
    binding = control["bench_profile_binding"]
    config = control["openocd_config"]
    assert isinstance(binding, Mapping)
    assert isinstance(config, Mapping)
    bench_bin = binding["bench_bin"]
    assert isinstance(bench_bin, Mapping)
    expected_path = Path(str(bench_bin["path"])).resolve(strict=True)
    expected_sha256 = str(bench_bin["file_sha256"])
    byte_count = int(bench_bin["size_bytes"])
    flash_address = int(bench_bin["flash_base_address"])
    config_path = Path(str(config["path"])).resolve(strict=True)
    exact_match = False
    target_running_reviewed_image = False
    target_kept_halted_on_failure = False
    with tempfile.TemporaryDirectory(prefix="one-hot-target-flash-") as temporary:
        target_dump = Path(temporary) / "target-flash.bin"
        command = (
            f"init; reset halt; dump_image {target_dump} 0x{flash_address:x} {byte_count}; shutdown"
        )
        try:
            subprocess.run(
                ("openocd", "-f", str(config_path), "-c", command),
                check=True,
                capture_output=True,
                text=True,
            )
            observed = target_dump.read_bytes()
            observed_sha256 = hashlib.sha256(observed).hexdigest()
            exact_match = (
                len(observed) == byte_count
                and observed_sha256 == expected_sha256
                and observed == expected_path.read_bytes()
            )
            followup_command = (
                "init; reset run; shutdown" if exact_match else "init; halt; shutdown"
            )
            followup = subprocess.run(
                (
                    "openocd",
                    "-f",
                    str(config_path),
                    "-c",
                    followup_command,
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            target_running_reviewed_image = exact_match and followup.returncode == 0
            target_kept_halted_on_failure = not exact_match and followup.returncode == 0
        except BaseException:
            halt = subprocess.run(
                ("openocd", "-f", str(config_path), "-c", "init; halt; shutdown"),
                check=False,
                capture_output=True,
                text=True,
            )
            target_kept_halted_on_failure = halt.returncode == 0
            raise
    passed = exact_match and target_running_reviewed_image
    return {
        "schema": 1,
        "evidence_kind": "exact_target_flash_readback_against_elf_bound_bench_bin",
        "status": "passed" if passed else "failed",
        "flash_base_address": flash_address,
        "byte_count": byte_count,
        "expected_bin_sha256": expected_sha256,
        "observed_target_sha256": observed_sha256,
        "exact_byte_match": exact_match,
        "reviewed_image_started_only_after_exact_match": target_running_reviewed_image,
        "target_kept_halted_on_failure": target_kept_halted_on_failure,
        "started_at": started_at,
        "completed_at": _now(),
        "error": (
            None if passed else {"type": "TargetImageMismatch", "message": "target flash differs"}
        ),
    }


def _call_target_image_attestation(
    boundary: TargetImageBoundary,
    selector_control: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        result = boundary(selector_control)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "exact_target_flash_readback_against_elf_bound_bench_bin",
            "status": "failed",
            "completed_at": _now(),
            "error": leakage._error_document(error),
        }
    return (
        result
        if isinstance(result, dict)
        else {
            "schema": 1,
            "evidence_kind": "exact_target_flash_readback_against_elf_bound_bench_bin",
            "status": "failed",
            "completed_at": _now(),
            "error": {"type": "InvalidTargetImageAttestation", "message": "not an object"},
        }
    )


def _target_image_passed(
    value: object,
    *,
    selector_control: Mapping[str, Any],
) -> bool:
    control = _validate_one_hot_selector_control(selector_control)
    binding = control["bench_profile_binding"]
    assert isinstance(binding, Mapping)
    bench_bin = binding["bench_bin"]
    assert isinstance(bench_bin, Mapping)
    return (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "exact_target_flash_readback_against_elf_bound_bench_bin"
        and value.get("status") == "passed"
        and value.get("flash_base_address") == bench_bin.get("flash_base_address")
        and value.get("byte_count") == bench_bin.get("size_bytes")
        and value.get("expected_bin_sha256") == bench_bin.get("file_sha256")
        and value.get("observed_target_sha256") == bench_bin.get("file_sha256")
        and value.get("exact_byte_match") is True
        and value.get("reviewed_image_started_only_after_exact_match") is True
        and value.get("target_kept_halted_on_failure") is False
        and value.get("error") is None
    )


def _live_selector_boundary(
    selector_control: Mapping[str, Any],
    state_name: str,
    state_code: int,
    purpose: str,
) -> dict[str, Any]:
    """Command/read one static state, or prove it remained applied, through OpenOCD."""

    started_at = _now()
    _verify_one_hot_artifacts(selector_control)
    control = _validate_one_hot_selector_control(selector_control)
    states = _state_map(control)
    if states.get(state_name) != state_code:
        raise OneHotLadderError("requested selector state differs from immutable state map")
    manifest_document = control["bench_manifest"]
    config_document = control["openocd_config"]
    assert isinstance(manifest_document, Mapping)
    assert isinstance(config_document, Mapping)
    manifest = BenchManifest.load(Path(str(manifest_document["path"])))
    controller = OpenOcdBench(manifest, Path(str(config_document["path"])))
    all_off_code = states[ALL_OFF_STATE]
    selected_lease_ms = int(control["selected_state_lease_ms"])
    before = None
    commanded = None
    if purpose == "before_condition":
        expected_name = state_name
        expected_code = state_code
        command_lease_ms = 0 if state_name == ALL_OFF_STATE else selected_lease_ms
        before = controller.status()
        commanded = controller.request(
            expected_code,
            command_lease_ms,
            wait_until_applied=True,
        )
        readback = controller.status()
    elif purpose == "after_pluto_mute":
        expected_name = state_name
        expected_code = state_code
        command_lease_ms = 0 if state_name == ALL_OFF_STATE else selected_lease_ms
        readback = controller.status()
    elif purpose in ALL_OFF_CLEANUP_PURPOSES:
        expected_name = ALL_OFF_STATE
        expected_code = all_off_code
        command_lease_ms = 0
        before = controller.status()
        commanded = controller.request(all_off_code, 0, wait_until_applied=True)
        readback = controller.status()
    else:
        raise ValueError("unsupported selector boundary purpose")

    selected_state = expected_name != ALL_OFF_STATE
    passed = (
        readback.applied_code == expected_code
        and readback.command_code == expected_code
        and readback.command_lease_ms == command_lease_ms
        and readback.command_sequence == readback.acknowledged_sequence
        and readback.command_valid
        and not readback.guard_active
        and not readback.invalid_command
        and (
            (selected_state and readback.lease_active and readback.remaining_lease_ms > 0)
            or (
                not selected_state
                and not readback.lease_active
                and readback.remaining_lease_ms == 0
            )
        )
    )
    if commanded is not None:
        passed = passed and (
            commanded.command_sequence == commanded.acknowledged_sequence
            and commanded.command_code == expected_code
            and commanded.command_lease_ms == command_lease_ms
            and commanded.applied_code == expected_code
        )
    gpio_result = subprocess.run(
        (
            "openocd",
            "-f",
            str(Path(str(config_document["path"]))),
            "-c",
            f"init; mdw 0x{GPIOA_ODR_ADDRESS:x} 1; shutdown",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    gpio_output = gpio_result.stdout + gpio_result.stderr
    gpio_matches = re.findall(
        rf"0x{GPIOA_ODR_ADDRESS:08x}:\s+([0-9A-Fa-f]{{8}})",
        gpio_output,
        re.IGNORECASE,
    )
    if len(gpio_matches) != 1:
        raise OneHotLadderError("cannot read one exact GPIOA ODR value")
    gpio_odr = int(gpio_matches[0], 16)
    gpio_masked = gpio_odr & SELECTOR_GPIO_MASK
    gpio_passed = gpio_masked == expected_code
    passed = passed and gpio_passed
    return {
        "schema": 1,
        "evidence_kind": "static_one_hot_selector_mailbox_readback",
        "purpose": purpose,
        "status": "passed" if passed else "failed",
        "condition_state_name": state_name,
        "condition_state_code": state_code,
        "expected_applied_state_name": expected_name,
        "expected_applied_code": expected_code,
        "command_lease_ms": command_lease_ms,
        "before": before.as_dict() if before is not None else None,
        "commanded": commanded.as_dict() if commanded is not None else None,
        "readback": readback.as_dict(),
        "gpio_output_latch_readback": {
            "register": "GPIOA_ODR",
            "address": GPIOA_ODR_ADDRESS,
            "selector_mask": SELECTOR_GPIO_MASK,
            "raw_value": gpio_odr,
            "masked_selector_code": gpio_masked,
            "expected_selector_code": expected_code,
            "passed": gpio_passed,
            "physical_rf_state_proven": False,
        },
        "started_at": started_at,
        "completed_at": _now(),
        "error": None,
    }


def _call_selector(
    boundary: OneHotSelectorBoundary,
    selector_control: Mapping[str, Any],
    state_name: str,
    state_code: int,
    purpose: str,
) -> dict[str, Any]:
    try:
        result = boundary(selector_control, state_name, state_code, purpose)
    except BaseException as error:
        return {
            "schema": 1,
            "evidence_kind": "static_one_hot_selector_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "condition_state_name": state_name,
            "condition_state_code": state_code,
            "completed_at": _now(),
            "error": leakage._error_document(error),
        }
    if not isinstance(result, dict):
        return {
            "schema": 1,
            "evidence_kind": "static_one_hot_selector_mailbox_readback",
            "purpose": purpose,
            "status": "failed",
            "condition_state_name": state_name,
            "condition_state_code": state_code,
            "completed_at": _now(),
            "error": {
                "type": "InvalidSelectorAttestation",
                "message": "selector boundary did not return an object",
            },
        }
    return result


def _selector_passed(
    value: object,
    *,
    selector_control: Mapping[str, Any],
    state_name: str,
    state_code: int,
    purpose: str,
) -> bool:
    states = _state_map(selector_control)
    cleanup = purpose in ALL_OFF_CLEANUP_PURPOSES
    expected_name = ALL_OFF_STATE if cleanup else state_name
    expected_code = states[ALL_OFF_STATE] if cleanup else state_code
    if not (
        isinstance(value, Mapping)
        and value.get("schema") == 1
        and value.get("evidence_kind") == "static_one_hot_selector_mailbox_readback"
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("condition_state_name") == state_name
        and value.get("condition_state_code") == state_code
        and value.get("expected_applied_state_name") == expected_name
        and value.get("expected_applied_code") == expected_code
        and value.get("error") is None
    ):
        return False
    readback = value.get("readback")
    gpio = value.get("gpio_output_latch_readback")
    if not isinstance(readback, Mapping) or not isinstance(gpio, Mapping):
        return False
    selected = expected_name != ALL_OFF_STATE
    expected_lease_ms = int(selector_control["selected_state_lease_ms"]) if selected else 0
    if value.get("command_lease_ms") != expected_lease_ms:
        return False
    readback_passed = (
        readback.get("applied_code") == expected_code
        and readback.get("command_code") == expected_code
        and readback.get("command_lease_ms") == expected_lease_ms
        and readback.get("command_sequence") == readback.get("acknowledged_sequence")
        and readback.get("command_valid") is True
        and readback.get("guard_active") is False
        and readback.get("invalid_command") is False
        and (
            (
                selected
                and readback.get("lease_active") is True
                and isinstance(readback.get("remaining_lease_ms"), int)
                and int(readback["remaining_lease_ms"]) > 0
            )
            or (
                not selected
                and readback.get("lease_active") is False
                and readback.get("remaining_lease_ms") == 0
            )
        )
    )
    if not readback_passed:
        return False
    if not (
        gpio.get("register") == "GPIOA_ODR"
        and gpio.get("address") == GPIOA_ODR_ADDRESS
        and gpio.get("selector_mask") == SELECTOR_GPIO_MASK
        and gpio.get("masked_selector_code") == expected_code
        and gpio.get("expected_selector_code") == expected_code
        and gpio.get("passed") is True
        and gpio.get("physical_rf_state_proven") is False
    ):
        return False
    mutating = purpose != "after_pluto_mute"
    commanded = value.get("commanded")
    if not mutating:
        return commanded is None
    return (
        isinstance(commanded, Mapping)
        and commanded.get("command_sequence") == commanded.get("acknowledged_sequence")
        and commanded.get("command_code") == expected_code
        and commanded.get("command_lease_ms") == expected_lease_ms
        and commanded.get("applied_code") == expected_code
    )


def _selector_hold_command_unchanged(
    before_condition: object,
    after_pluto_mute: object,
) -> bool:
    """Prove no mailbox command replaced the selected hold during RF capture."""

    if not isinstance(before_condition, Mapping) or not isinstance(after_pluto_mute, Mapping):
        return False
    before = before_condition.get("readback")
    after = after_pluto_mute.get("readback")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    immutable_fields = (
        "command_sequence",
        "acknowledged_sequence",
        "command_code",
        "command_lease_ms",
        "applied_code",
    )
    if any(before.get(field) != after.get(field) for field in immutable_fields):
        return False
    before_remaining = before.get("remaining_lease_ms")
    after_remaining = after.get("remaining_lease_ms")
    if (
        isinstance(before_remaining, bool)
        or not isinstance(before_remaining, int)
        or isinstance(after_remaining, bool)
        or not isinstance(after_remaining, int)
    ):
        return False
    lease_ms = before.get("command_lease_ms")
    if lease_ms == 0:
        return before_remaining == 0 and after_remaining == 0
    return (
        isinstance(lease_ms, int)
        and lease_ms > 0
        and after_remaining >= 0
        and before_remaining - after_remaining >= MINIMUM_SELECTED_HOLD_LEASE_DECREMENT_MS
    )


def _move_and_seal_one_hot_artifact(
    source: Path,
    *,
    capture_root: Path,
    destination_name: str,
    artifact_id: str,
    error: BaseException,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Move one direct run artifact into the verified quarantine root and seal it."""

    _assert_path_chain_has_no_symlink(source, label="failed one-hot artifact path")
    exact_capture_root, exact_failed_root = _safe_quarantine_parent(capture_root)
    exact_source = _assert_tree_has_no_symlink(
        source,
        label="failed one-hot artifact tree",
    )
    if exact_source.parent != exact_capture_root:
        raise OneHotLadderError("failed one-hot artifact resolves outside its immutable run root")
    destination = exact_failed_root / destination_name
    if _path_lexists(destination):
        raise OneHotLadderError("failed one-hot artifact destination already exists")
    os.replace(exact_source, destination)
    _require_safe_quarantine_tree(
        destination,
        expected_parent=exact_failed_root,
    )
    return leakage._seal_failed_directory(
        destination,
        artifact_id=artifact_id,
        error=error,
        context=context,
    )


def _capture_condition(
    condition: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
    forbidden_stream_ids: set[int],
    capture_boundary: leakage.CaptureBoundary = leakage._live_capture_boundary,
    mute_boundary: leakage.MuteBoundary = leakage._strict_mute,
    selector_boundary: OneHotSelectorBoundary = _live_selector_boundary,
) -> dict[str, Any]:
    native_runtime, native_runtime_sha256 = _native_runtime_from_contract(contract)
    selector_control = contract.get("selector_control")
    if not isinstance(selector_control, Mapping):
        raise OneHotLadderError("one-hot condition lacks selector control")
    selector_control = _validate_one_hot_selector_control(selector_control)
    driven_input = validate_antenna_name(contract.get("driven_input"), "contract driven input")
    fixture = _fixture_identity_contract(contract.get("fixture_identity", {}))
    state_name = str(condition["selector_state_name"])
    if (
        condition.get("driven_input") != driven_input
        or condition.get("topology_identity") != TOPOLOGY_IDENTITY
        or condition.get("physical_cell_role") != one_hot_cell_role(driven_input, state_name)
        or condition.get("fixture_identity") != fixture
    ):
        raise OneHotLadderError("condition driven input differs from immutable contract")
    state_code = int(condition["selector_gpio_code"])
    if _state_map(selector_control).get(state_name) != state_code:
        raise OneHotLadderError("condition selector state differs from immutable state map")
    plan = leakage._tone_plan(condition, contract)
    settings = RadioSettings(
        center_frequency_hz=leakage.CENTER_FREQUENCY_HZ,
        sample_rate_hz=leakage.SAMPLE_RATE_HZ,
        bandwidth_hz=leakage.BANDWIDTH_HZ,
        gain_mode=GainMode.MANUAL,
        gain_db=leakage.RECEIVER_GAIN_DB,
        channels=(0, 1),
    )
    retained: list[SampleBlockV2] = []

    def retain(block: SampleBlockV2) -> None:
        retained.append(replace(block, samples=block.samples.copy(order="C")))

    context: dict[str, Any] = {
        "condition": dict(condition),
        "topology_identity": contract["topology_identity"],
        "driven_input": driven_input,
        "fixture_identity": fixture,
        "immutable_plan": dict(plan_evidence),
        "native_libiio_runtime_attestation": native_runtime,
        "native_libiio_runtime_attestation_sha256": native_runtime_sha256,
        "selector_calibration_claim": False,
    }
    selector_before = _call_selector(
        selector_boundary,
        selector_control,
        state_name,
        state_code,
        "before_condition",
    )
    context["selector_before_condition"] = selector_before
    capture: Any | None = None
    capture_error: BaseException | None = None
    if not _selector_passed(
        selector_before,
        selector_control=selector_control,
        state_name=state_name,
        state_code=state_code,
        purpose="before_condition",
    ):
        capture_error = OneHotLadderError("exact selector pre-condition readback failed")
    else:
        try:
            capture = capture_boundary(
                plan,
                samples_per_frame=leakage.SAMPLES_PER_FRAME,
                frame_count=leakage.FRAME_COUNT,
                kernel_buffers=leakage.KERNEL_BUFFERS,
                block_consumer=retain,
            )
        except BaseException as error:
            capture_error = error

    post_mute = leakage._call_mute(mute_boundary, plan.serial, "post_condition")
    context["post_condition_exact_serial_mute"] = post_mute
    selector_after = _call_selector(
        selector_boundary,
        selector_control,
        state_name,
        state_code,
        "after_pluto_mute",
    )
    context["selector_after_pluto_mute"] = selector_after
    selector_cleanup = _call_selector(
        selector_boundary,
        selector_control,
        state_name,
        state_code,
        "cleanup_all_off",
    )
    context["selector_cleanup_all_off"] = selector_cleanup

    post_mute_passed = leakage._mute_passed(
        post_mute,
        serial=plan.serial,
        purpose="post_condition",
    )
    selector_after_passed = _selector_passed(
        selector_after,
        selector_control=selector_control,
        state_name=state_name,
        state_code=state_code,
        purpose="after_pluto_mute",
    )
    selector_hold_unchanged = _selector_hold_command_unchanged(
        selector_before,
        selector_after,
    )
    context["selector_hold_command_unchanged"] = selector_hold_unchanged
    selector_cleanup_passed = _selector_passed(
        selector_cleanup,
        selector_control=selector_control,
        state_name=state_name,
        state_code=state_code,
        purpose="cleanup_all_off",
    )
    if (
        capture_error is not None
        or not post_mute_passed
        or not selector_after_passed
        or not selector_hold_unchanged
        or not selector_cleanup_passed
    ):
        if capture_error is not None:
            failure = capture_error
        elif not post_mute_passed:
            failure = OneHotLadderError("exact post-condition mute attestation failed")
        elif not selector_after_passed or not selector_hold_unchanged:
            failure = OneHotLadderError("selector state did not remain applied through capture")
        else:
            failure = OneHotLadderError("selector ALL_OFF cleanup readback failed")
        quarantine = leakage._persist_memory_quarantine(
            capture_root,
            blocks=retained,
            error=failure,
            context=context,
        )
        retained.clear()
        raise leakage.ConditionCaptureFailure(
            str(failure),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from capture_error
    assert capture is not None

    writer: CaptureWriter | None = None
    artifact: Any | None = None
    try:
        stream_id, rf_readback, tone_readback_hz = leakage._validate_capture_result(
            capture,
            retained,
            plan=plan,
            settings=settings,
            forbidden_stream_ids=forbidden_stream_ids,
        )
        headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
        for block in retained:
            headroom_monitor.observe(block.samples)
        headroom = headroom_monitor.result()
        context["adc_headroom_admission"] = asdict(headroom)
        if not headroom.passed:
            raise OneHotLadderError(
                "ADC headroom admission failed; stronger ladder conditions are forbidden"
            )

        rx1 = np.concatenate([block.samples[0] for block in retained])
        rx2 = np.concatenate([block.samples[1] for block in retained])
        pilot = estimate_coherent_pilot_offset(
            rx1,
            sample_rate_hz=leakage.SAMPLE_RATE_HZ,
            nominal_tone_offset_hz=tone_readback_hz,
        )
        pilot_phase_rms_deg = math.degrees(pilot.phase_residual_rms_rad)
        pilot_rejection_reasons: list[str] = []
        if pilot.confidence < leakage.MINIMUM_PILOT_CONFIDENCE:
            pilot_rejection_reasons.append("rx1_pilot_confidence_below_minimum")
        if pilot.phase_step_coherence < leakage.MINIMUM_PILOT_PHASE_STEP_COHERENCE:
            pilot_rejection_reasons.append("rx1_pilot_phase_step_coherence_below_minimum")
        if pilot_phase_rms_deg > leakage.MAXIMUM_PILOT_PHASE_RMS_DEG:
            pilot_rejection_reasons.append("rx1_pilot_phase_rms_above_maximum")
        analysis = analyze_coherent_leakage(
            rx1,
            rx2,
            sample_rate_hz=leakage.SAMPLE_RATE_HZ,
            tone_offset_hz=pilot.estimated_offset_hz,
        )
        measurement_rejection_reasons = [
            *pilot_rejection_reasons,
            *analysis.quality_rejection_reasons,
        ]
        measurement_quality_passed = not measurement_rejection_reasons
        del rx1, rx2

        writer = CaptureWriter(
            capture_root,
            radio=capture.identity,
            settings=settings,
            label=(
                "EXPLORATORY marker-independent 5.8 GHz static one-hot path "
                f"drive={driven_input} select={state_name} "
                f"TX1={plan.tx_hardware_gain_db:g}dB"
            ),
        )
        for block in retained:
            writer.append(block, settings, revision=1)
        artifact = writer.finalize()
        if not verify_artifact(artifact):
            raise OneHotLadderError("finalized SigMF data failed SHA-256 verification")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=leakage.TOTAL_SAMPLES,
            expected_samples_per_block=leakage.SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(leakage.SAMPLE_RATE_HZ),
        )
        if continuity["stream_id"] != stream_id or continuity["metadata_abi"] != 2:
            raise OneHotLadderError("persisted continuity identity differs from live capture")

        artifact_evidence = leakage._artifact_evidence(artifact)
        analysis_document = leakage._json_safe(asdict(analysis))
        pilot_document = leakage._json_safe(asdict(pilot))
        assert isinstance(analysis_document, dict)
        assert isinstance(pilot_document, dict)
        pilot_document.update(
            {
                "phase_residual_rms_deg": pilot_phase_rms_deg,
                "minimum_confidence": leakage.MINIMUM_PILOT_CONFIDENCE,
                "minimum_phase_step_coherence": (leakage.MINIMUM_PILOT_PHASE_STEP_COHERENCE),
                "maximum_phase_rms_deg": leakage.MAXIMUM_PILOT_PHASE_RMS_DEG,
                "quality_passed": not pilot_rejection_reasons,
                "quality_rejection_reasons": pilot_rejection_reasons,
            }
        )
        record = {
            "schema": 1,
            "record_kind": "5g8_marker_independent_static_one_hot_path_condition",
            "created_at": _now(),
            "accepted_raw_artifact": False,
            "accepted_raw_artifact_pending_manifest_commit": True,
            "standalone_condition_record_is_not_acceptance": True,
            "acceptance_authority": (
                "plan-bound complete manifest attempt plus artifact revalidation"
            ),
            "accepted_for_selector_path_characterization": False,
            "selector_path_characterization_pending_manifest_validation": (
                measurement_quality_passed
            ),
            "accepted_for_selector_calibration": False,
            "causal_attribution_claim": False,
            "operational_switching_claim": False,
            "immutable_plan": dict(plan_evidence),
            "native_libiio_runtime_attestation": native_runtime,
            "native_libiio_runtime_attestation_sha256": native_runtime_sha256,
            "condition": dict(condition),
            "topology": {
                "identity": contract["topology_identity"],
                "driven_input": driven_input,
                "physical_cell_role": one_hot_cell_role(driven_input, state_name),
                "fixture_identity": fixture,
                "contract": contract["stage_contract"],
            },
            "artifact": artifact.model_dump(mode="json"),
            "artifact_evidence": artifact_evidence,
            "capture": {
                "serial": plan.serial,
                "uri": plan.uri,
                "center_frequency_hz": leakage.CENTER_FREQUENCY_HZ,
                "sample_rate_hz": leakage.SAMPLE_RATE_HZ,
                "bandwidth_hz": leakage.BANDWIDTH_HZ,
                "receiver_gain_db": leakage.RECEIVER_GAIN_DB,
                "tx_channel": 0,
                "tx_port": "TX1",
                "tx2_required_exact_muted": True,
                "tx_hardware_gain_db_requested": plan.tx_hardware_gain_db,
                "dds_scale_requested": leakage.DDS_SCALE,
                "tone_offset_hz_requested": leakage.TONE_OFFSET_HZ,
                "tone_offset_hz_readback": tone_readback_hz,
                "tone_offset_hz_measured": pilot.estimated_offset_hz,
                "pilot_frequency_refinement": pilot_document,
                "samples_per_frame": leakage.SAMPLES_PER_FRAME,
                "frame_count": leakage.FRAME_COUNT,
                "sample_count": leakage.TOTAL_SAMPLES,
                "kernel_buffers": leakage.KERNEL_BUFFERS,
                "metadata_abi": 2,
                "stream_id": stream_id,
                "rf_readback_evidence": rf_readback,
                "adc_headroom_admission": asdict(headroom),
            },
            "selector_state_attestation": {
                "before_condition": selector_before,
                "after_pluto_mute": selector_after,
                "hold_command_unchanged": selector_hold_unchanged,
                "cleanup_all_off": selector_cleanup,
            },
            "continuity_audit": continuity,
            "marker_independent_analysis": analysis_document,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "safety": {
                "post_condition_exact_serial_mute": post_mute,
                "selected_state_lease_survived_capture": True,
                "selected_state_mailbox_command_unchanged": True,
                "all_off_mailbox_and_gpio_latch_cleanup_passed": True,
                "digital_readback_does_not_prove_physical_rf_state": True,
                "fresh_stream_validated": True,
                "automatic_retry_count": 0,
            },
            "interpretation": (
                "One statically held selector state; no RF marker or timing-derived state label."
            ),
        }
        record_path = Path(artifact.path) / CONDITION_RECORD_NAME
        write_json_atomic(record_path, record)
        return {
            "condition_id": condition["condition_id"],
            "topology_identity": TOPOLOGY_IDENTITY,
            "driven_input": driven_input,
            "fixture_identity": fixture,
            "immutable_plan": dict(plan_evidence),
            "native_libiio_runtime_attestation": native_runtime,
            "native_libiio_runtime_attestation_sha256": native_runtime_sha256,
            "physical_cell_role": one_hot_cell_role(driven_input, state_name),
            "selector_state_name": state_name,
            "selector_gpio_code": state_code,
            "tx_hardware_gain_db": plan.tx_hardware_gain_db,
            "repeat_index": condition["repeat_index"],
            "repeat_count_at_gain": condition["repeat_count_at_gain"],
            "independent_fresh_stream_repeat": True,
            "attribution_gain_condition": condition["attribution_gain_condition"],
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_data_path": artifact_evidence["data_path"],
            "artifact_data_sha256": artifact_evidence["data_sha256"],
            "artifact_metadata_path": artifact_evidence["metadata_path"],
            "artifact_metadata_sha256": artifact_evidence["metadata_sha256"],
            "condition_record_path": str(record_path),
            "condition_record_sha256": sha256_path(record_path),
            "stream_id": stream_id,
            "metadata_abi": 2,
            "headroom_passed": headroom.passed,
            "measurement_quality_passed": measurement_quality_passed,
            "measurement_quality_rejection_reasons": measurement_rejection_reasons,
            "tone_offset_hz_requested": leakage.TONE_OFFSET_HZ,
            "tone_offset_hz_readback": tone_readback_hz,
            "tone_offset_hz_measured": pilot.estimated_offset_hz,
            "pilot_confidence": pilot.confidence,
            "rx2_tone_detected": analysis.rx2.tone_detected,
            "rx2_over_rx1": analysis_document["rx2_over_rx1"],
            "post_condition_exact_serial_mute": post_mute,
            "selector_before_condition": selector_before,
            "selector_after_pluto_mute": selector_after,
            "selector_hold_command_unchanged": selector_hold_unchanged,
            "selector_cleanup_all_off": selector_cleanup,
            "selector_calibration_claim": False,
            "causal_attribution_claim": False,
            "operational_switching_claim": False,
        }
    except BaseException as error:
        context["post_capture_error"] = leakage._error_document(error)
        if artifact is not None:
            quarantine = _move_and_seal_one_hot_artifact(
                Path(artifact.path),
                capture_root=capture_root,
                destination_name=f"{artifact.artifact_id}.failed",
                artifact_id=artifact.artifact_id,
                error=error,
                context=context,
            )
        elif writer is not None:
            finalized_candidate = capture_root / writer.artifact_id
            if _path_lexists(finalized_candidate):
                quarantine = _move_and_seal_one_hot_artifact(
                    finalized_candidate,
                    capture_root=capture_root,
                    destination_name=f"{writer.artifact_id}.failed",
                    artifact_id=writer.artifact_id,
                    error=error,
                    context=context,
                )
            else:
                exact_capture_root, exact_failed_root = _safe_quarantine_parent(capture_root)
                raw_partial_root = capture_root / ".partial"
                _assert_path_chain_has_no_symlink(
                    raw_partial_root,
                    label="one-hot partial-artifact root",
                )
                exact_partial_root = raw_partial_root.resolve(strict=True)
                if (
                    not exact_partial_root.is_dir()
                    or exact_partial_root.parent != exact_capture_root
                ):
                    raise OneHotLadderError(
                        "one-hot partial-artifact root resolves outside the capture root"
                    ) from error
                partial_candidate = raw_partial_root / writer.artifact_id
                if _path_lexists(partial_candidate):
                    exact_partial_candidate = _assert_tree_has_no_symlink(
                        partial_candidate,
                        label="one-hot partial artifact tree",
                    )
                    if exact_partial_candidate.parent != exact_partial_root:
                        raise OneHotLadderError(
                            "one-hot partial artifact resolves outside its partial root"
                        ) from error
                destination = exact_failed_root / writer.artifact_id
                if _path_lexists(destination):
                    raise OneHotLadderError(
                        "failed one-hot artifact destination already exists"
                    ) from error
                destination = writer.fail(error)
                _assert_path_chain_has_no_symlink(
                    destination,
                    label="writer failed-artifact path",
                )
                exact_destination = _assert_tree_has_no_symlink(
                    destination,
                    label="writer failed-artifact tree",
                )
                if exact_destination.parent != exact_failed_root:
                    raise OneHotLadderError(
                        "writer failed artifact resolves outside the quarantine root"
                    ) from error
                quarantine = leakage._seal_failed_directory(
                    exact_destination,
                    artifact_id=writer.artifact_id,
                    error=error,
                    context=context,
                )
        else:
            quarantine = leakage._persist_memory_quarantine(
                capture_root,
                blocks=retained,
                error=error,
                context=context,
            )
        raise leakage.ConditionCaptureFailure(
            str(error),
            quarantine=quarantine,
            post_mute=post_mute,
        ) from error
    finally:
        retained.clear()


def _new_manifest(plan_path: Path, envelope: Mapping[str, Any]) -> dict[str, Any]:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    return {
        "schema": 1,
        "run_kind": "5g8_marker_independent_one_hot_selector_path_ladder",
        "run_id": contract["run_id"],
        "topology_identity": contract["topology_identity"],
        "driven_input": contract["driven_input"],
        "fixture_identity": contract["fixture_identity"],
        "physical_confirmation_token": contract["stage_contract"]["confirmation_token"],
        "state_revision": 0,
        "execution_started": False,
        "status": "prepared",
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "immutable_plan": leakage._plan_file_evidence(plan_path, envelope),
        "confirmations": [],
        "native_runtime_preflight_attempts": [],
        "native_runtime_preflight": None,
        "identity_preflight_attempts": [],
        "identity_preflight": None,
        "preflight_mute_attempts": [],
        "preflight_mute": None,
        "preflight_selector_cleanup_attempts": [],
        "preflight_selector_cleanup": None,
        "target_image_preflight_attempts": [],
        "target_image_preflight": None,
        "attempts": [],
        "recovery_mute_attempts": [],
        "recovery_selector_cleanup_attempts": [],
        "orphan_quarantine_attempts": [],
        "final_mute_attempts": [],
        "final_selector_cleanup_attempts": [],
        "final_mute": None,
        "final_selector_cleanup": None,
        "one_hot_run_summary": None,
        "error": None,
        "summary": {},
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
        "operational_switching_claim": False,
    }


def _manifest_summary(manifest: Mapping[str, Any], condition_count: int) -> dict[str, Any]:
    attempts = [item for item in manifest.get("attempts", []) if isinstance(item, Mapping)]
    complete = [item for item in attempts if item.get("status") == "complete"]
    return {
        "planned_conditions": condition_count,
        "attempted_conditions": len(attempts),
        "completed_conditions": len(complete),
        "remaining_conditions": condition_count - len(complete),
        "measurement_quality_passed": sum(
            item.get("outcome") == "measurement_quality_passed" for item in complete
        ),
        "measurement_quality_rejected": sum(
            item.get("outcome") == "measurement_quality_rejected" for item in complete
        ),
        "failed_conditions": sum(item.get("status") == "failed" for item in attempts),
        "quarantine_count": sum(bool(item.get("quarantine")) for item in attempts),
        "one_hot_run_quality_passed": (
            manifest.get("one_hot_run_summary", {}).get("quality_passed")
            if isinstance(manifest.get("one_hot_run_summary"), Mapping)
            else None
        ),
        "selector_calibration_claim": False,
        "causal_attribution_claim": False,
        "operational_switching_claim": False,
    }


_RUN_STATE_EVENT_FIELDS = {
    "schema",
    "event_kind",
    "run_id",
    "immutable_plan",
    "state_revision",
    "previous_event_sha256",
    "transition_kind",
    "manifest_file_sha256",
    "manifest_size_bytes",
    "manifest_status",
    "execution_started",
    "run_id_burned",
    "attempt_count",
    "created_at",
}
_RUN_TOMBSTONE_FIELDS = {
    "schema",
    "tombstone_kind",
    "run_id",
    "immutable_plan",
    "created_at",
}
_MANIFEST_IDENTITY_FIELDS = (
    "schema",
    "run_kind",
    "run_id",
    "topology_identity",
    "driven_input",
    "fixture_identity",
    "physical_confirmation_token",
    "immutable_plan",
    "selector_calibration_claim",
    "causal_attribution_claim",
    "operational_switching_claim",
)
_MANIFEST_STATUSES = {"prepared", "running", "conditions_complete", "complete", "failed"}


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _assert_path_chain_has_no_symlink(path: Path, *, label: str) -> None:
    """Reject a symlink in the raw path before resolving its provenance away."""

    try:
        leakage._assert_path_chain_has_no_symlink(path, label=label)
    except leakage.LeakageLadderError as error:
        raise OneHotLadderError(str(error)) from error


def _assert_tree_has_no_symlink(root: Path, *, label: str) -> Path:
    """Return an exact directory only after its raw chain and tree are symlink-free."""

    try:
        return leakage._assert_tree_has_no_symlink(root, label=label)
    except leakage.LeakageLadderError as error:
        raise OneHotLadderError(str(error)) from error


def _safe_quarantine_parent(capture_root: Path) -> tuple[Path, Path]:
    """Create or validate the capture and quarantine roots without following symlinks."""

    try:
        return leakage._safe_quarantine_parent(capture_root)
    except leakage.LeakageLadderError as error:
        raise OneHotLadderError(str(error)) from error


def _require_real_directory(
    path: Path,
    *,
    label: str,
    expected_parent: Path | None = None,
    create: bool = False,
) -> Path:
    if path.is_symlink():
        raise OneHotLadderError(f"{label} must not be a symlink")
    if not path.exists():
        if not create:
            raise OneHotLadderError(f"{label} does not exist")
        path.mkdir(mode=0o700)
        leakage._fsync_directory(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise OneHotLadderError(f"{label} must be a real directory")
    exact = path.resolve(strict=True)
    if expected_parent is not None and exact.parent != expected_parent:
        raise OneHotLadderError(f"{label} resolves outside its immutable parent")
    return exact


def _run_state_path(manifest_path: Path) -> Path:
    run_root = manifest_path.parent
    return run_root.parent / RUN_STATE_DIRECTORY / run_root.name


def _run_state_is_present(manifest_path: Path) -> bool:
    return _path_lexists(_run_state_path(manifest_path))


def _state_transition_kind(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> str:
    status = current["manifest_status"]
    started = current["execution_started"]
    attempts = current["attempt_count"]
    if previous is None:
        if attempts != 0:
            raise OneHotLadderError("one-hot run-state genesis cannot contain attempts")
        if status == "prepared" and started is False:
            return "genesis_prepared"
        if status == "prepared" and started is True:
            return "genesis_execution_started"
        if status == "failed" and started is True:
            return "genesis_execution_failed"
        raise OneHotLadderError("one-hot run-state genesis is invalid")

    previous_status = previous["manifest_status"]
    previous_started = previous["execution_started"]
    allowed = {
        "prepared": {"prepared", "running", "failed"},
        "running": {"running", "conditions_complete", "complete", "failed"},
        "conditions_complete": {"conditions_complete", "complete", "failed"},
        "failed": {"failed"},
        "complete": set(),
    }
    if status not in allowed[previous_status]:
        raise OneHotLadderError(
            f"forbidden one-hot run-state transition: {previous_status} -> {status}"
        )
    if previous_started is True and started is not True:
        raise OneHotLadderError("one-hot execution-started state cannot regress")
    if status != "prepared" and started is not True:
        raise OneHotLadderError("non-prepared one-hot state must be execution-started")
    if status == "failed" and previous_status != "failed":
        return "run_failed"
    if previous_started is False and started is True:
        return "execution_started"
    if status != previous_status:
        return f"status_{previous_status}_to_{status}"
    if attempts > previous["attempt_count"]:
        return "condition_attempt_progress"
    return "evidence_update"


def _manifest_failure_latched(manifest: Mapping[str, Any]) -> bool:
    attempts = manifest.get("attempts")
    return manifest.get("status") == "failed" or (
        isinstance(attempts, list)
        and any(
            isinstance(attempt, Mapping) and attempt.get("status") == "failed"
            for attempt in attempts
        )
    )


def _load_run_state_events(
    manifest_path: Path,
    *,
    required: bool,
) -> list[tuple[dict[str, Any], str]]:
    run_root = manifest_path.parent
    if run_root.is_symlink() or not run_root.is_dir():
        raise OneHotLadderError("one-hot run root must be a real directory")
    exact_run_root = run_root.resolve(strict=True)
    exact_ladder_root = exact_run_root.parent
    state_path = _run_state_path(manifest_path)
    if not _path_lexists(state_path):
        if required:
            raise OneHotLadderError("persistent one-hot run state is missing")
        return []
    state_root = state_path.parent
    exact_state_root = _require_real_directory(
        state_root,
        label="persistent one-hot state root",
        expected_parent=exact_ladder_root,
    )
    exact_state = _require_real_directory(
        state_path,
        label="persistent one-hot run-state directory",
        expected_parent=exact_state_root,
    )
    all_entries = list(state_path.iterdir())
    marker_names = {RUN_STARTED_TOMBSTONE, RUN_FAILED_TOMBSTONE}
    unknown_entries = [
        entry
        for entry in all_entries
        if entry.name not in marker_names and RUN_STATE_EVENT_PATTERN.fullmatch(entry.name) is None
    ]
    if unknown_entries:
        raise OneHotLadderError("persistent one-hot run state contains an unsafe entry")
    entries = sorted(
        (
            entry
            for entry in all_entries
            if RUN_STATE_EVENT_PATTERN.fullmatch(entry.name) is not None
        ),
        key=lambda item: item.name,
    )
    if not entries:
        raise OneHotLadderError("persistent one-hot run state is empty")

    events: list[tuple[dict[str, Any], str]] = []
    previous_sha256: str | None = None
    previous: Mapping[str, Any] | None = None
    for expected_revision, event_path in enumerate(entries, start=1):
        match = RUN_STATE_EVENT_PATTERN.fullmatch(event_path.name)
        if (
            match is None
            or event_path.is_symlink()
            or not event_path.is_file()
            or event_path.resolve(strict=True).parent != exact_state
        ):
            raise OneHotLadderError("persistent one-hot run state contains an unsafe entry")
        revision = int(match.group("revision"))
        if revision != expected_revision:
            raise OneHotLadderError("persistent one-hot run-state revisions are not contiguous")
        document = leakage._read_json(event_path, "persistent one-hot run-state event")
        if (
            set(document) != _RUN_STATE_EVENT_FIELDS
            or isinstance(document.get("schema"), bool)
            or document.get("schema") != 1
            or document.get("event_kind") != RUN_STATE_EVENT_KIND
            or isinstance(document.get("state_revision"), bool)
            or not isinstance(document.get("state_revision"), int)
            or document.get("state_revision") != revision
            or document.get("previous_event_sha256") != previous_sha256
            or document.get("manifest_status") not in _MANIFEST_STATUSES
            or not isinstance(document.get("run_id"), str)
            or not isinstance(document.get("immutable_plan"), Mapping)
            or not isinstance(document.get("execution_started"), bool)
            or not isinstance(document.get("run_id_burned"), bool)
            or isinstance(document.get("manifest_size_bytes"), bool)
            or not isinstance(document.get("manifest_size_bytes"), int)
            or document["manifest_size_bytes"] <= 0
            or isinstance(document.get("attempt_count"), bool)
            or not isinstance(document.get("attempt_count"), int)
            or document["attempt_count"] < 0
            or not isinstance(document.get("created_at"), str)
            or not document["created_at"]
        ):
            raise OneHotLadderError("persistent one-hot run-state event is malformed")
        leakage._validate_sha256(
            document.get("manifest_file_sha256"),
            "persistent one-hot manifest hash",
        )
        expected_burned = bool(document["execution_started"]) or document["manifest_status"] in {
            "running",
            "conditions_complete",
            "complete",
            "failed",
        }
        if document["run_id_burned"] is not expected_burned:
            raise OneHotLadderError("persistent one-hot run-state burn flag is inconsistent")
        transition_kind = _state_transition_kind(previous, document)
        if document["transition_kind"] != transition_kind:
            raise OneHotLadderError("persistent one-hot run-state transition is inconsistent")
        if previous is not None and (
            document["run_id"] != previous["run_id"]
            or document["immutable_plan"] != previous["immutable_plan"]
            or document["attempt_count"] < previous["attempt_count"]
            or (previous["execution_started"] is True and document["execution_started"] is not True)
            or (previous["run_id_burned"] is True and document["run_id_burned"] is not True)
            or (previous["manifest_status"] == "failed" and document["manifest_status"] != "failed")
            or (
                previous["manifest_status"] == "complete"
                and document["manifest_status"] != "complete"
            )
        ):
            raise OneHotLadderError("persistent one-hot run state regressed")
        event_sha256 = sha256_path(event_path)
        events.append((document, event_sha256))
        previous = document
        previous_sha256 = event_sha256
    return events


def _ensure_run_state_directory(manifest_path: Path) -> Path:
    run_root = _require_real_directory(
        manifest_path.parent,
        label="one-hot run root",
    )
    ladder_root = run_root.parent
    state_path = _run_state_path(manifest_path)
    state_root = state_path.parent
    exact_state_root = _require_real_directory(
        state_root,
        label="persistent one-hot state root",
        expected_parent=ladder_root,
        create=not _path_lexists(state_root),
    )
    return _require_real_directory(
        state_path,
        label="persistent one-hot run-state directory",
        expected_parent=exact_state_root,
        create=not _path_lexists(state_path),
    )


def _load_run_tombstones(manifest_path: Path) -> dict[str, dict[str, Any]]:
    state_path = _run_state_path(manifest_path)
    exact_state = _ensure_run_state_directory(manifest_path)
    tombstones: dict[str, dict[str, Any]] = {}
    for filename, kind in (
        (RUN_STARTED_TOMBSTONE, "execution_started"),
        (RUN_FAILED_TOMBSTONE, "failed"),
    ):
        tombstone_path = state_path / filename
        if not _path_lexists(tombstone_path):
            continue
        if (
            tombstone_path.is_symlink()
            or not tombstone_path.is_file()
            or tombstone_path.resolve(strict=True).parent != exact_state
        ):
            raise OneHotLadderError("persistent one-hot run tombstone is unsafe")
        document = leakage._read_json(tombstone_path, "persistent one-hot run tombstone")
        if (
            set(document) != _RUN_TOMBSTONE_FIELDS
            or isinstance(document.get("schema"), bool)
            or document.get("schema") != 1
            or document.get("tombstone_kind") != kind
            or not isinstance(document.get("run_id"), str)
            or not isinstance(document.get("immutable_plan"), Mapping)
            or not isinstance(document.get("created_at"), str)
            or not document["created_at"]
        ):
            raise OneHotLadderError("persistent one-hot run tombstone is malformed")
        tombstones[kind] = document
    if "failed" in tombstones and "execution_started" not in tombstones:
        raise OneHotLadderError("failed one-hot tombstone lacks execution-started tombstone")
    return tombstones


def _write_run_tombstone(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    kind: str,
) -> None:
    if kind not in {"execution_started", "failed"}:
        raise OneHotLadderError("one-hot run tombstone kind is invalid")
    exact_state = _ensure_run_state_directory(manifest_path)
    filename = RUN_STARTED_TOMBSTONE if kind == "execution_started" else RUN_FAILED_TOMBSTONE
    tombstone_path = _run_state_path(manifest_path) / filename
    document = {
        "schema": 1,
        "tombstone_kind": kind,
        "run_id": manifest["run_id"],
        "immutable_plan": manifest["immutable_plan"],
        "created_at": _now(),
    }
    if _path_lexists(tombstone_path):
        if (
            tombstone_path.is_symlink()
            or not tombstone_path.is_file()
            or tombstone_path.resolve(strict=True).parent != exact_state
        ):
            raise OneHotLadderError("persistent one-hot run tombstone is unsafe")
        observed = leakage._read_json(tombstone_path, "existing one-hot run tombstone")
        if any(
            observed.get(field) != document.get(field)
            for field in ("schema", "tombstone_kind", "run_id", "immutable_plan")
        ):
            raise OneHotLadderError("persistent one-hot run tombstone identity differs")
        return
    if tombstone_path.parent.resolve(strict=True) != exact_state:
        raise OneHotLadderError("persistent one-hot run tombstone escapes its state directory")
    leakage._write_immutable_json(tombstone_path, document)


def _validate_manifest_run_state(
    path: Path,
    manifest: Mapping[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    if path.is_symlink() or not path.is_file():
        raise OneHotLadderError("one-hot manifest must be a real regular file")
    events = _load_run_state_events(path, required=True)
    tombstones = _load_run_tombstones(path)
    latest = events[-1][0]
    revision = manifest.get("state_revision")
    attempts = manifest.get("attempts")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != len(events)
        or latest["state_revision"] != revision
        or latest["run_id"] != manifest.get("run_id")
        or latest["immutable_plan"] != manifest.get("immutable_plan")
        or latest["manifest_file_sha256"] != sha256_path(path)
        or latest["manifest_size_bytes"] != path.stat().st_size
        or latest["manifest_status"] != manifest.get("status")
        or latest["execution_started"] is not manifest.get("execution_started")
        or ("execution_started" in tombstones) is not (manifest.get("execution_started") is True)
        or ("failed" in tombstones) is not _manifest_failure_latched(manifest)
        or not isinstance(attempts, list)
        or latest["attempt_count"] != len(attempts)
    ):
        raise OneHotLadderError(
            "one-hot manifest differs from persistent run state; deletion or rollback is forbidden"
        )
    if any(
        tombstone["run_id"] != manifest.get("run_id")
        or tombstone["immutable_plan"] != manifest.get("immutable_plan")
        for tombstone in tombstones.values()
    ):
        raise OneHotLadderError("persistent one-hot run tombstone identity differs")
    return events


def _append_run_state_event(
    path: Path,
    manifest: Mapping[str, Any],
    *,
    prior_events: list[tuple[dict[str, Any], str]],
) -> None:
    revision = manifest.get("state_revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise OneHotLadderError("one-hot manifest state revision is malformed")
    if revision != len(prior_events) + 1:
        raise OneHotLadderError("one-hot manifest state revision is not monotonic")
    state_path = _run_state_path(path)
    exact_state = _ensure_run_state_directory(path)
    previous = prior_events[-1][0] if prior_events else None
    if previous is not None and (
        previous["run_id"] != manifest.get("run_id")
        or previous["immutable_plan"] != manifest.get("immutable_plan")
    ):
        raise OneHotLadderError("one-hot run-state identity changed")
    status = manifest.get("status")
    execution_started = manifest.get("execution_started")
    attempts = manifest.get("attempts")
    if (
        status not in _MANIFEST_STATUSES
        or not isinstance(execution_started, bool)
        or not isinstance(attempts, list)
    ):
        raise OneHotLadderError("one-hot manifest cannot be recorded in persistent run state")
    event = {
        "schema": 1,
        "event_kind": RUN_STATE_EVENT_KIND,
        "run_id": manifest["run_id"],
        "immutable_plan": manifest["immutable_plan"],
        "state_revision": revision,
        "previous_event_sha256": prior_events[-1][1] if prior_events else None,
        "transition_kind": "",
        "manifest_file_sha256": sha256_path(path),
        "manifest_size_bytes": path.stat().st_size,
        "manifest_status": status,
        "execution_started": execution_started,
        "run_id_burned": bool(execution_started)
        or status in {"running", "conditions_complete", "complete", "failed"},
        "attempt_count": len(attempts),
        "created_at": _now(),
    }
    event["transition_kind"] = _state_transition_kind(previous, event)
    event_path = state_path / f"{revision:020d}.json"
    if _path_lexists(event_path) or event_path.parent.resolve(strict=True) != exact_state:
        raise OneHotLadderError("persistent one-hot run-state event already exists or escapes")
    leakage._write_immutable_json(event_path, event)


def _persist_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    condition_count: int,
) -> None:
    manifest_exists = _path_lexists(path)
    state_exists = _run_state_is_present(path)
    prior_events: list[tuple[dict[str, Any], str]]
    if manifest_exists:
        if path.is_symlink() or not path.is_file() or not state_exists:
            raise OneHotLadderError("existing one-hot manifest lacks its persistent run state")
        disk_manifest = leakage._read_json(path, "existing one-hot ladder manifest")
        prior_events = _validate_manifest_run_state(path, disk_manifest)
        if manifest.get("state_revision") != disk_manifest.get("state_revision"):
            raise OneHotLadderError("stale one-hot manifest object cannot overwrite run state")
        if any(
            manifest.get(field) != disk_manifest.get(field) for field in _MANIFEST_IDENTITY_FIELDS
        ):
            raise OneHotLadderError("one-hot manifest immutable identity changed")
    else:
        if state_exists:
            raise OneHotLadderError(
                "persistent one-hot run state exists without its manifest; run ID is burned"
            )
        if manifest.get("state_revision") != 0:
            raise OneHotLadderError("new one-hot manifest must begin at state revision zero")
        prior_events = []

    manifest["updated_at"] = _now()
    manifest["summary"] = _manifest_summary(manifest, condition_count)
    manifest["state_revision"] = len(prior_events) + 1
    previous_event = prior_events[-1][0] if prior_events else None
    _state_transition_kind(
        previous_event,
        {
            "manifest_status": manifest.get("status"),
            "execution_started": manifest.get("execution_started"),
            "attempt_count": len(manifest.get("attempts", [])),
        },
    )
    previous_started = bool(
        previous_event is not None and previous_event["execution_started"] is True
    )
    if manifest.get("execution_started") is True and not previous_started:
        _write_run_tombstone(path, manifest, kind="execution_started")
    if _manifest_failure_latched(manifest):
        _write_run_tombstone(path, manifest, kind="execution_started")
        _write_run_tombstone(path, manifest, kind="failed")
    write_json_atomic(path, manifest)
    _append_run_state_event(path, manifest, prior_events=prior_events)
    _validate_manifest_run_state(path, manifest)


def _load_manifest(
    path: Path,
    *,
    plan_path: Path,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise OneHotLadderError("one-hot manifest must be a real regular file")
    document = leakage._read_json(path, "one-hot ladder manifest")
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    if (
        document.get("schema") != 1
        or document.get("run_kind") != "5g8_marker_independent_one_hot_selector_path_ladder"
        or document.get("run_id") != contract.get("run_id")
        or document.get("topology_identity") != contract.get("topology_identity")
        or document.get("driven_input") != contract.get("driven_input")
        or document.get("fixture_identity") != contract.get("fixture_identity")
        or document.get("physical_confirmation_token")
        != contract.get("stage_contract", {}).get("confirmation_token")
        or document.get("immutable_plan") != leakage._plan_file_evidence(plan_path, envelope)
        or document.get("selector_calibration_claim") is not False
        or document.get("causal_attribution_claim") is not False
        or document.get("operational_switching_claim") is not False
        or not isinstance(document.get("state_revision"), int)
        or isinstance(document.get("state_revision"), bool)
        or document["state_revision"] < 1
        or not isinstance(document.get("execution_started"), bool)
    ):
        raise OneHotLadderError("manifest identity differs from the immutable one-hot plan")
    list_fields = (
        "confirmations",
        "native_runtime_preflight_attempts",
        "identity_preflight_attempts",
        "preflight_mute_attempts",
        "preflight_selector_cleanup_attempts",
        "target_image_preflight_attempts",
        "attempts",
        "recovery_mute_attempts",
        "recovery_selector_cleanup_attempts",
        "orphan_quarantine_attempts",
        "final_mute_attempts",
        "final_selector_cleanup_attempts",
    )
    if any(not isinstance(document.get(field), list) for field in list_fields):
        raise OneHotLadderError("manifest progress arrays are malformed")
    if document.get("preflight_selector_cleanup") is not None and not isinstance(
        document.get("preflight_selector_cleanup"), Mapping
    ):
        raise OneHotLadderError("manifest selector preflight evidence is malformed")
    if document.get("preflight_mute") is not None and not isinstance(
        document.get("preflight_mute"), Mapping
    ):
        raise OneHotLadderError("manifest mute preflight evidence is malformed")
    _validate_manifest_run_state(path, document)
    return document


def _verify_completed_result_files(
    result: Mapping[str, Any],
    *,
    condition: Mapping[str, Any],
    configuration: Mapping[str, Any],
    native_runtime_attestation: Mapping[str, Any],
    native_runtime_sha256: str,
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
) -> None:
    """Fail closed if a resumable result lost or changed any accepted artifact."""

    if (
        result.get("immutable_plan") != plan_evidence
        or result.get("native_libiio_runtime_attestation") != dict(native_runtime_attestation)
        or result.get("native_libiio_runtime_attestation_sha256") != native_runtime_sha256
    ):
        raise OneHotLadderError("completed result differs from the immutable plan evidence")
    try:
        raw_paths = {
            "capture root": capture_root,
            "completed artifact root": Path(str(result["artifact_path"])),
            "completed artifact data": Path(str(result["artifact_data_path"])),
            "completed artifact metadata": Path(str(result["artifact_metadata_path"])),
            "completed condition record": Path(str(result["condition_record_path"])),
        }
        for label, raw_path in raw_paths.items():
            _assert_path_chain_has_no_symlink(raw_path, label=label)
        exact_capture_root = capture_root.resolve(strict=True)
        artifact_root = raw_paths["completed artifact root"].resolve(strict=True)
        data_path = raw_paths["completed artifact data"].resolve(strict=True)
        metadata_path = raw_paths["completed artifact metadata"].resolve(strict=True)
        record_path = raw_paths["completed condition record"].resolve(strict=True)
    except (KeyError, OSError) as error:
        raise OneHotLadderError("completed result artifact path is missing") from error
    if (
        not artifact_root.is_dir()
        or artifact_root.parent != exact_capture_root
        or data_path.parent != artifact_root
        or metadata_path.parent != artifact_root
        or record_path.parent != artifact_root
        or record_path.name != CONDITION_RECORD_NAME
        or not data_path.is_file()
        or not metadata_path.is_file()
        or not record_path.is_file()
    ):
        raise OneHotLadderError("completed result artifact layout is invalid")
    _assert_tree_has_no_symlink(
        artifact_root,
        label="completed artifact tree",
    )
    digest_contract = (
        (data_path, "artifact_data_sha256"),
        (metadata_path, "artifact_metadata_sha256"),
        (record_path, "condition_record_sha256"),
    )
    for path, key in digest_contract:
        expected = _sha256_contract(result.get(key), key)
        if sha256_path(path) != expected:
            raise OneHotLadderError("completed result artifact hash differs from its evidence")
    record = leakage._read_json(record_path, "one-hot condition record")
    artifact_document = record.get("artifact")
    artifact_evidence = record.get("artifact_evidence")
    topology = record.get("topology")
    capture = record.get("capture")
    selector = record.get("selector_state_attestation")
    analysis = record.get("marker_independent_analysis")
    continuity_record = record.get("continuity_audit")
    safety = record.get("safety")
    try:
        artifact = ArtifactSummary.model_validate(artifact_document)
        if (
            artifact.artifact_id != result.get("artifact_id")
            or Path(artifact.path).resolve(strict=True) != artifact_root
            or artifact.sha256 != result.get("artifact_data_sha256")
            or not verify_artifact(artifact)
        ):
            raise OneHotLadderError("completed SigMF artifact verification failed")
        metadata = load_metadata(artifact)
        continuity = audit_continuity_metadata(
            metadata,
            expected_total_samples=leakage.TOTAL_SAMPLES,
            expected_samples_per_block=leakage.SAMPLES_PER_FRAME,
            expected_sample_rate_hz=float(leakage.SAMPLE_RATE_HZ),
        )
    except (OSError, TypeError, ValueError) as error:
        raise OneHotLadderError("completed SigMF artifact/ABI2 audit failed") from error
    pilot = capture.get("pilot_frequency_refinement") if isinstance(capture, Mapping) else None
    headroom = capture.get("adc_headroom_admission") if isinstance(capture, Mapping) else None
    rf_readback = capture.get("rf_readback_evidence") if isinstance(capture, Mapping) else None
    rx2 = analysis.get("rx2") if isinstance(analysis, Mapping) else None
    transfer = analysis.get("rx2_over_rx1") if isinstance(analysis, Mapping) else None
    if not isinstance(rf_readback, Mapping):
        raise OneHotLadderError("completed result lacks live RF readback evidence")
    try:
        validate_tx1_rf_readback_evidence(
            rf_readback,
            planned_kernel_buffers=int(configuration["kernel_buffers"]),
            planned_tx_gain_db=float(condition["tx_hardware_gain_db"]),
            planned_dds_scale=float(configuration["dds_scale"]),
            planned_tone_hz=float(configuration["tone_offset_hz_requested"]),
            sample_rate_hz=float(configuration["sample_rate_hz"]),
        )
        active_tone_readback_hz = leakage._active_tone_readback_hz(rf_readback)
    except (KeyError, RuntimeError, TypeError, ValueError) as error:
        raise OneHotLadderError("completed live RF readback is invalid") from error
    if (
        record.get("schema") != 1
        or record.get("record_kind") != "5g8_marker_independent_static_one_hot_path_condition"
        or record.get("accepted_raw_artifact") is not False
        or record.get("accepted_raw_artifact_pending_manifest_commit") is not True
        or record.get("standalone_condition_record_is_not_acceptance") is not True
        or record.get("acceptance_authority")
        != "plan-bound complete manifest attempt plus artifact revalidation"
        or record.get("accepted_for_selector_path_characterization") is not False
        or record.get("accepted_for_selector_calibration") is not False
        or record.get("causal_attribution_claim") is not False
        or record.get("operational_switching_claim") is not False
        or record.get("selector_path_characterization_pending_manifest_validation")
        != result.get("measurement_quality_passed")
        or record.get("immutable_plan") != plan_evidence
        or record.get("native_libiio_runtime_attestation") != dict(native_runtime_attestation)
        or record.get("native_libiio_runtime_attestation_sha256") != native_runtime_sha256
        or record.get("condition") != dict(condition)
        or not isinstance(artifact_evidence, Mapping)
        or artifact_evidence.get("artifact_id") != result.get("artifact_id")
        or artifact_evidence.get("path") != str(artifact_root)
        or artifact_evidence.get("data_path") != str(data_path)
        or artifact_evidence.get("data_sha256") != result.get("artifact_data_sha256")
        or artifact_evidence.get("metadata_path") != str(metadata_path)
        or artifact_evidence.get("metadata_sha256") != result.get("artifact_metadata_sha256")
        or not isinstance(topology, Mapping)
        or topology.get("identity") != TOPOLOGY_IDENTITY
        or topology.get("driven_input") != condition.get("driven_input")
        or topology.get("physical_cell_role") != condition.get("physical_cell_role")
        or topology.get("fixture_identity") != condition.get("fixture_identity")
        or not isinstance(capture, Mapping)
        or capture.get("serial") != configuration.get("serial")
        or capture.get("uri") != configuration.get("uri")
        or capture.get("center_frequency_hz") != configuration.get("center_frequency_hz")
        or capture.get("sample_rate_hz") != configuration.get("sample_rate_hz")
        or capture.get("bandwidth_hz") != configuration.get("bandwidth_hz")
        or capture.get("receiver_gain_db") != configuration.get("receiver_gain_db")
        or capture.get("tx_channel") != configuration.get("tx_channel")
        or capture.get("tx_port") != configuration.get("tx_port")
        or capture.get("tx2_required_exact_muted") != configuration.get("tx2_required_exact_muted")
        or capture.get("tx_hardware_gain_db_requested") != condition.get("tx_hardware_gain_db")
        or capture.get("dds_scale_requested") != configuration.get("dds_scale")
        or capture.get("samples_per_frame") != configuration.get("samples_per_frame")
        or capture.get("frame_count") != configuration.get("frame_count")
        or capture.get("sample_count") != configuration.get("sample_count_per_condition")
        or capture.get("kernel_buffers") != configuration.get("kernel_buffers")
        or capture.get("stream_id") != result.get("stream_id")
        or capture.get("metadata_abi") != configuration.get("metadata_abi")
        or capture.get("tone_offset_hz_requested") != configuration.get("tone_offset_hz_requested")
        or capture.get("tone_offset_hz_requested") != result.get("tone_offset_hz_requested")
        or capture.get("tone_offset_hz_readback") != result.get("tone_offset_hz_readback")
        or capture.get("tone_offset_hz_readback") != active_tone_readback_hz
        or capture.get("tone_offset_hz_measured") != result.get("tone_offset_hz_measured")
        or not isinstance(pilot, Mapping)
        or pilot.get("confidence") != result.get("pilot_confidence")
        or not isinstance(headroom, Mapping)
        or headroom.get("passed") != result.get("headroom_passed")
        or result.get("headroom_passed") is not True
        or not isinstance(selector, Mapping)
        or selector.get("before_condition") != result.get("selector_before_condition")
        or selector.get("after_pluto_mute") != result.get("selector_after_pluto_mute")
        or selector.get("hold_command_unchanged") is not True
        or selector.get("cleanup_all_off") != result.get("selector_cleanup_all_off")
        or record.get("measurement_quality_passed") != result.get("measurement_quality_passed")
        or record.get("measurement_quality_rejection_reasons")
        != result.get("measurement_quality_rejection_reasons")
        or record.get("rx2_tone_detected") != result.get("rx2_tone_detected")
        or not isinstance(analysis, Mapping)
        or not isinstance(rx2, Mapping)
        or rx2.get("tone_detected") != result.get("rx2_tone_detected")
        or transfer != result.get("rx2_over_rx1")
        or continuity != continuity_record
        or continuity.get("stream_id") != result.get("stream_id")
        or continuity.get("metadata_abi") != 2
        or not isinstance(safety, Mapping)
        or safety.get("post_condition_exact_serial_mute")
        != result.get("post_condition_exact_serial_mute")
        or safety.get("selected_state_mailbox_command_unchanged") is not True
        or safety.get("selected_state_lease_survived_capture") is not True
        or safety.get("all_off_mailbox_and_gpio_latch_cleanup_passed") is not True
        or safety.get("digital_readback_does_not_prove_physical_rf_state") is not True
        or safety.get("fresh_stream_validated") is not True
        or safety.get("automatic_retry_count") != 0
        or result.get("selector_calibration_claim") is not False
        or result.get("causal_attribution_claim") is not False
        or result.get("operational_switching_claim") is not False
    ):
        raise OneHotLadderError("completed result condition record is inconsistent")


def _require_safe_quarantine_tree(
    root: Path,
    *,
    expected_parent: Path,
) -> Path:
    exact_root = _assert_tree_has_no_symlink(
        root,
        label="one-hot orphan artifact tree",
    )
    if exact_root.parent != expected_parent:
        raise OneHotLadderError(
            "one-hot orphan artifact tree resolves outside its immutable parent"
        )
    return exact_root


def _quarantine_orphaned_current_plan_artifacts(
    capture_root: Path,
    *,
    manifest: Mapping[str, Any],
    plan_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Move current-plan condition records lacking a complete manifest attempt aside."""

    if not _path_lexists(capture_root):
        return []
    _assert_path_chain_has_no_symlink(capture_root, label="one-hot run capture root")
    exact_capture_root = capture_root.resolve(strict=True)
    if not exact_capture_root.is_dir():
        raise OneHotLadderError("one-hot run capture root must be a directory")
    referenced: set[str] = set()
    for attempt in manifest.get("attempts", []):
        if (
            not isinstance(attempt, Mapping)
            or attempt.get("status") != "complete"
            or not isinstance((result := attempt.get("result")), Mapping)
            or not isinstance(result.get("artifact_path"), str)
        ):
            continue
        raw_artifact_root = Path(str(result["artifact_path"]))
        _assert_path_chain_has_no_symlink(
            raw_artifact_root,
            label="referenced one-hot artifact path",
        )
        exact_artifact_root = _assert_tree_has_no_symlink(
            raw_artifact_root,
            label="referenced one-hot artifact tree",
        )
        if exact_artifact_root.parent != exact_capture_root:
            raise OneHotLadderError(
                "referenced one-hot artifact resolves outside its immutable run root"
            )
        referenced.add(str(exact_artifact_root))
    quarantines: list[dict[str, Any]] = []
    candidates: list[tuple[Path, Path]] = []
    for candidate in capture_root.iterdir():
        if candidate.name in {".failed", ".partial"}:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise OneHotLadderError("one-hot run capture root contains an unsafe orphan entry")
        candidates.append((candidate, exact_capture_root))
    partial_root = capture_root / ".partial"
    if _path_lexists(partial_root):
        exact_partial_root = _require_real_directory(
            partial_root,
            label="one-hot partial-artifact root",
            expected_parent=exact_capture_root,
        )
        for candidate in partial_root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                raise OneHotLadderError("one-hot partial-artifact root contains an unsafe entry")
            candidates.append((candidate, exact_partial_root))
    failed_root = capture_root / ".failed"
    exact_failed_root: Path | None = None
    if _path_lexists(failed_root):
        _, exact_failed_root = _safe_quarantine_parent(capture_root)
    for candidate, expected_parent in candidates:
        _require_safe_quarantine_tree(
            candidate,
            expected_parent=expected_parent,
        )
        record_path = candidate / CONDITION_RECORD_NAME
        exact_candidate = candidate.resolve(strict=True)
        if str(exact_candidate) in referenced:
            continue
        record: Mapping[str, Any] | None = None
        if record_path.is_file():
            try:
                record = leakage._read_json(record_path, "orphan one-hot condition record")
            except leakage.LeakageLadderError:
                record = None
            if record is not None and record.get("immutable_plan") != plan_evidence:
                raise OneHotLadderError(
                    "run-specific capture root contains a different immutable plan"
                )
        error = OneHotLadderError(
            "current-plan artifact has no complete immutable manifest attempt"
        )
        if exact_failed_root is None:
            _, exact_failed_root = _safe_quarantine_parent(capture_root)
        else:
            current_root, current_failed_root = _safe_quarantine_parent(capture_root)
            if current_root != exact_capture_root or current_failed_root != exact_failed_root:
                raise OneHotLadderError("one-hot quarantine parent identity changed")
        destination = exact_failed_root / f"{candidate.name}.orphaned"
        if _path_lexists(destination):
            raise OneHotLadderError("orphan quarantine destination already exists")
        os.replace(exact_candidate, destination)
        _require_safe_quarantine_tree(
            destination,
            expected_parent=exact_failed_root,
        )
        quarantines.append(
            leakage._seal_failed_directory(
                destination,
                artifact_id=candidate.name,
                error=error,
                context={
                    "immutable_plan": dict(plan_evidence),
                    "orphan_condition_record_present": record is not None,
                    "orphan_condition_record_sha256": (
                        sha256_path(destination / CONDITION_RECORD_NAME)
                        if (destination / CONDITION_RECORD_NAME).is_file()
                        else None
                    ),
                },
            )
        )
    return quarantines


def _downgrade_and_quarantine_completed_attempt(
    attempt: Mapping[str, Any],
    *,
    result: object,
    capture_root: Path,
    error: BaseException,
) -> None:
    quarantine: dict[str, Any] | None = None
    quarantine_error: dict[str, Any] | None = None
    if isinstance(result, Mapping) and isinstance(result.get("artifact_path"), str):
        try:
            raw_source = Path(str(result["artifact_path"]))
            _assert_path_chain_has_no_symlink(
                raw_source,
                label="completed one-hot artifact path",
            )
            exact_capture_root, failed_root = _safe_quarantine_parent(capture_root)
            source = _assert_tree_has_no_symlink(
                raw_source,
                label="completed one-hot artifact tree",
            )
            if source.parent != exact_capture_root:
                raise OneHotLadderError(
                    "completed one-hot artifact resolves outside its immutable run root"
                )
            destination = failed_root / f"{source.name}.resume-invalid"
            if _path_lexists(destination):
                raise OneHotLadderError("resume-invalid quarantine destination already exists")
            os.replace(source, destination)
            _require_safe_quarantine_tree(
                destination,
                expected_parent=failed_root,
            )
            quarantine = leakage._seal_failed_directory(
                destination,
                artifact_id=source.name,
                error=error,
                context={"invalid_completed_result": leakage._json_safe(result)},
            )
        except (OSError, leakage.LeakageLadderError) as quarantine_failure:
            quarantine_error = leakage._error_document(quarantine_failure)
    else:
        quarantine_error = leakage._error_document(
            OneHotLadderError("completed result lacks a quarantinable artifact path")
        )
    if isinstance(attempt, dict):
        attempt["status"] = "failed"
        attempt["outcome"] = "resume_validation_failed"
        attempt["failure_kind"] = "completed_artifact_or_evidence_invalid"
        attempt["quarantine"] = quarantine
        attempt["quarantine_error"] = quarantine_error
        attempt["error"] = leakage._error_document(error)
        attempt["completed_at"] = _now()


def _completed_condition_ids(
    manifest: Mapping[str, Any],
    *,
    planned_conditions: Mapping[str, Mapping[str, Any]],
    selector_control: Mapping[str, Any],
    configuration: Mapping[str, Any],
    native_runtime_attestation: Mapping[str, Any],
    native_runtime_sha256: str,
    serial: str,
    plan_evidence: Mapping[str, Any],
    capture_root: Path,
    downgrade_invalid: bool = True,
) -> set[str]:
    completed: set[str] = set()
    stream_ids: set[int] = set()
    artifact_ids: set[str] = set()
    artifact_data_hashes: set[str] = set()
    condition_record_hashes: set[str] = set()
    for raw in manifest.get("attempts", []):
        if not isinstance(raw, Mapping):
            raise OneHotLadderError("manifest attempt is malformed")
        condition_id = raw.get("condition_id")
        if not isinstance(condition_id, str) or condition_id not in planned_conditions:
            raise OneHotLadderError("manifest attempt is not bound to an immutable condition")
        if raw.get("status") != "complete":
            raise OneHotLadderError(
                "one-hot manifest contains a non-complete or unknown-status attempt"
            )
        if raw.get("status") == "complete":
            result = raw.get("result")
            condition = planned_conditions[condition_id]
            driven_input = str(condition["driven_input"])
            state_name = str(condition["selector_state_name"])
            state_code = int(condition["selector_gpio_code"])
            if (
                condition_id in completed
                or not isinstance(result, Mapping)
                or raw.get("condition") != dict(condition)
                or raw.get("automatic_retry_attempted") is not False
                or raw.get("failure_kind") is not None
                or raw.get("quarantine") is not None
                or raw.get("quarantine_error") is not None
                or raw.get("error") is not None
                or raw.get("post_condition_exact_serial_mute")
                != result.get("post_condition_exact_serial_mute")
                or raw.get("outcome")
                != (
                    "measurement_quality_passed"
                    if result.get("measurement_quality_passed") is True
                    else "measurement_quality_rejected"
                )
                or result.get("metadata_abi") != 2
                or isinstance(result.get("stream_id"), bool)
                or not isinstance(result.get("stream_id"), int)
                or result.get("condition_id") != condition_id
                or result.get("native_libiio_runtime_attestation")
                != dict(native_runtime_attestation)
                or result.get("native_libiio_runtime_attestation_sha256") != native_runtime_sha256
                or result.get("topology_identity") != TOPOLOGY_IDENTITY
                or result.get("driven_input") != driven_input
                or result.get("fixture_identity") != condition.get("fixture_identity")
                or result.get("physical_cell_role") != condition.get("physical_cell_role")
                or result.get("selector_state_name") != state_name
                or result.get("selector_gpio_code") != state_code
                or result.get("tx_hardware_gain_db") != condition.get("tx_hardware_gain_db")
                or result.get("repeat_index") != condition.get("repeat_index")
                or result.get("repeat_count_at_gain") != condition.get("repeat_count_at_gain")
                or result.get("independent_fresh_stream_repeat") is not True
                or result.get("attribution_gain_condition")
                != condition.get("attribution_gain_condition")
                or result.get("selector_hold_command_unchanged") is not True
                or not leakage._mute_passed(
                    result.get("post_condition_exact_serial_mute"),
                    serial=serial,
                    purpose="post_condition",
                )
                or not _selector_passed(
                    result.get("selector_before_condition"),
                    selector_control=selector_control,
                    state_name=state_name,
                    state_code=state_code,
                    purpose="before_condition",
                )
                or not _selector_passed(
                    result.get("selector_after_pluto_mute"),
                    selector_control=selector_control,
                    state_name=state_name,
                    state_code=state_code,
                    purpose="after_pluto_mute",
                )
                or not _selector_passed(
                    result.get("selector_cleanup_all_off"),
                    selector_control=selector_control,
                    state_name=state_name,
                    state_code=state_code,
                    purpose="cleanup_all_off",
                )
                or not _selector_hold_command_unchanged(
                    result.get("selector_before_condition"),
                    result.get("selector_after_pluto_mute"),
                )
            ):
                duplicate_error = OneHotLadderError(
                    "completed one-hot attempt evidence is malformed"
                )
                if downgrade_invalid:
                    _downgrade_and_quarantine_completed_attempt(
                        raw,
                        result=result,
                        capture_root=capture_root,
                        error=duplicate_error,
                    )
                raise duplicate_error
            try:
                _verify_completed_result_files(
                    result,
                    condition=condition,
                    configuration=configuration,
                    native_runtime_attestation=native_runtime_attestation,
                    native_runtime_sha256=native_runtime_sha256,
                    plan_evidence=plan_evidence,
                    capture_root=capture_root,
                )
            except BaseException as error:
                if downgrade_invalid:
                    _downgrade_and_quarantine_completed_attempt(
                        raw,
                        result=result,
                        capture_root=capture_root,
                        error=error,
                    )
                raise
            stream_id = int(result["stream_id"])
            artifact_id = result.get("artifact_id")
            data_sha = result.get("artifact_data_sha256")
            record_sha = result.get("condition_record_sha256")
            if (
                not isinstance(artifact_id, str)
                or not artifact_id
                or stream_id in stream_ids
                or artifact_id in artifact_ids
                or data_sha in artifact_data_hashes
                or record_sha in condition_record_hashes
            ):
                reuse_error = OneHotLadderError(
                    "completed one-hot conditions reused an artifact or ABI2 stream identity"
                )
                if downgrade_invalid:
                    _downgrade_and_quarantine_completed_attempt(
                        raw,
                        result=result,
                        capture_root=capture_root,
                        error=reuse_error,
                    )
                raise reuse_error
            stream_ids.add(stream_id)
            artifact_ids.add(artifact_id)
            artifact_data_hashes.add(str(data_sha))
            condition_record_hashes.add(str(record_sha))
            completed.add(condition_id)
    return completed


def _physical_confirmation_reverified(
    value: object,
    *,
    contract: Mapping[str, Any],
) -> bool:
    driven_input = str(contract["driven_input"])
    fixture = contract["fixture_identity"]
    return (
        isinstance(value, Mapping)
        and value.get("topology_identity") == TOPOLOGY_IDENTITY
        and value.get("driven_input") == driven_input
        and value.get("fixture_identity") == fixture
        and value.get("topology_confirmation_token") == physical_confirmation_token(driven_input)
        and value.get("no_antennas_anywhere") is True
        and value.get("tx1_matched_conducted_network") is True
        and value.get("tx2_muted_and_50ohm_terminated") is True
        and value.get("rx1_attenuated_conducted_reference") is True
        and value.get("reviewed_static_one_hot_mailbox_control") is True
        and value.get("exactly_one_board_input_driven") is True
        and value.get("other_seven_board_inputs_individually_50ohm_terminated") is True
        and value.get("simultaneous_eight_way_feed_absent") is True
        and value.get("attribution_repeats_without_cable_movement") is True
    )


def load_verified_one_hot_row_bundle(
    *,
    plan_path: Path,
    manifest_path: Path,
) -> VerifiedOneHotRowBundle:
    """Open and reverify one complete row before pure matrix aggregation."""

    expanded_plan_path = plan_path.expanduser()
    expanded_manifest_path = manifest_path.expanduser()
    if expanded_plan_path.is_symlink() or expanded_manifest_path.is_symlink():
        raise OneHotLadderError("row plan and manifest must not be symlinks")
    exact_plan_path = expanded_plan_path.resolve(strict=True)
    exact_manifest_path = expanded_manifest_path.resolve(strict=True)
    raw_envelope = leakage._read_json(exact_plan_path, "immutable one-hot plan")
    raw_contract = raw_envelope.get("plan_contract")
    if not isinstance(raw_contract, Mapping):
        raise OneHotLadderError("immutable one-hot plan contract is malformed")
    envelope = leakage._validate_plan_envelope(
        raw_envelope,
        expected_contract=raw_contract,
    )
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    manifest = _load_manifest(
        exact_manifest_path,
        plan_path=exact_plan_path,
        envelope=envelope,
    )
    selector_control = contract.get("selector_control")
    configuration = contract.get("configuration")
    conditions = contract.get("conditions")
    fixture_identity = contract.get("fixture_identity")
    if (
        contract.get("topology_identity") != TOPOLOGY_IDENTITY
        or not isinstance(selector_control, Mapping)
        or not isinstance(configuration, Mapping)
        or not isinstance(conditions, list)
        or not isinstance(fixture_identity, Mapping)
    ):
        raise OneHotLadderError("row plan is not an exact one-hot physical-row plan")
    native_runtime, native_runtime_sha256 = _native_runtime_from_contract(contract)
    if not runtime_preflight_passed(
        manifest.get("native_runtime_preflight"),
        expected=native_runtime,
    ):
        raise OneHotLadderError("row native libiio capture-runtime preflight is invalid")
    _verify_fixture_evidence(fixture_identity)
    if (
        manifest.get("status") != "complete"
        or manifest.get("error") is not None
        or manifest.get("causal_attribution_claim") is not False
        or manifest.get("operational_switching_claim") is not False
    ):
        raise OneHotLadderError("row manifest is not complete and claim-narrowed")
    confirmations = manifest.get("confirmations")
    if (
        not isinstance(confirmations, list)
        or not confirmations
        or not all(
            _physical_confirmation_reverified(item, contract=contract) for item in confirmations
        )
    ):
        raise OneHotLadderError("row physical confirmation provenance is invalid")
    if not _target_image_passed(
        manifest.get("target_image_preflight"),
        selector_control=selector_control,
    ):
        raise OneHotLadderError("row target-image attestation is invalid")
    serial = str(configuration["serial"])
    state_codes = _state_map(selector_control)
    if not leakage._mute_passed(
        manifest.get("preflight_mute"),
        serial=serial,
        purpose="preflight",
    ):
        raise OneHotLadderError("row preflight exact-serial mute attestation is invalid")
    if not _selector_passed(
        manifest.get("preflight_selector_cleanup"),
        selector_control=selector_control,
        state_name=ALL_OFF_STATE,
        state_code=state_codes[ALL_OFF_STATE],
        purpose="preflight_cleanup_all_off",
    ):
        raise OneHotLadderError("row preflight digital ALL_OFF attestation is invalid")
    if not leakage._mute_passed(manifest.get("final_mute"), serial=serial, purpose="final"):
        raise OneHotLadderError("row final exact-serial mute attestation is invalid")
    if not _selector_passed(
        manifest.get("final_selector_cleanup"),
        selector_control=selector_control,
        state_name=ALL_OFF_STATE,
        state_code=state_codes[ALL_OFF_STATE],
        purpose="final_cleanup_all_off",
    ):
        raise OneHotLadderError("row final digital ALL_OFF attestation is invalid")

    complete_results = [
        attempt["result"]
        for attempt in manifest.get("attempts", [])
        if isinstance(attempt, Mapping)
        and attempt.get("status") == "complete"
        and isinstance(attempt.get("result"), Mapping)
    ]
    storage = contract.get("storage")
    if not isinstance(storage, Mapping) or not isinstance(storage.get("run_capture_root"), str):
        raise OneHotLadderError("row plan lacks its immutable run-specific capture root")
    capture_root = Path(str(storage["run_capture_root"])).resolve(strict=True)
    planned_conditions = {
        str(condition["condition_id"]): condition
        for condition in conditions
        if isinstance(condition, Mapping)
    }
    plan_evidence = leakage._plan_file_evidence(exact_plan_path, envelope)
    completed = _completed_condition_ids(
        manifest,
        planned_conditions=planned_conditions,
        selector_control=selector_control,
        configuration=configuration,
        native_runtime_attestation=native_runtime,
        native_runtime_sha256=native_runtime_sha256,
        serial=serial,
        plan_evidence=plan_evidence,
        capture_root=capture_root,
        downgrade_invalid=False,
    )
    if len(completed) != len(conditions) or len(complete_results) != len(conditions):
        raise OneHotLadderError("row does not contain every immutable condition")
    recomputed = summarize_one_hot_run(
        complete_results,
        driven_input=str(contract["driven_input"]),
        fixture_identity=fixture_identity,
        planned_states=tuple(configuration["selector_state_order"]),
        planned_gains_db=tuple(configuration["tx_hardware_gains_db"]),
        attribution_gain_db=float(configuration["attribution_tx_hardware_gain_db"]),
        attribution_repeat_count=int(configuration["attribution_repeat_count"]),
        minimum_detected_attribution_repeats=int(
            configuration["minimum_detected_attribution_repeats"]
        ),
        minimum_intended_through_contrast_over_all_off_db=float(
            configuration["minimum_intended_through_contrast_over_all_off_db"]
        ),
        maximum_attribution_amplitude_span_db=float(
            configuration["maximum_attribution_amplitude_span_db"]
        ),
        maximum_attribution_phase_residual_deg=float(
            configuration["maximum_attribution_phase_residual_deg"]
        ),
    )
    recomputed_document = leakage._json_safe(asdict(recomputed))
    if manifest.get("one_hot_run_summary") != recomputed_document:
        raise OneHotLadderError("row summary differs from reaggregated condition evidence")
    if not recomputed.quality_passed:
        raise OneHotLadderError("row repeat/measurement quality admission did not pass")
    manifest_sha = sha256_path(exact_manifest_path)
    return _seal_verified_one_hot_row_bundle(
        {
            "schema": 1,
            "row_bundle_kind": "verified_one_hot_row",
            "run_id": contract["run_id"],
            "topology_identity": TOPOLOGY_IDENTITY,
            "driven_input": contract["driven_input"],
            "fixture_identity": fixture_identity,
            "matrix_identity": _matrix_identity_from_contract(contract),
            "native_libiio_runtime_attestation": native_runtime,
            "native_libiio_runtime_attestation_sha256": native_runtime_sha256,
            "manifest_status": "complete",
            "manifest_sha256": manifest_sha,
            "plan_contract_sha256": envelope["plan_contract_sha256"],
            "plan_file_sha256": sha256_path(exact_plan_path),
            "physical_confirmation_verified": True,
            "physical_confirmation_token": physical_confirmation_token(
                str(contract["driven_input"])
            ),
            "verification_evidence": {
                "verification_kind": "local_manifest_plan_artifact_byte_verification",
                "manifest_path": str(exact_manifest_path),
                "manifest_file_sha256": manifest_sha,
                "plan_path": str(exact_plan_path),
                "plan_contract_sha256": envelope["plan_contract_sha256"],
                "plan_file_sha256": sha256_path(exact_plan_path),
                "condition_artifacts_reverified": True,
                "abi2_continuity_reaudited": True,
                "native_libiio_capture_runtime_preflight_reverified": True,
                "physical_confirmation_reverified": True,
            },
            "results": [dict(result) for result in complete_results],
        }
    )


def _selector_cleanup(
    boundary: OneHotSelectorBoundary,
    selector_control: Mapping[str, Any],
    purpose: str,
) -> dict[str, Any]:
    states = _state_map(selector_control)
    return _call_selector(
        boundary,
        selector_control,
        ALL_OFF_STATE,
        states[ALL_OFF_STATE],
        purpose,
    )


def _execute_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    envelope: Mapping[str, Any],
    plan_path: Path,
    confirmation: Mapping[str, Any],
    capture_root: Path,
    capture_boundary: leakage.CaptureBoundary = leakage._live_capture_boundary,
    mute_boundary: leakage.MuteBoundary = leakage._strict_mute,
    identity_boundary: leakage.IdentityBoundary = leakage._live_identity_boundary,
    selector_boundary: OneHotSelectorBoundary = _live_selector_boundary,
    target_image_boundary: TargetImageBoundary = _live_target_image_attestation,
    runtime_attestation_boundary: RuntimeAttestationBoundary = attest_runtime,
    fixture_evidence_boundary: Any = _verify_fixture_evidence,
) -> None:
    contract = envelope["plan_contract"]
    assert isinstance(contract, Mapping)
    conditions = contract["conditions"]
    configuration = contract["configuration"]
    selector_control = contract["selector_control"]
    assert isinstance(conditions, list)
    assert isinstance(configuration, Mapping)
    assert isinstance(selector_control, Mapping)
    serial = str(configuration["serial"])
    uri = str(configuration["uri"])
    condition_count = len(conditions)
    plan_evidence = leakage._plan_file_evidence(plan_path, envelope)
    native_runtime, native_runtime_sha256 = _native_runtime_from_contract(contract)
    if manifest.get("status") == "complete":
        raise OneHotLadderError("completed one-hot runs cannot execute again")
    first_execution = manifest.get("execution_started") is not True
    if first_execution:
        manifest["execution_started"] = True
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    runtime_preflight = call_runtime_preflight(
        runtime_attestation_boundary,
        now=_now,
        error_document=leakage._error_document,
    )
    manifest["native_runtime_preflight_attempts"].append(runtime_preflight)
    manifest["native_runtime_preflight"] = runtime_preflight
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not runtime_preflight_passed(runtime_preflight, expected=native_runtime):
        error = OneHotLadderError(
            "native libiio runtime differs from the immutable path/version/hash attestation"
        )
        manifest["status"] = "failed"
        manifest["error"] = leakage._error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise error

    fixture_identity = contract["fixture_identity"]
    assert isinstance(fixture_identity, Mapping)
    try:
        fixture_evidence_boundary(fixture_identity)
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["error"] = leakage._error_document(error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise

    identity = leakage._call_identity(identity_boundary, serial, uri)
    manifest["identity_preflight_attempts"].append(identity)
    manifest["identity_preflight"] = identity
    _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if not leakage._identity_passed(identity, serial=serial, requested_uri=uri):
        recovery_mute = leakage._call_mute(
            mute_boundary,
            serial,
            "identity_preflight_recovery",
        )
        recovery_selector = _selector_cleanup(
            selector_boundary,
            selector_control,
            "identity_failure_cleanup_all_off",
        )
        manifest["recovery_mute_attempts"].append(recovery_mute)
        manifest["recovery_selector_cleanup_attempts"].append(recovery_selector)
        identity_error = OneHotLadderError(
            "read-only USB identity scan did not resolve the requested URI; exact-serial mute "
            "and selector digital ALL_OFF recovery were attempted"
        )
        manifest["status"] = "failed"
        manifest["error"] = leakage._error_document(identity_error)
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        raise identity_error

    stale = [
        item
        for item in manifest["attempts"]
        if isinstance(item, dict) and item.get("status") == "running"
    ]
    pending_error: BaseException | None = None
    try:
        if manifest.get("status") == "failed" or any(
            isinstance(item, Mapping) and item.get("status") == "failed"
            for item in manifest["attempts"]
        ):
            recovery_mute = leakage._call_mute(mute_boundary, serial, "resume_recovery")
            recovery_selector = _selector_cleanup(
                selector_boundary,
                selector_control,
                "resume_cleanup_all_off",
            )
            manifest["recovery_mute_attempts"].append(recovery_mute)
            manifest["recovery_selector_cleanup_attempts"].append(recovery_selector)
            raise OneHotLadderError("failed one-hot runs cannot retry; prepare a new run ID")
        if not first_execution and manifest.get("status") == "prepared":
            recovery_mute = leakage._call_mute(mute_boundary, serial, "resume_recovery")
            recovery_selector = _selector_cleanup(
                selector_boundary,
                selector_control,
                "resume_cleanup_all_off",
            )
            manifest["recovery_mute_attempts"].append(recovery_mute)
            manifest["recovery_selector_cleanup_attempts"].append(recovery_selector)
            raise OneHotLadderError(
                "started one-hot run has no resumable progress; prepare a new run ID"
            )
        if stale:
            recovery_mute = leakage._call_mute(mute_boundary, serial, "resume_recovery")
            recovery_selector = _selector_cleanup(
                selector_boundary,
                selector_control,
                "resume_cleanup_all_off",
            )
            manifest["recovery_mute_attempts"].append(recovery_mute)
            manifest["recovery_selector_cleanup_attempts"].append(recovery_selector)
            for stale_attempt in stale:
                stale_attempt["status"] = "failed"
                stale_attempt["outcome"] = "stale_process_failed"
                stale_attempt["failure_kind"] = "stale_process"
                stale_attempt["recovery_mute"] = recovery_mute
                stale_attempt["recovery_selector_cleanup"] = recovery_selector
                stale_attempt["completed_at"] = _now()
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            raise OneHotLadderError("stale live attempt recovered; use a new run ID")

        manifest["confirmations"].append(dict(confirmation))
        manifest["status"] = "running"
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)

        preflight = leakage._call_mute(mute_boundary, serial, "preflight")
        manifest["preflight_mute_attempts"].append(preflight)
        manifest["preflight_mute"] = preflight
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not leakage._mute_passed(preflight, serial=serial, purpose="preflight"):
            raise OneHotLadderError("exact preflight mute attestation failed")

        preflight_selector = _selector_cleanup(
            selector_boundary,
            selector_control,
            "preflight_cleanup_all_off",
        )
        manifest["preflight_selector_cleanup_attempts"].append(preflight_selector)
        manifest["preflight_selector_cleanup"] = preflight_selector
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        states = _state_map(selector_control)
        if not _selector_passed(
            preflight_selector,
            selector_control=selector_control,
            state_name=ALL_OFF_STATE,
            state_code=states[ALL_OFF_STATE],
            purpose="preflight_cleanup_all_off",
        ):
            raise OneHotLadderError("selector preflight ALL_OFF attestation failed")

        orphan_quarantines = _quarantine_orphaned_current_plan_artifacts(
            capture_root,
            manifest=manifest,
            plan_evidence=plan_evidence,
        )
        manifest["orphan_quarantine_attempts"].extend(orphan_quarantines)
        if orphan_quarantines:
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)

        target_image = _call_target_image_attestation(
            target_image_boundary,
            selector_control,
        )
        manifest["target_image_preflight_attempts"].append(target_image)
        manifest["target_image_preflight"] = target_image
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not _target_image_passed(target_image, selector_control=selector_control):
            raise OneHotLadderError("target flash is not the exact ELF-bound reviewed bench image")

        planned_conditions = {
            str(condition["condition_id"]): condition
            for condition in conditions
            if isinstance(condition, Mapping)
        }
        if len(planned_conditions) != len(conditions):
            raise OneHotLadderError("immutable condition IDs are malformed or duplicated")
        completed = _completed_condition_ids(
            manifest,
            planned_conditions=planned_conditions,
            selector_control=selector_control,
            configuration=configuration,
            native_runtime_attestation=native_runtime,
            native_runtime_sha256=native_runtime_sha256,
            serial=serial,
            plan_evidence=plan_evidence,
            capture_root=capture_root,
        )
        forbidden_stream_ids = {
            int(item["result"]["stream_id"])
            for item in manifest["attempts"]
            if isinstance(item, Mapping)
            and item.get("status") == "complete"
            and isinstance(item.get("result"), Mapping)
        }
        for raw_condition in conditions:
            if not isinstance(raw_condition, Mapping):
                raise OneHotLadderError("immutable one-hot condition is malformed")
            condition = dict(raw_condition)
            condition_id = str(condition["condition_id"])
            if condition_id in completed:
                continue
            attempt: dict[str, Any] = {
                "attempt_id": len(manifest["attempts"]) + 1,
                "condition_id": condition_id,
                "condition": condition,
                "started_at": _now(),
                "completed_at": None,
                "status": "running",
                "outcome": None,
                "failure_kind": None,
                "result": None,
                "quarantine": None,
                "quarantine_error": None,
                "post_condition_exact_serial_mute": None,
                "error": None,
                "automatic_retry_attempted": False,
            }
            manifest["attempts"].append(attempt)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)
            try:
                result = _capture_condition(
                    condition,
                    contract=contract,
                    plan_evidence=plan_evidence,
                    capture_root=capture_root,
                    forbidden_stream_ids=forbidden_stream_ids,
                    capture_boundary=capture_boundary,
                    mute_boundary=mute_boundary,
                    selector_boundary=selector_boundary,
                )
            except leakage.ConditionCaptureFailure as error:
                attempt["status"] = "failed"
                attempt["outcome"] = "condition_failed"
                attempt["failure_kind"] = "capture_or_validation"
                attempt["quarantine"] = error.quarantine
                attempt["quarantine_error"] = None
                attempt["post_condition_exact_serial_mute"] = error.post_mute
                attempt["error"] = leakage._error_document(error)
                attempt["completed_at"] = _now()
                _persist_manifest(manifest_path, manifest, condition_count=condition_count)
                raise
            except BaseException as error:
                attempt["status"] = "failed"
                attempt["outcome"] = "condition_failed"
                attempt["failure_kind"] = "capture_or_quarantine_validation"
                attempt["quarantine"] = None
                attempt["quarantine_error"] = leakage._error_document(error)
                attempt["error"] = leakage._error_document(error)
                attempt["completed_at"] = _now()
                _persist_manifest(manifest_path, manifest, condition_count=condition_count)
                raise
            attempt["result"] = result
            attempt["post_condition_exact_serial_mute"] = result["post_condition_exact_serial_mute"]
            attempt["outcome"] = (
                "measurement_quality_passed"
                if result["measurement_quality_passed"]
                else "measurement_quality_rejected"
            )
            attempt["status"] = "complete"
            attempt["completed_at"] = _now()
            forbidden_stream_ids.add(int(result["stream_id"]))
            completed.add(condition_id)
            _persist_manifest(manifest_path, manifest, condition_count=condition_count)

        results = [
            item["result"]
            for item in manifest["attempts"]
            if isinstance(item, Mapping)
            and item.get("status") == "complete"
            and isinstance(item.get("result"), Mapping)
        ]
        run_summary = summarize_one_hot_run(
            results,
            driven_input=str(contract["driven_input"]),
            fixture_identity=contract["fixture_identity"],
            planned_states=tuple(configuration["selector_state_order"]),
            planned_gains_db=tuple(configuration["tx_hardware_gains_db"]),
            attribution_gain_db=float(configuration["attribution_tx_hardware_gain_db"]),
            attribution_repeat_count=int(configuration["attribution_repeat_count"]),
            minimum_detected_attribution_repeats=int(
                configuration["minimum_detected_attribution_repeats"]
            ),
            minimum_intended_through_contrast_over_all_off_db=float(
                configuration["minimum_intended_through_contrast_over_all_off_db"]
            ),
            maximum_attribution_amplitude_span_db=float(
                configuration["maximum_attribution_amplitude_span_db"]
            ),
            maximum_attribution_phase_residual_deg=float(
                configuration["maximum_attribution_phase_residual_deg"]
            ),
        )
        manifest["one_hot_run_summary"] = leakage._json_safe(asdict(run_summary))
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
        if not run_summary.quality_passed:
            raise OneHotLadderError(
                "one-hot driven-input run admission failed: "
                + ", ".join(run_summary.quality_rejection_reasons)
            )
        manifest["status"] = "conditions_complete"
    except BaseException as error:
        pending_error = error
        manifest["error"] = leakage._error_document(error)
        manifest["status"] = "failed"
    finally:
        final_mute = leakage._call_mute(mute_boundary, serial, "final")
        final_selector = _selector_cleanup(
            selector_boundary,
            selector_control,
            "final_cleanup_all_off",
        )
        manifest["final_mute_attempts"].append(final_mute)
        manifest["final_selector_cleanup_attempts"].append(final_selector)
        manifest["final_mute"] = final_mute
        manifest["final_selector_cleanup"] = final_selector
        final_mute_passed = leakage._mute_passed(
            final_mute,
            serial=serial,
            purpose="final",
        )
        states = _state_map(selector_control)
        final_selector_passed = _selector_passed(
            final_selector,
            selector_control=selector_control,
            state_name=ALL_OFF_STATE,
            state_code=states[ALL_OFF_STATE],
            purpose="final_cleanup_all_off",
        )
        if not final_mute_passed or not final_selector_passed:
            pending_error = OneHotLadderError(
                "final exact mute or selector ALL_OFF cleanup attestation failed"
            )
            manifest["error"] = leakage._error_document(pending_error)
            manifest["status"] = "failed"
        elif pending_error is None:
            manifest["status"] = "complete"
            manifest["completed_at"] = _now()
            manifest["error"] = None
        _persist_manifest(manifest_path, manifest, condition_count=condition_count)
    if pending_error is not None:
        raise pending_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", default=DEFAULT_BOARD_ID)
    parser.add_argument("--serial", required=True, help="exact Pluto USB serial")
    parser.add_argument("--uri", required=True, help="current exact usb: IIO URI")
    parser.add_argument("--driven-input", choices=ANTENNA_STATES, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-only", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--bench-manifest", type=Path, required=True)
    parser.add_argument("--openocd-config", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--feed-arm-id", required=True)
    parser.add_argument("--feed-cable-id", required=True)
    parser.add_argument("--termination-load-set-id", required=True)
    parser.add_argument("--rx1-reference-plane-id", required=True)
    parser.add_argument("--rx2-reference-plane-id", required=True)
    parser.add_argument("--setup-evidence-file", type=Path, required=True)
    parser.add_argument("--confirm-no-antennas", action="store_true")
    parser.add_argument("--confirm-tx1-matched-conducted", action="store_true")
    parser.add_argument("--confirm-tx2-terminated-muted", action="store_true")
    parser.add_argument("--confirm-rx1-conducted-reference", action="store_true")
    parser.add_argument("--confirm-one-hot-static-control", action="store_true")
    parser.add_argument("--confirm-single-driven-input", action="store_true")
    parser.add_argument("--confirm-other-seven-terminated", action="store_true")
    parser.add_argument("--confirm-no-simultaneous-eight-way-feed", action="store_true")
    parser.add_argument(
        "--confirm-attribution-repeats-no-cable-movement",
        action="store_true",
    )
    parser.add_argument("--confirm-topology-token")
    return parser


def _signal_handler(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(
        f"received {signal.Signals(signum).name}; entering fail-muted ALL_OFF cleanup"
    )


def _install_signal_handlers() -> None:
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, _signal_handler)


def _prepare_plan_only_state(
    plan_path: Path,
    manifest_path: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_exists = _path_lexists(plan_path)
    manifest_exists = _path_lexists(manifest_path)
    state_exists = _run_state_is_present(manifest_path)
    if plan_exists:
        if plan_path.is_symlink() or not plan_path.is_file():
            raise OneHotLadderError("immutable one-hot plan must be a real regular file")
        if not manifest_exists or not state_exists:
            raise OneHotLadderError(
                "existing one-hot plan lacks its manifest or persistent run state; "
                "run ID cannot be recreated"
            )
    elif manifest_exists or state_exists:
        raise OneHotLadderError(
            "one-hot manifest or persistent run state exists without its immutable plan; "
            "run ID is burned"
        )

    envelope = leakage._prepare_plan(plan_path, contract)
    if plan_exists:
        manifest = _load_manifest(
            manifest_path,
            plan_path=plan_path,
            envelope=envelope,
        )
    else:
        manifest = _new_manifest(plan_path, envelope)
        _persist_manifest(
            manifest_path,
            manifest,
            condition_count=len(contract["conditions"]),
        )
    return envelope, manifest


def main() -> int:
    args = _parser().parse_args()
    _install_signal_handlers()
    try:
        source_commit = leakage._repository_commit_and_require_clean(
            _REPOSITORY,
            "smateway",
        )
        dependency_attestation = attest_pluto_plus_utils_source()
        native_runtime_attestation = attest_runtime()
        selector_control = _one_hot_selector_control_contract(
            bench_manifest_path=args.bench_manifest,
            openocd_config_path=args.openocd_config,
            profile_path=args.profile,
            source_commit=source_commit,
        )
        fixture_identity = _fixture_identity_from_cli(
            feed_arm_id=args.feed_arm_id,
            feed_cable_id=args.feed_cable_id,
            termination_load_set_id=args.termination_load_set_id,
            rx1_reference_plane_id=args.rx1_reference_plane_id,
            rx2_reference_plane_id=args.rx2_reference_plane_id,
            setup_evidence_path=args.setup_evidence_file,
        )
        contract = _build_plan_contract(
            run_id=args.run_id,
            board_id=args.board_id,
            serial=args.serial,
            uri=args.uri,
            driven_input=args.driven_input,
            source_commit=source_commit,
            pluto_plus_utils_source_attestation=dependency_attestation,
            native_libiio_runtime_attestation=native_runtime_attestation,
            selector_control=selector_control,
            fixture_identity=fixture_identity,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    board_root = leakage._board_root(str(contract["board_id"]))
    selector_lock_root = (
        Path.home() / ".local/state/smateway/hardware-locks" / "pluto-rx2-8way-selector-bench"
    )
    run_root = board_root / "5g8-one-hot-path-ladder" / str(contract["run_id"])
    plan_path = run_root / PLAN_FILENAME
    manifest_path = run_root / MANIFEST_FILENAME
    with leakage._board_lock(selector_lock_root), leakage._board_lock(board_root):
        if args.plan_only:
            try:
                envelope, manifest = _prepare_plan_only_state(
                    plan_path,
                    manifest_path,
                    contract,
                )
            except (OneHotLadderError, leakage.LeakageLadderError) as error:
                raise SystemExit(str(error)) from error
            print(
                json.dumps(
                    {
                        "run_id": contract["run_id"],
                        "topology_identity": contract["topology_identity"],
                        "driven_input": contract["driven_input"],
                        "status": manifest["status"],
                        "immutable_plan": str(plan_path),
                        "plan_contract_sha256": envelope["plan_contract_sha256"],
                        "plan_file_sha256": sha256_path(plan_path),
                        "manifest": str(manifest_path),
                        "condition_count": len(contract["conditions"]),
                        "rf_activity": False,
                    },
                    sort_keys=True,
                )
            )
            return 0

        if (
            plan_path.is_symlink()
            or manifest_path.is_symlink()
            or not plan_path.is_file()
            or not manifest_path.is_file()
            or not _run_state_is_present(manifest_path)
        ):
            raise SystemExit("execute requires a prior successful --plan-only invocation")
        try:
            envelope = leakage._validate_plan_envelope(
                leakage._read_json(plan_path, "immutable one-hot plan"),
                expected_contract=contract,
            )
            manifest = _load_manifest(
                manifest_path,
                plan_path=plan_path,
                envelope=envelope,
            )
            confirmation = _validate_confirmations(
                driven_input=args.driven_input,
                fixture_identity=contract["fixture_identity"],
                topology_token=args.confirm_topology_token,
                no_antennas=args.confirm_no_antennas,
                tx1_matched=args.confirm_tx1_matched_conducted,
                tx2_terminated_muted=args.confirm_tx2_terminated_muted,
                rx1_conducted_reference=args.confirm_rx1_conducted_reference,
                one_hot_static_control=args.confirm_one_hot_static_control,
                single_driven_input=args.confirm_single_driven_input,
                other_seven_terminated=args.confirm_other_seven_terminated,
                no_simultaneous_eight_way_feed=(args.confirm_no_simultaneous_eight_way_feed),
                attribution_repeats_no_cable_movement=(
                    args.confirm_attribution_repeats_no_cable_movement
                ),
            )
            _execute_stage(
                manifest,
                manifest_path,
                envelope=envelope,
                plan_path=plan_path,
                confirmation=confirmation,
                capture_root=Path(str(contract["storage"]["run_capture_root"])),
            )
        except (OneHotLadderError, leakage.ConditionCaptureFailure, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "run_id": contract["run_id"],
                    "topology_identity": contract["topology_identity"],
                    "driven_input": contract["driven_input"],
                    "status": manifest["status"],
                    "manifest": str(manifest_path),
                    "summary": manifest["summary"],
                    "one_hot_run_summary": manifest["one_hot_run_summary"],
                    "selector_calibration_claim": False,
                    "causal_attribution_claim": False,
                    "operational_switching_claim": False,
                },
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
