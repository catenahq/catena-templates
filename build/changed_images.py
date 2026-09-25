#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Print the image refs a change moves, each paired with the ref it replaces.

Compares every blueprints/*/docker-compose.yml in the working tree with the
same file at BASE_REF and prints a JSON array of
{"image": <new ref>, "base_image": <replaced ref, or "">} on stdout (logic in
lib/images.py). Used by .github/workflows/trivy-images.yml to scan and gate
only what a pull request changes.

Usage: build/changed_images.py BASE_REF
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.images import changed_images  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    base_ref = sys.argv[1]
    head = {p.parent.name: p.read_text(encoding="utf-8")
            for p in sorted((REPO_ROOT / "blueprints").glob("*/docker-compose.yml"))}
    base: dict[str, str] = {}
    for path in git("ls-tree", "-r", "--name-only", base_ref, "blueprints/").splitlines():
        parts = path.split("/")
        if len(parts) == 3 and parts[2] == "docker-compose.yml":
            base[parts[1]] = git("show", f"{base_ref}:{path}")
    print(json.dumps(changed_images(head, base)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
