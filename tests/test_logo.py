"""Unit tests for lib/logo.py: any source becomes a square RGBA PNG the
loader accepts. Needs pillow and resvg-py (`make test` adds them)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIL = pytest.importorskip("PIL")
pytest.importorskip("resvg_py")

from PIL import Image  # noqa: E402

from lib import logo, model  # noqa: E402

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">'
       '<rect x="10" y="5" width="80" height="30" fill="#c2410c"/></svg>')
SVG_MM = SVG.replace("<svg ", '<svg width="120mm" height="48mm" ', 1)


def _check(out: Path, size: int) -> None:
    data = out.read_bytes()
    assert len(data) == size <= model.LOGO_MAX_BYTES
    assert model.png_header(data) == (logo.SIDE, logo.SIDE, 8, 6)


def test_an_svg_becomes_a_centred_256_px_rgba_png(tmp_path):
    src, out = tmp_path / "in.svg", tmp_path / "out.png"
    src.write_text(SVG)
    _check(out, logo.make_logo(src, out))
    image = Image.open(out)
    box = image.getchannel("A").getbbox()
    assert box[2] - box[0] == logo.FIT_PX
    assert box[0] == (logo.SIDE - logo.FIT_PX) // 2


def test_a_png_with_a_transparent_margin_is_trimmed_and_fitted(tmp_path):
    src, out = tmp_path / "in.png", tmp_path / "out.png"
    picture = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    picture.paste(Image.new("RGBA", (100, 100), (200, 20, 20, 255)), (50, 50))
    picture.save(src)
    _check(out, logo.make_logo(src, out))
    box = Image.open(out).getchannel("A").getbbox()
    assert (box[2] - box[0], box[3] - box[1]) == (logo.FIT_PX, logo.FIT_PX)


def test_an_svg_sized_in_millimetres_is_rendered_from_its_view_box(tmp_path):
    src, out = tmp_path / "in.svg", tmp_path / "out.png"
    src.write_text(SVG_MM)
    _check(out, logo.make_logo(src, out))


def test_a_fully_transparent_picture_is_refused(tmp_path):
    src, out = tmp_path / "in.png", tmp_path / "out.png"
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(src)
    with pytest.raises(ValueError, match="fully transparent"):
        logo.make_logo(src, out)
    assert not out.exists()


def test_a_result_over_the_panel_limit_is_refused_and_not_kept(tmp_path, monkeypatch):
    src, out = tmp_path / "in.png", tmp_path / "out.png"
    Image.new("RGBA", (64, 64), (200, 20, 20, 255)).save(src)
    monkeypatch.setattr(logo, "LOGO_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="over 10"):
        logo.make_logo(src, out)
    assert not out.exists()
