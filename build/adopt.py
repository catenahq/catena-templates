#!/usr/bin/env -S uv run --quiet
"""Entrypoint: adopt.py <base repo> <fork repo> <fork head sha>. Needs GH_TOKEN. Logic in lib/adopt.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.adopt import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
