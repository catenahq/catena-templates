"""The app-ci checks that read one app: entry points of
.github/workflows/app-ci.yml.

  changed               repo-relative paths on stdin -> the app ids they touch
  images ID             the image references the app's compose pins
  provenance ID        each pulled image's project is old enough, with releases;
                        an image listed in PROVENANCE_OWN_IMAGES is the repository's own build
  ci-passed SHA         exit 0 when commit SHA of GITHUB_REPOSITORY has an app-ci status of success
                        posted by github-actions[bot] for a run of that repository (GITHUB_TOKEN or
                        GH_TOKEN reads it); any other answer, an unreadable one included, exits 1
  checker ID            the catena-admin image whose app checker reads the app: the release
                        its source names in x-catena.requires_catena, else the newest

Provenance reads the image's `org.opencontainers.image.source` label from
`docker image inspect`, so the app's images are pulled first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from . import changes, provenance
from .adopt import REPO, SHA
from .model import ROOT, SOURCES

CHECKER_IMAGE = "ghcr.io/catenahq/catena-admin"
CI_CONTEXT = "app-ci"
CI_CREATOR = "github-actions[bot]"


def checker_image(app: str) -> str:
    source = json.loads((SOURCES / f"{app}.json").read_text(encoding="utf-8"))
    release = (source.get("x-catena") or {}).get("requires_catena")
    return f"{CHECKER_IMAGE}:v{release}" if release else f"{CHECKER_IMAGE}:latest"


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


def ci_passed(sha: str, repo: str, fetch=provenance.http_fetch) -> bool:
    """Whether the newest app-ci status of the commit is a success posted by
    github-actions[bot] for a run of the repository."""
    statuses = fetch(f"{provenance.api_url()}/repos/{repo}/commits/{sha}/statuses?per_page=100")
    status = next((s for s in statuses or [] if s.get("context") == CI_CONTEXT), None)
    if not status:
        return False
    run = re.compile(rf"https://github\.com/{re.escape(repo)}/actions/runs/[0-9]+")
    return (status.get("state") == "success"
            and (status.get("creator") or {}).get("login") == CI_CREATOR
            and run.fullmatch(status.get("target_url") or "") is not None)


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
    sub.add_parser("ci-passed").add_argument("sha")
    for name in ("images", "provenance", "checker"):
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
    if args.command == "ci-passed":
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not SHA.fullmatch(args.sha) or not REPO.fullmatch(repo):
            print("ci-passed takes a full lowercase commit id and GITHUB_REPOSITORY", file=sys.stderr)
            return 1
        try:
            return 0 if ci_passed(args.sha, repo) else 1
        except provenance.ProvenanceError as exc:
            print(exc, file=sys.stderr)
            return 1
    if args.command == "images":
        refs, unresolved = provenance.compose_images(_compose(args.app))
        print("\n".join(refs))
        for raw in unresolved:
            print(f"{args.app}: image {raw!r} is not a plain reference", file=sys.stderr)
        return 1 if unresolved else 0
    if args.command == "checker":
        print(checker_image(args.app))
        return 0

    errors = provenance_errors(args.app)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0
