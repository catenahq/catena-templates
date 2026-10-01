#!/usr/bin/env python3
"""Build the Catena ERPNext image: Frappe plus the apps apps.json tracks.

  resolve  the newest upstream version of every input, compared with what the
           newest published image was built from (its io.catena.inputs label);
           writes inputs.json, with "build": false when nothing moved
  build    the image, from frappe_docker's layered Containerfile at the pinned
           builder commit, build-only node_modules removed in the same layer
  test     a site with every app, migrated and pinged; an upgrade from the
           previous published image when there is one
  publish  push the tag

Stdlib only. Run from anywhere; paths resolve beside this file.
"""
from __future__ import annotations

import argparse
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
SPEC = json.loads((HERE / "apps.json").read_text())
INPUTS = HERE / "inputs.json"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d+)$")

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
    print(f"[erpnext-image] {msg}", file=sys.stderr, flush=True)


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


def resolve_inputs() -> dict:
    frappe = SPEC["frappe"]
    head = ls_remote(frappe["repo"], f"refs/heads/{frappe['branch']}")
    fsha = head[f"refs/heads/{frappe['branch']}"]
    m = re.search(r'__version__ = "([^"]+)"', raw(frappe["repo"], fsha, "frappe/__init__.py"))
    inputs = {"builder": SPEC["builder"]["sha"],
              "recipe": hashlib.sha256(builder_steps().encode()).hexdigest()[:16],
              "frappe": {"version": m.group(1), "sha": fsha}}
    for app in SPEC["apps"]:
        if "branch" in app:
            ref = f"refs/heads/{app['branch']}"
            inputs[app["name"]] = {"branch": app["branch"], "sha": ls_remote(app["repo"], ref)[ref]}
            continue
        pattern = re.compile(app["tags"])
        tags = {ref.removeprefix("refs/tags/"): sha
                for ref, sha in ls_remote(app["repo"], tags=True).items()}
        for tag in sorted((t for t in tags if pattern.match(t)), key=vtuple, reverse=True):
            spec = frappe_range(app["repo"], tags[tag])
            if spec is None or admits(spec, inputs["frappe"]["version"]):
                inputs[app["name"]] = {"version": tag, "sha": tags[tag]}
                break
            log(f"{app['name']} {tag} needs frappe {spec}; looking further back")
        else:
            raise SystemExit(f"no {app['name']} release admits frappe {inputs['frappe']['version']}")
    return inputs


def registry(path: str, accept: str = "application/json") -> dict | None:
    """One anonymous GHCR read; None when the image does not exist yet."""
    repo = SPEC["image"].removeprefix("ghcr.io/")
    try:
        with urllib.request.urlopen(
                f"https://ghcr.io/token?scope=repository:{repo}:pull", timeout=30) as r:
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


def cmd_resolve(args) -> None:
    inputs = resolve_inputs()
    tags = published()
    previous = tags[-1] if tags else None
    last = built_from(previous) if previous else None
    build = args.force or last != inputs
    out = {"inputs": inputs, "build": build, "previous": previous,
           "tag": next_tag(inputs, tags) if build else previous}
    INPUTS.write_text(json.dumps(out, indent=2) + "\n")
    log(f"previous={previous} build={build} tag={out['tag']}")
    print(json.dumps(out, indent=2))


# --- build -------------------------------------------------------------------

def load() -> dict:
    return json.loads(INPUTS.read_text())


def cmd_build(args) -> None:
    state = load()
    inputs = state["inputs"]
    work = Path(tempfile.mkdtemp(prefix="erpnext-image-"))
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
        "--build-arg", f"FRAPPE_BRANCH={SPEC['frappe']['branch']}",
        "--build-arg", f"CACHE_BUST={bust}",
        "--secret", f"id=apps_json,src={apps_json}",
        "--label", f"org.opencontainers.image.version={state['tag']}",
        "--label", f"org.opencontainers.image.source=https://github.com/catenahq/catena-templates",
        "--label", f"io.catena.inputs={json.dumps(inputs, sort_keys=True)}",
        "-f", str(containerfile), "-t", image, str(builder))

    present = run("docker", "run", "--rm", "--entrypoint", "ls", image,
                  "/home/frappe/frappe-bench/apps", capture=True).split()
    missing = {"frappe", *(a["name"] for a in SPEC["apps"])} - set(present)
    if missing:
        raise SystemExit(f"{image} lacks {sorted(missing)}: the apps did not reach the image")
    log(f"built {image} with {sorted(present)}")


# --- test --------------------------------------------------------------------

def compose(image: str, *argv: str) -> None:
    """docker compose over the smoke stack, running `image`. Apps install in
    apps.json order, which puts each one after the apps it requires."""
    env = {**os.environ, "IMG": image,
           "INSTALL_ARGS": " ".join(f"--install-app {a['name']}" for a in SPEC["apps"])}
    argv = ("docker", "compose", "-p", "erpnext-smoke",
            "-f", str(HERE / "smoke-compose.yml"), *argv)
    log("$ " + " ".join(argv))
    subprocess.run(argv, check=True, text=True, env=env)


def wait_site(image: str) -> None:
    for _ in range(120):
        state = run("docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}",
                    "erpnext-smoke-create-site-1", capture=True).strip()
        if state.startswith("exited"):
            if state != "exited 0":
                compose(image, "logs", "create-site")
                raise SystemExit(f"site creation failed: {state}")
            return
        time.sleep(5)
    raise SystemExit("site creation did not finish in 10 minutes")


def ping(image: str) -> None:
    # nginx resolves the backend once at start; a restarted backend may move.
    compose(image, "restart", "frontend")
    for _ in range(30):
        res = subprocess.run(["curl", "-s", "-H", "Host: frontend",
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
        wait_site(first)
        if first != new:
            log(f"upgrading the site from {first} to {new}")
            compose(new, "up", "-d")
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
    run("docker", "push", f"{SPEC['image']}:{load()['tag']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--force", action="store_true", help="build even when nothing moved")
    sub.add_parser("build")
    t = sub.add_parser("test")
    t.add_argument("--fresh", action="store_true", help="skip the upgrade from the previous image")
    sub.add_parser("publish")
    args = p.parse_args()
    {"resolve": cmd_resolve, "build": cmd_build, "test": cmd_test,
     "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    main()
