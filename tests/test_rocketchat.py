"""Rocket.Chat's own declarations: the bundled Jitsi is wired through the
settings Rocket.Chat reads from its environment, with no configure hook and no
post-deploy call."""
from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "blueprints" / "rocketchat-oidc" / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_rocket_chat_dials_the_host_jitsi_web_answers_as():
    services = _compose()["services"]
    env = services["rocketchat"]["environment"]
    public_url = services["jitsi-web"]["environment"]["PUBLIC_URL"]
    route = next(label for label in services["jitsi-web"]["labels"] if label.startswith("vps.route.host="))
    host = public_url.removeprefix("https://")
    assert env["OVERWRITE_SETTING_Jitsi_Domain"] == host
    assert route == f"vps.route.host={host}"
    assert env["OVERWRITE_SETTING_Jitsi_SSL"] == "true"


def test_the_other_jitsi_settings_are_seeded_not_forced():
    env = _compose()["services"]["rocketchat"]["environment"]
    assert env["Jitsi_Enabled"] == "true"
    assert env["Jitsi_Enable_Channels"] == "true"
    assert env["Jitsi_URL_Room_Hash"] == "false"
    assert not {k for k in env if k.startswith("OVERWRITE_SETTING_Jitsi")} - {
        "OVERWRITE_SETTING_Jitsi_Domain", "OVERWRITE_SETTING_Jitsi_SSL"}


def test_the_only_hook_is_the_verify_check_that_asks_the_servers():
    hooks = _compose()["x-catena"]["hooks"]
    assert set(hooks) == {"service", "timeout_seconds", "verify"}
    assert hooks["service"] == "rocketchat"
    (argv,) = hooks["verify"]
    assert argv[:2] == ["node", "-e"]
    assert "/api/info" in argv[2]
    assert '"jitsi-web", 80' in argv[2]
    assert "Date.now() + 150000" in argv[2]
    assert 150 + 15 < hooks["timeout_seconds"] <= 300


class _Stub(http.server.BaseHTTPRequestHandler):
    """A route holds one answer, or a list of answers given in turn (the last one repeats)."""
    routes: dict = {}
    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.path)
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


INFO = b'{"version":"8.8.1","success":true}'
SWAP = [
    ('ask("127.0.0.1", 3000,', 'ask("127.0.0.1", {port},'),
    ('ask("jitsi-web", 80,', 'ask("127.0.0.1", {port},'),
    ("Date.now() + 150000", "Date.now() + 2000"),
    ("sleep(4000)", "sleep(100)"),
]


def _run(routes: dict, *, answer: bool = True):
    """The verify script with both servers pointed at one stub (or at a closed port)."""
    _Stub.routes, _Stub.seen = routes, []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    port = server.server_port if answer else 1
    if answer:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        argv = _compose()["x-catena"]["hooks"]["verify"][0]
        script = argv[2].replace("$$", "$")
        for old, new in SWAP:
            assert old in script, old
            script = script.replace(old, new.format(port=port))
        return subprocess.run([*argv[:2], script], capture_output=True, text=True, timeout=60), list(_Stub.seen)
    finally:
        server.server_close()


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_verify_passes_when_both_servers_answer():
    run, _ = _run({"/api/info": (200, INFO), "/": (200, b"<html></html>")})
    assert run.returncode == 0, run.stderr
    assert "Rocket.Chat 8.8.1 answers" in run.stdout


@needs_node
def test_verify_asks_again_while_a_server_is_starting():
    run, seen = _run({"/api/info": [(502, b""), (503, b""), (200, INFO)], "/": [(503, b""), (200, b"")]})
    assert run.returncode == 0, run.stderr
    assert len(seen) == 5
    assert run.stdout.count("\n") == 1 and run.stderr == ""


@needs_node
def test_verify_fails_at_once_for_a_definite_wrong_answer():
    run, seen = _run({"/api/info": (200, b"{}"), "/": (404, b"")})
    assert run.returncode == 1
    assert "/api/info answered HTTP 200 with no version" in run.stderr and "Jitsi web server answered HTTP 404" in run.stderr
    assert len(seen) == 2
    assert run.stderr.count("\n") == 1


@needs_node
def test_verify_fails_naming_the_last_answer_once_the_wait_is_over():
    run, seen = _run({"/api/info": (503, b""), "/": (200, b"")})
    assert run.returncode == 1 and "/api/info answered HTTP 503" in run.stderr
    assert len(seen) > 2
    run, _ = _run({}, answer=False)
    assert run.returncode == 1 and "ECONNREFUSED" in run.stderr and "Jitsi web server" in run.stderr


# The folders each Jitsi image declares as a VOLUME at docker-jitsi-meet tag
# stable-11031 (prosody/Dockerfile, jicofo/Dockerfile, jvb/Dockerfile,
# web/Dockerfile).
JITSI_VOLUMES = {
    "prosody": {"/config", "/prosody-plugins-custom"},
    "jicofo": {"/config"},
    "jvb": {"/config"},
    "jitsi-web": {"/config", "/usr/share/jitsi-meet/transcripts"},
}


def test_each_jitsi_volume_is_memory_and_no_jitsi_service_mounts_a_named_volume():
    services = _compose()["services"]
    for name, folders in JITSI_VOLUMES.items():
        mounts = services[name]["volumes"]
        assert {m["target"] for m in mounts if m.get("type") == "tmpfs"} == folders, name
        assert all(m.get("type") == "tmpfs" for m in mounts), name


def test_uploads_sit_on_a_named_volume_pinned_to_the_data_node():
    blueprint = _compose()
    rocketchat = blueprint["services"]["rocketchat"]
    assert "rocketchat-uploads:/app/uploads" in rocketchat["volumes"]
    assert "rocketchat-uploads" in blueprint["volumes"]
    assert "node.labels.catena.role==data" in rocketchat["deploy"]["placement"]["constraints"]


def test_the_video_bridge_takes_no_relay_setting():
    """The Jitsi video bridge reads no JVB_TURN_* value, so the template reads
    nothing of the call relay: it advertises the server's own address."""
    blueprint = _compose()
    block = blueprint["x-catena"]
    assert not [key for key in block["env"] if key.startswith("TURN_")]
    assert "secrets" not in block
    jvb = blueprint["services"]["jvb"]["environment"]
    assert not [key for key in jvb if "TURN" in key]
    assert jvb["JVB_ADVERTISE_IPS"] == "${VPS_PUBLIC_IP}"
