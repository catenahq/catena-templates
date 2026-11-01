"""Gatus, Healthchecks and the Beszel hub: the networks that keep the monitoring
plane apart from the client apps, the loopback ports, the data each declares and
the swarm shape of their composes.

Healthchecks takes the signed-in user from a header whoever sends it, so only the
network decides who may call it. The hub trusts the sign-in header from its gate's
network, so only the gate and the hub join that one. Gatus is the only one of the
three on catena-network.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model, quiesce_lint  # noqa: E402

APPS = ("healthchecks", "gatus", "beszel")
HC_NETWORK = "catena-healthchecks"
BESZEL_NETWORK = "catena-beszel"
GATE_NETWORK = "catena-beszelgate"
PORTS = {"healthchecks": (8000, 18000), "gatus": (8080, 18080), "beszel": (8090, 18190)}
VOLUMES = {"healthchecks": "healthchecks-data", "gatus": "gatus-data", "beszel": "beszel-hub-data"}
READ = re.compile(r"^\$\{(\w+):-(.*)\}$")


def _compose(slug: str) -> dict:
    return yaml.safe_load((ROOT / "blueprints" / slug / "docker-compose.yml").read_text(encoding="utf-8"))


def _entry(slug: str) -> model.Entry:
    return next(e for e in model.load_sources() if e.slug == slug)


def _networks(service: dict) -> set[str]:
    nets = service.get("networks") or {}
    return set(nets)


def _external(doc: dict) -> set[str]:
    return {name for name, decl in (doc.get("networks") or {}).items() if (decl or {}).get("external")}


def _default(value: str) -> str:
    match = READ.match(str(value))
    assert match, f"{value!r} is not a read of the app's .env with a default"
    return match.group(2)


def test_healthchecks_is_on_its_own_network_only():
    doc = _compose("healthchecks")
    app = doc["services"]["app"]
    assert app["environment"]["REMOTE_USER_HEADER"] == "HTTP_X_FORWARDED_EMAIL"
    assert list(app["networks"]) == [HC_NETWORK]
    assert app["networks"][HC_NETWORK]["aliases"] == ["healthchecks"]
    assert _external(doc) == {HC_NETWORK}


def test_the_hub_is_on_its_own_networks_beside_its_private_one():
    doc = _compose("beszel")
    app = doc["services"]["app"]
    assert _networks(app) == {"default", BESZEL_NETWORK, GATE_NETWORK}
    for net in (BESZEL_NETWORK, GATE_NETWORK):
        assert app["networks"][net]["aliases"] == ["beszel-hub"]
    assert _external(doc) == {BESZEL_NETWORK, GATE_NETWORK}


def test_the_helper_of_the_hub_is_on_no_shared_network():
    assert _networks(_compose("beszel")["services"]["probe"]) == {"default"}


def test_only_gatus_joins_catena_network():
    for slug in APPS:
        doc = _compose(slug)
        joined = {name for name, svc in doc["services"].items() if "catena-network" in _networks(svc)}
        assert joined == ({"app"} if slug == "gatus" else set()), slug


def test_gatus_reaches_healthchecks_and_the_hub_and_drops_raw_packets():
    doc = _compose("gatus")
    app = doc["services"]["app"]
    assert {HC_NETWORK, BESZEL_NETWORK} <= _networks(app)
    assert app["networks"]["catena-network"]["aliases"] == ["gatus"]
    assert "NET_RAW" in app["cap_drop"]


def test_no_client_network_reaches_the_gate_network_or_the_healthchecks_network():
    for slug in APPS:
        for name, svc in _compose(slug)["services"].items():
            nets = _networks(svc)
            if GATE_NETWORK in nets:
                assert (slug, name) == ("beszel", "app")
            if HC_NETWORK in nets:
                assert (slug, name) in {("healthchecks", "app"), ("gatus", "app")}
            if BESZEL_NETWORK in nets:
                assert (slug, name) in {("beszel", "app"), ("gatus", "app")}


def test_the_sign_in_header_is_read_with_its_allowlist_and_empty_reads_as_unset():
    """The hub trusts every peer when TRUSTED_PROXY_IPS is empty, so the server writes the two together."""
    env = _compose("beszel")["services"]["app"]["environment"]
    assert _default(env["TRUSTED_AUTH_HEADER"]) == "" and _default(env["TRUSTED_PROXY_IPS"]) == ""
    assert not {"USER_EMAIL", "USER_PASSWORD"} & set(env)


def test_the_loopback_ip_and_the_alias_are_allowed_hosts():
    """Every consumer on the server dials the loopback publish by IP; Django answers 400 for a Host it does not list."""
    hosts = _default(_compose("healthchecks")["services"]["app"]["environment"]["ALLOWED_HOSTS"]).split(",")
    assert "127.0.0.1" in hosts and "localhost" in hosts and "healthchecks" in hosts


def test_a_server_with_none_of_the_values_the_server_writes_still_starts_healthchecks():
    env = _compose("healthchecks")["services"]["app"]["environment"]
    assert _default(env["SITE_ROOT"]) == "http://localhost:8000"


def test_the_secret_key_is_minted_by_the_server_and_never_falls_back():
    """Healthchecks signs badge links, unsubscribe links and API key hashes with it, and its own default is a known value."""
    entry = _entry("healthchecks")
    assert entry.block["env"]["SECRET_KEY"] == {"generated": {"length": 50, "alphabet": "alnum"}}
    text = (ROOT / "blueprints" / "healthchecks" / "docker-compose.yml").read_text(encoding="utf-8")
    assert _compose("healthchecks")["services"]["app"]["environment"]["SECRET_KEY"] == "${SECRET_KEY}"
    assert "---" not in text


@pytest.mark.parametrize("slug", APPS)
def test_ports_are_published_in_host_mode_with_the_long_syntax(slug):
    target, published = PORTS[slug]
    ports = _compose(slug)["services"]["app"]["ports"]
    assert ports == [{"target": target, "published": published, "protocol": "tcp", "mode": "host"}]


@pytest.mark.parametrize("slug", APPS)
def test_the_data_is_declared_for_a_service_and_volume_the_file_has(slug):
    doc = _compose(slug)
    assert doc["x-catena"]["data"] == {"app": {"volume": VOLUMES[slug], "upgrade": "in_place"}}
    assert VOLUMES[slug] in doc["volumes"]
    assert VOLUMES[slug] in " ".join(doc["services"]["app"]["volumes"])
    assert [k for k in doc if k.startswith("x-")] == ["x-catena"]


@pytest.mark.parametrize("slug", APPS)
def test_data_services_stay_on_the_data_node_and_restart_with_a_delay(slug):
    app = _compose(slug)["services"]["app"]
    assert app["deploy"]["placement"]["constraints"] == ["node.labels.catena.role==data"]
    assert app["deploy"]["restart_policy"]["condition"] == "any"
    assert app["deploy"]["restart_policy"]["delay"]


@pytest.mark.parametrize("slug", APPS)
def test_no_swarm_ignored_or_refused_key(slug):
    text = (ROOT / "blueprints" / slug / "services.yml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for key in ("host_ip", "restart:", "network_mode:", "depends_on:"):
        assert key not in body, f"{slug}: {key}"


@pytest.mark.parametrize("slug", APPS)
def test_no_address_or_sign_in_label(slug):
    """The core sign-in gate fronts these apps; no vps.route, vps.auth or vps.health label is read."""
    for svc in _compose(slug)["services"].values():
        for label in svc.get("labels") or []:
            assert re.match(r"vps\.auto-update=", label), f"{slug}: {label}"


@pytest.mark.parametrize("slug", APPS)
def test_no_environment_value_is_a_literal_credential(slug):
    for svc in _compose(slug)["services"].values():
        for key, value in (svc.get("environment") or {}).items():
            if re.search(r"PASSWORD|SECRET|TOKEN|KEY", key):
                assert str(value).startswith("${"), f"{slug}: {key} is a literal"


@pytest.mark.parametrize("slug", APPS)
def test_the_block_names_its_version_service_and_asks_for_no_newer_release(slug):
    entry = _entry(slug)
    assert entry.block.get("version_from") == "app"
    assert "requires_catena" not in entry.block


# The capabilities whose private network trusts the sign-in header of whoever is on it: who may
# provide each and who may read it (the provider may always read its own). Every blueprint of
# the catalog is held to this table.
PRIVATE_CAPABILITIES = {
    "healthchecks": {"provider": "healthchecks", "readers": {"gatus"}},
    "beszel": {"provider": "beszel", "readers": {"gatus"}},
    "beszelgate": {"provider": "beszel", "readers": set()},
}


def test_only_the_listed_apps_provide_or_read_a_private_network_capability():
    problems = []
    for entry in model.load_sources():
        for capability in entry.block.get("provides") or {}:
            rule = PRIVATE_CAPABILITIES.get(capability)
            if rule and entry.slug != rule["provider"]:
                problems.append(f"{entry.slug} provides {capability}, which only {rule['provider']} may provide")
        read = {cap for expr in entry.managed_env.values() for cap, _ in quiesce_lint.provided_reads(expr)}
        for capability in sorted(read):
            rule = PRIVATE_CAPABILITIES.get(capability)
            if rule and entry.slug != rule["provider"] and entry.slug not in rule["readers"]:
                allowed = ", ".join(sorted(rule["readers"])) or "no app"
                problems.append(f"{entry.slug} reads {capability}, which only {allowed} may read")
    assert not problems, "; ".join(problems)


def test_the_private_network_capability_table_matches_the_apps_that_exist():
    entries = {e.slug: e for e in model.load_sources()}
    for capability, rule in PRIVATE_CAPABILITIES.items():
        assert capability in (entries[rule["provider"]].block.get("provides") or {})
        for reader in rule["readers"]:
            assert reader in entries


def test_the_capabilities_the_apps_provide_name_a_key_of_their_env():
    assert _entry("healthchecks").block["provides"] == {"healthchecks": {"url": "HC_URL"}}
    assert _entry("beszel").block["provides"] == {"beszel": {"url": "HUB_URL"}, "beszelgate": {"url": "HUB_URL"}}
    gatus = _entry("gatus").managed_env
    assert gatus["HC_URL"] == "{{ provided.healthchecks.url }}"
    assert gatus["HUB_URL"] == "{{ provided.beszel.url }}"
