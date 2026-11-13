"""The tools run in the one uv project at the repository root: no script carries its own
dependency block, the Makefile adds no package on the command line, and a package a workflow
adds with `uv run --with` is one the project already declares."""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SCRIPT_BLOCK = re.compile(r"^# /// script$", re.MULTILINE)
UV_RUN = re.compile(r"\buv run\b[^\n]*")
WITH_PACKAGE = re.compile(r"--with[ =]+(\S+)")


def _canonical(requirement: str) -> str:
    return re.sub(r"[-_.]+", "-", NAME.match(requirement).group(0)).lower()


def _declared() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = data["project"]["dependencies"] + [
        r for group in data.get("dependency-groups", {}).values() for r in group]
    return {_canonical(r) for r in requirements}


def _tracked_python() -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True,
                         check=False)
    if out.returncode:
        pytest.skip("not a git checkout")
    return out.stdout.split()


def test_no_script_carries_its_own_dependency_block():
    holders = [p for p in _tracked_python() if SCRIPT_BLOCK.search((ROOT / p).read_text())]
    assert holders == []


def test_the_makefile_adds_no_package_on_the_command_line():
    assert "uv run --with" not in (ROOT / "Makefile").read_text()


def test_every_package_a_workflow_adds_is_a_project_dependency():
    declared = _declared()
    added = {_canonical(m.group(1))
             for wf in sorted(WORKFLOWS.glob("*.yml"))
             for line in UV_RUN.findall(wf.read_text())
             for m in WITH_PACKAGE.finditer(line)}
    assert added - declared == set()
