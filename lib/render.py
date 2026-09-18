"""Render the hand-edited inputs into every generated artifact.

A blueprint directory mixes the two on purpose, because it is the unit
Portainer clones and a reader opens:

  blueprints/<id>/docker-compose.yml  HAND-EDITED; the type-2 stackfile
  blueprints/<id>/logo.png            HAND-PLACED; optional
  blueprints/<id>/README.md           generated from the source prose
  blueprints/<id>/logo.svg            generated placeholder, absent a logo.png
  blueprints/<id>/quiesce.yml         generated when the entry declares hooks

At the repo root, everything is generated:

  templates.json                      Portainer App Templates v3 index
  catalog.json                        the machine view every Catena consumer reads
  index.html                          static catalog preview

Two consumers, two artifacts, on purpose. Portainer reads
`templates.json` and can only carry what its format defines; catenahq/ops
(installer prompts, the bench scheduler) reads `catalog.json`, which
carries the SSO mode, quiesce hooks, bench membership, sizing, and the
bilingual prose alongside the same ids. Neither file is hand-edited.

Idempotency is the contract: rendering twice produces byte-identical
output, and CI fails the PR when the committed artifacts drift.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .model import BLUEPRINTS, ROOT, Entry, load_meta, load_sources, validate_output
from .readme import render_readme
TEMPLATES_JSON = ROOT / "templates.json"
CATALOG_JSON = ROOT / "catalog.json"
INDEX_HTML = ROOT / "index.html"

# The public raw-URL contract. templates.json embeds these and a client's
# Portainer fetches them over plain HTTP, so they must track the real
# repo name.
PORTAINER_TEMPLATES_VERSION = "3"
REPO_GIT_URL = "https://github.com/catenahq/catena-templates"
RAW_BASE = "https://raw.githubusercontent.com/catenahq/catena-templates/main"
MAINTAINER = "https://github.com/catenahq/catena-templates"

# Portainer App Templates have no per-deploy secret generator, so every
# secret and every operator-managed key renders to this sentinel and the
# converge injects the real value post-deploy.
SENTINEL_MANAGED = "__CATENA_OPERATOR_WIRED__"

# The trailing `(?:\|[^}]*)?` matters: several entries pipe the lookup
# through a filter (`... | lower`). Without it the expression failed to
# match and the raw Ansible lookup was published verbatim as the env
# default in templates.json -- a deploy from the Portainer catalog then
# set the app's secret to the literal string "{{ lookup(...) }}".
LOOKUP_PASSWORD_RE = re.compile(
    r"\{\{\s*lookup\(\s*['\"]password['\"]\s*,\s*['\"]/dev/null\s+length=(\d+)[^'\"]*['\"]\s*\)"
    r"(?:\s*\|[^}]*?)?\s*\}\}"
)
JINJA_VAULT_RE = re.compile(r"\{\{\s*vault_[a-zA-Z0-9_]+\s*\}\}")
JINJA_DOMAIN_HOSTS = {"{{ cloudflare_zone }}": ""}

# Human labels for the env keys that recur across the catalog. Anything
# unlisted is humanized from the key itself -- a label is a UI nicety,
# and inventing per-template prose for it would be data nobody verified.
ENV_LABELS = {
    "DOMAIN_HOST": "Domain",
    "DB_PASSWORD": "Database password",
    "DB_ROOT_PASSWORD": "Database root password",
    "SECRET_KEY_BASE": "Application secret",
    "SSO_SECRET": "Session signing secret",
}

# Tokens that read wrong in sentence case. Applied after humanizing an
# unlisted key, so DB_ROOT_HOST becomes "DB root host", not "Db root host".
ENV_LABEL_ACRONYMS = frozenset({
    "api", "cpu", "db", "dns", "ftp", "hpb", "http", "https", "id", "imap",
    "ip", "jwt", "ldap", "oidc", "ram", "s3", "saml", "smtp", "sso", "ssl",
    "tls", "turn", "url", "uri", "uid", "gid", "vapid",
})

# What a reader of the PUBLIC catalog is looking at. Nothing resolves the
# sentinel in this file: a client's Portainer reads its App Templates from its
# own host (catena-admin serves the catalog with every value substituted for
# that host), and this copy exists to be that render's input.
#
# The description must not promise that the value is "set on the first
# converge after deploy". Nothing does that, and the promise tells anyone
# deploying from this file directly to wait for a correction that never
# arrives.
MANAGED_ENV_DESCRIPTION = (
    "Placeholder. The copy of this catalog served by a Catena host carries "
    "the real value; deploying this file directly does not."
)


def strip_jinja_for_portainer(value: str) -> str:
    """Turn a raw env_defaults value into a Portainer env default.

    Vault refs and password lookups both collapse to the sentinel: the
    converge owns them. Domain Jinja we cannot resolve at render time
    becomes empty, so the field shows as blank rather than as a template
    expression a client would have to interpret."""
    out = LOOKUP_PASSWORD_RE.sub(SENTINEL_MANAGED, value)
    out = JINJA_VAULT_RE.sub(SENTINEL_MANAGED, out)
    for needle, replacement in JINJA_DOMAIN_HOSTS.items():
        out = out.replace(needle, replacement)
    return out


def env_label(key: str) -> str:
    if key in ENV_LABELS:
        return ENV_LABELS[key]
    words = [w.lower() for w in key.split("_") if w]
    out = [
        w.upper() if w in ENV_LABEL_ACRONYMS else w
        for w in words
    ]
    if out and out[0] not in ENV_LABEL_ACRONYMS:
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def render_env(entry: Entry) -> list[dict[str, str]]:
    managed = set(entry.catena.get("env_managed_keys", []) or [])
    env: list[dict[str, str]] = []
    for kv in entry.catena["env_defaults"]:
        key, raw_value = kv.split("=", 1)
        key = key.strip()
        field: dict[str, str] = {"name": key, "label": env_label(key)}
        default = SENTINEL_MANAGED if key in managed else strip_jinja_for_portainer(raw_value)
        # Anything still carrying Jinja is by definition something only
        # the converge can resolve -- Portainer has no template engine,
        # so shipping the expression would set the app's real value to
        # the literal string. Treat it as managed.
        if "{{" in default:
            default = SENTINEL_MANAGED
        field["default"] = default
        if default == SENTINEL_MANAGED:
            field["description"] = MANAGED_ENV_DESCRIPTION
        env.append(field)
    return env


def render_note(entry: Entry) -> str:
    """The note is Portainer's detail panel. It gets what the gallery
    description has no room for: the one-line pitch, what it replaces,
    and the docs link."""
    en = entry.prose("en")
    parts = [f"<p>{html.escape(en['what_it_is'])}</p>"]
    replaces = en.get("replaces") or []
    if replaces:
        parts.append(
            "<p>Replaces "
            + html.escape(", ".join(replaces))
            + ".</p>"
        )
    for line in en["compose_description"].splitlines():
        line = line.strip()
        if line.startswith("http"):
            safe = html.escape(line)
            parts.append(f'<p><a href="{safe}">{safe}</a></p>')
            break
    return "".join(parts)


def render_portainer_template(entry: Entry, logo_filename: str) -> dict[str, Any]:
    en = entry.prose("en")
    description = en["compose_description"].strip().split("\n")[0]
    return {
        "categories": list(entry.raw["categories"]),
        "description": description,
        "env": render_env(entry),
        "logo": f"{RAW_BASE}/blueprints/{entry.slug}/{logo_filename}",
        "maintainer": MAINTAINER,
        "name": entry.raw["name"],
        "note": render_note(entry),
        "platform": entry.raw["platform"],
        "repository": {
            "url": REPO_GIT_URL,
            "stackfile": f"blueprints/{entry.slug}/docker-compose.yml",
        },
        "title": entry.raw["title"],
        "type": entry.raw["type"],
    }


def render_catalog_entry(entry: Entry) -> dict[str, Any]:
    """The flat machine view. Key names match what ops/ consumers join
    on; the nesting in sources/ exists for the human editing it."""
    cat = entry.catena
    bench = cat["bench"]
    out: dict[str, Any] = {
        "id": entry.slug,
        "app_name": cat["app_name"],
        "upstream_url": cat["upstream_url"],
        "sso_mode": cat["sso_mode"],
        "domain_host": cat["domain"]["host"],
        "domain_service": cat["domain"]["service"],
        "domain_port": cat["domain"]["port"],
        "compose_file": entry.compose_file,
        "env_defaults": list(cat["env_defaults"]),
        "bench_pack": bench["pack"],
        "bench_fixture": bench.get("fixture", "required"),
        "hostname_baked": bench.get("hostname_baked", False),
        "categories": list(entry.raw["categories"]),
        "sizing": dict(cat["sizing"]),
        "en": cat["en"],
        "fr": cat["fr"],
    }
    if cat.get("env_managed_keys"):
        out["env_managed_keys"] = list(cat["env_managed_keys"])
    if cat.get("env_required_when"):
        out["env_required_when"] = dict(cat["env_required_when"])
    if cat.get("postgres_image_override"):
        out["postgres_image_override"] = cat["postgres_image_override"]
    if cat.get("pg_replay_depends_on"):
        out["pg_replay_depends_on"] = list(cat["pg_replay_depends_on"])
    if "pause_during_migrate_cutover" in cat:
        out["pause_during_migrate_cutover"] = cat["pause_during_migrate_cutover"]
    if cat.get("s3_reconcile"):
        out["s3_reconcile"] = cat["s3_reconcile"]
    if cat.get("post_restore_migrate"):
        out["post_restore_migrate"] = cat["post_restore_migrate"]
    quiesce = entry.quiesce
    if quiesce:
        out["quiesce_pre"] = quiesce["pre"]
        out["quiesce_post"] = quiesce["post"]
        out["quiesce_timeout_seconds"] = quiesce["timeout_seconds"]
    return out


# Deterministic placeholder-logo palette, indexed by hash(slug). Picked to
# stay legible on Portainer's light and dark catalog backgrounds.
_LOGO_PALETTE: tuple[tuple[str, str], ...] = (
    ("#0f766e", "#ffffff"),
    ("#1d4ed8", "#ffffff"),
    ("#7c3aed", "#ffffff"),
    ("#be123c", "#ffffff"),
    ("#c2410c", "#ffffff"),
    ("#65a30d", "#ffffff"),
    ("#0284c7", "#ffffff"),
    ("#a16207", "#ffffff"),
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


def resolve_logo(entry: Entry) -> str:
    """Return the logo filename inside the blueprint directory.

    A hand-placed `logo.png` wins and is left alone. Without one the
    render writes a deterministic `logo.svg` placeholder, and drops a
    placeholder left over from before a real logo arrived."""
    out_dir = entry.app_dir
    placeholder = out_dir / "logo.svg"
    if (out_dir / "logo.png").exists():
        placeholder.unlink(missing_ok=True)
        return "logo.png"
    label = entry.prose("en")["display_name"]
    placeholder.write_text(placeholder_logo_svg(entry.slug, label))
    return "logo.svg"


def render_quiesce_yaml(entry: Entry) -> str:
    """Hand-written rather than yaml.dump so the bytes are stable across
    PyYAML versions -- this file is drift-gated."""
    quiesce = entry.quiesce
    assert quiesce is not None
    lines = [
        "# Generated by catenahq/catena-templates lib/render.py. Do not edit.",
        f"id: {entry.slug}",
        f"timeout_seconds: {quiesce['timeout_seconds']}",
        "pre: |",
    ]
    lines.extend(f"  {line}" for line in str(quiesce["pre"]).splitlines() or [""])
    lines.append("post: |")
    lines.extend(f"  {line}" for line in str(quiesce["post"]).splitlines() or [""])
    return "\n".join(lines) + "\n"


def render_index_html(entries: list[Entry], meta: dict[str, Any]) -> str:
    """A static preview of the catalog. No build step, no external
    assets: it is opened straight from the repo or served by the
    Dockerfile alongside templates.json."""
    rows = []
    for entry in entries:
        en = entry.prose("en")
        cats = ", ".join(entry.raw["categories"])
        rows.append(
            "      <tr>"
            f"<td><code>{html.escape(entry.slug)}</code></td>"
            f"<td>{html.escape(en['display_name'])}</td>"
            f"<td>{html.escape(en['what_it_is'])}</td>"
            f"<td>{html.escape(cats)}</td>"
            f"<td>{html.escape(entry.catena['sso_mode'])}</td>"
            f"<td><a href=\"{html.escape(entry.catena['upstream_url'])}\">upstream</a></td>"
            "</tr>"
        )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Catena template catalog</title>\n"
        "  <style>\n"
        "    :root { color-scheme: light dark; }\n"
        "    body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 70rem; padding: 0 1rem; }\n"
        "    table { border-collapse: collapse; width: 100%; }\n"
        "    th, td { border-bottom: 1px solid #8883; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }\n"
        "    code { font-size: 0.9em; }\n"
        "    caption { text-align: left; padding-bottom: 0.8rem; color: #8889; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Catena template catalog</h1>\n"
        f"  <p>{len(entries)} templates. Generated from <code>sources/</code>; do not edit.</p>\n"
        "  <p>Portainer App Templates URL: "
        f'<code>{RAW_BASE}/templates.json</code></p>\n'
        "  <table>\n"
        f"    <caption>Sizing measured on {html.escape(meta['sizing']['measurement_host'])}, "
        f"last {html.escape(meta['sizing']['last_measured_at'])}.</caption>\n"
        "    <thead><tr><th>id</th><th>Name</th><th>What it is</th>"
        "<th>Categories</th><th>SSO</th><th>Upstream</th></tr></thead>\n"
        "    <tbody>\n"
        + "\n".join(rows)
        + "\n    </tbody>\n"
        "  </table>\n"
        "</body>\n"
        "</html>\n"
    )


def _prune_orphan_blueprints(entries: list[Entry]) -> list[str]:
    """Drop blueprint directories no source file claims any more.

    The render cannot simply wipe `blueprints/` and rebuild it: the
    compose in each directory is hand-edited, and so is any logo.png
    beside it. Deleting a whole directory is therefore the one
    destructive act here, and it happens only when the template's source
    file is gone."""
    if not BLUEPRINTS.is_dir():
        return []
    live = {entry.slug for entry in entries}
    dropped = []
    for path in sorted(BLUEPRINTS.iterdir()):
        if path.is_dir() and path.name not in live:
            shutil.rmtree(path)
            dropped.append(path.name)
    return dropped


def render_all() -> int:
    entries = load_sources()
    meta = load_meta()

    BLUEPRINTS.mkdir(parents=True, exist_ok=True)
    dropped = _prune_orphan_blueprints(entries)

    templates: list[dict[str, Any]] = []
    catalog_entries: list[dict[str, Any]] = []
    for entry in entries:
        out_dir = entry.app_dir
        (out_dir / "README.md").write_text(render_readme(entry))
        quiesce_path = out_dir / "quiesce.yml"
        if entry.quiesce:
            quiesce_path.write_text(render_quiesce_yaml(entry))
        else:
            quiesce_path.unlink(missing_ok=True)
        logo_filename = resolve_logo(entry)
        templates.append(render_portainer_template(entry, logo_filename))
        catalog_entries.append(render_catalog_entry(entry))

    doc = {"version": PORTAINER_TEMPLATES_VERSION, "templates": templates}
    schema_errors = validate_output(doc)
    if schema_errors:
        print("rendered templates.json violates Schema.json:")
        for err in schema_errors:
            print(f"  {err}")
        return 1

    TEMPLATES_JSON.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    CATALOG_JSON.write_text(
        json.dumps(
            {
                "version": 1,
                "postgres_default_image": meta["postgres_default_image"],
                "sizing": meta["sizing"],
                "templates": catalog_entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    INDEX_HTML.write_text(render_index_html(entries, meta))

    print(
        f"rendered {len(entries)} templates: a README, a logo and any "
        f"quiesce hooks per blueprint, plus templates.json + catalog.json "
        f"+ index.html"
    )
    if dropped:
        print(f"dropped {len(dropped)} blueprint(s) with no source file: "
              f"{', '.join(dropped)}")
    return 0
