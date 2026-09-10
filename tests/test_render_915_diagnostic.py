import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_915_diagnostic import raw_power, verified_run  # noqa: E402

from smateway.rate_timing import sha256


def test_raw_power_is_complex_power_not_component_power(tmp_path):
    path = tmp_path / "iq.cf32"
    np.array([3 + 4j, 3 - 4j], dtype=np.complex64).tofile(path)
    assert raw_power({"path": str(path)}) == 25


def test_report_audit_rejects_modified_raw_iq(tmp_path):
    raw = tmp_path / "iq.cf32"
    np.array([1 + 0j], dtype=np.complex64).tofile(raw)
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"status": "passed", "configuration": {
        "frequency_hz": 915_000_000, "name": "A", "sample_rate_hz": 2_000_000,
        "bandwidth_hz": 1_600_000,
    }, "capture": {"raw": [{
        "path": str(raw), "sha256": sha256(raw), "bytes": raw.stat().st_size,
    }]}}))
    item = {"run_json": str(path), "sha256": sha256(path)}
    assert verified_run(item)["status"] == "passed"
    np.array([2 + 0j], dtype=np.complex64).tofile(raw)
    with pytest.raises(ValueError, match="raw IQ hash"):
        verified_run(item)


def test_report_audit_rejects_modified_run(tmp_path):
    path = tmp_path / "run.json"
    path.write_text('{}')
    with pytest.raises(ValueError, match="run hash"):
        verified_run({"run_json": str(path), "sha256": "wrong"})


def test_report_rejects_other_frequency_even_with_matching_hash(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"status": "passed", "configuration": {
        "frequency_hz": 2_475_000_000, "name": "A", "sample_rate_hz": 2_000_000,
        "bandwidth_hz": 1_600_000,
    }}))
    with pytest.raises(ValueError, match="915 MHz"):
        verified_run({"run_json": str(path), "sha256": sha256(path)})
