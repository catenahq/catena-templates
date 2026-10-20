"""Which apps a set of changed paths touches.

An app is `blueprints/<id>/**` plus `sources/<id>.json`. Anything else in the
repo (the generated catalog files, the workflows, the library) belongs to no
app, and `sources/_meta.json` is the catalog's, not an app's.
"""
from __future__ import annotations

import re
from typing import Iterable

SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
BLUEPRINTS_PREFIX = "blueprints/"
SOURCES_PREFIX = "sources/"
META_NAME = "_meta.json"


def app_id(path: str) -> str | None:
    """The app a repo-relative path belongs to, or None."""
    if path.startswith(BLUEPRINTS_PREFIX):
        head, sep, _ = path[len(BLUEPRINTS_PREFIX):].partition("/")
        return head if sep and head else None
    if path.startswith(SOURCES_PREFIX):
        name = path[len(SOURCES_PREFIX):]
        if "/" in name or not name.endswith(".json") or name == META_NAME:
            return None
        return name[: -len(".json")]
    return None


def app_ids(paths: Iterable[str]) -> list[str]:
    """The distinct app ids the paths touch, sorted."""
    return sorted({i for i in (app_id(p.strip()) for p in paths) if i})


def slug_errors(ids: Iterable[str]) -> list[str]:
    """Ids that are not the plain slug sources.schema.json requires. A pull
    request names its files, so an id is untrusted until it passes."""
    return [f"{i!r} is not a plain app id (lowercase letters, digits, hyphens)"
            for i in ids if not SLUG.fullmatch(i)]
