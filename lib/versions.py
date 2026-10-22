"""App versions are git tags, not strings in files.

A tested app is tagged `<app>/<version>-<revision>` (`nextcloud-s3-oidc/31.0.8-1`).
The version is the numeric part of the image tag of the app's main address
service: the compose block's `version_from` service when it names one, else
the service `sources/<id>.json` names in `x-catena.domain.service`. The
revision is 1 plus the number of tags the app already has at that version, so
a change that leaves the image alone still gets a new tag. Tags order by
version, then revision; the update engine's full tag rules live in
catena-admin `payload/engines/stackupdate`, and this reads only the number a
catalog pin is required to carry.

  next APP     existing tags on stdin, one per line -> the next tag for APP
  scope        changed paths on stdin -> fail when they touch more than one app

`scope` is the pull-request check that keeps a change to one app, so one
merge is one tag.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import changes, provenance

ROOT = Path(__file__).resolve().parent.parent
BLOCK_KEY = "x-catena"

_NUMERIC = re.compile(r"v?(\d+(?:[._]\d+)*)")
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
    """1 plus the app's tags at this version."""
    parsed = (parse_tag(app, tag) for tag in tags)
    return 1 + sum(1 for p in parsed if p and p[0] == version)


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
                f"a pull request changes one app"]
    return []


def next_tag(app: str, tags: Iterable[str]) -> str:
    source = json.loads((ROOT / "sources" / f"{app}.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((ROOT / "blueprints" / app / "docker-compose.yml").read_text(encoding="utf-8"))
    version = app_version(source, compose)
    return tag_name(app, version, next_revision(app, version, tags))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="versions")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next").add_argument("app")
    sub.add_parser("scope")
    args = parser.parse_args(argv)
    lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]

    if args.command == "scope":
        errors = scope_errors(lines)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    if changes.slug_errors([args.app]):
        print(f"{args.app!r} is not a plain app id", file=sys.stderr)
        return 1
    try:
        print(next_tag(args.app, lines))
    except VersionError as exc:
        print(f"{args.app}: {exc}", file=sys.stderr)
        return 1
    return 0
