"""The upstream repository of an app, found from its main image when the
registry names none.

Two places name it, tried in order:

  label    the image's `org.opencontainers.image.source`, read from the
           image's registry without pulling it: the manifest's annotations,
           else the labels of its config blob (Docker Hub and GHCR hand out
           an anonymous pull token)
  hub      for a Docker Hub image, the source repository linked on the
           Docker Hub repository (`source`)

Both go through pipeline.fetch, so a test answers them from recorded
responses. lib/provenance.py supplies the label name and the reference
parsing.
"""
from __future__ import annotations

import json
from typing import Any

from .. import provenance
from . import pipeline

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
_TOKENS = {
    "docker.io": ("registry-1.docker.io",
                  "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{path}:pull"),
    "ghcr.io": ("ghcr.io", "https://ghcr.io/token?service=ghcr.io&scope=repository:{path}:pull"),
}


def _json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    doc = json.loads(pipeline.fetch(url, headers))
    if not isinstance(doc, dict):
        raise ValueError(f"{url}: not an object")
    return doc


def _reference(ref: str) -> str:
    if "@" in ref:
        return ref.split("@", 1)[1]
    name = ref.rsplit("/", 1)[-1]
    return name.split(":", 1)[1] if ":" in name else "latest"


def _annotation(manifest: dict[str, Any]) -> str:
    return str((manifest.get("annotations") or {}).get(provenance.SOURCE_LABEL) or "")


def label_source(ref: str) -> str:
    """The source address the image's registry gives it, or "" for none or
    for a registry that cannot be read."""
    registry, path = provenance.split_ref(ref)
    host, token_url = _TOKENS.get(registry, (registry, ""))
    headers = {"Accept": MANIFEST_TYPES}
    base = f"https://{host}/v2/{path}"
    try:
        if token_url:
            token = _json(token_url.format(path=path), {}).get("token")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        manifest = _json(f"{base}/manifests/{_reference(ref)}", headers)
        if _annotation(manifest):
            return _annotation(manifest)
        if isinstance(manifest.get("manifests"), list):
            entries = [m for m in manifest["manifests"] if isinstance(m, dict)
                       and (m.get("platform") or {}).get("os") == "linux"
                       and (m.get("platform") or {}).get("architecture") == "amd64"]
            if not entries:
                return ""
            manifest = _json(f"{base}/manifests/{entries[0]['digest']}", headers)
            if _annotation(manifest):
                return _annotation(manifest)
        digest = (manifest.get("config") or {}).get("digest")
        if not digest:
            return ""
        config = _json(f"{base}/blobs/{digest}", headers)
        return str(((config.get("config") or {}).get("Labels") or {}).get(provenance.SOURCE_LABEL) or "")
    except (pipeline.FetchError, ValueError, KeyError, TypeError):
        return ""


def hub_source(ref: str) -> str:
    """The source repository linked on a Docker Hub repository, or ""."""
    registry, path = provenance.split_ref(ref)
    if registry != "docker.io":
        return ""
    try:
        doc = _json(f"{provenance.HUB_API}/repositories/{path}/", {})
    except (pipeline.FetchError, ValueError):
        return ""
    return str(doc.get("source") or "")


def _repo(address: str) -> pipeline.Repo | None:
    github = provenance.github_repo(address)
    if github:
        return pipeline.Repo("github.com", f"{github[0]}/{github[1]}")
    found = pipeline.upstream_repo(address)
    return found if found is not None and "/" in found.path else None


def find(ref: str) -> tuple[pipeline.Repo | None, str]:
    """(repository, where it was found) for the image, or (None, "")."""
    for where, read in (("the image's source label", label_source),
                        ("the Docker Hub repository's linked source", hub_source)):
        repo = _repo(read(ref))
        if repo is not None:
            return repo, where
    return None, ""
