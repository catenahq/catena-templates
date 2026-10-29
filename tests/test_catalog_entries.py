"""Per-template facts a host or an action relies on and no generic gate
checks: what a template's backup mode does, where EspoCRM keeps what its
admin screens make, the Rocket.Chat settings its Keycloak sign-in needs, how
WordPress's mail plugin reads its relay password, and the user_oidc release
the Nextcloud wiring is written for, and how Nextcloud's start-up keeps its
master encryption key when the bucket changes.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
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


def _function(script: str, name: str) -> str:
    """A shell function of a wrapper, from its header to its closing brace."""
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}")
    return script[start:end]


def _shell(script: str) -> str:
    """What the shell runs after Docker reads each $$ as one $."""
    return script.replace("$$", "$")


def _sql(service: str, verb: str) -> str:
    """The statement of a wrapper that starts with `verb`, table names filled in."""
    found = re.search(rf'prepare\("({verb} [^"]*)"\)', _wrapper(service))
    assert found, verb
    return found.group(1).replace("$$t", "oc_storages").replace("$$f", "oc_filecache")


ROOT = "object::store:amazon::"
INSTANCE_ID = "ocabc123"
APPDATA_HASH = hashlib.md5(f"appdata_{INSTANCE_ID}".encode()).hexdigest()


def _long_id(bucket: str) -> str:
    """Nextcloud's Storage::adjustStorageId: an id over 64 characters is stored
    as its md5."""
    id_ = ROOT + bucket
    return hashlib.md5(id_.encode()).hexdigest() if len(id_) > 64 else id_


def _db(stores: dict[str, bool]) -> sqlite3.Connection:
    """oc_storages and oc_filecache holding `stores`: id -> holds the appdata folder."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE oc_storages (id TEXT UNIQUE, numeric_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE oc_filecache (storage INTEGER, path_hash TEXT)")
    for id_, appdata in stores.items():
        n = db.execute("INSERT INTO oc_storages (id) VALUES (?)", (id_,)).lastrowid
        if appdata:
            db.execute("INSERT INTO oc_filecache VALUES (?, ?)", (n, APPDATA_HASH))
    return db


def _roots(db: sqlite3.Connection, service: str = "app") -> list[str]:
    rows = db.execute(_sql(service, "SELECT"), [ROOT + "%", "%:%", APPDATA_HASH])
    return sorted(r[0] for r in rows)


def _rename(stores: dict[str, bool], bucket: str) -> list[str]:
    db = _db(stores)
    new = _long_id(bucket)
    rows = db.execute(_sql("app", "SELECT"), [ROOT + "%", "%:%", APPDATA_HASH]).fetchall()
    if len(rows) == 1 and rows[0][0] != new:
        db.execute(_sql("app", "UPDATE"), [new, rows[0][1], new])
    return sorted(r[0] for r in db.execute("SELECT id FROM oc_storages"))


def test_nextcloud_renames_the_root_storage_to_the_configured_bucket():
    """The master encryption key files sit on the root object store, which
    Nextcloud names after the bucket. A lone root store under another bucket's
    name takes the configured bucket's id, so the keys are found; the users'
    stores are untouched, and running it again changes nothing."""
    stores = {ROOT + "old": True, "object::user:alice": False, "home::bob": False}
    once = _rename(stores, "new")
    assert once == sorted([ROOT + "new", "object::user:alice", "home::bob"])
    assert _rename({i: i == ROOT + "new" for i in once}, "new") == once


def test_nextcloud_finds_a_hashed_root_store_by_its_appdata_folder():
    """A bucket name over 42 characters makes the id longer than 64, and
    Nextcloud stores its md5, which the id prefix never matches. The root
    store is then the 32-character, colon-free id that holds this instance's
    appdata folder; a hashed store without it (a user's) is not one."""
    old = _long_id("o" * 45)
    user = hashlib.md5(b"object::user:" + b"u" * 60).hexdigest()
    db = _db({old: True, user: False, "object::user:alice": False})
    assert _roots(db) == [old]


def test_nextcloud_renames_to_a_hashed_id_for_a_bucket_over_42_characters():
    new_bucket = "n" * 45
    assert len(ROOT + new_bucket) > 64 and _long_id(new_bucket) != ROOT + new_bucket
    once = _rename({ROOT + "old": True, "object::user:alice": False}, new_bucket)
    assert _long_id(new_bucket) in once and ROOT + "old" not in once
    hashed_old = _long_id("o" * 45)
    again = _rename({hashed_old: True}, "p" * 45)
    assert again == [_long_id("p" * 45)]


def test_nextcloud_changes_neither_of_two_root_storages():
    """A start on the new bucket already made an empty root store, with a
    master key of its own. Neither row is changed."""
    stores = {ROOT + "old": True, ROOT + "new": True, "object::user:alice": False}
    assert _rename(stores, "new") == sorted(stores)


def test_nextcloud_update_never_takes_an_id_that_exists():
    """An empty store that holds no appdata folder still owns its id: the
    UPDATE changes no row, which the wrapper turns into a refusal."""
    db = _db({ROOT + "old": True, _long_id("n" * 45): False})
    cur = db.execute(_sql("app", "UPDATE"), [_long_id("n" * 45), 1, _long_id("n" * 45)])
    assert cur.rowcount == 0


def test_nextcloud_renames_nothing_on_a_fresh_install():
    assert _rename({"object::user:alice": False}, "new") == ["object::user:alice"]


def _run_step(tmp_path, script: str, php_exit: int, tail: str = "echo continued"):
    """A shell snippet under the wrapper's `sh -ec`, with a php that exits
    `php_exit`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (bindir / "php").write_text(f"#!/bin/sh\nexit {php_exit}\n")
    (bindir / "php").chmod(0o755)
    return subprocess.run(
        ["sh", "-ec", _shell(script) + "\n" + tail],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )


def _rename_step() -> str:
    script = _wrapper("app")
    start = script.index("root_store_id() {")
    return script[start:script.index("\n", script.index("rename_root_storage ||"))]


def test_nextcloud_refuses_to_start_on_two_root_storages(tmp_path):
    """Running on the new, empty store would write files under a master key
    that cannot read the old ones, so the app exits 3 (the php step's
    refusal) and the start fails where the health checks see it."""
    r = _run_step(tmp_path, _rename_step(), php_exit=3)
    assert r.returncode == 3
    assert "continued" not in r.stdout


def test_nextcloud_goes_on_when_the_rename_step_cannot_run(tmp_path):
    for code, note in ((0, ""), (1, "rename failed")):
        r = _run_step(tmp_path / str(code), _rename_step(), php_exit=code)
        assert r.returncode == 0, r.stderr
        assert "continued" in r.stdout
        assert note in r.stderr


def test_nextcloud_refusal_names_both_stores_and_the_way_out():
    script = _wrapper("app")
    for text in ("refusing to start", 'implode(", ", $$ids)',
                 "files of the old store are unreadable while the new one is in use",
                 "delete the rows of the store you do not keep",
                 "set the bucket to the one the kept store belongs to", "exit(3)"):
        assert text in script, text


def test_nextcloud_store_id_is_hashed_over_64_characters(tmp_path):
    """The shell computes the id the rename and the cron guard compare with,
    by Nextcloud's rule: the md5 of an id longer than 64 characters."""
    for service in ("app", "cron"):
        function = _function(_wrapper(service), "root_store_id")
        for bucket in ("b" * 42, "b" * 43, "b" * 63):
            r = subprocess.run(
                ["sh", "-ec", _shell(function) + "\nroot_store_id"],
                capture_output=True, text=True, timeout=30,
                env=dict(os.environ, OBJECTSTORE_S3_BUCKET=bucket),
            )
            assert r.stdout == _long_id(bucket), (service, len(bucket))


def test_nextcloud_renames_before_the_upstream_entrypoint_runs():
    """The upstream entrypoint's upgrade step reads the object store, so the
    rename comes after the database wait and before the exec."""
    script = _wrapper("app")
    assert (script.index("until wait_for_db")
            < script.rindex("rename_root_storage")
            < script.index("exec /entrypoint.sh"))


def test_nextcloud_app_service_is_stop_first():
    """Swarm's default update order is stop-first and the app has no
    update_config, so no second app task runs beside the one being replaced."""
    assert "update_config" not in _service("nextcloud-s3-oidc", "app")["deploy"]


def test_nextcloud_cron_only_reads_the_storages():
    """A cron task, old env or new, must not insert or rename a root store."""
    script = _wrapper("cron")
    for verb in ("INSERT", "UPDATE", "DELETE", "rename_root_storage"):
        assert verb not in script, verb


def test_nextcloud_cron_guard_reads_the_same_roots_as_the_app():
    app, cron = _wrapper("app"), _wrapper("cron")
    assert _sql("cron", "SELECT") == _sql("app", "SELECT")
    assert _function(app, "root_store_id") == _function(cron, "root_store_id")


def _cron_loop() -> str:
    script = _wrapper("cron")
    return script[script.index("trap 'exit 0' TERM INT"):]


def _run_cron_tick(tmp_path, guard_exit: int):
    """One pass of the cron loop: the guard's php exits `guard_exit`; su
    logs the job it is asked to run; sleep ends the loop."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "ran"
    stubs = {
        "php": f"exit {guard_exit}",
        "su": f'echo "$@" >> "{log}"',
        "sleep": "exit 99",
    }
    for name, body in stubs.items():
        (bindir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bindir / name).chmod(0o755)
    cron = _wrapper("cron")
    script = _shell("\n".join([_function(cron, "root_store_id"),
                               _function(cron, "cron_guard"), _cron_loop()]))
    r = subprocess.run(
        ["sh", "-ec", script], capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"),
    )
    return r, log.read_text() if log.exists() else ""


def test_nextcloud_cron_runs_a_tick_only_when_the_guard_passes(tmp_path):
    _, ran = _run_cron_tick(tmp_path / "ok", guard_exit=0)
    assert "www-data" in ran and "cron.php" in ran
    _, ran = _run_cron_tick(tmp_path / "skip", guard_exit=4)
    assert ran == ""


def test_nextcloud_cron_loop_replaces_the_images_cron_script():
    script = _wrapper("cron")
    assert "exec /cron.sh" not in script
    assert script.index("cron_guard; then") < script.index("cron.php")


def _guard_passes(stores: dict[str, bool], bucket: str) -> bool:
    """The guard's decision on the rows its SELECT returns."""
    rows = _roots(_db(stores), "cron")
    return rows == [_long_id(bucket)]


def test_nextcloud_cron_guard_passes_only_for_the_one_configured_root_store():
    stores = {ROOT + "live": True, "object::user:alice": False}
    assert _guard_passes(stores, "live")
    assert not _guard_passes(stores, "other")
    assert not _guard_passes({**stores, ROOT + "other": True}, "live")
    assert not _guard_passes({"object::user:alice": False}, "live")
    hashed = {_long_id("h" * 45): True}
    assert _guard_passes(hashed, "h" * 45)


def test_the_container_managers_hold_the_socket_for_administrators_only():
    """Portainer and Arcane drive this server's Docker, so the block declares
    the docker_socket privilege the app checker looks for, the only address of
    each is for administrators, and the prose tells the client that what is
    deployed from them is outside Catena."""
    import json

    sources = Path(__file__).resolve().parent.parent / "sources"
    for slug, image in (("portainer", "portainer/portainer-ce:"), ("arcane", "ghcr.io/getarcaneapp/manager:v")):
        compose = yaml.safe_load(_entry(slug).compose_path.read_text(encoding="utf-8"))
        assert compose["x-catena"]["privileges"] == ["docker_socket"]
        service = compose["services"][slug]
        assert service["image"].startswith(image) and "latest" not in service["image"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
        assert "vps.auth.mode=admin-only" in service["labels"]
        assert not [label for label in service["labels"] if label.startswith("vps.auth.oidc")]
        for name, other in compose["services"].items():
            if name != slug:
                assert not [v for v in other.get("volumes", []) if "docker.sock" in str(v)], name
                assert not [label for label in other.get("labels", [])
                            if label.startswith(("vps.route", "vps.auth"))], name
        source = json.loads((sources / f"{slug}.json").read_text(encoding="utf-8"))["x-catena"]
        assert "outside Catena" in source["en"]["extra_notes"]
        assert "hors de Catena" in source["fr"]["extra_notes"]
