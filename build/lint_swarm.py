#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Entrypoint for the swarm-compatibility lint. Logic in lib/swarm_lint.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.swarm_lint import lint_all  # noqa: E402

if __name__ == "__main__":
    sys.exit(lint_all())
