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

from pr_watch.cli import build_parser
from tests import coverage_map as cm
from tests.harness import ROOT, load_schema, schema_names

INTERFACE = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
GLOBAL_FLAGS = ("--json", "--config", "--verbose", "--quiet", "--version")
CATEGORIES = ("commands", "flags", "schemas", "hooks", "events", "log", "errors", "exit codes")


# --- the items, from their sources of truth ------------------------------------------------------


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


# What a test must mention to count for an item: (scope, tokens). `function` is the test's own
# body; `module` is its file (the schema checks live in shared helpers beside the tests).
Mention = Callable[[str], tuple[str, tuple[str, ...]]]

MENTIONS: dict[str, Mention] = {
    "commands": lambda item: ("module", tuple(item.split())),
    "flags": lambda item: ("function", (item.split()[-1],)),
    "schemas": lambda item: ("module", (item,)),
    "hooks": lambda item: ("module", (item.removeprefix("on-"),)),
    "events": lambda item: ("module", (item,)),
    "log": lambda item: ("module", (item,)),
    "errors": lambda item: ("module", (item,)),
    "exit codes": lambda item: ("module", ()),
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
    words = _words(function if scope == "function" else tree)
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
