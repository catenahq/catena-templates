"""Provenance of the images an app pins: a project old enough, with enough
releases, stands behind each one.

An image passes when its source repository is at least a year old and has
three releases. The repository is the one the image's
`org.opencontainers.image.source` label names, read from GitHub. An image of
GHCR without the label is judged by the GitHub repository its path names
(`ghcr.io/<owner>/<name>`): a GHCR namespace is the GitHub account that
publishes the image. An image that
lives on Docker Hub is judged by its Docker Hub repository instead when it has
no such label, or when its GitHub repository is under a year old or has too few
releases: registered at least a year ago, with three pushed tags. A GitHub
repository that does not exist, a page that cannot be read, and an image of
another registry without the label (they expose no dates) fail, naming the image.
An image whose repository is listed in the environment variable
`PROVENANCE_OWN_IMAGES` (comma-separated, no tag) is the repository's own
build and passes unchecked.

The checks take `fetch(url) -> JSON | None` (None for a missing page), so a
test answers from recorded responses and the CLI answers over HTTPS.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

GITHUB_API = "https://api.github.com"
HUB_API = "https://hub.docker.com/v2"
SOURCE_LABEL = "org.opencontainers.image.source"
MIN_AGE = timedelta(days=365)
MIN_RELEASES = 3
HTTP_TIMEOUT = 30
USER_AGENT = "catena-templates-provenance"

Fetch = Callable[[str], Any]

_IMAGE_LINE = re.compile(r"""(?m)^\s*image:\s*["']?([^"'\s#]+)""")
_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}$]*)\}")
_GITHUB_SOURCE = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)")


class ProvenanceError(Exception):
    """A page that could not be read, as opposed to one that is missing."""


class SourceNotEstablished(Exception):
    """A GitHub repository that exists but is too young or has too few releases."""


def resolve_ref(raw: str) -> str:
    """An image value with each `${VAR:-default}` read as its default."""
    return _DEFAULT.sub(lambda m: m.group(1), raw)


def compose_images(body: str) -> tuple[list[str], list[str]]:
    """(image refs, refs that are still expressions) of a compose body."""
    refs: list[str] = []
    unresolved: list[str] = []
    for raw in _IMAGE_LINE.findall(body):
        ref = resolve_ref(raw)
        if "$" in ref or "{{" in ref:
            unresolved.append(raw)
        elif ref not in refs:
            refs.append(ref)
    return refs, unresolved


def split_ref(ref: str) -> tuple[str, str]:
    """(registry, repository path) of an image reference, without its tag or
    digest. Docker Hub's own images live under `library/`."""
    name = ref.split("@", 1)[0]
    if name.rfind(":") > name.rfind("/"):
        name = name[: name.rfind(":")]
    parts = name.split("/")
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, "/".join(parts[1:])
    else:
        registry, path = "docker.io", name
    if registry in ("index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
    if registry == "docker.io" and "/" not in path:
        path = "library/" + path
    return registry, path


def github_repo(source: str) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub address, or None."""
    match = _GITHUB_SOURCE.search(source or "")
    if not match:
        return None
    repo = match.group(2)
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return match.group(1), repo


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _old_enough(stamp: str | None, now: datetime) -> bool:
    return bool(stamp) and now - _when(stamp) >= MIN_AGE


def _github(owner: str, repo: str, fetch: Fetch, now: datetime) -> str | None:
    info = fetch(f"{GITHUB_API}/repos/{owner}/{repo}")
    if not info:
        return f"GitHub has no repository {owner}/{repo}"
    if not _old_enough(info.get("created_at"), now):
        raise SourceNotEstablished(f"{owner}/{repo} is under a year old")
    releases = fetch(f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=100") or []
    published = [r for r in releases if not r.get("draft")]
    if len(published) < MIN_RELEASES:
        raise SourceNotEstablished(
            f"{owner}/{repo} has {len(published)} release(s), needs {MIN_RELEASES}")
    return None


def _hub(path: str, fetch: Fetch, now: datetime) -> str | None:
    info = fetch(f"{HUB_API}/repositories/{path}/")
    if not info:
        return f"Docker Hub has no repository {path}"
    if not _old_enough(info.get("date_registered"), now):
        return f"Docker Hub repository {path} was registered under a year ago"
    tags = (fetch(f"{HUB_API}/repositories/{path}/tags?page_size=100") or {}).get("results", [])
    pushed = [t for t in tags if t.get("tag_last_pushed")]
    if len(pushed) < MIN_RELEASES:
        return f"Docker Hub repository {path} has {len(pushed)} pushed tag(s), needs {MIN_RELEASES}"
    return None


def own_images(environ: dict[str, str] | None = None) -> set[tuple[str, str]]:
    """The repositories PROVENANCE_OWN_IMAGES lists, as split_ref yields them."""
    raw = (environ if environ is not None else os.environ).get("PROVENANCE_OWN_IMAGES", "")
    return {split_ref(item.strip()) for item in raw.split(",") if item.strip()}


def is_own(ref: str, own: set[tuple[str, str]]) -> bool:
    return split_ref(ref) in own


def _hub_instead(ref: str, reason: str, fetch: Fetch, now: datetime) -> str | None:
    """A GitHub source that is too young or has too few releases: an image on
    Docker Hub falls back to its Docker Hub record."""
    registry, path = split_ref(ref)
    if registry != "docker.io":
        return reason
    hub = _hub(path, fetch, now)
    return f"{reason}; {hub}" if hub else None


def check_image(ref: str, labels: dict[str, str] | None, fetch: Fetch,
                now: datetime | None = None) -> str | None:
    """None when the image's provenance holds, else the failure naming it."""
    now = now or datetime.now(timezone.utc)
    source = (labels or {}).get(SOURCE_LABEL, "")
    registry, path = split_ref(ref)
    try:
        repo = github_repo(source)
        if not repo and registry == "ghcr.io" and "/" in path:
            owner, name = path.split("/")[:2]
            repo = (owner, name)
        if repo:
            try:
                why = _github(*repo, fetch, now)
            except SourceNotEstablished as exc:
                why = _hub_instead(ref, str(exc), fetch, now)
        else:
            if registry != "docker.io":
                return (f"{ref}: names no GitHub source repository and {registry} "
                        f"exposes no dates; set {SOURCE_LABEL} on the image or use "
                        f"an image from Docker Hub")
            why = _hub(path, fetch, now)
    except ProvenanceError as exc:
        why = str(exc)
    return f"{ref}: {why}" if why else None


def http_fetch(url: str) -> Any:
    """GET a JSON page; None for a 404. GitHub answers with GITHUB_TOKEN."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(GITHUB_API):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ProvenanceError(f"{url} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"{url} could not be read: {exc}") from exc
