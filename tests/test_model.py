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
        "type": 3,
        "title": "Example",
        "name": slug,
        "categories": ["Testing"],
        "platform": "linux",
        "x-catena": {
            "app_name": slug,
            "upstream_url": "https://example.com",
            "sso_mode": "none",
            "domain": {"host": "x.example.com", "service": "app", "port": 80},
            "compose_file": "compose/example.compose.yml",
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
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "example.compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(model, "SOURCES", tmp_path)
    return tmp_path


def _write(sources: Path, doc: dict, name: str | None = None) -> None:
    (sources / f"{name or doc['id']}.json").write_text(json.dumps(doc))


def test_valid_source_loads(sources):
    _write(sources, _valid_doc())
    entries = model.load_sources()
    assert [e.slug for e in entries] == ["example"]
    assert entries[0].sizing["peak_ram_mb"] == 256
    assert entries[0].quiesce is None


def test_meta_prefixed_files_are_not_templates(sources):
    _write(sources, _valid_doc())
    (sources / "_meta.json").write_text(json.dumps({"postgres_default_image": "postgres:18"}))
    assert [e.slug for e in model.load_sources()] == ["example"]


def test_id_must_match_filename(sources):
    _write(sources, _valid_doc("example"), name="something-else")
    with pytest.raises(model.SourceError, match="does not match the filename stem"):
        model.load_sources()


def test_missing_compose_file_is_an_error(sources):
    doc = _valid_doc()
    doc["x-catena"]["compose_file"] = "compose/absent.compose.yml"
    _write(sources, doc)
    with pytest.raises(model.SourceError, match="does not exist"):
        model.load_sources()


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
