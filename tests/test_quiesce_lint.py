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
import yaml

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
        "php", "occ", "node", "yarn", "npm", "npx", "bench",
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
    schema = json.loads((ROOT / "block.schema.json").read_text())
    lifecycle = schema["properties"]["lifecycle"]
    assert lifecycle["properties"]["timeout_seconds"]["maximum"] == 3600
    assert lifecycle["required"] == ["service", "timeout_seconds"]
    assert lifecycle["anyOf"] == [{"required": ["migrate"]},
                                  {"required": ["versioned_volumes"]}]


def test_quiesce_timeout_cap_lives_in_the_schema():
    """The cap lives in block.schema.json and nowhere else, so it is
    enforced on every load rather than only by the lint entrypoint."""
    schema = json.loads((ROOT / "block.schema.json").read_text())
    quiesce = schema["properties"]["quiesce"]
    assert quiesce["properties"]["timeout_seconds"]["maximum"] == 60
    assert quiesce["required"] == ["service", "pre", "timeout_seconds"]


def test_lint_all_against_real_sources_passes():
    assert L.lint_all() == 0


def test_lint_all_rejects_a_hostile_quiesce_command(monkeypatch, tmp_path, capsys):
    """A synthetic sources/ tree with a hostile command must fail. Proves
    the gate is reading sources, not a cached artifact."""
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "schema": 2,
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
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "schema": 2,
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
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "schema": 2,
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


def test_allowed_ready_commands_lock():
    """The ready question adds bash to the lifecycle allowlist, for a probe
    of the server's port where the image has no other client."""
    assert L.ALLOWED_READY_COMMANDS == L.ALLOWED_MIGRATE_COMMANDS | {"bash"}
    assert "bash" not in L.ALLOWED_MIGRATE_COMMANDS


@pytest.mark.parametrize("lifecycle", [
    {"migrate": [["php", "occ", "db:add-missing-indices"]]},
    {"versioned_volumes": ["code"]},
    {"before_update": [["php", "occ", "maintenance:mode", "--on"]],
     "migrate": [["php", "occ", "db:add-missing-indices"]]},
])
def test_a_lifecycle_without_ready_is_refused(monkeypatch, tmp_path, capsys, lifecycle):
    """The update lane asks ready before each phase; a lifecycle that does
    not declare it runs its commands against a container whose entrypoint
    may still be copying the application."""
    _synthetic_sources(monkeypatch, tmp_path, {}, _VERSIONED_COMPOSE, block={
        "schema": 2,
        "lifecycle": {"service": "app", "timeout_seconds": 60, **lifecycle},
    })
    assert L.lint_all() == 1
    assert "synthetic-bad.lifecycle.ready: missing" in capsys.readouterr().out


def test_a_bash_ready_probe_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "schema": 2,
        "lifecycle": {"service": "app", "timeout_seconds": 60,
                      "ready": ["bash", "-c", ": > /dev/tcp/127.0.0.1/8000"],
                      "migrate": [["bench", "--site", "all", "migrate"]]},
    })
    assert L.lint_all() == 0, capsys.readouterr().out


def test_bash_is_not_a_migrate_command(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "schema": 2,
        "lifecycle": {"service": "app", "timeout_seconds": 60,
                      "ready": ["php", "occ", "status"],
                      "migrate": [["bash", "-c", "true"]]},
    })
    assert L.lint_all() == 1
    assert "synthetic-bad.lifecycle.migrate[0]: starts with non-allowlisted command 'bash'" \
        in capsys.readouterr().out


_STORE_COMPOSE = """\
services:
  app:
    image: acme/app:1.0.0
    environment:
      BUCKET: ${S3_BUCKET}
      KEY: ${S3_ACCESS_KEY}
      SECRET: ${S3_SECRET_KEY}
"""
_STORE_ENV = {key: {"setting": {"default": ""}}
              for key in ("DOMAIN_HOST", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")}


def _object_store(**changes) -> dict:
    store = {
        "env": {"bucket": "S3_BUCKET", "access_key": "S3_ACCESS_KEY",
                "secret_key": "S3_SECRET_KEY"},
        "switch": {"service": "app", "user": "www-data",
                   "commands": [["php", "occ", "files:scan", "{new_bucket}"]]},
    }
    return {"schema": 2, "env": _STORE_ENV, "object_store": {**store, **changes}}


def test_an_object_store_the_compose_reads_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _STORE_COMPOSE, block=_object_store())
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("env,compose,needle", [
    ({k: v for k, v in _STORE_ENV.items() if k != "S3_SECRET_KEY"}, _STORE_COMPOSE,
     "synthetic-bad.object_store.env.secret_key: 'S3_SECRET_KEY' is not in the block's env"),
    (_STORE_ENV, _STORE_COMPOSE.replace("${S3_SECRET_KEY}", "fixed"),
     "synthetic-bad.object_store.env.secret_key: the compose never reads 'S3_SECRET_KEY'"),
])
def test_an_object_store_env_key_the_app_never_reads_is_refused(
        monkeypatch, tmp_path, capsys, env, compose, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, compose, block={**_object_store(), "env": env})
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


@pytest.mark.parametrize("switch,needle", [
    ({"service": "worker", "commands": [["php", "occ", "status"]]},
     "synthetic-bad.object_store.switch.service: 'worker' is not a service"),
    ({"service": "app", "commands": [["curl", "https://evil.example.com"]]},
     "synthetic-bad.object_store.switch.commands[0]: starts with non-allowlisted command 'curl'"),
    ({"service": "app", "commands": [["php", "occ", "status", "&&", "php", "occ", "list"]]},
     "synthetic-bad.object_store.switch.commands[0]: contains the shell operator '&&'"),
    ({"service": "app", "commands": [["php", "occ", "files:scan", "{bucket}"]]},
     "synthetic-bad.object_store.switch.commands[0]: '{bucket}' is not a placeholder"),
])
def test_an_object_store_switch_the_host_cannot_run_is_refused(
        monkeypatch, tmp_path, capsys, switch, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, _STORE_COMPOSE,
                       block=_object_store(switch=switch))
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


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
    return {"schema": 2, "lifecycle": {
        "service": "app",
        "ready": ["php", "occ", "status", "--exit-code"],
        "migrate": [["php", "occ", "db:add-missing-indices"]],
        "versioned_volumes": volumes,
        "timeout_seconds": 60,
    }}


def test_a_versioned_volume_long_running_services_mount_passes(
        monkeypatch, tmp_path, capsys):
    """Both compose forms count as a mount: the short `name:/path` and the
    long `source:`."""
    _synthetic_sources(monkeypatch, tmp_path, {}, _VERSIONED_COMPOSE,
                       block=_versioned(["code"]))
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
    _synthetic_sources(monkeypatch, tmp_path, {}, compose, block=_versioned([volume]))
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


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("key", ["OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"])
def test_a_sign_in_value_the_sync_writes_has_no_second_writer(
        monkeypatch, tmp_path, capsys, key, managed):
    """The settings sync writes the three values from the app's own
    Keycloak client; a catalog default or a managed key would write the
    same key from somewhere else."""
    spec = {"managed": {"expr": "x"}} if managed else {"setting": {"default": "from-the-catalog"}}
    _synthetic_sources(monkeypatch, tmp_path, {}, _SIGNIN_COMPOSE, block={
        "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}, key: spec}})
    assert L.lint_all() == 1
    assert f"synthetic-bad: {key} is declared in the block's env" in capsys.readouterr().out


def _auto_update(value: str) -> str:
    return ("services:\n  app:\n    image: acme/app:1.0.0\n    labels:\n"
            f'      - "vps.auto-update={value}"\n')


@pytest.mark.parametrize("value", ["off", "patch", "minor", "patch+minor", "major", "Minor"])
def test_an_auto_update_policy_the_engine_knows_passes(monkeypatch, tmp_path, capsys, value):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update(value))
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("value", ["minorr", "patch,minor", "true", "never"])
def test_an_unknown_auto_update_policy_is_refused(monkeypatch, tmp_path, capsys, value):
    """The engine replaces a value it does not know with its default, so a
    typo would pass as a policy nobody chose."""
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update(value))
    assert L.lint_all() == 1
    assert (f"synthetic-bad:app: vps.auto-update={value} is not one of "
            "major, minor, off, patch, patch+minor") in capsys.readouterr().out


def test_a_block_with_no_dollar_and_no_image_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "env": {"DOMAIN_HOST": {"managed": {"expr": "x.{{ cloudflare_zone }}"}}}})
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("block,needle", [
    ({"env": {"DOMAIN_HOST": {"managed": {"expr": "x.${ZONE}"}}}},
     "synthetic-bad.x-catena.env.DOMAIN_HOST.managed.expr: holds a $"),
    ({"quiesce": {"service": "app", "pre": [["pkill", "-x", "$$PID"]], "timeout_seconds": 10}},
     "synthetic-bad.x-catena.quiesce.pre[0][2]: holds a $"),
    ({"env": {"DOMAIN_HOST": {"managed": {"expr": "acme/app:1.0.0"}}}},
     "synthetic-bad.x-catena.env.DOMAIN_HOST.managed.expr: names the image acme/app:1.0.0"),
    ({"lifecycle": {"service": "app", "ready": ["acme/app:1.0.0", "run"],
                    "migrate": [["php", "occ", "status"]], "timeout_seconds": 60}},
     "synthetic-bad.x-catena.lifecycle.ready[0]: names the image acme/app:1.0.0"),
])
def test_a_block_with_a_dollar_or_an_image_is_refused(
        monkeypatch, tmp_path, capsys, block, needle):
    """Docker interpolates every value under a top-level x- key when it
    deploys the file, and the update lane rewrites each image reference it
    finds in the file."""
    _synthetic_sources(monkeypatch, tmp_path, {},
                       "services:\n  app:\n    image: acme/app:1.0.0\n", block=block)
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


@pytest.mark.parametrize("edit,needle", [
    (('      - "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"\n', ""),
     "synthetic-bad:app: vps.auth.oidc=true without vps.auth.oidc.redirect_uris"),
    (('      - "vps.auth.oidc=true"\n', '      - "vps.auth.oidc=true"\n'
      '      - "vps.auth.oidc.audience=app"\n'),
     "synthetic-bad:app: vps.auth.oidc.audience is not a sign-in label the host reads"),
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
                       compose: str = "services:\n  app:\n    image: x\n",
                       block: dict | None = None) -> None:
    """Point the loader at a one-template sources/ tree whose compose is
    `compose` (one service, `app`, by default) behind `block` as its x-catena
    block (the env defaults to one DOMAIN_HOST setting), and whose source x-catena
    block carries `catena` over the required fields."""
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    blueprint = tmp_path / "blueprints" / "synthetic-bad"
    blueprint.mkdir(parents=True)
    block = {"schema": 2, "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}},
             **(block or {})}
    head = yaml.safe_dump({model.BLOCK_KEY: block}) + "\n"
    (blueprint / model.COMPOSE_NAME).write_text(head + compose)
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
        json.dumps({"order": ["synthetic-bad"]})
    )
    monkeypatch.setattr(model, "SOURCES", sources)
    monkeypatch.setattr(model, "BLUEPRINTS", tmp_path / "blueprints")


def test_a_hooks_body_writes_each_dollar_doubled(monkeypatch, tmp_path, capsys):
    hooks = {"service": "app", "configure": [["sh", "-c", "echo $$HOME"]]}
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": hooks})
    assert L.lint_all() == 0, capsys.readouterr().out
    hooks["configure"] = [["sh", "-c", "echo $HOME"]]
    _synthetic_sources(monkeypatch, tmp_path / "bare", {}, block={"hooks": hooks})
    assert L.lint_all() == 1
    assert "synthetic-bad.x-catena.hooks.configure[0][2]: holds a $" in capsys.readouterr().out


@pytest.mark.parametrize("hooks,needle", [
    ({"service": "web", "verify": [["php", "occ", "status"]]},
     "synthetic-bad.hooks.service: 'web' is not a service"),
    ({"service": "app", "verify": [["curl", "https://evil.example.com"]]},
     "synthetic-bad.hooks.verify[0]: starts with non-allowlisted command 'curl'"),
    ({"service": "app", "users": [["php", "occ", "list", "&&", "php", "occ", "status"]]},
     "synthetic-bad.hooks.users[0]: contains the shell operator '&&'"),
    ({"service": "app", "configure": [["sh", "-c", "if true; then"]]},
     "synthetic-bad.hooks.configure[0]: does not parse as sh"),
    ({"service": "app", "configure": [["python3", "-c", "def ("]]},
     "synthetic-bad.hooks.configure[0]: does not parse as Python"),
])
def test_a_hooks_body_the_host_cannot_run_is_refused(monkeypatch, tmp_path, capsys, hooks, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": hooks})
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def test_a_hooks_script_that_parses_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": {
        "service": "app",
        "configure": [["sh", "-c", "set -e\nfor f in a b; do echo $$f; done"],
                      ["python3", "-c", "print(1)"]],
        "verify": [["php", "occ", "status"]]}})
    assert L.lint_all() == 0, capsys.readouterr().out


def _managed(expr: str, **block) -> dict:
    return {"env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}},
                    "TURN_SECRET": {"managed": {"expr": expr}}}, **block}


def test_a_managed_expression_reads_the_secrets_the_block_lists(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {},
                       block=_managed("{{ turn_static_auth_secret }}",
                                      secrets=["turn_static_auth_secret"]))
    assert L.lint_all() == 0, capsys.readouterr().out
    _synthetic_sources(monkeypatch, tmp_path / "host", {},
                       block=_managed("{{ coturn_hostname | default('turn.' ~ cloudflare_zone) }}"))
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_managed_expression_reading_an_unlisted_secret_is_refused(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block=_managed("{{ turn_static_auth_secret }}"))
    assert L.lint_all() == 1
    assert ("synthetic-bad.x-catena.env.TURN_SECRET.managed.expr: reads the store secret "
            "'turn_static_auth_secret', which the block's secrets does not list"
            ) in capsys.readouterr().out


def test_a_managed_expression_reading_a_denied_secret_is_refused(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {},
                       block=_managed("{{ admin_password }}", secrets=["admin_password"]))
    assert L.lint_all() == 1
    assert "reads 'admin_password', a secret no managed expression may read" in capsys.readouterr().out


def test_a_managed_expression_reads_what_a_template_provides(monkeypatch, tmp_path, capsys):
    block = _managed("{{ provided.nowhere.jwt_secret }}")
    _synthetic_sources(monkeypatch, tmp_path, {}, block=block)
    assert L.lint_all() == 1
    assert ("synthetic-bad.x-catena.env.TURN_SECRET.managed.expr: no template provides "
            "'jwt_secret' of 'nowhere'") in capsys.readouterr().out
    _synthetic_sources(monkeypatch, tmp_path / "own", {}, block={
        **_managed("{{ provided.nowhere.jwt_secret }}"),
        "provides": {"nowhere": {"jwt_secret": "TURN_SECRET"}}})
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_provided_value_is_a_key_of_the_env(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        **_managed("x"), "provides": {"turn": {"secret": "NOT_THERE"}}})
    assert L.lint_all() == 1
    assert ("synthetic-bad.x-catena.provides.turn.secret: 'NOT_THERE' is not in the block's env"
            ) in capsys.readouterr().out


def test_the_names_an_expression_reads():
    assert L.expression_names("a.{{ zone }}") == {"zone"}
    assert L.expression_names("{{ provided.office.jwt_secret }}") == set()
    assert L.provided_reads("{{ provided.turn.host }}:{{ provided.turn.port | default('1') }}") == {
        ("turn", "host"), ("turn", "port")}
    assert L.expression_names("{{ x | default('lit' ~ other_name) }}") == {"x", "other_name"}
    assert L.expression_names(
        "{{ lookup('password', '/dev/null length=8 chars=hexdigits') | lower }}") == set()
    assert L.expression_names("plain") == set()


def _signin_entry(tmp_path, labels):
    from types import SimpleNamespace
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(yaml.safe_dump({"services": {"web": {"labels": labels}}}), encoding="utf-8")
    return SimpleNamespace(compose_path=compose, slug="demo", env={})


def _signin(extra):
    return ["vps.route.host=a.example.test", "vps.auth.oidc=true",
            "vps.auth.oidc.redirect_uris=https://a.example.test/cb", *extra]


def test_the_scopes_and_introspect_labels_are_labels_the_host_reads(tmp_path):
    entry = _signin_entry(tmp_path, _signin(
        ["vps.auth.oidc.scopes=offline_access, api", "vps.auth.oidc.introspect=true"]))
    assert L.lint_signin(entry) == []


def test_a_scope_that_is_no_name_and_introspection_on_a_public_entry_are_refused(tmp_path):
    errs = L.lint_signin(_signin_entry(tmp_path, _signin(["vps.auth.oidc.scopes=a b,../x"])))
    assert any("'a b'" in e for e in errs) and any("'../x'" in e for e in errs)
    errs = L.lint_signin(_signin_entry(tmp_path, _signin(
        ["vps.auth.oidc.introspect=true", "vps.auth.oidc.public=true"])))
    assert any("introspect=true on a public sign-in entry" in e for e in errs)


def test_users_group_needs_a_users_hook_and_external_volumes_is_a_privilege():
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))
    base = {"schema": 2, "hooks": {"service": "app", "users": [["php", "occ"]], "users_group": "staff/sales"}}
    jsonschema.validate(base, schema)
    jsonschema.validate({"schema": 2, "privileges": ["external_volumes"]}, schema)
    for bad in ({"service": "app", "configure": [["php"]], "users_group": "staff"},
                {"service": "app", "users": [["php"]], "users_group": "staff//x"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"schema": 2, "hooks": bad}, schema)


def test_host_paths_are_absolute_paths_the_schema_and_the_lint_accept():
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate({"schema": 2, "host_paths": ["/mnt", "/media/disk"]}, schema)
    for bad in ([], ["mnt"], ["/mnt/$DIR"], ["/mnt", "/mnt"], [""]):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"schema": 2, "host_paths": bad}, schema)


@pytest.mark.parametrize("declared", ["/", "/mnt/", "/a/../b", "/run", "/var/run/docker", "/srv", "/srv/catena/compose/web"])
def test_a_host_path_the_checker_refuses_for_every_app_is_refused(tmp_path, declared):
    from types import SimpleNamespace
    entry = SimpleNamespace(slug="demo", block={"host_paths": [declared]})
    assert L.lint_host_paths(entry)


def test_host_paths_that_hold_nothing_forbidden_pass():
    from types import SimpleNamespace
    entry = SimpleNamespace(slug="demo", block={"host_paths": ["/mnt", "/media/disk", "/etc/ssl/certs"]})
    assert L.lint_host_paths(entry) == []
