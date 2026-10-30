""".clamav-allow.sfp loads as a ClamAV database: one that fails to load fails
every app's ClamAV pass."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ALLOW = ROOT / ".clamav-allow.sfp"
ENTRY = re.compile(r"[0-9a-f]{64}:[1-9][0-9]*:[^:\s]+")


def test_every_line_is_a_comment_or_a_sha256_entry():
    lines = ALLOW.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "the file ends with a newline"
    for number, line in enumerate(lines[:-1], 1):
        assert line.startswith("#") or ENTRY.fullmatch(line), f"line {number}: {line!r}"


def test_app_ci_mounts_it_into_the_scanner_s_database_folder():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "app-ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["app"]["steps"] if s.get("name") == "ClamAV over each image's files")
    assert '"$TOOLS/.clamav-allow.sfp"' in step["run"]
    assert ":/var/lib/clamav/catena-allow.sfp:ro" in step["run"]
