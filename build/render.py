#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23"]
# ///
"""Render sources/ into blueprints/ + templates.json + catalog.json + index.html.

Thin entrypoint; the work lives in lib/render.py. Run it after every
sources/ edit and commit the generated diff -- CI re-runs it and fails
the PR if the committed artifacts drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.model import SourceError  # noqa: E402
from lib.render import render_all  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(render_all())
    except SourceError as exc:
        print(f"sources are not valid:\n{exc}", file=sys.stderr)
        sys.exit(1)
