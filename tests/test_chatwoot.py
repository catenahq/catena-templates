"""Chatwoot: the verify hook run against a stub of the server it asks."""
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

SLUG = "chatwoot"
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
    assert hooks["service"] == "rails"
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

SWAP = [('"127.0.0.1", 3000', '"127.0.0.1", {port}')]
OK =b'{"version":"4.17.1","timestamp":"x","queue_services":"ok","data_services":"ok"}'


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


def test_the_hook_asks_the_port_the_server_listens_on_and_sends_the_proto_header():
    rails = _compose()["services"]["rails"]
    assert "3000" in rails["command"]
    assert "vps.route.port=3000" in rails["labels"]
    assert "X-Forwarded-Proto" in _verify()[-1]
    assert rails["environment"]["FORCE_SSL"] == "true"


needs_ruby = pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")


@needs_ruby
def test_verify_passes_when_rails_reports_redis_and_postgres_ok():
    run, seen = _run({"/api": (200, OK)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "Chatwoot 4.17.1 answers" in run.stdout
    assert seen[0]["X-Forwarded-Proto"] == "https"


@needs_ruby
def test_verify_fails_for_a_failing_backend_a_redirect_or_no_answer():
    run, _ = _run({"/api": (200, OK.replace(b'"data_services":"ok"', b'"data_services":"failing"'))}, swap=SWAP)
    assert run.returncode == 1 and "Postgres failing" in run.stderr
    run, _ = _run({"/api": (301, b"")}, swap=SWAP)
    assert run.returncode == 1 and "answered 301" in run.stderr
    run, _ = _run({}, answer=False, swap=SWAP)
    assert run.returncode == 1 and "does not answer on 3000" in run.stderr
