"""Swarm-compatibility gate over the catalog's compose sources (CI gate).

Every template in this catalog is a Portainer type-2 (swarm stack) entry, so
each `sources/compose/<id>.compose.yml` is read by `docker stack deploy`, not
by `docker compose up`. The two loaders accept overlapping but different
files, and the ways they differ are not symmetric:

  REFUSED   the swarm loader rejects the file and nothing deploys. Loud, and
            therefore the cheap case.
  WARNED    the file loads, `docker stack deploy` prints "Ignoring unsupported
            options: <key>", and the key does not reach the service. A client
            deploying from the Portainer UI never sees that line.
  DROPPED   the file loads, the deploy says NOTHING, and the key still does
            not reach the service.

The last two are the reason this module exists. A `depends_on` in a swarm
stack file is accepted in silence and ignored: the ordering it declares does
not happen, no warning is printed, and the template reads as if the ordering
were still there. That is a dead control -- a declaration nothing honours --
and it is worse here than in a settings form, because what it silently stops
protecting is an application's first-run install against a database that is
not up yet.

Every key below was classified by deploying it to a real single-node swarm
and reading the resulting service spec, not from documentation.

Three positive requirements go with the bans:

  restart-policy   `restart:` is gone, so every service states what happens
                   when it dies, in `deploy.restart_policy.condition`.
  data-pinning     a service that mounts a named volume or a host path is
                   pinned to node.labels.catena.role==data. At one node this
                   is a no-op; the moment a second node joins, swarm may
                   schedule the service onto it and CREATE the missing volume
                   there -- empty, and without an error. The same rule the
                   operator's own swarm services carry (ops
                   automation/audit/swarm_rules.py), applied to the catalog.
  host-mode ports  a published port goes through the swarm ingress mesh by
                   default, which SNATs the client address to a mesh address.
                   Every port this catalog publishes belongs to a service that
                   needs the real peer address (SMTP spam scoring and
                   fail2ban, media relays picking ICE candidates), so ingress
                   is never the intent and `mode: host` is required.

One more check rides along, because the swarm move is what created the need
for it. A swarm stack file has no inline `configs` content -- `configs`
accepts a file read from beside the compose (which does not exist when
Portainer deploys a posted string) or an external object -- so config files
that used to be declared are now written by the service's own entrypoint.
Those scripts live inside a YAML block scalar inside a file that is then
interpolated, which puts them out of reach of every editor and every other
check. `lint_entrypoints` reconstructs each one the way compose will and
offers it to the interpreter it names.

Exit codes: 0 clean, 1 lint failures, 2 structural error.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile

import yaml

from .model import SourceError, load_sources

# The node label roles/docker puts on the data node in catena-ce. Spelled out
# rather than imported: this repo does not depend on catena-ce, and the ops
# audit's label-agreement rule is what keeps the two copies honest.
DATA_NODE_CONSTRAINT = "node.labels.catena.role==data"

# The file does not load at all. `docker stack config` reports these, so they
# are also caught by the optional leg below; naming them here means the error
# says which key rather than "forbidden properties".
REFUSED = {
    "mem_limit": "use deploy.resources.limits.memory",
    "memswap_limit": "swarm has no swap limit; use deploy.resources.limits.memory",
    "cpus": "use deploy.resources.limits.cpus",
    "cpu_shares": "use deploy.resources.limits.cpus",
    "volumes_from": "name the volume in this service's own volumes: block",
    "profiles": "a swarm stack has no profiles; split the file or drop it",
    "scale": "use deploy.replicas",
}

# The file loads and the key does not reach the service. WARNED entries print
# a line on `docker stack deploy` that a client deploying from the Portainer
# UI never sees; DROPPED entries print nothing anywhere.
DROPPED = {
    "depends_on": (
        "swarm has no start ordering and drops this in SILENCE. Give the "
        "dependent service a deploy.restart_policy so swarm retries it, and "
        "where a crash-restart is destructive make the service wait for its "
        "dependency itself"
    ),
    "tmpfs": (
        "the short tmpfs key is dropped in silence. Use a long-form volume "
        "entry (type: tmpfs). Note it mounts noexec and swarm cannot ask "
        "otherwise -- a path that needs exec has to be a named volume"
    ),
    "oom_score_adj": "dropped in silence; swarm has no equivalent",
    "restart": "use deploy.restart_policy",
    "container_name": "swarm names tasks <stack>_<service>.<n>.<task-id>",
    "expose": "every service on the same network already reaches every port",
    "links": "use networks and the service name",
    "external_links": "join the external network instead",
    "build": "a swarm stack deploys images; publish the image first",
    "network_mode": "declare networks:",
    "pid": "swarm has no equivalent",
    "userns_mode": "swarm has no equivalent",
    "cgroup_parent": "swarm has no equivalent",
    "devices": "swarm has no equivalent",
    "privileged": "use cap_add for the specific capability",
    "security_opt": "swarm has no equivalent",
    "domainname": "swarm has no equivalent",
    "shm_size": "swarm has no equivalent",
}

# Long-form volume types that are NOT node-local state: a tmpfs lives and dies
# with the task, so a service mounting only tmpfs needs no placement pin.
EPHEMERAL_MOUNT_TYPES = frozenset({"tmpfs", "npipe"})


def _mounts_state(service: dict) -> bool:
    """Does this service mount something a node has to already hold?"""
    for vol in service.get("volumes") or []:
        if isinstance(vol, str):
            # "name:/path" or "/host:/path"; an anonymous "/path" does not.
            return ":" in vol
        if isinstance(vol, dict):
            if vol.get("type", "volume") not in EPHEMERAL_MOUNT_TYPES:
                return True
    return False


def _pinned(service: dict) -> bool:
    placement = ((service.get("deploy") or {}).get("placement") or {})
    return DATA_NODE_CONSTRAINT in (placement.get("constraints") or [])


def lint_service(name: str, service: dict, *, label: str) -> list[str]:
    errors: list[str] = []
    where = f"{label}:{name}"

    for key, why in REFUSED.items():
        if key in service:
            errors.append(
                f"{where}: {key} -- the swarm loader refuses the whole file; {why}"
            )
    for key, why in DROPPED.items():
        if key in service:
            errors.append(f"{where}: {key} does not reach a swarm service; {why}")

    deploy = service.get("deploy") or {}
    condition = ((deploy.get("restart_policy") or {}).get("condition"))
    if not condition:
        errors.append(
            f"{where}: no deploy.restart_policy.condition. `restart:` is "
            f"dropped in a swarm stack, so a service that does not say this "
            f"gets swarm's default and nobody chose it"
        )

    if _mounts_state(service) and not _pinned(service):
        errors.append(
            f"{where}: mounts state but is not pinned to "
            f"{DATA_NODE_CONSTRAINT}. Swarm may schedule it onto another node "
            f"and CREATE the missing volume or bind source there, empty and "
            f"without an error"
        )

    for port in service.get("ports") or []:
        if not isinstance(port, dict) or port.get("mode") != "host":
            errors.append(
                f"{where}: published port {port!r} goes through the swarm "
                f"ingress mesh, which replaces the client address with a mesh "
                f"address. Use the long form with mode: host"
            )

    return errors


# `${VAR}` and `${VAR:-default}`, which compose replaces before the script
# ever runs. Filled with a marker rather than removed: an empty string would
# turn `[${FEDERATION_DOMAIN_WHITELIST}]` into valid YAML by accident and a
# bare `${...}` left in place is not valid in any of the languages below.
_COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


_ESCAPED_DOLLAR = "\x00catena-dollar\x00"


def as_compose_writes_it(text: str) -> str:
    """The script as the container receives it.

    `$$` is compose's escape for a literal `$`, and it has to be taken out of
    the way BEFORE the placeholder pass: `$${VAR}` asks for the literal text
    `${VAR}` to reach the shell, and a placeholder pass that ran first would
    substitute it -- turning a runtime variable the script means to expand
    itself into a value baked at deploy time.
    """
    held = text.replace("$$", _ESCAPED_DOLLAR)
    filled = _COMPOSE_VAR.sub("PLACEHOLDER", held)
    return filled.replace(_ESCAPED_DOLLAR, "$")


def lint_entrypoint(argv, *, label: str) -> list[str]:
    """Offer an inline `<interpreter> -c <script>` to its own parser."""
    if not isinstance(argv, list) or len(argv) < 3:
        return []
    interpreter = str(argv[0]).rsplit("/", 1)[-1]
    body = as_compose_writes_it(str(argv[2]))

    if interpreter.startswith("python"):
        try:
            compile(body, label, "exec")
        except SyntaxError as exc:
            return [f"{label}: does not parse as Python: {exc}"]
        return []

    if interpreter not in ("sh", "bash", "ash", "dash"):
        return []
    checker = "bash" if interpreter == "bash" else "sh"
    if shutil.which(checker) is None:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
        handle.write(body)
        handle.flush()
        proc = subprocess.run([checker, "-n", handle.name],
                              capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return []
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return [f"{label}: does not parse as {checker}: {line}" for line in detail]


def lint_configs(doc: dict, *, label: str) -> list[str]:
    """A top-level `configs:` entry may only be `external`.

    `file:` reads a path beside the compose, and neither deploy path has one.
    Portainer's App Templates entry points at `blueprints/<id>/
    docker-compose.yml` and `build/render.py` emits exactly the compose, the
    logo and the quiesce hooks into that directory -- never a sibling asset.
    The API path is worse: a stack created from `StackFileContent` is a posted
    STRING with no directory at all, and that is the path the bench and the
    provisioner both use.

    Neither failure is visible from here. `docker stack config` resolves the
    path against THIS repository, where the file does sit next to the compose,
    so the file lints clean and fails at deploy with `no such file or
    directory` on a host. Content a service needs is written by its own
    entrypoint, which is the form every other template already uses.
    """
    configs = doc.get("configs")
    if not isinstance(configs, dict):
        return []
    errors: list[str] = []
    for name, spec in configs.items():
        if not isinstance(spec, dict):
            errors.append(f"{label}: configs.{name} is not a mapping")
            continue
        if spec.get("external") is True:
            continue
        source = spec.get("file") or spec.get("content")
        errors.append(
            f"{label}: configs.{name} declares {'file' if spec.get('file') else 'content'}"
            f"={source!r}, which no deploy path can resolve -- the blueprint "
            f"directory carries no sibling assets and a stack posted as a "
            f"string has no directory. Write the file from the service's "
            f"entrypoint instead"
        )
    return errors


def lint_compose(body: str, *, label: str) -> list[str]:
    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return [f"{label}: not valid YAML: {exc}"]
    if not isinstance(doc, dict):
        return [f"{label}: top level is not a mapping"]

    errors: list[str] = lint_configs(doc, label=label)
    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        return errors + [f"{label}: declares no services"]
    for name, service in services.items():
        if not isinstance(service, dict):
            errors.append(f"{label}:{name}: service is not a mapping")
            continue
        errors.extend(lint_service(name, service, label=label))
        for key in ("entrypoint", "command"):
            errors.extend(lint_entrypoint(
                service.get(key), label=f"{label}:{name}.{key}"))
    return errors


def stack_config(path, *, label: str) -> list[str]:
    """Offer the file to the real swarm loader, when docker is on PATH.

    The pure-Python rules above are the gate CI depends on -- they are
    deterministic and name the key. This leg catches whatever the rules do not
    know about yet, which is the whole reason to keep it: the ban list was
    written against one docker version and the loader is the authority.
    """
    if shutil.which("docker") is None:
        return ["__DOCKER_MISSING__"]
    try:
        proc = subprocess.run(
            ["docker", "stack", "config", "--skip-interpolation", "-c", str(path)],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [f"{label}: docker stack config timed out"]
    except OSError:
        return ["__DOCKER_MISSING__"]
    if proc.returncode == 0:
        return []
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    # A docker binary with no daemon behind it is a missing tool, not a
    # template defect. Reporting it as a violation would fail every template
    # in the catalog for a reason that is about the machine.
    if any("Cannot connect to the Docker daemon" in line for line in detail):
        return ["__DOCKER_MISSING__"]
    return [f"{label}: docker stack config: {line}" for line in detail]


def lint_all() -> int:
    try:
        entries = load_sources()
    except SourceError as exc:
        print(f"sources are not loadable:\n{exc}")
        return 2

    all_errors: list[str] = []
    docker_missing = False
    services_seen = 0

    for entry in entries:
        label = entry.catena["compose_file"]
        body = entry.compose_path.read_text(encoding="utf-8")
        errors = lint_compose(body, label=label)
        all_errors.extend(errors)

        doc = yaml.safe_load(body)
        if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
            services_seen += len(doc["services"])

        # Only offer a file the rules already accept: `docker stack config`
        # reports one problem per run, so a file with a known violation would
        # spend a round trip re-reporting it in the loader's own wording.
        if errors:
            continue
        loader = stack_config(entry.compose_path, label=label)
        if loader == ["__DOCKER_MISSING__"]:
            docker_missing = True
        else:
            all_errors.extend(loader)

    if docker_missing:
        print(
            "WARN: no usable docker on PATH; the swarm loader leg was "
            "skipped. CI runs it."
        )
    if all_errors:
        print("swarm lint failed:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    print(
        f"swarm lint OK ({len(entries)} templates, {services_seen} services)"
    )
    return 0
