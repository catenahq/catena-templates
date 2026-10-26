"""The pull-request checks that read one app: entry points of
.github/workflows/app-ci.yml.

  changed               repo-relative paths on stdin -> the app ids they touch
  images ID             the image references the app's compose pins
  licence ID [--new]    a new imported entry names a permitted licence; any named one is permitted
  provenance ID         each pulled image's project is old enough, with releases

Provenance reads the image's `org.opencontainers.image.source` label from
`docker image inspect`, so the app's images are pulled first.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import changes, provenance
from .model import ROOT, SOURCE_SCHEMA


def _compose(app: str) -> str:
    return (ROOT / "blueprints" / app / "docker-compose.yml").read_text(encoding="utf-8")


def permitted_licences() -> list[str]:
    """The licences sources.schema.json admits in `origin.licence`: the list
    the registry importer holds an import to."""
    schema = json.loads(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    return schema["properties"]["x-catena"]["properties"]["origin"]["properties"]["licence"]["enum"]


def licence_errors(app: str, source: dict, *, new: bool) -> list[str]:
    """A new imported entry (`x-catena.status: imported`) names a permitted
    licence in `x-catena.origin.licence`: the licence of the registry it
    copies. A hand-written app, new or already in the catalog, may carry
    none, but one it names must be permitted."""
    permitted = permitted_licences()
    catena = source.get("x-catena") or {}
    named = (catena.get("origin") or {}).get("licence")
    if named is None:
        if new and catena.get("status") == "imported":
            return [f"{app}: sources/{app}.json names no licence; an imported entry sets "
                    f"x-catena.origin.licence to one of {', '.join(permitted)}"]
        return []
    if named not in permitted:
        return [f"{app}: licence {named!r} is not one of {', '.join(permitted)}"]
    return []


def provenance_errors(app: str, labels_of=None, fetch=provenance.http_fetch) -> list[str]:
    refs, unresolved = provenance.compose_images(_compose(app))
    errors = [f"{app}: image {raw!r} is not a plain reference" for raw in unresolved]
    for ref in refs:
        labels = (labels_of or _docker_labels)(ref)
        failure = provenance.check_image(ref, labels, fetch)
        if failure:
            errors.append(f"{app}: {failure}")
    return errors


def _docker_labels(ref: str) -> dict[str, str]:
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", ref],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise provenance.ProvenanceError(f"{ref}: docker image inspect: {proc.stderr.strip()}")
    return json.loads(proc.stdout) or {}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app_check")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("changed")
    for name in ("images", "licence", "provenance"):
        cmd = sub.add_parser(name)
        cmd.add_argument("app")
        if name == "licence":
            cmd.add_argument("--new", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "changed":
        ids = changes.app_ids(sys.stdin.read().splitlines())
        bad = changes.slug_errors(ids)
        for error in bad:
            print(error, file=sys.stderr)
        if bad:
            return 1
        print(" ".join(ids))
        return 0
    if args.command == "images":
        refs, unresolved = provenance.compose_images(_compose(args.app))
        print("\n".join(refs))
        for raw in unresolved:
            print(f"{args.app}: image {raw!r} is not a plain reference", file=sys.stderr)
        return 1 if unresolved else 0

    if args.command == "licence":
        path = ROOT / "sources" / f"{args.app}.json"
        errors = licence_errors(args.app, json.loads(path.read_text(encoding="utf-8")), new=args.new)
    else:
        errors = provenance_errors(args.app)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0
