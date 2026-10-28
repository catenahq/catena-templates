"""Kimai: the verify hook run against a stub of the server it asks."""
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

SLUG = "kimai"
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
    assert hooks["service"] == "kimai"
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

SWAP = [("http://127.0.0.1:8001/login", "http://127.0.0.1:{port}/login")]
PAGE = b"<html><title>Kimai</title><body>Sign in</body></html>"


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


def test_the_server_trusts_the_loopback_host_the_hook_uses():
    env = _compose()["services"]["kimai"]["environment"]
    assert "127.0.0.1" in env["TRUSTED_HOSTS"].split("|")


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_passes_for_the_sign_in_page():
    run, _ = _run({"/login": (200, PAGE)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "answers on port 8001" in run.stdout


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_fails_for_another_page_and_for_no_answer():
    run, _ = _run({"/login": (200, b"<title>other</title>")}, swap=SWAP)
    assert run.returncode == 1 and "not with the Kimai sign-in page" in run.stderr
    run, _ = _run({"/login": (500, PAGE)}, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr
