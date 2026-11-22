"""Unit tests for lib/adopt.py: the one-app plan, the API copy, the fork pull request it
reads and the pull request it opens, with a fake API."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import adopt  # noqa: E402

HEAD = "a" * 40
MAIN = "b" * 40


def _f(name, status="added", **kw):
    return {"filename": name, "status": status, **kw}


def test_a_change_inside_one_app_is_planned():
    p = adopt.plan([_f("blueprints/x/docker-compose.yml"), _f("blueprints/x/app.json", "modified"),
                    _f("blueprints/x/old.md", "removed")])
    assert p == {"app": "x", "copy": ["blueprints/x/app.json", "blueprints/x/docker-compose.yml"],
                 "delete": ["blueprints/x/old.md"]}


def test_a_rename_copies_the_new_path_and_deletes_the_old():
    p = adopt.plan([_f("blueprints/x/b.yml", "renamed", previous_filename="blueprints/x/a.yml")])
    assert p["copy"] == ["blueprints/x/b.yml"] and p["delete"] == ["blueprints/x/a.yml"]


@pytest.mark.parametrize("files,why", [
    ([_f("blueprints/x/a"), _f("blueprints/y/a")], "more than one app"),
    ([_f("blueprints/x/a"), _f("lib/versions.py")], "belongs to no app"),
    ([_f(".github/workflows/ci.yml")], "belongs to no app"),
    ([_f("blueprints/x/../../lib/a.py")], "unsafe path"),
    ([_f("/blueprints/x/a")], "unsafe path"),
    ([_f("blueprints/X/a")], "not a plain app id"),
    ([_f("blueprints/x/a", "renamed", previous_filename="blueprints/y/a")], "more than one app"),
    ([_f("catalog.json")], "belongs to no app"),
])
def test_a_change_outside_one_app_is_refused(files, why):
    with pytest.raises(adopt.AdoptError, match=why):
        adopt.plan(files)


class FakeApi:
    def __init__(self, entries, branch_exists=False, truncated=False):
        self.entries = entries
        self.branch_exists = branch_exists
        self.truncated = truncated
        self.calls = []

    def __call__(self, method, path, body=None, paginate=False):
        self.calls.append((method, path, body))
        if path.startswith("repos/fork/x/git/trees/"):
            return {"truncated": self.truncated, "tree": self.entries}
        if path.startswith("repos/fork/x/git/blobs/"):
            return {"encoding": "base64", "content": base64.b64encode(b"data").decode()}
        if path == "repos/o/r/git/blobs":
            return {"sha": "n" * 40}
        if path == "repos/o/r/git/ref/heads/main":
            return {"object": {"sha": MAIN}}
        if path.startswith("repos/o/r/git/commits/"):
            return {"tree": {"sha": "t" * 40}}
        if path == "repos/o/r/git/trees":
            return {"sha": "u" * 40}
        if path == "repos/o/r/git/commits":
            return {"sha": "c" * 40}
        if path == "repos/o/r/git/ref/heads/adopt/7":
            if self.branch_exists:
                return {}
            raise LookupError(path)
        return {}


def _entries(mode="100644", kind="blob", size=4):
    return [{"path": "blueprints/x/docker-compose.yml", "mode": mode, "type": kind, "sha": "s" * 40,
             "size": size}]


FILES = [_f("blueprints/x/docker-compose.yml"), _f("blueprints/x/gone.md", "removed")]


def test_the_app_lands_on_a_new_branch_of_the_base_repository():
    api = FakeApi(_entries())
    out = adopt.adopt(api, "o/r", "fork/x", HEAD, 7, FILES)
    assert out["branch"] == "adopt/7" and out["app"] == "x"
    tree = next(b for m, p, b in api.calls if p == "repos/o/r/git/trees")
    assert tree["base_tree"] == "t" * 40
    assert tree["tree"][0]["sha"] == "n" * 40 and tree["tree"][1]["sha"] is None
    assert api.calls[-1] == ("POST", "repos/o/r/git/refs", {"ref": "refs/heads/adopt/7", "sha": "c" * 40})
    commit = next(b for m, p, b in api.calls if p == "repos/o/r/git/commits")
    assert commit["parents"] == [MAIN] and HEAD not in commit["message"]


def test_an_existing_adopt_branch_is_moved_to_the_new_commit():
    api = FakeApi(_entries(), branch_exists=True)
    adopt.adopt(api, "o/r", "fork/x", HEAD, 7, FILES)
    assert api.calls[-1][0:2] == ("PATCH", "repos/o/r/git/refs/heads/adopt/7")


@pytest.mark.parametrize("entries,truncated,why", [
    (_entries(mode="120000"), False, "link or submodule"),
    (_entries(mode="160000", kind="commit"), False, "not a file"),
    (_entries(size=2_000_000), False, "larger than"),
    ([], False, "not a file"),
    (_entries(), True, "too large"),
])
def test_a_link_a_submodule_a_large_file_or_a_missing_file_is_refused(entries, truncated, why):
    api = FakeApi(entries, truncated=truncated)
    with pytest.raises(adopt.AdoptError, match=why):
        adopt.adopt(api, "o/r", "fork/x", HEAD, 7, FILES)
    assert not any(m == "POST" for m, _, _ in api.calls)


@pytest.mark.parametrize("sha,fork", [("main", "fork/x"), (HEAD, "fork"), (HEAD, "a/b;c")])
def test_a_malformed_sha_or_repository_is_refused(sha, fork):
    with pytest.raises(adopt.AdoptError):
        adopt.adopt(FakeApi([]), "o/r", fork, sha, 7, FILES)


def _pr(state="open", repo="fork/x", sha=HEAD, base="main"):
    return {"state": state, "head": {"sha": sha, "repo": {"full_name": repo} if repo else None},
            "base": {"ref": base}}


def test_an_open_pull_request_from_a_fork_names_its_fork_head_and_base():
    seen = []

    def api(method, path, body=None, paginate=False):
        seen.append((method, path))
        return _pr(base="dev")

    assert adopt.fork_pr(api, "o/r", 7) == {"fork": "fork/x", "sha": HEAD, "base": "dev"}
    assert seen == [("GET", "repos/o/r/pulls/7")]


@pytest.mark.parametrize("data,why", [
    (_pr(state="closed"), "not open"),
    (_pr(repo="o/r"), "not come from a fork"),
    (_pr(repo="O/R"), "not come from a fork"),
    (_pr(repo=None), "no longer exists"),
    (_pr(sha="main"), "40 lowercase hex"),
    (_pr(repo="a/b;c"), "owner/name"),
])
def test_a_pull_request_that_is_not_an_open_one_from_a_fork_is_refused(data, why):
    with pytest.raises(adopt.AdoptError, match=why):
        adopt.fork_pr(lambda *a, **k: data, "o/r", 7)


def test_the_adopt_pull_request_is_opened_into_the_fork_s_base():
    calls = []

    def api(method, path, body=None, paginate=False):
        calls.append((method, path, body))
        return [] if method == "GET" else {"html_url": "https://github.com/o/r/pull/8"}

    src = {"fork": "fork/x", "sha": HEAD, "base": "main"}
    url = adopt.open_pr(api, "o/r", 7, src, {"app": "x", "branch": "adopt/7"})
    assert url == "https://github.com/o/r/pull/8"
    assert calls[0][:2] == ("GET", "repos/o/r/pulls?state=open&head=o:adopt/7&per_page=100")
    method, path, body = calls[1]
    assert (method, path) == ("POST", "repos/o/r/pulls")
    assert body["head"] == "adopt/7" and body["base"] == "main"
    assert body["title"] == "feat(x): adopt pull request 7"
    assert "#7" in body["body"] and HEAD in body["body"]


def test_an_open_adopt_pull_request_is_reused():
    calls = []

    def api(method, path, body=None, paginate=False):
        calls.append(method)
        return [{"html_url": "https://github.com/o/r/pull/8"}]

    src = {"fork": "fork/x", "sha": HEAD, "base": "main"}
    assert adopt.open_pr(api, "o/r", 7, src, {"app": "x", "branch": "adopt/7"}) == "https://github.com/o/r/pull/8"
    assert calls == ["GET"]


@pytest.mark.parametrize("argv", [["o/r", "0"], ["o/r", "7x"], ["o/r;x", "7"], ["o/r", "-7"]])
def test_the_command_refuses_a_bad_repository_or_number(argv, capsys):
    assert adopt.main(argv) == 1
    assert "adopt refused" in capsys.readouterr().err


def test_an_apps_card_file_is_planned_like_any_file_of_the_app():
    p = adopt.plan([_f("blueprints/x/app.json"), _f("blueprints/x/logo.png"),
                    _f("blueprints/x/docker-compose.yml", "modified"),
                    _f("blueprints/x/old.md", "removed")])
    assert p == {"app": "x",
                 "copy": ["blueprints/x/app.json", "blueprints/x/docker-compose.yml",
                          "blueprints/x/logo.png"],
                 "delete": ["blueprints/x/old.md"]}


@pytest.mark.parametrize("name", ["catalog-meta.json", "catalog-aliases.json"])
def test_the_catalogs_own_files_are_no_apps_to_adopt(name):
    with pytest.raises(adopt.AdoptError, match="belongs to no app"):
        adopt.plan([_f("blueprints/x/app.json"), _f(name, "modified")])


def test_a_card_file_is_copied_as_data_like_any_file_of_the_app():
    entries = [{"path": "blueprints/x/app.json", "mode": "100644", "type": "blob", "sha": "s" * 40,
                "size": 4}]
    api = FakeApi(entries)
    out = adopt.adopt(api, "o/r", "fork/x", HEAD, 7, [_f("blueprints/x/app.json")])
    assert out["app"] == "x"
    tree = next(b for m, p, b in api.calls if p == "repos/o/r/git/trees")
    assert [i["path"] for i in tree["tree"]] == ["blueprints/x/app.json"]
