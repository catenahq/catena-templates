"""Unit tests for lib/versions.py: version, revision, tag order and the
one-app scope check."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import versions  # noqa: E402


@pytest.mark.parametrize("ref,version", [
    ("nextcloud:31.0.8-apache", "31.0.8"),
    ("outlinewiki/outline:1.10.1", "1.10.1"),
    ("redis:v7.4.11-alpine", "7.4.11"),
    ("ghcr.io/nextcloud-releases/aio-talk:20261014_095932", "20261014_095932"),
    ("app:2.1@sha256:abc", "2.1"),
    ("${APP_IMAGE:-ghcr.io/example/app:3.0.0}", "3.0.0"),
])
def test_the_version_is_the_number_the_image_tag_opens_with(ref, version):
    assert versions.version_of_image(ref) == version


@pytest.mark.parametrize("ref", ["nextcloud", "app:latest", "app:stable-alpine", "app@sha256:abc"])
def test_an_image_without_a_numeric_tag_has_no_version(ref):
    with pytest.raises(versions.VersionError):
        versions.version_of_image(ref)


def _source(service="app"):
    return {"x-catena": {"domain": {"host": "x", "service": service, "port": 80}}}


def _compose(block=None):
    doc = {"services": {
        "app": {"image": "nextcloud:31.0.8-apache"},
        "db": {"image": "postgres:18.6-alpine"},
    }}
    if block:
        doc["x-catena"] = block
    return doc


def test_the_version_comes_from_the_main_address_service():
    assert versions.app_version(_source("app"), _compose()) == "31.0.8"
    assert versions.app_version(_source("db"), _compose()) == "18.6"


def test_version_from_names_the_service_when_the_block_has_one():
    assert versions.app_version(_source("app"), _compose({"version_from": "db"})) == "18.6"


def test_a_service_without_an_image_has_no_version():
    with pytest.raises(versions.VersionError):
        versions.app_version(_source("worker"), _compose())


def test_the_revision_is_one_plus_the_tags_at_that_version():
    tags = [
        "nextcloud/31.0.7-1",
        "nextcloud/31.0.8-1",
        "nextcloud/31.0.8-2",
        "other/31.0.8-1",
        "nextcloud/31.0.80-1",
        "nextcloud/not-a-version",
    ]
    assert versions.next_revision("nextcloud", "31.0.8", tags) == 3
    assert versions.next_revision("nextcloud", "31.0.9", tags) == 1
    assert versions.next_revision("nextcloud", "31.0.8", []) == 1
    assert versions.tag_name("nextcloud", "31.0.8", 3) == "nextcloud/31.0.8-3"


def test_tags_order_by_version_then_revision_not_as_text():
    tags = [
        "app/1.10.0-1",
        "app/1.9.0-2",
        "app/1.9.0-10",
        "app/1.9.0-1",
        "app/2.0.0-1",
        "other/9.9.9-1",
        "app/junk",
    ]
    assert versions.sort_tags("app", tags) == [
        "app/1.9.0-1", "app/1.9.0-2", "app/1.9.0-10", "app/1.10.0-1", "app/2.0.0-1"]


def test_an_app_is_one_app_across_its_blueprint_and_its_source():
    assert versions.scope_errors([
        "blueprints/outline/docker-compose.yml",
        "blueprints/outline/README.md",
        "sources/outline.json",
        "templates.json",
        "catalog.json",
    ]) == []


def test_a_change_to_two_apps_fails_the_scope_check():
    errors = versions.scope_errors(["blueprints/outline/docker-compose.yml", "sources/kimai.json"])
    assert errors == ["the change touches 2 apps (kimai, outline); a pull request changes one app"]


def test_a_change_to_no_app_passes_the_scope_check():
    assert versions.scope_errors(["lib/model.py", "sources/_meta.json"]) == []


def test_next_prints_the_tag_for_an_app_in_the_repo(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("outline/0.0.1-1\n"))
    assert versions.main(["next", "outline"]) == 0
    tag = capsys.readouterr().out.strip()
    assert tag.startswith("outline/") and tag.endswith("-1")
    assert versions.parse_tag("outline", tag) is not None


def test_every_app_in_the_catalog_has_a_readable_version():
    for path in sorted((ROOT / "sources").glob("*.json")):
        if path.name == "_meta.json":
            continue
        app = path.stem
        tag = versions.next_tag(app, [])
        assert versions.parse_tag(app, tag) is not None, tag


def test_scope_command_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("sources/a.json\nsources/b.json\n"))
    assert versions.main(["scope"]) == 1
    assert "touches 2 apps" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("sources/a.json\n"))
    assert versions.main(["scope"]) == 0
