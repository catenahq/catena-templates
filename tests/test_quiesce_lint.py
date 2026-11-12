"""Unit tests for lib/quiesce_lint.py.

Most argv cases drive lint_migrate_argv directly. The lint_all cases run
the gate over the real blueprints/ tree, or over a synthetic one to prove it
reads the files rather than a cached artifact.
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
    """A synthetic blueprints/ tree with a hostile command must fail. Proves
    the gate is reading the files, not a cached artifact."""
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
      - "vps.route.port=80"
      - "vps.auth.mode=${AUTH_MODE:-public}"
      - "vps.auth.oidc=true"
      - "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/cb"
  worker:
    image: acme/app:1.0.0
"""
_SIGNIN_ENV = {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}},
               "AUTH_MODE": {"setting": {"default": "public"}}}


def test_a_template_that_asks_for_a_sign_in_entry_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _SIGNIN_COMPOSE, block={"env": _SIGNIN_ENV})
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
        "env": {**_SIGNIN_ENV, key: spec}})
    assert L.lint_all() == 1
    assert f"synthetic-bad: {key} is declared in the block's env" in capsys.readouterr().out


def _auto_update(value: str, image: str = "acme/app:1.0.0", comment: str = "# kept on purpose") -> str:
    return (f"services:\n  app:\n    image: {image}\n    labels:\n"
            + (f"      {comment}\n" if comment else "")
            + f'      - "vps.auto-update={value}"\n')


_LEVEL_ENV = {"env": {"UPDATE_LEVEL": {"setting": {"default": "major"}}}}


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


@pytest.mark.parametrize("value", ["${UPDATE_LEVEL:-major}", "${_lvl2:-patch+minor}"])
def test_an_auto_update_variable_with_a_level_default_passes(
        monkeypatch, tmp_path, capsys, value):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update(value, comment=""),
                       block=_LEVEL_ENV)
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_literal_level_on_a_service_that_is_no_database_needs_a_comment(
        monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update("patch", comment=""))
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert "synthetic-bad:app: vps.auto-update=patch is a literal level on a service that is not" in out
    assert "names why the service keeps its level" in out


@pytest.mark.parametrize("comment", ["# why it stays", ""])
def test_a_literal_level_with_a_comment_above_or_after_it_passes(
        monkeypatch, tmp_path, capsys, comment):
    compose = _auto_update("off", comment=comment)
    if not comment:
        compose = compose.replace('off"', 'off"  # why it stays')
    _synthetic_sources(monkeypatch, tmp_path, {}, compose)
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("image", [
    "postgres:18.6-alpine", "redis:8.10.3-alpine3.23", "valkey/valkey:8.1.1", "memcached:1.6.41-alpine",
    "mongo:8.0.4", "docker.elastic.co/elasticsearch/elasticsearch:8.17.0", "mariadb:11.8.9"])
def test_a_literal_level_on_a_database_or_a_cache_needs_no_comment(
        monkeypatch, tmp_path, capsys, image):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update("minor", image=image, comment=""))
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_variable_the_block_does_not_declare_is_refused(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update("${UPDATE_LEVEL:-major}", comment=""))
    assert L.lint_all() == 1
    assert ("synthetic-bad: the compose reads UPDATE_LEVEL and the block's env does not declare it"
            in capsys.readouterr().out)


def test_the_data_tier_match_reads_the_image_name_only():
    assert L.data_tier_image("redis:8.10.3-alpine3.23")
    assert L.data_tier_image("ghcr.io/acme/memcached:1.6")
    assert L.data_tier_image("postgres:18.6-alpine")
    assert not L.data_tier_image("rabbitmq:4.1-management")
    assert not L.data_tier_image("minio/minio:RELEASE.2025-01-01")
    assert not L.data_tier_image("acme/app:redis")


@pytest.mark.parametrize("value,needle", [
    ("${UPDATE_LEVEL}", "names UPDATE_LEVEL with no default"),
    ("${UPDATE_LEVEL:-}", "has the default '', which is not one of"),
    ("${UPDATE_LEVEL:-never}", "has the default 'never', which is not one of"),
    ("${1BAD:-major}", "is not one of major, minor, off, patch, patch+minor"),
])
def test_an_auto_update_variable_without_a_level_default_is_refused(
        monkeypatch, tmp_path, capsys, value, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auto_update(value))
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert f"synthetic-bad:app: vps.auto-update={value}" in out
    assert needle in out


def _auth_mode(value: str, comment: str = "") -> str:
    return ("services:\n  app:\n    image: acme/app:1.0.0\n    labels:\n"
            '      - "vps.route.host=${DOMAIN_HOST}"\n'
            '      - "vps.route.port=80"\n'
            + (f"      {comment}\n" if comment else "")
            + f'      - "vps.auth.mode={value}"\n'
            '      - "vps.auto-update=${UPDATE_LEVEL:-major}"\n')


def _mode_env(default: str) -> dict:
    return {"env": {"UPDATE_LEVEL": {"setting": {"default": "major"}},
                    "AUTH_MODE": {"setting": {"default": default}}}}


@pytest.mark.parametrize("value,default", [
    ("${AUTH_MODE:-public}", "public"),
    ("${AUTH_MODE:-admin-only}", "admin-only"),
    ("${AUTH_MODE:-private}", "private"),
])
def test_an_auth_mode_variable_with_a_matching_setting_passes(
        monkeypatch, tmp_path, capsys, value, default):
    block = _mode_env(default)
    block["env"]["DOMAIN_HOST"] = {"setting": {"default": "x.example.com"}}
    _synthetic_sources(monkeypatch, tmp_path, {}, _auth_mode(value), block=block)
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("value", ["public", "private", "admin-only"])
def test_a_literal_auth_mode_with_a_comment_above_it_passes(monkeypatch, tmp_path, capsys, value):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auth_mode(value, "# why it stays"), block=_LEVEL_ENV)
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_literal_auth_mode_with_a_comment_after_it_passes(monkeypatch, tmp_path, capsys):
    compose = _auth_mode("public").replace('public"', 'public"  # why it stays')
    _synthetic_sources(monkeypatch, tmp_path, {}, compose, block=_LEVEL_ENV)
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("value", ["public", "private", "admin-only"])
def test_a_literal_auth_mode_with_no_comment_is_refused(monkeypatch, tmp_path, capsys, value):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auth_mode(value), block=_LEVEL_ENV)
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert f"synthetic-bad:app: vps.auth.mode={value} is a literal mode; write ${{AUTH_MODE:-<mode>}}, " \
           "or a comment beside the label that names why the address keeps its access" in out


@pytest.mark.parametrize("value,env,needle", [
    ("${AUTH_MODE}", _mode_env("public"), "names AUTH_MODE with no default"),
    ("${AUTH_MODE:-publik}", _mode_env("publik"),
     "has the default 'publik', which is not one of admin-only, private, public"),
    ("${AUTH_MODE:-public}", _LEVEL_ENV,
     "reads AUTH_MODE and the block's env does not declare it as a setting"),
    ("${AUTH_MODE:-public}", _mode_env("admin-only"),
     "reads public while AUTH_MODE is not set, and a new deploy writes 'admin-only'"),
    ("publik", _LEVEL_ENV, "is not one of admin-only, private, public; the host reads it as unknown"),
])
def test_an_auth_mode_the_host_cannot_read_is_refused(
        monkeypatch, tmp_path, capsys, value, env, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, _auth_mode(value), block=env)
    assert L.lint_all() == 1
    out = capsys.readouterr().out
    assert f"synthetic-bad:app: vps.auth.mode={value}" in out
    assert needle in out


def test_a_mode_label_on_a_service_with_no_address_is_not_read(monkeypatch, tmp_path, capsys):
    compose = _auth_mode("publik").replace(
        '      - "vps.route.host=${DOMAIN_HOST}"\n      - "vps.route.port=80"\n', "")
    _synthetic_sources(monkeypatch, tmp_path, {}, compose, block=_LEVEL_ENV)
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_label_comment_is_found_above_or_after_the_label_of_its_key():
    text = _auth_mode("public", "# why") + "  other:\n    labels:\n" \
        '      - "vps.auth.mode=public"  # why too\n  bare:\n    labels:\n      - "vps.auth.mode=public"\n'
    assert L.commented_label(text, "vps.auth.mode") == {"app", "other"}
    assert L.commented_label(text, "vps.auto-update") == set()


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
    _synthetic_sources(monkeypatch, tmp_path, {}, _SIGNIN_COMPOSE.replace(*edit),
                       block={"env": _SIGNIN_ENV})
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
            ported = [*labels, "vps.route.port=80"] if any(
                label.startswith("vps.route.host=") for label in labels) else labels
            lines += ["    labels:"] + [f'      - "{label}"' for label in ported]
    return "\n".join(lines) + "\n"


def test_sign_in_labels_on_the_marked_main_address_pass(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _compose({
        "app": [_HOST, _MAIN, "vps.auth.mode=${AUTH_MODE:-public}", *_SIGNIN],
        "admin": [_ADMIN_HOST],
    }), block={"env": _SIGNIN_ENV})
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
        "origin": {"registry": "https://example.com/registry.json", "entry": "Synthetic",
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
    assert L.database_image("pgvector/pgvector:0.8.7-pg18-trixie")
    assert L.database_image("postgis/postgis:17-3.5")
    assert L.database_image("timescale/timescaledb-ha:pg17")
    assert not L.database_image("acme/app:pgvector")
    assert not L.database_image("nextcloud:34.0.0-apache")
    assert not L.database_image("localhost:5000/acme/app:mysql-client")


def _synthetic_sources(monkeypatch, tmp_path, catena: dict,
                       compose: str = "services:\n  app:\n    image: x\n",
                       block: dict | None = None) -> None:
    """Point the loader at a one-template blueprints/ tree whose compose is
    `compose` (one service, `app`, by default) behind `block` as its x-catena
    block (the env defaults to one DOMAIN_HOST setting), and whose app.json
    carries `catena` over the required fields."""
    blueprint = tmp_path / "blueprints" / "synthetic-bad"
    blueprint.mkdir(parents=True)
    block = {"schema": 2, "env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}}},
             **(block or {})}
    head = yaml.safe_dump({model.BLOCK_KEY: block}) + "\n"
    (blueprint / model.COMPOSE_NAME).write_text(head + compose)
    prose = {
        "display_name": "Synthetic",
        "summary": "test",
        "what_it_is": "test",
        "replaces": [],
        "compose_description": "test",
    }
    doc = {
        "schema": 1,
        "app_name": "synthetic-bad",
        "upstream_url": "https://example.com",
        "categories": ["Testing"],
        "sso_mode": "none",
        "bench": {"pack": "nodb"},
        "sizing": {"peak_ram_mb": 128},
        "en": {**prose, "setup_steps": "1. none"},
        "fr": {**prose, "setup_steps": "1. aucune"},
        **catena,
    }
    (blueprint / model.CARD_NAME).write_text(json.dumps(doc))
    (tmp_path / model.CATALOG_META).write_text(json.dumps({"order": ["synthetic-bad"]}))
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


@pytest.mark.parametrize("argv", [
    ["/usr/bin/coolwsd", "--probe", "--use-env-vars"],
    ["/app/arcane", "health", "--timeout", "10s"],
    ["/windshift", "healthcheck"],
    ["/usr/local/bin/catena-reports", "probe"],
])
def test_the_health_command_of_a_shell_less_image_is_allowed(monkeypatch, tmp_path, capsys, argv):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": {"service": "app", "verify": [argv]}})
    assert L.lint_all() == 0, capsys.readouterr().out


def test_a_path_command_outside_the_allowlist_is_refused(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": {
        "service": "app", "verify": [["/usr/bin/curl", "https://evil.example.com"]]}})
    assert L.lint_all() == 1
    assert ("synthetic-bad.hooks.verify[0]: starts with non-allowlisted command '/usr/bin/curl'"
            ) in capsys.readouterr().out


def _managed(expr: str, **block) -> dict:
    return {"env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}},
                    "TURN_SECRET": {"managed": {"expr": expr}}}, **block}


def test_a_managed_expression_reads_the_secrets_the_block_lists(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {},
                       block=_managed("{{ turn_static_auth_secret }}",
                                      secrets=["turn_static_auth_secret"]))
    assert L.lint_all() == 0, capsys.readouterr().out
    _synthetic_sources(monkeypatch, tmp_path / "host", {},
                       block=_managed("{{ sso_hostname | default('auth.' ~ cloudflare_zone) }}"))
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


def test_a_blueprint_reads_the_denied_secrets_its_table_entry_names_and_no_other(monkeypatch, tmp_path, capsys):
    block = _managed("{{ turn_static_key | default('') }}", secrets=["turn_static_key"])
    monkeypatch.setattr(L, "DENIED_SECRETS", L.DENIED_SECRETS | {"turn_static_key", "turn_other_key"})
    monkeypatch.setattr(L, "PROVIDER_SECRETS", {"synthetic-bad": frozenset({"turn_static_key"})})
    _synthetic_sources(monkeypatch, tmp_path, {}, block=block)
    assert L.lint_all() == 0, capsys.readouterr().out
    _synthetic_sources(monkeypatch, tmp_path / "unlisted", {}, block={**block, "secrets": []})
    assert L.lint_all() == 1
    assert "reads the store secret 'turn_static_key', which the block's secrets does not list" in (
        capsys.readouterr().out)
    monkeypatch.setattr(L, "PROVIDER_SECRETS", {"someone-else": frozenset({"turn_static_key"})})
    _synthetic_sources(monkeypatch, tmp_path / "other", {}, block=block)
    assert L.lint_all() == 1
    assert "reads 'turn_static_key', a secret no managed expression may read" in capsys.readouterr().out
    monkeypatch.setattr(L, "PROVIDER_SECRETS", {"synthetic-bad": frozenset({"turn_static_key"})})
    _synthetic_sources(monkeypatch, tmp_path / "sibling", {}, block=_managed(
        "{{ turn_other_key }}", secrets=["turn_other_key"]))
    assert L.lint_all() == 1
    assert "reads 'turn_other_key', a secret no managed expression may read" in capsys.readouterr().out


def test_the_provider_table_never_holds_a_gate_cookie_secret_and_sits_inside_the_deny_list():
    for slug, names in L.PROVIDER_SECRETS.items():
        assert names <= L.DENIED_SECRETS, slug
        assert not [n for n in names if "gate_cookie_secret" in n or n.startswith(L.DENIED_SECRET_PREFIXES)], slug


def test_the_provider_table_is_the_denied_secrets_of_the_contract_wiring():
    wiring = json.loads((ROOT.parent / "contracts" / "default-apps" / "wiring.json").read_text(encoding="utf-8"))
    held = {app: frozenset(set(entry["secrets"]) & L.DENIED_SECRETS) for app, entry in wiring["apps"].items()}
    assert L.PROVIDER_SECRETS == {app: names for app, names in held.items() if names}


def test_the_blueprints_read_only_the_denied_secrets_the_table_gives_them():
    for entry in model.load_sources():
        assert L.lint_managed_secrets(entry) == [], entry.slug


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


@pytest.mark.parametrize("declared", [
    "/", "/mnt/", "/a/../b", "/run", "/var/run/docker", "/srv", "/srv/catena/compose/web", "mnt",
    "/etc", "/etc/ssl", "/usr/local/bin", "/bin", "/lib64", "/boot", "/root", "/proc", "/sys", "/dev", "/var",
    "/var/lib/docker", "/srv/catena", "/srv/catena/docker/volumes", "/srv/catena/apps"])
def test_a_host_path_the_checker_refuses_for_every_app_is_refused(tmp_path, declared):
    from types import SimpleNamespace
    entry = SimpleNamespace(slug="demo", block={"host_paths": [declared]})
    assert L.lint_host_paths(entry)


def test_capabilities_are_names_a_service_adds(tmp_path):
    from types import SimpleNamespace
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate({"schema": 2, "capabilities": ["NET_ADMIN"]}, schema)
    for bad in ([], ["ALL"], ["CAP_NET_ADMIN"], ["net_admin"], ["NET_ADMIN", "NET_ADMIN"]):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"schema": 2, "capabilities": bad}, schema)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(yaml.safe_dump({"services": {"mail": {"cap_add": ["NET_ADMIN"]}}}), encoding="utf-8")

    def entry(names):
        return SimpleNamespace(slug="demo", compose_path=compose, block={"capabilities": names})

    assert L.lint_capabilities(entry(["NET_ADMIN"])) == []
    assert L.lint_capabilities(entry(["SYS_ADMIN"]))
    assert L.lint_capabilities(entry(["ALL"]))
    assert L.lint_capabilities(SimpleNamespace(slug="demo", compose_path=compose, block={})) == []


def test_access_levels_are_slugs_with_a_label_in_both_languages():
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))
    label = {"en": "Nextcloud", "fr": "Nextcloud"}
    for good in ({"user": {"label": label}}, {"user": {"label": label}, "read-only": {"label": label}}):
        jsonschema.validate({"schema": 2, "access": good}, schema)
    for bad in ({}, {"User": {"label": label}}, {"1": {"label": label}}, {"user": {}},
                {"user": {"label": {"en": "Nextcloud"}}}, {"user": {"label": {"en": "", "fr": "x"}}},
                {"user": {"label": label, "extra": 1}}, {"user": "Nextcloud"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"schema": 2, "access": bad}, schema)


def test_host_paths_that_hold_nothing_forbidden_pass():
    from types import SimpleNamespace
    entry = SimpleNamespace(slug="demo", block={"host_paths": ["/mnt", "/media/disk", "/var/www", "/srv/catena/apps/web"]})
    assert L.lint_host_paths(entry) == []


def test_scan_and_reconcile_are_hook_lists_the_lint_reads():
    assert {"scan", "reconcile"} <= set(L.HOOK_LISTS)


@pytest.mark.parametrize("hooks,needle", [
    ({"service": "app", "scan": [["curl", "https://evil.example.com"]]},
     "synthetic-bad.hooks.scan[0]: starts with non-allowlisted command 'curl'"),
    ({"service": "app", "reconcile": [["php", "occ", "list", "&&", "php", "occ", "status"]]},
     "synthetic-bad.hooks.reconcile[0]: contains the shell operator '&&'"),
    ({"service": "app", "reconcile": [["sh", "-c", "if true; then"]]},
     "synthetic-bad.hooks.reconcile[0]: does not parse as sh"),
])
def test_a_scan_or_reconcile_body_the_host_cannot_run_is_refused(
        monkeypatch, tmp_path, capsys, hooks, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": hooks})
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def test_scan_and_reconcile_hooks_alone_pass_the_schema_and_the_lint(monkeypatch, tmp_path, capsys):
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))
    for name in ("scan", "reconcile"):
        jsonschema.validate({"schema": 2, "hooks": {"service": "app", name: [["sh", "-c", "cat >/dev/null"]]}},
                            schema)
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"hooks": {
        "service": "app", "scan": [["sh", "-c", "cat | wc -c; echo $$?"]],
        "reconcile": [["sh", "-c", "echo missing=0"]]}})
    assert L.lint_all() == 0, capsys.readouterr().out
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"schema": 2, "hooks": {"service": "app", "scan": []}}, schema)


_ANTIVIRUS = {"env": {"DOMAIN_HOST": {"setting": {"default": "x.example.com"}},
                      "AV_HOST": {"setting": {"default": "av"}}},
              "provides": {"antivirus": {"host": "AV_HOST"}}}


def test_a_block_that_provides_antivirus_declares_hooks_scan(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block=_ANTIVIRUS)
    assert L.lint_all() == 1
    assert "synthetic-bad.x-catena.provides.antivirus: the block declares no hooks.scan" \
        in capsys.readouterr().out
    _synthetic_sources(monkeypatch, tmp_path / "scan", {}, block={
        **_ANTIVIRUS, "hooks": {"service": "app", "scan": [["sh", "-c", "cat >/dev/null"]]}})
    assert L.lint_all() == 0, capsys.readouterr().out


def test_the_antivirus_rule_holds_for_every_blueprint_that_provides_antivirus():
    from types import SimpleNamespace
    assert L.lint_antivirus_scan(SimpleNamespace(slug="clamav", block=_ANTIVIRUS))
    assert L.lint_antivirus_scan(SimpleNamespace(slug="other", block={"provides": {"turn": {}}})) == []
    entries = [e for e in model.load_sources() if "antivirus" in (e.block.get("provides") or {})]
    assert [e.slug for e in entries] == ["clamav"]
    assert L.lint_antivirus_scan(entries[0]) == []


def test_a_probe_schema_holds_the_limits(monkeypatch):
    import jsonschema
    schema = json.loads((ROOT / "block.schema.json").read_text(encoding="utf-8"))

    def probe(**fields):
        return {"schema": 2, "probe": {"service": "app", "check": ["php", "-r", "exit(0);"], **fields}}

    for good in ({}, {"memory": "768m"}, {"memory": "2g"}, {"memory": "2048m"}, {"memory": "1g"},
                 {"timeout_seconds": 600}, {"user": "www-data"}):
        jsonschema.validate(probe(**good), schema)
    for bad in ({"memory": "3g"}, {"memory": "2049m"}, {"memory": "0g"}, {"memory": "768"},
                {"memory": "1.5g"}, {"timeout_seconds": 601}, {"timeout_seconds": 0}, {"check": []},
                {"extra": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(probe(**bad), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"schema": 2, "probe": {"check": ["true"]}}, schema)


def test_a_probe_with_an_allowlisted_check_in_a_compose_service_passes(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={
        "probe": {"service": "app", "memory": "768m", "timeout_seconds": 180,
                  "check": ["php", "-r", "exit(0);"]}})
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("probe,needle", [
    ({"service": "web", "check": ["php", "-r", "exit(0);"]},
     "synthetic-bad.probe.service: 'web' is not a service"),
    ({"service": "app", "check": ["curl", "https://evil.example.com"]},
     "synthetic-bad.probe.check: starts with non-allowlisted command 'curl'"),
    ({"service": "app", "check": ["php", "-r", "x", "&&", "php"]},
     "synthetic-bad.probe.check: contains the shell operator '&&'"),
    ({"service": "app", "check": ["php", "-r", "echo $HOME;"]},
     "synthetic-bad.x-catena.probe.check[2]: holds a $"),
    ({"service": "app"}, "synthetic-bad.probe.check: missing, and lifecycle.ready does not answer"),
])
def test_a_probe_the_host_cannot_run_is_refused(monkeypatch, tmp_path, capsys, probe, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"probe": probe})
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def test_a_probe_with_no_check_uses_lifecycle_ready_of_its_service(monkeypatch, tmp_path, capsys):
    lifecycle = {"service": "app", "ready": ["php", "occ", "status"],
                 "migrate": [["php", "occ", "status"]], "timeout_seconds": 60}
    _synthetic_sources(monkeypatch, tmp_path, {}, block={"probe": {"service": "app"}, "lifecycle": lifecycle})
    assert L.lint_all() == 0, capsys.readouterr().out
    compose = "services:\n  app:\n    image: x\n  worker:\n    image: x\n"
    _synthetic_sources(monkeypatch, tmp_path / "other", {}, compose,
                       block={"probe": {"service": "worker"}, "lifecycle": lifecycle})
    assert L.lint_all() == 1
    assert "synthetic-bad.probe.check: missing" in capsys.readouterr().out


_HOST_NETWORK_COMPOSE = """\
services:
  app:
    image: acme/app:1.0.0
    labels:
      - "{label}"
"""


@pytest.mark.parametrize("label", [
    "vps.expose.tcp=25,465,587,993", "vps.expose.udp=10000-10002", "vps.expose.udp=49152-50175",
    "vps.expose.tcp=3478:3479",
])
def test_a_host_network_block_with_declared_ports_passes(monkeypatch, tmp_path, capsys, label):
    _synthetic_sources(monkeypatch, tmp_path, {}, _HOST_NETWORK_COMPOSE.format(label=label),
                       block={"privileges": ["host_network"]})
    assert L.lint_all() == 0, capsys.readouterr().out


@pytest.mark.parametrize("label,needle", [
    ("vps.expose.udp=49152-50176", "a range of more than 1024 ports"),
    ("vps.expose.udp=1-65535", "a range of more than 1024 ports"),
    ("vps.expose.tcp=25,abc", "'abc', which is not a port or a range"),
    ("vps.expose.tcp=0", "which is not a range of ports 1 to 65535"),
    ("vps.expose.udp=2000-1000", "which is not a range of ports 1 to 65535"),
    ("vps.auto-update=off", "host_network is asked for, and no service declares its ports"),
])
def test_a_host_network_block_whose_ports_are_not_declared_is_refused(
        monkeypatch, tmp_path, capsys, label, needle):
    _synthetic_sources(monkeypatch, tmp_path, {}, _HOST_NETWORK_COMPOSE.format(label=label),
                       block={"privileges": ["host_network"]})
    assert L.lint_all() == 1
    assert needle in capsys.readouterr().out


def test_a_block_without_host_network_is_not_asked_for_ports(monkeypatch, tmp_path, capsys):
    _synthetic_sources(monkeypatch, tmp_path, {}, _HOST_NETWORK_COMPOSE.format(label="vps.expose.udp=1-65535"))
    assert L.lint_all() == 0, capsys.readouterr().out


def _host_network_entry(tmp_path, labels_by_service):
    from types import SimpleNamespace
    compose = tmp_path / f"compose-{len(list(tmp_path.iterdir()))}.yml"
    services = {name: {"labels": labels} for name, labels in labels_by_service.items()}
    compose.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")
    return SimpleNamespace(compose_path=compose, slug="demo", block={"privileges": ["host_network"]})


def test_a_host_network_service_declares_its_ports_or_marks_itself_as_listening_on_none(tmp_path):
    assert L.lint_host_network_ports(_host_network_entry(tmp_path, {"a": ["vps.expose.tcp=45876"]})) == []
    assert L.lint_host_network_ports(_host_network_entry(tmp_path, {"a": ["vps.expose.none=true"]})) == []


def test_a_host_network_block_with_no_declaration_is_refused(tmp_path):
    errors = L.lint_host_network_ports(_host_network_entry(tmp_path, {"a": ["vps.auto-update=patch"]}))
    assert errors and "vps.expose.none=true" in errors[0]
    assert L.lint_host_network_ports(_host_network_entry(tmp_path, {"a": ["vps.expose.none=false"]}))


def test_a_service_cannot_declare_ports_and_listen_on_none(tmp_path):
    errors = L.lint_host_network_ports(_host_network_entry(
        tmp_path, {"a": ["vps.expose.none=true", "vps.expose.udp=3478"]}))
    assert any("demo:a: carries vps.expose.none and a vps.expose.tcp or vps.expose.udp label" in e for e in errors)
