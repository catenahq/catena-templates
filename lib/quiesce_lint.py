"""Security lint over the hooks a client host runs unattended (CI gate).

Two families, both declared in `x-catena` and both executed on a client
box with no operator present: the backup quiesce hooks, and the
post-restore migration commands.

Schema-level checks (pairing, timeout cap, argv shape) live in
sources.schema.json and run on every render. This module adds the checks
that are too slow or too external for the render path:

  - shellcheck (POSIX sh) per quiesce snippet when it is on PATH.
    Missing locally is a warning; CI installs it.
  - Command allowlist on the head of each pipeline stage, so a template
    cannot smuggle `curl evil.com` into a hook that runs as root on
    every client host before every backup.
  - Path restriction on rm/mv: inside the app's own data path only.
  - Command allowlist on each migration argv, plus a refusal of shell
    metacharacters there: migrations run through `docker exec` with no
    shell, so an `&&` written by someone thinking in shell is passed to
    the application as a literal argument and does nothing.

Exit codes: 0 clean, 1 lint failures, 2 structural error.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from .model import SourceError, load_sources

ALLOWED_COMMANDS = frozenset({
    # shell + control-flow primitives used by the docker exec idiom
    "docker", "head", "true", "false",
    # app-side admin clients
    "occ", "php",                       # Nextcloud
    "mongo", "mongosh",                 # Rocket.Chat / Mongo apps
    "rocketchat-cli",                   # RC admin
    # filesystem primitives (rm/mv are path-restricted below)
    "touch", "rm", "mv",
    # per-engine logical-dump tools, executed INSIDE the app's DB
    # container via docker exec
    "mariadb-dump", "mysqldump",
    "pg_dump", "pg_dumpall",
    "mongodump",
    "sqlite3",
})

# Migration commands are a separate, tighter allowlist. A quiesce hook
# drives docker from the host; a migration runs inside one application
# container and has no business being anything but that application's own
# schema tool.
ALLOWED_MIGRATE_COMMANDS = frozenset({
    "php", "occ",           # Nextcloud, EspoCRM
    "yarn", "npm", "npx",   # the node applications
})

# Written as shell but executed as argv: these tokens reach the
# application as literal arguments, so the second half of the line never
# runs and the failure is silent.
SHELL_METACHARS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&"})

# rm + mv are allowed only against a recognised container data path:
# /var/lib/<app>/... or /data/... . Anything else fails, which is what
# catches `rm -rf /` and `mv /etc/shadow`.
RM_ALLOWED_PATH_RE = re.compile(r"^(?:/var/lib/[A-Za-z0-9._-]+|/data)/")


def shell_tokens(snippet: str) -> list[list[str]]:
    """One token list per pipeline stage. Deliberately not a shell
    parser: the allowlist needs only the first non-redirection token of
    each stage, and shellcheck covers the parse-level problems."""
    stages: list[list[str]] = []
    for stage in re.split(r"\||;|&&|\|\|", snippet):
        toks = [t for t in stage.strip().split() if t and not t.startswith("(")]
        if toks:
            stages.append(toks)
    return stages


def head_command(stage_tokens: list[str]) -> str:
    """First non-redirection token, skipping KEY=value env prefixes."""
    for tok in stage_tokens:
        if "=" in tok and tok.split("=", 1)[0].replace("_", "").isalnum():
            continue
        return tok
    return ""


def lint_snippet(snippet: str, *, label: str) -> list[str]:
    errors: list[str] = []
    if not snippet.strip():
        return [f"{label}: snippet is empty"]

    for stage_idx, toks in enumerate(shell_tokens(snippet)):
        head = head_command(toks)
        # `cmd $(other)` -- the outer command is the one that runs.
        if head.startswith("$("):
            head = head.lstrip("$(").rstrip(")")
        if head not in ALLOWED_COMMANDS:
            errors.append(
                f"{label}: stage {stage_idx + 1} starts with non-allowlisted "
                f"command {head!r}; allowed: {sorted(ALLOWED_COMMANDS)}"
            )
            continue
        if head in ("rm", "mv"):
            paths = [t for t in toks[1:] if not t.startswith("-")]
            if not paths:
                errors.append(f"{label}: {head} without explicit path")
            for path in paths:
                if not RM_ALLOWED_PATH_RE.match(path):
                    errors.append(
                        f"{label}: {head} path {path!r} not under /var/lib/<app>/ or /data/"
                    )
    return errors


def lint_migrate_argv(argv: list[str], *, label: str) -> list[str]:
    errors: list[str] = []
    head = head_command([str(t) for t in argv])
    if head not in ALLOWED_MIGRATE_COMMANDS:
        errors.append(
            f"{label}: starts with non-allowlisted command {head!r}; "
            f"allowed: {sorted(ALLOWED_MIGRATE_COMMANDS)}"
        )
    for tok in argv:
        if str(tok) in SHELL_METACHARS:
            errors.append(
                f"{label}: contains the shell operator {tok!r}, but migrations "
                f"run through docker exec with no shell. Split it into separate "
                f"commands, which run in order and stop at the first failure"
            )
    return errors


def shellcheck_snippet(snippet: str, *, label: str) -> list[str]:
    if shutil.which("shellcheck") is None:
        return ["__SHELLCHECK_MISSING__"]
    try:
        proc = subprocess.run(
            ["shellcheck", "-s", "sh", "-"],
            input=snippet + "\n",
            text=True, capture_output=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return [f"{label}: shellcheck timed out"]
    if proc.returncode == 0:
        return []
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return [f"{label}: shellcheck: {line}" for line in out]


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    all_errors: list[str] = []
    shellcheck_missing = False
    with_hooks = 0
    with_migrations = 0

    for entry in entries:
        migrate = entry.post_restore_migrate
        if migrate:
            with_migrations += 1
            for idx, argv in enumerate(migrate["commands"]):
                all_errors.extend(lint_migrate_argv(
                    argv, label=f"{entry.slug}.post_restore_migrate[{idx}]"))

        quiesce = entry.quiesce
        if not quiesce:
            continue
        with_hooks += 1
        for name in ("pre", "post"):
            label = f"{entry.slug}.quiesce_{name}"
            snippet = str(quiesce[name])
            all_errors.extend(lint_snippet(snippet, label=label))
            sh_errs = shellcheck_snippet(snippet, label=label)
            if sh_errs == ["__SHELLCHECK_MISSING__"]:
                shellcheck_missing = True
            else:
                all_errors.extend(sh_errs)

    if shellcheck_missing:
        print(
            "WARN: shellcheck not on PATH; static checks skipped. "
            "CI must install shellcheck for full coverage."
        )
    if all_errors:
        print("hook lint failed:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    print(
        f"hook lint OK ({with_hooks} templates with quiesce hooks, "
        f"{with_migrations} with post-restore migrations)"
    )
    return 0
