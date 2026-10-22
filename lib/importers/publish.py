"""A wave of imports: how many apps may be imported now, and the pull request
each one becomes.

Updates and vulnerability fixes come first. While a pull request from the
update bots is open (branches renovate/, engine-bump/ and scanctl/), a wave
imports nothing. Otherwise a wave is at most the number asked for, the
number of import pull requests the day still allows, and the number the
open cap still allows. The defaults are conservative; the flags
--daily-limit and --open-limit size them.

An imported app is one branch, import/<id>, one commit and one pull request,
so the scope check holds: a pull request changes one app. The branch starts
at the base branch of a clean checkout. The apps' generated files (the
catalog artifacts) are rendered into the commit.

git and gh run through `run`, a function from an argument list to the
command's output, which the tests replace.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

DAILY_LIMIT = 5
OPEN_LIMIT = 10
IMPORT_PREFIX = "import/"
PRIORITY_PREFIXES = ("renovate/", "engine-bump/", "scanctl/")
GENERATED = ("templates.json", "catalog.json", "index.html")

Run = Callable[[list[str]], str]


class WaveError(Exception):
    pass


def run_command(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WaveError(f"{' '.join(argv[:3])}: {exc}") from exc
    if proc.returncode != 0:
        raise WaveError(f"{' '.join(argv[:3])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


class GitHub:
    """Pull requests of the repository the checkout belongs to, through gh."""

    def __init__(self, run: Run):
        self.run = run

    def pull_requests(self, state: str, search: str = "") -> list[dict[str, Any]]:
        argv = ["gh", "pr", "list", "--state", state, "--limit", "200",
                "--json", "headRefName,createdAt"]
        if search:
            argv += ["--search", search]
        try:
            docs = json.loads(self.run(argv) or "[]")
        except ValueError as exc:
            raise WaveError(f"gh pr list: not JSON: {exc}") from exc
        return [d for d in docs if isinstance(d, dict)]


@dataclass(frozen=True)
class Allowance:
    size: int
    reason: str


def _heads(prs: list[dict[str, Any]], *prefixes: str) -> list[str]:
    return [h for h in (str(p.get("headRefName") or "") for p in prs) if h.startswith(prefixes)]


def allowance(asked: int, daily_limit: int, open_limit: int, open_prs: list[dict[str, Any]],
              recent_prs: list[dict[str, Any]], today: date) -> Allowance:
    """How many apps a wave may import now."""
    waiting = _heads(open_prs, *PRIORITY_PREFIXES)
    if waiting:
        return Allowance(0, f"{len(waiting)} update or fix pull requests are open "
                            f"({', '.join(waiting[:3])}{', ...' if len(waiting) > 3 else ''}): "
                            f"imports wait for them")
    opened = len(_heads(open_prs, IMPORT_PREFIX))
    day = [p for p in recent_prs if str(p.get("createdAt") or "").startswith(today.isoformat())]
    today_count = len(_heads(day, IMPORT_PREFIX))
    room = {"asked": asked, "the daily limit": daily_limit - today_count,
            "the open limit": open_limit - opened}
    binding = min(room, key=lambda k: room[k])
    size = max(0, room[binding])
    return Allowance(size, f"{opened} import pull requests open of {open_limit}, "
                           f"{today_count} opened today of {daily_limit}; bound by {binding}")


class Publisher:
    def __init__(self, run: Run, render: Callable[[], int], base: str = "main", remote: str = "origin"):
        self.run = run
        self.render = render
        self.base = base
        self.remote = remote
        self.home = ""

    def start(self) -> None:
        if self.run(["git", "status", "--porcelain"]).strip():
            raise WaveError("the working tree is not clean: a wave runs in a clean checkout")
        self.run(["git", "fetch", self.remote, self.base])
        self.home = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()

    def publish(self, name: str, title: str, body: str, write: Callable[[], None],
                paths: list[str], render: bool = True) -> str:
        """One branch, one commit and one pull request for `paths`, written by
        `write` on a branch of the base."""
        branch = f"{IMPORT_PREFIX}{name}"
        self.run(["git", "switch", "--create", branch, f"{self.remote}/{self.base}"])
        try:
            write()
            if render and self.render() != 0:
                raise WaveError(f"the render failed for {name}")
            self.run(["git", "add", "--", *paths, *(GENERATED if render else ())])
            self.run(["git", "commit", "--message", title, "--message", body])
            self.run(["git", "push", "--set-upstream", self.remote, branch])
            return self.run(["gh", "pr", "create", "--base", self.base, "--head", branch,
                             "--title", title, "--body", body]).strip()
        finally:
            self.run(["git", "switch", self.home])


def pull_request_title(source: dict[str, Any]) -> str:
    origin = source["x-catena"]["origin"]
    return f"feat(import): {source['id']} from {origin['registry'].split('//')[-1].split('/')[0]}"


def pull_request_body(source: dict[str, Any]) -> str:
    cat = source["x-catena"]
    origin = cat["origin"]
    lines = [f"Imported, untested: {source['title']}.", "",
             f"- registry: {origin['registry']}", f"- entry: {origin['entry']}",
             f"- source: {origin['source']}"]
    for key in ("commit", "path", "project"):
        if origin.get(key):
            lines.append(f"- {key}: {origin[key]}")
    lines += [f"- licence: {origin['licence']}", f"- attribution: {origin['attribution']}"]
    if origin.get("aliases"):
        lines.append(f"- aliases: {', '.join(a['entry'] for a in origin['aliases'])}")
    pending = cat["pending"]
    lines += ["", f"Findings left on the compose file: {len(pending['findings'])}. "
                  f"Choices for a person: {len(pending['to_choose'])}. "
                  f"Both are in x-catena.pending."]
    return "\n".join(lines)


def alias_body(records: list[dict[str, str]]) -> str:
    lines = ["Entries that are an app the catalog already has; they are recorded and not imported.", ""]
    lines += [f"- {r['entry']} ({r['registry']}) is {r['app']}" for r in records]
    return "\n".join(lines)
