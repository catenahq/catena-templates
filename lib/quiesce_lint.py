"""Security lint over the commands a client host runs unattended (CI gate).

Two families, both declared in `x-catena` and both executed on a client
box with no one present: the quiesce commands the nightly maintenance
runs around its backup, and the lifecycle commands (migration, before
and after an update, and the ready question asked before each of those).
Both are argv arrays the host runs inside one service's container
through `docker exec`, with no shell.

Schema-level checks (argv shape, required keys, timeout caps) live in
sources.schema.json and run on every render. This module adds:

  - A command allowlist per family on the first word of each argv, so a
    template cannot smuggle `curl evil.com` into a command that runs on
    every client host.
  - A refusal of shell operators: with no shell, an `&&` written by
    someone thinking in shell reaches the application as a literal
    argument and the second half of the line never runs.
  - The service a quiesce block names is a service the compose defines.
    The host finds the container by that name; a name the compose does
    not define fails the quiesce on every host, and the backup runs
    without it.

Exit codes: 0 clean, 1 lint failures, 2 structural error.
"""
from __future__ import annotations

import yaml

from .model import SourceError, load_sources

# Each family runs inside one application container and gets that
# application's own admin client, nothing else. Widening either set is a
# review decision.
ALLOWED_QUIESCE_COMMANDS = frozenset({
    "php",                  # Nextcloud
    "mongosh",              # MongoDB
})

ALLOWED_MIGRATE_COMMANDS = frozenset({
    "php", "occ",           # Nextcloud, EspoCRM
    "yarn", "npm", "npx",   # the node applications
    "bench",                # Frappe / ERPNext
})

# The argv lists a lifecycle block can carry.
LIFECYCLE_LISTS = ("before_update", "migrate", "after_update")

# Written as shell but executed as argv: these tokens reach the
# application as literal arguments, so the second half of the line never
# runs and the failure is silent.
SHELL_METACHARS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&"})

def lint_migrate_argv(argv: list[str], *, label: str,
                      allowed: frozenset[str]) -> list[str]:
    errors: list[str] = []
    head = str(argv[0])
    if head not in allowed:
        errors.append(
            f"{label}: starts with non-allowlisted command {head!r}; "
            f"allowed: {sorted(allowed)}"
        )
    for tok in argv:
        if str(tok) in SHELL_METACHARS:
            errors.append(
                f"{label}: contains the shell operator {tok!r}, but commands "
                f"run through docker exec with no shell. Split it into separate "
                f"commands, which run in order and stop at the first failure"
            )
    return errors


def compose_services(entry) -> set[str]:
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return set()
    return set(doc["services"])


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    all_errors: list[str] = []
    with_hooks = 0
    with_migrations = 0

    for entry in entries:
        lifecycle = entry.lifecycle
        if lifecycle:
            with_migrations += 1
            for key in LIFECYCLE_LISTS:
                for idx, argv in enumerate(lifecycle.get(key) or []):
                    all_errors.extend(lint_migrate_argv(
                        argv, label=f"{entry.slug}.lifecycle.{key}[{idx}]",
                        allowed=ALLOWED_MIGRATE_COMMANDS))
            if lifecycle.get("ready"):
                all_errors.extend(lint_migrate_argv(
                    lifecycle["ready"], label=f"{entry.slug}.lifecycle.ready",
                    allowed=ALLOWED_MIGRATE_COMMANDS))

        quiesce = entry.quiesce
        if not quiesce:
            continue
        with_hooks += 1
        services = compose_services(entry)
        if quiesce["service"] not in services:
            all_errors.append(
                f"{entry.slug}.quiesce.service: {quiesce['service']!r} is not a "
                f"service in this template's compose ({sorted(services)})"
            )
        for name in ("pre", "post"):
            for idx, argv in enumerate(quiesce.get(name) or []):
                all_errors.extend(lint_migrate_argv(
                    argv, label=f"{entry.slug}.quiesce.{name}[{idx}]",
                    allowed=ALLOWED_QUIESCE_COMMANDS))

    if all_errors:
        print("hook lint failed:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    print(
        f"hook lint OK ({with_hooks} templates with quiesce commands, "
        f"{with_migrations} with lifecycle commands)"
    )
    return 0
