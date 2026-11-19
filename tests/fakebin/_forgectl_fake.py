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
tests/forgectl_support.py is the driver that writes both and reads the log.

Not modeled, on purpose (see docs/plan.md and the report of issue 6): sanitizing of text (the
scenario data must already be sanitized), the retry loop and its timing (a scripted failure is
the final answer after retries), `log.jsonl`, the real config file (only its existence is
checked) and `FORGECTL_*` environment variables.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import time
from argparse import Namespace
from collections.abc import Callable
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

# Commands real forgectl has that this fake does not model.
UNMODELED_COMMANDS = frozenset(
    {
        "snapshots", "poll", "diff", "files", "comments", "reviews", "ci", "access", "merge",
        "review", "comment", "reply", "resolve", "unresolve", "approve", "browse", "export",
    }
)  # fmt: skip

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
REQUIRED_DETAILS_BY_CODE = {
    "config_invalid": ("key",),
    "host_rejected": ("status",),
    "tool_missing": ("install",),
    "wrong_host_response": ("observed",),
}
REQUIRED_DETAILS_BY_REASON = {"not_open": ("state",), "stale_base": ("base_ref",)}


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


class _Version(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kw: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS)

    def __call__(self, parser: Any, namespace: Namespace, values: Any, option_string: Any = None):
        raise VersionRequested


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise usage(message)

    def _get_option_tuples(self, option_string: str) -> list[Any]:
        return []  # no prefix matching


def _globals(parser: argparse.ArgumentParser, *, sub: bool) -> None:
    flag_default: Any = argparse.SUPPRESS if sub else False
    value_default: Any = argparse.SUPPRESS if sub else None
    parser.add_argument("--json", action=_OnceFlag, default=flag_default)
    parser.add_argument("--verbose", action=_OnceFlag, default=flag_default)
    parser.add_argument("--quiet", action=_OnceFlag, default=flag_default)
    parser.add_argument(
        "--config", metavar="PATH", type=nonempty, action=_Once, default=value_default
    )
    parser.add_argument(
        "--timeout", metavar="D", type=duration, action=_Once, default=value_default
    )


def _once(parser: argparse.ArgumentParser, flag: str, **kw: Any) -> None:
    kw.setdefault("default", None)
    parser.add_argument(flag, action=_Once, **kw)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="forgectl", allow_abbrev=False, add_help=False)
    parser.add_argument("--version", action=_Version)
    _globals(parser, sub=False)
    for flag in ("--yes", "--dry-run", "--actor", "--decision-id", "--override-escalation"):
        parser.add_argument(flag, action=_MustFollowCommand)
    subs = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, allow_abbrev=False, add_help=False)
        _globals(sub, sub=True)
        return sub

    p = add("prs")
    _once(p, "--repo", type=parse_repo, metavar="ID")
    _once(p, "--host", type=parse_host, metavar="HOST")
    _once(p, "--author", type=parse_login, metavar="me|USER")
    _once(p, "--review-requested-from", dest="review_requested_from", type=parse_login)
    _once(p, "--state", choices=["open", "closed", "merged", "all"], default=Implicit("open"))
    draft = p.add_mutually_exclusive_group()
    draft.add_argument("--draft", dest="draft", action=_OnceConst, const=True, default=None)
    draft.add_argument("--no-draft", dest="draft", action=_OnceConst, const=False)
    _once(p, "--updated-since", dest="updated_since", type=time_or_duration)
    _once(p, "--limit", type=limit, default=100)
    _once(p, "--query", type=str)  # grammar of the query is not modeled: exit 98 after parsing
    p.add_argument("--default-query", dest="default_query", action=_OnceFlag)

    def pr_arg(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("pr", type=parse_pr)

    pr_arg(add("show"))
    p = add("snapshot")
    pr_arg(p)
    p.add_argument("--no-bodies", dest="no_bodies", action=_OnceFlag)
    p = add("compare")
    pr_arg(p)
    p.add_argument("--from", dest="from_sha", type=parse_sha, required=True, action=_Once)
    p.add_argument("--to", dest="to_sha", type=parse_sha, required=True, action=_Once)
    p.add_argument("--no-diff", dest="no_diff", action=_OnceFlag)
    p = add("whoami")
    _once(p, "--host", type=parse_host, metavar="HOST")
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


def first_command(argv: list[str]) -> str | None:
    """The first positional token, skipping the global flags that take a value."""
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token in ("--config", "--timeout"):
            skip = True
        elif not token.startswith("-"):
            return token
    return None


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


class Session:
    """One fake invocation: the scenario, the rule counters and the call log."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        path = directory / "scenario.json"
        if not path.is_file():
            raise FakeBug(f"no scenario at {path}: the test did not set up the fake")
        self.scenario: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self.call_id = f"{os.getpid()}-{time.time_ns()}"
        self.hosts = Hosts(self.scenario)

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

    def log_descendant(self, pid: int) -> None:
        self._append(
            {"phase": "descendant", "id": self.call_id, "pid": pid, "start_ticks": start_ticks(pid)}
        )

    # -- rules
    def take_rule(self, ctx: dict[str, Any]) -> dict[str, Any] | None:
        """The action of the first rule that matches `ctx` and still fires, or None."""
        rules: list[dict[str, Any]] = self.scenario.get("rules", [])
        if not rules:
            return None
        lock_path = self.dir / "state.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state_path = self.dir / "state.json"
            state: dict[str, Any] = (
                json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            )
            chosen: dict[str, Any] | None = None
            for index, rule in enumerate(rules):
                if not _matches(rule.get("match", {}), ctx):
                    continue
                counter = state.setdefault(str(index), {"seen": 0, "fired": 0})
                counter["seen"] += 1
                if counter["seen"] <= int(rule.get("after", 0)):
                    continue
                times = rule.get("times")
                if times is not None and counter["fired"] >= int(times):
                    continue
                counter["fired"] += 1
                chosen = rule["action"]
                break
            state_path.write_text(json.dumps(state), encoding="utf-8")
        return chosen


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


class Output:
    """What a run prints: collected, so the call log can record stdout."""

    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""
        self.kind = "ok"

    def result(self, data: dict[str, Any], *, json_mode: bool) -> None:
        self.stdout += (dumps(data) if json_mode else format_human(data)) + "\n"

    def error(self, err: ForgectlError, *, command: str | None, json_mode: bool) -> None:
        self.kind = "error"
        if err.code == "partial_results" and command in ("prs", "whoami") and err.output:
            self.kind = "partial"
            self.result(err.output, json_mode=json_mode)
        elif json_mode:
            self.stdout += dumps(err.envelope()) + "\n"
        self.stderr += error_text(err) + "\n"


# --- scripted actions --------------------------------------------------------------------


def error_from_action(action: dict[str, Any]) -> ForgectlError:
    return ForgectlError(
        action["code"],
        action.get("message", "scripted failure"),
        reason=action.get("reason"),
        details=action.get("details"),
        retry_after=action.get("retry_after"),
    )


def pre_action(session: Session, action: dict[str, Any], args: Namespace | None) -> None:
    """Actions that replace the whole run (raise or never return) or only slow it down."""
    kind = action["kind"]
    delay = float(action.get("delay", 0) or 0)
    if delay:
        time.sleep(min(delay, MAX_HANG_SECONDS))
    if kind == "error":
        raise error_from_action(action)
    if kind == "hang":
        hang(session, action, args)
    if kind == "signal":
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), int(action["signum"]))
        time.sleep(MAX_HANG_SECONDS)


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
    if action.get("descendant"):
        spawn_pipe_holder(session, seconds)
    budget = getattr(args, "timeout", None)
    if action.get("respect_timeout") and budget is not None:
        time.sleep(min(budget, seconds))
        raise ForgectlError("network_error", "the host did not answer within the --timeout budget")
    time.sleep(seconds)
    raise ForgectlError("network_error", "scripted hang ended")


def post_action(
    session: Session, action: dict[str, Any] | None, text: str, out: Output
) -> tuple[str, str]:
    """Actions that change a result that was computed normally; returns (stdout, kind)."""
    if action is None:
        return text, out.kind
    kind = action["kind"]
    if kind == "pad":
        data = json.loads(text)
        field = "body" if "body" in data else "diff" if isinstance(data.get("diff"), str) else None
        if field is None:
            raise Unmodeled("pad needs a result with a body or a diff (snapshot, show, compare)")
        data[field] = str(data[field]) + "x" * int(action["bytes"])
        return dumps(data) + "\n", "oversize"
    if kind == "drop":
        data = json.loads(text)
        for name in action["fields"]:
            data.pop(name, None)
        return dumps(data) + "\n", "contract_violation"
    if kind == "truncate":
        return text[: max(1, len(text) // 2)], "garbage"
    if kind == "orphan":
        spawn_pipe_holder(session, float(action.get("seconds", MAX_DESCENDANT_SECONDS)))
        return text, out.kind
    if kind == "slow":
        return text, out.kind
    raise FakeBug(f"unknown action kind {kind!r}")


POST_KINDS = frozenset({"pad", "drop", "truncate", "orphan", "slow"})
PRE_KINDS = frozenset({"error", "hang", "signal"})


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
    entry = _prs_store(session).get(pr.lower())
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
    if _RFC3339_RE.fullmatch(text):
        return datetime.fromisoformat(text.upper().replace("Z", "+00:00"))
    return _now(session) - timedelta(seconds=duration(text))


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _list_item(snapshot: dict[str, Any], listing: dict[str, Any]) -> dict[str, Any]:
    item = {key: json.loads(json.dumps(snapshot[key])) for key in LIST_ITEM_FIELDS}
    head = snapshot["head"]
    item["head"] = {k: head[k] for k in ("sha", "ref", "repo", "is_fork")}
    item["base"] = {"ref": snapshot["base"]["ref"]}
    item.update(listing)
    return item


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
    """A per-host scripted failure of `prs`; any action that is not an error acts at once."""
    action = session.take_rule(
        {"command": "prs", "host": host, "argv": argv, "repo": str(args.repo or "") or None}
    )
    if action is None:
        return None
    if action["kind"] == "error":
        delay = float(action.get("delay", 0) or 0)
        if delay:
            time.sleep(min(delay, MAX_HANG_SECONDS))
        return error_from_action(action)
    pre_action(session, action, args)
    if action["kind"] in POST_KINDS:
        session.pending_post = action  # type: ignore[attr-defined]
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
    scenario_hosts = hosts.declared
    hidden = [str(h).lower() for h in session.scenario.get("hidden_repos", [])]
    if repo is not None and repo.lower() in hidden:
        return _prs_result([], True, [], queried, session, args)

    errors: list[dict[str, str]] = []
    failures: list[ForgectlError] = []
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for host in queried:
        failure = _host_failure(session, host, args, argv)
        if failure is not None:
            if len(queried) == 1:
                raise failure
            failures.append(failure)
            errors.append({"host": host, "code": failure.code, "message": failure.message})
            continue
        me = (scenario_hosts.get(host) or {}).get("me")
        for entry in session.scenario.get("prs", {}).values():
            snapshot = entry["snapshot"]
            if not str(snapshot["pr"]).lower().startswith(host + "/"):
                continue
            if repo is not None and str(snapshot["repo"]).lower() != repo.lower():
                continue
            if str(snapshot["repo"]).lower() in hidden:
                continue
            item = _list_item(snapshot, entry.get("listing", {}))
            if not _matches_filters(item, snapshot, args, me):
                continue
            if args.updated_since and _stamp(item["updated_at"]) < _since(
                session, args.updated_since
            ):
                continue
            matched.append((item, snapshot))
    matched.sort(key=lambda pair: pair[0]["pr"])
    matched.sort(key=lambda pair: _stamp(pair[0]["updated_at"]), reverse=True)
    items = [pair[0] for pair in matched]
    complete = len(items) <= int(args.limit) and not errors
    items = items[: int(args.limit)]
    result = _prs_result(items, complete, errors, queried, session, args)
    if failures:
        raise ForgectlError(
            "partial_results",
            f"{len(failures)} of {len(queried)} hosts failed",
            output=result,
        )
    return result


def _prs_result(
    items: list[dict[str, Any]],
    complete: bool,
    errors: list[dict[str, str]],
    queried: list[str],
    session: Session,
    args: Namespace,
) -> dict[str, Any]:
    readings = []
    for host in queried:
        limit_info = (session.hosts.declared.get(host) or {}).get("rate_limit") or {}
        readings.append(
            {
                "host": host,
                "remaining": limit_info.get("remaining"),
                "reset_at": limit_info.get("reset_at"),
            }
        )
    known = [r for r in readings if r["remaining"] is not None]
    if known:
        rate = min(known, key=lambda r: r["remaining"])
    elif readings:
        rate = {"host": readings[0]["host"], "remaining": None, "reset_at": None}
    else:
        rate = {"host": None, "remaining": None, "reset_at": None}
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
        action = session.take_rule({"command": "whoami", "host": host, "argv": argv})
        if action is not None:
            if action["kind"] == "error":
                failure = error_from_action(action)
                errors.append({"host": host, "code": failure.code, "message": failure.message})
                continue
            pre_action(session, action, args)
        declared = hosts.declared.get(host) or {}
        who = declared.get("whoami") or {}
        entry = {
            "host": host,
            "backend": hosts.backend(host),
            "login": who.get("login"),
            "id": who.get("id"),
            "me": who.get("me", declared.get("me")),
            "me_matches_login": who.get("me_matches_login"),
            "token_source": who.get("token_source", "none"),
            "scopes": who.get("scopes"),
        }
        entries.append(entry)
        if entry["me_matches_login"] is False:
            out.stderr += (
                f"forgectl: warning: {host}: me is {entry['me']} but the authenticated login is "
                f"{entry['login']}; fix hosts.{host}.me\n"
            )
    result = {"hosts": entries, "errors": errors}
    if errors:
        raise ForgectlError("partial_results", f"{len(errors)} hosts failed", output=result)
    return result


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
        session.log_start(argv)
        session.pending_post = None  # type: ignore[attr-defined]
        candidate = first_command(argv)
        if candidate in UNMODELED_COMMANDS:
            raise Unmodeled(f"forgectl {candidate} is not modeled by this fake")
        try:
            args = parse(argv)
        except VersionRequested:
            return _finish(session, out, version_run(session, argv, out), json_mode)
        json_mode = args.json
        command = args.command
        exit_code = execute(session, args, argv, out)
    except ForgectlError as err:
        out.error(err, command=command, json_mode=json_mode)
        exit_code = err.exit_code
    except FakeBug as bug:
        out.stdout = ""
        out.kind = "fake_refusal"
        out.stderr = f"fake-forgectl: {bug}\n"
        exit_code = FAKE_EXIT
    except SignalExit as signalled:
        out.kind = "signalled"
        exit_code = 128 + signalled.signum
    if session is None:
        write_text(sys.stderr, out.stderr)
        return exit_code
    return _finish(session, out, exit_code, json_mode)


def _finish(session: Session, out: Output, exit_code: int, json_mode: bool) -> int:
    session.log_end(exit_code, out.kind, out.stdout)
    write_text(sys.stdout, out.stdout)
    write_text(sys.stderr, out.stderr)
    return exit_code


def version_run(session: Session, argv: list[str], out: Output) -> int:
    action = session.take_rule({"command": "--version", "argv": argv})
    if action is not None:
        return raw_action(action, out)
    out.stdout = f"forgectl {VERSION}\n"
    return 0


def raw_action(action: dict[str, Any], out: Output) -> int:
    if action["kind"] == "error":
        raise error_from_action(action)
    if action["kind"] != "raw":
        raise FakeBug(f"a --version rule must be a raw or error action, not {action['kind']!r}")
    out.stdout = action.get("stdout", "")
    out.stderr = action.get("stderr", "")
    out.kind = "raw"
    return int(action.get("exit", 0))


def execute(session: Session, args: Namespace, argv: list[str], out: Output) -> int:
    command = args.command
    if args.config is not None and not Path(args.config).exists():
        raise ForgectlError(
            "config_invalid",
            f"config file not found: {_shown(args.config)}",
            details={"key": "config"},
        )
    pr: str | None = None
    if command in ("show", "snapshot", "compare"):
        pr = bind_pr(args.pr, session.hosts)
    elif command == "prs" and args.repo:
        bind_repo(args.repo, session.hosts)
    elif command in ("prs", "whoami") and args.host:
        session.hosts.backend(args.host)
    host = None
    if pr is not None:
        host = pr.split("/", 1)[0]
    ctx = {
        "command": command, "argv": argv, "pr": pr, "host": host or getattr(args, "host", None),
        "repo": (str(args.repo) if getattr(args, "repo", None) else None),
    }  # fmt: skip
    handler: Callable[[], dict[str, Any]]
    if command == "prs":
        handler = lambda: handle_prs(session, args, argv)  # noqa: E731
    elif command == "whoami":
        handler = lambda: handle_whoami(session, args, argv, out)  # noqa: E731
    else:
        assert pr is not None
        actions = {"show": handle_show, "snapshot": handle_snapshot, "compare": handle_compare}
        handler = lambda: actions[command](session, args, pr)  # noqa: E731
    if command in ("show", "snapshot", "compare"):
        action = session.take_rule(ctx)
        if action is not None:
            if action["kind"] in PRE_KINDS or action["kind"] == "raw":
                if action["kind"] == "raw":
                    return raw_action(action, out)
                pre_action(session, action, args)
            session.pending_post = action if action["kind"] in POST_KINDS else None  # type: ignore[attr-defined]
            if action["kind"] == "slow":
                time.sleep(min(float(action.get("seconds", 0)), MAX_HANG_SECONDS))
    elif command == "prs" or command == "whoami":
        pass
    result = handler()
    out.result(result, json_mode=args.json)
    text, out.kind = post_action(session, session.pending_post, out.stdout, out)  # type: ignore[attr-defined]
    out.stdout = text
    return 0


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_signal)
    return run(sys.argv[1:])
