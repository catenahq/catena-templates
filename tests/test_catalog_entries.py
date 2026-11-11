"""Per-template facts a host or an action relies on and no generic gate
checks: what a template's backup mode does, where EspoCRM keeps what its
admin screens make, the Rocket.Chat settings its Keycloak sign-in needs, how
WordPress's mail plugin reads its relay password, and the user_oidc release
the Nextcloud wiring is written for, how Nextcloud's start-up and cron loop
run, and that an alias named after
its app is that app's alone, and that no card names a panel button or tab
that does not exist.
"""
from __future__ import annotations

import os
import re
import subprocess
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


def _label_default(labels: list[str], key: str) -> str | None:
    """A label's value as an app reads it while its setting is unset: the
    literal, or the default of `${NAME:-default}`."""
    for label in labels:
        name, _, value = label.partition("=")
        if name == key:
            found = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", value.strip())
            return found.group(1) if found else value
    return None


def _names(compose: dict) -> tuple[set[str], set[str]]:
    """The service keys of a compose, and the aliases its services carry on any network."""
    aliases = set()
    for service in compose["services"].values():
        networks = service.get("networks")
        if isinstance(networks, dict):
            for network in networks.values():
                aliases.update((network or {}).get("aliases") or ())
    return set(compose["services"]), aliases


def test_an_alias_named_after_its_app_is_that_apps_alone():
    """A service on catena-network reaches a sibling by an alias named
    `<id>-<role>` on the stack network: catena-network holds every app's service
    keys, and Docker names a task there only once it is healthy. No other app
    has a service or an alias of that name."""
    names = {e.slug: _names(yaml.safe_load(e.compose_path.read_text(encoding="utf-8")))
             for e in model.load_sources()}
    owned = {(slug, alias) for slug, (_, aliases) in names.items() for alias in aliases
             if alias.startswith(slug + "-")}
    assert {("invoiceninja", "invoiceninja-fpm"), ("rocketchat-oidc", "rocketchat-oidc-jitsi")} <= owned
    for slug, alias in owned:
        holders = [other for other, (keys, aliases) in names.items()
                   if other != slug and alias in keys | aliases]
        assert holders == [], (alias, holders)


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
    the service ends in the image's own entrypoint running apache2-foreground."""
    assert _entry("mailserver").quiesce == {
        "service": "roundcube",
        "pre": [["pkill", "-STOP", "-x", "apache2"]],
        "post": [["pkill", "-CONT", "-x", "apache2"]],
        "timeout_seconds": 10,
    }
    roundcube = _service("mailserver", "roundcube")
    assert roundcube["init"] is True
    assert "command" not in roundcube
    assert roundcube["entrypoint"][-1].strip().endswith("exec /docker-entrypoint.sh apache2-foreground")
    assert roundcube["healthcheck"]["test"][1].startswith(
        "grep -qs '^State:[[:space:]]*T' /proc/$$(pgrep -o -x apache2)/status || ")


def test_the_webmail_verifies_the_mail_server_by_the_mail_hosts_name():
    """Roundcube connects to the service name dms, while the mail server's
    certificate names the mail host, so verification on its own fails on the
    name. The webmail's entrypoint writes a config file the image includes,
    with both connection options verifying the peer against ${MAIL_HOSTNAME}
    (Docker fills it in at deploy). Each $ of the PHP is written $$, which
    Docker reads as one $, and the heredoc is quoted so the shell leaves it."""
    script = _service("mailserver", "roundcube")["entrypoint"][-1]
    assert "<<'EOF'" in script
    assert "/var/roundcube/config/catena-tls.inc.php" in script
    for option in ("imap_conn_options", "smtp_conn_options"):
        assert f"$$config['{option}'] = $$tls;" in script
    for setting in ("'verify_peer' => true", "'verify_peer_name' => true",
                    "'peer_name' => '${MAIL_HOSTNAME}'"):
        assert setting in script
    assert "$" not in script.replace("$$", "").replace("${MAIL_HOSTNAME}", "")


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


def _wrapper(service: str) -> str:
    return _service("nextcloud-s3-oidc", service)["entrypoint"][2]


def _shell(script: str) -> str:
    """What the shell runs after Docker reads each $$ as one $."""
    return script.replace("$$", "$")


def test_nextcloud_app_service_is_stop_first():
    """Swarm's default update order is stop-first and the app has no
    update_config, so no second app task runs beside the one being replaced."""
    assert "update_config" not in _service("nextcloud-s3-oidc", "app")["deploy"]


def _cron_loop() -> str:
    script = _wrapper("cron")
    return script[script.index("trap 'exit 0' TERM INT"):]


def _run_cron_tick(tmp_path):
    """One pass of the cron loop: su logs the job it is asked to run; sleep
    ends the loop."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "ran"
    stubs = {"su": f'echo "$@" >> "{log}"', "sleep": "exit 99"}
    for name, body in stubs.items():
        (bindir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bindir / name).chmod(0o755)
    r = subprocess.run(
        ["sh", "-ec", _shell(_cron_loop())], capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    return r, log.read_text() if log.exists() else ""


def test_nextcloud_cron_runs_a_tick_as_www_data_on_every_pass(tmp_path):
    _, ran = _run_cron_tick(tmp_path)
    assert "www-data" in ran and "cron.php" in ran


def test_nextcloud_cron_loop_replaces_the_images_cron_script():
    script = _wrapper("cron")
    assert "exec /cron.sh" not in script
    assert "cron_guard" not in script and "OBJECTSTORE" not in script
    assert "sleep 300" in script and "cron.php" in script


def test_the_container_managers_hold_the_socket_for_administrators_only():
    """Portainer and Arcane drive this server's Docker, so the block declares
    the docker_socket privilege the app checker looks for, the only address of
    each is for administrators, and the prose tells the client that what is
    deployed from them is outside Catena."""
    for slug, image in (("portainer", "portainer/portainer-ce:"), ("arcane", "ghcr.io/getarcaneapp/manager:v")):
        compose = yaml.safe_load(_entry(slug).compose_path.read_text(encoding="utf-8"))
        assert compose["x-catena"]["privileges"] == ["docker_socket"]
        service = compose["services"][slug]
        assert service["image"].startswith(image) and "latest" not in service["image"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
        assert _label_default(service["labels"], "vps.auth.mode") == "admin-only"
        assert not [label for label in service["labels"] if label.startswith("vps.auth.oidc")]
        for name, other in compose["services"].items():
            if name != slug:
                assert not [v for v in other.get("volumes", []) if "docker.sock" in str(v)], name
                assert not [label for label in other.get("labels", [])
                            if label.startswith(("vps.route", "vps.auth"))], name
        card = _entry(slug).catena
        assert "outside Catena" in card["en"]["extra_notes"]
        assert "hors de Catena" in card["fr"]["extra_notes"]


# The words of the panel's cards

REFUSED_WORDS = ("Click **Deploy**", "Cliquez **Deploy**", "Environment tab", "onglet Environment",
                 "Start application", "Stop application", "Démarrer l'application", "Arrêter l'application")
RELATIVE_LINK = re.compile(r"\]\(\s*(?![A-Za-z][A-Za-z0-9+.-]*:)")
CARD_TEXTS = ("what_it_is", "setup_steps", "extra_notes")


def _refused_in_card(slug: str, card: dict) -> list[str]:
    """Each phrase a card's `what_it_is`, `setup_steps` and `extra_notes` carry in en and fr that
    names a button or tab the panel lacks, another product's name (except in that product's own
    card), or a link without a full address."""
    found = []
    for lang in ("en", "fr"):
        for field in CARD_TEXTS:
            text = card.get(lang, {}).get(field) or ""
            words = REFUSED_WORDS + (() if slug == "portainer" else ("Portainer",))
            found += [f"{lang}.{field}: {word}" for word in words if word in text]
            if RELATIVE_LINK.search(text):
                found.append(f"{lang}.{field}: a relative link")
    return found


def test_no_card_names_a_panel_button_or_tab_it_does_not_have():
    for entry in model.load_sources():
        assert _refused_in_card(entry.slug, entry.catena) == [], entry.slug


def test_the_card_guard_refuses_each_phrase_and_a_relative_link():
    card = {"en": {"setup_steps": "1. Click **Deploy**. Open the Environment tab. Press Start application, "
                                  "then Stop application. See [the guide](../guide.md) and [Portainer](https://x.test)."},
            "fr": {"what_it_is": "Cliquez **Deploy**, onglet Environment, Démarrer l'application, "
                                 "Arrêter l'application.",
                   "extra_notes": "Voir [le guide](/docs/guide)."}}
    found = _refused_in_card("erpnext", card)
    for word in REFUSED_WORDS[:1] + ("Environment tab", "Start application", "Stop application", "Portainer"):
        assert f"en.setup_steps: {word}" in found, word
    assert "en.setup_steps: a relative link" in found
    for word in ("Cliquez **Deploy**", "onglet Environment", "Démarrer l'application", "Arrêter l'application"):
        assert f"fr.what_it_is: {word}" in found, word
    assert "fr.extra_notes: a relative link" in found
    assert _refused_in_card("portainer", {"en": {"extra_notes": "Portainer holds [the socket](https://x.test)."}}) == []
