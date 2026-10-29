"""app-main.yml and the app-ci gate on a tag: the steps run as bash over a scratch
repository with a stub `gh`, and the workflow files are checked for their shape."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RUN = "https://github.com/o/r/actions/runs/5"

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the step runs the tools with uv")


def _doc(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(name, job, prefix):
    steps = _doc(name)["jobs"][job]["steps"]
    return next(s for s in steps if s.get("name", "").startswith(prefix))


@pytest.fixture
def scratch(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    repo, bin_dir, runner = tmp_path / "repo", tmp_path / "bin", tmp_path / "runner"
    for d in (repo, bin_dir, runner):
        d.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def commit(*files):
        for name in files:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.read_text() + "x" if path.exists() else "x")
        git("add", "-A")
        git("commit", "-qm", ",".join(files) or "empty", *([] if files else ["--allow-empty"]))
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "main")
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/bash\n'
                  'case "$*" in\n'
                  '  *-X\\ POST*) printf "%s\\n" "$*" >> "$RUNNER_TEMP/posted.txt" ;;\n'
                  '  */jobs*) printf "%b" "$JOBS" ;;\n'
                  '  */statuses*) printf "%b\\n" "$STATUS" ;;\n'
                  'esac\n')
    gh.chmod(0o755)

    def run(script, **extra):
        out = runner / "output.txt"
        out.write_text("")
        step_env = {**env, "PATH": f"{bin_dir}:{env['PATH']}", "RUNNER_TEMP": str(runner),
                    "GITHUB_OUTPUT": str(out), "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "5",
                    "TEMPLATES_ROOT": str(repo), "TOOLS": str(ROOT), "REPO": "o/r", "RUN_URL": RUN,
                    "GH_TOKEN": "x", **extra}
        done = subprocess.run(["bash", "-c", script], cwd=repo, env=step_env, capture_output=True,
                              text=True, check=False)
        values = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        return done, values

    return run, git, commit, runner


# list

def _list(scratch, before, sha):
    run, *_ = scratch
    return run(_step("app-main.yml", "list", "The commits")["run"], BEFORE=before, SHA=sha)


def test_list_names_the_first_parent_commits_oldest_first_with_their_parents(scratch):
    _, git, commit, _ = scratch
    root = commit("a.txt")
    one = commit("b.txt")
    git("checkout", "-q", "-b", "side")
    commit("side.txt")
    git("checkout", "-q", "main")
    two = commit("c.txt")
    git("merge", "--no-ff", "-qm", "merge", "side")
    head = git("rev-parse", "HEAD")
    done, out = _list(scratch, root, head)
    assert done.returncode == 0, done.stderr
    assert out["shas"].split() == [one, two, head]
    assert out["commits"] == (f'[{{"sha":"{one}","parent":"{root}"}},{{"sha":"{two}","parent":"{one}"}},'
                              f'{{"sha":"{head}","parent":"{two}"}}]')


@pytest.mark.parametrize("before", ["0" * 40, "f" * 40])
def test_list_takes_the_head_alone_when_the_before_is_zero_or_unknown(scratch, before):
    _, _, commit, _ = scratch
    parent = commit("a.txt")
    head = commit("b.txt")
    done, out = _list(scratch, before, head)
    assert done.returncode == 0, done.stderr
    assert out["shas"] == head
    assert out["commits"] == f'[{{"sha":"{head}","parent":"{parent}"}}]'


def test_list_is_empty_when_the_push_adds_no_commit(scratch):
    _, _, commit, _ = scratch
    commit("a.txt")
    head = commit("b.txt")
    done, out = _list(scratch, head, head)
    assert done.returncode == 0, done.stderr
    assert out["commits"] == "[]"


def test_list_fetches_a_before_a_force_push_left_unreachable(scratch, tmp_path):
    run, git, commit, _ = scratch
    root = commit("a.txt")
    origin = tmp_path / "origin"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "repo"), str(origin)], check=True, capture_output=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}
    (origin / "old.txt").write_text("x")
    for args in (("config", "uploadpack.allowAnySHA1InWant", "true"), ("add", "-A"), ("commit", "-qm", "old")):
        subprocess.run(["git", "-C", str(origin), *args], check=True, capture_output=True, env=env)
    old = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    git("remote", "add", "origin", str(origin))
    one = commit("b.txt")
    two = commit("c.txt")
    done, out = _list(scratch, old, two)
    assert done.returncode == 0, done.stderr
    assert out["shas"].split() == [one, two]
    assert out["commits"].startswith(f'[{{"sha":"{one}","parent":"{root}"}}')


def test_list_fails_above_256_commits(scratch):
    _, git, commit, _ = scratch
    root = commit("a.txt")
    for _ in range(257):
        commit()
    done, out = _list(scratch, root, git("rev-parse", "HEAD"))
    assert done.returncode == 1
    assert "at most 256" in done.stdout
    assert "commits" not in out


# verdict

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _jobs(*rows):
    return "".join(f"{name}\\t{conclusion}\\n" for name, conclusion in rows)


def _verdict(scratch, jobs, shas=(SHA_A, SHA_B, SHA_C)):
    run, _, _, runner = scratch
    done, out = run(_step("app-main.yml", "verdict", "Post app-ci")["run"], JOBS=jobs, SHAS=" ".join(shas))
    posted = (runner / "posted.txt").read_text().splitlines() if (runner / "posted.txt").exists() else []
    return done, out, posted


def test_verdict_posts_app_ci_on_each_commit_and_lists_the_passing_ones_in_order(scratch):
    jobs = _jobs(
        (f"app-ci {SHA_A} / scope", "success"), (f"app-ci {SHA_A} / app-checks", "success"),
        (f"app-ci {SHA_A} / make and app checker", "success"),
        (f"app-ci {SHA_B} / scope", "success"), (f"app-ci {SHA_B} / app-checks", "failure"),
        (f"app-ci {SHA_C} / scope", "success"), (f"app-ci {SHA_C} / app-checks", "success"),
    )
    done, out, posted = _verdict(scratch, jobs)
    assert done.returncode == 0, done.stderr
    assert out["passed"] == f"{SHA_A} {SHA_C}"
    assert len(posted) == 3
    assert f"repos/o/r/statuses/{SHA_A}" in posted[0]
    assert "state=success" in posted[0] and "context=app-ci" in posted[0]
    assert "description=app-ci passed" in posted[0] and f"target_url={RUN}" in posted[0]
    assert "state=failure" in posted[1] and "description=app-ci failed: app-checks" in posted[1]
    assert "state=success" in posted[2]


@pytest.mark.parametrize("scope,checks", [
    ("cancelled", "success"), ("success", "cancelled"), ("skipped", "success"), ("", "success"),
])
def test_verdict_fails_a_commit_whose_job_was_cancelled_skipped_or_has_no_conclusion(scratch, scope, checks):
    jobs = _jobs((f"app-ci {SHA_A} / scope", scope), (f"app-ci {SHA_A} / app-checks", checks))
    done, out, posted = _verdict(scratch, jobs, shas=(SHA_A,))
    assert done.returncode == 0, done.stderr
    assert out["passed"] == ""
    assert len(posted) == 1 and "state=failure" in posted[0]


def test_verdict_fails_a_commit_with_a_missing_job_and_names_it(scratch):
    jobs = _jobs((f"app-ci {SHA_A} / app-checks", "success"))
    done, out, posted = _verdict(scratch, jobs, shas=(SHA_A,))
    assert out["passed"] == ""
    assert "description=app-ci failed: scope" in posted[0]


# shape

def _app_main():
    return _doc("app-main.yml")


def test_checks_is_a_matrix_of_local_calls_that_do_not_cancel_each_other():
    checks = _app_main()["jobs"]["checks"]
    assert checks["strategy"]["fail-fast"] is False
    assert checks["uses"] == "./.github/workflows/app-ci.yml"
    assert checks["with"] == {"sha": "${{ matrix.commit.sha }}", "base": "${{ matrix.commit.parent }}"}
    assert checks["permissions"] == {"contents": "read"}
    assert checks["name"] == "app-ci ${{ matrix.commit.sha }}"


def test_dispatch_passes_the_passed_commits_and_explicit_secrets():
    dispatch = _app_main()["jobs"]["dispatch"]
    assert dispatch["uses"] == "./.github/workflows/app-dispatch.yml"
    assert dispatch["with"] == {"commits": "${{ needs.verdict.outputs.passed }}"}
    assert set(dispatch["secrets"]) == {"RENOVATE_APP_CLIENT_ID", "RENOVATE_APP_PRIVATE_KEY"}
    assert dispatch["secrets"] != "inherit"
    assert dispatch["permissions"] == {"contents": "read"}


def test_app_main_runs_on_a_push_to_main_with_least_permissions_and_no_concurrency_group():
    doc = _app_main()
    assert doc[True] == {"push": {"branches": ["main"]}}
    assert doc["permissions"] == {"contents": "read"}
    assert "concurrency" not in doc
    assert doc["jobs"]["verdict"]["permissions"] == {"actions": "read", "statuses": "write"}


@pytest.mark.parametrize("name", ["app-main.yml", "app-ci.yml", "app-dispatch.yml", "app-tag.yml"])
def test_no_expression_reaches_a_run_script_and_no_secrets_are_inherited(name):
    text = (WORKFLOWS / name).read_text()
    for job in _doc(name)["jobs"].values():
        assert job.get("secrets") != "inherit"
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), step.get("name")
    assert "secrets: inherit" not in text
    assert "origin/main" not in text


def test_app_ci_groups_runs_by_the_checked_commit_and_checks_it_out():
    doc = _doc("app-ci.yml")
    assert "inputs.sha || github.ref" in doc["concurrency"]["group"]
    assert doc[True]["workflow_call"]["inputs"]["sha"]["default"] == ""
    assert doc[True]["workflow_call"]["inputs"]["base"]["default"] == ""
    for job in ("repo", "scope", "plan", "app"):
        checkout = doc["jobs"][job]["steps"][0]
        assert checkout["with"]["ref"] == "${{ inputs.sha }}", job
    for job, step in (("scope", "The change"), ("plan", "Apps the change"), ("app", "scanctl on the app")):
        assert _step("app-ci.yml", job, step)["env"]["BASE_SHA"] == (
            "${{ inputs.base || github.event.pull_request.base.sha }}")
    for job in ("repo",):
        for step in doc["jobs"][job]["steps"]:
            if "new-failures-only" in step.get("uses", ""):
                assert step["with"]["base"] == "${{ inputs.base }}"


def test_app_dispatch_leaves_main_to_app_main_and_takes_the_commits():
    doc = _doc("app-dispatch.yml")
    assert doc[True]["push"] == {"branches-ignore": ["main"]}
    assert doc[True]["workflow_call"]["inputs"]["commits"]["default"] == ""
    assert _step("app-dispatch.yml", "dispatch", "The apps the push")["env"]["COMMITS"] == "${{ inputs.commits }}"


# the image scan

def _scan(scratch, base):
    run, _, _, runner = scratch
    gopath = runner.parent / "go"
    (gopath / "bin").mkdir(parents=True, exist_ok=True)
    scanctl = gopath / "bin" / "scanctl"
    scanctl.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$RUNNER_TEMP/args.txt"\n'
                       'printf "%s" "${GITHUB_ACTIONS-unset}" > "$RUNNER_TEMP/actions.txt"\n')
    scanctl.chmod(0o755)
    go = runner.parent / "bin" / "go"
    go.write_text('#!/bin/bash\n[ "$1 $2" = "env GOPATH" ] && echo "' + str(gopath) + '"\nexit 0\n')
    go.chmod(0o755)
    step = _step("app-ci.yml", "app", "scanctl on the app")
    done, _ = run(step["run"], APP="web", BASE_SHA=base, GITHUB_ACTIONS="true")
    assert done.returncode == 0, done.stderr
    return ((runner / "args.txt").read_text(), (runner / "actions.txt").read_text(),
            (runner / "scanctl-app.yml").read_text())


def test_the_image_scan_is_judged_from_the_changes_base_over_the_apps_compose(scratch):
    args, actions, config = _scan(scratch, "b" * 40)
    assert f"-baseline-ref {'b' * 40}" in args
    assert "-config" in args and "-summary" in args
    assert "file: blueprints/web/docker-compose.yml" in config
    assert actions == "true"


# own images in the provenance step

def _provenance(scratch, folders, templates_root, caller=""):
    run, _, _, runner = scratch
    (runner.parent / "repo" / "images").mkdir(exist_ok=True)
    for folder in folders:
        (runner.parent / "repo" / "images" / folder).mkdir(exist_ok=True)
    (runner.parent / "repo" / "images" / "README.md").write_text("x")
    uv = runner.parent / "bin" / "uv"
    uv.write_text('#!/bin/bash\nprintf "%s" "$PROVENANCE_OWN_IMAGES" > "$RUNNER_TEMP/own.txt"\n')
    uv.chmod(0o755)
    step = _step("app-ci.yml", "app", "Provenance")
    done, _ = run(step["run"], APP="web", OWNER="CatenaHQ", CALLER_OWN_IMAGES=caller,
                  TEMPLATES_ROOT=templates_root)
    assert done.returncode == 0, done.stderr
    return (runner / "own.txt").read_text()


def test_the_catalog_lists_an_own_image_for_each_folder_of_images(scratch):
    assert _provenance(scratch, ["erpnext-helpdesk", "other"], "") == (
        "ghcr.io/catenahq/erpnext-helpdesk,ghcr.io/catenahq/other")


def test_the_callers_own_images_join_the_catalogs(scratch):
    assert _provenance(scratch, ["erpnext-helpdesk"], "", caller="ghcr.io/x/y") == (
        "ghcr.io/x/y,ghcr.io/catenahq/erpnext-helpdesk")


def test_another_repository_keeps_only_its_own_images_list(scratch, tmp_path):
    assert _provenance(scratch, ["erpnext-helpdesk"], str(tmp_path), caller="ghcr.io/x/y") == "ghcr.io/x/y"
    assert _provenance(scratch, [], str(tmp_path)) == ""


def test_a_catalog_without_images_adds_nothing(scratch):
    assert _provenance(scratch, [], "") == ""


# scope of a merge commit

def _merge_scene(scratch):
    """main holds `root`; a side branch changes two apps; main merges it. Returns (merge, first parent)."""
    _, git, commit, _ = scratch
    commit("README.md")
    git("checkout", "-q", "-b", "side")
    commit("blueprints/x/docker-compose.yml")
    commit("blueprints/z/docker-compose.yml")
    git("checkout", "-q", "main")
    first = commit("notes.txt")
    git("merge", "--no-ff", "-qm", "merge side", "side")
    return git("rev-parse", "HEAD"), first


def _scope(scratch, base, checked):
    run, *_ = scratch
    return run(_step("app-ci.yml", "scope", "The change")["run"], BASE_SHA=base, CHECKED_SHA=checked)[0]


@needs_uv
def test_a_merge_commit_passes_scope_on_the_push_path_with_a_log_line(scratch):
    merge, first = _merge_scene(scratch)
    done = _scope(scratch, first, merge)
    assert done.returncode == 0, done.stderr
    assert "its sides are checked on their own" in done.stdout


@needs_uv
def test_the_pull_request_path_judges_a_merge_commit_as_before(scratch):
    merge, first = _merge_scene(scratch)
    done = _scope(scratch, first, "")
    assert done.returncode == 1
    assert "touches 2 apps" in done.stderr


@needs_uv
def test_a_single_parent_commit_of_two_apps_still_fails_scope_on_the_push_path(scratch):
    _, git, commit, _ = scratch
    commit("README.md")
    base = git("rev-parse", "HEAD")
    head = commit("blueprints/x/docker-compose.yml", "blueprints/z/docker-compose.yml")
    assert _scope(scratch, base, head).returncode == 1


# the gate on a tag

GATE = _step("app-tag.yml", "tag", "Hold back")


def _gate(scratch, plan, status, sha=None):
    run, git, commit, runner = scratch
    (runner / "plan.txt").write_text(plan)
    done, _ = run(GATE["run"], SHA=sha or git("rev-parse", "HEAD"), STATUS=status)
    assert done.returncode == 0, done.stderr
    return done, (runner / "plan.txt").read_text()


def _app_commit(scratch):
    _, git, commit, _ = scratch
    commit("README.md")
    last = commit("blueprints/web/docker-compose.yml", "sources/web.json")
    commit("notes.txt")
    return last


def _status(state="success", creator="github-actions[bot]", url=RUN):
    return f"{state}\\t{creator}\\t{url}"


@needs_uv
@pytest.mark.parametrize("action", ["create web/1.0-1", "mark web/1.0-1"])
def test_an_app_whose_last_change_passed_app_ci_keeps_its_line(scratch, action):
    _app_commit(scratch)
    done, plan = _gate(scratch, f"web {action}\n", _status())
    assert plan == f"web {action}\n"
    assert "::warning::" not in done.stdout


@needs_uv
@pytest.mark.parametrize("status", [
    _status(state="failure"),
    _status(state="pending"),
    "",
    _status(creator="someone"),
    _status(creator="catenahq-renovate[bot]"),
    _status(url="https://github.com/other/r/actions/runs/5"),
    _status(url="https://example.com/o/r/actions/runs/5"),
    _status(url="https://github.com/o/r/actions/runs/5/x"),
    _status(url=""),
])
def test_an_app_whose_last_change_has_no_app_ci_success_is_dropped_with_a_warning(scratch, status):
    last = _app_commit(scratch)
    done, plan = _gate(scratch, "web create web/1.0-1\n", status)
    assert plan == ""
    assert f"::warning::web: {last}" in done.stdout


@needs_uv
def test_an_app_with_no_commit_is_dropped(scratch):
    _app_commit(scratch)
    done, plan = _gate(scratch, "ghost create ghost/1.0-1\n", _status())
    assert plan == ""
    assert "::warning::ghost: no commit" in done.stdout


@needs_uv
def test_a_skip_line_passes_through_and_only_the_held_line_is_dropped(scratch):
    _app_commit(scratch)
    plan_in = "web skip web/1.0-1\nghost create ghost/1.0-1\n"
    _, plan = _gate(scratch, plan_in, _status())
    assert plan == "web skip web/1.0-1\n"


def test_the_gate_is_skipped_for_another_repository_and_reads_statuses_with_the_mint():
    assert GATE["if"] == "format('{0}', inputs.catalog) != 'false'"
    mint = _step("app-tag.yml", "tag", "Mint a GitHub App token")
    assert mint["with"]["permission-statuses"] == "read"
    names = [s.get("name", "") for s in _doc("app-tag.yml")["jobs"]["tag"]["steps"]]
    assert names.index(GATE["name"]) == names.index("Write the tags") - 1
