"""lib/pack.py: neat files pack into the compose and a compose unpacks into them."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import changes, pack, versions  # noqa: E402

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


def _pack(neat=None, hooks=None, snippets=None):
    return pack.pack_texts({**NEAT, **(neat or {})}, {**HOOKS, **(hooks or {})}, "blueprints/web/", snippets)


def _hook(item, hook="verify", interpreter="sh"):
    """A lifecycle.yml whose hook holds one script written as `item`."""
    return f"hooks:\n  service: web\n  {hook}:\n    - - {interpreter}\n      - -c\n      - {item}\n"


def _tree(tmp_path, apps, snippets):
    """A scratch tree of apps held as neat files (each `lifecycle.yml` text by app id) and
    the snippet files; the tree."""
    for app, lifecycle in apps.items():
        folder = tmp_path / "blueprints" / app
        (folder / "hooks").mkdir(parents=True)
        for name, text in {**NEAT, "lifecycle.yml": lifecycle}.items():
            (folder / name).write_text(text)
        (folder / "hooks" / "verify.sh").write_text("echo ok\n")
    for path, text in snippets.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return tmp_path


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
    assert pack.pack_all(tmp_path) == []
    assert (folder / pack.COMPOSE).read_text() == "hand edited\n"


@pytest.mark.parametrize("name", ["settings.yml", "access.yml", "lifecycle.yml", "hooks"])
def test_a_section_file_or_hooks_folder_without_a_services_file_is_refused(tmp_path, name):
    folder = tmp_path / "blueprints" / "web"
    folder.mkdir(parents=True)
    (folder / pack.COMPOSE).write_text("hand edited\n")
    if name == "hooks":
        (folder / name).mkdir()
    else:
        (folder / name).write_text(SETTINGS)
    with pytest.raises(pack.PackError, match=rf"blueprints/web/{name}: the folder has no services.yml"):
        pack.pack_all(tmp_path)


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
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: a/1}")}, None, "only"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: [a/1], file: hooks/verify.sh}")},
     None, "only"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: []}")}, None, "is not <snippet>/<n>"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: [a/0]}")}, None,
     "is not <snippet>/<n>"),
    ({"lifecycle.yml": LIFECYCLE.replace("{file: hooks/verify.sh}", "{include: [a/1]}")}, None,
     "the snippet a/1 does not exist"),
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
    (None, {"hooks/verify.sh": "\n a\n"}, r"verify.sh:2: the first line starts with a space"),
    (None, {"hooks/verify.sh": "\n"}, "holds no text"),
    ({"settings.yml": '"env":\n  A: 1\n'}, None, r"settings.yml:1: the key env is not written plainly"),
    ({"settings.yml": "env:\n  A: 1\n'secrets': {}\n"}, None,
     r"settings.yml:3: the key secrets is not written plainly"),
    ({"settings.yml": "env:\n  A: 1\nsecrets: {}\n? [x]\n: y\n"}, None, "settings.yml:4: the key"),
    ({"settings.yml": "env: 'a\nsecrets: {}\n'\n"}, None, r"settings.yml:2: this line is not a top-level key"),
    ({pack.SERVICES: "x-catena: {}\n" + SERVICES}, None, r"services.yml:1: x-catena is a key of the block"),
    ({pack.SERVICES: SERVICES + "env: {}\n"}, None, r"services.yml:4: env is a key of the block"),
    ({pack.SERVICES: SERVICES + "hooks: {}\n"}, None, r"services.yml:4: hooks is a key of the block"),
    ({pack.SERVICES: "services: [\n"}, None, "services.yml: not YAML"),
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


# snippets

SNIPPETS = {"snippets/wait/1.sh": "wait one\n", "snippets/sign/1.sh": "sign $ONE\n"}
BOTH = _hook("{file: hooks/verify.sh, include: [wait/1, sign/1]}")
ONLY = _hook("{include: [wait/1, sign/1]}")


def _packed_script(*lines, interpreter="sh"):
    body = "".join(f"          {line}\n" for line in lines)
    return ("x-catena:\n  schema: 2\n"
            f"  hooks:\n    service: web\n    verify:\n      - - {interpreter}\n        - -c\n        - |\n"
            f"{body}\n" + SERVICES)


def test_snippets_come_before_the_file_in_the_order_of_the_include():
    packed = _pack({"lifecycle.yml": BOTH}, snippets=SNIPPETS)
    assert "          wait one\n          sign $$ONE\n          echo ok\n" in packed


def test_an_include_alone_is_the_snippets_text_and_reads_no_hook_file():
    packed = pack.pack_texts({**NEAT, "lifecycle.yml": ONLY}, {}, "blueprints/web/", SNIPPETS)
    assert "        - |\n          wait one\n          sign $$ONE\n\n" in packed


def test_a_snippet_on_a_hook_of_two_scripts_counts_in_the_name_of_the_second():
    lifecycle = ONLY + "    - - sh\n      - -c\n      - {file: hooks/verify-2.sh}\n"
    packed = pack.pack_texts({**NEAT, "lifecycle.yml": lifecycle}, {"hooks/verify-2.sh": "two\n"},
                             "blueprints/web/", SNIPPETS)
    assert packed.count("- |") == 2
    with pytest.raises(pack.PackError, match="verify.sh is not named after its place"):
        pack.pack_texts({**NEAT, "lifecycle.yml": lifecycle.replace("verify-2", "verify")},
                        {"hooks/verify.sh": "two\n"}, "blueprints/web/", SNIPPETS)


@pytest.mark.parametrize("lifecycle,snippets,reason", [
    (_hook("{file: hooks/verify.sh, include: [wait/2]}"), SNIPPETS, "the snippet wait/2 does not exist"),
    (_hook("{file: hooks/verify.sh, include: [wait/1]}"), {"snippets/wait/1.py": "x\n"}, "wait/1.py is not a .sh"),
    (_hook("{file: hooks/verify.sh, include: [wait/1]}", interpreter="node"), SNIPPETS,
     "is not named after its place"),
    (_hook("{include: [wait/1]}", interpreter="node"), SNIPPETS, r"wait/1.sh is not a \.js file"),
    (_hook("{file: hooks/verify.sh, include: [wait/1]}"), {"snippets/wait/1.sh": "wait"}, "exactly one newline"),
    (_hook("{file: hooks/verify.sh, include: [wait/1]}"), {"snippets/wait/1.sh": " wait\n"},
     "first line starts with a space"),
    (_hook("{file: hooks/verify.sh}"), {"snippets/wait.sh": "x\n"}, "not a snippet file"),
    (_hook("{file: hooks/verify.sh}"), {"snippets/wait/0.sh": "x\n"}, "not a snippet file"),
    (_hook("{file: hooks/verify.sh}"), {"snippets/wait/1.txt": "x\n"}, "not a snippet file"),
    (_hook("{file: hooks/verify.sh}"), {"snippets/wait/1.sh": "x\n", "snippets/wait/1.py": "x\n"},
     "wait/1 is already held in snippets/wait/1.py"),
    (_hook("{file: hooks/verify.sh}"), {"snippets/wait/1.sh": "x\n", "snippets/wait/2.sh": "x\n"},
     "holds the text of snippets/wait/1.sh"),
])
def test_an_include_of_a_missing_or_misnamed_snippet_is_refused(lifecycle, snippets, reason):
    with pytest.raises(pack.PackError, match=reason):
        _pack({"lifecycle.yml": lifecycle}, snippets=snippets)


def test_the_same_text_in_two_snippets_is_two_snippets():
    snippets = {"snippets/wait/1.sh": "x\n", "snippets/sign/1.sh": "x\n"}
    assert _pack(snippets=snippets) == PACKED


def test_a_snippet_no_app_includes_is_refused_unless_it_is_the_newest_of_its_snippet(tmp_path):
    tree = _tree(tmp_path, {"web": _hook("{file: hooks/verify.sh, include: [wait/2]}")},
                 {"snippets/wait/1.sh": "one\n", "snippets/wait/2.sh": "two\n", "snippets/wait/3.sh": "three\n"})
    with pytest.raises(pack.PackError, match=r"snippets/wait/1.sh: no app includes this version and wait/3 is newer"):
        pack.pack_all(tree)
    assert not (tree / "blueprints" / "web" / pack.COMPOSE).exists()
    (tree / "snippets" / "wait" / "1.sh").unlink()
    assert pack.pack_all(tree) == [tree / "blueprints" / "web" / pack.COMPOSE]


def test_a_new_snippet_nobody_includes_yet_is_allowed(tmp_path):
    tree = _tree(tmp_path, {"web": LIFECYCLE}, {"snippets/frappe-verify/1.sh": "one\n"})
    assert pack.pack_all(tree) == [tree / "blueprints" / "web" / pack.COMPOSE]


def test_an_edited_version_two_apps_include_changes_two_composes_and_fails_scope(tmp_path):
    lifecycle = _hook("{file: hooks/verify.sh, include: [wait/1]}")
    tree = _tree(tmp_path, {"one": lifecycle, "two": lifecycle}, {"snippets/wait/1.sh": "before\n"})
    assert len(pack.pack_all(tree)) == 2
    assert pack.pack_all(tree) == []
    (tree / "snippets" / "wait" / "1.sh").write_text("after\n")
    written = pack.pack_all(tree)
    assert [p.parent.name for p in written] == ["one", "two"]
    paths = [p.relative_to(tree).as_posix() for p in written] + ["snippets/wait/1.sh"]
    assert versions.scope_errors(paths) == ["the change touches 2 apps (one, two); a commit or pull request "
                                            "changes one app"]


def test_an_edited_version_one_app_includes_changes_that_app_alone(tmp_path):
    tree = _tree(tmp_path, {"one": _hook("{file: hooks/verify.sh, include: [wait/1]}"), "two": LIFECYCLE},
                 {"snippets/wait/1.sh": "before\n"})
    pack.pack_all(tree)
    (tree / "snippets" / "wait" / "1.sh").write_text("after\n")
    written = pack.pack_all(tree)
    assert [p.parent.name for p in written] == ["one"]
    assert versions.scope_errors([p.relative_to(tree).as_posix() for p in written] + ["snippets/wait/1.sh"]) == []


def test_the_snippets_folder_belongs_to_no_app():
    assert changes.app_ids(["snippets/wait/1.sh", "snippets/wait/2.sh"]) == []
    assert changes.app_ids(["snippets/wait/1.sh", "blueprints/web/docker-compose.yml"]) == ["web"]
    assert versions.scope_errors(["snippets/wait/1.sh", "blueprints/web/docker-compose.yml"]) == []


def test_a_link_under_snippets_is_refused(tmp_path):
    tree = _tree(tmp_path, {"web": LIFECYCLE}, {})
    (tree / "snippets").mkdir()
    (tree / "snippets" / "other").symlink_to(tree)
    with pytest.raises(pack.PackError, match="snippets/other: a link"):
        pack.pack_all(tree)


# unpack

def _unpack(tmp_path, text, snippets=None):
    tree = _scratch(tmp_path, "web", text)
    for path, body in (snippets or {}).items():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(body)
    pack.unpack("web", tree)
    return tree / "blueprints" / "web"


def test_a_script_that_is_a_snippets_text_unpacks_into_an_include_alone(tmp_path):
    folder = _unpack(tmp_path, _packed_script("wait one"), {"snippets/wait/1.sh": "wait one\n"})
    assert "      - {include: [wait/1]}\n" in (folder / "lifecycle.yml").read_text()
    assert not (folder / "hooks").exists()


def test_a_script_that_begins_with_snippets_unpacks_into_them_then_a_file(tmp_path):
    packed = _packed_script("wait one", "sign $$ONE", "echo $$HOME")
    folder = _unpack(tmp_path, packed, SNIPPETS)
    assert "      - {file: hooks/verify.sh, include: [wait/1, sign/1]}\n" in (folder / "lifecycle.yml").read_text()
    assert (folder / "hooks" / "verify.sh").read_text() == "echo $HOME\n"


def test_the_longest_snippet_that_begins_a_script_is_the_one_included(tmp_path):
    snippets = {"snippets/short/1.sh": "a\n", "snippets/long/1.sh": "a\nb\n", "snippets/mid/1.sh": "a\nb\nc\nd\n"}
    folder = _unpack(tmp_path, _packed_script("a", "b", "c"), snippets)
    assert "- {include: [long/1, " not in (folder / "lifecycle.yml").read_text()
    assert "- {file: hooks/verify.sh, include: [long/1]}" in (folder / "lifecycle.yml").read_text()
    assert (folder / "hooks" / "verify.sh").read_text() == "c\n"


def test_a_snippet_of_another_extension_or_text_is_not_included(tmp_path):
    folder = _unpack(tmp_path, _packed_script("wait one", "x"),
                     {"snippets/wait/1.py": "wait one\n", "snippets/other/1.sh": "wait two\n"})
    assert "include" not in (folder / "lifecycle.yml").read_text()
    assert (folder / "hooks" / "verify.sh").read_text() == "wait one\nx\n"


def test_a_snippet_that_would_leave_a_file_starting_with_a_space_is_not_included(tmp_path):
    folder = _unpack(tmp_path, _packed_script("if x; then", "  y", "fi"), {"snippets/cut/1.sh": "if x; then\n"})
    assert "include" not in (folder / "lifecycle.yml").read_text()


@pytest.mark.parametrize("lifecycle", [BOTH, ONLY, _hook("{file: hooks/verify.sh}")])
def test_pack_then_unpack_and_unpack_then_pack_change_nothing(tmp_path, lifecycle):
    hooks = {} if lifecycle == ONLY else HOOKS
    neat = {**NEAT, "lifecycle.yml": lifecycle}
    text = pack.pack_texts(neat, hooks, "blueprints/web/", SNIPPETS)
    folder = _unpack(tmp_path, text, SNIPPETS)
    assert pack._read_neat(folder) == (neat, hooks)
    assert pack.pack_texts(*pack._read_neat(folder), snippets=SNIPPETS) == text


def test_erpnexts_verify_script_unpacks_into_the_snippet_that_holds_it(tmp_path):
    plain = _scratch(tmp_path / "plain", "erpnext")
    pack.unpack("erpnext", plain)
    script = (plain / "blueprints" / "erpnext" / "hooks" / "verify.sh").read_text()
    tree = _scratch(tmp_path / "shared", "erpnext")
    (tree / "snippets" / "frappe-verify").mkdir(parents=True)
    (tree / "snippets" / "frappe-verify" / "1.sh").write_text(script)
    original = (tree / "blueprints" / "erpnext" / pack.COMPOSE).read_bytes()
    pack.unpack("erpnext", tree)
    folder = tree / "blueprints" / "erpnext"
    assert "- {include: [frappe-verify/1]}" in (folder / "lifecycle.yml").read_text()
    assert not (folder / "hooks" / "verify.sh").exists()
    (folder / pack.COMPOSE).unlink()
    assert pack.pack_all(tree) == [folder / pack.COMPOSE]
    assert (folder / pack.COMPOSE).read_bytes() == original


@pytest.mark.parametrize("app", [a for a in APPS if a not in OUT_OF_FORM])
def test_an_app_round_trips_with_every_script_of_the_catalog_as_a_snippet(tmp_path, app):
    plain = _scratch(tmp_path / "plain", app)
    pack.unpack(app, plain)
    scripts = sorted((plain / "blueprints" / app).glob("hooks/*"))
    tree = _scratch(tmp_path / "shared", app)
    original = (tree / "blueprints" / app / pack.COMPOSE).read_bytes()
    for n, script in enumerate(scripts, 1):
        ext = script.suffix[1:]
        (tree / "snippets" / f"s{n}").mkdir(parents=True, exist_ok=True)
        (tree / "snippets" / f"s{n}" / f"1.{ext}").write_text(script.read_text())
    pack.unpack(app, tree)
    assert len(list((tree / "blueprints" / app).glob("hooks/*"))) == 0
    (tree / "blueprints" / app / pack.COMPOSE).unlink()
    pack.pack_all(tree)
    assert (tree / "blueprints" / app / pack.COMPOSE).read_bytes() == original


def test_a_snippet_no_script_begins_with_stays_unused_at_unpack(tmp_path):
    folder = _unpack(tmp_path, PACKED, {"snippets/wait/1.sh": "wait one\n"})
    assert (folder / "hooks" / "verify.sh").read_text() == "echo ok\n"


def test_a_snippet_file_that_is_not_one_stops_unpack_and_nothing_is_written(tmp_path):
    tree = _scratch(tmp_path, "web", PACKED)
    (tree / "snippets").mkdir()
    (tree / "snippets" / "stray.txt").write_text("x\n")
    with pytest.raises(pack.PackError, match="not a snippet file"):
        pack.unpack("web", tree)
    assert [p.name for p in (tree / "blueprints" / "web").iterdir()] == [pack.COMPOSE]


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
    (PACKED.replace("          echo ok\n", "\n           echo ok\n"), "empty or starts with a space"),
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
