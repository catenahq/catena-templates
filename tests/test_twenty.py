"""Twenty: the verify hook run against a stub of the server it asks."""
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

SLUG = "twenty"
INTERPRETER = "sh"
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
    assert hooks["service"] == "server"
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_wait_ends_inside_the_hook_timeout():
    script = _verify()[-1]
    assert "+ 150 ))" in script
    assert 150 + 10 < _entry().block["hooks"]["timeout_seconds"] <= 300


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
    ("http://127.0.0.1:3000/healthz", "http://127.0.0.1:{port}/healthz"),
    ("+ 150 ))", "+ 2 ))"),
    ("sleep 4", "sleep 0.1"),
]
OK = b'{"status":"ok","info":{},"error":{},"details":{}}'


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


def test_the_hook_asks_the_port_the_route_label_names():
    assert "vps.route.port=3000" in _compose()["services"]["server"]["labels"]


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_passes_for_a_server_that_reports_ok():
    run, _ = _run({"/healthz": (200, OK)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "answers /healthz" in run.stdout


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_asks_again_while_the_proxy_says_the_server_is_starting():
    run, seen = _run({"/healthz": [(502, b""), (503, b""), (200, OK)]}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert len(seen) == 3
    assert run.stdout.count("\n") == 1 and run.stderr == ""


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_fails_at_once_for_a_definite_wrong_answer():
    run, seen = _run({"/healthz": (200, b'{"status":"error"}')}, swap=SWAP)
    assert run.returncode == 1 and "not ok" in run.stderr
    assert len(seen) == 1
    run, seen = _run({"/healthz": (500, b"")}, swap=SWAP)
    assert run.returncode == 1 and "status 500" in run.stderr
    assert len(seen) == 1


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_fails_naming_the_last_answer_once_the_wait_is_over():
    run, seen = _run({"/healthz": (503, b"")}, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr and "status 503" in run.stderr
    assert len(seen) > 1
    run, _ = _run({}, answer=False, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr and "curl 7" in run.stderr
    assert run.stderr.count("\n") == 1
