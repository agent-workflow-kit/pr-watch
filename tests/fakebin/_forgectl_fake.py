"""A fake forgectl, pinned to forgectl v0.55.3 (see tests/forgectl_pinned/PIN.json).

It is a stand-in for the real program in tests of pr-watch, not a library. Nothing here imports
forgectl; the grammar, outputs, exit codes, error envelope, stderr lines and signal behavior were
written from forgectl's interface document and its command line surface at that tag.

Models: `--version`, `prs` (all flags but `--query` and `--default-query`), `show`, `snapshot`,
`compare` and `whoami`, which are the commands pr-watch calls. It is loud, never lenient:

- argv real forgectl rejects gets real forgectl's answer (exit 2 and the usage envelope, or
  exit 20 `unknown_host` for a host that is neither built in nor declared);
- argv real forgectl accepts but this fake does not model (another command, `prs --query`, an
  unscripted `compare`) exits 98 with `fake-forgectl:` on stderr and an empty stdout, so a
  client that starts using it fails its test instead of passing against a guess.

Data and failure scripts come from a scenario directory (`FAKE_FORGECTL_DIR`, else
`$HOME/.fake-forgectl`) holding `scenario.json`; every call is appended to `calls.jsonl` there.
tests/forgectl_support.py is the driver that writes both and reads the log. The scenario is
validated once per run: an unknown rule match key, an action kind the command cannot honor, an
unknown action field, a malformed scenario or state file, or an error envelope real forgectl
cannot print for that command, all exit 98 (never a traceback, never a silent no-op).

Rules and counters: a rule's `after` and `times` count matching *asks*, and `prs` asks once per
host queried, so with two hosts a rule for `prs` without a `host` match counts each host's turn
in sorted host order. A `hang` of `prs` on one of several hosts ends as that host's `errors[]`
item (exit 12 `partial_results`), as a real host timeout would; a hang that never ends (huge
`seconds`) still hangs the process, bounded only by MAX_HANG_SECONDS.

Process behavior: the handlers for SIGINT, SIGTERM and SIGHUP are installed at once, whereas real
forgectl has a startup window of about 0.8 s before its handlers exist (a SIGTERM then kills it
by signal, status -15, not exit 143) and a 2 s KILL_GRACE when it ends a child on the way out.
The fake models neither window; `term_delay` (scenario option, seconds) makes it take that long
between the signal and exit 128+N, for a client that must tell a prompt exit from a slow one.
Real forgectl also caps a host answer at 32 MiB: a 40 MiB answer here (the `pad` action) tests
the client's own cap, it is not something real forgectl emits.

The program starts with `#!/usr/bin/env python3`: it needs a `python3` on PATH, which the test
sandbox bin provides (tests/conftest.py); a client under test must keep that PATH when it runs
`forgectl` or the fake cannot start.

Not modeled, on purpose (see docs/plan.md and the report of issue 6): sanitizing of text (the
scenario data must already be sanitized), the retry loop and its timing (a scripted failure is
the final answer after retries), `log.jsonl`, the contents of the real config file (only that
it can be read and is a YAML mapping is checked; drop-ins are not read) and `FORGECTL_*`
environment variables.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import sys
import time
from argparse import Namespace
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

VERSION = "0.55.3"
FAKE_EXIT = 98
DIR_ENV = "FAKE_FORGECTL_DIR"
BUILT_IN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}
MAX_HANG_SECONDS = 120.0
MAX_DESCENDANT_SECONDS = 30.0
LOG_STDOUT_LIMIT = 1 << 20

MODELED_COMMANDS = ("prs", "show", "snapshot", "compare", "whoami")
# Commands real forgectl has that this fake parses (so a bad command line gets real's exit 2)
# but does not model: a command line that parses exits 98.
UNMODELED_COMMANDS = (
    "snapshots", "poll", "diff", "files", "comments", "reviews", "ci", "access", "merge",
    "review", "comment", "reply", "resolve", "unresolve", "approve", "browse", "export",
)  # fmt: skip
MAX_FILE_BYTES = 256 * 1024

# --- errors (forgectl errors.py) ----------------------------------------------------------

EXIT_BY_CODE: dict[str, int] = {
    "internal": 1, "usage": 2, "config_invalid": 2, "invalid_pr": 2, "body_too_large": 2,
    "method_required": 2, "not_found": 10, "vetoed": 11, "auth_required": 12,
    "permission_denied": 12, "rate_limited": 12, "upstream_error": 12, "network_error": 12,
    "tool_missing": 12, "host_rejected": 12, "outcome_unknown": 12, "outcome_partial": 12,
    "merge_unverified": 12, "invalid_position": 12, "quick_action_ran": 12,
    "approved_stale_head": 12, "wrong_host_response": 12, "audit_unavailable": 12,
    "audit_failed": 12, "partial_results": 12, "unsupported_host_version": 12,
    "confirmation_required": 13, "escalated": 13, "refused": 20,
}  # fmt: skip
REFUSAL_REASONS = frozenset(
    {
        "unknown_host", "stale_head", "stale_base", "no_head_match", "not_open", "not_ready",
        "method_not_allowed", "auto_merge_unavailable", "self_approval", "not_resolvable",
        "too_large", "drafts_present", "pending_review_exists",
    }
)  # fmt: skip
# `details` keys real forgectl requires (errors.py of the pinned tag, copied whole).
REQUIRED_DETAILS_BY_CODE: dict[str, tuple[str, ...]] = {
    "method_required": ("allowed",),
    "invalid_position": ("invalid",),
    "outcome_partial": ("landed", "failed"),
    "host_rejected": ("status",),
    "quick_action_ran": ("ids",),
    "approved_stale_head": ("ids",),
    "audit_failed": ("outcome",),
    "body_too_large": ("index", "limit"),
    "config_invalid": ("key",),
    "tool_missing": ("install",),
    "wrong_host_response": ("observed",),
    "merge_unverified": ("observed", "head_sha"),
}
REQUIRED_DETAILS_BY_REASON: dict[str, tuple[str, ...]] = {
    "not_ready": ("blocked_by",),
    "drafts_present": ("count",),
    "stale_base": ("base_ref",),
    "not_open": ("state",),
    "self_approval": ("basis",),
    "method_not_allowed": ("allowed",),
    "auto_merge_unavailable": ("observed",),
}

# What a scripted error may be, per command: the codes and refusal reasons the interface says
# those five commands can end with (`partial_results` is made by the fake, never scripted).
_HOST_CODES = frozenset(
    {
        "internal", "usage", "config_invalid", "not_found", "auth_required", "permission_denied",
        "rate_limited", "upstream_error", "network_error", "tool_missing", "host_rejected",
        "wrong_host_response", "unsupported_host_version", "refused",
    }
)  # fmt: skip
ALLOWED_CODES: dict[str, frozenset[str]] = {
    "prs": _HOST_CODES,
    "whoami": _HOST_CODES,
    "show": _HOST_CODES | {"invalid_pr"},
    "snapshot": _HOST_CODES | {"invalid_pr"},
    "compare": _HOST_CODES | {"invalid_pr"},
    "--version": frozenset({"internal"}),
}
ALLOWED_REASONS: dict[str, frozenset[str]] = {
    "prs": frozenset({"unknown_host"}),
    "whoami": frozenset({"unknown_host"}),
    "show": frozenset({"unknown_host"}),
    "snapshot": frozenset({"unknown_host"}),
    "compare": frozenset({"unknown_host", "too_large"}),
    "--version": frozenset(),
}


def check_error_allowed(command: str, code: str, reason: str | None) -> None:
    """Refuse (FakeBug) an error code or refusal reason `command` cannot end with."""
    if code not in ALLOWED_CODES[command]:
        raise FakeBug(f"{command} cannot fail with {code!r}")
    if reason is not None and reason not in ALLOWED_REASONS[command]:
        raise FakeBug(f"{command} cannot be refused with reason {reason!r}")


class ForgectlError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
        output: dict[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_BY_CODE:
            raise FakeBug(f"unknown forgectl error code: {code!r}")
        if (code == "refused") != (reason is not None):
            raise FakeBug(f"reason is for code 'refused' only: {code!r}, {reason!r}")
        if reason is not None and reason not in REFUSAL_REASONS:
            raise FakeBug(f"unknown refusal reason {reason!r}")
        required = REQUIRED_DETAILS_BY_CODE.get(code, ()) + REQUIRED_DETAILS_BY_REASON.get(
            reason or "", ()
        )
        missing = [key for key in required if key not in (details or {})]
        if missing:
            raise FakeBug(f"{reason or code} needs details {missing}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.details = details
        self.retry_after = retry_after
        self.output = output

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE[self.code]

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.details:
            body["details"] = self.details
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return {"error": body}


class FakeBug(Exception):
    """The fake itself was misused (a bad scenario, an unmodeled request): exit 98."""


class Unmodeled(FakeBug):
    pass


class SignalExit(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def usage(message: str) -> ForgectlError:
    return ForgectlError("usage", message)


def refused(reason: str, message: str, **kw: Any) -> ForgectlError:
    return ForgectlError("refused", message, reason=reason, **kw)


# --- identifiers (forgectl identifiers.py, grammar and binding) ----------------------------

MAX_INPUT_CHARS = 2048
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = rf"(?P<host>{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?"
_SEG = r"[A-Za-z0-9._-]+"
_PROJECT = rf"(?P<project>{_SEG}(?:/{_SEG})*)"
_PROJECT_LAZY = rf"(?P<project>{_SEG}(?:/{_SEG})*?)"
_NUMBER = r"(?P<number>[1-9][0-9]{0,9})"
_TAIL = r"(?:\?(?P<query>[A-Za-z0-9._~=&-]*))?(?:#(?P<fragment>[A-Za-z0-9._~=&-]*))?"
_SHORT_SHA = r"[0-9A-Fa-f]{7,40}"
_A = re.ASCII
_HOST_RE = re.compile(_HOST, _A)
_REPO_RE = re.compile(rf"{_HOST}/{_PROJECT}", _A)
_PR_TEXT_RE = re.compile(rf"{_HOST}/{_PROJECT}#{_NUMBER}", _A)
_GITHUB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}"
    rf"/pull/{_NUMBER}(?:(?:/files|/commits(?:/{_SHORT_SHA})?|/checks)?/?|\.(?:diff|patch)/?){_TAIL}",
    _A,
)
_GITLAB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}(?:/-)?"
    rf"/merge_requests/{_NUMBER}(?:(?:/diffs|/commits|/pipelines)?/?){_TAIL}",
    _A,
)
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?", _A)
_GITLAB_LOGIN_RE = re.compile(r"[A-Za-z0-9_]|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,253}[A-Za-z0-9_-]", _A)
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}", _A)
_BAD_URL_SEGMENTS = frozenset({"-", "merge_requests"})


def _shown(text: str) -> str:
    return repr(text if len(text) <= 100 else text[:100] + "...")


def _normalize_host(host: str, port: str | None) -> str | None:
    host = host.lower()
    if port is not None:
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            return None
        if port == "443":
            port = None
    if port is None and host.startswith("www.") and host[4:] in BUILT_IN_HOSTS:
        host = host[4:]
    if len(host) > 253:
        return None
    return host if port is None else f"{host}:{port}"


def _clean_segments(project: str) -> bool:
    return not any(seg in (".", "..") for seg in project.split("/"))


class PrInput:
    def __init__(self, host: str, project: str, number: int, kind: str | None, raw: str) -> None:
        self.host, self.project, self.number, self.kind, self.raw = host, project, number, kind, raw

    def __str__(self) -> str:
        return f"{self.host}/{self.project}#{self.number}"


class RepoInput:
    def __init__(self, host: str, path: str) -> None:
        self.host, self.path = host, path

    def __str__(self) -> str:
        return f"{self.host}/{self.path}"


def parse_host(text: str) -> str:
    match = _HOST_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    host = _normalize_host(match["host"], match["port"]) if match else None
    if host is None:
        raise usage(f"not a valid host: {_shown(text)}")
    return host


def parse_pr(text: str) -> PrInput:
    kind: str | None = None
    match: re.Match[str] | None = None
    if len(text) <= MAX_INPUT_CHARS:
        if text.startswith("https://"):
            match, kind = _GITHUB_URL_RE.fullmatch(text), "pull"
            if match is None:
                match, kind = _GITLAB_URL_RE.fullmatch(text), "merge_requests"
        else:
            match = _PR_TEXT_RE.fullmatch(text)
    host = _normalize_host(match["host"], match["port"]) if match else None
    if match is None or host is None or not _clean_segments(match["project"]):
        raise ForgectlError(
            "invalid_pr", f"not a pull request URL or host/<project path>#N: {_shown(text)}"
        )
    if kind == "merge_requests" and _BAD_URL_SEGMENTS.intersection(match["project"].split("/")):
        raise ForgectlError(
            "invalid_pr", f"not a pull request URL or host/<project path>#N: {_shown(text)}"
        )
    return PrInput(host, match["project"], int(match["number"]), kind, text)


def parse_repo(text: str) -> RepoInput:
    match = _REPO_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    host = _normalize_host(match["host"], match["port"]) if match else None
    if match is None or host is None or not _clean_segments(match["project"]):
        raise usage(f"not a valid repo id (host/<project path>): {_shown(text)}")
    return RepoInput(host, match["project"])


def parse_login(text: str) -> str:
    if len(text) <= MAX_INPUT_CHARS and (
        _GITHUB_LOGIN_RE.fullmatch(text) or _GITLAB_LOGIN_RE.fullmatch(text)
    ):
        return text
    raise usage(f"not a valid login: {_shown(text)}")


def parse_sha(text: str) -> str:
    if len(text) != 40 or not _SHA_RE.fullmatch(text):
        raise usage(f"not a full 40-hex sha: {_shown(text)}")
    return text.lower()


class Hosts:
    """The closed host list: the built-ins plus the hosts the scenario declares."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.declared: dict[str, dict[str, Any]] = scenario.get("hosts", {})

    def backend(self, host: str) -> str:
        if host in BUILT_IN_HOSTS:
            return BUILT_IN_HOSTS[host]
        declared = self.declared.get(host)
        if declared is None:
            raise refused(
                "unknown_host", f"host {_shown(host)} is not built in or declared in the config"
            )
        return str(declared.get("backend") or "github")

    def in_scope(self) -> list[str]:
        return sorted(self.declared)

    def all_known(self) -> list[str]:
        return sorted(set(BUILT_IN_HOSTS) | set(self.declared))


def _segments_ok(backend: str, path: str) -> bool:
    segments = path.split("/")
    if backend == "github" and len(segments) != 2:
        return False
    checked = segments if backend == "gitlab" else segments[:1]
    return not any(seg.startswith("-") for seg in checked)


def bind_pr(pr: PrInput, hosts: Hosts) -> str:
    backend = hosts.backend(pr.host)
    invalid = ForgectlError("invalid_pr", f"not a valid {backend} pull request: {_shown(pr.raw)}")
    expected = "pull" if backend == "github" else "merge_requests"
    if pr.kind not in (None, expected) or not _segments_ok(backend, pr.project):
        raise invalid
    declared = hosts.declared.get(pr.host, {})
    prefix = declared.get("api_prefix")
    project = pr.project
    if pr.kind is not None and backend == "gitlab" and prefix:
        rest = "/" + project
        if not rest.startswith(prefix + "/"):
            raise invalid
        project = rest[len(prefix) + 1 :]
        if _BAD_URL_SEGMENTS.intersection(project.split("/")) or not project:
            raise invalid
    return f"{pr.host}/{project}#{pr.number}"


def bind_repo(repo: RepoInput, hosts: Hosts) -> str:
    backend = hosts.backend(repo.host)
    if not _segments_ok(backend, repo.path):
        raise usage(f"not a valid {backend} repo id: {_shown(str(repo))}")
    return f"{repo.host}/{repo.path}"


# --- argv surface (forgectl surface.py, the modeled part) ----------------------------------

_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?", re.ASCII)
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
_LIMIT_RE = re.compile(r"[0-9]{1,4}", re.ASCII)
_MAX_DURATION_SECONDS = 100 * 365 * 86400.0


def _bad(message: str) -> NoReturn:
    raise argparse.ArgumentTypeError(message)


def nonempty(value: str) -> str:
    if not value:
        _bad("must not be empty")
    return value


def duration(value: str) -> float:
    match = _DURATION_RE.fullmatch(value)
    seconds = float(match[1]) * _UNIT_SECONDS[match[2]] if match else float("nan")
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds <= 0:
        _bad(f"not a positive duration (seconds, or a number with ms, s, m, h or d): {value!r}")
    if seconds > _MAX_DURATION_SECONDS:
        _bad(f"longer than 100 years: {value!r}")
    return seconds


def time_or_duration(value: str) -> str:
    if _RFC3339_RE.fullmatch(value):
        try:
            datetime.fromisoformat(value.upper())
        except ValueError:
            _bad(f"not a real RFC 3339 time: {value!r}")
        return value
    duration(value)
    return value


def limit(value: str) -> int:
    if not _LIMIT_RE.fullmatch(value) or not 1 <= int(value) <= 1000:
        _bad(f"must be a whole number from 1 to 1000: {value!r}")
    return int(value)


class Implicit(str):
    """The default of `--state`."""


_SEEN: ContextVar[set[str] | None] = ContextVar("fake_forgectl_seen_flags", default=None)


def _note_flag(parser: argparse.ArgumentParser, dest: str, option: str | None) -> None:
    seen = _SEEN.get()
    if seen is None:
        return
    if dest in seen:
        parser.error(f"{option} given more than once")
    seen.add(dest)


class _Once(argparse.Action):
    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        _note_flag(parser, self.dest, option_string)
        setattr(namespace, self.dest, values)


class _OnceFlag(_Once):
    def __init__(self, option_strings: list[str], dest: str, default: Any = False, **kw: Any):
        super().__init__(option_strings, dest, nargs=0, default=default, **kw)

    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        super().__call__(parser, namespace, True, option_string)


class _OnceConst(_Once):
    def __init__(
        self, option_strings: list[str], dest: str, const: Any = None, default: Any = None, **kw
    ):
        super().__init__(option_strings, dest, nargs=0, const=const, default=default, **kw)

    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        super().__call__(parser, namespace, self.const, option_string)


class _MustFollowCommand(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kw: Any) -> None:
        super().__init__(
            option_strings, dest, nargs="?", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )

    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        parser.error(
            f"{option_string} must follow the command (forgectl <command> ... {option_string})"
        )


class VersionRequested(Exception):
    pass


class HelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _Version(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kw: Any) -> None:
        super().__init__(
            option_strings,
            dest,
            nargs=0,
            default=argparse.SUPPRESS,
            help="show program's version number and exit",
        )

    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        raise VersionRequested


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise usage(message)

    def print_help(self, file: Any = None) -> None:
        raise HelpRequested(self.format_help())

    def _get_option_tuples(self, option_string: str) -> list[Any]:
        return []  # no prefix matching


def _globals(parser: argparse.ArgumentParser, *, sub: bool) -> None:
    flag_default: Any = argparse.SUPPRESS if sub else False
    value_default: Any = argparse.SUPPRESS if sub else None
    parser.add_argument(
        "--json", action=_OnceFlag, default=flag_default, help="machine-readable output"
    )
    parser.add_argument(
        "--verbose", action=_OnceFlag, default=flag_default, help="more human output and log detail"
    )
    parser.add_argument("--quiet", action=_OnceFlag, default=flag_default, help="less human output")
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=nonempty,
        action=_Once,
        default=value_default,
        help="config file instead of the default",
    )
    parser.add_argument(
        "--timeout",
        metavar="D",
        type=duration,
        action=_Once,
        default=value_default,
        help="budget for host calls: seconds, or a number with ms, s, m, h or d",
    )


def _once(parser: argparse.ArgumentParser, flag: str, **kw: Any) -> None:
    kw.setdefault("default", None)
    parser.add_argument(flag, action=_Once, **kw)


def duration_or_off(value: str) -> float:
    if value == "0":
        return 0.0
    seconds = duration(value)
    if seconds < 1:
        _bad(f"an interval is at least 1s (or 0 for off): {value!r}")
    return seconds


_DEDUPE_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)


def dedupe_key(value: str) -> str:
    if not _DEDUPE_KEY_RE.fullmatch(value):
        _bad(f"not a valid dedupe key: {value!r}")
    return value


def pr_or_stdin(value: str) -> PrInput | str:
    return value if value == "-" else parse_pr(value)


def _writes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action=_OnceFlag, help="skip the confirmation prompt")
    parser.add_argument("--dry-run", action=_OnceFlag, help="every check, no write")
    _once(parser, "--actor", metavar="A", help="human:<user> or auto:<name>")
    parser.add_argument(
        "--decision-id",
        dest="decision_id",
        action="append",
        metavar="D",
        help="decision id for the audit record; repeatable",
    )
    parser.add_argument(
        "--override-escalation",
        action=_OnceFlag,
        help="human-only: count a hook's exit 13 as allow",
    )


def _expected(parser: argparse.ArgumentParser) -> None:
    _once(parser, "--expected-head", type=parse_sha, metavar="SHA", help="full 40-hex head sha")
    _once(parser, "--expected-base", type=str, metavar="REF", help="base branch name")


def _pr(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pr", type=parse_pr, help="URL, or host/<project path>#N")


def build_parser() -> argparse.ArgumentParser:
    """The command line of real forgectl at the pinned tag. The grammar of the commands this fake
    does not model is kept only as far as real's own errors need it (the positionals, the
    required flags and the value types that need no other module); help texts are real's."""
    parser = _Parser(
        prog="forgectl",
        description="Normalized access to pull/merge requests across GitHub and GitLab.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action=_Version)
    _globals(parser, sub=False)
    for flag in ("--yes", "--dry-run", "--actor", "--decision-id", "--override-escalation"):
        parser.add_argument(flag, action=_MustFollowCommand)
    subs = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=summary, description=summary, allow_abbrev=False)
        _globals(sub, sub=True)
        return sub

    p = add("prs", "list pull requests")
    _once(p, "--repo", type=parse_repo, metavar="ID")
    _once(p, "--host", type=parse_host, metavar="HOST")
    _once(p, "--author", type=parse_login, metavar="me|USER")
    _once(
        p,
        "--review-requested-from",
        dest="review_requested_from",
        type=parse_login,
        metavar="me|USER",
    )
    _once(p, "--state", choices=["open", "closed", "merged", "all"], default=Implicit("open"))
    draft = p.add_mutually_exclusive_group()
    draft.add_argument("--draft", dest="draft", action=_OnceConst, const=True, default=None)
    draft.add_argument("--no-draft", dest="draft", action=_OnceConst, const=False)
    _once(p, "--updated-since", dest="updated_since", type=time_or_duration, metavar="TIME")
    _once(p, "--limit", type=limit, default=100, metavar="N")
    _once(
        p,
        "--query",
        type=str,  # the grammar of the query is not modeled: exit 98 after parsing
        metavar="TEXT",
        help="filter and order the list with a browse query (a filter and ORDER BY)",
    )
    p.add_argument(
        "--default-query",
        dest="default_query",
        action=_OnceFlag,
        help="use the browse.query of the config as the query (not with --query)",
    )

    for name, summary in (
        ("show", "the normalized PR object"),
        ("diff", "the unified diff"),
        ("files", "the changed files"),
        ("comments", "review threads and comments"),
        ("reviews", "reviews and who approved"),
        ("ci", "checks on the head"),
    ):
        _pr(add(name, summary))

    p = add("snapshot", "the PR with reviews, threads and CI in one call")
    _pr(p)
    p.add_argument(
        "--no-bodies",
        dest="no_bodies",
        action=_OnceFlag,
        help="leave out the bodies of the reviews and the comments",
    )

    p = add("snapshots", "snapshots of many PRs from a few bulk requests")
    p.add_argument(
        "refs",
        nargs="+",
        type=pr_or_stdin,
        metavar="PR|-",
        help="URL or host/<project path>#N, repeatable; a single - reads them from stdin",
    )
    p.add_argument(
        "--bulk-only",
        dest="bulk_only",
        action=_OnceFlag,
        help="read only through the bulk path; list a PR it cannot read whole in failed",
    )
    p.add_argument(
        "--lookup",
        dest="lookup",
        action=_OnceFlag,
        help="read each PR by its own lookup, for a few PRs (GitLab: not through the lists;"
        " GitHub: also reads check required-ness, one extra request per PR)",
    )

    add("poll", "the change-detection tuple of every PR of mine, in a few requests")

    p = add("compare", "compare two commits of a PR")
    _pr(p)
    p.add_argument(
        "--from", dest="from_sha", type=parse_sha, required=True, action=_Once, metavar="SHA"
    )
    p.add_argument(
        "--to", dest="to_sha", type=parse_sha, required=True, action=_Once, metavar="SHA"
    )
    p.add_argument(
        "--no-diff",
        dest="no_diff",
        action=_OnceFlag,
        help="answer with the status only: no `diff` key is printed"
        " (GitHub also skips the diff request)",
    )

    p = add("access", "whether a user has write access to a repo")
    p.add_argument("repo", type=parse_repo)
    p.add_argument("user", type=parse_login)

    p = add("whoami", "who forgectl acts as on each host")
    _once(p, "--host", type=parse_host, metavar="HOST")

    p = add("merge", "merge a PR")
    _pr(p)
    p.add_argument("--auto", action=_OnceFlag, help="human-only: merge when checks pass")
    _expected(p)
    _once(p, "--method", choices=["squash", "merge", "rebase"])
    p.add_argument("--override-blocks", action=_OnceFlag, help="human-only: skip not_ready")
    _writes(p)

    p = add("review", "post a review from a JSON file")
    _pr(p)
    _once(p, "--file", type=nonempty, required=True, metavar="PATH|-", default=argparse.SUPPRESS)
    _expected(p)
    p.add_argument(
        "--allow-self", action=_OnceFlag, help="human-only: skip the self-approval check"
    )
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    def body_file(sub: argparse.ArgumentParser, *, required: bool) -> None:
        kw: dict[str, Any] = {"default": argparse.SUPPRESS} if required else {}
        _once(
            sub, "--body-file", dest="body_file", type=nonempty, required=required,
            metavar="PATH|-", **kw,
        )  # fmt: skip

    p = add("comment", "post a general comment")
    _pr(p)
    body_file(p, required=True)
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    p = add("reply", "reply to a comment, thread or review")
    _pr(p)
    _once(p, "--to", type=str, required=True, metavar="ID", default=argparse.SUPPRESS)
    body_file(p, required=True)
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    for name, summary in (("resolve", "resolve a review thread"), ("unresolve", "reopen a thread")):
        p = add(name, summary)
        _pr(p)
        _once(p, "--thread", type=str, required=True, metavar="ID", default=argparse.SUPPRESS)
        _writes(p)

    p = add("approve", "approve a PR")
    _pr(p)
    _expected(p)
    p.add_argument(
        "--allow-self", action=_OnceFlag, help="human-only: skip the self-approval check"
    )
    body_file(p, required=False)
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    p = add("browse", "terminal browser (needs the tui extra)")
    _once(p, "--focus", type=nonempty, metavar="PR")
    _once(p, "--interval", type=duration_or_off, metavar="D")
    _once(p, "--result-file", dest="result_file", type=nonempty, metavar="PATH")

    p = add("export", "audit records as JSONL")
    _once(p, "--since", type=time_or_duration, metavar="TIME")

    return parser


def parse(argv: list[str]) -> Namespace:
    parser = build_parser()
    token = _SEEN.set(set())
    try:
        args = parser.parse_args(argv)
    finally:
        _SEEN.reset(token)
    if args.verbose and args.quiet:
        raise usage("--verbose and --quiet cannot be combined")
    if args.command == "prs":
        if args.repo and args.host:
            raise usage("--repo and --host cannot be combined")
        query_given = args.query is not None or args.default_query
        if not (args.repo or args.author or args.review_requested_from or query_given):
            raise usage("without --repo, prs needs --author or --review-requested-from")
        if args.query is not None and args.default_query:
            raise usage("--query and --default-query cannot be combined")
    return args


def wants_json(argv: list[str]) -> bool:
    head = argv[: argv.index("--")] if "--" in argv else argv
    return any(arg == "--json" or arg.startswith("--json=") for arg in head)


# --- scenario, rules, log ----------------------------------------------------------------------


def scenario_dir(env: dict[str, str] | os._Environ[str]) -> Path:
    explicit = env.get(DIR_ENV)
    if explicit:
        return Path(explicit)
    home = env.get("HOME")
    if not home:
        raise FakeBug(f"{DIR_ENV} and HOME are both unset: no scenario")
    return Path(home) / ".fake-forgectl"


PRE_KINDS = frozenset({"error", "hang", "signal", "raw", "slow", "orphan"})
POST_KINDS = frozenset({"pad", "drop", "truncate"})
_COMMON_KINDS = PRE_KINDS | POST_KINDS
ACTION_KINDS: dict[str, frozenset[str]] = {
    "prs": _COMMON_KINDS,
    "show": _COMMON_KINDS,
    "snapshot": _COMMON_KINDS,
    "compare": _COMMON_KINDS,
    "whoami": _COMMON_KINDS - {"pad"},  # no field of `whoami` is free text to pad
    "--version": frozenset({"raw", "error"}),
}
ACTION_FIELDS: dict[str, frozenset[str]] = {
    "error": frozenset({"code", "message", "reason", "details", "retry_after"}),
    "hang": frozenset({"seconds", "ignore_term", "descendant", "respect_timeout"}),
    "signal": frozenset({"signum"}),
    "raw": frozenset({"stdout", "stderr", "exit"}),
    "slow": frozenset({"seconds"}),
    "orphan": frozenset({"seconds"}),
    "truncate": frozenset(),
    "drop": frozenset({"fields"}),
    "pad": frozenset({"bytes"}),
}
ACTION_REQUIRED: dict[str, tuple[str, ...]] = {
    "error": ("code",), "signal": ("signum",), "drop": ("fields",), "pad": ("bytes",),
}  # fmt: skip
_BASE_MATCH = frozenset({"command", "argv_has"})
MATCH_KEYS: dict[str, frozenset[str]] = {
    "prs": _BASE_MATCH | {"host", "repo"},
    "whoami": _BASE_MATCH | {"host"},
    "show": _BASE_MATCH | {"pr", "host"},
    "snapshot": _BASE_MATCH | {"pr", "host"},
    "compare": _BASE_MATCH | {"pr", "host"},
    "--version": _BASE_MATCH,
}


def validate_rules(rules: Any) -> list[dict[str, Any]]:
    """Check every rule once; a bad one is a FakeBug (exit 98) before any call is answered."""
    if not isinstance(rules, list):
        raise FakeBug("scenario `rules` must be a list")
    for index, rule in enumerate(rules):
        where = f"rule {index}"
        if not isinstance(rule, dict) or set(rule) - {"match", "action", "times", "after"}:
            raise FakeBug(f"{where}: a rule has only match, action, times and after")
        match, action = rule.get("match"), rule.get("action")
        if not isinstance(match, dict) or not isinstance(action, dict):
            raise FakeBug(f"{where}: match and action must be objects")
        command = match.get("command")
        if command not in MATCH_KEYS:
            raise FakeBug(f"{where}: unknown command {command!r}")
        unknown = set(match) - MATCH_KEYS[command]
        if unknown:
            raise FakeBug(f"{where}: {command} cannot match on {sorted(unknown)}")
        if "argv_has" in match and not (
            isinstance(match["argv_has"], list)
            and all(isinstance(t, str) for t in match["argv_has"])
        ):
            raise FakeBug(f"{where}: argv_has must be a list of strings")
        for name in ("times", "after"):
            value = rule.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise FakeBug(f"{where}: {name} must be a whole number")
        validate_action(command, action, where)
    return rules


def validate_action(command: str, action: dict[str, Any], where: str) -> None:
    kind = action.get("kind")
    if kind not in _COMMON_KINDS:
        raise FakeBug(f"{where}: unknown action kind {kind!r}")
    if kind not in ACTION_KINDS[command]:
        raise FakeBug(f"{where}: {command} cannot honor a {kind!r} action")
    unknown = set(action) - ACTION_FIELDS[kind] - {"kind", "delay"}
    if unknown:
        raise FakeBug(f"{where}: a {kind!r} action has no field {sorted(unknown)}")
    missing = [name for name in ACTION_REQUIRED.get(kind, ()) if name not in action]
    if missing:
        raise FakeBug(f"{where}: a {kind!r} action needs {missing}")
    if kind == "error":
        error = error_from_action(action)
        check_error_allowed(command, error.code, error.reason)


class Session:
    """One fake invocation: the scenario, the rule counters and the call log."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        path = directory / "scenario.json"
        if not path.is_file():
            raise FakeBug(f"no scenario at {path}: the test did not set up the fake")
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(scenario, dict):
            raise FakeBug(f"{path}: the scenario must be an object")
        self.scenario: dict[str, Any] = scenario
        self.rules = validate_rules(scenario.get("rules", []))
        delay = scenario.get("term_delay", 0)
        if isinstance(delay, bool) or not isinstance(delay, int | float) or delay < 0:
            raise FakeBug("term_delay must be a number of seconds, 0 or more")
        self.term_delay = min(float(delay), MAX_HANG_SECONDS)
        self.call_id = f"{os.getpid()}-{time.time_ns()}"
        self.hosts = Hosts(self.scenario)
        self.post_actions: list[dict[str, Any]] = []

    # -- log
    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=True) + "\n"
        with (self.dir / "calls.jsonl").open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(line)
            fh.flush()

    def log_start(self, argv: list[str]) -> None:
        self._append(
            {
                "phase": "start", "id": self.call_id, "argv": argv, "pid": os.getpid(),
                "start_ticks": start_ticks(os.getpid()), "ts": time.time(), "cwd": os.getcwd(),
                "env_names": sorted(os.environ),
            }
        )  # fmt: skip

    def log_end(self, exit_code: int, kind: str, stdout: str) -> None:
        self._append(
            {
                "phase": "end", "id": self.call_id, "exit": exit_code, "kind": kind,
                "stdout": stdout if len(stdout) <= LOG_STDOUT_LIMIT else None,
                "stdout_bytes": len(stdout), "ts": time.time(),
            }
        )  # fmt: skip

    def log_ready(self) -> None:
        """Say that the scripted behaviour is in place (SIGTERM ignored, descendant logged)."""
        self._append({"phase": "ready", "id": self.call_id, "ts": time.time()})

    def log_descendant(self, pid: int) -> None:
        self._append(
            {"phase": "descendant", "id": self.call_id, "pid": pid, "start_ticks": start_ticks(pid)}
        )

    # -- rules
    def take_rule(self, ctx: dict[str, Any]) -> dict[str, Any] | None:
        """The action of the first rule that matches `ctx` and still fires, or None.

        Signals are blocked while the counters are read and replaced (the file is swapped in
        whole), so a signal never leaves a half-written state; one that arrives meanwhile is
        delivered when the mask is restored."""
        if not self.rules:
            return None
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _TERMINATION_SIGNALS)
        try:
            return self._take_rule_locked(ctx)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

    def _take_rule_locked(self, ctx: dict[str, Any]) -> dict[str, Any] | None:
        with (self.dir / "state.lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state_path = self.dir / "state.json"
            state: dict[str, Any] = {}
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except ValueError:
                    raise FakeBug(f"{state_path} is not valid JSON") from None
                if not isinstance(state, dict):
                    raise FakeBug(f"{state_path} is not an object")
            chosen: dict[str, Any] | None = None
            for index, rule in enumerate(self.rules):
                if not _matches(rule.get("match", {}), ctx):
                    continue
                counter = state.setdefault(str(index), {"seen": 0, "fired": 0})
                counter["seen"] += 1
                if counter["seen"] <= int(rule.get("after") or 0):
                    continue
                times = rule.get("times")
                if times is not None and counter["fired"] >= int(times):
                    continue
                counter["fired"] += 1
                chosen = rule["action"]
                break
            temp = self.dir / f"state.json.{os.getpid()}.tmp"
            temp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temp, state_path)
        return chosen


_TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _matches(match: dict[str, Any], ctx: dict[str, Any]) -> bool:
    for key, wanted in match.items():
        if key == "argv_has":
            if not all(token in ctx["argv"] for token in wanted):
                return False
        elif key == "command":
            if ctx.get("command") != wanted:
                return False
        else:
            have = ctx.get(key)
            if have is None or str(have).lower() != str(wanted).lower():
                return False
    return True


def start_ticks(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[19]


# --- output (forgectl output.py) ----------------------------------------------------------


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True)


def format_human(data: dict[str, Any]) -> str:
    lines = []
    for key, value in data.items():
        text = value if isinstance(value, str) else dumps(value)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def write_text(stream: Any, text: str) -> bool:
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, stream.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError):
            pass
        return False
    return True


def error_text(err: ForgectlError) -> str:
    label = err.code if err.reason is None else f"{err.code} ({err.reason})"
    return f"forgectl: error: {label}: {err.message}"


class RawExit(Exception):
    """A scripted `raw` action: the run ends with exactly this stdout, stderr and exit status."""

    def __init__(self, action: dict[str, Any]) -> None:
        super().__init__("raw")
        self.action = action


class Output:
    """What a run prints: collected, so the call log can record stdout."""

    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.kind = "ok"
        self.posts: list[dict[str, Any]] = []  # the session's post actions, shared

    def result(self, data: dict[str, Any], *, json_mode: bool) -> None:
        """Print `data`, shaped by the scripted pad, drop and truncate actions."""
        for action in self.posts:
            if action["kind"] == "pad":
                data = pad_result(data, action)
                self.kind = "oversize"
            elif action["kind"] == "drop":
                data = {k: v for k, v in data.items() if k not in action["fields"]}
                self.kind = "contract_violation"
        text = (dumps(data) if json_mode else format_human(data)) + "\n"
        for action in self.posts:
            if action["kind"] == "truncate":
                text = text[: max(1, len(text) // 2)]
                self.kind = "garbage"
        self.stdout += text

    def error(self, err: ForgectlError, *, command: str | None, json_mode: bool) -> None:
        self.kind = "error"
        if err.code == "partial_results" and command in ("prs", "whoami") and err.output:
            self.kind = "partial"
            self.result(err.output, json_mode=json_mode)
        elif json_mode:
            self.stdout += dumps(err.envelope()) + "\n"
        self.stderr += error_text(err) + "\n"


def pad_result(data: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """The result with its free text `bytes` longer: the body, the diff, or the first title."""
    extra = "x" * int(action["bytes"])
    if "body" in data:
        data["body"] = (data["body"] or "") + extra
    elif isinstance(data.get("diff"), str):
        data["diff"] += extra
    elif data.get("items"):
        data["items"][0]["title"] += extra
    else:
        raise Unmodeled("pad needs a result with a body, a diff or at least one listed PR")
    return data


# --- scripted actions --------------------------------------------------------------------


def error_from_action(action: dict[str, Any]) -> ForgectlError:
    return ForgectlError(
        action["code"],
        action.get("message", "scripted failure"),
        reason=action.get("reason"),
        details=action.get("details"),
        retry_after=action.get("retry_after"),
    )


def run_action(session: Session, action: dict[str, Any], args: Namespace | None) -> None:
    """Act on a fired rule: the actions that replace the run raise or never return, a slow one
    waits, an orphan leaves a descendant; pad, drop and truncate are kept for the output."""
    kind = action["kind"]
    delay = float(action.get("delay", 0) or 0)
    if delay:
        time.sleep(min(delay, MAX_HANG_SECONDS))
    if kind == "error":
        raise error_from_action(action)
    if kind == "raw":
        raise RawExit(action)
    if kind == "hang":
        hang(session, action, args)
    if kind == "signal":
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), int(action["signum"]))
        time.sleep(MAX_HANG_SECONDS)
    if kind == "slow":
        time.sleep(min(float(action.get("seconds", 0)), MAX_HANG_SECONDS))
    if kind == "orphan":
        spawn_pipe_holder(session, float(action.get("seconds", MAX_DESCENDANT_SECONDS)))
    if kind in POST_KINDS and not any(seen is action for seen in session.post_actions):
        session.post_actions.append(action)  # once per rule, however many hosts it fired for


def spawn_pipe_holder(session: Session, seconds: float) -> None:
    """A descendant that keeps stdout and stderr open and ignores SIGTERM, for a bounded time."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            time.sleep(min(seconds, MAX_DESCENDANT_SECONDS))
        finally:
            os._exit(0)
    session.log_descendant(pid)


def hang(session: Session, action: dict[str, Any], args: Namespace | None) -> None:
    seconds = min(float(action.get("seconds", MAX_HANG_SECONDS)), MAX_HANG_SECONDS)
    if action.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not action.get("descendant"):
            session.log_ready()  # tests wait for this before signalling
    if action.get("descendant"):
        spawn_pipe_holder(session, seconds)
        if action.get("ignore_term"):
            session.log_ready()  # after the holder is logged, so a test that waits sees it
    budget = getattr(args, "timeout", None)
    if action.get("respect_timeout") and budget is not None:
        time.sleep(min(budget, seconds))
        raise ForgectlError("network_error", "the host did not answer within the --timeout budget")
    time.sleep(seconds)
    raise ForgectlError("network_error", "scripted hang ended")


# --- data and handlers --------------------------------------------------------------------

SNAPSHOT_ONLY = (
    "review_requests", "teams_visible", "reviews", "latest_by_reviewer", "threads", "ci",
    "activity", "rate_limit", "complete", "incomplete", "fetched_at",
)  # fmt: skip
LIST_ITEM_FIELDS = (
    "pr", "pr_id", "repo", "number", "url", "title", "author", "state", "draft", "labels",
    "created_at", "updated_at", "repo_id",
)  # fmt: skip


def _prs_store(session: Session) -> dict[str, dict[str, Any]]:
    """The scenario's PRs keyed by lowercase `pr`."""
    return {str(k).lower(): v for k, v in session.scenario.get("prs", {}).items()}


def _find(session: Session, pr: str) -> dict[str, Any]:
    store = _prs_store(session)
    entry = store.get(pr.lower())
    if entry is None:  # a renamed repo's old name still resolves (a host redirect), never listed
        target = session.scenario.get("redirects", {}).get(pr.lower())
        entry = store.get(str(target).lower()) if target is not None else None
    if entry is None:
        raise ForgectlError("not_found", "pull request not found")
    return entry


def _strip_bodies(snapshot: dict[str, Any]) -> dict[str, Any]:
    for review in snapshot.get("reviews", []):
        review.pop("body", None)
    for thread in snapshot.get("threads", []):
        for comment in thread.get("comments", []):
            comment.pop("body", None)
    return snapshot


def handle_snapshot(session: Session, args: Namespace, pr: str) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(_find(session, pr)["snapshot"]))
    return _strip_bodies(snapshot) if args.no_bodies else snapshot


def handle_show(session: Session, args: Namespace, pr: str) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(_find(session, pr)["snapshot"]))
    for key in SNAPSHOT_ONLY:
        snapshot.pop(key, None)
    snapshot.get("head", {}).pop("pushed_at", None)
    snapshot.get("head", {}).pop("pushed_at_source", None)
    return snapshot


def handle_compare(session: Session, args: Namespace, pr: str) -> dict[str, Any]:
    entry = _find(session, pr)
    for item in session.scenario.get("compares", []):
        if (
            str(item["pr"]).lower() == pr.lower()
            and item["from"] == args.from_sha
            and item["to"] == args.to_sha
        ):
            result: dict[str, Any] = {
                "pr": entry["snapshot"]["pr"],
                "from_sha": args.from_sha,
                "to_sha": args.to_sha,
                "status": item["status"],
            }
            if not args.no_diff:
                result["diff"] = item.get("diff")
            return result
    raise Unmodeled(f"unscripted compare of {pr} {args.from_sha} {args.to_sha}")


def _now(session: Session) -> datetime:
    fixed = session.scenario.get("now")
    if fixed:
        return datetime.fromisoformat(str(fixed).replace("Z", "+00:00"))
    return datetime.now(UTC)


def _since(session: Session, text: str) -> datetime:
    """`--updated-since` as an absolute UTC time to the second; outside the years 1 to 9999 in
    UTC is a usage error, as in real forgectl."""
    try:
        if _RFC3339_RE.fullmatch(text):
            moment = datetime.fromisoformat(text.upper())
        else:
            moment = _now(session) - timedelta(seconds=duration(text))
        return moment.astimezone(UTC).replace(microsecond=0)
    except (OverflowError, ValueError):
        raise usage(f"--updated-since is outside the years 1 to 9999 in UTC: {text!r}") from None


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _list_item(snapshot: dict[str, Any], listing: dict[str, Any]) -> dict[str, Any]:
    item = {key: json.loads(json.dumps(snapshot[key])) for key in LIST_ITEM_FIELDS}
    head = snapshot["head"]
    item["head"] = {k: head[k] for k in ("sha", "ref", "repo", "is_fork")}
    item["base"] = {"ref": snapshot["base"]["ref"]}
    item.update(listing)
    return item


def _ascii_lower(text: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


def glob_match(pattern: str, text: str) -> bool:
    """Real forgectl's glob: `*` is any run (empty and `/` included), `?` any one character,
    everything else literal, both sides compared ASCII case-insensitively."""
    pat, name = _ascii_lower(pattern), _ascii_lower(text)
    p = n = 0
    star, resume = -1, 0
    while n < len(name):
        if p < len(pat) and pat[p] != "*" and (pat[p] == "?" or pat[p] == name[n]):
            p += 1
            n += 1
        elif p < len(pat) and pat[p] == "*":
            star = p
            p += 1
            resume = n
        elif star >= 0:
            p = star + 1
            resume += 1
            n = resume
        else:
            return False
    while p < len(pat) and pat[p] == "*":
        p += 1
    return p == len(pat)


def _hidden(session: Session, repo: str) -> bool:
    patterns = session.scenario.get("hidden_repos", [])
    return any(glob_match(str(pattern), repo) for pattern in patterns)


def check_me_spelling(value: str | None, what: str) -> None:
    if value is not None and value != "me" and value.lower() == "me":
        raise usage(f"{what}: `me` is reserved; write it as `me`")


def bind_login(text: str, backend: str) -> None:
    pattern = _GITHUB_LOGIN_RE if backend == "github" else _GITLAB_LOGIN_RE
    if len(text) > MAX_INPUT_CHARS or not pattern.fullmatch(text):
        raise usage(f"not a valid {backend} login: {_shown(text)}")


def _me_of(session: Session, host: str) -> str | None:
    """The configured `me` of `host`, else the login its scripted `whoami` authenticates."""
    declared = session.hosts.declared.get(host) or {}
    return declared.get("me") or (declared.get("whoami") or {}).get("login")


def _matches_filters(
    item: dict[str, Any], snapshot: dict[str, Any], args: Namespace, me: str | None
) -> bool:
    state = str(args.state)
    if state != "all" and item["state"] != state:
        return False
    if args.draft is not None and item["draft"] != args.draft:
        return False
    if args.author is not None:
        wanted = me if args.author == "me" else args.author
        login = item["author"].get("login")
        if wanted is None or login is None or login.lower() != wanted.lower():
            return False
    if args.review_requested_from is not None:
        wanted = me if args.review_requested_from == "me" else args.review_requested_from
        names = [
            r["reviewer"]["login"].lower()
            for r in snapshot.get("review_requests", [])
            if r["reviewer"]["kind"] == "user"
        ]
        if wanted is None or wanted.lower() not in names:
            return False
    return True


def _host_failure(
    session: Session, host: str, args: Namespace, argv: list[str]
) -> ForgectlError | None:
    """The scripted outcome of one host of `prs`: its failure (an error, or a hang that ended),
    or None. Any other action acts at once, as for every command."""
    action = session.take_rule(
        {"command": "prs", "host": host, "argv": argv, "repo": str(args.repo or "") or None}
    )
    if action is None:
        return None
    try:
        run_action(session, action, args)
    except ForgectlError as failure:
        return failure
    return None


def handle_prs(session: Session, args: Namespace, argv: list[str]) -> dict[str, Any]:
    if args.query is not None or args.default_query:
        raise Unmodeled("prs --query and --default-query are not modeled")
    hosts = session.hosts
    repo = bind_repo(args.repo, hosts) if args.repo else None
    if repo is not None:
        queried = [str(args.repo.host)]
    elif args.host:
        hosts.backend(args.host)
        queried = [args.host]
    else:
        queried = hosts.in_scope()
    check_me_spelling(args.author, "--author")
    check_me_spelling(args.review_requested_from, "--review-requested-from")
    for host in queried:
        for login in (args.author, args.review_requested_from):
            if login is not None and login != "me":
                bind_login(login, hosts.backend(host))
    since = _since(session, args.updated_since) if args.updated_since else None
    if repo is not None and _hidden(session, repo):
        return _prs_result([], True, [], queried, [], session)

    errors: list[dict[str, str]] = []
    failures: list[tuple[str, ForgectlError]] = []
    answered: list[str] = []
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for host in queried:
        failure = _host_failure(session, host, args, argv)
        if failure is not None:
            failures.append((host, failure))
            errors.append({"host": host, "code": failure.code, "message": failure.message})
            continue
        answered.append(host)
        me = _me_of(session, host)
        if me is None and "me" in (args.author, args.review_requested_from):
            raise FakeBug(f"{host}: `me` needs a configured me or a scripted whoami login")
        for entry in session.scenario.get("prs", {}).values():
            snapshot = entry["snapshot"]
            if not str(snapshot["pr"]).lower().startswith(host + "/"):
                continue
            if repo is not None and str(snapshot["repo"]).lower() != repo.lower():
                continue
            if _hidden(session, str(snapshot["repo"])):
                continue
            item = _list_item(snapshot, entry.get("listing", {}))
            if not _matches_filters(item, snapshot, args, me):
                continue
            if since is not None and _stamp(item["updated_at"]) < since:
                continue
            matched.append((item, snapshot))
    if len(failures) == 1 and len(queried) == 1:
        raise failures[0][1]  # one target: the host's own error and envelope
    matched.sort(key=lambda pair: pair[0]["pr"])
    matched.sort(key=lambda pair: _stamp(pair[0]["updated_at"]), reverse=True)
    items = [pair[0] for pair in matched]
    complete = len(items) <= int(args.limit) and not errors
    items = items[: int(args.limit)]
    result = _prs_result(items, complete, errors, queried, answered, session)
    if failures:
        shown = ", ".join(f"{host} ({failure.code})" for host, failure in failures)
        raise ForgectlError(
            "partial_results",
            f"{len(failures)} of {len(queried)} hosts failed: {shown}",
            output=result,
        )
    return result


def _prs_result(
    items: list[dict[str, Any]],
    complete: bool,
    errors: list[dict[str, str]],
    queried: list[str],
    answered: list[str],
    session: Session,
) -> dict[str, Any]:
    """`rate_limit` is the lowest reading among the hosts that answered; with none, the one
    host asked (nulls), or all null when several were."""
    readings = []
    for host in answered:
        info = (session.hosts.declared.get(host) or {}).get("rate_limit") or {}
        if info.get("remaining") is not None:
            readings.append(
                {"host": host, "remaining": info["remaining"], "reset_at": info.get("reset_at")}
            )
    if readings:
        rate = min(readings, key=lambda r: r["remaining"])
    else:
        rate = {
            "host": queried[0] if len(queried) == 1 else None,
            "remaining": None,
            "reset_at": None,
        }
    return {
        "items": items,
        "complete": complete,
        "errors": errors,
        "hosts_queried": queried,
        "rate_limit": rate,
    }


def handle_whoami(
    session: Session, args: Namespace, argv: list[str], out: Output
) -> dict[str, Any]:
    hosts = session.hosts
    if args.host:
        hosts.backend(args.host)
        names = [args.host]
    else:
        names = hosts.all_known()
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for host in names:
        declared = hosts.declared.get(host) or {}
        who = declared.get("whoami") or {}
        token_source = who.get("token_source", "none")
        action = session.take_rule({"command": "whoami", "host": host, "argv": argv})
        if action is not None:
            try:
                run_action(session, action, args)
            except ForgectlError as failure:
                # a failed host still has its entry, with nothing known but how it signs in
                entries.append(
                    {
                        "host": host, "backend": hosts.backend(host), "login": None, "id": None,
                        "me": None, "me_matches_login": None, "token_source": token_source,
                        "scopes": None,
                    }
                )  # fmt: skip
                errors.append({"host": host, "code": failure.code, "message": failure.message})
                continue
        entry = {
            "host": host,
            "backend": hosts.backend(host),
            "login": who.get("login"),
            "id": who.get("id"),
            "me": who.get("me", declared.get("me")),
            "me_matches_login": who.get("me_matches_login"),
            "token_source": token_source,
            "scopes": who.get("scopes"),
        }
        entries.append(entry)
        if entry["me_matches_login"] is False:
            out.stderr += (
                f"forgectl: warning: {host}: me is {entry['me']} but the authenticated login is "
                f"{entry['login']}; prs uses me, posts and rules act as the authenticated account\n"
            )
    result = {"hosts": entries, "errors": errors}
    if errors:
        shown = ", ".join(f"{e['host']} ({e['code']})" for e in errors)
        raise ForgectlError(
            "partial_results", f"{len(errors)} of {len(names)} hosts failed: {shown}", output=result
        )
    return result


# --- the config file ----------------------------------------------------------------------


def _config_invalid(key: str, message: str) -> ForgectlError:
    return ForgectlError("config_invalid", f"{key}: {message}", details={"key": key})


def check_config(path_text: str) -> None:
    """`--config` as real forgectl reads it: a readable regular UTF-8 file of at most 256 KiB
    holding one YAML document that is a mapping. Its keys are not interpreted."""
    import yaml  # here: only a run with --config needs it
    from yaml.composer import ComposerError

    path = os.path.abspath(path_text)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise _config_invalid(path, "no such file, or a symbolic link to nothing") from None
    except OSError as err:
        raise _config_invalid(path, f"cannot be read: {err.strerror or 'error'}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _config_invalid(path, "not a regular file")
        data = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError as err:
        raise _config_invalid(path, f"cannot be read: {err.strerror or 'error'}") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _config_invalid(path, f"file too large (limit {MAX_FILE_BYTES // 1024} KiB)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _config_invalid(path, "not valid UTF-8") from None
    try:
        document = yaml.safe_load(text)
    except yaml.MarkedYAMLError as err:
        mark = err.problem_mark or err.context_mark
        at = f" at line {mark.line + 1}" if mark is not None else ""
        phrase = "more than one document" if isinstance(err, ComposerError) else "syntax error"
        raise _config_invalid(path, f"invalid YAML{at}: {phrase}") from None
    except yaml.YAMLError:
        raise _config_invalid(path, "invalid YAML") from None
    if document is not None and not isinstance(document, dict):
        raise _config_invalid(path, "the top level must be a mapping")


# --- main --------------------------------------------------------------------------------


def _raise_signal(signum: int, frame: Any) -> None:
    raise SignalExit(signum)


def run(argv: list[str], env: Any = None) -> int:
    environ = os.environ if env is None else env
    json_mode = wants_json(argv)
    out = Output()
    session: Session | None = None
    command: str | None = None
    exit_code = 0
    try:
        session = Session(scenario_dir(environ))
        out.posts = session.post_actions
        session.log_start(argv)
        try:
            args = parse(argv)
        except VersionRequested:
            return _finish(session, out, version_run(session, argv, out))
        except HelpRequested as helped:
            out.stdout = helped.text
            out.kind = "help"
            return _finish(session, out, 0)
        json_mode = args.json
        command = args.command
        exit_code = execute(session, args, argv, out)
    except ForgectlError as err:
        try:
            out.error(err, command=command, json_mode=json_mode)
            exit_code = err.exit_code
        except FakeBug as bug:  # a partial answer the scripted post actions cannot be applied to
            _refuse(out, str(bug))
            exit_code = FAKE_EXIT
    except RawExit as raw:
        exit_code = raw_answer(raw.action, out)
    except FakeBug as bug:
        _refuse(out, str(bug))
        exit_code = FAKE_EXIT
    except SignalExit as signalled:
        out.kind = "signalled"
        exit_code = 128 + signalled.signum
        _shut_down(session)
    except Exception as exc:  # a broken scenario or a bug here: loud, never a traceback
        _refuse(out, f"{type(exc).__name__}: {exc}")
        exit_code = FAKE_EXIT
    if session is None:
        write_text(sys.stderr, out.stderr)
        return exit_code
    return _finish(session, out, exit_code)


def _refuse(out: Output, message: str) -> None:
    out.stdout = ""
    out.kind = "fake_refusal"
    out.stderr = f"fake-forgectl: {message}\n"


def _shut_down(session: Session | None) -> None:
    """Take the scripted `term_delay` between a signal and the exit; a further signal while
    waiting is ignored."""
    if session is None or not session.term_delay:
        return
    for sig in _TERMINATION_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    time.sleep(session.term_delay)


def _finish(session: Session, out: Output, exit_code: int) -> int:
    """Print the answer, then log it: a signal that interrupts a blocked write ends the run as
    128+N and the log says so."""
    try:
        write_text(sys.stdout, out.stdout)
        write_text(sys.stderr, out.stderr)
    except SignalExit as signalled:
        exit_code, out.kind = 128 + signalled.signum, "signalled"
        for fd in (1, 2):  # what is still buffered must not block the interpreter's own exit
            discard_stream(fd)
        _shut_down(session)
    session.log_end(exit_code, out.kind, out.stdout)
    return exit_code


def discard_stream(fd: int) -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except OSError:
        pass


def version_run(session: Session, argv: list[str], out: Output) -> int:
    action = session.take_rule({"command": "--version", "argv": argv})
    if action is not None:
        return raw_answer(action, out)
    out.stdout = f"forgectl {VERSION}\n"
    return 0


def raw_answer(action: dict[str, Any], out: Output) -> int:
    """A scripted `raw` (or, for `--version`, `error`) answer; the status it exits with."""
    if action["kind"] == "error":
        raise error_from_action(action)
    out.stdout = action.get("stdout", "")
    out.stderr = action.get("stderr", "")
    out.kind = "raw"
    return int(action.get("exit", 0))


def execute(session: Session, args: Namespace, argv: list[str], out: Output) -> int:
    command = args.command
    if command in UNMODELED_COMMANDS:
        raise Unmodeled(f"forgectl {command} is not modeled by this fake")
    if args.config is not None:
        check_config(args.config)
    pr: str | None = None
    if command in ("show", "snapshot", "compare"):
        pr = bind_pr(args.pr, session.hosts)
    elif command == "prs" and args.repo:
        bind_repo(args.repo, session.hosts)
    elif command in ("prs", "whoami") and args.host:
        session.hosts.backend(args.host)
    result: dict[str, Any]
    if command == "prs":
        result = handle_prs(session, args, argv)
    elif command == "whoami":
        result = handle_whoami(session, args, argv, out)
    else:
        assert pr is not None
        ctx = {"command": command, "argv": argv, "pr": pr, "host": pr.split("/", 1)[0]}
        action = session.take_rule(ctx)
        if action is not None:
            run_action(session, action, args)
        handlers = {"show": handle_show, "snapshot": handle_snapshot, "compare": handle_compare}
        result = handlers[command](session, args, pr)
    out.result(result, json_mode=args.json)
    return 0


def main() -> int:
    for sig in _TERMINATION_SIGNALS:
        signal.signal(sig, _raise_signal)
    return run(sys.argv[1:])
