"""Conservative reporting gates; never change the frozen capture/decoder policy."""

from collections import Counter

import numpy as np


def base_window_pass(metrics):
    """A longer average cannot substitute for the base observation budget."""
    studies = (metrics or {}).get("studies", [])
    return bool(
        studies
        and studies[0].get("groups", 0) >= 8
        and studies[0].get("passed", False)
        and metrics.get("closure", {}).get("passed", False)
    )


def window_audit(windows, *, sample_rate_hz, samples_per_channel):
    """Require all 50 ms windows after the 1 s preamble, including failed ones."""
    hop = sample_rate_hz // 20
    expected = list(range(sample_rate_hz, samples_per_channel - hop + 1, hop))
    support = len(windows) == len(expected) and bool(expected)
    for start, row in zip(expected, windows, strict=False):
        support &= (
            row.get("training_start_sample") == start - sample_rate_hz
            and row.get("training_stop_sample") == start
            and row.get("prediction_start_sample") == start
            and row.get("prediction_stop_sample") == start + hop
        )
    return {
        "expected_windows": len(expected),
        "recorded_windows": len(windows),
        "failed_windows": sum(w.get("status") != "analyzed" for w in windows),
        "support_matches": bool(support),
        "complete": bool(support and all(w.get("status") == "analyzed" for w in windows)),
    }


def condition_summary(main, controls, *, expected_control_dwells, bracket_pass, block_complete):
    """Three distinct main trials plus every planned control and reference bracket."""
    main_complete = (
        len(main) == 3
        and {r["round"] for r in main} == {1, 2, 3}
        and len({r["run_json"] for r in main}) == 3
    )
    expected = Counter((d, r) for d in expected_control_dwells for r in (1, 2, 3))
    actual = Counter((r["dwell_us"], r["round"]) for r in controls)
    paths = [r["run_json"] for r in [*main, *controls]]
    controls_complete = actual == expected and bool(expected) and len(paths) == len(set(paths))
    controls_pass = controls_complete and all(r["base_pass"] for r in controls)
    passes = sum(r["base_pass"] for r in main)
    return {
        "main_attempts": len(main),
        "main_passes": passes,
        "three_independent_main_trials": main_complete,
        "control_attempts": len(controls),
        "control_passes": sum(r["base_pass"] for r in controls),
        "controls_complete": controls_complete,
        "controls_pass": bool(controls_pass),
        "bracket_pass": bool(bracket_pass),
        "clean_condition_pass": bool(
            main_complete and passes == 3 and controls_pass and bracket_pass and block_complete
        ),
    }


def relative_phase_trace(transfers, references, weights):
    """Per-window common-phase removal, without fitting individual port offsets."""
    matrix, refs = np.asarray(transfers, complex), np.asarray(references, complex)
    weights = np.asarray(weights, float)
    if matrix.ndim != 2 or matrix.shape[1] != 6 or refs.shape != (6,) or weights.shape != (6,):
        raise ValueError("six aligned ports required")
    if not np.all(np.isfinite(matrix)) or np.any(abs(refs) == 0):
        raise ValueError("finite transfers and nonzero references required")
    ratio = matrix / refs
    common = np.angle(np.sum(weights * np.exp(1j * np.angle(ratio)), axis=1))
    return np.angle(ratio * np.exp(-1j * common[:, None]), deg=True)
