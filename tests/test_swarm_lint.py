"""Unit tests for lib/swarm_lint.py.

Every case drives the pure functions over a compose body, so the suite runs
without docker on the host. The end-to-end case at the bottom runs the lint
over the real sources/ tree, which is what CI gates on.

The bans below are not a reading of the documentation. Each key was deployed
to a single-node swarm and the resulting service spec inspected; the three in
`test_the_silent_bans_are_the_ones_that_matter` are the ones the deploy does
not even warn about.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import swarm_lint as L  # noqa: E402

MINIMAL = """\
services:
  app:
    image: nginx:alpine
    deploy:
      restart_policy:
        condition: any
"""


def _body(extra: str = "", *, deploy: str = "") -> str:
    block = (
        "services:\n"
        "  app:\n"
        "    image: nginx:alpine\n"
        "    deploy:\n"
        "      restart_policy:\n"
        "        condition: any\n"
    )
    return block + deploy + extra


def test_a_minimal_swarm_service_is_clean():
    assert L.lint_compose(MINIMAL, label="x") == []


def test_the_silent_bans_are_the_ones_that_matter():
    """depends_on, the short tmpfs key and oom_score_adj are accepted by the
    swarm loader, dropped by the deploy, and reported by nothing. A file
    carrying one reads as if the declaration were honoured."""
    for key in ("depends_on", "tmpfs", "oom_score_adj"):
        assert key in L.DROPPED, f"{key} is the dangerous kind and must be banned"


def test_depends_on_is_rejected():
    errs = L.lint_compose(_body("    depends_on:\n      - db\n"), label="x")
    assert any("depends_on" in e and "SILENCE" in e for e in errs)


def test_restart_is_rejected_in_favour_of_the_deploy_policy():
    errs = L.lint_compose(
        "services:\n  app:\n    image: nginx:alpine\n    restart: always\n",
        label="x",
    )
    assert any("restart does not reach" in e for e in errs)


def test_a_service_must_say_what_happens_when_it_dies():
    errs = L.lint_compose("services:\n  app:\n    image: nginx:alpine\n", label="x")
    assert any("restart_policy.condition" in e for e in errs)


def test_mem_limit_is_reported_as_a_refusal_not_a_drop():
    """The distinction is the whole point: a refused key fails the deploy,
    a dropped key does not, and the two need different fixes."""
    errs = L.lint_compose(_body("    mem_limit: 2g\n"), label="x")
    assert any("refuses the whole file" in e for e in errs)


def test_a_named_volume_needs_the_data_node_constraint():
    errs = L.lint_compose(
        _body("    volumes:\n      - data:/var/lib/x\n"), label="x")
    assert any(L.DATA_NODE_CONSTRAINT in e for e in errs)


def test_a_host_path_needs_it_too():
    errs = L.lint_compose(
        _body("    volumes:\n      - /srv/x:/var/lib/x\n"), label="x")
    assert any(L.DATA_NODE_CONSTRAINT in e for e in errs)


def test_a_tmpfs_mount_does_not():
    """A tmpfs lives and dies with the task, so no node holds anything for
    it and pinning it would constrain placement for nothing."""
    errs = L.lint_compose(
        _body("    volumes:\n      - type: tmpfs\n        target: /scratch\n"),
        label="x",
    )
    assert errs == []


def test_the_constraint_satisfies_the_rule():
    errs = L.lint_compose(
        _body(
            "    volumes:\n      - data:/var/lib/x\n",
            deploy=(
                "      placement:\n"
                "        constraints:\n"
                f"          - {L.DATA_NODE_CONSTRAINT}\n"
            ),
        ),
        label="x",
    )
    assert errs == []


def test_a_published_port_must_be_host_mode():
    errs = L.lint_compose(_body('    ports:\n      - "25:25"\n'), label="x")
    assert any("ingress mesh" in e for e in errs)

    errs = L.lint_compose(
        _body("    ports:\n      - target: 25\n        published: 25\n"
              "        protocol: tcp\n        mode: host\n"),
        label="x",
    )
    assert errs == []


# ── inline entrypoint scripts ────────────────────────────────────────


def test_a_broken_inline_python_entrypoint_is_caught():
    errs = L.lint_entrypoint(["python3", "-c", "def f(:\n  pass\n"], label="x")
    assert any("does not parse as Python" in e for e in errs)


def test_a_broken_inline_shell_entrypoint_is_caught():
    errs = L.lint_entrypoint(
        ["/bin/sh", "-ec", "if true; then\n  echo hi\n"], label="x")
    assert any("does not parse as sh" in e for e in errs)


def test_a_sound_script_passes():
    assert L.lint_entrypoint(["/bin/sh", "-ec", "echo ok\n"], label="x") == []
    assert L.lint_entrypoint(["python3", "-c", "print(1)\n"], label="x") == []


def test_an_argv_that_is_not_an_inline_script_is_left_alone():
    assert L.lint_entrypoint(["/cron.sh"], label="x") == []
    assert L.lint_entrypoint("nginx -g 'daemon off;'", label="x") == []
    assert L.lint_entrypoint(None, label="x") == []


def test_the_compose_escape_survives_the_placeholder_pass():
    """`$${VAR}` asks for the literal text `${VAR}` to reach the shell. A
    placeholder pass that ran first would substitute it, turning a variable
    the script expands at runtime into a value baked at deploy time -- and
    the check would then be reading a script nobody runs."""
    got = L.as_compose_writes_it("a ${FOO} b $${LITERAL} c ${BAR:-x} d $$((n + 1))")
    assert got == "a PLACEHOLDER b ${LITERAL} c PLACEHOLDER d $((n + 1))"


def test_a_placeholder_is_a_word_not_an_empty_string():
    """An empty substitution would turn `[${WHITELIST}]` into valid YAML by
    accident, and a script that only parses because a value was blank is not
    the script that runs on a configured host."""
    assert "PLACEHOLDER" in L.as_compose_writes_it("[${WHITELIST}]")


# ── end to end over the real catalog ─────────────────────────────────


def test_the_catalog_passes():
    assert L.lint_all() == 0
