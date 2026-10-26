"""Unit tests for lib/model.py: the schema layer plus the cross-file
invariants a JSON Schema cannot express.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model  # noqa: E402


def _valid_doc(slug: str = "example") -> dict:
    return {
        "id": slug,
        "type": 2,
        "title": "Example",
        "name": slug,
        "categories": ["Testing"],
        "platform": "linux",
        "x-catena": {
            "app_name": slug,
            "upstream_url": "https://example.com",
            "sso_mode": "none",
            "domain": {"host": "x.example.com", "service": "app", "port": 80},
            "bench": {"pack": "nodb"},
            "sizing": {"peak_ram_mb": 256},
            "en": {
                "display_name": "Example",
                "what_it_is": "test",
                "replaces": [],
                "compose_description": "test",
                "setup_steps": "1. none",
            },
            "fr": {
                "display_name": "Example",
                "what_it_is": "test",
                "replaces": [],
                "compose_description": "test",
                "setup_steps": "1. aucune",
            },
        },
    }


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """A sources/ tree plus the blueprint tree beside it. A template is
    two hand-edited files and the loader reads both, so a fixture that
    redirects only one of them tests a layout that cannot exist."""
    src = tmp_path / "sources"
    src.mkdir()
    blueprints = tmp_path / "blueprints"
    _blueprint(blueprints, "example")
    _meta(src, ["example"])
    monkeypatch.setattr(model, "SOURCES", src)
    monkeypatch.setattr(model, "BLUEPRINTS", blueprints)
    return src


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


def _meta(sources: Path, order: list[str]) -> None:
    (sources / model.META_NAME).write_text(
        json.dumps({"order": order})
    )


def _write(sources: Path, doc: dict, name: str | None = None) -> None:
    (sources / f"{name or doc['id']}.json").write_text(json.dumps(doc))


def test_valid_source_loads(sources):
    _write(sources, _valid_doc())
    entries = model.load_sources()
    assert [e.slug for e in entries] == ["example"]
    assert entries[0].sizing["peak_ram_mb"] == 256
    assert entries[0].block == {"schema": 2, "env": ENV}
    assert entries[0].quiesce is None
    assert entries[0].managed_env == {}


def test_meta_prefixed_files_are_not_templates(sources):
    """_meta.json sits in the same directory and must not be loaded as a
    template."""
    _write(sources, _valid_doc())
    assert [e.slug for e in model.load_sources()] == ["example"]


def test_id_must_match_filename(sources):
    _write(sources, _valid_doc("example"), name="something-else")
    with pytest.raises(model.SourceError, match="does not match the filename stem"):
        model.load_sources()


def test_order_drives_the_returned_sequence(sources):
    """The order is curated (hubs first), not alphabetical: it is what
    the Portainer gallery and the generated docs index show."""
    for slug in ("alpha", "omega", "middle"):
        _write(sources, _valid_doc(slug))
        _blueprint(model.BLUEPRINTS, slug)
    _meta(sources, ["omega", "alpha", "middle"])
    assert [e.slug for e in model.load_sources()] == ["omega", "alpha", "middle"]


def test_template_missing_from_order_is_an_error(sources):
    _write(sources, _valid_doc("example"))
    _write(sources, _valid_doc("unlisted"))
    _blueprint(model.BLUEPRINTS, "unlisted")
    with pytest.raises(model.SourceError, match="not listed in _meta.json order"):
        model.load_sources()


def test_order_naming_an_absent_template_is_an_error(sources):
    _write(sources, _valid_doc("example"))
    _meta(sources, ["example", "deleted-template"])
    with pytest.raises(model.SourceError, match="has no source file"):
        model.load_sources()


def test_a_template_without_a_compose_in_its_blueprint_is_an_error(sources):
    """A template's compose is found at blueprints/<id>/docker-compose.yml;
    a template without that file has none, which is an error."""
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).unlink()
    _write(sources, _valid_doc())
    with pytest.raises(model.SourceError, match="does not exist"):
        model.load_sources()


def test_compose_file_is_derived_from_the_id(sources):
    _write(sources, _valid_doc())
    entry = model.load_sources()[0]
    assert entry.compose_file == "blueprints/example/docker-compose.yml"
    assert entry.compose_path == model.BLUEPRINTS / "example" / "docker-compose.yml"


def test_a_compose_without_an_env_is_an_error(sources):
    """The deploy form lists the keys of the block's env; a template with
    none lists nothing."""
    _write(sources, _valid_doc())
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).write_text("services: {}\n")
    with pytest.raises(model.SourceError, match="declares no x-catena env"):
        model.load_sources()


def test_env_defaults_are_the_env_as_key_value_lines(sources):
    """A setting gives its default, a generated secret the lookup expression
    that mints it, a managed key its expression; the order is the block's."""
    _write(sources, _valid_doc())
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
    assert entry.env_label("NAME") == "Name"
    assert entry.env_label("DB_PASSWORD") is None


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
def test_an_env_the_format_refuses_is_reported(sources, env, needle):
    _write(sources, _valid_doc())
    _block({"schema": 2, "env": env})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_the_other_schema_2_fields_are_checked(sources):
    _write(sources, _valid_doc())
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


def test_a_block_names_its_format(sources):
    _write(sources, _valid_doc())
    path = model.BLUEPRINTS / "example" / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({model.BLOCK_KEY: {"env": ENV}, "services": {}}))
    with pytest.raises(model.SourceError, match="'schema' is a required property"):
        model.load_sources()
    _block({"schema": 1})
    with pytest.raises(model.SourceError, match="schema"):
        model.load_sources()


def test_a_block_holds_no_field_the_format_lacks(sources):
    _write(sources, _valid_doc())
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
def test_an_object_store_the_format_refuses_is_reported(sources, object_store, needle):
    _write(sources, _valid_doc())
    _block({"schema": 2, "object_store": object_store})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_a_source_holds_none_of_the_blocks_fields(sources):
    """The block in the compose is the one home of these fields."""
    for name, value in (("quiesce", {}), ("lifecycle", {}),
                        ("object_store", {}), ("env_managed_keys", ["DOMAIN_HOST"])):
        doc = _valid_doc()
        doc["x-catena"][name] = value
        _write(sources, doc)
        with pytest.raises(model.SourceError, match=name):
            model.load_sources()


def test_schema_violation_is_reported(sources):
    doc = _valid_doc()
    doc["x-catena"]["sso_mode"] = "invented-mode"
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="sso_mode"):
        model.load_sources()


def test_sizing_requires_peak_ram(sources):
    """The bench scheduler reads peak_ram_mb to size parallel slots; a
    template without it cannot be scheduled at all."""
    doc = _valid_doc()
    del doc["x-catena"]["sizing"]["peak_ram_mb"]
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="peak_ram_mb"):
        model.load_sources()


def test_quiesce_commands_are_argv_lists(sources):
    """The host runs each command through docker exec with no shell, so a
    shell line is refused at load. post is optional."""
    _write(sources, _valid_doc())
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


def test_quiesce_names_its_service(sources):
    _write(sources, _valid_doc())
    _block({"schema": 2, "quiesce": {"pre": [["php", "occ", "status"]], "timeout_seconds": 10}})
    with pytest.raises(model.SourceError, match="'service' is a required property"):
        model.load_sources()


def test_quiesce_timeout_cap(sources):
    _write(sources, _valid_doc())
    _block({"schema": 2, "quiesce": {
        "service": "app", "pre": [["php", "occ", "status"]], "timeout_seconds": 600}})
    with pytest.raises(model.SourceError, match="timeout_seconds"):
        model.load_sources()


def test_lifecycle_ready_is_one_argv(sources):
    """ready is one command: the update lane asks it until it answers, and
    a list of commands has no single answer to wait for."""
    _write(sources, _valid_doc())
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
def test_lifecycle_versioned_volumes_are_distinct_volume_names(sources, volumes):
    """The host copies `<stack>_<name>` before an update and puts the copy
    back on a rollback, so each entry is one volume name, listed once."""
    _write(sources, _valid_doc())
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


def test_a_lifecycle_declares_a_migration_or_versioned_volumes(sources):
    """An application whose new version upgrades a volume as it starts has
    no migration to declare, only the volumes a rollback puts back. A block
    with neither gives the host nothing to do."""
    _write(sources, _valid_doc())
    lifecycle = {"service": "app", "versioned_volumes": ["data"], "timeout_seconds": 300}
    _block({"schema": 2, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle == lifecycle
    del lifecycle["versioned_volumes"]
    _block({"schema": 2, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="lifecycle"):
        model.load_sources()


def test_empty_sources_directory_is_an_error(sources):
    with pytest.raises(model.SourceError, match="no template files"):
        model.load_sources()


def test_real_sources_load():
    """No monkeypatch: the shipped catalog must satisfy its own schema."""
    entries = model.load_sources()
    assert len(entries) >= 20
    assert len({e.slug for e in entries}) == len(entries)


def test_the_cross_file_invariants_still_run_without_jsonschema(sources, monkeypatch,
                                                               capsys):
    """build/render-ci.sh may run with a python3 and no pip, so the render has
    to work without jsonschema. Layer 1 is skipped and SAYS so; layer 2 is
    not."""
    monkeypatch.setattr(model, "jsonschema", None)
    _write(sources, _valid_doc())
    (model.BLUEPRINTS / "example" / model.COMPOSE_NAME).write_text("services: {}\n")
    with pytest.raises(model.SourceError, match="declares no x-catena env"):
        model.load_sources()
    assert "the JSON Schema layer is skipped" in capsys.readouterr().err


def test_the_real_catalog_renders_without_jsonschema(monkeypatch):
    """The no-pip render end to end: no monkeypatched sources, no jsonschema."""
    monkeypatch.setattr(model, "jsonschema", None)
    assert len(model.load_sources()) >= 20


def _fixture_tree(root: Path) -> None:
    """A one-app tree in another repository's layout."""
    (root / "sources").mkdir()
    (root / "sources" / "example.json").write_text(json.dumps(_valid_doc()))
    (root / "sources" / model.META_NAME).write_text(json.dumps({
        "order": ["example"],
        "sizing": {"measurement_host": "host", "last_measured_at": "2026-01-01"}}))
    _blueprint(root / "blueprints", "example")


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
            "print(model.ROOT, model.SOURCES, model.BLUEPRINTS, model.SOURCE_SCHEMA, "
            "[e.slug for e in model.load_sources()], sep='\\n')")
    done = _tools(tmp_path, "-c", code)
    assert done.returncode == 0, done.stderr
    root, sources, blueprints, schema, slugs = done.stdout.splitlines()
    assert Path(root) == tmp_path.resolve()
    assert Path(sources) == tmp_path.resolve() / "sources"
    assert Path(blueprints) == tmp_path.resolve() / "blueprints"
    assert Path(schema) == ROOT / "sources.schema.json"
    assert slugs == "['example']"


def test_the_render_writes_into_the_tree_templates_root_names(tmp_path):
    _fixture_tree(tmp_path)
    done = _tools(tmp_path, str(ROOT / "build" / "render.py"))
    assert done.returncode == 0, done.stderr
    for name in ("templates.json", "catalog.json", "index.html"):
        assert (tmp_path / name).is_file(), name
    assert (tmp_path / "blueprints" / "example" / "README.md").is_file()
    templates = json.loads((tmp_path / "templates.json").read_text())["templates"]
    assert [t["name"] for t in templates] == ["example"]
