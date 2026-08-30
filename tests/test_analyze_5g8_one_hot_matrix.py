from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from smateway.one_hot_ladder import ANTENNA_STATES, _seal_verified_one_hot_row_bundle

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_5g8_one_hot_matrix.py"
SPEC = importlib.util.spec_from_file_location("analyze_5g8_one_hot_matrix_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)

DEPENDENCY = {"schema": 1, "dependency": "pluto-plus-utils", "files": ["exact"]}
NATIVE = {"schema": 1, "runtime": "exact-native"}


def _source_identity() -> dict[str, Any]:
    return {
        "smateway_commit": "1" * 40,
        "pluto_plus_utils_source_attestation": DEPENDENCY,
        "pluto_plus_utils_source_attestation_sha256": analyzer._canonical_sha256(DEPENDENCY),
        "native_libiio_runtime_attestation": NATIVE,
        "native_libiio_runtime_attestation_sha256": analyzer._canonical_sha256(NATIVE),
        "analyzer": "smateway.leakage_ladder.analyze_coherent_leakage",
        "pilot_estimator": "smateway.ota_analysis.estimate_coherent_pilot_offset",
        "capture_helper": "pluto_plus.hardware.capture_continuous_safe_dds_tone",
        "identity_resolver": "pluto_plus.hardware.iio.resolve_iio_uri",
        "runner": "scripts/run_5g8_one_hot_path_ladder.py",
        "run_aggregator": "smateway.one_hot_ladder.summarize_one_hot_run",
        "matrix_aggregator": "smateway.one_hot_ladder.summarize_complete_one_hot_matrix",
    }


CURRENT_IDENTITY = _source_identity()


def _paths(tmp_path: Path) -> tuple[tuple[str, Path, Path], ...]:
    paths = tuple(
        (arm, tmp_path / arm / "plan.json", tmp_path / arm / "manifest.json")
        for arm in ANTENNA_STATES
    )
    for arm, plan, _ in paths:
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            json.dumps(
                {
                    "plan_contract": {
                        "source": _source_identity(),
                        "storage": {
                            "medium": "raspberry_pi_local_filesystem",
                            "pluto_onboard_storage_used": False,
                            "run_capture_root": str(tmp_path / "captures" / arm),
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
    return paths


def _bundle(arm: str) -> Any:
    return _seal_verified_one_hot_row_bundle(
        {
            "driven_input": arm,
            "matrix_identity": {
                "acquisition_configuration": {
                    "tx_hardware_gains_db": [-35.0, -30.0, -25.0, -20.0, -15.0, -10.0],
                    "attribution_tx_hardware_gain_db": -20.0,
                    "attribution_repeat_count": 5,
                    "minimum_detected_attribution_repeats": 3,
                    "minimum_intended_through_contrast_over_all_off_db": 6.0,
                    "maximum_attribution_amplitude_span_db": 0.2,
                    "maximum_attribution_phase_residual_deg": 2.0,
                }
            },
        }
    )


def test_cli_requires_exact_order_and_unique_file_pairs(tmp_path: Path) -> None:
    paths = list(_paths(tmp_path))
    with pytest.raises(analyzer.OneHotMatrixAnalysisError, match="ANT1..ANT8 order"):
        analyzer.analyze_rows(tuple(reversed(paths)))

    paths[1] = ("ANT2", paths[0][1], paths[1][2])
    with pytest.raises(analyzer.OneHotMatrixAnalysisError, match="reuse"):
        analyzer.analyze_rows(paths)


def test_every_input_is_delegated_to_authoritative_file_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []

    def load(*, plan_path: Path, manifest_path: Path) -> Any:
        calls.append((plan_path, manifest_path))
        return _bundle(plan_path.parent.name)

    expected_summary = SimpleNamespace(quality_passed=True)
    monkeypatch.setattr(
        analyzer,
        "_RUNNER",
        SimpleNamespace(load_verified_one_hot_row_bundle=load),
    )
    monkeypatch.setattr(
        analyzer,
        "_recompute_current_execution_identity",
        lambda _runner: CURRENT_IDENTITY,
    )
    monkeypatch.setattr(
        analyzer,
        "summarize_complete_one_hot_matrix",
        lambda *_args, **_kwargs: expected_summary,
    )

    summary, rows = analyzer.analyze_rows(_paths(tmp_path))

    assert summary is expected_summary
    assert len(rows) == 8
    assert calls == [(plan, manifest) for _, plan, manifest in _paths(tmp_path)]


def test_fabricated_row_json_cannot_bypass_file_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(**_kwargs: Any) -> Any:
        raise ValueError("manifest/raw-IQ revalidation failed")

    monkeypatch.setattr(
        analyzer,
        "_RUNNER",
        SimpleNamespace(load_verified_one_hot_row_bundle=reject),
    )
    monkeypatch.setattr(
        analyzer,
        "_recompute_current_execution_identity",
        lambda _runner: CURRENT_IDENTITY,
    )
    with pytest.raises(ValueError, match="raw-IQ"):
        analyzer.analyze_rows(_paths(tmp_path))


@pytest.mark.parametrize(
    ("identity", "message"),
    (
        ("smateway", "Smateway source closure differs"),
        ("dependency", "pluto-plus-utils source closure differs"),
        ("native", "native libiio identity differs"),
        ("descriptor", "complete analysis source contract differs"),
    ),
)
def test_current_execution_identity_must_exactly_match_frozen_row(
    identity: str,
    message: str,
) -> None:
    current = dict(CURRENT_IDENTITY)
    if identity == "smateway":
        current["smateway_commit"] = "2" * 40
    elif identity == "dependency":
        current["pluto_plus_utils_source_attestation"] = {"dependency": "changed"}
    elif identity == "native":
        current["native_libiio_runtime_attestation"] = {"runtime": "changed"}
    else:
        current["matrix_aggregator"] = "invented.aggregator"

    with pytest.raises(analyzer.OneHotMatrixAnalysisError, match=message):
        analyzer._require_current_execution_identity(
            {"source": _source_identity()},
            current=current,
        )


def test_import_and_argument_parsing_are_hardware_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyzer, "_RUNNER", None)
    runner = analyzer._runner()
    monkeypatch.setattr(
        runner.leakage,
        "_live_capture_boundary",
        lambda *_args, **_kwargs: pytest.fail("analysis invoked RF capture"),
    )
    parser = analyzer._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert {"--row", "--output"} <= options
