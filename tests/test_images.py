"""Unit tests for lib/images.py: what a pull request's CVE gate scans."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.images import changed_images, service_images  # noqa: E402


def compose(**images: str) -> str:
    return "services:\n" + "".join(
        f"  {name}:\n    image: {image}\n" for name, image in images.items())


def test_service_images_reads_every_service():
    assert service_images(compose(app="nginx:1.31.5-alpine", db="postgres:18.6-alpine")) == {
        "app": "nginx:1.31.5-alpine", "db": "postgres:18.6-alpine"}


def test_a_moved_pin_pairs_with_the_one_it_replaces():
    got = changed_images({"x": compose(app="nginx:1.31.6-alpine", db="postgres:18.6-alpine")},
                         {"x": compose(app="nginx:1.31.5-alpine", db="postgres:18.6-alpine")})
    assert got == [{"image": "nginx:1.31.6-alpine", "base_image": "nginx:1.31.5-alpine"}]


def test_an_unchanged_catalog_scans_nothing():
    same = {"x": compose(app="nginx:1.31.5-alpine")}
    assert changed_images(same, same) == []


def test_a_new_service_or_template_has_nothing_to_compare_against():
    got = changed_images({"x": compose(app="a:1.0.0", cache="redis:8.6.3-alpine"),
                          "y": compose(web="b:2.0.0")},
                         {"x": compose(app="a:1.0.0")})
    assert got == [{"image": "b:2.0.0", "base_image": ""},
                   {"image": "redis:8.6.3-alpine", "base_image": ""}]


def test_one_bump_shared_by_several_services_is_one_pair():
    got = changed_images({"x": compose(rails="c:4.18.0", sidekiq="c:4.18.0")},
                         {"x": compose(rails="c:4.17.1", sidekiq="c:4.17.1")})
    assert got == [{"image": "c:4.18.0", "base_image": "c:4.17.1"}]
