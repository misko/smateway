import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smateway.campaign_protocol import admit_capture, coverage_grid, read_protocol
from smateway.rate_timing import PORTS, RECEIVER_SERIAL, SOURCE_SERIAL, sha256

PROTOCOL = (
    Path(__file__).resolve().parents[1] / "docs/comprehensive_fast_switching/data/protocol-v1.json"
)


def fixture(tmp_path, **updates):
    value = {
        "ready": True,
        "fixture_id": "synthetic-test",
        "kind": "ota",
        "confirmed_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": sha256(PROTOCOL),
        "receiver_serial": RECEIVER_SERIAL,
        "source_serial": SOURCE_SERIAL,
        "ports": list(PORTS),
        "positions_m": [[i * 0.01, 0] for i in range(6)],
        "operator_confirmation": "test-only",
        "authorization_description": "test-only",
        "approved_intervals_hz": [[2_400_000_000, 2_483_500_000], [5_725_000_000, 5_875_000_000]],
        **updates,
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(value))
    return path


def test_grid_retains_all_edges_and_unapproved_points():
    rows = coverage_grid(read_protocol(PROTOCOL), [[2_400_000_000, 2_483_500_000]])
    assert len(rows) == 252
    lookup = {r["frequency_hz"]: r["status"] for r in rows}
    assert lookup[2_400_000_000] == "excluded_occupied_signal_margin"
    assert lookup[2_482_000_000] == "pending_measurement"
    assert lookup[2_483_000_000] == "excluded_authorization"
    assert lookup[5_800_000_000] == "excluded_authorization"


def test_muted_lower_band_check_does_not_need_ota_permission():
    assert admit_capture(PROTOCOL, 2_450_000_000, muted=True)["scope"].startswith("muted")


def test_old_readiness_flag_cannot_enable_new_rf():
    with pytest.raises(ValueError, match="fresh fixture"):
        admit_capture(PROTOCOL, 5_800_000_000, muted=False)


def test_fresh_fixture_uses_actual_geometry(tmp_path):
    p = fixture(tmp_path)
    result = admit_capture(PROTOCOL, 2_450_000_000, muted=False, fixture_path=p)
    assert result["positions_m"] == [[i * 0.01, 0] for i in range(6)]


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        {"receiver_serial": "another-radio"},
        {"protocol_sha256": "bad"},
        {"positions_m": [[0, 0]]},
        {"ready": False},
    ],
)
def test_bad_or_stale_fixture_is_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        admit_capture(
            PROTOCOL, 2_450_000_000, muted=False, fixture_path=fixture(tmp_path, **changes)
        )


def test_narrower_authorization_wins_over_nominal_ism_allocation(tmp_path):
    with pytest.raises(ValueError, match="authorized frequency"):
        admit_capture(PROTOCOL, 2_495_000_000, muted=False, fixture_path=fixture(tmp_path))
