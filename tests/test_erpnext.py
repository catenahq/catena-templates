"""The declarations of the two ERPNext templates, `erpnext` (Frappe's image) and
`erpnext-helpdesk` (Catena's build): the verify hook run against stubs of what
it asks, the data declaration of the database, the test.yml the CI harness runs
after a restore, and the places where the two differ."""
from __future__ import annotations

import copy
import http.server
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

SLUGS = ("erpnext", "erpnext-helpdesk")
VENV_PYTHON = "/home/frappe/frappe-bench/env/bin/python"
FRAPPE_SERVICES = ("backend", "frontend", "websocket", "queue-default", "queue-short",
                   "queue-long", "scheduler")

each_app = pytest.mark.parametrize("slug", SLUGS)


def _entry(slug: str) -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == slug)


def _compose(slug: str) -> dict:
    return yaml.safe_load((ROOT / "blueprints" / slug / "docker-compose.yml").read_text(encoding="utf-8"))


def _verify_body(slug: str) -> str:
    [argv] = _entry(slug).block["hooks"]["verify"]
    assert argv[:2] == ["bash", "-c"]
    return argv[2].replace("$$", "$")


def _backend_script(slug: str) -> str:
    shell, flag, script = _compose(slug)["services"]["backend"]["command"]
    assert (shell, flag) == ("bash", "-c")
    return script


@each_app
def test_the_hook_runs_in_a_service_of_the_stack(slug):
    hooks = _entry(slug).block["hooks"]
    assert hooks["service"] in _compose(slug)["services"]
    assert set(hooks) - {"service", "timeout_seconds"} == {"verify"}
    assert quiesce_lint.lint_hooks(_entry(slug)) == []


@each_app
def test_the_verify_body_parses_in_bash(slug):
    subprocess.run(["bash", "-n", "-c", _verify_body(slug)], check=True)


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


def _verify(slug: str, tmp_path: Path, *, process: str, answer: bytes, workers: int):
    """The verify body run against stand-ins for the first process, the app
    server, the interpreter and the redis and rq modules."""
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
        body = (_verify_body(slug)
                .replace("/proc/1/cmdline", str(cmdline))
                .replace(VENV_PYTHON, str(python))
                .replace("http://backend:8000", f"http://127.0.0.1:{server.server_port}"))
        env = dict(os.environ, SITE_NAME="erp.example.test", REDIS_QUEUE="redis-queue:6379",
                   PYTHONPATH=str(stubs))
        run = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60)
        return run, list(_Ping.host_seen)
    finally:
        server.shutdown()


@each_app
def test_verify_passes_for_a_site_that_answers_with_its_workers_registered(slug, tmp_path):
    run, hosts = _verify(slug, tmp_path, process="python bench schedule", answer=b'{"message":"pong"}', workers=3)
    assert run.returncode == 0, run.stderr
    assert "3 queue workers" in run.stdout
    assert hosts == ["erp.example.test"]


@each_app
def test_verify_fails_when_the_container_does_not_run_the_scheduler(slug, tmp_path):
    run, _ = _verify(slug, tmp_path, process="sleep infinity", answer=b'{"message":"pong"}', workers=3)
    assert run.returncode == 1
    assert "scheduler" in run.stderr


@each_app
def test_verify_fails_when_the_site_does_not_answer_pong(slug, tmp_path):
    run, _ = _verify(slug, tmp_path, process="bench schedule", answer=b"maintenance", workers=3)
    assert run.returncode != 0
    assert "did not answer pong" in run.stderr


@each_app
def test_verify_fails_when_a_queue_worker_is_missing(slug, tmp_path):
    run, _ = _verify(slug, tmp_path, process="bench schedule", answer=b'{"message":"pong"}', workers=2)
    assert run.returncode != 0
    assert "only 2 of 3 queue workers" in run.stderr


@each_app
def test_one_worker_registers_per_queue_service(slug):
    queues = [name for name in _compose(slug)["services"] if name.startswith("queue-")]
    assert len(queues) == 3


@each_app
def test_the_database_declares_a_dump_restore_upgrade_of_its_volume(slug):
    assert _entry(slug).block["data"] == {"db": {"volume": "db-data", "upgrade": "dump_restore"}}
    db = _compose(slug)["services"]["db"]
    assert "db-data:/var/lib/mysql" in db["volumes"]
    assert quiesce_lint.database_image(db["image"])


@each_app
def test_migrate_runs_in_the_backend_after_the_site_is_up(slug):
    lifecycle = _entry(slug).lifecycle
    assert lifecycle["service"] == "backend"
    assert lifecycle["migrate"] == [["bench", "--site", "all", "migrate"]]
    assert lifecycle["ready"]


# The two apps.

@each_app
def test_the_app_answers_on_the_address_its_source_names(slug):
    default = _entry(slug).block["env"]["DOMAIN_HOST"]["setting"]["default"]
    assert default == _entry(slug).catena["domain"]["host"]
    assert _entry(slug).block["env"]["ERPNEXT_HOSTNAME"]["setting"]["default"] == default


def test_the_two_apps_default_to_different_addresses():
    hosts = {slug: _entry(slug).catena["domain"]["host"] for slug in SLUGS}
    assert hosts == {"erpnext": "erp.{{ cloudflare_zone }}",
                     "erpnext-helpdesk": "desk.{{ cloudflare_zone }}"}


@each_app
def test_the_app_is_named_after_its_id(slug):
    entry = _entry(slug)
    assert entry.catena["app_name"] == f"catena-{slug}"
    assert entry.raw["name"] == f"catena-{slug}"


def test_the_plain_app_runs_frappes_own_image_and_installs_erpnext_alone():
    compose = _compose("erpnext")
    for name in FRAPPE_SERVICES:
        assert re.fullmatch(r"frappe/erpnext:v16\.\d+\.\d+", compose["services"][name]["image"]), name
    assert "images" not in _entry("erpnext").block
    assert "FRAPPE_APPS" not in _entry("erpnext").block["env"]
    assert "FRAPPE_APPS" not in compose["services"]["backend"]["environment"]
    script = _backend_script("erpnext")
    assert 'install-app erpnext\n' in script
    assert "FRAPPE_APPS" not in script


def test_the_helpdesk_app_runs_catenas_build_under_its_tag_rule():
    compose = _compose("erpnext-helpdesk")
    images = _entry("erpnext-helpdesk").block["images"]
    assert set(images) == set(FRAPPE_SERVICES)
    for name in FRAPPE_SERVICES:
        rule = images[name]["rule"]
        assert rule["kind"] == "build_suffix"
        reference = f"{rule['registry']}/{rule['repo']}"
        assert re.fullmatch(re.escape(reference) + r":v16\.\d+\.\d+-\d+", compose["services"][name]["image"]), name
    assert _entry("erpnext-helpdesk").block["env"]["FRAPPE_APPS"]["setting"]["default"] == "erpnext,telephony,helpdesk"
    assert "$${FRAPPE_APPS//,/ }" in _backend_script("erpnext-helpdesk")


def _without_the_differences(slug: str) -> dict:
    """The compose with what the two apps differ in set aside: the Frappe
    images and their tag rules, FRAPPE_APPS and the install-app line, and the
    address defaults."""
    compose = copy.deepcopy(_compose(slug))
    block = compose["x-catena"]
    block.pop("images", None)
    block["env"].pop("FRAPPE_APPS", None)
    for key in ("DOMAIN_HOST", "ERPNEXT_HOSTNAME"):
        block["env"][key] = "address"
    for name in FRAPPE_SERVICES:
        compose["services"][name]["image"] = "frappe-image"
    backend = compose["services"]["backend"]
    backend["environment"].pop("FRAPPE_APPS", None)
    shell, flag, script = backend["command"]
    backend["command"] = [shell, flag, re.sub(r".*install-app.*", "install-app", script)]
    return compose


def test_the_two_composes_differ_only_where_the_apps_differ():
    assert _without_the_differences("erpnext") == _without_the_differences("erpnext-helpdesk")


# test.yml: the seed and the checks the harness runs.

def _test_yml(slug: str) -> dict:
    return yaml.safe_load((ROOT / "blueprints" / slug / "test.yml").read_text(encoding="utf-8"))


@each_app
def test_the_test_file_names_the_template_and_holds_both_phases(slug):
    doc = _test_yml(slug)
    assert doc["id"] == slug
    assert doc["description"].strip()
    assert doc["seed"] and doc["assert"]


@each_app
def test_every_step_is_a_docker_exec_into_a_service_of_the_stack(slug):
    services = {name.replace("-", "_") for name in _compose(slug)["services"]}
    doc = _test_yml(slug)
    for phase in ("seed", "assert"):
        names = [step["name"] for step in doc[phase]]
        assert len(names) == len(set(names)), phase
        for step in doc[phase]:
            assert step["type"] == "docker_exec"
            assert isinstance(step["command"], list) and step["command"]
            container = re.fullmatch(r"\{\{ container_(\w+) \}\}", step["container"])
            assert container and container.group(1) in services, step["name"]


@each_app
def test_each_seeded_marker_has_a_check_that_reads_it_back(slug):
    doc = _test_yml(slug)
    seeded = "\n".join(" ".join(step["command"]) for step in doc["seed"])
    for step in doc["assert"]:
        assert "{{ run_id }}" in step["expect_stdout_contains"]
    assert "r25-marker.txt" in seeded and "catena_canary" in seeded
    checks = "\n".join(" ".join(step["command"]) for step in doc["assert"])
    assert "r25-marker.txt" in checks and "catena_canary" in checks


def test_the_two_test_files_differ_only_in_their_id():
    plain, helpdesk = _test_yml("erpnext"), _test_yml("erpnext-helpdesk")
    assert plain.pop("id") == "erpnext"
    assert helpdesk.pop("id") == "erpnext-helpdesk"
    assert plain == helpdesk
