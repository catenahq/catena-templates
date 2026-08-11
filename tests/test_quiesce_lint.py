"""Unit tests for lib/quiesce_lint.py.

Every case drives the pure snippet-level functions, so the suite runs
without shellcheck on the host. The end-to-end case at the bottom runs
the lint over the real sources/ tree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint as L  # noqa: E402


def test_allowed_commands_lock():
    """Widening the allowlist must be deliberate: these snippets run as
    root on every client host before every backup."""
    assert L.ALLOWED_COMMANDS == frozenset({
        "docker", "head", "true", "false",
        "occ", "php",
        "mongo", "mongosh",
        "rocketchat-cli",
        "touch", "rm", "mv",
        "mariadb-dump", "mysqldump",
        "pg_dump", "pg_dumpall",
        "mongodump",
        "sqlite3",
    })


def test_allowed_db_dump_commands_pass():
    for cmd in ("mariadb-dump", "pg_dump", "pg_dumpall", "mongodump", "sqlite3"):
        assert L.lint_snippet(f"{cmd} --help", label="x") == []


def test_simple_allowed_command_passes():
    assert L.lint_snippet("docker exec abc occ maintenance:mode --on", label="x") == []


def test_pipeline_with_head_passes():
    """The Nextcloud idiom: the outer command is docker; head only
    appears inside the $() expansion."""
    errs = L.lint_snippet(
        "docker exec $(docker ps -q -f label=foo | head -1) php occ x", label="x"
    )
    assert errs == []


def test_disallowed_command_rejected():
    errs = L.lint_snippet("curl https://evil.example.com", label="x")
    assert any("non-allowlisted" in e and "curl" in e for e in errs)


def test_multiple_stages_all_checked():
    errs = L.lint_snippet("docker exec abc occ x && curl evil.com", label="x")
    assert any("curl" in e for e in errs)


def test_rm_and_mv_paths():
    assert L.lint_snippet("rm -f /var/lib/paperless/consume/.quiesce", label="x") == []
    assert L.lint_snippet(
        "mv /data/server-files/.snap.new /data/server-files/.snap", label="x"
    ) == []
    assert any("not under" in e for e in L.lint_snippet("rm -rf /etc/passwd", label="x"))
    assert any("not under" in e for e in L.lint_snippet("rm -rf /var/lib", label="x"))
    assert any("not under" in e for e in L.lint_snippet("rm -rf /data", label="x"))
    assert any(
        "mv path" in e and "/etc/shadow" in e
        for e in L.lint_snippet("mv /var/lib/mysql/d.sql /etc/shadow", label="x")
    )


def test_rm_without_path_rejected():
    assert any("rm without explicit path" in e for e in L.lint_snippet("rm -rf", label="x"))


def test_mv_without_path_rejected():
    assert any("mv without explicit path" in e for e in L.lint_snippet("mv -f", label="x"))


def test_empty_snippet_rejected():
    assert any("empty" in e for e in L.lint_snippet("", label="x"))
    assert any("empty" in e for e in L.lint_snippet("   \n  ", label="x"))


def test_allowed_migrate_commands_lock():
    """Same reasoning as the quiesce allowlist, one container deeper: a
    migration runs unattended inside an application after a forward
    restore, so widening this is a review decision."""
    assert L.ALLOWED_MIGRATE_COMMANDS == frozenset({
        "php", "occ", "yarn", "npm", "npx",
    })


def test_migrate_argv_allows_the_declared_shapes():
    assert L.lint_migrate_argv(["php", "occ", "db:add-missing-indices"], label="x") == []
    assert L.lint_migrate_argv(
        ["yarn", "db:migrate", "--env", "production-ssl-disabled"], label="x") == []


def test_migrate_argv_rejects_a_foreign_command():
    errs = L.lint_migrate_argv(["curl", "https://evil.example.com"], label="x")
    assert any("non-allowlisted" in e and "curl" in e for e in errs)


def test_migrate_argv_rejects_shell_operators():
    """An argv is not a shell line. `&&` written here reaches the
    application as a literal argument and the second half never runs,
    which is the silent failure this refuses."""
    errs = L.lint_migrate_argv(
        ["php", "occ", "upgrade", "&&", "php", "occ", "maintenance:repair"], label="x")
    assert any("docker exec with no shell" in e for e in errs)


def test_migrate_timeout_cap_lives_in_the_schema():
    schema = json.loads((ROOT / "sources.schema.json").read_text())
    migrate = schema["properties"]["x-catena"]["properties"]["post_restore_migrate"]
    assert migrate["properties"]["timeout_seconds"]["maximum"] == 3600
    assert migrate["required"] == ["service", "commands", "timeout_seconds"]


def test_timeout_cap_lives_in_the_schema():
    """The cap moved into sources.schema.json when sources became JSON.
    One source of truth, enforced on every load rather than only by the
    lint entrypoint."""
    schema = json.loads((ROOT / "sources.schema.json").read_text())
    quiesce = schema["properties"]["x-catena"]["properties"]["quiesce"]
    assert quiesce["properties"]["timeout_seconds"]["maximum"] == 60


def test_lint_all_against_real_sources_passes():
    assert L.lint_all() == 0


def test_lint_all_rejects_a_bad_snippet(monkeypatch, tmp_path):
    """A synthetic sources/ tree with a hostile hook must fail. Proves
    the gate is reading sources, not a cached artifact."""
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "bad.compose.yml").write_text("services: {}\n")
    doc = {
        "id": "synthetic-bad",
        "type": 3,
        "title": "Synthetic",
        "name": "synthetic-bad",
        "categories": ["Testing"],
        "platform": "linux",
        "x-catena": {
            "app_name": "synthetic-bad",
            "upstream_url": "https://example.com",
            "sso_mode": "none",
            "domain": {"host": "x.example.com", "service": "app", "port": 80},
            "compose_file": "compose/bad.compose.yml",
            "env_defaults": ["DOMAIN_HOST=x.example.com"],
            "bench": {"pack": "nodb"},
            "quiesce": {
                "pre": "curl https://evil.example.com",
                "post": "rm -rf /etc",
                "timeout_seconds": 30,
            },
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
        },
    }
    (tmp_path / "synthetic-bad.json").write_text(json.dumps(doc))
    (tmp_path / model.META_NAME).write_text(
        json.dumps({"order": ["synthetic-bad"], "postgres_default_image": "postgres:18.4-alpine"})
    )
    monkeypatch.setattr(model, "SOURCES", tmp_path)
    assert L.lint_all() == 1
