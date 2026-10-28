"""Actual Budget: the verify hook run against a stub of the server it asks."""
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

SLUG = "actualbudget"
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
    assert hooks["service"] == "actual"
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

SWAP = [("port: 5006", "port: {port}")]
INFO = b'{"status":"ok","build":{"name":"@actual-app/sync-server","description":"x","version":"26.9.0"}}'


def _proc(tmp_path: Path, states: dict[str, str]) -> list[tuple[str, str]]:
    """A /proc of processes named node in the given states."""
    for pid, state in states.items():
        (tmp_path / pid).mkdir(exist_ok=True)
        (tmp_path / pid / "stat").write_text(f"{pid} (node) {state} 1 1 1 0 -1\n")
    return [("'/proc'", f"'{tmp_path}'"), ("'/proc/'", f"'{tmp_path}/'")]


def test_the_quiesce_stops_the_server_the_hook_asks():
    block = _entry().block
    assert block["quiesce"]["service"] == block["hooks"]["service"]
    assert block["quiesce"]["pre"] == [["pkill", "-STOP", "-x", "node"]]


@needs_interpreter
def test_verify_passes_when_the_server_names_its_build(tmp_path):
    run, _ = _run({"/info": (200, INFO)}, swap=SWAP + _proc(tmp_path, {"7": "S"}))
    assert run.returncode == 0, run.stderr
    assert "sync-server 26.9.0" in run.stdout


@needs_interpreter
def test_verify_fails_for_another_server_on_the_port(tmp_path):
    run, _ = _run({"/info": (200, b"{}")}, swap=SWAP + _proc(tmp_path, {"7": "S"}))
    assert run.returncode == 1 and "without its build name" in run.stderr
    run, _ = _run({}, swap=SWAP + _proc(tmp_path, {"7": "S"}))
    assert run.returncode == 1 and "answered 404" in run.stderr


@needs_interpreter
def test_verify_fails_when_nothing_listens(tmp_path):
    run, _ = _run({}, answer=False, swap=SWAP + _proc(tmp_path, {"7": "S"}))
    assert run.returncode == 1 and "does not answer on 5006" in run.stderr


@needs_interpreter
def test_verify_answers_ok_while_a_backup_holds_the_server_stopped(tmp_path):
    run, seen = _run({}, answer=False, swap=SWAP + _proc(tmp_path, {"7": "T"}))
    assert run.returncode == 0, run.stderr
    assert "paused for a backup or an update" in run.stdout
