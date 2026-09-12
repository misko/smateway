import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resume_5ms_completion as resume  # noqa: E402


@pytest.mark.parametrize("completed_count", [4, 6])
def test_nested_continuation_retains_failures_and_original_readiness(
    monkeypatch, tmp_path, completed_count
):
    parent, root = tmp_path / "parent", tmp_path / "resume"
    parent.mkdir()
    save = resume.campaign.prior.save
    good = []
    for i, (f, c, _g, _d) in enumerate(resume.campaign.BLOCKS[:completed_count]):
        path = parent / f"block-{i}.json"
        save(path, {"status": "diagnostic-complete"})
        good.append(
            {
                "frequency_hz": f,
                "configuration": c,
                "status": "diagnostic-complete",
                "block_json": str(path),
                "sha256": resume.sha256(path),
            }
        )
    failed = {"status": "failed", "block_json": "second-interruption"}
    save(parent / "blocks.json", {"status": "failed", "blocks": good + [failed]})
    save(parent / "screens.json", {"status": "complete"})
    old_failure = {"status": "failed", "block_json": "original-interruption"}
    save(
        parent / "plan.json",
        {
            "blocks": [
                {"frequency_hz": f, "configuration": c, "gain_db": g, "dwells_us": list(d)}
                for f, c, g, d in resume.campaign.BLOCKS
            ],
            "dense": {},
            "continuation": {"retained_incomplete_blocks": [old_failure]},
        },
    )
    for name in ("fixture-dual-band.json", "fixture-915.json"):
        save(parent / name, {"confirmed_at": "original-user-confirmation"})
    (parent / "initial-selector-flash.bin").write_bytes(b"original")
    monkeypatch.setattr(resume.campaign.prior, "binding", lambda *a: None)
    monkeypatch.setenv("SMATEWAY_STAGE_IQ_IN_RAM", "1")
    assert resume.initialize(parent, root) == good
    plan = resume.load(root / "plan.json")
    assert plan["continuation"]["retained_incomplete_blocks"] == [old_failure, failed]
    assert plan["continuation"]["host_iq_staging"]["enabled"]
    assert (
        resume.load(root / "fixture-dual-band.json")["confirmed_at"] == "original-user-confirmation"
    )
    with pytest.raises(FileExistsError):
        resume.initialize(parent, root)
