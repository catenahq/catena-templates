"""One catalog app per upstream project.

An entry is identified by keys: the repository of the image its main address
runs (tag, digest and Docker Hub's spelling removed) and the upstream source
repository when the registry or a listing names it. Two entries sharing a key
are one app. Among the entries of one group the richest seeds the new app:
the most settings, a health check, sign-in labels. The others, and every
entry that shares a key with an app already in the catalog, become alias
records: recorded, not imported.

An alias of an app being imported stays in that app's `origin.aliases`. An
alias of an app already in the catalog is kept in sources/_aliases.json, which
a later run reads so it does not weigh that entry again.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from .. import model
from . import pipeline

ALIASES_NAME = "_aliases.json"

# What the three parts of an entry's richness are worth, in settings.
HEALTH_WEIGHT = 3
SIGN_IN_WEIGHT = 3


def image_key(ref: str) -> str:
    """The image's repository as one spelling, or "" for a reference a
    variable sets."""
    name = str(ref).split("@", 1)[0].strip().lower()
    if not name or name.startswith(("$", "{")):
        return ""
    if name.rfind(":") > name.rfind("/"):
        name = name[:name.rfind(":")]
    first, _, rest = name.partition("/")
    if rest and first in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        name = rest
    name = name.removeprefix("library/")
    return f"image:{name}" if name and "$" not in name else ""


def repo_key(repo: pipeline.Repo) -> str:
    return f"source:{repo.host}/{repo.path}".lower()


def keys(stack: pipeline.Stack, project: pipeline.Repo | None) -> list[str]:
    """The keys of an entry whose compose file is `stack`."""
    found = [image_key(str(stack.doc["services"][stack.service].get("image") or ""))]
    if project is not None and "/" in project.path:
        found.append(repo_key(project))
    return [k for k in found if k]


def _label_names(service: dict[str, Any]) -> list[str]:
    labels = service.get("labels")
    if isinstance(labels, dict):
        return [str(k) for k in labels]
    return [str(item).partition("=")[0] for item in labels or []]


def richness(c: pipeline.Candidate, stack: pipeline.Stack) -> int:
    """How much the entry carries: its settings, and a bonus for a health
    check and for sign-in labels."""
    services = [s for s in stack.doc["services"].values() if isinstance(s, dict)]
    names = {name for name, _ in c.settings} | set(pipeline.compose_variables(stack.doc))
    health = any(s.get("healthcheck") for s in services)
    sign_in = any(n.startswith("vps.auth.oidc") for s in services for n in _label_names(s))
    return len(names) + HEALTH_WEIGHT * health + SIGN_IN_WEIGHT * sign_in


def groups(prepared: list[pipeline.Prepared]) -> list[list[pipeline.Prepared]]:
    """The entries that are one app, each group richest first, in the order
    the groups' first entries came."""
    parent = list(range(len(prepared)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, p in enumerate(prepared):
        for key in p.keys:
            if key in owner:
                parent[find(i)] = find(owner[key])
            else:
                owner[key] = i
    members: dict[int, list[int]] = {}
    for i in range(len(prepared)):
        members.setdefault(find(i), []).append(i)
    ordered = sorted(members.values(), key=min)
    return [[prepared[i] for i in sorted(group, key=lambda i: (-prepared[i].rich, i))]
            for group in ordered]


def reference(p: pipeline.Prepared) -> dict[str, str]:
    """Where an entry was read, as an alias record holds it."""
    c = p.c
    ref = {"registry": c.registry, "entry": c.entry, "source": c.source,
           "commit": c.commit, "path": c.path}
    return {k: v for k, v in ref.items() if v}


def _compose_keys(slug: str, cat: dict[str, Any]) -> list[str]:
    path = model.BLUEPRINTS / slug / model.COMPOSE_NAME
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    block = doc.get(model.BLOCK_KEY) if isinstance(doc, dict) else None
    service = ((block or {}).get("version_from") if isinstance(block, dict) else None) \
        or (cat.get("domain") or {}).get("service")
    spec = ((doc.get("services") or {}) if isinstance(doc, dict) else {}).get(service)
    return [k for k in [image_key(str((spec or {}).get("image") or ""))] if k]


def catalog_index() -> dict[str, str]:
    """Each key of the apps in the catalog, with the id of the app. A curated
    app's upstream address counts when it names a repository; an imported
    app's counts through its origin, as its upstream address may be the
    registry's."""
    index: dict[str, str] = {}
    for path in sorted(model.SOURCES.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            doc = json.loads(path.read_text())
            slug = doc["id"]
            cat = doc.get("x-catena") or {}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found = _compose_keys(slug, cat)
        address = (cat.get("origin") or {}).get("project") if cat.get("status") == "imported" \
            else cat.get("upstream_url")
        repo = pipeline.upstream_repo(str(address or ""))
        if repo is not None and "/" in repo.path:
            found.append(repo_key(repo))
        for key in found:
            index.setdefault(key, slug)
    return index


def _aliases_path() -> Path:
    return model.SOURCES / ALIASES_NAME


def _load_records() -> list[dict[str, str]]:
    try:
        return list(json.loads(_aliases_path().read_text()).get("aliases") or [])
    except (OSError, ValueError, AttributeError):
        return []


def load_aliases() -> dict[tuple[str, str], str]:
    """(registry, entry) of each alias already recorded, with the app it is
    an alias of."""
    return {(r.get("registry", ""), r.get("entry", "")): r.get("app", "") for r in _load_records()}


def add_aliases(records: Iterable[dict[str, str]]) -> None:
    """Records new aliases in sources/_aliases.json, one per entry."""
    known = {(r.get("registry"), r.get("entry")): r for r in _load_records()}
    for record in records:
        known.setdefault((record.get("registry"), record.get("entry")), record)
    ordered = sorted(known.values(), key=lambda r: (r.get("app", ""), r.get("registry", ""),
                                                    r.get("entry", "")))
    _aliases_path().write_text(json.dumps({"aliases": ordered}, indent=2) + "\n")
