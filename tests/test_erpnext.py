"""The declarations of the two ERPNext templates, `erpnext` (Frappe's image) and
`erpnext-helpdesk` (Catena's build): the verify hook run against stubs of what
it asks, the sign-in entry the scheduler's start writes and the users hook,
both run against a stub of frappe, the data declaration of the database, the
test.yml the CI harness runs, its seeds against a stub of frappe and its
asserts against a stub of the site, and the places where the two differ."""
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
import urllib.parse
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
HELPDESK_APPS = ("erpnext-helpdesk",)
GROUPS = {"erpnext": "staff", "erpnext-helpdesk": "staff,client"}
SIGN_UPS = {"erpnext": "Deny", "erpnext-helpdesk": "Allow"}
HOOKS = {"erpnext": {"verify", "users"}, "erpnext-helpdesk": {"verify"}}
OIDC_KEYS = {"OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER_URL"}
RETURN_ADDRESS = ("vps.auth.oidc.redirect_uris=https://${ERPNEXT_HOSTNAME}"
                  "/api/method/frappe.integrations.oauth2_logins.custom/keycloak")

each_app = pytest.mark.parametrize("slug", SLUGS)
each_signin_app = pytest.mark.parametrize("slug", SIGNIN_APPS)
each_customer_app = pytest.mark.parametrize("slug", CUSTOMER_APPS)
each_helpdesk_app = pytest.mark.parametrize("slug", HELPDESK_APPS)


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
    """The compose with what the two apps differ in set aside: every image
    (the engine bump moves each app's pins on its own) and the Frappe tag
    rules, FRAPPE_APPS and the install-app line, the
    address defaults, what the sign-in differs in (the access level, the users
    hook, the groups the main address admits, the name and description labels
    and the sign-ups the entry is made
    with) and what the helpdesk closes (the web forms setting and its start
    step, the site setting block_endpoints)."""
    compose = copy.deepcopy(_compose(slug))
    block = compose["x-catena"]
    block.pop("requires_catena", None)
    block.pop("images", None)
    block.pop("access", None)
    block["hooks"].pop("users", None)
    frontend = compose["services"]["frontend"]
    frontend["labels"] = [label for label in frontend["labels"]
                          if not label.startswith(("vps.auth.groups", "vps.homepage."))]
    scheduler = compose["services"]["scheduler"]
    scheduler["environment"].pop("SIGNIN_SIGN_UPS")
    scheduler["environment"].pop("ERPNEXT_WEB_FORMS", None)
    scheduler["environment"].pop("FRAPPE_FORMS_PY", None)
    scheduler["command"] = "start"
    block["env"].pop("FRAPPE_APPS", None)
    block["env"].pop("ERPNEXT_WEB_FORMS", None)
    for key in ("DOMAIN_HOST", "ERPNEXT_HOSTNAME"):
        block["env"][key] = "address"
    for service in compose["services"].values():
        service["image"] = "image"
    backend = compose["services"]["backend"]
    backend["environment"].pop("FRAPPE_APPS", None)
    shell, flag, script = backend["command"]
    script = re.sub(r"if ! jq.*?\nfi\n", "", re.sub(r".*install-app.*", "install-app", script), flags=re.S)
    backend["command"] = [shell, flag, script]
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
    entry = _entry(slug)
    assert tuple(map(int, entry.requires_catena.split("."))) >= (0, 11, 12)
    assert entry.catena["sso_mode"] == "pre-wired"


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
    python.write_text('#!/bin/sh\necho "$2 ran secret=[${OIDC_CLIENT_SECRET-unset}]"\n')
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
                              env={"PATH": os.environ["PATH"], "FRAPPE_SIGNIN_PY": "sign-in",
                                   "FRAPPE_FORMS_PY": "forms", **env})
    finally:
        listener.close()


WAITS = {"erpnext": "catena sign-in: waiting for the backend to serve the site",
         "erpnext-helpdesk": "catena start: waiting for the backend to serve the site"}


@each_signin_app
def test_the_scheduler_start_waits_runs_the_scripts_drops_the_secret_and_starts(slug, tmp_path):
    out = _start(slug, tmp_path, OIDC_CLIENT_ID="app-x", OIDC_CLIENT_SECRET="s3cret")
    assert out.returncode == 0, out.stderr
    steps = ["sign-in ran secret=[s3cret]"] + (["forms ran secret=[s3cret]"] if slug in HELPDESK_APPS else [])
    assert out.stdout.splitlines() == [WAITS[slug], *steps, "schedule secret=[unset]"]


@each_customer_app
def test_the_plain_scheduler_without_sign_in_values_starts_at_once(slug, tmp_path):
    out = _start(slug, tmp_path, OIDC_CLIENT_SECRET="")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["schedule secret=[unset]"]


@each_helpdesk_app
def test_the_helpdesk_scheduler_waits_for_the_backend_and_closes_the_forms_even_without_sign_in_values(slug, tmp_path):
    out = _start(slug, tmp_path, OIDC_CLIENT_SECRET="")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [WAITS[slug], "forms ran secret=[]", "schedule secret=[unset]"]


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
        return self.data["provider_name"].lower() if self.doctype == "Social Login Key" else self.data["name"]

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
        if doctype == "Web Form":
            return _state["docs"].get(doctype, {}).get(filters, {}).get(fieldname)
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

    def close_forms(self, slug: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", _scheduler(slug)["environment"]["FRAPPE_FORMS_PY"]],
                              capture_output=True, text=True, timeout=30, env=self._env(**env))

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


# The helpdesk closes what visitors and customers do not need.

def _web_forms(*published: int) -> dict:
    return {"Web Form": {name: {"name": name, "published": value}
                         for name, value in zip(("issues", "tasks"), published)}}


@each_helpdesk_app
def test_the_forms_setting_is_off_by_default_and_only_the_scheduler_reads_it(slug):
    setting = _entry(slug).block["env"]["ERPNEXT_WEB_FORMS"]["setting"]
    assert setting["default"] == "off"
    assert setting["label"] == {"en": "ERPNext issue and task forms",
                                "fr": "Formulaires d'incident et de tâche d'ERPNext"}
    readers = [name for name, service in _compose(slug)["services"].items()
               if "ERPNEXT_WEB_FORMS" in (service.get("environment") or {})]
    assert readers == ["scheduler"]
    assert _scheduler(slug)["environment"]["ERPNEXT_WEB_FORMS"] == "${ERPNEXT_WEB_FORMS:-off}"
    script = _scheduler(slug)["environment"]["FRAPPE_FORMS_PY"]
    compile(script, "forms", "exec")
    assert "$" not in script


def test_the_plain_app_has_neither_the_forms_setting_nor_the_endpoint_guard():
    assert "ERPNEXT_WEB_FORMS" not in _entry("erpnext").block["env"]
    assert "FRAPPE_FORMS_PY" not in _scheduler("erpnext")["environment"]
    assert "block_endpoints" not in _backend_script("erpnext")


@each_helpdesk_app
@pytest.mark.parametrize("setting", [{"ERPNEXT_WEB_FORMS": "off"}, {}])
def test_while_the_forms_setting_is_off_or_unset_both_forms_are_unpublished(slug, setting, tmp_path):
    site = _Site(tmp_path, docs=_web_forms(1, 1))
    out = site.close_forms(slug, **setting)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "catena start: ERPNext web forms closed: issues, tasks"
    assert [c[1:] for c in site.calls() if c[0] == "save"] == [
        ["Web Form", "issues", ["published"]], ["Web Form", "tasks", ["published"]]]
    assert {name: form["published"] for name, form in site.docs("Web Form").items()} == {"issues": 0, "tasks": 0}


@each_helpdesk_app
@pytest.mark.parametrize("setting", ["on", " On "])
def test_while_the_forms_setting_is_on_the_forms_are_left_alone(slug, setting, tmp_path):
    site = _Site(tmp_path, docs=_web_forms(1, 1))
    out = site.close_forms(slug, ERPNEXT_WEB_FORMS=setting)
    assert out.returncode == 0, out.stderr
    assert site.calls() == []
    assert {form["published"] for form in site.docs("Web Form").values()} == {1}


@each_helpdesk_app
def test_a_form_already_closed_or_absent_is_not_saved_again(slug, tmp_path):
    site = _Site(tmp_path, docs={"Web Form": {"issues": {"name": "issues", "published": 0}}})
    out = site.close_forms(slug, ERPNEXT_WEB_FORMS="off")
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""
    assert "save" not in site.kinds()


@each_helpdesk_app
def test_the_backend_writes_block_endpoints_after_the_install_and_only_when_the_site_has_none(slug, tmp_path):
    script = _backend_script(slug).replace("$$", "$")
    assert script.index("install-app") < script.index("block_endpoints") < script.index("unset ")
    snippet = re.search(r"if ! jq.*?\nfi\n", script, re.S).group(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    jq = bin_dir / "jq"
    jq.write_text(f'#!/bin/sh\nexec {sys.executable} -c \'import json, sys; '
                  'sys.exit(0 if "block_endpoints" in json.load(open(sys.argv[1])) else 1)\' "$3"\n')
    bench = bin_dir / "bench"
    bench.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/bench.log\n')
    for tool in (jq, bench):
        tool.chmod(0o755)
    (tmp_path / "sites" / "erp.a.example").mkdir(parents=True)
    config = tmp_path / "sites" / "erp.a.example" / "site_config.json"
    for content, called in (({}, True), ({"db_name": "x"}, True), ({"block_endpoints": 0}, False),
                            ({"block_endpoints": 1}, False)):
        config.write_text(json.dumps(content))
        (tmp_path / "bench.log").unlink(missing_ok=True)
        run = subprocess.run(["bash", "-ec", snippet], cwd=tmp_path, capture_output=True, text=True,
                             env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "SITE_NAME": "erp.a.example"})
        assert run.returncode == 0, run.stderr
        log = (tmp_path / "bench.log").read_text().splitlines() if (tmp_path / "bench.log").exists() else []
        assert log == (["--site erp.a.example set-config -p block_endpoints 1"] if called else []), content


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


def test_the_helpdesk_test_holds_every_step_of_erpnexts_plus_its_ticket():
    plain, desk = _test_yml("erpnext"), _test_yml("erpnext-helpdesk")
    for phase, extra in (("seed", ["helpdesk_ticket"]), ("assert", ["ticket_through_the_site", "web_forms_closed"])):
        steps = {step["name"]: step for step in desk[phase]}
        for name in extra:
            assert steps.pop(name)
        assert list(steps.values()) == plain[phase], phase


RUN = "r1"
PASSWORD = "s3cret-admin"
SITE_NAME = "erp.a.example"
SEED_STUB = '''
import json
import os

_path = os.environ["STUB_STATE"]
_state = json.load(open(_path))


def _note(*call):
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps(list(call)) + "\\n")


class _Doc:
    def __init__(self, data):
        self.data = data
        self.name = None

    def insert(self):
        doctype = self.data["doctype"]
        if os.environ.get("STUB_FAIL") == doctype:
            _note("refused", doctype)
            raise Exception("insert refused")
        self.name = self.data["name"] = doctype + "-" + str(len(_state.get(doctype, [])) + 1)
        _note("insert", doctype, self.data)
        _state.setdefault(doctype, []).append(self.data)
        return self


class _Db:
    def get_value(self, doctype, filters):
        found = [doc for doc in _state.get(doctype, []) if all(doc.get(k) == v for k, v in filters.items())]
        return found[0]["name"] if found else None

    def exists(self, doctype, filters):
        return self.get_value(doctype, filters)

    def commit(self):
        _note("commit")
        json.dump(_state, open(_path, "w"))

    def rollback(self):
        _note("rollback")


db = _Db()


def init(site):
    _note("init", site)


def connect():
    if os.environ.get("STUB_DOWN"):
        raise Exception("no database")
    _note("connect")


def destroy():
    _note("destroy")


def get_doc(data):
    return _Doc(dict(data))
'''
TICKET_STUB = '''
import frappe


def new(doc, attachments=[]):
    assert attachments == []
    return frappe.get_doc(dict(doc, doctype="HD Ticket", raised_by="Administrator")).insert()
'''


def _rendered(step: dict) -> list[str]:
    return [part.replace("{{ run_id }}", RUN) for part in step["command"]]


class _Seeds:
    """The seed steps of a test.yml run against a stub of frappe and of the
    helpdesk's ticket call, over a site held in a JSON file."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        stubs = tmp_path / "stubs"
        (stubs / "frappe").mkdir(parents=True)
        (stubs / "frappe" / "__init__.py").write_text(SEED_STUB, encoding="utf-8")
        package = stubs
        for part in ("helpdesk", "helpdesk", "doctype", "hd_ticket"):
            package = package / part
            package.mkdir()
            (package / "__init__.py").touch()
        (package / "api.py").write_text(TICKET_STUB, encoding="utf-8")
        self.state = tmp_path / "state.json"
        self.state.write_text("{}")
        self.log = tmp_path / "log"

    def run(self, slug: str, **env: str) -> list[subprocess.CompletedProcess]:
        runs = []
        for step in _test_yml(slug)["seed"]:
            shell, flag, body = _rendered(step)
            body = body.replace("/home/frappe/frappe-bench/sites", str(self.tmp)).replace(
                "../env/bin/python", sys.executable)
            runs.append(subprocess.run(
                [shell, flag, body], capture_output=True, text=True, timeout=30,
                env={"PATH": os.environ["PATH"], "PYTHONPATH": str(self.tmp / "stubs"), "SITE_NAME": SITE_NAME,
                     "STUB_STATE": str(self.state), "STUB_LOG": str(self.log), **env}))
        return runs

    def docs(self) -> dict:
        return json.loads(self.state.read_text())

    def kinds(self) -> list[str]:
        return [json.loads(line)[0] for line in self.log.read_text().splitlines()]


@each_app
def test_the_seed_makes_the_note_its_private_file_and_the_ticket_once_however_often_it_runs(slug, tmp_path):
    seeds = _Seeds(tmp_path)
    for _ in range(2):
        for step, run in zip(_test_yml(slug)["seed"], seeds.run(slug)):
            assert run.returncode == 0, run.stderr
            assert step["expect_stdout_contains"].replace("{{ run_id }}", RUN) in run.stdout
    docs = seeds.docs()
    assert docs["Note"] == [{"doctype": "Note", "title": "catena-r1", "content": "fixture-r1", "name": "Note-1"}]
    assert docs["File"] == [{"attached_to_doctype": "Note", "attached_to_name": "Note-1",
                             "file_name": "catena-r1.txt", "doctype": "File", "content": "fixture-r1",
                             "is_private": 1, "name": "File-1"}]
    if slug in HELPDESK_APPS:
        assert docs["HD Ticket"] == [{"subject": "catena-r1", "description": "fixture-r1", "doctype": "HD Ticket",
                                      "raised_by": "Administrator", "name": "HD Ticket-1"}]
    else:
        assert "HD Ticket" not in docs
    assert seeds.kinds().count("insert") == len(docs)


@each_app
@pytest.mark.parametrize("refused", ["Note", "File"])
def test_a_refused_insert_rolls_the_seed_back_and_commits_nothing(slug, refused, tmp_path):
    seeds = _Seeds(tmp_path)
    [run, *_] = seeds.run(slug, STUB_FAIL=refused)
    assert run.returncode != 0 and "insert refused" in run.stderr
    first = seeds.kinds()[:seeds.kinds().index("destroy") + 1]
    assert first[-3:] == ["refused", "rollback", "destroy"]
    assert "commit" not in first
    assert not {"Note", "File"} & set(seeds.docs())


@each_helpdesk_app
def test_a_refused_ticket_rolls_its_seed_back(slug, tmp_path):
    seeds = _Seeds(tmp_path)
    documents, ticket = seeds.run(slug, STUB_FAIL="HD Ticket")
    assert documents.returncode == 0 and ticket.returncode != 0
    assert seeds.kinds()[-3:] == ["refused", "rollback", "destroy"]
    assert "HD Ticket" not in seeds.docs()


@each_app
def test_the_seed_closes_frappe_when_the_site_does_not_answer(slug, tmp_path):
    seeds = _Seeds(tmp_path)
    for run in seeds.run(slug, STUB_DOWN="1"):
        assert run.returncode != 0
    assert seeds.kinds() == ["init", "destroy"] * len(_test_yml(slug)["seed"])


class _FrappeSite(http.server.BaseHTTPRequestHandler):
    """The backend's listener as the asserts ask it: the login, the REST lists
    of the documents it holds, filtered and cut to the fields asked, and the
    private files."""
    docs: dict = {}
    files: dict = {}
    seen: list = []

    def _answer(self, code: int, body: bytes, headers: dict | None = None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).seen.append(("POST", self.path, self.headers.get("Host")))
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if self.path == "/api/method/login" and form == {"usr": ["Administrator"], "pwd": [PASSWORD]}:
            self._answer(200, b'{"message": "Logged In"}', {"Set-Cookie": "sid=abc; HttpOnly"})
        else:
            self._answer(401, b"{}")

    def do_GET(self):
        type(self).seen.append(("GET", self.path, self.headers.get("Host")))
        if self.headers.get("Cookie") != "sid=abc":
            return self._answer(403, b"{}")
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        if path in type(self).files:
            return self._answer(200, type(self).files[path].encode())
        doctype = path.removeprefix("/api/resource/")
        if doctype not in type(self).docs:
            return self._answer(404, b"{}")
        query = {key: json.loads(value) for key, [value] in urllib.parse.parse_qs(url.query).items()}
        rows = [{field: doc.get(field) for field in query["fields"]} for doc in type(self).docs[doctype]
                if all(op == "=" and doc.get(field) == value for field, op, value in query["filters"])]
        self._answer(200, json.dumps({"data": rows}).encode())

    def log_message(self, *args):
        pass


def _seeded_site() -> tuple[dict, dict]:
    docs = {"Note": [{"name": "n1", "title": "catena-r1", "content": "<p>fixture-r1</p>"},
                     {"name": "n0", "title": "catena-r0", "content": "fixture-r0"}],
            "File": [{"attached_to_doctype": "Note", "attached_to_name": "n1",
                      "file_url": "/private/files/catena-r1.txt"}],
            "HD Ticket": [{"name": 7, "subject": "catena-r1", "description": "<p>fixture-r1</p>"}]}
    return docs, {"/private/files/catena-r1.txt": "fixture-r1"}


def _logged_in_asserts(slug: str) -> list[dict]:
    return [step for step in _test_yml(slug)["assert"] if step["name"] != "web_forms_closed"]


def _asserts(slug: str, docs: dict, files: dict,
             password: str = PASSWORD) -> list[tuple[dict, subprocess.CompletedProcess]]:
    _FrappeSite.docs, _FrappeSite.files, _FrappeSite.seen = docs, files, []
    server = http.server.HTTPServer(("127.0.0.1", 0), _FrappeSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        runs = []
        for step in _logged_in_asserts(slug):
            python, flag, script = _rendered(step)
            assert (python, flag) == ("python3", "-c")
            script = script.replace("http://127.0.0.1:8000", f"http://127.0.0.1:{server.server_port}")
            runs.append((step, subprocess.run(
                [sys.executable, flag, script], capture_output=True, text=True, timeout=60,
                env={"PATH": os.environ["PATH"], "SITE_NAME": SITE_NAME, "ERPNEXT_ADMIN_PASSWORD": password})))
        return runs
    finally:
        server.shutdown()


@each_app
def test_the_asserts_pass_on_a_site_that_lists_each_seeded_document_once(slug):
    docs, files = _seeded_site()
    for step, run in _asserts(slug, docs, files):
        assert run.returncode == 0, run.stderr
        assert step["expect_stdout_contains"].replace("{{ run_id }}", RUN) in run.stdout
        assert PASSWORD not in run.stdout + run.stderr + " ".join(step["command"])
    assert {host for _, _, host in _FrappeSite.seen} == {SITE_NAME}


def _break_note_twice(docs, files):
    docs["Note"].append({"name": "n2", "title": "catena-r1", "content": "fixture-r1"})


def _break_note_content(docs, files):
    docs["Note"][0]["content"] = "other"


def _break_note_gone(docs, files):
    del docs["Note"][0]


def _break_file_record(docs, files):
    docs["File"].clear()


def _break_file_twice(docs, files):
    docs["File"].append(dict(docs["File"][0], file_url="/private/files/catena-r1abcdef.txt"))


def _break_file_on_disk(docs, files):
    files.clear()


def _break_file_content(docs, files):
    files["/private/files/catena-r1.txt"] = "fixture-r1 and more"


@each_app
@pytest.mark.parametrize("damage", [_break_note_twice, _break_note_content, _break_note_gone, _break_file_record,
                                    _break_file_twice, _break_file_on_disk, _break_file_content])
def test_the_site_documents_assert_fails_on_a_site_that_lost_or_doubled_them(slug, damage):
    docs, files = _seeded_site()
    damage(docs, files)
    [(step, run)] = [(step, run) for step, run in _asserts(slug, docs, files)
                     if step["name"] == "site_documents_through_the_site"]
    assert run.returncode != 0
    assert "site documents ok" not in run.stdout


@each_helpdesk_app
@pytest.mark.parametrize("tickets", [[], [{"name": 7, "subject": "catena-r1", "description": "fixture-r1"}] * 2,
                                     [{"name": 7, "subject": "catena-r1", "description": "other"}]])
def test_the_ticket_assert_fails_on_a_site_without_exactly_the_seeded_ticket(slug, tickets):
    docs, files = _seeded_site()
    docs["HD Ticket"] = tickets
    [(step, run)] = [(step, run) for step, run in _asserts(slug, docs, files)
                     if step["name"] == "ticket_through_the_site"]
    assert run.returncode != 0
    assert "helpdesk ticket ok" not in run.stdout


@each_app
def test_the_asserts_fail_when_the_administrator_cannot_sign_in(slug):
    docs, files = _seeded_site()
    for _, run in _asserts(slug, docs, files, password="wrong"):
        assert run.returncode != 0
        assert "/api/method/login answered HTTP 401" in run.stderr
    assert [method for method, _, _ in _FrappeSite.seen] == ["POST"] * len(_logged_in_asserts(slug))


# test.yml of the helpdesk: the closures assert and the sign_in entry.

def _step_named(steps: list[dict], name: str) -> dict:
    [step] = [step for step in steps if step["name"] == name]
    return step


def _sign_in_entry(slug: str, name: str) -> dict:
    [entry] = [entry for entry in _test_yml(slug)["sign_in"] if entry["name"] == name]
    return entry


def _after(name: str) -> dict:
    return _step_named(_sign_in_entry("erpnext-helpdesk", "client_customer")["after"], name)


class _Listener(http.server.BaseHTTPRequestHandler):
    """The backend's or the frontend's listener as the closure and customer steps ask it: each
    (method, path) answers a (status, body) or a redirect."""
    routes: dict = {}
    seen: list = []

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, body))
        answer = type(self).routes.get((self.command, self.path.split("?")[0]), (404, "{}"))
        if answer[0] in (301, 302):
            self.send_response(answer[0])
            self.send_header("Location", answer[1])
            self.end_headers()
            return
        self.send_response(answer[0])
        self.end_headers()
        self.wfile.write(answer[1].encode())

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


def _listener(routes: dict):
    _Listener.routes, _Listener.seen = routes, []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Listener)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _python_step(step: dict, port: int, old_port: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
    python, flag, script = [part.replace("{{ sign_in_email }}", "ana@client.example") for part in step["command"]]
    assert (python, flag) == ("python3", "-c")
    return subprocess.run([sys.executable, flag, script.replace(old_port, f"127.0.0.1:{port}")], input=stdin,
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], "SITE_NAME": SITE_NAME, **env})


ACCEPT = "/api/method/frappe.website.doctype.web_form.web_form.accept"
NOT_PERMITTED = (403, '{"exc_type": "PermissionError", "_server_messages": "Not permitted"}')
CLOSED = {("GET", "/issues/new"): (404, ""), ("GET", "/tasks/new"): (403, ""), ("POST", ACCEPT): NOT_PERMITTED}


def _run_closures(tmp_path: Path, routes: dict, guard=1, forms: str | None = None) -> subprocess.CompletedProcess:
    config = tmp_path / SITE_NAME
    config.mkdir(exist_ok=True)
    (config / "site_config.json").write_text(json.dumps({} if guard is None else {"block_endpoints": guard}))
    step = _step_named(_test_yml("erpnext-helpdesk")["assert"], "web_forms_closed")
    assert step["env"] == ["ERPNEXT_WEB_FORMS"]
    step = {"command": [part.replace("/home/frappe/frappe-bench/sites", str(tmp_path)) for part in step["command"]]}
    server = _listener(routes)
    try:
        return _python_step(step, server.server_port, "127.0.0.1:8000",
                            **({} if forms is None else {"ERPNEXT_WEB_FORMS": forms}))
    finally:
        server.shutdown()


@pytest.mark.parametrize("forms", [None, "", "off", "OFF"])
def test_the_closures_assert_passes_on_a_site_with_closed_forms_and_the_guard(forms, tmp_path):
    run = _run_closures(tmp_path, CLOSED, forms=forms)
    assert run.returncode == 0, run.stderr
    assert "web forms closed: block_endpoints 1" in run.stdout
    assert {(method, path) for method, path, _, _ in _Listener.seen} == set(CLOSED)
    assert {headers["host"] for _, _, headers, _ in _Listener.seen} == {SITE_NAME}
    assert "web_form=issues" in [body for method, _, _, body in _Listener.seen if method == "POST"][0]


@pytest.mark.parametrize("forms", ["on", " On "])
def test_the_closures_assert_passes_without_asking_the_site_while_the_forms_are_on(forms, tmp_path):
    run = _run_closures(tmp_path, {}, guard=None, forms=forms)
    assert run.returncode == 0, run.stderr
    assert "skipped, ERPNEXT_WEB_FORMS is on" in run.stdout
    assert _Listener.seen == []


@pytest.mark.parametrize("damage, why", [
    ({"guard": 0}, "block_endpoints reads 0"),
    ({"guard": None}, "block_endpoints reads None"),
    ({"routes": {**CLOSED, ("GET", "/issues/new"): (200, "<form>")}}, "GET /issues/new answered HTTP 200"),
    ({"routes": {**CLOSED, ("GET", "/tasks/new"): (302, "/login")}}, "GET /tasks/new answered HTTP 302"),
    ({"routes": {**CLOSED, ("POST", ACCEPT): (200, "{}")}}, "accept on the issues form got HTTP 200"),
    ({"routes": {**CLOSED, ("POST", ACCEPT): (403, "{}")}}, "accept on the issues form got HTTP 403"),
])
def test_the_closures_assert_fails_on_an_open_form_or_a_missing_guard(damage, why, tmp_path):
    run = _run_closures(tmp_path, damage.get("routes", CLOSED), guard=damage.get("guard", 1))
    assert run.returncode != 0
    assert why in run.stderr
    assert "web forms closed" not in run.stdout


def test_the_helpdesk_declares_one_client_entry_for_the_customer_it_makes_and_cleans_up():
    [entry] = _test_yml("erpnext-helpdesk")["sign_in"]
    assert {key: value for key, value in entry.items() if key not in ("after", "cleanup")} == {
        "name": "client_customer", "group": "client", "facts": {"system_user": "no"}}
    assert [step["name"] for step in entry["after"]] == [
        "hd_customer_role_held", "guard_refuses_a_call_outside_the_list", "portal_opens_and_forms_are_refused",
        "customer_raises_and_rates_a_ticket"]
    assert [step["name"] for step in entry["cleanup"]] == ["delete_the_ticket_and_the_user"]


def test_every_customer_step_runs_in_the_frontends_helper_with_the_cookies_on_stdin_and_none_on_its_command_line():
    entry = _sign_in_entry("erpnext-helpdesk", "client_customer")
    services = {name.replace("-", "_") for name in _compose("erpnext-helpdesk")["services"]}
    assert "frontend" in services
    for step in entry["after"] + entry["cleanup"]:
        assert step["type"] == "docker_exec"
        assert "{{ sign_in_email }}" in " ".join(step["command"]) or step["stdin"] == "sign_in_cookies"
        assert "{{ sign_in_email }}" in step["expect_stdout_contains"] or step["stdin"] == "sign_in_cookies"
        assert "CATENA_TEST_PASSWORD" not in " ".join(step["command"])
        if step.get("stdin"):
            assert step["container"] == "{{ helper_frontend }}" and step["stdin"] == "sign_in_cookies"
        else:
            assert step["container"] == "{{ container_backend }}"
        if step["command"][0] == "bash":
            subprocess.run(["bash", "-n", "-c", step["command"][2]], check=True)
        else:
            compile(step["command"][2], step["name"], "exec")


COOKIE = "Cookie: sid=zzz-session; csrf=qqq"
PORTAL = '<script>window["csrf_token"] = "tok123";</script>'
TICKET_CALL = "/api/method/helpdesk.helpdesk.doctype.hd_ticket.api.new"
OPTIONS = "/api/method/frappe.client.get_list"
RATE = "/api/method/frappe.client.set_value"
CUSTOMER = {("GET", "/helpdesk/my-tickets"): (200, PORTAL), ("GET", "/api/method/frappe.auth.get_logged_user"): (403, "{}"),
            ("POST", ACCEPT): NOT_PERMITTED, ("POST", TICKET_CALL): (200, '{"message": {"name": 41}}'),
            ("GET", OPTIONS): (200, '{"message": [{"name": "Fixed"}]}'), ("POST", RATE): (200, '{"message": {}}')}


def _customer(name: str, routes: dict, stdin: str = COOKIE + "\n", **env: str) -> subprocess.CompletedProcess:
    server = _listener(routes)
    try:
        return _python_step(_after(name), server.server_port, "127.0.0.1:8080", stdin, **env)
    finally:
        server.shutdown()


def test_the_guard_step_wants_the_helpdesks_403_for_a_call_outside_its_list():
    run = _customer("guard_refuses_a_call_outside_the_list", CUSTOMER)
    assert run.returncode == 0, run.stderr
    assert "guard refused get_logged_user" in run.stdout
    assert [headers["cookie"] for _, _, headers, _ in _Listener.seen] == ["sid=zzz-session; csrf=qqq"]
    run = _customer("guard_refuses_a_call_outside_the_list",
                    {**CUSTOMER, ("GET", "/api/method/frappe.auth.get_logged_user"): (200, '{"message": "ana"}')})
    assert run.returncode != 0 and "answered HTTP 200 to the customer" in run.stderr


@pytest.mark.parametrize("name", ["guard_refuses_a_call_outside_the_list", "portal_opens_and_forms_are_refused",
                                  "customer_raises_and_rates_a_ticket"])
def test_a_customer_step_without_the_cookie_line_ends_before_asking_the_site(name):
    run = _customer(name, CUSTOMER, stdin="")
    assert run.returncode != 0 and "no Cookie line" in run.stderr
    assert _Listener.seen == []


def test_no_customer_step_prints_the_cookies():
    for name in ("guard_refuses_a_call_outside_the_list", "portal_opens_and_forms_are_refused",
                 "customer_raises_and_rates_a_ticket"):
        run = _customer(name, {**CUSTOMER, ("GET", "/api/method/frappe.auth.get_logged_user"): (200, "x"),
                               ("GET", "/helpdesk/my-tickets"): (500, "x")})
        assert run.returncode != 0
        assert "zzz-session" not in run.stdout + run.stderr


@pytest.mark.parametrize("forms", [None, "off"])
def test_the_portal_step_opens_the_portal_and_has_the_customers_accept_refused(forms):
    run = _customer("portal_opens_and_forms_are_refused", CUSTOMER, **({} if forms is None else {"ERPNEXT_WEB_FORMS": forms}))
    assert run.returncode == 0, run.stderr
    assert "portal ok: forms refused to the customer" in run.stdout
    posts = [(path, headers["x-frappe-csrf-token"]) for method, path, headers, _ in _Listener.seen if method == "POST"]
    assert posts == [(ACCEPT, "tok123")] * 2


def test_the_portal_step_leaves_the_forms_unchecked_while_they_are_on():
    run = _customer("portal_opens_and_forms_are_refused", CUSTOMER, ERPNEXT_WEB_FORMS="on")
    assert run.returncode == 0, run.stderr
    assert "forms not checked" in run.stdout
    assert [method for method, *_ in _Listener.seen] == ["GET"]


@pytest.mark.parametrize("damage, why", [
    ({("GET", "/helpdesk/my-tickets"): (403, "")}, "portal answered HTTP 403"),
    ({("GET", "/helpdesk/my-tickets"): (302, "/login")}, "portal answered HTTP"),
    ({("GET", "/helpdesk/my-tickets"): (200, "<html></html>")}, "no csrf_token"),
    ({("POST", ACCEPT): (200, "{}")}, "accept on the issues form got HTTP 200"),
])
def test_the_portal_step_fails_on_a_closed_portal_or_an_open_form(damage, why):
    run = _customer("portal_opens_and_forms_are_refused", {**CUSTOMER, **damage})
    assert run.returncode != 0 and why in run.stderr


def test_the_ticket_step_raises_the_ticket_and_rates_it_as_the_customer():
    run = _customer("customer_raises_and_rates_a_ticket", CUSTOMER)
    assert run.returncode == 0, run.stderr
    assert "ticket 41 raised and rated by the customer" in run.stdout
    calls = {path: (headers, body) for _, path, headers, body in _Listener.seen}
    assert json.loads(calls[TICKET_CALL][1])["doc"]["subject"] == "Bench ticket ana@client.example"
    rated = json.loads(calls[RATE][1])
    assert (rated["doctype"], rated["name"], rated["fieldname"]["status"], rated["fieldname"]["feedback"]) == (
        "HD Ticket", 41, "Closed", "Fixed")
    assert calls[TICKET_CALL][0]["x-frappe-csrf-token"] == calls[RATE][0]["x-frappe-csrf-token"] == "tok123"


@pytest.mark.parametrize("damage, why", [
    ({("POST", TICKET_CALL): (403, "{}")}, "hd_ticket.api.new answered HTTP 403"),
    ({("POST", TICKET_CALL): (200, '{"message": {}}')}, "returned no ticket name"),
    ({("GET", OPTIONS): (200, '{"message": []}')}, "lists no feedback option"),
    ({("POST", RATE): (417, "{}")}, "feedback on ticket 41 answered HTTP 417"),
])
def test_the_ticket_step_fails_when_a_call_of_the_customers_is_refused(damage, why):
    run = _customer("customer_raises_and_rates_a_ticket", {**CUSTOMER, **damage})
    assert run.returncode != 0 and why in run.stderr


DESK_STUB = '''
import json
import os

_path = os.environ["STUB_STATE"]
_state = json.load(open(_path))


class _Db:
    def exists(self, doctype, name):
        return name in _state.get(doctype, {})

    def commit(self):
        json.dump(_state, open(_path, "w"))


db = _Db()


def init(site):
    assert site == os.environ["SITE_NAME"]


def connect():
    pass


def destroy():
    pass


def get_all(doctype, filters, pluck):
    if doctype == "Has Role":
        assert (filters["parenttype"], pluck) == ("User", "role")
        return list(_state.get("Roles", {}).get(filters["parent"], []))
    assert (doctype, pluck) == ("HD Ticket", "name")
    return [name for name, doc in _state.get(doctype, {}).items() if doc["subject"] == filters["subject"]]


def delete_doc(doctype, name, force):
    assert force == 1
    _state[doctype].pop(name)
'''


def _desk(step: dict, tmp_path: Path, state: dict) -> tuple[subprocess.CompletedProcess, dict]:
    (tmp_path / "stubs").mkdir(exist_ok=True)
    (tmp_path / "stubs" / "frappe.py").write_text(DESK_STUB, encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps(state))
    shell, flag, body = [part.replace("{{ sign_in_email }}", "ana@client.example") for part in step["command"]]
    body = body.replace("/home/frappe/frappe-bench/sites", str(tmp_path)).replace("../env/bin/python", sys.executable)
    run = subprocess.run([shell, flag, body], capture_output=True, text=True, timeout=30,
                         env={"PATH": os.environ["PATH"], "PYTHONPATH": str(tmp_path / "stubs"), "SITE_NAME": SITE_NAME,
                              "STUB_STATE": str(tmp_path / "state.json")})
    return run, json.loads((tmp_path / "state.json").read_text())


@pytest.mark.parametrize("roles, passes", [(["HD Customer"], True), (["Customer", "Sales User"], False), ([], False)])
def test_the_role_step_passes_only_while_the_user_holds_hd_customer(roles, passes, tmp_path):
    run, _ = _desk(_after("hd_customer_role_held"), tmp_path, {"Roles": {"ana@client.example": roles}})
    assert (run.returncode == 0) == passes, run.stderr
    assert ("hd customer role held by ana@client.example" in run.stdout) == passes


def test_the_cleanup_deletes_the_customers_tickets_and_the_user_and_nothing_else(tmp_path):
    step = _sign_in_entry("erpnext-helpdesk", "client_customer")["cleanup"][0]
    state = {"User": {"ana@client.example": {}, "bo@client.example": {}},
             "HD Ticket": {"41": {"subject": "Bench ticket ana@client.example"},
                           "42": {"subject": "Bench ticket bo@client.example"}, "7": {"subject": "catena-r1"}}}
    run, after = _desk(step, tmp_path, state)
    assert run.returncode == 0, run.stderr
    assert after["User"] == {"bo@client.example": {}}
    assert set(after["HD Ticket"]) == {"42", "7"}
    run, again = _desk(step, tmp_path, after)
    assert run.returncode == 0, run.stderr
    assert again == after


# test.yml: the sign_in entries.

SIGN_IN_EMAIL = "ana@client.example"
SIGN_IN_STUB = '''
import json
import os

_path = os.environ["STUB_STATE"]
_state = json.load(open(_path))


def _save():
    json.dump(_state, open(_path, "w"))


class _Db:
    def get_value(self, doctype, name, fieldname):
        assert (doctype, fieldname) == ("User", "enabled")
        return _state["users"].get(name, {}).get("enabled")

    def commit(self):
        _save()


db = _Db()


def init(site):
    assert site == os.environ["SITE_NAME"]


def connect():
    pass


def destroy():
    pass


def get_all(doctype, filters, pluck):
    assert (doctype, filters["parenttype"], pluck) == ("Has Role", "User", "role")
    return list(_state["users"].get(filters["parent"], {}).get("roles", []))


def delete_doc(doctype, name, force):
    assert (doctype, force) == ("User", 1)
    _state["users"].pop(name, None)
'''


def _sign_in_entries(slug: str) -> dict[str, dict]:
    return {entry["name"]: entry for entry in _test_yml(slug)["sign_in"]}


def _run_sign_in_step(step: dict, tmp_path: Path, users: dict) -> tuple[subprocess.CompletedProcess, dict]:
    (tmp_path / "stubs").mkdir(exist_ok=True)
    (tmp_path / "stubs" / "frappe.py").write_text(SIGN_IN_STUB, encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"users": users}))
    shell, flag, body = [part.replace("{{ sign_in_email }}", SIGN_IN_EMAIL) for part in step["command"]]
    body = body.replace("/home/frappe/frappe-bench/sites", str(tmp_path)).replace("../env/bin/python", sys.executable)
    run = subprocess.run([shell, flag, body], capture_output=True, text=True, timeout=30,
                         env={"PATH": os.environ["PATH"], "PYTHONPATH": str(tmp_path / "stubs"),
                              "SITE_NAME": "erp.a.example", "STUB_STATE": str(state)})
    return run, json.loads(state.read_text())["users"]


def _step(entry: str, phase: str) -> dict:
    [step] = _sign_in_entries("erpnext")[entry][phase]
    return step


def test_the_plain_app_declares_a_refused_staff_account_and_the_customer_level():
    entries = _sign_in_entries("erpnext")
    assert list(entries) == ["unknown_staff_refused", "customer_access"]
    assert {key: value for key, value in entries["unknown_staff_refused"].items() if key != "name"} == {
        "group": "staff", "expect": "refused", "status": 403}
    customer = entries["customer_access"]
    assert (customer["group"], customer["access"], customer["facts"]) == ("client", "customer", {"system_user": "no"})
    assert customer["access"] in _entry("erpnext").block["access"]
    assert set(customer) == {"name", "group", "access", "facts", "granted", "removed", "cleanup"}


def test_every_sign_in_step_is_a_docker_exec_into_the_backend_naming_the_accounts_email():
    steps = [step for phase in ("granted", "removed", "cleanup")
             for step in _sign_in_entries("erpnext")["customer_access"][phase]]
    names = [step["name"] for step in steps]
    assert len(names) == len(set(names)) == 3
    for step in steps:
        assert step["type"] == "docker_exec" and step["container"] == "{{ container_backend }}"
        assert "{{ sign_in_email }}" in step["command"][2]
        assert "{{ sign_in_email }}" in step["expect_stdout_contains"]
        assert "CATENA_TEST_PASSWORD" not in " ".join(step["command"])
        subprocess.run(["bash", "-n", "-c", step["command"][2]], check=True)


def test_the_granted_step_passes_only_while_the_user_holds_the_customer_role(tmp_path):
    step = _step("customer_access", "granted")
    run, _ = _run_sign_in_step(step, tmp_path, {SIGN_IN_EMAIL: {"enabled": 1, "roles": ["Customer"]}})
    assert run.returncode == 0, run.stderr
    assert f"customer role held by {SIGN_IN_EMAIL}" in run.stdout
    for users in ({}, {SIGN_IN_EMAIL: {"enabled": 1, "roles": ["Sales User"]}}):
        run, _ = _run_sign_in_step(step, tmp_path, users)
        assert run.returncode != 0 and "expected Customer" in run.stderr


@pytest.mark.parametrize("user, passes", [
    ({"enabled": 0, "roles": []}, True),
    ({"enabled": 0, "roles": ["Sales User"]}, True),
    ({"enabled": 1, "roles": []}, False),
    ({"enabled": 0, "roles": ["Customer"]}, False),
    (None, False),
])
def test_the_removed_step_wants_a_disabled_user_without_the_customer_role(user, passes, tmp_path):
    run, _ = _run_sign_in_step(_step("customer_access", "removed"), tmp_path,
                               {} if user is None else {SIGN_IN_EMAIL: user})
    assert (run.returncode == 0) == passes, run.stderr
    assert (f"customer closed for {SIGN_IN_EMAIL}" in run.stdout) == passes


def test_the_cleanup_step_deletes_the_user_and_only_that_user(tmp_path):
    other = {"enabled": 1, "roles": []}
    run, users = _run_sign_in_step(_step("customer_access", "cleanup"), tmp_path,
                                   {SIGN_IN_EMAIL: {"enabled": 0, "roles": []}, "other@x.example": other})
    assert run.returncode == 0, run.stderr
    assert f"user deleted {SIGN_IN_EMAIL}" in run.stdout
    assert users == {"other@x.example": other}
