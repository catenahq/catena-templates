""".clamav-allow.sfp loads as a ClamAV database: one that fails to load fails the
ClamAV pass of every app."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW = ROOT / ".clamav-allow.sfp"
ENTRY = re.compile(r"[0-9a-f]{64}:[1-9][0-9]*:[^:\s]+")


def test_every_line_is_a_comment_or_a_sha256_entry():
    lines = ALLOW.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "the file ends with a newline"
    for number, line in enumerate(lines[:-1], 1):
        assert line.startswith("#") or ENTRY.fullmatch(line), f"line {number}: {line!r}"
