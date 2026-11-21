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
    section = text.split(heading, 1)[1]
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


def test_example_names_every_config_key() -> None:
    missing = [
        k
        for k in _leaf_keys(load_schema("config"))
        if not re.search(rf"\b{re.escape(k)}\b", EXAMPLE)
    ]
    assert missing == []


def test_example_has_a_comment_on_every_active_key() -> None:
    bare = [
        line
        for line in EXAMPLE.splitlines()
        if re.match(r"^\s*[A-Za-z_][\w-]*:", line)
        and "#" not in line
        and not line.rstrip().endswith(":")
    ]
    assert bare == []


def _relative_links(text: str) -> list[str]:
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    return [t for t in targets if not re.match(r"^[a-z]+:", t) and not t.startswith("#")]


@pytest.mark.parametrize("target", _relative_links(README))
def test_readme_relative_links_resolve(target: str) -> None:
    path, _, anchor = target.partition("#")
    assert (ROOT / path).exists()
    if anchor and path.endswith(".md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        slugs = {
            re.sub(r"[^a-z0-9 _-]", "", h.lower()).replace(" ", "-")
            for h in re.findall(r"^#+ (.+)$", text, flags=re.M)
        }
        assert anchor in slugs
