from pathlib import Path

from smateway.profile import load_profile, verify_provenance

PROFILE_ROOT = Path("profiles/fast20-v1")


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
