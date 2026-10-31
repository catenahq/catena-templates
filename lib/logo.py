"""Make a catalog logo: a square PNG the control panel keeps.

The picture is trimmed of transparent margins, fitted in FIT_PX centred on a
transparent SIDE x SIDE canvas and written as 8-bit RGBA. The limits are
those lib/model.py checks. Needs pillow and resvg-py, so the render never
imports it; build/logo.py is the entrypoint.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image

from lib.model import LOGO_MAX_BYTES

SIDE = 256
FIT_PX = 224
_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SIZE_ATTR_RE = re.compile(r"""\s(?:width|height)\s*=\s*(?:"[^"]*"|'[^']*')""")


def _svg_without_size(text: str) -> str:
    """The SVG with the root's width and height dropped when it has a viewBox:
    the renderer then sizes it from the viewBox, and a size with units
    (`120mm`) is refused."""
    root = _SVG_ROOT_RE.search(text)
    if root is None or "viewBox" not in root.group(0):
        return text
    bare = _SIZE_ATTR_RE.sub("", root.group(0))
    return text[:root.start()] + bare + text[root.end():]


def _open(source: Path) -> Image.Image:
    data = source.read_bytes()
    if b"<svg" in data[:4096]:
        import resvg_py

        svg = _svg_without_size(data.decode("utf-8"))
        png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=SIDE * 4))
        return Image.open(io.BytesIO(png))
    return Image.open(io.BytesIO(data))


def make_logo(source: Path, out: Path) -> int:
    """Write the logo made from `source` to `out`; return its size in bytes.
    A fully transparent picture, or a result over the panel's limit, raises
    ValueError."""
    image = _open(source).convert("RGBA")
    box = image.getchannel("A").getbbox()
    if box is None:
        raise ValueError(f"{source}: the picture is fully transparent")
    image = image.crop(box)
    scale = FIT_PX / max(image.size)
    image = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS)
    canvas = Image.new("RGBA", (SIDE, SIDE), (0, 0, 0, 0))
    canvas.paste(image, ((SIDE - image.width) // 2, (SIDE - image.height) // 2), image)
    canvas.save(out, optimize=True)
    if out.stat().st_size > LOGO_MAX_BYTES:
        reduced = canvas.quantize(256, method=Image.Quantize.FASTOCTREE).convert("RGBA")
        reduced.save(out, optimize=True)
    size = out.stat().st_size
    if size > LOGO_MAX_BYTES:
        out.unlink()
        raise ValueError(f"{source}: the logo is {size} bytes, over {LOGO_MAX_BYTES}")
    return size
