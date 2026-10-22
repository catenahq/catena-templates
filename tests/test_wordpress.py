"""WordPress's own declarations: the configure hook runs against a stand-in for
php (wp-cli and the download of its phar) and sleep that keeps the state of a
site in a file, and the declarations the hook relies on."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

BLUEPRINT = ROOT / "blueprints" / "wordpress"

RELAY = {"SMTP_RELAY_HOST": "smtp.example.test", "SMTP_RELAY_PORT": "587", "SMTP_RELAY_USER": "mailer",
         "SMTP_RELAY_TLS": "tls", "SMTP_RELAY_SENDER": "site@example.test"}

# php as the container has it: `php -r` copies the phar, and the phar itself is
# wp-cli, answering over one state file.
FAKE = r'''#!{python}
import json, os, sys

path = os.environ["FAKE_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]


def save():
    json.dump(state, open(path, "w"))


if argv[:1] == ["-r"]:
    state["calls"].append("download")
    save()
    if not state["download_ok"]:
        sys.exit(1)
    target = argv[1].split('"')[3]
    open(target, "w").write("phar")
    sys.exit(0)

# php -d memory_limit=... <phar> --path=<dir> <command...>
args = [a for a in argv[3:] if not a.startswith("--path=")]
line = " ".join(args)
state["calls"].append(line)
save()
plugins = state["plugins"]
if line == "core is-installed":
    sys.exit(0 if state["installed"] else 1)
if args[:2] == ["plugin", "is-installed"]:
    sys.exit(0 if args[2] in plugins else 1)
if args[:2] == ["plugin", "is-active"]:
    sys.exit(0 if plugins.get(args[2]) else 1)
if args[:2] == ["plugin", "install"]:
    if args[2] in state["store"]:
        plugins[args[2]] = "--activate" in args
        save()
        sys.exit(0)
    sys.exit(1)
if line == "redis status":
    print("Status: Connected" if state["redis"] else "Status: Not enabled")
    sys.exit(0)
if line == "redis enable":
    state["redis"] = True
    save()
    sys.exit(0)
if args[0] == "eval-file":
    state["seed"] = open(args[1]).read()
    state["seed_env"] = {k: v for k, v in os.environ.items() if k.startswith("SMTP_RELAY_")}
    save()
    sys.exit(0 if not state.get("eval_fails") else 1)
print("unknown command " + line, file=sys.stderr)
sys.exit(1)
'''

SLUGS = ["kadence-blocks", "performance-lab", "auto-sizes", "dominant-color-images", "embed-optimizer",
         "image-prioritizer", "optimization-detective", "speculation-rules", "webp-uploads",
         "fastcgi-cache-purge-and-preload-nginx", "redis-cache", "wp-mail-smtp", "wp-mail-logging",
         "complianz-gdpr", "wp-consent-api", "seo-by-rank-math", "fluentform"]


def _entry() -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == "wordpress")


def _compose() -> dict:
    return yaml.safe_load((BLUEPRINT / "docker-compose.yml").read_text(encoding="utf-8"))


def _body() -> str:
    [argv] = _entry().block["hooks"]["configure"]
    assert argv[:2] == ["sh", "-c"]
    return argv[2].replace("$$", "$")


def test_the_hook_runs_in_the_wp_service_as_www_data():
    hooks = _entry().block["hooks"]
    assert hooks["service"] in _compose()["services"]
    assert hooks["user"] == "www-data"
    assert set(hooks) - {"service", "user", "timeout_seconds"} == {"configure"}
    assert quiesce_lint.lint_hooks(_entry()) == []
    assert quiesce_lint.lint_block(_entry()) == []


def test_the_body_parses_in_sh():
    subprocess.run(["sh", "-n", "-c", _body()], check=True)


def test_the_relay_comes_from_settings_through_managed_values():
    env = _entry().block["env"]
    for key in RELAY:
        assert "managed" in env[key]
    assert "smtp_relay_host" in env["SMTP_RELAY_HOST"]["managed"]["expr"]


def test_the_curated_set_is_the_one_the_body_installs():
    body = _body()
    for slug in SLUGS:
        assert slug in body


class Site:
    """A WordPress container the hook runs in: a tree under tmp_path, the
    stand-in tools, and the state file they share."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "html"
        self.root.mkdir()
        (self.root / "wp-load.php").write_text("<?php\n")
        self.tmp = tmp_path / "tmp"
        self.tmp.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "php").write_text(FAKE.replace("{python}", sys.executable))
        (self.bin / "php").chmod(0o755)
        (self.bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "sleep").chmod(0o755)
        self.state_path = tmp_path / "state.json"
        self.state = {"calls": [], "installed": True, "plugins": {}, "store": list(SLUGS), "redis": False,
                      "download_ok": True}

    def now(self) -> dict:
        return json.loads(self.state_path.read_text())

    def run(self, **env) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps(self.state))
        body = (_body().replace("/tmp/", f"{self.tmp}/").replace("/var/www/html", str(self.root)))
        environ = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_STATE": str(self.state_path), **env}
        run = subprocess.run(["sh", "-c", body], env=environ, capture_output=True, text=True, timeout=120)
        self.state = self.now()
        self.calls = self.state["calls"]
        self.state["calls"] = []
        return run


@pytest.fixture
def wp(tmp_path):
    return Site(tmp_path)


def test_a_first_configure_installs_and_activates_every_plugin(wp):
    run = wp.run(**RELAY)
    assert run.returncode == 0, run.stderr
    assert wp.state["plugins"] == {slug: True for slug in SLUGS}
    assert wp.state["redis"] is True
    assert wp.state["seed_env"] == RELAY
    seed = wp.state["seed"]
    assert "nginx_cache_settings" in seed and "wp_mail_smtp" in seed
    assert not (wp.tmp / "catena-seed.php").exists()


def test_the_seeds_are_guarded_by_marker_options(wp):
    wp.run()
    seed = wp.state["seed"]
    assert 'get_option("catena_npp_seeded") === false' in seed
    assert 'get_option("catena_mail_seeded") === false' in seed
    # Keys the site holds win over the seeded values.
    assert "array_merge($desired, $current)" in seed
    assert "$mail + array(" in seed


def test_a_site_without_the_wizard_run_is_left_alone(wp):
    wp.state["installed"] = False
    run = wp.run(**RELAY)
    assert run.returncode == 0, run.stderr
    assert wp.state["plugins"] == {}
    assert "seed" not in wp.state


def test_a_plugin_an_administrator_deactivated_stays_deactivated(wp):
    wp.state["plugins"] = {slug: True for slug in SLUGS}
    wp.state["plugins"]["wp-mail-logging"] = False
    wp.state["redis"] = True
    run = wp.run()
    assert run.returncode == 0, run.stderr
    assert wp.state["plugins"]["wp-mail-logging"] is False


def test_an_installed_plugin_is_not_installed_again(wp):
    wp.state["plugins"] = {"kadence-blocks": True}
    wp.run()
    assert "plugin install kadence-blocks --activate" not in wp.calls
    assert "plugin install performance-lab --activate" in wp.calls


def test_a_plugin_store_that_does_not_answer_does_not_fail_the_deploy(wp):
    wp.state["store"] = []
    run = wp.run()
    assert run.returncode == 0, run.stderr
    assert "could not be installed" in run.stderr


def test_a_failed_download_of_wp_cli_leaves_the_wiring_for_the_next_deploy(wp):
    wp.state["download_ok"] = False
    run = wp.run()
    assert run.returncode == 0
    assert wp.state["plugins"] == {}


def test_a_failed_settings_write_fails_the_hook(wp):
    wp.state["eval_fails"] = True
    run = wp.run()
    assert run.returncode == 1
    assert not (wp.tmp / "catena-seed.php").exists()
