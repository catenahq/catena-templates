"""Invoice Ninja: the verify hook run against a stub of the server it asks."""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

SLUG = "invoiceninja"
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
    assert hooks["service"] == "nginx"
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_wait_ends_inside_the_hook_timeout():
    assert "+ 150 ))" in _verify()[-1]
    assert 150 + 10 < _entry().block["hooks"]["timeout_seconds"] <= 300


# The nginx image's wget is busybox's: this shim answers like it, in the words of its error lines.
BUSYBOX_WGET = f"""#!{sys.executable}
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[-1], timeout=10) as response:
        sys.stdout.write(response.read().decode())
except urllib.error.HTTPError as e:
    sys.exit("wget: server returned error: HTTP/1.1 %d %s" % (e.code, e.reason))
except urllib.error.URLError as e:
    sys.exit("wget: can't connect to remote host (127.0.0.1): " + ("Connection refused" if isinstance(e.reason, ConnectionRefusedError) else str(e.reason)))
"""


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
        with tempfile.TemporaryDirectory() as shim:
            wget = Path(shim) / "wget"
            wget.write_text(BUSYBOX_WGET)
            wget.chmod(0o755)
            env = {**os.environ, "PATH": shim + os.pathsep + os.environ["PATH"]}
            return subprocess.run([*argv[:-1], script], capture_output=True, text=True, timeout=60,
                                  env=env), list(_Stub.seen)
    finally:
        server.server_close()

SWAP = [
    ("http://127.0.0.1/health", "http://127.0.0.1:{port}/health"),
    ("+ 150 ))", "+ 2 ))"),
    ("sleep 4", "sleep 0.1"),
]
HEALTHY = b'{"status":"ok","message":"API is healthy"}'


def test_the_script_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _verify()[-1].replace("$$", "$")], check=True)


def test_the_hook_asks_a_path_nginx_hands_to_php():
    nginx = _compose()["services"]["nginx"]
    script = "\n".join(nginx["command"])
    assert "location = /nginx-health" in script and "return 204" in script
    assert "/health" not in script.replace("/nginx-health", "")
    assert "try_files $$uri $$uri/ /index.php?$$query_string;" in script


@needs_interpreter
def test_verify_passes_for_the_application_health_reply():
    run, _ = _run({"/health": (200, HEALTHY)}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "answers /health" in run.stdout


@needs_interpreter
def test_verify_asks_again_while_nginx_has_no_application_behind_it():
    run, seen = _run({"/health": [(502, b""), (503, b""), (200, HEALTHY)]}, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert len(seen) == 3
    assert run.stdout.count("\n") == 1 and run.stderr == ""


@needs_interpreter
def test_verify_fails_at_once_for_another_reply_and_an_error_status():
    run, seen = _run({"/health": (200, b"ok")}, swap=SWAP)
    assert run.returncode == 1 and "not with the Invoice Ninja health reply" in run.stderr
    assert len(seen) == 1
    run, seen = _run({"/health": (500, b"")}, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr and "HTTP/1.1 500" in run.stderr
    assert len(seen) == 1


@needs_interpreter
def test_verify_fails_naming_the_last_answer_once_the_wait_is_over():
    run, seen = _run({"/health": (502, b"")}, swap=SWAP)
    assert run.returncode == 1 and "does not answer" in run.stderr and "HTTP/1.1 502" in run.stderr
    assert len(seen) > 1
    run, _ = _run({}, answer=False, swap=SWAP)
    assert run.returncode == 1 and "Connection refused" in run.stderr
    assert run.stderr.count("\n") == 1
