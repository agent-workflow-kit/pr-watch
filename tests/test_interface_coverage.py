"""A test-enforced audit of the interface against the tests (issue #91, an item of #43).

The items come from their sources of truth, not from a list kept here: the argparse parser (commands
and flags), schemas/ (result documents, hook payloads, events, log records, error codes) and the
exit-code table of docs/interface.md. tests/coverage_map.py maps each item to the tests that
exercise it. This file fails when:

- an item has no entry (a new command, flag, schema, event type, log record or code was added
  without a test), or an entry names an item that no longer exists;
- a named test does not exist, or does not mention the item (so a renamed or emptied test cannot
  keep a stale claim alive);
- an item is listed as uncoverable without a reason;
- docs/interface.md and the parser disagree about a command's flags;
- the totals in docs/coverage.md are stale.

It also checks itself: `problems()` is run on broken maps and must report the breakage.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Callable
from functools import cache
from typing import Any

import pytest

from pr_watch.cli import build_parser
from tests import coverage_map as cm
from tests.harness import ROOT, load_schema, schema_names

INTERFACE = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
CATEGORIES = ("commands", "flags", "schemas", "hooks", "events", "log", "errors", "exit codes")


# --- the items, from their sources of truth ------------------------------------------------------


def _global_flags() -> tuple[str, ...]:
    """The long flags of the root parser (`--json`, `--config`, ...): what every command takes."""
    root: Any = build_parser()
    return tuple(
        opt
        for action in root._actions  # noqa: SLF001
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    )


GLOBAL_FLAGS = _global_flags()


def _leaf_commands() -> dict[str, set[str]]:
    """{command path: its own long flags} for every leaf command of the real parser."""
    found: dict[str, set[str]] = {}

    def walk(parser: argparse.ArgumentParser, path: list[str]) -> None:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]  # noqa: SLF001
        if not subs:
            flags = {
                opt
                for a in parser._actions  # noqa: SLF001
                for opt in a.option_strings
                if opt.startswith("--") and opt != "--help" and opt not in GLOBAL_FLAGS
            }
            found[" ".join(path)] = flags
            return
        for name, sub in subs[0].choices.items():
            walk(sub, [*path, name])

    root: Any = build_parser()
    for action in root._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                walk(sub, [name])
    return found


def _exit_codes() -> set[str]:
    """The codes of the `## Exit codes` table of docs/interface.md, plus `signal` (128+N)."""
    section = INTERFACE.split("## Exit codes", 1)[1].split("\n## ", 1)[0]
    codes = set(re.findall(r"^\| (\d+) \|", section, flags=re.M))
    assert codes, "no exit-code table found"
    return codes | {"signal"}  # "A signal ends the process with 128 plus its number" (same section)


def _schema_names(prefix: str) -> list[str]:
    return [n for n in schema_names() if n.startswith(prefix)]


def items() -> dict[str, set[str]]:
    commands = _leaf_commands()
    flags = {f"{cmd} {flag}" for cmd, own in commands.items() for flag in own}
    flags |= {f"* {flag}" for flag in GLOBAL_FLAGS}
    log = {b["properties"]["event"]["const"] for b in load_schema("log")["oneOf"]}
    events = set(load_schema("defs")["$defs"]["event_type"]["enum"])
    return {
        "commands": set(commands),
        "flags": flags,
        "schemas": set(_schema_names("cli/")),
        "hooks": {n.removeprefix("hooks/") for n in _schema_names("hooks/")},
        "events": events,
        "log": log,
        "errors": set(load_schema("defs")["$defs"]["error_code"]["enum"]),
        "exit codes": _exit_codes(),
    }


def mapping() -> dict[str, dict[str, list[str]]]:
    return {
        "commands": cm.COMMANDS,
        "flags": cm.FLAGS,
        "schemas": cm.SCHEMAS,
        "hooks": cm.HOOKS,
        "events": cm.EVENTS,
        "log": cm.LOG,
        "errors": cm.ERRORS,
        "exit codes": cm.EXIT_CODES,
    }


# --- reading the tests ---------------------------------------------------------------------------


@cache
def _module(path: str) -> tuple[ast.Module, str] | None:
    file = ROOT / path
    if not file.is_file():
        return None
    source = file.read_text(encoding="utf-8")
    return ast.parse(source), source


def _words(node: ast.AST) -> set[str]:
    """Every whitespace-separated word of every string constant under `node`."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.update(sub.value.split())
            out.add(sub.value)
    return out


def _strings(nodes: list[ast.AST]) -> list[str]:
    return [
        sub.value
        for node in nodes
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]


def _token_in(token: str, strings: list[str]) -> bool:
    """`token` as a whole name in some string: `"on-ci"` or `"hooks/on-ci"` or `"on-ci:\n"`, but not
    `"on-ci-x"`, `"poll_end_x"` or `"on-pushed"` for the token `pushed` (a name character, `-`,
    `_` or a letter, on either side makes it part of another name)."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
    return any(pattern.search(text) for text in strings)


def _test_function(
    path: str, name: str
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Module] | None:
    parsed = _module(path)
    if parsed is None:
        return None
    tree = parsed[0]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node, tree
    return None


def _scope(function: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.Module) -> list[ast.AST]:
    """The nodes that count as "the test's own body" for the `function` scope.

    The function and its decorators (a `parametrize` list), plus, one level deep and in the same
    module only, the functions it calls by name and the module-level constants it reads by name:
    the shared helpers and tables of a test, not the rest of its file."""
    top: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            top[node.target.id] = node.value
    scope: list[ast.AST] = [function]
    seen = {function.name}
    for sub in ast.walk(function):
        if isinstance(sub, ast.Name) and sub.id in top and sub.id not in seen:
            seen.add(sub.id)
            scope.append(top[sub.id])
    return scope


def _is_128(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 128


# The names an exit code is held in: `code` (`main()`'s return), `exc.value.code` (`SystemExit`),
# `proc.returncode` and the like. A compare with none of them is not about an exit code.
_EXIT_NAMES = {"code", "returncode", "exitcode", "exit_code", "poll_exit", "status"}


def _holds_exit_code(node: ast.AST) -> bool:
    return any(
        (isinstance(leaf, ast.Name) and leaf.id in _EXIT_NAMES)
        or (isinstance(leaf, ast.Attribute) and leaf.attr in _EXIT_NAMES)
        for leaf in ast.walk(node)
    )


def _compared_ints(nodes: list[ast.AST]) -> set[int]:
    """Every int literal an exit code is compared with in `nodes`: a compare with a `code`-like
    name on a side (`code == 12`, `exc.value.code in (0, 1)`) or a `SystemExit(2)`; -1 stands for
    a `128 + signal` sum, the signal row. `len(x) == 1` is not an exit code."""
    out: set[int] = set()
    for root in nodes:
        for sub in ast.walk(root):
            parts: list[ast.AST] = []
            if isinstance(sub, ast.Compare):
                parts = [sub.left, *sub.comparators]
                if not any(_holds_exit_code(part) for part in parts):
                    parts = []
            elif isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "SystemExit":
                parts = list(sub.args)
            for part in parts:
                for leaf in ast.walk(part):
                    if isinstance(leaf, ast.Constant) and type(leaf.value) is int:
                        out.add(leaf.value)
                    elif (
                        isinstance(leaf, ast.BinOp)
                        and isinstance(leaf.op, ast.Add)
                        and (_is_128(leaf.left) or _is_128(leaf.right))
                    ):
                        out.add(-1)
    return out


_EVENT_KEYS = {"type", "event_type", "event"}


def _asserted_strings(nodes: list[ast.AST]) -> list[str]:
    """The strings a test uses as a value rather than as prose: a string under a compare
    (`e["type"] == "ci"`, `types >= {"ci"}`), a dict value under a `type`-like key, or a
    `type=`-like keyword (`Event(type="ci")`), a `NewEvent("ci", ...)` first argument, or the value
    after a `--type` argument."""
    out: list[str] = []

    def strings(node: ast.AST) -> list[str]:
        return [
            leaf.value
            for leaf in ast.walk(node)
            if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
        ]

    for root in nodes:
        for sub in ast.walk(root):
            if isinstance(sub, ast.Compare):
                out += strings(sub)
            elif isinstance(sub, ast.Dict):
                for key, value in zip(sub.keys, sub.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value in _EVENT_KEYS:
                        out += strings(value)
            elif isinstance(sub, ast.keyword) and sub.arg in _EVENT_KEYS:
                out += strings(sub.value)
            elif isinstance(sub, ast.Call):
                args = sub.args
                if getattr(sub.func, "id", "") == "NewEvent":  # NewEvent("ci", pr_id, ...)
                    out += strings(args[0]) if args else []
                # the CLI filter: `run(capsys, "events", "--type", "renamed")`
                for flag, value in zip(args, args[1:], strict=False):
                    if isinstance(flag, ast.Constant) and flag.value == "--type":
                        out += strings(value)
    return out


# What a test must mention to count for an item: (scope, tokens). `body` is the test function
# alone; `function` adds its decorators and the same-module helpers and tables it names (`_scope`);
# `module` is its whole file (commands, whose runs sit in shared helpers); `exit` means the test
# compares the exit code literally (`signal`: a 128 + signal sum).
Mention = Callable[[str], tuple[str, tuple[str, ...]]]

MENTIONS: dict[str, Mention] = {
    "commands": lambda item: ("module", tuple(item.split())),
    "flags": lambda item: ("body", (item.split()[-1],)),
    "schemas": lambda item: ("function", (item,)),
    # a hook is named `on-ci` or, where a test builds the name from the event, `ci`
    "hooks": lambda item: ("function", (item, item.removeprefix("on-"))),
    "events": lambda item: ("function", (item,)),
    "log": lambda item: ("function", (item,)),
    "errors": lambda item: ("function", (item,)),
    "exit codes": lambda item: ("exit", (item,)),
}


def _node_problem(category: str, item: str, node_id: str) -> str | None:
    path, sep, name = node_id.partition("::")
    if not sep:
        return f"{category}: {item}: {node_id!r} is not a `file::function` node id"
    found = _test_function(path, name)
    if found is None or not name.startswith("test_"):
        return f"{category}: {item}: no test function {node_id}"
    function, tree = found
    scope, tokens = MENTIONS[category](item)
    if scope == "exit":
        want = -1 if item == "signal" else int(item)
        if want not in _compared_ints(_scope(function, tree)):
            return f"{category}: {item}: {node_id} never compares the exit code {item}"
        return None
    nodes = {"module": [tree], "body": [function], "function": _scope(function, tree)}[scope]
    if scope == "function":
        strings = _asserted_strings(nodes) if category == "events" else _strings(nodes)
        missing = list(tokens) if not any(_token_in(t, strings) for t in tokens) else []
    else:
        words: set[str] = set()
        for node in nodes:
            words |= _words(node)
        missing = [t for t in tokens if t not in words]
    if missing:
        return f"{category}: {item}: {node_id} never mentions {missing} (a {scope}-level check)"
    return None


def problems(
    found: dict[str, set[str]],
    maps: dict[str, dict[str, list[str]]],
    uncoverable: dict[str, str],
) -> list[str]:
    """Every way `maps` fails to cover `found`; empty means the audit passes."""
    out: list[str] = []
    for category, wanted in found.items():
        mapped = maps[category]
        excused = {k.split(": ", 1)[1] for k in uncoverable if k.startswith(f"{category}: ")}
        out += [f"{category}: {i}: no test mapped" for i in sorted(wanted - set(mapped) - excused)]
        out += [
            f"{category}: {i}: not an interface item (stale entry)"
            for i in sorted(set(mapped) - wanted)
        ]
        out += [f"{category}: {i}: excused and mapped" for i in sorted(excused & set(mapped))]
        out += [f"{category}: {i}: excused but not an item" for i in sorted(excused - wanted)]
        for item, ids in mapped.items():
            if item in wanted and not ids:
                out.append(f"{category}: {item}: empty test list")
            for node_id in ids:
                if item in wanted and (bad := _node_problem(category, item, node_id)):
                    out.append(bad)
    out += [f"{k}: empty reason" for k, reason in uncoverable.items() if not reason.strip()]
    return out


# --- the audit -----------------------------------------------------------------------------------


def test_every_interface_item_has_a_named_test_that_mentions_it() -> None:
    assert problems(items(), mapping(), cm.UNCOVERABLE) == []


def test_the_item_sources_are_not_empty() -> None:
    found = items()
    assert set(found) == set(CATEGORIES)
    floor = {"commands": 14, "flags": 40, "schemas": 14, "hooks": 21, "events": 20, "log": 13}
    for category, minimum in floor.items():
        assert len(found[category]) >= minimum, category
    assert {"0", "1", "2", "10", "12", "20", "signal"} <= found["exit codes"]


def test_the_global_flags_come_from_the_root_parser() -> None:
    assert {"--json", "--config", "--verbose", "--quiet", "--version"} <= set(GLOBAL_FLAGS)
    assert "--help" not in GLOBAL_FLAGS
    assert {f for f in items()["flags"] if f.startswith("* ")} == {f"* {f}" for f in GLOBAL_FLAGS}


def test_a_hook_per_event_plus_post_poll() -> None:
    found = items()
    assert found["hooks"] == {f"on-{event}" for event in found["events"]} | {"post-poll"}


def test_the_audit_reports_a_missing_item() -> None:
    maps = mapping()
    broken = {**maps, "events": {k: v for k, v in maps["events"].items() if k != "ci"}}
    assert "events: ci: no test mapped" in problems(items(), broken, cm.UNCOVERABLE)


def test_the_audit_reports_a_stale_entry() -> None:
    maps = mapping()
    broken = {
        **maps,
        "flags": {**maps["flags"], "poll --gone": ["tests/test_cli.py::test_poll_options_parse"]},
    }
    assert "flags: poll --gone: not an interface item (stale entry)" in problems(
        items(), broken, cm.UNCOVERABLE
    )


def test_the_audit_reports_a_ghost_test_and_a_test_that_does_not_mention_the_item() -> None:
    maps = mapping()
    ghost = {**maps, "log": {**maps["log"], "poll_end": ["tests/test_log.py::test_no_such_test"]}}
    assert any("no test function" in p for p in problems(items(), ghost, cm.UNCOVERABLE))
    wrong = {
        **maps,
        "flags": {
            **maps["flags"],
            "poll --full": ["tests/test_list.py::test_a_selected_watch_keeps_all_its_owners"],
        },
    }
    assert any("never mentions" in p for p in problems(items(), wrong, cm.UNCOVERABLE))
    not_a_node = {**maps, "log": {**maps["log"], "poll_end": ["tests/test_log.py"]}}
    assert any("not a `file::function`" in p for p in problems(items(), not_a_node, cm.UNCOVERABLE))
    empty = {**maps, "log": {**maps["log"], "poll_end": []}}
    assert any("empty test list" in p for p in problems(items(), empty, cm.UNCOVERABLE))


def _rejects(category: str, item: str, node_id: str, fragment: str) -> None:
    maps = mapping()
    broken = {**maps, category: {**maps[category], item: [node_id]}}
    found = problems(items(), broken, cm.UNCOVERABLE)
    assert any(fragment in p and f"{category}: {item}:" in p for p in found), found


@pytest.mark.parametrize(
    ("category", "item", "node_id", "fragment"),
    [
        # a test that exits 0 does not cover exit code 20, nor a test with no exit code at all
        ("exit codes", "20", "tests/test_cli.py::test_version_prints_version_and_exits_zero", "20"),
        ("exit codes", "0", "tests/test_cli.py::test_exit_code_table_matches_the_interface", "0"),
        # a test that compares only exit code 1 does not cover exit code 2
        (
            "exit codes",
            "2",
            "tests/test_cli_watches.py::test_a_corrupt_store_is_internal_exit_1",
            "2",
        ),
        # another hook's payload golden, or a schema test of another command, is not this item's
        (
            "hooks",
            "on-closed",
            "tests/test_hooks.py::test_the_on_pushed_payload_golden",
            "on-closed",
        ),
        ("schemas", "cli/poll", "tests/test_list.py::test_empty_store", "cli/poll"),
        (
            "events",
            "ci",
            "tests/test_diff.py::test_review_comment_line_bounded_by_store_limit",
            "ci",
        ),
        (
            "log",
            "poll_start",
            "tests/test_cli.py::test_bad_usage_exits_2_with_envelope",
            "poll_start",
        ),
        ("errors", "refused", "tests/test_cli.py::test_bad_usage_exits_2_with_envelope", "refused"),
    ],
)
def test_the_audit_rejects_a_wrong_mapping(
    category: str, item: str, node_id: str, fragment: str
) -> None:
    _rejects(category, item, node_id, fragment)


def _nodes(source: str) -> list[ast.AST]:
    return [ast.parse(source)]


def test_an_int_compared_with_something_that_is_not_an_exit_code_does_not_count() -> None:
    assert _compared_ints(_nodes("assert len(x) == 1\nassert n in (1, 2)")) == set()
    assert _compared_ints(_nodes("assert count(s, 'w') == 2 and x[0] == 1")) == set()


def test_an_int_compared_with_an_exit_code_name_counts() -> None:
    assert _compared_ints(_nodes("assert code == 1")) == {1}
    assert _compared_ints(_nodes("assert exc.value.code in (0, 2)")) == {0, 2}
    assert _compared_ints(_nodes("assert proc.returncode == 12")) == {12}
    assert _compared_ints(_nodes("assert code == 128 + number")) == {-1, 128}
    assert _compared_ints(_nodes("raise SystemExit(20)")) == {20}


def test_a_string_that_is_not_asserted_does_not_count_for_an_event() -> None:
    prose = _nodes("def test_x():\n    'a ci run'\n    log('ci')\n    names = ['ci']")
    assert not _token_in("ci", _asserted_strings(prose))
    for used in (
        "assert e['type'] == 'ci'",
        "assert types >= {'ci', 'review'}",
        "x = {'type': 'ci'}",
        "Event(type='ci')",
        "NewEvent('ci', 1)",
        "run('events', '--type', 'ci')",
    ):
        assert _token_in("ci", _asserted_strings(_nodes(used))), used


def test_a_name_inside_another_name_does_not_count() -> None:
    assert (
        _token_in("on-ci", ["hooks/on-ci", "x"]) and _token_in("ci", ["on-ci:\n- cmd: x"]) is False
    )
    assert not _token_in("review", ["review_comment", "on-review"])
    assert _token_in("review", ["a review sample"])


def test_an_uncoverable_item_needs_a_reason_and_must_exist() -> None:
    maps = mapping()
    without = {**maps, "log": {k: v for k, v in maps["log"].items() if k != "hook_cap"}}
    assert problems(items(), without, {"log: hook_cap": "needs a live host (tests/live)"}) == []
    assert any("empty reason" in p for p in problems(items(), without, {"log: hook_cap": " "}))
    assert any("not an item" in p for p in problems(items(), maps, {"log: nonesuch": "x"}))
    assert any("excused and mapped" in p for p in problems(items(), maps, {"log: hook_cap": "x"}))


def test_an_uncoverable_reason_that_points_at_live_names_a_real_file() -> None:
    for key, reason in cm.UNCOVERABLE.items():
        for path in re.findall(r"tests/live[\w/.-]*", reason):
            assert (ROOT / path).exists(), f"{key}: {path} does not exist"


# --- the docs ------------------------------------------------------------------------------------


def _synopsis_flags() -> dict[str, set[str]]:
    """{command path: flags} from the `pr-watch <command> ...` synopses of interface.md Commands."""
    section = INTERFACE.split("## Commands", 1)[1].split("\n## ", 1)[0]
    found: dict[str, set[str]] = {}
    top = {c.split()[0] for c in _leaf_commands()}
    for line in section.splitlines():
        if not line.startswith("`pr-watch "):
            continue
        for span in re.findall(r"`((?:pr-watch )?[a-z][^`]*)`", line):
            words: list[str] = []
            for token in span.removeprefix("pr-watch ").split():
                if token.startswith(("[", "<", "(", "-")):
                    break
                words.append(token)
            if not words or words[0] not in top:  # prose in code (`cron.env_file`), not a synopsis
                continue
            flags = set(re.findall(r"--[a-z][a-z-]*", span))
            found.setdefault(" ".join(words), set()).update(flags)
    return found


def test_interface_md_synopses_and_the_parser_agree_on_every_flag() -> None:
    parser = _leaf_commands()
    synopses = _synopsis_flags()
    assert set(synopses) == set(parser), set(synopses) ^ set(parser)
    for command, flags in synopses.items():
        assert flags - set(GLOBAL_FLAGS) == parser[command], command


def _coverage_doc_totals() -> dict[str, tuple[int, int]]:
    text = (ROOT / "docs" / "coverage.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([a-z ]+?) \| (\d+) \| (\d+) \| (\d+) \|", text, flags=re.M)
    return {name: (int(total), int(excused)) for name, total, _covered, excused in rows}


def test_docs_coverage_md_totals_match_the_audit() -> None:
    found = items()
    totals = _coverage_doc_totals()
    assert set(totals) == set(CATEGORIES)
    for category in CATEGORIES:
        excused = [k for k in cm.UNCOVERABLE if k.startswith(f"{category}: ")]
        assert totals[category] == (len(found[category]), len(excused)), category
    text = (ROOT / "docs" / "coverage.md").read_text(encoding="utf-8")
    for row in re.findall(r"^\| ([a-z ]+?) \| (\d+) \| (\d+) \| (\d+) \|", text, flags=re.M):
        assert int(row[2]) == int(row[1]) - int(row[3]), row  # covered = total - uncoverable


def test_docs_plan_links_to_the_coverage_audit() -> None:
    assert "coverage.md" in (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
