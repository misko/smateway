"""Fail-closed evidence model for exploratory Hexcal RX-gain qualification."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from smateway.capture_admission import AdcHeadroomMonitor
from smateway.hexcal import (
    EXPECTED_STATE_NAMES,
    MINIMUM_PHASE_GAUGE_RESULTANT,
    HexcalProfile,
    analyze_hexcal_samples,
    audit_continuity_metadata,
    canonical_json_sha256,
    evaluate_hexcal_quality,
    load_ci16_channel,
    sha256_path,
    validate_tx1_rf_readback_evidence,
)

QUALIFICATION_KIND = "hexcal_v1_exploratory_rx_gain_qualification"
STIMULUS_PROTOCOL_ID = "hexcal-v2.1-2g4-stimulus"
STIMULUS_QUALIFICATION_KIND = "hexcal_v2_1_2g4_tx_stimulus_qualification"
QUALIFICATION_SOURCE_FILES = (
    "profiles/hexcal-v1/control_profile.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2-2g4-stimulus.json",
    "docs/hexray_tx_in_middle_calibration/data/hexcal-v2.1-2g4-stimulus.json",
    "scripts/qualify_hexcal_rx_gain.py",
    "src/smateway/capture_admission.py",
    "src/smateway/capture_continuity.py",
    "src/smateway/hexcal.py",
    "src/smateway/hexcal_gain.py",
    "src/smateway/rf_policy.py",
    "pyproject.toml",
    "uv.lock",
)
SAMPLE_RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000
TONE_OFFSET_HZ = 100_000
SAMPLES_PER_FRAME = 100_000
FRAME_COUNT = 3
TOTAL_SAMPLES = SAMPLES_PER_FRAME * FRAME_COUNT
KERNEL_BUFFERS = 8
CONDITION_TIMEOUT_S = 30
DEFAULT_GAIN_CANDIDATES_DB = tuple(range(63))
DEFAULT_STIMULUS_TX_GAINS_DB = (-35.0, -30.0, -25.0, -20.0, -15.0, -10.0)
STIMULUS_CENTER_FREQUENCIES_HZ = (
    2_400_000_000,
    2_423_000_000,
    2_440_000_000,
    2_472_000_000,
    2_483_000_000,
)
STIMULUS_FIXED_RECEIVER_GAIN_DB = 20
MINIMUM_COMPLETE_CYCLES = 150
MINIMUM_DECODED_FRACTION = 0.98
MINIMUM_MARKER_CONTRAST_DB = 20.0
MINIMUM_STATE_SNR_DB = 20.0
MINIMUM_STATE_COHERENCE = 0.995
MAXIMUM_STATE_PHASE_STD_DEG = 6.0
MINIMUM_NULL_ISOLATION_DB = 20.0
MAXIMUM_PEAK_COMPONENT_COUNTS = 1_300.0
PINNED_PYTHON = "/home/pi/pluto-plus-utils/.venv/bin/python"
PINNED_PYTHON_PREFIX = "/home/pi/pluto-plus-utils/.venv"
EXPECTED_SMATEWAY_SOURCE_ROOT = "/home/pi/smateway/src"
EXPECTED_HEXCAL_GAIN_MODULE = "/home/pi/smateway/src/smateway/hexcal_gain.py"
ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class HexcalGainQualification:
    """Validated passed ledger that fixes one gain for the accepted run."""

    path: Path
    file_sha256: str
    qualification_id: str
    board_id: str
    serial: str
    uri: str
    source_commit: str
    profile_file_sha256: str
    profile_contract_sha256: str
    firmware_evidence_sha256: str
    pluto_plus_utils_source_attestation_sha256: str
    center_frequencies_hz: tuple[int, ...]
    candidate_gains_db: tuple[int, ...]
    tested_gains_db: tuple[int, ...]
    selected_receiver_gain_db: int
    completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "qualification_id": self.qualification_id,
            "board_id": self.board_id,
            "serial": self.serial,
            "uri": self.uri,
            "source_commit": self.source_commit,
            "profile_file_sha256": self.profile_file_sha256,
            "profile_contract_sha256": self.profile_contract_sha256,
            "firmware_evidence_sha256": self.firmware_evidence_sha256,
            "pluto_plus_utils_source_attestation_sha256": (
                self.pluto_plus_utils_source_attestation_sha256
            ),
            "center_frequencies_hz": list(self.center_frequencies_hz),
            "candidate_gains_db": list(self.candidate_gains_db),
            "tested_gains_db": list(self.tested_gains_db),
            "selected_receiver_gain_db": self.selected_receiver_gain_db,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class HexcalStimulusQualification:
    """Validated ledger that freezes the lowest sufficient TX1 stimulus."""

    path: Path
    file_sha256: str
    qualification_id: str
    board_id: str
    serial: str
    uri: str
    source_commit: str
    profile_file_sha256: str
    profile_contract_sha256: str
    firmware_evidence_sha256: str
    pluto_plus_utils_source_attestation_sha256: str
    center_frequencies_hz: tuple[int, ...]
    fixed_receiver_gain_db: int
    candidate_tx_hardware_gains_db: tuple[float, ...]
    tested_tx_hardware_gains_db: tuple[float, ...]
    selected_tx_hardware_gain_db: float
    dds_scale: float
    completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "qualification_id": self.qualification_id,
            "board_id": self.board_id,
            "serial": self.serial,
            "uri": self.uri,
            "source_commit": self.source_commit,
            "profile_file_sha256": self.profile_file_sha256,
            "profile_contract_sha256": self.profile_contract_sha256,
            "firmware_evidence_sha256": self.firmware_evidence_sha256,
            "pluto_plus_utils_source_attestation_sha256": (
                self.pluto_plus_utils_source_attestation_sha256
            ),
            "center_frequencies_hz": list(self.center_frequencies_hz),
            "fixed_receiver_gain_db": self.fixed_receiver_gain_db,
            "candidate_tx_hardware_gains_db": list(self.candidate_tx_hardware_gains_db),
            "tested_tx_hardware_gains_db": list(self.tested_tx_hardware_gains_db),
            "selected_tx_hardware_gain_db": self.selected_tx_hardware_gain_db,
            "dds_scale": self.dds_scale,
            "completed_at": self.completed_at,
        }


def qualification_thresholds() -> dict[str, float | int]:
    """Return the reviewed exploratory admission thresholds."""

    return {
        "minimum_complete_cycles": MINIMUM_COMPLETE_CYCLES,
        "minimum_decoded_fraction": MINIMUM_DECODED_FRACTION,
        "minimum_marker_contrast_db": MINIMUM_MARKER_CONTRAST_DB,
        "minimum_state_snr_db": MINIMUM_STATE_SNR_DB,
        "minimum_state_coherence": MINIMUM_STATE_COHERENCE,
        "maximum_state_phase_std_deg": MAXIMUM_STATE_PHASE_STD_DEG,
        "minimum_null_isolation_db": MINIMUM_NULL_ISOLATION_DB,
        "minimum_phase_gauge_resultant": MINIMUM_PHASE_GAUGE_RESULTANT,
        "maximum_peak_component_counts": MAXIMUM_PEAK_COMPONENT_COUNTS,
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _integer_list(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty integer list")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{label} must be a non-empty integer list")
    return tuple(value)


def _number_list(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty numeric list")
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError(f"{label} must be a non-empty finite numeric list")
    return tuple(float(item) for item in value)


def gain_headroom_passes(value: object) -> bool:
    """Apply exact dual-RX admission and the conservative 1,300-count peak cap."""

    if not isinstance(value, Mapping) or value.get("passed") is not True:
        return False
    receivers = value.get("receivers")
    if not isinstance(receivers, (list, tuple)) or len(receivers) != 2:
        return False
    observed: set[int] = set()
    for raw_receiver in receivers:
        if not isinstance(raw_receiver, Mapping):
            return False
        receiver = raw_receiver.get("receiver")
        peak = raw_receiver.get("peak_abs_component_counts")
        if (
            receiver not in (0, 1)
            or receiver in observed
            or raw_receiver.get("passed") is not True
            or raw_receiver.get("sample_count") != TOTAL_SAMPLES
            or raw_receiver.get("clipped_sample_count") != 0
            or isinstance(peak, bool)
            or not isinstance(peak, (int, float))
            or not math.isfinite(float(peak))
            or float(peak) > MAXIMUM_PEAK_COMPONENT_COUNTS
        ):
            return False
        observed.add(int(receiver))
    return observed == {0, 1}


def _mute_passed(value: object, *, serial: str, purpose: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == purpose
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _validate_artifact(value: object, *, ledger_root: Path) -> tuple[str, Path, Path]:
    evidence = _mapping(value, "condition artifact evidence")
    artifact_id = evidence.get("artifact_id")
    if not isinstance(artifact_id, str) or ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise ValueError("gain-qualification artifact ID is malformed")
    artifact_root = Path(str(evidence.get("path", ""))).expanduser().resolve(strict=True)
    allowed_root = (ledger_root / "exploratory-artifacts").resolve(strict=True)
    if (
        not artifact_root.is_dir()
        or not artifact_root.is_relative_to(allowed_root)
        or artifact_root.name != artifact_id
    ):
        raise ValueError("gain-qualification artifact escaped its exploratory root")
    paths: dict[str, Path] = {}
    for prefix in ("data", "metadata"):
        path = Path(str(evidence.get(f"{prefix}_path", ""))).expanduser().resolve(strict=True)
        if (
            not path.is_file()
            or path.parent != artifact_root
            or path.name != f"{artifact_id}.sigmf-{prefix if prefix == 'data' else 'meta'}"
        ):
            raise ValueError(f"gain-qualification {prefix} path is malformed")
        if evidence.get(f"{prefix}_sha256") != sha256_path(path):
            raise ValueError(f"gain-qualification {prefix} SHA-256 changed")
        if evidence.get(f"{prefix}_size_bytes") != path.stat().st_size:
            raise ValueError(f"gain-qualification {prefix} size changed")
        if prefix == "data" and path.stat().st_size != TOTAL_SAMPLES * 2 * 2 * 2:
            raise ValueError("gain-qualification data does not contain exact dual CI16 samples")
        paths[prefix] = path
    return artifact_id, paths["data"], paths["metadata"]


def _condition_pass_result(
    analysis: Mapping[str, Any], headroom: Mapping[str, Any]
) -> tuple[bool, list[str], dict[str, Any]]:
    quality = evaluate_hexcal_quality(
        analysis,
        headroom_passed=headroom.get("passed") is True,
        minimum_complete_cycles=MINIMUM_COMPLETE_CYCLES,
        minimum_decoded_fraction=MINIMUM_DECODED_FRACTION,
        minimum_marker_contrast_db=MINIMUM_MARKER_CONTRAST_DB,
        minimum_state_snr_db=MINIMUM_STATE_SNR_DB,
        minimum_state_coherence=MINIMUM_STATE_COHERENCE,
        maximum_state_phase_std_deg=MAXIMUM_STATE_PHASE_STD_DEG,
        minimum_null_isolation_db=MINIMUM_NULL_ISOLATION_DB,
        minimum_phase_gauge_resultant=MINIMUM_PHASE_GAUGE_RESULTANT,
    )
    reasons = list(quality["global_rejection_reasons"])
    for state in quality["states"]:
        reasons.extend(
            f"{str(state['name']).lower()}_{reason}" for reason in state["rejection_reasons"]
        )
    if not gain_headroom_passes(headroom):
        reasons.append("conservative_dual_rx_headroom_failed")
    return not reasons, reasons, quality


def replay_hexcal_gain_artifact(
    artifact_evidence: Mapping[str, Any],
    *,
    ledger_root: Path,
    profile: HexcalProfile,
    expected_serial: str,
    expected_uri: str,
    expected_center_frequency_hz: int,
    expected_receiver_gain_db: int,
    tone_offset_hz: float,
) -> dict[str, Any]:
    """Replay continuity, dual-RX headroom and six-state analysis from raw files."""

    artifact_id, data_file, metadata_file = _validate_artifact(
        artifact_evidence, ledger_root=ledger_root
    )
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot replay gain-qualification metadata: {error}") from error
    if not isinstance(metadata, Mapping):
        raise ValueError("gain-qualification metadata root must be an object")
    global_metadata = _mapping(metadata.get("global"), "gain artifact global metadata")
    radio = _mapping(global_metadata.get("pluto:radio"), "gain artifact radio metadata")
    capture_metadata = _mapping(metadata.get("pluto:capture"), "gain artifact capture metadata")
    initial_settings = _mapping(
        capture_metadata.get("initial_settings"), "gain artifact initial settings"
    )
    if (
        global_metadata.get("pluto:artifact_id") != artifact_id
        or global_metadata.get("core:datatype") != "ci16_le"
        or global_metadata.get("core:sample_rate") != SAMPLE_RATE_HZ
        or global_metadata.get("core:num_channels") != 2
        or radio.get("serial") != expected_serial
        or radio.get("uri") != expected_uri
        or capture_metadata.get("sample_count") != TOTAL_SAMPLES
        or capture_metadata.get("receiver_count") != 2
        or initial_settings.get("center_frequency_hz") != expected_center_frequency_hz
        or initial_settings.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or initial_settings.get("bandwidth_hz") != BANDWIDTH_HZ
        or initial_settings.get("gain_mode") != "manual"
        or initial_settings.get("gain_db") != expected_receiver_gain_db
        or initial_settings.get("channels") != [0, 1]
    ):
        raise ValueError("gain-qualification SigMF identity/settings differ from condition")
    continuity = audit_continuity_metadata(
        metadata,
        expected_total_samples=TOTAL_SAMPLES,
        expected_samples_per_block=SAMPLES_PER_FRAME,
    )
    rx1 = load_ci16_channel(
        data_file,
        sample_count=TOTAL_SAMPLES,
        receiver_count=2,
        channel=0,
    )
    rx2 = load_ci16_channel(
        data_file,
        sample_count=TOTAL_SAMPLES,
        receiver_count=2,
        channel=1,
    )
    headroom_monitor = AdcHeadroomMonitor(receiver_count=2)
    headroom_monitor.observe(np.stack((rx1, rx2)))
    headroom = json.loads(json.dumps(asdict(headroom_monitor.result()), allow_nan=False))
    analysis: dict[str, Any] | None = None
    analysis_error: str | None = None
    try:
        analysis = analyze_hexcal_samples(
            rx2,
            sample_rate_hz=SAMPLE_RATE_HZ,
            tone_offset_hz=tone_offset_hz,
            profile=profile,
            continuity_verified=True,
        )
        passed, rejection_reasons, quality = _condition_pass_result(analysis, headroom)
    except (RuntimeError, ValueError) as error:
        analysis_error = f"{type(error).__name__}: {error}"
        passed = False
        rejection_reasons = ["six_state_analysis_failed"]
        quality = None
    states = [] if analysis is None else list(analysis["states"])
    result = {
        "passed": passed,
        "tone_offset_hz_from_live_readback": tone_offset_hz,
        "rejection_reasons": rejection_reasons,
        "continuity_audit": continuity,
        "adc_headroom_admission": headroom,
        "analysis_error": analysis_error,
        "alignment": {"contrast_db": 0.0} if analysis is None else dict(analysis["alignment"]),
        "valid_cycle_count": 0 if analysis is None else analysis["valid_cycle_count"],
        "decoded_cycle_fraction": (0.0 if analysis is None else analysis["decoded_cycle_fraction"]),
        "all_six_states_observed": (
            [state.get("name") for state in states] == list(EXPECTED_STATE_NAMES)
        ),
        "state_metrics": [
            {
                "name": state["name"],
                "pilot_snr_db": state["pilot_snr_db"],
                "null_isolation_db": state["null_isolation_db"],
                "cycle_coherence": state["cycle_coherence"],
                "cycle_phase_std_deg": state["cycle_phase_std_deg"],
            }
            for state in states
        ],
        "quality_gate": quality,
        "analysis": analysis,
    }
    # Enforce a finite canonical JSON representation before it can be persisted.
    canonical = json.loads(json.dumps(result, sort_keys=True, allow_nan=False))
    if not isinstance(canonical, dict):
        raise RuntimeError("canonical gain replay did not produce an object")
    return canonical


def _record_passes(
    record: Mapping[str, Any],
    *,
    serial: str,
    uri: str,
    tx_hardware_gain_db: float,
    dds_scale: float,
    ledger_root: Path,
    profile: HexcalProfile,
) -> bool:
    if record.get("status") != "complete":
        raise ValueError("gain-qualification condition is not complete")
    if not _mute_passed(record.get("post_mute"), serial=serial, purpose="post_condition"):
        raise ValueError("gain-qualification condition lacks exact post mute")
    artifact = _mapping(record.get("artifact_evidence"), "condition artifact evidence")
    rf_readback = _mapping(record.get("rf_readback_evidence"), "condition RF readback")
    validate_tx1_rf_readback_evidence(
        rf_readback,
        planned_kernel_buffers=KERNEL_BUFFERS,
        planned_tx_gain_db=tx_hardware_gain_db,
        planned_dds_scale=dds_scale,
        planned_tone_hz=TONE_OFFSET_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )
    requested_gain = record.get("receiver_gain_db")
    if isinstance(requested_gain, bool) or not isinstance(requested_gain, int):
        raise ValueError("qualification requested RX gain is malformed")
    hold_evidence = _mapping(record.get("rx_hold_evidence"), "RX HOLD evidence")
    if (
        hold_evidence.get("schema") != 1
        or hold_evidence.get("mode") != "tandem_hold"
        or hold_evidence.get("channels") != [0, 1]
        or hold_evidence.get("requested_gain_db") != requested_gain
        or hold_evidence.get("verified_tolerance_db") != 0.25
        or hold_evidence.get("provenance")
        != "pinned_helper_verified_each_channel_within_requested_gain_tolerance"
    ):
        raise ValueError("qualification RX HOLD helper evidence differs from the plan")
    readback_frequencies = rf_readback.get("dds_frequency_readback_hz")
    assert isinstance(readback_frequencies, list)
    tone_offset_hz = (
        abs(float(readback_frequencies[0])) + abs(float(readback_frequencies[2]))
    ) / 2.0
    replayed = replay_hexcal_gain_artifact(
        artifact,
        ledger_root=ledger_root,
        profile=profile,
        expected_serial=serial,
        expected_uri=uri,
        expected_center_frequency_hz=int(record["center_frequency_hz"]),
        expected_receiver_gain_db=requested_gain,
        tone_offset_hz=tone_offset_hz,
    )
    persisted_derived = _mapping(
        record.get("replayed_artifact_analysis"), "persisted artifact replay"
    )
    if dict(persisted_derived) != replayed:
        raise ValueError("gain-qualification derived evidence differs from raw replay")
    live_headroom = record.get("live_adc_headroom_admission")
    observed = bool(replayed["passed"]) and gain_headroom_passes(live_headroom)
    persisted_pass = record.get("passed")
    if not isinstance(persisted_pass, bool) or persisted_pass != observed:
        raise ValueError("gain-qualification persisted pass result is not reproducible")
    return bool(observed)


def load_hexcal_gain_qualification(
    path: Path,
    *,
    expected_board_id: str,
    expected_serial: str,
    expected_uri: str,
    expected_source_commit: str,
    expected_source_attestation: Mapping[str, Any],
    expected_profile: HexcalProfile,
    expected_firmware_evidence_sha256: str,
    expected_pluto_plus_utils_source_attestation_sha256: str,
    expected_center_frequencies_hz: Sequence[int],
    expected_tx_hardware_gain_db: float,
    expected_dds_scale: float,
) -> HexcalGainQualification:
    """Load a passed ledger and independently reproduce its lowest-gain choice."""

    resolved = path.expanduser().resolve(strict=True)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load RX-gain qualification: {error}") from error
    root = _mapping(document, "gain-qualification root")
    configuration = _mapping(root.get("configuration"), "gain-qualification configuration")
    if (
        root.get("schema") != 1
        or root.get("qualification_kind") != QUALIFICATION_KIND
        or root.get("status") != "passed"
    ):
        raise ValueError("RX-gain qualification is not a passed supported ledger")
    qualification_id = root.get("qualification_id")
    completed_at = root.get("completed_at")
    if not isinstance(qualification_id, str) or not qualification_id:
        raise ValueError("RX-gain qualification ID is malformed")
    if not isinstance(completed_at, str) or not completed_at:
        raise ValueError("RX-gain qualification completion time is malformed")
    expected_frequencies = tuple(int(value) for value in expected_center_frequencies_hz)
    frequencies = _integer_list(
        configuration.get("center_frequencies_hz"), "qualification frequencies"
    )
    candidates = _integer_list(
        configuration.get("candidate_gains_db"), "qualification gain candidates"
    )
    if (
        candidates[0] != 0
        or any(
            second != first + 1 for first, second in zip(candidates, candidates[1:], strict=False)
        )
        or any(not 0 <= value <= 62 for value in candidates)
    ):
        raise ValueError("RX-gain candidates must be a contiguous ascending prefix from 0 dB")
    dependency = _mapping(
        configuration.get("pluto_plus_utils_source_attestation"),
        "qualification dependency attestation",
    )
    dependency_sha = configuration.get("pluto_plus_utils_source_attestation_sha256")
    if dependency_sha != canonical_json_sha256(dependency):
        raise ValueError("qualification dependency attestation SHA-256 is inconsistent")
    source_attestation = _mapping(
        configuration.get("source_attestation"),
        "qualification source attestation",
    )
    python_runtime = _mapping(configuration.get("python_runtime"), "qualification Python runtime")
    firmware_evidence = _mapping(
        configuration.get("firmware_evidence"), "qualification firmware evidence"
    )
    if (
        configuration.get("board_id") != expected_board_id
        or configuration.get("serial") != expected_serial
        or configuration.get("uri") != expected_uri
        or configuration.get("source_commit") != expected_source_commit
        or dict(source_attestation) != dict(expected_source_attestation)
        or configuration.get("profile_file_sha256") != expected_profile.file_sha256
        or configuration.get("profile_contract_sha256") != expected_profile.contract_sha256
        or configuration.get("firmware_evidence_sha256") != expected_firmware_evidence_sha256
        or firmware_evidence.get("file_sha256") != expected_firmware_evidence_sha256
        or firmware_evidence.get("board_id") != expected_board_id
        or firmware_evidence.get("source_commit") != expected_source_commit
        or firmware_evidence.get("profile_file_sha256") != expected_profile.file_sha256
        or firmware_evidence.get("profile_contract_sha256") != expected_profile.contract_sha256
        or dependency_sha != expected_pluto_plus_utils_source_attestation_sha256
        or frequencies != expected_frequencies
        or configuration.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or configuration.get("bandwidth_hz") != BANDWIDTH_HZ
        or configuration.get("samples_per_frame") != SAMPLES_PER_FRAME
        or configuration.get("frame_count") != FRAME_COUNT
        or configuration.get("kernel_buffers") != KERNEL_BUFFERS
        or configuration.get("condition_timeout_s") != CONDITION_TIMEOUT_S
        or python_runtime.get("requested_executable") != PINNED_PYTHON
        or python_runtime.get("sys_executable") != PINNED_PYTHON
        or python_runtime.get("sys_prefix") != PINNED_PYTHON_PREFIX
        or python_runtime.get("smateway_source_root") != EXPECTED_SMATEWAY_SOURCE_ROOT
        or python_runtime.get("hexcal_gain_module_path") != EXPECTED_HEXCAL_GAIN_MODULE
        or python_runtime.get("auto_reexec_before_pluto_import") is not True
        or configuration.get("tx_channel") != 0
        or configuration.get("tx_port") != "TX1"
        or configuration.get("tx2_policy") != "muted_-80dB_and_zero_DDS"
        or configuration.get("tx_hardware_gain_db") != expected_tx_hardware_gain_db
        or configuration.get("dds_scale") != expected_dds_scale
        or configuration.get("thresholds") != qualification_thresholds()
    ):
        raise ValueError("RX-gain qualification identity or exact plan differs")

    expected_plan = [
        {
            "gain_index": gain_index,
            "frequency_index": frequency_index,
            "receiver_gain_db": gain,
            "center_frequency_hz": frequency,
            "tx_channel": 0,
            "tx_port": "TX1",
        }
        for gain_index, gain in enumerate(candidates)
        for frequency_index, frequency in enumerate(frequencies)
    ]
    if root.get("plan") != expected_plan:
        raise ValueError("RX-gain qualification pre-RF execution plan changed")

    raw_records = root.get("conditions")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("RX-gain qualification has no condition evidence")
    records: dict[tuple[int, int], Mapping[str, Any]] = {}
    for raw_record in raw_records:
        record = _mapping(raw_record, "gain-qualification condition")
        gain = record.get("receiver_gain_db")
        frequency = record.get("center_frequency_hz")
        if (
            isinstance(gain, bool)
            or not isinstance(gain, int)
            or isinstance(frequency, bool)
            or not isinstance(frequency, int)
            or gain not in candidates
            or frequency not in frequencies
            or (gain, frequency) in records
        ):
            raise ValueError("gain-qualification condition identity is malformed or duplicated")
        records[(gain, frequency)] = record

    tested = _integer_list(root.get("tested_gains_db"), "tested gain list")
    selected = root.get("selected_receiver_gain_db")
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or selected not in candidates
        or tested != candidates[: candidates.index(selected) + 1]
    ):
        raise ValueError("RX-gain selection is not a tested ascending prefix")
    expected_keys = {(gain, frequency) for gain in tested for frequency in frequencies}
    if set(records) != expected_keys:
        raise ValueError("RX-gain qualification matrix is incomplete or contains extra rows")

    per_gain_passed: dict[int, bool] = {}
    ledger_root = resolved.parent
    for gain in tested:
        outcomes = [
            _record_passes(
                records[(gain, frequency)],
                serial=expected_serial,
                uri=expected_uri,
                tx_hardware_gain_db=expected_tx_hardware_gain_db,
                dds_scale=expected_dds_scale,
                ledger_root=ledger_root,
                profile=expected_profile,
            )
            for frequency in frequencies
        ]
        per_gain_passed[gain] = all(outcomes)
    reproduced = next((gain for gain in tested if per_gain_passed[gain]), None)
    if reproduced != selected or any(per_gain_passed[gain] for gain in tested[:-1]):
        raise ValueError("RX-gain ledger does not prove the lowest sufficient tested gain")
    if root.get("selection_policy") != "lowest_ascending_gain_passing_every_frequency_and_state":
        raise ValueError("RX-gain selection policy is unsupported")
    if root.get("calibration_gain_is_fixed") is not True:
        raise ValueError("RX-gain ledger does not freeze gain for the calibration")
    if not _mute_passed(root.get("preflight_mute"), serial=expected_serial, purpose="preflight"):
        raise ValueError("RX-gain qualification lacks an exact preflight mute")
    if not _mute_passed(root.get("final_mute"), serial=expected_serial, purpose="final"):
        raise ValueError("RX-gain qualification lacks an exact final mute")

    return HexcalGainQualification(
        path=resolved,
        file_sha256=sha256_path(resolved),
        qualification_id=qualification_id,
        board_id=expected_board_id,
        serial=expected_serial,
        uri=expected_uri,
        source_commit=expected_source_commit,
        profile_file_sha256=expected_profile.file_sha256,
        profile_contract_sha256=expected_profile.contract_sha256,
        firmware_evidence_sha256=expected_firmware_evidence_sha256,
        pluto_plus_utils_source_attestation_sha256=(
            expected_pluto_plus_utils_source_attestation_sha256
        ),
        center_frequencies_hz=frequencies,
        candidate_gains_db=candidates,
        tested_gains_db=tested,
        selected_receiver_gain_db=selected,
        completed_at=completed_at,
    )


def load_hexcal_stimulus_qualification(
    path: Path,
    *,
    expected_board_id: str,
    expected_serial: str,
    expected_uri: str,
    expected_source_commit: str,
    expected_source_attestation: Mapping[str, Any],
    expected_profile: HexcalProfile,
    expected_firmware_evidence_sha256: str,
    expected_pluto_plus_utils_source_attestation_sha256: str,
    expected_center_frequencies_hz: Sequence[int] = STIMULUS_CENTER_FREQUENCIES_HZ,
    expected_receiver_gain_db: int = STIMULUS_FIXED_RECEIVER_GAIN_DB,
    expected_candidate_tx_hardware_gains_db: Sequence[float] = (DEFAULT_STIMULUS_TX_GAINS_DB),
    expected_dds_scale: float = 0.125,
) -> HexcalStimulusQualification:
    """Replay and validate the first all-band passing TX1 stimulus ledger."""

    resolved = path.expanduser().resolve(strict=True)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load TX-stimulus qualification: {error}") from error
    root = _mapping(document, "TX-stimulus qualification root")
    configuration = _mapping(root.get("configuration"), "TX-stimulus qualification configuration")
    if (
        root.get("schema") != 1
        or root.get("protocol_id") != STIMULUS_PROTOCOL_ID
        or root.get("qualification_kind") != STIMULUS_QUALIFICATION_KIND
        or root.get("status") != "passed"
    ):
        raise ValueError("TX-stimulus qualification is not a passed supported ledger")
    qualification_id = root.get("qualification_id")
    completed_at = root.get("completed_at")
    if not isinstance(qualification_id, str) or not qualification_id:
        raise ValueError("TX-stimulus qualification ID is malformed")
    if not isinstance(completed_at, str) or not completed_at:
        raise ValueError("TX-stimulus qualification completion time is malformed")

    expected_frequencies = tuple(int(value) for value in expected_center_frequencies_hz)
    expected_candidates = tuple(float(value) for value in expected_candidate_tx_hardware_gains_db)
    frequencies = _integer_list(configuration.get("center_frequencies_hz"), "stimulus frequencies")
    candidates = _number_list(
        configuration.get("candidate_tx_hardware_gains_db"),
        "stimulus TX-gain candidates",
    )
    if (
        frequencies != expected_frequencies
        or candidates != expected_candidates
        or len(set(frequencies)) != len(frequencies)
        or len(set(candidates)) != len(candidates)
        or any(second <= first for first, second in zip(candidates, candidates[1:], strict=False))
        or any(not -80.0 <= value <= 0.0 for value in candidates)
    ):
        raise ValueError("TX-stimulus frequencies or ascending candidate ladder changed")
    if expected_receiver_gain_db != STIMULUS_FIXED_RECEIVER_GAIN_DB:
        raise ValueError("TX-stimulus qualification requires the frozen 20 dB RX gain")

    dependency = _mapping(
        configuration.get("pluto_plus_utils_source_attestation"),
        "stimulus dependency attestation",
    )
    dependency_sha = configuration.get("pluto_plus_utils_source_attestation_sha256")
    source_attestation = _mapping(
        configuration.get("source_attestation"),
        "stimulus source attestation",
    )
    python_runtime = _mapping(configuration.get("python_runtime"), "stimulus Python runtime")
    firmware_evidence = _mapping(
        configuration.get("firmware_evidence"), "stimulus firmware evidence"
    )
    if dependency_sha != canonical_json_sha256(dependency):
        raise ValueError("stimulus dependency attestation SHA-256 is inconsistent")
    if (
        configuration.get("board_id") != expected_board_id
        or configuration.get("serial") != expected_serial
        or configuration.get("uri") != expected_uri
        or configuration.get("source_commit") != expected_source_commit
        or dict(source_attestation) != dict(expected_source_attestation)
        or configuration.get("profile_file_sha256") != expected_profile.file_sha256
        or configuration.get("profile_contract_sha256") != expected_profile.contract_sha256
        or configuration.get("firmware_evidence_sha256") != expected_firmware_evidence_sha256
        or firmware_evidence.get("file_sha256") != expected_firmware_evidence_sha256
        or firmware_evidence.get("board_id") != expected_board_id
        or firmware_evidence.get("source_commit") != expected_source_commit
        or firmware_evidence.get("profile_file_sha256") != expected_profile.file_sha256
        or firmware_evidence.get("profile_contract_sha256") != expected_profile.contract_sha256
        or dependency_sha != expected_pluto_plus_utils_source_attestation_sha256
        or configuration.get("fixed_receiver_gain_db") != expected_receiver_gain_db
        or configuration.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or configuration.get("bandwidth_hz") != BANDWIDTH_HZ
        or configuration.get("samples_per_frame") != SAMPLES_PER_FRAME
        or configuration.get("frame_count") != FRAME_COUNT
        or configuration.get("kernel_buffers") != KERNEL_BUFFERS
        or configuration.get("condition_timeout_s") != CONDITION_TIMEOUT_S
        or python_runtime.get("requested_executable") != PINNED_PYTHON
        or python_runtime.get("sys_executable") != PINNED_PYTHON
        or python_runtime.get("sys_prefix") != PINNED_PYTHON_PREFIX
        or python_runtime.get("smateway_source_root") != EXPECTED_SMATEWAY_SOURCE_ROOT
        or python_runtime.get("hexcal_gain_module_path") != EXPECTED_HEXCAL_GAIN_MODULE
        or python_runtime.get("auto_reexec_before_pluto_import") is not True
        or configuration.get("tx_channel") != 0
        or configuration.get("tx_port") != "TX1"
        or configuration.get("tx2_policy") != "muted_-80dB_and_zero_DDS"
        or configuration.get("dds_scale") != expected_dds_scale
        or configuration.get("tone_offset_hz") != TONE_OFFSET_HZ
        or configuration.get("thresholds") != qualification_thresholds()
    ):
        raise ValueError("TX-stimulus qualification identity or exact plan differs")

    expected_plan = [
        {
            "tx_gain_index": gain_index,
            "frequency_index": frequency_index,
            "receiver_gain_db": expected_receiver_gain_db,
            "tx_hardware_gain_db": gain,
            "center_frequency_hz": frequency,
            "tx_channel": 0,
            "tx_port": "TX1",
        }
        for gain_index, gain in enumerate(candidates)
        for frequency_index, frequency in enumerate(frequencies)
    ]
    if root.get("plan") != expected_plan:
        raise ValueError("TX-stimulus pre-RF execution plan changed")

    raw_records = root.get("conditions")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("TX-stimulus qualification has no condition evidence")
    records: dict[tuple[float, int], Mapping[str, Any]] = {}
    for raw_record in raw_records:
        record = _mapping(raw_record, "TX-stimulus condition")
        raw_gain = record.get("tx_hardware_gain_db")
        frequency = record.get("center_frequency_hz")
        receiver_gain = record.get("receiver_gain_db")
        if (
            isinstance(raw_gain, bool)
            or not isinstance(raw_gain, (int, float))
            or isinstance(frequency, bool)
            or not isinstance(frequency, int)
            or receiver_gain != expected_receiver_gain_db
        ):
            raise ValueError("TX-stimulus condition identity is malformed")
        gain = float(raw_gain)
        key = (gain, frequency)
        if gain not in candidates or frequency not in frequencies or key in records:
            raise ValueError("TX-stimulus condition is outside or duplicates the plan")
        records[key] = record

    tested = _number_list(root.get("tested_tx_hardware_gains_db"), "tested stimulus gain list")
    raw_selected = root.get("selected_tx_hardware_gain_db")
    if isinstance(raw_selected, bool) or not isinstance(raw_selected, (int, float)):
        raise ValueError("selected TX-stimulus gain is malformed")
    selected = float(raw_selected)
    if selected not in candidates or tested != candidates[: candidates.index(selected) + 1]:
        raise ValueError("TX-stimulus selection is not a tested ascending prefix")
    expected_keys = {(gain, frequency) for gain in tested for frequency in frequencies}
    if set(records) != expected_keys:
        raise ValueError("TX-stimulus matrix is incomplete or contains extra rows")

    per_gain_passed: dict[float, bool] = {}
    ledger_root = resolved.parent
    for gain in tested:
        outcomes: list[bool] = []
        for frequency in frequencies:
            record = records[(gain, frequency)]
            if not gain_headroom_passes(record.get("live_adc_headroom_admission")):
                raise ValueError("passed TX-stimulus ledger crossed a headroom stop boundary")
            outcomes.append(
                _record_passes(
                    record,
                    serial=expected_serial,
                    uri=expected_uri,
                    tx_hardware_gain_db=gain,
                    dds_scale=expected_dds_scale,
                    ledger_root=ledger_root,
                    profile=expected_profile,
                )
            )
        per_gain_passed[gain] = all(outcomes)
    reproduced = next((gain for gain in tested if per_gain_passed[gain]), None)
    if reproduced != selected or any(per_gain_passed[gain] for gain in tested[:-1]):
        raise ValueError("TX-stimulus ledger does not prove the lowest sufficient level")
    if (
        root.get("selection_policy")
        != "lowest_power_ascending_tx_gain_passing_every_frequency_and_state"
        or root.get("receiver_gain_is_fixed") is not True
        or root.get("selected_stimulus_is_frozen") is not True
    ):
        raise ValueError("TX-stimulus selection/freeze policy is unsupported")
    if not _mute_passed(root.get("preflight_mute"), serial=expected_serial, purpose="preflight"):
        raise ValueError("TX-stimulus qualification lacks an exact preflight mute")
    if not _mute_passed(root.get("final_mute"), serial=expected_serial, purpose="final"):
        raise ValueError("TX-stimulus qualification lacks an exact final mute")

    return HexcalStimulusQualification(
        path=resolved,
        file_sha256=sha256_path(resolved),
        qualification_id=qualification_id,
        board_id=expected_board_id,
        serial=expected_serial,
        uri=expected_uri,
        source_commit=expected_source_commit,
        profile_file_sha256=expected_profile.file_sha256,
        profile_contract_sha256=expected_profile.contract_sha256,
        firmware_evidence_sha256=expected_firmware_evidence_sha256,
        pluto_plus_utils_source_attestation_sha256=(
            expected_pluto_plus_utils_source_attestation_sha256
        ),
        center_frequencies_hz=frequencies,
        fixed_receiver_gain_db=expected_receiver_gain_db,
        candidate_tx_hardware_gains_db=candidates,
        tested_tx_hardware_gains_db=tested,
        selected_tx_hardware_gain_db=selected,
        dds_scale=expected_dds_scale,
        completed_at=completed_at,
    )


__all__ = [
    "BANDWIDTH_HZ",
    "CONDITION_TIMEOUT_S",
    "DEFAULT_GAIN_CANDIDATES_DB",
    "DEFAULT_STIMULUS_TX_GAINS_DB",
    "FRAME_COUNT",
    "HexcalGainQualification",
    "HexcalStimulusQualification",
    "KERNEL_BUFFERS",
    "MAXIMUM_PEAK_COMPONENT_COUNTS",
    "QUALIFICATION_KIND",
    "QUALIFICATION_SOURCE_FILES",
    "SAMPLES_PER_FRAME",
    "SAMPLE_RATE_HZ",
    "STIMULUS_CENTER_FREQUENCIES_HZ",
    "STIMULUS_FIXED_RECEIVER_GAIN_DB",
    "STIMULUS_PROTOCOL_ID",
    "STIMULUS_QUALIFICATION_KIND",
    "TONE_OFFSET_HZ",
    "TOTAL_SAMPLES",
    "load_hexcal_gain_qualification",
    "load_hexcal_stimulus_qualification",
    "gain_headroom_passes",
    "qualification_thresholds",
    "replay_hexcal_gain_artifact",
]
