"""Validation of every user value before argv; offline reference parsing.

This is the argv-injection boundary (docs/interface.md: Identifiers). Rules every parser here
follows:

- Input is matched whole (`fullmatch`, never `$`), on the raw text, by ASCII patterns, before any
  normalization. Lowercasing is ASCII-only (`str.lower` would map non-ASCII characters onto
  ASCII ones), and only after an `isascii` check.
- A value that is not accepted raises `RefError`; its message is fixed text and never echoes the
  value. `code` is `invalid_pr` for PR references and `usage` for every other value, `kind` is
  the `details.kind` of the error envelope.
- What comes out is built from validated parts (`PrRef`, `Label`, canonical host and repo
  strings), so the caller never passes input text on. A `pr_id` is a separate type (`PrId`) that
  cannot be mistaken for a PR reference, and is for offline matching only: never argv, never a path.
- pr-watch never relies on `--` before positionals (design.md, Live verification V1): a value
  that starts with `-` is refused here, so no value can be read as an option.

The grammars are those of the schema's `defs.json`, tightened to what forgectl accepts at tag
v0.55.3 (userinfo, `%`, `\\`, whitespace and non-ASCII are refused in URLs; a `www.` prefix
on the built-in hosts and `:443` are normalized away) so pr-watch refuses what forgectl would
read differently.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, NamedTuple


class RefError(Exception):
    """A value failed validation. `code` is an error code of the envelope (`invalid_pr` or
    `usage`), `kind` its `details.kind`. The message never contains the value."""

    def __init__(self, code: str, message: str, kind: str) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


# --- shared patterns (config.py imports these; refs owns them) ----------------------------------


def ecma(pattern: str) -> str:
    """The same pattern for engines where `$` must not match before a trailing newline."""
    return pattern.replace("$", r"\Z")


HOST_BODY: Final = (
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)
HOST: Final = ecma(rf"^{HOST_BODY}$")
_PATH: Final = r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+"
REPO: Final = ecma(rf"^{HOST_BODY}{_PATH}$")
DURATION: Final = ecma(r"^(?:0|[0-9]{1,9}(?:ms|s|m|h|d))$")
INTERVAL: Final = ecma(r"^(?:[1-9][0-9]{0,8}m|[1-9][0-9]{0,8}h)$")
# A hook command: C0 and C1 control characters are refused (newline and tab may stay) and it
# needs a non-blank character (leading space, tab and newline are skipped, so matching is linear).
CMD: Final = ecma(
    r"^[ \t\n]*[^\u0000-\u0020\u007f-\u009f][^\u0000-\u0008\u000b-\u001f\u007f-\u009f]*$"
)

MAX_DURATION_MS: Final = 3650 * 86_400_000  # ten years
_DURATION_UNITS: Final = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def duration_ms(text: str) -> int:
    """A duration (`0`, `500ms`, `60s`, `5m`, `2h`, `30d`) in milliseconds.

    Raises ValueError for anything else, and for more than 3650d."""
    if not re.fullmatch(DURATION, text):
        raise ValueError("not a duration")
    if text == "0":
        return 0
    unit = "ms" if text.endswith("ms") else text[-1]
    value = int(text[: -len(unit)]) * _DURATION_UNITS[unit]
    if value > MAX_DURATION_MS:
        raise ValueError("duration is too large")
    return value


def interval_divides(text: str) -> bool:
    """Whether an interval (`5m`, `1h`) divides 60 minutes or 24 hours."""
    count, unit = int(text[:-1]), text[-1]
    return not (60 % count if unit == "m" else 24 % count)


_HOST_RE: Final = re.compile(HOST)
_REPO_RE: Final = re.compile(REPO)
_DURATION_RE: Final = re.compile(DURATION)
_INTERVAL_RE: Final = re.compile(INTERVAL)
_CMD_RE: Final = re.compile(CMD)

EVENT_TYPES: Final = (
    "pushed",
    "comment",
    "review_comment",
    "review",
    "approved",
    "changes_requested",
    "ci",
    "ready",
    "merged",
    "closed",
)
HOOK_NAMES: Final = (*(f"on-{t}" for t in EVENT_TYPES), "post-poll")

OWNER: Final = re.compile(r"[a-z0-9][a-z0-9_.:@-]{0,63}")
_LABEL: Final = re.compile(r"([a-z][a-z0-9_-]{0,31})=([A-Za-z0-9._:@/#+-]{1,128})")
_CURSOR: Final = re.compile(r"v1\.[a-z2-7]{8}\.[0-9]{1,18}")
_WATCH_ID: Final = re.compile(r"w-[a-z2-7]{10}")
_PR_ID: Final = re.compile(r"(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,127}")
_LIMIT: Final = re.compile(r"[1-9][0-9]{0,3}")
_SEGMENT: Final = re.compile(r"[A-Za-z0-9._-]+")
_PRINTABLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")
_QUERY_CHARS: Final = re.compile(r"[A-Za-z0-9._~=&-]*")

MAX_PR_CHARS: Final = 1024
MAX_HOST_CHARS: Final = 253 + 6  # name and `:65535`
MAX_REPO_CHARS: Final = 255
MAX_SEGMENTS: Final = 20
MAX_HOOK_CMD_CHARS: Final = 4096
MAX_OWNER_CHARS: Final = 64
_HUMAN_PREFIX: Final = "human:"
_NUMBER: Final = r"[1-9][0-9]{0,9}"
_BUILTIN_HOSTS: Final = frozenset({"github.com", "gitlab.com"})


def _ascii_lower(text: str) -> str:
    return text.lower() if text.isascii() else text


# --- hosts, paths ---------------------------------------------------------------------------


def _norm_host(text: str) -> str | None:
    """Lowercase host with the port normalized, or None. `:443` is dropped (forgectl does the
    same), a port is 1 to 65535 without a leading zero, labels are at most 63 characters."""
    if not text.isascii() or not 0 < len(text) <= MAX_HOST_CHARS:
        return None
    text = text.lower()
    if not _HOST_RE.fullmatch(text):
        return None
    name, _, port = text.partition(":")
    if len(name) > 253 or any(len(label) > 63 for label in name.split(".")):
        return None
    if port:
        if port[0] == "0" or int(port) > 65535:
            return None
        if port == "443":
            port = ""
    if name.startswith("www.") and name[4:] in _BUILTIN_HOSTS and not port:
        name = name[4:]
    return f"{name}:{port}" if port else name


def _norm_path(path: str, *, leading_slash: bool = False) -> str | None:
    """The path (`o/r`, `g/s/p`) when every segment is acceptable, else None. A segment is
    `[A-Za-z0-9._-]+`, not `.` or `..`, not starting with `-` (an option), and the last one is
    not a `.git` name. Both backends' rules at once: the conservative reading, since the
    backend of a host is config."""
    if leading_slash:
        path = path[1:]
    segments = path.split("/")
    if not 1 <= len(segments) <= MAX_SEGMENTS or len(path) > MAX_REPO_CHARS:
        return None
    for segment in segments:
        if not _SEGMENT.fullmatch(segment) or segment in (".", "..") or segment.startswith("-"):
            return None
    if segments[-1].lower().endswith(".git"):
        return None
    return path


# --- PR references --------------------------------------------------------------------------

_PR_MESSAGE: Final = "not a PR reference: use host/path#N or an https URL"


def _bad_pr() -> RefError:
    return RefError("invalid_pr", _PR_MESSAGE, "bad_pr")


@dataclass(frozen=True)
class PrRef:
    """A parsed PR reference: lowercase host, the path as typed, the number."""

    host: str
    path: str
    number: int

    @property
    def repo(self) -> str:
        return f"{self.host}/{self.path}"

    @property
    def canonical(self) -> str:
        """The `pr` form of schemas/defs.json: `host/path#N`."""
        return f"{self.repo}#{self.number}"

    @property
    def key(self) -> str:
        """The form matched against stored values: case-insensitive in host and path."""
        return self.canonical.lower()


@dataclass(frozen=True)
class PrId:
    """An opaque `pr_id`. Offline matching only: never argv, never a path."""

    value: str


_URL: Final = re.compile(r"https://([^/?#]+)(/[^?#]*)(?:\?([^#]*))?(?:#(.*))?", re.IGNORECASE)
_GITHUB_URL: Final = re.compile(
    rf"(/[^/]+/[^/]+)/pull/({_NUMBER})(?:/files|/commits|/commits/[0-9a-fA-F]{{7,40}}"
    r"|/checks|\.diff|\.patch)?"
)
_GITLAB_URL: Final = re.compile(
    rf"((?:/[^/]+)+?)(?:/-)?/merge_requests/({_NUMBER})(?:/diffs|/commits|/pipelines)?"
)
_PR_FORM: Final = re.compile(rf"([^/#]+)((?:/[^/#]+)+)#({_NUMBER})")


def _from_url(text: str) -> PrRef | None:
    match = _URL.fullmatch(text)
    if not match:
        return None
    host_text, rest, query, fragment = match.groups()
    for part in (query, fragment):
        if part is not None and not _QUERY_CHARS.fullmatch(part):
            return None
    host = _norm_host(host_text)
    if host is None:
        return None
    if rest.endswith("/"):
        rest = rest[:-1]
    if github := _GITHUB_URL.fullmatch(rest):
        path = _norm_path(github[1], leading_slash=True)
        if path is None or path.count("/") != 1:
            return None
        return PrRef(host, path, int(github[2]))
    if gitlab := _GITLAB_URL.fullmatch(rest):
        path = _norm_path(gitlab[1], leading_slash=True)
        if path is None or {"-", "merge_requests"} & set(path.split("/")):
            return None
        return PrRef(host, path, int(gitlab[2]))
    return None


def _is_url_form(text: str) -> bool:
    return text[:8].lower() == "https://"


def parse_pr(text: str) -> PrRef:
    """A PR reference: `host/path#N` or an `https://` URL (docs/interface.md: Identifiers).
    Raises RefError `invalid_pr`."""
    if not _PRINTABLE_ASCII.fullmatch(text) or len(text) > MAX_PR_CHARS or text.startswith("-"):
        raise _bad_pr()
    if _is_url_form(text):
        ref = _from_url(text)
    else:
        ref = None
        if match := _PR_FORM.fullmatch(text):
            host = _norm_host(match[1])
            path = _norm_path(match[2], leading_slash=True)
            if host is not None and path is not None:
                ref = PrRef(host, path, int(match[3]))
    if ref is None:
        raise _bad_pr()
    return ref


def parse_reference(text: str) -> PrRef | PrId:
    """A PR reference or a `pr_id`. A URL or anything with a `#` is held to the PR grammar and
    never falls back to a `pr_id`."""
    if _is_url_form(text) or "#" in text:
        return parse_pr(text)
    if len(text) <= 128 and _PR_ID.fullmatch(text):
        return PrId(text)
    raise _bad_pr()


def match_stored(ref: PrRef | PrId, rows: Iterable[tuple[str, str]]) -> str | None:
    """The `pr_id` of the stored row a reference names, from `(pr, pr_id)` rows, or None.

    A PR reference matches the stored canonical `pr` case-insensitively in host and path; a
    `pr_id` matches exactly. Two different `pr_id`s behind one name (a reused repo name) raise
    RefError `usage` (`ambiguous_reference`), with the hint to use the `pr_id`."""
    if isinstance(ref, PrId):
        return next((pr_id for _, pr_id in rows if pr_id == ref.value), None)
    found = {pr_id for pr, pr_id in rows if _ascii_lower(pr) == ref.key}
    if len(found) > 1:
        raise RefError(
            "usage",
            "the reference matches more than one stored PR; use the pr_id",
            "ambiguous_reference",
        )
    return next(iter(found), None)


# --- other values ---------------------------------------------------------------------------


def _usage(kind: str, message: str) -> RefError:
    return RefError("usage", message, kind)


def parse_host(text: str) -> str:
    """A `--host` value: lowercase host, optional port (`:443` dropped)."""
    host = _norm_host(text)
    if host is None:
        raise _usage("bad_host", "--host must be a host name with an optional port")
    return host


def parse_repo(text: str) -> str:
    """A `--repo` value: `host/<full project path>`, host lowercased."""
    host_text, slash, path_text = text.partition("/")
    host = _norm_host(host_text)
    path = _norm_path(path_text) if slash else None
    if host is None or path is None or not _REPO_RE.fullmatch(f"{host}/{path}"):
        raise _usage("bad_repo", "--repo must be host/<project path>")
    return f"{host}/{path}"


def parse_owner(text: str) -> str:
    if not OWNER.fullmatch(text):
        raise _usage(
            "bad_owner", "--owner must be lowercase letters, digits and _.:@- (64 at most)"
        )
    return text


def clean_me_name(raw: str) -> str:
    """`me.name` as the tail of an owner (design guide section 6 actor rule): ASCII-lowercase,
    anything outside the owner charset becomes `-`, a leading non-alphanumeric is stripped, and
    the rest is cut so `human:<name>` stays within the 64 characters of an owner."""
    mapped = "".join(
        ch if ch in "abcdefghijklmnopqrstuvwxyz0123456789_.:@-" else "-"
        for ch in "".join(c.lower() if c.isascii() else c for c in raw)
    )
    stripped = mapped.lstrip("_.:@-")
    return stripped[: MAX_OWNER_CHARS - len(_HUMAN_PREFIX)]


def _os_user() -> str | None:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return None


def resolve_owner(
    explicit: str | None,
    *,
    interactive: bool,
    me_name: str | None,
    os_user: Callable[[], str | None] = _os_user,
) -> str:
    """The owner of a `watch` or `label` command: `--owner` (validated), else on a terminal
    `human:<me.name>` (the OS user name when `me.name` is unset), else `owner_required`."""
    if explicit is not None:
        return parse_owner(explicit)
    if not interactive:
        raise _usage("owner_required", "--owner is required when not run from a terminal")
    name = me_name if me_name is not None else os_user()
    cleaned = clean_me_name(name or "")
    if not cleaned:
        raise _usage("owner_required", "cannot derive an owner from me.name; pass --owner")
    return f"{_HUMAN_PREFIX}{cleaned}"


class Label(NamedTuple):
    key: str
    value: str


def parse_label(text: str) -> Label:
    match = _LABEL.fullmatch(text)
    if not match:
        raise _usage("bad_label", "a label must be key=value (lowercase key, restricted value)")
    return Label(match[1], match[2])


def parse_cursor(text: str) -> str:
    """The shape of a cursor only; what it points at is the `events` command's business."""
    if not _CURSOR.fullmatch(text):
        raise _usage("bad_cursor", "not a cursor from a previous call")
    return text


def parse_duration(text: str) -> str:
    """`0` or an integer and a unit (`ms`, `s`, `m`, `h`, `d`), at most 3650d."""
    try:
        duration_ms(text)
    except ValueError:
        raise _usage(
            "bad_duration", "a duration is 0 or a number with unit ms, s, m, h or d"
        ) from None
    return text


def parse_every(text: str) -> str:
    """`--every`: `Nm` with N dividing 60 or `Nh` with N dividing 24."""
    if _INTERVAL_RE.fullmatch(text):
        try:
            duration_ms(text)
            if interval_divides(text):
                return text
        except ValueError:
            pass
    raise _usage("bad_every", "--every must be Nm with N dividing 60, or Nh with N dividing 24")


def parse_limit(text: str) -> int:
    if _LIMIT.fullmatch(text) and int(text) <= 1000:
        return int(text)
    raise _usage("bad_limit", "--limit must be an integer from 1 to 1000")


def parse_type(text: str) -> str:
    if text not in EVENT_TYPES:
        raise _usage("bad_type", "--type must be an event type")
    return text


def parse_hook(text: str) -> tuple[str, str]:
    """`--hook NAME=CMD`: NAME is one of the hook names, CMD a non-blank string of at most 4096
    characters without control, format or separator characters. CMD is a static trusted
    string that is never put in argv; control characters are refused anyway."""
    name, sep, cmd = text.partition("=")
    if (
        not sep
        or name not in HOOK_NAMES
        or len(cmd) > MAX_HOOK_CMD_CHARS
        or not _CMD_RE.fullmatch(cmd)
        or any(unicodedata.category(ch) in ("Cf", "Zl", "Zp", "Cs") for ch in cmd)
    ):
        raise _usage("bad_hook", "--hook must be NAME=CMD with a hook name and a plain command")
    return name, cmd


def parse_watch_id(text: str) -> str:
    if not _WATCH_ID.fullmatch(text):
        raise _usage("bad_watch_id", "not a watch id")
    return text


# --- watch keys -----------------------------------------------------------------------------

_KEY_REPO = rf"{HOST_BODY}(?:/(?!\.\.?(?:/|,|;|$))[A-Za-z0-9._-]+)+"
WATCH_KEY: Final = re.compile(
    rf"mine\+open(?:;host={HOST_BODY}(?:,{HOST_BODY})*)?(?:;repo={_KEY_REPO}(?:,{_KEY_REPO})*)?"
)


def watch_key(hosts: Iterable[str], repos: Iterable[str]) -> str:
    """The canonical key of the query watch `--mine --open`: `mine+open`, then, when filters
    were given, `;host=<h>,...;repo=<id>,...` with each list sorted (by code point) and
    deduplicated. Hosts and repos are the validated values of `parse_host` and `parse_repo`."""
    key = "mine+open"
    host_list = sorted({_ascii_lower(h) for h in hosts})
    repo_list = sorted(set(repos))
    if host_list:
        key += ";host=" + ",".join(host_list)
    if repo_list:
        key += ";repo=" + ",".join(repo_list)
    return key
