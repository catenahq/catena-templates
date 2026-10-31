#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=13.0", "resvg-py>=0.5.0", "pyyaml>=6.0"]
# ///
"""Make blueprints/<id>/logo.png from a project's own logo.

    uv run build/logo.py SOURCE blueprints/<id>/logo.png

SOURCE is an SVG or any raster image Pillow reads. The result is 256x256, 8-bit
RGBA and at most 64 KiB, the logo the control panel keeps; the work lives in
lib/logo.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logo import SIDE, make_logo  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: build/logo.py SOURCE OUT.png")
    try:
        size = make_logo(Path(sys.argv[1]), Path(sys.argv[2]))
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"{sys.argv[2]}: {SIDE}x{SIDE}, {size} bytes")
