"""The same-tree gate in app-ci.yml and build-and-verify.yml: every job waits for it and carries its
condition, the gate jobs are named as the rulesets require them, and every caller of a reusable
workflow that holds a gate grants the permission the gate reads with."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
GATED = {"app-ci.yml": "same tree (app-ci)", "build-and-verify.yml": "same tree (build-and-verify)"}
CONDITION = "needs.same-tree.outputs.run != 'false'"
# Reusable workflows whose gate job asks for `actions: read`.
CALLEES = ("app-ci.yml", "build-and-verify.yml", "scanctl/.github/workflows/github-reusable.yml")


def _jobs(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _needs(job):
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _reaches_gate(jobs, name, seen=()):
    if name in seen:
        return False
    needs = _needs(jobs[name])
    return "same-tree" in needs or any(_reaches_gate(jobs, n, (*seen, name)) for n in needs)


@pytest.mark.parametrize("workflow", GATED)
def test_every_job_waits_for_the_gate(workflow):
    jobs = _jobs(workflow)
    assert "same-tree" in jobs
    for name, job in jobs.items():
        if name != "same-tree":
            assert _reaches_gate(jobs, name), f"{workflow}: {name} does not wait for same-tree"


@pytest.mark.parametrize("workflow", GATED)
def test_every_job_that_needs_the_gate_carries_its_condition(workflow):
    for name, job in _jobs(workflow).items():
        if "same-tree" in _needs(job):
            assert CONDITION in str(job.get("if", "")), f"{workflow}: {name} lacks the condition"


@pytest.mark.parametrize("workflow", GATED)
def test_gate_job_is_named_and_reads_runs(workflow):
    gate = _jobs(workflow)["same-tree"]
    assert gate["name"] == GATED[workflow]
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    assert gate["outputs"] == {"run": "${{ steps.tree.outputs.run }}"}
    (step,) = gate["steps"]
    assert step["uses"] == "catenahq/contracts/.github/actions/same-tree@main"


def test_gate_names_are_unique_among_the_workflows():
    names = [job["name"] for f in WORKFLOWS.glob("*.yml") for job in _jobs(f.name).values()
             if str(job.get("name", "")).startswith("same tree")]
    assert len(names) == len(set(names))


def test_build_and_verify_duplication_keeps_its_catalog_condition():
    condition = _jobs("build-and-verify.yml")["duplication"]["if"]
    assert CONDITION in condition
    assert "inputs.catalog" in condition


def test_app_ci_gate_judges_the_change_and_yields_to_a_call_with_a_sha():
    gate = _jobs("app-ci.yml")["same-tree"]
    assert gate["if"] == "inputs.sha == ''"
    assert gate["steps"][0]["with"]["judge-change"] is True


def test_app_ci_jobs_still_run_when_the_gate_is_skipped():
    jobs = _jobs("app-ci.yml")
    for name in ("repo", "scope", "plan"):
        condition = jobs[name]["if"]
        assert "!cancelled()" in condition
        assert "needs.same-tree.result != 'failure'" in condition
    assert _needs(jobs["app"]) == ["plan"]


def test_every_caller_of_a_gated_workflow_grants_actions_read():
    found = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for name, job in doc["jobs"].items():
            uses = str(job.get("uses", ""))
            if not any(uses.endswith(callee) for callee in CALLEES):
                continue
            found += 1
            granted = job.get("permissions", doc.get("permissions", {}))
            assert granted.get("actions") == "read", f"{path.name}: {name} lacks actions: read"
    assert found
