"""Umami: the declaration, the admin password script and the verify hook run against a stub of the server they ask."""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

SLUG = "umami"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == SLUG)


def _services() -> dict:
    compose = yaml.safe_load((ROOT / "blueprints" / SLUG / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]


def _labels(service: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in _services()[service]["labels"])


def _verify() -> list[str]:
    [argv] = _entry().block["hooks"]["verify"]
    assert argv[0] == "node"
    return argv


# --- the declaration ----------------------------------------------------------

def test_the_address_is_gated_for_administrators_with_the_script_and_collect_paths_public():
    labels = _labels("umami")
    assert labels["vps.auth.mode"] == "admin-only"
    assert labels["vps.auth.public_paths"] == "/${TRACKER_SCRIPT_NAME},${COLLECT_API_ENDPOINT}"
    assert labels["vps.route.host"] == "${DOMAIN_HOST}"
    assert labels["vps.route.port"] == "3000"
    assert labels["vps.health.path"] == "/api/heartbeat"


def test_the_default_address_is_not_one_a_blocker_list_names_and_the_paths_are_neutral():
    env = _entry().block["env"]
    assert env["DOMAIN_HOST"]["setting"]["default"] == "visits.{{ cloudflare_zone }}"
    assert env["TRACKER_SCRIPT_NAME"]["setting"]["default"] == "app.js"
    assert env["COLLECT_API_ENDPOINT"]["setting"]["default"] == "/api/data"


def test_the_admin_password_is_set_once_and_the_secrets_are_generated():
    env = _entry().block["env"]
    admin = env["UMAMI_ADMIN_PASSWORD"]["generated"]
    assert admin["first_start_only"] is True and admin["length"] == 32 and admin["alphabet"] == "alnum"
    assert env["APP_SECRET"]["generated"] == {"length": 64, "alphabet": "hex"}
    assert env["TWO_FACTOR_ENCRYPTION_KEY"]["generated"] == {"length": 64, "alphabet": "hex"}


def test_the_server_makes_no_call_of_its_own():
    environment = _services()["umami"]["environment"]
    assert (environment["PRIVATE_MODE"], environment["DISABLE_TELEMETRY"], environment["DISABLE_UPDATES"]) == ("1", "1", "1")
    assert "egress" not in _entry().block


def test_umami_logs_in_as_its_own_role_and_keeps_no_volume_of_its_own():
    umami = _services()["umami"]
    assert umami["environment"]["DATABASE_URL"] == "postgresql://umami:${DB_PASSWORD}@db:5432/umami"
    assert "volumes" not in umami
    assert _services()["db"]["environment"]["POSTGRES_USER"] == "umami"


def test_the_lifecycle_migrates_in_the_umami_service():
    block = _entry().block
    assert block["lifecycle"]["service"] == "umami"
    assert block["lifecycle"]["migrate"] == [["node", "node_modules/prisma/build/index.js", "migrate", "deploy"]]
    assert block["images"]["db"]["rule"] == {"kind": "partial_line"}


def test_the_hook_runs_in_the_umami_service():
    hooks = _entry().block["hooks"]
    assert hooks["service"] == "umami"
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"verify"}
    assert hooks["timeout_seconds"] <= 60
    assert quiesce_lint.lint_hooks(_entry()) == []


# --- the admin password script ------------------------------------------------

class _Umami(http.server.BaseHTTPRequestHandler):
    """Umami's routes the admin script calls, with the default account."""

    default_works = True
    # Statuses the first login attempts answer with before the real answer.
    login_first: list[int] = []
    # Statuses the first heartbeats answer with before the real answer.
    heartbeat_first: list[int] = []
    calls: list[tuple[str, dict, str]] = []

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.calls.append((self.path, {}, ""))
        if self.heartbeat_first:
            self._send(self.heartbeat_first.pop(0), {})
        else:
            self._send(200, {"ok": True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.calls.append((self.path, body, self.headers.get("authorization", "")))
        if self.path == "/api/auth/login" and self.login_first:
            self._send(self.login_first.pop(0), {})
        elif self.path == "/api/auth/login":
            ok = self.default_works and body == {"username": "admin", "password": "umami"}
            self._send(200 if ok else 401, {"token": "t"} if ok else {})
        else:
            self._send(200, {})


def _admin_script(port: int) -> str:
    script = _services()["umami"]["environment"]["UMAMI_ADMIN_JS"]
    assert "const base = 3000;" in script
    return script.replace("127.0.0.1:3000", f"127.0.0.1:{port}").replace("const base = 3000;", "const base = 10;")


def _run_admin_script(default_works: bool, login_first: tuple[int, ...] = (),
                      heartbeat_first: tuple[int, ...] = ()) -> list[tuple[str, dict, str]]:
    handler = type("Handler", (_Umami,), {"default_works": default_works, "calls": [],
                                          "login_first": list(login_first),
                                          "heartbeat_first": list(heartbeat_first)})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run = subprocess.run(["node", "-e", _admin_script(server.server_port)], capture_output=True, text=True,
                             timeout=30, check=False, env={"PATH": os.environ["PATH"], "UMAMI_ADMIN_PASSWORD": "N3wPassw0rd"})
        assert run.returncode == 0, run.stderr
    finally:
        server.shutdown()
    return [call for call in handler.calls if call[0] != "/api/heartbeat"]


@needs_node
def test_the_script_replaces_the_default_password_with_the_generated_one():
    calls = _run_admin_script(default_works=True)
    assert [c[0] for c in calls] == ["/api/auth/login", "/api/me/password"]
    assert calls[1][1] == {"currentPassword": "umami", "newPassword": "N3wPassw0rd"}
    assert calls[1][2] == "Bearer t"


@needs_node
def test_the_script_never_touches_a_password_someone_set():
    calls = _run_admin_script(default_works=False)
    assert [c[0] for c in calls] == ["/api/auth/login"]


@needs_node
def test_the_script_retries_every_answer_but_401_until_it_signs_in():
    calls = _run_admin_script(default_works=True, login_first=(500, 429))
    assert [c[0] for c in calls] == ["/api/auth/login"] * 3 + ["/api/me/password"]
    assert calls[3][1]["newPassword"] == "N3wPassw0rd"


@needs_node
def test_the_script_polls_the_heartbeat_at_the_base_wait_until_the_server_answers():
    started = time.monotonic()
    calls = _run_admin_script(default_works=True, heartbeat_first=(503,) * 8)
    assert [c[0] for c in calls] == ["/api/auth/login", "/api/me/password"]
    assert time.monotonic() - started < 1.5


@needs_node
def test_the_script_stops_on_401_after_a_failed_answer():
    calls = _run_admin_script(default_works=False, login_first=(503,))
    assert [c[0] for c in calls] == ["/api/auth/login"] * 2


def test_the_script_keeps_the_password_out_of_its_logs_and_arguments():
    script = _services()["umami"]["environment"]["UMAMI_ADMIN_JS"]
    assert "console.log" in script and not re.search(r"log\([^)]*env\.UMAMI_ADMIN_PASSWORD", script)
    assert "UMAMI_ADMIN_PASSWORD" not in " ".join(_services()["umami"]["command"])


def test_the_command_starts_the_script_beside_the_server():
    assert _services()["umami"]["command"] == [
        "sh", "-c", "node -e \"$$UMAMI_ADMIN_JS\" & exec sh scripts/start-docker.sh"]


# --- the verify hook ----------------------------------------------------------

class _Server(http.server.BaseHTTPRequestHandler):
    heartbeat: tuple[int, bytes] = (200, b'{"ok":true}')
    tracker: tuple[int, bytes] = (200, b"fetch('/api/data')")
    login_status = 401

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/heartbeat":
            self._send(*type(self).heartbeat)
        elif self.path == "/app.js":
            self._send(*type(self).tracker)
        else:
            self._send(404, b"")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self._send(type(self).login_status, b"{}")


def _run(*, answer: bool = True, heartbeat=(200, b'{"ok":true}'), tracker=(200, b"fetch('/api/data')"),
         login_status=401, script_name="app.js"):
    handler = type("Handler", (_Server,), {"heartbeat": heartbeat, "tracker": tracker, "login_status": login_status})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_port if answer else 1
    if answer:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        argv = _verify()
        script = argv[-1].replace("$$", "$")
        assert "127.0.0.1:3000" in script
        script = script.replace("127.0.0.1:3000", f"127.0.0.1:{port}")
        env = {"PATH": os.environ["PATH"], "TRACKER_SCRIPT_NAME": script_name, "COLLECT_API_ENDPOINT": "/api/data"}
        return subprocess.run([*argv[:-1], script], capture_output=True, text=True, timeout=60, env=env)
    finally:
        if answer:
            server.shutdown()
        server.server_close()


@needs_node
def test_verify_passes_for_a_server_that_serves_its_script_and_refuses_the_default_password():
    run = _run()
    assert run.returncode == 0, run.stderr
    assert run.stdout.count("\n") == 1 and "refuses the default password" in run.stdout


@needs_node
def test_verify_fails_when_the_default_password_signs_in():
    run = _run(login_status=200)
    assert run.returncode == 1 and "default password answered 200" in run.stderr


@needs_node
def test_verify_fails_when_the_database_does_not_answer_the_sign_in():
    run = _run(login_status=500)
    assert run.returncode == 1 and "answered 500" in run.stderr


@needs_node
def test_verify_fails_for_a_heartbeat_without_ok():
    run = _run(heartbeat=(200, b'{"ok":false}'))
    assert run.returncode == 1 and "without ok" in run.stderr


@needs_node
def test_verify_fails_for_a_script_without_the_collect_path_and_for_a_missing_script():
    run = _run(tracker=(200, b"fetch('/api/send')"))
    assert run.returncode == 1 and "without the collect path /api/data" in run.stderr
    run = _run(tracker=(404, b""))
    assert run.returncode == 1 and "app.js answered 404" in run.stderr


@needs_node
def test_verify_fails_when_the_server_does_not_answer():
    run = _run(answer=False)
    assert run.returncode == 1 and "does not answer on 3000" in run.stderr


@needs_node
@pytest.mark.parametrize("name", ["/app.js", "app.js,other.js", ""])
def test_verify_refuses_a_script_name_the_sign_in_gate_would_not_leave_public(name):
    run = _run(script_name=name)
    assert run.returncode == 1 and "TRACKER_SCRIPT_NAME must be one file name" in run.stderr
