"""Element's own declarations: the start script of Synapse run against stand-ins
for the database wait and the homeserver, with and without the call relay."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model  # noqa: E402

COMPOSE = ROOT / "blueprints" / "element" / "docker-compose.yml"

# What Docker reads from the stack's environment when it deploys the file.
STACK = {
    "MATRIX_HOSTNAME": "matrix.example.test", "ELEMENT_HOSTNAME": "element.example.test",
    "DB_PASSWORD": "db-pass", "ALLOW_PUBLIC_REGISTRATION": "false", "FEDERATION_DOMAIN_WHITELIST": "",
    "SYNAPSE_REGISTRATION_SHARED_SECRET": "reg", "SYNAPSE_MACAROON_SECRET": "mac", "SYNAPSE_FORM_SECRET": "form",
}
RELAY = {"TURN_HOSTNAME": "turn.example.test", "TURN_PORT": "3478", "TURN_TLS_PORT": "5349", "TURN_SECRET": "relay-secret"}
NO_RELAY = {"TURN_HOSTNAME": "", "TURN_PORT": "", "TURN_TLS_PORT": "", "TURN_SECRET": ""}

HARNESS = """\
import os
import socket
import subprocess

socket.create_connection = lambda *a, **k: type("S", (), {"close": lambda self: None})()
subprocess.check_call = lambda *a, **k: None


def _exec(*a):
    raise SystemExit(0)


os.execvp = _exec
"""


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _interpolate(text: str, env: dict[str, str]) -> str:
    """Docker's interpolation of ${NAME} and ${NAME:-default} in a file."""
    def value(match: re.Match) -> str:
        return env.get(match.group(1)) or match.group(2) or ""
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", value, text.replace("$$", "\0")).replace("\0", "$")


def _homeserver(tmp_path: Path, relay: dict[str, str]) -> dict:
    """The homeserver.yaml the start script writes when the stack gives it
    `relay` in its environment."""
    synapse = _compose()["services"]["synapse"]
    script = _interpolate(synapse["entrypoint"][2], {**STACK, **relay})
    data = tmp_path / "data"
    (tmp_path / "start.py").write_text(HARNESS + script.replace('"/data"', repr(str(data))), encoding="utf-8")
    env = {key: _interpolate(str(value), {**STACK, **relay}) for key, value in synapse["environment"].items()}
    run = subprocess.run([sys.executable, "-I", str(tmp_path / "start.py")], capture_output=True, text=True,
                         timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    return yaml.safe_load((data / "homeserver.yaml").read_text(encoding="utf-8"))


def test_synapse_hands_its_clients_the_relay_the_stack_gives(tmp_path):
    config = _homeserver(tmp_path, RELAY)
    assert config["turn_uris"] == ["turn:turn.example.test:3478?transport=udp",
                                   "turns:turn.example.test:5349?transport=tcp"]
    assert config["turn_shared_secret"] == "relay-secret"
    assert config["turn_user_lifetime"] == 86400000 and config["turn_allow_guests"] is True
    assert config["oidc_providers"][0]["idp_id"] == "keycloak"


def test_synapse_hands_out_no_relay_when_the_server_has_none(tmp_path):
    config = _homeserver(tmp_path, NO_RELAY)
    assert not [key for key in config if key.startswith("turn_")]
    assert config["server_name"] == "matrix.example.test" and config["password_config"] == {"enabled": False}


def test_the_relay_secret_reaches_synapse_by_its_environment_alone():
    synapse = _compose()["services"]["synapse"]
    script = synapse["entrypoint"][2]
    assert not re.search(r"(?<!\$)\$\{?TURN_", script)
    assert synapse["environment"]["TURN_SECRET"] == "${TURN_SECRET:-}"


def test_the_template_reads_the_relay_the_host_provides_and_stores_no_secret_of_it():
    entry = next(e for e in model.load_sources() if e.slug == "element")
    assert {k: v for k, v in entry.managed_env.items() if k.startswith("TURN_")} == {
        "TURN_HOSTNAME": "{{ provided.turn.host }}", "TURN_PORT": "{{ provided.turn.port }}",
        "TURN_TLS_PORT": "{{ provided.turn.tls_port }}", "TURN_SECRET": "{{ provided.turn.secret }}"}
    assert "secrets" not in entry.block


# The folders each Jitsi image declares as a VOLUME at docker-jitsi-meet tag
# stable-11031 (prosody/Dockerfile, jicofo/Dockerfile, jvb/Dockerfile,
# web/Dockerfile, jigasi/Dockerfile).
JITSI_VOLUMES = {
    "prosody": {"/config", "/prosody-plugins-custom"},
    "jicofo": {"/config"},
    "jvb": {"/config"},
    "jitsi-web": {"/config", "/usr/share/jitsi-meet/transcripts"},
    "jigasi": {"/config", "/tmp/transcripts"},
}


def test_each_jitsi_volume_is_memory_and_no_jitsi_service_mounts_a_named_volume():
    services = _compose()["services"]
    for name, folders in JITSI_VOLUMES.items():
        mounts = services[name]["volumes"]
        assert {m["target"] for m in mounts if m.get("type") == "tmpfs"} == folders, name
        assert all(m.get("type") == "tmpfs" for m in mounts), name


def test_the_video_bridge_takes_no_relay_setting():
    jvb = _compose()["services"]["jvb"]["environment"]
    assert not [key for key in jvb if "TURN" in key]
    assert jvb["JVB_ADVERTISE_IPS"] == "${VPS_PUBLIC_IP}"
