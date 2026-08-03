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

from .model import SourceError, load_meta, load_sources

# `image: postgres:<tag>` only. MariaDB / Mongo / Redis are governed elsewhere.
_PG_PIN = re.compile(r"^\s*image:\s*(postgres:[^\s#]+)\s*(?:#.*)?$", re.MULTILINE)


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
        pins = _PG_PIN.findall(entry.compose_path.read_text())
        if not pins:
            continue
        override = entry.catena.get("postgres_image_override")
        expected = override or default
        for pin in pins:
            checked += 1
            if pin != expected:
                which = "postgres_image_override" if override else "postgres_default_image"
                errors.append(
                    f"{entry.slug}: {entry.compose_path.name} pins {pin!r} but "
                    f"{which} is {expected!r}. Match the central default, or add "
                    f"a justified postgres_image_override if this app cannot run it."
                )
        if override and override == default:
            errors.append(
                f"{entry.slug}: postgres_image_override equals "
                f"postgres_default_image ({default!r}); drop the override."
            )

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
