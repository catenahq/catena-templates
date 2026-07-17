#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Enforce the central Postgres image across every vanilla-postgres template.

The catalog declares one `postgres_default_image` (source/catalog.yml). Every
template whose compose pins a `postgres:<tag>` image MUST match it, UNLESS the
template's catalog entry declares `postgres_image_override` -- reserved for an
app whose upstream cannot run the default major. A bump is then one catalog
edit + the pins in the same PR; this lint fails the build on any drift, so the
central default stays the single source of truth without a render-time
substitution (compose files remain real, directly-deployable stackfiles).

Scope: vanilla `postgres:<tag>` image refs only. MariaDB / Mongo / Redis etc.
are governed elsewhere. Run: uv run build/lint_postgres_pins.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "source" / "catalog.yml"
COMPOSE_DIR = ROOT / "source" / "compose"

# `image: postgres:<tag>` lines only. Trailing comments / whitespace tolerated.
_PG_PIN = re.compile(r"^\s*image:\s*(postgres:[^\s#]+)\s*(?:#.*)?$", re.MULTILINE)


def main() -> int:
    catalog = yaml.safe_load(CATALOG.read_text())
    default = catalog.get("postgres_default_image")
    if not default:
        print(
            "lint: source/catalog.yml is missing the top-level "
            "`postgres_default_image` key.",
            file=sys.stderr,
        )
        return 1
    entries = catalog.get("catena_template_catalog") or []

    errors: list[str] = []
    checked = 0
    for entry in entries:
        cf = entry.get("compose_file")
        if not cf:
            continue
        compose = COMPOSE_DIR / Path(str(cf)).name
        if not compose.exists():
            continue
        pins = _PG_PIN.findall(compose.read_text())
        if not pins:
            continue
        override = entry.get("postgres_image_override")
        expected = override or default
        for pin in pins:
            checked += 1
            if pin != expected:
                which = (
                    "postgres_image_override"
                    if override
                    else "postgres_default_image"
                )
                errors.append(
                    f"{entry.get('id', compose.name)}: {compose.name} pins "
                    f"{pin!r} but {which} is {expected!r}. Match the central "
                    f"default, or add a justified postgres_image_override to "
                    f"the catalog entry if this app cannot run it."
                )
        if override and override == default:
            errors.append(
                f"{entry.get('id', compose.name)}: postgres_image_override "
                f"equals postgres_default_image ({default!r}); drop the "
                f"override -- it is already the default."
            )

    if errors:
        print("postgres-pin policy violations:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(
        f"postgres pins OK: {checked} vanilla-postgres pin(s) == "
        f"{default!r} (or a declared override)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
