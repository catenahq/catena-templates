"""Per-template facts a host or an action relies on and no generic gate
checks: what a template's backup mode does, the Rocket.Chat settings its
Keycloak sign-in needs, and the user_oidc release the Nextcloud wiring is
written for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model  # noqa: E402


def _entry(slug: str) -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == slug)


def _service(slug: str, service: str) -> dict:
    compose = yaml.safe_load(_entry(slug).compose_path.read_text(encoding="utf-8"))
    return compose["services"][service]


def test_actual_budget_holds_its_server_still_for_the_backup():
    """Actual Budget's server writes its SQLite files with no export the
    backup could take, so the backup copies the files. Its node process stops
    before the backup and continues after it, so the copy holds every file at
    one instant, which SQLite opens the way it opens files after a crash.
    `pkill -CONT` on a running process succeeds, so post succeeds when pre
    never ran. The name matches only while the service runs the image's own
    `node app.js`."""
    assert _entry("actualbudget").quiesce == {
        "service": "actual",
        "pre": [["pkill", "-STOP", "-x", "node"]],
        "post": [["pkill", "-CONT", "-x", "node"]],
        "timeout_seconds": 10,
    }
    actual = _service("actualbudget", "actual")
    assert "entrypoint" not in actual and "command" not in actual


def test_rocket_chat_sends_the_userinfo_token_in_the_header():
    """Keycloak reads a userinfo GET's access token from the Authorization
    header only. The registration value applies when the service is first
    made; the OVERWRITE_SETTING_ form applies on every start, so a server
    whose setting already holds the default moves too."""
    env = _service("rocketchat-oidc", "rocketchat")["environment"]
    assert env["Accounts_OAuth_Custom_Keycloak_identity_token_sent_via"] == "header"
    assert env["OVERWRITE_SETTING_Accounts_OAuth_Custom-Keycloak-identity_token_sent_via"] == "header"


def test_rocket_chat_sends_a_keycloak_account_no_emailed_code_until_an_admin_asks():
    """Keycloak's sign-in and its second factor are the check for an account
    that signs in with Keycloak. The bare setting name is the value Rocket.Chat
    creates the setting with; an OVERWRITE_SETTING_ form would undo, on every
    start, an administrator who turns emailed codes back on."""
    env = _service("rocketchat-oidc", "rocketchat")["environment"]
    key = "Accounts_twoFactorAuthentication_email_available_for_OAuth_users"
    assert env[key] == "false"
    assert f"OVERWRITE_SETTING_{key}" not in env


def test_nextcloud_names_the_user_oidc_release_its_wiring_is_written_for():
    """The wire action installs user_oidc from the app store and checks the
    installed release against this one before it runs the provider command
    written for it. Moving it is a decision to make after reading that
    command against the new release."""
    env = _service("nextcloud-s3-oidc", "app")["environment"]
    assert env["NEXTCLOUD_USER_OIDC_VERSION"] == "8.11.0"
