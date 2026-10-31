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

`pack` writes `x-catena:` and `  schema: 2`, then the text of each block key in
`ORDER`, every line indented two spaces, one empty line and `services.yml` as it is.
A key's text runs from the blank and comment lines just above it to the next key, so
a comment travels with the key below it. In `lifecycle.yml`'s `hooks`, an item
`- {file: hooks/<name>}` becomes `- |` followed by the file's lines, each indented two
past the dash and each `$` written `$$`. The packed file is what hosts, the bench and
every check read; the render commits it beside the neat files.

`unpack` cuts a packed compose into the neat files and writes them only when packing
them gives the compose back byte for byte. A compose out of normal form is refused with
the change to make. Pack reads text and PyYAML's node positions only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .model import BLUEPRINTS

COMPOSE = "docker-compose.yml"
SERVICES = "services.yml"
HOOKS_DIR = "hooks"

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
_INCLUDE = re.compile(r"( *)- \{file: (hooks/[A-Za-z0-9._-]+)\}")
_INCLUDE_FORM = re.compile(r"\s*- \{\s*(?:file|include)\s*:")
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


def _chunks(lines: list[str], where: str) -> list[tuple[str, int, int, list[str]]]:
    """(key, index of its line, index of its text's first line, its text) for each
    top-level key of `lines`."""
    keys = [i for i, line in enumerate(lines) if _KEY.match(line)]
    if not keys:
        raise PackError(f"{where}: holds no key")
    for i, line in enumerate(lines[:keys[0]]):
        if line and not line.startswith("#"):
            raise PackError(f"{where}:{i + 1}: text before the first key")
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
    if lines[0].startswith(" "):
        raise PackError(f"{where}:1: the first line starts with a space")
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


def _expand_hooks(chunk: list[str], first: int, hooks: dict[str, str], where: str) -> tuple[list[str], list[str]]:
    """The `hooks` text with each `- {file: ...}` item written out, and the files used."""
    out, source, placed, used = [], [], {}, []
    for n, line in enumerate(chunk):
        match = _INCLUDE.fullmatch(line)
        if not match:
            if _INCLUDE_FORM.match(line):
                raise PackError(f"{where}:{first + n + 1}: only `- {{file: hooks/<name>}}` is read here")
            out.append(line)
            source.append(n)
            continue
        indent, name = match.groups()
        if name not in hooks:
            raise PackError(f"{where}:{first + n + 1}: {name} does not exist")
        body = _hook_lines(name, hooks[name], f"{Path(where).parent / name}")
        placed[len(out)] = (name, n)
        used.append(name)
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
        name, n = placed[header]
        if name != expected:
            raise PackError(f"{where}:{first + n + 1}: {name} is not named after its place; "
                            f"this script of {hook} is {expected}")
    if len(scripts) != len(used):
        stray = next(n for h, (name, n) in placed.items() if h not in {s[3] for s in scripts})
        raise PackError(f"{where}:{first + stray + 1}: {chunk[stray].strip()} is not the last item of "
                        f"[interpreter, flag, body] of a hook")
    return out, used


def pack_texts(neat: dict[str, str], hooks: dict[str, str], prefix: str = "") -> str:
    """The packed compose of an app's neat files (`neat`: file name to text, `hooks`:
    `hooks/<name>` to text); `prefix` opens the paths of error messages."""
    if SERVICES not in neat:
        raise PackError(f"{prefix}{SERVICES}: missing")
    services = _lines(neat[SERVICES], prefix + SERVICES)
    if not services[0]:
        raise PackError(f"{prefix}{SERVICES}:1: starts with an empty line")
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
                chunk, files = _expand_hooks(chunk, start, hooks, where)
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


def pack_all(root: Path | None = None) -> list[Path]:
    """Pack every folder holding `services.yml`; the compose files written."""
    base = (root / "blueprints") if root else BLUEPRINTS
    written = []
    for services in sorted(base.glob(f"*/{SERVICES}")):
        folder = services.parent
        neat, hooks = _read_neat(folder)
        text = pack_texts(neat, hooks, f"blueprints/{folder.name}/")
        target = folder / COMPOSE
        if not target.is_file() or _read(target) != text:
            target.write_text(text, encoding="utf-8", newline="")
            written.append(target)
    return written


def _cut(text: str, where: str) -> tuple[dict[str, str], dict[str, str]]:
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
            chunk, hooks = _extract_hooks(chunk, starts[n], where)
        home = next(file for file, keys in SECTIONS.items() if name in keys)
        sections.setdefault(home, []).extend(chunk)
    neat = {file: "\n".join(chunk) + "\n" for file, chunk in sections.items()}
    neat[SERVICES] = "\n".join(lines[gap + 1:]) + "\n"
    return neat, hooks


def _extract_hooks(chunk: list[str], first: int, where: str) -> tuple[list[str], dict[str, str]]:
    """The `hooks` text with each script written as a `- {file: ...}` item, and the files."""
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
        if not body or body[0].startswith(" "):
            raise PackError(f"{where}:{first + header + 1}: the script of {hook} is empty or starts with a space")
        files[name] = "\n".join(body) + "\n"
        out[header:end] = [f"{' ' * indent}- {{file: {name}}}"]
    return out, files


def unpack(app: str, root: Path | None = None) -> list[Path]:
    """Write the neat files of the app's packed compose; the files written. Nothing is
    written unless packing them gives the compose back byte for byte."""
    folder = ((root / "blueprints") if root else BLUEPRINTS) / app
    where = f"blueprints/{app}/{COMPOSE}"
    if (folder / SERVICES).exists():
        raise PackError(f"blueprints/{app}: already held as neat files")
    text = _read(folder / COMPOSE)
    neat, hooks = _cut(text, where)
    if pack_texts(neat, hooks, f"blueprints/{app}/") != text:
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
