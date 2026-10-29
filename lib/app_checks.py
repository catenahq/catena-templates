"""The pull-request checks that read one app: entry points of
.github/workflows/app-ci.yml.

  changed               repo-relative paths on stdin -> the app ids they touch
  images ID             the image references the app's compose pins
  provenance ID        each pulled image's project is old enough, with releases;
                        an image listed in PROVENANCE_OWN_IMAGES is the repository's own build

Provenance reads the image's `org.opencontainers.image.source` label from
`docker image inspect`, so the app's images are pulled first.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import changes, provenance
from .model import ROOT


def _compose(app: str) -> str:
    return (ROOT / "blueprints" / app / "docker-compose.yml").read_text(encoding="utf-8")


def provenance_errors(app: str, labels_of=None, fetch=provenance.http_fetch) -> list[str]:
    refs, unresolved = provenance.compose_images(_compose(app))
    errors = [f"{app}: image {raw!r} is not a plain reference" for raw in unresolved]
    own = provenance.own_images()
    for ref in refs:
        if provenance.is_own(ref, own):
            print(f"{app}: {ref}: the repository's own build")
            continue
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
    for name in ("images", "provenance"):
        sub.add_parser(name).add_argument("app")
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

    errors = provenance_errors(args.app)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0
