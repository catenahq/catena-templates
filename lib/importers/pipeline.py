"""What every registry adapter shares: from Candidates to catalog entries
marked imported and untested.

An adapter reads one registry format into Candidates. For each, in order:

  id        the adapter's slug, refused when a source file or a blueprint
            directory (its app.json among its files) already holds it: the
            importer never overwrites. An
            entry whose title or id holds a word on the banned-word list
            check:unicode reads is skipped too.
  licence   the licence the forge detects for the repository the adapter
            names: the one a stack entry's compose file sits in, the
            registry's own for a compose the adapter writes from the
            registry's fields, or the app's own. GitHub's, GitLab's and
            Gitea's APIs are read (repo_licence). It is recorded with its
            notice in origin and refuses nothing. The app's own licence, read
            from its upstream project, must be one of APP_LICENCES; one the
            forge cannot name, or an upstream found nowhere, skips the entry.
  compose   fetched for a stack entry, or the adapter's, changed by the
            adapter where its registry's format asks, then made into a
            catalog stack file: no version key, no Traefik labels, a restart
            policy on each service that keeps running, and the route labels
            on the service and port the registry routes, or else on the first
            service publishing a TCP port. The proxy reaches that port over
            catena-network, so it is not published on the server.
  settings  each variable the registry declares or the compose reads is an
            env default; one named like a password, secret or key gets the
            catalog's minted value, unless the adapter gives it one.
  checker   catena-applint --fix (catena-admin payload/cmd/catena-applint)
            corrects the file. What it leaves, and what the catalog's swarm
            lint and Postgres pin rule refuse, is recorded on the entry.

Each entry is written as blueprints/<id>/app.json (sources/<id>.json while the
catalog has no catalog-meta.json) and blueprints/<id>/docker-compose.yml, held
as neat files beside it (services.yml, settings.yml), with
status "imported", its origin and what a person still has to choose. An entry
the catalog's order does not list follows the listed ones.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .. import model, pack, postgres_pins, render, swarm_lint

GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
HTTP_TIMEOUT = 30
USER_AGENT = "catena-templates-import-registry"

# The checker's CLI when --applint names none.
APPLINT_ENV = "CATENA_APPLINT"

# The host every imported entry answers on until a person picks one, in the
# form the host evaluates (catena-admin payload/engines/appenv).
ZONE = "{{ cloudflare_zone }}"

# The checker's rules that refuse a feature giving the app the server itself.
REFUSED_RULES = frozenset({"X1", "X2", "X3", "X4", "X5", "X6"})

# The licences an app's own project may carry. A file the catalog copies (a
# compose file, a registry's settings) is recorded with its licence and
# notice, whatever they are.
APP_LICENCES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0")

# The banned-word list contracts/scripts/check-unicode.mjs reads, in the
# contracts checkout beside this one.
BANNED_WORDS = model.TOOLS_ROOT.parent / "contracts" / "banned-words.json"


@dataclass
class Candidate:
    """One registry entry as an adapter reads it.

    `compose` is a stack file the adapter writes from the entry's fields;
    `compose_url` is where a stack entry's file is fetched from, and
    `convert` is the adapter's own change to that file, made first. `route`
    is the service and port the registry routes, when it names one. The
    licence recorded in origin is the one of `licence_repo` on its forge; an
    adapter that has no repository to name says why in `licence_problem`.
    `notice` is a further copyright notice the attribution carries: the
    registry's, when `licence_repo` is the app's. A non-empty `skip` keeps
    the entry out, with that reason.

    `registry` is the address the entry was read from, when it differs from
    the importer's own. `project` is the app's upstream repository when the
    registry or a listing names it: its licence is the app's, checked against
    APP_LICENCES, and it keys the deduplication. `compose_repo` and `compose_path` name a stack file
    fetched at the repository's pinned commit (`env_path` is a file of
    KEY=value defaults beside it); `commit` and `path` record where the
    entry was read, once known. A `listing` entry names a project and has no
    compose file: it only tells the importer which repository an entry by
    that name belongs to."""

    entry: str
    slug: str = ""
    title: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    settings: list[tuple[str, str]] = field(default_factory=list)
    compose: dict[str, Any] | None = None
    compose_url: str = ""
    convert: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    route: tuple[str, int] | None = None
    licence_repo: Repo | None = None
    licence_problem: str = ""
    notice: str = ""
    source: str = ""
    upstream_url: str = ""
    to_choose: list[str] = field(default_factory=list)
    skip: str = ""
    registry: str = ""
    project: Repo | None = None
    commit: str = ""
    path: str = ""
    compose_repo: Repo | None = None
    compose_path: str = ""
    env_path: str = ""
    listing: bool = False


class Skip(Exception):
    """An entry the importer leaves out. `kind` sorts the report: "licence"
    for an app licence outside APP_LICENCES, "unknown" for one nobody can
    name, "other" for the rest."""

    def __init__(self, reason: str, kind: str = "other"):
        super().__init__(reason)
        self.kind = kind


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    """GET one address. The tests replace this function, which is the
    importer's only way onto the network."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc


# The characters the unicode gate refuses, mapped to the ASCII forms it asks
# for, and the no-break space.
_ASCII = str.maketrans({
    "\u2014": "--", "\u2013": "-", "\u2192": "->", "\u2190": "<-", "\u2026": "...",
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u00ab": '"', "\u00bb": '"', "\u00a0": " ",
})


def plain(text: Any) -> str:
    """Registry text as catalog text: one line, the gate's ASCII forms, and
    no emoji or other symbol characters."""
    out = str(text or "").translate(_ASCII)
    out = "".join(ch for ch in out
                  if unicodedata.category(ch) not in ("So", "Cf", "Co", "Cn")
                  and not 0xFE00 <= ord(ch) <= 0xFE0F)
    return " ".join(out.split())


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True)
class Repo:
    """A repository on a forge: the forge's `host`, and the repository's
    `path`, its owner and name, or a GitLab project's groups and name. A
    `path` of one segment is an owner's page, which the importer reads as
    the owner's repository named as the entry's id."""

    host: str
    path: str

    def __str__(self) -> str:
        return self.path if self.host == "github.com" else f"{self.host}/{self.path}"


_GITHUB_REPO = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/[^?#]+)?/?$")
_RAW_FILE = re.compile(r"^https?://raw\.githubusercontent\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/")


def github_repo(url: str) -> Repo | None:
    """The GitHub repository of a repository address, of a /tree/<ref>/<path>
    address in one, or of a raw file in one; None for anything else."""
    m = _GITHUB_REPO.match(url.strip()) or _RAW_FILE.match(url.strip())
    return Repo("github.com", f"{m.group(1)}/{m.group(2)}") if m else None


def upstream_repo(url: str) -> Repo | None:
    """The repository an app's upstream address names: a GitHub one as
    github_repo reads it, or the address's path on another host. A path of
    one segment, an owner's page, counts only on a host FORGES names. None
    for any other address."""
    found = github_repo(url)
    if found:
        return found
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.strip("/").removesuffix(".git")
    if parts.scheme not in ("http", "https") or not host or not path:
        return None
    if "/" not in path:
        return Repo(host, path) if host in FORGES else None
    return None if host == "github.com" else Repo(host, path)


def raw_url(repo: Repo, path: str, ref: str = "HEAD") -> str:
    """A file on the GitHub repository at `ref`."""
    rel = path.strip().removeprefix("./").lstrip("/")
    return f"{RAW_GITHUB}/{repo.path}/{ref}/{rel}"


_SHA = re.compile(r"^[0-9a-f]{40}$")
_PINNED_RAW = re.compile(
    r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/([0-9a-f]{40})/(.+)$")


def pinned_raw(url: str) -> tuple[str, str]:
    """(commit, path) of a raw file address that names a commit, else ("", "")."""
    m = _PINNED_RAW.match(url.strip())
    return (m.group(1), m.group(2)) if m else ("", "")


@dataclass(frozen=True)
class Licence:
    """`spdx` is empty when the licence is unknown, and `problem` says why."""

    spdx: str
    attribution: str
    problem: str = ""


# A copyright notice: the word, then (c) or a year. Lines of a licence's
# own text that start with the word go on with other words ("copyright
# notice that is included in...").
_COPYRIGHT = re.compile(r"^copyright\s+(?:\(c\)|\d{4})", re.IGNORECASE)
# A licence template's copyright line to fill in.
_PLACEHOLDER = re.compile(r"\[yyyy\]|\{yyyy\}|\[year\]|<year>", re.IGNORECASE)


def _attribution(text: str, url: str) -> str:
    """The licence file's copyright lines, three at most, or the file's
    address when it carries none."""
    lines: list[str] = []
    for line in (plain(raw) for raw in text.splitlines()):
        if _COPYRIGHT.match(line) and not _PLACEHOLDER.search(line) and line not in lines:
            lines.append(line)
    return "; ".join(lines[:3]) or url


def _file_text(doc: dict[str, Any]) -> str:
    """A file's text as GitHub's and Gitea's APIs hand it over."""
    if doc.get("encoding") != "base64":
        return ""
    return base64.b64decode(doc.get("content") or "").decode("utf-8", "replace")


def _answer(url: str, key: str, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The JSON object a forge's API answers, or None for a 404 or for an
    answer that is no object holding `key`: that API holds no such
    repository. Any other failure raises FetchError."""
    try:
        doc = json.loads(fetch(url, headers))
    except FetchError as exc:
        if exc.status == 404:
            return None
        raise
    except ValueError:
        return None
    return doc if isinstance(doc, dict) and key in doc else None


def github_headers() -> dict[str, str]:
    """GITHUB_TOKEN, when set, lifts the API's rate limit for anonymous callers."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def pin_commit(repo: Repo) -> str:
    """The commit GitHub's default branch of the repository is at, or a
    FetchError."""
    doc = json.loads(fetch(f"{GITHUB_API}/repos/{repo.path}/commits/HEAD", github_headers()))
    sha = str(doc.get("sha") or "") if isinstance(doc, dict) else ""
    if not _SHA.match(sha):
        raise FetchError(f"{repo}: GitHub names no commit")
    return sha


def _github(repo: Repo) -> Licence | None:
    """GitHub's licence API, which hands over the licence file. Its 404
    answers a repository with no licence file and a missing one alike, so
    the repository is then asked for."""
    headers = github_headers()
    doc = _answer(f"{GITHUB_API}/repos/{repo.path}/license", "license", headers)
    if doc is None:
        if _answer(f"{GITHUB_API}/repos/{repo.path}", "full_name", headers) is None:
            return None
        return Licence("", "", f"GitHub finds no licence file in {repo}")
    spdx = str((doc.get("license") or {}).get("spdx_id") or "")
    url = str(doc.get("html_url") or f"https://github.com/{repo.path}")
    if spdx in ("", "NOASSERTION"):
        return Licence("", "", f"GitHub cannot name the licence in {url}")
    return Licence(spdx, _attribution(_file_text(doc), url))


def _gitlab(repo: Repo) -> Licence | None:
    """GitLab's project API. `license.key` is the SPDX id of the licence it
    detects, in lower case, or "other" for a licence file it cannot name;
    `license_url` is that file's page on the default branch, and the
    repository files API serves its text."""
    api = f"https://{repo.host}/api/v4/projects/{urllib.parse.quote(repo.path, safe='')}"
    doc = _answer(f"{api}?license=true", "path_with_namespace")
    if doc is None:
        return None
    key = str((doc.get("license") or {}).get("key") or "")
    url = str(doc.get("license_url") or "")
    if not key:
        return Licence("", "", f"GitLab finds no licence file in {repo}")
    if key == "other":
        return Licence("", "", f"GitLab cannot name the licence in {url}")
    spdx = {name.lower(): name for name in APP_LICENCES}.get(key, key)
    page = f"{doc.get('web_url')}/-/blob/{doc.get('default_branch')}/"
    text = ""
    if url.startswith(page):
        file = urllib.parse.quote(url.removeprefix(page), safe="")
        text = fetch(f"{api}/repository/files/{file}/raw").decode("utf-8", "replace")
    return Licence(spdx, _attribution(text, url))


def _gitea(repo: Repo) -> Licence | None:
    """Gitea's repository API, which Forgejo shares. Gitea names the
    licences it detects in the LICENSE file at the root under `licenses`,
    as SPDX ids, and its contents API hands over that file. Forgejo's
    answer, and Gitea's before 1.23, carry no `licenses`."""
    if repo.path.count("/") != 1:
        return None
    api = f"https://{repo.host}/api/v1/repos/{repo.path}"
    doc = _answer(api, "full_name")
    if doc is None:
        return None
    if "licenses" not in doc:
        return Licence("", "", f"the API at {repo.host} names no licence for {repo.path}")
    names = [str(name) for name in doc["licenses"] or []]
    if len(names) != 1:
        found = f"names {', '.join(names)}" if names else "finds no licence"
        return Licence("", "", f"Gitea {found} in the LICENSE file of {repo}")
    branch = urllib.parse.quote(str(doc.get("default_branch") or ""), safe="")
    file = _answer(f"{api}/contents/LICENSE?ref={branch}", "content") or {}
    url = str(file.get("html_url") or doc.get("html_url") or "")
    return Licence(names[0], _attribution(_file_text(file), url))


# The reader of each host whose forge the importer knows. Codeberg runs
# Forgejo, which shares Gitea's API.
FORGES: dict[str, Callable[[Repo], Licence | None]] = {
    "github.com": _github, "gitlab.com": _gitlab, "codeberg.org": _gitea}


def repo_licence(repo: Repo) -> Licence:
    """The licence the repository's forge detects, with the copyright lines
    of its licence file. A host FORGES leaves out is asked through Gitea's
    API and, when that holds no such repository or fails, through
    GitLab's."""
    readers = [FORGES[repo.host]] if repo.host in FORGES else [_gitea, _gitlab]
    error: FetchError | None = None
    for reader in readers:
        try:
            licence = reader(repo)
        except FetchError as exc:
            error = exc
            continue
        if licence is not None:
            return licence
    if error:
        return Licence("", "", f"the licence of {repo} cannot be read: {error}")
    if repo.host == "github.com":
        where = "GitHub"
    elif repo.host in FORGES:
        where = repo.host
    else:
        where = f"{repo.host}, through Gitea's API or GitLab's"
    return Licence("", "", f"no repository {repo.path} on {where}")


def banned_words() -> list[tuple[str, re.Pattern[str]]]:
    """Each word of BANNED_WORDS with the match the gate makes, ignoring
    case: no letter, digit or underscore before it, nor after it unless the
    list marks it a stem."""
    words = []
    for t in json.loads(BANNED_WORDS.read_text())["tokens"]:
        tail = "" if t["stem"] else r"(?![0-9A-Za-z_])"
        words.append((t["token"], re.compile(
            rf"(?<![0-9A-Za-z_]){re.escape(t['token'])}{tail}", re.IGNORECASE)))
    if not words:
        raise ValueError("it lists no words")
    return words


class ComposeLoader(yaml.SafeLoader):
    """Reads scalars the way compose does (YAML 1.2): yes, no, on and off
    stay strings, 22:22 is a string and not a base-60 number, and so is a
    date."""


ComposeLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers
            if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int",
                           "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp")]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ComposeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
ComposeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
ComposeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"), list("-+0123456789."))


class ComposeDumper(yaml.SafeDumper):
    """Block style with each list indented under its key, as the catalog's
    compose files are written, and a multi-line string as a literal block."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


ComposeDumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
    "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None))
ComposeDumper.add_representer(type(None), lambda dumper, _: dumper.represent_scalar(
    "tag:yaml.org,2002:null", ""))


def load_compose(text: str) -> dict[str, Any]:
    """A compose file as plain data, or ValueError. The JSON round trip gives
    each service its own copy of what a YAML anchor shares, so a change made
    to one service reaches no other."""
    loader = ComposeLoader(text)
    try:
        doc = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise ValueError(f"not YAML: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise ValueError("declares no services")
    return json.loads(json.dumps(doc))


def dump_compose(doc: dict[str, Any]) -> str:
    return yaml.dump(doc, Dumper=ComposeDumper, sort_keys=False, allow_unicode=True, width=4096)


# ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?error}, ${VAR:+other}, $VAR.
COMPOSE_VAR = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


def _strings(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def compose_variables(doc: dict[str, Any]) -> dict[str, str]:
    """Every variable the file reads, in order, with the default the file
    gives it. `$$` is compose's literal dollar and reads nothing."""
    found: dict[str, str] = {}
    for text in _strings(doc):
        for m in COMPOSE_VAR.finditer(text.replace("$$", "")):
            default = m.group(3) if m.group(2) in ("-", ":-") else ""
            found.setdefault(m.group(1) or m.group(4), default)
    return found


# A setting named like a password, a secret or a key. A *_FILE setting names
# the file an image reads its secret from: a path, which stays as written.
SECRET_WORDS = frozenset({"PASSWORD", "PASSWD", "PASS", "SECRET", "KEY"})


def secret_like(name: str) -> bool:
    upper = name.upper()
    if upper.endswith("_FILE"):
        return False
    words = [w for w in re.split(r"[^A-Z0-9]+", upper) if w]
    return any(w in SECRET_WORDS or w.endswith(("PASSWORD", "SECRET")) for w in words)


def lookup(length: int, chars: str = "ascii_letters,digits", lower: bool = False) -> str:
    """An expression for a value the host mints and keeps (catena-admin
    payload/engines/appenv): `length` letters and digits, or hex digits with
    chars="hexdigits" and lower, the two sets it mints."""
    pipe = " | lower" if lower else ""
    return f"{{{{ lookup('password', '/dev/null length={length} chars={chars}'){pipe} }}}}"


_GENERATED = re.compile(
    r"^\{\{ lookup\('password', '/dev/null length=(\d+) chars=(?:(ascii_letters,digits)'\)"
    r"|(hexdigits)'\) \| lower) \}\}$")


def env_spec(value: str) -> dict[str, Any]:
    """The block's env entry for one env value: an expression that mints letters
    and digits, or lowercase hex digits, is a generated secret; anything else,
    a lowercased lookup included, is a setting the host evaluates."""
    m = _GENERATED.match(value)
    if m:
        return {"generated": {"length": int(m.group(1)),
                              "alphabet": "alnum" if m.group(2) else "hex"}}
    return {"setting": {"default": value}}


def block_head(env: list[tuple[str, str]]) -> str:
    """The x-catena block an imported compose starts with: its env."""
    block = {model.BLOCK_KEY: {"schema": 2, "env": {name: env_spec(value) for name, value in env}}}
    return yaml.safe_dump(block, sort_keys=False, allow_unicode=True, width=4096) + "\n"


def minted(name: str) -> str:
    """The catalog's minted value for a setting named like a credential: 32
    characters for a password, 64 for a secret or a key."""
    return lookup(32 if "PASS" in name.upper() else 64)


def _drop_traefik(holder: dict[str, Any]) -> int:
    """Removes holder's Traefik labels, which would route the app around the
    host's own routes and gates, and says how many it removed."""
    labels = holder.get("labels")
    if isinstance(labels, dict):
        kept: Any = {k: v for k, v in labels.items() if not str(k).lower().startswith("traefik.")}
    elif isinstance(labels, list):
        kept = [item for item in labels if not str(item).lower().startswith("traefik.")]
    else:
        return 0
    if kept:
        holder["labels"] = kept
    else:
        holder.pop("labels")
    return len(labels) - len(kept)


def _add_labels(service: dict[str, Any], labels: dict[str, str]) -> None:
    current = service.get("labels")
    if isinstance(current, dict):
        current.update(labels)
    else:
        service["labels"] = list(current or []) + [f"{k}={v}" for k, v in labels.items()]


def _port(entry: Any) -> tuple[str, str] | None:
    """(container port, protocol) of one ports entry; None for a range or a
    port a variable sets."""
    if isinstance(entry, dict):
        target, protocol = str(entry.get("target", "")), str(entry.get("protocol") or "tcp")
    else:
        spec, _, protocol = str(entry).partition("/")
        target, protocol = spec.rsplit(":", 1)[-1], protocol or "tcp"
    return (target, protocol.lower()) if target.isdigit() else None


def _binds(service: dict[str, Any]) -> list[str]:
    out = []
    for item in service.get("volumes") or []:
        if isinstance(item, dict):
            if item.get("type") == "bind":
                out.append(str(item.get("source", "")))
        elif ":" in str(item) and str(item).startswith(("/", ".", "~", "$")):
            out.append(str(item).split(":", 1)[0])
    return out


def _environment(service: dict[str, Any]) -> dict[str, Any]:
    env = service.get("environment")
    if isinstance(env, dict):
        return env
    return {k: v for k, _, v in (str(item).partition("=") for item in env or [])}


@dataclass
class Stack:
    doc: dict[str, Any]
    service: str
    port: int
    to_choose: list[str]


def make_stack(doc: dict[str, Any], route: tuple[str, int] | None = None) -> Stack:
    """The compose as a catalog stack file, before the checker runs. The
    route labels go on `route`, the service and port the registry routes, or
    else on the first TCP port a service publishes."""
    doc.pop("version", None)
    services = doc["services"]
    to_choose: list[str] = []
    traefik = 0
    for name, service in services.items():
        if not isinstance(service, dict):
            raise Skip(f"service {name} is not a mapping")
        traefik += _drop_traefik(service)
        deploy = service.get("deploy") or {}
        traefik += _drop_traefik(deploy)
        policy = deploy.get("restart_policy") or {}
        # Swarm's restart conditions for what compose's restart says, when the
        # service keeps running. One that finishes keeps its restart key: the
        # checker turns it into its condition, and the swarm lint refuses it.
        if not policy.get("condition") and str(service.get("restart", "always")) in (
                "always", "unless-stopped"):
            policy["condition"] = "any"
            deploy["restart_policy"] = policy
            service.pop("restart", None)
        if deploy:
            service["deploy"] = deploy
        else:
            service.pop("deploy", None)
        for source in _binds(service):
            to_choose.append(f"bind mount {source} in service {name}: a named volume, or a "
                             f"folder the nightly backup covers")
        env_files = service.get("env_file") or []
        for env_file in env_files if isinstance(env_files, list) else [env_files]:
            to_choose.append(f"service {name} reads the env file {env_file}, which no deploy "
                             f"carries: its settings belong in the block's env")
        for key, value in _environment(service).items():
            if secret_like(key) and isinstance(value, str) and value and "$" not in value:
                to_choose.append(f"service {name} sets {key} to a fixed value in the compose "
                                 f"file: make it a setting, which gets a minted value")

    if route:
        why = "the registry's address for the app"
    else:
        why = "the first published TCP port"
        published = [(name, int(found[0])) for name, service in services.items()
                     for found in map(_port, service.get("ports") or [])
                     if found and found[1] == "tcp"]
        if not published:
            raise Skip("no service publishes a TCP port to route")
        route = published[0]
    name, port = route
    if name not in services:
        raise Skip(f"the registry routes service {name}, which the compose file does not define")
    service = services[name]
    ports = [entry for entry in service.get("ports") or [] if _port(entry) != (str(port), "tcp")]
    if ports:
        service["ports"] = ports
    else:
        service.pop("ports", None)
    _add_labels(service, {"vps.route.host": "${DOMAIN_HOST}", "vps.route.port": str(port)})
    note = f"route: service {name}, port {port}, {why}"
    if traefik:
        note += "; the registry's Traefik labels are dropped, as Catena routes the app itself"
    for other, svc in services.items():
        if svc.get("ports"):
            listed = ", ".join(_port_text(entry) for entry in svc["ports"])
            to_choose.append(f"ports service {other} publishes on the server ({listed}): "
                             f"declare each in vps.expose.tcp or vps.expose.udp, or drop it")
    return Stack(doc, name, port, [note] + to_choose)


def _port_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    published = f"{entry['published']}:" if entry.get("published") else ""
    return f"{published}{entry.get('target')}/{entry.get('protocol') or 'tcp'}"


_SETTING = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def settings_for(candidate: Candidate, doc: dict[str, Any], host: str) -> list[tuple[str, str]]:
    """The entry's env defaults before minting: the address, then what the
    registry declares, then what the compose reads besides."""
    settings = {"DOMAIN_HOST": host}
    for name, default in candidate.settings + list(compose_variables(doc).items()):
        if not _SETTING.match(name):
            raise Skip(f"the setting {name!r} is not a name compose can read")
        settings.setdefault(name, default)
    return list(settings.items())


def run_checker(applint: str, path: Path, app_name: str) -> list[dict[str, Any]]:
    """The findings catena-applint --fix leaves on the file it corrects in
    place. Two nodes, as the catalog check counts them, so every stateful
    service is pinned to the data node."""
    proc = subprocess.run(
        [applint, "--fix", "--json", "--nodes", "2", "--name", app_name, str(path)],
        capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode not in (0, 1):
        return [{"rule": "checker", "severity": "error",
                 "message": f"exit {proc.returncode}: {proc.stderr.strip()}"}]
    return [f for report in json.loads(proc.stdout or "[]")
            for f in report.get("findings") or [] if not f.get("fixed")]


def _finding(f: dict[str, Any]) -> str:
    where = f" {f['service']}" if f.get("service") else ""
    return f"{f['rule']} {f['severity']}{where}: {f['message']}"


# A checker finding at error severity as _finding writes it, and the line an
# entry records when no checker ran. make lint refuses an entry holding either.
CHECKER_ERROR = re.compile(r"^\S+ error(?: \S+)?: ")
CHECKER_NOT_RUN = "checker not run: "


@dataclass
class Report:
    seen: int = 0
    listed: int = 0
    deferred: int = 0
    imported: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    new_aliases: list[dict[str, str]] = field(default_factory=list)
    pull_requests: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(
        default_factory=lambda: {"licence": [], "unknown": [], "other": []})

    def lines(self, dry_run: bool) -> list[str]:
        out = [f"entries: {self.seen}"]
        if self.listed:
            out.append(f"listed with no compose file to import: {self.listed}")
        out.append(f"{'would import' if dry_run else 'imported'}: {len(self.imported)}")
        out += [f"  {line}" for line in self.imported]
        if self.aliases:
            out.append(f"aliases, recorded and not imported: {len(self.aliases)}")
            out += [f"  {line}" for line in self.aliases]
        if self.deferred:
            out.append(f"left for a later wave: {self.deferred}")
        if self.pull_requests:
            out.append(f"pull requests opened: {len(self.pull_requests)}")
            out += [f"  {line}" for line in self.pull_requests]
        for kind, title in (("licence", "skipped, licence not permitted"),
                            ("unknown", "skipped, licence unknown"),
                            ("other", "skipped, other reasons")):
            out.append(f"{title}: {len(self.skipped[kind])}")
            out += [f"  {line}" for line in self.skipped[kind]]
        return out


def card_of(source: dict[str, Any]) -> dict[str, Any]:
    """The entry as an app.json: the source without its Portainer fields and its web
    address (the compose holds it), each language gaining the card's one-line
    `summary` (the first line of the long description it keeps)."""
    cat = source["x-catena"]
    card: dict[str, Any] = {"schema": 1}
    for key in ("app_name", "upstream_url"):
        card[key] = cat[key]
    card["categories"] = source["categories"]
    for key in ("logo", "sso_mode", "status", "origin", "pending", "bench", "sizing"):
        if key in cat:
            card[key] = cat[key]
    for lang in ("en", "fr"):
        prose = dict(cat[lang])
        card[lang] = {"display_name": prose.pop("display_name"),
                      "summary": prose["compose_description"].strip().splitlines()[0], **prose}
    return card


def entry_paths(slug: str) -> list[str]:
    """The repo-relative paths an entry writes."""
    paths = [f"{model.BLUEPRINTS.name}/{slug}"]
    return paths if model.card_layout() else [*paths, f"{model.SOURCES.name}/{slug}.json"]


def write_entry(slug: str, source: dict[str, Any], compose: str) -> None:
    """The entry's files: its compose, held as neat files beside it (pack.unpack),
    and its card file (or, when the catalog has no catalog-meta.json, its source)."""
    app_dir = model.BLUEPRINTS / slug
    app_dir.mkdir(parents=True)
    (app_dir / model.COMPOSE_NAME).write_text(compose)
    pack.unpack(slug, model.BLUEPRINTS.parent)
    if model.card_layout():
        (app_dir / model.CARD_NAME).write_text(json.dumps(card_of(source), indent=2) + "\n")
    else:
        (model.SOURCES / f"{slug}.json").write_text(json.dumps(source, indent=2) + "\n")


@dataclass
class Prepared:
    """An entry whose app licence passed and whose compose file is a catalog
    stack file, before the checker runs. `keys` identify the app it is."""

    c: Candidate
    slug: str
    licence: Licence
    licence_checks: list[str]
    stack: Stack
    keys: frozenset[str]
    rich: int


class Importer:
    """`limit` is the most entries one run imports (a wave); `emit` writes
    one entry's files and returns the address of the pull request it opened,
    if any."""

    def __init__(self, registry: str, applint: str, dry_run: bool,
                 banned: list[tuple[str, re.Pattern[str]]], limit: int | None = None,
                 emit: Callable[[str, dict[str, Any], str], str | None] | None = None):
        self.registry = registry
        self.applint = applint
        self.dry_run = dry_run
        self.banned = banned
        self.limit = limit
        self.emit = emit or (lambda slug, source, compose: write_entry(slug, source, compose))
        self.licences: dict[tuple[str, str], Licence] = {}
        self.pins: dict[str, str] = {}
        self.found: dict[str, tuple[Repo | None, str]] = {}
        self.taken: set[str] = set()
        self.catalog = dedup.catalog_index()
        self.known = dedup.load_aliases()

    def run(self, candidates: list[Candidate]) -> Report:
        listings = {c.slug: c for c in candidates if c.listing and c.project}
        entries = [c for c in candidates if not c.listing]
        report = Report(seen=len(entries), listed=len(candidates) - len(entries))
        prepared: list[Prepared] = []
        for c in entries:
            c.registry = c.registry or self.registry
            recorded = self.known.get((c.registry, c.entry))
            if recorded:
                report.aliases.append(f"{c.entry}: already recorded as an alias of {recorded}")
                continue
            listed = listings.get(c.slug)
            if c.project is None and listed:
                c.project = listed.project
                c.to_choose.append(
                    f"upstream project: the listing {listed.source} names {listed.entry} at "
                    f"{listed.project}, matched by name: confirm it is this app")
            try:
                prepared.append(self._prepare(c))
            except Skip as exc:
                report.skipped[exc.kind].append(f"{c.entry}: {exc}")
        for group in dedup.groups(prepared):
            existing = next((self.catalog[k] for p in group for k in sorted(p.keys)
                             if k in self.catalog), None)
            if existing:
                for p in group:
                    report.aliases.append(f"{p.c.entry}: alias of {existing} (in the catalog)")
                    report.new_aliases.append({"app": existing, **dedup.reference(p)})
                continue
            if self.limit is not None and len(report.imported) >= self.limit:
                report.deferred += 1
                continue
            for p in group:
                try:
                    slug, source, compose = self._finish(p)
                except Skip as exc:
                    report.skipped[exc.kind].append(f"{p.c.entry}: {exc}")
                    continue
                rest = [q for q in group if q is not p]
                if rest:
                    source["x-catena"]["origin"]["aliases"] = [dedup.reference(q) for q in rest]
                    report.aliases += [f"{q.c.entry}: alias of {slug}" for q in rest]
                self.taken.add(slug)
                report.imported.append(f"{slug} ({p.c.entry})")
                if not self.dry_run:
                    url = self.emit(slug, source, compose)
                    if url:
                        report.pull_requests.append(url)
                break
        return report

    def _licence(self, repo: Repo | None, slug: str, problem: str = "",
                 app: bool = False) -> tuple[Licence, list[str]]:
        """The licence of `repo`, and what a person checks about it: an
        owner's page reads as the owner's repository named as the entry's id.
        The licence of the app's own project (`app`) admits the entry when it
        is one of APP_LICENCES. The licence of a repository a file is copied
        from is recorded with its notice and refuses nothing; when the forge
        names none it reads NOASSERTION, attributed to the repository's
        address."""
        if repo is None:
            raise Skip(problem or "no repository names its licence", "unknown")
        page = ""
        checks: list[str] = []
        if "/" not in repo.path:
            page = f"https://{repo.host}/{repo.path}"
            repo = Repo(repo.host, f"{repo.path}/{slug}")
            checks.append(f"upstream repository: the registry links the owner's page {page}, and "
                          f"the licence is read from {repo}, its repository named as the entry: "
                          f"confirm it holds the app")
        key = (repo.host, repo.path.lower())
        if key not in self.licences:
            self.licences[key] = repo_licence(repo)
        licence = self.licences[key]
        if not app:
            return licence if licence.spdx else Licence("NOASSERTION", f"https://{repo.host}/{repo.path}"), checks
        if not licence.spdx:
            where = f"{page} is an owner's page, read as {repo}: " if page else ""
            raise Skip(where + licence.problem, "unknown")
        if licence.spdx not in APP_LICENCES:
            raise Skip(f"{licence.spdx} ({repo})", "licence")
        return licence, checks

    def _project_licence(self, c: Candidate) -> None:
        """The app's own licence, its upstream project's, is one of
        APP_LICENCES."""
        if c.project is not None:
            try:
                self._licence(c.project, c.slug, app=True)
            except Skip as exc:
                raise Skip(f"upstream project {exc}", exc.kind) from exc

    def _pin(self, repo: Repo) -> str:
        if repo.path not in self.pins:
            try:
                self.pins[repo.path] = pin_commit(repo)
            except (FetchError, ValueError) as exc:
                raise Skip(f"cannot pin {repo} to a commit: {exc}") from exc
        return self.pins[repo.path]

    def _compose(self, candidate: Candidate) -> dict[str, Any]:
        if candidate.compose is not None:
            return candidate.compose
        url = candidate.compose_url
        if candidate.compose_repo:
            ref = self._pin(candidate.compose_repo)
            url = raw_url(candidate.compose_repo, candidate.compose_path, ref)
            candidate.commit, candidate.path = ref, candidate.compose_path.removeprefix("./")
            if candidate.env_path:
                self._env_defaults(candidate, raw_url(candidate.compose_repo, candidate.env_path, ref))
        try:
            text = fetch(url).decode("utf-8", "replace")
        except FetchError as exc:
            raise Skip(f"compose file: {exc}") from exc
        try:
            doc = load_compose(text)
        except ValueError as exc:
            raise Skip(f"compose file {url}: {exc}") from exc
        return candidate.convert(doc) if candidate.convert else doc

    @staticmethod
    def _env_defaults(candidate: Candidate, url: str) -> None:
        """The KEY=value lines of the registry's env file are settings; a
        missing file is no setting."""
        try:
            text = fetch(url).decode("utf-8", "replace")
        except FetchError:
            return
        named = {name for name, _ in candidate.settings}
        for line in text.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m and m.group(1) not in named:
                candidate.settings.append((m.group(1), m.group(2).strip().strip("\"'")))
                named.add(m.group(1))

    def _prepare(self, c: Candidate) -> Prepared:
        if c.skip:
            raise Skip(c.skip)
        slug = c.slug
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
            raise Skip("the entry gives no name to make an id from")
        for what, text in (("title", c.title), ("id", slug)):
            word = next((word for word, rx in self.banned if rx.search(text)), None)
            if word:
                raise Skip(f"its {what} {text!r} holds {word}, a word on the banned-word list "
                           f"check:unicode refuses")
        if (slug in self.taken or (model.SOURCES / f"{slug}.json").exists()
                or (model.BLUEPRINTS / slug).exists()):
            raise Skip(f"the id {slug} is already in the catalog")
        licence, licence_checks = self._licence(c.licence_repo, slug, c.licence_problem)
        self._project_licence(c)
        stack = make_stack(self._compose(c), c.route)
        if c.project is None:
            image = str(stack.doc["services"][stack.service].get("image") or "")
            if image not in self.found:
                self.found[image] = upstream.find(image)
            c.project, where = self.found[image]
            if c.project is None:
                raise Skip(f"the upstream repository is unknown: {image} names no source label "
                           f"and no linked source on Docker Hub", "unknown")
            c.to_choose.append(f"upstream project: {c.project} comes from {where} of {image}: "
                               f"confirm it is this app")
            self._project_licence(c)
        return Prepared(c, slug, licence, licence_checks, stack,
                        frozenset(dedup.keys(stack, c.project)), dedup.richness(c, stack))

    def _finish(self, p: Prepared) -> tuple[str, dict[str, Any], str]:
        c, slug, licence, licence_checks, stack = p.c, p.slug, p.licence, p.licence_checks, p.stack
        if slug in self.taken:
            raise Skip(f"the id {slug} is already in the catalog")
        host = f"{slug}.{ZONE}"
        env = [(name, value if render.LOOKUP_PASSWORD_RE.search(value)
                else minted(name) if secret_like(name) else value)
               for name, value in settings_for(c, stack.doc, host)]
        app_name = f"catena-{slug}"
        compose_file = f"{model.BLUEPRINTS.name}/{slug}/{model.COMPOSE_NAME}"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / model.COMPOSE_NAME
            path.write_text(dump_compose(stack.doc))
            checked = run_checker(self.applint, path, app_name) if self.applint else None
            compose = path.read_text()

        findings = [_finding(f) for f in checked or []]
        if checked is None:
            findings.append(f"{CHECKER_NOT_RUN}name catena-applint with --applint or {APPLINT_ENV}")
        findings += [f"catalog lint: {e}" for e in swarm_lint.lint_compose(compose, label=compose_file)]
        _, pins = postgres_pins.pin_errors(slug, compose)
        findings += [f"catalog lint: {e}" for e in pins]

        secrets = [name for name, value in env if render.LOOKUP_PASSWORD_RE.search(value)]
        images = ", ".join(f"{svc.get('image', '?')} (service {name})"
                           for name, svc in stack.doc["services"].items())
        to_choose = stack.to_choose + c.to_choose + licence_checks + [
            f"pinned version: {images}",
            f"access mode: no vps.auth.mode label, so only administrators reach {slug}.<domain>",
            f"host name: {slug}.<domain> is a placeholder",
            f"upstream URL: {c.upstream_url} comes from the registry; the project's home page "
            f"belongs there",
            "EN/FR text: what it is, what it replaces and the setup steps; the French text "
            "quotes the registry's English description",
            "sizing: peak_ram_mb is not measured",
            "bench pack and fixture",
            "SSO: whether the app signs in through Keycloak (sign-in labels, sso_mode)",
            "quiesce hooks: whether its live state needs a backup mode",
        ]
        to_choose += [f"a feature the security rules refuse: {f['rule']} on service "
                      f"{f.get('service', '?')}" for f in checked or []
                      if f["rule"] in REFUSED_RULES]
        if secrets:
            to_choose.append(f"minted on the host: {', '.join(secrets)}; a credential an "
                             f"outside service issues is entered by hand")
        to_choose = list(dict.fromkeys(to_choose))

        description = c.description or c.title or slug
        readme = f"{render.REPO_GIT_URL}/blob/main/blueprints/{slug}/README.md"
        french = f"Description du registre, non traduite : {description}"
        origin = {
            "registry": c.registry or self.registry,
            "entry": c.entry,
            "source": c.source,
            "licence": licence.spdx,
            "attribution": "; ".join(filter(None, [licence.attribution, c.notice])),
        }
        for key, value in (("commit", c.commit), ("path", c.path),
                           ("project", f"https://{c.project.host}/{c.project.path}"
                            if c.project else "")):
            if value:
                origin[key] = value
        source = {
            "id": slug,
            "type": 2,
            "title": c.title or slug,
            "name": app_name,
            "categories": c.categories or ["Other"],
            "platform": "linux",
            "x-catena": {
                "app_name": app_name,
                "status": "imported",
                "upstream_url": c.upstream_url,
                "sso_mode": "none",
                "domain": {"host": host, "service": stack.service, "port": stack.port},
                "origin": origin,
                "pending": {"findings": findings, "to_choose": to_choose},
                "bench": {"pack": None, "fixture": "skip"},
                "sizing": {"peak_ram_mb": None},
                "en": {
                    "display_name": c.title or slug,
                    "what_it_is": description,
                    "replaces": [],
                    "compose_description": f"{description}\n\n{readme}\n",
                    "setup_steps": "Not written yet: this entry is imported and untested.\n",
                },
                "fr": {
                    "display_name": c.title or slug,
                    "what_it_is": french,
                    "replaces": [],
                    "compose_description": f"{french}\n\n{readme}\n",
                    "setup_steps": "Pas encore rédigées : ce modèle est importé et non testé.\n",
                },
            },
        }
        return slug, source, block_head(env) + compose


def main(argv: list[str], adapters: dict[str, Callable[[Any, str], list[Candidate]]],
         runner: Callable[[list[str]], str] | None = None,
         render_fn: Callable[[], int] | None = None, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import_registry.py",
        description="Import a template registry into blueprints/ (and sources/ for a "
                    "catalog that holds its apps as sources) as entries "
                    "marked imported and untested.")
    parser.add_argument("registry", help="address of the registry document (https:// or file://)")
    parser.add_argument("--format", choices=sorted(adapters), default=next(iter(adapters)),
                        help="the registry's format")
    parser.add_argument("--also", action="append", default=[], metavar="FORMAT=ADDRESS",
                        help="a further registry read in the same run, so an app both list "
                             "is imported once, from the richer entry")
    parser.add_argument("--entry", action="append", default=[], metavar="TITLE",
                        help="read only the entries of this title, case ignored (repeatable); "
                             "titles of one app named together give one app and its aliases")
    parser.add_argument("--applint", default=os.environ.get(APPLINT_ENV, ""),
                        help=f"the catena-applint binary (default: ${APPLINT_ENV})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported and write nothing")
    parser.add_argument("--wave", type=int, metavar="N",
                        help="import up to N new apps, each on its own branch import/<id> with "
                             "its own pull request; run it in a clean checkout of the base branch")
    parser.add_argument("--daily-limit", type=int, default=publish.DAILY_LIMIT, metavar="N",
                        help=f"most import pull requests opened per UTC day (default {publish.DAILY_LIMIT})")
    parser.add_argument("--open-limit", type=int, default=publish.OPEN_LIMIT, metavar="N",
                        help=f"most import pull requests open at once (default {publish.OPEN_LIMIT})")
    args = parser.parse_args(argv)

    sources = [(args.format, args.registry)]
    for item in args.also:
        fmt, _, address = item.partition("=")
        if fmt not in adapters or not address:
            print(f"import_registry: --also wants FORMAT=ADDRESS with a format from "
                  f"{', '.join(sorted(adapters))}: {item!r}", file=sys.stderr)
            return 1
        sources.append((fmt, address))

    limit: int | None = None
    publisher: publish.Publisher | None = None
    if args.wave is not None:
        if args.wave < 1:
            print("import_registry: --wave wants a number of apps above 0", file=sys.stderr)
            return 1
        run = runner or publish.run_command
        today = today or datetime.now(timezone.utc).date()
        try:
            github = publish.GitHub(run)
            allowance = publish.allowance(args.wave, args.daily_limit, args.open_limit,
                                       github.pull_requests("open"),
                                       github.pull_requests("all", f"created:>={today.isoformat()}"),
                                       today)
        except publish.WaveError as exc:
            print(f"import_registry: {exc}", file=sys.stderr)
            return 1
        print(f"wave: {allowance.size} of {args.wave} ({allowance.reason})")
        if allowance.size == 0:
            return 0
        limit = allowance.size
        if not args.dry_run:
            publisher = publish.Publisher(run, render_fn or render.render_all)

    try:
        banned = banned_words()
    except (OSError, ValueError) as exc:
        print(f"import_registry: cannot read the banned-word list {BANNED_WORDS}: {exc}",
              file=sys.stderr)
        return 1
    candidates: list[Candidate] = []
    try:
        for fmt, address in sources:
            for c in adapters[fmt](json.loads(fetch(address)), address):
                c.registry = c.registry or address
                candidates.append(c)
    except (FetchError, ValueError) as exc:
        print(f"import_registry: cannot read the registry: {exc}", file=sys.stderr)
        return 1
    if args.entry:
        titles = {c.entry.casefold() for c in candidates if not c.listing}
        missing = [title for title in args.entry if plain(title).casefold() not in titles]
        if missing:
            print(f"import_registry: no entry titled {', '.join(map(repr, missing))}", file=sys.stderr)
            return 1
        wanted = {plain(title).casefold() for title in args.entry}
        candidates = [c for c in candidates if c.listing or c.entry.casefold() in wanted]
    applint = args.applint.strip()
    if applint and shutil.which(applint) is None:
        print(f"import_registry: WARN: {applint} is not an executable", file=sys.stderr)
        applint = ""
    if not applint:
        print("import_registry: WARN: no catena-applint, so the compose files are written "
              "without its corrections; each entry records that", file=sys.stderr)

    emit = None
    if publisher:
        def emit(slug: str, source: dict[str, Any], compose: str) -> str | None:
            return publisher.publish(
                slug, publish.pull_request_title(source), publish.pull_request_body(source),
                lambda: write_entry(slug, source, compose),
                entry_paths(slug))
    try:
        if publisher:
            publisher.start()
        report = Importer(args.registry, applint, args.dry_run, banned, limit, emit).run(candidates)
        if report.new_aliases and not args.dry_run:
            if publisher:
                url = publisher.publish(
                    "aliases", "chore(import): record aliases of apps already in the catalog",
                    publish.alias_body(report.new_aliases), lambda: dedup.add_aliases(report.new_aliases),
                    [model.aliases_path().relative_to(model.BLUEPRINTS.parent).as_posix()],
                    render=False)
                report.pull_requests.append(url)
            else:
                dedup.add_aliases(report.new_aliases)
    except publish.WaveError as exc:
        print(f"import_registry: {exc}", file=sys.stderr)
        return 1
    print(f"registry {args.registry}")
    for line in report.lines(args.dry_run):
        print(line)
    if report.imported and not args.dry_run and not publisher:
        print("next: run make, then settle each entry's x-catena.pending")
    return 0


from . import dedup, publish, upstream  # noqa: E402
