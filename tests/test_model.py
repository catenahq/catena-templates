"""Unit tests for lib/model.py: the schema layer plus the cross-file
invariants a JSON Schema cannot express.
"""
from __future__ import annotations

import json
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
            "env_defaults": ["DOMAIN_HOST=x.example.com", "DB_PASSWORD=secret"],
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


def _blueprint(blueprints: Path, slug: str) -> None:
    app_dir = blueprints / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / model.COMPOSE_NAME).write_text("services: {}\n")


def _block(block: dict, slug: str = "example") -> None:
    """Put `block` at the top of the blueprint's compose as its x-catena block."""
    path = model.BLUEPRINTS / slug / model.COMPOSE_NAME
    path.write_text(yaml.safe_dump({model.BLOCK_KEY: block, "services": {}}))


def _meta(sources: Path, order: list[str]) -> None:
    (sources / model.META_NAME).write_text(
        json.dumps({"order": order, "postgres_default_image": "postgres:18.4-alpine"})
    )


def _write(sources: Path, doc: dict, name: str | None = None) -> None:
    (sources / f"{name or doc['id']}.json").write_text(json.dumps(doc))


def test_valid_source_loads(sources):
    _write(sources, _valid_doc())
    entries = model.load_sources()
    assert [e.slug for e in entries] == ["example"]
    assert entries[0].sizing["peak_ram_mb"] == 256
    assert entries[0].block == {}
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


def test_env_managed_key_must_exist_in_env_defaults(sources):
    """A managed key that names nothing renders nowhere, so the converge
    would silently never inject it."""
    _write(sources, _valid_doc())
    _block({"schema": 1, "managed_env": {"NOT_DECLARED": "x"}})
    with pytest.raises(model.SourceError, match="not in env_defaults"):
        model.load_sources()


def test_managed_env_gives_the_value_and_env_defaults_keeps_the_place(sources):
    """The source lists a managed key with no value, so the deploy form keeps
    its order; the block holds the expression, and the entry joins them."""
    doc = _valid_doc()
    doc["x-catena"]["env_defaults"] = ["DOMAIN_HOST=", "DB_PASSWORD=secret"]
    _write(sources, doc)
    _block({"schema": 1, "managed_env": {"DOMAIN_HOST": "x.{{ cloudflare_zone }}"}})
    entry = model.load_sources()[0]
    assert entry.env_defaults == ["DOMAIN_HOST=x.{{ cloudflare_zone }}", "DB_PASSWORD=secret"]
    doc["x-catena"]["env_defaults"] = ["DOMAIN_HOST=x.example.com", "DB_PASSWORD=secret"]
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="gives the managed key 'DOMAIN_HOST' a value"):
        model.load_sources()


def test_a_block_names_its_format(sources):
    _write(sources, _valid_doc())
    _block({"managed_env": {"DOMAIN_HOST": "x"}})
    with pytest.raises(model.SourceError, match="'schema' is a required property"):
        model.load_sources()
    _block({"schema": 2})
    with pytest.raises(model.SourceError, match="schema"):
        model.load_sources()


def test_a_block_holds_no_field_the_format_lacks(sources):
    _write(sources, _valid_doc())
    _block({"schema": 1, "pause_during_migrate_cutover": True})
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
    _block({"schema": 1, "object_store": object_store})
    with pytest.raises(model.SourceError, match=needle):
        model.load_sources()


def test_a_source_holds_none_of_the_blocks_fields(sources):
    """The block in the compose is the one home of these fields."""
    for name, value in (("quiesce", {}), ("lifecycle", {}), ("s3_reconcile", {}),
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
    _block({"schema": 1, "quiesce": quiesce})
    with pytest.raises(model.SourceError, match="quiesce/pre"):
        model.load_sources()
    quiesce["pre"] = [["php", "occ", "maintenance:mode", "--on"]]
    _block({"schema": 1, "quiesce": quiesce})
    assert model.load_sources()[0].quiesce["pre"] == [
        ["php", "occ", "maintenance:mode", "--on"]]


def test_quiesce_names_its_service(sources):
    _write(sources, _valid_doc())
    _block({"schema": 1, "quiesce": {"pre": [["php", "occ", "status"]], "timeout_seconds": 10}})
    with pytest.raises(model.SourceError, match="'service' is a required property"):
        model.load_sources()


def test_quiesce_timeout_cap(sources):
    _write(sources, _valid_doc())
    _block({"schema": 1, "quiesce": {
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
    _block({"schema": 1, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="ready"):
        model.load_sources()
    lifecycle["ready"] = ["php", "occ", "status"]
    _block({"schema": 1, "lifecycle": lifecycle})
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
    _block({"schema": 1, "lifecycle": lifecycle})
    with pytest.raises(model.SourceError, match="versioned_volumes"):
        model.load_sources()
    lifecycle["versioned_volumes"] = ["code", "conf.d"]
    _block({"schema": 1, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle["versioned_volumes"] == ["code", "conf.d"]


def test_a_lifecycle_declares_a_migration_or_versioned_volumes(sources):
    """An application whose new version upgrades a volume as it starts has
    no migration to declare, only the volumes a rollback puts back. A block
    with neither gives the host nothing to do."""
    _write(sources, _valid_doc())
    lifecycle = {"service": "app", "versioned_volumes": ["data"], "timeout_seconds": 300}
    _block({"schema": 1, "lifecycle": lifecycle})
    assert model.load_sources()[0].lifecycle == lifecycle
    del lifecycle["versioned_volumes"]
    _block({"schema": 1, "lifecycle": lifecycle})
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
    _block({"schema": 1, "managed_env": {"NOT_IN_DEFAULTS": "x"}})
    with pytest.raises(model.SourceError, match="managed_env"):
        model.load_sources()
    assert "the JSON Schema layer is skipped" in capsys.readouterr().err


def test_the_real_catalog_renders_without_jsonschema(monkeypatch):
    """The no-pip render end to end: no monkeypatched sources, no jsonschema."""
    monkeypatch.setattr(model, "jsonschema", None)
    assert len(model.load_sources()) >= 20
