"""App versions are git tags, not strings in files.

A tested app is tagged `<app>/<version>-<revision>` (`nextcloud-s3-oidc/31.0.8-1`).
The version is the numeric part of the image tag of the app's main address
service: the compose block's `version_from` service when it names one, else
the service `sources/<id>.json` names in `x-catena.domain.service`. The
revision is 1 plus the highest revision the app already has at that version,
so a change that leaves the image alone still gets a new tag, and a deleted
tag's revision is never reused while a later one stands. Tags order by
version, then revision; the update engine's full tag rules live in
catena-admin `payload/engines/stackupdate`, and this reads only the number a
catalog pin is required to carry.

A tag a passing app-tests run made is annotated, its message opening with
PASSED; the tags made by hand are not. An app is `tested` when its newest
tag is a passing run's and holds the files (`blueprints/<id>/`,
`sources/<id>.json`) the commit holds, `changed` when those files differ from
its newest tag, and `untested` when it has no tag or its newest was made by
hand. All of it is read from the checkout's git.

  plan APP [--ref REF]  what a pass at commit REF (default HEAD) does to APP's tags:
                        `skip TAG` (tested, TAG is on a later commit, or the checked-out
                        commit holds other files of APP than REF; `skip -` with no tag),
                        `mark TAG` (annotate the hand-made TAG) or `create TAG`
  scope                 changed paths on stdin -> fail when they touch more than one app
  tested [--needs-run]  each app's state at the checked-out commit; with
                        --needs-run, the ids of the apps not `tested`
  same APP --ref SHA --base BASE --from CAND
                        exit 0 when commit CAND holds the change SHA holds: APP's files
                        are the same at both, and the same where each is judged from
                        (the merge base of CAND and BASE, and BASE)
  tested-run APP [--ref REF]
                        the address of the passing run that made APP's newest tag, when
                        APP is `tested` at REF (default HEAD)
  last-change APP [--ref REF]
                        the newest commit on REF's first-parent line (default HEAD) that
                        changed APP's files; nothing when none did

`scope` is the check that keeps a commit or a pull request to one app, so
one change is one tag.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Iterable

import yaml

from . import changes, provenance
from .model import ROOT

BLOCK_KEY = "x-catena"
PASSED = "app-tests passed"
TESTED, CHANGED, UNTESTED = "tested", "changed", "untested"

_NUMERIC = re.compile(r"v?(\d+(?:[._]\d+)*)")
_SHA = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"(?P<version>.+)-(?P<revision>\d+)")


class VersionError(Exception):
    """An app whose version cannot be read."""


def image_tag(ref: str) -> str | None:
    """The tag of an image reference, without a digest."""
    name = ref.split("@", 1)[0]
    if name.rfind(":") > name.rfind("/"):
        return name[name.rfind(":") + 1:]
    return None


def version_of_image(ref: str) -> str:
    """The numeric part a tag opens with: `34.0.5-apache` is `34.0.5`."""
    tag = image_tag(provenance.resolve_ref(ref))
    match = _NUMERIC.match(tag or "")
    if not match:
        raise VersionError(f"image {ref!r} has no version tag to read a number from")
    return match.group(1)


def version_service(source: dict[str, Any], compose: dict[str, Any]) -> str:
    """The service whose image tag is the app's version."""
    block = compose.get(BLOCK_KEY) or {}
    service = block.get("version_from") or (source[BLOCK_KEY].get("domain") or {}).get("service")
    if not service:
        raise VersionError("the app has no web address, so its block must name version_from")
    return service


def app_version(source: dict[str, Any], compose: dict[str, Any]) -> str:
    service = version_service(source, compose)
    spec = (compose.get("services") or {}).get(service)
    if not isinstance(spec, dict) or not spec.get("image"):
        raise VersionError(f"service {service!r} has no image to read the version from")
    return version_of_image(str(spec["image"]))


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[._]", version))


def tag_name(app: str, version: str, revision: int) -> str:
    return f"{app}/{version}-{revision}"


def parse_tag(app: str, tag: str) -> tuple[str, int] | None:
    """(version, revision) of one of the app's tags, or None."""
    prefix = f"{app}/"
    if not tag.startswith(prefix):
        return None
    match = _TAG.fullmatch(tag[len(prefix):])
    if not match or not _NUMERIC.fullmatch(match["version"]):
        return None
    return match["version"], int(match["revision"])


def next_revision(app: str, version: str, tags: Iterable[str]) -> int:
    """1 plus the app's highest revision at this version."""
    parsed = (parse_tag(app, tag) for tag in tags)
    return 1 + max((p[1] for p in parsed if p and p[0] == version), default=0)


def sort_tags(app: str, tags: Iterable[str]) -> list[str]:
    """The app's tags, oldest first: by version, then revision."""
    keyed = []
    for tag in tags:
        parsed = parse_tag(app, tag)
        if parsed:
            keyed.append(((version_key(parsed[0]), parsed[1]), tag))
    return [tag for _, tag in sorted(keyed)]


def scope_errors(paths: Iterable[str]) -> list[str]:
    """A change touches one app."""
    ids = changes.app_ids(paths)
    if len(ids) > 1:
        return [f"the change touches {len(ids)} apps ({', '.join(ids)}); "
                f"a commit or pull request changes one app"]
    return []


def _show(ref: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"],
                          capture_output=True, text=True, check=True).stdout


def next_tag(app: str, tags: Iterable[str], ref: str = "HEAD") -> str:
    """The next tag of the app as commit `ref` holds it."""
    source = json.loads(_show(ref, f"sources/{app}.json"))
    compose = yaml.safe_load(_show(ref, f"blueprints/{app}/docker-compose.yml"))
    version = app_version(source, compose)
    return tag_name(app, version, next_revision(app, version, tags))


def _object(spec: str) -> str:
    """The object `<rev>:<path>` names in the checkout, empty when there is none."""
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", spec],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip()


def app_tags(app: str) -> dict[str, bool]:
    """The app's tags in the checkout, each with whether a passing run made it."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "for-each-ref",
         "--format=%(refname)%09%(objecttype)%09%(contents:subject)", f"refs/tags/{app}/"],
        capture_output=True, text=True, check=True).stdout
    tags = {}
    for line in out.splitlines():
        ref, kind, subject = (line.split("\t", 2) + ["", ""])[:3]
        tags[ref.removeprefix("refs/tags/")] = kind == "tag" and subject.startswith(PASSED)
    return tags


def app_paths(app: str) -> tuple[str, str]:
    """The paths that make up an app."""
    return f"blueprints/{app}", f"sources/{app}.json"


def last_change(app: str, ref: str = "HEAD") -> str:
    """The newest commit on `ref`'s first-parent line that changed the app's files, or ''."""
    return subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--first-parent", "--format=%H", ref, "--", *app_paths(app)],
        capture_output=True, text=True, check=True).stdout.strip()


def same_files(app: str, a: str, b: str) -> bool:
    """Whether commits `a` and `b` hold the same files of the app."""
    return all(_object(f"{a}:{path}") == _object(f"{b}:{path}") for path in app_paths(app))


def tested_state(app: str, ref: str = "HEAD") -> tuple[str, str | None]:
    """(TESTED, CHANGED or UNTESTED, the newest tag) of the app at `ref`."""
    tags = app_tags(app)
    order = sort_tags(app, tags)
    if not order:
        return UNTESTED, None
    newest = order[-1]
    if not same_files(app, newest, ref):
        return CHANGED, newest
    return (TESTED if tags[newest] else UNTESTED), newest


def _is_ancestor(commit: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, of],
                          capture_output=True, check=False).returncode == 0


def plan(app: str, ref: str = "HEAD") -> str:
    """What a pass at commit `ref` does to the app's tags: nothing when the checked-out
    commit holds other files of the app than `ref`, a passing run made the newest tag
    or the newest tag is on a later commit; annotate a hand-made newest tag that holds
    the same files, else a new tag."""
    state, newest = tested_state(app, ref)
    if not same_files(app, "HEAD", ref):
        return f"skip {newest or '-'}"
    if state == TESTED:
        return f"skip {newest}"
    if state == UNTESTED and newest:
        return f"mark {newest}"
    if newest and _is_ancestor(ref, f"{newest}^{{commit}}"):
        return f"skip {newest}"
    return f"create {next_tag(app, app_tags(app), ref)}"


def same_change(app: str, ref: str, base: str, cand: str) -> bool:
    """Whether commit `cand` holds the change `ref` holds, judged from `base`: the app's
    files are the same at both commits and at the points each is judged from."""
    done = subprocess.run(["git", "-C", str(ROOT), "merge-base", cand, base],
                          capture_output=True, text=True, check=False)
    fork = done.stdout.strip()
    if done.returncode != 0 or not fork:
        return False
    return same_files(app, cand, ref) and same_files(app, fork, base)


def tested_run(app: str, ref: str = "HEAD") -> str | None:
    """The address of the run that made the app's newest tag, when the app is tested at `ref`."""
    state, newest = tested_state(app, ref)
    if state != TESTED:
        return None
    subject = subprocess.run(
        ["git", "-C", str(ROOT), "for-each-ref", "--format=%(contents:subject)", f"refs/tags/{newest}"],
        capture_output=True, text=True, check=True).stdout.strip()
    match = re.fullmatch(re.escape(PASSED) + r" at \S+: (\S+)", subject)
    return match.group(1) if match else None


def apps() -> list[str]:
    return sorted(p.parent.name for p in (ROOT / "blueprints").glob("*/docker-compose.yml"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="versions")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("app")
    plan_cmd.add_argument("--ref", default="HEAD")
    sub.add_parser("scope")
    sub.add_parser("tested").add_argument("--needs-run", action="store_true")
    same_cmd = sub.add_parser("same")
    same_cmd.add_argument("app")
    same_cmd.add_argument("--ref", required=True)
    same_cmd.add_argument("--base", required=True)
    same_cmd.add_argument("--from", dest="cand", required=True)
    run_cmd = sub.add_parser("tested-run")
    run_cmd.add_argument("app")
    run_cmd.add_argument("--ref", default="HEAD")
    last_cmd = sub.add_parser("last-change")
    last_cmd.add_argument("app")
    last_cmd.add_argument("--ref", default="HEAD")
    args = parser.parse_args(argv)

    if args.command == "scope":
        lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
        errors = scope_errors(lines)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    if args.command == "tested":
        for app in apps():
            state, newest = tested_state(app)
            if not args.needs_run:
                print(f"{app}\t{state}\t{newest or '-'}")
            elif state != TESTED:
                print(app)
        return 0
    if changes.slug_errors([args.app]):
        print(f"{args.app!r} is not a plain app id", file=sys.stderr)
        return 1
    if args.command == "same":
        shas = (args.ref, args.base, args.cand)
        return 0 if all(_SHA.fullmatch(sha) for sha in shas) and same_change(
            args.app, args.ref, args.base, args.cand) else 1
    if args.command == "last-change":
        commit = last_change(args.app, args.ref)
        if commit:
            print(commit)
        return 0
    if args.command == "tested-run":
        run = tested_run(args.app, args.ref)
        if run:
            print(run)
        return 0
    try:
        print(plan(args.app, args.ref))
    except VersionError as exc:
        print(f"{args.app}: {exc}", file=sys.stderr)
        return 1
    return 0
