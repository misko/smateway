import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "headroom", Path(__file__).resolve().parents[1] / "scripts/screen_comprehensive_headroom.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "peaks,clips,status,mute,expected",
    [
        ([1599, 42], [0, 0], "passed", True, True),
        ([1600, 42], [0, 0], "passed", True, False),
        ([42, 2048], [0, 1], "failed", True, False),
        ([42, 43], [0, 0], "passed", False, False),
        ([], [], "failed", True, False),
        ([42, float("nan")], [0, 0], "passed", True, False),
    ],
)
def test_headroom_requires_both_channels_and_exact_cleanup(peaks, clips, status, mute, expected):
    run = {
        "status": status,
        "capture": {"peak_component_counts": peaks, "clipped_samples": clips},
        "safety": {"final_source_mute": {"passed": mute}},
    }
    assert MODULE.headroom_pass(run) is expected


def test_capture_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("SMATEWAY_CAPTURE_TIMEOUT_S", "121")
    with pytest.raises(ValueError, match="45–120"):
        MODULE.capture([])


def test_supervisor_accepts_slower_complete_process_and_records_deadline(tmp_path, monkeypatch):
    import json

    run = {"status": "passed", "safety": {"final_source_mute": {"passed": True}}}
    (tmp_path / "run.json").write_text(json.dumps(run))
    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout):
            calls.append(timeout)
            return f"run_dir={tmp_path}\n", None

    monkeypatch.setenv("SMATEWAY_CAPTURE_TIMEOUT_S", "90")
    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *a, **kw: Process())
    _path, actual = MODULE.capture([])
    assert actual == run and calls == [90]
    evidence = json.loads((tmp_path / "capture-supervisor.json").read_text())
    assert evidence["timeout_s"] == 90 and evidence["returncode"] == 0


def test_real_timeout_is_still_failure_and_preserves_stdout(tmp_path, monkeypatch):
    class Process:
        returncode = -15
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise MODULE.subprocess.TimeoutExpired("synthetic", timeout)
            return "capture stalled", None

        def terminate(self):
            pass

    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *a, **kw: Process())
    with pytest.raises(MODULE.subprocess.TimeoutExpired):
        MODULE.capture(["--output-root", str(tmp_path)])
    assert len(list((tmp_path / "supervisor-errors").glob("*.json"))) == 1


def metadata_failure():
    return {
        "status": "failed",
        "error": {"type": "OSError", "errno": 61},
        "error_traceback": "iio_metadata.py\n self._buffer.refill()",
    }


def test_only_metadata_enodata_is_retryable():
    assert MODULE.retryable_metadata_error(metadata_failure())
    for field, value in (("errno", 110), ("type", "RuntimeError")):
        run = metadata_failure()
        run["error"][field] = value
        assert not MODULE.retryable_metadata_error(run)
    run = metadata_failure()
    run["error_traceback"] = "unrelated file read"
    assert not MODULE.retryable_metadata_error(run)


@pytest.mark.parametrize("last_status", ["passed", "failed"])
def test_bounded_attempts_preserve_all_records(tmp_path, monkeypatch, last_status):
    import json

    monkeypatch.setenv("SMATEWAY_METADATA_RETRIES", "2")
    calls = []

    def once(command):
        number = len(calls)
        directory = tmp_path / f"run-{number}"
        directory.mkdir()
        run = metadata_failure()
        if number == 2 and last_status == "passed":
            run = {"status": "passed", "error": None}
        path = directory / "run.json"
        path.write_text(json.dumps(run))
        calls.append(path)
        return path, run

    monkeypatch.setattr(MODULE, "capture_once", once)
    path, run = MODULE.capture(["--output-root", str(tmp_path)])
    assert len(calls) == 3 and run["status"] == last_status
    evidence = MODULE.load(path.parent / "capture-attempts.json")
    assert len(evidence["attempts"]) == 3
    assert evidence["selected_run_json"] == str(path)
    assert all(p.exists() for p in calls)
    assert run["transport_attempts"]["sha256"] == MODULE.sha256(
        path.parent / "capture-attempts.json"
    )


def test_quality_failure_and_unsafe_cleanup_are_never_retried(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("SMATEWAY_METADATA_RETRIES", "2")
    calls = []
    run = {"status": "failed", "error": {"type": "RuntimeError", "message": "clipping"}}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run))

    def once(command):
        calls.append(1)
        return path, run

    monkeypatch.setattr(MODULE, "capture_once", once)
    assert MODULE.capture(["--output-root", str(tmp_path)])[1]["status"] == "failed"
    assert len(calls) == 1

    def unsafe(command):
        raise RuntimeError("capture cleanup failed")

    monkeypatch.setattr(MODULE, "capture_once", unsafe)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        MODULE.capture(["--output-root", str(tmp_path)])


def test_retry_count_cannot_grow_unbounded(monkeypatch):
    monkeypatch.setenv("SMATEWAY_METADATA_RETRIES", "3")
    with pytest.raises(ValueError, match="0–2"):
        MODULE.capture([])
