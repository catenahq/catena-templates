"""The plain command of build/app_check.py, and the token the provenance helper reads with."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import app_checks  # noqa: E402


@pytest.mark.parametrize("bench,answer", [({"pack": "embedded", "plain": True}, "true"),
                                          ({"pack": "embedded", "plain": False}, "false"),
                                          ({"pack": "embedded"}, "false")])
def test_the_plain_command_prints_whether_the_card_opts_in(tmp_path, monkeypatch, capsys, bench, answer):
    (tmp_path / "blueprints" / "web").mkdir(parents=True)
    (tmp_path / "blueprints" / "web" / "app.json").write_text(json.dumps({"bench": bench}))
    monkeypatch.setattr(app_checks, "ROOT", tmp_path)
    assert app_checks.main(["plain", "web"]) == 0
    assert capsys.readouterr().out.strip() == answer


def test_the_schema_admits_bench_plain_as_a_boolean():
    bench = json.loads((ROOT / "app.schema.json").read_text())["properties"]["bench"]["properties"]
    assert bench["plain"]["type"] == "boolean"


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
