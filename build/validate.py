#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23"]
# ///
"""Validate the catalog without writing anything.

Two layers, both non-mutating:

  1. sources/ against sources.schema.json plus the cross-file invariants
     (id matches filename, compose file exists, no duplicate slug, no
     env_managed_key that names nothing).
  2. the COMMITTED templates.json against Schema.json -- the Portainer
     App Templates format. This catches a hand-edit of the generated file
     that a render would have overwritten anyway, before Portainer does.

Use it as the fast pre-commit gate; `render.py` runs the same checks but
rewrites the tree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.model import SourceError, load_sources, validate_output  # noqa: E402
from lib.render import TEMPLATES_JSON  # noqa: E402


def main() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not valid:\n{exc}", file=sys.stderr)
        return 1

    if not TEMPLATES_JSON.exists():
        print(f"missing {TEMPLATES_JSON.name}; run build/render.py", file=sys.stderr)
        return 1
    errors = validate_output(json.loads(TEMPLATES_JSON.read_text()))
    if errors:
        print("templates.json violates Schema.json:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print(f"validate OK: {len(entries)} source(s), templates.json matches Schema.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
