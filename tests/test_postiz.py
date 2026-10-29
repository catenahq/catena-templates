"""Postiz: the verify hook run against a stub of the server it asks."""
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

SLUG = "postiz"
INTERPRETER = "node"
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
    assert hooks["service"] == "postiz"
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_wait_ends_inside_the_hook_timeout():
    assert "Date.now() + 150000" in _verify()[-1]
    assert 150 + 15 < _entry().block["hooks"]["timeout_seconds"] <= 300


class _Stub(http.server.BaseHTTPRequestHandler):
    """A route holds one answer, or a list of answers given in turn (the last one repeats)."""
    routes: dict[str, tuple[int, bytes] | list[tuple[int, bytes]]] = {}
    seen: list[dict[str, str]] = []

    def do_GET(self):
        type(self).seen.append(dict(self.headers))
        answer = type(self).routes.get(self.path, (404, b""))
        if isinstance(answer, list):
            status, body = answer.pop(0) if len(answer) > 1 else answer[0]
        else:
            status, body = answer
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

SWAP = [
    ("port: 5000", "port: {port}"),
    ("Date.now() + 150000", "Date.now() + 2000"),
    ("sleep(4000)", "sleep(100)"),
]
ROUTES = {"/api/": (200, b"App is running!"), "/": (307, b"")}


def test_the_hook_asks_the_port_the_route_label_names():
    assert "vps.route.port=5000" in _compose()["services"]["postiz"]["labels"]


@needs_interpreter
def test_verify_passes_for_a_backend_and_a_front_end_that_answer():
    run, _ = _run(ROUTES, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "backend and front end answer" in run.stdout


@needs_interpreter
def test_verify_asks_again_while_nginx_has_no_process_behind_it():
    routes = {"/api/": [(502, b""), (503, b""), (200, b"App is running!")], "/": [(502, b""), (307, b"")]}
    run, seen = _run(routes, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert len(seen) == 5
    assert run.stdout.count("\n") == 1 and run.stderr == ""


@needs_interpreter
def test_verify_fails_at_once_for_another_backend_and_an_error_status():
    run, seen = _run({**ROUTES, "/api/": (200, b"<html>nginx</html>")}, swap=SWAP)
    assert run.returncode == 1 and "backend answered 200" in run.stderr
    assert len(seen) == 1
    run, seen = _run({**ROUTES, "/": (500, b"")}, swap=SWAP)
    assert run.returncode == 1 and "front end answered 500" in run.stderr
    assert len(seen) == 2


@needs_interpreter
def test_verify_fails_naming_the_last_answer_once_the_wait_is_over():
    run, seen = _run({**ROUTES, "/api/": (502, b"")}, swap=SWAP)
    assert run.returncode == 1 and "backend answered 502" in run.stderr
    assert len(seen) > 2
    run, seen = _run({**ROUTES, "/": (502, b"")}, swap=SWAP)
    assert run.returncode == 1 and "front end answered 502" in run.stderr
    run, _ = _run({}, answer=False, swap=SWAP)
    assert run.returncode == 1 and "does not answer on 5000" in run.stderr
    assert run.stderr.count("\n") == 1
