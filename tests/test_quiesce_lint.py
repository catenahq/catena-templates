"""Unit tests for lib/quiesce_lint.py.

Most argv cases drive lint_migrate_argv directly. The lint_all cases run
the gate over the real sources/ tree, or over a synthetic one to prove it
reads sources rather than a cached artifact.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint as L  # noqa: E402


def _quiesce(argv: list[str]) -> list[str]:
    return L.lint_migrate_argv(argv, label="x", allowed=L.ALLOWED_QUIESCE_COMMANDS)


def _migrate(argv: list[str]) -> list[str]:
    return L.lint_migrate_argv(argv, label="x", allowed=L.ALLOWED_MIGRATE_COMMANDS)


def test_allowed_quiesce_commands_lock():
    """Widening the allowlist must be deliberate: these commands run
    inside an application on every client host around every nightly
    backup."""
    assert L.ALLOWED_QUIESCE_COMMANDS == frozenset({"php", "mongosh", "pkill"})


def test_quiesce_argv_allows_the_declared_shapes():
    assert _quiesce(["php", "occ", "maintenance:mode", "--on"]) == []
    assert _quiesce(["mongosh", "--quiet", "--eval", "db.fsyncLock()"]) == []
    assert _quiesce(["pkill", "-STOP", "-x", "node"]) == []


def test_quiesce_argv_rejects_a_hostile_command():
    errs = _quiesce(["curl", "https://evil.example.com"])
    assert any("non-allowlisted" in e and "curl" in e for e in errs)


def test_quiesce_argv_rejects_shell_operators():
    """Quiesce commands run through docker exec with no shell, like
    lifecycle commands: an `&&` reaches the application as a literal
    argument."""
    errs = _quiesce(["php", "occ", "maintenance:mode", "--on", "&&", "curl", "x"])
    assert any("docker exec with no shell" in e for e in errs)


def test_allowed_migrate_commands_lock():
    """Same reasoning as the quiesce allowlist: a lifecycle command runs
    unattended inside an application around an update or after a forward
    restore, so widening this is a review decision."""
    assert L.ALLOWED_MIGRATE_COMMANDS == frozenset({
        "php", "occ", "yarn", "npm", "npx", "bench",
    })


def test_migrate_argv_allows_the_declared_shapes():
    assert _migrate(["php", "occ", "db:add-missing-indices"]) == []
    assert _migrate(["yarn", "db:migrate", "--env", "production-ssl-disabled"]) == []
    assert _migrate(["bench", "--site", "all", "migrate"]) == []


def test_migrate_argv_rejects_a_foreign_command():
    errs = _migrate(["curl", "https://evil.example.com"])
    assert any("non-allowlisted" in e and "curl" in e for e in errs)


def test_migrate_argv_rejects_shell_operators():
    """An argv is not a shell line. `&&` written here reaches the
    application as a literal argument and the second half never runs,
    which is the silent failure this refuses."""
    errs = _migrate(["php", "occ", "upgrade", "&&", "php", "occ", "maintenance:repair"])
    assert any("docker exec with no shell" in e for e in errs)


def test_the_families_do_not_share_an_allowlist():
    assert _migrate(["mongosh", "--eval", "db.fsyncLock()"])
    assert _quiesce(["bench", "--site", "all", "migrate"])


def test_lifecycle_timeout_cap_lives_in_the_schema():
    schema = json.loads((ROOT / "sources.schema.json").read_text())
    lifecycle = schema["properties"]["x-catena"]["properties"]["lifecycle"]
    assert lifecycle["properties"]["timeout_seconds"]["maximum"] == 3600
    assert lifecycle["required"] == ["service", "migrate", "timeout_seconds"]


def test_quiesce_timeout_cap_lives_in_the_schema():
    """The cap lives in sources.schema.json and nowhere else, so it is
    enforced on every load rather than only by the lint entrypoint."""
    schema = json.loads((ROOT / "sources.schema.json").read_text())
    quiesce = schema["properties"]["x-catena"]["properties"]["quiesce"]
    assert quiesce["properties"]["timeout_seconds"]["maximum"] == 60
    assert quiesce["required"] == ["service", "pre", "timeout_seconds"]


def test_lint_all_against_real_sources_passes():
    assert L.lint_all() == 0


def test_lint_all_rejects_a_hostile_quiesce_command(monkeypatch, tmp_path, capsys):
    """A synthetic sources/ tree with a hostile command must fail. Proves
    the gate is reading sources, not a cached artifact."""
    _synthetic_sources(monkeypatch, tmp_path, {
        "quiesce": {
            "service": "app",
            "pre": [["curl", "https://evil.example.com"]],
            "timeout_seconds": 30,
        },
    })
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert "synthetic-bad.quiesce.pre[0]: starts with non-allowlisted command 'curl'" in out
    assert "quiesce.service" not in out


def test_lint_all_rejects_a_service_the_compose_does_not_define(
        monkeypatch, tmp_path, capsys):
    """The host finds the container by the service name. A name the
    compose does not define fails the quiesce on every host, and the
    backup runs without it."""
    _synthetic_sources(monkeypatch, tmp_path, {
        "quiesce": {
            "service": "db",
            "pre": [["mongosh", "--quiet", "--eval", "db.fsyncLock()"]],
            "timeout_seconds": 30,
        },
    })
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert "synthetic-bad.quiesce.service: 'db' is not a service" in out


def test_lint_all_rejects_a_foreign_ready_command(monkeypatch, tmp_path, capsys):
    """The ready question runs inside the application container before
    every update phase, so it gets the lifecycle allowlist too."""
    _synthetic_sources(monkeypatch, tmp_path, {
        "lifecycle": {
            "service": "app",
            "ready": ["curl", "https://evil.example.com"],
            "migrate": [["php", "occ", "db:add-missing-indices"]],
            "timeout_seconds": 60,
        },
    })
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert "synthetic-bad.lifecycle.ready: starts with non-allowlisted" in out


_VERSIONED_COMPOSE = """\
services:
  app:
    image: acme/app:1.0.0
    volumes: ["code:/var/www/html", "data:/var/www/html/data"]
  cron:
    image: acme/app:1.0.0
    volumes:
      - type: volume
        source: code
        target: /var/www/html
  db:
    image: postgres:18.6-alpine
    volumes: ["db-data:/var/lib/postgresql/data"]
volumes:
  code:
  data:
  db-data:
"""


def _versioned(volumes: list[str]) -> dict:
    return {"lifecycle": {
        "service": "app",
        "migrate": [["php", "occ", "db:add-missing-indices"]],
        "versioned_volumes": volumes,
        "timeout_seconds": 60,
    }}


def test_a_versioned_volume_long_running_services_mount_passes(
        monkeypatch, tmp_path, capsys):
    """Both compose forms count as a mount: the short `name:/path` and the
    long `source:`."""
    _synthetic_sources(monkeypatch, tmp_path, _versioned(["code"]), _VERSIONED_COMPOSE)
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("volume,needle", [
    ("db-data", "is mounted by db, a database"),
    ("cache", "is not a top-level volume"),
    ("spare", "is mounted by no service"),
])
def test_a_versioned_volume_a_rollback_cannot_put_back_is_refused(
        monkeypatch, tmp_path, capsys, volume, needle):
    """A rollback scales the services that mount the volume to zero before
    it puts the copy back; a database is never stopped (its dump is
    replayed), and a volume the compose does not declare or no service
    mounts has nothing a copy could restore."""
    compose = _VERSIONED_COMPOSE + "  spare:\n"
    compose = compose.replace("    volumes: [\"code:/var/www/html\", ",
                              "    volumes: [\"cache:/tmp/cache\", \"code:/var/www/html\", ")
    _synthetic_sources(monkeypatch, tmp_path, _versioned([volume]), compose)
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert f"synthetic-bad.lifecycle.versioned_volumes: {volume!r} {needle}" in out


_SIGNIN_COMPOSE = """\
services:
  app:
    image: acme/app:1.0.0
    labels:
      - "vps.route.host=${DOMAIN_HOST}"
      - "vps.auth.mode=public"
      - "vps.auth.oidc=true"
      - "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"
  worker:
    image: acme/app:1.0.0
"""


def test_a_template_that_asks_for_a_sign_in_entry_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _SIGNIN_COMPOSE)
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("key", ["OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"])
def test_a_sign_in_value_the_sync_writes_has_no_second_writer(
        monkeypatch, tmp_path, capsys, key):
    """The settings sync writes the three values from the app's own
    Keycloak client; a catalog default or a managed key would write the
    same key from somewhere else."""
    _synthetic_sources(monkeypatch, tmp_path, {
        "env_defaults": ["DOMAIN_HOST=x.example.com", f"{key}=from-the-catalog"],
        "env_managed_keys": [key],
    }, _SIGNIN_COMPOSE)
    assert L.lint_all() == 1
    assert f"synthetic-bad: {key} is declared in env_defaults" in capsys.readouterr().out


@pytest.mark.parametrize("edit,needle", [
    (('      - "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"\n', ""),
     "synthetic-bad:app: vps.auth.oidc=true without vps.auth.oidc.redirect_uris"),
    (('      - "vps.auth.oidc=true"\n', '      - "vps.auth.oidc=true"\n'
      '      - "vps.auth.oidc.scopes=openid email"\n'),
     "synthetic-bad:app: vps.auth.oidc.scopes is not a sign-in label the host reads"),
])
def test_a_sign_in_label_the_host_cannot_use_is_refused(
        monkeypatch, tmp_path, capsys, edit, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, _SIGNIN_COMPOSE.replace(*edit))
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def test_sign_in_labels_off_the_route_service_are_refused(monkeypatch, tmp_path, capsys):
    """The host reads sign-in labels from the route service alone, so labels
    on another service ask for nothing."""
    compose = _SIGNIN_COMPOSE.replace(
        '      - "vps.auth.oidc=true"\n'
        '      - "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"\n', ""
    ) + ('    labels:\n      - "vps.auth.oidc=true"\n'
         '      - "vps.auth.oidc.redirect_uris=https://x.example.com/cb"\n')
    _synthetic_sources(monkeypatch, tmp_path, {}, compose)
    assert L.lint_all() == 1
    assert ("synthetic-bad:worker: carries sign-in labels but no vps.route.host"
            in capsys.readouterr().out)


def test_the_database_match_reads_the_image_name_only():
    assert L.database_image("postgres:18.6-alpine")
    assert L.database_image("ghcr.io/immich-app/postgres:14-vectorchord0.4.3")
    assert L.database_image("mariadb:11.8.9@sha256:" + "0" * 64)
    assert not L.database_image("nextcloud:34.0.0-apache")
    assert not L.database_image("localhost:5000/acme/app:mysql-client")


def _synthetic_sources(monkeypatch, tmp_path, catena: dict,
                       compose: str = "services:\n  app:\n    image: x\n") -> None:
    """Point the loader at a one-template sources/ tree whose compose is
    `compose` (one service, `app`, by default) and whose x-catena block
    carries `catena` over the required fields."""
    sources = tmp_path / "sources"
    sources.mkdir()
    blueprint = tmp_path / "blueprints" / "synthetic-bad"
    blueprint.mkdir(parents=True)
    (blueprint / model.COMPOSE_NAME).write_text(compose)
    doc = {
        "id": "synthetic-bad",
        "type": 2,
        "title": "Synthetic",
        "name": "synthetic-bad",
        "categories": ["Testing"],
        "platform": "linux",
        "x-catena": {
            "app_name": "synthetic-bad",
            "upstream_url": "https://example.com",
            "sso_mode": "none",
            "domain": {"host": "x.example.com", "service": "app", "port": 80},
            "env_defaults": ["DOMAIN_HOST=x.example.com"],
            "bench": {"pack": "nodb"},
            "sizing": {"peak_ram_mb": 128},
            "en": {
                "display_name": "Synthetic",
                "what_it_is": "test",
                "replaces": [],
                "compose_description": "test",
                "setup_steps": "1. none",
            },
            "fr": {
                "display_name": "Synthetic",
                "what_it_is": "test",
                "replaces": [],
                "compose_description": "test",
                "setup_steps": "1. aucune",
            },
            **catena,
        },
    }
    (sources / "synthetic-bad.json").write_text(json.dumps(doc))
    (sources / model.META_NAME).write_text(
        json.dumps({"order": ["synthetic-bad"], "postgres_default_image": "postgres:18.4-alpine"})
    )
    monkeypatch.setattr(model, "SOURCES", sources)
    monkeypatch.setattr(model, "BLUEPRINTS", tmp_path / "blueprints")
