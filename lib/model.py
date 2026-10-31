"""Load + validate the hand-edited inputs.

A template is two hand-edited files: its metadata and its compose at
`blueprints/<id>/docker-compose.yml`. The metadata is the card file
`blueprints/<id>/app.json` (schema `app.schema.json`) or
`sources/<id>.json` (schema `sources.schema.json`); an app holds one of
them, never both. `catalog-meta.json` (or `sources/_meta.json`) holds the values
that are catalog-wide rather than per-template. Everything else in the repo is
rendered from these.

The compose sits in the blueprint directory because that directory is
what a host fetches at a tag and deploys byte for byte: one file, in the
place that serves it, with the app's README and logo beside it. Its root `x-catena` block
declares what the host needs to back up, update and restore the deployed
app (env, lifecycle, quiesce, object_store, hooks and the rest of
block.schema.json); the loader reads it beside the source.

Validation runs in two layers:

  1. JSON Schema (`app.schema.json` for a card file, `sources.schema.json`
     for a source, `block.schema.json` for the compose block) -- shape, enums,
     required keys. Catches the mechanical mistakes at the field level.
  2. Cross-file invariants below -- id/filename agreement, compose file
     present with a block that declares an env, duplicate slugs, a logo.png
     the control panel keeps beside the source that names it, the main
     service's name label equal to the source's display name (the compose of an
     app held as app.json carries no vps.homepage.* label).
     A schema cannot see across files, and these are exactly the failures
     that reach a host as a broken app when nothing checks them.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    import jsonschema
except ModuleNotFoundError:  # see _validate_schema
    jsonschema = None

# TOOLS_ROOT is the checkout these tools live in and holds the schemas. ROOT is
# the data root: the tree holding sources/, blueprints/ and the generated
# files, which TEMPLATES_ROOT points at another repository's tree.
TOOLS_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("TEMPLATES_ROOT") or TOOLS_ROOT).resolve()
SOURCES = ROOT / "sources"
BLUEPRINTS = ROOT / "blueprints"
COMPOSE_NAME = "docker-compose.yml"
BLOCK_KEY = "x-catena"
META_NAME = "_meta.json"
CARD_NAME = "app.json"
CATALOG_META = "catalog-meta.json"
CATALOG_ALIASES = "catalog-aliases.json"
ALIASES_NAME = "_aliases.json"
SOURCE_SCHEMA = TOOLS_ROOT / "sources.schema.json"
CARD_SCHEMA = TOOLS_ROOT / "app.schema.json"
BLOCK_SCHEMA = TOOLS_ROOT / "block.schema.json"
OUTPUT_SCHEMA = TOOLS_ROOT / "Schema.json"

# The limits of a hand-placed logo are those of its reader, catena-admin
# payload/engines/applogo: it keeps a PNG of at most LOGO_MAX_BYTES and
# LOGO_MAX_PX a side, redrawn as 8-bit RGBA.
LOGO_NAME = "logo.png"
LOGO_MIN_PX = 128
LOGO_MAX_PX = 512
LOGO_MAX_BYTES = 65536
CARD_MAX_BYTES = 65536  # catena-admin payload/engines/applogo MaxCardBytes
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_RGBA = (8, 6)  # bit depth, colour type
NAME_LABEL = "vps.homepage.name"
HOMEPAGE_PREFIX = "vps.homepage."
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_TRUE = ("true", "yes", "1")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-[^}]*)?\}")


class SourceError(Exception):
    """Raised with every collected problem, not just the first. A build
    that fails one field at a time costs a round trip per typo."""


@dataclass(frozen=True)
class Entry:
    """One template, normalized. `raw` is the source document as written
    (a card entry's is its app.json with the folder name as `id`) and `block`
    the compose's x-catena block (empty when it has none); `domain` is the web
    address a card entry's compose gives (a source holds its own). The
    properties below are the accessors render/lint paths use so a future
    key rename lands here instead of in five call sites."""

    raw: dict[str, Any]
    block: dict[str, Any] = field(default_factory=dict)
    card: bool = False
    domain: dict[str, Any] | None = None

    @property
    def slug(self) -> str:
        return self.raw["id"]

    @property
    def catena(self) -> dict[str, Any]:
        """The catena-specific fields: a source's `x-catena` object, or a card
        file's fields with the web address derived from the compose."""
        if not self.card:
            return self.raw["x-catena"]
        out = {k: v for k, v in self.raw.items() if k not in ("id", "schema")}
        if self.domain:
            out["domain"] = self.domain
        return out

    @property
    def title(self) -> str:
        return self.raw["en"]["display_name"] if self.card else self.raw["title"]

    @property
    def source_path(self) -> str:
        """Repo-relative path of the file holding the metadata."""
        if self.card:
            return f"{BLUEPRINTS.name}/{self.slug}/{CARD_NAME}"
        return f"{SOURCES.name}/{self.slug}.json"

    @property
    def app_dir(self) -> Path:
        """The directory a host fetches: the compose, the README and the
        logo of this one template."""
        return BLUEPRINTS / self.slug

    @property
    def compose_path(self) -> Path:
        return self.app_dir / COMPOSE_NAME

    @property
    def logo_path(self) -> Path:
        return self.app_dir / LOGO_NAME

    @property
    def compose_file(self) -> str:
        """Repo-relative path to the compose, as catalog.json publishes
        it. Derived from the id, so it cannot disagree with where the
        file is."""
        return f"{BLUEPRINTS.name}/{self.slug}/{COMPOSE_NAME}"

    @property
    def quiesce(self) -> dict[str, Any] | None:
        return self.block.get("quiesce")

    @property
    def lifecycle(self) -> dict[str, Any] | None:
        return self.block.get("lifecycle")

    @property
    def object_store(self) -> dict[str, Any] | None:
        return self.block.get("object_store")

    @property
    def env(self) -> dict[str, Any]:
        """The block's env: each key one of setting, generated or managed."""
        return self.block.get("env") or {}

    @property
    def managed_env(self) -> dict[str, str]:
        """The env keys the host keeps set, each with the expression its
        value is rendered from."""
        return {key: spec["managed"]["expr"] for key, spec in self.env.items()
                if "managed" in spec}

    @property
    def env_defaults(self) -> list[str]:
        """KEY=value lines in env order: a setting's default, the lookup
        expression that mints a generated secret, a managed key's
        expression."""
        return [f"{key}={env_value(spec)}" for key, spec in self.env.items()]

    def env_label(self, key: str) -> str | None:
        """The English label the block gives a setting, if it gives one."""
        label = (self.env[key].get("setting") or {}).get("label")
        return label["en"] if label else None

    @property
    def sizing(self) -> dict[str, Any]:
        return self.catena["sizing"]

    @property
    def imported(self) -> bool:
        """Written by the registry importer and not finished by a person:
        published, and marked untested wherever the catalog shows it."""
        return self.catena.get("status") == "imported"

    @property
    def origin(self) -> dict[str, Any] | None:
        return self.catena.get("origin")

    @property
    def requires_catena(self) -> str | None:
        """The oldest Catena release the entry works on, when it needs a newer
        one than some servers run: named in its description and its README.
        The block names it; a source that still carries it is the fallback."""
        return self.block.get("requires_catena") or self.catena.get("requires_catena")

    def prose(self, lang: str) -> dict[str, Any]:
        return self.catena[lang]


GENERATED_CHARS = {"alnum": "ascii_letters,digits", "hex": "hexdigits"}


def env_value(spec: dict[str, Any]) -> str:
    """The catalog's spelling of one env key's value."""
    if "setting" in spec:
        return spec["setting"]["default"]
    if "managed" in spec:
        return spec["managed"]["expr"]
    gen = spec["generated"]
    lower = " | lower" if gen["alphabet"] == "hex" else ""
    return (f"{{{{ lookup('password', '/dev/null length={gen['length']} "
            f"chars={GENERATED_CHARS[gen['alphabet']]}'){lower} }}}}")


def _data_root() -> Path:
    return BLUEPRINTS.parent


def meta_path() -> Path:
    """The catalog-wide values: `catalog-meta.json`, else `sources/_meta.json`.
    Resolved at call time so a test can point the whole loader at a fixture tree."""
    new = _data_root() / CATALOG_META
    return new if new.exists() else SOURCES / META_NAME


def card_layout() -> bool:
    """Whether new entries are written as card files: the catalog holds `catalog-meta.json`."""
    return (_data_root() / CATALOG_META).exists()


def aliases_path() -> Path:
    """The importer's alias records: `catalog-aliases.json` in the card layout,
    else `sources/_aliases.json`."""
    if card_layout() or (_data_root() / CATALOG_ALIASES).exists():
        return _data_root() / CATALOG_ALIASES
    return SOURCES / ALIASES_NAME


def load_meta() -> dict[str, Any]:
    """Catalog-wide values: the deployment order and the sizing header."""
    return json.loads(meta_path().read_text())


def _schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _validate_schema(doc: dict[str, Any], schema: dict[str, Any], label: str) -> list[str]:
    """Layer 1, and NOT REQUIRED to render.

    build/render-ci.sh renders wherever a bump is made, including an
    environment with a python3 and no pip, where jsonschema cannot be
    installed -- and a render aborting on the import leaves a bump's generated
    artifacts describing the old pin, which is the render-idempotency red this
    skip exists to stop. Rendering needs the sources to BE valid, not to be
    proven valid here.

    Nothing is lost: build/validate.py imports jsonschema directly and cannot
    run degraded, and the render-idempotency job re-renders under uv and
    compares, so a render that skipped layer 1 is still checked by one that did
    not.

    The callers announce the skip, once each, rather than this returning an
    empty list per document with nothing said."""
    if jsonschema is None:
        return []
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{label}: {where}: {err.message}")
    return errors


def _read_block(path: Path, schema: dict[str, Any], label: str) -> tuple[dict[str, Any], list[str]]:
    """The compose's x-catena block and the problems with it: an empty block
    for a compose that has none."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, [f"{label}: invalid YAML: {exc}"]
    block = doc.get(BLOCK_KEY) if isinstance(doc, dict) else None
    if block is None:
        return {}, []
    errors = _validate_schema(block, schema, label)
    return (block if not errors else {}), errors


def png_header(data: bytes) -> tuple[int, int, int, int] | None:
    """Width, height, bit depth and colour type of a PNG, read from its IHDR
    chunk; None when `data` does not begin like a PNG. Stdlib only: the
    render runs where no package can be installed."""
    if len(data) < 29 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, colour


def _logo_errors(entry: Entry, label: str) -> list[str]:
    """A logo.png the control panel keeps and a source that names where it
    comes from; one without the other is an error."""
    has_file = entry.logo_path.exists()
    has_field = "logo" in entry.catena
    name = "logo" if entry.card else "x-catena.logo"
    if has_field and not has_file:
        return [f"{label}: {name} is set but {entry.slug}/{LOGO_NAME} does not exist"]
    if has_file and not has_field:
        return [f"{label}: {entry.slug}/{LOGO_NAME} has no {name} naming its source"]
    if not has_file:
        return []
    data = entry.logo_path.read_bytes()
    where = f"{label}: {entry.slug}/{LOGO_NAME}"
    header = png_header(data)
    if header is None:
        return [f"{where} is not a PNG"]
    width, height, depth, colour = header
    errors = []
    if (depth, colour) != _PNG_RGBA:
        errors.append(f"{where} is not 8-bit RGBA (bit depth {depth}, colour type {colour})")
    if width != height:
        errors.append(f"{where} is {width}x{height}, not square")
    if min(width, height) < LOGO_MIN_PX or max(width, height) > LOGO_MAX_PX:
        errors.append(f"{where} is {width}x{height}; each side must be {LOGO_MIN_PX} to {LOGO_MAX_PX} px")
    if len(data) > LOGO_MAX_BYTES:
        errors.append(f"{where} is {len(data)} bytes; the control panel keeps at most {LOGO_MAX_BYTES}")
    return errors


def _service_labels(service: Any) -> dict[str, str]:
    """A service's labels as a map, from the list form (`key=value`) or the
    map form, as the control panel's app checker reads them."""
    raw = service.get("labels") if isinstance(service, dict) else None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    pairs = (str(item).partition("=") for item in raw) if isinstance(raw, list) else ()
    return {key: value for key, _, value in pairs}


def main_service(compose: dict[str, Any]) -> str | None:
    """The service of the main address: the only one carrying vps.route.host, or of
    several the last also carrying vps.route.main as true, 1 or yes (catena-admin
    `payload/engines/stackupdate` reads it that way)."""
    labels = {name: _service_labels(spec) for name, spec in (compose.get("services") or {}).items()}
    hosts = [name for name, found in labels.items() if found.get("vps.route.host", "").strip()]
    marked = [name for name in hosts if labels[name].get("vps.route.main", "").strip().lower() in _TRUE]
    return hosts[0] if len(hosts) == 1 else marked[-1] if marked else None


def derive_domain(compose: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The web address a compose gives: its main service, the service's
    `vps.route.port` and its `vps.route.host`, which names an env key of the
    block (the key's default or managed expression is the host) or holds the
    host itself. None for a compose with no main address."""
    service = main_service(compose)
    if service is None:
        return None, []
    labels = _service_labels(compose["services"][service])
    host = labels["vps.route.host"].strip()
    ref = _ENV_REF.fullmatch(host)
    if ref:
        spec = ((compose.get(BLOCK_KEY) or {}).get("env") or {}).get(ref.group(1))
        if isinstance(spec, dict) and len(spec) == 1:
            host = env_value(spec)
    port = labels.get("vps.route.port", "").strip()
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None, [f"{service} labels vps.route.port={port!r}, not a port number"]
    return {"host": host, "service": service, "port": int(port)}, []


def compose_doc(path: Path) -> dict[str, Any]:
    """The compose as a map; empty when it cannot be read."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _homepage_errors(entry: Entry, label: str) -> list[str]:
    """The compose of an app held as app.json carries no vps.homepage.* label: its card file holds that data."""
    services = compose_doc(entry.compose_path).get("services")
    errors = []
    for name, service in (services.items() if isinstance(services, dict) else ()):
        for key in sorted(_service_labels(service)):
            if key.startswith(HOMEPAGE_PREFIX):
                errors.append(f"{label}: {name} labels {key}; the card data lives in "
                              f"{entry.source_path}, not in the compose")
    return errors


def _name_label_errors(entry: Entry, label: str) -> list[str]:
    """The main address's service carries the name the source gives, or no
    name label at all."""
    domain = entry.catena.get("domain")
    if not domain:
        return []
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8"))
    services = doc.get("services") if isinstance(doc, dict) else None
    service = services.get(domain["service"]) if isinstance(services, dict) else None
    name = _service_labels(service).get(NAME_LABEL)
    expected = entry.prose("en")["display_name"]
    if name is not None and name != expected:
        return [f"{label}: {domain['service']} labels {NAME_LABEL}={name!r}, "
                f"but x-catena.en.display_name is {expected!r}"]
    return []


def load_sources() -> list[Entry]:
    """Return every template in catalog order, or raise SourceError with
    the full problem list.

    Order comes from `_meta.json.order`, not from the filesystem. It is a
    curated deployment sequence (hubs first, integrations next,
    independents last) that the generated catalog files present; sorting
    by filename would silently replace it with alphabetical. An imported entry the order does not list
    comes after the listed ones, alphabetically."""
    sources = [p for p in sorted(SOURCES.glob("*.json")) if not p.name.startswith("_")] \
        if SOURCES.is_dir() else []
    cards = sorted(BLUEPRINTS.glob(f"*/{CARD_NAME}")) if BLUEPRINTS.is_dir() else []
    if not SOURCES.is_dir() and not cards:
        raise SourceError(f"missing sources directory: {SOURCES}")

    if jsonschema is None:
        print(
            "sources: jsonschema is not installed, so the JSON Schema layer is "
            "skipped. Field shapes and enums are NOT checked; the cross-file "
            "invariants are.",
            file=sys.stderr,
        )
    schema = _schema(SOURCE_SCHEMA)
    card_schema = _schema(CARD_SCHEMA)
    block_schema = _schema(BLOCK_SCHEMA)
    errors: list[str] = []
    entries: list[Entry] = []
    seen: dict[str, str] = {}

    for path in [*sources, *cards]:
        is_card = path.name == CARD_NAME
        label = f"{BLUEPRINTS.name}/{path.parent.name}/{CARD_NAME}" if is_card else f"sources/{path.name}"
        if is_card and path.stat().st_size > CARD_MAX_BYTES:
            errors.append(f"{label} is {path.stat().st_size} bytes; the control panel "
                          f"reads at most {CARD_MAX_BYTES}")
            continue
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: invalid JSON: {exc}")
            continue

        schema_errors = _validate_schema(doc, card_schema if is_card else schema, label)
        errors.extend(schema_errors)
        if schema_errors:
            # Cross-file checks below index into keys the schema just
            # reported as missing; running them anyway turns one real
            # error into a cascade of KeyErrors.
            continue

        if is_card:
            slug = path.parent.name
            if not SLUG_RE.fullmatch(slug):
                errors.append(f"{label}: the folder name {slug!r} is not a plain app id")
            doc = {"id": slug, **doc}
        else:
            slug = doc["id"]
            if slug != path.stem:
                errors.append(f"{label}: id {slug!r} does not match the filename stem")
        if slug in seen:
            errors.append(f"{label}: duplicate id {slug!r} (also in {seen[slug]}); "
                          f"an app has one metadata file, never both")
        seen[slug] = label

        entry = Entry(doc, card=is_card)
        if not entry.compose_path.exists():
            errors.append(
                f"{label}: {entry.compose_file} does not exist -- every "
                f"template needs its compose in its own blueprint directory"
            )
        else:
            block, block_errors = _read_block(
                entry.compose_path, block_schema, entry.compose_file)
            errors.extend(block_errors)
            domain = None
            if is_card:
                domain, domain_errors = derive_domain(compose_doc(entry.compose_path))
                errors.extend(f"{label}: {e}" for e in domain_errors)
            entry = Entry(doc, block, is_card, domain)
            if not block_errors:
                errors.extend(_homepage_errors(entry, label) if is_card
                              else _name_label_errors(entry, label))

        errors.extend(_logo_errors(entry, label))
        if entry.compose_path.exists() and not entry.env:
            errors.append(
                f"{label}: {entry.compose_file} declares no x-catena env -- every "
                f"template lists the keys its deploy form shows"
            )
        entries.append(entry)

    if not entries and not errors:
        errors.append(f"{SOURCES} contains no template files")

    order = load_meta().get("order") or []
    ranked = {slug: idx for idx, slug in enumerate(order)}
    found = {e.slug for e in entries}
    # An imported entry follows the listed ones, by id: its pull request
    # touches no shared file, so concurrent import pull requests never conflict.
    unlisted = {e.slug for e in entries if e.imported}
    meta_name = meta_path().name
    where = {e.slug: e.source_path for e in entries}
    for slug in sorted(found - set(ranked) - unlisted):
        errors.append(
            f"{where[slug]}: not listed in {meta_name} order -- add it "
            f"where the template belongs in the deployment sequence"
        )
    for slug in sorted(set(ranked) - found):
        errors.append(f"{meta_name} order names {slug!r}, which has no source file or {CARD_NAME}")

    if errors:
        raise SourceError("\n".join(f"  {e}" for e in errors))
    return sorted(entries, key=lambda e: (ranked.get(e.slug, len(ranked)), e.slug))


def validate_output(doc: dict[str, Any]) -> list[str]:
    """Validate a rendered templates.json against the published
    Portainer-format schema, as strictly as the input."""
    if jsonschema is None:
        print(
            "templates.json: jsonschema is not installed, so the rendered "
            "artifact is NOT checked against Schema.json here.",
            file=sys.stderr,
        )
    return _validate_schema(doc, _schema(OUTPUT_SCHEMA), "templates.json")
