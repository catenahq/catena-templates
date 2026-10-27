"""The carry step of app-dispatch.yml, run as bash over a scratch repository with a stub `gh`."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RUN = "https://github.com/o/ops/actions/runs/5"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="the step runs the tools with uv")


def _step_script() -> str:
    doc = yaml.safe_load((ROOT / ".github/workflows/app-dispatch.yml").read_text())
    steps = doc["jobs"]["dispatch"]["steps"]
    return next(s["run"] for s in steps if s.get("id") == "carry")


@pytest.fixture
def scene(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    repo, bin_dir, runner = tmp_path / "repo", tmp_path / "bin", tmp_path / "runner"
    for d in (repo, bin_dir, runner):
        d.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "blueprints" / "web").mkdir(parents=True)
    (repo / "sources").mkdir()
    (repo / "blueprints" / "web" / "docker-compose.yml").write_text("a")
    (repo / "sources" / "web.json").write_text("{}")
    git("add", "-A")
    git("commit", "-qm", "one")
    base = git("rev-parse", "HEAD")
    (repo / "notes.txt").write_text("n")
    git("add", "-A")
    git("commit", "-qm", "two")
    sha = git("rev-parse", "HEAD")
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/bash\ncase "$2" in\n'
                  '  */statuses) printf "%b\\n" "$STATUS" ;;\nesac\n')
    gh.chmod(0o755)
    (runner / "dispatches.txt").write_text(f"{sha} {base} web\n")

    def run(description, state="success", token="x", branch="feat"):
        status = f"{state}\\tcatenahq-renovate[bot]\\t{RUN}\\t{description}"
        step_env = {**env, "PATH": f"{bin_dir}:{env['PATH']}", "RUNNER_TEMP": str(runner),
                    "TEMPLATES_ROOT": str(repo), "TOOLS": str(ROOT), "GH_TOKEN": token,
                    "APP_SLUG": "catenahq-renovate", "OWNER": "o", "REPO_NAME": "r", "BRANCH": branch,
                    "DEFAULT_BRANCH": "main", "BEFORE": "0" * 40, "STATUS": status}
        done = subprocess.run(["bash", "-c", _step_script()], cwd=repo, env=step_env,
                              capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        return (runner / "payloads.txt").read_text().strip().split(" "), sha, base

    return run


@pytest.mark.parametrize("description", [
    "passed 1 app(s): web",
    "passed 1 app(s): web (the same change as run 4)",
])
def test_a_pass_that_names_exactly_this_app_is_carried(scene, description):
    fields, sha, base = scene(description)
    assert fields == [sha, base, "web", RUN, "success"]


@pytest.mark.parametrize("description", [
    "passed 1 app(s): mail",
    "passed 1 app(s): web-x",
    "passed 2 app(s): mail, web",
    "no app in scope",
    "failed: web (install)",
])
def test_a_pass_that_names_another_app_is_not_carried(scene, description):
    fields, sha, base = scene(description)
    assert fields == [sha, base, "web", "-", "-"]


def test_a_failure_of_this_app_is_carried_on_a_branch_only(scene):
    fields, sha, base = scene("failed: web (install)", state="failure")
    assert fields[3:] == [RUN, "failure"]
    assert scene("failed: web (install)", state="failure", branch="main")[0][3:] == ["-", "-"]


@pytest.mark.parametrize("description", ["failed: mail (install)", "failed: web-x", "failed: mail; web (x)"])
def test_a_failure_that_names_another_app_is_not_carried(scene, description):
    assert scene(description, state="failure")[0][3:] == ["-", "-"]


def test_without_a_read_token_every_commit_runs_in_full(scene):
    fields, sha, base = scene("passed 1 app(s): web", token="")
    assert fields == [sha, base, "web", "-", "-"]
