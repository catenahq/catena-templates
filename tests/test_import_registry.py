"""Unit tests for the registry importer (lib/importers/) and the imported
tier of the catalog it writes.

The network is a fixture: every address the importer asks for is answered
from WEB below, and anything else is a 404. The checker is a stub script
that corrects one line and reports one finding it corrected and one it
leaves, so the suite needs neither Go nor the catena-admin checkout.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint, readme, render  # noqa: E402
from lib.importers import pipeline, portainer  # noqa: E402

ADAPTERS = {"portainer": portainer.read}
REGISTRY = "https://raw.githubusercontent.com/example/registry/main/templates.json"
API = pipeline.GITHUB_API
# A banned-word list in the shape of contracts/banned-words.json, with words
# of its own so this file holds none of the real list's.
BANNED = {"tokens": [{"token": "forbiddenware", "stem": False}, {"token": "badstem", "stem": True}]}


def _licence(repo: str, spdx: str, text: str) -> bytes:
    """GitHub's answer to GET /repos/{owner}/{repo}/license."""
    return json.dumps({
        "html_url": f"https://github.com/{repo}/blob/main/LICENSE",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "license": {"spdx_id": spdx},
    }).encode()


STACK_COMPOSE = """\
version: "3.8"
services:
  web:
    image: example/web:2.1.0
    container_name: web
    restart: unless-stopped
    depends_on:
      - db
    ports:
      - "8080:80"
      - 22:22
    environment:
      DB_PASSWORD: ${DB_PASSWORD}
      APP_PORT: ${APP_PORT:-80}
      ENABLE: yes
    labels:
      - traefik.enable=true
      - traefik.http.routers.web.rule=Host(`web.example.com`)
  db:
    image: postgres:16
    restart: always
    environment:
      POSTGRES_PASSWORD: example
      POSTGRES_PASSWORD_FILE: /run/secrets/db
    volumes:
      - db:/var/lib/postgresql/data
volumes:
  db:
"""

REGISTRY_DOC = {"version": "3", "templates": [
    {"id": 1, "type": 3, "title": "Web Stack", "description": "A web app \u2014 with its database",
     "categories": ["Web"], "platform": "linux",
     "repository": {"url": "https://github.com/example/stacks",
                    "stackfile": "./web/docker-compose.yml"},
     "env": [{"name": "DB_PASSWORD", "label": "Database password"},
             {"name": "SITE_TITLE", "label": "Title", "default": "Hello"}]},
    {"id": 2, "type": 1, "title": "Notes", "name": "notes", "description": "Notes app",
     "categories": ["Productivity"], "platform": "linux",
     "image": "example/notes:1.2.3", "ports": ["8080:3000/tcp", "53/udp"],
     "volumes": [{"container": "/data"},
                 {"container": "/config", "bind": "/opt/notes", "readonly": True}],
     "env": [{"name": "ADMIN_PASSWORD", "label": "Admin password"},
             {"name": "APP_SECRET_KEY", "label": "Key"},
             {"name": "TZ", "label": "Time zone", "default": "UTC"},
             {"name": "MODE", "label": "Mode", "select": [
                 {"text": "A", "value": "a"}, {"text": "B", "value": "b", "default": True}]}],
     "labels": [{"name": "traefik.enable", "value": "true"},
                {"name": "com.example.team", "value": "notes"}],
     "restart_policy": "unless-stopped"},
    {"id": 3, "type": 2, "title": "Copyleft", "description": "x",
     "repository": {"url": "https://github.com/example/gpl", "stackfile": "docker-compose.yml"}},
    {"id": 4, "type": 2, "title": "No Licence", "description": "x",
     "repository": {"url": "https://github.com/example/bare", "stackfile": "docker-compose.yml"}},
    {"id": 5, "type": 3, "title": "Elsewhere", "description": "x",
     "repository": {"url": "https://gitlab.com/example/app", "stackfile": "docker-compose.yml"}},
]}

WEB = {
    REGISTRY: json.dumps(REGISTRY_DOC).encode(),
    f"{API}/repos/example/registry/license": _licence(
        "example/registry", "MIT", "MIT License\n\nCopyright (c) 2024 Example Registry\n"),
    f"{API}/repos/example/stacks/license": _licence(
        "example/stacks", "Apache-2.0",
        "Apache License\n\n      Copyright [yyyy] [name of copyright owner]\n"),
    f"{API}/repos/example/gpl/license": _licence("example/gpl", "GPL-3.0", "GNU GPL\n"),
    "https://raw.githubusercontent.com/example/stacks/HEAD/web/docker-compose.yml":
        STACK_COMPOSE.encode(),
}

# A checker that deletes the container_name line, reports that as corrected,
# and reports two Docker socket mounts it cannot correct. Each call appends its
# arguments to calls.jsonl beside it.
STUB_CHECKER = """\
#!{python}
import json, pathlib, sys
args = sys.argv[1:]
with open({calls!r}, "a") as out:
    out.write(json.dumps(args) + "\\n")
path = pathlib.Path(args[-1])
path.write_text("".join(l for l in path.read_text().splitlines(True) if "container_name" not in l))
print(json.dumps([{{"file": str(path), "name": args[args.index("--name") + 1], "findings": [
    {{"rule": "S2", "severity": "warn", "service": "web", "message": "container_name goes",
     "fixed": True}},
    {{"rule": "X6", "severity": "error", "service": "web",
     "message": "Service web mounts the Docker socket", "fixed": False}},
    {{"rule": "X6", "severity": "error", "service": "web",
     "message": "Service web mounts the Docker socket (/run)", "fixed": False}}]}}]))
sys.exit(1)
"""


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """An empty catalog tree the importer writes into and the loader reads
    back, the fixture banned-word list, and the fixture network."""
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
    banned.write_text(json.dumps(BANNED))
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
    return tmp_path, calls


def _run(*args: str) -> int:
    return pipeline.main([REGISTRY, *args], ADAPTERS)


def _source(slug: str) -> dict:
    return json.loads((model.SOURCES / f"{slug}.json").read_text())


def _compose(slug: str) -> dict:
    return yaml.safe_load((model.BLUEPRINTS / slug / model.COMPOSE_NAME).read_text())


def _stub_checker(tmp_path: Path) -> Path:
    path = tmp_path / "catena-applint"
    path.write_text(STUB_CHECKER.format(python=sys.executable, calls=str(tmp_path / "calls.jsonl")))
    path.chmod(0o755)
    return path


def test_a_stack_entry_is_imported_from_its_repository(catalog):
    assert _run() == 0
    cat = _source("web-stack")["x-catena"]
    assert cat["status"] == "imported"
    assert cat["origin"] == {
        "registry": REGISTRY,
        "entry": "Web Stack",
        "source": "https://github.com/example/stacks",
        "licence": "Apache-2.0",
        # The Apache text carries only its appendix's line to fill in.
        "attribution": "https://github.com/example/stacks/blob/main/LICENSE",
    }
    assert cat["domain"] == {"host": "web-stack.{{ cloudflare_zone }}", "service": "web", "port": 80}
    assert cat["en"]["what_it_is"] == "A web app -- with its database"

    compose = _compose("web-stack")
    assert "version" not in compose
    web, db = compose["services"]["web"], compose["services"]["db"]
    assert web["labels"] == ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=80"]
    # The routed port goes, and a short port reads as compose reads it.
    assert web["ports"] == ["22:22"]
    assert web["environment"]["ENABLE"] == "yes"
    for service in (web, db):
        assert service["deploy"]["restart_policy"]["condition"] == "any"
        assert "restart" not in service

    pending = cat["pending"]
    assert pending["to_choose"][0].startswith("route: service web, port 80")
    assert "Traefik labels are dropped" in pending["to_choose"][0]
    assert any(c.startswith("ports service web publishes on the server (22:22)")
               for c in pending["to_choose"])
    assert any("db sets POSTGRES_PASSWORD to a fixed value" in c for c in pending["to_choose"])
    assert not any("POSTGRES_PASSWORD_FILE" in c for c in pending["to_choose"])
    assert any("pins 'postgres:16'" in f for f in pending["findings"])
    assert any("depends_on" in f for f in pending["findings"])


def test_a_single_container_entry_becomes_a_one_service_compose(catalog):
    assert _run() == 0
    compose = _compose("notes")
    assert list(compose["services"]) == ["notes"]
    notes = compose["services"]["notes"]
    assert notes["image"] == "example/notes:1.2.3"
    assert notes["environment"] == {
        "ADMIN_PASSWORD": "${ADMIN_PASSWORD}", "APP_SECRET_KEY": "${APP_SECRET_KEY}",
        "TZ": "${TZ}", "MODE": "${MODE}"}
    assert notes["volumes"] == ["data:/data", "/opt/notes:/config:ro"]
    assert compose["volumes"] == {"data": None}
    assert notes["labels"] == [
        "com.example.team=notes", "vps.route.host=${DOMAIN_HOST}", "vps.route.port=3000"]
    assert notes["ports"] == ["53/udp"]
    assert notes["deploy"]["restart_policy"]["condition"] == "any"

    cat = _source("notes")["x-catena"]
    assert cat["upstream_url"] == "https://hub.docker.com/r/example/notes"
    assert cat["origin"]["source"] == REGISTRY
    assert cat["origin"]["licence"] == "MIT"
    assert cat["origin"]["attribution"] == "Copyright (c) 2024 Example Registry"
    assert "MODE=b" in cat["env_defaults"]
    to_choose = cat["pending"]["to_choose"]
    assert any(c.startswith("bind mount /opt/notes in service notes") for c in to_choose)
    assert any(c.startswith("ports service notes publishes on the server (53/udp)") for c in to_choose)


def test_a_licence_outside_the_permitted_list_is_skipped(catalog, capsys):
    assert _run() == 0
    assert not (model.SOURCES / "copyleft.json").exists()
    out = capsys.readouterr().out
    assert "skipped, licence not permitted: 1\n  Copyleft: GPL-3.0 (example/gpl)" in out


def test_an_unknown_licence_is_skipped_and_listed(catalog, capsys):
    _, calls = catalog
    assert _run() == 0
    for slug in ("no-licence", "elsewhere"):
        assert not (model.SOURCES / f"{slug}.json").exists()
    out = capsys.readouterr().out
    assert "skipped, licence unknown: 2" in out
    assert "No Licence: GitHub finds no licence file in example/bare" in out
    assert "Elsewhere: the repository https://gitlab.com/example/app is not on GitHub" in out
    # Nothing is fetched for an entry whose licence is unknown.
    assert not any("example/bare/HEAD" in url for url in calls)


def test_settings_named_like_a_password_secret_or_key_are_minted(catalog):
    assert _run() == 0
    notes = dict(kv.split("=", 1) for kv in _source("notes")["x-catena"]["env_defaults"])
    assert list(notes)[0] == "DOMAIN_HOST"
    assert notes["ADMIN_PASSWORD"] == pipeline.minted("ADMIN_PASSWORD")
    assert "length=32" in notes["ADMIN_PASSWORD"]
    assert "length=64" in notes["APP_SECRET_KEY"]
    assert notes["TZ"] == "UTC"
    web = dict(kv.split("=", 1) for kv in _source("web-stack")["x-catena"]["env_defaults"])
    assert web["SITE_TITLE"] == "Hello"
    assert web["APP_PORT"] == "80"
    # The value is one the public catalog collapses to its sentinel, and the
    # host's catalog render mints.
    assert render.strip_jinja_for_portainer(web["DB_PASSWORD"]) == render.SENTINEL_MANAGED

    assert pipeline.secret_like("API_KEY") and pipeline.secret_like("DBPASSWORD")
    for name in ("POSTGRES_PASSWORD_FILE", "MONKEY", "KEYCLOAK_URL", "TZ"):
        assert not pipeline.secret_like(name), name


def test_the_checkers_corrections_are_applied_and_what_it_leaves_is_recorded(catalog):
    tmp_path, _ = catalog
    assert _run("--applint", str(_stub_checker(tmp_path))) == 0
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [c[:6] for c in calls] == [["--fix", "--json", "--nodes", "2", "--name", "catena-web-stack"],
                                      ["--fix", "--json", "--nodes", "2", "--name", "catena-notes"]]
    assert "container_name" not in _compose("web-stack")["services"]["web"]
    pending = _source("web-stack")["x-catena"]["pending"]
    assert "X6 error web: Service web mounts the Docker socket" in pending["findings"]
    assert not any("container_name goes" in f for f in pending["findings"])
    assert not any("checker not run" in f for f in pending["findings"])
    assert pending["to_choose"].count("a feature the security rules refuse: X6 on service web") == 1


def test_without_the_checker_the_import_warns_and_records_it(catalog, capsys):
    tmp_path, _ = catalog
    assert _run("--applint", str(tmp_path / "absent")) == 0
    err = capsys.readouterr().err
    assert "is not an executable" in err and "WARN: no catena-applint" in err
    findings = _source("notes")["x-catena"]["pending"]["findings"]
    assert any(f.startswith("checker not run") for f in findings)


def test_make_lint_refuses_an_error_the_checker_leaves_until_it_is_settled(catalog, capsys):
    """make lint reads the findings as the importer writes them; catena-admin
    CI's catalog check fails on an error."""
    tmp_path, _ = catalog
    assert _run("--applint", str(_stub_checker(tmp_path))) == 0
    capsys.readouterr()
    assert quiesce_lint.lint_all() == 1
    out = capsys.readouterr().out
    assert ("web-stack.pending.findings: 'X6 error web: Service web mounts the Docker socket' is "
            "an app checker error") in out
    assert "did not run" not in out
    for slug in ("web-stack", "notes"):
        path = model.SOURCES / f"{slug}.json"
        doc = json.loads(path.read_text())
        pending = doc["x-catena"]["pending"]
        pending["findings"] = [f for f in pending["findings"] if not f.startswith("X6 error")]
        path.write_text(json.dumps(doc))
    assert quiesce_lint.lint_all() == 0, capsys.readouterr().out


def test_make_lint_refuses_an_entry_the_checker_did_not_run_on(catalog, capsys):
    assert _run() == 0
    capsys.readouterr()
    assert quiesce_lint.lint_all() == 1
    assert ("notes.pending.findings: the app checker did not run on this entry: run catena-applint "
            "--fix --nodes 2 --name catena-notes on its compose") in capsys.readouterr().out


def test_an_entry_whose_title_or_id_holds_a_banned_word_is_skipped(catalog, capsys, monkeypatch):
    notes = REGISTRY_DOC["templates"][1]
    monkeypatch.setitem(WEB, REGISTRY, json.dumps({"version": "3", "templates": [
        dict(notes, title="Forbiddenware Notes"),
        dict(notes, title="Jotter", name="jotter-forbiddenware"),
        dict(notes, title="Forbiddenwares", name="forbiddenwares"),
    ]}).encode())
    assert _run() == 0
    out = capsys.readouterr().out
    assert ("skipped, other reasons: 2\n  Forbiddenware Notes: its title 'Forbiddenware Notes' "
            "holds forbiddenware, a word on the banned-word list check:unicode refuses\n"
            "  Jotter: its id 'jotter-forbiddenware' holds forbiddenware") in out
    assert sorted(p.name for p in model.SOURCES.iterdir()) == [model.META_NAME, "forbiddenwares.json"]


@pytest.mark.parametrize("text,words", [
    ("uptime-forbiddenware", ["forbiddenware"]),
    ("Forbiddenware Monitor", ["forbiddenware"]),
    ("forbiddenwares", []),
    ("forbiddenware_ui", []),
    ("badstemmed", ["badstem"]),
    ("xbadstem", []),
])
def test_a_banned_word_matches_where_the_gate_matches_it(catalog, text, words):
    """The gate's boundary: no letter, digit or underscore before the word,
    nor after it unless it is a stem."""
    assert [word for word, rx in pipeline.banned_words() if rx.search(text)] == words


def test_without_the_banned_word_list_nothing_is_read(catalog, capsys, monkeypatch):
    tmp_path, calls = catalog
    monkeypatch.setattr(pipeline, "BANNED_WORDS", tmp_path / "absent.json")
    assert _run() == 1
    assert "cannot read the banned-word list" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(not pipeline.BANNED_WORDS.is_file(), reason="no contracts checkout beside this one")
def test_the_banned_word_list_is_the_one_the_unicode_gate_reads():
    gate = (pipeline.BANNED_WORDS.parent / "scripts" / "check-unicode.mjs").read_text()
    assert ('join(resolve(dirname(fileURLToPath(import.meta.url)), ".."), "banned-words.json")'
            in gate)
    assert pipeline.banned_words()


def test_the_imported_tier_validates_and_renders(catalog):
    assert _run() == 0
    entries = {e.slug: e for e in model.load_sources()}
    assert set(entries) == {"web-stack", "notes"}
    templates = []
    for entry in entries.values():
        assert entry.imported
        cat = render.render_catalog_entry(entry)
        assert cat["status"] == "imported"
        assert cat["origin"] == entry.origin
        assert cat["bench_pack"] is None and cat["bench_fixture"] == "skip"
        assert cat["sizing"] == {"peak_ram_mb": None}
        template = render.render_portainer_template(entry, "logo.svg")
        assert template["description"].startswith("Imported, untested: ")
        assert "Imported, untested: not yet tested" in template["note"]
        assert "Template source:" in template["note"]
        templates.append(template)
        body = readme.render_readme(entry)
        assert "**Imported, untested.**" in body and "**Importé, non testé.**" in body
        assert "**Template source:**" in body and "**Source du modèle :**" in body
        assert not re.search(r"\{\{\s", body)
    assert model.validate_output({"version": "3", "templates": templates}) == []
    assert "Notes (imported, untested)" in render.render_index_html(
        list(entries.values()), model.load_meta())


def test_a_curated_entry_carries_no_import_stubs(catalog):
    """Dropping the status makes an entry curated, and the schema then asks
    for everything a curated entry has: no pending list, a bench pack and a
    measured peak. The origin may stay."""
    assert _run() == 0
    path = model.SOURCES / "notes.json"
    doc = json.loads(path.read_text())
    cat = doc["x-catena"]
    del cat["status"]
    path.write_text(json.dumps(doc))
    with pytest.raises(model.SourceError) as err:
        model.load_sources()
    for where in ("x-catena: 'status' is a dependency of 'pending'",
                  "x-catena/bench/pack: None is not of type 'string'",
                  "x-catena/sizing/peak_ram_mb: None is not of type 'integer'"):
        assert f"sources/notes.json: {where}" in str(err.value)
    del cat["pending"]
    cat["bench"]["pack"] = "nodb"
    cat["sizing"]["peak_ram_mb"] = 256
    path.write_text(json.dumps(doc))
    assert "notes" in {e.slug for e in model.load_sources()}


def test_an_imported_entry_names_a_permitted_licence_and_its_origin(catalog):
    assert _run() == 0
    path = model.SOURCES / "notes.json"
    doc = json.loads(path.read_text())
    doc["x-catena"]["origin"]["licence"] = "GPL-3.0"
    path.write_text(json.dumps(doc))
    with pytest.raises(model.SourceError, match="origin/licence"):
        model.load_sources()
    del doc["x-catena"]["origin"]
    path.write_text(json.dumps(doc))
    with pytest.raises(model.SourceError, match="'origin' is a required property"):
        model.load_sources()
    assert pipeline.permissive_licences() == ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause")


def test_an_id_already_in_the_catalog_is_left_alone(catalog, capsys):
    curated = model.SOURCES / "notes.json"
    curated.write_text("{}\n")
    assert _run() == 0
    assert curated.read_text() == "{}\n"
    assert not (model.BLUEPRINTS / "notes").exists()
    assert "Notes: the id notes is already in the catalog" in capsys.readouterr().out
    assert model.load_meta()["order"] == ["web-stack"]


def test_a_dry_run_writes_nothing(catalog, capsys):
    meta = (model.SOURCES / model.META_NAME).read_text()
    assert _run("--dry-run") == 0
    assert "would import: 2" in capsys.readouterr().out
    assert [p.name for p in model.SOURCES.iterdir()] == [model.META_NAME]
    assert list(model.BLUEPRINTS.iterdir()) == []
    assert (model.SOURCES / model.META_NAME).read_text() == meta


def test_a_document_that_is_not_a_portainer_list_is_refused(catalog, capsys, monkeypatch):
    monkeypatch.setitem(WEB, REGISTRY, b'{"version": "1", "templates": []}')
    assert _run() == 1
    assert "cannot read the registry: not a Portainer template list" in capsys.readouterr().err


def test_compose_scalars_read_as_compose_reads_them():
    doc = pipeline.load_compose(
        "services:\n  a:\n    image: x\n    ports:\n      - 22:22\n"
        "    environment:\n      A: yes\n      B: on\n      C: 2024-01-01\n      D: true\n")
    service = doc["services"]["a"]
    assert service["ports"] == ["22:22"]
    assert service["environment"] == {"A": "yes", "B": "on", "C": "2024-01-01", "D": True}
    assert yaml.safe_load(pipeline.dump_compose(doc)) == doc


def test_a_change_to_one_service_reaches_no_other():
    """A YAML anchor shares one list between services; the route labels go
    on the route service alone."""
    doc = pipeline.load_compose(
        "x-labels: &labels\n  - team=a\n"
        "services:\n"
        "  web:\n    image: x\n    labels: *labels\n    ports: ['80']\n"
        "  worker:\n    image: y\n    labels: *labels\n")
    stack = pipeline.make_stack(doc)
    assert stack.service == "web" and stack.port == 80
    assert doc["services"]["worker"]["labels"] == ["team=a"]


def test_registry_text_is_made_plain():
    assert (pipeline.plain("Fast \u2014 \u201cnice\u201d \U0001F680 app\u2026\n done")
            == 'Fast -- "nice" app... done')


@pytest.mark.parametrize("image,page", [
    ("nginx:1.27.3", "https://hub.docker.com/_/nginx"),
    ("docker.io/library/redis:7.4.1", "https://hub.docker.com/_/redis"),
    ("example/notes:1.2.3", "https://hub.docker.com/r/example/notes"),
    ("ghcr.io/owner/app:1.0.0", "https://ghcr.io/owner/app"),
    ("localhost:5000/app@sha256:abc", "https://localhost:5000/app"),
])
def test_a_container_entry_points_at_its_image_page(image, page):
    assert portainer.image_page(image) == page
