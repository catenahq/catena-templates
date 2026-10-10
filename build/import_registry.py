#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Import a template registry as catalog entries marked imported and untested.

Thin entrypoint; the shared pipeline is lib/importers/pipeline.py and each
registry format has its adapter beside it.

  uv run build/import_registry.py [--format portainer] [--applint PATH]
                                  [--dry-run] REGISTRY_URL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.importers import pipeline, portainer  # noqa: E402

ADAPTERS = {"portainer": portainer.read}

if __name__ == "__main__":
    sys.exit(pipeline.main(sys.argv[1:], ADAPTERS))
