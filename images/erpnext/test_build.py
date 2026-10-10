"""Unit tests for build.py test, the image's smoke test.

The smoke stack runs the catalog template's backend, so the test fails on an
image whose backend cannot set its site up, a fresh one or the previous
image's. Every case here runs without docker: the docker calls are recorded,
and answered where build.py reads them.

Run: uv run --with pytest --with pyyaml pytest images/erpnext -q
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("erpnext_build", HERE / "build.py")
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
        HERE.parent.parent / "blueprints" / "erpnext" / "docker-compose.yml")
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
