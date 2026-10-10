"""Security lint over what a client host acts on unattended (CI gate).

Two families of commands, both declared in `x-catena` and both executed on
a client box with no one present: the quiesce commands the nightly
maintenance runs around its backup, and the lifecycle commands (migration,
before and after an update, and the ready question asked before each of
those). Both are argv arrays the host runs inside one service's container
through `docker exec`, with no shell. And the sign-in labels, from which
the host's settings sync makes a Keycloak client and writes its values into
the app's stack env.

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
    volume of the compose that a service mounts, and no database mounts
    it. A rollback puts the volume's pre-update copy back with the
    services that mount it scaled to zero; it never stops a database,
    whose dump it replays instead, and the host refuses every update of a
    stack where one mounts the volume. A one-shot service, which a
    rollback does not stop either, is refused outright by the swarm lint.
  - Sign-in labels sit on the main address's service, the only service
    the host reads them from: the one service carrying vps.route.host, or,
    of several, the one also carrying vps.route.main=true. Each is one the
    host knows; `vps.auth.oidc=true` comes with the return
    addresses; and the template declares none of the three values the sync
    writes (OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER_URL) in
    env_defaults or env_managed_keys, so each has one writer.

Exit codes: 0 clean, 1 lint failures, 2 structural error.
"""
from __future__ import annotations

import yaml

from .model import SourceError, load_sources

# Each family runs inside one application container and gets that
# application's own admin client. A quiesce may also signal a process of
# that container, for an application with no admin client that can hold its
# writes. Widening either set is a review decision.
ALLOWED_QUIESCE_COMMANDS = frozenset({
    "php",                  # Nextcloud
    "mongosh",              # MongoDB
    "pkill",                # Actual Budget: stops and continues its server
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
    return errors


# The sign-in labels catena-admin's app intent reads, and the stack env keys
# its settings sync writes from the Keycloak client it makes
# (payload/lib/app_intent.py, payload/lib/oidc_clients.py).
SIGNIN_LABELS = frozenset({
    "vps.auth.oidc", "vps.auth.oidc.redirect_uris", "vps.auth.oidc.public",
})
SIGNIN_KEYS = ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL")
_TRUE = ("true", "yes", "1", "on")


def service_labels(service: dict) -> dict[str, str]:
    labels = service.get("labels") or []
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items()}
    return dict((str(item).split("=", 1) + [""])[:2] for item in labels)


def lint_signin(entry) -> list[str]:
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8")) or {}
    services = {name: service_labels(service or {})
                for name, service in (doc.get("services") or {}).items()}
    # The main address as catena-admin payload/lib/app_intent.py row picks it.
    addresses = sorted(name for name, labels in services.items()
                       if labels.get("vps.route.host", "").strip())
    marked = [name for name in addresses
              if services[name].get("vps.route.main", "").strip().lower() in _TRUE]
    main = (addresses[0] if len(addresses) == 1
            else marked[0] if len(marked) == 1 else None)
    if main:
        where = main
    elif addresses:
        where = (f"and the app has none: {len(marked)} of its addresses "
                 f"({', '.join(addresses)}) carry vps.route.main=true, "
                 "where exactly one must")
    else:
        where = "and the app has none: no service carries vps.route.host"
    errors: list[str] = []
    asks = False
    for name, labels in services.items():
        signin = {key for key in labels if key.startswith("vps.auth.oidc")}
        if not signin:
            continue
        label = f"{entry.slug}:{name}"
        for key in sorted(signin - SIGNIN_LABELS):
            errors.append(f"{label}: {key} is not a sign-in label the host reads "
                          f"({', '.join(sorted(SIGNIN_LABELS))})")
        if name != main:
            errors.append(f"{label}: carries sign-in labels, but the host reads them "
                          f"only from the main address's service, {where}")
            continue
        if labels.get("vps.auth.oidc", "").strip().lower() not in _TRUE:
            continue
        asks = True
        if not labels.get("vps.auth.oidc.redirect_uris", "").strip():
            errors.append(f"{label}: vps.auth.oidc=true without "
                          f"vps.auth.oidc.redirect_uris; the sign-in entry would "
                          f"accept no return address")
    if asks:
        declared = {kv.split("=", 1)[0] for kv in entry.catena["env_defaults"]}
        declared |= set(entry.catena.get("env_managed_keys") or [])
        for key in SIGNIN_KEYS:
            if key in declared:
                errors.append(
                    f"{entry.slug}: {key} is declared in env_defaults or "
                    f"env_managed_keys, but the settings sync writes it from the "
                    f"app's sign-in entry; a second writer would undo the first")
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
        all_errors.extend(lint_signin(entry))
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
