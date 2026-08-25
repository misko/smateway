import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("matplotlib", reason="localization reports require the report extra")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_localization_report.py"
SPEC = importlib.util.spec_from_file_location("localization_report_renderer_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = renderer
SPEC.loader.exec_module(renderer)


def _source(path: Path, role: str, document: dict[str, object]) -> object:
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    return renderer._load_document(path, role)


def test_released_v021_rf_lengths_are_exact_and_not_journal_values() -> None:
    assert renderer.RELEASED_RF_COMMON_LENGTH_MM == 14.503822
    assert renderer.RELEASED_RF_LENGTH_MM == {
        "ANT1": 22.194973,
        "ANT2": 34.930782,
        "ANT3": 31.500992,
        "ANT4": 36.557345,
        "ANT5": 36.557345,
        "ANT6": 31.500992,
        "ANT7": 34.930819,
        "ANT8": 22.194973,
    }
    assert renderer.RF_RELEASE_REPORT_SHA256 == (
        "d1e4d45bc780cd765bf80cb13e02d459a09ad23ae6c677d1c2e09bf5b738a053"
    )


def test_anchored_provenance_uses_exact_source_bytes(tmp_path: Path) -> None:
    direct_path = tmp_path / "direct.json"
    direct = _source(direct_path, "direct", {"schema": 1, "value": 7})
    anchored = _source(
        tmp_path / "anchored.json",
        "anchored",
        {"schema": 1, "source": {"analysis_sha256": direct.sha256}},
    )

    renderer._require_anchored_source_hash(anchored, direct)

    # Semantically identical JSON with different whitespace has a different immutable identity.
    direct_path.write_text('{"schema":1,"value":7}\n', encoding="utf-8")
    changed_direct = renderer._load_document(direct_path, "changed direct")
    assert changed_direct.document == direct.document
    assert changed_direct.sha256 != direct.sha256
    with pytest.raises(renderer.ReportError, match=r"anchored.*does not hash.*direct\.json"):
        renderer._require_anchored_source_hash(anchored, changed_direct)


def test_wrapped_curve_breaks_only_at_circular_discontinuities() -> None:
    observed = np.asarray((-170.0, -175.0, 179.0, 170.0, -179.0, -160.0))

    masked = renderer._masked_wrapped_curve(observed)

    np.testing.assert_allclose(masked[[0, 1, 3, 5]], observed[[0, 1, 3, 5]])
    assert np.isnan(masked[2])
    assert np.isnan(masked[4])
    with pytest.raises(renderer.ReportError, match="finite vector"):
        renderer._masked_wrapped_curve((0.0, float("nan")))


def test_png_rendering_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    def draw(path: Path) -> None:
        with renderer.plt.rc_context(renderer.STYLE):
            figure = renderer._new_figure(figsize=(3.0, 2.0), layout=None)
            axis = figure.add_subplot(1, 1, 1)
            axis.plot((0.0, 1.0, 2.0), (1.0, -1.0, 0.5), color=renderer.COLORS["primary"])
            axis.set_title("deterministic")
            renderer._save_figure(figure, path)

    draw(first)
    draw(second)

    assert first.read_bytes() == second.read_bytes()
    assert renderer._sha256(first) == renderer._sha256(second)
    assert renderer._png_dimensions(first) == (480, 320)


def test_figure_manifest_hashes_snapshot_and_png_bytes(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"schema": 1}\n', encoding="utf-8")
    png = tmp_path / "figure.png"
    with renderer.plt.rc_context(renderer.STYLE):
        figure = renderer._new_figure(figsize=(1.0, 1.0), layout=None)
        figure.add_subplot(1, 1, 1).scatter((0.0,), (0.0,))
        renderer._save_figure(figure, png)

    manifest = renderer._figure_manifest(snapshot, (png,))

    assert manifest["snapshot_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert manifest["figures"] == [
        {
            "path": "png/figure.png",
            "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
            "byte_size": png.stat().st_size,
            "width_px": 160,
            "height_px": 160,
        }
    ]


@pytest.mark.parametrize("payload", ("[]", "null", "not-json"))
def test_json_sources_fail_closed_when_the_root_is_not_an_object(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "source.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(renderer.ReportError, match="object|valid JSON"):
        renderer._load_document(path, "synthetic source")
