"""Each app pins its own Postgres; the lint holds the pin to one the engine orders."""
from __future__ import annotations

from lib import postgres_pins


def _compose(image: str, rule: str = "") -> str:
    block = ("x-catena:\n  schema: 2\n  images:\n    db:\n      rule: " + rule + "\n") if rule else ""
    return block + f"services:\n  db:\n    image: {image}\n"


def test_a_partial_pin_with_its_rule_passes():
    count, errors = postgres_pins.pin_errors(
        "app", _compose("postgres:18.6-alpine", "{kind: partial_line}"))
    assert (count, errors) == (1, [])


def test_a_partial_pin_with_no_rule_is_reported():
    count, errors = postgres_pins.pin_errors("app", _compose("postgres:18.6-alpine"))
    assert count == 1
    assert len(errors) == 1 and "images.db.rule" in errors[0]


def test_a_full_version_needs_no_rule():
    assert postgres_pins.pin_errors("app", _compose("postgres:18.6.1-alpine")) == (1, [])


def test_each_app_may_pin_a_different_major():
    for tag in ("16.4-alpine", "18.6-alpine"):
        _, errors = postgres_pins.pin_errors(
            "app", _compose(f"postgres:{tag}", "{kind: partial_line}"))
        assert errors == []


def test_other_images_are_not_postgres_pins():
    body = _compose("ghcr.io/acme/postgres:14-x") + "  cache:\n    image: redis:8.6.7-alpine3.23\n"
    assert postgres_pins.pin_errors("app", body) == (0, [])
