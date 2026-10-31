#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Entrypoint to unpack an app's compose into neat files (lib/pack.py) and to export it
for bare Docker: `unpack --plain` and `plain-check` (lib/plain.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import pack, plain  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    plain_run = argv[:1] == ["plain-check"] or "--plain" in argv
    sys.exit((plain.main if plain_run else pack.main)(argv))
