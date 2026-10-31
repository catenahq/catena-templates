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
    assert "Date.now() + 130000" in _verify()[-1]
    assert 130 + 3 * 15 < _entry().block["hooks"]["timeout_seconds"] <= 300


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
    ("NGINX = 5000", "NGINX = {port}"),
    ("ORCHESTRATOR = 3002", "ORCHESTRATOR = {port}"),
    ("Date.now() + 130000", "Date.now() + 2000"),
    ("sleep(4000)", "sleep(100)"),
]
ROUTES = {"/api/": (200, b"App is running!"), "/": (307, b""), "/health/status": (200, b'{"status":"ok"}')}


def test_the_hook_asks_the_port_the_route_label_names():
    assert "vps.route.port=5000" in _compose()["services"]["postiz"]["labels"]
    assert "NGINX = 5000" in _verify()[-1]


def test_the_hook_asks_the_orchestrator_port_the_image_defaults_to():
    assert "ORCHESTRATOR_PORT" not in _compose()["services"]["postiz"]["environment"]
    assert "ORCHESTRATOR = 3002" in _verify()[-1]


@needs_interpreter
def test_verify_passes_for_a_backend_and_a_front_end_that_answer():
    run, _ = _run(ROUTES, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert "backend and front end answer" in run.stdout


@needs_interpreter
def test_verify_asks_again_while_nginx_has_no_process_behind_it():
    routes = {**ROUTES, "/api/": [(502, b""), (503, b""), (200, b"App is running!")], "/": [(502, b""), (307, b"")]}
    run, seen = _run(routes, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert len(seen) == 6
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
    assert run.returncode == 1 and "Postiz does not answer: " in run.stderr
    assert run.stderr.count("\n") == 1


@needs_interpreter
def test_verify_passes_only_when_the_orchestrator_reaches_temporal():
    run, seen = _run(ROUTES, swap=SWAP)
    assert run.returncode == 0 and "orchestrator reaches Temporal" in run.stdout
    assert len(seen) == 3


@needs_interpreter
def test_verify_asks_the_orchestrator_again_while_it_answers_500():
    routes = {**ROUTES, "/health/status": [(500, b""), (500, b""), (200, b'{"status":"ok"}')]}
    run, seen = _run(routes, swap=SWAP)
    assert run.returncode == 0, run.stderr
    assert len(seen) == 5


@needs_interpreter
def test_verify_fails_naming_the_orchestrator_once_the_wait_is_over():
    run, seen = _run({**ROUTES, "/health/status": (500, b"")}, swap=SWAP)
    assert run.returncode == 1 and "orchestrator answered 500" in run.stderr
    assert len(seen) > 3


@needs_interpreter
def test_verify_fails_at_once_when_the_orchestrator_has_no_health_route():
    run, seen = _run({"/api/": ROUTES["/api/"], "/": ROUTES["/"]}, swap=SWAP)
    assert run.returncode == 1 and "orchestrator answered 404" in run.stderr
    assert len(seen) == 3


def test_temporal_is_wired_to_postiz_and_to_the_db_service():
    services = _compose()["services"]
    postiz = services["postiz"]
    assert postiz["environment"]["TEMPORAL_ADDRESS"] == "temporal:7233"
    assert postiz["environment"]["RUN_CRON"] == "true"
    assert postiz["command"][-1].rstrip().endswith("nginx && pnpm run pm2")
    temporal, setup = services["temporal"], services["temporal-setup"]
    for service in (temporal, setup):
        assert service["networks"] == ["default"]
        assert "volumes" not in service and "depends_on" not in service
        assert service["deploy"]["restart_policy"]["condition"] == "any"
        assert "vps.auto-update=patch" in service["labels"]
    minor = lambda s: s["image"].rsplit(":", 1)[1].rsplit(".", 1)[0]
    assert minor(temporal) == minor(setup)
    assert temporal["environment"]["POSTGRES_SEEDS"] == "db"
    assert setup["environment"]["SQL_HOST"] == "db"
    assert temporal["environment"]["DYNAMIC_CONFIG_FILE_PATH"] == "/dev/null"
    assert temporal["environment"]["TEMPORAL_VERSION_CHECK_DISABLED"]
    images = _entry().block["images"]
    for name in ("temporal", "temporal-setup"):
        assert images[name]["rule"]["kind"] == "pattern"


STUB_TOOL = '#!/bin/sh\necho "$(basename "$0") $*" >> "$LOG"\n'
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not installed")


def _run_setup(tmp_path: Path, *, describe: int = 1, sql_tool: int = 0):
    """The temporal-setup script with stub executables first on PATH; returns the exit code and the calls made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name, code in (("temporal-sql-tool", sql_tool), ("temporal", 0)):
        tool = bin_dir / name
        body = STUB_TOOL
        if name == "temporal":
            body += f'case "$*" in *"namespace describe"*) exit {describe};; esac\n'
        else:
            body += f"exit {code}\n"
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    script = _compose()["services"]["temporal-setup"]["command"][-1]
    assert "sleep infinity" in script and "sleep 5" in script
    script = script.replace("sleep infinity", "true").replace("sleep 5", "sleep 0")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log)}
    run = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60, env=env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return run.returncode, calls


@needs_sh
def test_setup_runs_the_sql_steps_then_creates_the_missing_namespace(tmp_path):
    code, calls = _run_setup(tmp_path)
    assert code == 0
    assert [c.split()[2:4] for c in calls[:6]] == [
        ["temporal", "create"], ["temporal", "setup-schema"], ["temporal", "update-schema"],
        ["temporal_visibility", "create"], ["temporal_visibility", "setup-schema"],
        ["temporal_visibility", "update-schema"],
    ]
    assert all(c.startswith("temporal-sql-tool ") for c in calls[:6])
    assert [c.split()[:4] for c in calls[6:]] == [
        ["temporal", "operator", "cluster", "health"],
        ["temporal", "operator", "namespace", "describe"],
        ["temporal", "operator", "namespace", "create"],
    ]


@needs_sh
def test_setup_leaves_an_existing_namespace_as_it_is(tmp_path):
    code, calls = _run_setup(tmp_path, describe=0)
    assert code == 0
    assert not any("namespace create" in c for c in calls)


@needs_sh
def test_setup_exits_before_any_namespace_call_when_the_sql_tool_fails(tmp_path):
    code, calls = _run_setup(tmp_path, sql_tool=1)
    assert code != 0
    assert calls and not any(c.startswith("temporal ") for c in calls)
