"""The Dokploy template registry (github.com/Dokploy/templates): one
directory per app under blueprints/, holding docker-compose.yml,
template.toml and meta.json.

The registry document is GitHub's listing of the repository's files at one
commit, and every file is read at that commit:

  https://api.github.com/repos/Dokploy/templates/git/trees/COMMIT?recursive=1

The fields read are those the registry's own processor reads (Dokploy/dokploy
packages/server/src/templates/processors.ts):

  meta.json           name, description, tags, links.github (the app's own
                      repository, whose licence applies), links.website
  [variables]         name = value; ${name} in a value is another variable or
                      a helper
  [config] env        KEY=value lines, a list or a table, written to the .env
                      file the compose file reads as ${KEY} and through
                      env_file: .env. A ${NAME} that is neither a helper nor a
                      variable is left to that file, which reads its own lines.
  [[config.domains]]  serviceName, port, host, path; the first is the app's
                      address
  [[config.mounts]]   filePath, content: a file written beside the stack, which
                      the compose file mounts from ../files/<filePath>

The helpers, and what each becomes:

  domain              the app's address; a variable naming the host of a
                      further domain gets a host of its own
  password[:N]        N (16) lowercase letters and digits, minted
  base64[:N]          N (32) random bytes in base64, minted
  hash[:N]            N (8) lowercase hex digits, minted
  jwt:N               N bytes as 2N lowercase hex digits, minted
  username            a lowercase name, minted
  email               the server's admin email
  timestamps:DATE     DATE in seconds; timestampms:DATE in milliseconds
  uuid, randomPort, jwt, jwt:SECRET[:PAYLOAD], timestamp, timestamps,
  timestampms         nothing the host mints: a choice to make

The host mints one value per setting (catena-admin shell/marketplace). So a
generated value that two env lines read is one setting, and a line that
combines it with other text is written into the compose file in place of
its own.

The compose file and template.toml are the registry's text: the registry's
licence must be permitted, and its notice joins each entry's attribution.
The licence that admits an entry is the app's.
"""
from __future__ import annotations

import functools
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import pipeline
from .pipeline import Candidate, github_repo, lookup, plain, slugify

FILES = ("meta.json", "template.toml", "docker-compose.yml")

_TREE = re.compile(
    r"^https://api\.github\.com/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/git/trees/([^/?#]+)")
# A reference, as the registry's processor finds it.
_REF = re.compile(r"\$\{([^}]+)\}")
# A name the .env file substitutes, with the default it may give.
_ENV_REF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?::?-(.*))?$")
# A reference in a compose file, or compose's literal dollar.
_COMPOSE_REF = re.compile(r"\$\$|" + pipeline.COMPOSE_VAR.pattern)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNMINTED = {"uuid": "a random UUID", "randomPort": "a random port",
             "timestamp": "the time of the deploy", "timestamps": "the time of the deploy",
             "timestampms": "the time of the deploy"}


@dataclass(frozen=True)
class Atom:
    """A value the registry generates or the host supplies. Every line that
    reads one variable reads the same atom, by `key`. `value` is its env
    default. `kind` is "mint" for a value the host mints, "fact" for one it
    knows, the same in every line, and "choose" for one a person sets,
    which `note` describes."""

    key: str
    kind: str
    value: str = ""
    note: str = ""


def read(doc: Any, registry_url: str) -> list[Candidate]:
    m = _TREE.match(registry_url)
    if not m or not isinstance(doc, dict) or not isinstance(doc.get("tree"), list):
        raise ValueError("not GitHub's file listing of a repository at a commit "
                         "(https://api.github.com/repos/OWNER/REPO/git/trees/COMMIT?recursive=1)")
    if doc.get("truncated"):
        raise ValueError("GitHub's file listing is truncated")
    owner, repo, ref = m.groups()
    notice = _notice((owner, repo))
    paths = {str(e.get("path")) for e in doc["tree"] if isinstance(e, dict) and e.get("type") == "blob"}
    ids = sorted({p.split("/")[1] for p in paths if re.match(r"^blueprints/[^/]+/", p)})
    return [_candidate(ident, paths, notice,
                       f"{pipeline.RAW_GITHUB}/{owner}/{repo}/{ref}/blueprints/{ident}",
                       f"https://github.com/{owner}/{repo}/tree/{ref}/blueprints/{ident}")
            for ident in ids]


def _notice(repo: tuple[str, str]) -> str:
    licence = pipeline.repo_licence(repo)
    if not licence.spdx:
        raise ValueError(f"the registry's own licence is unknown: {licence.problem}")
    if licence.spdx not in pipeline.permissive_licences():
        raise ValueError(f"the registry's own licence, {licence.spdx}, is not one the catalog "
                         f"imports from")
    return f"compose file and settings, {licence.spdx}: {licence.attribution}"


def _candidate(ident: str, paths: set[str], notice: str, base: str, source: str) -> Candidate:
    c = Candidate(entry=ident, slug=slugify(ident), source=source, notice=notice)
    missing = [name for name in FILES if f"blueprints/{ident}/{name}" not in paths]
    if missing:
        c.skip = f"the registry has no {' or '.join(missing)} for it"
        return c
    try:
        meta = json.loads(pipeline.fetch(f"{base}/meta.json"))
    except (pipeline.FetchError, ValueError) as exc:
        c.skip = f"meta.json: {exc}"
        return c
    try:
        template = tomllib.loads(pipeline.fetch(f"{base}/template.toml").decode())
    except (pipeline.FetchError, ValueError) as exc:
        c.skip = f"template.toml: {exc}"
        return c
    if not isinstance(meta, dict):
        c.skip = "meta.json holds no object"
        return c

    links = meta.get("links") if isinstance(meta.get("links"), dict) else {}
    upstream = str(links.get("github") or "").strip()
    c.entry = c.title = plain(meta.get("name")) or ident
    c.description = plain(meta.get("description")) or c.title
    c.categories = [tag for tag in (plain(x) for x in meta.get("tags") or []) if tag]
    c.upstream_url = str(links.get("website") or "").strip() or upstream or source
    c.licence_repo = github_repo(upstream)
    if c.licence_repo is None:
        c.licence_problem = (f"its upstream {upstream or '(none)'} is not a GitHub repository, "
                             f"so the app's licence is unknown")
    c.compose_url = f"{base}/docker-compose.yml"
    _configure(c, template)
    return c


def _text(value: Any) -> str:
    """A TOML value as the registry writes it into a line."""
    return "true" if value is True else "false" if value is False else str(value)


def _env(config: dict[str, Any]) -> dict[str, str]:
    """The env lines, KEY to value. A list's comments and blank lines go."""
    env = config.get("env")
    if isinstance(env, dict):
        return {str(key): _text(value) for key, value in env.items()}
    lines: dict[str, str] = {}
    for line in env or []:
        key, sep, value = str(line).partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            lines[key] = value
    return lines


def _configure(c: Candidate, template: dict[str, Any]) -> None:
    """The route, the settings, the compose conversion and the choices one
    template.toml gives."""
    config = template.get("config") or {}
    domains = [d for d in config.get("domains") or [] if isinstance(d, dict) and d.get("serviceName")]
    variables = {str(k): _text(v) for k, v in (template.get("variables") or {}).items()}
    t = _Template(c.slug, variables, _env(config), domains)

    if domains:
        first = domains[0]
        port = _text(first.get("port", "")).strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            c.skip = f"the registry's address names no port for service {first['serviceName']}"
            return
        c.route = (str(first["serviceName"]), int(port))
        host = t.shown(first.get("host"))
        if host != t.shown("${domain}"):
            c.to_choose.append(f"the registry's first address is {host or 'a random host'}; the "
                               f"entry answers on {t.shown('${domain}')}")
        if str(first.get("path") or "/") != "/":
            c.to_choose.append(f"the registry sends only {first['path']} at the app's address to "
                               f"service {first['serviceName']}; Catena routes the whole address there")
    for d in domains[1:]:
        path = f", path {d['path']}" if str(d.get("path") or "/") != "/" else ""
        c.to_choose.append(
            f"another address: service {d['serviceName']}, port {d.get('port')}, host "
            f"{t.shown(d.get('host')) or 'a random host'}{path}. It gets no route: catena-admin "
            f"routes several addresses of one app (vps.route.main), and no curated entry uses that "
            f"yet")
    for mount in config.get("mounts") or []:
        if isinstance(mount, dict) and mount.get("filePath"):
            size = len(str(mount.get("content") or "").splitlines())
            c.to_choose.append(
                f"config file {mount['filePath']}: the registry writes it beside the stack (its "
                f"template.toml holds the {size} line{'' if size == 1 else 's'}), and a stack "
                f"deploy carries no file, so the service that mounts it from ../files writes it "
                f"in its entrypoint")

    settings, inline, notes = t.settings()
    c.settings = settings
    c.to_choose += notes
    c.convert = functools.partial(_convert, list(t.env), inline)


class _Template:
    """One template.toml's values, resolved into parts: text, and the atoms
    the registry generates or the host supplies."""

    def __init__(self, slug: str, variables: dict[str, str], env: dict[str, str],
                 domains: list[dict[str, Any]]):
        self.slug = slug
        self.variables = variables
        self.env = env
        self.main = Atom("domain", "fact", f"{slug}.{pipeline.ZONE}")
        first = set(_REF.findall(str(domains[0].get("host", "")))) if domains else set()
        self.further = {name for d in domains[1:] for name in _REF.findall(str(d.get("host", "")))
                        if variables.get(name) == "${domain}"} - first
        self.memo: dict[str, list[str | Atom]] = {}

    def parts(self, text: str, scope: str, seen: frozenset[str] = frozenset()) -> list[str | Atom]:
        out: list[str | Atom] = []
        last = 0
        for index, m in enumerate(_REF.finditer(text)):
            out.append(text[last:m.start()])
            out += self.reference(m.group(1), f"{scope}#{index}", seen)
            last = m.end()
        out.append(text[last:])
        return [p for p in out if p != ""]

    def reference(self, name: str, key: str, seen: frozenset[str]) -> list[str | Atom]:
        """A helper first, as the registry's processor reads it, then a
        variable, then what the .env file substitutes."""
        found = _helper(name, key, self.main, self.variables)
        if found is not None:
            return found
        if name in self.further:
            return [Atom(name, "fact", f"{self.slug}-{slugify(name).removesuffix('-domain')}."
                                       f"{pipeline.ZONE}")]
        if name in self.variables:
            return self.resolved(f"var:{name}", self.variables[name], name, seen)
        m = _ENV_REF.match(name)
        if m and m.group(1) in self.env:
            return self.resolved(f"env:{m.group(1)}", self.env[m.group(1)], m.group(1), seen)
        if m and m.group(2) is not None:
            return [m.group(2)] if m.group(2) else []
        return [Atom(key, "choose", note=f"it reads ${{{name}}}, which the registry defines nowhere")]

    def resolved(self, ident: str, text: str, scope: str, seen: frozenset[str]) -> list[str | Atom]:
        """A variable's or a line's parts, the same atoms for every reader."""
        if ident in seen:
            return []
        if ident not in self.memo:
            self.memo[ident] = self.parts(text, scope, seen | {ident})
        return self.memo[ident]

    def shown(self, host: Any) -> str:
        """A host as a person reads it."""
        parts = self.parts(str(host or ""), "host")
        return "".join(p.value if isinstance(p, Atom) else p for p in parts).replace(
            pipeline.ZONE, "<domain>")

    def settings(self) -> tuple[list[tuple[str, str]], dict[str, str], list[str]]:
        """The settings, the lines written into the compose file instead
        (KEY to compose text), and what a person still chooses.

        A line is its own setting while its value carries one minted value
        at most and reads no atom another line reads too, unless the line is
        that atom alone, which makes it the atom's setting. Every other line
        is written into the compose file, reading each atom from its
        setting."""
        lines = {key: self.resolved(f"env:{key}", text, key, frozenset())
                 for key, text in self.env.items()}
        readers: dict[str, list[str]] = {}
        for key, parts in lines.items():
            for atom_key in dict.fromkeys(p.key for p in parts if isinstance(p, Atom)):
                readers.setdefault(atom_key, []).append(key)
        holders = {self.main.key: "DOMAIN_HOST"}
        for key, parts in lines.items():
            if len(parts) == 1 and isinstance(parts[0], Atom):
                holders.setdefault(parts[0].key, key)

        def own(key: str) -> bool:
            atoms = [p for p in lines[key] if isinstance(p, Atom)]
            return sum(a.kind == "mint" for a in atoms) <= 1 and all(
                a.kind == "fact" or readers[a.key] == [key] or holders.get(a.key) == key
                for a in atoms)

        settings: list[tuple[str, str]] = []
        extra: list[tuple[str, str]] = []
        inline: dict[str, str] = {}
        where: dict[str, list[str]] = {}
        atoms: dict[str, Atom] = {}

        def holder(atom: Atom) -> str:
            if atom.key not in holders:
                base = re.sub(r"[^A-Za-z0-9_]", "_", atom.key.removesuffix("#0")).upper()
                base = f"_{base}" if base[:1].isdigit() else base
                name, n = base, 2
                while name in self.env or name in holders.values():
                    name, n = f"{base}_{n}", n + 1
                holders[atom.key] = name
                extra.append((name, atom.value))
                where.setdefault(atom.key, []).append(name)
            return holders[atom.key]

        for key, parts in lines.items():
            for p in parts:
                if isinstance(p, Atom):
                    atoms[p.key] = p
            if own(key):
                settings.append((key, "".join(p.value if isinstance(p, Atom) else p for p in parts)))
                for atom_key in dict.fromkeys(p.key for p in parts if isinstance(p, Atom)):
                    where.setdefault(atom_key, []).append(key)
            else:
                inline[key] = "".join(f"${{{holder(p)}}}" if isinstance(p, Atom)
                                      else p.replace("$", "$$") for p in parts)

        notes = [f"{', '.join(where[a.key])}: {a.note}: set it by hand"
                 for a in atoms.values() if a.kind == "choose" and where.get(a.key)]
        emails = [name for a in atoms.values() if a.value == "{{ admin_email }}"
                  for name in where.get(a.key, [])]
        if emails:
            notes.append(f"{', '.join(emails)}: the server's admin email stands in for the address "
                         f"the registry makes up")
        plain_http = [key for key, parts in lines.items()
                      if any(isinstance(p, str) and p.endswith("http://") and isinstance(q, Atom)
                             and q.kind == "fact" for p, q in zip(parts, parts[1:]))]
        if plain_http:
            notes.append(f"{', '.join(plain_http)}: the registry's address starts with http://, "
                         f"and Catena serves the app over https")
        if inline:
            notes.append(f"written into the compose file from the settings they combine, so a "
                         f"generated value two lines read stays one setting: {', '.join(inline)}")
        return settings + extra, inline, notes


def _count(arg: str, default: int) -> int:
    """The length a helper names, read as the registry's processor reads it:
    leading digits, else the default."""
    m = re.match(r"\s*\+?(\d+)", arg)
    return int(m.group(1)) if m and int(m.group(1)) else default


def _helper(name: str, key: str, main: Atom, variables: dict[str, str]) -> list[str | Atom] | None:
    """What a helper becomes, or None for a name that is no helper."""
    helper, colon, arg = name.partition(":")
    if name == "domain":
        return [main]
    if helper == "password":
        return [Atom(key, "mint", lookup(_count(arg, 16), lower=True))]
    if helper == "base64":
        return [_base64(key, _count(arg, 32))]
    if helper == "hash":
        return [Atom(key, "mint", lookup(_count(arg, 8), "hexdigits", lower=True))]
    if helper == "jwt" and re.fullmatch(r"\d{1,3}", arg) and int(arg):
        return [Atom(key, "mint", lookup(2 * int(arg), "hexdigits", lower=True))]
    if name == "username":
        # A letter first, as a database user name may need.
        return [Atom(key, "mint", "u" + lookup(11, lower=True))]
    if name == "email":
        return [Atom(key, "fact", "{{ admin_email }}")]
    if helper in ("timestamps", "timestampms") and colon:
        try:
            moment = datetime.fromisoformat(arg)
        except ValueError:
            moment = None
        if moment is None or moment.tzinfo is None:
            return [Atom(key, "choose", note=f"the registry reads the time {arg!r}, which the "
                                             f"importer cannot")]
        ms = (moment - _EPOCH) // timedelta(milliseconds=1)
        return [str(ms if helper == "timestampms" else (ms + 500) // 1000)]
    if helper == "jwt":
        secret = arg.split(":")[0]
        # The secret is named only when it is a variable: a literal one is a
        # value that stays out of the catalog.
        signer = (f"the variable {secret}" if secret in variables
                  else "a secret it names" if secret else "a random secret")
        return [Atom(key, "choose", note=f"the registry signs a JWT with {signer}, which the host "
                                         f"does not mint")]
    if name in _UNMINTED:
        return [Atom(key, "choose", note=f"the registry generates {_UNMINTED[name]}, which the host "
                                         f"does not mint")]
    return None


def _base64(key: str, size: int) -> Atom:
    """`size` random bytes in base64. Letters and digits are base64 digits,
    four of them three bytes, so 4*(size//3) minted ones make the first
    bytes; the one or two left over are zero bytes ("AA==", "AAA="), and the
    value decodes to the size an app may check."""
    if size < 3:
        return Atom(key, "choose", note=f"the registry generates {size} random bytes in base64, "
                                        f"which the host does not mint")
    return Atom(key, "mint", lookup(4 * (size // 3)) + ("", "AA==", "AAA=")[size % 3])


def _convert(keys: list[str], inline: dict[str, str], doc: dict[str, Any]) -> dict[str, Any]:
    """The registry's compose file as the catalog deploys it: a service that
    reads the .env file reads each env line itself, and a line written into
    the compose file replaces each reference to it."""
    for service in doc["services"].values():
        if isinstance(service, dict):
            _read_env_file(service, keys)
    return _substituted(doc, inline)


def _read_env_file(service: dict[str, Any], keys: list[str]) -> None:
    files = service.get("env_file")
    listed = files if isinstance(files, list) else [files] if files else []
    kept = [f for f in listed if (f.get("path") if isinstance(f, dict) else f) not in (".env", "./.env")]
    if len(kept) == len(listed):
        return
    if kept:
        service["env_file"] = kept
    else:
        del service["env_file"]
    env = service.get("environment")
    if isinstance(env, list):
        named = {str(item).partition("=")[0] for item in env}
        env += [f"{key}=${{{key}}}" for key in keys if key not in named]
        return
    env = dict(env or {})
    for key in keys:
        env.setdefault(key, f"${{{key}}}")
    if env:
        service["environment"] = env


def _substituted(node: Any, inline: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {k: _substituted(v, inline) for k, v in node.items()}
    if isinstance(node, list):
        return [_substituted(v, inline) for v in node]
    if isinstance(node, str) and inline:
        return _COMPOSE_REF.sub(lambda m: _inlined(m, inline), node)
    return node


def _inlined(m: re.Match[str], inline: dict[str, str]) -> str:
    name = m.group(1) or m.group(4)
    if name not in inline:
        return m.group(0)
    # ${KEY:+other} reads other when KEY is set, and a written line always is.
    return m.group(3) if m.group(2) in ("+", ":+") else inline[name]
