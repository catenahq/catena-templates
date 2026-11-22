"""Copies one app from a fork's open pull request onto the branch adopt/<pr> of this
repository and opens the pull request from that branch into the fork's base branch.

Only data moves: the fork's files are read as blobs through the API and written
as blobs of this repository, so no fork code is checked out or run. The change
must stay inside one app (`blueprints/<id>/**`, `app.json` and the logo among them);
the catalog's hand-edited root files (`catalog-meta.json`, `catalog-aliases.json`)
belong to no app and are refused.
"""
from __future__ import annotations

import base64
import re
import sys
from typing import Callable

from lib.changes import SLUG, app_id

FILE_MODES = {"100644", "100755"}
MAX_BYTES = 1_000_000
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

Api = Callable[..., dict | list]


class AdoptError(Exception):
    pass


def _clean(path: str) -> str:
    parts = path.split("/")
    if (not path or path.startswith("/") or "\\" in path
            or any(p in ("", ".", "..", ".git") for p in parts)):
        raise AdoptError(f"unsafe path {path!r}")
    return path


def plan(files: list[dict]) -> dict:
    """The copy and delete lists for a pull request's file entries, or AdoptError.
    An entry has `filename`, `status` and, for a rename, `previous_filename`."""
    copy: list[str] = []
    delete: list[str] = []
    apps: set[str] = set()
    for f in files:
        name = _clean(f["filename"])
        old = _clean(f["previous_filename"]) if f.get("previous_filename") else None
        for path in filter(None, (name, old)):
            app = app_id(path)
            if app is None:
                raise AdoptError(f"{path} belongs to no app")
            if not SLUG.fullmatch(app):
                raise AdoptError(f"{app!r} is not a plain app id")
            apps.add(app)
        if f["status"] == "removed":
            delete.append(name)
        else:
            copy.append(name)
            if old:
                delete.append(old)
    if not apps:
        raise AdoptError("the pull request changes no app")
    if len(apps) > 1:
        raise AdoptError(f"the pull request changes more than one app: {sorted(apps)}")
    return {"app": next(iter(apps)), "copy": sorted(copy), "delete": sorted(delete)}


def _check(sha: str, fork: str) -> None:
    if not SHA.fullmatch(sha):
        raise AdoptError("head sha is not 40 lowercase hex")
    if not REPO.fullmatch(fork):
        raise AdoptError("fork repository name is not owner/name")


def adopt(api: Api, base_repo: str, fork: str, head_sha: str, pr: int, files: list[dict],
          base_branch: str = "main") -> dict:
    """Writes branch adopt/<pr> of base_repo holding base_branch plus the fork's app.
    `api(method, path, body=None)` returns the decoded JSON of a GitHub API call."""
    _check(head_sha, fork)
    p = plan(files)
    tree = api("GET", f"repos/{fork}/git/trees/{head_sha}?recursive=1")
    if tree.get("truncated"):
        raise AdoptError("the fork's tree is too large to read")
    entries = {e["path"]: e for e in tree["tree"]}
    items = []
    for path in p["copy"]:
        e = entries.get(path)
        if e is None or e["type"] != "blob":
            raise AdoptError(f"{path} is not a file at {head_sha[:12]}")
        if e["mode"] not in FILE_MODES:
            raise AdoptError(f"{path} is a link or submodule (mode {e['mode']})")
        if e.get("size", 0) > MAX_BYTES:
            raise AdoptError(f"{path} is larger than {MAX_BYTES} bytes")
        blob = api("GET", f"repos/{fork}/git/blobs/{e['sha']}")
        if blob["encoding"] != "base64":
            raise AdoptError(f"{path}: unexpected blob encoding")
        content = base64.b64encode(base64.b64decode(blob["content"])).decode()
        made = api("POST", f"repos/{base_repo}/git/blobs", {"content": content, "encoding": "base64"})
        items.append({"path": path, "mode": e["mode"], "type": "blob", "sha": made["sha"]})
    items += [{"path": d, "mode": "100644", "type": "blob", "sha": None} for d in p["delete"]
              if d not in p["copy"]]
    ref = api("GET", f"repos/{base_repo}/git/ref/heads/{base_branch}")
    parent = ref["object"]["sha"]
    base_tree = api("GET", f"repos/{base_repo}/git/commits/{parent}")["tree"]["sha"]
    new_tree = api("POST", f"repos/{base_repo}/git/trees", {"base_tree": base_tree, "tree": items})
    commit = api("POST", f"repos/{base_repo}/git/commits", {
        "message": f"feat({p['app']}): adopt pull request {pr}",
        "tree": new_tree["sha"], "parents": [parent]})
    branch = f"adopt/{pr}"
    try:
        api("GET", f"repos/{base_repo}/git/ref/heads/{branch}")
        api("PATCH", f"repos/{base_repo}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
    except LookupError:
        api("POST", f"repos/{base_repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    return {"app": p["app"], "branch": branch, "sha": commit["sha"]}


def fork_pr(api: Api, base_repo: str, pr: int) -> dict:
    """The fork, head commit and base branch of pull request `pr` of base_repo, or
    AdoptError when it is not open or does not come from a fork."""
    data = api("GET", f"repos/{base_repo}/pulls/{pr}")
    if data.get("state") != "open":
        raise AdoptError(f"pull request {pr} is not open")
    fork = ((data.get("head") or {}).get("repo") or {}).get("full_name")
    if not fork:
        raise AdoptError(f"the repository of pull request {pr} no longer exists")
    if fork.lower() == base_repo.lower():
        raise AdoptError(f"pull request {pr} does not come from a fork")
    head_sha = data["head"]["sha"]
    _check(head_sha, fork)
    return {"fork": fork, "sha": head_sha, "base": data["base"]["ref"]}


def open_pr(api: Api, base_repo: str, pr: int, src: dict, adopted: dict) -> str:
    """The address of the open pull request from adopt/<pr> into the fork's base branch,
    opened when there is none."""
    owner = base_repo.split("/")[0]
    branch = adopted["branch"]
    found = api("GET", f"repos/{base_repo}/pulls?state=open&head={owner}:{branch}&per_page=100",
                paginate=True)
    if found:
        return found[0]["html_url"]
    made = api("POST", f"repos/{base_repo}/pulls", {
        "title": f"feat({adopted['app']}): adopt pull request {pr}",
        "head": branch, "base": src["base"],
        "body": (f"Adopts #{pr} from {src['fork']} at {src['sha']}: the files of "
                 f"{adopted['app']}, copied as data.")})
    return made["html_url"]


def main(argv: list[str]) -> int:
    import json
    import subprocess

    base_repo, pr_text = argv
    if not REPO.fullmatch(base_repo) or not re.fullmatch(r"[1-9][0-9]*", pr_text):
        print("adopt refused: the arguments are <owner/name> <pull request number>", file=sys.stderr)
        return 1
    pr = int(pr_text)

    def api(method: str, path: str, body: dict | None = None, paginate: bool = False):
        cmd = ["gh", "api", "-X", method, path]
        if paginate:
            cmd += ["--paginate", "--slurp"]
        if body is not None:
            cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True, check=False, timeout=120)
        if r.returncode != 0:
            if "404" in r.stderr:
                raise LookupError(path)
            raise AdoptError(f"{method} {path}: {r.stderr.strip()[:200]}")
        out = json.loads(r.stdout or "null")
        return [x for page in out for x in page] if paginate else out

    try:
        src = fork_pr(api, base_repo, pr)
        files = api("GET", f"repos/{base_repo}/pulls/{pr}/files?per_page=100", paginate=True)
        adopted = adopt(api, base_repo, src["fork"], src["sha"], pr, files, src["base"])
        adopted["pull_request"] = open_pr(api, base_repo, pr, src, adopted)
        print(json.dumps(adopted))
    except (AdoptError, LookupError) as e:
        print(f"adopt refused: {e}", file=sys.stderr)
        return 1
    return 0
