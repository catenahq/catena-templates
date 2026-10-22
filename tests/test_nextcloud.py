"""Nextcloud's own declarations: the configure and verify hooks run against a
stand-in for php (occ), curl, getent and sleep that keeps the state of an
instance in a file, and the declarations the hooks rely on."""
from __future__ import annotations

import json
import os
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
    "TURN_HOSTNAME": "turn.example.test", "TURN_STATIC_AUTH_SECRET": "turn-secret",
    "WEBMAIL_HOSTNAME": "webmail.example.test",
}

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
    up = state["signaling_up"] if "signaling:8081" in url else state["s3_up"]
    sys.exit(0 if up else 7)

# php
if not argv:
    sys.stdin.read()
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
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"configure", "verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []
    assert quiesce_lint.lint_block(_entry()) == []


@pytest.mark.parametrize("name", ["configure", "verify"])
def test_each_body_parses_in_sh(name):
    subprocess.run(["sh", "-n", "-c", _body(name)], check=True)


def test_the_values_the_hooks_read_come_from_the_env_file_not_the_container():
    env = _entry().block["env"]
    app_env = _compose()["services"]["app"]["environment"]
    for key in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"):
        assert key not in env  # the settings sync is their one writer
    for key in ("SIGNALING_HOSTNAME", "SIGNALING_SECRET", "TURN_HOSTNAME", "TURN_STATIC_AUTH_SECRET"):
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
            "store": {"user_oidc": "8.11.3", "mail": "5.0", "files_antivirus": "6.0", "spreed": "21.0",
                      "external": "5.4"},
            "ready": True, "signaling_up": True, "roundcube": True, "s3_up": True, "db_rc": 1,
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
        assert "WAIT=900" in _body(name) or name == "verify"
        body = (_body(name).replace("/var/www/html", str(self.root))
                .replace("/usr/src/nextcloud", str(self.image))
                .replace("WAIT=900", "WAIT=10"))
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


def test_talk_is_given_the_relay_and_the_signaling_server(nc):
    assert nc.run("configure").returncode == 0
    calls = nc.calls()
    assert "talk:turn:add turn.example.test:5349 udp,tcp --secret=turn-secret" in calls
    assert "talk:stun:add turn.example.test:3478" in calls
    assert "talk:signaling:add https://signaling.example.test sig-secret" in calls


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


def test_talk_is_installed_but_not_wired_while_the_signaling_server_is_down(nc):
    nc.state["signaling_up"] = False
    run = nc.run("configure")
    assert run.returncode == 0, run.stderr
    assert nc.state["apps"]["spreed"] == "enabled"
    assert not any(c.startswith("talk:") for c in nc.calls())


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
    assert "the Talk STUN entry failed" in run.stderr
    assert nc.state["appconfig"]["spreed/external_signaling_only"] == "yes"  # later steps still ran


def test_the_antivirus_settings_are_not_marked_done_when_one_fails(nc):
    nc.state["fail"] = "config:app:set files_antivirus av_port"
    run = nc.run("configure")
    assert run.returncode == 1
    assert "catena/antivirus_wired" not in nc.state["appconfig"]


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
