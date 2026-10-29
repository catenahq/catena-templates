"""Portainer: the verify hook runs in a busybox helper and asks Portainer's main page."""
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

SLUG = "portainer"
needs_wget = pytest.mark.skipif(shutil.which("wget") is None, reason="wget is not installed")


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == SLUG)


def _compose() -> dict:
    return yaml.safe_load((ROOT / "blueprints" / SLUG / "docker-compose.yml").read_text(encoding="utf-8"))


def _verify() -> list[str]:
    [argv] = _entry().block["hooks"]["verify"]
    assert argv[:2] == ["sh", "-c"]
    return argv


def test_the_hook_runs_in_a_helper_that_only_waits():
    hooks = _entry().block["hooks"]
    services = _compose()["services"]
    assert hooks["service"] == "probe"
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert hooks["timeout_seconds"] <= 60
    helper = services["probe"]
    assert helper["image"].startswith("busybox:") and "@sha256:" in helper["image"]
    assert helper["command"][0] == "sleep"
    assert not set(helper) & {"volumes", "ports", "labels", "environment"}
    assert helper["networks"] == ["default"]
    resources = helper["deploy"]["resources"]
    assert resources["limits"]["memory"] == resources["reservations"]["memory"] == "16M"
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_hook_asks_the_service_and_port_the_stack_serves():
    script = _verify()[-1]
    assert "http://portainer:9000/" in script
    portainer = _compose()["services"]["portainer"]
    assert "vps.route.port=9000" in portainer["labels"]
    assert "default" in portainer["networks"]


class _Stub(http.server.BaseHTTPRequestHandler):
    reply: tuple[int, bytes] = (200, b"")

    def do_GET(self):
        status, body = type(self).reply
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _run(reply: tuple[int, bytes] | None):
    """The verify script with the Portainer address pointed at a stub server (reply None: a closed port)."""
    _Stub.reply = reply or (200, b"")
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    port = server.server_port if reply else 1
    if reply:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        argv = _verify()
        script = argv[-1].replace("$$", "$")
        assert "http://portainer:9000/" in script
        script = script.replace("http://portainer:9000/", f"http://127.0.0.1:{port}/")
        return subprocess.run([*argv[:-1], script], capture_output=True, text=True, timeout=60)
    finally:
        server.server_close()


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


@needs_wget
def test_verify_passes_for_the_portainer_page():
    run = _run((200, b"<html><title>Portainer</title></html>"))
    assert run.returncode == 0, run.stderr
    assert "Portainer answers" in run.stdout


@needs_wget
def test_verify_fails_for_another_page_an_error_and_no_answer():
    run = _run((200, b"<html>hello</html>"))
    assert run.returncode == 1 and "not with the Portainer page" in run.stderr
    run = _run((502, b""))
    assert run.returncode == 1 and "does not answer" in run.stderr
    run = _run(None)
    assert run.returncode == 1 and "does not answer" in run.stderr
