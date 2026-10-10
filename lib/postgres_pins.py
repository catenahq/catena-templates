"""Enforce the central Postgres image across every vanilla-postgres template.

`sources/_meta.json` declares one `postgres_default_image`. Every compose
that pins `postgres:<tag>` must match it, unless that template declares
`x-catena.postgres_image_override` -- reserved for an app whose upstream
cannot run the default major.

Compose files stay real, directly-deployable stackfiles (no render-time
substitution), so a bump is one _meta.json edit plus the pins in the same
change, and this gate fails the build on any drift.
"""
from __future__ import annotations

import re

from .model import COMPOSE_NAME, SourceError, load_meta, load_sources

# `image: postgres:<tag>` only. MariaDB / Mongo / Redis are governed elsewhere.
_PG_PIN = re.compile(r"^\s*image:\s*(postgres:[^\s#]+)\s*(?:#.*)?$", re.MULTILINE)


def pin_errors(slug: str, body: str, *, override: str | None,
               default: str) -> tuple[int, list[str]]:
    """How many vanilla-postgres pins one compose body has, and the
    violations among them. The registry importer records these on an
    imported entry before the lint sees it."""
    pins = _PG_PIN.findall(body)
    if not pins:
        return 0, []
    expected = override or default
    which = "postgres_image_override" if override else "postgres_default_image"
    errors = [
        f"{slug}: {COMPOSE_NAME} pins {pin!r} but {which} is {expected!r}. "
        f"Match the central default, or add a justified "
        f"postgres_image_override if this app cannot run it."
        for pin in pins if pin != expected
    ]
    if override and override == default:
        errors.append(
            f"{slug}: postgres_image_override equals "
            f"postgres_default_image ({default!r}); drop the override."
        )
    return len(pins), errors


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    default = load_meta().get("postgres_default_image")
    if not default:
        print("sources/_meta.json is missing `postgres_default_image`.")
        return 1

    errors: list[str] = []
    checked = 0
    for entry in entries:
        count, found = pin_errors(
            entry.slug, entry.compose_path.read_text(),
            override=entry.catena.get("postgres_image_override"), default=default)
        checked += count
        errors.extend(found)

    if errors:
        print("postgres-pin policy violations:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(
        f"postgres pins OK: {checked} vanilla-postgres pin(s) == {default!r} "
        f"(or a declared override)."
    )
    return 0
