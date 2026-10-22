"""Unit tests for lib/render.py: the transforms that decide what a
client's Portainer shows, and what catenahq/ops reads back.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, readme, render  # noqa: E402


def test_password_lookup_collapses_to_the_sentinel():
    """Portainer has no per-deploy secret generator. A rendered default
    that still carried the Ansible lookup would be shipped verbatim into
    a client's environment tab."""
    out = render.strip_jinja_for_portainer(
        "{{ lookup('password', '/dev/null length=32 chars=ascii_letters,digits') }}"
    )
    assert out == render.SENTINEL_MANAGED


def test_vault_ref_collapses_to_the_sentinel():
    assert render.strip_jinja_for_portainer("{{ vault_smtp_password }}") == render.SENTINEL_MANAGED


def test_unresolvable_domain_jinja_renders_empty():
    assert render.strip_jinja_for_portainer("work.{{ cloudflare_zone }}") == "work."


def test_env_label_humanizes_and_keeps_acronyms():
    assert render.env_label("DB_PASSWORD") == "Database password"
    assert render.env_label("KIMAI_MAIL_URL") == "Kimai mail URL"
    assert render.env_label("OIDC_CLIENT_ID") == "OIDC client ID"
    assert render.env_label("PLANE_SECRET_KEY") == "Plane secret key"


def test_managed_keys_render_as_sentinel_with_a_description():
    entry = model.Entry({
        "id": "x", "type": 3, "title": "X", "name": "x",
        "categories": ["Testing"], "platform": "linux",
        "x-catena": {},
    }, {"schema": 2, "env": {
        "DOMAIN_HOST": {"setting": {"default": "x.example.com",
                                    "label": {"en": "Address", "fr": "Adresse"}}},
        "DB_PASSWORD": {"generated": {"length": 32, "alphabet": "alnum"}},
        "OIDC_CLIENT_SECRET": {"managed": {"expr": "placeholder"}},
    }})
    env = {f["name"]: f for f in render.render_env(entry)}
    assert env["DOMAIN_HOST"]["default"] == "x.example.com"
    assert env["DOMAIN_HOST"]["label"] == "Address"
    assert env["DB_PASSWORD"]["label"] == "Database password"
    assert env["DB_PASSWORD"]["default"] == render.SENTINEL_MANAGED
    assert "description" not in env["DOMAIN_HOST"]
    assert env["OIDC_CLIENT_SECRET"]["default"] == render.SENTINEL_MANAGED
    # The description says where the real value comes from. It must not promise
    # that something resolves this file in place: nothing does, and a reader
    # who deployed it expecting one would get the placeholder as their secret.
    assert "Placeholder" in env["OIDC_CLIENT_SECRET"]["description"]
    assert "converge" not in env["OIDC_CLIENT_SECRET"]["description"]


def test_rendered_templates_json_matches_the_published_schema():
    """The committed artifact is what a client's Portainer fetches over
    plain HTTP. Validate the file on disk, not a freshly built one."""
    doc = json.loads((ROOT / "templates.json").read_text())
    assert model.validate_output(doc) == []
    assert doc["version"] == "3"


def test_every_template_is_a_git_repo_stack_pointing_at_its_blueprint():
    doc = json.loads((ROOT / "templates.json").read_text())
    for tmpl in doc["templates"]:
        assert tmpl["type"] == 2
        stackfile = tmpl["repository"]["stackfile"]
        assert stackfile.startswith("blueprints/")
        assert (ROOT / stackfile).exists(), stackfile


def test_no_ansible_templating_reaches_portainer():
    """templates.json is deployed by a human clicking Deploy, with no
    Ansible in the loop. An unresolved lookup or vault ref there becomes
    the app's literal secret value. This caught a real leak: entries
    piping a lookup through `| lower` did not match the collapse regex.

    catalog.json is the opposite case and deliberately keeps the Jinja:
    the host's render is what resolves it."""
    portainer = (ROOT / "templates.json").read_text()
    assert "lookup('password'" not in portainer
    assert "{{" not in portainer

    catalog = (ROOT / "catalog.json").read_text()
    assert "lookup('password'" in catalog


def test_catalog_json_carries_what_portainer_cannot():
    """The second artifact exists precisely for the fields the Portainer
    format has no slot for. If they stop being emitted, ops loses SSO
    wiring, bench membership, and sizing in one go. An imported entry
    carries its origin where a curated one carries its bench pack and its
    measured peak."""
    doc = json.loads((ROOT / "catalog.json").read_text())
    assert "postgres_default_image" not in doc
    assert set(doc["sizing"]) == {
        "last_measured_at", "measurement_host", "measurement_method",
    }
    for entry in doc["templates"]:
        assert entry["sso_mode"]
        assert entry["en"]["display_name"] and entry["fr"]["display_name"]
        if entry.get("status") == "imported":
            assert entry["origin"]["licence"]
            continue
        assert entry["bench_pack"]
        assert entry["sizing"]["peak_ram_mb"] > 0


def test_catalog_json_and_templates_json_cover_the_same_ids():
    templates = json.loads((ROOT / "templates.json").read_text())["templates"]
    catalog = json.loads((ROOT / "catalog.json").read_text())["templates"]
    assert {t["name"] for t in templates} == {e["app_name"] for e in catalog}
    assert len(templates) == len(catalog)


def test_the_blocks_managed_env_reaches_the_catalog_as_env_lines_and_keys():
    """The managed key's line keeps its place in env_defaults and carries the
    block's expression, and env_managed_keys lists the keys in the block's
    order; the host's render and the Portainer sentinel both read them."""
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    managed = 0
    for entry in model.load_sources():
        published = catalog[entry.slug]
        assert published["env_defaults"] == entry.env_defaults, entry.slug
        assert published.get("env_managed_keys") == (list(entry.managed_env) or None), entry.slug
        for key, expression in entry.managed_env.items():
            assert f"{key}={expression}" in published["env_defaults"], entry.slug
            managed += 1
    assert managed > 0


def test_s3_reconcile_reaches_the_catalog_unchanged():
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    for entry in model.load_sources():
        assert catalog[entry.slug].get("s3_reconcile") == entry.s3_reconcile, entry.slug


def test_object_store_reaches_the_catalog_unchanged():
    """The bucket repair reads the env keys and the switch out of
    catalog.json; an entry that declares none carries no key."""
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    declared = 0
    for entry in model.load_sources():
        assert catalog[entry.slug].get("object_store") == entry.object_store, entry.slug
        declared += bool(entry.object_store)
    assert declared > 0


def test_no_blueprint_carries_a_generated_quiesce_file():
    """The block in the compose is the one home of a backup mode."""
    assert not list((ROOT / "blueprints").glob("*/quiesce.yml"))


def test_quiesce_reaches_the_catalog_unchanged():
    """The nightly maintenance reads this out of catalog.json on a client
    host, unattended. A key the render drops or reshapes is a backup taken
    without the quiesce, and nothing downstream can tell that from an app
    that declared none."""
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    declared = 0
    for entry in model.load_sources():
        assert catalog[entry.slug].get("quiesce") == entry.quiesce, entry.slug
        if entry.quiesce:
            declared += 1
    assert declared > 0


def test_lifecycle_reaches_the_catalog_unchanged():
    """The update engine and the recovery engine read this out of
    catalog.json on a client host, unattended. A key the render drops is a
    migration that never runs, or a volume a rollback does not put back, and
    nothing downstream can tell that from an app that declared none."""
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    migrating = versioning = 0
    for entry in model.load_sources():
        spec = entry.lifecycle
        assert catalog[entry.slug].get("lifecycle") == spec, entry.slug
        if spec:
            assert spec.get("migrate") or spec.get("versioned_volumes"), entry.slug
            migrating += bool(spec.get("migrate"))
            versioning += bool(spec.get("versioned_volumes"))
    assert migrating > 0 and versioning > 0


def test_every_blueprint_carries_its_compose_and_its_readme():
    """The blueprint directory is the unit Portainer clones. Whoever
    opens it gets the stack file and the description of what it deploys
    in the same place."""
    for entry in model.load_sources():
        app_dir = ROOT / "blueprints" / entry.slug
        assert (app_dir / "docker-compose.yml").is_file(), entry.slug
        assert (app_dir / "README.md").is_file(), entry.slug


def test_the_catalog_points_at_the_blueprint_compose():
    catalog = json.loads((ROOT / "catalog.json").read_text())["templates"]
    for e in catalog:
        assert e["compose_file"] == f"blueprints/{e['id']}/docker-compose.yml"
        assert (ROOT / e["compose_file"]).is_file(), e["id"]


def test_a_readme_carries_both_languages_and_no_jinja():
    """The README ships in a public repo. An unresolved expression there
    reads as the literal value a client is meant to type.

    Matched on `{{ ` with the space: the catalog's Jinja is spaced, and
    the setup steps legitimately quote Go templates (`{{.Names}}`) in
    the docker commands they tell a reader to run."""
    jinja = re.compile(r"\{\{\s")
    for entry in model.load_sources():
        body = (ROOT / "blueprints" / entry.slug / "README.md").read_text()
        assert "## English" in body, entry.slug
        assert "## Français" in body, entry.slug
        assert not jinja.search(body), entry.slug


def test_a_readme_has_no_root_relative_link():
    """GitHub resolves a root-relative link against github.com, not the
    docs site, so every one of them has to leave here absolute."""
    for entry in model.load_sources():
        body = (ROOT / "blueprints" / entry.slug / "README.md").read_text()
        assert "](/" not in body, entry.slug


def _requiring(entry: model.Entry, version: str | None) -> model.Entry:
    raw = copy.deepcopy(entry.raw)
    raw["x-catena"].pop("requires_catena", None)
    if version:
        raw["x-catena"]["requires_catena"] = version
    return model.Entry(raw, entry.block)


def test_an_entry_needing_a_newer_catena_says_so_in_its_description_and_readme():
    """A server on an older release deploys such an entry in a shape its
    panel cannot serve. The client reads the description in Portainer's
    gallery and the README in the blueprint, so both name the release."""
    entry = model.load_sources()[0]
    needs = _requiring(entry, "0.11.0")
    description = render.render_portainer_template(needs, "logo.svg")["description"]
    assert description.startswith("Requires Catena 0.11.0 or later. ")
    assert render.render_portainer_template(needs, "logo.svg")["note"].startswith(
        "<p>Requires Catena 0.11.0 or later.</p>"
    )
    assert render.render_catalog_entry(needs)["requires_catena"] == "0.11.0"
    assert "Requires Catena 0.11.0 or later. " in render.render_index_html([needs], model.load_meta())
    body = readme.render_readme(needs)
    assert "**Requires Catena 0.11.0 or later.**" in body
    assert "**Requiert Catena 0.11.0 ou une version ultérieure.**" in body

    plain = _requiring(entry, None)
    assert "Requires Catena" not in render.render_portainer_template(plain, "logo.svg")["description"]
    assert "requires_catena" not in render.render_catalog_entry(plain)
    assert "Catena 0.11.0" not in readme.render_readme(plain)


def test_the_required_release_is_a_full_version():
    schema = model._schema(model.SOURCE_SCHEMA)
    entry = model.load_sources()[0]
    assert model._validate_schema(_requiring(entry, "0.11.0").raw, schema, "x") == []
    for version in ("0.11", "v0.11.0", "latest"):
        errors = model._validate_schema(_requiring(entry, version).raw, schema, "x")
        assert errors and "requires_catena" in errors[0], version


def test_the_published_catalog_names_each_required_release():
    """The committed artifacts carry what each source requires."""
    templates = {t["name"]: t for t in json.loads((ROOT / "templates.json").read_text())["templates"]}
    required = 0
    for entry in model.load_sources():
        if not entry.requires_catena:
            continue
        required += 1
        sentence = f"Requires Catena {entry.requires_catena} or later."
        assert templates[entry.raw["name"]]["description"].startswith(sentence), entry.slug
        assert f"**{sentence}**" in (entry.app_dir / "README.md").read_text(), entry.slug
    assert required > 0


def test_rendering_does_not_touch_the_hand_edited_compose():
    """blueprints/ mixes generated files with hand-edited ones, so the
    render cannot wipe and rebuild it."""
    entry = model.load_sources()[0]
    compose = ROOT / "blueprints" / entry.slug / "docker-compose.yml"
    before = compose.read_bytes()
    render.render_all()
    assert compose.read_bytes() == before


def test_rendering_twice_gives_identical_artifacts():
    render.render_all()
    first = {path: path.read_bytes()
             for path in (render.TEMPLATES_JSON, render.CATALOG_JSON, render.INDEX_HTML)}
    render.render_all()
    for path, content in first.items():
        assert path.read_bytes() == content, path.name


def test_a_blueprint_with_no_source_file_is_dropped(tmp_path, monkeypatch):
    """A template deleted from sources/ leaves a directory Portainer
    would still clone and deploy."""
    blueprints = tmp_path / "blueprints"
    for slug in ("kept", "orphan"):
        (blueprints / slug).mkdir(parents=True)
        (blueprints / slug / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(render, "BLUEPRINTS", blueprints)

    kept = model.Entry({"id": "kept", "x-catena": {}})
    assert render._prune_orphan_blueprints([kept]) == ["orphan"]
    assert (blueprints / "kept").is_dir()
    assert not (blueprints / "orphan").exists()
