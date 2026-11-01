"""The Healthchecks configure hook keeps an email channel to the admin while outgoing mail
is on (EMAIL_HOST set): verified, notifying on down and on up, attached to every check
when it is created and never again, updated in place, and removed when mail is turned
off. A channel a person added is never touched."""
from __future__ import annotations

import json

from healthchecks_configure_standins import BASE_ENV, install_fake_django, run_hook

ADMIN = BASE_ENV["ADMIN_EMAIL"]
HOST = "smtp.example.com"


def _email_channels(managers) -> list:
    return [c for c in managers["channel"].rows if getattr(c, "kind", "") == "email"]


def test_mail_on_makes_a_verified_channel_to_the_admin(monkeypatch, capsys):
    managers = install_fake_django(monkeypatch)
    run_hook(EMAIL_HOST=HOST)
    [channel] = _email_channels(managers)
    assert json.loads(channel.value) == {"value": ADMIN, "up": True, "down": True}
    assert channel.email_verified is True, "Healthchecks sends nothing to an unverified email channel"
    assert f"email={channel.code}" in capsys.readouterr().out


def test_the_channel_reaches_checks_that_already_exist(monkeypatch):
    """Mail is chosen after the install, so the channel arrives on a server whose checks
    exist: it attaches to every check in the project."""
    managers = install_fake_django(monkeypatch)
    run_hook()
    project = managers["project"].rows[0]
    gatus = managers["check"].create(project=project, slug="gatus-auth")
    assert _email_channels(managers) == []

    run_hook(EMAIL_HOST=HOST)
    [channel] = _email_channels(managers)
    assert all(channel in c.channel_set.added for c in managers["check"].rows)
    assert channel in gatus.channel_set.added


def test_an_update_keeps_the_bindings_and_never_reattaches(monkeypatch):
    """A stale address is rewritten in place, and a check the person detached the channel
    from stays detached."""
    managers = install_fake_django(monkeypatch)
    run_hook()
    project = managers["project"].rows[0]
    for slug in ("catena-backup-attempted", "catena-backup-succeeded"):
        managers["check"].create(project=project, slug=slug)
    run_hook(EMAIL_HOST=HOST)
    [channel] = _email_channels(managers)
    detached, kept = managers["check"].rows
    detached.channel_set.remove(channel)
    channel.value = json.dumps({"value": "old@example.com", "up": True, "down": True})

    run_hook(EMAIL_HOST=HOST)
    assert _email_channels(managers) == [channel]
    assert json.loads(channel.value)["value"] == ADMIN
    assert channel not in detached.channel_set.added
    assert channel in kept.channel_set.added


def test_mail_turned_off_removes_the_channel(monkeypatch, capsys):
    managers = install_fake_django(monkeypatch)
    run_hook(EMAIL_HOST=HOST)
    assert len(_email_channels(managers)) == 1
    run_hook()
    assert _email_channels(managers) == []
    assert "email=none" in capsys.readouterr().out


def test_a_channel_a_person_added_is_never_touched(monkeypatch):
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    project = managers["project"].create(owner=operator, name="vps-1")
    mine = managers["channel"].create(
        project=project, kind="email", value="me@example.com", email_verified=False)
    for host in (HOST, ""):
        run_hook(EMAIL_HOST=host)
    assert mine in managers["channel"].rows
    assert mine.value == "me@example.com" and mine.email_verified is False
