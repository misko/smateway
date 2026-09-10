import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from screen_915_diagnostic import static_usable  # noqa: E402


@pytest.mark.parametrize("coherence,rms,peak,expected", [
    (0.95, 1, 900, True), (0.8, 1, 900, False), (0.95, 6, 900, False),
    (0.95, 1, 1600, False), (float("nan"), 1, 900, False),
])
def test_static_screen_requires_coherence_stability_and_headroom(coherence, rms, peak, expected):
    run = {
        "status": "passed",
        "capture": {"peak_component_counts": [peak, 100], "clipped_samples": [0, 0]},
        "safety": {"final_source_mute": {"passed": True}},
        "reference": {"coherence": coherence, "phase_rms_10ms_deg": rms},
    }
    assert static_usable(run) is expected


def test_missing_static_reference_is_not_usable():
    assert not static_usable({})
