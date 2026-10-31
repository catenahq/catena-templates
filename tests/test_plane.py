"""Plane: the verify hook run against a stub of the server it asks."""
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

SLUG = "plane"
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
    assert hooks["service"] == "api"
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_api_migrates_the_database_before_it_serves():
    script = _compose()["services"]["api"]["command"][-1]
    assert script.startswith("set -e")
    assert script.index("./bin/docker-entrypoint-migrator.sh") < script.index("exec ./bin/docker-entrypoint-api.sh")
    for service in ("worker", "beat-worker"):
        assert "migrat" not in str(_compose()["services"][service]["command"])


def test_the_wait_ends_inside_the_hook_timeout():
    assert "time.monotonic() + 150" in _verify()[-1]
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
    ("http://127.0.0.1:8000", "http://127.0.0.1:{port}"),
    ("time.monotonic() + 150", "time.monotonic() + 2"),
    ("time.sleep(4)", "time.sleep(0.1)"),
]
STORE = [('os.environ.get("AWS_S3_ENDPOINT_URL", "")', '"http://127.0.0.1:{port}"')]
INSTANCE = b'{"instance":{},"config":{},"is_activated":true}'
ROUTES = {"/api/instances/": (200, INSTANCE), "/minio/health/live": (200, b"")}


def _env(**extra):
    import os
    return {**{k: v for k, v in os.environ.items() if k not in ("AWS_S3_ENDPOINT_URL", "USE_MINIO")}, **extra}


def test_the_hook_asks_the_service_that_holds_the_object_store_settings():
    compose = _compose()["services"]
    assert compose["api"]["environment"]["AWS_S3_ENDPOINT_URL"] == "http://minio:9000"
    assert compose["api"]["environment"]["USE_MINIO"] == "1"
    assert "minio" in compose


@needs_interpreter
def test_verify_passes_and_asks_the_object_store_when_it_is_wired():
    run, seen = _run(ROUTES, swap=SWAP + STORE, env=_env(USE_MINIO="1", AWS_S3_ENDPOINT_URL="http://minio:9000"))
    assert run.returncode == 0, run.stderr
    assert "object store is alive" in run.stdout


@needs_interpreter
def test_verify_skips_the_object_store_without_its_settings():
    run, _ = _run({"/api/instances/": (200, INSTANCE)}, swap=SWAP, env=_env())
    assert run.returncode == 0, run.stderr
    assert "object store" not in run.stdout
    run, _ = _run({"/api/instances/": (200, INSTANCE)}, swap=SWAP + STORE, env=_env(USE_MINIO="0"))
    assert run.returncode == 0, run.stderr
    assert "object store" not in run.stdout


@needs_interpreter
def test_verify_asks_again_while_the_api_is_starting():
    routes = {**ROUTES, "/api/instances/": [(502, b""), (503, b""), (200, INSTANCE)]}
    run, seen = _run(routes, swap=SWAP, env=_env())
    assert run.returncode == 0, run.stderr
    assert len(seen) == 3
    assert run.stdout.count("\n") == 1 and run.stderr == ""


@needs_interpreter
def test_verify_fails_at_once_for_a_definite_wrong_answer():
    env = _env()
    run, seen = _run({"/api/instances/": (500, b"")}, swap=SWAP, env=env)
    assert run.returncode == 1 and "HTTP Error 500" in run.stderr
    assert len(seen) == 1
    run, seen = _run({"/api/instances/": (200, b'{"x":1}')}, swap=SWAP, env=env)
    assert run.returncode == 1 and "without its instance record" in run.stderr
    assert len(seen) == 1


@needs_interpreter
def test_verify_fails_for_a_dead_object_store_and_no_answer_once_the_wait_is_over():
    env = _env(USE_MINIO="1")
    run, seen = _run({**ROUTES, "/minio/health/live": (503, b"")}, swap=SWAP + STORE, env=env)
    assert run.returncode == 1 and "/minio/health/live" in run.stderr
    assert len(seen) > 2
    run, _ = _run({}, answer=False, swap=SWAP, env=env)
    assert run.returncode == 1 and "Plane does not answer" in run.stderr and "refused" in run.stderr
    assert run.stderr.count("\n") == 1
