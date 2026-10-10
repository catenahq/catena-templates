"""What every registry adapter shares: from Candidates to catalog entries
marked imported and untested.

An adapter reads one registry format into Candidates. For each, in order:

  id        the adapter's slug, refused when a source file or a blueprint
            directory already holds it: the importer never overwrites.
  licence   GitHub's licence API, for the repository whose text the entry
            copies: the one a stack entry's compose file sits in, or the
            registry's own for a compose the adapter writes from the
            registry's fields. A licence outside sources.schema.json's
            origin.licence list skips the entry; so does a licence the API
            cannot name, and a source off GitHub.
  compose   fetched for a stack entry, or the adapter's, then made into a
            catalog stack file: no version key, no Traefik labels, a restart
            policy on each service that keeps running, and the route labels
            on the first service publishing a TCP port. The proxy reaches that
            port over catena-network, so it is not published on the server.
  settings  each variable the registry declares or the compose reads is an
            env default; one named like a password, secret or key gets the
            catalog's minted value.
  checker   catena-applint --fix (catena-admin payload/cmd/catena-applint)
            corrects the file. What it leaves, and what the catalog's swarm
            lint and Postgres pin rule refuse, is recorded on the entry.

Each entry is written as sources/<id>.json and
blueprints/<id>/docker-compose.yml with status "imported", its origin and
what a person still has to choose, and its id joins the _meta.json order.
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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .. import model, postgres_pins, render, swarm_lint

GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
HTTP_TIMEOUT = 30
USER_AGENT = "catena-templates-import-registry"

# The checker's CLI when --applint names none.
APPLINT_ENV = "CATENA_APPLINT"

# The host every imported entry answers on until a person picks one, in the
# form the host's catalog render resolves.
ZONE = "{{ cloudflare_zone }}"

# The checker's rules that refuse a feature giving the app the server itself.
REFUSED_RULES = frozenset({"X1", "X2", "X3", "X4", "X5", "X6"})


@dataclass
class Candidate:
    """One registry entry as an adapter reads it.

    `compose` is a stack file the adapter writes from the entry's fields;
    `compose_url` is where a stack entry's file is fetched from. The licence
    that applies is the one of `licence_repo` on GitHub; an adapter that has
    no repository to name says why in `licence_problem`. A non-empty `skip`
    keeps the entry out, with that reason."""

    entry: str
    slug: str = ""
    title: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    settings: list[tuple[str, str]] = field(default_factory=list)
    compose: dict[str, Any] | None = None
    compose_url: str = ""
    licence_repo: tuple[str, str] | None = None
    licence_problem: str = ""
    source: str = ""
    upstream_url: str = ""
    to_choose: list[str] = field(default_factory=list)
    skip: str = ""


class Skip(Exception):
    """An entry the importer leaves out. `kind` sorts the report: "licence"
    for a licence outside the permitted list, "unknown" for one nobody can
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


_GITHUB_REPO = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_RAW_FILE = re.compile(r"^https?://raw\.githubusercontent\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/")


def github_repo(url: str) -> tuple[str, str] | None:
    """(owner, repository) of a GitHub repository address or of a raw file
    in one; None for anything else."""
    m = _GITHUB_REPO.match(url.strip()) or _RAW_FILE.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def raw_url(repo: tuple[str, str], path: str) -> str:
    """A file on the repository's default branch."""
    rel = path.strip().removeprefix("./").lstrip("/")
    return f"{RAW_GITHUB}/{repo[0]}/{repo[1]}/HEAD/{rel}"


@dataclass(frozen=True)
class Licence:
    """`spdx` is empty when the licence is unknown, and `problem` says why."""

    spdx: str
    attribution: str
    problem: str = ""


_COPYRIGHT = re.compile(r"^copyright\b", re.IGNORECASE)
# The Apache licence's appendix carries a copyright line to fill in.
_PLACEHOLDER = re.compile(r"\[yyyy\]|\{yyyy\}|\[year\]|<year>", re.IGNORECASE)


def repo_licence(repo: tuple[str, str]) -> Licence:
    """The licence GitHub detects for the repository, with the copyright
    lines of its licence file. GITHUB_TOKEN, when set, lifts the API's rate
    limit for anonymous callers."""
    owner, name = repo
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        doc = json.loads(fetch(f"{GITHUB_API}/repos/{owner}/{name}/license", headers))
    except FetchError as exc:
        if exc.status == 404:
            return Licence("", "", f"GitHub finds no licence file in {owner}/{name}")
        return Licence("", "", f"the licence of {owner}/{name} cannot be read: {exc}")
    except ValueError:
        return Licence("", "", f"GitHub's licence answer for {owner}/{name} is not JSON")
    spdx = str((doc.get("license") or {}).get("spdx_id") or "")
    url = str(doc.get("html_url") or f"https://github.com/{owner}/{name}")
    if spdx in ("", "NOASSERTION"):
        return Licence("", "", f"GitHub cannot name the licence in {url}")
    text = ""
    if doc.get("encoding") == "base64":
        text = base64.b64decode(doc.get("content") or "").decode("utf-8", "replace")
    lines: list[str] = []
    for line in (plain(raw) for raw in text.splitlines()):
        if _COPYRIGHT.match(line) and not _PLACEHOLDER.search(line) and line not in lines:
            lines.append(line)
    return Licence(spdx, "; ".join(lines[:3]) or url)


def permissive_licences() -> tuple[str, ...]:
    """origin.licence's list in sources.schema.json, which refuses any other
    licence on a source file."""
    schema = json.loads(model.SOURCE_SCHEMA.read_text())
    origin = schema["properties"]["x-catena"]["properties"]["origin"]
    return tuple(origin["properties"]["licence"]["enum"])


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
_COMPOSE_VAR = re.compile(
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
        for m in _COMPOSE_VAR.finditer(text.replace("$$", "")):
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


def minted(name: str) -> str:
    """The catalog's minted value, which each host's catalog render
    (catena-admin shell/marketplace) mints and keeps: 32 characters for a
    password, 64 for a secret or a key."""
    length = 32 if "PASS" in name.upper() else 64
    return f"{{{{ lookup('password', '/dev/null length={length} chars=ascii_letters,digits') }}}}"


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


def make_stack(doc: dict[str, Any]) -> Stack:
    """The compose as a catalog stack file, before the checker runs."""
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
                             f"carries: its settings belong in env_defaults")
        for key, value in _environment(service).items():
            if secret_like(key) and isinstance(value, str) and value and "$" not in value:
                to_choose.append(f"service {name} sets {key} to a fixed value in the compose "
                                 f"file: make it a setting, which gets a minted value")

    for name, service in services.items():
        ports = service.get("ports") or []
        for index, entry in enumerate(ports):
            found = _port(entry)
            if found and found[1] == "tcp":
                break
        else:
            continue
        port = int(found[0])
        del ports[index]
        if not ports:
            service.pop("ports")
        _add_labels(service, {"vps.route.host": "${DOMAIN_HOST}", "vps.route.port": str(port)})
        note = f"route: service {name}, port {port}, the first published TCP port"
        if traefik:
            note += "; the registry's Traefik labels are dropped, as Catena routes the app itself"
        for other, svc in services.items():
            if svc.get("ports"):
                listed = ", ".join(_port_text(entry) for entry in svc["ports"])
                to_choose.append(f"ports service {other} publishes on the server ({listed}): "
                                 f"declare each in vps.expose.tcp or vps.expose.udp, or drop it")
        return Stack(doc, name, port, [note] + to_choose)
    raise Skip("no service publishes a TCP port to route")


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


@dataclass
class Report:
    seen: int = 0
    imported: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(
        default_factory=lambda: {"licence": [], "unknown": [], "other": []})

    def lines(self, dry_run: bool) -> list[str]:
        out = [f"entries: {self.seen}",
               f"{'would import' if dry_run else 'imported'}: {len(self.imported)}"]
        out += [f"  {line}" for line in self.imported]
        for kind, title in (("licence", "skipped, licence not permitted"),
                            ("unknown", "skipped, licence unknown"),
                            ("other", "skipped, other reasons")):
            out.append(f"{title}: {len(self.skipped[kind])}")
            out += [f"  {line}" for line in self.skipped[kind]]
        return out


class Importer:
    def __init__(self, registry: str, applint: str, dry_run: bool):
        self.registry = registry
        self.applint = applint
        self.dry_run = dry_run
        self.allowed = permissive_licences()
        self.licences: dict[tuple[str, str], Licence] = {}
        self.taken: set[str] = set()
        self.postgres_default = model.load_meta()["postgres_default_image"]

    def run(self, candidates: list[Candidate]) -> Report:
        report = Report(seen=len(candidates))
        written: list[str] = []
        for candidate in candidates:
            try:
                slug, source, compose = self._entry(candidate)
            except Skip as exc:
                report.skipped[exc.kind].append(f"{candidate.entry}: {exc}")
                continue
            self.taken.add(slug)
            report.imported.append(f"{slug} ({candidate.entry})")
            if self.dry_run:
                continue
            app_dir = model.BLUEPRINTS / slug
            app_dir.mkdir(parents=True)
            (app_dir / model.COMPOSE_NAME).write_text(compose)
            (model.SOURCES / f"{slug}.json").write_text(json.dumps(source, indent=2) + "\n")
            written.append(slug)
        if written:
            meta = model.load_meta()
            meta["order"] = list(meta.get("order") or []) + written
            (model.SOURCES / model.META_NAME).write_text(json.dumps(meta, indent=2) + "\n")
        return report

    def _licence(self, candidate: Candidate) -> Licence:
        repo = candidate.licence_repo
        if repo is None:
            raise Skip(candidate.licence_problem or "no repository names its licence", "unknown")
        key = (repo[0].lower(), repo[1].lower())
        if key not in self.licences:
            self.licences[key] = repo_licence(repo)
        licence = self.licences[key]
        if not licence.spdx:
            raise Skip(licence.problem, "unknown")
        if licence.spdx not in self.allowed:
            raise Skip(f"{licence.spdx} ({repo[0]}/{repo[1]})", "licence")
        return licence

    def _compose(self, candidate: Candidate) -> dict[str, Any]:
        if candidate.compose is not None:
            return candidate.compose
        try:
            text = fetch(candidate.compose_url).decode("utf-8", "replace")
        except FetchError as exc:
            raise Skip(f"compose file: {exc}") from exc
        try:
            return load_compose(text)
        except ValueError as exc:
            raise Skip(f"compose file {candidate.compose_url}: {exc}") from exc

    def _entry(self, c: Candidate) -> tuple[str, dict[str, Any], str]:
        if c.skip:
            raise Skip(c.skip)
        slug = c.slug
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
            raise Skip("the entry gives no name to make an id from")
        if (slug in self.taken or (model.SOURCES / f"{slug}.json").exists()
                or (model.BLUEPRINTS / slug).exists()):
            raise Skip(f"the id {slug} is already in the catalog")
        licence = self._licence(c)
        stack = make_stack(self._compose(c))
        host = f"{slug}.{ZONE}"
        settings = settings_for(c, stack.doc, host)
        app_name = f"catena-{slug}"
        compose_file = f"{model.BLUEPRINTS.name}/{slug}/{model.COMPOSE_NAME}"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / model.COMPOSE_NAME
            path.write_text(dump_compose(stack.doc))
            checked = run_checker(self.applint, path, app_name) if self.applint else None
            compose = path.read_text()

        findings = [_finding(f) for f in checked or []]
        if checked is None:
            findings.append(f"checker not run: name catena-applint with --applint or {APPLINT_ENV}")
        findings += [f"catalog lint: {e}" for e in swarm_lint.lint_compose(compose, label=compose_file)]
        _, pins = postgres_pins.pin_errors(slug, compose, override=None,
                                           default=self.postgres_default)
        findings += [f"catalog lint: {e}" for e in pins]

        secrets = [name for name, _ in settings if secret_like(name)]
        images = ", ".join(f"{svc.get('image', '?')} (service {name})"
                           for name, svc in stack.doc["services"].items())
        to_choose = stack.to_choose + c.to_choose + [
            f"pinned version: {images}",
            f"access mode: no vps.auth.mode label, so only administrators reach {slug}.<domain>",
            f"host name: {slug}.<domain> is a placeholder",
            f"upstream URL: {c.upstream_url} is where the template comes from; the project's "
            f"home page belongs there",
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
                "env_defaults": [f"{name}={minted(name) if secret_like(name) else value}"
                                 for name, value in settings],
                "origin": {
                    "registry": self.registry,
                    "entry": c.entry,
                    "source": c.source,
                    "licence": licence.spdx,
                    "attribution": licence.attribution,
                },
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
        return slug, source, compose


def main(argv: list[str], adapters: dict[str, Callable[[Any, str], list[Candidate]]]) -> int:
    parser = argparse.ArgumentParser(
        prog="import_registry.py",
        description="Import a template registry into sources/ and blueprints/ as entries "
                    "marked imported and untested.")
    parser.add_argument("registry", help="address of the registry document (https:// or file://)")
    parser.add_argument("--format", choices=sorted(adapters), default=next(iter(adapters)),
                        help="the registry's format")
    parser.add_argument("--applint", default=os.environ.get(APPLINT_ENV, ""),
                        help=f"the catena-applint binary (default: ${APPLINT_ENV})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported and write nothing")
    args = parser.parse_args(argv)

    try:
        candidates = adapters[args.format](json.loads(fetch(args.registry)), args.registry)
    except (FetchError, ValueError) as exc:
        print(f"import_registry: cannot read the registry: {exc}", file=sys.stderr)
        return 1
    applint = args.applint.strip()
    if applint and shutil.which(applint) is None:
        print(f"import_registry: WARN: {applint} is not an executable", file=sys.stderr)
        applint = ""
    if not applint:
        print("import_registry: WARN: no catena-applint, so the compose files are written "
              "without its corrections; each entry records that", file=sys.stderr)

    report = Importer(args.registry, applint, args.dry_run).run(candidates)
    print(f"registry {args.registry}")
    for line in report.lines(args.dry_run):
        print(line)
    if report.imported and not args.dry_run:
        print("next: run make, then settle each entry's x-catena.pending")
    return 0
