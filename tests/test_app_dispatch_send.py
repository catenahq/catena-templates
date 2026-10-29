"""The send step of app-dispatch.yml, run as bash over a scratch repository and its origin,
with a stub `gh` that records each dispatch."""
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
    return next(s["run"] for s in steps if s.get("name") == "Dispatch app-test to ops")


@pytest.fixture
def scene(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    repo, origin, bin_dir, runner = (tmp_path / n for n in ("repo", "origin.git", "bin", "runner"))
    for d in (repo, bin_dir, runner):
        d.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def commit(*files):
        for name in files:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.read_text() + "x" if path.exists() else "x")
        git("add", "-A")
        git("commit", "-qm", ",".join(files))
        return git("rev-parse", "HEAD")

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=env)
    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", str(origin))
    shas = {"root": commit("README.md")}
    shas["web"] = commit("blueprints/web/docker-compose.yml", "sources/web.json")
    shas["mail"] = commit("blueprints/mail/docker-compose.yml", "sources/mail.json")
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")
    calls = runner / "calls.txt"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/bash\necho "$*" >> "{calls}"\n')
    gh.chmod(0o755)

    def run(lines, *, branch="main"):
        calls.write_text("")
        (runner / "payloads.txt").write_text("".join(f"{line}\n" for line in lines))
        step_env = {**env, "PATH": f"{bin_dir}:{env['PATH']}", "RUNNER_TEMP": str(runner),
                    "TEMPLATES_ROOT": str(repo), "TOOLS": str(ROOT), "GH_TOKEN": "x", "READ_TOKEN": "",
                    "OWNER": "o", "BRANCH": branch, "DEFAULT_BRANCH": "main", "REPO_NAME": "r"}
        done = subprocess.run(["bash", "-c", _step_script()], cwd=repo, env=step_env,
                              capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        sent = [c for c in calls.read_text().splitlines() if "repos/o/ops/dispatches" in c]
        return done, [next(f.split("=", 1)[1] for f in c.split() if f.startswith("client_payload[sha]="))
                      for c in sent]

    return run, shas, git, commit


def test_a_commit_of_main_that_is_its_app_s_last_change_is_sent(scene):
    run, shas, _, _ = scene
    _, sent = run([f"{shas['web']} {shas['root']} web - -", f"{shas['mail']} {shas['web']} mail - -"])
    assert sent == [shas["web"], shas["mail"]]


def test_a_push_that_landed_after_the_checkout_makes_the_older_commit_of_its_app_stale(scene):
    run, shas, git, commit = scene
    again = commit("blueprints/web/docker-compose.yml")
    git("push", "-q", "origin", "main")
    # The checkout saw main before that push; the send reads main again.
    git("update-ref", "refs/remotes/origin/main", shas["mail"])
    done, sent = run([f"{shas['web']} {shas['root']} web - -", f"{shas['mail']} {shas['web']} mail - -"])
    assert sent == [shas["mail"]]
    assert f"{shas['web']}: not sent, web changed again on main" in done.stdout
    assert git("rev-parse", "refs/remotes/origin/main") == again


def test_main_is_judged_as_the_checkout_saw_it_when_it_cannot_be_fetched(scene):
    run, shas, git, commit = scene
    commit("blueprints/web/docker-compose.yml")
    git("remote", "set-url", "origin", "/nonexistent/origin.git")
    done, sent = run([f"{shas['web']} {shas['root']} web - -"])
    assert sent == [shas["web"]]
    assert "could not be fetched again" in done.stdout


def test_a_branch_other_than_main_is_sent_without_reading_main(scene):
    run, shas, git, _ = scene
    git("remote", "set-url", "origin", "/nonexistent/origin.git")
    done, sent = run([f"{shas['web']} {shas['root']} web - -"], branch="feat")
    assert sent == [shas["web"]]
    assert "could not be fetched" not in done.stdout
