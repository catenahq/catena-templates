#!/usr/bin/env -S uv run --quiet
"""Print the static catalog preview (index.html) to stdout.

Thin entrypoint; the work lives in lib/render.py. It reads blueprints/*/app.json and
the composes and writes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.model import SourceError, load_meta, load_sources  # noqa: E402
from lib.render import render_index_html  # noqa: E402

if __name__ == "__main__":
    try:
        sys.stdout.write(render_index_html(load_sources(), load_meta()))
    except SourceError as exc:
        print(f"the catalog is not valid:\n{exc}", file=sys.stderr)
        sys.exit(1)
