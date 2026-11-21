"""The forgectl client: argv from validated parts, a deadline and byte cap, strict parsing, version.

What this module does (docs/interface.md: Failures, Version rule, Resolution):

- Builds argv for `prs`, `snapshot` (always `--no-bodies`), `show`, `compare` and `whoami` from
  validated values only:
  a `refs.PrRef` (its `canonical` form, re-validated here), a host or repo id that round-trips
  through `refs.parse_host`/`parse_repo`, an aware datetime, a 40-hex sha, a fixed state set.
  Options are `--option=value`, a positional always starts with a host (never `-`), and `--` is
  never used. A `PrId` or any free string is a TypeError/ValueError, never argv.
- Runs one call through the gateway: the caller's environment is passed to forgectl unchanged
  (`env`; forgectl reads its tokens and config location from it), the wall-clock deadline is
  `forgectl.timeout` plus `deadline_slack` (10 s), stdout is read under `max_output` bytes, and
  the gateway signals the child's group (SIGTERM, then SIGKILL; during a poll's shutdown the
  SIGKILL follows `gateway.SHUTDOWN_KILL_AFTER`, 5 s, after the shutdown began).
- Parses stdout strictly: UTF-8 decoded explicitly, one JSON object, no duplicate keys, no NaN or
  Infinity, every integer and float bounded, nesting at most MAX_DEPTH levels (checked after the
  parse by an iterative walk; a deeper document that the parser itself cannot read is also a
  `contract` failure), no lone surrogate in any key or string; the object must have the required
  fields of the command's pinned schema (`REQUIRED`, kept equal to the schema by a test) and the
  fields the digest reads must have the right JSON types (`_SHAPE`; enums are not checked, a newer
  minor may add values).
- Returns a structured raw `Outcome` and nothing more: the exit code, the parsed result (a
  complete one, or a partial `prs`/`whoami` object at exit 12), the parsed error envelope, the
  local failure (`too_large`, `killed`, `contract`) and the gateway's facts. Mapping an outcome to
  a counter, a pause or a failure record is the failure table's job (`pr_watch.failures`); exit
  codes above 128 and 137 are left to it as raw codes.
- Resolves the executable once against absolute `PATH` entries only and checks `--version`
  (`MIN_FORGECTL`) before every other call (cached once it succeeded): `ToolMissing` and
  `ToolTooOld` map to the exit 12 errors `tool_missing` and `tool_too_old`.

Text from forgectl (error messages, details) is untrusted: it is made one line, stripped of
control characters and cut to MAX_MESSAGE characters before it leaves this module, and only a
whitelist of detail keys is kept.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pr_watch import gateway
from pr_watch.config import Forgectl
from pr_watch.refs import PrRef, RefError, duration_ms, parse_host, parse_pr, parse_repo

MIN_FORGECTL: Final = (0, 55)
INSTALL: Final = "uv tool install git+https://github.com/agent-workflow-kit/forgectl"
DEADLINE_SLACK: Final = 10.0  # seconds added to forgectl.timeout for the call deadline
VERSION_MAX_OUTPUT: Final = 4096
MAX_RETRY_AFTER: Final = 3600.0  # Pacing and backoff: a larger retry_after is clamped
MAX_MESSAGE: Final = 200
MAX_INT: Final = 10**15  # every integer read from forgectl is below this in magnitude
_MAX_INT_CHARS: Final = 17
MAX_LIMIT: Final = 1000
MAX_DEPTH: Final = 32  # nesting levels of one forgectl document, the top object being level 1
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
STATES: Final = frozenset({"open", "closed", "merged", "all"})
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_VERSION: Final = re.compile(r"forgectl ([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})", re.ASCII)
_CODE: Final = re.compile(r"[a-z_]{1,64}", re.ASCII)
_DETAIL_TEXT: Final = ("install", "variable", "key")
_DETAIL_INT: Final = ("status",)

# Top-level required fields of forgectl's pinned schemas (tests/forgectl_pinned/schemas/cli).
REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "prs": ("items", "complete", "errors", "hosts_queried", "rate_limit"),
    "whoami": ("hosts", "errors"),
    "compare": ("pr", "from_sha", "to_sha", "status"),
    "show": (
        "pr", "repo", "repo_id", "pr_id", "number", "url", "backend", "title", "body", "author",
        "state", "draft", "created_at", "updated_at", "closed_at", "merged_at", "merged_by",
        "labels", "author_association", "head", "base", "merged_head_sha", "merge_commit_sha",
        "head_fetch_ref", "mergeable", "blocked_by", "auto_merge", "merge_queue",
        "merge_methods_allowed", "additions", "deletions", "changed_files", "review_decision",
        "approvals", "approvals_at_head", "approvals_left",
    ),
    "snapshot": (
        "pr", "repo", "repo_id", "pr_id", "number", "url", "backend", "title", "body", "author",
        "state", "draft", "created_at", "updated_at", "closed_at", "merged_at", "merged_by",
        "labels", "author_association", "head", "base", "merged_head_sha", "merge_commit_sha",
        "head_fetch_ref", "mergeable", "blocked_by", "auto_merge", "merge_queue",
        "merge_methods_allowed", "additions", "deletions", "changed_files", "review_decision",
        "approvals", "approvals_at_head", "approvals_left", "review_requests", "teams_visible",
        "reviews", "latest_by_reviewer", "threads", "ci", "activity", "rate_limit", "complete",
        "incomplete", "fetched_at",
    ),
}  # fmt: skip
# What the digest reads, by JSON type, after forgectl's pinned schemas. A spec is a token below, a
# dict (an object that has these fields, others are ignored) or a one-item list (a list whose
# items all match). Enums are not checked: a newer minor may add values.
_STR: Final = "str"  # any string
_NSTR: Final = "nstr"  # a non-empty string
_ID: Final = "id"  # a non-empty string without control characters
_NUM: Final = "num"  # an integer of at least 1
_BOOL: Final = "bool"
_OBJ: Final = "obj"
_LIST: Final = "list"
_HEAD: Final = {"sha": _NSTR}
_PR_FIELDS: Final[dict[str, Any]] = {
    "pr": _NSTR, "repo": _NSTR, "pr_id": _ID, "number": _NUM, "title": _STR, "state": _STR,
    "draft": _BOOL, "labels": [_STR], "head": _HEAD,
}  # fmt: skip
_SHAPE: Final[dict[str, dict[str, Any]]] = {
    "prs": {
        "items": [
            {"pr": _NSTR, "pr_id": _ID, "number": _NUM, "state": _STR, "head": _HEAD}
        ],
        "complete": _BOOL,
        "errors": [{"code": _NSTR}],
        "hosts_queried": [_NSTR],
        "rate_limit": _OBJ,
    },
    "whoami": {"hosts": [{"host": _NSTR}], "errors": [{"code": _NSTR}]},
    "compare": {"pr": _NSTR, "from_sha": _NSTR, "to_sha": _NSTR, "status": _NSTR},
    "show": _PR_FIELDS,
    "snapshot": {
        **_PR_FIELDS,
        "complete": _BOOL,
        "incomplete": [_STR],
        "ci": {"state": _NSTR},
        "threads": [_OBJ],
        "reviews": [_OBJ],
        "review_requests": [_OBJ],
        "latest_by_reviewer": [_OBJ],
        "activity": _OBJ,
        "rate_limit": _OBJ,
        "fetched_at": _STR,
    },
}  # fmt: skip


class ToolError(Exception):
    """forgectl cannot be used at all (exit 12 for `poll`)."""

    def __init__(self, message: str, install: str = INSTALL) -> None:
        super().__init__(message)
        self.message = message
        self.install = install


class ToolMissing(ToolError):
    """forgectl is missing, not executable or `--version` could not run: `tool_missing`."""


class ToolTooOld(ToolError):
    """`--version` is below the minimum, unparseable or exited non-zero: `tool_too_old`."""

    def __init__(self, found: str, required: str) -> None:
        super().__init__(f"forgectl {found} found, {required} or newer required")
        self.found = found
        self.required = required


@dataclass(frozen=True)
class ToolVersion:
    text: str
    major: int
    minor: int
    patch: int


@dataclass(frozen=True)
class ForgectlError:
    """A parsed error envelope: bounded, one-line text and whitelisted details."""

    code: str
    message: str
    reason: str | None = None
    retry_after: float | None = None  # seconds, clamped to MAX_RETRY_AFTER
    details: Mapping[str, str | int] | None = None


@dataclass(frozen=True)
class Outcome:
    """One forgectl call, raw. `local` is `too_large`, `killed`, `contract` or None.

    With `local` None: `result` is the command's object (exit 0, or a partial prs/whoami object
    at exit 12: `partial`), or `error` is the envelope; both None means a non-zero exit with no
    envelope to read (128+N). `detail` explains a `contract` failure (our words, no forgectl text).
    """

    command: str
    returncode: int | None
    result: dict[str, Any] | None = None
    error: ForgectlError | None = None
    local: str | None = None
    detail: str = ""
    timed_out: bool = False
    too_large: bool = False
    abandoned: bool = False
    elapsed: float = 0.0

    @property
    def partial(self) -> bool:
        return self.result is not None and self.returncode not in (0, None)


@dataclass(frozen=True)
class Expect:
    """What a call asked for, to check the answer against: compare is (from, to, no_diff)."""

    compare: tuple[str, str, bool] | None = None


# --- version --------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`forgectl X.Y.Z` plus at most one trailing newline, ASCII digits, else None."""
    body = text[:-1] if text.endswith("\n") else text
    match = _VERSION.fullmatch(body)
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def version_accepted(found: tuple[int, int, int], minimum: tuple[int, int] = MIN_FORGECTL) -> bool:
    """Same major, minor at least the minimum's (0.x included); the patch is ignored."""
    return found[0] == minimum[0] and found[1] >= minimum[1]


# --- resolution -----------------------------------------------------------------------------


def resolve_executable(path: str, name: str = "forgectl") -> str | None:
    """The first executable regular file `name` in an absolute entry of `path`, else None."""
    for entry in path.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# --- argv -----------------------------------------------------------------------------------


def _timeout_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("timeout_ms must be an int")
    if value < 1:
        raise ValueError("timeout_ms must be positive")
    return value


def _tail(timeout_ms: object) -> list[str]:
    return ["--json", f"--timeout={_timeout_ms(timeout_ms)}ms"]


def _canonical(ref: object) -> str:
    if not isinstance(ref, PrRef):
        raise TypeError(f"a PrRef is required, got {type(ref).__name__}")
    try:
        again = parse_pr(ref.canonical)
    except RefError as exc:
        raise ValueError("PrRef is not a valid reference") from exc
    if again != ref or ref.canonical.startswith("-"):
        raise ValueError("PrRef is not a valid reference")
    return ref.canonical


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("a sha is 40 lowercase hex characters")
    return value


def _host(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("host must be str")
    try:
        host = parse_host(value)
    except RefError as exc:
        raise ValueError("invalid host") from exc
    if host != value:
        raise ValueError("host is not in its canonical form")
    return host


def _repo(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("repo must be str")
    try:
        repo = parse_repo(value)
    except RefError as exc:
        raise ValueError("invalid repo") from exc
    if repo != value:
        raise ValueError("repo is not in its canonical form")
    return repo


def _timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise TypeError("updated_since must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("updated_since must be timezone-aware")
    try:
        utc = value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("updated_since is outside the UTC range") from exc
    text = (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
    )
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text + "Z"


def prs_argv(
    *,
    timeout_ms: int,
    host: str | None = None,
    repo: str | None = None,
    author: str | None = None,
    state: str | None = None,
    updated_since: datetime | None = None,
    limit: int | None = None,
) -> list[str]:
    """`prs` for exactly one of a host or a repo (forgectl refuses both together)."""
    if (host is None) == (repo is None):
        raise ValueError("exactly one of host and repo is required")
    if host is not None and author is None:
        raise ValueError("a host listing needs author='me' (forgectl refuses it without --repo)")
    argv = ["prs", f"--host={_host(host)}" if host is not None else f"--repo={_repo(repo)}"]
    if author is not None:
        if author != "me":
            raise ValueError("author must be 'me'")
        argv.append("--author=me")
    if state is not None:
        if state not in STATES:
            raise ValueError("state must be open, closed, merged or all")
        argv.append(f"--state={state}")
    if updated_since is not None:
        argv.append(f"--updated-since={_timestamp(updated_since)}")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an int")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError("limit must be 1 to 1000")
        argv.append(f"--limit={limit}")
    return [*argv, *_tail(timeout_ms)]


def snapshot_argv(ref: PrRef, timeout_ms: int) -> list[str]:
    """`snapshot --no-bodies`: the digest, the diff and every event payload read only ids,
    authors, states, shas and times, so the bodies of reviews and comments (the part an author
    can pad past the output cap) are left out. The PR's own `body` stays (forgectl keeps it)."""
    return ["snapshot", _canonical(ref), "--no-bodies", *_tail(timeout_ms)]


def show_argv(ref: PrRef, timeout_ms: int) -> list[str]:
    return ["show", _canonical(ref), *_tail(timeout_ms)]


def compare_argv(
    ref: PrRef, from_sha: str, to_sha: str, *, no_diff: bool, timeout_ms: int
) -> list[str]:
    argv = ["compare", _canonical(ref), f"--from={_sha(from_sha)}", f"--to={_sha(to_sha)}"]
    if no_diff:
        argv.append("--no-diff")
    return [*argv, *_tail(timeout_ms)]


def whoami_argv(timeout_ms: int) -> list[str]:
    return ["whoami", *_tail(timeout_ms)]


# --- parsing --------------------------------------------------------------------------------


def _bounded_int(text: str) -> int:
    if len(text) > _MAX_INT_CHARS:
        raise ValueError("integer too long")
    value = int(text)
    if abs(value) >= MAX_INT:
        raise ValueError("integer out of range")
    return value


def _bounded_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or abs(value) >= MAX_INT:
        raise ValueError("number out of range")
    return value


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _one_line(text: str) -> str:
    cleaned = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(cleaned.split())[:MAX_MESSAGE]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_error(raw: Any) -> ForgectlError | str:
    """The envelope's `error` object, or the reason it is malformed."""
    if not isinstance(raw, dict):
        return "error is not an object"
    code, message = raw.get("code"), raw.get("message")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return "error.code is not a code"
    if not isinstance(message, str):
        return "error.message is not a string"
    reason = raw.get("reason")
    if reason is not None and (not isinstance(reason, str) or not _CODE.fullmatch(reason)):
        return "error.reason is not a reason"
    retry = raw.get("retry_after")
    if retry is not None:
        if not _is_number(retry) or retry < 0:
            return "error.retry_after is not a non-negative number"
        retry = max(0.0, min(float(retry), MAX_RETRY_AFTER))  # -0.0 comes out as 0.0
    details = raw.get("details")
    if details is not None and not isinstance(details, dict):
        return "error.details is not an object"
    kept: dict[str, str | int] = {}
    for key, value in (details or {}).items():
        if key in _DETAIL_TEXT and isinstance(value, str):
            kept[key] = _one_line(value)
        elif key in _DETAIL_INT and value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
                return f"error.details.{key} is not a bounded integer"
            kept[key] = value
    return ForgectlError(
        code=code,
        message=_one_line(message),
        reason=reason,
        retry_after=retry,
        details=kept or None,
    )


def _has_control(text: str) -> bool:
    return any(c < " " or "\x7f" <= c <= "\x9f" for c in text)


def _kind_problem(value: object, spec: Any, path: str) -> str | None:
    """Why `value` does not match `spec`, naming the field path (our words only), or None."""
    wrong = f"field {path} has the wrong type"
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return wrong
        for name, sub in spec.items():
            if name not in value:
                return f"missing required field(s): {path}.{name}"
            problem = _kind_problem(value[name], sub, f"{path}.{name}")
            if problem:
                return problem
        return None
    if isinstance(spec, list):
        if not isinstance(value, list):
            return wrong
        for index, item in enumerate(value):
            problem = _kind_problem(item, spec[0], f"{path}[{index}]")
            if problem:
                return problem
        return None
    ok = {
        _STR: lambda v: isinstance(v, str),
        _NSTR: lambda v: isinstance(v, str) and v != "",
        _ID: lambda v: isinstance(v, str) and v != "" and not _has_control(v),
        _NUM: lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
        _BOOL: lambda v: isinstance(v, bool),
        _OBJ: lambda v: isinstance(v, dict),
        _LIST: lambda v: isinstance(v, list),
    }[spec]
    return None if ok(value) else wrong


def _shape_problem(command: str, doc: dict[str, Any]) -> str | None:
    missing = [name for name in REQUIRED[command] if name not in doc]
    if missing:
        return "missing required field(s): " + ", ".join(missing)
    return _kind_problem(doc, _SHAPE[command], "$")


def _walk_problem(doc: object) -> str | None:
    """Depth and text checks over the whole document, iteratively (no recursion to exhaust)."""
    stack: list[tuple[object, int]] = [(doc, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, str):
            if _SURROGATE.search(node):
                return "a string holds a lone surrogate"
        elif isinstance(node, (dict, list)):
            if depth > MAX_DEPTH:
                return "nesting is too deep"
            if isinstance(node, dict):
                for key, value in node.items():
                    if _SURROGATE.search(key):
                        return "a key holds a lone surrogate"
                    stack.append((value, depth + 1))
            else:
                stack.extend((item, depth + 1) for item in node)
    return None


def _compare_problem(doc: dict[str, Any], expect: Expect | None) -> str | None:
    if expect is None or expect.compare is None:
        return None
    from_sha, to_sha, no_diff = expect.compare
    if doc["from_sha"] != from_sha or doc["to_sha"] != to_sha:
        return "compare answered for other shas"
    if not no_diff and "diff" not in doc:
        return "missing required field(s): diff"
    return None


def _local(command: str, res: gateway.Result, local: str | None, detail: str = "") -> Outcome:
    return Outcome(
        command=command,
        returncode=res.returncode,
        local=local,
        detail=detail,
        timed_out=res.timed_out,
        too_large=res.too_large,
        abandoned=res.abandoned,
        elapsed=res.elapsed,
    )


def classify(command: str, res: gateway.Result, *, expect: Expect | None = None) -> Outcome:
    """Turn a gateway result into an Outcome. Pure: no I/O."""
    if command not in REQUIRED:
        raise ValueError(f"unknown forgectl command {command!r}")
    code = res.returncode
    if res.too_large:
        return _local(command, res, "too_large")
    if res.timed_out or res.abandoned or code is None or code < 0:
        return _local(command, res, "killed")
    if code > 128:  # 128+N: a signal ended forgectl, no envelope; the failure table decides
        return _local(command, res, None)

    def bad(detail: str) -> Outcome:
        return _local(command, res, "contract", detail)

    try:
        raw = res.stdout.encode("utf-8", "surrogateescape")
        doc = json.loads(
            raw.decode("utf-8"),
            parse_int=_bounded_int,
            parse_float=_bounded_float,
            parse_constant=_no_constant,
            object_pairs_hook=_unique,
        )
    except UnicodeDecodeError:
        return bad("stdout is not valid UTF-8")
    except (ValueError, RecursionError):
        return bad("stdout is not one bounded JSON document")
    if not isinstance(doc, dict):
        return bad("stdout is not a JSON object")
    walked = _walk_problem(doc)
    if walked:
        return bad(walked)

    def with_result() -> Outcome:
        problem = _shape_problem(command, doc) or (
            _compare_problem(doc, expect) if command == "compare" else None
        )
        if problem:
            return bad(problem)
        return Outcome(
            command=command,
            returncode=code,
            result=doc,
            timed_out=res.timed_out,
            too_large=res.too_large,
            abandoned=res.abandoned,
            elapsed=res.elapsed,
        )

    if code == 0:
        return with_result()
    if code == 12 and command in ("prs", "whoami") and "error" not in doc:
        return with_result()  # partial results: the command's object with `errors`
    if not isinstance(doc.get("error"), dict):
        return bad("stdout is not an error envelope")
    error = _parse_error(doc["error"])
    if isinstance(error, str):
        return bad(error)
    return Outcome(
        command=command,
        returncode=code,
        error=error,
        timed_out=res.timed_out,
        too_large=res.too_large,
        abandoned=res.abandoned,
        elapsed=res.elapsed,
    )


# --- the client -----------------------------------------------------------------------------


class ForgectlClient:
    """One resolved forgectl and its call settings. Build one per process (poll, `watch add`)."""

    def __init__(
        self,
        exe: str,
        *,
        timeout_ms: int,
        max_output: int,
        env: Mapping[str, str],
        deadline_slack: float = DEADLINE_SLACK,
        term_grace: float = gateway.TERM_GRACE,
    ) -> None:
        if not os.path.isabs(exe):
            raise ValueError("exe must be an absolute path")
        self.exe = exe
        self.timeout_ms = _timeout_ms(timeout_ms)
        if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 1:
            raise ValueError("max_output must be a positive int (bytes)")
        self.max_output = max_output
        self.env = dict(env)
        self.deadline = self.timeout_ms / 1000 + deadline_slack
        self.term_grace = term_grace
        self._version: ToolVersion | None = None

    @classmethod
    def from_config(cls, cfg: Forgectl, *, env: Mapping[str, str] | None = None) -> ForgectlClient:
        """Resolve forgectl against the absolute `PATH` entries of `env` (default: the process
        environment, which is also what the child gets, unchanged); ToolMissing if absent."""
        environment = dict(os.environ if env is None else env)
        exe = resolve_executable(environment.get("PATH", ""))
        if exe is None:
            raise ToolMissing("forgectl was not found in an absolute PATH entry")
        return cls(
            exe,
            timeout_ms=duration_ms(cfg.timeout),
            max_output=cfg.max_output_mib * 1024 * 1024,
            env=environment,
        )

    def _run(self, argv: list[str], *, max_output: int) -> gateway.Result:
        try:
            return gateway.run(
                [self.exe, *argv],
                env=self.env,
                timeout=self.deadline,
                max_output=max_output,
                term_grace=self.term_grace,
            )
        except gateway.LaunchError as exc:
            raise ToolMissing(f"forgectl could not be started: {_one_line(str(exc))}") from exc

    def _call(self, command: str, argv: list[str], expect: Expect | None = None) -> Outcome:
        self.version()  # fail closed: no call to an unchecked forgectl
        return classify(command, self._run(argv, max_output=self.max_output), expect=expect)

    def version(self) -> ToolVersion:
        """`forgectl --version` checked against MIN_FORGECTL; cached on success only."""
        if self._version is not None:
            return self._version
        res = self._run(["--version"], max_output=VERSION_MAX_OUTPUT)
        if res.too_large or res.timed_out or res.abandoned or res.returncode is None:
            raise ToolMissing("forgectl --version did not finish in time or answered too much")
        if res.returncode < 0 or res.returncode > 128:
            raise ToolMissing("forgectl --version was killed by a signal")
        required = f"{MIN_FORGECTL[0]}.{MIN_FORGECTL[1]}"
        parsed = parse_version(res.stdout) if res.returncode == 0 else None
        if parsed is None:
            raise ToolTooOld("unknown", required)
        if not version_accepted(parsed):
            raise ToolTooOld(".".join(map(str, parsed)), required)
        self._version = ToolVersion(".".join(map(str, parsed)), *parsed)
        return self._version

    def prs(
        self,
        *,
        host: str | None = None,
        repo: str | None = None,
        author: str | None = None,
        state: str | None = None,
        updated_since: datetime | None = None,
        limit: int | None = None,
    ) -> Outcome:
        argv = prs_argv(
            timeout_ms=self.timeout_ms,
            host=host,
            repo=repo,
            author=author,
            state=state,
            updated_since=updated_since,
            limit=limit,
        )
        return self._call("prs", argv)

    def snapshot(self, ref: PrRef) -> Outcome:
        return self._call("snapshot", snapshot_argv(ref, self.timeout_ms))

    def show(self, ref: PrRef) -> Outcome:
        return self._call("show", show_argv(ref, self.timeout_ms))

    def compare(self, ref: PrRef, from_sha: str, to_sha: str, *, no_diff: bool = False) -> Outcome:
        argv = compare_argv(ref, from_sha, to_sha, no_diff=no_diff, timeout_ms=self.timeout_ms)
        return self._call("compare", argv, Expect(compare=(from_sha, to_sha, no_diff)))

    def whoami(self) -> Outcome:
        return self._call("whoami", whoami_argv(self.timeout_ms))
