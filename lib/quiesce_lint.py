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
  - Each volume a lifecycle block declares versioned is a top-level
    volume of the compose that a service mounts, and no database and no
    one-shot service mounts it. A rollback puts the volume's pre-update
    copy back with the services that mount it scaled to zero; it never
    stops a database, whose dump it replays instead, nor a one-shot,
    which would run again if brought back. The host refuses every update
    of a stack where one of them mounts the volume.

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


# The database engines the host dumps and replays, matched on the image name
# as catena-admin payload/engines/appdb Family matches them.
DATABASE_IMAGE_WORDS = ("mariadb", "mysql", "postgres")


def database_image(image: str) -> bool:
    name = str(image).lower().split("@", 1)[0]
    if name.rfind(":") > name.rfind("/"):
        name = name[:name.rfind(":")]
    return any(word in name for word in DATABASE_IMAGE_WORDS)


def mounted_volumes(service: dict) -> set[str]:
    """The source of each volume entry of a compose service: the part before
    the first colon of the short form, the `source` of the long form."""
    sources = set()
    for item in service.get("volumes") or []:
        if isinstance(item, str):
            sources.add(item.split(":", 1)[0])
        elif isinstance(item, dict) and item.get("source"):
            sources.add(str(item["source"]))
    return sources


def lint_versioned_volumes(entry, volumes: list[str]) -> list[str]:
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8")) or {}
    declared = doc.get("volumes") or {}
    services = doc.get("services") or {}
    errors: list[str] = []
    for volume in volumes:
        label = f"{entry.slug}.lifecycle.versioned_volumes: {volume!r}"
        if volume not in declared:
            errors.append(f"{label} is not a top-level volume of this template's compose")
        mounting = [name for name, svc in services.items()
                    if volume in mounted_volumes(svc or {})]
        if not mounting:
            errors.append(f"{label} is mounted by no service")
        for name in mounting:
            svc = services[name] or {}
            if database_image(svc.get("image", "")):
                errors.append(
                    f"{label} is mounted by {name}, a database: a rollback "
                    f"replays its dump and never stops it")
            restart = ((svc.get("deploy") or {}).get("restart_policy") or {})
            if restart.get("condition") in ("none", "on-failure"):
                errors.append(
                    f"{label} is mounted by {name}, a one-shot: a rollback "
                    f"never stops it, and bringing it back would run it again")
    return errors


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
            if lifecycle.get("versioned_volumes"):
                all_errors.extend(lint_versioned_volumes(
                    entry, lifecycle["versioned_volumes"]))

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
