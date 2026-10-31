"""app-ci's scope step and the helper that reads an app-ci status: the step runs as bash
over a scratch repository and a local GitHub API, and the workflow is checked for the
steps it skips."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import app_checks  # noqa: E402
from test_app_main import _statuses_api  # noqa: E402

RUN = "https://github.com/o/r/actions/runs/5"
HEAVY = ["Pull the app's images", "Provenance", "scanctl on the app's images", "ClamAV over each image's files"]

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the step runs the tools with uv")


def _steps():
    return yaml.safe_load((ROOT / ".github/workflows/app-ci.yml").read_text())["jobs"]["app"]["steps"]


def _status(state="success", creator="github-actions[bot]", url=RUN, context="app-ci"):
    return json.dumps([{"context": context, "state": state, "creator": {"login": creator}, "target_url": url}])


# the workflow

def test_the_scope_step_comes_first_and_the_heavy_steps_wait_for_it():
    steps = _steps()
    names = [s.get("name", "") for s in steps]
    scope = names.index("Scope of the change")
    assert scope < names.index("Swarm loader reads the stack file")
    heavy = [s for s in steps if any(s.get("name", "").startswith(h) for h in HEAVY) or "setup-go" in s.get("uses", "")]
    assert len(heavy) == 5
    for step in heavy:
        assert "steps.scope.outputs.heavy == 'true'" in step["if"], step
    summary = next(s for s in steps if s.get("name") == "Scan summary")
    assert summary["if"] == "always() && steps.scope.outputs.heavy == 'true'"
    light = [s for s in steps if s.get("name", "").startswith(("Swarm loader", "The released app checker"))]
    assert len(light) == 2 and all("if" not in s for s in light)


def test_the_app_job_reads_statuses():
    job = yaml.safe_load((ROOT / ".github/workflows/app-ci.yml").read_text())["jobs"]["app"]
    assert job["permissions"] == {"contents": "read", "statuses": "read"}


# the step

@pytest.fixture
def scene(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    repo = tmp_path / "repo"
    repo.mkdir()

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
    runtime = commit("README.md", "blueprints/web/docker-compose.yml", "sources/web.json")

    def run(base, body, code=200):
        out, summary = tmp_path / "output.txt", tmp_path / "summary.md"
        out.write_text("")
        summary.write_text("")
        with _statuses_api(body, code) as (api, paths):
            step_env = {**env, "APP": "web", "BASE_SHA": base, "GH_TOKEN": "x", "GITHUB_OUTPUT": str(out),
                        "GITHUB_STEP_SUMMARY": str(summary), "TOOLS": str(ROOT), "TEMPLATES_ROOT": str(repo),
                        "GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": api}
            script = next(s["run"] for s in _steps() if s.get("id") == "scope")
            done = subprocess.run(["bash", "-c", script], cwd=repo, env=step_env, capture_output=True,
                                  text=True, check=False)
        assert done.returncode == 0, done.stderr
        values = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        return values["heavy"], summary.read_text(), paths

    return run, commit, runtime


@needs_uv
@pytest.mark.parametrize("file", ["blueprints/web/README.md", "blueprints/web/test.yml"])
def test_a_change_beside_the_runtime_skips_the_heavy_steps_when_app_ci_passed_at_the_last_runtime_change(scene, file):
    run, commit, runtime = scene
    commit(file)
    heavy, summary, paths = run(runtime, _status())
    assert heavy == "false"
    assert "heavy steps skipped" in summary and runtime in summary
    assert paths == [f"/repos/o/r/commits/{runtime}/statuses?per_page=100"]


@needs_uv
@pytest.mark.parametrize("body,code", [
    (_status(state="failure"), 200),
    (_status(state="pending"), 200),
    (_status(creator="someone"), 200),
    (_status(url="https://github.com/other/r/actions/runs/5"), 200),
    ("[]", 200),
    ("{}", 500),
])
def test_a_missing_failed_or_unreadable_status_runs_the_heavy_steps(scene, body, code):
    run, commit, runtime = scene
    commit("blueprints/web/README.md")
    heavy, summary, _ = run(runtime, body, code)
    assert heavy == "true"
    assert "heavy steps run" in summary


@needs_uv
def test_a_change_of_the_runtime_runs_the_heavy_steps_without_asking_for_a_status(scene):
    run, commit, runtime = scene
    commit("blueprints/web/docker-compose.yml")
    heavy, summary, paths = run(runtime, _status())
    assert heavy == "true"
    assert "touches the runtime" in summary
    assert paths == []


@needs_uv
def test_a_runtime_change_beside_a_presentation_one_runs_the_heavy_steps(scene):
    run, commit, runtime = scene
    commit("blueprints/web/README.md", "blueprints/web/docker-compose.yml")
    assert run(runtime, _status())[0] == "true"


@needs_uv
def test_the_status_is_read_at_the_last_runtime_change_not_at_the_base(scene):
    run, commit, runtime = scene
    commit("blueprints/web/README.md")
    base = commit("blueprints/web/test.yml")
    heavy, _, paths = run(base, _status())
    assert heavy == "false"
    assert paths == [f"/repos/o/r/commits/{runtime}/statuses?per_page=100"]


# the helper

def _fetch(body):
    seen = []

    def fetch(url):
        seen.append(url)
        return body

    fetch.seen = seen
    return fetch


SHA = "a" * 40


def test_ci_passed_reads_the_commit_s_statuses_of_the_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    fetch = _fetch(json.loads(_status()))
    assert app_checks.ci_passed(SHA, "o/r", fetch)
    assert fetch.seen == [f"https://api.github.com/repos/o/r/commits/{SHA}/statuses?per_page=100"]


def test_ci_passed_judges_the_newest_app_ci_status_only():
    newest_first = json.loads(_status(context="app-tests")) + json.loads(_status(state="failure")) \
        + json.loads(_status())
    assert not app_checks.ci_passed(SHA, "o/r", _fetch(newest_first))


@pytest.mark.parametrize("body", [None, [], json.loads(_status(context="app-tests")),
                                  json.loads(_status(state="error")), json.loads(_status(url=RUN + "/x")),
                                  json.loads(_status(creator="catenahq-renovate[bot]"))])
def test_ci_passed_is_false_for_anything_but_a_success_of_a_run_of_the_repository(body):
    assert not app_checks.ci_passed(SHA, "o/r", _fetch(body))


def test_the_command_exits_by_the_answer_and_one_for_an_unreadable_page(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(app_checks, "ci_passed", lambda sha, repo: True)
    assert app_checks.main(["ci-passed", SHA]) == 0
    monkeypatch.setattr(app_checks, "ci_passed", lambda sha, repo: False)
    assert app_checks.main(["ci-passed", SHA]) == 1

    def broken(sha, repo):
        raise app_checks.provenance.ProvenanceError("unreadable")

    monkeypatch.setattr(app_checks, "ci_passed", broken)
    assert app_checks.main(["ci-passed", SHA]) == 1
    assert "unreadable" in capsys.readouterr().err


@pytest.mark.parametrize("sha,repo", [("abc", "o/r"), (SHA, ""), (SHA, "o"), ("A" * 40, "o/r")])
def test_the_command_refuses_a_bad_commit_or_repository(monkeypatch, sha, repo):
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    assert app_checks.main(["ci-passed", sha]) == 1


def test_the_command_reads_a_token_from_gh_token(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"[]"

    def urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        return Response()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "t0k")
    monkeypatch.setattr(app_checks.provenance.urllib.request, "urlopen", urlopen)
    assert app_checks.provenance.http_fetch("https://api.github.com/repos/o/r") == []
    assert seen["auth"] == "Bearer t0k"
