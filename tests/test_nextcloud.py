"""Nextcloud's own declarations: the configure and verify hooks run against a
stand-in for php (occ), curl, getent and sleep that keeps the state of an
instance in a file, and the declarations the hooks rely on."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

BLUEPRINT = ROOT / "blueprints" / "nextcloud-s3-oidc"

ENV = {
    "S3_BUCKET": "files-bucket", "S3_HOST": "s3.example.test", "S3_PORT": "443", "S3_SSL": "true",
    "OIDC_CLIENT_ID": "nextcloud", "OIDC_CLIENT_SECRET": "s3cr3t",
    "OIDC_ISSUER_URL": "https://auth.example.test/realms/vps/",
    "SIGNALING_HOSTNAME": "signaling.example.test", "SIGNALING_SECRET": "sig-secret",
    "TURN_HOSTNAME": "turn.example.test", "TURN_PORT": "3478", "TURN_TLS_PORT": "5349",
    "TURN_SECRET": "turn-secret",
    "WEBMAIL_HOSTNAME": "webmail.example.test",
    "ANTIVIRUS_HOST": "clamav", "ANTIVIRUS_PORT": "3310",
}

COLLABORA = {"OFFICE_EDITOR": "collabora", "OFFICE_URL": "https://office.example.test"}
ONLYOFFICE = {"OFFICE_EDITOR": "onlyoffice", "OFFICE_URL": "https://office.example.test",
              "OFFICE_JWT_SECRET": "jwt-secret"}

# php (occ), curl and getent as the container has them, over one state file.
FAKE = r'''#!{python}
import json, os, sys

path = os.environ["FAKE_STATE"]
tool = os.path.basename(sys.argv[0])
argv = sys.argv[1:]

# The listing's reader runs beside the occ that prints it, so it reads no state.
if tool == "php" and argv[:1] == ["-r"]:
    data = json.loads(sys.stdin.read() or "{}")
    sys.exit(0 if argv[2] in data.get("enabled", {}) else 1)

state = json.load(open(path))


def save():
    json.dump(state, open(path, "w"))


def log(line):
    state["calls"].append(line)
    save()


def options(rest):
    out, plain, i = {}, [], 0
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("--"):
            key, eq, value = arg[2:].partition("=")
            if not eq and key in ("value", "type", "secret") and i + 1 < len(rest):
                value, i = rest[i + 1], i + 1
            out[key] = value
        else:
            plain.append(arg)
        i += 1
    return out, plain


if tool == "getent":
    log("getent " + " ".join(argv))
    sys.exit(0 if state["roundcube"] else 2)

if tool == "curl":
    url = argv[-1]
    log("curl " + url)
    if url.endswith("/hosting/discovery") or url.endswith("/healthcheck"):
        if not state["office_up"]:
            sys.exit(7)
        print("<wopi-discovery>" if url.endswith("discovery") else "true")
        sys.exit(0)
    up = state["signaling_up"] if "signaling:8081" in url else state["s3_up"]
    sys.exit(0 if up else 7)

# php
if not argv:
    script = sys.stdin.read()
    if "mimetypes" in script:
        if state["filecache_down"]:
            print("catena: connection refused", file=sys.stderr)
            sys.exit(2)
        print("\n".join(str(i) for i in state["filecache_ids"]))
        sys.exit(0)
    sys.exit(state["db_rc"])
cmd, rest = argv[1], argv[2:]
line = " ".join(argv[1:])
log(line)
apps, appconfig, system = state["apps"], state["appconfig"], state["system"]
if state.get("fail") and line.startswith(state["fail"]):
    print("forced failure", file=sys.stderr)
    sys.exit(1)

if cmd == "status":
    sys.exit(0 if state["ready"] else 1)
if cmd == "files:object:list":
    print("+-----------+\n| Object    |\n+-----------+")
    for urn in state["bucket"]:
        print("| " + urn + " |")
    sys.exit(0)
if cmd == "app:getpath":
    sys.exit(0 if rest[0] in apps else 1)
if cmd == "app:install":
    if rest[0] in state["store"] and rest[0] not in apps:
        apps[rest[0]] = "enabled"
        appconfig[rest[0] + "/installed_version"] = state["store"][rest[0]]
        save()
        sys.exit(0)
    sys.exit(1)
if cmd == "app:enable":
    if rest[0] in apps:
        apps[rest[0]] = "enabled"
        save()
        sys.exit(0)
    sys.exit(1)
if cmd == "app:remove":
    apps.pop(rest[0], None)
    save()
    sys.exit(0)
if cmd == "config:app:delete":
    appconfig.pop(rest[0] + "/" + rest[1], None)
    save()
    sys.exit(0)
if cmd == "richdocuments:activate-config":
    sys.exit(0)
if cmd == "app:list":
    print(json.dumps({"enabled": {a: "1" for a, s in apps.items() if s == "enabled"},
                      "disabled": {a: "1" for a, s in apps.items() if s != "enabled"}}))
    sys.exit(0)
if cmd == "config:app:get":
    key = rest[0] + "/" + rest[1]
    if key in appconfig:
        print(appconfig[key])
        sys.exit(0)
    sys.exit(1)
if cmd == "config:app:set":
    opts, plain = options(rest)
    appconfig[plain[0] + "/" + plain[1]] = opts.get("value", "")
    save()
    sys.exit(0)
if cmd == "config:system:get":
    key = "/".join(rest)
    if key in system:
        print(system[key])
        sys.exit(0)
    sys.exit(1)
if cmd == "config:system:set":
    opts, plain = options(rest)
    system["/".join(plain)] = opts.get("value", "")
    save()
    sys.exit(0)
if cmd == "user_oidc:provider":
    opts, plain = options(rest[1:])
    opts.pop("no-interaction", None)
    shown = opts.pop("output", None)
    providers = state["providers"]
    if not opts:
        if rest[0] in providers:
            print(json.dumps(providers[rest[0]]))
            sys.exit(0)
        print("no such provider", file=sys.stderr)
        sys.exit(1)
    providers.setdefault(rest[0], {}).update(opts)
    save()
    sys.exit(0)
if cmd.startswith("talk:"):
    talk = state["talk"]
    opts, plain = options(rest)
    if cmd == "talk:turn:add":
        row = [plain[0], plain[1], plain[2], opts.get("secret", "")]
        if any(r[:3] == row[:3] for r in talk["turn"]):
            print("the entry exists already", file=sys.stderr)
            sys.exit(1)
        talk["turn"].append(row)
    elif cmd == "talk:turn:delete":
        talk["turn"] = [r for r in talk["turn"] if r[:3] != plain[:3]]
    elif cmd == "talk:stun:add":
        if plain[0] in talk["stun"]:
            print("the entry exists already", file=sys.stderr)
            sys.exit(1)
        talk["stun"].append(plain[0])
    elif cmd == "talk:stun:delete":
        talk["stun"] = [s for s in talk["stun"] if s != plain[0]]
    elif cmd == "talk:signaling:add":
        talk["signaling"].append([plain[0], plain[1]])
    elif cmd == "talk:signaling:delete":
        talk["signaling"] = [s for s in talk["signaling"] if s[0] != plain[0]]
    save()
    sys.exit(0)
print("unknown command " + cmd, file=sys.stderr)
sys.exit(1)
'''


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == "nextcloud-s3-oidc")


def _compose() -> dict:
    return yaml.safe_load((BLUEPRINT / "docker-compose.yml").read_text(encoding="utf-8"))


def _body(name: str) -> str:
    [argv] = _entry().block["hooks"][name]
    assert argv[:2] == ["sh", "-c"]
    return argv[2].replace("$$", "$")


def test_the_hooks_run_in_the_app_service_as_www_data():
    hooks = _entry().block["hooks"]
    assert hooks["service"] in _compose()["services"]
    assert hooks["user"] == "www-data"
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"configure", "verify", "reconcile"}
    assert quiesce_lint.lint_hooks(_entry()) == []
    assert quiesce_lint.lint_block(_entry()) == []


@pytest.mark.parametrize("name", ["configure", "verify", "reconcile"])
def test_each_body_parses_in_sh(name):
    subprocess.run(["sh", "-n", "-c", _body(name)], check=True)


def test_the_values_the_hooks_read_come_from_the_env_file_not_the_container():
    env = _entry().block["env"]
    app_env = _compose()["services"]["app"]["environment"]
    for key in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"):
        assert key not in env  # the settings sync is their one writer
    for key in ("SIGNALING_HOSTNAME", "SIGNALING_SECRET", "TURN_HOSTNAME", "TURN_PORT", "TURN_TLS_PORT",
                "TURN_SECRET", "ANTIVIRUS_HOST", "ANTIVIRUS_PORT", "OFFICE_EDITOR", "OFFICE_URL", "OFFICE_JWT_SECRET"):
        assert key in env and key not in app_env
    assert not any(k.startswith("NEXTCLOUD_OIDC_") or k == "NEXTCLOUD_USER_OIDC_VERSION" for k in app_env)
    assert "WEBMAIL_HOSTNAME" in env
    assert "talk-hpb" in _compose()["services"]


def test_the_configure_body_names_the_user_oidc_release_its_provider_command_is_written_for():
    """The hook installs user_oidc from the app store and checks the installed
    release against this one before it runs the provider command written for
    it. Moving it is a decision to make after reading that command against the
    new release."""
    assert "USER_OIDC_VERSION=8.11.0\n" in _body("configure")


def test_the_wait_for_a_first_install_fits_inside_the_hook_timeout():
    assert "WAIT=900\n" in _body("configure")
    assert _entry().block["hooks"]["timeout_seconds"] > 900


class Instance:
    """A Nextcloud container the hooks run in: a tree under tmp_path, the
    stand-in tools, and the state file they share."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "html"
        (self.root / "config").mkdir(parents=True)
        self.image = tmp_path / "image"
        (self.image / "config").mkdir(parents=True)
        (self.image / "config" / "s3.config.php").write_text("<?php // image copy\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for tool in ("php", "curl", "getent"):
            (self.bin / tool).write_text(FAKE.replace("{python}", sys.executable))
            (self.bin / tool).chmod(0o755)
        (self.bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "sleep").chmod(0o755)
        self.state_path = tmp_path / "state.json"
        self.state = {
            "calls": [], "apps": {}, "appconfig": {}, "system": {}, "providers": {},
            "talk": {"turn": [], "stun": [], "signaling": []},
            "store": {"user_oidc": "8.11.3", "mail": "5.0", "files_antivirus": "6.0", "spreed": "21.0",
                      "external": "5.4", "richdocuments": "9.0", "onlyoffice": "9.0"},
            "ready": True, "signaling_up": True, "roundcube": True, "s3_up": True, "office_up": True,
            "db_rc": 1, "bucket": [], "filecache_ids": [], "filecache_down": False,
        }
        self.installed()

    def installed(self, maintenance: bool = False):
        flag = "true" if maintenance else "false"
        (self.root / "config" / "config.php").write_text(
            f"<?php\n$CONFIG = array (\n  'installed' => true,\n  'maintenance' => {flag},\n);\n")

    def calls(self) -> list[str]:
        return json.loads(self.state_path.read_text())["calls"]

    def now(self) -> dict:
        return json.loads(self.state_path.read_text())

    def run(self, name: str, **env) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps(self.state))
        body = (_body(name).replace("/var/www/html", str(self.root))
                .replace("/usr/src/nextcloud", str(self.image)))
        body = re.sub(r"^WAIT=\d+$", "WAIT=10", body, flags=re.M)
        environ = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_STATE": str(self.state_path)}
        environ.update({**ENV, **env})
        run = subprocess.run(["sh", "-c", body], env=environ, capture_output=True, text=True, timeout=120)
        self.state = self.now()
        self.state["calls"] = []
        return run


@pytest.fixture
def nc(tmp_path):
    return Instance(tmp_path)


# configure

def test_a_first_configure_wires_everything_the_instance_can_take(nc):
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    state = nc.state
    assert {a: s for a, s in state["apps"].items()} == {a: "enabled" for a in
                                                          ("user_oidc", "mail", "files_antivirus", "spreed", "external")}
    provider = state["providers"]["keycloak"]
    assert provider["clientid"] == "nextcloud" and provider["clientsecret"] == "s3cr3t"
    assert provider["discoveryuri"] == "https://auth.example.test/realms/vps/.well-known/openid-configuration"
    assert provider["scope"] == "openid email profile groups" and provider["mapping-uid"] == "preferred_username"
    config = state["appconfig"]
    assert config["files_antivirus/av_mode"] == "daemon" and config["files_antivirus/av_host"] == "clamav"
    assert config["files_antivirus/av_port"] == "3310" and config["catena/antivirus_wired"] == "1"
    assert config["spreed/external_signaling_only"] == "yes"
    assert state["system"]["external_sites/0/url"] == "https://webmail.example.test"


def _talk(nc) -> dict:
    return nc.state["talk"]


def test_talk_is_given_the_relay_and_the_signaling_server(nc):
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert _talk(nc) == {
        "turn": [["turn", "turn.example.test:3478", "udp", "turn-secret"],
                 ["turns", "turn.example.test:5349", "tcp", "turn-secret"]],
        "stun": ["turn.example.test:3478"],
        "signaling": [["https://signaling.example.test", "sig-secret"]],
    }
    config = nc.state["appconfig"]
    assert config["catena/turn_wired"].startswith("turn.example.test 3478 5349 ")
    assert "turn-secret" not in config["catena/turn_wired"]
    assert config["catena/signaling_wired"].startswith("https://signaling.example.test ")
    assert "sig-secret" not in config["catena/signaling_wired"]


def test_a_second_configure_adds_nothing_twice(nc):
    assert nc.run("configure").returncode == 0
    first = json.loads(json.dumps(_talk(nc)))
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert _talk(nc) == first
    assert not any(c.startswith(("talk:turn:add", "talk:stun:add", "talk:signaling:add")) for c in nc.calls())


def test_a_changed_secret_or_port_replaces_the_entries_the_hook_wrote(nc):
    assert nc.run("configure").returncode == 0
    assert nc.run("configure", TURN_SECRET="rotated", TURN_PORT="3479").returncode == 0
    assert _talk(nc)["turn"] == [["turn", "turn.example.test:3479", "udp", "rotated"],
                                 ["turns", "turn.example.test:5349", "tcp", "rotated"]]
    assert _talk(nc)["stun"] == ["turn.example.test:3479"]
    assert nc.run("configure", SIGNALING_SECRET="sig-rotated").returncode == 0
    assert _talk(nc)["signaling"] == [["https://signaling.example.test", "sig-rotated"]]


def test_entries_left_by_a_failed_run_are_replaced_not_refused(nc):
    """Talk refuses to add an entry that exists; the hook removes the ones it is
    about to add, so a run that failed half way is mended by the next."""
    _talk(nc)["turn"].append(["turn", "turn.example.test:3478", "udp", "stale"])
    _talk(nc)["stun"].append("turn.example.test:3478")
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert _talk(nc)["turn"][0] == ["turn", "turn.example.test:3478", "udp", "turn-secret"]
    assert _talk(nc)["stun"] == ["turn.example.test:3478"]
    assert nc.state["appconfig"]["catena/turn_wired"].startswith("turn.example.test 3478 5349 ")


def test_without_the_relay_talk_gets_no_relay_and_no_signaling_entry(nc):
    run = nc.run("configure", TURN_HOSTNAME="", TURN_PORT="", TURN_TLS_PORT="", TURN_SECRET="")
    assert run.returncode == 0, run.stderr
    assert nc.state["apps"]["spreed"] == "enabled"
    assert _talk(nc) == {"turn": [], "stun": [], "signaling": []}
    assert not any(k.startswith(("catena/turn", "catena/signaling")) for k in nc.state["appconfig"])
    assert not any(c.startswith("talk:") for c in nc.calls() if "delete" in c or "add" in c)


def test_a_relay_that_goes_takes_its_entries_away_and_leaves_an_administrators(nc):
    assert nc.run("configure").returncode == 0
    _talk(nc)["turn"].append(["turns", "turn.other.test:443", "tcp", "mine"])
    _talk(nc)["signaling"].append(["wss://own.example.test", "own"])
    run = nc.run("configure", TURN_HOSTNAME="", TURN_PORT="", TURN_TLS_PORT="", TURN_SECRET="")
    assert run.returncode == 0, run.stderr
    assert _talk(nc) == {"turn": [["turns", "turn.other.test:443", "tcp", "mine"]], "stun": [],
                         "signaling": [["wss://own.example.test", "own"]]}
    assert not any(k.startswith(("catena/turn", "catena/signaling")) for k in nc.state["appconfig"])
    assert "spreed/external_signaling_only" not in nc.state["appconfig"]


def test_the_relay_is_wired_while_the_signaling_server_is_down(nc):
    nc.state["signaling_up"] = False
    assert nc.run("configure").returncode == 0
    assert _talk(nc)["turn"] and _talk(nc)["signaling"] == []
    assert "catena/signaling_wired" not in nc.state["appconfig"]


def test_a_second_configure_keeps_what_an_administrator_changed(nc):
    assert nc.run("configure").returncode == 0
    nc.state["appconfig"]["files_antivirus/av_infected_action"] = "delete"
    nc.state["providers"]["keycloak"]["scope"] = "openid custom"
    nc.state["apps"]["mail"] = "disabled"
    run = nc.run("configure", OIDC_CLIENT_SECRET="rotated")
    assert run.returncode == 0, run.stderr
    state = nc.state
    assert state["appconfig"]["files_antivirus/av_infected_action"] == "delete"
    assert state["providers"]["keycloak"]["scope"] == "openid custom"
    assert state["providers"]["keycloak"]["clientsecret"] == "rotated"
    assert state["apps"]["mail"] == "disabled"
    assert "app:enable mail" not in nc.calls()
    assert not any(c.startswith("config:app:set files_antivirus") for c in nc.calls())


def test_a_provider_that_exists_is_updated_with_its_managed_values_alone(nc):
    nc.state["apps"]["user_oidc"] = "enabled"
    nc.state["appconfig"]["user_oidc/installed_version"] = "8.11.0"
    nc.state["providers"]["keycloak"] = {"clientid": "old", "scope": "kept"}
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    update = [c for c in nc.calls() if c.startswith("user_oidc:provider keycloak --no-interaction")]
    assert len(update) == 1
    assert "--scope" not in update[0] and "--mapping" not in update[0]
    assert "--clientid=nextcloud" in update[0]
    assert nc.state["providers"]["keycloak"]["scope"] == "kept"


def test_an_app_the_app_store_does_not_have_is_left_for_the_next_deploy(nc):
    nc.state["store"] = {}
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert "could not be installed from the app store" in run.stderr
    assert nc.state["apps"] == {} and nc.state["providers"] == {}


def test_the_sign_in_provider_is_refused_over_another_major_version_of_user_oidc(nc):
    nc.state["store"]["user_oidc"] = "9.0.1"
    run = nc.run("configure")
    assert run.returncode == 1
    assert "user_oidc 9.0.1 is installed" in run.stderr
    assert "written for user_oidc 8.11.0" in run.stderr
    assert nc.state["providers"] == {}
    assert nc.state["apps"]["mail"] == "enabled"  # the rest was still wired


def test_an_older_release_of_user_oidc_is_refused_too(nc):
    nc.state["store"]["user_oidc"] = "8.10.0"
    assert nc.run("configure").returncode == 1


def test_without_the_sign_in_values_everything_else_is_wired(nc):
    run = nc.run("configure", OIDC_CLIENT_ID="", OIDC_CLIENT_SECRET="", OIDC_ISSUER_URL="")
    assert run.returncode == 0, run.stderr
    assert "user_oidc" not in nc.state["apps"] and nc.state["providers"] == {}
    assert nc.state["apps"]["mail"] == "enabled"
    assert "sign-in entry values are not in the environment" in run.stdout


def test_the_webmail_link_waits_for_a_mail_server(nc):
    nc.state["roundcube"] = False
    assert nc.run("configure").returncode == 0
    assert "external" not in nc.state["apps"]
    assert not any(k.startswith("external_sites") for k in nc.state["system"])


def test_the_webmail_link_follows_a_changed_address(nc):
    assert nc.run("configure").returncode == 0
    assert nc.run("configure", WEBMAIL_HOSTNAME="mail2.example.test").returncode == 0
    assert nc.state["system"]["external_sites/0/url"] == "https://mail2.example.test"
    assert nc.state["system"]["external_sites/0/redirect"] == "true"


def test_maintenance_mode_leaves_the_wiring_for_the_next_deploy(nc):
    nc.installed(maintenance=True)
    nc.state["ready"] = False
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert "maintenance mode is on" in run.stdout
    assert nc.state["apps"] == {}


def test_a_restore_that_has_not_replayed_the_database_is_not_a_failure(nc):
    nc.state["ready"] = False
    nc.state["db_rc"] = 0
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert "holds no Nextcloud table yet" in run.stdout
    assert nc.state["apps"] == {}


def test_a_first_install_that_never_finishes_is_not_a_failure_either(nc):
    (nc.root / "config" / "config.php").unlink()
    nc.state["ready"] = False
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert "did not answer within 10s" in run.stderr
    assert nc.state["apps"] == {}


def test_a_wiring_command_that_fails_on_an_answering_instance_fails_the_hook(nc):
    nc.state["fail"] = "talk:stun:add"
    run = nc.run("configure")
    assert run.returncode == 1
    assert "the Talk relay entries failed" in run.stderr
    assert nc.state["appconfig"]["spreed/external_signaling_only"] == "yes"  # later steps still ran


def test_the_antivirus_settings_are_not_marked_done_when_one_fails(nc):
    nc.state["fail"] = "config:app:set files_antivirus av_port"
    run = nc.run("configure")
    assert run.returncode == 1
    assert "catena/antivirus_wired" not in nc.state["appconfig"]


def test_the_scanner_is_wired_to_the_address_the_provider_gave(nc):
    assert nc.run("configure", ANTIVIRUS_HOST="scanner", ANTIVIRUS_PORT="3311").returncode == 0
    config = nc.state["appconfig"]
    assert config["files_antivirus/av_host"] == "scanner" and config["files_antivirus/av_port"] == "3311"


def test_without_a_scanner_the_antivirus_is_left_alone(nc):
    run = nc.run("configure", ANTIVIRUS_HOST="", ANTIVIRUS_PORT="")
    assert run.returncode == 0, run.stderr
    assert "files_antivirus" not in nc.state["apps"]
    assert "catena/antivirus_wired" not in nc.state["appconfig"]


def test_the_services_that_scan_join_the_scanners_own_network():
    compose = _compose()
    for name in ("app", "cron"):
        assert "antivirus" in compose["services"][name]["networks"]
    assert compose["networks"]["antivirus"] == {"external": True, "name": "catena-antivirus"}


def test_the_scanner_is_on_no_shared_network():
    clamav = yaml.safe_load((ROOT / "blueprints" / "clamav" / "docker-compose.yml").read_text(encoding="utf-8"))
    joined = set(clamav["services"]["clamav"]["networks"])
    assert joined == {"antivirus", "default"}
    assert clamav["networks"] == {"antivirus": {"external": True, "name": "catena-antivirus"}}


# office

def test_collabora_is_wired_to_the_address_the_provider_gave(nc):
    run = nc.run("configure", **COLLABORA)
    assert run.returncode == 0, run.stderr
    config = nc.state["appconfig"]
    assert nc.state["apps"]["richdocuments"] == "enabled"
    assert config["richdocuments/wopi_url"] == "https://office.example.test"
    assert config["richdocuments/public_wopi_url"] == "https://office.example.test"
    assert config["richdocuments/disable_certificate_verification"] == "no"
    assert "curl https://office.example.test/hosting/discovery" in nc.calls()


def test_onlyoffice_is_wired_with_its_jwt_secret(nc):
    run = nc.run("configure", **ONLYOFFICE)
    assert run.returncode == 0, run.stderr
    config = nc.state["appconfig"]
    assert nc.state["apps"]["onlyoffice"] == "enabled"
    assert config["onlyoffice/DocumentServerUrl"] == "https://office.example.test/"
    assert config["onlyoffice/DocumentServerInternalUrl"] == "https://office.example.test/"
    assert config["onlyoffice/StorageUrl"] == "" and config["onlyoffice/jwt_secret"] == "jwt-secret"
    assert "jwt-secret" not in run.stdout + run.stderr


def test_the_editor_that_was_wired_before_is_removed_with_its_settings(nc):
    nc.state["apps"]["onlyoffice"] = "enabled"
    nc.state["appconfig"].update({"onlyoffice/jwt_secret": "old", "onlyoffice/DocumentServerUrl": "https://x/"})
    run = nc.run("configure", **COLLABORA)
    assert run.returncode == 0, run.stderr
    assert "onlyoffice" not in nc.state["apps"]
    assert not any(k.startswith("onlyoffice/") for k in nc.state["appconfig"])
    assert nc.state["apps"]["richdocuments"] == "enabled"


def test_with_no_editor_provided_an_editor_an_administrator_set_up_stays(nc):
    nc.state["apps"]["onlyoffice"] = "enabled"
    nc.state["appconfig"]["onlyoffice/jwt_secret"] = "mine"
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert nc.state["apps"]["onlyoffice"] == "enabled"
    assert nc.state["appconfig"]["onlyoffice/jwt_secret"] == "mine"


def test_an_editor_that_does_not_answer_fails_the_hook_and_changes_nothing(nc):
    nc.state["office_up"] = False
    run = nc.run("configure", **COLLABORA)
    assert run.returncode == 1
    assert "does not answer" in run.stderr
    assert "richdocuments" not in nc.state["apps"]


def test_onlyoffice_without_a_jwt_secret_is_not_wired(nc):
    run = nc.run("configure", **{**ONLYOFFICE, "OFFICE_JWT_SECRET": ""})
    assert run.returncode == 1
    assert "gave no JWT secret" in run.stderr
    assert "onlyoffice" not in nc.state["apps"]


def test_the_object_store_file_is_put_back_from_the_image(nc):
    assert nc.run("configure").returncode == 0
    assert (nc.root / "config" / "s3.config.php").read_text() == "<?php // image copy\n"


def test_an_object_store_file_already_there_is_kept(nc):
    (nc.root / "config" / "s3.config.php").write_text("<?php // mine\n")
    assert nc.run("configure").returncode == 0
    assert (nc.root / "config" / "s3.config.php").read_text() == "<?php // mine\n"


def test_no_object_store_file_is_written_into_a_config_directory_that_is_not_installed_yet(nc):
    (nc.root / "config" / "config.php").unlink()
    nc.state["ready"] = False
    nc.run("configure")
    assert not (nc.root / "config" / "s3.config.php").exists()


def test_no_object_store_file_is_written_without_a_bucket(nc):
    assert nc.run("configure", S3_BUCKET="").returncode == 0
    assert not (nc.root / "config" / "s3.config.php").exists()


# verify

def _healthy(nc: Instance):
    nc.state["system"].update({"objectstore/class": "\\OC\\Files\\ObjectStore\\S3",
                               "objectstore/arguments/bucket": "files-bucket"})


def test_verify_passes_for_an_instance_on_its_bucket_with_the_endpoint_answering(nc):
    _healthy(nc)
    run = nc.run("verify")
    assert run.returncode == 0, run.stderr
    assert "uses the bucket files-bucket" in run.stdout
    assert "curl https://s3.example.test:443/" in nc.calls()


def test_verify_asks_a_plain_http_endpoint_over_http(nc):
    _healthy(nc)
    assert nc.run("verify", S3_SSL="false", S3_PORT="9000").returncode == 0
    assert "curl http://s3.example.test:9000/" in nc.calls()


def test_verify_fails_when_nextcloud_does_not_answer(nc):
    nc.state["ready"] = False
    run = nc.run("verify")
    assert run.returncode == 1 and "does not answer" in run.stderr


def test_verify_passes_during_a_backup_window(nc):
    nc.installed(maintenance=True)
    nc.state["ready"] = False
    run = nc.run("verify")
    assert run.returncode == 0 and "maintenance mode" in run.stdout


def test_verify_fails_when_the_object_store_is_not_configured(nc):
    run = nc.run("verify")
    assert run.returncode == 1 and "the object store is not configured" in run.stderr


def test_verify_fails_when_nextcloud_uses_another_bucket(nc):
    _healthy(nc)
    nc.state["system"]["objectstore/arguments/bucket"] = "old-bucket"
    run = nc.run("verify")
    assert run.returncode == 1
    assert "uses the bucket 'old-bucket' while the environment names 'files-bucket'" in run.stderr


def test_verify_fails_when_the_endpoint_does_not_answer(nc):
    _healthy(nc)
    nc.state["s3_up"] = False
    run = nc.run("verify")
    assert run.returncode == 1 and "s3.example.test:443 does not answer" in run.stderr


def test_verify_without_a_bucket_only_asks_nextcloud_to_answer(nc):
    run = nc.run("verify", S3_BUCKET="")
    assert run.returncode == 0 and run.stdout.strip() == "Nextcloud answers"


# the declarations the engines read

def test_the_block_declares_the_object_store_the_hooks_check():
    entry = _entry()
    assert entry.object_store["env"]["bucket"] == "S3_BUCKET"
    assert quiesce_lint.lint_object_store(entry) == []
    for key in ("S3_BUCKET", "S3_HOST", "S3_PORT", "S3_SSL"):
        assert key in entry.env


# reconcile

def _indexed(nc: Instance, ids, held):
    nc.state["filecache_ids"] = list(ids)
    nc.state["bucket"] = [f"urn:oid:{i}" for i in held]


def test_reconcile_counts_the_files_the_bucket_lacks(nc):
    _indexed(nc, [3, 4, 5, 6, 10], [3, 5, 10, 11])
    run = nc.run("reconcile")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[-1] == "missing=2 bucket=4 database=5"
    assert "urn:oid:4 urn:oid:6" in run.stdout


def test_reconcile_of_a_complete_bucket_says_zero_missing(nc):
    _indexed(nc, [3, 4], [3, 4])
    run = nc.run("reconcile")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[-1] == "missing=0 bucket=2 database=2"


def test_reconcile_of_an_empty_bucket_reports_every_row_as_missing(nc):
    _indexed(nc, [3, 4], [])
    run = nc.run("reconcile")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[-1] == "missing=2 bucket=0 database=2"


def test_reconcile_in_maintenance_mode_prints_no_count(nc):
    _indexed(nc, [3], [3])
    nc.installed(maintenance=True)
    nc.state["ready"] = False
    run = nc.run("reconcile")
    assert run.returncode == 0, run.stderr
    assert "missing=" not in run.stdout
    assert "maintenance mode is on" in run.stdout


def test_reconcile_fails_when_the_bucket_cannot_be_listed(nc):
    _indexed(nc, [3], [3])
    nc.state["fail"] = "files:object:list"
    run = nc.run("reconcile")
    assert run.returncode == 1
    assert "missing=" not in run.stdout
    assert "files:object:list failed" in run.stderr


def test_reconcile_fails_when_the_database_cannot_be_read(nc):
    _indexed(nc, [3], [3])
    nc.state["filecache_down"] = True
    run = nc.run("reconcile")
    assert run.returncode == 1
    assert "missing=" not in run.stdout
    assert "the filecache query failed" in run.stderr


@pytest.mark.parametrize("tool,call", [("sort", 1), ("sort", 2), ("comm", 1)])
def test_reconcile_prints_no_count_when_a_sort_or_the_comparison_fails(nc, tool, call):
    """A tool that cannot write its result (a full /tmp) must not leave a
    short file that reads as a count."""
    _indexed(nc, [3, 4], [3])
    counter = nc.bin / f"{tool}.calls"
    (nc.bin / tool).write_text(
        f'#!/bin/sh\nn=$(cat "{counter}" 2>/dev/null || echo 0)\nn=$((n + 1))\n'
        f'echo $n >"{counter}"\n[ "$n" -eq {call} ] && exit 1\nexec /usr/bin/{tool} "$@"\n')
    (nc.bin / tool).chmod(0o755)
    run = nc.run("reconcile")
    assert run.returncode == 1, run.stdout
    assert "missing=" not in run.stdout
    assert run.stderr.strip()


def test_reconcile_fails_when_the_database_lists_no_object_backed_file(nc):
    _indexed(nc, [], [3, 4])
    run = nc.run("reconcile")
    assert run.returncode == 1
    assert "missing=" not in run.stdout
    assert "lists no object-backed file" in run.stderr


def test_reconcile_fails_when_nextcloud_never_answers(nc):
    nc.state["ready"] = False
    run = nc.run("reconcile")
    assert run.returncode == 1
    assert "did not answer within 10s" in run.stderr
    assert "missing=" not in run.stdout


def test_reconcile_of_an_instance_on_disk_has_no_object_store_to_count(nc):
    _indexed(nc, [3], [3])
    run = nc.run("reconcile", S3_BUCKET="")
    assert run.returncode == 1
    assert "keeps its files on disk" in run.stderr
    assert "missing=" not in run.stdout
    assert not any(c.startswith("files:object:list") for c in nc.calls())


# the boot probe

def test_the_probe_starts_the_app_service_and_asks_for_an_installed_instance():
    entry = _entry()
    probe = entry.block["probe"]
    assert probe["service"] in _compose()["services"]
    assert probe["memory"] == "768m" and probe["timeout_seconds"] == 180
    assert probe["check"][:2] == ["php", "-r"]
    assert "status.php" in probe["check"][2]
    assert "->installed === true" in probe["check"][2] and "->maintenance === false" in probe["check"][2]
    assert quiesce_lint.lint_probe(entry) == []


def test_the_block_declares_the_reconcile_hook():
    entry = _entry()
    assert entry.block["hooks"]["reconcile"]


# talk-hpb

def _talk_hpb() -> dict:
    return _compose()["services"]["talk-hpb"]


def _start_wrapper(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """The wrapper of talk-hpb run against a /start.sh that prints the secret
    and the arguments it gets."""
    service = _talk_hpb()
    assert service["entrypoint"][:2] == ["/bin/sh", "-ec"]
    tmp_path.mkdir(parents=True, exist_ok=True)
    start = tmp_path / "start.sh"
    start.write_text('#!/bin/sh\nprintf "secret=%s\\n" "$TURN_SECRET"\nprintf "arg=%s\\n" "$@"\n')
    start.chmod(0o755)
    body = service["entrypoint"][2].replace("$$", "$").replace("/start.sh", str(start))
    argv = ["sh", "-ec", body, *service["entrypoint"][3:], *service["command"]]
    return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ["PATH"], **env})


def test_the_talk_backend_takes_the_relay_the_env_gives_and_keeps_the_images_command():
    service = _talk_hpb()
    env = service["environment"]
    assert env["TURN_DOMAIN"] == "${TURN_HOSTNAME:-}"
    assert env["TALK_PORT"] == "${TURN_PORT:-3478}"
    assert env["TURN_SECRET"] == "${TURN_SECRET:-}"
    assert service["command"] == ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]
    assert "TURN_STATIC_AUTH_SECRET" not in yaml.safe_dump(_compose())


def test_the_talk_backend_starts_with_the_secret_of_the_relay(tmp_path):
    run = _start_wrapper(tmp_path, TURN_SECRET="from-the-relay")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[0] == "secret=from-the-relay"
    assert run.stdout.splitlines()[1:] == ["arg=dinit", "arg=--system", "arg=--container", "arg=nats-server",
                                           "arg=eturnal", "arg=janus", "arg=signaling"]


def test_the_talk_backend_mints_a_secret_when_there_is_no_relay(tmp_path):
    secrets = set()
    for n in range(2):
        run = _start_wrapper(tmp_path / str(n), TURN_SECRET="")
        assert run.returncode == 0, run.stderr
        secrets.add(run.stdout.splitlines()[0])
    assert len(secrets) == 2
    assert all(len(s) > len("secret=") + 32 for s in secrets)
