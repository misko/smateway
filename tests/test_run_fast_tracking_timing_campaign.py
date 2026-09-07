from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_fast_tracking_timing_campaign.py"
SPEC = importlib.util.spec_from_file_location("fast_timing_campaign", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def _run(
    path: Path,
    latency_ms: float | None,
    *,
    frequency_difference_qualified: bool = True,
) -> None:
    rows = [
        {
            "nominal_wall_latency_ms": 1.0,
            "phase_rms_deg_maximum_port": 15.0,
            "phase_rms_deg_maximum_observable_port": 15.0,
            "phase_rms_deg_power_weighted_ports": 15.0,
            "groups_per_port": 20,
            "cycles_averaged": 1,
            "measured_wall_latency_ms": 1.0,
        }
    ]
    if latency_ms is not None:
        rows.append(
            {
                "nominal_wall_latency_ms": latency_ms,
                "phase_rms_deg_maximum_port": 9.5,
                "phase_rms_deg_maximum_observable_port": 9.5,
                "phase_rms_deg_power_weighted_ports": 9.0,
                "groups_per_port": 8,
                "cycles_averaged": 8,
                "measured_wall_latency_ms": latency_ms,
            }
        )
    bearing_rows = [
        {
            "cycles_averaged": row["cycles_averaged"],
            "group_count": row["groups_per_port"],
            "bearing_repeatability_rms_deg": (
                4.0 if row["phase_rms_deg_maximum_observable_port"] <= 10.0 else 8.0
            ),
        }
        for row in rows
    ]
    path.write_text(
        json.dumps(
            {
                "analysis": {
                    "frequency_difference_acceptance": {
                        "qualified": frequency_difference_qualified
                    },
                    "integration_study": rows,
                    "bearing_study": {"integration_study": bearing_rows},
                }
            }
        )
    )


def test_selects_minimum_worst_case_latency(tmp_path: Path) -> None:
    worst_by_dwell = {200: 120.0, 100: 90.0, 50: 110.0, 25: 160.0}
    conditions = []
    for dwell_us in campaign.DWELLS_US:
        for frequency_hz in campaign.SENTINEL_FREQUENCIES_HZ:
            for tx_channel in (0, 1):
                path = tmp_path / f"{dwell_us}-{frequency_hz}-{tx_channel}.json"
                latency = worst_by_dwell[dwell_us] - (frequency_hz % 3_000_000) / 1e6
                _run(path, latency)
                conditions.append(
                    {
                        "dwell_us": dwell_us,
                        "frequency_hz": frequency_hz,
                        "tx_channel": tx_channel,
                        "run_json": str(path),
                    }
                )
    selected, summary = campaign._select_dwell(conditions)
    assert selected == 100
    assert summary["selected_worst_wall_latency_ms"] <= 90.0
    assert all(
        condition["first_phase_rms_10deg_wall_latency_ms"] is not None
        for condition in conditions
    )


def test_rejects_incomplete_or_nonqualifying_ladder(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    _run(path, None)
    conditions = [
        {
            "dwell_us": 200,
            "frequency_hz": campaign.SENTINEL_FREQUENCIES_HZ[0],
            "tx_channel": 0,
            "run_json": str(path),
        }
    ]
    try:
        campaign._select_dwell(conditions)
    except RuntimeError as error:
        assert "no autonomous dwell" in str(error)
    else:
        raise AssertionError("incomplete ladder was accepted")


def test_frequency_difference_failure_cannot_pass_phase_gate(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    _run(path, 10.0, frequency_difference_qualified=False)
    assert campaign._first_passing_latency(json.loads(path.read_text())) is None


def test_capture_retries_quality_failure_and_records_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_directory = tmp_path / "failed"
    failed_directory.mkdir()
    (failed_directory / "run.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "error": {"type": "RuntimeError", "message": "low objective"},
            }
        )
    )
    passed_directory = tmp_path / "passed"
    passed_directory.mkdir()
    (passed_directory / "run.json").write_text(json.dumps({"status": "passed"}))
    calls = iter(
        (
            RuntimeError(f"child failed\nrun_dir={failed_directory}"),
            f"run_dir={passed_directory}\n",
        )
    )

    def fake_run(_command: tuple[str, ...]) -> str:
        result = next(calls)
        if isinstance(result, RuntimeError):
            raise result
        return result

    monkeypatch.setattr(campaign, "_run", fake_run)
    failures: list[dict[str, object]] = []
    path = campaign._capture(
        tmp_path,
        dwell_us=50,
        flash_evidence=tmp_path / "flash.json",
        frequency_hz=5_775_000_000,
        tx_channel=1,
        attempts=3,
        failed_attempts=failures,
    )
    assert path == passed_directory / "run.json"
    assert len(failures) == 1
    assert failures[0]["attempt"] == 1
    assert failures[0]["capture_error"] == {
        "type": "RuntimeError",
        "message": "low objective",
    }


def test_capture_exhausts_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fail(_command: tuple[str, ...]) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("child failed")

    monkeypatch.setattr(campaign, "_run", fail)
    failures: list[dict[str, object]] = []
    with pytest.raises(RuntimeError, match="failed after 2 attempt"):
        campaign._capture(
            tmp_path,
            dwell_us=25,
            flash_evidence=tmp_path / "flash.json",
            frequency_hz=5_800_000_000,
            tx_channel=0,
            attempts=2,
            failed_attempts=failures,
        )
    assert calls == 2
    assert len(failures) == 2
