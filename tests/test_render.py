"""Unit tests for lib/render.py: the transforms that decide what a
client's Portainer shows, and what catenahq/ops reads back.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, render  # noqa: E402


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
        "x-catena": {
            "env_defaults": ["DOMAIN_HOST=x.example.com", "OIDC_CLIENT_SECRET=placeholder"],
            "env_managed_keys": ["OIDC_CLIENT_SECRET"],
        },
    })
    env = {f["name"]: f for f in render.render_env(entry)}
    assert env["DOMAIN_HOST"]["default"] == "x.example.com"
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
    the converge is what resolves it."""
    portainer = (ROOT / "templates.json").read_text()
    assert "lookup('password'" not in portainer
    assert "{{" not in portainer

    catalog = (ROOT / "catalog.json").read_text()
    assert "lookup('password'" in catalog


def test_catalog_json_carries_what_portainer_cannot():
    """The second artifact exists precisely for the fields the Portainer
    format has no slot for. If they stop being emitted, ops loses SSO
    wiring, bench membership, and sizing in one go."""
    doc = json.loads((ROOT / "catalog.json").read_text())
    assert doc["postgres_default_image"].startswith("postgres:")
    assert set(doc["sizing"]) == {
        "last_measured_at", "measurement_host", "measurement_method",
    }
    for entry in doc["templates"]:
        assert entry["sso_mode"]
        assert entry["bench_pack"]
        assert entry["sizing"]["peak_ram_mb"] > 0
        assert entry["en"]["display_name"] and entry["fr"]["display_name"]


def test_catalog_json_and_templates_json_cover_the_same_ids():
    templates = json.loads((ROOT / "templates.json").read_text())["templates"]
    catalog = json.loads((ROOT / "catalog.json").read_text())["templates"]
    assert {t["name"] for t in templates} == {e["app_name"] for e in catalog}
    assert len(templates) == len(catalog)


def test_quiesce_yaml_is_emitted_for_every_declaring_entry():
    for entry in model.load_sources():
        path = ROOT / "blueprints" / entry.slug / "quiesce.yml"
        assert path.exists() == bool(entry.quiesce), entry.slug


def test_post_restore_migrate_reaches_the_catalog_unchanged():
    """The recovery engine reads this out of catalog.json on a client
    host mid-restore. A key the render drops is a migration that never
    runs, and nothing downstream can tell that from an app that declared
    none."""
    catalog = {
        e["id"]: e for e in json.loads((ROOT / "catalog.json").read_text())["templates"]
    }
    declared = 0
    for entry in model.load_sources():
        spec = entry.post_restore_migrate
        assert catalog[entry.slug].get("post_restore_migrate") == spec, entry.slug
        if spec:
            declared += 1
            assert spec["commands"], entry.slug
    assert declared > 0


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


def test_rendering_does_not_touch_the_hand_edited_compose():
    """blueprints/ mixes generated files with hand-edited ones, so the
    render cannot wipe and rebuild it."""
    entry = model.load_sources()[0]
    compose = ROOT / "blueprints" / entry.slug / "docker-compose.yml"
    before = compose.read_bytes()
    render.render_all()
    assert compose.read_bytes() == before


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
