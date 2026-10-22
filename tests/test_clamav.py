"""The ClamAV template's scan hook run against a stand-in for clamdscan."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == "clamav")


def _scan(tmp_path: Path, clamdscan: str, data: str = "file bytes") -> subprocess.CompletedProcess:
    """The scan hook's body, run with a clamdscan that is the script `clamdscan`
    and the file on stdin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "clamdscan"
    stub.write_text("#!/bin/sh\n" + clamdscan, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    [argv] = _entry().block["hooks"]["scan"]
    assert argv[:2] == ["sh", "-c"]
    return subprocess.run(["sh", "-c", argv[2].replace("$$", "$")], input=data, capture_output=True, text=True,
                          check=False, env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})


def test_the_block_declares_a_scan_hook_that_passes_the_lint():
    hooks = _entry().block["hooks"]
    assert hooks["service"] == "clamav"
    assert quiesce_lint.lint_hooks(_entry()) == []
    assert quiesce_lint.lint_antivirus_scan(_entry()) == []


def test_a_clean_file_ends_with_zero(tmp_path):
    out = _scan(tmp_path, "cat >/dev/null; echo 'stdin: OK'\n")
    assert out.returncode == 0 and out.stdout == ""


def test_a_flagged_file_ends_with_one_and_names_the_signature(tmp_path):
    out = _scan(tmp_path, "cat >/dev/null; echo 'stdin: Win.Test.EICAR_HDB-1 FOUND'; exit 1\n")
    assert out.returncode == 1 and out.stdout == "FLAGGED Win.Test.EICAR_HDB-1\n"


def test_the_scan_reads_the_file_from_stdin(tmp_path):
    seen = tmp_path / "seen"
    out = _scan(tmp_path, f"cat > {seen}\nexit 0\n", data="the bytes")
    assert out.returncode == 0 and seen.read_text() == "the bytes"


def test_an_answer_that_is_not_clean_or_found_passes_through_with_its_message(tmp_path):
    out = _scan(tmp_path, "cat >/dev/null; echo 'ERROR: Could not connect to clamd' >&2; exit 2\n")
    assert out.returncode == 2 and "Could not connect to clamd" in out.stdout and "FLAGGED" not in out.stdout


def test_an_exit_of_one_with_no_signature_line_prints_no_flag(tmp_path):
    out = _scan(tmp_path, "cat >/dev/null; echo 'something else'; exit 1\n")
    assert out.returncode == 1 and "FLAGGED" not in out.stdout
