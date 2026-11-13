#!/usr/bin/env -S uv run --quiet
"""Validate the catalog without writing anything.

Each blueprints/<id>/app.json against app.schema.json plus the cross-file
invariants (compose file exists, its block declares an env, logo and card agree,
no vps.homepage.* label, catalog order).

Use it as the fast pre-commit gate; `render.py` runs the same checks but
rewrites the tree.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.model import SourceError, load_sources  # noqa: E402


def main() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"the catalog is not valid:\n{exc}", file=sys.stderr)
        return 1

    print(f"validate OK: {len(entries)} app(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
