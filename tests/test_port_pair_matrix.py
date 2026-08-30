from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, replace
from math import cos, radians, sin

import pytest

from smateway.port_pair_matrix import (
    CALIBRATION_KIND,
    CAPTURE_TX_GAIN_DB,
    CELL_IDS,
    DDS_SCALE,
    FIXTURE_KIND,
    PREFLIGHT_TX_GAIN_DB,
    TX_PORTS,
    CaptureIdentity,
    ComplexDetection,
    HeadroomPreflight,
    PortPairMatrixError,
    PortPairRepeat,
    analyze_port_pair_matrix,
    canonical_sha256,
    evaluate_headroom_preflight,
    port_pair_repeat_from_observation,
    validate_calibration,
    validate_fixture,
)

PLAN_SHA256 = "f" * 64
SOURCE_COMMIT = "a" * 40
DEPENDENCY_COMMIT = "b" * 40
NATIVE_SHA256 = "c" * 64


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _phasor(magnitude: float, phase_deg: float) -> complex:
    angle = radians(phase_deg)
    return magnitude * complex(cos(angle), sin(angle))


def _termination(label: str) -> dict[str, object]:
    return {
        "termination_id": label,
        "identity_sha256": _hash(label),
        "rated_min_hz": 2_000_000_000,
        "rated_max_hz": 8_000_000_000,
        "impedance_ohm": 50.0,
        "maximum_input_dbm": 20.0,
    }


def _chain(label: str, receiver: str, *, permanent: bool, independent: bool) -> dict[str, object]:
    return {
        "chain_id": label,
        "identity_sha256": _hash(label),
        "assigned_receiver": receiver,
        "rated_min_hz": 2_000_000_000,
        "rated_max_hz": 8_000_000_000,
        "attenuation_db": 30.0,
        "attenuation_tolerance_db": 0.5,
        "maximum_input_dbm": 20.0,
        "permanently_installed": permanent,
        "removal_forbidden": permanent,
        "independent_of_rx1_chain": independent,
    }


def _fixture_document() -> dict[str, object]:
    return {
        "schema": 1,
        "fixture_kind": FIXTURE_KIND,
        "fixture_id": "protected-port-pair-fixture-01",
        "center_frequency_hz": 5_800_000_000,
        "fixed_graph_sha256": _hash("fixed-graph"),
        "receiver_input_limit_dbm": -10.0,
        "required_safety_margin_db": 10.0,
        "rx1_protection": _chain(
            "rx1-established-protection", "RX1", permanent=True, independent=False
        ),
        "rx2_reference_chain": _chain(
            "rx2-second-reference-chain", "RX2", permanent=False, independent=True
        ),
        "reference_distribution": {
            "identity_sha256": _hash("reference-distribution"),
            "active_tx_reference_plane_sha256": _hash("active-tx-reference-plane"),
            "minimum_path_loss_db": 3.0,
            "unused_output_termination": _termination("distribution-output-load"),
        },
        "inactive_tx_terminations": {
            "TX1": _termination("tx1-load"),
            "TX2": _termination("tx2-load"),
        },
        "test_receiver_terminations": {
            "RX1": _termination("rx1-test-load"),
            "RX2": _termination("rx2-test-load"),
        },
        "test_reference_plane_sha256s": {
            "RX1": _hash("rx1-protected-test-plane"),
            "RX2": _hash("rx2-test-plane"),
        },
        "reference_plane_sha256s": {
            "RX1": _hash("rx1-protected-reference-plane"),
            "RX2": _hash("rx2-protected-reference-plane"),
        },
    }


def _complex_document(value: complex) -> dict[str, float]:
    return {"real": value.real, "imag": value.imag}


def _calibration_document(fixture_sha256: str) -> dict[str, object]:
    return {
        "schema": 1,
        "calibration_kind": CALIBRATION_KIND,
        "calibration_id": "protected-port-pair-calibration-01",
        "fixture_sha256": fixture_sha256,
        "center_frequency_hz": 5_800_000_000,
        "receiver_calibrations": {
            "RX1": {
                "test_receiver_response": _complex_document(_phasor(2.0, 12.0)),
                "test_response_evidence_sha256": _hash("rx1-test-calibration"),
                "reference_chain_response": _complex_document(_phasor(0.08, -33.0)),
                "reference_response_evidence_sha256": _hash("rx1-reference-calibration"),
                "reference_chain_sha256": _hash("rx1-established-protection"),
                "test_reference_plane_sha256": _hash("rx1-protected-test-plane"),
                "reference_plane_sha256": _hash("rx1-protected-reference-plane"),
            },
            "RX2": {
                "test_receiver_response": _complex_document(_phasor(4.0, -18.0)),
                "test_response_evidence_sha256": _hash("rx2-test-calibration"),
                "reference_chain_response": _complex_document(_phasor(0.035, 47.0)),
                "reference_response_evidence_sha256": _hash("rx2-reference-calibration"),
                "reference_chain_sha256": _hash("rx2-second-reference-chain"),
                "test_reference_plane_sha256": _hash("rx2-test-plane"),
                "reference_plane_sha256": _hash("rx2-protected-reference-plane"),
            },
        },
    }


def _capture_identity(label: str, condition_label: str) -> CaptureIdentity:
    leaf = (_hash(f"leaf-{label}"),)
    return CaptureIdentity(
        run_id=f"run-{condition_label}",
        stream_id=f"stream-{label}",
        artifact_sha256=_hash(f"artifact-{label}"),
        raw_iq_sha256=_hash(f"raw-{label}"),
        metadata_sha256=_hash(f"metadata-{label}"),
        condition_record_sha256=_hash(f"condition-{condition_label}"),
        leaf_source_sha256s=leaf,
        leaf_source_set_sha256=canonical_sha256(leaf),
    )


def _expected_transfer(cell_id: str) -> complex:
    index = CELL_IDS.index(cell_id)
    return _phasor(0.001 + index * 0.0002, -120.0 + index * 57.0)


def _repeats(*, nondetected_cell: str | None = None) -> list[PortPairRepeat]:
    fixture = validate_fixture(_fixture_document())
    calibration = validate_calibration(_calibration_document(fixture.fixture_sha256), fixture)
    output = []
    for cell_id in CELL_IDS:
        cell = fixture.cell(cell_id)
        test_calibration = calibration.receiver(cell.test_receiver)
        reference_calibration = calibration.receiver(cell.reference_receiver)
        transfer = _expected_transfer(cell_id)
        for repeat_index in range(1, 6):
            reference_plane_tone = _phasor(1.0 + repeat_index * 0.001, 5.0)
            raw_reference = reference_plane_tone * reference_calibration.reference_chain_response
            raw_test = transfer * reference_plane_tone * test_calibration.test_receiver_response
            active_index = TX_PORTS.index(cell.active_tx)
            gains = [-80.0, -80.0]
            gains[active_index] = CAPTURE_TX_GAIN_DB
            scales = [0.0] * 8
            scales[active_index * 4] = DDS_SCALE
            scales[active_index * 4 + 2] = DDS_SCALE
            label = f"{cell_id}-{repeat_index}"
            nondetected = nondetected_cell == cell_id and repeat_index == 3
            output.append(
                PortPairRepeat(
                    cell_id=cell_id,
                    repeat_index=repeat_index,
                    plan_sha256=PLAN_SHA256,
                    fixture_sha256=fixture.fixture_sha256,
                    calibration_sha256=calibration.calibration_sha256,
                    topology_sha256=cell.topology_sha256,
                    source_commit=SOURCE_COMMIT,
                    dependency_commit=DEPENDENCY_COMMIT,
                    native_attestation_sha256=NATIVE_SHA256,
                    preflight_capture=_capture_identity(f"preflight-{label}", label),
                    main_capture=_capture_identity(f"main-{label}", label),
                    headroom_preflight=HeadroomPreflight(
                        preflight_tx_gain_db=PREFLIGHT_TX_GAIN_DB,
                        capture_tx_gain_db=CAPTURE_TX_GAIN_DB,
                        clip_threshold_abs_counts=2047.0,
                        peak_abs_counts_by_receiver=(35.0, 42.0),
                        clipped_sample_count_by_receiver=(0, 0),
                    ),
                    tx_gain_readback_db_by_channel=(gains[0], gains[1]),
                    dds_scale_readback=tuple(scales),
                    clipped_sample_count_by_receiver=(0, 0),
                    inactive_tx_termination_sha256=(cell.inactive_tx_termination_sha256),
                    test_receiver_termination_sha256=(cell.test_receiver_termination_sha256),
                    reference_chain_sha256=cell.reference_chain_sha256,
                    rx1_protection_sha256=cell.rx1_protection_sha256,
                    test_receiver_tone=(
                        ComplexDetection(False, None, abs(raw_test) * 1.1)
                        if nondetected
                        else ComplexDetection(True, raw_test, None)
                    ),
                    reference_receiver_tone=raw_reference,
                    reference_tone_snr_db=35.0,
                    continuity_passed=True,
                    quality_passed=True,
                    final_mute_passed=True,
                    final_tx_gain_readback_db_by_channel=(-80.0, -80.0),
                    final_dds_scale_readback=(0.0,) * 8,
                )
            )
    return output


def _observation(repeat: PortPairRepeat) -> dict[str, object]:
    def artifact(capture: CaptureIdentity, label: str) -> dict[str, str]:
        return {
            "artifact_id": f"artifact-id-{label}-{capture.stream_id}",
            "raw_iq_sha256": capture.raw_iq_sha256,
            "metadata_sha256": capture.metadata_sha256,
        }

    detected = repeat.test_receiver_tone.detected
    test_phasor = repeat.test_receiver_tone.phasor
    return {
        "schema": 1,
        "observation_kind": "5g8_port_pair_normalized_observation",
        "campaign_id": "campaign-a",
        "run_id": repeat.main_capture.run_id,
        "cell_id": repeat.cell_id,
        "repeat_index": repeat.repeat_index,
        "campaign_plan_sha256": repeat.plan_sha256,
        "plan_contract_sha256": _hash(f"condition-plan-{repeat.cell_id}-{repeat.repeat_index}"),
        "fixture_sha256": repeat.fixture_sha256,
        "calibration_sha256": repeat.calibration_sha256,
        "topology_sha256": repeat.topology_sha256,
        "source_commit": repeat.source_commit,
        "dependency_commit": repeat.dependency_commit,
        "native_attestation_sha256": repeat.native_attestation_sha256,
        "preflight": {
            "stream_id": repeat.preflight_capture.stream_id,
            "artifact": artifact(repeat.preflight_capture, "preflight"),
            "condition_record_sha256": repeat.preflight_capture.condition_record_sha256,
            "headroom": {
                "input": asdict(repeat.headroom_preflight),
                "admission": asdict(evaluate_headroom_preflight(repeat.headroom_preflight)),
            },
            "continuity_passed": True,
        },
        "main": {
            "stream_id": repeat.main_capture.stream_id,
            "artifact": artifact(repeat.main_capture, "main"),
            "condition_record_sha256": repeat.main_capture.condition_record_sha256,
            "rf_readback": {
                "tx_gain_readback_db_by_channel": list(repeat.tx_gain_readback_db_by_channel),
                "dds_scale_readback": list(repeat.dds_scale_readback),
            },
            "clipped_sample_count_by_receiver": list(repeat.clipped_sample_count_by_receiver),
            "analysis": {
                "test_receiver_tone_detected": detected,
                "test_receiver_tone": (
                    None
                    if test_phasor is None
                    else {"real": test_phasor.real, "imag": test_phasor.imag}
                ),
                "test_receiver_tone_magnitude_upper_bound": (
                    repeat.test_receiver_tone.magnitude_upper_bound
                ),
                "reference_receiver_tone": {
                    "real": repeat.reference_receiver_tone.real,
                    "imag": repeat.reference_receiver_tone.imag,
                },
                "reference_tone_snr_db": repeat.reference_tone_snr_db,
            },
            "continuity_passed": True,
        },
        "physical_safety": {
            "inactive_tx_termination_sha256": repeat.inactive_tx_termination_sha256,
            "test_receiver_termination_sha256": repeat.test_receiver_termination_sha256,
            "reference_chain_sha256": repeat.reference_chain_sha256,
            "rx1_protection_sha256": repeat.rx1_protection_sha256,
        },
        "final_mute": {
            "status": "passed",
            "tx_gain_readback_db_by_channel": list(repeat.final_tx_gain_readback_db_by_channel),
            "dds_scale_readback": list(repeat.final_dds_scale_readback),
        },
        "quality_passed": True,
        "raw_channel_amplitudes_comparable": False,
    }


def _validated_fixture_and_calibration():
    fixture = validate_fixture(_fixture_document())
    calibration = validate_calibration(_calibration_document(fixture.fixture_sha256), fixture)
    return fixture, calibration


def test_runner_observation_parser_feeds_the_same_twenty_repeat_analyzer() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    parsed = [port_pair_repeat_from_observation(_observation(item)) for item in _repeats()]

    result = analyze_port_pair_matrix(
        fixture,
        calibration,
        parsed,
        plan_sha256=PLAN_SHA256,
        bootstrap_draw_count=256,
    )

    assert result.exact_four_cells_verified
    assert all(result.cell(cell_id).normalized_transfer is not None for cell_id in CELL_IDS)


def test_fixture_derives_exact_four_protected_cells() -> None:
    fixture = validate_fixture(_fixture_document())

    assert tuple(cell.cell_id for cell in fixture.cells) == CELL_IDS
    assert all(
        cell.rx1_protection_sha256 == fixture.rx1_protection.identity_sha256
        for cell in fixture.cells
    )
    assert fixture.rx1_protection.permanently_installed
    assert fixture.rx1_protection.removal_forbidden
    assert fixture.rx2_reference_chain.independent_of_rx1_chain
    assert fixture.rx1_protection.identity_sha256 != fixture.rx2_reference_chain.identity_sha256


def test_rejects_unprotected_or_removable_rx1() -> None:
    document = _fixture_document()
    rx1 = document["rx1_protection"]
    assert isinstance(rx1, dict)
    rx1["removal_forbidden"] = False

    with pytest.raises(PortPairMatrixError, match="permanent and removal-forbidden"):
        validate_fixture(document)


def test_rejects_moving_same_attenuator_between_receivers() -> None:
    document = _fixture_document()
    rx1 = document["rx1_protection"]
    rx2 = document["rx2_reference_chain"]
    assert isinstance(rx1, dict) and isinstance(rx2, dict)
    rx2["identity_sha256"] = rx1["identity_sha256"]

    with pytest.raises(PortPairMatrixError, match="physically distinct"):
        validate_fixture(document)


def test_rejects_attenuator_not_rated_at_5p8_ghz() -> None:
    document = _fixture_document()
    rx2 = document["rx2_reference_chain"]
    assert isinstance(rx2, dict)
    rx2["rated_max_hz"] = 5_000_000_000

    with pytest.raises(PortPairMatrixError, match="not rated through 5.8 GHz"):
        validate_fixture(document)


def test_rejects_unsafe_reference_chain_before_any_capture() -> None:
    document = _fixture_document()
    document["receiver_input_limit_dbm"] = -70.0

    with pytest.raises(PortPairMatrixError, match="reference chain is unsafe"):
        validate_fixture(document)


def test_headroom_preflight_projects_main_gain_and_rejects_low_margin() -> None:
    passed = evaluate_headroom_preflight(
        HeadroomPreflight(
            preflight_tx_gain_db=-40.0,
            capture_tx_gain_db=-20.0,
            clip_threshold_abs_counts=2047.0,
            peak_abs_counts_by_receiver=(40.0, 50.0),
            clipped_sample_count_by_receiver=(0, 0),
        )
    )
    failed = evaluate_headroom_preflight(
        HeadroomPreflight(
            preflight_tx_gain_db=-40.0,
            capture_tx_gain_db=-20.0,
            clip_threshold_abs_counts=2047.0,
            peak_abs_counts_by_receiver=(40.0, 150.0),
            clipped_sample_count_by_receiver=(0, 0),
        )
    )

    assert passed.passed
    assert passed.projected_peak_abs_counts_by_receiver == pytest.approx((400.0, 500.0))
    assert not failed.passed
    assert "RX2_projected_headroom_below_minimum" in failed.rejection_reasons


def test_normalizes_receiver_and_reference_chain_gain_before_comparison() -> None:
    fixture, calibration = _validated_fixture_and_calibration()

    result = analyze_port_pair_matrix(
        fixture,
        calibration,
        _repeats(),
        plan_sha256=PLAN_SHA256,
        bootstrap_draw_count=512,
    )

    assert result.exact_four_cells_verified
    assert result.five_source_distinct_repeats_per_cell_verified
    assert result.receiver_reference_gain_normalization_applied
    assert result.raw_channel_comparison_forbidden
    for cell_id in CELL_IDS:
        cell = result.cell(cell_id)
        assert cell.normalized_transfer is not None
        assert cell.normalized_transfer.center == pytest.approx(_expected_transfer(cell_id))
        assert not cell.raw_channel_amplitudes_comparable
        assert "G_test_receiver" in cell.normalization_equation


def test_rejects_active_inactive_tx_despite_physical_termination() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    first = repeats[0]
    scales = list(first.dds_scale_readback)
    inactive_index = TX_PORTS.index(fixture.cell(first.cell_id).inactive_tx)
    scales[inactive_index * 4] = 0.01
    repeats[0] = replace(first, dds_scale_readback=tuple(scales))

    with pytest.raises(PortPairMatrixError, match="inactive TX DDS is active"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_rejects_clipped_main_capture() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    repeats[4] = replace(repeats[4], clipped_sample_count_by_receiver=(0, 1))

    with pytest.raises(PortPairMatrixError, match="clipped samples"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_rejects_missing_or_wrong_test_receiver_termination() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    repeats[8] = replace(repeats[8], test_receiver_termination_sha256=_hash("wrong-load"))

    with pytest.raises(PortPairMatrixError, match="direct termination"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_rejects_wrong_reference_attenuator() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    repeats[2] = replace(repeats[2], reference_chain_sha256=_hash("wrong-attenuator"))

    with pytest.raises(PortPairMatrixError, match="wrong attenuator"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_rejects_non_disjoint_main_or_preflight_sources() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    target = repeats[1].main_capture
    reused = repeats[0].main_capture
    repeats[1] = replace(
        repeats[1],
        main_capture=replace(
            target,
            stream_id=reused.stream_id,
            artifact_sha256=reused.artifact_sha256,
            raw_iq_sha256=reused.raw_iq_sha256,
            metadata_sha256=reused.metadata_sha256,
            leaf_source_sha256s=reused.leaf_source_sha256s,
            leaf_source_set_sha256=reused.leaf_source_set_sha256,
        ),
    )

    with pytest.raises(PortPairMatrixError, match="not source-distinct"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


@pytest.mark.parametrize("count", [19, 21])
def test_requires_exactly_five_repeats_in_each_of_four_cells(count: int) -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    selected = repeats[:count] if count == 19 else [*repeats, repeats[-1]]

    with pytest.raises(PortPairMatrixError, match="exactly 20"):
        analyze_port_pair_matrix(
            fixture, calibration, selected, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_rejects_failure_to_prove_exact_final_mute() -> None:
    fixture, calibration = _validated_fixture_and_calibration()
    repeats = _repeats()
    repeats[12] = replace(repeats[12], final_mute_passed=False)

    with pytest.raises(PortPairMatrixError, match="exact final mute"):
        analyze_port_pair_matrix(
            fixture, calibration, repeats, plan_sha256=PLAN_SHA256, bootstrap_draw_count=256
        )


def test_nondetected_test_tone_returns_only_normalized_phase_free_bound() -> None:
    fixture, calibration = _validated_fixture_and_calibration()

    result = analyze_port_pair_matrix(
        fixture,
        calibration,
        _repeats(nondetected_cell="TX2_RX1"),
        plan_sha256=PLAN_SHA256,
        bootstrap_draw_count=256,
    )
    cell = result.cell("TX2_RX1")

    assert not cell.all_test_tones_detected
    assert cell.normalized_transfer is None
    assert cell.normalized_magnitude_upper_bound is not None
    assert cell.normalized_magnitude_upper_bound > 0.0
    assert not cell.phase_available


def test_calibration_rejects_wrong_attenuator_and_stale_fixture() -> None:
    fixture = validate_fixture(_fixture_document())
    wrong_chain = _calibration_document(fixture.fixture_sha256)
    receivers = wrong_chain["receiver_calibrations"]
    assert isinstance(receivers, dict) and isinstance(receivers["RX2"], dict)
    receivers["RX2"]["reference_chain_sha256"] = _hash("not-the-rx2-chain")

    with pytest.raises(PortPairMatrixError, match="wrong attenuator"):
        validate_calibration(wrong_chain, fixture)

    stale = _calibration_document(_hash("other-fixture"))
    with pytest.raises(PortPairMatrixError, match="stale"):
        validate_calibration(stale, fixture)


def test_fixture_rejects_missing_port_and_non_50_ohm_termination() -> None:
    missing = _fixture_document()
    terms = missing["inactive_tx_terminations"]
    assert isinstance(terms, dict)
    del terms["TX2"]
    with pytest.raises(PortPairMatrixError, match="bind exactly TX1 and TX2"):
        validate_fixture(missing)

    wrong_impedance = copy.deepcopy(_fixture_document())
    rx_terms = wrong_impedance["test_receiver_terminations"]
    assert isinstance(rx_terms, dict) and isinstance(rx_terms["RX1"], dict)
    rx_terms["RX1"]["impedance_ohm"] = 75.0
    with pytest.raises(PortPairMatrixError, match="50-ohm"):
        validate_fixture(wrong_impedance)
