from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pluto_plus.hardware import SampleBlockV2

ROOT = Path(__file__).resolve().parents[1]

CAPTURE_SPEC = importlib.util.spec_from_file_location(
    "capture_hexcal_timing_under_test", ROOT / "scripts/capture_hexcal_timing.py"
)
assert CAPTURE_SPEC is not None and CAPTURE_SPEC.loader is not None
capture = importlib.util.module_from_spec(CAPTURE_SPEC)
sys.modules[CAPTURE_SPEC.name] = capture
CAPTURE_SPEC.loader.exec_module(capture)

ANALYZE_SPEC = importlib.util.spec_from_file_location(
    "analyze_hexcal_timing_under_test", ROOT / "scripts/analyze_hexcal_timing.py"
)
assert ANALYZE_SPEC is not None and ANALYZE_SPEC.loader is not None
analyze = importlib.util.module_from_spec(ANALYZE_SPEC)
sys.modules[ANALYZE_SPEC.name] = analyze
ANALYZE_SPEC.loader.exec_module(analyze)


def _block(sequence: int, first_sample: int) -> SampleBlockV2:
    samples = np.vstack(
        (
            np.arange(10, dtype=np.float32) + 1j * np.arange(10, dtype=np.float32),
            2 * np.arange(10, dtype=np.float32) - 1j * np.arange(10, dtype=np.float32),
        )
    ).astype(np.complex64)
    return SampleBlockV2(
        utc_ns=1_000_000_000 + sequence * 2_000,
        samples=samples,
        stream_id=88,
        buffer_sequence=sequence,
        first_sample_sequence=first_sample,
        metadata_flags=(1 << 4) | (1 << 21),
        metadata_abi=2,
        missing_samples_before=0,
        sample_time_realtime_start_ns=2_000_000_000 + sequence * 2_000,
        sample_time_realtime_end_ns=2_000_001_000 + sequence * 2_000,
        sample_time_monotonic_start_ns=3_000_000_000 + sequence * 2_000,
        sample_time_monotonic_end_ns=3_000_001_000 + sequence * 2_000,
        sample_time_uncertainty_ns=100,
    )


def test_failed_memory_fragment_is_hashed_quarantined_and_never_accepted(
    tmp_path: Path,
) -> None:
    blocks = [_block(0, 1_000), _block(1, 1_010)]
    result = capture._persist_memory_quarantine(
        tmp_path,
        blocks=blocks,
        error=OSError("ENODATA"),
        context={"serial": "exact-radio", "source_commit": "a" * 40},
    )

    root = Path(result["path"])
    assert root.parent.name == ".failed"
    assert root.name.endswith(".failed")
    assert result["accepted"] is False
    failure = json.loads(Path(result["failure_record"]).read_text(encoding="utf-8"))
    assert failure["accepted"] is False
    assert failure["automatic_retry_attempted"] is False
    assert failure["retained_frame_count"] == 2
    assert failure["retained_sample_count"] == 20
    for evidence in failure["files"]:
        path = root / evidence["name"]
        assert path.stat().st_size == evidence["size_bytes"]
        assert capture.sha256_path(path) == evidence["sha256"]
    metadata_path = next(root.glob("*.sigmf-meta"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["smateway:quarantine"]["accepted"] is False
    assert metadata["smateway:quarantine"]["may_be_used_for_qualification"] is False
    assert metadata["pluto:continuity"]["block_count"] == 2


def test_active_tone_readback_requires_agreeing_tx1_iq() -> None:
    capture_result = SimpleNamespace(dds_frequency_readback_hz=(100_021.0, 0.0, -100_019.0, 0.0))
    assert capture._active_tone_readback_hz(capture_result) == pytest.approx(100_020.0)
    capture_result.dds_frequency_readback_hz = (100_000.0, 0.0, -101_000.0, 0.0)
    with pytest.raises(RuntimeError, match="I/Q DDS frequency readbacks disagree"):
        capture._active_tone_readback_hz(capture_result)


def _minimal_analysis(marker: float, cycle: float) -> dict[str, object]:
    return {
        "timing": {
            "combined_rf_marker_us": {"median": marker},
            "dwells_us": {
                f"ANT{state}": {"median": 200.0 + state / 100.0} for state in range(1, 7)
            },
            "ordinary_guards_us": {
                f"ANT{state}_TO_ANT{state + 1}": {"q50_us": {"median": 20.0 + state / 100.0}}
                for state in range(1, 6)
            },
            "cycle_us": {"median": cycle},
        }
    }


def test_replicate_agreement_has_independent_frozen_limits() -> None:
    passed = analyze._replicate_agreement(
        [_minimal_analysis(200.0, 1_500.0), _minimal_analysis(200.8, 1_501.9)]
    )
    failed = analyze._replicate_agreement(
        [_minimal_analysis(200.0, 1_500.0), _minimal_analysis(201.1, 1_502.1)]
    )
    assert passed["passed"] is True
    assert passed["frozen_gates"] == {
        "maximum_marker_dwell_guard_median_delta_us": 1.0,
        "maximum_cycle_median_delta_us": 2.0,
    }
    assert failed["passed"] is False
    assert failed["failed_metrics"] == [
        "combined_rf_marker_median",
        "cycle_median",
    ]


def _pair_plan_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    tone_plan = capture.SafeDdsTonePlan(
        uri="usb:1.2.3",
        serial="exact-serial",
        center_frequency_hz=2_400_000_000,
        sample_rate_hz=5_000_000,
        bandwidth_hz=4_000_000,
        tone_frequency_hz=100_000,
        tx_channel=0,
        tx_hardware_gain_db=-40.0,
        dds_scale=0.125,
        receiver_gain_db=0.0,
        source_peak_output_bound_dbm=7.0,
        load_input_limit_dbm=0.0,
        path_attenuation_before_load_db=0.0,
        required_margin_db=10.0,
        settle_ms=100,
    )
    source = {"commit": "a" * 40, "files": []}
    dependency = {
        "schema": 1,
        "dependency": "pluto-plus-utils",
        "commit": "e" * 40,
        "files": [],
    }
    dependency_sha256 = capture.canonical_json_sha256(dependency)
    profile = {"file_sha256": "b" * 64, "contract_sha256": "c" * 64}
    firmware = {"file_sha256": "d" * 64}
    plan = capture._pair_plan_contract(
        run_id="hexcal-timing-test",
        board_id="stm32c011-test",
        source=source,
        pluto_plus_utils_source_attestation=dependency,
        pluto_plus_utils_source_attestation_sha256=dependency_sha256,
        profile=profile,
        firmware=firmware,
        center_frequency_policy="official_ad9363_range",
        plan=tone_plan,
        bench_lock_path=tmp_path / ".bench.lock",
    )
    mute = {
        "purpose": "post_replicate_1",
        "status": "passed",
        "serial": "exact-serial",
        "attestation": "mute_returned_radio_exact_serial_readback",
        "started_at": "2026-08-26T00:00:00+00:00",
        "completed_at": "2026-08-26T00:00:01+00:00",
        "error": None,
    }
    capture_document: dict[str, object] = {
        "board_id": "stm32c011-test",
        "serial": "exact-serial",
        "uri": "usb:1.2.3",
        "center_frequency_hz": 2_400_000_000,
        "center_frequency_policy": "official_ad9363_range",
        "tone_offset_hz_requested": 100_000,
        "tx_hardware_gain_db_requested": -40.0,
        "dds_scale_requested": 0.125,
        "receiver_gain_db": 0.0,
        "worst_case_load_input_dbm": tone_plan.worst_case_load_input_dbm,
        "kernel_buffers": 8,
        "tx_gain_readback_db": -40.0,
        "dds_scale_readback": [0.125, 0.0, -0.125, 0.0, 0.0, 0.0, 0.0, 0.0],
        "dds_enabled_readback": [True, False, True, False, False, False, False, False],
        "dds_frequency_readback_hz": [
            100_000,
            0,
            -100_000,
            0,
            0,
            0,
            0,
            0,
        ],
        "tone_offset_readback_hz": 100_000.0,
        "tx_readback_contract": {
            "selected_tx_gain_readback_db": -40.0,
            "unselected_tx2_gain_readback_db_attested_by_helper": -80.0,
            "active_dds_indices": [0, 2],
            "inactive_dds_scales_required_zero": True,
            "tx2_never_enabled": True,
        },
    }
    rf_readback = {
        "schema": 1,
        "evidence_kind": "pluto_tx1_dds_live_readback",
        "tx_channel": 0,
        "tx_port": "TX1",
        "kernel_buffers": 8,
        "tx_hardware_gain_db_requested": -40.0,
        "tx_hardware_gain_readback_db_by_channel": [-40.0, -80.0],
        "tx2_gain_readback_provenance": ("pluto_plus_utils_capture_helper_internal_exact_readback"),
        "dds_scale_requested": 0.125,
        "dds_scale_readback": capture_document["dds_scale_readback"],
        "dds_enabled_readback": capture_document["dds_enabled_readback"],
        "tone_frequency_hz_requested": 100_000.0,
        "dds_frequency_readback_hz": capture_document["dds_frequency_readback_hz"],
        "active_dds_indices": [0, 2],
        "inactive_dds_indices": [1, 3, 4, 5, 6, 7],
        "inactive_dds_rf_activity_contract": (
            "exact_zero_scale; enable_and_frequency_are_raw_diagnostics"
        ),
    }
    capture_document["rf_readback_evidence"] = rf_readback
    capture_document["rf_readback_evidence_sha256"] = capture.canonical_json_sha256(rf_readback)
    root: dict[str, object] = {
        "run_id": "hexcal-timing-test",
        "replicate_index": 1,
        "source": source,
        "pluto_plus_utils_source_attestation": dependency,
        "pluto_plus_utils_source_attestation_sha256": dependency_sha256,
        "source_profile": profile,
        "firmware_evidence": firmware,
        "pair_plan_contract": plan,
        "pair_plan_contract_sha256": capture._canonical_sha256(plan),
        "capture_safety": {
            "refill_callback_action": "copy_to_ram_only",
            "disk_persistence_began_after_helper_returned_and_tx_was_muted": True,
            "post_helper_exact_serial_mute": mute,
            "tx2_never_enabled": True,
            "no_automatic_retry": True,
            "sigint_sigterm_sighup_are_cooperative_exceptions": True,
            "sigkill_cannot_be_intercepted": True,
        },
    }
    return root, capture_document


def test_analyzer_binds_full_dds_and_mute_readbacks_to_pair_plan(tmp_path: Path) -> None:
    root, capture_document = _pair_plan_fixture(tmp_path)
    analyze._validate_pair_plan_binding(root, capture_document)

    corrupted_dds = json.loads(json.dumps(capture_document))
    corrupted_dds["dds_scale_readback"][5] = 0.01
    corrupted_dds["rf_readback_evidence"]["dds_scale_readback"][5] = 0.01
    corrupted_dds["rf_readback_evidence_sha256"] = capture.canonical_json_sha256(
        corrupted_dds["rf_readback_evidence"]
    )
    with pytest.raises(ValueError, match="inactive DDS scale"):
        analyze._validate_pair_plan_binding(root, corrupted_dds)

    corrupted_mute = json.loads(json.dumps(root))
    corrupted_mute["capture_safety"]["post_helper_exact_serial_mute"]["status"] = "failed"
    with pytest.raises(ValueError, match="mute attestation"):
        analyze._validate_pair_plan_binding(corrupted_mute, capture_document)


def test_analyzer_binds_v2_timing_settings_to_stimulus_qualification(
    tmp_path: Path,
) -> None:
    root, capture_document = _pair_plan_fixture(tmp_path)
    plan = root["pair_plan_contract"]
    assert isinstance(plan, dict)
    plan["plan_kind"] = "hexcal_v2_2g4_rf_timing_two_replicates"
    plan["protocol_id"] = "hexcal-v2-2g4-stimulus"
    plan["stimulus"]["receiver_gain_db"] = 20
    capture_document["receiver_gain_db"] = 20
    plan["stimulus_qualification"] = {
        "path": "/evidence/stimulus.json",
        "file_sha256": "f" * 64,
        "fixed_receiver_gain_db": 20,
        "selected_tx_hardware_gain_db": -40.0,
        "dds_scale": 0.125,
    }
    root["pair_plan_contract_sha256"] = capture._canonical_sha256(plan)
    analyze._validate_pair_plan_binding(root, capture_document)

    corrupted = json.loads(json.dumps(root))
    corrupted_plan = corrupted["pair_plan_contract"]
    corrupted_plan["stimulus_qualification"]["selected_tx_hardware_gain_db"] = -35.0
    corrupted["pair_plan_contract_sha256"] = capture._canonical_sha256(corrupted_plan)
    with pytest.raises(ValueError, match="frozen qualification"):
        analyze._validate_pair_plan_binding(corrupted, capture_document)


def test_capture_record_stream_identity_is_bound_to_replayed_abi2_ledger() -> None:
    continuity = {
        "metadata_abi": 2,
        "stream_id": 8123,
        "first_buffer_sequence": 0,
        "last_buffer_sequence": 8,
        "first_sample_sequence": 900_000,
        "last_sample_sequence_exclusive": 3_150_000,
    }
    capture_record = {
        "metadata_abi": 2,
        "stream_id": 8123,
        "first_buffer_sequence": 0,
        "last_buffer_sequence": 8,
        "first_sample_sequence": 900_000,
        "last_sample_sequence_exclusive": 3_150_000,
    }
    analyze._bind_capture_record_to_continuity(capture_record, continuity)

    relabeled = dict(capture_record)
    relabeled["stream_id"] = 9001
    with pytest.raises(ValueError, match="stream_id differs"):
        analyze._bind_capture_record_to_continuity(relabeled, continuity)


def test_cooperative_signals_and_board_lock_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(capture.CooperativeTermination, match="SIGTERM"):
        capture._signal_handler(capture.signal.SIGTERM, None)

    lock_path = tmp_path / "board" / ".bench.lock"
    with (
        capture._exclusive_bench_lock(lock_path),
        pytest.raises(RuntimeError, match="already held"),
        capture._exclusive_bench_lock(lock_path),
    ):
        pass

    called: list[str] = []
    monkeypatch.setattr(capture, "mute_returned_radio", called.append)
    attestation = capture._exact_serial_mute("exact-serial", "final")
    assert called == ["exact-serial"]
    assert capture._mute_passed(attestation, serial="exact-serial", purpose="final")
