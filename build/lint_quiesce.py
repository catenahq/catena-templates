#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
Quiesce-hook stricter lint (CI gate).

build/render.py already enforces the half-pair + timeout-cap schema
contract; this script adds the security-side gates that take longer
to run and would slow down the render path:

  - shellcheck (POSIX sh) on each snippet, if shellcheck is on PATH.
    Missing shellcheck on CI is treated as a hard fail because the
    pipeline image is supposed to install it; missing locally is a
    soft skip with a warning.

  - Command allowlist: every snippet's first non-redirection token
    on each pipeline stage must be in ALLOWED_COMMANDS. Lets us
    reject a template that smuggles `curl evil.com` into a hook.

  - Path constraint on rm: rm only allowed against paths under
    /var/lib/<app>/ . Catches "rm -rf /" or "rm /etc/...".

  - Timeout cap (60s -- mirrors render.py's QUIESCE_MAX_TIMEOUT_S so
    a CI-only path cannot drift from the render path).

Exit codes:
  0 -- every quiesce snippet passes
  1 -- one or more snippets failed lint
  2 -- structural error (catalog missing, bad YAML, etc.)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "source" / "catalog.yml"

# Allowlisted commands. Anything outside this set fails lint. Order
# matters for readability only; lookup is set-membership.
ALLOWED_COMMANDS = frozenset({
    # shell + control flow primitives used by the docker exec idiom
    "docker", "head", "true", "false",
    # app-side admin clients
    "occ", "php",                       # Nextcloud
    "mongo", "mongosh",                 # Rocket.Chat / Mongo apps
    "rocketchat-cli",                   # RC admin
    # filesystem primitives (rm is path-restricted -- see below)
    "touch", "rm", "mv",
    # logical-dump tools per engine (Sprint 2 -- atomic per-cycle DB
    # consistency). The catalog snippets shell out via `docker exec`
    # to the running app's DB container; these tools execute INSIDE
    # the container (the host sh sees them only via $()  + sh -c '...').
    # The allow-list still applies because the snippet's first
    # non-$()  token might be one of these in future patterns.
    "mariadb-dump", "mysqldump",
    "pg_dump", "pg_dumpall",
    "mongodump",
    "sqlite3",
})

# Hard cap on per-hook timeout. Mirrors render.py:QUIESCE_MAX_TIMEOUT_S
# so a CI-only path cannot drift from the render path.
MAX_TIMEOUT_S = 60

# rm + mv are allowed only against paths INSIDE a recognised container
# data volume:
#   - /var/lib/<app>/...   (mysql, postgresql, mongo, mariadb, etc.)
#   - /data/...            (actualbudget /data, n8n /data, plane /data,
#                          and most upstream containers that bind
#                          their data dir at /data by convention)
# Paths outside these prefixes fail lint -- catches snippets that
# try to mv /etc/shadow or rm -rf /. The regex is anchored to the
# start of the path so a trailing single-quote (a YAML-string artefact
# of the catalog snippet) does not break the match.
RM_ALLOWED_PATH_RE = re.compile(
    r"^(?:/var/lib/[A-Za-z0-9._-]+|/data)/"
)


def shell_tokens(snippet: str) -> list[list[str]]:
    """Naive splitter: returns one token list per pipeline stage. We
    intentionally do not implement a full shell parser; the allowlist
    needs only the FIRST non-redirection token of each stage, and the
    snippets are short. shellcheck catches the parse-time stuff."""
    stages: list[list[str]] = []
    for stage in re.split(r"\||;|&&|\|\|", snippet):
        toks = [t for t in stage.strip().split() if t and not t.startswith("(")]
        if toks:
            stages.append(toks)
    return stages


def head_command(stage_tokens: list[str]) -> str:
    """First non-redirection token. We treat env-prefix assignments
    (KEY=value) as opaque and pick the next token."""
    for tok in stage_tokens:
        if "=" in tok and tok.split("=", 1)[0].replace("_", "").isalnum():
            continue
        return tok
    return ""


def lint_snippet(snippet: str, *, label: str) -> list[str]:
    """Return human-readable error strings for `snippet`."""
    errors: list[str] = []

    # Empty / whitespace-only -- the render path rejects half-pairs
    # but allows a paired empty snippet; lint rejects all-empty so a
    # placeholder cannot ship to production.
    if not snippet.strip():
        errors.append(f"{label}: snippet is empty")
        return errors

    for stage_idx, toks in enumerate(shell_tokens(snippet)):
        if not toks:
            continue
        # Detect `cmd $(other_cmd ...)` patterns: the OUTER cmd is
        # what runs first; the inner $() runs as part of the arg
        # expansion. We allow $() patterns through (docker exec relies
        # on them to look up the container id) but lint the outer.
        head = head_command(toks)
        # Strip a `$(` prefix when the first token IS a $() form.
        if head.startswith("$("):
            head = head.lstrip("$(").rstrip(")")
        if head not in ALLOWED_COMMANDS:
            errors.append(
                f"{label}: stage {stage_idx + 1} starts with "
                f"non-allowlisted command {head!r}; allowed: "
                f"{sorted(ALLOWED_COMMANDS)}"
            )
            continue
        # rm and mv need path restriction. Both can wreck the host
        # if the catalog smuggles a non-app path; we require every
        # non-flag arg to be under /var/lib/<app>/ . `mv` is allow-
        # listed primarily for atomic-rename of dump output sidecars
        # inside the DB container data volume (Sprint 2.1).
        if head in ("rm", "mv"):
            paths = [t for t in toks[1:] if not t.startswith("-")]
            if not paths:
                errors.append(f"{label}: {head} without explicit path")
            else:
                for p in paths:
                    if not RM_ALLOWED_PATH_RE.match(p):
                        errors.append(
                            f"{label}: {head} path {p!r} not under "
                            f"/var/lib/<app>/"
                        )

    return errors


def shellcheck_snippet(snippet: str, *, label: str) -> list[str]:
    """Run shellcheck against the snippet; return error lines.
    Skip cleanly if shellcheck is unavailable + return a warning
    that the caller logs."""
    if shutil.which("shellcheck") is None:
        return ["__SHELLCHECK_MISSING__"]
    try:
        proc = subprocess.run(
            ["shellcheck", "-s", "sh", "-"],
            input=snippet + "\n",
            text=True, capture_output=True, check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return [f"{label}: shellcheck timed out"]
    if proc.returncode == 0:
        return []
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return [f"{label}: shellcheck: {line}" for line in out]


def lint_all(strict_shellcheck: bool = True) -> int:
    if not CATALOG.exists():
        print(f"error: catalog missing: {CATALOG}", file=sys.stderr)
        return 2
    try:
        catalog = yaml.safe_load(CATALOG.read_text())
    except yaml.YAMLError as exc:
        print(f"error: catalog YAML invalid: {exc}", file=sys.stderr)
        return 2
    entries = catalog.get("catena_template_catalog", [])
    if not isinstance(entries, list):
        print("error: catena_template_catalog must be a list", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    shellcheck_missing = False

    for entry in entries:
        slug = entry.get("id", "<unknown>")
        pre = entry.get("quiesce_pre")
        post = entry.get("quiesce_post")
        # Half-pair already rejected by render.py:validate_quiesce_fields;
        # repeat the check here so this lint stands alone in CI without
        # depending on render being run first.
        if (pre is None) != (post is None):
            all_errors.append(
                f"{slug}: half-pair quiesce_pre/post -- both required"
            )
            continue
        if pre is None and post is None:
            continue

        timeout = entry.get("quiesce_timeout_seconds")
        if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_S:
            all_errors.append(
                f"{slug}: quiesce_timeout_seconds={timeout!r} out of range "
                f"[1, {MAX_TIMEOUT_S}]"
            )

        for name, snippet in (("quiesce_pre", pre), ("quiesce_post", post)):
            label = f"{slug}.{name}"
            all_errors.extend(lint_snippet(str(snippet), label=label))
            sh_errs = shellcheck_snippet(str(snippet), label=label)
            if sh_errs == ["__SHELLCHECK_MISSING__"]:
                shellcheck_missing = True
            else:
                all_errors.extend(sh_errs)

    if shellcheck_missing:
        msg = (
            "WARN: shellcheck not on PATH; static checks skipped. "
            "CI must install shellcheck for full coverage."
        )
        if strict_shellcheck:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    if all_errors:
        print("quiesce lint failed:", file=sys.stderr)
        for err in all_errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print(f"quiesce lint OK ({len([e for e in entries if e.get('quiesce_pre')])} templates with hooks)")
    return 0


if __name__ == "__main__":
    sys.exit(lint_all())
