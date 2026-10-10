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
    assert _quiesce(["pkill", "-CONT", "-x", "apache2"]) == []


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
    assert lifecycle["required"] == ["service", "timeout_seconds"]
    assert lifecycle["anyOf"] == [{"required": ["migrate"]},
                                  {"required": ["versioned_volumes"]}]


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


_SIGNIN = ["vps.auth.oidc=true", "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"]
_HOST = "vps.route.host=${DOMAIN_HOST}"
_ADMIN_HOST = "vps.route.host=admin.${DOMAIN_HOST}"
_MAIN = "vps.route.main=true"
_OFF_MAIN = ("carries sign-in labels, but the host reads them only from the main "
             "address's service, ")


def _compose(services: dict[str, list[str]]) -> str:
    lines = ["services:"]
    for name, labels in services.items():
        lines += [f"  {name}:", "    image: acme/app:1.0.0"]
        if labels:
            lines += ["    labels:"] + [f'      - "{label}"' for label in labels]
    return "\n".join(lines) + "\n"


def test_sign_in_labels_on_the_marked_main_address_pass(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _compose({
        "app": [_HOST, _MAIN, "vps.auth.mode=public", *_SIGNIN],
        "admin": [_ADMIN_HOST, "vps.auth.mode=admin-only"],
    }))
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("services,needle", [
    ({"app": [_HOST], "worker": _SIGNIN}, "synthetic-bad:worker: " + _OFF_MAIN + "app"),
    ({"app": [_HOST, _MAIN], "admin": [_ADMIN_HOST, *_SIGNIN]},
     "synthetic-bad:admin: " + _OFF_MAIN + "app"),
    ({"app": [_HOST, *_SIGNIN], "admin": [_ADMIN_HOST]},
     "synthetic-bad:app: " + _OFF_MAIN + "and the app has none: 0 of its addresses "
     "(admin, app) carry vps.route.main=true"),
    ({"app": [_HOST, _MAIN, *_SIGNIN], "admin": [_ADMIN_HOST, _MAIN]},
     "synthetic-bad:app: " + _OFF_MAIN + "and the app has none: 2 of its addresses"),
    ({"app": _SIGNIN}, "synthetic-bad:app: " + _OFF_MAIN + "and the app has none: "
     "no service carries vps.route.host"),
])
def test_sign_in_labels_off_the_main_address_are_refused(
        monkeypatch, tmp_path, capsys, services, needle):
    """The host reads sign-in labels from the main address's service alone:
    the one address, or of several the one marked vps.route.main=true. Labels
    anywhere else, or in an app with no main address, ask for nothing."""
    _synthetic_sources(monkeypatch, tmp_path, {}, _compose(services))
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def _imported(findings: list[str]) -> dict:
    return {
        "status": "imported",
        "origin": {"registry": "https://example.com/templates.json", "entry": "Synthetic",
                   "source": "https://github.com/example/app", "licence": "MIT",
                   "attribution": "Copyright (c) 2025 Example"},
        "pending": {"findings": findings, "to_choose": []},
    }


def test_an_imported_entry_with_no_checker_error_passes(monkeypatch, tmp_path, capsys):
    """catena-admin CI's catalog check fails on an error alone. A catalog
    lint line belongs to the swarm and Postgres lints, which check the
    compose itself."""
    _synthetic_sources(monkeypatch, tmp_path, _imported([
        "K3 warn app: an error page answers on /",
        "S2 info: the file names no version",
        "catalog lint: blueprints/synthetic-bad/docker-compose.yml: an error",
    ]))
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("finding,needle", [
    ("X6 error app: Service app mounts the Docker socket",
     "'X6 error app: Service app mounts the Docker socket' is an app checker error"),
    ("K1 error: the file declares no services",
     "'K1 error: the file declares no services' is an app checker error"),
    ("checker error: exit 2: catena-applint: one compose file",
     "'checker error: exit 2: catena-applint: one compose file' is an app checker error"),
    ("checker not run: name catena-applint with --applint or CATENA_APPLINT",
     "the app checker did not run on this entry: run catena-applint --fix --nodes 2 "
     "--name synthetic-bad on its compose"),
])
def test_an_imported_entry_the_catalog_check_refuses_fails(
        monkeypatch, tmp_path, capsys, finding, needle):
    """catena-admin CI runs catena-applint --catalog over every blueprint and
    fails on an error; an entry the checker never ran on is unchecked."""
    _synthetic_sources(monkeypatch, tmp_path, _imported([finding]))
    assert L.lint_all() == 1
    assert f"synthetic-bad.pending.findings: {needle}" in capsys.readouterr().out


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
