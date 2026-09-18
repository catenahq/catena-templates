"""Unit tests for lib/model.py: the schema layer plus the cross-file
invariants a JSON Schema cannot express.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    assert entries[0].quiesce is None


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
    """The compose is not named by the source file any more: it is found
    at blueprints/<id>/docker-compose.yml or the template has none."""
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
    doc = _valid_doc()
    doc["x-catena"]["env_managed_keys"] = ["NOT_DECLARED"]
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="not in env_defaults"):
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


def test_quiesce_half_pair_is_rejected(sources):
    doc = _valid_doc()
    doc["x-catena"]["quiesce"] = {"pre": "true", "timeout_seconds": 10}
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="post"):
        model.load_sources()


def test_quiesce_timeout_cap(sources):
    doc = _valid_doc()
    doc["x-catena"]["quiesce"] = {"pre": "true", "post": "true", "timeout_seconds": 600}
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="timeout_seconds"):
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
    """Renovate's container has a python3 and no pip, so the render has to work
    without jsonschema. Layer 1 is skipped and SAYS so; layer 2 is not."""
    monkeypatch.setattr(model, "jsonschema", None)
    doc = _valid_doc()
    doc["x-catena"]["env_managed_keys"] = ["NOT_IN_DEFAULTS"]
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="env_managed_keys"):
        model.load_sources()
    assert "the JSON Schema layer is skipped" in capsys.readouterr().err


def test_the_real_catalog_renders_without_jsonschema(monkeypatch):
    """The Renovate path end to end: no monkeypatched sources, no jsonschema."""
    monkeypatch.setattr(model, "jsonschema", None)
    assert len(model.load_sources()) >= 20
