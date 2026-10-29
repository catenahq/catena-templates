"""Mautic: the verify hook run against a stub of the server it asks."""
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

SLUG = "mautic"
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
    assert hooks["service"] == "mautic_web"
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

SWAP = [("http://127.0.0.1/s/login", "http://127.0.0.1:{port}/s/login")]
LOGIN = b"<html><title>Mautic</title><body>Log in</body></html>"
INSTALLER = b'Redirecting to <a href="/installer">/installer</a>.'


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


def test_the_hook_asks_the_service_the_route_label_names():
    web = _compose()["services"]["mautic_web"]
    assert "vps.route.port=80" in web["labels"]
    assert "curl" in web["healthcheck"]["test"][1]


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_passes_for_the_sign_in_page_and_for_a_site_waiting_for_its_installer():
    run, _ = _run({"/s/login": (200, LOGIN)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "Mautic answers on port 80" in run.stdout
    run, _ = _run({"/s/login": (302, INSTALLER)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "waits for its installer" in run.stdout


@needs_interpreter
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_verify_fails_for_another_page_a_foreign_redirect_a_server_error_and_no_answer():
    run, _ = _run({"/s/login": (200, b"<title>Welcome to nginx</title>")}, swap=SWAP)
    assert run.returncode == 1 and "200, but not with a Mautic page" in run.stderr
    run, _ = _run({"/s/login": (302, b"Redirecting to /elsewhere")}, swap=SWAP)
    assert run.returncode == 1 and "302, but not with a Mautic page" in run.stderr
    run, _ = _run({"/s/login": (500, LOGIN)}, swap=SWAP)
    assert run.returncode == 1 and "500, but not with a Mautic page" in run.stderr
    run, _ = _run({}, answer=False, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr
