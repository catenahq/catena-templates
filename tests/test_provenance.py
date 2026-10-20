"""Unit tests for lib/provenance.py, lib/changes.py and lib/app_checks.py,
answered from recorded API responses in tests/fixtures/provenance."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import app_checks, changes, provenance  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provenance"
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
GH = provenance.GITHUB_API
HUB = provenance.HUB_API


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fetch(pages: dict[str, str]):
    """A fetch answering each url from its fixture; any other url is a 404."""
    def fetch(url: str):
        name = pages.get(url)
        return _fixture(name) if name else None
    return fetch


GITHUB_OK = {
    f"{GH}/repos/example/app": "github_repo.json",
    f"{GH}/repos/example/app/releases?per_page=100": "github_releases.json",
}
HUB_OK = {
    f"{HUB}/repositories/library/redis/": "hub_repo.json",
    f"{HUB}/repositories/library/redis/tags?page_size=100": "hub_tags.json",
}
LABELS = {provenance.SOURCE_LABEL: "https://github.com/example/app"}


def test_a_github_source_old_enough_with_three_releases_passes():
    assert provenance.check_image("ghcr.io/example/app:3.0.0", LABELS, _fetch(GITHUB_OK), NOW) is None


def test_a_draft_release_does_not_count():
    pages = {**GITHUB_OK, f"{GH}/repos/example/app/releases?per_page=100": "github_releases_few.json"}
    failure = provenance.check_image("ghcr.io/example/app:1", LABELS, _fetch(pages), NOW)
    assert failure.startswith("ghcr.io/example/app:1: ")
    assert "1 release(s), needs 3" in failure


def test_a_source_under_a_year_old_fails_naming_the_image():
    pages = {**GITHUB_OK, f"{GH}/repos/example/app": "github_young.json"}
    failure = provenance.check_image("ghcr.io/example/app:3", LABELS, _fetch(pages), NOW)
    assert failure == "ghcr.io/example/app:3: example/app is under a year old"


def test_a_label_naming_a_missing_repository_fails():
    failure = provenance.check_image("ghcr.io/example/app:3", LABELS, _fetch({}), NOW)
    assert "GitHub has no repository example/app" in failure


@pytest.mark.parametrize("source", [
    "https://github.com/example/app",
    "https://github.com/example/app.git",
    "https://github.com/example/app/tree/main/docker",
    "git@github.com:example/app.git",
])
def test_github_addresses_read_as_owner_and_repo(source):
    assert provenance.github_repo(source) == ("example", "app")


def test_other_addresses_are_not_github():
    assert provenance.github_repo("https://gitlab.com/example/app") is None
    assert provenance.github_repo("") is None


def test_an_official_docker_hub_image_without_a_label_is_judged_on_docker_hub():
    assert provenance.check_image("redis:7.4", None, _fetch(HUB_OK), NOW) is None


def test_a_namespaced_docker_hub_image_reads_its_own_repository():
    pages = {
        f"{HUB}/repositories/someone/tiny/": "hub_young.json",
        f"{HUB}/repositories/someone/tiny/tags?page_size=100": "hub_tags_few.json",
    }
    failure = provenance.check_image("someone/tiny:1", {}, _fetch(pages), NOW)
    assert failure == "someone/tiny:1: Docker Hub repository someone/tiny was registered under a year ago"


def test_docker_hub_needs_three_pushed_tags():
    pages = {**HUB_OK, f"{HUB}/repositories/library/redis/tags?page_size=100": "hub_tags_few.json"}
    failure = provenance.check_image("redis:7", None, _fetch(pages), NOW)
    assert "1 pushed tag(s), needs 3" in failure


def test_ghcr_without_a_label_fails_naming_the_image():
    failure = provenance.check_image("ghcr.io/example/app:3", {}, _fetch(GITHUB_OK), NOW)
    assert failure.startswith("ghcr.io/example/app:3: ")
    assert "ghcr.io exposes no dates" in failure


def test_a_label_wins_over_docker_hub():
    pages = {**GITHUB_OK}
    assert provenance.check_image("redis:7", LABELS, _fetch(pages), NOW) is None


def test_an_unreadable_page_fails_the_image_instead_of_crashing():
    def broken(url: str):
        raise provenance.ProvenanceError(f"{url} answered HTTP 503")
    failure = provenance.check_image("redis:7", None, broken, NOW)
    assert failure.startswith("redis:7: ") and "HTTP 503" in failure


@pytest.mark.parametrize("ref,expected", [
    ("redis:7", ("docker.io", "library/redis")),
    ("bitnami/redis:7@sha256:abc", ("docker.io", "bitnami/redis")),
    ("ghcr.io/example/app:3", ("ghcr.io", "example/app")),
    ("quay.io/a/b/c", ("quay.io", "a/b/c")),
    ("registry:5000/app:1", ("registry:5000", "app")),
    ("docker.io/library/nginx", ("docker.io", "library/nginx")),
])
def test_split_ref(ref, expected):
    assert provenance.split_ref(ref) == expected


def test_compose_images_reads_defaults_and_flags_expressions():
    body = (
        "services:\n"
        "  a:\n    image: redis:7.4\n"
        "  b:\n    image: \"${APP_IMAGE:-ghcr.io/example/app:3.0.0}\"\n"
        "  c:\n    image: ${OTHER_IMAGE}\n"
        "  d:\n    image: redis:7.4  # same as a\n"
    )
    refs, unresolved = provenance.compose_images(body)
    assert refs == ["redis:7.4", "ghcr.io/example/app:3.0.0"]
    assert unresolved == ["${OTHER_IMAGE}"]


def test_provenance_errors_checks_every_image_of_an_app(tmp_path, monkeypatch):
    (tmp_path / "blueprints" / "demo").mkdir(parents=True)
    (tmp_path / "blueprints" / "demo" / "docker-compose.yml").write_text(
        "services:\n  a:\n    image: redis:7.4\n  b:\n    image: ghcr.io/example/app:3\n")
    monkeypatch.setattr(app_checks, "ROOT", tmp_path)
    errors = app_checks.provenance_errors(
        "demo", labels_of=lambda ref: {}, fetch=_fetch({**HUB_OK, **GITHUB_OK}))
    assert len(errors) == 1
    assert errors[0].startswith("demo: ghcr.io/example/app:3: ")


def _source(licence=None) -> dict:
    origin = {"licence": licence} if licence else {}
    return {"id": "demo", "x-catena": {"origin": origin} if origin else {}}


def test_a_new_app_must_name_a_permitted_licence():
    assert app_checks.licence_errors("demo", _source("MIT"), new=True) == []
    assert "names no licence" in app_checks.licence_errors("demo", _source(), new=True)[0]


def test_an_existing_app_without_a_licence_passes_but_a_bad_one_fails():
    assert app_checks.licence_errors("demo", _source(), new=False) == []
    assert "GPL-3.0" in app_checks.licence_errors("demo", _source("GPL-3.0"), new=False)[0]


def test_the_permitted_licences_are_the_importers_list():
    assert app_checks.permitted_licences() == ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"]


def test_changed_paths_reduce_to_app_ids():
    paths = [
        "blueprints/nextcloud-s3-oidc/docker-compose.yml",
        "blueprints/nextcloud-s3-oidc/README.md",
        "sources/nextcloud-s3-oidc.json",
        "sources/_meta.json",
        "templates.json",
        "lib/model.py",
        "blueprints/outline/logo.png",
        "blueprints/stray.txt",
        "",
    ]
    assert changes.app_ids(paths) == ["nextcloud-s3-oidc", "outline"]


def test_an_id_that_is_not_a_plain_slug_is_refused(monkeypatch, capsys):
    ids = changes.app_ids(["blueprints/a b$(x)/docker-compose.yml", "sources/ok-app.json"])
    assert changes.slug_errors(ids) == [
        "'a b$(x)' is not a plain app id (lowercase letters, digits, hyphens)"]
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("blueprints/Bad_Id/x.yml\n"))
    assert app_checks.main(["changed"]) == 1
    assert "Bad_Id" in capsys.readouterr().err
