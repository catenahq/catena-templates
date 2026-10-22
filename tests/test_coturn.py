"""The call relay's own declarations: the block, the configuration its start
script writes (with the secret kept out of every command), its hooks run with
the tools of its image, and the applications that read what it provides."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

BLUEPRINT = ROOT / "blueprints" / "coturn"
CONF = "/tmp/turnserver.conf"
SECRET = "S3cretS3cretS3cretS3cretS3cretS3cretS3cretS3cretS3cretS3cretS3cretS3cr"
ENV = {
    "TURN_HOSTNAME": "turn.example.test", "TURN_PORT": "3478", "TURN_TLS_PORT": "5349",
    "TURN_EXTERNAL_IP": "203.0.113.7", "TURN_SECRET": SECRET,
}

# Every directive the start script writes, each one accepted by the pinned
# turnserver binary. A directive added or renamed is checked against the
# binary first (`docker run --rm --entrypoint= <image> turnserver --help`
# lists the options a configuration file may hold), then listed here.
DIRECTIVES = {
    "listening-port", "tls-listening-port", "external-ip", "listening-ip", "relay-ip", "min-port", "max-port",
    "use-auth-secret", "static-auth-secret", "realm", "user-quota", "total-quota", "max-bps", "stale-nonce",
    "cert", "pkey", "dtls", "cipher-list", "denied-peer-ip", "no-multicast-peers", "no-tcp-relay", "verbose", "simple-log",
    "no-stdout-log", "log-file",
}
# Options the binary refuses or fails to start on at this release.
REFUSED = {"no-loopback-peers", "no-cli"}

DENIED_PEERS = [
    "0.0.0.0-0.255.255.255", "10.0.0.0-10.255.255.255", "100.64.0.0-100.127.255.255", "127.0.0.0-127.255.255.255",
    "169.254.0.0-169.254.255.255", "172.16.0.0-172.31.255.255", "192.0.0.0-192.0.0.255", "192.0.2.0-192.0.2.255",
    "192.88.99.0-192.88.99.255", "192.168.0.0-192.168.255.255", "198.18.0.0-198.19.255.255",
    "198.51.100.0-198.51.100.255", "203.0.113.0-203.0.113.255", "224.0.0.0-239.255.255.255",
    "240.0.0.0-255.255.255.255", "::1", "64:ff9b::-64:ff9b::ffff:ffff", "100::-100::ffff:ffff:ffff:ffff",
    "2001:db8::-2001:db8:ffff:ffff:ffff:ffff:ffff:ffff", "2002::-2002:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    "fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    "::ffff:0.0.0.0-::ffff:255.255.255.255",
]


def _entry(slug: str = "coturn") -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == slug)


def _compose() -> dict:
    return yaml.safe_load((BLUEPRINT / "docker-compose.yml").read_text(encoding="utf-8"))


def _service() -> dict:
    return _compose()["services"]["coturn"]


def _body(argv: list[str]) -> str:
    return argv[2].replace("$$", "$")


def _hook(name: str) -> list[str]:
    [argv] = _entry().block["hooks"][name]
    return argv


# --- the declaration ----------------------------------------------------------

def test_the_relay_has_no_web_address_and_no_bench_restore_drill():
    """The relay keeps no data, so the bench restore drill has nothing to seed
    or to check after a restore, and the per-app checks cannot install it."""
    catena = _entry().catena
    assert "domain" not in catena
    assert catena["sso_mode"] == "n/a"
    assert catena["bench"]["fixture"] == "skip" and catena["bench"]["pack"] == "nodb"
    assert not (BLUEPRINT / "test.yml").exists()
    assert _entry().block["version_from"] == "coturn"


def test_the_block_asks_for_the_host_network_and_declares_its_ports():
    block = _entry().block
    assert block["privileges"] == ["host_network"]
    assert quiesce_lint.lint_host_network_ports(_entry()) == []
    service = _service()
    assert "ports" not in service
    assert service["networks"] == ["hostnet"]
    assert _compose()["networks"]["hostnet"] == {"external": True, "name": "host"}
    assert service["deploy"]["mode"] == "global"
    assert "node.labels.catena.role==data" in service["deploy"]["placement"]["constraints"]
    labels = {k: v for k, v in (line.split("=", 1) for line in service["labels"])}
    assert labels["vps.expose.udp"] == "3478,5349,50000-50100" and labels["vps.expose.tcp"] == "5349"


def test_the_env_keeps_the_names_and_ports_managed_and_mints_the_secret():
    entry = _entry()
    assert entry.managed_env == {
        "TURN_HOSTNAME": "turn.{{ cloudflare_zone }}", "TURN_PORT": "3478", "TURN_TLS_PORT": "5349",
        "TURN_EXTERNAL_IP": "{{ public_ip }}"}
    assert entry.block["env"]["TURN_SECRET"] == {"generated": {"length": 64, "alphabet": "alnum"}}
    assert "secrets" not in entry.block
    assert entry.block["provides"] == {"turn": {
        "host": "TURN_HOSTNAME", "port": "TURN_PORT", "tls_port": "TURN_TLS_PORT", "secret": "TURN_SECRET"}}


def test_the_ports_of_the_labels_and_the_start_script_agree(tmp_path):
    entry = _entry()
    config = _config_of(tmp_path, {**ENV, "TURN_PORT": entry.managed_env["TURN_PORT"],
                                   "TURN_TLS_PORT": entry.managed_env["TURN_TLS_PORT"]})
    assert config["listening-port"] == [entry.managed_env["TURN_PORT"]]
    assert config["tls-listening-port"] == [entry.managed_env["TURN_TLS_PORT"]]
    labels = _service()["labels"]
    assert f"vps.expose.udp={config['listening-port'][0]},{config['tls-listening-port'][0]}," \
           f"{config['min-port'][0]}-{config['max-port'][0]}" in labels
    assert f"vps.expose.tcp={config['tls-listening-port'][0]}" in labels


def test_the_needs_name_the_certificate_and_the_address_record():
    needs = _entry().block["needs"]
    assert needs["certificate"] == {"names": ["{{ turn_hostname }}"], "volume": "certs"}
    assert needs["dns"] == [{"type": "A", "name": "{{ turn_hostname }}", "value": "{{ public_ip }}"}]
    assert _service()["volumes"] == ["certs:/certs:ro"] and "certs" in _compose()["volumes"]


def test_the_image_pin_runs_as_its_own_user():
    """coturn/coturn:4.18.0-alpine sets USER nobody:nogroup; the certificate and
    its key are readable by that user, so the service sets no user."""
    service = _service()
    assert service["image"] == "coturn/coturn:4.18.0-alpine"
    assert "user" not in service and "privileged" not in service
    assert service["cap_drop"] == ["NET_RAW"]


def test_the_hooks_run_in_the_relay_and_pass_the_lint():
    hooks = _entry().block["hooks"]
    assert hooks["service"] == "coturn" and "user" not in hooks
    assert set(hooks) - {"service", "timeout_seconds"} == {"configure", "verify"}
    assert quiesce_lint.lint_hooks(_entry()) == []
    assert quiesce_lint.lint_block(_entry()) == []
    for name in ("configure", "verify"):
        subprocess.run(["sh", "-n", "-c", _body(_hook(name))], check=True)


def test_what_the_relay_provides_is_all_the_readers_ask_for():
    entries = model.load_sources()
    assert quiesce_lint.lint_provided(entries) == []
    readers = {e.slug: {name for expr in e.managed_env.values()
                        for capability, name in quiesce_lint.provided_reads(expr) if capability == "turn"}
               for e in entries}
    assert {slug: names for slug, names in readers.items() if names} == {
        "element": {"host", "port", "secret", "tls_port"},
        "nextcloud-s3-oidc": {"host", "port", "secret", "tls_port"}}


# --- the start script ---------------------------------------------------------

def _config_of(tmp_path: Path, env: dict[str, str]) -> dict[str, list[str]]:
    """The directives the start script writes for `env`, by name."""
    run = _start(tmp_path, env)
    assert run.returncode == 0, run.stderr
    config: dict[str, list[str]] = {}
    for line in (tmp_path / "turnserver.conf").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        config.setdefault(key, []).append(value)
    return config


def _start(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """The start script with the configuration in tmp_path and turnserver
    standing in as a program that records its arguments."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "turnserver"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/argv\n', encoding="utf-8")
    stub.chmod(0o755)
    entrypoint = _service()["entrypoint"]
    assert entrypoint[:2] == ["/bin/bash", "-ec"]
    body = _body(entrypoint).replace(CONF, str(tmp_path / "turnserver.conf"))
    return subprocess.run(["bash", "-ec", body], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:{os.environ['PATH']}", **env})


def test_the_start_script_writes_the_configuration_from_the_environment(tmp_path):
    config = _config_of(tmp_path, ENV)
    assert config["listening-port"] == ["3478"] and config["tls-listening-port"] == ["5349"]
    assert config["external-ip"] == ["203.0.113.7"] and config["realm"] == ["turn.example.test"]
    assert config["static-auth-secret"] == [SECRET]
    assert config["min-port"] == ["50000"] and config["max-port"] == ["50100"]
    assert config["cert"] == ["/certs/fullchain.pem"] and config["pkey"] == ["/certs/privkey.pem"]
    assert config["user-quota"] == ["12"] and config["total-quota"] == ["1200"]
    assert config["max-bps"] == ["3000000"] and config["stale-nonce"] == ["600"]
    assert config["cipher-list"] == [
        '"ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:'
        'ECDHE-RSA-AES128-GCM-SHA256"']
    for flag in ("use-auth-secret", "dtls", "no-multicast-peers", "no-tcp-relay"):
        assert config[flag] == [""]


def test_the_configuration_refuses_every_special_purpose_range(tmp_path):
    assert _config_of(tmp_path, ENV)["denied-peer-ip"] == DENIED_PEERS


def test_the_configuration_holds_only_directives_the_pinned_binary_lists(tmp_path):
    config = _config_of(tmp_path, ENV)
    assert set(config) == DIRECTIVES
    assert not set(config) & REFUSED


def test_the_secret_is_in_the_file_and_never_in_a_command(tmp_path):
    run = _start(tmp_path, ENV)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "argv").read_text().split() == ["-c", str(tmp_path / "turnserver.conf")]
    assert SECRET not in run.stdout + run.stderr
    assert (tmp_path / "turnserver.conf").stat().st_mode & 0o777 == 0o600
    service = _service()
    assert "command" not in service
    for part in service["entrypoint"]:
        assert not re.search(r"(?<!\$)\$\{?TURN_", part), part
    assert "TURN_SECRET" not in "".join(service["labels"])


def test_the_start_script_ends_in_turnserver_so_it_receives_the_signals():
    assert _body(_service()["entrypoint"]).rstrip().endswith('exec turnserver -c "$conf"')


# --- the hooks ----------------------------------------------------------------

APPLETS = ("awk", "base64", "cat", "cut", "date", "grep", "sed", "sha256sum")


def _tools(tmp_path: Path) -> tuple[Path, list[str]]:
    """A bin directory with the tools of the image's busybox (the host's own
    where there is no busybox), and the shell that runs the hooks."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    busybox = shutil.which("busybox")
    for applet in APPLETS:
        target = busybox or shutil.which(applet)
        link = bin_dir / applet
        if not link.exists():
            link.symlink_to(target)
    return bin_dir, [busybox, "sh"] if busybox else ["sh"]


def _run_hook(tmp_path: Path, name: str, replace: dict[str, str], env: dict[str, str]) -> subprocess.CompletedProcess:
    bin_dir, shell = _tools(tmp_path)
    body = _body(_hook(name))
    for old, new in replace.items():
        assert old in body, old
        body = body.replace(old, new)
    return subprocess.run([*shell, "-c", body], capture_output=True, text=True, check=False,
                          env={"PATH": str(bin_dir), **env})


def _certificate(path: Path, days: int) -> None:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(path) + ".key", "-out", str(path), "-days", str(days), "-subj", "/CN=turn.example.test"],
                   check=True, capture_output=True)


def _sockets(proc: Path, *, udp=(3478, 5349), tcp=(5349,), state="0A") -> None:
    """The /proc/net tables of a host that listens on these ports."""
    proc.mkdir(parents=True, exist_ok=True)
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    row = "  {n}: 00000000:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000     0        0 1\n"
    (proc / "udp").write_text(head + "".join(row.format(n=i, port=p, state="07") for i, p in enumerate(udp)))
    (proc / "tcp").write_text(head + "".join(row.format(n=i, port=p, state=state) for i, p in enumerate(tcp)))
    (proc / "udp6").write_text(head)
    (proc / "tcp6").write_text(head)


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


def _verify(tmp_path: Path, cert: Path, **sockets) -> subprocess.CompletedProcess:
    _sockets(tmp_path / "proc", **sockets)
    return _run_hook(tmp_path, "verify", {"/proc/net/": str(tmp_path / "proc") + "/", "/certs/fullchain.pem": str(cert)},
                     {"TURN_PORT": "3478", "TURN_TLS_PORT": "5349"})


@needs_openssl
def test_verify_passes_with_the_ports_open_and_a_certificate_valid_for_a_day(tmp_path):
    cert = tmp_path / "fullchain.pem"
    _certificate(cert, 30)
    run = _verify(tmp_path, cert)
    assert run.returncode == 0, run.stderr
    assert "certificate is valid for 24 hours" in run.stdout


@needs_openssl
def test_verify_names_a_port_that_does_not_listen(tmp_path):
    cert = tmp_path / "fullchain.pem"
    _certificate(cert, 30)
    run = _verify(tmp_path, cert, udp=(3478,))
    assert run.returncode == 1 and "udp-5349-not-listening" in run.stderr
    run = _verify(tmp_path / "b", cert, tcp=())
    assert run.returncode == 1 and "tcp-5349-not-listening" in run.stderr
    run = _verify(tmp_path / "c", cert, tcp=(5349,), state="01")
    assert run.returncode == 1 and "tcp-5349-not-listening" in run.stderr


@needs_openssl
def test_verify_fails_a_certificate_that_ends_within_a_day(tmp_path):
    cert = tmp_path / "fullchain.pem"
    _certificate(cert, 1)
    run = _verify(tmp_path, cert)
    assert run.returncode == 1 and "certificate-expires-within-24h" in run.stderr


@needs_openssl
def test_verify_reads_the_first_certificate_of_the_chain(tmp_path):
    short, long_ = tmp_path / "short.pem", tmp_path / "long.pem"
    _certificate(short, 1)
    _certificate(long_, 90)
    leaf_short = tmp_path / "a" / "fullchain.pem"
    leaf_short.parent.mkdir()
    leaf_short.write_text(short.read_text() + long_.read_text())
    assert "certificate-expires-within-24h" in _verify(tmp_path / "a", leaf_short).stderr
    leaf_long = tmp_path / "b" / "fullchain.pem"
    leaf_long.parent.mkdir()
    leaf_long.write_text(long_.read_text() + short.read_text())
    assert _verify(tmp_path / "b", leaf_long).returncode == 0


def test_verify_names_a_missing_certificate(tmp_path):
    run = _verify(tmp_path, tmp_path / "absent.pem")
    assert run.returncode == 1 and "certificate-missing" in run.stderr


def test_configure_does_not_mark_a_certificate_loaded_when_the_signal_fails(tmp_path):
    cert, mark = tmp_path / "fullchain.pem", tmp_path / "mark"
    cert.write_text("first")
    run = _run_hook(tmp_path, "configure", {"/certs/fullchain.pem": str(cert), "/tmp/.catena-cert.sha256": str(mark),
                                            "kill -USR2 1": "false"}, {})
    assert run.returncode == 1 and not mark.exists()


def test_configure_signals_turnserver_only_when_the_certificate_changed(tmp_path):
    body = _body(_hook("configure"))
    assert "kill -USR2 1 || exit 1\n" in body
    cert, mark, log = tmp_path / "fullchain.pem", tmp_path / "mark", tmp_path / "log"
    replace = {"/certs/fullchain.pem": str(cert), "/tmp/.catena-cert.sha256": str(mark),
               "kill -USR2 1": f"echo USR2 >> {log}"}

    def configure():
        return _run_hook(tmp_path, "configure", replace, {})

    assert configure().returncode == 0 and not log.exists()
    cert.write_text("first")
    for _ in range(2):
        assert configure().returncode == 0
    assert log.read_text().splitlines() == ["USR2"]
    cert.write_text("renewed")
    assert configure().returncode == 0
    assert log.read_text().splitlines() == ["USR2", "USR2"]
