"""Rocket.Chat's own declarations: the bundled Jitsi is wired through the
settings Rocket.Chat reads from its environment, with no hook and no post-deploy
call."""
from __future__ import annotations

from pathlib import Path

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


def test_no_hook_wires_jitsi():
    blueprint = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "hooks" not in blueprint["x-catena"]


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
