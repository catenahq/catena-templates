"""Export an app for bare Docker, and check that the export starts.

`unpack --plain APP --out DIR` reads the app's packed `docker-compose.yml` and writes,
into a folder that does not exist yet and lies outside the repositories:

  docker-compose.yml   the compose with its anchors expanded, the `x-` keys,
                       `catena-network` and `deploy.placement` removed, and the main
                       address's port published on 127.0.0.1 only
  .env                 the block's env keys: generated secrets minted here, settings at
                       their default, host-provided values empty (or asked), mode 0600
  <hook>.sh            one script per hook of the block and `migrate.sh`, each running
                       `docker exec` in the container of the hook's service
  hooks/<hook>.<ext>   a hook script body, each `$` written once

An app whose block asks for the host network or a certificate is refused: it needs what
Catena does for it. `plain-check APP` exports into a temporary folder, starts it, waits
for any HTTP answer on the published port and removes everything it started.
"""
from __future__ import annotations

import argparse
import getpass
import http.client
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .importers.pipeline import dump_compose, load_compose
from .model import BLUEPRINTS, derive_domain
from .pack import COMPOSE, HOOKS_DIR, INTERPRETERS

ROOT = Path(__file__).resolve().parent.parent
BLOCK_KEY = "x-catena"
NETWORK = "catena-network"
HOOK_NAMES = ("configure", "verify", "users", "scan", "reconcile")
# Hooks whose script reads standard input, and the one that is given no env value.
STDIN_HOOKS = ("users", "scan")
NO_ENV_HOOKS = ("scan",)
ALPHABETS = {"alnum": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "hex": "0123456789abcdef"}
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@%+=,-]*")
_APP = re.compile(r"[a-z0-9][a-z0-9-]*")
READY_POLL_SECONDS = 3


class PlainError(Exception):
    """An export that cannot be made."""


@dataclass
class Export:
    files: dict[str, tuple[str, int]] = field(default_factory=dict)  # path: (text, mode)
    service: str | None = None
    port: int | None = None
    to_fill: list[str] = field(default_factory=list)


def _dotenv(key: str, value: str) -> str:
    if _PLAIN_VALUE.fullmatch(value):
        return value
    if "'" in value or "\n" in value:
        raise PlainError(f"{key}: a value with a quote or a line break cannot be written to .env")
    return f"'{value}'"


def _env_lines(block: dict[str, Any], ask: Callable[[str], str] | None) -> tuple[str, list[str]]:
    lines, to_fill = [], []
    for key, spec in (block.get("env") or {}).items():
        kind, body = next(iter(spec.items()))
        if kind == "generated":
            alphabet = ALPHABETS[body["alphabet"]]
            value = "".join(secrets.choice(alphabet) for _ in range(body["length"]))
        else:
            value = str(body["default"] if kind == "setting" else body["expr"])
        if "{{" in value:
            lines.append(f"# {key}: Catena sets this from {value}")
            to_fill.append(key)
            value = ask(key) if ask else ""
        lines.append(f"{key}={_dotenv(key, value)}")
    return "\n".join(lines) + "\n" if lines else "", to_fill


def _refuse(block: dict[str, Any]) -> None:
    if "host_network" in (block.get("privileges") or []):
        raise PlainError("the app runs on the server's own network, which Catena sets up for it")
    if (block.get("needs") or {}).get("certificate"):
        raise PlainError("the app needs a certificate, which Catena writes for it")


def _compose_for_docker(doc: dict[str, Any], service: str | None, port: int | None) -> dict[str, Any]:
    out = {key: value for key, value in doc.items() if not key.startswith("x-")}
    for spec in out["services"].values():
        nets = spec.get("networks")
        if isinstance(nets, dict):
            nets.pop(NETWORK, None)
        elif isinstance(nets, list):
            nets[:] = [n for n in nets if n != NETWORK]
        if "networks" in spec and not nets:
            del spec["networks"]
        deploy = spec.get("deploy")
        if isinstance(deploy, dict):
            deploy.pop("placement", None)
            if not deploy:
                del spec["deploy"]
    networks = out.get("networks")
    if isinstance(networks, dict):
        networks.pop(NETWORK, None)
        for name, spec in networks.items():
            if isinstance(spec, dict) and spec.get("external"):
                networks[name] = {}
        if not networks:
            del out["networks"]
    if service:
        out["services"][service].setdefault("ports", []).append(f"127.0.0.1:{port}:{port}")
    return out


class _Scripts:
    """The scripts and hook bodies of one export."""

    def __init__(self, export: Export, env_keys: list[str]):
        self.export, self.env_keys = export, env_keys

    def _argv(self, hook: str, ordinal: list[int], argv: list[str]) -> str:
        if len(argv) == 3 and INTERPRETERS.get(argv[0]) and argv[1].startswith("-") and "\n" in argv[2]:
            ordinal[0] += 1
            suffix = "" if ordinal[0] == 1 else f"-{ordinal[0]}"
            name = f"{HOOKS_DIR}/{hook}{suffix}.{INTERPRETERS[argv[0]]}"
            self.export.files[name] = (argv[2].replace("$$", "$").rstrip("\n") + "\n", 0o644)
            return f'{shlex.quote(argv[0])} {shlex.quote(argv[1])} "$(cat {name})"'
        return " ".join(shlex.quote(part) for part in argv)

    def _exec(self, user: str | None, env: bool, stdin: bool) -> str:
        parts = ["docker exec"]
        if stdin:
            parts.append("-i")
        if env:
            parts += [f"-e {key}" for key in self.env_keys]
        if user:
            parts.append(f"-u {shlex.quote(user)}")
        return " ".join(parts) + ' "$cid"'

    def _head(self, what: str, service: str, env: bool, stdin: bool) -> list[str]:
        head = ["#!/bin/sh", f"# {what}, run inside the {service} container.", "set -eu", 'cd "$(dirname "$0")"']
        if env and self.env_keys:
            head += ["set -a", ". ./.env", "set +a"]
        head += [f"cid=$(docker compose ps -q {shlex.quote(service)})",
                 f'[ -n "$cid" ] || {{ echo "{service} is not running" >&2; exit 1; }}']
        if stdin:
            head += ['input=$(mktemp)', "trap 'rm -f \"$input\"' EXIT", 'cat > "$input"']
        return head

    def hook(self, name: str, service: str, user: str | None, argvs: list[list[str]]) -> None:
        env, stdin = name not in NO_ENV_HOOKS, name in STDIN_HOOKS
        lines = self._head(f"The {name} hook", service, env, stdin)
        ordinal = [0]
        for argv in argvs:
            line = f"{self._exec(user, env, stdin)} {self._argv(name, ordinal, argv)}"
            lines.append(line + (' < "$input"' if stdin else ""))
        self.export.files[f"{name}.sh"] = ("\n".join(lines) + "\n", 0o755)

    def migrate(self, spec: dict[str, Any]) -> None:
        exec_ = self._exec(spec.get("user"), False, False)
        lines = self._head("The schema migration", spec["service"], False, False)
        ordinal = [0]
        if spec.get("ready"):
            ready = " ".join(shlex.quote(part) for part in spec["ready"])
            lines += [f'deadline=$(( $(date +%s) + {int(spec["timeout_seconds"])} ))',
                      f"until {exec_} {ready} >/dev/null 2>&1; do",
                      '  if [ "$(date +%s)" -ge "$deadline" ]; then echo "not ready in time" >&2; exit 1; fi',
                      f"  sleep {READY_POLL_SECONDS}", "done"]
        for argv in spec["migrate"]:
            lines.append(f"{exec_} {self._argv('migrate', ordinal, argv)}")
        self.export.files["migrate.sh"] = ("\n".join(lines) + "\n", 0o755)


def build(text: str, ask: Callable[[str], str] | None = None) -> Export:
    """The files of the plain export of a packed compose."""
    try:
        doc = load_compose(text)
    except ValueError as exc:
        raise PlainError(str(exc)) from exc
    block = doc.get(BLOCK_KEY)
    if not isinstance(block, dict):
        raise PlainError(f"the compose has no {BLOCK_KEY} block")
    _refuse(block)
    address, errors = derive_domain(doc)
    if errors:
        raise PlainError(errors[0])
    export = Export(service=address and address["service"], port=address and address["port"])
    env_text, export.to_fill = _env_lines(block, ask)
    export.files[".env"] = (env_text, 0o600)
    export.files[COMPOSE] = (dump_compose(_compose_for_docker(doc, export.service, export.port)), 0o644)
    scripts = _Scripts(export, list((block.get("env") or {})))
    hooks = block.get("hooks") or {}
    for name in HOOK_NAMES:
        if hooks.get(name):
            scripts.hook(name, hooks["service"], hooks.get("user"), hooks[name])
    lifecycle = block.get("lifecycle") or {}
    if lifecycle.get("migrate"):
        scripts.migrate(lifecycle)
    return export


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def write(out: Path, export: Export, roots: list[Path]) -> None:
    """Create `out` (it must not exist, nor lie inside a repository) and write the files."""
    target = out.resolve()
    if any(_inside(target, root.resolve()) for root in roots):
        raise PlainError(f"{out} lies inside a repository; choose a folder outside it")
    if out.exists() or out.is_symlink():
        raise PlainError(f"{out} exists already")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o700)
    (out / HOOKS_DIR).mkdir(mode=0o755)
    for name, (text, mode) in export.files.items():
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
    if not any(path.is_file() for path in (out / HOOKS_DIR).iterdir()):
        (out / HOOKS_DIR).rmdir()


def export_app(app: str, out: Path, root: Path | None = None, ask: Callable[[str], str] | None = None) -> Export:
    base = (root / "blueprints") if root else BLUEPRINTS
    try:
        text = (base / app / COMPOSE).read_bytes().decode("utf-8")
    except OSError as exc:
        raise PlainError(f"no compose for {app}: {exc}") from exc
    try:
        export = build(text, ask)
    except PlainError as exc:
        raise PlainError(f"{app}: {exc}") from exc
    write(out, export, [ROOT, base.parent])
    return export


def _ask(key: str) -> str:
    prompt = f"{key}: "
    secret = re.search(r"PASSWORD|SECRET|TOKEN|KEY", key)
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def _docker(out: Path, *args: str) -> int:
    try:
        return subprocess.run(["docker", *args], cwd=out, check=False).returncode
    except OSError as exc:
        raise PlainError(f"docker cannot run: {exc}") from exc


def _answers(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).close()
    except urllib.error.HTTPError:
        return True
    except (OSError, http.client.HTTPException):
        return False
    return True


def plain_check(app: str, root: Path | None = None, timeout: float = 600, interval: float = 5,
                clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> int:
    """Start the app's plain export, wait for any HTTP answer on its port, and always
    run `docker compose down -v`. 0 when the app answered."""
    tmp = Path(tempfile.mkdtemp(prefix="catena-plain-"))
    out = tmp / f"plain-{app}-{secrets.token_hex(3)}"
    try:
        export = export_app(app, out, root)
        if export.port is None:
            raise PlainError(f"{app} has no web address to check")
        try:
            answered = _docker(out, "compose", "up", "-d") == 0
            deadline = clock() + timeout
            while answered and not _answers(export.port):
                if clock() >= deadline:
                    answered = False
                    print(f"no answer on port {export.port} in {timeout:g} seconds", file=sys.stderr)
                else:
                    sleep(interval)
            if not answered:
                _docker(out, "compose", "logs", "--tail", "100")
        finally:
            _docker(out, "compose", "down", "-v")
        return 0 if answered else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pack")
    sub = parser.add_subparsers(dest="command", required=True)
    unpack = sub.add_parser("unpack")
    unpack.add_argument("app")
    unpack.add_argument("--plain", action="store_true", required=True)
    unpack.add_argument("--out", required=True, type=Path)
    unpack.add_argument("--ask", action="store_true")
    sub.add_parser("plain-check").add_argument("app")
    args = parser.parse_args(argv)
    if not _APP.fullmatch(args.app):
        print(f"{args.app!r} is not a plain app id", file=sys.stderr)
        return 1
    try:
        if args.command == "plain-check":
            return plain_check(args.app)
        if args.ask and not sys.stdin.isatty():
            raise PlainError("--ask needs a terminal")
        export = export_app(args.app, args.out, ask=_ask if args.ask else None)
    except (PlainError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Exported {args.app} to {args.out}")
    print(f"Start it: cd {args.out} && docker compose up -d")
    if export.port:
        print(f"Address: http://127.0.0.1:{export.port}")
    if export.to_fill and not args.ask:
        print(f"Fill in .env: {', '.join(export.to_fill)}")
    print("Backups, updates, sign-in, routing and monitoring stay Catena's.")
    return 0
