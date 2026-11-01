"""The Healthchecks configure hook keeps one superuser and one project across an admin
email change, creates no check, pins the project's keys, leaves every channel it does not
own alone, and sets a fresh profile's theme once."""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest
from healthchecks_configure_standins import BASE_ENV, HOOK, install_fake_django, run_hook

ADMIN = BASE_ENV["ADMIN_EMAIL"]


def test_an_admin_email_change_renames_the_one_superuser(monkeypatch):
    """Found by its new email the superuser would be a second one, whose project can
    never take the pinned ping key (unique in Healthchecks)."""
    managers = install_fake_django(monkeypatch)
    run_hook(EMAIL_HOST="smtp.example.com")
    [operator] = managers["user"].rows
    [project] = managers["project"].rows
    [channel] = managers["channel"].rows
    check = managers["check"].create(project=project, slug="gatus-auth")
    check.channel_set.add(channel)

    run_hook(EMAIL_HOST="smtp.example.com", ADMIN_EMAIL="new@example.com")
    assert managers["user"].rows == [operator]
    assert operator.username == operator.email == "new@example.com"
    assert operator.is_superuser and operator.is_staff
    assert managers["project"].rows == [project]
    assert project.ping_key == BASE_ENV["PING_KEY"]
    assert managers["channel"].rows == [channel]
    assert json.loads(channel.value)["value"] == "new@example.com"
    assert channel in check.channel_set.added


def test_a_mixed_case_admin_email_gives_a_lowercase_login_and_an_as_given_channel(monkeypatch):
    """Keycloak sends the address lowercase and Healthchecks matches the header exactly,
    so the superuser's username and email are lowercase; the email channel keeps the
    address as given."""
    managers = install_fake_django(monkeypatch)
    run_hook(EMAIL_HOST="smtp.example.com", ADMIN_EMAIL="Ops@Example.com")
    [operator] = managers["user"].rows
    assert operator.username == operator.email == "ops@example.com"
    [channel] = managers["channel"].rows
    assert json.loads(channel.value)["value"] == "Ops@Example.com"

    run_hook(EMAIL_HOST="smtp.example.com", ADMIN_EMAIL="Ops@Example.com")
    assert managers["user"].rows == [operator]


def test_a_lowercase_duplicate_of_the_mixed_case_operator_is_merged_into_it(monkeypatch):
    """A gated visit made Healthchecks create a second user on the lowercase address.
    The deploy ends with the operator alone, owning every project; a user on another
    address is left as it is."""
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username="Ops@Example.com", email="Ops@Example.com")
    mine = managers["project"].create(owner=operator, name="vps-1", ping_key="ping-key")
    dup = managers["user"].create(username="u-abc", email="ops@example.com")
    theirs = managers["project"].create(owner=dup, name="second")
    other = managers["user"].create(username="bob", email="bob@example.com")
    bobs = managers["project"].create(owner=other, name="bobs")

    run_hook(ADMIN_EMAIL="Ops@Example.com")
    assert managers["user"].rows == [operator, other]
    assert operator.username == operator.email == "ops@example.com"
    assert mine.owner is operator and theirs.owner is operator
    assert bobs.owner is other
    assert mine.ping_key == "ping-key"


def test_an_unpinned_host_adopts_the_lowercase_user_instead_of_adding_a_second(monkeypatch):
    managers = install_fake_django(monkeypatch)
    gate = managers["user"].create(username="u-abc", email="ops@example.com")
    run_hook(ADMIN_EMAIL="Ops@Example.com")
    assert managers["user"].rows == [gate]
    assert gate.username == gate.email == "ops@example.com"


def test_the_hook_creates_no_check(monkeypatch):
    """The scheduled lanes' checks belong to catena-admin's catena-schedule, which keeps
    them on the lanes' schedule; a check the hook made would be a second owner holding
    another one."""
    managers = install_fake_django(monkeypatch)
    run_hook(EMAIL_HOST="smtp.example.com")
    assert managers["check"].rows == []


def test_a_channel_the_hook_does_not_own_is_left_alone(monkeypatch):
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    project = managers["project"].create(owner=operator, name="vps-1")
    mine = managers["channel"].create(project=project, kind="email", value="me@example.com")
    ntfy = managers["channel"].create(project=project, kind="ntfy",
                                      value='{"topic": "s3cret"}', name="ntfy (vps-1)")
    for host in ("smtp.example.com", "", "smtp.example.com"):
        run_hook(EMAIL_HOST=host)
    assert mine in managers["channel"].rows and mine.value == "me@example.com"
    assert ntfy in managers["channel"].rows and ntfy.value == '{"topic": "s3cret"}'


def test_the_hook_reads_no_push_channel_setting():
    assert "NTFY" not in HOOK.read_text()


# --- the project and its keys ---

def test_the_project_takes_the_three_keys_and_the_name(monkeypatch):
    managers = install_fake_django(monkeypatch)
    run_hook(HC_PROJECT_NAME="Acme")
    [project] = managers["project"].rows
    assert (project.name, project.ping_key, project.api_key_readonly, project.api_key) == (
        "Acme", "ping-key", "ro-key", "rw-key")


def test_a_project_without_a_given_name_is_called_catena_and_keeps_a_name_it_has(monkeypatch):
    managers = install_fake_django(monkeypatch)
    run_hook()
    [project] = managers["project"].rows
    assert project.name == "Catena"
    project.name = "Renamed by a person"
    run_hook()
    assert project.name == "Renamed by a person"
    run_hook(HC_PROJECT_NAME="Acme")
    assert project.name == "Acme"


@pytest.mark.parametrize("key, field", [("PING_KEY", "ping_key"), ("READ_KEY", "api_key_readonly"),
                                        ("API_KEY", "api_key")])
def test_a_key_the_store_does_not_hold_leaves_the_project_key_as_it_is(monkeypatch, key, field):
    managers = install_fake_django(monkeypatch)
    run_hook()
    [project] = managers["project"].rows
    setattr(project, field, "kept")
    run_hook(**{key: ""})
    assert getattr(project, field) == "kept"
    assert [getattr(project, f) for f in ("ping_key", "api_key_readonly", "api_key") if f != field] == [
        v for k, v in (("PING_KEY", "ping-key"), ("READ_KEY", "ro-key"), ("API_KEY", "rw-key"))
        if k != key]


def test_the_superuser_password_is_set_on_creation_only(monkeypatch):
    managers = install_fake_django(monkeypatch)
    run_hook()
    [operator] = managers["user"].rows
    assert operator.password == "hunter2"
    operator.password = "changed in /admin/"
    run_hook(SUPERUSER_PASSWORD="another")
    assert operator.password == "changed in /admin/"


def test_a_missing_superuser_password_or_admin_email_fails_the_deploy(monkeypatch):
    install_fake_django(monkeypatch)
    with pytest.raises(SystemExit):
        run_hook(ADMIN_EMAIL="")
    env = {k: v for k, v in BASE_ENV.items() if k != "SUPERUSER_PASSWORD"}
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises(KeyError):
        exec(compile(HOOK.read_text(), str(HOOK), "exec"), {"__name__": "__hook__"})


# --- theme default ---

def test_the_theme_default_is_set_once_and_never_argued_with():
    """The hook sets "system" only on a profile whose theme is still NULL, and writes a
    value the accounts view accepts."""
    body = HOOK.read_text()
    assert "Profile.objects.for_user(operator)" in body, (
        "the profile is not resolved through the manager, which get-or-creates")
    assert "profile.theme is None" in body, (
        "the theme is set unconditionally: a person who chose Light would have it "
        "overwritten on every deploy")
    assert 'profile.theme = "system"' in body


def test_the_theme_write_touches_only_the_theme():
    """A bare .save() would rewrite every column on the profile, including any a person
    changed through the UI between deploys."""
    assert 'profile.save(update_fields=["theme"])' in HOOK.read_text()


def test_a_fresh_profile_follows_the_browser_preference(monkeypatch):
    managers = install_fake_django(monkeypatch)
    run_hook()
    [profile] = managers["profile"].rows
    assert profile.theme == "system"


def test_a_person_who_chose_light_keeps_it(monkeypatch):
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    chosen = managers["profile"].create(user=operator)
    chosen.theme = ""
    run_hook()
    assert chosen.theme == "", "a deliberate Light choice was overwritten"
