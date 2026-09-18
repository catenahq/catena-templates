#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Enumerate unique container image refs across blueprints/*/docker-compose.yml.

Walks every services.*.image value, dedupes, prints one ref per line on
stdout in stable (sorted) order. Used by .github/workflows/trivy-images.yml
to drive a per-image matrix of trivy image scans.

Exit codes:
  0 -- one or more image refs printed
  2 -- blueprints/ missing or contained no parseable compose files
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS = REPO_ROOT / "blueprints"


def collect_images(blueprints: Path) -> set[str]:
    images: set[str] = set()
    for path in sorted(blueprints.glob("*/docker-compose.yml")):
        with path.open() as fh:
            doc = yaml.safe_load(fh)
        if not isinstance(doc, dict):
            continue
        services = doc.get("services") or {}
        if not isinstance(services, dict):
            continue
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            image = svc.get("image")
            if isinstance(image, str) and image.strip():
                images.add(image.strip())
    return images


def main() -> int:
    if not BLUEPRINTS.is_dir():
        print(f"blueprints/ not found at {BLUEPRINTS}", file=sys.stderr)
        return 2
    images = collect_images(BLUEPRINTS)
    if not images:
        print("no image refs found", file=sys.stderr)
        return 2
    for ref in sorted(images):
        print(ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
