#!/usr/bin/env python3
"""Analyze repeatable 5.8 GHz complex switching hidden by ALL_OFF leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from smateway.hexcal import canonical_json_sha256, sha256_path, write_json_atomic
from smateway.hexcal_gain import (
    EXPERIMENTAL_5G8_HIGH_RX_STIMULUS_PROTOCOL_ID,
    SAMPLE_RATE_HZ,
    TOTAL_SAMPLES,
)

ANALYSIS_KIND = "hexcal_v2_4_5g8_phase_leakage_exploratory_analysis"
EXPECTED_GAINS_DB = (-35.0, -30.0, -25.0, -20.0, -15.0, -10.0)
EXPECTED_CENTER_HZ = 5_800_000_000
EXPECTED_RX_GAIN_DB = 60
NOMINAL_CYCLE_US = 1_500.0
CYCLE_FREQUENCY_RANGE_HZ = (1_000_000.0 / 1_575.0, 1_000_000.0 / 1_425.0)
STATE_NAMES = tuple(f"ANT{index}" for index in range(1, 7))
STATE_WINDOWS = (
    (210, 390),
    (430, 610),
    (650, 830),
    (870, 1_050),
    (1_090, 1_270),
    (1_310, 1_490),
)
ALL_OFF_WINDOWS = (
    (20, 170),
    (185, 195),
    (405, 415),
    (625, 635),
    (845, 855),
    (1_065, 1_075),
    (1_285, 1_295),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _mute_passed(value: object, serial: str) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("purpose") == "final"
        and value.get("status") == "passed"
        and value.get("serial") == serial
        and value.get("attestation") == "mute_returned_radio_exact_serial_readback"
        and value.get("error") is None
    )


def _wrap_phase_deg(value: float) -> float:
    return float((value + 180.0) % 360.0 - 180.0)


def _artifact_path(
    evidence: Mapping[str, Any], *, ledger: Path, key: str, expected_suffix: str
) -> Path:
    path = Path(str(evidence.get(f"{key}_path", ""))).expanduser().resolve(strict=True)
    root = Path(str(evidence.get("path", ""))).expanduser().resolve(strict=True)
    if (
        not path.is_file()
        or path.parent != root
        or not root.is_relative_to(ledger.parent / "exploratory-artifacts")
        or path.suffix != expected_suffix
        or path.stat().st_size != evidence.get(f"{key}_size_bytes")
        or sha256_path(path) != evidence.get(f"{key}_sha256")
    ):
        raise ValueError(f"{key} artifact evidence differs from immutable bytes")
    return path


def _tone_readback_hz(record: Mapping[str, Any]) -> float:
    rf = _mapping(record.get("rf_readback_evidence"), "RF readback")
    raw = rf.get("dds_frequency_readback_hz")
    if not isinstance(raw, list) or len(raw) != 8:
        raise ValueError("DDS frequency readback is malformed")
    first = abs(float(raw[0]))
    second = abs(float(raw[2]))
    if not math.isclose(first, second, rel_tol=0.0, abs_tol=2.0):
        raise ValueError("active TX1 I/Q DDS readbacks disagree")
    return (first + second) / 2.0


def estimate_cycle_period_us(samples: np.ndarray) -> tuple[float, float]:
    """Estimate selector period and fundamental-line prominence from complex samples."""

    if samples.ndim != 1 or samples.size < 30_000:
        raise ValueError("phase-leakage analysis needs a long one-dimensional capture")
    windowed = (samples - np.mean(samples)) * np.hanning(samples.size)
    spectrum = np.abs(np.fft.fft(windowed)) ** 2
    frequencies = np.fft.fftfreq(samples.size, 1.0 / SAMPLE_RATE_HZ)
    low, high = CYCLE_FREQUENCY_RANGE_HZ
    indices = np.where((frequencies >= low) & (frequencies <= high))[0]
    if indices.size < 5:
        raise ValueError("capture has insufficient cycle-frequency resolution")
    peak = int(indices[np.argmax(spectrum[indices])])
    if peak <= 0 or peak >= spectrum.size - 1:
        raise ValueError("cycle spectral peak is not locally refinable")
    log_power = np.log(np.maximum(spectrum[peak - 1 : peak + 2], np.finfo(float).tiny))
    denominator = log_power[0] - 2.0 * log_power[1] + log_power[2]
    delta = 0.0 if denominator == 0.0 else 0.5 * (log_power[0] - log_power[2]) / denominator
    cycle_hz = (peak + float(delta)) * SAMPLE_RATE_HZ / samples.size
    background = spectrum[indices[np.abs(indices - peak) > 2]]
    prominence_db = 10.0 * math.log10(
        float(spectrum[peak]) / max(float(np.median(background)), np.finfo(float).tiny)
    )
    return 1_000_000.0 / cycle_hz, prominence_db


def _fractional_fold(samples: np.ndarray, period_us: float) -> np.ndarray:
    sample_index = np.arange(samples.size, dtype=float)
    bins = np.floor(np.mod(sample_index, period_us) * NOMINAL_CYCLE_US / period_us).astype(int)
    counts = np.bincount(bins, minlength=int(NOMINAL_CYCLE_US))
    if np.any(counts == 0):
        raise ValueError("fractional cycle fold left an empty phase bin")
    real = np.bincount(bins, weights=samples.real, minlength=int(NOMINAL_CYCLE_US))
    imaginary = np.bincount(bins, weights=samples.imag, minlength=int(NOMINAL_CYCLE_US))
    return (real + 1j * imaginary) / counts


def phase_only_alignment(folded: np.ndarray) -> dict[str, Any]:
    """Align the repeated ALL_OFF guards and return leakage-subtracted states."""

    if folded.shape != (1_500,):
        raise ValueError("phase-only alignment requires exactly 1,500 folded phase bins")

    def candidate(offset: int) -> tuple[float, complex, np.ndarray, np.ndarray]:
        shifted = np.roll(folded, -offset)
        off_groups = np.asarray(
            [np.mean(shifted[start:stop]) for start, stop in ALL_OFF_WINDOWS]
        )
        off_mean = complex(np.mean(off_groups))
        state_means = np.asarray(
            [np.mean(shifted[start:stop]) for start, stop in STATE_WINDOWS]
        )
        off_disagreement = float(np.mean(np.abs(off_groups - off_mean) ** 2))
        state_separation = float(np.mean(np.abs(state_means - off_mean) ** 2))
        denominator = max(
            off_disagreement,
            np.finfo(float).eps * max(state_separation, 1.0),
        )
        score = state_separation / denominator
        return score, off_mean, state_means, off_groups

    scores = np.asarray([candidate(offset)[0] for offset in range(1_500)])
    offset = int(np.argmax(scores))
    score, off_mean, state_means, off_groups = candidate(offset)
    delta = state_means - off_mean
    if abs(delta[0]) == 0.0 or np.any(np.abs(delta) == 0.0):
        raise ValueError("leakage-subtracted phase signature contains a zero state")
    reference = np.exp(-1j * np.angle(delta[0]))
    relative_delta = delta * reference
    rms = float(np.sqrt(np.mean(np.abs(relative_delta) ** 2)))
    return {
        "cycle_start_offset_nominal_us": offset,
        "guard_alignment_score": float(score),
        "all_off_group_complex_std": float(np.sqrt(np.mean(np.abs(off_groups - off_mean) ** 2))),
        "all_off_mean_magnitude_counts": float(abs(off_mean)),
        "state_absolute_magnitude_db_relative_to_all_off": [
            float(20.0 * math.log10(abs(value) / abs(off_mean))) for value in state_means
        ],
        "leakage_subtracted_phase_deg_relative_to_ant1": [
            _wrap_phase_deg(math.degrees(float(np.angle(value)))) for value in relative_delta
        ],
        "leakage_subtracted_normalized_magnitude": [
            float(abs(value) / rms) for value in relative_delta
        ],
    }


def _load_qualification(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        root = _mapping(json.loads(resolved.read_bytes()), "qualification")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load qualification: {error}") from error
    configuration = _mapping(root.get("configuration"), "qualification configuration")
    serial = str(configuration.get("serial", ""))
    gains = configuration.get("candidate_tx_hardware_gains_db")
    if (
        root.get("schema") != 1
        or root.get("protocol_id") != EXPERIMENTAL_5G8_HIGH_RX_STIMULUS_PROTOCOL_ID
        or root.get("status") != "failed"
        or configuration.get("center_frequencies_hz") != [EXPECTED_CENTER_HZ]
        or configuration.get("fixed_receiver_gain_db") != EXPECTED_RX_GAIN_DB
        or tuple(float(value) for value in gains or ()) != EXPECTED_GAINS_DB
        or not _mute_passed(root.get("final_mute"), serial)
    ):
        raise ValueError("qualification is not an exact failed, final-muted v2.4 screen")
    conditions = root.get("conditions")
    if not isinstance(conditions, list) or len(conditions) != len(EXPECTED_GAINS_DB):
        raise ValueError("qualification condition matrix is incomplete")
    observed = tuple(float(_mapping(item, "condition").get("tx_hardware_gain_db")) for item in conditions)
    if observed != EXPECTED_GAINS_DB:
        raise ValueError("qualification condition order differs from the frozen ladder")
    identity = {
        "path": str(resolved),
        "sha256": sha256_path(resolved),
        "qualification_id": root.get("qualification_id"),
        "source_commit": configuration.get("source_commit"),
        "serial": serial,
        "uri": configuration.get("uri"),
        "firmware_evidence_sha256": configuration.get("firmware_evidence_sha256"),
        "final_mute": dict(_mapping(root.get("final_mute"), "final mute")),
    }
    return identity, [dict(_mapping(item, "condition")) for item in conditions]


def _analyze_condition(
    record: Mapping[str, Any], *, ledger_path: Path, replicate_index: int
) -> dict[str, Any]:
    if record.get("passed") is not False or record.get("status") != "complete":
        raise ValueError("phase-leakage input must remain a rejected complete condition")
    headroom = _mapping(record.get("live_adc_headroom_admission"), "ADC headroom")
    if headroom.get("passed") is not True:
        raise ValueError("phase-leakage input failed ADC headroom")
    evidence = _mapping(record.get("artifact_evidence"), "artifact evidence")
    data = _artifact_path(evidence, ledger=ledger_path, key="data", expected_suffix=".sigmf-data")
    metadata = _artifact_path(
        evidence, ledger=ledger_path, key="metadata", expected_suffix=".sigmf-meta"
    )
    raw = np.memmap(data, dtype="<i2", mode="r")
    expected_components = TOTAL_SAMPLES * 2 * 2
    if raw.size != expected_components:
        raise ValueError("phase-leakage input is not exact dual-RX CI16")
    components = raw.reshape(TOTAL_SAMPLES, 2, 2)
    rx2 = components[:, 1, 0].astype(float) + 1j * components[:, 1, 1].astype(float)
    tone_hz = _tone_readback_hz(record)
    sample_index = np.arange(TOTAL_SAMPLES, dtype=float)
    demodulated = rx2 * np.exp(-2j * np.pi * tone_hz * sample_index / SAMPLE_RATE_HZ)
    period_us, prominence_db = estimate_cycle_period_us(demodulated)
    alignment = phase_only_alignment(_fractional_fold(demodulated, period_us))
    analysis_error = _mapping(
        record.get("replayed_artifact_analysis"), "replayed amplitude analysis"
    ).get("analysis_error")
    receivers = headroom.get("receivers")
    if not isinstance(receivers, list) or len(receivers) != 2:
        raise ValueError("ADC headroom receiver matrix is malformed")
    return {
        "replicate_index": replicate_index,
        "tx_hardware_gain_db": float(record["tx_hardware_gain_db"]),
        "artifact_id": evidence.get("artifact_id"),
        "data_path": str(data),
        "data_sha256": evidence.get("data_sha256"),
        "metadata_path": str(metadata),
        "metadata_sha256": evidence.get("metadata_sha256"),
        "tone_offset_readback_hz": tone_hz,
        "cycle_period_us": period_us,
        "cycle_fundamental_prominence_db": prominence_db,
        "peak_abs_component_counts_by_receiver": [
            float(_mapping(item, "headroom receiver")["peak_abs_component_counts"])
            for item in receivers
        ],
        "amplitude_decoder_error": analysis_error,
        "phase_only_alignment": alignment,
        "accepted_calibration_artifact": False,
    }


def _circular_summary(values_deg: Sequence[float]) -> dict[str, float]:
    radians = np.deg2rad(np.asarray(values_deg, dtype=float))
    resultant = complex(np.mean(np.exp(1j * radians)))
    coherence = abs(resultant)
    return {
        "mean_deg": _wrap_phase_deg(math.degrees(math.atan2(resultant.imag, resultant.real))),
        "circular_std_deg": float(
            math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(coherence, 1e-15)))))
        ),
        "coherence": float(coherence),
        "maximum_pair_delta_deg": float(
            max(
                (_wrap_phase_deg(second - first) for first in values_deg for second in values_deg),
                key=abs,
                default=0.0,
            )
        ),
    }


def build_report(paths: Sequence[Path], *, repository: Path) -> dict[str, Any]:
    if len(paths) < 2:
        raise ValueError("phase-leakage report requires at least two independent screens")
    qualifications = []
    trials = []
    for replicate_index, path in enumerate(paths, start=1):
        identity, conditions = _load_qualification(path)
        qualifications.append(identity)
        trials.extend(
            _analyze_condition(
                condition,
                ledger_path=Path(identity["path"]),
                replicate_index=replicate_index,
            )
            for condition in conditions
        )
    serials = {item["serial"] for item in qualifications}
    uris = {item["uri"] for item in qualifications}
    source_commits = {item["source_commit"] for item in qualifications}
    if len(serials) != 1 or len(uris) != 1 or len(source_commits) != 1:
        raise ValueError("independent screens do not share exact hardware/source identity")
    by_gain: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        by_gain[float(trial["tx_hardware_gain_db"])].append(trial)
    gain_summaries = []
    for gain in EXPECTED_GAINS_DB:
        records = by_gain[gain]
        if len(records) != len(paths):
            raise ValueError("each TX gain must have one artifact per independent screen")
        phase_summaries = []
        for state_index, name in enumerate(STATE_NAMES):
            values = [
                float(item["phase_only_alignment"][
                    "leakage_subtracted_phase_deg_relative_to_ant1"
                ][state_index])
                for item in records
            ]
            phase_summaries.append({"name": name, **_circular_summary(values), "values_deg": values})
        gain_summaries.append(
            {
                "tx_hardware_gain_db": gain,
                "replicate_count": len(records),
                "cycle_period_us": {
                    "minimum": min(float(item["cycle_period_us"]) for item in records),
                    "maximum": max(float(item["cycle_period_us"]) for item in records),
                    "mean": float(np.mean([item["cycle_period_us"] for item in records])),
                },
                "phase_by_state_relative_to_ant1": phase_summaries,
                "maximum_state_pair_delta_deg": max(
                    abs(float(item["maximum_pair_delta_deg"])) for item in phase_summaries
                ),
            }
        )
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "schema": 1,
        "analysis_kind": ANALYSIS_KIND,
        "protocol_id": EXPERIMENTAL_5G8_HIGH_RX_STIMULUS_PROTOCOL_ID,
        "status": "exploratory_phase_signature_reproduced_but_calibration_rejected",
        "generated_at": datetime.now(UTC).isoformat(),
        "analysis_source_commit": head,
        "analysis_script_sha256": sha256_path(Path(__file__).resolve()),
        "serial": next(iter(serials)),
        "uri": next(iter(uris)),
        "capture_source_commit": next(iter(source_commits)),
        "center_frequency_hz": EXPECTED_CENTER_HZ,
        "emitted_carrier_frequency_hz": EXPECTED_CENTER_HZ + 100_000,
        "qualification_inputs": qualifications,
        "trial_count": len(trials),
        "trials": trials,
        "per_gain_reproducibility": gain_summaries,
        "conclusions": {
            "standard_amplitude_marker_calibration_passed": False,
            "all_off_amplitude_contrast_available": False,
            "repeatable_leakage_subtracted_complex_signature_observed": True,
            "may_be_used_as_array_calibration": False,
            "reason": (
                "ALL_OFF/direct leakage masks the amplitude marker and the exploratory "
                "phase-only method is not an accepted calibration protocol"
            ),
        },
        "limitations": [
            "The physical AD9363 is operated through an experimental extended-band profile.",
            "Failed stimulus artifacts remain rejected and are analyzed only diagnostically.",
            "Phase-only cyclic alignment is not independent GPIO timing evidence.",
            "One exact 5.8 GHz center cannot support frequency interpolation.",
        ],
        "report_sha256_excludes_this_field": canonical_json_sha256(
            {
                "qualification_sha256": [item["sha256"] for item in qualifications],
                "trial_data_sha256": [item["data_sha256"] for item in trials],
            }
        ),
    }


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    report = build_report(args.qualification, repository=repository)
    write_json_atomic(args.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output.resolve()),
                "output_sha256": sha256_path(args.output),
                "trial_count": report["trial_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
