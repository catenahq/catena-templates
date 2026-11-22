"""ci/run runs each check .github/checks.yml names and refuses any other name, and the
python check reads the contracts checkout beside this one without a token."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKS = yaml.safe_load((ROOT / ".github" / "checks.yml").read_text())["checks"]
CASE = re.compile(r"^  ([a-z][a-z0-9-]*)\)$", re.MULTILINE)


def test_ci_run_has_a_case_for_each_check_and_no_other():
    assert set(CASE.findall((ROOT / "ci" / "run").read_text())) == set(CHECKS)


def test_an_unknown_check_exits_2_naming_the_checks():
    done = subprocess.run(["bash", str(ROOT / "ci" / "run"), "nope"], capture_output=True, text=True,
                          check=False)
    assert done.returncode == 2
    assert "unknown check 'nope'" in done.stderr


def test_the_python_check_reads_contracts_without_a_token():
    siblings = CHECKS["python"]["siblings"]
    assert list(siblings) == ["contracts"]
    assert "token" not in (siblings["contracts"] or {})
