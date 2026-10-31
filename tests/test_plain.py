"""lib/plain.py: the plain Docker export of an app and its start check."""
from __future__ import annotations

import http.server
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import plain  # noqa: E402

CATALOG = ROOT / "blueprints"
APPS = sorted(p.parent.name for p in CATALOG.glob("*/docker-compose.yml"))
REFUSED = {"coturn": "own network", "mailserver": "certificate"}

SYNTHETIC = """\
x-catena:
  schema: 2
  env:
    DOMAIN_HOST:
      managed: {expr: "web.{{ cloudflare_zone }}"}
    SECRET_A:
      generated: {length: 40, alphabet: alnum}
    SECRET_B:
      generated: {length: 24, alphabet: hex}
    GREETING:
      setting: {default: "hello world"}
  lifecycle:
    service: web
    ready: [web, --ready]
    timeout_seconds: 90
    user: app
    migrate:
      - [web, migrate]
      - - sh
        - -c
        - |
          echo "$$HOME"
  hooks:
    service: web
    user: app
    users:
      - [web, users]
    scan:
      - [web, scan]

services:
  web:
    image: web:1.0
    labels:
      - "vps.route.host=${DOMAIN_HOST}"
      - "vps.route.port=8080"
    ports:
      - "9999:9999"
      - "10010:10010/udp"
      - "0.0.0.0:7000:7000"
      - "[::]:7001:7001"
      - "5000"
      - "${PORT:-6000}:6000"
      - {target: 53, published: 5353, protocol: udp}
    deploy:
      placement:
        constraints:
          - node.labels.catena.role==data
    networks:
      catena-network: {}
      default: {}
  db:
    image: db:1.0
    networks:
      - catena-network

networks:
  catena-network:
    external: true
"""


def _tree(tmp_path, text, app="web"):
    folder = tmp_path / "tree" / "blueprints" / app
    folder.mkdir(parents=True)
    (folder / plain.COMPOSE).write_text(text)
    return tmp_path / "tree"


def _export(tmp_path, app, out="out"):
    export = plain.export_app(app, tmp_path / out)
    return export, tmp_path / out


def _env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


# the catalog

def test_actualbudget_export(tmp_path):
    export, out = _export(tmp_path, "actualbudget")
    doc = yaml.safe_load((out / plain.COMPOSE).read_text())
    assert not [key for key in doc if key.startswith("x-")]
    assert "catena-network" not in (out / plain.COMPOSE).read_text()
    assert "placement" not in (out / plain.COMPOSE).read_text()
    assert "networks" not in doc["services"]["actual"] or "catena-network" not in doc["services"]["actual"]["networks"]
    assert doc["services"]["actual"]["ports"] == ["127.0.0.1:5006:5006"]
    assert (export.service, export.port) == ("actual", 5006)
    env = (out / ".env").read_text()
    assert env.splitlines()[-1] == "DOMAIN_HOST="
    assert env.splitlines()[-2].startswith("# DOMAIN_HOST:")
    assert stat.S_IMODE((out / ".env").stat().st_mode) == 0o600
    assert export.to_fill == ["DOMAIN_HOST"]


def test_actualbudget_verify_script(tmp_path):
    _, out = _export(tmp_path, "actualbudget")
    script = (out / "verify.sh").read_text()
    assert "-e DOMAIN_HOST" in script
    assert 'node -e "$(cat hooks/verify.js)"' in script
    assert ". ./.env" in script
    assert "docker compose ps -q actual" in script
    assert (out / "hooks" / "verify.js").read_text().startswith("const fs = require('fs');")
    assert os.access(out / "verify.sh", os.X_OK)


def test_clamav_scan_script_passes_input_and_no_value(tmp_path):
    export, out = _export(tmp_path, "clamav")
    script = (out / "scan.sh").read_text()
    assert "-i " in script and '< "$input"' in script
    assert "-e " not in script and ".env" not in script
    assert export.port is None
    assert "ports" not in yaml.safe_load((out / plain.COMPOSE).read_text())["services"]["clamav"]
    assert "sh -c clamdcheck.sh" in (out / "verify.sh").read_text()


@pytest.mark.parametrize("app", APPS)
def test_an_app_exports_or_is_refused(tmp_path, app):
    if app in REFUSED:
        with pytest.raises(plain.PlainError, match=REFUSED[app]):
            plain.export_app(app, tmp_path / "out")
        assert not (tmp_path / "out").exists()
        return
    _, out = _export(tmp_path, app)
    doc = yaml.safe_load((out / plain.COMPOSE).read_text())
    assert not [key for key in doc if key.startswith("x-")]
    assert "catena-network" not in (out / plain.COMPOSE).read_text()
    for script in out.glob("*.sh"):
        assert script.read_text().startswith("#!/bin/sh\n")
        assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0


# the synthetic app

def test_generated_keys_have_their_length_and_alphabet(tmp_path):
    tree = _tree(tmp_path, SYNTHETIC)
    plain.export_app("web", tmp_path / "out", tree)
    env = _env(tmp_path / "out" / ".env")
    assert len(env["SECRET_A"]) == 40 and env["SECRET_A"].isalnum()
    assert len(env["SECRET_B"]) == 24 and set(env["SECRET_B"]) <= set("0123456789abcdef")
    assert env["GREETING"] == "'hello world'"
    assert env["DOMAIN_HOST"] == ""
    again = plain.build(SYNTHETIC)
    assert _env_of(again)["SECRET_A"] != env["SECRET_A"]


def _env_of(export):
    return dict(line.split("=", 1) for line in export.files[".env"][0].splitlines()
                if line and not line.startswith("#"))


def test_compose_of_the_synthetic_app(tmp_path):
    doc = yaml.safe_load(plain.build(SYNTHETIC).files[plain.COMPOSE][0])
    assert doc["services"]["web"]["ports"] == [
        "127.0.0.1:9999:9999", "127.0.0.1:10010:10010/udp", "127.0.0.1:7000:7000", "127.0.0.1:7001:7001",
        "127.0.0.1::5000", "127.0.0.1:${PORT:-6000}:6000",
        {"target": 53, "published": 5353, "protocol": "udp", "host_ip": "127.0.0.1"}, "127.0.0.1:8080:8080"]
    assert doc["services"]["web"]["networks"] == {"default": {}}
    assert "networks" not in doc["services"]["db"]
    assert "deploy" not in doc["services"]["web"]
    assert "networks" not in doc


def test_ask_fills_the_host_provided_values():
    export = plain.build(SYNTHETIC, ask=lambda key: "web.example.org")
    assert _env_of(export)["DOMAIN_HOST"] == "web.example.org"


def test_migrate_asks_ready_before_it_migrates(tmp_path):
    files = plain.build(SYNTHETIC).files
    lines = files["migrate.sh"][0].splitlines()
    ready = next(i for i, line in enumerate(lines) if line.startswith("until docker exec -u app"))
    first = next(i for i, line in enumerate(lines) if line == 'docker exec -u app "$cid" web migrate')
    assert ready < first
    assert "web --ready" in lines[ready] and "deadline=$(( $(date +%s) + 90 ))" in lines
    assert "-e " not in files["migrate.sh"][0]
    assert 'sh -c "$(cat hooks/migrate.sh)"' in files["migrate.sh"][0]
    assert files["hooks/migrate.sh"][0] == 'echo "$HOME"\n'


def test_users_and_scan_read_standard_input():
    files = plain.build(SYNTHETIC).files
    assert 'docker exec -i -e DOMAIN_HOST -e SECRET_A -e SECRET_B -e GREETING -u app "$cid" web users < "$input"' \
        in files["users.sh"][0]
    assert 'docker exec -i -u app "$cid" web scan < "$input"' in files["scan.sh"][0]


def test_a_value_with_a_quote_is_refused():
    text = SYNTHETIC.replace("hello world", "it's")
    with pytest.raises(plain.PlainError, match="GREETING"):
        plain.build(text)


# the folder

def test_an_existing_folder_is_refused(tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(plain.PlainError, match="exists already"):
        plain.export_app("actualbudget", tmp_path / "out")


def test_a_folder_inside_the_repository_is_refused():
    out = ROOT / "plain-export-test"
    with pytest.raises(plain.PlainError, match="inside a repository"):
        plain.export_app("actualbudget", out)
    assert not out.exists()


def test_a_folder_inside_the_templates_tree_is_refused(tmp_path):
    tree = _tree(tmp_path, SYNTHETIC)
    with pytest.raises(plain.PlainError, match="inside a repository"):
        plain.export_app("web", tree / "out", tree)


def test_a_link_to_the_repository_is_refused(tmp_path):
    (tmp_path / "link").symlink_to(ROOT)
    with pytest.raises(plain.PlainError, match="inside a repository"):
        plain.export_app("actualbudget", tmp_path / "link" / "out")


def test_the_command_prints_no_value(tmp_path, capsys):
    app = next(a for a in APPS if a not in REFUSED and "generated:" in (CATALOG / a / plain.COMPOSE).read_text())
    assert plain.main(["unpack", app, "--plain", "--out", str(tmp_path / "out")]) == 0
    shown = capsys.readouterr().out
    values = [v for v in _env(tmp_path / "out" / ".env").values() if len(v) >= 16]
    assert values and not [v for v in values if v in shown]
    assert str(tmp_path / "out") in shown


def test_the_command_refuses_a_bad_app_id(tmp_path, capsys):
    assert plain.main(["unpack", "../x", "--plain", "--out", str(tmp_path / "out")]) == 1
    assert "not a plain app id" in capsys.readouterr().err


# plain-check

def _fake_docker(tmp_path, monkeypatch, fail_up=False):
    """A docker on PATH that records its arguments; the log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        f'[ "$1 $2" = "compose up" ] && [ -n "{int(fail_up) or ""}" ] && exit 1\n'
        "exit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _web(port):
    return SYNTHETIC.replace("8080", str(port))


def test_plain_check_removes_everything_after_an_answer(tmp_path, monkeypatch):
    log = _fake_docker(tmp_path, monkeypatch)
    port = _port()
    server = http.server.HTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tree = _tree(tmp_path, _web(port))
        assert plain.plain_check("web", tree, interval=0.05) == 0
    finally:
        server.shutdown()
    calls = log.read_text().splitlines()
    assert calls == ["compose up -d", "compose down -v"]


def test_plain_check_runs_down_after_no_answer(tmp_path, monkeypatch, capsys):
    log = _fake_docker(tmp_path, monkeypatch)
    tree = _tree(tmp_path, _web(_port()))
    assert plain.plain_check("web", tree, timeout=0.3, interval=0.05) == 1
    assert log.read_text().splitlines() == ["compose up -d", "compose logs --tail 100", "compose down -v"]
    assert "no answer" in capsys.readouterr().err


def test_plain_check_runs_down_after_a_failed_start(tmp_path, monkeypatch):
    log = _fake_docker(tmp_path, monkeypatch, fail_up=True)
    tree = _tree(tmp_path, _web(_port()))
    assert plain.plain_check("web", tree, timeout=0.3, interval=0.05) == 1
    assert log.read_text().splitlines() == ["compose up -d", "compose logs --tail 100", "compose down -v"]


def test_plain_check_refuses_before_it_starts_anything(tmp_path, monkeypatch):
    log = _fake_docker(tmp_path, monkeypatch)
    with pytest.raises(plain.PlainError, match="own network"):
        plain.plain_check("coturn")
    with pytest.raises(plain.PlainError, match="no web address"):
        plain.plain_check("clamav")
    assert not log.exists()


# the review fixes

@pytest.mark.parametrize("app", [a for a in APPS if a not in REFUSED])
def test_every_published_port_is_on_loopback(tmp_path, app):
    _, out = _export(tmp_path, app)
    doc = yaml.safe_load((out / plain.COMPOSE).read_text())
    for spec in doc["services"].values():
        for entry in spec.get("ports") or []:
            assert entry["host_ip"] == "127.0.0.1" if isinstance(entry, dict) else entry.startswith("127.0.0.1:")


@pytest.mark.parametrize("old, new, match", [
    ('managed: {expr: "web.{{ cloudflare_zone }}"}', 'managed: {expr: "{{ zone }}\\ntouch /tmp/PWN"}', "DOMAIN_HOST"),
    ("    GREETING:", "    BAD KEY:", "not an env key"),
    ("    GREETING:", "    $(touch_x):", "not an env key"),
    ("  hooks:\n    service: web", "  hooks:\n    service: nope", "not a service"),
    ("  lifecycle:\n    service: web", '  lifecycle:\n    service: "web\\nid"', "not a service"),
    ("[web, users]", "[web, 7]", "list of strings"),
])
def test_a_block_that_could_inject_a_command_is_refused(old, new, match):
    with pytest.raises(plain.PlainError, match=match):
        plain.build(SYNTHETIC.replace(old, new))


def test_a_service_is_quoted_in_the_not_running_message():
    script = plain.build(SYNTHETIC).files["users.sh"][0]
    assert "echo 'web is not running' >&2" in script


def test_a_dollar_pair_of_a_one_line_command_is_one_dollar():
    files = plain.build(SYNTHETIC.replace("- [web, scan]", '- [web, scan, "$$HOME"]')).files
    assert "web scan '$HOME'" in files["scan.sh"][0]
    assert "$$" not in files["migrate.sh"][0].replace("$(( $(date", "")


def test_a_redirect_counts_as_an_answer():
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https:///")
            self.end_headers()

        def log_message(self, *args):
            pass

    port = _port()
    server = http.server.HTTPServer(("127.0.0.1", port), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert plain._answers(port)
    finally:
        server.shutdown()
    assert not plain._answers(_port())


def test_a_folder_in_any_work_tree_is_refused(tmp_path):
    (tmp_path / "work" / ".git").mkdir(parents=True)
    with pytest.raises(plain.PlainError, match="inside a repository"):
        plain.export_app("actualbudget", tmp_path / "work" / "deeper" / "out")
    assert not (tmp_path / "work" / "deeper").exists()
