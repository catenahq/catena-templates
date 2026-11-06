"""Every blueprint's test.yml proves the app's own data: no step reads or
writes a stand-in (a catena_canary table, a database server's own database), at
least one assert asks the app more than a plain read of a path, and each word a
seed builds from the run id appears in an assert. An app listed in
app-tests-debt.txt is held to none of it until its test is rewritten, and its
line goes once the test meets the contract."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEBT = Path(__file__).with_name("app-tests-debt.txt")
TESTS = sorted(ROOT.glob("blueprints/*/test.yml"))
CANARY = re.compile(r"catena_canary")
PLACEHOLDER = re.compile(r"\{\{ \w+ \}\}")
RUN_RECORD = re.compile(r"[\w./-]*\{\{ run_id \}\}[\w./-]*")
SHELLS = {"sh", "bash", "ash", "dash"}
SHELL_FLAGS = re.compile(r"-[a-z]*c[a-z]*")
# Words that lead a command without being its program.
LEADERS = {"exec", "if", "then", "do", "else", "elif", "while", "until", "!", "command", "env", "time"}
READERS = {"cat", "head", "tail", "test", "[", "grep", "egrep", "fgrep", "ls", "stat", "wc", "file", "od",
           "xxd", "md5sum", "sha1sum", "sha256sum", "readlink", "realpath", "find", "du"}
NEUTRAL = {"set", "echo", "printf", "true", "exit", ":"}
MARIADB_CLIENTS = {"mariadb", "mysql"}
MARIADB_VALUES = {"-h", "--host", "-u", "--user", "-P", "--port", "-S", "--socket", "-e", "--execute",
                  "--default-character-set", "--protocol"}
PSQL_CLIENTS = {"psql", "pg_dump"}
PSQL_VALUES = {"-h", "--host", "-p", "--port", "-c", "--command", "-f", "--file", "-v", "--set", "--variable",
               "-o", "--output", "-F", "--field-separator", "-R", "--record-separator", "-P", "--pset", "-T",
               "-L", "--log-file", "-n", "-N", "-t", "-Z", "-j", "-E", "--format"}
# SQL that names the `mysql` schema or one of its tables, and psql's switch to
# the `postgres` database.
SERVER_SQL = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|EXISTS|USE)\s+`?mysql`?(?:\.|\s*;|\s*$)"
                        r"|\\c(?:onnect)?\s+postgres\b", re.I | re.M)


def _debt() -> dict[str, str]:
    entries = {}
    for line in DEBT.read_text(encoding="utf-8").splitlines():
        app, sep, reason = line.partition(" -- ")
        assert sep and app.strip() and reason.strip(), f"app-tests-debt.txt: {line!r} is not '<id> -- reason'"
        assert app not in entries, f"app-tests-debt.txt lists {app} twice"
        entries[app] = reason
    return entries


def _text(step: dict) -> str:
    command = step.get("command") or []
    return " ".join([*command, step.get("content", ""), step.get("path", "")])


def _name(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _commands(script: str) -> list[tuple[dict[str, str], list[str]]]:
    """The simple commands of a shell script, each as (the variables set for
    it, its words from the program on), heredoc bodies and redirections left
    out. A variable an `export` sets holds for the commands after it."""
    found, exported, ending = [], {}, None
    for line in PLACEHOLDER.sub("x", script).replace("\\\n", " ").splitlines():
        if ending is not None:
            ending = None if line.strip() == ending else ending
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            words = list(lexer)
        except ValueError:
            words = line.split()
        command: list[str] = []
        skip = False
        for i, word in enumerate(words + [";"]):
            if skip:
                skip = False
            elif word == "<<":
                ending, skip = (words[i + 1].lstrip("-") if i + 1 < len(words) else None), True
            elif set(word) <= set("<>&") and set(word) & set("<>"):
                skip = True
                if command and command[-1].isdigit():
                    command.pop()
            elif set(word) <= set(";|&()"):
                env = dict(exported)
                while command and (command[0] in LEADERS or re.fullmatch(r"\w+=.*", command[0])):
                    key, _, value = command.pop(0).partition("=")
                    if value or key not in LEADERS:
                        env[key] = value
                if command[:1] == ["export"]:
                    exported.update(w.split("=", 1) for w in command[1:] if "=" in w)
                elif command:
                    found.append((env, command))
                command = []
            else:
                command.append(word)
    return found


def _step_commands(argv: list[str]) -> list[tuple[dict[str, str], list[str]]]:
    if len(argv) >= 3 and _name(argv[0]) in SHELLS and SHELL_FLAGS.fullmatch(argv[1]):
        return _commands(argv[2])
    return [({}, argv)] if argv else []


def _option(args: list[str], i: int, short: str, long: str) -> tuple[str | None, int]:
    """(the value of `short`/`long` at args[i], how many words it took)."""
    word = args[i]
    if word in (short, long):
        return (args[i + 1] if i + 1 < len(args) else None), 2
    if word.startswith(long + "="):
        return word.split("=", 1)[1], 1
    if word.startswith(short) and len(word) > len(short) and not word.startswith("--"):
        return word[len(short):], 1
    return None, 0


def _mariadb_database(args: list[str]) -> str | None:
    database, positional, i = None, [], 0
    while i < len(args):
        value, taken = _option(args, i, "-D", "--database")
        if taken:
            database, i = value, i + taken
            continue
        if args[i] in MARIADB_VALUES:
            i += 1
        elif not args[i].startswith("-"):
            positional.append(args[i])
        i += 1
    return database or (positional[0] if positional else None)


def _psql_database(env: dict[str, str], args: list[str]) -> str | None:
    """The database a psql or pg_dump call opens: -d, else the first operand,
    else PGDATABASE, else the user (-U, the second operand, PGUSER)."""
    database = user = None
    positional, i = [], 0
    while i < len(args):
        value, taken = _option(args, i, "-d", "--dbname")
        if taken:
            database, i = value, i + taken
            continue
        value, taken = _option(args, i, "-U", "--username")
        if taken:
            user, i = value, i + taken
            continue
        if args[i] in PSQL_VALUES:
            i += 1
        elif not args[i].startswith("-"):
            positional.append(args[i])
        i += 1
    database = database or (positional[0] if positional else None)
    if database and ("=" in database or "://" in database):
        named = re.search(r"dbname=(\S+)", database) or re.fullmatch(r"\w+://[^/]*/([^?]*).*", database)
        database = named.group(1) if named else None
    user = user or (positional[1] if len(positional) > 1 else None) or env.get("PGUSER")
    return database or env.get("PGDATABASE") or user


def _server_database(step: dict) -> bool:
    """Whether a step opens a database server's own database."""
    if SERVER_SQL.search(_text(step)):
        return True
    for env, words in _step_commands(step.get("command") or []):
        for i, word in enumerate(words):
            if _name(word) in MARIADB_CLIENTS:
                if _mariadb_database(words[i + 1:]) == "mysql":
                    return True
                break
            if _name(word) in PSQL_CLIENTS:
                if _psql_database(env, words[i + 1:]) == "postgres":
                    return True
                break
    return False


def _plain_read(command: list[str]) -> bool:
    """Whether a command only reads paths: every program it runs a reader
    (`cat`, `head`, `grep`, `test`, `ls`, `stat` and the like), beside `set`,
    `echo` and `exit`."""
    programs = [_name(words[0]) for _, words in _step_commands(command)]
    return any(p in READERS for p in programs) and all(p in READERS | NEUTRAL for p in programs)


def problems(doc: dict) -> list[str]:
    """Why a test.yml does not prove the app's own data."""
    seed, checks = doc.get("seed") or [], doc.get("assert") or []
    found = []
    for step in (*seed, *checks):
        if CANARY.search(_text(step)):
            found.append(f"step {step['name']} names a catena_canary table")
        if _server_database(step):
            found.append(f"step {step['name']} reads or writes a database server's own database")
    if not any(step["type"] == "docker_exec" and not _plain_read(step["command"]) for step in checks):
        found.append("no assert is a docker_exec that asks the app more than a plain read of a path")
    named = " ".join(_text(step) + " " + step.get("expect_stdout_contains", "") for step in checks)
    for step in seed:
        for record in sorted(set(RUN_RECORD.findall(_text(step)))):
            if record not in named:
                found.append(f"seed {step['name']} writes {record} and no assert names it")
    return found


def _app(path: Path) -> str:
    return path.parent.name


@pytest.mark.parametrize("path", [p for p in TESTS if _app(p) not in _debt()], ids=_app)
def test_the_test_proves_the_apps_own_data(path):
    assert problems(yaml.safe_load(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("app", sorted(_debt()))
def test_an_app_listed_as_debt_has_a_test_that_still_breaks_the_contract(app):
    path = ROOT / "blueprints" / app / "test.yml"
    assert path.is_file(), f"{app} has no test.yml: delete its line in app-tests-debt.txt"
    assert problems(yaml.safe_load(path.read_text(encoding="utf-8"))), (
        f"{app}'s test.yml meets the contract: delete its line in app-tests-debt.txt")


def _exec(name: str, *command: str, expect: str = "") -> dict:
    return {"name": name, "type": "docker_exec", "container": "{{ container_app }}", "command": list(command),
            "expect_stdout_contains": expect}


GOOD = {"seed": [_exec("make", "sh", "-c", "curl -X POST -d name=catena-{{ run_id }} http://app/api/items")],
        "assert": [_exec("read", "sh", "-c", "curl http://app/api/items", expect="catena-{{ run_id }}")]}


def test_a_test_that_seeds_and_reads_through_the_app_passes():
    assert problems(GOOD) == []


@pytest.mark.parametrize("command", [
    "mariadb -uroot mysql -e 'SELECT 1'",
    "MYSQL_PWD=x mariadb -uroot -N -B mysql -e 'SELECT 1'",
    "mysql -uroot -D mysql",
    "mysql --database=mysql",
    "exec mariadb -h db -u root mysql",
    "mariadb -uroot -e 'SELECT * FROM mysql.user'",
    "MYSQL_PWD=x mariadb -uroot app <<'SQL'\nINSERT INTO mysql.catena_x VALUES (1);\nSQL",
    "mariadb -uroot -e 'USE mysql; SELECT 1'",
    "psql -U postgres -d postgres -c 'SELECT 1'",
    "psql --dbname=postgres",
    "psql -U postgres -c 'SELECT 1'",
    "psql -U app postgres",
    "PGDATABASE=postgres psql -U app -c 'SELECT 1'",
    "export PGDATABASE=postgres\npsql -U app -c 'SELECT 1'",
    "psql 'postgresql://app@localhost/postgres' -c 'SELECT 1'",
    "psql -d 'dbname=postgres user=app'",
    "PGUSER=postgres psql -c 'SELECT 1'",
    "pg_dump -U app postgres > /tmp/x",
    "psql -U app -d app -c '\\connect postgres'",
])
def test_a_step_on_a_database_servers_own_database_is_refused(command):
    doc = {**GOOD, "seed": [*GOOD["seed"], _exec("server", "sh", "-c", command)]}
    assert problems(doc) == ["step server reads or writes a database server's own database"]


def test_an_argv_step_on_a_database_servers_own_database_is_refused():
    doc = {**GOOD, "seed": [*GOOD["seed"], _exec("server", "mariadb", "-uroot", "mysql", "-e", "SELECT 1")]}
    assert problems(doc) == ["step server reads or writes a database server's own database"]


@pytest.mark.parametrize("command", [
    "psql -U postgres -d app -c 'SELECT 1'",
    "psql -U \"$POSTGRES_USER\" -c 'SELECT 1'",
    "psql -h postgres -U app -d app",
    "MYSQL_PWD=\"$MARIADB_ROOT_PASSWORD\" mariadb -uroot app -e 'SELECT 1'",
    "mariadb -h mysql -uroot app",
    "mysql --host mysql --user root app",
    "mariadb -hmysql -uroot -D app",
    "mariadb-admin ping -h localhost -uroot",
    "wait-for-it mysql:3306",
    "python3 -c 'import mysql.connector'",
    "mariadb -uroot app -e 'SELECT mysql_version FROM settings'",
])
def test_a_step_on_the_apps_own_database_is_not_a_server_database(command):
    doc = {**GOOD, "seed": [*GOOD["seed"], _exec("own", "sh", "-c", command)]}
    assert problems(doc) == []


def test_a_canary_table_is_refused():
    doc = {**GOOD, "assert": [*GOOD["assert"], _exec("canary", "sh", "-c", "SELECT run_id FROM catena_canary")]}
    assert problems(doc) == ["step canary names a catena_canary table"]


@pytest.mark.parametrize("step", [
    _exec("read", "cat", "/data/catena-{{ run_id }}.txt", expect="fixture-{{ run_id }}"),
    _exec("read", "sh", "-c", "cat /data/catena-{{ run_id }}.txt\n", expect="fixture-{{ run_id }}"),
    _exec("read", "sh", "-ec", "cat /data/catena-{{ run_id }}.txt"),
    _exec("read", "bash", "-euc", "set -o pipefail\ncat /data/a /data/catena-{{ run_id }}.txt"),
    _exec("read", "head", "-n1", "/data/catena-{{ run_id }}.txt"),
    _exec("read", "test", "-s", "/data/catena-{{ run_id }}.txt"),
    _exec("read", "sh", "-c", "grep -q fixture /data/catena-{{ run_id }}.txt && echo ok"),
    _exec("read", "sh", "-c", "ls -l /data/catena-{{ run_id }}.txt; stat /data"),
    _exec("read", "/bin/sh", "-c", "[ -f /data/catena-{{ run_id }}.txt ] || exit 1"),
    {"name": "read", "type": "volume_file", "volume": "data", "path": "/catena-{{ run_id }}.txt",
     "expect_stdout_contains": "fixture-{{ run_id }}"},
])
def test_asserts_that_only_read_a_path_are_refused(step):
    assert problems({**GOOD, "assert": [step]}) == [
        "no assert is a docker_exec that asks the app more than a plain read of a path"]


@pytest.mark.parametrize("command", [
    "cat /data/catena-{{ run_id }}.txt | app-cli import -",
    "curl -s http://app/api/items/catena-{{ run_id }}",
    "set -eu\nocc files:scan --path /catena-{{ run_id }}",
])
def test_an_assert_that_asks_the_app_is_more_than_a_plain_read(command):
    assert problems({**GOOD, "assert": [_exec("read", "sh", "-c", command)]}) == []


def test_each_word_a_seed_builds_from_the_run_needs_an_assert_that_names_it():
    seed = _exec("make", "sh", "-c", "put catena-{{ run_id }} fixture-{{ run_id }}")
    doc = {"seed": [seed], "assert": [_exec("read", "sh", "-c", "curl http://app/api/items",
                                            expect="catena-{{ run_id }}")]}
    assert problems(doc) == ["seed make writes fixture-{{ run_id }} and no assert names it"]


def test_a_second_seed_is_held_to_its_own_words():
    doc = {**GOOD, "seed": [*GOOD["seed"], _exec("more", "sh", "-c", "put note-{{ run_id }}")]}
    assert problems(doc) == ["seed more writes note-{{ run_id }} and no assert names it"]


def test_a_test_with_no_seed_needs_no_run_id():
    assert problems({"assert": [_exec("probe", "sh", "-c", "curl http://app/health")]}) == []
