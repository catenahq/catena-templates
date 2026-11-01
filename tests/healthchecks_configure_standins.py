"""Stand-ins for the Django models blueprints/healthchecks/hooks/configure.py touches, so
its tests run the real script and exercise its branches rather than pattern-match it."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent.parent / "blueprints" / "healthchecks" / "hooks" / "configure.py"

BASE_ENV = {
    "ADMIN_EMAIL": "ops@example.com",
    "SUPERUSER_PASSWORD": "hunter2",
    "PING_KEY": "ping-key",
    "READ_KEY": "ro-key",
    "API_KEY": "rw-key",
    "HC_PROJECT_NAME": "",
    "EMAIL_HOST": "",
}


class _Row:
    """One stand-in model instance. `managers` is set per test, so a channel can attach
    itself to the project's checks and a check to the project's channels, as
    Healthchecks's Channel.assign_all_checks() and Check.assign_all_channels() do."""

    _next_code = 0
    managers: dict = {}

    def __init__(self, **fields):
        type(self)._next_code += 1
        self.code = f"code-{type(self)._next_code}"
        self.pk = type(self)._next_code
        self.email = ""
        self.name = ""
        self.is_staff = True
        self.is_superuser = True
        self.channel_set = _ChannelSet()
        for key, value in fields.items():
            setattr(self, key, value)

    def save(self, **_kwargs):
        pass

    def delete(self):
        for manager in self.managers.values():
            if self in manager.rows:
                manager.rows.remove(self)

    def assign_all_checks(self):
        for check in self.managers["check"].rows:
            if check.project is self.project:
                check.channel_set.add(self)

    def assign_all_channels(self):
        self.channel_set.added = [
            c for c in self.managers["channel"].rows if c.project is self.project]


class _ChannelSet:
    """A check's channel bindings (Healthchecks's Check.channel_set)."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        if obj not in self.added:
            self.added.append(obj)

    def remove(self, obj):
        self.added.remove(obj)


def _field_matches(row, key, value):
    if key.endswith("__iexact"):
        return str(getattr(row, key[:-8], "")).lower() == str(value).lower()
    return getattr(row, key, None) == value


class _QuerySet:
    def __init__(self, manager, rows):
        self._manager = manager
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def exclude(self, **kwargs):
        return _QuerySet(self._manager, [
            r for r in self._rows
            if not all(getattr(r, k, None) == v for k, v in kwargs.items())])

    def update(self, **kwargs):
        for row in self._rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def exists(self):
        return bool(self._rows)

    def delete(self):
        for row in self._rows:
            self._manager.rows.remove(row)
        return len(self._rows), {}


class _Manager:
    def __init__(self):
        self.rows: list[_Row] = []

    def _match(self, **kwargs):
        return [
            r for r in self.rows
            if all(_field_matches(r, k, v) for k, v in kwargs.items())
        ]

    def filter(self, **kwargs):
        return _QuerySet(self, self._match(**kwargs))

    def create(self, **kwargs):
        row = _Row(**kwargs)
        self.rows.append(row)
        return row

    def create_superuser(self, **kwargs):
        return self.create(**kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        found = self._match(**kwargs)
        if found:
            for key, value in (defaults or {}).items():
                setattr(found[0], key, value)
            return found[0], False
        row = self.create(**{**kwargs, **(defaults or {})})
        return row, True


class _ProfileManager(_Manager):
    """Stand-in for Healthchecks's ProfileManager.

    for_user() get-or-creates, and a fresh profile's `theme` is None, not "": None
    means "never chosen", "" means the person picked Light. A stub that returned ""
    here would let a regression through.
    """

    def for_user(self, user):
        found = self._match(user=user)
        if found:
            return found[0]
        row = self.create(user=user)
        row.theme = None
        return row


def install_fake_django(monkeypatch) -> dict[str, _Manager]:
    """Register the module graph the hook imports, and return the managers so
    a test can inspect what the script did."""
    managers = {name: _Manager() for name in ("user", "project", "channel", "check")}
    managers["profile"] = _ProfileManager()
    monkeypatch.setattr(_Row, "managers", managers)

    def _model(manager):
        return type("Model", (), {"objects": manager})

    django = types.ModuleType("django")
    django.setup = lambda: None
    auth = types.ModuleType("django.contrib.auth")
    auth.get_user_model = lambda: _model(managers["user"])
    accounts_models = types.ModuleType("hc.accounts.models")
    accounts_models.Project = _model(managers["project"])
    accounts_models.Profile = _model(managers["profile"])
    api_models = types.ModuleType("hc.api.models")
    api_models.Channel = _model(managers["channel"])
    api_models.Check = _model(managers["check"])

    for name, mod in {
        "django": django,
        "django.contrib": types.ModuleType("django.contrib"),
        "django.contrib.auth": auth,
        "hc": types.ModuleType("hc"),
        "hc.accounts": types.ModuleType("hc.accounts"),
        "hc.accounts.models": accounts_models,
        "hc.api": types.ModuleType("hc.api"),
        "hc.api.models": api_models,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return managers


def run_hook(**env) -> None:
    """Run the real hook once with BASE_ENV overridden by `env`, as the host runs it
    in the app's container with the folder's .env."""
    with mock.patch.dict(os.environ, {**BASE_ENV, **env}):
        exec(compile(HOOK.read_text(), str(HOOK), "exec"), {"__name__": "__hook__"})
