"""The README's `pr-watch` command lines run for real (issue #91, an item of #43).

Every `pr-watch ...` line in a fenced shell block of README.md is extracted, expanded where it is
a synopsis (`[--every 5m|1h]`), and run through the installed `pr-watch` console script as a
subprocess, in README order so that state flows (watch add, poll, events), against the fake
forgectl and the fake crontab in a scratch HOME. Nothing reaches a host, the real crontab or the
real forgectl (tests/conftest.py).

What is deliberately not run, and why, is listed in `SKIP_REASONS`; the extractor reports every
fenced line it did not run with one of those reasons, and the test fails on any other reason, so
a new kind of README line has to be decided about here.

Not covered by the line extractor: the `Commands` bullets are inline code, not fenced blocks, so
they are checked by a separate flag-existence test (`test_the_commands_bullets_name_real_flags`).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from pr_watch.cli import build_parser
from tests.forgectl_support import FakeForgectl, new_pr
from tests.harness import ROOT, assert_valid

README = (ROOT / "README.md").read_text(encoding="utf-8")

SHELL_LANGS = {"sh", "bash", "shell", "console", "zsh"}
SKIP_REASONS = {
    "comment": "a shell comment line (the numbered steps in the quick start)",
    "other-tool": "not a pr-watch command (an installer or another program); never run",
    "not-shell": "a fenced block that is not shell (the YAML hook example is tested separately)",
}
FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w-]*)\s*$")  # indented and ~~~ fences too
FENCE_START = ("```", "~~~")
UNLABELED = {"", "text", "console"}  # a block of these is shell when it holds a pr-watch line
PLACEHOLDER = re.compile(r"<(\w+)>")
OPTIONAL = re.compile(r"\[([^\[\]]+)\]")


@dataclass(frozen=True)
class Line:
    text: str
    block: int  # index of the fenced block, so the expansion can keep README order


def fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    """(language, lines) of every fenced block (``` or ~~~, indented or not); a fence it cannot
    read and an unterminated fence are errors."""
    blocks: list[tuple[str, list[str]]] = []
    lang: str | None = None
    marker = ""
    body: list[str] = []
    for line in text.splitlines():
        if lang is None:
            if line.lstrip().startswith(FENCE_START):
                match = FENCE.match(line)
                assert match, f"unsupported fence line: {line!r}"
                marker, lang, body = match.group(1), match.group(2), []
        else:
            stripped = line.strip()
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                blocks.append((lang, body))
                lang = None
            else:
                body.append(line)
    assert lang is None, "unterminated code fence in README.md"
    return blocks


def is_shell_block(lang: str, lines: list[str]) -> bool:
    """A shell block by its language, or an unlabeled/text/console block with a `pr-watch ` line."""
    if lang in SHELL_LANGS:
        return True
    return lang in UNLABELED and any(line.strip().startswith("pr-watch ") for line in lines)


def skipped_languages(text: str) -> set[str]:
    """The languages of the fenced blocks that are not run as shell (`plain` for no language)."""
    return {
        lang or "plain" for lang, lines in fenced_blocks(text) if not is_shell_block(lang, lines)
    }


def logical_lines(lines: list[str]) -> list[str]:
    """Join `\\` continuations; a trailing ` # comment` is not stripped (none is expected)."""
    out: list[str] = []
    pending = ""
    for raw in lines:
        stripped = raw.strip()
        if stripped.endswith("\\"):
            pending += stripped[:-1].rstrip() + " "
            continue
        out.append((pending + stripped).strip())
        pending = ""
    assert not pending, "a continuation line ends the block"
    return [line for line in out if line]


def extract(text: str) -> tuple[list[Line], list[tuple[str, str]]]:
    """(command lines to run, [(skipped line, reason key)]) from the fenced blocks of `text`."""
    run: list[Line] = []
    skipped: list[tuple[str, str]] = []
    for index, (lang, lines) in enumerate(fenced_blocks(text)):
        if not is_shell_block(lang, lines):
            skipped.append((f"<{lang or 'plain'} block, {len(lines)} lines>", "not-shell"))
            continue
        for line in logical_lines(lines):
            if line.startswith("#"):
                skipped.append((line, "comment"))
            elif line.startswith("pr-watch "):
                run.append(Line(line, index))
            else:
                skipped.append((line, "other-tool"))
    return run, skipped


def expand(line: str) -> list[str]:
    """A synopsis line as runnable variants.

    `[a|b]` is an optional group: one variant leaves every group out, and one variant per
    alternative includes it (`--dry-run` always kept as a flag of its own). A line without a
    bracket is returned as is."""
    groups = OPTIONAL.findall(line)
    if not groups:
        return [line]
    bare = re.sub(r"\s*\[[^\[\]]+\]", "", line)
    variants = [bare]
    for index, group in enumerate(groups):
        head, _, tail = group.rpartition(" ")
        for alternative in tail.split("|"):
            variants.append(_only(line, index, f"{head} {alternative}".strip()))
    return list(dict.fromkeys(v.strip() for v in variants))


def _only(line: str, keep: int, alternative: str) -> str:
    """`line` with only optional group number `keep` included, as `alternative`."""
    count = -1

    def swap(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return alternative if count == keep else ""

    return re.sub(r"\s+", " ", OPTIONAL.sub(swap, line))


# --- extractor unit tests -----------------------------------------------------------------------


def test_extract_finds_commands_comments_and_other_tools() -> None:
    text = "```sh\n# one\npr-watch poll\nuv tool install x\npr-watch events \\\n  --json\n```\n"
    run, skipped = extract(text)
    assert [line.text for line in run] == ["pr-watch poll", "pr-watch events --json"]
    assert skipped == [("# one", "comment"), ("uv tool install x", "other-tool")]


def test_extract_skips_non_shell_blocks_by_name() -> None:
    text = "```yaml\nhooks: {}\n```\n```json\n{}\n```\n```text\nnothing to run\n```\n"
    run, skipped = extract(text)
    assert run == [] and [reason for _, reason in skipped] == ["not-shell"] * 3
    assert skipped_languages(text) == {"yaml", "json", "text"}


def test_an_unlabeled_text_or_console_block_with_a_pr_watch_line_is_shell() -> None:
    for lang in ("", "text", "console"):
        run, skipped = extract(f"```{lang}\npr-watch list\n```\n")
        assert [line.text for line in run] == ["pr-watch list"] and skipped == [], lang
    run, _ = extract("```json\npr-watch list\n```\n")
    assert run == []  # a language that is not shell is never promoted


def test_indented_and_tilde_fences_are_read() -> None:
    run, _ = extract("- item\n\n  ```sh\n  pr-watch list\n  ```\n\n~~~sh\npr-watch poll\n~~~\n")
    assert [line.text for line in run] == ["pr-watch list", "pr-watch poll"]
    text = "````sh\n```\npr-watch list\n````\n"  # a shorter fence inside is content
    assert len(fenced_blocks(text)) == 1
    assert [line.text for line in extract(text)[0]] == ["pr-watch list"]


def test_a_fence_that_cannot_be_read_is_an_error() -> None:
    with pytest.raises(AssertionError):
        fenced_blocks("``` sh extra\npr-watch list\n```\n")


def test_an_unterminated_fence_is_an_error() -> None:
    with pytest.raises(AssertionError):
        extract("```sh\npr-watch list\n")


def test_expand_a_synopsis_into_its_variants() -> None:
    line = "pr-watch cron install [--every 5m|1h] [--dry-run]"
    assert expand(line) == [
        "pr-watch cron install",
        "pr-watch cron install --every 5m",
        "pr-watch cron install --every 1h",
        "pr-watch cron install --dry-run",
    ]
    assert expand("pr-watch poll") == ["pr-watch poll"]


# --- the README as it is ------------------------------------------------------------------------


def test_every_skipped_readme_line_has_a_documented_reason() -> None:
    run, skipped = extract(README)
    assert run, "README.md has no pr-watch command line"
    assert {reason for _, reason in skipped} <= set(SKIP_REASONS)
    # the only block not run is the YAML hooks example (loaded as a config by its own test): an
    # unlabeled or text block of pr-watch lines would be run, any other kind fails here
    assert skipped_languages(README) == {"yaml"}
    # The one non-pr-watch shell line is the installer: running it would install software.
    assert [line for line, reason in skipped if reason == "other-tool"] == [
        "uv tool install git+https://github.com/agent-workflow-kit/pr-watch"
    ]


def pr_watch() -> str:
    found = shutil.which("pr-watch")
    assert found, "the pr-watch console script must be installed beside the interpreter"
    return found


def run_cli(line: str, placeholders: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one README line through the real console script, stdin closed (not a terminal)."""
    text = PLACEHOLDER.sub(lambda m: placeholders[m.group(1)], line)
    argv = shlex.split(text)
    assert argv[0] == "pr-watch"
    return subprocess.run(
        [pr_watch(), *argv[1:]],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


@pytest.fixture
def scratch_forgectl(fake_forgectl: FakeForgectl) -> FakeForgectl:
    """The placeholder host and PR of the quick start, at the fake forgectl."""
    fake_forgectl.host("example.com", me="user-1")
    fake_forgectl.add(new_pr(host="example.com", repo="group/project", number=42))
    return fake_forgectl


def is_cron(line: str) -> bool:
    return line.startswith("pr-watch cron ")


def test_the_readme_examples_run_in_order_against_the_fake_forgectl(
    scratch_forgectl: FakeForgectl, isolated_env: Path
) -> None:
    run, _ = extract(README)
    placeholders: dict[str, str] = {}
    ran: list[str] = []
    for example in run:
        for variant in expand(example.text):
            line = variant
            if is_cron(line) and "--dry-run" not in line:
                line += " --dry-run"  # never a real crontab write from a README line
            if "<cursor>" in line and "cursor" not in placeholders:
                pytest.fail(f"no cursor captured before: {line}")
            done = run_cli(line, placeholders)
            ran.append(line)
            assert done.returncode == 0, f"{line!r} exited {done.returncode}: {done.stderr}"
            check_shape(line, done.stdout, placeholders)
    assert not (isolated_env / ".fake-crontab").exists(), "a README line wrote the crontab"
    assert len(ran) >= 8, ran


def check_shape(line: str, stdout: str, placeholders: dict[str, str]) -> None:
    """The output shape the README shows or implies, for the commands it prints."""
    if line.startswith("pr-watch watch add example.com/group/project#42") and "--json" in line:
        doc = json.loads(stdout)
        assert_valid("cli/watch-add", doc)
        assert doc["baselined"] is True and doc["changed"] is True
        assert isinstance(doc["cursor"], str) and doc["cursor"]
        placeholders["cursor"] = doc["cursor"]  # the README's "take your cursor from this result"
    elif line.startswith("pr-watch events") and "--json" in line:
        doc = json.loads(stdout)
        assert_valid("cli/events", doc)
        assert doc["gap"] is False, "the README's step 1: the add result's cursor has no gap"
        assert set(doc) >= {"events", "cursor", "gap", "more", "last_poll_finished_at"}
    elif line.startswith("pr-watch cron") and "--dry-run" in line:
        # human text without --json: a dry run reports and writes nothing
        assert stdout.strip()
    elif line.startswith("pr-watch poll"):
        assert stdout.strip() or "--quiet" in line


def test_the_poll_the_readme_shows_reports_the_watched_pr_as_json(
    scratch_forgectl: FakeForgectl,
) -> None:
    """`poll --json` after the quick start's `watch add` is a valid report."""
    run_cli("pr-watch watch add example.com/group/project#42 --owner my-tool", {})
    done = run_cli("pr-watch poll --json", {})
    assert done.returncode == 0, done.stderr
    assert_valid("cli/poll", json.loads(done.stdout))


# --- the Commands bullets and the hook example ---------------------------------------------------


def command_help_flags(path: list[str]) -> set[str]:
    """Every long flag the real parser defines for the command `path`."""
    parser: Any = build_parser()
    for word in path:
        for action in parser._actions:  # noqa: SLF001 (argparse has no public subparser lookup)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and word in choices:
                parser = choices[word]
                break
        else:
            raise AssertionError(f"no command {' '.join(path)!r}")
    return {
        opt for action in parser._actions for opt in action.option_strings if opt.startswith("--")
    }  # noqa: SLF001


def top_level_commands() -> set[str]:
    parser: Any = build_parser()
    return {
        name
        for action in parser._actions  # noqa: SLF001
        for name in (getattr(action, "choices", None) or {})
        if isinstance(getattr(action, "choices", None), dict)
    }


def bullet_synopses(text: str) -> list[str]:
    """Inline `pr-watch ...` code spans of the README's bullets (the `Commands` list), each with
    the bare `<command> ...` spans after it on its line (`pr-watch prune`, `cron install`), which
    are returned with the `pr-watch ` they leave out."""
    spans: list[str] = []
    commands = top_level_commands()
    for line in text.splitlines():
        if not line.startswith("- `pr-watch "):
            continue
        after_first = False
        for span in re.findall(r"`([^`]+)`", line):
            if span.startswith("pr-watch "):
                spans.append(span)
                after_first = True
            elif after_first and span.split()[0] in commands:
                spans.append(f"pr-watch {span}")
    return spans


def unknown_flags(span: str) -> set[str]:
    """The flags `span` names that its command (or every command of `a|b`) does not have."""
    words = []
    for token in span.split()[1:]:
        if token.startswith(("[", "<", "(", "-")):
            break
        words.append(token.rstrip(","))
    paths: list[list[str]] = [[]]
    for word in words:
        paths = [p + [w] for p in paths for w in word.split("|")]
    flags = set(re.findall(r"--[a-z][a-z-]*", span))
    known = {"--json", "--config", "--verbose", "--quiet"}
    for path in paths:
        known |= command_help_flags(path)
    return flags - known


def test_bare_command_spans_after_a_pr_watch_span_are_checked_too() -> None:
    line = (
        "- `pr-watch prune [--dry-run]`, `cron install|uninstall [--dry-run]`, `show <pr>`, `--x`"
    )
    assert bullet_synopses(line) == [
        "pr-watch prune [--dry-run]",
        "pr-watch cron install|uninstall [--dry-run]",
        "pr-watch show <pr>",
    ]
    assert unknown_flags("pr-watch cron install|uninstall [--dry-run]") == set()
    assert unknown_flags("pr-watch cron install|uninstall [--nope]") == {"--nope"}


def test_the_commands_bullets_name_real_flags() -> None:
    spans = bullet_synopses(README)
    assert len(spans) >= 8
    assert "pr-watch cron install|uninstall [--dry-run]" in spans  # a bare span of the README
    for span in spans:
        assert not unknown_flags(span), f"{span!r}: unknown {sorted(unknown_flags(span))}"


def test_the_hooks_yaml_example_loads_as_a_valid_config(
    scratch_forgectl: FakeForgectl, tmp_path: Path
) -> None:
    blocks = [lines for lang, lines in fenced_blocks(README) if lang == "yaml"]
    assert len(blocks) == 1
    config = tmp_path / "config.yaml"
    config.write_text("\n".join(blocks[0]) + "\n", encoding="utf-8")
    config.chmod(0o600)
    assert isinstance(yaml.safe_load(config.read_text(encoding="utf-8")), dict)
    done = run_cli(f"pr-watch validate --json --config {shlex.quote(str(config))}", {})
    assert done.returncode == 0, done.stdout + done.stderr
    assert_valid("cli/validate", json.loads(done.stdout))
