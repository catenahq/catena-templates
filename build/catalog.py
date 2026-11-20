#!/usr/bin/env -S uv run --quiet
"""Print the catalog document (the text of catalog.json) to stdout.

Thin entrypoint; the work lives in lib/render.py. It reads blueprints/*/app.json and
the composes and writes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.model import SourceError, load_meta, load_sources  # noqa: E402
from lib.render import catalog_document  # noqa: E402

if __name__ == "__main__":
    try:
        sys.stdout.write(catalog_document(load_sources(), load_meta()))
    except SourceError as exc:
        print(f"the catalog is not valid:\n{exc}", file=sys.stderr)
        sys.exit(1)
