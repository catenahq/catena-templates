#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Entrypoint for the per-app pull-request checks. Logic in lib/app_checks.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.app_checks import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
