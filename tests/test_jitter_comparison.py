import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_jitter_comparison as analysis  # noqa: E402


def write_capture(tmp_path):
    raw = tmp_path / "raw.cf32"
    np.ones(16, dtype=np.complex64).tofile(raw)
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "status": "passed",
                "capture": {
                    "samples_per_channel": 16,
                    "raw": [{"path": str(raw), "sha256": analysis.sha256(raw)}],
                },
            }
        )
    )
    return {"run_json": str(run), "sha256": analysis.sha256(run)}, raw


def test_common_phase_rotation_cancels_without_per_port_fit():
    values = np.exp(1j * np.arange(6) / 10)
    assert np.allclose(
        analysis.relative_phase(values), analysis.relative_phase(values * np.exp(1.7j))
    )
    values[3] = 0
    with pytest.raises(ValueError, match="zero"):
        analysis.relative_phase(values)


def test_raw_and_run_integrity(tmp_path):
    item, raw = write_capture(tmp_path)
    assert analysis.verified_run(item)["status"] == "passed"
    with pytest.raises(ValueError, match="Run-record"):
        analysis.verified_run({**item, "sha256": "not-the-hash"})
    raw.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Raw IQ"):
        analysis.verified_run(item)


def test_cached_dense_requires_current_policy_and_revalidates_raw(tmp_path, monkeypatch):
    item, raw = write_capture(tmp_path)
    contract = {"policy": "current"}
    monkeypatch.setattr(analysis, "source_contract", lambda _root: contract)
    output = tmp_path / "jitter-dense-analysis.json"
    output.write_text(
        json.dumps(
            {"run_sha256": item["sha256"], "source_contract": contract, "status": "analyzed"}
        )
    )
    assert analysis.analyze_dense_run(item, tmp_path)["status"] == "analyzed"
    contract["policy"] = "different"
    with pytest.raises(ValueError, match="Cached dense"):
        analysis.analyze_dense_run(item, tmp_path)
    raw.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Raw IQ"):
        analysis.analyze_dense_run(item, tmp_path)
