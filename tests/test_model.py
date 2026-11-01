"""Unit tests for lib/model.py: the schema layer plus the cross-file
invariants a JSON Schema cannot express.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model  # noqa: E402


def _valid_doc(slug: str = "example") -> dict:
    """An app.json as written (the folder name is the id)."""
    prose = {
        "display_name": "Example",
        "summary": "test",
        "what_it_is": "test",
        "replaces": [],
        "compose_description": "test",
        "setup_steps": "1. none",
    }
    return {
        "schema": 1,
        "app_name": slug,
        "upstream_url": "https://example.com",
        "categories": ["Testing"],
        "sso_mode": "none",
        "bench": {"pack": "nodb"},
        "sizing": {"peak_ram_mb": 256},
        "en": dict(prose),
        "fr": {**prose, "setup_steps": "1. aucune"},
    }


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """A blueprint tree with its catalog-meta.json beside it. A template is
    two hand-edited files and the loader reads both, so a fixture that
    redirects only one of them tests a layout that cannot exist."""
    blueprints = tmp_path / "blueprints"
    monkeypatch.setattr(model, "BLUEPRINTS", blueprints)
    _blueprint(blueprints, "example")
    _meta(tmp_path, ["example"])
    return tmp_path


ENV = {
    "DOMAIN_HOST": {"setting": {"default": "x.example.com"}},
    "DB_PASSWORD": {"setting": {"default": "secret"}},
}


def _blueprint(blueprints: Path, slug: str) -> None:
    app_dir = blueprints / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / model.COMPOSE_NAME).write_text(
        yaml.safe_dump({model.BLOCK_KEY: {"schema": 2, "env": ENV}, "services": {}}))


def _block(block: dict, slug: str = "example") -> None:
    """Put `block` at the top of the blueprint's compose as its x-catena block;
    a block with no env gets the default one."""
    path = model.BLUEPRINTS / slug / model.COMPOSE_NAME
    block = {"env": ENV, **block}
    path.write_text(yaml.safe_dump({model.BLOCK_KEY: block, "services": {}}, sort_keys=False))


def _meta(root: Path, order: list[str]) -> None:
    (root / model.CATALOG_META).write_text(json.dumps({"order": order}))


def _write(doc: dict | None = None, slug: str = "example") -> None:
    app_dir = model.BLUEPRINTS / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / model.CARD_NAME).write_text(json.dumps(doc or _valid_doc(slug)))


def test_valid_card_loads(catalog):
    _write()
    entries = model.load_sources()
    assert [e.slug for e in entries] == ["example"]
    assert entries[0].sizing["peak_ram_mb"] == 256
    assert entries[0].block == {"schema": 2, "env": ENV}
    assert entries[0].quiesce is None
    assert entries[0].managed_env == {}


def test_order_drives_the_returned_sequence(catalog):
    """The order is curated (hubs first), not alphabetical: it is what
    the generated catalog and its preview show."""
    for slug in ("alpha", "omega", "middle"):
        _write(slug=slug)
        _blueprint(model.BLUEPRINTS, slug)
    _meta(catalog, ["omega", "alpha", "middle"])
    assert [e.slug for e in model.load_sources()] == ["omega", "alpha", "middle"]


def test_template_missing_from_order_is_an_error(catalog):
    _write()
    _write(slug="unlisted")
    _blueprint(model.BLUEPRINTS, "unlisted")
    with pytest.raises(model.SourceError, match="not listed in catalog-meta.json order"):
        model.load_sources()


def test_order_naming_an_absent_template_is_an_error(catalog):
    _write()
    _meta(catalog, ["example", "deleted-template"])
    with pytest.raises(model.SourceError, match="has no app.json"):
        model.load_sources()


def test_a_template_without_a_compose_in_its_blueprint_is_an_error(catalog):
    """A template's compose is found at blueprints/<id>/docker-compose.yml;
    a template without that file has none, which is an error."""
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).unlink()
    _write()
    with pytest.raises(model.SourceError, match="does not exist"):
        model.load_sources()


def test_compose_file_is_derived_from_the_id(catalog):
    _write()
    entry = model.load_sources()[0]
    assert entry.compose_file == "blueprints/example/docker-compose.yml"
    assert entry.compose_path == model.BLUEPRINTS / "example" / "docker-compose.yml"


def test_a_compose_without_an_env_is_an_error(catalog):
    """The deploy form lists the keys of the block's env; a template with
    none lists nothing."""
    _write()
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).write_text("services: {}\n")
    with pytest.raises(model.SourceError, match="declares no x-catena env"):
        model.load_sources()


def test_env_defaults_are_the_env_as_key_value_lines(catalog):
    """A setting gives its default, a generated secret the lookup expression
    that mints it, a managed key its expression; the order is the block's."""
    _write()
    _block({"schema": 2, "env": {
        "DOMAIN_HOST": {"managed": {"expr": "x.{{ cloudflare_zone }}"}},
        "NAME": {"setting": {"default": "auto, 7", "label": {"en": "Name", "fr": "Nom"}}},
        "DB_PASSWORD": {"generated": {"length": 32, "alphabet": "alnum"}},
        "KEY": {"generated": {"length": 64, "alphabet": "hex"}},
    }})
    entry = model.load_sources()[0]
    assert entry.env_defaults == [
        "DOMAIN_HOST=x.{{ cloudflare_zone }}",
        "NAME=auto, 7",
        "DB_PASSWORD={{ lookup('password', '/dev/null length=32 chars=ascii_letters,digits') }}",
        "KEY={{ lookup('password', '/dev/null length=64 chars=hexdigits') | lower }}",
    ]
    assert entry.managed_env == {"DOMAIN_HOST": "x.{{ cloudflare_zone }}"}


@pytest.mark.parametrize("env,needle", [
    ({"A": {}}, "env/A"),
    ({"A": {"setting": {"default": "x"}, "managed": {"expr": "y"}}}, "env/A"),
    ({"A": {"setting": {"default": "x", "label": {"en": "A"}}}}, "label"),
    ({"A": {"generated": {"length": 0, "alphabet": "hex"}}}, "length"),
    ({"A": {"generated": {"length": 8, "alphabet": "base64"}}}, "alphabet"),
    ({"1A": {"setting": {"default": "x"}}}, "1A"),
    ({"DOCKER_HOST": {"setting": {"default": "x"}}}, "DOCKER_HOST"),
    ({"path": {"setting": {"default": "x"}}}, "path"),
    ({"HOME": {"setting": {"default": "x"}}}, "HOME"),
    ({"LD_PRELOAD": {"setting": {"default": "x"}}}, "LD_PRELOAD"),
    ({"https_proxy": {"setting": {"default": "x"}}}, "https_proxy"),
])
def test_an_env_the_format_refuses_is_reported(catalog, env, needle):
    _write()
    _block({"schema": 2, "env": env})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_the_other_schema_2_fields_are_checked(catalog):
    _write()
    good = {
        "version_from": "app",
        "hooks": {"service": "app", "configure": [["sh", "-c", "true"]], "timeout_seconds": 60},
        "needs": {"certificate": {"names": ["a.example.com"], "volume": "certs"},
                  "dns": [{"type": "MX", "name": "example.com", "value": "mail", "priority": 10}]},
        "data": {"db": {"volume": "db-data", "upgrade": "dump_restore"}},
        "write_hold": "backup_mode",
        "images": {"app": {"policy": "minor", "rule": {"kind": "semver", "stops": ["1.0.0"]}}},
        "secrets": ["turn_secret"],
        "stops": ["1.0.0"],
        "egress": ["api.example.com"],
        "privileges": ["host_network"],
        "provides": {"turn": {"host": "TURN_HOST", "secret": "TURN_SECRET"}},
    }
    _block({"schema": 2, **good})
    assert model.load_sources()[0].block["write_hold"] == "backup_mode"
    feed = {"app": {"rule": {
        "kind": "pattern", "tag_pattern": "(\\d+)", "feed": "compose", "leader": "acme/lead",
        "compose_url": "https://h/{version}/c.yml", "compose_service": "db"}}}
    _block({"schema": 2, **good, "images": feed})
    assert model.load_sources()[0].block["images"]["app"]["rule"]["feed"] == "compose"
    for field, bad in (("write_hold", "never"), ("privileges", ["root"]),
                       ("images", {"app": {"policy": "sometimes"}}),
                       ("images", {"app": {"rule": {"kind": "bogus"}}}),
                       ("data", {"db": {"volume": "v", "upgrade": "copy"}}),
                       ("needs", {"dns": [{"type": "NS", "name": "a", "value": "b"}]}),
                       ("hooks", {"service": "app"}),
                       ("provides", {"Turn": {"host": "TURN_HOST"}}),
                       ("provides", {"turn": {}}),
                       ("provides", {"turn": {"Host": "TURN_HOST"}}),
                       ("provides", {"turn": {"host": "not a key"}}),
                       ("version_from", "")):
        _block({"schema": 2, **good, field: bad})
        with pytest.raises(model.SourceError, match=field):
            model.load_sources()


def test_a_block_names_its_format(catalog):
    _write()
    path = model.BLUEPRINTS / "example" / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({model.BLOCK_KEY: {"env": ENV}, "services": {}}))
    with pytest.raises(model.SourceError, match="'schema' is a required property"):
        model.load_sources()
    _block({"schema": 1})
    with pytest.raises(model.SourceError, match="schema"):
        model.load_sources()


def test_a_block_holds_no_field_the_format_lacks(catalog):
    _write()
    _block({"schema": 2, "pause_during_migrate_cutover": True})
    with pytest.raises(model.SourceError, match="pause_during_migrate_cutover"):
        model.load_sources()


@pytest.mark.parametrize("object_store,needle", [
    ({"env": {"bucket": "S3_BUCKET", "access_key": "S3_ACCESS_KEY"}}, "secret_key"),
    ({"env": {"bucket": "S3_BUCKET", "access_key": "S3_ACCESS_KEY",
              "secret_key": "S3_SECRET_KEY", "endpoint": "S3_URL"}}, "endpoint"),
    ({"env": {"bucket": "S3 BUCKET", "access_key": "S3_ACCESS_KEY",
              "secret_key": "S3_SECRET_KEY"}}, "S3 BUCKET"),
    ({"env": {"bucket": "B", "access_key": "A", "secret_key": "S"},
      "switch": {"service": "app"}}, "commands"),
])
def test_an_object_store_the_format_refuses_is_reported(catalog, object_store, needle):
    _write()
    _block({"schema": 2, "object_store": object_store})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_a_card_holds_none_of_the_blocks_fields(catalog):
    """The block in the compose is the one home of these fields."""
    for name, value in (("quiesce", {}), ("lifecycle", {}), ("domain", {}),
                        ("requires_catena", "0.11.0"), ("type", 2), ("title", "T"),
                        ("env_managed_keys", ["DOMAIN_HOST"])):
        doc = _valid_doc()
        doc[name] = value
        _write(doc)
        with pytest.raises(model.SourceError, match=f"app.json: .*{name}"):
            model.load_sources()


def test_schema_violation_is_reported(catalog):
    doc = _valid_doc()
    doc["sso_mode"] = "invented-mode"
    _write(doc)
    with pytest.raises(model.SourceError, match="sso_mode"):
        model.load_sources()


def test_sizing_requires_peak_ram(catalog):
    """The bench scheduler reads peak_ram_mb to size parallel slots; a
    template without it cannot be scheduled at all."""
    doc = _valid_doc()
    del doc["sizing"]["peak_ram_mb"]
    _write(doc)
    with pytest.raises(model.SourceError, match="peak_ram_mb"):
        model.load_sources()


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_card_line_is_required(catalog, lang):
    doc = _valid_doc()
    del doc[lang]["summary"]
    _write(doc)
    with pytest.raises(model.SourceError, match="summary"):
        model.load_sources()


def test_quiesce_commands_are_argv_lists(catalog):
    """The host runs each command through docker exec with no shell, so a
    shell line is refused at load. post is optional."""
    _write()
    quiesce = {
        "service": "app",
        "pre": "php occ maintenance:mode --on",
        "timeout_seconds": 30,
    }
    _block({"schema": 2, "quiesce": quiesce})
    with pytest.raises(model.SourceError, match="quiesce/pre"):
        model.load_sources()
    quiesce["pre"] = [["php", "occ", "maintenance:mode", "--on"]]
    _block({"schema": 2, "quiesce": quiesce})
    assert model.load_sources()[0].quiesce["pre"] == [
        ["php", "occ", "maintenance:mode", "--on"]]


def test_quiesce_names_its_service(catalog):
    _write()
    _block({"schema": 2, "quiesce": {"pre": [["php", "occ", "status"]], "timeout_seconds": 10}})
    with pytest.raises(model.SourceError, match="'service' is a required property"):
        model.load_sources()


def test_quiesce_timeout_cap(catalog):
    _write()
    _block({"schema": 2, "quiesce": {
        "service": "app", "pre": [["php", "occ", "status"]], "timeout_seconds": 600}})
    with pytest.raises(model.SourceError, match="timeout_seconds"):
        model.load_sources()


def test_lifecycle_ready_is_one_argv(catalog):
    """ready is one command: the update lane asks it until it answers, and
    a list of commands has no single answer to wait for."""
    _write()
    lifecycle = {
        "service": "app",
        "ready": [["php", "occ", "status"]],
        "migrate": [["php", "occ", "db:add-missing-indices"]],
        "timeout_seconds": 60,
    }
    _block({"schema": 2, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="ready"):
        model.load_sources()
    lifecycle["ready"] = ["php", "occ", "status"]
    _block({"schema": 2, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle["ready"] == ["php", "occ", "status"]


@pytest.mark.parametrize("volumes", [[], ["code", "code"], ["code/../etc"], "code"])
def test_lifecycle_versioned_volumes_are_distinct_volume_names(catalog, volumes):
    """The host copies `<stack>_<name>` before an update and puts the copy
    back on a rollback, so each entry is one volume name, listed once."""
    _write()
    lifecycle = {
        "service": "app",
        "migrate": [["php", "occ", "db:add-missing-indices"]],
        "versioned_volumes": volumes,
        "timeout_seconds": 60,
    }
    _block({"schema": 2, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="versioned_volumes"):
        model.load_sources()
    lifecycle["versioned_volumes"] = ["code", "conf.d"]
    _block({"schema": 2, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle["versioned_volumes"] == ["code", "conf.d"]


def test_a_lifecycle_declares_a_migration_or_versioned_volumes(catalog):
    """An application whose new version upgrades a volume as it starts has
    no migration to declare, only the volumes a rollback puts back. A block
    with neither gives the host nothing to do."""
    _write()
    lifecycle = {"service": "app", "versioned_volumes": ["data"], "timeout_seconds": 300}
    _block({"schema": 2, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle == lifecycle
    del lifecycle["versioned_volumes"]
    _block({"schema": 2, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="lifecycle"):
        model.load_sources()


def test_a_catalog_whose_order_names_no_app_is_an_error(catalog):
    with pytest.raises(model.SourceError, match="order names 'example', which has no app.json"):
        model.load_sources()


def test_real_sources_load():
    """No monkeypatch: the shipped catalog must satisfy its own schema."""
    entries = model.load_sources()
    assert len(entries) >= 20
    assert len({e.slug for e in entries}) == len(entries)


def test_the_cross_file_invariants_still_run_without_jsonschema(catalog, monkeypatch,
                                                               capsys):
    """build/render-ci.sh may run with a python3 and no pip, so the render has
    to work without jsonschema. Layer 1 is skipped and SAYS so; layer 2 is
    not."""
    monkeypatch.setattr(model, "jsonschema", None)
    _write()
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).write_text("services: {}\n")
    with pytest.raises(model.SourceError, match="declares no x-catena env"):
        model.load_sources()
    assert "the JSON Schema layer is skipped" in capsys.readouterr().err


def test_the_real_catalog_renders_without_jsonschema(monkeypatch):
    """The no-pip render end to end: no monkeypatched tree, no jsonschema."""
    monkeypatch.setattr(model, "jsonschema", None)
    assert len(model.load_sources()) >= 20


def _fixture_tree(root: Path) -> None:
    """A one-app tree in another repository's layout."""
    _write(slug="example", doc=None) if False else None
    app_dir = root / "blueprints" / "example"
    _blueprint(root / "blueprints", "example")
    (app_dir / model.CARD_NAME).write_text(json.dumps(_valid_doc()))
    (root / model.CATALOG_META).write_text(json.dumps({
        "order": ["example"],
        "sizing": {"measurement_host": "host", "last_measured_at": "2026-01-01"}}))


def _tools(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a tools entrypoint with TEMPLATES_ROOT at `root`, from outside the
    tools' checkout."""
    env = {**os.environ, "TEMPLATES_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, *args], cwd=root, env=env,
                          capture_output=True, text=True, check=False)


def test_the_data_root_defaults_to_the_tools_checkout():
    assert model.TOOLS_ROOT == ROOT
    if "TEMPLATES_ROOT" not in os.environ:
        assert model.ROOT == ROOT


def test_templates_root_moves_the_data_but_not_the_schemas(tmp_path):
    _fixture_tree(tmp_path)
    code = ("from lib import model; "
            "print(model.ROOT, model.BLUEPRINTS, model.CARD_SCHEMA, "
            "[e.slug for e in model.load_sources()], sep='\\n')")
    done = _tools(tmp_path, "-c", code)
    assert done.returncode == 0, done.stderr
    root, blueprints, schema, slugs = done.stdout.splitlines()
    assert Path(root) == tmp_path.resolve()
    assert Path(blueprints) == tmp_path.resolve() / "blueprints"
    assert Path(schema) == ROOT / "app.schema.json"
    assert slugs == "['example']"


def test_the_render_writes_into_the_tree_templates_root_names(tmp_path):
    _fixture_tree(tmp_path)
    done = _tools(tmp_path, str(ROOT / "build" / "render.py"))
    assert done.returncode == 0, done.stderr
    for name in ("catalog.json", "index.html"):
        assert (tmp_path / name).is_file(), name
    assert not (tmp_path / "templates.json").exists()
    assert not (tmp_path / "blueprints" / "example" / "README.md").exists()
    catalog = json.loads((tmp_path / "catalog.json").read_text())["templates"]
    assert [t["id"] for t in catalog] == ["example"]


def _png(width: int = 256, height: int | None = None, depth: int = 8, colour: int = 6,
         padding: int = 0) -> bytes:
    """A PNG header with the given fields and `padding` bytes after it: all the
    loader reads."""
    height = width if height is None else height
    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + struct.pack(">I", crc) + b"\0" * padding)


def _logo(png: bytes, source: dict | None = None) -> None:
    (model.BLUEPRINTS / "example" / model.LOGO_NAME).write_bytes(png)
    doc = _valid_doc()
    if source is not None:
        doc["logo"] = source
    _write(doc)


def test_a_logo_with_its_source_loads(catalog):
    _logo(_png(), {"source": "https://example.com/logo.svg", "notice": "Example is a mark."})
    assert model.load_sources()[0].catena["logo"]["notice"] == "Example is a mark."
    _logo(_png(128), {"source": "https://example.com/logo.svg"})
    assert model.load_sources()[0].catena["logo"] == {"source": "https://example.com/logo.svg"}


@pytest.mark.parametrize("png,needle", [
    (b"not a png at all, but long enough to hold a header", "is not a PNG"),
    (_png(depth=8, colour=2), "not 8-bit RGBA"),
    (_png(depth=16, colour=6), "not 8-bit RGBA"),
    (_png(256, 200), "not square"),
    (_png(127), "each side must be 128 to 512"),
    (_png(513), "each side must be 128 to 512"),
    (_png(padding=model.LOGO_MAX_BYTES), "the control panel keeps at most"),
])
def test_a_logo_the_panel_would_refuse_is_reported(catalog, png, needle):
    _logo(png, {"source": "https://example.com/logo.svg"})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_a_logo_and_its_source_field_come_together(catalog):
    _logo(_png())
    with pytest.raises(model.SourceError, match="has no logo naming its source"):
        model.load_sources()
    (model.BLUEPRINTS / "example" / model.LOGO_NAME).unlink()
    _logo(_png(), {"source": "https://example.com/logo.svg"})
    (model.BLUEPRINTS / "example" / model.LOGO_NAME).unlink()
    with pytest.raises(model.SourceError, match="logo is set but"):
        model.load_sources()


def test_a_logo_source_holds_no_other_field(catalog):
    _logo(_png(), {"source": "https://example.com/logo.svg", "licence": "MIT"})
    with pytest.raises(model.SourceError, match="licence"):
        model.load_sources()


def _service_labels(labels) -> None:
    path = model.BLUEPRINTS / "example" / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({
        model.BLOCK_KEY: {"schema": 2, "env": ENV},
        "services": {"app": {"labels": labels}}}))


def _routed(labels, block: dict | None = None, slug: str = "example") -> None:
    """The compose of `slug` with one service `app` carrying `labels`."""
    path = model.BLUEPRINTS / slug / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({
        model.BLOCK_KEY: {"schema": 2, "env": ENV, **(block or {})},
        "services": {"app": {"labels": labels}}}, sort_keys=False))


ROUTE = ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=80"]


def test_a_card_file_loads_with_its_web_address_derived_from_the_compose(catalog):
    _write()
    _routed(ROUTE)
    entry = model.load_sources()[0]
    assert (entry.slug, entry.title) == ("example", "Example")
    assert entry.domain == {"host": "x.example.com", "service": "app", "port": 80}
    assert entry.catena["domain"] == entry.domain
    assert "id" not in entry.catena and "schema" not in entry.catena
    assert entry.source_path == "blueprints/example/app.json"
    assert entry.catena["app_name"] == "example"


def test_a_card_over_the_size_the_panel_reads_is_reported(catalog):
    doc = _valid_doc()
    doc["en"]["extra_notes"] = "x" * model.CARD_MAX_BYTES
    _write(doc)
    _routed(ROUTE)
    with pytest.raises(model.SourceError, match=rf"app\.json is \d+ bytes.*at most {model.CARD_MAX_BYTES}"):
        model.load_sources()


def test_a_card_missing_its_long_description_is_reported(catalog):
    doc = _valid_doc()
    del doc["fr"]["compose_description"]
    _write(doc)
    _routed(ROUTE)
    with pytest.raises(model.SourceError, match="compose_description"):
        model.load_sources()


def test_a_card_file_is_checked_against_its_schema(catalog):
    for field, value in (("x-catena", {}), ("domain", {"host": "h", "service": "a", "port": 1}),
                         ("requires_catena", "0.11.0"), ("title", "T"), ("schema", 2),
                         ("sso_mode", "invented-mode")):
        doc = _valid_doc()
        doc[field] = value
        _write(doc)
        with pytest.raises(model.SourceError, match=f"app.json: .*{field}"):
            model.load_sources()


def test_a_card_folder_name_is_a_plain_app_id(catalog):
    _write({**_valid_doc(), "app_name": "bad-name"}, slug="Bad_Name")
    _blueprint(model.BLUEPRINTS, "Bad_Name")
    with pytest.raises(model.SourceError, match="not a plain app id"):
        model.load_sources()


@pytest.mark.parametrize("label", ["vps.homepage.name=Example", "vps.homepage.icon=x.png",
                                   "vps.homepage.hidden=true"])
def test_a_compose_carries_no_homepage_label(catalog, label):
    _write()
    _routed(ROUTE + [label, "vps.homepage.description=x"])
    with pytest.raises(model.SourceError) as err:
        model.load_sources()
    text = str(err.value)
    key = label.split("=")[0]
    assert f"app labels {key}; the card data lives in blueprints/example/app.json" in text
    assert "vps.homepage.description" in text
    _routed(ROUTE + ["vps.display-name=Example"])
    assert [e.slug for e in model.load_sources()] == ["example"]


def test_a_homepage_label_on_any_service_is_refused(catalog):
    _write()
    path = model.BLUEPRINTS / "example" / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({
        model.BLOCK_KEY: {"schema": 2, "env": ENV},
        "services": {"app": {}, "db": {"labels": {"vps.homepage.name": "Other"}}}}))
    with pytest.raises(model.SourceError, match="db labels vps.homepage.name"):
        model.load_sources()


def test_a_cards_logo_field_and_file_come_together(catalog):
    _write()
    (model.BLUEPRINTS / "example" / model.LOGO_NAME).write_bytes(_png())
    with pytest.raises(model.SourceError, match="has no logo naming its source"):
        model.load_sources()
    doc = _valid_doc()
    doc["logo"] = {"source": "https://example.com/logo.svg", "notice": "Example is a mark."}
    _write(doc)
    assert model.load_sources()[0].catena["logo"]["notice"] == "Example is a mark."
    (model.BLUEPRINTS / "example" / model.LOGO_NAME).unlink()
    with pytest.raises(model.SourceError, match="logo is set but"):
        model.load_sources()


def test_requires_catena_comes_from_the_block(catalog):
    _write()
    assert model.load_sources()[0].requires_catena is None
    _block({"schema": 2, "requires_catena": "0.11.5"})
    assert model.load_sources()[0].requires_catena == "0.11.5"
    _block({"schema": 2})
    assert model.load_sources()[0].requires_catena is None


def test_the_block_refuses_a_malformed_requires_catena(catalog):
    _write()
    _block({"schema": 2, "requires_catena": "v0.11"})
    with pytest.raises(model.SourceError, match="requires_catena"):
        model.load_sources()


def _compose_of(labels: dict[str, list[str]], env: dict | None = None) -> dict:
    return {model.BLOCK_KEY: {"schema": 2, "env": env if env is not None else ENV},
            "services": {name: {"labels": value} for name, value in labels.items()}}


@pytest.mark.parametrize("labels,env,expected", [
    # the host names an env key: a setting's default, or a managed expression
    ({"a": ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=8080"]}, None,
     {"host": "x.example.com", "service": "a", "port": 8080}),
    ({"a": ["vps.route.host=${DOMAIN_HOST:-z.example.com}", "vps.route.port=80"]},
     {"DOMAIN_HOST": {"managed": {"expr": "app.{{ cloudflare_zone }}"}}},
     {"host": "app.{{ cloudflare_zone }}", "service": "a", "port": 80}),
    # a literal host, and a host an env key cannot give
    ({"a": ["vps.route.host=app.example.org", "vps.route.port=80"]}, None,
     {"host": "app.example.org", "service": "a", "port": 80}),
    ({"a": ["vps.route.host=${UNKNOWN}", "vps.route.port=80"]}, None,
     {"host": "${UNKNOWN}", "service": "a", "port": 80}),
    # the map form of labels
    ({"a": []}, None, None),
])
def test_the_web_address_is_derived_from_the_main_service(labels, env, expected):
    assert model.derive_domain(_compose_of(labels, env)) == (expected, [])


def test_the_main_address_of_several_is_the_last_one_marked():
    compose = _compose_of({
        "web": ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=80", "vps.route.main=yes"],
        "admin": ["vps.route.host=admin.example.org", "vps.route.port=81"],
        "late": ["vps.route.host=late.example.org", "vps.route.port=82", "vps.route.main=1"]})
    assert model.derive_domain(compose)[0]["service"] == "late"
    compose["services"]["late"]["labels"] = ["vps.route.host=late.example.org", "vps.route.port=82",
                                             "vps.route.main=no"]
    assert model.derive_domain(compose)[0]["service"] == "web"
    compose["services"]["web"]["labels"] = ["vps.route.host=h", "vps.route.port=80"]
    assert model.derive_domain(compose) == (None, [])
    compose["services"]["web"]["labels"] = {"vps.route.host": "h", "vps.route.port": "80",
                                            "vps.route.main": "true"}
    assert model.derive_domain(compose)[0]["service"] == "web"


@pytest.mark.parametrize("port", ["", "http", "0", "70000"])
def test_a_main_service_without_a_port_number_is_reported(catalog, port):
    _write()
    _routed(["vps.route.host=${DOMAIN_HOST}", f"vps.route.port={port}"])
    with pytest.raises(model.SourceError, match="not a port number"):
        model.load_sources()


def test_every_real_compose_gives_a_web_address_or_names_version_from():
    for entry in model.load_sources():
        derived, errors = model.derive_domain(model.compose_doc(entry.compose_path))
        assert errors == []
        assert derived is not None or entry.block.get("version_from"), entry.slug
