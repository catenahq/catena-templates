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
