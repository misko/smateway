import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smateway.campaign_protocol import admit_capture, coverage_grid, read_protocol
from smateway.rate_timing import PORTS, RECEIVER_SERIAL, SOURCE_SERIAL, sha256

PROTOCOL = (
    Path(__file__).resolve().parents[1] / "docs/comprehensive_fast_switching/data/protocol-v1.json"
)
SUBGHZ = PROTOCOL.parents[2] / "subghz_915_diagnostic/data/protocol-v1.json"


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


def test_explicit_diagnostic_scope_preserves_unknown_geometry(tmp_path):
    path = fixture(
        tmp_path,
        positions_m=None,
        geometry_status="unconfirmed",
        capture_scope="diagnostic_no_geometry",
    )
    result = admit_capture(PROTOCOL, 2_450_000_000, muted=False, fixture_path=path)
    assert result["positions_m"] is None
    assert result["capture_scope"] == "diagnostic_no_geometry"
    assert not result["surveyed_angle_accuracy_available"]


@pytest.mark.parametrize("scope", [None, "geometry_bound"])
def test_missing_geometry_does_not_silently_become_diagnostic(tmp_path, scope):
    path = fixture(tmp_path, positions_m=None, geometry_status="unconfirmed", capture_scope=scope)
    with pytest.raises(ValueError, match="geometry"):
        admit_capture(PROTOCOL, 2_450_000_000, muted=False, fixture_path=path)


def test_subghz_has_separate_scope_and_fixture_binding(tmp_path):
    path = fixture(
        tmp_path, protocol_sha256=sha256(SUBGHZ),
        approved_intervals_hz=[[902_000_000, 928_000_000]],
    )
    assert admit_capture(SUBGHZ, 915_000_000, muted=False, fixture_path=path)["kind"] == "ota"
    with pytest.raises(ValueError, match="outside"):
        admit_capture(PROTOCOL, 915_000_000, muted=True)
    with pytest.raises(ValueError, match="identity/readiness"):
        admit_capture(SUBGHZ, 915_000_000, muted=False, fixture_path=fixture(tmp_path))


@pytest.mark.parametrize("frequency", [900_000_000, 905_000_000, 925_000_000, 2_450_000_000])
def test_single_subghz_trial_does_not_authorize_a_sweep(frequency):
    with pytest.raises(ValueError, match="outside"):
        admit_capture(SUBGHZ, frequency, muted=True)


@pytest.mark.parametrize("updates", [
    {"allowed_frequencies_hz": [905_000_000, 915_000_000]},
    {"tx_hardware_gain_db": -20}, {"dds_scale": 0.5},
    {"provisional_edge_guard_hz": 0},
    {"band_allocations_hz": [[900_000_000, 928_000_000]]},
])
def test_subghz_protocol_rejects_expanded_limits(tmp_path, updates):
    value = json.loads(SUBGHZ.read_text()) | updates
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="scope"):
        read_protocol(path)


def test_subghz_coverage_has_only_one_candidate():
    rows = coverage_grid(read_protocol(SUBGHZ), [[902_000_000, 928_000_000]])
    pending = [r["frequency_hz"] for r in rows if r["status"] == "pending_measurement"]
    assert pending == [915_000_000]
