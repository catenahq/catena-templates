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
from datetime import date
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import changes, model, pack, provenance, quiesce_lint, readme, render  # noqa: E402
from lib.importers import (arcane, awesome_selfhosted, dedup, dokploy, pipeline,  # noqa: E402
                           portainer, publish, upstream)

ADAPTERS = {"portainer": portainer.read}
REGISTRY = "https://raw.githubusercontent.com/example/registry/main/templates.json"
API = pipeline.GITHUB_API
STACKS_COMMIT = "a" * 40
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
    f"{API}/repos/example/bare": json.dumps({"full_name": "example/bare"}).encode(),
    f"{API}/repos/example/stacks/commits/HEAD": json.dumps({"sha": STACKS_COMMIT}).encode(),
    f"https://raw.githubusercontent.com/example/stacks/{STACKS_COMMIT}/web/docker-compose.yml":
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
            # Every image is linked on Docker Hub to a repository of its own
            # under the MIT licence, unless a test says otherwise.
            if url.startswith(f"{provenance.HUB_API}/repositories/"):
                return json.dumps({"source": f"https://github.com/upstream/{url.split('/')[-2]}"}).encode()
            if url.startswith(f"{API}/repos/upstream/") and url.endswith("/license"):
                return _licence(url.split("/repos/")[1].removesuffix("/license"), "MIT", "MIT\n")
            raise pipeline.FetchError(f"{url}: HTTP 404", 404)
        return WEB[url]

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    return tmp_path, calls


def _run(*args: str) -> int:
    return pipeline.main([REGISTRY, *args], ALL)


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
        "commit": STACKS_COMMIT,
        "path": "web/docker-compose.yml",
        "project": "https://github.com/upstream/web",
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


def _env(slug: str) -> dict[str, str]:
    env = _compose(slug)["x-catena"]["env"]
    return {key: model.env_value(spec) for key, spec in env.items()}


def test_the_imported_compose_starts_with_a_block_that_holds_its_env(catalog):
    assert _run() == 0
    block = _compose("notes")["x-catena"]
    assert block["schema"] == 2
    assert block["env"]["ADMIN_PASSWORD"] == {"generated": {"length": 32, "alphabet": "alnum"}}
    assert block["env"]["TZ"] == {"setting": {"default": "UTC"}}
    assert "env_defaults" not in _source("notes")["x-catena"]


def test_an_imported_entry_is_held_as_neat_files_that_pack_to_its_compose(catalog):
    assert _run() == 0
    for slug in ("web-stack", "notes"):
        folder = model.BLUEPRINTS / slug
        text = (folder / model.COMPOSE_NAME).read_text()
        assert (folder / "services.yml").is_file() and (folder / "settings.yml").is_file()
        assert text.startswith("x-catena:\n  schema: 2\n  env:\n")
        neat, hooks = pack._read_neat(folder)
        assert pack.pack_texts(neat, hooks) == text
        assert pack.pack_all(model.BLUEPRINTS.parent) == []


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
    assert _compose("notes")["x-catena"]["env"]["MODE"] == {"setting": {"default": "b"}}
    to_choose = cat["pending"]["to_choose"]
    assert any(c.startswith("bind mount /opt/notes in service notes") for c in to_choose)
    assert any(c.startswith("ports service notes publishes on the server (53/udp)") for c in to_choose)


def _copied_from(monkeypatch, repo: str) -> None:
    """The fixture network holds a pinned compose file in `repo`."""
    monkeypatch.setitem(WEB, f"{API}/repos/{repo}/commits/HEAD", json.dumps({"sha": STACKS_COMMIT}).encode())
    image = f"{repo}:1.0.0"
    monkeypatch.setitem(WEB, f"https://raw.githubusercontent.com/{repo}/{STACKS_COMMIT}/docker-compose.yml",
                        f"services:\n  app:\n    image: {image}\n    ports:\n      - '8080:80'\n".encode())


def test_a_copied_files_licence_is_recorded_whatever_it_is(catalog, capsys, monkeypatch):
    _copied_from(monkeypatch, "example/gpl")
    _copied_from(monkeypatch, "example/bare")
    assert _run() == 0
    origin = _source("copyleft")["x-catena"]["origin"]
    assert origin["licence"] == "GPL-3.0"
    assert origin["attribution"] == "https://github.com/example/gpl/blob/main/LICENSE"
    origin = _source("no-licence")["x-catena"]["origin"]
    assert origin["licence"] == "NOASSERTION" and origin["attribution"] == "https://github.com/example/bare"
    assert "skipped, licence not permitted: 0" in capsys.readouterr().out


def test_a_source_on_no_forge_the_importer_pins_is_skipped(catalog, capsys):
    assert _run() == 0
    assert not (model.SOURCES / "elsewhere.json").exists()
    out = capsys.readouterr().out
    assert "skipped, licence unknown: 1" in out
    assert "Elsewhere: the repository https://gitlab.com/example/app is not on GitHub" in out


def test_settings_named_like_a_password_secret_or_key_are_minted(catalog):
    assert _run() == 0
    notes = _env("notes")
    assert list(notes)[0] == "DOMAIN_HOST"
    assert notes["ADMIN_PASSWORD"] == pipeline.minted("ADMIN_PASSWORD")
    assert "length=32" in notes["ADMIN_PASSWORD"]
    assert "length=64" in notes["APP_SECRET_KEY"]
    assert notes["TZ"] == "UTC"
    web = _env("web-stack")
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
    # A curated entry takes its place in the deployment order.
    with pytest.raises(model.SourceError, match="notes.json: not listed in _meta.json order"):
        model.load_sources()
    meta = model.load_meta()
    meta["order"] = ["notes"]
    (model.SOURCES / model.META_NAME).write_text(json.dumps(meta))
    assert "notes" in {e.slug for e in model.load_sources()}


def test_an_imported_entry_names_its_origin_and_any_licence_it_has(catalog):
    assert _run() == 0
    path = model.SOURCES / "notes.json"
    doc = json.loads(path.read_text())
    doc["x-catena"]["origin"]["licence"] = "GPL-3.0"
    path.write_text(json.dumps(doc))
    assert "notes" in {e.slug for e in model.load_sources()}
    doc["x-catena"]["origin"]["licence"] = ""
    path.write_text(json.dumps(doc))
    with pytest.raises(model.SourceError, match="origin/licence"):
        model.load_sources()
    del doc["x-catena"]["origin"]
    path.write_text(json.dumps(doc))
    with pytest.raises(model.SourceError, match="'origin' is a required property"):
        model.load_sources()


def test_an_id_already_in_the_catalog_is_left_alone(catalog, capsys):
    curated = model.SOURCES / "notes.json"
    curated.write_text("{}\n")
    assert _run() == 0
    assert curated.read_text() == "{}\n"
    assert not (model.BLUEPRINTS / "notes").exists()
    assert "Notes: the id notes is already in the catalog" in capsys.readouterr().out
    assert model.load_meta()["order"] == []


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


@pytest.mark.parametrize("url,repo", [
    ("https://github.com/example/app", ("github.com", "example/app")),
    ("https://github.com/example/app.git", ("github.com", "example/app")),
    ("https://github.com/example/app/tree/main", ("github.com", "example/app")),
    ("https://github.com/example/app/tree/v2.1/server/", ("github.com", "example/app")),
    ("https://github.com/example-org/", ("github.com", "example-org")),
    ("https://gitlab.com/group/sub/project.git", ("gitlab.com", "group/sub/project")),
    ("https://codeberg.org/owner", ("codeberg.org", "owner")),
    ("https://www.Git.Example.org/owner/app/", ("git.example.org", "owner/app")),
    ("https://github.com/example/app/blob/main/README.md", None),
    ("https://example.org/docs/", None),
    ("https://example.org", None),
    ("", None),
])
def test_an_upstream_address_names_its_repository(url, repo):
    found = pipeline.upstream_repo(url)
    assert (found and (found.host, found.path)) == repo


def _gitlab_project(host: str, path: str, key: str | None) -> bytes:
    """GitLab's answer to GET /projects/:id?license=true."""
    web = f"https://{host}/{path}"
    return json.dumps({
        "path_with_namespace": path, "web_url": web, "default_branch": "trunk",
        "license_url": f"{web}/-/blob/trunk/LICENSE" if key else None,
        "license": {"key": key, "name": key} if key else None,
    }).encode()


def _gitea_repo(host: str, path: str, licenses: list[str] | None) -> bytes:
    """Gitea's answer to GET /repos/{owner}/{repo}; Forgejo's when licenses
    is None."""
    doc = {"full_name": path, "html_url": f"https://{host}/{path}", "default_branch": "main"}
    if licenses is not None:
        doc["licenses"] = licenses
    return json.dumps(doc).encode()


GITLAB = "https://gitlab.com/api/v4/projects"
SELF_GITLAB = "https://lab.example.org/api/v4/projects"
GITEA = "https://git.example.org/api/v1/repos"
FORGE_WEB = {
    f"{GITLAB}/example%2Flab?license=true": _gitlab_project("gitlab.com", "example/lab", "mit"),
    f"{GITLAB}/example%2Flab/repository/files/LICENSE/raw":
        b"MIT License\n\nCopyright (c) 2023 Lab Example\n",
    f"{GITLAB}/example%2Fgpl?license=true": _gitlab_project("gitlab.com", "example/gpl", "gpl-3.0"),
    f"{GITLAB}/example%2Fgpl/repository/files/LICENSE/raw": b"GNU GPL\n",
    f"{GITLAB}/example%2Fsspl?license=true": _gitlab_project("gitlab.com", "example/sspl", "sspl-1.0"),
    f"{GITLAB}/example%2Fsspl/repository/files/LICENSE/raw": b"Server Side Public License\n",
    f"{GITLAB}/example%2Fodd?license=true": _gitlab_project("gitlab.com", "example/odd", "other"),
    f"{GITLAB}/example%2Fplain?license=true": _gitlab_project("gitlab.com", "example/plain", None),
    f"{SELF_GITLAB}/group%2Fsub%2Ftool?license=true":
        _gitlab_project("lab.example.org", "group/sub/tool", "apache-2.0"),
    f"{SELF_GITLAB}/group%2Fsub%2Ftool/repository/files/LICENSE/raw":
        b"Apache License\n\n      Copyright [yyyy] [name of copyright owner]\n",
    # A GitLab sends an address it does not serve to its sign-in page, or a
    # proxy in front of it refuses it.
    "https://lab.example.org/api/v1/repos/owner/app": b"<!DOCTYPE html><title>Sign in</title>",
    f"{SELF_GITLAB}/owner%2Fapp?license=true": _gitlab_project("lab.example.org", "owner/app", "mit"),
    f"{SELF_GITLAB}/owner%2Fapp/repository/files/LICENSE/raw": b"MIT License\n",
    "https://lab.example.org/api/v1/repos/owner/other":
        pipeline.FetchError("https://lab.example.org/api/v1/repos/owner/other: HTTP 403", 403),
    f"{SELF_GITLAB}/owner%2Fother?license=true":
        _gitlab_project("lab.example.org", "owner/other", "bsd-2-clause"),
    f"{SELF_GITLAB}/owner%2Fother/repository/files/LICENSE/raw": b"BSD 2-Clause License\n",
    f"{GITEA}/example/tea": _gitea_repo("git.example.org", "example/tea", ["BSD-2-Clause"]),
    f"{GITEA}/example/tea/contents/LICENSE?ref=main": json.dumps({
        "html_url": "https://git.example.org/example/tea/src/branch/main/LICENSE",
        "encoding": "base64",
        "content": base64.b64encode(b"BSD 2-Clause License\n\nCopyright (c) 2021 Tea Example\n").decode(),
    }).encode(),
    f"{GITEA}/example/two": _gitea_repo("git.example.org", "example/two", ["MIT", "Apache-2.0"]),
    f"{GITEA}/example/none": _gitea_repo("git.example.org", "example/none", None),
    "https://codeberg.org/api/v1/repos/example/fj": _gitea_repo("codeberg.org", "example/fj", None),
    f"{API}/repos/example/bare": json.dumps({"full_name": "example/bare"}).encode(),
}


@pytest.fixture
def forges(monkeypatch):
    """The fixture network of forge answers, a failure where FORGE_WEB holds
    one; anything else is a 404."""
    calls: list[str] = []

    def fake_fetch(url, headers=None):
        calls.append(url)
        if url not in FORGE_WEB:
            raise pipeline.FetchError(f"{url}: HTTP 404", 404)
        if isinstance(FORGE_WEB[url], pipeline.FetchError):
            raise FORGE_WEB[url]
        return FORGE_WEB[url]

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    return calls


def _read(host: str, path: str) -> pipeline.Licence:
    return pipeline.repo_licence(pipeline.Repo(host, path))


def test_gitlab_names_the_licence_and_its_file_the_copyright(forges):
    assert _read("gitlab.com", "example/lab") == pipeline.Licence(
        "MIT", "Copyright (c) 2023 Lab Example")
    # A licence an upstream project may carry reads in SPDX's spelling; any
    # other keeps GitLab's key, for the report.
    assert _read("gitlab.com", "example/gpl").spdx == "GPL-3.0"
    assert _read("gitlab.com", "example/sspl").spdx == "sspl-1.0"
    assert _read("gitlab.com", "example/odd").problem == (
        "GitLab cannot name the licence in https://gitlab.com/example/odd/-/blob/trunk/LICENSE")
    assert _read("gitlab.com", "example/plain").problem == (
        "GitLab finds no licence file in gitlab.com/example/plain")
    assert _read("gitlab.com", "example/gone").problem == "no repository example/gone on gitlab.com"
    # gitlab.com is asked through GitLab's API alone.
    assert not any("/api/v1/" in url for url in forges)


def test_gitea_names_the_licence_and_forgejo_names_none(forges):
    assert _read("git.example.org", "example/tea") == pipeline.Licence(
        "BSD-2-Clause", "Copyright (c) 2021 Tea Example")
    assert _read("git.example.org", "example/two").problem == (
        "Gitea names MIT, Apache-2.0 in the LICENSE file of git.example.org/example/two")
    for host, path in (("codeberg.org", "example/fj"), ("git.example.org", "example/none")):
        assert _read(host, path).problem == f"the API at {host} names no licence for {path}"
    # codeberg.org is asked through Gitea's API alone.
    assert not any("codeberg.org/api/v4/" in url for url in forges)


def test_a_host_the_importer_does_not_know_is_asked_as_gitea_then_gitlab(forges):
    assert _read("lab.example.org", "owner/app").spdx == "MIT"
    assert forges[:2] == ["https://lab.example.org/api/v1/repos/owner/app",
                          f"{SELF_GITLAB}/owner%2Fapp?license=true"]
    assert _read("lab.example.org", "owner/other").spdx == "BSD-2-Clause"
    forges.clear()
    # The Apache text carries only its appendix's line to fill in. A path
    # with groups is no Gitea repository, so GitLab is asked alone.
    assert _read("lab.example.org", "group/sub/tool") == pipeline.Licence(
        "Apache-2.0", "https://lab.example.org/group/sub/tool/-/blob/trunk/LICENSE")
    assert not any("/api/v1/" in url for url in forges)
    assert _read("elsewhere.example", "owner/app").problem == (
        "no repository owner/app on elsewhere.example, through Gitea's API or GitLab's")


def test_only_copyright_notices_make_the_attribution():
    text = ("Apache License\n"
            "      copyright notice that is included in or attached to the work\n"
            "      copyright license to reproduce, prepare Derivative Works of,\n"
            "Copyright 2019 The Tool Authors\n"
            "Copyright (c) [year] [fullname]\n"
            "Copyright © 2021 Other Authors\n")
    assert pipeline._attribution(text, "LICENSE") == (
        "Copyright 2019 The Tool Authors; Copyright 2021 Other Authors")
    assert pipeline._attribution(text.split("Copyright 2019")[0], "LICENSE") == "LICENSE"


def test_github_tells_a_missing_repository_from_one_without_a_licence_file(forges):
    assert _read("github.com", "example/bare").problem == "GitHub finds no licence file in example/bare"
    assert _read("github.com", "example/gone").problem == "no repository example/gone on GitHub"


# --- the registry feed: adapters, deduplication, licences, waves -------------

ALL = {"portainer": portainer.read, "dokploy": dokploy.read, "arcane": arcane.read,
       "awesome-selfhosted": awesome_selfhosted.read}
TODAY = date(2026, 10, 22)

LISSY_COMMIT, ARCANE_COMMIT, DOKPLOY_COMMIT, AWESOME_COMMIT = "b" * 40, "c" * 40, "d" * 40, "e" * 40
LISSY = f"https://raw.githubusercontent.com/Lissy93/portainer-templates/{LISSY_COMMIT}/templates.json"
LISSY_DOC = {"version": "2", "templates": [
    {"type": 1, "title": "Actual Budget", "name": "actual-budget", "description": "Budgeting",
     "categories": ["Finance"], "platform": "linux", "image": "actualbudget/actual-server:25.1.0",
     "ports": ["5006/tcp"], "env": [{"name": "TZ", "default": "UTC"}]}]}

ARCANE = "https://registry.getarcane.app/registry.json"
ARCANE_SITE = "https://registry.getarcane.app/templates/actual"
ARCANE_DOC = {"name": "Arcane", "description": "d", "author": "a", "version": "1.0.0",
              "url": "https://registry.getarcane.app", "templates": [
    {"id": "actual", "name": "Actual", "description": "Privacy-focused budgeting server.",
     "version": "1.0.0", "author": "getarcaneapp",
     "compose_url": f"{ARCANE_SITE}/compose.yaml", "env_url": f"{ARCANE_SITE}/.env.example",
     "documentation_url": "https://github.com/getarcaneapp/templates/tree/main/templates/actual",
     "content_hash": "0" * 64, "tags": ["finance"]}]}
ARCANE_COMPOSE = """\
services:
  actual:
    image: actualbudget/actual-server:25.1.0
    ports:
      - 5006:5006
    healthcheck:
      test: [CMD, "true"]
    environment:
      ACTUAL_PORT: ${ACTUAL_PORT:-5006}
      TZ: ${TZ}
"""
ARCANE_ENV = "# a comment\nACTUAL_PORT=5006\nTZ='UTC'\nEXTRA=1\n"

DOK_REGISTRY = f"{API}/repos/example/dok/git/trees/{DOKPLOY_COMMIT}?recursive=1"
DOK_RAW = f"https://raw.githubusercontent.com/example/dok/{DOKPLOY_COMMIT}/blueprints"
DOK_TREE = {"truncated": False, "tree": [
    {"path": f"blueprints/{i}/{n}", "type": "blob"}
    for i in ("one", "two") for n in dokploy.FILES]}
DOK_TOML = '[variables]\n\n[config]\n[[config.domains]]\nserviceName = "app"\nport = 80\nhost = "${domain}"\n'

AWESOME = f"{API}/repos/awesome-selfhosted/awesome-selfhosted-data/git/trees/{AWESOME_COMMIT}?recursive=1"
AWESOME_RAW = (f"https://raw.githubusercontent.com/awesome-selfhosted/awesome-selfhosted-data/"
               f"{AWESOME_COMMIT}/software")


def _awesome_entry(name: str, source: str, platforms: list[str]) -> bytes:
    return yaml.safe_dump({"name": name, "website_url": "https://example.org",
                           "source_code_url": source, "description": "x", "licenses": ["MIT"],
                           "platforms": platforms, "tags": ["Misc"]}).encode()


FEED_WEB = {
    LISSY: json.dumps(LISSY_DOC).encode(),
    f"{API}/repos/Lissy93/portainer-templates/license": _licence(
        "Lissy93/portainer-templates", "MIT", "MIT License\n\nCopyright (c) 2021 Lissy93\n"),
    ARCANE: json.dumps(ARCANE_DOC).encode(),
    f"{API}/repos/getarcaneapp/templates/license": _licence(
        "getarcaneapp/templates", "Apache-2.0", "Apache License\n\nCopyright 2025 Arcane\n"),
    f"{API}/repos/getarcaneapp/templates/commits/HEAD": json.dumps({"sha": ARCANE_COMMIT}).encode(),
    f"https://raw.githubusercontent.com/getarcaneapp/templates/{ARCANE_COMMIT}/templates/actual/compose.yaml":
        ARCANE_COMPOSE.encode(),
    f"https://raw.githubusercontent.com/getarcaneapp/templates/{ARCANE_COMMIT}/templates/actual/.env.example":
        ARCANE_ENV.encode(),
    DOK_REGISTRY: json.dumps(DOK_TREE).encode(),
    f"{API}/repos/example/dok/license": _licence("example/dok", "MIT", "MIT\n\nCopyright (c) 2025 Dok\n"),
    f"{API}/repos/example/shared/license": _licence("example/shared", "MIT", "MIT\n\nCopyright (c) 2024 S\n"),
    AWESOME: json.dumps({"truncated": False, "tree": [
        {"path": "software/notes.yml", "type": "blob"},
        {"path": "software/native.yml", "type": "blob"},
        {"path": "software/nosource.yml", "type": "blob"},
        {"path": "README.md", "type": "blob"}]}).encode(),
    f"{AWESOME_RAW}/notes.yml": _awesome_entry("Notes", "https://github.com/example/notes-app", ["Docker"]),
    f"{AWESOME_RAW}/native.yml": _awesome_entry("Native", "https://github.com/example/native", ["Python"]),
    f"{AWESOME_RAW}/nosource.yml": _awesome_entry("NoSource", "", ["Docker"]),
    f"{API}/repos/example/notes-app/license": _licence(
        "example/notes-app", "MIT", "MIT\n\nCopyright (c) 2023 Notes\n"),
    f"{API}/repos/example/notes-gpl/license": _licence("example/notes-gpl", "AGPL-3.0", "GNU AGPL\n"),
}
for _ident in ("one", "two"):
    FEED_WEB[f"{DOK_RAW}/{_ident}/meta.json"] = json.dumps({
        "name": _ident.title(), "description": "d", "tags": [],
        "links": {"github": "https://github.com/example/shared"}}).encode()
    FEED_WEB[f"{DOK_RAW}/{_ident}/template.toml"] = DOK_TOML.encode()
    FEED_WEB[f"{DOK_RAW}/{_ident}/docker-compose.yml"] = (
        f"services:\n  app:\n    image: example/{_ident}:1.0.0\n    expose:\n      - 80\n").encode()


@pytest.fixture
def feed(catalog, monkeypatch):
    for url, body in FEED_WEB.items():
        monkeypatch.setitem(WEB, url, body)
    return catalog


def _main(*args: str, runner=None) -> int:
    return pipeline.main(list(args), ALL, runner=runner, render_fn=lambda: 0, today=TODAY)


def test_the_portainer_adapter_reads_lissy93s_list_at_its_commit(feed):
    assert _main(LISSY) == 0
    origin = _source("actual-budget")["x-catena"]["origin"]
    assert origin["registry"] == LISSY and origin["source"] == LISSY
    assert origin["commit"] == LISSY_COMMIT and origin["path"] == "templates.json"
    assert origin["licence"] == "MIT" and origin["attribution"] == "Copyright (c) 2021 Lissy93"


def test_the_arcane_adapter_reads_a_template_from_the_repository_at_a_pinned_commit(feed):
    assert _main(ARCANE, "--format", "arcane") == 0
    cat = _source("actual")["x-catena"]
    assert cat["origin"] == {
        "registry": ARCANE, "entry": "Actual",
        "source": "https://github.com/getarcaneapp/templates", "licence": "Apache-2.0",
        "attribution": "Copyright 2025 Arcane", "commit": ARCANE_COMMIT,
        "path": "templates/actual/compose.yaml",
        "project": "https://github.com/upstream/actual-server"}
    env = _env("actual")
    assert env["ACTUAL_PORT"] == "5006" and env["TZ"] == "UTC" and env["EXTRA"] == "1"
    service = _compose("actual")["services"]["actual"]
    assert service["labels"] == ["vps.route.host=${DOMAIN_HOST}", "vps.route.port=5006"]
    assert "ports" not in service
    assert _source("actual")["categories"] == ["Finance"]


def test_an_unpinnable_repository_skips_its_entries(feed, capsys, monkeypatch):
    monkeypatch.delitem(WEB, f"{API}/repos/getarcaneapp/templates/commits/HEAD")
    assert _main(ARCANE, "--format", "arcane") == 0
    assert "Actual: cannot pin getarcaneapp/templates to a commit" in capsys.readouterr().out
    assert not (model.SOURCES / "actual.json").exists()


def test_a_document_that_is_no_arcane_registry_is_refused(feed, capsys, monkeypatch):
    monkeypatch.setitem(WEB, ARCANE, b'{"templates": []}')
    assert _main(ARCANE, "--format", "arcane") == 1
    assert "not an Arcane template registry" in capsys.readouterr().err


def test_entries_sharing_an_image_are_one_app_and_the_richest_seeds_it(feed, capsys):
    assert _main(LISSY, "--also", f"arcane={ARCANE}") == 0
    out = capsys.readouterr().out
    assert "imported: 1\n  actual (Actual)" in out
    assert "Actual Budget: alias of actual" in out
    assert not (model.SOURCES / "actual-budget.json").exists()
    origin = _source("actual")["x-catena"]["origin"]
    assert origin["aliases"] == [{"registry": LISSY, "entry": "Actual Budget", "source": LISSY,
                                  "commit": LISSY_COMMIT, "path": "templates.json"}]
    assert model.load_sources()


def test_entries_naming_one_source_repository_are_one_app(feed, capsys):
    assert _main(DOK_REGISTRY, "--format", "dokploy") == 0
    assert "imported: 1\n  one (One)" in capsys.readouterr().out
    cat = _source("one")["x-catena"]
    assert cat["origin"]["project"] == "https://github.com/example/shared"
    assert [a["entry"] for a in cat["origin"]["aliases"]] == ["Two"]
    assert not (model.SOURCES / "two.json").exists()


def test_an_entry_of_an_app_already_in_the_catalog_is_recorded_as_an_alias(feed, capsys):
    (model.SOURCES / "curated.json").write_text(json.dumps({
        "id": "curated", "x-catena": {"domain": {"host": "h", "service": "app", "port": 80}}}))
    (model.BLUEPRINTS / "curated").mkdir()
    (model.BLUEPRINTS / "curated" / model.COMPOSE_NAME).write_text(
        "services:\n  app:\n    image: docker.io/example/notes:2.0.0\n")
    assert _run() == 0
    out = capsys.readouterr().out
    assert "Notes: alias of curated (in the catalog)" in out
    assert not (model.SOURCES / "notes.json").exists()
    records = json.loads((model.SOURCES / model.ALIASES_NAME).read_text())["aliases"]
    assert records == [{"app": "curated", "registry": REGISTRY, "entry": "Notes", "source": REGISTRY}]
    # The next run knows the entry and weighs nothing again.
    assert _run() == 0
    assert "Notes: already recorded as an alias of curated" in capsys.readouterr().out
    assert changes.app_ids([f"sources/{model.ALIASES_NAME}", "sources/_meta.json",
                            model.CATALOG_ALIASES, model.CATALOG_META]) == []


def _card_layout(root: Path) -> None:
    """The catalog's order moves to catalog-meta.json: the layout whose new entries are card files."""
    (root / model.CATALOG_META).write_text((model.SOURCES / model.META_NAME).read_text())
    (model.SOURCES / model.META_NAME).unlink()


def test_the_card_layout_writes_an_app_json_and_no_source(catalog):
    root, _ = catalog
    _card_layout(root)
    assert _run() == 0
    assert list(model.SOURCES.glob("*.json")) == []
    card = json.loads((model.BLUEPRINTS / "web-stack" / model.CARD_NAME).read_text())
    assert card["schema"] == 1
    assert card["status"] == "imported"
    assert card["origin"]["entry"] == "Web Stack"
    assert card["categories"] == ["Web"]
    assert not {"id", "type", "title", "name", "platform", "x-catena", "domain"} & set(card)
    assert card["en"]["display_name"] == "Web Stack"
    assert card["en"]["summary"] == "A web app -- with its database"
    assert card["en"]["compose_description"].startswith("A web app -- with its database")
    assert card["fr"]["compose_description"].strip()
    assert card["fr"]["summary"].startswith("Description du registre, non traduite : ")
    entry = next(e for e in model.load_sources() if e.slug == "web-stack")
    assert entry.card
    assert entry.domain == {"host": "web-stack.{{ cloudflare_zone }}", "service": "web", "port": 80}
    assert entry.title == "Web Stack"


def test_the_card_layout_lists_the_paths_an_entry_commits(catalog):
    root, _ = catalog
    assert pipeline.entry_paths("x") == ["blueprints/x", "sources/x.json"]
    _card_layout(root)
    assert pipeline.entry_paths("x") == ["blueprints/x"]


def test_the_card_layout_reads_card_apps_and_records_aliases_in_catalog_aliases(catalog, capsys):
    root, _ = catalog
    _card_layout(root)
    (model.BLUEPRINTS / "curated").mkdir()
    (model.BLUEPRINTS / "curated" / model.CARD_NAME).write_text(json.dumps({"schema": 1}))
    (model.BLUEPRINTS / "curated" / model.COMPOSE_NAME).write_text(
        "services:\n  app:\n    image: docker.io/example/notes:2.0.0\n"
        "    labels:\n      - vps.route.host=h\n      - vps.route.port=80\n")
    assert _run() == 0
    assert "Notes: alias of curated (in the catalog)" in capsys.readouterr().out
    assert not (model.BLUEPRINTS / "notes").exists()
    records = json.loads((root / model.CATALOG_ALIASES).read_text())["aliases"]
    assert records == [{"app": "curated", "registry": REGISTRY, "entry": "Notes", "source": REGISTRY}]
    assert not (model.SOURCES / model.ALIASES_NAME).exists()
    assert model.aliases_path() == root / model.CATALOG_ALIASES


def test_the_richness_counts_settings_a_health_check_and_sign_in_labels():
    def prepared(compose: str, settings=()):
        doc = pipeline.load_compose(compose)
        stack = pipeline.make_stack(doc)
        return dedup.richness(pipeline.Candidate(entry="x", settings=list(settings)), stack)

    base = "services:\n  a:\n    image: x\n    ports: ['80']\n    environment:\n      A: ${A}\n"
    # The route label every stack gets reads DOMAIN_HOST, a setting of its own.
    plain = prepared(base)
    assert plain == 2
    assert prepared(base, [("B", ""), ("C", "")]) == plain + 2
    assert prepared(base + "    healthcheck:\n      test: [CMD, 'true']\n") == plain + dedup.HEALTH_WEIGHT
    assert prepared(base + "    labels:\n      vps.auth.oidc: 'true'\n") == plain + dedup.SIGN_IN_WEIGHT


@pytest.mark.parametrize("ref,key", [
    ("nginx:1.27", "image:nginx"), ("docker.io/library/nginx@sha256:ab", "image:nginx"),
    ("Docker.io/Example/App:1", "image:example/app"), ("ghcr.io/o/app:1", "image:ghcr.io/o/app"),
    ("${IMAGE}", ""), ("example/app:${TAG}", "image:example/app")])
def test_an_image_is_keyed_by_its_repository(ref, key):
    assert dedup.image_key(ref) == key


def test_an_awesome_selfhosted_project_gives_an_entry_its_upstream_repository(feed, capsys):
    assert _run("--also", f"awesome-selfhosted={AWESOME}") == 0
    out = capsys.readouterr().out
    assert "listed with no compose file to import: 1" in out
    cat = _source("notes")["x-catena"]
    assert cat["origin"]["project"] == "https://github.com/example/notes-app"
    assert any("matched by name: confirm it is this app" in c for c in cat["pending"]["to_choose"])
    assert not (model.SOURCES / "native.json").exists()


def test_the_upstream_projects_licence_is_checked_too(feed, capsys, monkeypatch):
    monkeypatch.setitem(WEB, f"{AWESOME_RAW}/notes.yml", _awesome_entry(
        "Notes", "https://github.com/example/notes-sspl", ["Docker"]))
    monkeypatch.setitem(WEB, f"{API}/repos/example/notes-sspl/license", _licence(
        "example/notes-sspl", "SSPL-1.0", "Server Side Public License\n"))
    assert _run("--also", f"awesome-selfhosted={AWESOME}") == 0
    out = capsys.readouterr().out
    assert "skipped, licence not permitted: 1\n  Notes: upstream project SSPL-1.0 (example/notes-sspl)" in out
    assert not (model.SOURCES / "notes.json").exists()


def test_an_upstream_project_may_carry_a_copyleft_licence(feed, capsys, monkeypatch):
    monkeypatch.setitem(WEB, f"{AWESOME_RAW}/notes.yml", _awesome_entry(
        "Notes", "https://github.com/example/notes-gpl", ["Docker"]))
    assert _run("--also", f"awesome-selfhosted={AWESOME}") == 0
    assert "skipped, licence not permitted: 0" in capsys.readouterr().out
    origin = _source("notes")["x-catena"]["origin"]
    assert origin["project"] == "https://github.com/example/notes-gpl" and origin["licence"] == "MIT"


def test_an_upstream_project_with_no_readable_licence_skips_the_entry(feed, capsys, monkeypatch):
    monkeypatch.setitem(WEB, f"{AWESOME_RAW}/notes.yml", _awesome_entry(
        "Notes", "https://github.com/example/notes-bare", ["Docker"]))
    monkeypatch.setitem(WEB, f"{API}/repos/example/notes-bare", json.dumps({"full_name": "x"}).encode())
    assert _run("--also", f"awesome-selfhosted={AWESOME}") == 0
    assert "Notes: upstream project GitHub finds no licence file in example/notes-bare" \
        in capsys.readouterr().out


def test_the_entry_option_reads_only_the_entries_it_names(catalog, capsys):
    assert _run("--entry", "notes") == 0
    out = capsys.readouterr().out
    assert "entries: 1\n" in out and "imported: 1\n  notes (Notes)" in out
    assert not (model.SOURCES / "web-stack.json").exists()


def test_entries_named_together_are_one_app_and_its_aliases(feed, capsys):
    assert _main(LISSY, "--also", f"arcane={ARCANE}", "--entry", "Actual", "--entry", "actual budget") == 0
    assert "imported: 1\n  actual (Actual)" in capsys.readouterr().out
    assert [a["entry"] for a in _source("actual")["x-catena"]["origin"]["aliases"]] == ["Actual Budget"]


def test_an_entry_title_the_registry_lacks_stops_the_run(catalog, capsys):
    assert _run("--entry", "Notes", "--entry", "Gone") == 1
    assert "no entry titled 'Gone'" in capsys.readouterr().err
    assert not (model.SOURCES / "notes.json").exists()


def test_imported_entries_not_in_the_order_follow_the_listed_ones_by_id(catalog):
    assert _run() == 0
    assert [e.slug for e in model.load_sources()] == ["notes", "web-stack"]


def test_a_wave_allowance_waits_for_updates_and_keeps_to_both_limits():
    def prs(*heads, day="2026-10-22"):
        return [{"headRefName": h, "createdAt": f"{day}T10:00:00Z"} for h in heads]

    wait = publish.allowance(5, 5, 10, prs("renovate/x", "import/a"), [], TODAY)
    assert wait.size == 0 and "1 update or fix pull requests are open" in wait.reason
    assert publish.allowance(5, 5, 10, prs("scanctl/y"), [], TODAY).size == 0
    assert publish.allowance(5, 5, 10, prs("engine-bump/z"), [], TODAY).size == 0
    assert publish.allowance(3, 5, 10, [], [], TODAY).size == 3
    assert publish.allowance(9, 5, 10, [], [], TODAY).size == 5
    recent = prs("import/a", "import/b", "feature/c") + prs("import/old", day="2026-10-21")
    got = publish.allowance(5, 5, 10, [], recent, TODAY)
    assert got.size == 3 and "2 opened today of 5" in got.reason
    assert publish.allowance(5, 5, 10, prs(*[f"import/{i}" for i in range(9)]), [], TODAY).size == 1
    assert publish.allowance(5, 5, 2, prs("import/a", "import/b"), [], TODAY).size == 0


class FakeGit:
    """Answers the git and gh commands of a wave and keeps them."""

    def __init__(self, open_prs=(), recent=(), dirty=False):
        self.calls: list[list[str]] = []
        self.open, self.recent, self.dirty = list(open_prs), list(recent), dirty

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps(self.open if argv[argv.index("--state") + 1] == "open" else self.recent)
        if argv[:2] == ["git", "status"]:
            return " M x" if self.dirty else ""
        if argv[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "main\n"
        if argv[:3] == ["gh", "pr", "create"]:
            return f"https://github.com/o/r/pull/{len([c for c in self.calls if c[:3] == argv[:3]])}\n"
        return ""

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] in (["git", "switch"], ["git", "add"], ["git", "commit"],
                                                      ["git", "push"], ["gh", "pr"]) and c[:3] != ["gh", "pr", "list"]]


def test_a_wave_opens_one_branch_and_one_pull_request_per_app(feed, capsys):
    git = FakeGit()
    assert _main(REGISTRY, "--wave", "1", runner=git) == 0
    out = capsys.readouterr().out
    assert "wave: 1 of 1" in out
    assert "imported: 1\n  web-stack (Web Stack)" in out
    assert "left for a later wave: 1" in out
    assert "pull requests opened: 1\n  https://github.com/o/r/pull/1" in out
    assert not (model.SOURCES / "notes.json").exists()
    steps = [c for c in git.writes()]
    assert steps[0] == ["git", "switch", "--create", "import/web-stack", "origin/main"]
    add = next(c for c in steps if c[:2] == ["git", "add"])
    assert add[add.index("--") + 1:] == ["blueprints/web-stack", "sources/web-stack.json",
                                         "templates.json", "catalog.json", "index.html"]
    create = next(c for c in steps if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--head") + 1] == "import/web-stack"
    assert create[create.index("--title") + 1] == "feat(import): web-stack from raw.githubusercontent.com"
    assert "- licence: Apache-2.0" in create[create.index("--body") + 1]
    assert steps[-1] == ["git", "switch", "main"]


def test_a_wave_needs_a_clean_checkout(feed, capsys):
    git = FakeGit(dirty=True)
    assert _main(REGISTRY, "--wave", "1", runner=git) == 1
    assert "the working tree is not clean" in capsys.readouterr().err
    assert git.writes() == []


def test_a_wave_holds_while_an_update_is_open_and_reads_nothing(feed, capsys):
    git = FakeGit(open_prs=[{"headRefName": "renovate/nextcloud", "createdAt": "2026-10-22T01:00:00Z"}])
    _, calls = feed
    assert _main(REGISTRY, "--wave", "3", runner=git) == 0
    assert "wave: 0 of 3 (1 update or fix pull requests are open" in capsys.readouterr().out
    assert calls == [] and git.writes() == []


def test_a_wave_keeps_to_the_daily_and_open_limits(feed, capsys):
    recent = [{"headRefName": f"import/a{i}", "createdAt": "2026-10-22T01:00:00Z"} for i in range(4)]
    git = FakeGit(recent=recent)
    assert _main(REGISTRY, "--wave", "5", "--dry-run", runner=git) == 0
    assert "wave: 1 of 5" in capsys.readouterr().out
    git = FakeGit(open_prs=[{"headRefName": "import/a", "createdAt": "2026-10-20T01:00:00Z"}])
    assert _main(REGISTRY, "--wave", "5", "--open-limit", "1", runner=git) == 0
    assert "wave: 0 of 5" in capsys.readouterr().out


def test_a_wave_dry_run_prints_the_plan_and_changes_nothing(feed, capsys):
    git = FakeGit()
    assert _main(REGISTRY, "--wave", "2", "--dry-run", runner=git) == 0
    out = capsys.readouterr().out
    assert "wave: 2 of 2" in out and "would import: 2" in out
    assert git.writes() == []
    assert [p.name for p in model.SOURCES.iterdir()] == [model.META_NAME]
    assert list(model.BLUEPRINTS.iterdir()) == []


def test_aliases_of_catalog_apps_go_in_their_own_pull_request(feed):
    (model.SOURCES / "curated.json").write_text(json.dumps({
        "id": "curated", "x-catena": {"domain": {"host": "h", "service": "app", "port": 80}}}))
    (model.BLUEPRINTS / "curated").mkdir()
    (model.BLUEPRINTS / "curated" / model.COMPOSE_NAME).write_text(
        "services:\n  app:\n    image: example/notes:2.0.0\n")
    git = FakeGit()
    assert _main(REGISTRY, "--wave", "5", runner=git) == 0
    switches = [c[3] for c in git.writes() if c[:3] == ["git", "switch", "--create"]]
    assert switches == ["import/web-stack", "import/aliases"]
    add = [c for c in git.writes() if c[:2] == ["git", "add"]][-1]
    assert add[add.index("--") + 1:] == ["sources/_aliases.json"]


REG = "https://registry-1.docker.io/v2/example/lab"
GHCR = "https://ghcr.io/v2/owner/tool"
SOURCE = provenance.SOURCE_LABEL


def _image_web(monkeypatch, extra: dict) -> None:
    for url, body in extra.items():
        monkeypatch.setitem(WEB, url, body if isinstance(body, bytes) else json.dumps(body).encode())


def test_the_image_label_names_the_upstream_repository(catalog, monkeypatch):
    _image_web(monkeypatch, {
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:example/lab:pull":
            {"token": "t"},
        f"{REG}/manifests/1.0": {"config": {"digest": "sha256:c1"}},
        f"{REG}/blobs/sha256:c1": {"config": {"Labels": {SOURCE: "https://github.com/example/lab-src"}}},
    })
    assert upstream.find("example/lab:1.0") == (
        pipeline.Repo("github.com", "example/lab-src"), "the image's source label")


def test_an_index_manifest_is_followed_to_its_linux_amd64_image_and_its_annotations(catalog, monkeypatch):
    _image_web(monkeypatch, {
        "https://ghcr.io/token?service=ghcr.io&scope=repository:owner/tool:pull": {"token": "t"},
        f"{GHCR}/manifests/2.0": {"manifests": [
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]},
        f"{GHCR}/manifests/sha256:amd": {
            "config": {"digest": "sha256:c2"},
            "annotations": {SOURCE: "https://github.com/owner/tool.git"}},
    })
    assert upstream.find("ghcr.io/owner/tool:2.0")[0] == pipeline.Repo("github.com", "owner/tool")


def test_a_docker_hub_repository_links_its_source_when_the_image_has_no_label(catalog, monkeypatch):
    _image_web(monkeypatch, {
        f"{provenance.HUB_API}/repositories/example/lab/": {"source": "https://github.com/example/linked"}})
    assert upstream.find("example/lab:1.0") == (
        pipeline.Repo("github.com", "example/linked"), "the Docker Hub repository's linked source")


def test_an_image_with_no_label_and_no_link_names_no_repository(catalog, monkeypatch):
    _image_web(monkeypatch, {f"{provenance.HUB_API}/repositories/example/lab/": {"source": None}})
    assert upstream.find("example/lab:1.0") == (None, "")
    assert upstream.find("quay.io/example/lab:1.0") == (None, "")


def test_an_entry_whose_upstream_repository_cannot_be_found_is_skipped(catalog, capsys, monkeypatch):
    _image_web(monkeypatch, {f"{provenance.HUB_API}/repositories/example/notes/": {"source": None}})
    assert _run() == 0
    out = capsys.readouterr().out
    assert ("Notes: the upstream repository is unknown: example/notes:1.2.3 names no source label "
            "and no linked source on Docker Hub") in out
    assert not (model.SOURCES / "notes.json").exists()
    assert (model.SOURCES / "web-stack.json").exists()


def test_a_found_upstream_repository_is_licence_checked_and_recorded(catalog, capsys, monkeypatch):
    _image_web(monkeypatch, {
        f"{provenance.HUB_API}/repositories/example/notes/": {"source": "https://github.com/example/notes-sspl"},
        f"{API}/repos/example/notes-sspl/license": _licence(
            "example/notes-sspl", "SSPL-1.0", "Server Side Public License\n")})
    assert _run() == 0
    assert "Notes: upstream project SSPL-1.0 (example/notes-sspl)" in capsys.readouterr().out
    cat = _source("web-stack")["x-catena"]
    assert cat["origin"]["project"] == "https://github.com/upstream/web"
    assert any("comes from the Docker Hub repository's linked source of example/web:2.1.0"
               in c for c in cat["pending"]["to_choose"])
