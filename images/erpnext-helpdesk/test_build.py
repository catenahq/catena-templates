"""Unit tests for build.py: the image's smoke test, and the commands that read
apps.json, plan a build, publish and pin the blueprint.

The smoke stack runs the catalog template's backend, so the test fails on an
image whose backend cannot set its site up, a fresh one or the previous
image's. Every case here runs without docker or network: the docker, git and
registry calls are recorded or stubbed, and answered where build.py reads them.

Run: uv run --with pytest --with pyyaml pytest images/erpnext-helpdesk -q
"""
from __future__ import annotations

import argparse
import base64
import copy
import importlib.util
import io
import json
import re
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("erpnext_helpdesk_build", HERE / "build.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

SMOKE = yaml.safe_load((HERE / "smoke-compose.yml").read_text())


def _blueprint_backend() -> dict:
    extends = SMOKE["services"]["backend"]["extends"]
    path = (HERE / extends["file"]).resolve()
    return yaml.safe_load(path.read_text())["services"][extends["service"]]


# --- the smoke stack --------------------------------------------------------

def test_the_smoke_backend_is_the_templates():
    """No command or entrypoint of its own: the template's script runs."""
    backend = SMOKE["services"]["backend"]
    extends = backend["extends"]
    assert (HERE / extends["file"]).resolve() == (
        HERE.parent.parent / "blueprints" / "erpnext-helpdesk" / "docker-compose.yml")
    assert extends["service"] == "backend"
    assert set(backend) == {"extends", "image"}
    assert backend["image"] == "${IMG}"
    shell, flag, script = _blueprint_backend()["command"]
    assert (shell, flag) == ("bash", "-c")
    assert "install-app" in script
    assert script.rstrip().endswith("exec start.sh")


def test_the_smoke_env_carries_what_the_templates_backend_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(B.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    B.compose("img:1", "up", "-d")
    [(argv, kw)] = calls
    assert argv[-2:] == ("up", "-d")
    env = kw["env"]
    names = set(re.findall(r"\$\{(\w+)", str(_blueprint_backend()["environment"])))
    assert {"ERPNEXT_HOSTNAME", "DB_ROOT_PASSWORD"} <= names
    assert [name for name in sorted(names) if not env.get(name)] == []
    assert env["IMG"] == "img:1"
    assert env["ERPNEXT_HOSTNAME"] == B.SITE
    assert env["FRAPPE_APPS"] == ",".join(a["name"] for a in B.SPEC["apps"])


def test_the_database_and_the_router_read_the_same_values():
    services = SMOKE["services"]
    assert services["db"]["environment"]["MARIADB_ROOT_PASSWORD"] == "${DB_ROOT_PASSWORD}"
    assert services["frontend"]["environment"]["FRAPPE_SITE_NAME_HEADER"] == "${ERPNEXT_HOSTNAME}"


# --- the order of the test --------------------------------------------------

def _record(monkeypatch, state):
    events = []
    monkeypatch.setattr(B, "load", lambda: state)
    monkeypatch.setattr(B, "run", lambda *argv, **_kw: events.append(("run", *argv)) or "")
    monkeypatch.setattr(B, "compose", lambda image, *argv: events.append((image, *argv)))
    monkeypatch.setattr(B, "wait_backend", lambda image: events.append(("backend up", image)))
    monkeypatch.setattr(B, "ping", lambda image: events.append(("ping", image)))
    return events


def test_the_upgrade_starts_the_new_backend_on_the_old_site_before_migrating(monkeypatch):
    events = _record(monkeypatch, {"tag": "v16.1.0-2", "previous": "v16.1.0-1"})
    B.cmd_test(argparse.Namespace(fresh=False))
    old, new = f"{B.SPEC['image']}:v16.1.0-1", f"{B.SPEC['image']}:v16.1.0-2"
    assert events[:6] == [
        ("run", "docker", "pull", old),
        (old, "up", "-d"),
        ("backend up", old),
        (new, "up", "-d"),
        ("backend up", new),
        (new, "exec", "-T", "backend", "bench", "--site", "all", "migrate"),
    ]
    assert events[-2:] == [("ping", new), (new, "down", "-v")]


def test_a_fresh_test_sets_the_site_up_on_the_new_image_only(monkeypatch):
    events = _record(monkeypatch, {"tag": "v16.1.0-2", "previous": "v16.1.0-1"})
    B.cmd_test(argparse.Namespace(fresh=True))
    new = f"{B.SPEC['image']}:v16.1.0-2"
    assert events[:3] == [(new, "up", "-d"), ("backend up", new),
                          (new, "exec", "-T", "backend", "bench", "--site", "all", "migrate")]
    assert all(e[0] in (new, "backend up", "ping") for e in events)


def test_the_stack_comes_down_when_the_backend_fails(monkeypatch):
    events = _record(monkeypatch, {"tag": "v16.1.0-2", "previous": None})

    def fail(image):
        raise SystemExit("the backend's setup failed")

    monkeypatch.setattr(B, "wait_backend", fail)
    with pytest.raises(SystemExit):
        B.cmd_test(argparse.Namespace(fresh=False))
    new = f"{B.SPEC['image']}:v16.1.0-2"
    assert events == [(new, "up", "-d"), (new, "down", "-v")]


# --- the backend wait -------------------------------------------------------

def _backend(monkeypatch, states, probes):
    """wait_backend with `states` as docker inspect's answers and `probes` as
    the port probe's exit codes. Returns the compose calls it made."""
    states, probes = iter(states), iter(probes)
    composed = []
    monkeypatch.setattr(B, "run", lambda *_a, **_k: next(states))
    monkeypatch.setattr(B.subprocess, "run",
                        lambda argv, **_k: subprocess.CompletedProcess(argv, next(probes)))
    monkeypatch.setattr(B, "compose", lambda image, *argv: composed.append(argv))
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)
    return composed


def test_a_backend_that_reaches_its_app_server_passes(monkeypatch):
    composed = _backend(monkeypatch, ["running 0 0\n"] * 3, [1, 1, 0])
    B.wait_backend("img:1")
    assert composed == []


@pytest.mark.parametrize("state,needle", [
    ("exited 1 0\n", "exited, exit code 1"),
    ("running 0 1\n", "1 restart(s)"),
    ("restarting 1 3\n", "restarting"),
])
def test_a_setup_step_that_fails_fails_the_test_with_its_logs(monkeypatch, state, needle):
    composed = _backend(monkeypatch, ["running 0 0\n", state], [1])
    with pytest.raises(SystemExit, match=re.escape(needle)):
        B.wait_backend("img:1")
    assert composed == [("logs", "backend")]


def test_a_backend_that_never_serves_fails_the_test(monkeypatch):
    composed = _backend(monkeypatch, ["running 0 0\n"] * 120, [1] * 120)
    with pytest.raises(SystemExit, match="did not reach its app server"):
        B.wait_backend("img:1")
    assert composed == [("logs", "backend")]


# --- apps.json, plan, pin ----------------------------------------------------

REPO = {"frappe": "https://github.com/frappe/frappe",
        "erpnext": "https://github.com/frappe/erpnext",
        "telephony": "https://github.com/frappe/telephony",
        "helpdesk": "https://github.com/frappe/helpdesk"}


@pytest.fixture
def spec(monkeypatch, tmp_path):
    """A copy of apps.json the commands may rewrite, with known pins."""
    doc = copy.deepcopy(B.SPEC)
    doc["frappe"].update(version="v16.1.0", sha="f1")
    for app in doc["apps"]:
        app["sha"] = app["name"][0] + "1"
        if "tags" in app:
            app["version"] = "v16.1.0" if app["name"] == "erpnext" else "v1.0.0"
    monkeypatch.setattr(B, "SPEC", doc)
    monkeypatch.setattr(B, "APPS_JSON", tmp_path / "apps.json")
    monkeypatch.setattr(B, "INPUTS", tmp_path / "inputs.json")
    return doc


def _stub_upstream(monkeypatch, tags, heads=None, ranges=None, absent=()):
    """ls_remote answers from `tags` {repo: {tag: sha}} and `heads` {repo: sha};
    frappe_range from `ranges` {sha: range}; Docker Hub lacks the (name, tag)
    pairs in `absent`."""
    monkeypatch.setattr(B, "hub_tag_exists", lambda name, tag: (name, tag) not in absent)
    def ls_remote(repo, *refs, tags=False, _tags=tags, _heads=heads or {}):
        if tags:
            return {f"refs/tags/{t}": sha for t, sha in _tags[repo].items()}
        return {ref: _heads[repo] for ref in refs}

    monkeypatch.setattr(B, "ls_remote", ls_remote)
    monkeypatch.setattr(B, "frappe_range", lambda repo, sha: (ranges or {}).get(sha))


def test_inputs_carry_every_pin_in_the_labels_shape(spec):
    assert B.inputs() == {
        "builder": spec["builder"]["sha"],
        "recipe": B.inputs()["recipe"],
        "frappe": {"version": "v16.1.0", "sha": "f1"},
        "erpnext": {"version": "v16.1.0", "sha": "e1"},
        "telephony": {"branch": "develop", "sha": "t1"},
        "helpdesk": {"version": "v1.0.0", "sha": "h1"},
    }


PINNED_TAGS = {REPO["frappe"]: {"v16.1.0": "f1"}, REPO["erpnext"]: {"v16.1.0": "e1"},
               REPO["helpdesk"]: {"v1.0.0": "h1"}}


def test_resolve_sets_every_sha_from_upstream(spec, monkeypatch):
    """A pinned tag's sha is the commit the tag names now; a branch's, its head."""
    _stub_upstream(monkeypatch, heads={REPO["telephony"]: "t2"}, tags={
        REPO["frappe"]: {"v16.1.0": "f9"}, REPO["erpnext"]: {"v16.1.0": "e9"},
        REPO["helpdesk"]: {"v1.0.0": "h9"}})
    B.cmd_resolve(argparse.Namespace(newest=False))
    assert B.inputs() | {"recipe": ""} == {
        "builder": spec["builder"]["sha"], "recipe": "",
        "frappe": {"version": "v16.1.0", "sha": "f9"},
        "erpnext": {"version": "v16.1.0", "sha": "e9"},
        "telephony": {"branch": "develop", "sha": "t2"},
        "helpdesk": {"version": "v1.0.0", "sha": "h9"}}


def test_resolve_fails_when_a_pinned_tag_no_longer_exists(spec, monkeypatch):
    _stub_upstream(monkeypatch, heads={REPO["telephony"]: "t2"}, tags={
        **PINNED_TAGS, REPO["helpdesk"]: {"v1.1.0": "h2"}})
    with pytest.raises(SystemExit, match="no tag v1.0.0"):
        B.cmd_resolve(argparse.Namespace(newest=False))


def test_resolve_keeps_tag_pins_and_refreshes_branch_shas(spec, monkeypatch):
    _stub_upstream(monkeypatch, tags=PINNED_TAGS, heads={REPO["telephony"]: "t2"})
    keys = [list(a) for a in spec["apps"]]
    B.cmd_resolve(argparse.Namespace(newest=False))
    written = json.loads(B.APPS_JSON.read_text())
    assert B.APPS_JSON.read_text().endswith("}\n")
    assert [list(a) for a in written["apps"]] == keys
    assert written["frappe"]["version"] == "v16.1.0"
    got = {a["name"]: a["sha"] for a in written["apps"]}
    assert got == {"erpnext": "e1", "telephony": "t2", "helpdesk": "h1"}


def test_resolve_newest_moves_to_the_newest_admissible_tags(spec, monkeypatch):
    _stub_upstream(
        monkeypatch,
        tags={REPO["frappe"]: {"v16.1.0": "f1", "v16.3.0": "f3", "v16.2.0": "f2", "v17.0.0": "f9"},
              REPO["erpnext"]: {"v16.1.0": "e1", "v16.2.0": "e2", "v15.9.0": "e0"},
              REPO["helpdesk"]: {"v1.0.0": "h1", "v1.1.0": "h2", "v1.2.0": "h3"}},
        heads={REPO["telephony"]: "t2"},
        ranges={"h3": ">=16.0.0,<16.3.0", "h2": ">=16.0.0,<17.0.0"})
    B.cmd_resolve(argparse.Namespace(newest=True))
    assert B.inputs() == {
        "builder": spec["builder"]["sha"], "recipe": B.inputs()["recipe"],
        "frappe": {"version": "v16.3.0", "sha": "f3"},
        "erpnext": {"version": "v16.2.0", "sha": "e2"},
        "telephony": {"branch": "develop", "sha": "t2"},
        "helpdesk": {"version": "v1.1.0", "sha": "h2"}}
    assert json.loads(B.APPS_JSON.read_text()) == spec


def test_resolve_newest_fails_when_no_release_admits_the_frappe(spec, monkeypatch):
    _stub_upstream(
        monkeypatch,
        tags={REPO["frappe"]: {"v16.3.0": "f3"}, REPO["erpnext"]: {"v16.2.0": "e2"},
              REPO["helpdesk"]: {"v1.2.0": "h3"}},
        heads={REPO["telephony"]: "t2"}, ranges={"h3": "<16.0.0"})
    with pytest.raises(SystemExit, match="no helpdesk release admits frappe v16.3.0"):
        B.cmd_resolve(argparse.Namespace(newest=True))


FRAPPE_TAGS = {REPO["frappe"]: {"v16.2.0": "f2", "v16.3.0": "f3"},
               REPO["erpnext"]: {"v16.2.0": "e2"}, REPO["helpdesk"]: {"v1.1.0": "h2"}}


@pytest.mark.parametrize("absent", [{("build", "v16.3.0")}, {("base", "v16.3.0")}])
def test_resolve_newest_skips_a_frappe_tag_without_its_builder_images(spec, monkeypatch, absent):
    _stub_upstream(monkeypatch, tags=FRAPPE_TAGS, heads={REPO["telephony"]: "t2"}, absent=absent)
    B.cmd_resolve(argparse.Namespace(newest=True))
    assert (spec["frappe"]["version"], spec["frappe"]["sha"]) == ("v16.2.0", "f2")


def test_resolve_newest_fails_when_no_frappe_tag_has_its_builder_images(spec, monkeypatch):
    _stub_upstream(monkeypatch, tags=FRAPPE_TAGS, heads={REPO["telephony"]: "t2"},
                   absent={("build", "v16.3.0"), ("base", "v16.2.0")})
    with pytest.raises(SystemExit, match="frappe/build and frappe/base"):
        B.cmd_resolve(argparse.Namespace(newest=True))


@pytest.mark.parametrize("code,want", [(404, False), (200, True)])
def test_the_hub_answer_decides_whether_a_builder_image_exists(monkeypatch, code, want):
    def urlopen(url, timeout=None):
        assert url.endswith("/namespaces/frappe/repositories/build/tags/v16.2.0")
        if code != 200:
            raise urllib.error.HTTPError(url, code, "x", {}, None)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(B.urllib.request, "urlopen", urlopen)
    assert B.hub_tag_exists("build", "v16.2.0") is want


def test_any_other_hub_answer_raises(monkeypatch):
    def urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "x", {}, None)

    monkeypatch.setattr(B.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        B.hub_tag_exists("build", "v16.2.0")


PINS = """x-catena:
  images:
    backend:
      rule: {repo: catenahq/erpnext-helpdesk}

services:
  # kept comment
""" + "".join(f"  {name}:\n    image: {B.SPEC['image']}:v16.1.0-1\n    command: x\n"
              for name in B.FRAPPE_SERVICES) + "  db:\n    image: mariadb:11.8.9\n"


@pytest.fixture
def blueprint(monkeypatch, tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(PINS)
    monkeypatch.setattr(B, "BLUEPRINT", path)
    return path


def _plan(monkeypatch, labels, force=False):
    """cmd_plan with `labels` {tag: io.catena.inputs label} published."""
    monkeypatch.setattr(B, "published", lambda: sorted(labels, key=B.vtuple))
    monkeypatch.setattr(B, "built_from", lambda tag: labels[tag])
    B.cmd_plan(argparse.Namespace(force=force))
    return json.loads(B.INPUTS.read_text())


OTHER = {"frappe": {"version": "v16.0.0", "sha": "f0"}}


def _pinned(blueprint, tag):
    blueprint.write_text(PINS.replace("v16.1.0-1", tag))


def test_plan_builds_when_no_published_image_was_built_from_these_inputs(spec, blueprint, monkeypatch):
    out = _plan(monkeypatch, {"v16.1.0-1": OTHER})
    assert out == {"inputs": B.inputs(), "build": True, "previous": "v16.1.0-1",
                   "pin": True, "tag": "v16.1.0-2"}


def test_plan_builds_the_first_image_when_none_is_published(spec, blueprint, monkeypatch):
    out = _plan(monkeypatch, {})
    assert (out["build"], out["previous"], out["tag"], out["pin"]) == (True, None, "v16.1.0-1", False)


def test_plan_builds_nothing_when_the_label_equals_the_inputs(spec, blueprint, monkeypatch):
    out = _plan(monkeypatch, {"v16.1.0-1": B.inputs()})
    assert (out["build"], out["tag"], out["pin"]) == (False, "v16.1.0-1", False)


def test_plan_takes_the_older_build_of_the_release_that_already_holds_these_inputs(spec, blueprint, monkeypatch):
    _pinned(blueprint, "v16.1.0-3")
    out = _plan(monkeypatch, {"v16.1.0-1": B.inputs(), "v16.1.0-2": OTHER, "v16.1.0-3": OTHER})
    assert (out["build"], out["tag"], out["pin"], out["previous"]) == (False, "v16.1.0-1", True, None)


def test_plan_ignores_the_labels_of_other_releases(spec, blueprint, monkeypatch):
    out = _plan(monkeypatch, {"v16.0.0-1": B.inputs()})
    assert (out["build"], out["tag"]) == (True, "v16.1.0-1")


def test_plan_force_builds_without_a_difference(spec, blueprint, monkeypatch):
    out = _plan(monkeypatch, {"v16.1.0-1": B.inputs()}, force=True)
    assert (out["build"], out["tag"]) == (True, "v16.1.0-2")


def test_previous_is_the_pinned_tag_when_it_is_published_and_older(spec, blueprint, monkeypatch):
    _pinned(blueprint, "v16.0.0-1")
    assert _plan(monkeypatch, {"v16.0.0-1": OTHER})["previous"] == "v16.0.0-1"


def test_previous_is_none_when_the_pinned_tag_is_not_published(spec, blueprint, monkeypatch):
    assert _plan(monkeypatch, {"v16.0.0-1": OTHER})["previous"] is None


def test_pin_rewrites_the_seven_image_lines_and_keeps_the_rest(spec, blueprint, monkeypatch):
    monkeypatch.setattr(B, "load", lambda: {"tag": "v16.2.0-1"})
    B.cmd_pin(argparse.Namespace())
    text = blueprint.read_text()
    assert text.count(f"{B.SPEC['image']}:v16.2.0-1") == 7
    assert text == PINS.replace("v16.1.0-1", "v16.2.0-1")
    assert B.pinned_tag() == "v16.2.0-1"


@pytest.mark.parametrize("edit", [
    lambda t: t.replace(f"    image: {B.SPEC['image']}:v16.1.0-1\n    command: x\n  scheduler", "    command: x\n  scheduler"),
    lambda t: t + f"  extra:\n    image: {B.SPEC['image']}:v16.1.0-1\n",
])
def test_pin_refuses_another_set_of_services(spec, blueprint, monkeypatch, edit):
    blueprint.write_text(edit(PINS))
    before = blueprint.read_text()
    monkeypatch.setattr(B, "load", lambda: {"tag": "v16.2.0-1"})
    with pytest.raises(SystemExit):
        B.cmd_pin(argparse.Namespace())
    assert blueprint.read_text() == before


# --- publish and the registry ------------------------------------------------

def test_publish_refuses_to_run_without_ghcr_token(monkeypatch):
    ran = []
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.setattr(B, "load", lambda: {"tag": "v16.1.0-2"})
    monkeypatch.setattr(B, "published", lambda: [])
    monkeypatch.setattr(B, "run", lambda *argv, **_k: ran.append(argv))
    with pytest.raises(SystemExit, match="GHCR_TOKEN"):
        B.cmd_publish(argparse.Namespace())
    assert ran == []


def test_publish_refuses_a_tag_the_registry_holds(monkeypatch):
    monkeypatch.setenv("GHCR_TOKEN", "secret")
    ran = []
    monkeypatch.setattr(B, "load", lambda: {"tag": "v16.1.0-1"})
    monkeypatch.setattr(B, "published", lambda: ["v16.1.0-1"])
    monkeypatch.setattr(B, "run", lambda *argv, **_k: ran.append(argv))
    with pytest.raises(SystemExit, match="already published"):
        B.cmd_publish(argparse.Namespace())
    assert ran == []


def test_publish_pushes_a_new_tag(monkeypatch):
    monkeypatch.setenv("GHCR_TOKEN", "secret")
    ran = []
    monkeypatch.setattr(B, "load", lambda: {"tag": "v16.1.0-2"})
    monkeypatch.setattr(B, "published", lambda: ["v16.1.0-1"])
    monkeypatch.setattr(B, "run", lambda *argv, **_k: ran.append(argv))
    B.cmd_publish(argparse.Namespace())
    assert ran == [("docker", "push", f"{B.SPEC['image']}:v16.1.0-2")]


def _registry_requests(monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return io.BytesIO(b'{"token": "t", "tags": []}')

    monkeypatch.setattr(B.urllib.request, "urlopen", urlopen)
    return seen


def test_registry_reads_authenticate_with_ghcr_token(monkeypatch):
    monkeypatch.setenv("GHCR_USER", "me")
    monkeypatch.setenv("GHCR_TOKEN", "secret")
    seen = _registry_requests(monkeypatch)
    B.registry("tags/list")
    assert seen[0].get_header("Authorization") == "Basic " + base64.b64encode(b"me:secret").decode()
    assert seen[1].get_header("Authorization") == "Bearer t"


def test_registry_reads_are_anonymous_without_ghcr_token(monkeypatch):
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    seen = _registry_requests(monkeypatch)
    B.registry("tags/list")
    assert not seen[0].has_header("Authorization")


# --- build -------------------------------------------------------------------

def _build(monkeypatch, tmp_path, telephony_head):
    """cmd_build with docker and git recorded; the registry of branch heads
    answers `telephony_head`. Returns the recorded calls."""
    calls = []

    def run(*argv, **_k):
        calls.append(argv)
        if argv[:2] == ("git", "clone"):
            containerfile = Path(argv[-1]) / "images" / "layered" / "Containerfile"
            containerfile.parent.mkdir(parents=True)
            containerfile.write_text(f"RUN x && \\\n  {B.ANCHOR}\n")
        if argv[:2] == ("docker", "run"):
            return "frappe erpnext telephony helpdesk"
        return ""

    monkeypatch.setattr(B, "run", run)
    monkeypatch.setattr(B, "ls_remote", lambda repo, *refs, **_k: {refs[0]: telephony_head})
    monkeypatch.setattr(B.tempfile, "mkdtemp", lambda **_k: str(tmp_path / "work"))
    monkeypatch.setattr(B, "load", lambda: {"inputs": B.inputs(), "tag": "v16.1.0-1"})
    B.cmd_build(argparse.Namespace())
    return calls


def test_build_passes_the_frappe_tag_as_frappe_branch(spec, monkeypatch, tmp_path):
    calls = _build(monkeypatch, tmp_path, "t1")
    [build] = [c for c in calls if c[:3] == ("docker", "buildx", "build")]
    assert "FRAPPE_BRANCH=v16.1.0" in build
    sent = json.loads((tmp_path / "work" / "apps.json").read_text())
    assert [a["branch"] for a in sent] == ["v16.1.0", "develop", "v1.0.0"]


def test_build_fails_when_a_branch_moved_while_it_ran(spec, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="telephony develop moved to t2"):
        _build(monkeypatch, tmp_path, "t2")
