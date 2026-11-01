"""The Beszel configure hook (blueprints/beszel/hooks/configure.py) sets the hub up through
its API: the sign-in with Catena beside the password path, the alert delivery to the
relay, the default rules, the outgoing mail, the admin email change and the public key
the agent reads."""
from __future__ import annotations

import json
import re
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = ROOT / "blueprints" / "beszel"
HOOK = BLUEPRINT / "hooks" / "configure.py"

# The hook's folder is the app's: loading it leaves no bytecode there.
hook = types.ModuleType("beszel_configure")
hook.__file__ = str(HOOK)
exec(compile(HOOK.read_text(encoding="utf-8"), str(HOOK), "exec"), hook.__dict__)

ISSUER = "https://auth.example.test/realms/vps"
OIDC = {"issuer": ISSUER, "secret": "s"}


def _provider(secret="s", issuer=ISSUER):
    return {"name": "oidc", "clientId": "beszel", "clientSecret": secret,
            "authURL": issuer + "/protocol/openid-connect/auth",
            "tokenURL": issuer + "/protocol/openid-connect/token",
            "userInfoURL": issuer + "/protocol/openid-connect/userinfo", "displayName": "Catena"}


# --- the password path stays ---

def test_password_login_is_never_disabled():
    """DISABLE_PASSWORD_AUTH would make the resource monitor unreachable exactly when the
    sign-in tool is the thing that is broken. An assignment is refused, not a mention."""
    assigned = re.compile(r"DISABLE_PASSWORD_AUTH\s*[:=]")
    for path in (BLUEPRINT / "services.yml", BLUEPRINT / "x-catena.yml", HOOK):
        offenders = [ln for ln in path.read_text().splitlines()
                     if assigned.search(ln) and not ln.lstrip().startswith("#")]
        assert not offenders, f"{path.name} sets DISABLE_PASSWORD_AUTH: {offenders}"


def test_the_hook_signs_in_as_superuser():
    """`_superusers` is a different collection from the `users` one the sign-in with
    Catena governs, so enabling it leaves this hook's own sign-in alone."""
    assert "/api/collections/_superusers/auth-with-password" in HOOK.read_text()


# --- the provider ---

def test_the_provider_uses_the_field_names_pocketbase_reads(monkeypatch):
    """PocketBase drops unknown keys instead of refusing them, so the pre-v0.23 spelling
    (authUrl, tokenUrl, userApiUrl) would save cleanly and never complete a login."""
    sent = {}

    def fake(method, path, *, token=None, body=None):
        if method == "PATCH":
            sent["providers"] = body["oauth2"]["providers"]
            return {}
        return {"oauth2": {"enabled": True, "providers": sent.get("providers", [])}}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_oidc("tok", ISSUER, "s") == "configured"
    assert sent["providers"] == [_provider()]
    assert set(sent["providers"][0]) == {"name", "clientId", "clientSecret", "authURL", "tokenURL",
                                         "userInfoURL", "displayName"}
    assert hook.PROVIDER in ("oidc", "oidc2", "oidc3")


def test_an_absent_secret_on_read_is_not_drift_and_a_returned_one_that_differs_is(monkeypatch):
    held = {"p": _provider()}
    writes = []

    def fake(method, path, *, token=None, body=None):
        if method == "PATCH":
            writes.append(body)
            return {}
        return {"oauth2": {"enabled": True, "providers": [held["p"]]}}
    monkeypatch.setattr(hook, "req", fake)
    held["p"] = {k: v for k, v in _provider().items() if k != "clientSecret"}
    assert hook.configure_oidc("tok", ISSUER, "s") == "ok-exists" and writes == []
    held["p"] = _provider(secret="rotated")
    assert hook.configure_oidc("tok", ISSUER, "s") == "updated"
    assert writes, "a secret that differs was not written"


def test_other_providers_are_preserved(monkeypatch):
    state = {"providers": [{"name": "github", "clientId": "gh"}, _provider(issuer="https://old.example/realms/x")]}

    def fake(method, path, *, token=None, body=None):
        if method == "PATCH":
            state["providers"] = body["oauth2"]["providers"]
            return {}
        return {"oauth2": {"enabled": True, "providers": state["providers"]}}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_oidc("tok", ISSUER, "s") == "updated"
    names = [p["name"] for p in state["providers"]]
    assert "github" in names and names.count("oidc") == 1


def test_a_dropped_key_on_read_back_fails_loudly(monkeypatch):
    def fake(method, path, *, token=None, body=None):
        if method == "GET":
            return {"oauth2": {"enabled": True, "providers": [dict(_provider(), authURL="")]}}
        return {}
    monkeypatch.setattr(hook, "req", fake)
    with pytest.raises(SystemExit):
        hook.configure_oidc("tok", ISSUER, "s")


# --- alert delivery and rules ---

def test_the_webhook_points_at_the_relay_the_app_deploys():
    services = yaml.safe_load((BLUEPRINT / "services.yml").read_text())["services"]
    assert "relay" in services
    assert hook.WEBHOOK == "generic+http://relay:8099/notify"
    assert "HTTPServer((\"0.0.0.0\", 8099), Handler)" in "\n".join(services["relay"]["command"])


def test_an_added_webhook_is_not_dropped_and_the_admin_gets_no_hub_mail(monkeypatch):
    calls = []

    def fake(method, path, *, token=None, body=None):
        if method == "GET":
            return {"items": [{"id": "us1", "settings": {
                "emails": ["Admin@Example.test", "ops@example.test"],
                "webhooks": ["ntfy://example.test/mine"]}}]}
        calls.append((method, body))
        return {}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_alert_delivery("tok", "u1", "admin@example.test") == "updated"
    [(method, body)] = calls
    assert method == "PATCH"
    assert body["settings"]["webhooks"] == ["ntfy://example.test/mine", hook.WEBHOOK]
    assert body["settings"]["emails"] == ["ops@example.test"]


def test_a_webhook_already_present_writes_nothing(monkeypatch):
    def fake(method, path, *, token=None, body=None):
        assert method == "GET", f"wrote on a no-op: {method} {path}"
        return {"items": [{"id": "us1", "settings": {"webhooks": [hook.WEBHOOK], "emails": ["a@example.test"]}}]}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_alert_delivery("tok", "u1", "admin@example.test") == "ok-exists"


def test_a_new_settings_record_is_written_again_after_the_hub_creates_it(monkeypatch):
    calls = []

    def fake(method, path, *, token=None, body=None):
        if method == "GET":
            return {"items": []}
        calls.append((method, path))
        return {"id": "us9"} if method == "POST" else {}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_alert_delivery("tok", "u1", "admin@example.test") == "configured"
    assert [c[0] for c in calls] == ["POST", "PATCH"] and calls[1][1].endswith("/user_settings/records/us9")


def test_the_default_rules_use_names_the_collection_accepts():
    """`name` is a SELECT on the alerts collection; `min` is minutes, so a zero would page on a
    single sample."""
    allowed = {"Status", "CPU", "Memory", "Disk", "Temperature", "Bandwidth", "GPU", "LoadAvg1",
               "LoadAvg5", "LoadAvg15", "Battery"}
    assert hook.RULES
    for rule in hook.RULES:
        assert rule["name"] in allowed and rule["min"] >= 1, rule


def test_existing_rules_are_never_overwritten(monkeypatch):
    posted = []

    def fake(method, path, *, token=None, body=None):
        if method == "GET" and "systems/records" in path:
            return {"items": [{"id": "sys1"}]}
        if method == "GET" and "alerts/records" in path:
            return {"items": [{"id": "a1", "name": "CPU", "value": 99}]}
        posted.append(body)
        return {}
    monkeypatch.setattr(hook, "req", fake)
    assert hook.configure_alert_rules("tok", "u1").startswith("created")
    names = [p["name"] for p in posted]
    assert "CPU" not in names and "Status" in names


def test_the_rules_wait_for_the_agent_s_system_and_a_system_that_never_comes_fails_the_deploy(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(hook, "req", lambda method, path, **kw: {"items": []})
    with pytest.raises(SystemExit):
        hook.configure_alert_rules("tok", "u1")
    assert clock[0] >= hook.SYSTEM_WAIT_S


# --- outgoing mail ---

def _hub_settings(enabled=True, tls=False, port=587):
    return {"smtp": {"enabled": enabled, "host": "smtp.resend.com", "port": port, "username": "resend",
                     "tls": tls, "password": ""},
            "meta": {"senderAddress": "no-reply@example.test", "senderName": "Catena"}}


def _mail_env(monkeypatch, **extra):
    for key, value in {"SMTP_RELAY_HOST": "smtp.resend.com", "SMTP_RELAY_PORT": "587",
                       "SMTP_RELAY_USER": "resend", "SMTP_RELAY_TLS": "tls",
                       "SMTP_RELAY_SENDER": "no-reply@example.test", "SMTP_PASSWORD": "re_new",
                       "STATE_KEY": "state-key", **extra}.items():
        monkeypatch.setenv(key, value)


def _smtp_hub(monkeypatch, settings):
    writes = []

    def fake(method, path, *, token=None, body=None):
        if method == "PATCH":
            writes.append(body)
            settings["smtp"].update(body["smtp"])
            settings["meta"].update(body.get("meta") or {})
        return settings
    monkeypatch.setattr(hook, "req", fake)
    return writes


def _fp(password):
    """The keyed fingerprint of a mail password under the STATE_KEY the tests set."""
    import hashlib
    import hmac
    return hmac.new(b"state-key", password.encode(), hashlib.sha256).hexdigest()


def test_a_new_mail_password_alone_is_written(monkeypatch):
    """PocketBase blanks the password in every read, so the fingerprint of the last write
    tells an unchanged password from a rotated one."""
    _mail_env(monkeypatch)
    writes = _smtp_hub(monkeypatch, _hub_settings())
    state, fp = hook.configure_smtp("tok", _fp("re_old"))
    assert state == "updated" and writes[0]["smtp"]["password"] == "re_new" and fp == _fp("re_new")


def test_an_unchanged_mail_password_writes_nothing(monkeypatch):
    _mail_env(monkeypatch)
    writes = _smtp_hub(monkeypatch, _hub_settings())
    assert hook.configure_smtp("tok", _fp("re_new")) == ("ok-exists", _fp("re_new"))
    assert writes == []


def test_implicit_tls_follows_the_relay_s_mode(monkeypatch):
    _mail_env(monkeypatch, SMTP_RELAY_TLS="ssl", SMTP_RELAY_PORT="465")
    writes = _smtp_hub(monkeypatch, _hub_settings())
    state, _ = hook.configure_smtp("tok", _fp("re_new"))
    assert state == "updated"
    assert writes[0]["smtp"]["tls"] is True and writes[0]["smtp"]["port"] == 465


def test_mail_turned_off_turns_the_hub_s_off(monkeypatch):
    _mail_env(monkeypatch, SMTP_RELAY_HOST="")
    writes = _smtp_hub(monkeypatch, _hub_settings())
    assert hook.configure_smtp("tok", "x")[0] == "disabled" and writes == [{"smtp": {"enabled": False}}]
    writes = _smtp_hub(monkeypatch, _hub_settings(enabled=False))
    assert hook.configure_smtp("tok", "")[0] == "no-mail" and writes == []


# --- the public key ---

def test_the_public_key_is_written_whole_by_rename_and_only_when_it_differs(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "KEY_FILE", str(tmp_path / "key.pub"))
    monkeypatch.setattr(hook, "req", lambda method, path, **kw: {"key": "ssh-ed25519 AAAA", "v": "0.21.1"})
    assert hook.write_hub_key("tok") == "written"
    assert (tmp_path / "key.pub").read_text() == "ssh-ed25519 AAAA\n"
    assert not (tmp_path / "key.pub.tmp").exists()
    assert hook.write_hub_key("tok") == "ok-exists"
    monkeypatch.setattr(hook, "req", lambda method, path, **kw: {"key": ""})
    with pytest.raises(SystemExit):
        hook.write_hub_key("tok")


# --- the admin email and the users records ---

class Hub:
    """A hub whose superuser and users record hold `email` (None: a hub on an empty volume, with
    the placeholder superuser and no user), and that answers every other step as already set up."""

    def __init__(self, email):
        self.superuser = email or "_@b.b"
        self.user = email
        self.claims = []

    def req(self, method, path, *, token=None, body=None):
        if path.endswith("/api/beszel/first-run"):
            return {"firstRun": self.user is None}
        if path.endswith("/api/beszel/create-user"):
            self.claims.append(body)
            if self.user is not None or self.superuser != "_@b.b":
                raise urllib.error.HTTPError(path, 403, "Forbidden", None, None)
            self.superuser = self.user = body["email"]
            return {"msg": "User created"}
        if path.endswith("/api/beszel/info"):
            return {"key": "ssh-ed25519 AAAA"}
        if path.endswith("/_superusers/auth-with-password"):
            if body["identity"] != self.superuser or body["password"] != "pw":
                raise urllib.error.HTTPError(path, 400, "Failed to authenticate.", None, None)
            return {"token": "su-token", "record": {"id": "su1"}}
        if method == "PATCH" and "/_superusers/records/su1" in path:
            self.superuser = body["email"]
            return {}
        if method == "PATCH" and "/users/records/u1" in path:
            self.user = body["email"]
            return {}
        if "/users/records?" in path:
            needle = urllib.parse.unquote(path).split("email~'", 1)[1].split("'")[0]
            found = self.user is not None and needle.lower() in self.user.lower()
            return {"items": [{"id": "u1", "email": self.user}] if found else []}
        if "/universal_tokens/" in path:
            return {"items": [{"id": "t1", "token": "tok"}]}
        if path.endswith("/api/collections/users"):
            return {"oauth2": {"enabled": True, "providers": [_provider()]}}
        if "/user_settings/" in path:
            return {"items": [{"id": "s1", "settings": {"webhooks": [hook.WEBHOOK], "emails": []}}]}
        if "/systems/" in path:
            return {"items": [{"id": "sys1"}]}
        if "/alerts/" in path:
            return {"items": [{"name": r["name"]} for r in hook.RULES]}
        if path.endswith("/api/settings"):
            return _hub_settings(enabled=False)
        raise AssertionError(f"unexpected {method} {path}")


def run_main(monkeypatch, tmp_path, hub, email):
    for key, value in {"ADMIN_EMAIL": email, "HUB_PASSWORD": "pw", "TOKEN": "tok", "STATE_KEY": "state-key",
                       "HUB_OIDC_ISSUER": ISSUER, "HUB_OIDC_SECRET": "s", "SMTP_RELAY_HOST": ""}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HC_URL", raising=False)
    monkeypatch.delenv("HC_PING_KEY", raising=False)
    monkeypatch.setattr(hook, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(hook, "KEY_FILE", str(tmp_path / "key.pub"))
    monkeypatch.setattr(hook, "TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setattr(hook, "PING_FILE", str(tmp_path / "ping-url"))
    monkeypatch.setattr(hook, "req", hub.req)
    hook.main()


def test_an_admin_email_change_moves_the_hub_s_records(monkeypatch, tmp_path, capsys):
    """The hub keeps the email it first started with; the hook signs in with the one it recorded
    and moves both records to the new address."""
    hub = Hub("old@example.test")
    run_main(monkeypatch, tmp_path, hub, "old@example.test")
    run_main(monkeypatch, tmp_path, hub, "new@example.test")
    assert hub.superuser == hub.user == "new@example.test"
    out = capsys.readouterr().out.splitlines()[-1]
    assert out.count("ok-exists") == 7 and "smtp: no-mail" in out
    state = tmp_path / "state.json"
    assert json.loads(state.read_text())["admin_email"] == "new@example.test"
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert (tmp_path / "key.pub").read_text() == "ssh-ed25519 AAAA\n"


def test_the_agent_reads_the_token_from_a_file_only_it_and_root_can_read(monkeypatch, tmp_path):
    hub = Hub("admin@example.test")
    run_main(monkeypatch, tmp_path, hub, "admin@example.test")
    token = tmp_path / "token"
    assert token.read_text() == "tok\n" and oct(token.stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "token.tmp").exists()


def test_the_ping_address_is_written_for_the_relay_and_removed_with_the_monitor(monkeypatch, tmp_path):
    hub = Hub("admin@example.test")
    run_main(monkeypatch, tmp_path, hub, "admin@example.test")
    assert not (tmp_path / "ping-url").exists()
    monkeypatch.setenv("HC_URL", "http://healthchecks:8000/")
    monkeypatch.setenv("HC_PING_KEY", "pk")
    monkeypatch.setattr(hook, "req", hub.req)
    hook.main()
    ping = tmp_path / "ping-url"
    assert ping.read_text() == "http://healthchecks:8000/ping/pk\n" and oct(ping.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("HC_PING_KEY", "")
    hook.main()
    assert not ping.exists()


def test_the_state_holds_a_keyed_fingerprint_and_no_password_hash_anyone_can_guess(monkeypatch, tmp_path):
    hub = Hub("admin@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "re_new")
    run_main(monkeypatch, tmp_path, hub, "admin@example.test")
    monkeypatch.setenv("SMTP_RELAY_HOST", "smtp.resend.com")
    monkeypatch.setattr(hook, "req", _smtp_hub_with(hub))
    hook.main()
    held = json.loads((tmp_path / "state.json").read_text())["smtp_password_sha256"]
    assert held == _fp("re_new")
    import hashlib
    assert held != hashlib.sha256(b"re_new").hexdigest()


def _smtp_hub_with(hub):
    settings = _hub_settings(enabled=False)

    def fake(method, path, *, token=None, body=None):
        if path.endswith("/api/settings"):
            if method == "PATCH":
                settings["smtp"].update(body["smtp"])
                settings["meta"].update(body.get("meta") or {})
            return settings
        return hub.req(method, path, token=token, body=body)
    return fake


def test_a_mixed_case_email_leaves_the_superuser_in_its_case_and_the_users_record_lowercase(
        monkeypatch, tmp_path):
    """Keycloak sends the address lowercase and the hub compares the users record exactly."""
    hub = Hub("Old@example.test")
    run_main(monkeypatch, tmp_path, hub, "Old@example.test")
    assert (hub.superuser, hub.user) == ("Old@example.test", "old@example.test")
    run_main(monkeypatch, tmp_path, hub, "New@example.test")
    assert (hub.superuser, hub.user) == ("New@example.test", "new@example.test")


def test_a_hub_with_no_user_is_claimed_as_the_admin_and_one_with_a_user_is_not(monkeypatch, tmp_path):
    hub = Hub(None)
    run_main(monkeypatch, tmp_path, hub, "Admin@example.test")
    assert hub.claims == [{"email": "Admin@example.test", "password": "pw"}]
    assert (hub.superuser, hub.user) == ("Admin@example.test", "admin@example.test")
    again = Hub("admin@example.test")
    run_main(monkeypatch, tmp_path, again, "admin@example.test")
    assert again.claims == []


def test_an_unknown_admin_email_fails_loudly(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, Hub("someone@example.test"), "new@example.test")


def test_the_sign_in_with_catena_is_set_only_when_the_issuer_and_the_secret_are_both_set(
        monkeypatch, tmp_path, capsys):
    hub = Hub("admin@example.test")
    run_main(monkeypatch, tmp_path, hub, "admin@example.test")
    monkeypatch.setenv("HUB_OIDC_SECRET", "")
    monkeypatch.setattr(hook, "req", hub.req)
    hook.main()
    assert "oidc: not-set" in capsys.readouterr().out


class DupHub(Hub):
    """A hub whose users collection holds several records: `users` maps id to email, `token_owner`
    owns the universal token, and the unique email index refuses a patch onto an address another
    record holds exactly."""

    def __init__(self, users, token_owner):
        super().__init__(None)
        self.superuser = "Admin@example.test"
        self.users = dict(users)
        self.token_owner = token_owner
        self.systems = {"sys1": ["dup"]}
        self.settings = {"dup": "s-dup"}
        self.alerts = {"a-dup": ("dup", "sys1", "CPU")}
        self.deleted = []

    def req(self, method, path, *, token=None, body=None):
        plain = urllib.parse.unquote(path)
        if path.endswith("/api/beszel/first-run"):
            return {"firstRun": False}
        if method == "PATCH" and "/users/records/" in path:
            if body["email"] in self.users.values():
                raise urllib.error.HTTPError(path, 400, "unique", None, None)
            self.users[path.rsplit("/", 1)[1]] = body["email"]
            return {}
        if method == "DELETE" and "/users/records/" in path:
            self.users.pop(path.rsplit("/", 1)[1])
            self.deleted.append(path.rsplit("/", 1)[1])
            return {}
        if "/users/records?" in plain:
            needle = plain.split("email~'", 1)[1].split("'")[0].lower()
            return {"items": [{"id": i, "email": e, "created": i} for i, e in self.users.items()
                              if needle in e.lower()]}
        if "/universal_tokens/" in path:
            uid = plain.split("user='", 1)[1].split("'")[0] if "user=" in plain else None
            return {"items": [{"id": "t1", "token": "tok"}] if uid == self.token_owner else []}
        if "/systems/records" in path:
            if method == "PATCH":
                self.systems[path.rsplit("/", 1)[1]] = body["users"]
                return {}
            if "users?=" in plain:
                uid = plain.split("users?='", 1)[1].split("'")[0]
                return {"items": [{"id": s, "users": u} for s, u in self.systems.items() if uid in u]}
            if "users" in plain:
                raise AssertionError(f"systems filtered with a match-all operator: {plain}")
            return {"items": [{"id": "sys1"}]}
        if "/user_settings/records" in path:
            if method != "GET":
                return {"id": "s-new"}
            uid = plain.split("user='", 1)[1].split("'")[0]
            return {"items": [{"id": s, "settings": {"webhooks": [hook.WEBHOOK], "emails": []}}
                              for u, s in self.settings.items() if u == uid]}
        if "/alerts/records" in path:
            if method != "GET":
                return {}
            uid = plain.split("user='", 1)[1].split("'")[0]
            rows = [{"id": i, "user": u, "system": s, "name": n} for i, (u, s, n) in self.alerts.items() if u == uid]
            if "system=" in plain:
                rows += [{"name": r["name"]} for r in hook.RULES]
            return {"items": rows}
        return super().req(method, path, token=token, body=body)


def test_a_lowercase_duplicate_is_merged_into_the_token_owner(monkeypatch, tmp_path):
    """A Keycloak button sign-in created a record on the lowercase address beside the mixed-case
    one. The one owning the universal token is kept, the other is deleted first, and the systems it
    held point at the kept record."""
    hub = DupHub({"seed": "Admin@example.test", "dup": "admin@example.test"}, "seed")
    run_main(monkeypatch, tmp_path, hub, "Admin@example.test")
    assert hub.users == {"seed": "admin@example.test"}
    assert hub.deleted == ["dup"] and hub.systems == {"sys1": ["seed"]}


def test_the_lowercase_record_is_kept_when_it_owns_the_token(monkeypatch, tmp_path):
    hub = DupHub({"dup": "admin@example.test", "seed": "Admin@example.test"}, "dup")
    hub.systems, hub.settings, hub.alerts = {"sys1": ["seed"]}, {}, {}
    run_main(monkeypatch, tmp_path, hub, "Admin@example.test")
    assert hub.users == {"dup": "admin@example.test"} and hub.systems == {"sys1": ["dup"]}


def test_a_system_both_records_share_ends_with_the_kept_one_alone(monkeypatch, tmp_path):
    hub = DupHub({"seed": "Admin@example.test", "dup": "admin@example.test"}, "seed")
    hub.systems = {"sys1": ["seed", "dup"], "sys2": ["dup"]}
    run_main(monkeypatch, tmp_path, hub, "Admin@example.test")
    assert hub.systems == {"sys1": ["seed"], "sys2": ["seed"]}


def test_without_a_token_owner_the_oldest_record_is_kept(monkeypatch, tmp_path):
    hub = DupHub({"b": "admin@example.test", "a": "Admin@example.test"}, None)
    hub.systems, hub.settings, hub.alerts = {}, {}, {}
    run_main(monkeypatch, tmp_path, hub, "Admin@example.test")
    assert hub.users == {"a": "admin@example.test"}


# --- the relay ---

def _relay():
    script = "\n".join(yaml.safe_load((BLUEPRINT / "services.yml").read_text())["services"]["relay"]["command"][2:])
    script = script.replace('HTTPServer(("0.0.0.0", 8099), Handler).serve_forever()', "")
    names = {"__name__": "relay"}
    exec(compile(script, "relay", "exec"), names)
    return names


@pytest.mark.parametrize("title, expected", [
    ("Connection to Web Server 1 is down", ("beszel-web-server-1-status", False)),
    ("Connection to Web Server 1 is up", ("beszel-web-server-1-status", True)),
    ("web-1 CPU above threshold", ("beszel-web-1-cpu", False)),
    ("web-1 CPU below threshold", ("beszel-web-1-cpu", True)),
    ("something else", None),
    ("", None),
])
def test_the_relay_names_one_check_for_the_triggered_and_the_resolved_alert(title, expected):
    assert _relay()["parse_alert"]({"title": title}) == expected


class _Request:
    def __init__(self, body):
        self.rfile = type("R", (), {"read": lambda self, n: body})()
        self.headers = {"Content-Length": str(len(body))}
        self.sent = []

    def send_response(self, code):
        self.sent.append(code)

    def end_headers(self):
        pass


def _post(monkeypatch, tmp_path, payload, base="http://healthchecks:8000/ping/pk", pinged=None, fail=False):
    names = _relay()
    ping = tmp_path / "ping-url"
    if base:
        ping.write_text(base + "\n")
    names["PING_FILE"] = str(ping)

    def urlopen(url, timeout):
        if fail:
            raise OSError("down")
        pinged.append(url)
        return type("A", (), {"read": lambda self: b"OK"})()
    monkeypatch.setattr(names["urllib"].request, "urlopen", urlopen)
    handler = _Request(json.dumps(payload).encode())
    names["Handler"].do_POST(handler)
    return handler.sent


def test_a_triggered_alert_is_a_fail_ping_and_a_resolved_one_a_success_ping(monkeypatch, tmp_path):
    pinged = []
    assert _post(monkeypatch, tmp_path, {"title": "web-1 CPU above threshold"}, pinged=pinged) == [200]
    assert _post(monkeypatch, tmp_path, {"title": "web-1 CPU below threshold"}, pinged=pinged) == [200]
    assert pinged == ["http://healthchecks:8000/ping/pk/beszel-web-1-cpu/fail?create=1",
                      "http://healthchecks:8000/ping/pk/beszel-web-1-cpu?create=1"]


def test_an_unknown_title_pings_nothing_and_a_missing_address_or_a_down_monitor_is_reported(
        monkeypatch, tmp_path):
    pinged = []
    assert _post(monkeypatch, tmp_path, {"title": "hello"}, pinged=pinged) == [204]
    assert _post(monkeypatch, tmp_path / "none", {"title": "web-1 CPU above threshold"}, base="",
                 pinged=pinged) == [503]
    assert _post(monkeypatch, tmp_path, {"title": "web-1 CPU above threshold"}, pinged=pinged, fail=True) == [502]
    assert pinged == []
