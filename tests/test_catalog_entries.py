"""Per-template facts a host or an action relies on and no generic gate
checks: what a template's backup mode does, where EspoCRM keeps what its
admin screens make, the Rocket.Chat settings its Keycloak sign-in needs, how
WordPress's mail plugin reads its relay password, and the user_oidc release
the Nextcloud wiring is written for.
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


def test_the_webmail_holds_its_server_still_for_the_backup():
    """Roundcube keeps its users' settings and sessions in SQLite with a
    write-ahead log, and the nightly backup and the pre-update copy read the
    files. Its apache processes stop before the copy and continue after it,
    so the copy holds the database and its log at one instant. The hold is
    in the webmail's service alone: dms goes on receiving mail. docker-init
    runs as PID 1, because a namespace's first process ignores a SIGSTOP sent
    from inside it, and the healthcheck passes a stopped master, because
    swarm replaces a task that turns unhealthy. The name matches only while
    the service runs the image's own apache2-foreground."""
    assert _entry("mailserver").quiesce == {
        "service": "roundcube",
        "pre": [["pkill", "-STOP", "-x", "apache2"]],
        "post": [["pkill", "-CONT", "-x", "apache2"]],
        "timeout_seconds": 10,
    }
    roundcube = _service("mailserver", "roundcube")
    assert roundcube["init"] is True
    assert "entrypoint" not in roundcube and "command" not in roundcube
    assert roundcube["healthcheck"]["test"][1].startswith(
        "grep -qs '^State:[[:space:]]*T' /proc/$$(pgrep -o -x apache2)/status || ")


def test_espocrm_keeps_what_its_admin_screens_make_in_volumes():
    """EspoCRM writes the fields, layouts, entities and extensions its admin
    screens make under custom/ and client/custom/, and the database columns
    of those fields in MariaDB. A path left in the container is lost each
    time swarm recreates the task, which every update does. The job runner
    reads the same tree. A minor version's migration rewrites the metadata
    under custom/ in place, so an update copies it and a rollback puts it
    back."""
    mounts = {"server-data:/var/www/html/data", "custom:/var/www/html/custom",
              "client-custom:/var/www/html/client/custom"}
    for service in ("espocrm", "cron"):
        assert mounts <= set(_service("espocrm", service)["volumes"])
    assert _entry("espocrm").lifecycle["versioned_volumes"] == ["custom"]


def test_espocrm_runs_its_jobs_with_the_images_job_runner():
    """The image's job runner is docker-daemon.sh. An entrypoint the image
    does not ship keeps the task from starting, and no scheduled job runs."""
    assert _service("espocrm", "cron")["entrypoint"] == "docker-daemon.sh"


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


def test_wordpress_mail_authenticates_with_the_relay_password_from_its_env():
    """WP Mail SMTP reads a WPMS_* constant only while WPMS_ON is true
    (src/Options.php is_const_enabled), so the password constant is defined
    with it, and both only when the env carries the password."""
    extra = _service("wordpress", "wp")["environment"]["WORDPRESS_CONFIG_EXTRA"]
    guarded = extra.split("if ( $$_wpms_smtp_pass !== false && $$_wpms_smtp_pass !== '' ) {", 1)
    assert len(guarded) == 2, extra
    body = guarded[1].split("}", 1)[0]
    assert "define( 'WPMS_ON', true );" in body
    assert "define( 'WPMS_SMTP_PASS', $$_wpms_smtp_pass );" in body
    assert "WPMS_ON" not in guarded[0]


def test_nextcloud_names_the_user_oidc_release_its_wiring_is_written_for():
    """The wire action installs user_oidc from the app store and checks the
    installed release against this one before it runs the provider command
    written for it. Moving it is a decision to make after reading that
    command against the new release."""
    env = _service("nextcloud-s3-oidc", "app")["environment"]
    assert env["NEXTCLOUD_USER_OIDC_VERSION"] == "8.11.0"
