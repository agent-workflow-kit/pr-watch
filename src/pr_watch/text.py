"""Text sanitizing shared by the CLI and the log writer: one-line, length-capped, control-free."""

from __future__ import annotations

import re
import unicodedata

MAX_MESSAGE_CHARS = 256


def _tokens(text: str, keep: str = "") -> list[str]:
    """Each character, or a `<U+XXXX>` marker for a control, format (bidi), line/paragraph
    separator or surrogate character (`keep` lists control characters to leave alone)."""
    out: list[str] = []
    for ch in text:
        if ch not in keep and unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Cs"):
            out.append(f"<U+{ord(ch):04X}>")
        else:
            out.append(ch)
    return out


def one_line(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Sanitized one-line text of at most `limit` characters, cut at a marker boundary with a
    trailing `...` kept inside the limit."""
    tokens = _tokens(text)
    if sum(map(len, tokens)) <= limit:
        return "".join(tokens)
    kept: list[str] = []
    size = 0
    for token in tokens:
        if size + len(token) > limit - 3:
            break
        kept.append(token)
        size += len(token)
    return "".join(kept) + "..."


def scrub_text(text: str) -> str:
    """Multi-line human text: markers for control characters other than newline and tab."""
    return "".join(_tokens(text, keep="\n\t"))


_TOKEN = re.compile(r"<U\+[0-9A-F]{4,6}>|.", re.DOTALL)


def sanitized(text: str) -> str:
    """The whole text with control, format, separator and surrogate characters replaced by
    `<U+XXXX>` markers (no length limit)."""
    return "".join(_tokens(text))


def bounded_one_line(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Sanitized one-line text of at most `limit` characters, cut only at a marker boundary
    (a `<U+XXXX>` marker, newly made or already in the text, is never split) with a trailing
    `...` kept inside the limit."""
    clean = sanitized(text)
    if len(clean) <= limit:
        return clean
    kept: list[str] = []
    size = 0
    for token in _TOKEN.findall(clean):
        if size + len(token) > limit - 3:
            break
        kept.append(token)
        size += len(token)
    return "".join(kept) + "..."
