"""Unit tests for lib/render.py: the transforms that decide what a
client's Portainer shows, and what catenahq/ops reads back.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
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


def test_a_readme_prints_the_logo_source_and_notice_in_both_languages():
    entry = model.load_sources()[0]
    raw = copy.deepcopy(entry.raw)
    raw["x-catena"]["logo"] = {"source": "https://example.com/logo.svg",
                               "notice": "Example is a trademark of Example Inc."}
    body = readme.render_readme(model.Entry(raw, entry.block))
    assert "- **Logo:** <https://example.com/logo.svg>. Example is a trademark of Example Inc." in body
    assert "- **Logo :** <https://example.com/logo.svg>. Example is a trademark of Example Inc." in body

    del raw["x-catena"]["logo"]["notice"]
    body = readme.render_readme(model.Entry(raw, entry.block))
    assert "- **Logo:** <https://example.com/logo.svg>.\n" in body

    del raw["x-catena"]["logo"]
    assert "**Logo" not in readme.render_readme(model.Entry(raw, entry.block))


def test_the_published_readmes_name_each_logo_source():
    """A placed logo is the one the templates.json entry names, and its
    README prints the source; an entry with none keeps the placeholder."""
    templates = {t["name"]: t for t in json.loads((ROOT / "templates.json").read_text())["templates"]}
    for entry in model.load_sources():
        logo = templates[entry.raw["name"]]["logo"]
        body = (entry.app_dir / "README.md").read_text()
        if entry.logo_path.exists():
            assert logo.endswith("/logo.png"), entry.slug
            assert not (entry.app_dir / "logo.svg").exists(), entry.slug
            assert f"<{entry.catena['logo']['source']}>" in body, entry.slug
        else:
            assert logo.endswith("/logo.svg"), entry.slug
            assert "**Logo" not in body, entry.slug


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


# The keys a catalog.json entry of an app with a web address carries, whichever layout holds the app.
CONSUMED_KEYS = {
    "id", "app_name", "upstream_url", "sso_mode", "domain_host", "domain_service", "domain_port",
    "compose_file", "env_defaults", "env", "bench_pack", "bench_fixture", "hostname_baked",
    "categories", "sizing", "en", "fr",
}


def _tree(root: Path, as_card: bool) -> None:
    """A one-app tree in another repository's layout: the app `example` held as
    sources/example.json or as blueprints/example/app.json."""
    prose = {"display_name": "Example", "what_it_is": "An example.", "replaces": ["Other"],
             "setup_steps": "1. Open it.\n"}
    cat = {"app_name": "catena-example", "upstream_url": "https://example.com/",
           "sso_mode": "none", "bench": {"pack": "nodb"}, "sizing": {"peak_ram_mb": 256},
           "en": {**prose, "compose_description": "An example.\n"},
           "fr": {**prose, "compose_description": "Un exemple.\n"}}
    app_dir = root / "blueprints" / "example"
    app_dir.mkdir(parents=True)
    block = {"schema": 2, "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}}}
    services = {"app": {"image": "example/app:1.2.3", "labels": [
        "vps.route.host=${DOMAIN_HOST}", "vps.route.port=8080"]}}
    meta = {"order": ["example"], "sizing": {"measurement_host": "h", "last_measured_at": "d"}}
    if as_card:
        block["requires_catena"] = "0.11.5"
        (app_dir / "docker-compose.yml").write_text(
            json.dumps({"x-catena": block, "services": services}))
        (root / "catalog-meta.json").write_text(json.dumps(meta))
        card = {"schema": 1, **cat, "categories": ["Testing"]}
        for lang in ("en", "fr"):
            del card[lang]["compose_description"]
        (app_dir / "app.json").write_text(json.dumps(card))
        return
    (app_dir / "docker-compose.yml").write_text(
        json.dumps({"x-catena": block, "services": services}))
    cat["domain"] = {"host": "x.example.com", "service": "app", "port": 8080}
    cat["requires_catena"] = "0.11.5"
    (root / "sources").mkdir()
    (root / "sources" / "_meta.json").write_text(json.dumps(meta))
    (root / "sources" / "example.json").write_text(json.dumps({
        "id": "example", "type": 2, "title": "Example", "name": "catena-example",
        "categories": ["Testing"], "platform": "linux", "x-catena": cat}))


def _rendered(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TEMPLATES_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, str(ROOT / "build" / "render.py")], cwd=root, env=env,
                          capture_output=True, text=True, check=False)


def test_an_app_held_as_a_card_renders_what_its_source_renders(tmp_path):
    """The two layouts give one catalog entry, README and index row; the card has no
    Portainer fields, so templates.json lists the source app alone."""
    sourced, carded = tmp_path / "sourced", tmp_path / "carded"
    _tree(sourced, as_card=False)
    _tree(carded, as_card=True)
    for root in (sourced, carded):
        done = _rendered(root)
        assert done.returncode == 0, done.stderr

    def catalog(root: Path) -> dict:
        entry = json.loads((root / "catalog.json").read_text())["templates"][0]
        for lang in ("en", "fr"):
            entry[lang].pop("compose_description", None)
            entry[lang].pop("summary", None)
        return entry

    assert catalog(carded) == catalog(sourced)
    published = catalog(carded)
    assert published["domain_host"] == "x.example.com"
    assert (published["domain_service"], published["domain_port"]) == ("app", 8080)
    assert published["requires_catena"] == "0.11.5"
    assert CONSUMED_KEYS <= set(published)

    def readme(root: Path) -> list[str]:
        return (root / "blueprints" / "example" / "README.md").read_text().splitlines()[1:]

    assert readme(carded) == readme(sourced)
    first = (carded / "blueprints" / "example" / "README.md").read_text().splitlines()[0]
    assert "from blueprints/example/app.json." in first
    assert (carded / "index.html").read_text() == (sourced / "index.html").read_text()
    assert [t["name"] for t in json.loads((sourced / "templates.json").read_text())["templates"]] == [
        "catena-example"]
    assert json.loads((carded / "templates.json").read_text())["templates"] == []


def test_rendering_a_card_tree_twice_gives_identical_artifacts(tmp_path):
    _tree(tmp_path, as_card=True)
    names = ("catalog.json", "index.html", "templates.json", "blueprints/example/README.md")
    assert _rendered(tmp_path).returncode == 0
    first = {name: (tmp_path / name).read_bytes() for name in names}
    assert _rendered(tmp_path).returncode == 0
    assert {name: (tmp_path / name).read_bytes() for name in names} == first


def _neat_tree(root: Path) -> Path:
    """The card tree with its app `example` held as neat files; its folder."""
    _tree(root, as_card=True)
    folder = root / "blueprints" / "example"
    (folder / "docker-compose.yml").unlink()
    (folder / "services.yml").write_text(
        "services:\n  app:\n    image: example/app:1.2.3\n    labels:\n"
        "      - vps.route.host=${DOMAIN_HOST}\n      - vps.route.port=8080\n")
    (folder / "settings.yml").write_text("env:\n  DOMAIN_HOST:\n    setting: {default: x.example.com}\n")
    (folder / "lifecycle.yml").write_text("requires_catena: 0.11.5\n")
    return folder


def test_the_render_packs_an_app_held_as_neat_files_before_it_reads_the_block(tmp_path):
    folder = _neat_tree(tmp_path)
    done = _rendered(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "packed 1 compose file(s)" in done.stdout
    assert (folder / "docker-compose.yml").read_text() == (
        "x-catena:\n  schema: 2\n  requires_catena: 0.11.5\n  env:\n    DOMAIN_HOST:\n"
        "      setting: {default: x.example.com}\n\n" + (folder / "services.yml").read_text())
    entry = json.loads((tmp_path / "catalog.json").read_text())["templates"][0]
    assert (entry["domain_host"], entry["domain_service"], entry["requires_catena"]) == (
        "x.example.com", "app", "0.11.5")


def test_rendering_an_app_held_as_neat_files_twice_gives_identical_artifacts(tmp_path):
    folder = _neat_tree(tmp_path)
    names = ("catalog.json", "index.html", "blueprints/example/README.md", "blueprints/example/docker-compose.yml")
    assert _rendered(tmp_path).returncode == 0
    first = {name: (tmp_path / name).read_bytes() for name in names}
    done = _rendered(tmp_path)
    assert done.returncode == 0 and "packed" not in done.stdout
    assert {name: (tmp_path / name).read_bytes() for name in names} == first
    assert (folder / "docker-compose.yml").is_file()


def test_the_render_stops_on_neat_files_out_of_normal_form(tmp_path):
    folder = _neat_tree(tmp_path)
    (folder / "settings.yml").write_text("bogus: 1\n")
    done = _rendered(tmp_path)
    assert done.returncode == 1
    assert "blueprints/example/settings.yml:1: bogus is not a key of the block" in done.stdout
    assert not (folder / "docker-compose.yml").exists()


def test_a_card_blueprint_is_kept_by_the_render(tmp_path, monkeypatch):
    blueprints = tmp_path / "blueprints"
    (blueprints / "moved").mkdir(parents=True)
    monkeypatch.setattr(render, "BLUEPRINTS", blueprints)
    moved = model.Entry({"id": "moved", "en": {"display_name": "Moved"}}, card=True)
    assert render._prune_orphan_blueprints([moved]) == []
    assert (blueprints / "moved").is_dir()
