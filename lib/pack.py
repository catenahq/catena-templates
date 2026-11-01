"""Pack an app held as neat files into its `docker-compose.yml`, and unpack one.

An app is held as neat files when `blueprints/<id>/` holds `services.yml`:

  services.yml    every top-level key after the block (services, volumes, networks,
                  x-<name>), as written
  x-catena.yml    every key of the block, `schema: 2` first, in `ORDER`, as written
  hooks/<place>[-<n>].<ext>
                  one multi-line script per file, each `$` written once

and, shared by apps and belonging to none, at the root of the tree:

  snippets/<name>/<n>.<ext>
                  a script several apps run, one file per version, each `$` written once

`pack` writes `x-catena:`, then every line of `x-catena.yml` indented two spaces, one
empty line and `services.yml` as it is. An item of a hook list or of the lifecycle
`before_update`, `migrate` or `after_update` that is the plain path of a script file
(`- hooks/verify.js`, `- snippets/frappe-verify/2.bash`) becomes
`- - <interpreter>`, `  - <flag>`, `  - |` and the file's lines, each indented four past
the dash and each `$` written `$$`; RUNS gives the interpreter and flag of an extension.
A hook file is named after its hook or step and its ordinal in the list (`verify.js`,
`verify-2.js`). A snippet version no app names is refused unless it is the newest of its
snippet, and so are two versions of a snippet with one extension and one text. Everything
else in a list, one-line commands included, stays argv as written. The packed file is
what hosts, the bench and every check read; the render commits it beside the neat files.

A folder without `x-catena.yml` holds the block in the section files `settings.yml`,
`access.yml` and `lifecycle.yml` instead, with hook items written `- {file: hooks/<name>}`
and `- {include: [<snippet>/<n>, ...]}`; pack reads that layout too.

`unpack` cuts a packed compose into `services.yml`, `x-catena.yml` and the script files,
and writes them only when packing them gives the compose back byte for byte. A script
whose text and extension equal a snippet's is written as that snippet's path. A compose
out of normal form, a script in another form and a block scalar elsewhere in the block
are refused with the change to make. Pack reads text and PyYAML's node positions only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .model import BLUEPRINTS, HOOK_PLACES, STEP_PLACES

COMPOSE = "docker-compose.yml"
BLOCK_KEY = "x-catena"
SERVICES = "services.yml"
HOOKS_DIR = "hooks"
SNIPPETS_DIR = "snippets"

# The block keys each section file holds.
SECTIONS = {
    "settings.yml": ("env", "secrets"),
    "access.yml": ("privileges", "capabilities", "host_paths", "access", "provides", "needs", "egress"),
    "lifecycle.yml": ("requires_catena", "version_from", "lifecycle", "quiesce", "probe",
                      "object_store", "write_hold", "stops", "images", "hooks", "data"),
}
# The order of the block keys in the packed file, `schema` first: x-catena.yml holds it and pack
# writes it for an app held in section files.
ORDER = ("schema", "requires_catena", "version_from", "privileges", "env", "secrets", "access",
         "capabilities", "host_paths", "provides", "lifecycle", "quiesce", "probe", "object_store",
         "needs", "write_hold", "stops", "images", "hooks", "egress", "data")
BLOCK_FILE = "x-catena.yml"
# The files pack reads besides the files under HOOKS_DIR.
NEAT_FILES = (SERVICES, BLOCK_FILE, *SECTIONS)
# The interpreter and flag that run a script file, by its extension, and the reverse.
RUNS = {"sh": ("sh", "-c"), "bash": ("bash", "-c"), "py": ("python3", "-c"), "js": ("node", "-e"),
        "php": ("php", "-r")}
EXTENSIONS = {run: ext for ext, run in RUNS.items()}
# The extension of a hook script file of a section-file layout, by the interpreter that runs it.
INTERPRETERS = {"sh": "sh", "bash": "sh", "python3": "py", "python": "py", "node": "js", "php": "php"}

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?=:(?: |$))")
_ITEM = re.compile(r"(?P<indent> *)- \{(?:file: (?P<file>hooks/[A-Za-z0-9._-]+)"
                   r"(?:, include: \[(?P<both>[^\]]*)\])?|include: \[(?P<only>[^\]]*)\])\}")
_INCLUDE_FORM = re.compile(r"\s*- \{\s*(?:file|include)\s*:")
_REF = re.compile(r"([a-z0-9][a-z0-9-]*)/([1-9][0-9]*)")
_SNIPPET_PATH = re.compile(r"snippets/([a-z0-9][a-z0-9-]*)/([1-9][0-9]*)\.([a-z]+)")
_PATH_TEXT = r"(?:hooks/[A-Za-z0-9._-]+|snippets/[a-z0-9][a-z0-9-]*/[1-9][0-9]*\.[a-z]+)"
_PATH = re.compile(_PATH_TEXT)
_PATH_ITEM = re.compile(r"(?P<indent> *)- (?P<path>" + _PATH_TEXT + ")")
_HEADER = re.compile(r"( *)- \|")
_ODD_DOLLARS = re.compile(r"(?<!\$)(?:\$\$)*\$(?!\$)")
_APP = re.compile(r"[a-z0-9][a-z0-9-]*")
# The characters besides a line feed that YAML parsers read as a line break.
_BREAKS = {"\r": "a carriage return", "\x85": "a next-line character (U+0085)",
           "\u2028": "a line separator (U+2028)", "\u2029": "a paragraph separator (U+2029)"}
_LINE_BREAK = re.compile("[\n" + "".join(_BREAKS) + "]")
_BREAK = re.compile("[" + "".join(_BREAKS) + "]")


class PackError(Exception):
    """A set of neat files or a compose that is not in normal form."""


def _no_other_breaks(text: str, where: str) -> None:
    found = _BREAK.search(text)
    if found:
        raise PackError(f"{where}:{text.count(chr(10), 0, found.start()) + 1}: holds {_BREAKS[found[0]]}; "
                        f"use line feeds")


def _lines(text: str, where: str) -> list[str]:
    _no_other_breaks(text, where)
    if not text.endswith("\n"):
        raise PackError(f"{where}: does not end with a newline")
    return text[:-1].split("\n")


def _top_keys(text: str, where: str) -> list[tuple[str, int]]:
    """(key, index of its line) for each top-level key of the YAML mapping `text`; the
    list is empty for any other kind of document."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise PackError(f"{where}: not YAML: {exc}") from exc
    if not isinstance(root, yaml.MappingNode):
        return []
    return [(str(key.value), key.start_mark.line) for key, _ in root.value]


def _chunks(lines: list[str], where: str) -> list[tuple[str, int, int, list[str]]]:
    """(key, index of its line, index of its text's first line, its text) for each
    top-level key of `lines`."""
    keys = [i for i, line in enumerate(lines) if _KEY.match(line)]
    if keys:
        for i, line in enumerate(lines[:keys[0]]):
            if line and not line.startswith("#"):
                raise PackError(f"{where}:{i + 1}: text before the first key")
    parsed = _top_keys("\n".join(lines) + "\n", where)
    for found, at in parsed:
        if at not in keys:
            raise PackError(f"{where}:{at + 1}: the key {found} is not written plainly as `{found}:` "
                            f"at the start of its line")
    for at in keys:
        if at not in {line for _, line in parsed}:
            raise PackError(f"{where}:{at + 1}: this line is not a top-level key of the file")
    if not keys:
        raise PackError(f"{where}: holds no key")
    starts = []
    for n, k in enumerate(keys):
        start, floor = k, keys[n - 1] + 1 if n else 0
        while start > floor and (lines[start - 1] == "" or lines[start - 1].startswith("#")):
            start -= 1
        starts.append(start)
    ends = starts[1:] + [len(lines)]
    return [(_KEY.match(lines[k]).group(), k, starts[n], lines[starts[n]:ends[n]]) for n, k in enumerate(keys)]


def _hook_lines(name: str, text: str, where: str) -> list[str]:
    _no_other_breaks(text, where)
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise PackError(f"{where}: must end with exactly one newline")
    lines = text[:-1].split("\n")
    for i, line in enumerate(lines):
        if line and not line.strip():
            raise PackError(f"{where}:{i + 1}: a line of spaces only")
    first = next((i for i, line in enumerate(lines) if line), None)
    if first is None:
        raise PackError(f"{where}: holds no text")
    if lines[first].startswith(" "):
        raise PackError(f"{where}:{first + 1}: the first line starts with a space")
    return lines


def _hook_name(hook: str, ordinal: int, ext: str) -> str:
    return f"{HOOKS_DIR}/{hook}{'' if ordinal == 1 else f'-{ordinal}'}.{ext}"


def _scripts(lines: list[str]) -> list[tuple[str, int, str, int]]:
    """(hook, ordinal in the hook, extension, index of the `- |` line) of each script of
    the `hooks` key held in `lines`: the last item of `[<interpreter>, <flag>, <body>]`."""
    try:
        root = yaml.compose("\n".join(lines) + "\n")
    except yaml.YAMLError as exc:
        raise PackError(f"hooks: not YAML: {exc}") from exc
    found = []
    if not isinstance(root, yaml.MappingNode):
        return found
    for key, value in root.value:
        if key.value != "hooks" or not isinstance(value, yaml.MappingNode):
            continue
        for hook, argvs in value.value:
            if not isinstance(argvs, yaml.SequenceNode):
                continue
            ordinal = 0
            for argv in argvs.value:
                if not isinstance(argv, yaml.SequenceNode) or len(argv.value) != 3:
                    continue
                interpreter, flag, body = argv.value
                if not all(isinstance(n, yaml.ScalarNode) for n in argv.value):
                    continue
                ext = INTERPRETERS.get(interpreter.value)
                if ext and flag.value.startswith("-") and body.style == "|" \
                        and _HEADER.fullmatch(lines[body.start_mark.line]):
                    ordinal += 1
                    found.append((hook.value, ordinal, ext, body.start_mark.line))
    return found


class _Snippet:
    """One snippet file: its path, extension, lines and text."""

    def __init__(self, path: str, ext: str, text: str):
        self.path, self.ext, self.text = path, ext, text
        self.lines = _hook_lines(path, text, path)


def _snippet_index(snippets: dict[str, str]) -> dict[str, _Snippet]:
    """The snippet files (`snippets/<name>/<n>.<ext>` to text) by `<name>/<n>`, or the
    reason one is not a snippet."""
    index, texts = {}, {}
    for path in sorted(snippets):
        match = _SNIPPET_PATH.fullmatch(path)
        if not match or match[3] not in RUNS:
            raise PackError(f"{path}: not a snippet file; {SNIPPETS_DIR}/<name>/<n>.<ext> is expected, "
                            f"<n> counting from 1 and <ext> one of {', '.join(RUNS)}")
        ref = f"{match[1]}/{match[2]}"
        if ref in index:
            raise PackError(f"{path}: {ref} is already held in {index[ref].path}")
        index[ref] = _Snippet(path, match[3], snippets[path])
        key = (match[1], match[3], snippets[path])
        if key in texts:
            raise PackError(f"{path}: holds the text of {texts[key]}; a snippet keeps one version per text")
        texts[key] = path
    return index


def _unused_snippets(snippets: dict[str, str], included: set[str]) -> None:
    """Refuse a snippet version no app includes unless it is the newest of its snippet."""
    index = _snippet_index(snippets)
    newest = {}
    for ref in index:
        name, n = ref.split("/")
        newest[name] = max(newest.get(name, 0), int(n))
    for ref, snippet in index.items():
        name, n = ref.split("/")
        if snippet.path not in included and int(n) != newest[name]:
            raise PackError(f"{snippet.path}: no app includes this version and {name}/{newest[name]} is "
                            f"newer; delete it")


def _expand_hooks(chunk: list[str], first: int, hooks: dict[str, str], where: str,
                  index: dict[str, _Snippet], included: set[str]) -> tuple[list[str], list[str]]:
    """The `hooks` text with each `- {file: ...}` and `- {include: ...}` item written out,
    and the files used; the snippet files used join `included`."""
    out, source, placed, used = [], [], {}, []
    for n, line in enumerate(chunk):
        match = _ITEM.fullmatch(line)
        if not match:
            if _INCLUDE_FORM.match(line):
                raise PackError(f"{where}:{first + n + 1}: only `- {{file: hooks/<name>}}`, "
                                f"`- {{include: [<snippet>/<n>, ...]}}` or `- {{file: hooks/<name>, "
                                f"include: [<snippet>/<n>, ...]}}` is read here")
            out.append(line)
            source.append(n)
            continue
        indent, name = match["indent"], match["file"]
        listed = match["both"] if match["both"] is not None else match["only"]
        refs = listed.split(", ") if listed is not None else []
        body = []
        for ref in refs:
            if not _REF.fullmatch(ref):
                raise PackError(f"{where}:{first + n + 1}: {ref!r} is not <snippet>/<n>")
            if ref not in index:
                raise PackError(f"{where}:{first + n + 1}: the snippet {ref} does not exist")
            body += index[ref].lines
            included.add(index[ref].path)
        if name:
            if name not in hooks:
                raise PackError(f"{where}:{first + n + 1}: {name} does not exist")
            body += _hook_lines(name, hooks[name], f"{Path(where).parent / name}")
            used.append(name)
        placed[len(out)] = (name, n, refs)
        out.append(f"{indent}- |")
        source.append(n)
        for text in body:
            out.append(f"{indent}  {text.replace('$', '$$')}" if text else "")
            source.append(n)
    scripts = _scripts(out)
    for hook, ordinal, ext, header in scripts:
        expected = _hook_name(hook, ordinal, ext)
        if header not in placed:
            raise PackError(f"{where}:{first + source[header] + 1}: the script of {hook} is inline; "
                            f"move it to {expected}")
        name, n, refs = placed[header]
        if name and name != expected:
            raise PackError(f"{where}:{first + n + 1}: {name} is not named after its place; "
                            f"this script of {hook} is {expected}")
        for ref in refs:
            if index[ref].ext != ext:
                raise PackError(f"{where}:{first + n + 1}: {index[ref].path} is not a .{ext} file; "
                                f"this script of {hook} is one")
    if len(scripts) != len(placed):
        stray = next(n for h, (name, n, refs) in placed.items() if h not in {s[3] for s in scripts})
        raise PackError(f"{where}:{first + stray + 1}: {chunk[stray].strip()} is not the last item of "
                        f"[interpreter, flag, body] of a hook")
    return out, used


def _scalars(node: yaml.Node):
    """Every scalar node under `node`, keys included."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _scalars(item)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _scalars(key)
            yield from _scalars(value)


def _step_lists(root: yaml.Node) -> list[tuple[str, yaml.SequenceNode]]:
    """(place, list) for each hook list and each lifecycle step list of the block `root`."""
    found = []
    if not isinstance(root, yaml.MappingNode):
        return found
    for key, value in root.value:
        places = {"hooks": HOOK_PLACES, "lifecycle": STEP_PLACES}.get(key.value, ())
        if places and isinstance(value, yaml.MappingNode):
            found += [(place.value, seq) for place, seq in value.value
                      if place.value in places and isinstance(seq, yaml.SequenceNode)]
    return found


def _refuse_inline_script(item: yaml.SequenceNode, place: str, where: str, first: int) -> None:
    """Refuse a command list that is `[<interpreter>, <flag>, <body>]` of a script file's pair
    with a body of several lines: every multi-line script is a file. `first` is the number of
    the file's first line."""
    if len(item.value) == 3 and all(isinstance(n, yaml.ScalarNode) for n in item.value):
        interpreter, flag, body = item.value
        if (interpreter.value, flag.value) in EXTENSIONS and _LINE_BREAK.search(body.value):
            raise PackError(f"{where}:{item.start_mark.line + first}: a script of {place} with several lines "
                            f"is a file; give it as the path "
                            f"{HOOKS_DIR}/{place}.{EXTENSIONS[interpreter.value, flag.value]}")


def _pack_block(text: str, services: list[str], files: dict[str, str], prefix: str,
                index: dict[str, _Snippet], included: set[str]) -> str:
    """The packed compose of the block file `text` and the `services` lines; the snippet
    files used join `included`."""
    where = prefix + BLOCK_FILE
    lines = _lines(text, where)
    if lines[0] != "schema: 2":
        raise PackError(f"{where}:1: the first line must be `schema: 2`")
    if lines[-1] == "":
        raise PackError(f"{where}:{len(lines)}: ends with an empty line")
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise PackError(f"{where}: not YAML: {exc}") from exc
    for node in _scalars(root):
        if node.style in ("|", ">"):
            raise PackError(f"{where}:{node.start_mark.line + 1}: a block scalar; a multi-line script is a "
                            f"file under {HOOKS_DIR}/, named by its path alone")
    previous, seen = -1, set()
    for key, at, _, _ in _chunks(lines, where):
        if key not in ORDER:
            raise PackError(f"{where}:{at + 1}: {key} is not a key of the block")
        if key in seen:
            raise PackError(f"{where}:{at + 1}: {key} is written twice")
        seen.add(key)
        if ORDER.index(key) < previous:
            raise PackError(f"{where}:{at + 1}: {key} is out of order; the order puts it before "
                            f"{ORDER[previous]}: {', '.join(ORDER)}")
        previous = ORDER.index(key)
    by_path = {snippet.path: snippet for snippet in index.values()}
    used, claimed, written = [], set(), {}
    for place, seq in _step_lists(root):
        ordinal = 0
        for item in seq.value:
            if isinstance(item, yaml.SequenceNode):
                _refuse_inline_script(item, place, where, 1)
                continue
            at = item.start_mark.line
            if not isinstance(item, yaml.ScalarNode) or item.style is not None or not _PATH.fullmatch(item.value):
                raise PackError(f"{where}:{at + 1}: an item of {place} is a command list or the plain path "
                                f"of a script file")
            match = _PATH_ITEM.fullmatch(lines[at])
            if not match:
                raise PackError(f"{where}:{at + 1}: write the script path as `- {item.value}` on its own line")
            path, claimed = item.value, claimed | {id(item)}
            ordinal += 1
            for under in range(at + 1, len(lines)):
                if lines[under] and not lines[under].startswith(" " * (len(match["indent"]) + 4)):
                    break
                if lines[under]:
                    raise PackError(f"{where}:{under + 1}: a line indented under the script path {path}; it "
                                    f"would join the script, so write it elsewhere")
            if path.startswith(f"{SNIPPETS_DIR}/"):
                if path not in by_path:
                    raise PackError(f"{where}:{at + 1}: the snippet {path} does not exist")
                snippet = by_path[path]
                ext, body = snippet.ext, snippet.lines
                included.add(path)
            else:
                ext = path.rpartition(".")[2]
                if ext not in RUNS:
                    raise PackError(f"{where}:{at + 1}: {path} has no extension of {', '.join(RUNS)}")
                if path != _hook_name(place, ordinal, ext):
                    raise PackError(f"{where}:{at + 1}: {path} is not named after its place; "
                                    f"this script of {place} is {_hook_name(place, ordinal, ext)}")
                if path not in files:
                    raise PackError(f"{where}:{at + 1}: {path} does not exist")
                body = _hook_lines(path, files[path], f"{prefix}{path}")
                used.append(path)
            interpreter, flag = RUNS[ext]
            indent = match["indent"]
            written[at] = ([f"{indent}- - {interpreter}", f"{indent}  - {flag}", f"{indent}  - |"]
                           + [f"{indent}    {line.replace('$', '$$')}" if line else "" for line in body])
    for node in _scalars(root):
        if _PATH.fullmatch(node.value) and id(node) not in claimed:
            raise PackError(f"{where}:{node.start_mark.line + 1}: a script path belongs only in a hook list "
                            f"or in lifecycle before_update, migrate or after_update")
    for name in sorted(set(files) - set(used)):
        raise PackError(f"{prefix}{name}: no item of {BLOCK_FILE} names this file")
    block = []
    for at, line in enumerate(lines):
        block += written.get(at, [line])
    out = ["x-catena:"] + [f"  {line}" if line else "" for line in block]
    return "\n".join(out + [""] + services) + "\n"


def pack_texts(neat: dict[str, str], hooks: dict[str, str], prefix: str = "",
               snippets: dict[str, str] | None = None, included: set[str] | None = None) -> str:
    """The packed compose of an app's neat files (`neat`: file name to text, `hooks`:
    `hooks/<name>` to text, `snippets`: `snippets/<name>/<n>.<ext>` to text); `prefix`
    opens the paths of error messages; the snippet files the app includes join `included`."""
    index = _snippet_index(snippets or {})
    included = set() if included is None else included
    if SERVICES not in neat:
        raise PackError(f"{prefix}{SERVICES}: missing")
    services = _lines(neat[SERVICES], prefix + SERVICES)
    if not services[0]:
        raise PackError(f"{prefix}{SERVICES}:1: starts with an empty line")
    for found, at in _top_keys(neat[SERVICES], prefix + SERVICES):
        if found == BLOCK_KEY or any(found in keys for keys in SECTIONS.values()):
            raise PackError(f"{prefix}{SERVICES}:{at + 1}: {found} is a key of the block; "
                            f"it belongs in {BLOCK_FILE} or a section file, not here")
    if BLOCK_FILE in neat:
        for name in SECTIONS:
            if name in neat:
                raise PackError(f"{prefix}{name}: the folder holds {BLOCK_FILE}, which holds the whole block")
        return _pack_block(neat[BLOCK_FILE], services, hooks, prefix, index, included)
    home = {key: name for name, keys in SECTIONS.items() for key in keys}
    chunks, used = {}, []
    for name, keys in SECTIONS.items():
        if name not in neat:
            continue
        where = prefix + name
        lines = _lines(neat[name], where)
        if lines[-1] == "" or lines[-1].startswith("#"):
            raise PackError(f"{where}:{len(lines)}: ends with an empty or comment line, which would "
                            f"travel with the key that follows it in the packed file")
        previous = -1
        for key, at, start, chunk in _chunks(lines, where):
            if key == "schema":
                raise PackError(f"{where}:{at + 1}: pack writes schema")
            if key not in ORDER:
                raise PackError(f"{where}:{at + 1}: {key} is not a key of the block")
            if key not in keys:
                raise PackError(f"{where}:{at + 1}: {key} belongs in {home[key]}")
            if key in chunks:
                raise PackError(f"{where}:{at + 1}: {key} is already held in {chunks[key][0]}")
            if ORDER.index(key) < previous:
                raise PackError(f"{where}:{at + 1}: {key} is out of order; the order puts it before "
                                f"{ORDER[previous]}: {', '.join(ORDER)}")
            previous = ORDER.index(key)
            if key == "hooks":
                chunk, files = _expand_hooks(chunk, start, hooks, where, index, included)
                used += files
            chunks[key] = (name, chunk)
    for name in sorted(set(hooks) - set(used)):
        raise PackError(f"{prefix}{name}: no hook of lifecycle.yml names this file")
    out = ["x-catena:", "  schema: 2"]
    for key in sorted(chunks, key=ORDER.index):
        out += [f"  {line}" if line else "" for line in chunks[key][1]]
    return "\n".join(out + [""] + services) + "\n"


def _read(path: Path) -> str:
    """A file's text, line endings untouched."""
    return path.read_bytes().decode("utf-8")


def _read_neat(folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    for name in (*NEAT_FILES, HOOKS_DIR):
        if (folder / name).is_symlink():
            raise PackError(f"blueprints/{folder.name}/{name}: a link")
    neat = {name: _read(folder / name) for name in NEAT_FILES if (folder / name).is_file()}
    hooks = {}
    if (folder / HOOKS_DIR).is_dir():
        for path in sorted((folder / HOOKS_DIR).rglob("*")):
            rel = path.relative_to(folder).as_posix()
            if path.is_symlink():
                raise PackError(f"blueprints/{folder.name}/{rel}: a link")
            if path.is_file():
                hooks[rel] = _read(path)
    return neat, hooks


def _read_snippets(tree: Path) -> dict[str, str]:
    """The files under the tree's `snippets/` folder, by path from the tree."""
    folder = tree / SNIPPETS_DIR
    if folder.is_symlink():
        raise PackError(f"{SNIPPETS_DIR}: a link")
    snippets = {}
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(tree).as_posix()
            if path.is_symlink():
                raise PackError(f"{rel}: a link")
            if path.is_file():
                snippets[rel] = _read(path)
    return snippets


def pack_all(root: Path | None = None) -> list[Path]:
    """Pack every folder holding `services.yml`; the compose files written. `x-catena.yml`, a
    section file or a `hooks/` folder in a folder without `services.yml` is refused, and so is a snippet
    version that no app includes while a newer one exists. Nothing is written on a refusal."""
    base = (root / "blueprints") if root else BLUEPRINTS
    snippets = _read_snippets(base.parent)
    included: set[str] = set()
    packed = []
    for folder in sorted(p for p in base.glob("*") if p.is_dir()):
        if not (folder / SERVICES).is_file():
            for name in (BLOCK_FILE, *SECTIONS, HOOKS_DIR):
                if (folder / name).exists() or (folder / name).is_symlink():
                    raise PackError(f"blueprints/{folder.name}/{name}: the folder has no {SERVICES}, "
                                    f"so nothing reads this")
            continue
        neat, hooks = _read_neat(folder)
        text = pack_texts(neat, hooks, f"blueprints/{folder.name}/", snippets, included)
        target = folder / COMPOSE
        if not target.is_file() or _read(target) != text:
            packed.append((target, text))
    _unused_snippets(snippets, included)
    for target, text in packed:
        target.write_text(text, encoding="utf-8", newline="")
    return [target for target, _ in packed]


def _cut(text: str, where: str, index: dict[str, _Snippet]) -> tuple[dict[str, str], dict[str, str]]:
    """The neat files of a packed compose, or the reason it is out of normal form."""
    lines = _lines(text, where)
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise PackError(f"{where}: not YAML: {exc}") from exc
    if not isinstance(root, yaml.MappingNode) or len(root.value) < 2:
        raise PackError(f"{where}:1: a mapping holding x-catena and services is expected")
    (top, block), after = root.value[0], root.value[1][0]
    if top.value != "x-catena" or top.start_mark.line != 0 or not isinstance(block, yaml.MappingNode):
        raise PackError(f"{where}:1: the block is not first; x-catena must open the file")
    if lines[1:2] != ["  schema: 2"] or block.value[0][0].value != "schema":
        raise PackError(f"{where}:2: `  schema: 2` must be the first line of the block")
    names = [key.value for key, _ in block.value[1:]]
    for n, name in enumerate(names):
        at = block.value[1:][n][0].start_mark.line + 1
        if name not in ORDER[1:]:
            raise PackError(f"{where}:{at}: {name} is not a key of the block")
        if n and ORDER.index(name) <= ORDER.index(names[n - 1]):
            raise PackError(f"{where}:{at}: {name} is out of order; it follows {names[n - 1]} but "
                            f"the order is {', '.join(ORDER)}")
    gap = after.start_mark.line - 1
    while gap > 0 and lines[gap].startswith("#"):
        gap -= 1
    if lines[gap] != "" or lines[gap - 1] == "":
        raise PackError(f"{where}:{after.start_mark.line + 1}: the block must be followed by exactly "
                        f"one empty line before {after.value}")
    for n, line in enumerate(lines[1:gap], 1):
        if line and not line.startswith("  "):
            raise PackError(f"{where}:{n + 1}: a line of the block must be indented by two spaces")
    block_lines, files = _extract_scripts([line[2:] for line in lines[1:gap]], where, index)
    return {BLOCK_FILE: "\n".join(block_lines) + "\n", SERVICES: "\n".join(lines[gap + 1:]) + "\n"}, files


def _extract_scripts(block: list[str], where: str, index: dict[str, _Snippet]) -> tuple[list[str], dict[str, str]]:
    """The block lines (the compose's, two spaces removed) with each script of a hook or
    lifecycle step list written as the path of its file or of the snippet holding its
    text, and the script files. `block[0]` is line 2 of the compose."""
    root = yaml.compose("\n".join(block) + "\n")
    found, claimed = [], set()
    for place, seq in _step_lists(root):
        ordinal = 0
        for item in seq.value:
            if not isinstance(item, yaml.SequenceNode) or len(item.value) != 3 \
                    or not all(isinstance(n, yaml.ScalarNode) for n in item.value):
                continue
            interpreter, flag, body = item.value
            ext = EXTENSIONS.get((interpreter.value, flag.value))
            if body.style not in ("|", ">"):
                _refuse_inline_script(item, place, where, 2)
            if ext is None or body.style not in ("|", ">"):
                if body.style in ("|", ">") and (interpreter.value, flag.value) == ("python", "-c"):
                    raise PackError(f"{where}:{interpreter.start_mark.line + 2}: python -c has no script file; "
                                    f"write python3")
                continue
            at = interpreter.start_mark.line
            indent = max(block[at].find("- - "), 0)
            plain = interpreter.style is None and flag.style is None
            shaped = plain and body.style == "|" and block[at] == f"{' ' * indent}- - {interpreter.value}" \
                and block[at + 1] == f"{' ' * indent}  - {flag.value}" \
                and block[at + 2] == f"{' ' * indent}  - |" and body.start_mark.line == at + 2
            if not shaped:
                raise PackError(f"{where}:{at + 2}: the script of {place} is not written as `- - "
                                f"{interpreter.value}`, `  - {flag.value}`, `  - |` on three lines")
            ordinal += 1
            found.append((place, ordinal, ext, at, indent))
            claimed.add(id(body))
    for node in _scalars(root):
        if node.style in ("|", ">") and id(node) not in claimed:
            raise PackError(f"{where}:{node.start_mark.line + 2}: a block scalar outside a script of a hook "
                            f"or lifecycle step list; write it on one line or as a script of "
                            f"{', '.join(f'{i} {f}' for i, f in EXTENSIONS)}")
    by_text = {}
    for _, snippet in sorted(index.items()):
        by_text.setdefault((snippet.ext, snippet.text), snippet.path)
    files, out = {}, list(block)
    for place, ordinal, ext, at, indent in reversed(found):
        header = at + 2
        end = header + 1
        while end < len(block) and (block[end] == "" or block[end].startswith(" " * (indent + 4))):
            end += 1
        while end - 1 > header and block[end - 1] == "":
            end -= 1
        body = []
        for i in range(header + 1, end):
            line = block[i][indent + 4:]
            where_i = f"{where}:{i + 2}"
            if line and not line.strip():
                raise PackError(f"{where_i}: a line of spaces only in the script of {place}")
            if _ODD_DOLLARS.search(line):
                raise PackError(f"{where_i}: an odd run of $ in the script of {place}; each $ is written $$")
            body.append(line.replace("$$", "$"))
        opening = next((line for line in body if line), None)
        if opening is None or opening.startswith(" "):
            raise PackError(f"{where}:{header + 2}: the script of {place} is empty or starts with a space")
        script = "\n".join(body) + "\n"
        path = by_text.get((ext, script))
        if path is None:
            path = _hook_name(place, ordinal, ext)
            files[path] = script
        out[at:end] = [f"{' ' * indent}- {path}"]
    return out, files


def unpack(app: str, root: Path | None = None) -> list[Path]:
    """Write the neat files of the app's packed compose; the files written. Nothing is
    written unless packing them gives the compose back byte for byte."""
    folder = ((root / "blueprints") if root else BLUEPRINTS) / app
    where = f"blueprints/{app}/{COMPOSE}"
    if (folder / SERVICES).exists() or (folder / BLOCK_FILE).exists():
        raise PackError(f"blueprints/{app}: already held as neat files")
    text = _read(folder / COMPOSE)
    snippets = _read_snippets(folder.parent.parent)
    neat, files = _cut(text, where, _snippet_index(snippets))
    if pack_texts(neat, files, f"blueprints/{app}/", snippets) != text:
        raise PackError(f"{where}: does not pack back to the same bytes")
    written = []
    for name, body in {**neat, **files}.items():
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")
        written.append(target)
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pack")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("unpack").add_argument("app")
    args = parser.parse_args(argv)
    if not _APP.fullmatch(args.app):
        print(f"{args.app!r} is not a plain app id", file=sys.stderr)
        return 1
    try:
        for path in unpack(args.app):
            print(path)
    except (PackError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0
