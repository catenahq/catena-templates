"""Unit tests for lib/versions.py: version, revision, tag order, the
one-app scope check, the tested state and the same-change test."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import versions  # noqa: E402


@pytest.mark.parametrize("ref,version", [
    ("nextcloud:31.0.8-apache", "31.0.8"),
    ("outlinewiki/outline:1.10.1", "1.10.1"),
    ("redis:v7.4.11-alpine", "7.4.11"),
    ("ghcr.io/nextcloud-releases/aio-talk:20261014_095932", "20261014_095932"),
    ("app:2.1@sha256:abc", "2.1"),
    ("${APP_IMAGE:-ghcr.io/example/app:3.0.0}", "3.0.0"),
])
def test_the_version_is_the_number_the_image_tag_opens_with(ref, version):
    assert versions.version_of_image(ref) == version


@pytest.mark.parametrize("ref", ["nextcloud", "app:latest", "app:stable-alpine", "app@sha256:abc"])
def test_an_image_without_a_numeric_tag_has_no_version(ref):
    with pytest.raises(versions.VersionError):
        versions.version_of_image(ref)


def _source(service="app"):
    return {"x-catena": {"domain": {"host": "x", "service": service, "port": 80}}}


def _compose(block=None):
    doc = {"services": {
        "app": {"image": "nextcloud:31.0.8-apache"},
        "db": {"image": "postgres:18.6-alpine"},
    }}
    if block:
        doc["x-catena"] = block
    return doc


def test_the_version_comes_from_the_main_address_service():
    assert versions.app_version(_source("app"), _compose()) == "31.0.8"
    assert versions.app_version(_source("db"), _compose()) == "18.6"


def test_version_from_names_the_service_when_the_block_has_one():
    assert versions.app_version(_source("app"), _compose({"version_from": "db"})) == "18.6"


def test_a_service_without_an_image_has_no_version():
    with pytest.raises(versions.VersionError):
        versions.app_version(_source("worker"), _compose())


def test_the_revision_is_one_plus_the_highest_revision_at_that_version():
    tags = [
        "nextcloud/31.0.7-1",
        "nextcloud/31.0.8-1",
        "nextcloud/31.0.8-2",
        "other/31.0.8-1",
        "nextcloud/31.0.80-1",
        "nextcloud/not-a-version",
    ]
    assert versions.next_revision("nextcloud", "31.0.8", tags) == 3
    assert versions.next_revision("nextcloud", "31.0.9", tags) == 1
    assert versions.next_revision("nextcloud", "31.0.8", []) == 1
    assert versions.tag_name("nextcloud", "31.0.8", 3) == "nextcloud/31.0.8-3"
    # A deleted lower revision leaves a gap: the next tag follows the highest one.
    assert versions.next_revision("nextcloud", "34.0.5", ["nextcloud/34.0.5-2"]) == 3


def test_tags_order_by_version_then_revision_not_as_text():
    tags = [
        "app/1.10.0-1",
        "app/1.9.0-2",
        "app/1.9.0-10",
        "app/1.9.0-1",
        "app/2.0.0-1",
        "other/9.9.9-1",
        "app/junk",
    ]
    assert versions.sort_tags("app", tags) == [
        "app/1.9.0-1", "app/1.9.0-2", "app/1.9.0-10", "app/1.10.0-1", "app/2.0.0-1"]


def test_an_app_is_one_app_across_its_blueprint_and_its_source():
    assert versions.scope_errors([
        "blueprints/outline/docker-compose.yml",
        "blueprints/outline/README.md",
        "sources/outline.json",
        "templates.json",
        "catalog.json",
    ]) == []


def test_a_change_to_two_apps_fails_the_scope_check():
    errors = versions.scope_errors(["blueprints/outline/docker-compose.yml", "sources/kimai.json"])
    assert errors == ["the change touches 2 apps (kimai, outline); "
                      "a commit or pull request changes one app"]


def test_a_change_to_no_app_passes_the_scope_check():
    assert versions.scope_errors(["lib/model.py", "sources/_meta.json"]) == []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A catalog checkout holding the app `web` at image web:1.2.0, as versions reads it."""
    for key, value in (("GIT_CONFIG_GLOBAL", "/dev/null"), ("GIT_CONFIG_NOSYSTEM", "1"),
                       ("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.test"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.test")):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(versions, "ROOT", tmp_path)

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    def commit(image):
        (tmp_path / "blueprints" / "web").mkdir(parents=True, exist_ok=True)
        (tmp_path / "blueprints" / "web" / "docker-compose.yml").write_text(
            f"services:\n  web:\n    image: {image}\n")
        (tmp_path / "sources").mkdir(exist_ok=True)
        (tmp_path / "sources" / "web.json").write_text(json.dumps(_source("web")))
        git("add", "-A")
        git("commit", "-qm", image)

    git("init", "-q")
    commit("web:1.2.0")
    return git, commit


def test_an_app_without_a_tag_is_untested_and_a_pass_creates_its_first(repo):
    assert versions.tested_state("web") == (versions.UNTESTED, None)
    assert versions.plan("web") == "create web/1.2.0-1"


def test_a_pass_marks_the_hand_made_tag_that_holds_the_same_files(repo):
    git, _ = repo
    git("tag", "web/1.2.0-1")
    assert versions.tested_state("web") == (versions.UNTESTED, "web/1.2.0-1")
    assert versions.plan("web") == "mark web/1.2.0-1"


def test_an_app_whose_newest_tag_a_pass_made_is_tested(repo):
    git, _ = repo
    git("tag", "web/1.2.0-1")
    git("tag", "-a", "web/1.2.0-2", "-m", f"{versions.PASSED} at abc: https://x")
    assert versions.tested_state("web") == (versions.TESTED, "web/1.2.0-2")
    assert versions.plan("web") == "skip web/1.2.0-2"


def test_an_app_changed_since_its_tag_gets_a_new_one(repo):
    git, commit = repo
    git("tag", "-a", "web/1.2.0-1", "-m", f"{versions.PASSED} at abc: https://x")
    commit("web:1.3.0")
    assert versions.tested_state("web") == (versions.CHANGED, "web/1.2.0-1")
    assert versions.plan("web") == "create web/1.3.0-1"


def test_a_late_pass_of_an_older_commit_leaves_the_newer_tag_the_newest(repo):
    # Hosts take the highest tag: a tag on the older commit would take them back to it.
    git, commit = repo
    older = subprocess.run(["git", "-C", str(versions.ROOT), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    commit("web:1.2.0\n    restart: always")
    git("tag", "-a", "web/1.2.0-1", "-m", f"{versions.PASSED} at def: https://x")
    assert versions.tested_state("web", older) == (versions.CHANGED, "web/1.2.0-1")
    assert versions.plan("web", older) == "skip web/1.2.0-1"


def _head():
    return subprocess.run(["git", "-C", str(versions.ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _outside_file(git, name):
    """A commit that changes a file outside the app."""
    (versions.ROOT / name).write_text(name)
    git("add", "-A")
    git("commit", "-qm", name)


def test_a_plan_reads_the_passed_commit_not_the_checkout(repo):
    # app-tag.yml runs the default branch's tools over a commit that may predate them.
    git, _ = repo
    older = _head()
    _outside_file(git, "notes.txt")
    assert versions.plan("web", older) == "create web/1.2.0-1"
    assert versions.plan("web") == "create web/1.2.0-1"


def test_a_pass_of_a_commit_whose_app_main_has_since_changed_is_skipped(repo):
    git, commit = repo
    older = _head()
    commit("web:1.3.0")
    assert versions.plan("web", older) == "skip -"
    git("tag", "-a", "web/1.3.0-1", "-m", f"{versions.PASSED} at {'a' * 40}: https://x")
    assert versions.plan("web", older) == "skip web/1.3.0-1"


def _squash(repo, main_move, merged_image):
    """(sha, base, cand): a branch holding `web:1.3.0`, and the commit that squashes it onto main."""
    git, commit = repo
    git("branch", "-M", "main")
    git("checkout", "-q", "-b", "br")
    commit("web:1.3.0")
    cand = _head()
    git("checkout", "-q", "main")
    main_move(git, commit)
    base = _head()
    commit(merged_image)
    return _head(), base, cand


def test_a_squash_of_the_same_files_is_the_same_change_whatever_main_did_outside_the_app(repo):
    sha, base, cand = _squash(repo, lambda git, _: _outside_file(git, "notes.txt"), "web:1.3.0")
    assert versions.same_change("web", sha, base, cand)
    assert versions.main(["same", "web", "--ref", sha, "--base", base, "--from", cand]) == 0


def test_a_squash_that_holds_other_app_files_than_the_branch_is_a_different_change(repo):
    sha, base, cand = _squash(repo, lambda git, _: _outside_file(git, "notes.txt"), "web:1.4.0")
    assert not versions.same_change("web", sha, base, cand)
    assert versions.main(["same", "web", "--ref", sha, "--base", base, "--from", cand]) == 1


def test_main_changing_the_app_between_the_two_forks_is_a_different_change(repo):
    sha, base, cand = _squash(repo, lambda _, commit: commit("web:1.1.0"), "web:1.3.0")
    assert not versions.same_change("web", sha, base, cand)


def test_a_commit_is_the_same_change_as_itself(repo):
    _, commit = repo
    base = _head()
    commit("web:1.3.0")
    sha = _head()
    assert versions.same_change("web", sha, base, sha)


def test_a_candidate_absent_from_the_checkout_is_not_the_same_change(repo):
    _, commit = repo
    base = _head()
    commit("web:1.3.0")
    sha = _head()
    assert not versions.same_change("web", sha, base, "0" * 40)
    assert versions.main(["same", "web", "--ref", sha, "--base", base, "--from", "0" * 40]) == 1


def test_same_takes_only_full_lowercase_shas_and_plain_app_ids(repo):
    sha = _head()
    assert versions.main(["same", "web", "--ref", sha, "--base", sha, "--from=--upload-pack=x"]) == 1
    assert versions.main(["same", "web", "--ref", sha, "--base", sha, "--from", sha.upper()]) == 1
    assert versions.main(["same", "../web", "--ref", sha, "--base", sha, "--from", sha]) == 1
    assert versions.main(["same", "web", "--ref", sha, "--base", sha, "--from", sha]) == 0


def test_tested_run_is_the_address_of_the_run_that_made_the_newest_tag(repo, capsys):
    git, _ = repo
    run = "https://github.com/o/ops/actions/runs/7"
    git("tag", "-a", "web/1.2.0-1", "-m", f"{versions.PASSED} at {'a' * 40}: {run}")
    assert versions.tested_run("web") == run
    assert versions.main(["tested-run", "web"]) == 0
    assert capsys.readouterr().out == run + "\n"


def test_tested_run_is_empty_for_a_hand_made_newest_tag(repo, capsys):
    git, _ = repo
    git("tag", "-a", "web/1.2.0-1", "-m", f"{versions.PASSED} at {'a' * 40}: https://x")
    git("tag", "web/1.2.0-2")
    assert versions.tested_run("web") is None
    assert versions.main(["tested-run", "web"]) == 0
    assert capsys.readouterr().out == ""


def test_tested_run_is_empty_for_changed_files(repo):
    git, commit = repo
    git("tag", "-a", "web/1.2.0-1", "-m", f"{versions.PASSED} at {'a' * 40}: https://x")
    commit("web:1.3.0")
    assert versions.tested_run("web") is None


def test_templates_root_points_the_tags_at_another_tree(repo, tmp_path):
    git, _ = repo
    git("tag", "web/1.2.0-1")
    env = {**os.environ, "TEMPLATES_ROOT": str(tmp_path), "PYTHONPATH": str(ROOT)}
    done = subprocess.run([sys.executable, str(ROOT / "build" / "versions.py"), "plan", "web"],
                          cwd=ROOT.parent, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "mark web/1.2.0-1"


def test_last_change_is_the_newest_commit_that_touched_the_app(repo, capsys):
    git, commit = repo
    first = _head()
    _outside_file(git, "notes.txt")
    assert versions.last_change("web", "HEAD") == first
    commit("web:1.3.0")
    second = _head()
    _outside_file(git, "more.txt")
    assert versions.last_change("web") == second
    assert versions.last_change("web", first) == first
    assert versions.main(["last-change", "web", "--ref", "HEAD"]) == 0
    assert capsys.readouterr().out == second + "\n"


def test_last_change_follows_first_parents_so_a_merge_is_the_change(repo):
    git, commit = repo
    git("branch", "-M", "main")
    git("checkout", "-q", "-b", "br")
    commit("web:1.3.0")
    git("checkout", "-q", "main")
    _outside_file(git, "notes.txt")
    git("merge", "--no-ff", "-qm", "merge br", "br")
    assert versions.last_change("web") == _head()


def test_last_change_prints_nothing_for_an_app_no_commit_touched(repo, capsys):
    assert versions.last_change("other") == ""
    assert versions.main(["last-change", "other"]) == 0
    assert capsys.readouterr().out == ""


def test_last_change_refuses_an_id_that_is_not_a_plain_slug(repo, capsys):
    assert versions.main(["last-change", "../web"]) == 1
    assert "not a plain app id" in capsys.readouterr().err


def test_tested_lists_the_apps_that_need_a_run(repo, capsys):
    assert versions.main(["tested"]) == 0
    assert capsys.readouterr().out == "web\tuntested\t-\n"
    assert versions.main(["tested", "--needs-run"]) == 0
    assert capsys.readouterr().out == "web\n"


def test_every_app_in_the_catalog_has_a_readable_version():
    for path in sorted((ROOT / "sources").glob("*.json")):
        if path.name == "_meta.json":
            continue
        app = path.stem
        tag = versions.next_tag(app, [])
        assert versions.parse_tag(app, tag) is not None, tag


def test_scope_command_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("sources/a.json\nsources/b.json\n"))
    assert versions.main(["scope"]) == 1
    assert "touches 2 apps" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("sources/a.json\n"))
    assert versions.main(["scope"]) == 0


# Classes of a change.

RUNTIME, VERIFICATION, PRESENTATION = versions.RUNTIME, versions.VERIFICATION, versions.PRESENTATION
COMPOSE = "blueprints/web/docker-compose.yml"
SOURCE = "sources/web.json"
CARD = "blueprints/web/app.json"


def _put(git, files=None, remove=()):
    """A commit that writes `files` (path -> text) and deletes `remove`; its id."""
    for path, text in (files or {}).items():
        target = versions.ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for path in remove:
        (versions.ROOT / path).unlink()
    git("add", "-A")
    git("commit", "-qm", "change")
    return _head()


def _card(block=None, **top):
    doc = {"id": "web", "title": "Web", **top}
    doc["x-catena"] = {"domain": {"host": "x", "service": "web", "port": 80},
                       "en": {"display_name": "Web"}, "bench": {"pack": "a"}, **(block or {})}
    return json.dumps(doc)


def _flat_card(**fields):
    return json.dumps({"schema": 1, "en": {"display_name": "Web"}, "bench": {"pack": "a"}, **fields})


def _classes(a, b):
    return versions.changed_classes("web", a, b)


def _only(git, path, text):
    """A commit where `path` is the app's only card file, holding `text`."""
    other = SOURCE if path == CARD else CARD
    return _put(git, {path: text}, remove=[other] if (versions.ROOT / other).exists() else ())


def test_every_schema_property_has_a_class():
    names = set()
    for name in ("sources.schema.json", "app.schema.json"):
        path = ROOT / name
        if path.exists():
            props = json.loads(path.read_text())["properties"]
            names |= (set(props) - {"x-catena"}) | set(props.get("x-catena", {}).get("properties", {}))
    assert names
    assert names <= versions.FIELD_CLASSES.keys(), sorted(names - versions.FIELD_CLASSES.keys())
    assert set(versions.FIELD_CLASSES.values()) | set(versions.FILE_CLASSES.values()) <= {
        RUNTIME, VERIFICATION, PRESENTATION}


@pytest.mark.parametrize("name,expected", [
    ("docker-compose.yml", RUNTIME),
    ("test.yml", VERIFICATION),
    ("README.md", PRESENTATION),
    ("logo.png", PRESENTATION),
    ("logo.svg", PRESENTATION),
    ("notes.txt", RUNTIME),
    ("hooks/verify.sh", RUNTIME),
    ("services.yml", RUNTIME),
])
def test_a_file_takes_its_class_when_changed_added_or_removed(repo, name, expected):
    git, _ = repo
    path = f"blueprints/web/{name}"
    start = _put(git, {path: "one"} if name != "docker-compose.yml" else {path: "services: {}\n"})
    changed = _put(git, {path: "two"})
    removed = _put(git, remove=[path])
    assert _classes(start, changed) == {expected}
    assert _classes(changed, removed) == {expected}
    assert _classes(removed, start) == {expected}


def test_equal_objects_and_files_outside_the_app_change_nothing(repo):
    git, _ = repo
    before = _head()
    after = _put(git, {"notes.txt": "x", "sources/other.json": "{}"})
    assert _classes(before, after) == frozenset()
    assert _classes(before, before) == frozenset()


def test_a_mode_change_of_the_compose_is_runtime(repo):
    git, _ = repo
    before = _head()
    (versions.ROOT / COMPOSE).chmod(0o755)
    git("add", "-A")
    git("commit", "-qm", "mode")
    assert _classes(before, _head()) == {RUNTIME}


@pytest.mark.parametrize("block,top,expected", [
    ({"en": {"display_name": "Webby"}}, {}, PRESENTATION),
    ({"fr": {"display_name": "Toile"}}, {}, PRESENTATION),
    ({"sizing": {"peak_ram_mb": 5}}, {}, PRESENTATION),
    ({"upstream_url": "https://x"}, {}, PRESENTATION),
    ({}, {"title": "Webby"}, PRESENTATION),
    ({"bench": {"pack": "b"}}, {}, VERIFICATION),
    ({"app_name": "catena-web"}, {}, VERIFICATION),
    ({"domain": {"host": "y", "service": "web", "port": 80}}, {}, RUNTIME),
    ({"requires_catena": "0.11.0"}, {}, RUNTIME),
    ({"env_required_when": {}}, {}, RUNTIME),
    ({"field_nobody_listed": 1}, {}, RUNTIME),
    ({}, {"id": "other"}, RUNTIME),
])
def test_a_source_field_takes_its_class(repo, block, top, expected):
    git, _ = repo
    start = _put(git, {SOURCE: _card()})
    changed = _put(git, {SOURCE: _card(block, **top)})
    assert _classes(start, changed) == {expected}


def test_a_change_of_several_fields_holds_each_class(repo):
    git, _ = repo
    start = _put(git, {SOURCE: _card()})
    changed = _put(git, {SOURCE: _card({"en": {"display_name": "B"}, "bench": {"pack": "b"}})})
    assert _classes(start, changed) == {PRESENTATION, VERIFICATION}


def test_a_card_file_changed_in_form_alone_is_presentation(repo):
    git, _ = repo
    start = _only(git, CARD, _flat_card())
    changed = _only(git, CARD, json.dumps(json.loads(_flat_card()), indent=2))
    assert _classes(start, changed) == {PRESENTATION}


def test_an_app_json_field_takes_its_class(repo):
    git, _ = repo
    start = _only(git, CARD, _flat_card())
    assert _classes(start, _put(git, {CARD: _flat_card(en={"display_name": "B"})})) == {PRESENTATION}
    start = _head()
    kept = {"en": {"display_name": "B"}}
    assert _classes(start, _put(git, {CARD: _flat_card(bench={"pack": "z"}, **kept)})) == {VERIFICATION}
    start = _head()
    assert _classes(start, _put(git, {CARD: _flat_card(bench={"pack": "z"}, requires_catena="1", **kept)})) == {RUNTIME}


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"x-catena": 3}', '{"en": 1, "x-catena": {"en": 2}}'])
def test_a_card_file_that_does_not_parse_to_fields_is_runtime(repo, text):
    git, _ = repo
    for path in (SOURCE, CARD):
        start = _only(git, path, _card() if path == SOURCE else _flat_card())
        assert _classes(start, _only(git, path, text)) == {RUNTIME}
        assert _classes(_head(), start) == {RUNTIME}


def test_a_card_that_appears_or_goes_is_compared_with_no_fields(repo):
    git, _ = repo
    before = _put(git, remove=[SOURCE])
    added = _put(git, {CARD: _flat_card()})
    everything = {PRESENTATION, VERIFICATION, RUNTIME}
    assert _classes(before, added) == everything
    assert _classes(added, _put(git, remove=[CARD])) == everything


def test_both_names_at_one_commit_are_runtime_unless_neither_changed(repo):
    git, _ = repo
    start = _put(git, {SOURCE: _card()})
    both = _put(git, {CARD: _flat_card()})
    assert _classes(start, both) == {RUNTIME}
    more = _put(git, {"blueprints/web/README.md": "r"})
    assert _classes(both, more) == {PRESENTATION}


def test_a_source_moved_to_app_json_is_compared_field_by_field(repo):
    git, _ = repo
    block = {"en": {"display_name": "Web"}, "bench": {"pack": "a"}}
    start = _put(git, {SOURCE: _card()})
    moved = {"id": "web", "title": "Web", "en": block["en"], "bench": block["bench"]}

    def move(card):
        return _put(git, {CARD: json.dumps(card)}, remove=[SOURCE])

    assert _classes(start, move({**moved, "domain": {"host": "x", "service": "web", "port": 80}})) == {PRESENTATION}
    back = _put(git, {SOURCE: _card()}, remove=[CARD])
    assert _classes(back, move({**moved, "en": {"display_name": "B"},
                                "domain": {"host": "x", "service": "web", "port": 80}})) == {PRESENTATION}
    back = _put(git, {SOURCE: _card()}, remove=[CARD])
    assert _classes(back, move(moved)) == {RUNTIME}
    back = _put(git, {SOURCE: _card()}, remove=[CARD])
    assert _classes(back, move({**moved, "bench": {"pack": "q"},
                                "domain": {"host": "x", "service": "web", "port": 80}})) == {VERIFICATION}


def test_the_classes_command_prints_one_per_line(repo, capsys):
    git, _ = repo
    before = _head()
    after = _put(git, {"blueprints/web/README.md": "r", "blueprints/web/test.yml": "t"})
    assert versions.main(["classes", "web", "--ref", after, "--base", before]) == 0
    assert capsys.readouterr().out == "presentation\nverification\n"
    assert versions.main(["classes", "web", "--ref", after, "--base", after]) == 0
    assert capsys.readouterr().out == ""
    assert versions.main(["classes", "web", "--ref", "HEAD", "--base", before]) == 1
    assert versions.main(["classes", "../web", "--ref", after, "--base", before]) == 1


# The four states.

def _passed(git, name="web/1.2.0-1"):
    git("tag", "-a", name, "-m", f"{versions.PASSED} at {'a' * 40}: https://x/runs/1")


def test_a_verification_diff_leaves_a_tested_app_tested(repo):
    git, _ = repo
    _passed(git)
    _put(git, {"blueprints/web/test.yml": "seed: []"})
    assert versions.tested_state("web") == (versions.TESTED, "web/1.2.0-1")
    assert versions.plan("web") == "skip web/1.2.0-1"


def test_a_presentation_diff_is_the_presentation_state_and_the_plan_names_its_origin(repo):
    git, _ = repo
    _passed(git)
    _put(git, {"blueprints/web/README.md": "new words"})
    assert versions.tested_state("web") == (versions.PRESENTATION, "web/1.2.0-1")
    assert versions.plan("web") == "create web/1.2.0-2 web/1.2.0-1"


def test_a_presentation_pass_of_an_older_commit_is_skipped(repo):
    git, _ = repo
    older = _put(git, {"blueprints/web/README.md": "one"})
    _put(git, {"blueprints/web/README.md": "two"})
    _passed(git)
    _put(git, {"blueprints/web/README.md": "one"})
    assert versions.tested_state("web", older) == (versions.PRESENTATION, "web/1.2.0-1")
    assert versions.plan("web", older) == "skip web/1.2.0-1"


def test_a_runtime_diff_is_changed_even_beside_presentation(repo):
    git, commit = repo
    _passed(git)
    commit("web:1.3.0")
    _put(git, {"blueprints/web/README.md": "new words"})
    assert versions.tested_state("web") == (versions.CHANGED, "web/1.2.0-1")
    assert versions.plan("web") == "create web/1.3.0-1"


def test_verification_with_presentation_is_changed_and_creates_a_tag_without_origin(repo):
    git, _ = repo
    _passed(git)
    _put(git, {"blueprints/web/README.md": "new words", "blueprints/web/test.yml": "seed: []"})
    assert versions.tested_state("web") == (versions.CHANGED, "web/1.2.0-1")
    assert versions.plan("web") == "create web/1.2.0-2"


def test_a_hand_made_tag_is_untested_after_a_verification_diff_and_changed_after_a_presentation_diff(repo):
    git, _ = repo
    git("tag", "web/1.2.0-1")
    _put(git, {"blueprints/web/test.yml": "seed: []"})
    assert versions.tested_state("web") == (versions.UNTESTED, "web/1.2.0-1")
    assert versions.plan("web") == "mark web/1.2.0-1"
    _put(git, {"blueprints/web/README.md": "new words"})
    assert versions.tested_state("web") == (versions.CHANGED, "web/1.2.0-1")
    assert versions.plan("web") == "create web/1.2.0-2"


def test_needs_run_lists_neither_tested_nor_presentation_apps(repo, capsys):
    git, commit = repo
    _passed(git)
    _put(git, {"blueprints/web/README.md": "new words"})
    assert versions.main(["tested"]) == 0
    assert capsys.readouterr().out == "web\tpresentation\tweb/1.2.0-1\n"
    assert versions.main(["tested", "--needs-run"]) == 0
    assert capsys.readouterr().out == ""
    commit("web:1.3.0")
    assert versions.main(["tested", "--needs-run"]) == 0
    assert capsys.readouterr().out == "web\n"


# The run a tag carries, and the same change.

def test_tested_run_follows_a_presentation_diff_and_refuses_a_verification_one(repo):
    git, _ = repo
    _passed(git)
    assert versions.tested_run("web") == "https://x/runs/1"
    _put(git, {"blueprints/web/README.md": "new words"})
    assert versions.tested_run("web") == "https://x/runs/1"
    _put(git, {"blueprints/web/test.yml": "seed: []"})
    assert versions.tested_run("web") is None


def test_tested_run_refuses_a_runtime_diff_and_a_hand_made_tag(repo):
    git, commit = repo
    git("tag", "web/1.2.0-1")
    assert versions.tested_run("web") is None
    _passed(git, "web/1.2.0-2")
    commit("web:1.3.0")
    assert versions.tested_run("web") is None


def _branch_squash(repo, branch_files, squash_files):
    """(sha, base, cand): a branch holding `branch_files`, squashed onto main as `squash_files`."""
    git, _ = repo
    git("branch", "-M", "main")
    base = _head()
    git("checkout", "-q", "-b", "br")
    cand = _put(git, branch_files)
    git("checkout", "-q", "main")
    return _put(git, squash_files), base, cand


def test_a_squash_differing_in_presentation_alone_is_the_same_change(repo):
    sha, base, cand = _branch_squash(repo, {"blueprints/web/README.md": "a"}, {"blueprints/web/README.md": "b"})
    assert versions.same_change("web", sha, base, cand)
    assert versions.main(["same", "web", "--ref", sha, "--base", base, "--from", cand]) == 0


def test_a_squash_differing_in_the_tests_is_a_different_change(repo):
    sha, base, cand = _branch_squash(repo, {"blueprints/web/test.yml": "a"}, {"blueprints/web/test.yml": "b"})
    assert not versions.same_change("web", sha, base, cand)


def test_a_squash_with_another_compose_is_a_different_change(repo):
    sha, base, cand = _branch_squash(repo, {COMPOSE: "services:\n  web:\n    image: web:1.3.0\n"},
                                     {COMPOSE: "services:\n  web:\n    image: web:1.4.0\n"})
    assert not versions.same_change("web", sha, base, cand)


def test_main_changing_the_tests_between_the_forks_is_a_different_change(repo):
    git, _ = repo
    git("branch", "-M", "main")
    git("checkout", "-q", "-b", "br")
    cand = _put(git, {"blueprints/web/README.md": "a"})
    git("checkout", "-q", "main")
    _put(git, {"blueprints/web/test.yml": "moved"})
    base = _head()
    sha = _put(git, {"blueprints/web/README.md": "a"})
    assert not versions.same_change("web", sha, base, cand)


# The last runtime change.

def test_runtime_change_is_the_newest_commit_that_added_the_app_or_changed_its_runtime(repo, capsys):
    git, commit = repo
    added = _head()
    _put(git, {"blueprints/web/README.md": "r"})
    _put(git, {"blueprints/web/test.yml": "t"})
    assert versions.runtime_change("web") == added
    commit("web:1.3.0")
    runtime = _head()
    _put(git, {"blueprints/web/README.md": "again"})
    _outside_file(git, "notes.txt")
    assert versions.runtime_change("web") == runtime
    assert versions.runtime_change("web", added) == added
    assert versions.main(["runtime-change", "web", "--ref", "HEAD"]) == 0
    assert capsys.readouterr().out == runtime + "\n"


def test_runtime_change_counts_a_change_of_a_runtime_card_field(repo):
    git, _ = repo
    _put(git, {SOURCE: _card()})
    _put(git, {SOURCE: _card({"en": {"display_name": "B"}})})
    runtime = _put(git, {SOURCE: _card({"requires_catena": "0.11.0"})})
    _put(git, {SOURCE: _card({"requires_catena": "0.11.0", "bench": {"pack": "z"}})})
    assert versions.runtime_change("web") == runtime


def test_runtime_change_is_empty_for_an_unknown_app_and_refuses_an_option(repo, capsys):
    assert versions.runtime_change("other") == ""
    assert versions.main(["runtime-change", "other", "--ref", "HEAD"]) == 0
    assert capsys.readouterr().out == ""
    assert versions.main(["runtime-change", "web", "--ref=--output=x"]) == 1


# The version service.

def _labelled(**labels):
    return {"services": {
        name: {"image": f"{name}:1.0", "labels": found} for name, found in labels.items()}}


def test_the_version_service_is_the_only_service_that_carries_a_route_host():
    compose = _labelled(app=["vps.route.host=${DOMAIN_HOST}"], db=["a=b"])
    assert versions.version_service(None, compose) == "app"
    assert versions.app_version(None, compose) == "1.0"


def test_the_version_service_is_the_marked_one_among_several_hosts():
    compose = _labelled(admin={"vps.route.host": "a"}, app={"vps.route.host": "b", "vps.route.main": "true"})
    assert versions.version_service({"x-catena": {}}, compose) == "app"


@pytest.mark.parametrize("compose", [
    _labelled(admin=["vps.route.host=a"], app=["vps.route.host=b"]),
    _labelled(db=["a=b"]),
    _labelled(),
])
def test_an_app_with_no_single_main_address_has_no_version_service(compose):
    with pytest.raises(versions.VersionError):
        versions.version_service(None, compose)


def test_version_from_and_the_source_come_before_the_labels():
    compose = _labelled(app=["vps.route.host=a"], db=["a=b"])
    assert versions.version_service(None, {**compose, "x-catena": {"version_from": "db"}}) == "db"
    assert versions.version_service(_source("db"), compose) == "db"


def test_the_next_tag_of_an_app_without_a_source_reads_the_labels(repo):
    git, _ = repo
    _put(git, {COMPOSE: "services:\n  web:\n    image: web:2.0.1\n    labels:\n      - vps.route.host=x\n"},
         remove=[SOURCE])
    assert versions.next_tag("web", []) == "web/2.0.1-1"


# Classes of a link or a submodule standing where a file stood.

def _replace_with_link(git, path):
    (versions.ROOT / path).unlink()
    (versions.ROOT / path).symlink_to("elsewhere")
    git("add", "-A")
    git("commit", "-qm", "link")
    return _head()


def _replace_with_submodule(git, path):
    git("rm", "-q", "--cached", path)
    (versions.ROOT / path).unlink()
    git("update-index", "--add", "--cacheinfo", f"160000,{_head()},{path}")
    git("commit", "-qm", "submodule")
    return _head()


@pytest.mark.parametrize("name", ["README.md", "logo.png", "test.yml", "app.json"])
@pytest.mark.parametrize("swap", [_replace_with_link, _replace_with_submodule])
def test_a_light_file_turned_into_a_link_or_a_submodule_is_runtime(repo, name, swap):
    git, _ = repo
    path = f"blueprints/web/{name}"
    start = _put(git, {path: _flat_card() if name == "app.json" else "one"}, remove=())
    swapped = swap(git, path)
    assert _classes(start, swapped) == {RUNTIME}
    assert _classes(swapped, start) == {RUNTIME}


def test_an_executable_bit_on_a_light_file_keeps_its_class(repo):
    git, _ = repo
    start = _put(git, {"blueprints/web/README.md": "one"})
    (versions.ROOT / "blueprints/web/README.md").chmod(0o755)
    git("add", "-A")
    git("commit", "-qm", "mode")
    assert _classes(start, _head()) == {PRESENTATION}


# A field differs by its JSON form, not by Python equality.

def test_a_number_written_as_a_float_is_another_value(repo):
    git, _ = repo
    port = lambda value: _card({"domain": {"host": "x", "service": "web", "port": value}})  # noqa: E731
    start = _put(git, {SOURCE: port(8080)})
    assert _classes(start, _put(git, {SOURCE: port(8080.0)})) == {RUNTIME}
    start = _head()
    assert _classes(start, _put(git, {SOURCE: port(True)})) == {RUNTIME}


def test_the_order_of_keys_inside_a_field_is_not_a_difference(repo):
    git, _ = repo
    start = _put(git, {SOURCE: _card({"domain": {"host": "x", "service": "web", "port": 80}})})
    reordered = _put(git, {SOURCE: _card({"domain": {"port": 80, "service": "web", "host": "x"}})})
    assert _classes(start, reordered) == {PRESENTATION}


# The main address, as catena-admin reads it.

def _routed(**marks):
    """A compose whose services a, b, c each carry vps.route.host; `marks` maps a service to its main label."""
    services = {}
    for name in ("a", "b", "c"):
        labels = [f"vps.route.host={name}.example.test"]
        if name in marks:
            labels.append(f"vps.route.main={marks[name]}")
        services[name] = {"image": f"{name}:{ord(name) - 96}.0", "labels": labels}
    return {"services": services}


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1", " true "])
def test_the_last_service_marked_main_is_the_main_address(value):
    assert versions.version_service(None, _routed(a=value, b=value)) == "b"
    assert versions.app_version(None, _routed(a=value, b=value)) == "2.0"


def test_a_label_catena_admin_does_not_read_as_true_marks_nothing():
    for value in ("on", "false", "0", ""):
        with pytest.raises(versions.VersionError):
            versions.version_service(None, _routed(b=value))


def test_the_only_address_needs_no_mark():
    compose = _routed()
    del compose["services"]["b"], compose["services"]["c"]
    assert versions.version_service(None, compose) == "a"
    with pytest.raises(versions.VersionError):
        versions.version_service(None, _routed())
