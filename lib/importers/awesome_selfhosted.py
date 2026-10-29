"""The awesome-selfhosted list (github.com/awesome-selfhosted/awesome-selfhosted-data):
one software/<name>.yml per project, with name, website_url, source_code_url,
description, licenses (SPDX ids), platforms and tags.

The list holds no compose file, so nothing is imported from it. It tells the
importer which repository a project is at: an entry of another registry whose
id is the project's name gets that repository as its upstream project, whose
licence is then read from its forge as the app's own
(pipeline.APP_LICENCES). Only the project's name and repository are used, as
facts; none of the list's text reaches the catalog.

The registry document is GitHub's file listing of the repository at one
commit, and every file is read at that commit:

  https://api.github.com/repos/awesome-selfhosted/awesome-selfhosted-data/git/trees/COMMIT?recursive=1

Only projects whose platforms include Docker are listed.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from . import pipeline
from .pipeline import Candidate, plain, slugify, upstream_repo

_TREE = re.compile(
    r"^https://api\.github\.com/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/git/trees/([^/?#]+)")


def read(doc: Any, registry_url: str) -> list[Candidate]:
    m = _TREE.match(registry_url)
    if not m or not isinstance(doc, dict) or not isinstance(doc.get("tree"), list):
        raise ValueError("not GitHub's file listing of a repository at a commit "
                         "(https://api.github.com/repos/OWNER/REPO/git/trees/COMMIT?recursive=1)")
    if doc.get("truncated"):
        raise ValueError("GitHub's file listing is truncated")
    owner, repo, ref = m.groups()
    paths = sorted(str(e.get("path")) for e in doc["tree"]
                   if isinstance(e, dict) and e.get("type") == "blob"
                   and re.fullmatch(r"software/[^/]+\.ya?ml", str(e.get("path"))))
    out = []
    for path in paths:
        c = _candidate(f"{pipeline.RAW_GITHUB}/{owner}/{repo}/{ref}/{path}")
        if c:
            c.commit = ref if re.fullmatch(r"[0-9a-f]{40}", ref) else ""
            c.path = path
            c.source = f"https://github.com/{owner}/{repo}/blob/{ref}/{path}"
            out.append(c)
    return out


def _candidate(url: str) -> Candidate | None:
    try:
        data = yaml.safe_load(pipeline.fetch(url))
    except (pipeline.FetchError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or "Docker" not in (data.get("platforms") or []):
        return None
    name = plain(data.get("name"))
    project = upstream_repo(str(data.get("source_code_url") or ""))
    if not name or project is None or "/" not in project.path:
        return None
    return Candidate(entry=name, slug=slugify(name), title=name, listing=True, project=project,
                     upstream_url=str(data.get("website_url") or ""))
