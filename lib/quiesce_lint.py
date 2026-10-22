"""Security lint over what a client host acts on unattended (CI gate).

Three families of commands, all declared in the compose's `x-catena` block
and all executed on a client box with no one present: the quiesce commands the nightly
maintenance runs around its backup, the lifecycle commands (migration,
before and after an update, and the ready question asked before each of
those) and the hooks (configure, verify, users). All are argv arrays the host
runs inside one service's container through `docker exec`, with no shell of
their own. And the sign-in labels, from which
the host's settings sync makes a Keycloak client and writes its values into
the app's stack env. And what the registry importer left on an imported
entry, which reaches every host's app catalog.

Schema-level checks (argv shape, required keys including the block's
`schema`, timeout caps) live in block.schema.json and sources.schema.json and
run on every render. This module adds:

  - A block holds no `$` and names no image of its compose: Docker
    interpolates every value under a top-level x- key when it deploys the
    file, and the update lane rewrites each image reference it finds. A hooks
    body writes each `$` as `$$`.
  - A host_paths entry is a normalised absolute path other than `/` that
    holds neither the Docker socket's folders nor the compose folders.
  - A hooks body is offered to the interpreter it names (`sh -n`, `bash -n`
    or Python's compiler), as an inline script of a compose service is, and
    runs in a service the compose defines.
  - A managed env expression reads no store secret the block's `secrets` does
    not list, and none on the deny list.
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
  - Every lifecycle block declares ready, and an object_store block names
    env keys the source lists and the compose reads, with a switch that runs
    in a service the compose defines.
  - A vps.auto-update label holds a policy the update engine knows.
  - Sign-in labels sit on the main address's service, the only service
    the host reads them from: the one service carrying vps.route.host, or,
    of several, the one also carrying vps.route.main=true. Each is one the
    host knows; `vps.auth.oidc=true` comes with the return
    addresses; and the template declares none of the three values the sync
    writes (OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER_URL) in
    the block's env, so each has one writer.
  - An imported entry's pending findings hold no app checker error and no
    record that the checker did not run: catena-admin CI runs
    catena-applint --catalog over every blueprint and fails on an error.

Exit codes: 0 clean, 1 lint failures, 2 structural error.
"""
from __future__ import annotations

import posixpath
import re

import yaml

from .importers.pipeline import CHECKER_ERROR, CHECKER_NOT_RUN
from . import swarm_lint
from .model import SourceError, load_sources

# Each family runs inside one application container and gets that
# application's own admin client. A quiesce may also signal a process of
# that container, for an application with no admin client that can hold its
# writes. Widening either set is a review decision.
ALLOWED_QUIESCE_COMMANDS = frozenset({
    "php",                  # Nextcloud
    "mongosh",              # MongoDB
    "pkill",                # Actual Budget, Roundcube: stop and continue their servers
})

ALLOWED_MIGRATE_COMMANDS = frozenset({
    "php", "occ",           # Nextcloud, EspoCRM
    "node", "yarn", "npm", "npx",   # the node applications
    "bench",                # Frappe / ERPNext
})

# The ready question is a probe of the service's own server, which may be a
# bash /dev/tcp check where the image has no other client.
ALLOWED_READY_COMMANDS = ALLOWED_MIGRATE_COMMANDS | {"bash"}

# The placeholders an object-store switch command can carry.
SWITCH_PLACEHOLDERS = frozenset({"{old_bucket}", "{new_bucket}"})

# The commands a hooks body may start with: the lifecycle commands and the
# interpreters of an inline script.
ALLOWED_HOOK_COMMANDS = ALLOWED_MIGRATE_COMMANDS | {"sh", "bash", "python", "python3"}

# The argv lists a hooks block can carry.
HOOK_LISTS = ("configure", "verify", "users")

# Store secrets no managed expression reads: they open the host itself. The
# same list is the root-side authority in catena-admin payload/engines/appenv
# (DeniedSecrets), and a test there holds the two equal.
DENIED_SECRETS = frozenset({
    "admin_password", "backup_restic_password", "backup_s3_access_key", "backup_s3_secret_key",
    "beszel_gate_cookie_secret", "beszel_oidc_client_secret", "beszel_universal_token",
    "catena_admin_gate_cookie_secret", "catena_admin_gate_secret", "catena_admin_panel_client_secret",
    "catena_admin_session_key", "catena_identity_probe_client_secret", "catena_license",
    "catena_marketplace_token", "catena_postgres_password", "cloudflare_api_token",
    "cloudflare_api_tokens", "console_recovery_password", "dashboard_sync_client_secret",
    "gatus_gate_cookie_secret", "headscale_api_key", "headscale_preauth_key",
    "healthchecks_api_key_readonly", "healthchecks_api_key_readwrite",
    "healthchecks_gate_cookie_secret", "healthchecks_ping_key", "healthchecks_secret_key",
    "healthchecks_superuser_password", "keycloak_automation_client_secret", "keycloak_db_password",
    "oauth2_proxy_client_secret", "oauth2_proxy_cookie_secret", "storage_bulk_password",
    "storage_bulk_username", "tailscale_oauth_client_id", "tailscale_oauth_client_secret", "zap_api_key",
})

# Names that start like one of these are denied too: one cookie secret per
# domain.
DENIED_SECRET_PREFIXES = ("oauth2_proxy_cookie_secret_",)

# A name in an expression is a store secret when it ends like one (readme.py
# recognises them the same way).
SECRET_NAME = re.compile(r"_(?:secret|password)$")
EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
QUOTED = re.compile(r"'[^']*'")
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
# A value another application provides: provided.<capability>.<name>.
PROVIDED = re.compile(r"\bprovided\.([a-z]+)\.([a-z][a-z0-9_]*)")
EXPRESSION_WORDS = frozenset({"default", "lower", "lookup", "password"})

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
    "vps.auth.oidc.scopes", "vps.auth.oidc.introspect",
})
# A client scope name oidc_clients.SCOPE_RE accepts.
SIGNIN_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
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
        if labels.get("vps.auth.oidc.introspect", "").strip().lower() in _TRUE and \
                labels.get("vps.auth.oidc.public", "").strip().lower() in _TRUE:
            errors.append(f"{label}: vps.auth.oidc.introspect=true on a public sign-in "
                          f"entry; a public entry has no secret to authenticate with, "
                          f"so the host refuses it")
        for scope in (s.strip() for s in labels.get("vps.auth.oidc.scopes", "").split(",")):
            if scope and not SIGNIN_SCOPE.match(scope):
                errors.append(f"{label}: vps.auth.oidc.scopes names {scope!r}, which is "
                              f"not a client scope name (letters, digits and . _ : -)")
    if asks:
        for key in SIGNIN_KEYS:
            if key in entry.env:
                errors.append(
                    f"{entry.slug}: {key} is declared in the block's env, but the "
                    f"settings sync writes it from the app's sign-in entry; a "
                    f"second writer would undo the first")
    return errors


def block_strings(node, path: str = ""):
    """Every key and string value of a block, with the path it sits at."""
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            yield where, str(key)
            yield from block_strings(value, where)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from block_strings(value, f"{path}[{idx}]")
    elif isinstance(node, str):
        yield path, node


def tagged_images(services: dict) -> set[str]:
    """The image references of a compose's services that carry a tag and no
    digest: what the update lane rewrites."""
    images = set()
    for service in services.values():
        image = str((service or {}).get("image", "")).split("@", 1)[0]
        if ":" in image.rsplit("/", 1)[-1]:
            images.add(image)
    return images


def lint_block(entry) -> list[str]:
    if not entry.block:
        return []
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8")) or {}
    images = tagged_images(doc.get("services") or {})
    errors: list[str] = []
    for where, text in block_strings(entry.block):
        label = f"{entry.slug}.x-catena.{where}"
        if where.startswith("hooks."):
            text_checked = text.replace("$$", "")
            hint = " (write $$ for a literal $)"
        else:
            text_checked = text
            hint = ""
        if "$" in text_checked:
            errors.append(f"{label}: holds a $, which Docker reads as a variable "
                          f"when it deploys the file{hint}")
        for image in sorted(images):
            if image in text:
                errors.append(f"{label}: names the image {image}, which an update "
                              f"rewrites wherever the file names it")
    return errors


def expression_names(expr: str) -> set[str]:
    """The bare names the `{{ ... }}` parts of an expression read."""
    names: set[str] = set()
    for body in EXPRESSION.findall(expr):
        for name in IDENTIFIER.findall(PROVIDED.sub("", QUOTED.sub("", body))):
            if name not in EXPRESSION_WORDS:
                names.add(name)
    return names


def provided_reads(expr: str) -> set[tuple[str, str]]:
    """The (capability, name) pairs the `{{ ... }}` parts of an expression read
    from the applications that provide them."""
    return {m.groups() for body in EXPRESSION.findall(expr) for m in PROVIDED.finditer(QUOTED.sub("", body))}


def lint_provided(entries) -> list[str]:
    """A block provides only values that are keys of its env, and a managed
    expression reads only values some template provides."""
    provides: dict[str, set[str]] = {}
    errors: list[str] = []
    for entry in entries:
        for capability, given in (entry.block.get("provides") or {}).items():
            for name, key in given.items():
                if key not in entry.env:
                    errors.append(f"{entry.slug}.x-catena.provides.{capability}.{name}: "
                                  f"{key!r} is not in the block's env")
                provides.setdefault(capability, set()).add(name)
    for entry in entries:
        for key, expr in entry.managed_env.items():
            for capability, name in sorted(provided_reads(expr)):
                if name not in provides.get(capability, set()):
                    errors.append(f"{entry.slug}.x-catena.env.{key}.managed.expr: no template provides "
                                  f"{name!r} of {capability!r}")
    return errors


def lint_managed_secrets(entry) -> list[str]:
    """A managed expression reads only the store secrets the block lists in
    `secrets`, and never one on the deny list."""
    allowed = set(entry.block.get("secrets") or [])
    errors: list[str] = []
    for key, expr in entry.managed_env.items():
        label = f"{entry.slug}.x-catena.env.{key}.managed.expr"
        for name in sorted(expression_names(expr)):
            if name in DENIED_SECRETS or name.startswith(DENIED_SECRET_PREFIXES):
                errors.append(f"{label}: reads {name!r}, a secret no managed expression may read")
            elif SECRET_NAME.search(name) and name not in allowed:
                errors.append(f"{label}: reads the store secret {name!r}, which the block's "
                              f"secrets does not list")
    return errors


def lint_hooks(entry) -> list[str]:
    """The hooks run in a service the compose defines, as argv whose first
    word the allowlist knows, with no shell operator, and an inline script
    parses in the interpreter it names."""
    hooks = entry.block.get("hooks")
    if not hooks:
        return []
    errors: list[str] = []
    services = compose_services(entry)
    if hooks["service"] not in services:
        errors.append(
            f"{entry.slug}.hooks.service: {hooks['service']!r} is not a "
            f"service in this template's compose ({sorted(services)})")
    for name in HOOK_LISTS:
        for idx, argv in enumerate(hooks.get(name) or []):
            label = f"{entry.slug}.hooks.{name}[{idx}]"
            errors.extend(lint_migrate_argv(argv, label=label, allowed=ALLOWED_HOOK_COMMANDS))
            errors.extend(swarm_lint.lint_entrypoint(argv, label=label))
    return errors


def lint_object_store(entry) -> list[str]:
    """The env keys an object_store block names are stack env keys the
    source lists and the compose reads, and its switch runs in a service the
    compose defines, as allowlisted argv with only the placeholders the host
    fills."""
    store = entry.object_store
    if not store:
        return []
    errors: list[str] = []
    text = entry.compose_path.read_text(encoding="utf-8")
    for setting, key in store["env"].items():
        label = f"{entry.slug}.object_store.env.{setting}"
        if key not in entry.env:
            errors.append(f"{label}: {key!r} is not in the block's env")
        if "${" + key not in text:
            errors.append(f"{label}: the compose never reads {key!r}")
    switch = store.get("switch")
    if not switch:
        return errors
    services = compose_services(entry)
    if switch["service"] not in services:
        errors.append(
            f"{entry.slug}.object_store.switch.service: {switch['service']!r} is not a "
            f"service in this template's compose ({sorted(services)})")
    for idx, argv in enumerate(switch["commands"]):
        label = f"{entry.slug}.object_store.switch.commands[{idx}]"
        errors.extend(lint_migrate_argv(argv, label=label, allowed=ALLOWED_MIGRATE_COMMANDS))
        for tok in argv:
            for found in re.findall(r"\{[^{}]*\}", str(tok)):
                if found not in SWITCH_PLACEHOLDERS:
                    errors.append(
                        f"{label}: {found!r} is not a placeholder the host fills "
                        f"({', '.join(sorted(SWITCH_PLACEHOLDERS))})")
    return errors


# The values of the vps.auto-update label the update engine reads
# (catena-admin payload/engines/stackupdate/policy.go); any other value it
# silently replaces with its default.
AUTO_UPDATE_POLICIES = frozenset({"off", "patch", "minor", "patch+minor", "major"})


def lint_auto_update(entry) -> list[str]:
    doc = yaml.safe_load(entry.compose_path.read_text(encoding="utf-8")) or {}
    errors = []
    for name, service in (doc.get("services") or {}).items():
        value = service_labels(service or {}).get("vps.auto-update")
        if value is not None and value.strip().lower() not in AUTO_UPDATE_POLICIES:
            errors.append(f"{entry.slug}:{name}: vps.auto-update={value} is not one of "
                          f"{', '.join(sorted(AUTO_UPDATE_POLICIES))}; the update engine "
                          f"would ignore it")
    return errors


# Host areas no host_paths entry may name or contain: the app checker refuses a
# bind of any of them whatever a block declares (catena-admin payload/engines/
# applint, X6 and X12).
HOST_PATH_FORBIDDEN = ("/var/run", "/run", "/srv/catena/compose")


def lint_host_paths(entry) -> list[str]:
    """Each host_paths entry is a normalised absolute path other than `/` that
    holds none of the areas the app checker refuses for every app."""
    errors: list[str] = []
    for idx, declared in enumerate(entry.block.get("host_paths") or []):
        label = f"{entry.slug}.x-catena.host_paths[{idx}]"
        if posixpath.normpath(declared) != declared or declared == "/":
            errors.append(f"{label}: {declared!r} is not a normalised absolute path other than /")
        for area in HOST_PATH_FORBIDDEN:
            if declared == area or declared.startswith(area + "/") or area.startswith(declared.rstrip("/") + "/"):
                errors.append(f"{label}: {declared!r} holds {area}, which the app checker "
                              f"refuses for every app")
    return errors


def lint_pending(entry) -> list[str]:
    label = f"{entry.slug}.pending.findings"
    errors: list[str] = []
    for finding in entry.catena.get("pending", {}).get("findings", []):
        if finding.startswith(CHECKER_NOT_RUN):
            errors.append(
                f"{label}: the app checker did not run on this entry: run catena-applint "
                f"--fix --nodes 2 --name {entry.catena['app_name']} on its compose and record "
                f"the findings it leaves in place of this line, or drop the entry")
        elif CHECKER_ERROR.match(finding):
            errors.append(
                f"{label}: {finding!r} is an app checker error, which fails catena-admin CI "
                f"(catena-applint --catalog): fix it and delete the line, or drop the entry")
    return errors


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    all_errors: list[str] = []
    with_hooks = 0
    with_lifecycle = 0

    for entry in entries:
        all_errors.extend(lint_signin(entry))
        all_errors.extend(lint_pending(entry))
        all_errors.extend(lint_block(entry))
        all_errors.extend(lint_host_paths(entry))
        all_errors.extend(lint_auto_update(entry))
        all_errors.extend(lint_object_store(entry))
        all_errors.extend(lint_hooks(entry))
        all_errors.extend(lint_managed_secrets(entry))
        lifecycle = entry.lifecycle
        if lifecycle:
            with_lifecycle += 1
            for key in LIFECYCLE_LISTS:
                for idx, argv in enumerate(lifecycle.get(key) or []):
                    all_errors.extend(lint_migrate_argv(
                        argv, label=f"{entry.slug}.lifecycle.{key}[{idx}]",
                        allowed=ALLOWED_MIGRATE_COMMANDS))
            if lifecycle.get("ready"):
                all_errors.extend(lint_migrate_argv(
                    lifecycle["ready"], label=f"{entry.slug}.lifecycle.ready",
                    allowed=ALLOWED_READY_COMMANDS))
            else:
                all_errors.append(
                    f"{entry.slug}.lifecycle.ready: missing; every lifecycle declares the "
                    f"command that exits 0 once its service can run the lifecycle commands")
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

    all_errors.extend(lint_provided(entries))
    if all_errors:
        print("hook lint failed:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    print(
        f"hook lint OK ({with_hooks} templates with quiesce commands, "
        f"{with_lifecycle} with a lifecycle block)"
    )
    return 0
