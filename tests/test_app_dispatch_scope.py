"""The scope step of app-dispatch.yml, run as bash over a scratch repository."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="the step runs the tools with uv")


def _step_script() -> str:
    doc = yaml.safe_load((ROOT / ".github/workflows/app-dispatch.yml").read_text())
    steps = doc["jobs"]["dispatch"]["steps"]
    return next(s["run"] for s in steps if s.get("id") == "scope")


@pytest.fixture
def scene(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    repo, runner = tmp_path / "repo", tmp_path / "runner"
    repo.mkdir()
    runner.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def commit(*files):
        for name in files:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.read_text() + "x" if path.exists() else "x")
        git("add", "-A")
        git("commit", "-qm", ",".join(files))
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "main")
    shas = {"root": commit("README.md")}
    shas["web"] = commit("blueprints/web/docker-compose.yml", "sources/web.json")
    shas["mail"] = commit("blueprints/mail/docker-compose.yml", "sources/mail.json")
    shas["notes"] = commit("notes.txt")
    git("update-ref", "refs/remotes/origin/main", shas["notes"])

    def run(*, commits="", before=None, branch="main", sha=None):
        out = runner / "out.txt"
        out.write_text("")
        step_env = {**env, "RUNNER_TEMP": str(runner), "GITHUB_OUTPUT": str(out), "TOOLS": str(ROOT),
                    "TEMPLATES_ROOT": str(repo), "SHA": sha or shas["notes"], "BEFORE": before or shas["root"],
                    "BRANCH": branch, "DEFAULT_BRANCH": "main", "COMMITS": commits}
        done = subprocess.run(["bash", "-c", _step_script()], cwd=repo, env=step_env,
                              capture_output=True, text=True, check=False)
        lines = (runner / "dispatches.txt").read_text().splitlines() if done.returncode == 0 else []
        return done, [line.split(" ") for line in lines]

    return run, shas, git, commit


def test_a_push_to_main_sends_each_commit_of_an_app_judged_from_its_parent(scene):
    run, shas, _, _ = scene
    done, lines = run()
    assert done.returncode == 0, done.stderr
    assert lines == [[shas["web"], shas["root"], "web"],
                     [shas["mail"], shas["web"], "mail"]]
    assert f"{shas['notes']}: not sent, it changes no app" in done.stdout


def test_the_commits_input_sends_those_commits_each_judged_from_its_parent(scene):
    run, shas, _, _ = scene
    done, lines = run(commits=f"{shas['web']} {shas['notes']}")
    assert done.returncode == 0, done.stderr
    assert lines == [[shas["web"], shas["root"], "web"]]


def test_a_commit_whose_app_main_changed_again_is_not_sent(scene):
    run, shas, git, commit = scene
    again = commit("blueprints/web/docker-compose.yml")
    git("update-ref", "refs/remotes/origin/main", again)
    done, lines = run(sha=again)
    assert done.returncode == 0, done.stderr
    assert lines == [[shas["mail"], shas["web"], "mail"], [again, shas["notes"], "web"]]
    assert f"{shas['web']}: not sent, web changed again on main" in done.stdout
    # A later push already on main makes an earlier push's commit of the app stale too.
    done, lines = run(commits=shas["web"])
    assert done.returncode == 0, done.stderr
    assert lines == []


def test_the_commits_input_is_used_when_the_before_is_unknown(scene):
    run, shas, _, _ = scene
    done, lines = run(commits=shas["mail"], before="0" * 40)
    assert done.returncode == 0, done.stderr
    assert lines == [[shas["mail"], shas["web"], "mail"]]


@pytest.mark.parametrize("commits", [
    "main", "abc", "HEAD", f"{'a' * 40},{'b' * 40}", f"{'a' * 40}  {'b' * 40}", f"{'A' * 40}",
    f"{'a' * 40} --upload-pack=x", f"{'a' * 39}",
])
def test_a_malformed_commits_list_fails(scene, commits):
    run, _, _, _ = scene
    done, _ = run(commits=commits)
    assert done.returncode == 1
    assert "full commit ids" in done.stdout + done.stderr


def test_a_branch_other_than_main_is_sent_as_a_whole_judged_from_where_it_left_main(scene):
    run, shas, git, commit = scene
    git("update-ref", "refs/remotes/origin/main", shas["notes"])
    git("checkout", "-q", "-b", "feat")
    head = commit("blueprints/web/docker-compose.yml")
    done, lines = run(branch="feat", sha=head, before="0" * 40)
    assert done.returncode == 0, done.stderr
    assert lines == [[head, shas["notes"], "web"]]


def test_a_branch_that_changes_two_apps_is_not_sent(scene):
    run, shas, git, commit = scene
    git("update-ref", "refs/remotes/origin/main", shas["notes"])
    git("checkout", "-q", "-b", "feat")
    commit("blueprints/web/docker-compose.yml")
    head = commit("blueprints/mail/docker-compose.yml")
    done, _ = run(branch="feat", sha=head, before="0" * 40)
    assert done.returncode == 1
