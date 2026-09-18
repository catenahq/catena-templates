"""Load + validate the hand-edited inputs.

A template is two hand-edited files: its metadata at
`sources/<id>.json`, and its compose at `blueprints/<id>/docker-compose.yml`.
`sources/_meta.json` holds the values that are catalog-wide rather than
per-template. Everything else in the repo is rendered from these.

The compose sits in the blueprint directory because that directory is
what Portainer clones and deploys: one file, in the place that serves
it, with the app's README and logo beside it.

Validation runs in two layers:

  1. JSON Schema (`sources.schema.json`) -- shape, enums, required keys.
     Catches the mechanical mistakes at the field level.
  2. Cross-file invariants below -- id/filename agreement, compose file
     present, duplicate slugs, unknown env_managed_keys. A schema cannot
     see across files, and these are exactly the failures that reach a
     client's Portainer as a broken template when nothing checks them.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ModuleNotFoundError:  # see _validate_schema
    jsonschema = None

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"
BLUEPRINTS = ROOT / "blueprints"
COMPOSE_NAME = "docker-compose.yml"
META_NAME = "_meta.json"
SOURCE_SCHEMA = ROOT / "sources.schema.json"
OUTPUT_SCHEMA = ROOT / "Schema.json"


class SourceError(Exception):
    """Raised with every collected problem, not just the first. A build
    that fails one field at a time costs a round trip per typo."""


@dataclass(frozen=True)
class Entry:
    """One template, normalized. `raw` is the source document as written;
    the properties below are the accessors render/lint paths use so a
    future key rename lands here instead of in five call sites."""

    raw: dict[str, Any]

    @property
    def slug(self) -> str:
        return self.raw["id"]

    @property
    def catena(self) -> dict[str, Any]:
        return self.raw["x-catena"]

    @property
    def app_dir(self) -> Path:
        """The directory Portainer clones: the compose, the README, the
        logo, and the quiesce hooks for this one template."""
        return BLUEPRINTS / self.slug

    @property
    def compose_path(self) -> Path:
        return self.app_dir / COMPOSE_NAME

    @property
    def compose_file(self) -> str:
        """Repo-relative path to the compose, as catalog.json publishes
        it. Derived from the id, so it cannot disagree with where the
        file is."""
        return f"{BLUEPRINTS.name}/{self.slug}/{COMPOSE_NAME}"

    @property
    def quiesce(self) -> dict[str, Any] | None:
        return self.catena.get("quiesce")

    @property
    def post_restore_migrate(self) -> dict[str, Any] | None:
        return self.catena.get("post_restore_migrate")

    @property
    def sizing(self) -> dict[str, Any]:
        return self.catena["sizing"]

    def prose(self, lang: str) -> dict[str, Any]:
        return self.catena[lang]


def load_meta() -> dict[str, Any]:
    """Catalog-wide values: the deployment order and the central Postgres
    pin. Resolved against SOURCES at call time so a test can point the
    whole loader at a fixture tree."""
    return json.loads((SOURCES / META_NAME).read_text())


def _schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _validate_schema(doc: dict[str, Any], schema: dict[str, Any], label: str) -> list[str]:
    """Layer 1, and NOT REQUIRED to render.

    Renovate's container has a python3 and no pip, so a bump that re-renders
    through build/render-ci.sh cannot install jsonschema -- and the render
    aborting on the import left every catalog bump's generated artifacts
    describing the old pin, which is the render-idempotency red this skip
    exists to stop. Rendering needs the sources to BE valid, not to be proven
    valid here.

    Nothing is lost: build/validate.py imports jsonschema directly and cannot
    run degraded, and the render-idempotency job re-renders under uv and
    compares, so a Renovate render that skipped layer 1 is still checked by one
    that did not.

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


def load_sources() -> list[Entry]:
    """Return every template in catalog order, or raise SourceError with
    the full problem list.

    Order comes from `_meta.json.order`, not from the filesystem. It is a
    curated deployment sequence (hubs first, integrations next,
    independents last) that the Portainer gallery and the generated docs
    index both present to clients; sorting by filename would silently
    replace it with alphabetical."""
    if not SOURCES.is_dir():
        raise SourceError(f"missing sources directory: {SOURCES}")

    if jsonschema is None:
        print(
            "sources: jsonschema is not installed, so the JSON Schema layer is "
            "skipped. Field shapes and enums are NOT checked; the cross-file "
            "invariants are.",
            file=sys.stderr,
        )
    schema = _schema(SOURCE_SCHEMA)
    errors: list[str] = []
    entries: list[Entry] = []
    seen: dict[str, Path] = {}

    for path in sorted(SOURCES.glob("*.json")):
        if path.name.startswith("_"):
            continue
        label = f"sources/{path.name}"
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: invalid JSON: {exc}")
            continue

        schema_errors = _validate_schema(doc, schema, label)
        errors.extend(schema_errors)
        if schema_errors:
            # Cross-file checks below index into keys the schema just
            # reported as missing; running them anyway turns one real
            # error into a cascade of KeyErrors.
            continue

        slug = doc["id"]
        if slug != path.stem:
            errors.append(f"{label}: id {slug!r} does not match the filename stem")
        if slug in seen:
            errors.append(f"{label}: duplicate id {slug!r} (also in {seen[slug].name})")
        seen[slug] = path

        entry = Entry(doc)
        if not entry.compose_path.exists():
            errors.append(
                f"{label}: {entry.compose_file} does not exist -- every "
                f"template needs its compose in its own blueprint directory"
            )

        declared = {kv.split("=", 1)[0] for kv in entry.catena["env_defaults"]}
        for key in entry.catena.get("env_managed_keys", []):
            if key not in declared:
                errors.append(
                    f"{label}: env_managed_keys names {key!r}, which is not "
                    f"in env_defaults -- it would render nowhere"
                )
        entries.append(entry)

    if not entries and not errors:
        errors.append(f"{SOURCES} contains no template files")

    order = load_meta().get("order") or []
    ranked = {slug: idx for idx, slug in enumerate(order)}
    found = {e.slug for e in entries}
    for slug in sorted(found - set(ranked)):
        errors.append(
            f"sources/{slug}.json: not listed in _meta.json order -- add it "
            f"where the template belongs in the deployment sequence"
        )
    for slug in sorted(set(ranked) - found):
        errors.append(f"_meta.json order names {slug!r}, which has no source file")

    if errors:
        raise SourceError("\n".join(f"  {e}" for e in errors))
    return sorted(entries, key=lambda e: ranked[e.slug])


def validate_output(doc: dict[str, Any]) -> list[str]:
    """Validate a rendered templates.json against the published
    Portainer-format schema. The generated artifact is what a client's
    Portainer actually fetches, so it gets checked as strictly as the
    input."""
    if jsonschema is None:
        print(
            "templates.json: jsonschema is not installed, so the rendered "
            "artifact is NOT checked against Schema.json here.",
            file=sys.stderr,
        )
    return _validate_schema(doc, _schema(OUTPUT_SCHEMA), "templates.json")
