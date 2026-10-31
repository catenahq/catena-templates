"""Pack an app held as neat files into its `docker-compose.yml`, and unpack one.

An app is held as neat files when `blueprints/<id>/` holds `services.yml`:

  services.yml    every top-level key after the block (services, volumes, networks,
                  x-<name>), as written
  settings.yml    block keys env, secrets
  access.yml      block keys privileges, capabilities, host_paths, access, provides,
                  needs, egress
  lifecycle.yml   block keys requires_catena, version_from, lifecycle, quiesce, probe,
                  object_store, write_hold, stops, images, hooks, data
  hooks/<hook>[-<n>].<ext>
                  one hook script per file, each `$` written once

and, shared by apps and belonging to none, at the root of the tree:

  snippets/<name>/<n>.<ext>
                  a part of hook scripts, one file per version, each `$` written once

`pack` writes `x-catena:` and `  schema: 2`, then the text of each block key in
`ORDER`, every line indented two spaces, one empty line and `services.yml` as it is.
A key's text runs from the blank and comment lines just above it to the next key, so
a comment travels with the key below it. In `lifecycle.yml`'s `hooks`, an item
`- {file: hooks/<name>}`, `- {include: [<snippet>/<n>, ...]}` or
`- {file: hooks/<name>, include: [<snippet>/<n>, ...]}` becomes `- |` followed by the
snippets' lines, then the file's, each indented two past the dash and each `$` written
`$$`. An include names an existing snippet file with the script's extension. A snippet
version no app includes is refused unless it is the newest of its snippet, and so are
two versions of a snippet with the same text. The packed file is what hosts, the bench
and every check read; the render commits it beside the neat files.

`unpack` cuts a packed compose into the neat files and writes them only when packing
them gives the compose back byte for byte. A script body that begins with a snippet's
text (`$$` read as `$`) is written as an include of it, the longest snippet first. A
compose out of normal form is refused with the change to make. Pack reads text and
PyYAML's node positions only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .model import BLUEPRINTS

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
# The order of the block keys in the packed file; `schema` opens it and no file holds it.
ORDER = ("schema", "requires_catena", "version_from", "privileges", "env", "secrets", "access",
         "capabilities", "host_paths", "provides", "lifecycle", "quiesce", "probe", "object_store",
         "needs", "write_hold", "stops", "images", "hooks", "egress", "data")
# The files pack reads besides the files under HOOKS_DIR.
NEAT_FILES = (SERVICES, *SECTIONS)
# The extension of a hook script file, by the interpreter that runs it.
INTERPRETERS = {"sh": "sh", "bash": "sh", "python3": "py", "python": "py", "node": "js", "php": "php"}

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?=:(?: |$))")
_ITEM = re.compile(r"(?P<indent> *)- \{(?:file: (?P<file>hooks/[A-Za-z0-9._-]+)"
                   r"(?:, include: \[(?P<both>[^\]]*)\])?|include: \[(?P<only>[^\]]*)\])\}")
_INCLUDE_FORM = re.compile(r"\s*- \{\s*(?:file|include)\s*:")
_REF = re.compile(r"([a-z0-9][a-z0-9-]*)/([1-9][0-9]*)")
_SNIPPET_PATH = re.compile(r"snippets/([a-z0-9][a-z0-9-]*)/([1-9][0-9]*)\.([a-z]+)")
_HEADER = re.compile(r"( *)- \|")
_ODD_DOLLARS = re.compile(r"(?<!\$)(?:\$\$)*\$(?!\$)")
_APP = re.compile(r"[a-z0-9][a-z0-9-]*")


class PackError(Exception):
    """A set of neat files or a compose that is not in normal form."""


def _lines(text: str, where: str) -> list[str]:
    if "\r" in text:
        raise PackError(f"{where}: holds a carriage return; use line feeds")
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
    if "\r" in text:
        raise PackError(f"{where}: holds a carriage return; use line feeds")
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
        if not match or match[3] not in INTERPRETERS.values():
            raise PackError(f"{path}: not a snippet file; {SNIPPETS_DIR}/<name>/<n>.<ext> is expected, "
                            f"<n> counting from 1 and <ext> one of {', '.join(sorted(set(INTERPRETERS.values())))}")
        ref = f"{match[1]}/{match[2]}"
        if ref in index:
            raise PackError(f"{path}: {ref} is already held in {index[ref].path}")
        index[ref] = _Snippet(path, match[3], snippets[path])
        if (match[1], snippets[path]) in texts:
            raise PackError(f"{path}: holds the text of {texts[match[1], snippets[path]]}; "
                            f"a snippet keeps one version per text")
        texts[match[1], snippets[path]] = path
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
                            f"it belongs in a section file, not here")
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
    """Pack every folder holding `services.yml`; the compose files written. A section file
    or a `hooks/` folder in a folder without `services.yml` is refused, and so is a snippet
    version that no app includes while a newer one exists. Nothing is written on a refusal."""
    base = (root / "blueprints") if root else BLUEPRINTS
    snippets = _read_snippets(base.parent)
    included: set[str] = set()
    packed = []
    for folder in sorted(p for p in base.glob("*") if p.is_dir()):
        if not (folder / SERVICES).is_file():
            for name in (*SECTIONS, HOOKS_DIR):
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
    rest = block.value[1:]
    names = [key.value for key, _ in rest]
    for n, name in enumerate(names):
        at = rest[n][0].start_mark.line + 1
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
    block_lines = lines[:gap]
    for n, line in enumerate(block_lines):
        if line and not line.startswith("  ") and n:
            raise PackError(f"{where}:{n + 1}: a line of the block must be indented by two spaces")
    marks = [key.start_mark.line for key, _ in rest]
    starts = []
    for n, mark in enumerate(marks):
        start, floor = mark, (marks[n - 1] if n else 1) + 1
        while start > floor and (block_lines[start - 1] == "" or block_lines[start - 1].startswith("  #")):
            start -= 1
        starts.append(start)
    ends = starts[1:] + [len(block_lines)]
    sections: dict[str, list[str]] = {}
    hooks: dict[str, str] = {}
    for n, name in enumerate(names):
        chunk = [line[2:] for line in block_lines[starts[n]:ends[n]]]
        if name == "hooks":
            chunk, hooks = _extract_hooks(chunk, starts[n], where, index)
        home = next(file for file, keys in SECTIONS.items() if name in keys)
        sections.setdefault(home, []).extend(chunk)
    neat = {file: "\n".join(chunk) + "\n" for file, chunk in sections.items()}
    neat[SERVICES] = "\n".join(lines[gap + 1:]) + "\n"
    return neat, hooks


def _split_snippets(body: str, ext: str, index: dict[str, _Snippet]) -> tuple[list[str], str]:
    """The snippets a script body begins with, longest first, and what follows them."""
    found = sorted(((ref, s.text) for ref, s in index.items() if s.ext == ext), key=lambda p: -len(p[1]))
    refs = []
    while True:
        for ref, text in found:
            rest = body[len(text):]
            if body.startswith(text) and (not rest or _valid_hook_text(rest)):
                refs.append(ref)
                body = rest
                break
        else:
            return refs, body


def _valid_hook_text(text: str) -> bool:
    try:
        _hook_lines("", text, "")
    except PackError:
        return False
    return True


def _extract_hooks(chunk: list[str], first: int, where: str,
                   index: dict[str, _Snippet]) -> tuple[list[str], dict[str, str]]:
    """The `hooks` text with each script written as a `- {file: ...}` and `- {include: ...}`
    item, and the files."""
    files, out = {}, list(chunk)
    for hook, ordinal, ext, header in reversed(_scripts(chunk)):
        indent = len(_HEADER.fullmatch(chunk[header]).group(1))
        end = header + 1
        while end < len(chunk) and (chunk[end] == "" or chunk[end].startswith(" " * (indent + 2))):
            end += 1
        while end - 1 > header and chunk[end - 1] == "":
            end -= 1
        name = _hook_name(hook, ordinal, ext)
        body = []
        for i in range(header + 1, end):
            line = chunk[i][indent + 2:]
            at = f"{where}:{first + i + 1}"
            if line and not line.strip():
                raise PackError(f"{at}: a line of spaces only in the script of {hook}")
            if _ODD_DOLLARS.search(line):
                raise PackError(f"{at}: an odd run of $ in the script of {hook}; each $ is written $$")
            body.append(line.replace("$$", "$"))
        opening = next((line for line in body if line), None)
        if opening is None or opening.startswith(" "):
            raise PackError(f"{where}:{first + header + 1}: the script of {hook} is empty or starts with a space")
        refs, rest = _split_snippets("\n".join(body) + "\n", ext, index)
        parts = ([f"file: {name}"] if rest else []) + ([f"include: [{', '.join(refs)}]"] if refs else [])
        if rest:
            files[name] = rest
        out[header:end] = [f"{' ' * indent}- {{{', '.join(parts)}}}"]
    return out, files


def unpack(app: str, root: Path | None = None) -> list[Path]:
    """Write the neat files of the app's packed compose; the files written. Nothing is
    written unless packing them gives the compose back byte for byte."""
    folder = ((root / "blueprints") if root else BLUEPRINTS) / app
    where = f"blueprints/{app}/{COMPOSE}"
    if (folder / SERVICES).exists():
        raise PackError(f"blueprints/{app}: already held as neat files")
    text = _read(folder / COMPOSE)
    snippets = _read_snippets(folder.parent.parent)
    neat, hooks = _cut(text, where, _snippet_index(snippets))
    if pack_texts(neat, hooks, f"blueprints/{app}/", snippets) != text:
        raise PackError(f"{where}: does not pack back to the same bytes")
    written = []
    for name, body in {**neat, **hooks}.items():
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
