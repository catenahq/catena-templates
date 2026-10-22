"""ERPNext's own declarations: its verify hook run against stubs of what it
asks, the data declaration of its database, and the test.yml the CI harness
runs after a restore."""
from __future__ import annotations

import http.server
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

BLUEPRINT = ROOT / "blueprints" / "erpnext"
VENV_PYTHON = "/home/frappe/frappe-bench/env/bin/python"


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == "erpnext")


def _compose() -> dict:
    return yaml.safe_load((BLUEPRINT / "docker-compose.yml").read_text(encoding="utf-8"))


def _verify_body() -> str:
    [argv] = _entry().block["hooks"]["verify"]
    assert argv[:2] == ["bash", "-c"]
    return argv[2].replace("$$", "$")


def test_the_hook_runs_in_a_service_of_the_stack():
    hooks = _entry().block["hooks"]
    assert hooks["service"] in _compose()["services"]
    assert set(hooks) - {"service", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_the_verify_body_parses_in_bash():
    subprocess.run(["bash", "-n", "-c", _verify_body()], check=True)


class _Ping(http.server.BaseHTTPRequestHandler):
    answer = b'{"message":"pong"}'
    host_seen: list[str] = []

    def do_GET(self):
        type(self).host_seen.append(self.headers.get("Host", ""))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(type(self).answer)

    def log_message(self, *args):
        pass


def _verify(tmp_path: Path, *, process: str, answer: bytes, workers: int):
    """The verify body with the first process, the app server, the interpreter and
    the redis and rq modules replaced by stand-ins."""
    _Ping.answer = answer
    _Ping.host_seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Ping)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        (stubs / "redis.py").write_text(
            "class Redis:\n"
            "    @staticmethod\n"
            "    def from_url(url):\n"
            "        return url\n")
        (stubs / "rq.py").write_text(
            "class Worker:\n"
            "    @staticmethod\n"
            f"    def count(connection):\n        assert connection == 'redis://redis-queue:6379'\n        return {workers}\n")
        cmdline = tmp_path / "cmdline"
        cmdline.write_bytes(process.replace(" ", "\0").encode() + b"\0")
        python = tmp_path / "python"
        python.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        python.chmod(0o755)
        body = (_verify_body()
                .replace("/proc/1/cmdline", str(cmdline))
                .replace(VENV_PYTHON, str(python))
                .replace("http://backend:8000", f"http://127.0.0.1:{server.server_port}"))
        env = dict(os.environ, SITE_NAME="erp.example.test", REDIS_QUEUE="redis-queue:6379",
                   PYTHONPATH=str(stubs))
        run = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60)
        return run, list(_Ping.host_seen)
    finally:
        server.shutdown()


def test_verify_passes_for_a_site_that_answers_with_its_workers_registered(tmp_path):
    run, hosts = _verify(tmp_path, process="python bench schedule", answer=b'{"message":"pong"}', workers=3)
    assert run.returncode == 0, run.stderr
    assert "3 queue workers" in run.stdout
    assert hosts == ["erp.example.test"]


def test_verify_fails_when_the_container_does_not_run_the_scheduler(tmp_path):
    run, _ = _verify(tmp_path, process="sleep infinity", answer=b'{"message":"pong"}', workers=3)
    assert run.returncode == 1
    assert "scheduler" in run.stderr


def test_verify_fails_when_the_site_does_not_answer_pong(tmp_path):
    run, _ = _verify(tmp_path, process="bench schedule", answer=b"maintenance", workers=3)
    assert run.returncode != 0
    assert "did not answer pong" in run.stderr


def test_verify_fails_when_a_queue_worker_is_missing(tmp_path):
    run, _ = _verify(tmp_path, process="bench schedule", answer=b'{"message":"pong"}', workers=2)
    assert run.returncode != 0
    assert "only 2 of 3 queue workers" in run.stderr


def test_one_worker_registers_per_queue_service():
    queues = [name for name in _compose()["services"] if name.startswith("queue-")]
    assert len(queues) == 3


def test_the_database_declares_a_dump_restore_upgrade_of_its_volume():
    assert _entry().block["data"] == {"db": {"volume": "db-data", "upgrade": "dump_restore"}}
    db = _compose()["services"]["db"]
    assert "db-data:/var/lib/mysql" in db["volumes"]
    assert quiesce_lint.database_image(db["image"])


def test_migrate_runs_in_the_backend_after_the_site_is_up():
    lifecycle = _entry().lifecycle
    assert lifecycle["service"] == "backend"
    assert lifecycle["migrate"] == [["bench", "--site", "all", "migrate"]]
    assert lifecycle["ready"]


# test.yml: the seed and the checks the harness runs.

def _test_yml() -> dict:
    return yaml.safe_load((BLUEPRINT / "test.yml").read_text(encoding="utf-8"))


def test_the_test_file_names_the_template_and_holds_both_phases():
    doc = _test_yml()
    assert doc["id"] == "erpnext"
    assert doc["description"].strip()
    assert doc["seed"] and doc["assert"]


def test_every_step_is_a_docker_exec_into_a_service_of_the_stack():
    services = {name.replace("-", "_") for name in _compose()["services"]}
    doc = _test_yml()
    for phase in ("seed", "assert"):
        names = [step["name"] for step in doc[phase]]
        assert len(names) == len(set(names)), phase
        for step in doc[phase]:
            assert step["type"] == "docker_exec"
            assert isinstance(step["command"], list) and step["command"]
            container = re.fullmatch(r"\{\{ container_(\w+) \}\}", step["container"])
            assert container and container.group(1) in services, step["name"]


def test_each_seeded_marker_has_a_check_that_reads_it_back():
    doc = _test_yml()
    seeded = "\n".join(" ".join(step["command"]) for step in doc["seed"])
    for step in doc["assert"]:
        assert "{{ run_id }}" in step["expect_stdout_contains"]
    assert "r25-marker.txt" in seeded and "catena_canary" in seeded
    checks = "\n".join(" ".join(step["command"]) for step in doc["assert"])
    assert "r25-marker.txt" in checks and "catena_canary" in checks
