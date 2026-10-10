"""Unit tests for the registry importer's Dokploy adapter
(lib/importers/dokploy.py), through the pipeline it shares.

The network is a fixture: every address the importer asks for is answered
from WEB below, and anything else is a 404. No checker runs, so each entry
records that it did not.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, readme, render  # noqa: E402
from lib.importers import dokploy, pipeline  # noqa: E402

ADAPTERS = {"dokploy": dokploy.read}
REGISTRY = "https://api.github.com/repos/example/templates/git/trees/abc123?recursive=1"
RAW = "https://raw.githubusercontent.com/example/templates/abc123/blueprints"
API = pipeline.GITHUB_API
ZONE = "{{ cloudflare_zone }}"


def _licence(repo: str, spdx: str, text: str) -> bytes:
    """GitHub's answer to GET /repos/{owner}/{repo}/license."""
    return json.dumps({
        "html_url": f"https://github.com/{repo}/blob/main/LICENSE",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "license": {"spdx_id": spdx},
    }).encode()


def _meta(ident: str, name: str, github: str, website: str = "") -> bytes:
    return json.dumps({"id": ident, "name": name, "version": "1.0.0",
                       "description": f"{name} {chr(0x2014)} for tests", "logo": "x.svg",
                       "links": {"github": github, "website": website, "docs": ""},
                       "tags": ["productivity"]}).encode()


WEBAPP_TOML = '''\
[variables]
main_domain = "${domain}"
api_domain = "${domain}"
db_password = "${password:24}"
secret_key = "${base64:32}"
vault_key = "${base64:48}"
token = "${hash:12}"
admin_user = "${username}"
admin_email = "${email}"
instance = "${uuid}"
release = "${timestamps:2030-01-01T00:00:00Z}"

[config]
env = [
  "# a comment",
  "",
  "APP_HOST=${main_domain}",
  "APP_URL=http://${main_domain}",
  "API_URL=https://${api_domain}",
  "DB_PASSWORD=${db_password}",
  "DATABASE_URL=postgres://app:${db_password}@db:5432/app",
  "POSTGRES_PASSWORD=${db_password}",
  "SECRET_KEY=${secret_key}",
  "VAULT_KEY=${vault_key}",
  "SESSION_TOKEN=${token}",
  "ADMIN_USER=${admin_user}",
  "ADMIN_EMAIL=${admin_email}",
  "INSTANCE_ID=${instance}",
  "RELEASE=${release}",
  "PLUGIN=${weird:5}",
  "COOKIE=${password}",
]

[[config.domains]]
serviceName = "web"
port = 3_000
host = "${main_domain}"

[[config.domains]]
serviceName = "api"
port = 8080
host = "${api_domain}"
path = "/api"

[[config.mounts]]
filePath = "/config/app.yml"
content = """
a: 1
b: 2
"""
'''

WEBAPP_COMPOSE = """\
services:
  web:
    image: example/web:1.4.2
    restart: unless-stopped
    expose:
      - 3000
    env_file:
      - .env
    environment:
      MODE: production
  api:
    image: example/api:1.4.2
    restart: unless-stopped
    environment:
      DATABASE_URL: ${DATABASE_URL}
      KEY: ${SECRET_KEY}
      PRICE: $$5
    volumes:
      - ../files/config/app.yml:/etc/app.yml:ro
  db:
    image: postgres:16
    restart: always
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - db:/var/lib/postgresql/data
volumes:
  db:
"""

# A template with no address, whose compose publishes nothing to route.
WORKER_TOML = '[variables]\n\n[config]\n[config.env]\nQUEUE = "jobs"\n'
WORKER_COMPOSE = "services:\n  worker:\n    image: example/worker:2.0.1\n"

BLUEPRINTS = {
    "webapp": ("Web App", "https://github.com/example/webapp", "https://webapp.example.org"),
    "copyleft": ("Copyleft", "https://github.com/example/gpl-app", ""),
    "offsite": ("Offsite", "https://gitlab.com/example/app", ""),
    "bare": ("Bare", "https://github.com/example/bare", ""),
    "worker": ("Worker", "https://github.com/example/tool", ""),
}
TREE = {"sha": "def456", "truncated": False, "tree": [
    {"path": "README.md", "type": "blob"},
    {"path": "blueprints/half/meta.json", "type": "blob"},
] + [{"path": f"blueprints/{ident}/{name}", "type": "blob"}
     for ident in BLUEPRINTS for name in dokploy.FILES]}

WEB = {
    REGISTRY: json.dumps(TREE).encode(),
    f"{API}/repos/example/templates/license": _licence(
        "example/templates", "MIT", "MIT License\n\nCopyright (c) 2024 Example Templates\n"),
    f"{API}/repos/example/webapp/license": _licence(
        "example/webapp", "MIT", "MIT License\n\nCopyright (c) 2025 Example Web\n"),
    f"{API}/repos/example/gpl-app/license": _licence("example/gpl-app", "GPL-3.0", "GNU GPL\n"),
    f"{API}/repos/example/tool/license": _licence(
        "example/tool", "Apache-2.0", "Apache License\n"),
    f"{RAW}/webapp/template.toml": WEBAPP_TOML.encode(),
    f"{RAW}/webapp/docker-compose.yml": WEBAPP_COMPOSE.encode(),
    f"{RAW}/worker/template.toml": WORKER_TOML.encode(),
    f"{RAW}/worker/docker-compose.yml": WORKER_COMPOSE.encode(),
    f"{RAW}/half/meta.json": _meta("half", "Half", "https://github.com/example/webapp"),
}
for _ident, (_name, _github, _website) in BLUEPRINTS.items():
    WEB[f"{RAW}/{_ident}/meta.json"] = _meta(_ident, _name, _github, _website)
    WEB.setdefault(f"{RAW}/{_ident}/template.toml", WORKER_TOML.encode())
    WEB.setdefault(f"{RAW}/{_ident}/docker-compose.yml", WORKER_COMPOSE.encode())


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """An empty catalog tree the importer writes into and the loader reads
    back, a banned-word list, and the fixture network."""
    sources = tmp_path / "sources"
    sources.mkdir()
    blueprints = tmp_path / "blueprints"
    blueprints.mkdir()
    (sources / model.META_NAME).write_text(json.dumps({
        "order": [],
        "postgres_default_image": "postgres:18.6-alpine",
        "sizing": {"last_measured_at": "x", "measurement_host": "x", "measurement_method": "x"},
    }, indent=2) + "\n")
    banned = tmp_path / "banned-words.json"
    banned.write_text(json.dumps({"tokens": [{"token": "forbiddenware", "stem": False}]}))
    monkeypatch.setattr(model, "SOURCES", sources)
    monkeypatch.setattr(model, "BLUEPRINTS", blueprints)
    monkeypatch.setattr(pipeline, "BANNED_WORDS", banned)
    monkeypatch.delenv(pipeline.APPLINT_ENV, raising=False)
    calls: list[str] = []

    def fake_fetch(url, headers=None):
        calls.append(url)
        if url not in WEB:
            raise pipeline.FetchError(f"{url}: HTTP 404", 404)
        return WEB[url]

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    return calls


def _run(*args: str) -> int:
    return pipeline.main([REGISTRY, "--format", "dokploy", *args], ADAPTERS)


def _env(slug: str) -> dict[str, str]:
    source = json.loads((model.SOURCES / f"{slug}.json").read_text())
    return dict(kv.split("=", 1) for kv in source["x-catena"]["env_defaults"])


def _cat(slug: str) -> dict:
    return json.loads((model.SOURCES / f"{slug}.json").read_text())["x-catena"]


def _compose(slug: str) -> dict:
    return yaml.safe_load((model.BLUEPRINTS / slug / model.COMPOSE_NAME).read_text())


def test_generators_become_minted_settings(catalog):
    assert _run() == 0
    env = _env("webapp")
    assert env["DB_PASSWORD"] == pipeline.lookup(24, lower=True)
    assert env["COOKIE"] == pipeline.lookup(16, lower=True)
    # 32 bytes: ten groups of three, then two zero bytes.
    assert env["SECRET_KEY"] == pipeline.lookup(40) + "AAA="
    assert env["VAULT_KEY"] == pipeline.lookup(64)
    assert env["SESSION_TOKEN"] == pipeline.lookup(12, "hexdigits", lower=True)
    assert env["ADMIN_USER"] == "u" + pipeline.lookup(11, lower=True)
    assert env["ADMIN_EMAIL"] == "{{ admin_email }}"
    assert env["RELEASE"] == "1893456000"
    assert env["APP_HOST"] == f"webapp.{ZONE}"
    assert env["APP_URL"] == f"http://webapp.{ZONE}"
    assert env["API_URL"] == f"https://webapp-api.{ZONE}"
    # A generated value the public catalog collapses to its sentinel.
    assert render.strip_jinja_for_portainer(env["DB_PASSWORD"]) == render.SENTINEL_MANAGED
    to_choose = _cat("webapp")["pending"]["to_choose"]
    assert any(c.startswith("minted on the host: DB_PASSWORD, SECRET_KEY, VAULT_KEY, "
                            "SESSION_TOKEN, ADMIN_USER, COOKIE") for c in to_choose)
    assert "ADMIN_EMAIL: the server's admin email stands in for the address the registry " \
           "makes up" in to_choose
    assert any(c.startswith("APP_URL: the registry's address starts with http://")
               for c in to_choose)


@pytest.mark.parametrize("size", [1, 2, 3, 16, 32, 48, 64])
def test_a_base64_value_decodes_to_the_size_the_registry_names(size):
    atom = dokploy._base64("k", size)
    if size < 3:
        assert atom.kind == "choose"
        return
    length = int(render.LOOKUP_PASSWORD_RE.search(atom.value).group(1))
    sample = render.LOOKUP_PASSWORD_RE.sub(("aZ9" * length)[:length], atom.value)
    assert len(base64.b64decode(sample, validate=True)) == size


def test_a_value_two_lines_read_stays_one_setting(catalog):
    assert _run() == 0
    env = _env("webapp")
    assert "DATABASE_URL" not in env and "POSTGRES_PASSWORD" not in env
    services = _compose("webapp")["services"]
    # The env file's lines are the web service's own, and the combined ones read
    # the password's one setting.
    web = services["web"]
    assert "env_file" not in web
    assert web["environment"]["MODE"] == "production"
    assert web["environment"]["APP_HOST"] == "${APP_HOST}"
    assert web["environment"]["DATABASE_URL"] == "postgres://app:${DB_PASSWORD}@db:5432/app"
    assert web["environment"]["POSTGRES_PASSWORD"] == "${DB_PASSWORD}"
    api = services["api"]["environment"]
    assert api["DATABASE_URL"] == "postgres://app:${DB_PASSWORD}@db:5432/app"
    assert api["KEY"] == "${SECRET_KEY}"
    assert api["PRICE"] == "$$5"
    assert services["db"]["environment"]["POSTGRES_PASSWORD"] == "${DB_PASSWORD}"
    to_choose = _cat("webapp")["pending"]["to_choose"]
    assert any(c.startswith("written into the compose file") and
               c.endswith(": DATABASE_URL, POSTGRES_PASSWORD") for c in to_choose)


def test_a_shared_value_no_line_holds_alone_gets_a_setting_of_its_own():
    t = dokploy._Template("app", {"pw": "${password:20}"},
                          {"A_URL": "redis://:${pw}@a", "B_URL": "redis://:${pw}@b"}, [])
    settings, inline, _ = t.settings()
    assert settings == [("PW", pipeline.lookup(20, lower=True))]
    assert inline == {"A_URL": "redis://:${PW}@a", "B_URL": "redis://:${PW}@b"}


def test_domains_become_the_route_and_further_ones_a_choice(catalog):
    assert _run() == 0
    cat = _cat("webapp")
    assert cat["domain"] == {"host": f"webapp.{ZONE}", "service": "web", "port": 3000}
    assert cat["upstream_url"] == "https://webapp.example.org"
    assert cat["en"]["what_it_is"] == "Web App -- for tests"
    web = _compose("webapp")["services"]["web"]
    assert web["labels"] == ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=3000"]
    to_choose = cat["pending"]["to_choose"]
    assert to_choose[0] == "route: service web, port 3000, the registry's address for the app"
    assert ("another address: service api, port 8080, host webapp-api.<domain>, path /api. It "
            "gets no route: catena-admin routes several addresses of one app (vps.route.main), "
            "and no curated entry uses that yet") in to_choose
    assert not any("vps.route.main" in label for svc in _compose("webapp")["services"].values()
                   for label in svc.get("labels") or [])
    assert any(c.startswith("config file /config/app.yml: the registry writes it beside the "
                            "stack (its template.toml holds the 2 lines)") for c in to_choose)


def test_an_unknown_generator_is_a_choice(catalog):
    assert _run() == 0
    env = _env("webapp")
    assert env["PLUGIN"] == "" and env["INSTANCE_ID"] == ""
    to_choose = _cat("webapp")["pending"]["to_choose"]
    assert "PLUGIN: it reads ${weird:5}, which the registry defines nowhere: set it by hand" \
        in to_choose
    assert ("INSTANCE_ID: the registry generates a random UUID, which the host does not mint: "
            "set it by hand") in to_choose


def test_the_apps_licence_admits_an_entry_and_the_registrys_notice_joins_it(catalog, capsys):
    calls = catalog
    assert _run() == 0
    origin = _cat("webapp")["origin"]
    assert origin == {
        "registry": REGISTRY,
        "entry": "Web App",
        "source": "https://github.com/example/templates/tree/abc123/blueprints/webapp",
        "licence": "MIT",
        "attribution": "Copyright (c) 2025 Example Web; compose file and settings, MIT: "
                       "Copyright (c) 2024 Example Templates",
    }
    out = capsys.readouterr().out
    assert "skipped, licence not permitted: 1\n  Copyleft: GPL-3.0 (example/gpl-app)" in out
    assert "skipped, licence unknown: 2" in out
    assert ("Offsite: its upstream https://gitlab.com/example/app is not a GitHub repository, so "
            "the app's licence is unknown") in out
    assert "Bare: GitHub finds no licence file in example/bare" in out
    assert "half: the registry has no template.toml or docker-compose.yml for it" in out
    assert "Worker: no service publishes a TCP port to route" in out
    # The compose file of an entry the licence keeps out is never fetched.
    for slug in ("copyleft", "offsite", "bare"):
        assert f"{RAW}/{slug}/docker-compose.yml" not in calls
        assert not (model.SOURCES / f"{slug}.json").exists()


def test_a_registry_whose_own_licence_is_not_permitted_is_refused(catalog, capsys, monkeypatch):
    monkeypatch.setitem(WEB, f"{API}/repos/example/templates/license",
                        _licence("example/templates", "GPL-3.0", "GNU GPL\n"))
    assert _run() == 1
    assert "the registry's own licence, GPL-3.0, is not one the catalog imports from" in \
        capsys.readouterr().err


def test_a_document_that_is_not_a_tree_listing_is_refused(catalog, capsys, monkeypatch):
    monkeypatch.setitem(WEB, REGISTRY, b'{"version": "3", "templates": []}')
    assert _run() == 1
    assert "cannot read the registry: not GitHub's file listing" in capsys.readouterr().err


def test_a_dry_run_writes_nothing(catalog, capsys):
    meta = (model.SOURCES / model.META_NAME).read_text()
    assert _run("--dry-run") == 0
    out = capsys.readouterr().out
    assert "entries: 6" in out and "would import: 1\n  webapp (Web App)" in out
    assert [p.name for p in model.SOURCES.iterdir()] == [model.META_NAME]
    assert list(model.BLUEPRINTS.iterdir()) == []
    assert (model.SOURCES / model.META_NAME).read_text() == meta


def test_the_imported_entry_validates_and_renders(catalog):
    assert _run() == 0
    entries = {e.slug: e for e in model.load_sources()}
    assert set(entries) == {"webapp"}
    entry = entries["webapp"]
    assert render.render_catalog_entry(entry)["status"] == "imported"
    template = render.render_portainer_template(entry, "logo.svg")
    assert model.validate_output({"version": "3", "templates": [template]}) == []
    assert all("{{" not in field["default"] for field in template["env"])
    assert "**Imported, untested.**" in readme.render_readme(entry)
