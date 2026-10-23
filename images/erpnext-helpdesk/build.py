#!/usr/bin/env python3
"""Build the Catena ERPNext image: Frappe plus the apps apps.json tracks.

  resolve  re-read the sha of every input (the commit of a pinned tag, the head
           of a branch) and write apps.json;
           with --newest, also move frappe to its newest release tag whose
           frappe/build and frappe/base images exist on Docker Hub, and each
           tag-tracked app to its newest tag that admits that frappe
  plan     look for a published tag of this ERPNext release whose
           io.catena.inputs label equals apps.json's inputs; write inputs.json
           with the inputs, whether to build, the tag (that one, or the next),
           whether the blueprint must move to it (pin) and the previous tag
           the upgrade test starts from (the blueprint's pinned tag, when
           published and older than the tag); --force builds anyway
  build    the image, from frappe_docker's layered Containerfile at the pinned
           builder commit, Frappe at the tag apps.json names, build-only
           node_modules removed in the same layer
  test     the catalog template's backend sets up a site with every app, which
           is migrated and pinged; when there is a previous published image,
           the site is set up on it, and the new image's backend starts on it
           before the migration
  publish  push the tag; needs GHCR_TOKEN and refuses a tag the registry
           already holds
  pin      set the seven Frappe services of the blueprint to inputs.json's tag

A GHCR_TOKEN in the environment (with GHCR_USER) authenticates registry reads.
Stdlib only. Run from anywhere; paths resolve beside this file.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPS_JSON = HERE / "apps.json"
SPEC = json.loads(APPS_JSON.read_text())
INPUTS = HERE / "inputs.json"
BLUEPRINT = HERE.parent.parent / "blueprints" / "erpnext-helpdesk" / "docker-compose.yml"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d+)$")
BUILDER_IMAGES = ("build", "base")
FRAPPE_SERVICES = ("backend", "frontend", "websocket", "queue-default", "queue-short",
                   "queue-long", "scheduler")

# The last command of the builder stage's RUN in images/layered/Containerfile.
# Build-time steps are appended to it, so removed files never reach a layer.
ANCHOR = 'find apps -mindepth 1 -path "*/.git" | xargs rm -fr'
# Build-only node_modules; Frappe's own run the websocket service.
STRIP = ('find apps -mindepth 2 -maxdepth 4 -type d -name node_modules'
         ' -not -path "apps/frappe/*" -prune -exec rm -rf {} +')
# NLTK corpora an app loads at runtime, into the bench venv's nltk_data, which
# NLTK searches. An app that finds none downloads them into its container.
NLTK = ("env/bin/python -c \"import sys, nltk; [nltk.download(c, download_dir='env/nltk_data',"
        " raise_on_error=True) for c in sys.argv[1:]]\" ")


def log(msg: str) -> None:
    print(f"[erpnext-helpdesk-image] {msg}", file=sys.stderr, flush=True)


def run(*argv: str, cwd: Path | None = None, capture: bool = False) -> str:
    log("$ " + " ".join(argv))
    res = subprocess.run(argv, cwd=cwd, check=True, text=True,
                         capture_output=capture)
    return res.stdout if capture else ""


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def builder_steps() -> str:
    """What replaces ANCHOR in the builder stage."""
    steps = [ANCHOR, STRIP]
    corpora = [c for app in SPEC["apps"] for c in app.get("nltk_data", [])]
    if corpora:
        steps.append(NLTK + " ".join(corpora))
    return " && \\\n  ".join(steps)


# --- resolve -----------------------------------------------------------------

def ls_remote(repo: str, *refs: str, tags: bool = False) -> dict[str, str]:
    """{ref: sha}. With tags, every tag ref, peeled to the commit it names."""
    if tags:
        out = run("git", "ls-remote", "--tags", repo, capture=True)
        peeled = {}
        for sha, ref in (line.split("\t") for line in out.splitlines()):
            if ref.endswith("^{}"):
                peeled[ref[:-3]] = sha
            else:
                peeled.setdefault(ref, sha)
        return peeled
    out = run("git", "ls-remote", repo, *refs, capture=True)
    return {ref: sha for sha, ref in (line.split("\t") for line in out.splitlines())}


def raw(repo: str, sha: str, path: str) -> str:
    url = repo.replace("https://github.com/", "https://raw.githubusercontent.com/")
    with urllib.request.urlopen(f"{url}/{sha}/{path}", timeout=30) as r:
        return r.read().decode()


def admits(spec: str, version: str) -> bool:
    """A PEP 440-style range of plain comparisons, as Frappe apps declare."""
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, "<": lambda a, b: a < b,
           "==": lambda a, b: a == b}
    have = vtuple(version)[:3]
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        op = next(o for o in (">=", "<=", "==", ">", "<") if clause.startswith(o))
        want = vtuple(clause[len(op):].split("-")[0])[:3]
        if not ops[op](have, want):
            return False
    return True


def frappe_range(repo: str, sha: str) -> str | None:
    try:
        doc = tomllib.loads(raw(repo, sha, "pyproject.toml"))
    except (urllib.error.URLError, tomllib.TOMLDecodeError):
        return None
    deps = doc.get("tool", {}).get("bench", {}).get("frappe-dependencies", {})
    return deps.get("frappe")


def inputs() -> dict:
    """What apps.json names, in the shape of the image's io.catena.inputs label."""
    out = {"builder": SPEC["builder"]["sha"],
           "recipe": hashlib.sha256(builder_steps().encode()).hexdigest()[:16],
           "frappe": {"version": SPEC["frappe"]["version"], "sha": SPEC["frappe"]["sha"]}}
    for app in SPEC["apps"]:
        key = "version" if "tags" in app else "branch"
        out[app["name"]] = {key: app[key], "sha": app["sha"]}
    return out


def tag_shas(repo: str, pattern: str) -> dict[str, str]:
    """{tag: commit sha} of the repo's tags matching the pattern."""
    rx = re.compile(pattern)
    return {ref.removeprefix("refs/tags/"): sha
            for ref, sha in ls_remote(repo, tags=True).items()
            if rx.match(ref.removeprefix("refs/tags/"))}


def hub_tag_exists(name: str, tag: str) -> bool:
    """Whether Docker Hub holds frappe/<name>:<tag> (200 yes, 404 no)."""
    url = f"https://hub.docker.com/v2/namespaces/frappe/repositories/{name}/tags/{tag}"
    try:
        with urllib.request.urlopen(url, timeout=30):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def move_to_newest() -> None:
    """Frappe to its newest release tag whose builder images exist, each
    tag-tracked app to its newest tag whose frappe-dependencies admit that
    Frappe."""
    frappe = SPEC["frappe"]
    tags = tag_shas(frappe["repo"], frappe["tags"])
    for tag in sorted(tags, key=vtuple, reverse=True):
        missing = [f"frappe/{n}:{tag}" for n in BUILDER_IMAGES if not hub_tag_exists(n, tag)]
        if not missing:
            frappe["version"], frappe["sha"] = tag, tags[tag]
            break
        log(f"frappe {tag} skipped: {', '.join(missing)} not published")
    else:
        raise SystemExit(f"no frappe tag matching {frappe['tags']} has its frappe/build and frappe/base images")
    for app in SPEC["apps"]:
        if "tags" not in app:
            continue
        tags = tag_shas(app["repo"], app["tags"])
        for tag in sorted(tags, key=vtuple, reverse=True):
            spec = frappe_range(app["repo"], tags[tag])
            if spec is None or admits(spec, frappe["version"]):
                app["version"], app["sha"] = tag, tags[tag]
                break
            log(f"{app['name']} {tag} needs frappe {spec}; looking further back")
        else:
            raise SystemExit(f"no {app['name']} release admits frappe {frappe['version']}")


def registry(path: str, accept: str = "application/json") -> dict | None:
    """One GHCR read, authenticated when GHCR_TOKEN is set; None when the image
    does not exist or is not readable."""
    repo = SPEC["image"].removeprefix("ghcr.io/")
    token_req = urllib.request.Request(f"https://ghcr.io/token?scope=repository:{repo}:pull")
    if os.environ.get("GHCR_TOKEN"):
        basic = base64.b64encode(
            f"{os.environ.get('GHCR_USER', '')}:{os.environ['GHCR_TOKEN']}".encode()).decode()
        token_req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(token_req, timeout=30) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": accept})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return None
        raise


def published() -> list[str]:
    tags = (registry("tags/list?n=1000") or {}).get("tags") or []
    return sorted((t for t in tags if TAG_RE.match(t)), key=vtuple)


def built_from(tag: str) -> dict | None:
    manifest = registry(f"manifests/{tag}", ",".join([
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json"]))
    if not manifest:
        return None
    config = registry(f"blobs/{manifest['config']['digest']}")
    labels = (config or {}).get("config", {}).get("Labels") or {}
    raw_inputs = labels.get("io.catena.inputs")
    return json.loads(raw_inputs) if raw_inputs else None


def next_tag(inputs: dict, tags: list[str]) -> str:
    """v<erpnext>-<n>: n counts the builds of one ERPNext release."""
    erpnext = inputs["erpnext"]["version"].lstrip("v")
    same = [int(TAG_RE.match(t).group(4)) for t in tags if t.startswith(f"v{erpnext}-")]
    return f"v{erpnext}-{max(same, default=0) + 1}"


def refresh_shas() -> None:
    """Every input's sha from upstream: the commit a tag-tracked input's tag
    names, the head of a branch-tracked one."""
    for node in (SPEC["frappe"], *SPEC["apps"]):
        if "branch" in node:
            ref = f"refs/heads/{node['branch']}"
            node["sha"] = ls_remote(node["repo"], ref)[ref]
            continue
        ref = f"refs/tags/{node['version']}"
        found = ls_remote(node["repo"], tags=True)
        if ref not in found:
            raise SystemExit(f"{node['repo']} has no tag {node['version']}")
        node["sha"] = found[ref]


def cmd_resolve(args) -> None:
    if args.newest:
        move_to_newest()
    refresh_shas()
    APPS_JSON.write_text(json.dumps(SPEC, indent=2) + "\n")
    log("apps.json: " + json.dumps(inputs(), sort_keys=True))


def pinned_images(text: str) -> list[tuple[int, str, str]]:
    """(line index, service, tag) of every `image: <SPEC image>:<tag>` line."""
    found, service = [], None
    pattern = re.compile(rf"^\s+image:\s*{re.escape(SPEC['image'])}:(\S+)\s*$")
    for i, line in enumerate(text.splitlines()):
        head = re.match(r"^  ([\w-]+):\s*$", line)
        if head:
            service = head.group(1)
        m = pattern.match(line)
        if m:
            found.append((i, service, m.group(1)))
    return found


def pinned_tag() -> str | None:
    """The tag the blueprint pins, None when its Frappe services disagree."""
    tags = {tag for _i, _s, tag in pinned_images(BLUEPRINT.read_text())}
    return tags.pop() if len(tags) == 1 else None


def cmd_plan(args) -> None:
    """A build happens unless a published tag of this ERPNext release already
    carries these inputs; that tag is then the plan's tag. `pin` says the
    blueprint must move to the tag."""
    now = inputs()
    tags = published()
    release = f"v{now['erpnext']['version'].lstrip('v')}-"
    same = next((t for t in reversed(tags) if t.startswith(release) and built_from(t) == now), None)
    build = args.force or same is None
    tag = next_tag(now, tags) if build else same
    pinned = pinned_tag()
    out = {"inputs": now, "build": build,
           "previous": pinned if pinned in tags and vtuple(pinned) < vtuple(tag) else None,
           "pin": pinned != tag, "tag": tag}
    INPUTS.write_text(json.dumps(out, indent=2) + "\n")
    log(f"previous={out['previous']} build={build} pin={out['pin']} tag={tag}")
    print(json.dumps(out, indent=2))


def cmd_pin(args) -> None:
    tag = load()["tag"]
    text = BLUEPRINT.read_text()
    found = pinned_images(text)
    if sorted(s for _i, s, _t in found) != sorted(FRAPPE_SERVICES):
        raise SystemExit(f"{BLUEPRINT.name} pins {[s for _i, s, _t in found]} to {SPEC['image']}; "
                         f"expected exactly {list(FRAPPE_SERVICES)}")
    lines = text.splitlines(keepends=True)
    for i, _service, _old in found:
        lines[i] = re.sub(r"(image:\s*)\S+", lambda m: f"{m.group(1)}{SPEC['image']}:{tag}",
                          lines[i], count=1)
    BLUEPRINT.write_text("".join(lines))
    log(f"{BLUEPRINT.name} pins {SPEC['image']}:{tag}")


# --- build -------------------------------------------------------------------

def load() -> dict:
    return json.loads(INPUTS.read_text())


def cmd_build(args) -> None:
    state = load()
    inputs = state["inputs"]
    work = Path(tempfile.mkdtemp(prefix="erpnext-helpdesk-image-"))
    builder = work / "frappe_docker"
    run("git", "clone", "--quiet", SPEC["builder"]["repo"], str(builder))
    run("git", "-C", str(builder), "checkout", "--quiet", SPEC["builder"]["sha"])
    containerfile = builder / "images" / "layered" / "Containerfile"
    text = containerfile.read_text()
    if text.count(ANCHOR) != 1:
        raise SystemExit("the layered Containerfile changed shape: the build-time "
                         "steps have no single anchor; review the builder bump")
    containerfile.write_text(text.replace(ANCHOR, builder_steps()))

    apps = []
    for app in SPEC["apps"]:
        got = inputs[app["name"]]
        apps.append({"url": app["repo"], "branch": got.get("version") or got["branch"]})
    apps_json = work / "apps.json"
    apps_json.write_text(json.dumps(apps))
    bust = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    image = f"{SPEC['image']}:{state['tag']}"
    run("docker", "buildx", "build", "--load",
        "--build-arg", f"FRAPPE_BRANCH={SPEC['frappe']['version']}",
        "--build-arg", f"CACHE_BUST={bust}",
        "--secret", f"id=apps_json,src={apps_json}",
        "--label", f"org.opencontainers.image.version={state['tag']}",
        "--label", f"org.opencontainers.image.source=https://github.com/catenahq/catena-templates",
        "--label", f"io.catena.inputs={json.dumps(inputs, sort_keys=True)}",
        "-f", str(containerfile), "-t", image, str(builder))

    for app in SPEC["apps"]:
        if "branch" in app:
            ref = f"refs/heads/{app['branch']}"
            head = ls_remote(app["repo"], ref)[ref]
            if head != inputs[app["name"]]["sha"]:
                raise SystemExit(f"{app['name']} {app['branch']} moved to {head} during the build; "
                                 f"the image may hold a commit other than {inputs[app['name']]['sha']}")

    present = run("docker", "run", "--rm", "--entrypoint", "ls", image,
                  "/home/frappe/frappe-bench/apps", capture=True).split()
    missing = {"frappe", *(a["name"] for a in SPEC["apps"])} - set(present)
    if missing:
        raise SystemExit(f"{image} lacks {sorted(missing)}: the apps did not reach the image")
    log(f"built {image} with {sorted(present)}")


# --- test --------------------------------------------------------------------

# The site the smoke stack creates, which every request names.
SITE = "frontend"
BACKEND = "erpnext-smoke-backend-1"


def compose(image: str, *argv: str) -> None:
    """docker compose over the smoke stack, running `image`, with the values
    the template's backend reads. It installs FRAPPE_APPS in order, and
    apps.json's order puts each app after the apps it requires."""
    env = {**os.environ, "IMG": image, "ERPNEXT_HOSTNAME": SITE,
           "ERPNEXT_ADMIN_PASSWORD": "admin", "DB_ROOT_PASSWORD": "admin",
           "FRAPPE_APPS": ",".join(a["name"] for a in SPEC["apps"])}
    argv = ("docker", "compose", "-p", "erpnext-smoke",
            "-f", str(HERE / "smoke-compose.yml"), *argv)
    log("$ " + " ".join(argv))
    subprocess.run(argv, check=True, text=True, env=env)


def wait_backend(image: str) -> None:
    """Wait for the template's backend script to reach the app server, its
    last step: start.sh runs gunicorn on port 8000. A step that fails ends the
    script, and the container exits or restarts."""
    for _ in range(120):
        status, code, restarts = run(
            "docker", "inspect", "-f",
            "{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}",
            BACKEND, capture=True).split()
        if status != "running" or restarts != "0":
            compose(image, "logs", "backend")
            raise SystemExit(f"the backend's setup failed on {image}: {status}, "
                             f"exit code {code}, {restarts} restart(s)")
        probe = subprocess.run(["docker", "exec", BACKEND, "bash", "-c",
                                ": > /dev/tcp/127.0.0.1/8000"], capture_output=True)
        if probe.returncode == 0:
            log(f"the backend serves on {image}")
            return
        time.sleep(5)
    compose(image, "logs", "backend")
    raise SystemExit(f"the backend did not reach its app server on {image} in 10 minutes")


def ping(image: str) -> None:
    # nginx resolves the backend once at start; a restarted backend may move.
    compose(image, "restart", "frontend")
    for _ in range(30):
        res = subprocess.run(["curl", "-s", "-H", f"Host: {SITE}",
                              "http://localhost:8080/api/method/ping"],
                             text=True, capture_output=True)
        if '"pong"' in res.stdout:
            log("ping answered pong")
            return
        time.sleep(5)
    raise SystemExit(f"ping never answered pong: {res.stdout[-300:]}")


def cmd_test(args) -> None:
    state = load()
    new = f"{SPEC['image']}:{state['tag']}"
    first = f"{SPEC['image']}:{state['previous']}" if state.get("previous") and not args.fresh else new
    try:
        if first != new:
            run("docker", "pull", first)
        compose(first, "up", "-d")
        wait_backend(first)
        if first != new:
            # As on a host: the new backend's setup, install-app included, runs
            # against the previous image's schema before the migration.
            log(f"upgrading the site from {first} to {new}")
            compose(new, "up", "-d")
            wait_backend(new)
        compose(new, "exec", "-T", "backend", "bench", "--site", "all", "migrate")
        try:
            compose(new, "exec", "-T", "backend", "test", "!", "-e", "/home/frappe/nltk_data")
        except subprocess.CalledProcessError:
            raise SystemExit("migrate downloaded NLTK data: add the corpus to that "
                             "app's nltk_data in apps.json") from None
        ping(new)
    finally:
        compose(new, "down", "-v")


def cmd_publish(args) -> None:
    if not os.environ.get("GHCR_TOKEN"):
        raise SystemExit("publish needs GHCR_TOKEN: the check that the tag is free is an authenticated read")
    tag = load()["tag"]
    if tag in published():
        raise SystemExit(f"{SPEC['image']}:{tag} is already published; a tag is never overwritten")
    run("docker", "push", f"{SPEC['image']}:{tag}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--newest", action="store_true", help="move tag-tracked inputs to their newest tags")
    pl = sub.add_parser("plan")
    pl.add_argument("--force", action="store_true", help="build even when nothing moved")
    sub.add_parser("build")
    t = sub.add_parser("test")
    t.add_argument("--fresh", action="store_true", help="skip the upgrade from the previous image")
    sub.add_parser("publish")
    sub.add_parser("pin")
    args = p.parse_args()
    {"resolve": cmd_resolve, "plan": cmd_plan, "build": cmd_build, "test": cmd_test,
     "publish": cmd_publish, "pin": cmd_pin}[args.cmd](args)


if __name__ == "__main__":
    main()
