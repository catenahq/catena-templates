"""Unit tests for lib/render.py: the transforms that decide what catalog.json
holds, which catenahq/ops reads back.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, render  # noqa: E402


def test_no_generated_file_is_a_portainer_index():
    assert not (ROOT / "templates.json").exists()
    assert not (ROOT / "Schema.json").exists()
    assert not (ROOT / "sources").exists()


def test_catalog_json_keeps_the_jinja_of_a_generated_secret():
    """The host resolves the lookup that mints a generated secret."""
    assert "lookup('password'" in (ROOT / "catalog.json").read_text()


def test_catalog_json_carries_what_the_ops_tooling_reads():
    """If catalog.json stops emitting these fields, ops loses SSO wiring, bench
    membership and sizing in one go. An imported entry
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


def test_catalog_json_lists_every_app():
    catalog = json.loads((ROOT / "catalog.json").read_text())["templates"]
    entries = model.load_sources()
    assert {e["id"] for e in catalog} == {e.slug for e in entries}
    assert len(catalog) == len(entries)


def test_the_blocks_managed_env_reaches_the_catalog_as_env_lines_and_keys():
    """The managed key's line keeps its place in env_defaults and carries the
    block's expression, and env_managed_keys lists the keys in the block's
    order; the host's settling of the env reads them."""
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


def test_every_blueprint_carries_its_compose():
    """The blueprint directory is the unit a host fetches."""
    for entry in model.load_sources():
        assert (ROOT / "blueprints" / entry.slug / "docker-compose.yml").is_file(), entry.slug


def test_the_catalog_points_at_the_blueprint_compose():
    catalog = json.loads((ROOT / "catalog.json").read_text())["templates"]
    for e in catalog:
        assert e["compose_file"] == f"blueprints/{e['id']}/docker-compose.yml"
        assert (ROOT / e["compose_file"]).is_file(), e["id"]


def _sample() -> model.Entry:
    prose = {"display_name": "Example", "summary": "An example.", "what_it_is": "An example.",
             "replaces": ["Other"], "setup_steps": "1. Open it.\n"}
    raw = {"id": "example", "schema": 1, "app_name": "catena-example",
           "upstream_url": "https://example.com/", "categories": ["Testing"],
           "sso_mode": "none", "bench": {"pack": "nodb"}, "sizing": {"peak_ram_mb": 256},
           "en": {**prose, "compose_description": "An example.\n"},
           "fr": {**prose, "compose_description": "Un exemple.\n"}}
    block = {"schema": 2, "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}}}
    return model.Entry(raw, block, {"host": "x.example.com", "service": "app", "port": 8080})


def _requiring(entry: model.Entry, version: str | None) -> model.Entry:
    block = {k: v for k, v in entry.block.items() if k != "requires_catena"}
    if version:
        block["requires_catena"] = version
    return model.Entry(entry.raw, block, entry.domain)


def test_an_entry_needing_a_newer_catena_says_so_in_its_index_row():
    """A server on an older release deploys such an entry in a shape its
    panel cannot serve. The preview names the release."""
    needs = _requiring(_sample(), "0.11.0")
    assert render.render_catalog_entry(needs)["requires_catena"] == "0.11.0"
    assert "Requires Catena 0.11.0 or later. " in render.render_index_html([needs], model.load_meta())

    plain = _requiring(_sample(), None)
    assert "requires_catena" not in render.render_catalog_entry(plain)
    assert "Catena 0.11.0" not in render.render_index_html([plain], model.load_meta())


def test_an_entry_without_a_placed_logo_keeps_the_placeholder():
    """A placed logo.png replaces the placeholder; an entry with none keeps it."""
    for entry in model.load_sources():
        if entry.logo_path.exists():
            assert not (entry.app_dir / "logo.svg").exists(), entry.slug
        else:
            assert (entry.app_dir / "logo.svg").is_file(), entry.slug


def test_the_required_release_is_a_full_version():
    block_schema = model._schema(model.BLOCK_SCHEMA)
    assert model._validate_schema({"schema": 2, "requires_catena": "0.11.0"}, block_schema, "x") == []
    for version in ("0.11", "v0.11.0", "latest"):
        errors = model._validate_schema({"schema": 2, "requires_catena": version}, block_schema, "x")
        assert errors and "requires_catena" in errors[0], version


def test_the_index_row_names_each_required_release():
    required = 0
    index = (ROOT / "index.html").read_text()
    for entry in model.load_sources():
        if not entry.requires_catena:
            continue
        required += 1
        assert f"Requires Catena {entry.requires_catena} or later." in index, entry.slug
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
             for path in (render.CATALOG_JSON, render.INDEX_HTML)}
    render.render_all()
    for path, content in first.items():
        assert path.read_bytes() == content, path.name


def test_a_blueprint_with_no_card_file_is_dropped(tmp_path, monkeypatch):
    """A template whose app.json is deleted leaves a directory a host could
    still fetch and deploy."""
    blueprints = tmp_path / "blueprints"
    for slug in ("kept", "orphan"):
        (blueprints / slug).mkdir(parents=True)
        (blueprints / slug / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(render, "BLUEPRINTS", blueprints)

    kept = model.Entry({"id": "kept"})
    assert render._prune_orphan_blueprints([kept]) == ["orphan"]
    assert (blueprints / "kept").is_dir()
    assert not (blueprints / "orphan").exists()


# The keys a catalog.json entry of an app with a web address carries.
CONSUMED_KEYS = {
    "id", "app_name", "upstream_url", "sso_mode", "domain_host", "domain_service", "domain_port",
    "compose_file", "env_defaults", "env", "bench_pack", "bench_fixture", "hostname_baked",
    "categories", "sizing", "en", "fr",
}
# The keys of each language's prose that ops reads from every catalog.json entry.
CONSUMED_PROSE_KEYS = {"display_name", "what_it_is", "replaces", "compose_description", "setup_steps"}


def _tree(root: Path) -> None:
    """A one-app tree in another repository's layout."""
    prose = {"display_name": "Example", "summary": "An example.", "what_it_is": "An example.",
             "replaces": ["Other"], "setup_steps": "1. Open it.\n"}
    app_dir = root / "blueprints" / "example"
    app_dir.mkdir(parents=True)
    block = {"schema": 2, "requires_catena": "0.11.5",
             "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}}}
    services = {"app": {"image": "example/app:1.2.3", "labels": [
        "vps.route.host=${DOMAIN_HOST}", "vps.route.port=8080"]}}
    (app_dir / "docker-compose.yml").write_text(json.dumps({"x-catena": block, "services": services}))
    meta = {"order": ["example"], "sizing": {"measurement_host": "h", "last_measured_at": "d"}}
    (root / "catalog-meta.json").write_text(json.dumps(meta))
    card = {"schema": 1, "app_name": "catena-example", "upstream_url": "https://example.com/",
            "categories": ["Testing"], "sso_mode": "none", "bench": {"pack": "nodb"},
            "sizing": {"peak_ram_mb": 256},
            "en": {**prose, "compose_description": "An example.\n"},
            "fr": {**prose, "compose_description": "Un exemple.\n"}}
    (app_dir / "app.json").write_text(json.dumps(card))


def _rendered(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TEMPLATES_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, str(ROOT / "build" / "render.py")], cwd=root, env=env,
                          capture_output=True, text=True, check=False)


def test_an_app_renders_its_catalog_entry_and_index_row(tmp_path):
    _tree(tmp_path)
    done = _rendered(tmp_path)
    assert done.returncode == 0, done.stderr
    published = json.loads((tmp_path / "catalog.json").read_text())["templates"][0]
    for lang in ("en", "fr"):
        assert CONSUMED_PROSE_KEYS <= set(published[lang])
    assert published["domain_host"] == "x.example.com"
    assert (published["domain_service"], published["domain_port"]) == ("app", 8080)
    assert published["requires_catena"] == "0.11.5"
    assert CONSUMED_KEYS <= set(published)
    assert not (tmp_path / "templates.json").exists()


def test_the_render_neither_writes_nor_requires_a_readme_and_leaves_one_alone(tmp_path):
    _tree(tmp_path)
    readme = tmp_path / "blueprints" / "example" / "README.md"
    assert _rendered(tmp_path).returncode == 0
    assert not readme.exists()
    readme.write_text("kept\n")
    assert _rendered(tmp_path).returncode == 0
    assert readme.read_text() == "kept\n"


def _validated(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TEMPLATES_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, str(ROOT / "build" / "validate.py")], cwd=root, env=env,
                          capture_output=True, text=True, check=False)


def test_validate_passes_a_tree_of_cards(tmp_path):
    _tree(tmp_path)
    done = _validated(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "validate OK: 1 app(s)" in done.stdout


def test_validate_names_a_card_that_breaks_its_schema(tmp_path):
    _tree(tmp_path)
    path = tmp_path / "blueprints" / "example" / "app.json"
    card = json.loads(path.read_text())
    del card["en"]["summary"]
    path.write_text(json.dumps(card))
    done = _validated(tmp_path)
    assert done.returncode == 1
    assert "summary" in done.stderr


def test_rendering_a_tree_twice_gives_identical_artifacts(tmp_path):
    _tree(tmp_path)
    names = ("catalog.json", "index.html")
    assert _rendered(tmp_path).returncode == 0
    first = {name: (tmp_path / name).read_bytes() for name in names}
    assert _rendered(tmp_path).returncode == 0
    assert {name: (tmp_path / name).read_bytes() for name in names} == first


def _neat_tree(root: Path) -> Path:
    """The tree with its app `example` held as neat files; its folder."""
    _tree(root)
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
    names = ("catalog.json", "index.html", "blueprints/example/docker-compose.yml")
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


def test_a_blueprint_with_a_card_is_kept_by_the_render(tmp_path, monkeypatch):
    blueprints = tmp_path / "blueprints"
    (blueprints / "moved").mkdir(parents=True)
    monkeypatch.setattr(render, "BLUEPRINTS", blueprints)
    moved = model.Entry({"id": "moved", "en": {"display_name": "Moved"}})
    assert render._prune_orphan_blueprints([moved]) == []
    assert (blueprints / "moved").is_dir()
