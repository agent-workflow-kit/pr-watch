"""README and config.example.yaml stay aligned with the interface (issue #30)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from pr_watch.cli import EXIT_CODES
from tests.harness import ROOT, load_schema

README = (ROOT / "README.md").read_text(encoding="utf-8")
INTERFACE = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "config.example.yaml").read_text(encoding="utf-8")


def _table_rows(text: str, heading: str) -> dict[str, str]:
    """The `| code | meaning |` rows of the table that follows `heading`."""
    match = re.search(rf"^{re.escape(heading)}\s*$", text, flags=re.M)
    assert match, f"heading {heading!r} not found"
    section = text[match.end() :]
    rows: dict[str, str] = {}
    started = False
    for line in section.splitlines():
        if line.startswith("|"):
            started = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] not in ("Code", "---"):
                rows[cells[0]] = cells[1]
        elif started:
            break
    return rows


def test_readme_exit_codes_match_the_interface_table() -> None:
    readme = _table_rows(README, "## Exit codes")
    interface = _table_rows(INTERFACE, "## Exit codes")
    # The README adds the signal row; every other code is the interface's.
    assert readme.pop("128+N").startswith("ended by signal")
    assert set(readme) == set(interface)


def test_readme_exit_codes_cover_every_code_the_cli_can_return() -> None:
    readme = {c for c in _table_rows(README, "## Exit codes") if c.isdigit()}
    assert {str(code) for code in EXIT_CODES.values()} | {"0"} <= readme


def _leaf_keys(node: dict[str, Any]) -> Iterator[str]:
    for name, sub in node.get("properties", {}).items():
        yield name
        yield from _leaf_keys(sub)


def _defines_key(text: str, key: str) -> bool:
    """True when `key:` starts a line, active or commented out (not prose)."""
    return re.search(rf"^\s*#?\s*{re.escape(key)}:", text, flags=re.M) is not None


def test_defines_key_ignores_prose_and_accepts_commented_keys() -> None:
    assert _defines_key("a:\n  timeout: 5\n", "timeout")
    assert _defines_key("#   timeout: 5\n", "timeout")
    assert not _defines_key("# the timeout is five seconds\n", "timeout")
    assert not _defines_key("a:\n  other: 1  # timeout: x\n", "timeout")


def test_example_names_every_config_key() -> None:
    missing = [k for k in _leaf_keys(load_schema("config")) if not _defines_key(EXAMPLE, k)]
    assert missing == []


def test_example_check_fails_when_a_key_line_is_removed() -> None:
    key = next(iter(_leaf_keys(load_schema("config"))))
    stripped = "\n".join(
        line for line in EXAMPLE.splitlines() if not re.match(rf"^\s*#?\s*{re.escape(key)}:", line)
    )
    assert not _defines_key(stripped, key)


def test_example_has_a_comment_on_every_active_key() -> None:
    bare = [
        line
        for line in EXAMPLE.splitlines()
        if re.match(r"^\s*[A-Za-z_][\w-]*:", line)
        and "#" not in line
        and not line.rstrip().endswith(":")
    ]
    assert bare == []


def _slugs(text: str) -> set[str]:
    """GitHub-style heading anchors: skips fenced code, strips backticks, numbers duplicates."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        match = None if fenced else re.match(r"^#+ (.+?)\s*$", line)
        if not match:
            continue
        title = match.group(1).replace("`", "").lower()
        base = re.sub(r"[^a-z0-9 _-]", "", title).replace(" ", "-")
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def test_slugs_skip_fences_number_duplicates_and_strip_backticks() -> None:
    text = "# A `b`\n```sh\n# not a heading\n```\n## Same\n## Same\n"
    assert _slugs(text) == {"a-b", "same", "same-1"}


def test_table_rows_needs_a_heading_at_line_start() -> None:
    text = "see ## Exit codes in prose\n## Exit codes\n| Code | Meaning |\n|---|---|\n| 0 | ok |\n"
    assert _table_rows(text, "## Exit codes") == {"0": "ok"}


def _relative_links(text: str) -> list[str]:
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    return [t for t in targets if not re.match(r"^[a-z]+:", t) and not t.startswith("#")]


@pytest.mark.parametrize("target", _relative_links(README))
def test_readme_relative_links_resolve(target: str) -> None:
    path, _, anchor = target.partition("#")
    assert (ROOT / path).exists()
    if anchor and path.endswith(".md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert anchor in _slugs(text)
