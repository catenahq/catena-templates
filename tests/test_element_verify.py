"""Element and Synapse: the verify hook run against a stub of the server it asks."""
from __future__ import annotations

import http.server
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

SLUG = "element"
INTERPRETER = "python3"
needs_interpreter = pytest.mark.skipif(shutil.which(INTERPRETER) is None, reason=INTERPRETER + " is not installed")


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == SLUG)


def _compose() -> dict:
    return yaml.safe_load((ROOT / "blueprints" / SLUG / "docker-compose.yml").read_text(encoding="utf-8"))


def _verify() -> list[str]:
    [argv] = _entry().block["hooks"]["verify"]
    assert argv[0] == INTERPRETER
    return argv


def test_the_hook_runs_in_a_service_of_the_stack():
    hooks = _entry().block["hooks"]
    assert hooks["service"] == "synapse"
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert hooks["timeout_seconds"] <= 60
    assert quiesce_lint.lint_hooks(_entry()) == []


class _Stub(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    seen: list[dict[str, str]] = []

    def do_GET(self):
        type(self).seen.append(dict(self.headers))
        status, body = type(self).routes.get(self.path, (404, b""))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _run(routes: dict[str, tuple[int, bytes]], *, answer: bool = True, swap=(), env=None):
    """The verify script with every address in `swap` pointed at a stub server (or at a closed port)."""
    _Stub.routes, _Stub.seen = routes, []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    port = server.server_port if answer else 1
    if answer:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        argv = _verify()
        script = argv[-1].replace("$$", "$")
        for old, new in swap:
            assert old in script, old
            script = script.replace(old, new.format(port=port))
        return subprocess.run([*argv[:-1], script], capture_output=True, text=True, timeout=60,
                              env=env), list(_Stub.seen)
    finally:
        server.server_close()

SWAP = [("http://127.0.0.1:8008", "http://127.0.0.1:{port}")]
def _env(**extra):
    import os
    return {**{k: v for k, v in os.environ.items() if k != "TURN_HOSTNAME"}, **extra}


ROUTES = {"/health": (200, b"OK"),
          "/_matrix/client/versions": (200, b'{"versions":["r0.6.1","v1.11"]}')}


def test_the_hook_asks_the_listener_the_route_label_names():
    synapse = _compose()["services"]["synapse"]
    assert "vps.route.port=8008" in synapse["labels"]
    assert "vps.health.path=/health" in synapse["labels"]


@needs_interpreter
def test_verify_passes_for_a_homeserver_that_lists_its_versions():
    run, seen = _run(ROUTES, swap=SWAP, env=_env())
    assert run.returncode == 0, run.stderr
    assert "newest Matrix version v1.11" in run.stdout
    assert "call relay" not in run.stdout
    assert seen[0]["X-Forwarded-For"] == "127.0.0.1"


@needs_interpreter
def test_verify_fails_without_a_health_answer_a_version_or_a_server():
    run, _ = _run({**ROUTES, "/health": (500, b"")}, swap=SWAP, env=_env())
    assert run.returncode == 1 and "Synapse does not answer /health" in run.stderr
    run, _ = _run({**ROUTES, "/_matrix/client/versions": (200, b'{"versions":[]}')}, swap=SWAP, env=_env())
    assert run.returncode == 1 and "lists no version" in run.stderr
    run, _ = _run({}, answer=False, swap=SWAP, env=_env())
    assert run.returncode == 1 and "does not answer" in run.stderr


@needs_interpreter
def test_verify_checks_the_call_relay_entries_only_when_a_relay_is_provided(tmp_path):
    conf = tmp_path / "homeserver.yaml"
    swap = SWAP + [("/data/homeserver.yaml", str(conf))]
    conf.write_text('turn_uris:\n  - "turn:turn.example.test:3478?transport=udp"\nturn_shared_secret: "s"\n')
    run, _ = _run(ROUTES, swap=swap, env=_env(TURN_HOSTNAME="turn.example.test"))
    assert run.returncode == 0, run.stderr
    assert "call relay turn.example.test wired" in run.stdout
    conf.write_text("turn_uris: []\n")
    run, _ = _run(ROUTES, swap=swap, env=_env(TURN_HOSTNAME="turn.example.test"))
    assert run.returncode == 1 and "does not name it" in run.stderr
    run, _ = _run(ROUTES, swap=swap, env=_env(TURN_HOSTNAME=""))
    assert run.returncode == 0, run.stderr
