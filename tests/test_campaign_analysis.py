import numpy as np

from smateway.campaign_analysis import (
    base_window_pass,
    condition_summary,
    relative_phase_trace,
    window_audit,
)


def test_longer_average_or_too_few_groups_is_not_a_base_pass():
    metrics = {
        "closure": {"passed": True},
        "studies": [{"passed": False, "groups": 60}, {"passed": True, "groups": 30}],
    }
    assert not base_window_pass(metrics)
    metrics["studies"][0] = {"passed": True, "groups": 1}
    assert not base_window_pass(metrics)
    metrics["studies"][0]["groups"] = 60
    assert base_window_pass(metrics)
    assert not base_window_pass(None)


def test_missing_failed_or_future_training_windows_cannot_disappear():
    windows = [
        {
            "training_start_sample": s - 100,
            "training_stop_sample": s,
            "prediction_start_sample": s,
            "prediction_stop_sample": s + 5,
            "status": "analyzed",
        }
        for s in range(100, 115, 5)
    ]
    kwargs = {"sample_rate_hz": 100, "samples_per_channel": 115}
    assert window_audit(windows, **kwargs)["complete"]
    assert not window_audit(windows[1:], **kwargs)["support_matches"]
    windows[1]["status"] = "analysis-failed"
    assert window_audit(windows, **kwargs)["support_matches"]
    assert not window_audit(windows, **kwargs)["complete"]
    windows[1]["training_stop_sample"] += 1
    assert not window_audit(windows, **kwargs)["support_matches"]


def test_controls_brackets_and_disjoint_restarts_are_required():
    main = [{"round": r, "run_json": f"main{r}", "base_pass": True} for r in (1, 2, 3)]
    controls = [
        {"round": r, "dwell_us": d, "run_json": f"control{d}-{r}", "base_pass": True}
        for d in (200, 1000)
        for r in (1, 2, 3)
    ]
    args = {"expected_control_dwells": [200, 1000], "bracket_pass": True, "block_complete": True}
    assert condition_summary(main, controls, **args)["clean_condition_pass"]
    assert not condition_summary(main, controls[:-1], **args)["clean_condition_pass"]
    assert not condition_summary(main, controls, **{**args, "bracket_pass": False})[
        "clean_condition_pass"
    ]
    controls[0]["base_pass"] = False
    assert not condition_summary(main, controls, **args)["clean_condition_pass"]
    controls[0]["base_pass"] = True
    controls[0]["run_json"] = "main1"
    assert not condition_summary(main, controls, **args)["clean_condition_pass"]


def test_common_rotation_is_removed_without_hiding_a_bad_port():
    refs = np.exp(1j * np.arange(6))
    h = refs[None, :] * np.exp(1j * np.arange(3)[:, None])
    np.testing.assert_allclose(relative_phase_trace(h, refs, np.ones(6)), 0, atol=1e-12)
    h[:, 2] *= np.exp(0.5j)
    assert np.max(abs(relative_phase_trace(h, refs, np.ones(6)))) > 20
