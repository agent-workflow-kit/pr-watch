"""Shared text sanitizing: invisible code points become markers."""

from __future__ import annotations

import pytest

from pr_watch.text import bounded_one_line, one_line, sanitized, scrub_text

INVISIBLE = [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x17B4, 0x17B5]


@pytest.mark.parametrize("code", INVISIBLE)
def test_invisible_code_points_become_markers(code: int) -> None:
    marker = f"<U+{code:04X}>"
    text = f"a{chr(code)}b"
    assert one_line(text) == f"a{marker}b"
    assert scrub_text(text) == f"a{marker}b"
    assert sanitized(text) == f"a{marker}b"
    assert bounded_one_line(text) == f"a{marker}b"


def test_variation_selectors_and_plain_text_are_kept() -> None:
    assert one_line("❤️ ok é") == "❤️ ok é"
