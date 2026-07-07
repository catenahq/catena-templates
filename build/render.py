#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Render source/ -> blueprints/ + templates.json (Portainer App Templates v3).

Inputs:
  source/catalog.yml             - per-template metadata + env_defaults
  source/compose/<basename>      - compose file referenced by each entry
  source/assets/<id>/logo.png    - optional, copied verbatim

Outputs (overwritten on each run; idempotent):
  blueprints/<id>/docker-compose.yml   - the Portainer type-3 stackfile
  blueprints/<id>/logo.svg|png         - placeholder or source asset
  blueprints/<id>/quiesce.yml          - if the entry declares quiesce hooks
  templates.json                       - Portainer App Templates index (v3)

The transformation (source catalog -> Portainer App Templates):

- The compose file is copied verbatim into blueprints/ as the type-3
  stackfile. Portainer clones this repo and deploys
  blueprints/<id>/docker-compose.yml; ops/ converge reconciles env +
  routing post-deploy (catena is operator-managed).

- templates.json holds one Portainer App Template per catalog entry:
    - type 3 (compose stack deployed FROM this git repo),
    - title/description/note/categories/logo from catalog metadata,
    - repository{url, stackfile} pointing at blueprints/<id>/,
    - env [{name,label,default}] from env_defaults. Portainer has no
      per-deploy secret generator, so secret + managed keys default to
      the sentinel (__CATENA_OPERATOR_WIRED__) that ops/ re-injects.

Idempotency: render must produce byte-identical output across runs.
CI verifies via `git diff --exit-code blueprints templates.json`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source"
BLUEPRINTS = ROOT / "blueprints"
TEMPLATES_JSON = ROOT / "templates.json"

# Portainer App Templates format version + the repo Portainer clones for
# type-3 (compose git-repo) stacks. This is the public raw-URL contract:
# templates.json embeds these URLs and Portainer fetches them over plain
# HTTP, so they must track the real repo name (catenahq/catena-templates).
PORTAINER_TEMPLATES_VERSION = "3"
REPO_GIT_URL = "https://github.com/catenahq/catena-templates"
RAW_BASE = "https://raw.githubusercontent.com/catenahq/catena-templates/main"

# Allowed enum values for the operator-facing bench_pack + bench_fixture
# fields. The catena-ops test bench reads these from the catalog at
# load time to partition templates into restore-drill groups
# (BENCH_DEPLOY_PACK=postgres etc.) and to enforce that every template
# either ships a fixture file or explicitly opts out.
BENCH_PACK_VALUES = frozenset({"postgres", "mariadb", "mongo", "nodb", "embedded"})
BENCH_FIXTURE_VALUES = frozenset({"required", "skip"})

# Sentinel placeholder for env vars that catenahq/ops re-injects on every
# converge (env_managed_keys). Operator never sees these in the Portainer
# UI; ops/ overwrites them post-deploy.
SENTINEL_MANAGED = "__CATENA_OPERATOR_WIRED__"

# Pattern matching Ansible `lookup('password', '/dev/null length=N chars=...')`.
# Collapses to the operator-wired sentinel (Portainer App Templates have no
# per-deploy secret generator; ops/ converge injects the real value).
LOOKUP_PASSWORD_RE = re.compile(
    r"\{\{\s*lookup\(\s*['\"]password['\"]\s*,\s*['\"]/dev/null\s+length=(\d+)[^'\"]*['\"]\s*\)\s*\}\}"
)

# Jinja vault refs collapse to the sentinel; ops/ converge resolves them.
JINJA_VAULT_RE = re.compile(r"\{\{\s*vault_[a-zA-Z0-9_]+\s*\}\}")

# Generic Jinja that we cannot resolve at render time: hostnames, defaults,
# concatenations. The known cases collapse to empty so the operator (or the
# ops route-writer) fills the host post-deploy; anything unrecognized is left
# as-is so a downstream gap is visible.
JINJA_DOMAIN_HOSTS = {
    "{{ cloudflare_zone }}": "${domain}",
}


def strip_jinja_for_portainer(value: str) -> str:
    """Transform a raw env_defaults value into a Portainer App Template env
    default. Portainer App Templates have NO per-deploy secret generator
    -- catena is operator-managed, so ops/ converge re-injects
    every secret post-deploy (env_managed_keys). So both vault refs AND
    password lookups collapse to the sentinel here; ops/ owns them. Domain
    jinja we cannot resolve at render time becomes empty (the operator fills
    the host, or the ops route-writer sets it)."""
    out = value
    out = LOOKUP_PASSWORD_RE.sub(SENTINEL_MANAGED, out)
    out = JINJA_VAULT_RE.sub(SENTINEL_MANAGED, out)
    for needle in JINJA_DOMAIN_HOSTS:
        out = out.replace(needle, "")
    return out


def compose_basename(compose_file_jinja: str) -> str:
    """Extract the actual filename from a catalog compose_file Jinja path."""
    return compose_file_jinja.rstrip().split("/")[-1]


def render_portainer_template(entry: dict[str, Any], logo_filename: str) -> dict[str, Any]:
    """One Portainer App Template (templates.json v3) entry: a type-3
    compose-stack deployed FROM this repo's blueprint (Portainer clones the
    git repo + uses the stackfile path). env is the operator-facing form."""
    slug = entry["id"]
    en = entry.get("en", {}) or {}
    title = en.get("display_name", entry.get("app_name", slug))
    description = (en.get("compose_description", "") or "").strip().split("\n")[0]
    sso = entry.get("sso_mode", "")

    env: list[dict[str, str]] = []
    managed = set(entry.get("env_managed_keys", []) or [])
    for kv in entry.get("env_defaults", []) or []:
        if "=" not in kv:
            continue
        key, raw_value = kv.split("=", 1)
        key = key.strip()
        default = SENTINEL_MANAGED if key in managed else strip_jinja_for_portainer(raw_value)
        env.append({"name": key, "label": key, "default": default})

    tmpl: dict[str, Any] = {
        "type": 3,
        "title": title,
        "description": description,
        "note": f"<p>{description}</p>",
        "categories": [sso] if sso else [],
        "platform": "linux",
        "logo": f"{RAW_BASE}/blueprints/{slug}/{logo_filename}",
        "repository": {
            "url": REPO_GIT_URL,
            "stackfile": f"blueprints/{slug}/docker-compose.yml",
        },
    }
    if env:
        tmpl["env"] = env
    return tmpl


# Tailwind-ish palette for deterministic placeholder logos. Indexed by
# hash(slug) % len. Each entry: (background, foreground). Picked to be
# legible on Portainer's light + dark catalog backgrounds.
_LOGO_PALETTE: tuple[tuple[str, str], ...] = (
    ("#0f766e", "#ffffff"),  # teal
    ("#1d4ed8", "#ffffff"),  # blue
    ("#7c3aed", "#ffffff"),  # violet
    ("#be123c", "#ffffff"),  # rose
    ("#c2410c", "#ffffff"),  # orange
    ("#65a30d", "#ffffff"),  # lime
    ("#0284c7", "#ffffff"),  # sky
    ("#a16207", "#ffffff"),  # amber
)


def placeholder_logo_svg(slug: str, label: str) -> str:
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    bg, fg = _LOGO_PALETTE[digest[0] % len(_LOGO_PALETTE)]
    glyph = (label or slug or "?")[0].upper()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
        f'<rect width="256" height="256" rx="32" fill="{bg}"/>'
        f'<text x="128" y="128" text-anchor="middle" dominant-baseline="central" '
        f'font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif" '
        f'font-size="148" font-weight="600" fill="{fg}">{glyph}</text>'
        '</svg>\n'
    )


def resolve_logo(entry: dict[str, Any], out_dir: Path) -> str:
    """Place the logo file for `entry` under `out_dir`. Returns the
    filename to record in meta.json's `logo` field.

    Source precedence: `source/assets/<id>/logo.png` -> copied as-is;
    `.svg` -> copied as-is; nothing -> deterministic placeholder SVG."""
    slug = entry["id"]
    asset_dir = SOURCE / "assets" / slug
    for name in ("logo.png", "logo.svg"):
        src = asset_dir / name
        if src.exists():
            shutil.copyfile(src, out_dir / name)
            return name
    label = (entry.get("en") or {}).get("display_name") or entry.get("app_name") or slug
    (out_dir / "logo.svg").write_text(placeholder_logo_svg(slug, label))
    return "logo.svg"


def validate_bench_fields(entries: list[dict[str, Any]]) -> list[str]:
    """Validate every catalog entry's bench-facing fields.

    Returns a list of human-readable error strings; empty list means
    the catalog is valid.

    - bench_pack (required): pack membership, declared per-entry so
      it shows up in the catalog diff.
    - bench_fixture (optional, default "required"): whether the bench
      requires a fixture file.
    - hostname_baked (optional, default false, bool): whether the
      template persists the install-time FQDN into immutable state.
      The parallel scheduler pins any pack containing a baked
      template to slot 0.

    Peak-RAM data lives in source/sizing-data.yml and is validated
    separately by validate_sizing_data() so this function stays purely
    catalog-shape.
    """
    errors: list[str] = []
    for entry in entries:
        slug = entry.get("id", "<unknown>")
        pack = entry.get("bench_pack")
        if pack is None:
            errors.append(
                f"{slug}: missing required field bench_pack "
                f"(one of {sorted(BENCH_PACK_VALUES)})"
            )
        elif pack not in BENCH_PACK_VALUES:
            errors.append(
                f"{slug}: bench_pack={pack!r} not in "
                f"{sorted(BENCH_PACK_VALUES)}"
            )
        fixture = entry.get("bench_fixture", "required")
        if fixture not in BENCH_FIXTURE_VALUES:
            errors.append(
                f"{slug}: bench_fixture={fixture!r} not in "
                f"{sorted(BENCH_FIXTURE_VALUES)}"
            )
        hostname_baked = entry.get("hostname_baked", False)
        if not isinstance(hostname_baked, bool):
            errors.append(
                f"{slug}: hostname_baked={hostname_baked!r} must be bool"
            )
    return errors


def validate_sizing_data(
    entries: list[dict[str, Any]], sizing_services: list[dict[str, Any]]
) -> list[str]:
    """Enforce the catalog <-> sizing-data parity contract.

    Every catalog id must have a sizing-data entry with a positive int
    peak_ram_mb (the catena-ops parallel scheduler reads it + applies a
    fixed 1.15x safety margin to gate slot acquisition). Sizing-data
    entries that point at an id not in the catalog are flagged too --
    they're either a typo or stale.
    """
    errors: list[str] = []
    cat_ids = {entry.get("id") for entry in entries}
    siz_by_id: dict[str, dict[str, Any]] = {}
    for svc in sizing_services:
        sid = svc.get("id")
        if sid is None:
            errors.append("sizing-data: entry missing required field id")
            continue
        if sid in siz_by_id:
            errors.append(f"sizing-data: duplicate id={sid!r}")
            continue
        siz_by_id[sid] = svc

    for slug in cat_ids:
        if slug is None:
            continue
        svc = siz_by_id.get(slug)
        if svc is None:
            errors.append(
                f"{slug}: missing entry in source/sizing-data.yml "
                f"(every catalog id must declare peak_ram_mb there)"
            )
            continue
        peak = svc.get("peak_ram_mb")
        if peak is None:
            errors.append(
                f"{slug}: sizing-data peak_ram_mb is required "
                f"(catena-ops bench scheduler reads it)"
            )
        elif not isinstance(peak, int) or isinstance(peak, bool) or peak <= 0:
            errors.append(
                f"{slug}: sizing-data peak_ram_mb={peak!r} "
                f"must be a positive int"
            )

    orphans = sorted(set(siz_by_id) - cat_ids)
    for slug in orphans:
        errors.append(
            f"{slug}: sizing-data entry has no matching catalog id"
        )
    return errors


# Cap on per-template quiesce timeout. The catena-daily chain holds
# /run/catena.lock while quiesce hooks run; a runaway hook would block
# every other catena-* driver indefinitely. 60s lets even a busy
# MongoDB fsyncLock complete on a multi-GB write workload, while
# capping the worst-case mutex hold within the chain's 6h timeout
# (well below it: 60s + backup + verify + ... = the chain has slack).
QUIESCE_MAX_TIMEOUT_S = 60


def validate_quiesce_fields(entries: list[dict[str, Any]]) -> list[str]:
    """Reject half-pairs and out-of-range timeouts. The stricter
    allowlist + shellcheck gate lives in build/lint_quiesce.py so
    the render path stays fast (no external tools); this validator
    only enforces the schema invariants that drive render output."""
    errors: list[str] = []
    for entry in entries:
        slug = entry.get("id", "<unknown>")
        pre = entry.get("quiesce_pre")
        post = entry.get("quiesce_post")
        if (pre is None) != (post is None):
            errors.append(
                f"{slug}: quiesce_pre / quiesce_post must be paired "
                f"(got pre={bool(pre)} post={bool(post)})"
            )
        if pre is None and post is None:
            continue
        timeout = entry.get("quiesce_timeout_seconds")
        if not isinstance(timeout, int) or timeout < 1:
            errors.append(
                f"{slug}: quiesce_timeout_seconds must be a positive int; "
                f"got {timeout!r}"
            )
        elif timeout > QUIESCE_MAX_TIMEOUT_S:
            errors.append(
                f"{slug}: quiesce_timeout_seconds={timeout} exceeds "
                f"hard cap {QUIESCE_MAX_TIMEOUT_S} (mutex-hold ceiling)"
            )
    return errors


def render_quiesce_yaml(entry: dict[str, Any]) -> str:
    """Emit a deterministic quiesce.yml the ops-side catena-quiesce-
    render consumer reads at converge time. Hand-written (no yaml.dump)
    so the output is byte-stable across PyYAML versions."""
    lines = [
        "# Generated by catenahq/templates/build/render.py. Do not edit.",
        f"id: {entry['id']}",
        f"timeout_seconds: {entry['quiesce_timeout_seconds']}",
        "pre: |",
    ]
    for line in str(entry["quiesce_pre"]).splitlines() or [""]:
        lines.append(f"  {line}")
    lines.append("post: |")
    for line in str(entry["quiesce_post"]).splitlines() or [""]:
        lines.append(f"  {line}")
    return "\n".join(lines) + "\n"


def render_all() -> int:
    if not SOURCE.exists():
        print(f"error: missing {SOURCE}", file=sys.stderr)
        return 1

    catalog_path = SOURCE / "catalog.yml"
    catalog = yaml.safe_load(catalog_path.read_text())
    entries = catalog["catena_template_catalog"]

    sizing_path = SOURCE / "sizing-data.yml"
    sizing = yaml.safe_load(sizing_path.read_text())
    sizing_services = sizing.get("services", []) or []

    field_errors = validate_bench_fields(entries)
    if field_errors:
        print("catalog validation failed:", file=sys.stderr)
        for err in field_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    sizing_errors = validate_sizing_data(entries, sizing_services)
    if sizing_errors:
        print("sizing-data validation failed:", file=sys.stderr)
        for err in sizing_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    quiesce_errors = validate_quiesce_fields(entries)
    if quiesce_errors:
        print("quiesce field validation failed:", file=sys.stderr)
        for err in quiesce_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    if BLUEPRINTS.exists():
        shutil.rmtree(BLUEPRINTS)
    BLUEPRINTS.mkdir(parents=True)

    templates: list[dict[str, Any]] = []
    for entry in entries:
        slug = entry["id"]
        out_dir = BLUEPRINTS / slug
        out_dir.mkdir()

        basename = compose_basename(entry.get("compose_file", "") or "")
        source_compose = SOURCE / "compose" / basename
        if not source_compose.exists():
            print(f"error: {slug}: compose file missing: {source_compose}", file=sys.stderr)
            return 1
        # The blueprint compose IS the Portainer type-3 stackfile (Portainer
        # clones this repo + deploys blueprints/<id>/docker-compose.yml).
        (out_dir / "docker-compose.yml").write_bytes(source_compose.read_bytes())

        if entry.get("quiesce_pre") and entry.get("quiesce_post"):
            (out_dir / "quiesce.yml").write_text(render_quiesce_yaml(entry))

        logo_filename = resolve_logo(entry, out_dir)
        templates.append(render_portainer_template(entry, logo_filename))

    # Portainer App Templates: a single templates.json (v3) at the repo root,
    # replacing the previous per-blueprint template.toml + meta.json.
    doc = {"version": PORTAINER_TEMPLATES_VERSION, "templates": templates}
    TEMPLATES_JSON.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    print(f"rendered {len(entries)} templates into {BLUEPRINTS.relative_to(ROOT)}/"
          f" + {TEMPLATES_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(render_all())
