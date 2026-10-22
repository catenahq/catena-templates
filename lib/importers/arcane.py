"""Arcane's template registry (github.com/getarcaneapp/templates, served at
registry.getarcane.app): one registry.json listing the templates, each stored
in the repository at templates/<id>/ as compose.yaml and .env.example.

A registry document is {"name", "description", "author", "url", "version",
"templates": [...]} and a template is {"id", "name", "description", "version",
"author", "compose_url", "env_url", "documentation_url", "icon_url",
"content_hash", "tags"}.

The compose file and the env file are copied from the repository, at the
commit its default branch is at when the wave runs (recorded in the entry's
origin), so the repository's licence applies. The repository is named by
`documentation_url`; the files' paths are those of `compose_url` and `env_url`
on the registry site, which mirrors the repository's layout.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from .pipeline import Candidate, github_repo, plain, slugify


def read(doc: Any, registry_url: str) -> list[Candidate]:
    if (not isinstance(doc, dict) or not isinstance(doc.get("templates"), list)
            or not doc.get("name") or not doc.get("version")):
        raise ValueError("not an Arcane template registry")
    return [_candidate(t) for t in doc["templates"] if isinstance(t, dict)]


def _path(url: Any) -> str:
    return urllib.parse.urlsplit(str(url or "")).path.strip("/")


def _candidate(t: dict[str, Any]) -> Candidate:
    title = plain(t.get("name"))
    docs = str(t.get("documentation_url") or "").strip()
    repo = github_repo(docs)
    c = Candidate(
        entry=title or str(t.get("id") or "?"),
        slug=slugify(str(t.get("id") or title)),
        title=title,
        description=plain(t.get("description")) or title,
        categories=[tag for tag in (plain(x).capitalize() for x in t.get("tags") or []) if tag],
        source=f"https://github.com/{repo.path}" if repo else docs,
        upstream_url=docs,
    )
    compose = _path(t.get("compose_url"))
    if not compose:
        c.skip = "the entry names no compose file"
    elif repo is None:
        c.licence_problem = (f"the entry's repository {docs or '(none)'} is not on GitHub, so its "
                             f"licence is unknown")
    else:
        c.licence_repo = c.compose_repo = repo
        c.compose_path = compose
        c.env_path = _path(t.get("env_url"))
    return c
