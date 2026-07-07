"""Unit tests for build/lint_quiesce.py.

The lint script is invoked from CI (build-and-verify.yml in a
follow-up; for now `npm run lint:quiesce` is the manual gate).
These tests cover the assertion contracts directly without needing
shellcheck on the test runner: every lint case takes a snippet
string + label, returns a list of human-readable errors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))

import lint_quiesce as L  # noqa: E402


def test_allowed_commands_lock():
    """Tightening the allowlist must be deliberate -- this test fails
    loud if someone adds a new command, forcing them to update the
    test alongside the lint."""
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
    """Sprint 2 lint coverage: the new dump-tool commands appear as
    inner $()  or as the first token of an inner sh -c '...' stage.
    Lint focuses on the outer (`docker`) for these snippets; assert
    the inner pattern doesn't trip the allowlist when it does appear
    standalone."""
    for cmd in ("mariadb-dump", "pg_dump", "pg_dumpall", "mongodump", "sqlite3"):
        errs = L.lint_snippet(f"{cmd} --help", label="x")
        assert errs == [], (cmd, errs)


def test_mv_under_var_lib_app_passes():
    """quiesce_post rotates the dump file to .prev via mv. mv is
    allow-listed; both paths are the data volume's interior."""
    errs = L.lint_snippet(
        "mv /var/lib/mysql/.catena-dump.sql "
        "/var/lib/mysql/.catena-dump.sql.prev",
        label="x",
    )
    assert errs == [], errs


def test_mv_outside_var_lib_rejected():
    """mv with a path outside the allowed container-data prefixes is
    rejected, same as rm. Catches a snippet that tries to mv
    /etc/shadow."""
    errs = L.lint_snippet(
        "mv /var/lib/mysql/.catena-dump.sql /etc/shadow",
        label="x",
    )
    assert any("mv path" in e and "/etc/shadow" in e for e in errs), errs


def test_paths_under_data_root_pass():
    """The actualbudget upstream image mounts its data at /data.
    /data/<subpath>/ is in the allow-list alongside /var/lib/<app>/."""
    errs = L.lint_snippet(
        "mv /data/server-files/.catena-snapshot.sqlite.new "
        "/data/server-files/.catena-snapshot.sqlite",
        label="x",
    )
    assert errs == [], errs


def test_bare_data_no_subpath_rejected():
    """/data without a subpath does NOT match -- the regex requires
    a trailing slash and at least one subdir."""
    errs = L.lint_snippet("rm -rf /data", label="x")
    assert any("not under" in e for e in errs), errs


def test_mv_without_path_rejected():
    errs = L.lint_snippet("mv -f", label="x")
    assert any("mv without explicit path" in e for e in errs), errs


def test_max_timeout_matches_render():
    """Lint cap mirrors render.py's QUIESCE_MAX_TIMEOUT_S. Two-source
    invariant; lock it here so they cannot drift."""
    from importlib import import_module
    render = import_module("render")
    assert L.MAX_TIMEOUT_S == render.QUIESCE_MAX_TIMEOUT_S == 60


def test_empty_snippet_rejected():
    assert any("empty" in e for e in L.lint_snippet("", label="x"))
    assert any("empty" in e for e in L.lint_snippet("   \n  ", label="x"))


def test_simple_allowed_command_passes():
    errs = L.lint_snippet(
        "docker exec abc occ maintenance:mode --on",
        label="x",
    )
    assert errs == [], errs


def test_pipeline_with_head_passes():
    """The Nextcloud snippet uses a docker $(...|head -1) pattern --
    the OUTER command is docker; head appears in the $() expansion."""
    errs = L.lint_snippet(
        "docker exec $(docker ps -q -f label=foo | head -1) php occ x",
        label="x",
    )
    assert errs == [], errs


def test_disallowed_command_rejected():
    errs = L.lint_snippet("curl https://evil.example.com", label="x")
    assert any("non-allowlisted" in e and "curl" in e for e in errs), errs


def test_rm_without_path_rejected():
    errs = L.lint_snippet("rm -rf", label="x")
    assert any("rm without explicit path" in e for e in errs), errs


def test_rm_outside_var_lib_rejected():
    errs = L.lint_snippet("rm -rf /etc/passwd", label="x")
    assert any("not under" in e and "/etc/passwd" in e for e in errs), errs


def test_rm_at_var_lib_root_rejected():
    """/var/lib without a subdir does NOT match -- the regex requires
    /var/lib/<app>/."""
    errs = L.lint_snippet("rm -rf /var/lib", label="x")
    assert any("not under" in e for e in errs), errs


def test_rm_under_var_lib_app_passes():
    errs = L.lint_snippet("rm -f /var/lib/paperless/consume/.quiesce",
                          label="x")
    assert errs == [], errs


def test_multiple_stages_all_checked():
    """Each stage of a `&&` chain must pass independently."""
    errs = L.lint_snippet(
        "docker exec abc occ x && curl evil.com",
        label="x",
    )
    assert any("curl" in e for e in errs), errs


def test_lint_all_against_real_catalog_passes():
    """The real catalog should always pass its own lint as long as
    every shipped template is well-formed. Treat shellcheck as
    non-strict here so the test does not depend on the dev host
    having shellcheck installed."""
    rc = L.lint_all(strict_shellcheck=False)
    assert rc == 0


def test_lint_all_rejects_synthetic_bad_entry(monkeypatch, tmp_path):
    """Swap the catalog with a synthetic bad entry; lint_all must
    return 1 with errors collected. Restores the path after the test
    via monkeypatch so the real catalog stays intact."""
    bad = tmp_path / "catalog.yml"
    bad.write_text("""---
catena_template_catalog:
  - id: synthetic-bad
    quiesce_pre: "curl https://evil.example.com"
    quiesce_post: "rm -rf /etc"
    quiesce_timeout_seconds: 9999
""")
    monkeypatch.setattr(L, "CATALOG", bad)
    rc = L.lint_all(strict_shellcheck=False)
    assert rc == 1
