"""Portainer App Templates lists, versions 2 and 3: the format Catena serves.

A list is {"version": "2" or "3", "templates": [...]}; version 3 gives each
entry a numeric id. The fields read are those of Portainer's Template type
(portainer api/portainer.go) and its published format
(docs.portainer.io, advanced/app-templates/format):

  type 1     one container: image (with registry in front when set), command,
             ports ("8080:80/tcp", "443/tcp"), volumes ({container, bind,
             readonly}), env, labels ({name, value}), privileged, interactive,
             restart_policy, hostname, network
  type 2, 3  a swarm or a compose stack: the compose file at
             repository.stackfile in the git repository at repository.url
  env        {name, label, description, default, preset, select}; without a
             default, a select's default option gives the value, else its
             first option

A stack entry copies its repository's compose file, so that repository's
licence applies. A container entry's compose file is written from the
registry's own fields, so the registry's repository licenses it. Type 4 (an
edge stack) and Windows entries are left out.
"""
from __future__ import annotations

import re
from typing import Any

from .pipeline import Candidate, github_repo, plain, raw_url, slugify

# Portainer's restart_policy values as swarm restart conditions. Portainer
# restarts a container always when the entry names no policy.
_RESTART = {"always": "any", "unless-stopped": "any", "on-failure": "on-failure", "no": "none"}


def read(doc: Any, registry_url: str) -> list[Candidate]:
    if (not isinstance(doc, dict) or str(doc.get("version")) not in ("2", "3")
            or not isinstance(doc.get("templates"), list)):
        raise ValueError("not a Portainer template list of version 2 or 3")
    registry_repo = github_repo(registry_url)
    return [_candidate(t, registry_url, registry_repo)
            for t in doc["templates"] if isinstance(t, dict)]


def _candidate(t: dict[str, Any], registry_url: str,
               registry_repo: tuple[str, str] | None) -> Candidate:
    title = plain(t.get("title"))
    c = Candidate(
        entry=title or f"entry {t.get('id', '?')}",
        slug=slugify(plain(t.get("name")) or title),
        title=title,
        description=plain(t.get("description")) or title,
        categories=[name for name in (plain(x) for x in t.get("categories") or []) if name],
        settings=_settings(t.get("env")),
    )
    kind = t.get("type")
    platform = str(t.get("platform") or "linux")
    if platform != "linux":
        c.skip = f"a {platform} template"
    elif kind in (2, 3):
        _stack(c, t)
    elif kind == 1:
        _container(c, t, registry_url, registry_repo)
    else:
        c.skip = f"type {kind} entries are not imported"
    return c


def _settings(env: Any) -> list[tuple[str, str]]:
    out = []
    for item in env or []:
        if not isinstance(item, dict):
            continue
        default = item.get("default")
        options = [o for o in item.get("select") or [] if isinstance(o, dict)]
        if default is None and options:
            default = ([o for o in options if o.get("default")] or options)[0].get("value")
        out.append((str(item.get("name") or "").strip(), "" if default is None else str(default)))
    return out


def _stack(c: Candidate, t: dict[str, Any]) -> None:
    repository = t.get("repository") or {}
    url = str(repository.get("url") or "").strip()
    stackfile = str(repository.get("stackfile") or "").strip()
    repo = github_repo(url)
    c.source = c.upstream_url = url
    if not stackfile:
        c.skip = "the entry names no stack file"
    elif repo is None:
        c.licence_problem = f"the repository {url or '(none)'} is not on GitHub"
    else:
        c.source = c.upstream_url = f"https://github.com/{repo[0]}/{repo[1]}"
        c.licence_repo = repo
        c.compose_url = raw_url(repo, stackfile)


def _container(c: Candidate, t: dict[str, Any], registry_url: str,
               registry_repo: tuple[str, str] | None) -> None:
    image = str(t.get("image") or "").strip()
    if not image:
        c.skip = "the entry names no image"
        return
    registry = str(t.get("registry") or "").strip().rstrip("/")
    if registry and not image.startswith(registry + "/"):
        image = f"{registry}/{image}"
    c.source = registry_url
    c.upstream_url = image_page(image)
    if registry_repo is None:
        c.licence_problem = "the registry is not in a GitHub repository, so its licence is unknown"
    else:
        c.licence_repo = registry_repo

    service: dict[str, Any] = {"image": image}
    if t.get("command"):
        service["command"] = str(t["command"])
    if t.get("hostname"):
        service["hostname"] = str(t["hostname"])
    if t.get("privileged"):
        service["privileged"] = True
    if t.get("interactive"):
        service["stdin_open"] = True
        service["tty"] = True
    network = str(t.get("network") or "").strip()
    if network == "host":
        service["network_mode"] = "host"
    elif network:
        c.to_choose.append(f"the registry joins the container to the network {network}, "
                           f"which a Catena server does not have")
    if c.settings:
        service["environment"] = {name: f"${{{name}}}" for name, _ in c.settings if name}
    if t.get("ports"):
        service["ports"] = [str(p) for p in t["ports"]]
    volumes, named = _volumes(t.get("volumes"))
    if volumes:
        service["volumes"] = volumes
    labels = [f"{item['name']}={item.get('value', '')}" for item in t.get("labels") or []
              if isinstance(item, dict) and item.get("name")]
    if labels:
        service["labels"] = labels
    condition = _RESTART.get(str(t.get("restart_policy") or "always"), "any")
    service["deploy"] = {"restart_policy": {"condition": condition}}

    c.compose = {"services": {c.slug: service}}
    if named:
        c.compose["volumes"] = {name: None for name in named}


def _volumes(volumes: Any) -> tuple[list[str], list[str]]:
    """Compose volume entries for Portainer's {container, bind, readonly}. A
    bind stays a bind. The volume Portainer creates for the rest gets a name
    from its container path, so it outlives the task that mounts it."""
    entries: list[str] = []
    named: list[str] = []
    for item in volumes or []:
        if not isinstance(item, dict) or not item.get("container"):
            continue
        target = str(item["container"])
        source = str(item.get("bind") or "").strip()
        if not source:
            base = re.sub(r"[^a-z0-9_.-]+", "-", target.lower()).strip("-.") or "data"
            source, n = base, 2
            while source in named:
                source, n = f"{base}-{n}", n + 1
            named.append(source)
        entries.append(f"{source}:{target}" + (":ro" if item.get("readonly") else ""))
    return entries, named


def image_page(image: str) -> str:
    """The image's page on its registry: Docker Hub's for a Docker Hub image,
    the image's own address for any other registry."""
    ref = image.split("@", 1)[0]
    if ref.rfind(":") > ref.rfind("/"):
        ref = ref[:ref.rfind(":")]
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        if first not in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            return f"https://{ref}"
        ref = rest
    ref = ref.removeprefix("library/")
    return f"https://hub.docker.com/{'r' if '/' in ref else '_'}/{ref}"
