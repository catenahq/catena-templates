"""The mail server's own declarations: the start script of dms and its hooks run
against stand-ins for the commands of the image, the needs it declares and the
sign-in labels of the webmail."""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

BLUEPRINT = ROOT / "blueprints" / "mailserver"
CONFIG = "/tmp/docker-mailserver"
STATE = CONFIG + "/.catena-mailboxes.json"


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == "mailserver")


def _compose() -> dict:
    return yaml.safe_load((BLUEPRINT / "docker-compose.yml").read_text(encoding="utf-8"))


def _body(argv: list[str]) -> str:
    return argv[-1].replace("$$", "$")


def _hook(name: str) -> list[str]:
    [argv] = _entry().block["hooks"][name]
    return argv


def _command(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_the_hooks_run_in_dms_and_pass_the_lint():
    hooks = _entry().block["hooks"]
    assert hooks["service"] == "dms" and "dms" in _compose()["services"]
    assert set(hooks) - {"service", "timeout_seconds", "users_group"} == {"configure", "verify", "users"}
    assert hooks["users_group"] == "staff,client"
    assert quiesce_lint.lint_hooks(_entry()) == []


def test_every_hook_body_parses():
    subprocess.run(["sh", "-n", "-c", _body(_hook("configure"))], check=True)
    assert _hook("verify")[:2] == ["bash", "-c"]
    subprocess.run(["bash", "-n", "-c", _body(_hook("verify"))], check=True)
    argv = _hook("users")
    assert argv[:2] == ["python3", "-c"]
    compile(_body(argv), "users", "exec")
    subprocess.run(["bash", "-n", "-c", _body(_compose()["services"]["dms"]["entrypoint"])], check=True)


def test_the_mail_ports_stay_published_on_the_host_and_in_the_registry():
    dms = _compose()["services"]["dms"]
    assert [(p["published"], p["mode"]) for p in dms["ports"]] == [(25, "host"), (465, "host"), (587, "host"), (993, "host")]
    assert "vps.expose.tcp=25,465,587,993" in dms["labels"]


def test_the_needs_name_the_certificate_and_the_three_records():
    needs = _entry().block["needs"]
    assert needs["certificate"] == {"names": ["{{ mail_hostname }}"], "volume": "mail-certs"}
    assert "mail-certs" in _compose()["volumes"]
    assert [(r["type"], r["name"]) for r in needs["dns"]] == [
        ("A", "{{ mail_hostname }}"), ("TXT", "_mta-sts.{{ mail_domain }}"), ("TXT", "_smtp._tls.{{ mail_domain }}")]
    assert needs["dns"][0]["value"] == "{{ public_ip }}"
    assert "MX" not in {r["type"] for r in needs["dns"]}


def test_the_relay_password_and_the_spamhaus_key_are_settings_of_the_app_with_both_labels():
    block = _entry().block
    for key, label in (("RELAY_PASSWORD", "Relay password"), ("SPAMHAUS_DQS_KEY", "Spamhaus Data Query Service key")):
        setting = block["env"][key]["setting"]
        assert setting["default"] == "" and setting["label"]["en"] == label and setting["label"]["fr"]
    assert "secrets" not in block
    assert not {k for k in _entry().managed_env if k in ("RELAY_PASSWORD", "SPAMHAUS_DQS_KEY")}


def test_the_webmail_signs_in_with_its_own_client_that_dovecot_introspects_with():
    labels = _compose()["services"]["roundcube"]["labels"]
    for label in ("vps.route.main=true", "vps.auth.oidc=true", "vps.auth.oidc.introspect=true",
                  "vps.auth.groups=staff,client",
                  "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/index.php/login/oauth"):
        assert label in labels
    for service in ("dms", "roundcube"):
        env = _compose()["services"][service]["environment"]
        assert {"OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"} <= set(env)
    assert not {k for k in _entry().env if k.startswith("OIDC_CLIENT") or k == "OIDC_ISSUER_URL"}


def test_the_webmail_sends_keycloak_its_https_return_address():
    body = _body(_compose()["services"]["roundcube"]["entrypoint"])
    sign_in = body.split("> /var/roundcube/config/oauth.inc.php <<'EOF'\n")[1].split("\nEOF\n")[0]
    block = sign_in[sign_in.index("if ("):]
    assert "$config['use_https'] = true;" in block
    assert "force_https" not in body
    labels = _compose()["services"]["roundcube"]["labels"]
    assert "vps.auth.oidc.redirect_uris=https://${DOMAIN_HOST}/index.php/login/oauth" in labels


# --- the start script of dms ------------------------------------------------

def _start(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """The dms start script with the config directory in tmp_path, doveadm and
    the image's own start standing in as stubs."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _command(bin_dir / "doveadm", 'echo "{SHA512-CRYPT}hashed-$5"\n')
    body = _body(_compose()["services"]["dms"]["entrypoint"])
    body = body.replace("cfg=" + CONFIG, "cfg=" + str(cfg))
    body = body.replace("exec /usr/bin/dumb-init -- supervisord", "echo started supervisord")
    return subprocess.run(["bash", "-ec", body], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "MAIL_DOMAIN": "a.example", **env})


def test_the_start_makes_the_postmaster_mailbox_and_the_abuse_alias_once(tmp_path):
    for _ in range(2):
        out = _start(tmp_path)
        assert out.returncode == 0, out.stderr
        assert "started supervisord" in out.stdout
    accounts = (tmp_path / "cfg" / "postfix-accounts.cf").read_text().splitlines()
    assert len(accounts) == 1 and accounts[0].startswith("postmaster@a.example|{SHA512-CRYPT}hashed-")
    assert (tmp_path / "cfg" / "postfix-virtual.cf").read_text().splitlines() == ["abuse@a.example postmaster@a.example"]


def test_the_start_keeps_the_mailboxes_it_finds_and_ends_a_line_it_extends(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "postfix-accounts.cf").write_text("ana@a.example|{SHA512-CRYPT}x")
    assert _start(tmp_path).returncode == 0
    assert (cfg / "postfix-accounts.cf").read_text().splitlines()[0] == "ana@a.example|{SHA512-CRYPT}x"
    assert len((cfg / "postfix-accounts.cf").read_text().splitlines()) == 2


def test_the_dovecot_login_is_written_only_when_the_sign_in_values_are_there(tmp_path):
    conf = tmp_path / "cfg" / "dovecot-oauth2.conf.ext"
    assert _start(tmp_path).returncode == 0 and not conf.exists()
    out = _start(tmp_path, OIDC_CLIENT_ID="app-catena-mailserver", OIDC_CLIENT_SECRET="s3cret",
                 OIDC_ISSUER_URL="https://auth.a.example/realms/vps/")
    assert out.returncode == 0, out.stderr
    assert ("introspection_url = https://app-catena-mailserver:s3cret@auth.a.example/realms/vps"
            "/protocol/openid-connect/token/introspect") in conf.read_text()
    assert stat.S_IMODE(conf.stat().st_mode) == 0o640
    assert _start(tmp_path).returncode == 0 and not conf.exists()


SCANNER = {"ANTIVIRUS_HOST": "scan", "ANTIVIRUS_PORT": "3310"}


def test_the_antivirus_rule_exists_only_with_a_scanner_and_names_its_address(tmp_path):
    local = tmp_path / "cfg" / "rspamd" / "local.d"
    assert _start(tmp_path, **SCANNER).returncode == 0
    assert (local / "force_actions.conf").is_file()
    assert 'servers = "scan:3310";' in (local / "antivirus.conf").read_text()
    assert _start(tmp_path).returncode == 0
    assert sorted(p.name for p in local.iterdir()) == ["actions.conf"]


def test_the_blocklist_file_exists_only_with_a_key_and_carries_it(tmp_path):
    local = tmp_path / "cfg" / "rspamd" / "local.d"
    assert _start(tmp_path, **SCANNER).returncode == 0
    assert sorted(p.name for p in local.iterdir()) == ["actions.conf", "antivirus.conf", "force_actions.conf"]
    assert _start(tmp_path, SPAMHAUS_DQS_KEY="k3y").returncode == 0
    rbl = (local / "rbl.conf").read_text()
    assert "k3y.zen.dq.spamhaus.net" in rbl and "__DQS_KEY__" not in rbl
    assert '$LOCAL_CONFDIR/local.d/sh_rbl_hbl.conf' in rbl
    assert _start(tmp_path).returncode == 0 and not (local / "rbl.conf").exists()


def test_the_start_hook_puts_the_settings_where_rspamd_reads_them(tmp_path):
    assert _start(tmp_path, **SCANNER).returncode == 0
    hook = (tmp_path / "cfg" / "user-patches.sh").read_text()
    assert "cp /tmp/docker-mailserver/rspamd/local.d/*.conf /etc/rspamd/local.d/" in hook
    assert 'getent hosts "$ANTIVIRUS_HOST"' in hook
    assert (tmp_path / "cfg" / "user-patches.sh").stat().st_mode & stat.S_IXUSR


# --- the hooks --------------------------------------------------------------

def _run_sh(tmp_path: Path, body: str, env: dict[str, str] | None = None,
            **commands: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, script in commands.items():
        _command(bin_dir / name, script)
    return subprocess.run(["sh", "-c", body], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:{os.environ['PATH']}", **(env or {})})


def test_configure_reloads_the_mail_daemons_only_when_the_certificate_changed(tmp_path):
    cert = tmp_path / "fullchain.pem"
    mark = tmp_path / "mark"
    log = tmp_path / "log"
    body = _body(_hook("configure")).replace("/certs/fullchain.pem", str(cert)).replace(
        "/tmp/docker-mailserver/.catena-cert.sha256", str(mark))
    stubs = {"postfix": f'echo postfix "$@" >> {log}\n', "doveadm": f'echo doveadm "$@" >> {log}\n'}
    assert _run_sh(tmp_path, body, **stubs).returncode == 0 and not log.exists()
    cert.write_text("first")
    for _ in range(2):
        assert _run_sh(tmp_path, body, **stubs).returncode == 0
    assert log.read_text().splitlines() == ["postfix reload", "doveadm reload"]
    cert.write_text("renewed")
    assert _run_sh(tmp_path, body, **stubs).returncode == 0
    assert len(log.read_text().splitlines()) == 4


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


def _serve(handler, context: ssl.SSLContext | None = None) -> int:
    """A server on a free loopback port that hands each connection to `handler`."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                if context:
                    conn = context.wrap_socket(conn, server_side=True)
                conn.settimeout(5)
                handler(conn)
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return listener.getsockname()[1]


def _smtp(banner: bytes = b"220 mail.example.test ESMTP\r\n", ehlo: bytes = b"250-mail.example.test\r\n250 SIZE 1\r\n"):
    def handler(conn: socket.socket) -> None:
        conn.sendall(banner)
        if conn.recv(100).startswith(b"EHLO"):
            conn.sendall(ehlo)
            conn.recv(100)
    return handler


def _imap(greeting: bytes = b"* OK [CAPABILITY IMAP4rev1] Dovecot ready.\r\n"):
    def handler(conn: socket.socket) -> None:
        conn.sendall(greeting)
        conn.recv(100)
    return handler


def _tls_context(tmp_path: Path) -> ssl.SSLContext:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=mail.example.test"],
                   check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def _verify(tmp_path: Path, smtp_port: int, imap_port: int, path: str | None = None, **env: str) -> subprocess.CompletedProcess:
    """The verify hook with its two ports pointed at stand-ins for Postfix and Dovecot."""
    body = _body(_hook("verify"))
    for old, new in (("/dev/tcp/127.0.0.1/25", f"/dev/tcp/127.0.0.1/{smtp_port}"),
                     ("127.0.0.1:993", f"127.0.0.1:{imap_port}")):
        assert old in body, old
        body = body.replace(old, new)
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False,
                          env={"PATH": path or os.environ["PATH"], **env})


@needs_openssl
def test_verify_passes_when_postfix_answers_its_banner_and_ehlo_and_dovecot_its_greeting(tmp_path):
    run = _verify(tmp_path, _serve(_smtp()), _serve(_imap(), _tls_context(tmp_path)))
    assert run.returncode == 0, run.stderr
    assert run.stdout == "SMTP on 25 answers its banner and EHLO and IMAP on 993 its greeting\n"


@needs_openssl
def test_verify_names_each_server_that_does_not_answer(tmp_path):
    context = _tls_context(tmp_path)
    good_smtp, good_imap = _serve(_smtp()), _serve(_imap(), context)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    cases = [
        ((closed_port, good_imap), "smtp-25-does-not-answer"),
        ((_serve(_smtp(banner=b"421 go away\r\n")), good_imap), "smtp-25-sends-no-banner"),
        ((_serve(_smtp(ehlo=b"500 what\r\n")), good_imap), "smtp-25-refuses-ehlo"),
        ((good_smtp, closed_port), "imap-993-sends-no-greeting"),
        ((good_smtp, _serve(_imap(b"* BYE no\r\n"), context)), "imap-993-sends-no-greeting"),
    ]
    for (smtp_port, imap_port), needle in cases:
        run = _verify(tmp_path, smtp_port, imap_port)
        assert run.returncode == 1 and needle in run.stderr, (needle, run.stderr)


@needs_openssl
def test_verify_asks_rspamd_for_the_test_file_only_while_a_scanner_is_provided(tmp_path):
    smtp_port, imap_port = _serve(_smtp()), _serve(_imap(), _tls_context(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = f"{bin_dir}:{os.environ['PATH']}"
    _command(bin_dir / "rspamc", 'cat >/dev/null\necho "Symbol: CLAM_VIRUS (1.00)"\n')
    ok = _verify(tmp_path, smtp_port, imap_port, path, **SCANNER)
    assert ok.returncode == 0 and "detects the EICAR" in ok.stdout
    _command(bin_dir / "rspamc", "cat >/dev/null\necho clean\n")
    no_virus = _verify(tmp_path, smtp_port, imap_port, path, **SCANNER)
    assert no_virus.returncode == 1 and "eicar-not-detected" in no_virus.stderr
    assert _verify(tmp_path, smtp_port, imap_port, path).returncode == 0


class _Mailboxes:
    """A fake `setup`: the mailbox list in a file, the calls in another."""

    def __init__(self, tmp_path: Path, present: list[str], *, list_rc: int = 0):
        self.state = tmp_path / "mailboxes.json"
        self.calls = tmp_path / "calls"
        self.listing = tmp_path / "listing"
        self.listing.write_text("".join(f"* {a} ( 0 / ~ ) [0%]\n" for a in present))
        self.script = (
            f'echo "$@" >> {self.calls}\n'
            f'case "$1 $2" in\n'
            f'  "email list") cat {self.listing}; exit {list_rc} ;;\n'
            f'  "email add") echo "* $3" >> {self.listing} ;;\n'
            f'  "email del") grep -v -F -- "$4" {self.listing} > {self.listing}.new; mv {self.listing}.new {self.listing} ;;\n'
            f'esac\n')
        self.tmp = tmp_path

    def run(self, members: list[dict], domain: str | None = "a.example") -> subprocess.CompletedProcess:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        _command(bin_dir / "setup", self.script)
        body = _body(_hook("users")).replace(STATE, str(self.state))
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        if domain is not None:
            env["MAIL_DOMAIN"] = domain
        return subprocess.run([sys.executable, "-c", body], input=json.dumps(members), capture_output=True,
                              text=True, check=False, env=env)

    def changes(self) -> list[str]:
        """The adds (without the password) and the removals, in order."""
        if not self.calls.exists():
            return []
        out = []
        for call in self.calls.read_text().splitlines():
            words = call.split()
            if call.startswith("email add"):
                out.append(" ".join(words[:3]))
            elif call.startswith("email del"):
                out.append(" ".join(words[:4]))
        return out


def _members(*emails: str) -> list[dict]:
    return [{"username": e.split("@")[0], "email": e, "enabled": True} for e in emails]


def test_the_users_hook_adds_the_missing_mailboxes_and_records_them(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example"])
    first = box.run(_members("ana@a.example"))
    assert first.returncode == 0, first.stderr
    assert box.changes() == ["email add ana@a.example"]
    assert json.loads(box.state.read_text()) == ["ana@a.example"]
    out = box.run(_members("ana@a.example", "bob@a.example"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email add ana@a.example", "email add bob@a.example"]
    assert json.loads(box.state.read_text()) == ["ana@a.example", "bob@a.example"]


def test_the_users_hook_takes_the_mailboxes_already_there_for_its_own_only_after_a_record_exists(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example", "ana@a.example", "left@a.example"])
    out = box.run(_members("ana@a.example"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == [] and "left@a.example" in box.listing.read_text()
    assert json.loads(box.state.read_text()) == ["ana@a.example"]


def test_the_users_hook_removes_only_what_it_made_and_never_postmaster(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example", "info@a.example", "gone@a.example", "ana@a.example"])
    box.state.write_text(json.dumps(["gone@a.example", "ana@a.example", "postmaster@a.example"]))
    out = box.run(_members("ana@a.example"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email del -y gone@a.example"]
    assert json.loads(box.state.read_text()) == ["ana@a.example", "postmaster@a.example"]
    assert "info@a.example" in box.listing.read_text()


def test_the_users_hook_makes_no_mailbox_for_an_address_outside_the_mail_domain(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example"])
    out = box.run(_members("ana@a.example", "cli@client.example", "x@sub.a.example", "y@a.example.evil"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email add ana@a.example"]
    assert json.loads(box.state.read_text()) == ["ana@a.example"]


def test_the_users_hook_removes_a_mailbox_it_made_outside_the_mail_domain(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example", "ana@a.example", "cli@client.example", "mine@other.example"])
    box.state.write_text(json.dumps(["ana@a.example", "cli@client.example"]))
    out = box.run(_members("ana@a.example", "cli@client.example"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email del -y cli@client.example"]
    assert json.loads(box.state.read_text()) == ["ana@a.example"]
    assert "mine@other.example" in box.listing.read_text()


def test_the_users_hook_removes_outside_mailboxes_when_every_member_is_outside(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example", "cli@client.example"])
    box.state.write_text(json.dumps(["cli@client.example", "postmaster@a.example"]))
    out = box.run(_members("cli@client.example"))
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email del -y cli@client.example"]


def test_the_users_hook_compares_the_mail_domain_without_regard_to_case(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example"])
    out = box.run(_members("Ana@A.Example", "bob@b.example"), domain="A.EXAMPLE")
    assert out.returncode == 0, out.stderr
    assert box.changes() == ["email add ana@a.example"]


def test_the_users_hook_changes_nothing_without_a_mail_domain(tmp_path):
    box = _Mailboxes(tmp_path, ["postmaster@a.example"])
    out = box.run(_members("ana@a.example"), domain=None)
    assert out.returncode == 1 and "MAIL_DOMAIN" in out.stderr
    assert box.changes() == []


def test_the_users_hook_changes_nothing_for_an_empty_list_or_a_listing_that_failed(tmp_path):
    box = _Mailboxes(tmp_path, ["gone@a.example"])
    box.state.write_text(json.dumps(["gone@a.example"]))
    assert box.run(_members()).returncode == 0
    assert box.run([{"username": "x", "email": ""}]).returncode == 0
    assert box.changes() == []
    failing = _Mailboxes(tmp_path, ["gone@a.example"], list_rc=1)
    out = failing.run(_members("ana@a.example"))
    assert out.returncode == 1 and "could not be listed" in out.stderr
    assert failing.changes() == []
