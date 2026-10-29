"""The declarations of the two ERPNext templates, `erpnext` (Frappe's image) and
`erpnext-helpdesk` (Catena's build): the verify hook run against stubs of what
it asks, the sign-in entry the scheduler's start writes and the users hook,
both run against a stub of frappe, the data declaration of the database, the
test.yml the CI harness runs after a restore, and the places where the two
differ."""
from __future__ import annotations

import copy
import http.server
import json
import os
import re
import socket
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

# The apps that sign in through Keycloak, and the hooks each declares.
SIGNIN_APPS = SLUGS
CUSTOMER_APPS = ("erpnext",)
GROUPS = {"erpnext": "staff", "erpnext-helpdesk": "staff,client"}
SIGN_UPS = {"erpnext": "Deny", "erpnext-helpdesk": "Allow"}
HOOKS = {"erpnext": {"verify", "users"}, "erpnext-helpdesk": {"verify"}}
OIDC_KEYS = {"OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"}
RETURN_ADDRESS = ("vps.auth.oidc.redirect_uris=https://${ERPNEXT_HOSTNAME}"
                  "/api/method/frappe.integrations.oauth2_logins.custom/keycloak")

each_app = pytest.mark.parametrize("slug", SLUGS)
each_signin_app = pytest.mark.parametrize("slug", SIGNIN_APPS)
each_customer_app = pytest.mark.parametrize("slug", CUSTOMER_APPS)


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
    assert set(hooks) - {"service", "timeout_seconds"} == HOOKS[slug]
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


@each_signin_app
def test_verify_fails_while_the_scheduler_start_still_waits_for_the_backend(slug, tmp_path):
    command = _compose(slug)["services"]["scheduler"]["command"]
    run, _ = _verify(slug, tmp_path, process=" ".join(command[:2] + ["exec", "bench", "schedule"]),
                     answer=b'{"message":"pong"}', workers=3)
    assert run.returncode == 1
    assert "has not started" in run.stderr


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
    image = json.loads((ROOT / "images" / "erpnext-helpdesk" / "apps.json").read_text())["image"]
    assert image == "ghcr.io/catenahq/erpnext-helpdesk"
    for name in FRAPPE_SERVICES:
        rule = images[name]["rule"]
        assert f"{rule['registry']}/{rule['repo']}" == image, name
    assert _entry("erpnext-helpdesk").block["env"]["FRAPPE_APPS"]["setting"]["default"] == "erpnext,telephony,helpdesk"
    assert "$${FRAPPE_APPS//,/ }" in _backend_script("erpnext-helpdesk")


def _without_the_differences(slug: str) -> dict:
    """The compose with what the two apps differ in set aside: the Frappe
    images and their tag rules, FRAPPE_APPS and the install-app line, the
    address defaults, and what the sign-in differs in: the access level, the
    users hook, the groups the main address admits and the sign-ups the entry
    is made with."""
    compose = copy.deepcopy(_compose(slug))
    block = compose["x-catena"]
    block.pop("images", None)
    block.pop("access", None)
    block["hooks"].pop("users", None)
    frontend = compose["services"]["frontend"]
    frontend["labels"] = [label for label in frontend["labels"] if not label.startswith("vps.auth.groups")]
    compose["services"]["scheduler"]["environment"].pop("SIGNIN_SIGN_UPS")
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


# Keycloak sign-in: the labels, the scheduler's start and the users hook.

@each_signin_app
def test_the_main_address_asks_for_the_sign_in_entry_and_names_who_it_admits(slug):
    labels = _compose(slug)["services"]["frontend"]["labels"]
    for label in ("vps.auth.oidc=true", "vps.auth.groups=" + GROUPS[slug], RETURN_ADDRESS):
        assert label in labels
    assert quiesce_lint.lint_signin(_entry(slug)) == []


@each_customer_app
def test_the_block_offers_the_customer_level_and_runs_no_group_hook(slug):
    assert _entry(slug).block["access"] == {
        "customer": {"label": {"en": "Customer portal", "fr": "Portail client"}}}
    assert "users_group" not in _entry(slug).block["hooks"]


@each_signin_app
def test_the_entry_needs_the_release_that_reads_the_sign_in_labels(slug):
    assert _entry(slug).catena["requires_catena"] == "0.11.10"
    assert _entry(slug).catena["sso_mode"] == "pre-wired"


def test_the_helpdesk_offers_no_access_level_and_has_no_users_hook():
    assert "access" not in _entry("erpnext-helpdesk").block
    assert "users" not in _entry("erpnext-helpdesk").block["hooks"]


@each_signin_app
def test_the_sign_ups_the_entry_is_made_with_follow_the_app(slug):
    assert _scheduler(slug)["environment"]["SIGNIN_SIGN_UPS"] == SIGN_UPS[slug]


@each_signin_app
def test_only_the_scheduler_reads_the_sign_in_values(slug):
    for name, service in _compose(slug)["services"].items():
        env = service.get("environment") or {}
        assert (OIDC_KEYS <= set(env)) == (name == "scheduler"), name
        assert not (OIDC_KEYS & set(env)) or name == "scheduler"
    assert not OIDC_KEYS & set(_entry(slug).env)


def _scheduler(slug: str) -> dict:
    return _compose(slug)["services"]["scheduler"]


def _signin_script(slug: str) -> str:
    return _scheduler(slug)["environment"]["FRAPPE_SIGNIN_PY"]


@each_signin_app
def test_the_scheduler_start_parses_and_the_script_has_no_dollar(slug):
    shell, flag, body = _scheduler(slug)["command"]
    assert (shell, flag) == ("bash", "-c")
    subprocess.run(["bash", "-n", "-c", body.replace("$$", "$")], check=True)
    compile(_signin_script(slug), "sign-in", "exec")
    assert "$" not in _signin_script(slug)


def _start(slug: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """The scheduler's start with the backend's port, the interpreter and the
    final command standing in as stubs; each stub prints whether the secret is
    still set."""
    (tmp_path / "sites").mkdir(exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    python = tmp_path / "bin" / "python"
    python.write_text('#!/bin/sh\necho "script ran secret=[${OIDC_CLIENT_SECRET-unset}]"\n')
    python.chmod(0o755)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    try:
        body = (_scheduler(slug)["command"][2].replace("$$", "$")
                .replace("/dev/tcp/backend/8000", f"/dev/tcp/127.0.0.1/{listener.getsockname()[1]}")
                .replace("../env/bin/python", str(python))
                .replace("exec bench schedule", 'echo "schedule secret=[${OIDC_CLIENT_SECRET-unset}]"'))
        return subprocess.run(["bash", "-c", body], cwd=tmp_path, capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"], "FRAPPE_SIGNIN_PY": "ignored", **env})
    finally:
        listener.close()


@each_signin_app
def test_the_scheduler_start_waits_runs_the_script_drops_the_secret_and_starts(slug, tmp_path):
    out = _start(slug, tmp_path, OIDC_CLIENT_ID="app-x", OIDC_CLIENT_SECRET="s3cret")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "catena sign-in: waiting for the backend to serve the site",
        "script ran secret=[s3cret]",
        "schedule secret=[unset]"]


@each_signin_app
def test_the_scheduler_start_without_sign_in_values_starts_at_once(slug, tmp_path):
    out = _start(slug, tmp_path, OIDC_CLIENT_SECRET="")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["schedule secret=[unset]"]


FRAPPE_STUB = '''
import json
import os
import types

_state_path = os.environ["STUB_STATE"]
_log_path = os.environ["STUB_LOG"]
_state = json.load(open(_state_path))


def _note(*call):
    with open(_log_path, "a") as log:
        log.write(json.dumps(list(call)) + "\\n")


class Doc:
    def __init__(self, doctype, data):
        self.__dict__["doctype"] = doctype
        self.__dict__["data"] = data

    def get(self, key):
        return self.data.get(key)

    def update(self, values):
        self.data.update(values)

    def __setattr__(self, key, value):
        self.data[key] = value

    def __getattr__(self, key):
        if key == "roles":
            return [types.SimpleNamespace(role=role) for role in self.data["roles"]]
        return self.data.get(key)

    def get_password(self, fieldname, raise_exception=True):
        return self.data.get(fieldname)

    def _name(self):
        return self.data["name"] if self.doctype == "User" else self.data["provider_name"].lower()

    def _store(self):
        _state["docs"].setdefault(self.doctype, {})[self._name()] = json.loads(json.dumps(self.data))

    def insert(self):
        _note("insert", self.doctype, self._name(), self.data)
        self._store()

    def save(self):
        stored = _state["docs"][self.doctype][self._name()]
        _note("save", self.doctype, self._name(), sorted(k for k in self.data if stored.get(k) != self.data[k]))
        self._store()

    def append_roles(self, *roles):
        for role in roles:
            if role not in self.data["roles"]:
                self.data["roles"].append(role)

    def add_roles(self, *roles):
        self.append_roles(*roles)
        self.save()

    def remove_roles(self, *roles):
        self.data["roles"] = [role for role in self.data["roles"] if role not in roles]
        self.save()


class _Db:
    def exists(self, doctype, name):
        return name in _state["docs"].get(doctype, {})

    def get_value(self, doctype, filters, fieldname):
        assert doctype == "DefaultValue" and fieldname == "defvalue"
        return _state["defaults"].get(filters["defkey"])

    def set_single_value(self, doctype, fieldname, value):
        _note("single", doctype, fieldname, value)

    def commit(self):
        _note("commit")
        json.dump(_state, open(_state_path, "w"))


def _set_global_default(key, value):
    _note("default", key, value)
    _state["defaults"][key] = value


db = _Db()
defaults = types.SimpleNamespace(set_global_default=_set_global_default)


def init(site):
    _note("init", site)


def connect():
    if os.environ.get("STUB_DOWN"):
        raise Exception("no database")
    _note("connect")


def destroy():
    _note("destroy")


def new_doc(doctype):
    return Doc(doctype, {"roles": []} if doctype == "User" else {})


def get_doc(doctype, name):
    return Doc(doctype, json.loads(json.dumps(_state["docs"][doctype][name])))
'''

ISSUER = "https://auth.a.example/realms/vps"
IN_STEP = {
    "client_id": "app-x", "base_url": ISSUER, "custom_base_url": 0,
    "authorize_url": ISSUER + "/protocol/openid-connect/auth",
    "access_token_url": ISSUER + "/protocol/openid-connect/token",
    "api_endpoint": ISSUER + "/protocol/openid-connect/userinfo",
    "redirect_url": "https://erp.a.example/api/method/frappe.integrations.oauth2_logins.custom/keycloak",
    "client_secret": "s3cret", "enable_social_login": 1, "sign_ups": "Deny", "provider_name": "Keycloak",
}


class _Site:
    """A stub of frappe over a site held in a JSON file: the documents it has,
    the global defaults, and a log of the calls the script made."""

    def __init__(self, tmp_path: Path, docs: dict | None = None, defaults: dict | None = None):
        self.tmp = tmp_path
        stubs = tmp_path / "stubs" / "frappe"
        stubs.mkdir(parents=True)
        (stubs / "__init__.py").write_text(FRAPPE_STUB, encoding="utf-8")
        self.state = tmp_path / "state.json"
        self.state.write_text(json.dumps({"docs": docs or {}, "defaults": defaults or {}}))
        self.log = tmp_path / "log"

    def _env(self, **env: str) -> dict:
        return {"PATH": os.environ["PATH"], "PYTHONPATH": str(self.tmp / "stubs"), "STUB_STATE": str(self.state),
                "STUB_LOG": str(self.log), "SITE_NAME": "erp.a.example", **env}

    def calls(self) -> list[list]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls()]

    def docs(self, doctype: str) -> dict:
        return json.loads(self.state.read_text())["docs"].get(doctype, {})

    def record(self) -> dict:
        return json.loads(json.loads(self.state.read_text())["defaults"]["catena_access_customer"])

    def sign_in(self, slug: str, **env: str) -> subprocess.CompletedProcess:
        values = {"OIDC_CLIENT_ID": "app-x", "OIDC_CLIENT_SECRET": "s3cret", "OIDC_ISSUER_URL": ISSUER}
        values.update(env)
        return subprocess.run([sys.executable, "-c", _signin_script(slug)], capture_output=True, text=True,
                              timeout=30, env=self._env(SIGNIN_SIGN_UPS="Deny", **values))

    def users(self, slug: str, members: list[dict], **env: str) -> subprocess.CompletedProcess:
        [argv] = _entry(slug).block["hooks"]["users"]
        body = (argv[2].replace("$$", "$").replace("/home/frappe/frappe-bench/sites", str(self.tmp))
                .replace("../env/bin/python", sys.executable))
        return subprocess.run(["bash", "-c", body], input=json.dumps(members), capture_output=True, text=True,
                              timeout=30, env=self._env(**env))


@each_customer_app
def test_the_users_hook_body_parses(slug):
    [argv] = _entry(slug).block["hooks"]["users"]
    subprocess.run(["bash", "-n", "-c", argv[2].replace("$$", "$")], check=True)
    assert "$" not in argv[2].replace("$$", "")


@each_signin_app
def test_with_no_sign_in_values_the_script_does_not_touch_the_site(slug, tmp_path):
    site = _Site(tmp_path)
    out = site.sign_in(slug, OIDC_CLIENT_SECRET="")
    assert out.returncode == 0, out.stderr
    assert "stays off" in out.stdout
    assert site.calls() == []


@each_signin_app
def test_a_missing_entry_is_made_once_with_the_sign_ups_the_environment_names(slug, tmp_path):
    site = _Site(tmp_path)
    out = site.sign_in(slug, OIDC_ISSUER_URL=ISSUER + "/")
    assert out.returncode == 0, out.stderr
    assert site.kinds() == ["init", "connect", "insert", "single", "commit", "destroy"]
    [insert] = [c for c in site.calls() if c[0] == "insert"]
    assert insert[1:3] == ["Social Login Key", "keycloak"]
    made = insert[3]
    assert {key: made[key] for key in IN_STEP} == IN_STEP
    assert made["social_login_provider"] == "Custom" and made["user_id_property"] == "sub"
    assert made["show_in_resource_metadata"] == 0
    assert json.loads(made["auth_url_data"]) == {"response_type": "code", "scope": "openid email profile"}
    assert ["single", "Contact Us Settings", "is_disabled", 1] in site.calls()


@each_signin_app
def test_the_entry_takes_its_sign_ups_from_the_environment(slug, tmp_path):
    site = _Site(tmp_path)
    assert site.sign_in(slug).returncode == 0
    assert site.docs("Social Login Key")["keycloak"]["sign_ups"] == "Deny"
    other = _Site(tmp_path / "other")
    run = subprocess.run([sys.executable, "-c", _signin_script(slug)], capture_output=True, text=True, timeout=30,
                         env=other._env(OIDC_CLIENT_ID="app-x", OIDC_CLIENT_SECRET="s3cret",
                                        OIDC_ISSUER_URL=ISSUER, SIGNIN_SIGN_UPS="Allow"))
    assert run.returncode == 0, run.stderr
    assert other.docs("Social Login Key")["keycloak"]["sign_ups"] == "Allow"


@each_signin_app
def test_an_entry_in_step_is_left_alone_and_so_is_the_contact_form(slug, tmp_path):
    site = _Site(tmp_path, docs={"Social Login Key": {"keycloak": IN_STEP}})
    out = site.sign_in(slug)
    assert out.returncode == 0, out.stderr
    assert site.kinds() == ["init", "connect", "commit", "destroy"]


@each_signin_app
def test_a_stale_secret_and_issuer_change_only_the_fields_catena_owns(slug, tmp_path):
    stale = dict(IN_STEP, client_secret="old", base_url="https://old.example/realms/x",
                 authorize_url="https://old.example/auth", sign_ups="Allow", enable_social_login=0)
    site = _Site(tmp_path, docs={"Social Login Key": {"keycloak": stale}})
    out = site.sign_in(slug)
    assert out.returncode == 0, out.stderr
    assert site.kinds() == ["init", "connect", "save", "commit", "destroy"]
    [save] = [c for c in site.calls() if c[0] == "save"]
    assert save[3] == ["authorize_url", "base_url", "client_secret"]
    kept = site.docs("Social Login Key")["keycloak"]
    assert kept["sign_ups"] == "Allow" and kept["enable_social_login"] == 0
    assert kept["base_url"] == ISSUER and kept["client_secret"] == "s3cret"


# The users hook.

ANA ="ana@client.example"


def _member(email: str, *levels: str, enabled: bool = True, first: str = "Ana", last: str = "B") -> dict:
    return {"username": email.split("@")[0], "email": email, "first_name": first, "last_name": last,
            "enabled": enabled, "access": list(levels)}


def _user(*roles: str, enabled: int = 1) -> dict:
    return {"name": ANA, "enabled": enabled, "roles": list(roles)}


@each_customer_app
def test_a_new_member_gets_a_website_user_with_the_customer_role_and_it_is_recorded(slug, tmp_path):
    site = _Site(tmp_path)
    out = site.users(slug, [_member("Ana@Client.Example", "customer"), _member("staff@a.example")])
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "catena access: customer: 1 granted, 0 removed"
    [(email, user)] = site.docs("User").items()
    assert email == "ana@client.example"
    assert (user["user_type"], user["send_welcome_email"], user["enabled"]) == ("Website User", 0, 1)
    assert (user["first_name"], user["last_name"], user["roles"]) == ("Ana", "B", ["Customer"])
    assert site.record() == {"ana@client.example": "made"}


@each_customer_app
def test_a_disabled_member_gets_nothing(slug, tmp_path):
    site = _Site(tmp_path)
    assert site.users(slug, [_member("ana@client.example", "customer", enabled=False)]).returncode == 0
    assert site.docs("User") == {} and site.record() == {}


@each_customer_app
def test_an_existing_user_gets_the_role_added_and_nothing_else(slug, tmp_path):
    site = _Site(tmp_path, docs={"User": {"ana@client.example": _user("Sales User")}})
    out = site.users(slug, [_member("ana@client.example", "customer")])
    assert out.returncode == 0, out.stderr
    assert [c for c in site.calls() if c[0] in ("insert", "save")] == [["save", "User", "ana@client.example", ["roles"]]]
    assert site.docs("User")["ana@client.example"]["roles"] == ["Sales User", "Customer"]
    assert site.record() == {"ana@client.example": "added"}


@each_customer_app
def test_a_member_gone_loses_the_role_and_the_account_the_hook_made_is_disabled(slug, tmp_path):
    site = _Site(tmp_path, docs={"User": {"ana@client.example": _user("Customer")}},
                 defaults={"catena_access_customer": json.dumps({"ana@client.example": "made"})})
    out = site.users(slug, [])
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "catena access: customer: 0 granted, 1 removed"
    assert site.docs("User")["ana@client.example"]["roles"] == []
    assert site.docs("User")["ana@client.example"]["enabled"] == 0
    assert site.record() == {"ana@client.example": "closed"}


@each_customer_app
def test_an_account_the_hook_made_that_holds_another_role_stays_enabled(slug, tmp_path):
    site = _Site(tmp_path, docs={"User": {"ana@client.example": _user("Customer", "Sales User")}},
                 defaults={"catena_access_customer": json.dumps({"ana@client.example": "made"})})
    assert site.users(slug, []).returncode == 0
    assert site.docs("User")[ANA] == _user("Sales User")
    assert site.record() == {}


@each_customer_app
def test_a_role_given_by_hand_is_kept_when_the_member_goes(slug, tmp_path):
    site = _Site(tmp_path, docs={"User": {"ana@client.example": _user("Customer")}})
    assert site.users(slug, [_member("ana@client.example", "customer")]).returncode == 0
    assert site.record() == {"ana@client.example": "had"}
    assert site.users(slug, []).returncode == 0
    assert site.docs("User")["ana@client.example"] == _user("Customer")
    assert site.record() == {}


@each_customer_app
def test_a_member_given_the_level_again_has_the_closed_account_enabled(slug, tmp_path):
    site = _Site(tmp_path, docs={"User": {"ana@client.example": _user(enabled=0)}},
                 defaults={"catena_access_customer": json.dumps({"ana@client.example": "closed"})})
    assert site.users(slug, [_member("ana@client.example", "customer")]).returncode == 0
    assert site.docs("User")["ana@client.example"] == _user("Customer")
    assert site.record() == {"ana@client.example": "made"}


@each_customer_app
def test_a_member_without_the_level_is_not_given_the_role(slug, tmp_path):
    site = _Site(tmp_path)
    assert site.users(slug, [_member("ana@client.example", "other")]).returncode == 0
    assert site.docs("User") == {}


@each_customer_app
def test_a_site_that_does_not_answer_ends_the_hook_with_exit_one(slug, tmp_path):
    site = _Site(tmp_path)
    out = site.users(slug, [_member("ana@client.example", "customer")], STUB_DOWN="1")
    assert out.returncode == 1
    assert "does not answer" in out.stderr
    assert "insert" not in site.kinds()


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
