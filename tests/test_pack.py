"""lib/pack.py: neat files pack into the compose and a compose unpacks into them."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import changes, pack, versions  # noqa: E402

CATALOG = ROOT / "blueprints"
APPS = sorted(p.parent.name for p in CATALOG.glob("*/docker-compose.yml"))

# The apps whose compose is out of normal form, each with the reason unpack gives. A
# commit that puts an app in normal form deletes its line.
OUT_OF_FORM = {
}

SERVICES = "services:\n  web:\n    image: web:1.0\n"
SETTINGS = "env:\n  DOMAIN_HOST:\n    managed: {expr: \"x\"}\n"
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


def test_a_comment_above_a_key_travels_with_it(tmp_path):
    tree = _scratch(tmp_path, "actualbudget")
    pack.unpack("actualbudget", tree)
    folder = tree / "blueprints" / "actualbudget"
    lines = (folder / pack.BLOCK_FILE).read_text().split("\n")
    assert lines[0] == "schema: 2"
    at = lines.index("hooks:")
    assert lines[at - 2].startswith("# verify asks the running server")
    assert lines[at - 1].startswith("# that holds the server stopped")
    assert "    - hooks/verify.js" in lines
    assert (folder / "hooks" / "verify.js").read_text().startswith("const fs = require('fs');\n")


def test_the_anchors_of_element_stay_in_services(tmp_path):
    tree = _scratch(tmp_path, "element")
    pack.unpack("element", tree)
    services = (tree / "blueprints" / "element" / pack.SERVICES).read_text()
    assert services.startswith("x-synapse-image:")
    assert "services:" in services
    assert "x-synapse-image" not in (tree / "blueprints" / "element" / pack.BLOCK_FILE).read_text()


def _hook_names(tree, app):
    folder = tree / "blueprints" / app / "hooks"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_erpnext_holds_a_file_per_script_or_the_snippet_that_has_its_text(tmp_path):
    plain = _scratch(tmp_path / "plain", "erpnext")
    pack.unpack("erpnext", plain)
    assert _hook_names(plain, "erpnext") == ["after_update.js", "users.bash", "verify.bash"]
    hooks = plain / "blueprints" / "erpnext" / "hooks"
    tree = _scratch(tmp_path / "shared", "erpnext")
    original = (tree / "blueprints" / "erpnext" / pack.COMPOSE).read_bytes()
    for path, name in (("snippets/frappe-verify/2.bash", "verify.bash"), ("snippets/frappe-wait-ping/1.js", "after_update.js")):
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text((hooks / name).read_text())
    pack.unpack("erpnext", tree)
    assert _hook_names(tree, "erpnext") == ["users.bash"]
    block = (tree / "blueprints" / "erpnext" / pack.BLOCK_FILE).read_text()
    assert "    - snippets/frappe-verify/2.bash\n" in block
    assert "    - snippets/frappe-wait-ping/1.js\n" in block
    (tree / "blueprints" / "erpnext" / pack.COMPOSE).unlink()
    assert pack.pack_all(tree) == [tree / "blueprints" / "erpnext" / pack.COMPOSE]
    assert (tree / "blueprints" / "erpnext" / pack.COMPOSE).read_bytes() == original


def test_a_php_script_is_the_code_php_r_runs(tmp_path):
    tree = _scratch(tmp_path, "espocrm")
    pack.unpack("espocrm", tree)
    script = (tree / "blueprints" / "espocrm" / "hooks" / "verify.php").read_text()
    assert not script.startswith("<?php")


def test_mailserver_holds_bash_python_and_sh_scripts(tmp_path):
    tree = _scratch(tmp_path, "mailserver")
    pack.unpack("mailserver", tree)
    assert _hook_names(tree, "mailserver") == ["configure.sh", "users.py", "verify.bash"]


@pytest.mark.parametrize("app", [a for a in APPS if a not in OUT_OF_FORM])
def test_the_block_file_of_an_app_matches_the_block_schema(tmp_path, app):
    tree = _scratch(tmp_path, app)
    pack.unpack(app, tree)
    block = yaml.safe_load((tree / "blueprints" / app / pack.BLOCK_FILE).read_text())
    jsonschema.Draft202012Validator(json.loads((ROOT / "block.schema.json").read_text())).validate(block)


@pytest.mark.parametrize("app", APPS)
def test_every_catalog_app_holds_its_services_file(app):
    assert (CATALOG / app / pack.SERVICES).is_file()


def test_the_block_order_is_the_block_schema_properties():
    properties = set(json.loads((ROOT / "block.schema.json").read_text())["properties"])
    assert sorted(pack.ORDER) == sorted(properties)



def test_every_file_of_every_app_folder_has_a_class_of_its_own():
    strays = []
    for app in APPS:
        for path in sorted((CATALOG / app).rglob("*")):
            rel = path.relative_to(CATALOG / app).as_posix()
            if path.is_file() and rel not in versions.FILE_CLASSES \
                    and not rel.startswith(f"{pack.HOOKS_DIR}/") and rel != versions.CARD:
                strays.append(f"{app}/{rel}")
    assert strays == []





# pack

def test_a_folder_with_no_services_file_is_left_alone(tmp_path):
    folder = tmp_path / "blueprints" / "web"
    folder.mkdir(parents=True)
    (folder / pack.COMPOSE).write_text("hand edited\n")
    assert pack.pack_all(tmp_path) == []
    assert (folder / pack.COMPOSE).read_text() == "hand edited\n"


@pytest.mark.parametrize("name", [pack.BLOCK_FILE, "hooks"])
def test_the_block_file_or_hooks_folder_without_a_services_file_is_refused(tmp_path, name):
    folder = tmp_path / "blueprints" / "web"
    folder.mkdir(parents=True)
    (folder / pack.COMPOSE).write_text("hand edited\n")
    if name == "hooks":
        (folder / name).mkdir()
    else:
        (folder / name).write_text(BLOCK)
    with pytest.raises(pack.PackError, match=rf"blueprints/web/{name}: the folder has no services.yml"):
        pack.pack_all(tmp_path)


def test_a_missing_services_file_is_refused():
    with pytest.raises(pack.PackError, match="services.yml: missing"):
        pack.pack_texts({pack.BLOCK_FILE: BLOCK}, {})


def test_a_missing_block_file_is_refused():
    with pytest.raises(pack.PackError, match="x-catena.yml: missing"):
        pack.pack_texts({pack.SERVICES: SERVICES}, {})


@pytest.mark.parametrize("services,reason", [
    ("\nservices: {}\n", "starts with an empty line"),
    ("x-catena: {}\n" + SERVICES, r"services.yml:1: x-catena is a key of the block"),
    (SERVICES + "env: {}\n", r"services.yml:4: env is a key of the block"),
    (SERVICES + "hooks: {}\n", r"services.yml:4: hooks is a key of the block"),
    ("services: [\n", "services.yml: not YAML"),
])
def test_a_services_file_out_of_normal_form_is_refused(services, reason):
    with pytest.raises(pack.PackError, match=reason):
        _pack_block(services=services)


def test_the_snippets_folder_belongs_to_no_app():
    assert changes.app_ids(["snippets/wait/1.sh", "snippets/wait/2.sh"]) == []
    assert changes.app_ids(["snippets/wait/1.sh", "blueprints/web/docker-compose.yml"]) == ["web"]
    assert versions.scope_errors(["snippets/wait/1.sh", "blueprints/web/docker-compose.yml"]) == []


def test_a_link_under_snippets_is_refused(tmp_path):
    tree = _block_tree(tmp_path, {"web": BLOCK}, {})
    (tree / "snippets").mkdir()
    (tree / "snippets" / "other").symlink_to(tree)
    with pytest.raises(pack.PackError, match="snippets/other: a link"):
        pack.pack_all(tree)


# the block file

BLOCK = "schema: 2\n" + SETTINGS + "hooks:\n  service: web\n  verify:\n    - hooks/verify.sh\n"
BLOCK_FILES = {pack.SERVICES: SERVICES, pack.BLOCK_FILE: BLOCK}


def _pack_block(block=None, hooks=None, snippets=None, services=SERVICES):
    return pack.pack_texts({pack.SERVICES: services, pack.BLOCK_FILE: BLOCK if block is None else block},
                           HOOKS if hooks is None else hooks, "blueprints/web/", snippets)


def _verify(*items):
    """An x-catena.yml whose verify hook lists `items` (one line each, after the dash)."""
    return "schema: 2\nhooks:\n  service: web\n  verify:\n" + "".join(f"    - {item}\n" for item in items)


def test_the_block_file_packs_into_the_block_then_one_empty_line_then_services():
    assert _pack_block() == PACKED


def test_a_comment_and_blank_lines_in_the_block_file_stay_where_they_are():
    packed = _pack_block(BLOCK.replace("env:", "# first\n\nenv:").replace("hooks:", "# note\nhooks:"))
    assert "  # first\n\n  env:" in packed
    assert "  # note\n  hooks:" in packed


def test_a_command_list_beside_a_script_stays_as_written():
    block = _verify('["bench", "set-maintenance-mode", "off"]', "hooks/verify.sh")
    packed = _pack_block(block)
    assert '      - ["bench", "set-maintenance-mode", "off"]\n      - - sh\n        - -c\n        - |\n' in packed
    assert packed.count("- |") == 1


def test_a_lifecycle_step_holds_a_script_path():
    block = ("schema: 2\nlifecycle:\n  service: web\n  after_update:\n    - [bench, off]\n"
             "    - hooks/after_update.js\n  timeout_seconds: 60\n")
    packed = _pack_block(block, {"hooks/after_update.js": "process.exit(0);\n"})
    assert "      - - node\n        - -e\n        - |\n          process.exit(0);\n" in packed
    assert "    timeout_seconds: 60\n" in packed


@pytest.mark.parametrize("ext,run", [("sh", "sh\n        - -c"), ("bash", "bash\n        - -c"),
                                     ("py", "python3\n        - -c"), ("js", "node\n        - -e"),
                                     ("php", "php\n        - -r")])
def test_the_extension_names_the_interpreter_and_flag(ext, run):
    packed = _pack_block(_verify(f"hooks/verify.{ext}"), {f"hooks/verify.{ext}": "x\n"})
    assert f"      - - {run}\n        - |\n          x\n" in packed


def test_dollars_in_a_script_file_are_written_twice():
    packed = _pack_block(hooks={"hooks/verify.sh": "echo $HOME $$\n"})
    assert "echo $$HOME $$$$\n" in packed


def test_a_blank_line_of_a_script_stays_empty():
    assert "          a\n\n          b\n" in _pack_block(hooks={"hooks/verify.sh": "a\n\nb\n"})


def test_a_second_script_of_a_list_is_named_with_its_number():
    packed = _pack_block(_verify("hooks/verify.sh", "hooks/verify-2.py"), {**HOOKS, "hooks/verify-2.py": "two\n"})
    assert packed.count("- |") == 2


@pytest.mark.parametrize("block,hooks,reason", [
    ("env: {}\nschema: 2\n", None, r"x-catena.yml:1: the first line must be `schema: 2`"),
    (BLOCK + "\n", None, "ends with an empty line"),
    (BLOCK.rstrip("\n"), None, "does not end with a newline"),
    (BLOCK.replace("env:", "bogus:"), None, "bogus is not a key of the block"),
    (BLOCK + "env: {}\n", None, "env is written twice"),
    ("schema: 2\nhooks:\n  service: web\n  verify:\n    - hooks/verify.sh\n" + SETTINGS, None,
     "env is out of order"),
    ("schema: 2\nhooks:\n  service: web\n  verify:\n    - - sh\n      - -c\n      - |\n        echo ok\n", None,
     r"x-catena.yml:7: a block scalar"),
    ("schema: 2\nquiesce:\n  service: web\n  pre:\n    - hooks/verify.sh\n  timeout_seconds: 5\n", None,
     "a script path belongs only in a hook list"),
    ("schema: 2\nlifecycle:\n  service: web\n  ready: [hooks/verify.sh]\n", None,
     "a script path belongs only in a hook list"),
    (_verify("hooks/other.sh"), {"hooks/other.sh": "x\n"}, "not named after its place"),
    (_verify("hooks/verify.sh", "hooks/verify.sh"), None, "hooks/verify.sh is not named after its place"),
    (_verify("hooks/verify.txt"), {"hooks/verify.txt": "x\n"}, "has no extension of sh, bash, py, js, php"),
    (_verify("hooks/verify.sh"), {}, "hooks/verify.sh does not exist"),
    (BLOCK, {**HOOKS, "hooks/other.sh": "x\n"}, "no item of x-catena.yml names this file"),
    (_verify("snippets/wait/1.sh"), None, "the snippet snippets/wait/1.sh does not exist"),
    (_verify("echo"), None, "a command list or the plain path of a script file"),
    (_verify("'hooks/verify.sh'"), None, "a command list or the plain path of a script file"),
    ("schema: 2\nhooks:\n  service: web\n  verify: [hooks/verify.sh]\n", None, "on its own line"),
    (BLOCK, {"hooks/verify.sh": "echo ok"}, "exactly one newline"),
    (BLOCK, {"hooks/verify.sh": "a\n  \nb\n"}, "a line of spaces only"),
    (BLOCK, {"hooks/verify.sh": " a\n"}, "first line starts with a space"),
])
def test_a_block_file_out_of_normal_form_is_refused(block, hooks, reason):
    with pytest.raises(pack.PackError, match=reason):
        _pack_block(block, hooks)


def test_the_block_file_alone_without_services_is_refused(tmp_path):
    folder = tmp_path / "blueprints" / "web"
    folder.mkdir(parents=True)
    (folder / pack.BLOCK_FILE).write_text(BLOCK)
    with pytest.raises(pack.PackError, match=r"x-catena.yml: the folder has no services.yml"):
        pack.pack_all(tmp_path)


def test_a_key_of_the_block_in_the_services_file_is_refused():
    with pytest.raises(pack.PackError, match=r"services.yml:4: env is a key of the block"):
        _pack_block(services=SERVICES + "env: {}\n")


def test_pack_all_packs_a_folder_of_the_block_file(tmp_path):
    tree = _block_tree(tmp_path, {"web": BLOCK}, {})
    folder = tree / "blueprints" / "web"
    assert pack.pack_all(tree) == [folder / pack.COMPOSE]
    assert (folder / pack.COMPOSE).read_text() == PACKED
    assert pack.pack_all(tree) == []


def _block_tree(tmp_path, apps, snippets):
    """A scratch tree of apps held as `services.yml`, `x-catena.yml` and, when the block names
    it, `hooks/verify.sh` (each block text by app id), and the snippet files; the tree."""
    for app, block in apps.items():
        folder = tmp_path / "blueprints" / app
        folder.mkdir(parents=True)
        for name, text in {**BLOCK_FILES, pack.BLOCK_FILE: block}.items():
            (folder / name).write_text(text)
        if "hooks/verify.sh" in block:
            (folder / "hooks").mkdir()
            (folder / "hooks" / "verify.sh").write_text("echo ok\n")
    for path, text in snippets.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return tmp_path


# snippets by path

PATHS = {"snippets/wait/1.sh": "wait one\n", "snippets/sign/1.bash": "sign $ONE\n"}


def test_a_snippet_path_is_the_text_of_its_file_run_by_its_extension():
    packed = _pack_block(_verify("snippets/wait/1.sh", "snippets/sign/1.bash"), {}, PATHS)
    assert ("      - - sh\n        - -c\n        - |\n          wait one\n"
            "      - - bash\n        - -c\n        - |\n          sign $$ONE\n") in packed


def test_a_snippet_counts_in_the_name_of_the_next_script_of_the_list():
    packed = _pack_block(_verify("snippets/wait/1.sh", "hooks/verify-2.sh"), {"hooks/verify-2.sh": "two\n"}, PATHS)
    assert packed.count("- |") == 2
    with pytest.raises(pack.PackError, match="hooks/verify.sh is not named after its place"):
        _pack_block(_verify("snippets/wait/1.sh", "hooks/verify.sh"), HOOKS, PATHS)


def test_two_versions_with_one_text_and_two_extensions_are_allowed_and_with_one_extension_refused():
    assert _pack_block(snippets={"snippets/wait/1.sh": "x\n", "snippets/wait/2.bash": "x\n"}) == PACKED
    with pytest.raises(pack.PackError, match="holds the text of snippets/wait/1.sh"):
        _pack_block(snippets={"snippets/wait/1.sh": "x\n", "snippets/wait/2.sh": "x\n"})


def test_an_older_snippet_version_no_app_names_is_refused_unless_it_is_the_newest(tmp_path):
    tree = _block_tree(tmp_path, {"web": _verify("snippets/wait/2.sh")},
                       {"snippets/wait/1.sh": "one\n", "snippets/wait/2.sh": "two\n", "snippets/wait/3.sh": "three\n"})
    with pytest.raises(pack.PackError, match=r"snippets/wait/1.sh: no app includes this version and wait/3 is newer"):
        pack.pack_all(tree)
    (tree / "snippets" / "wait" / "1.sh").unlink()
    assert pack.pack_all(tree) == [tree / "blueprints" / "web" / pack.COMPOSE]


def test_an_edited_snippet_two_apps_name_changes_two_composes_and_fails_scope(tmp_path):
    block = _verify("snippets/wait/1.sh")
    tree = _block_tree(tmp_path, {"one": block, "two": block}, {"snippets/wait/1.sh": "before\n"})
    assert len(pack.pack_all(tree)) == 2
    (tree / "snippets" / "wait" / "1.sh").write_text("after\n")
    written = pack.pack_all(tree)
    paths = [p.relative_to(tree).as_posix() for p in written] + ["snippets/wait/1.sh"]
    assert versions.scope_errors(paths) == ["the change touches 2 apps (one, two); a commit or pull request "
                                            "changes one app"]


# unpack

def _unpack(tmp_path, text, snippets=None):
    tree = _scratch(tmp_path, "web", text)
    for path, body in (snippets or {}).items():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(body)
    pack.unpack("web", tree)
    return tree / "blueprints" / "web"


def _packed_script(*lines, interpreter="sh", flag="-c"):
    body = "".join(f"          {line}\n" for line in lines)
    return ("x-catena:\n  schema: 2\n"
            f"  hooks:\n    service: web\n    verify:\n      - - {interpreter}\n        - {flag}\n        - |\n"
            f"{body}\n" + SERVICES)


def test_a_packed_compose_unpacks_into_the_files_that_pack_it(tmp_path):
    folder = _unpack(tmp_path, PACKED)
    assert (folder / pack.BLOCK_FILE).read_text() == BLOCK
    assert (folder / "hooks" / "verify.sh").read_text() == "echo ok\n"
    assert (folder / pack.SERVICES).read_text() == SERVICES
    assert sorted(p.name for p in folder.iterdir()) == ["docker-compose.yml", "hooks", "services.yml", "x-catena.yml"]


def test_a_script_that_is_a_snippets_text_unpacks_into_its_path(tmp_path):
    folder = _unpack(tmp_path, _packed_script("wait one"), {"snippets/wait/1.sh": "wait one\n"})
    assert "    - snippets/wait/1.sh\n" in (folder / pack.BLOCK_FILE).read_text()
    assert not (folder / "hooks").exists()


def test_a_snippet_of_another_extension_or_text_or_a_longer_script_is_not_a_match(tmp_path):
    folder = _unpack(tmp_path, _packed_script("wait one", "x"),
                     {"snippets/wait/1.py": "wait one\n", "snippets/other/1.sh": "wait two\n",
                      "snippets/part/1.sh": "wait one\n"})
    assert "snippets" not in (folder / pack.BLOCK_FILE).read_text()
    assert (folder / "hooks" / "verify.sh").read_text() == "wait one\nx\n"


def test_the_script_of_a_bash_pair_is_a_bash_file(tmp_path):
    folder = _unpack(tmp_path, _packed_script("echo", interpreter="bash"))
    assert (folder / "hooks" / "verify.bash").read_text() == "echo\n"


@pytest.mark.parametrize("block", [BLOCK, _verify("snippets/wait/1.sh"),
                                   _verify('["bench", "off"]', "snippets/wait/1.sh", "hooks/verify-2.py")])
def test_pack_then_unpack_and_unpack_then_pack_change_nothing(tmp_path, block):
    hooks = {"hooks/verify.sh": "echo ok\n", "hooks/verify-2.py": "print(1)\n"}
    hooks = {k: v for k, v in hooks.items() if k in block}
    text = _pack_block(block, hooks, PATHS)
    folder = _unpack(tmp_path, text, PATHS)
    neat, files = pack._read_neat(folder)
    assert neat == {pack.SERVICES: SERVICES, pack.BLOCK_FILE: block}
    assert files == hooks
    assert pack.pack_texts(neat, files, snippets=PATHS) == text


@pytest.mark.parametrize("app", [a for a in APPS if a not in OUT_OF_FORM])
def test_an_app_round_trips_with_every_script_of_the_catalog_as_a_snippet(tmp_path, app):
    plain = _scratch(tmp_path / "plain", app)
    pack.unpack(app, plain)
    scripts = sorted((plain / "blueprints" / app).glob("hooks/*"))
    tree = _scratch(tmp_path / "shared", app)
    original = (tree / "blueprints" / app / pack.COMPOSE).read_bytes()
    for n, script in enumerate(scripts, 1):
        (tree / "snippets" / f"s{n}").mkdir(parents=True, exist_ok=True)
        (tree / "snippets" / f"s{n}" / f"1{script.suffix}").write_text(script.read_text())
    pack.unpack(app, tree)
    assert len(list((tree / "blueprints" / app).glob("hooks/*"))) == 0
    (tree / "blueprints" / app / pack.COMPOSE).unlink()
    pack.pack_all(tree)
    assert (tree / "blueprints" / app / pack.COMPOSE).read_bytes() == original


def test_a_snippet_file_that_is_not_one_stops_unpack_and_nothing_is_written(tmp_path):
    tree = _scratch(tmp_path, "web", PACKED)
    (tree / "snippets").mkdir()
    (tree / "snippets" / "stray.txt").write_text("x\n")
    with pytest.raises(pack.PackError, match="not a snippet file"):
        pack.unpack("web", tree)
    assert [p.name for p in (tree / "blueprints" / "web").iterdir()] == [pack.COMPOSE]


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
    (_packed_script("print(1)", interpreter="python"), "python -c has no script file; write python3"),
    (PACKED.replace("- |\n          echo ok\n", "- |-\n          echo ok\n"), "is not written as"),
    (PACKED.replace("- -c", "- '-c'"), "is not written as"),
    (PACKED.replace("- sh", "- zsh"), "a block scalar outside a script"),
    (PACKED.replace("  env:\n    DOMAIN_HOST:\n      managed: {expr: \"x\"}\n",
                    "  quiesce:\n    service: web\n    pre:\n      - - sh\n        - -c\n        - |\n          x\n"),
     "a block scalar outside a script"),
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
    assert (folder / "hooks" / "verify-2.bash").read_text() == "echo two\n"
    assert pack.pack_texts(*pack._read_neat(folder)) == packed


def test_a_one_line_script_and_a_command_list_stay_in_the_block_file(tmp_path):
    packed = PACKED.replace("      - - sh\n        - -c\n        - |\n          echo ok\n",
                            "      - [sh, -c, \"exit 0\"]\n      - [bench, off]\n")
    folder = _unpack(tmp_path, packed)
    assert not (folder / "hooks").exists()
    assert '    - [sh, -c, "exit 0"]\n    - [bench, off]\n' in (folder / pack.BLOCK_FILE).read_text()


# the command line

def test_the_command_unpacks_an_app_of_the_tree(tmp_path, monkeypatch, capsys):
    tree = _scratch(tmp_path, "web", PACKED)
    monkeypatch.setattr(pack, "BLUEPRINTS", tree / "blueprints")
    assert pack.main(["unpack", "web"]) == 0
    assert str(tree / "blueprints" / "web" / pack.BLOCK_FILE) in capsys.readouterr().out
    assert pack.main(["unpack", "web"]) == 1
    assert "already held" in capsys.readouterr().err
    assert pack.main(["unpack", "../web"]) == 1
    assert "not a plain app id" in capsys.readouterr().err


# review fixes

@pytest.mark.parametrize("char,name", [("\x85", "U+0085"), (" ", "U+2028"), (" ", "U+2029"),
                                       ("\r", "carriage return")])
def test_a_line_break_other_than_a_line_feed_is_refused_in_every_file_with_its_line(char, name):
    name = re.escape(name)
    smuggled = f"echo a{char}  privileges:{char}    host_network: true\n"
    with pytest.raises(pack.PackError, match=rf"blueprints/web/hooks/verify.sh:1: holds .*{name}"):
        _pack_block(hooks={"hooks/verify.sh": smuggled})
    with pytest.raises(pack.PackError, match=rf"x-catena.yml:9: holds .*{name}"):
        _pack_block(BLOCK + f"data: {{}}{char}privileges: [host_network]\n")
    with pytest.raises(pack.PackError, match=rf"services.yml:4: holds .*{name}"):
        _pack_block(services=SERVICES + f"x-a: 1{char}x-b: 2\n")
    with pytest.raises(pack.PackError, match=rf"snippets/wait/1.sh:1: holds .*{name}"):
        _pack_block(_verify("snippets/wait/1.sh"), {}, {"snippets/wait/1.sh": f"a{char}b\n"})


def test_a_line_deeper_than_a_script_path_is_refused_so_it_cannot_join_the_script():
    deep = _verify("hooks/verify.sh").replace("verify.sh\n", "verify.sh\n        # note\n")
    with pytest.raises(pack.PackError, match=r"x-catena.yml:6: a line indented under the script path"):
        _pack_block(deep)
    blank = _verify("hooks/verify.sh").replace("verify.sh\n", "verify.sh\n\n        # rm\n")
    with pytest.raises(pack.PackError, match=r"x-catena.yml:7: a line indented under the script path"):
        _pack_block(blank)


def test_a_comment_level_with_the_dash_after_a_script_path_is_allowed():
    packed = _pack_block(BLOCK + "# end\n")
    assert packed.endswith("  # end\n\n" + SERVICES)


def test_a_quoted_multi_line_script_of_a_script_pair_is_refused_on_pack_and_unpack(tmp_path):
    block = _verify('[sh, -c, "echo a\\necho b"]')
    with pytest.raises(pack.PackError, match=r"x-catena.yml:5: a script of verify with several lines is a file"):
        _pack_block(block)
    compose = ("x-catena:\n  schema: 2\n  hooks:\n    service: web\n    verify:\n"
               '      - [node, -e, "a\\u2028b"]\n\n' + SERVICES)
    tree = _scratch(tmp_path, "web", compose)
    with pytest.raises(pack.PackError, match=r"docker-compose.yml:6: a script of verify with several lines"):
        pack.unpack("web", tree)
    assert [p.name for p in (tree / "blueprints" / "web").iterdir()] == [pack.COMPOSE]


def test_a_one_line_script_of_a_script_pair_and_other_commands_stay_inline():
    packed = _pack_block(_verify('[sh, -c, "exit 0"]', '[sh, -c, "echo a echo b"]'), {})
    assert '      - [sh, -c, "exit 0"]\n      - [sh, -c, "echo a echo b"]\n' in packed


@pytest.mark.parametrize("name", [pack.SERVICES, pack.BLOCK_FILE, "hooks"])
def test_a_link_in_place_of_a_neat_file_or_the_hooks_folder_is_refused(tmp_path, name):
    tree = _block_tree(tmp_path / "tree", {"web": BLOCK}, {})
    folder = tree / "blueprints" / "web"
    target = tmp_path / "linked"
    target.mkdir() if name == "hooks" else target.write_text(SETTINGS)
    if (folder / name).is_dir():
        shutil.rmtree(folder / name)
    else:
        (folder / name).unlink(missing_ok=True)
    (folder / name).symlink_to(target)
    with pytest.raises(pack.PackError, match=rf"blueprints/web/{name}: a link"):
        pack.pack_all(tree)
