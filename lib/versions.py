"""App versions are git tags, not strings in files.

A tested app is tagged `<app>/<version>-<revision>` (`nextcloud-s3-oidc/31.0.8-1`).
The version is the numeric part of the image tag of the app's main address
service: the compose block's `version_from` service when it names one, else
the service `sources/<id>.json` names in `x-catena.domain.service`, else the
service the compose labels as the main address (the only one carrying
`vps.route.host`, or of several the last also carrying `vps.route.main` as true,
1 or yes, as catena-admin `payload/engines/stackupdate` reads it). The
revision is 1 plus the highest revision the app already has at that version,
so a change that leaves the image alone still gets a new tag, and a deleted
tag's revision is never reused while a later one stands. Tags order by
version, then revision; the update engine's full tag rules live in
catena-admin `payload/engines/stackupdate`, and this reads only the number a
catalog pin is required to carry.

Classes. A change of an app's files falls in three classes: RUNTIME (what
decides what runs and how Catena operates it), VERIFICATION (what tests it) and
PRESENTATION (name, descriptions, logo, README). FILE_CLASSES gives the class of
each file of `blueprints/<id>/` and FIELD_CLASSES the class of each field of the
card file (`blueprints/<id>/app.json`, or `sources/<id>.json` with its
`x-catena.` prefix dropped); anything unlisted is RUNTIME. `changed_classes`
returns the classes of the difference between two commits.

A tag a passing app-tests run made is annotated, its message opening with
PASSED; the tags made by hand are not. An app is `tested` when its newest tag is
a passing run's and the files differ from it in verification alone (or not at
all), `presentation` when that tag is a passing run's and they differ in
presentation alone, `changed` otherwise, and `untested` when it has no tag or its
newest was made by hand and they differ in verification alone (or not at all).
All of it is read from the checkout's git.

  plan APP [--ref REF]  what a pass at commit REF (default HEAD) does to APP's tags:
                        `skip TAG` (tested, TAG is on a later commit, or the checked-out
                        commit holds other files of APP than REF; `skip -` with no tag),
                        `mark TAG` (annotate the hand-made TAG), `create TAG` or, in
                        state presentation, `create TAG FROM` (FROM: the newest tag)
  scope                 changed paths on stdin -> fail when they touch more than one app
  tested [--needs-run]  each app's state at the checked-out commit; with
                        --needs-run, the ids of the apps neither `tested` nor `presentation`
  same APP --ref SHA --base BASE --from CAND
                        exit 0 when commit CAND holds the change SHA holds: APP's files
                        differ between them in presentation alone, and likewise where each
                        is judged from (the merge base of CAND and BASE, and BASE)
  tested-run APP [--ref REF]
                        the address of the passing run that made APP's newest tag, when
                        APP differs from that tag at REF (default HEAD) in presentation
                        alone or not at all
  last-change APP [--ref REF]
                        the newest commit on REF's first-parent line (default HEAD) that
                        changed APP's files; nothing when none did
  classes APP --ref SHA --base SHA
                        the classes of the change from BASE to SHA, one per line
  runtime-change APP --ref REV
                        the newest commit on REV's first-parent line whose change of APP
                        holds RUNTIME, or that added APP; nothing when none did

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
from .adopt import FILE_MODES
from .model import ROOT, _service_labels

BLOCK_KEY = "x-catena"
PASSED = "app-tests passed"
CARD = "app.json"

RUNTIME, VERIFICATION, PRESENTATION = "runtime", "verification", "presentation"
# The state of an app whose files differ from its tested tag in presentation alone is
# named like that class.
TESTED, CHANGED, UNTESTED = "tested", "changed", "untested"

# Class of each file of blueprints/<id>/. app.json is judged per field (FIELD_CLASSES).
FILE_CLASSES = {
    "docker-compose.yml": RUNTIME,
    "test.yml": VERIFICATION,
    "logo.png": PRESENTATION,
    "logo.svg": PRESENTATION,
    "README.md": PRESENTATION,
}

# Class of each field of the card file; a source's fields are keyed without `x-catena.`.
FIELD_CLASSES = {
    "id": RUNTIME,
    "type": PRESENTATION,
    "platform": PRESENTATION,
    "name": PRESENTATION,
    "title": PRESENTATION,
    "categories": PRESENTATION,
    "upstream_url": PRESENTATION,
    "logo": PRESENTATION,
    "sso_mode": PRESENTATION,
    "status": PRESENTATION,
    "origin": PRESENTATION,
    "pending": PRESENTATION,
    "en": PRESENTATION,
    "fr": PRESENTATION,
    "sizing": PRESENTATION,
    "bench": VERIFICATION,
    "app_name": VERIFICATION,
    "domain": RUNTIME,
    "requires_catena": RUNTIME,
    "env_required_when": RUNTIME,
}
_TRUE = ("true", "yes", "1")
_ABSENT = object()

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


def _main_service(compose: dict[str, Any]) -> str | None:
    """The service of the main address: the only one carrying vps.route.host, or of
    several the last also carrying vps.route.main=true."""
    labels = {name: _service_labels(spec) for name, spec in (compose.get("services") or {}).items()}
    hosts = [name for name, found in labels.items() if found.get("vps.route.host", "").strip()]
    marked = [name for name in hosts if labels[name].get("vps.route.main", "").strip().lower() in _TRUE]
    return hosts[0] if len(hosts) == 1 else marked[-1] if marked else None


def version_service(source: dict[str, Any] | None, compose: dict[str, Any]) -> str:
    """The service whose image tag is the app's version."""
    block = compose.get(BLOCK_KEY) or {}
    domain = ((source or {}).get(BLOCK_KEY) or {}).get("domain") or {}
    service = block.get("version_from") or domain.get("service") or _main_service(compose)
    if not service:
        raise VersionError("the app has no web address, so its block must name version_from")
    return service


def app_version(source: dict[str, Any] | None, compose: dict[str, Any]) -> str:
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
    path = f"sources/{app}.json"
    source = json.loads(_show(ref, path)) if _object(f"{ref}:{path}") else None
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


def runtime_change(app: str, ref: str = "HEAD") -> str:
    """The newest commit on `ref`'s first-parent line whose change of the app holds
    RUNTIME, or that added the app, or ''."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--first-parent", "--format=%H %P", ref, "--", *app_paths(app)],
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        commit, *parents = line.split()
        if not parents or not _files(app, parents[0]) or RUNTIME in changed_classes(app, parents[0], commit):
            return commit
    return ""


def same_files(app: str, a: str, b: str) -> bool:
    """Whether commits `a` and `b` hold the same files of the app."""
    return all(_object(f"{a}:{path}") == _object(f"{b}:{path}") for path in app_paths(app))


def _files(app: str, rev: str) -> dict[str, tuple[str, str]]:
    """Path -> (mode, object) of the app's files at `rev`; empty when it holds none."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "-z", rev, "--", f"blueprints/{app}/", f"sources/{app}.json"],
        capture_output=True, text=True, check=False).stdout
    files = {}
    for entry in filter(None, out.split("\0")):
        meta, _, path = entry.partition("\t")
        mode, _, oid = meta.split(" ")
        files[path] = (mode, oid)
    return files


def _fields(paths: list[str], rev: str) -> dict[str, Any] | None:
    """The card fields held by the (at most one) card file at `rev`; None when it
    does not parse to an object."""
    if not paths:
        return {}
    try:
        doc = json.loads(_show(rev, paths[0]))
    except (subprocess.CalledProcessError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    block = doc.get(BLOCK_KEY, {})
    fields = {key: value for key, value in doc.items() if key != BLOCK_KEY}
    if not isinstance(block, dict) or fields.keys() & block.keys():
        return None
    return {**fields, **block}


def _form(value: Any) -> str:
    """A field's value in one canonical text: 8080 and 8080.0 differ, key order does not."""
    return "" if value is _ABSENT else json.dumps(value, sort_keys=True)


def _card_classes(old: list[str], a: str, new: list[str], b: str) -> set[str]:
    """Classes of the difference between the card files `old` at `a` and `new` at `b`."""
    if len(old) > 1 or len(new) > 1:
        return {RUNTIME}
    before, after = _fields(old, a), _fields(new, b)
    if before is None or after is None:
        return {RUNTIME}
    found = {FIELD_CLASSES.get(key, RUNTIME) for key in before.keys() | after.keys()
             if _form(before.get(key, _ABSENT)) != _form(after.get(key, _ABSENT))}
    return found or {PRESENTATION}


def _regular(entry: tuple[str, str] | None) -> bool:
    """Whether a (mode, object) entry is absent or a regular file: a link or a
    submodule is not a file a host serves, so it takes no light class."""
    return entry is None or entry[0] in FILE_MODES


def changed_classes(app: str, a: str, b: str) -> frozenset[str]:
    """The classes of the difference between the app's files at commits `a` and `b`:
    a differing, added or removed file takes its class, and the card file is read at
    both commits (under whichever of its two names it has there) and each differing
    field takes its class."""
    folder = f"blueprints/{app}/"
    cards = (folder + CARD, f"sources/{app}.json")
    before, after = _files(app, a), _files(app, b)
    classes = {FILE_CLASSES.get(path.removeprefix(folder), RUNTIME)
               if _regular(before.get(path)) and _regular(after.get(path)) else RUNTIME
               for path in before.keys() | after.keys()
               if path not in cards and before.get(path) != after.get(path)}
    if any(before.get(path) != after.get(path) for path in cards):
        if not all(_regular(entry.get(path)) for entry in (before, after) for path in cards):
            classes.add(RUNTIME)
        classes |= _card_classes([p for p in cards if p in before], a, [p for p in cards if p in after], b)
    return frozenset(classes)


def _newest_tag(app: str) -> tuple[str | None, bool]:
    """(the app's newest tag, whether a passing run made it)."""
    tags = app_tags(app)
    order = sort_tags(app, tags)
    return (order[-1], tags[order[-1]]) if order else (None, False)


def tested_state(app: str, ref: str = "HEAD") -> tuple[str, str | None]:
    """(TESTED, PRESENTATION, CHANGED or UNTESTED, the newest tag) of the app at `ref`."""
    newest, made = _newest_tag(app)
    if not newest:
        return UNTESTED, None
    classes = changed_classes(app, newest, ref)
    if classes <= {VERIFICATION}:
        return (TESTED if made else UNTESTED), newest
    if made and classes == {PRESENTATION}:
        return PRESENTATION, newest
    return CHANGED, newest


def _is_ancestor(commit: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, of],
                          capture_output=True, check=False).returncode == 0


def plan(app: str, ref: str = "HEAD") -> str:
    """What a pass at commit `ref` does to the app's tags: nothing when the checked-out
    commit holds other files of the app than `ref`, the app is tested or the newest tag
    is on a later commit; annotate a hand-made newest tag whose difference from `ref`
    is verification alone; else a new tag, with the newest tag as its FROM when the
    difference is presentation alone."""
    state, newest = tested_state(app, ref)
    if not same_files(app, "HEAD", ref):
        return f"skip {newest or '-'}"
    if state == TESTED:
        return f"skip {newest}"
    if state == UNTESTED and newest:
        return f"mark {newest}"
    if newest and _is_ancestor(ref, f"{newest}^{{commit}}"):
        return f"skip {newest}"
    tag = next_tag(app, app_tags(app), ref)
    return f"create {tag} {newest}" if state == PRESENTATION else f"create {tag}"


def same_change(app: str, ref: str, base: str, cand: str) -> bool:
    """Whether commit `cand` holds the change `ref` holds, judged from `base`: the app's
    files differ between `cand` and `ref`, and between where each is judged from, in
    presentation alone."""
    done = subprocess.run(["git", "-C", str(ROOT), "merge-base", cand, base],
                          capture_output=True, text=True, check=False)
    fork = done.stdout.strip()
    if done.returncode != 0 or not fork:
        return False
    return (changed_classes(app, cand, ref) <= {PRESENTATION}
            and changed_classes(app, fork, base) <= {PRESENTATION})


def tested_run(app: str, ref: str = "HEAD") -> str | None:
    """The address of the run that made the app's newest tag, when `ref` differs from
    that tag in presentation alone."""
    newest, made = _newest_tag(app)
    if not made or not changed_classes(app, newest, ref) <= {PRESENTATION}:
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
    classes_cmd = sub.add_parser("classes")
    classes_cmd.add_argument("app")
    classes_cmd.add_argument("--ref", required=True)
    classes_cmd.add_argument("--base", required=True)
    runtime_cmd = sub.add_parser("runtime-change")
    runtime_cmd.add_argument("app")
    runtime_cmd.add_argument("--ref", required=True)
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
            elif state not in (TESTED, PRESENTATION):
                print(app)
        return 0
    if changes.slug_errors([args.app]):
        print(f"{args.app!r} is not a plain app id", file=sys.stderr)
        return 1
    if args.command == "same":
        shas = (args.ref, args.base, args.cand)
        return 0 if all(_SHA.fullmatch(sha) for sha in shas) and same_change(
            args.app, args.ref, args.base, args.cand) else 1
    if args.command == "classes":
        if not all(_SHA.fullmatch(sha) for sha in (args.ref, args.base)):
            print("--ref and --base take full lowercase commit ids", file=sys.stderr)
            return 1
        for name in sorted(changed_classes(args.app, args.base, args.ref)):
            print(name)
        return 0
    if args.command == "runtime-change":
        if args.ref.startswith("-"):
            print(f"{args.ref!r} is not a revision", file=sys.stderr)
            return 1
        commit = runtime_change(args.app, args.ref)
        if commit:
            print(commit)
        return 0
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
