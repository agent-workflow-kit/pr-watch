"""Command-line entry point: argparse, output, exit codes and the error envelope.

Shape of the frame (docs/interface.md: Commands, Exit codes, Output by exit code):

- `build_parser` declares every command and global flag. Values are parsed as plain strings;
  `validate_values` then checks every one with `refs` (before the config is read) and replaces it
  with its validated form: `PrRef`/`PrId` for references, `Label` for labels, lowercase hosts,
  canonical repos, ints for `--limit`. An owner default (`human:<me.name>`) is resolved after
  the config is read. Handlers see only validated values.
- `HANDLERS` maps a command path (`"watch add"`) to `handler(ctx, args) -> exit code`. A later
  issue replaces its command's stub with one small function. A handler prints its result with
  `ctx.emit` and returns 0 (or its own code, as `poll` does for exit 12 with a report), or raises
  `CliError` for a failure that gets the error envelope.
- `main` maps every failure to one envelope and one exit code: `CliError` as raised,
  `ConfigError` as `config_invalid` (naming the key, never a value), any other exception as
  `internal` with a fixed message (no traceback, no exception text).

Channels: a result goes to stdout (JSON with `--json`, text otherwise unless `--quiet`); an error
goes to stderr as one text line, and with `--json` also to stdout as the envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from . import __version__
from .config import ConfigError, LoadedConfig, load_config
from .refs import (
    RefError,
    parse_cursor,
    parse_duration,
    parse_every,
    parse_hook,
    parse_host,
    parse_label,
    parse_limit,
    parse_owner,
    parse_pr,
    parse_reference,
    parse_repo,
    parse_type,
    parse_watch_id,
    resolve_owner,
    watch_key,
)

PROG = "pr-watch"
DESCRIPTION = "Watches PRs for changes and exposes them as events and hooks."
MAX_MESSAGE_CHARS = 256

# The error codes of schemas/defs.json and the exit code each one maps to (Exit codes).
EXIT_CODES: dict[str, int] = {
    "usage": 2,
    "config_invalid": 2,
    "invalid_pr": 2,
    "not_found": 10,
    "host_failed": 12,
    "tool_missing": 12,
    "tool_too_old": 12,
    "contract": 12,
    "store_too_new": 12,
    "state_unsafe": 12,
    "refused": 20,
    "internal": 1,
}
# schemas/cli/error.json bounds on `details` strings; every other string is cut at 256.
_DETAIL_LIMITS = {"required": 32, "found": 32, "kind": 32}
_REQUIRED_DETAILS = {"tool_missing": ("install",), "tool_too_old": ("required", "found")}


# --- text sanitizing -------------------------------------------------------------------------


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


# --- errors and the envelope -----------------------------------------------------------------


class CliError(Exception):
    """A failure that ends the command with the error envelope and the exit code of `code`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        lower_error: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code {code!r}")
        if (code == "refused") != (reason is not None):
            raise ValueError("`reason` is set exactly when code is `refused`")
        for key in _REQUIRED_DETAILS.get(code, ()):
            if details is None or key not in details:
                raise ValueError(f"{code} needs details.{key}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.details = dict(details) if details else None
        self.lower_error = dict(lower_error) if lower_error is not None else None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]


class OutputError(Exception):
    """Writing to stdout failed (a closed pipe, a full disk). `main` ends with exit 1, quietly."""


class UsageError(CliError):
    """argparse rejected the command line; `usage` is the parser's usage text for stderr."""

    def __init__(self, message: str, usage: str = "", kind: str | None = None) -> None:
        super().__init__("usage", message, details={"kind": kind} if kind else None)
        self.usage = usage


def config_error_to_cli(exc: ConfigError) -> CliError:
    details = {
        name: one_line(value)
        for name, value in (("key", exc.key), ("source", exc.source))
        if value is not None
    }
    return CliError("config_invalid", str(exc), details=details or None)


def _clean_details(details: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: one_line(value, _DETAIL_LIMITS.get(key, MAX_MESSAGE_CHARS))
        if isinstance(value, str)
        else value
        for key, value in details.items()
    }


def _clean_lower(lower: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(lower)
    if isinstance(cleaned.get("message"), str):
        cleaned["message"] = one_line(cleaned["message"])
    return cleaned


def build_envelope(error: CliError | ConfigError) -> dict[str, Any]:
    """The `{"error": {...}}` document of schemas/cli/error.json."""
    if isinstance(error, ConfigError):
        error = config_error_to_cli(error)
    body: dict[str, Any] = {
        "code": error.code,
        "message": one_line(error.message),
        "reason": error.reason,
    }
    if error.details:
        body["details"] = _clean_details(error.details)
    if error.lower_error is not None:
        body["lower_error"] = _clean_lower(error.lower_error)
    return {"error": body}


# --- context and output ----------------------------------------------------------------------


@dataclass
class Context:
    json: bool
    quiet: bool
    verbose: bool
    loaded: LoadedConfig  # TODO(#10): write `loaded.warnings` to log.jsonl

    def emit(self, data: Any, human: str | None = None) -> None:
        """Print a command result: a JSON document with `--json`, else `human` unless `--quiet`."""
        try:
            if self.json:
                print(json.dumps(data), flush=True)
            elif human is not None and not self.quiet:
                print(scrub_text(human), flush=True)
        except OSError as exc:
            raise OutputError from exc


Handler = Callable[[Context, argparse.Namespace], int]


def _stub(issue: int) -> Handler:
    def handler(ctx: Context, args: argparse.Namespace) -> int:
        raise CliError("internal", f"not implemented yet (issue #{issue})")

    return handler


# One entry per command. A later issue replaces its stub with the real handler.
HANDLERS: dict[str, Handler] = {
    "watch add": _stub(18),  # TODO(#18): watch add
    "watch remove": _stub(17),  # TODO(#17): watch remove
    "label add": _stub(17),  # TODO(#17): label add
    "label remove": _stub(17),  # TODO(#17): label remove
    "list": _stub(19),  # TODO(#19): list
    "prune": _stub(20),  # TODO(#20): prune
    "poll": _stub(25),  # TODO(#25): poll (and the signal contract, exit 128+N)
    "events": _stub(27),  # TODO(#27): events
    "show": _stub(27),  # TODO(#27): show
    "export": _stub(27),  # TODO(#27): export
    "cron install": _stub(29),  # TODO(#29): cron install
    "cron uninstall": _stub(29),  # TODO(#29): cron uninstall
}


# --- parser ----------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise UsageError(_without_values(message), usage=self.format_usage())


_INVALID_CHOICE = re.compile(r"^(.*?)invalid choice: .*?(\(choose from .*\))$", re.S)
_EXPLICIT_ARGUMENT = re.compile(r"^(.*?)ignored explicit argument .*$", re.S)


def _without_values(message: str) -> str:
    """argparse messages with the user's tokens taken out: envelopes never echo argv values.
    Only our own option names and choices stay."""
    if match := _INVALID_CHOICE.match(message):
        return f"{match[1]}invalid choice {match[2]}"
    if match := _EXPLICIT_ARGUMENT.match(message):
        return f"{match[1]}does not take a value"
    return message


def _global_flags(*, suppress: bool) -> argparse.ArgumentParser:
    """The flags every command takes, valid before and after the command. On the command
    parsers they default to SUPPRESS so a value given before the command is not overwritten."""
    parent = _Parser(add_help=False)
    unset: Any = argparse.SUPPRESS if suppress else None
    parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="machine-readable JSON output",
    )
    parent.add_argument(
        "--config", metavar="PATH", default=unset, help="config file (replaces config.yaml)"
    )
    parent.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="extra diagnostics on stderr",
    )
    parent.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="no human output on success",
    )
    return parent


def _command(sub: Any, name: str, path: str, help_text: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        name, parents=[_global_flags(suppress=True)], help=help_text, description=help_text
    )
    parser.set_defaults(command=path, leaf=parser)
    return parser


def _group(sub: Any, name: str, help_text: str) -> Any:
    parser = sub.add_parser(
        name, parents=[_global_flags(suppress=True)], help=help_text, description=help_text
    )
    return parser.add_subparsers(dest=f"{name}_command", metavar="<command>", required=True)


def _add_filters(parser: argparse.ArgumentParser, *, pr: bool = True) -> None:
    parser.add_argument("--label", action="append", metavar="K=V", help="label filter (repeat)")
    if pr:
        parser.add_argument("--pr", action="append", metavar="REF", help="PR filter (repeat)")
    parser.add_argument("--type", action="append", metavar="T", help="event type (repeat)")


def _add_query_flags(parser: argparse.ArgumentParser, *, label: bool) -> None:
    parser.add_argument("--mine", action="store_true", help="query form: PRs authored by me")
    parser.add_argument("--open", action="store_true", help="query form: open PRs")
    parser.add_argument("--host", action="append", metavar="H", help="query form: host (repeat)")
    parser.add_argument("--repo", action="append", metavar="ID", help="query form: repo (repeat)")
    if label:
        parser.add_argument("--label", action="append", metavar="K=V", help="label (repeat)")
    parser.add_argument("--owner", metavar="O", help="owner of the reference")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, parents=[_global_flags(suppress=False)])
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="group", metavar="<command>", required=True)

    watch = _group(sub, "watch", "add or remove watches")
    p = _command(watch, "add", "watch add", "watch a PR, or the PRs a query matches")
    p.add_argument("pr", nargs="?", metavar="PR", help="the PR (per-PR form)")
    _add_query_flags(p, label=True)
    p.add_argument("--no-baseline", action="store_true", help="per-PR form: do not baseline now")
    p = _command(watch, "remove", "watch remove", "drop an owner's reference to a watch")
    p.add_argument("pr", nargs="?", metavar="PR", help="the PR (per-PR form)")
    _add_query_flags(p, label=False)

    p = _command(sub, "poll", "poll", "run one poll cycle")
    p.add_argument("--pr", metavar="REF", help="refresh only this PR")
    p.add_argument("--repo", metavar="ID", help="limit to this repo")
    p.add_argument("--full", action="store_true", help="snapshot every active PR in scope")
    p.add_argument("--no-hooks", action="store_true", help="store events, skip hooks")
    p.add_argument("--hook", action="append", metavar="NAME=CMD", help="extra hook (repeat)")

    p = _command(sub, "list", "list", "print watches and PRs")
    p.add_argument("--label", action="append", metavar="K=V", help="label filter (repeat)")
    p.add_argument("--owner", metavar="O", help="owner filter")
    p.add_argument("--pr", metavar="REF", help="one PR")
    p.add_argument("--status", choices=["active", "terminal", "unreachable"], help="PR status")

    p = _command(sub, "show", "show", "show one watched PR")
    p.add_argument("pr", metavar="PR")

    p = _command(sub, "events", "events", "print events after a cursor")
    _add_filters(p)
    p.add_argument("--since", metavar="CURSOR", help="cursor from a previous call")
    p.add_argument("--limit", metavar="N", help="maximum events (1 to 1000, default 100)")

    label = _group(sub, "label", "add or remove labels on a watch")
    for verb in ("add", "remove"):
        p = _command(label, verb, f"label {verb}", f"{verb} labels on a watch")
        p.add_argument("--watch", metavar="ID", help="name the watch by id instead of a PR")
        p.add_argument("--owner", metavar="O", help="owner of the labels")
        # "*": an option between the items leaves later ones to parse_known_args (_merge_leftovers).
        p.add_argument("items", nargs="*", metavar="PR K=V", help="[PR] then labels k=v...")

    p = _command(sub, "prune", "prune", "delete expired events, rows and leases")
    p.add_argument("--dry-run", action="store_true", help="report only")

    cron = _group(sub, "cron", "install or remove the crontab entry")
    p = _command(cron, "install", "cron install", "write the tagged crontab block")
    p.add_argument("--every", metavar="D", help="interval, e.g. 5m or 1h")
    _command(cron, "uninstall", "cron uninstall", "remove the tagged crontab block")

    p = _command(sub, "export", "export", "print stored events as JSON lines")
    p.add_argument("--since", metavar="D", help="events detected within the last D")
    _add_filters(p)
    return parser


# --- argument shapes (what argparse cannot express) --------------------------------------------


def _shape_error(kind: str, message: str) -> UsageError:
    return UsageError(message, kind=kind)


def _check_watch_target(args: argparse.Namespace) -> None:
    """`<pr>` or `--mine --open`, never both, with the flags of each form on their own form."""
    query = args.mine or args.open
    if args.pr is None:
        if not query:
            raise _shape_error("missing_target", "give a PR, or --mine --open")
        if not (args.mine and args.open):
            raise _shape_error("bad_combination", "--mine and --open must be given together")
        if getattr(args, "no_baseline", False):
            raise _shape_error("bad_combination", "--no-baseline applies to the per-PR form only")
    else:
        if query:
            raise _shape_error("bad_combination", "a PR cannot be combined with --mine or --open")
        if args.host or args.repo:
            raise _shape_error("bad_combination", "--host and --repo apply to the query form only")


def _merge_leftovers(args: argparse.Namespace, extras: list[str]) -> None:
    """Handle what parse_known_args left over. `label` items split by an option (`add PR --owner
    O k=v`) come back here and join the items; a lone `--` is dropped and every token after it is
    an item, dash-leading or not. Anything else is an unrecognized argument. Counting rule: the
    first unknown option and every token after it are counted (a value after an unknown option
    cannot be told from its argument); items before it were fine. The message never echoes the
    tokens."""
    if "--" in extras:
        at = extras.index("--")
        extras = [*extras[:at], *extras[at + 1 :]]
        tail = len(extras) - at  # tokens that followed the `--`
    else:
        tail = 0
    head = extras[: len(extras) - tail]
    first = next((i for i, e in enumerate(head) if e.startswith("-")), len(extras))
    if first == len(extras) and extras and args.command in ("label add", "label remove"):
        args.items = [*args.items, *extras]
        return
    if first < len(extras):
        raise UsageError(f"unrecognized arguments ({len(extras) - first})")
    if extras:
        raise UsageError(f"unrecognized arguments ({len(extras)})")


def _check_label(args: argparse.Namespace) -> None:
    items: list[str] = args.items
    delattr(args, "items")
    if args.watch is not None:
        args.pr, args.labels = None, items
    else:
        if not items:
            raise _shape_error("missing_target", "give a PR (or --watch ID) and labels k=v")
        args.pr, args.labels = items[0], items[1:]
    if not args.labels:
        raise _shape_error("missing_labels", "give at least one label k=v")


SHAPE_CHECKS: dict[str, Callable[[argparse.Namespace], None]] = {
    "watch add": _check_watch_target,
    "watch remove": _check_watch_target,
    "label add": _check_label,
    "label remove": _check_label,
}


# --- value validation (refs) --------------------------------------------------------------------


def _each(values: list[str] | None, parse: Callable[[str], Any]) -> list[Any] | None:
    return None if values is None else [parse(value) for value in values]


def _one(value: str | None, parse: Callable[[str], Any]) -> Any:
    return None if value is None else parse(value)


def _check_filter_hosts(args: argparse.Namespace) -> None:
    """A `--repo` whose host is not in a given `--host` list is a usage error."""
    if args.host and args.repo:
        hosts = set(args.host)
        if any(repo.partition("/")[0] not in hosts for repo in args.repo):
            raise _shape_error("bad_combination", "--repo names a host that --host does not list")


def _query_filters(args: argparse.Namespace) -> None:
    args.host = _each(args.host, parse_host)
    args.repo = _each(args.repo, parse_repo)
    _check_filter_hosts(args)
    watch_key(args.host or [], args.repo or [])  # refuses filters that make a key too long


def _check_watch_values(args: argparse.Namespace) -> None:
    args.pr = _one(args.pr, parse_pr if args.command == "watch add" else parse_reference)
    _query_filters(args)
    args.label = _each(getattr(args, "label", None), parse_label)
    args.owner = _one(args.owner, parse_owner)


def _check_poll_values(args: argparse.Namespace) -> None:
    args.pr = _one(args.pr, parse_pr)
    args.repo = _one(args.repo, parse_repo)
    args.hook = _each(args.hook, parse_hook)


def _check_list_values(args: argparse.Namespace) -> None:
    args.label = _each(args.label, parse_label)
    args.owner = _one(args.owner, parse_owner)
    args.pr = _one(args.pr, parse_reference)


def _check_show_values(args: argparse.Namespace) -> None:
    args.pr = parse_reference(args.pr)


def _check_filter_values(args: argparse.Namespace) -> None:
    args.label = _each(args.label, parse_label)
    args.pr = _each(args.pr, parse_reference)
    args.type = _each(args.type, parse_type)


def _check_events_values(args: argparse.Namespace) -> None:
    _check_filter_values(args)
    args.since = _one(args.since, parse_cursor)
    args.limit = _one(args.limit, parse_limit)


def _check_export_values(args: argparse.Namespace) -> None:
    _check_filter_values(args)
    args.since = _one(args.since, parse_duration)


def _check_label_values(args: argparse.Namespace) -> None:
    args.pr = _one(args.pr, parse_reference)
    args.watch = _one(args.watch, parse_watch_id)
    args.labels = [parse_label(item) for item in args.labels]
    args.owner = _one(args.owner, parse_owner)


def _check_cron_values(args: argparse.Namespace) -> None:
    args.every = _one(args.every, parse_every)


VALUE_CHECKS: dict[str, Callable[[argparse.Namespace], None]] = {
    "watch add": _check_watch_values,
    "watch remove": _check_watch_values,
    "poll": _check_poll_values,
    "list": _check_list_values,
    "show": _check_show_values,
    "events": _check_events_values,
    "export": _check_export_values,
    "label add": _check_label_values,
    "label remove": _check_label_values,
    "cron install": _check_cron_values,
}
# Commands whose `--owner` defaults to `human:<me.name>` on a terminal (Identifiers: Owner).
OWNER_DEFAULTED = frozenset({"watch add", "watch remove", "label add", "label remove"})


def validate_values(args: argparse.Namespace) -> None:
    """Replace every raw value in `args` with its validated form (refs). Raises RefError."""
    VALUE_CHECKS.get(args.command, lambda _: None)(args)


def ref_error_to_cli(exc: RefError, usage: str = "") -> CliError:
    """The envelope error of a refused value. The message is fixed text, never the value."""
    if exc.code == "usage":
        return UsageError(str(exc), usage=usage, kind=exc.kind)
    return CliError(exc.code, str(exc), details={"kind": exc.kind})


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# --- main ------------------------------------------------------------------------------------


def _wants_json(argv: Sequence[str]) -> bool:
    """Whether `--json` is among the options, for errors raised before parsing finished."""
    options = argv[: argv.index("--")] if "--" in argv else argv
    return "--json" in options


def _stderr_line(text: str) -> None:
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except OSError:
        pass  # nowhere left to report it


def _output_failed() -> int:
    """stdout cannot be written: say so once on stderr, make the shutdown flush quiet, exit 1."""
    _stderr_line(f"{PROG}: error: cannot write to stdout\n")
    # One-shot CLI: the process exits right after this, so repointing fd 1 at /dev/null is safe
    # and keeps the interpreter's final stdout flush from failing again (a second message and
    # exit code 120). Only done when sys.stdout is the real stream, never for a swapped-in one.
    try:
        sink = open(os.devnull, "w")  # noqa: SIM115 - kept open for the shutdown flush
        if sys.stdout is sys.__stdout__:
            os.dup2(sink.fileno(), 1)
        sys.stdout = sink
    except (OSError, ValueError):
        pass
    return 1


def _report(error: CliError, as_json: bool) -> int:
    if isinstance(error, UsageError) and error.usage:
        _stderr_line(error.usage)
    _stderr_line(f"{PROG}: error: {one_line(error.message)}\n")
    if as_json:
        try:
            print(json.dumps(build_envelope(error)), flush=True)
        except OSError:
            return _output_failed()
    return error.exit_code


def _execute(argv: Sequence[str]) -> int:
    args, extras = build_parser().parse_known_args(argv)
    leaf = args.leaf
    delattr(args, "leaf")
    try:
        _merge_leftovers(args, extras)
        SHAPE_CHECKS.get(args.command, lambda _: None)(args)
        if args.verbose and args.quiet:
            raise _shape_error("conflicting_flags", "--verbose and --quiet cannot be combined")
        validate_values(args)
    except RefError as exc:
        raise ref_error_to_cli(exc, leaf.format_usage()) from None
    except UsageError as exc:
        exc.usage = exc.usage or leaf.format_usage()
        raise
    defaulted = args.command in OWNER_DEFAULTED
    interactive = _stdin_is_tty()
    if defaulted and args.owner is None and not interactive:
        # Needs no config: refuse before the config is read, so a broken file cannot mask it.
        try:
            resolve_owner(None, interactive=False, me_name=None)
        except RefError as exc:
            raise ref_error_to_cli(exc, leaf.format_usage()) from None
    loaded = load_config(args.config)
    if defaulted:
        try:
            args.owner = resolve_owner(
                args.owner, interactive=interactive, me_name=loaded.config.me.name
            )
        except RefError as exc:
            raise ref_error_to_cli(exc, leaf.format_usage()) from None
    ctx = Context(json=args.json, quiet=args.quiet, verbose=args.verbose, loaded=loaded)
    return HANDLERS[args.command](ctx, args)


def main(argv: Sequence[str] | None = None) -> int:
    # todo(#25): catch KeyboardInterrupt and SIGTERM here and exit 128+N (the signal contract).
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _execute(args)
    except OutputError:
        return _output_failed()
    except CliError as exc:
        return _report(exc, _wants_json(args))
    except ConfigError as exc:
        return _report(config_error_to_cli(exc), _wants_json(args))
    except RefError as exc:  # raised by a handler that validates a stored value
        return _report(ref_error_to_cli(exc), _wants_json(args))
    except Exception as exc:
        # Never traceback or exception text: it can carry host text or values.
        name = type(exc).__name__
        return _report(
            CliError("internal", f"unexpected internal error ({name})"), _wants_json(args)
        )
