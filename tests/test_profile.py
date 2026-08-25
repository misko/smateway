from pathlib import Path

from smateway.profile import load_profile, verify_provenance

PROFILE_ROOT = Path("profiles/fast20-v1")
PHASE_PROFILE_ROOT = Path("profiles/phase20-v1")


def test_generated_profile_is_internally_consistent() -> None:
    profile = load_profile(PROFILE_ROOT / "control_profile.json")

    assert profile.profile_id
    assert profile.revision >= 1
    assert profile.all_off_code not in {state.gpio_code for state in profile.states}
    assert all(state.window_ms[0] < state.dwell_ms < state.window_ms[1] for state in profile.states)
    assert profile.recommended_capture_ms >= profile.nominal_cycle_ms * 2


def test_profile_matches_recorded_provenance() -> None:
    assert verify_provenance(
        PROFILE_ROOT / "control_profile.json", PROFILE_ROOT / "provenance.json"
    )


def test_phase20_profile_preserves_truth_table_and_equal_dwell() -> None:
    production = load_profile(PROFILE_ROOT / "control_profile.json")
    phase = load_profile(PHASE_PROFILE_ROOT / "control_profile.json")

    assert phase.profile_id == "phase20-v1"
    assert phase.all_off_code == production.all_off_code == 0x8
    assert phase.guard_ms == production.guard_ms == 5
    assert phase.marker_body_ms == 20
    assert phase.nominal_cycle_ms == 220
    assert [state.name for state in phase.states] == [
        state.name for state in production.states
    ]
    assert [state.gpio_code for state in phase.states] == [
        state.gpio_code for state in production.states
    ]
    assert {state.dwell_ms for state in phase.states} == {20}
