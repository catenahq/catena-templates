"""lib/pack.py: neat files pack into the compose and a compose unpacks into them."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import pack  # noqa: E402

CATALOG = ROOT / "blueprints"
APPS = sorted(p.parent.name for p in CATALOG.glob("*/docker-compose.yml"))

# The apps whose compose is out of normal form, each with the reason unpack gives. A
# commit that puts an app in normal form deletes its line.
OUT_OF_FORM = {
    "clamav": "images is out of order",
    "collabora": "images is out of order",
    "erpnext-helpdesk": "images is out of order",
    "nextcloud-s3-oidc": "images is out of order",
    "rocketchat-oidc": "images is out of order",
    "element": "followed by exactly one empty line",
    "wordpress": "followed by exactly one empty line",
}

SERVICES = "services:\n  web:\n    image: web:1.0\n"
SETTINGS = "env:\n  DOMAIN_HOST:\n    managed: {expr: \"x\"}\n"
LIFECYCLE = "hooks:\n  service: web\n  verify:\n    - - sh\n      - -c\n      - {file: hooks/verify.sh}\n"
NEAT = {pack.SERVICES: SERVICES, "settings.yml": SETTINGS, "lifecycle.yml": LIFECYCLE}
HOOKS = {"hooks/verify.sh": "echo ok\n"}
PACKED = (
    "x-catena:\n  schema: 2\n"
    "  env:\n    DOMAIN_HOST:\n      managed: {expr: \"x\"}\n"
    "  hooks:\n    service: web\n    verify:\n      - - sh\n        - -c\n        - |\n          echo ok\n"
    "\n" + SERVICES)


def _scratch(tmp_path, app, text=None):
    """A copy of the app's compose in a tree of its own; the tree."""
    folder = tmp_path / "blueprints" / app
    folder.mkdir(parents=True)
    if text is None:
        shutil.copy(CATALOG / app / pack.COMPOSE, folder / pack.COMPOSE)
    else:
        (folder / pack.COMPOSE).write_text(text)
    return tmp_path


def _gap(text):
    """The compose with an empty line after its block."""
    lines = text.split("\n")
    at = next(i for i, line in enumerate(lines) if i and line and not line.startswith(" "))
    return "\n".join(lines[:at] + [""] + lines[at:])


def _pack(neat=None, hooks=None):
    return pack.pack_texts({**NEAT, **(neat or {})}, {**HOOKS, **(hooks or {})}, "blueprints/web/")


# the catalog

@pytest.mark.parametrize("app", APPS)
def test_an_app_unpacks_and_packs_back_to_the_same_bytes_or_is_refused(tmp_path, app):
    tree = _scratch(tmp_path, app)
    folder = tree / "blueprints" / app
    original = (folder / pack.COMPOSE).read_bytes()
    if app in OUT_OF_FORM:
        with pytest.raises(pack.PackError, match=OUT_OF_FORM[app]):
            pack.unpack(app, tree)
        assert [p.name for p in folder.iterdir()] == [pack.COMPOSE]
        return
    written = pack.unpack(app, tree)
    assert folder / pack.SERVICES in written
    assert pack.pack_all(tree) == []
    (folder / pack.COMPOSE).unlink()
    assert pack.pack_all(tree) == [folder / pack.COMPOSE]
    assert (folder / pack.COMPOSE).read_bytes() == original
    assert pack.pack_all(tree) == []
    with pytest.raises(pack.PackError, match="already held as neat files"):
        pack.unpack(app, tree)


def test_the_refusal_list_names_apps_of_the_catalog():
    assert set(OUT_OF_FORM) <= set(APPS)


@pytest.mark.parametrize("app", ["element", "wordpress"])
def test_an_empty_line_after_the_block_is_all_element_and_wordpress_lack(tmp_path, app):
    text = _gap((CATALOG / app / pack.COMPOSE).read_text())
    tree = _scratch(tmp_path, app, text)
    pack.unpack(app, tree)
    (tree / "blueprints" / app / pack.COMPOSE).unlink()
    pack.pack_all(tree)
    assert (tree / "blueprints" / app / pack.COMPOSE).read_text() == text


def test_a_comment_above_a_key_travels_with_it(tmp_path):
    tree = _scratch(tmp_path, "actualbudget")
    pack.unpack("actualbudget", tree)
    folder = tree / "blueprints" / "actualbudget"
    lines = (folder / "lifecycle.yml").read_text().split("\n")
    at = lines.index("hooks:")
    assert lines[at - 2].startswith("# verify asks the running server")
    assert lines[at - 1].startswith("# that holds the server stopped")
    assert "      - {file: hooks/verify.js}" in lines
    assert (folder / "hooks" / "verify.js").read_text().startswith("const fs = require('fs');\n")


def test_the_anchors_of_element_stay_in_services(tmp_path):
    tree = _scratch(tmp_path, "element", _gap((CATALOG / "element" / pack.COMPOSE).read_text()))
    pack.unpack("element", tree)
    services = (tree / "blueprints" / "element" / pack.SERVICES).read_text()
    assert services.startswith("x-synapse-image:")
    assert "services:" in services


def test_erpnext_holds_a_file_per_hook(tmp_path):
    tree = _scratch(tmp_path, "erpnext")
    pack.unpack("erpnext", tree)
    hooks = sorted(p.name for p in (tree / "blueprints" / "erpnext" / "hooks").iterdir())
    assert hooks == ["users.sh", "verify.sh"]


def test_a_php_script_is_the_code_php_r_runs(tmp_path):
    tree = _scratch(tmp_path, "espocrm")
    pack.unpack("espocrm", tree)
    script = (tree / "blueprints" / "espocrm" / "hooks" / "verify.php").read_text()
    assert not script.startswith("<?php")


def test_sections_are_the_block_schema_properties():
    properties = set(json.loads((ROOT / "block.schema.json").read_text())["properties"])
    held = {key for keys in pack.SECTIONS.values() for key in keys}
    assert held | {"schema"} == properties
    assert sorted(pack.ORDER) == sorted(properties)
    assert len(held) == sum(len(keys) for keys in pack.SECTIONS.values())


# pack

def test_neat_files_pack_into_the_block_then_one_empty_line_then_services():
    assert _pack() == PACKED


def test_block_keys_follow_the_order_across_files():
    access = "privileges:\n  - host_network\n"
    packed = _pack({"access.yml": access})
    assert packed.index("  privileges:") < packed.index("  env:") < packed.index("  hooks:")


def test_a_comment_and_blank_lines_above_a_key_move_with_it():
    packed = _pack({"settings.yml": "# first\n\nenv:\n  A:\n    managed: {expr: x}\n",
                    "lifecycle.yml": "# note\nhooks:\n  service: web\n  verify:\n"
                                     "    - - sh\n      - -c\n      - {file: hooks/verify.sh}\n"})
    assert "  # first\n\n  env:" in packed
    assert "  # note\n  hooks:" in packed


def test_dollars_in_a_hook_file_are_written_twice():
    packed = _pack(hooks={"hooks/verify.sh": "echo $HOME $$\n"})
    assert "echo $$HOME $$$$\n" in packed


def test_a_blank_line_of_a_hook_stays_empty():
    packed = _pack(hooks={"hooks/verify.sh": "a\n\nb\n"})
    assert "          a\n\n          b\n" in packed


def test_a_second_script_of_a_hook_is_named_with_its_number():
    lifecycle = LIFECYCLE + "    - - sh\n      - -c\n      - {file: hooks/verify-2.sh}\n"
    packed = _pack({"lifecycle.yml": lifecycle}, {"hooks/verify-2.sh": "echo two\n"})
    assert packed.count("- |") == 2


def test_a_folder_with_no_services_file_is_left_alone(tmp_path):
    folder = tmp_path / "blueprints" / "web"
    folder.mkdir(parents=True)
    (folder / pack.COMPOSE).write_text("hand edited\n")
    (folder / "settings.yml").write_text(SETTINGS)
    assert pack.pack_all(tmp_path) == []
    assert (folder / pack.COMPOSE).read_text() == "hand edited\n"


def test_pack_all_writes_the_compose_once(tmp_path):
    folder = tmp_path / "blueprints" / "web"
    (folder / "hooks").mkdir(parents=True)
    for name, text in NEAT.items():
        (folder / name).write_text(text)
    (folder / "hooks" / "verify.sh").write_text("echo ok\n")
    assert pack.pack_all(tmp_path) == [folder / pack.COMPOSE]
    assert (folder / pack.COMPOSE).read_text() == PACKED
    assert pack.pack_all(tmp_path) == []


@pytest.mark.parametrize("neat,hooks,reason", [
    ({"settings.yml": "bogus:\n  a: 1\n"}, None, "bogus is not a key of the block"),
    ({"settings.yml": "schema: 2\n" + SETTINGS}, None, "pack writes schema"),
    ({"settings.yml": "quiesce:\n  service: web\n"}, None, "quiesce belongs in lifecycle.yml"),
    ({"settings.yml": "secrets: {}\n" + SETTINGS}, None, "env is out of order"),
    ({"settings.yml": SETTINGS + SETTINGS}, None, "env is already held"),
    ({"settings.yml": SETTINGS + "# last\n"}, None, "ends with an empty or comment line"),
    ({"settings.yml": "text\n" + SETTINGS}, None, "text before the first key"),
    ({"settings.yml": "# only\n"}, None, "ends with an empty or comment line"),
    ({"settings.yml": "text\n"}, None, "holds no key"),
    ({"settings.yml": "env: {}"}, None, "does not end with a newline"),
    ({pack.SERVICES: "\nservices: {}\n"}, None, "starts with an empty line"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: [a/1]}")}, None, "only"),
    ({"lifecycle.yml": LIFECYCLE.replace("hooks/verify.sh", "hooks/other.sh")}, None, "does not exist"),
    ({"lifecycle.yml": LIFECYCLE.replace("hooks/verify.sh", "hooks/configure.sh")},
     {"hooks/configure.sh": "x\n"}, "not named after its place"),
    ({"lifecycle.yml": LIFECYCLE.replace("hooks/verify.sh", "hooks/verify.js")},
     {"hooks/verify.js": "x\n"}, "not named after its place"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "|\n        echo ok")}, None,
     "is inline; move it to hooks/verify.sh"),
    (None, {"hooks/other.sh": "x\n"}, "no hook of lifecycle.yml names this file"),
    (None, {"hooks/verify.sh": "echo ok"}, "exactly one newline"),
    (None, {"hooks/verify.sh": "echo ok\n\n"}, "exactly one newline"),
    (None, {"hooks/verify.sh": "a\n  \nb\n"}, "a line of spaces only"),
    (None, {"hooks/verify.sh": " a\n"}, "first line starts with a space"),
    ({"lifecycle.yml": "hooks:\n  service: web\n  verify:\n    - {file: hooks/verify.sh}\n"}, None,
     "not the last item"),
])
def test_neat_files_out_of_normal_form_are_refused(neat, hooks, reason):
    with pytest.raises(pack.PackError, match=reason):
        _pack(neat, hooks)


def test_a_refusal_names_the_file_and_the_line():
    with pytest.raises(pack.PackError) as caught:
        _pack({"settings.yml": "# c\n" + "bogus:\n  a: 1\n"})
    assert str(caught.value).startswith("blueprints/web/settings.yml:2:")


def test_a_missing_services_file_is_refused():
    with pytest.raises(pack.PackError, match="services.yml: missing"):
        pack.pack_texts({"settings.yml": SETTINGS}, {})


# unpack

def _unpack(tmp_path, text):
    tree = _scratch(tmp_path, "web", text)
    pack.unpack("web", tree)
    return tree / "blueprints" / "web"


def test_a_packed_compose_unpacks_into_the_files_that_pack_it(tmp_path):
    folder = _unpack(tmp_path, PACKED)
    assert (folder / "settings.yml").read_text() == SETTINGS
    assert (folder / "lifecycle.yml").read_text() == LIFECYCLE
    assert (folder / "hooks" / "verify.sh").read_text() == "echo ok\n"
    assert (folder / pack.SERVICES).read_text() == SERVICES
    assert not (folder / "access.yml").exists()


def test_dollar_pairs_of_a_script_are_written_once_in_its_file(tmp_path):
    folder = _unpack(tmp_path, PACKED.replace("echo ok", "echo $$HOME $$$$"))
    assert (folder / "hooks" / "verify.sh").read_text() == "echo $HOME $$\n"


def test_comments_above_services_stay_in_the_services_file(tmp_path):
    folder = _unpack(tmp_path, PACKED.replace("\nservices:", "\n# the stack\nservices:"))
    assert (folder / pack.SERVICES).read_text() == "# the stack\n" + SERVICES


@pytest.mark.parametrize("text,reason", [
    ("# header\n" + PACKED, "the block is not first"),
    (PACKED.replace("  env:", "  bogus:"), "bogus is not a key of the block"),
    (PACKED.replace("\n\nservices:", "\nservices:"), "followed by exactly one empty line"),
    (PACKED.replace("\n\nservices:", "\n\n\nservices:"), "followed by exactly one empty line"),
    (PACKED.replace("echo ok", "echo $HOME"), "an odd run of"),
    (PACKED.replace("echo ok\n", "echo ok\n            \n          echo two\n"), "a line of spaces only"),
    (PACKED.replace("echo ok", "echo ok\r"), "carriage return"),
    (PACKED.rstrip("\n"), "does not end with a newline"),
])
def test_a_compose_out_of_normal_form_is_refused_and_nothing_is_written(tmp_path, text, reason):
    tree = _scratch(tmp_path, "web", text)
    with pytest.raises(pack.PackError, match=reason):
        pack.unpack("web", tree)
    assert [p.name for p in (tree / "blueprints" / "web").iterdir()] == [pack.COMPOSE]


def test_block_keys_out_of_order_are_refused_with_the_order(tmp_path):
    swapped = ("x-catena:\n  schema: 2\n  hooks:\n    service: web\n    verify:\n      - - sh\n        - -c\n"
               "        - |\n          echo ok\n  env:\n    A:\n      managed: {expr: x}\n\n" + SERVICES)
    tree = _scratch(tmp_path, "web", swapped)
    with pytest.raises(pack.PackError, match="env is out of order; it follows hooks"):
        pack.unpack("web", tree)


def test_an_app_already_held_as_neat_files_is_refused(tmp_path):
    folder = _unpack(tmp_path, PACKED)
    with pytest.raises(pack.PackError, match="already held as neat files"):
        pack.unpack("web", folder.parent.parent)


def test_a_second_script_of_a_hook_unpacks_into_a_numbered_file(tmp_path):
    packed = PACKED.replace("          echo ok\n", "          echo ok\n"
                            "      - - bash\n        - -c\n        - |\n          echo two\n")
    folder = _unpack(tmp_path, packed)
    assert (folder / "hooks" / "verify-2.sh").read_text() == "echo two\n"
    assert pack.pack_texts(*pack._read_neat(folder)) == packed


def test_a_script_with_another_interpreter_flag_or_style_stays_in_the_compose(tmp_path):
    kept = PACKED.replace("- |\n          echo ok\n", "- |-\n          echo ok\n")
    folder = _unpack(tmp_path, kept)
    assert not (folder / "hooks").exists()
    assert "|-" in (folder / "lifecycle.yml").read_text()


# the command line

def test_the_command_unpacks_an_app_of_the_tree(tmp_path, monkeypatch, capsys):
    tree = _scratch(tmp_path, "web", PACKED)
    monkeypatch.setattr(pack, "BLUEPRINTS", tree / "blueprints")
    assert pack.main(["unpack", "web"]) == 0
    assert str(tree / "blueprints" / "web" / pack.SERVICES) in capsys.readouterr().out
    assert pack.main(["unpack", "web"]) == 1
    assert "already held" in capsys.readouterr().err
    assert pack.main(["unpack", "../web"]) == 1
    assert "not a plain app id" in capsys.readouterr().err
