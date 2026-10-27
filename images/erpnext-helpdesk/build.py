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
           node_modules removed in the same layer; the result is repacked into
           the layer groups of LAYERS, and the build fails unless the repack
           holds the same files and configuration and no layer exceeds
           LAYER_BOUND
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


# --- the repack --------------------------------------------------------------

# The backend image's tree is copied into a scratch stage one group per path,
# so the image holds about ten layers.
# A path's group holds what is under it except the groups listed below it.
LAYERS = ("/home/frappe/frappe-bench/env", "/home/frappe/frappe-bench/apps/frappe",
          "/home/frappe/frappe-bench/apps/erpnext", "/home/frappe/frappe-bench",
          "/home/frappe/.nvm", "/home/frappe", "/usr/lib/x86_64-linux-gnu", "/usr/lib",
          "/usr/share", "/usr/local", "/")
# The largest layer, in bytes, a built image may hold.
LAYER_BOUND = 400 * 1000 * 1000
# COPY --exclude needs Dockerfile 1.19 or later.
SYNTAX = ("# syntax=docker/dockerfile:1.27.1"
          "@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e")
# The fields of an image's .Config that name the build, not its content: the
# parent image id and the build host. Every other field must be equal, so one
# the scratch stage does not restate (a Healthcheck, say) fails the build.
CONFIG_IGNORED = ("Image", "Hostname")


def under(path: str, parent: str) -> bool:
    return path != parent and (parent == "/" or path.startswith(parent + "/"))


def copy_lines(layers: tuple[str, ...] = ()) -> list[str]:
    """One COPY per group, a parent before the groups below it. A group
    excludes the contents of the topmost groups below it, not their directories.

    The COPY semantics this relies on (tonistiigi/fsutil copy/copy.go, as pinned
    by BuildKit dockerfile/1.27.1):
    - an exclude pattern matches a path relative to the COPY source, with no
      leading slash, and a matching directory is skipped whole;
    - a missing destination directory is created root-owned with mode 0755, and
      the source directory's own owner and mode are copied only when the
      directory is created by the copy of its parent's contents;
    - the entries inside a copied directory keep their metadata;
    - hard links survive within one COPY only."""
    layers = layers or LAYERS
    lines = []
    for path in sorted(layers, key=lambda p: (len(p.strip("/").split("/")) if p != "/" else 0, p)):
        below = [c for c in layers if under(c, path)]
        top = [c for c in below if not any(under(c, o) for o in below)]
        excludes = "".join(f" --exclude={c.removeprefix(path).strip('/')}/*" for c in top)
        lines.append(f"COPY --from=backend{excludes} {path} {path}")
    return lines


def quote(value: str) -> str:
    """A double-quoted Containerfile word holding exactly `value`."""
    if re.search(r"[\x00-\x1f]", value):
        raise SystemExit(f"a configuration value holds a control character: {value!r}")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def stage(config: dict) -> str:
    """The scratch stage: the groups of LAYERS, then the backend image's
    configuration."""
    out = ["FROM scratch AS catena", *copy_lines()]
    out += [f"ENV {key}={quote(value)}" for key, _, value in
            (e.partition("=") for e in config.get("Env") or [])]
    if config.get("User"):
        out.append(f"USER {config['User']}")
    if config.get("WorkingDir"):
        out.append(f"WORKDIR {config['WorkingDir']}")
    if config.get("Entrypoint"):
        out.append(f"ENTRYPOINT {json.dumps(config['Entrypoint'])}")
    if config.get("Cmd"):
        out.append(f"CMD {json.dumps(config['Cmd'])}")
    if config.get("Volumes"):
        out.append(f"VOLUME {json.dumps(sorted(config['Volumes']))}")
    if config.get("ExposedPorts"):
        out.append("EXPOSE " + " ".join(sorted(config["ExposedPorts"])))
    out += [f"LABEL {quote(k)}={quote(v)}" for k, v in sorted((config.get("Labels") or {}).items())]
    return "\n".join(out) + "\n"


def with_syntax(text: str) -> str:
    """The Containerfile with SYNTAX as its first line."""
    lines = text.split("\n")
    if lines[0].startswith("# syntax="):
        lines = lines[1:]
    return "\n".join([SYNTAX, *lines])


# Prints one line per path of the image's tree: path, type, uid, gid, mode,
# size, link target, extended attributes, content sha256, link count and the
# first path of its hard-link group.
WALK = r'''
import hashlib, json, os, stat, sys
SKIP = {"/proc", "/sys", "/dev", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
first = {}
def visit(p):
    st = os.lstat(p)
    mode = st.st_mode
    kind = ("d" if stat.S_ISDIR(mode) else "l" if stat.S_ISLNK(mode) else
            "f" if stat.S_ISREG(mode) else "o")
    target = os.readlink(p) if kind == "l" else ""
    try:
        names = sorted(os.listxattr(p, follow_symlinks=False))
        xattrs = ",".join(n + "=" + os.getxattr(p, n, follow_symlinks=False).hex() for n in names)
    except OSError:
        xattrs = ""
    digest = ""
    if kind == "f":
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
    link = first.setdefault((st.st_dev, st.st_ino), p) if kind == "f" and st.st_nlink > 1 else ""
    print("\t".join([json.dumps(p), kind, str(st.st_uid), str(st.st_gid), oct(stat.S_IMODE(mode)),
                     str(st.st_size if kind == "f" else 0), json.dumps(target), xattrs, digest,
                     str(st.st_nlink if kind == "f" else 0), json.dumps(link if link != p else "")]))
    if kind == "d":
        for name in sorted(os.listdir(p)):
            child = os.path.join(p, name)
            if child not in SKIP:
                visit(child)
visit("/")
'''
WALK_FIELDS = ("path", "type", "uid", "gid", "mode", "size", "link", "xattrs", "sha256",
               "nlink", "hardlink-of")


def walk(image: str) -> str:
    return run("docker", "run", "--rm", "--user", "0", "--entrypoint", "python3",
               image, "-c", WALK, capture=True)


def tree_diff(want: str, got: str, limit: int = 20) -> list[str]:
    """The first `limit` differences between two walks: a path one lacks, one
    adds, or whose fields differ."""
    def parse(text):
        return {json.loads(line.split("\t")[0]): line.split("\t") for line in text.splitlines() if line}

    a, b = parse(want), parse(got)
    found = [f"missing from the repack: {p}" for p in a if p not in b]
    found += [f"added by the repack: {p}" for p in b if p not in a]
    for path, fields in a.items():
        if path in b and fields != b[path]:
            changed = [f"{n} {x} -> {y}" for n, x, y in zip(WALK_FIELDS, fields, b[path]) if x != y]
            found.append(f"{path}: " + ", ".join(changed))
    return found[:limit]


def config_diff(want: dict, got: dict) -> list[str]:
    return [f"{field}: {want.get(field)!r} -> {got.get(field)!r}"
            for field in sorted((want.keys() | got.keys()) - set(CONFIG_IGNORED))
            if (want.get(field) or None) != (got.get(field) or None)]


def oversize(history: str, bound: int = LAYER_BOUND) -> list[str]:
    """The layers of a `docker history --human=false` listing (size, tab,
    command) above `bound` bytes."""
    found = []
    for line in history.splitlines():
        size, _, command = line.partition("\t")
        if size.isdigit() and int(size) > bound:
            found.append(f"{int(size)} bytes: {command[:120]}")
    return found


def image_config(image: str) -> dict:
    return json.loads(run("docker", "image", "inspect", "--format", "{{json .Config}}",
                          image, capture=True))


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
           "recipe": hashlib.sha256("\n".join([builder_steps(), SYNTAX, *copy_lines()]).encode()
                                    ).hexdigest()[:16],
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
    if not re.search(r"^FROM .* AS backend\s*$", text, re.M):
        raise SystemExit("the layered Containerfile has no backend stage: review the builder bump")
    patched = with_syntax(text.replace(ANCHOR, builder_steps()))
    containerfile.write_text(patched)

    apps = []
    for app in SPEC["apps"]:
        got = inputs[app["name"]]
        apps.append({"url": app["repo"], "branch": got.get("version") or got["branch"]})
    apps_json = work / "apps.json"
    apps_json.write_text(json.dumps(apps))
    bust = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    image = f"{SPEC['image']}:{state['tag']}"
    def buildx(target: str, tag: str) -> None:
        run("docker", "buildx", "build", "--load", "--target", target,
            "--build-arg", f"FRAPPE_BRANCH={SPEC['frappe']['version']}",
            "--build-arg", f"CACHE_BUST={bust}",
            "--secret", f"id=apps_json,src={apps_json}",
            "--label", f"org.opencontainers.image.version={state['tag']}",
            "--label", f"org.opencontainers.image.source=https://github.com/catenahq/catena-templates",
            "--label", f"io.catena.inputs={json.dumps(inputs, sort_keys=True)}",
            "-f", str(containerfile), "-t", tag, str(builder))

    backend = f"{image}-backend"
    buildx("backend", backend)
    config = image_config(backend)
    containerfile.write_text(patched + "\n" + stage(config))
    buildx("catena", image)

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

    differences = [f"configuration {d}" for d in config_diff(config, image_config(image))]
    differences += tree_diff(walk(backend), walk(image))
    if differences:
        raise SystemExit(f"the repack of {image} differs from the backend image:\n  "
                         + "\n  ".join(differences[:20]))
    big = oversize(run("docker", "history", "--no-trunc", "--human=false",
                       "--format", "{{.Size}}\t{{.CreatedBy}}", image, capture=True))
    if big:
        raise SystemExit(f"{image} has a layer above {LAYER_BOUND} bytes:\n  " + "\n  ".join(big))
    run("docker", "image", "rm", backend)


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
