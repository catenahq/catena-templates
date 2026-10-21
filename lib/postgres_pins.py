"""Hold every vanilla-postgres pin to a version the update engine can order.

Each app pins its own Postgres and moves it itself. A partial pin
(`postgres:18.6-alpine`) is ordered only by a `partial_line` rule, which the
app's `x-catena.images.<service>.rule` declares; a pin with no rule and no full
`X.Y.Z` version is never updated, and an app that is never updated keeps its
vulnerabilities.
"""
from __future__ import annotations

import re

import yaml

from .model import COMPOSE_NAME, SourceError, load_sources

_FULL_VERSION = re.compile(r"^v?\d+\.\d+\.\d+")


def pin_errors(slug: str, body: str) -> tuple[int, list[str]]:
    """How many vanilla-postgres pins one compose body has, and the
    violations among them. The registry importer records these on an
    imported entry before the lint sees it."""
    doc = yaml.safe_load(body) or {}
    images = (doc.get("x-catena") or {}).get("images") or {}
    count, errors = 0, []
    for name, service in (doc.get("services") or {}).items():
        ref = str((service or {}).get("image") or "").split("@", 1)[0]
        repo, _, tag = ref.rpartition(":")
        if repo != "postgres":
            continue
        count += 1
        kind = ((images.get(name) or {}).get("rule") or {}).get("kind")
        if not _FULL_VERSION.match(tag) and kind != "partial_line":
            errors.append(
                f"{slug}: {COMPOSE_NAME} service {name} pins {ref!r}, a tag the update "
                f"engine orders only with x-catena.images.{name}.rule of kind partial_line."
            )
    return count, errors


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    errors: list[str] = []
    checked = 0
    for entry in entries:
        count, found = pin_errors(entry.slug, entry.compose_path.read_text())
        checked += count
        errors.extend(found)

    if errors:
        print("postgres-pin policy violations:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"postgres pins OK: {checked} vanilla-postgres pin(s) the engine can order.")
    return 0
